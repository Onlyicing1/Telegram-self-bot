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
