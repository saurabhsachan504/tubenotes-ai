"""Razorpay Subscriptions implementation (useful when billing from India)."""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone

import razorpay

from app.config import settings
from app.models import SubscriptionStatus, User
from app.services.payments.base import (
    CheckoutSession,
    NormalizedEvent,
    PaymentProvider,
    WebhookVerificationError,
)

_STATUS_MAP = {
    "created": SubscriptionStatus.incomplete,
    "authenticated": SubscriptionStatus.incomplete,
    "pending": SubscriptionStatus.past_due,
    "halted": SubscriptionStatus.unpaid,
    "active": SubscriptionStatus.active,
    "paused": SubscriptionStatus.canceled,
    "cancelled": SubscriptionStatus.canceled,
    "completed": SubscriptionStatus.canceled,
    "expired": SubscriptionStatus.canceled,
}


class RazorpayProvider(PaymentProvider):
    name = "razorpay"

    def __init__(self) -> None:
        if not (settings.RAZORPAY_KEY_ID and settings.RAZORPAY_KEY_SECRET):
            raise RuntimeError("RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are not configured")
        self.client = razorpay.Client(
            auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET)
        )

    def create_checkout_session(
        self, user: User, *, plan, success_url: str, cancel_url: str
    ) -> CheckoutSession:
        if not plan.razorpay_plan_id:
            label = (
                f"INR Rs {plan.price_cents / 100:g}"
                if plan.currency == "INR"
                else f"USD ${plan.price_cents / 100:g}"
            )
            raise RuntimeError(
                f"The Razorpay {label} Plan ID is not configured. "
                "Create the plan in Razorpay and set its environment variable."
            )
        subscription = self.client.subscription.create(
            {
                "plan_id": plan.razorpay_plan_id,
                "customer_notify": 1,
                "total_count": 120,  # up to 10 years of monthly cycles
                "notes": {"user_id": user.id, "email": user.email},
            }
        )
        return CheckoutSession(
            provider=self.name,
            checkout_url=subscription["short_url"],
            session_id=subscription["id"],
        )

    def cancel_subscription(self, subscription_id: str, *, at_period_end: bool = True) -> None:
        self.client.subscription.cancel(
            subscription_id, {"cancel_at_cycle_end": 1 if at_period_end else 0}
        )

    def fetch_invoice_url(self, invoice_id: str) -> str | None:
        """Return Razorpay's customer-facing invoice URL, if one is ready."""
        try:
            invoice = self.client.invoice.fetch(invoice_id)
        except Exception:
            # Invoice generation can lag the payment webhook. The next Account
            # refresh retries this lookup without making checkout fail.
            return None
        url = invoice.get("short_url") or invoice.get("hosted_invoice_url")
        return url if isinstance(url, str) and url.startswith("https://") else None

    def fetch_subscription_payments(self, subscription_id: str) -> list[dict]:
        """Read prior Razorpay payments for one known local subscription."""
        try:
            response = self.client.payment.all(
                {"subscription_id": subscription_id, "count": 100}
            )
        except Exception:
            return []
        items = response.get("items", []) if isinstance(response, dict) else []
        return [dict(item) for item in items if isinstance(item, dict)]

    def parse_webhook(self, payload: bytes, headers: dict[str, str]) -> NormalizedEvent:
        signature = headers.get("x-razorpay-signature")
        secret = settings.RAZORPAY_WEBHOOK_SECRET
        if not signature or not secret:
            raise WebhookVerificationError("missing razorpay signature or secret")

        expected = hmac.new(
            secret.encode("utf-8"), payload, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise WebhookVerificationError("signature mismatch")

        body = json.loads(payload.decode("utf-8"))
        etype = body.get("event", "")
        payload_data = body.get("payload", {})
        entity = payload_data.get("subscription", {}).get("entity", {})
        payment = payload_data.get("payment", {}).get("entity", {})
        payment_created = payment.get("created_at") or body.get("created_at")

        normalised = NormalizedEvent(
            provider=self.name,
            event_id=str(
                headers.get("x-razorpay-event-id")
                or f"{etype}:{entity.get('id') or payment.get('id')}:{body.get('created_at')}"
            ),
            event_type=etype,
            user_id=(entity.get("notes") or {}).get("user_id"),
            customer_id=entity.get("customer_id"),
            subscription_id=entity.get("id"),
            raw=body,
        )

        if entity.get("status"):
            normalised.status = _STATUS_MAP.get(
                entity["status"], SubscriptionStatus.incomplete
            )
        if entity.get("current_end"):
            normalised.current_period_end = datetime.fromtimestamp(
                entity["current_end"], tz=timezone.utc
            )
        if payment:
            normalised.payment_id = payment.get("id")
            normalised.payment_status = payment.get("status") or (
                "captured" if etype == "subscription.charged" else None
            )
            normalised.amount_subunits = payment.get("amount")
            normalised.currency = payment.get("currency")
            normalised.invoice_id = payment.get("invoice_id")
            normalised.failure_message = (
                payment.get("error_description") or payment.get("error_reason")
            )
            if isinstance(payment_created, (int, float)):
                normalised.paid_at = datetime.fromtimestamp(payment_created, tz=timezone.utc)
        return normalised
