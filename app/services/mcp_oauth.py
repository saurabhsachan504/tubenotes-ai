"""OAuth 2.1 state for TubeNotes' ChatGPT and Claude MCP connections.

The browser signs in to the existing TubeNotes account system.  The MCP hosts
never receive a site password or a normal web-app JWT: they receive an opaque
MCP bearer token after the PKCE exchange.
"""
from __future__ import annotations

import secrets
from uuid import uuid4
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlparse

from mcp.server.auth.provider import AccessToken, AuthorizationCode, RefreshToken, TokenError
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import ValidationError
from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.models import (
    McpAccessToken,
    McpAuthorizationCode,
    McpAuthorizationRequest,
    McpOAuthClient,
    McpRefreshToken,
)
from app.schemas import DeviceFingerprint
from app.security import sha256
from app.services import devices as device_service

MCP_SCOPE = "tubenotes:summary"


def now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime) -> datetime:
    """Normalise SQLite's naive datetimes and Postgres' aware datetimes."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _scopes(value: str) -> list[str]:
    return [scope for scope in value.split(" ") if scope]


def _is_persistent_connector_client(client: OAuthClientInformationFull) -> bool:
    """Identify first-party callbacks that support silent OAuth renewal.

    This is intentionally based on the registered callback host, never the
    client display name supplied during dynamic registration.
    """
    hosts = {
        (urlparse(str(uri)).hostname or "").lower()
        for uri in client.redirect_uris
    }
    return any(
        host == "chatgpt.com"
        or host.endswith(".chatgpt.com")
        or host == "chat.openai.com"
        or host.endswith(".chat.openai.com")
        or host == "oauth.openai.com"
        or host == "oauth-redirect.openai.com"
        or host == "claude.ai"
        or host.endswith(".claude.ai")
        for host in hosts
    )


def _with_persistent_refresh_grant(client: OAuthClientInformationFull) -> OAuthClientInformationFull:
    """Allow ChatGPT and Claude to renew a connected account silently."""
    if not _is_persistent_connector_client(client) or "refresh_token" in client.grant_types:
        return client
    return client.model_copy(update={"grant_types": [*client.grant_types, "refresh_token"]})


class TubeNotesAuthorizationCode(AuthorizationCode):
    user_id: str
    machine_hash: str | None = None


class TubeNotesAccessToken(AccessToken):
    user_id: str
    machine_hash: str | None = None


class TubeNotesRefreshToken(RefreshToken):
    """Refresh-token metadata kept server-side and never sent to MCP tools."""

    refresh_token_id: str
    user_id: str
    machine_hash: str | None = None
    resource: str | None = None


class TubeNotesOAuthProvider:
    """Small persistent OAuth provider used by FastMCP's standard handlers."""

    def __init__(self, resource_url: str | None = None):
        # The same authorization server backs the ChatGPT/Claude and Gemini
        # MCP transports. Each transport supplies its own protected resource
        # URL so an authorization code cannot be minted for the wrong one.
        self.resource_url = (resource_url or settings.mcp_public_url).rstrip("/")

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        if not client_id:
            return None
        db = SessionLocal()
        try:
            row = db.get(McpOAuthClient, client_id)
            if row is None:
                return None
            client = _with_persistent_refresh_grant(
                OAuthClientInformationFull.model_validate_json(row.metadata_json)
            )
            # Older ChatGPT and Claude records were created before refresh
            # support. Upgrade only their trusted callback records on the next
            # OAuth request; Gemini metadata remains exactly as registered.
            payload = client.model_dump_json()
            if payload != row.metadata_json:
                row.metadata_json = payload
                db.commit()
            return client
        finally:
            db.close()

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        client_info = _with_persistent_refresh_grant(client_info)
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
                # DCR metadata may be refreshed by a connector. The client ID
                # is stable and update is safer than accumulating stale callbacks.
                row.metadata_json = payload
            db.commit()
        finally:
            db.close()

    async def authorize(self, client, params) -> str:
        # RFC 8707's resource indicator is optional. Bind a token to the
        # protected MCP resource that received this OAuth request rather than
        # persisting a null audience.
        resource = self.resource_url
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
                    resource=resource,
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
                machine_hash=row.machine_hash,
            )
        finally:
            db.close()

    def _issue_token_pair(
        self,
        db,
        *,
        user_id: str,
        client_id: str,
        scopes: str,
        resource: str | None,
        machine_hash: str | None,
        issue_refresh_token: bool,
    ) -> OAuthToken:
        """Create a short-lived access token and, for capable clients, a durable refresh token.

        The plaintext values exist only in the OAuth response. The database
        stores their hashes, so a database export cannot be used to operate a
        user's connector session.
        """
        raw_access = secrets.token_urlsafe(48)
        expires_at = now() + timedelta(minutes=settings.MCP_ACCESS_TOKEN_TTL_MINUTES)
        refresh_token_id: str | None = None
        raw_refresh: str | None = None
        if issue_refresh_token:
            raw_refresh = secrets.token_urlsafe(64)
            refresh = McpRefreshToken(
                token_hash=sha256(raw_refresh),
                user_id=user_id,
                client_id=client_id,
                scopes=scopes,
                resource=resource,
                machine_hash=machine_hash,
                # A plugin connection is intentionally persistent. ChatGPT or
                # Claude can end it through OAuth revocation when the plugin
                # is removed, which revokes this session and its access tokens.
                expires_at=None,
            )
            db.add(refresh)
            db.flush()
            refresh_token_id = refresh.id
        db.add(
            McpAccessToken(
                token_hash=sha256(raw_access),
                user_id=user_id,
                client_id=client_id,
                scopes=scopes,
                resource=resource,
                machine_hash=machine_hash,
                refresh_token_id=refresh_token_id,
                expires_at=expires_at,
            )
        )
        return OAuthToken(
            access_token=raw_access,
            expires_in=int((expires_at - now()).total_seconds()),
            scope=scopes,
            refresh_token=raw_refresh,
        )

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
            token = self._issue_token_pair(
                db,
                user_id=row.user_id,
                client_id=row.client_id,
                scopes=row.scopes,
                resource=row.resource,
                machine_hash=row.machine_hash,
                # Only trusted ChatGPT and Claude callbacks are upgraded to
                # the refresh grant. Gemini's code-only flow remains unchanged.
                issue_refresh_token="refresh_token" in (client.grant_types or []),
            )
            db.commit()
            return token
        finally:
            db.close()

    async def load_refresh_token(self, client, refresh_token: str) -> TubeNotesRefreshToken | None:
        db = SessionLocal()
        try:
            row = db.execute(
                select(McpRefreshToken).where(
                    McpRefreshToken.token_hash == sha256(refresh_token),
                    McpRefreshToken.client_id == str(client.client_id),
                    McpRefreshToken.revoked_at.is_(None),
                )
            ).scalar_one_or_none()
            if row is None or (row.expires_at is not None and _utc(row.expires_at) <= now()):
                return None
            return TubeNotesRefreshToken(
                token=refresh_token,
                client_id=row.client_id,
                scopes=_scopes(row.scopes),
                expires_at=int(row.expires_at.timestamp()) if row.expires_at is not None else None,
                refresh_token_id=row.id,
                user_id=row.user_id,
                machine_hash=row.machine_hash,
                resource=row.resource,
            )
        finally:
            db.close()

    async def exchange_refresh_token(
        self, client, refresh_token: TubeNotesRefreshToken, scopes: list[str]
    ) -> OAuthToken:
        db = SessionLocal()
        try:
            row = db.execute(
                select(McpRefreshToken).where(
                    McpRefreshToken.id == refresh_token.refresh_token_id,
                    McpRefreshToken.token_hash == sha256(refresh_token.token),
                    McpRefreshToken.client_id == str(client.client_id),
                    McpRefreshToken.revoked_at.is_(None),
                )
            ).scalar_one_or_none()
            if row is None or (row.expires_at is not None and _utc(row.expires_at) <= now()):
                raise TokenError("invalid_grant", "TubeNotes connection is no longer active")

            # Rotate the refresh secret on every exchange. Reuse of an older
            # secret cannot keep the connection alive, and access tokens from
            # its family are revoked at the same time.
            row.revoked_at = now()
            active_access_tokens = db.execute(
                select(McpAccessToken).where(
                    McpAccessToken.refresh_token_id == row.id,
                    McpAccessToken.revoked_at.is_(None),
                )
            ).scalars()
            for access_token in active_access_tokens:
                access_token.revoked_at = now()
            token = self._issue_token_pair(
                db,
                user_id=row.user_id,
                client_id=row.client_id,
                scopes=" ".join(scopes),
                resource=row.resource,
                machine_hash=row.machine_hash,
                issue_refresh_token=True,
            )
            db.commit()
            return token
        finally:
            db.close()

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
                machine_hash=row.machine_hash,
            )
        finally:
            db.close()

    async def revoke_token(self, token) -> None:
        raw = getattr(token, "token", "")
        if not raw:
            return
        db = SessionLocal()
        try:
            refresh = db.execute(
                select(McpRefreshToken).where(McpRefreshToken.token_hash == sha256(raw))
            ).scalar_one_or_none()
            if refresh is not None:
                refresh.revoked_at = now()
                rows = db.execute(
                    select(McpAccessToken).where(
                        McpAccessToken.refresh_token_id == refresh.id,
                        McpAccessToken.revoked_at.is_(None),
                    )
                ).scalars()
                for row in rows:
                    row.revoked_at = now()
                db.commit()
                return
            row = db.execute(
                select(McpAccessToken).where(McpAccessToken.token_hash == sha256(raw))
            ).scalar_one_or_none()
            if row is not None:
                row.revoked_at = now()
                if row.refresh_token_id:
                    refresh = db.get(McpRefreshToken, row.refresh_token_id)
                    if refresh is not None:
                        refresh.revoked_at = now()
                        rows = db.execute(
                            select(McpAccessToken).where(
                                McpAccessToken.refresh_token_id == refresh.id,
                                McpAccessToken.revoked_at.is_(None),
                            )
                        ).scalars()
                        for linked in rows:
                            linked.revoked_at = now()
                db.commit()
        finally:
            db.close()


async def register_dynamic_client(metadata: object) -> OAuthClientInformationFull:
    """Register a public OAuth client without FastMCP 1.12's DCR restrictions.

    That FastMCP release rejects a standards-valid client that asks only for an
    authorization-code grant; it requires ``refresh_token`` too. Claude uses
    the former when creating a custom connector. We deliberately support both
    shapes, while retaining PKCE and the normal token-endpoint checks.
    """
    if not isinstance(metadata, dict):
        raise ValueError("Client metadata must be a JSON object")

    raw_redirect_uris = metadata.get("redirect_uris")
    if not isinstance(raw_redirect_uris, list) or not raw_redirect_uris:
        raise ValueError("redirect_uris must contain at least one callback URL")
    if len(raw_redirect_uris) > 16 or any(not isinstance(uri, str) or len(uri) > 2048 for uri in raw_redirect_uris):
        raise ValueError("redirect_uris contains an invalid callback URL")

    method = metadata.get("token_endpoint_auth_method", "none")
    if method not in {"none", "client_secret_post"}:
        raise ValueError("Unsupported token_endpoint_auth_method")

    grant_types = metadata.get("grant_types", ["authorization_code"])
    if not isinstance(grant_types, list) or not grant_types or not all(isinstance(item, str) for item in grant_types):
        raise ValueError("grant_types must be an array")
    if "authorization_code" not in grant_types or not set(grant_types).issubset({"authorization_code", "refresh_token"}):
        raise ValueError("Only authorization_code and refresh_token grants are supported")

    response_types = metadata.get("response_types", ["code"])
    if response_types != ["code"]:
        raise ValueError("Only the code response type is supported")

    scope = metadata.get("scope", MCP_SCOPE)
    if not isinstance(scope, str) or not scope.strip() or not set(_scopes(scope)).issubset({MCP_SCOPE}):
        raise ValueError("Requested scopes are not valid")

    # OAuthClientInformationFull gives us URL/type validation and strips any
    # unknown, non-security-relevant metadata fields sent by a newer client.
    client_secret = secrets.token_urlsafe(32) if method == "client_secret_post" else None
    try:
        client = OAuthClientInformationFull.model_validate(
            {
                "client_id": str(uuid4()),
                "client_id_issued_at": int(now().timestamp()),
                "client_secret": client_secret,
                "redirect_uris": raw_redirect_uris,
                "token_endpoint_auth_method": method,
                "grant_types": grant_types,
                "response_types": response_types,
                "scope": " ".join(_scopes(scope)),
                **{
                    key: metadata[key]
                    for key in (
                        "client_name", "client_uri", "logo_uri", "contacts", "tos_uri", "policy_uri",
                        "jwks_uri", "jwks", "software_id", "software_version",
                    )
                    if key in metadata
                },
            }
        )
    except ValidationError as exc:
        raise ValueError("Invalid client metadata") from exc

    client = _with_persistent_refresh_grant(client)
    await TubeNotesOAuthProvider().register_client(client)
    return client


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


def approve_authorization_request(
    request_id: str, user_id: str, device: DeviceFingerprint
) -> str | None:
    """Create one code after an existing TubeNotes user authenticated."""
    machine_hash = device_service.fingerprint_to_machine_hash(device)
    if machine_hash is None:
        raise ValueError(
            "This browser did not provide enough device details. Please reconnect using a full browser."
        )
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
                machine_hash=machine_hash,
                expires_at=now() + timedelta(minutes=settings.MCP_AUTHORIZATION_TTL_MINUTES),
            )
        )
        db.delete(request)
        db.commit()
        params = {"code": raw_code}
        if state:
            params["state"] = state
        if resource == settings.gemini_mcp_public_url:
            # Gemini validates OAuth authorization responses using RFC 9207
            # before it sends the one-time code to the token endpoint. Keep
            # this transport-specific so established ChatGPT/Claude callback
            # behavior remains unchanged.
            params["iss"] = settings.gemini_mcp_public_url
        separator = "&" if "?" in redirect_uri else "?"
        return f"{redirect_uri}{separator}{urlencode(params)}"
    finally:
        db.close()
