"""Henüz raporlanmamış ilgili kayıtlardan rapor maili üretir ve gönderir.

Mailde link yok, bu bir karar. Okuyucu her kalemi kaynakçayla doğrular. Taranmış belgelerin
OCR metni güvenilir olmadığı için orijinal PDF ek olarak konur, MEVZUAT_EK_EKLE=0 ile kapatılır.
"""

# Rapor işinin hepsi burada, kayıtlardan rapor maili üretmek (özet, yürürlük, tablo), onaya sunmak,
# onay/ret kararını işlemek ve onaylanan raporu kişi başı maillerle "en fazla bir kez" dağıtmak.
import logging
import re
# html_kacis, metindeki < > & işaretlerini zararsız hale getirir.
from html import escape as html_kacis
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import httpx
# Jinja2, mail şablonlarını (sablonlar/rapor.html.j2) doldurmak için şablon motoru.
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from mevzuat import alicilar
from mevzuat.db import Gonderim, Kayit, KonuTanimi, Rapor
from mevzuat.filtre import TUM_DUYURULAR, tr_kucuk
from mevzuat.mail import Ek, Mail, MailGonderici

log = logging.getLogger(__name__)

# Raporda bir belgenin en fazla 1500 karakteri gösterilir (tamamı ekte).
OZET_UZUNLUGU = 1500  # karakter, tam metin ekte
# "Neden size geldi" kısmında en fazla 3 cümle.
ONE_CIKAN_SAYISI = 3
# Maile eklenen PDF'lerin toplamı en fazla 15 MB.
EK_TOPLAM_SINIR = 15 * 1024 * 1024  # Gmail sınırı 25 MB, base64 kodlama boyutu ~%33 büyütür

# Şablon ortamı, şablonlar "sablonlar" klasöründen okunur.
_sablonlar = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "sablonlar"),
    # İçerik dış sitelerden geliyor, HTML'de mutlaka kaçışla. Uzantı ".html.j2" olarak verilmeli,
    # sadece "html" yazılırsa "rapor.html.j2" eşleşmez ve kaçış sessizce kapalı kalır (test yakaladı).
    autoescape=select_autoescape(enabled_extensions=("html.j2",), default_for_string=True),
    trim_blocks=True,
    lstrip_blocks=True,
)


# Rapordaki tek bir kalem (bir belge) için hazırlanmış her şey.
@dataclass
class RaporKalemi:
    no: int
    kayit: Kayit
    tur_adi: str  # "Cumhurbaşkanı Kararı", "Yönetmelik" …
    ozet: str | None  # işlemin asıl hükmü (genelde MADDE 1'in ilk cümlesi)
    ozet_tablosu: list[str]  # özet "aşağıdaki tabloda" diyorsa tablo satırları
    yururluk: str | None
    one_cikanlar: list[str]  # eşleşen kelimenin geçtiği diğer cümleler ("neden size geldi" kanıtı)
    kaynakca: str
    metin: str | None  # kısaltılmış içerik (panelde gösterilir, mailde değil)
    kisaltildi: bool
    ocr: bool
    ek_adi: str | None = None
    # "Güncel metni değişti" kalemleri (surum.py), [{bolum, tur, eski, yeni}]. Doluysa özet/tablo/yürürlük yerine gösterilir.
    degisiklikler: list[dict] | None = None
    # "Neden önemli" satırları, [(konu, açıklama)], rapor_olustur doldurur.
    nedenler: list[tuple[str, str]] = field(default_factory=list)
    # Belgenin resmî kaynaktaki adresi (sadece http ve https), rapor_olustur doldurur.
    kaynak_linki: str | None = None


# RG alt başlıkları büyük harf ve çoğul ("CUMHURBAŞKANI KARARLARI"), raporda tekil ve okunur ad.
TUR_ADLARI = {
    "CUMHURBAŞKANI KARARLARI": "Cumhurbaşkanı Kararı", "CUMHURBAŞKANI KARARI": "Cumhurbaşkanı Kararı",
    "CUMHURBAŞKANLIĞI KARARNAMELERİ": "Cumhurbaşkanlığı Kararnamesi",
    "YÖNETMELİKLER": "Yönetmelik", "YÖNETMELİK": "Yönetmelik", "TEBLİĞLER": "Tebliğ", "TEBLİĞ": "Tebliğ",
    "KANUNLAR": "Kanun", "KANUN": "Kanun", "GENELGELER": "Genelge", "GENELGE": "Genelge",
    "KURUL KARARLARI": "Kurul Kararı", "KURUL KARARI": "Kurul Kararı", "KARARLAR": "Karar", "KARAR": "Karar",
    "ATAMA KARARLARI": "Atama Kararı", "ATAMA KARARI": "Atama Kararı",
    "MİLLETLERARASI ANDLAŞMALAR": "Milletlerarası Andlaşma", "MİLLETLERARASI ANDLAŞMA": "Milletlerarası Andlaşma",
}


# Belgenin türünü okunur hale getirir, örneğin "YÖNETMELİKLER" yerine "Yönetmelik" yazar.
def tur_adi(kayit: Kayit) -> str:
    if kayit.tur in TUR_ADLARI:
        return TUR_ADLARI[kayit.tur]
    # Tabloda yoksa ve büyük harfse her kelimenin sadece ilk harfini büyük bırak.
    if kayit.tur and kayit.tur.isupper():
        # tr_kucuk arama içindir ve şapkalı harfleri sadeleştirir, gösterimde ise "Hâkimler" gibi yazılar korunmalı.
        kucuk = lambda m: m.replace("İ", "i").replace("I", "ı").lower()  # noqa: E731
        return " ".join(k[:1] + kucuk(k[1:]) for k in kayit.tur.split())
    return kayit.tur or "Belge"


# Metni cümlelere böler.
def _cumleler(metin: str) -> list[str]:
    # Satır sonları cümle sonu değildir (OCR ve PDF satırları cümleyi ortadan böler), önce
    # satırları birleştir, sonra noktalama ve "MADDE n" başlangıçlarından böl.
    duz = re.sub(r"\s+", " ", metin)
    # Kalıptaki geriye bakma kısmı, "G.T.İ.P." gibi tek harfli kısaltmalardaki noktada bölmeyi engeller.
    parcalar = re.split(r"(?<=[.;:])(?<!\b\w\.)\s+|\s+(?=MADDE \d)", duz)
    return [c.strip() for c in parcalar if c.strip()]


# Uzun cümleyi kısaltır ama hem başını hem sonunu korur (Türkçede fiil sondadır).
def _kisalt(metin: str, uzunluk: int = 350) -> str:
    """Uzun cümleyi hem baştan hem sondan koruyarak kısaltır. Türkçede yüklem sondadır, sadece baştan
    kesmek "…bilgiler güncellenmiştir" gibi asıl bilgiyi, yani ne yapıldığını siler."""
    if len(metin) <= uzunluk:
        return metin
    # Baştan %55, sondan %40'ını al, ortaya "…" koy.
    bas = metin[: uzunluk * 55 // 100].rsplit(" ", 1)[0]
    son = metin[-(uzunluk * 40 // 100):].split(" ", 1)[-1]
    return f"{bas} … {son}"


# Cümle başlığın tekrarı mı diye bakar. 4 harften uzun kelimelerin yüzde 70'inden fazlası aynıysa tekrar sayılır.
def _basliga_benziyor(cumle: str, baslik: str) -> bool:
    """Cümlenin başlığın tekrarı olup olmadığına bakar. OCR'da belgenin ilk cümlesi çoğu zaman başlığın aynısıdır."""
    kelime = lambda m: set(re.findall(r"\w{4,}", tr_kucuk(m)))  # noqa: E731
    c, b = kelime(cumle), kelime(baslik)
    return bool(c) and len(c & b) / len(c) > 0.7


# "MADDE 3 – (1)" gibi madde başlangıçlarını yakalayan kalıp.
MADDE_ONEKI = re.compile(r"^MADDE \d+\s*[-–—]\s*(\(\d+\)\s*)?")


# Metni maddelere böler, başlarındaki "MADDE n –" kısmını atar.
def _maddeler(metin: str) -> list[str]:
    """Metni maddelere böler (öneksiz). Madde yapısı yoksa boş liste."""
    duz = re.sub(r"\s+", " ", metin)
    parcalar = [p.strip() for p in re.split(r"(?=\bMADDE \d+\s*[-–—])", duz) if p.startswith("MADDE")]
    sonuc = []
    for p in parcalar:
        govde = MADDE_ONEKI.sub("", p)
        # Bir sonraki maddenin başlığı ("Yürütme", "Yürürlük") bu maddenin sonuna yapışır, son noktadan kes.
        if "." in govde:
            govde = govde[: govde.rfind(".") + 1]
        sonuc.append(govde)
    return sonuc


# Belirli numaralı maddenin metnini verir.
def _madde(metin: str, no: int) -> str | None:
    duz = re.sub(r"\s+", " ", metin)
    for p, govde in zip(re.findall(r"\bMADDE (\d+)\s*[-–—]", duz), _maddeler(metin)):
        if int(p) == no:
            return govde
    return None


# Özet cümlesi, mevzuatta MADDE 1'in ilk cümlesi, madde yoksa başlığı tekrarlamayan ilk uzun cümle.
def ozet_cumlesi(metin: str, baslik: str) -> str | None:
    """Mevzuatta asıl hüküm MADDE 1'dedir. Madde yoksa (ör. duyuru) başlığı tekrarlamayan ilk anlamlı cümle."""
    # Madde bazında oku, "Bu Tebliğin amacı; …" gibi maddeler ";" ile başlar, cümle bölücü onları keserdi.
    madde1 = _madde(metin, 1)
    if madde1:
        ilk_cumle = re.split(r"(?<=\.)(?<!\b\w\.)\s+", madde1, maxsplit=1)[0]
        return _kisalt(ilk_cumle) if len(ilk_cumle) > 20 else None
    for c in _cumleler(metin):
        if len(c) >= 40 and not _basliga_benziyor(c, baslik):
            return _kisalt(c)
    return None


# MADDE 1 "aşağıdaki tabloda" diyorsa tablonun satırlarını çıkarır (ör. ÖTV tutarları).
def ozet_tablosu(metin: str) -> list[str]:
    """MADDE 1 "aşağıdaki tabloda" diyorsa tablonun satırlarını bir sonraki maddeye kadar döner.
    Örneğin ÖTV kararında tutarlar sadece tablodadır ve özetin asıl bilgisi onlardır."""
    # Boş olmayan satırlar.
    satirlar = [s.strip() for s in metin.splitlines() if s.strip()]
    # "MADDE 1" ile başlayan satırı bul.
    madde1 = next((i for i, s in enumerate(satirlar) if re.match(r"MADDE 1\b", s)), None)
    if madde1 is None:
        return []
    # "tabloda" geçen satırı MADDE 1 içinde ara (MADDE 2'ye gelirsen tablo yok demektir).
    i = madde1
    while i < len(satirlar) and "tabloda" not in tr_kucuk(satirlar[i]):
        i += 1
        if i < len(satirlar) and re.match(r"MADDE \d", satirlar[i]):
            return []
    # Cümle "tabloda" satırından sonra devam edebilir, tablo cümle bittikten sonra başlar.
    while i < len(satirlar) and not satirlar[i].endswith((".", ":")):
        i += 1
    # Tablo satırlarını topla (en fazla 6 satır ya da sonraki maddeye kadar).
    tablo = []
    for satir in satirlar[i + 1:]:
        if re.match(r"MADDE \d", satir) or len(tablo) == 6:
            break
        tablo.append(satir)
    return tablo


# "Yürürlüğe girer" geçen maddeyi bulur (en sondakini, yürürlük maddesi genelde sondadır).
def _yururluk_hukmu(metin: str) -> str | None:
    """Yürürlük hükmünün tamamı. Madde bazında okunur. Noktalı virgülden cümlelere bölünseydi kademeli yürürlükte
    "a) … 1/1/2027 tarihinde; b) diğer hükümleri yayımı tarihinde yürürlüğe girer" maddesinin a) kısmı kaybolurdu."""
    adaylar = [m for m in _maddeler(metin) if "yürürlüğe gir" in tr_kucuk(m)]
    if adaylar:
        return adaylar[-1]
    # Madde yapısı yoksa (ör. duyuru) cümle bazında
    return next((c for c in _cumleler(metin) if "yürürlüğe gir" in tr_kucuk(c)), None)


# Yürürlük hükmünü sadeleştirir, örneğin "yayımı tarihinde" yazısı tarihiyle birlikte gösterilir.
def yururluk(metin: str, yayin_tarihi: date) -> str | None:
    """Yürürlük hükmünü sadeleştirir. Tek ve basit bir hüküm değilse hükmün kendisini verir."""
    hukum = _yururluk_hukmu(metin)
    if hukum is None:
        return None
    k = tr_kucuk(hukum)
    # Hükümdeki tarihler (1/1/2027 biçiminde).
    tarihler = re.findall(r"\b\d{1,2}/\d{1,2}/\d{4}\b", hukum)
    # Kademeli yürürlük tek tarihe indirgenemez, tek tarih yazmak, hükümlerin çoğu yürürlükteyken
    # "2027'de başlıyor" demek olur. Bu durumda hükmün kendisi gösterilir.
    kademeli = (
        bool(re.search(r"(?:^|[\s;,])[a-zçğıöşü]\)", hukum))
        or k.count("yürürlüğe gir") > 1
        or len(tarihler) > 1
        or bool(tarihler and ("yayımı tarihinde" in k or "yayımını izleyen" in k))
    )
    # Kısa ve tek tarihli/basit hükümse sadeleştir.
    if len(hukum) < 120 and not kademeli:
        if "yayımını izleyen gün" in k:
            return f"Yayımını izleyen gün ({yayin_tarihi + timedelta(days=1):%d.%m.%Y})"
        if "yayımı tarihinde" in k and not tarihler:
            return f"Yayım tarihinde ({yayin_tarihi:%d.%m.%Y})"
        if len(tarihler) == 1:
            g, a, y = tarihler[0].split("/")
            return f"{int(g):02d}.{int(a):02d}.{y}"
    # Değilse hükmün kendisini (kısaltarak) göster.
    return _kisalt(hukum, 300)


# Kaynakçayı kısaltır, başlığı tekrar etmez, varsa karar sayısını korur.
def kisa_kaynakca(kayit: Kayit) -> str:
    """Kaynakça başlığı tekrar etmesin diye kısaltır, varsa karar ya da seri numarasını korur. Örneğin
        "Resmî Gazete, 01.10.2026, Sayı: 33387, CUMHURBAŞKANI KARARLARI: <başlık>"
    şu hale gelir.
        "Resmî Gazete, 01.10.2026, Sayı: 33387 — Cumhurbaşkanı Kararı (Karar Sayısı: 11822)"
    """
    kaynakca = kayit.kaynakca
    if not kaynakca.endswith(kayit.baslik):
        return kaynakca
    kaynakca = kaynakca[: -len(kayit.baslik)].rstrip(" :,")
    bas, _, son = kaynakca.rpartition(",")
    if bas and son.strip().isupper():  # büyük harfli tür etiketi (RG alt başlığı)
        kaynakca = f"{bas} — {tur_adi(kayit)}"
    no = re.search(r"\(((?:Karar Sayısı|Karar|Sıra No|Seri No|No|Sayı)\s*:[^)]*)\)\s*$", kayit.baslik)
    if no:
        kaynakca += f" ({no.group(1).strip()})"
    return kaynakca


# Rapora girmeyi bekleyen kayıtlar, ilgili, henüz hiçbir rapora bağlanmamış ve içeriği indirilmiş.
def bekleyen_kayitlar(session: Session) -> list[Kayit]:
    """İlgili, henüz raporlanmamış ve içerik adımı bitmiş kayıtlar."""
    return list(
        session.scalars(
            select(Kayit)
            .where(Kayit.ilgili, Kayit.rapor_id.is_(None), Kayit.icerik_durumu != "BEKLIYOR")
            .order_by(Kayit.yayin_tarihi, Kayit.kaynak, Kayit.id)
        )
    )


# Raporda konu adının altında gösterilecek açıklamalar, eski adlar da aynı açıklamaya gider.
def konu_aciklamalari(session: Session) -> dict[str, str]:
    """Açıklaması olan her konunun adı ve eski adları, açıklamasıyla. Pasif konular da dahil, eski kayıtlar onlara bağlı olabilir."""
    sonuc = {}
    for k in session.scalars(select(KonuTanimi).where(KonuTanimi.aciklama != "")):
        for ad in (*k.eski_adlar, k.ad):
            sonuc[ad] = k.aciklama
    return sonuc


# Kalemin "Neden önemli" açıklamaları, sadece başlıkta kelimesi yakalanan konular için.
def neden_onemli(kayit: Kayit, aciklamalar: dict[str, str]) -> list[tuple[str, str]]:
    """Kaynağın tüm duyurularına bağlı konular atlanır, yoksa o kaynağın her kaleminde aynı açıklamalar tekrar eder
    ve bilgi vermez (Reddit'te her gönderinin altında 8 açıklama çıktı)."""
    return [(konu, aciklamalar[konu]) for konu, kelimeler in kayit.eslesmeler.items()
            if konu in aciklamalar and kelimeler != [TUM_DUYURULAR]]


# Her CB kararında geçen kalıp giriş cümlesi, bilgi taşımaz.
KALIP_IFADELER = ("yürürlüğe konulmasına", "karar verilmiştir")


# Cümlenin başındaki "MADDE n –" kısmını atar.
def _madde_oneksiz(cumle: str) -> str:
    return re.sub(r"^MADDE \d+\s*[-–—]\s*(\(\d+\)\s*)?", "", cumle)


# "Neden size geldi", metinde eşleşen anahtar kelimelerin geçtiği cümleler.
def one_cikanlar(metin: str, eslesmeler: dict[str, list[str]], haric: tuple[str, ...] = ()) -> list[str]:
    """İçerikte eşleşen kelimelerin geçtiği cümleler ("neden size geldi" kanıtı)."""
    # Eşleşen kelimeler (sonundaki "(içerikte)" eki olmadan, parantezle başlayan açıklamalar hariç).
    kelimeler = {
        tr_kucuk(k.removesuffix(" (içerikte)"))
        for liste in eslesmeler.values()
        for k in liste
        if not k.startswith("(")
    }
    sonuc = []
    for cumle in _cumleler(metin):
        # Çok kısa ya da zaten eklenmiş cümleyi atla.
        if len(cumle) < 20 or cumle in sonuc:
            continue
        if any(_madde_oneksiz(cumle).startswith(h.rstrip("…")[:60]) for h in haric if h):
            continue  # özetle aynı cümle
        # Her kararda geçen kalıp cümleyi atla.
        if all(i in tr_kucuk(cumle) for i in KALIP_IFADELER):
            continue
        # Kelimelerden biri geçiyorsa ekle.
        if any(k in tr_kucuk(cumle) for k in kelimeler):
            sonuc.append(_kisalt(cumle, 300))
        if len(sonuc) == ONE_CIKAN_SAYISI:
            break
    return sonuc


# Metindeki web adreslerini yakalayan kalıp.
URL = re.compile(r"(https?://|www\.)\S+", re.IGNORECASE)


# Metindeki bütün adresleri "[bağlantı kaldırıldı]" yapar. Mailde tek link her kalemin resmî kaynak adresidir.
def linksiz(metin: str) -> str:
    # Metnin içindeki adresler silinir, Gmail/Outlook düz metindeki adresleri de tıklanabilir yapar.
    return URL.sub("[bağlantı kaldırıldı]", metin)


# Bir kaydı rapor kalemine çevirir, özet, tablo, yürürlük, öne çıkanlar...
def _kalem(no: int, kayit: Kayit) -> RaporKalemi:
    # Metni değişen mevzuat kalemi, özet/tablo yerine eski/yeni farkları gösterilir.
    if kayit.degisiklikler:
        # Metin zaten fark özeti, "MADDE 1'in ilk cümlesi" gibi çıkarımlar burada anlamsız.
        return RaporKalemi(
            no=no, kayit=kayit, tur_adi=tur_adi(kayit), ozet=None, ozet_tablosu=[], yururluk=None, one_cikanlar=[],
            kaynakca=kisa_kaynakca(kayit), metin=None, kisaltildi=False, ocr=False,
            degisiklikler=[{**d, "eski": linksiz(d["eski"] or ""), "yeni": linksiz(d["yeni"] or "")} for d in kayit.degisiklikler],
        )
    # Metni temizle (adresleri sil, fazla boş satırları azalt).
    metin = linksiz((kayit.icerik or "").replace("\r\n", "\n").strip())
    metin = re.sub(r"\n{3,}", "\n\n", metin) or None
    # Özet cümlesi ve (özetle aynı olmayan, başlığı tekrarlamayan) en fazla 2 öne çıkan cümle.
    ozet = ozet_cumlesi(metin, kayit.baslik) if metin else None
    one_cikan = [
        c for c in (one_cikanlar(metin, kayit.eslesmeler, haric=(ozet or "",)) if metin else [])
        if not _basliga_benziyor(c, kayit.baslik)
    ][:2]
    return RaporKalemi(
        no=no,
        kayit=kayit,
        tur_adi=tur_adi(kayit),
        ozet=ozet,
        ozet_tablosu=ozet_tablosu(metin) if metin else [],
        yururluk=yururluk(metin, kayit.yayin_tarihi) if metin else None,
        one_cikanlar=one_cikan,
        kaynakca=kisa_kaynakca(kayit),
        metin=metin[:OZET_UZUNLUGU] if metin else None,
        kisaltildi=bool(metin and len(metin) > OZET_UZUNLUGU),
        ocr=kayit.icerik_durumu == "OCR_ILE_OKUNDU",
    )


# Kalemin belgesi PDF ise indirip mail eki yapar (aynı PDF'i önbellekten tekrar kullanır).
def _ek_indir(client: httpx.Client, kalem: RaporKalemi, onbellek: dict[str, bytes] | None = None) -> Ek | None:
    """Sadece PDF'ler eklenir, HTML belgelerin metni zaten güvenilir ve mailin içinde.
    `onbellek` sayesinde aynı raporun birden çok maili aynı PDF'i tekrar tekrar indirmez."""
    url = kalem.kayit.url
    # PDF değilse ek yok.
    if not urlparse(url).path.lower().endswith(".pdf"):
        return None
    # Önbellekte yoksa indir ve önbelleğe koy.
    if onbellek is None or url not in onbellek:
        response = client.get(url)
        response.raise_for_status()
        if onbellek is None:
            onbellek = {}
        onbellek[url] = response.content
    # Ek dosya adı, "01_20261001_belge.pdf".
    ad = f"{kalem.no:02d}_{kalem.kayit.yayin_tarihi:%Y%m%d}_{PurePosixPath(urlparse(url).path).name}"
    return Ek(ad, onbellek[url], "application/pdf")


# Belgenin resmî kaynaktaki adresi, her kalemin altında ve maile eklenemeyen belgeler listesinde.
def _belge_linki(kalem: RaporKalemi) -> dict:
    """Belgenin resmî kaynaktaki adresi. Rapor metninin içindeki adresler yine silinir.
    Sadece http ve https kabul edilir, javascript gibi başka şemalar maile konmaz."""
    url = kalem.kayit.url
    return {"no": kalem.no, "url": url if urlparse(url).scheme in ("http", "https") else None}


# Kayıtların iş kolları (tekrarsız, sıralı).
def _is_kollari(kayitlar: list[Kayit]) -> list[str]:
    return sorted({ik for k in kayitlar for ik in alicilar.kalem_is_kollari(k)})


# Mail konusunu üretir, tarih, kalem sayısı ve iş kolları yazar.
def rapor_konusu(kayitlar: list[Kayit], tarih: date) -> str:
    return f"Mevzuat Raporu — {tarih:%d.%m.%Y} — {len(kayitlar)} kalem ({', '.join(_is_kollari(kayitlar))})"


# Rapor mailini oluşturur. Kalemleri hazırlar, PDF'leri ekler, şablonları doldurur.
def rapor_olustur(
    kayitlar: list[Kayit],
    tarih: date,
    client: httpx.Client | None = None,
    ek_ekle: bool = True,
    sorumlu_notu: str | None = None,
    ek_onbellek: dict[str, bytes] | None = None,
    onay: dict | None = None,
    aciklamalar: dict[str, str] | None = None,
) -> Mail:
    """`onay` onaylayıcıya giden bildirim için verilir, içinde rapor_id ve link olur. Verilirse üste "onayınızı bekliyor" bandı eklenir.
    `aciklamalar` konu adından açıklamaya giden sözlüktür (konu_aciklamalari), "Neden size geldi" satırının altında gösterilir."""
    # Her kaydı numaralı kaleme çevir.
    kalemler = [_kalem(i, k) for i, k in enumerate(kayitlar, start=1)]
    # Mailde link olmaz kuralı açıklamaya da uygulanır.
    linksiz_aciklamalar = {ad: linksiz(a) for ad, a in (aciklamalar or {}).items()}
    for kalem in kalemler:
        kalem.nedenler = neden_onemli(kalem.kayit, linksiz_aciklamalar)
        kalem.kaynak_linki = _belge_linki(kalem)["url"]

    ekler: list[Ek] = []
    eklenemeyen: list[dict] = []  # [{no, url}], maile sığmayan/indirilemeyen belge, yerine resmî kaynağın linki
    toplam = 0
    # PDF ekleri, indir, 15 MB sınırını aşan ya da indirilemeyeni eklemeyip linkini listele.
    if ek_ekle and client is not None:
        for kalem in kalemler:
            try:
                ek = _ek_indir(client, kalem, ek_onbellek)
            except Exception:
                log.exception("Ek indirilemedi: %s", kalem.kayit.kaynakca)
                eklenemeyen.append(_belge_linki(kalem))
                continue
            if ek is None:
                continue
            if toplam + len(ek.icerik) > EK_TOPLAM_SINIR:
                eklenemeyen.append(_belge_linki(kalem))
                continue
            toplam += len(ek.icerik)
            ekler.append(ek)
            kalem.ek_adi = ek.dosya_adi

    # Kalemleri iş kollarına göre grupla (raporun başındaki "iş koluna göre" özeti).
    is_kollari: dict[str, list[RaporKalemi]] = {}
    for kalem in kalemler:
        for ik in alicilar.kalem_is_kollari(kalem.kayit):
            is_kollari.setdefault(ik, []).append(kalem)

    # Şablonlara gidecek bilgiler.
    konu = rapor_konusu(kayitlar, tarih)
    baglam = {
        "sorumlu_notu": sorumlu_notu,
        "konu": konu,
        "tarih": tarih,
        "kalemler": kalemler,
        "is_kollari": dict(sorted(is_kollari.items())),
        "eklenemeyen": eklenemeyen,
        "onay": onay,
    }
    # HTML ve düz metin şablonlarını doldurup maili döndür (alıcılar sonra eklenir).
    return Mail(
        konu=konu,
        html=_sablonlar.get_template("rapor.html.j2").render(**baglam),
        metin=_sablonlar.get_template("rapor.txt.j2").render(**baglam),
        alicilar=[],
        ekler=ekler,
    )


# "Rapor beklenen durumda değil" hatası (ör. başkası az önce karar verdi).
class DurumHatasi(Exception):
    """Rapor beklenen durumda değil (ör. başka biri az önce karar verdi)."""


# Onay bekleyen raporlar.
def onay_bekleyenler(session: Session) -> list[Rapor]:
    return list(session.scalars(select(Rapor).where(Rapor.durum == "ONAY_BEKLIYOR").order_by(Rapor.id)))


# Onaylanmış ama bütün mailleri henüz gitmemiş raporlar.
def dagitim_bekleyenler(session: Session) -> list[Rapor]:
    """Onaylanmış ama dağıtım maili gidememiş raporlar (bir sonraki çalışmada tekrar denenir)."""
    return list(session.scalars(select(Rapor).where(Rapor.durum == "ONAYLANDI").order_by(Rapor.id)))


# Bir rapora bağlı kayıtlar (rapordaki sırayla).
def rapor_kayitlari(session: Session, rapor: Rapor) -> list[Kayit]:
    return list(
        session.scalars(
            select(Kayit).where(Kayit.rapor_id == rapor.id).order_by(Kayit.yayin_tarihi, Kayit.kaynak, Kayit.id)
        )
    )


# Onay mailindeki "Onay paneline git" linki, panel adresinin sonuna rapor numarası eklenerek kurulur.
def panel_linki(panel_adresi: str | None, rapor_id: int) -> str | None:
    """Onay mailindeki tek link, panelin kendi adresi ve açılacak rapor. Link oturum ya da anahtar taşımaz,
    giriş ve iki adımlı doğrulama yine istenir. Adres tanımlı değilse link konmaz."""
    return f"{panel_adresi.rstrip('/')}/?rapor={rapor_id}" if panel_adresi else None


# Onaylayıcıya giden "onay bekliyor" maili, raporun dağıtılacak hali + üstte onay bandı ve panel düğmesi.
def onay_bildirimi(
    rapor: Rapor,
    kayitlar: list[Kayit],
    alicilar: list[str],
    panel_adresi: str | None = None,
    client: httpx.Client | None = None,
    ek_ekle: bool = True,
    aciklamalar: dict[str, str] | None = None,
) -> Mail:
    """Onaylayıcıya giden bildirim. İçerik raporun dağıtılacak hâliyle aynıdır, PDF belgeler de eklenir, böylece onaylayıcı ne
    onayladığını belgelerin kendisinden de görür. Üstte "onayınızı bekliyor" bandı ve panel düğmesi olur.
    Onay yine sadece panelden verilir."""
    mail = rapor_olustur(kayitlar, rapor.olusturuldu.date(), client, ek_ekle,
                         onay={"rapor_id": rapor.id, "link": panel_linki(panel_adresi, rapor.id)},
                         aciklamalar=aciklamalar)
    return replace(mail, konu=f"Onay bekliyor: {rapor.konu}", alicilar=alicilar)


# Raporlanmamış ilgili kayıtları yeni bir rapora bağlar ve onaylayıcılara bildirir.
def onaya_sun(
    session: Session,
    gonderici: MailGonderici,
    onaylayicilar: list[str],
    bugun: date | None = None,
    panel_adresi: str | None = None,
    client: httpx.Client | None = None,
    ek_ekle: bool = True,
) -> Rapor | None:
    """Raporlanmamış ilgili kayıtları yeni bir rapora bağlar ve sorumluya bildirir.
    Bekleyen kayıt yoksa hiçbir şey yapmaz, çünkü karar gereği mail sadece değişiklik olunca gider."""
    kayitlar = bekleyen_kayitlar(session)
    # Bekleyen kayıt yoksa rapor da yok, mail de yok.
    if not kayitlar:
        return None
    # Yeni raporu "ONAY_BEKLIYOR" durumunda oluştur.
    rapor = Rapor(
        olusturuldu=datetime.now(),
        durum="ONAY_BEKLIYOR",
        konu=rapor_konusu(kayitlar, bugun or date.today()),
        alicilar=[],
        kayit_sayisi=len(kayitlar),
    )
    session.add(rapor)
    session.flush()
    # Kayıtları bu rapora bağla.
    for kayit in kayitlar:
        kayit.rapor_id = rapor.id
    session.commit()  # bildirim gidemese bile rapor panelde görünür

    # Onaylayıcı yoksa hata yaz, varsa onay mailini gönder (gönderilemezse hatayı rapora yaz).
    if not onaylayicilar:
        rapor.hata = "Onaylayıcı kullanıcı yok; bildirim gönderilmedi."
    else:
        try:
            gonderici.gonder(onay_bildirimi(rapor, kayitlar, onaylayicilar, panel_adresi, client, ek_ekle,
                                            konu_aciklamalari(session)))
        except Exception as e:
            log.exception("Onay bildirimi gönderilemedi")
            rapor.hata = f"Onay bildirimi gönderilemedi: {e!r}"
    session.commit()
    return rapor


# Onay anında dağıtım planını (kim hangi kalemleri alacak) gonderimler tablosuna yazar.
def _plani_kaydet(
    session: Session, rapor: Rapor, kayitlar: list[Kayit], gruplar: list, ek_adresler: list[str], notu: str | None
) -> list[Gonderim]:
    """Dağıtım planını (kim hangi kalemleri alacak) onay anında dondurur. Sonradan grup değişse de
    onaylanmış rapor, onaylayıcının panelde gördüğü ve seçtiği gibi gider. Ek gönderim de aynı yoldan geçer,
    raporun alıcı listesi büyür. Commit etmez."""
    plan = alicilar.dagitim_plani(gruplar, kayitlar, ek_adresler)
    yeni = [
        Gonderim(rapor_id=rapor.id, alicilar=m.alicilar, kayit_idler=m.kayit_idler, gruplar=m.gruplar, durum="BEKLIYOR",
                 notu=notu)
        for m in plan
    ]
    session.add_all(yeni)
    rapor.alicilar = sorted(set(rapor.alicilar or []) | {a for m in plan for a in m.alicilar})
    return yeni


# Seçilen grupları ve adresleri doğrular, seçilen kalemler kimseye gitmiyorsa hata verir.
def _alicilari_dogrula(
    session: Session, dahil: list[Kayit], grup_idler: set[int] | None, ek_adresler: list[str]
) -> tuple[list, list[str]]:
    gruplar = alicilar.aktif_gruplar(session) if grup_idler is None else alicilar.secilen_gruplar(session, grup_idler)
    ek, hatali = alicilar.adresleri_ayikla("\n".join(ek_adresler))
    if hatali:
        raise ValueError(f"Geçersiz e-posta adresi: {', '.join(hatali)}")
    if not alicilar.dagitim_plani(gruplar, dahil, ek):
        raise ValueError("Seçilen kalemler hiçbir alıcı grubuna gitmiyor ve kişiye özel adres eklenmedi. "
                         "Grup seçin ya da adres ekleyin.")
    return gruplar, ek


# Panelden gelen onay ya da ret kararını işler.
def karar_ver(
    session: Session,
    rapor: Rapor,
    kullanici_id: int,
    onay: bool,
    dahil_idler: set[int],
    notu: str | None,
    grup_idler: set[int] | None = None,
    ek_adresler: list[str] = (),
    *, dagitim_gruplari: list | None = None, commit: bool = True,
) -> None:
    """Onay ya da ret. Onayda alıcılar onaylayıcının işaretlediği gruplardır (`grup_idler`). None verilirse iş koluna
    uyan bütün aktif gruplar alınır. Bunlara kişiye özel `ek_adresler` eklenir, onlar seçili bütün kalemleri alır."""
    # Not boşsa None.
    notu = (notu or "").strip() or None
    kayitlar = rapor_kayitlari(session, rapor)
    # Onaylayıcının işaretlediği (dağıtıma dahil) kalemler.
    dahil = [k for k in kayitlar if k.id in dahil_idler]
    # Reddederken not zorunlu.
    if not onay and not notu:
        raise ValueError("Reddederken sebep yazmak zorunludur.")
    # Onaylarken, en az bir kalem, geçerli gruplar/adresler ve en az bir alıcı olmalı.
    if onay:
        if not dahil:
            raise ValueError("En az bir kalem seçilmeli. Hiçbiri gönderilmeyecekse raporu reddedin.")
        if dagitim_gruplari is None:
            gruplar, ek = _alicilari_dogrula(session, dahil, grup_idler, ek_adresler)
        else:
            gruplar, ek = dagitim_gruplari, []
            if not alicilar.dagitim_plani(gruplar, dahil):
                raise ValueError("Seçilen kalemlere uygun, mail adresi olan portal personeli bulunamadı.")

    # Koşullu güncelleme, iki kişi aynı anda karar verirse sadece biri başarılı olur,
    # rapor iki kez dağıtılmaz.
    sonuc = session.execute(
        update(Rapor)
        .where(Rapor.id == rapor.id, Rapor.durum == "ONAY_BEKLIYOR")
        .values(
            durum="ONAYLANDI" if onay else "REDDEDILDI",
            karar_veren_id=kullanici_id,
            karar_zamani=datetime.now(),
            karar_notu=notu,
        )
    )
    # Güncellenen satır 1 değilse rapor zaten karara bağlanmış, dur.
    if sonuc.rowcount != 1:
        session.rollback()
        raise DurumHatasi("Bu rapor için zaten karar verilmiş.")
    # Onaylandıysa, seçilmeyen kalemleri "hariç" işaretle, konuyu güncelle, dağıtım planını kaydet.
    if onay:
        for kayit in kayitlar:
            kayit.haric = kayit.id not in dahil_idler
        rapor.konu = rapor_konusu(dahil, rapor.olusturuldu.date())
        _plani_kaydet(session, rapor, dahil, gruplar, ek, notu)  # onayla aynı commit'te, onaylı ama plansız rapor kalmaz
    if commit:
        session.commit()
        session.refresh(rapor)


# Onaylanmış rapordan başka kalemleri başka kişilere gönderir, ilk onayda seçilmeyen kalemler de seçilebilir.
def ek_gonderim_planla(
    session: Session,
    rapor: Rapor,
    dahil_idler: set[int],
    notu: str | None,
    grup_idler: set[int],
    ek_adresler: list[str] = (),
) -> None:
    """Onaylanmış ya da gönderilmiş rapora yeni dağıtım mailleri ekler, mailleri sonra `dagit` gönderir.
    Seçilen kalemler artık hariç sayılmaz. Gönderilmiş rapor tekrar ONAYLANDI olur, mailler gidince yine GONDERILDI olur."""
    notu = (notu or "").strip() or None
    kayitlar = rapor_kayitlari(session, rapor)
    dahil = [k for k in kayitlar if k.id in dahil_idler]
    if not dahil:
        raise ValueError("En az bir kalem seçilmeli.")
    gruplar, ek = _alicilari_dogrula(session, dahil, grup_idler, ek_adresler)
    # Koşullu güncelleme, rapor bu arada başka bir duruma geçtiyse ek gönderim yapılmaz.
    sonuc = session.execute(
        update(Rapor).where(Rapor.id == rapor.id, Rapor.durum.in_(("ONAYLANDI", "GONDERILDI"))).values(durum="ONAYLANDI")
    )
    if sonuc.rowcount != 1:
        session.rollback()
        raise DurumHatasi("Sadece onaylanmış raporlardan ek gönderim yapılabilir.")
    for kayit in dahil:
        kayit.haric = False
    rapor.konu = rapor_konusu([k for k in kayitlar if not k.haric], rapor.olusturuldu.date())
    _plani_kaydet(session, rapor, dahil, gruplar, ek, notu)
    session.commit()
    session.refresh(rapor)


# Raporun dağıtım mailleri.
def gonderimler(session: Session, rapor: Rapor) -> list[Gonderim]:
    return list(session.scalars(select(Gonderim).where(Gonderim.rapor_id == rapor.id).order_by(Gonderim.id)))


# Tek bir dağıtım mailini gönderir ("en fazla bir kez" kuralıyla).
def _gonderimi_yap(
    session: Session,
    client: httpx.Client | None,
    gonderici: MailGonderici,
    rapor: Rapor,
    gonderim: Gonderim,
    kayitlar: dict[int, Kayit],
    ek_ekle: bool,
    ek_onbellek: dict[str, bytes],
    mail_onbellek: dict[tuple, Mail],
) -> bool | None:
    """Tek bir dağıtım maili gönderir. Mail gittiyse True döner. Gitmediyse False döner, hata yazılır ve sonra tekrar denenir.
    Başka bir süreç bu maili sahiplendiyse None döner, maili o gönderecek."""
    # Mail sahiplenmeden ÖNCE hazırlanır, hazırlarken hata çıkarsa gönderim BEKLIYOR'da kalır.
    # Aynı kalemleri ve aynı notu alan kişilerin maili bir kez hazırlanır (kişi başı ayrı mail, içerik aynı).
    anahtar = (tuple(gonderim.kayit_idler), gonderim.notu)
    if anahtar not in mail_onbellek:
        try:
            secilen = [kayitlar[i] for i in gonderim.kayit_idler]
            mail_onbellek[anahtar] = rapor_olustur(secilen, rapor.olusturuldu.date(), client, ek_ekle,
                                                   sorumlu_notu=gonderim.notu, ek_onbellek=ek_onbellek,
                                                   aciklamalar=konu_aciklamalari(session))
        except Exception as e:
            log.exception("Dağıtım maili hazırlanamadı")
            gonderim.hata = f"Mail hazırlanamadı: {e!r}"
            session.commit()
            return False
    # Hazır maili bu kişinin adresiyle kopyala, sabit Message-ID ver.
    mail = replace(mail_onbellek[anahtar], alicilar=list(gonderim.alicilar))
    mail.message_id = f"<mevzuat-rapor-{rapor.id}-{gonderim.id}@mevzuat-takip>"

    # Sahiplenme, "durumu BEKLIYOR ise GONDERILIYOR yap". 1 satır güncellenmediyse başka süreç almış.
    sahiplenme = session.execute(
        update(Gonderim)
        .where(Gonderim.id == gonderim.id, Gonderim.durum == "BEKLIYOR")
        .values(durum="GONDERILIYOR", gonderim_denemesi=datetime.now())
    )
    if sahiplenme.rowcount != 1:
        session.rollback()
        return None
    session.commit()

    # Gönder. Hata olursa maili BEKLIYOR'a geri al (sonra tekrar denenir).
    try:
        gonderici.gonder(mail)
    except Exception as e:
        log.exception("Dağıtım maili gönderilemedi")
        session.refresh(gonderim)
        gonderim.durum = "BEKLIYOR"
        gonderim.hata = f"Gönderilemedi: {e!r}"
        session.commit()
        return False

    # Gitti, GONDERILDI olarak işaretle.
    session.refresh(gonderim)
    gonderim.durum = "GONDERILDI"
    gonderim.gonderildi = datetime.now()
    gonderim.hata = None
    session.commit()
    return True


# Raporun bütün mailleri gittiyse raporu GONDERILDI yapar.
def raporu_tamamla(session: Session, rapor: Rapor) -> bool:
    """Bütün mailleri gitmiş raporu GONDERILDI yapar. Rapor tamamen gönderildiyse True döner."""
    hepsi = gonderimler(session, rapor)
    if not hepsi or any(g.durum != "GONDERILDI" for g in hepsi):
        return False
    session.execute(
        update(Rapor)
        .where(Rapor.id == rapor.id, Rapor.durum == "ONAYLANDI")
        .values(durum="GONDERILDI", gonderildi=max(g.gonderildi for g in hepsi), hata=None)
    )
    session.commit()
    session.refresh(rapor)
    return rapor.durum == "GONDERILDI"


# Onaylanmış raporu dağıtır, bekleyen her maili tek tek gönderir.
def dagit(
    session: Session,
    client: httpx.Client | None,
    gonderici: MailGonderici,
    rapor: Rapor,
    ek_ekle: bool = True,
) -> bool:
    """Onaylanmış raporun bekleyen dağıtım maillerini gönderir. Her mail en fazla bir kez gider.
    Rapor tamamen gönderildiyse True döner.

    Mail ve veritabanı ayrı sistemler, ikisini birlikte "ya hep ya hiç" yapmak mümkün değil. Bu yüzden
    her mail (Gonderim) için şu adımlar izlenir.
      1. Koşullu güncellemeyle durum BEKLIYOR'dan GONDERILIYOR'a çevrilip commit edilir, buna sahiplenme denir.
         Aynı anda panel ve günlük iş göndermeye çalışırsa sadece biri başarır.
      2. Mail gönderilir.
         - SMTP hata verdiyse mail kesinlikle gitmemiştir. BEKLIYOR durumuna geri alınır, sonra tekrar denenir.
      3. GONDERILDI yazılır.
         - Mail gittikten sonra süreç çöker ya da DB'ye yazılamazsa gönderim GONDERILIYOR'da kalır ve
           kendiliğinden tekrar gönderilmez. Çift mail yerine yöneticiye "durumu belirsiz" uyarısı gider.
    Her kişiye ayrı mail gittiği için garanti kişi başınadır. Bir mailin hatası diğerlerini durdurmaz,
    biri SMTP hatası alsa da diğerleri raporunu alır, sonraki çalışmada sadece gitmeyen tekrar denenir.
    Eski sürümde planlanmış çok alıcılı gönderimler de aynı yoldan, eskisi gibi tek mailde gider.
    Message-ID her mail için sabittir, yine de iki kez giderse alıcı tarafında aynı mail olarak görünür.
    """
    # Sadece onaylanmış rapor dağıtılabilir.
    if rapor.durum != "ONAYLANDI":
        raise DurumHatasi(f"Rapor #{rapor.id} dağıtılamaz: durum {rapor.durum}")

    kayitlar = {k.id: k for k in rapor_kayitlari(session, rapor)}
    # Planı olmayan eski rapor, şimdiki gruplarla plan çıkar.
    if not gonderimler(session, rapor):
        # Alıcı grupları gelmeden (eski sürümde) onaylanmış rapor, şimdiki gruplarla planla.
        _plani_kaydet(session, rapor, [k for k in kayitlar.values() if not k.haric], alicilar.aktif_gruplar(session), [],
                      rapor.karar_notu)
        session.commit()
        if not gonderimler(session, rapor):
            rapor.hata = "Hiçbir alıcı grubu bu raporun kalemlerini almıyor; panelden alıcı grubu tanımlayın."
            session.commit()
            return False

    # Aynı PDF'ler ve aynı içerikli mailler bir kez hazırlansın diye önbellekler.
    ek_onbellek: dict[str, bytes] = {}
    mail_onbellek: dict[tuple, Mail] = {}
    hatalar = []
    # BEKLIYOR durumundaki her maili gönder, gidemeyenleri hata listesine yaz.
    for gonderim in [g for g in gonderimler(session, rapor) if g.durum == "BEKLIYOR"]:
        if _gonderimi_yap(session, client, gonderici, rapor, gonderim, kayitlar, ek_ekle, ek_onbellek,
                          mail_onbellek) is False:
            hatalar.append(f"{', '.join(gonderim.alicilar)}: {gonderim.hata}")

    # Hepsi gittiyse tamam, değilse hataları rapora yaz.
    if raporu_tamamla(session, rapor):
        return True
    rapor.hata = ("Dağıtım maili gönderilemedi — " + " | ".join(hatalar)) if hatalar else None
    session.commit()
    return False


# 30 dakikadan uzun süredir GONDERILIYOR'da kalan mailler (gitti mi bilinmiyor, yöneticiye uyarı gider).
def belirsiz_gonderimler(session: Session, esik: timedelta = timedelta(minutes=30)) -> list[Gonderim]:
    """GONDERILIYOR durumunda takılı kalmış mailler. Gidip gitmedikleri bilinmiyor, bir insan karar vermeli."""
    sinir = datetime.now() - esik
    return list(session.scalars(
        select(Gonderim)
        .where(Gonderim.durum == "GONDERILIYOR", Gonderim.gonderim_denemesi < sinir)
        .order_by(Gonderim.id)
    ))
