from __future__ import annotations

from app.config import settings
from app.models import User
from app.services import ntfy


class _Response:
    def raise_for_status(self):
        return None


def test_ntfy_publish_uses_private_token_and_masks_account(monkeypatch):
    sent = {}
    monkeypatch.setattr(settings, "NTFY_ENABLED", True)
    monkeypatch.setattr(settings, "NTFY_BASE_URL", "https://ntfy.example.test")
    monkeypatch.setattr(settings, "NTFY_TOPIC", "private-alerts")
    monkeypatch.setattr(settings, "NTFY_TOKEN", "tk_test_token")

    def fake_post(url, *, content, headers, timeout):
        sent.update(url=url, content=content, headers=headers, timeout=timeout)
        return _Response()

    monkeypatch.setattr(ntfy.httpx, "post", fake_post)
    user = User(
        email="saurabh.sachan@example.com",
        password_hash="unused",
        billing_country="IN",
    )

    assert ntfy.signup(user, 20) is True
    assert sent["url"] == "https://ntfy.example.test/private-alerts"
    assert sent["headers"]["Authorization"] == "Bearer tk_test_token"
    assert sent["headers"]["Title"] == "New TubeNotes signup"
    assert b"sa***@example.com" in sent["content"]
    assert b"saurabh.sachan@example.com" not in sent["content"]


def test_ntfy_does_nothing_until_enabled(monkeypatch):
    monkeypatch.setattr(settings, "NTFY_ENABLED", False)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("ntfy must not be contacted when disabled")

    monkeypatch.setattr(ntfy.httpx, "post", fail_if_called)
    assert ntfy.publish("Ignored", "No notification") is False
