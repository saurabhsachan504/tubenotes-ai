from __future__ import annotations

import asyncio
import json
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
from app.services.mcp_server import _pdf_style_for_client
from app.models import McpOAuthClient
from app.schemas import DeviceFingerprint
from tests.conftest import register


def test_public_mcp_pdf_style_follows_the_connector_callback_host(db):
    db.add_all(
        [
            McpOAuthClient(
                client_id="claude-pdf-client",
                metadata_json=json.dumps({"redirect_uris": ["https://claude.ai/oauth/callback"]}),
            ),
            McpOAuthClient(
                client_id="chatgpt-pdf-client",
                metadata_json=json.dumps({"redirect_uris": ["https://chatgpt.com/plugin-callback"]}),
            ),
        ]
    )
    db.commit()

    assert _pdf_style_for_client("claude-pdf-client") == "claude"
    assert _pdf_style_for_client("chatgpt-pdf-client") == "plugin"


@pytest.mark.parametrize(
    ("resource_url", "expected_issuer"),
    [
        (settings.mcp_public_url, None),
        (settings.gemini_mcp_public_url, settings.gemini_mcp_public_url),
    ],
)
def test_mcp_oauth_code_is_one_time_and_scoped_to_the_user(
    client, db, device, resource_url, expected_issuer
):
    body, _headers, _device = register(client, email="mcp@example.com", device=device)
    user_id = body["user"]["id"]
    provider = TubeNotesOAuthProvider(resource_url)
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
        callback_params = parse_qs(urlparse(callback).query)
        assert callback_params["state"] == ["state-123"]
        if expected_issuer:
            assert callback_params["iss"] == [expected_issuer]
        else:
            assert "iss" not in callback_params
        stored = await provider.load_authorization_code(client_info, code)
        assert stored is not None
        assert stored.user_id == user_id
        assert stored.resource == resource_url
        token = await provider.exchange_authorization_code(client_info, stored)
        assert token.access_token
        assert token.refresh_token
        assert await provider.load_authorization_code(client_info, code) is None
        access = await provider.load_access_token(token.access_token)
        assert access is not None
        assert access.user_id == user_id
        assert access.scopes == [MCP_SCOPE]
        assert access.machine_hash

        refresh = await provider.load_refresh_token(client_info, token.refresh_token)
        assert refresh is not None
        rotated = await provider.exchange_refresh_token(client_info, refresh, [MCP_SCOPE])
        assert rotated.access_token and rotated.refresh_token
        assert rotated.refresh_token != token.refresh_token
        assert await provider.load_access_token(token.access_token) is None
        assert await provider.load_refresh_token(client_info, token.refresh_token) is None
        assert await provider.load_access_token(rotated.access_token) is not None

        refreshed = await provider.load_refresh_token(client_info, rotated.refresh_token)
        assert refreshed is not None
        await provider.revoke_token(refreshed)
        assert await provider.load_access_token(rotated.access_token) is None
        assert await provider.load_refresh_token(client_info, rotated.refresh_token) is None

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
    assert registered["grant_types"] == ["authorization_code", "refresh_token"]
    stored = db.get(McpOAuthClient, registered["client_id"])
    assert stored is not None and "refresh_token" in stored.metadata_json


def test_mcp_dynamic_registration_adds_refresh_grant_for_chatgpt(client, db):
    response = client.post(
        "/mcp/register",
        json={
            "redirect_uris": ["https://chatgpt.com/connector_platform_oauth_redirect"],
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
            "scope": MCP_SCOPE,
            "client_name": "TubeNotes for ChatGPT",
        },
    )

    assert response.status_code == 201, response.text
    registered = response.json()
    assert registered["grant_types"] == ["authorization_code", "refresh_token"]
    stored = db.get(McpOAuthClient, registered["client_id"])
    assert stored is not None and "refresh_token" in stored.metadata_json


def test_gemini_dynamic_registration_keeps_code_only_grant(client, db):
    response = client.post(
        "/gemini-mcp/register",
        json={
            "redirect_uris": [
                "https://oauth-redirect.googleusercontent.com/r/tubenotes-test"
            ],
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
            "scope": MCP_SCOPE,
            "client_name": "TubeNotes for Gemini",
        },
    )

    assert response.status_code == 201, response.text
    registered = response.json()
    assert registered["grant_types"] == ["authorization_code"]
    stored = db.get(McpOAuthClient, registered["client_id"])
    assert stored is not None and "refresh_token" not in stored.metadata_json


@pytest.mark.parametrize(
    "path",
    [
        "/gemini-mcp/.well-known/oauth-authorization-server",
        "/.well-known/oauth-authorization-server/gemini-mcp",
    ],
)
def test_gemini_oauth_metadata_advertises_pkce_public_client(client, path):
    response = client.get(path)

    assert response.status_code == 200
    metadata = response.json()
    assert metadata["issuer"] == settings.gemini_mcp_public_url
    assert metadata["registration_endpoint"] == f"{settings.gemini_mcp_public_url}/register"
    assert metadata["token_endpoint_auth_methods_supported"] == ["none"]
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    assert metadata["authorization_response_iss_parameter_supported"] is True
    assert response.headers["access-control-allow-origin"] == "*"


@pytest.mark.parametrize(
    "path",
    [
        "/gemini-mcp/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/gemini-mcp",
    ],
)
def test_gemini_protected_resource_metadata_uses_standard_discovery_paths(client, path):
    response = client.get(path)

    assert response.status_code == 200
    assert response.json() == {
        "resource": settings.gemini_mcp_public_url,
        "authorization_servers": [settings.gemini_mcp_public_url],
        "scopes_supported": [MCP_SCOPE],
        "bearer_methods_supported": ["header"],
    }


def test_gemini_dynamic_registration_allows_browser_preflight(client):
    response = client.options("/gemini-mcp/register")

    assert response.status_code == 204
    assert response.headers["access-control-allow-origin"] == "*"
    assert response.headers["access-control-allow-methods"] == "POST, OPTIONS"
