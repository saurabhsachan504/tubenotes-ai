import asyncio

import httpx

from app.routers import trending as trending_router
from app.services import trending as trending_service
from app.services.trending import TrendingPage, TrendingVideo


def test_podcast_search_is_country_scoped_and_recent(monkeypatch):
    requests = []

    async def handler(request):
        requests.append(request)
        if request.url.path.endswith("/search"):
            return httpx.Response(
                200,
                json={
            "nextPageToken": "NEXT_PAGE_TOKEN",
            "items": [{
                        "id": {"videoId": "dQw4w9WgXcQ"},
                        "snippet": {
                            "title": "Recent podcast", "channelTitle": "Podcast channel",
                            "thumbnails": {"high": {"url": "https://example.test/thumb.jpg"}},
                        },
                    }],
                },
            )
        return httpx.Response(
            200,
            json={"items": [{"id": "dQw4w9WgXcQ", "statistics": {"viewCount": "456789"}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(trending_service, "_http_client", lambda: client)
    monkeypatch.setattr(trending_service.settings, "YOUTUBE_DATA_API_KEY", "test-key")
    monkeypatch.setattr(trending_service.settings, "YOUTUBE_TRENDING_WINDOW_DAYS", 90)
    trending_service._cache.clear()
    try:
        videos = asyncio.run(trending_service.fetch("IN"))
    finally:
        asyncio.run(client.aclose())
        trending_service._cache.clear()

    assert len(videos.videos) == 1
    assert videos.videos[0].views == 456789
    assert videos.next_page_token == "NEXT_PAGE_TOKEN"
    search_params = dict(requests[0].url.params)
    assert search_params["q"] == "podcast"
    assert search_params["regionCode"] == "IN"
    assert search_params["order"] == "viewCount"
    assert search_params["videoDuration"] == "long"
    assert "publishedAfter" in search_params


def test_trending_podcasts_use_trusted_cloudflare_country(client, monkeypatch):
    seen = {}

    async def fake_fetch(region, limit, page_token=None):
        seen.update(region=region, limit=limit, page_token=page_token)
        return TrendingPage(videos=(
            TrendingVideo(
                video_id="dQw4w9WgXcQ",
                title="A popular video",
                channel="TubeNotes test channel",
                thumbnail="https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
                views=123456,
                published_at="2026-10-20T12:00:00Z",
                url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            ),
        ), next_page_token="NEXT_TOKEN")

    monkeypatch.setattr(trending_router.trending, "fetch", fake_fetch)
    response = client.get("/api/v1/trending", headers={"CF-IPCountry": "US"})

    assert response.status_code == 200, response.text
    assert seen == {"region": "US", "limit": 10, "page_token": None}
    assert response.json()["source"] == "detected"
    assert response.json()["next_page_token"] == "NEXT_TOKEN"
    assert response.json()["videos"][0]["published_at"] == "2026-10-20T12:00:00Z"
    assert response.json()["videos"][0]["url"].endswith("dQw4w9WgXcQ")


def test_trending_region_override_and_validation(client, monkeypatch):
    async def fake_fetch(region, limit, page_token=None):
        return TrendingPage(videos=(
            TrendingVideo(
                video_id="dQw4w9WgXcQ", title="Video", channel="Channel",
                thumbnail="", views=None, published_at=None,
                url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            ),
        ), next_page_token=None)

    monkeypatch.setattr(trending_router.trending, "fetch", fake_fetch)
    response = client.get("/api/v1/trending?region=IN")
    assert response.status_code == 200
    assert response.json()["region"] == "IN"
    assert response.json()["source"] == "selected"

    invalid = client.get("/api/v1/trending?region=IND")
    assert invalid.status_code == 422
