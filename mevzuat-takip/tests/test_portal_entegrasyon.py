"""Portal kullanıcı bilgisi sadece izinli ve imzalı servis isteğinden kabul edilir."""
import json
import secrets
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from mevzuat.db import Denetim, Rapor, init_db
from mevzuat.web import uygulama_olustur, imza
from mevzuat.web.guvenlik import api_anahtari_uret, kullanici_ekle

GIZLI = "t" * 40
YOL = "/api/portal/raporlar/1/detay"


class Posta:
    def gonder(self, mail):
        raise AssertionError("Rapor okuma mail göndermemeli")


@pytest.fixture
def portal_ortam(tmp_path, monkeypatch):
    from datetime import datetime
    engine = create_engine(f"sqlite:///{tmp_path / 'portal.db'}")
    init_db(engine)
    with Session(engine) as db:
        admin = kullanici_ekle(db, "admin@firma.com", "Admin", "admin", "dogru-parola-123")
        _, anahtar = api_anahtari_uret(db, "Portal", "onaylayici", None, admin, gizli_anahtar=GIZLI)
        db.add(Rapor(id=1, olusturuldu=datetime.now(), durum="ONAY_BEKLIYOR", konu="Test", kayit_sayisi=0))
        db.commit()
    monkeypatch.setenv("MEVZUAT_PORTAL_ANAHTAR_ON_EKI", anahtar[:10])
    client = TestClient(uygulama_olustur(engine, Posta(), [], GIZLI, arayuz=None, api_imza=True))
    return client, anahtar, engine


def gonder(client, anahtar, veri=None, *, degistir=False):
    veri = veri or {"aktor": {"id": "42", "ad": "Çağrı Şen"}}
    govde = json.dumps(veri, ensure_ascii=False).encode()
    zaman, numara = str(int(time.time())), secrets.token_hex(16)
    headers = {"X-Anahtar": anahtar[:10], "X-Zaman": zaman, "X-Numara": numara,
               "X-Imza": imza.imza_hesapla(anahtar, "POST", YOL, "", zaman, numara, govde),
               "Content-Type": "application/json"}
    return client.post(YOL, content=govde + (b" " if degistir else b""), headers=headers)


def test_portal_kullanicisi_denetimde_kaydedilir(portal_ortam):
    client, key, engine = portal_ortam
    cevap = gonder(client, key)
    assert cevap.status_code == 200, cevap.text
    assert cevap.json()["rapor"]["id"] == 1
    with Session(engine) as db:
        log = db.scalar(select(Denetim).where(Denetim.islem == "portal_rapor_goruntule"))
        assert log.detay["portal_aktor"] == {"id": "42", "ad": "Çağrı Şen"}
        assert db.get(Rapor, 1).durum == "ONAY_BEKLIYOR"


def test_portal_yapilandirilmadan_acilmaz(portal_ortam, monkeypatch):
    client, key, _ = portal_ortam
    monkeypatch.delenv("MEVZUAT_PORTAL_ANAHTAR_ON_EKI")
    assert gonder(client, key).status_code == 403


def test_baska_anahtar_portal_adina_kisi_bildiremez(portal_ortam, monkeypatch):
    client, key, _ = portal_ortam
    monkeypatch.setenv("MEVZUAT_PORTAL_ANAHTAR_ON_EKI", "mvz_baska1")
    assert gonder(client, key).status_code == 403


def test_degistirilen_kimlik_ve_imzasiz_istek_reddedilir(portal_ortam):
    client, key, _ = portal_ortam
    assert gonder(client, key, degistir=True).status_code == 401
    assert client.post(YOL, json={"aktor": {"id": "42", "ad": "Test"}}).status_code == 403


def test_gecersiz_kullanici_ve_ek_alanlar_reddedilir(portal_ortam):
    client, key, _ = portal_ortam
    assert gonder(client, key, {"aktor": {"id": "-1", "ad": "Test"}}).status_code == 422
    assert gonder(client, key, {"aktor": {"id": "42", "ad": "Test"}, "rol": "admin"}).status_code == 422
