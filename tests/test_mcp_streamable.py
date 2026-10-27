from __future__ import annotations


def _request(client, method: str, params: dict | None = None, request_id: int = 1):
    return client.post(
        "/mcp/",
        json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}},
    )


def test_public_mcp_lists_oauth_protected_tools_without_login(client):
    initialized = _request(
        client,
        "initialize",
        {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
    )
    assert initialized.status_code == 200
    assert initialized.json()["result"]["capabilities"]["tools"] == {"listChanged": False}

    listed = _request(client, "tools/list")
    assert listed.status_code == 200
    tools = {tool["name"]: tool for tool in listed.json()["result"]["tools"]}
    assert tools["summarize_youtube_video"]["securitySchemes"] == [{"type": "oauth2", "scopes": ["tubenotes:summary"]}]
    assert tools["get_tubenotes_profile"]["_meta"]["openai/profile"] is True


def test_public_mcp_tool_call_requests_oauth_login(client):
    response = _request(
        client,
        "tools/call",
        {"name": "summarize_youtube_video", "arguments": {"video_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}},
    )
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["isError"] is True
    challenge = result["_meta"]["mcp/www_authenticate"][0]
    assert "resource_metadata=" in challenge
    assert "Connect your TubeNotes account" in challenge
