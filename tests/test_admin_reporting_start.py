from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.config import settings
from app.models import BillingPayment, ProcessingJob, Subscription, SubscriptionStatus, User
from tests.conftest import API, register


def test_admin_reporting_start_hides_old_records_without_deleting_them(
    client, db, monkeypatch
):
    cutoff = datetime.now(timezone.utc)
    monkeypatch.setattr(settings, "ADMIN_API_KEY", "admin-test-key")
    monkeypatch.setattr(settings, "ADMIN_REPORTING_START_AT", cutoff)

    register(client, email="before@example.com")
    register(client, email="after@example.com")
    old_user = db.query(User).filter_by(email="before@example.com").one()
    new_user = db.query(User).filter_by(email="after@example.com").one()
    old_user.created_at = cutoff - timedelta(seconds=1)
    new_user.created_at = cutoff + timedelta(seconds=1)
    db.add_all([
        ProcessingJob(
            user_id=old_user.id, kind="summary", status="success",
            # An old account can still use the site after the reporting reset.
            # Its fresh activity must appear, while its pre-reset history stays
            # hidden from the admin dashboard.
            started_at=cutoff + timedelta(seconds=2),
        ),
        ProcessingJob(
            user_id=new_user.id, kind="summary", status="success",
            started_at=cutoff + timedelta(seconds=1),
        ),
        Subscription(
            user_id=old_user.id, provider="mock", status=SubscriptionStatus.active,
            price_cents=29_900, currency="INR", created_at=cutoff + timedelta(seconds=2),
        ),
        Subscription(
            user_id=new_user.id, provider="mock", status=SubscriptionStatus.active,
            price_cents=29_900, currency="INR", created_at=cutoff + timedelta(seconds=1),
        ),
        BillingPayment(
            user_id=old_user.id, provider="razorpay", provider_payment_id="pay_before",
            status="captured", amount_subunits=29_900, currency="INR",
            created_at=cutoff + timedelta(seconds=2),
        ),
        BillingPayment(
            user_id=new_user.id, provider="razorpay", provider_payment_id="pay_after",
            provider_invoice_id="inv_after", status="captured", amount_subunits=9_900,
            currency="INR", invoice_url="https://rzp.io/i/inv_after",
            created_at=cutoff + timedelta(seconds=1),
        ),
    ])
    db.commit()

    headers = {"X-Admin-Key": "admin-test-key"}
    dashboard = client.get(f"{API}/admin/dashboard", headers=headers)
    assert dashboard.status_code == 200, dashboard.text
    payload = dashboard.json()
    assert payload["metrics"]["total_users"] == 2
    assert payload["metrics"]["video_jobs"] == 2
    assert payload["metrics"]["active_today"] == 2
    assert payload["metrics"]["paid_users"] == 2
    assert payload["metrics"]["active_pro_users"] == 2
    assert {row["email"] for row in payload["recent_jobs"]} == {
        "before@example.com", "after@example.com"
    }

    # Each card filter includes every account with report-period activity,
    # including accounts created before the reporting reset.
    for user_filter in ("active_today", "paid", "subscribed"):
        directory = client.get(
            f"{API}/admin/users/overview?status={user_filter}", headers=headers
        )
        assert directory.status_code == 200, directory.text
        assert {row["email"] for row in directory.json()["users"]} == {
            "before@example.com", "after@example.com"
        }

    billing = client.get(f"{API}/admin/billing-history?days=10", headers=headers)
    assert billing.status_code == 200, billing.text
    billing_payload = billing.json()
    assert billing_payload["total_payments"] == 2
    assert billing_payload["successful_payments"] == 2
    assert billing_payload["paid_by_currency"] == {"INR": 39_800}
    payments = {item["payment_id"]: item for item in billing_payload["payments"]}
    assert payments["pay_after"]["email"] == "after@example.com"
    assert payments["pay_after"]["invoice_url"] == "https://rzp.io/i/inv_after"

    # The old account is still in PostgreSQL and re-enters the admin report
    # when it performs fresh activity; no historical row was deleted.
    assert db.query(User).filter_by(email="before@example.com").one_or_none() is not None


def test_dashboard_separates_completed_extension_operations(client, db, monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_API_KEY", "admin-test-key")
    body, _headers, _device = register(client, email="extension@example.com")
    now = datetime.now(timezone.utc)
    db.add_all([
        ProcessingJob(
            user_id=body["user"]["id"], kind="summary", client_source="extension",
            status="success", started_at=now,
        ),
        ProcessingJob(
            user_id=body["user"]["id"], kind="notes", client_source="extension",
            status="success", pdf_generated=True, started_at=now,
        ),
        ProcessingJob(
            user_id=body["user"]["id"], kind="translation", client_source="extension",
            status="success", started_at=now,
        ),
        ProcessingJob(
            user_id=body["user"]["id"], kind="summary", client_source="web",
            status="success", started_at=now,
        ),
    ])
    db.commit()

    admin_headers = {"X-Admin-Key": "admin-test-key"}
    dashboard = client.get(f"{API}/admin/dashboard", headers=admin_headers)
    assert dashboard.status_code == 200, dashboard.text
    metrics = dashboard.json()["metrics"]
    assert metrics["extension_summaries"] == 1
    assert metrics["extension_pdfs"] == 1
    assert metrics["extension_translations"] == 1

    overview = client.get(
        f"{API}/admin/operations?category=extension_summaries&days=10",
        headers=admin_headers,
    )
    assert overview.status_code == 200, overview.text
    assert overview.json()["total_jobs"] == 1
    assert overview.json()["operations"][0]["client_source"] == "extension"


def test_extension_translation_activity_is_counted_only_for_extension_origin(client, db, monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_API_KEY", "admin-test-key")
    _body, headers, _device = register(client, email="extension-translate@example.com")
    payload = {
        "kind": "translation",
        "language": "hi",
        "video_url": "https://www.youtube.com/watch?v=abcdefghijk",
        "title": "Extension translation",
        "output_chars": 42,
    }

    blocked = client.post(f"{API}/extension/activity", json=payload, headers=headers)
    assert blocked.status_code == 403

    allowed = client.post(
        f"{API}/extension/activity",
        json=payload,
        headers={**headers, "Origin": "chrome-extension://abcdefghijklmnop"},
    )
    assert allowed.status_code == 200, allowed.text

    dashboard = client.get(
        f"{API}/admin/dashboard", headers={"X-Admin-Key": "admin-test-key"}
    )
    assert dashboard.status_code == 200, dashboard.text
    assert dashboard.json()["metrics"]["extension_translations"] == 1

