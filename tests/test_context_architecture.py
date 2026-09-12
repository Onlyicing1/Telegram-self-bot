"""
Context architecture — behavioral tests for the Conversation Context pipeline.

Source-proven findings this file pins (see IMPLEMENTATION_REPORT.md):

  1. The current turn was rendered TWICE in the provider payload: Stage 1
     appends the owner's message to the runtime session history, and
     ``_build_context`` rendered that history into the [History] block while the
     same text also traveled as the USER_MESSAGE section.
  2. The [Tool Context] section was dead: ``_build_context`` passed an empty
     ``ToolContext()`` even though the runtime session records every completed
     tool call (``ConversationManager.add_tool_result``), so the section always
     read "Current Tool: None / Last Tool: None".
  3. The available-tool schema block (~1.9k tokens for the real registry) was
     appended to the package AFTER ``PromptBuilder.build`` computed the token
     budget, so the budget — and the reported estimate — were blind to it.

Tests drive the REAL Engine (scripted provider) and the REAL PromptBuilder, and
assert on the exact payload the provider receives.
"""
from __future__ import annotations

import os
import re
from typing import Any
from unittest.mock import patch

import pytest

from backend.ai.engine.engine import Engine
from backend.ai.providers.base.capabilities import ProviderCapabilities
from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.base.contract import BaseProvider, ProviderResponse
from backend.ai.providers.manager.manager import ProviderManager
from backend.ai.providers.registry.registry import ProviderRegistry
from backend.ai.prompt.builder import PromptBuilder
from backend.ai.prompt.template import PromptSection
from backend.ai.session.request import AIRequest
from backend.ai.conversation.context_builder import ReplyContext
from backend.ai.conversation.state import ConversationState

CHAT = -100555000


# ── Scripted provider machinery ──


class _ScriptedProvider(BaseProvider):
    """Records every payload it is handed; never calls the network."""

    def __init__(self, payloads: list[list[dict[str, Any]]], text: str = "sure thing") -> None:
        super().__init__(ProviderConfig(provider_name="scripted", enabled=True, default_model="m1"))
        self._payloads = payloads
        self._text = text

    @property
    def name(self) -> str:
        return "scripted"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(supports_tools=True)

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> ProviderResponse:
        self._payloads.append([dict(m) for m in messages])
        return ProviderResponse(text=self._text, provider_name="scripted", success=True)

    def initialize(self) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def health(self) -> dict[str, Any]:
        return {"healthy": True, "ready": True}


class _RecordingMemory:
    """MemoryManager stand-in that records which owner it was asked about."""

    def __init__(self, data: dict[str, str] | None = None) -> None:
        self.calls: list[int] = []
        self.data = data or {}

    def retrieve_for_prompt(self, owner_id: int, query_text: str = "") -> dict[str, str]:
        self.calls.append(owner_id)
        return dict(self.data)


class _CaptureBuilder(PromptBuilder):
    """The REAL builder, plus the (context, tool_block) it was called with."""

    def __init__(self) -> None:
        super().__init__()
        self.context = None
        self.tool_block: str | None = None
        self.packages: list[Any] = []

    def build(self, context, tool_block: str = ""):
        self.context = context
        self.tool_block = tool_block
        package = super().build(context, tool_block)
        self.packages.append(package)
        return package


def _engine(payloads: list[list[dict[str, Any]]], **kwargs: Any) -> Engine:
    registry = ProviderRegistry()
    registry.register(_ScriptedProvider(payloads))
    return Engine(providers=ProviderManager(registry), **kwargs)


def _payload_text(payload: list[dict[str, Any]]) -> str:
    return "\n".join(str(m.get("content", "")) for m in payload)


async def _dispatch(engine: Engine, **overrides: Any) -> Any:
    base: dict[str, Any] = {
        "session_id": "ctx-session",
        "user_message": "hello there, how are you today?",
        "owner_id": 841001,
        "chat_id": CHAT,
        "message_id": 11,
    }
    base.update(overrides)
    return await engine.execute(AIRequest(**base))


# ── 1. The current turn is rendered exactly once ──


@pytest.mark.asyncio
async def test_current_turn_is_rendered_once_and_not_inside_history():
    payloads: list[list[dict[str, Any]]] = []
    engine = _engine(payloads)
    user_text = "hello there, how are you today?"

    result = await _dispatch(engine, user_message=user_text)

    assert result.success is True
    payload = payloads[0]
    # Once as the user message, and nowhere else (no [History] echo).
    assert _payload_text(payload).count(user_text) == 1
    assert payload[-1]["role"] == "user"
    assert payload[-1]["content"] == user_text

    conversation_section = next(
        m["content"] for m in payload if "[Conversation State]" in m["content"]
    )
    assert user_text not in conversation_section


@pytest.mark.asyncio
async def test_previous_turns_still_reach_the_history_block():
    """Deduplication must not drop real conversational continuity."""
    payloads: list[list[dict[str, Any]]] = []
    engine = _engine(payloads)

    await _dispatch(engine, session_id="ctx-session-2", message_id=1, owner_id=841002)
    payloads.clear()
    await _dispatch(
        engine, session_id="ctx-session-2", message_id=2, owner_id=841002,
        user_message="what did I just say?",
    )

    conversation_section = next(
        m["content"] for m in payloads[0] if "[Conversation State]" in m["content"]
    )
    assert "hello there, how are you today?" in conversation_section
    assert "what did I just say?" not in conversation_section


# ── 2. Required identity context survives ──


@pytest.mark.asyncio
async def test_context_preserves_owner_chat_message_and_session_identity():
    payloads: list[list[dict[str, Any]]] = []
    capture = _CaptureBuilder()
    engine = _engine(payloads, prompt_builder=capture)

    await _dispatch(
        engine, owner_id=841003, chat_id=-100777, message_id=4242,
        session_id="identity-session", timezone="Asia/Tehran", language="Persian",
    )

    ctx = capture.context
    assert ctx.owner_id == 841003
    assert ctx.chat_id == -100777
    assert ctx.message_id == 4242
    assert ctx.session_id  # non-empty, real session id
    assert ctx.timezone == "Asia/Tehran"
    assert ctx.language == "Persian"

    payload = payloads[0]
    text = _payload_text(payload)
    assert "Timezone: Asia/Tehran" in text
    assert "Language: Persian" in text
    assert "Current Time: " in text


# ── 3. Reply context ──


def _ai_reply(content: str) -> ReplyContext:
    return ReplyContext(
        exists=True, message_id=900, sender_id=1, sender_name="Nova",
        chat_id=CHAT, chat_title="Saved Messages", media_type="",
        text_preview="", timestamp="2026-01-01T10:00:00+00:00",
        is_ai_message=True, ai_session_id="ai-session", ai_role="assistant",
        ai_content=content, ai_provider="scripted", ai_model="m1",
        ai_timestamp="2026-01-01T10:00:00+00:00",
    )


@pytest.mark.asyncio
async def test_reply_to_ai_message_keeps_full_untruncated_content():
    long_reply = "IMPORTANT-" + ("x" * 3000)
    payloads: list[list[dict[str, Any]]] = []
    engine = _engine(payloads)

    await _dispatch(
        engine, owner_id=841004, user_message="explain that again",
        reply_context=_ai_reply(long_reply),
    )

    text = _payload_text(payloads[0])
    assert "[Reply to AI Message]" in text
    assert long_reply in text  # full content, never a 200-char preview
    assert "AI Session: ai-session" in text
    assert "Provider: scripted" in text


@pytest.mark.asyncio
async def test_reply_to_non_ai_message_remains_distinguishable():
    payloads: list[list[dict[str, Any]]] = []
    engine = _engine(payloads)

    await _dispatch(
        engine, owner_id=841005, user_message="summarize this",
        reply_context=ReplyContext(
            exists=True, message_id=901, sender_id=2, sender_name="Design Channel",
            chat_id=CHAT, chat_title="Design Inspiration", media_type="Photo",
            text_preview="A long article about quantum computing.",
            timestamp="2026-01-01T10:00:00+00:00",
        ),
    )

    text = _payload_text(payloads[0])
    assert "[Reply Context]" in text
    assert "[Reply to AI Message]" not in text
    assert "Sender: Design Channel" in text
    assert "Text: A long article about quantum computing." in text
    assert "Media: Photo" in text


@pytest.mark.asyncio
async def test_request_without_reply_is_valid_and_renders_none():
    payloads: list[list[dict[str, Any]]] = []
    engine = _engine(payloads)

    result = await _dispatch(engine, owner_id=841006)

    assert result.success is True
    assert "Reply: None" in _payload_text(payloads[0])


# ── 4. Memory is separate from conversation history ──


@pytest.mark.asyncio
async def test_memory_is_delivered_and_separate_from_history():
    memory = _RecordingMemory({
        "permanent": "- Owner's name is TestUser",
        "long": "- Owner prefers concise answers",
        "short": "",
    })
    payloads: list[list[dict[str, Any]]] = []
    engine = _engine(payloads, memory_manager=memory)

    await _dispatch(engine, owner_id=841007)

    payload = payloads[0]
    system_text = "\n".join(m["content"] for m in payload if m["role"] == "system")
    conversation_section = next(
        m["content"] for m in payload if "[Conversation State]" in m["content"]
    )
    # Retrieved memory must actually REACH the model (it used to be rendered
    # into the sections and then dropped from the merged system prompt).
    assert "[Permanent Facts]" in system_text
    assert "Owner's name is TestUser" in system_text
    assert "[Long-term Memory]" in system_text
    # ... without masquerading as conversation history.
    assert "Owner's name is TestUser" not in conversation_section
    assert memory.calls == [841007]


class _OwnerScopedMemory:
    """Memory stand-in that answers per owner and records the requests."""

    def __init__(self) -> None:
        self.calls: list[int] = []

    def retrieve_for_prompt(self, owner_id: int, query_text: str = "") -> dict[str, str]:
        self.calls.append(owner_id)
        return {"permanent": f"- owner-{owner_id}-secret", "long": "", "short": ""}


@pytest.mark.asyncio
async def test_memory_reaches_only_its_own_owner():
    fake = _OwnerScopedMemory()
    payloads: list[list[dict[str, Any]]] = []
    engine = _engine(payloads, memory_manager=fake)

    await _dispatch(engine, owner_id=841008)
    first_owner_payload = _payload_text(payloads[0])
    payloads.clear()
    await _dispatch(engine, owner_id=841009, message_id=12, session_id="ctx-session-b")
    second_owner_payload = _payload_text(payloads[0])

    assert fake.calls == [841008, 841009]
    assert "owner-841008-secret" in first_owner_payload
    assert "owner-841008-secret" not in second_owner_payload
    assert "owner-841009-secret" in second_owner_payload
    assert "owner-841009-secret" not in first_owner_payload


# ── 4b. Every rendered section must be DELIVERED (no silent drops) ──


@pytest.mark.asyncio
async def test_every_rendered_section_reaches_the_provider_payload():
    """Rendering a section is not delivering it: this pins the whole class."""
    payloads: list[list[dict[str, Any]]] = []
    capture = _CaptureBuilder()
    engine = _engine(
        payloads, prompt_builder=capture,
        memory_manager=_RecordingMemory({"permanent": "- MEM-CANARY", "long": "", "short": ""}),
    )

    await _dispatch(engine, owner_id=841021)

    package = capture.packages[-1]
    text = _payload_text(payloads[0])
    undelivered = [
        section.value
        for section, rendered in package.sections.items()
        if rendered and rendered not in text
    ]
    assert undelivered == [], f"sections rendered but never delivered: {undelivered}"


@pytest.mark.asyncio
async def test_output_instructions_reach_the_model_in_canonical_order():
    payloads: list[list[dict[str, Any]]] = []
    capture = _CaptureBuilder()
    engine = _engine(
        payloads, prompt_builder=capture,
        memory_manager=_RecordingMemory({"permanent": "- MEM-CANARY", "long": "", "short": ""}),
    )

    await _dispatch(engine, owner_id=841022)

    system_text = "\n".join(m["content"] for m in payloads[0] if m["role"] == "system")
    # The JSON action contract the deterministic execution path depends on.
    assert "Output Rules:" in system_text
    assert '"action":"task_list"' in system_text or '"action": "save"' in system_text
    # SECTION_ORDER: memory → preferences → output instructions.
    assert system_text.index("[Memory]") < system_text.index("[Preferences]")
    assert system_text.index("[Preferences]") < system_text.index("Output Rules:")


# ── 5. Preferences influence behavior deterministically ──


@pytest.mark.asyncio
async def test_preferences_are_propagated_into_the_system_prompt():
    from backend.ai.database.manager import get_repository_manager

    owner = 841010
    repo = get_repository_manager().preferences
    repo.update(owner, {
        "language": "Persian",
        "personality": "warm",
        "response_style": "detailed",
        "custom_instructions": "Always answer with bullet points.",
    })

    payloads: list[list[dict[str, Any]]] = []
    engine = _engine(payloads)

    await _dispatch(engine, owner_id=owner)

    system_text = "\n".join(m["content"] for m in payloads[0] if m["role"] == "system")
    assert "[Preferences]" in system_text
    assert "Personality: warm" in system_text
    assert "Response Style: detailed" in system_text
    assert "Custom Instructions: Always answer with bullet points." in system_text


@pytest.mark.asyncio
async def test_preferences_are_owner_scoped():
    from backend.ai.database.manager import get_repository_manager

    repo = get_repository_manager().preferences
    repo.update(841011, {"custom_instructions": "ONLY-FOR-841011"})

    payloads: list[list[dict[str, Any]]] = []
    engine = _engine(payloads)

    await _dispatch(engine, owner_id=841012)
    assert "ONLY-FOR-841011" not in _payload_text(payloads[0])


# ── 6. Tool context ──


@pytest.mark.asyncio
async def test_tool_context_reports_the_last_recorded_tool():
    payloads: list[list[dict[str, Any]]] = []
    engine = _engine(payloads)
    owner = 841013

    await _dispatch(engine, owner_id=owner, session_id="tool-session")
    assert "Last Tool: None" in _payload_text(payloads[0])

    # Exactly what the dispatcher records after a tool round.
    engine.conversation_manager.add_tool_result(owner, "get_bio", "🕒 12:00 | 💭 calm")

    payloads.clear()
    await _dispatch(engine, owner_id=owner, session_id="tool-session", message_id=13)

    text = _payload_text(payloads[0])
    assert "Last Tool: get_bio" in text
    # No tool runs while the prompt is being built.
    assert "Current Tool: None" in text


@pytest.mark.asyncio
async def test_tool_results_are_not_duplicated_into_a_tool_results_block():
    payloads: list[list[dict[str, Any]]] = []
    engine = _engine(payloads)
    owner = 841014

    await _dispatch(engine, owner_id=owner, session_id="tool-session-2")
    engine.conversation_manager.add_tool_result(owner, "get_bio", "BIO-RESULT-CANARY")

    payloads.clear()
    await _dispatch(engine, owner_id=owner, session_id="tool-session-2", message_id=14)

    text = _payload_text(payloads[0])
    assert text.count("BIO-RESULT-CANARY") == 1  # exactly one carrier: [History]
    assert "[Tool Results]" not in text


# ── 7. Tool schemas: exactly one carrier, and counted by the budget ──


@pytest.mark.asyncio
async def test_tool_schemas_reach_the_payload_exactly_once():
    payloads: list[list[dict[str, Any]]] = []
    engine = _engine(payloads)
    engine.attach_tools(engine.tool_registry or _default_registry(), owner_id=841015)

    await _dispatch(engine, owner_id=841015)

    text = _payload_text(payloads[0])
    assert text.count("[Available Tools]") == 1
    assert "- get_bio(" in text or "- task_list(" in text


@pytest.mark.asyncio
async def test_tool_schemas_are_absent_when_tools_are_disabled():
    payloads: list[list[dict[str, Any]]] = []
    engine = _engine(payloads)
    engine.attach_tools(_default_registry(), owner_id=841016)

    await _dispatch(engine, owner_id=841016, allow_tools=False)

    text = _payload_text(payloads[0])
    assert "[Available Tools]" not in text
    assert "[Tool Context]" in text


def _default_registry():
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.registry import create_default_registry

    return create_default_registry(ToolContext(telegram=None, owner_id=0, tz_str="UTC"))


@pytest.mark.asyncio
async def test_builder_counts_the_tool_block_against_the_budget():
    """The schemas are part of the prompt, so they must be part of the budget."""
    from backend.ai.prompt.budget import estimate_tokens

    payloads: list[list[dict[str, Any]]] = []
    capture = _CaptureBuilder()
    engine = _engine(payloads, prompt_builder=capture)
    registry = _default_registry()
    engine.attach_tools(registry, owner_id=841017)

    await _dispatch(engine, owner_id=841017)

    assert capture.tool_block, "the dispatcher must hand the schemas to the builder"
    package = capture.packages[-1]
    block_tokens = estimate_tokens(capture.tool_block, "English")

    # The rendered tool section carries the block, and the budget estimate
    # covers it (previously it was appended after the budget was computed).
    tool_section = package.sections[PromptSection.TOOL_METADATA]
    assert capture.tool_block in tool_section
    assert package.estimated_tokens.estimated_input_tokens >= block_tokens
    assert package.estimated_tokens.prompt_size_chars >= len(capture.tool_block)


def _context_with_history(entries: int, chars_each: int):
    from backend.ai.conversation.context_builder import ContextBuilder
    from backend.ai.conversation.history import HistoryEntry

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

    history = [
        HistoryEntry(role="user" if i % 2 == 0 else "assistant", content="y" * chars_each)
        for i in range(entries)
    ]
    return ContextBuilder().build(
        session=_FakeSession(), user_text="hi", message_id=1, history=history,
    )


def _history_rows(section: str) -> int:
    return len(re.findall(r"^\s+\d+\. \[", section, flags=re.MULTILINE))


def test_tool_block_consumes_budget_and_history_is_trimmed_to_fit():
    ctx = _context_with_history(entries=20, chars_each=3000)
    tool_block = "[Available Tools]\n" + ("  - tool_x(string) — does a thing [safe]\n" * 120)

    without = PromptBuilder().build(ctx)
    with_block = PromptBuilder().build(ctx, tool_block=tool_block)

    assert without.estimated_tokens.within_budget is True
    assert with_block.estimated_tokens.within_budget is True

    rows_without = _history_rows(without.sections[PromptSection.CONVERSATION_STATE])
    rows_with = _history_rows(with_block.sections[PromptSection.CONVERSATION_STATE])
    assert rows_with < rows_without, "schemas must push history out through the budget"

    # The critical sections survive: schemas, the request, the output rules.
    assert tool_block in with_block.sections[PromptSection.TOOL_METADATA]
    assert with_block.sections[PromptSection.USER_MESSAGE] == "hi"
    assert with_block.sections[PromptSection.OUTPUT_INSTRUCTIONS]


def test_budget_estimate_grows_by_the_tool_block():
    from backend.ai.prompt.budget import estimate_tokens

    ctx = _context_with_history(entries=1, chars_each=40)
    tool_block = "z" * 4000

    without = PromptBuilder().build(ctx)
    with_block = PromptBuilder().build(ctx, tool_block=tool_block)

    delta = (
        with_block.estimated_tokens.estimated_input_tokens
        - without.estimated_tokens.estimated_input_tokens
    )
    assert delta == estimate_tokens(tool_block, "English")
    assert with_block.estimated_tokens.prompt_size_chars - without.estimated_tokens.prompt_size_chars >= len(tool_block)


# ── 8. No internal state or secrets are dumped into the payload ──


@pytest.mark.asyncio
async def test_internal_session_state_and_env_secrets_never_reach_the_payload(monkeypatch):
    payloads: list[list[dict[str, Any]]] = []
    engine = _engine(payloads)
    owner = 841018

    await _dispatch(engine, owner_id=owner)

    session = engine.conversation_manager.get_session(owner)
    assert session is not None
    # Unrestricted runtime objects must never be serialized into a prompt.
    session.session_string = "SESSION-STRING-CANARY"  # type: ignore[attr-defined]
    session.api_key = "PROVIDER-KEY-CANARY"  # type: ignore[attr-defined]
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "SUPABASE-CANARY-VALUE")

    payloads.clear()
    await _dispatch(engine, owner_id=owner, message_id=15)

    text = _payload_text(payloads[0])
    for canary in ("SESSION-STRING-CANARY", "PROVIDER-KEY-CANARY", "SUPABASE-CANARY-VALUE"):
        assert canary not in text
    for name in ("SESSION_STRING", "API_HASH", "BOT_TOKEN", "SUPABASE_SERVICE_ROLE_KEY"):
        value = os.environ.get(name, "")
        if len(value) >= 12:
            assert value not in text


# ── 9. One context build per dispatch ──


@pytest.mark.asyncio
async def test_context_is_built_exactly_once_per_dispatch():
    from backend.ai.engine.dispatcher import Dispatcher

    payloads: list[list[dict[str, Any]]] = []
    capture = _CaptureBuilder()
    engine = _engine(payloads, prompt_builder=capture)
    original = Dispatcher._build_context

    calls: list[int] = []

    async def counting(self, request, session):
        calls.append(1)
        return await original(self, request, session)

    # Dispatcher declares __slots__, so the counter is applied to the class.
    with patch.object(Dispatcher, "_build_context", counting):
        result = await _dispatch(engine, owner_id=841019)

    assert result.success is True
    # One context, one prompt package — every provider round reuses them.
    assert len(calls) == 1
    assert len(capture.packages) == 1
    assert len(payloads) >= 1
    for payload in payloads:
        assert _payload_text(payload).count(capture.context.user_text) == 1


# ── 10. The prompt keeps its fixed section order ──


def test_section_order_is_unchanged_by_the_tool_block():
    from backend.ai.prompt.template import SECTION_ORDER

    ctx = _context_with_history(entries=2, chars_each=60)
    package = PromptBuilder().build(ctx, tool_block="[Available Tools]\n  - x() — y [safe]")

    assert list(SECTION_ORDER) == list(PromptSection)
    for section in SECTION_ORDER:
        assert section in package.sections
    assert package.sections[PromptSection.TOOL_METADATA].startswith("[Tool Context]")
