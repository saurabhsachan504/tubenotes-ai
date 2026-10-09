"""Best-effort private operations alerts sent to a self-hosted ntfy server.

This is deliberately separate from customer email. Ntfy is an internal admin
channel, so messages use only a masked account reference and never include
passwords, API keys, full payment identifiers, or raw webhook payloads.
"""
from __future__ import annotations

import logging
from datetime import datetime
from urllib.parse import quote, urlparse

import httpx

from app.config import settings
from app.models import User

logger = logging.getLogger("trialguard.ntfy")


def _configured() -> bool:
    return bool(
        settings.NTFY_ENABLED
        and settings.NTFY_BASE_URL
        and settings.NTFY_TOPIC
        and settings.NTFY_TOKEN
    )


def _topic_url() -> str | None:
    base = settings.NTFY_BASE_URL.rstrip("/")
    parsed = urlparse(base)
    if parsed.scheme != "https" or not parsed.netloc:
        logger.error("ntfy is enabled but NTFY_BASE_URL is not a valid HTTPS URL")
        return None
    return f"{base}/{quote(settings.NTFY_TOPIC, safe='-_')}"


def publish(
    title: str,
    message: str,
    *,
    tags: tuple[str, ...] = (),
    priority: str = "default",
) -> bool:
    """Publish an alert without allowing ntfy outages to break TubeNotes."""
    if not _configured():
        return False
    url = _topic_url()
    if url is None:
        return False

    headers = {
        "Authorization": f"Bearer {settings.NTFY_TOKEN}",
        "Title": title[:256],
        "Priority": priority,
    }
    if tags:
        headers["Tags"] = ",".join(tags)
    try:
        response = httpx.post(
            url,
            content=message[:4_000].encode("utf-8"),
            headers=headers,
            timeout=max(0.5, settings.NTFY_TIMEOUT_SECONDS),
        )
        response.raise_for_status()
        return True
    except Exception:
        # Never log the URL because it could include a badly configured token.
        logger.exception("Failed to publish ntfy alert title=%r", title)
        return False


def account_reference(user: User) -> str:
    """A useful but non-sensitive account identifier for an operator alert."""
    local, _, domain = user.email.partition("@")
    visible = local[:2] if local else "user"
    return f"{visible}***@{domain or 'unknown'}"


def _money(amount_subunits: int | None, currency: str | None) -> str:
    if amount_subunits is None or not currency:
        return "amount unavailable"
    amount = amount_subunits / 100
    return f"{currency.upper()} {amount:,.2f}"


def signup(user: User, trial_limit: int) -> bool:
    country = user.billing_country or "unknown"
    return publish(
        "New TubeNotes signup",
        f"Account: {account_reference(user)}\nCountry: {country}\nFree trials: {trial_limit}",
        tags=("tada", "new"),
    )


def trial_milestone(user: User, trials_remaining: int) -> bool:
    if trials_remaining == 0:
        title = "Free trials complete"
        body = f"Account: {account_reference(user)}\nTrials remaining: 0"
        priority = "high"
        tags = ("warning", "hourglass_flowing_sand")
    else:
        title = f"Trial milestone: {trials_remaining} remaining"
        body = f"Account: {account_reference(user)}\nTrials remaining: {trials_remaining}"
        priority = "default"
        tags = ("chart_with_downwards_trend",)
    return publish(title, body, tags=tags, priority=priority)


def payment_received(
    user: User,
    *,
    amount_subunits: int | None,
    currency: str | None,
    renewal: bool,
) -> bool:
    return publish(
        "TubeNotes Pro renewal received" if renewal else "New TubeNotes Pro payment",
        f"Account: {account_reference(user)}\nAmount: {_money(amount_subunits, currency)}",
        tags=("moneybag", "white_check_mark"),
        priority="high",
    )


def payment_attention(user: User) -> bool:
    return publish(
        "TubeNotes payment needs attention",
        f"Account: {account_reference(user)}\nRazorpay reported a pending or failed subscription payment.",
        tags=("warning", "credit_card"),
        priority="high",
    )


def cancellation_scheduled(user: User, period_end: datetime | None) -> bool:
    end = period_end.strftime("%d %b %Y") if period_end else "end of current period"
    return publish(
        "TubeNotes cancellation scheduled",
        f"Account: {account_reference(user)}\nAccess ends: {end}",
        tags=("calendar",),
    )


def subscription_ended(user: User) -> bool:
    return publish(
        "TubeNotes subscription ended",
        f"Account: {account_reference(user)}\nAccount is now on the free plan.",
        tags=("x", "credit_card"),
        priority="high",
    )


def campaign_completed(
    kind: str,
    *,
    target_users: int,
    target_endpoints: int,
    sent_endpoints: int,
    failed_endpoints: int,
    stale_endpoints: int,
) -> bool:
    """Private summary only; customer endpoints are never published to ntfy."""
    return publish(
        f"TubeNotes {kind} push campaign completed",
        "\n".join(
            (
                f"Eligible users: {target_users}",
                f"Browser endpoints: {target_endpoints}",
                f"Sent: {sent_endpoints}",
                f"Failed: {failed_endpoints}",
                f"Expired removed: {stale_endpoints}",
            )
        ),
        tags=("bell", "chart_with_upwards_trend"),
        priority="default",
    )


def email_delivery_failed(to: str, subject: str) -> bool:
    local, _, domain = to.partition("@")
    reference = f"{local[:2] or 'user'}***@{domain or 'unknown'}"
    return publish(
        "TubeNotes email delivery failed",
        f"Recipient: {reference}\nSubject: {subject[:160]}",
        tags=("warning", "email"),
        priority="high",
    )
