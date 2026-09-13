"""Semantic-completeness boundary for natural-language task creation."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from backend.ai import task_creation
from backend.ai.database import manager as dbm
from backend.ai.task_creation import (
    TaskCreationError,
    TaskCreationService,
    TaskSemanticCompletenessError,
)
from backend.ai.task_candidate import TaskCandidate
from backend.ai.task_interpreter import TaskInterpreter
from backend.ai.tools.context import ToolContext
from backend.ai.tools.registry import ToolRegistry, create_default_registry
from backend.ai.tools.save import SaveTool
from backend.ai.tools.task import CreateTaskTool
from backend.ai.database.task_repository import InMemoryTaskRepository

OWNER = 777
REQUEST = "update my bio every 5 minutes"
REF = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _candidate(*, action="bio_set_text", text="", instruction=None):
    value = {
        "label": "Bio update",
        "schedule_type": "interval",
        "schedule": {"seconds": 300},
        "timezone": "UTC",
        "actions": [{"name": action, "arguments": {"text": text}}],
        "notification_destination": {},
    }
    if instruction is not None:
        value["ai_instruction"] = instruction
    return value


class _Provider:
    def __init__(self, text: str):
        self.text = text
        self.calls = 0
        self.last_messages = None
        self.name = "fake"
        self.config = type("Config", (), {"default_model": "m"})()
        self.capabilities = type("Capabilities", (), {"supports_tools": True, "supports_function_call": True})()
        self.initialize = lambda: None
        self.shutdown = lambda: None

    def count_tokens(self, text):
        return max(1, len(text) // 4)

    def health(self):
        return {"healthy": True}

    async def chat(self, messages, **kwargs):
        from backend.ai.providers.base.contract import ProviderResponse
        self.calls += 1
        self.last_messages = messages
        return ProviderResponse(text=self.text, provider_name="fake", success=True)


def _manager(provider):

    from backend.ai.providers.manager.manager import ProviderManager

    manager = ProviderManager()
    manager.register_provider(provider)
    manager.switch_provider("fake")
    manager._fallback_chain = []
    return manager


def _context(provider_manager):
    return ToolContext(
        telegram=None,
        owner_id=OWNER,
        tz_str="UTC",
        client=None,
        extra={"provider_manager": provider_manager, "chat_id": -1001},
    )


@pytest.mark.asyncio
async def test_schema_valid_empty_profile_candidate_is_not_persisted():
    provider = _Provider(json.dumps(_candidate()))
    provider_manager = _manager(provider)
    repository_manager = dbm.RepositoryManager(supabase_available=False)
    with patch.object(dbm, "get_repository_manager", return_value=repository_manager):
        result = await CreateTaskTool(_context(provider_manager)).execute(
            _context(provider_manager), {"request": REQUEST}
        )

    assert result.success is False
    assert result.data == {
        "open_taskloom_wizard": True,
        "wizard_reason": "candidate_semantically_incomplete",
    }
    assert await repository_manager.task.list_tasks(OWNER) == []


@pytest.mark.asyncio
async def test_fully_specified_generated_profile_candidate_creates_directly():
    provider = _Provider(json.dumps(_candidate(instruction=REQUEST)))
    provider_manager = _manager(provider)
    repository_manager = dbm.RepositoryManager(supabase_available=False)
    with patch.object(dbm, "get_repository_manager", return_value=repository_manager):
        result = await CreateTaskTool(_context(provider_manager)).execute(
            _context(provider_manager), {"request": REQUEST}
        )

    assert result.success is True
    tasks = await repository_manager.task.list_tasks(OWNER)
    assert len(tasks) == 1
    assert tasks[0].ai_instruction == REQUEST
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_direct_creation_service_rejects_empty_profile_candidate_before_repository():
    repository = InMemoryTaskRepository()
    candidate = TaskCandidate.from_untrusted(_candidate())
    with pytest.raises(TaskSemanticCompletenessError):
        await TaskCreationService(repository, OWNER).create(candidate, __import__("datetime").datetime.now(__import__("datetime").timezone.utc))
    assert await repository.list_tasks(OWNER) == []


@pytest.mark.asyncio
async def test_interpreter_prompt_forbids_inventing_missing_requirements():
    from backend.ai.task_interpreter import CANDIDATE_SCHEMA

    provider = _Provider(json.dumps(_candidate(instruction=REQUEST)))
    await TaskInterpreter(_manager(provider)).interpret(REQUEST, timezone="UTC")
    system = " ".join(str(item["content"]) for item in provider.last_messages if item["role"] == "system")
    assert "Never invent missing schedule" in system
    assert "schedule expression alone does not authorize invented content" in system
    assert CANDIDATE_SCHEMA["required"]


# ═══════════════════════════════════════════════════════════════════════════
# Implemented semantic-completeness boundaries.
#
# Each test below exercises the REAL creation path (provider stub →
# TaskInterpreter → TaskCandidate → TaskCreationService → repository) or the
# real ToolRegistry/ToolExecutor contract. No production behavior is faked.
# ═══════════════════════════════════════════════════════════════════════════


def _registry() -> ToolRegistry:
    """The real authoritative registry the runtime attaches to the Engine."""
    return create_default_registry(ToolContext(telegram=None, owner_id=OWNER, tz_str="UTC"))


def _action_candidate(
    action: str,
    arguments: dict,
    *,
    instruction: str | None = None,
    destination: dict | None = None,
) -> dict:
    value = {
        "label": "Action task",
        "schedule_type": "interval",
        "schedule": {"seconds": 300},
        "timezone": "UTC",
        "actions": [{"name": action, "arguments": dict(arguments)}],
        "notification_destination": dict(destination or {}),
    }
    if instruction is not None:
        value["ai_instruction"] = instruction
    return value


def _tool_context(provider_manager, chat_id):
    return ToolContext(
        telegram=None, owner_id=OWNER, tz_str="UTC", client=None,
        extra={"provider_manager": provider_manager, "chat_id": chat_id},
    )


# ── Target 1: required nested arguments before persistence ──

_INCOMPLETE_ACTION_ARGUMENTS = [
    ("web_search", {}),
    ("search", {}),
    ("memory_store", {}),
    ("save_by_link", {}),
    ("retrieve_save", {}),
    ("settings_get", {}),
    ("task_inspect", {}),
    ("task_transition", {}),
    ("task_delete", {}),
    ("create_task", {}),
    ("delete", {}),
    ("delete_by_id", {}),
    ("delete_message_by_id", {}),
    ("delete_messages_by_ids", {}),
    ("bio_set_template", {}),
    ("bio_set_mood", {}),
    ("username_set_template", {}),
    ("username_set_mood", {}),
    ("send_message", {}),
]

_COMPLETE_ACTION_ARGUMENTS = [
    ("send_message", {"text": "hello"}),
    ("account_show", {}),
    ("list_saves", {}),
    ("database_stats", {}),
    ("organize_list", {}),
    ("bio_on", {}),
    ("memory_list", {}),
    ("task_list", {}),
    ("delete", {"count": 3}),
    ("delete", {"mode": "all"}),
    ("bio_set_template", {"template": "{time} | {mood}"}),
    ("bio_set_mood", {"mood": "😊"}),
    ("username_set_template", {"template": "{time} | {mood}"}),
    ("username_set_mood", {"mood": "😊"}),
    ("search", {"query": "notes"}),
    ("web_search", {"query": "weather"}),
    ("save_by_link", {"link": "https://t.me/example/1"}),
    ("retrieve_save", {"save_code": "S0001"}),
    ("settings_get", {"key": "language"}),
    ("task_inspect", {"task_id": 3}),
    ("task_transition", {"task_id": 3, "expected_version": 1, "action": "paused"}),
    ("task_transition", {"task_id": 3, "expected_version": 1, "action_status": "paused"}),
    ("task_delete", {"task_id": 3, "expected_version": 1}),
    ("delete_by_id", {"message_id": 9}),
    ("delete_message_by_id", {"message_id": 9}),
    ("delete_messages_by_ids", {"message_ids": [9, 10]}),
    ("create_task", {"request": "every 5 minutes write hello"}),
    ("memory_store", {"content": "the owner prefers tea"}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("action,arguments", _INCOMPLETE_ACTION_ARGUMENTS)
async def test_incomplete_action_arguments_never_reach_persistence(action, arguments):
    repository = InMemoryTaskRepository()
    service = TaskCreationService(repository, OWNER, _registry())
    with pytest.raises(TaskCreationError):
        await service.create(_action_candidate(action, arguments), REF)
    assert await repository.list_tasks(OWNER) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("action,arguments", _COMPLETE_ACTION_ARGUMENTS)
async def test_complete_action_arguments_still_create(action, arguments):
    repository = InMemoryTaskRepository()
    task = await TaskCreationService(repository, OWNER, _registry()).create(
        _action_candidate(action, arguments), REF
    )
    assert task.id >= 1
    assert [t.id for t in await repository.list_tasks(OWNER)] == [task.id]


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [{"task_id": 0}, {"task_id": -4}, {"task_id": "abc"}])
async def test_declared_positive_id_constraint_is_enforced(arguments):
    repository = InMemoryTaskRepository()
    with pytest.raises(TaskCreationError):
        await TaskCreationService(repository, OWNER, _registry()).create(
            _action_candidate("task_inspect", arguments), REF
        )
    assert await repository.list_tasks(OWNER) == []


@pytest.mark.asyncio
async def test_declared_enum_constraint_is_enforced_for_required_argument():
    repository = InMemoryTaskRepository()
    with pytest.raises(TaskCreationError):
        await TaskCreationService(repository, OWNER, _registry()).create(
            _action_candidate(
                "task_transition",
                {"task_id": 3, "expected_version": 1, "action": "resume"},
            ),
            REF,
        )
    assert await repository.list_tasks(OWNER) == []


@pytest.mark.asyncio
async def test_content_argument_may_be_absent_when_generation_is_authorized():
    """The preparation path supplies content arguments at each occurrence."""
    repository = InMemoryTaskRepository()
    task = await TaskCreationService(repository, OWNER, _registry()).create(
        _action_candidate(
            "memory_store", {}, instruction="every day remember one line about my day"
        ),
        REF,
    )
    assert task.ai_instruction == "every day remember one line about my day"


@pytest.mark.asyncio
async def test_non_content_argument_is_required_even_with_an_instruction():
    repository = InMemoryTaskRepository()
    with pytest.raises(TaskCreationError):
        await TaskCreationService(repository, OWNER, _registry()).create(
            _action_candidate("web_search", {}, instruction="search the web every morning"),
            REF,
        )
    assert await repository.list_tasks(OWNER) == []


# ── Target 2: ai_instruction authorization / non-invention ──


@pytest.mark.asyncio
async def test_provider_invented_ai_instruction_is_not_authorization():
    """A static request is not authorized to become generated content, so a
    provider cannot manufacture authorization by returning ai_instruction."""
    payload = {
        "label": "Hello",
        "schedule_type": "interval",
        "schedule": {"seconds": 60},
        "timezone": "UTC",
        "actions": [{"name": "send_message", "arguments": {"text": "hello"}}],
        "notification_destination": {},
        "ai_instruction": "invent a brand new random quote on every run",
    }
    provider = _Provider(json.dumps(payload))
    provider_manager = _manager(provider)
    repository_manager = dbm.RepositoryManager(supabase_available=False)
    with patch.object(dbm, "get_repository_manager", return_value=repository_manager):
        result = await CreateTaskTool(_context(provider_manager)).execute(
            _context(provider_manager), {"request": "every 1 minute write hello"}
        )

    assert result.success is True, result.message
    task = (await repository_manager.task.list_tasks(OWNER))[0]
    assert task.ai_instruction is None
    assert task.actions == [{"name": "send_message", "arguments": {"text": "hello"}}]


@pytest.mark.asyncio
async def test_authorized_profile_generation_is_grounded_to_the_request():
    """A profile-change request authorizes generation, but the persisted
    instruction is the user's request, never a provider paraphrase."""
    provider = _Provider(json.dumps(_candidate(instruction="write a fresh bio quote")))
    provider_manager = _manager(provider)
    repository_manager = dbm.RepositoryManager(supabase_available=False)
    with patch.object(dbm, "get_repository_manager", return_value=repository_manager):
        result = await CreateTaskTool(_context(provider_manager)).execute(
            _context(provider_manager), {"request": REQUEST}
        )

    assert result.success is True, result.message
    task = (await repository_manager.task.list_tasks(OWNER))[0]
    assert task.ai_instruction == REQUEST


@pytest.mark.asyncio
async def test_ungrounded_instruction_cannot_authorize_profile_content():
    """An empty profile action plus a provider-invented instruction, for a
    request that does not authorize generation, stays incomplete."""
    payload = {
        "label": "Bio",
        "schedule_type": "interval",
        "schedule": {"seconds": 300},
        "timezone": "UTC",
        "actions": [{"name": "bio_set_text", "arguments": {"text": ""}}],
        "notification_destination": {},
        "ai_instruction": "generate a random bio line each run",
    }
    provider = _Provider(json.dumps(payload))
    provider_manager = _manager(provider)
    repository_manager = dbm.RepositoryManager(supabase_available=False)
    with patch.object(dbm, "get_repository_manager", return_value=repository_manager):
        result = await CreateTaskTool(_context(provider_manager)).execute(
            _context(provider_manager), {"request": "every 5 minutes write hello"}
        )

    assert result.success is False
    assert result.data.get("open_taskloom_wizard") is True
    assert await repository_manager.task.list_tasks(OWNER) == []


@pytest.mark.asyncio
async def test_interpreter_drops_unauthorized_ai_instruction():
    """The provider-output boundary applies the same rule as the creation
    boundary, so every model-driven creation path is covered."""
    payload = {
        "label": "Hello",
        "schedule_type": "interval",
        "schedule": {"seconds": 60},
        "timezone": "UTC",
        "actions": [{"name": "send_message", "arguments": {"text": "hello"}}],
        "notification_destination": {},
        "ai_instruction": "invent a random quote on each run",
    }
    candidate = await TaskInterpreter(
        _manager(_Provider(json.dumps(payload)))
    ).interpret("every 1 minute write hello", timezone="UTC")

    assert candidate.ai_instruction is None


@pytest.mark.asyncio
async def test_interpreter_grounds_authorized_instruction_to_the_request():
    payload = {
        "label": "Bio update",
        "schedule_type": "interval",
        "schedule": {"seconds": 300},
        "timezone": "UTC",
        "actions": [{"name": "bio_set_text", "arguments": {"text": ""}}],
        "notification_destination": {},
        "ai_instruction": "change my bio to anime quotes",
    }
    candidate = await TaskInterpreter(
        _manager(_Provider(json.dumps(payload)))
    ).interpret(REQUEST, timezone="UTC")

    assert candidate.ai_instruction == REQUEST


@pytest.mark.asyncio
async def test_blank_ai_instruction_is_rejected_before_persistence():
    repository = InMemoryTaskRepository()
    with pytest.raises(TaskSemanticCompletenessError):
        await TaskCreationService(repository, OWNER, _registry()).create(
            _candidate(instruction="   "), REF
        )
    assert await repository.list_tasks(OWNER) == []


# ── Target 3: trusted destination enforcement ──


@pytest.mark.asyncio
async def test_interpreter_drops_model_supplied_destination_identifiers():
    payload = {
        "label": "Hello",
        "schedule_type": "interval",
        "schedule": {"seconds": 60},
        "timezone": "UTC",
        "actions": [{"name": "send_message", "arguments": {"text": "hi"}}],
        "notification_destination": {
            "chat_id": 424242,
            "chat_title": "Somewhere else",
            "deliver_result": True,
        },
    }
    candidate = await TaskInterpreter(
        _manager(_Provider(json.dumps(payload)))
    ).interpret("every 1 minute write hi", timezone="UTC")

    assert candidate.notification_destination == {"deliver_result": True}


@pytest.mark.asyncio
async def test_model_supplied_chat_id_is_never_persisted():
    payload = {
        "label": "Bio update",
        "schedule_type": "interval",
        "schedule": {"seconds": 300},
        "timezone": "UTC",
        "actions": [{"name": "send_message", "arguments": {"text": "hello"}}],
        "notification_destination": {"chat_id": 999999},
    }
    provider = _Provider(json.dumps(payload))
    provider_manager = _manager(provider)
    context = _tool_context(provider_manager, None)
    repository_manager = dbm.RepositoryManager(supabase_available=False)
    with patch.object(dbm, "get_repository_manager", return_value=repository_manager):
        result = await CreateTaskTool(context).execute(context, {"request": REQUEST})

    assert result.success is True, result.message
    task = (await repository_manager.task.list_tasks(OWNER))[0]
    assert "chat_id" not in task.notification_destination


@pytest.mark.asyncio
async def test_trusted_request_chat_id_overrides_a_model_supplied_value():
    payload = {
        "label": "Bio update",
        "schedule_type": "interval",
        "schedule": {"seconds": 300},
        "timezone": "UTC",
        "actions": [{"name": "send_message", "arguments": {"text": "hello"}}],
        "notification_destination": {"chat_id": 999999},
    }
    provider = _Provider(json.dumps(payload))
    provider_manager = _manager(provider)
    context = _tool_context(provider_manager, -1001234567890)
    repository_manager = dbm.RepositoryManager(supabase_available=False)
    with patch.object(dbm, "get_repository_manager", return_value=repository_manager):
        result = await CreateTaskTool(context).execute(context, {"request": REQUEST})

    assert result.success is True, result.message
    task = (await repository_manager.task.list_tasks(OWNER))[0]
    assert task.notification_destination.get("chat_id") == -1001234567890


class _DialogsClient:
    async def get_dialogs(self):
        return [{"id": 77, "title": "OskarBeam", "username": "oskar"}]


@pytest.mark.asyncio
async def test_resolved_chat_name_becomes_the_trusted_destination():
    payload = {
        "label": "Bio update",
        "schedule_type": "interval",
        "schedule": {"seconds": 300},
        "timezone": "UTC",
        "actions": [{"name": "send_message", "arguments": {"text": "hello"}}],
        "notification_destination": {"chat_name": "OskarBeam", "chat_id": 999999},
    }
    provider = _Provider(json.dumps(payload))
    provider_manager = _manager(provider)
    context = ToolContext(
        telegram=None, owner_id=OWNER, tz_str="UTC", client=_DialogsClient(),
        extra={"provider_manager": provider_manager, "chat_id": None},
    )
    repository_manager = dbm.RepositoryManager(supabase_available=False)
    with patch.object(dbm, "get_repository_manager", return_value=repository_manager):
        result = await CreateTaskTool(context).execute(context, {"request": REQUEST})

    assert result.success is True, result.message
    task = (await repository_manager.task.list_tasks(OWNER))[0]
    assert task.notification_destination.get("chat_id") == 77
    assert task.notification_destination.get("chat_title") == "OskarBeam"


# ── Targets 4 & 5: scheduled-context and confirmation eligibility ──


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["save", "delete_replied"])
async def test_reply_dependent_actions_cannot_become_scheduled_tasks(action):
    repository = InMemoryTaskRepository()
    with pytest.raises(TaskCreationError):
        await TaskCreationService(repository, OWNER, _registry()).create(
            _action_candidate(action, {}), REF
        )
    assert await repository.list_tasks(OWNER) == []


class _ReplyClient:
    async def get_messages(self, chat_id, ids=None):
        return object()


class _ReplyTelegram:
    client = _ReplyClient()


@pytest.mark.asyncio
async def test_immediate_reply_based_save_still_executes():
    from backend.ai.tools.executor import ToolExecutor
    from backend.services import save_service

    registry = ToolRegistry()
    context = ToolContext(
        telegram=_ReplyTelegram(), owner_id=OWNER, tz_str="UTC",
        extra={"reply_msg": {"chat_id": 5, "message_id": 6}},
    )
    registry.register(SaveTool(context))
    executor = ToolExecutor(registry, context)
    with patch.object(save_service, "execute_save", new=AsyncMock(return_value="✅ Saved S0001")):
        results = await executor.execute_calls(
            [{"name": "save", "arguments": {}}], owner_id=OWNER
        )

    assert results[0].needs_confirmation is False
    assert results[0].success is True


@pytest.mark.asyncio
async def test_scheduled_settings_set_is_rejected_before_persistence():
    repository = InMemoryTaskRepository()
    with pytest.raises(TaskCreationError):
        await TaskCreationService(repository, OWNER, _registry()).create(
            _action_candidate("settings_set", {"key": "language", "value": "en"}), REF
        )
    assert await repository.list_tasks(OWNER) == []


@pytest.mark.asyncio
async def test_immediate_confirmed_settings_set_is_unchanged():
    from backend.ai.tools.executor import ToolExecutor
    from backend.services import settings_service

    context = ToolContext(telegram=None, owner_id=OWNER, tz_str="UTC")
    executor = ToolExecutor(_registry(), context)
    call = {"name": "settings_set", "arguments": {"key": "language", "value": "en"}}

    blocked = await executor.execute_calls([call], owner_id=OWNER)
    assert blocked[0].needs_confirmation is True
    assert blocked[0].error == "confirmation_required"

    with patch.object(settings_service, "set_setting", return_value=True) as set_setting:
        confirmed = await executor.execute_confirmed(call, owner_id=OWNER)
    assert confirmed.success is True
    set_setting.assert_called_once_with("language", "en")


# ── Target 6: unregistered actions ──


@pytest.mark.asyncio
async def test_unregistered_action_is_rejected_before_persistence():
    repository = InMemoryTaskRepository()
    with pytest.raises(TaskCreationError):
        await TaskCreationService(repository, OWNER, _registry()).create(
            _action_candidate("mystery_tool", {"x": 1}), REF
        )
    assert await repository.list_tasks(OWNER) == []


@pytest.mark.asyncio
async def test_without_an_attached_registry_no_registry_is_invented(monkeypatch):
    """With no runtime-attached registry there is no authority to consult;
    the coordinator's own registry check stays the backstop."""
    monkeypatch.setattr(task_creation, "_attached_tool_registry", lambda: None)
    repository = InMemoryTaskRepository()
    task = await TaskCreationService(repository, OWNER).create(
        _action_candidate("mystery_tool", {"x": 1}), REF
    )
    assert task.id >= 1
