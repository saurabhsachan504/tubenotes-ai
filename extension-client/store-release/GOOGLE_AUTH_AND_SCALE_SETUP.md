# TubeNotes extension — Google Auth + shared scalable summarizer

This build uses the same TrialGuard backend as the web app for account/session,
trial/subscription checks, summaries and full notes. The extension does not need
a public vLLM/model port for summarization.

## 1. Backend production settings

In the TrialGuard backend `.env` set at minimum:

```env
GOOGLE_LOGIN_ENABLED=true
GOOGLE_CLIENT_ID=YOUR_GOOGLE_OAUTH_WEB_CLIENT_ID.apps.googleusercontent.com
DATABASE_URL=postgresql+psycopg://...
ALLOWED_ORIGINS=*
# Optional after the extension ID is stable:
# ALLOWED_EXTENSION_IDS=YOUR_CHROME_EXTENSION_ID
```

Keep the existing production vLLM queue/concurrency settings from
`.env.prod.example`. `VLLM_MAX_CONCURRENCY` must match the capacity of your
vLLM deployment; excess simultaneous requests wait in the backend queue rather
than all being fired directly at the GPU.

Do not use SQLite for a multi-worker production deployment. Use Postgres.

## 2. Google Cloud OAuth configuration

Create/use an OAuth 2.0 Client ID of type **Web application**. Use the SAME
client ID in the TrialGuard backend `GOOGLE_CLIENT_ID`.

After loading the unpacked extension, copy its Chrome extension ID from
`chrome://extensions`. Add this redirect URI to the OAuth client:

```
https://EXTENSION_ID.chromiumapp.org/google
```

The extension obtains a Google ID token through `chrome.identity.launchWebAuthFlow`
and sends that ID token to `/api/v1/auth/google`. The backend verifies Google's
signature, issuer, expiry and audience before creating/logging in the user.

## 3. Install/test

1. Deploy the TrialGuard backend and run its database migrations.
2. Confirm `https://tubenotes.ai/api/v1/meta` reports
   `google_login: true` and the expected client ID.
3. Open `chrome://extensions`, enable Developer mode, choose **Load unpacked**
   and select this extension folder.
4. Configure the OAuth redirect URI using the extension ID as described above.
5. Click the TubeNotes toolbar icon and choose **Continue with Google**.
6. Summarize a YouTube video. The request streams through
   `/api/v1/summarize`; full notes stream through `/api/v1/notes`.

## Capacity note

The code can accept many simultaneous extension users and queue model work, but
"100 users at exactly the same instant" does not mean 100 generations should be
run on the GPU at once. Actual throughput depends on GPU(s), vLLM
`--max-num-seqs`, model size, context length and output length. Set backend
`VLLM_MAX_CONCURRENCY` to measured GPU capacity and let the queue absorb bursts.
For multiple API workers/hosts, use Postgres and size the total worker
concurrency so it does not exceed the model server's real capacity.
