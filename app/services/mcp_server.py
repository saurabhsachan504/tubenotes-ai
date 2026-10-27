"""TubeNotes tools exposed to ChatGPT and Codex through MCP.

This deliberately calls the same entitlement, transcript, model, translation
and PDF services as the website.  The only surface-specific detail is a stable
per-account virtual device used by the existing trial ledger.
"""
import re
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from urllib.parse import urlencode

import jwt
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.database import SessionLocal
from app.models import User
from app.routers.summarize import (
    SummarizeRequest,
    VideoRequest,
    _charge_and_lookup,
    _may_cache,
    _prepare_video,
    _resolve_target,
    _store_cached,
    _video_id_or_400,
)
from app.schemas import DeviceFingerprint
from app.services import entitlements, output_cache, pdf, summarizer, translate, youtube
from app.services.mcp_oauth import MCP_SCOPE, TubeNotesAccessToken, TubeNotesOAuthProvider

_LANGUAGE_OPTIONS = Literal[
    "auto", "en", "hi", "bn", "gu", "kn", "ml", "mr", "pa", "ta", "te",
    "ur", "ar", "de", "es", "fr", "id", "it", "ja", "ko", "nl", "pt", "ru", "tr", "vi", "zh",
]

_MCP_TOOL_SECURITY = [{"type": "oauth2", "scopes": [MCP_SCOPE]}]


def _tool_input_schema(*, video: bool = False) -> dict[str, Any]:
    if not video:
        return {"type": "object", "properties": {}, "additionalProperties": False}
    return {
        "type": "object",
        "properties": {
            "video_url": {"type": "string", "description": "Public YouTube video URL."},
            "language": {
                "type": "string",
                "default": "auto",
                "description": "Output language code, or auto to use the transcript language.",
            },
        },
        "required": ["video_url"],
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
            "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
            "securitySchemes": _MCP_TOOL_SECURITY,
            "_meta": {"openai/profile": True},
        },
        {
            "name": "summarize_youtube_video",
            "title": "Summarize YouTube video",
            "description": "Use this when the user wants a summary of a public YouTube video in a chosen language.",
            "inputSchema": _tool_input_schema(video=True),
            "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True},
            "securitySchemes": _MCP_TOOL_SECURITY,
        },
        {
            "name": "create_video_pdf",
            "title": "Create video notes PDF",
            "description": "Use this when the user wants a downloadable PDF of complete YouTube video notes in a chosen language.",
            "inputSchema": _tool_input_schema(video=True),
            "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True},
            "securitySchemes": _MCP_TOOL_SECURITY,
        },
    ]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _plugin_device(user_id: str) -> DeviceFingerprint:
    """A deterministic virtual device without pretending ChatGPT exposes hardware."""
    return DeviceFingerprint(
        installation_id=f"chatgpt-mcp:{user_id}",
        platform="ChatGPT plugin",
        user_agent_brand="ChatGPT",
        language="en",
        label="ChatGPT plugin",
    )


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


def _profile_payload(user: User) -> dict[str, Any]:
    db = SessionLocal()
    try:
        entitlement, _device = entitlements.check(db, user, _plugin_device(user.id))
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


async def _claim(user: User, payload: VideoRequest, video_id: str, mode: str):
    db = SessionLocal()
    try:
        return await run_in_threadpool(
            _charge_and_lookup,
            db,
            user,
            payload,
            video_id,
            action=f"mcp:{mode}",
            mode=mode,
            surface="mcp",
        )
    finally:
        db.close()


async def _create_text(user: User, video_url: str, language: str, mode: Literal["summary", "notes"]) -> dict[str, Any]:
    video_id = _video_id_or_400(video_url)
    payload = (
        SummarizeRequest(url=video_url, target_lang=language, device=_plugin_device(user.id), mode="summary")
        if mode == "summary"
        else VideoRequest(url=video_url, target_lang=language, device=_plugin_device(user.id))
    )
    entitlement, cached, cached_target, cached_model = await _claim(user, payload, video_id, mode)
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

    meta, transcript, _metadata_ms, _transcript_ms = await _prepare_video(payload, video_id)
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


async def _pdf_payload(user: User, video_url: str, language: str) -> dict[str, Any]:
    if not settings.OUTPUT_CACHE_ENABLED:
        raise RuntimeError("PDF export is unavailable until TubeNotes output caching is enabled.")
    result = await _create_text(user, video_url, language, "notes")
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
    result: dict[str, Any] = {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        "structuredContent": payload,
        "isError": is_error,
    }
    if meta:
        result["_meta"] = meta
    return result


def _authentication_required_result() -> dict[str, Any]:
    return _tool_result(
        {"error": "authentication_required", "message": "Connect your TubeNotes account to continue."},
        is_error=True,
        meta={"mcp/www_authenticate": [_oauth_challenge()]},
    )


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
    if (
        token is None
        or MCP_SCOPE not in token.scopes
        or str(token.resource or "").rstrip("/") != settings.mcp_public_url
    ):
        return _authentication_required_result()

    try:
        user = _active_user(token.user_id)
        values = arguments or {}
        if tool_name == "get_tubenotes_profile":
            return _tool_result(_profile_payload(user))
        video_url = str(values.get("video_url", "")).strip()
        language = str(values.get("language", "auto")).strip() or "auto"
        if tool_name == "summarize_youtube_video":
            result = await _create_text(user, video_url, language, "summary")
            payload = {
                **{key: value for key, value in result.items() if key != "text"},
                "summary": result["text"],
                "open_in_tubenotes": f"{settings.APP_BASE_URL.rstrip('/')}/?{urlencode({'youtube_url': video_url})}",
            }
            return _tool_result(payload)
        if tool_name == "create_video_pdf":
            return _tool_result(await _pdf_payload(user, video_url, language))
        return _tool_result({"error": "unknown_tool", "message": "Unknown TubeNotes tool."}, is_error=True)
    except Exception as exc:
        return _tool_result({"error": "tool_failed", "message": str(exc)}, is_error=True)


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
            "Offer the user an output-language choice before calling a creation tool. "
            "When they do not choose one, use auto so the result stays in the video's transcript language. "
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
        return _profile_payload(user)

    @server.tool(
        title="Summarize YouTube video",
        description="Use this when the user wants a summary of a public YouTube video in a chosen language.",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True),
    )
    async def summarize_youtube_video(
        video_url: str,
        language: _LANGUAGE_OPTIONS = "auto",
    ) -> dict[str, Any]:
        user = _current_user()
        result = await _create_text(user, video_url, language, "summary")
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
        language: _LANGUAGE_OPTIONS = "auto",
    ) -> dict[str, Any]:
        user = _current_user()
        return await _pdf_payload(user, video_url, language)

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
    )
    safe_name = re.sub(r"[^A-Za-z0-9 _-]+", "", meta.title or "tubenotes-notes")[:60].strip() or "tubenotes-notes"
    return data, f"{safe_name}.pdf"
