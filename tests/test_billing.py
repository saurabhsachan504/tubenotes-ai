from __future__ import annotations

import hashlib
import hmac
import json

from app.config import settings
from tests.conftest import activate_subscription, make_device, register

API = settings.API_PREFIX


def test_plans_offer_india_and_international_monthly_prices(client):
    plans = client.get(f"{API}/billing/plans").json()
    assert {(p["currency"], p["price_cents"]) for p in plans} == {
        ("INR", 29_900),
        ("USD", 500),
    }
    assert all(p["interval"] == "month" and p["free_trials"] == 5 for p in plans)


def test_checkout_requires_auth(client):
    assert client.post(f"{API}/billing/checkout", json={}).status_code == 401


def test_checkout_returns_a_url(client, device):
    _, headers, _ = register(client, device=device)
    res = client.post(f"{API}/billing/checkout", json={}, headers=headers)
    assert res.status_code == 200
    body = res.json()
    assert body["provider"] == "mock"
    assert body["checkout_url"].startswith("http")


def test_automatic_country_price_uses_trusted_cloudflare_header(client, device):
    international_price = client.get(f"{API}/billing/price")
    assert international_price.headers["cache-control"] == "private, no-store"
    assert international_price.json()["currency"] == "USD"
    india_price = client.get(
        f"{API}/billing/price", headers={"CF-IPCountry": "IN"}
    ).json()
    assert india_price["currency"] == "INR"
    assert india_price["price_cents"] == 29_900

    _, headers, _ = register(client, email="india@example.com", device=device)
    checkout = client.post(
        f"{API}/billing/checkout",
        json={},
        headers={**headers, "CF-IPCountry": "IN"},
    )
    assert checkout.status_code == 200, checkout.text
    sub = activate_subscription(client, headers)
    assert sub["currency"] == "INR"
    assert sub["price_cents"] == 29_900


def test_india_launch_offer_checkout_uses_the_server_selected_rs99_plan(client, device):
    _, headers, _ = register(client, email="offer@example.com", device=device)
    checkout = client.post(
        f"{API}/billing/checkout",
        json={"offer_code": "india_launch_99"},
        headers={**headers, "CF-IPCountry": "IN"},
    )
    assert checkout.status_code == 200, checkout.text
    sub = activate_subscription(client, headers)
    assert sub["currency"] == "INR"
    assert sub["price_cents"] == 9_900


def test_india_launch_offer_rejects_non_india_billing_accounts(client, device):
    _, headers, _ = register(client, email="not-india@example.com", device=device)
    res = client.post(
        f"{API}/billing/checkout", json={"offer_code": "india_launch_99"}, headers=headers
    )
    assert res.status_code == 422


def test_country_header_is_ignored_when_not_trusted(client, monkeypatch):
    monkeypatch.setattr(settings, "TRUST_CLOUDFLARE_COUNTRY_HEADER", False)
    price = client.get(f"{API}/billing/price", headers={"CF-IPCountry": "IN"}).json()
    assert price["currency"] == "USD"


def test_cannot_checkout_twice_while_active(client, device):
    body, headers, _ = register(client, device=device)
    activate_subscription(client, headers)
    res = client.post(f"{API}/billing/checkout", json={}, headers=headers)
    assert res.status_code == 409


def test_subscription_status_endpoint(client, device):
    body, headers, _ = register(client, device=device)
    assert client.get(f"{API}/billing/subscription", headers=headers).json() is None

    activate_subscription(client, headers)
    sub = client.get(f"{API}/billing/subscription", headers=headers).json()
    assert sub["status"] == "active"
    assert sub["price_cents"] == 500
    assert sub["current_period_end"] is not None


def test_cancel_marks_end_of_period(client, device):
    body, headers, _ = register(client, device=device)
    activate_subscription(client, headers)

    res = client.post(f"{API}/billing/cancel", json={}, headers=headers)
    assert res.status_code == 200

    sub = client.get(f"{API}/billing/subscription", headers=headers).json()
    assert sub["cancel_at_period_end"] is True
    # Access continues until the period actually ends.
    ent = client.post(
        f"{API}/entitlement/check", json={"device": {"installation_id": "x" * 12}},
        headers=headers,
    ).json()
    assert ent["allowed"] is True


def test_expired_subscription_stops_granting_access(client, db, device):
    from datetime import datetime, timedelta, timezone

    from app.models import Subscription

    body, headers, _ = register(client, device=device)
    activate_subscription(client, headers)

    sub = db.query(Subscription).filter_by(user_id=body["user"]["id"]).one()
    sub.current_period_end = datetime.now(timezone.utc) - timedelta(days=1)
    db.commit()

    ent = client.post(
        f"{API}/entitlement/check", json={"device": device}, headers=headers
    ).json()
    assert ent["plan"] == "free_trial"


def test_mock_confirm_requires_auth_and_only_affects_the_caller(client, device):
    """The fake checkout must not be a free-subscription faucet."""
    victim, victim_headers, _ = register(client, email="victim@example.com", device=device)

    # No token at all.
    assert client.post(f"{API}/billing/mock/confirm", json={}).status_code == 401

    # A signed-in attacker cannot name someone else's user_id.
    _, attacker_headers, _ = register(
        client, email="attacker@example.com", device=make_device()
    )
    res = client.post(
        f"{API}/billing/mock/confirm",
        json={"user_id": victim["user"]["id"]},
        headers=attacker_headers,
    )
    assert res.status_code == 200
    # ...the subscription landed on the attacker, not the victim.
    assert client.get(f"{API}/billing/subscription", headers=victim_headers).json() is None


def test_mock_endpoints_disabled_without_a_secret(client, device, monkeypatch):
    from app.config import settings as live

    _, headers, _ = register(client, device=device)
    monkeypatch.setattr(live, "MOCK_BILLING_SECRET", "")

    assert (
        client.post(f"{API}/billing/mock/confirm", json={}, headers=headers).status_code
        == 404
    )
    assert (
        client.get(f"{API}/billing/mock/checkout?session_id=x&token=y").status_code == 404
    )


def test_mock_webhook_verifies_its_signature(client, device):
    from app.config import settings as live

    body, _, _ = register(client, email="mockwh@example.com", device=device)
    payload = json.dumps(
        {"id": "evt_1", "type": "subscription.updated", "user_id": body["user"]["id"]}
    ).encode()

    unsigned = client.post(
        f"{API}/webhooks/mock", content=payload, headers={"content-type": "application/json"}
    )
    assert unsigned.status_code == 400

    forged = client.post(
        f"{API}/webhooks/mock",
        content=payload,
        headers={"x-mock-signature": "deadbeef", "content-type": "application/json"},
    )
    assert forged.status_code == 400

    signature = hmac.new(
        live.MOCK_BILLING_SECRET.encode(), payload, hashlib.sha256
    ).hexdigest()
    signed = client.post(
        f"{API}/webhooks/mock",
        content=payload,
        headers={"x-mock-signature": signature, "content-type": "application/json"},
    )
    assert signed.status_code == 200


def test_webhook_rejects_bad_signature(client, monkeypatch):
    from app.config import settings as live

    monkeypatch.setattr(live, "PAYMENT_PROVIDER", "razorpay")
    monkeypatch.setattr(live, "RAZORPAY_WEBHOOK_SECRET", "whsec")
    monkeypatch.setattr(live, "RAZORPAY_KEY_ID", "rzp_test")
    monkeypatch.setattr(live, "RAZORPAY_KEY_SECRET", "secret")
    monkeypatch.setattr(live, "RAZORPAY_PLAN_ID", "plan_123")

    from app.services import payments

    payments.get_provider.cache_clear()
    try:
        res = client.post(
            f"{API}/webhooks/razorpay",
            content=b'{"event":"subscription.activated"}',
            headers={"x-razorpay-signature": "deadbeef", "content-type": "application/json"},
        )
        assert res.status_code == 400
    finally:
        payments.get_provider.cache_clear()


def test_razorpay_webhook_activates_and_is_idempotent(client, device, monkeypatch):
    from app.config import settings as live

    body, headers, _ = register(client, email="rz@example.com", device=device)
    user_id = body["user"]["id"]

    monkeypatch.setattr(live, "PAYMENT_PROVIDER", "razorpay")
    monkeypatch.setattr(live, "RAZORPAY_WEBHOOK_SECRET", "whsec")
    monkeypatch.setattr(live, "RAZORPAY_KEY_ID", "rzp_test")
    monkeypatch.setattr(live, "RAZORPAY_KEY_SECRET", "secret")
    monkeypatch.setattr(live, "RAZORPAY_PLAN_ID", "plan_123")

    from app.services import payments
    from app.services.payments.razorpay_provider import RazorpayProvider

    monkeypatch.setattr(
        RazorpayProvider,
        "fetch_invoice_url",
        lambda _self, invoice_id: f"https://rzp.io/i/{invoice_id}",
    )
    monkeypatch.setattr(
        RazorpayProvider,
        "fetch_subscription_payments",
        lambda _self, _subscription_id: [
            {
                "id": "pay_before_history",
                "status": "captured",
                "amount": 29900,
                "currency": "INR",
                "invoice_id": "inv_before_history",
                "created_at": 1690000000,
            }
        ],
    )
    payments.get_provider.cache_clear()
    try:
        payload = json.dumps(
            {
                "event": "subscription.activated",
                "created_at": 1700000000,
                "payload": {
                    "subscription": {
                        "entity": {
                            "id": "sub_test_1",
                            "status": "active",
                            "customer_id": "cust_1",
                            "current_end": 1900000000,
                            "notes": {"user_id": user_id},
                        }
                    },
                    "payment": {
                        "entity": {
                            "id": "pay_test_1",
                            "status": "captured",
                            "amount": 9900,
                            "currency": "INR",
                            "invoice_id": "inv_test_1",
                            "created_at": 1700000000,
                        }
                    },
                },
            }
        ).encode()
        signature = hmac.new(b"whsec", payload, hashlib.sha256).hexdigest()
        hdrs = {
            "x-razorpay-signature": signature,
            "x-razorpay-event-id": "evt_1",
            "content-type": "application/json",
        }

        first = client.post(f"{API}/webhooks/razorpay", content=payload, headers=hdrs)
        assert first.status_code == 200
        assert first.json()["detail"] == "ok"

        # Provider retries the same event - must not create a second subscription.
        second = client.post(f"{API}/webhooks/razorpay", content=payload, headers=hdrs)
        assert second.json()["detail"] == "duplicate ignored"
    finally:
        payments.get_provider.cache_clear()

    sub = client.get(f"{API}/billing/subscription", headers=headers).json()
    assert sub["status"] == "active"

    history = client.get(f"{API}/billing/history", headers=headers)
    assert history.status_code == 200, history.text
    rows = history.json()
    assert len(rows) == 2
    assert rows[0]["provider"] == "razorpay"
    assert rows[0]["status"] == "captured"
    assert rows[0]["amount_subunits"] == 9900
    assert rows[0]["currency"] == "INR"
    assert rows[0]["paid_at"] is not None
    assert rows[0]["invoice_url"] == "https://rzp.io/i/inv_test_1"
    assert rows[1]["amount_subunits"] == 29900
    assert rows[1]["invoice_url"] == "https://rzp.io/i/inv_before_history"

    from app.models import Subscription

    ent = client.post(
        f"{API}/entitlement/check", json={"device": device}, headers=headers
    ).json()
    assert ent["plan"] == "subscription"
