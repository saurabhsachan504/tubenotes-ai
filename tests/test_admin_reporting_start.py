from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.config import settings
from app.models import ProcessingJob, Subscription, SubscriptionStatus, User
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
            # Its activity must not inflate a card whose click-through directory
            # deliberately hides that account.
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
    ])
    db.commit()

    headers = {"X-Admin-Key": "admin-test-key"}
    dashboard = client.get(f"{API}/admin/dashboard", headers=headers)
    assert dashboard.status_code == 200, dashboard.text
    payload = dashboard.json()
    assert payload["metrics"]["total_users"] == 1
    assert payload["metrics"]["video_jobs"] == 1
    assert payload["metrics"]["active_today"] == 1
    assert payload["metrics"]["paid_users"] == 1
    assert payload["metrics"]["active_pro_users"] == 1
    assert [row["email"] for row in payload["recent_jobs"]] == ["after@example.com"]

    # Each card filter now returns exactly the reportable account it counted.
    for user_filter in ("active_today", "paid", "subscribed"):
        directory = client.get(
            f"{API}/admin/users/overview?status={user_filter}", headers=headers
        )
        assert directory.status_code == 200, directory.text
        assert [row["email"] for row in directory.json()["users"]] == ["after@example.com"]

    # The old account is still in PostgreSQL; only its admin reporting view is hidden.
    assert db.query(User).filter_by(email="before@example.com").one_or_none() is not None

