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


def gonder(client, anahtar, veri=None, *, degistir=False, yol=YOL):
    veri = veri or {"aktor": {"id": "42", "ad": "Çağrı Şen"}}
    govde = json.dumps(veri, ensure_ascii=False).encode()
    zaman, numara = str(int(time.time())), secrets.token_hex(16)
    headers = {"X-Anahtar": anahtar[:10], "X-Zaman": zaman, "X-Numara": numara,
               "X-Imza": imza.imza_hesapla(anahtar, "POST", yol, "", zaman, numara, govde),
               "Content-Type": "application/json"}
    return client.post(yol, content=govde + (b" " if degistir else b""), headers=headers)


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


KAYNAK_YOL = "/api/portal/kaynaklar"


def kaynak_istegi(client, key, islem, veri=None, ad=None):
    return gonder(client, key, {"aktor": {"id": "42", "ad": "Çağrı Şen"},
                               "islem": islem, "veri": veri or {}, "ad": ad}, yol=KAYNAK_YOL)


def test_kaynak_listesi_ve_form_tanimlari(portal_ortam):
    client, key, _ = portal_ortam
    assert kaynak_istegi(client, key, "liste").json() == {"kaynaklar": []}
    cevap = kaynak_istegi(client, key, "tipler")
    assert cevap.status_code == 200
    assert any(t["tip"] == "rss" for t in cevap.json()["tipler"])


def test_kaynak_ekleme_surumu_ve_portal_denetimi(portal_ortam, monkeypatch):
    from mevzuat import kaynak_yonetimi
    monkeypatch.setattr(kaynak_yonetimi, "_dns", lambda host: ["93.184.216.34"])
    client, key, engine = portal_ortam
    veri = {"tip": "rss", "etiket": "Portal Test Kaynağı", "ayarlar": {"akis_url": "https://example.com/rss"},
            "varsayilan_konular": [], "aktif": True}
    cevap = kaynak_istegi(client, key, "ekle", veri)
    assert cevap.status_code == 200, cevap.text
    ad = cevap.json()["ad"]
    assert kaynak_istegi(client, key, "duzenle", {**veri, "surum": 999}, ad).status_code == 409
    cevap = kaynak_istegi(client, key, "liste")
    assert "son_basarili" in cevap.json()["kaynaklar"][0]
    with Session(engine) as db:
        log = db.scalar(select(Denetim).where(Denetim.islem == "portal_kaynak_ekle"))
        assert log.detay["portal_aktor"]["id"] == "42"


def test_kaynak_guvenligi_ve_gecersiz_islem(portal_ortam, monkeypatch):
    client, key, _ = portal_ortam
    assert kaynak_istegi(client, key, "kullanicilari_sil").status_code == 422
    assert kaynak_istegi(client, key, "duzenle", {}).status_code == 422
    assert kaynak_istegi(client, key, "bul", {"adres": "http://127.0.0.1"}).status_code == 400
    monkeypatch.delenv("MEVZUAT_PORTAL_ANAHTAR_ON_EKI")
    assert kaynak_istegi(client, key, "liste").status_code == 403


KONU_YOL = "/api/portal/konular"


def konu_istegi(client, key, islem, veri=None, kid=None):
    return gonder(client, key, {"aktor": {"id": "42", "ad": "Çağrı Şen"},
                               "islem": islem, "konu_id": kid, "veri": veri or {}}, yol=KONU_YOL)


def test_konu_onizleme_kaydetmez_ve_kullanici_denetimi(portal_ortam):
    from mevzuat.db import KonuTanimi
    client, key, engine = portal_ortam
    veri = {"ad": "Portal Konusu", "is_kollari": ["Kuyum"], "kelimeler": ["altın"],
            "haric": ["altında"], "dislanan": [], "aciklama": "Kuyum takibi"}
    cevap = konu_istegi(client, key, "onizleme", veri)
    assert cevap.status_code == 200, cevap.text
    assert cevap.json()["gun"] == 90
    with Session(engine) as db:
        assert db.scalar(select(KonuTanimi)) is None
    cevap = konu_istegi(client, key, "ekle", veri)
    assert cevap.status_code == 200, cevap.text
    kid, surum = cevap.json()["id"], cevap.json()["surum"]
    assert konu_istegi(client, key, "duzenle", {**veri, "surum": 999}, kid).status_code == 409
    # Son aktif konu kapatılamaz; başka aktif konu varken durum değişebilir.
    assert konu_istegi(client, key, "pasif", {"surum": surum}, kid).status_code == 400
    assert konu_istegi(client, key, "ekle", {**veri, "ad": "Yedek Portal Konusu"}).status_code == 200
    assert konu_istegi(client, key, "pasif", {"surum": surum}, kid).status_code == 200
    listing = konu_istegi(client, key, "liste").json()
    assert next(k for k in listing["konular"] if k["id"] == kid)["aktif"] is False
    assert "eslesme_90" in listing["konular"][0]
    with Session(engine) as db:
        log = db.scalar(select(Denetim).where(Denetim.islem == "portal_konu_ekle"))
        assert log.detay["portal_aktor"]["id"] == "42"


def test_konu_gecersiz_islemler_ve_portal_anahtari(portal_ortam, monkeypatch):
    client, key, _ = portal_ortam
    assert konu_istegi(client, key, "duzenle", {}).status_code == 422
    assert konu_istegi(client, key, "sil").status_code == 422
    monkeypatch.delenv("MEVZUAT_PORTAL_ANAHTAR_ON_EKI")
    assert konu_istegi(client, key, "liste").status_code == 403


TARAMA_YOL = "/api/portal/tarama"


def tarama_istegi(client, key, islem, veri=None):
    return gonder(client, key, {"aktor": {"id": "42", "ad": "Çağrı Şen"},
                               "islem": islem, "veri": veri or {}}, yol=TARAMA_YOL)


def test_portal_tarama_kuyrugu_kimligi_ve_tekrar_korumasi(portal_ortam):
    from datetime import datetime, time, timedelta
    from mevzuat import zamanlama
    client, key, engine = portal_ortam
    assert tarama_istegi(client, key, "baslat").status_code == 409
    with Session(engine) as db:
        zamanlama.nabiz_yaz(db, datetime.now(), "bekliyor", datetime.now() + timedelta(hours=1), [time(6, 30), time(18)])
    cevap = tarama_istegi(client, key, "baslat")
    assert cevap.status_code == 200, cevap.text
    assert cevap.json()["istek"]["durum"] == "BEKLIYOR"
    assert cevap.json()["istek"]["isteyen"] == "Çağrı Şen"
    assert tarama_istegi(client, key, "baslat").status_code == 409
    assert tarama_istegi(client, key, "durum").json()["istek"]["isteyen"] == "Çağrı Şen"


def test_portal_tarama_saatleri_ve_surumu(portal_ortam):
    client, key, _ = portal_ortam
    durum = tarama_istegi(client, key, "durum").json()["zamanlama"]
    cevap = tarama_istegi(client, key, "saatler", {"saatler": ["07:00", "18:00"], "surum": durum["surum"]})
    assert cevap.status_code == 200, cevap.text
    assert cevap.json()["saatler"] == ["07:00", "18:00"]
    assert tarama_istegi(client, key, "saatler", {"saatler": ["08:00"], "surum": durum["surum"]}).status_code == 409
    assert tarama_istegi(client, key, "saatler", {"saatler": ["23:45", "00:15"], "surum": cevap.json()["surum"]}).status_code == 400


AUDIT_YOL = "/api/portal/denetim"


def audit_istegi(client, key, **filters):
    return gonder(client, key, {"aktor": {"id": "42", "ad": "Test"}, **filters}, yol=AUDIT_YOL)


def test_portal_denetim_aktor_sayfalama_ve_gizleme(portal_ortam):
    from datetime import datetime
    client, key, engine = portal_ortam
    with Session(engine) as db:
        for i in range(3):
            db.add(Denetim(zaman=datetime.now(), islem="portal_test", detay={"portal_aktor": {"id": "42", "ad": "Portal Test"}, "smtp_sifre": "hidden"}))
        db.commit()
    first = audit_istegi(client, key, islem="portal_test", adet=2).json()
    assert first["toplam"] == 3 and len(first["kayitlar"]) == 2
    assert first["kayitlar"][0]["kullanici"] == "Portal Test"
    assert first["kayitlar"][0]["detay"]["smtp_sifre"] == "[Gizlendi]"
    with Session(engine) as db:
        db.add(Denetim(zaman=datetime.now(), islem="portal_test", detay={}))
        db.commit()
    second = audit_istegi(client, key, islem="portal_test", adet=2, sayfa=2, sinir_id=first["sinir_id"]).json()
    assert second["toplam"] == 3 and len(second["kayitlar"]) == 1
    assert not ({r["id"] for r in first["kayitlar"]} & {r["id"] for r in second["kayitlar"]})
    assert audit_istegi(client, key, islem="portal_test", q="Portal Test").json()["toplam"] == 3


def test_portal_denetim_yetkisiz_anahtar_ve_siralama(portal_ortam, monkeypatch):
    client, key, _ = portal_ortam
    assert audit_istegi(client, key, sirala="parola_hash").status_code == 422
    assert audit_istegi(client, key, baslangic="2026-10-31", bitis="2026-10-01").status_code == 422
    monkeypatch.delenv("MEVZUAT_PORTAL_ANAHTAR_ON_EKI")
    assert audit_istegi(client, key).status_code == 403


AYAR_YOL = "/api/portal/ayarlar"
def ayar_istegi(client, key, islem="liste", **data):
    return gonder(client, key, {"aktor": {"id": "42", "ad": "Çağrı Şen"}, "islem": islem, **data}, yol=AYAR_YOL)


def test_portal_ayarlari_sifreyi_gizler_sifirlar_ve_surumu_korur(portal_ortam, monkeypatch):
    from mevzuat import panel_ayarlari
    from mevzuat.db import SistemAyari
    client, key, engine = portal_ortam
    monkeypatch.setenv("MEVZUAT_SMTP_HOST", "smtp.env.test")
    first = ayar_istegi(client, key).json()
    assert not {"mfa_zorunlu", "api_imza", "pasif_silme_gun"} & {a["ad"] for a in first["alanlar"]}
    response = ayar_istegi(client, key, "kaydet", surum=first["surum"], degisiklikler={"smtp_host": "smtp.portal.test", "smtp_sifre": "gizli-test-parolasi"})
    assert response.status_code == 200, response.text
    assert "gizli-test-parolasi" not in response.text
    fields = {a["ad"]: a for a in response.json()["alanlar"]}
    assert fields["smtp_sifre"]["dolu"] and fields["smtp_sifre"]["deger"] is None
    with Session(engine) as db:
        assert "gizli-test-parolasi" not in str(db.get(SistemAyari, panel_ayarlari.AYAR_ANAHTARI).deger)
        log = db.scalar(select(Denetim).where(Denetim.islem == "portal_ayarlar_kaydet"))
        assert log.detay["portal_aktor"]["id"] == "42" and "gizli-test-parolasi" not in str(log.detay)
    assert ayar_istegi(client, key, "kaydet", surum=first["surum"], degisiklikler={"smtp_port": 25}).status_code == 409
    reset = ayar_istegi(client, key, "kaydet", surum=response.json()["surum"], degisiklikler={"smtp_host": None})
    assert next(a for a in reset.json()["alanlar"] if a["ad"] == "smtp_host")["deger"] == "smtp.env.test"


def test_portal_ayar_guvenligi_ve_hatali_kayit_atomik(portal_ortam, monkeypatch):
    client, key, _ = portal_ortam
    for changes in [{"api_imza": False}, {"mfa_zorunlu": True}, {"pasif_silme_gun": 0}, {"smtp_port": 70000, "smtp_host": "changed.test"}]:
        assert ayar_istegi(client, key, "kaydet", surum=0, degisiklikler=changes).status_code in (400, 422)
    assert ayar_istegi(client, key).json()["surum"] == 0
    assert ayar_istegi(client, key, "kaydet", degisiklikler={}).status_code == 422
    monkeypatch.delenv("MEVZUAT_PORTAL_ANAHTAR_ON_EKI")
    assert ayar_istegi(client, key).status_code == 403


def test_portal_deneme_maili_kayitli_ayari_ve_gercek_aktoru_kullanir(portal_ortam, monkeypatch):
    from mevzuat import panel_ayarlari
    client, key, engine = portal_ortam
    for a in panel_ayarlari.ALANLAR:
        monkeypatch.delenv(a.degisken, raising=False)
    assert ayar_istegi(client, key, "mail_dene", adres="test@example.com").json()["tur"] == "hata"
    assert ayar_istegi(client, key, "mail_dene", adres="bad").status_code == 422
    saved = ayar_istegi(client, key, "kaydet", surum=0, degisiklikler={"smtp_host": "smtp.saved.test"})
    assert saved.status_code == 200
    sent = []
    class FakeSender:
        def gonder(self, mail): sent.append(mail)
    def sender(config):
        assert config["smtp_host"] == "smtp.saved.test"
        return FakeSender()
    monkeypatch.setattr(panel_ayarlari, "gonderici", sender)
    assert ayar_istegi(client, key, "mail_dene", adres="test@example.com").json()["tur"] == "basari"
    assert len(sent) == 1 and sent[0].alicilar == ["test@example.com"]
    with Session(engine) as db:
        log = db.scalar(select(Denetim).where(Denetim.islem == "portal_ayarlar_mail_dene"))
        assert log.detay["portal_aktor"]["id"] == "42"
