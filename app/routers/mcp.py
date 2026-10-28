"""Browser hand-off and secure PDF download routes for the TubeNotes MCP app."""
from __future__ import annotations

import jwt
from pathlib import Path

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from app.deps import get_current_user
from app.config import settings
from app.models import User
from app.schemas import DeviceFingerprint
from app.services import pdf
from app.services.mcp_oauth import (
    MCP_SCOPE,
    approve_authorization_request,
    authorization_request_details,
    register_dynamic_client,
)
from app.services.mcp_server import call_public_mcp_tool, mcp_tool_definitions, render_plugin_pdf

api_router = APIRouter(prefix="/mcp", tags=["mcp"])
public_router = APIRouter(tags=["mcp"])
_STATIC_DIR = Path(__file__).resolve().parents[1] / "static"


class ApproveMcpAuthorizationRequest(BaseModel):
    request_id: str = Field(min_length=24, max_length=128)
    device: DeviceFingerprint


def _rpc_result(request_id: Any, result: dict[str, Any]) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result})


@public_router.post("/register", include_in_schema=False)
@public_router.post("/mcp/register", include_in_schema=False)
@public_router.post("/gemini-mcp/register", include_in_schema=False)
async def register_mcp_oauth_client(request: Request):
    """OAuth Dynamic Client Registration for the supported MCP connectors.

    The FastMCP version used by the service requires a refresh-token grant at
    registration time. Claude correctly registers a PKCE-only public client,
    while Gemini requires an OAuth public client without a client secret. This
    compatibility endpoint is served before the FastMCP sub-application is
    mounted.
    """
    try:
        metadata = await request.json()
    except ValueError:
        metadata = None
    try:
        client = await register_dynamic_client(metadata)
    except ValueError as exc:
        return JSONResponse(
            {"error": "invalid_client_metadata", "error_description": str(exc)},
            status_code=status.HTTP_400_BAD_REQUEST,
            headers={
                "Cache-Control": "no-store",
                "Access-Control-Allow-Origin": "*",
            },
        )
    return JSONResponse(
        client.model_dump(mode="json", exclude_none=True),
        status_code=status.HTTP_201_CREATED,
        headers={
            "Cache-Control": "no-store",
            "Access-Control-Allow-Origin": "*",
        },
    )


@public_router.options("/gemini-mcp/register", include_in_schema=False)
async def gemini_mcp_register_preflight():
    """Allow browser-based OAuth registration from Gemini's Connected Apps UI."""
    return Response(
        status_code=status.HTTP_204_NO_CONTENT,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Max-Age": "3600",
        },
    )


@public_router.get("/gemini-mcp/.well-known/oauth-authorization-server", include_in_schema=False)
async def gemini_oauth_authorization_server_metadata():
    """Advertise public-client OAuth metadata accepted by Gemini Connected Apps.

    FastMCP 1.12 hard-codes ``client_secret_post`` in discovery despite also
    supporting the standard public-client ``none`` method. Gemini correctly
    requires PKCE public-client OAuth, so expose that accurate metadata only on
    the separate Gemini transport.
    """
    issuer = settings.gemini_mcp_public_url
    return JSONResponse(
        {
            "issuer": issuer,
            "authorization_endpoint": f"{issuer}/authorize",
            "token_endpoint": f"{issuer}/token",
            "registration_endpoint": f"{issuer}/register",
            "scopes_supported": [MCP_SCOPE],
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code"],
            "token_endpoint_auth_methods_supported": ["none"],
            "service_documentation": f"{settings.APP_BASE_URL.rstrip('/')}/privacy",
            "code_challenge_methods_supported": ["S256"],
        },
        headers={
            "Cache-Control": "public, max-age=3600",
            "Access-Control-Allow-Origin": "*",
        },
    )


def _mcp_oauth_redirect(request: Request, endpoint: str) -> RedirectResponse:
    """Compatibility aliases for clients that resolve OAuth URLs at the origin.

    Claude Desktop currently requests ``/register`` after discovering this MCP
    endpoint, despite the advertised ``/mcp/register`` URL. Keep the aliases
    narrow and use 307 so a token POST retains its body and method.
    """
    target = f"{settings.mcp_public_url.rstrip('/')}/{endpoint}"
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return RedirectResponse(target, status_code=status.HTTP_307_TEMPORARY_REDIRECT)


@public_router.get("/authorize", include_in_schema=False)
async def root_oauth_authorize_alias(request: Request):
    return _mcp_oauth_redirect(request, "authorize")


@public_router.post("/token", include_in_schema=False)
async def root_oauth_token_alias(request: Request):
    return _mcp_oauth_redirect(request, "token")


@public_router.post("/revoke", include_in_schema=False)
async def root_oauth_revoke_alias(request: Request):
    return _mcp_oauth_redirect(request, "revoke")


@public_router.post("/mcp/", include_in_schema=False)
async def public_streamable_mcp(request: Request):
    """OAuth-aware Streamable HTTP transport used by ChatGPT Plugins.

    The installed FastMCP 1.x transport protects ``initialize`` and
    ``tools/list`` behind OAuth. ChatGPT needs those two calls to be public in
    order to discover a tool's ``securitySchemes`` and display account linking.
    OAuth endpoints remain served by the mounted FastMCP application below.
    """
    try:
        payload = await request.json()
    except ValueError:
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid JSON"}}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Batch requests are unsupported"}}, status_code=400)

    request_id = payload.get("id")
    method = payload.get("method")
    params = payload.get("params") or {}
    if not isinstance(params, dict):
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "params must be an object"}}, status_code=400)
    if isinstance(method, str) and method.startswith("notifications/"):
        return Response(status_code=status.HTTP_202_ACCEPTED)
    if method == "initialize":
        return _rpc_result(request_id, {
            "protocolVersion": str(params.get("protocolVersion") or "2025-03-26"),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "TubeNotes", "version": "1.0.10"},
            "instructions": "Before each TubeNotes summary or PDF request, ask the user to explicitly choose an output language. Never default it automatically.",
        })
    if method == "ping":
        return _rpc_result(request_id, {})
    if method == "tools/list":
        return _rpc_result(request_id, {"tools": mcp_tool_definitions()})
    if method == "tools/call":
        tool_name = str(params.get("name", ""))
        arguments = params.get("arguments")
        if arguments is not None and not isinstance(arguments, dict):
            return JSONResponse({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "arguments must be an object"}}, status_code=400)
        result = await call_public_mcp_tool(request.headers.get("authorization"), tool_name, arguments)
        return _rpc_result(request_id, result)
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}}, status_code=404)


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
    try:
        redirect_url = approve_authorization_request(payload.request_id, user.id, payload.device)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
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
