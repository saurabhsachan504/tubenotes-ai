from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.config import settings as live_settings
from app.models import ManualProGrant
from tests.conftest import API, make_device, register


def _admin_headers(monkeypatch) -> dict[str, str]:
    monkeypatch.setattr(live_settings, "ADMIN_API_KEY", "manual-pro-admin-key")
    return {"X-Admin-Key": "manual-pro-admin-key"}


def test_admin_can_grant_extend_and_revoke_complimentary_pro(client, db, device, monkeypatch):
    body, user_headers, _ = register(client, email="gifted@example.com", device=device)
    admin_headers = _admin_headers(monkeypatch)
    user_id = body["user"]["id"]

    grant = client.post(
        f"{API}/admin/users/{user_id}/manual-pro",
        json={"duration_days": 30, "note": "Launch partner"},
        headers=admin_headers,
    )
    assert grant.status_code == 200, grant.text
    assert grant.json()["manual_pro"]["note"] == "Launch partner"

    entitlement = client.post(
        f"{API}/entitlement/check", json={"device": device}, headers=user_headers
    )
    assert entitlement.status_code == 200
    assert entitlement.json()["plan"] == "subscription"
    assert entitlement.json()["reason"] == "manual_pro_active"
    assert entitlement.json()["subscription_status"] == "manual"

    run = client.post(
        f"{API}/usage/consume",
        json={"device": device, "action": "run", "idempotency_key": "manual-pro-run"},
        headers=user_headers,
    )
    assert run.status_code == 200, run.text
    assert run.json()["granted_by"] == "manual_pro"
    assert run.json()["consumed"] is False

    extend = client.post(
        f"{API}/admin/users/{user_id}/manual-pro",
        json={"duration_days": 30}, headers=admin_headers,
    )
    assert extend.status_code == 200, extend.text
    rows = db.query(ManualProGrant).filter_by(user_id=user_id).all()
    assert {row.status for row in rows} == {"active", "superseded"}
    active = next(row for row in rows if row.status == "active")
    assert active.expires_at is not None
    active_expiry = active.expires_at.replace(tzinfo=timezone.utc) if active.expires_at.tzinfo is None else active.expires_at
    assert active_expiry > datetime.now(timezone.utc) + timedelta(days=55)

    overview = client.get(f"{API}/admin/users/overview?status=subscribed", headers=admin_headers)
    assert overview.status_code == 200, overview.text
    assert any(row["id"] == user_id and row["manual_pro"] for row in overview.json()["users"])

    revoke = client.delete(f"{API}/admin/users/{user_id}/manual-pro", headers=admin_headers)
    assert revoke.status_code == 200, revoke.text
    after = client.post(
        f"{API}/entitlement/check", json={"device": device}, headers=user_headers
    ).json()
    assert after["reason"] == "trial_available"


def test_manual_pro_does_not_create_a_billing_payment(client, db, device, monkeypatch):
    body, user_headers, _ = register(client, email="lifetime@example.com", device=device)
    admin_headers = _admin_headers(monkeypatch)
    user_id = body["user"]["id"]

    response = client.post(
        f"{API}/admin/users/{user_id}/manual-pro",
        json={"lifetime": True, "duration_days": 30, "note": "Team account"},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["manual_pro"]["expires_at"] is None

    assert client.get(f"{API}/billing/history", headers=user_headers).json() == []
    detail = client.get(f"{API}/admin/users/{user_id}/detail", headers=admin_headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["user"]["manual_pro"]["expires_at"] is None
    assert len(detail.json()["manual_pro_history"]) == 1
