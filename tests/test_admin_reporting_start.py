from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.config import settings
from app.models import ProcessingJob, User
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
            started_at=cutoff - timedelta(seconds=1),
        ),
        ProcessingJob(
            user_id=new_user.id, kind="summary", status="success",
            started_at=cutoff + timedelta(seconds=1),
        ),
    ])
    db.commit()

    headers = {"X-Admin-Key": "admin-test-key"}
    dashboard = client.get(f"{API}/admin/dashboard", headers=headers)
    assert dashboard.status_code == 200, dashboard.text
    payload = dashboard.json()
    assert payload["metrics"]["total_users"] == 1
    assert payload["metrics"]["video_jobs"] == 1
    assert [row["email"] for row in payload["recent_jobs"]] == ["after@example.com"]

    # The old account is still in PostgreSQL; only its admin reporting view is hidden.
    assert db.query(User).filter_by(email="before@example.com").one_or_none() is not None

