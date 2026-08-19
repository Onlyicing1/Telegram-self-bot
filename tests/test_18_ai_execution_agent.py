"""
Focused tests for the AI execution-agent layer: deterministic target
resolution, request-state cleanup, telemetry decoupling, and prompt.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── DeleteRepliedTool: deterministic target resolution ──


@pytest.mark.asyncio
async def test_delete_replied_deletes_owner_message():
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.delete import DeleteRepliedTool

    class FakeMessage:
        out = True

    class FakeClient:
        def __init__(self):
            self.deleted = []
            self.fetched = FakeMessage()

        async def get_messages(self, chat_id, ids):
            assert chat_id == -100
            assert ids == 55
            return self.fetched

        async def delete_messages(self, chat_id, message_ids):
            assert chat_id == -100
            self.deleted.extend(message_ids)

    client = FakeClient()

    class FakeTelegram:
        pass

    tg = FakeTelegram()
    tg.client = client

    ctx = ToolContext(
        telegram=tg,
        owner_id=1,
        tz_str="UTC",
        extra={"reply_msg": {"chat_id": -100, "message_id": 55}},
    )
    result = await DeleteRepliedTool(ctx).execute(ctx, {})

    assert result.success is True
    assert result.data["message_id"] == 55
    assert client.deleted == [55]


@pytest.mark.asyncio
async def test_delete_replied_refuses_incoming_message():
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.delete import DeleteRepliedTool

    class FakeMessage:
        out = False

    class FakeClient:
        def __init__(self):
            self.deleted = []

        async def get_messages(self, chat_id, ids):
            return FakeMessage()

        async def delete_messages(self, chat_id, message_ids):
            self.deleted.extend(message_ids)

    client = FakeClient()

    class FakeTelegram:
        pass

    tg = FakeTelegram()
    tg.client = client

    ctx = ToolContext(
        telegram=tg,
        owner_id=1,
        tz_str="UTC",
        extra={"reply_msg": {"chat_id": -100, "message_id": 55}},
    )
    result = await DeleteRepliedTool(ctx).execute(ctx, {})

    assert result.success is False
    assert "outgoing-only" in result.message
    assert client.deleted == []


@pytest.mark.asyncio
async def test_delete_replied_requires_reply_context():
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.delete import DeleteRepliedTool

    ctx = ToolContext(telegram=None, owner_id=1, tz_str="UTC", extra={})
    result = await DeleteRepliedTool(ctx).execute(ctx, {})
    assert result.success is False
    assert "No replied message" in result.message


# ── Request telemetry must not rewrite AI config ──


def test_record_request_is_targeted_update():
    from backend.ai import config_store

    captured: dict = {}

    class FakeTable:
        def update(self, payload):
            captured["payload"] = payload
            return self

        def eq(self, *a, **k):
            return self

        def execute(self):
            return MagicMock()

    class FakeDB:
        def table(self, name):
            captured["table"] = name
            return FakeTable()

    with patch.object(config_store, "_get_db", return_value=FakeDB()):
        ok = config_store._record_request_sync(777, 123.0)

    assert ok is True
    assert captured["table"] == "ai_config"
    payload = captured["payload"]
    assert payload["last_latency_ms"] == 123.0
    assert "last_request_at" in payload
    for key in ("provider", "model", "system_prompt", "trigger_en", "trigger_fa", "temperature"):
        assert key not in payload


# ── Request-state cleanup: ai_active must return to 0 ──


@pytest.mark.asyncio
async def test_execute_ai_cleans_up_active_request():
    import backend.bot.handlers.ai_unified as ai_unified
    from backend.ai import diagnostics as ai_diag
    from backend.ai.engine.result import EngineResult

    ai_diag._active.clear()

    class FakeProviderManager:
        def get_active_name(self):
            return "dummy"

        def get_active(self):
            return MagicMock(config=MagicMock(default_model="dummy"))

    class FakeEngine:
        def __init__(self):
            self.provider_manager = FakeProviderManager()

        async def execute(self, request, status_callback=None):
            return EngineResult(
                success=True,
                provider="dummy",
                model="dummy",
                latency=0.01,
                response="ok",
            )

    class FakeMessage:
        id = 123

    class FakeEvent:
        chat_id = -100
        message = FakeMessage()

        async def edit(self, text):
            pass

        async def reply(self, text):
            pass

    orig_engine = ai_unified._engine
    orig_restore = ai_unified._restore_config
    ai_unified._engine = FakeEngine()

    async def _noop_restore(owner_id):
        return None

    ai_unified._restore_config = _noop_restore

    try:
        with patch("backend.ai.config_store.record_request",
                   new=lambda owner_id, latency_ms: None), \
             patch("backend.runtime.task_guard.guarded_create_task",
                   new=MagicMock(return_value=None)):
            await ai_unified._execute_ai(FakeEvent(), 777, "hello", "Nova", "UTC")

        assert ai_diag.active_count() == 0
    finally:
        ai_unified._engine = orig_engine
        ai_unified._restore_config = orig_restore


# ── Prompt must be an execution-agent prompt, not a chatbot ──


def test_prompt_is_execution_agent():
    from backend.ai.prompt import template

    assert "execution agent" in template.SYSTEM_RULES_TEMPLATE.lower()
    assert "replied-to message" in template.SYSTEM_RULES_TEMPLATE
    assert "do NOT ask for a message ID" in template.SYSTEM_RULES_TEMPLATE
    assert "outgoing-only" in template.SYSTEM_RULES_TEMPLATE.lower()


def test_prompt_runtime_rules_resolve_deterministic_targets():
    from backend.ai.prompt import template

    rules = template.RUNTIME_RULES_TEMPLATE
    assert "do not ask for an ID" in rules
    assert "report the tool's actual result" in rules


def test_default_registry_includes_delete_replied():
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.registry import create_default_registry

    registry = create_default_registry(ToolContext(telegram=None, owner_id=1, tz_str="UTC"))
    assert registry.has("delete_replied") is True
