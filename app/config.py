"""Application settings, loaded from environment variables / .env file."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # .env is the deploy/default configuration.  A developer can create
        # an untracked .env.local beside it; values there intentionally win so
        # running locally never requires editing DGX production settings.
        env_file=(".env", ".env.local"), env_file_encoding="utf-8", extra="ignore"
    )

    # ---- core ----------------------------------------------------------
    APP_NAME: str = "TrialGuard API"
    ENV: Literal["dev", "test", "prod"] = "dev"
    DEBUG: bool = True
    API_PREFIX: str = "/api/v1"

    # ---- database ------------------------------------------------------
    # dev/test -> sqlite, prod -> postgresql+psycopg://user:pass@host/db
    DATABASE_URL: str = "sqlite:///./trialguard.db"

    # ---- crypto / auth -------------------------------------------------
    # MUST be overridden in production. Used to sign JWTs.
    SECRET_KEY: str = "dev-only-insecure-secret-change-me"
    # Separate secret used to HMAC device fingerprints before storage.
    DEVICE_HASH_SECRET: str = "dev-only-insecure-device-pepper-change-me"

    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_TTL_MINUTES: int = 15
    REFRESH_TOKEN_TTL_DAYS: int = 30
    EMAIL_VERIFY_TTL_HOURS: int = 48
    PASSWORD_RESET_TTL_MINUTES: int = 30

    PASSWORD_MIN_LENGTH: int = 8
    # bcrypt work factor. 12 is the right cost for production; the test suite
    # drops it to 4 so hashing does not dominate the run time.
    BCRYPT_ROUNDS: int = 12

    # ---- trials --------------------------------------------------------
    FREE_TRIAL_LIMIT: int = 5
    # Enforce the limit per physical device as well as per account, so a user
    # cannot get a fresh allowance by registering a second email address.
    ENFORCE_DEVICE_TRIAL_LIMIT: bool = True
    # Second, coarser ledger keyed only on stable hardware traits (no client
    # -supplied installation id). It survives "clear extension storage" and
    # "reinstall the extension", which the composite fingerprint does not.
    # Its entropy is lower, so two genuinely different users on identical
    # hardware could share a row - hence a deliberately looser cap.
    ENFORCE_MACHINE_TRIAL_LIMIT: bool = True
    MACHINE_TRIAL_LIMIT: int = 15
    # Max distinct devices a single free account may register.
    MAX_DEVICES_PER_FREE_USER: int = 2
    MAX_DEVICES_PER_PAID_USER: int = 5
    # Require a verified email address before trials can be consumed.
    REQUIRE_EMAIL_VERIFICATION: bool = False

    # ---- billing -------------------------------------------------------
    PAYMENT_PROVIDER: Literal["mock", "stripe", "razorpay"] = "mock"
    PLAN_PRICE_CENTS: int = 500  # $5.00
    PLAN_CURRENCY: str = "USD"
    PLAN_INTERVAL: str = "month"

    # Shared secret for the mock provider's test endpoints. Empty (the default)
    # means the mock checkout/webhook routes are disabled outright.
    MOCK_BILLING_SECRET: str = ""

    STRIPE_SECRET_KEY: str = ""
    STRIPE_PRICE_ID: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""

    RAZORPAY_KEY_ID: str = ""
    RAZORPAY_KEY_SECRET: str = ""
    RAZORPAY_PLAN_ID: str = ""
    RAZORPAY_WEBHOOK_SECRET: str = ""

    BILLING_SUCCESS_URL: str = "https://example.com/billing/success"
    BILLING_CANCEL_URL: str = "https://example.com/billing/cancel"

    # ---- web app / summarisation ---------------------------------------
    # Serve the browser UI from this same service at "/".
    WEB_APP_ENABLED: bool = True

    # vLLM exposes an OpenAI-compatible API.  When this API itself runs in
    # Docker and vLLM is exposed on the host at :8010, host.docker.internal is
    # the correct address (docker-compose adds the Linux host-gateway mapping).
    VLLM_URL: str = "http://host.docker.internal:8010/v1"
    VLLM_MODEL: str = "google/gemma-4-26B-A4B-it"
    # Leave blank when vLLM was started without --api-key.
    VLLM_API_KEY: str = ""
    # Cloudflare Access service-token credentials for a vLLM endpoint exposed
    # through a protected Cloudflare Tunnel. Leave both blank when Access is
    # not enabled for the endpoint.
    CF_ACCESS_CLIENT_ID: str = ""
    CF_ACCESS_CLIENT_SECRET: str = ""
    VLLM_TIMEOUT_SECONDS: int = 600

    # Hard ceiling on how many requests THIS worker keeps in flight at vLLM at
    # once, across every feature - summary, notes chunks and translation alike.
    #
    # NOTES_CONCURRENCY only shapes the fan-out inside a single video. Nothing
    # bounded the number of videos, so N simultaneous users put
    # N x NOTES_CONCURRENCY generations on a server that runs --max-num-seqs of
    # them at a time; the overflow sat in vLLM's queue until it timed out, and
    # the retry loop then fed it straight back in. That is the hang.
    #
    # THIS IS PER UVICORN WORKER, so the number that reaches the GPU is
    # (workers x VLLM_MAX_CONCURRENCY). The deployment runs 3 workers x 10 = 30
    # in flight, matching vLLM's --max-num-seqs 30 exactly: the app is allowed
    # to fill the server and never to overfill it.
    VLLM_MAX_CONCURRENCY: int = 10
    # How long a request may wait for a free slot before it is told the server
    # is busy. Answering "try again shortly" in a minute is kinder than a
    # connection that hangs for half an hour and then dies anyway.
    # 0 means wait forever. At 30-way load a slot can legitimately take a few
    # minutes to come free, so this is generous - it exists to end a pile-up,
    # not to police normal queueing.
    VLLM_QUEUE_TIMEOUT_SECONDS: int = 300

    # A 350-400 word summary normally needs well under 1,000 tokens. Keeping
    # this bounded prevents a model that ignores the length instruction from
    # spending several minutes generating an unnecessarily long answer.
    SUMMARY_NUM_PREDICT: int = 1200

    # Longer transcripts are sampled down to this budget before summarising.
    # This applies to the on-screen SUMMARY only - the full notes always read
    # the entire transcript.
    TRANSCRIPT_MAX_CHARS: int = 12000
    # Summary and PDF requests commonly target the same video back-to-back.
    # Keep that transcript in process so the second request does not repeat
    # YouTube's slow, rate-limited network path. 0 disables the cache.
    TRANSCRIPT_CACHE_TTL_SECONDS: int = 1800
    TRANSCRIPT_CACHE_MAX_ENTRIES: int = 200
    # Applied to each request made by youtube-transcript-api and yt-dlp.
    YOUTUBE_REQUEST_TIMEOUT_SECONDS: int = 12
    # ---- output cache ----------------------------------------------------
    # Ek baar bani summary sabke liye. Default BAND hai - table ban jaane aur
    # sab theek dikhne ke baad .env me OUTPUT_CACHE_ENABLED=true kijiye.
    OUTPUT_CACHE_ENABLED: bool = False
    # Isse chhota jawab kabhi cache me nahi jaata - adhoora ya toota hua jawab
    # hamesha ke liye baithane se accha hai ki wo dobara bane.
    OUTPUT_CACHE_MIN_CHARS: int = 200
    # Itne din tak koi na maange to entry hat jaati hai. 0 = kabhi na hatao.
    OUTPUT_CACHE_TTL_DAYS: int = 90

    # ---- full notes (the PDF) ------------------------------------------
    # 6k keeps a chunk within the model context while requiring substantially
    # fewer model round trips than the old 3.5k default. Overlap stops a point
    # from falling between two chunks.
    NOTES_CHUNK_CHARS: int = 6000
    NOTES_CHUNK_OVERLAP: int = 400
    # 0 = no limit. Anything above 0 truncates long videos, and the user is
    # told when that happens - it is never silent.
    NOTES_MAX_CHUNKS: int = 0
    # Output budget per chunk. Notes are meant to be exhaustive, so this is
    # deliberately large.
    NOTES_NUM_PREDICT: int = 4096
    # How many chunks of ONE video's notes to have in flight at once. It no
    # longer needs to be conservative: VLLM_MAX_CONCURRENCY is the real cap, and
    # it is enforced across every request instead of inside each one. Set this
    # high and let the gate do the limiting.
    NOTES_CONCURRENCY: int = 30
    # Attempts per chunk before it is reported as missing.
    NOTES_CHUNK_RETRIES: int = 3
    # How many chunks of ONE translation to run at a time. Translation is
    # per-chunk independent, so it parallelises; the cap stops a single
    # 60-page set of notes from taking every vLLM slot on the box.
    TRANSLATE_CONCURRENCY: int = 30
    # Optional http(s) proxy for YouTube. Set this if your server's IP gets
    # rate-limited or blocked - e.g. http://user:pass@proxy-host:port
    YOUTUBE_PROXY: str = ""

    # Blocking work - the yt-dlp transcript fetch, and every database call an
    # endpoint makes - runs on this pool. Starlette defaults to 40 threads,
    # which a burst of long videos exhausts; then even a health check queues.
    THREADPOOL_MAX_THREADS: int = 96

    # ---- transport / CORS ---------------------------------------------
    # Chrome extensions call the API from origin chrome-extension://<id>
    ALLOWED_ORIGINS: str = "*"
    # Optional: only accept requests from these extension ids (comma separated).
    ALLOWED_EXTENSION_IDS: str = ""
    # Only honour X-Forwarded-For when the app really sits behind a proxy you
    # control. Otherwise any client could spoof its IP past the rate limiter.
    TRUST_PROXY_HEADERS: bool = False

    # ---- rate limiting -------------------------------------------------
    RATE_LIMIT_ENABLED: bool = True
    LOGIN_RATE_LIMIT: int = 10          # attempts
    LOGIN_RATE_WINDOW_SECONDS: int = 300
    SIGNUP_RATE_LIMIT: int = 5
    SIGNUP_RATE_WINDOW_SECONDS: int = 3600
    # Hard cap on how many accounts may ever be created from one device.
    MAX_ACCOUNTS_PER_DEVICE: int = 3

    # ---- email ---------------------------------------------------------
    # "console" just logs the message; swap for a real provider in prod.
    EMAIL_BACKEND: Literal["console", "smtp"] = "console"
    EMAIL_FROM: str = "no-reply@example.com"
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_STARTTLS: bool = True

    APP_BASE_URL: str = "http://localhost:8000"

    ADMIN_API_KEY: str = Field(default="", description="Static key for /admin routes")

    # ---- Google sign-in --------------------------------------------------
    # false rakhne par /auth/google 404 deta hai aur UI me button dikhta hi
    # nahi - kuch bigde to .env me false karke up -d, bas.
    GOOGLE_LOGIN_ENABLED: bool = False
    # Google Cloud -> Credentials -> OAuth client ID (Web application).
    # Client SECRET ki zaroorat NAHI hai.
    GOOGLE_CLIENT_ID: str = ""

    @field_validator("SECRET_KEY", "DEVICE_HASH_SECRET")
    @classmethod
    def _no_default_secrets_in_prod(cls, v: str, info):  # pragma: no cover - guard
        return v

    @property
    def allowed_origins_list(self) -> list[str]:
        if self.ALLOWED_ORIGINS.strip() == "*":
            return ["*"]
        return [o.strip() for o in self.ALLOWED_ORIGINS.split(",") if o.strip()]

    @property
    def allowed_extension_ids(self) -> list[str]:
        return [e.strip() for e in self.ALLOWED_EXTENSION_IDS.split(",") if e.strip()]

    @property
    def is_sqlite(self) -> bool:
        return self.DATABASE_URL.startswith("sqlite")




    # Ye email hamesha unlimited rahenge - owner/team ke liye. Comma se alag karo.
    # DB me nahi, isliye database reset ya naya deploy isse mitata nahi.
    UNLIMITED_EMAILS: str = ""

    @property
    def unlimited_emails(self) -> set[str]:
        return {e.strip().lower() for e in self.UNLIMITED_EMAILS.split(",") if e.strip()}   


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
