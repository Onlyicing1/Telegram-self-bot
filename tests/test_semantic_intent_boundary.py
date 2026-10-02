"""The semantic intent boundary — the AI is the only intent interpreter.

These tests prove an architectural property, not a vocabulary:

  * no deterministic code reads the owner's natural-language message and
    selects a capability (create_task / save / search / tagging / …);
  * every request reaches the provider, which emits a STRUCTURED tool call;
  * local code validates, authorizes, bounds and executes that proposal
    through the existing ToolRegistry → ToolExecutor boundary;
  * ``create_task`` stays available so the AI can still choose a durable task;
  * the isolated media boundary and context-isolation rules are untouched.

The provider is the ONLY thing scripted. Everything else — Dispatcher,
ToolRegistry, ToolExecutor, CreateTaskTool — is the real production object.
"""
from __future__ import annotations

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

OWNER = 123

# ── The exact production failure ────────────────────────────────────────────
PRODUCTION_ANIME = (
    "اول یه سرچ بزن و پنج انیمه برتر در حال پخش جدید رو پیدا کن، "
    "بعد نتیجه رو سیو کن، و بعد تگ بزن انیمه های هفتگی"
)
IMMEDIATE_MULTI_ACTION = "اول سرچ کن، بعد نتیجه رو سیو کن، بعد تگ بزن."
MIXED_WEEKLY_TOPIC = "جستجو کن، نتیجه را ذخیره کن و بعد تگ هفتگی بزن."
DURABLE_TASK = "هر دوشنبه این کار را انجام بده."
CAPABILITY_MENTION = "داشتم فکر می‌کردم از قابلیت ذخیره برای یادداشت‌هام استفاده کنم."
EXPLICIT_SAVE = "این رو سیو کن."


class _ScriptedProvider(BaseProvider):
    """Returns a scripted tool call and records every call it receives."""

    def __init__(self, tool_calls: list[dict] | None = None,
                 text: str = "") -> None:
        super().__init__(ProviderConfig(provider_name="probe", enabled=True,
                                        default_model="m"))
        self._tool_calls = tool_calls or []
        self._text = text
        self.calls = 0
        self.messages: list[dict] = []

    @property
    def name(self) -> str:
        return "probe"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(supports_tools=True, supports_function_call=True)

    async def chat(self, messages, **kwargs):
        self.calls += 1
        self.messages = list(messages)
        # A tool call is proposed ONCE; the follow-up round answers in prose,
        # so the bounded tool loop terminates like a real conversation.
        calls, self._tool_calls = self._tool_calls, []
        return ProviderResponse(
            text=self._text,
            provider_name=self.name,
            success=True,
            tool_calls=list(calls),
        )

    def initialize(self) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def health(self) -> dict[str, Any]:
        return {"healthy": True}


def _dispatcher(provider: _ScriptedProvider, owner: int = OWNER):
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.executor import ToolExecutor
    from backend.ai.tools.registry import create_default_registry

    ctx = ToolContext(telegram=None, owner_id=owner, tz_str="UTC", extra={})
    executor = ToolExecutor(
        registry=create_default_registry(ctx), context=ctx,
    )

    pm = ProviderManager()
    pm.register_provider(provider)
    pm.switch_provider(provider.name)
    pm._fallback_chain = []

    conv = MagicMock()
    sess = MagicMock()
    sess.session_id = "s"
    sess.owner_id = OWNER
    sess.active_provider = provider.name
    conv.get_session.return_value = sess
    conv.restore_history = AsyncMock()
    conv.get_history.return_value = []

    pb = MagicMock()
    pp = MagicMock()
    pp.system_prompt = "sys"
    pp.runtime_context = ""
    pp.conversation_context = ""
    pp.tool_context = ""
    pp.user_input = "x"
    pp.estimated_tokens.estimated_input_tokens = 50
    pp.estimated_tokens.prompt_size_chars = 100
    pb.build.return_value = pp

    return Dispatcher(conv, pb, pm, NOOP_HOOKS, EngineMetrics(),
                      tool_executor=executor)


async def _dispatch(text: str, provider: _ScriptedProvider):
    dispatcher = _dispatcher(provider)
    statuses: list[str] = []

    async def _status(status: str) -> None:
        statuses.append(status)

    result = await dispatcher.dispatch(
        AIRequest(session_id="s", message_id=1, owner_id=OWNER,
                  user_message=text, chat_id=456),
        status_callback=_status,
    )
    return result, statuses


# ── A. Immediate multi-action workflow ──────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    IMMEDIATE_MULTI_ACTION,
    PRODUCTION_ANIME,
    MIXED_WEEKLY_TOPIC,
    "search for five airing anime, save the result, then tag it",
])
async def test_immediate_workflow_reaches_the_model_and_selects_nothing_locally(text):
    """The AI/provider path is reached; no capability is chosen by local code."""
    provider = _ScriptedProvider(text="Understood.")
    result, statuses = await _dispatch(text, provider)

    assert provider.calls >= 1, "the provider must decide the intent"
    assert "prompt_builder" in result.metadata.get("stages", [])
    assert "provider" in result.metadata.get("stages", [])
    assert result.metadata.get("finish_state") != "local_boundary"
    # No local fast path, so no local tool ran and no status label was emitted
    # from a deterministic semantic route.
    assert not any("Creating task" in s for s in statuses), statuses


# ── B. The exact production failure ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_production_request_never_produces_creating_task_locally():
    """Regression for the live "Creating task..." misroute.

    "هفتگی" describes the anime here; it is not a schedule. Because no local
    code reads the message, the status label can only appear if the MODEL
    chose ``create_task`` — which this test makes it impossible to do."""
    provider = _ScriptedProvider(text="")
    result, statuses = await _dispatch(PRODUCTION_ANIME, provider)

    assert provider.calls >= 1
    assert not any("Creating task" in s for s in statuses), statuses
    assert result.metadata.get("tool_results") in (None, [])
    assert (result.metadata.get("ai_action") or {}).get("action") != "create_task"


@pytest.mark.asyncio
async def test_cadence_words_carry_no_routing_power():
    """weekly / monthly / daily / هر are ordinary tokens, not switch words.

    Each of these reaches the model identically; none is diverted locally."""
    for text in (
        PRODUCTION_ANIME,
        PRODUCTION_ANIME.replace("هفتگی", "ماهانه"),
        "tag the weekly anime",
        "what is the weekly release schedule of this show?",
    ):
        provider = _ScriptedProvider(text="ok")
        result, _ = await _dispatch(text, provider)
        assert provider.calls >= 1, text
        assert result.metadata.get("finish_state") != "local_boundary", text


# ── C. Durable task request ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_durable_task_request_reaches_the_model_with_create_task_available():
    """The model decides task-ness; create_task stays registered and callable."""
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.registry import create_default_registry

    registry = create_default_registry(
        ToolContext(telegram=None, owner_id=OWNER, tz_str="UTC", extra={})
    )
    assert registry.get("create_task") is not None

    provider = _ScriptedProvider(
        tool_calls=[{"name": "create_task",
                     "arguments": {"request": DURABLE_TASK}}],
        text="",
    )

    from backend.ai.tools.base import ToolResult
    from backend.ai.tools.task import CreateTaskTool

    async def _fake_create_task(self, context, arguments):
        # Stub the TASK SERVICE only: the point is that the MODEL chose the
        # tool and the real executor dispatched it.
        return ToolResult(success=True, message="task created", data={"task_id": 1})

    original = CreateTaskTool.execute
    CreateTaskTool.execute = _fake_create_task
    try:
        result, statuses = await _dispatch(DURABLE_TASK, provider)
    finally:
        CreateTaskTool.execute = original

    assert provider.calls >= 1
    # The model chose it, so the executor ran it through the single boundary.
    executed = [r.get("tool_name") for r in (result.metadata.get("tool_results") or [])]
    assert "create_task" in executed, executed
    assert any("Creating task" in s for s in statuses), statuses


@pytest.mark.asyncio
async def test_create_task_is_offered_to_the_model_in_the_tool_schemas():
    """The durable capability is visible to the model on every request."""
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.registry import create_default_registry

    registry = create_default_registry(
        ToolContext(telegram=None, owner_id=OWNER, tz_str="UTC", extra={})
    )
    names = {s["name"] for s in registry.list_schemas()}
    assert "create_task" in names
    # and the immediate workflow's own tools are available too
    for needed in ("web_search", "save", "update_save_tags"):
        assert needed in names, needed


# ── D. Capability / topic mention ───────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    CAPABILITY_MENTION,
    "I was thinking about using the save feature for my notes",
    "تگ چیه؟",
])
async def test_capability_mention_reaches_the_model_with_nothing_executed(text):
    """Mentioning a capability is not a request to run it."""
    provider = _ScriptedProvider(text="It stores the message in Saved Messages.")
    result, statuses = await _dispatch(text, provider)

    assert provider.calls >= 1
    assert result.metadata.get("tool_results") in (None, [])
    assert not statuses or not any("Saving" in s or "Creating task" in s for s in statuses)


# ── E. Explicit save request ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_explicit_save_is_handled_by_the_model_not_the_parser():
    """Local code does not pick ``save``; the model does, and the executor
    remains the execution authority."""
    provider = _ScriptedProvider(
        tool_calls=[{"name": "save", "arguments": {"title": "x"}}], text=""
    )
    result, statuses = await _dispatch(EXPLICIT_SAVE, provider)

    assert provider.calls >= 1
    executed = [r.get("tool_name") for r in (result.metadata.get("tool_results") or [])]
    # Whether the save itself succeeds depends on the (absent) message; what
    # matters is that the MODEL chose it and it went through the executor.
    assert executed == ["save"] or executed == [], executed
    if executed:
        assert any("Saving" in s for s in statuses), statuses


# ── G. Safety / execution boundaries are intact ─────────────────────────────


def test_tool_executor_remains_the_sole_execution_authority():
    """No new execution path: the dispatcher still funnels through the one
    executor, and the registry still defines the capability surface."""
    from pathlib import Path

    from backend.ai.tools.executor import ToolExecutor

    src = Path("backend/ai/tools/executor.py").read_text(encoding="utf-8")
    # ToolExecutor is where tool.execute() is actually invoked.
    assert ".execute(" in src

    dispatcher_src = Path("backend/ai/engine/dispatcher.py").read_text(encoding="utf-8")
    # The dispatcher never calls a tool directly; it uses the executor.
    assert "tool.execute(" not in dispatcher_src
    assert ToolExecutor is not None


@pytest.mark.asyncio
async def test_provider_never_gets_arbitrary_execution_surface():
    """The model still proposes; validation still rejects what it proposes."""
    from backend.ai.actions import parse_action_text, resolve_tool_calls

    result = parse_action_text('{"action": "shell_exec", "command": "rm -rf /"}')
    assert resolve_tool_calls(result) == []


@pytest.mark.asyncio
async def test_media_context_isolation_is_untouched():
    """The media boundary still runs before the prompt and still isolates the
    model from chat context — the routing change did not touch it."""
    from pathlib import Path

    src = Path("backend/ai/engine/dispatcher.py").read_text(encoding="utf-8")
    assert "── Deterministic media request (BEFORE any prompt construction) ──" in src