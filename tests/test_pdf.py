"""The server-side PDF renderer, and the cap that keeps it from taking the box.

Rendering moved out of the reader's browser and onto this machine, so the tests
that matter are the ones about bounds: the pool must not grow, a full queue must
fail fast instead of piling up, and an oversized document must be refused.
"""
from __future__ import annotations

import asyncio

import pytest

from app.config import settings
from app.services import pdf


@pytest.fixture(autouse=True)
def _fresh_gate():
    pdf._slots = None
    yield
    pdf._slots = None


def test_an_empty_document_is_refused():
    with pytest.raises(ValueError):
        asyncio.run(pdf.render_notes_pdf("   ", title="t"))


def test_an_oversized_document_is_refused(monkeypatch):
    monkeypatch.setattr(settings, "PDF_MAX_CHARS", 100)
    with pytest.raises(pdf.PDFTooLarge):
        asyncio.run(pdf.render_notes_pdf("x" * 200, title="t"))


def test_a_full_queue_gives_up_rather_than_piling_on(monkeypatch):
    """The whole point of the cap: say "busy" instead of queueing forever."""
    monkeypatch.setattr(settings, "PDF_RENDER_WORKERS", 1)
    monkeypatch.setattr(settings, "PDF_QUEUE_TIMEOUT_SECONDS", 0.05)
    pdf._slots = None

    async def hog_every_slot():
        sem = pdf._get_slots()
        # depth is workers * 3
        for _ in range(3):
            await sem.acquire()
        with pytest.raises(pdf.PDFBusy):
            await pdf.render_notes_pdf("# hi\n\nbody", title="t")

    asyncio.run(hog_every_slot())


def test_the_pool_is_capped_and_reused(monkeypatch):
    monkeypatch.setattr(settings, "PDF_RENDER_WORKERS", 2)
    pdf.shutdown()
    try:
        a = pdf._get_pool()
        b = pdf._get_pool()
        assert a is b, "a new pool per call would defeat the cap entirely"
        assert a._max_workers == 2
    finally:
        pdf.shutdown()


@pytest.mark.skipif(
    pytest.importorskip is None, reason="never skipped; placeholder"
)
def test_markdown_becomes_a_real_pdf():
    """Only runs where WeasyPrint's native stack is installed (the container)."""
    pytest.importorskip("weasyprint")
    pytest.importorskip("markdown")
    data = asyncio.run(
        pdf.render_notes_pdf(
            "# Title\n\n## Section\n\n- one\n- two\n\nBody text.",
            title="Test", subtitle="sub",
        )
    )
    assert data.startswith(b"%PDF"), "not a PDF"
    assert len(data) > 800
