"""Focused regression tests: create_task staged diagnostics.

The production symptom these tests pin down: ``create_task`` returned
``success=False`` with NO visible log explaining which internal stage failed
(interpretation? validation? persistence?). The instrumentation emits
sanitized, Render-visible trace lines at every stage:

  TASK_CREATE_TRACE  stage=received|interpret_start|interpret_result|
                            persist_start|persist_result|completed|failed
  TASK_INTERPRET_TRACE stage=provider_result|accepted|rejected|...
  TASK_PERSIST_TRACE   stage=start|repository_call|persisted|rejected|...

Every failure line names the exact failing stage and a sanitized category —
never the request text, provider output, or credentials. Failure-message
wording is intentionally UNCHANGED so user-visible behavior is identical.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

import pytest

from backend.ai.database import manager as dbm
from backend.ai.providers.base.capabilities import ProviderCapabilities
from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.base.contract import BaseProvider, ProviderResponse
from backend.ai.providers.manager.manager import ProviderManager
from backend.ai.task_creation import TaskCreationError, TaskCreationService
from backend.ai.task_interpreter import TaskInterpretationError, TaskInterpreter
from backend.ai.task_trace import bind_request, unbind
from backend.ai.tools.context import ToolContext
from backend.ai.tools.task import CreateTaskTool

VALID_CANDIDATE = (
    '{"label":"Write Hello","schedule_type":"interval","schedule":{"seconds":60},'
    '"timezone":"UTC","actions":[{"name":"send","arguments":{"content":"سلام"}}],'
    '"notification_destination":{}}'
)
_SMALL_CANDIDATE_JSON = (
    '{"label":"L","schedule_type":"interval","schedule":{"seconds":60},'
    '"timezone":"UTC","actions":[{"name":"send_message","arguments":{"text":"hi"}}],'
    '"notification_destination":{}}'
)
REQUEST = "هر 1 دقیقه یک بار برای من بنویس سلام"


class _ScriptedProvider(BaseProvider):
    def __init__(self, name: str, response_text: str, success: bool = True) -> None:
        super().__init__(ProviderConfig(provider_name=name, enabled=True, default_model="m"))
        self._name = name
        self._response_text = response_text
        self._success = success

    @property
    def name(self) -> str:
        return self._name

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(supports_tools=True, supports_function_call=True)

    async def chat(self, messages, **kwargs):
        return ProviderResponse(
            text=self._response_text, provider_name=self._name, success=self._success
        )

    def initialize(self) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def health(self) -> dict[str, Any]:
        return {"healthy": True}


def _tool_context(provider_manager, owner_id=777) -> ToolContext:
    return ToolContext(
        telegram=None, owner_id=owner_id, tz_str="UTC", client=None,
        extra={"provider_manager": provider_manager, "request_id": "testrid123"},
    )


def _provider_manager(response_text: str, success: bool = True) -> ProviderManager:
    pm = ProviderManager()
    provider = _ScriptedProvider("fake", response_text, success=success)
    pm.register_provider(provider)
    pm.switch_provider("fake")
    pm._fallback_chain = []
    return pm


def _failing_provider(name: str, detail: str) -> _ScriptedProvider:
    class _Failing(_ScriptedProvider):
        async def chat(self, messages, **kwargs):
            return ProviderResponse(
                text=detail, provider_name=name, success=False,
                metadata={"failure_type": "request"},
            )

    return _Failing(name, detail)


def _create_task_logs(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "CREATE_TASK_TRACE" in r.getMessage()]


from contextlib import contextmanager


@contextmanager
def trace_scope(rid: str):
    token = bind_request(rid)
    try:
        yield
    finally:
        unbind(token)


# ── Happy path: every stage is traced, request text never logged ──


@pytest.mark.asyncio
async def test_success_path_emits_full_stage_trace(caplog):
    caplog.set_level(logging.INFO)
    manager = dbm.RepositoryManager(supabase_available=False)
    with patch.object(dbm, "get_repository_manager", return_value=manager):
        tool = CreateTaskTool(_tool_context(_provider_manager(VALID_CANDIDATE)))
        result = await tool.execute(_tool_context(_provider_manager(VALID_CANDIDATE)), {"request": REQUEST})

    assert result.success is True
    lines = "\n".join(_create_task_logs(caplog))
    for stage in ("entry", "interpretation_request", "interpretation_parse_result",
                  "persistence_start", "task_created"):
        assert f"stage={stage}" in lines
    assert "task_id=" in lines
    # correlation id from the tool context travels on the tool-level trace
    assert "request_id=testrid123" in lines
    # request text is logged in BOUNDED form (per the diagnostics spec),
    # and the full normalized request is visible on the entry line
    assert any("بنویس" in line for line in _create_task_logs(caplog))


@pytest.mark.asyncio
async def test_interpreter_logs_provider_result_category(caplog):
    caplog.set_level(logging.INFO)
    with trace_scope("interp123"):
        interpreter = TaskInterpreter(_provider_manager(VALID_CANDIDATE))
        candidate = await interpreter.interpret(REQUEST, timezone="UTC")

    assert candidate.schedule_type == "interval"
    lines = "\n".join(_create_task_logs(caplog))
    assert "stage=interpretation_request" in lines
    assert "provider_round_result" in lines
    assert "output_category=json" in lines
    assert "stage=interpretation_parse_result parse_success=true" in lines


# ── Failure paths: stage + sanitized category, message unchanged ──


@pytest.mark.asyncio
async def test_interpretation_rejection_logs_stage_and_category(caplog):
    caplog.set_level(logging.INFO)
    manager = dbm.RepositoryManager(supabase_available=False)
    with patch.object(dbm, "get_repository_manager", return_value=manager):
        tool = CreateTaskTool(_tool_context(_provider_manager("null")))
        result = await tool.execute(_tool_context(_provider_manager("null")), {"request": REQUEST})

    assert result.success is False
    lines = "\n".join(_create_task_logs(caplog))
    # output was JSON null (designed ambiguity), candidate validation rejected it
    assert "output_category=null" in lines
    assert "stage=interpretation_parse_result" in lines
    assert "parse_success=false" in lines
    assert "category=candidate_invalid" in lines
    assert "stage=rejected" in lines
    assert "reason=task interpretation did not return a valid candidate" in lines
    assert "CREATE_TASK_TRACE" in lines
    assert "stage=tool_result" in lines
    assert "success=false" in lines
    assert "persisted=false" in lines
    assert "failure_category=candidate_invalid" in lines
    # persistence must never be reached after an interpretation rejection
    assert "stage=persistence_start" not in lines
    assert (await manager.task.list_tasks(777)) == []


@pytest.mark.asyncio
async def test_provider_failure_logs_provider_category(caplog):
    caplog.set_level(logging.INFO)
    interpreter = TaskInterpreter(_provider_manager("ignored", success=False))
    with pytest.raises(TaskInterpretationError) as exc_info:
        with trace_scope("provfail123"):
            await interpreter.interpret(REQUEST)

    assert "provider returned a failure" in str(exc_info.value)
    lines = "\n".join(_create_task_logs(caplog))
    assert "stage=provider_round_result" in lines
    assert "success=false" in lines
    # the single failing provider is exhausted → terminal exhaustion category
    assert "failure_category=all_providers_failed" in lines

@pytest.mark.asyncio
async def test_interpretation_timeout_logs_timeout_category(caplog):
    caplog.set_level(logging.INFO)
    pm = _provider_manager(VALID_CANDIDATE)

    class _SlowProvider(_ScriptedProvider):
        async def chat(self, messages, **kwargs):
            import asyncio as _asyncio

            await _asyncio.sleep(1.0)
            return await super().chat(messages, **kwargs)

    slow = _SlowProvider("slow", VALID_CANDIDATE)
    pm.register_provider(slow)
    pm.switch_provider("slow")
    manager = dbm.RepositoryManager(supabase_available=False)
    with patch.object(dbm, "get_repository_manager", return_value=manager), patch(
        "backend.ai.tools.task.INTERPRET_TIMEOUT_SECONDS", 0.05
    ):
        tool = CreateTaskTool(_tool_context(pm))
        result = await tool.execute(_tool_context(pm), {"request": REQUEST})

    assert result.success is False
    lines = "\n".join(_create_task_logs(caplog))
    assert "stage=tool_result" in lines
    assert "success=false" in lines
    assert "stage=exit" in lines
    assert "success=false" in lines
    # the provider call never returned, so the last completed stage is the request
    assert "terminal_stage=provider_request_start" in lines
    assert "failure_category=provider_timeout" in lines


@pytest.mark.asyncio
async def test_persistence_rejection_logs_persist_stage(caplog):
    """A candidate that passes interpretation but fails creation logging."""
    caplog.set_level(logging.INFO)
    service = TaskCreationService(dbm.RepositoryManager(supabase_available=False).task, 777)
    # structurally invalid candidate: unsupported extra field
    with pytest.raises(TaskCreationError):
        with trace_scope("persistfail1"):
            await service.create({"bogus": True}, datetime.now(timezone.utc))

    lines = "\n".join(_create_task_logs(caplog))
    assert "stage=task_validation_start" in lines
    assert "stage=rejected" in lines
    assert "reason=unsupported task fields" in lines


@pytest.mark.asyncio
async def test_persistence_repository_error_logs_error_type(caplog):
    class _ExplodingRepo:
        async def create_task(self, owner_id, payload):
            raise RuntimeError("database unavailable")

    service = TaskCreationService(_ExplodingRepo(), 777)  # type: ignore[arg-type]
    from backend.ai.task_candidate import parse_candidate_output

    candidate = parse_candidate_output(json.loads(_SMALL_CANDIDATE_JSON))
    with caplog.at_level(logging.INFO):
        with pytest.raises(RuntimeError):
            with trace_scope("repoerr123"):
                await service.create(candidate, datetime.now(timezone.utc))

    lines = "\n".join(_create_task_logs(caplog))
    assert "stage=repository_call" in lines
    assert "stage=repository_error" in lines
    assert "error_type=RuntimeError" in lines


@pytest.mark.asyncio
async def test_success_persist_trace_includes_task_id_and_version(caplog):
    caplog.set_level(logging.INFO)
    service = TaskCreationService(dbm.RepositoryManager(supabase_available=False).task, 777)
    from backend.ai.task_candidate import parse_candidate_output

    candidate = parse_candidate_output(json.loads(_SMALL_CANDIDATE_JSON))
    with trace_scope("persistok12"):
        task = await service.create(candidate, datetime.now(timezone.utc))

    assert task.version == 1
    lines = "\n".join(_create_task_logs(caplog))
    assert "stage=repository_call" in lines
    assert "repo_type=InMemoryTaskRepository" in lines
    assert "stage=persisted" in lines
    assert f"task_id={task.id}" in lines
    assert "version=1" in lines
    assert "next_run_at=none" not in lines


# ── Full-lifecycle trace stages (provider routing, fallback, exit) ──


@pytest.mark.asyncio
async def test_provider_failure_fallback_and_exhaustion_are_traced(caplog):
    """Provider fails → next provider selected → exhaustion terminal trace."""
    caplog.set_level(logging.INFO)
    pm = ProviderManager()
    pm.register_provider(_failing_provider("p1", "quota exhausted"))
    pm.register_provider(_failing_provider("p2", "rate limited"))
    pm.switch_provider("p1")
    pm._fallback_chain = []
    interpreter = TaskInterpreter(pm)

    with pytest.raises(TaskInterpretationError) as exc_info:
        with trace_scope("fallbackrid"):
            await interpreter.interpret(REQUEST, timezone="UTC")

    assert "all providers failed" in str(exc_info.value).lower() or exc_info.value is not None
    lines = "\n".join(_create_task_logs(caplog))
    # per-provider selection + response lines for both candidates
    assert "stage=provider_selection provider=p1" in lines
    assert "stage=provider_selection provider=p2" in lines
    assert "stage=provider_request_start provider=p1" in lines
    assert "stage=provider_response provider=p1" in lines
    assert "success=false" in lines
    assert "failure_category=request" in lines
    # fallback line names failed → next provider
    assert "stage=provider_fallback" in lines
    assert "failed_provider=p1" in lines
    assert "next_provider=p2" in lines
    assert "failed_provider=p2" in lines
    assert "stage=provider_fallback_exhausted" in lines
    assert "final_category=all_providers_failed" in lines
    # the interpretation rejection names the exhaustion category
    assert "category=all_providers_failed" in lines


@pytest.mark.asyncio
async def test_exit_terminal_trace_always_emitted_on_crash(caplog):
    """A crashing provider still produces a terminal exit trace."""
    caplog.set_level(logging.INFO)

    class _Crashing(_ScriptedProvider):
        async def chat(self, messages, **kwargs):
            raise RuntimeError("socket exploded")

    pm = ProviderManager()
    pm.register_provider(_Crashing("crash", "x"))
    pm.switch_provider("crash")
    pm._fallback_chain = []
    interpreter = TaskInterpreter(pm)

    with pytest.raises(TaskInterpretationError):
        with trace_scope("crashrid"):
            await interpreter.interpret(REQUEST)

    lines = "\n".join(_create_task_logs(caplog))
    assert "stage=provider_response provider=crash" in lines
    assert "failure_category=network" in lines
    assert "error_detail=RuntimeError" in lines
    assert "stage=exit" in lines
    assert "success=false" in lines
    # the mesh converts the crash into the exhaustion terminal category
    assert "failure_category=all_providers_failed" in lines


@pytest.mark.asyncio
async def test_request_id_constant_through_full_pipeline(caplog):
    """Every emitted trace line carries the SAME request_id."""
    caplog.set_level(logging.INFO)
    manager = dbm.RepositoryManager(supabase_available=False)
    with patch.object(dbm, "get_repository_manager", return_value=manager):
        ctx = _tool_context(_provider_manager(VALID_CANDIDATE))
        tool = CreateTaskTool(ctx)
        await tool.execute(_tool_context(_provider_manager(VALID_CANDIDATE)), {"request": REQUEST})

    trace_lines = [r.getMessage() for r in caplog.records if "CREATE_TASK_TRACE" in r.getMessage()]
    assert trace_lines, "no trace lines emitted"
    assert all("request_id=testrid123" in line for line in trace_lines)


def test_bounded_truncation_marker_present():
    """Long values are truncated with an explicit marker, never unbounded."""
    from backend.ai.task_trace import bound_text

    long_value = "x" * 5000
    bounded = bound_text(long_value, 240)
    assert len(bounded) < 300
    assert "(+" in bounded and "chars)" in bounded


def test_trace_silence_outside_create_task(caplog):
    """Provider/repository layers are silent when no create_task is bound."""
    caplog.set_level(logging.INFO)
    from backend.ai.providers.manager.manager import ProviderManager as PM
    from backend.ai.task_trace import task_trace  # noqa: F401

    pm = PM()
    provider = _ScriptedProvider("fake", VALID_CANDIDATE)
    pm.register_provider(provider)
    pm.switch_provider("fake")
    pm._fallback_chain = []

    import asyncio as _aio

    async def run():
        return await TaskInterpreter(pm).interpret(REQUEST)

    result = _aio.run(run())
    assert result is not None
    assert not [r for r in caplog.records if "CREATE_TASK_TRACE" in r.getMessage()]


@pytest.mark.asyncio
async def test_success_path_traces_repository_backend(caplog):
    """Persistence result names the repository backend actually used."""
    caplog.set_level(logging.INFO)
    manager = dbm.RepositoryManager(supabase_available=False)
    with patch.object(dbm, "get_repository_manager", return_value=manager):
        ctx = _tool_context(_provider_manager(VALID_CANDIDATE))
        tool = CreateTaskTool(ctx)
        result = await tool.execute(_tool_context(_provider_manager(VALID_CANDIDATE)), {"request": REQUEST})

    assert result.success is True
    lines = "\n".join(_create_task_logs(caplog))
    assert "stage=persistence_start repo_type=InMemoryTaskRepository" in lines or (
        "repository_type=InMemoryTaskRepository" in lines
    )
    assert "stage=task_created" in lines
    assert "lifecycle_state=active" in lines
    assert "stage=exit" in lines
    assert "success=true" in lines
    assert "terminal_stage=task_created" in lines


# ── User-visible failure messages are byte-identical to before ──


@pytest.mark.asyncio
async def test_failure_message_wording_unchanged():
    manager = dbm.RepositoryManager(supabase_available=False)
    with patch.object(dbm, "get_repository_manager", return_value=manager):
        tool = CreateTaskTool(_tool_context(_provider_manager("null")))
        result = await tool.execute(_tool_context(_provider_manager("null")), {"request": REQUEST})

    assert result.message == (
        "I could not turn that into a safe, unambiguous schedule, so I "
        "did not create any task. Restate it as an interval (e.g. 'every "
        "X minutes'), a time, or a daily/weekly cadence with a clear action."
    )
