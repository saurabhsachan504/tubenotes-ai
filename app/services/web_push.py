"""Best-effort customer browser Web Push delivery."""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime

from pywebpush import WebPushException, webpush
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import User, WebPushSubscription

logger = logging.getLogger("trialguard.web_push")

# These first-party static assets make the compact and expanded Android views
# feel like a TubeNotes notification instead of a generic browser message.
_ICON_URL = "/static/push-icon-v1.png"
_BANNER_URL = "/static/push-banner-v1.png"


def configured() -> bool:
    return settings.web_push_configured


def _send_status(subscription: WebPushSubscription, payload: dict) -> str:
    """Return ``sent``, ``stale`` or ``failed`` for one encrypted payload."""
    try:
        webpush(
            subscription_info={
                "endpoint": subscription.endpoint,
                "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
            },
            data=json.dumps(payload, separators=(",", ":")),
            vapid_private_key=settings.WEB_PUSH_VAPID_PRIVATE_KEY.strip(),
            vapid_claims={"sub": settings.WEB_PUSH_VAPID_CONTACT.strip()},
            ttl=max(0, settings.WEB_PUSH_TTL_SECONDS),
            timeout=max(1.0, settings.WEB_PUSH_TIMEOUT_SECONDS),
        )
        return "sent"
    except WebPushException as exc:
        response = getattr(exc, "response", None)
        if getattr(response, "status_code", None) in {404, 410}:
            logger.info("removing expired Web Push endpoint for user %s", subscription.user_id)
            return "stale"
        logger.warning("Web Push delivery failed for user %s: %s", subscription.user_id, exc)
    except Exception:
        logger.warning("Web Push delivery failed for user %s", subscription.user_id, exc_info=True)
    return "failed"


def _send(subscription: WebPushSubscription, payload: dict) -> bool:
    """Compatibility helper for event notifications.

    Existing callers use a bool where ``False`` means a definitely-expired
    endpoint. A transient delivery failure is retained for a later account
    event, while campaigns receive the full three-state result below.
    """
    return _send_status(subscription, payload) != "stale"


def _payload(
    *, title: str, body: str, tag: str, url: str = "/", actions: list[dict[str, str]] | None = None,
) -> dict:
    return {
        "title": title[:120],
        "body": body[:500],
        "tag": tag[:80],
        "url": url,
        "icon": _ICON_URL,
        "badge": _ICON_URL,
        "image": _BANNER_URL,
        "actions": actions or [{"action": "open", "title": "Open TubeNotes"}],
    }


@dataclass(frozen=True)
class BulkDeliveryResult:
    sent_ids: frozenset[str]
    stale_ids: frozenset[str]
    failed_ids: frozenset[str]


def deliver_campaign_batch(
    db: Session,
    subscriptions: list[WebPushSubscription],
    *,
    title: str,
    body: str,
    tag: str,
    url: str,
) -> BulkDeliveryResult:
    """Send a campaign batch concurrently and remove only confirmed stale rows."""
    if not configured() or not subscriptions:
        return BulkDeliveryResult(frozenset(), frozenset(), frozenset())
    payload = _payload(title=title, body=body, tag=tag, url=url)
    workers = max(1, min(settings.WEB_PUSH_CAMPAIGN_MAX_WORKERS, len(subscriptions)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="web-push") as pool:
        outcomes = list(pool.map(lambda row: (row.id, _send_status(row, payload)), subscriptions))
    sent = frozenset(row_id for row_id, state in outcomes if state == "sent")
    stale = frozenset(row_id for row_id, state in outcomes if state == "stale")
    failed = frozenset(row_id for row_id, state in outcomes if state == "failed")
    if stale:
        db.query(WebPushSubscription).filter(WebPushSubscription.id.in_(stale)).delete(
            synchronize_session=False
        )
        db.commit()
    return BulkDeliveryResult(sent, stale, failed)


def notify_user(
    db: Session,
    user: User,
    *,
    title: str,
    body: str,
    tag: str,
    url: str = "/",
    actions: list[dict[str, str]] | None = None,
) -> int:
    """Deliver an account event to all browser endpoints that user allowed."""
    if not configured():
        return 0
    rows = db.execute(
        select(WebPushSubscription).where(WebPushSubscription.user_id == user.id)
    ).scalars().all()
    payload = _payload(title=title, body=body, tag=tag, url=url, actions=actions)
    delivered, stale = 0, []
    for row in rows:
        if _send(row, payload):
            delivered += 1
        else:
            stale.append(row)
    for row in stale:
        db.delete(row)
    if stale:
        db.commit()
    return delivered


def welcome(db: Session, user: User, trial_limit: int) -> int:
    name = (user.full_name or "there").strip() or "there"
    return notify_user(db, user, title="Welcome to TubeNotes", body=f"Hi {name}, your account is ready with {trial_limit} free videos.", tag="signup-welcome")


def trial_milestone(db: Session, user: User, trials_remaining: int) -> int:
    if trials_remaining == 0:
        return notify_user(
            db, user, title="Your free trial is complete",
            body="You've used all free videos. Subscribe to continue summarizing.",
            tag="trial-0", url="/?account=1",
            actions=[
                {"action": "subscribe", "title": "Subscribe now"},
                {"action": "open", "title": "Open TubeNotes"},
            ],
        )
    return notify_user(db, user, title=f"{trials_remaining} free videos left", body="Keep exploring TubeNotes before your free trial ends.", tag=f"trial-{trials_remaining}")


def payment_received(db: Session, user: User, *, amount_subunits: int | None, currency: str | None, renewal: bool) -> int:
    amount = f" ({currency.upper()} {amount_subunits / 100:,.2f})" if amount_subunits is not None and currency else ""
    return notify_user(db, user, title="TubeNotes Pro renewed" if renewal else "Welcome to TubeNotes Pro", body=("Your renewal payment was successful" if renewal else "Your subscription is now active") + amount + ".", tag="subscription-renewal" if renewal else "subscription-started")


def payment_attention(db: Session, user: User) -> int:
    return notify_user(
        db, user, title="Payment needs attention",
        body="We couldn't complete your subscription payment. Please check your payment method.",
        tag="payment-attention", url="/?account=1",
        actions=[
            {"action": "subscribe", "title": "Update payment"},
            {"action": "open", "title": "Open TubeNotes"},
        ],
    )


def cancellation_scheduled(db: Session, user: User, period_end: datetime | None) -> int:
    end = period_end.strftime("%d %b %Y") if period_end else "the end of the current period"
    return notify_user(db, user, title="Cancellation scheduled", body=f"Your TubeNotes Pro access remains active until {end}.", tag="cancellation-scheduled", url="/?account=1")


def subscription_ended(db: Session, user: User) -> int:
    return notify_user(
        db, user, title="Your subscription has ended",
        body="Your account is now on the free plan. Subscribe anytime to continue.",
        tag="subscription-ended", url="/?account=1",
        actions=[
            {"action": "subscribe", "title": "Subscribe now"},
            {"action": "open", "title": "Open TubeNotes"},
        ],
    )
