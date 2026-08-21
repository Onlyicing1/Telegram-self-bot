"""
TASK 35 — Retry system + AI UX polish regression tests.

  1. Failure classification bounds retries: permanent errors never retry,
     transient errors get exactly ONE bounded retry.
  2. A short provider Retry-After is honored with one bounded retry; a long
     one never stalls the request.
  3. Retry counts survive the fallback chain — the winning candidate's
     response carries the whole request's recovery effort.
  4. The terminal failure preserves retries + failures (never fake success).
  5. The user-facing failure notice is human (no HTTP codes, no tracebacks)
     and reports retries/backup recovery on one compact line.
  6. The Health panel renders the three honest states with causes.
  7. End-to-end: a rate-limited primary recovered by a backup records
     EXACTLY ONE telemetry entry with true retry/fallback facts.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.ai.engine.result import EngineResult
from backend.ai.engine.telemetry import telemetry


@pytest.fixture(autouse=True)
def _reset_telemetry():
    telemetry.reset_for_tests()
    yield
    telemetry.reset_for_tests()


# ── Scripted provider machinery ──


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


_FAIL_REQUEST = {"success": False, "text": "invalid argument", "metadata": {"failure_type": "request"}}
_FAIL_AUTH = {"success": False, "text": "401 unauthorized", "metadata": {"failure_type": "auth"}}
_FAIL_NETWORK = {"success": False, "text": "connection reset", "metadata": {"failure_type": "network"}}
_FAIL_SERVER = {"success": False, "text": "500 oops", "metadata": {"failure_type": "server"}}
_OK = {"success": True}


def _manager(*providers):
    from backend.ai.providers.manager.manager import ProviderManager

    pm = ProviderManager()
    for p in providers:
        pm.register_provider(p)
    if providers:
        pm.switch_provider(providers[0].name)
    return pm


@pytest.fixture()
def no_wait(monkeypatch):
    """Track asyncio.sleep waits without actually sleeping."""
    waits: list[float] = []

    async def fake_sleep(delay, *a, **k):
        waits.append(float(delay))

    monkeypatch.setattr("asyncio.sleep", fake_sleep)
    return waits


# ── 1. Classification bounds ──


@pytest.mark.asyncio
async def test_non_retryable_failure_never_retries():
    pm = _manager(_Scripted("perm", [_FAIL_REQUEST]))
    response = await pm.chat([{"role": "user", "content": "hi"}])

    assert pm.registry.get("perm").calls == 1
    assert response.success is False
    assert response.metadata.get("fallback_exhausted") is True


@pytest.mark.asyncio
async def test_auth_failure_is_not_retried_and_disables_provider():
    p = _Scripted("locked", [_FAIL_AUTH])
    pm = _manager(p)
    response = await pm.chat([{"role": "user", "content": "hi"}])

    assert p.calls == 1
    assert response.success is False
    assert pm.health_snapshot().get("locked", {}).get("state") == "disabled"


@pytest.mark.asyncio
async def test_transient_failure_retries_once_then_succeeds(no_wait):
    p = _Scripted("flaky-net", [_FAIL_NETWORK, _OK])
    pm = _manager(p)
    response = await pm.chat([{"role": "user", "content": "hi"}])

    assert p.calls == 2
    assert response.success is True
    assert response.metadata.get("ai_retry_count") == 1


@pytest.mark.asyncio
async def test_transient_double_failure_reports_the_retry(no_wait):
    p = _Scripted("down", [_FAIL_NETWORK, _FAIL_SERVER])
    pm = _manager(p)
    response = await pm.chat([{"role": "user", "content": "hi"}])

    assert p.calls == 2
    assert response.success is False
    # The retry DID happen — the terminal response must say so.
    assert response.metadata.get("ai_retry_count") == 1
    assert response.metadata.get("fallback_exhausted") is True


# ── 2. Rate limits honor a SHORT Retry-After only ──


@pytest.mark.asyncio
async def test_short_rate_limit_window_waits_and_retries_once(no_wait):
    rl = {
        "success": False, "text": "429",
        "metadata": {"failure_type": "rate_limited", "retry_after": 2},
    }
    p = _Scripted("rl-short", [rl, _OK])
    pm = _manager(p)
    response = await pm.chat([{"role": "user", "content": "hi"}])

    assert p.calls == 2
    assert response.success is True
    assert response.metadata.get("ai_retry_count") == 1
    # The wait honored the provider's own Retry-After.
    assert any(abs(w - 2.0) < 1e-9 for w in no_wait)


@pytest.mark.asyncio
async def test_long_rate_limit_window_does_not_stall_the_request(no_wait):
    rl = {
        "success": False, "text": "429",
        "metadata": {"failure_type": "rate_limited", "retry_after": 120},
    }
    p = _Scripted("rl-long", [rl])
    pm = _manager(p)
    response = await pm.chat([{"role": "user", "content": "hi"}])

    assert p.calls == 1
    assert no_wait == []  # never waited out a long window
    # Classified as rate_limited: the provider is cooling down (Retry-After
    # honored by the health tracker) instead of being hammered.
    state = pm.health_snapshot().get("rl-long", {})
    assert state.get("state") == "cooling_down"


@pytest.mark.asyncio
async def test_rate_limit_without_retry_after_fails_over_immediately(no_wait):
    rl = {"success": False, "text": "429", "metadata": {"failure_type": "rate_limited"}}
    backup = _Scripted("backup-ok", [_OK])
    primary = _Scripted("primary-rl", [rl])
    pm = _manager(primary, backup)
    response = await pm.chat([{"role": "user", "content": "hi"}])

    assert no_wait == []
    assert primary.calls == 1
    assert response.success is True
    assert response.metadata.get("fallback_to") == "backup-ok"


# ── 3. Retry counts survive the fallback chain ──


@pytest.mark.asyncio
async def test_fallback_success_carries_accumulated_retries(no_wait):
    broken = _Scripted("broken", [_FAIL_NETWORK, _FAIL_SERVER])  # retries once, still dies
    healthy = _Scripted("healthy", [_OK])
    pm = _manager(broken, healthy)
    response = await pm.chat([{"role": "user", "content": "hi"}])

    assert response.success is True
    assert response.metadata.get("fallback") is True
    assert response.metadata.get("fallback_from") == "broken"
    assert response.metadata.get("fallback_to") == "healthy"
    # broken burned its one bounded retry before the failover — the winning
    # response preserves that fact for telemetry.
    assert response.metadata.get("ai_retry_count") == 1
    assert broken.calls == 2 and healthy.calls == 1


# ── 4. Terminal failure honesty ──


@pytest.mark.asyncio
async def test_terminal_failure_preserves_per_provider_retries_and_errors(no_wait):
    a = _Scripted("ta", [_FAIL_NETWORK, _FAIL_SERVER])
    b = _Scripted("tb", [_FAIL_NETWORK, _FAIL_SERVER])
    pm = _manager(a, b)
    response = await pm.chat([{"role": "user", "content": "hi"}])

    assert response.success is False
    assert response.metadata.get("ai_retry_count") == 2  # both burned their bounded retry
    errors_text = " ".join(response.metadata.get("errors", []))
    assert "ta" in errors_text and "tb" in errors_text
    assert "All AI providers failed" in response.text


# ── 5. Human failure notice (delivery layer) ──


def _failed_result(metadata, errors=None):
    return EngineResult(
        success=False, provider="gemini", model="m",
        latency=7.7, errors=errors or [], metadata=metadata,
    )


def test_failure_notice_translates_classification_without_internals():
    from backend.bot.handlers.ai_unified import _failure_notice

    notice = _failure_notice(_failed_result(
        {"failure_type": "rate_limited", "retry_count": 1, "fallback_used": True},
        errors=["All AI providers failed. Last error: gemini: HTTP 429"],
    ))
    assert "Couldn't get a response" in notice
    assert "Rate limited" in notice
    assert "1 retry" in notice
    assert "backup tried" in notice
    assert "429" not in notice
    assert "HTTP" not in notice


def test_failure_notice_pluralizes_and_skips_silent_recovery():
    from backend.bot.handlers.ai_unified import _failure_notice

    notice = _failure_notice(_failed_result(
        {"failure_type": "timeout", "retry_count": 2, "fallback_used": False},
    ))
    assert "Timeout" in notice
    assert "2 retries" in notice
    assert "backup" not in notice


def test_failure_notice_keeps_configuration_hint_for_auth():
    from backend.bot.handlers.ai_unified import _failure_notice

    notice = _failure_notice(_failed_result({"failure_type": "auth"}))
    assert "Sign-in failed" in notice
    assert "API key" in notice


def test_failure_notice_legacy_path_still_humanizes_raw_error():
    from backend.bot.handlers.ai_unified import _failure_notice

    notice = _failure_notice(_failed_result({}, errors=["connection reset by peer"]))
    assert "temporarily unavailable" in notice


def test_format_failure_keeps_message_hierarchy():
    from backend.bot.handlers.ai_unified import _format_failure

    text = _format_failure("prompt", "Nova", "✕ Couldn't get a response\nTimeout")
    assert text.startswith("prompt\n────────────\n🤖 Nova\n")
    assert "❌ Error" not in text


# ── 6. Health panel states ──


@pytest.mark.asyncio
async def test_health_panel_healthy_state():
    from backend.bot.handlers import ai as ai_module

    telemetry.record_execution(
        EngineResult(success=True, latency=1.4, total_tokens=2400,
                     metadata={"token_source": "actual", "retry_count": 0, "fallback_used": False}),
        1,
    )
    config = {"provider": "openrouter", "model": "gpt-5"}
    with patch.object(ai_module, "_get_saved_config", AsyncMock(return_value=config)), \
         patch.object(ai_module, "_get_engine", return_value=MagicMock()), \
         patch.object(ai_module, "_get_engine_info",
                      return_value={"provider": "openrouter", "model": "gpt-5", "connected": True}), \
         patch.object(ai_module, "_discover", AsyncMock(return_value=[])):
        title, body, buttons = await ai_module._ai_health_panel_handler(None, "")

    assert title == "AI · Health"
    assert "AI is healthy" in body
    assert "gpt-5 · OpenRouter" in body
    assert "Last response · 1.4s" in body


@pytest.mark.asyncio
async def test_health_panel_degraded_with_cause():
    from backend.bot.handlers import ai as ai_module

    telemetry.record_execution(
        EngineResult(success=False, errors=["timed out"],
                     metadata={"token_source": "unavailable", "failure_type": "timeout"}),
        1,
    )
    config = {"provider": "gemini", "model": "gemini-2.5-flash"}
    with patch.object(ai_module, "_get_saved_config", AsyncMock(return_value=config)), \
         patch.object(ai_module, "_get_engine", return_value=MagicMock()), \
         patch.object(ai_module, "_get_engine_info",
                      return_value={"provider": "gemini", "model": "gemini-2.5-flash", "connected": False}), \
         patch.object(ai_module, "_discover", AsyncMock(return_value=[])):
        title, body, buttons = await ai_module._ai_health_panel_handler(None, "")

    assert "AI is degraded" in body
    assert "Provider unreachable" in body


@pytest.mark.asyncio
async def test_health_panel_offline_names_missing_configuration():
    from backend.bot.handlers import ai as ai_module

    with patch.object(ai_module, "_get_saved_config", AsyncMock(return_value={"provider": "", "model": ""})), \
         patch.object(ai_module, "_get_engine", return_value=None), \
         patch.object(ai_module, "_get_engine_info",
                      return_value={"provider": "—", "model": "—", "connected": False}):
        title, body, buttons = await ai_module._ai_health_panel_handler(None, "")

    assert "AI is offline" in body
    assert "No provider configured" in body


# ── 7. End-to-end: rate-limited primary → backup, exactly-one record ──


@pytest.mark.asyncio
async def test_rate_limited_primary_recovered_by_backup_records_once(no_wait):
    from backend.ai.engine.engine import Engine
    from backend.ai.providers.manager.manager import ProviderManager
    from backend.ai.session.request import AIRequest

    rl = {
        "success": False, "text": "429 slow down",
        "metadata": {"failure_type": "rate_limited", "retry_after": 0.05},
    }
    flaky = _Scripted("flaky-primary", [rl, rl])
    steady = _Scripted("steady-backup", [_OK])

    pm = ProviderManager()
    pm.register_provider(flaky)
    pm.register_provider(steady)
    pm.switch_provider("flaky-primary")

    engine = Engine(providers=pm)
    before = len(telemetry.recent(50))
    result = await engine.execute(
        AIRequest(session_id="t35", user_message="hello there", owner_id=42,
                  chat_id=-100, message_id=1)
    )

    assert result.success is True
    assert result.metadata.get("fallback_used") is True
    assert result.metadata.get("retry_count") == 1
    records = telemetry.recent(50)
    # EXACTLY one normalized record for the whole request.
    assert len(records) == before + 1
    rec = records[-1]
    assert rec.status == "success"
    assert rec.provider == "steady-backup"
    assert rec.fallback_used is True
    assert rec.retry_count == 1
    assert rec.token_source == "actual"
