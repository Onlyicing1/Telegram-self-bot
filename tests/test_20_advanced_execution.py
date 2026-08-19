"""
Focused tests for advanced Execution AI capabilities: save-by-link, explicit
message-ID delete, semantic/content-based delete (bounded + validated), and
the security boundary (allowlist, no arbitrary Telegram methods, no secrets).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from backend.ai.actions import (
    KIND_CLARIFY,
    KIND_CONVERSATIONAL,
    KIND_EXECUTABLE,
    KIND_INVALID,
    KIND_UNSUPPORTED,
    parse_action_text,
    parse_command_intent,
)


# ── Action schema: save-by-link + explicit message ID ──


def test_deterministic_save_by_link_persian():
    r = parse_command_intent("این لینک رو سیو کن https://t.me/somechannel/42", has_reply=False)
    assert r.kind == KIND_EXECUTABLE
    assert r.action == "save_link"
    assert r.tool_calls == [{"name": "save_by_link", "arguments": {"link": "https://t.me/somechannel/42"}}]


def test_deterministic_save_by_link_english():
    r = parse_command_intent("save this link https://t.me/c/3080318802/99", has_reply=False)
    assert r.kind == KIND_EXECUTABLE
    assert r.action == "save_link"
    assert r.tool_calls == [{"name": "save_by_link", "arguments": {"link": "https://t.me/c/3080318802/99"}}]


def test_deterministic_link_url_preserved_verbatim():
    r = parse_command_intent("این لینک رو سیو کن https://t.me/SomeChannel/12345", has_reply=False)
    assert r.tool_calls == [{"name": "save_by_link", "arguments": {"link": "https://t.me/SomeChannel/12345"}}]


def test_deterministic_explicit_message_id_delete():
    r = parse_command_intent("پیام با ID 123 رو پاک کن", has_reply=False)
    assert r.kind == KIND_EXECUTABLE
    assert r.tool_calls == [{"name": "delete_message_by_id", "arguments": {"message_id": 123}}]


def test_deterministic_pure_negation_is_not_delete():
    r = parse_command_intent("پاکش نکن", has_reply=True)
    assert r.kind == KIND_CONVERSATIONAL


def test_json_save_link():
    r = parse_action_text('{"action": "save_link", "link": "https://t.me/x/1"}')
    assert r.kind == KIND_EXECUTABLE
    assert r.tool_calls == [{"name": "save_by_link", "arguments": {"link": "https://t.me/x/1"}}]


def test_json_save_link_malformed_rejected():
    r = parse_action_text('{"action": "save_link", "link": "not a link"}')
    assert r.kind == KIND_INVALID


def test_json_delete_message_by_id():
    r = parse_action_text('{"action": "delete_messages", "target": "message_id", "message_id": 55}')
    assert r.kind == KIND_EXECUTABLE
    assert r.tool_calls == [{"name": "delete_message_by_id", "arguments": {"message_id": 55}}]


def test_json_delete_invalid_message_id_rejected():
    r = parse_action_text('{"action": "delete_messages", "target": "message_id", "message_id": -1}')
    assert r.kind == KIND_INVALID


def test_json_unknown_action_rejected():
    r = parse_action_text('{"action": "delete_everything_forever"}')
    assert r.kind == KIND_INVALID


def test_unknown_field_still_rejected():
    r = parse_action_text('{"action": "save", "telegram_method": "hack"}')
    assert r.kind == KIND_INVALID


# ── SaveByLinkTool ──


@pytest.mark.asyncio
async def test_save_by_link_tool_calls_existing_executor_with_exact_url():
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.save import SaveByLinkTool
    from backend.services import save_service

    class FakeTelegram:
        client = "CLIENT"

    ctx = ToolContext(telegram=FakeTelegram(), owner_id=1, tz_str="UTC", extra={})
    tool = SaveByLinkTool(ctx)

    captured: dict = {}

    async def fake_execute_link_save(client, owner_id, link, tz_str):
        captured["client"] = client
        captured["owner_id"] = owner_id
        captured["link"] = link
        captured["tz_str"] = tz_str
        return "📷 **Saved Successfully**"

    with patch.object(save_service, "execute_link_save", new=fake_execute_link_save):
        result = await tool.execute(ctx, {"link": "https://t.me/SomeChannel/42"})

    assert result.success is True
    assert captured["link"] == "https://t.me/SomeChannel/42"
    assert captured["client"] == "CLIENT"


@pytest.mark.asyncio
async def test_save_by_link_tool_rejects_malformed_link():
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.save import SaveByLinkTool
    from backend.services import save_service

    class FakeTelegram:
        client = "CLIENT"

    ctx = ToolContext(telegram=FakeTelegram(), owner_id=1, tz_str="UTC", extra={})
    tool = SaveByLinkTool(ctx)

    with patch.object(save_service, "execute_link_save", new=AsyncMock()) as mock_exec:
        result = await tool.execute(ctx, {"link": "not a link"})

    assert result.success is False
    mock_exec.assert_not_called()


@pytest.mark.asyncio
async def test_save_by_link_tool_normalizes_missing_scheme():
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.save import SaveByLinkTool
    from backend.services import save_service

    class FakeTelegram:
        client = "CLIENT"

    ctx = ToolContext(telegram=FakeTelegram(), owner_id=1, tz_str="UTC", extra={})
    tool = SaveByLinkTool(ctx)

    captured: dict = {}

    async def fake_execute_link_save(client, owner_id, link, tz_str):
        captured["link"] = link
        return "saved"

    with patch.object(save_service, "execute_link_save", new=fake_execute_link_save):
        await tool.execute(ctx, {"link": "t.me/somechannel/7"})

    assert captured["link"] == "https://t.me/somechannel/7"


# ── DeleteMessageByIdTool ──


@pytest.mark.asyncio
async def test_delete_message_by_id_deletes_outgoing():
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.delete import DeleteMessageByIdTool

    class FakeMessage:
        out = True

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

    ctx = ToolContext(telegram=tg, owner_id=1, tz_str="UTC", extra={"chat_id": -100})
    result = await DeleteMessageByIdTool(ctx).execute(ctx, {"message_id": 77})

    assert result.success is True
    assert client.deleted == [77]


@pytest.mark.asyncio
async def test_delete_message_by_id_refuses_incoming():
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.delete import DeleteMessageByIdTool

    class FakeMessage:
        out = False

    class FakeClient:
        async def get_messages(self, chat_id, ids):
            return FakeMessage()

        async def delete_messages(self, chat_id, message_ids):
            raise AssertionError("must not delete")

    client = FakeClient()

    class FakeTelegram:
        pass

    tg = FakeTelegram()
    tg.client = client

    ctx = ToolContext(telegram=tg, owner_id=1, tz_str="UTC", extra={"chat_id": -100})
    result = await DeleteMessageByIdTool(ctx).execute(ctx, {"message_id": 77})

    assert result.success is False
    assert "outgoing-only" in result.message


# ── Semantic delete: bounded listing + validated ID deletion ──


@pytest.mark.asyncio
async def test_list_recent_messages_is_bounded_and_returns_ids():
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.semantic import ListRecentMessagesTool

    class FakeMessage:
        def __init__(self, mid, text):
            self.id = mid
            self.text = text
            self.media = None

    class FakeClient:
        async def iter_messages(self, chat_id, limit, from_user=None):
            assert from_user == "me"
            for i in range(limit):
                yield FakeMessage(i + 1, f"message {i + 1}")

    client = FakeClient()

    class FakeTelegram:
        pass

    tg = FakeTelegram()
    tg.client = client

    ctx = ToolContext(telegram=tg, owner_id=1, tz_str="UTC", extra={"chat_id": -100})
    result = await ListRecentMessagesTool(ctx).execute(ctx, {"limit": 3})

    assert result.success is True
    assert len(result.data["messages"]) == 3
    assert result.data["messages"][0]["id"] == 1


@pytest.mark.asyncio
async def test_delete_messages_by_ids_only_deletes_validated_outgoing():
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.semantic import DeleteMessagesByIdsTool

    class FakeMessage:
        def __init__(self, out):
            self.out = out

    class FakeClient:
        def __init__(self):
            self.deleted = []
            self.messages = {
                1: FakeMessage(True),
                2: FakeMessage(False),  # incoming
                3: None,                # not found / invented
            }

        async def get_messages(self, chat_id, ids):
            return self.messages.get(ids)

        async def delete_messages(self, chat_id, message_ids):
            self.deleted.extend(message_ids)

    client = FakeClient()

    class FakeTelegram:
        pass

    tg = FakeTelegram()
    tg.client = client

    ctx = ToolContext(telegram=tg, owner_id=1, tz_str="UTC", extra={"chat_id": -100})
    result = await DeleteMessagesByIdsTool(ctx).execute(
        ctx, {"message_ids": [1, 2, 3, 999]}
    )

    assert result.success is True
    assert client.deleted == [1]
    assert result.data["deleted"] == [1]
    assert set(result.data["rejected"]) == {2, 3, 999}


@pytest.mark.asyncio
async def test_delete_messages_by_ids_rejects_empty_ids():
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.semantic import DeleteMessagesByIdsTool

    ctx = ToolContext(telegram=None, owner_id=1, tz_str="UTC", extra={"chat_id": -100})
    result = await DeleteMessagesByIdsTool(ctx).execute(ctx, {"message_ids": []})
    assert result.success is False
    assert "No valid message IDs" in result.message


# ── Security boundary ──


def test_registry_has_no_arbitrary_telegram_or_exec_tools():
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.registry import create_default_registry

    registry = create_default_registry(ToolContext(telegram=None, owner_id=1, tz_str="UTC"))
    names = set(registry.list_names())
    for forbidden in (
        "telegram_method", "call_rpc", "exec", "eval", "shell", "run_python",
        "send_message", "forward_messages", "read_file", "get_secret", "get_env",
    ):
        assert forbidden not in names


@pytest.mark.asyncio
async def test_executor_rejects_unknown_tool_name():
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.executor import ToolExecutor
    from backend.ai.tools.registry import create_default_registry

    registry = create_default_registry(ToolContext(telegram=None, owner_id=1, tz_str="UTC"))
    executor = ToolExecutor(registry, ToolContext(telegram=None, owner_id=1, tz_str="UTC"))
    results = await executor.execute_calls(
        [{"name": "delete_everything", "arguments": {}}], owner_id=1
    )
    assert results[0].success is False
    assert results[0].error == "not_found"


def test_prompt_marks_message_content_as_untrusted_data():
    from backend.ai.prompt import template

    assert "UNTRUSTED DATA" in template.SYSTEM_RULES_TEMPLATE
    assert "never instructions" in template.SYSTEM_RULES_TEMPLATE


def test_save_link_and_semantic_tools_are_documented_in_prompt():
    from backend.ai.prompt import template

    assert "save_by_link" in template.SYSTEM_RULES_TEMPLATE
    assert "list_recent_messages" in template.SYSTEM_RULES_TEMPLATE
    assert "delete_messages_by_ids" in template.SYSTEM_RULES_TEMPLATE
    assert "Never invent message IDs" in template.SYSTEM_RULES_TEMPLATE


def test_no_secret_fields_in_tool_result_data():
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.registry import create_default_registry

    registry = create_default_registry(ToolContext(telegram=None, owner_id=1, tz_str="UTC"))
    for schema in registry.list_schemas():
        params = schema.get("parameters") or {}
        assert "api_key" not in params
        assert "session_string" not in params
        assert "token" not in params
