from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlparse

import pytest

from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull

from app.config import settings
from app.services.mcp_oauth import (
    MCP_SCOPE,
    TubeNotesOAuthProvider,
    approve_authorization_request,
    authorization_request_details,
)
from app.models import McpOAuthClient
from app.schemas import DeviceFingerprint
from tests.conftest import register


def test_mcp_oauth_code_is_one_time_and_scoped_to_the_user(client, db, device):
    body, _headers, _device = register(client, email="mcp@example.com", device=device)
    user_id = body["user"]["id"]
    provider = TubeNotesOAuthProvider()
    client_info = OAuthClientInformationFull(
        client_id="chatgpt-test-client",
        redirect_uris=["https://chatgpt.com/plugin-callback"],
        token_endpoint_auth_method="none",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope=MCP_SCOPE,
        client_name="ChatGPT",
    )

    async def flow():
        await provider.register_client(client_info)
        redirect = await provider.authorize(
            client_info,
            AuthorizationParams(
                state="state-123",
                scopes=[MCP_SCOPE],
                code_challenge="pkce-challenge",
                redirect_uri="https://chatgpt.com/plugin-callback",
                redirect_uri_provided_explicitly=True,
                resource=None,
            ),
        )
        request_id = parse_qs(urlparse(redirect).query)["request"][0]
        assert authorization_request_details(request_id) == {
            "client_name": "ChatGPT",
            "scope": MCP_SCOPE,
        }
        callback = approve_authorization_request(
            request_id, user_id, DeviceFingerprint.model_validate(device)
        )
        code = parse_qs(urlparse(callback).query)["code"][0]
        assert parse_qs(urlparse(callback).query)["state"] == ["state-123"]
        stored = await provider.load_authorization_code(client_info, code)
        assert stored is not None
        assert stored.user_id == user_id
        assert stored.resource == settings.mcp_public_url
        token = await provider.exchange_authorization_code(client_info, stored)
        assert token.access_token
        assert await provider.load_authorization_code(client_info, code) is None
        access = await provider.load_access_token(token.access_token)
        assert access is not None
        assert access.user_id == user_id
        assert access.scopes == [MCP_SCOPE]
        assert access.machine_hash

    asyncio.run(flow())


def test_mcp_oauth_approval_requires_signed_in_user(client, device):
    body, _headers, _device = register(client, email="mcp-api@example.com", device=device)
    response = client.post(
        "/api/v1/mcp/oauth/approve",
        json={"request_id": "x" * 32, "device": device},
    )
    assert response.status_code == 401
    assert body["user"]["email"] == "mcp-api@example.com"


@pytest.mark.parametrize("path", ["/mcp/register", "/register", "/gemini-mcp/register"])
def test_mcp_dynamic_registration_accepts_claude_pkce_client(client, db, path):
    response = client.post(
        path,
        json={
            "redirect_uris": ["https://claude.ai/mcp/callback"],
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
            "scope": MCP_SCOPE,
            "client_name": "Claude",
        },
    )

    assert response.status_code == 201, response.text
    registered = response.json()
    assert registered["client_id"]
    assert registered["token_endpoint_auth_method"] == "none"
    assert registered["grant_types"] == ["authorization_code"]
    assert db.get(McpOAuthClient, registered["client_id"]) is not None


def test_gemini_oauth_metadata_advertises_pkce_public_client(client):
    response = client.get("/gemini-mcp/.well-known/oauth-authorization-server")

    assert response.status_code == 200
    metadata = response.json()
    assert metadata["issuer"] == settings.gemini_mcp_public_url
    assert metadata["registration_endpoint"] == f"{settings.gemini_mcp_public_url}/register"
    assert metadata["token_endpoint_auth_methods_supported"] == ["none"]
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    assert response.headers["access-control-allow-origin"] == "*"


def test_gemini_dynamic_registration_allows_browser_preflight(client):
    response = client.options("/gemini-mcp/register")

    assert response.status_code == 204
    assert response.headers["access-control-allow-origin"] == "*"
    assert response.headers["access-control-allow-methods"] == "POST, OPTIONS"
