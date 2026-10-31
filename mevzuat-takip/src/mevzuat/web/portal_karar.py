"""Portal personel dizininden dondurulmuş dağıtım, önizleme ve karar."""
import hashlib
import json
from types import SimpleNamespace
from typing import Literal
from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from mevzuat import alicilar, rapor as rapor_modulu
from mevzuat.db import Rapor
from mevzuat.web.guvenlik import denetle
from mevzuat.web.portal_entegrasyon import PortalDetayIstegi

class Grup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int = Field(gt=0)
    ad: str = Field(min_length=1, max_length=200)
    is_kollari: list[str] = Field(max_length=50)
    adresler: list[str] = Field(max_length=1000)

class Karar(PortalDetayIstegi):
    islem: Literal["onizleme", "onayla", "reddet"]
    dahil: list[int] = Field(default_factory=list, max_length=5000)
    gruplar: list[Grup] = Field(default_factory=list, max_length=100)
    notu: str = Field("", max_length=2000)
    plan_izi: str | None = Field(None, max_length=64)

def ekle(router, oturum, giris_gerekli, portal_gerekli, ayar, make_client):
    @router.post("/portal/raporlar/{rapor_id}/karar", tags=["Portal"])
    def karar(request: Request, rapor_id: int, istek: Karar):
        portal_gerekli(request)
        with oturum() as db:
            hesap = giris_gerekli(request, db)
            rapor = db.get(Rapor, rapor_id)
            if rapor is None: raise HTTPException(404, "Rapor bulunamadı.")
            if rapor.durum != "ONAY_BEKLIYOR": raise HTTPException(409, "Bu rapor için zaten karar verilmiş. Listeyi yenileyin.")
            records = rapor_modulu.rapor_kayitlari(db, rapor)
            ids = {k.id for k in records}
            if set(istek.dahil) - ids: raise HTTPException(422, "Seçilen kalem rapora ait değil.")
            selected = [k for k in records if k.id in istek.dahil]
            groups = []
            for group in istek.gruplar:
                addresses, invalid = alicilar.adresleri_ayikla("\n".join(group.adresler))
                if invalid: raise HTTPException(422, "Portal personel mail adreslerinden biri geçersiz.")
                groups.append(SimpleNamespace(ad=group.ad, is_kollari=group.is_kollari, adresler=addresses))
            plan = alicilar.dagitim_plani(groups, selected)
            if istek.islem != "reddet":
                if not selected: raise HTTPException(422, "En az bir kalem seçin.")
                if not plan: raise HTTPException(422, "Seçilen kalemlere uygun alıcı bulunamadı. İş kollarını ve personel mail adreslerini kontrol edin.")
                covered = {i for mail in plan for i in mail.kayit_idler}
                if set(istek.dahil) - covered: raise HTTPException(422, "Seçilen bazı kalemlerin alıcısı yok. Uygun grup seçin veya bu kalemleri çıkarın.")
            payload = {"rapor": rapor_id, "dahil": sorted(istek.dahil), "notu": istek.notu.strip(),
                       "gruplar": sorted([g.model_dump() for g in istek.gruplar], key=lambda g: g["id"]),
                       "kalemler": [{"id": k.id, "is_kollari": k.is_kollari} for k in selected]}
            fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if istek.islem == "onizleme":
                return {"plan_izi": fingerprint, "kisi_sayisi": len(plan),
                        "plan": [{"adres": m.alicilar[0], "kalemler": m.kayit_idler, "gruplar": m.gruplar} for m in plan]}
            if istek.islem == "onayla" and istek.plan_izi != fingerprint:
                raise HTTPException(409, "Alıcılar veya seçimler değişti. Dağıtım önizlemesini yeniden oluşturun.")
            settings = ayar(db)
            if istek.islem == "onayla" and settings.mail_kapali:
                raise HTTPException(422, "SMTP sunucusu tanımlı değil. Onay ve dağıtım için önce mail ayarlarını tamamlayın.")
            try:
                rapor_modulu.karar_ver(db, rapor, hesap.id, istek.islem == "onayla", set(istek.dahil), istek.notu,
                                      dagitim_gruplari=groups, commit=False)
            except ValueError as e: raise HTTPException(422, str(e)) from None
            except rapor_modulu.DurumHatasi as e: raise HTTPException(409, str(e)) from None
            denetle(db, "portal_rapor_" + istek.islem, hesap.id, request.client.host if request.client else None,
                    rapor_id=rapor_id, dahil=sorted(istek.dahil), grup_idler=[g.id for g in istek.gruplar],
                    portal_aktor=istek.aktor.model_dump())
            db.commit()
            db.refresh(rapor)
            if istek.islem == "reddet": return {"tur": "basari", "mesaj": "Rapor reddedildi; dağıtım yapılmayacak."}
            try:
                with make_client() as client:
                    complete = rapor_modulu.dagit(db, client, settings.gonderici, rapor, settings.ek_ekle)
            except Exception:
                # Decision and frozen delivery plan are already committed. Never resubmit approval.
                return {"tur": "hata", "mesaj": "Rapor onaylandı. Dağıtım tamamlanamadı; gönderim durumunu kontrol edin. Onayı tekrar göndermeyin."}
            return {"tur": "basari" if complete else "hata", "mesaj":
                    f"Rapor onaylandı ve {len(plan)} personel adresine gönderildi." if complete else
                    "Rapor onaylandı. Dağıtım henüz tamamlanmadı; bekleyen gönderimler servis tarafından tekrar denenecek."}
