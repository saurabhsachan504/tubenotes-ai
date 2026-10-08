"""Best-effort customer browser Web Push delivery."""
from __future__ import annotations

import json
import logging
from datetime import datetime

from pywebpush import WebPushException, webpush
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import User, WebPushSubscription

logger = logging.getLogger("trialguard.web_push")


def configured() -> bool:
    return settings.web_push_configured


def _send(subscription: WebPushSubscription, payload: dict) -> bool:
    """Send one encrypted payload. False means the endpoint has expired."""
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
        return True
    except WebPushException as exc:
        response = getattr(exc, "response", None)
        if getattr(response, "status_code", None) in {404, 410}:
            logger.info("removing expired Web Push endpoint for user %s", subscription.user_id)
            return False
        logger.warning("Web Push delivery failed for user %s: %s", subscription.user_id, exc)
    except Exception:
        logger.warning("Web Push delivery failed for user %s", subscription.user_id, exc_info=True)
    return True


def notify_user(
    db: Session, user: User, *, title: str, body: str, tag: str, url: str = "/"
) -> int:
    """Deliver an account event to all browser endpoints that user allowed."""
    if not configured():
        return 0
    rows = db.execute(
        select(WebPushSubscription).where(WebPushSubscription.user_id == user.id)
    ).scalars().all()
    payload = {"title": title[:120], "body": body[:500], "tag": tag[:80], "url": url}
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
        return notify_user(db, user, title="Your free trial is complete", body="You've used all free videos. Subscribe to continue summarizing.", tag="trial-0")
    return notify_user(db, user, title=f"{trials_remaining} free videos left", body="Keep exploring TubeNotes before your free trial ends.", tag=f"trial-{trials_remaining}")


def payment_received(db: Session, user: User, *, amount_subunits: int | None, currency: str | None, renewal: bool) -> int:
    amount = f" ({currency.upper()} {amount_subunits / 100:,.2f})" if amount_subunits is not None and currency else ""
    return notify_user(db, user, title="TubeNotes Pro renewed" if renewal else "Welcome to TubeNotes Pro", body=("Your renewal payment was successful" if renewal else "Your subscription is now active") + amount + ".", tag="subscription-renewal" if renewal else "subscription-started")


def payment_attention(db: Session, user: User) -> int:
    return notify_user(db, user, title="Payment needs attention", body="We couldn't complete your subscription payment. Please check your payment method.", tag="payment-attention", url="/?account=1")


def cancellation_scheduled(db: Session, user: User, period_end: datetime | None) -> int:
    end = period_end.strftime("%d %b %Y") if period_end else "the end of the current period"
    return notify_user(db, user, title="Cancellation scheduled", body=f"Your TubeNotes Pro access remains active until {end}.", tag="cancellation-scheduled", url="/?account=1")


def subscription_ended(db: Session, user: User) -> int:
    return notify_user(db, user, title="Your subscription has ended", body="Your account is now on the free plan. Subscribe anytime to continue.", tag="subscription-ended", url="/?account=1")
