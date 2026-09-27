"""YouTube URL parsing, metadata and transcript retrieval.

The Chrome extension scrapes the transcript inside the user's own browser, which
is fast and never rate-limited because the request comes from a logged-in
residential client. A web app cannot do that (CORS forbids reading youtube.com
from our page), so the fetch happens here instead - and YouTube does throttle
datacenter IPs. The strategy is therefore:

    1. youtube-transcript-api   - fastest, uses the caption tracks directly
    2. yt-dlp                   - slower but a different code path, often works
                                  when (1) is blocked
    3. clear error              - so the UI can say something honest

Set YOUTUBE_PROXY to a residential/rotating proxy if your server's IP gets
blocked; both paths pick it up automatically.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

import httpx

from app.config import settings

logger = logging.getLogger("trialguard.youtube")

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


class TranscriptUnavailable(Exception):
    """No usable transcript could be retrieved."""


@dataclass(slots=True)
class VideoMeta:
    video_id: str
    title: str
    author: str | None
    thumbnail: str
    url: str
    # Did YouTube's oEmbed endpoint answer for this video WITHOUT any login?
    # True  - anonymously reachable, so its transcript is not a secret.
    # False - private, members-only, deleted or nonexistent.
    # None  - the lookup itself failed, so we know nothing.
    #
    # This costs no extra request: fetch_metadata() already calls oEmbed for
    # the title and thumbnail. Measured: 200 for public, 400 for private and
    # deleted alike.
    public: bool | None = None


@dataclass(slots=True)
class Transcript:
    text: str
    language: str | None
    is_generated: bool
    source: str  # "captions" | "yt-dlp" | "client"
    # YouTube ka apna "availability": public / unlisted / private /
    # subscriber_only / premium_only / needs_auth. Sirf yt-dlp wala raasta ise
    # bharta hai, kyunki wo info pehle se laata hai; captions wala raasta ye
    # nahi jaanta, isliye None. None ka matlab "pata nahi" hai, "kharab" nahi -
    # is_cacheable() usse sambhalta hai.
    availability: str | None = None


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------
def extract_video_id(url_or_id: str) -> str | None:
    """Accept every YouTube URL shape, or a bare 11-character id."""
    value = (url_or_id or "").strip()
    if not value:
        return None
    if _ID_RE.match(value):
        return value

    if "://" not in value:
        value = "https://" + value

    try:
        u = urlparse(value)
    except ValueError:
        return None

    host = (u.hostname or "").lower().removeprefix("www.").removeprefix("m.")

    if host == "youtu.be":
        candidate = u.path.lstrip("/").split("/")[0]
        return candidate if _ID_RE.match(candidate) else None

    if host not in {"youtube.com", "music.youtube.com", "youtube-nocookie.com"}:
        return None

    if u.path == "/watch":
        candidate = (parse_qs(u.query).get("v") or [""])[0]
        return candidate if _ID_RE.match(candidate) else None

    # /shorts/<id>, /embed/<id>, /live/<id>, /v/<id>
    parts = [p for p in u.path.split("/") if p]
    if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live", "v"}:
        return parts[1] if _ID_RE.match(parts[1]) else None

    return None


def watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------
_metadata_cache: dict[str, VideoMeta] = {}
_METADATA_CACHE_MAX = 5000
_oembed_client: httpx.AsyncClient | None = None


def _get_oembed_client() -> httpx.AsyncClient:
    """Return the worker's shared oEmbed connection pool."""
    global _oembed_client
    if _oembed_client is None or _oembed_client.is_closed:
        _oembed_client = httpx.AsyncClient(
            timeout=settings.YOUTUBE_REQUEST_TIMEOUT_SECONDS,
            proxy=settings.YOUTUBE_PROXY or None,
        )
    return _oembed_client


async def close_oembed_client() -> None:
    global _oembed_client
    client, _oembed_client = _oembed_client, None
    if client is not None and not client.is_closed:
        await client.aclose()


async def fetch_metadata(video_id: str) -> VideoMeta:
    """Title/author via the public oEmbed endpoint (no API key needed)."""
    cached = _metadata_cache.get(video_id)
    if cached is not None:
        return cached

    url = watch_url(video_id)
    title, author = f"YouTube video {video_id}", None
    public: bool | None = None

    try:
        res = await _get_oembed_client().get(
            "https://www.youtube.com/oembed",
            params={"url": url, "format": "json"},
        )
        if res.status_code == 200:
            data = res.json()
            title = data.get("title") or title
            author = data.get("author_name")
            public = True
        elif res.status_code in (400, 401, 403, 404):
            # YouTube refuses oEmbed for anything that needs a login.
            public = False
    except Exception as exc:  # pragma: no cover - network
        # Unknown, not "private". is_cacheable() treats None as "do not share".
        logger.info("oEmbed lookup failed for %s: %s", video_id, exc)

    meta = VideoMeta(
        video_id=video_id,
        title=title,
        author=author,
        thumbnail=f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
        url=url,
        public=public,
    )
    _metadata_cache[video_id] = meta
    # Bounded: this dict used to grow for the life of the process.
    if len(_metadata_cache) > _METADATA_CACHE_MAX:
        for stale in list(_metadata_cache)[: len(_metadata_cache) - _METADATA_CACHE_MAX]:
            _metadata_cache.pop(stale, None)
    return meta


# ---------------------------------------------------------------------------
# Transcript
# ---------------------------------------------------------------------------
def _proxy_config():
    if not settings.YOUTUBE_PROXY:
        return None
    from youtube_transcript_api.proxies import GenericProxyConfig

    return GenericProxyConfig(
        http_url=settings.YOUTUBE_PROXY, https_url=settings.YOUTUBE_PROXY
    )


_TITLE_SCRIPT_HINTS = (
    (re.compile(r"[\u0980-\u09ff]"), "bn"),
    (re.compile(r"[\u0a80-\u0aff]"), "gu"),
    (re.compile(r"[\u0a00-\u0a7f]"), "pa"),
    (re.compile(r"[\u0b80-\u0bff]"), "ta"),
    (re.compile(r"[\u0c00-\u0c7f]"), "te"),
    (re.compile(r"[\u0c80-\u0cff]"), "kn"),
    (re.compile(r"[\u0d00-\u0d7f]"), "ml"),
    (re.compile(r"[\u0b00-\u0b7f]"), "or"),
    (re.compile(r"[\u0900-\u097f]"), "hi"),
)


def title_language_hint(title: str | None) -> str | None:
    """Return a safe caption preference from a title's unambiguous script.

    YouTube occasionally marks an auto-caption track as the wrong language.
    A Hindi title plus an available Hindi caption is a much better selection
    signal than that incorrect default.  This is only a *track preference*;
    summarizer.detect_language still reads the transcript text itself before
    choosing the summary language.
    """
    text = title or ""
    for pattern, language in _TITLE_SCRIPT_HINTS:
        if len(pattern.findall(text)) >= 2:
            return language
    return None


def _pick_track(transcript_list, preferred_language: str | None = None):
    """Choose the track that is actually in the SPOKEN language.

    A creator can add manual subtitles in a different language (Hindi subs on a
    Punjabi video), so "manual first" picks the wrong one. The auto-generated
    (ASR) track is always the spoken language - use its code to decide, and
    prefer a manual track in that same language when one exists.
    """
    tracks = list(transcript_list)
    if not tracks:
        return None

    preferred = (preferred_language or "").split("-")[0].lower()
    if preferred:
        matching = [
            t for t in tracks
            if (t.language_code or "").split("-")[0].lower() == preferred
        ]
        if matching:
            # A creator-provided transcript is clearer than ASR when both
            # describe the same preferred language.
            return next((t for t in matching if not t.is_generated), matching[0])

    asr = next((t for t in tracks if t.is_generated), None)
    if asr is not None:
        spoken = (asr.language_code or "").split("-")[0].lower()
        manual_same = next(
            (
                t
                for t in tracks
                if not t.is_generated
                and (t.language_code or "").split("-")[0].lower() == spoken
            ),
            None,
        )
        return manual_same or asr

    return next((t for t in tracks if not t.is_generated), tracks[0])


def _fetch_via_api(video_id: str, preferred_language: str | None = None) -> Transcript:
    import requests
    from youtube_transcript_api import YouTubeTranscriptApi

    class _TimeoutSession(requests.Session):
        def request(self, *args, **kwargs):
            kwargs.setdefault("timeout", settings.YOUTUBE_REQUEST_TIMEOUT_SECONDS)
            return super().request(*args, **kwargs)

    with _TimeoutSession() as session:
        api = YouTubeTranscriptApi(
            proxy_config=_proxy_config(), http_client=session
        )
        listing = api.list(video_id)
        track = _pick_track(listing, preferred_language)
        if track is None:
            raise TranscriptUnavailable("no caption tracks")

        fetched = track.fetch()
        text = " ".join(snippet.text for snippet in fetched)
        return Transcript(
            text=clean_transcript(text),
            language=(track.language_code or "").split("-")[0].lower() or None,
            is_generated=bool(track.is_generated),
            source="captions",
        )


def _parse_json3(payload: str) -> str:
    data = json.loads(payload)
    out = []
    for event in data.get("events", []):
        for seg in event.get("segs", []) or []:
            out.append(seg.get("utf8", ""))
    return "".join(out)


def _parse_vtt(payload: str) -> str:
    lines = []
    for raw in payload.splitlines():
        line = raw.strip()
        if not line or "-->" in line or line.startswith(("WEBVTT", "Kind:", "Language:")):
            continue
        if line.isdigit():
            continue
        lines.append(re.sub(r"<[^>]+>", "", line))
    # Auto-captions repeat the previous line as a rolling window; drop repeats.
    deduped = []
    for line in lines:
        if not deduped or deduped[-1] != line:
            deduped.append(line)
    return " ".join(deduped)


_MAX_CAPTION_TRIES = 6


def _caption_candidates(
    info: dict, manual: dict, auto: dict, preferred_language: str | None = None,
) -> list[str]:
    """The few caption codes worth fetching, spoken language first.

    yt-dlp reports the video's own language, and marks the original ASR track
    with an "-orig" suffix. Either is reliable. The first key of the
    automatic_captions dict is NOT - that is YouTube's translation menu in
    alphabetical order, so it is always "ab".
    """
    spoken = (info.get("language") or "").split("-")[0].lower() or None
    if not spoken:
        orig = next((c for c in auto if c.endswith("-orig")), None)
        if orig:
            spoken = orig.split("-")[0].lower()

    order: list[str] = []

    def add(*codes: str) -> None:
        for code in codes:
            if code and code not in order and (code in manual or code in auto):
                order.append(code)

    preferred = (preferred_language or "").split("-")[0].lower()
    if preferred:
        add(f"{preferred}-orig")
        add(*[c for c in manual if c.split("-")[0].lower() == preferred])
        add(preferred)

    if spoken:
        add(f"{spoken}-orig")
        add(*[c for c in manual if c.split("-")[0].lower() == spoken])
        add(spoken)
    add(*list(manual))
    add(*[c for c in auto if c.endswith("-orig")])
    add("en")
    return order[:_MAX_CAPTION_TRIES]


def _fetch_via_ytdlp(video_id: str, preferred_language: str | None = None) -> Transcript:
    import yt_dlp

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "socket_timeout": settings.YOUTUBE_REQUEST_TIMEOUT_SECONDS,
    }
    if settings.YOUTUBE_PROXY:
        opts["proxy"] = settings.YOUTUBE_PROXY

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(watch_url(video_id), download=False)

    # Ye pehle se aa chuka hai - iske liye koi alag request nahi jaati.
    availability = info.get("availability")
    manual = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}
    order = _caption_candidates(info, manual, auto, preferred_language)
    logger.info("yt-dlp caption order for %s: %s", video_id, order)

    throttled = False

    with httpx.Client(
        timeout=settings.YOUTUBE_REQUEST_TIMEOUT_SECONDS,
        proxy=settings.YOUTUBE_PROXY or None,
    ) as client:
        for code in order:
            formats = manual.get(code) or auto.get(code) or []
            chosen = next(
                (f for f in formats if f.get("ext") == "json3"),
                next((f for f in formats if f.get("ext") in ("vtt", "srv1")), None),
            )
            if not chosen or not chosen.get("url"):
                continue

            body = None
            for attempt in range(1):
                try:
                    res = client.get(chosen["url"])
                except Exception as exc:
                    logger.info("yt-dlp subtitle request failed (%s): %s", code, exc)
                    break
                if res.status_code == 429:
                    throttled = True
                    logger.info("yt-dlp subtitle 429 (%s)", code)
                    break
                if res.status_code != 200:
                    logger.info("yt-dlp subtitle HTTP %s (%s)", res.status_code, code)
                    break
                body = res.text
                break

            if not body:
                # Every caption URL uses the same YouTube host and client IP.
                # Retrying other languages after a 429 only repeats the block.
                if throttled:
                    break
                continue

            try:
                text = _parse_json3(body) if chosen["ext"] == "json3" else _parse_vtt(body)
            except Exception as exc:
                logger.info("yt-dlp subtitle parse failed (%s): %s", code, exc)
                continue

            text = clean_transcript(text)
            if len(text) > 40:
                return Transcript(
                    text=text,
                    language=code.split("-")[0].lower(),
                    is_generated=code in auto and code not in manual,
                    source="yt-dlp",
                    availability=availability,
                )

    if throttled:
        raise TranscriptUnavailable(
            "YouTube is rate-limiting this server (HTTP 429). "
            "Wait a few minutes and try again, or set YOUTUBE_PROXY."
        )
    raise TranscriptUnavailable("yt-dlp found no usable captions")


# Jinke liye YouTube khud login maangta hai. Inki summary saanjhi nahi hoti.
_PRIVATE_AVAILABILITY = frozenset(
    {"private", "premium_only", "subscriber_only", "needs_auth"}
)


def is_cacheable(transcript: Transcript, *, video_public: bool | None = None) -> bool:
    """Kya is summary ko sab ke liye rakha ja sakta hai?

    Asli sawaal ye nahi ki video public hai ya unlisted - asli sawaal ye hai ki
    transcript AAYA KAHAN SE.

    Hamara server bina kisi login ke captions maangta hai. Wo tabhi milte hain
    jab video gumnaam pahunch me ho: private aur members-only videos ka
    transcript server ko milta hi nahi. To "server ne bina login ke utha liya"
    apne aap me saboot hai ki video gumnaam pahunch me hai.

    Extension alag baat hai. Wo user ke apne cookies ke saath chalta hai,
    isliye wo ek private video ka transcript bhi bhej sakta hai. Wo kabhi
    saanjha nahi hota.
    """
    if (transcript.availability or "") in _PRIVATE_AVAILABILITY:
        return False

    if transcript.source == "client":
        # The extension reads with the user's own cookies, so on its own a
        # client transcript proves nothing about who else may see the video.
        #
        # oEmbed does prove it, and we already have the answer: YouTube
        # answered for this video with no login at all. That is the same test
        # the server-fetched path passes implicitly, so the same conclusion is
        # available - and without it the cache never fills for extension users,
        # who then pay a full GPU generation for a video already summarised a
        # hundred times.
        if not settings.CACHE_CLIENT_TRANSCRIPTS:
            return False
        return video_public is True

    return True


_transcript_cache: OrderedDict[tuple[str, str], tuple[float, Transcript]] = OrderedDict()
_transcript_cache_guard = threading.Lock()
# A fixed set avoids one permanent lock object per video while still ensuring
# that the same video can only be fetched once at a time.
_transcript_fetch_locks = tuple(threading.Lock() for _ in range(64))


def clear_transcript_cache() -> None:
    """Clear the small in-process cache (also useful for tests)."""
    with _transcript_cache_guard:
        _transcript_cache.clear()


def _transcript_cache_key(video_id: str, preferred_language: str | None) -> tuple[str, str]:
    return video_id, (preferred_language or "").split("-")[0].lower()


def _cached_transcript(video_id: str, preferred_language: str | None = None) -> Transcript | None:
    ttl = settings.TRANSCRIPT_CACHE_TTL_SECONDS
    if ttl <= 0:
        return None
    now = time.monotonic()
    with _transcript_cache_guard:
        key = _transcript_cache_key(video_id, preferred_language)
        cached = _transcript_cache.get(key)
        if cached is None:
            return None
        stored_at, transcript = cached
        if now - stored_at >= ttl:
            _transcript_cache.pop(key, None)
            return None
        _transcript_cache.move_to_end(key)
        return transcript


def _cache_transcript(
    video_id: str, transcript: Transcript, preferred_language: str | None = None,
) -> None:
    if settings.TRANSCRIPT_CACHE_TTL_SECONDS <= 0:
        return
    with _transcript_cache_guard:
        key = _transcript_cache_key(video_id, preferred_language)
        _transcript_cache[key] = (time.monotonic(), transcript)
        _transcript_cache.move_to_end(key)
        limit = max(1, settings.TRANSCRIPT_CACHE_MAX_ENTRIES)
        while len(_transcript_cache) > limit:
            _transcript_cache.popitem(last=False)


def _fetch_lock(video_id: str) -> threading.Lock:
    return _transcript_fetch_locks[hash(video_id) % len(_transcript_fetch_locks)]


def fetch_transcript(video_id: str, preferred_language: str | None = None) -> Transcript:
    """Blocking - call it from a worker thread.

    Successful transcripts are cached briefly, and concurrent requests for the
    same video share one fetch instead of stampeding YouTube.
    """
    cached = _cached_transcript(video_id, preferred_language)
    if cached is not None:
        logger.info("transcript cache hit for %s", video_id)
        return cached

    lock = _fetch_lock(video_id)
    with lock:
        cached = _cached_transcript(video_id, preferred_language)
        if cached is not None:
            return cached
        return _fetch_transcript_uncached(video_id, preferred_language)


def _fetch_transcript_uncached(
    video_id: str, preferred_language: str | None = None,
) -> Transcript:
    """The two sources, hedged rather than queued behind one another.

    These used to run strictly in order: the captions API, and yt-dlp only once
    it had given up. A captions request that times out therefore costs
    YOUTUBE_REQUEST_TIMEOUT_SECONDS before the alternative is even attempted -
    measured on a real request, 19.1s to a transcript yt-dlp produced in about
    7 on its own.

    So yt-dlp is started alongside after TRANSCRIPT_HEDGE_SECONDS and the first
    usable answer wins. The common case is untouched: captions normally answer
    in a second or two, well inside the hedge, and YouTube sees one request.
    """
    errors: list[str] = []

    def usable(name: str, transcript: Transcript) -> Transcript | None:
        if len(transcript.text) > 40:
            logger.info(
                "transcript for %s via %s (%s chars, lang=%s)",
                video_id, name, len(transcript.text), transcript.language,
            )
            _cache_transcript(video_id, transcript, preferred_language)
            return transcript
        errors.append(f"{name}: too short")
        return None

    # NOT a `with` block: ThreadPoolExecutor.__exit__ calls shutdown(wait=True),
    # so winning the race would still block until the losing fetch finished -
    # which is the opposite of the point. Measured with the `with` in place:
    # captions answered in 3s and the request still took 12.6s, waiting on the
    # yt-dlp call nobody needed. The pool is released without waiting instead,
    # and the loser's thread simply finishes into a discarded result.
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="transcript")
    try:
        api_args = (video_id, preferred_language) if preferred_language else (video_id,)
        ytdlp_args = (video_id, preferred_language) if preferred_language else (video_id,)
        futures: dict = {pool.submit(_fetch_via_api, *api_args): "captions"}
        hedge = max(0.0, settings.TRANSCRIPT_HEDGE_SECONDS)
        deadline = time.monotonic() + hedge
        hedged = False

        while futures:
            timeout = None if hedged else max(0.0, deadline - time.monotonic())
            done, _pending = wait(
                list(futures), timeout=timeout, return_when=FIRST_COMPLETED
            )
            if not done and not hedged:
                hedged = True
                futures[pool.submit(_fetch_via_ytdlp, *ytdlp_args)] = "yt-dlp"
                continue

            for fut in done:
                name = futures.pop(fut)
                try:
                    got = usable(name, fut.result())
                except Exception as exc:
                    errors.append(f"{name}: {type(exc).__name__}: {exc}".strip()[:200])
                    logger.info("transcript %s failed for %s: %s", name, video_id, exc)
                    continue
                if got is not None:
                    return got

            if not futures and not hedged:
                hedged = True
                futures[pool.submit(_fetch_via_ytdlp, *ytdlp_args)] = "yt-dlp"
    finally:
        # Do not wait: a losing fetch is abandoned, not awaited.
        pool.shutdown(wait=False, cancel_futures=True)

    raise TranscriptUnavailable(
        "This video has no usable subtitles, or YouTube is blocking this server. "
        + " | ".join(errors)
    )


# ---------------------------------------------------------------------------
def clean_transcript(text: str) -> str:
    """Strip caption noise like [Music] / [संगीत] / [Applause] and tidy spacing."""
    if not text:
        return ""
    text = re.sub(r"\[[^\]\n]{0,40}\]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def sample_for_model(text: str, max_chars: int) -> str:
    """Keep a long transcript within budget without losing the middle/end.

    A few large contiguous blocks, not many tiny fragments - scattered snippets
    break the narrative and the model starts confusing people and events.
    """
    if not text or len(text) <= max_chars:
        return text
    parts = 4
    slice_len = max_chars // parts
    step = len(text) // parts
    return "\n…\n".join(text[i * step : i * step + slice_len].strip() for i in range(parts))
