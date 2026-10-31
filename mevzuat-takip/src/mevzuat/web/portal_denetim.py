"""Portal için salt okunur, sayfalı denetim listesi."""
from datetime import date, datetime, time, timedelta
import re
from typing import Literal
from fastapi import HTTPException, Request
from pydantic import Field
from sqlalchemy import select, func, or_
from mevzuat.db import Denetim, Kullanici
from mevzuat.web.portal_entegrasyon import PortalDetayIstegi


class DenetimIstegi(PortalDetayIstegi):
    sayfa: int = Field(1, ge=1)
    adet: int = Field(20, ge=1, le=100)
    sinir_id: int | None = Field(None, ge=0)
    q: str = Field("", max_length=200)
    islem: str = Field("", max_length=100)
    baslangic: date | None = None
    bitis: date | None = None
    sirala: Literal["id", "zaman", "kullanici", "islem", "ip"] = "id"
    yon: Literal["asc", "desc"] = "desc"


def gizle(value):
    if isinstance(value, dict):
        return {k: "[Gizlendi]" if re.search(r"password|parola|sifre|secret|token", k, re.I) else gizle(v) for k, v in value.items()}
    if isinstance(value, list):
        return [gizle(v) for v in value]
    return value


def ekle(router, oturum, giris_gerekli, portal_gerekli):
    @router.post("/portal/denetim", tags=["Portal"], summary="Portal denetim listesi")
    def denetim(request: Request, istek: DenetimIstegi):
        portal_gerekli(request)
        if istek.baslangic and istek.bitis and istek.bitis < istek.baslangic:
            raise HTTPException(422, "Bitiş tarihi başlangıçtan önce olamaz.")
        with oturum() as db:
            giris_gerekli(request, db)
            limit = istek.sinir_id if istek.sinir_id is not None else (db.scalar(select(func.max(Denetim.id))) or 0)
            actor = func.coalesce(Denetim.detay["portal_aktor"]["ad"].as_string(), Kullanici.ad, "Sistem")
            query = select(Denetim, actor.label("actor_name")).outerjoin(Kullanici, Denetim.kullanici_id == Kullanici.id).where(Denetim.id <= limit)
            if istek.q:
                term = "%" + istek.q.strip() + "%"
                query = query.where(or_(actor.ilike(term), Denetim.islem.ilike(term), Denetim.ip.ilike(term)))
            if istek.islem:
                query = query.where(Denetim.islem == istek.islem)
            if istek.baslangic:
                query = query.where(Denetim.zaman >= datetime.combine(istek.baslangic, time.min))
            if istek.bitis:
                query = query.where(Denetim.zaman < datetime.combine(istek.bitis + timedelta(days=1), time.min))
            total = db.scalar(select(func.count()).select_from(query.subquery()))
            sort = {"id": Denetim.id, "zaman": Denetim.zaman, "kullanici": actor, "islem": Denetim.islem, "ip": Denetim.ip}[istek.sirala]
            query = query.order_by(sort.asc() if istek.yon == "asc" else sort.desc(), Denetim.id.desc())
            rows = [{"id": row.id, "zaman": row.zaman.isoformat(), "kullanici": name,
                     "islem": row.islem, "ip": row.ip, "detay": gizle(row.detay)}
                    for row, name in db.execute(query.offset((istek.sayfa - 1) * istek.adet).limit(istek.adet))]
            operations = list(db.scalars(select(Denetim.islem).where(Denetim.id <= limit).distinct().order_by(Denetim.islem)))
            return {"kayitlar": rows, "toplam": total, "sayfa": istek.sayfa, "adet": istek.adet,
                    "sinir_id": limit, "islemler": operations}
