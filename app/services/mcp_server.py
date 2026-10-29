"""TubeNotes tools exposed to ChatGPT and Codex through MCP.

This deliberately calls the same transcript, model, translation and PDF
services as the website.  Its trial allowance is separate: the OAuth connect
page binds every token to a coarse browser-machine hash, so account switching
cannot mint another free MCP allowance.
"""
import re
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from urllib.parse import urlencode, urlparse

import jwt
from sqlalchemy import select
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from starlette.concurrency import run_in_threadpool
from fastapi import HTTPException

from app.config import settings
from app.database import SessionLocal
from app.models import McpOAuthClient, McpTranscriptCache, McpTrialLedger, McpTrialUsage, User
from app.routers.summarize import (
    SummarizeRequest,
    VideoRequest,
    _cache_lookup,
    _may_cache,
    _prepare_video,
    _resolve_target,
    _store_cached,
    _friendly,
    _video_id_or_400,
)
from app.schemas import DeviceFingerprint, EntitlementOut
from app.services import entitlements, output_cache, pdf, summarizer, translate, youtube
from app.services.mcp_oauth import MCP_SCOPE, TubeNotesAccessToken, TubeNotesOAuthProvider

_LANGUAGE_CODES = (
    "en", "hi", "bn", "gu", "kn", "ml", "mr", "pa", "ta", "te",
    "ur", "ar", "de", "es", "fr", "id", "it", "ja", "ko", "nl", "pt", "ru", "tr", "vi", "zh",
)
_LANGUAGE_OPTIONS = Literal[
    "en", "hi", "bn", "gu", "kn", "ml", "mr", "pa", "ta", "te",
    "ur", "ar", "de", "es", "fr", "id", "it", "ja", "ko", "nl", "pt", "ru", "tr", "vi", "zh",
]

_MCP_TOOL_SECURITY = [{"type": "oauth2", "scopes": [MCP_SCOPE]}]
logger = logging.getLogger("trialguard.mcp")
_MCP_TOOL_OUTPUT_SCHEMA = {
    "type": "object",
    # Results differ between the profile, summary, PDF, and error paths.  The
    # schema still declares that every structured result is an object, which
    # lets ChatGPT accept the `structuredContent` field on every path.
    "additionalProperties": True,
}


def _tool_input_schema(*, video: bool = False) -> dict[str, Any]:
    if not video:
        return {"type": "object", "properties": {}, "additionalProperties": False}
    return {
        "type": "object",
        "properties": {
            "video_url": {"type": "string", "description": "Public YouTube video URL."},
            "language": {
                "type": "string",
                "enum": list(_LANGUAGE_CODES),
                "description": "Required output language code chosen by the user, for example hi for Hindi or en for English. Never infer or default this value.",
            },
        },
        "required": ["video_url", "language"],
        "additionalProperties": False,
    }


def mcp_tool_definitions() -> list[dict[str, Any]]:
    """Return OAuth-aware tool descriptors for the public streamable endpoint.

    FastMCP 1.x can serve our OAuth endpoints, but predates per-tool OAuth
    metadata.  ChatGPT needs that metadata before it can show its account-link
    UI, so the public transport publishes the current MCP descriptor directly.
    """
    return [
        {
            "name": "get_tubenotes_profile",
            "title": "TubeNotes account",
            "description": "Use this to confirm the connected TubeNotes account and remaining video trials.",
            "inputSchema": _tool_input_schema(),
            "outputSchema": _MCP_TOOL_OUTPUT_SCHEMA,
            "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
            "securitySchemes": _MCP_TOOL_SECURITY,
            "_meta": {"openai/profile": True},
        },
        {
            "name": "summarize_youtube_video",
            "title": "Summarize YouTube video",
            "description": "Use this only after the user has explicitly selected an output language for a public YouTube video summary.",
            "inputSchema": _tool_input_schema(video=True),
            "outputSchema": _MCP_TOOL_OUTPUT_SCHEMA,
            "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True},
            "securitySchemes": _MCP_TOOL_SECURITY,
        },
        {
            "name": "create_video_pdf",
            "title": "Create video notes PDF",
            "description": "Use this only after the user has explicitly selected an output language for complete YouTube video notes PDF.",
            "inputSchema": _tool_input_schema(video=True),
            "outputSchema": _MCP_TOOL_OUTPUT_SCHEMA,
            "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True},
            "securitySchemes": _MCP_TOOL_SECURITY,
        },
    ]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _mcp_request_device(user_id: str) -> DeviceFingerprint:
    """Schema placeholder only; plugin billing uses OAuth-bound machine_hash."""
    return DeviceFingerprint(installation_id=f"chatgpt-mcp:{user_id}", platform="ChatGPT plugin")


def _active_user(user_id: str) -> User:
    if not user_id:
        raise PermissionError("Connect your TubeNotes account before using this tool.")
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        if user is None or not user.is_active:
            raise PermissionError("Your TubeNotes account is unavailable.")
        # Return an ORM object whose scalar fields are already loaded. None of
        # this module touches relationships after the session is closed.
        db.expunge(user)
        return user
    finally:
        db.close()


def _current_user() -> User:
    token = get_access_token()
    return _active_user(str(getattr(token, "user_id", "") or ""))


def _current_machine_hash() -> str:
    token = get_access_token()
    value = str(getattr(token, "machine_hash", "") or "")
    if not value:
        raise PermissionError("Reconnect TubeNotes to apply the device trial limit.")
    return value


def _mcp_ledger(db, machine_hash: str, *, lock: bool = False) -> McpTrialLedger:
    statement = select(McpTrialLedger).where(McpTrialLedger.machine_hash == machine_hash)
    if lock and not settings.is_sqlite:
        statement = statement.with_for_update()
    row = db.execute(statement).scalar_one_or_none()
    if row is None:
        row = McpTrialLedger(machine_hash=machine_hash, trials_used=0)
        db.add(row)
        db.flush()
    return row


def _mcp_entitlement(db, user: User, machine_hash: str) -> EntitlementOut:
    """Plugin-only quota: twenty public videos per browser machine.

    Web/extension trials intentionally remain independent.  A TubeNotes
    subscription (or manual Pro grant) always takes precedence over this cap.
    """
    limit = settings.MCP_FREE_TRIAL_LIMIT
    # The checkout endpoint is an authenticated POST API, not a browser page.
    # Give ChatGPT a real user-facing destination; the TubeNotes account page
    # takes the signed-in customer through the normal secure checkout flow.
    upgrade_url = settings.APP_BASE_URL.rstrip("/")
    if not user.is_active:
        return EntitlementOut(
            allowed=False, reason="account_disabled", plan="blocked",
            trials_limit=limit, trials_used=0, trials_remaining=0,
            device_trials_used=0, device_trials_remaining=0, upgrade_url=upgrade_url,
        )
    if entitlements.active_manual_pro(db, user) is not None or entitlements.active_subscription(db, user) is not None:
        return EntitlementOut(
            allowed=True, reason="subscription_active", plan="subscription",
            trials_limit=limit, trials_used=0, trials_remaining=limit,
            device_trials_used=0, device_trials_remaining=limit,
        )
    ledger = _mcp_ledger(db, machine_hash)
    remaining = max(0, limit - ledger.trials_used)
    return EntitlementOut(
        allowed=remaining > 0,
        reason="trial_available" if remaining > 0 else "machine_trial_exhausted",
        plan="free_trial",
        trials_limit=limit,
        trials_used=ledger.trials_used,
        trials_remaining=remaining,
        device_trials_used=ledger.trials_used,
        device_trials_remaining=remaining,
        upgrade_url=upgrade_url,
    )


def _profile_payload(user: User, machine_hash: str) -> dict[str, Any]:
    db = SessionLocal()
    try:
        entitlement = _mcp_entitlement(db, user, machine_hash)
        return {
            "id": user.id,
            "email": user.email,
            "name": user.full_name or user.email,
            "plan": entitlement.plan,
            "trials_remaining": entitlement.trials_remaining,
            "profile": {"id": user.id, "name": user.full_name or user.email},
        }
    finally:
        db.close()


def _trial_status(entitlement: EntitlementOut, *, trial_consumed: bool) -> dict[str, Any] | None:
    """Give a precise, user-facing notice only when a trial was spent."""
    if not trial_consumed or entitlement.plan != "free_trial":
        return None
    remaining = entitlement.trials_remaining
    if remaining > 0:
        message = f"🎟️ Free trials left: {remaining} / {entitlement.trials_limit}"
    else:
        message = (
            f"You have used all {entitlement.trials_limit} free TubeNotes trials. "
            "Subscribe to continue."
        )
    status = {
        "trial_consumed": True,
        "trials_remaining": remaining,
        "trials_limit": entitlement.trials_limit,
        "message": message,
    }
    if remaining == 0:
        # The site completes sign-in and opens the user's account area, where
        # the normal server-side checkout flow chooses the correct plan.
        status["subscribe_url"] = settings.APP_BASE_URL.rstrip("/")
    return status


def _claim_mcp_trial(db, user: User, machine_hash: str, video_id: str, payload, mode: str):
    """Atomically unlock one video for an MCP browser machine and check cache."""
    ledger = _mcp_ledger(db, machine_hash, lock=True)
    prior = db.execute(
        select(McpTrialUsage).where(
            McpTrialUsage.machine_hash == machine_hash,
            McpTrialUsage.video_id == video_id,
        )
    ).scalar_one_or_none()
    trial_consumed = False
    if prior is None:
        entitlement = _mcp_entitlement(db, user, machine_hash)
        if not entitlement.allowed:
            raise HTTPException(
                status_code=402,
                detail={
                    "message": (
                        f"This device has used all {settings.MCP_FREE_TRIAL_LIMIT} free TubeNotes plugin trials. "
                        "Subscribe to continue."
                    ),
                    "entitlement": entitlement.model_dump(mode="json"),
                },
            )
        if entitlement.plan == "free_trial":
            ledger.trials_used += 1
            db.add(McpTrialUsage(machine_hash=machine_hash, user_id=user.id, video_id=video_id))
            db.flush()
            trial_consumed = True
        entitlement = _mcp_entitlement(db, user, machine_hash)
    else:
        # A video already unlocked on this browser remains available for its
        # PDF or a retry even after the twentieth new video was consumed.
        entitlement = _mcp_entitlement(db, user, machine_hash)
        if not entitlement.allowed:
            entitlement = entitlement.model_copy(update={"allowed": True, "reason": "already_unlocked"})
    db.commit()
    cached_row, cached_target, cached_model = _cache_lookup(db, payload, video_id, mode=mode)
    return entitlement, cached_row, cached_target, cached_model, trial_consumed


async def _claim(user: User, machine_hash: str, payload: VideoRequest, video_id: str, mode: str):
    db = SessionLocal()
    try:
        return await run_in_threadpool(
            _claim_mcp_trial,
            db,
            user,
            machine_hash,
            video_id,
            payload,
            mode,
        )
    finally:
        db.close()


def _as_utc(value: datetime) -> datetime:
    """SQLite returns naive timestamps while Postgres returns UTC-aware ones."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _load_mcp_transcript(user_id: str, video_id: str) -> youtube.Transcript | None:
    """Read one user's short-lived transcript; never share raw captions."""
    db = SessionLocal()
    try:
        row = db.scalar(
            select(McpTranscriptCache).where(
                McpTranscriptCache.user_id == user_id,
                McpTranscriptCache.video_id == video_id,
            )
        )
        if row is None:
            return None
        if _as_utc(row.expires_at) <= _now():
            db.delete(row)
            db.commit()
            return None
        row.last_used_at = _now()
        db.commit()
        return youtube.Transcript(
            text=row.text,
            language=row.language,
            is_generated=True,
            source=row.source,
        )
    finally:
        db.close()


def _store_mcp_transcript(user_id: str, video_id: str, transcript: youtube.Transcript) -> None:
    """Upsert a transcript after a successful fetch for the signed-in user."""
    db = SessionLocal()
    try:
        now = _now()
        expiry = now + timedelta(minutes=settings.MCP_TRANSCRIPT_CACHE_TTL_MINUTES)
        row = db.scalar(
            select(McpTranscriptCache).where(
                McpTranscriptCache.user_id == user_id,
                McpTranscriptCache.video_id == video_id,
            )
        )
        if row is None:
            row = McpTranscriptCache(
                user_id=user_id,
                video_id=video_id,
                text=transcript.text,
                language=transcript.language,
                source=transcript.source,
                expires_at=expiry,
                last_used_at=now,
            )
            db.add(row)
        else:
            row.text = transcript.text
            row.language = transcript.language
            row.source = transcript.source
            row.expires_at = expiry
            row.last_used_at = now
        db.commit()
    finally:
        db.close()


async def _prepare_mcp_video(user: User, payload: VideoRequest, video_id: str):
    """Reuse captions from the immediately preceding MCP summary/PDF request."""
    cached = await run_in_threadpool(_load_mcp_transcript, user.id, video_id)
    meta, transcript, metadata_ms, transcript_ms = await _prepare_video(
        payload, video_id, transcript_override=cached
    )
    if cached is None:
        # Store only after the fetch succeeded.  A failed/transient YouTube
        # response must never poison the subsequent PDF request.
        await run_in_threadpool(_store_mcp_transcript, user.id, video_id, transcript)
    else:
        logger.info("reused MCP transcript for %s (%s chars)", video_id, len(transcript.text))
    return meta, transcript, metadata_ms, transcript_ms


async def _create_text(
    user: User, machine_hash: str, video_url: str, language: str,
    mode: Literal["summary", "notes"], *, include_trial_status: bool = False,
    cache_mode: Literal["summary", "notes", "gemini_full_notes"] | None = None,
    notes_chunk_chars: int | None = None,
) -> dict[str, Any]:
    video_id = _video_id_or_400(video_url)
    output_mode = cache_mode or mode
    payload = (
        SummarizeRequest(url=video_url, target_lang=language, device=_mcp_request_device(user.id), mode="summary")
        if mode == "summary"
        else VideoRequest(url=video_url, target_lang=language, device=_mcp_request_device(user.id))
    )
    entitlement, cached, cached_target, cached_model, trial_consumed = await _claim(
        user, machine_hash, payload, video_id, output_mode
    )
    if cached is not None:
        meta = await youtube.fetch_metadata(video_id)
        text = cached.text
        if cached.translate_to:
            text = await translate.translate(text, cached.translate_to)
        result = {
            "video_id": video_id,
            "title": meta.title,
            "channel": meta.author or "YouTube",
            "thumbnail": meta.thumbnail,
            "language": cached_target,
            "language_name": summarizer.language_name(cached_target),
            "text": text,
            "cached": True,
            "entitlement": entitlement.model_dump(mode="json"),
        }
        if include_trial_status:
            result["trial_status"] = _trial_status(entitlement, trial_consumed=trial_consumed)
        return result

    meta, transcript, _metadata_ms, _transcript_ms = await _prepare_mcp_video(
        user, payload, video_id
    )
    detected = summarizer.detect_language(transcript.text, hint=transcript.language)
    target = _resolve_target(language, detected)
    model, write_language, translate_to = summarizer.plan_for(target)
    if mode == "summary":
        parts: list[str] = []
        async for part in summarizer.stream_summary(transcript.text, lang=write_language, mode="summary"):
            parts.append(part)
        text = "".join(parts)
    else:
        text = await summarizer.full_notes(
            transcript.text,
            lang=write_language,
            chunk_chars=notes_chunk_chars,
        )
    if not text.strip():
        raise RuntimeError("TubeNotes could not create an answer for this video. Please try again.")
    if translate_to:
        text = await translate.translate(text, translate_to)
    else:
        text, _fixed = await translate.ensure_language(text, target)
    if _may_cache(transcript, meta):
        await run_in_threadpool(
            _store_cached,
            video_id,
            output_mode,
            target,
            model,
            text,
            detected,
            len(transcript.text),
            transcript.source,
        )
    result = {
        "video_id": video_id,
        "title": meta.title,
        "channel": meta.author or "YouTube",
        "thumbnail": meta.thumbnail,
        "language": target,
        "language_name": summarizer.language_name(target),
        "text": text,
        "cached": False,
        "entitlement": entitlement.model_dump(mode="json"),
    }
    if include_trial_status:
        result["trial_status"] = _trial_status(entitlement, trial_consumed=trial_consumed)
    return result


async def _pdf_payload(
    user: User,
    machine_hash: str,
    video_url: str,
    language: str,
    *,
    include_trial_status: bool = False,
    pdf_style: Literal["plugin", "claude"] = "plugin",
    cache_mode: Literal["notes", "gemini_full_notes"] = "notes",
    notes_chunk_chars: int | None = None,
    external_download_only: bool = False,
) -> dict[str, Any]:
    if not settings.OUTPUT_CACHE_ENABLED:
        raise RuntimeError("PDF export is unavailable until TubeNotes output caching is enabled.")
    result = await _create_text(
        user,
        machine_hash,
        video_url,
        language,
        "notes",
        include_trial_status=include_trial_status,
        cache_mode=cache_mode,
        notes_chunk_chars=notes_chunk_chars,
    )
    token = create_pdf_download_token(
        user_id=user.id,
        video_id=result["video_id"],
        language=result["language"],
        style=pdf_style,
        output_mode=cache_mode,
    )
    payload = {
        "video_id": result["video_id"],
        "title": result["title"],
        "language": result["language"],
        "language_name": result["language_name"],
        "pdf_download_url": f"{settings.APP_BASE_URL.rstrip('/')}/mcp/download/{token}",
        "expires_in_minutes": settings.MCP_DOWNLOAD_TTL_MINUTES,
        "message": "Your PDF is ready. Open the secure download link before it expires.",
    }
    if external_download_only:
        # Gemini can create a Canvas document from a tool response. That document
        # is only its own short summary, not the PDF rendered by TubeNotes. Make
        # the delivery contract explicit in both structured fields and prose so
        # the client presents this signed link rather than making a substitute.
        payload.update(
            {
                "delivery_type": "tubenotes_premium_pdf_download",
                "must_use_pdf_download_url": True,
                "message": (
                    "Your complete TubeNotes premium PDF is ready. Present and open "
                    "pdf_download_url as the download link. Do not create, export, or "
                    "substitute a Canvas/document PDF from the summary."
                ),
            }
        )
    if include_trial_status:
        payload["trial_status"] = result.get("trial_status")
    return payload


def _oauth_challenge() -> str:
    metadata_url = f"{settings.mcp_public_url}/.well-known/oauth-protected-resource"
    return (
        f'Bearer resource_metadata="{metadata_url}", '
        'error="invalid_token", error_description="Connect your TubeNotes account to continue"'
    )


def _tool_result(payload: dict[str, Any], *, is_error: bool = False, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    # ChatGPT displays error content directly. A plain, useful sentence is more
    # reliable than asking it to decode an error-shaped JSON object, and avoids
    # the unhelpful "without an explanation" fallback for empty exceptions.
    text = (
        str(payload.get("message") or "TubeNotes could not complete this request. Please try again.")
        if is_error
        else json.dumps(payload, ensure_ascii=False)
    )
    result: dict[str, Any] = {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    }
    if not is_error:
        result["structuredContent"] = payload
    if meta:
        result["_meta"] = meta
    return result


def _tool_failure_message(exc: Exception) -> str:
    """Return the same kind of safe, human-readable failure as the web app."""
    if isinstance(exc, HTTPException):
        detail = exc.detail
        if isinstance(detail, str) and detail.strip():
            return detail.strip()
        if isinstance(detail, dict):
            message = str(detail.get("message") or "").strip()
            upgrade_url = str((detail.get("entitlement") or {}).get("upgrade_url") or "").strip()
            if message and upgrade_url:
                return f"{message} Subscribe here: {upgrade_url}"
            if message:
                return message
        return "TubeNotes could not process this request. Please try again."
    message = _friendly(exc).strip()
    return message if message and message != "Summary failed:" else "TubeNotes could not create the summary. Please try again."


def _authentication_required_result(message: str = "Connect your TubeNotes account to continue.") -> dict[str, Any]:
    return _tool_result(
        {"error": "authentication_required", "message": message},
        is_error=True,
        meta={"mcp/www_authenticate": [_oauth_challenge()]},
    )


def _selected_mcp_language(arguments: dict[str, Any]) -> str | None:
    """Accept only an explicit supported code; ``auto`` is intentionally invalid."""
    value = str(arguments.get("language", "")).strip().lower()
    return value if value in _LANGUAGE_CODES else None


def _pdf_style_for_client(client_id: str) -> Literal["plugin", "claude"]:
    """Select the premium design for first-party ChatGPT and Claude clients.

    Both ChatGPT and Claude currently use the OAuth-compatible ``/mcp/``
    transport.  The bearer token carries a client ID, and its registered OAuth
    callback is the reliable host identity - unlike a mutable User-Agent.
    """
    db = SessionLocal()
    try:
        client = db.get(McpOAuthClient, client_id)
        if client is None:
            return "plugin"
        try:
            metadata = json.loads(client.metadata_json)
        except (TypeError, ValueError):
            return "plugin"
        redirects = metadata.get("redirect_uris", []) if isinstance(metadata, dict) else []
        hosts = {
            (urlparse(uri).hostname or "").lower()
            for uri in redirects
            if isinstance(uri, str)
        }
        premium_hosts = (
            "claude.ai",
            "chatgpt.com",
            "chat.openai.com",
            "oauth.openai.com",
            "oauth-redirect.openai.com",
        )
        if any(
            host == trusted_host or host.endswith(f".{trusted_host}")
            for host in hosts
            for trusted_host in premium_hosts
        ):
            return "claude"
        return "plugin"
    finally:
        db.close()


async def call_public_mcp_tool(
    authorization: str | None,
    tool_name: str,
    arguments: dict[str, Any] | None,
) -> dict[str, Any]:
    """Call a tool after optional bearer-token verification for ChatGPT's MCP UI."""
    raw_token = ""
    if authorization and authorization.lower().startswith("bearer "):
        raw_token = authorization[7:].strip()
    token = await TubeNotesOAuthProvider().load_access_token(raw_token) if raw_token else None
    # Older Claude connections omit RFC 8707's optional resource indicator;
    # their opaque tokens were issued by this same server with resource=None.
    # Treat that legacy empty value as this MCP resource while still rejecting
    # a token explicitly issued for an unrelated resource.
    accepted_resources = {
        settings.mcp_public_url.rstrip("/"),
        settings.APP_BASE_URL.rstrip("/"),
        "",
    }
    if (
        token is None
        or MCP_SCOPE not in token.scopes
        or str(token.resource or "").rstrip("/") not in accepted_resources
    ):
        if token is None:
            reason = "no_bearer_token" if not raw_token else "unknown_or_expired_token"
        elif MCP_SCOPE not in token.scopes:
            reason = "scope_missing"
        else:
            reason = "unexpected_resource"
        logger.info("MCP tool %s rejected authentication: %s", tool_name, reason)
        return _authentication_required_result()
    machine_hash = str(getattr(token, "machine_hash", "") or "")
    if not machine_hash:
        return _authentication_required_result(
            "Reconnect TubeNotes to apply the 20-trial device limit."
        )

    try:
        user = _active_user(token.user_id)
        values = arguments or {}
        if tool_name == "get_tubenotes_profile":
            return _tool_result(_profile_payload(user, machine_hash))
        video_url = str(values.get("video_url", "")).strip()
        language = _selected_mcp_language(values)
        if tool_name in {"summarize_youtube_video", "create_video_pdf"} and language is None:
            return _tool_result(
                {
                    "error": "language_required",
                    "message": "Ask the user which language they want (for example Hindi or English), then call TubeNotes again with that selected language code.",
                },
                is_error=True,
            )
        if tool_name == "summarize_youtube_video":
            assert language is not None
            result = await _create_text(
                user, machine_hash, video_url, language, "summary", include_trial_status=True
            )
            payload = {
                **{key: value for key, value in result.items() if key != "text"},
                "summary": result["text"],
                "open_in_tubenotes": f"{settings.APP_BASE_URL.rstrip('/')}/?{urlencode({'youtube_url': video_url})}",
            }
            return _tool_result(payload)
        if tool_name == "create_video_pdf":
            assert language is not None
            return _tool_result(
                await _pdf_payload(
                    user,
                    machine_hash,
                    video_url,
                    language,
                    include_trial_status=True,
                    pdf_style=_pdf_style_for_client(str(token.client_id or "")),
                )
            )
        return _tool_result({"error": "unknown_tool", "message": "Unknown TubeNotes tool."}, is_error=True)
    except Exception as exc:
        logger.exception("MCP tool %s failed for TubeNotes user %s", tool_name, token.user_id)
        return _tool_result(
            {"error": "tool_failed", "message": _tool_failure_message(exc)},
            is_error=True,
        )


def create_pdf_download_token(
    *,
    user_id: str,
    video_id: str,
    language: str,
    style: Literal["plugin", "claude"] = "plugin",
    output_mode: Literal["notes", "gemini_full_notes"] = "notes",
) -> str:
    if style not in {"plugin", "claude"}:
        raise ValueError("unsupported plugin PDF style")
    if output_mode not in {"notes", "gemini_full_notes"}:
        raise ValueError("unsupported PDF output mode")
    expiry = _now() + timedelta(minutes=settings.MCP_DOWNLOAD_TTL_MINUTES)
    return jwt.encode(
        {
            "sub": user_id,
            "video_id": video_id,
            "language": language,
            "style": style,
            "output_mode": output_mode,
            "type": "mcp_pdf_download",
            "iss": settings.APP_NAME,
            "exp": int(expiry.timestamp()),
        },
        settings.SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )


def decode_pdf_download_token(token: str) -> dict[str, str]:
    payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM], options={"require": ["exp", "sub"]})
    if payload.get("type") != "mcp_pdf_download":
        raise jwt.InvalidTokenError("wrong token type")
    return payload


def _video_selection_message() -> str:
    return "Would you like a Summary or a Full PDF, and in which language?"


def _create_video_selection_token(*, user_id: str, video_id: str) -> str:
    """Create a short-lived, URL-bound first step for Gemini's video flow."""
    expiry = _now() + timedelta(minutes=settings.MCP_DOWNLOAD_TTL_MINUTES)
    return jwt.encode(
        {
            "sub": user_id,
            "video_id": video_id,
            "stage": "choose_output",
            "type": "mcp_video_selection",
            "iss": settings.APP_NAME,
            "exp": int(expiry.timestamp()),
        },
        settings.SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )


def _approve_video_selection(
    token: str,
    *,
    user_id: str,
    video_url: str,
    output_type: Literal["summary", "pdf"],
    language: str,
) -> str:
    """Turn a pending selection into the only token accepted by work tools."""
    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
            options={"require": ["exp", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise ValueError("Your selection has expired. Please choose Summary or Full PDF again.") from exc
    if (
        payload.get("type") != "mcp_video_selection"
        or payload.get("stage") != "choose_output"
        or str(payload.get("sub", "")) != user_id
        or str(payload.get("video_id", "")) != _video_id_or_400(video_url)
    ):
        raise ValueError("This selection does not match the requested video. Please choose again.")
    expiry = _now() + timedelta(minutes=settings.MCP_DOWNLOAD_TTL_MINUTES)
    return jwt.encode(
        {
            "sub": user_id,
            "video_id": str(payload["video_id"]),
            "output_type": output_type,
            "language": language,
            "stage": "approved",
            "type": "mcp_video_selection",
            "iss": settings.APP_NAME,
            "exp": int(expiry.timestamp()),
        },
        settings.SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )


def _selection_is_approved(
    token: str,
    *,
    user_id: str,
    video_url: str,
    output_type: Literal["summary", "pdf"],
    language: str,
) -> bool:
    """Whether a work call follows an explicit Gemini selection handshake."""
    if not token:
        return False
    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
            options={"require": ["exp", "sub"]},
        )
    except jwt.PyJWTError:
        return False
    return (
        payload.get("type") == "mcp_video_selection"
        and payload.get("stage") == "approved"
        and str(payload.get("sub", "")) == user_id
        and str(payload.get("video_id", "")) == _video_id_or_400(video_url)
        and payload.get("output_type") == output_type
        and payload.get("language") == language
    )


def _selection_required_payload() -> dict[str, Any]:
    return {
        "error": "video_selection_required",
        "message": _video_selection_message(),
        "required_steps": [
            "Call begin_video_request for this URL.",
            "Wait for the user's explicit output and language choice.",
            "Call select_video_output, then use its approved_selection_token.",
        ],
    }


def build_mcp_server(
    public_url: str | None = None,
    *,
    include_trial_status: bool = False,
    pdf_style: Literal["plugin", "claude"] = "plugin",
    pdf_cache_mode: Literal["notes", "gemini_full_notes"] = "notes",
    pdf_notes_chunk_chars: int | None = None,
    external_pdf_download_only: bool = False,
    guided_video_choices: bool = False,
    requires_video_selection: bool = False,
) -> FastMCP:
    """Build an authenticated MCP app for one public transport URL."""
    resource_url = (public_url or settings.mcp_public_url).rstrip("/")
    if not resource_url.startswith("https://"):
        raise RuntimeError("Each public MCP URL must use HTTPS when MCP_ENABLED=true")
    provider = TubeNotesOAuthProvider(resource_url)
    server = FastMCP(
        "TubeNotes",
        instructions=(
            "Use TubeNotes to summarize public YouTube videos or prepare a PDF. "
            "Before every summary or PDF creation tool call, the user must explicitly choose both "
            "the output type and the output language. Never infer, default, or send auto for either choice. "
            + (
                "If the user supplies only a YouTube URL, or either choice is missing, do not call a "
                "TubeNotes video tool. Ask exactly: 'Would you like a Summary or a Full PDF, and in "
                "which language?' A video title, transcript language, interface language, or previous "
                "default is not an explicit choice. "
                if guided_video_choices
                else ""
            )
            + (
                "For this connector, direct summary or PDF calls without an approved_selection_token are "
                "rejected. On a URL-only request, call begin_video_request and ask its question. After the "
                "user explicitly selects output type and language, call select_video_output and immediately "
                "call the matching work tool with approved_selection_token. "
                if requires_video_selection
                else ""
            )
            + "Never claim a PDF is ready until create_video_pdf returns its download URL."
            + (
                " For every PDF request, call create_video_pdf and present its "
                "pdf_download_url as the PDF download. Never create, export, or "
                "substitute a Canvas, document, artifact, or other PDF from the "
                "summary: it is not the complete TubeNotes PDF."
                if external_pdf_download_only
                else ""
            )
            + (
                " When a tool response includes trial_status, tell the user its message. "
                "Present subscribe_url as a Subscribe to continue link only when "
                "trial_status.trials_remaining is zero."
                if include_trial_status
                else ""
            )
        ),
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=resource_url,
            resource_server_url=resource_url,
            service_documentation_url=f"{settings.APP_BASE_URL.rstrip('/')}/privacy",
            required_scopes=[MCP_SCOPE],
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=[MCP_SCOPE],
                default_scopes=[MCP_SCOPE],
            ),
        ),
        # Mounted at /mcp by FastAPI, so / is externally https://…/mcp.
        streamable_http_path="/",
        stateless_http=True,
    )

    @server.tool(
        title="TubeNotes account",
        description="Use this to confirm the connected TubeNotes account and remaining video trials.",
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
    )
    async def get_tubenotes_profile() -> dict[str, Any]:
        user = _current_user()
        return _profile_payload(user, _current_machine_hash())

    if requires_video_selection:
        @server.tool(
            title="Start TubeNotes video request",
            description=(
                "Use when the user supplied a YouTube URL but has not explicitly selected Summary or Full PDF "
                "and a language. Do not summarize or create a PDF. Return the question to the user and wait "
                "for their answer."
            ),
            annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True),
        )
        async def begin_video_request(video_url: str) -> dict[str, Any]:
            user = _current_user()
            video_id = _video_id_or_400(video_url)
            return {
                "selection_token": _create_video_selection_token(user_id=user.id, video_id=video_id),
                "message": _video_selection_message(),
                "output_options": ["summary", "pdf"],
                "language_options": list(_LANGUAGE_CODES),
            }

        @server.tool(
            title="Confirm TubeNotes video request",
            description=(
                "Use only after the user explicitly answered the output type (summary or pdf) and language "
                "question. It approves one matching video request; then immediately call the matching work tool."
            ),
            annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True),
        )
        async def select_video_output(
            video_url: str,
            selection_token: str,
            output_type: Literal["summary", "pdf"],
            language: _LANGUAGE_OPTIONS,
        ) -> dict[str, Any]:
            user = _current_user()
            return {
                "approved_selection_token": _approve_video_selection(
                    selection_token,
                    user_id=user.id,
                    video_url=video_url,
                    output_type=output_type,
                    language=language,
                ),
                "message": "Selection confirmed. Create the selected TubeNotes output now.",
            }

    @server.tool(
        title="Summarize YouTube video",
        description=(
            "Use this only after the user explicitly chose Summary and a language for a public YouTube video. "
            "Return the structured TubeNotes summary as generated: preserve its Markdown headings, short "
            "paragraphs, key-point bullets, and conclusion. Do not replace it with a Canvas/document."
            + (
                " A URL by itself is not permission to call this tool; first ask whether the user wants "
                "a Summary or a Full PDF, and which language."
                if guided_video_choices
                else ""
            )
        ),
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True),
    )
    async def summarize_youtube_video(
        video_url: str,
        language: _LANGUAGE_OPTIONS,
        approved_selection_token: str = "",
    ) -> dict[str, Any]:
        user = _current_user()
        if requires_video_selection and not _selection_is_approved(
            approved_selection_token,
            user_id=user.id,
            video_url=video_url,
            output_type="summary",
            language=language,
        ):
            return _selection_required_payload()
        result = await _create_text(
            user,
            _current_machine_hash(),
            video_url,
            language,
            "summary",
            include_trial_status=include_trial_status,
        )
        payload = {
            **{key: value for key, value in result.items() if key != "text"},
            "summary": result["text"],
            "open_in_tubenotes": f"{settings.APP_BASE_URL.rstrip('/')}/?{urlencode({'youtube_url': video_url})}",
        }
        if guided_video_choices:
            payload["presentation_instruction"] = (
                "Present the summary exactly as supplied in the summary field. Keep its Markdown headings "
                "and bullet points; do not rewrite, compress, or turn it into a separate document."
            )
        return payload

    @server.tool(
        title="Create video notes PDF",
        description=(
            "Use this when the user wants a downloadable PDF of exhaustive, complete "
            "YouTube video notes in a chosen language. The PDF covers the full "
            "transcript and every major topic, not a short summary."
            + (
                " Use this only after the user explicitly chose Full PDF and a language. A URL alone is "
                "not permission to call this tool."
                if guided_video_choices
                else ""
            )
            + (
                " This tool's pdf_download_url is the only PDF to present. Do not "
                "make a Canvas, document, artifact, or substitute PDF from its content."
                if external_pdf_download_only
                else ""
            )
        ),
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True),
    )
    async def create_video_pdf(
        video_url: str,
        language: _LANGUAGE_OPTIONS,
        approved_selection_token: str = "",
    ) -> dict[str, Any]:
        user = _current_user()
        if requires_video_selection and not _selection_is_approved(
            approved_selection_token,
            user_id=user.id,
            video_url=video_url,
            output_type="pdf",
            language=language,
        ):
            return _selection_required_payload()
        return await _pdf_payload(
            user,
            _current_machine_hash(),
            video_url,
            language,
            include_trial_status=include_trial_status,
            pdf_style=pdf_style,
            cache_mode=pdf_cache_mode,
            notes_chunk_chars=pdf_notes_chunk_chars,
            external_download_only=external_pdf_download_only,
        )

    return server


async def render_plugin_pdf(token: str) -> tuple[bytes, str]:
    """Render a cached notes PDF for a signed MCP download link."""
    payload = decode_pdf_download_token(token)
    video_id = str(payload.get("video_id", ""))
    language = str(payload.get("language", ""))
    if not video_id or not language:
        raise ValueError("Invalid PDF link")
    output_mode = str(payload.get("output_mode", "notes"))
    if output_mode not in {"notes", "gemini_full_notes"}:
        raise ValueError("Invalid PDF link")
    model = summarizer.plan_for(language)[0]
    db = SessionLocal()
    try:
        row = output_cache.get(db, video_id, output_mode, language, model)
    finally:
        db.close()
    if row is None:
        raise ValueError("This PDF link has expired. Ask TubeNotes to create it again.")
    meta = await youtube.fetch_metadata(video_id)
    style = str(payload.get("style", "plugin"))
    if style not in {"plugin", "claude"}:
        raise ValueError("Invalid PDF link")
    data = await pdf.render_notes_pdf(
        row.text,
        title=meta.title,
        subtitle=f"{meta.author or 'YouTube'} - {youtube.watch_url(video_id)}",
        style=style,
        hero_image=meta.thumbnail if style == "claude" else "",
    )
    safe_name = re.sub(r"[^A-Za-z0-9 _-]+", "", meta.title or "tubenotes-notes")[:60].strip() or "tubenotes-notes"
    return data, f"{safe_name}.pdf"
