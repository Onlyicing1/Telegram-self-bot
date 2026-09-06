"""RC-5 regression tests — `settings_set` must fail closed for unknown keys.

Proven defect before the fix (reproduced in-process): `SettingsSetTool`
routed every non-AI key to `settings_service.set_setting()`, which had no
key allowlist. An unknown key skipped validation, the repository write
failed (not a real panel_settings column / no DB in the fallback), and the
fallback path cached the arbitrary key and returned True — a phantom
success: the owner confirmed an ADMIN_ONLY change, was told it was applied,
and nothing was persisted.

These tests pin the fix:
  - `set_setting` returns False for unknown keys, never touches the
    repository, and never pollutes the in-memory cache;
  - `SettingsSetTool.execute` returns `success=False` with an explicit
    unknown-setting error;
  - the CONFIRMED path (`ToolExecutor.execute_confirmed`) also fails closed
    for unknown keys even after owner approval;
  - valid AI-runtime keys (temperature) still route through `config_store`;
  - valid panel keys (language) still route through `settings_service`;
  - invalid values for known keys are still rejected by the validators.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from backend.ai.tools.context import ToolContext
from backend.ai.tools.executor import ToolExecutor
from backend.ai.tools.registry import create_default_registry

OWNER = 905001
UNKNOWN_KEY = "no_such_setting"


@pytest.fixture(autouse=True)
def _restore_settings_cache():
    """The settings service caches panel settings process-wide; restore the
    exact snapshot so values written here never leak into other suites."""
    from backend.services import settings_service

    before = dict(settings_service.get_all())
    yield
    settings_service._cache.clear()
    settings_service._cache.update(before)


def make_executor(owner_id: int = OWNER):
    ctx = ToolContext(telegram=None, owner_id=owner_id, tz_str="UTC")
    registry = create_default_registry(ctx)
    return ToolExecutor(registry, ctx), ctx


# ── Unit: service boundary ──


def test_set_setting_unknown_key_returns_false_and_never_touches_repo_or_cache():
    from backend.services import settings_service

    settings_service.load_all()
    with patch(
        "backend.services.panel_settings_repository.update_field",
        return_value=False,
    ) as updater:
        ok = settings_service.set_setting(UNKNOWN_KEY, "xyz")

    assert ok is False
    updater.assert_not_called()
    assert UNKNOWN_KEY not in settings_service.get_all()
    assert settings_service.get_setting(UNKNOWN_KEY) is None


def test_known_key_invalid_value_still_fails_validation():
    from backend.services import settings_service

    ok = settings_service.set_setting("language", "")
    assert ok is False


def test_known_key_valid_value_still_succeeds_through_service():
    from backend.services import settings_service

    ok = settings_service.set_setting("language", "en")
    assert ok is True
    assert settings_service.get_setting("language") == "en"


# ── Unit: tool boundary ──


@pytest.mark.asyncio
async def test_settings_set_tool_unknown_key_fails_closed():
    from backend.services import settings_service

    executor, ctx = make_executor()
    tool = executor._registry.get("settings_set")
    assert tool is not None

    result = await tool.execute(ctx, {"key": UNKNOWN_KEY, "value": "xyz"})

    assert result.success is False
    assert "Unknown setting key" in result.message
    assert UNKNOWN_KEY not in settings_service.get_all()


@pytest.mark.asyncio
async def test_settings_set_tool_valid_panel_key_still_works():
    executor, ctx = make_executor()
    tool = executor._registry.get("settings_set")

    result = await tool.execute(ctx, {"key": "language", "value": "en"})

    assert result.success is True
    assert "updated" in result.message


@pytest.mark.asyncio
async def test_settings_set_tool_ai_key_still_routes_to_config_store():
    from backend.ai import config_store

    owner_id = 905002
    executor, ctx = make_executor(owner_id=owner_id)
    tool = executor._registry.get("settings_set")

    result = await tool.execute(ctx, {"key": "temperature", "value": "0.7"})

    assert result.success is True
    assert "Temperature set to 0.7" in result.message
    config = await config_store.get_config(owner_id)
    assert config.get("temperature") == 0.7


# ── Integration: confirmed execution path ──


@pytest.mark.asyncio
async def test_execute_confirmed_unknown_key_fails_closed():
    from backend.services import settings_service

    executor, ctx = make_executor()
    result = await executor.execute_confirmed(
        {"name": "settings_set", "arguments": {"key": UNKNOWN_KEY, "value": "xyz"}},
        owner_id=OWNER,
        session_id="rc5-confirmed",
        context_override=ctx,
    )

    assert result.success is False
    assert result.needs_confirmation is False  # the gate was satisfied; the TOOL failed
    assert "Unknown setting key" in result.message
    assert UNKNOWN_KEY not in settings_service.get_all()


@pytest.mark.asyncio
async def test_execute_confirmed_valid_panel_key_still_executes():
    executor, ctx = make_executor()
    result = await executor.execute_confirmed(
        {"name": "settings_set", "arguments": {"key": "language", "value": "fa"}},
        owner_id=OWNER,
        session_id="rc5-confirmed",
        context_override=ctx,
    )

    assert result.success is True
    assert "updated" in result.message