// Chrome runs this file as an MV3 service worker, where importScripts() is the
// only way to pull in siblings. Firefox runs MV3 backgrounds as an event page
// and lists the same files in manifest.background.scripts, so importScripts is
// undefined there - hence the guard.
if (typeof importScripts === "function") {
  importScripts("browser-polyfill.js", "trialguard.js", "panel-compat.js");
}

// Ollama used to be reachable at a public hostname with no authentication at
// all - anyone who knew the name could run models on the GPU. It now sits
// behind the same login as everything else, so every call from here carries the
// signed-in user's token. Without one the backend answers 401 and nothing
// reaches the model. A signed-out user simply gets no summary, which is the
// correct outcome.
async function ollamaHeaders() {
  const headers = { "Content-Type": "application/json" };
  try {
    const token = await TrialGuard.accessToken();
    if (token) headers.Authorization = "Bearer " + token;
  } catch (_) {
    // No token: send the request anyway so the backend's 401 surfaces as a
    // normal error the panel already knows how to show.
  }
  return headers;
}

const POLL_ALARM_PREFIX = "poll-";
let _lastWarm = { key: "", ts: 0 }; // throttles Ollama model preload (WARM_OLLAMA)

// Chrome otherwise uses a grey square with the first letter of the extension
// name. Draw the TubeNotes purple/pink play mark at the exact toolbar sizes so
// it stays crisp on normal and high-DPI displays without an external image.
function tubeNotesIcon(size) {
  const canvas = new OffscreenCanvas(size, size);
  const ctx = canvas.getContext("2d", { alpha: true });
  const inset = Math.max(1, Math.round(size * 0.04));
  const edge = size - (inset * 2);
  const radius = Math.max(3, Math.round(size * 0.24));
  const gradient = ctx.createLinearGradient(inset, inset, size - inset, size - inset);
  gradient.addColorStop(0, "#7c3aed");
  gradient.addColorStop(0.58, "#a83fe3");
  gradient.addColorStop(1, "#ec4899");
  ctx.fillStyle = gradient;
  ctx.beginPath();
  ctx.roundRect(inset, inset, edge, edge, radius);
  ctx.fill();

  const left = size * 0.42;
  const top = size * 0.31;
  const triangle = size * 0.38;
  ctx.fillStyle = "#ffffff";
  ctx.beginPath();
  ctx.moveTo(left, top);
  ctx.lineTo(left, top + triangle);
  ctx.lineTo(left + triangle * 0.72, size * 0.5);
  ctx.closePath();
  ctx.fill();
  return ctx.getImageData(0, 0, size, size);
}

function setTubeNotesToolbarIcon() {
  if (!browser.action || !browser.action.setIcon || typeof OffscreenCanvas === "undefined") return;
  const imageData = {};
  [16, 32, 48, 128].forEach((size) => { imageData[size] = tubeNotesIcon(size); });
  browser.action.setIcon({ imageData }).catch(() => {});
}

// NOTE: openPanelOnActionClick is intentionally NOT enabled. With it on, the
// side panel becomes a single GLOBAL instance that Chrome keeps open on every
// tab, and per-tab `enabled:false` can't close it — so it leaked onto non-
// YouTube tabs (New Tab, WhatsApp, …). Instead we manage the panel PER TAB:
// enable + open it only on YouTube tabs (from the Summarize click), disable it
// everywhere else, so it truly hides on non-YouTube tabs.
browser.runtime.onInstalled.addListener((details) => {
  setTubeNotesToolbarIcon();
  Panel.init();
  TrialGuard.refreshBadge();

  // Chrome cannot open an action popup by itself after installation, but it
  // can open an extension tab.  Make the first-run step happen immediately so
  // users sign in once here, rather than being asked again in the YouTube side
  // panel when they press Summarize.
  if (details && details.reason === "install") {
    (async () => {
      if (await TrialGuard.isSignedIn()) return;
      await browser.tabs.create({
        url: browser.runtime.getURL("account.html?welcome=1"),
        active: true,
      });
    })().catch(() => {});
  }
});
browser.runtime.onStartup.addListener(() => {
  setTubeNotesToolbarIcon();
  Panel.init();
  TrialGuard.refreshBadge();
});
setTubeNotesToolbarIcon();

// ---- Show the side panel ONLY on YouTube tabs ----
// The side panel is one instance per window. By enabling it per-tab we make it
// auto-hide when the user switches to a non-YouTube tab (e.g. WhatsApp) and
// re-appear (with its state intact) when they switch back to a YouTube tab —
// without the user having to click the icon again.
function isYouTubeUrl(url) {
  try { return /(^|\.)youtube\.com$/.test(new URL(url).hostname); }
  catch { return false; }
}
const _panelState = new Map(); // tabId -> last enabled bool (avoid redundant calls that reload the panel)
async function updatePanelForTab(tabId, url) {
  const enabled = isYouTubeUrl(url);
  if (_panelState.get(tabId) === enabled) return;
  _panelState.set(tabId, enabled);
  // Chrome hides/shows the panel per tab; on Firefox the sidebar is global and
  // this is a no-op (see panel-compat.js).
  await Panel.setEnabledForTab(tabId, enabled);
}
browser.tabs.onRemoved.addListener((tabId) => _panelState.delete(tabId));
browser.tabs.onActivated.addListener(async ({ tabId }) => {
  try {
    const t = await browser.tabs.get(tabId);
    await updatePanelForTab(tabId, (t && t.url) || "");
  } catch (_) {}
});
browser.tabs.onUpdated.addListener((tabId, info, tab) => {
  if (info.url || info.status === "complete") {
    updatePanelForTab(tabId, (tab && tab.url) || "");
  }
});

// ---- Job storage helpers ----
// jobs[sourceId] = {
//   apiUrl, notebookId, videoUrl, videoTitle, createdAt,
//   transformationId,      // only set once an "export" was requested
//   stage,                 // waiting_source_only -> chat_ready -> waiting_insight -> done
//   status,                // 'processing' | 'done' | 'error'
//   sessionId, messages,   // chat state (used by the side panel)
// }
async function getJobs() {
  const { jobs = {} } = await browser.storage.local.get("jobs");
  return jobs;
}
async function saveJobs(jobs) {
  await browser.storage.local.set({ jobs });
}

function findJobByUrl(jobs, url) {
  for (const id of Object.keys(jobs)) {
    if (jobs[id].videoUrl === url) return id;
  }
  return null;
}

// ---- Create the underlying source (no transformation) ----
// Two ways in:
//  1) transcriptText present  -> the side panel already scraped the transcript
//     straight from the YouTube "Show transcript" panel in the browser. We send
//     it as a TEXT source, so the server SKIPS its own YouTube extraction
//     entirely (that step used the DGX's IP and was slow / rate-limited /
//     sometimes blocked). This is the fast path: source ready in well under a
//     second.
//  2) no transcriptText       -> fall back to a LINK source and let the server
//     (content-core) extract it, exactly as before. Used for videos with no
//     captions, thumbnail clicks on listing pages, or non-YouTube pages.
//
// fastMode (default true): skip embedding and ask for synchronous processing so
// the source becomes usable ASAP. Transformations (summaries) read the source's
// full text and do NOT need embeddings, so skipping them is safe and much faster.
// Turn fastMode OFF if you rely heavily on RAG-based chat follow-ups.
// Very long transcripts (1hr+ videos) make the model take far too long — a 32k
// char transcript took ~95s to summarize. Cap by sampling evenly across the
// whole video, so we keep coverage of beginning/middle/end while bounding the
// generation time. Normal videos (under the cap) are sent in full, unchanged.
const MAX_TRANSCRIPT_CHARS = 12000;
function capTranscript(text) {
  if (!text || text.length <= MAX_TRANSCRIPT_CHARS) return text;
  const parts = 12;
  const sliceLen = Math.floor(MAX_TRANSCRIPT_CHARS / parts);
  const step = Math.floor(text.length / parts);
  const out = [];
  for (let i = 0; i < parts; i++) out.push(text.substr(i * step, sliceLen).trim());
  return out.join(" … ");
}

async function createSource({ apiUrl, notebookId, videoUrl, videoTitle, transcriptText, fastMode = true }) {
  const form = new FormData();
  form.append("notebook_id", notebookId);
  if (transcriptText && transcriptText.length > 40) {
    form.append("type", "text");
    form.append("content", capTranscript(transcriptText));
    if (videoTitle) form.append("title", videoTitle);
  } else {
    form.append("type", "link");
    form.append("url", videoUrl);
  }
  form.append("embed", fastMode ? "false" : "true");
  // async_processing:false => the server tries to finish before responding, which
  // removes a whole poll round. If the server ignores it and still goes async, the
  // side panel's fast poll picks it up anyway — so this is never slower.
  form.append("async_processing", fastMode ? "false" : "true");

  const res = await fetch(`${apiUrl}/api/sources`, { method: "POST", body: form });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`${res.status}: ${text.slice(0, 200)}`);
  }
  return res.json();
}

// ---- Lightweight path: get chat ready ASAP (no transformation needed) ----
async function ensureChatSource({ apiUrl, notebookId, videoUrl, videoTitle, autoSummarize, fastMode = true, transcriptText }) {
  const jobs = await getJobs();
  const existingId = findJobByUrl(jobs, videoUrl);
  if (existingId) {
    if (autoSummarize && !jobs[existingId].autoSummarizeRequested) {
      jobs[existingId].autoSummarizeRequested = true;
      await saveJobs(jobs);
    }
    return { sourceId: existingId, stage: jobs[existingId].stage };
  }

  const created = await createSource({ apiUrl, notebookId, videoUrl, videoTitle, transcriptText, fastMode });
  jobs[created.id] = {
    apiUrl,
    notebookId,
    videoUrl,
    videoTitle,
    stage: "waiting_source_only",
    status: "processing",
    createdAt: Date.now(),
    autoSummarizeRequested: !!autoSummarize,
  };
  await saveJobs(jobs);

  // No background alarm here on purpose: the side panel actively fast-polls
  // (every 2s) while it's open to get chat ready ASAP. Creating an alarm too
  // caused a race — both writers updating the same job entry and clobbering
  // each other's changes (that's what caused the "reading sessionId of
  // undefined" errors). The export flow (startExport) still uses an alarm,
  // since that needs to keep running even if the panel is closed.

  return { sourceId: created.id, stage: "waiting_source_only" };
}

// ---- Full path: request the polished, detailed export (transformation + auto .md/PDF) ----
async function startExport({ apiUrl, notebookId, transformationId, videoUrl, videoTitle, fastMode = true, transcriptText }) {
  const jobs = await getJobs();
  const existingId = findJobByUrl(jobs, videoUrl);

  if (existingId) {
    const job = jobs[existingId];
    job.transformationId = transformationId;

    if (job.stage === "waiting_source_only") {
      // checkJob will trigger the transformation itself once the source
      // finishes, since transformationId is now set.
      jobs[existingId] = job;
      await saveJobs(jobs);
    } else if (job.stage === "waiting_insight") {
      // Already actively exporting — don't double-trigger.
    } else {
      // 'chat_ready', 'done', or any other resolved state — (re)trigger a
      // fresh export. This also fixes videos that got stuck 'done' with
      // stale content from an earlier bug.
      job.exportInsightBaselineIds = await getInsightIds(apiUrl, existingId);
      await triggerTransformation(job, existingId);
      job.stage = "waiting_insight";
      job.status = "processing";
      jobs[existingId] = job;
      await saveJobs(jobs);
      browser.alarms.create(POLL_ALARM_PREFIX + existingId, {
        delayInMinutes: 0.15,
        periodInMinutes: 1,
      });
    }
    return { sourceId: existingId };
  }

  const created = await createSource({ apiUrl, notebookId, videoUrl, videoTitle, transcriptText, fastMode });
  jobs[created.id] = {
    apiUrl,
    notebookId,
    transformationId,
    videoTitle,
    videoUrl,
    stage: "waiting_source_only",
    status: "processing",
    createdAt: Date.now(),
  };
  await saveJobs(jobs);

  browser.alarms.create(POLL_ALARM_PREFIX + created.id, {
    delayInMinutes: 0.15,
    periodInMinutes: 1,
  });

  return { sourceId: created.id };
}

async function getInsightIds(apiUrl, sourceId) {
  try {
    const res = await fetch(`${apiUrl}/api/sources/${sourceId}/insights`);
    if (!res.ok) return [];
    const insights = await res.json();
    return Array.isArray(insights) ? insights.map((i) => i.id) : [];
  } catch {
    return [];
  }
}

async function triggerTransformation(job, sourceId) {
  const res = await fetch(`${job.apiUrl}/api/sources/${sourceId}/insights`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ transformation_id: job.transformationId }),
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`Failed to start transformation: ${res.status} ${text.slice(0, 200)}`);
  }
}

// ---- Poll loop, called every ~1 minute by the alarm ----
async function checkJob(sourceId) {
  const jobs = await getJobs();
  const job = jobs[sourceId];
  if (!job || job.stage === "done" || job.stage === "chat_ready") return;

  try {
    if (job.stage === "waiting_source_only") {
      const sourceRes = await fetch(`${job.apiUrl}/api/sources/${sourceId}`);
      if (!sourceRes.ok) return; // transient, retry next tick
      const source = await sourceRes.json();

      if (source.status === "error") {
        job.stage = "done";
        job.status = "error";
        jobs[sourceId] = job;
        await saveJobs(jobs);
        browser.alarms.clear(POLL_ALARM_PREFIX + sourceId);
        return;
      }

      if (source.status === "completed") {
        if (job.transformationId) {
          job.exportInsightBaselineIds = await getInsightIds(job.apiUrl, sourceId);
          await triggerTransformation(job, sourceId);
          job.stage = "waiting_insight";
        } else {
          job.stage = "chat_ready";
          browser.alarms.clear(POLL_ALARM_PREFIX + sourceId);
        }
        jobs[sourceId] = job;
        await saveJobs(jobs);
      }
      return;
    }

    if (job.stage === "waiting_insight") {
      const insRes = await fetch(`${job.apiUrl}/api/sources/${sourceId}/insights`);
      if (!insRes.ok) return;
      const insights = await insRes.json();
      const baselineIds = new Set(job.exportInsightBaselineIds || []);
      const newInsight = Array.isArray(insights)
        ? insights.find((i) => !baselineIds.has(i.id))
        : null;
      if (newInsight) {
        const content = newInsight.content;
        await downloadSummary(job.videoTitle, job.videoUrl, content);
        await openPrintTab(sourceId, job.videoTitle, job.videoUrl, content);

        job.stage = "done";
        job.status = "done";
        jobs[sourceId] = job;
        await saveJobs(jobs);
        browser.alarms.clear(POLL_ALARM_PREFIX + sourceId);
      }
    }
  } catch (e) {
    // network hiccup — just retry on the next alarm tick
  }
}

function sanitizeFilename(name) {
  return name.replace(/[\\/:*?"<>|]/g, "").trim().slice(0, 120) || "summary";
}

async function downloadSummary(title, url, content) {
  const md = `# ${title}\n\nSource: ${url}\n\n---\n\n${content}`;
  const dataUrl = "data:text/markdown;charset=utf-8," + encodeURIComponent(md);
  await browser.downloads.download({
    url: dataUrl,
    filename: `YouTube Summaries/${sanitizeFilename(title)}.md`,
    saveAs: false,
    conflictAction: "uniquify",
  });
}

async function openPrintTab(sourceId, title, url, content) {
  const { printJobs = {} } = await browser.storage.local.get("printJobs");
  printJobs[sourceId] = { title, url, content };
  await browser.storage.local.set({ printJobs });

  browser.tabs.create({
    url: browser.runtime.getURL(`print.html?printJobId=${encodeURIComponent(sourceId)}`),
    active: true,
  });
}

// ---- Wiring ----
browser.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name.startsWith(POLL_ALARM_PREFIX)) {
    checkJob(alarm.name.slice(POLL_ALARM_PREFIX.length));
  }
});

browser.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.type === "ENSURE_CHAT_SOURCE") {
    ensureChatSource(msg.payload)
      .then((r) => sendResponse({ ok: true, ...r }))
      .catch((err) => sendResponse({ ok: false, error: err.message }));
    return true;
  }
  if (msg.type === "START_EXPORT") {
    startExport(msg.payload)
      .then((r) => sendResponse({ ok: true, ...r }))
      .catch((err) => sendResponse({ ok: false, error: err.message }));
    return true;
  }
  if (msg.type === "PING") {
    // Lightweight wake-up so the worker is warm before the click's open call.
    sendResponse({ ok: true });
    return; // synchronous response
  }
  if (msg.type === "WARM_OLLAMA") {
    // Preload the model into memory so the first real summary isn't cold. Fire-
    // and-forget; throttled per (url, model) so frequent refreshes don't spam it.
    const url = String(msg.url || "").replace(/\/+$/, "");
    const model = msg.model;
    const key = url + "|" + model;
    const now = Date.now();
    if (url && model && (key !== _lastWarm.key || now - _lastWarm.ts > 90000)) {
      _lastWarm = { key, ts: now };
      ollamaHeaders().then((headers) => {
        fetch(`${url}/api/generate`, {
          method: "POST",
          headers,
          body: JSON.stringify({ model, prompt: "", stream: false, keep_alive: -1 }),
        }).catch(() => {});
      });
    }
    sendResponse({ ok: true });
    return; // synchronous response
  }
  if (msg.type === "OPEN_SIDE_PANEL_FOR_URL") {
    // Store the target video FIRST (fire-and-forget) so the panel already has it
    // when it loads, then open with minimal delay to stay inside the user gesture.
    browser.storage.local.set({
      pendingOverride: {
        url: msg.payload.videoUrl,
        title: msg.payload.videoTitle,
        ts: Date.now(),
      },
    });

    const tid = _sender.tab && _sender.tab.id;
    if (tid != null) _panelState.set(tid, true);
    // Panel.open() picks Chrome's side panel, Firefox's sidebar, or a popup
    // window - whichever this browser actually supports.
    Panel.open(tid)
      .then((r) => sendResponse({ ok: true, openError: r.ok ? null : r.error, via: r.via }))
      .catch((e) => sendResponse({ ok: false, openError: (e && e.message) || String(e) }));
    return true; // async
  }
  if (msg.type === "EXPORT_CONTENT") {
    // Content already generated (e.g. by the fast Ollama path) — just download
    // the Markdown and open a print-ready tab for saving as PDF.
    (async () => {
      try {
        const { title, url, content } = msg.payload;
        const id = "export-" + Date.now();
        await downloadSummary(title, url, content);
        await openPrintTab(id, title, url, content);
        sendResponse({ ok: true });
      } catch (err) {
        sendResponse({ ok: false, error: err.message });
      }
    })();
    return true;
  }
  if (msg.type === "GET_JOBS") {
    getJobs().then((jobs) => sendResponse({ jobs }));
    return true;
  }
  // ---- One-shot (non-streaming) Ollama call — used for follow-up questions ----
  if (msg.type === "OLLAMA_ONESHOT") {
    ollamaOneShot(msg)
      .then((text) => sendResponse({ ok: true, text }))
      .catch((e) => sendResponse({ ok: false, error: (e && e.message) || String(e) }));
    return true; // async
  }
  // ---- Full-notes PDF export through TrialGuard + vLLM ----
  // The side panel supplies the transcript when it can read captions locally.
  // The backend falls back to its own transcript fetch when it cannot.
  if (msg.type === "START_PDF_JOB") {
    (async () => {
      try {
        const { transcript, title, url, targetLang, targetLangName } = msg.payload || {};
        if (!url) throw new Error("missing video URL");
        const job = {
          active: true,
          transcript: transcript || null,
          title: title || "YouTube video",
          url,
          targetLang: targetLang || null,
          targetLangName: targetLangName || null,
          pct: 4,
          stage: 2,
          label: "Analyzing video content…",
          ts: Date.now(),
        };
        await browser.storage.local.set({ pdfJob: job });
        runPdfJob();
        sendResponse({ ok: true });
      } catch (e) {
        sendResponse({ ok: false, error: (e && e.message) || String(e) });
      }
    })();
    return true;
  }
});

// ========================================================================
// ---- Full-notes PDF job engine (runs in the background service worker) ----
// Ported from the side panel so generation continues even when the panel is
// hidden. Progress + partial notes are persisted to storage.local.pdfJob after
// every chunk, so the job also RESUMES if the worker is suspended mid-run.
// ========================================================================
const NOTES_FORMAT_RULE = `- CHOOSE THE FORMAT FROM THE CONTENT TYPE:
  • If this is a STORY / narrative / anecdote (कहानी, a historical tale, a biography told as a story): write each "## Heading" followed by flowing detailed PARAGRAPHS (3-6 sentences each). Do NOT use bullet points for story content — tell it as prose.
  • If this is INFORMATIONAL (travel, documentary, a school subject/lesson, tutorial, how-to, news, a facts/points podcast, comparisons, lists): under each "## Heading" use a mix — a short intro sentence or small paragraph where it helps, PLUS bullet points where each bullet bolds the **key idea / term** and then explains it in 1-2 clear sentences.
  • You may also use a "### Sub-heading" to group related points.
- Capture ALL details either way — do not compress. You MAY bold **key names, terms, dates, numbers**.`;
const NOTES_QUALITY_RULE = `- HINDI QUALITY (when writing Hindi): use pure, correct, natural Hindi with PERFECT spelling and grammar — no typos, no broken or half-English sentences, correct मात्राएँ and genders. Prefer simple everyday words.
- STYLE FOR ALL AGES: write so a school student, a child, and an elderly reader all find it easy and interesting — clear, warm and readable.`;
const FULL_NOTES_FIRST_PROMPT = `You are writing COMPREHENSIVE, detailed notes for a video, capturing EVERYTHING taught — miss nothing. This is the FIRST part of the transcript.

- Begin with "# <Concise, clear Topic Title>" then a 2-3 sentence introduction PARAGRAPH about the whole video.
- Then cover THIS part in "## Heading" sections, formatted per the rule below.
${NOTES_FORMAT_RULE}
${NOTES_QUALITY_RULE}
- Do not invent facts. Do NOT write a final conclusion — more parts follow.`;
const FULL_NOTES_SEGMENT_PROMPT = `Continue the COMPREHENSIVE, detailed notes for the SAME video. This is a LATER part of its transcript.

- Write notes for THIS part only, continuing naturally from earlier parts, in "## Heading" sections formatted per the rule below. Capture EVERY concept, example, definition, step and detail — miss nothing, do not compress.
- Do NOT repeat the title or an overall intro. Do NOT add a final wrap-up unless this is clearly the very end.
${NOTES_FORMAT_RULE}
${NOTES_QUALITY_RULE}
- Do not invent facts.`;

// ---- Full-notes tuning ----------------------------------------------------
// How many chunks are sent to Ollama at the same time.
//
// This can never be faster than the server allows: Ollama runs
// OLLAMA_NUM_PARALLEL requests at once and queues the rest, so setting this
// HIGHER than that number buys nothing. The default here matches
// OLLAMA_NUM_PARALLEL=4.
//
// The one trade-off: while a PDF is building it occupies these slots, so a chat
// message or a summary started at the same time waits behind them. If that
// bothers you, set it to 3 on the Options page and one slot always stays free
// for interactive use.
const NOTES_CONCURRENCY_DEFAULT = 4;

async function notesConcurrency() {
  try {
    const s = await browser.storage.sync.get("notesConcurrency");
    const n = parseInt(s && s.notesConcurrency, 10);
    if (n >= 1 && n <= 8) return n;
  } catch (_) {}
  return NOTES_CONCURRENCY_DEFAULT;
}
// Attempts per chunk before it is reported as missing (was 2).
const NOTES_CHUNK_RETRIES = 3;
// Per-chunk ceiling. Must comfortably exceed num_predict / your model's tok/s.
const NOTES_CHUNK_TIMEOUT_MS = 300000;
// Google's translate endpoint takes the text in the URL, so pieces must stay
// small; these run in parallel batches instead of one after another.
const TRANSLATE_PIECE_CHARS = 1500;
const TRANSLATE_CONCURRENCY = 4;

function detectTranscriptLang(text) {
  const dev = (text.match(/[ऀ-ॿ]/g) || []).length;
  const lat = (text.match(/[A-Za-z]/g) || []).length;
  return dev > Math.max(20, lat * 0.15) ? "hi" : "en";
}
function langDirective(lang) {
  return lang === "hi"
    ? `ABSOLUTE LANGUAGE RULE: Write the ENTIRE output in natural, correct Hindi using Devanagari (शुद्ध, सरल हिंदी). Every heading, label and sentence must be in Hindi. Do NOT write English sentences (well-known proper nouns / technical terms may keep their usual form). Correct spelling, grammar, मात्राएँ and genders.`
    : `ABSOLUTE LANGUAGE RULE: Write the ENTIRE output in clear English. Do NOT use any Hindi or Devanagari anywhere — not even a single word or heading. The source is English; keep everything in English.`;
}
function splitIntoChunks(text, size, overlap) {
  const chunks = [];
  let i = 0;
  while (i < text.length) {
    let end = Math.min(text.length, i + size);
    if (end < text.length) {
      const slice = text.slice(i, end);
      const cut = Math.max(slice.lastIndexOf(". "), slice.lastIndexOf("। "), slice.lastIndexOf(" "));
      if (cut > size * 0.6) end = i + cut + 1;
    }
    chunks.push(text.slice(i, end).trim());
    if (end >= text.length) break;
    i = Math.max(0, end - overlap);
  }
  return chunks;
}

// One chunk → its notes. Non-streaming (we only need per-chunk progress), with a
// generous timeout so a cold/large model on the first chunk still succeeds.
async function ollamaChunkNotes(settings, chunk, systemPrompt) {
  const base = String(settings.ollamaUrl || "").replace(/\/+$/, "");
  const controller = new AbortController();
  // 120s was wrong: a chunk may write up to num_predict tokens, and
  // 3000 tokens / 120 s = 25 tok/s. Any model slower than that had EVERY full
  // chunk aborted, retried (another 120s wasted) and then dropped - which is
  // both the slowness and the missing sections. 300s matches the backend's
  // OLLAMA_TIMEOUT_SECONDS.
  const to = setTimeout(() => { try { controller.abort(); } catch (_) {} }, NOTES_CHUNK_TIMEOUT_MS);
  try {
    const res = await fetch(`${base}/api/chat`, {
      method: "POST",
      headers: await ollamaHeaders(),
      body: JSON.stringify({
        model: settings.ollamaModel,
        messages: [
          { role: "system", content: systemPrompt },
          { role: "user", content: chunk },
        ],
        stream: false,
        keep_alive: -1,
        options: {
          temperature: 0.4,
          top_p: 0.9,
          repeat_penalty: 1.15,
          num_predict: 3000,
          // WITHOUT THIS the notes silently lose their beginning. Ollama's
          // default context is 4096 tokens; a 6000-char chunk (~1500-2000
          // tokens) + this system prompt (~400) + up to 3000 output tokens
          // does not fit, so Ollama drops the OLDEST tokens - i.e. the start
          // of the very chunk we asked it to write notes for. Every other
          // call in this extension already sets 8192; this one did not.
          num_ctx: 8192,
        },
      }),
      signal: controller.signal,
    });
    if (!res.ok) throw new Error(`Ollama HTTP ${res.status}`);
    const data = await res.json();
    return (data && data.message && data.message.content) || "";
  } finally {
    clearTimeout(to);
  }
}

// Skip translation when the notes are already in the requested language.
function pdfSameLang(target, srcLang) {
  const t = String(target || "").toLowerCase();
  if (srcLang === "hi") return t === "hi";
  if (srcLang === "en") return t === "en";
  return false;
}

// One-shot non-streaming Ollama completion (short outputs like follow-up Qs).
async function ollamaOneShot(msg) {
  const base = String(msg.url || "").replace(/\/+$/, "");
  const controller = new AbortController();
  const to = setTimeout(() => { try { controller.abort(); } catch (_) {} }, 30000);
  try {
    const res = await fetch(`${base}/api/chat`, {
      method: "POST",
      headers: await ollamaHeaders(),
      body: JSON.stringify({
        model: msg.model,
        messages: [
          { role: "system", content: msg.system || "" },
          { role: "user", content: msg.content || "" },
        ],
        stream: false,
        keep_alive: -1,
        options: Object.assign({ temperature: 0.6, top_p: 0.9, num_predict: 220 }, msg.options || {}),
      }),
      signal: controller.signal,
    });
    if (!res.ok) throw new Error(`Ollama HTTP ${res.status}`);
    const data = await res.json();
    return (data && data.message && data.message.content) || "";
  } finally {
    clearTimeout(to);
  }
}

function pdfKeepalive(on) {
  try {
    if (on) browser.alarms.create("pdf-keepalive", { periodInMinutes: 0.5 });
    else browser.alarms.clear("pdf-keepalive");
  } catch (_) {}
}

let _pdfRunning = false;
async function runPdfJob() {
  if (_pdfRunning) return;
  const { pdfJob } = await browser.storage.local.get("pdfJob");
  if (!pdfJob || !pdfJob.active) return;
  _pdfRunning = true;
  pdfKeepalive(true);
  try {
    const job = pdfJob;
    let notes = "";

    const done = await TrialGuard.streamNotes({
      url: job.url,
      transcript: job.transcript || null,
      targetLang: job.targetLang || null,
      onEvent: async (ev) => {
        if (!ev) return;
        if (ev.type === "meta" && ev.job_id) {
          // The job is already recorded as extension activity by the backend;
          // save its id so the completed browser PDF can be acknowledged.
          job.auditJobId = ev.job_id;
        } else if (ev.type === "progress") {
          // The first real generation event means analysis is finished and
          // note writing has started. Never advance to Preparing PDF here.
          job.stage = 3;
          job.pct = Math.max(5, Math.min(96, Number(ev.percent || 0)));
          job.doneChunks = Number(ev.done || 0);
          job.totalChunks = Number(ev.total || 0);
          if (ev.text || ev.preview || ev.partial) job.preview = String(ev.text || ev.preview || ev.partial).slice(-6000);
          job.label = `Writing detailed notes… ${ev.done || 0}/${ev.total || 0}`;
        } else if (ev.type === "status" && ev.message) {
          // Status messages can arrive while chunks are still being written.
          // Keep step 3 active until streamNotes has fully returned.
          job.stage = 3;
          job.pct = Math.max(job.pct || 0, 96);
          job.label = `🌐 ${ev.message}`;
        } else if (ev.type === "warning" && ev.message) {
          job.warning = ev.message;
          job.label = `⚠ ${ev.message}`;
        } else if (ev.type === "done") {
          notes = ev.text || notes;
          job.preview = String(notes || "").slice(-6000);
        }
        job.ts = Date.now();
        await browser.storage.local.set({ pdfJob: job });
      },
    });

    if (done && done.text) notes = done.text;
    notes = String(notes || "").trim();
    if (notes.length < 40) throw new Error("vLLM returned no usable notes");

    // All note chunks are complete. Only now may step 3 turn green and
    // step 4 become the active spinning step.
    job.stage = 4;
    job.pct = 98;
    job.label = "📄 Building the PDF…";
    job.ts = Date.now();
    await browser.storage.local.set({ pdfJob: job });

    await downloadSummary(job.title, job.url, notes);
    await openPrintTab("export-" + Date.now(), job.title, job.url, notes);
    // The extension creates the printable document locally. This authenticated
    // acknowledgement lets the admin dashboard count it as a completed PDF.
    await TrialGuard.markPdfReady(job.auditJobId).catch(() => {});
    await browser.storage.local.set({
      pdfJob: {
        active: false, done: true, pct: 100, stage: 5,
        label: "✅ Full notes ready — Markdown downloaded & print tab opened (Save as PDF there).",
        ts: Date.now(),
      },
    });
  } catch (e) {
    await browser.storage.local.set({
      pdfJob: {
        active: false, error: true, pct: 100, stage: Number((pdfJob && pdfJob.stage) || 1),
        label: `⚠ Couldn't create the PDF: ${(e && e.message) || "vLLM request failed"}`,
        ts: Date.now(),
      },
    });
  } finally {
    _pdfRunning = false;
    pdfKeepalive(false);
  }
}

browser.alarms.onAlarm.addListener((a) => {
  if (a && a.name === "pdf-keepalive") runPdfJob(); // wake + resume if suspended mid-job
});
browser.runtime.onStartup.addListener(() => { runPdfJob(); });
// On every worker wake, resume an interrupted job (no-op if none / already running).
runPdfJob();

// ---- Direct Ollama streaming (fast, token-by-token summary) ----
// The side panel opens a port and sends { type:"START", url, model, system,
// content }. We fetch Ollama's /api/chat with stream:true and relay each token
// back over the port so the side panel can render it live. Runs in the service
// worker (privileged) so it can reach http://…:11434 despite the YouTube page
// being https (mixed-content rules don't apply to the extension background).
browser.runtime.onConnect.addListener((port) => {
  if (port.name !== "ollama-stream") return;

  // If the side panel disconnects (e.g. it timed out, or the user navigated),
  // stop everything: abort the fetch and don't post to the dead port.
  let alive = true;
  const controller = new AbortController();
  port.onDisconnect.addListener(() => {
    alive = false;
    try { controller.abort(); } catch (_) {}
  });
  const post = (m) => {
    if (!alive) return;
    try { port.postMessage(m); } catch (_) { alive = false; }
  };

  port.onMessage.addListener(async (msg) => {
    if (!msg || msg.type !== "START") return;
    const base = String(msg.url || "").replace(/\/+$/, "");
    try {
      // If the side panel supplied a full messages[] array (chat with history),
      // use it as-is; otherwise build the classic system+user pair (summary).
      const messages = Array.isArray(msg.messages) && msg.messages.length
        ? msg.messages
        : [
            { role: "system", content: msg.system || "" },
            { role: "user", content: msg.content || "" },
          ];
      const res = await fetch(`${base}/api/chat`, {
        method: "POST",
        headers: await ollamaHeaders(),
        body: JSON.stringify(Object.assign(
          {
            model: msg.model,
            messages,
            stream: true,
            keep_alive: -1, // keep the model warm so later summaries are instant
            // Base options; the side panel can override any of them per request
            // (e.g. a much larger num_predict for a full, uncut translation).
            options: Object.assign(
              {
                temperature: 0.4,
                top_p: 0.9,
                repeat_penalty: 1.15, // light — enough to avoid loops, not so high it hurts prose
                num_predict: 1500, // room for an elaborate, multi-section summary
              },
              (msg && msg.options) || {}
            ),
          },
          // Reasoning models (Sarvam) generate a hidden <think> block before the
          // answer — that DOUBLES generation time. When the side panel sends
          // think:false we tell Ollama to skip reasoning → much faster, answer
          // streams directly. (Ignored by models that don't support thinking.)
          typeof msg.think === "boolean" ? { think: msg.think } : {}
        )),
        signal: controller.signal,
      });
      if (!res.ok || !res.body) {
        throw new Error(`Ollama HTTP ${res.status}`);
      }
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      while (alive) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let nl;
        while ((nl = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, nl).trim();
          buf = buf.slice(nl + 1);
          if (!line) continue;
          let obj;
          try {
            obj = JSON.parse(line);
          } catch {
            continue;
          }
          const tok = obj.message && obj.message.content;
          if (tok) post({ type: "DELTA", text: tok });
          if (obj.done) {
            post({ type: "DONE" });
            return;
          }
        }
      }
      post({ type: "DONE" });
    } catch (e) {
      if (e && e.name === "AbortError") return; // panel gave up — silent
      post({ type: "ERROR", error: (e && e.message) || String(e) });
    }
  });
});
