"""Subscription checkout, status and the mock-provider test harness."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.deps import get_current_user
from app.models import BillingPayment, Subscription, SubscriptionStatus, User
from app.schemas import (
    CheckoutRequest,
    CheckoutSessionOut,
    BillingPaymentOut,
    CouponClaimOut,
    MessageOut,
    PlanOut,
    PromotionOfferOut,
    SubscriptionOut,
)
from app.security import constant_time_equals
from app.services import billing as billing_service
from app.services import email as email_service
from app.services.payments import get_provider
from app.services.pricing import plan_for_headers, plans
from app.services.promotions import (
    claim_launch_coupon,
    launch_offer_for_user,
    plan_for_coupon,
    redeem_coupon,
)

router = APIRouter(prefix="/billing", tags=["billing"])


@router.get("/plans", response_model=list[PlanOut])
def list_plans():
    return [
        PlanOut(
            id=plan.id,
            name=f"{settings.APP_NAME} Pro",
            price_cents=plan.price_cents,
            currency=plan.currency,
            interval=plan.interval,
            description=plan.description,
            free_trials=settings.FREE_TRIAL_LIMIT,
        )
        for plan in plans()
    ]


def _plan_out(plan) -> PlanOut:
    return PlanOut(
        id=plan.id,
        name=f"{settings.APP_NAME} Pro",
        price_cents=plan.price_cents,
        currency=plan.currency,
        interval=plan.interval,
        description=plan.description,
        free_trials=settings.FREE_TRIAL_LIMIT,
    )


@router.get("/price", response_model=PlanOut)
def current_price(request: Request, response: Response):
    """Public display price, derived from the trusted Cloudflare header.

    This response is country-specific, so browser/CDN caches must never reuse
    one visitor's price for another visitor.
    """
    response.headers["Cache-Control"] = "private, no-store"
    return _plan_out(plan_for_headers(request.headers))


@router.post("/offers/launch/claim", response_model=CouponClaimOut)
def claim_launch_offer(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Issue an expiring, account-bound code for the caller's eligible campaign."""
    from app.services.entitlements import active_subscription

    if active_subscription(db, user) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You already have an active subscription.",
        )
    try:
        coupon = claim_launch_coupon(db, user, request.headers)
        db.commit()
        db.refresh(coupon)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    return CouponClaimOut(
        code=coupon.code,
        price_cents=coupon.price_subunits,
        currency=coupon.currency,
        interval=settings.PLAN_INTERVAL,
        expires_at=coupon.expires_at,
    )


@router.get("/offers/launch", response_model=PromotionOfferOut)
def launch_offer(request: Request, user: User = Depends(get_current_user)):
    """Return display information for the server-controlled campaign price."""
    try:
        offer = launch_offer_for_user(user, request.headers)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return PromotionOfferOut(
        price_cents=offer.plan.price_cents,
        regular_price_cents=offer.regular_price_cents,
        currency=offer.plan.currency,
        interval=offer.plan.interval,
        ends_at=offer.ends_at,
        server_time=datetime.now(timezone.utc),
        title=offer.title,
    )


@router.post("/checkout", response_model=CheckoutSessionOut)
def create_checkout(
    payload: CheckoutRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # The old domain may stay online for existing users, but should never be
    # able to create a fresh payment after billing moves to the primary site.
    # The browser also redirects there; this server-side check protects the
    # endpoint from direct API calls.
    if settings.BILLING_PRIMARY_SITE_URL.strip():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "New payments are available at "
                f"{settings.BILLING_PRIMARY_SITE_URL.rstrip('/')}"
            ),
        )

    provider = get_provider()

    from app.services.entitlements import active_subscription

    if active_subscription(db, user) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You already have an active subscription.",
        )

    coupon = None
    try:
        if payload.coupon_code:
            coupon, plan = plan_for_coupon(db, user, payload.coupon_code, request.headers)
        else:
            plan = plan_for_headers(request.headers)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    session = provider.create_checkout_session(
        user,
        plan=plan,
        success_url=payload.success_url or settings.BILLING_SUCCESS_URL,
        cancel_url=payload.cancel_url or settings.BILLING_CANCEL_URL,
    )
    sub = billing_service.start_checkout_record(db, user, provider.name, plan=plan)
    if provider.name == "razorpay":
        # Razorpay creates the subscription up front, so we already know its id.
        sub.provider_subscription_id = session.session_id
    if coupon is not None:
        # A coupon is redeemed only after the provider successfully created a
        # hosted checkout. Failed API calls leave the code usable.
        redeem_coupon(coupon, provider=provider.name, session_id=session.session_id)
    db.commit()

    return CheckoutSessionOut(
        provider=session.provider,
        checkout_url=session.checkout_url,
        session_id=session.session_id,
        expires_at=session.expires_at,
    )


@router.get("/subscription", response_model=SubscriptionOut | None)
def get_subscription(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    sub = db.execute(
        select(Subscription)
        .where(Subscription.user_id == user.id)
        .order_by(Subscription.created_at.desc())
    ).scalars().first()
    if sub is None:
        return None
    return SubscriptionOut.model_validate(sub)


@router.get("/history", response_model=list[BillingPaymentOut])
def billing_history(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    """Customer-visible history from verified payment webhooks only."""
    # Existing customers may have paid before billing history was added. Pull
    # only the caller's already-recorded provider subscriptions to backfill
    # those rows; failure here never blocks Account from opening.
    try:
        billing_service.sync_subscription_payment_history(db, user, get_provider())
        db.commit()
    except Exception:
        db.rollback()

    records = db.execute(
        select(BillingPayment)
        .where(BillingPayment.user_id == user.id)
        .order_by(BillingPayment.paid_at.desc(), BillingPayment.created_at.desc())
    ).scalars().all()

    # Razorpay sends an invoice id with a payment, but its shareable link can
    # appear a little later. Refresh it on Account open until it is available.
    changed = False
    provider = None
    for record in records:
        if record.invoice_url or not record.provider_invoice_id:
            continue
        try:
            provider = provider or get_provider()
            fetch_url = getattr(provider, "fetch_invoice_url", None)
            url = fetch_url(record.provider_invoice_id) if fetch_url and provider.name == record.provider else None
        except Exception:
            url = None
        if url:
            record.invoice_url = url
            changed = True
    if changed:
        db.commit()
    return [BillingPaymentOut.model_validate(record) for record in records]


@router.post("/portal", response_model=MessageOut)
def billing_portal(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    url = get_provider().create_portal_session(
        user, return_url=settings.BILLING_SUCCESS_URL
    )
    if not url:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No billing portal available for this account.",
        )
    return MessageOut(detail=url)


@router.post("/cancel", response_model=MessageOut)
def cancel_subscription(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    from app.services.entitlements import active_subscription

    sub = active_subscription(db, user)
    if sub is None:
        raise HTTPException(status_code=404, detail="No active subscription.")
    if sub.provider_subscription_id:
        get_provider().cancel_subscription(
            sub.provider_subscription_id, at_period_end=True
        )
    sub.cancel_at_period_end = True
    db.commit()
    email_service.send_cancellation_scheduled_email(user, sub.current_period_end)
    return MessageOut(
        detail="Subscription will end at the close of the current billing period."
    )


# ---------------------------------------------------------------------------
# Mock provider harness (dev/test only)
# ---------------------------------------------------------------------------
def _assert_mock_enabled() -> None:
    """The fake checkout may only ever exist outside production.

    It grants a paid subscription for free, so it is also gated on a shared
    secret - being non-prod is not on its own a good enough reason to expose it.
    """
    if (
        settings.PAYMENT_PROVIDER != "mock"
        or settings.ENV == "prod"
        or not settings.MOCK_BILLING_SECRET
    ):
        raise HTTPException(status_code=404, detail="Not found")


@router.get("/mock/checkout", response_class=HTMLResponse, include_in_schema=False)
def mock_checkout_page(session_id: str, token: str = ""):
    _assert_mock_enabled()
    if not constant_time_equals(token, settings.MOCK_BILLING_SECRET):
        raise HTTPException(status_code=404, detail="Not found")
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Mock checkout</title>
<style>body{{font-family:system-ui;margin:4rem auto;max-width:32rem;line-height:1.6}}
button{{padding:.75rem 1.25rem;font-size:1rem;border-radius:8px;border:0;
background:#111;color:#fff;cursor:pointer}}
input{{width:100%;padding:.5rem;font-family:monospace}}</style></head><body>
<h1>Mock checkout</h1>
<p>Subscribe to <b>{settings.APP_NAME} Pro</b> &mdash;
${settings.PLAN_PRICE_CENTS / 100:.2f}/{settings.PLAN_INTERVAL}</p>
<p><small>session {session_id}</small></p>
<p><label>Paste your access token<br><input id="tok" placeholder="eyJ..."></label></p>
<button onclick="pay()">Pay now</button>
<pre id="out"></pre>
<script>
async function pay() {{
  const r = await fetch('{settings.API_PREFIX}/billing/mock/confirm', {{
    method: 'POST',
    headers: {{
      'Content-Type': 'application/json',
      'Authorization': 'Bearer ' + document.getElementById('tok').value.trim()
    }},
    body: JSON.stringify({{session_id: '{session_id}'}})
  }});
  document.getElementById('out').textContent = await r.text();
}}
</script></body></html>"""


@router.post("/mock/confirm", response_model=SubscriptionOut, include_in_schema=False)
def mock_confirm(
    body: dict,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Stand-in for a real provider webhook: activates the caller's subscription.

    Requires a valid access token, so it can only ever affect the authenticated
    user - never an arbitrary user_id supplied in the body.
    """
    _assert_mock_enabled()
    session_id = str(body.get("session_id", "mock_sub"))[:64]

    sub = billing_service.start_checkout_record(db, user, "mock")
    sub.provider_subscription_id = f"sub_{session_id}"
    sub.provider_customer_id = f"cus_{user.id[:12]}"
    sub.status = SubscriptionStatus.active
    sub.current_period_end = datetime.now(timezone.utc) + timedelta(days=30)
    db.commit()
    db.refresh(sub)
    return SubscriptionOut.model_validate(sub)
