from datetime import datetime, date
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_portal_entegrasyon import portal_ortam, gonder, GIZLI
from mevzuat.web import uygulama_olustur
from mevzuat.db import Rapor, Kayit, Calisma, Gonderim, Denetim, AliciGrubu

YOL = "/api/portal/raporlar/1/karar"
@pytest.fixture
def ortam(portal_ortam):
    _, key, engine = portal_ortam
    class Sender:
        def __init__(self): self.sent = []; self.fail = False
        def gonder(self, mail):
            if self.fail: raise RuntimeError("SMTP unavailable")
            self.sent.append(mail)
    sender = Sender()
    with Session(engine) as db:
        db.add(Calisma(id=1, baslangic=datetime.now(), durum="BASARILI"))
        db.add(AliciGrubu(ad="Standalone", is_kollari=["Kuyum"], adresler=["wrong@example.com"], aktif=True, guncellendi=datetime.now()))
        for i, branches in [(1, ["Kuyum"]), (2, ["Ortak"])]:
            db.add(Kayit(id=i, kaynak="test", dis_id=str(i), yayin_tarihi=date.today(), baslik=f"Test {i}",
                         url="https://example.com/item", kaynakca="Test", ilgili=True, is_kollari=branches,
                         icerik="Test metni", icerik_durumu="TAMAM", ilk_gorulme=datetime.now(), calisma_id=1, rapor_id=1))
        db.commit()
    client = TestClient(uygulama_olustur(engine, sender, [], GIZLI, arayuz=None, api_imza=True, ek_ekle=False))
    return client, key, engine, sender

def request(env, operation, **changes):
    client,key,_,_ = env
    body = {"aktor": {"id": "42", "ad": "Portal Onaycı"}, "islem": operation, "dahil": [1],
            "gruplar": [{"id": 50, "ad": "Portal Grubu", "is_kollari": ["Kuyum"], "adresler": ["person@example.com"]}], "notu": "Kontrol edildi"}
    body.update(changes)
    return gonder(client,key,body,yol=YOL)

def test_preview_does_not_write_and_approval_freezes_portal_recipients_once(ortam):
    client,key,engine,sender = ortam
    preview = request(ortam, "onizleme")
    assert preview.status_code == 200, preview.text
    assert preview.json()["kisi_sayisi"] == 1 and not sender.sent
    with Session(engine) as db:
        assert db.get(Rapor,1).durum == "ONAY_BEKLIYOR" and not list(db.scalars(select(Gonderim)))
    response = request(ortam, "onayla", plan_izi=preview.json()["plan_izi"])
    assert response.status_code == 200, response.text
    assert response.json()["tur"] == "basari" and len(sender.sent) == 1
    assert sender.sent[0].alicilar == ["person@example.com"]
    with Session(engine) as db:
        assert db.get(Rapor,1).durum == "GONDERILDI" and db.get(Kayit,2).haric
        log = db.scalar(select(Denetim).where(Denetim.islem == "portal_rapor_onayla"))
        assert log.detay["portal_aktor"]["id"] == "42"
        assert db.scalar(select(Gonderim)).gruplar == ["Portal Grubu"]
    assert request(ortam,"onayla",plan_izi=preview.json()["plan_izi"]).status_code == 409
    assert len(sender.sent) == 1
    detail = gonder(client,key,{"aktor":{"id":"42","ad":"Portal Onaycı"}},yol="/api/portal/raporlar/1/detay")
    assert detail.json()["rapor"]["karar_veren"] == "Portal Onaycı"

def test_changed_recipients_or_missing_preview_are_rejected(ortam):
    preview = request(ortam,"onizleme").json()
    assert request(ortam,"onayla").status_code == 409
    changed = [{"id":50,"ad":"Portal Grubu","is_kollari":["Kuyum"],"adresler":["changed@example.com"]}]
    assert request(ortam,"onayla",gruplar=changed,plan_izi=preview["plan_izi"]).status_code == 409
    assert not ortam[3].sent

def test_rejection_requires_reason_and_never_sends(ortam):
    assert request(ortam,"reddet",notu=" ",dahil=[],gruplar=[]).status_code == 422
    response = request(ortam,"reddet",notu="İlgili değil",dahil=[],gruplar=[])
    assert response.status_code == 200 and not ortam[3].sent
    with Session(ortam[2]) as db:
        assert db.get(Rapor,1).durum == "REDDEDILDI" and not list(db.scalars(select(Gonderim)))

def test_invalid_items_uncovered_items_and_wrong_service_are_rejected(ortam,monkeypatch):
    assert request(ortam,"onizleme",dahil=[999]).status_code == 422
    assert request(ortam,"onizleme",dahil=[1,2]).status_code == 422
    assert request(ortam,"onizleme",gruplar=[]).status_code == 422
    monkeypatch.delenv("MEVZUAT_PORTAL_ANAHTAR_ON_EKI")
    assert request(ortam,"reddet").status_code == 403

def test_duplicate_members_get_one_mail_and_failures_keep_frozen_plan(ortam):
    groups = [{"id":1,"ad":"Bir","is_kollari":["Kuyum"],"adresler":["person@example.com"]},
              {"id":2,"ad":"İki","is_kollari":["Kuyum"],"adresler":["person@example.com"]}]
    preview = request(ortam,"onizleme",gruplar=groups).json()
    assert preview["kisi_sayisi"] == 1
    ortam[3].fail = True
    response = request(ortam,"onayla",gruplar=groups,plan_izi=preview["plan_izi"])
    assert response.json()["tur"] == "hata"
    with Session(ortam[2]) as db:
        assert db.get(Rapor,1).durum == "ONAYLANDI"
        delivery = db.scalar(select(Gonderim))
        assert delivery.durum == "BEKLIYOR" and delivery.alicilar == ["person@example.com"]

def test_smtp_not_configured_blocks_approval_before_decision(ortam,tmp_path):
    from mevzuat.mail import DosyaGonderici
    client,key,engine,_ = ortam
    offline = TestClient(uygulama_olustur(engine,DosyaGonderici(tmp_path/"mail"),[],GIZLI,arayuz=None,api_imza=True))
    env=(offline,key,engine,None)
    preview=request(env,"onizleme").json()
    assert request(env,"onayla",plan_izi=preview["plan_izi"]).status_code == 422
    with Session(engine) as db: assert db.get(Rapor,1).durum == "ONAY_BEKLIYOR"
