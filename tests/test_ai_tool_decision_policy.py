"""
AI tool-decision policy — behavioural validation (Stage 6, §6).

The MODEL makes every intent decision; these tests exercise the REAL decision
pipeline (Dispatcher → PromptBuilder → ProviderManager → ToolRegistry →
ToolExecutor → existing service) with a scripted provider standing in for the
model. They assert what the pipeline makes POSSIBLE and what it actually
executes — never that a scripted text implies model understanding, and never
with local keyword/pre-fix routing. No production code contains any special
case for "One Piece", for searching, for Persian text, or for any example here.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from backend.ai.conversation.context_builder import ReplyContext
from backend.ai.engine.engine import Engine
from backend.ai.providers.base.capabilities import ProviderCapabilities
from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.base.contract import BaseProvider, ProviderResponse
from backend.ai.providers.manager.manager import ProviderManager
from backend.ai.providers.registry.registry import ProviderRegistry
from backend.ai.session.request import AIRequest
from backend.ai.tools.base import PermissionLevel, ToolResult
from backend.ai.tools.context import ToolContext
from backend.ai.tools.registry import ToolRegistry, create_default_registry
from backend.telegram_api import TelegramAPI

CHAT = -100555300


# ── scripted provider (the stand-in for the model) ──


class _ScriptedProvider(BaseProvider):
    """Replays scripted responses in order, recording every payload."""

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


def _text(text: str) -> ProviderResponse:
    return ProviderResponse(text=text, provider_name="scripted", success=True)


def _tool(name: str, arguments: dict[str, Any], call_id: str = "call_1") -> ProviderResponse:
    return ProviderResponse(
        text="",
        provider_name="scripted",
        success=True,
        tool_calls=[{"id": call_id, "name": name, "arguments": arguments}],
    )


class _FakeClient:
    async def get_messages(self, chat_id, ids=None):
        return object()


def _tool_context(owner_id: int, client=None) -> ToolContext:
    """Mirror the supervisor's wiring: the TelegramAPI facade is always set.

    Production builds one ``ToolContext(telegram=TelegramAPI(client), ...)``
    and hands it to BOTH the registry factory and the executor; a context
    carrying only a bare client would exercise a shape the runtime never
    creates.
    """
    telegram = TelegramAPI(client) if client is not None else None
    return ToolContext(telegram=telegram, owner_id=owner_id, tz_str="UTC", client=client)


def _registry(owner_id: int):
    return create_default_registry(_tool_context(owner_id))


def _engine(provider, registry=None, owner_id: int = 1, client=None) -> Engine:
    provider_registry = ProviderRegistry()
    provider_registry.register(provider)
    engine = Engine(providers=ProviderManager(provider_registry))
    context = _tool_context(owner_id, client)
    if registry is None:
        registry = create_default_registry(context)
    engine.attach_tools(registry, context=context)
    return engine


async def _dispatch(engine: Engine, message: str, **overrides):
    base: dict[str, Any] = {
        "session_id": "policy-session",
        "user_message": message,
        "owner_id": 1,
        "chat_id": CHAT,
        "message_id": 1,
    }
    base.update(overrides)
    return await engine.execute(AIRequest(**base))


def _system_text(payload: dict[str, Any]) -> str:
    return "\n".join(
        str(m.get("content", "")) for m in payload["messages"] if m.get("role") == "system"
    )


def _tool_names(payload: dict[str, Any]) -> set[str]:
    return {d["function"]["name"] for d in payload["tools"]}


# ── CASE 1 — current factual information ──


@pytest.mark.asyncio
async def test_case1_current_information_question_gets_the_search_capability_and_policy():
    """The pipeline must make a current-info request answerable by the model.

    It delivers (a) the web_search tool with an accurate schema, (b) the
    current-information policy in the system instructions, (c) the request
    verbatim — and executes NOTHING before the model decides.
    """
    owner = 930001
    provider = _ScriptedProvider()
    engine = _engine(provider, registry=_registry(owner), owner_id=owner)
    question = "قسمت بعدی وان پیس کی میاد؟"

    result = await _dispatch(engine, question, owner_id=owner)

    payload = provider.payloads[0]
    assert "web_search" in _tool_names(payload)
    web_search = next(
        d["function"] for d in payload["tools"] if d["function"]["name"] == "web_search"
    )
    assert web_search["parameters"]["required"] == ["query"]
    system = _system_text(payload)
    assert "depends on current or external facts" in system
    assert "web_search" in system
    assert payload["messages"][-1]["role"] == "user"
    assert payload["messages"][-1]["content"] == question
    # No local selection: nothing ran before the model had its say.
    assert result.metadata["tool_call_count"] == 0


# ── CASE 2 — explicit search ──


@pytest.mark.asyncio
async def test_case2_explicit_search_executes_with_valid_arguments_and_flows_back():
    owner = 930002
    provider = _ScriptedProvider([
        _tool("web_search", {"query": "One Piece next episode release date"}),
        _text("Episode 1140 airs on the date returned by the search."),
    ])
    engine = _engine(provider, registry=_registry(owner), owner_id=owner)

    from backend.services import web_search_service

    async def fake_search(query, **kwargs):
        return (True, f"🔎 Results for: {query}", {"results": [{"title": "Source", "url": "https://example.com/1"}], "query": query})

    with patch.object(web_search_service, "do_web_search", AsyncMock(side_effect=fake_search)) as svc:
        result = await _dispatch(
            engine, "وب سرچ بزن و تاریخ قسمت بعدی وان پیس رو پیدا کن.", owner_id=owner,
        )

    assert svc.await_count == 1
    assert svc.await_args.args[0] == "One Piece next episode release date"
    assert result.response == "Episode 1140 airs on the date returned by the search."
    # The REAL tool result reached the model as a protocol tool message.
    continuation = provider.payloads[1]
    tool_message = next(m for m in continuation["messages"] if m["role"] == "tool")
    assert "Results for: One Piece next episode release date" in tool_message["content"]
    assert tool_message["tool_call_id"] == "call_1"


# ── CASE 3 — implicit search (no word "search" anywhere) ──


@pytest.mark.asyncio
async def test_case3_implicit_current_information_is_not_gated_by_local_keywords():
    owner = 930003
    provider = _ScriptedProvider()
    engine = _engine(provider, registry=_registry(owner), owner_id=owner)
    question = "who won the champions league final last night?"
    assert "search" not in question and "web" not in question

    result = await _dispatch(engine, question, owner_id=owner)

    payload = provider.payloads[0]
    assert "web_search" in _tool_names(payload)
    assert "depends on current or external facts" in _system_text(payload)
    assert payload["messages"][-1]["content"] == question
    # Nothing happened locally: no keyword gate, no fast path, no execution,
    # and the model's own answer is what the owner receives.
    assert result.metadata["tool_call_count"] == 0
    assert result.response == "understood"


# ── CASE 4 — immediate multi-step workflow ──


@pytest.mark.asyncio
async def test_case4_ordered_multi_step_workflow_runs_each_tool_in_order():
    """search → save → tag, each chosen by the (scripted) model, in order."""
    owner = 930004
    provider = _ScriptedProvider([
        _tool("web_search", {"query": "top new anime"}, "call_1"),
        _tool("save", {"tags": ["anime"]}, "call_2"),
        _tool("update_save_tags", {"save_code": "S0001", "tags": ["anime"], "mode": "add"}, "call_3"),
        _text("Saved the result and tagged it."),
    ])
    engine = _engine(provider, owner_id=owner, client=_FakeClient())

    from backend.services import retrieve_service, save_service, web_search_service

    class _Outcome:
        save_code = "S0001"

        def __str__(self):
            return "✅ Saved to Saved Messages (S0001)"

    async def fake_search(query, **kwargs):
        return (True, "🔎 Five airing shows", {"results": [{"title": "Show", "url": "https://example.com/s"}], "query": query})

    reply = ReplyContext(
        exists=True, message_id=55, sender_id=owner, sender_name="Owner",
        chat_id=CHAT, chat_title="Saved Messages", text_preview="a message to save",
    )

    with (
        patch.object(web_search_service, "do_web_search", AsyncMock(side_effect=fake_search)) as search_svc,
        patch.object(save_service, "execute_save", AsyncMock(return_value=_Outcome())) as save_svc,
        patch.object(
            retrieve_service, "resolve_management_target",
            AsyncMock(return_value=SimpleNamespace(status=retrieve_service.TARGET_OK, save_code="S0001", message="")),
        ),
        patch.object(retrieve_service, "do_edit_tags", AsyncMock(return_value="✅ Tags updated for S0001")) as tags_svc,
    ):
        result = await _dispatch(
            engine,
            "اول سرچ کن، پنج مورد برتر رو پیدا کن، بعد ذخیرهشون کن و تگ بزن.",
            owner_id=owner,
            reply_context=reply,
        )

    assert result.response == "Saved the result and tagged it."
    assert search_svc.await_count == 1
    assert save_svc.await_count == 1
    assert tags_svc.await_count == 1
    assert tags_svc.await_args.args[2] == "add"  # (owner_id, save_code, mode, tags)
    assert result.metadata["tool_rounds"] == 3
    executed = [r["tool_name"] for r in result.metadata["tool_results"]]
    assert executed == ["web_search", "save", "update_save_tags"]
    # Each result fed the NEXT model decision: the initial payload plus one
    # continuation payload per tool result (the last one produced the answer).
    assert len(provider.payloads) == 4


# ── CASE 5 — durable task ──


@pytest.mark.asyncio
async def test_case5_durable_request_reaches_the_model_with_create_task_available():
    """The model decides task-ness; the executor runs what it chose."""
    owner = 930005
    provider = _ScriptedProvider([_tool("create_task", {"request": "هر هفته این کار رو انجام بده."})])
    engine = _engine(provider, registry=_registry(owner), owner_id=owner)

    from backend.ai.tools.task import CreateTaskTool

    async def fake_create_task(self, context, arguments):
        return ToolResult(success=True, message="task created", data={"task_id": 1})

    with patch.object(CreateTaskTool, "execute", fake_create_task):
        result = await _dispatch(engine, "هر هفته این کار رو انجام بده.", owner_id=owner)

    payload = provider.payloads[0]
    assert "create_task" in _tool_names(payload)
    system = _system_text(payload)
    assert "DURABLE TASK" in system and "IMMEDIATE WORKFLOW" in system
    executed = [r["tool_name"] for r in result.metadata.get("tool_results") or []]
    assert executed == ["create_task"]


# ── CASE 6 — capability mention only ──


@pytest.mark.asyncio
async def test_case6_capability_mention_executes_nothing():
    owner = 930006
    provider = _ScriptedProvider([_text("ذخیره یعنی پیام رو در Saved Messages نگه میدارم.")])
    engine = _engine(provider, registry=_registry(owner), owner_id=owner)

    result = await _dispatch(engine, "چه ابزارهایی برای ذخیره کردن داری؟", owner_id=owner)

    assert provider.payloads[0]["messages"][-1]["content"] == "چه ابزارهایی برای ذخیره کردن داری؟"
    assert result.metadata["tool_call_count"] == 0
    assert result.metadata.get("tool_results") in (None, [])
    assert result.response == "ذخیره یعنی پیام رو در Saved Messages نگه میدارم."


# ── CASE 7 — explicit save ──


@pytest.mark.asyncio
async def test_case7_explicit_save_uses_the_replied_target_without_inventing_metadata():
    owner = 930007
    provider = _ScriptedProvider([
        _tool("save", {}),
        _text("ذخیره شد."),
    ])
    engine = _engine(provider, owner_id=owner, client=_FakeClient())

    from backend.services import save_service

    class _Outcome:
        save_code = "S0007"

        def __str__(self):
            return "✅ Saved to Saved Messages (S0007)"

    reply = ReplyContext(
        exists=True, message_id=77, sender_id=owner, sender_name="Owner",
        chat_id=CHAT, chat_title="Saved Messages", text_preview="این رو ذخیره کن",
    )

    with patch.object(save_service, "execute_save", AsyncMock(return_value=_Outcome())) as svc:
        result = await _dispatch(engine, "این رو ذخیره کن.", owner_id=owner, reply_context=reply)

    assert svc.await_count == 1
    _client, _owner, saved_message, _tz = svc.await_args.args[:4]
    assert saved_message is not None  # the REAL replied-to message travelled as the target
    metadata = svc.await_args.kwargs.get("metadata")
    assert metadata is not None
    # The model passed no name/tags, so none were invented at this boundary.
    assert not getattr(metadata, "display_name", "") and not getattr(metadata, "tags", [])
    assert result.response == "ذخیره شد."
    assert [r["tool_name"] for r in result.metadata["tool_results"]] == ["save"]


# ── CASE 8 — genuinely ambiguous request ──


@pytest.mark.asyncio
async def test_case8_ambiguous_request_clarifies_without_inventing_an_action():
    owner = 930008
    provider = _ScriptedProvider([
        _text('{"action":"clarify","reason":"کدام مورد را انجام دهم؟"}'),
    ])
    engine = _engine(provider, registry=_registry(owner), owner_id=owner)

    result = await _dispatch(engine, "اون رو انجام بده.", owner_id=owner)

    assert result.response == "کدام مورد را انجام دهم؟"
    assert result.metadata["tool_call_count"] == 0
    assert result.metadata["ai_action"]["action"] == "clarify"


@pytest.mark.asyncio
async def test_case8_conversational_clarification_also_executes_nothing():
    owner = 930009
    provider = _ScriptedProvider([_text("کدام پیام را میخواهید پاک کنم؟")])
    engine = _engine(provider, registry=_registry(owner), owner_id=owner)

    result = await _dispatch(engine, "اون رو پاک کن.", owner_id=owner)

    assert result.response == "کدام پیام را میخواهید پاک کنم؟"
    assert result.metadata["tool_call_count"] == 0


# ── CASE 9 — multi-turn correction ──


@pytest.mark.asyncio
async def test_case9_correction_sees_the_previous_turn_and_can_redo_with_a_tool():
    owner = 930010
    provider = _ScriptedProvider([
        _text("میتونی خودت گوگل کنی."),                     # turn 1 (the answer to correct)
        _text("میتونی خودت گوگل کنی."),                     # turn 1 bounded format nudge: model repeats itself
        _tool("web_search", {"query": "One Piece next episode"}, "call_1"),  # turn 2 correction
        _text("قسمت بعدی ۲۴ آبان پخش میشود."),
    ])
    engine = _engine(provider, registry=_registry(owner), owner_id=owner)

    await _dispatch(engine, "قسمت بعدی وان پیس کی میاد؟", owner_id=owner, message_id=1)
    provider.payloads.clear()

    from backend.services import web_search_service

    async def fake_search(query, **kwargs):
        return (True, f"🔎 {query}: next episode", {"results": [], "query": query})

    correction = "نه، تو سرچ کن — اگر میخواستم خودم سرچ کنم بهت میگفتم."
    with patch.object(web_search_service, "do_web_search", AsyncMock(side_effect=fake_search)) as svc:
        result = await _dispatch(engine, correction, owner_id=owner, message_id=2)

    # The previous turn (request + answer) reached the model, so the
    # correction has a referent...
    system = _system_text(provider.payloads[0])
    assert "قسمت بعدی وان پیس کی میاد؟" in system
    assert "میتونی خودت گوگل کنی." in system
    # ... and the model's corrected choice executed through the real pipeline.
    assert svc.await_count == 1
    assert result.response == "قسمت بعدی ۲۴ آبان پخش میشود."


# ── CASE 10 — tool-result continuation ──


@pytest.mark.asyncio
async def test_case10_model_consumes_a_result_and_calls_another_tool():
    owner = 930011
    provider = _ScriptedProvider([
        _tool("web_search", {"query": "anime season 2026"}, "call_1"),
        _tool("web_search", {"query": "anime season 2026 schedule"}, "call_2"),
        _text("بر اساس نتایج، برنامه فصل جدید این است."),
    ])
    engine = _engine(provider, registry=_registry(owner), owner_id=owner)

    from backend.services import web_search_service

    async def fake_search(query, **kwargs):
        return (True, f"🔎 results for {query}", {"results": [], "query": query})

    with patch.object(web_search_service, "do_web_search", AsyncMock(side_effect=fake_search)) as svc:
        result = await _dispatch(engine, "برنامه فصل جدید انیمهها رو پیدا کن", owner_id=owner)

    assert svc.await_count == 2
    assert [c.args[0] for c in svc.await_args_list] == [
        "anime season 2026", "anime season 2026 schedule",
    ]
    # The second call was decided AFTER the first real result reached the model.
    second_payload = provider.payloads[1]
    first_result = next(m for m in second_payload["messages"] if m["role"] == "tool")
    assert "results for anime season 2026" in first_result["content"]
    assert result.response == "بر اساس نتایج، برنامه فصل جدید این است."
    assert len(provider.payloads) == 3
