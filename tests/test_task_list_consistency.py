"""Task-list consistency: created tasks must be visible, and a degraded store
must never be presented as authoritative.

Live symptom this file locks down: a task reported as created
(``✅ Task #3 created``) was missing from the task list requested two minutes
later, which showed an older durable task (#27) instead. The created task
carried a FRESH LOW id although the durable table already held #27, which is
the signature of a write that degraded into the process-local in-memory
fallback — and that degradation was never reported.

Covered here:
  A. create then immediately list with the SAME owner -> the task appears.
  B. multiple active tasks all appear.
  C. deleted tasks stay out of the normal list.
  D. owner isolation: owner A's task never appears for owner B.
  E. a failed durable read is flagged, never presented as an authoritative
     (possibly empty) durable list.
  F. a task_list tool result is verbatim-authoritative: the model cannot
     replace the real result with fabricated prose.
  G. task_count always matches the ids actually returned/rendered.
Plus: one repository read per list (no double read), and an honest
non-durable creation report.
"""
from __future__ import annotations

import re
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.ai.database import manager as dbm
from backend.ai.database.task_repository import InMemoryTaskRepository, SupabaseTaskRepository
from backend.ai.task_management import TaskManagementService
from backend.ai.task_management_interface import FALLBACK_NOTE, list_text
from backend.ai.tools.context import ToolContext
from backend.ai.tools.executor import ToolExecutor
from backend.ai.tools.registry import create_default_registry

OWNER = 777
OTHER = 888

_TASK_ID_RE = re.compile(r"Task #(\d+)")


def task_data(**overrides):
    data = {
        "label": "consistency",
        "schedule_type": "interval",
        "schedule": {"seconds": 3600},
        "timezone": "Asia/Tehran",
        "actions": [{"name": "send_message", "arguments": {"text": "hello"}}],
        "notification_destination": {},
    }
    data.update(overrides)
    return data


def row_task(**overrides):
    row = {
        "id": 27,
        "owner_id": OWNER,
        "label": "durable old task",
        "status": "active",
        "version": 1,
        "schedule_type": "interval",
        "schedule": {"seconds": 300},
        "timezone": "Asia/Tehran",
        "next_run_at": None,
        "actions": [{"name": "send_message", "arguments": {"text": "hi"}}],
        "notification_destination": {},
        "created_at": "2026-09-01T08:00:00+00:00",
        "updated_at": "2026-09-01T08:00:00+00:00",
        "terminal_at": None,
    }
    row.update(overrides)
    return row


class _FakeQuery:
    """Minimal PostgREST-shaped query double (insert / select / update)."""

    def __init__(self, client, table_name):
        self.client, self.table_name = client, table_name
        self.filters, self.payload, self.operation = [], {}, None
        self.single = False

    def select(self, *_args, **_kwargs):
        self.operation = "select"
        return self

    def insert(self, payload):
        self.payload, self.operation = payload, "insert"
        return self

    def update(self, payload):
        self.payload, self.operation = payload, "update"
        return self

    def eq(self, key, value):
        self.filters.append((key, value))
        return self

    def order(self, *_args, **_kwargs):
        return self

    def limit(self, value):
        return self

    def maybe_single(self):
        self.single = True
        return self

    def execute(self):
        self.client.last_query = self
        if self.operation == "insert" and self.client.fail_insert:
            raise RuntimeError("insert unavailable")
        if self.operation == "update" and self.client.fail_update:
            raise RuntimeError("update unavailable")
        if self.operation == "select" and self.client.fail_select:
            raise RuntimeError("read unavailable")
        if self.operation == "select" and self.client.fail_select_times > 0:
            self.client.fail_select_times -= 1
            raise RuntimeError("read unavailable")
        rows = self.client.rows[self.table_name]
        matches = [
            r for r in rows
            if all(r.get(k) == v for k, v in self.filters)
        ]
        if self.operation == "insert":
            row = dict(self.payload)
            row.setdefault("id", self.client.next_id[self.table_name])
            self.client.next_id[self.table_name] += 1
            # Column defaults the real table applies on insert (version,
            # timestamps, status, ai_instruction).
            row.setdefault("status", "active")
            row.setdefault("version", 1)
            row.setdefault("terminal_at", None)
            row.setdefault("ai_instruction", None)
            row.setdefault("created_at", "2026-09-11T10:56:00+00:00")
            row.setdefault("updated_at", "2026-09-11T10:56:00+00:00")
            rows.append(row)
            matches = [row]
        elif self.operation == "update":
            for row in matches:
                row.update(self.payload)
        data = matches[0] if self.single else matches
        return SimpleNamespace(data=data)


class _FakeClient:
    def __init__(self, task_rows=None, fail_insert=False, fail_select=False,
                 fail_select_times=0, fail_update=False):
        self.rows = {
            "ai_tasks": [dict(r) for r in (task_rows or [])],
            "ai_task_occurrences": [],
        }
        self.next_id = {"ai_tasks": 28, "ai_task_occurrences": 1}
        self.fail_insert = fail_insert
        self.fail_select = fail_select
        # Fail only the next N reads (models a transient outage that recovers
        # between two reads of the same logical list request).
        self.fail_select_times = fail_select_times
        self.fail_update = fail_update
        self.last_query = None

    def table(self, name):
        return _FakeQuery(self, name)


class _CountingRepository(InMemoryTaskRepository):
    """In-memory repository that records how many list reads it served."""

    def __init__(self):
        super().__init__()
        self.list_calls = 0

    async def list_tasks(self, owner_id):
        self.list_calls += 1
        return await super().list_tasks(owner_id)


def _manager(repo):
    return SimpleNamespace(task=repo)


async def _run_tool(repo, owner_id, tool_name="task_list", arguments=None):
    ctx = ToolContext(telegram=None, owner_id=owner_id, tz_str="UTC", extra={})
    registry = create_default_registry(ctx)
    executor = ToolExecutor(registry, ctx)
    with patch.object(dbm, "get_repository_manager", return_value=_manager(repo)):
        results = await executor.execute_calls(
            [{"name": tool_name, "arguments": arguments or {}}], owner_id=owner_id,
        )
    return results[0]


# ── A: a created task is visible to the very next list ──────────────────────


@pytest.mark.asyncio
async def test_created_task_appears_in_the_next_list():
    repo = InMemoryTaskRepository()
    created = await repo.create_task(OWNER, task_data(label="bio update"))

    result = await _run_tool(repo, OWNER)

    assert result.success is True
    assert f"Task #{created.id}" in result.message
    assert "bio update" in result.message
    assert created.id in result.data["task_ids"]
    assert result.data["task_count"] == 1


# ── B: every active task appears ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_multiple_active_tasks_all_appear():
    repo = InMemoryTaskRepository()
    first = await repo.create_task(OWNER, task_data(label="one"))
    second = await repo.create_task(OWNER, task_data(label="two"))
    third = await repo.create_task(OWNER, task_data(label="three"))

    result = await _run_tool(repo, OWNER)

    assert result.success is True
    assert result.data["task_ids"] == [first.id, second.id, third.id]
    assert result.data["task_count"] == 3
    for task in (first, second, third):
        assert f"Task #{task.id}" in result.message


# ── C: deleted tasks stay out of the normal list ────────────────────────────


@pytest.mark.asyncio
async def test_deleted_task_stays_out_of_the_next_list():
    repo = InMemoryTaskRepository()
    kept = await repo.create_task(OWNER, task_data(label="kept"))
    doomed = await repo.create_task(OWNER, task_data(label="doomed"))
    service = TaskManagementService(repo, OWNER)
    await service.delete(doomed.id, expected_version=doomed.version)

    result = await _run_tool(repo, OWNER)

    assert result.data["task_ids"] == [kept.id]
    assert "kept" in result.message
    assert "doomed" not in result.message


# ── D: owner isolation ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_owner_isolation_holds_for_created_tasks():
    repo = InMemoryTaskRepository()
    mine = await repo.create_task(OWNER, task_data(label="mine"))
    theirs = await repo.create_task(OTHER, task_data(label="theirs"))

    result = await _run_tool(repo, OWNER)

    assert result.data["task_ids"] == [mine.id]
    assert "mine" in result.message
    assert "theirs" not in result.message
    assert theirs.id not in result.data["task_ids"]


# ── E: a failed durable read is flagged, never silently authoritative ────────


@pytest.mark.asyncio
async def test_failed_durable_read_is_flagged_not_authoritative():
    client = _FakeClient([row_task(label="durable task")])
    repo = SupabaseTaskRepository(client, InMemoryTaskRepository())
    service = TaskManagementService(repo, OWNER)

    # Healthy read sees the durable task.
    healthy = await list_text(service)
    assert "durable task" in healthy
    assert FALLBACK_NOTE not in healthy

    # The durable store becomes unreadable: the degraded view must announce
    # itself instead of masquerading as the authoritative task list.
    client.fail_select = True
    degraded = await list_text(service)
    assert FALLBACK_NOTE in degraded
    assert "durable task" not in degraded

    result = await _run_tool(repo, OWNER)
    assert result.data["fallback_active"] is True
    assert FALLBACK_NOTE in result.message
    # The empty fallback list is reported as a degraded view, not as truth.
    assert "No tasks found." in result.message


# ── F: the task_list result is authoritative, never model prose ─────────────


@pytest.mark.asyncio
async def test_task_list_result_is_verbatim_authoritative():
    """A native task_list round must return the real result, not narration.

    The provider's second round would say "it may have completed or been
    removed" while the fresh tool result says otherwise — the continuation
    round must never happen for a deterministic task read.
    """
    from typing import Any

    from backend.ai.engine.dispatcher import Dispatcher
    from backend.ai.engine.hooks import NOOP_HOOKS
    from backend.ai.engine.metrics import EngineMetrics
    from backend.ai.providers.base.capabilities import ProviderCapabilities
    from backend.ai.providers.base.config import ProviderConfig
    from backend.ai.providers.base.contract import BaseProvider, ProviderResponse
    from backend.ai.providers.manager.manager import ProviderManager
    from backend.ai.session.request import AIRequest
    from backend.ai.tools.executor import ToolExecutionResult

    # The contract itself: a task_list-only round is authoritative.
    calls = [{"name": "task_list", "arguments": {}}]
    ok = [ToolExecutionResult(tool_name="task_list", success=True, message="Tasks\n\nTask #3")]
    assert Dispatcher._read_results_authoritative(calls, ok) is True
    failed = [ToolExecutionResult(tool_name="task_list", success=False, message="x")]
    assert Dispatcher._read_results_authoritative(calls, failed) is False
    mixed = ok + [ToolExecutionResult(tool_name="search", success=True, message="y")]
    assert Dispatcher._read_results_authoritative(calls + [{"name": "search"}], mixed) is False

    repo = InMemoryTaskRepository()
    created = await repo.create_task(OWNER, task_data(label="bio update"))

    class NarratingProvider(BaseProvider):
        """Emits the native task_list call, then WOULD fabricate a summary."""

        def __init__(self) -> None:
            super().__init__(ProviderConfig(provider_name="narrator", enabled=True, default_model="m"))
            self.calls = 0

        @property
        def name(self) -> str:
            return "narrator"

        @property
        def capabilities(self) -> ProviderCapabilities:
            return ProviderCapabilities(supports_tools=True, supports_function_call=True)

        async def chat(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return ProviderResponse(
                    text="", provider_name=self.name, success=True,
                    tool_calls=[{"name": "task_list", "arguments": {}}],
                )
            return ProviderResponse(
                text=(
                    "Only one active task right now. The bio task doesn't appear "
                    "in the active list — it may have completed or been removed."
                ),
                provider_name=self.name,
                success=True,
            )

        def initialize(self) -> None:
            return None

        def shutdown(self) -> None:
            return None

        def count_tokens(self, text: str) -> int:
            return max(1, len(text) // 4)

        def health(self) -> dict[str, Any]:
            return {"healthy": True}

    ctx = ToolContext(telegram=None, owner_id=OWNER, tz_str="UTC", extra={})
    registry = create_default_registry(ctx)
    executor = ToolExecutor(registry, ctx)
    provider = NarratingProvider()

    pm = ProviderManager()
    pm.register_provider(provider)
    pm.switch_provider(provider.name)
    pm._fallback_chain = []

    from unittest.mock import AsyncMock, MagicMock

    mock_conv = MagicMock()
    mock_sess = MagicMock()
    mock_sess.session_id = "s"
    mock_sess.owner_id = OWNER
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
    pp.user_input = "list my active tasks"
    pp.estimated_tokens.estimated_input_tokens = 50
    pp.estimated_tokens.prompt_size_chars = 100
    mock_pb.build.return_value = pp

    d = Dispatcher(mock_conv, mock_pb, pm, NOOP_HOOKS, EngineMetrics(), tool_executor=executor)

    with patch.object(dbm, "get_repository_manager", return_value=_manager(repo)):
        result = await d.dispatch(AIRequest(
            session_id="s1", message_id=9, owner_id=OWNER,
            user_message="list my active tasks", chat_id=456,
        ))

    assert result.success is True
    assert f"Task #{created.id}" in result.response
    assert "bio update" in result.response
    # A task-list round is given ONE continuation round, because the same read
    # tools are the prerequisite for a CAS-guarded mutation (task_list ->
    # task_transition / task_delete). The guarantee under test is unchanged and
    # is what matters: the continuation's fabricated narration NEVER reaches
    # the owner — the real tool result is delivered verbatim instead.
    assert provider.calls == 2
    assert "may have completed or been removed" not in result.response


# ── G: task_count always matches the returned ids ───────────────────────────


@pytest.mark.asyncio
async def test_task_count_matches_returned_ids_in_one_snapshot():
    repo = InMemoryTaskRepository()
    first = await repo.create_task(OWNER, task_data(label="one"))
    second = await repo.create_task(OWNER, task_data(label="two"))
    doomed = await repo.create_task(OWNER, task_data(label="doomed"))
    await TaskManagementService(repo, OWNER).delete(doomed.id, expected_version=doomed.version)

    result = await _run_tool(repo, OWNER)

    rendered_ids = [int(v) for v in _TASK_ID_RE.findall(result.message)]
    assert rendered_ids == [first.id, second.id]
    assert result.data["task_ids"] == rendered_ids
    assert result.data["task_count"] == len(rendered_ids) == 2

    # Same invariant on a degraded snapshot.
    client = _FakeClient([row_task()], fail_select=True)
    degraded_repo = SupabaseTaskRepository(client, InMemoryTaskRepository())
    result = await _run_tool(degraded_repo, OWNER)
    assert result.data["task_count"] == 0 == len(result.data["task_ids"])
    assert _TASK_ID_RE.findall(result.message) == []
    assert result.data["fallback_active"] is True


@pytest.mark.asyncio
async def test_degraded_then_healthy_reads_cannot_split_the_snapshot():
    """A transient outage that recovers mid-request must not mix two stores.

    Pre-fix the tool read twice: the first (failed) read supplied the count
    while the second (healthy) read supplied the rendered list — reporting an
    empty count beside a real task and dropping the degraded marker.
    """
    client = _FakeClient([row_task(label="durable task")], fail_select_times=1)
    repo = SupabaseTaskRepository(client, InMemoryTaskRepository())

    result = await _run_tool(repo, OWNER)

    assert result.data["task_count"] == len(result.data["task_ids"]) == 0
    assert _TASK_ID_RE.findall(result.message) == []
    assert result.data["fallback_active"] is True
    assert FALLBACK_NOTE in result.message
    # The second read never happened: the state is resolved in one snapshot.
    assert client.fail_select_times == 0


@pytest.mark.asyncio
async def test_task_list_reads_the_repository_once():
    """One authoritative snapshot per list — no count/content divergence."""
    repo = _CountingRepository()
    await repo.create_task(OWNER, task_data(label="one"))

    result = await _run_tool(repo, OWNER)

    assert result.data["task_count"] == 1
    assert repo.list_calls == 1

    # An explicit snapshot must be rendered as-is, with no extra read.
    repo.list_calls = 0
    snapshot = await TaskManagementService(repo, OWNER).snapshot()
    assert await list_text(TaskManagementService(repo, OWNER), snapshot=snapshot)
    assert repo.list_calls == 1


# ── The live root cause: a degraded WRITE reported as a durable success ─────


def _candidate_json(label="bio update", text="hello"):
    import json as _json

    return _json.dumps(
        {
            "label": label,
            "schedule_type": "interval",
            "schedule": {"seconds": 300},
            "timezone": "UTC",
            "actions": [{"name": "send_message", "arguments": {"text": text}}],
            "notification_destination": {},
        },
        ensure_ascii=False,
    )


def _provider_manager_with(response_text):
    from backend.ai.providers.base.capabilities import ProviderCapabilities
    from backend.ai.providers.base.config import ProviderConfig
    from backend.ai.providers.base.contract import BaseProvider, ProviderResponse
    from backend.ai.providers.manager.manager import ProviderManager

    class _FakeProvider(BaseProvider):
        def __init__(self):
            super().__init__(ProviderConfig(provider_name="fake", enabled=True, default_model="m"))

        @property
        def name(self):
            return "fake"

        @property
        def capabilities(self):
            return ProviderCapabilities(supports_tools=True, supports_function_call=True)

        async def chat(self, messages, **kwargs):
            return ProviderResponse(text=response_text, provider_name="fake", success=True)

        def initialize(self):
            return None

        def shutdown(self):
            return None

        def count_tokens(self, text):
            return max(1, len(text) // 4)

        def health(self):
            return {"healthy": True}

    pm = ProviderManager()
    provider = _FakeProvider()
    pm.register_provider(provider)
    pm.switch_provider(provider.name)
    pm._fallback_chain = []
    return pm


async def _create_task_via_tool(repo, request, candidate):
    from backend.ai.tools.task import CreateTaskTool

    pm = _provider_manager_with(candidate)
    ctx = ToolContext(
        telegram=None, owner_id=OWNER, tz_str="UTC", client=None,
        extra={"provider_manager": pm, "chat_id": -1001},
    )
    with patch.object(dbm, "get_repository_manager", return_value=_manager(repo)):
        return await CreateTaskTool(ctx).execute(ctx, {"request": request})


@pytest.mark.asyncio
async def test_non_durable_creation_is_reported_honestly():
    """The live root cause: an in-memory (non-durable) create must say so.

    The durable store already holds #27, so a durable insert can never return
    a low id. A low id therefore proves the write degraded — and that state
    used to be reported as a plain, durable-looking success.
    """
    client = _FakeClient([row_task()], fail_insert=True)
    repo = SupabaseTaskRepository(client, InMemoryTaskRepository())

    result = await _create_task_via_tool(repo, "هر 5 دقیقه بنویس سلام", _candidate_json())

    assert result.success is True
    assert result.data["durable"] is False
    assert result.data["fallback_backend"] == "InMemoryTaskRepository"
    assert result.data["task_id"] != 27  # fresh process-local counter, not bigserial
    assert FALLBACK_NOTE in result.message

    # The task exists, but only inside the degraded in-memory view — which
    # announces itself instead of looking like the durable list.
    client.fail_select = True
    degraded = await _run_tool(repo, OWNER)
    assert "bio update" in degraded.message
    assert degraded.data["fallback_active"] is True
    assert FALLBACK_NOTE in degraded.message

    # A healthy durable read cannot see it (the exact live symptom). Because
    # the creation was reported honestly, no durable success was ever claimed
    # for a task the durable store never received.
    client.fail_insert = False
    client.fail_select = False
    durable = await TaskManagementService(repo, OWNER).snapshot()
    assert durable.fallback_active is False
    assert [t.label for t in durable.tasks] == ["durable old task"]


@pytest.mark.asyncio
async def test_durable_creation_is_reported_and_listed_as_durable():
    """A healthy Supabase create is reported durable and appears in the next list."""
    client = _FakeClient([row_task(label="older task")])
    repo = SupabaseTaskRepository(client, InMemoryTaskRepository())

    result = await _create_task_via_tool(repo, "هر 5 دقیقه بنویس سلام", _candidate_json())
    assert result.success is True
    assert result.data["durable"] is True
    assert "fallback_backend" not in result.data
    assert FALLBACK_NOTE not in result.message

    listed = await _run_tool(repo, OWNER)
    assert result.data["task_id"] in listed.data["task_ids"]
    assert listed.data["task_count"] == 2
    assert listed.data["fallback_active"] is False
    assert f"Task #{result.data['task_id']}" in listed.message


# ── H: a degraded transition is reported honestly, never as durable ─────────


async def _pause_via_tool(repo, task_id, version):
    from backend.ai.tools.task_management_tools import TaskTransitionTool

    ctx = ToolContext(telegram=None, owner_id=OWNER, tz_str="UTC", extra={})
    with patch.object(dbm, "get_repository_manager", return_value=_manager(repo)):
        return await TaskTransitionTool(ctx).execute(
            ctx,
            {"task_id": task_id, "action": "paused", "expected_version": version},
        )


@pytest.mark.asyncio
async def test_non_durable_transition_is_reported_honestly():
    """Sibling of the create-path root cause: a Supabase update that degrades
    into the in-memory fallback must say so, exactly like a degraded create.

    A pause that only landed in memory is silently reverted by the next healthy
    durable read or a restart — reporting it as a plain durable success is the
    same false persistence claim this file exists to prevent.
    """
    client = _FakeClient([row_task(label="durable old task")], fail_insert=True)
    repo = SupabaseTaskRepository(client, InMemoryTaskRepository())
    created = await _create_task_via_tool(repo, "هر 5 دقیقه بنویس سلام", _candidate_json())
    assert created.data["durable"] is False  # task lives ONLY in the fallback
    task_id = created.data["task_id"]

    # Supabase stays degraded: the read serves the fallback copy, the write
    # degrades into the same fallback — and the result must not look durable.
    client.fail_select = True
    client.fail_update = True
    result = await _pause_via_tool(repo, task_id, 1)

    assert result.success is True  # the fallback architecture still succeeds
    assert result.data["durable"] is False
    assert result.data["fallback_backend"] == "InMemoryTaskRepository"
    assert FALLBACK_NOTE in result.message
    assert result.data["status"] == "paused"

    # Once Supabase recovers, a healthy durable read proves the pause was
    # never durable: the durable store never saw the task at all.
    client.fail_select = False
    client.fail_update = False
    durable = await TaskManagementService(repo, OWNER).snapshot()
    assert durable.fallback_active is False
    assert [t.label for t in durable.tasks] == ["durable old task"]


@pytest.mark.asyncio
async def test_durable_transition_is_reported_as_durable():
    """A healthy Supabase update is reported durable with no fallback note."""
    client = _FakeClient([row_task(label="bio task")])
    repo = SupabaseTaskRepository(client, InMemoryTaskRepository())

    result = await _pause_via_tool(repo, 27, 1)

    assert result.success is True
    assert result.data["durable"] is True
    assert "fallback_backend" not in result.data
    assert FALLBACK_NOTE not in result.message
    assert result.data["status"] == "paused"
    assert result.data["version"] == 2
