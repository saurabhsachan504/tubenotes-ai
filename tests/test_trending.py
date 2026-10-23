from app.routers import trending as trending_router
from app.services.trending import TrendingVideo


def test_trending_uses_trusted_cloudflare_country(client, monkeypatch):
    seen = {}

    async def fake_fetch(region, limit):
        seen.update(region=region, limit=limit)
        return (
            TrendingVideo(
                video_id="dQw4w9WgXcQ",
                title="A popular video",
                channel="TubeNotes test channel",
                thumbnail="https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
                views=123456,
                url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            ),
        )

    monkeypatch.setattr(trending_router.trending, "fetch", fake_fetch)
    response = client.get("/api/v1/trending", headers={"CF-IPCountry": "US"})

    assert response.status_code == 200, response.text
    assert seen == {"region": "US", "limit": 10}
    assert response.json()["source"] == "detected"
    assert response.json()["videos"][0]["url"].endswith("dQw4w9WgXcQ")


def test_trending_region_override_and_validation(client, monkeypatch):
    async def fake_fetch(region, limit):
        return (
            TrendingVideo(
                video_id="dQw4w9WgXcQ", title="Video", channel="Channel",
                thumbnail="", views=None,
                url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            ),
        )

    monkeypatch.setattr(trending_router.trending, "fetch", fake_fetch)
    response = client.get("/api/v1/trending?region=IN")
    assert response.status_code == 200
    assert response.json()["region"] == "IN"
    assert response.json()["source"] == "selected"

    invalid = client.get("/api/v1/trending?region=IND")
    assert invalid.status_code == 422
