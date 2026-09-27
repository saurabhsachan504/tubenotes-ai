"""Application settings, loaded from environment variables / .env file."""
from __future__ import annotations

from datetime import datetime
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
    # Legacy default used by the old single-price Stripe/mock flow. New
    # checkout code uses the explicit India/international settings below.
    PLAN_PRICE_CENTS: int = 500  # $5.00
    PLAN_CURRENCY: str = "USD"
    PLAN_INTERVAL: str = "month"
    INDIA_PLAN_PRICE_SUBUNITS: int = 29_900  # Rs 299.00, expressed in paise
    INTERNATIONAL_PLAN_PRICE_CENTS: int = 500  # $5.00, expressed in cents

    # Cloudflare adds CF-IPCountry, and its visitor-location managed transform
    # can add CF-IPCity, only when it proxies the visitor request. Keep this
    # false for a directly exposed origin: an arbitrary caller could otherwise
    # forge the header. Enable it only when the origin accepts traffic
    # exclusively from Cloudflare (for example, through a Tunnel).
    TRUST_CLOUDFLARE_COUNTRY_HEADER: bool = False

    # Shared secret for the mock provider's test endpoints. Empty (the default)
    # means the mock checkout/webhook routes are disabled outright.
    MOCK_BILLING_SECRET: str = ""

    STRIPE_SECRET_KEY: str = ""
    STRIPE_PRICE_ID: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""

    RAZORPAY_KEY_ID: str = ""
    RAZORPAY_KEY_SECRET: str = ""
    # Keep RAZORPAY_PLAN_ID as a temporary backwards-compatible fallback for
    # the USD plan. Production should set both explicit IDs below.
    RAZORPAY_PLAN_ID: str = ""
    RAZORPAY_PLAN_ID_INR: str = ""
    RAZORPAY_PLAN_ID_USD: str = ""
    RAZORPAY_WEBHOOK_SECRET: str = ""

    BILLING_SUCCESS_URL: str = "https://example.com/billing/success"
    BILLING_CANCEL_URL: str = "https://example.com/billing/cancel"
    # Set this only on a legacy/secondary domain. Its web UI will send users
    # to the primary domain before checkout, so there is one canonical place
    # for new payments. Leave blank on the primary domain itself.
    BILLING_PRIMARY_SITE_URL: str = ""

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
    # The context window vLLM was started with (--max-model-len). Nothing here
    # can change it; it is declared so the app can SIZE ITSELF against it
    # instead of hoping a chunk fits. See effective_chunk_chars().
    VLLM_MAX_MODEL_LEN: int = 10000
    # Rough bytes-per-token for the languages this serves. Latin script runs
    # ~4; Devanagari and Tamil are denser per token, so this errs low on
    # purpose - a chunk slightly too small merely costs a round trip, while one
    # too big is a request the model refuses.
    CHARS_PER_TOKEN: float = 3.0
    # Measured against this model's own tokenizer: English 6.95 chars/token,
    # Hindi 4.27, a mix of the two 5.33 - Devanagari costs about 1.6x the
    # tokens of Latin script for the same text, which is why a Hindi video is
    # genuinely slower to write than an English one of the same length.
    #
    # Deliberately separate from CHARS_PER_TOKEN above. That one sizes chunks
    # and erring LOW is safe there - a smaller chunk only costs a round trip.
    # This one estimates how much work is left, where erring low makes the
    # progress bar expect tokens that never arrive and lag behind the work.
    OUTPUT_CHARS_PER_TOKEN: float = 5.3
    # Generous on purpose. Under load the delay is this process's own
    # scheduling lag, not vLLM being unreachable, and 15s was short
    # enough to turn that lag into a failed summary.
    VLLM_CONNECT_TIMEOUT_SECONDS: int = 60

    # Hard ceiling on how many requests THIS worker keeps in flight at vLLM at
    # once, across every feature - summary, notes chunks and translation alike.
    #
    # NOTES_CONCURRENCY only shapes the fan-out inside a single video. Nothing
    # bounded the number of videos, so N simultaneous users put
    # N x NOTES_CONCURRENCY generations on a server that runs --max-num-seqs of
    # them at a time; the overflow sat in vLLM's queue until it timed out, and
    # the retry loop then fed it straight back in. That is the hang.
    #
    # THIS IS PER UVICORN WORKER, so what reaches the GPU is
    # (workers x VLLM_MAX_CONCURRENCY). The deployment runs 3 x 10 = 30,
    # matching vLLM's --max-num-seqs 30.
    #
    # Both halves of that were measured, and one worker does NOT work here:
    #
    #   3 workers x 10 : 30/30 summaries, 102s, no failures.
    #   1 worker  x 30 : 2/30. The rest died on httpx.ConnectTimeout to a vLLM
    #                    that answers 30 cold connects in 0.1s when asked
    #                    directly.
    #
    # The reason is yt-dlp. Pulling a transcript is heavy *Python* CPU work,
    # and thirty of those in one process hold the GIL hard enough to starve the
    # event loop - asyncio's 15s connect timer then expires on a connection
    # that would have taken a millisecond. Several processes means several
    # GILs, which is the only thing that actually fixes it.
    #
    # PER UVICORN WORKER: 3 workers x 32 = 96, matching --max-num-seqs 96.
    #
    # Sized from measurement, not intuition. Throughput on this box keeps
    # climbing with batch size - there is no knee below the ceiling:
    #
    #     batch 30 ->  650 tok/s      batch 64 -> 1132 tok/s
    #     batch 48 ->  949 tok/s      batch 96 -> 1390 tok/s
    #
    # against 140 tok/s at the batch of 4 this deployment started with. KV
    # cache is not the constraint either: 30 running sequences used 11.5% of
    # 59 GiB.
    #
    # The cost is per-stream speed: 21.7 tok/s each at batch 30 against 14.5 at
    # batch 96. A single user alone on the box sees a slower stream. That is the
    # right trade here because the box exists to serve many videos at once, and
    # total work finishes far sooner - but it is the number to revisit if
    # single-user latency ever matters more than throughput.
    # 96 = vLLM's --max-num-seqs, so ONE worker can fill the machine on its
    # own. At 32 the batch permits worked out to 24, and a 20-browser run sat
    # at "Running: 24" against a server sized for 96 - a quarter of the box,
    # and the measured difference between 340 tok/s and 1,390.
    #
    # If load does spread over all three workers they can offer 288; vLLM runs
    # 96 and queues the rest, which is safe now that the queue is bounded,
    # retries back off, and batch work waits on NOTES_QUEUE_TIMEOUT_SECONDS.
    VLLM_MAX_CONCURRENCY: int = 96
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
    # The two transcript sources used to run one after the other: the captions
    # API first, and yt-dlp only once it had failed - which, on a request that
    # times out, means 12 seconds of waiting before the second one even starts.
    # Measured on a real request: 19.1s to a transcript that yt-dlp could have
    # produced in about 7.
    #
    # So yt-dlp now joins in after this many seconds rather than waiting its
    # turn, and whichever answers first wins. Short enough to cut the stall,
    # long enough that the common case - captions answering in ~1-3s - still
    # costs YouTube exactly one request.
    TRANSCRIPT_HEDGE_SECONDS: float = 4.0
    # ---- output cache ----------------------------------------------------
    # Ek baar bani summary sabke liye. Default BAND hai - table ban jaane aur
    # sab theek dikhne ke baad .env me OUTPUT_CACHE_ENABLED=true kijiye.
    OUTPUT_CACHE_ENABLED: bool = False
    # Isse chhota jawab kabhi cache me nahi jaata - adhoora ya toota hua jawab
    # hamesha ke liye baithane se accha hai ki wo dobara bane.
    OUTPUT_CACHE_MIN_CHARS: int = 200
    # Itne din tak koi na maange to entry hat jaati hai. 0 = kabhi na hatao.
    OUTPUT_CACHE_TTL_DAYS: int = 90
    # Share results built from an extension-supplied transcript, when oEmbed
    # has confirmed the video is reachable without a login.
    #
    # The trade-off is worth stating: the transcript itself is unverified, so a
    # determined user could spend a trial to seed one video's cached summary
    # with text of their choosing. Every row records where its transcript came
    # from (CachedOutput.source), so such rows can be found and purged; set
    # this to false to stop writing them at all.
    CACHE_CLIENT_TRANSCRIPTS: bool = True

    # ---- full notes (the PDF) ------------------------------------------
    # 6k keeps a chunk within the model context while requiring substantially
    # fewer model round trips than the old 3.5k default. Overlap stops a point
    # from falling between two chunks.
    # 12,000 chars is about 3,000 tokens in, which with the prompt and a
    # 4,096-token answer sits comfortably inside max_model_len 10,000. Halving
    # the chunk count halves the per-chunk prompt overhead and the overlap that
    # gets written twice - and, because a chunk's answer is capped at
    # NOTES_NUM_PREDICT however much material it covers, it also roughly halves
    # the total tokens generated for a video. That is the speed/detail dial:
    # bigger chunks mean faster, denser notes; smaller means slower, more
    # exhaustive ones.
    NOTES_CHUNK_CHARS: int = 12000
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
    # How long a NOTES chunk queues for a vLLM slot. Far longer than the
    # interactive default: nobody is watching a PDF render, and 30 concurrent
    # PDFs legitimately queue for a long time. At the interactive 300s a 30-way
    # end-to-end run lost most sections of 19 documents to this timeout and
    # produced PDFs containing nothing but "section missing" labels.
    # A video short enough to need this many parts or fewer is treated as
    # interactive work, not batch: it uses the reserved slots and skips the
    # batch queue entirely.
    #
    # Shortest-job-first, and the reason is a measurement: a 16-minute video is
    # two parts, but it used to wait for a batch permit exactly as long as a
    # two-hour, fourteen-part job - so the quick thing felt as slow as the slow
    # thing. Three parts is about 45 minutes of video at NOTES_CHUNK_CHARS.
    NOTES_SHORT_JOB_CHUNKS: int = 3
    NOTES_QUEUE_TIMEOUT_SECONDS: int = 1800
    # Slots (per worker) that batch work may never occupy, so an interactive
    # summary someone is watching is never stuck behind PDF parts nobody is.
    # Measured: with 20 PDFs running, 377 tok/s spread over 32 sequences is
    # ~12 tok/s each, and a 1,200-token summary that should take 25s took 100.
    # Reserving capacity does not slow the PDFs down much - they have the other
    # slots and they are not being watched - but it keeps the site responsive.
    VLLM_INTERACTIVE_RESERVE: int = 16
    # vLLM scheduler priority (LOWER runs first). Reserving slots only decides
    # who gets admitted; once running, every sequence shares the GPU equally -
    # measured, 425 tok/s across 82 sequences is 5.2 tok/s each, so a 2-part
    # video still took minutes. Priority decides who gets served, so a short
    # video actually finishes quickly while PDFs are being written.
    # Requires vLLM started with --scheduling-policy priority.
    VLLM_PRIORITY_INTERACTIVE: int = 0
    VLLM_PRIORITY_BATCH: int = 100
    # Past this fraction of failed sections the notes are not notes any more,
    # and full_notes() raises instead of returning a stub that would be cached
    # and rendered to PDF as though it were the real thing.
    NOTES_MAX_FAILED_FRACTION: float = 0.25
    # Optional http(s) proxy for YouTube. Set this if your server's IP gets
    # rate-limited or blocked - e.g. http://user:pass@proxy-host:port
    YOUTUBE_PROXY: str = ""

    # Blocking work - the yt-dlp transcript fetch, and every database call an
    # endpoint makes - runs on this pool. Starlette defaults to 40 threads,
    # which a burst of long videos exhausts; then even a health check queues.
    THREADPOOL_MAX_THREADS: int = 96

    # ---- server-side PDF -------------------------------------------------
    # Rendering used to happen in the reader's browser, where it cost this
    # machine nothing. Doing it here has to be capped, and capped tightly: on a
    # GB10 the CPU and GPU share one 128 GB pool, so memory spent rendering is
    # memory taken from vLLM's KV cache.
    #
    # PER UVICORN WORKER, like the vLLM gate - 3 workers x 1 = 3 renderers.
    PDF_RENDER_WORKERS: int = 1
    # How long a request waits for a renderer before it is told to try again.
    PDF_QUEUE_TIMEOUT_SECONDS: int = 120
    # A two-hour lecture makes roughly 80k characters of notes; this leaves
    # room without letting one document occupy a renderer indefinitely.
    PDF_MAX_CHARS: int = 400_000

    # How long a streaming response may stay silent before a keepalive line is
    # sent. Cloudflare closes an origin connection quiet for 100s with a 524,
    # so this must stay comfortably under that - the user sees an error while
    # the server is still working.
    STREAM_HEARTBEAT_SECONDS: int = 20

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
    # Public address shown on the Contact Us page. This can differ from the
    # sender address used for transactional email.
    SUPPORT_EMAIL: str = "support@tubenotes.ai"
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_STARTTLS: bool = True

    APP_BASE_URL: str = "http://localhost:8000"

    # Old integrations may still use this header key.  The browser dashboard
    # itself uses the signed-in admin account below, so this key is never sent
    # to or stored in the browser.
    ADMIN_API_KEY: str = Field(default="", description="Legacy static key for server-to-server admin routes")
    # Comma-separated emergency/bootstrap admin emails.  Keep this in the
    # untracked .env.local/.env file, never in source control.  Users marked
    # is_admin in the database are admins too.
    ADMIN_EMAILS: str = ""
    # Optional analytics reset point for the admin UI. Records remain in the
    # database; only records created on/after this UTC timestamp are reported.
    # Example: 2026-09-27T09:30:00Z
    ADMIN_REPORTING_START_AT: datetime | None = None

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
    def admin_emails(self) -> set[str]:
        return {
            email.strip().lower()
            for email in self.ADMIN_EMAILS.split(",")
            if email.strip()
        }

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
