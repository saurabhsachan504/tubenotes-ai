"""Small, failure-proof audit writes for the admin dashboard.

These writes intentionally use a short independent SQLAlchemy session.  A
streaming request can live for many minutes; sharing its request session here
would hold a transaction open for the whole video generation.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.database import SessionLocal
from app.models import ProcessingJob


def _now() -> datetime:
    return datetime.now(timezone.utc)


def estimate_output_tokens(text: str) -> int:
    """Conservative display estimate until vLLM returns token usage per stream.

    The model's tokenizer is not installed in every web worker.  This avoids
    pretending word count is a precise token count while giving the operator a
    useful, consistent size signal.  The UI labels it as an estimate.
    """
    return max(0, round(len(text or "") / 4))


def start(
    *,
    user_id: str,
    video_id: str | None,
    video_url: str | None,
    kind: str,
    client_source: str = "web",
    language: str | None = None,
    request_city: str | None = None,
    request_country: str | None = None,
) -> str:
    db = SessionLocal()
    try:
        job = ProcessingJob(
            user_id=user_id,
            video_id=video_id,
            video_url=(video_url or "")[:500] or None,
            kind=kind,
            client_source=(client_source if client_source in {"web", "extension"} else "web"),
            language=(language or "")[:32] or None,
            request_city=(request_city or "Unknown")[:120],
            request_country=(request_country or "Unknown")[:8].upper(),
            status="processing",
        )
        db.add(job)
        db.commit()
        return job.id
    except Exception:
        # Observability must never stop a user's paid/credited generation.  A
        # missing migration or temporary DB issue simply omits this one row.
        db.rollback()
        return ""
    finally:
        db.close()


def finish(
    job_id: str,
    *,
    status: str,
    title: str | None = None,
    language: str | None = None,
    pdf_generated: bool = False,
    cached: bool = False,
    output_text: str | None = None,
    error_message: str | None = None,
) -> None:
    if not job_id:
        return
    db = SessionLocal()
    try:
        job = db.get(ProcessingJob, job_id)
        if job is None:
            return
        finished = _now()
        job.status = status
        job.finished_at = finished
        started = job.started_at if job.started_at.tzinfo else job.started_at.replace(tzinfo=timezone.utc)
        job.duration_ms = max(0, round((finished - started).total_seconds() * 1000))
        if title:
            job.title = title[:500]
        if language:
            job.language = language[:32]
        job.pdf_generated = pdf_generated
        job.cached = cached
        if output_text is not None:
            job.output_chars = len(output_text)
            job.output_tokens = estimate_output_tokens(output_text)
        if error_message:
            job.error_message = error_message[:500]
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()
