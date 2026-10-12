const $ = (id) => document.getElementById(id);

const STATES = ["notConfigured", "notPage", "chatState", "tgGate"];
function showState(name) {
  STATES.forEach((s) => $(s).classList.toggle("hidden", s !== name));
}

// Summary, notes, chat and translation are all handled by the authenticated
// TubeNotes API. There is no separately configurable model or notebook server.
const BUILT_IN_DEFAULTS = {};

// Short instruction for STAGE 1 of the two-stage summary — small output so it
// comes back fast and the user has something to read almost immediately.
const DEFAULT_SHORT_SUMMARY_INSTRUCTION = `Summarize this video transcript in English only.
- 5 short bullet points, most important first. Start each with a fitting emoji icon.
- Each bullet max 14 words. Be specific: names, numbers, key claims.
- No intro, no takeaway, no headings. Under 75 words total.
- Never use Devanagari or any other script.`;

// Default instruction used for the streamed on-the-spot summary. Kept close to
// the server-side "quick_summary" transformation so quality is unchanged — the
// only difference is that this streams live instead of arriving all at once.
// Override it in Options if you want a different style/length.
const DEFAULT_SUMMARY_INSTRUCTION = `Summarize this video transcript.

## 🎯 Overview
2-3 sentences capturing what this video is about and its overall purpose.

## 📌 Key Points
A numbered list of the main points, in the order they're discussed (typically 4-7 points). For each: bold the core idea in 3-6 words, then explain it in 1-2 sentences. Be specific — names, examples, numbers, claims — not vague generalizations.

## 💡 Takeaway
1-2 sentences on what the viewer should remember or do.

Rules:
- Never include timestamps or time ranges — the transcript has no reliable timing.
- Keep it concise: under 200 words (excluding headers) for videos up to 30 min; up to 300 words for longer videos.
- No filler like "the video discusses" — straight to substance. Simple, clear language.
- Respond in English only, regardless of the video's language (Hindi/Hinglish/etc.). Translate and summarize into natural English. Never use Devanagari or any other script.`;

// Poll cadence for the "waiting" loops. Short intervals + an immediate first
// check remove most of the dead-time that used to dominate the wait.
const SOURCE_POLL_MS = 350;
const INSIGHT_POLL_MS = 400;

// ---- Direct-Ollama streaming summary ----
// System prompt for the streamed summary — a detailed, structured write-up
// (overview + thematic sections + explained points), in Markdown.
const OLLAMA_SUMMARY_PROMPT = `Write a CONCISE but complete summary of the video from the content below, in Markdown. TARGET LENGTH: about 350-400 words TOTAL (never more than ~420 words). Be selective — capture the MAIN parts of the whole video from start to end; do not list every tiny detail.

Use EXACTLY this structure. If writing in Hindi, translate the fixed labels too (Overview→अवलोकन, "Key Point"→मुख्य बिंदु, "Background Information"→पृष्ठभूमि, "Specific Details"→विशेष विवरण, "Conclusion & Key Takeaways"→निष्कर्ष और मुख्य बातें, "In summary"→संक्षेप में):

## Overview
A 3-5 sentence paragraph on what the whole video is about.

Then 4 to 7 sections (NOT more). For each, YOU choose a real, short, descriptive title (for example "## Early Life & Education", "## Impact on Nature"). NEVER output the literal words "Section Title" or any angle-bracket placeholder — always write a real title:

## <your real descriptive title>
**Key Point:** one sentence.

- **Background Information:** 1-2 sentences of context.
- **Specific Details:** 1-2 sentences with the real names, numbers and facts.

Then finish with:

## Conclusion & Key Takeaways
**Key Point:** one sentence.

- **Specific Details:** 1-2 sentences on the key takeaways.

**In summary,** a 2-3 sentence closing paragraph.

Strict rules:
- Keep the WHOLE summary around 350-400 words. Choose only the 4-7 most important sections; merge related topics; do NOT repeat the same point in multiple sections.
- Be FAITHFUL and ACCURATE: real names, events, dates and numbers exactly; never confuse two people/events, never invent.
- Same language as the content (LANGUAGE RULE at the top): Hindi → natural Hindi in Devanagari with Hindi labels; English → English.
- NEVER include caption noise like "[Music]"/"[संगीत]" or anything in square brackets.
- Start directly with "## Overview". Always use REAL section titles, never a placeholder.`;

// ---- Full, comprehensive PDF notes (chunked — covers the WHOLE video) ----
// The transcript is split into chunks and each is turned into detailed notes,
// then joined. This way nothing is dropped even for a 1-2 hour video.
// LANGUAGE: the PDF notes follow the video's own language — Hindi/Hinglish video
// → notes in correct Hindi (Devanagari); English video → English. Never translate.
const NOTES_LANG_RULE = `- LANGUAGE: Write in the SAME language as the transcript. If the transcript is Hindi or Hinglish, write the notes in natural Hindi using Devanagari (हिन्दी) — do NOT translate to English. If the transcript is English, write in English. Keep well-known proper nouns / technical terms in their usual form.`;

// Quality bar: audience is mixed (school students, children AND older readers).
const NOTES_QUALITY_RULE = `- HINDI QUALITY (when writing Hindi): use pure, correct, natural Hindi with PERFECT spelling and grammar — no typos, no broken or half-English sentences, correct मात्राएँ and genders. Prefer simple everyday words. Re-read each sentence mentally and fix any mistake before writing it.
- STYLE FOR ALL AGES: write so a school student, a child, and an elderly reader all find it easy and interesting — clear, warm and readable.`;

// FORMAT: pick the shape from the CONTENT TYPE of the video.
//  - Stories / narratives (kahani, history tale, biography told as a story)
//    → flowing PARAGRAPHS under each heading (no bullets).
//  - Informational (travel, documentary, a subject/lesson, tutorial, list,
//    news, podcast facts) → headings + a short intro line where useful + BULLET
//    points, each bolding the **key idea** then explaining it. Mix paragraphs
//    and bullets as fits.
const NOTES_FORMAT_RULE = `- CHOOSE THE FORMAT FROM THE CONTENT TYPE:
  • If this is a STORY / narrative / anecdote (कहानी, a historical tale, a biography told as a story): write each "## Heading" followed by flowing detailed PARAGRAPHS (3-6 sentences each). Do NOT use bullet points for story content — tell it as prose.
  • If this is INFORMATIONAL (travel, documentary, a school subject/lesson, tutorial, how-to, news, a facts/points podcast, comparisons, lists): under each "## Heading" use a mix — a short intro sentence or small paragraph where it helps, PLUS bullet points where each bullet bolds the **key idea / term** and then explains it in 1-2 clear sentences. Use paragraphs for narrative parts and bullets for distinct facts, points or steps.
  • You may also use a "### Sub-heading" to group related points.
- Capture ALL details either way — do not compress. You MAY bold **key names, terms, dates, numbers**.`;

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

// Detect the transcript's language ONCE (by script), so every chunk is forced
// into the SAME language — otherwise chunks disagree and the PDF comes out half
// Hindi, half English. Devanagari present in meaningful amount → Hindi; else
// English (covers English and romanised transcripts).
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

// For the ON-SCREEN summary: make the model write in the transcript's OWN
// language (Spanish video → Spanish summary, French → French, …), not force
// English. Hindi (Devanagari) still gets the strong Hindi-quality directive.
function langDirectiveAuto(text) {
  if (detectTranscriptLang(text || "") === "hi") return langDirective("hi");
  return `ABSOLUTE LANGUAGE RULE: Write the ENTIRE summary in the SAME language as the transcript below. Detect the transcript's language and use ONLY that language for every heading, label and sentence (e.g. a Spanish transcript → Spanish summary, French → French, German → German, English → English). Do NOT translate into English if the transcript is not English. Use correct, natural grammar and spelling in that language. Well-known proper nouns / technical terms may keep their usual form.`;
}

// Google-Translate language codes → readable names, so we can name the target
// language EXPLICITLY in the directive ("Write in French") — gemma2 follows a
// named language far more reliably than a vague "same language" instruction.
const LANG_CODE_NAMES = {
  en: "English", hi: "Hindi", fr: "French", es: "Spanish", de: "German",
  it: "Italian", pt: "Portuguese", ru: "Russian", ar: "Arabic", ur: "Urdu",
  fa: "Persian", tr: "Turkish", nl: "Dutch", pl: "Polish", uk: "Ukrainian",
  vi: "Vietnamese", th: "Thai", id: "Indonesian", ms: "Malay", he: "Hebrew",
  el: "Greek", ja: "Japanese", ko: "Korean", zh: "Chinese", bn: "Bengali",
  gu: "Gujarati", kn: "Kannada", ml: "Malayalam", mr: "Marathi", pa: "Punjabi",
  ta: "Tamil", te: "Telugu", or: "Odia", as: "Assamese", ne: "Nepali",
  si: "Sinhala", sw: "Swahili", ro: "Romanian", cs: "Czech", sv: "Swedish",
  hu: "Hungarian", fi: "Finnish", da: "Danish", no: "Norwegian",
};

// Detect the summary language locally. The backend performs the authoritative
// language detection for generation and translation.
async function detectSummaryLangName(text) {
  if (detectTranscriptLang(text || "") === "hi") return "Hindi";
  return "English";
}

// Languages gemma2 writes WELL natively. For these we generate directly (fast).
// Everything else (Marathi, Gujarati, Tamil, Kannada, Malayalam, Punjabi,
// Telugu, Bengali, Urdu…) — the model drifts to English or writes broken text,
// so we generate in English and TRANSLATE to that language with Google.
const STRONG_SUMMARY_LANGS = new Set([
  "en", "hi", "es", "fr", "de", "it", "pt", "ru", "ja", "zh", "ar", "nl", "tr", "ko", "id", "vi",
]);

// Regional Indian languages → use the Indic-strong Sarvam model (native, good
// quality). Hindi is deliberately NOT here (gemma2 handles Hindi well + faster).
const SARVAM_LANGS = new Set([
  "mr", "gu", "pa", "ta", "te", "kn", "ml", "bn", "or", "as", "ur", "sd", "ne", "kok", "mai",
]);
// Regional Indic model. Sarvam-M (24B) — best native quality for regional Indian
// languages. Speed levers applied: (1) a Q4_K_M build (~14GB vs ~25GB) → ~1.8x
// faster tokens; (2) NO_THINK below disables its hidden <think> reasoning block
// (which otherwise doubles generation) → answer streams directly. Together these
// bring Sarvam close to gemma2:9b speed while keeping its quality.
const SARVAM_MODEL_DEFAULT = "sarvam-m-q4";

// NOTE: we tried sending think:false to skip Sarvam's reasoning (for speed), but
// this Q4 build's /api/chat returns HTTP 400 on the think param (the CLI flag was
// lenient; the API is strict). So we DON'T send it — thinking stays on and is
// hidden during streaming (reasoningModel=true) + stripped from the final text.
// To actually skip thinking for extra speed, disable it at the MODEL level with a
// Modelfile instead. Keep this false so no think param is sent (avoids the 400).
const NO_THINK = false;

// A reasoning model writes an internal <think>…</think> block (usually English)
// BEFORE the answer. We must hide that while streaming. Navarasa does NOT; Sarvam
// does. Key off the model NAME so switching models Just Works either way.
function isReasoningModel(name) {
  return /sarvam|deepseek-r1|qwq|reason|think/i.test(String(name || ""));
}

// Detect the transcript language locally. Devanagari is Hindi as a safe UI
// fallback; the TubeNotes backend uses a fuller detector when generating.
async function detectSummaryLang(text) {
  const code = /[ऀ-ॿ]/.test(text || "") ? "hi" : "en";
  return { code, name: LANG_CODE_NAMES[code] || null, strong: STRONG_SUMMARY_LANGS.has(code) };
}

// Instant, offline language guess from the writing script. Only returns a result
// for scripts that map to exactly ONE language (so no network detect is needed).
// Devanagari is deliberately excluded (Hindi vs Marathi share it) and Latin too
// (English vs Spanish/French/…). Requires a decent number of script characters
// so a stray glyph in an English transcript doesn't trip it.
function quickScriptDetect(text) {
  const s = String(text || "");
  const RANGES = [
    [/[઀-૿]/g, "gu", "Gujarati"],
    [/[਀-੿]/g, "pa", "Punjabi"],
    [/[஀-௿]/g, "ta", "Tamil"],
    [/[ఀ-౿]/g, "te", "Telugu"],
    [/[ಀ-೿]/g, "kn", "Kannada"],
    [/[ഀ-ൿ]/g, "ml", "Malayalam"],
    [/[ঀ-৿]/g, "bn", "Bengali"],
    [/[଀-୿]/g, "or", "Odia"],
  ];
  for (const [re, code, name] of RANGES) {
    const m = s.match(re);
    if (m && m.length >= 15) return { code, name, strong: false };
  }
  return null;
}

// Build a strict directive that names the language explicitly.
function langDirectiveNamed(name) {
  if (!name) {
    return `ABSOLUTE LANGUAGE RULE: Write the ENTIRE summary in the SAME language as the transcript below. Do NOT translate to English if the transcript is not English.`;
  }
  if (name === "Hindi") return langDirective("hi");
  if (name === "English") return langDirective("en");
  return `ABSOLUTE LANGUAGE RULE: The transcript is in ${name}. Write the ENTIRE summary in ${name} ONLY. Every heading, label and sentence MUST be in ${name}. Do NOT write in English or any other language. Use correct, natural ${name} grammar and spelling. Well-known proper nouns / technical terms may keep their usual form.`;
}

// Localised fixed labels used in the structured summary format.
function summaryLabels(lang) {
  return lang === "hi"
    ? { overview: "अवलोकन", keyPoint: "मुख्य बिंदु", background: "पृष्ठभूमि", details: "विशेष विवरण", conclusion: "निष्कर्ष और मुख्य बातें", inSummary: "संक्षेप में" }
    : { overview: "Overview", keyPoint: "Key Point", background: "Background Information", details: "Specific Details", conclusion: "Conclusion & Key Takeaways", inSummary: "In summary" };
}

// Split text into ~size-char chunks, breaking on a sentence/space boundary near
// the end, with a small overlap so nothing falls between two chunks.
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

// Generate full notes for the whole transcript, chunk by chunk. onProgress is
// called as (completedChunks, totalChunks, charsInCurrentChunk).
async function generateFullNotes(settings, transcript, onProgress) {
  const chunks = splitIntoChunks(transcript, 6000, 250).slice(0, 30); // safety cap ~3h video
  // Decide the language ONCE for the whole PDF and force every chunk into it.
  const lang = detectTranscriptLang(transcript);
  const directive = langDirective(lang);
  console.log(`[ONS] PDF language locked to: ${lang}`);
  const parts = [];
  for (let idx = 0; idx < chunks.length; idx++) {
    const base = idx === 0 ? FULL_NOTES_FIRST_PROMPT : FULL_NOTES_SEGMENT_PROMPT;
    const prompt = directive + "\n\n" + base; // language directive first — highest priority
    const onProg = (t) => { if (onProgress) onProgress(idx, chunks.length, t.length); };
    // A single chunk failing (timeout / hiccup) must NOT kill the whole export.
    // Try once, retry once, then skip that chunk and carry on with the rest.
    const chunkOpts = { ollama: { num_predict: 3000 } }; // more room so a chunk's notes aren't cut off
    let seg = "";
    try {
      seg = await collectOllamaText(settings, chunks[idx], prompt, onProg, chunkOpts);
    } catch (e) {
      console.warn(`[ONS] notes chunk ${idx + 1}/${chunks.length} failed (${e && e.message}) — retrying once`);
      try {
        seg = await collectOllamaText(settings, chunks[idx], prompt, onProg, chunkOpts);
      } catch (e2) {
        console.warn(`[ONS] notes chunk ${idx + 1} failed again — skipping it`);
        seg = "";
      }
    }
    parts.push((seg || "").trim());
    if (onProgress) onProgress(idx + 1, chunks.length, 0);
  }
  return parts.filter(Boolean).join("\n\n");
}

// Clean caption noise from a transcript before summarising: YouTube captions
// insert sound annotations like "[Music]", "[Applause]", "[संगीत]", "[हंसी]" in
// square brackets. Strip those (short bracketed tags) and tidy whitespace, so
// they never leak into the summary or PDF.
function cleanTranscript(text) {
  if (!text) return text;
  return text
    .replace(/\[[^\]\n]{0,40}\]/g, " ") // [Music] / [संगीत] / [Applause] / [हंसी] …
    .replace(/\s{2,}/g, " ")
    .trim();
}

// Very long transcripts make even a fast model slow. Sample evenly across the
// whole video so we keep coverage of beginning/middle/end within a budget.
const OLLAMA_MAX_CHARS = 12000;
function capForOllama(text) {
  if (!text || text.length <= OLLAMA_MAX_CHARS) return text;
  // Use a FEW large contiguous blocks (not many tiny fragments). Big contiguous
  // pieces keep the narrative connected so the model doesn't confuse people /
  // events; tiny scattered fragments were causing garbled, mixed-up facts.
  const parts = 4;
  const sliceLen = Math.floor(OLLAMA_MAX_CHARS / parts);
  const step = Math.floor(text.length / parts);
  const out = [];
  for (let i = 0; i < parts; i++) out.push(text.substr(i * step, sliceLen).trim());
  return out.join("\n…\n");
}

// Minimal, safe Markdown → HTML for the summary bubble: headings, **bold**,
// bullet and numbered lists, paragraphs. Everything is escaped first, then only
// bold/list/heading structure is re-introduced — no raw HTML from the model.
// Reasoning models (e.g. Sarvam-M) write their chain-of-thought in a
// <think>…</think> block (usually English) BEFORE the answer. Strip it so only
// the real answer is shown/stored. Handles: complete blocks, a still-open block
// mid-stream (hide everything from <think> on), and a lone closing </think>
// (some models omit the opening tag) → keep only what's after the last </think>.
function stripThink(s) {
  if (!s) return s;
  let out = s.replace(/<think>[\s\S]*?<\/think>/gi, ""); // complete blocks
  const closeIdx = out.lastIndexOf("</think>");
  if (closeIdx >= 0) out = out.slice(closeIdx + "</think>".length); // lone close
  const openIdx = out.search(/<think>/i);
  if (openIdx >= 0) out = out.slice(0, openIdx); // still-open block → hide it
  return out.trim();
}

function mdToHtmlLite(md) {
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const inline = (s) => esc(s).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  const lines = (md || "").split("\n");
  let html = "";
  let listTag = null;
  const closeList = () => { if (listTag) { html += `</${listTag}>`; listTag = null; } };
  for (const raw of lines) {
    const line = raw.trim();
    if (!line) { closeList(); continue; }
    let m;
    if ((m = line.match(/^(#{1,6})\s+(.*)$/))) {
      closeList();
      const cls = m[1].length <= 1 ? "md-h md-h1" : "md-h";
      html += `<div class="${cls}">${inline(m[2])}</div>`;
    } else if (/^\d+[.)]\s+/.test(line)) {
      if (listTag !== "ol") { closeList(); html += `<ol style="margin:4px 0 4px 20px;">`; listTag = "ol"; }
      html += `<li style="margin:3px 0;">${inline(line.replace(/^\d+[.)]\s+/, ""))}</li>`;
    } else if (/^[-*•]\s+/.test(line)) {
      if (listTag !== "ul") { closeList(); html += `<ul style="margin:4px 0 4px 20px;">`; listTag = "ul"; }
      html += `<li style="margin:3px 0;">${inline(line.replace(/^[-*•]\s+/, ""))}</li>`;
    } else {
      closeList();
      html += `<div style="margin:4px 0;">${inline(line)}</div>`;
    }
  }
  closeList();
  return html;
}

// Collect a full completion from Ollama (via the background worker) without
// rendering to the chat — used for the PDF export. onProgress(text) is called
// as it streams so callers can show progress. Resolves with the full text.
function collectOllamaText(settings, transcript, systemPrompt, onProgress, options) {
  return new Promise((resolve, reject) => {
    let port;
    try { port = browser.runtime.connect({ name: "ollama-stream" }); }
    catch (e) { reject(e); return; }
    let text = "";
    let started = false;
    let settled = false;
    const to = setTimeout(() => {
      if (!started && !settled) { settled = true; try { port.disconnect(); } catch (_) {} reject(new Error("Ollama: no response in time")); }
    }, 120000); // generous — a cold / large model (e.g. gpt-oss:20b) can take a while to load on the first chunk
    port.onMessage.addListener((m) => {
      if (!m) return;
      if (m.type === "DELTA") {
        started = true;
        clearTimeout(to);
        text += m.text;
        if (onProgress) onProgress(text);
      } else if (m.type === "DONE") {
        if (settled) return;
        settled = true;
        clearTimeout(to);
        try { port.disconnect(); } catch (_) {}
        text ? resolve(text) : reject(new Error("Ollama: empty response"));
      } else if (m.type === "ERROR") {
        if (started) { if (!settled) { settled = true; resolve(text); } }
        else if (!settled) { settled = true; clearTimeout(to); try { port.disconnect(); } catch (_) {} reject(new Error(m.error || "Ollama error")); }
      }
    });
    port.onDisconnect.addListener(() => {
      if (!settled && !started) { settled = true; reject(new Error("Ollama: disconnected")); }
    });
    port.postMessage({
      type: "START",
      url: settings.ollamaUrl,
      model: settings.ollamaModel,
      system: systemPrompt,
      content: options && options.noCap ? transcript : capForOllama(transcript),
      options: options && options.ollama ? options.ollama : undefined,
    });
  });
}

// Stream a summary directly from Ollama (via the background worker), rendering
// tokens live. Resolves with the full text, or rejects if it can't start (so
// the caller falls back to the server-side transformation path).
function streamVllmSummary(settings, transcript, videoUrl) {
  return new Promise(async (resolve, reject) => {
    const bubble = appendThinking("Summarizing…");
    const box = $("messages");
    const t0 = performance.now();
    let text = "";
    let started = false;
    let settled = false;
    let renderTimer = null;
    let renderDirty = false;

    const paint = () => {
      renderDirty = false;
      bubble.innerHTML = text
        ? mdToHtmlLite(text)
        : `<span style="color:#9ec0e2;font-style:italic;">Summarizing…</span>`;
      box.scrollTop = box.scrollHeight;
    };
    const scheduleRender = () => {
      renderDirty = true;
      if (renderTimer) return;
      paint();
      renderTimer = setTimeout(function tick() {
        if (renderDirty) { paint(); renderTimer = setTimeout(tick, 80); }
        else renderTimer = null;
      }, 80);
    };

    try {
      const done = await TrialGuard.streamSummary({
        url: videoUrl,
        transcript: transcript || null,
        mode: "summary",
        onEvent: async (ev) => {
          if (!ev) return;
          if (ev.type === "delta" && ev.text) {
            if (!started) {
              started = true;
              bubble.classList.remove("thinking");
              bubble.classList.add("assistant");
              console.log(`[ONS] vLLM first token in ${Math.round(performance.now() - t0)}ms`);
            }
            text += ev.text;
            scheduleRender();
          } else if (ev.type === "status" && !started && ev.message) {
            bubble.textContent = ev.message;
          } else if (ev.type === "done") {
            if (ev.text) text = ev.text;
          }
        },
      });
      settled = true;
      if (renderTimer) clearTimeout(renderTimer);
      if (done && done.text) text = done.text;
      if (!text.trim()) throw new Error("vLLM returned an empty summary");
      bubble.classList.remove("thinking");
      bubble.classList.add("assistant");
      bubble.innerHTML = mdToHtmlLite(text);
      box.scrollTop = box.scrollHeight;
      console.log(`[ONS] vLLM summary done in ${Math.round(performance.now() - t0)}ms`);
      resolve(text);
    } catch (e) {
      if (renderTimer) clearTimeout(renderTimer);
      if (!settled && !text) bubble.remove();
      reject(e);
    }
  });
}

// Whole-video summary for LONG transcripts (15 min → 1 hr+). Reads the ENTIRE
// transcript chunk-by-chunk (nothing sampled away, nothing cut), extracts the
// key events/topics from each part as bullets, and assembles the final summary
// in the required shape: intro paragraph + "Key events include:" + bullets.
// Renders progressively so the user sees it fill in.
async function summarizeLongVideo(settings, transcript) {
  const lang = detectTranscriptLang(transcript);
  const dir = langDirectiveAuto(transcript); // summary in the transcript's own language
  const box = $("messages");
  const note = appendBubble("thinking", "Reading the whole video…");

  // 1) MAP: read the whole transcript chunk-by-chunk, pull a few short key points
  //    from each part (plain one-line bullets). This covers everything without
  //    generating a huge output.
  const chunks = splitIntoChunks(transcript, 6000, 200).slice(0, 24); // up to ~2.5h
  const mapSys =
    dir +
    `\n\nFrom the transcript part below, list 3-6 SHORT key points (one line each, as "- " bullets) capturing what actually happens/said in this part — with the real names, numbers and facts. Be faithful. Output ONLY the bullet lines. Never include any text inside square brackets like [Music]/[संगीत].`;
  // Fire ALL chunk reads at once (parallel) instead of one-by-one, so the whole
  // read finishes much faster.
  note.textContent = "Reading the whole video…";
  const mapResults = await Promise.all(
    chunks.map((chunk) =>
      collectOllamaText(settings, chunk, mapSys, null, { ollama: { num_predict: 500, repeat_penalty: 1.15 } }).catch(() => "")
    )
  );
  const noteLines = [];
  mapResults.forEach((b) => {
    (b || "").split("\n").map((l) => l.trim()).forEach((l) => {
      if (/^[-*]\s+/.test(l) && !looksRepetitive(l)) noteLines.push(l.replace(/^\*/, "-"));
    });
  });

  // 2) REDUCE: turn all those key points (which represent the WHOLE video) into
  //    ONE concise ~350-400 word structured summary in the required format.
  note.textContent = lang === "hi" ? "सारांश लिख रहे हैं…" : "Writing the summary…";
  const notesText = noteLines.join("\n") || capForOllama(transcript);
  const reduceSys =
    dir +
    "\n\nThe content below is a list of key points covering a whole video, in order. " +
    OLLAMA_SUMMARY_PROMPT;

  note.classList.remove("thinking");
  note.classList.add("assistant");
  let out = "";
  try {
    await collectOllamaText(settings, notesText, reduceSys, (t) => {
      out = t;
      note.innerHTML = mdToHtmlLite(t);
      box.scrollTop = box.scrollHeight;
    }, { noCap: true, ollama: { num_predict: 1600, repeat_penalty: 1.15 } });
  } catch (_) {}

  if (out) { note.innerHTML = mdToHtmlLite(out); return out.trim(); }
  note.remove();
  return "";
}

async function getSettings() {
  return {};
}

async function getJobs() {
  const { jobs = {} } = await browser.storage.local.get("jobs");
  return jobs;
}
async function saveJobs(jobs) {
  await browser.storage.local.set({ jobs });
}

function isSummarizableUrl(url) {
  if (!url) return false;
  try {
    const u = new URL(url);
    return u.protocol === "http:" || u.protocol === "https:";
  } catch {
    return false;
  }
}

function cleanTitle(tab) {
  return (tab?.title || tab?.url || "").replace(/ - YouTube$/, "");
}

async function getActiveTab() {
  const [tab] = await browser.tabs.query({ active: true, currentWindow: true });
  return tab;
}

// ---- Client-side transcript grab ----
// Ask the YouTube page's content script (transcript.js) to scrape the transcript
// straight from the "Show transcript" panel. Returns the plain text, or null if
// it can't (no captions, panel won't open, or the active tab isn't the watch
// page for this exact video — e.g. a thumbnail click on a listing page). Null
// makes the caller fall back to the server-side link extraction — so the worst
// case is exactly the old behaviour.
async function tryGetTranscript(videoUrl) {
  const vid = getYoutubeVideoId(videoUrl);
  if (!vid) return null;
  // Retry for a few seconds: a thumbnail Summarize NAVIGATES to the video, so the
  // new page + content script + player data take a moment to be ready. On the
  // watch page it succeeds on the first try (instant). The transcript is read
  // SILENTLY from the player data — the "Show transcript" panel is not opened.
  for (let i = 0; i < 10; i++) {
    const tab = await getActiveTab();
    if (tab && tab.id && /(^|\.)youtube\.com/.test(String(tab.url || ""))) {
      const sameVideo = getYoutubeVideoId(tab.url || "") === vid;
      try {
        const resp = await browser.tabs.sendMessage(
          tab.id,
          { type: sameVideo ? "GET_TRANSCRIPT" : "GET_TRANSCRIPT_ANY", videoId: vid },
          { frameId: 0 }
        );
        if (resp && resp.ok && resp.text && resp.text.length > 40) return resp.text;
      } catch (_) {}
    }
    await new Promise((r) => setTimeout(r, 1000));
  }
  return null;
}

function findJobForUrl(jobs, url) {
  for (const id of Object.keys(jobs)) {
    if (jobs[id].videoUrl === url) return { id, job: jobs[id] };
  }
  return null;
}

let fastPollTimer = null;
function stopFastPoll() {
  if (fastPollTimer) clearInterval(fastPollTimer);
  fastPollTimer = null;
}

let currentUrl = null;
// Remembers the last video we streamed a summary for, so a later refresh()
// re-renders it instead of re-running the whole prep flow (which caused a
// spurious "Couldn't start chat" error).
let streamedSummary = null; // { url, text }
let activeSummaryUrl = null; // URL currently being streamed (single-flight guard)
let activeStreamPort = null; // the live Ollama stream port, so we can cancel it
let genToken = 0; // bumps each prep; a stale stream's result is ignored
let overrideLockUrl = null;
let overrideLockUntil = 0;
// The page/video the panel is "pinned" to. Once the panel is showing something,
// a plain tab switch must NOT reset it — only a fresh Summarize click changes it.
let pinnedUrl = null;

// ---- Main entry point: refresh the panel for whichever tab is active ----
async function getPendingOverride() {
  const { pendingOverride } = await browser.storage.local.get("pendingOverride");
  if (!pendingOverride) return null;
  // Only honor overrides requested in the last 8 seconds (fresh click).
  if (Date.now() - pendingOverride.ts > 8000) return null;
  await browser.storage.local.remove("pendingOverride");
  return pendingOverride;
}

let refreshInFlight = false;
let refreshQueued = false;

async function refresh() {
  if (refreshInFlight) {
    refreshQueued = true;
    return;
  }
  refreshInFlight = true;
  try {
    await refreshInner();
  } finally {
    refreshInFlight = false;
    if (refreshQueued) {
      refreshQueued = false;
      refresh();
    }
  }
}

// ---- Video likes + views (read exactly from the YouTube page) ----
function getYoutubeVideoId(url) {
  try {
    const u = new URL(url);
    if (!/(^|\.)youtube\.com$/.test(u.hostname) && u.hostname !== "youtu.be") {
      return null;
    }
    if (u.hostname === "youtu.be") return u.pathname.slice(1) || null;
    if (u.pathname === "/watch") return u.searchParams.get("v");
    return null;
  } catch {
    return null;
  }
}

// Read the EXACT likes + views straight from the YouTube page (via the content
// script), so the side panel shows the same numbers the user sees on the video.
// No community dislike estimate anymore — only real likes and views.
async function fetchVideoStats(videoId) {
  for (let i = 0; i < 12; i++) {
    const tab = await getActiveTab();
    if (tab && tab.id && /(^|\.)youtube\.com/.test(String(tab.url || ""))) {
      try {
        const resp = await browser.tabs.sendMessage(tab.id, {
          type: "GET_VIDEO_STATS",
          videoId,
        });
        if (resp && (resp.likes || resp.views)) return resp;
      } catch (_) {
        // content script not ready yet — retry
      }
    }
    await new Promise((r) => setTimeout(r, 400));
  }
  return null;
}

// Attractive inline SVG icons (crisp at any zoom, colour-matched to the card).
const ICON_LIKE =
  '<svg class="cred-ic" viewBox="0 0 24 24" width="14" height="14" aria-hidden="true">' +
  '<path fill="#4ea1ff" d="M2 21h3V9H2v12zm20-11c0-1.1-.9-2-2-2h-6.31l.95-4.57.03-.32c0-.41-.17-.79-.44-1.06L13.17 1 6.59 7.59C6.22 7.95 6 8.45 6 9v10c0 1.1.9 2 2 2h9c.83 0 1.54-.5 1.84-1.22l3.02-7.05c.09-.23.14-.47.14-.73v-2z"/></svg>';
const ICON_VIEW =
  '<svg class="cred-ic" viewBox="0 0 24 24" width="14" height="14" aria-hidden="true">' +
  '<path fill="#c9a35a" d="M12 4.5C7 4.5 2.73 7.61 1 12c1.73 4.39 6 7.5 11 7.5s9.27-3.11 11-7.5C21.27 7.61 17 4.5 12 4.5zm0 12a4.5 4.5 0 1 1 0-9 4.5 4.5 0 0 1 0 9zm0-7a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5z"/></svg>';

function renderCredCard(data) {
  const el = $("credCard");
  if (!data || (!data.likes && !data.views)) {
    el.classList.add("hidden");
    return;
  }

  // ---- Approval, computed purely from likes & views (engagement rate). ----
  // No dislikes involved. A likes/views ratio of ~5% is treated as a top score,
  // which is where genuinely well-liked videos land.
  let approvalPct = null, cls = "high", label = "Well received";
  if (data.likesNum && data.viewsNum) {
    const ratio = data.likesNum / data.viewsNum;
    // Map engagement into a 70–100 band (never below 70). A likes/views ratio of
    // ~5%+ hits 100; 0% sits at the 70 floor; everything scales in between.
    const frac = Math.max(0, Math.min(1, ratio / 0.05));
    approvalPct = Math.round(70 + frac * 30); // 70 … 100
    if (approvalPct >= 85) { cls = "high"; label = "Well received"; }
    else { cls = "mid"; label = "Good engagement"; }
  }

  const stats = [];
  if (data.likes) stats.push(`<span class="cred-stat">${ICON_LIKE} ${data.likes}</span>`);
  if (data.views) stats.push(`<span class="cred-stat">${ICON_VIEW} ${data.views} views</span>`);

  const ring = approvalPct !== null ? credRingSVG(approvalPct, cls) : "";
  const top =
    approvalPct !== null
      ? `<div class="cred-top">
           <span class="cred-score ${cls}">${approvalPct}% Approval</span>
           <span class="cred-label ${cls}">⚡ ${label}</span>
         </div>`
      : "";

  el.innerHTML =
    `<div class="cred-row">` +
    (ring ? `<div class="cred-ring">${ring}</div>` : "") +
    `<div class="cred-main">${top}` +
    `<div class="cred-stats cred-statline">${stats.join("")}</div>` +
    `</div></div>`;
  el.classList.remove("hidden");
}

// A purple gradient approval ring (donut) with the % in the centre.
function credRingSVG(pct, cls) {
  const p = Math.max(0, Math.min(100, pct));
  const r = 20, c = 2 * Math.PI * r;
  const off = c * (1 - p / 100);
  const uid = "rg" + p;
  return (
    `<svg width="58" height="58" viewBox="0 0 58 58">` +
    `<defs><linearGradient id="${uid}" x1="0" y1="0" x2="1" y2="1">` +
    `<stop offset="0" stop-color="#6d5efc"/><stop offset="1" stop-color="#c05cf0"/></linearGradient></defs>` +
    `<circle cx="29" cy="29" r="${r}" fill="none" stroke="#2a2547" stroke-width="5"/>` +
    `<circle cx="29" cy="29" r="${r}" fill="none" stroke="url(#${uid})" stroke-width="5" stroke-linecap="round" ` +
    `stroke-dasharray="${c.toFixed(1)}" stroke-dashoffset="${off.toFixed(1)}" transform="rotate(-90 29 29)"/>` +
    `<text x="29" y="33" text-anchor="middle" font-size="13" font-weight="800" fill="#ffffff">${p}%</text>` +
    `</svg>`
  );
}

async function updateCredCard(url) {
  const videoId = getYoutubeVideoId(url);
  if (!videoId) {
    renderCredCard(null);
    return;
  }
  try {
    const data = await fetchVideoStats(videoId);
    if (currentUrl === url) renderCredCard(data);
  } catch {
    if (currentUrl === url) renderCredCard(null);
  }
}

async function refreshInner() {
  const tab = await getActiveTab();
  const settings = await getSettings();
  const override = await getPendingOverride();

  // If the panel document was reloaded (it auto-hid on a non-YouTube tab and
  // re-showed on return), restore the pin + last summary from storage so the
  // video's summary appears instantly instead of being re-generated.
  if (!streamedSummary) {
    try {
      const { lastStreamed } = await browser.storage.local.get("lastStreamed");
      if (lastStreamed && lastStreamed.text) streamedSummary = lastStreamed;
    } catch (_) {}
  }
  if (!pinnedUrl) {
    try {
      const { pinnedUrl: pu } = await browser.storage.local.get("pinnedUrl");
      if (pu) pinnedUrl = pu;
    } catch (_) {}
  }

  // A fresh Summarize click re-pins to that video.
  if (override) {
    pinnedUrl = override.url;
    try { browser.storage.local.set({ pinnedUrl }); } catch (_) {}
  }

  // Not a fresh click, and the active tab is NOT the pinned video (user switched
  // to another tab — YouTube or WhatsApp/anything). NEVER summarise the other
  // page — just keep showing the pinned video's summary.
  if (!override && pinnedUrl && tab?.url !== pinnedUrl) {
    if (streamedSummary && streamedSummary.url === pinnedUrl) {
      showState("chatState");
      $("exportBtn").disabled = false;
      if ($("messages").children.length === 0) {
        const b = appendBubble("assistant", "");
        b.innerHTML = mdToHtmlLite(streamedSummary.text);
        renderSuggestions();
      }
    }
    return;
  }

  const effectiveUrl = override ? override.url : (pinnedUrl || tab?.url);

  // This panel only works on YouTube videos. On any other page (or a non-video
  // YouTube page) show the neutral state — don't try to summarise it.
  if (!getYoutubeVideoId(effectiveUrl)) {
    stopFastPoll();
    currentUrl = null;
    $("exportBtn").disabled = true;
    showState("notPage");
    return;
  }

  // Summary, notes and follow-up chat are all available through the
  // authenticated TubeNotes API; no separate server setup is required.

  const title = override ? override.title : cleanTitle(tab);
  pinnedUrl = effectiveUrl; // commit the pin
  try { browser.storage.local.set({ pinnedUrl }); } catch (_) {}

  // Already actively polling this exact URL — nothing new to do, avoid
  // starting a duplicate prepChat()/poll for it.
  if (!override && fastPollTimer && effectiveUrl === currentUrl) {
    return;
  }

  if (effectiveUrl !== currentUrl) {
    stopFastPoll();
    // Switched to a DIFFERENT video — cancel any in-flight stream and drop the
    // previous video's summary state so we cleanly summarise the new one.
    if (activeStreamPort) {
      try { activeStreamPort.disconnect(); } catch (_) {}
      activeStreamPort = null;
    }
    activeSummaryUrl = null;
    if (streamedSummary && streamedSummary.url !== effectiveUrl) streamedSummary = null;
    _chatHistory = []; // fresh video → fresh conversation
    _followupToken++; // cancel any in-flight follow-up generation
    $("messages").innerHTML = ""; // also clears inline follow-ups
  }
  currentUrl = effectiveUrl;
  $("pageTitle").textContent = title;
  $("exportBtn").disabled = false;
  showState("chatState");
  renderCredCard(null); // clear stale card from a previous video while loading
  updateCredCard(effectiveUrl);

  // Already streamed a summary for this exact video — just re-show it (with
  // Markdown formatting), don't re-run the prep flow.
  if (streamedSummary && streamedSummary.url === effectiveUrl) {
    $("messages").innerHTML = "";
    const b = appendBubble("assistant", "");
    b.innerHTML = mdToHtmlLite(streamedSummary.text);
    renderSuggestions();
    window._activeSettings = settings;
    refreshFollowups(settings); // regenerate follow-up suggestions for this summary
    return;
  }

  const jobs = await getJobs();
  const found = findJobForUrl(jobs, effectiveUrl);

  if (found && found.job.stage === "chat_ready" && found.job.sessionId) {
    // Already fully ready with a chat session from before.
    renderMessages(found.job.messages || []);
    renderSuggestions();
    window._activeSettings = settings;
    window._activeSourceId = found.id;
    return;
  }

  // ---- TrialGuard: one video = one credit --------------------------
  // This is the single place a NEW video starts being summarized. Every
  // early return above (already-streamed summary, an existing chat_ready job)
  // never reaches this line, so a video is never charged twice.
  const gate = await TrialGuard.requireAccess(getYoutubeVideoId(effectiveUrl));
  if (!gate.ok) {
    renderTrialGate(gate);
    return;
  }
  TrialGuard.refreshBadge();

  // Otherwise: (re)start the prep flow.
  $("messages").innerHTML = "";
  await prepChat(settings, { url: effectiveUrl }, title, !!override);
}

// Generation runs behind TrialGuard. The extension streams through the
// authenticated TubeNotes API and never talks to a model host directly.
async function prepChat(settings, tab, title, autoSummarize) {
  const myGen = ++genToken;
  let thinkingEl = appendThinking("Reading the video…");

  // Prefer the browser-side transcript. It avoids YouTube server-side fetches
  // and is the fastest path. If captions are unavailable here, the backend has
  // its own fallback and will fetch them server-side.
  let transcriptText = "";
  try {
    transcriptText = cleanTranscript(await tryGetTranscript(tab.url));
  } catch (_) {}
  if (transcriptText) {
    console.log(`[ONS] client transcript: ${transcriptText.length} chars`);
  }

  if (!autoSummarize) {
    thinkingEl.remove();
    return;
  }
  if (activeSummaryUrl === tab.url) {
    thinkingEl.remove();
    return;
  }

  activeSummaryUrl = tab.url;
  try {
    thinkingEl.remove();
    renderSuggestions();
    const summary = await streamVllmSummary(settings, transcriptText || null, tab.url);
    if (myGen !== genToken) return;
    streamedSummary = { url: tab.url, text: summary };
    try { await browser.storage.local.set({ lastStreamed: streamedSummary }); } catch (_) {}
    refreshFollowups(settings);
  } catch (e) {
    if (myGen !== genToken) return;
    console.warn("[ONS] vLLM summary failed:", e && e.message);
    appendBubble(
      "assistant",
      `⚠ Couldn't generate the summary through TubeNotes. ${((e && e.message) || "Please try again.")}`
    );
  } finally {
    activeSummaryUrl = null;
  }
}

async function finishPrep(settings, sourceId, thinkingEl, autoSummarizeParam) {
  try {
    const jobs = await getJobs();
    const job = jobs[sourceId];
    if (!job) {
      // Job vanished (race with another refresh / streamed-summary path) —
      // nothing to do here.
      if (thinkingEl) thinkingEl.remove();
      return;
    }
    const isBrandNew = !job.sessionId;
    const shouldAutoSummarize =
      (job.autoSummarizeRequested || autoSummarizeParam) && !job.autoSummarizeDone;

    window._activeSettings = settings;
    window._activeSourceId = sourceId;

    if (isBrandNew && shouldAutoSummarize) {
      // Mark done immediately so a duplicate refresh() for this same job won't
      // also try to auto-trigger.
      job.autoSummarizeDone = true;
      jobs[sourceId] = job;
      await saveJobs(jobs);
      renderSuggestions();

      // --- PRIMARY: fast transformation path ---
      // This reads the source's full text directly (no chat/RAG, no embeddings),
      // which on this setup is ~10x faster than the chat endpoint. The response
      // arrives all at once (the server doesn't stream tokens), so we reveal it
      // with a quick typewriter effect for a "being written" feel.
      if (settings.quickTransformationId) {
        const tEl = appendThinking("Summarizing…");
        try {
          const ts = performance.now();
          const baselineIds = await getInsightIds(settings.apiUrl, sourceId);
          await triggerQuickSummary(
            settings.apiUrl,
            sourceId,
            settings.quickTransformationId,
            settings.chatModelId
          );
          const content = await pollForQuickSummary(settings.apiUrl, sourceId, baselineIds);
          console.log(`[ONS] summary generated in ${Math.round(performance.now() - ts)}ms`);
          tEl.remove();

          const bubble = appendBubble("assistant", "");
          await typeInto(bubble, content); // typewriter reveal

          const freshJobs = await getJobs();
          const freshJob = freshJobs[sourceId] || job;
          freshJob.messages = freshJob.messages || [];
          freshJob.messages.push({ role: "assistant", content });
          freshJobs[sourceId] = freshJob;
          await saveJobs(freshJobs);

          // Prepare a chat session in the background for follow-up questions.
          ensureSessionInBackground(settings, sourceId, freshJob);
          return;
        } catch (err) {
          tEl.remove();
          // fall through to the chat fallback below
          console.warn("[ONS] transformation summary failed, falling back:", err && err.message);
        }
      }

      // --- FALLBACK: chat path (only when no quick transformation is configured) ---
      if (settings.streamSummary && settings.summaryInstruction) {
        try {
          if (!job.sessionId) {
            const session = await createChatSession(settings.apiUrl, sourceId, settings.chatModelId);
            const all = await getJobs();
            const j = all[sourceId] || job;
            j.sessionId = session.id;
            j.messages = j.messages || [];
            all[sourceId] = j;
            await saveJobs(all);
          }
          thinkingEl.remove();
          await runChatMessage(settings, sourceId, settings.summaryInstruction, true);
          return;
        } catch (e) {
          console.warn("[ONS] chat summary failed:", e && e.message);
        }
      }

      // Last resort: a plain "Summarize this video" over chat.
      thinkingEl.remove();
      await runChatMessage(settings, sourceId, "Summarize this video", true);
      return;
    }

    if (isBrandNew) {
      thinkingEl.textContent = "Setting up chat…";
      const session = await createChatSession(settings.apiUrl, sourceId, settings.chatModelId);
      job.sessionId = session.id;
      job.messages = [];
      jobs[sourceId] = job;
      await saveJobs(jobs);
    }
    thinkingEl.remove();

    renderMessages(jobs[sourceId].messages || []);
    renderSuggestions();
  } catch (err) {
    thinkingEl.remove();
    appendBubble("assistant", `⚠ Couldn't start chat: ${err.message}`);
  }
}

async function ensureSessionInBackground(settings, sourceId, job) {
  if (job.sessionId) return;
  try {
    const session = await createChatSession(settings.apiUrl, sourceId, settings.chatModelId);
    const jobs = await getJobs();
    const j = jobs[sourceId];
    if (j && !j.sessionId) {
      j.sessionId = session.id;
      j.messages = j.messages || [];
      jobs[sourceId] = j;
      await saveJobs(jobs);
    }
  } catch (e) {
    // silent — a follow-up message will retry session creation if needed
  }
}

// ---- Rendering helpers ----
function renderMessages(messages) {
  const el = $("messages");
  el.innerHTML = "";
  if (messages.length === 0) {
    appendBubble(
      "assistant",
      "Ready! Ask me anything about this page, or try a suggestion below."
    );
  } else {
    messages.forEach((m) => appendBubble(m.role, m.content));
  }
  el.scrollTop = el.scrollHeight;
}

function appendBubble(role, text) {
  const el = $("messages");
  const div = document.createElement("div");
  div.className = `bubble ${role}`;
  div.textContent = text;
  if (role === "assistant") addAssistantCopyControl(div, text);
  el.appendChild(div);
  el.scrollTop = el.scrollHeight;
  return div;
}

function addAssistantCopyControl(bubble, replyText) {
  if (!bubble || !bubble.classList.contains("assistant")) return;
  const text = String(replyText == null ? (bubble.dataset.copyText || bubble.innerText) : replyText).trim();
  if (!text) return;
  bubble.dataset.copyText = text;

  let copy = bubble.querySelector(".bubble-copy");
  if (!copy) {
    copy = document.createElement("button");
    copy.className = "bubble-copy";
    copy.type = "button";
    copy.title = "Copy reply";
    copy.setAttribute("aria-label", "Copy this reply");
    copy.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="9" width="11" height="11" rx="2"></rect><path d="M15 9V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h3"></path></svg>';
    copy.addEventListener("click", async () => {
      const value = bubble.dataset.copyText || "";
      try {
        await navigator.clipboard.writeText(value);
      } catch (_) {
        const area = document.createElement("textarea");
        area.value = value;
        area.style.cssText = "position:fixed;opacity:0;pointer-events:none";
        document.body.appendChild(area);
        area.select();
        document.execCommand("copy");
        area.remove();
      }
      copy.classList.add("copied");
      copy.title = "Copied";
      setTimeout(() => { copy.classList.remove("copied"); copy.title = "Copy reply"; }, 1300);
    });
    bubble.appendChild(copy);
  }
}

function appendThinking(text) {
  return appendBubble("thinking", text);
}

// Reveal text into a bubble progressively, so a summary that arrives all at once
// still looks like it's being written. Fast enough to feel lively, not sluggish.
function typeInto(el, text, opts = {}) {
  return new Promise((resolve) => {
    const step = opts.step || 4;   // characters revealed per tick
    const delay = opts.delay || 12; // ms between ticks  (~330 chars/sec)
    const box = $("messages");
    let i = 0;
    const tick = () => {
      i += step;
      el.textContent = text.slice(0, i);
      box.scrollTop = box.scrollHeight;
      if (i < text.length) {
        setTimeout(tick, delay);
      } else {
        el.textContent = text;
        resolve();
      }
    };
    tick();
  });
}

function renderSuggestions() {
  const el = $("suggestions");
  el.innerHTML = "";

  // Only the Convert-language chip (the 3 fixed prompt chips were removed).
  const conv = document.createElement("button");
  conv.className = "suggestion-chip convert-only";
  conv.innerHTML = `<span class="chip-ic">🌐</span>Convert to Other Language`;
  conv.addEventListener("click", openConvertPicker);
  el.appendChild(conv);
}

// Languages offered in the "Convert To" list (Google Translate codes). Native
// name shown alongside English name so it's easy to find.
const TRANSLATE_LANGS = [
  { code: "en", name: "English" },
  { code: "hi", name: "Hindi — हिन्दी" },
  { code: "bn", name: "Bengali — বাংলা" },
  { code: "gu", name: "Gujarati — ગુજરાતી" },
  { code: "kn", name: "Kannada — ಕನ್ನಡ" },
  { code: "ml", name: "Malayalam — മലയാളം" },
  { code: "mr", name: "Marathi — मराठी" },
  { code: "pa", name: "Punjabi — ਪੰਜਾਬੀ" },
  { code: "ta", name: "Tamil — தமிழ்" },
  { code: "te", name: "Telugu — తెలుగు" },
  { code: "ur", name: "Urdu — اردو" },
  { code: "or", name: "Odia — ଓଡ଼ିଆ" },
  { code: "as", name: "Assamese — অসমীয়া" },
  { code: "sa", name: "Sanskrit — संस्कृतम्" },
  { code: "ne", name: "Nepali — नेपाली" },
  { code: "sd", name: "Sindhi — سنڌي" },
  { code: "mai", name: "Maithili — मैथिली" },
  { code: "bho", name: "Bhojpuri — भोजपुरी" },
  { code: "gom", name: "Konkani — कोंकणी" },
  { code: "mni-Mtei", name: "Manipuri — ꯃꯤꯇꯩ" },
  { code: "doi", name: "Dogri — डोगरी" },
  { code: "es", name: "Spanish — Español" },
  { code: "fr", name: "French — Français" },
  { code: "de", name: "German — Deutsch" },
  { code: "it", name: "Italian — Italiano" },
  { code: "pt", name: "Portuguese — Português" },
  { code: "ru", name: "Russian — Русский" },
  { code: "ar", name: "Arabic — العربية" },
  { code: "fa", name: "Persian — فارسی" },
  { code: "tr", name: "Turkish — Türkçe" },
  { code: "zh-CN", name: "Chinese (Simplified) — 简体中文" },
  { code: "zh-TW", name: "Chinese (Traditional) — 繁體中文" },
  { code: "ja", name: "Japanese — 日本語" },
  { code: "ko", name: "Korean — 한국어" },
  { code: "nl", name: "Dutch — Nederlands" },
  { code: "pl", name: "Polish — Polski" },
  { code: "uk", name: "Ukrainian — Українська" },
  { code: "vi", name: "Vietnamese — Tiếng Việt" },
  { code: "th", name: "Thai — ไทย" },
  { code: "id", name: "Indonesian — Bahasa Indonesia" },
  { code: "ms", name: "Malay — Bahasa Melayu" },
  { code: "he", name: "Hebrew — עברית" },
  { code: "el", name: "Greek — Ελληνικά" },
  { code: "sw", name: "Swahili — Kiswahili" },
];

// Read the current summary text (streamed, or the last assistant bubble).
function currentSummaryText() {
  let src = streamedSummary && streamedSummary.text ? streamedSummary.text : "";
  if (!src) {
    const bubbles = $("messages").querySelectorAll(".bubble.assistant");
    if (bubbles.length) src = bubbles[bubbles.length - 1].innerText || "";
  }
  return src;
}

// Show a searchable overlay of languages. onPick(code, name) fires on selection.
// Reused by the "Convert To" chip (translate the summary) AND the 📄 button
// (download the PDF in the chosen language).
function openLanguagePicker(onPick, options) {
  options = options || {};
  if (document.getElementById("langOverlay")) return; // already open

  const overlay = document.createElement("div");
  overlay.id = "langOverlay";
  overlay.className = "lang-overlay";
  overlay.innerHTML =
    '<div class="lang-box">' +
    '<div class="lang-head"><span></span>' +
    '<button class="lang-close" title="Close">✕</button></div>' +
    '<input class="lang-search" type="text" placeholder="Search language…">' +
    '<div class="lang-list"></div></div>';
  overlay.querySelector(".lang-head span").textContent = options.title || "Convert to…";
  document.body.appendChild(overlay);

  const list = overlay.querySelector(".lang-list");
  const search = overlay.querySelector(".lang-search");
  const close = () => overlay.remove();

  const draw = (filter) => {
    const f = (filter || "").trim().toLowerCase();
    list.innerHTML = "";
    if (options.sameAsVideo && !f) {
      const same = document.createElement("button");
      same.className = "lang-item lang-item-auto";
      same.innerHTML = '<span>Same as the video language</span><small>Default</small>';
      same.title = "Use the language detected from the video transcript";
      same.addEventListener("click", () => {
        close();
        onPick(null, "Same as the video language");
      });
      list.appendChild(same);
    }
    TRANSLATE_LANGS.filter((l) => l.name.toLowerCase().includes(f)).forEach((l) => {
      const it = document.createElement("button");
      it.className = "lang-item";
      it.textContent = l.name;
      it.addEventListener("click", () => {
        close();
        onPick(l.code, l.name);
      });
      list.appendChild(it);
    });
  };

  overlay.querySelector(".lang-close").addEventListener("click", close);
  overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });
  search.addEventListener("input", () => draw(search.value));
  search.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
  draw("");
  search.focus();
}

// The "Convert To" chip → translate the on-screen summary (needs a summary first).
function openConvertPicker() {
  const src = currentSummaryText();
  if (!src || src.length < 10) {
    appendBubble("assistant", "⚠ Please generate a summary first, then use Convert.");
    return;
  }
  openLanguagePicker(translateSummaryTo, { title: "Convert summary to…" });
}

// Translate the currently-shown summary through the authenticated TubeNotes API
// and replace it in place while keeping its Markdown.
let _translating = false;
async function translateSummaryTo(code, name) {
  if (_translating) return;
  const src = currentSummaryText();
  if (!src || src.length < 10) {
    appendBubble("assistant", "⚠ Please generate a summary first, then use Convert.");
    return;
  }
  const label = (name || code).split(" — ")[0];
  _translating = true;
  // Remember the current follow-ups so we can translate them to the new language.
  const prevFollowups = _currentFollowups.slice();
  removeInlineFollowups(); // hide old-language chips while converting
  const bubble = appendThinking(`🌐 Converting to ${label}…`);
  try {
    const resp = await TrialGuard.translateText(src, code);
    if (!resp || !resp.text) throw new Error("translate failed");
    bubble.classList.remove("thinking");
    bubble.classList.add("assistant");
    bubble.innerHTML = mdToHtmlLite(resp.text);
    $("messages").scrollTop = $("messages").scrollHeight;
    const url = (streamedSummary && streamedSummary.url) || currentUrl;
    streamedSummary = { url, text: resp.text };
    try { browser.storage.local.set({ lastStreamed: streamedSummary }); } catch (_) {}

    // Follow-up questions bhi usi language me — translate the existing ones
    // (Tareeka A: fast + exact). If none existed, regenerate from the new summary.
    if (prevFollowups.length) {
      try {
        const fr = await TrialGuard.translateText(prevFollowups.join("\n"), code);
        const tq = fr && fr.text
          ? fr.text.split("\n").map((l) => l.trim()).filter(Boolean).slice(0, prevFollowups.length)
          : [];
        if (tq.length) renderFollowups(tq);
        else refreshFollowups(window._activeSettings);
      } catch (_) {
        refreshFollowups(window._activeSettings);
      }
    } else {
      refreshFollowups(window._activeSettings); // regenerate from the translated summary
    }
  } catch (e) {
    console.warn("[ONS] translate failed:", e && e.message);
    bubble.remove();
    appendBubble("assistant", "⚠ Conversion failed. Please try again.");
  } finally {
    _translating = false;
  }
}

// Detect a degenerate repetition loop: if one short segment repeats many times,
// or unique content is a tiny fraction of the whole, treat it as garbage.
function looksRepetitive(text) {
  if (!text) return false;
  const words = text.split(/\s+/).filter(Boolean);
  if (words.length < 30) return false;
  const uniq = new Set(words);
  if (uniq.size / words.length < 0.18) return true; // <18% unique words → looping
  // Any 4-word phrase repeated 6+ times?
  const counts = new Map();
  for (let i = 0; i + 4 <= words.length; i++) {
    const g = words.slice(i, i + 4).join(" ");
    const c = (counts.get(g) || 0) + 1;
    counts.set(g, c);
    if (c >= 6) return true;
  }
  return false;
}

// ---- Fast on-the-spot summary: direct transformation call, skips chat/RAG ----
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

async function triggerQuickSummary(apiUrl, sourceId, transformationId, modelId) {
  const res = await fetch(`${apiUrl}/api/sources/${sourceId}/insights`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      transformation_id: transformationId,
      model_id: modelId || null,
    }),
  });
  if (!res.ok) throw new Error(`${res.status} ${(await res.text()).slice(0, 200)}`);
}

async function pollForQuickSummary(apiUrl, sourceId, baselineIds, maxWaitMs = 120000) {
  const baselineSet = new Set(baselineIds || []);
  const start = Date.now();
  let first = true;
  while (Date.now() - start < maxWaitMs) {
    // Small first delay only; the model needs a beat before the insight exists.
    await new Promise((r) => setTimeout(r, first ? 250 : INSIGHT_POLL_MS));
    first = false;
    const res = await fetch(`${apiUrl}/api/sources/${sourceId}/insights`);
    if (res.ok) {
      const insights = await res.json();
      const newInsight = Array.isArray(insights)
        ? insights.find((i) => !baselineSet.has(i.id))
        : null;
      if (newInsight) return newInsight.content;
    }
  }
  throw new Error("Timed out waiting for the summary.");
}

// ---- Open Notebook chat API ----
async function createChatSession(apiUrl, sourceId, modelOverride) {
  const res = await fetch(`${apiUrl}/api/sources/${sourceId}/chat/sessions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      source_id: sourceId,
      model_override: modelOverride || null,
    }),
  });
  if (!res.ok) throw new Error(`${res.status} ${(await res.text()).slice(0, 200)}`);
  return res.json();
}

function parseSseText(text) {
  let aiContent = null;
  for (const line of text.split("\n")) {
    if (!line.startsWith("data: ")) continue;
    try {
      const evt = JSON.parse(line.slice(6));
      if (evt.type === "ai_message") aiContent = evt.content;
    } catch {
      // ignore malformed lines
    }
  }
  return aiContent;
}

// Pull a token/delta out of an SSE event, tolerant of a few possible shapes.
function extractDelta(evt) {
  if (!evt || typeof evt !== "object") return "";
  if (typeof evt.delta === "string") return evt.delta;
  if (typeof evt.token === "string") return evt.token;
  if (evt.type && /chunk|delta|token|stream|partial/i.test(evt.type)) {
    return evt.content || evt.text || evt.message || "";
  }
  return "";
}

// Stream the assistant reply over SSE, calling onUpdate(fullTextSoFar) as tokens
// arrive so the UI can render live. Works whether the server sends incremental
// deltas or a single final "ai_message"; returns the final full text.
async function streamChatMessage(apiUrl, sourceId, sessionId, message, modelOverride, onUpdate) {
  const res = await fetch(
    `${apiUrl}/api/sources/${sourceId}/chat/sessions/${sessionId}/messages`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, model_override: modelOverride || null }),
    }
  );
  if (!res.ok) throw new Error(`${res.status} ${(await res.text()).slice(0, 200)}`);

  // If streaming isn't available, fall back to buffered read.
  if (!res.body || typeof res.body.getReader !== "function") {
    const text = await res.text();
    const content = parseSseText(text);
    if (!content) throw new Error("No reply received.");
    onUpdate && onUpdate(content);
    return content;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let assembled = "";   // built up from delta-style events
  let lastFull = null;  // last full "ai_message" content, if any
  let logged = 0;

  const handle = (payload) => {
    if (!payload || payload === "[DONE]") return;
    let evt;
    try { evt = JSON.parse(payload); } catch { return; }
    if (logged < 6) { console.debug("[ONS] sse", evt); logged++; }
    if (evt.type === "ai_message" && typeof evt.content === "string") {
      lastFull = evt.content;
    } else {
      const d = extractDelta(evt);
      if (d) assembled += d;
    }
    const display = lastFull !== null ? lastFull : assembled;
    if (display && onUpdate) onUpdate(display);
  };

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let nl;
    while ((nl = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, nl).trim();
      buffer = buffer.slice(nl + 1);
      if (line.startsWith("data:")) handle(line.slice(5).trim());
    }
  }
  const tail = buffer.trim();
  if (tail.startsWith("data:")) handle(tail.slice(5).trim());

  const final = lastFull !== null ? lastFull : assembled;
  if (!final) throw new Error("No reply received.");
  return final;
}

// Two-stage summary (Raasta B): show a fast, short summary first so the user has
// something to read in a few seconds, then quietly generate the full detailed
// summary and swap it in when ready. Uses two clean sessions so neither summary
// leaks into the other's context.
async function runTwoStageSummary(settings, sourceId) {
  const box = $("messages");

  // Session for the quick pass.
  const shortSession = await createChatSession(settings.apiUrl, sourceId, settings.chatModelId);

  const bubble = appendBubble("assistant", "");
  const note = document.createElement("div");
  note.className = "muted";
  note.style.cssText = "font-size:11px;margin:2px 0 4px 2px;opacity:0.8;";
  note.textContent = "⚡ Quick summary — detailed version loading…";
  box.appendChild(note);
  box.scrollTop = box.scrollHeight;

  const t0 = performance.now();

  // ---- Stage 1: short summary (fast) ----
  const short = await streamChatMessage(
    settings.apiUrl, sourceId, shortSession.id,
    settings.summaryInstructionShort, settings.chatModelId,
    (t) => { bubble.textContent = t; box.scrollTop = box.scrollHeight; }
  );
  console.log(`[ONS] quick summary in ${Math.round(performance.now() - t0)}ms`);

  // Persist the short one immediately so a reload isn't empty if stage 2 fails.
  {
    const jobs = await getJobs();
    const j = jobs[sourceId];
    if (j) {
      j.sessionId = j.sessionId || shortSession.id;
      j.messages = j.messages || [];
      j.messages.push({ role: "assistant", content: short });
      jobs[sourceId] = j;
      await saveJobs(jobs);
    }
  }

  // ---- Stage 2: detailed summary (replaces the short one) ----
  try {
    const detailSession = await createChatSession(settings.apiUrl, sourceId, settings.chatModelId);
    const t1 = performance.now();
    const detailed = await streamChatMessage(
      settings.apiUrl, sourceId, detailSession.id,
      settings.summaryInstruction, settings.chatModelId,
      null // don't live-update; swap in once when done to avoid flicker
    );
    console.log(`[ONS] detailed summary in ${Math.round(performance.now() - t1)}ms`);

    bubble.textContent = detailed;
    note.remove();
    box.scrollTop = box.scrollHeight;

    // Replace the stored short summary with the detailed one, and keep the
    // detailed session as the live one for follow-up questions.
    const jobs = await getJobs();
    const j = jobs[sourceId] || {};
    j.sessionId = detailSession.id;
    j.messages = j.messages || [];
    for (let i = j.messages.length - 1; i >= 0; i--) {
      if (j.messages[i].role === "assistant" && j.messages[i].content === short) {
        j.messages[i] = { role: "assistant", content: detailed };
        break;
      }
    }
    jobs[sourceId] = j;
    await saveJobs(jobs);
  } catch (e) {
    // Detailed pass failed — keep the short summary, drop the loading note.
    note.remove();
    console.warn("[ONS] detailed stage failed, keeping quick summary:", e && e.message);
  }
}

async function runChatMessage(settings, sourceId, message, silent) {
  if (!silent) appendBubble("user", message);
  // One bubble that starts as a "thinking" indicator and becomes the live
  // assistant reply the moment the first token arrives.
  const bubbleEl = appendThinking(silent ? "Generating summary…" : "Thinking…");
  let started = false;
  const onUpdate = (text) => {
    if (!started) {
      started = true;
      bubbleEl.classList.remove("thinking");
      bubbleEl.classList.add("assistant");
    }
    bubbleEl.textContent = text;
    const box = $("messages");
    box.scrollTop = box.scrollHeight;
  };

  $("sendBtn").disabled = true;

  const jobs = await getJobs();
  const job = jobs[sourceId];

  try {
    if (!job.sessionId) {
      const session = await createChatSession(settings.apiUrl, sourceId, settings.chatModelId);
      job.sessionId = session.id;
      job.messages = job.messages || [];
      jobs[sourceId] = job;
      await saveJobs(jobs);
    }
    const reply = await streamChatMessage(
      settings.apiUrl, sourceId, job.sessionId, message, settings.chatModelId, onUpdate
    );
    // If we somehow got a reply without any streamed token, render it now.
    if (!started) onUpdate(reply);

    const fresh = await getJobs();
    const fjob = fresh[sourceId] || job;
    fjob.messages = fjob.messages || [];
    if (!silent) fjob.messages.push({ role: "user", content: message });
    fjob.messages.push({ role: "assistant", content: reply });
    fresh[sourceId] = fjob;
    await saveJobs(fresh);
  } catch (err) {
    if (silent) {
      // Let the caller decide on a fallback (e.g. transformation summary).
      bubbleEl.remove();
      $("sendBtn").disabled = false;
      throw err;
    }
    bubbleEl.classList.remove("thinking");
    bubbleEl.classList.add("assistant");
    bubbleEl.textContent = `⚠ ${err.message}`;
  } finally {
    $("sendBtn").disabled = false;
  }
}

function requestedChatReplyLanguage(question) {
  // Match the web app's useful commands such as "answer in Hindi" without
  // changing the normal behaviour (the backend detects the question language).
  const q = String(question || "").toLowerCase();
  if (!/answer|reply|respond|explain|describe|tell|bata|samjha|bol|language|\bin\b|\bme\b|\bmein\b/i.test(q)) return null;
  const aliases = [
    ["hi", ["hindi", "हिंदी", "हिन्दी"]], ["en", ["english", "अंग्रेजी"]],
    ["bn", ["bengali", "bangla", "বাংলা"]], ["ta", ["tamil", "தமிழ்"]],
    ["gu", ["gujarati", "gujrati", "ગુજરાતી"]], ["mr", ["marathi"]],
    ["pa", ["punjabi", "ਪੰਜਾਬੀ"]], ["te", ["telugu", "తెలుగు"]],
    ["kn", ["kannada", "ಕನ್ನಡ"]], ["ml", ["malayalam", "മലയാളം"]],
    ["ur", ["urdu", "اردو"]], ["ne", ["nepali"]], ["es", ["spanish", "español"]],
    ["fr", ["french", "français"]], ["de", ["german", "deutsch"]],
    ["pt", ["portuguese"]], ["it", ["italian"]], ["ru", ["russian", "русский"]],
    ["ar", ["arabic", "العربية"]], ["zh", ["chinese", "中文"]], ["ja", ["japanese", "日本語"]],
    ["ko", ["korean", "한국어"]],
  ];
  for (const [code, words] of aliases) {
    if (words.some((word) => q.includes(word.toLowerCase()))) return code;
  }
  return null;
}

async function runTubeNotesVideoChat(question) {
  const summary = (streamedSummary && streamedSummary.text) || currentSummaryText();
  if (!summary || summary.trim().length < 20) {
    throw new Error("Generate the video summary before asking a question.");
  }
  const token = await TrialGuard.accessToken();
  if (!token) throw new Error("Please sign in again to continue this chat.");

  const detected = await detectSummaryLang(summary);
  const response = await fetch(`${String(TrialGuard.apiBase || "").replace(/\/+$/, "")}/video-chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify({
      summary,
      language: (detected && detected.code) || "en",
      reply_language: requestedChatReplyLanguage(question),
      question,
      history: _chatHistory.slice(-10),
    }),
  });
  if (!response.ok) {
    let detail = "Video chat is temporarily unavailable. Please try again.";
    try {
      const body = await response.json();
      detail = (body && (body.detail || body.message)) || detail;
    } catch (_) {}
    throw new Error(typeof detail === "string" ? detail : "Video chat is temporarily unavailable. Please try again.");
  }
  const body = await response.json();
  const answer = String((body && body.answer) || "").trim();
  if (!answer) throw new Error("Video chat did not return an answer. Please try again.");
  _chatHistory.push({ role: "user", content: question }, { role: "assistant", content: answer });
  _chatHistory = _chatHistory.slice(-12);
  return answer;
}

async function sendCurrentMessage() {
  const input = $("chatInput");
  const send = $("sendBtn");
  const message = input.value.trim();
  if (!message || send.disabled) return;
  input.value = "";
  removeInlineFollowups();
  appendBubble("user", message);
  const pending = appendThinking("Thinking about the video…");
  send.disabled = true;
  input.disabled = true;
  try {
    const answer = await runTubeNotesVideoChat(message);
    pending.classList.remove("thinking");
    pending.classList.add("assistant");
    pending.innerHTML = mdToHtmlLite(answer);
    addAssistantCopyControl(pending, answer);
    $("messages").scrollTop = $("messages").scrollHeight;
  } catch (error) {
    pending.classList.remove("thinking");
    pending.classList.add("assistant");
    pending.textContent = `⚠ ${(error && error.message) || "Video chat is temporarily unavailable. Please try again."}`;
  } finally {
    send.disabled = false;
    input.disabled = false;
    input.focus();
  }
}

// Chat history for follow-up questions (reset when the video changes).
let _chatHistory = [];

// ---- Auto-suggested follow-up questions (in the video's language) ----
let _followupToken = 0;
let _currentFollowups = []; // the questions currently shown (for re-translating on convert)
function removeInlineFollowups() {
  const box = document.getElementById("messages");
  const old = box && box.querySelector(".followups-inline");
  if (old) old.remove();
}
// Render follow-up chips INSIDE the messages area, right under the last message
// (summary/answer), so they sit directly below the summary and scroll with it.
function renderFollowups(questions) {
  const box = document.getElementById("messages");
  if (!box) return;
  removeInlineFollowups();
  _currentFollowups = (questions || []).slice();
  if (!questions || !questions.length) return;
  const wrap = document.createElement("div");
  wrap.className = "followups-inline";
  questions.forEach((q) => {
    const b = document.createElement("button");
    b.className = "followup-chip";
    b.textContent = q;
    b.addEventListener("click", () => {
      $("chatInput").value = q;
      sendCurrentMessage();
    });
    wrap.appendChild(b);
  });
  box.appendChild(wrap);
  box.scrollTop = box.scrollHeight;
}
// Lightweight follow-up suggestions. These are local so they add zero model
// latency and never make a second AI request after the summary finishes.
async function refreshFollowups(settings) {
  const myToken = ++_followupToken;
  let context = streamedSummary && streamedSummary.text ? streamedSummary.text : "";
  if (!context) return;
  if (myToken !== _followupToken) return;
  const hindi = /[ऀ-ॿ]/.test(context.slice(0, 1000));
  renderFollowups(hindi
    ? ["इस वीडियो का मुख्य निष्कर्ष क्या है?", "सबसे महत्वपूर्ण 3 बातें कौन-सी हैं?", "इसे आसान उदाहरण से समझाओ"]
    : ["What is the main takeaway?", "What are the 3 most important points?", "Explain this with a simple example"]);
}

// Decide the answer language from the QUESTION's language/style, so a Hinglish
// question gets a Hinglish answer, Hindi→Hindi, English→English, other→that lang.
function chatLangDirective(q) {
  const text = q || "";
  if (/[ऀ-ॿ]/.test(text)) {
    return "LANGUAGE RULE: Reply ENTIRELY in Hindi using Devanagari (शुद्ध, सरल हिंदी). Do NOT reply in English.";
  }
  // Hinglish = Hindi written in Roman letters. Detect by common Hindi words.
  const hinglish = /\b(kya|kyu|kyun|kaise|kaisa|kaisi|hai|hain|ho|hota|hoti|ka|ki|ke|ko|kaun|kahan|kab|kitna|kitne|matlab|batao|bata|samjhao|samjha|samjhaiye|mujhe|mera|meri|mere|nahi|nhi|acha|achha|thoda|aur|wala|wali|karo|karke|raha|rahe|rahi|liye|iska|isme|uska|usme|yaha|vaha|hume|hame|chahiye|kripya)\b/i;
  if (hinglish.test(text)) {
    return "LANGUAGE RULE: The user asked in HINGLISH (Hindi language written in Roman/English letters). Reply in the SAME Hinglish style — Hindi in Roman letters, exactly how the user typed. Do NOT reply in pure English, and do NOT use Devanagari.";
  }
  // English or another language → mirror the question's own language + script.
  return "LANGUAGE RULE: Reply in the SAME language AND script as the user's question. If it is English, reply in English; if it is another language (Spanish, French, etc.), reply entirely in that language.";
}

// Answer the user's question about THIS video via Ollama, streaming live.
async function runOllamaChat(settings, question) {
  removeInlineFollowups(); // clear old suggestions; new ones after the answer
  appendBubble("user", question);
  const bubble = appendThinking("Thinking…");
  const box = $("messages");
  if (box) box.scrollTop = box.scrollHeight;
  $("sendBtn").disabled = true;

  // Context = the video's transcript (best), else the already-shown summary.
  let context = "";
  try {
    const t = cleanTranscript(await tryGetTranscript(currentUrl));
    if (t) context = capForOllama(t);
  } catch (_) {}
  if (!context && streamedSummary && streamedSummary.text) context = streamedSummary.text;

  const langRule = chatLangDirective(question);
  const sys =
    "You are a helpful assistant answering the user's questions about a specific YouTube video. " +
    "Use the VIDEO CONTENT below as your main source. If the answer isn't in it, say so briefly and answer from general knowledge. " +
    "Be clear, accurate and to the point (use short paragraphs or bullets).\n\n" +
    langRule + "\n\n" +
    "=== VIDEO CONTENT ===\n" + (context || "(transcript unavailable)") + "\n=== END VIDEO CONTENT ===\n\n" +
    "REMINDER: " + langRule;

  const history = _chatHistory.slice(-6); // last 3 Q&A pairs for follow-ups
  const messages = [{ role: "system", content: sys }, ...history, { role: "user", content: question }];

  try {
    const reply = await streamOllamaChat(settings, messages, bubble);
    if (reply && reply.trim()) {
      _chatHistory.push({ role: "user", content: question });
      _chatHistory.push({ role: "assistant", content: reply });
      if (_chatHistory.length > 12) _chatHistory = _chatHistory.slice(-12);
      refreshFollowups(settings); // new follow-up suggestions based on this answer
    } else {
      bubble.classList.remove("thinking");
      bubble.classList.add("assistant");
      bubble.textContent = "⚠ No response received. Please try again.";
    }
  } catch (e) {
    bubble.classList.remove("thinking");
    bubble.classList.add("assistant");
    bubble.textContent = "⚠ Couldn't get a response — unable to reach Ollama. Please try again.";
  } finally {
    $("sendBtn").disabled = false;
  }
}

// Stream an Ollama chat answer into `bubble`. Resolves with the full text.
function streamOllamaChat(settings, messages, bubble) {
  return new Promise((resolve, reject) => {
    let text = "", started = false, settled = false;
    let port;
    try { port = browser.runtime.connect({ name: "ollama-stream" }); }
    catch (e) { reject(e); return; }
    activeStreamPort = port;
    const clearActive = () => { if (activeStreamPort === port) activeStreamPort = null; };

    const startTimeout = setTimeout(() => {
      if (!started && !settled) { settled = true; clearActive(); try { port.disconnect(); } catch (_) {} reject(new Error("no response")); }
    }, 45000);

    port.onMessage.addListener((m) => {
      if (!m) return;
      if (m.type === "DELTA") {
        if (!started) {
          started = true;
          clearTimeout(startTimeout);
          bubble.classList.remove("thinking");
          bubble.classList.add("assistant");
        }
        text += m.text;
        bubble.innerHTML = mdToHtmlLite(text);
        const box = $("messages");
        if (box) box.scrollTop = box.scrollHeight;
      } else if (m.type === "DONE") {
        if (settled) return;
        settled = true; clearTimeout(startTimeout); clearActive();
        try { port.disconnect(); } catch (_) {}
        resolve(text);
      } else if (m.type === "ERROR") {
        if (settled) return;
        settled = true; clearTimeout(startTimeout); clearActive();
        try { port.disconnect(); } catch (_) {}
        reject(new Error(m.error || "ollama error"));
      }
    });
    port.onDisconnect.addListener(() => {
      if (settled) return;
      settled = true; clearTimeout(startTimeout); clearActive();
      resolve(text); // whatever streamed so far
    });

    port.postMessage({
      type: "START",
      url: settings.ollamaUrl,
      model: settings.ollamaModel,
      messages,
      // num_ctx 8192 so the transcript context + a full answer fit — otherwise
      // the follow-up answer gets cut off (same root cause as the summary).
      options: { temperature: 0.4, top_p: 0.9, repeat_penalty: 1.15, num_predict: 1200, num_ctx: 8192 },
    });
  });
}

// ---- Export (detailed PDF/Markdown) ----
// The full-notes PDF export runs in the BACKGROUND worker now, so it keeps going
// even when the panel is hidden (user on a non-YouTube tab). The panel only
// DISPLAYS the progress, reading it from storage.local.pdfJob — so whenever the
// panel is visible on a YouTube tab (including after re-showing on tab return),
// the live bar appears and keeps updating.
let pdfBarRef = null;
let _pdfDoneCleanup = null;
let _pdfLoadTimer = null;

async function refreshPdfServerLoad() {
  const panel = document.getElementById("pdfProgressPanel");
  if (!panel || panel.classList.contains("hidden")) return;
  const count = document.getElementById("pdfCountLabel");
  const fill = document.getElementById("pdfMainFill");
  try {
    const apiBase = String(TrialGuard.apiBase || "").replace(/\/+$/, "");
    if (!apiBase) throw new Error("TubeNotes API unavailable");
    const r = await fetch(`${apiBase}/load`, { cache: "no-store" });
    if (!r.ok) throw new Error(`load ${r.status}`);
    const d = await r.json();
    const running = d.running == null ? null : Math.round(Number(d.running));
    const capacity = Math.max(1, Math.round(Number(d.capacity) || 1));
    const waiting = Math.max(0, Math.round(Number(d.waiting) || 0));
    const jobs = Math.max(0, Math.round(Number(d.jobs) || 0));
    const busy = d.busy_percent == null
      ? (running == null ? 0 : Math.min(100, running / capacity * 100))
      : Math.max(0, Math.min(100, Number(d.busy_percent) || 0));
    const bits = [];
    if (running != null) bits.push(`${running}/${capacity} generating`);
    if (waiting) bits.push(`${waiting} queued`);
    if (jobs) bits.push(`${jobs} PDF${jobs === 1 ? "" : "s"}`);
    if (count) count.textContent = bits.length ? bits.join(" · ") : "idle";
    if (fill) fill.style.width = `${Math.max(2, busy)}%`;
  } catch (_) {
    if (count && (!count.textContent || count.textContent === "Starting…")) count.textContent = "checking…";
  }
}

function startPdfServerLoadMeter() {
  if (_pdfLoadTimer) return;
  refreshPdfServerLoad();
  _pdfLoadTimer = setInterval(refreshPdfServerLoad, 2000);
}
function stopPdfServerLoadMeter() {
  if (_pdfLoadTimer) clearInterval(_pdfLoadTimer);
  _pdfLoadTimer = null;
}
function pdfStageFor(pct, job) {
  // Prefer the lifecycle stage written by the background worker. Percentage
  // alone must never move the UI to the next step before work is complete.
  const explicit = Number(job && job.stage);
  if (explicit >= 1 && explicit <= 5) return explicit;
  if (job && job.done) return 5;
  // Backward-compatible fallback for an old/in-flight job from v2.3.2.
  // 96% is still Writing Notes; Preparing PDF starts only at 98%+.
  if (pct < 12) return 1;
  if (pct < 25) return 2;
  if (pct < 98) return 3;
  return 4;
}
function ensurePdfBarFromJob(job) {
  const panel = document.getElementById("pdfProgressPanel");
  if (!panel) return;
  if (!job || (!job.active && !job.done && !job.error)) { panel.classList.add("hidden"); stopPdfServerLoadMeter(); return; }
  if (!job.active && job.ts && Date.now() - job.ts > 60000) { panel.classList.add("hidden"); stopPdfServerLoadMeter(); return; }
  panel.classList.remove("hidden");
  startPdfServerLoadMeter();
  const pct = Math.min(100, Math.max(0, Number(job.pct || 0)));
  const stage = pdfStageFor(pct, job);
  const title = document.getElementById("pdfProgressTitle");
  const sub = document.getElementById("pdfProgressSub");
  const fill = document.getElementById("pdfMainFill");
  const pctFill = document.getElementById("pdfPercentFill");
  const pctText = document.getElementById("pdfPercentText");
  const videoTitleEl = document.getElementById("pdfVideoTitle");
  const thumbEl = document.getElementById("pdfVideoThumb");
  const count = document.getElementById("pdfCountLabel");
  const preview = document.getElementById("pdfPreviewText");
  if (title) title.textContent = job.error ? "PDF generation failed" : job.done ? "Detailed notes ready" : "Writing detailed notes…";
  if (sub) sub.textContent = job.label || "Every section is included in your PDF.";
  if (pctFill) pctFill.style.width = pct + "%";
  if (pctText) pctText.textContent = Math.round(pct) + "%";
  if (videoTitleEl) videoTitleEl.textContent = ($("pageTitle") && $("pageTitle").textContent) || "YouTube video";
  if (thumbEl && currentUrl) {
    try { const u=new URL(currentUrl); const id=u.searchParams.get("v") || (u.hostname.includes("youtu.be") ? u.pathname.slice(1) : ""); if(id) thumbEl.src=`https://i.ytimg.com/vi/${id}/mqdefault.jpg`; } catch(_) {}
  }
  // pdfCountLabel is reserved for REAL backend /load metrics.
  refreshPdfServerLoad();
  if (preview && job.preview) preview.textContent = job.preview;
  panel.querySelectorAll(".pdf-step").forEach((el, i) => {
    const n = i + 1;
    // Reset both state classes first so a previously completed/active step can
    // never visually leak into the next render. Exactly one unfinished step
    // may spin; only strictly earlier steps are green.
    el.classList.remove("done", "active");
    if (job.done && n <= 4) el.classList.add("done");
    else if (n < stage) el.classList.add("done");
    else if (!job.error && n === stage && stage <= 4) el.classList.add("active");
  });
  if (!job.active) {
    if (_pdfDoneCleanup) clearTimeout(_pdfDoneCleanup);
    _pdfDoneCleanup=setTimeout(()=>{ try{browser.storage.local.remove("pdfJob");}catch(_){} },8000);
  }
}

// 📄 button → open the language list. Pick a language → the PDF is generated
// with FULL details and downloaded in THAT language.
function requestExport() {
  if (!currentUrl || !isSummarizableUrl(currentUrl)) return;
  openLanguagePicker(startPdfExport, { title: "Download PDF in…", sameAsVideo: true });
}

async function startPdfExport(targetCode, targetName) {
  const settings = await getSettings();
  if (!currentUrl || !isSummarizableUrl(currentUrl)) return;

  const videoUrl = currentUrl;
  const videoTitle = $("pageTitle").textContent;

  const transcriptText = cleanTranscript(await tryGetTranscript(videoUrl));

  // Full-detail PDF path through TrialGuard's vLLM-backed /notes endpoint.
  // The transcript is supplied when available for maximum speed; otherwise the
  // backend fetches captions itself. The background worker keeps the job alive
  // even if the side panel is hidden.
  ensurePdfBarFromJob({ active: true, pct: 4, label: "📄 Creating full detailed notes with vLLM…", ts: Date.now() });
  try {
    const resp = await browser.runtime.sendMessage({
      type: "START_PDF_JOB",
      payload: {
        transcript: transcriptText || null,
        title: videoTitle,
        url: videoUrl,
        targetLang: targetCode,
        targetLangName: (targetName || "").split(" — ")[0],
      },
    });
    if (!resp || !resp.ok) throw new Error((resp && resp.error) || "could not start");
  } catch (e) {
    console.warn("[ONS] start vLLM pdf job failed:", e && e.message);
    ensurePdfBarFromJob({ error: true, pct: 100, label: "⚠ Couldn't start the PDF. Please press 📄 again.", ts: Date.now() });
  }
  return;

  // Legacy server transformation path (kept below only for compatibility with
  // very old saved jobs; new exports never reach it).
  appendBubble("thinking", "📄 Preparing export via server…");
  try {
    const resp = await browser.runtime.sendMessage({
      type: "START_EXPORT",
      payload: {
        apiUrl: settings.apiUrl,
        notebookId: settings.notebookId,
        transformationId: settings.transformationId,
        videoUrl,
        videoTitle,
        fastMode: settings.fastMode,
        transcriptText: transcriptText || undefined,
      },
    });
    if (!resp.ok) throw new Error(resp.error);
    watchExport(resp.sourceId, videoUrl);
  } catch (err) {
    appendBubble("assistant", `⚠ Couldn't start export: ${err.message}`);
  }
}

let exportWatchTimer = null;
function watchExport(sourceId, url) {
  if (exportWatchTimer) clearInterval(exportWatchTimer);
  exportWatchTimer = setInterval(async () => {
    if (currentUrl !== url) {
      clearInterval(exportWatchTimer);
      return;
    }
    const jobs = await getJobs();
    const job = jobs[sourceId];
    if (!job) return;
    if (job.status === "done") {
      clearInterval(exportWatchTimer);
      appendBubble("assistant", "✅ Detailed summary exported — Markdown downloaded and a print-ready tab opened for saving as PDF.");
    } else if (job.status === "error") {
      clearInterval(exportWatchTimer);
      appendBubble("assistant", "⚠ Export failed. Check Open Notebook for details.");
    }
  }, 3000);
}

// ---- Static wiring ----
// =====================================================================
// TrialGuard — the sign-in / upgrade screen shown instead of the chat.
// =====================================================================
function paintProductBrand(entitlement) {
  const label = $("productBrand");
  if (!label) return;
  const isPro = entitlement && entitlement.plan === "subscription";
  label.textContent = isPro ? "TubeNotes Pro" : "TubeNotes";
  label.closest(".tn-brand")?.setAttribute("aria-label", isPro ? "TubeNotes Pro" : "TubeNotes");
}

function renderTrialGate(gate) {
  stopFastPoll();
  $("exportBtn").disabled = true;

  const titles = {
    signin_required: "Sign in to summarize",
    subscription_required: "Free videos used up",
    unavailable: "Can't reach the account server",
  };
  $("tgGateTitle").textContent = titles[gate.reason] || "Not available";
  $("tgGateMsg").textContent = gate.message || "";

  const ent = gate.entitlement;
  paintProductBrand(ent);
  if (ent && ent.trials_limit) {
    const left = Math.min(ent.trials_remaining, ent.device_trials_remaining);
    $("tgMeter").classList.remove("hidden");
    $("tgMeterFill").style.width = `${((ent.trials_limit - left) / ent.trials_limit) * 100}%`;
  } else {
    $("tgMeter").classList.add("hidden");
  }

  const btn = $("tgGateBtn");
  const alt = $("tgGateAlt");
  alt.classList.add("hidden");

  if (gate.reason === "subscription_required") {
    let gatePlan = null;
    TrialGuard.getBillingPrice()
      .then((plan) => {
        gatePlan = plan;
        const amount = plan.currency === "INR" ? `₹${plan.price_cents / 100}`
          : plan.currency === "USD" ? `$${(plan.price_cents / 100).toFixed(0)}`
            : `${plan.currency} ${(plan.price_cents / 100).toFixed(2)}`;
        btn.textContent = `Subscribe — ${amount}/month`;
      })
      .catch(() => {});
    btn.textContent = "Subscribe — $5/month";
    btn.onclick = async () => {
      // India uses the account popup so the account-bound coupon is claimed
      // and applied before checkout; it must never silently use regular price.
      if (gatePlan && gatePlan.currency === "INR") { openAccountUi(); return; }
      btn.disabled = true;
      try {
        await TrialGuard.startCheckout();
      } catch (e) {
        $("tgGateMsg").textContent =
          e && e.status === 409
            ? "You already have an active subscription — hit Refresh."
            : "Couldn't open checkout. Please try again.";
      } finally {
        btn.disabled = false;
        alt.classList.remove("hidden");
      }
    };
    alt.textContent = "Payment done? Refresh";
    alt.onclick = () => { currentUrl = null; refresh(); };
    $("tgGateFoot").textContent =
      "Unlimited videos, cancel any time. Videos you already summarized stay free.";
  } else if (gate.reason === "unavailable") {
    btn.textContent = "Try again";
    btn.onclick = () => { currentUrl = null; refresh(); };
    $("tgGateFoot").textContent =
      "Summaries are unlocked by the account server, so it has to be reachable.";
  } else {
    btn.textContent = "Sign in / Create account";
    btn.onclick = openAccountUi;
    $("tgGateFoot").textContent = "New accounts get 20 free videos on this device.";
  }

  showState("tgGate");
}

/** Open the account popup, falling back to a normal tab. */
async function openAccountUi() {
  try {
    const action = browser.action || browser.browserAction;
    await action.openPopup();
  } catch (_) {
    browser.tabs.create({ url: browser.runtime.getURL("account.html") });
  }
}

$("accountBtn")?.addEventListener("click", openAccountUi);

// Signing in or subscribing happens in another window, so watch the stored
// tokens and drop the gate by itself when they change.
browser.storage.onChanged.addListener((changes, area) => {
  if (area !== "local") return;
  if (changes.tg_entitlement) paintProductBrand(changes.tg_entitlement.newValue);
  if (!changes.tg_tokens && !changes.tg_entitlement) return;
  if ($("tgGate") && !$("tgGate").classList.contains("hidden")) {
    currentUrl = null;
    refresh();
  }
});

$("settingsBtn").addEventListener("click", () => browser.runtime.openOptionsPage());
$("openOptions")?.addEventListener("click", () => browser.runtime.openOptionsPage());
$("exportBtn").addEventListener("click", requestExport);

$("sendBtn").addEventListener("click", sendCurrentMessage);
$("chatInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendCurrentMessage();
  }
});

browser.tabs.onActivated.addListener(() => refresh());
browser.tabs.onUpdated.addListener((_tabId, info) => {
  if (info.status === "complete") refresh();
});
browser.storage.onChanged.addListener((changes, area) => {
  if (area !== "local") return;
  if (changes.pendingOverride) refresh();
  // Live progress: the background writes pdfJob as it generates; reflect it.
  if (changes.pdfJob) ensurePdfBarFromJob(changes.pdfJob.newValue);
});

// Whenever the panel (re)renders, re-attach the progress bar if a job is live —
// this is what makes the bar re-appear (with current progress) when the user
// comes back to a YouTube tab and the panel re-shows.
const _origRefreshInner = refreshInner;
refreshInner = async function () {
  await _origRefreshInner();
  try {
    paintProductBrand(await TrialGuard.getCachedEntitlement());
    const { pdfJob } = await browser.storage.local.get("pdfJob");
    if (pdfJob) ensurePdfBarFromJob(pdfJob);
  } catch (_) {}
};

// On first load, a normal refresh renders the panel; the wrapped refreshInner
// then re-attaches the progress bar from storage if a job is still in flight.
refresh();
