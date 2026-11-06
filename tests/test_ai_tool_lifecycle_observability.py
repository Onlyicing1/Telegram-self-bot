"""End-to-end observability of the AI tool lifecycle.

These tests drive the REAL ``ToolExecutor`` and the REAL ``Dispatcher`` tool
loop (only the provider/conversation/prompt collaborators are fakes), so they
prove the production lifecycle is instrumented — not a standalone logging
helper.

They pin the required properties:

  * every stage from provider response to tool result and continuation emits a
    distinguishable, searchable event;
  * every event of one request carries the SAME dispatcher request id;
  * the executor instruments every registered tool centrally, including tools
    that are unknown, malformed, confirmation-gated, slow or raising;
  * no argument VALUE and no result CONTENT ever reaches the log.
"""
from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.ai.engine.dispatcher import Dispatcher
from backend.ai.engine.hooks import NOOP_HOOKS
from backend.ai.engine.metrics import EngineMetrics
from backend.ai.providers.base.contract import ProviderResponse
from backend.ai.session.request import AIRequest
from backend.ai.tools.base import PermissionLevel, ToolResult
from backend.ai.tools.context import ToolContext
from backend.ai.tools.executor import ToolExecutor
from backend.ai.tools.registry import ToolRegistry

RID = "req-observ-1"
SECRET_ARG = "sk-live-SECRET-ARGUMENT-VALUE"
SECRET_RESULT = "PRIVATE-RESULT-CONTENT"


class StubTool:
    """A duck-typed tool: the executor needs name/level/execute and nothing else."""

    description = "stub"
    parameters = {"type": "object", "properties": {}}
    return_type = "text"
    safe = True
    long_running = False

    def __init__(
        self,
        name: str = "probe",
        *,
        result: ToolResult | None = None,
        delay: float = 0.0,
        raises: Exception | None = None,
        level: PermissionLevel = PermissionLevel.READ_ONLY,
        timeout_seconds: float = 10,
    ) -> None:
        self.name = name
        self.permission_level = level
        self.timeout_seconds = timeout_seconds
        self._result = result
        self._delay = delay
        self._raises = raises
        self.calls = 0

    async def execute(self, context, arguments) -> ToolResult:
        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._raises is not None:
            raise self._raises
        return self._result or ToolResult(
            success=True, message="ok", data={"items": 2}
        )


def _executor(tool: StubTool, request_id: str = RID) -> ToolExecutor:
    context = ToolContext(
        telegram=None,
        owner_id=1,
        tz_str="UTC",
        extra={
            "request_id": request_id,
            "provider_name": "stubprov",
            "model_name": "stub-model",
        },
    )
    registry = ToolRegistry()
    registry.register(tool)
    return ToolExecutor(registry, context)


def _lines(caplog, event: str) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.getMessage().startswith(event)]


def _records(caplog, event: str) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.getMessage().startswith(event)]


# ── executor: every registered tool, centrally ──────────────────────────────


@pytest.mark.asyncio
async def test_successful_execution_emits_correlated_start_and_success(caplog):
    caplog.set_level(logging.DEBUG)
    tool = StubTool()
    executor = _executor(tool)

    results = await executor.execute_calls(
        [{"id": "call_abc", "name": "probe", "arguments": {"secret": SECRET_ARG}}],
        owner_id=1,
        session_id="s",
        round_no=2,
    )

    assert results[0].success is True
    assert tool.calls == 1
    started = _lines(caplog, "AI_TOOL_EXECUTION_STARTED")
    succeeded = _lines(caplog, "AI_TOOL_EXECUTION_SUCCEEDED")
    assert len(started) == 1 and len(succeeded) == 1
    for line in started + succeeded:
        assert f"id={RID}" in line
        assert "round=2" in line
        assert "call=call_abc" in line
        assert "tool=probe" in line
        assert "provider=stubprov" in line
        assert "model=stub-model" in line
    assert "status=started" in started[0]
    assert "status=success" in succeeded[0]
    assert "elapsed_ms=" in succeeded[0]
    assert "result_type=success" in succeeded[0]


@pytest.mark.asyncio
async def test_unsuccessful_tool_result_emits_failure_event(caplog):
    caplog.set_level(logging.DEBUG)
    tool = StubTool(result=ToolResult(success=False, message="nothing matched"))
    executor = _executor(tool)

    results = await executor.execute_calls(
        [{"id": "c1", "name": "probe", "arguments": {}}], owner_id=1, session_id="s"
    )

    assert results[0].success is False
    failed = _lines(caplog, "AI_TOOL_EXECUTION_FAILED")
    assert len(failed) == 1
    assert "status=failed" in failed[0]
    assert "error=tool_result_unsuccessful" in failed[0]
    assert f"id={RID}" in failed[0]


@pytest.mark.asyncio
async def test_timeout_emits_warning_timeout_event(caplog):
    caplog.set_level(logging.DEBUG)
    tool = StubTool(delay=0.2, timeout_seconds=0.01)
    executor = _executor(tool)

    results = await executor.execute_calls(
        [{"id": "c1", "name": "probe", "arguments": {}}], owner_id=1, session_id="s"
    )

    assert results[0].error == "timeout"
    records = _records(caplog, "AI_TOOL_EXECUTION_TIMEOUT")
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert "status=timeout" in records[0].getMessage()
    assert "elapsed_ms=" in records[0].getMessage()


@pytest.mark.asyncio
async def test_unexpected_exception_emits_sanitized_failure(caplog):
    caplog.set_level(logging.DEBUG)
    tool = StubTool(raises=ValueError(SECRET_RESULT))
    executor = _executor(tool)

    results = await executor.execute_calls(
        [{"id": "c1", "name": "probe", "arguments": {}}], owner_id=1, session_id="s"
    )

    assert results[0].success is False
    records = _records(caplog, "AI_TOOL_EXECUTION_FAILED")
    assert len(records) == 1
    message = records[0].getMessage()
    assert records[0].levelno == logging.WARNING
    assert "error=exception:ValueError" in message
    assert SECRET_RESULT not in caplog.text


@pytest.mark.asyncio
async def test_unknown_tool_emits_lookup_failure_and_never_executes(caplog):
    caplog.set_level(logging.DEBUG)
    tool = StubTool()
    executor = _executor(tool)

    results = await executor.execute_calls(
        [{"id": "c1", "name": "ghost_tool", "arguments": {}}], owner_id=1, session_id="s"
    )

    assert results[0].error == "not_found"
    assert tool.calls == 0
    looked_up = _lines(caplog, "AI_TOOL_LOOKUP_FAILED")
    assert len(looked_up) == 1
    assert "tool=ghost_tool" in looked_up[0]
    assert "status=not_registered" in looked_up[0]
    assert _lines(caplog, "AI_TOOL_EXECUTION_STARTED") == []


@pytest.mark.asyncio
async def test_malformed_arguments_emit_validation_failure(caplog):
    caplog.set_level(logging.DEBUG)
    tool = StubTool()
    executor = _executor(tool)

    results = await executor.execute_calls(
        [
            {
                "id": "c1",
                "name": "probe",
                "arguments": {},
                "malformed_arguments": True,
                "arguments_error": "invalid JSON",
            }
        ],
        owner_id=1,
        session_id="s",
    )

    assert results[0].error == "malformed_arguments"
    assert tool.calls == 0
    invalid = _lines(caplog, "AI_TOOL_CALL_VALIDATION_FAILED")
    assert len(invalid) == 1
    assert "error=malformed_arguments" in invalid[0]
    assert _lines(caplog, "AI_TOOL_EXECUTION_STARTED") == []


@pytest.mark.asyncio
async def test_confirmation_required_is_distinguishable_from_execution(caplog):
    caplog.set_level(logging.DEBUG)
    tool = StubTool(level=PermissionLevel.ADMIN_ONLY)
    executor = _executor(tool)

    blocked = await executor.execute_calls(
        [{"id": "c1", "name": "probe", "arguments": {"secret": SECRET_ARG}}],
        owner_id=1,
        session_id="s",
    )

    assert blocked[0].needs_confirmation is True
    assert tool.calls == 0
    assert len(_lines(caplog, "AI_TOOL_CONFIRMATION_REQUIRED")) == 1
    assert _lines(caplog, "AI_TOOL_EXECUTION_STARTED") == []

    confirmed = await executor.execute_confirmed(
        {"id": "c1", "name": "probe", "arguments": {"secret": SECRET_ARG}},
        owner_id=1,
        session_id="s",
        context_override=executor._context,
    )

    assert confirmed.success is True
    assert tool.calls == 1
    assert len(_lines(caplog, "AI_TOOL_EXECUTION_STARTED")) == 1
    assert len(_lines(caplog, "AI_TOOL_EXECUTION_SUCCEEDED")) == 1


@pytest.mark.asyncio
async def test_argument_values_and_result_content_never_reach_the_log(caplog):
    caplog.set_level(logging.DEBUG)
    tool = StubTool(
        result=ToolResult(success=True, message=SECRET_RESULT, data={"blob": SECRET_RESULT})
    )
    executor = _executor(tool)

    await executor.execute_calls(
        [
            {
                "id": "c1",
                "name": "probe",
                "arguments": {"secret": SECRET_ARG, "count": 3},
                "result": SECRET_RESULT,
            }
        ],
        owner_id=1,
        session_id="s",
    )

    assert SECRET_ARG not in caplog.text
    assert SECRET_RESULT not in caplog.text
    started = _lines(caplog, "AI_TOOL_EXECUTION_STARTED")[0]
    assert "arg_count=2" in started
    assert "arg_fields=count,secret" in started
    succeeded = _lines(caplog, "AI_TOOL_EXECUTION_SUCCEEDED")[0]
    assert f"message_chars={len(SECRET_RESULT)}" in succeeded
    assert "data_keys=1" in succeeded


@pytest.mark.asyncio
async def test_multiple_tool_calls_are_correlated_individually(caplog):
    caplog.set_level(logging.DEBUG)
    tool = StubTool()
    executor = _executor(tool)

    await executor.execute_calls(
        [
            {"id": "c1", "name": "probe", "arguments": {}},
            {"id": "c2", "name": "ghost_tool", "arguments": {}},
            {"name": "probe", "arguments": {}},
        ],
        owner_id=1,
        session_id="s",
    )

    assert "call=c1" in _lines(caplog, "AI_TOOL_EXECUTION_SUCCEEDED")[0]
    assert "call=c2" in _lines(caplog, "AI_TOOL_LOOKUP_FAILED")[0]
    # A provider that omits tool-call ids gets a bounded synthetic correlation id.
    assert "call=auto-r1-c3" in _lines(caplog, "AI_TOOL_EXECUTION_SUCCEEDED")[1]


# ── dispatcher: absent / returned / continuation, same request id ───────────


def _dispatcher(provider_responses, executor) -> Dispatcher:
    provider_manager = MagicMock()
    provider_manager.get_active_name.return_value = "stubprov"
    provider_manager.get_active.return_value.config.model = "stub-model"
    provider_manager.get_active().chat = AsyncMock(side_effect=provider_responses)

    conversation = MagicMock()
    session = MagicMock()
    session.session_id = "s"
    session.active_provider = "stubprov"
    conversation.get_session.return_value = session
    conversation.restore_history = AsyncMock()
    conversation.get_history.return_value = []

    prompt_builder = MagicMock()
    package = MagicMock()
    package.system_prompt = "sys"
    package.runtime_context = ""
    package.conversation_context = ""
    package.tool_context = ""
    package.user_input = "do it"
    package.estimated_tokens.estimated_input_tokens = 10
    package.estimated_tokens.prompt_size_chars = 20
    prompt_builder.build.return_value = package

    return Dispatcher(
        conversation,
        prompt_builder,
        provider_manager,
        NOOP_HOOKS,
        EngineMetrics(),
        tool_executor=executor,
    )


def _request() -> AIRequest:
    return AIRequest(
        session_id="s",
        user_message="do it",
        owner_id=1,
        chat_id=2,
        message_id=3,
        request_id=RID,
    )


@pytest.mark.asyncio
async def test_provider_response_without_tool_calls_emits_empty_event(caplog):
    caplog.set_level(logging.DEBUG)
    response = ProviderResponse(
        text="plain answer",
        provider_name="stubprov",
        success=True,
        metadata={"finish_reason": "stop", "model": "stub-model"},
    )
    dispatcher = _dispatcher([response], _executor(StubTool()))

    result = await dispatcher.dispatch(_request())

    assert result.success is True
    empty = _lines(caplog, "AI_TOOL_CALLS_EMPTY")
    assert len(empty) == 1
    assert f"id={RID}" in empty[0]
    assert "status=no_tool_calls" in empty[0]
    assert _lines(caplog, "AI_TOOL_CALLS_RECEIVED") == []
    assert _lines(caplog, "AI_TOOL_EXECUTION_STARTED") == []


@pytest.mark.asyncio
async def test_tool_lifecycle_and_continuation_share_one_request_id(caplog):
    caplog.set_level(logging.DEBUG)
    first = ProviderResponse(
        text="",
        provider_name="stubprov",
        success=True,
        tool_calls=[{"id": "c1", "name": "probe", "arguments": {"secret": SECRET_ARG}}],
        metadata={"finish_reason": "tool_calls", "model": "stub-model"},
    )
    final = ProviderResponse(
        text="done",
        provider_name="stubprov",
        success=True,
        metadata={"finish_reason": "stop", "model": "stub-model"},
    )
    tool = StubTool()
    dispatcher = _dispatcher([first, final], _executor(tool))

    result = await dispatcher.dispatch(_request())

    assert result.success is True
    assert tool.calls == 1
    received = _lines(caplog, "AI_TOOL_CALLS_RECEIVED")
    started = _lines(caplog, "AI_TOOL_EXECUTION_STARTED")
    returned = _lines(caplog, "AI_TOOL_RESULT_RETURNED")
    cont_started = _lines(caplog, "AI_TOOL_CONTINUATION_STARTED")
    cont_done = _lines(caplog, "AI_TOOL_CONTINUATION_COMPLETED")
    for group in (received, started, returned, cont_started, cont_done):
        assert len(group) == 1, group
        assert f"id={RID}" in group[0]

    assert "round=1" in received[0]
    assert "count=1" in received[0]
    assert "tools=probe" in received[0]
    assert "call=c1" in started[0]
    assert "status=success" in returned[0]
    assert "round=2" in cont_started[0]
    assert "tool_calls=0" in cont_done[0]
    assert SECRET_ARG not in caplog.text
    # The request-received event of the same request is correlated too.
    assert f"id={RID}" in _lines(caplog, "AI_TOOL_REQUEST_RECEIVED")[0]
