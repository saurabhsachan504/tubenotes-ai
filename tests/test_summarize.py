"""Web-app summarisation: URL parsing, language routing, trial accounting."""
from __future__ import annotations

import asyncio
import json

import pytest

from app.config import settings
from app.services import summarizer, youtube
from tests.conftest import register

API = settings.API_PREFIX

HINDI = "यह वीडियो भारत के इतिहास के बारे में है। " * 40
ENGLISH = "This video explains how photosynthesis works in plants. " * 40


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtube.com/watch?v=dQw4w9WgXcQ&t=42s", "dQw4w9WgXcQ"),
        ("https://m.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ?si=abc", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/embed/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/live/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://vimeo.com/123456", None),
        ("https://www.youtube.com/watch?v=short", None),
        ("", None),
        ("not a url", None),
    ],
)
def test_extract_video_id(url, expected):
    assert youtube.extract_video_id(url) == expected


def test_clean_transcript_strips_caption_noise():
    raw = "[Music] hello   there [संगीत] friends [Applause]"
    assert youtube.clean_transcript(raw) == "hello there friends"


def test_sample_keeps_start_middle_and_end():
    text = "".join(f"{i:05d} " for i in range(4000))  # ~24k chars
    out = youtube.sample_for_model(text, 4000)
    assert len(out) < len(text)
    assert out.startswith("00000")
    assert "…" in out
    # a chunk from the last quarter survives
    assert "03" in out.rsplit("…", 1)[-1]


# ---------------------------------------------------------------------------
# Language routing
# ---------------------------------------------------------------------------
def test_language_detection_and_model_routing():
    assert summarizer.detect_language(HINDI) == "hi"
    assert summarizer.detect_language(ENGLISH) == "en"
    # The caption track's own code wins - it separates Hindi from Marathi,
    # which share the Devanagari script.
    assert summarizer.detect_language(HINDI, hint="mr") == "mr"
    # A YouTube caption can be mislabelled. Its Bengali code must not turn
    # actual Devanagari/Hindi transcript text into a Bengali summary.
    assert summarizer.detect_language(HINDI, hint="bn") == "hi"
    assert summarizer.detect_language("", hint="ta-IN") == "ta"

    assert summarizer.model_for("en") == settings.VLLM_MODEL
    assert summarizer.model_for("hi") == settings.VLLM_MODEL
    assert summarizer.model_for("mr") == settings.VLLM_MODEL
    assert summarizer.model_for("ta") == settings.VLLM_MODEL


def test_latin_transcript_overrides_wrong_caption_label(monkeypatch):
    # Keep the test deterministic even before a developer installs langdetect
    # in a local virtual environment.
    monkeypatch.setattr(summarizer, "_statistical_language", lambda text: "en")
    assert summarizer.detect_language(ENGLISH, hint="bn") == "en"


def test_chat_language_follows_the_question_not_the_summary(monkeypatch):
    assert summarizer.detect_chat_language("\u092f\u0939 \u0915\u094d\u092f\u093e \u0939\u0948", "en") == "hi"
    # Keep Latin language guessing deterministic in a local environment.
    monkeypatch.setattr(
        summarizer,
        "detect_langs",
        lambda text: [type("Guess", (), {"lang": "fr", "prob": 0.99})()],
    )
    assert summarizer.detect_chat_language(
        "Pouvez-vous expliquer cette idee simplement", "hi"
    ) == "fr"


def test_hindi_title_prefers_hindi_track_over_wrong_asr_default():
    class Track:
        def __init__(self, code: str, generated: bool):
            self.language_code = code
            self.is_generated = generated

    bengali_asr = Track("bn", True)
    hindi_manual = Track("hi", False)

    assert youtube.title_language_hint("हिंदी समाचार और आज की बड़ी खबरें") == "hi"
    assert youtube._pick_track([bengali_asr, hindi_manual], "hi") is hindi_manual


def test_multiple_auto_caption_languages_use_source_language_resolution():
    class Track:
        def __init__(self, code: str, generated: bool = True):
            self.language_code = code
            self.is_generated = generated

    # Auto-dubbed videos return translated tracks alphabetically, where Arabic
    # can precede the original English captions.
    tracks = [Track("ar"), Track("bn"), Track("en")]
    assert youtube._needs_source_language_resolution(tracks) is True
    # A title with an unambiguous native script remains a safe preference.
    assert youtube._needs_source_language_resolution(tracks, "hi") is False


def test_language_directive_names_the_language():
    assert "Devanagari" in summarizer.language_directive("hi")
    assert "Tamil" in summarizer.language_directive("ta")
    assert "English" in summarizer.language_directive("en")


def test_strip_think_hides_reasoning_blocks():
    assert summarizer.strip_think("<think>plan</think>Answer") == "Answer"
    assert summarizer.strip_think("noise</think>Answer") == "Answer"
    # A block that is still open mid-stream must show nothing from it.
    assert summarizer.strip_think("Answer<think>still going") == "Answer"


def test_chunking_covers_everything_with_overlap():
    text = "word " * 4000  # 20k chars
    chunks = summarizer.split_into_chunks(text, 6000, 250)
    assert len(chunks) >= 3
    assert sum(len(c) for c in chunks) >= len(text.strip())


# ---------------------------------------------------------------------------
# Endpoints (transcript + model are stubbed - no network in tests)
# ---------------------------------------------------------------------------
@pytest.fixture
def stub_youtube(monkeypatch):
    async def fake_meta(video_id):
        return youtube.VideoMeta(
            video_id=video_id,
            title="Test video",
            author="Test channel",
            thumbnail=f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
            url=youtube.watch_url(video_id),
        )

    def fake_transcript(video_id):
        return youtube.Transcript(
            text=HINDI, language="hi", is_generated=True, source="captions"
        )

    monkeypatch.setattr("app.routers.summarize.youtube.fetch_metadata", fake_meta)
    monkeypatch.setattr("app.routers.summarize.youtube.fetch_transcript", fake_transcript)


@pytest.fixture
def stub_model(monkeypatch):
    async def fake_stream(**kwargs):
        for piece in ["## अवलोकन\n", "यह एक ", "परीक्षण सारांश है।"]:
            yield piece

    monkeypatch.setattr(summarizer, "stream_chat", fake_stream)


def read_events(response):
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def test_summarize_streams_and_charges_one_trial(client, device, stub_youtube, stub_model):
    _, headers, _ = register(client, device=device)

    res = client.post(
        f"{API}/summarize",
        json={"url": "https://youtu.be/dQw4w9WgXcQ", "device": device, "mode": "summary"},
        headers=headers,
    )
    assert res.status_code == 200, res.text
    events = read_events(res)

    meta = events[0]
    assert meta["type"] == "meta"
    assert meta["language"] == "hi"
    assert meta["language_name"] == "Hindi"
    assert meta["video"]["title"] == "Test video"
    assert meta["entitlement"]["trials_remaining"] == 4

    done = events[-1]
    assert done["type"] == "done"
    assert "परीक्षण सारांश" in done["text"]
    assert "".join(e["text"] for e in events if e["type"] == "delta") == done["text"]


def test_web_summary_sends_each_trial_milestone_push(
    client, device, stub_youtube, stub_model, monkeypatch
):
    """The website's streaming path must notify at 15, 10, 5 and 0 too."""
    from app.services import web_push as web_push_service

    monkeypatch.setattr(settings, "FREE_TRIAL_LIMIT", 20)
    monkeypatch.setattr(settings, "ENFORCE_MACHINE_TRIAL_LIMIT", False)
    sent = []
    monkeypatch.setattr(
        web_push_service,
        "trial_milestone",
        lambda db, user, remaining: sent.append((user.email, remaining)),
    )
    email = "web-trial-push@example.com"
    _, headers, _ = register(client, email=email, device=device)

    for number in range(20):
        video_id = f"webtrial{number:03d}"
        result = client.post(
            f"{API}/summarize",
            json={
                "url": f"https://youtu.be/{video_id}",
                "device": device,
                "mode": "summary",
            },
            headers=headers,
        )
        assert result.status_code == 200, result.text

    assert sent == [(email, 15), (email, 10), (email, 5), (email, 0)]


def test_video_chat_is_authenticated_and_does_not_consume_a_trial(client, device, monkeypatch):
    _, headers, _ = register(client, device=device)

    async def fake_answer(summary, question, history, *, lang):
        assert "photosynthesis" in summary
        assert question == "What is the main idea?"
        assert history == []
        assert lang == "en"
        return "It explains how plants make food.", "en", None

    monkeypatch.setattr(summarizer, "answer_about_summary", fake_answer)
    res = client.post(
        f"{API}/video-chat",
        json={
            "summary": "This video explains photosynthesis in plants and how they make food.",
            "question": "What is the main idea?",
            "language": "en",
            "history": [],
        },
        headers=headers,
    )
    assert res.status_code == 200, res.text
    assert res.json()["answer"] == "It explains how plants make food."

    entitlement = client.post(
        f"{API}/entitlement/check", json={"device": device}, headers=headers
    ).json()
    assert entitlement["trials_used"] == 0


def test_video_chat_replies_in_the_question_language(client, device, monkeypatch):
    _, headers, _ = register(client, device=device)

    async def fake_answer(summary, question, history, *, lang):
        assert lang == "hi"
        return "\u0939\u093f\u0902\u0926\u0940 \u091c\u0935\u093e\u092c", "hi", None

    async def fake_ensure(text, target):
        assert target == "hi"
        return text, False

    monkeypatch.setattr(summarizer, "answer_about_summary", fake_answer)
    monkeypatch.setattr("app.routers.summarize.translate.ensure_language", fake_ensure)
    res = client.post(
        f"{API}/video-chat",
        json={
            "summary": "This video explains photosynthesis in plants and how they make food.",
            "question": "\u092f\u0939 \u0915\u094d\u092f\u093e \u0939\u0948",
            "language": "en",
            "history": [],
        },
        headers=headers,
    )
    assert res.status_code == 200, res.text
    assert res.json()["language"] == "hi"


def test_video_chat_explicit_language_command_overrides_question_language(client, device, monkeypatch):
    _, headers, _ = register(client, device=device)

    async def fake_answer(summary, question, history, *, lang):
        assert question == "Please explain this in Hindi."
        assert lang == "hi"
        return "\u0939\u093f\u0902\u0926\u0940 \u092e\u0947\u0902 \u0938\u094d\u092a\u0937\u094d\u091f\u0940\u0915\u0930\u0923", "hi", None

    async def fake_ensure(text, target):
        assert target == "hi"
        return text, False

    monkeypatch.setattr(summarizer, "answer_about_summary", fake_answer)
    monkeypatch.setattr("app.routers.summarize.translate.ensure_language", fake_ensure)
    res = client.post(
        f"{API}/video-chat",
        json={
            "summary": "This video explains photosynthesis in plants and how they make food.",
            "question": "Please explain this in Hindi.",
            "language": "en",
            "reply_language": "hi",
            "history": [],
        },
        headers=headers,
    )
    assert res.status_code == 200, res.text
    assert res.json()["language"] == "hi"


def test_chat_command_translates_the_full_summary_without_a_trial(client, device, monkeypatch):
    _, headers, _ = register(client, device=device)

    async def fake_translate(text, target):
        assert text.startswith("This video explains")
        assert target == "fr"
        return "Cette vidéo explique la photosynthèse."

    monkeypatch.setattr("app.routers.summarize.translate.translate", fake_translate)
    res = client.post(
        f"{API}/video-chat/translate-summary",
        json={
            "summary": "This video explains photosynthesis in plants and how they make food.",
            "target_lang": "fr",
        },
        headers=headers,
    )
    assert res.status_code == 200, res.text
    assert res.json() == {
        "text": "Cette vidéo explique la photosynthèse.",
        "target_lang": "fr",
        "language_name": "French",
    }
    entitlement = client.post(
        f"{API}/entitlement/check", json={"device": device}, headers=headers
    ).json()
    assert entitlement["trials_used"] == 0


def test_video_chat_uses_a_low_temperature_model_call(monkeypatch):
    seen = {}

    async def fake_stream(**kwargs):
        seen.update(kwargs)
        yield "The answer is in the summary."

    monkeypatch.setattr(summarizer, "stream_chat", fake_stream)
    answer, write_lang, translate_to = asyncio.run(
        summarizer.answer_about_summary(
            "A video summary about solar energy.", "What is it about?", [], lang="en"
        )
    )
    assert answer == "The answer is in the summary."
    assert (write_lang, translate_to) == ("en", None)
    assert seen["temperature"] == 0.35


def test_same_video_twice_is_charged_once(client, device, stub_youtube, stub_model):
    _, headers, _ = register(client, device=device)
    body = {"url": "https://youtu.be/dQw4w9WgXcQ", "device": device, "mode": "summary"}

    client.post(f"{API}/summarize", json=body, headers=headers)
    # Different mode, same video -> still one charge.
    res = client.post(
        f"{API}/summarize", json={**body, "mode": "key_points"}, headers=headers
    )
    meta = read_events(res)[0]
    assert meta["entitlement"]["trials_used"] == 1
    assert meta["entitlement"]["trials_remaining"] == 4


def test_pdf_notes_do_not_charge_again_for_the_same_video(
    client, device, stub_youtube, stub_model
):
    _, headers, _ = register(client, device=device)
    client.post(
        f"{API}/summarize",
        json={"url": "https://youtu.be/dQw4w9WgXcQ", "device": device, "mode": "summary"},
        headers=headers,
    )
    res = client.post(
        f"{API}/notes",
        json={"url": "https://youtu.be/dQw4w9WgXcQ", "device": device},
        headers=headers,
    )
    assert res.status_code == 200
    # A streaming endpoint returns 200 even when the body carries an error, so
    # the status alone proves nothing - check that real notes actually came out.
    events = read_events(res)
    assert not [e for e in events if e["type"] == "error"], events
    assert events[-1]["type"] == "done"
    assert events[-1]["text"].strip()

    ent = client.post(
        f"{API}/entitlement/check", json={"device": device}, headers=headers
    ).json()
    assert ent["trials_used"] == 1


def test_five_videos_then_402(client, device, stub_youtube, stub_model):
    _, headers, _ = register(client, device=device)
    ids = ["dQw4w9WgXcQ", "jNQXAC9IVRw", "9bZkp7q19f0", "kJQP7kiw5Fk", "fJ9rUzIMcZQ"]
    for vid in ids:
        res = client.post(
            f"{API}/summarize",
            json={"url": f"https://youtu.be/{vid}", "device": device},
            headers=headers,
        )
        assert res.status_code == 200

    res = client.post(
        f"{API}/summarize",
        json={"url": "https://youtu.be/L_jWHffIx5E", "device": device},
        headers=headers,
    )
    assert res.status_code == 402
    assert "$5/month" in res.json()["detail"]["message"]


def test_summarize_requires_auth(client, device):
    res = client.post(
        f"{API}/summarize", json={"url": "https://youtu.be/dQw4w9WgXcQ", "device": device}
    )
    assert res.status_code == 401


def test_bad_url_is_rejected_before_charging(client, device):
    _, headers, _ = register(client, device=device)
    res = client.post(
        f"{API}/summarize",
        json={"url": "https://vimeo.com/123", "device": device},
        headers=headers,
    )
    assert res.status_code == 400
    ent = client.post(
        f"{API}/entitlement/check", json={"device": device}, headers=headers
    ).json()
    assert ent["trials_used"] == 0


def test_missing_transcript_is_reported_clearly(client, device, monkeypatch, stub_youtube):
    _, headers, _ = register(client, device=device)

    def boom(video_id):
        raise youtube.TranscriptUnavailable("no captions on this video")

    monkeypatch.setattr("app.routers.summarize.youtube.fetch_transcript", boom)
    res = client.post(
        f"{API}/summarize",
        json={"url": "https://youtu.be/dQw4w9WgXcQ", "device": device},
        headers=headers,
    )
    assert res.status_code == 422
    assert "no captions" in res.json()["detail"]


def test_model_failure_becomes_a_friendly_error_event(
    client, device, stub_youtube, monkeypatch
):
    _, headers, _ = register(client, device=device)

    async def broken(**kwargs):
        raise RuntimeError("vLLM HTTP 404: model not found")
        yield  # pragma: no cover

    monkeypatch.setattr(summarizer, "stream_chat", broken)
    res = client.post(
        f"{API}/summarize",
        json={"url": "https://youtu.be/dQw4w9WgXcQ", "device": device},
        headers=headers,
    )
    events = read_events(res)
    assert events[-1]["type"] == "error"
    assert "was not found" in events[-1]["message"]


def test_video_info_is_free(client, device, stub_youtube):
    _, headers, _ = register(client, device=device)
    res = client.post(
        f"{API}/video/info",
        json={"url": "https://youtu.be/dQw4w9WgXcQ", "device": device},
        headers=headers,
    )
    assert res.status_code == 200
    assert res.json()["title"] == "Test video"

    ent = client.post(
        f"{API}/entitlement/check", json={"device": device}, headers=headers
    ).json()
    assert ent["trials_used"] == 0


def test_home_page_is_served(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "YouTube Summarizer" in res.text
    assert res.headers["content-type"].startswith("text/html")


# ---------------------------------------------------------------------------
# Language: the bug where a Hindi video came back in English
# ---------------------------------------------------------------------------
from app.services import translate as tr  # noqa: E402


def test_prompt_labels_are_localised_not_english():
    """The English template was the bug: the model copied '## Overview' and then
    kept writing English. Labels must arrive already translated."""
    hi = summarizer.summary_prompt("hi")
    # The template asks for real, descriptive section titles, so only the two
    # fixed headings are injected. Both must already be in Hindi.
    assert summarizer.LABELS["hi"]["overview"] in hi
    assert summarizer.LABELS["hi"]["conclusion"] in hi
    assert "## Overview" not in hi
    assert "**Key Point:**" not in hi
    assert "Conclusion & Key Takeaways" not in hi

    ta = summarizer.summary_prompt("ta")
    assert "மேலோட்டம்" in ta and "## Overview" not in ta

    # A language we have no label table for still gets an explicit instruction.
    sw = summarizer.summary_prompt("sw")
    assert "Swahili" in sw


def test_language_rule_is_first_and_last_in_the_system_prompt(monkeypatch):
    captured = {}

    async def fake_stream(*, model, system, content, num_predict=3000, **kw):
        captured["system"] = system
        captured["model"] = model
        yield "ok"

    monkeypatch.setattr(summarizer, "stream_chat", fake_stream)

    import asyncio

    async def run():
        async for _ in summarizer.stream_summary(HINDI, lang="hi", mode="summary"):
            pass

    asyncio.run(run())
    assert captured["system"].startswith("ABSOLUTE LANGUAGE RULE")
    assert "FINAL REMINDER" in captured["system"]
    assert "Hindi" in captured["system"]


@pytest.mark.parametrize(
    "target,model,write_lang,translate_to",
    [
        ("hi", settings.VLLM_MODEL, "hi", None),
        ("en", settings.VLLM_MODEL, "en", None),
        ("ta", settings.VLLM_MODEL, "ta", None),
        ("mr", settings.VLLM_MODEL, "mr", None),
        # A language neither model writes well: produce English, then translate.
        ("sw", settings.VLLM_MODEL, "en", "sw"),
        ("si", settings.VLLM_MODEL, "en", "si"),
    ],
)
def test_plan_for_language(target, model, write_lang, translate_to):
    assert summarizer.plan_for(target) == (model, write_lang, translate_to)


def test_script_check_catches_wrong_language_output():
    english = "This video explains the history of the Indian freedom struggle in detail."
    hindi = "यह वीडियो भारत के स्वतंत्रता संग्राम के इतिहास को विस्तार से समझाता है।"

    assert tr.script_matches(hindi, "hi")
    assert not tr.script_matches(english, "hi")      # the reported bug
    assert tr.script_matches(english, "en")
    assert tr.script_matches("இந்த வீடியோ விரிவாக விளக்குகிறது என்பதை நன்கு காட்டுகிறது", "ta")
    assert not tr.script_matches(english, "ta")
    # Too short to judge -> never rejected.
    assert tr.script_matches("ok", "hi")


def test_wrong_language_output_is_auto_translated(client, device, stub_youtube, monkeypatch):
    """End to end: model answers in English for a Hindi video -> we fix it."""
    _, headers, _ = register(client, device=device)

    async def english_stream(**kwargs):
        yield "## Overview\nThis video explains the concept of Manonash in Indian philosophy."

    async def fake_translate(text, target):
        assert target == "hi"
        return "## अवलोकन\nयह वीडियो भारतीय दर्शन में मनोनाश की अवधारणा समझाता है।"

    monkeypatch.setattr(summarizer, "stream_chat", english_stream)
    monkeypatch.setattr(tr, "translate", fake_translate)

    res = client.post(
        f"{API}/summarize",
        json={"url": "https://youtu.be/dQw4w9WgXcQ", "device": device},
        headers=headers,
    )
    events = read_events(res)
    assert any(e["type"] == "status" and "wrong language" in e["message"] for e in events)
    assert "अवलोकन" in events[-1]["text"]
    assert events[-1]["language"] == "hi"


def test_target_lang_overrides_the_video_language(client, device, stub_youtube, stub_model):
    _, headers, _ = register(client, device=device)
    res = client.post(
        f"{API}/summarize",
        json={"url": "https://youtu.be/dQw4w9WgXcQ", "device": device, "target_lang": "ta"},
        headers=headers,
    )
    meta = read_events(res)[0]
    assert meta["detected_language"] == "hi"
    assert meta["language"] == "ta"
    assert meta["model"] == settings.VLLM_MODEL


def test_translate_endpoint_does_not_charge_a_trial(client, device, monkeypatch):
    _, headers, _ = register(client, device=device)

    async def fake_translate(text, target):
        return "अनुवादित पाठ"

    monkeypatch.setattr(tr, "translate", fake_translate)
    res = client.post(
        f"{API}/translate",
        json={"text": "Some English summary text here.", "target_lang": "hi"},
        headers=headers,
    )
    assert res.status_code == 200
    assert res.json()["text"] == "अनुवादित पाठ"
    assert res.json()["language_name"] == "Hindi"

    ent = client.post(
        f"{API}/entitlement/check", json={"device": device}, headers=headers
    ).json()
    assert ent["trials_used"] == 0


def test_translate_chunking_preserves_line_structure():
    md = "\n".join(f"- point number {i} with some words" for i in range(200))
    chunks = tr._chunk(md, size=500)
    assert len(chunks) > 1
    assert "\n".join(chunks) == md          # nothing lost, nothing reordered
    assert all(len(c) <= 560 for c in chunks)



def test_full_notes_actually_produce_text(client, device, stub_youtube, monkeypatch):
    """Guards the bug where the notes prompts were missing and every chunk died."""
    calls = []

    async def fake_stream(*, model, system, content, num_predict=3000, **kw):
        calls.append(system)
        yield "# विषय\n\n## भाग एक\nविस्तृत नोट्स यहाँ हैं।"

    monkeypatch.setattr(summarizer, "stream_chat", fake_stream)
    _, headers, _ = register(client, device=device)

    res = client.post(
        f"{API}/notes",
        json={"url": "https://youtu.be/dQw4w9WgXcQ", "device": device},
        headers=headers,
    )
    events = read_events(res)
    assert not [e for e in events if e["type"] == "error"], events
    assert "विस्तृत नोट्स" in events[-1]["text"]
    # The language rule must wrap the notes prompt, exactly like the summary.
    assert calls and calls[0].startswith("ABSOLUTE LANGUAGE RULE")
    assert "exhaustive" in calls[0]


def test_notes_prompts_exist_and_carry_no_stray_english_labels():
    for prompt in (summarizer.NOTES_FIRST_PROMPT, summarizer.NOTES_SEGMENT_PROMPT):
        assert "## Heading" in prompt          # shape, not literal text
        assert "## Overview" not in prompt     # no copyable English heading


def test_notes_respects_target_lang(client, device, stub_youtube, monkeypatch):
    async def fake_stream(**kwargs):
        yield "# தலைப்பு\n\n## பகுதி ஒன்று\nவிரிவான குறிப்புகள்."

    monkeypatch.setattr(summarizer, "stream_chat", fake_stream)
    _, headers, _ = register(client, device=device)

    res = client.post(
        f"{API}/notes",
        json={"url": "https://youtu.be/dQw4w9WgXcQ", "device": device, "target_lang": "ta"},
        headers=headers,
    )
    events = read_events(res)
    assert events[0]["language"] == "ta"
    assert events[0]["detected_language"] == "hi"
    assert events[-1]["type"] == "done"



# ---------------------------------------------------------------------------
# Full notes must cover the WHOLE video - nothing silently dropped
# ---------------------------------------------------------------------------
def test_notes_read_the_entire_transcript_not_a_sample(monkeypatch):
    """A 3-hour transcript must be fully covered, in order, with overlap."""
    import asyncio

    seen: list[str] = []

    async def fake_chat(*, model, system, content, num_predict=3000, queue_wait=None, batch=False, on_token=None):
        seen.append(content)
        return f"## part {len(seen)}\nnotes"

    monkeypatch.setattr(summarizer, "collect_chat", fake_chat)

    # ~200k chars - far more than any sampling window would pass through.
    transcript = " ".join(f"sentence{i}." for i in range(20000))
    out = asyncio.run(summarizer.full_notes(transcript, lang="en"))

    joined = "".join(seen)
    # Every sentence of the source reached the model.
    for probe in ("sentence0.", "sentence9999.", "sentence19999."):
        assert probe in joined, probe
    # The chunk COUNT depends on NOTES_CHUNK_CHARS, so asserting a fixed number
    # only encodes today's tuning. What must hold is that the whole transcript
    # was covered - i.e. roughly length/chunk_size chunks, not a fixed sample.
    expected = len(transcript) / summarizer.effective_chunk_chars()
    assert len(seen) >= expected * 0.9, f"{len(seen)} chunks for {expected:.0f} expected"
    assert len(joined) > len(transcript)
    assert out.count("## part") == len(seen)


def test_notes_chunk_cap_is_off_by_default_and_warns_when_set(monkeypatch):
    import asyncio

    async def fake_chat(**kwargs):
        return "notes"

    monkeypatch.setattr(summarizer, "collect_chat", fake_chat)
    assert settings.NOTES_MAX_CHUNKS == 0, "a silent cap would lose content"

    warnings: list[str] = []

    async def on_warning(msg):
        warnings.append(msg)

    monkeypatch.setattr(settings, "NOTES_MAX_CHUNKS", 3)
    transcript = "word " * 20000
    asyncio.run(summarizer.full_notes(transcript, lang="en", on_warning=on_warning))
    assert warnings and "left out" in warnings[0]
    assert "NOTES_MAX_CHUNKS" in warnings[0]


def test_a_failing_chunk_is_reported_not_silently_dropped(monkeypatch):
    import asyncio

    calls = {"n": 0}

    async def flaky(*, model, system, content, num_predict=3000, queue_wait=None, batch=False, on_token=None):
        calls["n"] += 1
        if "BOOM" in content:
            raise RuntimeError("model exploded")
        return "## ok\nnotes"

    monkeypatch.setattr(summarizer, "collect_chat", flaky)

    warnings: list[str] = []

    async def on_warning(msg):
        warnings.append(msg)

    # Small chunks so ONE section fails out of many. That is this test's
    # subject: a document with a hole in it. A document that is mostly holes is
    # a different case and full_notes() now raises for it - see
    # test_a_mostly_empty_document_is_not_returned_as_notes.
    monkeypatch.setattr(settings, "NOTES_CHUNK_CHARS", 500)
    monkeypatch.setattr(settings, "NOTES_CHUNK_OVERLAP", 0)
    transcript = ("good " * 900) + ("BOOM " * 60) + ("good " * 900)
    out = asyncio.run(
        summarizer.full_notes(transcript, lang="en", on_warning=on_warning)
    )
    assert out                       # the healthy parts still come through
    assert "## ok" in out
    assert warnings, "a lost section must be reported"
    assert "could not be written" in warnings[0]


def test_notes_keep_chunk_order_even_when_run_concurrently(monkeypatch):
    import asyncio

    monkeypatch.setattr(settings, "NOTES_CONCURRENCY", 4)

    async def slow_for_early_chunks(*, model, system, content, num_predict=3000, queue_wait=None, batch=False, on_token=None):
        # Make the first chunk the slowest: if ordering were by completion
        # time, the notes would come out shuffled.
        marker = content.strip().split()[0]
        await asyncio.sleep(0.05 if marker == "aaa" else 0.0)
        return f"## {marker}"

    monkeypatch.setattr(summarizer, "collect_chat", slow_for_early_chunks)

    transcript = ("aaa " * 900) + ("bbb " * 900) + ("ccc " * 900)
    out = asyncio.run(summarizer.full_notes(transcript, lang="en"))
    headings = [line for line in out.splitlines() if line.startswith("## ")]
    assert headings == sorted(headings), headings


def test_notes_progress_counts_every_chunk(monkeypatch):
    import asyncio

    async def fake_chat(**kwargs):
        return "notes"

    monkeypatch.setattr(summarizer, "collect_chat", fake_chat)
    seen: list[tuple[int, int, int]] = []

    async def on_progress(done, total, started=0, fraction=None):
        seen.append((done, total, started))

    transcript = "word " * 6000
    asyncio.run(
        summarizer.full_notes(transcript, lang="en", on_progress=on_progress)
    )
    total = seen[0][1]

    # Progress is now reported when a part STARTS as well as when it finishes,
    # so a given "done" count shows up more than once. What must hold is that
    # every count from 0 to total is reported, and that it ends at total.
    assert sorted({d for d, _t, _s in seen}) == list(range(0, total + 1))
    assert max(d for d, _t, _s in seen) == total, "the last part was never reported"
    assert max(s for _d, _t, s in seen) == total, "not every part reported starting"
    # done must never run ahead of started, or the bar would exceed 100%
    assert all(d <= s for d, _t, s in seen if s), "more parts done than started"

    # And the caller hears about the job BEFORE any part finishes. Parts run in
    # parallel, so without this the UI froze on one number for minutes.
    assert seen[0][0] == 0, "the shape of the job must be reported up front"
    assert seen[0][1] == total
    assert any(s > 0 and d == 0 for d, _t, s in seen), "chunk starts unreported"
