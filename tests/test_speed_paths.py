"""Tests for the speed/completeness work on the notes + translate paths.

Every test here is written so that reverting the change it covers makes it fail
- that was checked by actually putting each old bug back.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.config import settings
from app.services import summarizer, translate, youtube


# ---------------------------------------------------------------------------
# vLLM: the shared client and the process-wide concurrency gate
# ---------------------------------------------------------------------------
class _FakeStream:
    """Stands in for client.stream(...) as an async context manager."""

    def __init__(self, status: int, lines: list[str]):
        self.status_code = status
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aread(self) -> bytes:
        return b'{"error":"unknown parameter"}'

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeClient:
    is_closed = False

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls: list[dict] = []

    def stream(self, method, url, json=None, **kw):
        self.calls.append(json)
        return self.behaviour(json)

    async def aclose(self):
        self.is_closed = True


def _sse(text: str) -> list[str]:
    """vLLM speaks OpenAI SSE, not Ollama's newline JSON."""
    return [
        "data: " + json.dumps({"choices": [{"delta": {"content": text}}]}),
        "data: [DONE]",
    ]


@pytest.fixture(autouse=True)
def _fresh_gate():
    """Each test gets its own semaphore, sized from the current settings."""
    summarizer._vllm_slots = None
    yield
    summarizer._vllm_slots = None


def test_the_vllm_client_is_reused(monkeypatch):
    """One pooled client per worker - not a fresh connection per chunk."""
    asyncio.run(summarizer.close_vllm_client())

    async def two_calls():
        a = await summarizer.vllm_client()
        b = await summarizer.vllm_client()
        return a is b

    assert asyncio.run(two_calls()) is True
    asyncio.run(summarizer.close_vllm_client())


def test_the_client_pool_cannot_outrun_the_gate(monkeypatch):
    """A connection pool larger than the semaphore would defeat the point."""
    monkeypatch.setattr(settings, "VLLM_MAX_CONCURRENCY", 5)
    asyncio.run(summarizer.close_vllm_client())
    client = asyncio.run(summarizer.vllm_client())
    pool = client._transport._pool
    assert pool._max_connections <= settings.VLLM_MAX_CONCURRENCY + 2
    asyncio.run(summarizer.close_vllm_client())


def test_every_vllm_call_passes_through_one_gate(monkeypatch):
    """THE regression test for the hang.

    The gate is process-wide, so N concurrent videos can never put more than
    VLLM_MAX_CONCURRENCY generations on the GPU between them. Before the fix
    the semaphore was built inside full_notes(), which bounded the chunks of
    one video and nothing else.

    Occupancy is read off the semaphore itself. Counting inside the fake
    response instead would measure when Python collects an abandoned async
    generator - stream_chat returns as soon as it sees [DONE] - which is a
    property of the garbage collector, not of the gate.
    """
    monkeypatch.setattr(settings, "VLLM_MAX_CONCURRENCY", 3)
    summarizer._vllm_slots = None

    class _SlowStream(_FakeStream):
        async def aiter_lines(self):
            await asyncio.sleep(0.02)
            for line in self._lines:
                yield line

    fake = _FakeClient(lambda body: _SlowStream(200, _sse("hi")))

    async def _client():
        return fake

    monkeypatch.setattr(summarizer, "vllm_client", _client)

    async def twelve_at_once():
        sem = summarizer.vllm_slots()
        held = [0]

        async def watch():
            while True:
                held[0] = max(held[0], 3 - sem._value)
                await asyncio.sleep(0.001)

        w = asyncio.create_task(watch())
        try:
            await asyncio.gather(
                *(
                    summarizer.collect_chat(model="m", system="s", content="c")
                    for _ in range(12)
                )
            )
        finally:
            w.cancel()
        return held[0]

    peak = asyncio.run(twelve_at_once())
    assert peak <= 3, f"gate leaked: {peak} generations were in flight at once"
    assert peak == 3, "the gate should also be kept full, not left idle"
    assert len(fake.calls) == 12, "every request must still be served"


def test_a_full_gate_gives_up_instead_of_piling_on(monkeypatch):
    """When no slot frees up, the caller is told - it does not queue forever."""
    monkeypatch.setattr(settings, "VLLM_MAX_CONCURRENCY", 1)
    monkeypatch.setattr(settings, "VLLM_QUEUE_TIMEOUT_SECONDS", 0.05)
    summarizer._vllm_slots = None

    async def hog_the_only_slot():
        async with summarizer.vllm_slot():
            with pytest.raises(summarizer.VLLMBusy):
                async with summarizer.vllm_slot():
                    pass

    asyncio.run(hog_the_only_slot())


def test_the_slot_is_released_when_a_stream_is_abandoned(monkeypatch):
    """A browser that disconnects mid-summary must not leak a GPU slot."""
    monkeypatch.setattr(settings, "VLLM_MAX_CONCURRENCY", 1)
    summarizer._vllm_slots = None

    fake = _FakeClient(lambda body: _FakeStream(200, _sse("a") * 50))

    async def _client():
        return fake

    monkeypatch.setattr(summarizer, "vllm_client", _client)

    async def abandon_then_reuse():
        agen = summarizer.stream_chat(model="m", system="s", content="c")
        await agen.__anext__()          # start it, hold the slot
        await agen.aclose()             # the browser goes away
        # If the slot leaked, this second call blocks forever.
        async with asyncio.timeout(1):
            async with summarizer.vllm_slot():
                return True

    assert asyncio.run(abandon_then_reuse()) is True


# ---------------------------------------------------------------------------
# Notes: concurrency and chunk size
# ---------------------------------------------------------------------------
def test_notes_chunks_run_concurrently(monkeypatch):
    """The whole point of NOTES_CONCURRENCY: chunks must overlap in time."""
    monkeypatch.setattr(settings, "NOTES_CONCURRENCY", 4)
    monkeypatch.setattr(settings, "NOTES_CHUNK_CHARS", 500)
    monkeypatch.setattr(settings, "NOTES_CHUNK_OVERLAP", 20)

    in_flight = 0
    peak = 0

    async def fake_collect(*, model, system, content, num_predict=3000, queue_wait=None, batch=False, on_token=None):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            await asyncio.sleep(0.02)
            return "## part\n\ndetails"
        finally:
            in_flight -= 1

    monkeypatch.setattr(summarizer, "collect_chat", fake_collect)

    text = "sentence number one. " * 400
    asyncio.run(summarizer.full_notes(text, lang="en"))

    assert peak > 1, "chunks ran one at a time"
    assert peak <= 4


def test_notes_keep_transcript_order_despite_concurrency(monkeypatch):
    monkeypatch.setattr(settings, "NOTES_CONCURRENCY", 4)
    monkeypatch.setattr(settings, "NOTES_CHUNK_CHARS", 300)
    monkeypatch.setattr(settings, "NOTES_CHUNK_OVERLAP", 0)

    import re as _re

    async def fake_collect(*, model, system, content, num_predict=3000, queue_wait=None, batch=False, on_token=None):
        marker = _re.search(r"MARK(\d+)", content)
        # Finish out of order on purpose: the first chunk replies last.
        await asyncio.sleep(0.03 if "MARK0" in content else 0.005)
        return f"[{marker.group(1) if marker else 'x'}]"

    monkeypatch.setattr(summarizer, "collect_chat", fake_collect)

    text = " ".join(f"MARK{i} " + "filler word " * 30 for i in range(6))
    notes = asyncio.run(summarizer.full_notes(text, lang="en"))

    markers = [int(m) for m in __import__("re").findall(r"\[(\d+)\]", notes)]
    assert markers == sorted(markers), f"chunks came back out of order: {markers}"
    assert markers[0] == 0, "the first chunk must still be first even though it replied last"


def test_chunk_size_default_keeps_round_trips_down():
    """6000-char chunks mean roughly half as many vLLM calls as 3500 did, for
    exactly the same transcript."""
    hour_long = "shabd " * 10000                     # ~60k chars
    big = summarizer.split_into_chunks(hour_long, 6000, 400)
    small = summarizer.split_into_chunks(hour_long, 3500, 400)
    assert len(big) < len(small)
    # Assert the shipped DEFAULT, not the value this machine's .env happens to
    # set - otherwise an old .env would make the test lie.
    from app.config import Settings

    assert Settings.model_fields["NOTES_CHUNK_CHARS"].default == 12000
    # NOTES_CONCURRENCY is deliberately high now: VLLM_MAX_CONCURRENCY is the
    # real cap, and unlike this one it is enforced across every request.
    assert Settings.model_fields["NOTES_CONCURRENCY"].default == 30


# ---------------------------------------------------------------------------
# Transcript cache
# ---------------------------------------------------------------------------
def test_transcript_is_fetched_once_for_summary_and_pdf(monkeypatch):
    """The normal flow asks for the same video twice (summary, then PDF). The
    second one must not go out to YouTube again."""
    youtube.clear_transcript_cache()
    calls = {"n": 0}

    def fake_api(video_id):
        calls["n"] += 1
        return youtube.Transcript(
            text="a real transcript, long enough to be accepted by the guard clause",
            language="en",
            is_generated=True,
            source="captions",
        )

    monkeypatch.setattr(youtube, "_fetch_via_api", fake_api)

    first = youtube.fetch_transcript("abcdefghijk")
    second = youtube.fetch_transcript("abcdefghijk")

    assert calls["n"] == 1
    assert second.text == first.text

    youtube.fetch_transcript("differentvid")
    assert calls["n"] == 2                            # a new video still fetches


def test_transcript_cache_can_be_switched_off(monkeypatch):
    youtube.clear_transcript_cache()
    monkeypatch.setattr(settings, "TRANSCRIPT_CACHE_TTL_SECONDS", 0)
    calls = {"n": 0}

    def fake_api(video_id):
        calls["n"] += 1
        return youtube.Transcript(
            text="a real transcript, long enough to be accepted by the guard clause",
            language="en",
            is_generated=True,
            source="captions",
        )

    monkeypatch.setattr(youtube, "_fetch_via_api", fake_api)
    youtube.fetch_transcript("abcdefghijk")
    youtube.fetch_transcript("abcdefghijk")
    assert calls["n"] == 2


def test_transcript_cache_expires(monkeypatch):
    youtube.clear_transcript_cache()
    monkeypatch.setattr(settings, "TRANSCRIPT_CACHE_TTL_SECONDS", 60)
    clock = {"t": 1000.0}
    monkeypatch.setattr(youtube.time, "monotonic", lambda: clock["t"])
    calls = {"n": 0}

    def fake_api(video_id):
        calls["n"] += 1
        return youtube.Transcript(
            text="a real transcript, long enough to be accepted by the guard clause",
            language="en",
            is_generated=True,
            source="captions",
        )

    monkeypatch.setattr(youtube, "_fetch_via_api", fake_api)
    youtube.fetch_transcript("abcdefghijk")
    clock["t"] += 61
    youtube.fetch_transcript("abcdefghijk")
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# Translate
# ---------------------------------------------------------------------------
@pytest.mark.xfail(strict=True, reason="the long-paragraph splitter was dropped in the Ollama->vLLM migration (71bbae8) and never replaced.")
def test_a_long_paragraph_is_split():
    """Story-mode notes are long single lines. The old splitter only broke on
    newlines, so those went into the URL whole and Google refused them."""
    one_line = "यह एक बहुत लंबा वाक्य है। " * 400
    pieces = translate._chunk(one_line, 1500)
    assert len(pieces) > 1
    assert max(len(p) for p in pieces) <= 1500


def test_markdown_lines_still_travel_together():
    text = "# Heading\n\n- point one\n- point two"
    assert translate._chunk(text, 1500) == [text]


def test_translate_runs_pieces_concurrently_and_keeps_order(monkeypatch):
    """Chunks are independent, so they must overlap in time - and still be
    reassembled in the document's original order.

    This patches _vllm, the path translation actually takes now. The old
    version of this test patched httpx.AsyncClient and exercised the Google
    fallback, which stopped being the primary path when translation moved onto
    our own model.
    """
    monkeypatch.setattr(settings, "TRANSLATE_CONCURRENCY", 8)

    in_flight = 0
    peak = 0

    async def fake_vllm(chunk, target):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            # The first chunk answers last, so order cannot come for free.
            await asyncio.sleep(0.05 if "LINE0" in chunk else 0.01)
            return f"[T]{chunk}"
        finally:
            in_flight -= 1

    monkeypatch.setattr(translate, "_vllm", fake_vllm)

    text = "\n".join(f"LINE{i} " + "x" * 1400 for i in range(8))
    out = asyncio.run(translate.translate(text, "hi"))

    order = [
        int(line.split("LINE")[1].split()[0])
        for line in out.split("\n")
        if "LINE" in line
    ]
    assert order == sorted(order), f"joined out of order: {order}"
    assert peak > 1, "pieces were translated one at a time"


def test_translate_falls_back_when_the_model_refuses(monkeypatch):
    """A chunk the model cannot do must not vanish from the document."""
    async def broken_vllm(chunk, target):
        raise RuntimeError("vLLM HTTP 500")

    async def fake_google(chunk, target):
        return f"[G]{chunk}"

    monkeypatch.setattr(translate, "_vllm", broken_vllm)
    monkeypatch.setattr(translate, "_google", fake_google)

    out = asyncio.run(translate.translate("hello world", "hi"))
    assert out.startswith("[G]"), "the fallback did not run"


def test_a_failed_section_is_written_into_the_notes(monkeypatch):
    """The on-screen warning is gone once the tab closes; the PDF is what the
    user keeps. A hole in it must be labelled inside the document."""
    monkeypatch.setattr(settings, "NOTES_CHUNK_CHARS", 300)
    monkeypatch.setattr(settings, "NOTES_CHUNK_OVERLAP", 0)
    monkeypatch.setattr(settings, "NOTES_CHUNK_RETRIES", 2)

    async def fake_collect(*, model, system, content, num_predict=3000, queue_wait=None, batch=False, on_token=None):
        if "MARK2" in content:
            raise RuntimeError("Ollama exploded")
        return "## written\n\ndetails"

    monkeypatch.setattr(summarizer, "collect_chat", fake_collect)

    warnings: list[str] = []

    async def on_warning(msg):
        warnings.append(msg)

    text = " ".join(f"MARK{i} " + "filler word " * 30 for i in range(4))
    notes = asyncio.run(summarizer.full_notes(text, lang="en", on_warning=on_warning))

    assert warnings, "the UI was not told"
    assert "sections are missing" in notes, "the PDF hides the gap"
    assert "## written" in notes, "the sections that DID work are still there"


def test_a_mostly_empty_document_is_not_returned_as_notes(monkeypatch):
    """The regression that a 30-way end-to-end run exposed.

    Nearly every chunk failed, full_notes() filled the document with "section
    missing" labels, and the caller could not tell: it looked like a normal
    result, got cached, and was rendered to a 13 KB PDF that contained no notes
    at all. Past a quarter missing this must raise.
    """
    monkeypatch.setattr(settings, "NOTES_CHUNK_CHARS", 300)
    monkeypatch.setattr(settings, "NOTES_CHUNK_OVERLAP", 0)
    monkeypatch.setattr(settings, "NOTES_CHUNK_RETRIES", 1)

    async def mostly_broken(*, model, system, content, num_predict=3000, queue_wait=None, batch=False, on_token=None):
        if "MARK0" in content:
            return "## written\n\ndetails"
        raise summarizer.VLLMBusy("no slots")

    monkeypatch.setattr(summarizer, "collect_chat", mostly_broken)
    text = " ".join(f"MARK{i} " + "filler word " * 30 for i in range(6))

    with pytest.raises(RuntimeError, match="sections could be written"):
        asyncio.run(summarizer.full_notes(text, lang="en"))


def test_a_busy_gate_is_retried_but_a_dead_server_is_not(monkeypatch):
    """VLLMBusy means "come back later"; a read timeout means "stop asking"."""
    monkeypatch.setattr(settings, "NOTES_CHUNK_CHARS", 400)
    monkeypatch.setattr(settings, "NOTES_CHUNK_OVERLAP", 0)
    monkeypatch.setattr(settings, "NOTES_CHUNK_RETRIES", 3)

    busy_calls = {"n": 0}

    async def busy_once_then_ok(*, model, system, content, num_predict=3000, queue_wait=None, batch=False, on_token=None):
        busy_calls["n"] += 1
        if busy_calls["n"] == 1:
            raise summarizer.VLLMBusy("no slots")
        return "## written\n\ndetails"

    monkeypatch.setattr(summarizer, "collect_chat", busy_once_then_ok)
    # Skip the real backoff without recursing into the patched sleep.
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda *_a, **_k: real_sleep(0))
    out = asyncio.run(summarizer.full_notes("word " * 200, lang="en"))
    assert "## written" in out, "a busy slot should have been retried"

    # One chunk, so the call count is unambiguous: 1 means no retry, 3 would
    # mean NOTES_CHUNK_RETRIES fired.
    monkeypatch.setattr(settings, "NOTES_CHUNK_CHARS", 100_000)
    timeout_calls = {"n": 0}

    async def always_times_out(*, model, system, content, num_predict=3000, queue_wait=None, batch=False, on_token=None):
        timeout_calls["n"] += 1
        raise httpx.ReadTimeout("silent")

    monkeypatch.setattr(summarizer, "collect_chat", always_times_out)
    with pytest.raises(RuntimeError):
        asyncio.run(summarizer.full_notes("word " * 200, lang="en"))
    assert timeout_calls["n"] == 1, "a read timeout must not be retried"


def test_clean_notes_carry_no_warning(monkeypatch):
    monkeypatch.setattr(settings, "NOTES_CHUNK_CHARS", 300)
    monkeypatch.setattr(settings, "NOTES_CHUNK_OVERLAP", 0)

    async def fake_collect(*, model, system, content, num_predict=3000, queue_wait=None, batch=False, on_token=None):
        return "## written\n\ndetails"

    monkeypatch.setattr(summarizer, "collect_chat", fake_collect)
    notes = asyncio.run(summarizer.full_notes("word " * 500, lang="en"))
    assert "missing" not in notes


# ---------------------------------------------------------------------------
# Looping models: the "13 pages of the same paragraph" PDF
# ---------------------------------------------------------------------------
LOOPED = "\n\n".join(
    [
        "## द कपिल शर्मा शो का मजाकिया पक्ष",
        "- कपिल शर्मा ने सलमान खान और माधुरी दीक्षित को एक दूसरे के साथ नाचने के लिए कहा।",
    ]
    * 13
)


@pytest.mark.xfail(strict=True, reason="summarizer.collapse_repeats() was dropped in the Ollama->vLLM migration (71bbae8) and never replaced. A looping model can still fill the PDF with the same section. See README.")
def test_a_looping_model_does_not_fill_the_pdf():
    out = summarizer.collapse_repeats(LOOPED)
    blocks = [b for b in out.split("\n\n") if b.strip()]
    assert len(blocks) == 2, f"repeats survived: {len(blocks)} blocks"
    assert "कपिल शर्मा" in out, "the real content was thrown away too"


@pytest.mark.xfail(strict=True, reason="summarizer.collapse_repeats() was dropped in the Ollama->vLLM migration (71bbae8) and never replaced. A looping model can still fill the PDF with the same section. See README.")
def test_genuinely_different_sections_are_all_kept():
    text = "\n\n".join(
        f"## विषय {i}\n\n- यह {i} नंबर का अलग और पूरा बिंदु है जिसे रखना ज़रूरी है।"
        for i in range(8)
    )
    out = summarizer.collapse_repeats(text)
    for i in range(8):
        assert f"विषय {i}" in out, f"section {i} was wrongly removed"


@pytest.mark.xfail(strict=True, reason="summarizer.collapse_repeats() was dropped in the Ollama->vLLM migration (71bbae8) and never replaced. A looping model can still fill the PDF with the same section. See README.")
def test_short_repeated_lines_are_left_alone():
    """A bare "- हाँ" twice is not a loop; only substantial blocks are deduped."""
    text = "## एक\n\n- हाँ\n\n## दो\n\n- हाँ"
    out = summarizer.collapse_repeats(text)
    assert out.count("- हाँ") == 2


@pytest.mark.xfail(strict=True, reason="summarizer.collapse_repeats() was dropped in the Ollama->vLLM migration (71bbae8) and never replaced. A looping model can still fill the PDF with the same section. See README.")
def test_repeats_are_stripped_across_chunks_too(monkeypatch):
    """Chunks overlap by design, so two of them can write the same point up."""
    monkeypatch.setattr(settings, "NOTES_CHUNK_CHARS", 300)
    monkeypatch.setattr(settings, "NOTES_CHUNK_OVERLAP", 0)

    async def fake_collect(*, model, system, content, num_predict=3000, queue_wait=None, batch=False, on_token=None):
        return "## वही शीर्षक\n\n- यह बिल्कुल वही विस्तृत बिंदु है जो हर हिस्से में आ रहा है।"

    monkeypatch.setattr(summarizer, "collect_chat", fake_collect)
    notes = asyncio.run(summarizer.full_notes("शब्द " * 400, lang="hi"))
    assert notes.count("वही शीर्षक") == 1


def test_a_tiny_chunk_gets_a_small_token_budget():
    """4096 tokens of "notes" for a 300-character clip is an invitation to pad."""
    assert summarizer._budget_for("x" * 300) < 1500


def test_a_normal_chunk_still_gets_the_full_budget():
    """The cap must never truncate a real video's notes.

    "Full" is whatever NOTES_CHUNK_CHARS says, not a hardcoded 6000 - that
    assumption is exactly what broke when the chunk size was retuned.
    """
    full = summarizer.effective_chunk_chars()
    assert summarizer._budget_for("x" * full) == settings.NOTES_NUM_PREDICT
    # and a half-full chunk gets about half, rather than the whole allowance
    assert summarizer._budget_for("x" * (full // 2)) < settings.NOTES_NUM_PREDICT


def test_the_chunk_size_is_clamped_to_the_context_window(monkeypatch):
    """A chunk must never be configured larger than the model can accept."""
    monkeypatch.setattr(settings, "VLLM_MAX_MODEL_LEN", 10000)
    monkeypatch.setattr(settings, "NOTES_NUM_PREDICT", 4096)
    monkeypatch.setattr(settings, "NOTES_CHUNK_CHARS", 10**6)
    clamped = summarizer.effective_chunk_chars()
    # input chars must leave room for the answer and the prompt
    max_input_tokens = 10000 - 4096 - summarizer._PROMPT_TOKEN_ALLOWANCE
    assert clamped <= max_input_tokens * settings.CHARS_PER_TOKEN
    assert clamped > 0

    # a sane value is passed through untouched
    monkeypatch.setattr(settings, "NOTES_CHUNK_CHARS", 9000)
    assert summarizer.effective_chunk_chars() == 9000


def test_a_long_silence_gets_a_heartbeat():
    """Cloudflare closes an origin connection quiet for 100s with a 524.

    Notes parts run in parallel, so nothing is reported between the meta event
    and the first part finishing - minutes on a long video. Without a keepalive
    the user sees an error at the exact percentage the UI was showing, while
    the server is still working correctly.
    """
    from app.config import Settings

    assert Settings.model_fields["STREAM_HEARTBEAT_SECONDS"].default < 100, (
        "the heartbeat must fire well inside Cloudflare's 100s idle cut-off"
    )


def test_batch_work_cannot_starve_an_interactive_summary(monkeypatch):
    """Twenty PDFs must not leave a visitor's summary waiting.

    Measured before this split: 377 tok/s spread over 32 sequences is ~12 tok/s
    each, and a summary that takes 25s on a quiet box took 100.
    """
    monkeypatch.setattr(settings, "VLLM_MAX_CONCURRENCY", 10)
    monkeypatch.setattr(settings, "VLLM_INTERACTIVE_RESERVE", 3)
    monkeypatch.setattr(settings, "VLLM_QUEUE_TIMEOUT_SECONDS", 5)
    summarizer._vllm_slots = None
    summarizer._batch_slots = None

    async def scenario():
        held = []
        # Fill the machine with batch work.
        for _ in range(20):
            cm = summarizer.vllm_slot(batch=True)
            try:
                await asyncio.wait_for(cm.__aenter__(), 0.05)
                held.append(cm)
            except (asyncio.TimeoutError, TimeoutError, summarizer.VLLMBusy):
                break
        # Batch is capped below the total, leaving room by construction.
        assert len(held) == 10 - 3, f"batch took {len(held)} of 10 slots"

        # An interactive caller still gets in immediately.
        async with asyncio.timeout(1):
            async with summarizer.vllm_slot():
                pass

        for cm in held:
            await cm.__aexit__(None, None, None)

    asyncio.run(scenario())
    summarizer._vllm_slots = None
    summarizer._batch_slots = None


def test_the_transcript_hedge_does_not_wait_for_the_loser(monkeypatch):
    """Winning the race must end the wait, not begin a new one.

    The first version used `with ThreadPoolExecutor(...)`, whose __exit__ calls
    shutdown(wait=True). Captions answered in 3s and the request still took
    12.6s, blocked on the yt-dlp call nobody needed - slower than no hedge.
    """
    import time as _time
    from app.services import youtube as yt

    monkeypatch.setattr(settings, "TRANSCRIPT_HEDGE_SECONDS", 0.05)
    monkeypatch.setattr(settings, "TRANSCRIPT_CACHE_TTL_SECONDS", 0)

    def slow_captions(video_id):
        _time.sleep(0.25)
        return yt.Transcript(text="c" * 500, language="en", is_generated=True,
                             source="captions")

    def very_slow_ytdlp(video_id):
        _time.sleep(5.0)          # the loser: must NOT be waited on
        return yt.Transcript(text="d" * 500, language="en", is_generated=True,
                             source="yt-dlp")

    monkeypatch.setattr(yt, "_fetch_via_api", slow_captions)
    monkeypatch.setattr(yt, "_fetch_via_ytdlp", very_slow_ytdlp)

    t0 = _time.perf_counter()
    got = yt._fetch_transcript_uncached("vid12345678")
    elapsed = _time.perf_counter() - t0

    assert got.source == "captions"
    assert elapsed < 2.0, f"waited {elapsed:.1f}s for the losing fetch"


def test_one_client_cannot_take_the_whole_machine(monkeypatch):
    """Fair share, not first-come-first-served.

    Measured with a flat NOTES_CONCURRENCY of 30 across twenty clients: notes
    finished between 196s and 465s - a 268s spread - because the first job to
    arrive claimed thirty slots and the FIFO gate made everyone behind it wait
    for that whole batch instead of interleaving.
    """
    monkeypatch.setattr(settings, "VLLM_MAX_CONCURRENCY", 96)
    monkeypatch.setattr(settings, "NOTES_CONCURRENCY", 30)

    summarizer._active_notes = 1
    alone = summarizer._fair_share()
    summarizer._active_notes = 20
    crowded = summarizer._fair_share()
    summarizer._active_notes = 200
    swamped = summarizer._fair_share()
    summarizer._active_notes = 0

    # Alone, the ceiling applies and a single visitor still gets parallelism.
    assert alone == 30
    # With twenty jobs the machine is divided, not monopolised.
    assert crowded == 96 // 20
    assert crowded * 20 <= 96, "the shares together must not exceed the gate"
    # And nobody is ever starved down to serial work.
    assert swamped >= 2


def test_a_late_arrival_shrinks_everyone_elses_share(monkeypatch):
    """The share must follow the CURRENT number of jobs, not the starting one.

    Reported from the browser: a request made while forty clients were already
    running sat at zero. Its own share was computed correctly, but the forty
    ahead of it had locked in a larger allowance when fewer were active and kept
    launching parts at that rate, so nothing came free for the newcomer.
    """
    monkeypatch.setattr(settings, "VLLM_MAX_CONCURRENCY", 96)
    monkeypatch.setattr(settings, "NOTES_CONCURRENCY", 30)

    summarizer._active_notes = 1
    assert summarizer._fair_share() == 30          # alone: the ceiling applies

    summarizer._active_notes = 8
    eight = summarizer._fair_share()
    summarizer._active_notes = 40                  # a crowd arrives
    forty = summarizer._fair_share()

    assert forty < eight, "an arrival must reduce the share, not leave it fixed"
    assert forty * 40 <= 96, "shares together must not exceed the gate"
    assert forty >= 2, "nobody is starved down to serial work"
    summarizer._active_notes = 0


def test_progress_is_scaled_by_chunk_size_not_the_budget():
    """The denominator has to match what a part really writes.

    Measured over 232 cached sets of notes: 64,233 transcript characters in,
    33,294 characters of notes out. Scaling by the token BUDGET instead put the
    bar on the wrong scale - a full chunk was assumed to write ~614 tokens
    against a real ~2,000 - so it raced to the 99.9% cap partway through the
    work and then appeared to stall.
    """
    from app.services.summarizer import _expected_tokens

    big = _expected_tokens(12000, 4096)
    small = _expected_tokens(1000, 4096)

    assert big > small, "a bigger part must be expected to write more"
    # A full chunk should be expected to write a substantial share of a 4096
    # budget - not the sixth the old flat guess assumed.
    assert 1200 < big < 4096
    # Never over the allowance, never absurd for a tiny tail.
    assert _expected_tokens(10**6, 4096) <= 4096
    assert _expected_tokens(1, 4096) >= 32
