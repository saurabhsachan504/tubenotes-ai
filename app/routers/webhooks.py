"""Payment provider webhooks.

Signatures are verified before anything is trusted, and every event id is
recorded so provider retries are idempotent.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import BillingPayment, Subscription, User
from app.schemas import MessageOut
from app.services import billing as billing_service
from app.services import email as email_service
from app.services import ntfy as ntfy_service
from app.services import web_push as web_push_service
from app.services.payments import get_provider
from app.services.payments.base import WebhookVerificationError

logger = logging.getLogger("trialguard.webhooks")
router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def _send_billing_notification(
    db: Session, event, subscription: Subscription | None
) -> None:
    """Best-effort customer mail after the verified webhook is committed.

    Provider retries are handled by the event ledger before this point, so an
    identical webhook cannot create duplicate messages. An SMTP failure is
    logged by the email service and must never make a payment webhook fail.
    """
    if subscription is None:
        return
    user = db.get(User, subscription.user_id)
    if user is None:
        return

    if event.event_type in {"subscription.charged", "invoice.paid"}:
        if event.payment_status not in (None, "captured", "paid", "succeeded"):
            return
        payment_count = db.execute(
            select(BillingPayment.id).where(
                BillingPayment.provider == event.provider,
                BillingPayment.provider_subscription_id
                == subscription.provider_subscription_id,
                BillingPayment.status.in_(("captured", "paid", "succeeded")),
            )
        ).scalars().all()
        email_service.send_payment_success_email(
            user,
            amount_subunits=event.amount_subunits,
            currency=event.currency,
            renewal=len(payment_count) > 1,
            period_end=subscription.current_period_end,
        )
        ntfy_service.payment_received(
            user,
            amount_subunits=event.amount_subunits,
            currency=event.currency,
            renewal=len(payment_count) > 1,
        )
        web_push_service.payment_received(
            db, user,
            amount_subunits=event.amount_subunits,
            currency=event.currency,
            renewal=len(payment_count) > 1,
        )
    elif event.event_type in {"subscription.pending", "invoice.payment_failed"}:
        email_service.send_payment_attention_email(user)
        ntfy_service.payment_attention(user)
        web_push_service.payment_attention(db, user)
    elif event.event_type in {"subscription.cancelled", "customer.subscription.deleted"}:
        email_service.send_subscription_ended_email(user)
        ntfy_service.subscription_ended(user)
        web_push_service.subscription_ended(db, user)


async def _handle(request: Request, db: Session, expected: str) -> MessageOut:
    provider = get_provider()
    if provider.name != expected:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{expected} is not the configured payment provider",
        )

    payload = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}

    try:
        event = provider.parse_webhook(payload, headers)
    except WebhookVerificationError as exc:
        logger.warning("Rejected %s webhook: %s", expected, exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid signature"
        )

    if billing_service.already_processed(db, event.provider, event.event_id):
        return MessageOut(detail="duplicate ignored")

    if not billing_service.record_event(db, event):
        return MessageOut(detail="duplicate ignored")

    subscription = billing_service.apply_event(db, event)
    db.commit()
    _send_billing_notification(db, event, subscription)
    return MessageOut(detail="ok")


@router.post("/stripe", response_model=MessageOut)
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    return await _handle(request, db, "stripe")


@router.post("/razorpay", response_model=MessageOut)
async def razorpay_webhook(request: Request, db: Session = Depends(get_db)):
    return await _handle(request, db, "razorpay")


@router.post("/mock", response_model=MessageOut, include_in_schema=False)
async def mock_webhook(request: Request, db: Session = Depends(get_db)):
    return await _handle(request, db, "mock")
