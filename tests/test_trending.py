import asyncio

import httpx

from app.routers import trending as trending_router
from app.services import trending as trending_service
from app.services.trending import TrendingPage, TrendingVideo


def test_official_regional_chart_filters_to_podcasts(monkeypatch):
    requests = []

    async def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "nextPageToken": "NEXT_PAGE_TOKEN",
                "items": [{
                    "id": "not-a-podcast",
                    "snippet": {
                        "title": "A regionally trending movie trailer",
                        "channelTitle": "Trending channel",
                        "publishedAt": "2026-10-20T12:00:00Z",
                        "thumbnails": {"high": {"url": "https://example.test/thumb.jpg"}},
                    },
                    "statistics": {"viewCount": "456789"},
                }, {
                    "id": "dQw4w9WgXcQ",
                    "snippet": {
                        "title": "A regionally trending interview",
                        "description": "A long-form conversation with the guest.",
                        "channelTitle": "Trending channel",
                        "publishedAt": "2026-10-20T12:00:00Z",
                        "thumbnails": {"high": {"url": "https://example.test/podcast.jpg"}},
                    },
                    "statistics": {"viewCount": "456789"},
                }],
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(trending_service, "_http_client", lambda: client)
    monkeypatch.setattr(trending_service.settings, "YOUTUBE_DATA_API_KEY", "test-key")
    trending_service._cache.clear()
    try:
        page = asyncio.run(trending_service.fetch("US", page_token="FIRST_TOKEN"))
    finally:
        asyncio.run(client.aclose())
        trending_service._cache.clear()

    assert len(page.videos) == 1
    assert page.videos[0].video_id == "dQw4w9WgXcQ"
    assert page.videos[0].views == 456789
    assert page.next_page_token == "NEXT_PAGE_TOKEN"
    params = dict(requests[0].url.params)
    assert requests[0].url.path.endswith("/videos")
    assert params["chart"] == "mostPopular"
    assert params["regionCode"] == "US"
    assert params["pageToken"] == "FIRST_TOKEN"
    assert params["maxResults"] == "50"
    assert "q" not in params
    assert "publishedAfter" not in params


def test_podcast_detection_accepts_common_podcast_indicators():
    assert trending_service._is_podcast({"title": "Creator interview: episode 12"})
    assert trending_service._is_podcast({"channelTitle": "Hindi \u092c\u093e\u0924\u091a\u0940\u0924"})
    assert trending_service._is_podcast({"description": "Un nuevo episodio de conversaci\u00f3n"})
    assert trending_service._is_podcast({"channelTitle": "BeerBiceps"})
    assert trending_service._is_podcast({"channelTitle": "Lex Fridman"})
    assert not trending_service._is_podcast({"title": "Official movie trailer"})


def test_trending_detects_cloudflare_country(client, monkeypatch):
    seen = {}

    async def fake_fetch(region, limit, page_token=None):
        seen.update(region=region, limit=limit, page_token=page_token)
        return TrendingPage(videos=(
            TrendingVideo(
                video_id="dQw4w9WgXcQ", title="Video", channel="Channel",
                thumbnail="", views=123456, published_at="2026-10-20T12:00:00Z",
                url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            ),
        ), next_page_token="NEXT_TOKEN")

    monkeypatch.setattr(trending_router.trending, "fetch", fake_fetch)
    response = client.get("/api/v1/trending", headers={"CF-IPCountry": "US"})

    assert response.status_code == 200, response.text
    assert seen == {"region": "US", "limit": 10, "page_token": None}
    assert response.json()["source"] == "detected"
    assert response.json()["next_page_token"] == "NEXT_TOKEN"


def test_trending_manual_country_and_next_page(client, monkeypatch):
    seen = {}

    async def fake_fetch(region, limit, page_token=None):
        seen.update(region=region, limit=limit, page_token=page_token)
        return TrendingPage(videos=(
            TrendingVideo(
                video_id="dQw4w9WgXcQ", title="Video", channel="Channel",
                thumbnail="", views=None, published_at=None,
                url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            ),
        ), next_page_token=None)

    monkeypatch.setattr(trending_router.trending, "fetch", fake_fetch)
    response = client.get("/api/v1/trending?region=JP&page_token=NEXT_TOKEN")

    assert response.status_code == 200
    assert response.json()["region"] == "JP"
    assert response.json()["source"] == "selected"
    assert seen == {"region": "JP", "limit": 10, "page_token": "NEXT_TOKEN"}

    invalid = client.get("/api/v1/trending?region=IND")
    assert invalid.status_code == 422
