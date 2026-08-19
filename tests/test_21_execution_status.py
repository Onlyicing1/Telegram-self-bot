"""
Focused tests for Execution AI tool-selection/status intents, bounded retry,
provider failure classification, and the corrected "delete last N" scope.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.ai.actions import (
    KIND_CONVERSATIONAL,
    KIND_EXECUTABLE,
    KIND_INVALID,
    parse_action_text,
    parse_command_intent,
)


# ── Deterministic status/query intents (list saves / db / username / bio) ──


@pytest.mark.parametrize(
    "text, tool_name",
    [
        ("چه چیزایی سیو دارم؟", "list_saves"),
        ("چه چیزایی سیو شدن؟", "list_saves"),
        ("لیست سیوها رو بده", "list_saves"),
        ("وضعیت سیوها چیه؟", "list_saves"),
        ("چه چیزایی ذخیره دارم", "list_saves"),
        ("list my saved items", "list_saves"),
        ("show my saves", "list_saves"),
        ("saved items", "list_saves"),
    ],
)
def test_deterministic_list_saved_items(text, tool_name):
    r = parse_command_intent(text, has_reply=False)
    assert r.kind == KIND_EXECUTABLE
    assert r.tool_calls == [{"name": tool_name, "arguments": {}}]


def test_deterministic_database_stats_persian():
    r = parse_command_intent("وضعیت دیتابیس چیه؟", has_reply=False)
    assert r.kind == KIND_EXECUTABLE
    assert r.tool_calls == [{"name": "database_stats", "arguments": {}}]


def test_deterministic_database_stats_english():
    r = parse_command_intent("database status", has_reply=False)
    assert r.kind == KIND_EXECUTABLE
    assert r.tool_calls == [{"name": "database_stats", "arguments": {}}]


def test_deterministic_username_status():
    # Casual Persian "یوزرنیم" means the account FIRST NAME in this project
    # (the username engine updates first_name) — not the Telegram @username.
    r = parse_command_intent("وضعیت یوزرنیم رو بگو", has_reply=False)
    assert r.kind == KIND_EXECUTABLE
    assert r.tool_calls == [{"name": "account_show", "arguments": {"fields": ["first_name"]}}]


def test_deterministic_bio_status():
    r = parse_command_intent("وضعیت بایو چیه", has_reply=False)
    assert r.kind == KIND_EXECUTABLE
    assert r.tool_calls == [{"name": "bio_show", "arguments": {}}]


def test_deterministic_status_does_not_shadow_save_or_delete():
    # "اینو سیو کن" is a save command, not a list-saves query.
    r = parse_command_intent("اینو سیو کن", has_reply=True)
    assert r.kind == KIND_EXECUTABLE
    assert r.tool_calls == [{"name": "save", "arguments": {}}]
    # "اینو پاک کن" is a delete command, not a status query.
    r = parse_command_intent("اینو پاک کن", has_reply=True)
    assert r.kind == KIND_EXECUTABLE
    assert r.tool_calls == [{"name": "delete_replied", "arguments": {}}]


def test_deterministic_question_about_save_stays_conversational():
    # "what does save mean?" must not become a list-saves tool call.
    r = parse_command_intent("what does save mean?", has_reply=False)
    assert r.kind == KIND_CONVERSATIONAL


# ── JSON action schema for status actions ──


def test_json_list_saved_items():
    r = parse_action_text('{"action": "list_saved_items"}')
    assert r.kind == KIND_EXECUTABLE
    assert r.tool_calls == [{"name": "list_saves", "arguments": {}}]


def test_json_database_stats():
    r = parse_action_text('{"action": "database_stats"}')
    assert r.kind == KIND_EXECUTABLE
    assert r.tool_calls == [{"name": "database_stats", "arguments": {}}]


def test_json_search_saved_items_requires_query():
    r = parse_action_text('{"action": "search_saved_items", "query": "photo"}')
    assert r.kind == KIND_EXECUTABLE
    assert r.tool_calls == [{"name": "search", "arguments": {"query": "photo"}}]
    r = parse_action_text('{"action": "search_saved_items"}')
    assert r.kind == KIND_INVALID


# ── DatabaseStatsTool actually executes ──


@pytest.mark.asyncio
async def test_database_stats_tool_calls_service():
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.database import DatabaseStatsTool
    from backend.services import database_service

    ctx = ToolContext(telegram=None, owner_id=1, tz_str="UTC")
    tool = DatabaseStatsTool(ctx)

    async def fake_do_stats(owner_id, tz_str):
        return "📊 Total saved items: 4"

    with patch.object(database_service, "do_stats", new=fake_do_stats):
        result = await tool.execute(ctx, {})

    assert result.success is True
    assert "4" in result.message


# ── Delete last N: count ALL real messages, delete only outgoing ──


@pytest.mark.asyncio
async def test_delete_last_n_includes_all_participants_and_request_message():
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.delete import DeleteTool

    class FakeMessage:
        def __init__(self, mid, out):
            self.id = mid
            self.out = out

    class FakeClient:
        def __init__(self):
            self.deleted = []

        async def iter_messages(self, chat_id, limit):
            # Telethon returns newest-first; the request message (100) is newest.
            newest_first = [
                FakeMessage(100, True),   # the owner's "delete last N" request
                FakeMessage(99, True),    # Nova's own generated/edited message
                FakeMessage(98, False),   # another participant
                FakeMessage(97, True),    # owner
                FakeMessage(96, False),   # another participant
            ]
            for m in newest_first[:limit]:
                yield m

        async def delete_messages(self, chat_id, ids):
            self.deleted.extend(ids)

    client = FakeClient()

    class FakeTelegram:
        pass

    tg = FakeTelegram()
    tg.client = client

    ctx = ToolContext(telegram=tg, owner_id=1, tz_str="UTC", extra={"chat_id": -100})
    result = await DeleteTool(ctx).execute(ctx, {"count": 5})

    assert result.success is True
    # All three outgoing messages (request + Nova + owner) are deleted;
    # incoming messages are never deleted.
    assert set(client.deleted) == {100, 99, 97}
    assert result.data["considered"] == 5
    assert result.data["count"] == 3


@pytest.mark.asyncio
async def test_delete_last_n_nothing_outgoing():
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.delete import DeleteTool

    class FakeMessage:
        def __init__(self, mid):
            self.id = mid
            self.out = False

    class FakeClient:
        def __init__(self):
            self.deleted = []

        async def iter_messages(self, chat_id, limit):
            for m in [FakeMessage(1), FakeMessage(2)][:limit]:
                yield m

        async def delete_messages(self, chat_id, ids):
            raise AssertionError("must not delete")

    client = FakeClient()

    class FakeTelegram:
        pass

    tg = FakeTelegram()
    tg.client = client

    ctx = ToolContext(telegram=tg, owner_id=1, tz_str="UTC", extra={"chat_id": -100})
    result = await DeleteTool(ctx).execute(ctx, {"count": 2})

    assert result.success is True
    assert client.deleted == []
    assert result.data["count"] == 0


# ── Bounded empty-response retry without double tool execution ──


def _make_dispatcher(mock_te, provider_responses):
    from backend.ai.engine.dispatcher import Dispatcher
    from backend.ai.engine.hooks import NOOP_HOOKS
    from backend.ai.engine.metrics import EngineMetrics

    mock_pm = MagicMock()
    mock_pm.get_active_name.return_value = "test"
    mock_pm.get_active.return_value.config.model = "m"
    mock_pm.get_active.return_value.health.return_value = {"healthy": True}
    mock_pm.get_active.return_value.chat = AsyncMock(side_effect=provider_responses)

    mock_conv = MagicMock()
    mock_sess = MagicMock()
    mock_sess.session_id = "s"
    mock_sess.owner_id = 123
    mock_sess.active_provider = "test"
    mock_conv.get_session.return_value = mock_sess
    mock_conv.restore_history = AsyncMock()
    mock_conv.get_history.return_value = []

    mock_pb = MagicMock()
    pp = MagicMock()
    pp.system_prompt = "sys"
    pp.runtime_context = ""
    pp.conversation_context = ""
    pp.tool_context = ""
    pp.user_input = "do it"
    pp.estimated_tokens.estimated_input_tokens = 50
    pp.estimated_tokens.prompt_size_chars = 100
    mock_pb.build.return_value = pp

    return Dispatcher(
        mock_conv, mock_pb, mock_pm, NOOP_HOOKS, EngineMetrics(), tool_executor=mock_te,
    ), mock_pm


@pytest.mark.asyncio
async def test_empty_provider_response_is_retried_once_then_tool_executes():
    from backend.ai.providers.base.contract import ProviderResponse
    from backend.ai.session.request import AIRequest
    from backend.ai.tools.executor import ToolExecutionResult

    empty = ProviderResponse(
        text="", provider_name="test", success=True, usage={},
        metadata={"finish_reason": "stop"},
    )
    tool_response = ProviderResponse(
        text="", provider_name="test", success=True, usage={},
        tool_calls=[{"id": "t1", "name": "database_stats", "arguments": {}}],
        metadata={"finish_reason": "tool_calls"},
    )
    # The dispatcher makes a continuation provider round after the tool runs.
    continuation = ProviderResponse(
        text="", provider_name="test", success=True, usage={},
        metadata={"finish_reason": "stop"},
    )

    mock_te = MagicMock()
    mock_te.execute_calls = AsyncMock(return_value=[
        ToolExecutionResult(tool_name="database_stats", success=True, message="Total saved items: 4", data={}),
    ])
    mock_te._context = MagicMock()
    mock_te._context.extra = {}
    mock_te._context.telegram = None
    mock_te._context.tz_str = "UTC"
    mock_te._context.client = None

    d, mock_pm = _make_dispatcher(mock_te, [empty, tool_response, continuation])

    result = await d.dispatch(AIRequest(
        session_id="s1", message_id=1, owner_id=123,
        # Conversational (not a deterministic command) so the request reaches
        # the provider and exercises the empty-response → tool-call retry.
        user_message="سلام", chat_id=456,
    ))

    assert result.success is True
    # initial (empty) → empty-response retry (tool) → continuation round = 3.
    assert mock_pm.get_active.return_value.chat.await_count == 3
    # The tool executed exactly once — no duplicate destructive/read execution.
    assert mock_te.execute_calls.await_count == 1
    assert "Total saved items: 4" in result.response


@pytest.mark.asyncio
async def test_empty_provider_response_without_recovery_is_reported():
    from backend.ai.providers.base.contract import ProviderResponse
    from backend.ai.session.request import AIRequest

    empty = ProviderResponse(
        text="", provider_name="test", success=True, usage={},
        metadata={"finish_reason": "stop"},
    )

    mock_te = MagicMock()
    mock_te.execute_calls = AsyncMock()
    mock_te._context = MagicMock()
    mock_te._context.extra = {}

    d, mock_pm = _make_dispatcher(mock_te, [empty, empty])

    result = await d.dispatch(AIRequest(
        session_id="s1", message_id=1, owner_id=123, user_message="hi", chat_id=456,
    ))

    # initial (empty) + one empty-response retry (still empty) = 2 calls.
    assert mock_pm.get_active.return_value.chat.await_count == 2
    assert mock_te.execute_calls.await_count == 0
    assert result.metadata.get("finish_state") == "empty"


# ── Provider failure classification + cooldown recovery ──


def test_failure_type_classifies_model_not_found():
    from backend.ai.providers.base.contract import ProviderResponse
    from backend.ai.providers.manager.manager import ProviderManager

    pm = ProviderManager()
    resp = ProviderResponse(text="", provider_name="g", success=False, metadata={"http_status": 404})
    assert pm._failure_type(resp) == "model_not_found"


def test_model_not_found_does_not_cool_down():
    from backend.ai.providers.base.contract import ProviderResponse
    from backend.ai.providers.manager.health import ProviderHealthState
    from backend.ai.providers.manager.manager import ProviderManager

    pm = ProviderManager()
    resp = ProviderResponse(text="", provider_name="g", success=False, metadata={"http_status": 404})
    pm._apply_failure("g", pm._failure_type(resp), resp)
    assert pm.health_snapshot().get("g") is None
    assert pm._health.state("g") == ProviderHealthState.HEALTHY


def test_rate_limited_cools_down_then_recovers():
    import time as _time

    from backend.ai.providers.manager.health import ProviderHealthState, ProviderHealthTracker

    tracker = ProviderHealthTracker()
    tracker.mark_cooling_down("g", 60)
    assert tracker.state("g") == ProviderHealthState.COOLING_DOWN

    with patch(
        "backend.ai.providers.manager.health.time.monotonic",
        return_value=_time.monotonic() + 61,
    ):
        assert tracker.state("g") == ProviderHealthState.HEALTHY


# ── Security: status tools expose no secrets / no arbitrary methods ──


def test_database_stats_tool_has_no_secret_parameters():
    from backend.ai.tools.database import DatabaseStatsTool
    from backend.ai.tools.context import ToolContext

    tool = DatabaseStatsTool(ToolContext(telegram=None, owner_id=1, tz_str="UTC"))
    assert tool.name == "database_stats"
    assert tool.permission_level.value == "read_only"
    assert "api_key" not in tool.parameters
    assert "session_string" not in tool.parameters


def test_registry_has_status_tools():
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.registry import create_default_registry

    registry = create_default_registry(ToolContext(telegram=None, owner_id=1, tz_str="UTC"))
    names = set(registry.list_names())
    assert "database_stats" in names
    assert "list_saves" in names
    assert "search" in names
    assert "username_show" in names
    assert "bio_show" in names
