"""Portal backend'ine özel uçlar. Kimlik yalnız HMAC ile imzalı gövdeden alınır."""
import os
import secrets
from collections.abc import Callable

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from mevzuat.web.guvenlik import denetle


class PortalAktor(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(min_length=1, max_length=64, pattern=r"^[1-9][0-9]*$")
    ad: str = Field(min_length=1, max_length=200)


class PortalDetayIstegi(BaseModel):
    model_config = ConfigDict(extra="forbid")
    aktor: PortalAktor


def ekle(router: APIRouter, oturum: Callable, giris_gerekli: Callable, rapor_detay: Callable) -> None:
    @router.post("/portal/raporlar/{rapor_id}/detay", tags=["Portal"],
                 summary="Portal kullanıcısı için tam rapor ayrıntısı")
    def detay(request: Request, rapor_id: int, istek: PortalDetayIstegi):
        # Çerez, bearer ya da başka entegrasyon anahtarı portal adına kişi bildiremez.
        izinli = os.environ.get("MEVZUAT_PORTAL_ANAHTAR_ON_EKI", "").strip()
        gelen = request.headers.get("x-anahtar", "")
        if (not izinli or len(izinli) != 10 or not secrets.compare_digest(izinli, gelen)
                or getattr(request.state, "imzali_hesap", None) is None):
            raise HTTPException(403, "Bu adres yalnız yetkilendirilmiş portal servisine açıktır.")
        sonuc = rapor_detay(request, rapor_id)
        with oturum() as db:
            hesap = giris_gerekli(request, db)
            denetle(db, "portal_rapor_goruntule", hesap.id,
                    request.client.host if request.client else None,
                    rapor_id=rapor_id, portal_aktor=istek.aktor.model_dump())
            db.commit()
        return sonuc
