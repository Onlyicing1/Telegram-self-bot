from __future__ import annotations

from types import SimpleNamespace
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
from backend.ai.tools.base import PermissionLevel, ToolResult
from backend.ai.tools.registry import ToolRegistry
from backend.bot.handlers import ghost_seen_v2 as handler
from backend.helper import inline_engine
from backend.helper import panels
from backend.helper.lifecycle import get_lifecycle
from backend.helper.panels import get_action
from backend.services import ghost_seen_v2 as service


class _Provider(BaseProvider):
    def __init__(self, responses: list[ProviderResponse] | None = None) -> None:
        super().__init__(ProviderConfig(provider_name="test", enabled=True, default_model="model"))
        self.responses = list(responses or [])
        self.calls: list[dict] = []

    @property
    def name(self) -> str:
        return "test"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(supports_tools=True, supports_function_call=True)

    async def chat(self, messages, **kwargs):
        self.calls.append(kwargs)
        if self.responses:
            return self.responses.pop(0)
        return ProviderResponse(text="ok", provider_name=self.name, success=True)

    def initialize(self) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def health(self) -> dict:
        return {"healthy": True, "provider": self.name}


class _PingTool:
    name = "ping"
    description = "Return pong"
    parameters = {}
    permission_level = PermissionLevel.READ_ONLY
    safe = True
    return_type = "text"
    long_running = False

    async def execute(self, ctx, args):
        return ToolResult(success=True, message="pong")


class _SearchClient:
    def __init__(self, entities: dict[int, object], messages: dict[int, list[object]] | None = None):
        self.entities = entities
        self.messages = messages or {}
        self.deleted: list[tuple[int, list[int]]] = []

    async def get_entity(self, entity_id: int):
        return self.entities[int(entity_id)]

    def iter_messages(self, chat_id: int, **kwargs):
        async def stream():
            for message in self.messages.get(int(chat_id), []):
                yield message

        return stream()

    async def delete_messages(self, chat_id: int, ids: list[int]):
        self.deleted.append((int(chat_id), list(ids)))


class _HelperClient:
    def __init__(self):
        self.edits: list[tuple[int, int, str, list]] = []

    async def edit_message(self, entity, message, text=None, *, buttons=None):
        self.edits.append((int(entity), int(message), text or "", buttons or []))


def _entity(chat_id: int, first_name: str, username: str = ""):
    return SimpleNamespace(
        id=chat_id,
        first_name=first_name,
        last_name="",
        username=username,
        bot=False,
        is_bot=False,
        is_self=False,
        deleted=False,
    )


def _message(message_id: int, text: str):
    return SimpleNamespace(id=message_id, text=text, message=text, date=None)


def _dispatcher(provider: _Provider):
    registry = ToolRegistry()
    registry.register(_PingTool())

    manager = ProviderManager()
    manager.register_provider(provider)
    manager.switch_provider(provider.name)
    manager._fallback_chain = []

    conversation = MagicMock()
    session = MagicMock()
    session.session_id = "session"
    session.owner_id = 1
    conversation.get_session.return_value = session
    conversation.restore_history = AsyncMock()
    conversation.get_history.return_value = []

    from backend.ai.prompt.budget import TokenBudget
    from backend.ai.prompt.builder import PromptPackage

    prompt_builder = MagicMock()
    package = PromptPackage(
        system_prompt="system",
        runtime_context="",
        conversation_context="",
        tool_context="",
        user_input="reply",
        metadata={},
        estimated_tokens=TokenBudget(
            estimated_input_tokens=10,
            estimated_output_budget=0,
            estimated_total=10,
            prompt_size_chars=20,
            max_total_tokens=8500,
            max_output_tokens=1000,
            within_budget=True,
        ),
        sections={},
    )
    prompt_builder.build.return_value = package

    executor = MagicMock()
    executor.execute_calls = AsyncMock()
    executor._context = MagicMock()
    executor._context.extra = {}
    executor._context.telegram = None
    executor._context.client = None
    executor._context.tz_str = "UTC"

    return Dispatcher(
        conversation,
        prompt_builder,
        manager,
        NOOP_HOOKS,
        EngineMetrics(),
        tool_registry=registry,
        tool_executor=executor,
    ), executor


@pytest.mark.asyncio
async def test_disabled_request_hides_tools_and_blocks_provider_tool_calls():
    provider = _Provider([
        ProviderResponse(
            text="",
            provider_name="test",
            success=True,
            tool_calls=[{"id": "call-1", "name": "ping", "arguments": {}}],
        ),
    ])
    dispatcher, executor = _dispatcher(provider)

    result = await dispatcher.dispatch(AIRequest(
        session_id="s1",
        user_message="reply",
        owner_id=1,
        chat_id=123,
        message_id=456,
        allow_tools=False,
    ))

    assert result.success is False
    assert result.metadata["failure_type"] == "tools_disabled"
    assert result.metadata["tool_calls_blocked"] == 1
    assert provider.calls[0].get("tools") in (None, [])
    executor.execute_calls.assert_not_awaited()


@pytest.mark.asyncio
async def test_normal_owner_request_retains_tool_access():
    provider = _Provider([ProviderResponse(text="ok", provider_name="test", success=True)])
    dispatcher, _ = _dispatcher(provider)

    result = await dispatcher.dispatch(AIRequest(
        session_id="s1",
        user_message="reply",
        owner_id=1,
        chat_id=123,
        message_id=456,
    ))

    assert result.success is True
    assert provider.calls[0]["tools"]
    assert provider.calls[0]["tools"][0]["function"]["name"] == "ping"


@pytest.mark.asyncio
async def test_ghost_seen_constructs_tool_disabled_request(monkeypatch):
    service.reset_allowed_chats()
    service.clear_selection(123)
    service.allow_chat(123)
    service.toggle_selection(123, 10)
    handler._ai_states.clear()
    handler._ai_locks.clear()

    engine = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(success=True, response="reply")))
    send = AsyncMock(return_value={})
    client = _SearchClient({}, {123: [_message(9, "previous")]})
    target = service.ViewerMessage(10, 123, "target")

    monkeypatch.setattr(inline_engine, "get_self_client", lambda: client)
    monkeypatch.setattr(inline_engine, "get_owner_id", lambda: 99)
    monkeypatch.setattr(handler, "_load_target_message", AsyncMock(return_value=target))
    monkeypatch.setattr(handler, "send_reply", send)
    monkeypatch.setattr("backend.ai.engine.engine.get_engine", lambda: engine)

    await handler._run_ai_reply(50, 123, 10, 1, False, "User")

    request = engine.execute.await_args.args[0]
    assert request.allow_tools is False
    send.assert_awaited_once()
    assert send.await_args.args[1:3] == (123, 10)

    service.clear_selection(123)
    handler._ai_states.clear()
    handler._ai_locks.clear()


@pytest.fixture
def search_state(monkeypatch):
    service.reset_allowed_chats()
    service.invalidate_manage_directory()
    lifecycle = get_lifecycle()
    lifecycle.sessions.destroy(60, 5)
    lifecycle.clear_render(60, 5)
    lifecycle.sessions.create(60, 5, panel_type="ghost_seen_v2", extra="")
    client = _SearchClient(
        {
            101: _entity(101, "User Ali"),
            202: _entity(202, "User Sara"),
        },
        {
            101: [_message(10, "hello")],
            202: [_message(20, "hi")],
        },
    )
    helper = _HelperClient()
    monkeypatch.setattr(inline_engine, "_self_client", client)
    monkeypatch.setattr(inline_engine, "_owner_id", 999)
    monkeypatch.setattr(handler, "get_client", lambda: helper)
    service.allow_chat(101)
    service.allow_chat(202)
    yield lifecycle, client, helper
    lifecycle.sessions.destroy(60, 5)
    lifecycle.clear_render(60, 5)
    service.reset_allowed_chats()


@pytest.mark.asyncio
async def test_main_search_persists_query_and_keeps_distinct_result_ids(search_state):
    lifecycle, _client, helper = search_state

    await handler._search_input_handler("User", 60, 77, 60, 5)

    assert lifecycle.sessions.current_nav(60, 5) == ("ghost_seen_v2", "p=1&q=User")
    assert len(helper.edits) == 1
    buttons = helper.edits[0][3]
    result_data = {
        bytes(getattr(button, "data", b"")).decode("utf-8")
        for row in buttons
        for button in row
        if bytes(getattr(button, "data", b"")).decode("utf-8").startswith("action:ghost_seen_v2_open:")
    }
    assert result_data == {"action:ghost_seen_v2_open:101", "action:ghost_seen_v2_open:202"}


@pytest.mark.asyncio
async def test_main_search_callback_reaches_registered_open_action(search_state, monkeypatch):
    lifecycle, client, _helper = search_state
    handler.register(object(), owner_id=999)
    assert get_action("ghost_seen_v2_open") is handler._open_chat_action

    await handler._search_input_handler("Ali", 60, 77, 60, 5)

    captured = {}

    async def capture_render(event, result, panel_id, chat_id, msg_id):
        captured.update(result=result, panel_id=panel_id, chat_id=chat_id, msg_id=msg_id)

    monkeypatch.setattr(panels, "_render_and_edit", capture_render)
    event = SimpleNamespace(chat_id=60, message_id=0)
    await panels._handle_action(event, "ghost_seen_v2_open:101", 60, 5, 999)

    assert captured["chat_id"] == 60
    assert captured["msg_id"] == 5
    assert captured["result"][0] == "👀 Ghost Seen"
    assert "Ali" in captured["result"][1]
    nav = lifecycle.sessions.current_nav(60, 5)
    assert nav is not None and nav[0] == "ghost_seen_v2_viewer"
    assert "source=101" in nav[1]
    assert client.deleted == [(60, [77])]
