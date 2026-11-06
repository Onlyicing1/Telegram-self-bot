"""
Regression tests for the four root causes in
``TOOL_CALLING_ROOT_CAUSE_INVESTIGATION.md``.

  RC-1  Gemini function responses travel in ONE ``user`` Content per
        function-call batch, in call order, with the answered call's id.
  RC-2  ``Dispatcher._render_tool_schemas`` consumes the registry's FLAT
        parameter map, so every tool's arguments reach the prompt.
  RC-3  Every verbatim read tool defers its text for one continuation round,
        so a read-then-act request still performs the requested action while a
        read-only request still returns the tool's own text verbatim.
  RC-4  A tool that EXECUTED but whose history write failed is never reported
        as "not executed" and is never re-executed; calls that never ran are
        still reported as not executed.

No network access: provider HTTP is mocked and the dispatcher runs against a
scripted provider with a mocked tool executor.
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.ai.engine.dispatcher import Dispatcher
from backend.ai.engine.hooks import NOOP_HOOKS
from backend.ai.engine.metrics import EngineMetrics
from backend.ai.providers.base.capabilities import ProviderCapabilities
from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.base.contract import BaseProvider, ProviderResponse
from backend.ai.providers.manager.manager import ProviderManager
from backend.ai.session.request import AIRequest
from backend.ai.tools.context import ToolContext
from backend.ai.tools.executor import ToolExecutionResult
from backend.ai.tools.registry import create_default_registry


# ── RC-1: Gemini continuation shape ──


def _gemini_provider():
    from backend.ai.providers.gemini import GeminiProvider

    provider = GeminiProvider(
        ProviderConfig(api_key="k", enabled=True, default_model="gemini-pro")
    )
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}],
        "usageMetadata": {},
    }
    provider._http_client = MagicMock()
    provider._http_client.post = AsyncMock(return_value=response)
    return provider


def _sent_contents(provider) -> list[dict[str, Any]]:
    return provider._http_client.post.call_args.kwargs["json"]["contents"]


def _openai_call(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _tool_message(
    call_id: str, name: str, payload: dict[str, Any]
) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "name": name,
        "content": json.dumps(payload),
    }


def _continuation(
    calls: list[dict[str, Any]], results: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": "", "tool_calls": calls},
        *results,
    ]


@pytest.mark.asyncio
async def test_gemini_single_response_is_one_user_turn():
    provider = _gemini_provider()
    messages = _continuation(
        [_openai_call("call-1", "get_bio", {})],
        [_tool_message("call-1", "get_bio", {"success": True})],
    )

    await provider.chat(messages)

    contents = _sent_contents(provider)
    assert [c["role"] for c in contents] == ["user", "model", "user"]
    assert contents[0]["parts"] == [{"text": "do it"}]
    assert contents[1]["parts"][0]["functionCall"]["name"] == "get_bio"
    assert contents[1]["parts"][0]["functionCall"]["id"] == "call-1"
    responses = contents[2]["parts"]
    assert len(responses) == 1
    assert responses[0]["functionResponse"]["name"] == "get_bio"
    assert responses[0]["functionResponse"]["id"] == "call-1"


@pytest.mark.asyncio
async def test_gemini_multi_call_batch_is_one_user_content_in_call_order():
    provider = _gemini_provider()
    messages = _continuation(
        [
            _openai_call("call-a", "create_task", {"request": "remind me"}),
            _openai_call("call-b", "send_message", {"text": "sent"}),
        ],
        [
            _tool_message("call-a", "create_task", {"success": True}),
            _tool_message("call-b", "send_message", {"success": True}),
        ],
    )

    await provider.chat(messages)

    contents = _sent_contents(provider)
    # ONE model turn with both calls, ONE user turn with both responses.
    assert [c["role"] for c in contents] == ["user", "model", "user"]
    assert [
        part["functionCall"]["id"] for part in contents[1]["parts"]
    ] == ["call-a", "call-b"]
    response_parts = contents[2]["parts"]
    assert len(response_parts) == 2, "no response may be dropped or duplicated"
    assert [p["functionResponse"]["name"] for p in response_parts] == [
        "create_task",
        "send_message",
    ]
    assert [p["functionResponse"]["id"] for p in response_parts] == [
        "call-a",
        "call-b",
    ]


@pytest.mark.asyncio
async def test_gemini_flushes_the_response_batch_before_the_next_turn():
    provider = _gemini_provider()
    messages = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "first"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                _openai_call("a1", "get_bio", {}),
                _openai_call("b2", "task_list", {}),
            ],
        },
        _tool_message("a1", "get_bio", {"success": True}),
        _tool_message("b2", "task_list", {"success": True}),
        {"role": "user", "content": "second"},
    ]

    await provider.chat(messages)

    contents = _sent_contents(provider)
    assert [c["role"] for c in contents] == ["user", "model", "user", "user"]
    assert len(contents[2]["parts"]) == 2
    assert contents[2]["parts"][0]["functionResponse"]["name"] == "get_bio"
    assert contents[2]["parts"][1]["functionResponse"]["name"] == "task_list"
    # The unrelated user turn stays its own turn, after the responses.
    assert contents[3]["parts"] == [{"text": "second"}]


@pytest.mark.asyncio
async def test_gemini_omits_ids_that_the_call_did_not_carry():
    provider = _gemini_provider()
    messages = _continuation(
        [_openai_call("", "get_bio", {})],
        [{"role": "tool", "name": "get_bio", "content": json.dumps({"ok": True})}],
    )

    await provider.chat(messages)

    contents = _sent_contents(provider)
    assert "id" not in contents[1]["parts"][0]["functionCall"]
    assert "id" not in contents[2]["parts"][0]["functionResponse"]


@pytest.mark.asyncio
async def test_gemini_flushes_a_trailing_response_batch():
    provider = _gemini_provider()
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "tool",
            "tool_call_id": "c9",
            "name": "get_bio",
            "content": json.dumps({"ok": True}),
        },
    ]

    await provider.chat(messages)

    contents = _sent_contents(provider)
    assert [c["role"] for c in contents] == ["user", "user"]
    assert contents[1]["parts"][0]["functionResponse"]["id"] == "c9"


# ── RC-2: the prompt tool block ──


def _default_schemas() -> list[dict[str, Any]]:
    registry = create_default_registry(
        ToolContext(telegram=None, owner_id=1, tz_str="UTC")
    )
    return registry.list_schemas()


def _line_for(block: str, tool_name: str) -> str:
    for line in block.splitlines():
        if line.startswith(f"  - {tool_name}("):
            return line
    raise AssertionError(f"{tool_name} is missing from the prompt block: {block}")


def test_real_registry_renders_every_tool_parameter():
    schemas = _default_schemas()
    dispatcher, _, _, _ = _harness([], [[]])

    block = dispatcher._render_tool_schemas(schemas)

    with_params = [s for s in schemas if s.get("parameters")]
    # The investigation measured 55 tools, every one with a flat parameter map
    # (44 of them declaring at least one parameter at this commit).
    assert len(schemas) >= 50, len(schemas)
    assert len(with_params) >= 40, len(with_params)
    empty_signatures = []
    for schema in with_params:
        line = _line_for(block, schema["name"])
        required = set(schema.get("required") or [])
        for param in schema["parameters"]:
            marker = f"{param}*(" if param in required else f"{param}("
            assert marker in line, (schema["name"], param, line)
        if "((" not in line and line.split("(", 1)[1].startswith(")"):
            empty_signatures.append(schema["name"])
    # The previous 55/55 zero-parameter failure is no longer reproducible.
    assert empty_signatures == [], empty_signatures


def test_required_notation_matches_the_declared_requiredness():
    schemas = _default_schemas()
    dispatcher, _, _, _ = _harness([], [[]])

    block = dispatcher._render_tool_schemas(schemas)

    for schema in schemas:
        line = _line_for(block, schema["name"])
        required = set(schema.get("required") or [])
        for param in schema.get("parameters") or {}:
            if param in required:
                assert f"{param}*(" in line, (schema["name"], param, line)
            else:
                assert f"{param}*(" not in line, (schema["name"], param, line)


def test_tools_without_parameters_still_render():
    dispatcher, _, _, _ = _harness([], [[]])

    block = dispatcher._render_tool_schemas(
        [
            {
                "name": "no_params",
                "description": "nothing to pass",
                "parameters": {},
                "required": [],
            }
        ]
    )

    assert "  - no_params() — nothing to pass [safe]" in block

    # Any real tool that declares no parameters keeps the empty signature.
    for schema in _default_schemas():
        if not schema.get("parameters"):
            rendered = dispatcher._render_tool_schemas([schema])
            assert f"{schema['name']}()" in _line_for(rendered, schema["name"])


def test_parameter_types_and_descriptions_are_preserved():
    dispatcher, _, _, _ = _harness([], [[]])

    block = dispatcher._render_tool_schemas(
        [
            {
                "name": "synthetic",
                "description": "synthetic tool",
                "parameters": {
                    "query": {"type": "string", "description": "the search query"},
                    "limit": {"type": "integer"},
                },
                "required": ["query"],
            }
        ]
    )

    assert "query*(string: the search query)" in block
    assert "limit(integer)" in block


def test_full_json_schema_shape_is_still_rendered():
    """Only a full schema carries "properties"; that shape must keep working."""
    dispatcher, _, _, _ = _harness([], [[]])

    block = dispatcher._render_tool_schemas(
        [
            {
                "name": "nested",
                "description": "nested shape",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "file path"}
                    },
                    "required": ["path"],
                },
            }
        ]
    )

    assert "path*(string: file path)" in block


# ── Dispatcher harness ──


class _ScriptedProvider(BaseProvider):
    def __init__(self, script: list[Any]) -> None:
        super().__init__(
            ProviderConfig(
                provider_name="scripted", enabled=True, default_model="scripted-1"
            )
        )
        self._script = list(script)
        self.calls = 0

    @property
    def name(self) -> str:
        return "scripted"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(supports_tools=True, supports_function_call=True)

    async def chat(self, messages, **kwargs):  # noqa: ANN001, ANN003, ANN201
        self.calls += 1
        if self._script:
            return self._script.pop(0)
        return _resp(text="done")

    def initialize(self) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def health(self) -> dict[str, Any]:
        return {"healthy": True}


def _resp(*, text: str = "", calls: list[dict[str, Any]] | None = None) -> ProviderResponse:
    return ProviderResponse(
        text=text,
        provider_name="scripted",
        success=True,
        tool_calls=list(calls or []),
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        metadata={"finish_reason": "tool_calls" if calls else "stop"},
    )


def _result(name: str, message: str) -> ToolExecutionResult:
    return ToolExecutionResult(tool_name=name, success=True, message=message)


def _request() -> AIRequest:
    return AIRequest(
        session_id="s1", message_id=1, owner_id=7, user_message="hi", chat_id=1
    )


def _harness(script, execute_side_effect, conv=None):
    provider = _ScriptedProvider(script)
    manager = ProviderManager()
    manager.register_provider(provider)
    manager.switch_provider("scripted")

    conversation = conv if conv is not None else MagicMock()
    session = MagicMock()
    session.session_id = "s1"
    session.active_provider = "scripted"
    conversation.get_session.return_value = session
    conversation.restore_history = AsyncMock()
    conversation.get_history.return_value = []

    builder = MagicMock()
    package = MagicMock()
    package.system_prompt = "sys"
    package.runtime_context = ""
    package.conversation_context = ""
    package.tool_context = ""
    package.user_input = "hi"
    package.estimated_tokens.estimated_input_tokens = 10
    package.estimated_tokens.prompt_size_chars = 40
    builder.build.return_value = package

    executor = MagicMock()
    executor.execute_calls = AsyncMock(side_effect=execute_side_effect)
    executor._context = MagicMock()
    executor._context.extra = {}

    dispatcher = Dispatcher(
        conversation, builder, manager, NOOP_HOOKS, EngineMetrics(),
        tool_executor=executor,
    )
    return dispatcher, executor, provider, conversation


# ── RC-3: read-then-act ──


@pytest.mark.asyncio
async def test_read_then_act_is_not_stopped_after_a_non_task_read():
    """"preview save 3, then rename it" must still rename."""
    dispatcher, executor, provider, _ = _harness(
        [
            _resp(calls=[{"id": "c1", "name": "preview_save", "arguments": {"id": 3}}]),
            _resp(calls=[{"id": "c2", "name": "rename_save", "arguments": {"id": 3}}]),
            _resp(text="renamed"),
        ],
        [[_result("preview_save", "PREVIEW-TEXT")], [_result("rename_save", "RENAMED")]],
    )

    result = await dispatcher.dispatch(_request())

    assert executor.execute_calls.await_count == 2
    second_calls = executor.execute_calls.call_args_list[1].args[0]
    assert [c["name"] for c in second_calls] == ["rename_save"]
    assert provider.calls == 3
    assert result.response == "renamed"
    # The read text did not replace the final answer.
    assert "PREVIEW-TEXT" not in result.response


@pytest.mark.asyncio
async def test_read_only_request_still_returns_the_verbatim_tool_text():
    dispatcher, executor, provider, _ = _harness(
        [
            _resp(calls=[{"id": "c1", "name": "get_bio", "arguments": {}}]),
            _resp(text="here is a summary of your bio"),
        ],
        [[_result("get_bio", "BIO-EXACT")]],
    )

    result = await dispatcher.dispatch(_request())

    assert executor.execute_calls.await_count == 1  # the read ran once
    assert provider.calls == 2  # exactly one continuation
    assert "BIO-EXACT" in result.response
    # Anti-paraphrase guarantee: the model's own wording never wins.
    assert "here is a summary of your bio" not in result.response


# ── RC-4: execution vs. recording ──


def _recording_failure_conversation(fail_on: int) -> MagicMock:
    conversation = MagicMock()
    seen = {"count": 0}

    def _record(**_kwargs: Any) -> None:
        seen["count"] += 1
        if seen["count"] == fail_on:
            raise RuntimeError("history write failed")

    conversation.add_tool_result = MagicMock(side_effect=_record)
    return conversation


@pytest.mark.asyncio
async def test_executed_tool_with_failed_history_write_is_never_called_unexecuted():
    conversation = _recording_failure_conversation(fail_on=1)
    dispatcher, executor, _, _ = _harness(
        [
            _resp(calls=[{"id": "c1", "name": "probe", "arguments": {}}]),
            _resp(text="it ran"),
        ],
        [[_result("probe", "RAN-ONCE")]],
        conv=conversation,
    )

    result = await dispatcher.dispatch(_request())

    from backend.bot.handlers.ai_unified import _tool_execution_note

    # Executed exactly once — a bookkeeping failure is never a recovery signal.
    assert executor.execute_calls.await_count == 1
    # The real result survives in the request metadata.
    assert [r["tool_name"] for r in result.metadata["tool_results"]] == ["probe"]
    # Executed, not recorded: its own state, never "not executed".
    assert result.metadata.get("tool_result_recording_failed") is True
    assert result.metadata.get("tool_rounds_exhausted") is None
    assert not result.metadata.get("pending_tool_calls")
    assert result.metadata["finish_state"] == "text"
    assert result.response == "it ran"
    assert any(w.startswith("tool_history_round_1") for w in result.warnings)

    note = _tool_execution_note(result.metadata)
    assert "were not executed" not in note
    assert "could not be saved" in note


@pytest.mark.asyncio
async def test_calls_that_never_executed_are_still_reported_as_not_executed():
    async def _explode(*_args: Any, **_kwargs: Any) -> list[ToolExecutionResult]:
        raise RuntimeError("executor unavailable")

    dispatcher, executor, _, _ = _harness(
        [_resp(calls=[{"id": "c1", "name": "probe", "arguments": {}}])],
        _explode,
    )

    result = await dispatcher.dispatch(_request())

    from backend.bot.handlers.ai_unified import _tool_execution_note

    assert executor.execute_calls.await_count == 1  # never retried
    assert result.metadata.get("tool_rounds_exhausted") is True
    assert [c["name"] for c in result.metadata["pending_tool_calls"]] == ["probe"]
    assert result.metadata.get("tool_result_recording_failed") is None

    note = _tool_execution_note(result.metadata)
    assert "were not executed" in note
    assert "could not be saved" not in note


@pytest.mark.asyncio
async def test_salvage_at_the_round_limit_survives_a_failed_history_write():
    call = {"id": "c1", "name": "probe", "arguments": {}}
    conversation = _recording_failure_conversation(fail_on=4)
    dispatcher, executor, _, _ = _harness(
        [_resp(calls=[call]) for _ in range(4)],
        [[_result("probe", "RAN")] for _ in range(4)],
        conv=conversation,
    )

    result = await dispatcher.dispatch(_request())

    from backend.bot.handlers.ai_unified import _tool_execution_note

    # 3 in-loop rounds + the 4th pending batch executed once at the limit.
    assert executor.execute_calls.await_count == 4
    assert result.metadata.get("pending_calls_executed_at_limit") is True
    assert result.metadata.get("tool_result_recording_failed") is True
    assert result.metadata.get("tool_rounds_exhausted") is None
    assert any(w.startswith("tool_history_at_limit") for w in result.warnings)
    assert any("executed without an additional round" in w for w in result.warnings)

    note = _tool_execution_note(result.metadata)
    assert "were not executed" not in note
    assert "could not be saved" in note
