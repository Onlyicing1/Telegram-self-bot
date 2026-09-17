"""Groq STT provider test + the candidate → engine resolution seam (M2.1).

``tests/test_groq_stt_engine.py`` proves the adapter speaks the transcription
API correctly. This file proves the two capability-level seams around it:

  1. **resolution** — a REGISTERED candidate becomes the engine of ITS OWN
     provider and model (``backend/services/stt_engine_factory.py``); an
     unregistered model, an unimplemented candidate or a missing credential
     never silently becomes a different provider's model, and the media boundary
     stays fail-closed when nothing can be provisioned;
  2. **provider test** (``backend/ai/stt_provider_probe.py``) — "a credential
     exists" and "the provider answered with a transcript" are DIFFERENT states,
     an untested candidate says so, the observation is process-local (nothing is
     persisted, no new column or table), and no probe can carry a credential, a
     transcript or a Telegram identifier.

Nothing here claims recognition QUALITY: no real provider is contacted, and the
one live probe is opt-in and skipped without a credential.
"""
from __future__ import annotations

import ast
import inspect
import logging
import os
from typing import Any

import httpx
import pytest

from backend.ai import stt_control_plane, stt_provider_probe
from backend.services import groq_stt_engine, media_service, stt_engine_factory
from backend.services.media_service import MediaError

API_KEY = "gsk_provider-test-suite-key-must-never-be-logged"
TRANSCRIPT = "این یک متن آزمایشی است"

_BASE_CONFIG: dict[str, Any] = {
    "provider": "gemini", "model": "gemini-2.5-flash",
    "temperature": 0.7, "max_tokens": 4096, "history_budget": 4000,
    "system_prompt": "", "is_configured": True,
    "trigger_en": "Nova", "trigger_fa": "", "show_question": False,
}

_HTTPX_CLIENT = httpx.Client


# ── A minimal scripted transport: no credential, no byte leaves the process ──


class _Transport(httpx.BaseTransport):
    """One scriptable transcription leg: ``None`` (success), an exception or a status."""

    def __init__(self, hook: Any = None) -> None:
        self.hook = hook
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        if isinstance(self.hook, BaseException):
            raise self.hook
        if isinstance(self.hook, int):
            return httpx.Response(
                self.hook,
                json={"error": {"message": f"scripted {self.hook}"}},
                request=request,
            )
        body = self.hook if isinstance(self.hook, dict) else {"text": TRANSCRIPT}
        return httpx.Response(200, json=body, request=request)


def script(monkeypatch, hook: Any = None) -> _Transport:
    transport = _Transport(hook)

    def factory(*args: Any, **inner: Any) -> httpx.Client:
        inner["transport"] = transport
        return _HTTPX_CLIENT(*args, **inner)

    monkeypatch.setattr(httpx, "Client", factory)
    return transport


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """No credential, no probe history and no provisioned engine leaks between tests."""
    monkeypatch.delenv("AI_GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("AI_GEMINI_API_KEY", raising=False)
    stt_provider_probe.clear_results()
    media_service.set_stt_engine(None)
    yield
    stt_provider_probe.clear_results()
    media_service.set_stt_engine(None)


def _candidate(candidate_id: str) -> stt_control_plane.SttCandidate:
    candidate = stt_control_plane.get_candidate(candidate_id)
    assert candidate is not None, candidate_id
    return candidate


def _flatten(buttons) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for row in buttons:
        cells = row if isinstance(row, list) else [row]
        for btn in cells:
            data = getattr(btn, "data", None) or ""
            if isinstance(data, bytes):
                data = data.decode("utf-8", errors="replace")
            out.append((str(getattr(btn, "text", "") or ""), str(data)))
    return out


def _async_return(value):
    async def _inner(*_args, **_kwargs):
        return value

    return _inner


def _patch_ai(monkeypatch) -> None:
    """Point the handler's owner/config lookups at a deterministic owner."""
    from backend.bot.handlers import ai as ai_module

    monkeypatch.setattr(ai_module, "_get_owner_id", _async_return(4242))
    monkeypatch.setattr(ai_module, "_get_saved_config", _async_return(dict(_BASE_CONFIG)))


# ── 1. Candidate → engine resolution ───────────────────────────────────


def test_a_registered_groq_candidate_yields_its_own_model(monkeypatch):
    monkeypatch.setenv("AI_GROQ_API_KEY", API_KEY)

    engine, reason = stt_engine_factory.build_engine(_candidate("groq:whisper-large-v3"))

    assert reason == ""
    assert engine is not None and isinstance(engine, groq_stt_engine.GroqWhisperEngine)
    assert engine.model == "whisper-large-v3"
    assert engine.endpoint == f"{groq_stt_engine.GROQ_API_BASE}/audio/transcriptions"


def test_the_turbo_candidate_yields_its_own_model(monkeypatch):
    monkeypatch.setenv("AI_GROQ_API_KEY", API_KEY)

    engine, _reason = stt_engine_factory.build_engine(_candidate("groq:whisper-large-v3-turbo"))

    assert engine is not None and engine.model == "whisper-large-v3-turbo"


def test_every_registered_groq_candidate_resolves_to_its_own_registered_model(monkeypatch):
    """The registry IS the model list: each id maps to the adapter's own model."""
    monkeypatch.setenv("AI_GROQ_API_KEY", API_KEY)

    for candidate in stt_control_plane.all_candidates():
        if candidate.provider != "groq":
            continue
        engine, reason = stt_engine_factory.build_engine(candidate)

        assert reason == ""
        assert engine is not None and engine.model == candidate.model


def test_the_selection_reaches_the_engine_as_plain_settings(monkeypatch):
    monkeypatch.setenv("AI_GROQ_API_KEY", API_KEY)

    engine, _reason = stt_engine_factory.build_engine(
        _candidate("groq:whisper-large-v3"), language="fa-IR", passes=2,
    )

    assert engine is not None
    assert (engine.language, engine.passes) == ("fa", 2)
    assert engine.key_env_var == "AI_GROQ_API_KEY"


def test_an_unregistered_model_is_refused_by_the_resolution_seam(monkeypatch):
    """A typed model id can never be resolved into an engine."""
    monkeypatch.setenv("AI_GROQ_API_KEY", API_KEY)
    forged = stt_control_plane.SttCandidate(
        candidate_id="groq:whisper-huge", provider="groq", model="whisper-huge",
        label="forged", implemented=True,
    )

    engine, reason = stt_engine_factory.build_engine(forged)

    assert engine is None
    assert reason == groq_stt_engine.FAILURE_UNSUPPORTED_MODEL


def test_an_unimplemented_candidate_is_never_built():
    engine, reason = stt_engine_factory.build_engine(_candidate("speechmatics:standard"))

    assert engine is None
    assert reason == stt_engine_factory.REASON_NOT_IMPLEMENTED


def test_no_candidate_resolves_to_an_unregistered_stt_candidate():
    """An unregistered stored value stays LEGACY — it never becomes a candidate."""
    plane = stt_control_plane.parse_stt_config({"stt_model": "groq:whisper-large-v3"})

    assert plane.active_id == "groq:whisper-large-v3"
    assert plane.active_candidate is not None
    assert plane.active_candidate.model in groq_stt_engine.SUPPORTED_MODELS


# ── 2. Applying the selection to the LIVE boundary ─────────────────────


def test_the_selection_provisions_the_groq_engine_on_the_boundary(monkeypatch):
    monkeypatch.setenv("AI_GROQ_API_KEY", API_KEY)

    status = stt_engine_factory.apply_stt_config({
        "stt_model": "groq:whisper-large-v3-turbo", "stt_language": "fa-IR", "stt_passes": 2,
    })

    assert status["configured"] is True
    assert status["provider"] == "groq"
    assert status["stt_model"] == "whisper-large-v3-turbo"
    assert media_service.stt_available() is True
    engine = media_service.get_stt_engine()
    assert isinstance(engine, groq_stt_engine.GroqWhisperEngine)
    assert (engine.model, engine.language, engine.passes) == ("whisper-large-v3-turbo", "fa", 2)
    assert API_KEY not in repr(status)


def test_a_missing_credential_provisions_nothing_and_no_substitute(monkeypatch):
    """Fail closed: a Groq selection with no key never becomes a Gemini engine."""
    status = stt_engine_factory.apply_stt_config({"stt_model": "groq:whisper-large-v3"})

    assert status["configured"] is False
    assert status["provider"] == "groq"
    assert status["reason"] == groq_stt_engine.FAILURE_MISSING_CREDENTIAL
    assert media_service.stt_available() is False
    assert media_service.get_stt_engine() is None


def test_applying_a_config_never_raises_on_a_broken_value(monkeypatch):
    monkeypatch.setenv("AI_GROQ_API_KEY", API_KEY)

    for broken in (None, {}, {"stt_passes": object()}, {"stt_model": ["x"]}, {"stt_language": 5}):
        status = stt_engine_factory.apply_stt_config(broken)

        assert isinstance(status, dict) and "configured" in status


def test_a_legacy_value_keeps_the_previous_gemini_route(monkeypatch):
    monkeypatch.setenv("AI_GEMINI_API_KEY", "gemini-suite-key")

    status = stt_engine_factory.apply_stt_config({"stt_model": "gemini-2.5-pro"})

    assert status["configured"] is True
    engine = media_service.get_stt_engine()
    assert engine is not None and engine.stt_model == "gemini-2.5-pro"


def test_a_gemini_candidate_stays_on_the_gemini_leg(monkeypatch):
    monkeypatch.setenv("AI_GEMINI_API_KEY", "gemini-suite-key")

    status = stt_engine_factory.apply_stt_config({"stt_model": "gemini:gemini-3.5-transcribe"})

    assert status["configured"] is True
    engine = media_service.get_stt_engine()
    assert engine is not None and engine.stt_model == "gemini-3.5-transcribe"


def test_the_engine_factory_carries_no_owner_or_telegram_state(monkeypatch):
    monkeypatch.setenv("AI_GROQ_API_KEY", API_KEY)
    poisoned = {
        "stt_model": "groq:whisper-large-v3", "stt_language": "fa-IR", "stt_passes": 1,
        "owner_id": 12345, "chat_id": -100999, "message_id": 987,
        "sender": "someone", "username": "@someone", "caption": "secret caption",
        "filename": "voice.ogg", "history": ["earlier turn"],
    }

    stt_engine_factory.apply_stt_config(poisoned)

    engine = media_service.get_stt_engine()
    assert engine is not None
    blob = repr(engine) + repr(engine.__slots__)
    for leak in ("someone", "secret caption", "voice.ogg", "earlier turn", "12345"):
        assert leak not in blob


# ── 3. The provider test: credential presence is NOT health ────────────


@pytest.mark.asyncio
async def test_an_unknown_candidate_is_refused_without_a_request():
    result = await stt_provider_probe.test_candidate("groq:whisper-huge")

    assert result.state == stt_provider_probe.SttTestState.FAILED.value
    assert result.failure_class == stt_provider_probe.FAILURE_UNKNOWN_CANDIDATE
    assert result.transcript_chars == 0


@pytest.mark.asyncio
async def test_an_unimplemented_candidate_is_reported_and_never_probed():
    result = await stt_provider_probe.test_candidate("speechmatics:standard")

    assert result.state == stt_provider_probe.SttTestState.NOT_IMPLEMENTED.value
    assert result.failure_class == ""


@pytest.mark.asyncio
async def test_a_missing_credential_is_its_own_state_not_a_pass(monkeypatch):
    transport = script(monkeypatch)

    result = await stt_provider_probe.test_candidate("groq:whisper-large-v3")

    assert result.state == stt_provider_probe.SttTestState.CREDENTIAL_MISSING.value
    assert result.passed is False
    assert result.failure_class == ""
    assert transport.requests == [], "no request may be made without a credential"


@pytest.mark.asyncio
async def test_a_reachable_provider_with_a_transcript_passes(monkeypatch):
    monkeypatch.setenv("AI_GROQ_API_KEY", API_KEY)
    transport = script(monkeypatch)

    result = await stt_provider_probe.test_candidate("groq:whisper-large-v3")

    assert result.state == stt_provider_probe.SttTestState.PASSED.value
    assert result.passed is True
    assert result.transcript_chars == len(TRANSCRIPT)
    assert result.latency_ms >= 0
    assert len(transport.requests) == 1
    assert stt_provider_probe.last_result("groq:whisper-large-v3") == result
    assert stt_provider_probe.result_state("groq:whisper-large-v3") == "passed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "hook,failure_class",
    [
        (401, groq_stt_engine.FAILURE_AUTH),
        (403, groq_stt_engine.FAILURE_FORBIDDEN),
        (429, groq_stt_engine.FAILURE_RATE_LIMIT),
        (500, groq_stt_engine.FAILURE_SERVER),
        (httpx.ConnectError("scripted"), groq_stt_engine.FAILURE_TRANSPORT),
    ],
)
async def test_a_failed_probe_reports_the_deterministic_failure_class(monkeypatch, hook, failure_class):
    monkeypatch.setenv("AI_GROQ_API_KEY", API_KEY)
    script(monkeypatch, hook)

    result = await stt_provider_probe.test_candidate("groq:whisper-large-v3")

    assert result.state == stt_provider_probe.SttTestState.FAILED.value
    assert result.failure_class == failure_class
    assert result.passed is False


@pytest.mark.asyncio
async def test_an_empty_transcript_is_a_failed_probe(monkeypatch):
    """A provider that answers nothing is never a successful empty transcription."""
    monkeypatch.setenv("AI_GROQ_API_KEY", API_KEY)
    script(monkeypatch, {"text": ""})

    result = await stt_provider_probe.test_candidate("groq:whisper-large-v3")

    assert result.state == stt_provider_probe.SttTestState.FAILED.value
    assert result.failure_class == groq_stt_engine.FAILURE_EMPTY
    assert result.transcript_chars == 0


@pytest.mark.asyncio
async def test_the_probe_itself_is_bounded(monkeypatch):
    monkeypatch.setenv("AI_GROQ_API_KEY", API_KEY)
    script(monkeypatch)

    result = await stt_provider_probe.test_candidate("groq:whisper-large-v3", timeout=0.0)

    assert result.state == stt_provider_probe.SttTestState.FAILED.value
    assert result.failure_class == stt_provider_probe.FAILURE_TEST_TIMEOUT


@pytest.mark.asyncio
async def test_the_probe_runs_in_the_control_planes_canonical_order(monkeypatch):
    """Sequential, implemented-only, registry order — never a burst of requests."""
    recorded: list[str] = []

    def fake_build(candidate, **_kwargs):
        recorded.append(candidate.candidate_id)
        return None, groq_stt_engine.FAILURE_MISSING_CREDENTIAL

    monkeypatch.setattr(stt_engine_factory, "build_engine", fake_build)

    results = await stt_provider_probe.test_candidates()

    expected = [c.candidate_id for c in stt_control_plane.all_candidates() if c.implemented]
    assert recorded == expected
    assert [r.candidate_id for r in results] == expected


def test_the_probe_payload_is_inside_the_media_boundary_contract():
    payload = stt_provider_probe.test_audio_payload()

    channels, rate, duration = media_service._validate_audio_payload(payload, "audio/wav")

    assert (channels, rate) == (1, stt_provider_probe.TEST_SAMPLE_RATE)
    assert 0 < duration <= media_service.MAX_STT_DURATION_S
    assert groq_stt_engine.container_for(payload) == ("audio.wav", "audio/wav")
    assert len(payload) <= media_service.MAX_STT_INPUT_BYTES


def test_the_probe_payload_duration_is_clamped():
    long_payload = stt_provider_probe.test_audio_payload(10_000)

    _channels, _rate, duration = media_service._validate_audio_payload(long_payload, "audio/wav")

    assert duration <= 5.0


def test_the_probe_payload_carries_no_telegram_or_conversational_metadata():
    payload = stt_provider_probe.test_audio_payload()

    for leak in (b"owner", b"chat", b"caption", b"voice.ogg", b"@someone"):
        assert leak not in payload


# ── 4. Honest, bounded, process-local observations ─────────────────────


def test_an_untested_candidate_says_so():
    assert stt_provider_probe.result_state("groq:whisper-large-v3") == "not_tested"
    assert stt_provider_probe.state_label("groq:whisper-large-v3") == "not tested"
    assert stt_provider_probe.last_result("groq:whisper-large-v3") is None


def test_an_unimplemented_candidate_can_never_be_upgraded_by_a_stale_observation():
    """An observation can never make an unexecutable capability look usable."""
    candidate = _candidate("speechmatics:standard")

    assert stt_provider_probe.candidate_state_row(candidate) == "not available"
    assert stt_provider_probe.candidate_state_row(candidate) == stt_provider_probe.STATE_LABELS[
        stt_provider_probe.SttTestState.NOT_IMPLEMENTED.value
    ]


@pytest.mark.asyncio
async def test_a_probe_result_is_process_local_and_never_persisted(monkeypatch):
    """A health observation is not configuration: no store, no column, no table."""
    monkeypatch.setenv("AI_GROQ_API_KEY", API_KEY)
    script(monkeypatch)

    await stt_provider_probe.test_candidate("groq:whisper-large-v3")
    assert stt_provider_probe.result_state("groq:whisper-large-v3") == "passed"

    assert isinstance(stt_provider_probe._results, dict)
    tree = ast.parse(inspect.getsource(stt_provider_probe))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    for module_name in imported:
        assert not module_name.startswith(
            ("backend.ai.config_store", "backend.ai.persistence", "backend.db")
        ), module_name

    stt_provider_probe.clear_results()
    assert stt_provider_probe.result_state("groq:whisper-large-v3") == "not_tested"


@pytest.mark.asyncio
async def test_the_probe_logs_no_credential_and_no_transcript(monkeypatch, caplog):
    monkeypatch.setenv("AI_GROQ_API_KEY", API_KEY)
    script(monkeypatch)

    with caplog.at_level(logging.DEBUG):
        result = await stt_provider_probe.test_candidate("groq:whisper-large-v3")

    assert API_KEY not in caplog.text
    assert TRANSCRIPT not in caplog.text
    assert API_KEY not in str(result.to_dict())
    assert "transcript" not in {key for key in result.to_dict()}
    assert "STT_PROVIDER_TEST" in caplog.text


def test_the_probe_module_imports_no_telegram_or_handler_layer():
    tree = ast.parse(inspect.getsource(stt_provider_probe))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")

    for module_name in imported:
        assert not module_name.startswith(
            ("telethon", "backend.bot", "backend.helper", "backend.db", "backend.runtime")
        ), module_name


def test_the_adapter_does_not_depend_on_the_probe_or_the_factory():
    """The dependency direction stays one-way: engine ← factory ← probe."""
    source = inspect.getsource(groq_stt_engine)

    assert "stt_provider_probe" not in source
    assert "stt_engine_factory" not in source


# ── 5. The Telegram surface: one bounded probe per candidate ───────────


@pytest.mark.asyncio
async def test_the_panel_offers_a_test_action_for_every_executable_candidate():
    from backend.bot.handlers import ai_stt_settings as module

    _body, buttons = await module._media_stt_body_and_buttons(dict(_BASE_CONFIG))
    flat = _flatten(buttons)

    for candidate in stt_control_plane.all_candidates():
        if not candidate.implemented:
            continue
        assert (
            "Test", f"action:ai_stt_test_candidate:{candidate.candidate_id}",
        ) in flat or (
            f"Test · {candidate.label}",
            f"action:ai_stt_test_candidate:{candidate.candidate_id}",
        ) in flat

    assert not any("speechmatics" in data for _text, data in flat), (
        "an unimplemented candidate is never offered as a probe target"
    )


@pytest.mark.asyncio
async def test_the_panel_shows_the_probe_state_of_each_candidate():
    from backend.bot.handlers import ai_stt_settings as module

    body, _buttons = await module._media_stt_body_and_buttons({"stt_model": "groq:whisper-large-v3"})

    assert "not tested" in body
    assert "not available" in body  # the registered-but-unimplemented candidate


@pytest.mark.asyncio
async def test_the_test_action_probes_the_selected_registered_candidate(monkeypatch):
    from backend.bot.handlers import ai_stt_settings as module

    _patch_ai(monkeypatch)
    called: list[str] = []

    async def fake_probe(candidate_id, **_kwargs):
        called.append(candidate_id)
        return stt_provider_probe.SttTestResult(
            candidate_id=candidate_id, provider="groq", model="whisper-large-v3",
            state=stt_provider_probe.SttTestState.PASSED.value,
            latency_ms=412, transcript_chars=12, tested_at="now",
        )

    monkeypatch.setattr(stt_provider_probe, "test_candidate", fake_probe)

    result = await module._ai_stt_test_candidate_action(None, "groq:whisper-large-v3", 1)

    assert called == ["groq:whisper-large-v3"]
    assert result is not None
    assert result[1].startswith("\u2713")
    assert "412 ms" in result[1]


@pytest.mark.asyncio
async def test_the_test_action_refuses_an_unknown_candidate_without_probing(monkeypatch):
    from backend.bot.handlers import ai_stt_settings as module

    _patch_ai(monkeypatch)

    async def forbidden(*_args, **_kwargs):  # pragma: no cover - must never run
        raise AssertionError("an unknown identifier must never be probed")

    monkeypatch.setattr(stt_provider_probe, "test_candidate", forbidden)

    result = await module._ai_stt_test_candidate_action(None, "groq:whisper-huge", 1)

    assert result is not None and "\u00d7 Unknown" in result[1]


@pytest.mark.asyncio
async def test_the_panel_line_and_the_notice_never_imply_health(monkeypatch):
    from backend.bot.handlers import ai_stt_settings as module

    _patch_ai(monkeypatch)
    missing = stt_provider_probe.SttTestResult(
        candidate_id="groq:whisper-large-v3", provider="groq", model="whisper-large-v3",
        state=stt_provider_probe.SttTestState.CREDENTIAL_MISSING.value,
        detail="No credential is configured for this provider on this deployment.",
    )

    notice = module.test_notice(missing)

    assert notice.startswith("!")
    assert "no credential" in notice
    assert "nothing was sent" in notice

    line = module.stt_selection_line({"stt_model": "groq:whisper-large-v3"})
    assert "not tested" in line
    assert "passed" not in line


# ── 6. Live, opt-in probe (never part of the deterministic suite) ──────


@pytest.mark.skipif(
    not (os.getenv("AI_GROQ_API_KEY") or os.getenv("GROQ_API_KEY")),
    reason="no Groq credential in this environment — the live probe is opt-in",
)
@pytest.mark.asyncio
async def test_live_probe_reaches_groq_when_a_credential_is_available():
    """The ONLY test that contacts Groq, and only when a credential is present.

    A synthetic tone carries no speech, so the honest live outcome is either a
    transcript or an empty-transcription failure; what this proves is that the
    credential was accepted and the documented endpoint answered — never that
    recognition quality is good, and never by printing the credential.
    """
    result = await stt_provider_probe.test_candidate("groq:whisper-large-v3")

    assert result.state != stt_provider_probe.SttTestState.CREDENTIAL_MISSING.value
    assert result.failure_class not in {
        groq_stt_engine.FAILURE_MISSING_CREDENTIAL,
        groq_stt_engine.FAILURE_AUTH,
        groq_stt_engine.FAILURE_FORBIDDEN,
        groq_stt_engine.FAILURE_TIMEOUT,
        groq_stt_engine.FAILURE_TRANSPORT,
    }, result.failure_class
