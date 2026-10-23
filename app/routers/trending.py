"""Public, country-aware top-podcast endpoint for the TubeNotes home page."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from app.config import settings
from app.schemas import TrendingOut, TrendingVideoOut
from app.services import pricing, trending

router = APIRouter(tags=["trending"])


@router.get("/trending", response_model=TrendingOut)
async def list_trending(
    request: Request,
    region: str | None = Query(default=None, min_length=2, max_length=2),
    limit: int = Query(default=10, ge=5, le=10),
    page_token: str | None = Query(default=None, min_length=1, max_length=256),
    days: int = Query(default=90, ge=60, le=90),
):
    """Return the selected or Cloudflare-detected country's top podcasts."""
    selected = trending.normalise_region(region)
    if region is not None and selected is None:
        raise HTTPException(status_code=422, detail="Choose a valid two-letter country code.")
    if days not in (60, 90):
        raise HTTPException(status_code=422, detail="Choose either the last 60 or 90 days.")

    detected = pricing.country_code_for_headers(request.headers)
    fallback = trending.normalise_region(settings.YOUTUBE_TRENDING_DEFAULT_REGION) or "IN"
    chosen = selected or detected or fallback
    source = "selected" if selected else ("detected" if detected else "fallback")
    try:
        page = await trending.fetch(chosen, limit, page_token, days=days)
    except trending.TrendingUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return TrendingOut(
        region=chosen,
        source=source,
        days=days,
        next_page_token=page.next_page_token,
        videos=[
            TrendingVideoOut(
                video_id=video.video_id,
                title=video.title,
                channel=video.channel,
                thumbnail=video.thumbnail,
                views=video.views,
                published_at=video.published_at,
                url=video.url,
            )
            for video in page.videos
        ],
    )
