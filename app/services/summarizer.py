"""Summary generation via vLLM's OpenAI-compatible API.

ONE model, Gemma, serves every language. There is no second model to pick and
no router to get wrong: `VLLM_MODEL` is the whole story. The only per-language
decision left is whether Gemma writes that language well enough to be asked
directly, or whether it should write English and have the result translated.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

from app.config import settings
from app.services.youtube import sample_for_model

logger = logging.getLogger("trialguard.summarizer")

# Headroom for the system prompt, the language directive and the chat template.
# Measured against the longest of these prompts with a wide margin.
_PROMPT_TOKEN_ALLOWANCE = 900

_vllm_client: httpx.AsyncClient | None = None
_vllm_slots: asyncio.Semaphore | None = None


class VLLMBusy(RuntimeError):
    """Every generation slot was taken for longer than the caller would wait."""


# Languages Gemma writes well enough to be asked for directly. Anything outside
# this set is written in English and then translated - see plan_for().
NATIVE_LANGS = {
    # widely-spoken languages the model handles natively
    "en", "hi", "es", "fr", "de", "it", "pt", "ru", "ja", "zh", "ar", "nl", "tr", "ko", "id", "vi",
    # regional Indian languages
    "mr", "gu", "pa", "ta", "te", "kn", "ml", "bn", "or", "as", "ur", "sd", "ne", "kok", "mai",
}

LANG_NAMES = {
    "en": "English", "hi": "Hindi", "fr": "French", "es": "Spanish", "de": "German",
    "it": "Italian", "pt": "Portuguese", "ru": "Russian", "ar": "Arabic", "ur": "Urdu",
    "fa": "Persian", "tr": "Turkish", "nl": "Dutch", "pl": "Polish", "uk": "Ukrainian",
    "vi": "Vietnamese", "th": "Thai", "id": "Indonesian", "ms": "Malay", "he": "Hebrew",
    "el": "Greek", "ja": "Japanese", "ko": "Korean", "zh": "Chinese", "bn": "Bengali",
    "gu": "Gujarati", "kn": "Kannada", "ml": "Malayalam", "mr": "Marathi", "pa": "Punjabi",
    "ta": "Tamil", "te": "Telugu", "or": "Odia", "as": "Assamese", "ne": "Nepali",
    "si": "Sinhala", "sw": "Swahili", "ro": "Romanian", "cs": "Czech", "sv": "Swedish",
    "hu": "Hungarian", "fi": "Finnish", "da": "Danish", "no": "Norwegian",
}

# Scripts that map to exactly one language - an instant, offline detection.
_SCRIPT_RANGES = [
    (re.compile(r"[઀-૿]"), "gu"),
    (re.compile(r"[਀-੿]"), "pa"),
    (re.compile(r"[஀-௿]"), "ta"),
    (re.compile(r"[ఀ-౿]"), "te"),
    (re.compile(r"[ಀ-೿]"), "kn"),
    (re.compile(r"[ഀ-ൿ]"), "ml"),
    (re.compile(r"[ঀ-৿]"), "bn"),
    (re.compile(r"[଀-୿]"), "or"),
    (re.compile(r"[؀-ۿ]"), "ar"),
]
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")


def detect_language(text: str, hint: str | None = None) -> str:
    """Language code for the summary.

    The caption track's own code is the most reliable signal (it distinguishes
    Hindi from Marathi, which share a script), so it wins when present.
    """
    if hint:
        code = hint.split("-")[0].lower()
        if code:
            return code

    sample = (text or "")[:4000]
    for pattern, code in _SCRIPT_RANGES:
        if len(pattern.findall(sample)) >= 15:
            return code
    if len(_DEVANAGARI.findall(sample)) >= 15:
        return "hi"
    return "en"


def language_name(code: str) -> str:
    return LANG_NAMES.get(code, code)


def model_for(code: str) -> str:
    """The model that will answer. Gemma, always - the argument is ignored.

    Kept as a function because the model name is part of the output-cache key,
    and one place to read it from means the cache can never disagree with what
    actually generated the row.
    """
    return settings.VLLM_MODEL


def plan_for(target: str) -> tuple[str, str, str | None]:
    """Decide (model, generation_language, translate_to) for a target language.

    Two cases:
      * Gemma writes this language well -> ask for it directly.
      * It does not -> write in English, then translate. Forcing a model into a
        language it writes badly produces broken text; translating clean
        English is far better.
    """
    if target in NATIVE_LANGS:
        return settings.VLLM_MODEL, target, None
    return settings.VLLM_MODEL, "en", target


def language_directive(code: str) -> str:
    name = language_name(code)
    if code == "hi":
        return (
            "ABSOLUTE LANGUAGE RULE: Write the ENTIRE output in natural, correct Hindi "
            "using Devanagari (शुद्ध, सरल हिंदी). Every heading, label and sentence must "
            "be in Hindi. Do NOT write English sentences (well-known proper nouns and "
            "technical terms may keep their usual form). Correct spelling, grammar, "
            "मात्राएँ and genders."
        )
    if code == "en":
        return (
            "ABSOLUTE LANGUAGE RULE: Write the ENTIRE output in clear English. Do NOT "
            "use any other script anywhere - not even a single word or heading."
        )
    return (
        f"ABSOLUTE LANGUAGE RULE: The video is in {name}. Write the ENTIRE output in "
        f"{name} ONLY. Every heading, label and sentence MUST be in {name}. Do NOT write "
        f"in English or any other language. Use correct, natural {name} grammar and "
        "spelling. Well-known proper nouns and technical terms may keep their usual form."
    )


# ---------------------------------------------------------------------------
# Prompts
#
# The section labels are injected ALREADY TRANSLATED. That matters: an earlier
# version left literal English labels ("## Overview", "**Key Point:**") in the
# template and just asked the model to translate them. Models copy the template
# verbatim, and once the first heading is English the whole answer continues in
# English - which is exactly the bug this fixes.
# ---------------------------------------------------------------------------
LABELS = {
    "en": {"overview": "Overview", "key_point": "Key Point", "background": "Background",
           "details": "Details", "conclusion": "Conclusion & Key Takeaways",
           "in_summary": "In summary", "key_points": "Key Points"},
    "hi": {"overview": "अवलोकन", "key_point": "मुख्य बिंदु", "background": "पृष्ठभूमि",
           "details": "विशेष विवरण", "conclusion": "निष्कर्ष और मुख्य बातें",
           "in_summary": "संक्षेप में", "key_points": "मुख्य बिंदु"},
    "mr": {"overview": "आढावा", "key_point": "मुख्य मुद्दा", "background": "पार्श्वभूमी",
           "details": "तपशील", "conclusion": "निष्कर्ष आणि महत्त्वाचे मुद्दे",
           "in_summary": "थोडक्यात", "key_points": "मुख्य मुद्दे"},
    "gu": {"overview": "ઝાંખી", "key_point": "મુખ્ય મુદ્દો", "background": "પૃષ્ઠભૂમિ",
           "details": "વિગતો", "conclusion": "નિષ્કર્ષ અને મુખ્ય બાબતો",
           "in_summary": "ટૂંકમાં", "key_points": "મુખ્ય મુદ્દા"},
    "bn": {"overview": "সংক্ষিপ্ত বিবরণ", "key_point": "মূল বিষয়", "background": "পটভূমি",
           "details": "বিস্তারিত", "conclusion": "উপসংহার ও মূল বিষয়",
           "in_summary": "সংক্ষেপে", "key_points": "মূল বিষয়সমূহ"},
    "ta": {"overview": "மேலோட்டம்", "key_point": "முக்கிய கருத்து", "background": "பின்னணி",
           "details": "விவரங்கள்", "conclusion": "முடிவும் முக்கிய கருத்துகளும்",
           "in_summary": "சுருக்கமாக", "key_points": "முக்கிய கருத்துகள்"},
    "te": {"overview": "అవలోకనం", "key_point": "ముఖ్య అంశం", "background": "నేపథ్యం",
           "details": "వివరాలు", "conclusion": "ముగింపు మరియు ముఖ్యాంశాలు",
           "in_summary": "సంక్షిప్తంగా", "key_points": "ముఖ్య అంశాలు"},
    "kn": {"overview": "ಅವಲೋಕನ", "key_point": "ಮುಖ್ಯ ಅಂಶ", "background": "ಹಿನ್ನೆಲೆ",
           "details": "ವಿವರಗಳು", "conclusion": "ತೀರ್ಮಾನ ಮತ್ತು ಮುಖ್ಯಾಂಶಗಳು",
           "in_summary": "ಸಂಕ್ಷಿಪ್ತವಾಗಿ", "key_points": "ಮುಖ್ಯ ಅಂಶಗಳು"},
    "ml": {"overview": "അവലോകനം", "key_point": "പ്രധാന ആശയം", "background": "പശ്ചാത്തലം",
           "details": "വിശദാംശങ്ങൾ", "conclusion": "നിഗമനവും പ്രധാന കാര്യങ്ങളും",
           "in_summary": "ചുരുക്കത്തിൽ", "key_points": "പ്രധാന ആശയങ്ങൾ"},
    "pa": {"overview": "ਸੰਖੇਪ", "key_point": "ਮੁੱਖ ਨੁਕਤਾ", "background": "ਪਿਛੋਕੜ",
           "details": "ਵੇਰਵੇ", "conclusion": "ਸਿੱਟਾ ਅਤੇ ਮੁੱਖ ਗੱਲਾਂ",
           "in_summary": "ਸੰਖੇਪ ਵਿੱਚ", "key_points": "ਮੁੱਖ ਨੁਕਤੇ"},
    "ur": {"overview": "جائزہ", "key_point": "اہم نکتہ", "background": "پس منظر",
           "details": "تفصیلات", "conclusion": "نتیجہ اور اہم باتیں",
           "in_summary": "خلاصہ یہ کہ", "key_points": "اہم نکات"},
}


def labels_for(code: str) -> dict[str, str]:
    """Localised labels, or English ones plus an explicit translate instruction."""
    return LABELS.get(code, LABELS["en"])


def _label_rule(code: str) -> str:
    if code in LABELS or code == "en":
        return ""
    name = language_name(code)
    return (
        f"\n- The section labels below are shown in English only as a guide. Write "
        f"every label in {name} instead - do not leave any English label in the output."
    )


def summary_prompt(code: str) -> str:
    L = labels_for(code)
    return f"""Write a natural, engaging, and easy-to-scan summary of the video from the content below, in Markdown. Write like a skilled editor explaining the video clearly to a curious reader. TARGET LENGTH: about 350-400 words TOTAL (never more than ~420 words). Be selective, but cover the MAIN ideas from the beginning, middle, and end.

Use this structure:

## {L["overview"]}
A short 3-4 sentence overview that immediately tells the reader the subject, central idea, and why it matters. Start with the substance, not phrases such as "this video discusses".

Then write 3 to 6 sections in the order the ideas appear. Give each section a short, specific, descriptive heading in the SAME language as the rest of the output:

## <your real descriptive title>
Explain the idea in one short, flowing paragraph. Use plain language and natural transitions. When a section contains several distinct facts, steps, reasons, or examples, present them as 2-4 concise bullet points instead of packing them into a dense paragraph. Do not force bullets into every section.

Then finish with:

## {L["conclusion"]}
Give 3-5 concise bullet points containing the most useful conclusions or takeaways. Each bullet must add new information rather than repeat a sentence from above.

Strict rules:
- Make the writing flow naturally and feel enjoyable to read, not like a form or a list of database fields. Vary sentence openings and avoid repetitive labels such as "Key Point", "Background", and "Details".
- Keep paragraphs short (normally 2-4 sentences). Bold only a few genuinely important names, terms, numbers, or conclusions; do not bold whole sentences.
- Keep the WHOLE summary around 350-400 words. Merge related topics and do NOT repeat the same point in multiple sections.
- Be FAITHFUL and ACCURATE: real names, events, dates and numbers exactly; never confuse two people or events, never invent.
- Be engaging through clarity and specificity, never through hype, clickbait, invented emotion, or unsupported claims.
- NEVER include caption noise like "[Music]" or anything in square brackets.
- Start directly with "## {L["overview"]}". Always use REAL section titles, never a placeholder.{_label_rule(code)}"""


def key_points_prompt(code: str) -> str:
    L = labels_for(code)
    return f"""From the video content below, write the key points ONLY, in Markdown.

- Start with "## {L["key_points"]}".
- 6 to 10 numbered points, in the order they are discussed.
- Each point: bold the core idea in 3-6 words, then explain it in one or two sentences with the real names, numbers and facts.
- No introduction, no conclusion, no filler like "the video discusses".
- Never include anything in square brackets.{_label_rule(code)}"""

def prompt_for(mode: str, code: str) -> str:
    return key_points_prompt(code) if mode == "key_points" else summary_prompt(code)



# ---------------------------------------------------------------------------
# Full-notes prompts (the PDF). These describe the SHAPE of the output rather
# than giving literal labels, so there is no English text for the model to copy
# - the language directive wrapped around them decides the language.
# ---------------------------------------------------------------------------
_NOTES_FORMAT_RULE = """- COMPLETENESS IS THE WHOLE POINT. These notes replace watching the video. A reader who only has your notes must learn EVERYTHING the video taught, in the same order.
- Do NOT summarise, shorten, generalise or "cover the highlights". Write up every single thing in this part: every claim, example, story, name, place, date, number, definition, step, comparison, warning, aside, question asked, answer given, quotation and conclusion.
- There is NO length limit. If this part of the transcript contains 15 distinct points, write all 15. Long output is correct output - never stop early to keep it short.
- If something is repeated in the transcript for emphasis, write it once, clearly - but never drop a point because it "sounds similar" to another one.
- Keep the speaker's own examples and phrasing where they carry meaning; do not replace a concrete example with a vague description of it.
- CHOOSE THE SHAPE FROM THE CONTENT TYPE:
  \u2022 STORY / narrative / biography told as a story: each "## Heading" followed by flowing detailed PARAGRAPHS (3-6 sentences each). No bullet points for story content.
  \u2022 INFORMATIONAL (lesson, tutorial, documentary, travel, news, how-to, a facts podcast, comparisons, lists): under each "## Heading", a short intro line where it helps, PLUS bullet points where each bullet bolds the **key idea / term** and then explains it in 1-2 clear sentences.
  \u2022 Use "### Sub-headings" freely to group related points - more structure is better than less.
- Every heading and sub-heading must be in the output language - never leave one in English.
- You MAY bold **key names, terms, dates, numbers**.
- STYLE FOR ALL AGES: write so a school student, a child and an elderly reader all find it easy and interesting."""

NOTES_FIRST_PROMPT = f"""You are writing COMPLETE, exhaustive study notes for a video - nothing may be missed. This is the FIRST part of the transcript.

- Begin with "# <a concise, clear topic title>" then a 2-3 sentence introduction PARAGRAPH about the whole video.
- Then cover THIS part fully in "## Heading" sections, per the rules below.
{_NOTES_FORMAT_RULE}
- Do not invent anything that is not in the transcript.
- Do NOT write a final conclusion - more parts follow.
- Never include caption noise like "[Music]" or anything in square brackets."""

NOTES_SEGMENT_PROMPT = f"""Continue the COMPLETE, exhaustive notes for the SAME video. This is a LATER part of its transcript.

- Write up THIS part in full, in "## Heading" sections, continuing naturally from the earlier parts.
- The first sentence or two may overlap with the previous part - do not repeat what was already written, start from where the new material begins.
- Do NOT repeat the title or an overall introduction. Do NOT add a final wrap-up unless this is clearly the very end of the video.
{_NOTES_FORMAT_RULE}
- Do not invent anything that is not in the transcript.
- Never include caption noise like "[Music]" or anything in square brackets."""

# ---------------------------------------------------------------------------
# vLLM serves Gemma with --default-chat-template-kwargs {"enable_thinking":
# false}, so no <think> block is ever produced. This stays as a cheap guard on
# the non-streaming path in case that flag is ever dropped; the streaming path
# does not pay for it, because buffering a whole answer to look for a block
# that cannot appear is exactly the latency this deployment is trying to avoid.
# ---------------------------------------------------------------------------
def strip_think(text: str) -> str:
    if not text:
        return text
    out = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.I)
    idx = out.rfind("</think>")
    if idx >= 0:
        out = out[idx + len("</think>") :]
    open_idx = out.lower().find("<think>")
    if open_idx >= 0:
        out = out[:open_idx]
    return out.strip()


# ---------------------------------------------------------------------------
# vLLM / OpenAI-compatible chat completions
# ---------------------------------------------------------------------------
def _vllm_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if settings.VLLM_API_KEY:
        headers["Authorization"] = f"Bearer {settings.VLLM_API_KEY}"
    if settings.CF_ACCESS_CLIENT_ID:
        headers["CF-Access-Client-Id"] = settings.CF_ACCESS_CLIENT_ID
    if settings.CF_ACCESS_CLIENT_SECRET:
        headers["CF-Access-Client-Secret"] = settings.CF_ACCESS_CLIENT_SECRET
    return headers


async def vllm_client() -> httpx.AsyncClient:
    """Return one connection-pooled client for all model calls in this worker."""
    global _vllm_client
    if _vllm_client is not None and not _vllm_client.is_closed:
        return _vllm_client
    # There is no await between this check and assignment, so tasks on the
    # worker's event loop cannot race and create duplicate clients.
    timeout = httpx.Timeout(
        settings.VLLM_TIMEOUT_SECONDS,
        connect=settings.VLLM_CONNECT_TIMEOUT_SECONDS,
    )
    # Sized from the same number that gates the semaphore. httpx defaults to
    # 100 connections, which let this process open far more sockets than vLLM
    # could ever serve - the pool should not be able to outrun the gate.
    slots = max(1, settings.VLLM_MAX_CONCURRENCY)
    limits = httpx.Limits(
        max_connections=slots + 2, max_keepalive_connections=slots + 2
    )
    _vllm_client = httpx.AsyncClient(timeout=timeout, limits=limits)
    return _vllm_client


def vllm_slots() -> asyncio.Semaphore:
    """The one gate in front of every call this process makes to vLLM.

    NOTES_CONCURRENCY bounds the chunks of a SINGLE video, and for a long time
    that was the only limit there was - so ten people on ten videos put ten
    times that many generations on a server built to run --max-num-seqs of them
    at once. The overflow queued inside vLLM, timed out, and was resent by the
    retry loop into the very queue that had just failed it.

    Bounding it here is what stops that: a request that cannot get a slot waits
    in Python, where waiting is cheap and cancellation actually works, instead
    of in vLLM, where it is neither.
    """
    global _vllm_slots
    if _vllm_slots is None:
        # No await between the check and the assignment, so two tasks on this
        # worker's loop cannot race and build two different semaphores.
        _vllm_slots = asyncio.Semaphore(max(1, settings.VLLM_MAX_CONCURRENCY))
    return _vllm_slots


@asynccontextmanager
async def vllm_slot(wait: float | None = None) -> AsyncIterator[None]:
    """Hold a generation slot for the WHOLE stream, not just the request.

    vLLM keeps a sequence busy until its last token is out, so releasing when
    the response headers arrive would let straight back in everything this is
    meant to hold back.

    `wait` is how long to queue for a slot. It is not one number for everything:
    a person watching a summary appear should be told the server is busy within
    a couple of minutes, while a chunk of a PDF is batch work that nobody is
    staring at and should wait far longer. Getting this wrong is not academic -
    with one 300s deadline for both, 30 concurrent PDFs lost most of their
    sections to the timeout and produced near-empty documents.
    """
    sem = vllm_slots()
    wait = settings.VLLM_QUEUE_TIMEOUT_SECONDS if wait is None else wait
    if wait and wait > 0:
        try:
            await asyncio.wait_for(sem.acquire(), wait)
        except (asyncio.TimeoutError, TimeoutError):
            raise VLLMBusy(
                "The AI server is busy with other videos right now. "
                "Please try again in a few minutes."
            ) from None
    else:
        await sem.acquire()
    try:
        yield
    finally:
        # Never await in here. This also runs while the generator is being
        # closed after a client disconnect, and awaiting during that unwind is
        # an error - releasing a semaphore is not.
        sem.release()


async def close_vllm_client() -> None:
    global _vllm_client
    client, _vllm_client = _vllm_client, None
    if client is not None and not client.is_closed:
        await client.aclose()


async def stream_chat(
    *,
    model: str,
    system: str,
    content: str,
    num_predict: int = 3000,
    temperature: float = 0.4,
    queue_wait: float | None = None,
) -> AsyncIterator[str]:
    """Yield text deltas from vLLM's /v1/chat/completions SSE stream."""
    url = settings.VLLM_URL.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ],
        "stream": True,
        "temperature": temperature,
        "top_p": 0.9,
        "max_tokens": num_predict,
    }

    client = await vllm_client()
    # Every path into vLLM - summary, notes chunk, translation - goes through
    # this one gate, and holds it until the last token is out.
    async with vllm_slot(queue_wait):
        async with client.stream(
            "POST", url, json=payload, headers=_vllm_headers()
        ) as res:
            if res.status_code != 200:
                body = (await res.aread()).decode("utf-8", "replace")[:500]
                raise RuntimeError(f"vLLM HTTP {res.status_code}: {body}")

            async for line in res.aiter_lines():
                line = line.strip()
                if not line or line.startswith(":"):
                    continue
                if line.startswith("data:"):
                    line = line[5:].strip()
                if line == "[DONE]":
                    return
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                choices = obj.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                token = delta.get("content")
                if token:
                    yield token


async def collect_chat(
    *, model: str, system: str, content: str, num_predict: int = 3000,
    queue_wait: float | None = None,
) -> str:
    parts: list[str] = []
    async for token in stream_chat(
        model=model, system=system, content=content, num_predict=num_predict,
        queue_wait=queue_wait,
    ):
        parts.append(token)
    return strip_think("".join(parts))


async def stream_summary(
    transcript: str, *, lang: str, mode: str = "summary"
) -> AsyncIterator[str]:
    """Stream the on-screen summary, hiding any reasoning block as it goes."""
    model = model_for(lang)
    # Language rule first (highest priority), prompt second, reminder last -
    # models weight the opening and closing of a system prompt most heavily.
    system = language_directive(lang) + "\n\n" + prompt_for(mode, lang)
    if lang != "en":
        system += (
            f"\n\nFINAL REMINDER: every single word of your answer - headings, labels, "
            f"body text - must be in {language_name(lang)}. Do not write in English."
        )

    # Straight through. Gemma emits no reasoning block, so there is nothing to
    # hide and no reason to buffer - the first token reaches the browser the
    # moment vLLM produces it.
    async for token in stream_chat(
        model=model,
        system=system,
        content=sample_for_model(transcript, settings.TRANSCRIPT_MAX_CHARS),
        num_predict=settings.SUMMARY_NUM_PREDICT,
        temperature=0.7 if mode == "summary" else 0.4,
    ):
        yield token


def split_into_chunks(text: str, size: int, overlap: int) -> list[str]:
    chunks: list[str] = []
    i = 0
    while i < len(text):
        end = min(len(text), i + size)
        if end < len(text):
            window = text[i:end]
            cut = max(window.rfind(". "), window.rfind("। "), window.rfind(" "))
            if cut > size * 0.6:
                end = i + cut + 1
        chunks.append(text[i:end].strip())
        if end >= len(text):
            break
        i = max(0, end - overlap)
    return chunks


def _missing_marker(part: int, total: int, lang: str) -> str:
    """The label that stands in for a section that could not be written.

    It goes INSIDE the document, in the failed section's own place. The
    on-screen warning disappears the moment the tab closes, and the PDF is what
    the user keeps - a silent gap in it reads as though the video simply had
    nothing to say there, which is the one impression these notes must never
    give. Being told "part 3 is missing, try again" is always better.
    """
    note = (
        f"_[Part {part} of {total}: this section could not be written, so "
        f"{total} sections are missing one piece. Everything else is complete - "
        f"run it again to fill this gap.]_"
    )
    if lang != "en":
        note += f"\n\n_[{language_name(lang)}: {part}/{total}]_"
    return note


def effective_chunk_chars() -> int:
    """NOTES_CHUNK_CHARS, clamped to what the model can actually accept.

    A chunk has to share the context window with the system prompt and with the
    answer the model is asked to write. Nothing was checking that: set
    NOTES_CHUNK_CHARS high enough and every notes request would be rejected for
    exceeding max_model_len, which is a configuration mistake that only shows up
    under load. Deriving the ceiling instead means the chunk size can be tuned
    freely and the worst case is a slightly smaller chunk than requested.
    """
    budget_tokens = (
        max(1, settings.VLLM_MAX_MODEL_LEN)
        - max(1, settings.NOTES_NUM_PREDICT)
        - _PROMPT_TOKEN_ALLOWANCE
    )
    if budget_tokens <= 0:
        # NOTES_NUM_PREDICT alone does not fit; leave room for a minimal chunk.
        budget_tokens = max(1, settings.VLLM_MAX_MODEL_LEN // 4)
    ceiling = int(budget_tokens * max(1.0, settings.CHARS_PER_TOKEN))
    wanted = max(1, settings.NOTES_CHUNK_CHARS)
    if wanted > ceiling:
        logger.warning(
            "NOTES_CHUNK_CHARS=%s exceeds what max_model_len=%s leaves for input "
            "(%s chars); using %s",
            wanted, settings.VLLM_MAX_MODEL_LEN, ceiling, ceiling,
        )
        return ceiling
    return wanted


def _budget_for(chunk: str) -> int:
    """Scale the notes output allowance to how much material the chunk holds.

    A FULL chunk keeps the configured exhaustive-notes budget; a 300-character
    tail does not, because giving it the same 4,096 tokens invites padding and
    repetition and keeps the GPU busy long after the useful answer.

    "Full" means NOTES_CHUNK_CHARS. That used to be hardcoded to 6000, which
    quietly broke the scaling the moment the chunk size was configured to
    anything else: at 12,000-char chunks every half-full chunk still claimed the
    entire budget.
    """
    full = effective_chunk_chars()
    cap = max(1, settings.NOTES_NUM_PREDICT)
    # A floor proportional to the budget rather than a fixed 768, so it keeps
    # meaning the same thing if NOTES_NUM_PREDICT is tuned.
    floor = max(1, cap // 5)
    proportional = (len(chunk) * cap + full - 1) // full
    return min(cap, max(floor, proportional))


async def full_notes(
    transcript: str, *, lang: str, on_progress=None, on_warning=None
) -> str:
    """Exhaustive notes covering the WHOLE video - this is what the PDF shows.

    Unlike the on-screen summary, nothing here is sampled away: the entire
    transcript is split into chunks and every chunk is written up in full. The
    output is meant to be long - a two-hour lecture legitimately produces
    dozens of pages.

    Three things protect completeness:
      * chunks are small (NOTES_CHUNK_CHARS) with an overlap, so the model has
        little reason to compress and no point falls between two chunks;
      * a chunk that fails is retried, and if it still fails the caller is told
        which part is missing - it is never dropped silently;
      * NOTES_MAX_CHUNKS defaults to 0 (no cap), and any cap that is set is
        reported too.
    """
    model = model_for(lang)
    directive = language_directive(lang)
    reminder = (
        ""
        if lang == "en"
        else f"\n\nFINAL REMINDER: write everything in {language_name(lang)} only."
    )

    chunks = split_into_chunks(
        transcript, effective_chunk_chars(), settings.NOTES_CHUNK_OVERLAP
    )
    if settings.NOTES_MAX_CHUNKS and len(chunks) > settings.NOTES_MAX_CHUNKS:
        dropped = len(chunks) - settings.NOTES_MAX_CHUNKS
        chunks = chunks[: settings.NOTES_MAX_CHUNKS]
        if on_warning:
            await on_warning(
                f"This video is very long: the last {dropped} section(s) were left "
                f"out because NOTES_MAX_CHUNKS is set to {settings.NOTES_MAX_CHUNKS}. "
                "Set it to 0 for no limit."
            )

    total = len(chunks)
    parts: list[str] = [""] * total
    failed: list[int] = []
    done = 0
    started = 0
    lock = asyncio.Lock()

    # Tell the caller the shape of the job before any of it finishes. Chunks
    # run in PARALLEL, so the first completion can be minutes away - and until
    # then the UI had nothing at all to show and sat on one frozen percentage.
    if on_progress:
        await on_progress(0, total, 0)
    semaphore = asyncio.Semaphore(max(1, settings.NOTES_CONCURRENCY))

    async def write_chunk(idx: int, chunk: str) -> None:
        nonlocal done, started
        base = NOTES_FIRST_PROMPT if idx == 0 else NOTES_SEGMENT_PROMPT
        text = ""
        attempts = max(1, settings.NOTES_CHUNK_RETRIES)
        async with semaphore:
            # A slot was taken, so this part is genuinely being written now.
            async with lock:
                started += 1
                if on_progress:
                    await on_progress(done, total, started)
            for attempt in range(attempts):
                try:
                    text = await collect_chat(
                        model=model,
                        system=directive + "\n\n" + base + reminder,
                        content=chunk,
                        num_predict=_budget_for(chunk),
                        queue_wait=settings.NOTES_QUEUE_TIMEOUT_SECONDS,
                    )
                    if text.strip():
                        break
                except VLLMBusy as exc:
                    # "Every slot is taken" means come back later, not that
                    # anything is broken - so this one IS worth retrying, after
                    # a wait. Treating it like a dead server is what emptied 19
                    # of 30 PDFs in the end-to-end run: each chunk gave up on
                    # first contact and left a "missing section" marker behind.
                    logger.warning(
                        "notes chunk %s/%s waiting for a slot (attempt %s): %s",
                        idx + 1, total, attempt + 1, exc,
                    )
                except httpx.TimeoutException as exc:
                    # A read timeout is different: the request WAS accepted and
                    # then went quiet. Re-sending it adds load to a server that
                    # is already not answering, and that feedback loop is how a
                    # busy minute became a hang.
                    logger.warning(
                        "notes chunk %s/%s gave up (no answer): %s",
                        idx + 1, total, exc,
                    )
                    break
                except Exception as exc:
                    logger.warning(
                        "notes chunk %s/%s attempt %s failed: %s",
                        idx + 1, total, attempt + 1, exc,
                    )
                if attempt + 1 < attempts:
                    # Back off, so a transient upstream hiccup is not answered
                    # with three requests in as many milliseconds.
                    await asyncio.sleep(min(8.0, 2.0 * (2 ** attempt)))
        parts[idx] = (text or "").strip()
        async with lock:
            done += 1
            if not parts[idx]:
                failed.append(idx + 1)
                parts[idx] = _missing_marker(idx + 1, total, lang)
            if on_progress:
                await on_progress(done, total, started)

    await asyncio.gather(*(write_chunk(i, c) for i, c in enumerate(chunks)))

    if failed and on_warning:
        await on_warning(
            f"{len(failed)} of {total} sections could not be written "
            f"(part {', '.join(str(n) for n in sorted(failed))}). "
            "Everything else is included - try again to fill the gaps."
        )

    # A handful of gaps is a document with holes, and the markers say so. Most
    # of it missing is not a document at all, and handing one over - cached,
    # rendered to PDF, counted a success - is worse than admitting the failure.
    # This is exactly what a 30-way end-to-end run produced before the retry
    # above was fixed: 19 of 30 "notes" were nothing but missing-section labels.
    if total and len(failed) > total * settings.NOTES_MAX_FAILED_FRACTION:
        raise RuntimeError(
            f"Only {total - len(failed)} of {total} sections could be written. "
            "The server is too busy to produce complete notes right now - "
            "please try again shortly."
        )

    return "\n\n".join(p for p in parts if p).strip()
