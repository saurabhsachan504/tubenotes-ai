"""Applies normalised payment events to local subscription state."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import BillingPayment, Subscription, SubscriptionStatus, User, WebhookEvent
from app.services.payments.base import NormalizedEvent

logger = logging.getLogger("trialguard.billing")


def already_processed(db: Session, provider: str, event_id: str) -> bool:
    return (
        db.execute(
            select(WebhookEvent).where(
                WebhookEvent.provider == provider, WebhookEvent.event_id == event_id
            )
        ).scalar_one_or_none()
        is not None
    )


def record_event(db: Session, event: NormalizedEvent) -> bool:
    """Store the event id. Returns False if it was already recorded."""
    record = WebhookEvent(
        provider=event.provider,
        event_id=event.event_id,
        event_type=event.event_type,
        payload=json.dumps(event.raw, default=str)[:100_000],
    )
    db.add(record)
    try:
        db.flush()
        return True
    except IntegrityError:
        db.rollback()
        return False


def _find_user(db: Session, event: NormalizedEvent) -> User | None:
    if event.user_id:
        user = db.get(User, event.user_id)
        if user:
            return user
    if event.subscription_id:
        sub = db.execute(
            select(Subscription).where(
                Subscription.provider == event.provider,
                Subscription.provider_subscription_id == event.subscription_id,
            )
        ).scalar_one_or_none()
        if sub:
            return db.get(User, sub.user_id)
    if event.customer_id:
        sub = db.execute(
            select(Subscription).where(
                Subscription.provider == event.provider,
                Subscription.provider_customer_id == event.customer_id,
            )
        ).scalar_one_or_none()
        if sub:
            return db.get(User, sub.user_id)
    return None


def apply_event(db: Session, event: NormalizedEvent) -> Subscription | None:
    """Create or update the local Subscription row from a webhook event."""
    user = _find_user(db, event)
    if user is None:
        logger.warning(
            "Webhook %s/%s could not be mapped to a user", event.provider, event.event_id
        )
        return None

    sub: Subscription | None = None
    if event.subscription_id:
        sub = db.execute(
            select(Subscription).where(
                Subscription.provider == event.provider,
                Subscription.provider_subscription_id == event.subscription_id,
            )
        ).scalar_one_or_none()
    if sub is None:
        # Fall back to an in-flight row created when checkout started.
        sub = db.execute(
            select(Subscription)
            .where(
                Subscription.user_id == user.id,
                Subscription.provider == event.provider,
                Subscription.provider_subscription_id.is_(None),
            )
            .order_by(Subscription.created_at.desc())
        ).scalar_one_or_none()

    if sub is None:
        sub = Subscription(
            user_id=user.id,
            provider=event.provider,
            price_cents=settings.PLAN_PRICE_CENTS,
            currency=settings.PLAN_CURRENCY,
        )
        db.add(sub)

    if event.customer_id:
        sub.provider_customer_id = event.customer_id
    if event.subscription_id:
        sub.provider_subscription_id = event.subscription_id
    if event.status is not None:
        sub.status = event.status
        if event.status == SubscriptionStatus.canceled:
            sub.canceled_at = sub.canceled_at or event.current_period_end
    if event.current_period_end is not None:
        sub.current_period_end = event.current_period_end
    sub.cancel_at_period_end = event.cancel_at_period_end

    _record_payment(db, user, event)

    db.flush()
    return sub


def _record_payment(db: Session, user: User, event: NormalizedEvent) -> None:
    """Upsert a payment emitted by a verified provider webhook."""
    if not event.payment_id:
        return

    payment = db.execute(
        select(BillingPayment).where(
            BillingPayment.provider == event.provider,
            BillingPayment.provider_payment_id == event.payment_id,
        )
    ).scalar_one_or_none()
    if payment is None:
        payment = BillingPayment(
            user_id=user.id,
            provider=event.provider,
            provider_payment_id=event.payment_id,
            status=event.payment_status or "unknown",
        )
        db.add(payment)

    payment.user_id = user.id
    payment.provider_subscription_id = event.subscription_id or payment.provider_subscription_id
    payment.provider_invoice_id = event.invoice_id or payment.provider_invoice_id
    payment.status = event.payment_status or payment.status
    payment.amount_subunits = (
        event.amount_subunits if event.amount_subunits is not None else payment.amount_subunits
    )
    payment.currency = event.currency or payment.currency
    payment.invoice_url = event.invoice_url or payment.invoice_url
    payment.paid_at = event.paid_at or payment.paid_at
    payment.failure_message = event.failure_message or payment.failure_message


def sync_subscription_payment_history(db: Session, user: User, provider) -> None:
    """Backfill prior payments for the signed-in user's known subscriptions.

    This lets a payment made before this feature was deployed appear in Account.
    The provider is queried only with subscription ids already owned by the
    current user; browser input never selects a subscription or payment.
    """
    fetch_payments = getattr(provider, "fetch_subscription_payments", None)
    if not fetch_payments:
        return

    subscriptions = db.execute(
        select(Subscription).where(
            Subscription.user_id == user.id,
            Subscription.provider == provider.name,
            Subscription.provider_subscription_id.is_not(None),
        )
    ).scalars().all()
    for subscription in subscriptions:
        try:
            remote_payments = fetch_payments(subscription.provider_subscription_id)
        except Exception:
            logger.info("Could not refresh payment history for %s", subscription.id)
            continue
        for remote in remote_payments:
            payment_id = remote.get("id")
            if not isinstance(payment_id, str) or not payment_id:
                continue
            payment = db.execute(
                select(BillingPayment).where(
                    BillingPayment.provider == provider.name,
                    BillingPayment.provider_payment_id == payment_id,
                )
            ).scalar_one_or_none()
            if payment is None:
                payment = BillingPayment(
                    user_id=user.id,
                    provider=provider.name,
                    provider_payment_id=payment_id,
                    status=str(remote.get("status") or "unknown"),
                )
                db.add(payment)
            created = remote.get("created_at")
            paid_at = (
                datetime.fromtimestamp(created, tz=timezone.utc)
                if isinstance(created, (int, float))
                else None
            )
            payment.user_id = user.id
            payment.provider_subscription_id = subscription.provider_subscription_id
            payment.provider_invoice_id = remote.get("invoice_id") or payment.provider_invoice_id
            payment.status = str(remote.get("status") or payment.status)
            payment.amount_subunits = remote.get("amount") if isinstance(remote.get("amount"), int) else payment.amount_subunits
            payment.currency = remote.get("currency") or payment.currency
            payment.paid_at = paid_at or payment.paid_at
            payment.failure_message = remote.get("error_description") or payment.failure_message


def start_checkout_record(
    db: Session, user: User, provider: str, *, plan=None
) -> Subscription:
    """Placeholder row so a webhook that arrives before we store ids still lands."""
    existing = db.execute(
        select(Subscription).where(
            Subscription.user_id == user.id,
            Subscription.provider == provider,
            Subscription.provider_subscription_id.is_(None),
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    # The authenticated checkout route supplies the server-selected plan.
    # The mock-confirm endpoint may call this directly in development, where
    # retaining the legacy USD default keeps the isolated test harness useful.
    if plan is not None:
        price_cents, currency = plan.price_cents, plan.currency
    else:
        price_cents, currency = settings.PLAN_PRICE_CENTS, settings.PLAN_CURRENCY

    sub = Subscription(
        user_id=user.id,
        provider=provider,
        status=SubscriptionStatus.incomplete,
        price_cents=price_cents,
        currency=currency,
    )
    db.add(sub)
    db.flush()
    return sub
