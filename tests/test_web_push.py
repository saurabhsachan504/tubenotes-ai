from __future__ import annotations

import json

from app.config import settings
from app.models import User, WebPushSubscription
from app.services import web_push
from tests.conftest import register

API = settings.API_PREFIX


def _enable(monkeypatch):
    monkeypatch.setattr(settings, "WEB_PUSH_ENABLED", True)
    monkeypatch.setattr(settings, "WEB_PUSH_VAPID_PUBLIC_KEY", "B" * 87)
    monkeypatch.setattr(settings, "WEB_PUSH_VAPID_PRIVATE_KEY", "test-private-key")


def _subscription(send_welcome=False):
    return {
        "endpoint": "https://push.example.test/subscriptions/abc123",
        "p256dh": "a" * 32,
        "auth": "b" * 16,
        "send_welcome": send_welcome,
    }


def test_push_config_is_inert_until_vapid_is_configured(client):
    assert client.get(f"{API}/push/config").json() == {"enabled": False, "public_key": ""}


def test_authenticated_browser_subscription_is_saved(client, db, monkeypatch):
    _enable(monkeypatch)
    body, headers, _ = register(client, email="push-user@example.com")

    res = client.post(f"{API}/push/subscribe", json=_subscription(), headers=headers)
    assert res.status_code == 200, res.text
    row = db.query(WebPushSubscription).one()
    assert row.user_id == body["user"]["id"]
    assert row.endpoint == _subscription()["endpoint"]


def test_trial_push_is_encrypted_for_the_users_registered_browser(client, db, monkeypatch):
    _enable(monkeypatch)
    body, headers, _ = register(client, email="push-trial@example.com")
    assert client.post(f"{API}/push/subscribe", json=_subscription(), headers=headers).status_code == 200

    sent = {}

    def fake_webpush(**kwargs):
        sent.update(kwargs)

    monkeypatch.setattr(web_push, "webpush", fake_webpush)
    user = db.get(User, body["user"]["id"])
    assert web_push.trial_milestone(db, user, 5) == 1
    payload = json.loads(sent["data"])
    assert payload["title"] == "5 free videos left"
    assert sent["subscription_info"]["endpoint"] == _subscription()["endpoint"]
    assert sent["vapid_private_key"] == "test-private-key"
