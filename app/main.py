"""FastAPI application entry point."""
from __future__ import annotations

import socket as _socket

_orig_getaddrinfo = _socket.getaddrinfo


def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, _socket.AF_INET, type, proto, flags)


_socket.getaddrinfo = _ipv4_only_getaddrinfo

import hashlib
import logging
import re
import time
from contextlib import asynccontextmanager
from html import escape

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

from pathlib import Path

from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import engine, init_db
from app.routers import admin, auth, billing, contact, push, summarize, usage, webhooks
from app.services import translate as translate_service
from app.services import youtube

STATIC_DIR = Path(__file__).parent / "static"

logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("trialguard")

_INSECURE_DEFAULTS = {
    "dev-only-insecure-secret-change-me",
    "dev-only-insecure-device-pepper-change-me",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Every blocking call this app makes - the yt-dlp transcript fetch, and now
    # each endpoint's database work - runs on Starlette's thread pool. Its
    # default of 40 is not enough for a burst of long videos: once it is full,
    # unrelated requests queue behind a transcript download, and /healthz stops
    # answering while the app is in fact fine.
    try:
        import anyio.to_thread

        anyio.to_thread.current_default_thread_limiter().total_tokens = max(
            10, settings.THREADPOOL_MAX_THREADS
        )
        logger.info("thread pool sized to %s", settings.THREADPOOL_MAX_THREADS)
    except Exception:  # pragma: no cover - anyio internals moved
        logger.warning("could not resize the thread pool", exc_info=True)

    if settings.ENV == "prod":
        if settings.SECRET_KEY in _INSECURE_DEFAULTS or (
            settings.DEVICE_HASH_SECRET in _INSECURE_DEFAULTS
        ):
            raise RuntimeError(
                "SECRET_KEY and DEVICE_HASH_SECRET must be set to strong random "
                "values before running in production."
            )
        if settings.PAYMENT_PROVIDER == "mock":
            logger.warning("PAYMENT_PROVIDER=mock in production - no money will move.")
    else:
        # Convenient for dev/tests; production should run Alembic migrations.
        init_db()
    yield
    summarize.pdf.shutdown()
    await summarize.summarizer.close_vllm_client()
    await translate_service.close_google_client()
    await youtube.close_oembed_client()
    engine.dispose()


app = FastAPI(
    title=settings.APP_NAME,
    version="1.0.0",
    description=(
        "Registration, device-bound free trials and $5/month subscriptions "
        "for a Chrome extension."
    ),
    lifespan=lifespan,
    docs_url="/docs" if settings.ENV != "prod" else None,
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,
    allow_credentials=False,  # we use Authorization headers, not cookies
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "Idempotency-Key",
        "X-Admin-Key",
        # Explicitly identifies requests made by the official Chrome extension
        # so their completed operations can be shown separately in admin.
        "X-TubeNotes-Client",
    ],
    max_age=600,
)


@app.middleware("http")
async def enforce_extension_allowlist(request: Request, call_next):
    """Only let the extensions you named call this API.

    ALLOWED_EXTENSION_IDS and its allowed_extension_ids property existed in the
    settings but nothing ever read them - the lock was configured and never
    installed, so any packed extension could call the API as readily as yours.

    Scope is deliberately narrow. It applies ONLY to chrome-extension://
    origins, so the web app and ordinary browsers are untouched, and an empty
    list keeps today's behaviour of allowing every extension. Auth and the
    trial ledger still do the real work; this just stops a stranger's build
    using your backend as free infrastructure.
    """
    allowed = settings.allowed_extension_ids
    if allowed:
        origin = request.headers.get("origin") or ""
        if origin.startswith("chrome-extension://"):
            ext_id = origin.removeprefix("chrome-extension://").strip("/")
            if ext_id not in allowed:
                logger.warning("blocked extension origin %s", origin)
                return JSONResponse(
                    status_code=status.HTTP_403_FORBIDDEN,
                    content={"detail": "This extension is not allowed to use this API."},
                )
    return await call_next(request)


@app.middleware("http")
async def add_timing_and_security_headers(request: Request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Response-Time-ms"] = f"{(time.perf_counter() - started) * 1000:.1f}"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": "Invalid request.", "errors": exc.errors()},
    )


app.include_router(auth.router, prefix=settings.API_PREFIX)
app.include_router(usage.router, prefix=settings.API_PREFIX)
app.include_router(summarize.router, prefix=settings.API_PREFIX)
app.include_router(billing.router, prefix=settings.API_PREFIX)
app.include_router(webhooks.router, prefix=settings.API_PREFIX)
app.include_router(admin.router, prefix=settings.API_PREFIX)
app.include_router(contact.router, prefix=settings.API_PREFIX)
app.include_router(push.router, prefix=settings.API_PREFIX)


@app.get("/healthz", tags=["meta"])
def healthz():
    return {"status": "ok", "env": settings.ENV, "version": app.version}


@app.get(f"{settings.API_PREFIX}/meta", tags=["meta"])
def meta():
    return {
        "name": settings.APP_NAME,
        "docs": "/docs" if settings.ENV != "prod" else None,
        "api": settings.API_PREFIX,
        "free_trials": settings.FREE_TRIAL_LIMIT,
        "price": f"${settings.PLAN_PRICE_CENTS / 100:.2f}/{settings.PLAN_INTERVAL}",
        # Frontend ko batata hai ki Google button dikhana hai ya nahi.
        "google_login": settings.GOOGLE_LOGIN_ENABLED and bool(settings.GOOGLE_CLIENT_ID),
        "google_client_id": settings.GOOGLE_CLIENT_ID if settings.GOOGLE_LOGIN_ENABLED else "",
        # A secondary site can be kept online while all new payments happen
        # on the primary site. This is a public URL, not a secret.
        "billing_primary_site_url": settings.BILLING_PRIMARY_SITE_URL.rstrip("/"),
        "chrome_web_store_url": settings.chrome_web_store_url,
    }


if settings.WEB_APP_ENABLED and STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/sw.js", include_in_schema=False)
    def web_push_service_worker():
        """Must be at the origin root so it can control the whole web app."""
        return FileResponse(
            STATIC_DIR / "sw.js",
            media_type="application/javascript",
            headers={
                "Cache-Control": "no-cache",
                "Service-Worker-Allowed": "/",
            },
        )

    @app.get("/admin", include_in_schema=False)
    def admin_dashboard():
        """The shell is public; every dashboard API call requires an admin JWT."""
        return FileResponse(
            STATIC_DIR / "admin.html",
            media_type="text/html",
            headers={"Cache-Control": "no-cache"},
        )

    def _asset_version() -> str:
        """A cache-buster derived from app.js itself.

        index.html used to carry a hand-written "?v=6", which meant every
        change to app.js shipped behind a stale one: the server served the new
        file and browsers kept the old, so a deploy silently did nothing on the
        front end. Hashing the file makes the version impossible to forget.
        """
        js = STATIC_DIR / "app.js"
        try:
            return hashlib.sha256(js.read_bytes()).hexdigest()[:12]
        except OSError:  # pragma: no cover - app.js always ships
            return "dev"

    _ASSET_VERSION = _asset_version()

    @app.get("/", include_in_schema=False)
    def home():
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        html = re.sub(r"app\.js\?v=[^\"\']*", f"app.js?v={_ASSET_VERSION}", html)
        # The shell must always revalidate; the assets it points at are
        # content-addressed, so they can be cached hard.
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

    _LEGAL_PAGE_POLISH = """
<style>
html[data-theme="light"] body{position:relative;isolation:isolate;overflow-x:hidden;background:linear-gradient(180deg,#faf8ff 0,#fff 46%)!important}html[data-theme="light"] body:before,html[data-theme="light"] body:after{content:"";position:fixed;z-index:-1;width:390px;height:390px;border-radius:50%;filter:blur(18px);opacity:.42;pointer-events:none}html[data-theme="light"] body:before{top:95px;left:-210px;background:radial-gradient(circle,#c4b5fd 0,rgba(196,181,253,0) 70%)}html[data-theme="light"] body:after{right:-185px;bottom:55px;background:radial-gradient(circle,#f9a8d4 0,rgba(249,168,212,0) 70%)}html[data-theme="light"] .hero{position:relative;overflow:hidden;background:radial-gradient(circle at 84% 10%,#e9d5ff 0,rgba(233,213,255,.1) 29%,transparent 49%),radial-gradient(circle at 11% 94%,#bae6fd 0,rgba(186,230,253,0) 35%),linear-gradient(125deg,#f4efff 0,#fff 49%,#fff5fb 100%)!important}html[data-theme="light"] .hero:after{content:"";position:absolute;right:9%;bottom:-70px;width:235px;height:145px;border:24px solid rgba(124,58,237,.13);border-radius:50%;transform:rotate(-16deg);pointer-events:none}html[data-theme="light"] .eyebrow{display:inline-flex;align-items:center;gap:7px;padding:5px 9px;border-radius:999px;background:linear-gradient(90deg,#ede9fe,#fce7f3);box-shadow:0 5px 14px rgba(124,58,237,.1)}html[data-theme="light"] .eyebrow:before{content:"✦";font-size:11px}html[data-theme="light"] .hero h1{background:linear-gradient(110deg,#29144e,#6d28d9 45%,#be185d);-webkit-background-clip:text;background-clip:text;color:transparent}html[data-theme="light"] .policy-nav{padding:8px;border:1px solid #e9defb;border-radius:16px;background:linear-gradient(120deg,rgba(255,255,255,.84),rgba(248,244,255,.92));box-shadow:0 10px 26px rgba(81,45,145,.08)}html[data-theme="light"] .policy-nav a{transition:transform .18s ease,box-shadow .18s ease,background .18s ease}html[data-theme="light"] .policy-nav a:hover{transform:translateY(-1px);box-shadow:0 5px 12px rgba(124,58,237,.14)}html[data-theme="light"] .policy-nav a.active{color:#fff!important;border-color:transparent!important;background:linear-gradient(125deg,#7c3aed,#db2777)!important;box-shadow:0 6px 14px rgba(124,58,237,.24)}html[data-theme="light"] .card{position:relative;overflow:hidden;border-color:#e4dbf1!important;transition:transform .2s ease,box-shadow .2s ease}html[data-theme="light"] .card:before{content:"";position:absolute;top:0;left:0;right:0;height:4px;background:linear-gradient(90deg,#8b5cf6,#ec4899,#38bdf8)}html[data-theme="light"] .card:nth-of-type(3n+2):before{background:linear-gradient(90deg,#06b6d4,#3b82f6,#8b5cf6)}html[data-theme="light"] .card:nth-of-type(3n):before{background:linear-gradient(90deg,#f59e0b,#f97316,#ec4899)}html[data-theme="light"] .card:hover{transform:translateY(-3px);box-shadow:0 16px 35px rgba(57,30,108,.12)!important}html[data-theme="light"] .card h2{color:#31204f!important}html[data-theme="light"] .card h2:after{content:"";display:block;width:42px;height:3px;margin-top:10px;border-radius:9px;background:linear-gradient(90deg,#8b5cf6,#ec4899)}html[data-theme="light"] .tile{border-color:#e7ddf5!important;background:linear-gradient(145deg,#fff,#f8f4ff 58%,#fff0f8)!important;transition:transform .18s ease,border-color .18s ease}html[data-theme="light"] .tile:hover{transform:translateY(-2px);border-color:#c4b5fd!important}html[data-theme="light"] .tile strong{color:#4c1d95}html[data-theme="light"] .notice{border-left:4px solid #38bdf8;background:linear-gradient(100deg,#eff6ff,#faf5ff)!important;box-shadow:0 8px 20px rgba(59,130,246,.07)}html[data-theme="light"] .callout{border-left-color:#ec4899!important;background:linear-gradient(100deg,#fff1f7,#f6f1ff)!important;box-shadow:0 10px 24px rgba(190,24,93,.08)}html[data-theme="light"] .footer{background:linear-gradient(90deg,rgba(248,245,255,.72),rgba(255,246,251,.72))}html[data-theme="light"] .back,html[data-theme="light"] .legal-theme{transition:transform .18s ease,border-color .18s ease,box-shadow .18s ease}html[data-theme="light"] .back:hover,html[data-theme="light"] .legal-theme:hover{transform:translateY(-1px);box-shadow:0 6px 14px rgba(124,58,237,.13)}html[data-theme="dark"] body{position:relative;isolation:isolate;overflow-x:hidden;background:linear-gradient(180deg,#120d20,#0d0a16)!important}html[data-theme="dark"] body:before,html[data-theme="dark"] body:after{content:"";position:fixed;z-index:-1;width:390px;height:390px;border-radius:50%;filter:blur(18px);pointer-events:none}html[data-theme="dark"] body:before{top:95px;left:-210px;background:radial-gradient(circle,#5b21b6 0,rgba(91,33,182,0) 70%);opacity:.26}html[data-theme="dark"] body:after{right:-185px;bottom:55px;background:radial-gradient(circle,#9d174d 0,rgba(157,23,77,0) 70%);opacity:.22}html[data-theme="dark"] .hero{position:relative;overflow:hidden;background:radial-gradient(circle at 84% 10%,#54267a 0,rgba(84,38,122,.12) 33%,transparent 52%),radial-gradient(circle at 11% 94%,#083d57 0,rgba(8,61,87,0) 37%),linear-gradient(125deg,#171025,#100d1b 74%,#1d1023)!important}html[data-theme="dark"] .hero h1{background:linear-gradient(110deg,#f5f3ff,#c4b5fd 48%,#f9a8d4);-webkit-background-clip:text;background-clip:text;color:transparent}html[data-theme="dark"] .policy-nav{padding:8px;border:1px solid #3a2b58;background:linear-gradient(120deg,rgba(29,22,47,.88),rgba(23,16,37,.94));box-shadow:none}html[data-theme="dark"] .card{position:relative;overflow:hidden;border-color:#36284e!important;background:linear-gradient(145deg,#1b152a,#151020)!important;transition:transform .2s ease,box-shadow .2s ease}html[data-theme="dark"] .card:before{content:"";position:absolute;top:0;left:0;right:0;height:4px;background:linear-gradient(90deg,#8b5cf6,#ec4899,#38bdf8)}html[data-theme="dark"] .card:hover{transform:translateY(-3px);box-shadow:0 16px 34px rgba(0,0,0,.28)!important}html[data-theme="dark"] .card h2{color:#f3edff!important}html[data-theme="dark"] .tile{border-color:#392c54!important;background:linear-gradient(145deg,#211832,#171120)!important}html[data-theme="dark"] .notice{border-left:4px solid #38bdf8;background:linear-gradient(100deg,#122842,#1c1531)!important}html[data-theme="dark"] .callout{border-left-color:#ec4899!important;background:linear-gradient(100deg,#31162d,#211735)!important}@media(prefers-reduced-motion:reduce){.card,.tile,.back,.legal-theme,.policy-nav a{transition:none}.card:hover,.tile:hover,.back:hover,.legal-theme:hover,.policy-nav a:hover{transform:none}}
</style>
"""

    def _legal_page(filename: str):
        """Serve legal pages with the same saved light/dark preference as TubeNotes."""
        page = (STATIC_DIR / filename).read_text(encoding="utf-8")
        page = page.replace("{{SUPPORT_EMAIL}}", escape(settings.SUPPORT_EMAIL))
        page = page.replace(
            "</head>",
            """<script>try{document.documentElement.dataset.theme=localStorage.getItem('tn_theme')||'light'}catch(_){document.documentElement.dataset.theme='light'}</script>""" + _LEGAL_PAGE_POLISH + "</head>",
            1,
        )
        page = page.replace(
            '<a class="back"',
            '<button id="legalThemeBtn" class="legal-theme" type="button" aria-label="Switch to dark theme" title="Switch to dark theme">Dark</button><a class="back"',
            1,
        )
        page = page.replace("</body>", '<script src="/static/legal.js" defer></script></body>', 1)
        return HTMLResponse(page, headers={"Cache-Control": "no-cache"})

    @app.get("/about", include_in_schema=False)
    def about_page():
        return _legal_page("about.html")

    @app.get("/terms", include_in_schema=False)
    def terms_page():
        return _legal_page("terms.html")

    @app.get("/privacy", include_in_schema=False)
    def privacy_page():
        return _legal_page("privacy.html")

    @app.get("/refund-policy", include_in_schema=False)
    def refund_policy_page():
        return _legal_page("refund-policy.html")

    @app.get("/contact", include_in_schema=False)
    def contact_page():
        return _legal_page("contact.html")

else:  # pragma: no cover - API-only deployment

    @app.get("/", tags=["meta"])
    def root():
        return meta()
