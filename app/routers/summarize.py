"""YouTube summarisation endpoints for the web app.

Billing rule, identical to the extension: **one trial = one video**. The
idempotency key sent to the entitlement engine is `video:<youtube_id>`, so the
summary, the key points and the full PDF notes for a single video all share one
charge, and re-summarising a video you already paid for is free forever.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import time
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.database import get_db
from app.deps import get_current_user
from app.models import User
from app.schemas import DeviceFingerprint, EntitlementOut
from app.services import entitlements, output_cache, ratelimit, summarizer, translate, youtube

logger = logging.getLogger("trialguard.summarize")
router = APIRouter(tags=["summarize"])


# ---------------------------------------------------------------------------
class VideoRequest(BaseModel):
    url: str = Field(min_length=5, max_length=500)
    device: DeviceFingerprint
    # None / "auto" => write in the video's own language.
    target_lang: str | None = Field(default=None, max_length=8)

    # The extension (and, later, the mobile app) reads the transcript inside
    # the user's own browser, where YouTube sees an ordinary viewer instead of
    # our server. When that text arrives we never touch YouTube at all - which
    # is why that path can never be rate-limited or IP-blocked.
    #
    # The cap is a memory guard, not a trust boundary: a caller who sends
    # nonsense only wastes their own trial, because billing happens before
    # this text is ever read.
    # A freshly generated summary returns the transcript to the same browser so
    # the follow-up Full Notes/PDF request can reuse it across uvicorn workers.
    # Nginx accepts 25 MB request bodies; this guard stays comfortably below it.
    transcript: str | None = Field(default=None, max_length=5_000_000)
    transcript_lang: str | None = Field(default=None, max_length=8)


class SummarizeRequest(VideoRequest):
    mode: str = Field(default="summary", pattern="^(summary|key_points|notes)$")


class TranslateRequest(BaseModel):
    text: str = Field(min_length=1, max_length=200_000)
    target_lang: str = Field(min_length=2, max_length=8)


class TranslateOut(BaseModel):
    text: str
    target_lang: str
    language_name: str


def _resolve_target(requested: str | None, detected: str) -> str:
    """The language the user actually gets."""
    if not requested or requested in ("auto", "same"):
        return detected
    return requested.split("-")[0].lower()


# Long enough to be a real transcript rather than an empty string or a stray
# word - the same threshold fetch_transcript() uses on its own results.
_MIN_CLIENT_TRANSCRIPT = 40


async def _obtain_transcript(payload: VideoRequest, video_id: str) -> youtube.Transcript:
    """Use the client's transcript when it sent one; otherwise fetch it here.

    Falling back matters: a visitor without the extension, or on a phone, still
    gets the old server-side path, so nothing that works today stops working.
    """
    supplied = (payload.transcript or "").strip()
    if len(supplied) >= _MIN_CLIENT_TRANSCRIPT:
        logger.info(
            "transcript supplied by client for %s (%s chars, lang=%s)",
            video_id,
            len(supplied),
            payload.transcript_lang,
        )
        # clean_transcript() is a regex sweep over as much as 5 MB. On the
        # event loop that stalls every other stream in this worker, which
        # matters more now that the worker count is low and each worker
        # carries many streams.
        cleaned = await run_in_threadpool(youtube.clean_transcript, supplied)
        return youtube.Transcript(
            text=cleaned,
            language=(payload.transcript_lang or "").split("-")[0].lower() or None,
            is_generated=True,
            source="client",
        )

    return await run_in_threadpool(youtube.fetch_transcript, video_id)


async def _prepare_video(payload: VideoRequest, video_id: str):
    """Fetch independent YouTube data concurrently and record each duration."""
    async def metadata():
        started = time.perf_counter()
        value = await youtube.fetch_metadata(video_id)
        return value, (time.perf_counter() - started) * 1000

    async def transcript():
        started = time.perf_counter()
        value = await _obtain_transcript(payload, video_id)
        return value, (time.perf_counter() - started) * 1000

    (meta, metadata_ms), (captions, transcript_ms) = await asyncio.gather(
        metadata(), transcript()
    )
    logger.info(
        "prepared %s: metadata=%.1fms transcript=%.1fms source=%s",
        video_id,
        metadata_ms,
        transcript_ms,
        captions.source,
    )
    return meta, captions, metadata_ms, transcript_ms


class VideoInfoOut(BaseModel):
    video_id: str
    title: str
    author: str | None
    thumbnail: str
    url: str


def _video_id_or_400(url: str) -> str:
    video_id = youtube.extract_video_id(url)
    if not video_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That doesn't look like a YouTube link. Paste a youtube.com or youtu.be URL.",
        )
    return video_id


def _event(payload: dict) -> bytes:
    """One NDJSON line - the browser reads these as they arrive."""
    return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
@router.post("/video/info", response_model=VideoInfoOut)
async def video_info(payload: VideoRequest, user: User = Depends(get_current_user)):
    """Title + thumbnail so the UI can show the video immediately.

    Deliberately free: it spends no trial, because nothing has been generated.
    """
    video_id = _video_id_or_400(payload.url)
    meta = await youtube.fetch_metadata(video_id)
    return VideoInfoOut(**dataclasses.asdict(meta))


def _store_cached(
    video_id: str,
    mode: str,
    target: str,
    model: str,
    text: str,
    detected: str,
    transcript_chars: int,
) -> None:
    """Cache me likho - APNI ALAG session me, threadpool par.

    Request ki apni session streaming ke poore samay khuli rehti hai. Usi par
    likhna do tarah se khatarnak hai, aur dono baar hum ye dekh chuke hain:

      * likhna fail ho jaye to session poisoned ho jaati hai (PendingRollback),
        aur uske baad us session se kuch bhi nahi ho paata.
      * lamba chalne wale transaction me row lock pakde rehna - isi ne poore
        app ko jama diya tha (Cloudflare 524).

    Isliye yahan apni chhoti session kholi jaati hai, likhkar turant band. Jo
    bhi bigde, wo yahin ruk jaata hai - user ka jawab to ja hi chuka hai.
    """
    gen = get_db()
    db = next(gen)
    try:
        output_cache.put(
            db,
            video_id,
            mode,
            target,
            model,
            text,
            detected_lang=detected,
            transcript_chars=transcript_chars,
        )
    except Exception:  # noqa: BLE001 - cache kabhi summary na todhe
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning("cache me rakhna fail hua: %s", video_id, exc_info=True)
    finally:
        try:
            next(gen)
        except StopIteration:
            pass
        except Exception:
            logger.exception("cache session band karne me dikkat")


@dataclasses.dataclass(slots=True)
class _CachedText:
    """A plain copy of a cache row, safe to read after the session is closed.

    The ORM object is not: the session is closed before streaming starts (see
    _charge_and_lookup), and touching a detached, expired instance after that
    raises rather than returning the summary.
    """

    text: str
    detected_lang: str
    transcript_chars: int


def _cache_lookup(db: Session, payload, video_id: str, *, mode: str | None = None):
    """(row, target, model). row None hai to cache me kuch nahi mila.

    Ye transcript laane se PEHLE chalta hai, isliye hit hone par YouTube par ek
    bhi request nahi jaati - aur wahi requests pehle rate-limit ka kaaran bani
    thi.

    Agar user ne bhasha nahi chuni ("video ki apni bhasha"), to hamein pata
    nahi ki wo kaun si hai - wo transcript se nikalti hai. Isliye cache se hi
    poocha jaata hai ki is video ki bhasha pehle kya nikli thi.
    """
    requested = (payload.target_lang or "").strip().lower()
    if requested in ("", "auto", "same"):
        detected = output_cache.detected_lang_for(db, video_id)
        if not detected:
            # Is video ko pehle kabhi nahi dekha - to cache me ho hi nahi sakti.
            return None, "", ""
    else:
        detected = ""
    # Wahi function jo saamanya raaste me chalta hai, taaki dono kabhi alag na
    # ho jayein.
    target = _resolve_target(payload.target_lang, detected)
    model = summarizer.plan_for(target)[0]
    output_mode = mode or payload.mode
    row = output_cache.get(db, video_id, output_mode, target, model)
    snapshot = (
        None
        if row is None
        else _CachedText(
            text=row.text,
            detected_lang=row.detected_lang or "",
            transcript_chars=row.transcript_chars,
        )
    )
    return snapshot, target, model


def _charge_and_lookup(db: Session, user: User, payload, video_id: str, *, action: str, mode: str):
    """Every database touch of this request, in ONE blocking call.

    This runs on a worker thread, and that is the whole point. It used to run
    inline in an `async def` endpoint, where entitlements.consume()'s
    SELECT ... FOR UPDATE blocks a psycopg socket - and a blocked socket on the
    event loop freezes the entire uvicorn worker, every other user's stream
    along with it. Two such stalls froze the whole API.

    The session is closed here too. Leaving it to the request-scoped dependency
    meant the cache-lookup SELECT sat idle-in-transaction for as long as the
    response streamed - up to half an hour for a set of notes.
    """
    try:
        result = entitlements.consume(
            db,
            user,
            payload.device,
            action=action,
            idempotency_key=f"video:{video_id}",
            meta={"video_id": video_id, "mode": mode, "surface": "web"},
        )
        db.commit()
        entitlement: EntitlementOut = result.entitlement
        cached_row, cached_target, cached_model = _cache_lookup(
            db, payload, video_id, mode=mode
        )
        return entitlement, cached_row, cached_target, cached_model
    finally:
        # Idempotent - get_db()'s own finally will call it again harmlessly.
        db.close()


async def _replay_cached(row, *, meta, target: str, model: str, entitlement):
    """Cache se mili summary ko usi shakl me bhejo jaisi taazi banti hai.

    Ek hi delta me poora text - saamanya raaste jaisa hi kram (meta, delta,
    done), taaki UI me kuch alag na karna pade.
    """
    yield _event(
        {
            "type": "meta",
            "video": dataclasses.asdict(meta),
            "detected_language": row.detected_lang or target,
            "detected_language_name": summarizer.language_name(row.detected_lang or target),
            "language": target,
            "language_name": summarizer.language_name(target),
            "model": model,
            "transcript_chars": row.transcript_chars,
            "transcript_source": "cache",
            "cached": True,
            "entitlement": json.loads(entitlement.model_dump_json()),
        }
    )
    yield _event({"type": "delta", "text": row.text})
    yield _event({"type": "done", "text": row.text, "language": target, "cached": True})


@router.post("/summarize")
async def summarize(
    payload: SummarizeRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Stream a summary in the video's own language.

    Response is NDJSON, one JSON object per line:
      {"type":"meta",  "video": {...}, "language": "hi", "entitlement": {...}}
      {"type":"delta", "text": "..."}          (many)
      {"type":"done",  "text": "<full markdown>"}
      {"type":"error", "message": "..."}
    """
    video_id = _video_id_or_400(payload.url)

    # 1. Charge, and 2. look in the cache - both on a worker thread, and the
    #    session closed before a single byte is streamed. Cache hit means the
    #    same video, mode, language and model, so YouTube is never touched.
    entitlement, cached_row, cached_target, cached_model = await run_in_threadpool(
        _charge_and_lookup,
        db,
        user,
        payload,
        video_id,
        action=f"summarize:{payload.mode}",
        mode=payload.mode,
    )
    if cached_row is not None:
        meta = await youtube.fetch_metadata(video_id)
        return StreamingResponse(
            _replay_cached(
                cached_row,
                meta=meta,
                target=cached_target,
                model=cached_model,
                entitlement=entitlement,
            ),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    # Metadata and captions are independent network calls. Running them in
    # parallel removes the metadata round trip from the critical path.
    try:
        meta, transcript, metadata_ms, transcript_ms = await _prepare_video(
            payload, video_id
        )
    except youtube.TranscriptUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    detected = summarizer.detect_language(transcript.text, hint=transcript.language)
    target = _resolve_target(payload.target_lang, detected)
    model, write_lang, translate_to = summarizer.plan_for(target)

    async def generate() -> AsyncIterator[bytes]:
        yield _event(
            {
                "type": "meta",
                "video": dataclasses.asdict(meta),
                "detected_language": detected,
                "detected_language_name": summarizer.language_name(detected),
                "language": target,
                "language_name": summarizer.language_name(target),
                "model": model,
                "transcript_chars": len(transcript.text),
                "transcript_source": transcript.source,
                # Full Notes is a second HTTP request and may hit another
                # uvicorn worker. Hand the already-fetched transcript back to
                # this browser so that request never has to ask YouTube again.
                "transcript": transcript.text,
                "transcript_lang": transcript.language,
                "timings_ms": {
                    "metadata": round(metadata_ms, 1),
                    "transcript": round(transcript_ms, 1),
                },
                "entitlement": json.loads(entitlement.model_dump_json()),
            }
        )

        collected: list[str] = []
        try:
            async for delta in summarizer.stream_summary(
                transcript.text, lang=write_lang, mode=payload.mode
            ):
                collected.append(delta)
                yield _event({"type": "delta", "text": delta})
        except Exception as exc:  # noqa: BLE001 - surface it to the UI
            logger.exception("summary failed for %s", video_id)
            if collected:
                # Partial output is still useful - hand it over rather than
                # throwing away what the model already wrote.
                yield _event({"type": "done", "text": "".join(collected), "partial": True})
            else:
                yield _event({"type": "error", "message": _friendly(exc)})
            return

        text = "".join(collected)

        # Two ways the text can end up in the wrong language: we deliberately
        # wrote English for a language the model handles badly, or the model
        # simply ignored the instruction. Both are fixed the same way.
        if translate_to:
            yield _event({"type": "status", "message": f"Translating to {summarizer.language_name(translate_to)}…"})
            try:
                text = await translate.translate(text, translate_to)
            except Exception:  # pragma: no cover - network
                logger.warning("translation to %s failed", translate_to)
        else:
            text, fixed = await translate.ensure_language(text, target)
            if fixed:
                yield _event(
                    {
                        "type": "status",
                        "message": f"The model answered in the wrong language - translated to {summarizer.language_name(target)}.",
                    }
                )

        yield _event({"type": "done", "text": text, "language": target})

        # Jama karna SABSE AAKHIR me - user ka jawab ja chuka hai, to yahan
        # kuch bigde bhi to farq nahi padta. Cache band ho to _store_cached()
        # khud hi kuch nahi karta.
        if _may_cache(transcript):
            await run_in_threadpool(
                _store_cached,
                video_id,
                payload.mode,
                target,
                model,
                text,
                detected,
                len(transcript.text),
            )

    return StreamingResponse(
        generate(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.post("/notes")
async def notes(
    payload: VideoRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Full, chunked notes covering the whole video - what the PDF is built from.

    Same idempotency key as /summarize, so a user who already summarised this
    video is not charged again for the PDF.
    """
    video_id = _video_id_or_400(payload.url)

    entitlement, cached_row, cached_target, cached_model = await run_in_threadpool(
        _charge_and_lookup,
        db,
        user,
        payload,
        video_id,
        action="notes",
        mode="notes",
    )
    if cached_row is not None:
        meta = await youtube.fetch_metadata(video_id)
        return StreamingResponse(
            _replay_cached(
                cached_row,
                meta=meta,
                target=cached_target,
                model=cached_model,
                entitlement=entitlement,
            ),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    try:
        meta, transcript, metadata_ms, transcript_ms = await _prepare_video(
            payload, video_id
        )
    except youtube.TranscriptUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    detected = summarizer.detect_language(transcript.text, hint=transcript.language)
    target = _resolve_target(payload.target_lang, detected)
    model, lang, translate_to = summarizer.plan_for(target)

    async def generate() -> AsyncIterator[bytes]:
        queue: list[bytes] = []

        async def on_progress(done: int, total: int) -> None:
            queue.append(
                _event(
                    {
                        "type": "progress",
                        "done": done,
                        "total": total,
                        "percent": round(done / total * 100),
                    }
                )
            )

        async def on_warning(message: str) -> None:
            # Anything that could make the notes incomplete is surfaced, never
            # swallowed - the whole promise of this feature is completeness.
            queue.append(_event({"type": "warning", "message": message}))

        yield _event(
            {
                "type": "meta",
                "video": dataclasses.asdict(meta),
                "detected_language": detected,
                "detected_language_name": summarizer.language_name(detected),
                "language": target,
                "language_name": summarizer.language_name(target),
                "transcript_source": transcript.source,
                "timings_ms": {
                    "metadata": round(metadata_ms, 1),
                    "transcript": round(transcript_ms, 1),
                },
            }
        )

        # full_notes reports progress through the callback; we drain the queue
        # between chunks so the browser sees a live progress bar.
        task = asyncio.create_task(
            summarizer.full_notes(
                transcript.text,
                lang=lang,
                on_progress=on_progress,
                on_warning=on_warning,
            )
        )
        try:
            while not task.done():
                await asyncio.sleep(0.4)
                while queue:
                    yield queue.pop(0)
            while queue:
                yield queue.pop(0)
            text = await task
        except Exception as exc:  # noqa: BLE001
            logger.exception("notes failed for %s", video_id)
            yield _event({"type": "error", "message": _friendly(exc)})
            return
        finally:
            # The browser going away - tab closed, refresh, proxy timeout -
            # cancels this generator at the await above. Without this the task
            # carried on writing a 30-minute set of notes that nobody would
            # ever read, holding a vLLM slot for every chunk of it. Every
            # abandoned tab permanently cost the GPU.
            #
            # No await here: cancel() is a plain call, and awaiting during a
            # GeneratorExit unwind is an error.
            if not task.done():
                task.cancel()

        if not text:
            yield _event({"type": "error", "message": "The model returned nothing. Please try again."})
            return

        if translate_to:
            yield _event({"type": "status", "message": f"Translating to {summarizer.language_name(translate_to)}…"})
            try:
                text = await translate.translate(text, translate_to)
            except Exception:  # pragma: no cover - network
                logger.warning("notes translation to %s failed", translate_to)
        else:
            text, _fixed = await translate.ensure_language(text, target)

        yield _event({"type": "done", "text": text, "language": target})

        if _may_cache(transcript):
            await run_in_threadpool(
                _store_cached,
                video_id,
                "notes",
                target,
                model,
                text,
                detected,
                len(transcript.text),
            )

    return StreamingResponse(
        generate(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.post("/translate", response_model=TranslateOut)
async def translate_text(
    payload: TranslateRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Translate an already-generated summary or notes into another language.

    No trial is charged: the video was already paid for, this is just a
    presentation of the same result. Rate-limited so it cannot be used as a free
    general-purpose translation API.
    """
    await run_in_threadpool(_rate_limit_translate, db, user.id)

    target = payload.target_lang.split("-")[0].lower()
    try:
        text = await translate.translate(payload.text, target)
    except Exception as exc:  # noqa: BLE001
        logger.warning("translate failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Translation service is unreachable right now. Please try again.",
        )
    return TranslateOut(
        text=text, target_lang=target, language_name=summarizer.language_name(target)
    )


def _rate_limit_translate(db: Session, user_id) -> None:
    """The rate-limit write, on a worker thread, session closed after."""
    try:
        ratelimit.hit(db, f"translate:{user_id}", limit=60, window_seconds=3600)
        db.commit()
    finally:
        db.close()


def _may_cache(transcript) -> bool:
    """Never let the caching DECISION break a summary that already shipped.

    is_cacheable() is a pure check today, but it is the gate on sharing another
    user's content - if it ever raises, the honest answer is "do not share",
    not "abort the request the user already paid for".
    """
    try:
        return bool(youtube.is_cacheable(transcript))
    except Exception:  # noqa: BLE001
        logger.warning("is_cacheable() failed; not caching", exc_info=True)
        return False


def _friendly(exc: Exception) -> str:
    if isinstance(exc, summarizer.VLLMBusy):
        # Already written for a human: the server is full, come back shortly.
        return str(exc)
    text = str(exc)
    if "vLLM HTTP 404" in text:
        return f"The vLLM endpoint or model '{settings.VLLM_MODEL}' was not found."
    if "ConnectError" in type(exc).__name__ or "Connect" in text:
        return "Can't reach vLLM. Check VLLM_URL and that the vLLM server is running."
    if "Timeout" in type(exc).__name__ or "timeout" in text.lower():
        return "The AI server took too long to answer. Try a shorter video."
    return f"Summary failed: {text[:200]}"
