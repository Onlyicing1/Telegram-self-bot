"""Current-bio determinism — production regression for the exact symptom.

Production scenario this file locks down:

  Internal Bio Engine state: "NatN. disalle"   (engine last_bio — NOT the bio)
  Real Telegram full-profile bio: "I am I. Nothing more."
  User: "بیو رو نشونم بده"
  Expected user-visible result contains EXACTLY "I am I. Nothing more."
  Any result containing "NatN. disalle" is a FAILURE.

Layers covered:
  1. Deterministic intent resolution (fused pronoun show-forms).
  2. Real ToolRegistry + ToolExecutor + TelegramAPI chain (no Telegram network).
  3. Verbatim delivery — the dispatcher returns the tool result without a
     continuation provider round, so the model can never stylize/replace it.
  4. Distinct failure semantics (retrieval failure ≠ empty bio).
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.ai.actions import parse_command_intent

ENGINE_STATE = "NatN. disalle"        # Bio Engine last_bio — must NEVER appear
REAL_BIO = "I am I. Nothing more."    # authoritative full_user.about


# ── 1. Deterministic resolution (no provider decision needed) ──


@pytest.mark.parametrize(
    "text",
    [
        "بیو رو نشونم بده",
        "بیو رو نشون بده",
        "بیومو نشون بده",
        "بیوم رو نشون بده",
        "بیوم چیه",
        "بیوم چیه؟",
        "بیو الانم چیه",
        "بیو الانم چیه؟",
        "بیوی فعلیم چیه",
        "بیوی فعلیم رو بگو",
        "بیوی فعلیم رو نشون بده",
        "بیو اکانتم رو بگو",
        "بیو اکانتم رو نشون بده",
        "متن بیوم چیه",
        "متن بیوی فعلی من چیه",
        "بیو فعلیمو بگو",
        "بیو فعلیمو نشون بده",
        "بیو الان من چیه",
        "بیو الانم",
        "بیوم الان",
        "بیو فعلیم",
        "what is my bio?",
        "what's my bio?",
        "show my bio",
        "show me my bio",
        "tell me my bio",
        "what is my current bio?",
        "show my current bio",
        "my current bio",
        "current bio",
        "bio now",
    ],
)
def test_current_bio_requests_resolve_deterministically(text):
    r = parse_command_intent(text, has_reply=False)
    assert r.kind == "executable", f"{text!r} fell off the deterministic path"
    assert r.tool_calls == [{"name": "get_bio", "arguments": {}}]


@pytest.mark.parametrize(
    "text",
    [
        "وضعیت موتور بیو چیه؟",
        "bio engine status",
        "template بیوم چیه؟",
        "mood بیو چیه؟",
    ],
)
def test_explicit_engine_queries_stay_distinct_from_production_path(text):
    # Engine/configuration questions must never leak the engine's last_bio as
    # the CURRENT Telegram bio — the production request path is get_bio and
    # only get_bio. bio_show remains a separate registered tool for engine
    # state; the natural-language bio branch itself has always resolved bio
    # mentions to the real Telegram bio (source-verified behavior).
    from backend.ai.actions import parse_command_intent

    r = parse_command_intent(text, has_reply=False)
    assert r.kind == "executable"
    assert r.tool_calls[0]["name"] == "get_bio"


def test_non_bio_show_forms_do_not_regress():
    # The fused show-form matcher must not swallow other intents.
    r = parse_command_intent("پیام آخر رو نشونم بده", has_reply=False)
    assert r.tool_calls[0]["name"] == "list_recent_messages"
    r = parse_command_intent("سیوها رو نشونم بده", has_reply=False)
    assert r.tool_calls[0]["name"] == "list_saves"


# ── 2-3. Real chain: tool result is authoritative and verbatim ──


def _fake_telegram_api(bio: str | None = None, fail: bool = False):
    """A TelegramAPI-shaped fake that mimics entities.get_bio behavior."""
    from backend.telegram_api.exceptions import TelegramAPIError

    class FakeTelegramAPI:
        async def get_bio(self) -> str:
            if fail:
                raise TelegramAPIError("full-profile request failed")
            return bio or ""

        async def get_me(self):  # must NOT be used by the bio path
            raise AssertionError("get_me must not serve the bio path")

    return FakeTelegramAPI()


def _real_bio_tool_context(telegram: Any) -> Any:
    from backend.ai.tools.context import ToolContext

    return ToolContext(telegram=telegram, owner_id=777, tz_str="UTC")


@pytest.mark.asyncio
async def test_bio_engine_state_never_reaches_current_bio_result():
    from backend.ai.tools.bio import BioGetTool

    telegram = _fake_telegram_api(bio=REAL_BIO)
    ctx = _real_bio_tool_context(telegram)
    result = await BioGetTool(ctx).execute(ctx, {})

    assert result.success is True
    assert result.data == {"bio": REAL_BIO}
    assert ENGINE_STATE not in result.message
    assert ENGINE_STATE not in (result.data or {}).get("bio", "")


@pytest.mark.asyncio
async def test_get_bio_result_is_delivered_verbatim_without_provider_round():
    """End-to-end through the REAL Dispatcher fast path with a REAL executor.

    A provider that would stylize/hallucinate must NEVER be consulted: the
    deterministic fast path executes get_bio and returns the tool result.
    """
    from backend.ai.engine.dispatcher import Dispatcher
    from backend.ai.engine.hooks import NOOP_HOOKS
    from backend.ai.engine.metrics import EngineMetrics
    from backend.ai.providers.base.capabilities import ProviderCapabilities
    from backend.ai.providers.base.config import ProviderConfig
    from backend.ai.providers.base.contract import BaseProvider, ProviderResponse
    from backend.ai.providers.manager.manager import ProviderManager
    from backend.ai.session.request import AIRequest
    from backend.ai.tools.bio import BioGetTool
    from backend.ai.tools.executor import ToolExecutor
    from backend.ai.tools.registry import ToolRegistry, create_default_registry

    class StylizingProvider(BaseProvider):
        """Would emit the production symptom if it were ever consulted."""

        def __init__(self) -> None:
            super().__init__(ProviderConfig(provider_name="stylizer", enabled=True, default_model="m"))
            self.calls = 0

        @property
        def name(self) -> str:
            return "stylizer"

        @property
        def capabilities(self) -> ProviderCapabilities:
            return ProviderCapabilities(supports_tools=True, supports_function_call=True)

        async def chat(self, messages, **kwargs):
            self.calls += 1
            return ProviderResponse(
                text=f"𝓝atN. disalle — {ENGINE_STATE}",  # the symptom
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

    telegram = _fake_telegram_api(bio=REAL_BIO)
    ctx = _real_bio_tool_context(telegram)
    registry = create_default_registry(ctx)
    executor = ToolExecutor(registry, ctx)
    provider = StylizingProvider()

    pm = ProviderManager()
    pm.register_provider(provider)
    pm.switch_provider(provider.name)
    pm._fallback_chain = []

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
    pp.user_input = "بیو رو نشونم بده"
    pp.estimated_tokens.estimated_input_tokens = 50
    pp.estimated_tokens.prompt_size_chars = 100
    mock_pb.build.return_value = pp

    d = Dispatcher(mock_conv, mock_pb, pm, NOOP_HOOKS, EngineMetrics(), tool_executor=executor)

    result = await d.dispatch(AIRequest(
        session_id="s1", message_id=1, owner_id=777,
        user_message="بیو رو نشونم بده", chat_id=456,
    ))

    assert result.success is True
    assert REAL_BIO in result.response
    assert ENGINE_STATE not in result.response
    # No mathematical-alphanumeric Unicode may touch the bio value.
    assert "𝓝" not in result.response
    # The provider was never consulted — deterministic execution only.
    assert provider.calls == 0
    # The result came from the real tool execution (one round, real tool).
    assert result.metadata.get("tool_call_count") == 1
    tool_results = result.metadata.get("tool_results") or []
    assert tool_results and tool_results[0]["tool_name"] == "get_bio"
    assert tool_results[0]["data"]["bio"] == REAL_BIO


@pytest.mark.asyncio
async def test_native_tool_call_round_returns_bio_verbatim_without_continuation():
    """If the PROVIDER path emits get_bio natively, the result is still verbatim.

    The continuation provider round (where the model could restyle the
    value) is skipped for verbatim-read tools.
    """
    from backend.ai.engine.dispatcher import Dispatcher
    from backend.ai.engine.hooks import NOOP_HOOKS
    from backend.ai.engine.metrics import EngineMetrics
    from backend.ai.providers.base.capabilities import ProviderCapabilities
    from backend.ai.providers.base.config import ProviderConfig
    from backend.ai.providers.base.contract import BaseProvider, ProviderResponse
    from backend.ai.providers.manager.manager import ProviderManager
    from backend.ai.session.request import AIRequest
    from backend.ai.tools.bio import BioGetTool
    from backend.ai.tools.executor import ToolExecutor
    from backend.ai.tools.registry import ToolRegistry, create_default_registry

    class NativeToolProvider(BaseProvider):
        """Emits a native get_bio call, then WOULD restyle the result."""

        def __init__(self) -> None:
            super().__init__(ProviderConfig(provider_name="native", enabled=True, default_model="m"))
            self.calls = 0

        @property
        def name(self) -> str:
            return "native"

        @property
        def capabilities(self) -> ProviderCapabilities:
            return ProviderCapabilities(supports_tools=True, supports_function_call=True)

        async def chat(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return ProviderResponse(
                    text="",
                    provider_name=self.name,
                    success=True,
                    tool_calls=[{"name": "get_bio", "arguments": {}}],
                )
            return ProviderResponse(
                text=f"𝓝atN. disalle ({ENGINE_STATE})",  # the restyle attempt
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

    telegram = _fake_telegram_api(bio=REAL_BIO)
    ctx = _real_bio_tool_context(telegram)
    registry = create_default_registry(ctx)
    executor = ToolExecutor(registry, ctx)
    provider = NativeToolProvider()

    pm = ProviderManager()
    pm.register_provider(provider)
    pm.switch_provider(provider.name)
    pm._fallback_chain = []

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
    pp.user_input = "my profile summary please"
    pp.estimated_tokens.estimated_input_tokens = 50
    pp.estimated_tokens.prompt_size_chars = 100
    mock_pb.build.return_value = pp

    d = Dispatcher(mock_conv, mock_pb, pm, NOOP_HOOKS, EngineMetrics(), tool_executor=executor)

    result = await d.dispatch(AIRequest(
        session_id="s1", message_id=2, owner_id=777,
        user_message="my profile summary please", chat_id=456,
    ))

    assert result.success is True
    assert REAL_BIO in result.response
    assert ENGINE_STATE not in result.response
    assert "𝓝" not in result.response
    # Round 1 ran the tool; the restyling continuation round never happened.
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_create_task_native_round_still_gets_continuation():
    """Verbatim delivery is scoped to read tools — create_task keeps its
    normal continuation behavior (regression guard for the scoping)."""
    from backend.ai.engine.dispatcher import Dispatcher
    from backend.ai.engine.hooks import NOOP_HOOKS
    from backend.ai.engine.metrics import EngineMetrics
    from backend.ai.providers.base.capabilities import ProviderCapabilities
    from backend.ai.providers.base.config import ProviderConfig
    from backend.ai.providers.base.contract import BaseProvider, ProviderResponse
    from backend.ai.providers.manager.manager import ProviderManager
    from backend.ai.session.request import AIRequest
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.executor import ToolExecutor
    from backend.ai.tools.registry import ToolRegistry
    from backend.ai.tools.base import PermissionLevel, ToolResult

    class FakeTaskTool:
        """Duck-typed Tool (the base Tool is a Protocol — never instantiated)."""

        def __init__(self, context) -> None:
            self._context = context

        @property
        def name(self) -> str:
            return "create_task"

        @property
        def description(self) -> str:
            return "create a task"

        @property
        def parameters(self) -> dict[str, Any]:
            return {}

        @property
        def permission_level(self) -> PermissionLevel:
            return PermissionLevel.READ_WRITE

        async def execute(self, context, arguments) -> ToolResult:
            return ToolResult(success=True, message="Task #1 created.")

    class ContinuationProvider(BaseProvider):
        def __init__(self) -> None:
            super().__init__(ProviderConfig(provider_name="cont", enabled=True, default_model="m"))
            self.calls = 0

        @property
        def name(self) -> str:
            return "cont"

        @property
        def capabilities(self) -> ProviderCapabilities:
            return ProviderCapabilities(supports_tools=True, supports_function_call=True)

        async def chat(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return ProviderResponse(
                    text="", provider_name=self.name, success=True,
                    tool_calls=[{"name": "create_task", "arguments": {"request": "x"}}],
                )
            return ProviderResponse(
                text="Task created and scheduled.", provider_name=self.name, success=True,
            )

        def initialize(self) -> None:
            return None

        def shutdown(self) -> None:
            return None

        def count_tokens(self, text: str) -> int:
            return max(1, len(text) // 4)

        def health(self) -> dict[str, Any]:
            return {"healthy": True}

    ctx = ToolContext(telegram=None, owner_id=777, tz_str="UTC")
    registry = ToolRegistry()
    registry.register(FakeTaskTool(ctx))
    executor = ToolExecutor(registry, ctx)
    provider = ContinuationProvider()

    pm = ProviderManager()
    pm.register_provider(provider)
    pm.switch_provider(provider.name)
    pm._fallback_chain = []

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
    pp.user_input = "Nova یه تسک بساز"
    pp.estimated_tokens.estimated_input_tokens = 50
    pp.estimated_tokens.prompt_size_chars = 100
    mock_pb.build.return_value = pp

    d = Dispatcher(mock_conv, mock_pb, pm, NOOP_HOOKS, EngineMetrics(), tool_executor=executor)

    result = await d.dispatch(AIRequest(
        session_id="s1", message_id=3, owner_id=777,
        user_message="Nova یه تسک بساز", chat_id=456,
    ))

    # Two provider rounds: the tool call AND its continuation happened.
    assert provider.calls == 2
    assert "Task created and scheduled." in result.response


# ── 4. Distinct failure semantics ──


@pytest.mark.asyncio
async def test_retrieval_failure_is_not_reported_as_empty_bio():
    from backend.ai.tools.bio import BioGetTool

    telegram = _fake_telegram_api(fail=True)
    ctx = _real_bio_tool_context(telegram)
    result = await BioGetTool(ctx).execute(ctx, {})

    assert result.success is False
    assert "Bio: —" not in result.message


@pytest.mark.asyncio
async def test_empty_authoritative_bio_is_success_with_empty_data():
    from backend.ai.tools.bio import BioGetTool

    telegram = _fake_telegram_api(bio="")
    ctx = _real_bio_tool_context(telegram)
    result = await BioGetTool(ctx).execute(ctx, {})

    assert result.success is True
    assert result.data == {"bio": ""}
    assert "📝 Bio: —" in result.message


# ── 5. Exactly one get_bio tool; bio_show remains distinct ──


def test_registry_has_exactly_one_get_bio_and_separate_bio_show():
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.registry import create_default_registry

    ctx = ToolContext(telegram=_fake_telegram_api(bio=REAL_BIO), owner_id=1, tz_str="UTC")
    registry = create_default_registry(ctx)
    names = registry.list_names()
    assert names.count("get_bio") == 1
    assert names.count("bio_show") == 1
    assert "get_bio" in names and "bio_show" in names


@pytest.mark.asyncio
async def test_telegram_api_get_bio_is_authoritative_entity_path():
    """TelegramAPI.get_bio() delegates to entities.get_bio() (full-profile)."""
    from backend.telegram_api import api as api_module
    from backend.telegram_api.api import TelegramAPI

    client = MagicMock()

    async def fake_get_me():
        return SimpleNamespace(id=1)

    client.get_me = AsyncMock(side_effect=fake_get_me)

    async def fake_call(request):
        return SimpleNamespace(full_user=SimpleNamespace(about=REAL_BIO), users=[])

    client.side_effect = fake_call

    facade = TelegramAPI(client)
    bio = await facade.get_bio()
    assert bio == REAL_BIO
