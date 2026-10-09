"""Recurring, filtered browser-push campaigns for the admin dashboard.

Customer account events (payment, renewal, cancellation) stay one-to-one.
This module is only for the operator's daily/weekly free-user reminders.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.models import (
    ManualProGrant,
    PushCampaign,
    PushCampaignDelivery,
    PushCampaignRun,
    Subscription,
    SubscriptionStatus,
    User,
    WebPushSubscription,
)
from app.services import ntfy, web_push

IST = ZoneInfo("Asia/Kolkata")
KINDS = frozenset({"daily", "weekly"})

_DEFAULTS = {
    "daily": {
        "title": "Keep learning with TubeNotes",
        "body": "You still have free videos to explore. Summarize your next YouTube video today.",
        "daily_time": "10:00",
        "weekly_day": 6,
        "cooldown_hours": 24,
    },
    "weekly": {
        "title": "Your TubeNotes weekly reminder",
        "body": "Unlock unlimited summaries and FullNotes PDFs with TubeNotes Pro.",
        "daily_time": "11:00",
        "weekly_day": 6,
        "cooldown_hours": 168,
    },
}


def now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def ensure_campaigns(db: Session) -> dict[str, PushCampaign]:
    """Create the two safe defaults on first use without a startup write."""
    rows = db.execute(select(PushCampaign)).scalars().all()
    by_kind = {row.kind: row for row in rows}
    for kind, defaults in _DEFAULTS.items():
        if kind not in by_kind:
            by_kind[kind] = PushCampaign(kind=kind, url="/?account=1", **defaults)
            db.add(by_kind[kind])
    db.flush()
    return by_kind


def campaign_dict(campaign: PushCampaign) -> dict:
    return {
        "id": campaign.id,
        "kind": campaign.kind,
        "enabled": campaign.enabled,
        "title": campaign.title,
        "body": campaign.body,
        "url": campaign.url,
        "daily_time": campaign.daily_time,
        "weekly_day": campaign.weekly_day,
        "cooldown_hours": campaign.cooldown_hours,
    }


def run_dict(run: PushCampaignRun) -> dict:
    return {
        "id": run.id,
        "campaign_id": run.campaign_id,
        "trigger": run.trigger,
        "status": run.status,
        "title": run.title,
        "body": run.body,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "target_users": run.target_users,
        "target_endpoints": run.target_endpoints,
        "sent_endpoints": run.sent_endpoints,
        "failed_endpoints": run.failed_endpoints,
        "stale_endpoints": run.stale_endpoints,
        "error_message": run.error_message,
    }


def _paid_user_ids(now_value: datetime):
    return select(Subscription.user_id).where(
        Subscription.status.in_((SubscriptionStatus.active, SubscriptionStatus.trialing))
    ).union(
        select(ManualProGrant.user_id).where(
            ManualProGrant.status == "active",
            ManualProGrant.starts_at <= now_value,
            (ManualProGrant.expires_at.is_(None)) | (ManualProGrant.expires_at > now_value),
        )
    )


def eligible_rows(
    db: Session, campaign: PushCampaign, *, at: datetime | None = None
) -> list[WebPushSubscription]:
    """Return live free-user browser endpoints, excluding recently contacted users."""
    at = at or now()
    cutoff = at - timedelta(hours=max(1, campaign.cooldown_hours))
    recently_sent = select(PushCampaignDelivery.user_id).where(
        PushCampaignDelivery.campaign_id == campaign.id,
        PushCampaignDelivery.last_sent_at > cutoff,
    )
    return db.execute(
        select(WebPushSubscription)
        .join(User, WebPushSubscription.user_id == User.id)
        .where(
            User.is_active.is_(True),
            ~User.id.in_(_paid_user_ids(at)),
            ~User.id.in_(recently_sent),
        )
        .order_by(WebPushSubscription.created_at.asc())
    ).scalars().all()


def preview(db: Session, campaign: PushCampaign) -> dict:
    rows = eligible_rows(db, campaign)
    return {
        "eligible_users": len({row.user_id for row in rows}),
        "eligible_endpoints": len(rows),
        "web_push_configured": web_push.configured(),
    }


def queue_run(
    db: Session,
    campaign: PushCampaign,
    *,
    trigger: str,
    trigger_key: str | None = None,
) -> PushCampaignRun | None:
    """Queue a run. Scheduled callers receive ``None`` for a duplicate date."""
    trigger_key = trigger_key or f"manual:{secrets.token_hex(12)}"
    run = PushCampaignRun(
        campaign_id=campaign.id,
        trigger=trigger,
        trigger_key=trigger_key,
        status="queued",
        title=campaign.title,
        body=campaign.body,
    )
    db.add(run)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if trigger == "scheduled":
            return None
        raise
    db.refresh(run)
    return run


def _record_deliveries(
    db: Session,
    campaign: PushCampaign,
    sent_by_user: dict[str, int],
    *,
    at: datetime,
) -> None:
    if not sent_by_user:
        return
    existing = {
        row.user_id: row
        for row in db.execute(
            select(PushCampaignDelivery).where(
                PushCampaignDelivery.campaign_id == campaign.id,
                PushCampaignDelivery.user_id.in_(tuple(sent_by_user)),
            )
        ).scalars()
    }
    for user_id, endpoint_count in sent_by_user.items():
        row = existing.get(user_id)
        if row is None:
            db.add(
                PushCampaignDelivery(
                    campaign_id=campaign.id,
                    user_id=user_id,
                    last_sent_at=at,
                    endpoints_sent=endpoint_count,
                )
            )
        else:
            row.last_sent_at = at
            row.endpoints_sent = endpoint_count


def execute_run(run_id: str) -> dict | None:
    """Run a queued campaign from a background task or the cron CLI."""
    db = SessionLocal()
    try:
        run = db.get(PushCampaignRun, run_id)
        if run is None or run.status != "queued":
            return run_dict(run) if run is not None else None
        campaign = db.get(PushCampaign, run.campaign_id)
        if campaign is None:
            run.status, run.error_message, run.completed_at = "failed", "Campaign not found.", now()
            db.commit()
            return run_dict(run)
        if not campaign.enabled:
            run.status, run.completed_at = "skipped", now()
            db.commit()
            return run_dict(run)
        if not web_push.configured():
            run.status, run.error_message, run.completed_at = "failed", "Web Push is not configured.", now()
            db.commit()
            return run_dict(run)

        started = now()
        run.status, run.started_at = "running", started
        db.commit()
        rows = eligible_rows(db, campaign, at=started)
        run.target_users = len({row.user_id for row in rows})
        run.target_endpoints = len(rows)
        db.commit()

        sent_by_user: dict[str, int] = {}
        batch_size = max(1, settings.WEB_PUSH_CAMPAIGN_BATCH_SIZE)
        for start in range(0, len(rows), batch_size):
            batch = rows[start : start + batch_size]
            result = web_push.deliver_campaign_batch(
                db,
                batch,
                title=run.title,
                body=run.body,
                tag=f"campaign-{campaign.kind}",
                url=campaign.url,
            )
            run.sent_endpoints += len(result.sent_ids)
            run.failed_endpoints += len(result.failed_ids)
            run.stale_endpoints += len(result.stale_ids)
            for row in batch:
                if row.id in result.sent_ids:
                    sent_by_user[row.user_id] = sent_by_user.get(row.user_id, 0) + 1
            db.commit()

        _record_deliveries(db, campaign, sent_by_user, at=started)
        run.status, run.completed_at = "completed", now()
        db.commit()
        ntfy.campaign_completed(
            campaign.kind,
            target_users=run.target_users,
            target_endpoints=run.target_endpoints,
            sent_endpoints=run.sent_endpoints,
            failed_endpoints=run.failed_endpoints,
            stale_endpoints=run.stale_endpoints,
        )
        return run_dict(run)
    except Exception as exc:  # A campaign must leave an operator-visible audit.
        db.rollback()
        run = db.get(PushCampaignRun, run_id)
        if run is not None:
            run.status = "failed"
            run.error_message = str(exc)[:500]
            run.completed_at = now()
            db.commit()
            return run_dict(run)
        return None
    finally:
        db.close()


def due_campaigns(db: Session, *, at: datetime | None = None) -> list[PushCampaign]:
    """Campaigns due in this exact IST minute; intended for a once/minute cron."""
    at = at or now()
    ist = _as_utc(at).astimezone(IST)
    clock = ist.strftime("%H:%M")
    campaigns = ensure_campaigns(db)
    due: list[PushCampaign] = []
    daily = campaigns["daily"]
    if daily.enabled and daily.daily_time == clock:
        due.append(daily)
    weekly = campaigns["weekly"]
    if weekly.enabled and weekly.weekly_day == ist.weekday() and weekly.daily_time == clock:
        due.append(weekly)
    return due


def scheduled_key(campaign: PushCampaign, *, at: datetime) -> str:
    ist_date = _as_utc(at).astimezone(IST).date().isoformat()
    return f"scheduled:{campaign.kind}:{ist_date}"
