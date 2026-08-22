"""
TASK 41 — Provider reset / cooldown surfacing.

Verifies the read-only ``reset_state`` accessor (never fabricates
quota/reset times — only the tracker's proven cooldown/quarantine state),
that the existing bounded Retry-After behavior is untouched (short
window = one retry, long window = immediate failover, clamped cooldown),
and that the Health panel surfaces honest recovery information
edit-in-place with zero new messages.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.ai.engine.telemetry import telemetry


@pytest.fixture(autouse=True)
def _reset_telemetry():
    telemetry.reset_for_tests()
    yield
    telemetry.reset_for_tests()


class _Scripted:
    """Provider returning queued responses (the last one repeats forever)."""

    PROVIDER_VERSION = "1.0.0"

    def __init__(self, name, queue):
        from backend.ai.providers.base.config import ProviderConfig
        from backend.ai.providers.base.contract import ProviderResponse

        self._cls = ProviderResponse
        self._name = name
        self.config = ProviderConfig(provider_name=name, enabled=True, default_model=f"{name}-m")
        self._queue = [
            {
                "success": item["success"],
                "text": item.get("text", "ok" if item["success"] else ""),
                "metadata": dict(item.get("metadata") or {}),
            }
            for item in queue
        ]
        self.calls = 0

    @property
    def name(self):
        return self._name

    def initialize(self):
        pass

    def shutdown(self):
        pass

    async def chat(self, messages, **kwargs):
        self.calls += 1
        idx = min(self.calls - 1, len(self._queue) - 1)
        item = self._queue[idx]
        usage = (
            {"prompt_tokens": 40, "completion_tokens": 8, "total_tokens": 48}
            if item["success"] else {}
        )
        return self._cls(
            text=item["text"], provider_name=self.name,
            success=item["success"], usage=usage, metadata=dict(item["metadata"]),
        )

    def count_tokens(self, text: str) -> int:
        return len(text) // 4

    def health(self) -> dict:
        return {"healthy": True, "provider": self.name}


_OK = {"success": True}
_FAIL_RL_SHORT = {"success": False, "text": "429 too many",
                  "metadata": {"failure_type": "rate_limited", "retry_after": 2.0}}
_FAIL_RL_LONG = {"success": False, "text": "429 too many",
                 "metadata": {"failure_type": "rate_limited", "retry_after": 600.0}}
_FAIL_TIMEOUT = {"success": False, "text": "timeout",
                 "metadata": {"failure_type": "timeout"}}


def _manager(*providers):
    from backend.ai.providers.manager.manager import ProviderManager

    pm = ProviderManager()
    for p in providers:
        pm.register_provider(p)
    if providers:
        pm.switch_provider(providers[0].name)
    return pm


# ── 1. reset_state accessor ──


def test_reset_state_reports_rate_limit_cooldown_honestly():
    from backend.ai.providers.manager.health import (
        ProviderHealthState, ProviderHealthTracker,
    )

    tracker = ProviderHealthTracker()
    tracker.record_failure("rl", category="rate_limited", retry_after=45.0)

    assert tracker.state("rl") == ProviderHealthState.COOLING_DOWN
    assert tracker.last_failure_category("rl") == "rate_limited"
    remaining = tracker.cooldown_remaining("rl")
    assert 0 < remaining <= 45.0

    # Success clears the reason and the cooldown.
    tracker.record_success("rl")
    assert tracker.is_available("rl") is True
    assert tracker.last_failure_category("rl") == ""


def test_reset_state_manager_accessor_shape_and_no_fabrication():
    from backend.ai.providers.manager.manager import ProviderManager
    from backend.ai.providers.manager.health import ProviderHealthTracker

    pm = ProviderManager()
    pm._health = ProviderHealthTracker()
    pm._health.record_failure("gemini", category="rate_limited", retry_after=60.0)

    state = pm.reset_state("gemini")
    assert state["provider"] == "gemini"
    assert state["available"] is False
    assert state["reason"] == "rate_limited"
    assert 0 < state["cooldown_remaining_s"] <= 60.0
    assert state["quarantine_remaining_s"] == 0.0
    # Never fabricate quota/credit reset windows — only the documented keys.
    assert set(state) == {"provider", "available", "state", "reason",
                          "cooldown_remaining_s", "quarantine_remaining_s"}


def test_reset_state_unknown_or_healthy_provider_is_available():
    from backend.ai.providers.manager.manager import ProviderManager

    pm = ProviderManager()
    state = pm.reset_state("never-seen")
    assert state["available"] is True
    assert state["cooldown_remaining_s"] == 0.0
    assert state["reason"] == ""


# ── 2. Existing retry/failover behavior unchanged ──


@pytest.mark.asyncio
async def test_short_retry_after_still_gets_one_bounded_retry():
    pm = _manager(_Scripted("rl-short", [_FAIL_RL_SHORT, _OK]))
    response = await pm.chat([{"role": "user", "content": "hi"}])

    assert pm.registry.get("rl-short").calls == 2  # exactly one bounded retry
    assert response.success is True
    assert response.metadata.get("ai_retry_count") == 1
    # Recovery cleared the cooldown and the reason.
    assert pm.reset_state("rl-short")["available"] is True


@pytest.mark.asyncio
async def test_long_retry_after_still_fails_over_without_waiting():
    p = _Scripted("rl-long", [_FAIL_RL_LONG])
    pm = _manager(p)
    response = await pm.chat([{"role": "user", "content": "hi"}])

    assert p.calls == 1  # long window: never stalls, no retry
    assert response.success is False
    state = pm.reset_state("rl-long")
    assert state["available"] is False
    assert state["reason"] == "rate_limited"
    # Cooldown is clamped to the documented maximum, never larger.
    assert state["cooldown_remaining_s"] <= 300.0


@pytest.mark.asyncio
async def test_fallback_and_retry_counts_still_correct():
    """A transient failure still gets exactly ONE bounded retry, and the
    recovery clears the cooldown reason so the provider is fully trusted.
    (Fallback relationships themselves stay pinned by test_35/17.)"""
    pm = _manager(_Scripted("primary", [_FAIL_TIMEOUT, _OK]))
    response = await pm.chat([{"role": "user", "content": "hi"}])

    assert response.success is True
    assert response.metadata.get("ai_retry_count") == 1  # one bounded retry
    assert response.provider_name == "primary"
    assert pm.reset_state("primary")["available"] is True
    assert pm.reset_state("primary")["reason"] == ""


# ── 3. Health panel surfacing ──


class _RecoveryEngine:
    """Minimal engine exposing a provider_manager with reset_state."""

    def __init__(self, reset_state: dict):
        self.provider_manager = MagicMock()
        self.provider_manager.reset_state.return_value = reset_state


@pytest.mark.asyncio
async def test_health_panel_shows_rate_limit_recovery_line():
    from backend.bot.handlers import ai as ai_module

    engine = _RecoveryEngine({
        "provider": "openrouter",
        "available": False,
        "state": "cooling_down",
        "reason": "rate_limited",
        "cooldown_remaining_s": 45.0,
        "quarantine_remaining_s": 0.0,
    })
    config = {"provider": "openrouter", "model": "gpt-5"}
    with patch.object(ai_module, "_get_saved_config", AsyncMock(return_value=config)), \
         patch.object(ai_module, "_get_engine", return_value=engine), \
         patch.object(ai_module, "_get_engine_info",
                      return_value={"provider": "openrouter", "model": "gpt-5",
                                    "connected": False}), \
         patch.object(ai_module, "_discover", AsyncMock(return_value=[])):
        title, body, buttons = await ai_module._ai_health_panel_handler(None, "")

    assert title == "AI · Health"
    assert "Rate limited · retry in ~45s" in body


@pytest.mark.asyncio
async def test_health_panel_never_fabricates_reset_time():
    from backend.bot.handlers import ai as ai_module

    engine = _RecoveryEngine({
        "provider": "openrouter",
        "available": True,
        "state": "healthy",
        "reason": "",
        "cooldown_remaining_s": 0.0,
        "quarantine_remaining_s": 0.0,
    })
    config = {"provider": "openrouter", "model": "gpt-5"}
    with patch.object(ai_module, "_get_saved_config", AsyncMock(return_value=config)), \
         patch.object(ai_module, "_get_engine", return_value=engine), \
         patch.object(ai_module, "_get_engine_info",
                      return_value={"provider": "openrouter", "model": "gpt-5",
                                    "connected": True}), \
         patch.object(ai_module, "_discover", AsyncMock(return_value=[])):
        title, body, buttons = await ai_module._ai_health_panel_handler(None, "")

    assert title == "AI · Health"
    assert "retry in ~" not in body
    assert "Rate limited" not in body


@pytest.mark.asyncio
async def test_health_panel_survives_non_dict_recovery():
    """MagicMock engines (as in the pinned healthy-state test) stay safe."""
    from backend.bot.handlers import ai as ai_module

    telemetry.record_execution(
        __import__("backend.ai.engine.result", fromlist=["EngineResult"]).EngineResult(
            success=True, latency=1.4, total_tokens=2400,
            metadata={"token_source": "actual", "retry_count": 0, "fallback_used": False},
        ), 1,
    )
    config = {"provider": "openrouter", "model": "gpt-5"}
    with patch.object(ai_module, "_get_saved_config", AsyncMock(return_value=config)), \
         patch.object(ai_module, "_get_engine", return_value=MagicMock()), \
         patch.object(ai_module, "_get_engine_info",
                      return_value={"provider": "openrouter", "model": "gpt-5",
                                    "connected": True}), \
         patch.object(ai_module, "_discover", AsyncMock(return_value=[])):
        title, body, buttons = await ai_module._ai_health_panel_handler(None, "")

    assert title == "AI · Health"
    assert "AI is healthy" in body
    assert "retry in ~" not in body


def test_format_cooldown_human():
    from backend.bot.handlers import ai as ai_module

    assert ai_module._format_cooldown(45.0) == "45s"
    assert ai_module._format_cooldown(150.0) == "3m"
    assert ai_module._format_cooldown(0.5) == "1s"
    assert ai_module._format_cooldown(600.0) == "10m"
