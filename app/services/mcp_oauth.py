"""OAuth 2.1 state for ChatGPT's TubeNotes MCP connection.

The browser signs in to the existing TubeNotes account system.  ChatGPT never
receives a site password or a normal web-app JWT: it receives a short-lived,
opaque MCP bearer token after the PKCE exchange.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from mcp.server.auth.provider import AccessToken, AuthorizationCode, RefreshToken, TokenError
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.models import McpAccessToken, McpAuthorizationCode, McpAuthorizationRequest, McpOAuthClient
from app.security import sha256

MCP_SCOPE = "tubenotes:summary"


def now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime) -> datetime:
    """Normalise SQLite's naive datetimes and Postgres' aware datetimes."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _scopes(value: str) -> list[str]:
    return [scope for scope in value.split(" ") if scope]


class TubeNotesAuthorizationCode(AuthorizationCode):
    user_id: str


class TubeNotesAccessToken(AccessToken):
    user_id: str


class TubeNotesOAuthProvider:
    """Small persistent OAuth provider used by FastMCP's standard handlers."""

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        if not client_id:
            return None
        db = SessionLocal()
        try:
            row = db.get(McpOAuthClient, client_id)
            return OAuthClientInformationFull.model_validate_json(row.metadata_json) if row else None
        finally:
            db.close()

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        client_id = str(client_info.client_id or "")
        if not client_id:
            raise ValueError("OAuth client ID is required")
        db = SessionLocal()
        try:
            row = db.get(McpOAuthClient, client_id)
            payload = client_info.model_dump_json()
            if row is None:
                db.add(McpOAuthClient(client_id=client_id, metadata_json=payload))
            else:
                # DCR metadata may be refreshed by ChatGPT. The client ID is
                # stable and update is safer than accumulating stale callbacks.
                row.metadata_json = payload
            db.commit()
        finally:
            db.close()

    async def authorize(self, client, params) -> str:
        request_id = secrets.token_urlsafe(32)
        db = SessionLocal()
        try:
            db.add(
                McpAuthorizationRequest(
                    id=request_id,
                    client_id=str(client.client_id),
                    redirect_uri=str(params.redirect_uri),
                    code_challenge=params.code_challenge,
                    scopes=" ".join(params.scopes or [MCP_SCOPE]),
                    state=params.state,
                    resource=params.resource,
                    expires_at=now() + timedelta(minutes=settings.MCP_AUTHORIZATION_TTL_MINUTES),
                )
            )
            db.commit()
        finally:
            db.close()
        query = urlencode({"request": request_id})
        return f"{settings.APP_BASE_URL.rstrip('/')}/plugin/connect?{query}"

    async def load_authorization_code(self, client, authorization_code: str) -> TubeNotesAuthorizationCode | None:
        db = SessionLocal()
        try:
            row = db.execute(
                select(McpAuthorizationCode).where(
                    McpAuthorizationCode.code_hash == sha256(authorization_code),
                    McpAuthorizationCode.client_id == str(client.client_id),
                    McpAuthorizationCode.consumed_at.is_(None),
                )
            ).scalar_one_or_none()
            if row is None or _utc(row.expires_at) <= now():
                return None
            return TubeNotesAuthorizationCode(
                code=authorization_code,
                scopes=_scopes(row.scopes),
                expires_at=row.expires_at.timestamp(),
                client_id=row.client_id,
                code_challenge=row.code_challenge,
                redirect_uri=row.redirect_uri,
                redirect_uri_provided_explicitly=True,
                resource=row.resource,
                user_id=row.user_id,
            )
        finally:
            db.close()

    async def exchange_authorization_code(self, client, authorization_code: TubeNotesAuthorizationCode) -> OAuthToken:
        db = SessionLocal()
        try:
            row = db.execute(
                select(McpAuthorizationCode).where(
                    McpAuthorizationCode.code_hash == sha256(authorization_code.code),
                    McpAuthorizationCode.client_id == str(client.client_id),
                )
            ).scalar_one_or_none()
            if row is None or row.consumed_at is not None or _utc(row.expires_at) <= now():
                raise TokenError("invalid_grant", "Authorization code is expired or already used")
            row.consumed_at = now()
            raw = secrets.token_urlsafe(48)
            expires_at = now() + timedelta(minutes=settings.MCP_ACCESS_TOKEN_TTL_MINUTES)
            db.add(
                McpAccessToken(
                    token_hash=sha256(raw),
                    user_id=row.user_id,
                    client_id=row.client_id,
                    scopes=row.scopes,
                    resource=row.resource,
                    expires_at=expires_at,
                )
            )
            db.commit()
            return OAuthToken(
                access_token=raw,
                expires_in=int((expires_at - now()).total_seconds()),
                scope=row.scopes,
            )
        finally:
            db.close()

    async def load_refresh_token(self, client, refresh_token: str) -> RefreshToken | None:
        # Deliberately no persistent refresh token. A short-lived MCP token
        # limits exposure and ChatGPT reconnects through the normal PKCE flow.
        return None

    async def exchange_refresh_token(self, client, refresh_token, scopes: list[str]) -> OAuthToken:
        raise TokenError("invalid_grant", "Reconnect TubeNotes to continue")

    async def load_access_token(self, token: str) -> TubeNotesAccessToken | None:
        db = SessionLocal()
        try:
            row = db.execute(
                select(McpAccessToken).where(
                    McpAccessToken.token_hash == sha256(token),
                    McpAccessToken.revoked_at.is_(None),
                )
            ).scalar_one_or_none()
            if row is None or _utc(row.expires_at) <= now():
                return None
            return TubeNotesAccessToken(
                token=token,
                client_id=row.client_id,
                scopes=_scopes(row.scopes),
                expires_at=int(row.expires_at.timestamp()),
                resource=row.resource,
                user_id=row.user_id,
            )
        finally:
            db.close()

    async def revoke_token(self, token) -> None:
        raw = getattr(token, "token", "")
        if not raw:
            return
        db = SessionLocal()
        try:
            row = db.execute(select(McpAccessToken).where(McpAccessToken.token_hash == sha256(raw))).scalar_one_or_none()
            if row is not None:
                row.revoked_at = now()
                db.commit()
        finally:
            db.close()


def authorization_request_details(request_id: str) -> dict[str, str] | None:
    """Public, non-sensitive label shown on the TubeNotes OAuth login page."""
    db = SessionLocal()
    try:
        row = db.get(McpAuthorizationRequest, request_id)
        if row is None or _utc(row.expires_at) <= now():
            return None
        client = db.get(McpOAuthClient, row.client_id)
        name = "ChatGPT"
        if client:
            try:
                name = OAuthClientInformationFull.model_validate_json(client.metadata_json).client_name or name
            except Exception:
                pass
        return {"client_name": name, "scope": row.scopes}
    finally:
        db.close()


def approve_authorization_request(request_id: str, user_id: str) -> str | None:
    """Create one code after an existing TubeNotes user authenticated."""
    db = SessionLocal()
    try:
        request = db.get(McpAuthorizationRequest, request_id)
        if request is None or _utc(request.expires_at) <= now():
            return None
        redirect_uri = request.redirect_uri
        state = request.state
        client_id = request.client_id
        code_challenge = request.code_challenge
        scopes = request.scopes
        resource = request.resource
        raw_code = secrets.token_urlsafe(32)
        db.add(
            McpAuthorizationCode(
                code_hash=sha256(raw_code),
                user_id=user_id,
                client_id=client_id,
                redirect_uri=redirect_uri,
                code_challenge=code_challenge,
                scopes=scopes,
                resource=resource,
                expires_at=now() + timedelta(minutes=settings.MCP_AUTHORIZATION_TTL_MINUTES),
            )
        )
        db.delete(request)
        db.commit()
        params = {"code": raw_code}
        if state:
            params["state"] = state
        separator = "&" if "?" in redirect_uri else "?"
        return f"{redirect_uri}{separator}{urlencode(params)}"
    finally:
        db.close()
