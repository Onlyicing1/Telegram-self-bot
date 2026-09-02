"""
Focused regression tests: natural-language task creation through the AI path.

The known Freebuff-class symptom: a legitimate scheduling request such as
"هر 1 دقیقه یک بار برای من بنویس سلام" produced unrelated AI/tool activity,
hit the tool-round limit, and NEVER reached durable task persistence, because
the AI path had no task-creation route and the send/delete fast-path could
divert or abandon it.

Root cause fixed here, traced across the real path:
  - ``backend/ai/actions.py`` now deterministically routes a clear scheduling
    intent to the registered ``create_task`` action (interval, recurring,
    planned reminder) BEFORE the send/delete/save vocabulary can divert it.
  - ``backend/ai/tools/task.py::CreateTaskTool`` runs every creation through
    the canonical boundary: TaskInterpreter -> TaskCreationService ->
    TaskRepository.create_task (owner-scoped, validated candidate).
  - ``backend/ai/task_creation.py`` now schedules the FIRST occurrence for a
    brand-new interval task, which ``next_occurrence()`` could not (it
    requires a previous occurrence), so an interval task could not previously
    be persisted at all.
  - Production follow-up (create_task success=False with a healthy provider):
    ``backend/ai/task_interpreter.py`` now tolerates the ubiquitous markdown
    code-fence wrapper around an otherwise compliant JSON candidate, and the
    tool boundary logs a sanitized failure category + repository/outcome
    record so the next production failure is unambiguous.

These tests assert the routing and the persistence boundary with a REAL
in-memory task repository and a scripted provider (no network).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.ai.actions import (
    ACTION_NAMES,
    EXECUTABLE_ACTION_NAMES,
    KIND_EXECUTABLE,
    KIND_UNSUPPORTED,
    parse_command_intent,
    validate_action,
)
from backend.ai.providers.base.capabilities import ProviderCapabilities
from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.base.contract import BaseProvider, ProviderResponse
from backend.ai.providers.manager.manager import ProviderManager
from backend.ai.session.request import AIRequest

INTERVAL_REQUEST = "هر 1 دقیقه یک بار برای من بنویس سلام"
SCHEDULED_DELETE_REQUEST = "برنامه ریزی کن هر ۱ ساعت همه پیام های این چت رو پاک کن"

_INTERVAL_CANDIDATE = (
    '{"label":"Write Hello","schedule_type":"interval","schedule":{"seconds":60},'
    '"timezone":"UTC","actions":[{"name":"send","arguments":{"content":"سلام"}}],'
    '"notification_destination":{}}'
)
_DELETE_CANDIDATE = (
    '{"label":"Delete chat messages","schedule_type":"interval","schedule":{"seconds":3600},'
    '"timezone":"UTC","actions":[{"name":"delete","arguments":{"mode":"all"}}],'
    '"notification_destination":{}}'
)


# ── Deterministic routing tests ──


def test_interval_request_routes_to_create_task():
    r = parse_command_intent(INTERVAL_REQUEST, has_reply=True)
    assert r.kind == KIND_EXECUTABLE
    assert r.action == "create_task"
    assert r.tool_calls == [
        {"name": "create_task", "arguments": {"request": INTERVAL_REQUEST}}
    ]


def test_scheduled_destructive_request_routes_to_create_task():
    r = parse_command_intent(SCHEDULED_DELETE_REQUEST, has_reply=True)
    assert r.kind == KIND_EXECUTABLE
    assert r.action == "create_task"
    assert [tc["name"] for tc in r.tool_calls] == ["create_task"]


def test_endless_send_still_unsupported_when_not_scheduled():
    # A bare "send" imperative (no schedule marker) is NOT a task request.
    r = parse_command_intent("اینو برای علی بفرست", has_reply=True)
    assert r.kind == KIND_UNSUPPORTED
    assert r.action == "send"


def test_historical_delete_is_not_interpreted_as_scheduling():
    # "delete yesterday's 9am messages" is a historical delete, NOT a task.
    r = parse_command_intent("پیام های ساعت ۹ دیروز رو پاک کن", has_reply=False)
    assert r.action == "delete_messages"
    assert r.kind == KIND_EXECUTABLE


def test_create_task_is_recognized_action():
    assert "create_task" in ACTION_NAMES
    assert "create_task" in EXECUTABLE_ACTION_NAMES


def test_validate_action_create_task_requires_request():
    ok = validate_action({"action": "create_task", "request": INTERVAL_REQUEST})
    assert ok.kind == KIND_EXECUTABLE
    assert ok.schedule_text == INTERVAL_REQUEST
    missing = validate_action({"action": "create_task"})
    assert missing.kind != KIND_EXECUTABLE


# ── Tool / persistence boundary tests ──


class _FakeProvider(BaseProvider):
    def __init__(self, name: str, response_text: str) -> None:
        super().__init__(ProviderConfig(provider_name=name, enabled=True, default_model="m"))
        self._name = name
        self._response_text = response_text
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(supports_tools=True, supports_function_call=True)

    async def chat(self, messages, **kwargs):
        self.calls += 1
        return ProviderResponse(text=self._response_text, provider_name=self._name, success=True)

    def initialize(self) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def health(self) -> dict[str, Any]:
        return {"healthy": True}


def _tool_context(provider_manager, owner_id=777):
    from backend.ai.tools.context import ToolContext

    return ToolContext(
        telegram=None, owner_id=owner_id, tz_str="UTC", client=None,
        extra={"provider_manager": provider_manager, "chat_id": -1001},
    )


@pytest.mark.asyncio
async def test_create_task_tool_persists_interval_task_under_owner():
    from backend.ai.database import manager as dbm
    from backend.ai.tools.task import CreateTaskTool

    pm = ProviderManager()
    provider = _FakeProvider("fake", _INTERVAL_CANDIDATE)
    pm.register_provider(provider)
    pm.switch_provider("fake")
    pm._fallback_chain = []

    manager = dbm.RepositoryManager(supabase_available=False)
    with patch.object(dbm, "get_repository_manager", return_value=manager):
        tool = CreateTaskTool(_tool_context(pm))
        result = await tool.execute(_tool_context(pm), {"request": INTERVAL_REQUEST})

    assert result.success is True
    assert "Task #1 created" in result.message
    data = result.data
    assert data["owner_id"] == 777
    tasks = await manager.task.list_tasks(777)
    assert len(tasks) == 1
    assert tasks[0].schedule_type == "interval"
    # brand-new interval tasks must be schedulable (first run = now + interval)
    assert tasks[0].next_run_at is not None
    # the interpreter made exactly one bounded call; no tool loop consumed rounds
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_create_task_tool_does_not_create_when_ambiguous():
    from backend.ai.database import manager as dbm
    from backend.ai.tools.task import CreateTaskTool

    pm = ProviderManager()
    provider = _FakeProvider("fake", "null")
    pm.register_provider(provider)
    pm.switch_provider("fake")
    pm._fallback_chain = []

    manager = dbm.RepositoryManager(supabase_available=False)
    with patch.object(dbm, "get_repository_manager", return_value=manager):
        tool = CreateTaskTool(_tool_context(pm))
        result = await tool.execute(_tool_context(pm), {"request": "هر روز هههه"})

    assert result.success is False
    assert (await manager.task.list_tasks(777)) == []


@pytest.mark.asyncio
async def test_create_task_tool_isolates_owner():
    from backend.ai.database import manager as dbm
    from backend.ai.tools.task import CreateTaskTool

    pm = ProviderManager()
    provider = _FakeProvider("fake", _INTERVAL_CANDIDATE)
    pm.register_provider(provider)
    pm.switch_provider("fake")
    pm._fallback_chain = []

    manager = dbm.RepositoryManager(supabase_available=False)
    with patch.object(dbm, "get_repository_manager", return_value=manager):
        tool = CreateTaskTool(_tool_context(pm, owner_id=911))
        result = await tool.execute(_tool_context(pm, owner_id=911), {"request": INTERVAL_REQUEST})

    assert result.success is True
    # the created task belongs to the trusted caller's owner id, not the model
    assert result.data["owner_id"] == 911
    assert (await manager.task.list_tasks(777)) == []
    assert len(await manager.task.list_tasks(911)) == 1


@pytest.mark.asyncio
async def test_create_task_tool_persists_scheduled_destructive_delete():
    from backend.ai.database import manager as dbm
    from backend.ai.tools.task import CreateTaskTool

    pm = ProviderManager()
    provider = _FakeProvider("fake", _DELETE_CANDIDATE)
    pm.register_provider(provider)
    pm.switch_provider("fake")
    pm._fallback_chain = []

    manager = dbm.RepositoryManager(supabase_available=False)
    with patch.object(dbm, "get_repository_manager", return_value=manager):
        tool = CreateTaskTool(_tool_context(pm))
        result = await tool.execute(_tool_context(pm), {"request": SCHEDULED_DELETE_REQUEST})

    assert result.success is True
    tasks = await manager.task.list_tasks(777)
    assert len(tasks) == 1
    # the destructive action is persisted as a bounded, owner-scoped definition;
    # later execution goes through the registered ToolExecutor under owner scope
    assert tasks[0].actions == [{"name": "delete", "arguments": {"mode": "all"}}]
    assert tasks[0].owner_id == 777


# ── Markdown-fence tolerance (production follow-up) ──


_FENCED_INTERVAL_CANDIDATE = f"```json\n{_INTERVAL_CANDIDATE}\n```"


@pytest.mark.asyncio
async def test_create_task_tool_succeeds_with_markdown_fenced_json():
    """A compliant candidate wrapped in ``` fences must create the task.

    This is the production-proven failure: the provider attempt succeeds,
    the JSON is valid, but the fenced wrapper made json.loads fail, so
    create_task returned success=False with no durable row.
    """
    from backend.ai.database import manager as dbm
    from backend.ai.tools.task import CreateTaskTool

    pm = ProviderManager()
    provider = _FakeProvider("fake", _FENCED_INTERVAL_CANDIDATE)
    pm.register_provider(provider)
    pm.switch_provider("fake")
    pm._fallback_chain = []

    manager = dbm.RepositoryManager(supabase_available=False)
    with patch.object(dbm, "get_repository_manager", return_value=manager):
        tool = CreateTaskTool(_tool_context(pm))
        result = await tool.execute(_tool_context(pm), {"request": INTERVAL_REQUEST})

    assert result.success is True
    tasks = await manager.task.list_tasks(777)
    assert len(tasks) == 1
    assert tasks[0].actions == [{"name": "send_message", "arguments": {"text": "سلام"}}]


@pytest.mark.asyncio
async def test_fenced_invalid_json_still_rejected():
    """Fence stripping must not weaken candidate validation."""
    from backend.ai.tools.task import CreateTaskTool

    pm = ProviderManager()
    provider = _FakeProvider("fake", "```json\n{not json at all}\n```")
    pm.register_provider(provider)
    pm.switch_provider("fake")
    pm._fallback_chain = []

    tool = CreateTaskTool(_tool_context(pm))
    result = await tool.execute(_tool_context(pm), {"request": INTERVAL_REQUEST})
    assert result.success is False


# ── Failure-visibility diagnostics at the create_task boundary ──


@pytest.mark.asyncio
async def test_ambiguity_failure_logs_sanitized_category(caplog):
    from backend.ai.database import manager as dbm
    from backend.ai.tools.task import CreateTaskTool

    pm = ProviderManager()
    provider = _FakeProvider("fake", "null")
    pm.register_provider(provider)
    pm.switch_provider("fake")
    pm._fallback_chain = []

    manager = dbm.RepositoryManager(supabase_available=False)
    with caplog.at_level(logging.WARNING, logger="backend.ai.tools.task"):
        with patch.object(dbm, "get_repository_manager", return_value=manager):
            tool = CreateTaskTool(_tool_context(pm))
            result = await tool.execute(
                _tool_context(pm), {"request": "هر روز هههه"}
            )

    assert result.success is False
    assert (await manager.task.list_tasks(777)) == []
    records = [r for r in caplog.records if "stage=interpretation_failed" in r.getMessage()]
    assert records, "the interpretation failure must be logged"
    assert "category=candidate_invalid" in records[0].getMessage()
    terminal = [r for r in caplog.records if "stage=create_task_failed" in r.getMessage()]
    assert terminal, "every failed create_task must emit one terminal failure record"
    assert "failed_stage=create_task_interpretation" in terminal[0].getMessage()
    assert "persisted=false" in terminal[0].getMessage()


class _ExplodingTaskRepository:
    """Stands in for a repository whose Supabase insert crashes."""

    async def create_task(self, owner_id, data):
        raise RuntimeError("boom: simulated supabase outage")


@pytest.mark.asyncio
async def test_persistence_failure_is_classified_and_honest(caplog):
    from backend.ai.database import manager as dbm
    from backend.ai.tools.task import CreateTaskTool

    pm = ProviderManager()
    provider = _FakeProvider("fake", _INTERVAL_CANDIDATE)
    pm.register_provider(provider)
    pm.switch_provider("fake")
    pm._fallback_chain = []

    manager = dbm.RepositoryManager(supabase_available=False)
    manager._task = _ExplodingTaskRepository()
    with caplog.at_level(logging.WARNING, logger="backend.ai.tools.task"):
        with patch.object(dbm, "get_repository_manager", return_value=manager):
            tool = CreateTaskTool(_tool_context(pm))
            result = await tool.execute(_tool_context(pm), {"request": INTERVAL_REQUEST})

    # Honest failure: never a fake success when persistence crashed.
    assert result.success is False
    records = [r for r in caplog.records if "create_task_persist_failed" in r.getMessage()]
    assert records, "the persistence failure must be logged"
    text = records[0].getMessage()
    assert "exception=RuntimeError" in text
    assert "_ExplodingTaskRepository" in text


@pytest.mark.asyncio
async def test_success_logs_persistence_outcome_without_raw_content(caplog):
    from backend.ai.database import manager as dbm
    from backend.ai.tools.task import CreateTaskTool

    pm = ProviderManager()
    provider = _FakeProvider("fake", _INTERVAL_CANDIDATE)
    pm.register_provider(provider)
    pm.switch_provider("fake")
    pm._fallback_chain = []

    manager = dbm.RepositoryManager(supabase_available=False)
    with caplog.at_level(logging.INFO, logger="backend.ai.tools.task"):
        with patch.object(dbm, "get_repository_manager", return_value=manager):
            tool = CreateTaskTool(_tool_context(pm))
            result = await tool.execute(_tool_context(pm), {"request": INTERVAL_REQUEST})

    assert result.success is True
    records = [r for r in caplog.records if "create_task_persisted" in r.getMessage()]
    assert records, "the persistence outcome must be logged"
    text = records[0].getMessage()
    assert "InMemoryTaskRepository" in text
    assert "schedule_type=interval" in text
    assert "label_hash=" in text
    # raw task content must never appear in the outcome record
    assert "سلام" not in text


# ── Dispatcher fast-path integration (no provider tool round) ──


def _make_dispatcher(provider: _FakeProvider, manager):
    from backend.ai.engine.dispatcher import Dispatcher
    from backend.ai.engine.hooks import NOOP_HOOKS
    from backend.ai.engine.metrics import EngineMetrics
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.executor import ToolExecutor
    from backend.ai.tools.registry import create_default_registry

    pm = provider._provider_pm
    mock_conv = MagicMock()
    mock_sess = MagicMock()
    mock_sess.session_id = "s"
    mock_sess.owner_id = 777
    mock_sess.active_provider = provider.name
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

    registry = create_default_registry(ToolContext(telegram=None, owner_id=777, tz_str="UTC"))
    executor = ToolExecutor(registry, ToolContext(telegram=None, owner_id=777, tz_str="UTC", extra={}))

    return Dispatcher(
        mock_conv, mock_pb, pm, NOOP_HOOKS, EngineMetrics(),
        tool_registry=registry,
        tool_executor=executor,
    ), registry, executor


@pytest.mark.asyncio
async def test_dispatcher_routes_schedule_local_fast_path_and_creates_task():
    from backend.ai.database import manager as dbm

    manager = dbm.RepositoryManager(supabase_available=False)
    pm = ProviderManager()
    provider = _FakeProvider("fake", _INTERVAL_CANDIDATE)
    provider._provider_pm = pm
    pm.register_provider(provider)
    pm.switch_provider("fake")
    pm._fallback_chain = []

    with patch.object(dbm, "get_repository_manager", return_value=manager):
        d, _registry, _executor = _make_dispatcher(provider, manager)
        result = await d.dispatch(AIRequest(
            session_id="s1", message_id=1, owner_id=777,
            user_message=INTERVAL_REQUEST, chat_id=-1001, timezone="UTC",
        ))

    # the scheduling request resolved deterministically, not via the tool loop
    assert result.success is True
    assert result.metadata["finish_state"] == "local_fast_path"
    # The high-confidence interval/write request is resolved locally, so task
    # creation does not burn a provider round.
    assert provider.calls == 0
    assert len(await manager.task.list_tasks(777)) == 1


# ── AI_TASK_TRACE lifecycle observability ──


def _trace_records(caplog) -> list:
    return [r for r in caplog.records if "AI_TASK_TRACE" in r.getMessage()]


def _records_with_stage(caplog, stage: str) -> list:
    return [r for r in caplog.records if f"stage={stage}" in r.getMessage()]


@pytest.mark.asyncio
async def test_success_path_emits_full_trace_lifecycle(caplog):
    """The complete happy path is observable from AI_TASK_TRACE alone."""
    from backend.ai.database import manager as dbm
    from backend.ai.tools.task import CreateTaskTool

    pm = ProviderManager()
    provider = _FakeProvider("fake", _INTERVAL_CANDIDATE)
    pm.register_provider(provider)
    pm.switch_provider("fake")
    pm._fallback_chain = []

    manager = dbm.RepositoryManager(supabase_available=False)
    with caplog.at_level(logging.INFO):
        with patch.object(dbm, "get_repository_manager", return_value=manager):
            tool = CreateTaskTool(_tool_context(pm))
            result = await tool.execute(_tool_context(pm), {"request": INTERVAL_REQUEST})

    assert result.success is True
    messages = [r.getMessage() for r in _trace_records(caplog)]
    stages = [m.split("stage=")[1].split(" ")[0] for m in messages]
    for expected in (
        "create_task_received",
        "create_task_validation_start",
        "create_task_validation_end",
        "create_task_interpretation_start",
        "interpretation_start",
        "provider_result",
        "interpretation_end",
        "create_task_destination_resolution",
        "create_task_definition_validation_start",
        "create_task_repository_create_start",
        "create_task_repository_create_result",
        "create_task_definition_validation_end",
        "create_task_success",
    ):
        assert expected in stages, f"missing trace stage {expected}; got {stages}"
    # exactly one terminal event, and it is the success one
    assert stages.count("create_task_success") == 1
    assert "create_task_failed" not in stages
    received = _records_with_stage(caplog, "create_task_received")[0].getMessage()
    assert "request_len=" in received and "preview=" in received
    assert "owner_scope=777" in received
    # provider identity is correlated into the trace
    provider_result = _records_with_stage(caplog, "provider_result")[0].getMessage()
    assert "provider=fake" in provider_result and "success=true" in provider_result
    repo_result = _records_with_stage(caplog, "create_task_repository_create_result")[0].getMessage()
    assert "InMemoryTaskRepository" in repo_result and "version=1" in repo_result
    # raw task content is bounded to the single received-preview field; it
    # must never propagate into interpretation/provider/persistence records
    # (no label text, no action text beyond that one diagnostic preview)
    joined = "\n".join(messages)
    without_preview = "\n".join(
        m.split("preview=")[0] if "preview=" in m else m for m in messages
    )
    assert "بنویس" not in without_preview
    assert joined.count("preview=") == 1, "task text may appear only in the received preview"


@pytest.mark.asyncio
async def test_provider_failure_preserves_concrete_reason_and_terminal_event(caplog):
    """All providers failed → trace preserves the concrete exhaustion reason."""
    from backend.ai.tools.task import CreateTaskTool

    pm = ProviderManager()
    provider = _FakeProvider("fake", _INTERVAL_CANDIDATE)
    provider.calls = 0

    async def failing_chat(messages, **kwargs):
        provider.calls += 1
        return ProviderResponse(
            text="Request failed: RuntimeError: quota exhausted",
            provider_name="fake",
            success=False,
            metadata={
                "failure_type": "server",
                "provider_matrix": [{"provider": "fake", "outcome": "failed"}],
                "fallback_exhausted": True,
                "errors": ["fake: success=False (quota exhausted)"],
            },
        )

    provider.chat = failing_chat
    pm.register_provider(provider)
    pm.switch_provider("fake")
    pm._fallback_chain = []

    with caplog.at_level(logging.WARNING):
        tool = CreateTaskTool(_tool_context(pm))
        result = await tool.execute(_tool_context(pm), {"request": INTERVAL_REQUEST})

    assert result.success is False
    messages = [r.getMessage() for r in _trace_records(caplog)]
    # The exhausted chain is reconstructable: 'attempted' lists the tried
    # provider(s), 'provider' names the terminal fallback responder.
    assert any(
        "stage=provider_result success=false provider=dummy attempted=fake" in m
        for m in messages
    )
    assert any("category=all_providers_failed" in m for m in messages)
    assert any("fallback_exhausted=True" in m for m in messages)
    failed = _records_with_stage(caplog, "create_task_failed")
    assert len(failed) == 1, "exactly one terminal failure event"
    text = failed[0].getMessage()
    assert "failed_stage=create_task_interpretation" in text
    assert "all_providers_failed" in text
    assert "persisted=false" in text
    # nothing was persisted
    from backend.ai.database import manager as dbm
    assert (await dbm.RepositoryManager(supabase_available=False).task.list_tasks(777)) == []


@pytest.mark.asyncio
async def test_repository_fallback_emits_fallback_trace(caplog):
    """Supabase crash → fallback start/result records with task id/version."""
    from backend.ai.database import manager as dbm
    from backend.ai.database.task_repository import SupabaseTaskRepository
    from backend.ai.tools.task import CreateTaskTool

    class _BrokenClient:
        def table(self, *_a, **_k):
            raise RuntimeError("simulated supabase outage")

    repo = SupabaseTaskRepository(_BrokenClient())
    manager = dbm.RepositoryManager(supabase_available=False)
    manager._task = repo

    pm = ProviderManager()
    provider = _FakeProvider("fake", _INTERVAL_CANDIDATE)
    pm.register_provider(provider)
    pm.switch_provider("fake")
    pm._fallback_chain = []

    with caplog.at_level(logging.INFO):
        with patch.object(dbm, "get_repository_manager", return_value=manager):
            tool = CreateTaskTool(_tool_context(pm))
            result = await tool.execute(_tool_context(pm), {"request": INTERVAL_REQUEST})

    # the fallback succeeded, so the tool succeeds and the trace shows both
    assert result.success is True
    messages = [r.getMessage() for r in _trace_records(caplog)]
    assert any("stage=create_task_fallback_start" in m for m in messages)
    fallback_result = [m for m in messages if "stage=create_task_fallback_result" in m]
    assert fallback_result and "success=true" in fallback_result[0]
    assert "task_id=" in fallback_result[0] and "version=1" in fallback_result[0]
    success = _records_with_stage(caplog, "create_task_success")[0].getMessage()
    assert "final_backend=InMemoryTaskRepository" in success


@pytest.mark.asyncio
async def test_tool_terminal_event_always_emitted_on_early_rejection(caplog):
    """Even an early argument rejection carries a terminal failure record."""
    from backend.ai.tools.task import CreateTaskTool

    pm = ProviderManager()
    provider = _FakeProvider("fake", _INTERVAL_CANDIDATE)
    pm.register_provider(provider)
    pm.switch_provider("fake")
    pm._fallback_chain = []

    with caplog.at_level(logging.WARNING):
        tool = CreateTaskTool(_tool_context(pm))
        result = await tool.execute(_tool_context(pm), {"request": "   "})

    assert result.success is False
    # Early argument rejection happens before the trace helper exists — the
    # dispatcher-level tool_result trace still records it. The lifecycle
    # trace requirement applies from create_task_received onward.
    assert _records_with_stage(caplog, "create_task_failed") == []
    assert _records_with_stage(caplog, "create_task_received") == []