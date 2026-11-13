"""Observability regression: AI model/provider fallback log events.

The pipeline already logged provider attempts and failures. What it did NOT
emit was the set of TRANSITIONS a deployed log reader needs: the 429 scope
classification, the fallback start (model vs provider), candidate exhaustion,
the final serving model, and continuation fallbacks. These tests pin the
emitted records, their levels, their mutual distinguishability, and the
absence of secrets in them.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.ai.providers.base.capabilities import ProviderCapabilities
from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.base.contract import BaseProvider, ProviderResponse
from backend.ai.providers.manager import manager as manager_module
from backend.ai.providers.manager.health import ProviderHealthState
from backend.ai.providers.manager.manager import ProviderManager

API_KEY = "sk-live-DEADBEEF0000000000"
PRIVATE_PROMPT = "PRIVATE-PROMPT-owner-only-text"
RESPONSE_BODY = "FULL-PROVIDER-RESPONSE-BODY-MARKER"


class _Scripted(BaseProvider):
    """Provider double that records the model each request actually asked for."""

    def __init__(
        self,
        name: str,
        responses: list[ProviderResponse] | None = None,
        model: str = "",
    ) -> None:
        super().__init__(
            ProviderConfig(provider_name=name, enabled=True, default_model=model)
        )
        self._name = name
        self._responses = list(responses or [])
        self.calls = 0
        self.seen_models: list[str] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supports_tools=True, supports_function_call=True, supports_json=True,
        )

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> ProviderResponse:
        self.calls += 1
        self.seen_models.append(
            str(kwargs.get("model") or self._config.default_model or "")
        )
        await asyncio.sleep(0)
        if self._responses:
            return self._responses.pop(0)
        return ProviderResponse(text="ok", provider_name=self._name, success=True)

    def initialize(self) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def health(self) -> dict[str, Any]:
        return {"healthy": True}


def _fail(text: str = "failed", **meta: Any) -> ProviderResponse:
    return ProviderResponse(text=text, provider_name="", success=False, metadata=meta)


def _ok(text: str = "ok", **meta: Any) -> ProviderResponse:
    return ProviderResponse(text=text, provider_name="", success=True, metadata=meta)


def _lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records]


def _with(lines: list[str], prefix: str) -> list[str]:
    return [line for line in lines if line.startswith(prefix)]


@pytest.fixture(autouse=True)
def _no_retry_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the ONE bounded retry, but not its wall-clock sleep."""
    monkeypatch.setattr(manager_module, "_RETRY_BACKOFF_SECONDS", 0.0)


@pytest.fixture(autouse=True)
def _restore_backend_log_level():
    """``_configure_logging()`` deliberately sets the namespace level.

    That is process-wide state, so leaving it behind would silently drop
    DEBUG records asserted by later tests in the same run.
    """
    namespace = logging.getLogger("backend")
    saved = namespace.level
    yield
    namespace.setLevel(saved)


# ── ProviderManager events ──


@pytest.mark.asyncio
async def test_attempt_and_selection_events_are_emitted_at_info(caplog):
    caplog.set_level(logging.INFO)
    pm = ProviderManager()
    pm.register_provider(_Scripted("p", [_ok()], model="m1"))
    pm.switch_provider("p")

    await pm.chat([{"role": "user", "content": PRIVATE_PROMPT}])

    selection = _with(_lines(caplog), "ROUTER_SELECTED")
    attempt = _with(_lines(caplog), "AI_PROVIDER_ATTEMPT")
    assert selection and "provider=p" in selection[0] and "model=m1" in selection[0]
    assert attempt and "attempt=1" in attempt[0]
    levels = {
        record.getMessage().split()[0]: record.levelno
        for record in caplog.records
        if record.getMessage().startswith(("ROUTER_SELECTED", "AI_PROVIDER_ATTEMPT"))
    }
    assert levels["ROUTER_SELECTED"] == logging.INFO
    assert levels["AI_PROVIDER_ATTEMPT"] == logging.INFO


@pytest.mark.asyncio
async def test_model_scoped_429_logs_classification_and_model_cooldown(caplog):
    caplog.set_level(logging.INFO)
    pm = ProviderManager()
    pm.register_provider(
        _Scripted(
            "p",
            [
                _fail(
                    "429 per model",
                    http_status=429,
                    failure_type="rate_limited",
                    retry_after=30,
                    rate_limit_scope="model",
                ),
                _ok(),
            ],
            model="m1",
        )
    )
    pm.switch_provider("p")
    pm.set_model_candidates("p", ["m2"])

    response = await pm.chat([{"role": "user", "content": "hi"}])

    lines = _lines(caplog)
    classified = _with(lines, "AI_RATE_LIMIT_CLASSIFIED")
    model_cooldown = _with(lines, "PROVIDER_MODEL_COOLDOWN")
    assert response.success is True
    assert classified and "scope=model" in classified[0]
    assert model_cooldown and "provider=p model=m1" in model_cooldown[0]
    assert "category=rate_limited" in model_cooldown[0]
    assert _with(lines, "PROVIDER_COOLDOWN") == [], (
        "a model-scoped limit must not open the provider-wide circuit"
    )


@pytest.mark.asyncio
async def test_provider_quota_logs_a_distinguishable_provider_cooldown(caplog):
    caplog.set_level(logging.INFO)
    pm = ProviderManager()
    pm.register_provider(
        _Scripted(
            "p",
            [
                _fail(
                    "429 quota",
                    http_status=429,
                    failure_type="rate_limited",
                    retry_after=30,
                    provider_error_message=(
                        "You exceeded your current quota, please check your plan "
                        f"and billing details. key={API_KEY}"
                    ),
                ),
                _ok(),
            ],
            model="m1",
        )
    )
    pm.switch_provider("p")
    pm.set_model_candidates("p", ["m2", "m3"])

    await pm.chat([{"role": "user", "content": "hi"}])

    lines = _lines(caplog)
    classified = _with(lines, "AI_RATE_LIMIT_CLASSIFIED")
    provider_cooldown = _with(lines, "PROVIDER_COOLDOWN")
    assert classified and "scope=provider" in classified[0]
    assert provider_cooldown and "provider=p" in provider_cooldown[0]
    assert "category=rate_limited" in provider_cooldown[0]
    assert _with(lines, "PROVIDER_MODEL_COOLDOWN") == []


@pytest.mark.asyncio
async def test_ambiguous_429_sibling_probe_is_observable_as_a_model_fallback(caplog):
    caplog.set_level(logging.INFO)
    pm = ProviderManager()
    pm.register_provider(
        _Scripted(
            "p",
            [_fail("429 opaque", http_status=429, failure_type="rate_limited", retry_after=30), _ok()],
            model="m1",
        )
    )
    pm.switch_provider("p")
    pm.set_model_candidates("p", ["m2", "m3"])

    response = await pm.chat([{"role": "user", "content": "hi"}])

    lines = _lines(caplog)
    assert response.success is True
    assert _with(lines, "PROVIDER_AMBIGUOUS_429_PROBE")
    starts = _with(lines, "AI_FALLBACK_START")
    assert starts, "the fallback transition must be logged, not only its metadata"
    assert "kind=model" in starts[0]
    assert "from=p/m1" in starts[0] and "to=p/m2" in starts[0]


@pytest.mark.asyncio
async def test_same_model_retry_is_distinguishable_from_a_model_fallback(caplog):
    caplog.set_level(logging.INFO)
    pm = ProviderManager()
    pm.register_provider(
        _Scripted(
            "p",
            [_fail("timed out", failure_type="timeout"), _ok()],
            model="m1",
        )
    )
    pm.switch_provider("p")

    response = await pm.chat([{"role": "user", "content": "hi"}])

    lines = _lines(caplog)
    attempts = _with(lines, "AI_PROVIDER_ATTEMPT")
    assert response.success is True
    assert any("attempt=2" in line and "reason=transient" in line for line in attempts)
    assert _with(lines, "AI_PROVIDER_FAILURE")
    assert _with(lines, "AI_FALLBACK_START") == [], (
        "a same-model retry is not a model fallback"
    )
    assert (response.metadata or {}).get("model_fallback") is not True


@pytest.mark.asyncio
async def test_provider_fallback_is_distinguishable_from_a_model_fallback(caplog):
    caplog.set_level(logging.INFO)
    pm = ProviderManager()
    pm.register_provider(
        _Scripted("dead", [_fail("unauthorized", http_status=401, failure_type="auth")], model="m1")
    )
    pm.register_provider(_Scripted("backup", [_ok()], model="b1"))
    pm.switch_provider("dead")
    pm._fallback_chain = ["backup"]

    response = await pm.chat([{"role": "user", "content": "hi"}])

    lines = _lines(caplog)
    starts = _with(lines, "AI_FALLBACK_START")
    assert response.success is True
    assert starts and "kind=provider" in starts[0]
    assert "from=dead/m1" in starts[0] and "to=backup/b1" in starts[0]
    assert (response.metadata or {}).get("model_fallback") is not True, (
        "a provider change must not be reported as a model fallback"
    )


@pytest.mark.asyncio
async def test_candidate_exhaustion_reports_counts_and_a_sanitized_category(caplog):
    caplog.set_level(logging.INFO)
    pm = ProviderManager()
    pm.register_provider(
        _Scripted(
            "p",
            [_fail(RESPONSE_BODY, http_status=404, failure_type="model_not_found") for _ in range(3)],
            model="m1",
        )
    )
    pm.switch_provider("p")
    pm.set_model_candidates("p", ["m2", "m3"])

    response = await pm.chat([{"role": "user", "content": PRIVATE_PROMPT}])

    lines = _lines(caplog)
    exhausted = _with(lines, "AI_CANDIDATES_EXHAUSTED")
    assert response.success is False
    assert exhausted and "failed=3" in exhausted[0]
    assert "category=all_providers_failed" in exhausted[0]


# ── Dispatcher events ──


def _make_dispatcher(provider_responses: list[ProviderResponse], tool_executor=None):
    """Dispatcher wired to the REAL manager with one scripted provider.

    A ``MagicMock`` here is not a substitute: ``Dispatcher`` treats a
    non-``ProviderManager`` argument as a registry and wraps it, so these
    tests drive the same routing object production uses.
    """
    from backend.ai.engine.dispatcher import Dispatcher
    from backend.ai.engine.hooks import NOOP_HOOKS
    from backend.ai.engine.metrics import EngineMetrics

    pm = ProviderManager()
    pm.register_provider(_Scripted("p", provider_responses, model="configured-model"))
    pm.switch_provider("p")

    conv = MagicMock()
    sess = MagicMock()
    sess.session_id = "s"
    sess.owner_id = 123
    sess.active_provider = "test"
    conv.get_session.return_value = sess
    conv.restore_history = AsyncMock()
    conv.get_history.return_value = []

    pb = MagicMock()
    package = MagicMock()
    package.system_prompt = "sys"
    package.runtime_context = ""
    package.conversation_context = ""
    package.tool_context = ""
    package.user_input = "do it"
    package.estimated_tokens.estimated_input_tokens = 50
    package.estimated_tokens.prompt_size_chars = 100
    pb.build.return_value = package

    return Dispatcher(
        conv, pb, pm, NOOP_HOOKS, EngineMetrics(), tool_executor=tool_executor,
    )


def _request():
    from backend.ai.session.request import AIRequest

    return AIRequest(
        session_id="s1", message_id=1, owner_id=123,
        user_message=PRIVATE_PROMPT, chat_id=456,
    )


@pytest.mark.asyncio
async def test_dispatcher_logs_the_final_serving_model(caplog):
    caplog.set_level(logging.INFO)
    dispatcher = _make_dispatcher([
        _ok("done", finish_reason="stop", model="served-model"),
    ])

    await dispatcher.dispatch(_request())

    result = _with(_lines(caplog), "AI_RESULT")
    assert result, "the final AI result must be logged"
    assert "success=True" in result[0]
    assert "provider=p" in result[0]
    assert "model=served-model" in result[0]


@pytest.mark.asyncio
async def test_dispatcher_never_reports_a_failed_candidate_as_the_serving_model(caplog):
    caplog.set_level(logging.INFO)
    dispatcher = _make_dispatcher([
        _fail(RESPONSE_BODY, failure_type="rate_limited", model="dead-model"),
    ])

    await dispatcher.dispatch(_request())

    result = _with(_lines(caplog), "AI_RESULT")
    assert result and "success=False" in result[0]
    assert "model=-" in result[0] and "provider=-" in result[0]
    assert "dead-model" not in result[0], (
        "a failed candidate must never be reported as the serving model"
    )


@pytest.mark.asyncio
async def test_continuation_round_preserves_fallback_observability(caplog):
    from backend.ai.tools.executor import ToolExecutionResult

    caplog.set_level(logging.INFO)
    tool_response = ProviderResponse(
        text="", provider_name="test", success=True, usage={},
        tool_calls=[{"id": "t1", "name": "username_show", "arguments": {}}],
        metadata={"finish_reason": "tool_calls"},
    )
    continuation = _ok(
        "final answer",
        finish_reason="stop",
        model="fallback-model",
        fallback=True,
        model_fallback=True,
        fallback_from="p",
        fallback_from_model="primary-model",
        fallback_to="p",
        fallback_to_model="fallback-model",
    )

    executor = MagicMock()
    executor.execute_calls = AsyncMock(return_value=[
        ToolExecutionResult(tool_name="username_show", success=True, message="ok", data={}),
    ])
    executor._context = MagicMock()
    executor._context.extra = {}
    executor._context.telegram = None
    executor._context.tz_str = "UTC"
    executor._context.client = None

    dispatcher = _make_dispatcher([tool_response, continuation], tool_executor=executor)
    await dispatcher.dispatch(_request())

    lines = _lines(caplog)
    continuation_line = _with(lines, "AI_CONTINUATION_FALLBACK")
    assert continuation_line, "a continuation round that fell back must say so"
    assert "round=2" in continuation_line[0]
    assert "kind=model" in continuation_line[0]
    assert "from=p/primary-model" in continuation_line[0]
    assert "to=p/fallback-model" in continuation_line[0]
    result = _with(lines, "AI_RESULT")
    assert result and "model=fallback-model" in result[0]
    assert "model_fallback=True" in result[0]


# ── Hygiene and configuration ──


@pytest.mark.asyncio
async def test_emitted_records_never_carry_secrets_or_content(caplog):
    caplog.set_level(logging.DEBUG)
    pm = ProviderManager()
    pm.register_provider(
        _Scripted(
            "p",
            [
                _fail(
                    RESPONSE_BODY,
                    http_status=429,
                    failure_type="rate_limited",
                    retry_after=30,
                    rate_limit_scope="provider",
                    provider_error_message=f"invalid key {API_KEY}",
                )
            ],
            model="m1",
        )
    )
    pm.switch_provider("p")
    pm.set_model_candidates("p", ["m2"])

    await pm.chat([{"role": "user", "content": PRIVATE_PROMPT}])

    emitted = "\n".join(_lines(caplog))
    assert emitted, "the request must produce diagnostic records"
    for secret in (API_KEY, PRIVATE_PROMPT, RESPONSE_BODY):
        assert secret not in emitted
    assert all("\n" not in line for line in _lines(caplog))


def test_logging_configuration_adds_no_duplicate_handler_or_record(caplog):
    import backend.main as main_module

    root = logging.getLogger()
    handlers_before = list(root.handlers)
    main_module._configure_logging()
    main_module._configure_logging()
    assert list(root.handlers) == handlers_before, "no second root handler"

    caplog.set_level(logging.INFO)
    logging.getLogger("backend.ai.providers.manager.manager").info("ONCE_ONLY_MARKER")
    assert sum(1 for line in _lines(caplog) if line == "ONCE_ONLY_MARKER") == 1


def test_log_level_is_resolved_and_clamped_to_the_application_namespace(monkeypatch):
    import backend.main as main_module

    assert main_module._resolve_level("", logging.INFO) == logging.INFO
    assert main_module._resolve_level("debug", logging.INFO) == logging.DEBUG
    assert main_module._resolve_level("WARNING", logging.INFO) == logging.WARNING
    assert main_module._resolve_level("not-a-level", logging.INFO) == logging.INFO

    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    main_module._configure_logging()
    assert logging.getLogger("backend").getEffectiveLevel() == logging.DEBUG
    assert logging.getLogger().level == logging.WARNING, (
        "third-party namespaces must stay at WARNING"
    )

    monkeypatch.setenv("LOG_LEVEL", "INFO")
    main_module._configure_logging()
    assert logging.getLogger("backend").getEffectiveLevel() == logging.INFO
