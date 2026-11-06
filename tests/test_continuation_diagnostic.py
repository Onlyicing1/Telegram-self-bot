"""Automatic continuation diagnostics.

These tests exercise the REAL ``ProviderManager`` for routing/health state and
a fake provider transport for the probe itself — no live API key, no network.
The probe must stay a diagnostic: one bounded synthetic request for exactly the
pair that failed, never a tool execution, never a production side effect.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest

from backend.ai import continuation_diagnostic as diag
from backend.ai.providers.base.capabilities import ProviderCapabilities
from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.base.contract import BaseProvider, ProviderResponse
from backend.ai.providers.manager.manager import ProviderManager
from backend.ai.providers.registry.registry import ProviderRegistry
from backend.ai.tools.executor import ToolExecutor


# ── Doubles ──


class _ScriptedProvider(BaseProvider):
    """A registered provider whose answer the test controls."""

    def __init__(
        self,
        name: str,
        *,
        config: ProviderConfig | None = None,
        responses: list[ProviderResponse] | None = None,
    ) -> None:
        super().__init__(config or ProviderConfig(provider_name=name, enabled=True))
        self._name = name
        self._responses = list(responses or [])
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(supports_tools=True, supports_function_call=True)

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> ProviderResponse:
        self.calls += 1
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


class _ProbeTransport(BaseProvider):
    """Stand-in for the probe's own provider instance (never a live call)."""

    def __init__(self, name: str, config: ProviderConfig, factory: "_FakeFactory") -> None:
        super().__init__(config)
        self._name = name
        self._factory = factory

    @property
    def name(self) -> str:
        return self._name

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(supports_tools=True)

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> ProviderResponse:
        self._factory.calls.append({"messages": messages, "kwargs": kwargs})
        if self._factory.delay:
            await asyncio.sleep(self._factory.delay)
        if self._factory.responses:
            return self._factory.responses.pop(0)
        return ProviderResponse(text="ok", provider_name=self._name, success=True)

    def initialize(self) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def health(self) -> dict[str, Any]:
        return {"healthy": True}


class _FakeFactory:
    """Records every probe instance and request; returns scripted responses."""

    def __init__(
        self,
        responses: list[ProviderResponse] | None = None,
        *,
        error: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self.created: list[tuple[str, ProviderConfig]] = []
        self.calls: list[dict[str, Any]] = []
        self.responses = list(responses or [])
        self.error = error
        self.delay = delay

    def create_provider(self, name: str, config: ProviderConfig | None = None) -> BaseProvider:
        self.created.append((name, config))
        if self.error is not None:
            raise self.error
        return _ProbeTransport(name, config, self)


# ── Builders ──


def _config(name: str = "gemini", model: str = "gemini-2.5-pro") -> ProviderConfig:
    return ProviderConfig(
        provider_name=name,
        api_key="sk-test-secret-value",
        default_model=model,
        enabled=True,
        timeout=30,
    )


def _manager(
    name: str = "gemini",
    model: str = "gemini-2.5-pro",
    responses: list[ProviderResponse] | None = None,
) -> tuple[ProviderManager, ProviderConfig]:
    config = _config(name, model)
    registry = ProviderRegistry()
    registry.register(_ScriptedProvider(name, config=config, responses=responses))
    registry.switch_provider(name)
    return ProviderManager(registry), config


def _failure(
    provider: str = "gemini",
    model: str = "gemini-2.5-pro",
    category: str = "request",
) -> ProviderResponse:
    """A failed continuation response carrying the manager's failure matrix."""
    return ProviderResponse(
        text="All AI providers failed.",
        provider_name="",
        success=False,
        metadata={
            "fallback": True,
            "fallback_exhausted": True,
            "provider_matrix": [
                {"provider": "other", "model": "other-1", "outcome": "skipped", "reason": "cooling"},
                {"provider": provider, "model": model, "outcome": "failed", "category": category},
            ],
        },
    )


def _patch_factory(monkeypatch: pytest.MonkeyPatch, factory: _FakeFactory) -> _FakeFactory:
    monkeypatch.setattr(diag, "ProviderFactory", factory)
    return factory


# ── Triggering ──


@pytest.mark.asyncio
async def test_qualifying_continuation_failure_runs_one_probe(monkeypatch):
    diag.reset_state()
    manager, config = _manager()
    factory = _patch_factory(monkeypatch, _FakeFactory())

    task = diag.schedule_continuation_diagnostic(
        manager, _failure(category="request"), request_id="rid-1",
    )

    assert task is not None
    await task
    assert len(factory.created) == 1
    name, probe_config = factory.created[0]
    assert name == "gemini"
    # Same credential/endpoint/model resolution as production, separate object.
    assert probe_config is not config
    assert probe_config.api_key == config.api_key
    assert probe_config.default_model == config.default_model
    assert probe_config.base_url == config.base_url
    assert len(factory.calls) == 1


@pytest.mark.asyncio
async def test_probe_config_is_cheap_and_leaves_live_config_untouched(monkeypatch):
    diag.reset_state()
    config = ProviderConfig(
        provider_name="gemini",
        api_key="sk-live-secret",
        base_url="https://example.test/v1beta",
        default_model="gemini-2.5-pro",
        temperature=0.9,
        max_tokens=8192,
        enabled=True,
    )
    registry = ProviderRegistry()
    registry.register(_ScriptedProvider("gemini", config=config))
    registry.switch_provider("gemini")
    manager = ProviderManager(registry)
    factory = _patch_factory(monkeypatch, _FakeFactory())

    task = diag.schedule_continuation_diagnostic(manager, _failure(), request_id="rid")
    assert task is not None
    await task

    _, probe_config = factory.created[0]
    assert diag.PROBE_MAX_TOKENS == 64
    assert probe_config.max_tokens == 64
    assert probe_config.temperature == 0.0
    # Credential, endpoint and requested model are preserved exactly.
    assert probe_config.api_key == "sk-live-secret"
    assert probe_config.base_url == "https://example.test/v1beta"
    assert probe_config.default_model == "gemini-2.5-pro"
    assert factory.calls[0]["kwargs"]["model"] == "gemini-2.5-pro"
    # The live config is the very object production reads and is NOT mutated.
    live = manager.get_provider_config("gemini")
    assert live is config
    assert live.max_tokens == 8192
    assert live.temperature == 0.9
    assert live.api_key == "sk-live-secret"
    assert live.base_url == "https://example.test/v1beta"
    assert live.default_model == "gemini-2.5-pro"


@pytest.mark.asyncio
async def test_event_reports_probe_generation_settings(monkeypatch, caplog):
    diag.reset_state()
    manager, _ = _manager()
    _patch_factory(monkeypatch, _FakeFactory())
    caplog.set_level(logging.INFO, logger=diag.logger.name)

    await diag.run_continuation_probe(manager, "gemini", "gemini-2.5-pro", request_id="rid")

    assert diag.PROBE_MAX_TOKENS == 64
    assert diag.PROBE_TEMPERATURE == 0.0
    events = [
        record.getMessage() for record in caplog.records
        if record.getMessage().startswith("PROVIDER_CONTINUATION_DIAGNOSTIC")
    ]
    assert events
    assert "max_tokens=64" in events[-1]
    assert "temperature=0.0" in events[-1]


@pytest.mark.asyncio
async def test_skipped_event_reports_probe_generation_settings(monkeypatch, caplog):
    diag.reset_state()
    manager, _ = _manager()
    _patch_factory(monkeypatch, _FakeFactory())
    caplog.set_level(logging.DEBUG, logger=diag.logger.name)

    first = diag.schedule_continuation_diagnostic(manager, _failure(), request_id="rid-1")
    assert first is not None
    await first
    assert diag.schedule_continuation_diagnostic(manager, _failure(), request_id="rid-2") is None

    skipped = [
        record.getMessage() for record in caplog.records
        if "result=SKIPPED" in record.getMessage()
    ]
    assert skipped
    assert "max_tokens=64" in skipped[-1]
    assert "temperature=0.0" in skipped[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_reason", ["length", "MAX_TOKENS"])
async def test_output_token_truncation_is_inconclusive(monkeypatch, caplog, finish_reason):
    diag.reset_state()
    manager, _ = _manager()
    _patch_factory(
        monkeypatch,
        _FakeFactory([ProviderResponse(
            # Exactly what each adapter returns for a capped answer: the
            # synthesized sentence plus the provider's own finish reason.
            text="Response truncated due to token limit.",
            provider_name="gemini",
            success=True,
            metadata={"finish_reason": finish_reason},
        )]),
    )
    caplog.set_level(logging.INFO, logger=diag.logger.name)

    result = await diag.run_continuation_probe(
        manager, "gemini", "gemini-2.5-pro", request_id="rid",
    )

    assert result == diag.CONTINUATION_INCONCLUSIVE
    assert "category=output_truncated" in caplog.text
    assert "CONTINUATION_REJECTED" not in caplog.text


@pytest.mark.asyncio
async def test_empty_success_without_finish_reason_stays_rejected(monkeypatch):
    diag.reset_state()
    manager, _ = _manager()
    _patch_factory(
        monkeypatch,
        _FakeFactory([ProviderResponse(text="", provider_name="gemini", success=True, metadata={})]),
    )

    assert await diag.run_continuation_probe(
        manager, "gemini", "gemini-2.5-pro", request_id="rid",
    ) == diag.CONTINUATION_REJECTED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("finish_reason", "text"),
    [
        ("content_filter", "Response blocked by content filter."),
        ("SAFETY", "Response blocked by content filter."),
        ("RECITATION", "Response blocked due to recitation."),
    ],
)
async def test_content_filter_is_not_token_truncation(monkeypatch, finish_reason, text):
    diag.reset_state()
    manager, _ = _manager()
    _patch_factory(
        monkeypatch,
        _FakeFactory([ProviderResponse(
            text=text, provider_name="gemini", success=True,
            metadata={"finish_reason": finish_reason},
        )]),
    )

    result = await diag.run_continuation_probe(
        manager, "gemini", "gemini-2.5-pro", request_id="rid",
    )

    assert result == diag.CONTINUATION_ACCEPTED
    assert result != diag.CONTINUATION_INCONCLUSIVE


@pytest.mark.asyncio
async def test_truncation_sentence_without_finish_reason_is_not_inferred(monkeypatch):
    """Text alone never proves truncation: only the adapter's reason does."""
    diag.reset_state()
    manager, _ = _manager()
    _patch_factory(
        monkeypatch,
        _FakeFactory([ProviderResponse(
            text="Response truncated due to token limit.",
            provider_name="gemini", success=True, metadata={},
        )]),
    )

    assert await diag.run_continuation_probe(
        manager, "gemini", "gemini-2.5-pro", request_id="rid",
    ) == diag.CONTINUATION_ACCEPTED


@pytest.mark.asyncio
async def test_provider_without_finish_reason_keeps_existing_classification(monkeypatch):
    diag.reset_state()
    manager, _ = _manager()
    _patch_factory(
        monkeypatch,
        _FakeFactory([ProviderResponse(text="ok", provider_name="gemini", success=True, metadata={})]),
    )

    assert await diag.run_continuation_probe(
        manager, "gemini", "gemini-2.5-pro", request_id="rid",
    ) == diag.CONTINUATION_ACCEPTED


@pytest.mark.asyncio
async def test_repeated_failure_inside_cooldown_skips_the_probe(monkeypatch, caplog):
    diag.reset_state()
    manager, _ = _manager()
    factory = _patch_factory(monkeypatch, _FakeFactory())
    caplog.set_level(logging.DEBUG, logger=diag.logger.name)

    first = diag.schedule_continuation_diagnostic(manager, _failure(), request_id="rid-1")
    assert first is not None
    await first
    second = diag.schedule_continuation_diagnostic(manager, _failure(), request_id="rid-2")

    assert second is None
    assert len(factory.calls) == 1
    assert "skipped=cooldown" in caplog.text


@pytest.mark.asyncio
async def test_different_provider_model_pairs_have_independent_state(monkeypatch):
    diag.reset_state()
    manager, _ = _manager()
    factory = _patch_factory(monkeypatch, _FakeFactory())

    first = diag.schedule_continuation_diagnostic(
        manager, _failure(model="gemini-2.5-pro"), request_id="rid-1",
    )
    second = diag.schedule_continuation_diagnostic(
        manager, _failure(model="gemini-2.5-flash"), request_id="rid-2",
    )

    assert first is not None and second is not None
    await asyncio.gather(first, second)
    assert len(factory.created) == 2
    # Each probe asks for the model that FAILED, on the same provider config.
    assert [call["kwargs"]["model"] for call in factory.calls] == [
        "gemini-2.5-pro", "gemini-2.5-flash",
    ]
    assert {created[1].api_key for created in factory.created} == {
        manager.get_provider_config("gemini").api_key
    }


@pytest.mark.asyncio
async def test_in_flight_probe_is_not_duplicated(monkeypatch, caplog):
    diag.reset_state()
    manager, _ = _manager()
    factory = _patch_factory(monkeypatch, _FakeFactory(delay=0.05))
    caplog.set_level(logging.DEBUG, logger=diag.logger.name)

    first = diag.schedule_continuation_diagnostic(manager, _failure(), request_id="rid-1")
    second = diag.schedule_continuation_diagnostic(manager, _failure(), request_id="rid-2")

    assert first is not None
    assert second is None
    assert "skipped=in_flight" in caplog.text
    await first
    assert len(factory.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("category", ["auth", "rate_limited", "quota", "model_not_found"])
async def test_non_continuation_failures_never_probe(monkeypatch, category):
    diag.reset_state()
    manager, _ = _manager()
    factory = _patch_factory(monkeypatch, _FakeFactory())

    task = diag.schedule_continuation_diagnostic(
        manager, _failure(category=category), request_id="rid-1",
    )

    assert task is None
    assert factory.created == []


@pytest.mark.asyncio
async def test_response_without_a_failed_matrix_entry_never_probes(monkeypatch):
    diag.reset_state()
    manager, _ = _manager()
    factory = _patch_factory(monkeypatch, _FakeFactory())
    response = ProviderResponse(
        text="ok", provider_name="gemini", success=True,
        metadata={"provider_matrix": [{"provider": "gemini", "model": "m", "outcome": "success"}]},
    )

    assert diag.schedule_continuation_diagnostic(manager, response, request_id="rid") is None
    assert factory.created == []


# ── Classification ──


@pytest.mark.asyncio
async def test_synthetic_continuation_success_is_accepted(monkeypatch):
    diag.reset_state()
    manager, _ = _manager()
    _patch_factory(
        monkeypatch,
        _FakeFactory([ProviderResponse(text="done", provider_name="gemini", success=True)]),
    )

    result = await diag.run_continuation_probe(
        manager, "gemini", "gemini-2.5-pro", request_id="rid",
    )

    assert result == diag.CONTINUATION_ACCEPTED


@pytest.mark.asyncio
async def test_synthetic_continuation_rejection_is_classified(monkeypatch):
    diag.reset_state()
    manager, _ = _manager()
    _patch_factory(
        monkeypatch,
        _FakeFactory([
            ProviderResponse(
                text="API error (400)", provider_name="gemini", success=False,
                metadata={"failure_type": "request", "http_status": 400},
            )
        ]),
    )

    result = await diag.run_continuation_probe(
        manager, "gemini", "gemini-2.5-pro", request_id="rid",
    )

    assert result == diag.CONTINUATION_REJECTED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ({"failure_type": "auth", "http_status": 401}, diag.AUTH_ERROR),
        ({"failure_type": "rate_limited", "http_status": 429}, diag.RATE_LIMITED),
        ({"failure_type": "model_not_found", "http_status": 404}, diag.MODEL_NOT_FOUND),
        ({"failure_type": "timeout"}, diag.TIMEOUT),
        ({"failure_type": "server", "http_status": 500}, diag.UNKNOWN),
        ({"failure_type": "tool_call"}, diag.CONTINUATION_REJECTED),
    ],
)
async def test_probe_classification_matrix(monkeypatch, metadata, expected):
    diag.reset_state()
    manager, _ = _manager()
    _patch_factory(
        monkeypatch,
        _FakeFactory([
            ProviderResponse(text="failed", provider_name="gemini", success=False, metadata=metadata)
        ]),
    )

    assert await diag.run_continuation_probe(
        manager, "gemini", "gemini-2.5-pro", request_id="rid",
    ) == expected


@pytest.mark.asyncio
async def test_probe_timeout_is_reported(monkeypatch):
    diag.reset_state()
    manager, _ = _manager()
    _patch_factory(monkeypatch, _FakeFactory(delay=5.0))
    monkeypatch.setattr(diag, "PROBE_TIMEOUT_SECONDS", 0.01)

    result = await diag.run_continuation_probe(
        manager, "gemini", "gemini-2.5-pro", request_id="rid",
    )

    assert result == diag.TIMEOUT


@pytest.mark.asyncio
async def test_unregistered_provider_is_unsupported(monkeypatch):
    diag.reset_state()
    manager, _ = _manager()
    factory = _patch_factory(monkeypatch, _FakeFactory())

    result = await diag.run_continuation_probe(
        manager, "not-registered", "m", request_id="rid",
    )

    assert result == diag.UNSUPPORTED
    assert factory.created == []


# ── The synthetic contract itself ──


@pytest.mark.asyncio
async def test_multiple_tool_responses_preserve_the_adapter_contract(monkeypatch):
    diag.reset_state()
    manager, _ = _manager()
    factory = _patch_factory(monkeypatch, _FakeFactory())

    await diag.run_continuation_probe(manager, "gemini", "gemini-2.5-pro", request_id="rid")

    call = factory.calls[0]
    messages = call["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "tool"]
    tool_calls = messages[1]["tool_calls"]
    assert len(tool_calls) == 2
    assert all(tc["type"] == "function" for tc in tool_calls)
    assert all(isinstance(tc["function"]["arguments"], str) for tc in tool_calls)
    assert all(json.loads(tc["function"]["arguments"]) for tc in tool_calls)
    assert [m["tool_call_id"] for m in messages[2:]] == [tc["id"] for tc in tool_calls]
    assert all(m["name"] == diag.SYNTHETIC_TOOL_NAME for m in messages[2:])
    assert all(json.loads(m["content"])["success"] is True for m in messages[2:])
    # The same tool definitions production sends with a continuation round.
    assert call["kwargs"]["tools"] == [diag.synthetic_tool_definition()]
    assert call["kwargs"]["model"] == "gemini-2.5-pro"


@pytest.mark.asyncio
async def test_probe_never_executes_a_registered_tool(monkeypatch):
    diag.reset_state()
    manager, _ = _manager()
    _patch_factory(monkeypatch, _FakeFactory())

    async def _explode(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the probe must never execute a tool")

    monkeypatch.setattr(ToolExecutor, "execute_calls", _explode)
    monkeypatch.setattr(ToolExecutor, "execute_confirmed", _explode)

    assert await diag.run_continuation_probe(
        manager, "gemini", "gemini-2.5-pro", request_id="rid",
    ) == diag.CONTINUATION_ACCEPTED


@pytest.mark.asyncio
async def test_probe_carries_no_user_or_telegram_content(monkeypatch):
    diag.reset_state()
    manager, _ = _manager()
    factory = _patch_factory(monkeypatch, _FakeFactory())

    await diag.run_continuation_probe(manager, "gemini", "gemini-2.5-pro", request_id="rid")

    messages = factory.calls[0]["messages"]
    assert messages == diag.build_synthetic_conversation()
    rendered = json.dumps(messages)
    for private in ("Nova", "owner", "chat_id", "telegram", "session"):
        assert private.lower() not in rendered.lower()


# ── Safety and side effects ──


@pytest.mark.asyncio
async def test_probe_failure_cannot_break_the_original_request(monkeypatch):
    diag.reset_state()
    manager, _ = _manager()
    _patch_factory(monkeypatch, _FakeFactory(error=RuntimeError("transport exploded")))

    task = diag.schedule_continuation_diagnostic(manager, _failure(), request_id="rid")

    assert task is not None
    await task  # must complete without raising
    assert diag._cooldown_remaining(("gemini", "gemini-2.5-pro"), _now()) >= 0.0


@pytest.mark.asyncio
async def test_probe_does_not_change_production_health_or_metrics(monkeypatch):
    diag.reset_state()
    manager, _ = _manager()
    _patch_factory(
        monkeypatch,
        _FakeFactory([
            ProviderResponse(
                text="API error (400)", provider_name="gemini", success=False,
                metadata={"failure_type": "request", "http_status": 400},
            )
        ]),
    )
    before_health = manager.health_snapshot()
    before_metrics = manager.metrics_snapshot()

    await diag.run_continuation_probe(manager, "gemini", "gemini-2.5-pro", request_id="rid")

    assert manager.health_snapshot() == before_health
    assert manager.metrics_snapshot() == before_metrics
    assert manager.health_snapshot() == {}
    assert manager.reset_state("gemini")["available"] is True
    assert manager._model_unavailable("gemini", "gemini-2.5-pro") is False


@pytest.mark.asyncio
async def test_probe_log_carries_no_secret_or_private_content(monkeypatch, caplog):
    diag.reset_state()
    manager, config = _manager()
    _patch_factory(
        monkeypatch,
        _FakeFactory([
            ProviderResponse(
                text="API error (400): Authorization: Bearer sk-test-secret-value",
                provider_name="gemini",
                success=False,
                metadata={
                    "failure_type": "request",
                    "http_status": 400,
                    "provider_error_message": "Bearer sk-test-secret-value rejected",
                },
            )
        ]),
    )
    caplog.set_level(logging.DEBUG, logger=diag.logger.name)

    await diag.run_continuation_probe(manager, "gemini", "gemini-2.5-pro", request_id="rid-1")

    assert "PROVIDER_CONTINUATION_DIAGNOSTIC" in caplog.text
    assert config.api_key not in caplog.text
    assert "Bearer" not in caplog.text
    assert "Diagnostic continuation probe." not in caplog.text
    assert "result=CONTINUATION_REJECTED" in caplog.text
    assert "category=request" in caplog.text


@pytest.mark.asyncio
async def test_manager_failure_matrix_carries_the_category(monkeypatch):
    diag.reset_state()
    config = _config("groq", "llama-3.3-70b")
    registry = ProviderRegistry()
    registry.register(_ScriptedProvider(
        "groq",
        config=config,
        responses=[ProviderResponse(
            text="API error (400)", provider_name="groq", success=False,
            metadata={"failure_type": "request", "http_status": 400},
        )],
    ))
    registry.switch_provider("groq")
    manager = ProviderManager(registry)

    response = await manager.chat([{"role": "user", "content": "x"}])

    matrix = response.metadata["provider_matrix"]
    failed = [entry for entry in matrix if entry["outcome"] == "failed"]
    assert failed, matrix
    assert failed[0]["provider"] == "groq"
    assert failed[0]["model"] == "llama-3.3-70b"
    assert failed[0]["category"] == "request"
    # The diagnostic now finds exactly that pair.
    assert diag._qualifying_failure(response) == ("groq", "llama-3.3-70b", "request")


def _now() -> float:
    import time

    return time.monotonic()
