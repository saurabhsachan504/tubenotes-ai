"""Server-side YouTube most-popular video lookup.

The YouTube Data API key is deliberately used only here.  The browser receives
video metadata and canonical watch URLs, never the credential itself.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

import httpx

from app.config import settings

logger = logging.getLogger("trialguard.trending")

_REGION_RE = re.compile(r"^[A-Z]{2}$")
_API_URL = "https://www.googleapis.com/youtube/v3/videos"
_cache: dict[tuple[str, int], tuple[float, tuple["TrendingVideo", ...]]] = {}
_client: httpx.AsyncClient | None = None


class TrendingUnavailable(Exception):
    """The upstream trending chart cannot currently be shown."""


@dataclass(frozen=True, slots=True)
class TrendingVideo:
    video_id: str
    title: str
    channel: str
    thumbnail: str
    views: int | None
    url: str


def normalise_region(value: str | None) -> str | None:
    """Return an ISO-3166 alpha-2 code or ``None`` for an invalid value."""
    code = (value or "").strip().upper()
    return code if _REGION_RE.fullmatch(code) else None


def _http_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=settings.YOUTUBE_REQUEST_TIMEOUT_SECONDS)
    return _client


def _thumbnail(snippet: dict) -> str:
    thumbnails = snippet.get("thumbnails") or {}
    for size in ("maxres", "standard", "high", "medium", "default"):
        url = (thumbnails.get(size) or {}).get("url")
        if isinstance(url, str) and url.startswith("https://"):
            return url
    return ""


async def fetch(region: str, limit: int = 10) -> tuple[TrendingVideo, ...]:
    """Fetch and briefly cache the most-popular chart for one country."""
    region = normalise_region(region)
    if not region:
        raise TrendingUnavailable("Choose a valid two-letter country code.")
    api_key = settings.YOUTUBE_DATA_API_KEY.strip()
    if not api_key:
        raise TrendingUnavailable("Trending videos are not configured yet.")

    limit = max(1, min(int(limit), 50))
    key = (region, limit)
    now = time.monotonic()
    cached = _cache.get(key)
    if cached and now - cached[0] < settings.YOUTUBE_TRENDING_CACHE_SECONDS:
        return cached[1]

    try:
        response = await _http_client().get(
            _API_URL,
            params={
                "part": "snippet,statistics",
                "chart": "mostPopular",
                "regionCode": region,
                "maxResults": limit,
                "key": api_key,
                "fields": "items(id,snippet(title,channelTitle,thumbnails),statistics(viewCount))",
            },
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        # Do not return Google's detailed response because it could reveal
        # project configuration.  The server log retains the status for ops.
        logger.warning("YouTube trending API returned HTTP %s", exc.response.status_code)
        raise TrendingUnavailable("Trending videos are temporarily unavailable.") from exc
    except httpx.HTTPError as exc:
        logger.warning("YouTube trending API request failed: %s", exc.__class__.__name__)
        raise TrendingUnavailable("Trending videos are temporarily unavailable.") from exc

    videos: list[TrendingVideo] = []
    for item in response.json().get("items", []):
        video_id = item.get("id")
        snippet = item.get("snippet") or {}
        title = snippet.get("title")
        if not isinstance(video_id, str) or not isinstance(title, str):
            continue
        views_raw = (item.get("statistics") or {}).get("viewCount")
        try:
            views = int(views_raw) if views_raw is not None else None
        except (TypeError, ValueError):
            views = None
        videos.append(
            TrendingVideo(
                video_id=video_id,
                title=title,
                channel=str(snippet.get("channelTitle") or "YouTube"),
                thumbnail=_thumbnail(snippet),
                views=views,
                url=f"https://www.youtube.com/watch?v={video_id}",
            )
        )

    if not videos:
        raise TrendingUnavailable("Trending videos are unavailable for this country.")
    result = tuple(videos)
    _cache[key] = (now, result)
    return result


async def close_client() -> None:
    """Close the shared HTTP connection pool during application shutdown."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
