"""Server-side country-aware popular podcast lookup.

The YouTube Data API key is deliberately used only here.  The browser receives
video metadata and canonical watch URLs, never the credential itself.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from app.config import settings

logger = logging.getLogger("trialguard.trending")

_REGION_RE = re.compile(r"^[A-Z]{2}$")
_SEARCH_API_URL = "https://www.googleapis.com/youtube/v3/search"
_VIDEOS_API_URL = "https://www.googleapis.com/youtube/v3/videos"
_cache: dict[tuple[str, int, str], tuple[float, "TrendingPage"]] = {}
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
    published_at: str | None
    url: str


@dataclass(frozen=True, slots=True)
class TrendingPage:
    videos: tuple[TrendingVideo, ...]
    next_page_token: str | None


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


def _published_after() -> str:
    """Use a day-stable cutoff so YouTube's next-page token stays valid."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=settings.YOUTUBE_TRENDING_WINDOW_DAYS)).date()
    return f"{cutoff.isoformat()}T00:00:00Z"


async def fetch(
    region: str,
    limit: int = 10,
    page_token: str | None = None,
) -> TrendingPage:
    """Fetch recent, highest-viewed podcast videos available in one country.

    YouTube does not expose a separate podcast ``mostPopular`` chart.  Its
    ``search.list`` endpoint can, however, restrict videos by country and sort
    matching podcasts by view count.  A recent time window prevents a list of
    all-time classics from being presented as current trending podcasts.
    """
    region = normalise_region(region)
    if not region:
        raise TrendingUnavailable("Choose a valid two-letter country code.")
    api_key = settings.YOUTUBE_DATA_API_KEY.strip()
    if not api_key:
        raise TrendingUnavailable("Trending videos are not configured yet.")

    limit = max(1, min(int(limit), 50))
    page_token = (page_token or "").strip()
    key = (region, limit, page_token)
    now = time.monotonic()
    cached = _cache.get(key)
    if cached and now - cached[0] < settings.YOUTUBE_TRENDING_CACHE_SECONDS:
        return cached[1]

    try:
        response = await _http_client().get(
            _SEARCH_API_URL,
            params={
                "part": "snippet",
                "q": "podcast",
                "type": "video",
                "order": "viewCount",
                "regionCode": region,
                "videoDuration": "long",
                "publishedAfter": _published_after(),
                "safeSearch": "moderate",
                "maxResults": limit,
                "pageToken": page_token or None,
                "key": api_key,
                "fields": "nextPageToken,items(id/videoId,snippet(title,channelTitle,publishedAt,thumbnails))",
            },
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        # Do not return Google's detailed response because it could reveal
        # project configuration.  The server log retains the status for ops.
        logger.warning("YouTube podcast search API returned HTTP %s", exc.response.status_code)
        raise TrendingUnavailable("Trending podcasts are temporarily unavailable.") from exc
    except httpx.HTTPError as exc:
        logger.warning("YouTube podcast search API request failed: %s", exc.__class__.__name__)
        raise TrendingUnavailable("Trending podcasts are temporarily unavailable.") from exc

    items = response.json().get("items", [])
    video_ids = []
    for item in items:
        identity = item.get("id") or {}
        video_id = identity.get("videoId") if isinstance(identity, dict) else None
        if isinstance(video_id, str):
            video_ids.append(video_id)
    views_by_id: dict[str, int] = {}
    if video_ids:
        # search.list does not return statistics. This inexpensive second call
        # lets the cards show the actual count without changing their ranking.
        try:
            stats_response = await _http_client().get(
                _VIDEOS_API_URL,
                params={
                    "part": "statistics",
                    "id": ",".join(video_ids),
                    "key": api_key,
                    "fields": "items(id,statistics/viewCount)",
                },
            )
            stats_response.raise_for_status()
            for stat in stats_response.json().get("items", []):
                try:
                    views_by_id[stat["id"]] = int(stat.get("statistics", {}).get("viewCount"))
                except (KeyError, TypeError, ValueError):
                    continue
        except httpx.HTTPError as exc:
            logger.info("YouTube podcast statistics unavailable: %s", exc.__class__.__name__)

    videos: list[TrendingVideo] = []
    for item in items:
        video_id = (item.get("id") or {}).get("videoId")
        snippet = item.get("snippet") or {}
        title = snippet.get("title")
        if not isinstance(video_id, str) or not isinstance(title, str):
            continue
        videos.append(
            TrendingVideo(
                video_id=video_id,
                title=title,
                channel=str(snippet.get("channelTitle") or "YouTube"),
                thumbnail=_thumbnail(snippet),
                views=views_by_id.get(video_id),
                published_at=(
                    snippet.get("publishedAt")
                    if isinstance(snippet.get("publishedAt"), str)
                    else None
                ),
                url=f"https://www.youtube.com/watch?v={video_id}",
            )
        )

    if not videos:
        raise TrendingUnavailable("Trending podcasts are unavailable for this country.")
    result = TrendingPage(
        videos=tuple(videos),
        next_page_token=(
            response.json().get("nextPageToken")
            if isinstance(response.json().get("nextPageToken"), str)
            else None
        ),
    )
    _cache[key] = (now, result)
    return result


async def close_client() -> None:
    """Close the shared HTTP connection pool during application shutdown."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
