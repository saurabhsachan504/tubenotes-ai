from __future__ import annotations

from datetime import timedelta

from app.config import settings
from app.models import McpTranscriptCache
from app.services import youtube
from app.services.mcp_server import (
    _load_mcp_transcript,
    _selected_mcp_language,
    _store_mcp_transcript,
)
from tests.conftest import register


def test_mcp_language_requires_an_explicit_supported_code():
    assert _selected_mcp_language({}) is None
    assert _selected_mcp_language({"language": "auto"}) is None
    assert _selected_mcp_language({"language": "hi"}) == "hi"


def test_mcp_transcript_is_reused_only_for_the_same_signed_in_user(client, db, device, monkeypatch):
    first, _headers, _ = register(client, email="first-mcp@example.com", device=device)
    second, _headers, _ = register(client, email="second-mcp@example.com")
    monkeypatch.setattr(settings, "MCP_TRANSCRIPT_CACHE_TTL_MINUTES", 60)
    transcript = youtube.Transcript(
        text="A complete transcript that belongs only to the first MCP user.",
        language="en",
        is_generated=True,
        source="captions",
    )

    _store_mcp_transcript(first["user"]["id"], "dQw4w9WgXcQ", transcript)

    reused = _load_mcp_transcript(first["user"]["id"], "dQw4w9WgXcQ")
    assert reused is not None
    assert reused.text == transcript.text
    assert _load_mcp_transcript(second["user"]["id"], "dQw4w9WgXcQ") is None

    row = db.query(McpTranscriptCache).one()
    row.expires_at = row.expires_at - timedelta(hours=2)
    db.commit()
    assert _load_mcp_transcript(first["user"]["id"], "dQw4w9WgXcQ") is None
