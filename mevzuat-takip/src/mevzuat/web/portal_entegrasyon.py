"""Portal backend'ine özel uçlar. Kimlik yalnız HMAC ile imzalı gövdeden alınır."""
import os
import secrets
from collections.abc import Callable
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from sqlalchemy import select
from mevzuat.db import Denetim
from mevzuat.web.guvenlik import denetle


class PortalAktor(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(min_length=1, max_length=64, pattern=r"^[1-9][0-9]*$")
    ad: str = Field(min_length=1, max_length=200)


class PortalDetayIstegi(BaseModel):
    model_config = ConfigDict(extra="forbid")
    aktor: PortalAktor


class PortalKaynakIstegi(PortalDetayIstegi):
    islem: Literal["liste", "tipler", "ekle", "duzenle", "dene", "bul", "kaldir", "geri_getir"]
    ad: str | None = Field(None, max_length=200, pattern=r"^[a-z0-9_-]+$")
    veri: dict = Field(default_factory=dict)


class PortalKonuIstegi(PortalDetayIstegi):
    islem: Literal["liste", "onizleme", "ekle", "duzenle", "aktif", "pasif"]
    konu_id: int | None = Field(None, gt=0)
    veri: dict = Field(default_factory=dict)


class PortalTaramaIstegi(PortalDetayIstegi):
    islem: Literal["durum", "baslat", "saatler"]
    veri: dict = Field(default_factory=dict)


def ekle(router: APIRouter, oturum: Callable, giris_gerekli: Callable, rapor_detay: Callable,
         kaynak_islemleri: dict[str, Callable] | None = None,
         konu_islemleri: dict[str, Callable] | None = None,
         tarama_islemleri: dict[str, Callable] | None = None,
         gizli_anahtar: str | None = None,
         rapor_isletim: tuple | None = None) -> None:
    def portal_gerekli(request: Request):
        izinli = os.environ.get("MEVZUAT_PORTAL_ANAHTAR_ON_EKI", "").strip()
        gelen = request.headers.get("x-anahtar", "")
        if (not izinli or len(izinli) != 10 or not secrets.compare_digest(izinli, gelen)
                or getattr(request.state, "imzali_hesap", None) is None):
            raise HTTPException(403, "Bu adres yalnız yetkilendirilmiş portal servisine açıktır.")

    @router.post("/portal/raporlar/{rapor_id}/detay", tags=["Portal"],
                 summary="Portal kullanıcısı için tam rapor ayrıntısı")
    def detay(request: Request, rapor_id: int, istek: PortalDetayIstegi):
        # Çerez, bearer ya da başka entegrasyon anahtarı portal adına kişi bildiremez.
        portal_gerekli(request)
        sonuc = rapor_detay(request, rapor_id)
        with oturum() as db:
            karar = db.scalar(select(Denetim).where(Denetim.islem.in_(["portal_rapor_onayla", "portal_rapor_reddet"]),
                Denetim.detay["rapor_id"].as_integer() == rapor_id).order_by(Denetim.id.desc()).limit(1))
            if karar:
                sonuc["rapor"]["karar_veren"] = karar.detay["portal_aktor"]["ad"]
            hesap = giris_gerekli(request, db)
            denetle(db, "portal_rapor_goruntule", hesap.id,
                    request.client.host if request.client else None,
                    rapor_id=rapor_id, portal_aktor=istek.aktor.model_dump())
            db.commit()
        return sonuc

    @router.post("/portal/kaynaklar", tags=["Portal"], summary="Portal kaynak yönetimi")
    def kaynak(request: Request, istek: PortalKaynakIstegi):
        portal_gerekli(request)
        if not kaynak_islemleri or istek.islem not in kaynak_islemleri:
            raise HTTPException(404, "Kaynak işlemi bulunamadı.")
        if istek.islem in ("duzenle", "kaldir", "geri_getir") and not istek.ad:
            raise HTTPException(422, "Kaynak kod adı zorunludur.")
        try:
            sonuc = kaynak_islemleri[istek.islem](request, istek.ad, istek.veri)
        except ValidationError as e:
            raise HTTPException(422, "Kaynak alanları geçersiz: " + "; ".join(x["msg"] for x in e.errors())) from None
        if istek.islem not in ("liste", "tipler"):
            with oturum() as db:
                hesap = giris_gerekli(request, db)
                denetle(db, "portal_kaynak_" + istek.islem, hesap.id,
                        request.client.host if request.client else None,
                        kaynak=istek.ad or (sonuc.get("ad") if isinstance(sonuc, dict) else None),
                        portal_aktor=istek.aktor.model_dump())
                db.commit()
        return sonuc

    @router.post("/portal/konular", tags=["Portal"], summary="Portal konu yönetimi")
    def konu(request: Request, istek: PortalKonuIstegi):
        portal_gerekli(request)
        if not konu_islemleri or istek.islem not in konu_islemleri:
            raise HTTPException(404, "Konu işlemi bulunamadı.")
        if istek.islem in ("duzenle", "aktif", "pasif") and istek.konu_id is None:
            raise HTTPException(422, "Konu numarası zorunludur.")
        try:
            sonuc = konu_islemleri[istek.islem](request, istek.konu_id, istek.veri)
        except ValidationError as e:
            raise HTTPException(422, "Konu alanları geçersiz: " + "; ".join(x["msg"] for x in e.errors())) from None
        if istek.islem != "liste":
            with oturum() as db:
                hesap = giris_gerekli(request, db)
                denetle(db, "portal_konu_" + istek.islem, hesap.id,
                        request.client.host if request.client else None,
                        konu_id=istek.konu_id or (sonuc.get("id") if isinstance(sonuc, dict) else None),
                        portal_aktor=istek.aktor.model_dump())
                db.commit()
        return sonuc

    @router.post("/portal/tarama", tags=["Portal"], summary="Portal tarama ve zamanlama")
    def tarama(request: Request, istek: PortalTaramaIstegi):
        portal_gerekli(request)
        if not tarama_islemleri or istek.islem not in tarama_islemleri:
            raise HTTPException(404, "Tarama işlemi bulunamadı.")
        try:
            sonuc = tarama_islemleri[istek.islem](request, istek.veri)
        except ValidationError as e:
            raise HTTPException(422, "Tarama alanları geçersiz: " + "; ".join(x["msg"] for x in e.errors())) from None
        tarama_istegi = sonuc.get("istek")
        with oturum() as db:
            if istek.islem != "durum":
                hesap = giris_gerekli(request, db)
                denetle(db, "portal_tarama_" + istek.islem, hesap.id,
                        request.client.host if request.client else None,
                        istek_id=tarama_istegi["id"] if tarama_istegi else None,
                        portal_aktor=istek.aktor.model_dump())
                db.commit()
            if tarama_istegi:
                # İstek FK'si servis hesabını korur; portalda gerçek talep sahibini göster.
                kayit = db.scalar(select(Denetim).where(
                    Denetim.islem == "portal_tarama_baslat",
                    Denetim.detay["istek_id"].as_integer() == tarama_istegi["id"],
                ).order_by(Denetim.id.desc()).limit(1))
                if kayit:
                    tarama_istegi["isteyen"] = kayit.detay["portal_aktor"]["ad"]
        return sonuc

    if rapor_isletim:
        from mevzuat.web import portal_karar
        portal_karar.ekle(router, oturum, giris_gerekli, portal_gerekli, *rapor_isletim)
    from mevzuat.web import portal_denetim
    portal_denetim.ekle(router, oturum, giris_gerekli, portal_gerekli)
    if gizli_anahtar:
        from mevzuat.web import portal_ayarlar
        portal_ayarlar.ekle(router, oturum, giris_gerekli, portal_gerekli, gizli_anahtar)
