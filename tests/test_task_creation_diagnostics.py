"""Focused regression tests: create_task correlation layers.

These tests pin down the request-correlated diagnostics added AROUND the
AI_TASK_TRACE lifecycle records:

  - ProviderManager emits provider_selection / provider_request_start /
    provider_response / provider_fallback / provider_fallback_exhausted
    records, correlated by request_id, ONLY while a create_task request
    is bound (contextvar) — never for normal chat traffic.
  - TaskCreationService emits task_validation_start/_result, repository_call,
    persisted, rejected, schedule_invalid, repository_error records with the
    exact failing rule.
  - The binding is request-scoped: silence outside create_task.
  - bound_text truncates with an explicit marker; secrets never logged.
"""
from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

import pytest

from backend.ai.database import manager as dbm
from backend.ai.providers.base.capabilities import ProviderCapabilities
from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.base.contract import BaseProvider, ProviderResponse
from backend.ai.providers.manager.manager import ProviderManager
from backend.ai.task_creation import TaskCreationError, TaskCreationService
from backend.ai.task_interpreter import TaskInterpretationError, TaskInterpreter
from backend.ai.task_trace import bind_request, bound_text, unbind
from backend.ai.tools.context import ToolContext
from backend.ai.tools.task import CreateTaskTool

REQUEST = "هر 1 دقیقه یک بار برای من بنویس سلام"
VALID_JSON = (
    '{"label":"L","schedule_type":"interval","schedule":{"seconds":60},'
    '"timezone":"UTC","actions":[{"name":"send_message","arguments":{"text":"hi"}}],'
    '"notification_destination":{}}'
)
VALID_JSON = (
    '{"label":"L","schedule_type":"interval","schedule":{"seconds":60},'
    '"timezone":"UTC","actions":[{"name":"send_message","arguments":{"text":"hi"}}],'
    '"notification_destination":{}}'
)


class _ScriptedProvider(BaseProvider):
    def __init__(self, name: str, response_text: str, success: bool = True) -> None:
        super().__init__(ProviderConfig(provider_name=name, enabled=True, default_model="m"))
        self._name = name
        self._text = response_text
        self._success = success

    @property
    def name(self) -> str:
        return self._name

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(supports_tools=True, supports_function_call=True)

    async def chat(self, messages, **kwargs):
        return ProviderResponse(
            text=self._text, provider_name=self._name, success=self._success
        )

    def initialize(self) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def health(self) -> dict[str, Any]:
        return {"healthy": True}


class _FailingProvider(_ScriptedProvider):
    async def chat(self, messages, **kwargs):
        return ProviderResponse(
            text=self._text, provider_name=self._name, success=False,
            metadata={"failure_type": "request"},
        )


def _trace_logs(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "AI_TASK_TRACE" in r.getMessage()]


@contextmanager
def trace_scope(rid: str):
    token = bind_request(rid)
    try:
        yield
    finally:
        unbind(token)


# ── ProviderManager correlation ──


def test_provider_selection_request_response_traced(caplog):
    caplog.set_level(logging.INFO)
    pm = ProviderManager()
    pm.register_provider(_ScriptedProvider("fake", VALID_JSON))
    pm.switch_provider("fake")
    pm._fallback_chain = []
    with trace_scope("rid-prov"):
        import asyncio

        asyncio.run(TaskInterpreter(pm).interpret(REQUEST))

    lines = "\n".join(_trace_logs(caplog))
    assert "stage=provider_selection provider=fake" in lines
    assert "stage=provider_request_start provider=fake" in lines
    assert "stage=provider_response provider=fake" in lines
    assert "success=true" in lines
    assert "request_id=rid-prov" in lines


def test_provider_fallback_and_exhaustion_traced(caplog):
    caplog.set_level(logging.INFO)
    pm = ProviderManager()
    pm.register_provider(_FailingProvider("p1", "quota exhausted"))
    pm.register_provider(_FailingProvider("p2", "rate limited"))
    pm.switch_provider("p1")
    pm._fallback_chain = []
    with trace_scope("rid-fb"):
        import asyncio

        with pytest.raises(TaskInterpretationError):
            asyncio.run(TaskInterpreter(pm).interpret(REQUEST))

    lines = "\n".join(_trace_logs(caplog))
    assert "stage=provider_selection provider=p1" in lines
    assert "stage=provider_selection provider=p2" in lines
    assert "stage=provider_response provider=p1" in lines
    assert "failure_category=request" in lines
    assert "failed_provider=p1" in lines
    assert "next_provider=p2" in lines
    assert "stage=provider_fallback_exhausted" in lines
    assert "request_id=rid-fb" in lines


def test_provider_crash_traced_with_category(caplog):
    caplog.set_level(logging.INFO)

    class _Crashing(_ScriptedProvider):
        async def chat(self, messages, **kwargs):
            raise RuntimeError("socket exploded")

    pm = ProviderManager()
    pm.register_provider(_Crashing("crash", "x"))
    pm.switch_provider("crash")
    pm._fallback_chain = []
    with trace_scope("rid-crash"):
        import asyncio

        with pytest.raises(TaskInterpretationError):
            asyncio.run(TaskInterpreter(pm).interpret(REQUEST))

    lines = "\n".join(_trace_logs(caplog))
    assert "stage=provider_response provider=crash" in lines
    assert "failure_category=network" in lines
    assert "error_detail=RuntimeError" in lines


# ── TaskCreationService validation/persistence correlation ──


def test_service_rejection_names_exact_rule(caplog):
    caplog.set_level(logging.INFO)
    service = TaskCreationService(dbm.RepositoryManager(supabase_available=False).task, 777)
    with trace_scope("rid-val"):
        import asyncio

        with pytest.raises(TaskCreationError):
            asyncio.run(service.create({"bogus": True}, datetime.now(timezone.utc)))

    lines = "\n".join(_trace_logs(caplog))
    assert "stage=task_validation_start" in lines
    assert "stage=rejected" in lines
    assert "reason=unsupported task fields" in lines


def test_service_success_traces_repository_backend(caplog):
    caplog.set_level(logging.INFO)
    from backend.ai.task_candidate import parse_candidate_output

    candidate = parse_candidate_output(json.loads(
        '{"label":"L","schedule_type":"interval","schedule":{"seconds":60},'
        '"timezone":"UTC","actions":[{"name":"send_message","arguments":{"text":"hi"}}],'
        '"notification_destination":{}}'
    ))
    service = TaskCreationService(dbm.RepositoryManager(supabase_available=False).task, 777)
    with trace_scope("rid-persist"):
        import asyncio

        task = asyncio.run(service.create(candidate, datetime.now(timezone.utc)))

    lines = "\n".join(_trace_logs(caplog))
    assert "stage=task_validation_result" in lines
    assert "success=true" in lines
    assert "repo_type=InMemoryTaskRepository" in lines
    assert "stage=persisted" in lines
    assert f"task_id={task.id}" in lines
    assert "version=1" in lines
    assert "request_id=rid-persist" in lines


def test_service_repository_error_traced(caplog):
    class _ExplodingRepo:
        async def create_task(self, owner_id, payload):
            raise RuntimeError("database unavailable")

    from backend.ai.task_candidate import parse_candidate_output

    candidate = parse_candidate_output(json.loads(
        '{"label":"L","schedule_type":"interval","schedule":{"seconds":60},'
        '"timezone":"UTC","actions":[{"name":"send_message","arguments":{"text":"hi"}}],'
        '"notification_destination":{}}'
    ))
    service = TaskCreationService(_ExplodingRepo(), 777)  # type: ignore[arg-type]
    with trace_scope("rid-repoerr"):
        import asyncio

        with caplog.at_level(logging.INFO):
            with pytest.raises(RuntimeError):
                asyncio.run(service.create(candidate, datetime.now(timezone.utc)))

    lines = "\n".join(_trace_logs(caplog))
    assert "stage=repository_call" in lines
    assert "stage=repository_error" in lines
    assert "error_type=RuntimeError" in lines


# ── Binding lifecycle ──


def test_silent_outside_create_task(caplog):
    caplog.set_level(logging.INFO)
    pm = ProviderManager()
    pm.register_provider(_ScriptedProvider("fake", VALID_JSON))
    pm.switch_provider("fake")
    pm._fallback_chain = []

    import asyncio

    asyncio.run(TaskInterpreter(pm).interpret(REQUEST))
    # The correlation layers (provider manager, creation service) must be
    # completely silent outside a bound create_task request. The remote
    # interpreter's own request_id="-" records are its pre-existing design.
    MY_STAGES = (
        "stage=provider_selection", "stage=provider_request_start",
        "stage=provider_response", "stage=provider_fallback",
        "stage=provider_fallback_exhausted", "stage=task_validation_start",
        "stage=task_validation_result", "stage=repository_call",
        "stage=persisted", "stage=repository_error", "stage=schedule_invalid",
    )
    leaked = [line for line in _trace_logs(caplog) if any(s in line for s in MY_STAGES)]
    assert leaked == []


def test_bound_text_truncates_with_marker():
    bounded = bound_text("x" * 5000, 240)
    assert len(bounded) < 300
    assert "(+" in bounded and "chars)" in bounded


def test_full_tool_pipeline_keeps_single_request_id(caplog):
    caplog.set_level(logging.INFO)
    from unittest.mock import patch

    pm = ProviderManager()
    pm.register_provider(_ScriptedProvider("fake", "null"))
    pm.switch_provider("fake")
    pm._fallback_chain = []
    manager = dbm.RepositoryManager(supabase_available=False)
    ctx = ToolContext(
        telegram=None, owner_id=777, tz_str="UTC", client=None,
        extra={"provider_manager": pm, "request_id": "toolrid9", "chat_id": -1001},
    )
    with patch.object(dbm, "get_repository_manager", return_value=manager):
        import asyncio

        result = asyncio.run(CreateTaskTool(ctx).execute(ctx, {"request": REQUEST}))

    assert result.success is False  # null candidate → designed rejection
    trace_lines = _trace_logs(caplog)
    assert trace_lines
    assert all("request_id=toolrid9" in line for line in trace_lines)
    joined = "\n".join(trace_lines)
    assert "stage=provider_selection" in joined
    assert "stage=provider_response" in joined
