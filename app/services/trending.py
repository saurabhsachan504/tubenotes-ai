"""Country-specific, top-watched podcast lookup.

Each country uses a query based on its curated podcast directory.  YouTube
searches are ordered by total video views, then each result is enriched with
its current view count.  This is intentionally different from YouTube's
short-lived ``mostPopular`` (Trending) chart.  The API key is server-only.
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
_PODCAST_RE = re.compile(r"\b(?:pod[\s-]*casts?|vodcasts?)\b", re.IGNORECASE)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
# Trusted channel-name markers curated from the product's podcast catalogue.
# They only classify candidates returned by YouTube's selected-country search.
# They never change the selected country or the API's view-count ordering.
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
    # USA
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
    # Canada
    "full send", "very really good", "kurtis conner", "tmg podcast", "wan show",
    "linus tech tips", "sidenote", "asapscience", "area52", "chris ramsay",
    "jeff nippard", "steve dangle",
    # Australia
    "hamish and andy", "inspired unemployed", "misfits podcast", "howie games",
    "betoota advocate", "cold ones", "alpha blokes", "grade cricketer", "hello sport",
    "bloke in a bar", "the imperfects", "shameless", "mark bouris", "casefile",
    "abc conversations",
    # Germany
    "hotel matze", "jung und naiv", "leeroy will s wissen", "die deutschen",
    "baywatch berlin", "hobbylos", "omr podcast", "finanzfluss", "kack sachgeschichten",
    # France
    "zack en roue libre", "zacknani", "popcorn", "legend", "un bon moment",
    "hugodecrypte", "fouloscopie", "squeezie", "canape six places", "mcfly et carlito",
    "mcskyz", "affaires sensibles",
    # Brazil
    "flow podcast", "inteligencia ltda", "poddelas", "nerdcast", "ticaracaticast",
    "beto ribeiro", "quinta misteriosa", "nao inviabilize", "mamilos", "tv 247",
    "jota jota podcast",
    # United Arab Emirates
    "abtalks", "anas bukhash", "hikmat wehbi", "dukkan show", "kris fade",
    "dubai works", "lovin dubai", "with loulou", "dubai stars", "taking care of business",
    "helen farmer",
    # Indonesia
    "deddy corbuzier", "close the door", "curhat bang", "denny sumargo", "vindes",
    "akbar faizal", "pwk", "has creative", "podkesmas", "podcast seminggu",
    "lentera malam", "rjl 5", "fajar aditya", "nadia omara", "endgame",
    "gita wirjawan", "raymond chin",
    # Mexico
    "la cotorrisa", "hermanos de leche", "la pension", "las alucines", "envinadas",
    "relatos de la noche", "leyendas legendarias", "extra anormal",
    "hablemos de lo que no existe", "penitencia", "yordi rosado", "creativo",
    "robertomtz", "gusgri", "farid dieck", "cracks podcast", "oso trava",
    "dimes y billetes",
    # Other global/niche shows
    "trevor noah", "mo gawdat", "iced coffee hour", "graham stephan", "kinda funny",
    "ign unfiltered", "dropped frames", "the yard", "new heights", "pat mcafee",
    "club shay shay", "games with names", "crime junkie", "my favorite murder",
    "rotten mango", "stephanie soo",
})
_SEARCH_API_URL = "https://www.googleapis.com/youtube/v3/search"
_VIDEOS_API_URL = "https://www.googleapis.com/youtube/v3/videos"
_COUNTRY_PODCAST_QUERIES = {
    "IN": "podcast|raj shamani|beerbiceps|ranveer allahbadia|nikhil kamath|lallantop|sandeep maheshwari",
    "US": "podcast|joe rogan|lex fridman|huberman lab|tim ferriss|call her daddy|my first million",
    "GB": "podcast|diary of a ceo|steven bartlett|modern wisdom|rest is history|shxtsngigs|the fellas",
    "CA": "podcast|full send|tmg podcast|wan show|jeff nippard|kurtis conner|steve dangle",
    "AU": "podcast|cold ones|misfits|hamish and andy|howie games|alpha blokes|casefile",
    "DE": "podcast|hotel matze|jung und naiv|omr podcast|hobbylos|baywatch berlin|finanzfluss",
    "FR": "podcast|zack nani|popcorn|legend|hugo decrypte|mcfly carlito|affaires sensibles",
    "BR": "podcast|flow podcast|inteligencia ltda|poddelas|nerdcast|ticaracaticast|mamilos",
    "AE": "podcast|abtalks|anas bukhash|hikmat wehbi|dukkan show|kris fade|dubai works",
    "ID": "podcast|deddy corbuzier|close the door|vindes|podkesmas|gita wirjawan|raymond chin",
    "MX": "podcast|la cotorrisa|leyendas legendarias|creativo|gusgri|farid dieck|cracks podcast",
}
_RELEVANCE_LANGUAGES = {
    "DE": "de", "FR": "fr", "BR": "pt", "AE": "ar", "ID": "id", "MX": "es",
}
_cache: dict[tuple[str, int, str], tuple[float, "TrendingPage"]] = {}
_client: httpx.AsyncClient | None = None


class TrendingUnavailable(Exception):
    """The upstream top-podcast lookup cannot currently be shown."""


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

    YouTube's public search API has no official podcast-only filter,
    so this requires an explicit ``podcast``/``vodcast`` metadata marker or a
    trusted channel-name marker after obtaining the country search results.
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
    """Return top-watched podcast videos for one selected country."""
    region = normalise_region(region)
    if not region:
        raise TrendingUnavailable("Choose a valid two-letter country code.")
    api_key = settings.YOUTUBE_DATA_API_KEY.strip()
    if not api_key:
        raise TrendingUnavailable("Trending podcasts are not configured yet.")

    limit = max(1, min(int(limit), 50))
    # Search up to 50 candidates, then keep the first requested podcast videos.
    candidate_limit = min(50, max(25, limit * 5))
    page_token = (page_token or "").strip()
    key = (region, limit, page_token)
    now = time.monotonic()
    cached = _cache.get(key)
    if cached and now - cached[0] < settings.YOUTUBE_TRENDING_CACHE_SECONDS:
        return cached[1]

    try:
        search_response = await _http_client().get(
            _SEARCH_API_URL,
            params={
                "part": "snippet",
                "q": _COUNTRY_PODCAST_QUERIES.get(region, "podcast"),
                "regionCode": region,
                "relevanceLanguage": _RELEVANCE_LANGUAGES.get(region),
                "type": "video",
                "order": "viewCount",
                "videoDuration": "long",
                "maxResults": candidate_limit,
                "pageToken": page_token or None,
                "key": api_key,
                "fields": "nextPageToken,items(id/videoId,snippet(title,description,channelTitle,publishedAt,thumbnails))",
            },
        )
        search_response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        logger.warning("YouTube top-podcast search returned HTTP %s", exc.response.status_code)
        raise TrendingUnavailable("Top podcasts are temporarily unavailable.") from exc
    except httpx.HTTPError as exc:
        logger.warning("YouTube top-podcast search failed: %s", exc.__class__.__name__)
        raise TrendingUnavailable("Top podcasts are temporarily unavailable.") from exc

    payload = search_response.json()
    search_items = payload.get("items", [])
    video_ids = [
        item.get("id", {}).get("videoId")
        for item in search_items
        if isinstance(item, dict) and isinstance(item.get("id"), dict) and isinstance(item["id"].get("videoId"), str)
    ]
    if not video_ids:
        raise TrendingUnavailable("No top podcasts are available for this country.")

    try:
        details_response = await _http_client().get(
            _VIDEOS_API_URL,
            params={
                "part": "snippet,statistics",
                "id": ",".join(video_ids),
                "key": api_key,
                "fields": "items(id,snippet(title,description,tags,channelTitle,publishedAt,thumbnails),statistics(viewCount))",
            },
        )
        details_response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        logger.warning("YouTube top-podcast details returned HTTP %s", exc.response.status_code)
        raise TrendingUnavailable("Top podcasts are temporarily unavailable.") from exc
    except httpx.HTTPError as exc:
        logger.warning("YouTube top-podcast details request failed: %s", exc.__class__.__name__)
        raise TrendingUnavailable("Top podcasts are temporarily unavailable.") from exc

    details_by_id = {
        item.get("id"): item
        for item in details_response.json().get("items", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    videos: list[TrendingVideo] = []
    for video_id in video_ids:
        item = details_by_id.get(video_id) or {}
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
        raise TrendingUnavailable("No top podcasts are available for this country.")
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
