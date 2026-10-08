"""Transactional email delivery and TubeNotes-branded notification templates."""
from __future__ import annotations

import html
import logging
import smtplib
from datetime import datetime
from email.message import EmailMessage

from app.config import settings
from app.models import User
from app.services import ntfy

logger = logging.getLogger("trialguard.email")


def send_email(
    to: str,
    subject: str,
    body: str,
    *,
    html_body: str | None = None,
    reply_to: str | None = None,
) -> bool:
    """Send one transactional email without letting mail outages break the app."""
    if settings.EMAIL_BACKEND == "console" or not settings.SMTP_HOST:
        logger.info("EMAIL to=%s subject=%s\n%s", to, subject, body)
        return True

    msg = EmailMessage()
    msg["From"] = settings.EMAIL_FROM
    msg["To"] = to
    msg["Subject"] = subject
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    try:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15) as smtp:
            if settings.SMTP_STARTTLS:
                smtp.starttls()
            if settings.SMTP_USER:
                smtp.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            smtp.send_message(msg)
        return True
    except Exception:  # pragma: no cover - network
        logger.exception("Failed to send email to %s", to)
        ntfy.email_delivery_failed(to, subject)
        return False


def _name(user: User) -> str:
    """Use a friendly name while never putting profile data in HTML unsafely."""
    name = " ".join((user.full_name or "").split())
    return name or user.email.split("@", 1)[0]


def _money(amount_subunits: int | None, currency: str | None) -> str:
    if amount_subunits is None or not currency:
        return "your selected plan"
    amount = amount_subunits / 100
    if currency.upper() == "INR":
        return f"₹{amount:,.0f}"
    if currency.upper() == "USD":
        return f"${amount:,.0f}"
    return f"{amount:,.2f} {currency.upper()}"


def _date(value: datetime | None) -> str | None:
    return value.strftime("%d %b %Y") if value is not None else None


def _brand_email(*, title: str, greeting: str, paragraphs: list[str], cta: str | None = None) -> str:
    """A compact HTML template that renders reliably in major inboxes."""
    safe_title = html.escape(title)
    safe_greeting = html.escape(greeting)
    copy = "".join(f"<p>{html.escape(text)}</p>" for text in paragraphs)
    button = ""
    if cta:
        button = (
            f'<p class="cta"><a href="{html.escape(settings.APP_BASE_URL, quote=True)}">'
            f"{html.escape(cta)}</a></p>"
        )
    return f"""<!doctype html>
<html><body style="margin:0;padding:0;background:#f5f2fb;font-family:Arial,sans-serif;color:#28213b;">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="padding:28px 12px;background:#f5f2fb;"><tr><td align="center">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:600px;background:#ffffff;border:1px solid #e8e0f7;border-radius:16px;overflow:hidden;">
      <tr><td style="padding:18px 30px 17px;background:linear-gradient(120deg,#7135e8,#d735b2);color:#ffffff;">
        <div style="font-size:25px;font-weight:800;letter-spacing:-0.4px;line-height:1.15;">▶ TubeNotes</div>
        <div style="margin-top:4px;font-size:13px;font-weight:600;letter-spacing:0.2px;line-height:1.2;">YouTube Summarizer</div>
      </td></tr>
      <tr><td style="padding:32px 30px 22px;"><h1 style="margin:0 0 18px;font-size:25px;line-height:1.25;color:#2a1748;">{safe_title}</h1>
        <p style="margin:0 0 16px;font-size:16px;line-height:1.55;">{safe_greeting}</p>
        <div style="font-size:15px;line-height:1.6;color:#5d536e;">{copy}</div>{button}
      </td></tr>
      <tr><td style="padding:17px 30px;background:#faf8ff;border-top:1px solid #eee8f8;font-size:12px;line-height:1.5;color:#81758e;">This is a transactional email from TubeNotes. Please do not reply to this message.</td></tr>
    </table>
  </td></tr></table>
  <style>.cta{{margin:24px 0 5px!important}}.cta a{{display:inline-block;padding:12px 18px;border-radius:9px;background:#7135e8;color:#fff!important;font-weight:700;text-decoration:none}}</style>
</body></html>"""


def _send_notification(user: User, subject: str, title: str, paragraphs: list[str], *, cta: str | None = "Open TubeNotes") -> bool:
    greeting = f"Hi {_name(user)},"
    plain = "\n\n".join([greeting, *paragraphs, f"{settings.APP_NAME}: {settings.APP_BASE_URL}"])
    return send_email(
        user.email,
        subject,
        plain,
        html_body=_brand_email(title=title, greeting=greeting, paragraphs=paragraphs, cta=cta),
    )


def send_welcome_email(user: User, trial_limit: int) -> bool:
    return _send_notification(
        user,
        f"Welcome to {settings.APP_NAME}",
        "Welcome to TubeNotes",
        [
            f"Your account is ready. You have {trial_limit} free videos to explore TubeNotes before a subscription is needed.",
            "Paste a YouTube link to get a quick summary, FullNotes PDF and support for 40+ languages.",
        ],
        cta="Start your first summary",
    )


def send_trial_exhausted_email(user: User, trial_limit: int) -> bool:
    return _send_notification(
        user,
        "Your free TubeNotes trials are complete",
        "Your free trials are complete",
        [
            f"You have used all {trial_limit} free videos included with your TubeNotes account.",
            "Subscribe to continue with unlimited summaries, FullNotes PDFs and 40+ languages. You can cancel anytime.",
        ],
        cta="View subscription",
    )


def send_trial_remaining_email(user: User, trials_remaining: int) -> bool:
    """Send a friendly one-time reminder at a configured trial milestone."""
    plural = "trial" if trials_remaining == 1 else "trials"
    return _send_notification(
        user,
        f"{trials_remaining} free TubeNotes {plural} remaining",
        f"You have {trials_remaining} free {plural} remaining",
        [
            f"You still have {trials_remaining} free TubeNotes {plural} to use.",
            "Paste a YouTube link whenever you need a quick summary or FullNotes PDF.",
        ],
        cta="Use your free trials",
    )


def send_payment_success_email(
    user: User,
    *,
    amount_subunits: int | None,
    currency: str | None,
    renewal: bool,
    period_end: datetime | None,
) -> bool:
    amount = _money(amount_subunits, currency)
    renewal_date = _date(period_end)
    if renewal:
        title = "Your TubeNotes Pro subscription was renewed"
        paragraphs = [
            f"Your subscription payment of {amount} was received successfully.",
            (f"Your Pro access is active through {renewal_date}." if renewal_date else "Your Pro access remains active."),
        ]
    else:
        title = "Welcome to TubeNotes Pro"
        paragraphs = [
            f"Your subscription payment of {amount} was received successfully.",
            (f"Your Pro access is active through {renewal_date}." if renewal_date else "Your Pro access is now active."),
            "You can now enjoy unlimited summaries, FullNotes PDFs and 40+ languages.",
        ]
    return _send_notification(user, title, title, paragraphs, cta="Open TubeNotes")


def send_payment_attention_email(user: User) -> bool:
    return _send_notification(
        user,
        "Action needed: your TubeNotes Pro payment",
        "We could not process your subscription payment",
        [
            "Your TubeNotes Pro renewal needs attention. Please check your payment method in Razorpay to keep uninterrupted Pro access.",
            "Once Razorpay confirms the payment, your subscription will continue automatically.",
        ],
        cta="Open TubeNotes",
    )


def send_cancellation_scheduled_email(user: User, period_end: datetime | None) -> bool:
    end_date = _date(period_end)
    return _send_notification(
        user,
        "Your TubeNotes Pro cancellation is scheduled",
        "Your cancellation is scheduled",
        [
            (f"Your Pro access will remain available until {end_date}." if end_date else "Your Pro access will remain available until the end of the current billing period."),
            "There will be no further renewal after that date unless you resume your subscription through Razorpay.",
        ],
        cta="Open TubeNotes",
    )


def send_subscription_ended_email(user: User) -> bool:
    return _send_notification(
        user,
        "Your TubeNotes Pro subscription has ended",
        "Your TubeNotes Pro subscription has ended",
        [
            "Your Pro subscription is no longer active. Your account remains available on the free plan.",
            "You can subscribe again whenever you are ready to continue with unlimited summaries and FullNotes PDFs.",
        ],
        cta="View subscription",
    )


def send_verification_email(to: str, token: str) -> None:
    link = f"{settings.APP_BASE_URL}{settings.API_PREFIX}/auth/verify-email?token={token}"
    send_email(
        to,
        "Verify your email",
        f"Welcome! Confirm your address to activate your free trials:\n\n{link}\n\n"
        f"This link expires in {settings.EMAIL_VERIFY_TTL_HOURS} hours.",
    )


def send_password_reset_email(to: str, token: str) -> None:
    link = f"{settings.APP_BASE_URL}/reset-password?token={token}"
    send_email(
        to,
        "Reset your password",
        f"Use this link to choose a new password:\n\n{link}\n\n"
        f"It expires in {settings.PASSWORD_RESET_TTL_MINUTES} minutes. "
        "If you did not request this, you can ignore this email.",
    )
