"""Country-specific, podcast-only YouTube Trending chart lookup.

Candidates come from YouTube's official
``videos.list(chart=mostPopular, regionCode=...)`` endpoint.  We preserve that
country's chart order and retain videos with public podcast-style metadata.
This means a podcast from any creator country can appear if it is trending for
the selected audience country.  The API key is used only on the server and
never sent to the browser.
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
_PODCAST_RE = re.compile(
    r"(?:"
    r"\bpod[\s-]*casts?\b|\bvodcasts?\b|\btalk\s*show\b|"
    r"\binterviews?\b|\bconversations?\b|\bround\s*table\b|"
    r"\bpanel\s+discussion\b|\bfireside\s+chat\b|"
    r"\b(?:talk|podcast)\s+episode\b|\bep(?:isode)?\.?\s*\d+\b|"
    r"\btalks\b|बातचीत|इंटरव्यू|साक्षात्कार|चर्चा|संवाद|"
    r"ポッドキャスト|インタビュー|entrevistas?|conversaci[óo]n|charlas?|bate-papo"
    r")",
    re.IGNORECASE,
)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
# Trusted channel-name markers curated from the product's podcast catalogue.
# They are only used to classify videos that already appear in YouTube's
# selected regional ``mostPopular`` chart; they never alter the chart country
# or its ordering.
_KNOWN_PODCAST_CHANNEL_MARKERS = frozenset({
    # India
    "beerbiceps", "the ranveer show", "raj shamani", "figuring out", "wtf is",
    "nikhil kamath", "ankur warikoo", "shwetabh gangwar", "mensutra",
    "prakhar ke pravachan", "dostcast", "vinamre kasanaa", "the indian dream",
    "the neon show", "suhani shah", "ani podcast", "smita prakash",
    "the lallantop", "unfiltered by samdish", "the seen and the unseen",
    "amit varma", "realhit", "neelesh misra", "the slow interview",
    "abhi and niyu", "keerthi history", "the internet said so", "cyrus says",
    "untriggered", "aminjaz", "chalchitra talks", "men of culture",
    "tanmay bhat", "samay raina", "aakash gupta", "rohan joshi",
    "khooni monday", "desi crime", "bhootiyagiri", "indian murder mystery",
    "sandeep maheshwari", "gaur gopal das", "sadhguru", "jay shetty",
    # USA and Canada
    "lex fridman", "huberman lab", "waveform", "mkbhd", "wan show",
    "linus tech tips", "startalk", "hard fork", "darknet diaries",
    "sean carroll", "mindscape", "tim ferriss", "garyvee", "gary vaynerchuk",
    "colin and samir", "my first million", "shaan puri", "sam parr",
    "the ramsey show", "dave ramsey", "how i built this", "guy raz",
    "smart passive income", "pat flynn", "entrepreneurs on fire", "john lee dumas",
    "biggerpockets", "powerfuljre", "joe rogan", "impaulsive", "h3 podcast",
    "call her daddy", "anything goes", "emma chamberlain", "flagrant",
    "andrew schulz", "this past weekend", "theo von", "2 bears 1 cave",
    "tigerbelly", "monday morning podcast", "bill burr", "conan obrien",
    "armchair expert", "dax shepard", "wtf with marc maron", "trash taste",
    "jordan b peterson", "jordan peterson", "making sense", "sam harris",
    "ben shapiro", "valuetainment", "patrick bet david", "pbd podcast",
    "megyn kelly", "triggernometry", "rich roll", "peter attia", "david sinclair",
    "on purpose", "mind pump",
    # United Kingdom and Europe
    "diary of a ceo", "steven bartlett", "modern wisdom", "chris williamson",
    "ali abdaal", "high performance", "grace beverley", "ricky gervais",
    "sidecast", "sidemen", "fellas podcast", "jaackmaate", "shxtsngigs",
    "help i sexted my boss", "rest is entertainment", "rest is history",
    "rest is politics", "russell brand",
    # Australia and global/niche shows
    "hamish and andy", "inspired unemployed", "misfits podcast", "howie games",
    "betoota advocate", "trevor noah", "mo gawdat", "iced coffee hour",
    "graham stephan", "kinda funny", "ign unfiltered", "dropped frames",
    "the yard", "new heights", "pat mcafee", "club shay shay", "games with names",
    "crime junkie", "my favorite murder", "rotten mango", "stephanie soo",
})
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


def _normalise_channel_name(value: str) -> str:
    return f" {_NON_ALNUM_RE.sub(' ', value.casefold()).strip()} "


def _is_known_podcast_channel(channel_name: object) -> bool:
    if not isinstance(channel_name, str):
        return False
    normalised = _normalise_channel_name(channel_name)
    return any(f" {marker} " in normalised for marker in _KNOWN_PODCAST_CHANNEL_MARKERS)


def _is_podcast(snippet: dict) -> bool:
    """Identify podcast-style videos using public YouTube metadata.

    YouTube's regional Trending endpoint has no official podcast-only filter,
    so this uses practical podcast indicators and trusted channel-name markers
    in public metadata after obtaining the official regional chart.
    """
    parts = [
        snippet.get("title"),
        snippet.get("description"),
        snippet.get("channelTitle"),
    ]
    tags = snippet.get("tags") or []
    if isinstance(tags, list):
        parts.extend(tag for tag in tags if isinstance(tag, str))
    metadata = " ".join(part for part in parts if isinstance(part, str))
    return bool(_PODCAST_RE.search(metadata)) or _is_known_podcast_channel(snippet.get("channelTitle"))


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
