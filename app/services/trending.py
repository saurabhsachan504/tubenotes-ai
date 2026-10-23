"""Country-specific, podcast-only YouTube Trending chart lookup.

Candidates come from YouTube's official
``videos.list(chart=mostPopular, regionCode=...)`` endpoint.  We preserve that
country's chart order and retain videos whose public metadata explicitly marks
them as podcasts.  This means a podcast from any creator country can appear if
it is trending for the selected audience country.  The API key is used only on
the server and never sent to the browser.
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
_PODCAST_RE = re.compile(r"\bpod[\s-]*casts?\b", re.IGNORECASE)
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


def _is_podcast(snippet: dict) -> bool:
    """Identify videos explicitly described as podcasts by YouTube metadata.

    YouTube's regional Trending endpoint has no official podcast-only filter,
    so this intentionally uses only public title, description, channel and tag
    metadata after obtaining the official regional chart.
    """
    parts = [
        snippet.get("title"),
        snippet.get("description"),
        snippet.get("channelTitle"),
    ]
    tags = snippet.get("tags") or []
    if isinstance(tags, list):
        parts.extend(tag for tag in tags if isinstance(tag, str))
    return _PODCAST_RE.search(" ".join(part for part in parts if isinstance(part, str))) is not None


async def fetch(
    region: str,
    limit: int = 10,
    page_token: str | None = None,
) -> TrendingPage:
    """Return podcasts from one page of YouTube's official regional chart."""
    region = normalise_region(region)
    if not region:
        raise TrendingUnavailable("Choose a valid two-letter country code.")
    api_key = settings.YOUTUBE_DATA_API_KEY.strip()
    if not api_key:
        raise TrendingUnavailable("Trending podcasts are not configured yet.")

    limit = max(1, min(int(limit), 50))
    # Fetch a wider part of the official chart before filtering it to podcasts.
    # ``videos.list`` permits at most 50 items per request.
    candidate_limit = min(50, max(25, limit * 5))
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
                "maxResults": candidate_limit,
                "pageToken": page_token or None,
                "key": api_key,
                "fields": "nextPageToken,items(id,snippet(title,description,tags,channelTitle,publishedAt,thumbnails),statistics(viewCount))",
            },
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        logger.warning("YouTube regional trending API returned HTTP %s", exc.response.status_code)
        raise TrendingUnavailable("Trending podcasts are temporarily unavailable.") from exc
    except httpx.HTTPError as exc:
        logger.warning("YouTube regional trending API request failed: %s", exc.__class__.__name__)
        raise TrendingUnavailable("Trending podcasts are temporarily unavailable.") from exc

    payload = response.json()
    videos: list[TrendingVideo] = []
    for item in payload.get("items", []):
        video_id = item.get("id")
        snippet = item.get("snippet") or {}
        title = snippet.get("title")
        if not isinstance(video_id, str) or not isinstance(title, str) or not _is_podcast(snippet):
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
        if len(videos) >= limit:
            break

    if not videos:
        raise TrendingUnavailable("No trending podcasts are available for this country.")
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
