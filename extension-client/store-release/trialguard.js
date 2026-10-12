/**
 * trialguard.js — accounts, 20 free videos, then $5/month.
 *
 * Talks to the same backend the web app uses, so a user's credits are shared:
 * 20 free videos in total across the extension and the website, one account.
 *
 * Written as a CLASSIC script (no import/export) so the same file can be loaded
 * with <script src> in the panel and popup, and with importScripts() in
 * Chrome's service worker / as a plain background script in Firefox.
 * Everything lives on  self.TrialGuard.
 *
 * WHY NOT A MAC ADDRESS
 * Neither Chrome nor Firefox exposes network hardware to an extension, so a MAC
 * address cannot be read here. We send the strongest identifier the browser
 * allows - a stored install id plus stable hardware traits - and the SERVER
 * decides. The server also keeps a hardware-only ledger, so clearing extension
 * storage does not hand out a fresh set of free videos.
 */
(function () {
  "use strict";

  const api = typeof browser !== "undefined" ? browser : chrome;

  // Overridable at runtime from the Options page by storing
  // `trialGuardApiUrl` in storage.sync.
  // The live server. 127.0.0.1 would only ever mean "the machine this browser
  // is running on", so it can never work for anyone but the developer.
  // Canonical production API. Older extension releases saved the
  // trueworks.in URL, so normalise that value below instead of letting an old
  // browser setting keep calls on the retired host.
  const LEGACY_API_BASE = "https://tubenotes.trueworks.in/api/v1";
  const DEFAULT_API_BASE = "https://tubenotes.ai/api/v1";

  function normaliseApiBase(value) {
    const base = String(value || "").replace(/\/+$/, "");
    return base === LEGACY_API_BASE ? DEFAULT_API_BASE : base;
  }

  /**
   * On Windows "localhost" usually resolves to ::1 (IPv6) first, while uvicorn
   * listens on 127.0.0.1 only - the fetch then fails with "Failed to fetch"
   * even though the server is running. So a local URL gets the other spelling
   * as a fallback.
   *
   * This ONLY applies to local URLs. The live server never falls back to
   * localhost: if it did, and the live server were down, the extension would
   * post the user's email and password to whatever happens to be listening on
   * port 8000 of their own machine.
   */
  function siblingsOf(base) {
    const m = /^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?(\/.*)?$/.exec(base);
    if (!m) return [];
    const other = m[1] === "localhost" ? "127.0.0.1" : "localhost";
    return [`http://${other}${m[2] || ""}${m[3] || ""}`];
  }

  const TOKENS_KEY = "tg_tokens";
  const DEVICE_KEY = "tg_device";
  const CACHE_KEY = "tg_entitlement";
  const BASE_KEY = "tg_api_base_ok";

  let apiBase = DEFAULT_API_BASE;
  let refreshInFlight = null;

  // Every request awaits this, otherwise the very first call can go out with
  // the default URL before the stored one has been read back.
  const baseReady = (async () => {
    try {
      const s = await api.storage.sync.get("trialGuardApiUrl");
      if (s && s.trialGuardApiUrl) {
        const savedBase = String(s.trialGuardApiUrl).replace(/\/+$/, "");
        apiBase = normaliseApiBase(savedBase);
        if (apiBase !== savedBase) {
          await api.storage.sync.set({ trialGuardApiUrl: apiBase });
        }
        return;
      }
    } catch (_) {}
    try {
      const remembered = await get(BASE_KEY);
      if (remembered) {
        apiBase = normaliseApiBase(remembered);
        if (apiBase !== remembered) {
          await api.storage.local.set({ [BASE_KEY]: apiBase });
        }
      }
    } catch (_) {}
  })();

  function NetworkError(tried, cause) {
    const local = /\/\/(localhost|127\.0\.0\.1)/.test(tried[0]);
    const hint = local
      ? 'Start it with "uvicorn app.main:app --reload" and keep that terminal open.'
      : "The server may be down or you may be offline. Please try again in a minute.";
    const e = new Error(
      `Cannot reach the server at ${tried.join(" or ")}. ${hint} ` +
        `(${(cause && cause.message) || cause})`
    );
    e.name = "NetworkError";
    e.tried = tried;
    return e;
  }

  /**
   * fetch() that survives the localhost/127.0.0.1 split. A *network* failure
   * (server down, wrong host) throws TypeError; an HTTP error does not, so we
   * only ever retry on the former - a 401 or 402 is a real answer.
   */
  async function baseFetch(path, init) {
    await baseReady;
    const candidates = [apiBase, ...siblingsOf(apiBase)];
    let last = null;
    for (const base of candidates) {
      try {
        const res = await fetch(`${base}${path}`, init);
        if (base !== apiBase) {
          apiBase = base;
          try {
            await api.storage.local.set({ [BASE_KEY]: base });
          } catch (_) {}
        }
        return res;
      } catch (e) {
        last = e;
      }
    }
    throw NetworkError(candidates, last);
  }

  // ---- helpers -------------------------------------------------------
  function uuid() {
    if (crypto.randomUUID) return crypto.randomUUID();
    const b = crypto.getRandomValues(new Uint8Array(16));
    b[6] = (b[6] & 15) | 64;
    b[8] = (b[8] & 63) | 128;
    const h = [...b].map((x) => x.toString(16).padStart(2, "0")).join("");
    return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
  }

  async function get(key) {
    const s = await api.storage.local.get(key);
    return s[key] || null;
  }

  // ---- device fingerprint --------------------------------------------
  const hasDocument = typeof document !== "undefined" && typeof screen !== "undefined";

  function readGpu() {
    try {
      if (!hasDocument) return null;
      const canvas = document.createElement("canvas");
      const gl = canvas.getContext("webgl") || canvas.getContext("experimental-webgl");
      const ext = gl && gl.getExtension("WEBGL_debug_renderer_info");
      return ext ? String(gl.getParameter(ext.UNMASKED_RENDERER_WEBGL)).slice(0, 200) : null;
    } catch (_) {
      return null;
    }
  }

  function readBrand() {
    const brands = navigator.userAgentData && navigator.userAgentData.brands;
    if (Array.isArray(brands)) {
      return brands.map((b) => `${b.brand} ${b.version}`).join("|").slice(0, 200);
    }
    return String(navigator.userAgent || "").slice(0, 200);
  }

  /**
   * `screen` and the WebGL renderer only exist in a page context, never in the
   * background worker. If the worker recomputed the fingerprint it would send a
   * DIFFERENT hash than the panel and the server would count one machine as two
   * devices. So a page computes the traits once, caches them, and every context
   * sends that same cached set.
   */
  async function getDevice() {
    let rec = await get(DEVICE_KEY);
    if (!rec || !rec.installation_id) {
      rec = { installation_id: uuid(), created_at: Date.now() };
    }

    if (hasDocument || !rec.traits) {
      const traits = {
        platform:
          navigator.platform ||
          (navigator.userAgentData && navigator.userAgentData.platform) ||
          null,
        user_agent_brand: readBrand(),
        screen: hasDocument ? `${screen.width}x${screen.height}x${screen.colorDepth}` : null,
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || null,
        language: navigator.language || null,
        hardware_concurrency: navigator.hardwareConcurrency || null,
        device_memory: navigator.deviceMemory || null,
        gpu: readGpu() || (rec.traits && rec.traits.gpu) || null,
      };
      if (hasDocument || !rec.traits) rec.traits = traits;
      await api.storage.local.set({ [DEVICE_KEY]: rec });
    }

    const fp = Object.assign({}, rec.traits, {
      installation_id: rec.installation_id,
      extension_version: api.runtime.getManifest().version,
    });

    const out = {};
    for (const k of Object.keys(fp)) {
      if (fp[k] !== null && fp[k] !== undefined && fp[k] !== "") out[k] = fp[k];
    }
    return out;
  }

  // ---- tokens ---------------------------------------------------------
  async function setTokens(t) {
    await api.storage.local.set({
      [TOKENS_KEY]: {
        access_token: t.access_token,
        refresh_token: t.refresh_token,
        expires_at: Date.now() + (t.expires_in - 60) * 1000, // refresh a minute early
      },
    });
  }

  async function clearTokens() {
    await api.storage.local.remove([TOKENS_KEY, CACHE_KEY]);
  }

  async function isSignedIn() {
    return (await get(TOKENS_KEY)) !== null;
  }

  async function refreshTokens() {
    // Single-flight: a rotated refresh token is single-use, so two parallel
    // refreshes would revoke the family and sign the user out.
    if (refreshInFlight) return refreshInFlight;
    refreshInFlight = (async () => {
      const t = await get(TOKENS_KEY);
      if (!t || !t.refresh_token) return null;
      try {
        const res = await baseFetch(`/auth/refresh`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ refresh_token: t.refresh_token }),
        });
        if (!res.ok) {
          await clearTokens();
          return null;
        }
        const fresh = await res.json();
        await setTokens(fresh);
        return fresh;
      } catch (_) {
        return null;
      } finally {
        refreshInFlight = null;
      }
    })();
    return refreshInFlight;
  }

  async function accessToken() {
    const t = await get(TOKENS_KEY);
    if (!t) return null;
    if (Date.now() < t.expires_at) return t.access_token;
    const fresh = await refreshTokens();
    return fresh ? fresh.access_token : null;
  }

  // ---- HTTP -----------------------------------------------------------
  function ApiError(status, detail) {
    const msg =
      typeof detail === "string" ? detail : (detail && detail.message) || "Request failed";
    const e = new Error(msg);
    e.name = "ApiError";
    e.status = status;
    e.detail = detail;
    e.needsSubscription = status === 402;
    e.entitlement = (detail && detail.entitlement) || null;
    return e;
  }

  async function request(path, opts) {
    opts = opts || {};
    const headers = Object.assign({
      "Content-Type": "application/json",
      "X-TubeNotes-Client": "extension",
    }, opts.headers || {});

    if (opts.auth !== false) {
      const tok = await accessToken();
      if (!tok) throw ApiError(401, "Not signed in");
      headers.Authorization = `Bearer ${tok}`;
    }

    const init = {
      method: opts.method || "GET",
      headers,
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
    };

    let res = await baseFetch(path, init);
    if (res.status === 401 && opts.auth !== false) {
      const fresh = await refreshTokens();
      if (fresh) {
        headers.Authorization = `Bearer ${fresh.access_token}`;
        res = await baseFetch(path, init);
      }
    }

    const text = await res.text();
    let json = null;
    try {
      json = text ? JSON.parse(text) : null;
    } catch (_) {}

    if (!res.ok) {
      if (res.status === 401) await clearTokens();
      throw ApiError(res.status, json ? json.detail : text);
    }
    return json;
  }

  /**
   * Stream an authenticated NDJSON endpoint. TrialGuard's vLLM-backed
   * /summarize and /notes routes both use this format so the extension can
   * paint tokens/progress as soon as the backend receives them.
   */
  async function streamNdjson(path, body, onEvent) {
    async function open(token) {
      return baseFetch(path, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-TubeNotes-Client": "extension",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify(body || {}),
      });
    }

    let token = await accessToken();
    if (!token) throw ApiError(401, "Not signed in");
    let res = await open(token);
    if (res.status === 401) {
      const fresh = await refreshTokens();
      if (fresh) {
        token = fresh.access_token;
        res = await open(token);
      }
    }
    if (!res.ok) {
      const raw = await res.text();
      let detail = raw;
      try {
        const parsed = raw ? JSON.parse(raw) : null;
        detail = parsed && parsed.detail !== undefined ? parsed.detail : parsed;
      } catch (_) {}
      if (res.status === 401) await clearTokens();
      throw ApiError(res.status, detail || `HTTP ${res.status}`);
    }

    if (!res.body || !res.body.getReader) {
      throw new Error("Streaming response is not supported by this browser.");
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    let doneEvent = null;

    const consumeLine = async (line) => {
      const clean = String(line || "").trim();
      if (!clean) return;
      let ev;
      try { ev = JSON.parse(clean); } catch (_) { return; }
      if (onEvent) await onEvent(ev);
      if (ev.type === "error") throw new Error(ev.message || "Generation failed");
      if (ev.type === "done") doneEvent = ev;
    };

    while (true) {
      const part = await reader.read();
      if (part.done) break;
      buf += decoder.decode(part.value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl);
        buf = buf.slice(nl + 1);
        await consumeLine(line);
      }
    }
    buf += decoder.decode();
    if (buf.trim()) await consumeLine(buf);
    return doneEvent;
  }

  async function streamSummary(opts) {
    opts = opts || {};
    const device = await getDevice();
    return streamNdjson("/summarize", {
      url: opts.url,
      device,
      mode: opts.mode || "summary",
      target_lang: opts.targetLang || null,
      transcript: opts.transcript || null,
      transcript_lang: opts.transcriptLang || null,
    }, opts.onEvent);
  }

  async function streamNotes(opts) {
    opts = opts || {};
    const device = await getDevice();
    return streamNdjson("/notes", {
      url: opts.url,
      device,
      target_lang: opts.targetLang || null,
      transcript: opts.transcript || null,
      transcript_lang: opts.transcriptLang || null,
    }, opts.onEvent);
  }

  async function markPdfReady(jobId) {
    if (!jobId) return null;
    return request("/jobs/pdf-ready", { method: "POST", body: { job_id: jobId } });
  }

  async function recordExtensionActivity(activity) {
    return request("/extension/activity", { method: "POST", body: activity });
  }

  // Keep all user-content processing behind the authenticated TubeNotes API.
  // The extension never calls a third-party translation endpoint directly.
  async function translateText(text, targetLang) {
    return request("/translate", {
      method: "POST",
      body: { text: String(text || ""), target_lang: targetLang },
    });
  }

  // ---- entitlement ----------------------------------------------------
  async function cacheEntitlement(ent) {
    await api.storage.local.set({ [CACHE_KEY]: { entitlement: ent, fetched_at: Date.now() } });
  }

  async function getCachedEntitlement() {
    const c = await get(CACHE_KEY);
    return c ? c.entitlement : null;
  }

  async function checkEntitlement() {
    const device = await getDevice();
    const ent = await request("/entitlement/check", { method: "POST", body: { device } });
    await cacheEntitlement(ent);
    return ent;
  }

  /**
   * Spend one unit of entitlement.
   *
   * The key makes the charge idempotent. We pass the YouTube video id, so the
   * summary, the follow-up chat and the PDF for ONE video all cost one credit,
   * and re-opening that video later is free forever.
   */
  async function consume(key, action, meta) {
    const device = await getDevice();
    const result = await request("/usage/consume", {
      method: "POST",
      headers: { "Idempotency-Key": key },
      body: { device, action: action || "summarize", meta: meta || null, idempotency_key: key },
    });
    await cacheEntitlement(result.entitlement);
    return result;
  }

  // ---- auth -----------------------------------------------------------
  async function signup(email, password, fullName) {
    const device = await getDevice();
    const data = await request("/auth/signup", {
      auth: false,
      method: "POST",
      body: { email, password, full_name: fullName || null, device },
    });
    await setTokens(data.tokens);
    await cacheEntitlement(data.entitlement);
    return data;
  }

  async function login(email, password) {
    const device = await getDevice();
    const data = await request("/auth/login", {
      auth: false,
      method: "POST",
      body: { email, password, device },
    });
    await setTokens(data.tokens);
    await cacheEntitlement(data.entitlement);
    return data;
  }

  /** Google OAuth for Manifest V3 extensions.
   * Uses chrome.identity so no remote Google JS is injected into the extension.
   * The backend still verifies Google's signed ID token and owns the session.
   */
  async function googleLogin() {
    if (!api.identity || !api.identity.launchWebAuthFlow) {
      throw new Error("Google sign-in is not supported by this browser build.");
    }

    const meta = await request("/meta", { auth: false });
    if (!meta || !meta.google_login || !meta.google_client_id) {
      throw new Error("Google sign-in is not enabled on the account server.");
    }

    const redirectUri = api.identity.getRedirectURL("google");
    const nonceBytes = crypto.getRandomValues(new Uint8Array(24));
    const nonce = Array.from(nonceBytes, (b) => b.toString(16).padStart(2, "0")).join("");
    const state = uuid();
    const authUrl = new URL("https://accounts.google.com/o/oauth2/v2/auth");
    authUrl.searchParams.set("client_id", meta.google_client_id);
    authUrl.searchParams.set("redirect_uri", redirectUri);
    authUrl.searchParams.set("response_type", "id_token");
    authUrl.searchParams.set("scope", "openid email profile");
    authUrl.searchParams.set("nonce", nonce);
    authUrl.searchParams.set("state", state);
    authUrl.searchParams.set("prompt", "select_account");

    const callbackUrl = await api.identity.launchWebAuthFlow({
      url: authUrl.toString(),
      interactive: true,
    });
    if (!callbackUrl) throw new Error("Google sign-in was cancelled.");

    const parsed = new URL(callbackUrl);
    const params = new URLSearchParams((parsed.hash || "").replace(/^#/, ""));
    if (params.get("state") !== state) throw new Error("Google sign-in state check failed.");
    const oauthError = params.get("error");
    if (oauthError) throw new Error(params.get("error_description") || oauthError);
    const credential = params.get("id_token");
    if (!credential) throw new Error("Google did not return an ID token.");

    const device = await getDevice();
    const data = await request("/auth/google", {
      auth: false,
      method: "POST",
      body: { credential, device },
    });
    await setTokens(data.tokens);
    await cacheEntitlement(data.entitlement);
    return data;
  }

  async function logout() {
    const t = await get(TOKENS_KEY);
    try {
      if (t) {
        await request("/auth/logout", {
          method: "POST",
          body: { refresh_token: t.refresh_token, all_devices: false },
        });
      }
    } catch (_) {
    } finally {
      await clearTokens();
    }
  }

  const me = () => request("/auth/me");
  const forgotPassword = (email) =>
    request("/auth/password/forgot", { auth: false, method: "POST", body: { email } });
  const getSubscription = () => request("/billing/subscription");
  // Pricing is selected by the server from the visitor's country. Do not
  // hard-code USD in the extension: the website and extension must show the
  // same tier.
  const getBillingPrice = () => request("/billing/price", { auth: false });
  const getLaunchOffer = () => request("/billing/offers/launch");
  const claimLaunchOffer = () =>
    request("/billing/offers/launch/claim", { method: "POST", body: {} });

  async function startCheckout(couponCode) {
    const code = typeof couponCode === "string" ? couponCode.trim() : "";
    const session = await request("/billing/checkout", {
      method: "POST",
      body: code ? { coupon_code: code } : {},
    });
    await api.tabs.create({ url: session.checkout_url });
    return session;
  }

  async function openBillingPortal() {
    const res = await request("/billing/portal", { method: "POST" });
    await api.tabs.create({ url: res.detail });
    return res.detail;
  }

  /**
   * The one call the panel makes before summarizing.
   * Returns { ok: true, entitlement } or
   *         { ok: false, reason, message, entitlement }.
   * reason: 'signin_required' | 'subscription_required' | 'unavailable'
   */
  async function requireAccess(videoId) {
    if (!(await isSignedIn())) {
      return {
        ok: false,
        reason: "signin_required",
        message: "Sign in to start summarizing. New accounts get 20 free videos.",
      };
    }
    try {
      const r = await consume(`video:${videoId}`, "summarize_video", { video_id: videoId });
      return { ok: true, entitlement: r.entitlement, duplicate: r.duplicate };
    } catch (err) {
      if (err.needsSubscription) {
        return {
          ok: false,
          reason: "subscription_required",
          message: err.message,
          entitlement: err.entitlement,
        };
      }
      if (err.status === 401 || err.status === 403) {
        return { ok: false, reason: "signin_required", message: "Please sign in again." };
      }
      // Network or server problem. Fail CLOSED on purpose: anyone who can block
      // your API must not thereby get unlimited free summaries.
      return {
        ok: false,
        reason: "unavailable",
        message: "Can't reach the account server. Please try again in a moment.",
      };
    }
  }

  /** Toolbar badge: how many free videos are left. */
  async function refreshBadge() {
    let ent = await getCachedEntitlement();
    try {
      if (await isSignedIn()) ent = await checkEntitlement();
    } catch (_) {}

    const action = api.action || api.browserAction;
    if (!action) return;
    try {
      if (!(await isSignedIn())) {
        await action.setBadgeText({ text: "•" });
        await action.setBadgeBackgroundColor({ color: "#8a5cf0" });
        return;
      }
      if (!ent) return;
      if (ent.plan === "subscription") {
        await action.setBadgeText({ text: "" });
        return;
      }
      const left = Math.min(ent.trials_remaining, ent.device_trials_remaining);
      await action.setBadgeText({ text: left > 0 ? String(left) : "!" });
      await action.setBadgeBackgroundColor({ color: left > 0 ? "#1a73e8" : "#d93025" });
    } catch (_) {}
  }

  function setApiBase(url) {
    apiBase = normaliseApiBase(url);
  }

  /** Used by the popup's "Test connection" button. Never throws. */
  async function ping() {
    await baseReady;
    const candidates = [apiBase, ...siblingsOf(apiBase)];
    for (const base of candidates) {
      try {
        const res = await fetch(`${base.replace(/\/api\/v1$/, "")}/healthz`);
        if (res.ok) {
          apiBase = base;
          try {
            await api.storage.local.set({ [BASE_KEY]: base });
          } catch (_) {}
          return { ok: true, base };
        }
      } catch (_) {}
    }
    return { ok: false, tried: candidates };
  }

  (typeof self !== "undefined" ? self : globalThis).TrialGuard = {
    // background.js needs this to authenticate Ollama calls, which now go
    // through the backend instead of straight at an open server.
    accessToken,
    isSignedIn,
    signup,
    login,
    googleLogin,
    logout,
    me,
    forgotPassword,
    getDevice,
    checkEntitlement,
    getCachedEntitlement,
    consume,
    requireAccess,
    getSubscription,
    getBillingPrice,
    getLaunchOffer,
    claimLaunchOffer,
    startCheckout,
    openBillingPortal,
    refreshBadge,
    setApiBase,
    ping,
    streamNdjson,
    streamSummary,
    streamNotes,
    markPdfReady,
    recordExtensionActivity,
    translateText,
    get apiBase() {
      return apiBase;
    },
  };
})();
