"""Server-side PDF rendering for the full notes.

WHY THIS IS BOUNDED, AND WHY THAT IS THE WHOLE DESIGN
-----------------------------------------------------
Rendering used to happen in the reader's own browser, which cost the server
nothing and could never be overloaded. Doing it here moves that cost onto one
machine, so the only safe shape is a queue in front of a small pool - never one
renderer per request.

The machine makes that sharper than usual. This is a GB10: CPU and GPU share a
single 128 GB pool, and vLLM is already holding ~76 GB of it. Memory spent
rendering is memory taken from the model's KV cache, so an unbounded renderer
would not merely slow PDFs down - it would starve generation. Hence a hard cap
of PDF_RENDER_WORKERS processes and a queue that fails fast when full.

Processes, not threads: rendering is CPU-bound Python, and thirty concurrent
yt-dlp calls already proved on this codebase that CPU-bound work inside the API
process starves the event loop through the GIL.

WeasyPrint rather than ReportLab because the notes are written in the video's
own language - Hindi, Tamil, Telugu, Bengali, Urdu. Those need real text
shaping, and WeasyPrint gets it from Pango. ReportLab would draw broken glyphs.
"""
from __future__ import annotations

import asyncio
import logging
import os
from concurrent.futures import ProcessPoolExecutor

from app.config import settings

logger = logging.getLogger("trialguard.pdf")

_pool: ProcessPoolExecutor | None = None
_slots: asyncio.Semaphore | None = None


class PDFBusy(RuntimeError):
    """The render queue was full for longer than the caller would wait."""


class PDFTooLarge(RuntimeError):
    """The document is past the size this endpoint will render."""


# ---------------------------------------------------------------------------
# Rendering (runs inside a pool process - no app state, no event loop)
# ---------------------------------------------------------------------------
_CSS = """
@page { size: A4; margin: 18mm 16mm 20mm 16mm;
        @bottom-center { content: counter(page) " / " counter(pages);
                         font-size: 9pt; color: #888; } }
body { font-family: "Noto Sans", "Noto Sans Devanagari", "Noto Sans Tamil",
       "Noto Sans Telugu", "Noto Sans Bengali", "Noto Sans Arabic", sans-serif;
       font-size: 10.5pt; line-height: 1.55; color: #1a1a1a; }
h1 { font-size: 19pt; margin: 0 0 4mm; line-height: 1.25; }
h2 { font-size: 13.5pt; margin: 7mm 0 2mm; color: #12304f;
     border-bottom: 0.4pt solid #d5dde5; padding-bottom: 1.2mm; }
h3 { font-size: 11.5pt; margin: 5mm 0 1.5mm; color: #24425f; }
p  { margin: 0 0 2.6mm; }
ul, ol { margin: 0 0 3mm; padding-left: 6mm; }
li { margin: 0 0 1.2mm; }
em { color: #7a4a00; }
a { color: #14568a; text-decoration: none; }
.tn-head { border-bottom: 1.2pt solid #12304f; padding-bottom: 3mm; margin-bottom: 6mm; }
.tn-meta { font-size: 9pt; color: #6b7785; margin-top: 1.5mm; }
"""


def _render_sync(markdown_text: str, title: str, subtitle: str) -> bytes:
    """Markdown -> styled PDF. Imported lazily so the API boots without these."""
    import markdown as md
    from weasyprint import CSS, HTML

    body = md.markdown(
        markdown_text or "",
        extensions=["extra", "sane_lists", "nl2br"],
        output_format="html5",
    )
    safe_title = (title or "Notes").replace("<", "&lt;").replace(">", "&gt;")
    safe_sub = (subtitle or "").replace("<", "&lt;").replace(">", "&gt;")
    html = (
        "<html><head><meta charset='utf-8'></head><body>"
        f"<div class='tn-head'><h1>{safe_title}</h1>"
        f"<div class='tn-meta'>{safe_sub}</div></div>{body}</body></html>"
    )
    return HTML(string=html).write_pdf(stylesheets=[CSS(string=_CSS)])


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------
def _get_pool() -> ProcessPoolExecutor:
    global _pool
    if _pool is None:
        workers = max(1, settings.PDF_RENDER_WORKERS)
        _pool = ProcessPoolExecutor(max_workers=workers)
        logger.info("pdf render pool: %s process(es)", workers)
    return _pool


def _get_slots() -> asyncio.Semaphore:
    global _slots
    if _slots is None:
        # Depth, not just width: the pool queues internally forever, which
        # would turn a burst into a pile of requests all waiting on a timeout
        # far away. This bounds how many may be in the system at once.
        _slots = asyncio.Semaphore(max(1, settings.PDF_RENDER_WORKERS) * 3)
    return _slots


def shutdown() -> None:
    global _pool
    pool, _pool = _pool, None
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)


async def render_notes_pdf(markdown_text: str, *, title: str, subtitle: str = "") -> bytes:
    """Render, waiting for a slot. Raises PDFBusy rather than queueing forever."""
    if not markdown_text or not markdown_text.strip():
        raise ValueError("nothing to render")
    if len(markdown_text) > settings.PDF_MAX_CHARS:
        raise PDFTooLarge(
            f"These notes are {len(markdown_text):,} characters, past the "
            f"{settings.PDF_MAX_CHARS:,} this renderer accepts."
        )

    sem = _get_slots()
    wait = settings.PDF_QUEUE_TIMEOUT_SECONDS
    try:
        if wait and wait > 0:
            await asyncio.wait_for(sem.acquire(), wait)
        else:
            await sem.acquire()
    except (asyncio.TimeoutError, TimeoutError):
        raise PDFBusy(
            "The PDF service is busy right now. Your notes are safe - "
            "try the download again in a moment."
        ) from None

    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            _get_pool(), _render_sync, markdown_text, title, subtitle
        )
    finally:
        sem.release()
