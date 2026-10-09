from __future__ import annotations

from datetime import datetime, timezone

from app.config import settings
from app.models import PushCampaignDelivery, PushCampaignRun, Subscription, SubscriptionStatus, User, WebPushSubscription
from app.services import ntfy, push_campaigns, web_push
from tests.conftest import register

API = settings.API_PREFIX


def _enable(monkeypatch):
    monkeypatch.setattr(settings, "WEB_PUSH_ENABLED", True)
    monkeypatch.setattr(settings, "WEB_PUSH_VAPID_PUBLIC_KEY", "B" * 87)
    monkeypatch.setattr(settings, "WEB_PUSH_VAPID_PRIVATE_KEY", "test-private-key")


def _browser(db, user, suffix: str):
    row = WebPushSubscription(
        user_id=user.id,
        endpoint=f"https://push.example.test/{suffix}",
        p256dh="a" * 32,
        auth="b" * 16,
    )
    db.add(row)
    db.commit()
    return row


def test_campaign_audience_excludes_active_paid_and_manual_pro_users(client, db):
    free_body, _, _ = register(client, email="campaign-free@example.com")
    paid_body, _, _ = register(client, email="campaign-paid@example.com")
    grant_body, _, _ = register(client, email="campaign-grant@example.com")
    free = db.get(User, free_body["user"]["id"])
    paid = db.get(User, paid_body["user"]["id"])
    grant = db.get(User, grant_body["user"]["id"])
    _browser(db, free, "free")
    _browser(db, paid, "paid")
    _browser(db, grant, "grant")
    db.add(Subscription(user_id=paid.id, provider="mock", status=SubscriptionStatus.active))
    from app.models import ManualProGrant
    db.add(ManualProGrant(user_id=grant.id, status="active", starts_at=push_campaigns.now()))
    db.commit()

    daily = push_campaigns.ensure_campaigns(db)["daily"]
    db.commit()
    preview = push_campaigns.preview(db, daily)
    assert preview["eligible_users"] == 1
    assert preview["eligible_endpoints"] == 1


def test_campaign_run_records_delivery_and_cooldown(client, db, monkeypatch):
    _enable(monkeypatch)
    body, _, _ = register(client, email="campaign-run@example.com")
    user = db.get(User, body["user"]["id"])
    _browser(db, user, "run")
    monkeypatch.setattr(web_push, "webpush", lambda **_: None)
    reports = []
    monkeypatch.setattr(ntfy, "campaign_completed", lambda *args, **kwargs: reports.append((args, kwargs)))

    daily = push_campaigns.ensure_campaigns(db)["daily"]
    db.commit()
    run = push_campaigns.queue_run(db, daily, trigger="manual")
    assert run is not None
    result = push_campaigns.execute_run(run.id)

    assert result["status"] == "completed"
    assert result["target_users"] == 1
    assert result["sent_endpoints"] == 1
    db.expire_all()
    stored = db.get(PushCampaignRun, run.id)
    assert stored.status == "completed"
    assert db.query(PushCampaignDelivery).filter_by(campaign_id=daily.id, user_id=user.id).one()
    assert push_campaigns.preview(db, daily)["eligible_users"] == 0
    assert reports and reports[0][0][0] == "daily"


def test_scheduled_campaign_key_is_idempotent_and_uses_ist_time(db):
    campaigns = push_campaigns.ensure_campaigns(db)
    daily = campaigns["daily"]
    daily.daily_time = "10:00"
    campaigns["weekly"].enabled = False
    db.commit()
    # 04:30 UTC is 10:00 IST.
    at = datetime(2026, 10, 9, 4, 30, tzinfo=timezone.utc)
    assert [item.kind for item in push_campaigns.due_campaigns(db, at=at)] == ["daily"]
    key = push_campaigns.scheduled_key(daily, at=at)
    assert push_campaigns.queue_run(db, daily, trigger="scheduled", trigger_key=key) is not None
    assert push_campaigns.queue_run(db, daily, trigger="scheduled", trigger_key=key) is None


def test_admin_can_edit_and_preview_campaign(client, db):
    body, headers, _ = register(client, email="campaign-admin@example.com")
    user = db.get(User, body["user"]["id"])
    user.is_admin = True
    db.commit()
    listed = client.get(f"{API}/admin/push-campaigns", headers=headers)
    assert listed.status_code == 200, listed.text
    daily = next(item for item in listed.json()["campaigns"] if item["kind"] == "daily")
    changed = client.patch(
        f"{API}/admin/push-campaigns/daily",
        headers=headers,
        json={**{key: daily[key] for key in ("enabled", "title", "body", "url", "daily_time", "weekly_day", "cooldown_hours")}, "title": "A new daily reminder"},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["title"] == "A new daily reminder"
    invalid = client.patch(
        f"{API}/admin/push-campaigns/daily",
        headers=headers,
        json={**changed.json(), "url": "https://not-tubenotes.example"},
    )
    assert invalid.status_code == 422
