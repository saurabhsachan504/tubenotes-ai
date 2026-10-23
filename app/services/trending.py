"""Official country-specific YouTube trending chart lookup.

This uses YouTube's ``videos.list(chart=mostPopular, regionCode=...)`` endpoint
instead of a keyword search. Therefore a selected country matches the same
regional audience chart that YouTube exposes for that country. The API key is
used only on the server and never sent to the browser.
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
_VIDEOS_API_URL = "https://www.googleapis.com/youtube/v3/videos"
_cache: dict[tuple[str, int, str], tuple[float, "TrendingPage"]] = {}
_client: httpx.AsyncClient | None = None


class TrendingUnavailable(Exception):
    """The upstream regional chart cannot currently be shown."""


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


async def fetch(
    region: str,
    limit: int = 10,
    page_token: str | None = None,
) -> TrendingPage:
    """Return one page from YouTube's official ``mostPopular`` regional chart."""
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
            _VIDEOS_API_URL,
            params={
                "part": "snippet,statistics",
                "chart": "mostPopular",
                "regionCode": region,
                "maxResults": limit,
                "pageToken": page_token or None,
                "key": api_key,
                "fields": "nextPageToken,items(id,snippet(title,channelTitle,publishedAt,thumbnails),statistics(viewCount))",
            },
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        logger.warning("YouTube regional trending API returned HTTP %s", exc.response.status_code)
        raise TrendingUnavailable("Trending videos are temporarily unavailable.") from exc
    except httpx.HTTPError as exc:
        logger.warning("YouTube regional trending API request failed: %s", exc.__class__.__name__)
        raise TrendingUnavailable("Trending videos are temporarily unavailable.") from exc

    payload = response.json()
    videos: list[TrendingVideo] = []
    for item in payload.get("items", []):
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
                published_at=(snippet.get("publishedAt") if isinstance(snippet.get("publishedAt"), str) else None),
                url=f"https://www.youtube.com/watch?v={video_id}",
            )
        )

    if not videos:
        raise TrendingUnavailable("No trending chart is available for this country.")
    result = TrendingPage(
        videos=tuple(videos),
        next_page_token=payload.get("nextPageToken") if isinstance(payload.get("nextPageToken"), str) else None,
    )
    _cache[key] = (now, result)
    return result


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
