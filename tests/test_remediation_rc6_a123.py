"""Remediation of the small source-proven findings (RC-6 / A-1 / A-2 / A-3).

Pins the real contracts so the drift cannot silently return:

  - RC-6: the ToolExecutor's actual authorization model — READ_ONLY /
    READ_WRITE / DANGEROUS execute directly (owner message = authorization);
    ADMIN_ONLY/CONFIRMATION_REQUIRED never auto-execute; ``execute_confirmed``
    remains the only gate bypass. No docstring may claim otherwise.
  - A-1: ``delete_messages_by_ids`` must advertise the ENFORCED boundary
    (re-fetch + outgoing-only + same chat, invalid ids skipped) and must not
    claim an unenforced turn-scoped provenance requirement.
  - A-2: ``settings_get`` on an unset AI key returns success=False with an
    explicit "is not set" message — never a fake ``key = `` success.
  - A-3: web-search catch-alls propagate a bounded, secret-free failure
    reason (exception type) instead of collapsing to a generic message.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.ai.tools.base import PermissionLevel, ToolResult
from backend.ai.tools.context import ToolContext
from backend.ai.tools.executor import ToolExecutor
from backend.ai.tools.registry import ToolRegistry, create_default_registry

OWNER = 906001
UNKNOWN_KEY = "no_such_setting"

_SRC_BASE = "backend/ai/tools/base.py"
_SRC_DELETE = "backend/ai/tools/delete.py"
_SRC_ORGANIZE = "backend/ai/tools/organize.py"
_SRC_SEMANTIC = "backend/ai/tools/semantic.py"


def make_executor(owner_id: int = OWNER, **ctx_kwargs) -> ToolExecutor:
    ctx = ToolContext(telegram=None, owner_id=owner_id, tz_str="UTC", **ctx_kwargs)
    return ToolExecutor(create_default_registry(ctx), ctx)


# ── RC-6: real authorization contract + doc alignment ──


class _StubTool:
    name = "stub"
    description = "stub"
    parameters: dict = {}
    return_type = "text"
    long_running = False

    def __init__(self, level: PermissionLevel) -> None:
        self.permission_level = level
        self.calls = 0

    @property
    def safe(self) -> bool:
        return self.permission_level in (PermissionLevel.READ_ONLY, PermissionLevel.READ_WRITE)

    async def execute(self, context, arguments) -> ToolResult:
        self.calls += 1
        return ToolResult(success=True, message="ran")


def _executor_with(tool: _StubTool) -> ToolExecutor:
    registry = ToolRegistry()
    registry.register(tool)
    return ToolExecutor(registry, ToolContext(telegram=None, owner_id=OWNER, tz_str="UTC"))


@pytest.mark.asyncio
@pytest.mark.parametrize("level", [PermissionLevel.READ_ONLY, PermissionLevel.READ_WRITE, PermissionLevel.DANGEROUS])
async def test_read_only_read_write_and_dangerous_execute_directly(level):
    tool = _StubTool(level)
    executor = _executor_with(tool)
    results = await executor.execute_calls(
        [{"name": "stub", "arguments": {}}], owner_id=OWNER, session_id="rc6"
    )
    assert results[0].success is True
    assert tool.calls == 1, "DANGEROUS must not require an extra confirmation round-trip"


@pytest.mark.asyncio
async def test_admin_only_never_auto_executes_and_needs_confirmation():
    tool = _StubTool(PermissionLevel.ADMIN_ONLY)
    executor = _executor_with(tool)
    results = await executor.execute_calls(
        [{"name": "stub", "arguments": {}}], owner_id=OWNER, session_id="rc6"
    )
    assert results[0].success is False
    assert getattr(results[0], "needs_confirmation", False) is True
    assert tool.calls == 0


@pytest.mark.asyncio
async def test_confirmed_path_is_the_only_gate_bypass():
    tool = _StubTool(PermissionLevel.ADMIN_ONLY)
    executor = _executor_with(tool)
    result = await executor.execute_confirmed(
        {"name": "stub", "arguments": {}},
        owner_id=OWNER,
        session_id="rc6",
        context_override=executor._context,
    )
    assert result.success is True
    assert tool.calls == 1


@pytest.mark.asyncio
async def test_confirmation_required_never_auto_executes():
    tool = _StubTool(PermissionLevel.CONFIRMATION_REQUIRED)
    executor = _executor_with(tool)
    results = await executor.execute_calls(
        [{"name": "stub", "arguments": {}}], owner_id=OWNER, session_id="rc6"
    )
    assert results[0].success is False
    assert tool.calls == 0


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_no_docstring_claims_dangerous_needs_owner_confirmation():
    for path in (_SRC_BASE, _SRC_DELETE, _SRC_ORGANIZE):
        src = _read(path)
        assert "must ask the owner" not in src, path
    base = _read(_SRC_BASE)
    assert "AI must ask the owner first" not in base
    assert "IS the authorization" in base


# ── A-1: description states the enforced boundary, not turn-scoped provenance ──


def test_delete_messages_by_ids_description_states_enforced_boundary():
    src = _read(_SRC_SEMANTIC)
    assert "re-fetched" in src and "re-validated" in src
    assert "in this turn" not in src
    assert "MUST have been returned" not in src


@pytest.mark.asyncio
async def test_delete_messages_by_ids_enforces_outgoing_only_at_runtime():
    """A non-outgoing id is rejected by the service regardless of its source."""
    from backend.ai.tools.semantic import DeleteMessagesByIdsTool
    from backend.services import delete_service

    chat_id = -900
    incoming_msg = SimpleNamespace(id=42, out=False, sender_id=777)

    client = SimpleNamespace(
        me=SimpleNamespace(id=OWNER),
        get_messages=AsyncMock(return_value=[incoming_msg]),
        delete_messages=AsyncMock(),
    )
    telegram = SimpleNamespace(client=client)
    ctx = ToolContext(telegram=telegram, owner_id=OWNER, tz_str="UTC",
                      extra={"chat_id": chat_id})

    async def _rpc_passthrough(op, **kwargs):
        return await op()

    with patch.object(delete_service, "_telegram_rpc",
                      side_effect=_rpc_passthrough):
        result = await DeleteMessagesByIdsTool(ctx).execute(
            ctx, {"message_ids": [42]}
        )

    assert result.success is False
    assert result.data["deleted"] == []
    assert 42 in result.data["rejected"]
    client.delete_messages.assert_not_called()
    client.get_messages.assert_awaited_once()
    assert client.get_messages.await_args.args[0] == chat_id


# ── A-2: settings_get on an unset AI key fails honestly ──


@pytest.mark.asyncio
async def test_settings_get_unset_ai_key_reports_not_set():
    from backend.ai import config_store
    from backend.ai.tools.settings import SettingsGetTool

    ctx = ToolContext(telegram=None, owner_id=906010, tz_str="UTC")
    with patch.object(config_store, "get_config",
                      AsyncMock(return_value={"model": "", "provider": None})):
        result = await SettingsGetTool(ctx).execute(ctx, {"key": "model"})

    assert result.success is False
    assert "is not set" in result.message
    assert "model = " not in result.message
    assert result.data == {"key": "model", "value": ""}


@pytest.mark.asyncio
async def test_settings_get_set_ai_key_still_succeeds():
    from backend.ai import config_store
    from backend.ai.tools.settings import SettingsGetTool

    ctx = ToolContext(telegram=None, owner_id=906011, tz_str="UTC")
    with patch.object(config_store, "get_config",
                      AsyncMock(return_value={"model": "gemini-2.5-flash"})):
        result = await SettingsGetTool(ctx).execute(ctx, {"key": "model"})

    assert result.success is True
    assert "model = gemini-2.5-flash" in result.message
    assert result.data == {"key": "model", "value": "gemini-2.5-flash"}


# ── A-3: web-search failures keep a bounded, secret-free reason ──


def _svc():
    from backend.services import web_search_service
    return web_search_service


def test_sanitize_reason_keeps_type_and_redacts_secrets():
    svc = _svc()

    class Boom(ValueError):
        pass

    reason = svc.sanitize_reason(Boom("X-API-Key: ydc-secret-123 request failed"))
    assert reason.startswith("Boom:")
    assert "ydc-secret-123" not in reason
    assert "[REDACTED]" in reason
    assert len(reason) <= 200

    bearer = svc.sanitize_reason(RuntimeError("Authorization: Bearer abc.def.ghi denied"))
    assert "abc.def.ghi" not in bearer

    empty = svc.sanitize_reason(RuntimeError())
    assert empty == "RuntimeError"


@pytest.mark.asyncio
async def test_service_propagates_reason_on_unexpected_error():
    svc = _svc()
    manager = SimpleNamespace(
        web_search=AsyncMock(side_effect=RuntimeError("connection reset"))
    )
    ok, text, data = await svc.do_web_search("q", provider_manager=manager)
    assert ok is False
    assert text.startswith("❌ Web search failed:")
    assert "RuntimeError: connection reset" in text
    assert data == {}


@pytest.mark.asyncio
async def test_tool_propagates_reason_on_service_crash():
    from backend.ai.tools.websearch import WebSearchTool

    ctx = ToolContext(telegram=None, owner_id=OWNER, tz_str="UTC")
    with patch("backend.services.web_search_service.do_web_search",
               AsyncMock(side_effect=RuntimeError("engine exploded"))):
        result = await WebSearchTool(ctx).execute(ctx, {"query": "q"})

    assert result.success is False
    assert result.message.startswith("❌ Web search failed:")
    assert "RuntimeError: engine exploded" in result.message


@pytest.mark.asyncio
async def test_provider_dict_error_path_unchanged():
    svc = _svc()
    manager = SimpleNamespace(
        web_search=AsyncMock(return_value={
            "success": False, "query": "q", "results": [], "metadata": {},
            "error": "Web search request timed out.",
        })
    )
    ok, text, _ = await svc.do_web_search("q", provider_manager=manager)
    assert ok is False
    assert "⚠️ Web search failed: Web search request timed out." == text
