"""Account-bound subscription promotion coupons."""
from __future__ import annotations

import secrets
from datetime import timedelta, timezone
from typing import Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import PromotionCoupon, User, utcnow
from app.services.pricing import (
    BILLING_COUNTRY_INDIA,
    BillingPlan,
    country_for_headers,
    india_launch_offer_plan,
)

LAUNCH_OFFER_KEY = "india_launch"


def _code_for_price(price_subunits: int) -> str:
    rupees = price_subunits // 100
    # token_hex is deliberately much longer than a sequential code.  The code
    # is additionally tied to user_id at redemption, so it is not transferable.
    return f"TUBE{rupees}-{secrets.token_hex(5).upper()}"


def launch_offer_for_headers(headers: Mapping[str, str]) -> BillingPlan:
    """Return the current campaign plan only to an eligible India visitor."""
    if not settings.INDIA_LAUNCH_OFFER_ENABLED:
        raise ValueError("This offer is not available right now.")
    if country_for_headers(headers) != BILLING_COUNTRY_INDIA:
        raise ValueError("This offer is available only for India billing accounts.")
    if utcnow() >= settings.INDIA_LAUNCH_OFFER_ENDS_AT:
        raise ValueError("This limited-time offer has ended.")
    return india_launch_offer_plan()


def claim_launch_coupon(
    db: Session, user: User, headers: Mapping[str, str]
) -> PromotionCoupon:
    """Return the caller's valid personal offer or issue a fresh one."""
    now = utcnow()
    existing = db.execute(
        select(PromotionCoupon)
        .where(
            PromotionCoupon.user_id == user.id,
            PromotionCoupon.offer_key == LAUNCH_OFFER_KEY,
            PromotionCoupon.status == "issued",
            PromotionCoupon.expires_at > now,
        )
        .order_by(PromotionCoupon.created_at.desc())
    ).scalars().first()
    if existing is not None:
        return existing

    plan = launch_offer_for_headers(headers)
    coupon = PromotionCoupon(
        user_id=user.id,
        code=_code_for_price(plan.price_cents),
        offer_key=LAUNCH_OFFER_KEY,
        status="issued",
        price_subunits=plan.price_cents,
        currency=plan.currency,
        razorpay_plan_id=plan.razorpay_plan_id or None,
        expires_at=now + timedelta(hours=max(1, settings.INDIA_LAUNCH_COUPON_TTL_HOURS)),
    )
    db.add(coupon)
    db.flush()
    return coupon


def plan_for_coupon(
    db: Session, user: User, code: str, headers: Mapping[str, str]
) -> tuple[PromotionCoupon, BillingPlan]:
    """Validate an issued code and reconstruct its immutable server plan."""
    normalized = code.strip().upper()
    coupon = db.execute(
        select(PromotionCoupon).where(
            PromotionCoupon.user_id == user.id,
            PromotionCoupon.code == normalized,
        )
    ).scalar_one_or_none()
    if coupon is None:
        raise ValueError("That coupon code is not valid for this account.")

    now = utcnow()
    if coupon.status != "issued":
        raise ValueError("This coupon code has already been used.")
    expires_at = coupon.expires_at
    # SQLite (used in tests/local development) returns timezone-naive values
    # for DateTime columns even when timezone=True. PostgreSQL preserves UTC.
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= now:
        coupon.status = "expired"
        db.flush()
        raise ValueError("This coupon code has expired. Claim a new offer to continue.")
    if coupon.offer_key != LAUNCH_OFFER_KEY or country_for_headers(headers) != BILLING_COUNTRY_INDIA:
        raise ValueError("This coupon is available only for India billing accounts.")

    return coupon, BillingPlan(
        id=f"pro-monthly-india-coupon-{coupon.id}",
        billing_country=BILLING_COUNTRY_INDIA,
        price_cents=coupon.price_subunits,
        currency=coupon.currency,
        interval=settings.PLAN_INTERVAL,
        description="Personal limited offer for monthly TubeNotes Pro.",
        razorpay_plan_id=coupon.razorpay_plan_id or "",
    )


def redeem_coupon(coupon: PromotionCoupon, *, provider: str, session_id: str) -> None:
    """Make a code single-use only after the provider created checkout."""
    coupon.status = "redeemed"
    coupon.redeemed_at = utcnow()
    coupon.provider = provider
    coupon.provider_session_id = session_id
