// transcript.js — client-side YouTube transcript scraper (robust panel opener).
//
// The browser, being a logged-in YouTube client, already has the transcript in
// the "Show transcript" panel. Scraping it here is near-instant and never hits
// YouTube's server-side rate-limits/blocks. The tricky part is OPENING the
// transcript panel when it isn't already open — YouTube's UI varies, so this
// tries several strategies and only clicks VISIBLE controls, verifying after
// each attempt that transcript segments actually appeared.
//
// Responds to { type: "GET_TRANSCRIPT", videoId } from the side panel. Returns
// the plain transcript text, or null (→ caller falls back to server-side).

(() => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const log = (...a) => console.log("[ONS-t]", ...a);
  log("transcript.js v1.7.92 loaded");

  // ============ Caption fetch for ANY video (no need to open it) ============
  // Fetches a video's captions directly from YouTube (same-origin), so a
  // thumbnail on the home/search page can be summarised WITHOUT navigating to
  // the video. Works for any videoId from any YouTube page.
  function matchBraces(s, start) {
    let depth = 0, inStr = false, esc = false;
    for (let j = start; j < s.length; j++) {
      const c = s[j];
      if (inStr) { if (esc) esc = false; else if (c === "\\") esc = true; else if (c === '"') inStr = false; }
      else if (c === '"') inStr = true;
      else if (c === "{") depth++;
      else if (c === "}") { if (--depth === 0) return s.slice(start, j + 1); }
    }
    return null;
  }
  function extractPlayerResponse(text) {
    const marker = "ytInitialPlayerResponse";
    let i = text.indexOf(marker);
    while (i >= 0) {
      const brace = text.indexOf("{", i);
      if (brace < 0) return null;
      const obj = matchBraces(text, brace);
      if (obj) { try { return JSON.parse(obj); } catch (_) {} }
      i = text.indexOf(marker, i + marker.length);
    }
    return null;
  }
  function playerResponseFromDom(videoId) {
    for (const s of document.querySelectorAll("script")) {
      const txt = s.textContent || "";
      if (txt.indexOf("ytInitialPlayerResponse") < 0) continue;
      const pr = extractPlayerResponse(txt);
      if (pr && pr.videoDetails && pr.videoDetails.videoId === videoId) return pr;
    }
    return null;
  }
  function pickCaptionTrack(tracks) {
    if (!tracks || !tracks.length) return null;
    // The AUTO (ASR) track is ALWAYS in the video's SPOKEN language. A manual
    // caption can be a DIFFERENT language the creator added (e.g. Hindi subtitles
    // on a Punjabi video). Old logic "manual first" therefore picked the wrong
    // language. Fix: use the ASR track's language as the spoken language, and
    // prefer a caption in THAT language (manual if available, else the ASR).
    const asr = tracks.find((t) => t.kind === "asr");
    const spoken = asr && String(asr.languageCode || "").split("-")[0].toLowerCase();
    if (spoken) {
      const manualSame = tracks.find(
        (t) => t.kind !== "asr" && String(t.languageCode || "").split("-")[0].toLowerCase() === spoken
      );
      return manualSame || asr;
    }
    // No ASR info → best-effort: a manual track, else the first.
    return tracks.find((t) => t.kind !== "asr") || tracks[0];
  }
  function parseJson3(data) {
    if (!data || !data.events) return "";
    return data.events.flatMap((e) => (e.segs || []).map((s) => s.utf8 || "")).join("").replace(/\s+/g, " ").trim();
  }
  function decodeEntities(s) { const t = document.createElement("textarea"); t.innerHTML = s; return t.value; }
  function parseXmlCaptions(xml) {
    try {
      const doc = new DOMParser().parseFromString(xml, "text/xml");
      return [...doc.querySelectorAll("text")].map((n) => decodeEntities(n.textContent || "")).join(" ").replace(/\s+/g, " ").trim();
    } catch (_) { return ""; }
  }
  function getInnertube() {
    let key = null, ver = null, visitorData = null;
    for (const s of document.querySelectorAll("script")) {
      const t = s.textContent || "";
      if (t.indexOf("INNERTUBE") < 0 && t.indexOf("clientVersion") < 0 && t.indexOf("visitorData") < 0) continue;
      if (!key) { const m = t.match(/"INNERTUBE_API_KEY":"([^"]+)"/); if (m) key = m[1]; }
      if (!ver) { const m = t.match(/"INNERTUBE_CLIENT_VERSION":"([^"]+)"/) || t.match(/"clientVersion":"([^"]+)"/); if (m) ver = m[1]; }
      if (!visitorData) { const m = t.match(/"visitorData":"([^"]+)"/) || t.match(/"VISITOR_DATA":"([^"]+)"/); if (m) visitorData = m[1]; }
      if (key && ver && visitorData) break;
    }
    if (!key) return null;
    return { key, ver: ver || "2.20240710.01.00", visitorData };
  }
  const WEB_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8";
  const ANDROID_KEY = "AIzaSyA8eiZmM1FaDVjRy-df2KTyQ_vz_yYM39w";
  const IOS_KEY = "AIzaSyB-63vPrdThhKuerbB2N_l7Kwwcxj6yUAc";
  const INNERTUBE_CLIENTS = [
    { key: null, ctx: { clientName: "WEB", clientVersion: null } },
    { key: ANDROID_KEY, ctx: { clientName: "ANDROID", clientVersion: "19.09.37", androidSdkVersion: 30 } },
    { key: IOS_KEY, ctx: { clientName: "IOS", clientVersion: "19.09.3", deviceModel: "iPhone14,3" } },
    { key: null, ctx: { clientName: "MWEB", clientVersion: "2.20240726.01.00" } },
  ];
  async function fetchPlayerViaInnertube(videoId, client) {
    const cfg = getInnertube();
    if (!cfg) return null;
    const key = client.key || cfg.key || WEB_KEY;
    const c = Object.assign({ hl: "en", gl: "US" }, client.ctx);
    if (!c.clientVersion) c.clientVersion = cfg.ver;
    if (cfg.visitorData) c.visitorData = cfg.visitorData;
    try {
      const res = await fetch(`https://www.youtube.com/youtubei/v1/player?key=${key}`, {
        method: "POST", credentials: "include", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ context: { client: c }, videoId, contentCheckOk: true, racyCheckOk: true }),
      });
      if (!res.ok) return null;
      return await res.json();
    } catch (_) { return null; }
  }
  function setFmt(url, fmt) {
    let u = url.replace(/([?&])fmt=[^&]*/g, "$1").replace(/[?&]$/, "");
    return u + (u.indexOf("?") >= 0 ? "&" : "?") + "fmt=" + fmt;
  }
  async function captionsFromPlayer(pr) {
    const tracks = pr && pr.captions && pr.captions.playerCaptionsTracklistRenderer && pr.captions.playerCaptionsTracklistRenderer.captionTracks;
    if (!tracks || !tracks.length) return null;
    const track = pickCaptionTrack(tracks);
    if (!track || !track.baseUrl) return null;
    const base = track.baseUrl;
    try {
      const r = await fetch(setFmt(base, "json3"), { credentials: "include" });
      const body = await r.text();
      if (r.ok && body) { try { const txt = parseJson3(JSON.parse(body)); if (txt.length > 40) return txt; } catch (_) {} }
    } catch (_) {}
    try {
      const r = await fetch(base, { credentials: "include" });
      const body = await r.text();
      if (r.ok && body) { const txt = parseXmlCaptions(body); if (txt.length > 40) return txt; }
    } catch (_) {}
    return null;
  }
  async function fetchCaptionTranscript(videoId) {
    if (!videoId) return null;
    // (a) inline player response if it's this page's video
    const domPr = playerResponseFromDom(videoId);
    if (domPr) { const t = await captionsFromPlayer(domPr); if (t) return t; }
    // (b) watch-page HTML (same-origin, cookies)
    try {
      const res = await fetch(`https://www.youtube.com/watch?v=${videoId}&hl=en&bpctr=9999999999&has_verified=1`, { credentials: "include" });
      if (res.ok) { const t = await captionsFromPlayer(extractPlayerResponse(await res.text())); if (t) return t; }
    } catch (_) {}
    // (c) innertube player API across clients
    for (const client of INNERTUBE_CLIENTS) {
      const pr = await fetchPlayerViaInnertube(videoId, client);
      if (!pr) continue;
      const t = await captionsFromPlayer(pr);
      if (t) return t;
    }
    return null;
  }
  // =========================================================================

  // ---- Scrape the rendered transcript segments (new + classic panels) ----
  function grabTranscript() {
    let nodes = document.querySelectorAll(
      'transcript-segment-view-model span[role="text"]'
    );
    if (!nodes.length) {
      nodes = document.querySelectorAll(
        "ytd-transcript-segment-renderer .segment-text, " +
          "ytd-transcript-segment-renderer yt-formatted-string.segment-text"
      );
    }
    return [...nodes]
      .map((e) => (e.textContent || "").trim())
      .filter(Boolean)
      .join(" ")
      .replace(/\s+/g, " ")
      .trim();
  }

  function isVisible(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && el.offsetParent !== null;
  }

  const CLICKABLE =
    "button, tp-yt-paper-button, yt-button-shape, ytd-button-renderer, a, " +
    "ytd-menu-service-item-renderer, yt-formatted-string, tp-yt-paper-item, " +
    "yt-list-item-view-model, [role='menuitem'], [role='button']";

  // Controls whose (short) label mentions "transcript" — the actual "Show
  // transcript" buttons, not big containers that merely contain the word. By
  // default only visible ones; includeHidden=true also returns ones tucked
  // inside a collapsed description (offsetParent null) — clicking those often
  // still triggers the panel.
  function transcriptButtons(includeHidden) {
    const out = [];
    for (const el of document.querySelectorAll(CLICKABLE)) {
      const label = (el.getAttribute("aria-label") || el.textContent || "").trim();
      if (!label || label.length > 40) continue;
      if (!/transcript/i.test(label)) continue;
      if (includeHidden || isVisible(el)) out.push(el);
    }
    out.sort((a, b) => {
      const sa = /show transcript/i.test(a.getAttribute("aria-label") || a.textContent || "") ? 1 : 0;
      const sb = /show transcript/i.test(b.getAttribute("aria-label") || b.textContent || "") ? 1 : 0;
      return sb - sa;
    });
    return out;
  }

  // Expand the video description ("...more") so the "Show transcript" button
  // inside it becomes reachable. Tries several selectors + a text match.
  function expandDescription() {
    const sel = document.querySelector(
      "#description #expand, ytd-text-inline-expander #expand, " +
        "tp-yt-paper-button#expand, #description-inline-expander #expand, " +
        "#expand.ytd-text-inline-expander, #expand"
    );
    if (sel && isVisible(sel)) {
      try { sel.click(); } catch (_) {}
      return true;
    }
    // Fallback: any small control literally reading "...more" / "more".
    for (const el of document.querySelectorAll("tp-yt-paper-button, button, #expand, span")) {
      const t = (el.textContent || "").trim().toLowerCase();
      if ((t === "...more" || t === "…more" || t === "more") && isVisible(el)) {
        try { el.click(); } catch (_) {}
        return true;
      }
    }
    return false;
  }

  // True as soon as transcript segments render (waits up to `ms`).
  async function segmentsPresent(ms) {
    const start = performance.now();
    while (performance.now() - start < ms) {
      if (grabTranscript().length > 40) return true;
      await sleep(150);
    }
    return grabTranscript().length > 40;
  }

  // Try, in order, several ways to open the transcript panel. After each click
  // we wait briefly for segments; the first that works wins. Returns true if the
  // panel populated.
  async function openViaMoreMenu() {
    const more = [...document.querySelectorAll("button, yt-button-shape, ytd-button-renderer")].find(
      (el) => /more actions/i.test(el.getAttribute("aria-label") || "") && isVisible(el)
    );
    if (!more) return false;
    log("opening More actions menu");
    try { more.click(); } catch (_) {}
    await sleep(500);
    const item = [
      ...document.querySelectorAll(
        "ytd-menu-service-item-renderer, tp-yt-paper-item, yt-list-item-view-model, [role='menuitem']"
      ),
    ].find((el) => /transcript/i.test(el.textContent || "") && isVisible(el));
    if (item) {
      log("clicking transcript menu item");
      try { item.click(); } catch (_) {}
      return await segmentsPresent(2200);
    }
    try { document.body.click(); } catch (_) {} // close the menu
    return false;
  }

  // Fire every open-trigger at once, then do a SINGLE poll for segments. This
  // avoids waiting out each strategy's timeout in sequence — whichever trigger
  // works, the segments appear and we return immediately. Much faster than the
  // sequential approach while still trying everything.
  const clickAllTranscriptButtons = () => {
    let n = 0;
    for (const b of transcriptButtons(true)) {
      try { b.click(); n++; } catch (_) {}
    }
    return n;
  };

  async function tryOpen() {
    // Round A: click all transcript buttons, expand the description, click again.
    let clicked = clickAllTranscriptButtons();
    const expanded = expandDescription();
    if (expanded) log("expanded description");
    await sleep(350);
    clicked += clickAllTranscriptButtons();
    log(`clicked ${clicked} transcript button(s)`);
    if (await segmentsPresent(3500)) return true;

    // Round B: the "More actions" (⋯) menu → transcript item.
    if (await openViaMoreMenu()) return true;

    // Round C: one more try (button may have rendered late).
    await sleep(600);
    clickAllTranscriptButtons();
    return await segmentsPresent(2500);
  }

  // Wait until the transcript length stops growing (segments can render
  // progressively), so we never return a partial. Returns the fullest text seen.
  async function stabilize() {
    let best = grabTranscript();
    let last = best.length;
    let stable = 0;
    const start = performance.now();
    // Tighter than before: return as soon as the length holds for ~300ms, or
    // after 1.8s max. Cuts source-ready time; full transcripts render fast.
    while (performance.now() - start < 1800) {
      await sleep(150);
      const t = grabTranscript();
      if (t.length > best.length) best = t;
      if (t.length === last) {
        if (++stable >= 2) break;
      } else {
        stable = 0;
        last = t.length;
      }
    }
    return best;
  }

  // Close the transcript / "In this video" engagement panel (newer layouts label
  // it "In this video" with a Transcript tab, so we match by segments too — not
  // just the header). Clicks the panel's own Close/visibility button.
  function closeTranscriptPanel() {
    try {
      const panels = document.querySelectorAll(
        "ytd-engagement-panel-section-list-renderer"
      );
      for (const p of panels) {
        const tid = p.getAttribute("target-id") || "";
        const hasSeg = p.querySelector(
          "ytd-transcript-segment-renderer, transcript-segment-view-model, ytd-transcript-segment-list-renderer"
        );
        if (!/transcript|searchable/i.test(tid) && !hasSeg) continue;
        const close = p.querySelector(
          "ytd-engagement-panel-title-header-renderer #visibility-button button, " +
            'ytd-engagement-panel-title-header-renderer button[aria-label*="Close" i], ' +
            'ytd-engagement-panel-title-header-renderer button[aria-label*="Hide" i]'
        );
        if (close) close.click();
      }
      // Global fallback (older single-panel layout).
      const close = document.querySelector(
        "ytd-engagement-panel-title-header-renderer #visibility-button button"
      );
      if (close && !panels.length) close.click();
    } catch (_) {}
  }

  // Keep the "Show transcript" engagement panel OFF-SCREEN + invisible at all
  // times, so when we open it to scrape, the user never actually SEES it. The
  // segments still render in the DOM (so scraping works); they're just moved out
  // of view. Injected once at load and left in place (broad selector so it
  // catches every YouTube layout variant).
  function injectHideTranscript() {
    if (document.getElementById("ons-hide-transcript")) return;
    const st = document.createElement("style");
    st.id = "ons-hide-transcript";
    // IMPORTANT: only make it transparent + non-interactive. Do NOT move it
    // off-screen or set display/visibility:none — YouTube stops RENDERING the
    // transcript segments when the panel is off-screen/hidden, which breaks the
    // scrape. opacity:0 keeps segments fully rendered (so scraping works) while
    // the user sees nothing.
    // Match EVERY layout: the classic transcript panel (target-id contains
    // "transcript"), the newer "In this video" panel (target-id "…searchable…"),
    // AND — most robust — any engagement panel that actually contains transcript
    // segments (:has, supported in modern Chrome). opacity:0 keeps the segments
    // rendered (so scraping still works) while the user sees nothing.
    st.textContent =
      'ytd-engagement-panel-section-list-renderer[target-id*="transcript" i],' +
      'ytd-engagement-panel-section-list-renderer[target-id*="searchable" i],' +
      "ytd-engagement-panel-section-list-renderer:has(ytd-transcript-segment-list-renderer)," +
      "ytd-engagement-panel-section-list-renderer:has(ytd-transcript-segment-renderer)," +
      "ytd-engagement-panel-section-list-renderer:has(transcript-segment-view-model){" +
      "opacity:0 !important;pointer-events:none !important;}";
    (document.head || document.documentElement).appendChild(st);
  }
  function removeHideTranscript() { /* kept permanently — no-op */ }
  injectHideTranscript(); // apply immediately at content-script load

  async function scrapeOnce() {
    // Fastest + most reliable: fetch caption tracks directly (no UI panel at all).
    try {
      const cap = await fetchCaptionTranscript(currentVideoId());
      if (cap && cap.length > 40) {
        log(`captions → ${cap.length} chars`);
        // Captions came silently — but YouTube may have auto-opened the
        // "In this video"/transcript panel on its own. Close it so it's not shown.
        closeTranscriptPanel();
        return cap;
      }
    } catch (_) {}

    // Fallback = the "Show transcript" panel, but kept OFF-SCREEN so the user
    // never sees it. Segments still render in the DOM, so scraping works.
    injectHideTranscript();
    try {
      if (grabTranscript().length > 40) {
        const text = await stabilize();
        log(`already open → ${text.length} chars`);
        return text.length > 40 ? text : null;
      }
      const opened = await tryOpen();
      const text = await stabilize();
      log(`opened=${opened} (hidden) → ${text.length} chars`);
      return text.length > 40 ? text : null;
    } finally {
      // ALWAYS close the panel when we're done — whether we opened it or it was
      // already open (YouTube sometimes auto-opens the "In this video" panel).
      // This removes the empty gap the invisible panel would otherwise leave.
      closeTranscriptPanel();
      setTimeout(closeTranscriptPanel, 400); // catch the late-rendering panel too
    }
  }

  // ---- Cache + pre-load ----
  // Pre-loading: a few seconds after the watch page settles, we quietly scrape
  // the transcript in the background and cache it. Then when the user clicks
  // Summarize, the transcript is already in hand → source is ready instantly
  // (no scrape wait on the click path). The panel briefly opens and closes
  // during the pre-scrape.
  const CACHE_TTL_MS = 10 * 60 * 1000;
  let cache = null; // { videoId, text, ts }
  let inflight = null; // { videoId, promise }

  function currentVideoId() {
    return new URLSearchParams(location.search).get("v");
  }

  // ---- Exact likes + views, read straight from the watch page (what the
  // user actually SEES on YouTube). No community/estimate API — so the numbers
  // in the side panel match the video 1:1. Dislikes are intentionally ignored.
  function firstNumberToken(s) {
    if (!s) return null;
    // Grab a YouTube-style count token: "2.5K", "1.2M", "95,858", "543".
    const m = String(s).match(/(\d[\d,.\s]*\s?[KMB]?)/i);
    return m ? m[1].replace(/\s+/g, "").trim() : null;
  }

  // EXACT like count. YouTube's accessibility label carries the full integer
  // ("2,543 likes") even though the button only shows "2.5K". Prefer that.
  function exactLikesFromData() {
    // (a) the rendered like button's own accessible label (live, current video)
    const btn = document.querySelector(
      'like-button-view-model button, ytd-toggle-button-renderer button, button[aria-label*="like" i]'
    );
    if (btn) {
      const lab = btn.getAttribute("aria-label") || btn.getAttribute("title") || "";
      let m = lab.match(/([\d,]+)\s+likes?/i) || lab.match(/along with\s+([\d,]+)/i);
      if (m) return m[1];
    }
    // (b) ytInitialData script text — "…: 2,543 likes" accessibility label
    for (const s of document.querySelectorAll("script")) {
      const t = s.textContent || "";
      if (t.indexOf("likeButton") < 0 && t.indexOf("ytInitialData") < 0) continue;
      const m = t.match(/"(?:accessibilityText|label)":"([\d,]+) likes"/);
      if (m) return m[1]; // "2,543"
    }
    return null;
  }

  function readLikes() {
    // Exact first…
    const exact = exactLikesFromData();
    if (exact) return { text: exact, num: parseInt(exact.replace(/[^\d]/g, ""), 10) };
    // …else the visible (abbreviated) button text as a last resort.
    const sels = [
      "like-button-view-model button .yt-spec-button-shape-next__button-text-content",
      "#segmented-like-button .yt-spec-button-shape-next__button-text-content",
      "#top-level-buttons-computed ytd-toggle-button-renderer:first-of-type #text",
    ];
    for (const s of sels) {
      const el = document.querySelector(s);
      const t = el && (el.textContent || "").trim();
      if (t && /\d/.test(t)) return { text: t, num: null };
    }
    return { text: null, num: null };
  }

  function readViews() {
    // Exact view integer from the page's player data → matches YouTube exactly.
    try {
      const pr = playerResponseFromDom(currentVideoId());
      const vc = pr && pr.videoDetails && pr.videoDetails.viewCount;
      if (vc && /^\d+$/.test(String(vc))) {
        const num = Number(vc);
        return { text: num.toLocaleString("en-US"), num }; // "95,858"
      }
    } catch (_) {}
    const info = document.querySelector(
      "ytd-watch-info-text #info, ytd-watch-metadata #info-container, .view-count, #info-strings"
    );
    if (info) {
      const n = firstNumberToken(info.textContent || "");
      if (n) return { text: n, num: parseInt(n.replace(/[^\d]/g, ""), 10) || null };
    }
    return { text: null, num: null };
  }

  function readVideoStats() {
    const l = readLikes();
    const v = readViews();
    return {
      likes: l.text, likesNum: l.num,
      views: v.text, viewsNum: v.num,
      videoId: currentVideoId(),
    };
  }

  // Stats may not be painted yet right after a navigation — poll briefly.
  function readVideoStatsWait(wantId) {
    return new Promise((resolve) => {
      let tries = 0;
      const tick = () => {
        tries++;
        const cur = currentVideoId();
        if (!wantId || wantId === cur) {
          const st = readVideoStats();
          if (st.likes || st.views || tries >= 16) return resolve(st);
        } else if (tries >= 16) {
          return resolve({ likes: null, views: null, videoId: cur });
        }
        setTimeout(tick, 250); // up to ~4s
      };
      tick();
    });
  }

  async function getTranscriptCached() {
    const vid = currentVideoId();
    if (!vid) return null;
    if (cache && cache.videoId === vid && cache.text && Date.now() - cache.ts < CACHE_TTL_MS) {
      log("cache hit");
      return cache.text;
    }
    if (inflight && inflight.videoId === vid) return inflight.promise;

    const promise = (async () => {
      try {
        const text = await scrapeOnce();
        if (text) cache = { videoId: vid, text, ts: Date.now() };
        return text;
      } finally {
        if (inflight && inflight.videoId === vid) inflight = null;
      }
    })();
    inflight = { videoId: vid, promise };
    return promise;
  }

  // Kick off a background pre-scrape shortly after a watch page loads / changes.
  let preloadTimer = null;
  function schedulePreload() {
    if (preloadTimer) clearTimeout(preloadTimer);
    if (!currentVideoId()) return;
    preloadTimer = setTimeout(() => {
      if (currentVideoId()) {
        log("pre-loading transcript in background");
        getTranscriptCached().catch(() => {});
      }
    }, 1000); // read the transcript early so it's already cached when the user clicks
  }
  let lastVid = currentVideoId();
  schedulePreload();
  // YouTube is a SPA — when it navigates to a NEW video, the old transcript
  // panel can linger with the previous video's text. Drop all cached state AND
  // close the stale panel so the next scrape re-opens a fresh one for the
  // current video (this fixes summaries showing the wrong video's content).
  document.addEventListener("yt-navigate-finish", () => {
    const vid = currentVideoId();
    if (vid === lastVid) return; // same video (e.g. a non-video nav) — ignore
    lastVid = vid;
    cache = null;
    inflight = null;
    closeTranscriptPanel();
    schedulePreload();
  });

  browser.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (!msg) return;

    if (msg.type === "GET_VIDEO_STATS") {
      readVideoStatsWait(msg.videoId || null)
        .then((st) => sendResponse({ ok: !!(st.likes || st.views), ...st }))
        .catch(() => sendResponse({ ok: false }));
      return true; // async
    }

    if (msg.type !== "GET_TRANSCRIPT" && msg.type !== "GET_TRANSCRIPT_ANY") return;

    const currentV = currentVideoId();
    const wantId = msg.videoId || currentV;

    // Same video as this page → cached scrape (caption-first inside).
    if (wantId && currentV && wantId === currentV) {
      getTranscriptCached()
        .then((text) => sendResponse({ ok: !!text, text: text || null }))
        .catch((e) => { log("error:", e && e.message); sendResponse({ ok: false, error: e && e.message }); });
      return true; // async
    }

    // A DIFFERENT video (thumbnail on home/search) or not on a watch page →
    // fetch that video's captions directly. No navigation, no opening the video.
    if (wantId) {
      fetchCaptionTranscript(wantId)
        .then((text) => sendResponse({ ok: !!text, text: text || null, via: "captions" }))
        .catch((e) => { log("caption error:", e && e.message); sendResponse({ ok: false, error: e && e.message }); });
      return true; // async
    }

    sendResponse({ ok: false, reason: "no_video" });
    return; // synchronous
  });
})();
