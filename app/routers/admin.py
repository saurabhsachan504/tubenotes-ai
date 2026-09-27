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

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.deps import get_admin_user
from app.models import (
    Device,
    DeviceTrialLedger,
    CachedOutput,
    ProcessingJob,
    Subscription,
    SubscriptionStatus,
    UsageEvent,
    User,
    WebhookEvent,
)
from app.schemas import MessageOut, UserOut
from app.services import devices as device_service
from app.services import output_cache

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(get_admin_user)])
_STARTED_AT = time.monotonic()


class GrantTrialsRequest(BaseModel):
    email: str
    trial_limit: int = Field(ge=0, le=10_000)


class BlockDeviceRequest(BaseModel):
    device_hash: str
    blocked: bool = True
    reason: str | None = None


@router.get("/stats")
def stats(db: Session = Depends(get_db)):
    reporting_start = _reporting_start()
    total_users = db.execute(
        select(func.count()).select_from(User).where(User.created_at >= reporting_start)
    ).scalar_one()
    active_subs = db.execute(
        select(func.count())
        .select_from(Subscription)
        .where(
            Subscription.status == SubscriptionStatus.active,
            Subscription.created_at >= reporting_start,
        )
    ).scalar_one()
    total_runs = db.execute(
        select(func.count()).select_from(UsageEvent).where(UsageEvent.created_at >= reporting_start)
    ).scalar_one()
    devices = db.execute(
        select(func.count()).select_from(Device).where(Device.created_at >= reporting_start)
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


def _window_start(days: int, now: datetime) -> datetime:
    """Selected dashboard range, clamped to the reporting reset point."""
    return max(now - timedelta(days=days - 1), _reporting_start())


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


def _job_data(job: ProcessingJob) -> dict:
    return {
        "id": job.id,
        "video_id": job.video_id,
        "video_url": job.video_url,
        "title": job.title,
        "kind": job.kind,
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
            "max_free_devices": settings.MAX_DEVICES_PER_FREE_USER,
            "max_paid_devices": settings.MAX_DEVICES_PER_PAID_USER,
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
        select(User).where(User.created_at >= reporting_start)
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
        select(Subscription).where(Subscription.created_at >= reporting_start)
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
    paid_users = sum(1 for state in current_statuses.values() if state == "active")
    active_pro_users = sum(1 for state in current_statuses.values() if state in {"active", "trialing"})
    payment_issue_users = sum(1 for state in current_statuses.values() if state in {"past_due", "unpaid", "incomplete"})
    cancelled_users = sum(1 for state in current_statuses.values() if state == "canceled")
    notes_outputs = sum(1 for job, _email in recent_window if job.kind == "notes" and job.status == "success")
    summary_outputs = sum(1 for job, _email in recent_window if job.kind in {"summary", "key_points"} and job.status == "success")
    pdf_requests = sum(1 for job, _email in recent_window if job.pdf_generated and job.status == "success")
    translations = sum(1 for job, _email in recent_window if job.kind == "translation" and job.status == "success")
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
    # Keep this chart aligned with its selected date range: it represents
    # accounts that signed up in that period, not an unrelated all-time total.
    country_users = [
        user for user in users
        if (_as_utc(user.created_at) or now) >= start
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
        .where(User.created_at >= _reporting_start())
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
        select(User).where(User.created_at >= reporting_start).order_by(User.created_at.desc())
    ).scalars().all()
    jobs = db.execute(
        select(ProcessingJob).where(ProcessingJob.started_at >= reporting_start)
    ).scalars().all()
    usage_events = db.execute(
        select(UsageEvent).where(UsageEvent.created_at >= reporting_start)
    ).scalars().all()
    devices = db.execute(
        select(Device).where(Device.created_at >= reporting_start)
    ).scalars().all()
    subscriptions = db.execute(
        select(Subscription).where(Subscription.created_at >= reporting_start)
    ).scalars().all()
    subscriptions_by_user: dict[str, list[Subscription]] = {}
    for subscription in subscriptions:
        subscriptions_by_user.setdefault(subscription.user_id, []).append(subscription)
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
        if requested_status == "paid" and current_status != "active":
            continue
        if requested_status == "subscribed" and active is None:
            continue
        if requested_status == "free" and (current is not None or not user.is_active):
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
            "counts": _user_counts(user.id, jobs, usage_events, devices),
        })
        if len(rows) >= limit:
            break
    return {"users": rows, "total": len(rows)}


@router.get("/users/{user_id}/detail")
def user_detail(user_id: str, db: Session = Depends(get_db)):
    """Full support profile for one user, without returning sensitive data."""
    user = db.get(User, user_id)
    reporting_start = _reporting_start()
    if user is None or (_as_utc(user.created_at) or datetime.min.replace(tzinfo=timezone.utc)) < reporting_start:
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
        .where(Device.user_id == user.id, Device.created_at >= reporting_start)
        .order_by(Device.last_seen_at.desc())
    ).scalars().all()
    subscriptions = db.execute(
        select(Subscription)
        .where(Subscription.user_id == user.id, Subscription.created_at >= reporting_start)
        .order_by(Subscription.updated_at.desc())
    ).scalars().all()
    active = subscriptions[0] if subscriptions and _subscription_status(subscriptions[0].status) in {"active", "trialing"} else None
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
            "counts": _user_counts(user.id, all_jobs, usage_events, devices),
        },
        "subscriptions": [_subscription_data(item) for item in subscriptions],
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
    if category not in {"recent", "jobs", "successful_summaries", "pdf", "translations", "completed_translations"}:
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
