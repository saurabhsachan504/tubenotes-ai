"""Opt-in latest-podcast newsletter delivery.

This module is intentionally driven by host cron, rather than an in-process
timer: web workers may restart or be scaled, while a unique schedule key keeps
a repeated cron invocation from delivering the same issue twice.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import logging
from datetime import datetime, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import NewsletterRun, User
from app.services import email, trending

logger = logging.getLogger("trialguard.newsletter")
IST = ZoneInfo("Asia/Kolkata")
SCHEDULE_WEEKDAYS = {2, 6}  # Wednesday and Sunday, datetime.weekday()


def now() -> datetime:
    return datetime.now(IST)


def is_due(at: datetime | None = None) -> bool:
    current = (at or now()).astimezone(IST)
    return current.weekday() in SCHEDULE_WEEKDAYS and current.hour == 9 and current.minute == 0


def schedule_key(at: datetime | None = None) -> str:
    return f"latest-podcasts:{(at or now()).astimezone(IST).date().isoformat()}"


def unsubscribe_token(user_id: str) -> str:
    """Return a stable signed token suitable for a one-click unsubscribe URL."""
    signature = hmac.new(
        settings.SECRET_KEY.encode("utf-8"), user_id.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"{user_id}.{signature}"


def user_id_from_unsubscribe_token(token: str) -> str | None:
    user_id, separator, signature = token.rpartition(".")
    if not separator or not user_id or len(signature) != 64:
        return None
    expected = unsubscribe_token(user_id).rpartition(".")[2]
    return user_id if hmac.compare_digest(signature, expected) else None


def unsubscribe_url(user: User) -> str:
    token = quote(unsubscribe_token(user.id), safe="")
    return f"{settings.APP_BASE_URL.rstrip('/')}{settings.API_PREFIX}/newsletter/unsubscribe?token={token}"


def _published(value: str | None) -> str:
    if not value:
        return "Recently uploaded"
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%d %b %Y")
    except ValueError:
        return "Recently uploaded"


def newsletter_content(user: User, videos: tuple[trending.TrendingVideo, ...]) -> tuple[str, str, str]:
    """Build an inbox-safe HTML issue plus a plain-text alternative."""
    name = " ".join((user.full_name or "").split()) or user.email.split("@", 1)[0]
    cards = ""
    plain_rows = []
    for video in videos:
        title = html.escape(video.title)
        channel = html.escape(video.channel)
        url = html.escape(video.url, quote=True)
        image = html.escape(video.thumbnail, quote=True)
        thumbnail = (
            f'<img src="{image}" alt="" width="138" style="display:block;width:138px;max-width:138px;height:78px;object-fit:cover;border:0;border-radius:8px;" />'
            if image
            else ""
        )
        cards += f"""
        <tr><td style="padding:0 0 12px;">
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border:1px solid #e6ddf5;border-radius:12px;background:#ffffff;"><tr>
            <td width="150" valign="top" style="padding:10px 2px 10px 10px;">{thumbnail}</td>
            <td valign="top" style="padding:11px 12px 10px 10px;">
              <a href="{url}" style="color:#24123e;font-size:14px;line-height:1.35;font-weight:700;text-decoration:none;">{title}</a>
              <div style="margin-top:5px;color:#746783;font-size:12px;line-height:1.35;">{channel} &middot; {_published(video.published_at)}</div>
              <a href="{url}" style="display:inline-block;margin-top:8px;color:#7c3aed;font-size:12px;font-weight:700;text-decoration:none;">Open on YouTube &rarr;</a>
            </td>
          </tr></table>
        </td></tr>"""
        plain_rows.append(f"{video.title} — {video.channel} ({video.url})")

    unsubscribe = unsubscribe_url(user)
    subject = "Fresh podcast picks from TubeNotes"
    plain = (
        f"Hi {name},\n\nHere are fresh long-form podcast uploads picked for you:\n\n"
        + "\n".join(plain_rows)
        + f"\n\nSummarize any video with TubeNotes: {settings.APP_BASE_URL}\n"
        + f"\nYou are receiving this because you opted in to TubeNotes updates. Unsubscribe: {unsubscribe}\n"
        + (f"\n{settings.MARKETING_ADDRESS}\n" if settings.MARKETING_ADDRESS.strip() else "")
    )
    address = html.escape(settings.MARKETING_ADDRESS.strip())
    html_body = f"""<!doctype html><html><body style="margin:0;padding:0;background:#f6f2fb;font-family:Arial,sans-serif;color:#281b3d;">
      <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="padding:26px 10px;background:#f6f2fb;"><tr><td align="center">
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:620px;background:#fff;border:1px solid #e6ddf5;border-radius:16px;overflow:hidden;">
          <tr><td style="padding:20px 28px;background:linear-gradient(120deg,#7135e8,#d735b2);color:#fff;"><div style="font-size:24px;font-weight:800;">&#9654; TubeNotes</div><div style="margin-top:4px;font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;">New on YouTube</div></td></tr>
          <tr><td style="padding:27px 28px 16px;"><h1 style="margin:0 0 8px;color:#2b1748;font-size:24px;line-height:1.25;">Fresh podcast picks</h1><p style="margin:0 0 20px;color:#685d78;font-size:15px;line-height:1.55;">Hi {html.escape(name)}, here are a few recent long-form podcasts to explore this week.</p>
            <table role="presentation" width="100%" cellspacing="0" cellpadding="0">{cards}</table>
            <p style="margin:8px 0 10px;"><a href="{html.escape(settings.APP_BASE_URL, quote=True)}" style="display:inline-block;padding:12px 16px;border-radius:9px;background:#7135e8;color:#fff;font-size:13px;font-weight:700;text-decoration:none;">Summarize a video</a></p>
          </td></tr>
          <tr><td style="padding:16px 28px;background:#faf8ff;border-top:1px solid #eee8f8;color:#746783;font-size:11px;line-height:1.55;">You are receiving this because you chose TubeNotes podcast updates. <a href="{html.escape(unsubscribe, quote=True)}" style="color:#6730c5;">Unsubscribe</a> anytime.{f'<br>{address}' if address else ''}</td></tr>
        </table>
      </td></tr></table>
    </body></html>"""
    return subject, plain, html_body


def _latest_videos() -> tuple[trending.TrendingVideo, ...]:
    async def fetch_latest() -> trending.TrendingPage:
        return await trending.fetch(
            settings.YOUTUBE_TRENDING_DEFAULT_REGION,
            limit=max(1, min(settings.NEWSLETTER_MAX_VIDEOS, 10)),
            sort="latest",
        )

    try:
        return asyncio.run(fetch_latest()).videos
    finally:
        # The command is a one-shot process, but closing avoids noisy resource
        # warnings when it is run manually or in tests.
        asyncio.run(trending.close_client())


def run_due(db: Session, *, at: datetime | None = None, force: bool = False) -> dict[str, int | str]:
    """Send one due issue.  Returns counts useful for cron logs and tests."""
    current = (at or now()).astimezone(IST)
    if not settings.NEWSLETTER_ENABLED:
        return {"status": "disabled", "recipients": 0, "delivered": 0, "failed": 0}
    if not force and not is_due(current):
        return {"status": "not_due", "recipients": 0, "delivered": 0, "failed": 0}
    if settings.EMAIL_BACKEND != "smtp" or not settings.SMTP_HOST:
        logger.error("Newsletter is enabled but SMTP is not configured; nothing sent.")
        return {"status": "smtp_not_configured", "recipients": 0, "delivered": 0, "failed": 0}
    if not settings.MARKETING_ADDRESS.strip():
        logger.error("Newsletter is enabled but MARKETING_ADDRESS is blank; nothing sent.")
        return {"status": "marketing_address_missing", "recipients": 0, "delivered": 0, "failed": 0}

    key = schedule_key(current)
    run = NewsletterRun(schedule_key=key)
    db.add(run)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return {"status": "already_sent", "recipients": 0, "delivered": 0, "failed": 0}

    users = db.execute(
        select(User).where(
            User.is_active.is_(True),
            User.newsletter_opt_in.is_(True),
            User.newsletter_unsubscribed_at.is_(None),
        )
    ).scalars().all()
    run.recipient_count = len(users)
    try:
        videos = _latest_videos() if users else ()
    except trending.TrendingUnavailable as exc:
        logger.warning("Newsletter content unavailable: %s", exc)
        run.status = "content_unavailable"
        run.completed_at = datetime.now(timezone.utc)
        db.commit()
        return {"status": run.status, "recipients": len(users), "delivered": 0, "failed": 0}

    for user in users:
        subject, plain, html_body = newsletter_content(user, videos)
        if email.send_email(user.email, subject, plain, html_body=html_body, reply_to=settings.SUPPORT_EMAIL):
            run.delivered_count += 1
        else:
            run.failed_count += 1
    run.status = "completed"
    run.completed_at = datetime.now(timezone.utc)
    db.commit()
    return {"status": run.status, "recipients": len(users), "delivered": run.delivered_count, "failed": run.failed_count}
