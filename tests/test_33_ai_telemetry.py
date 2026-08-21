"""
TASK 33 — AI Execution Telemetry + Observability UX Regression Tests

Focused coverage for the normalized AI execution record and the user-facing
AI panels (Overview / Details / Usage / Health):

  1. Token formatting is honest (actual vs estimated vs unavailable).
  2. Failure classification maps internal types to short user reasons.
  3. record_execution normalizes an EngineResult into the common contract.
  4. summary() aggregates only the requested window.
  5. The dispatcher writes token_source/retry/fallback facts and records.
  6. The new panels render from the record and register correctly.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.ai.engine.result import EngineResult
from backend.ai.engine.telemetry import (
    compact_telemetry_line,
    format_latency,
    format_tokens,
    format_tokens_exact,
    humanize_failure,
    telemetry,
)


@pytest.fixture(autouse=True)
def _reset_telemetry():
    telemetry.reset_for_tests()
    yield
    telemetry.reset_for_tests()


# ── 1. Token / latency formatting ──


def test_format_tokens_compact():
    assert format_tokens(487) == "487"
    assert format_tokens(2671) == "2.7k"
    assert format_tokens(96912) == "96.9k"
    assert format_tokens(100000) == "100k"


def test_format_tokens_exact():
    assert format_tokens_exact(2184) == "2,184"
    assert format_tokens_exact(32768) == "32,768"


def test_format_latency_compact():
    assert format_latency(2.734) == "2.7s"
    assert format_latency(0.8) == "0.8s"
    assert format_latency(15.4) == "15s"


def test_compact_telemetry_line_never_invents_usage():
    actual = EngineResult(success=True, latency=2.734, total_tokens=2671,
                          metadata={"token_source": "actual"})
    est = EngineResult(success=True, latency=2.734, total_tokens=2671,
                       metadata={"token_source": "estimated"})
    none = EngineResult(success=True, latency=2.734, total_tokens=0,
                        metadata={"token_source": "unavailable"})

    assert compact_telemetry_line(telemetry.record_execution(actual, 1)) == "2.7s · 2.7k tokens"
    assert compact_telemetry_line(telemetry.record_execution(est, 1)) == "2.7s · ≈2.7k tokens"
    assert compact_telemetry_line(telemetry.record_execution(none, 1)) == "2.7s"


# ── 2. Failure classification ──


def test_humanize_failure_maps_known_types():
    assert humanize_failure("timeout") == "Timeout"
    assert humanize_failure("rate_limited") == "Rate limited"
    assert humanize_failure("auth") == "Sign-in failed"
    assert humanize_failure("unknown", "The request timed out") == "Timeout"
    assert humanize_failure("unknown", "HTTP 429") == "Rate limited"
    assert humanize_failure("unknown", "something else") == "Unavailable"


# ── 3. record_execution normalization ──


def test_record_execution_normalizes_actual_usage():
    result = EngineResult(
        success=True,
        provider="gemini",
        model="gemini-2.5-flash",
        latency=2.734,
        prompt_tokens=2184,
        completion_tokens=487,
        total_tokens=2671,
        metadata={"token_source": "actual", "retry_count": 0,
                  "fallback_used": False, "tool_call_count": 2,
                  "finish_state": "complete", "context_tokens": 8412},
    )
    record = telemetry.record_execution(result, owner_id=7)

    assert record.status == "success"
    assert record.provider == "gemini"
    assert record.model == "gemini-2.5-flash"
    assert record.input_tokens == 2184
    assert record.output_tokens == 487
    assert record.total_tokens == 2671
    assert record.token_source == "actual"
    assert record.context_tokens == 8412
    assert record.tool_call_count == 2
    assert record.owner_id == 7


def test_record_execution_normalizes_failure_reason():
    result = EngineResult(
        success=False,
        provider="gemini",
        errors=["request timed out"],
        metadata={"token_source": "unavailable", "failure_type": "timeout"},
    )
    record = telemetry.record_execution(result, owner_id=7)

    assert record.status == "failed"
    assert record.error_reason == "Timeout"
    assert record.error_detail == "request timed out"
    assert record.token_source == "unavailable"


def test_record_execution_defaults_token_source_to_unavailable():
    result = EngineResult(success=True, provider="dummy", total_tokens=0)
    record = telemetry.record_execution(result)
    assert record.token_source == "unavailable"
    assert record.total_tokens == 0


# ── 4. summary aggregation ──


def test_summary_aggregates_records():
    telemetry.record_execution(EngineResult(success=True, total_tokens=100, prompt_tokens=80, completion_tokens=20, metadata={"token_source": "actual"}), 1)
    telemetry.record_execution(EngineResult(success=False, total_tokens=0, metadata={"token_source": "unavailable", "failure_type": "timeout", "retry_count": 1, "fallback_used": True}), 1)

    summary = telemetry.summary(since_midnight_utc=True)
    assert summary["requests"] == 2
    assert summary["success"] == 1
    assert summary["failed"] == 1
    assert summary["total_tokens"] == 100
    assert summary["fallbacks"] == 1
    assert summary["retries"] == 1


# ── 5. chat telemetry preference ──


def test_telemetry_pref_toggle_and_reset():
    assert telemetry.get_telemetry_pref(1) is False
    telemetry.set_telemetry_pref(1, True)
    assert telemetry.get_telemetry_pref(1) is True
    telemetry.reset_for_tests()
    assert telemetry.get_telemetry_pref(1) is False


# ── 6. Dispatcher writes telemetry facts ──


class _ScriptedProvider:
    PROVIDER_NAME = "scripted"
    PROVIDER_VERSION = "1.0.0"

    def __init__(self):
        from backend.ai.providers.base.config import ProviderConfig
        from backend.ai.providers.base.contract import ProviderResponse
        self.config = ProviderConfig(provider_name="scripted", enabled=True, default_model="scripted-1")
        self.ProviderResponse = ProviderResponse

    @property
    def name(self):
        return self.PROVIDER_NAME

    def initialize(self):
        pass

    def shutdown(self):
        pass

    async def chat(self, messages, **kwargs):
        return self.ProviderResponse(
            text="ok", provider_name=self.name, success=True,
            usage={"prompt_tokens": 40, "completion_tokens": 8, "total_tokens": 48},
        )

    def count_tokens(self, text: str) -> int:
        return len(text) // 4

    def health(self) -> dict:
        return {"healthy": True, "provider": self.name}


@pytest.mark.asyncio
async def test_dispatcher_writes_token_source_and_records():
    from backend.ai.engine.engine import Engine
    from backend.ai.providers.manager.manager import ProviderManager
    from backend.ai.session.request import AIRequest

    pm = ProviderManager()
    pm.register_provider(_ScriptedProvider())
    pm.switch_provider("scripted")

    engine = Engine(providers=pm)
    result = await engine.execute(
        AIRequest(session_id="t1", user_message="hi", owner_id=777, chat_id=-1, message_id=1)
    )

    assert result.success is True
    assert result.metadata.get("token_source") == "actual"
    assert result.metadata.get("retry_count") == 0
    assert result.metadata.get("fallback_used") is False
    assert result.metadata.get("tool_call_count") == 0

    record = telemetry.last()
    assert record is not None
    assert record.provider == "scripted"
    assert record.owner_id == 777
    assert record.total_tokens == 48
    assert record.token_source == "actual"


# ── 7. New AI panels render + register ──


def _flatten_datas(buttons) -> list[str]:
    datas = []
    for row in buttons:
        if isinstance(row, list):
            for btn in row:
                data = getattr(btn, "data", None)
                if isinstance(data, bytes):
                    datas.append(data.decode("utf-8", errors="replace"))
                elif isinstance(data, str):
                    datas.append(data)
        else:
            data = getattr(row, "data", None)
            if isinstance(data, bytes):
                datas.append(data.decode("utf-8", errors="replace"))
            elif isinstance(data, str):
                datas.append(data)
    return datas


@pytest.mark.asyncio
async def test_ai_overview_panel_renders_ready_with_new_buttons():
    from backend.bot.handlers import ai as ai_module
    from backend.ai.discovery import ProviderStatus

    configured = {
        "provider": "gemini", "model": "gemini-2.5-flash", "temperature": 1.0,
        "max_tokens": 4096, "system_prompt": "", "history_budget": 4000,
        "is_configured": True, "trigger_en": "Nova", "trigger_fa": "",
    }
    available = [
        ProviderStatus(
            name="gemini", display_name="Google Gemini", env_var="AI_GEMINI_API_KEY",
            status="available", has_key=True, validated=True,
            default_model="gemini-2.5-flash", base_url="", icon="🧠",
        )
    ]
    with patch.object(ai_module, "_get_saved_config", AsyncMock(return_value=configured)), \
         patch.object(ai_module, "_discover", AsyncMock(return_value=available)), \
         patch.object(ai_module, "_get_engine_info", return_value={"provider": "gemini", "model": "gemini-2.5-flash", "connected": True}):
        title, body, buttons = await ai_module._ai_main_panel_handler(None, "")

    assert title == "AI"
    assert "gemini-2.5-flash" in body
    datas = _flatten_datas(buttons)
    for expected in ("panel:ai_usage", "panel:ai_health", "panel:ai_details",
                     "panel:ai_model", "panel:ai_provider", "panel:ai_settings"):
        assert expected in datas


@pytest.mark.asyncio
async def test_ai_details_panel_renders_from_record():
    from backend.bot.handlers import ai as ai_module

    telemetry.record_execution(
        EngineResult(
            success=True, provider="gemini", model="gemini-2.5-flash",
            latency=2.734, prompt_tokens=2184, completion_tokens=487,
            total_tokens=2671,
            metadata={"token_source": "actual", "retry_count": 0,
                      "fallback_used": False, "tool_call_count": 1,
                      "context_tokens": 8412},
        ),
        owner_id=0,
    )
    with patch.object(ai_module, "_get_saved_config", AsyncMock(return_value={"model": "gemini-2.5-flash"})):
        title, body, buttons = await ai_module._ai_details_panel_handler(None, "")

    assert title == "AI · Details"
    assert "Model" in body
    assert "2,184" in body
    assert "2.734s" in body


@pytest.mark.asyncio
async def test_ai_usage_panel_renders_summary():
    from backend.bot.handlers import ai as ai_module

    telemetry.record_execution(
        EngineResult(success=True, total_tokens=2671, prompt_tokens=2184, completion_tokens=487,
                     metadata={"token_source": "actual"}), 1
    )
    title, body, buttons = await ai_module._ai_usage_panel_handler(None, "today")

    assert title == "AI · Usage"
    assert "1 requests" in body
    assert "2.7k tokens" in body


@pytest.mark.asyncio
async def test_ai_health_panel_renders_offline_without_config():
    from backend.bot.handlers import ai as ai_module

    with patch.object(ai_module, "_get_saved_config", AsyncMock(return_value={"provider": "", "model": ""})), \
         patch.object(ai_module, "_get_engine", return_value=None), \
         patch.object(ai_module, "_get_engine_info", return_value={"provider": "—", "model": "—", "connected": False}):
        title, body, buttons = await ai_module._ai_health_panel_handler(None, "")

    assert title == "AI · Health"
    assert "OFFLINE" in body


@pytest.mark.asyncio
async def test_new_ai_panels_and_actions_registered():
    from backend.bot.handlers import ai as ai_module

    panels, actions = [], []
    with patch.object(ai_module, "register_panel", side_effect=lambda *a, **k: panels.append(a[0])), \
         patch.object(ai_module, "register_action", side_effect=lambda *a, **k: actions.append(a[0])), \
         patch.object(ai_module, "register_inline_builder"), \
         patch.object(ai_module, "register_input"):
        ai_module.register(None, 0)

    for panel in ("ai_usage", "ai_health", "ai_details"):
        assert panel in panels
    for action in ("ai_health_refresh", "ai_toggle_telemetry"):
        assert action in actions
