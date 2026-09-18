"""
Media Processing M2.3 — STT provider health and the BOUNDED automatic fallback.

The Speech-to-Text control plane already knows every registered candidate and the
owner's persisted selection; until this phase the selection was the only candidate
ever tried, so ONE provider's transient failure failed the whole media operation.
This suite pins the execution layer that sits between them
(``backend/services/stt_fallback.py``) and nothing else:

  1. the CONFIGURED candidate is always the FIRST attempt of every request — it
     never permanently loses priority because it failed once, and nothing here
     writes the owner's selection, the ``ai_config`` row or the Telegram UI state;
  2. the rotation is derived from the control plane's own canonical order (never a
     hard-coded provider list), and only candidates the registry implements and the
     factory can actually build may be tried;
  3. classification is FAIL-CLOSED — the adapter's own ``retryable`` verdict wins,
     a recognized transient class may fall back, and every deterministic failure
     (credentials, unsupported audio/model, rejections, malformed or empty output,
     programming errors) propagates unchanged instead of cascading;
  4. every bound is finite and shared: at most ``MAX_PROVIDER_ATTEMPTS`` attempts
     per unit, each a STRICT SUBSET of the one budget the boundary handed in, and a
     starved substitute is never started just to time out;
  5. exhaustion is reported as ITSELF — one controlled failure that says the
     providers were exhausted and never that the audio was invalid — while a
     request with no runnable substitute keeps the selected provider's own failure
     verbatim;
  6. health/cooldown is process-local posture: a substitute that keeps failing
     leaves the rotation for a bounded cooldown that expires deterministically, a
     success restores it immediately, and nothing is persisted;
  7. the boundary keeps every existing guarantee — one download, the same
     validation/cleanup, the same normalized ``MediaAnalysis.content``, one
     engine call per attempt, the multi-chunk route pinned to the candidate that
     worked, and no second STT pipeline, provider adapter or dependency;
  8. zero context — an attempt carries candidate ids, audio bytes and a numeric
     budget, and no Telegram identifier, caption, transcript or credential ever
     reaches a trace.

The engines here are scripted and the audio fixtures are synthetic containers, so
nothing in this file says anything about recognition QUALITY, and no provider is
claimed healthy. Live Telegram verification of this phase was NOT performed.
"""
from __future__ import annotations

import asyncio
import ast
import inspect
import logging
import pathlib
from typing import Any

import pytest

from backend.ai import stt_control_plane
from backend.services import media_service, stt_engine_factory, stt_fallback
from backend.services.media_service import (
    MAX_STT_CHARS,
    MEDIA_STAGE_STT_ENGINE,
    MEDIA_STAGE_STT_EXHAUSTED,
    MEDIA_STAGE_STT_TIMEOUT,
    MediaError,
)
from tests.test_media_stt import (
    CAPTION,
    OWNER,
    _FakeClient,
    _media_temp_dirs,
    _ogg_opus,
    _voice_message,
)
from tests.test_media_stt_chunking import _ogg_opus_paged

#: A transcript that must never appear in a trace, a log line or another
#: candidate's attempt — the token is deliberately improbable.
TRANSCRIPT = "پاسخ-فالبک-7f3a"
SELECTED_TEXT = "پاسخ-مدل-انتخاب‌شده-2b91"

BACKUP_ID = "groq:whisper-large-v3"
TURBO_ID = "groq:whisper-large-v3-turbo"
SPEECHMATICS_ID = "speechmatics:standard"
GEMINI_TRANSCRIBE_ID = "gemini:gemini-3.5-transcribe"
DEFAULT_ID = stt_control_plane.DEFAULT_CANDIDATE_ID

#: The control plane's OWN canonical tail — the rotation order is derived from it.
CANONICAL = stt_control_plane.candidate_ids()

_MODULE_SOURCE = pathlib.Path(
    stt_fallback.__file__ if stt_fallback.__file__ else "backend/services/stt_fallback.py"
).read_text()


# ── Scripted candidate engines and the factory seam ──


class _Engine:
    """One scripted candidate engine: one outcome per call, then ``default``.

    An outcome is either the transcript to return or the exception to raise, so a
    single class covers success, a classified provider failure and a programming
    error. Every received payload is recorded, which is how "exactly one engine
    call per attempt" and "the audio is never handed to a second engine" are
    proven.
    """

    def __init__(self, *outcomes: Any, default: str = "") -> None:
        self.outcomes = list(outcomes)
        self.default = default
        self.calls: list[bytes] = []

    def transcribe(self, audio: bytes) -> str:
        self.calls.append(audio)
        if not self.outcomes:
            return self.default
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _provider_failure(
    failure_class: str = "timeout",
    *,
    retryable: bool | None = None,
    message: str = "the provider failed",
) -> MediaError:
    """A failure shaped exactly like the ones the provider adapters raise."""
    error = MediaError(message, stage=MEDIA_STAGE_STT_ENGINE)
    error.failure_class = failure_class
    if retryable is not None:
        error.retryable = retryable
    return error


class _Factory:
    """The candidate → engine seam, scripted and recorded.

    Mirrors ``stt_engine_factory.build_engine``'s contract: ``(engine, reason)``,
    with ``engine=None`` for a candidate this build cannot run. Recording the
    requested candidate ids (and the language/passes they were built with) is how
    "only eligible candidates are tried" and "the substitutes receive the owner's
    own settings" are proven.
    """

    def __init__(
        self, engines: dict[str, Any], *, unbuildable: tuple[str, ...] = ()
    ) -> None:
        self.engines = dict(engines)
        self.unbuildable = set(unbuildable)
        self.built: list[str] = []
        self.settings: dict[str, tuple[str, int]] = {}

    def build_engine(self, candidate: Any, *, language: str = "", passes: int = 1):
        candidate_id = candidate.candidate_id
        self.built.append(candidate_id)
        self.settings[candidate_id] = (language, passes)
        if candidate_id in self.unbuildable:
            return None, "missing_credential"
        engine = self.engines.get(candidate_id)
        if engine is None:
            return None, "not_implemented"
        return engine, ""


class _Attempts:
    """The boundary's attempt primitive, recorded instead of executed.

    ``AttemptPlan.run`` hands this callable one engine, the already-validated audio,
    the REMAINING budget of the unit and the request's trace id — nowhere for a
    Telegram identifier to travel.
    """

    def __init__(self, *, delay: float = 0.0) -> None:
        self.calls: list[tuple[Any, bytes, float, str]] = []
        self.delay = delay

    async def __call__(self, engine: Any, data: bytes, timeout_s: float, *, request_id: str = "") -> str:
        self.calls.append((engine, data, timeout_s, request_id))
        if self.delay:
            await asyncio.sleep(self.delay)
        return engine.transcribe(data)

    @property
    def engines(self) -> list[Any]:
        return [call[0] for call in self.calls]

    @property
    def bounds(self) -> list[float]:
        return [call[2] for call in self.calls]


@pytest.fixture(autouse=True)
def _isolate_stt_state():
    """The engine seam, the rotation and the health map are process globals."""
    engine = media_service.get_stt_engine()
    media_service.set_stt_engine(None)
    stt_fallback.clear_registration()
    stt_fallback.reset_health()
    yield
    media_service.set_stt_engine(engine)
    stt_fallback.clear_registration()
    stt_fallback.reset_health()


class _Installer:
    """Installs a scripted candidate resolver for ONE test and exposes it.

    Callable (``factory({candidate_id: engine})``) so a test reads as the runtime
    does, while ``built`` / ``engines`` / ``settings`` stay reachable as plain
    attributes for the assertions.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch
        self.installed: _Factory | None = None

    def __call__(self, engines: dict[str, Any], *, unbuildable: tuple[str, ...] = ()) -> _Factory:
        self.installed = _Factory(engines, unbuildable=unbuildable)
        # ``stt_fallback`` resolves the factory module attribute per build, so this
        # is the same seam production uses — never a private override.
        self._monkeypatch.setattr(
            stt_engine_factory, "build_engine", self.installed.build_engine
        )
        return self.installed

    @property
    def built(self) -> list[str]:
        return [] if self.installed is None else self.installed.built

    @property
    def engines(self) -> dict[str, Any]:
        return {} if self.installed is None else self.installed.engines

    @property
    def settings(self) -> dict[str, tuple[str, int]]:
        return {} if self.installed is None else self.installed.settings


@pytest.fixture
def factory(monkeypatch) -> _Installer:
    """Install a scripted candidate → engine resolver for one test."""
    return _Installer(monkeypatch)


def _arm(active_id: str) -> stt_control_plane.SttControlPlane:
    """Arm the rotation the way the runtime does: from the parsed control plane."""
    plane = stt_control_plane.parse_stt_config(
        {stt_control_plane.STORAGE_KEY_ACTIVE: stt_control_plane.storage_value(active_id)}
    )
    stt_fallback.register_plan(plane)
    return plane


def _plan(active_id: str, selected_engine: Any) -> stt_fallback.AttemptPlan:
    """One request's attempt plan for a provisioned selected engine."""
    _arm(active_id)
    plan = stt_fallback.attempt_plan(selected_engine)
    assert plan is not None, "the rotation must be armed for this plan to exist"
    return plan


# ══ 1. Failure classification is fail-closed ══


def test_an_adapters_own_retryable_verdict_wins_over_its_class():
    """The adapter that talked to the provider is the honest classifier."""
    assert stt_fallback.fallback_eligible(
        _provider_failure("auth", retryable=True)
    ) is True
    assert stt_fallback.fallback_eligible(
        _provider_failure("timeout", retryable=False)
    ) is False


@pytest.mark.parametrize(
    "failure_class",
    [
        "timeout",
        "transport",
        "transport_failure",
        "server",
        "rate_limit",
        "operation_deadline",
        "upload_timeout",
        "request_timeout",
        "interaction_timeout",
    ],
)
def test_a_transient_class_without_a_verdict_may_fall_back(failure_class):
    """The engines' own transient vocabulary — mirrored, never invented."""
    assert stt_fallback.fallback_eligible(_provider_failure(failure_class)) is True


@pytest.mark.parametrize(
    "failure_class",
    [
        "auth",
        "forbidden",
        "missing_credential",
        "unsupported_model",
        "unsupported_audio",
        "invalid_request",
        "malformed_response",
        "empty_transcription",
        "provider_rejection",
        "upload_failed",
        "file_processing",
        "http_rejection",
        "something_a_future_adapter_invented",
    ],
)
def test_a_deterministic_class_never_falls_back(failure_class):
    """A rejected credential, payload or response must not cascade providers."""
    assert stt_fallback.fallback_eligible(_provider_failure(failure_class)) is False


def test_the_boundarys_own_timeout_leg_is_eligible():
    """``media_stt_timeout`` means the provider was too slow for THIS budget."""
    error = MediaError("too slow", stage=MEDIA_STAGE_STT_TIMEOUT)
    assert stt_fallback.fallback_eligible(error) is True


def test_an_unclassified_media_error_is_never_eligible():
    assert stt_fallback.fallback_eligible(
        MediaError("unclassified", stage=MEDIA_STAGE_STT_ENGINE)
    ) is False
    assert stt_fallback.fallback_eligible(MediaError("bare")) is False


@pytest.mark.parametrize(
    "error", [RuntimeError("engine exploded"), ValueError("bad"), KeyError("k")]
)
def test_a_programming_error_is_never_hidden_behind_fallback(error):
    """Anything that is not the boundary's failure type stays visible."""
    assert stt_fallback.fallback_eligible(error) is False


def test_a_failure_class_is_never_empty():
    assert stt_fallback.failure_class_of(_provider_failure("rate_limit")) == "rate_limit"
    assert stt_fallback.failure_class_of(
        MediaError("slow", stage=MEDIA_STAGE_STT_TIMEOUT)
    ) == "timeout"
    assert stt_fallback.failure_class_of(MediaError("bare")) == "unknown"


# ══ 2. Health and cooldown are bounded, process-local posture ══


def test_a_failure_puts_only_that_candidate_in_cooldown():
    cooldown = stt_fallback.record_failure(BACKUP_ID, "timeout")

    assert cooldown == stt_fallback.COOLDOWN_BASE_S
    assert stt_fallback.is_cooled_down(BACKUP_ID) is True
    assert stt_fallback.is_cooled_down(SPEECHMATICS_ID) is False


def test_the_cooldown_grows_boundedly_and_is_capped():
    seen = [
        stt_fallback.record_failure(BACKUP_ID, "timeout") for _ in range(8)
    ]

    assert seen[:3] == [
        stt_fallback.COOLDOWN_BASE_S,
        stt_fallback.COOLDOWN_BASE_S * 2,
        stt_fallback.COOLDOWN_BASE_S * 4,
    ]
    assert seen[-1] == stt_fallback.COOLDOWN_MAX_S
    assert all(value <= stt_fallback.COOLDOWN_MAX_S for value in seen)


def test_a_success_restores_health_immediately():
    stt_fallback.record_failure(BACKUP_ID, "transport")
    stt_fallback.record_failure(BACKUP_ID, "transport")
    assert stt_fallback.is_cooled_down(BACKUP_ID) is True

    stt_fallback.record_success(BACKUP_ID)

    assert stt_fallback.is_cooled_down(BACKUP_ID) is False


def test_the_health_map_is_process_local_and_resettable():
    stt_fallback.record_failure(BACKUP_ID, "timeout")
    stt_fallback.reset_health()

    assert stt_fallback.is_cooled_down(BACKUP_ID) is False


def test_provider_health_is_never_persisted_and_never_reads_a_credential():
    """Runtime posture, not configuration: no store, no DB, no ENV, no secret."""
    imported: list[str] = []
    for node in ast.walk(ast.parse(_MODULE_SOURCE)):
        if isinstance(node, ast.Import):
            imported += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)

    banned = ("config_store", "supabase", "backend.db", "persistence", "sqlalchemy")
    assert not [name for name in imported if any(word in name for word in banned)]
    assert "os" not in imported, "the fallback layer owns no credential and no ENV"
    assert "getenv" not in _MODULE_SOURCE and "environ" not in _MODULE_SOURCE


def test_the_fallback_bounds_are_finite_and_small():
    assert 1 <= stt_fallback.MAX_PROVIDER_ATTEMPTS <= 3
    assert stt_fallback.MIN_ATTEMPT_S > 0
    assert 0 < stt_fallback.COOLDOWN_BASE_S <= stt_fallback.COOLDOWN_MAX_S


def test_the_fallback_adds_no_second_pipeline_or_transport():
    """It decides WHICH candidate runs — it never talks to a provider itself."""
    for statement in (
        "import httpx",
        "import requests",
        "import subprocess",
        "import socket",
    ):
        assert statement not in _MODULE_SOURCE
    assert "asyncio.to_thread" not in _MODULE_SOURCE
    assert "def transcribe" not in _MODULE_SOURCE


# ══ 3. Arming: the rotation comes from the control plane, never a new list ══


def test_no_rotation_means_no_plan():
    """An unarmed runtime keeps its exact pre-fallback single-engine behavior."""
    assert stt_fallback.registration() is None
    assert stt_fallback.attempt_plan(_Engine(default="x")) is None


def test_no_provisioned_engine_means_no_plan():
    _arm(BACKUP_ID)
    assert stt_fallback.attempt_plan(None) is None


def test_the_rotation_is_the_control_planes_own_canonical_tail():
    plan = _plan(BACKUP_ID, _Engine(default="x"))

    assert plan.selected_id == BACKUP_ID
    assert plan.candidate_count == len(CANONICAL)
    registration = stt_fallback.registration()
    assert registration is not None
    assert registration.active_id == BACKUP_ID
    assert registration.fallback_ids == tuple(
        candidate_id for candidate_id in CANONICAL if candidate_id != BACKUP_ID
    )


def test_the_selected_candidate_is_the_first_attempt_of_every_request():
    selected = _Engine(default=SELECTED_TEXT)
    plan = _plan(SPEECHMATICS_ID, selected)

    assert plan._order()[0] == SPEECHMATICS_ID


def test_a_legacy_selection_deactivates_the_rotation():
    """An unresolved pre-existing model keeps its verbatim single-engine route."""
    plane = stt_control_plane.parse_stt_config(
        {stt_control_plane.STORAGE_KEY_ACTIVE: "some-legacy-model-id"}
    )
    assert plane.is_legacy is True

    stt_fallback.register_plan(plane)

    assert stt_fallback.registration() is None
    assert stt_fallback.attempt_plan(_Engine(default="x")) is None


def test_the_factory_arms_the_rotation_from_the_same_config_it_applies():
    """ONE parsed configuration provisions the engine and the rotation."""
    config = {stt_control_plane.STORAGE_KEY_ACTIVE: ""}

    stt_engine_factory.apply_stt_config(config)

    registration = stt_fallback.registration()
    assert registration is not None
    assert registration.active_id == DEFAULT_ID
    assert registration.fallback_ids == tuple(
        candidate_id for candidate_id in CANONICAL if candidate_id != DEFAULT_ID
    )


def test_a_selected_candidate_with_no_engine_leaves_the_boundary_fail_closed(
    factory, monkeypatch
):
    """A rotation whose FIRST candidate cannot run must not arm substitutes."""
    factory({}, unbuildable=(BACKUP_ID,))

    result = stt_engine_factory.apply_stt_config(
        {stt_control_plane.STORAGE_KEY_ACTIVE: BACKUP_ID}
    )

    assert result["configured"] is False
    assert stt_fallback.registration() is None
    assert media_service.get_stt_engine() is None


@pytest.mark.asyncio
async def test_substitutes_are_built_with_the_owners_own_settings(factory):
    factory({TURBO_ID: _Engine(default=TRANSCRIPT)})
    plane = _arm(BACKUP_ID)
    plan = stt_fallback.attempt_plan(_Engine(_provider_failure("timeout"), default="x"))
    assert plan is not None

    await plan.run(b"audio", 30.0, _Attempts(), request_id="r1")

    assert factory.settings[TURBO_ID] == (plane.language, plane.passes)


# ══ 4. The bounded attempt loop ══


@pytest.mark.asyncio
async def test_a_healthy_selected_provider_serves_the_request_alone(factory):
    factory({TURBO_ID: _Engine(default=TRANSCRIPT)})
    selected = _Engine(default=SELECTED_TEXT)
    plan = _plan(BACKUP_ID, selected)
    attempts = _Attempts()

    text = await plan.run(b"audio", 30.0, attempts, request_id="r1")

    assert text == SELECTED_TEXT
    assert attempts.engines == [selected]
    assert factory.built == [], "a healthy request never even builds a substitute"


@pytest.mark.asyncio
async def test_an_eligible_failure_moves_to_the_next_candidate(factory):
    factory({TURBO_ID: _Engine(default=TRANSCRIPT)})
    selected = _Engine(_provider_failure("transport"))
    plan = _plan(BACKUP_ID, selected)
    attempts = _Attempts()

    text = await plan.run(b"audio", 30.0, attempts, request_id="r1")

    assert text == TRANSCRIPT
    assert attempts.engines == [selected, factory.engines[TURBO_ID]]
    assert len(selected.calls) == 1


@pytest.mark.asyncio
async def test_a_deterministic_failure_is_never_cascaded(factory):
    factory({TURBO_ID: _Engine(default=TRANSCRIPT)})
    original = _provider_failure("auth", retryable=False, message="bad credential")
    plan = _plan(BACKUP_ID, _Engine(original))

    with pytest.raises(MediaError) as error:
        await plan.run(b"audio", 30.0, _Attempts(), request_id="r1")

    assert error.value is original
    assert error.value.stage == MEDIA_STAGE_STT_ENGINE
    assert error.value.failure_class == "auth"
    assert factory.built == [], "a rejected credential must never cascade"


@pytest.mark.asyncio
async def test_a_programming_error_propagates_unwrapped(factory):
    factory({TURBO_ID: _Engine(default=TRANSCRIPT)})
    plan = _plan(BACKUP_ID, _Engine(RuntimeError("engine exploded")))

    with pytest.raises(RuntimeError, match="engine exploded"):
        await plan.run(b"audio", 30.0, _Attempts(), request_id="r1")

    assert factory.built == []


@pytest.mark.asyncio
async def test_the_attempt_ceiling_is_enforced(factory):
    engines = {
        candidate_id: _Engine(_provider_failure("server"))
        for candidate_id in CANONICAL
    }
    selected = engines.pop(BACKUP_ID)
    factory(engines)
    plan = _plan(BACKUP_ID, selected)
    attempts = _Attempts()

    with pytest.raises(MediaError) as error:
        await plan.run(b"audio", 60.0, attempts, request_id="r1")

    assert len(attempts.calls) == stt_fallback.MAX_PROVIDER_ATTEMPTS
    assert error.value.stage == MEDIA_STAGE_STT_EXHAUSTED
    assert "3 attempts" in str(error.value)


@pytest.mark.asyncio
async def test_the_budget_is_shared_and_never_extended(factory):
    factory(
        {
            TURBO_ID: _Engine(default=TRANSCRIPT),
            SPEECHMATICS_ID: _Engine(default=TRANSCRIPT),
        }
    )
    plan = _plan(BACKUP_ID, _Engine(_provider_failure("timeout")))
    attempts = _Attempts(delay=0.05)

    await plan.run(b"audio", 30.0, attempts, request_id="r1")

    bounds = attempts.bounds
    assert len(bounds) == 2
    assert bounds[1] < bounds[0], "the first attempt's elapsed time is not given back"
    assert bounds[1] <= 30.0


@pytest.mark.asyncio
async def test_a_starved_substitute_is_never_started(caplog, factory):
    caplog.set_level(logging.INFO)
    substitute = _Engine(default=TRANSCRIPT)
    factory({TURBO_ID: substitute})
    original = _provider_failure("timeout", message="selected was too slow")
    plan = _plan(BACKUP_ID, _Engine(original))
    attempts = _Attempts(delay=0.25)

    with pytest.raises(MediaError) as error:
        await plan.run(b"audio", stt_fallback.MIN_ATTEMPT_S + 0.05, attempts, request_id="r1")

    # Only the selected provider ran, so its OWN failure is the honest diagnosis.
    assert error.value is original
    assert len(attempts.calls) == 1
    assert substitute.calls == []
    assert factory.built == [], "a starved substitute is not even constructed"
    assert "STT_FALLBACK_STOPPED reason=insufficient_budget" in caplog.text


@pytest.mark.asyncio
async def test_exhaustion_is_reported_as_itself_and_keeps_the_last_reason(factory):
    factory(
        {
            TURBO_ID: _Engine(_provider_failure("rate_limit", message="turbo said 429")),
            SPEECHMATICS_ID: _Engine(
                _provider_failure("server", message="speechmatics said 500")
            ),
        }
    )
    plan = _plan(BACKUP_ID, _Engine(_provider_failure("timeout", message="slow")))
    attempts = _Attempts()

    with pytest.raises(MediaError) as error:
        await plan.run(b"audio", 60.0, attempts, request_id="r1")

    assert len(attempts.calls) == 3
    assert error.value.stage == MEDIA_STAGE_STT_EXHAUSTED
    assert error.value.failure_class == stt_fallback.STT_FALLBACK_EXHAUSTED
    assert "every eligible provider" in str(error.value)
    # The LAST failure's bounded reason is preserved, already sanitized by the
    # adapter that raised it — and the error never blames the audio.
    assert "speechmatics said 500" in str(error.value)
    assert "audio" not in str(error.value).lower()


@pytest.mark.asyncio
async def test_no_runnable_substitute_keeps_the_selected_failure_verbatim(factory):
    """Fail-closed: arming the layer never rewrites a single-provider failure."""
    factory(
        {TURBO_ID: _Engine(default=TRANSCRIPT)},
        unbuildable=(GEMINI_TRANSCRIBE_ID, TURBO_ID, SPEECHMATICS_ID),
    )
    original = _provider_failure("transport", message="selected transport died")
    plan = _plan(BACKUP_ID, _Engine(original))
    attempts = _Attempts()

    with pytest.raises(MediaError) as error:
        await plan.run(b"audio", 60.0, attempts, request_id="r1")

    assert error.value is original
    assert error.value.stage == MEDIA_STAGE_STT_ENGINE
    assert len(attempts.calls) == 1


@pytest.mark.asyncio
async def test_a_candidate_that_failed_this_request_is_not_retried_within_it(factory):
    factory({TURBO_ID: _Engine(default=TRANSCRIPT)})
    plan = _plan(BACKUP_ID, _Engine(_provider_failure("server"), _provider_failure("server")))
    attempts = _Attempts()

    await plan.run(b"audio", 60.0, attempts, request_id="r1")
    order = plan._order()

    assert order[0] == TURBO_ID, "the candidate that worked is pinned for this request"
    assert BACKUP_ID not in order, "a candidate that failed this request is not retried"


@pytest.mark.asyncio
async def test_a_cooldown_prunes_only_the_fallback_rotation(caplog, factory):
    caplog.set_level(logging.INFO)
    turbo = _Engine(default="turbo text")
    speechmatics = _Engine(default=TRANSCRIPT)
    factory({TURBO_ID: turbo, SPEECHMATICS_ID: speechmatics})
    stt_fallback.record_failure(TURBO_ID, "server")
    selected = _Engine(_provider_failure("timeout"))
    plan = _plan(BACKUP_ID, selected)
    attempts = _Attempts()

    text = await plan.run(b"audio", 60.0, attempts, request_id="r1")

    assert text == TRANSCRIPT
    assert attempts.engines[0] is selected, "cooldown never demotes the selection"
    assert turbo.calls == [], "a cooled-down substitute leaves the rotation"
    assert speechmatics.calls == [b"audio"]
    assert (
        "STT_FALLBACK_SKIPPED candidate=groq:whisper-large-v3-turbo reason=cooldown"
        in caplog.text
    )


@pytest.mark.asyncio
async def test_the_selected_candidate_keeps_its_priority_after_a_fallback(factory):
    """A cooled-down selection is still attempted FIRST on the next request."""
    factory({TURBO_ID: _Engine(default=TRANSCRIPT)})
    stt_fallback.record_failure(BACKUP_ID, "timeout")
    first = _Engine(default=SELECTED_TEXT)
    second = _Engine(_provider_failure("timeout"), default=SELECTED_TEXT)
    _arm(BACKUP_ID)

    plan_one = stt_fallback.attempt_plan(first)
    assert plan_one is not None
    assert await plan_one.run(b"audio", 30.0, _Attempts(), request_id="r1") == SELECTED_TEXT

    plan_two = stt_fallback.attempt_plan(second)
    assert plan_two is not None
    attempts = _Attempts()
    text = await plan_two.run(b"audio", 30.0, attempts, request_id="r2")

    assert text == TRANSCRIPT
    assert attempts.engines[0] is second, "the owner's selection is always attempt one"
    assert attempts.engines[1] is factory.engines[TURBO_ID]


@pytest.mark.asyncio
async def test_a_success_clears_the_candidates_cooldown(factory):
    """Cooldown never suppresses the selection, and success heals it at once."""
    factory({TURBO_ID: _Engine(default=TRANSCRIPT)})
    stt_fallback.record_failure(BACKUP_ID, "timeout")
    assert stt_fallback.is_cooled_down(BACKUP_ID) is True
    plan = _plan(BACKUP_ID, _Engine(default=SELECTED_TEXT))

    text = await plan.run(b"audio", 60.0, _Attempts(), request_id="r1")

    assert text == SELECTED_TEXT
    assert stt_fallback.is_cooled_down(BACKUP_ID) is False


# ══ 5. The boundary end-to-end ══


@pytest.mark.asyncio
async def test_a_single_piece_request_uses_the_rotation_end_to_end(factory):
    payload = _ogg_opus(2.0)
    factory({TURBO_ID: _Engine(default=TRANSCRIPT)})
    selected = _Engine(_provider_failure("timeout"))
    _arm(BACKUP_ID)
    media_service.set_stt_engine(selected)

    analysis = await media_service.analyze_media(
        _FakeClient(payload=payload), OWNER, _voice_message(payload),
    )

    assert analysis.content == TRANSCRIPT
    assert len(selected.calls) == 1
    assert factory.engines[TURBO_ID].calls == [payload]


@pytest.mark.asyncio
async def test_the_transcript_reaches_the_boundary_normalized(factory):
    payload = _ogg_opus(2.0)
    factory({TURBO_ID: _Engine(default="  خط\u200cاول \n\n خط دوم  ")})
    _arm(BACKUP_ID)
    media_service.set_stt_engine(_Engine(_provider_failure("transport")))

    analysis = await media_service.analyze_media(
        _FakeClient(payload=payload), OWNER, _voice_message(payload),
    )

    assert analysis.content == "خط\u200cاول\n\nخط دوم"
    assert len(analysis.content) <= MAX_STT_CHARS


@pytest.mark.asyncio
async def test_a_fallback_never_rewrites_the_owners_selection(factory, monkeypatch):
    payload = _ogg_opus(2.0)
    factory({TURBO_ID: _Engine(default=TRANSCRIPT)})
    selected = _Engine(_provider_failure("timeout"))
    config = {
        stt_control_plane.STORAGE_KEY_ACTIVE: stt_control_plane.storage_value(BACKUP_ID)
    }
    _arm(BACKUP_ID)
    media_service.set_stt_engine(selected)

    from backend.ai import config_store

    async def _forbidden_write(*args: Any, **kwargs: Any) -> bool:
        raise AssertionError("a runtime fallback must never write the persisted config")

    monkeypatch.setattr(config_store, "update_setting", _forbidden_write)

    analysis = await media_service.analyze_media(
        _FakeClient(payload=payload), OWNER, _voice_message(payload),
    )

    assert analysis.content == TRANSCRIPT
    assert media_service.get_stt_engine() is selected
    registration = stt_fallback.registration()
    assert registration is not None and registration.active_id == BACKUP_ID
    # The SAME persisted configuration still resolves to the same selection.
    assert (
        stt_control_plane.parse_stt_config(config).active_id == BACKUP_ID
    )


@pytest.mark.asyncio
async def test_an_unarmed_boundary_keeps_its_single_engine_identity(factory):
    """No rotation (legacy/unconfigured) → the pre-fallback behavior, verbatim."""
    payload = _ogg_opus(2.0)
    factory({TURBO_ID: _Engine(default=TRANSCRIPT)})
    original = _provider_failure("timeout", message="the only engine was slow")
    selected = _Engine(original)
    media_service.set_stt_engine(selected)

    with pytest.raises(MediaError) as error:
        await media_service.analyze_media(
            _FakeClient(payload=payload), OWNER, _voice_message(payload),
        )

    assert error.value is original
    assert error.value.stage == MEDIA_STAGE_STT_ENGINE
    assert len(selected.calls) == 1
    assert factory.built == []


@pytest.mark.asyncio
async def test_exhaustion_at_the_boundary_names_the_dead_leg(factory):
    payload = _ogg_opus(2.0)
    factory(
        {TURBO_ID: _Engine(_provider_failure("rate_limit"))},
        unbuildable=(GEMINI_TRANSCRIBE_ID, SPEECHMATICS_ID),
    )
    _arm(BACKUP_ID)
    media_service.set_stt_engine(_Engine(_provider_failure("timeout")))

    with pytest.raises(MediaError) as error:
        await media_service.analyze_media(
            _FakeClient(payload=payload), OWNER, _voice_message(payload),
        )

    assert error.value.stage == MEDIA_STAGE_STT_EXHAUSTED
    assert error.value.failure_class == stt_fallback.STT_FALLBACK_EXHAUSTED


@pytest.mark.asyncio
async def test_a_chunked_request_pins_the_substitute_for_later_chunks(factory):
    payload = _ogg_opus_paged(840.0)
    substitute = _Engine("chunk 1", "chunk 2", "chunk 3")
    factory({TURBO_ID: substitute})
    selected = _Engine(_provider_failure("timeout"))
    _arm(BACKUP_ID)
    media_service.set_stt_engine(selected)

    analysis = await media_service.analyze_media(
        _FakeClient(payload=payload), OWNER, _voice_message(payload),
    )

    assert analysis.content == "chunk 1\nchunk 2\nchunk 3"
    assert len(selected.calls) == 1, "the selected provider served only the chunk it failed"
    assert len(substitute.calls) == 3, "the candidate that worked kept the whole recording"


@pytest.mark.asyncio
async def test_a_chunked_operation_still_fails_whole_when_the_rotation_is_exhausted(
    factory,
):
    payload = _ogg_opus_paged(840.0)
    substitute = _Engine(_provider_failure("server"))
    factory({TURBO_ID: substitute}, unbuildable=(GEMINI_TRANSCRIBE_ID, SPEECHMATICS_ID))
    selected = _Engine(_provider_failure("timeout"))
    _arm(BACKUP_ID)
    media_service.set_stt_engine(selected)

    with pytest.raises(MediaError) as error:
        await media_service.analyze_media(
            _FakeClient(payload=payload), OWNER, _voice_message(payload),
        )

    assert error.value.stage == MEDIA_STAGE_STT_EXHAUSTED
    assert len(selected.calls) == 1 and len(substitute.calls) == 1
    assert "chunk" not in str(error.value)


@pytest.mark.asyncio
async def test_a_fallback_adds_no_second_download_and_leaks_nothing(factory):
    payload = _ogg_opus(2.0)
    factory({TURBO_ID: _Engine(default=TRANSCRIPT)})
    _arm(BACKUP_ID)
    media_service.set_stt_engine(_Engine(_provider_failure("timeout")))
    client = _FakeClient(payload=payload)
    before = _media_temp_dirs()

    await media_service.analyze_media(client, OWNER, _voice_message(payload))

    downloads = [call for call in client.calls if call["op"] == "download_media"]
    assert len(downloads) == 1, "the boundary still owns ONE bounded transfer"
    assert _media_temp_dirs() == before, "no temporary artefact survives the attempt"


# ══ 6. Content-free traces and zero Telegram context ══


@pytest.mark.asyncio
async def test_the_traces_are_bounded_diagnostic_and_content_free(caplog, factory):
    caplog.set_level(logging.INFO)
    payload = _ogg_opus(2.0)
    factory({TURBO_ID: _Engine(default=TRANSCRIPT)})
    _arm(BACKUP_ID)
    media_service.set_stt_engine(_Engine(_provider_failure("transport")))

    await media_service.analyze_media(
        _FakeClient(payload=payload), OWNER, _voice_message(payload),
    )

    # Only the FALLBACK layer's own lines are judged here: the media boundary's
    # pre-existing traces are a different component's contract.
    text = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name == "backend.services.stt_fallback"
    )

    assert (
        "STT_FALLBACK_ATTEMPT candidate=groq:whisper-large-v3 index=1 ceiling=3"
        in text
    )
    assert "STT_FALLBACK_FAILURE candidate=groq:whisper-large-v3 attempt=1" in text
    assert "failure_class=transport eligible=True" in text
    assert "STT_FALLBACK_SUCCESS candidate=groq:whisper-large-v3-turbo attempt=2" in text
    for forbidden in (TRANSCRIPT, SELECTED_TEXT, CAPTION, str(OWNER), "api_key"):
        assert forbidden not in text


def test_an_attempt_carries_no_telegram_context_by_construction():
    """The attempt seam has nowhere to carry a chat, message, sender or caption."""
    parameters = list(inspect.signature(stt_fallback.AttemptPlan.run).parameters)
    assert parameters == ["self", "audio", "bound_s", "run_engine", "request_id"]
    for forbidden in ("chat", "message", "sender", "caption", "reply", "telegram", "user"):
        assert not any(forbidden in name for name in parameters)


@pytest.mark.asyncio
async def test_the_engines_receive_only_the_validated_audio_bytes(factory):
    payload = _ogg_opus(2.0)
    substitute = _Engine(default=TRANSCRIPT)
    factory({TURBO_ID: substitute})
    _arm(BACKUP_ID)
    selected = _Engine(_provider_failure("timeout"))
    media_service.set_stt_engine(selected)

    # ``_voice_message`` carries CAPTION, which must never reach an engine.
    await media_service.analyze_media(
        _FakeClient(payload=payload), OWNER, _voice_message(payload),
    )

    assert selected.calls == [payload]
    assert substitute.calls == [payload]
    assert all(CAPTION.encode() not in call for call in selected.calls + substitute.calls)
