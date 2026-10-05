"""Country-based subscription plans chosen from trusted request metadata.

The browser may display a price, but it never chooses the amount sent to a
payment provider.  The checkout route independently derives the plan from the
Cloudflare country header, after the request reaches the server.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Mapping
from urllib.parse import unquote

from app.config import settings


BILLING_COUNTRY_INDIA = "IN"
BILLING_COUNTRY_INTERNATIONAL = "INTL"
VALID_BILLING_COUNTRIES = {
    BILLING_COUNTRY_INDIA,
    BILLING_COUNTRY_INTERNATIONAL,
}


@dataclass(frozen=True, slots=True)
class BillingPlan:
    id: str
    billing_country: str
    price_cents: int  # smallest currency unit: paise for INR, cents for USD
    currency: str
    interval: str
    description: str
    razorpay_plan_id: str


def plans() -> tuple[BillingPlan, BillingPlan]:
    """Return the two public plans without exposing Razorpay identifiers."""
    return (
        BillingPlan(
            id="pro-monthly-india",
            billing_country=BILLING_COUNTRY_INDIA,
            price_cents=settings.INDIA_PLAN_PRICE_SUBUNITS,
            currency="INR",
            interval=settings.PLAN_INTERVAL,
            description="Unlimited usage, billed monthly. For India billing accounts.",
            razorpay_plan_id=settings.RAZORPAY_PLAN_ID_INR,
        ),
        BillingPlan(
            id="pro-monthly-international",
            billing_country=BILLING_COUNTRY_INTERNATIONAL,
            price_cents=settings.INTERNATIONAL_PLAN_PRICE_CENTS,
            currency="USD",
            interval=settings.PLAN_INTERVAL,
            description="Unlimited usage, billed monthly. For billing accounts outside India.",
            # Keep existing deployments working until the explicit USD variable
            # is added to their environment file.
            razorpay_plan_id=(
                settings.RAZORPAY_PLAN_ID_USD or settings.RAZORPAY_PLAN_ID
            ),
        ),
    )


def plan_for_country(country: str | None) -> BillingPlan:
    if country == BILLING_COUNTRY_INDIA:
        return plans()[0]
    if country == BILLING_COUNTRY_INTERNATIONAL:
        return plans()[1]
    return plans()[1]


def india_launch_offer_plan() -> BillingPlan:
    """The campaign plan stored with a newly issued personal coupon."""
    return BillingPlan(
        id="pro-monthly-india-launch99",
        billing_country=BILLING_COUNTRY_INDIA,
        price_cents=settings.INDIA_LAUNCH_OFFER_PRICE_SUBUNITS,
        currency="INR",
        interval=settings.PLAN_INTERVAL,
        description="Limited launch offer: unlimited usage at the campaign price.",
        razorpay_plan_id=settings.RAZORPAY_PLAN_ID_INR_LAUNCH_OFFER,
    )


def international_launch_offer_plan() -> BillingPlan:
    """The account-bound USD campaign plan."""
    return BillingPlan(
        id="pro-monthly-international-launch",
        billing_country=BILLING_COUNTRY_INTERNATIONAL,
        price_cents=settings.INTERNATIONAL_LAUNCH_OFFER_PRICE_CENTS,
        currency="USD",
        interval=settings.PLAN_INTERVAL,
        description="Limited launch offer for monthly TubeNotes Pro.",
        razorpay_plan_id=settings.RAZORPAY_PLAN_ID_USD_LAUNCH_OFFER,
    )


def personal_india_offer_plan() -> BillingPlan:
    """The private INR campaign plan, available only to configured emails."""
    return BillingPlan(
        id="pro-monthly-india-personal298",
        billing_country=BILLING_COUNTRY_INDIA,
        price_cents=settings.PERSONAL_LAUNCH_OFFER_PRICE_SUBUNITS,
        currency="INR",
        interval=settings.PLAN_INTERVAL,
        description="Private monthly TubeNotes Pro offer.",
        razorpay_plan_id=settings.RAZORPAY_PLAN_ID_INR_PERSONAL_OFFER,
    )


def country_for_headers(headers: Mapping[str, str]) -> str:
    """Return the safe country tier for a request.

    A missing, malformed, or untrusted Cloudflare header always gets the
    international price. This is intentional: the cheaper INR plan must never
    be selected by a browser-provided value.
    """
    if not settings.TRUST_CLOUDFLARE_COUNTRY_HEADER:
        return BILLING_COUNTRY_INTERNATIONAL
    return (
        BILLING_COUNTRY_INDIA
        if headers.get("cf-ipcountry", "").strip().upper() == "IN"
        else BILLING_COUNTRY_INTERNATIONAL
    )


def country_code_for_headers(headers: Mapping[str, str]) -> str | None:
    """Return a real ISO country code only when Cloudflare headers are trusted.

    This is for admin analytics, not pricing.  It deliberately never trusts a
    browser-supplied header, and it leaves direct local requests as ``None``
    instead of guessing a user's country.
    """
    if not settings.TRUST_CLOUDFLARE_COUNTRY_HEADER:
        return None
    code = headers.get("cf-ipcountry", "").strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", code) or code in {"T1", "XX"}:
        return None
    return code


def city_for_headers(headers: Mapping[str, str]) -> str | None:
    """Return Cloudflare's visitor city for admin job analytics.

    ``CF-IPCity`` is available only when the Cloudflare visitor-location
    managed transform is enabled. Like the country header, it is accepted only
    when the origin is reachable exclusively through Cloudflare. The value is
    display data, so we normalise URL encoding/whitespace and reject control
    characters rather than ever storing an arbitrary header verbatim.
    """
    if not settings.TRUST_CLOUDFLARE_COUNTRY_HEADER:
        return None
    raw = headers.get("cf-ipcity", "")
    city = re.sub(r"\s+", " ", unquote(raw).strip())
    if not city or len(city) > 120 or any(ord(char) < 32 for char in city):
        return None
    return city


def plan_for_headers(headers: Mapping[str, str]) -> BillingPlan:
    """Return the normal public price; promotional pricing needs a coupon."""
    return plan_for_country(country_for_headers(headers))
