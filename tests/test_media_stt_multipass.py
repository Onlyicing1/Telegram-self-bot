"""
Media Processing — the bounded MULTI-PASS transcription seam of the explicit
Voice/Audio path (``INVESTIGATION.md`` §19's recognition-quality class).

This suite pins the ENGINE side of the accuracy seam and nothing else. The
reconciliation RULE itself is pinned by ``tests/test_stt_consensus.py``; here the
questions are all about the operation around it:

  * the seam is OFF unless ``AI_GEMINI_STT_PASSES`` asks for it, and with it unset
    the single-pass route is byte-identical to M1.7c (one request, no consensus);
  * a multi-pass operation is bounded by ONE deadline and by the configured pass
    count, uploads the audio ONCE, keeps ONE request in flight, and issues no more
    requests than the configured number of passes;
  * a pass is a RECOGNITION result, never a transport retry: a failed pass
    contributes no hypothesis (so a 503 can never be read as a transcript), a
    transient failure consumes a pass instead of multiplying attempts, and a
    deterministic failure stops the loop instead of re-sending the same request;
  * the reconciler is handed the hypotheses and NOTHING else — no chat id, no
    sender, no filename, no caption, no reply and no history can reach it, and
    none of them is sent to the model either;
  * OCR does not run the consensus, an empty payload sends nothing, a refused
    container is refused before any request, and the engine still retains no HTTP
    or event-loop state between operations.

The HTTP boundary is the same scripted ``httpx`` transport the reliability suite
uses: no credential, no network, and — like every media suite here — nothing below
proves anything about recognition QUALITY. Whether repeated Persian passes differ
at all, and whether consensus helps, is a LIVE measurement
(``backend/tools/stt_benchmark.py``), never a claim of this file.
"""
from __future__ import annotations

import base64
import json
import time
from typing import Any

import httpx
import pytest

from backend.services import gemini_media_engine as engine_module
from backend.services import media_service
from backend.services.gemini_media_engine import (
    DEFAULT_MEDIA_MODEL,
    DEDICATED_TRANSCRIPTION_MODEL,
    STT_MAX_PASSES,
    STT_PASSES_ENV_VAR,
    GeminiMediaEngine,
    resolve_stt_passes,
)
from backend.services.media_service import MediaError
from tests.test_media_stt_reliability import (
    _FILE_NAME,
    _FILE_URI,
    _Script,
    _interaction,
    _large_wav,
    _ogg_opus,
    _wav,
    script,
)

API_KEY = "multipass-suite-key-not-a-credential"
CAPTION = "multipass-caption-must-never-reach-the-model-7c31"
FILE_LABEL = "voice-filename-must-never-reach-the-model.ogg"

#: The owner's §19.1 speech, three readings of the SAME audio.
SPEECH = "دیدم اتفاقا تو گپ چیز باحالیه خلاصه چت"
READING_MAJORITY = SPEECH
READING_MINORITY = "دیه اتفاقا تو کپ چیز باحالیه حالا سید چت"


def _engine(
    passes: int | None = 1,
    *,
    stt_model: str = "",
    stt_language: str = "",
    model: str = DEFAULT_MEDIA_MODEL,
) -> GeminiMediaEngine:
    return GeminiMediaEngine(
        API_KEY, model, key_env_var="AI_GEMINI_API_KEY",
        stt_model=stt_model, stt_language=stt_language,
        **({} if passes is None else {"stt_passes": passes}),
    )


def _dedicated(passes: int = 1, language: str = "") -> GeminiMediaEngine:
    return _engine(passes, stt_model=DEDICATED_TRANSCRIPTION_MODEL, stt_language=language)


def _general(passes: int = 1) -> GeminiMediaEngine:
    return _engine(passes)


def _generate_body(text: str) -> dict[str, Any]:
    return {
        "candidates": [
            {"content": {"parts": [{"text": text}]}, "finishReason": "STOP"},
        ]
    }


def _bodies(transport: _Script, leg: str) -> list[dict[str, Any]]:
    return [json.loads(r.content.decode("utf-8")) for r in transport.leg_requests(leg)]


def _items(transport: _Script) -> list[dict[str, Any]]:
    return [body["input"][0] for body in _bodies(transport, "interaction")]


def _consensus_line(caplog) -> str:
    lines = [
        r.getMessage() for r in caplog.records
        if r.getMessage().startswith("GEMINI_MEDIA_ENGINE_CONSENSUS")
    ]
    assert len(lines) == 1, "one consensus line per multi-pass operation"
    return lines[0]


@pytest.fixture(autouse=True)
def _reset_engines_and_env(monkeypatch):
    media_service.set_ocr_engine(None)
    media_service.set_stt_engine(None)
    for name in (
        "AI_GEMINI_API_KEY", "GEMINI_API_KEY", "AI_GEMINI_MEDIA_MODEL", "AI_GEMINI_MODEL",
        "AI_GEMINI_STT_MODEL", "AI_GEMINI_STT_LANGUAGE", STT_PASSES_ENV_VAR,
    ):
        monkeypatch.delenv(name, raising=False)
    yield
    media_service.set_ocr_engine(None)
    media_service.set_stt_engine(None)


# ── 1. The seam is opt-in, and its default is the existing single pass ──


def test_the_default_is_exactly_one_pass():
    assert _dedicated().stt_passes == 1
    assert _engine().stt_passes == 1


@pytest.mark.asyncio
async def test_an_unset_pass_count_keeps_the_single_request_route(script, caplog):
    transport = script()

    with caplog.at_level("INFO", logger="backend.services.gemini_media_engine"):
        _dedicated().transcribe(_ogg_opus())

    assert len(transport.leg_requests("interaction")) == 1
    assert not any(
        r.getMessage().startswith("GEMINI_MEDIA_ENGINE_CONSENSUS") for r in caplog.records
    ), "no consensus runs unless it is configured"


@pytest.mark.parametrize(
    "raw,expected",
    [("", 1), ("1", 1), ("2", 2), ("3", 3), (" 3 ", 3), ("4", STT_MAX_PASSES),
     ("0", 1), ("-2", 1), ("lots", 1), ("3.5", 1)],
)
def test_the_env_value_is_read_clamped_and_never_trusted(monkeypatch, raw, expected):
    monkeypatch.setenv(STT_PASSES_ENV_VAR, raw)

    passes, name = resolve_stt_passes()

    assert passes == expected
    assert name == (STT_PASSES_ENV_VAR if raw.strip() else "")
    assert 1 <= passes <= STT_MAX_PASSES


def test_a_non_integer_env_value_warns_without_echoing_the_value(monkeypatch, caplog):
    monkeypatch.setenv(STT_PASSES_ENV_VAR, "hunter2-looking-value")

    with caplog.at_level("WARNING", logger="backend.services.gemini_media_engine"):
        passes, _ = resolve_stt_passes()

    assert passes == 1
    warnings = [r.getMessage() for r in caplog.records if "STT_PASSES_INVALID" in r.getMessage()]
    assert warnings and all("hunter2" not in line for line in warnings)


def test_an_explicit_pass_count_beats_the_environment(monkeypatch):
    monkeypatch.setenv(STT_PASSES_ENV_VAR, "3")

    assert _dedicated(1).stt_passes == 1
    assert _dedicated(3).stt_passes == 3


def test_every_configured_count_is_clamped_to_the_ceiling():
    assert _dedicated(99).stt_passes == STT_MAX_PASSES
    assert _dedicated(0).stt_passes == 1
    assert _dedicated(-5).stt_passes == 1


def test_the_env_default_is_used_when_the_caller_states_nothing(monkeypatch):
    monkeypatch.setenv(STT_PASSES_ENV_VAR, "2")

    assert _engine(passes=None).stt_passes == 2


# ── 2. One upload, N model calls, ONE deadline ──


@pytest.mark.asyncio
async def test_three_passes_upload_once_and_ask_the_model_three_times(script):
    transport = script(
        interaction=[
            _interaction(READING_MINORITY),
            _interaction(READING_MAJORITY),
            _interaction(READING_MAJORITY),
        ]
    )

    text = _dedicated(3).transcribe(_ogg_opus())

    assert text == READING_MAJORITY
    assert len(transport.leg_requests("upload_start")) == 1, "the audio is uploaded ONCE"
    assert len(transport.leg_requests("upload_finalize")) == 1
    items = _items(transport)
    assert len(items) == 3
    assert {item["uri"] for item in items} == {_FILE_URI}, "every pass reuses that one file"
    assert all("data" not in item for item in items)
    assert transport.deleted == [f"{'https://generativelanguage.googleapis.com/v1beta'}/{_FILE_NAME}"]


@pytest.mark.asyncio
async def test_the_file_is_deleted_once_after_the_last_pass(script):
    transport = script(interaction=[_interaction(SPEECH)] * 3)

    _dedicated(3).transcribe(_ogg_opus())

    assert transport.legs.count("delete") == 1
    assert transport.legs.index("delete") > max(
        index for index, leg in enumerate(transport.legs) if leg == "interaction"
    )


@pytest.mark.asyncio
async def test_the_reconciled_transcript_is_the_majority_reading(script):
    transport = script(
        interaction=[
            _interaction(READING_MINORITY),
            _interaction(READING_MAJORITY),
            _interaction(READING_MAJORITY),
        ]
    )

    assert _dedicated(3).transcribe(_ogg_opus()) == READING_MAJORITY


@pytest.mark.asyncio
async def test_a_minority_reading_first_cannot_set_the_word_grid(script):
    """The first pass is the mangled one: the median grid keeps the majority words."""
    transport = script(
        interaction=[
            _interaction(READING_MINORITY),
            _interaction(READING_MAJORITY),
            _interaction(READING_MAJORITY),
        ]
    )

    text = _dedicated(3).transcribe(_ogg_opus())

    assert "خلاصه" in text, "a word two passes heard is never lost to the alignment"
    assert text == READING_MAJORITY


@pytest.mark.asyncio
async def test_two_passes_return_the_first_pass_byte_exact(script):
    transport = script(
        interaction=[_interaction(SPEECH), _interaction(READING_MINORITY)],
    )

    text = _dedicated(2).transcribe(_ogg_opus())

    assert text == SPEECH
    assert len(transport.leg_requests("interaction")) == 2


@pytest.mark.asyncio
async def test_every_request_timeout_is_still_derived_from_the_one_deadline(script):
    transport = script(interaction=[_interaction(SPEECH)] * 3)

    _dedicated(3).transcribe(_ogg_opus())

    reads = [t.read for t in transport.timeouts]
    assert len(reads) == len(transport.requests), "one client per leg"
    assert reads[0] <= engine_module.STT_OPERATION_DEADLINE_S
    for earlier, later in zip(reads, reads[1:]):
        assert later <= earlier, "each leg is bounded by what is left of the deadline"
    assert all(t.connect <= engine_module.STT_CONNECT_TIMEOUT_S for t in transport.timeouts)
    assert all(t.write <= engine_module.STT_WRITE_TIMEOUT_S for t in transport.timeouts)


@pytest.mark.asyncio
async def test_a_spent_deadline_sends_nothing_even_with_three_passes(script, monkeypatch):
    transport = script()
    monkeypatch.setattr(engine_module, "STT_OPERATION_DEADLINE_S", 0.0)

    with pytest.raises(MediaError) as exc:
        _dedicated(3).transcribe(_ogg_opus())

    assert exc.value.failure_class == engine_module.FAILURE_DEADLINE
    assert transport.requests == []


@pytest.mark.asyncio
async def test_no_pass_starts_without_a_meaningful_budget_left(script, monkeypatch):
    transport = script(interaction=[_interaction(SPEECH)] * 3)
    monkeypatch.setattr(engine_module, "STT_MIN_ATTEMPT_S", 10 ** 6)

    text = _dedicated(3).transcribe(_ogg_opus())

    assert text == SPEECH
    assert len(transport.leg_requests("interaction")) == 1, "a pass needs budget to start"


@pytest.mark.asyncio
async def test_the_calls_of_a_multi_pass_operation_are_strictly_sequential(script):
    transport = script(interaction=[_interaction(SPEECH)] * 3)
    order: list[str] = []
    real_handle = transport.handle_request

    def handle(request: httpx.Request) -> httpx.Response:
        order.append(f"start:{transport._leg(request)}")
        response = real_handle(request)
        order.append(f"end:{transport._leg(request)}")
        return response

    transport.handle_request = handle            # type: ignore[method-assign]

    _dedicated(3).transcribe(_ogg_opus())

    assert order == [
        "start:upload_start", "end:upload_start",
        "start:upload_finalize", "end:upload_finalize",
        "start:interaction", "end:interaction",
        "start:interaction", "end:interaction",
        "start:interaction", "end:interaction",
        "start:delete", "end:delete",
    ]


# ── 3. A pass is a recognition result, never a transport retry ──


@pytest.mark.asyncio
async def test_a_transient_pass_failure_is_not_counted_as_a_hypothesis(script, caplog):
    """Three passes, one 503: two hypotheses, three requests — no retry multiplication."""
    transport = script(
        interaction=[503, _interaction(SPEECH), _interaction(SPEECH)],
    )

    with caplog.at_level("INFO", logger="backend.services.gemini_media_engine"):
        text = _dedicated(3).transcribe(_ogg_opus())

    assert text == SPEECH
    assert len(transport.leg_requests("interaction")) == 3, "bounded by the pass count"
    line = _consensus_line(caplog)
    assert "passes=3" in line and "hypotheses=2" in line


@pytest.mark.asyncio
async def test_persistent_transient_failures_never_exceed_the_configured_passes(script):
    transport = script(interaction=503)

    with pytest.raises(MediaError) as exc:
        _dedicated(3).transcribe(_ogg_opus())

    assert len(transport.leg_requests("interaction")) == 3, "no per-pass retry exists here"
    assert exc.value.http_status == 503
    assert exc.value.failure_class == engine_module.FAILURE_HTTP


@pytest.mark.asyncio
async def test_a_deterministic_failure_stops_the_loop_at_once(script):
    transport = script(interaction=400)

    with pytest.raises(MediaError) as exc:
        _dedicated(3).transcribe(_ogg_opus())

    assert len(transport.leg_requests("interaction")) == 1, "the same request is never re-sent"
    assert exc.value.http_status == 400
    assert transport.deleted == [f"{'https://generativelanguage.googleapis.com/v1beta'}/{_FILE_NAME}"]


@pytest.mark.asyncio
async def test_a_good_pass_followed_by_a_deterministic_failure_keeps_the_transcript(
    script,
):
    transport = script(interaction=[_interaction(SPEECH), 400])

    text = _dedicated(3).transcribe(_ogg_opus())

    assert text == SPEECH
    assert len(transport.leg_requests("interaction")) == 2, "the third pass never runs"


@pytest.mark.asyncio
async def test_the_reported_failure_is_the_first_one_recorded(script):
    transport = script(interaction=[503, 401])

    with pytest.raises(MediaError) as exc:
        _dedicated(3).transcribe(_ogg_opus())

    assert exc.value.http_status == 503, "the first failure explains the operation"


# ── 4. The upload fallback and its two outcomes ──


@pytest.mark.asyncio
async def test_a_transient_upload_failure_falls_back_to_the_inline_form_once(script):
    transport = script(upload_start=429, interaction=[_interaction(SPEECH)] * 3)

    text = _dedicated(3).transcribe(_ogg_opus())

    assert text == SPEECH
    items = _items(transport)
    assert len(items) == 3
    assert all("data" in item and "uri" not in item for item in items)
    assert base64.b64decode(items[0]["data"]) == _ogg_opus(), "the same bytes, unchanged"
    assert transport.leg_requests("upload_finalize") == []
    assert transport.deleted == [], "no file existed to delete"


@pytest.mark.asyncio
async def test_a_deterministic_upload_failure_fails_closed_without_a_request(script):
    transport = script(upload_start=401)

    with pytest.raises(MediaError) as exc:
        _dedicated(3).transcribe(_ogg_opus())

    assert exc.value.http_status == 401
    assert transport.leg_requests("interaction") == []
    assert transport.deleted == []


# ── 5. Both routes, and the routes that stay untouched ──


@pytest.mark.asyncio
async def test_the_dedicated_route_never_touches_the_general_surface(script):
    transport = script(interaction=[_interaction(SPEECH)] * 3)

    _dedicated(3).transcribe(_ogg_opus())

    assert transport.leg_requests("generate") == [], (
        "the route is chosen before the operation and never falls back mid-flight"
    )


@pytest.mark.asyncio
async def test_the_general_route_runs_the_consensus_too(script):
    transport = script(
        generate=[_generate_body(READING_MINORITY), _generate_body(SPEECH),
                  _generate_body(SPEECH)],
    )

    text = _general(3).transcribe(_wav())

    assert text == SPEECH
    assert len(transport.leg_requests("generate")) == 3
    assert len(transport.leg_requests("upload_start")) == 0, "a small payload is inline"
    assert transport.deleted == [], "nothing was uploaded, so nothing is deleted"
    bodies = _bodies(transport, "generate")
    assert "temperature" in bodies[0]["generationConfig"]
    parts = [body["contents"][0]["parts"] for body in bodies]
    assert all(part[0]["text"] == engine_module.stt_instruction("") for part in parts)
    assert all("inlineData" in part[1] for part in parts)


@pytest.mark.asyncio
async def test_the_general_route_uploads_once_for_a_large_payload(script):
    transport = script(generate=[_generate_body(SPEECH)] * 3)

    text = _general(3).transcribe(_large_wav())

    assert text == SPEECH
    assert len(transport.leg_requests("upload_start")) == 1
    assert len(transport.leg_requests("generate")) == 3
    parts = [body["contents"][0]["parts"][1] for body in _bodies(transport, "generate")]
    assert all(part["fileData"]["fileUri"] == _FILE_URI for part in parts)
    assert transport.legs.count("delete") == 1


@pytest.mark.asyncio
async def test_ocr_never_runs_the_consensus(script):
    transport = script(generate=_generate_body("ocr text"))

    text = _dedicated(3).recognize(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40)

    assert text == "ocr text"
    assert len(transport.leg_requests("generate")) == 1, "OCR is one request per operation"


@pytest.mark.asyncio
async def test_an_empty_payload_sends_nothing(script):
    transport = script()

    assert _dedicated(3).transcribe(b"") == ""
    assert transport.requests == []


@pytest.mark.asyncio
async def test_a_non_audio_payload_is_refused_before_any_request(script):
    transport = script()

    with pytest.raises(MediaError):
        _dedicated(3).transcribe(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40)

    assert transport.requests == []


# ── 6. Isolation, observability, and no state between operations ──


@pytest.mark.asyncio
async def test_the_reconciler_receives_only_the_hypotheses(script, monkeypatch):
    captured: list[tuple[Any, ...]] = []
    real_reconcile = engine_module.reconcile_hypotheses

    def spy(hypotheses):
        captured.append(tuple(hypotheses))
        return real_reconcile(hypotheses)

    monkeypatch.setattr(engine_module, "reconcile_hypotheses", spy)
    transport = script(interaction=[_interaction(SPEECH)] * 3)

    _dedicated(3).transcribe(_ogg_opus())

    assert len(captured) == 1
    (hypotheses,) = captured
    assert all(isinstance(text, str) for text in hypotheses)
    assert len(hypotheses) == 3
    assert not any(
        isinstance(value, (int, float, dict, list)) for value in hypotheses
    ), "no id, object or metadata can be passed alongside a hypothesis"


@pytest.mark.asyncio
async def test_no_telegram_metadata_reaches_the_model_or_the_reconciler(script, monkeypatch):
    captured: list[tuple[Any, ...]] = []
    real_reconcile = engine_module.reconcile_hypotheses

    def spy(hypotheses):
        captured.append(tuple(hypotheses))
        return real_reconcile(hypotheses)

    monkeypatch.setattr(engine_module, "reconcile_hypotheses", spy)
    transport = script(interaction=[_interaction(SPEECH)] * 3)

    _dedicated(3).transcribe(_ogg_opus())

    sent = b"\n".join(r.content for r in transport.requests).decode("utf-8", "ignore")
    for secret in (CAPTION, FILE_LABEL, "7770003", "-1007778889997"):
        assert secret not in sent, secret
        assert all(secret not in text for text in captured[0]), secret


@pytest.mark.asyncio
async def test_the_engine_still_holds_no_http_state_between_operations(script):
    transport = script(interaction=[_interaction(SPEECH)] * 3)

    engine = _dedicated(3)
    engine.transcribe(_ogg_opus())
    engine.transcribe(_ogg_opus())

    held = [
        getattr(engine, name)
        for name in GeminiMediaEngine.__slots__
        if type(getattr(engine, name)).__name__ in {"Client", "AsyncClient"}
    ]
    assert held == [], "one client per leg, nothing retained"
    assert len(transport.timeouts) == len(transport.requests)
    assert set(GeminiMediaEngine.__slots__) == {
        "_api_key", "_model", "_key_env_var", "_stt_model", "_stt_language", "_stt_passes",
    }


@pytest.mark.asyncio
async def test_the_consensus_line_carries_evidence_and_no_content(script, caplog):
    transport = script(interaction=[_interaction(READING_MINORITY),
                                    _interaction(SPEECH), _interaction(SPEECH)])

    with caplog.at_level("INFO", logger="backend.services.gemini_media_engine"):
        _dedicated(3).transcribe(_ogg_opus())

    line = _consensus_line(caplog)
    for field in ("passes=3", "hypotheses=3", "positions=", "changed=", "dropped=",
                  "elapsed_ms=", "transport=uri", "bytes="):
        assert field in line, field
    for secret in (SPEECH, READING_MINORITY, _FILE_URI, API_KEY, CAPTION, FILE_LABEL):
        assert secret not in line, secret


@pytest.mark.asyncio
async def test_the_outcome_line_is_emitted_once_per_multi_pass_operation(script, caplog):
    transport = script(interaction=[_interaction(SPEECH)] * 3)

    with caplog.at_level("INFO", logger="backend.services.gemini_media_engine"):
        _dedicated(3).transcribe(_ogg_opus())

    runs = [
        r.getMessage() for r in caplog.records
        if r.getMessage().startswith("GEMINI_MEDIA_ENGINE kind=")
    ]
    assert len(runs) == 1
    assert "stt_passes=3" in runs[0]
    assert "attempts=3" in runs[0]


@pytest.mark.asyncio
async def test_a_multi_pass_operation_is_still_sub_second_and_bounded(script):
    transport = script(interaction=[_interaction(SPEECH)] * 3)

    started = time.monotonic()
    _dedicated(3).transcribe(_ogg_opus())
    elapsed = time.monotonic() - started

    assert elapsed < 5.0, "one bounded operation, no waiting loop"
    assert len(transport.requests) == 6, "start, finalize, three interactions, one delete"
