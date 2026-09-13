"""Stage I/J gap closures: recursive scheduled creation and message-ID provenance.

Gap I — a scheduled occurrence must not create durable tasks (the recursion
vector), enforced from the trusted occurrence context marker emitted by
``TaskExecutionCoordinator``.

Gap J — a literal Telegram message reference (numeric message ID argument, or
the chat+message ID inside a Telegram link) is only accepted when the OWNER's
request or trusted runtime message identity grounds it.

No provider behavior is relied on for either guard: both are deterministic and
fail closed before persistence.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.ai.database import manager as dbm
from backend.ai.database.task_repository import InMemoryTaskRepository
from backend.ai.providers.base.contract import ProviderResponse
from backend.ai.providers.manager.manager import ProviderManager
from backend.ai.task_execution import TaskExecutionCoordinator
from backend.ai.tools.base import PermissionLevel, ToolResult
from backend.ai.tools.context import ToolContext
from backend.ai.tools.delete import DeleteMessageByIdTool
from backend.ai.tools.executor import ToolExecutor
from backend.ai.tools.registry import ToolRegistry, create_default_registry
from backend.ai.tools.task import CreateTaskTool

import backend.ai.engine.engine as engine_mod

OWNER = 777
CHAT = -1001
REF = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


# ── provider / context helpers (same shape the runtime uses) ──


class _Provider:
    """Minimal provider stub returning one fixed candidate payload."""

    def __init__(self, text: str):
        self.text = text
        self.calls = 0
        self.last_messages = None
        self.name = "fake"
        self.config = type("Config", (), {"default_model": "m"})()
        self.capabilities = type(
            "Capabilities", (), {"supports_tools": True, "supports_function_call": True}
        )()
        self.initialize = lambda: None
        self.shutdown = lambda: None

    def count_tokens(self, text):
        return max(1, len(text) // 4)

    def health(self):
        return {"healthy": True}

    async def chat(self, messages, **kwargs):
        self.calls += 1
        self.last_messages = messages
        return ProviderResponse(text=self.text, provider_name="fake", success=True)


def _manager(provider):
    manager = ProviderManager()
    manager.register_provider(provider)
    manager.switch_provider("fake")
    manager._fallback_chain = []
    return manager


def _context(provider_manager, **extra):
    merged = {"provider_manager": provider_manager, "chat_id": CHAT}
    merged.update(extra)
    return ToolContext(None, OWNER, "UTC", client=None, extra=merged)


def _candidate(actions, *, destination=None, schedule=None):
    return {
        "label": "Task",
        "schedule_type": "interval",
        "schedule": schedule or {"seconds": 300},
        "timezone": "UTC",
        "actions": actions,
        "notification_destination": destination or {},
    }


async def _create(provider_text, request, *, context_extra=None):
    """Run the real CreateTaskTool against an in-memory repository."""
    provider = _Provider(provider_text)
    manager = _manager(provider)
    repository_manager = dbm.RepositoryManager(supabase_available=False)
    # The interactive runtime always exposes the owner's raw request text
    # (dispatcher `_build_tool_context`); mirror that unless a test overrides it.
    merged = {"request_text": request}
    merged.update(context_extra or {})
    context = _context(manager, **merged)
    with patch.object(dbm, "get_repository_manager", return_value=repository_manager):
        result = await CreateTaskTool(context).execute(context, {"request": request})
    return result, provider, repository_manager


# ═══════════════════════════════════════════════════════════════════════════
# Gap I — recursive / chained create_task
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_direct_user_task_creation_still_succeeds():
    provider = _Provider(
        json.dumps(
            _candidate([{"name": "send_message", "arguments": {"text": "hello"}}])
        )
    )
    manager = _manager(provider)
    repository_manager = dbm.RepositoryManager(supabase_available=False)
    context = _context(manager)
    with patch.object(dbm, "get_repository_manager", return_value=repository_manager):
        result = await CreateTaskTool(context).execute(context, {"request": "every 5 minutes send hello"})

    assert result.success is True, result.message
    tasks = await repository_manager.task.list_tasks(OWNER)
    assert [t.actions for t in tasks] == [
        [{"name": "send_message", "arguments": {"text": "hello"}}]
    ]


@pytest.mark.asyncio
async def test_scheduled_occurrence_cannot_create_a_task():
    """The guard runs before any provider resolution and persists nothing."""
    provider = _Provider(
        json.dumps(
            _candidate([{"name": "send_message", "arguments": {"text": "hello"}}])
        )
    )
    manager = _manager(provider)
    repository_manager = dbm.RepositoryManager(supabase_available=False)
    context = _context(manager, scheduled_occurrence=True)
    with patch.object(dbm, "get_repository_manager", return_value=repository_manager), patch.object(
        engine_mod, "get_engine", side_effect=AssertionError("provider must not be resolved")
    ):
        result = await CreateTaskTool(context).execute(context, {"request": "every 5 minutes send hello"})

    assert result.success is False
    assert "cannot create other tasks" in result.message
    assert await repository_manager.task.list_tasks(OWNER) == []
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_scheduled_creation_is_provider_independent():
    """Even a candidate that would be a valid nested creation is refused."""
    payload = _candidate(
        [{"name": "create_task", "arguments": {"request": "every 1 minute send hi"}}]
    )
    provider = _Provider(json.dumps(payload))
    manager = _manager(provider)
    repository_manager = dbm.RepositoryManager(supabase_available=False)
    context = _context(manager, scheduled_occurrence=True)
    with patch.object(dbm, "get_repository_manager", return_value=repository_manager), patch.object(
        engine_mod, "get_engine", side_effect=AssertionError("provider must not be resolved")
    ):
        result = await CreateTaskTool(context).execute(context, {"request": "create a task every minute"})

    assert result.success is False
    assert await repository_manager.task.list_tasks(OWNER) == []
    assert provider.calls == 0


class _CapturingTool:
    """Test tool that records the context the coordinator supplied."""

    permission_level = PermissionLevel.READ_ONLY
    long_running = False
    safe = True
    return_type = "object"
    description = "capture"
    parameters = {}
    name = "capture"

    def __init__(self):
        self.seen_extra = "unset"

    async def execute(self, context, arguments):
        self.seen_extra = dict(context.extra or {})
        return ToolResult(True, "ok")


def _seed_occurrence(repo, owner, actions, key="k"):
    async def _seed():
        task = await repo.create_task(
            owner,
            {
                "label": "scheduled",
                "schedule_type": "once",
                "schedule": {"at": "2026-01-01T12:00:00", "timezone": "UTC"},
                "timezone": "UTC",
                "actions": actions,
                "notification_destination": {},
            },
        )
        await repo.create_occurrence(
            owner,
            {
                "task_id": task.id,
                "occurrence_key": key,
                "definition_version": task.version,
                "action_snapshot": actions,
                "scheduled_for": datetime.now(timezone.utc),
            },
        )
        await repo.claim_occurrence(owner, task.id, key)
        return task, await repo.get_occurrence(owner, task.id, key)

    return _seed()


@pytest.mark.asyncio
async def test_coordinator_marks_the_occurrence_context():
    """The marker is written by the coordinator, not by task/candidate data."""
    repo = InMemoryTaskRepository()
    task, occurrence = await _seed_occurrence(
        repo, OWNER, [{"name": "capture", "arguments": {}}]
    )
    tool = _CapturingTool()
    registry = ToolRegistry()
    registry.register(tool)
    ctx = ToolContext(None, OWNER, "UTC", extra=None)
    result = await TaskExecutionCoordinator(
        repo, ToolExecutor(registry, ctx), OWNER, ctx
    ).execute(occurrence)

    assert result.success is True
    assert tool.seen_extra.get("scheduled_occurrence") is True
    assert "scheduled_occurrence" not in (task.actions[0].get("arguments") or {})


@pytest.mark.asyncio
async def test_occurrence_of_a_create_task_action_persists_no_child():
    """End-to-end: the recursive case is blocked and no child task is written."""
    repo = InMemoryTaskRepository()
    task, occurrence = await _seed_occurrence(
        repo,
        OWNER,
        [{"name": "create_task", "arguments": {"request": "every 1 minute send hi"}}],
    )
    ctx = ToolContext(None, OWNER, "UTC", extra=None)
    registry = create_default_registry(ctx)
    with patch.object(
        engine_mod, "get_engine", side_effect=AssertionError("provider must not be resolved")
    ):
        result = await TaskExecutionCoordinator(
            repo, ToolExecutor(registry, ctx), OWNER, ctx
        ).execute(occurrence)

    assert result.success is False
    assert result.error != ""
    tasks = await repo.list_tasks(OWNER)
    assert [t.id for t in tasks] == [task.id]


@pytest.mark.asyncio
async def test_ordinary_scheduled_actions_still_execute():
    repo = InMemoryTaskRepository()
    task, occurrence = await _seed_occurrence(
        repo, OWNER, [{"name": "capture", "arguments": {}}]
    )
    tool = _CapturingTool()
    registry = ToolRegistry()
    registry.register(tool)
    ctx = ToolContext(None, OWNER, "UTC", extra=None)
    result = await TaskExecutionCoordinator(
        repo, ToolExecutor(registry, ctx), OWNER, ctx
    ).execute(occurrence)

    assert result.success is True and result.status == "succeeded"
    assert (await repo.get_occurrence(OWNER, task.id, "k")).status == "succeeded"


# ═══════════════════════════════════════════════════════════════════════════
# Gap J — literal Telegram message-ID provenance
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_provider_invented_message_id_cannot_become_a_task():
    payload = _candidate(
        [{"name": "delete_message_by_id", "arguments": {"message_id": 12345}}]
    )
    result, provider, repository_manager = await _create(
        json.dumps(payload), "delete my old messages every day at 9"
    )

    assert result.success is False
    assert "does not authorize" in result.message
    assert await repository_manager.task.list_tasks(OWNER) == []
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_reply_grounded_message_id_is_authorized():
    payload = _candidate(
        [{"name": "delete_message_by_id", "arguments": {"message_id": 42}}]
    )
    result, _provider, repository_manager = await _create(
        json.dumps(payload),
        "delete this message every day at 9",
        context_extra={"reply_msg": {"message_id": 42, "chat_id": CHAT}},
    )

    assert result.success is True, result.message
    tasks = await repository_manager.task.list_tasks(OWNER)
    assert tasks[0].actions == [
        {"name": "delete_message_by_id", "arguments": {"message_id": 42}}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("request_text", ["delete message 45 every day", "پیام ۴۵ رو هر روز پاک کن"])
async def test_request_declared_message_id_is_authorized(request_text):
    payload = _candidate(
        [{"name": "delete_message_by_id", "arguments": {"message_id": 45}}]
    )
    result, _provider, repository_manager = await _create(
        json.dumps(payload), request_text, context_extra={"request_text": request_text}
    )

    assert result.success is True, result.message
    tasks = await repository_manager.task.list_tasks(OWNER)
    assert tasks[0].actions[0]["arguments"]["message_id"] == 45


@pytest.mark.asyncio
async def test_message_id_list_grounding_is_per_id():
    payload = _candidate(
        [{"name": "delete_messages_by_ids", "arguments": {"message_ids": [45, 999]}}]
    )
    result, _provider, repository_manager = await _create(
        json.dumps(payload),
        "delete messages 45 every day",
        context_extra={"request_text": "delete messages 45 every day"},
    )

    assert result.success is False
    assert await repository_manager.task.list_tasks(OWNER) == []


@pytest.mark.asyncio
async def test_telegram_link_must_come_from_the_request():
    refused_payload = _candidate(
        [{"name": "save_by_link", "arguments": {"link": "https://t.me/somechannel/42"}}]
    )
    result, _provider, repository_manager = await _create(
        json.dumps(refused_payload), "save that link every day"
    )
    assert result.success is False
    assert await repository_manager.task.list_tasks(OWNER) == []

    allowed_payload = _candidate(
        [{"name": "save_by_link", "arguments": {"link": "https://t.me/somechannel/42"}}]
    )
    result, _provider, repository_manager = await _create(
        json.dumps(allowed_payload),
        "every day save https://t.me/somechannel/42",
    )
    assert result.success is True, result.message
    tasks = await repository_manager.task.list_tasks(OWNER)
    assert tasks[0].actions == [
        {"name": "save_by_link", "arguments": {"link": "https://t.me/somechannel/42"}}
    ]


@pytest.mark.asyncio
async def test_model_destination_cannot_override_trusted_chat_scope():
    payload = _candidate(
        [{"name": "delete_message_by_id", "arguments": {"message_id": 42}}],
        destination={"chat_id": 999999, "chat_title": "Somewhere else"},
    )
    result, _provider, repository_manager = await _create(
        json.dumps(payload),
        "delete this message every day at 9",
        context_extra={"reply_msg": {"message_id": 42, "chat_id": CHAT}},
    )

    assert result.success is True, result.message
    tasks = await repository_manager.task.list_tasks(OWNER)
    assert tasks[0].notification_destination.get("chat_id") == CHAT
    assert tasks[0].notification_destination.get("chat_title") != "Somewhere else"


@pytest.mark.asyncio
async def test_unrelated_actions_are_not_over_blocked():
    payload = _candidate([{"name": "search", "arguments": {"query": "notes"}}])
    result, _provider, repository_manager = await _create(
        json.dumps(payload), "search my notes every day at 9"
    )

    assert result.success is True, result.message
    assert len(await repository_manager.task.list_tasks(OWNER)) == 1


# ── execution-side safety is unchanged ──


class _Msg:
    def __init__(self, mid, outgoing, sender):
        self.id, self.out, self.sender_id = mid, outgoing, sender


class _FakeClient:
    def __init__(self, messages):
        self._messages = messages
        self.me = SimpleNamespace(id=OWNER)
        self.deleted = []

    async def get_messages(self, chat_id, ids=None):
        if isinstance(ids, list):
            return [self._messages.get(i) for i in ids]
        return self._messages.get(ids)

    async def delete_messages(self, chat_id, ids):
        self.deleted.extend(ids)


@pytest.mark.asyncio
async def test_execution_time_ownership_and_stale_checks_remain_enforced():
    client = _FakeClient({42: _Msg(42, True, OWNER), 43: _Msg(43, False, 55)})

    def ctx(extra):
        return ToolContext(
            telegram=None, owner_id=OWNER, tz_str="UTC", client=client, extra=extra
        )

    stale = await DeleteMessageByIdTool(ctx({"chat_id": CHAT})).execute(
        ctx({"chat_id": CHAT}), {"message_id": 999}
    )
    assert stale.success is False and "not found" in stale.message

    foreign = await DeleteMessageByIdTool(ctx({"chat_id": CHAT})).execute(
        ctx({"chat_id": CHAT}), {"message_id": 43}
    )
    assert foreign.success is False and "outgoing-only" in foreign.message

    no_chat = await DeleteMessageByIdTool(ctx(None)).execute(ctx(None), {"message_id": 42})
    assert no_chat.success is False and "No chat context" in no_chat.message

    owned = await DeleteMessageByIdTool(ctx({"chat_id": CHAT})).execute(
        ctx({"chat_id": CHAT}), {"message_id": 42}
    )
    assert owned.success is True and client.deleted == [42]
