"""Admin-only operator and dashboard endpoints.

The web dashboard authenticates with the same short-lived JWT as the main app;
only a database admin or an email listed in ``ADMIN_EMAILS`` can read it.  The
older ``X-Admin-Key`` support remains for server-side maintenance scripts.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.deps import get_admin_user
from app.models import (
    BillingPayment,
    Device,
    DeviceTrialLedger,
    CachedOutput,
    ManualProGrant,
    ProcessingJob,
    PushCampaign,
    PushCampaignRun,
    Subscription,
    SubscriptionStatus,
    UsageEvent,
    User,
    WebhookEvent,
)
from app.schemas import MessageOut, UserOut
from app.services import devices as device_service
from app.services import output_cache
from app.services import push_campaigns
from app.services.payments import get_provider

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(get_admin_user)])
_STARTED_AT = time.monotonic()


class GrantTrialsRequest(BaseModel):
    email: str
    trial_limit: int = Field(ge=0, le=10_000)


class BlockDeviceRequest(BaseModel):
    device_hash: str
    blocked: bool = True
    reason: str | None = None


class ManualProRequest(BaseModel):
    duration_days: int = Field(default=30, ge=1, le=3650)
    lifetime: bool = False
    note: str | None = Field(default=None, max_length=500)


class PushCampaignUpdate(BaseModel):
    enabled: bool
    title: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=500)
    url: str = Field(min_length=1, max_length=500)
    daily_time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    weekly_day: int = Field(ge=0, le=6)
    cooldown_hours: int = Field(ge=1, le=24 * 30)


@router.get("/stats")
def stats(db: Session = Depends(get_db)):
    reporting_start = _reporting_start()
    total_users = db.execute(
        select(func.count()).select_from(User).where(_reportable_user_condition(reporting_start))
    ).scalar_one()
    active_subs = db.execute(
        select(func.count())
        .select_from(Subscription)
        .join(User, Subscription.user_id == User.id)
        .where(
            Subscription.status == SubscriptionStatus.active,
            _reportable_user_condition(reporting_start),
        )
    ).scalar_one()
    total_runs = db.execute(
        select(func.count())
        .select_from(UsageEvent)
        .where(UsageEvent.created_at >= reporting_start)
    ).scalar_one()
    devices = db.execute(
        select(func.count()).select_from(Device).where(Device.last_seen_at >= reporting_start)
    ).scalar_one()
    exhausted = db.execute(
        select(func.count())
        .select_from(DeviceTrialLedger)
        .where(
            DeviceTrialLedger.trials_used >= 5,
            DeviceTrialLedger.first_seen_at >= reporting_start,
        )
    ).scalar_one()
    return {
        "users": total_users,
        "active_subscriptions": active_subs,
        "usage_events": total_runs,
        "devices": devices,
        "devices_with_exhausted_trials": exhausted,
        "mrr_usd": round(active_subs * 5.0, 2),
    }


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _reporting_start() -> datetime:
    """Inclusive lower bound for admin reporting, without deleting records."""
    return _as_utc(settings.ADMIN_REPORTING_START_AT) or datetime.min.replace(tzinfo=timezone.utc)


def _reportable_user_condition(reporting_start: datetime):
    """Users created or active since the admin reporting reset.

    The reset hides historical activity, not people.  An account created before
    the reset must return to the admin view as soon as it logs in, processes a
    video, uses a trial, adds a device, or changes its subscription afterwards.
    """
    return or_(
        User.created_at >= reporting_start,
        User.last_login_at >= reporting_start,
        User.id.in_(select(ProcessingJob.user_id).where(ProcessingJob.started_at >= reporting_start)),
        User.id.in_(select(UsageEvent.user_id).where(UsageEvent.created_at >= reporting_start)),
        User.id.in_(select(Device.user_id).where(Device.last_seen_at >= reporting_start)),
        User.id.in_(
            select(Subscription.user_id).where(
                or_(
                    Subscription.created_at >= reporting_start,
                    Subscription.updated_at >= reporting_start,
                )
            )
        ),
        User.id.in_(
            select(ManualProGrant.user_id).where(
                or_(
                    ManualProGrant.created_at >= reporting_start,
                    ManualProGrant.updated_at >= reporting_start,
                )
            )
        ),
    )


def _window_start(days: int, now: datetime) -> datetime:
    """Selected dashboard range, clamped to the reporting reset point."""
    return max(now - timedelta(days=days - 1), _reporting_start())


def _payment_state(status_value: str | None) -> str:
    """Small, provider-neutral grouping for the operator payment table."""
    status_value = (status_value or "").strip().lower()
    if status_value in {"captured", "paid", "succeeded", "success"}:
        return "successful"
    if status_value in {"failed", "refunded", "reversed"}:
        return "failed"
    return "pending"


def _usage_meta(raw: str | None) -> dict:
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def _job_kind(action: str) -> str:
    if action == "notes_pdf":
        return "PDF"
    if action == "notes":
        return "Full notes"
    if action.startswith("summarize:key_points"):
        return "Key points"
    return "Summary"


def _iso(value: datetime | None) -> str | None:
    """Serialize datetimes consistently without leaking database internals."""
    value = _as_utc(value)
    return value.isoformat() if value is not None else None


def _subscription_status(value: SubscriptionStatus | str) -> str:
    return value.value if isinstance(value, SubscriptionStatus) else str(value)


def _subscription_data(subscription: Subscription | None) -> dict | None:
    """Return only the billing fields an operator needs to support a user.

    Provider customer/subscription ids are deliberately not returned: they are
    payment-provider identifiers, not useful dashboard information.
    """
    if subscription is None:
        return None
    return {
        "provider": subscription.provider,
        "status": _subscription_status(subscription.status),
        "currency": subscription.currency,
        "price_subunits": subscription.price_cents,
        "current_period_end": _iso(subscription.current_period_end),
        "cancel_at_period_end": subscription.cancel_at_period_end,
        "canceled_at": _iso(subscription.canceled_at),
        "created_at": _iso(subscription.created_at),
        "updated_at": _iso(subscription.updated_at),
    }


def _active_manual_pro(grants: list[ManualProGrant], now: datetime | None = None) -> ManualProGrant | None:
    now = now or datetime.now(timezone.utc)
    valid = [
        grant for grant in grants
        if grant.status == "active"
        and (_as_utc(grant.starts_at) or now) <= now
        and (grant.expires_at is None or (_as_utc(grant.expires_at) or now) > now)
    ]
    return max(valid, key=lambda grant: _as_utc(grant.created_at) or _as_utc(grant.starts_at) or now, default=None)


def _manual_pro_data(grant: ManualProGrant | None) -> dict | None:
    if grant is None:
        return None
    return {
        "id": grant.id,
        "status": grant.status,
        "starts_at": _iso(grant.starts_at),
        "expires_at": _iso(grant.expires_at),
        "note": grant.note,
        "granted_by": grant.granted_by_email or "Administrator",
        "granted_at": _iso(grant.created_at),
        "revoked_at": _iso(grant.revoked_at),
        "revoked_by": grant.revoked_by_email,
    }


def _job_data(job: ProcessingJob) -> dict:
    return {
        "id": job.id,
        "video_id": job.video_id,
        "video_url": job.video_url,
        "title": job.title,
        "kind": job.kind,
        "client_source": job.client_source or "web",
        "language": job.language or "Auto",
        "status": job.status,
        "pdf_generated": job.pdf_generated,
        "cached": job.cached,
        "started_at": _iso(job.started_at),
        "finished_at": _iso(job.finished_at),
        "duration_ms": job.duration_ms,
        "output_tokens": job.output_tokens,
        "city": job.request_city or "Unknown",
        "country": job.request_country or "Unknown",
        "error_message": job.error_message,
    }


def _user_counts(user_id: str, jobs: list[ProcessingJob], usage_events: list[UsageEvent], devices: list[Device]) -> dict:
    user_jobs = [job for job in jobs if job.user_id == user_id]
    return {
        # Usage events are the billing ledger: one row is created for each
        # distinct permitted video run, regardless of its final result.
        "videos": sum(1 for event in usage_events if event.user_id == user_id),
        "pdfs": sum(1 for job in user_jobs if job.pdf_generated),
        "translations": sum(1 for job in user_jobs if job.kind == "translation" and job.status == "success"),
        "failed_jobs": sum(1 for job in user_jobs if job.status == "failed"),
        "devices": sum(1 for device in devices if device.user_id == user_id),
        "active_devices": sum(1 for device in devices if device.user_id == user_id and not device.revoked),
    }


def _gpu_status() -> dict:
    """Return NVIDIA telemetry when nvidia-smi is available, otherwise None.

    This uses an argument list (not a shell) and a very short timeout.  It is
    safe on local Windows without NVIDIA tools and on CPU-only deployments.
    """
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,utilization.gpu,temperature.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if result.returncode or not result.stdout.strip():
            return {"available": False}
        fields = [part.strip() for part in result.stdout.splitlines()[0].split(",")]
        if len(fields) != 5:
            return {"available": False}
        return {
            "available": True,
            "name": fields[0],
            "utilization": fields[1],
            "temperature": fields[2],
            "memory_used": fields[3],
            "memory_total": fields[4],
        }
    except (OSError, subprocess.SubprocessError):
        return {"available": False}


@router.get("/session")
def session_info(admin: User | None = Depends(get_admin_user)):
    """Small endpoint used by /admin before the dashboard is rendered."""
    return {
        "email": admin.email if admin is not None else "Server administrator",
        "name": (admin.full_name if admin is not None else None) or "Admin",
    }


@router.get("/settings/status")
async def settings_status(db: Session = Depends(get_db)):
    """Safe configuration/health summary for the administrator settings UI.

    The response intentionally contains booleans and operational values only;
    it never returns secrets, passwords, raw URLs, tokens or payment IDs.
    """
    latest_webhook = db.execute(
        select(WebhookEvent)
        .where(WebhookEvent.provider == settings.PAYMENT_PROVIDER)
        .order_by(WebhookEvent.received_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    try:
        from app.services import summarizer
        vllm = await summarizer.server_load()
    except Exception:  # pragma: no cover - settings must still be usable
        vllm = {"running": None, "waiting": None, "capacity": 0}
    recent_error_count = db.execute(
        select(func.count()).select_from(ProcessingJob).where(
            ProcessingJob.status == "failed",
            ProcessingJob.started_at >= datetime.now(timezone.utc) - timedelta(days=30),
        )
    ).scalar_one()
    return {
        "trials": {
            "default_limit": settings.FREE_TRIAL_LIMIT,
            "device_limit_enabled": settings.ENFORCE_DEVICE_TRIAL_LIMIT,
            "machine_limit_enabled": settings.ENFORCE_MACHINE_TRIAL_LIMIT,
            "machine_limit": settings.MACHINE_TRIAL_LIMIT,
            "max_active_devices": settings.MAX_ACTIVE_DEVICES_PER_USER,
        },
        "billing": {
            "provider": settings.PAYMENT_PROVIDER,
            "checkout_configured": (
                bool(settings.RAZORPAY_KEY_ID and settings.RAZORPAY_KEY_SECRET)
                if settings.PAYMENT_PROVIDER == "razorpay" else settings.PAYMENT_PROVIDER == "mock"
            ),
            "india_plan_configured": bool(settings.RAZORPAY_PLAN_ID_INR),
            "international_plan_configured": bool(settings.RAZORPAY_PLAN_ID_USD),
            "webhook_configured": bool(settings.RAZORPAY_WEBHOOK_SECRET),
            "last_webhook_type": latest_webhook.event_type if latest_webhook else None,
            "last_webhook_at": _iso(latest_webhook.received_at) if latest_webhook else None,
        },
        "model": {
            "model": settings.VLLM_MODEL,
            "max_concurrency": settings.VLLM_MAX_CONCURRENCY,
            "queue_timeout_seconds": settings.VLLM_QUEUE_TIMEOUT_SECONDS,
            "context_tokens": settings.VLLM_MAX_MODEL_LEN,
            "vllm": vllm,
            "gpu": _gpu_status(),
        },
        "maintenance": {
            "output_cache_enabled": settings.OUTPUT_CACHE_ENABLED,
            "output_cache_ttl_days": settings.OUTPUT_CACHE_TTL_DAYS,
            "cache_entries": db.execute(select(func.count()).select_from(CachedOutput)).scalar_one(),
            "failed_jobs_last_30_days": recent_error_count,
        },
    }


@router.get("/dashboard")
async def dashboard(days: int = 10, db: Session = Depends(get_db)):
    """One compact, real-data payload for the dashboard.

    The application historically recorded every permitted video operation as a
    UsageEvent, while generated output is kept in CachedOutput.  We expose
    exactly those persisted facts instead of inventing completion times or
    fake counters.  New data appears after a normal summary/PDF run.
    """
    days = max(1, min(days, 90))
    now = datetime.now(timezone.utc)
    start = _window_start(days, now)
    reporting_start = _reporting_start()
    users = db.execute(
        select(User).where(_reportable_user_condition(reporting_start))
    ).scalars().all()
    usage_rows = db.execute(
        select(UsageEvent, User.email)
        .join(User, UsageEvent.user_id == User.id)
        .where(UsageEvent.created_at >= reporting_start)
        .order_by(UsageEvent.created_at.desc())
        .limit(3000)
    ).all()
    job_rows = db.execute(
        select(ProcessingJob, User.email)
        .join(User, ProcessingJob.user_id == User.id)
        .where(ProcessingJob.started_at >= reporting_start)
        .order_by(ProcessingJob.started_at.desc())
        .limit(3000)
    ).all()
    outputs = db.execute(select(CachedOutput)).scalars().all()
    subscriptions = db.execute(
        select(Subscription)
        .join(User, Subscription.user_id == User.id)
        .where(_reportable_user_condition(reporting_start))
    ).scalars().all()
    manual_grants = db.execute(
        select(ManualProGrant).where(
            ManualProGrant.user_id.in_(select(User.id).where(_reportable_user_condition(reporting_start)))
        )
    ).scalars().all()

    recent_window = [
        (job, email) for job, email in job_rows
        if (_as_utc(job.started_at) or now) >= start
    ]
    today = now.date()
    active_today = {
        job.user_id for job, _email in job_rows
        if (_as_utc(job.started_at) or now).date() == today
    }
    # Statuses are written by verified provider webhooks.  Count the latest
    # subscription state for each account, not every historical/retried row.
    latest_subscriptions: dict[str, Subscription] = {}
    for subscription in subscriptions:
        current = latest_subscriptions.get(subscription.user_id)
        subscription_updated = _as_utc(subscription.updated_at) or _as_utc(subscription.created_at)
        current_updated = (_as_utc(current.updated_at) or _as_utc(current.created_at)) if current else None
        if current is None or (subscription_updated and (current_updated is None or subscription_updated > current_updated)):
            latest_subscriptions[subscription.user_id] = subscription
    current_statuses = {
        user_id: _subscription_status(subscription.status)
        for user_id, subscription in latest_subscriptions.items()
    }
    manual_by_user: dict[str, list[ManualProGrant]] = {}
    for grant in manual_grants:
        manual_by_user.setdefault(grant.user_id, []).append(grant)
    manual_active_ids = {
        user_id for user_id, grants in manual_by_user.items()
        if _active_manual_pro(grants, now) is not None
    }
    paid_users = sum(1 for state in current_statuses.values() if state == "active")
    active_pro_ids = {
        user_id for user_id, state in current_statuses.items()
        if state in {"active", "trialing"}
    } | manual_active_ids
    active_pro_users = len(active_pro_ids)
    payment_issue_users = sum(1 for state in current_statuses.values() if state in {"past_due", "unpaid", "incomplete"})
    cancelled_users = sum(1 for state in current_statuses.values() if state == "canceled")
    notes_outputs = sum(1 for job, _email in recent_window if job.kind == "notes" and job.status == "success")
    summary_outputs = sum(1 for job, _email in recent_window if job.kind in {"summary", "key_points"} and job.status == "success")
    pdf_requests = sum(1 for job, _email in recent_window if job.pdf_generated and job.status == "success")
    translations = sum(1 for job, _email in recent_window if job.kind == "translation" and job.status == "success")
    extension_summaries = sum(
        1 for job, _email in recent_window
        if job.client_source == "extension"
        and job.kind in {"summary", "key_points"}
        and job.status == "success"
    )
    extension_pdfs = sum(
        1 for job, _email in recent_window
        if job.client_source == "extension" and job.pdf_generated and job.status == "success"
    )
    extension_translations = sum(
        1 for job, _email in recent_window
        if job.client_source == "extension"
        and job.kind == "translation"
        and job.status == "success"
    )
    failed_jobs = sum(1 for job, _email in recent_window if job.status == "failed")

    date_keys = [(now - timedelta(days=offset)).date() for offset in range(days - 1, -1, -1)]
    daily_users = {key.isoformat(): set() for key in date_keys}
    daily_jobs = {key.isoformat(): Counter() for key in date_keys}
    for job, _email in recent_window:
        created = _as_utc(job.started_at) or now
        key = created.date().isoformat()
        if key not in daily_users:
            continue
        daily_users[key].add(job.user_id)
        daily_jobs[key][job.status] += 1

    # Country is recorded only from a trusted Cloudflare header. "Unknown" is
    # an honest outcome for earlier/direct-local signups; never infer it from email.
    # Keep this chart aligned with the reportable users in its selected period:
    # an older account appears only after fresh activity following the reset.
    country_users = [
        user for user in users
        if (
            (_as_utc(user.created_at) or datetime.min.replace(tzinfo=timezone.utc)) >= start
            or (_as_utc(user.last_login_at) or datetime.min.replace(tzinfo=timezone.utc)) >= start
            or any(job.user_id == user.id for job, _email in recent_window)
        )
    ]
    country_counts = Counter(
        (user.billing_country or "Unknown").upper() for user in country_users
    )
    if not country_counts:
        country_counts["Unknown"] = 0

    recent_jobs = []
    for job, email in job_rows[:12]:
        recent_jobs.append({
            "email": email,
            "video_id": job.video_id,
            "video_url": job.video_url,
            "title": job.title,
            "kind": job.kind,
            "client_source": job.client_source or "web",
            "language": job.language or "Auto",
            "status": job.status,
            "pdf_generated": job.pdf_generated,
            "cached": job.cached,
            "created_at": (_as_utc(job.started_at) or now).isoformat(),
            "duration_ms": job.duration_ms,
            "output_tokens": job.output_tokens,
            "city": job.request_city or "Unknown",
            "country": job.request_country or "Unknown",
            "error_message": job.error_message,
        })

    try:
        from app.services import summarizer
        vllm = await summarizer.server_load()
    except Exception:  # pragma: no cover - dashboard must remain usable
        vllm = {"running": None, "waiting": None, "kv_cache": None, "capacity": 0, "jobs": 0}

    try:
        location = settings.DATABASE_URL.removeprefix("sqlite:///") if settings.is_sqlite else "."
        disk = shutil.disk_usage(location or ".")
        storage = {"used_gb": round((disk.total - disk.free) / 1024**3, 1), "total_gb": round(disk.total / 1024**3, 1)}
    except OSError:
        storage = {"used_gb": None, "total_gb": None}

    return {
        "range_days": days,
        "generated_at": now.isoformat(),
        "metrics": {
            "total_users": len(users),
            "active_today": len(active_today),
            "video_jobs": len(recent_window),
            "summary_outputs": summary_outputs,
            "full_notes": notes_outputs,
            "pdf_requests": pdf_requests,
            "translations": translations,
            "extension_summaries": extension_summaries,
            "extension_pdfs": extension_pdfs,
            "extension_translations": extension_translations,
            "failed_jobs": failed_jobs,
            "active_subscriptions": active_pro_users,
            "paid_users": paid_users,
            "active_pro_users": active_pro_users,
            "payment_issue_users": payment_issue_users,
            "cancelled_users": cancelled_users,
        },
        "charts": {
            # %-d is not supported by Windows' strftime, so format portably.
            "labels": [key.strftime("%b %d").replace(" 0", " ") for key in date_keys],
            "user_activity": [len(daily_users[key.isoformat()]) for key in date_keys],
            "jobs": {
                "Success": [daily_jobs[key.isoformat()]["success"] for key in date_keys],
                "Failed": [daily_jobs[key.isoformat()]["failed"] for key in date_keys],
                "Processing": [daily_jobs[key.isoformat()]["processing"] for key in date_keys],
            },
            "countries": [{"name": name, "count": count} for name, count in country_counts.most_common(6)],
            "country_total": len(country_users),
        },
        "recent_jobs": recent_jobs,
        "top_users": [
            {
                "email": email,
                "videos": sum(1 for job, row_email in job_rows if row_email == email and job.kind in {"summary", "key_points", "notes"}),
                "pdfs": sum(1 for job, row_email in job_rows if row_email == email and job.pdf_generated),
                "translations": sum(1 for job, row_email in job_rows if row_email == email and job.kind == "translation" and job.status == "success"),
            }
            for email, _count in Counter(email for _job, email in job_rows).most_common(5)
        ],
        "top_videos": [
            {
                "video_id": video_id,
                "title": next((job.title for job, _email in job_rows if job.video_id == video_id and job.title), None),
                "runs": len([job for job, _email in job_rows if job.video_id == video_id]),
                "success_rate": round(
                    100 * sum(1 for job, _email in job_rows if job.video_id == video_id and job.status == "success") /
                    max(1, sum(1 for job, _email in job_rows if job.video_id == video_id)), 1
                ),
            }
            for video_id, _count in Counter(job.video_id for job, _email in job_rows if job.video_id).most_common(5)
        ],
        "recent_errors": [
            {
                "email": email,
                "message": job.error_message or "Processing failed",
                "created_at": (_as_utc(job.finished_at) or _as_utc(job.started_at) or now).isoformat(),
            }
            for job, email in job_rows if job.status == "failed"
        ][:5],
        "health": {
            "vllm": vllm,
            "gpu": _gpu_status(),
            "storage": storage,
            "uptime_seconds": round(time.monotonic() - _STARTED_AT),
            "database": "connected",
        },
    }


@router.get("/users", response_model=list[UserOut])
def list_users(limit: int = 50, offset: int = 0, db: Session = Depends(get_db)):
    rows = db.execute(
        select(User)
        .where(_reportable_user_condition(_reporting_start()))
        .order_by(User.created_at.desc())
        .limit(min(limit, 200)).offset(offset)
    ).scalars().all()
    return [UserOut.model_validate(u) for u in rows]


@router.get("/users/overview")
def users_overview(
    query: str = "",
    status: str = "all",
    limit: int = 100,
    db: Session = Depends(get_db),
):
    """Operator-safe user directory used by the Users screen.

    This intentionally returns support information rather than credentials,
    IP addresses, device fingerprints or payment-provider identifiers.
    """
    limit = max(1, min(limit, 200))
    needle = query.strip().lower()
    requested_status = status.strip().lower()
    if requested_status not in {"all", "active_today", "paid", "subscribed", "free", "payment_issue", "cancelled", "disabled"}:
        raise HTTPException(status_code=422, detail="Unsupported user status filter")

    reporting_start = _reporting_start()
    users = db.execute(
        select(User)
        .where(_reportable_user_condition(reporting_start))
        .order_by(User.created_at.desc())
    ).scalars().all()
    jobs = db.execute(
        select(ProcessingJob).where(ProcessingJob.started_at >= reporting_start)
    ).scalars().all()
    usage_events = db.execute(
        select(UsageEvent).where(UsageEvent.created_at >= reporting_start)
    ).scalars().all()
    devices = db.execute(
        select(Device).where(Device.last_seen_at >= reporting_start)
    ).scalars().all()
    subscriptions = db.execute(
        select(Subscription).where(
            Subscription.user_id.in_(select(User.id).where(_reportable_user_condition(reporting_start)))
        )
    ).scalars().all()
    manual_grants = db.execute(
        select(ManualProGrant).where(
            ManualProGrant.user_id.in_(select(User.id).where(_reportable_user_condition(reporting_start)))
        )
    ).scalars().all()
    subscriptions_by_user: dict[str, list[Subscription]] = {}
    for subscription in subscriptions:
        subscriptions_by_user.setdefault(subscription.user_id, []).append(subscription)
    manual_by_user: dict[str, list[ManualProGrant]] = {}
    for grant in manual_grants:
        manual_by_user.setdefault(grant.user_id, []).append(grant)
    today = datetime.now(timezone.utc).date()
    active_today_ids = {
        job.user_id for job in jobs
        if (_as_utc(job.started_at) or datetime.min.replace(tzinfo=timezone.utc)).date() == today
    }

    rows = []
    for user in users:
        if needle and needle not in " ".join(filter(None, [user.email, user.full_name, user.billing_country])).lower():
            continue
        user_subscriptions = sorted(
            subscriptions_by_user.get(user.id, []),
            key=lambda item: _as_utc(item.updated_at) or _as_utc(item.created_at) or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        current = user_subscriptions[0] if user_subscriptions else None
        current_status = _subscription_status(current.status) if current is not None else ""
        active = current if current_status in {"active", "trialing"} else None
        manual_pro = _active_manual_pro(manual_by_user.get(user.id, []))
        if requested_status == "paid" and current_status != "active":
            continue
        if requested_status == "subscribed" and active is None and manual_pro is None:
            continue
        if requested_status == "free" and (current is not None or manual_pro is not None or not user.is_active):
            continue
        if requested_status == "payment_issue" and current_status not in {"past_due", "unpaid", "incomplete"}:
            continue
        if requested_status == "cancelled" and current_status != "canceled":
            continue
        if requested_status == "disabled" and user.is_active:
            continue
        if requested_status == "active_today" and user.id not in active_today_ids:
            continue
        trial_limit = user.trial_limit_override if user.trial_limit_override is not None else settings.FREE_TRIAL_LIMIT
        rows.append({
            "id": user.id,
            "email": user.email,
            "full_name": user.full_name,
            "is_active": user.is_active,
            "email_verified": user.email_verified,
            # Existing accounts have no reliable location record; unknown is
            # preferable to guessing from email address or a historic IP.
            "country": (user.billing_country or "Unknown").upper(),
            "signup_at": _iso(user.created_at),
            "last_login_at": _iso(user.last_login_at),
            "trials_used": user.trials_used,
            "trial_limit": trial_limit,
            "subscription": _subscription_data(active or current),
            "manual_pro": _manual_pro_data(manual_pro),
            "counts": _user_counts(user.id, jobs, usage_events, devices),
        })
        if len(rows) >= limit:
            break
    return {"users": rows, "total": len(rows)}


@router.get("/billing-history")
def billing_history(
    days: int = 10,
    query: str = "",
    status: str = "all",
    limit: int = 500,
    db: Session = Depends(get_db),
):
    """Admin-only payment ledger, scoped to the configured reporting period."""
    days = max(1, min(days, 90))
    limit = max(1, min(limit, 1_000))
    status_filter = status.strip().lower()
    if status_filter not in {"all", "successful", "failed", "pending"}:
        raise HTTPException(status_code=422, detail="Unsupported payment status filter")

    start = _window_start(days, datetime.now(timezone.utc))
    conditions = [
        func.coalesce(BillingPayment.paid_at, BillingPayment.created_at) >= start,
    ]
    if status_filter == "successful":
        conditions.append(BillingPayment.status.in_({"captured", "paid", "succeeded", "success"}))
    elif status_filter == "failed":
        conditions.append(BillingPayment.status.in_({"failed", "refunded", "reversed"}))
    elif status_filter == "pending":
        conditions.append(BillingPayment.status.not_in({"captured", "paid", "succeeded", "success", "failed", "refunded", "reversed"}))

    needle = query.strip().lower()
    if needle:
        pattern = f"%{needle}%"
        conditions.append(
            func.lower(User.email).like(pattern)
            | func.lower(func.coalesce(BillingPayment.provider_payment_id, "")).like(pattern)
            | func.lower(func.coalesce(BillingPayment.provider_invoice_id, "")).like(pattern)
            | func.lower(BillingPayment.provider).like(pattern)
        )

    rows = db.execute(
        select(BillingPayment, User.email)
        .join(User, BillingPayment.user_id == User.id)
        .where(*conditions)
        .order_by(BillingPayment.paid_at.desc(), BillingPayment.created_at.desc())
        .limit(limit)
    ).all()

    # Razorpay can publish the payment before its customer-facing invoice URL
    # is ready.  An admin refresh also completes that link, so support does
    # not need the customer to open their Account page first.
    try:
        provider = get_provider()
        fetch_invoice_url = getattr(provider, "fetch_invoice_url", None)
        changed = False
        if fetch_invoice_url:
            for payment, _email in rows:
                if payment.provider == provider.name and payment.provider_invoice_id and not payment.invoice_url:
                    url = fetch_invoice_url(payment.provider_invoice_id)
                    if url:
                        payment.invoice_url = url
                        changed = True
        if changed:
            db.commit()
    except Exception:
        db.rollback()

    counts = Counter(_payment_state(payment.status) for payment, _email in rows)
    paid_by_currency: dict[str, int] = {}
    for payment, _email in rows:
        if _payment_state(payment.status) != "successful" or payment.amount_subunits is None:
            continue
        currency = (payment.currency or "Unknown").upper()
        paid_by_currency[currency] = paid_by_currency.get(currency, 0) + payment.amount_subunits

    return {
        "range_days": days,
        "total_payments": len(rows),
        "successful_payments": counts["successful"],
        "failed_payments": counts["failed"],
        "pending_payments": counts["pending"],
        "paid_by_currency": paid_by_currency,
        "payments": [
            {
                "id": payment.id,
                "email": email,
                "provider": payment.provider,
                "payment_id": payment.provider_payment_id,
                "invoice_id": payment.provider_invoice_id,
                "status": payment.status,
                "state": _payment_state(payment.status),
                "amount_subunits": payment.amount_subunits,
                "currency": payment.currency,
                "paid_at": _iso(payment.paid_at),
                "created_at": _iso(payment.created_at),
                "failure_message": payment.failure_message,
                "invoice_url": payment.invoice_url,
            }
            for payment, email in rows
        ],
    }


@router.get("/users/{user_id}/detail")
def user_detail(user_id: str, db: Session = Depends(get_db)):
    """Full support profile for one user, without returning sensitive data."""
    user = db.get(User, user_id)
    reporting_start = _reporting_start()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    all_jobs = db.execute(
        select(ProcessingJob)
        .where(ProcessingJob.user_id == user.id, ProcessingJob.started_at >= reporting_start)
        .order_by(ProcessingJob.started_at.desc())
    ).scalars().all()
    # The drawer stays fast/readable while its counters remain exact even for
    # a heavy user with more than one hundred historical operations.
    jobs = all_jobs[:100]
    usage_events = db.execute(
        select(UsageEvent).where(
            UsageEvent.user_id == user.id, UsageEvent.created_at >= reporting_start
        )
    ).scalars().all()
    devices = db.execute(
        select(Device)
        .where(Device.user_id == user.id, Device.last_seen_at >= reporting_start)
        .order_by(Device.last_seen_at.desc())
    ).scalars().all()
    subscriptions = db.execute(
        select(Subscription)
        .where(Subscription.user_id == user.id)
        .order_by(Subscription.updated_at.desc())
    ).scalars().all()
    manual_grants = db.execute(
        select(ManualProGrant)
        .where(ManualProGrant.user_id == user.id)
        .order_by(ManualProGrant.created_at.desc())
    ).scalars().all()
    active = subscriptions[0] if subscriptions and _subscription_status(subscriptions[0].status) in {"active", "trialing"} else None
    manual_pro = _active_manual_pro(manual_grants)
    trial_limit = user.trial_limit_override if user.trial_limit_override is not None else settings.FREE_TRIAL_LIMIT
    return {
        "user": {
            "id": user.id,
            "email": user.email,
            "full_name": user.full_name,
            "auth_provider": user.auth_provider or "password",
            "is_active": user.is_active,
            "email_verified": user.email_verified,
            "country": (user.billing_country or "Unknown").upper(),
            "signup_at": _iso(user.created_at),
            "last_login_at": _iso(user.last_login_at),
            "trials_used": user.trials_used,
            "trial_limit": trial_limit,
            "subscription": _subscription_data(active or (subscriptions[0] if subscriptions else None)),
            "manual_pro": _manual_pro_data(manual_pro),
            "counts": _user_counts(user.id, all_jobs, usage_events, devices),
        },
        "subscriptions": [_subscription_data(item) for item in subscriptions],
        "manual_pro_history": [_manual_pro_data(item) for item in manual_grants],
        "devices": [{
            "label": item.label or "Unnamed device",
            "platform": item.platform or "Unknown platform",
            "extension_version": item.extension_version,
            "revoked": item.revoked,
            "created_at": _iso(item.created_at),
            "last_seen_at": _iso(item.last_seen_at),
        } for item in devices],
        "jobs": [_job_data(item) for item in jobs],
        "errors": [_job_data(item) for item in jobs if item.status == "failed"],
    }


@router.get("/operations")
def operations_overview(
    category: str,
    days: int = 10,
    limit: int = 500,
    db: Session = Depends(get_db),
):
    """Filtered operational history for the Video Jobs/PDF/Translations tabs."""
    category = category.strip().lower()
    if category not in {"recent", "jobs", "successful_summaries", "pdf", "translations", "completed_translations", "extension_summaries", "extension_pdf", "extension_translations"}:
        raise HTTPException(status_code=422, detail="Unsupported operation category")
    days = max(1, min(days, 90))
    limit = max(1, min(limit, 1_000))
    start = _window_start(days, datetime.now(timezone.utc))
    if category == "recent":
        conditions = (ProcessingJob.started_at >= start,)
    elif category == "jobs":
        operation_filter = ProcessingJob.kind.in_({"summary", "key_points"})
        conditions = (ProcessingJob.started_at >= start, operation_filter)
    elif category == "successful_summaries":
        conditions = (
            ProcessingJob.started_at >= start,
            ProcessingJob.kind.in_({"summary", "key_points"}),
            ProcessingJob.status == "success",
        )
    elif category == "pdf":
        operation_filter = ProcessingJob.pdf_generated.is_(True)
        conditions = (ProcessingJob.started_at >= start, operation_filter)
    elif category == "completed_translations":
        conditions = (
            ProcessingJob.started_at >= start,
            ProcessingJob.kind == "translation",
            ProcessingJob.status == "success",
        )
    elif category == "extension_summaries":
        conditions = (
            ProcessingJob.started_at >= start,
            ProcessingJob.client_source == "extension",
            ProcessingJob.kind.in_({"summary", "key_points"}),
            ProcessingJob.status == "success",
        )
    elif category == "extension_pdf":
        conditions = (
            ProcessingJob.started_at >= start,
            ProcessingJob.client_source == "extension",
            ProcessingJob.pdf_generated.is_(True),
            ProcessingJob.status == "success",
        )
    elif category == "extension_translations":
        conditions = (
            ProcessingJob.started_at >= start,
            ProcessingJob.client_source == "extension",
            ProcessingJob.kind == "translation",
            ProcessingJob.status == "success",
        )
    else:
        operation_filter = ProcessingJob.kind == "translation"
        conditions = (ProcessingJob.started_at >= start, operation_filter)
    rows = db.execute(
        select(ProcessingJob, User.email)
        .join(User, ProcessingJob.user_id == User.id)
        .where(*conditions)
        .order_by(ProcessingJob.started_at.desc())
        .limit(limit)
    ).all()
    statuses = dict(db.execute(
        select(ProcessingJob.status, func.count())
        .where(*conditions)
        .group_by(ProcessingJob.status)
    ).all())
    total_jobs = sum(statuses.values())
    unique_users = db.execute(
        select(func.count(func.distinct(ProcessingJob.user_id))).where(*conditions)
    ).scalar_one()
    return {
        "category": category,
        "range_days": days,
        "total_jobs": total_jobs,
        "unique_users": unique_users,
        "success": statuses.get("success", 0),
        "failed": statuses.get("failed", 0),
        "processing": statuses.get("processing", 0),
        "operations": [{**_job_data(job), "email": email} for job, email in rows],
    }


@router.get("/errors")
def error_logs(
    days: int = 10,
    limit: int = 500,
    db: Session = Depends(get_db),
):
    """Full failed-job history for the Error Logs view."""
    days = max(1, min(days, 90))
    limit = max(1, min(limit, 1_000))
    start = _window_start(days, datetime.now(timezone.utc))
    conditions = (ProcessingJob.status == "failed", ProcessingJob.started_at >= start)
    rows = db.execute(
        select(ProcessingJob, User.email)
        .join(User, ProcessingJob.user_id == User.id)
        .where(*conditions)
        .order_by(ProcessingJob.started_at.desc())
        .limit(limit)
    ).all()
    total_errors = db.execute(
        select(func.count()).select_from(ProcessingJob).where(*conditions)
    ).scalar_one()
    unique_users = db.execute(
        select(func.count(func.distinct(ProcessingJob.user_id))).where(*conditions)
    ).scalar_one()
    return {
        "range_days": days,
        "total_errors": total_errors,
        "unique_users": unique_users,
        "errors": [{**_job_data(job), "email": email} for job, email in rows],
    }


@router.post("/grant-trials", response_model=UserOut)
def grant_trials(payload: GrantTrialsRequest, db: Session = Depends(get_db)):
    user = db.execute(
        select(User).where(User.email == payload.email.lower())
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    user.trial_limit_override = payload.trial_limit
    db.commit()
    db.refresh(user)
    return UserOut.model_validate(user)


@router.post("/users/{user_id}/manual-pro")
def grant_manual_pro(
    user_id: str,
    payload: ManualProRequest,
    admin: User | None = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Grant or extend complimentary Pro without forging a provider payment."""
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    now = datetime.now(timezone.utc)
    active_rows = db.execute(
        select(ManualProGrant).where(
            ManualProGrant.user_id == user.id,
            ManualProGrant.status == "active",
        )
    ).scalars().all()
    current = _active_manual_pro(active_rows, now)
    if current is not None:
        current.status = "superseded"
        current.revoked_at = now
        current.revoked_by_user_id = admin.id if admin else None
        current.revoked_by_email = admin.email if admin else "Server administrator"

    if payload.lifetime:
        expires_at = None
    else:
        base = now
        if current is not None and current.expires_at is not None:
            base = max(base, _as_utc(current.expires_at) or now)
        expires_at = base + timedelta(days=payload.duration_days)
    grant = ManualProGrant(
        user_id=user.id,
        status="active",
        starts_at=now,
        expires_at=expires_at,
        note=(payload.note or "").strip() or None,
        granted_by_user_id=admin.id if admin else None,
        granted_by_email=admin.email if admin else "Server administrator",
    )
    db.add(grant)
    db.commit()
    db.refresh(grant)
    return {"detail": "Complimentary Pro access granted", "manual_pro": _manual_pro_data(grant)}


@router.delete("/users/{user_id}/manual-pro", response_model=MessageOut)
def revoke_manual_pro(
    user_id: str,
    admin: User | None = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    now = datetime.now(timezone.utc)
    rows = db.execute(
        select(ManualProGrant).where(
            ManualProGrant.user_id == user.id,
            ManualProGrant.status == "active",
        )
    ).scalars().all()
    active = _active_manual_pro(rows, now)
    if active is None:
        raise HTTPException(status_code=404, detail="No active complimentary Pro access")
    active.status = "revoked"
    active.revoked_at = now
    active.revoked_by_user_id = admin.id if admin else None
    active.revoked_by_email = admin.email if admin else "Server administrator"
    db.commit()
    return MessageOut(detail="Complimentary Pro access revoked")


# ---------------------------------------------------------------------------
# Browser push campaigns
# ---------------------------------------------------------------------------
def _campaign_or_404(db: Session, kind: str) -> PushCampaign:
    if kind not in push_campaigns.KINDS:
        raise HTTPException(status_code=404, detail="Campaign not found")
    campaigns = push_campaigns.ensure_campaigns(db)
    db.commit()
    return campaigns[kind]


@router.get("/push-campaigns")
def list_push_campaigns(db: Session = Depends(get_db)):
    """Daily/weekly free-user campaign configuration plus a live audience preview."""
    campaigns = push_campaigns.ensure_campaigns(db)
    db.commit()
    runs = db.execute(
        select(PushCampaignRun)
        .order_by(PushCampaignRun.started_at.desc(), PushCampaignRun.id.desc())
        .limit(30)
    ).scalars().all()
    return {
        "campaigns": [
            {**push_campaigns.campaign_dict(campaign), "preview": push_campaigns.preview(db, campaign)}
            for campaign in (campaigns["daily"], campaigns["weekly"])
        ],
        "runs": [push_campaigns.run_dict(run) for run in runs],
        "timezone": "Asia/Kolkata",
    }


@router.patch("/push-campaigns/{kind}")
def update_push_campaign(
    kind: str, payload: PushCampaignUpdate, db: Session = Depends(get_db)
):
    campaign = _campaign_or_404(db, kind)
    url = payload.url.strip()
    # Campaign clicks must remain on TubeNotes.  This prevents an admin UI
    # typo from turning a trusted notification into an external redirect.
    if not url.startswith("/") or url.startswith("//"):
        raise HTTPException(status_code=422, detail="Campaign URL must be a TubeNotes path starting with '/'.")
    campaign.enabled = payload.enabled
    campaign.title = payload.title.strip()
    campaign.body = payload.body.strip()
    campaign.url = url
    campaign.daily_time = payload.daily_time
    campaign.weekly_day = payload.weekly_day
    campaign.cooldown_hours = payload.cooldown_hours
    db.commit()
    db.refresh(campaign)
    return {**push_campaigns.campaign_dict(campaign), "preview": push_campaigns.preview(db, campaign)}


@router.post("/push-campaigns/{kind}/preview")
def preview_push_campaign(kind: str, db: Session = Depends(get_db)):
    campaign = _campaign_or_404(db, kind)
    return push_campaigns.preview(db, campaign)


@router.post("/push-campaigns/{kind}/run")
def run_push_campaign(
    kind: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Queue a manual send; the browser polls campaign history for its result."""
    campaign = _campaign_or_404(db, kind)
    if not campaign.enabled:
        raise HTTPException(status_code=409, detail="Enable this campaign before running it.")
    run = push_campaigns.queue_run(db, campaign, trigger="manual")
    if run is None:  # defensive; manual keys are random and never collide
        raise HTTPException(status_code=409, detail="Campaign is already queued.")
    background_tasks.add_task(push_campaigns.execute_run, run.id)
    return {"detail": "Campaign queued", "run": push_campaigns.run_dict(run)}


@router.post("/block-device", response_model=MessageOut)
def block_device(payload: BlockDeviceRequest, db: Session = Depends(get_db)):
    ledger = device_service.get_ledger(db, payload.device_hash)
    ledger.blocked = payload.blocked
    ledger.block_reason = payload.reason
    db.commit()
    return MessageOut(detail="updated")


@router.post("/reset-device-trials", response_model=MessageOut)
def reset_device_trials(payload: BlockDeviceRequest, db: Session = Depends(get_db)):
    ledger = device_service.get_ledger(db, payload.device_hash)
    ledger.trials_used = 0
    db.commit()
    return MessageOut(detail="device trial counter reset")


class PurgeResponse(BaseModel):
    removed: int
    older_than_days: int


@router.post("/cache/purge", response_model=PurgeResponse)
def purge_cache(older_than_days: int | None = None, db: Session = Depends(get_db)):
    """Drop shared summaries nobody has opened in a long time.

    OUTPUT_CACHE_TTL_DAYS decides the cut-off; pass older_than_days to override
    it for one call. Without this the cache only ever grew - the TTL was
    configured and then never acted on by anything.
    """
    days = older_than_days if older_than_days is not None else settings.OUTPUT_CACHE_TTL_DAYS
    removed = output_cache.purge_stale(db, days)
    return PurgeResponse(removed=removed, older_than_days=days)
