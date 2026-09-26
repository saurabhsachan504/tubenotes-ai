"""Public, rate-limited support contact form."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.deps import get_client_ip
from app.schemas import ContactRequest, MessageOut
from app.services import email as email_service
from app.services import ratelimit

router = APIRouter(tags=["contact"])


@router.post("/contact", response_model=MessageOut, status_code=status.HTTP_202_ACCEPTED)
def submit_contact_form(
    payload: ContactRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> MessageOut:
    """Forward a validated support request to the configured support inbox."""
    # Silently accept simple bot submissions so the honeypot does not reveal
    # how it is detected. No email is sent for these requests.
    if payload.website:
        return MessageOut(detail="Thanks. Your message has been received.")

    try:
        ratelimit.hit(
            db,
            f"contact:{get_client_ip(request)}",
            limit=5,
            window_seconds=3600,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise

    category = payload.category.replace("_", " ").title()
    body = (
        "New TubeNotes support request\n\n"
        f"Category: {category}\n"
        f"Name: {payload.name}\n"
        f"Email: {payload.email}\n\n"
        "Message:\n"
        f"{payload.message}\n"
    )
    sent = email_service.send_email(
        settings.SUPPORT_EMAIL,
        f"[TubeNotes Support] {category} — {payload.name}",
        body,
        reply_to=str(payload.email),
    )
    if not sent:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Support email is temporarily unavailable. Please try again later.",
        )
    return MessageOut(detail="Thanks—your message has been sent to TubeNotes support.")
