# TubeNotes.ai

TubeNotes.ai is an AI-powered learning platform that turns YouTube videos into clear summaries, key points, detailed notes, translations, and downloadable PDF documents. It includes a responsive web application, a Chrome Extension, secure account management, subscription billing, browser notifications, and an administrator dashboard.

> The Hindi project guide is available in [README.hi.md](README.hi.md).

## What TubeNotes does

- Accepts standard YouTube, `youtu.be`, and Shorts URLs.
- Retrieves available video metadata and captions/transcripts.
- Generates AI summaries and key points in the video's language or a selected language.
- Creates structured full notes for long videos, then renders them as a PDF.
- Translates generated notes into supported languages.
- Provides a Chrome Extension that works directly on YouTube pages through a side panel.
- Supports email/password and Google sign-in.
- Manages free credits, subscriptions, invoices, cancellation at the end of a billing cycle, and active devices.
- Includes an admin dashboard for users, jobs, billing, usage, system health, analytics, and browser push campaigns.

## Architecture

```text
Web App (HTML/CSS/JavaScript) ─┐
                               ├── FastAPI REST API ── SQLAlchemy ── PostgreSQL / SQLite
Chrome Extension (Manifest V3) ┘           │
                                            ├── vLLM OpenAI-compatible API ── Gemma model
                                            ├── YouTube transcript sources / yt-dlp
                                            ├── Razorpay or Stripe
                                            ├── Google Identity Services
                                            ├── Web Push (VAPID)
                                            └── SMTP / ntfy operational alerts
```

## Technology stack

### Frontend web application

| Technology | Usage in TubeNotes |
|---|---|
| HTML5 | Customer web app, legal pages, account UI, and admin dashboard markup. |
| CSS3 | Responsive layouts, light/dark themes, gradients, animations, accessibility states, and admin styling. |
| Vanilla JavaScript (ES modules / modern browser APIs) | UI state, API calls, streaming results, account actions, PDF downloads, analytics events, and admin interactions. No React, Vue, or Angular framework is required. |
| Fetch API | Communicates with the FastAPI backend using JSON and authenticated requests. |
| NDJSON streaming | Streams AI summary generation progress and content to the browser. |
| Progressive Web App (PWA) | `manifest.webmanifest`, service worker, install experience, icons, and offline-capable asset handling. |
| Service Worker / Push API / Notifications API | Browser push subscription and customer notifications. |
| Google Analytics 4 | Public web-page analytics through the Google tag (`gtag.js`). |
| Meta Pixel | Public marketing page-view analytics. |

### Chrome Extension

| Technology | Usage in TubeNotes |
|---|---|
| Chrome Extensions Manifest V3 | Modern Chrome Extension platform and packaging format. |
| Background service worker | Manages long-running extension tasks, API communication, alarms, and PDF workflows. |
| Chrome Side Panel API | Displays TubeNotes alongside a YouTube video. |
| Content scripts | Adds TubeNotes actions and reads permitted YouTube page context. |
| Chrome Identity API | Google sign-in flow inside the extension. |
| Chrome Storage API | Stores extension session and device-related local state. |
| Chrome Tabs, Active Tab, Downloads, and Alarms APIs | YouTube integration, tab access, PDF download support, and scheduled background work. |
| WebExtension browser polyfill | Browser-API compatibility layer included with the extension build. |
| Plain HTML, CSS, and JavaScript | Extension account page, side panel, options page, content UI, and background logic. |

### Backend and API

| Technology | Usage in TubeNotes |
|---|---|
| Python 3.11 | Primary server-side language and Docker runtime. |
| FastAPI | REST API, routers, request validation, static-file serving, health check, and API documentation during development. |
| Uvicorn | ASGI application server. |
| Pydantic v2 and pydantic-settings | Request/response models, validation, and environment-based configuration. |
| HTTPX | Async HTTP requests to AI, YouTube, translation, and external services. |
| AnyIO / asyncio | Asynchronous streaming, concurrency limits, background work, and task coordination. |
| Starlette components | CORS middleware, static files, response handling, and FastAPI runtime features. |

### AI, summarization, translation, and video processing

| Technology | Usage in TubeNotes |
|---|---|
| Gemma model | The language model used for summaries, full notes, key points, and supported-language generation. |
| vLLM | Serves the Gemma model through an OpenAI-compatible `/chat/completions` API. It is deployed separately so model weights remain resident during application deployments. |
| OpenAI-compatible API format | Standard request/response format used by the FastAPI service to call vLLM. |
| Server-side concurrency gate | Controls AI workload across interactive summaries, long-note chunks, and translations to protect GPU capacity. |
| `youtube-transcript-api` | First transcript/caption retrieval source. |
| `yt-dlp` | Fallback transcript/video metadata retrieval path where captions need another extraction route. |
| YouTube oEmbed / public metadata endpoints | Video title, author, thumbnail, and availability information. |
| `langdetect` | Detects caption language when YouTube metadata is incomplete or incorrect. |
| Google Translate endpoint | Translation fallback when model output needs language correction or vLLM translation is unavailable. |

### Database and data migrations

| Technology | Usage in TubeNotes |
|---|---|
| PostgreSQL 16 | Production relational database for users, devices, subscriptions, payment history, jobs, cached outputs, push subscriptions, and admin data. |
| SQLite | Lightweight local development and test database option. |
| SQLAlchemy 2 | ORM, database models, queries, transactions, and row-level entitlement protection. |
| Alembic | Versioned database schema migrations. |
| psycopg 3 | PostgreSQL database driver. |

### Authentication, security, and abuse protection

| Technology | Usage in TubeNotes |
|---|---|
| JWT / PyJWT | Short-lived access tokens and rotating refresh tokens. |
| Cryptography | Secure verification support, including Google Identity token verification. |
| bcrypt / Passlib | Password hashing with configurable work factors. |
| HMAC-SHA256 | Hashes device fingerprints and validates signed webhook events. Raw device fingerprints are not stored. |
| Google Identity Services | Google sign-in for the web application. |
| Chrome Identity API | Google sign-in for the Chrome Extension. |
| CORS middleware | Restricts allowed browser origins; extension IDs can also be allow-listed. |
| Server-side rate limiting | Protects login, sign-up, verification, and password reset routes. |
| Device and machine ledgers | Enforces free-credit limits across accounts and devices. |
| Optional native messaging helper | A local helper can supply a real MAC address when this stronger device signal is intentionally installed. |
| Security headers | Adds `X-Content-Type-Options` and `Referrer-Policy` responses. |

### Payments and subscriptions

| Technology | Usage in TubeNotes |
|---|---|
| Razorpay SDK | India-focused subscription checkout, payment records, invoices, webhooks, and end-of-cycle cancellation. |
| Stripe SDK | Alternative international subscription checkout and webhook integration. |
| Webhooks | Signature-verified, idempotent payment and subscription state updates. |
| Mock payment provider | Safe local-development billing flow; disabled for real production use. |
| Country-aware pricing | Trusted Cloudflare visitor-country headers can select INR or international plans. |

### PDF generation

| Technology | Usage in TubeNotes |
|---|---|
| WeasyPrint | Server-side HTML/CSS to PDF rendering for full notes. |
| Python-Markdown | Converts structured Markdown notes for PDF rendering. |
| Pango and Cairo | Text shaping and graphics rendering required by WeasyPrint. |
| Noto fonts | Correct glyph coverage for Hindi, Tamil, Arabic, and other multilingual PDF content. |

### Notifications, email, and monitoring

| Technology | Usage in TubeNotes |
|---|---|
| `pywebpush` | Sends VAPID-authenticated browser push notifications. |
| VAPID | Public/private key authentication for Web Push subscriptions. |
| SMTP / Python `smtplib` | Email verification, password reset, billing, and account emails. |
| ntfy | Optional private operational alerts for service and delivery failures. |
| Admin dashboard | Browser-based reporting for usage, jobs, payments, error visibility, model queue, database, storage, and health signals. |

### Deployment and infrastructure

| Technology | Usage in TubeNotes |
|---|---|
| Docker | Reproducible Python application container. |
| Docker Compose | Local, production, and shared-database deployment stacks. |
| PostgreSQL Docker image | Production database container in the standard stack. |
| Nginx | HTTPS reverse proxy configuration for the public TubeNotes domain. |
| Cloudflare | Optional trusted country headers and optional Access protection for internal AI connectivity. |
| Linux / Debian slim image | Container base image for the API and PDF-rendering libraries. |

### Quality assurance and developer tooling

| Technology | Usage in TubeNotes |
|---|---|
| pytest | Backend integration and unit test suite. |
| `requirements.txt` | Production Python dependency lock list. |
| `requirements-dev.txt` | Development and testing dependencies. |
| `.env` configuration | Environment-driven secrets and deployment settings. Never commit real secrets. |
| Git | Source control and deployment history. |

## Repository structure

```text
app/
  main.py                 FastAPI application, public pages, security middleware
  config.py               Environment-based application settings
  database.py             SQLAlchemy engine and session setup
  models.py               Database models
  schemas.py              Pydantic API schemas
  routers/                Auth, summaries, billing, admin, push, contact, webhooks
  services/               AI, YouTube, PDFs, payments, email, devices, push, caching
  static/                 Web application, PWA, legal pages, and admin dashboard

alembic/                  Database migrations
extension-client/         Chrome Extension source and store-release package
native-helper/            Optional native messaging helper for MAC address support
deploy/                   Nginx and deployment scripts
tests/                    pytest test suite
Dockerfile                API container definition
docker-compose.yml        Local Docker development stack
docker-compose.prod.yml   Production stack with PostgreSQL
docker-compose.shared-db.prod.yml  Production API using an existing shared database
```

## API overview

The backend uses the `/api/v1` prefix.

| Area | Example endpoints |
|---|---|
| Authentication | `/auth/signup`, `/auth/login`, `/auth/google`, `/auth/refresh`, `/auth/me` |
| User devices | `/auth/devices` |
| Entitlements and credits | `/entitlement/check`, `/usage/consume`, `/usage/history` |
| Video intelligence | `/video/info`, `/summarize`, `/notes`, translation endpoints |
| Billing | `/billing/plans`, `/billing/checkout`, `/billing/subscription`, `/billing/cancel` |
| Payment webhooks | `/webhooks/razorpay`, `/webhooks/stripe` |
| Browser push | `/push/config`, subscription and campaign endpoints |
| Administration | `/admin/stats`, users, jobs, billing, analytics, health, and campaign routes |

Interactive API documentation is available at `/docs` in non-production environments.

## Local development

### Prerequisites

- Python 3.11+
- Optional: Docker Desktop and Docker Compose
- Optional: PostgreSQL 16 for a production-like local setup
- A reachable vLLM/Gemma endpoint if you want to generate real AI output

### Run with Python

```bash
git clone <your-repository-url>
cd tubenotes-ai

python -m venv .venv
```

Activate the environment:

```bash
# Windows PowerShell
.\.venv\Scripts\Activate.ps1

# macOS / Linux
source .venv/bin/activate
```

Install packages and create local configuration:

```bash
pip install -r requirements-dev.txt
copy .env.example .env       # Windows
# cp .env.example .env       # macOS / Linux
```

Start the API:

```bash
uvicorn app.main:app --reload
```

Open:

- Web app: `http://localhost:8000/`
- API docs: `http://localhost:8000/docs`
- Admin UI: `http://localhost:8000/admin`

### Run with Docker

```bash
copy .env.example .env       # Windows
# cp .env.example .env       # macOS / Linux

docker compose up --build
```

This starts the FastAPI service and PostgreSQL. Docker startup applies Alembic migrations before serving the API.

## Environment configuration

Start with `.env.example` for the full list. Important production settings include:

```dotenv
ENV=prod
DATABASE_URL=postgresql+psycopg://USER:PASSWORD@HOST:5432/DATABASE
SECRET_KEY=use-a-long-random-secret
DEVICE_HASH_SECRET=use-a-different-long-random-secret

GOOGLE_LOGIN_ENABLED=true
GOOGLE_CLIENT_ID=your-google-oauth-client-id

PAYMENT_PROVIDER=razorpay  # or stripe
RAZORPAY_KEY_ID=
RAZORPAY_KEY_SECRET=
RAZORPAY_WEBHOOK_SECRET=

VLLM_URL=http://gemma4:8000/v1
VLLM_MODEL=gemma4-summarizer

WEB_PUSH_ENABLED=true
WEB_PUSH_VAPID_PUBLIC_KEY=
WEB_PUSH_VAPID_PRIVATE_KEY=

EMAIL_BACKEND=smtp
SMTP_HOST=
SMTP_USER=
SMTP_PASSWORD=
```

Do not commit `.env`, payment keys, database passwords, VAPID private keys, or OAuth credentials.

## Database migrations

```bash
alembic upgrade head
```

Generate a new migration only after changing SQLAlchemy models:

```bash
alembic revision --autogenerate -m "describe the schema change"
alembic upgrade head
```

## Tests

```bash
pytest
```

The test suite covers authentication, trials, billing, summaries, PDF generation, web push, output caching, admin reporting, analytics, and extension-related flows.

## Production deployment

The repository includes deployment files for Docker, Docker Compose, and Nginx:

- `docker-compose.prod.yml` — API plus dedicated PostgreSQL stack.
- `docker-compose.shared-db.prod.yml` — API deployment that connects to an existing private PostgreSQL Docker network.
- `deploy/nginx-tubenotes-ai.conf` — Nginx reverse-proxy configuration.
- `deploy/deploy.sh` and `deploy/deploy-shared-db.sh` — deployment helpers.

For the full deployment procedure, see [DEPLOY.hi.md](DEPLOY.hi.md) and [DEPLOY_SHARED_DATABASE.hi.md](DEPLOY_SHARED_DATABASE.hi.md).

## Security notes

- Secrets are read from environment variables, not source code.
- Passwords are bcrypt-hashed.
- Access and refresh tokens are separate; refresh tokens rotate.
- Payment webhooks are signature-verified and processed idempotently.
- Device identifiers are HMAC-hashed before database storage.
- Production should use PostgreSQL, HTTPS, strict CORS origins, and a reverse proxy.
- Use `ALLOWED_EXTENSION_IDS` to allow only the official Chrome Extension to access extension-origin API calls.
- Keep mock billing disabled on public production deployments.

## License

No licence has been declared in this repository. Add the appropriate licence before distributing the code publicly.
