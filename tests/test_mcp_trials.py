from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.config import settings
from app.models import McpTrialLedger, McpTrialUsage, User
from app.routers.summarize import SummarizeRequest
from app.services.mcp_server import _claim_mcp_trial
from tests.conftest import make_device, register


def _payload(video_id: str) -> SummarizeRequest:
    return SummarizeRequest(
        url=f"https://www.youtube.com/watch?v={video_id}",
        target_lang="hi",
        device=make_device(),
        mode="summary",
    )


def test_mcp_machine_gets_twenty_trials_across_accounts(client, db, device, monkeypatch):
    monkeypatch.setattr(settings, "MCP_FREE_TRIAL_LIMIT", 20)
    first, _headers, _ = register(client, email="mcp-limit-one@example.com", device=device)
    second, _headers, _ = register(client, email="mcp-limit-two@example.com")
    first_user = db.get(User, first["user"]["id"])
    second_user = db.get(User, second["user"]["id"])
    machine_hash = "a" * 64

    for number in range(20):
        video_id = f"video{number:06d}"[-11:]
        entitlement, *_ = _claim_mcp_trial(
            db, first_user, machine_hash, video_id, _payload(video_id), "summary"
        )
        assert entitlement.allowed

    ledger = db.get(McpTrialLedger, machine_hash)
    assert ledger is not None and ledger.trials_used == 20
    assert db.query(McpTrialUsage).count() == 20

    with pytest.raises(HTTPException) as blocked:
        _claim_mcp_trial(
            db, second_user, machine_hash, "anothervid1", _payload("anothervid1"), "summary"
        )
    assert blocked.value.status_code == 402

    # A PDF/retry for an already unlocked video remains free after the cap.
    unlocked, *_ = _claim_mcp_trial(
        db, second_user, machine_hash, "video000000", _payload("video000000"), "notes"
    )
    assert unlocked.allowed
