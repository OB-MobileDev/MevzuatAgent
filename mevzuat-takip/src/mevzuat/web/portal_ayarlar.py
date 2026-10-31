"""Portal yetkileriyle yönetilen Mevzuat işletim ayarları."""
from datetime import datetime
from typing import Literal
from fastapi import HTTPException, Request
from pydantic import Field, StrictBool, StrictInt, StrictStr
from mevzuat import alicilar, panel_ayarlari
from mevzuat.mail import Mail
from mevzuat.web.guvenlik import denetle
from mevzuat.web.portal_entegrasyon import PortalDetayIstegi

# Kullanıcı yaşam döngüsü ve MFA portalda; servis imzası buradan kapatılamaz.
ALANLAR = frozenset(panel_ayarlari.ALAN) - {"mfa_zorunlu", "pasif_silme_gun", "api_imza"}

class AyarIstegi(PortalDetayIstegi):
    islem: Literal["liste", "kaydet", "mail_dene"]
    degisiklikler: dict[str, StrictStr | StrictInt | StrictBool | list[StrictStr] | None] = Field(default_factory=dict, max_length=12)
    surum: int | None = Field(None, ge=0)
    adres: str | None = Field(None, max_length=200)


def ekle(router, oturum, giris_gerekli, portal_gerekli, gizli_anahtar):
    def gorunum(db):
        result = panel_ayarlari.panel_gorunumu(db, gizli_anahtar)
        result["alanlar"] = [a for a in result["alanlar"] if a["ad"] in ALANLAR]
        return result

    @router.post("/portal/ayarlar", tags=["Portal"], summary="Portal işletim ayarları")
    def ayarlar(request: Request, istek: AyarIstegi):
        portal_gerekli(request)
        with oturum() as db:
            hesap = giris_gerekli(request, db)
            if istek.islem == "liste":
                return gorunum(db)
            if istek.islem == "kaydet":
                if istek.surum is None:
                    raise HTTPException(422, "Ayar sürümü zorunludur.")
                if set(istek.degisiklikler) - ALANLAR:
                    raise HTTPException(422, "Bu ayar portal üzerinden değiştirilemez.")
                try:
                    degisen = panel_ayarlari.kaydet(db, istek.degisiklikler, istek.surum, hesap.id, gizli_anahtar)
                except ValueError as e:
                    raise HTTPException(400, str(e)) from None
                except panel_ayarlari.CakismaHatasi as e:
                    raise HTTPException(409, str(e)) from None
                if degisen:
                    # Değerleri, özellikle SMTP şifresini, denetime yazma.
                    denetle(db, "portal_ayarlar_kaydet", hesap.id, request.client.host if request.client else None,
                            degisen=degisen, portal_aktor=istek.aktor.model_dump())
                db.commit()
                return {**gorunum(db), "mesaj": f"{len(degisen)} ayar kaydedildi." if degisen else "Değişiklik yok."}
            addresses, invalid = alicilar.adresleri_ayikla(istek.adres or "")
            if invalid or len(addresses) != 1:
                raise HTTPException(422, "Tek bir geçerli e-posta adresi yazın.")
            ayar = panel_ayarlari.etkin(db, gizli_anahtar)
            if not ayar["smtp_host"]:
                return {"tur": "hata", "mesaj": "SMTP sunucusu tanımlı değil, mail gönderilmez."}
            simdi = datetime.now().strftime("%d.%m.%Y %H:%M")
            text = f"Bu bir Mevzuat deneme mailidir ({simdi}). Mail ayarları çalışıyor."
            try:
                panel_ayarlari.gonderici(ayar).gonder(Mail(konu=f"Mevzuat Takip — deneme maili ({simdi})",
                                                        html=f"<p>{text}</p>", metin=text, alicilar=addresses))
                result = {"tur": "basari", "mesaj": f"Deneme maili {addresses[0]} adresine gönderildi."}
            except Exception:
                result = {"tur": "hata", "mesaj": "Deneme maili gönderilemedi. SMTP ayarlarını ve sunucu bağlantısını kontrol edin."}
            denetle(db, "portal_ayarlar_mail_dene", hesap.id, request.client.host if request.client else None,
                    adres=addresses[0], sonuc=result["tur"], portal_aktor=istek.aktor.model_dump())
            db.commit()
            return result
