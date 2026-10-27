from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlparse

from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull

from app.services.mcp_oauth import (
    MCP_SCOPE,
    TubeNotesOAuthProvider,
    approve_authorization_request,
    authorization_request_details,
)
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
                resource="https://tubenotes.ai/mcp",
            ),
        )
        request_id = parse_qs(urlparse(redirect).query)["request"][0]
        assert authorization_request_details(request_id) == {
            "client_name": "ChatGPT",
            "scope": MCP_SCOPE,
        }
        callback = approve_authorization_request(request_id, user_id)
        code = parse_qs(urlparse(callback).query)["code"][0]
        assert parse_qs(urlparse(callback).query)["state"] == ["state-123"]
        stored = await provider.load_authorization_code(client_info, code)
        assert stored is not None
        assert stored.user_id == user_id
        token = await provider.exchange_authorization_code(client_info, stored)
        assert token.access_token
        assert await provider.load_authorization_code(client_info, code) is None
        access = await provider.load_access_token(token.access_token)
        assert access is not None
        assert access.user_id == user_id
        assert access.scopes == [MCP_SCOPE]

    asyncio.run(flow())


def test_mcp_oauth_approval_requires_signed_in_user(client, device):
    body, _headers, _device = register(client, email="mcp-api@example.com", device=device)
    response = client.post("/api/v1/mcp/oauth/approve", json={"request_id": "x" * 32})
    assert response.status_code == 401
    assert body["user"]["email"] == "mcp-api@example.com"
