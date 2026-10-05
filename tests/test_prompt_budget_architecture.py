"""
Prompt budget architecture (Stage 6 — Workstream A2).

The investigation measured that the base prompt plus the REAL 55-tool catalog
already exceeded the whole-prompt ceiling on every request, so the history
trim removed EVERY entry, on every request, for every provider. These tests
pin the corrected policy:

  - conversation history is bounded by its OWN budget;
  - the tool contract and the current request never evict it;
  - the whole-prompt estimate still counts the tool block;
  - continuation tool results travel outside the prompt budget entirely.
"""
from __future__ import annotations

import re
from typing import Any

import pytest

from backend.ai.conversation.context_builder import ContextBuilder
from backend.ai.conversation.history import HistoryEntry
from backend.ai.conversation.state import ConversationState
from backend.ai.engine.engine import Engine
from backend.ai.providers.base.capabilities import ProviderCapabilities
from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.base.contract import BaseProvider, ProviderResponse
from backend.ai.providers.manager.manager import ProviderManager
from backend.ai.providers.registry.registry import ProviderRegistry
from backend.ai.prompt.builder import PromptBuilder
from backend.ai.prompt.template import PromptSection
from backend.ai.session.request import AIRequest
from backend.ai.tools.base import PermissionLevel, ToolResult
from backend.ai.tools.context import ToolContext
from backend.ai.tools.registry import ToolRegistry, create_default_registry

CHAT = -100555200


# ── helpers ──


def _tool_block() -> str:
    """The REAL production tool block rendered from the 55-tool registry."""
    from backend.ai.engine.dispatcher import Dispatcher

    registry = create_default_registry(
        ToolContext(telegram=None, owner_id=1, tz_str="UTC")
    )
    dispatcher = Dispatcher.__new__(Dispatcher)
    dispatcher.set_tool_registry(registry)
    return dispatcher._render_tool_schemas(registry.list_schemas())


class _FakeSession:
    session_id = "budget-session"
    owner_id = 1
    chat_id = 1
    state = ConversationState.IDLE
    current_panel = ""
    current_category = ""
    current_flow = ""
    pending_action = ""
    language = "English"
    timezone = "UTC"
    current_tool = ""
    last_tool = ""


def _context(history, *, language="English", user_text="hi"):
    class _Session(_FakeSession):
        pass

    _Session.language = language
    return ContextBuilder().build(
        session=_Session(), user_text=user_text, message_id=1, history=history,
    )


def _rows(section: str) -> int:
    return len(re.findall(r"^\s+\d+\. \[", section, flags=re.MULTILINE))


class _ScriptedProvider(BaseProvider):
    """Records every payload and replays scripted responses in order."""

    def __init__(self, responses: list[ProviderResponse] | None = None, text: str = "understood"):
        super().__init__(ProviderConfig(provider_name="scripted", enabled=True, default_model="m1"))
        self._responses = list(responses or [])
        self._text = text
        self.payloads: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return "scripted"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(supports_tools=True)

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> ProviderResponse:
        self.payloads.append({
            "messages": [dict(m) for m in messages],
            "tools": list(kwargs.get("tools") or []),
        })
        if self._responses:
            return self._responses.pop(0)
        return ProviderResponse(text=self._text, provider_name="scripted", success=True)

    def initialize(self) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def health(self) -> dict[str, Any]:
        return {"healthy": True, "ready": True}


def _engine(provider, registry: ToolRegistry | None = None, owner_id: int = 1) -> Engine:
    provider_registry = ProviderRegistry()
    provider_registry.register(provider)
    engine = Engine(providers=ProviderManager(provider_registry))
    if registry is not None:
        engine.attach_tools(registry, owner_id=owner_id, tz_str="UTC")
    return engine


def _text_response(text: str) -> ProviderResponse:
    return ProviderResponse(text=text, provider_name="scripted", success=True)


def _tool_response(name: str, arguments: dict[str, Any], call_id: str) -> ProviderResponse:
    return ProviderResponse(
        text="",
        provider_name="scripted",
        success=True,
        tool_calls=[{"id": call_id, "name": name, "arguments": arguments}],
    )


class _EchoTool:
    name = "echo"
    description = "Echo a token (test tool)"
    parameters = {"token": {"type": "string", "default": ""}}
    permission_level = PermissionLevel.READ_ONLY
    safe = True
    return_type = "text"
    long_running = False

    async def execute(self, ctx, args):
        return ToolResult(success=True, message=f"echo:{args.get('token', '')}")


def _payload_system(payload: dict[str, Any]) -> str:
    return "\n".join(
        str(m.get("content", "")) for m in payload["messages"] if m.get("role") == "system"
    )


# ── Direct PromptBuilder properties ──


def test_real_55_tool_catalog_does_not_evict_history():
    block = _tool_block()
    history = [
        HistoryEntry(role="user" if i % 2 == 0 else "assistant", content=f"پیام شماره {i}: متن کوتاه گفتگو")
        for i in range(12)
    ]
    ctx = _context(history, language="Persian", user_text="قسمت بعدی وان پیس کی میاد؟")

    package = PromptBuilder().build(ctx, tool_block=block)

    section = package.sections[PromptSection.CONVERSATION_STATE]
    assert _rows(section) == 12
    assert package.metadata["history_trimmed"] == 0
    assert block in package.sections[PromptSection.TOOL_METADATA]
    assert package.user_input == "قسمت بعدی وان پیس کی میاد؟"


def test_history_is_bounded_by_its_own_budget_oldest_trimmed_first():
    block = _tool_block()
    history = [
        HistoryEntry(
            role="user" if i % 2 == 0 else "assistant",
            content=f"ENTRY-{i:02d}:" + ("متن گفتگوی طولانی " * 40),
        )
        for i in range(40)
    ]
    ctx = _context(history, language="Persian")

    package = PromptBuilder().build(ctx, tool_block=block)
    section = package.sections[PromptSection.CONVERSATION_STATE]

    rows = _rows(section)
    assert 0 < rows < 40
    assert package.metadata["history_trimmed"] > 0
    assert package.metadata["history_tokens"] <= package.metadata["history_budget_tokens"]
    assert "ENTRY-39" in section  # the most recent turn survives
    assert "ENTRY-00" not in section  # oldest-first eviction


def test_long_tool_catalog_cannot_evict_history_or_the_request():
    noise = "\n".join(
        f"  - synthetic_tool_{i}(query*(string)) — noise [safe]" for i in range(3000)
    )
    block = _tool_block() + "\n" + noise
    history = [
        HistoryEntry(role="user", content=f"canary-{i}") for i in range(6)
    ]
    user_text = "final request must survive"
    ctx = _context(history, language="Persian", user_text=user_text)

    package = PromptBuilder().build(ctx, tool_block=block)

    section = package.sections[PromptSection.CONVERSATION_STATE]
    assert _rows(section) == 6
    assert "canary-0" in section
    assert package.user_input == user_text
    assert block in package.sections[PromptSection.TOOL_METADATA]


# ── Engine-level: what actually reaches the provider ──


@pytest.mark.asyncio
async def test_multi_turn_history_reaches_the_provider_with_the_real_tool_catalog():
    provider = _ScriptedProvider([_text_response("first answer")])
    engine = _engine(provider, registry=create_default_registry(
        ToolContext(telegram=None, owner_id=920001, tz_str="UTC")
    ), owner_id=920001)

    await engine.execute(AIRequest(
        session_id="budget-multi", user_message="برنامه دانشگاه رو توضیح بده",
        owner_id=920001, chat_id=CHAT, message_id=1, language="Persian",
    ))
    provider.payloads.clear()

    correction = "نه، منظورم برنامه هفتگی بود — همون رو دوباره بررسی کن"
    await engine.execute(AIRequest(
        session_id="budget-multi", user_message=correction,
        owner_id=920001, chat_id=CHAT, message_id=2, language="Persian",
    ))

    payload = provider.payloads[0]
    system = _payload_system(payload)
    # The correction's referent is preserved even though the real 55-tool
    # catalog was attached in the same request.
    assert "[History]" in system
    assert "برنامه دانشگاه رو توضیح بده" in system
    assert "first answer" in system
    # The current correction is the USER message, not a history echo.
    assert payload["messages"][-1]["role"] == "user"
    assert payload["messages"][-1]["content"] == correction


@pytest.mark.asyncio
async def test_current_request_is_never_dropped_by_any_budget():
    provider = _ScriptedProvider()
    engine = _engine(provider, registry=create_default_registry(
        ToolContext(telegram=None, owner_id=920002, tz_str="UTC")
    ), owner_id=920002)
    question = "قسمت بعدی وان پیس کی میاد؟"

    await engine.execute(AIRequest(
        session_id="budget-current", user_message=question,
        owner_id=920002, chat_id=CHAT, message_id=3, language="Persian",
    ))

    payload = provider.payloads[0]
    assert payload["messages"][-1]["role"] == "user"
    assert payload["messages"][-1]["content"] == question
    assert payload["tools"], "the tool contract must reach the provider too"


@pytest.mark.asyncio
async def test_continuation_tool_results_are_outside_the_prompt_budget():
    provider = _ScriptedProvider([
        _tool_response("echo", {"token": "T-1"}, "call_1"),
        _text_response("saw the real tool result"),
    ])
    registry = ToolRegistry()
    registry.register(_EchoTool())
    engine = _engine(provider, registry=registry, owner_id=920003)

    result = await engine.execute(AIRequest(
        session_id="budget-cont", user_message="run the echo tool",
        owner_id=920003, chat_id=CHAT, message_id=4,
    ))

    assert result.success is True
    assert result.response == "saw the real tool result"

    continuation = provider.payloads[1]
    assistant = next(m for m in continuation["messages"] if m.get("tool_calls"))
    assert assistant["tool_calls"][0]["id"] == "call_1"
    tool_message = next(m for m in continuation["messages"] if m["role"] == "tool")
    assert tool_message["tool_call_id"] == "call_1"
    assert "echo:T-1" in tool_message["content"]
    # The request that drove the tool call still travels with the result.
    assert any(
        m["role"] == "user" and m["content"] == "run the echo tool"
        for m in continuation["messages"]
    )


@pytest.mark.asyncio
async def test_tool_call_overflow_still_yields_one_tool_message_per_call():
    calls = [
        {"id": f"call_{i}", "name": "echo", "arguments": {"token": f"T{i}"}}
        for i in range(8)
    ]
    provider = _ScriptedProvider([
        ProviderResponse(text="", provider_name="scripted", success=True, tool_calls=calls),
        _text_response("done"),
    ])
    registry = ToolRegistry()
    registry.register(_EchoTool())
    engine = _engine(provider, registry=registry, owner_id=920004)

    result = await engine.execute(AIRequest(
        session_id="budget-overflow", user_message="run many echoes",
        owner_id=920004, chat_id=CHAT, message_id=5,
    ))

    assert result.success is True
    continuation = provider.payloads[1]
    tool_messages = [m for m in continuation["messages"] if m["role"] == "tool"]
    # EVERY assistant tool_call id gets a tool response — 5 executed plus 3
    # bounded overflow failures — so the provider protocol stays complete.
    assert [m["tool_call_id"] for m in tool_messages] == [f"call_{i}" for i in range(8)]
    overflow = [m for m in tool_messages if "limit reached" in m["content"]]
    assert len(overflow) == 3
    assert "echo:T0" in tool_messages[0]["content"]
