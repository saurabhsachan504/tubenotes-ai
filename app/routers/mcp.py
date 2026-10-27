"""Browser hand-off and secure PDF download routes for the TubeNotes MCP app."""
from __future__ import annotations

import jwt
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from app.deps import get_current_user
from app.models import User
from app.services import pdf
from app.services.mcp_oauth import approve_authorization_request, authorization_request_details
from app.services.mcp_server import render_plugin_pdf

api_router = APIRouter(prefix="/mcp", tags=["mcp"])
public_router = APIRouter(tags=["mcp"])
_STATIC_DIR = Path(__file__).resolve().parents[1] / "static"


class ApproveMcpAuthorizationRequest(BaseModel):
    request_id: str = Field(min_length=24, max_length=128)


@api_router.get("/oauth/request/{request_id}")
def oauth_request(request_id: str):
    details = authorization_request_details(request_id)
    if details is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This connection request expired. Return to ChatGPT and try again.")
    return details


@api_router.post("/oauth/approve")
def approve_oauth_request(
    payload: ApproveMcpAuthorizationRequest,
    user: User = Depends(get_current_user),
):
    redirect_url = approve_authorization_request(payload.request_id, user.id)
    if redirect_url is None:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="This connection request expired. Return to ChatGPT and try again.")
    return {"redirect_url": redirect_url}


@public_router.get("/plugin/connect", include_in_schema=False)
def plugin_connect_page():
    return FileResponse(_STATIC_DIR / "plugin-connect.html", media_type="text/html", headers={"Cache-Control": "no-store"})


@public_router.get("/mcp/download/{token}", include_in_schema=False)
async def plugin_pdf_download(token: str):
    try:
        data, filename = await render_plugin_pdf(token)
    except jwt.PyJWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="This PDF link is invalid or expired.")
    except (ValueError, pdf.PDFTooLarge) as exc:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail=str(exc))
    except pdf.PDFBusy as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc), headers={"Retry-After": "30"})
    return Response(
        content=data,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
        },
    )
