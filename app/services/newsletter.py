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
import re
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


_DURATION_RE = re.compile(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$")


def _duration(value: str | None) -> str | None:
    """Render YouTube's ISO-8601 duration in the compact web-card format."""
    if not value or not (match := _DURATION_RE.fullmatch(value)):
        return None
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    parts = ([f"{hours}h"] if hours else []) + ([f"{minutes}m"] if minutes else [])
    if seconds or not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts)


def _views(value: int | None) -> str | None:
    if value is None:
        return None
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M views"
    if value >= 1_000:
        return f"{value / 1_000:.1f}".rstrip("0").rstrip(".") + "K views"
    return f"{value:,} views"


def _card_text(value: str, limit: int) -> str:
    """Keep email-grid card heights consistent without cutting a word midway."""
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    shortened = text[: limit - 1].rsplit(" ", 1)[0].rstrip()
    return (shortened or text[: limit - 1]).rstrip() + "…"


def _summarize_url(video_url: str) -> str:
    """Open the web app with this YouTube URL already filled in."""
    return f"{settings.APP_BASE_URL.rstrip('/')}/?youtube_url={quote(video_url, safe='')}"


def newsletter_content(
    user: User,
    videos: tuple[trending.TrendingVideo, ...],
    *,
    preview: bool = False,
) -> tuple[str, str, str]:
    """Build an inbox-safe HTML issue plus a plain-text alternative."""
    name = " ".join((user.full_name or "").split()) or user.email.split("@", 1)[0]
    card_cells: list[str] = []
    plain_rows = []
    for index, video in enumerate(videos, start=1):
        # YouTube API titles may already contain HTML entities. Decode once,
        # then escape for our email so Gmail does not show literal &quot; text.
        title_text = html.unescape(video.title)
        channel_text = html.unescape(video.channel)
        title = html.escape(_card_text(title_text, 78))
        channel = html.escape(_card_text(channel_text, 42))
        summary_url = html.escape(_summarize_url(video.url), quote=True)
        image = html.escape(video.thumbnail, quote=True)
        thumbnail = (
            f'<img src="{image}" alt="" width="100%" style="display:block;width:100%;height:auto;border:0;" />'
            if image
            else ""
        )
        views = _views(video.views)
        published = _published(video.published_at)
        duration = _duration(video.duration)
        publish_line = f"Published: {published}" + (f" &middot; Duration: {duration}" if duration else "")
        card_cells.append(f"""
        <td class="podcast-column" valign="top" width="33.33%" style="width:33.33%;padding:0 5px 12px;">
          <table role="presentation" width="100%" height="300" cellspacing="0" cellpadding="0" style="height:300px;border:1px solid #e6ddf5;border-radius:10px;background:#ffffff;overflow:hidden;">
            <tr><td style="line-height:0;font-size:0;">{thumbnail}</td></tr>
            <tr><td height="184" valign="top" style="height:184px;padding:11px 11px 12px;">
              <div style="margin:0 0 6px;color:#7c3aed;font-size:10px;font-weight:800;letter-spacing:.03em;">New &middot; #{index} recent upload</div>
              <a href="{summary_url}" style="color:#24123e;font-size:13px;line-height:1.35;font-weight:750;text-decoration:none;">{title}</a>
              <div style="margin-top:7px;color:#6f647c;font-size:11px;line-height:1.4;">{channel}{f' &middot; {views}' if views else ''}</div>
              <div style="margin-top:4px;color:#85798e;font-size:10.5px;line-height:1.4;">{publish_line}</div>
              <a href="{summary_url}" style="display:inline-block;margin-top:9px;color:#7335d8;font-size:11px;font-weight:750;text-decoration:none;">Summarize Video</a>
            </td></tr>
          </table>
        </td>""")
        plain_rows.append(f"{title_text} — {channel_text} ({video.url})")

    # Email clients do not reliably support CSS Grid. A presentation table is
    # intentionally used for three desktop cards per row, with a media-query
    # fallback that makes each card readable on a narrow phone screen.
    rows: list[str] = []
    for start in range(0, len(card_cells), 3):
        row = card_cells[start : start + 3]
        row.extend(['<td class="podcast-column podcast-empty" width="33.33%" style="width:33.33%;padding:0 5px 12px;">&nbsp;</td>'] * (3 - len(row)))
        rows.append("<tr>" + "".join(row) + "</tr>")
    cards = "".join(rows)

    unsubscribe = unsubscribe_url(user)
    subject = "Fresh podcast picks from TubeNotes"
    footer_plain = (
        "This is a TubeNotes newsletter preview; no subscription preference was changed."
        if preview
        else "You are receiving this because you opted in to TubeNotes updates. "
        f"Unsubscribe: {unsubscribe}"
    )
    footer_html = (
        "This is a TubeNotes newsletter preview; no subscription preference was changed."
        if preview
        else 'You are receiving this because you chose TubeNotes podcast updates. '
        f'<a href="{html.escape(unsubscribe, quote=True)}" style="color:#6730c5;">Unsubscribe</a> anytime.'
    )
    plain = (
        f"Hi {name},\n\nHere are fresh long-form podcast uploads picked for you:\n\n"
        + "\n".join(plain_rows)
        + f"\n\nSummarize any video with TubeNotes: {settings.APP_BASE_URL}\n"
        + f"\n{footer_plain}\n"
        + (f"\n{settings.MARKETING_ADDRESS}\n" if settings.MARKETING_ADDRESS.strip() else "")
    )
    address = html.escape(settings.MARKETING_ADDRESS.strip())
    html_body = f"""<!doctype html><html><head><style>@media screen and (max-width:600px){{.podcast-column{{display:block!important;width:100%!important;box-sizing:border-box!important;padding:0 0 12px!important}}.podcast-empty{{display:none!important}}}}</style></head><body style="margin:0;padding:0;background:#f6f2fb;font-family:Arial,sans-serif;color:#281b3d;">
      <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="padding:26px 10px;background:#f6f2fb;"><tr><td align="center">
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:620px;background:#fff;border:1px solid #e6ddf5;border-radius:16px;overflow:hidden;">
          <tr><td style="padding:20px 28px;background:linear-gradient(120deg,#7135e8,#d735b2);color:#fff;"><div style="font-size:24px;font-weight:800;">&#9654; TubeNotes</div><div style="margin-top:4px;font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;">YouTube Summarizer &amp; PDF Generator</div></td></tr>
          <tr><td style="padding:27px 28px 16px;"><h1 style="margin:0 0 8px;color:#2b1748;font-size:24px;line-height:1.25;">Fresh podcast picks</h1><p style="margin:0 0 20px;color:#685d78;font-size:15px;line-height:1.55;">Hi {html.escape(name)}, here are a few recent long-form podcasts to explore this week.</p>
            <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin:0 -5px;width:calc(100% + 10px);">{cards}</table>
            <p style="margin:8px 0 10px;text-align:center;"><a href="{html.escape(settings.APP_BASE_URL, quote=True)}" style="display:inline-block;padding:12px 16px;border-radius:9px;background:#7135e8;color:#fff;font-size:13px;font-weight:700;text-decoration:none;">Summarize a video</a></p>
          </td></tr>
          <tr><td style="padding:16px 28px;background:#faf8ff;border-top:1px solid #eee8f8;color:#746783;font-size:11px;line-height:1.55;">{footer_html}{f'<br>{address}' if address else ''}</td></tr>
        </table>
      </td></tr></table>
    </body></html>"""
    return subject, plain, html_body


def _latest_videos() -> tuple[trending.TrendingVideo, ...]:
    async def fetch_latest() -> trending.TrendingPage:
        # The shared HTTPX client belongs to this asyncio event loop.  Fetch
        # and close it in the same run; closing in a second ``asyncio.run``
        # tries to touch the first, already-closed loop.
        try:
            return await trending.fetch(
                settings.YOUTUBE_TRENDING_DEFAULT_REGION,
                limit=max(6, min(settings.NEWSLETTER_MAX_VIDEOS, 10)),
                sort="latest",
            )
        finally:
            # The command is a one-shot process, but closing avoids noisy
            # resource warnings when it is run manually or in tests.
            await trending.close_client()

    return asyncio.run(fetch_latest()).videos


def send_test_email(recipient: str) -> dict[str, int | str]:
    """Send one operator-requested visual preview without changing audience state."""
    recipient = recipient.strip().lower()
    if "@" not in recipient or recipient.startswith("@") or recipient.endswith("@"):
        return {"status": "invalid_recipient", "recipients": 0, "delivered": 0, "failed": 0}
    if settings.EMAIL_BACKEND != "smtp" or not settings.SMTP_HOST:
        logger.error("Newsletter preview requested but SMTP is not configured.")
        return {"status": "smtp_not_configured", "recipients": 0, "delivered": 0, "failed": 0}
    if not settings.MARKETING_ADDRESS.strip():
        logger.error("Newsletter preview requested but MARKETING_ADDRESS is blank.")
        return {"status": "marketing_address_missing", "recipients": 0, "delivered": 0, "failed": 0}
    try:
        videos = _latest_videos()
    except trending.TrendingUnavailable as exc:
        logger.warning("Newsletter preview content unavailable: %s", exc)
        return {"status": "content_unavailable", "recipients": 0, "delivered": 0, "failed": 0}

    # This in-memory user is deliberately never persisted: a preview does not
    # create consent, alter an unsubscribe preference, or reserve a schedule.
    preview_user = User(id="newsletter-preview", email=recipient, password_hash="")
    subject, plain, html_body = newsletter_content(preview_user, videos, preview=True)
    ok = email.send_email(recipient, f"[Preview] {subject}", plain, html_body=html_body, reply_to=settings.SUPPORT_EMAIL)
    return {
        "status": "test_sent" if ok else "test_failed",
        "recipients": 1,
        "delivered": int(ok),
        "failed": int(not ok),
    }


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

    users = db.execute(
        select(User).where(
            User.is_active.is_(True),
            User.newsletter_opt_in.is_(True),
            User.newsletter_unsubscribed_at.is_(None),
        )
    ).scalars().all()
    try:
        videos = _latest_videos() if users else ()
    except trending.TrendingUnavailable as exc:
        logger.warning("Newsletter content unavailable: %s", exc)
        # Do not claim this schedule key when the content provider is down:
        # an operator can safely retry while the issue is still relevant.
        return {"status": "content_unavailable", "recipients": len(users), "delivered": 0, "failed": 0}

    # Claim only after the external content request succeeds.  A duplicate
    # cron process may do the same harmless fetch, but only one can send.
    key = schedule_key(current)
    run = NewsletterRun(schedule_key=key, recipient_count=len(users))
    db.add(run)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return {"status": "already_sent", "recipients": 0, "delivered": 0, "failed": 0}

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
