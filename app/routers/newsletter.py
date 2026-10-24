"""Public unsubscribe endpoint for opt-in TubeNotes newsletter recipients."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.services import newsletter

router = APIRouter(prefix="/newsletter", tags=["newsletter"])


def _page(title: str, message: str, *, ok: bool) -> HTMLResponse:
    color = "#7135e8" if ok else "#a23b54"
    return HTMLResponse(f"""<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title></head><body style="margin:0;padding:32px 16px;background:#f6f2fb;font-family:Arial,sans-serif;color:#28213b"><main style="max-width:520px;margin:9vh auto;padding:30px;border:1px solid #e6ddf5;border-radius:16px;background:#fff;text-align:center;box-shadow:0 14px 45px rgba(57,25,100,.12)"><div style="color:{color};font-size:25px;font-weight:800">&#9654; TubeNotes</div><h1 style="margin:22px 0 10px;font-size:23px">{title}</h1><p style="margin:0;color:#665b74;line-height:1.55">{message}</p></main></body></html>""")


@router.get("/unsubscribe", response_class=HTMLResponse)
def unsubscribe(token: str = Query(min_length=38, max_length=160), db: Session = Depends(get_db)):
    user_id = newsletter.user_id_from_unsubscribe_token(token)
    if user_id is None:
        return _page("This link is not valid", "Please use the unsubscribe link from a recent TubeNotes email.", ok=False)
    user = db.get(User, user_id)
    if user is None:
        return _page("This link is not valid", "Please use the unsubscribe link from a recent TubeNotes email.", ok=False)
    user.newsletter_opt_in = False
    user.newsletter_unsubscribed_at = datetime.now(timezone.utc)
    db.commit()
    return _page("You have been unsubscribed", "You will no longer receive TubeNotes podcast updates. You can opt in again from your account whenever you want.", ok=True)
