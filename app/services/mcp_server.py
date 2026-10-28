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
from urllib.parse import urlencode

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
from app.models import McpTranscriptCache, McpTrialLedger, McpTrialUsage, User
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
    upgrade_url = f"{settings.APP_BASE_URL}{settings.API_PREFIX}/billing/checkout"
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


def _claim_mcp_trial(db, user: User, machine_hash: str, video_id: str, payload, mode: str):
    """Atomically unlock one video for an MCP browser machine and check cache."""
    ledger = _mcp_ledger(db, machine_hash, lock=True)
    prior = db.execute(
        select(McpTrialUsage).where(
            McpTrialUsage.machine_hash == machine_hash,
            McpTrialUsage.video_id == video_id,
        )
    ).scalar_one_or_none()
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
        entitlement = _mcp_entitlement(db, user, machine_hash)
    else:
        # A video already unlocked on this browser remains available for its
        # PDF or a retry even after the twentieth new video was consumed.
        entitlement = _mcp_entitlement(db, user, machine_hash)
        if not entitlement.allowed:
            entitlement = entitlement.model_copy(update={"allowed": True, "reason": "already_unlocked"})
    db.commit()
    cached_row, cached_target, cached_model = _cache_lookup(db, payload, video_id, mode=mode)
    return entitlement, cached_row, cached_target, cached_model


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
    mode: Literal["summary", "notes"],
) -> dict[str, Any]:
    video_id = _video_id_or_400(video_url)
    payload = (
        SummarizeRequest(url=video_url, target_lang=language, device=_mcp_request_device(user.id), mode="summary")
        if mode == "summary"
        else VideoRequest(url=video_url, target_lang=language, device=_mcp_request_device(user.id))
    )
    entitlement, cached, cached_target, cached_model = await _claim(
        user, machine_hash, payload, video_id, mode
    )
    if cached is not None:
        meta = await youtube.fetch_metadata(video_id)
        text = cached.text
        if cached.translate_to:
            text = await translate.translate(text, cached.translate_to)
        return {
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
        text = await summarizer.full_notes(transcript.text, lang=write_language)
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
            mode,
            target,
            model,
            text,
            detected,
            len(transcript.text),
            transcript.source,
        )
    return {
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


async def _pdf_payload(user: User, machine_hash: str, video_url: str, language: str) -> dict[str, Any]:
    if not settings.OUTPUT_CACHE_ENABLED:
        raise RuntimeError("PDF export is unavailable until TubeNotes output caching is enabled.")
    result = await _create_text(user, machine_hash, video_url, language, "notes")
    token = create_pdf_download_token(
        user_id=user.id,
        video_id=result["video_id"],
        language=result["language"],
    )
    return {
        "video_id": result["video_id"],
        "title": result["title"],
        "language": result["language"],
        "language_name": result["language_name"],
        "pdf_download_url": f"{settings.APP_BASE_URL.rstrip('/')}/mcp/download/{token}",
        "expires_in_minutes": settings.MCP_DOWNLOAD_TTL_MINUTES,
        "message": "Your PDF is ready. Open the secure download link before it expires.",
    }


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
            result = await _create_text(user, machine_hash, video_url, language, "summary")
            payload = {
                **{key: value for key, value in result.items() if key != "text"},
                "summary": result["text"],
                "open_in_tubenotes": f"{settings.APP_BASE_URL.rstrip('/')}/?{urlencode({'youtube_url': video_url})}",
            }
            return _tool_result(payload)
        if tool_name == "create_video_pdf":
            assert language is not None
            return _tool_result(await _pdf_payload(user, machine_hash, video_url, language))
        return _tool_result({"error": "unknown_tool", "message": "Unknown TubeNotes tool."}, is_error=True)
    except Exception as exc:
        logger.exception("MCP tool %s failed for TubeNotes user %s", tool_name, token.user_id)
        return _tool_result(
            {"error": "tool_failed", "message": _tool_failure_message(exc)},
            is_error=True,
        )


def create_pdf_download_token(*, user_id: str, video_id: str, language: str) -> str:
    expiry = _now() + timedelta(minutes=settings.MCP_DOWNLOAD_TTL_MINUTES)
    return jwt.encode(
        {
            "sub": user_id,
            "video_id": video_id,
            "language": language,
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


def build_mcp_server() -> FastMCP:
    """Build the authenticated MCP app; called only when MCP_ENABLED=true."""
    if not settings.mcp_public_url.startswith("https://"):
        raise RuntimeError("MCP_PUBLIC_URL must be a public HTTPS URL when MCP_ENABLED=true")
    provider = TubeNotesOAuthProvider()
    server = FastMCP(
        "TubeNotes",
        instructions=(
            "Use TubeNotes to summarize public YouTube videos or prepare a PDF. "
            "Before every summary or PDF creation tool call, ask the user to choose an output language. "
            "Never infer, default, or send auto for the language. "
            "Never claim a PDF is ready until create_video_pdf returns its download URL."
        ),
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=settings.mcp_public_url,
            resource_server_url=settings.mcp_public_url,
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

    @server.tool(
        title="Summarize YouTube video",
        description="Use this when the user wants a summary of a public YouTube video in a chosen language.",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True),
    )
    async def summarize_youtube_video(
        video_url: str,
        language: _LANGUAGE_OPTIONS,
    ) -> dict[str, Any]:
        user = _current_user()
        result = await _create_text(user, _current_machine_hash(), video_url, language, "summary")
        return {
            **{key: value for key, value in result.items() if key != "text"},
            "summary": result["text"],
            "open_in_tubenotes": f"{settings.APP_BASE_URL.rstrip('/')}/?{urlencode({'youtube_url': video_url})}",
        }

    @server.tool(
        title="Create video notes PDF",
        description="Use this when the user wants a downloadable PDF of complete YouTube video notes in a chosen language.",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True),
    )
    async def create_video_pdf(
        video_url: str,
        language: _LANGUAGE_OPTIONS,
    ) -> dict[str, Any]:
        user = _current_user()
        return await _pdf_payload(user, _current_machine_hash(), video_url, language)

    return server


async def render_plugin_pdf(token: str) -> tuple[bytes, str]:
    """Render a cached notes PDF for a short-lived link returned to ChatGPT."""
    payload = decode_pdf_download_token(token)
    video_id = str(payload.get("video_id", ""))
    language = str(payload.get("language", ""))
    if not video_id or not language:
        raise ValueError("Invalid PDF link")
    model = summarizer.plan_for(language)[0]
    db = SessionLocal()
    try:
        row = output_cache.get(db, video_id, "notes", language, model)
    finally:
        db.close()
    if row is None:
        raise ValueError("This PDF link has expired. Ask TubeNotes to create it again.")
    meta = await youtube.fetch_metadata(video_id)
    data = await pdf.render_notes_pdf(
        row.text,
        title=meta.title,
        subtitle=f"{meta.author or 'YouTube'} - {youtube.watch_url(video_id)}",
        style="plugin",
    )
    safe_name = re.sub(r"[^A-Za-z0-9 _-]+", "", meta.title or "tubenotes-notes")[:60].strip() or "tubenotes-notes"
    return data, f"{safe_name}.pdf"
