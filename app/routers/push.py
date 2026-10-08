"""Authenticated browser Web Push subscription endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.deps import get_current_user
from app.models import User, WebPushSubscription
from app.schemas import MessageOut, WebPushConfigOut, WebPushSubscriptionRequest
from app.services import web_push

router = APIRouter(prefix="/push", tags=["web-push"])


@router.get("/config", response_model=WebPushConfigOut)
def config():
    if not web_push.configured():
        return WebPushConfigOut(enabled=False)
    return WebPushConfigOut(enabled=True, public_key=settings.WEB_PUSH_VAPID_PUBLIC_KEY.strip())


@router.post("/subscribe", response_model=MessageOut)
def subscribe(
    payload: WebPushSubscriptionRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not web_push.configured():
        raise HTTPException(status_code=404, detail="Web Push is not enabled.")
    row = db.execute(select(WebPushSubscription).where(WebPushSubscription.endpoint == payload.endpoint)).scalar_one_or_none()
    created_for_user = row is None or row.user_id != user.id
    if row is None:
        row = WebPushSubscription(user_id=user.id, endpoint=payload.endpoint, p256dh=payload.p256dh, auth=payload.auth, user_agent=(request.headers.get("user-agent") or "")[:512] or None)
        db.add(row)
    else:
        row.user_id, row.p256dh, row.auth = user.id, payload.p256dh, payload.auth
        row.user_agent = (request.headers.get("user-agent") or "")[:512] or None
    db.commit()
    if payload.send_welcome and created_for_user:
        web_push.welcome(db, user, settings.FREE_TRIAL_LIMIT)
    return MessageOut(detail="Browser notifications enabled.")
