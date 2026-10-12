// Runs on every youtube.com page. Injects a "Summarize" button:
//  1) On the watch page, near the Like/Share row.
//  2) On every video thumbnail (search results, home, subscriptions, sidebar).
// Clicking a thumbnail's button opens the side panel and tells it to focus
// on THAT video, even though the active tab is still the listing page.

function makeButton(compact) {
  const btn = document.createElement("button");
  btn.className = compact ? "ons-btn ons-btn-compact" : "ons-btn";
  btn.type = "button";
  const icon = `<svg class="ons-brand-icon" viewBox="0 0 32 32" aria-hidden="true" focusable="false"><rect x="2" y="2" width="28" height="28" rx="8" fill="rgba(255,255,255,.2)" stroke="rgba(255,255,255,.72)" stroke-width="1.5"/><path d="M13 10l9 6-9 6z" fill="#fff"/></svg>`;
  btn.innerHTML = compact
    ? icon
    : `${icon}<span class="ons-label">Summarize</span>`;
  btn.title = "Summarize with TubeNotes";
  btn.setAttribute("aria-label", "Summarize with TubeNotes");
  return btn;
}

function cleanVideoUrl(href) {
  try {
    const u = new URL(href, location.href);
    const v = u.searchParams.get("v");
    if (!v) return null;
    return `${u.origin}/watch?v=${v}`;
  } catch {
    return null;
  }
}

function requestSummarize(videoUrl, videoTitle) {
  browser.runtime.sendMessage({
    type: "OPEN_SIDE_PANEL_FOR_URL",
    payload: { videoUrl, videoTitle },
  });
  // For a thumbnail (a DIFFERENT video than the current page), open that video.
  // The transcript is then read SILENTLY from the page's player data — the
  // "Show transcript" panel is NOT opened in front of the user.
  let currentV = null, targetV = null;
  try { currentV = new URLSearchParams(location.search).get("v"); } catch (_) {}
  try { targetV = new URL(videoUrl, location.href).searchParams.get("v"); } catch (_) {}
  if (targetV && targetV !== currentV) {
    setTimeout(() => { location.href = videoUrl; }, 150);
  }
}

// Wake the background service worker EARLY (on press/hover), before the actual
// click. In MV3 the worker often sleeps; waking it on the click itself adds
// latency and can make the browser drop the user-gesture window, so the side
// panel opens late (or only on a second click). Pinging on pointerdown means the
// worker is already awake by the time OPEN_SIDE_PANEL_FOR_URL arrives, so
// sidePanel.open() runs instantly within the gesture.
let _lastWarm = 0;
function warmUpWorker() {
  const now = Date.now();
  if (now - _lastWarm < 1500) return; // throttle — don't spam pings
  _lastWarm = now;
  try {
    // Promise style: the polyfill gives Chrome the same API Firefox already has.
    browser.runtime.sendMessage({ type: "PING" }).catch(() => {});
  } catch {
    // worker not reachable yet — the click path will still wake it
  }
}

// One capturing listener covers every Summarize button (watch page + thumbnails).
document.addEventListener(
  "pointerdown",
  (e) => {
    const t = e.target;
    if (t && t.closest && t.closest(".ons-btn, .ons-btn-inline, .ons-btn-compact-fallback, .ons-btn-overlay")) {
      warmUpWorker();
    }
  },
  true
);

function isVisible(el) {
  if (!el) return false;
  const rect = el.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0 && el.offsetParent !== null;
}

let watchBtnAttempts = 0;
const MAX_WATCH_BTN_ATTEMPTS = 5;

// ---- 1) Watch page button (right next to the Subscribe button) ----
function injectWatchButton() {
  if (!location.pathname.startsWith("/watch")) return;
  if (watchBtnAttempts >= MAX_WATCH_BTN_ATTEMPTS) return;

  const existing = document.getElementById("ons-watch-btn");
  if (existing) {
    if (isVisible(existing)) return; // already placed correctly
    existing.remove(); // was placed in a hidden duplicate — retry below
  }

  watchBtnAttempts++;

  const candidates = Array.from(
    document.querySelectorAll("button, ytd-button-renderer, yt-button-shape, ytd-subscribe-button-renderer")
  ).filter(isVisible);

  const subscribeEl = candidates.find((el) => {
    const label = (el.getAttribute("aria-label") || el.textContent || "")
      .trim()
      .toLowerCase();
    return label === "subscribe" || label.startsWith("subscribe");
  });
  if (!subscribeEl) return;

  const container =
    subscribeEl.closest("div#subscribe-button") ||
    subscribeEl.closest("ytd-subscribe-button-renderer") ||
    subscribeEl;
  if (!container || !container.parentElement) return;

  const btn = makeButton(false);
  btn.id = "ons-watch-btn";
  btn.addEventListener("click", () => {
    const videoUrl = cleanVideoUrl(location.href);
    const videoTitle = document.title.replace(/ - YouTube$/, "");
    if (videoUrl) requestSummarize(videoUrl, videoTitle);
  });

  container.parentElement.insertBefore(btn, container.nextSibling);
}

// ---- 2) Thumbnail buttons (search / home / subscriptions / sidebar) ----
const RENDERER_SELECTOR = [
  "ytd-video-renderer",
  "ytd-rich-item-renderer",
  "ytd-compact-video-renderer",
  "ytd-grid-video-renderer",
  "yt-lockup-view-model",
].join(",");

function processRenderer(renderer) {
  const link = renderer.querySelector(
    'a#thumbnail, a#video-title-link, a#video-title, a[href*="/watch?v="]'
  );
  if (!link || !link.href) return; // not rendered yet, try again later

  const videoUrl = cleanVideoUrl(link.href);
  if (!videoUrl) return;

  // Self-healing + recycle-aware: YouTube reuses the same tile DOM for a
  // DIFFERENT video while scrolling / when going Home, which silently drops our
  // button (or leaves one pointing at the wrong video). We key everything off
  // the CURRENT video URL:
  //  - button present, same URL, visible → nothing to do.
  //  - button present but URL changed / hidden → remove & re-place.
  //  - URL changed → reset the retry counter so a recycled tile gets a fresh button.
  const existingBtn = renderer.querySelector(".ons-btn-inline, .ons-btn-overlay");
  if (existingBtn) {
    if (renderer.dataset.onsForUrl === videoUrl && isVisible(existingBtn)) return;
    existingBtn.remove();
  }
  if (renderer.dataset.onsForUrl !== videoUrl) renderer.dataset.onsAttempts = "0";
  const attempts = parseInt(renderer.dataset.onsAttempts || "0", 10);
  if (attempts >= 8) return; // truly can't place (rare) — give up for THIS url only

  const titleEl = renderer.querySelector(
    '#video-title, yt-formatted-string#video-title, h3 a, a[href*="/watch?v="] span, a[href*="/watch?v="] yt-formatted-string'
  );
  let videoTitle = titleEl ? titleEl.textContent.trim() : "";
  if (!videoTitle) {
    videoTitle =
      link.getAttribute("title") ||
      link.getAttribute("aria-label") ||
      link.textContent.trim();
  }
  if (!videoTitle) videoTitle = document.title; // last resort

  const btn = makeButton(false);
  btn.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    requestSummarize(videoUrl, videoTitle);
  });

  // Preferred: sit inline right after the "views • time ago" metadata line.
  // Classic YouTube uses #metadata-line; newer layouts use a
  // yt-content-metadata-view-model with rows of class
  // ytContentMetadataViewModelMetadataRow (there can be more than one row,
  // e.g. one for badges and one for views/date — pick the one that looks
  // like it has view/date text).
  const metaCandidates = Array.from(
    renderer.querySelectorAll(
      "#metadata-line, ytd-video-meta-block #metadata-line, .metadata-line, .ytContentMetadataViewModelMetadataRow"
    )
  );
  const metaLine =
    metaCandidates.find((el) =>
      /view|ago|hour|day|month|year|min|sec|watching|live|•/i.test(
        el.textContent || ""
      )
    ) || metaCandidates[metaCandidates.length - 1];

  if (metaLine) {
    btn.classList.add("ons-btn-inline");
    metaLine.style.setProperty("overflow", "visible", "important");
    metaLine.appendChild(btn);
  } else {
    // Fallback: small overlay badge on the thumbnail image.
    btn.classList.add("ons-btn-overlay");
    btn.classList.remove("ons-btn");
    btn.classList.add("ons-btn-compact-fallback");
    renderer.style.position = renderer.style.position || "relative";
    renderer.appendChild(btn);
  }

  renderer.dataset.onsAttempts = String(attempts + 1);
  renderer.dataset.onsForUrl = isVisible(btn) ? videoUrl : "";
}

function scanAll() {
  const all = Array.from(document.querySelectorAll(RENDERER_SELECTOR));
  all.forEach((el) => {
    // If this element is nested inside another matched renderer, skip it —
    // only process the outermost one to avoid double-processing the same
    // video (some layouts nest a newer element type inside an older one).
    const isNested = all.some((other) => other !== el && other.contains(el));
    if (!isNested) processRenderer(el);
  });
  injectWatchButton();
}

// Initial pass (content scripts run after DOM is mostly parsed at document_idle)
scanAll();

// YouTube is a single-page app; re-scan on its internal navigation event. The
// grid re-renders progressively (and recycles tiles), so scan a few times after
// the nav to reliably re-place buttons that a Home/back navigation dropped.
document.addEventListener("yt-navigate-finish", () => {
  watchBtnAttempts = 0;
  scanAll();
  setTimeout(scanAll, 300);
  setTimeout(scanAll, 900);
  setTimeout(scanAll, 1800);
});

// Debounce: our own scanAll() inserts/removes elements, which would
// otherwise re-trigger this same observer and cause a runaway loop.
// Coalesce bursts of YouTube's own DOM churn into one scan at a time.
let scanScheduled = false;
function scheduleScan() {
  if (scanScheduled) return;
  scanScheduled = true;
  setTimeout(() => {
    scanScheduled = false;
    scanAll();
  }, 800);
}

const observer = new MutationObserver(() => scheduleScan());
observer.observe(document.documentElement, { childList: true, subtree: true });

// Low-frequency safety net, in case something was missed.
setInterval(scanAll, 5000);
