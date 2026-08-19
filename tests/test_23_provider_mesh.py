"""
Focused tests for the AI Provider Mesh reliability upgrade:

- circuit breaker / quarantine after consecutive failures
- per-failure-category cooldown penalties
- capability-based routing (tool-calling mismatch → skip)
- adaptive provider scoring (reliability × latency × streak)
- provider failure matrix in response metadata
- automatic recovery (record_success clears quarantine)
- graceful no-provider failure
- semantic response-quality feedback (HTTP 200 but empty → lower score)

No real API keys or network access required — provider HTTP is mocked.
"""
from __future__ import annotations

from typing import Any

import pytest

from backend.ai.providers.base.capabilities import ProviderCapabilities
from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.base.contract import BaseProvider, ProviderResponse
from backend.ai.providers.manager.health import (
    ProviderHealthState,
    ProviderHealthTracker,
    QUARANTINE_AFTER_FAILURES,
)
from backend.ai.providers.manager.manager import ProviderManager


class _StubProvider(BaseProvider):
    """Scripted provider with configurable capabilities."""

    def __init__(
        self,
        name: str,
        responses: list[ProviderResponse] | None = None,
        caps: ProviderCapabilities | None = None,
    ) -> None:
        super().__init__(ProviderConfig(provider_name=name, enabled=True))
        self._name = name
        self._responses = list(responses or [])
        self._caps = caps
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._caps or ProviderCapabilities(
            supports_tools=True,
            supports_function_call=True,
        )

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> ProviderResponse:
        self.calls += 1
        if self._responses:
            return self._responses.pop(0)
        return ProviderResponse(text="stub ok", provider_name=self._name, success=True)

    def initialize(self) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def health(self) -> dict[str, Any]:
        return {"healthy": True}


def _failure(text: str, **meta: Any) -> ProviderResponse:
    return ProviderResponse(text=text, provider_name="", success=False, metadata=meta)


# ── Circuit breaker / quarantine ──


def test_consecutive_failures_quarantine_provider():
    tracker = ProviderHealthTracker()
    for _ in range(QUARANTINE_AFTER_FAILURES):
        state = tracker.record_failure("x", "server")
    assert state == ProviderHealthState.QUARANTINED
    assert tracker.state("x") == ProviderHealthState.QUARANTINED
    assert not tracker.is_available("x")
    assert tracker.consecutive_failures("x") == QUARANTINE_AFTER_FAILURES


def test_timeout_penalty_is_short():
    tracker = ProviderHealthTracker()
    tracker.record_failure("x", "timeout")
    assert tracker.state("x") == ProviderHealthState.COOLING_DOWN
    assert 0 < tracker.cooldown_remaining("x") <= 30.0


def test_record_success_clears_quarantine():
    tracker = ProviderHealthTracker()
    for _ in range(QUARANTINE_AFTER_FAILURES):
        tracker.record_failure("x", "server")
    assert tracker.state("x") == ProviderHealthState.QUARANTINED
    tracker.record_success("x")
    assert tracker.state("x") == ProviderHealthState.HEALTHY
    assert tracker.consecutive_failures("x") == 0


def test_quarantine_expires_automatically():
    import time as _time

    from unittest.mock import patch

    tracker = ProviderHealthTracker()
    for _ in range(QUARANTINE_AFTER_FAILURES):
        tracker.record_failure("x", "server")
    assert tracker.state("x") == ProviderHealthState.QUARANTINED

    with patch(
        "backend.ai.providers.manager.health.time.monotonic",
        return_value=_time.monotonic() + 601,
    ):
        assert tracker.state("x") == ProviderHealthState.HEALTHY


# ── Capability-based routing ──


@pytest.mark.asyncio
async def test_router_skips_provider_without_tool_calling():
    no_tools = _StubProvider(
        "no_tools",
        caps=ProviderCapabilities(supports_tools=False, supports_function_call=False),
    )
    with_tools = _StubProvider(
        "with_tools",
        [ProviderResponse(text="used", provider_name="with_tools", success=True)],
    )

    pm = ProviderManager()
    pm.register_provider(no_tools)
    pm.register_provider(with_tools)
    pm.switch_provider("no_tools")
    pm._fallback_chain = ["with_tools"]

    response = await pm.chat(
        [{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "x", "parameters": {}}}],
    )

    assert response.success is True
    assert response.provider_name == "with_tools"
    assert no_tools.calls == 0, "tool-incapable provider must be skipped, not called"


# ── Failure matrix ──


@pytest.mark.asyncio
async def test_failure_matrix_metadata_on_fallback():
    rate = _StubProvider("rate", [
        _failure("Rate limited.", http_status=429, retry_after=60, failure_type="rate_limited"),
    ])
    backup = _StubProvider("backup", [
        ProviderResponse(text="ok", provider_name="backup", success=True),
    ])

    pm = ProviderManager()
    pm.register_provider(rate)
    pm.register_provider(backup)
    pm.switch_provider("rate")
    pm._fallback_chain = ["backup"]

    response = await pm.chat([{"role": "user", "content": "hi"}])

    matrix = response.metadata.get("provider_matrix", [])
    assert any(m["provider"] == "rate" and m["outcome"] == "failed" for m in matrix)
    assert any(m["provider"] == "backup" and m["outcome"] == "success" for m in matrix)
    assert response.metadata.get("fallback") is True


# ── Safety net: other registered providers are eligible ──


@pytest.mark.asyncio
async def test_router_tries_other_registered_providers_without_chain():
    active = _StubProvider("active", [
        _failure("boom", http_status=500, failure_type="server"),
        _failure("boom2", http_status=500, failure_type="server"),
    ])
    other = _StubProvider("other", [
        ProviderResponse(text="ok", provider_name="other", success=True),
    ])

    pm = ProviderManager()
    pm.register_provider(active)
    pm.register_provider(other)
    pm.switch_provider("active")
    pm._fallback_chain = []  # no configured chain

    response = await pm.chat([{"role": "user", "content": "hi"}])

    assert response.success is True
    assert response.provider_name == "other"


# ── Graceful no-provider failure ──


@pytest.mark.asyncio
async def test_all_providers_exhausted_graceful_failure():
    active = _StubProvider("active", [
        _failure("bad key", http_status=401, failure_type="auth"),
    ])

    pm = ProviderManager()
    pm.register_provider(active)
    pm.switch_provider("active")
    pm._fallback_chain = []

    response = await pm.chat([{"role": "user", "content": "hi"}])

    assert response.success is False
    assert "All AI providers failed" in response.text
    assert response.metadata.get("fallback_exhausted") is True


# ── Adaptive scoring ──


@pytest.mark.asyncio
async def test_scoring_prefers_higher_success_rate_when_active_down():
    good = _StubProvider("good", [
        ProviderResponse(text="ok", provider_name="good", success=True),
    ])
    bad = _StubProvider("bad", [
        ProviderResponse(text="ok", provider_name="bad", success=True),
    ])
    active = _StubProvider("active", [])

    pm = ProviderManager()
    pm.register_provider(active)
    pm.register_provider(bad)
    pm.register_provider(good)
    pm.switch_provider("active")
    pm._health.mark_cooling_down("active", 100)
    pm._fallback_chain = ["bad", "good"]

    # Seed metrics: bad has 50% success, good has 100%.
    pm._metrics.record("bad", latency=0.5, error="")
    pm._metrics.record("bad", latency=0.5, error="boom")
    pm._metrics.record("good", latency=0.5, error="")

    response = await pm.chat([{"role": "user", "content": "hi"}])

    assert response.success is True
    assert response.provider_name == "good"
    assert bad.calls == 0, "lower-scoring healthy provider must not be tried first"


# ── Semantic response-quality feedback ──


@pytest.mark.asyncio
async def test_empty_response_with_tools_records_quality_failure():
    empty = _StubProvider("empty", [
        ProviderResponse(text="", provider_name="empty", success=True),
    ])

    pm = ProviderManager()
    pm.register_provider(empty)
    pm.switch_provider("empty")

    await pm.chat(
        [{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "x", "parameters": {}}}],
    )

    metrics = pm.metrics_snapshot()["empty"]
    assert metrics["quality_requests"] == 1
    assert metrics["quality_failures"] == 1
    # A single empty response must NOT cool the provider down.
    assert pm._health.is_available("empty") is True


# ── Unknown provider is not registered / no crash ──


def test_unknown_provider_raises_provider_not_found():
    from backend.ai.providers.base.exceptions import ProviderNotFound
    from backend.ai.providers.factory import ProviderFactory

    with pytest.raises(ProviderNotFound):
        ProviderFactory.create_provider("future_provider_x")


# ── Provider failover boundary (this task) ──


class _CaptureProvider(_StubProvider):
    """Stub that records the exact messages it was asked to process."""

    def __init__(self, name: str) -> None:
        super().__init__(name, [ProviderResponse(text="ok", provider_name=name, success=True)])
        self.last_messages: list[dict[str, Any]] | None = None

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> ProviderResponse:
        self.last_messages = list(messages)
        return await super().chat(messages, **kwargs)


@pytest.mark.asyncio
async def test_primary_provider_succeeds_without_fallback():
    active = _StubProvider("active", [
        ProviderResponse(text="ok", provider_name="active", success=True),
    ])
    backup = _StubProvider("backup", [
        ProviderResponse(text="should not be used", provider_name="backup", success=True),
    ])

    pm = ProviderManager()
    pm.register_provider(active)
    pm.register_provider(backup)
    pm.switch_provider("active")
    pm._fallback_chain = ["backup"]

    response = await pm.chat([{"role": "user", "content": "hi"}])

    assert response.success is True
    assert response.provider_name == "active"
    assert response.metadata.get("fallback") is not True
    assert backup.calls == 0


@pytest.mark.asyncio
async def test_timeout_fails_over_to_fallback_after_one_retry():
    slow = _StubProvider("slow", [
        _failure("timeout 1", failure_type="timeout"),
        _failure("timeout 2", failure_type="timeout"),
    ])
    backup = _StubProvider("backup", [
        ProviderResponse(text="ok", provider_name="backup", success=True),
    ])

    pm = ProviderManager()
    pm.register_provider(slow)
    pm.register_provider(backup)
    pm.switch_provider("slow")
    pm._fallback_chain = ["backup"]

    response = await pm.chat([{"role": "user", "content": "hi"}])

    assert response.success is True
    assert response.provider_name == "backup"
    # exactly one bounded retry on the failing provider, then failover
    assert slow.calls == 2
    assert backup.calls == 1
    assert pm._health.state("slow") == ProviderHealthState.COOLING_DOWN


@pytest.mark.asyncio
async def test_server_503_fails_over_to_fallback():
    server = _StubProvider("server", [
        _failure("503", http_status=503, failure_type="server"),
        _failure("503 again", http_status=503, failure_type="server"),
    ])
    backup = _StubProvider("backup", [
        ProviderResponse(text="ok", provider_name="backup", success=True),
    ])

    pm = ProviderManager()
    pm.register_provider(server)
    pm.register_provider(backup)
    pm.switch_provider("server")
    pm._fallback_chain = ["backup"]

    response = await pm.chat([{"role": "user", "content": "hi"}])

    assert response.success is True
    assert response.provider_name == "backup"
    assert server.calls == 2


@pytest.mark.asyncio
async def test_model_not_found_fails_over_without_retrying_dead_pair():
    dead = _StubProvider("dead", [
        _failure("model not found", http_status=404, failure_type="model_not_found", model="stale-model"),
    ])
    backup = _StubProvider("backup", [
        ProviderResponse(text="ok", provider_name="backup", success=True),
    ])

    pm = ProviderManager()
    pm.register_provider(dead)
    pm.register_provider(backup)
    pm.switch_provider("dead")
    pm._fallback_chain = ["backup"]

    response = await pm.chat([{"role": "user", "content": "hi"}])

    assert response.success is True
    assert response.provider_name == "backup"
    assert dead.calls == 1, "model_not_found must never be retried"
    assert ("dead", "stale-model") in pm._unavailable_models


@pytest.mark.asyncio
async def test_empty_response_fails_over_to_next_provider():
    empty = _StubProvider("empty", [
        ProviderResponse(text="", provider_name="empty", success=True),
    ])
    backup = _StubProvider("backup", [
        ProviderResponse(text="ok", provider_name="backup", success=True),
    ])

    pm = ProviderManager()
    pm.register_provider(empty)
    pm.register_provider(backup)
    pm.switch_provider("empty")
    pm._fallback_chain = ["backup"]

    response = await pm.chat(
        [{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "x", "parameters": {}}}],
    )

    assert response.success is True
    assert response.provider_name == "backup"
    assert empty.calls == 1
    # empty output is a request-level quality signal, NOT a health failure
    assert pm._health.is_available("empty") is True
    matrix = response.metadata.get("provider_matrix", [])
    assert any(m["provider"] == "empty" and m["outcome"] == "empty_response" for m in matrix)


@pytest.mark.asyncio
async def test_all_malformed_tool_calls_fail_over():
    malformed = _StubProvider("malformed", [
        ProviderResponse(
            text="", provider_name="malformed", success=True,
            tool_calls=[{
                "id": "t1", "name": "save", "arguments": {},
                "malformed_arguments": True, "arguments_error": "bad json",
            }],
        ),
    ])
    backup = _StubProvider("backup", [
        ProviderResponse(text="ok", provider_name="backup", success=True),
    ])

    pm = ProviderManager()
    pm.register_provider(malformed)
    pm.register_provider(backup)
    pm.switch_provider("malformed")
    pm._fallback_chain = ["backup"]

    response = await pm.chat(
        [{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "x", "parameters": {}}}],
    )

    assert response.success is True
    assert response.provider_name == "backup"
    matrix = response.metadata.get("provider_matrix", [])
    assert any(m["provider"] == "malformed" and m["outcome"] == "structured_output" for m in matrix)


@pytest.mark.asyncio
async def test_primary_and_fallback_both_fail_clean_error():
    a = _StubProvider("a", [_failure("429", http_status=429, retry_after=60, failure_type="rate_limited")])
    b = _StubProvider("b", [_failure("429", http_status=429, retry_after=60, failure_type="rate_limited")])

    pm = ProviderManager()
    pm.register_provider(a)
    pm.register_provider(b)
    pm.switch_provider("a")
    pm._fallback_chain = ["b"]

    response = await pm.chat([{"role": "user", "content": "hi"}])

    assert response.success is False
    assert "All AI providers failed" in response.text
    assert response.metadata.get("fallback_exhausted") is True
    matrix = response.metadata.get("provider_matrix", [])
    assert sum(1 for m in matrix if m["outcome"] == "failed") == 2


@pytest.mark.asyncio
async def test_auth_failure_does_not_retry_and_disables_provider():
    auth = _StubProvider("auth", [_failure("401 invalid key", http_status=401, failure_type="auth")])

    pm = ProviderManager()
    pm.register_provider(auth)
    pm.switch_provider("auth")

    response = await pm.chat([{"role": "user", "content": "hi"}])

    assert response.success is False
    assert auth.calls == 1, "auth failures must never be retried"
    assert pm._health.state("auth") == ProviderHealthState.DISABLED


@pytest.mark.asyncio
async def test_cooldown_prevents_immediate_repeated_calls():
    rate = _StubProvider("rate", [
        _failure("429", http_status=429, retry_after=60, failure_type="rate_limited"),
        ProviderResponse(text="ok", provider_name="rate", success=True),
    ])

    pm = ProviderManager()
    pm.register_provider(rate)
    pm.switch_provider("rate")

    first = await pm.chat([{"role": "user", "content": "hi"}])
    assert first.success is False
    assert pm._health.state("rate") == ProviderHealthState.COOLING_DOWN

    # Second request while cooling down: the provider is skipped entirely.
    second = await pm.chat([{"role": "user", "content": "hi"}])
    assert second.success is False
    assert rate.calls == 1


@pytest.mark.asyncio
async def test_success_after_cooldown_expiry_clears_failure_state():
    import time as _time

    from unittest.mock import patch

    rate = _StubProvider("rate", [
        _failure("429", http_status=429, retry_after=60, failure_type="rate_limited"),
        ProviderResponse(text="ok", provider_name="rate", success=True),
    ])

    pm = ProviderManager()
    pm.register_provider(rate)
    pm.switch_provider("rate")

    await pm.chat([{"role": "user", "content": "hi"}])
    assert pm._health.state("rate") == ProviderHealthState.COOLING_DOWN

    with patch(
        "backend.ai.providers.manager.health.time.monotonic",
        return_value=_time.monotonic() + 61,
    ):
        recovered = await pm.chat([{"role": "user", "content": "hi"}])

    assert recovered.success is True
    assert pm._health.state("rate") == ProviderHealthState.HEALTHY
    assert pm._health.consecutive_failures("rate") == 0


@pytest.mark.asyncio
async def test_active_provider_priority_is_deterministic():
    active = _StubProvider("active", [
        ProviderResponse(text="ok", provider_name="active", success=True),
    ])
    fallback = _StubProvider("fallback", [
        ProviderResponse(text="ok", provider_name="fallback", success=True),
    ])

    pm = ProviderManager()
    pm.register_provider(active)
    pm.register_provider(fallback)
    pm.switch_provider("active")
    pm._fallback_chain = ["fallback"]

    response = await pm.chat([{"role": "user", "content": "hi"}])

    # The user's configured provider is always tried first while healthy,
    # even when a fallback could score identically.
    assert response.provider_name == "active"
    assert fallback.calls == 0


@pytest.mark.asyncio
async def test_failover_preserves_original_messages_persian_intent():
    fail = _StubProvider("fail", [_failure("429", http_status=429, retry_after=60, failure_type="rate_limited")])
    capture = _CaptureProvider("backup")

    pm = ProviderManager()
    pm.register_provider(fail)
    pm.register_provider(capture)
    pm.switch_provider("fail")
    pm._fallback_chain = ["backup"]

    messages = [{"role": "user", "content": "ده پیام آخر رو پاک کن"}]
    response = await pm.chat(messages, tools=[{"type": "function", "function": {"name": "x", "parameters": {}}}])

    assert response.success is True
    assert response.provider_name == "backup"
    # The SAME logical request (Persian intent, chat context) is resent —
    # failover never alters the user's intended action.
    assert capture.last_messages == messages


@pytest.mark.asyncio
async def test_unknown_tool_from_provider_rejected_by_executor():
    """Failover can never let a provider execute an unregistered tool:
    unknown tool names are rejected deterministically by the ToolExecutor
    (the only component allowed to call tools)."""
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.executor import ToolExecutor
    from backend.ai.tools.registry import create_default_registry

    ctx = ToolContext(telegram=None, owner_id=1, tz_str="UTC")
    executor = ToolExecutor(create_default_registry(ctx), ctx)

    results = await executor.execute_calls(
        [{"name": "send_money_to_anyone", "arguments": {}}],
        owner_id=1,
    )

    assert len(results) == 1
    assert results[0].success is False
    assert results[0].error == "not_found"
