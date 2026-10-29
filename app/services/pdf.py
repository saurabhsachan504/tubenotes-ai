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


# The ChatGPT plugin has its own compact, reader-first document treatment.
# Keep it separate from the website renderer so a plugin update cannot change
# a web user's established PDF appearance.
_PLUGIN_CSS = """
@page { size: A4; margin: 15mm 16mm 18mm 16mm;
        @bottom-center { content: counter(page) " / " counter(pages);
                         font-size: 8.5pt; color: #9a9aab; } }
body { font-family: "Noto Sans", "Noto Sans Devanagari", "Noto Sans Tamil",
       "Noto Sans Telugu", "Noto Sans Bengali", "Noto Sans Arabic", sans-serif;
       font-size: 10.25pt; line-height: 1.52; color: #20212b; }
h1 { font-size: 18pt; line-height: 1.25; margin: 0 0 2mm; color: #1f2030; }
h2 { break-after: avoid; font-size: 12.5pt; line-height: 1.3; color: #9146f5;
     border-left: 2.2pt solid #9146f5; margin: 6mm 0 2.4mm; padding: 0 0 0 2.6mm; }
h3 { break-after: avoid; font-size: 11pt; color: #6d34bf; margin: 4.5mm 0 1.5mm; }
p { margin: 0 0 2.6mm; }
ul, ol { margin: 0 0 3mm; padding-left: 6.3mm; }
li { margin: 0 0 1.15mm; }
strong { font-weight: 700; color: #252332; }
em { color: #625b78; }
a { color: #7c3aed; text-decoration: none; }
.tn-head { border-bottom: 0.5pt solid #e8e1f4; padding-bottom: 3.2mm; margin-bottom: 5.2mm; }
.tn-meta { font-size: 8.8pt; color: #756f82; margin-top: 1.3mm; overflow-wrap: anywhere; }
"""


# Claude receives an intentionally more expressive treatment. It is selected
# from the signed MCP download token, so the website, ChatGPT, and Gemini keep
# their existing document appearance.
_CLAUDE_CSS = """
@page { size: A4; margin: 14mm 15mm 18mm 15mm;
        @bottom-left { content: "TubeNotes  •  Claude Edition";
                       font-size: 8pt; color: #8b83a8; }
        @bottom-right { content: counter(page) " / " counter(pages);
                        font-size: 8pt; color: #8b83a8; } }
body { font-family: "Noto Sans", "Noto Sans Devanagari", "Noto Sans Tamil",
       "Noto Sans Telugu", "Noto Sans Bengali", "Noto Sans Arabic", sans-serif;
       font-size: 10.3pt; line-height: 1.58; color: #242035; }
h1 { font-size: 21pt; line-height: 1.18; margin: 0; color: #20143d; }
h2 { break-after: avoid; font-size: 13.2pt; line-height: 1.28; color: #47227f;
     background: #f4edff; border-left: 3.3pt solid #8b4df0; border-radius: 2mm;
     margin: 7mm 0 2.8mm; padding: 2.3mm 3mm; }
h2::before { content: "✦  "; color: #e05aa4; }
h3 { break-after: avoid; font-size: 11.3pt; color: #62409a; margin: 5mm 0 1.7mm; }
h3::before { content: "◆  "; color: #f29b3d; font-size: 8pt; vertical-align: 1pt; }
p { margin: 0 0 2.8mm; }
ul, ol { margin: 0 0 3.6mm; padding-left: 6.7mm; }
li { margin: 0 0 1.45mm; }
li::marker { color: #8b4df0; font-weight: 700; }
strong { color: #3e2172; font-weight: 700; background: #fff0a8;
         border-radius: 1mm; padding: 0 0.7mm; }
em { color: #80509d; }
blockquote { margin: 4mm 0; padding: 3mm 4mm; color: #4d4265;
             background: #eef8ff; border-left: 3pt solid #37a8e8; border-radius: 2mm; }
code { color: #63319e; background: #f1ecf8; border-radius: 1mm; padding: 0 0.8mm; }
a { color: #6f2dcc; text-decoration: none; font-weight: 700; }
.tn-head { background: linear-gradient(135deg, #f2eaff, #fff0f8);
            border: 0.6pt solid #e2d2fa; border-radius: 3mm; margin-bottom: 6mm;
            padding: 5mm 5mm 4mm; }
.tn-kicker { color: #8b4df0; font-size: 8pt; font-weight: 700; letter-spacing: 0.8pt;
             margin-bottom: 1.8mm; text-transform: uppercase; }
.tn-meta { font-size: 8.8pt; color: #716986; margin-top: 1.8mm; overflow-wrap: anywhere; }
"""


def _render_sync(markdown_text: str, title: str, subtitle: str, style: str) -> bytes:
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
    kicker = "<div class='tn-kicker'>TubeNotes • Claude Edition</div>" if style == "claude" else ""
    html = (
        "<html><head><meta charset='utf-8'></head><body>"
        f"<div class='tn-head'>{kicker}<h1>{safe_title}</h1>"
        f"<div class='tn-meta'>{safe_sub}</div></div>{body}</body></html>"
    )
    css = {"web": _CSS, "plugin": _PLUGIN_CSS, "claude": _CLAUDE_CSS}[style]
    return HTML(string=html).write_pdf(stylesheets=[CSS(string=css)])


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


async def render_notes_pdf(
    markdown_text: str, *, title: str, subtitle: str = "", style: str = "web"
) -> bytes:
    """Render a web, ChatGPT, or Claude PDF, waiting for a bounded slot."""
    if not markdown_text or not markdown_text.strip():
        raise ValueError("nothing to render")
    if style not in {"web", "plugin", "claude"}:
        raise ValueError("unknown PDF style")
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
            _get_pool(), _render_sync, markdown_text, title, subtitle, style
        )
    finally:
        sem.release()
