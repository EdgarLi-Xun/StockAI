# 验证站点邮件配置的预设、授权码保密、权限和发信回退。
import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from backend.cloud import mail
from backend.cloud import smtp_settings
from backend.cloud.auth import Auth
from backend.cloud.config import CloudConfig
from backend.cloud.crypto import Keyring
from backend.cloud.lifecycle import ErasureJournal
from backend.cloud.store import Store
from backend.public_app import create_app

PASSWORD = "test-cloud-password-123"
SECRET = "qq-auth-ZZ99"


@pytest.fixture
def cloud(tmp_path):
    cfg = CloudConfig(data=tmp_path / "public", erasures=tmp_path / "erasures", key_file=tmp_path / "secrets" / "keys.json", public_url="http://testserver", dev=True, background=False)
    store = Store(cfg).initialize()
    keys = Keyring.create(cfg.key_file)
    auth = Auth(store, keys)
    ErasureJournal(store, keys).create()
    admin = auth.create_admin("admin@example.test", PASSWORD)
    user = auth.new_user("user@example.test", PASSWORD, verified=True)
    return store, keys, auth, admin, user


def login(client, email):
    csrf = client.get("/api/auth/csrf").json()["csrf_token"]
    client.headers.update({"Origin": "http://testserver", "X-CSRF-Token": csrf})
    response = client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    client.headers["X-CSRF-Token"] = client.cookies["stockai_dev_csrf"]
    return response.json()["user"]


def test_qq_preset_hides_secret_and_keeps_blank_password(cloud):
    store, keys, *_ = cloud
    with TestClient(create_app(store.cfg)) as client:
        login(client, "user@example.test")
        denied = client.get("/api/admin/mail")
        assert denied.status_code == 403 and denied.json()["code"] == "ADMIN_REQUIRED"
        client.cookies.clear()
        login(client, "admin@example.test")
        saved = client.put("/api/admin/mail", json={"provider": "qq", "enabled": True, "username": "sender@qq.com", "sender": "sender@qq.com", "password": SECRET})
        assert saved.status_code == 200, saved.text
        body = saved.json()
        assert body["host"] == "smtp.qq.com" and body["port"] == 465 and body["starttls"] is False
        assert body["password_set"] is True and body["password_hint"] == "ZZ99" and body["configured"] is True
        assert SECRET not in saved.text
        stored = store.one("SELECT value FROM cloud_meta WHERE key='smtp_settings'")["value"]
        assert SECRET not in stored and "password" not in json.loads(stored)
        sealed = store.one("SELECT value FROM cloud_meta WHERE key='smtp_secret'")["value"]
        assert SECRET not in sealed
        changed = client.put("/api/admin/mail", json={"provider": "qq", "enabled": True, "username": "sender@qq.com", "sender": "other@qq.com", "password": ""})
        assert changed.status_code == 200, changed.text
        assert changed.json()["sender"] == "other@qq.com" and SECRET not in changed.text
    account = smtp_settings.effective(store, keys)
    assert account is not None and account.password == SECRET and account.sender == "other@qq.com" and account.host == "smtp.qq.com"


def test_custom_without_host_is_rejected(cloud):
    store, *_ = cloud
    with TestClient(create_app(store.cfg)) as client:
        login(client, "admin@example.test")
        response = client.put("/api/admin/mail", json={"provider": "custom", "enabled": True, "host": "", "port": 465, "starttls": False, "username": "sender@example.com", "sender": "sender@example.com", "password": SECRET})
        assert response.status_code == 400 and response.json()["detail"] == "请填写 SMTP 服务器"
        assert store.one("SELECT value FROM cloud_meta WHERE key='smtp_settings'") is None


def test_unconfigured_mail_is_not_sent(cloud):
    store, keys, auth, *_ = cloud
    auth.resend("user@example.test", "127.0.0.1", "reset")
    assert mail.send_one(store, keys) is False
    row = store.one("SELECT state,error_code FROM mail_outbox WHERE state!='cancelled'")
    assert row == {"state": "queued", "error_code": "SMTP_NOT_CONFIGURED"}


def test_saved_account_overrides_environment_and_disabled_does_not_fall_back(cloud, monkeypatch):
    store, keys, auth, *_ = cloud
    store.cfg = replace(store.cfg, smtp_host="smtp.env.invalid", smtp_port=2525, smtp_user="env@example.test", smtp_password="env-secret-should-not-send", smtp_from="env@example.test")
    with TestClient(create_app(store.cfg)) as client:
        login(client, "admin@example.test")
        saved = client.put("/api/admin/mail", json={"provider": "outlook", "enabled": True, "username": "sender@outlook.com", "sender": "sender@outlook.com", "password": SECRET})
        assert saved.status_code == 200, saved.text
        assert saved.json()["host"] == "smtp.office365.com" and saved.json()["port"] == 587 and saved.json()["starttls"] is True
    captured = {}

    class SMTP:
        def __init__(self, host, port, timeout=15, context=None):
            captured["address"] = (host, port)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def starttls(self, **kwargs):
            captured["starttls"] = True
        def login(self, username, password):
            captured["login"] = (username, password)
        def send_message(self, message):
            captured["sender"] = message["From"]
            return {}

    monkeypatch.setattr(smtp_settings.smtplib, "SMTP", SMTP)
    auth.resend("user@example.test", "127.0.0.1", "reset")
    assert mail.send_one(store, keys) is True
    assert captured == {"address": ("smtp.office365.com", 587), "starttls": True, "login": ("sender@outlook.com", SECRET), "sender": "sender@outlook.com"}
    with TestClient(create_app(store.cfg)) as client:
        login(client, "admin@example.test")
        disabled = client.put("/api/admin/mail", json={"provider": "outlook", "enabled": False, "username": "sender@outlook.com", "sender": "sender@outlook.com", "password": ""})
        assert disabled.status_code == 200 and disabled.json()["configured"] is False
    assert smtp_settings.effective(store, keys) is None
    store.execute("DELETE FROM cloud_meta WHERE key IN ('smtp_settings','smtp_secret')")
    fallback = smtp_settings.effective(store, keys)
    assert fallback is not None and fallback.host == "smtp.env.invalid" and fallback.password == "env-secret-should-not-send"
