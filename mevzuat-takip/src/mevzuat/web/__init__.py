"""Onay paneli. JSON API (/api/...) ve derlenmiş React arayüzü (frontend/dist) buradan sunulur. Mailde link olmadığı için
onaylayıcı maildeki düğmeyle ya da kendisi girer.

Çalıştırmak için `uv run python -m mevzuat.cli panel` kullanılır, varsayılan adres 127.0.0.1:8000.
Arayüz frontend klasöründe `npm run build` ile frontend/dist klasörüne derlenir. Depoda derlenmiş hali durur, sunucuda Node gerekmez.
Gereken ayarlar aşağıda.
    MEVZUAT_GIZLI_ANAHTAR  oturum çerezini imzalar, en az 32 karakter olmalı ve gizli tutulmalı
    MEVZUAT_HTTPS=1        panel HTTPS arkasındaysa verilir, çerez sadece HTTPS'te gönderilir
    MEVZUAT_MFA=1          herkes için iki adımlı doğrulama (telefondaki doğrulayıcı uygulama) zorunlu olur
    MEVZUAT_DOKUMAN_ACIK=0 API dokümanı (Swagger) da anahtar ister, varsayılan açık (istekler her durumda anahtar ister)

Güvenlik modelinde JWT bilerek kullanılmadı.
  - Oturum imzalı, HttpOnly, SameSite=Strict bir çerezdir. JavaScript oturum bilgisine erişemez, tarayıcıda saklanan
    bir anahtar olmadığı için XSS olsa bile oturum çalınamaz.
  - CSRF için veri değiştiren her istek X-CSRF-Token başlığında oturuma özel token taşımalı, token'ı GET /api/oturum verir.
  - Arayüz ve API aynı adreste durur, CORS gerekmez, başka siteler API'yi çağıramaz.
"""

# Panelin sunucu tarafı (backend). Tarayıcıdaki React arayüzü buradaki /api/... adreslerine istek atar,
# bu dosya da veritabanından okuyup/yazıp JSON cevap verir. Ayrıca derlenmiş arayüz dosyalarını sunar.
# Her "@api.get/post/put(...)" satırı bir API adresi tanımlar, altındaki fonksiyon o adrese gelen isteği karşılar.
import html
import json
import logging
import os
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

# FastAPI, Python web çatısı. APIRouter, API adreslerini gruplar. Depends, her istekte önce çalışacak kontrol. HTTPException, hata cevabı.
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Security
from fastapi.openapi.utils import get_openapi
from fastapi.security import HTTPBearer
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool
# pydantic BaseModel gelen JSON'un yapısını tarif eder ve otomatik kontrol eder, eksik ya da yanlış tipte alan gelirse 422 hatası döner.
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
# SessionMiddleware, oturum bilgisini imzalı çerezde tutan katman.
from starlette.middleware.sessions import SessionMiddleware

from mevzuat import (
    alicilar,
    gecmis,
    kaynak_bulucu,
    kaynak_yonetimi,
    konu_yonetimi,
    tanimlar,
    zamanlama,
)
from mevzuat import panel_ayarlari
from mevzuat import rapor as rapor_modulu
from mevzuat.db import (
    AliciGrubu, ApiAnahtari, Denetim, KaynakTanimi, KonuTanimi, Kullanici, MenuOgesi, ParolaLinki, Rapor,
)
from mevzuat.http import make_client
from mevzuat.mail import DosyaGonderici, Mail, MailGonderici
from mevzuat.web import guvenlik, imza, panel_belgesi, portal
from mevzuat.web.guvenlik import denetle, giris_dene

log = logging.getLogger(__name__)

# Oturum 8 saat geçerli.
OTURUM_SURESI = 8 * 60 * 60  # saniye, bir iş günü
# Rol kuralları, kim ne yapabilir. "tam" sadece API anahtarlarının rolüdür (test ve entegrasyon için her şeyi yapar),
# panel kullanıcısı bu role sahip olamaz, panelde görev ayrılığı aynen geçerlidir.
TAM_ROL = "tam"
KARAR_ROLLERI = ("onaylayici", TAM_ROL)  # raporu onaylayıp reddedebilenler, panelde sadece onaylayıcı
GRUP_ROLLERI = ("admin", "onaylayici", TAM_ROL)  # alıcı gruplarını yönetebilenler (kullanıcının kararı, 2026-10-02)
AYAR_ROLLERI = ("admin", "onaylayici", TAM_ROL)  # kaynak/konu yönetimi, asıl kullanıcı onaylayıcı, admin kurtarma (2026-10-02)
# Kullanıcılar, ayarlar, denetim, API anahtarları, kaynak/konu geçmişinden "önceki hale döndür".
KURTARMA_ROLLERI = ("admin", TAM_ROL)
API_ROLU = "api"  # panele girer ama sadece API dokümanını görür, istek atmak için API anahtarı gerekir
# API kullanıcısının oturumla erişebildiği adresler, gerisi API anahtarı ister.
API_ROLU_ADRESLERI = ("/panel/api/menu", "/api/dokuman/openapi.json", "/api/dokuman/indir", "/panel/api/parolam")
# Dokümandaki Authorize penceresinde görünen açıklama.
IMZA_ACIKLAMASI = (
    "API anahtarınızı girin. Bu sayfa anahtarı göndermez, her isteği onunla imzalar.\n\n"
    "Kendi sunucunuzdan istek atarken de anahtar gönderilmez. Her isteğe şu başlıklar eklenir:\n\n"
    "- `X-Anahtar`: anahtarın ilk 10 karakteri\n"
    "- `X-Zaman`: şimdiki zaman, Unix saniyesi\n"
    "- `X-Numara`: her istekte yeni, 16-64 karakter rastgele değer (harf, rakam, - ve _)\n"
    "- `X-Imza`: aşağıdaki metnin anahtarla HMAC-SHA256 özeti, küçük harf hex\n\n"
    "İmzalanan metin, satırlar \\n ile birleşir: yöntem (GET), adres (/api/mevzuat), sorgu (q=altin&sayfa=1, "
    "yoksa boş), X-Zaman, X-Numara, gövdenin SHA-256 özeti (küçük harf hex, gövde yoksa boş metnin özeti).\n\n"
    "Aynı numara ikinci kez kabul edilmez, 5 dakikadan eski istek kabul edilmez, anahtar başına dakikada en fazla 300 istek."
)
# Dış sistemlerin adresleri bununla başlar. Panelin kendi arayüzünün adresleri /panel/api altında.
DIS_ON_EK = "/api/"
PANEL_ON_EK = "/panel/api"
# Doküman adresleri, /api altında ama imza istemez, panel oturumuyla da açılır.
DOKUMAN_ADRESLERI = ("/api/dokuman", "/api/dokuman/openapi.json", "/api/dokuman/indir")
# Tarayıcıda yeni sekmede açılabilen adresler, doküman sayfası ve indirilen doküman. Diğer API adresleri açılmaz.
TARAYICIDA_ACILANLAR = ("/api/dokuman", "/api/dokuman/indir")
# Dış sistemlerin API anahtarını gönderdiği başlık.
API_BASLIGI = "X-API-Anahtari"  # eski biçim, hâlâ kabul edilir ama dokümanda sadece "Authorization: Bearer" var
# Aynı anahtarın son kullanım zamanı en fazla dakikada bir yazılır.
SON_KULLANIM_ARALIGI = 60
# Panel oturumuyla API dokümanını görebilenler. Onaylayıcı görmez, dışarıdan gelen API anahtarıyla doküman yine açılır.
DOKUMAN_ROLLERI = ("admin", "api", TAM_ROL)
# Parola doğrulandıktan sonra MFA kodunu girmek için 5 dakika süre.
MFA_BEKLEME = 5 * 60  # saniye, parola doğrulandıktan sonra kodu girmek için süre
# Derlenmiş arayüzün klasörü, proje/frontend/dist.
VARSAYILAN_ARAYUZ = Path(__file__).parents[3] / "frontend" / "dist"

# Her cevaba eklenen güvenlik başlıkları (tarayıcıya "sadece kendi dosyalarımı çalıştır" vb. der).
GUVENLIK_BASLIKLARI = {
    # Sadece kendi dosyalarımız, dış script/stil/font yok, satır içi script yok. Başka sitede çerçeveye alınamaz.
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


# API dokümanında uç noktaların grupları, adresin /api/ sonrasındaki ilk parçasına göre.
API_GRUPLARI = {
    "oturum": "Oturum ve giriş", "giris": "Oturum ve giriş", "cikis": "Oturum ve giriş", "parolam": "Oturum ve giriş",
    "parola-linki": "Oturum ve giriş", "menu": "Menü", "raporlar": "Raporlar", "gruplar": "Alıcı grupları",
    "kaynaklar": "Kaynaklar", "kaynak-tipleri": "Kaynaklar", "konular": "Konular", "tarama": "Tarama",
    "zamanlama": "Tarama", "kullanicilar": "Kullanıcılar", "denetim": "Denetim kaydı", "ayarlar": "Ayarlar",
    "api-anahtarlari": "API anahtarları",
}

# Swagger sayfası. Dosyalar (frontend/public/swagger) kendi sunucumuzdan gelir, satır içi script yoktur, CSP bozulmaz.
SWAGGER_SAYFASI = """<!doctype html>
<html lang="tr">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mevzuat Takip API</title><link rel="stylesheet" href="/swagger/swagger-ui.css">
<link rel="stylesheet" href="/swagger/baslat.css"></head>
<body><div id="anahtar-girisi"></div><div id="swagger-ui"></div>
<script src="/swagger/swagger-ui-bundle.js"></script><script src="/swagger/baslat.js"></script></body>
</html>"""


# Panelin o anki işletim ayarları (mail göndericisi, PDF eki, MFA, panel adresi, pasif silme süresi).
@dataclass(frozen=True)
class Isletim:
    gonderici: MailGonderici
    ek_ekle: bool
    mfa_zorunlu: bool
    panel_adresi: str | None
    pasif_silme_gun: int
    dokuman_acik: bool
    api_imza: bool = False

    # SMTP ayarsızsa mailler sadece dosyaya yazılır, panel bunu "gönderildi" diye göstermemeli.
    @property
    def mail_kapali(self) -> bool:
        return isinstance(self.gonderici, DosyaGonderici)


# İndirilen doküman, Swagger dosyaları ve API tanımı içinde, internetsiz açılan tek HTML dosyası.
def _tek_dosya_dokuman(sema: dict, klasor: Path) -> bytes:
    css = (klasor / "swagger-ui.css").read_text(encoding="utf-8")
    js = (klasor / "swagger-ui-bundle.js").read_text(encoding="utf-8").replace("</script", "<\\/script")
    # JSON içindeki "</" script etiketini erken kapatmasın.
    veri = json.dumps(sema, ensure_ascii=False).replace("</", "<\\/")
    baslik = html.escape(sema.get("info", {}).get("title", "API"))
    return (f'<!doctype html><html lang="tr"><head><meta charset="utf-8"><title>{baslik}</title><style>{css}</style>'
            f'</head><body><div id="swagger-ui"></div><script>{js}</script>'
            f'<script>SwaggerUIBundle({{spec: {veri}, dom_id: "#swagger-ui"}});</script></body></html>').encode("utf-8")


# İstekteki API anahtarı. X-API-Anahtari başlığında ya da "Authorization: Bearer ..." biçiminde gelir, yoksa None.
def istek_anahtari(request: Request) -> str | None:
    if (anahtar := request.headers.get(API_BASLIGI)) is not None:
        return anahtar
    yetki = request.headers.get("authorization", "")
    return yetki[7:] if yetki[:7].lower() == "bearer " else None


# --- istek gövdeleri

# Aşağıdaki sınıflar, API'ye gelen isteklerin gövdeleri (JSON'da hangi alanlar olmalı). max_length, aşırı uzun veriyi reddet.
class GirisIstegi(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{"eposta": "onay@firma.com.tr", "parola": "parola-en-az-12"}]})
    eposta: str = Field(max_length=254)
    parola: str = Field(max_length=1024)


# MFA kodu isteği.
class KodIstegi(BaseModel):
    kod: str = Field(max_length=20)


# Onay/ret isteği.
class KararIstegi(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{"karar": "onayla", "dahil": [101, 102], "notu": "Vergi kalemine dikkat.", "gruplar": [1, 2], "ek_adresler": ["mudur@firma.com.tr"]}]})
    karar: Literal["onayla", "reddet"]
    dahil: list[int] = []
    notu: str = Field("", max_length=2000)
    # Onaylayıcının işaretlediği gruplar (None, iş koluna uyan bütün aktif gruplar) ve kişiye özel adresler.
    gruplar: list[int] | None = None
    ek_adresler: list[str] = Field(default_factory=list, max_length=50)


# Onaylanmış rapordan ek gönderim isteği, seçilen kalemler, gruplar, kişiye özel adresler ve not.
class EkGonderimIstegi(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{"dahil": [103], "notu": "Size de gelsin.", "gruplar": [3], "ek_adresler": []}]})
    dahil: list[int] = []
    notu: str = Field("", max_length=2000)
    gruplar: list[int] = []
    ek_adresler: list[str] = Field(default_factory=list, max_length=50)


# Panel ayarlarını kaydetme isteği. Değeri null olan ayar sıfırlanır (.env'e döner), gönderilmeyene dokunulmaz.
class AyarIstegi(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{"degisiklikler": {"smtp_gonderen_adi": "Uyum Birimi", "saklama_gun": 45, "panel_adresi": None}, "surum": 3}]})
    degisiklikler: dict[str, str | int | bool | list[str] | None] = Field(max_length=50)
    surum: int


# Deneme maili isteği.
class MailDenemeIstegi(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{"adres": "bilgi-islem@firma.com.tr"}]})
    adres: str = Field(max_length=200)


# Alıcı grubu ekleme/düzenleme isteği.
class GrupIstegi(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{"ad": "Kuyum Ekibi", "is_kollari": ["Kuyum", "Ortak"], "adresler": ["kuyum@firma.com.tr"], "aktif": True}]})
    ad: str = Field(max_length=200)
    is_kollari: list[str] = []
    adresler: list[str] = []
    aktif: bool = True


# Kullanıcı ekleme isteği.
class KullaniciIstegi(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{"eposta": "yeni@firma.com.tr", "ad": "Ad Soyad", "rol": "onaylayici"}]})
    eposta: str = Field(max_length=254)
    ad: str = Field(max_length=200)
    rol: Literal["admin", "onaylayici", "api"]


# API anahtarı üretme isteği. gun boşsa anahtar süresizdir.
class ApiAnahtariIstegi(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{"ad": "Portal entegrasyonu", "rol": "tam", "gun": 90}]})
    ad: str = Field(max_length=100)
    rol: Literal["tam", "admin", "onaylayici"]
    gun: int | None = None


# Kullanıcıyı aktif/pasif yapma isteği.
class AktiflikIstegi(BaseModel):
    aktif: bool


# Parola linki kontrol/kullanma isteği.
class ParolaLinkiIstegi(BaseModel):
    token: str = Field(max_length=100)
    parola: str = Field("", max_length=1024)  # kontrol isteğinde boş


# Kendi parolasını değiştirme isteği.
class ParolaDegistirIstegi(BaseModel):
    eski: str = Field(max_length=1024)
    yeni: str = Field(max_length=1024)


# Kaynak ekleme/düzenleme/deneme isteği.
class KaynakIstegi(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{"tip": "rss", "etiket": "TCMB Basın Duyuruları", "ayarlar": {"akis_url": "https://www.tcmb.gov.tr/wps/wcm/connect/TR/TCMB+TR/Bottom+Menu/Diger/RSS/Basin+Duyurulari"}, "varsayilan_konular": [], "aktif": True}]})
    tip: str = Field("", max_length=30)  # sadece eklerken, düzenlemede tip değişmez
    etiket: str = Field(max_length=300)
    ayarlar: dict = Field(default_factory=dict)
    varsayilan_konular: list[str] = Field(default_factory=list, max_length=50)
    aktif: bool = True
    surum: int | None = None  # düzenlemede zorunlu, formun açıldığı andaki sürüm


# Adresten kaynak bulma isteği.
class AdresIstegi(BaseModel):
    adres: str = Field(max_length=2000)


# Sadece sürüm numarası taşıyan istek (kaldır/geri getir/pasifleştir).
class SurumIstegi(BaseModel):
    surum: int


# Tarama saatleri isteği.
class ZamanlamaIstegi(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{"saatler": ["06:30", "12:00", "18:00"], "surum": 2}]})
    saatler: list[str] = Field(max_length=10)
    surum: int


# "Önceki hale döndür" isteği.
class GeriAlIstegi(BaseModel):
    denetim_id: int  # bu değişiklikten ÖNCEKİ hale dönülür
    surum: int


# Konu ekleme/düzenleme/önizleme isteği.
class KonuIstegi(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{"ad": "Döviz ve kambiyo", "is_kollari": ["Döviz/Altın"], "kelimeler": ["döviz", "politika faizi"], "haric": [], "dislanan": [], "aciklama": "Kur ve faiz kararları fiyatlamayı etkiler."}]})
    ad: str = Field(max_length=300)
    is_kollari: list[str] = Field(default_factory=list, max_length=50)
    kelimeler: list[str] = Field(default_factory=list, max_length=500)
    haric: list[str] = Field(default_factory=list, max_length=500)
    dislanan: list[str] = Field(default_factory=list, max_length=500)
    aciklama: str = Field("", max_length=2000)  # bu konu bizi neden ilgilendiriyor
    surum: int | None = None  # düzenlemede zorunlu
    id: int | None = None  # önizlemede, düzenlenen konu (yeni konuysa boş)


# --- JSON'a çevirme

# Tarihi "2026-10-05T18:30" biçiminde yazıya çevirir (yoksa None).
def _zaman(z) -> str | None:
    return z.isoformat(timespec="minutes") if z else None


# Giriş yapan kullanıcının arayüze gönderilen bilgisi, arayüz hangi menüyü göstereceğine bunlara bakarak karar verir.
def _kullanici_json(k: Kullanici) -> dict:
    return {"id": k.id, "ad": k.ad, "eposta": k.eposta, "rol": k.rol, "mfa_aktif": k.mfa_aktif,
            "karar_verebilir": k.rol in KARAR_ROLLERI, "grup_yonetebilir": k.rol in GRUP_ROLLERI,
            "ayar_yonetebilir": k.rol in AYAR_ROLLERI, "kurtarma_yapabilir": k.rol in KURTARMA_ROLLERI,
            "api_kullanicisi": k.rol == API_ROLU}


# Kullanıcılar sayfasındaki satır.
def _yonetim_json(k: Kullanici, davet_bekliyor: bool, pasif_silme_gun: int) -> dict:
    return {"id": k.id, "ad": k.ad, "eposta": k.eposta, "rol": k.rol, "aktif": k.aktif,
            "son_giris": _zaman(k.son_giris), "olusturuldu": _zaman(k.olusturuldu),
            "silinecek": _zaman(guvenlik.silinme_tarihi(k, pasif_silme_gun)),
            "kilitli": bool(k.kilit_bitis and k.kilit_bitis > datetime.now()), "davet_bekliyor": davet_bekliyor}


# Rapor listesindeki satır.
def _rapor_ozeti(r: Rapor) -> dict:
    return {"id": r.id, "konu": r.konu, "durum": r.durum, "olusturuldu": _zaman(r.olusturuldu),
            "karar_zamani": _zaman(r.karar_zamani), "kayit_sayisi": r.kayit_sayisi, "hata": r.hata}


# Alıcı grubu satırı.
def _grup_json(g: AliciGrubu) -> dict:
    return {"id": g.id, "ad": g.ad, "is_kollari": g.is_kollari, "adresler": g.adresler, "aktif": g.aktif,
            "guncellendi": _zaman(g.guncellendi)}


# Panel uygulamasını kuran ana fonksiyon. Bütün API adresleri bunun içinde tanımlanıyor.
def uygulama_olustur(
    engine: Engine,
    gonderici: MailGonderici,
    is_kollari: list[str] | None,  # None, her istekte DB'deki konulardan (panelden eklenen iş kolu hemen çıkar)
    gizli_anahtar: str,
    ek_ekle: bool = True,
    https: bool = False,
    mfa_zorunlu: bool = False,
    arayuz: Path | None = VARSAYILAN_ARAYUZ,
    panel_adresi: str | None = None,  # davet/sıfırlama maillerindeki link (MEVZUAT_PANEL_ADRESI)
    pasif_silme_gun: int = guvenlik.PASIF_SILME_GUN,  # sadece ekranda gösterilir, silmeyi günlük iş yapar
    dokuman_acik: bool = False,  # API dokümanı anahtarsız açılsın mı (sunucuda varsayılan açık, MEVZUAT_DOKUMAN_ACIK)
    api_imza: bool = False,  # dış sistem istekleri imzalı olmalı mı (sunucuda varsayılan açık, MEVZUAT_API_IMZA)
    ayar_oku: Callable[[Session], dict] | None = None,  # verilirse yukarıdaki ayarlar her istekte buradan okunur
) -> FastAPI:
    # Gizli anahtar kısaysa çalışma (çerez imzası tahmin edilebilir olur).
    if len(gizli_anahtar) < 32:
        raise ValueError("MEVZUAT_GIZLI_ANAHTAR en az 32 karakter olmalı (ör. python -c \"import secrets; print(secrets.token_urlsafe(48))\")")

    # FastAPI uygulaması, otomatik API dokümanı sayfaları kapalı.
    # Hazır doküman adresleri kapalı. Doküman /api/dokuman adresinde, sadece giriş yapmış kullanıcıya açılır.
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, title="Mevzuat Takip API", version="1.0",
                  description="Mevzuat Takip panelinin API'si. Bütün uç noktalar oturum çerezi ister, veri değiştiren "
                              "istekler ayrıca X-CSRF-Token başlığı ister (bu sayfa kendisi ekler).")
    # Oturum çerezi, imzalı, 8 saat, sadece aynı siteden gönderilir, HTTPS ayarlıysa sadece HTTPS'te.
    app.add_middleware(
        SessionMiddleware,
        secret_key=gizli_anahtar,
        session_cookie="mevzuat_oturum",
        max_age=OTURUM_SURESI,
        same_site="strict",
        https_only=https,
    )
    # Veritabanı oturumu üreten fabrika.
    Oturum = sessionmaker(engine)
    # O anki ayarlar. Sunucuda panelden değişen ayarlar her istekte okunur (ayar_oku), testlerde verilen değerler sabittir.
    sabit = Isletim(gonderici, ek_ekle, mfa_zorunlu, panel_adresi, pasif_silme_gun, dokuman_acik, api_imza)

    def ayar(db: Session) -> Isletim:
        if ayar_oku is None:
            return sabit
        a = ayar_oku(db)
        return Isletim(panel_ayarlari.gonderici(a), a["ek_ekle"], a["mfa_zorunlu"], a["panel_adresi"], a["pasif_silme_gun"],
                       a["dokuman_acik"], a["api_imza"])

    # Her cevaba güvenlik başlıklarını ekleyen ara katman.
    @app.middleware("http")
    async def guvenlik_basliklari(request: Request, call_next):
        cevap = await call_next(request)
        for ad, deger in GUVENLIK_BASLIKLARI.items():
            cevap.headers.setdefault(ad, deger)
        return cevap

    # --- yardımcılar

    # İsteği yapan bilgisayarın IP adresi (denetim kaydı için).
    def ip(request: Request) -> str | None:
        return request.client.host if request.client else None

    # Oturuma özel CSRF anahtarı (yoksa üretir).
    def csrf(request: Request) -> str:
        if "csrf" not in request.session:
            request.session["csrf"] = secrets.token_urlsafe(32)
        return request.session["csrf"]

    # CSRF kontrolü. Veri değiştiren isteklerde başlıktaki anahtar oturumdakiyle aynı olmalı.
    def csrf_dogrula(request: Request) -> None:
        """Veri değiştiren her API isteğinde (giriş dahil) X-CSRF-Token başlığı oturumdaki token'la aynı olmalı.
        Başka bir site kullanıcının tarayıcısından istek gönderse bile bu başlığı koyamaz."""
        # Adres tarayıcıda yeni sekmede açılırsa (adres çubuğu, link) reddedilir, giriş yapmış olsa da.
        # Panel ve dış sistemler adresleri sayfanın içinden çağırır, doküman sayfası ve indirme bunun dışında.
        if request.headers.get("sec-fetch-mode") == "navigate" and request.url.path not in TARAYICIDA_ACILANLAR:
            raise HTTPException(401, "Bu adres tarayıcıda doğrudan açılamaz.")
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return
        # API anahtarıyla gelen istek çerezle değil anahtarla tanınır. Başka bir site tarayıcıdan bu başlığı koyamaz.
        if istek_anahtari(request):
            return
        # İmzalı istek de çerezle tanınmaz, imzası /api adreslerinde ayrıca doğrulanır.
        if imza.imzali_mi(request.headers) and request.url.path.startswith(DIS_ON_EK):
            return
        beklenen = request.session.get("csrf")
        gelen = request.headers.get("x-csrf-token", "")
        if not beklenen or not secrets.compare_digest(beklenen, gelen):
            raise HTTPException(403, "Oturum süresi doldu ya da geçersiz istek. Sayfayı yenileyin.")

    # Çerezdeki oturumdan kullanıcıyı bulur, oturum geçersizse (pasif, parola değişmiş...) çerezi temizleyip None döner.
    def oturum_kullanicisi(request: Request, db: Session) -> Kullanici | None:
        kid = request.session.get("kid")
        if kid is None:
            return None
        kullanici = db.get(Kullanici, kid)
        gecerli = (
            kullanici is not None
            and kullanici.aktif  # pasifleştirilen kullanıcının açık oturumu da biter
            and secrets.compare_digest(request.session.get("iz", ""), guvenlik.oturum_izi(kullanici))
            # MFA sonradan zorunlu yapıldıysa, kodsuz açılmış eski oturumlar geçmez.
            and (request.session.get("mfa") or not guvenlik.mfa_gerekli(kullanici, ayar(db).mfa_zorunlu))
        )
        if not gecerli:
            request.session.clear()
            return None
        return kullanici

    # Giriş yapılmamışsa 401 hatası ver. API anahtarı gönderildiyse çerez değil anahtar geçerlidir.
    def giris_gerekli(request: Request, db: Session) -> Kullanici:
        # İmzası doğrulanmış istek (imza_kontrol), hesabı anahtarın hesabıdır.
        hesap_id = getattr(request.state, "imzali_hesap", None)
        if hesap_id is not None:
            return db.get(Kullanici, hesap_id)
        anahtar = istek_anahtari(request)
        if anahtar is not None:
            if ayar(db).api_imza:
                denetle(db, "api_imzasiz_istek", None, ip(request), yol=request.url.path, on_ek=anahtar.strip()[:10])
                db.commit()
                raise HTTPException(401, "API anahtarı doğrudan gönderilemez, istek anahtarla imzalanmalı (HMAC). "
                                         "Nasıl imzalanacağı API dokümanındaki Authorize penceresinde.")
            return api_anahtari_gerekli(request, db, anahtar)
        # İmza zorunluyken /api adresleri panel oturumuyla da açılmaz, sadece imzalı istekle. Doküman bunun dışında.
        yol = request.url.path
        if yol.startswith(DIS_ON_EK) and yol not in DOKUMAN_ADRESLERI and ayar(db).api_imza:
            raise HTTPException(401, "Bu adres imzalı istek ister (X-Anahtar, X-Zaman, X-Numara, X-Imza).")
        kullanici = oturum_kullanicisi(request, db)
        if kullanici is None:
            raise HTTPException(401, "Giriş yapmanız gerekiyor.")
        # API kullanıcısı panelde sadece dokümanı görür, istek atmak için API anahtarı kullanır.
        if kullanici.rol == API_ROLU and request.url.path not in API_ROLU_ADRESLERI:
            raise HTTPException(403, "API kullanıcısı bu adrese oturumla erişemez, API anahtarı gönderin.")
        return kullanici

    # İmzalı isteği doğrular, hesabı request.state'e koyar. İmza yoksa bir şey yapmaz, giris_gerekli karar verir.
    # Gövde burada okunur, imza gövdeyi de kapsar. Veritabanı işi ayrı iş parçacığında, sunucu beklemesin.
    async def imza_kontrol(request: Request) -> None:
        if not imza.imzali_mi(request.headers):
            return
        govde = await request.body()
        adres = request.scope.get("raw_path", request.url.path.encode()).decode("latin-1")
        sorgu = request.scope.get("query_string", b"").decode("latin-1")

        def dogrula() -> int:
            with Oturum() as db:
                try:
                    kayit, hesap = imza.dogrula(db, request.headers, request.method, adres, sorgu, govde, gizli_anahtar)
                except imza.ImzaHatasi as e:
                    db.rollback()
                    denetle(db, "api_imza_gecersiz", None, ip(request), yol=request.url.path, sebep=e.sebep,
                            on_ek=request.headers.get(imza.ANAHTAR_BASLIGI, "")[:10])
                    db.commit()
                    raise HTTPException(e.kod, e.mesaj) from None
                simdi = datetime.now()
                if kayit.son_kullanim is None or (simdi - kayit.son_kullanim).total_seconds() > SON_KULLANIM_ARALIGI:
                    kayit.son_kullanim = simdi
                denetle(db, "api_istegi", hesap.id, ip(request), anahtar_id=kayit.id, yontem=request.method,
                        yol=request.url.path)
                db.commit()
                return hesap.id

        request.state.imzali_hesap = await run_in_threadpool(dogrula)

    # Dış sistemden mi geldi, API anahtarıyla ya da imzayla.
    def anahtarla_mi(request: Request) -> bool:
        return istek_anahtari(request) is not None or getattr(request.state, "imzali_hesap", None) is not None

    # API anahtarının hesabı. Geçersizse 401. Her istek denetim kaydına yazılır.
    def api_anahtari_gerekli(request: Request, db: Session, anahtar: str) -> Kullanici:
        anahtar = anahtar.strip()
        bulunan = guvenlik.api_anahtari_hesabi(db, anahtar)
        if bulunan is None:
            # Anahtarın kendisi yazılmaz, sadece neden tutmadığını anlamaya yarayan ilk harfleri ve uzunluğu.
            denetle(db, "api_anahtari_gecersiz", None, ip(request), yol=request.url.path, on_ek=anahtar[:10],
                    uzunluk=len(anahtar), durum=guvenlik.api_anahtari_durumu(db, anahtar))
            db.commit()
            raise HTTPException(401, "API anahtarı geçersiz, süresi dolmuş ya da iptal edilmiş.")
        kayit, hesap = bulunan
        simdi = datetime.now()
        if kayit.son_kullanim is None or (simdi - kayit.son_kullanim).total_seconds() > SON_KULLANIM_ARALIGI:
            kayit.son_kullanim = simdi
        denetle(db, "api_istegi", hesap.id, ip(request), anahtar_id=kayit.id, yontem=request.method, yol=request.url.path)
        db.commit()
        return hesap

    # Parolası doğru girilmiş, MFA kodunu bekleyen kullanıcı.
    def mfa_bekleyen(request: Request, db: Session) -> Kullanici | None:
        """Parolası doğrulanmış ama kodunu henüz girmemiş kullanıcı, süre dolduysa None. Bu durum oturum değildir,
        diğer istekler sadece "kid" alanına bakar."""
        kid, baslangic = request.session.get("mfa_kid"), request.session.get("mfa_baslangic", 0)
        if kid is None or time.time() - baslangic > MFA_BEKLEME:
            return None
        kullanici = db.get(Kullanici, kid)
        return kullanici if kullanici is not None and kullanici.aktif else None

    # MFA adımında olması gereken uç noktalar için, değilse 401.
    def mfa_bekleyen_gerekli(request: Request, db: Session, kurulum: bool) -> Kullanici:
        kullanici = mfa_bekleyen(request, db)
        if kullanici is None or kullanici.mfa_aktif == kurulum:
            raise HTTPException(401, "Süre doldu, tekrar giriş yapın.")
        return kullanici

    # Başarılı girişte yeni oturum açar.
    def oturumu_ac(request: Request, kullanici: Kullanici, mfa_ile: bool) -> None:
        request.session.clear()  # girişte yeni oturum (oturum sabitleme saldırısına karşı)
        request.session["kid"] = kullanici.id
        request.session["iz"] = guvenlik.oturum_izi(kullanici)
        request.session["mfa"] = mfa_ile
        csrf(request)

    # API anahtarının iki biçimi dokümanda "Authorize" düğmesi olarak görünür, kontrolü giris_gerekli yapar.
    anahtar_semalari = [Security(HTTPBearer(auto_error=False, description=IMZA_ACIKLAMASI))]
    # Panel arayüzünün adresleri (/panel/api), her istekte önce CSRF kontrolü çalışır.
    api = APIRouter(prefix=PANEL_ON_EK, dependencies=[Depends(csrf_dogrula), *anahtar_semalari])
    # API dokümanının adresleri (/api/dokuman), dış sistemlerin adresleriyle aynı yerde.
    dokuman_yolu = APIRouter(prefix="/api", dependencies=[Depends(csrf_dogrula)])

    # --- oturum, giriş, çıkış

    # Arayüz açılınca kimin giriş yaptığını bu adrese sorar.
    @api.get("/oturum")
    def oturum(request: Request):
        """Arayüz açılırken çağrılır. Kimin giriş yaptığını, MFA adımında olup olmadığını ve CSRF token'ını döner."""
        with Oturum() as db:
            kullanici = oturum_kullanicisi(request, db)
            bekleyen = None if kullanici else mfa_bekleyen(request, db)
            mfa = ("kod" if bekleyen.mfa_aktif else "kurulum") if bekleyen else None
            return {"csrf": csrf(request), "kullanici": _kullanici_json(kullanici) if kullanici else None, "mfa": mfa,
                    "mail_kapali": bool(kullanici) and ayar(db).mail_kapali}

    # E-posta ve parolayla giriş.
    @api.post("/giris")
    def giris(request: Request, istek: GirisIstegi):
        with Oturum() as db:
            zorunlu = ayar(db).mfa_zorunlu
            kullanici = giris_dene(db, istek.eposta, istek.parola, ip(request), zorunlu)
            if kullanici is None:
                raise HTTPException(400, "E-posta veya parola hatalı ya da hesap geçici olarak kilitli.")
            # MFA gerekiyorsa oturumu açma, sadece "kod bekleniyor" bilgisini sakla.
            if guvenlik.mfa_gerekli(kullanici, zorunlu):
                request.session.clear()
                request.session["mfa_kid"] = kullanici.id
                request.session["mfa_baslangic"] = int(time.time())
                return {"sonraki": "kod" if kullanici.mfa_aktif else "kurulum", "csrf": csrf(request)}
            oturumu_ac(request, kullanici, mfa_ile=False)
            return {"sonraki": "panel", "csrf": csrf(request)}

    # MFA kurulumu için gizli anahtarı verir, elle girmek isteyenler için dörderli gruplar halinde.
    @api.get("/giris/mfa-kurulum")
    def mfa_kurulum_bilgisi(request: Request):
        with Oturum() as db:
            kullanici = mfa_bekleyen_gerekli(request, db, kurulum=True)
            gizli = guvenlik.mfa_kurulum_baslat(db, kullanici)
            return {"gizli": " ".join(gizli[i:i + 4] for i in range(0, len(gizli), 4))}

    # MFA kurulumu için QR kodu resmini verir.
    @api.get("/giris/mfa-qr.svg")
    def mfa_qr(request: Request):
        # Resim olarak sunulur (<img src>), SVG resim içinden script çalıştırılamaz, sayfaya HTML gömülmez.
        with Oturum() as db:
            kullanici = mfa_bekleyen_gerekli(request, db, kurulum=True)
            guvenlik.mfa_kurulum_baslat(db, kullanici)
            return Response(guvenlik.mfa_qr_svg(kullanici), media_type="image/svg+xml")

    # MFA kodunu doğrulayıp oturumu açan ortak yardımcı.
    def _kod_ile_tamamla(request: Request, istek: KodIstegi, kurulum: bool, hata: str):
        with Oturum() as db:
            kullanici = mfa_bekleyen_gerekli(request, db, kurulum=kurulum)
            if not guvenlik.mfa_dogrula(db, kullanici, istek.kod, ip(request)):
                raise HTTPException(400, hata)
            oturumu_ac(request, kullanici, mfa_ile=True)
            return {"sonraki": "panel", "csrf": csrf(request)}

    # MFA kurulumunu ilk doğru kodla tamamlar.
    @api.post("/giris/mfa-kurulum")
    def mfa_kurulum(request: Request, istek: KodIstegi):
        return _kod_ile_tamamla(request, istek, True, "Kod hatalı. Uygulamadaki güncel kodu girin.")

    # MFA'sı kurulmuş kullanıcı kodunu buraya gönderir.
    @api.post("/giris/kod")
    def kod_gir(request: Request, istek: KodIstegi):
        return _kod_ile_tamamla(request, istek, False,
                                "Kod hatalı ya da süresi geçmiş (veya hesap geçici olarak kilitli).")

    # Çıkış yapar.
    @api.post("/cikis")
    def cikis(request: Request):
        request.session.clear()
        return {"csrf": csrf(request)}

    # --- raporlar

    # Rapor listesini verir, bekleyenler ve son 50 geçmiş rapor.
    @api.get("/raporlar")
    def rapor_listesi(request: Request):
        with Oturum() as db:
            giris_gerekli(request, db)
            gecmis = db.scalars(select(Rapor).where(Rapor.durum != "ONAY_BEKLIYOR").order_by(Rapor.id.desc()).limit(50))
            return {"bekleyenler": [_rapor_ozeti(r) for r in rapor_modulu.onay_bekleyenler(db)],
                    "gecmis": [_rapor_ozeti(r) for r in gecmis]}

    # Tek bir raporun ayrıntısını verir, kalemler, kime gideceği ve gönderim durumu.
    @api.get("/raporlar/{rapor_id}")
    def rapor_detay(request: Request, rapor_id: int):
        with Oturum() as db:
            giris_gerekli(request, db)
            rapor = db.get(Rapor, rapor_id)
            if rapor is None:
                raise HTTPException(404, "Rapor bulunamadı.")
            # Raporun kalemlerini hazırla.
            kayitlar = rapor_modulu.rapor_kayitlari(db, rapor)
            kalemler = [rapor_modulu._kalem(i, k) for i, k in enumerate(kayitlar, start=1)]
            no = {k.kayit.id: k.no for k in kalemler}
            aciklamalar = rapor_modulu.konu_aciklamalari(db)
            # Onay öncesi, her kalem şu anki gruplara göre kime gidecek. Onaydan sonra, dondurulmuş gönderimler.
            gruplar = alicilar.aktif_gruplar(db)
            karar_veren = db.get(Kullanici, rapor.karar_veren_id) if rapor.karar_veren_id else None
            return {
                "rapor": {**_rapor_ozeti(rapor), "karar_notu": rapor.karar_notu,
                          "karar_veren": karar_veren.ad if karar_veren else None,
                          "gonderildi": _zaman(rapor.gonderildi), "alicilar": rapor.alicilar},
                "kalemler": [{
                    "id": k.kayit.id, "no": k.no, "tur_adi": k.tur_adi, "baslik": k.kayit.baslik,
                    "kaynak": k.kayit.kaynak, "yayin_tarihi": k.kayit.yayin_tarihi.isoformat(),
                    "is_kollari": alicilar.kalem_is_kollari(k.kayit), "eslesmeler": k.kayit.eslesmeler,
                    "ozet": k.ozet, "ozet_tablosu": k.ozet_tablosu, "yururluk": k.yururluk,
                    "one_cikanlar": k.one_cikanlar, "metin": k.metin, "kisaltildi": k.kisaltildi, "ocr": k.ocr,
                    "okunamadi": k.kayit.icerik_durumu in ("OCR_GEREKLI", "HATA"),
                    "degisiklikler": k.degisiklikler, "kaynakca": k.kaynakca, "haric": k.kayit.haric,
                    # Onaylayıcı belgenin aslına baksın diye resmî adres, sadece http ve https.
                    "url": k.kayit.url if k.kayit.url.startswith(("https://", "http://")) else None,
                    # Başlıkta kelimesi yakalanan konuların açıklamaları, bu konu bizi neden ilgilendiriyor.
                    "nedenler": [{"konu": ad, "aciklama": a}
                                 for ad, a in rapor_modulu.neden_onemli(k.kayit, aciklamalar)],
                    "gidecek": [{"ad": g.ad, "kisi": len(g.adresler)}
                                for g in gruplar if alicilar.grubun_kalemleri(g, [k.kayit])],
                } for k in kalemler],
                # Onay ekranındaki alıcı seçimi için, aktif gruplar (iş kollarıyla, öneriyi arayüz hesaplar)
                # ve kişiye özel adres kutusuna öneri olarak bilinen adresler.
                "gruplar": [{"id": g.id, "ad": g.ad, "adresler": g.adresler, "is_kollari": g.is_kollari} for g in gruplar],
                "adres_onerileri": sorted({a for g in gruplar for a in g.adresler}),
                "gonderimler": [{
                    "id": g.id, "gruplar": g.gruplar, "alicilar": g.alicilar, "durum": g.durum,
                    "kalemler": [no[i] for i in g.kayit_idler if i in no],
                    "gonderildi": _zaman(g.gonderildi), "hata": g.hata, "notu": g.notu,
                } for g in rapor_modulu.gonderimler(db, rapor)],
            }

    # Raporu onaylar ya da reddeder.
    @api.post("/raporlar/{rapor_id}/karar")
    def karar(request: Request, rapor_id: int, istek: KararIstegi):
        with Oturum() as db:
            kullanici = giris_gerekli(request, db)
            # Görev ayrılığı, sistemi yöneten (admin) rapor onaylamaz/reddetmez, bu iş onaylayıcınındır.
            # Arayüz admin'e butonları hiç göstermez, bu kontrol doğrudan isteklere karşı.
            if kullanici.rol not in KARAR_ROLLERI:
                denetle(db, "yetkisiz_karar_denemesi", kullanici.id, ip(request), rapor_id=rapor_id, rol=kullanici.rol)
                db.commit()
                raise HTTPException(403, "Bu işlem için yetkiniz yok.")
            rapor = db.get(Rapor, rapor_id)
            if rapor is None:
                raise HTTPException(404, "Rapor bulunamadı.")
            # Kararı işle, hatalı istekse 400, başkası önce karar verdiyse 409.
            onay = istek.karar == "onayla"
            try:
                rapor_modulu.karar_ver(db, rapor, kullanici.id, onay, set(istek.dahil), istek.notu,
                                       None if istek.gruplar is None else set(istek.gruplar), istek.ek_adresler)
            except ValueError as e:
                raise HTTPException(400, str(e))
            except rapor_modulu.DurumHatasi as e:
                raise HTTPException(409, str(e))
            # Kararı denetim kaydına yaz.
            denetle(db, "onay" if onay else "ret", kullanici.id, ip(request),
                    rapor_id=rapor_id, dahil=sorted(istek.dahil), notu=istek.notu.strip() or None,
                    gruplar=istek.gruplar, ek_adresler=istek.ek_adresler)
            db.commit()

            # Reddedildiyse iş bitti.
            if not onay:
                return {"tur": "basari", "mesaj": "Rapor reddedildi, dağıtılmayacak."}
            # Onaylandıysa hemen dağıt ve sonucu anlaşılır bir mesajla bildir.
            return dagit_ve_bildir(db, rapor, "Onaylandı", len(rapor.alicilar))

    # Bekleyen mailleri gönderir ve sonucu panelde gösterilecek mesaja çevirir.
    def dagit_ve_bildir(db: Session, rapor: Rapor, bas: str, adres_sayisi: int) -> dict:
        a = ayar(db)
        with make_client() as client:
            gitti = rapor_modulu.dagit(db, client, a.gonderici, rapor, a.ek_ekle)
        if gitti and a.mail_kapali:
            return {"tur": "hata", "mesaj": f"{bas} ama mail GÖNDERİLMEDİ: mail sunucusu (Ayarlar sayfası) "
                    f"ayarlı değil, {adres_sayisi} adrese gidecek mail sunucudaki giden_mailler/ klasörüne yazıldı."}
        if gitti:
            return {"tur": "basari", "mesaj": f"{bas} ve {adres_sayisi} adrese gönderildi."}
        if rapor.hata:
            return {"tur": "hata",
                    "mesaj": f"{bas} ama bazı mailler gönderilemedi; otomatik tekrar denenecek. ({rapor.hata})"}
        # Tam o anda günlük iş (tekrar deneme adımı) bazı mailleri sahiplendi, o gönderecek.
        return {"tur": "basari", "mesaj": f"{bas}; rapor şu anda gönderiliyor."}

    # Onaylanmış rapordan başka kalemleri başka kişilere gönderir.
    @api.post("/raporlar/{rapor_id}/ek-gonderim")
    def ek_gonderim(request: Request, rapor_id: int, istek: EkGonderimIstegi):
        with Oturum() as db:
            kullanici = giris_gerekli(request, db)
            # Onay gibi ek gönderim de sadece onaylayıcının işidir.
            if kullanici.rol not in KARAR_ROLLERI:
                denetle(db, "yetkisiz_ek_gonderim_denemesi", kullanici.id, ip(request), rapor_id=rapor_id, rol=kullanici.rol)
                db.commit()
                raise HTTPException(403, "Bu işlem için yetkiniz yok.")
            rapor = db.get(Rapor, rapor_id)
            if rapor is None:
                raise HTTPException(404, "Rapor bulunamadı.")
            onceki = {g.id for g in rapor_modulu.gonderimler(db, rapor)}
            try:
                rapor_modulu.ek_gonderim_planla(db, rapor, set(istek.dahil), istek.notu, set(istek.gruplar),
                                                istek.ek_adresler)
            except ValueError as e:
                raise HTTPException(400, str(e))
            except rapor_modulu.DurumHatasi as e:
                raise HTTPException(409, str(e))
            yeni_adresler = {a for g in rapor_modulu.gonderimler(db, rapor) if g.id not in onceki for a in g.alicilar}
            denetle(db, "ek_gonderim", kullanici.id, ip(request),
                    rapor_id=rapor_id, dahil=sorted(istek.dahil), notu=istek.notu.strip() or None,
                    gruplar=istek.gruplar, ek_adresler=istek.ek_adresler)
            db.commit()
            return dagit_ve_bildir(db, rapor, "Ek gönderim hazırlandı", len(yeni_adresler))

    # --- alıcı grupları

    # Alıcı grupları yetkisi (admin + onaylayıcı), yetkisiz değiştirme denemesi denetime yazılır.
    def grup_yetkisi(request: Request, db: Session) -> Kullanici:
        kullanici = giris_gerekli(request, db)
        if kullanici.rol not in GRUP_ROLLERI:
            if request.method != "GET":
                denetle(db, "yetkisiz_grup_denemesi", kullanici.id, ip(request), rol=kullanici.rol)
                db.commit()
            raise HTTPException(403, "Bu işlem için yetkiniz yok.")
        return kullanici

    # Alıcı gruplarını, seçilebilecek iş kollarını ve hiçbir grubun almadığı iş kollarını verir.
    @api.get("/gruplar")
    def grup_listesi(request: Request):
        with Oturum() as db:
            grup_yetkisi(request, db)
            gruplar = list(db.scalars(select(AliciGrubu).order_by(AliciGrubu.aktif.desc(), AliciGrubu.ad)))
            ayardaki = tanimlar.is_kollari(db) if is_kollari is None else is_kollari
            # Seçenekler, konu ayarındaki iş kolları + grupta kayıtlı olup ayardan kalkmış olanlar
            # (yoksa düzenleyip kaydedince sessizce silinirdi).
            secenekler = sorted(set(ayardaki) | {ik for g in gruplar for ik in g.is_kollari})
            return {"gruplar": [_grup_json(g) for g in gruplar], "is_kollari": secenekler,
                    "kapsanmayan": [ik for ik in ayardaki if not any(ik in g.is_kollari for g in gruplar if g.aktif)]}

    # Grup ekleme/düzenlemenin ortak kısmı, kaydet, denetime önce/sonra halini yaz.
    def _grup_kaydet(request: Request, istek: GrupIstegi, grup_id: int | None):
        with Oturum() as db:
            kullanici = grup_yetkisi(request, db)
            grup = db.get(AliciGrubu, grup_id) if grup_id is not None else None
            if grup_id is not None and grup is None:
                raise HTTPException(404, "Grup bulunamadı.")
            once = alicilar.grup_bilgisi(grup) if grup else None
            try:
                grup = alicilar.grup_kaydet(db, grup, istek.ad, istek.is_kollari, "\n".join(istek.adresler), istek.aktif)
            except ValueError as e:
                db.rollback()
                raise HTTPException(400, str(e))
            denetle(db, "grup_degistir" if once else "grup_ekle", kullanici.id, ip(request),
                    grup_id=grup.id, once=once, sonra=alicilar.grup_bilgisi(grup))
            db.commit()
            return _grup_json(grup)

    # Yeni grup ekler.
    @api.post("/gruplar")
    def grup_ekle(request: Request, istek: GrupIstegi):
        return _grup_kaydet(request, istek, None)

    # Var olan grubu düzenler.
    @api.put("/gruplar/{grup_id}")
    def grup_degistir(request: Request, grup_id: int, istek: GrupIstegi):
        return _grup_kaydet(request, istek, grup_id)

    # --- kaynaklar (#10)

    # Kaynak/konu yönetimi yetkisi (admin + onaylayıcı).
    def ayar_yetkisi(request: Request, db: Session) -> Kullanici:
        kullanici = giris_gerekli(request, db)
        if kullanici.rol not in AYAR_ROLLERI:
            if request.method != "GET":
                denetle(db, "yetkisiz_ayar_denemesi", kullanici.id, ip(request), rol=kullanici.rol)
                db.commit()
            raise HTTPException(403, "Bu işlem için yetkiniz yok.")
        return kullanici

    # İstekten kaynak formu üretir.
    def _form(istek: KaynakIstegi) -> kaynak_yonetimi.KaynakFormu:
        return kaynak_yonetimi.KaynakFormu(istek.etiket, istek.ayarlar, istek.varsayilan_konular, istek.aktif)

    # Kaynağı adına göre getirir, yoksa 404.
    def _kaynak_getir(db: Session, ad: str) -> KaynakTanimi:
        kaynak = db.get(KaynakTanimi, ad)
        if kaynak is None:
            raise HTTPException(404, "Kaynak bulunamadı.")
        return kaynak

    # Kaynak değişikliklerinin ortak kısmı, değişikliği yap, hataları uygun koda çevir, denetime yaz, güncel halini döndür.
    def _kaynak_kaydet(request: Request, db: Session, kullanici: Kullanici, islem: str, kaynak: KaynakTanimi | None,
                       degistir, **ek) -> dict:
        once = kaynak_yonetimi.kaynak_bilgisi(kaynak) if kaynak else None
        try:
            kaynak = degistir()
        except kaynak_yonetimi.CakismaHatasi as e:
            db.rollback()
            raise HTTPException(409, str(e))
        except ValueError as e:
            db.rollback()
            raise HTTPException(400, str(e))
        denetle(db, islem, kullanici.id, ip(request), kaynak=kaynak.ad, once=once,
                sonra=kaynak_yonetimi.kaynak_bilgisi(kaynak), **ek)
        db.commit()
        return next(k for k in kaynak_yonetimi.kaynak_listesi(db) if k["ad"] == kaynak.ad)

    # Kaynak tiplerini, formlarını ve seçilebilecek konuları verir.
    @api.get("/kaynak-tipleri")
    def kaynak_tipleri(request: Request):
        with Oturum() as db:
            ayar_yetkisi(request, db)
            return {"tipler": kaynak_yonetimi.tip_listesi(),
                    "konular": [k.ad for k in tanimlar.konulari_oku(db)]}

    # Kaynak listesini verir.
    @api.get("/kaynaklar")
    def kaynak_listesi(request: Request):
        with Oturum() as db:
            ayar_yetkisi(request, db)
            return {"kaynaklar": kaynak_yonetimi.kaynak_listesi(db)}

    # Yeni kaynak ekler.
    @api.post("/kaynaklar")
    def kaynak_ekle(request: Request, istek: KaynakIstegi):
        with Oturum() as db:
            kullanici = ayar_yetkisi(request, db)
            return _kaynak_kaydet(request, db, kullanici, "kaynak_ekle", None,
                                  lambda: kaynak_yonetimi.kaynak_ekle(db, istek.tip, _form(istek), kullanici.id))

    # Kaynağı kaydetmeden dener.
    @api.post("/kaynaklar/dene")
    def kaynak_dene(request: Request, istek: KaynakIstegi):
        """Kaydedilmemiş tanımı son 7 gün için dener, veritabanına hiçbir şey yazmaz."""
        with Oturum() as db:
            ayar_yetkisi(request, db)
            konular = tanimlar.konulari_oku(db)
        try:
            return kaynak_yonetimi.dene(istek.tip, _form(istek), konular, {k.ad for k in konular})
        except ValueError as e:
            raise HTTPException(400, str(e))

    # Girilen adresten kaynak tipini bulur.
    @api.post("/kaynaklar/bul")
    def kaynak_bul(request: Request, istek: AdresIstegi):
        """Adresten kaynak tipini ve ayarlarını bulur, sırayla WordPress, RSS ve düz HTML denenir. Hiçbir şey kaydetmez."""
        with Oturum() as db:
            ayar_yetkisi(request, db)
        try:
            return kaynak_bulucu.bul(istek.adres)
        except ValueError as e:
            raise HTTPException(400, str(e))

    # Var olan kaynağı düzenler.
    @api.put("/kaynaklar/{ad}")
    def kaynak_degistir(request: Request, ad: str, istek: KaynakIstegi):
        with Oturum() as db:
            kullanici = ayar_yetkisi(request, db)
            kaynak = _kaynak_getir(db, ad)
            if istek.surum is None:
                raise HTTPException(400, "Sürüm bilgisi eksik; sayfayı yenileyin.")

            def degistir():
                kaynak_yonetimi.kaynak_degistir(db, kaynak, _form(istek), istek.surum, kullanici.id)
                return kaynak
            return _kaynak_kaydet(request, db, kullanici, "kaynak_degistir", kaynak, degistir)

    # Kaldır/geri getir ortak kısmı.
    def _kaldir(request: Request, ad: str, istek: SurumIstegi, kaldir: bool):
        with Oturum() as db:
            kullanici = ayar_yetkisi(request, db)
            kaynak = _kaynak_getir(db, ad)

            def degistir():
                kaynak_yonetimi.kaldir_ya_da_geri_getir(kaynak, kaldir, istek.surum, kullanici.id)
                return kaynak
            return _kaynak_kaydet(request, db, kullanici, "kaynak_kaldir" if kaldir else "kaynak_geri_getir",
                                  kaynak, degistir)

    # Kaynağı kaldırır.
    @api.post("/kaynaklar/{ad}/kaldir")
    def kaynak_kaldir(request: Request, ad: str, istek: SurumIstegi):
        return _kaldir(request, ad, istek, True)

    # Kaldırılmış kaynağı geri getirir.
    @api.post("/kaynaklar/{ad}/geri-getir")
    def kaynak_geri_getir(request: Request, ad: str, istek: SurumIstegi):
        return _kaldir(request, ad, istek, False)

    # --- konular (#10)

    # İstekten konu formu üretir.
    def _konu_formu(istek: KonuIstegi) -> konu_yonetimi.KonuFormu:
        return konu_yonetimi.KonuFormu(istek.ad, istek.is_kollari, istek.kelimeler, istek.haric, istek.dislanan,
                                       istek.aciklama)

    # Konuyu numarasına göre getirir, yoksa 404.
    def _konu_getir(db: Session, konu_id: int) -> KonuTanimi:
        konu = db.get(KonuTanimi, konu_id)
        if konu is None:
            raise HTTPException(404, "Konu bulunamadı.")
        return konu

    # Konu değişikliklerinin ortak kısmı.
    def _konu_kaydet(request: Request, db: Session, kullanici: Kullanici, islem: str, konu: KonuTanimi | None,
                     degistir, **ek) -> dict:
        once = konu_yonetimi.konu_bilgisi(konu) if konu else None
        try:
            konu = degistir()
        except konu_yonetimi.CakismaHatasi as e:
            db.rollback()
            raise HTTPException(409, str(e))
        except ValueError as e:
            db.rollback()
            raise HTTPException(400, str(e))
        denetle(db, islem, kullanici.id, ip(request), konu_id=konu.id, once=once,
                sonra=konu_yonetimi.konu_bilgisi(konu), **ek)
        db.commit()
        izlenen = konu_yonetimi.izlenen_konulari()
        return next(k for k in konu_yonetimi.konu_listesi(db, izlenen) if k["id"] == konu.id)

    # Konu listesini ve iş kolu seçeneklerini verir.
    @api.get("/konular")
    def konu_listesi(request: Request):
        with Oturum() as db:
            ayar_yetkisi(request, db)
            konular = konu_yonetimi.konu_listesi(db, konu_yonetimi.izlenen_konulari())
            gruplardaki = {ik for g in db.scalars(select(AliciGrubu)) for ik in g.is_kollari}
            return {"konular": konular, "gun": konu_yonetimi.ONIZLEME_GUN,
                    "is_kolu_secenekleri": sorted(set(tanimlar.is_kollari(db)) | gruplardaki)}

    # Konu önizlemesi, yani "Etkisini gör" düğmesi.
    @api.post("/konular/onizleme")
    def konu_onizleme(request: Request, istek: KonuIstegi):
        """Kaydedilmemiş tanımı son 90 günün başlıklarına uygular, hiçbir şey yazmaz."""
        with Oturum() as db:
            ayar_yetkisi(request, db)
            if istek.id is not None:
                _konu_getir(db, istek.id)
            try:
                return konu_yonetimi.onizleme(db, _konu_formu(istek), istek.id)
            except ValueError as e:
                raise HTTPException(400, str(e))

    # Yeni konu ekler.
    @api.post("/konular")
    def konu_ekle(request: Request, istek: KonuIstegi):
        with Oturum() as db:
            kullanici = ayar_yetkisi(request, db)
            return _konu_kaydet(request, db, kullanici, "konu_ekle", None,
                                lambda: konu_yonetimi.konu_ekle(db, _konu_formu(istek), kullanici.id))

    # Var olan konuyu düzenler.
    @api.put("/konular/{konu_id}")
    def konu_degistir(request: Request, konu_id: int, istek: KonuIstegi):
        with Oturum() as db:
            kullanici = ayar_yetkisi(request, db)
            konu = _konu_getir(db, konu_id)
            if istek.surum is None:
                raise HTTPException(400, "Sürüm bilgisi eksik; sayfayı yenileyin.")

            def degistir():
                konu_yonetimi.konu_degistir(db, konu, _konu_formu(istek), istek.surum, kullanici.id)
                return konu
            return _konu_kaydet(request, db, kullanici, "konu_degistir", konu, degistir)

    # Konu aktif/pasif ortak kısmı.
    def _konu_aktiflik(request: Request, konu_id: int, istek: SurumIstegi, aktif: bool):
        with Oturum() as db:
            kullanici = ayar_yetkisi(request, db)
            konu = _konu_getir(db, konu_id)

            def degistir():
                konu_yonetimi.aktiflik(db, konu, aktif, istek.surum, kullanici.id, konu_yonetimi.izlenen_konulari())
                return konu
            return _konu_kaydet(request, db, kullanici, "konu_aktif" if aktif else "konu_pasif", konu, degistir)

    # Konuyu pasifleştirir.
    @api.post("/konular/{konu_id}/pasif")
    def konu_pasif(request: Request, konu_id: int, istek: SurumIstegi):
        return _konu_aktiflik(request, konu_id, istek, False)

    # Konuyu aktifleştirir.
    @api.post("/konular/{konu_id}/aktif")
    def konu_aktif(request: Request, konu_id: int, istek: SurumIstegi):
        return _konu_aktiflik(request, konu_id, istek, True)

    # --- değişiklik geçmişi ve admin kurtarma (#10)

    # "Önceki hale döndür" yetkisi, sadece admin.
    def kurtarma_yetkisi(request: Request, db: Session) -> Kullanici:
        kullanici = ayar_yetkisi(request, db)
        if kullanici.rol not in KURTARMA_ROLLERI:
            denetle(db, "yetkisiz_kurtarma_denemesi", kullanici.id, ip(request), rol=kullanici.rol)
            db.commit()
            raise HTTPException(403, "Önceki hale döndürme sadece yöneticiye (admin) açık.")
        return kullanici

    # Kaynağın değişiklik geçmişini verir.
    @api.get("/kaynaklar/{ad}/gecmis")
    def kaynak_gecmisi(request: Request, ad: str):
        with Oturum() as db:
            ayar_yetkisi(request, db)
            _kaynak_getir(db, ad)
            return {"gecmis": gecmis.kaynak_gecmisi(db, ad)}

    # Kaynağı önceki haline döndürür, sadece admin.
    @api.post("/kaynaklar/{ad}/geri-al")
    def kaynak_geri_al(request: Request, ad: str, istek: GeriAlIstegi):
        with Oturum() as db:
            kullanici = kurtarma_yetkisi(request, db)
            kaynak = _kaynak_getir(db, ad)

            def degistir():
                gecmis.kaynagi_geri_al(db, kaynak, istek.denetim_id, istek.surum, kullanici.id)
                return kaynak
            return _kaynak_kaydet(request, db, kullanici, "kaynak_geri_al", kaynak, degistir,
                                  geri_alinan=istek.denetim_id)

    # Konunun değişiklik geçmişini verir.
    @api.get("/konular/{konu_id}/gecmis")
    def konu_gecmisi(request: Request, konu_id: int):
        with Oturum() as db:
            ayar_yetkisi(request, db)
            _konu_getir(db, konu_id)
            return {"gecmis": gecmis.konu_gecmisi(db, konu_id)}

    # Konuyu önceki haline döndürür, sadece admin.
    @api.post("/konular/{konu_id}/geri-al")
    def konu_geri_al(request: Request, konu_id: int, istek: GeriAlIstegi):
        with Oturum() as db:
            kullanici = kurtarma_yetkisi(request, db)
            konu = _konu_getir(db, konu_id)

            def degistir():
                gecmis.konuyu_geri_al(db, konu, istek.denetim_id, istek.surum, kullanici.id,
                                      konu_yonetimi.izlenen_konulari())
                return konu
            return _konu_kaydet(request, db, kullanici, "konu_geri_al", konu, degistir, geri_alinan=istek.denetim_id)

    # --- zamanlama (tarama saatleri panelden, taramayı zamanlayıcı süreci başlatır)

    # Tarama saatlerini ve zamanlayıcının durumunu verir.
    @api.get("/zamanlama")
    def zamanlama_durumu(request: Request):
        with Oturum() as db:
            giris_gerekli(request, db)  # herkes görür (zamanlayıcı çalışmıyor uyarısı), değiştirmek ayar yetkisi ister
            return zamanlama.zamanlayici_durumu(db)

    # Tarama saatlerini değiştirir.
    @api.put("/zamanlama")
    def zamanlama_degistir(request: Request, istek: ZamanlamaIstegi):
        with Oturum() as db:
            kullanici = ayar_yetkisi(request, db)
            try:
                eski, yeni = zamanlama.saatleri_kaydet(db, istek.saatler, istek.surum, kullanici.id)
            except zamanlama.CakismaHatasi as e:
                db.rollback()
                raise HTTPException(409, str(e))
            except ValueError as e:
                db.rollback()
                raise HTTPException(400, str(e))
            denetle(db, "tarama_saatleri", kullanici.id, ip(request), once=eski, sonra=yeni)
            db.commit()
            return zamanlama.zamanlayici_durumu(db)

    # --- panelden "Şimdi tara", panel sadece istek yazar, taramayı zamanlayıcı başlatır (zamanlama.py)

    # "Şimdi tara" isteğinin durumunu ve son çalışmaları verir.
    @api.get("/tarama/durum")
    def tarama_durumu(request: Request):
        with Oturum() as db:
            giris_gerekli(request, db)
            return zamanlama.tarama_durumu(db)

    # "Şimdi tara" isteği oluşturur.
    @api.post("/tarama")
    def tarama_iste(request: Request):
        with Oturum() as db:
            kullanici = ayar_yetkisi(request, db)
            try:
                istek = zamanlama.istek_olustur(db, kullanici.id)
            except ValueError as e:
                db.rollback()
                raise HTTPException(409, str(e))
            denetle(db, "tarama_istegi", kullanici.id, ip(request), istek_id=istek.id)
            db.commit()
            return zamanlama.tarama_durumu(db)

    # --- kullanıcı yönetimi ve denetim kaydı (sadece admin, kurtarma işleri, rol kararı 2026-10-02)

    # Sadece admin'in yapabildiği işler için kontrol, yetkisiz deneme denetime yazılır.
    def admin_gerekli(request: Request, db: Session, islem: str) -> Kullanici:
        kullanici = giris_gerekli(request, db)
        if kullanici.rol not in KURTARMA_ROLLERI:
            denetle(db, "yetkisiz_yonetim_denemesi", kullanici.id, ip(request), istek=islem, rol=kullanici.rol)
            db.commit()
            raise HTTPException(403, "Bu işlem için yetkiniz yok.")
        return kullanici

    # Kullanıcılar sayfasının listesi (davet bekleyenler işaretli).
    def kullanicilar_json(db: Session) -> list[dict]:
        bekleyen = set(db.scalars(select(ParolaLinki.kullanici_id).where(
            ParolaLinki.tur == "davet", ParolaLinki.kullanildi.is_(None), ParolaLinki.son_gecerlilik > datetime.now())))
        hic_girmemis = {k.id for k in db.scalars(select(Kullanici).where(Kullanici.son_giris.is_(None)))}
        # Silinmiş hesaplar ve API anahtarlarının hesapları listede görünmez.
        gun = ayar(db).pasif_silme_gun
        return [_yonetim_json(k, k.id in bekleyen and k.id in hic_girmemis, gun)
                for k in db.scalars(select(Kullanici).where(Kullanici.silindi.is_(None), Kullanici.api_hesabi.is_(False))
                                    .order_by(Kullanici.aktif.desc(), Kullanici.ad))]

    # Kişiye davet ya da parola sıfırlama linki mail atar.
    def link_gonder(db: Session, hedef: Kullanici, yapan: Kullanici, request: Request) -> str | None:
        """Davet linki (kişi hiç giriş yapmamışsa) ya da sıfırlama linki maili gönderir. Mail gitmediyse hata mesajını döner."""
        a = ayar(db)
        if not a.panel_adresi:
            return "Panel adresi tanımlı değil; link gönderilemedi. Ayarlar sayfasından panel adresini girin."
        tur = "davet" if hedef.son_giris is None else "sifirlama"
        token, son = guvenlik.parola_linki_olustur(db, hedef, tur, yapan.id)
        denetle(db, f"parola_linki_{tur}", yapan.id, ip(request), hedef=hedef.eposta)
        db.commit()  # mail gitse de gitmese de link kaydı ve denetim kalsın
        try:
            a.gonderici.gonder(guvenlik.parola_linki_maili(hedef, f"{a.panel_adresi}/?parola={token}", tur, son, yapan.ad))
        except Exception as e:
            return f"Mail gönderilemedi ({e.__class__.__name__}). Mail ayarlarını kontrol edip tekrar link gönderin."
        return None

    # Kullanıcı listesini verir.
    @api.get("/kullanicilar")
    def kullanici_listesi(request: Request):
        with Oturum() as db:
            admin_gerekli(request, db, "kullanici_listesi")
            a = ayar(db)
            return {"kullanicilar": kullanicilar_json(db), "panel_adresi_var": bool(a.panel_adresi),
                    "pasif_silme_gun": a.pasif_silme_gun}

    # Yeni kullanıcı ekler, kişiye davet maili gider.
    @api.post("/kullanicilar")
    def kullanici_ekle(request: Request, istek: KullaniciIstegi):
        with Oturum() as db:
            yapan = admin_gerekli(request, db, "kullanici_ekle")
            ad = " ".join(istek.ad.split())
            if not ad:
                raise HTTPException(400, "Ad boş olamaz.")
            try:
                # Kimsenin bilmediği rastgele parola, kişi parolasını davet linkiyle kendisi belirler.
                yeni = guvenlik.kullanici_ekle(db, istek.eposta, ad, istek.rol, secrets.token_urlsafe(32))
            except ValueError as e:
                raise HTTPException(400, str(e))
            denetle(db, "kullanici_eklendi_panel", yapan.id, ip(request), hedef=yeni.eposta, rol=yeni.rol)
            db.commit()
            hata = link_gonder(db, yeni, yapan, request)
            mesaj = ({"tur": "hata", "mesaj": f"{yeni.eposta} eklendi ama davet gitmedi: {hata}"} if hata else
                     {"tur": "basari", "mesaj": f"{yeni.eposta} eklendi; parolasını belirlemesi için davet maili gönderildi."})
            return {"mesaj": mesaj, "kullanicilar": kullanicilar_json(db)}

    # Kişiye parola linki gönderir.
    @api.post("/kullanicilar/{kullanici_id}/parola-linki")
    def parola_linki_gonder(request: Request, kullanici_id: int):
        with Oturum() as db:
            yapan = admin_gerekli(request, db, "parola_linki")
            hedef = db.get(Kullanici, kullanici_id)
            if hedef is None or hedef.api_hesabi:
                raise HTTPException(404, "Kullanıcı bulunamadı.")
            if not hedef.aktif:
                raise HTTPException(400, "Pasif kullanıcıya link gönderilmez; önce yeniden açın.")
            hata = link_gonder(db, hedef, yapan, request)
            mesaj = ({"tur": "hata", "mesaj": hata} if hata else
                     {"tur": "basari", "mesaj": f"{hedef.eposta} adresine parola belirleme linki gönderildi."})
            return {"mesaj": mesaj, "kullanicilar": kullanicilar_json(db)}

    # Kullanıcıyı aktif ya da pasif yapar.
    @api.post("/kullanicilar/{kullanici_id}/aktiflik")
    def aktiflik(request: Request, kullanici_id: int, istek: AktiflikIstegi):
        with Oturum() as db:
            yapan = admin_gerekli(request, db, "aktiflik")
            hedef = db.get(Kullanici, kullanici_id)
            if hedef is None or hedef.api_hesabi:
                raise HTTPException(404, "Kullanıcı bulunamadı.")
            try:
                guvenlik.aktiflik_degistir(db, hedef, istek.aktif, yapan, ip(request))
            except ValueError as e:
                raise HTTPException(400, str(e))
            return {"kullanicilar": kullanicilar_json(db)}

    # Maildeki link, giriş gerektirmez (parolası olmayan kişi giremez). Link 256 bit rastgele, tahmin edilemez.
    # Maildeki parola linkinin geçerli olup olmadığını söyler. Giriş gerektirmez.
    @api.post("/parola-linki/kontrol")
    def parola_linki_kontrol(istek: ParolaLinkiIstegi):
        with Oturum() as db:
            link = guvenlik.parola_linki_bul(db, istek.token)
            if link is None:
                raise HTTPException(400, "Link geçersiz ya da süresi dolmuş. Yöneticinizden yeni link isteyin.")
            k = db.get(Kullanici, link.kullanici_id)
            return {"eposta": k.eposta, "ad": k.ad, "tur": link.tur}

    # Linkle gelen kişi yeni parolasını belirler.
    @api.post("/parola-linki/kullan")
    def parola_linki_kullan(request: Request, istek: ParolaLinkiIstegi):
        with Oturum() as db:
            try:
                k = guvenlik.parola_belirle(db, istek.token, istek.parola, ip(request))
            except ValueError as e:
                raise HTTPException(400, str(e))
            request.session.clear()  # açık bir oturum varsa (başka kullanıcının) kapanır, kişi yeni parolayla girer
            return {"tur": "basari", "mesaj": f"Parolanız belirlendi. {k.eposta} ile giriş yapabilirsiniz.",
                    "csrf": csrf(request)}

    # Giriş yapmış kişi kendi parolasını değiştirir.
    @api.post("/parolam")
    def parolam(request: Request, istek: ParolaDegistirIstegi):
        with Oturum() as db:
            kullanici = giris_gerekli(request, db)
            try:
                guvenlik.parola_degistir(db, kullanici, istek.eski, istek.yeni, ip(request))
            except ValueError as e:
                raise HTTPException(400, str(e))
            oturumu_ac(request, kullanici, mfa_ile=bool(request.session.get("mfa")))  # iz parolaya bağlı, yenile
            return {"tur": "basari", "mesaj": "Parolanız değiştirildi.", "csrf": csrf(request)}

    # Denetim kaydını süzgeçli ve sayfa sayfa verir.
    @api.get("/denetim")
    def denetim(request: Request, islem: str = "", kullanici_id: int | None = None, once: int | None = None):
        """En yeni 100 kayıt. `once` verilirse o id'den eski olanlar döner, sayfalama için."""
        with Oturum() as db:
            admin_gerekli(request, db, "denetim")
            # En yeni 100 kayıt, işlem/kişi süzgeci ve "daha eskileri" için sayfalama.
            sorgu = select(Denetim).order_by(Denetim.id.desc()).limit(100)
            if islem:
                sorgu = sorgu.where(Denetim.islem == islem)
            if kullanici_id is not None:
                sorgu = sorgu.where(Denetim.kullanici_id == kullanici_id)
            if once is not None:
                sorgu = sorgu.where(Denetim.id < once)
            adlar = {k.id: k.ad for k in db.scalars(select(Kullanici))}
            kayitlar = [{"id": d.id, "zaman": _zaman(d.zaman), "kullanici_id": d.kullanici_id,
                         "kullanici": adlar.get(d.kullanici_id), "islem": d.islem, "detay": d.detay, "ip": d.ip}
                        for d in db.scalars(sorgu)]
            islemler = list(db.scalars(select(Denetim.islem).group_by(Denetim.islem).order_by(func.count().desc())))
            return {"kayitlar": kayitlar, "islemler": islemler,
                    "kullanicilar": [{"id": i, "ad": a} for i, a in sorted(adlar.items(), key=lambda x: x[1])]}

    # --- panel ayarları (sadece admin)

    # Panelden değiştirilebilen ayarlar, her birinin değeri ve nereden geldiği (panel, .env, varsayılan). Şifre gönderilmez.
    @api.get("/ayarlar")
    def ayarlari_getir(request: Request):
        with Oturum() as db:
            admin_gerekli(request, db, "ayarlari_getir")
            return panel_ayarlari.panel_gorunumu(db, gizli_anahtar)

    # Ayarları kaydeder. Değişen ayarların adları denetim kaydına yazılır, değerleri yazılmaz (şifre olabilir).
    @api.put("/ayarlar")
    def ayarlari_kaydet(request: Request, istek: AyarIstegi):
        with Oturum() as db:
            kullanici = admin_gerekli(request, db, "ayarlari_kaydet")
            try:
                degisen = panel_ayarlari.kaydet(db, istek.degisiklikler, istek.surum, kullanici.id, gizli_anahtar)
            except ValueError as e:
                raise HTTPException(400, str(e))
            except panel_ayarlari.CakismaHatasi as e:
                raise HTTPException(409, str(e))
            if degisen:
                denetle(db, "panel_ayarlari", kullanici.id, ip(request), degisen=degisen)
            db.commit()
            return {**panel_ayarlari.panel_gorunumu(db, gizli_anahtar),
                    "mesaj": {"tur": "basari", "mesaj": f"{len(degisen)} ayar kaydedildi." if degisen else "Değişiklik yok."}}

    # Geçerli mail ayarıyla tek bir adrese deneme maili atar.
    @api.post("/ayarlar/mail-dene")
    def ayar_mail_dene(request: Request, istek: MailDenemeIstegi):
        with Oturum() as db:
            kullanici = admin_gerekli(request, db, "ayar_mail_dene")
            adresler, hatali = alicilar.adresleri_ayikla(istek.adres)
            if hatali or len(adresler) != 1:
                raise HTTPException(400, "Tek bir geçerli e-posta adresi yazın.")
            a = ayar(db)
            if a.mail_kapali:
                return {"tur": "hata", "mesaj": "SMTP sunucusu tanımlı değil, mail gönderilmez."}
            simdi = datetime.now().strftime("%d.%m.%Y %H:%M")
            metin = f"Bu bir deneme mailidir ({simdi}). Mail ayarları çalışıyor, bir şey yapmanıza gerek yok."
            try:
                a.gonderici.gonder(Mail(konu=f"Mevzuat Takip — deneme maili ({simdi})", html=f"<p>{metin}</p>",
                                        metin=metin, alicilar=adresler))
            except Exception as e:
                return {"tur": "hata", "mesaj": f"Mail gönderilemedi: {e.__class__.__name__}: {str(e)[:200]}"}
            finally:
                denetle(db, "ayar_mail_dene", kullanici.id, ip(request), adres=adresler[0])
                db.commit()
            return {"tur": "basari", "mesaj": f"Deneme maili {adresler[0]} adresine gönderildi."}

    # --- API anahtarları (sadece panele giriş yapmış admin, anahtarla anahtar yönetilmez)

    def anahtar_yoneticisi(request: Request, db: Session, islem: str) -> Kullanici:
        if istek_anahtari(request):
            raise HTTPException(403, "API anahtarları sadece panelden yönetilir.")
        return admin_gerekli(request, db, islem)

    def anahtar_json(k: ApiAnahtari, hesap: Kullanici | None, adlar: dict[int, str]) -> dict:
        simdi = datetime.now()
        durum = ("iptal" if k.iptal else "suresi_doldu" if k.son_kullanma and k.son_kullanma <= simdi else "aktif")
        return {"id": k.id, "ad": k.ad, "rol": hesap.rol if hesap else None, "on_ek": k.on_ek, "durum": durum,
                "olusturan": adlar.get(k.olusturan_id), "olusturuldu": _zaman(k.olusturuldu),
                "son_kullanma": _zaman(k.son_kullanma), "son_kullanim": _zaman(k.son_kullanim), "iptal": _zaman(k.iptal)}

    def anahtar_listesi(db: Session) -> list[dict]:
        anahtarlar = list(db.scalars(select(ApiAnahtari).order_by(ApiAnahtari.id.desc())))
        hesaplar = {k.id: k for k in db.scalars(select(Kullanici).where(
            Kullanici.id.in_({a.kullanici_id for a in anahtarlar} | {a.olusturan_id for a in anahtarlar if a.olusturan_id})))}
        adlar = {i: k.ad for i, k in hesaplar.items()}
        return [anahtar_json(a, hesaplar.get(a.kullanici_id), adlar) for a in anahtarlar]

    @api.get("/api-anahtarlari")
    def api_anahtarlari(request: Request):
        """API anahtarlarının listesi. Anahtarın kendisi listede yoktur, sadece ilk harfleri."""
        with Oturum() as db:
            anahtar_yoneticisi(request, db, "api_anahtarlari")
            return {"anahtarlar": anahtar_listesi(db), "baslik": "Authorization: Bearer"}

    @api.post("/api-anahtarlari")
    def api_anahtari_uret(request: Request, istek: ApiAnahtariIstegi):
        """Yeni anahtar üretir. Anahtar sadece bu cevapta bir kez gösterilir."""
        with Oturum() as db:
            yapan = anahtar_yoneticisi(request, db, "api_anahtari_uret")
            try:
                kayit, anahtar = guvenlik.api_anahtari_uret(db, istek.ad, istek.rol, istek.gun, yapan, ip(request),
                                                            gizli_anahtar=gizli_anahtar)
            except ValueError as e:
                raise HTTPException(400, str(e))
            db.commit()
            return {"anahtar": anahtar, "anahtarlar": anahtar_listesi(db),
                    "mesaj": {"tur": "basari", "mesaj": f"'{kayit.ad}' anahtarı üretildi. Anahtarı şimdi kopyalayın, "
                              "bir daha gösterilmeyecek."}}

    @api.post("/api-anahtarlari/{anahtar_id}/iptal")
    def api_anahtari_iptal(request: Request, anahtar_id: int):
        with Oturum() as db:
            yapan = anahtar_yoneticisi(request, db, "api_anahtari_iptal")
            kayit = db.get(ApiAnahtari, anahtar_id)
            if kayit is None:
                raise HTTPException(404, "Anahtar bulunamadı.")
            guvenlik.api_anahtari_iptal(db, kayit, yapan, ip(request))
            db.commit()
            return {"anahtarlar": anahtar_listesi(db),
                    "mesaj": {"tur": "basari", "mesaj": f"'{kayit.ad}' anahtarı iptal edildi, artık çalışmaz."}}

    # --- menü ve API dokümanı

    # Menü öğesinin yetkisi bu kullanıcıda var mı. Bilinmeyen yetki kimseye gösterilmez.
    def menu_yetkisi(kullanici: Kullanici, yetki: str) -> bool:
        return {"herkes": True, "panel": kullanici.rol != API_ROLU, "grup": kullanici.rol in GRUP_ROLLERI,
                "ayar": kullanici.rol in AYAR_ROLLERI, "kurtarma": kullanici.rol in KURTARMA_ROLLERI,
                "dokuman": kullanici.rol in DOKUMAN_ROLLERI}.get(yetki, False)

    # Sol menü veritabanından gelir.
    @api.get("/menu")
    def menu(request: Request):
        """Sol menü. Kullanıcının yetkisine uyan aktif öğeler, sırasıyla. `adres` doluysa öğe yeni sekmede açılan bağlantıdır."""
        with Oturum() as db:
            kullanici = giris_gerekli(request, db)
            ogeler = db.scalars(select(MenuOgesi).where(MenuOgesi.aktif).order_by(MenuOgesi.sira, MenuOgesi.anahtar))
            return [{"anahtar": o.anahtar, "etiket": o.etiket, "aciklama": o.aciklama, "ikon": o.ikon, "adres": o.adres}
                    for o in ogeler if menu_yetkisi(kullanici, o.yetki)]

    # API'nin OpenAPI tanımı. Panele giriş yapmış kullanıcı ya da geçerli API anahtarı ister.
    # İndirilen dokümana panel adresi sunucu adresi olarak yazılır (Postman ve bilgisayardan açılan HTML istekleri oraya atar).
    # Sayfadaki doküman zaten bu sunucudan açıldığı için adres yazılmaz, istekler sayfanın adresine gider.
    def openapi_semasi(db: Session, sunucu_adresli: bool = False) -> dict:
        # Dokümanda sadece dış sistemlerin adresleri (/api) var, panelin kendi adresleri yok.
        sema = get_openapi(title="Mevzuat Takip API", version="1.0", routes=portal_yolu.routes,
                           tags=panel_belgesi.ETIKETLER)
        adres = ayar(db).panel_adresi
        if adres and sunucu_adresli:
            sema["servers"] = [{"url": adres, "description": "Mevzuat Takip"}]
        return sema

    # Oturumla gelen onaylayıcı dokümanı göremez, anahtarla gelen (dış sistem) görür.
    def dokuman_gerekli(request: Request, db: Session) -> None:
        # Doküman açıksa (varsayılan) kimse sorulmaz. Şirket portalı paneli kullanmaz, dokümanı doğrudan açar.
        if ayar(db).dokuman_acik:
            return
        kullanici = giris_gerekli(request, db)
        if not anahtarla_mi(request) and kullanici.rol not in DOKUMAN_ROLLERI:
            raise HTTPException(403, "API dokümanına erişim yetkiniz yok.")

    @dokuman_yolu.get("/dokuman/openapi.json", include_in_schema=False, dependencies=[Depends(imza_kontrol)])
    def openapi_tanimi(request: Request):
        with Oturum() as db:
            dokuman_gerekli(request, db)
            return openapi_semasi(db)

    # Swagger sayfasının kabuğu herkese açılır, içinde veri yoktur. Doküman açıksa (varsayılan) Swagger doğrudan gelir,
    # kapalıysa sayfa API anahtarı ister ve doküman (openapi.json) ancak oturumla ya da geçerli anahtarla gelir.
    @dokuman_yolu.get("/dokuman", include_in_schema=False)
    def api_dokumani():
        return HTMLResponse(SWAGGER_SAYFASI)

    # Dokümanı dosya olarak verir. html, internet ve giriş gerektirmeden açılan tek dosya. json, Postman gibi araçlara yüklenir.
    @dokuman_yolu.get("/dokuman/indir", include_in_schema=False, dependencies=[Depends(imza_kontrol)])
    def api_dokumani_indir(request: Request, bicim: Literal["html", "json"] = "html"):
        with Oturum() as db:
            dokuman_gerekli(request, db)
            sema = openapi_semasi(db, sunucu_adresli=True)
        tarih = datetime.now().strftime("%Y%m%d")
        if bicim == "json":
            return JSONResponse(sema, headers={"Content-Disposition": f'attachment; filename="mevzuat-api-{tarih}.json"'})
        klasor = (arayuz / "swagger") if arayuz else None
        if klasor is None or not (klasor / "swagger-ui-bundle.js").is_file():
            raise HTTPException(503, "Swagger dosyaları bulunamadı, arayüz derlenmemiş.")
        return Response(_tek_dosya_dokuman(sema, klasor), media_type="text/html; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="mevzuat-api-{tarih}.html"'})

    # Dış sistemlerin adresleri (/api), portal bunları kullanır. Mevzuat okuma kendi fonksiyonlarıyla, onay, tarama, kaynak,
    # konu ve alıcı grubu panelin aynı fonksiyonlarıyla ama sabit cevap biçimiyle (web/portal.py).
    portal_yolu = portal.portal_api(Oturum, giris_gerekli, [Depends(csrf_dogrula), Depends(imza_kontrol),
                                                             *anahtar_semalari])
    from mevzuat.web import portal_entegrasyon
    portal_entegrasyon.ekle(portal_yolu, Oturum, giris_gerekli, rapor_detay)
    surumlu = portal.yazma_adreslerini_ekle(portal_yolu, api.routes, panel_belgesi.BELGE, API_GRUPLARI)
    # Her uç noktanın başlığı, açıklaması, yetkisi ve hata cevapları (web/panel_belgesi.py). dış adreslere açılan panel
    # adresleri dokümanda tekrar görünmez, panel onları kullanmaya devam eder.
    panel_belgesi.belgele(api.routes, gizli=surumlu)
    # Dokümanda uç noktalar adreslerine göre gruplanır.
    for yol in api.routes:
        if isinstance(yol, APIRoute):
            yol.tags = [API_GRUPLARI.get(yol.path.removeprefix(PANEL_ON_EK).split("/")[1], "Diğer")]
    # API adreslerini uygulamaya ekle. Doküman önce, /api/{...} adresleriyle karışmasın.
    app.include_router(dokuman_yolu)
    app.include_router(portal_yolu)
    app.include_router(api)

    # --- sağlık ve arayüz dosyaları

    # Sağlık kontrolü adresi (Docker "panel ayakta mı" diye buna bakıyor).
    @app.get("/saglik", response_class=PlainTextResponse, include_in_schema=False)
    def saglik() -> str:
        return "ok"

    # Bilinmeyen /api ve /panel/api adresine 404.
    @app.get("/api/{yol:path}", include_in_schema=False)
    @app.get("/panel/api/{yol:path}", include_in_schema=False)
    def api_bulunamadi(yol: str):
        return JSONResponse({"detail": "Bulunamadı."}, status_code=404)

    # Diğer bütün adresler, derlenmiş arayüz dosyaları.
    @app.get("/{yol:path}", include_in_schema=False)
    def arayuz_dosyasi(yol: str):
        """Derlenmiş React arayüzü. Dosya varsa onu, yoksa index.html'i döner (arayüz tek sayfa)."""
        if arayuz is None or not (arayuz / "index.html").is_file():
            return PlainTextResponse("Arayüz derlenmemiş: frontend klasöründe `npm ci && npm run build`.", status_code=503)
        kok = arayuz.resolve()
        dosya = (kok / yol).resolve()
        if yol and dosya.is_file() and dosya.is_relative_to(kok):  # ../ ile klasör dışına çıkılamaz
            return FileResponse(dosya)
        return FileResponse(kok / "index.html")

    return app


# uvicorn'un çağırdığı fonksiyon, ayarları .env'den okuyup paneli kurar.
def ayardan_olustur() -> FastAPI:
    """uvicorn --factory için, ayarları ortamdan okur."""
    from dotenv import load_dotenv

    from mevzuat.db import make_engine

    load_dotenv()
    anahtar = os.environ.get("MEVZUAT_GIZLI_ANAHTAR", "")
    engine = make_engine()
    tanimlar.hazirla(engine)
    arayuz = os.environ.get("MEVZUAT_ARAYUZ")
    # Mail, panel adresi, MFA gibi ayarlar her istekte okunur, panelden değişince yeniden başlatma gerekmez.
    with Session(engine) as db:
        ilk = panel_ayarlari.etkin(db, anahtar)
    if not ilk["panel_adresi"]:
        log.warning("Panel adresi boş: onay maillerinde panel düğmesi olmaz, davet/parola linkleri gönderilemez.")
    return uygulama_olustur(
        engine=engine,
        gonderici=panel_ayarlari.gonderici(ilk),
        is_kollari=None,  # grup formundaki iş kolları her istekte DB'deki konulardan
        gizli_anahtar=anahtar,
        https=os.environ.get("MEVZUAT_HTTPS") == "1",
        arayuz=Path(arayuz) if arayuz else VARSAYILAN_ARAYUZ,
        ayar_oku=lambda db: panel_ayarlari.etkin(db, anahtar),
    )
