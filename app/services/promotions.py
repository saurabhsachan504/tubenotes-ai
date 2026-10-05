"""Account-bound subscription promotion coupons."""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
    international_launch_offer_plan,
    personal_india_offer_plan,
    plan_for_country,
)

INDIA_LAUNCH_OFFER_KEY = "india_launch"
INTERNATIONAL_LAUNCH_OFFER_KEY = "international_launch"
PERSONAL_LAUNCH_OFFER_KEY = "personal_india_298"


@dataclass(frozen=True, slots=True)
class LaunchOffer:
    """A campaign selected entirely on the server for one account."""

    key: str
    plan: BillingPlan
    regular_price_cents: int
    ends_at: datetime
    coupon_ttl_hours: int
    title: str


def _code_for_price(price_subunits: int) -> str:
    amount = price_subunits // 100
    # token_hex is deliberately much longer than a sequential code. The code
    # is additionally tied to user_id at redemption, so it is not transferable.
    return f"TUBE{amount}-{secrets.token_hex(5).upper()}"


def _configured_personal_emails() -> set[str]:
    return {
        email.strip().casefold()
        for email in settings.PERSONAL_LAUNCH_OFFER_EMAILS.split(",")
        if email.strip()
    }


def _is_personal_offer_account(user: User) -> bool:
    return (
        settings.PERSONAL_LAUNCH_OFFER_ENABLED
        and user.email.casefold() in _configured_personal_emails()
    )


def _india_offer() -> LaunchOffer:
    plan = india_launch_offer_plan()
    return LaunchOffer(
        key=INDIA_LAUNCH_OFFER_KEY,
        plan=plan,
        regular_price_cents=plan_for_country(BILLING_COUNTRY_INDIA).price_cents,
        ends_at=settings.INDIA_LAUNCH_OFFER_ENDS_AT,
        coupon_ttl_hours=settings.INDIA_LAUNCH_COUPON_TTL_HOURS,
        title=f"Unlock Pro for ₹{plan.price_cents // 100}",
    )


def _international_offer() -> LaunchOffer:
    plan = international_launch_offer_plan()
    return LaunchOffer(
        key=INTERNATIONAL_LAUNCH_OFFER_KEY,
        plan=plan,
        regular_price_cents=plan_for_country("INTL").price_cents,
        ends_at=settings.INTERNATIONAL_LAUNCH_OFFER_ENDS_AT,
        coupon_ttl_hours=settings.INTERNATIONAL_LAUNCH_COUPON_TTL_HOURS,
        title=f"Unlock Pro for ${plan.price_cents / 100:.2f}",
    )


def _personal_offer() -> LaunchOffer:
    plan = personal_india_offer_plan()
    return LaunchOffer(
        key=PERSONAL_LAUNCH_OFFER_KEY,
        plan=plan,
        regular_price_cents=plan_for_country(BILLING_COUNTRY_INDIA).price_cents,
        ends_at=settings.PERSONAL_LAUNCH_OFFER_ENDS_AT,
        coupon_ttl_hours=settings.PERSONAL_LAUNCH_COUPON_TTL_HOURS,
        title=f"Your personal Pro offer: ₹{plan.price_cents // 100}",
    )


def launch_offer_for_user(user: User, headers: Mapping[str, str]) -> LaunchOffer:
    """Return exactly one currently eligible campaign for this signed-in user."""
    now = utcnow()
    if _is_personal_offer_account(user):
        offer = _personal_offer()
        if now < offer.ends_at:
            return offer

    if country_for_headers(headers) == BILLING_COUNTRY_INDIA:
        if not settings.INDIA_LAUNCH_OFFER_ENABLED:
            raise ValueError("This offer is not available right now.")
        offer = _india_offer()
        if now >= offer.ends_at:
            raise ValueError("This limited-time offer has ended.")
        return offer

    if not settings.INTERNATIONAL_LAUNCH_OFFER_ENABLED:
        raise ValueError("This offer is not available right now.")
    offer = _international_offer()
    if now >= offer.ends_at:
        raise ValueError("This limited-time offer has ended.")
    return offer


def claim_launch_coupon(
    db: Session, user: User, headers: Mapping[str, str]
) -> PromotionCoupon:
    """Return the caller's valid personal code or issue a fresh one."""
    now = utcnow()
    offer = launch_offer_for_user(user, headers)
    existing = db.execute(
        select(PromotionCoupon)
        .where(
            PromotionCoupon.user_id == user.id,
            PromotionCoupon.offer_key == offer.key,
            PromotionCoupon.status == "issued",
            PromotionCoupon.expires_at > now,
        )
        .order_by(PromotionCoupon.created_at.desc())
    ).scalars().first()
    if existing is not None:
        return existing

    coupon = PromotionCoupon(
        user_id=user.id,
        code=_code_for_price(offer.plan.price_cents),
        offer_key=offer.key,
        status="issued",
        price_subunits=offer.plan.price_cents,
        currency=offer.plan.currency,
        razorpay_plan_id=offer.plan.razorpay_plan_id or None,
        expires_at=now + timedelta(hours=max(1, offer.coupon_ttl_hours)),
    )
    db.add(coupon)
    db.flush()
    return coupon


def _coupon_is_eligible(
    coupon: PromotionCoupon, user: User, headers: Mapping[str, str]
) -> bool:
    if coupon.offer_key == PERSONAL_LAUNCH_OFFER_KEY:
        return _is_personal_offer_account(user)
    if coupon.offer_key == INDIA_LAUNCH_OFFER_KEY:
        return country_for_headers(headers) == BILLING_COUNTRY_INDIA
    if coupon.offer_key == INTERNATIONAL_LAUNCH_OFFER_KEY:
        return country_for_headers(headers) != BILLING_COUNTRY_INDIA
    return False


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
    if not _coupon_is_eligible(coupon, user, headers):
        raise ValueError("This coupon is not available for this billing account.")

    return coupon, BillingPlan(
        id=f"pro-monthly-coupon-{coupon.id}",
        billing_country=(
            BILLING_COUNTRY_INDIA if coupon.currency == "INR" else "INTL"
        ),
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
