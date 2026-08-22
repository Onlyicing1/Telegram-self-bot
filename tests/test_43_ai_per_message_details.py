"""
TASK 43 — Remove obsolete ai_status + per-message AI Details.

Verifies that:
  1. the obsolete Telegram ai_status surface has no production consumer,
  2. the internal (observability) ai_status runtime state is untouched,
  3. per-message Details renders from the ReplyResolver mapping with
     honest token-source semantics, retry/fallback facts, no fabricated
     values, no raw provider internals, and zero-spam edit-in-place
     output,
  4. unrequested Details leaves the normal AI response behavior intact.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.ai.context.reply_resolver import ReplyResolver, get_resolver
from backend.ai.engine.result import EngineResult
from backend.ai.engine.telemetry import telemetry


@pytest.fixture(autouse=True)
def _cleanup():
    telemetry.reset_for_tests()
    get_resolver().clear()
    yield
    telemetry.reset_for_tests()
    get_resolver().clear()


# ── 1. ai_status removal / internal state ──


def test_obsolete_ai_status_has_no_production_consumer():
    ai = Path("backend/bot/handlers/ai.py").read_text()
    assert "ai_status" not in ai
    assert 'register_panel("ai_status"' not in ai
    assert 'register_action("ai_status_refresh"' not in ai
    assert 'panel:ai_status' not in ai


def test_internal_runtime_ai_status_state_still_works():
    """The observability/runtime ai_status is separate execution state and
    must remain intact (crash reports + diagnostics depend on it)."""
    from backend.observability.runtime_status import _ai_status

    status = _ai_status()
    assert isinstance(status, dict)
    assert "available" in status


def test_ai_status_replaced_by_overview_entry():
    """The three former Status entry points now point at the Overview."""
    from backend.bot.handlers import ai as ai_module

    panels, actions = [], []
    with __import__("unittest.mock", fromlist=["patch"]).patch.object(
        ai_module, "register_panel", side_effect=lambda *a, **k: panels.append(a[0])), \
         __import__("unittest.mock", fromlist=["patch"]).patch.object(
        ai_module, "register_action", side_effect=lambda *a, **k: actions.append(a[0])), \
         __import__("unittest.mock", fromlist=["patch"]).patch.object(
        ai_module, "register_inline_builder"), \
         __import__("unittest.mock", fromlist=["patch"]).patch.object(
        ai_module, "register_input"):
        ai_module.register(None, 0)

    assert "ai_status" not in panels
    assert "ai_status_refresh" not in actions
    assert "ai" in panels  # Overview still registered


# ── 2. Per-message Details ──


def _register(msg_id: int = 1001, **overrides) -> None:
    fields = dict(
        session_id="s1", role="assistant", content="hello from the model",
        provider="gemini", model="gemini-2.5-flash",
        input_tokens=100, output_tokens=20, total_tokens=120,
        token_source="actual", latency_s=0.75, retry_count=1,
        fallback_used=True,
    )
    fields.update(overrides)
    get_resolver().register(telegram_msg_id=msg_id, **fields)


@pytest.mark.asyncio
async def test_per_message_details_renders_for_normal_execution():
    from backend.bot.handlers import ai as ai_module

    _register()
    title, body, buttons = await ai_module._ai_details_panel_handler(None, "1001")

    assert title == "AI · Details"
    assert "gemini-2.5-flash" in body
    assert "Gemini" in body or "gemini" in body
    assert "100 in · 20 out" in body


@pytest.mark.asyncio
async def test_per_message_provider_model_correct():
    from backend.bot.handlers import ai as ai_module

    _register(provider="openrouter", model="openai/gpt-5")
    _, body, _ = await ai_module._ai_details_panel_handler(None, "1001")

    assert "openai/gpt-5" in body
    assert "OpenRouter" in body


@pytest.mark.asyncio
async def test_per_message_tokens_estimated_marked():
    from backend.bot.handlers import ai as ai_module

    _register(token_source="estimated")
    _, body, _ = await ai_module._ai_details_panel_handler(None, "1001")

    assert "100 in · 20 out ≈" in body


@pytest.mark.asyncio
async def test_per_message_unavailable_tokens_not_fabricated():
    from backend.bot.handlers import ai as ai_module

    _register(token_source="unavailable", input_tokens=0, output_tokens=0,
              total_tokens=0)
    _, body, _ = await ai_module._ai_details_panel_handler(None, "1001")

    assert "Unavailable" in body
    assert "0 in" not in body


@pytest.mark.asyncio
async def test_per_message_unknown_source_not_claimed_actual():
    from backend.bot.handlers import ai as ai_module

    # Legacy entry (no usage fields): totals unknown — must not render as 0.
    _register(token_source="", input_tokens=0, output_tokens=0, total_tokens=0)
    _, body, _ = await ai_module._ai_details_panel_handler(None, "1001")

    assert "Unavailable" in body


@pytest.mark.asyncio
async def test_per_message_retry_fallback_represented():
    from backend.bot.handlers import ai as ai_module

    _register(retry_count=2, fallback_used=True)
    _, body, _ = await ai_module._ai_details_panel_handler(None, "1001")

    lines = body.splitlines()
    retry_row = next(l for l in lines if l.startswith("Retries"))
    backup_row = next(l for l in lines if l.startswith("Backup"))
    assert retry_row.endswith("2")
    assert backup_row.endswith("Used")


@pytest.mark.asyncio
async def test_per_message_no_fabricated_cooldown_or_quota():
    from backend.bot.handlers import ai as ai_module

    _register()
    _, body, _ = await ai_module._ai_details_panel_handler(None, "1001")

    assert "retry in ~" not in body
    assert "quota" not in body.lower()
    assert "reset" not in body.lower()


@pytest.mark.asyncio
async def test_per_message_no_raw_internals_or_secrets():
    from backend.bot.handlers import ai as ai_module

    _register()
    _, body, _ = await ai_module._ai_details_panel_handler(None, "1001")

    lowered = body.lower()
    for leaked in ("http ", "traceback", "api key", "bearer", "sk-", "exception"):
        assert leaked not in lowered, f"leaked '{leaked}' in Details"


@pytest.mark.asyncio
async def test_per_message_edit_in_place_single_render():
    """The Details surface returns one render payload — no extra messages."""
    from backend.bot.handlers import ai as ai_module

    _register()
    result = await ai_module._ai_details_panel_handler(None, "1001")
    assert result is not None
    title, body, buttons = result
    assert isinstance(title, str) and isinstance(body, str) and isinstance(buttons, list)


@pytest.mark.asyncio
async def test_resolve_miss_falls_back_to_latest_record():
    from backend.bot.handlers import ai as ai_module

    telemetry.record_execution(
        EngineResult(success=True, total_tokens=2671, prompt_tokens=2184,
                     completion_tokens=487, metadata={"token_source": "actual"}),
        1,
    )
    _register()  # registered under 1001; we ask for an unknown id
    _, body, _ = await ai_module._ai_details_panel_handler(None, "999999")

    assert "2,184 in" in body  # from the latest AIExecutionRecord


@pytest.mark.asyncio
async def test_details_without_extra_uses_latest_record():
    from backend.bot.handlers import ai as ai_module

    telemetry.record_execution(
        EngineResult(success=True, total_tokens=120, prompt_tokens=100,
                     completion_tokens=20, metadata={"token_source": "actual"}),
        1,
    )
    _register()
    _, body, _ = await ai_module._ai_details_panel_handler(None, "")

    assert "100 in · 20 out" in body


def test_parse_msg_id_rejects_non_positive():
    from backend.bot.handlers import ai as ai_module

    assert ai_module._parse_msg_id("123") == 123
    assert ai_module._parse_msg_id("") is None
    assert ai_module._parse_msg_id("abc") is None
    assert ai_module._parse_msg_id("-5") is None
    assert ai_module._parse_msg_id("0") is None


def test_resolver_defaults_keep_legacy_callers_compiling():
    resolver = ReplyResolver()
    resolver.register(telegram_msg_id=7, session_id="s", role="assistant",
                      content="ok", provider="p", model="m")
    entry = resolver.resolve(7)
    assert entry is not None
    assert entry.input_tokens == 0
    assert entry.output_tokens == 0
    assert entry.total_tokens == 0
    assert entry.token_source == ""
    assert entry.latency_s == 0.0
    assert entry.retry_count == 0
    assert entry.fallback_used is False
