"""Regression tests for the two Saved-Items bugs reported from live usage.

BUG #1 — ``preview_save`` showed the wrong sender identity:
``save_service._resolve_sender`` only handled user-shaped entities and
degraded to ``str(sender_id)``, so a channel-sourced save persisted the
channel's raw numeric identity (or the wrong name) as the "Sender" shown by
``retrieve_service.format_preview``. These tests pin the source-sender
contract on the REAL pipeline: source message → ``_resolve_sender`` → DB row
→ ``do_preview`` → ``format_preview``.

BUG #2 — "send this here" did not deliver the saved item:
``parse_command_intent`` answered every send request with
``Unsupported action: send`` before the saved-item vocabulary was consulted,
so an explicit save code (or a replied save-code message) never reached the
registered ``retrieve_save`` tool. These tests pin the deterministic route,
the trusted (runtime) destination, and the fact that the AI receives no
Telegram conversational context.

No live Telegram, no Supabase, no providers: the Telegram and DB boundaries
are faked exactly where the service layer touches them.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.ai.actions import parse_command_intent
from backend.ai.tools.context import ToolContext
from backend.ai.tools.executor import ToolExecutor
from backend.ai.tools.registry import create_default_registry
from backend.db import client as db_client
from backend.services import retrieve_service, save_service


OWNER = 777
OTHER_OWNER = 999
CHAT = -100123
ORIGIN_CHAT = -100555
SAVE_CODE_MSG = "**LifeOS** `S0001`\n**Saved** 2026-01-02 03:04"
ORDINARY_MSG = "سلام، این یه پیام معمولیه"


# ── Telegram-shaped fakes ───────────────────────────────────────────────────


class FakeUser:
    def __init__(self, uid=11, first_name="Ali", last_name="Rezaei", username=""):
        self.id = uid
        self.first_name = first_name
        self.last_name = last_name
        self.username = username


class FakeChannel:
    def __init__(self, cid=-100999, title="Design Channel"):
        self.id = cid
        self.title = title


_NO_SENDER = object()


class FakeSourceMessage:
    """A source message as Telethon exposes it (sender + chat are distinct)."""

    def __init__(
        self,
        *,
        sender_id=11,
        sender=_NO_SENDER,
        chat_id=ORIGIN_CHAT,
        msg_id=200,
        text="source text",
        media=None,
        post_author=None,
        chat_title="Origin Group",
    ):
        self.sender_id = sender_id
        if sender is _NO_SENDER:
            self._sender = FakeUser(uid=sender_id)
        else:
            self._sender = sender
        self.chat_id = chat_id
        self.id = msg_id
        self.text = text
        self.media = media
        self.post_author = post_author
        self._chat_title = chat_title

    async def get_sender(self):
        return self._sender

    async def get_chat(self):
        return FakeChannel(cid=self.chat_id, title=self._chat_title)


class FakeSent:
    def __init__(self, media=None):
        self.chat_id = "me"
        self.id = 600
        self.media = media


class FakeSaveClient:
    """Only the calls ``execute_save`` makes for a TEXT source."""

    def __init__(self):
        self.calls = []

    async def send_message(self, entity, text):
        self.calls.append(("send_message", entity, text))
        return FakeSent()

    async def send_file(self, entity, file, **kwargs):  # pragma: no cover
        raise AssertionError("a text-only source must never upload media")

    async def download_media(self, *a, **kw):  # pragma: no cover
        raise AssertionError("a text-only source must never download media")

    async def forward_messages(self, *a, **kw):  # pragma: no cover
        raise AssertionError("Deep Save must never forward")


@pytest.fixture(autouse=True)
def reset_fallback():
    db_client._fallback["saved_items"] = []
    db_client._fallback["bot_logs"] = []
    yield
    db_client._fallback["saved_items"] = []
    db_client._fallback["bot_logs"] = []


async def _save_and_row(msg, owner_id=OWNER):
    client = FakeSaveClient()
    result = await save_service.execute_save(client, owner_id, msg, "UTC")
    assert "Saved Successfully" in result
    row = db_client._fallback["saved_items"][-1]
    return result, row


# ── BUG #1: the Sender field is the SOURCE message sender ───────────────────


@pytest.mark.asyncio
async def test_sender_is_the_source_sender_not_the_origin_chat():
    """A user sender in a group: their name, never the group's title."""
    msg = FakeSourceMessage(
        sender_id=11, sender=FakeUser(11, "Ali", "Rezaei"), chat_title="Origin Group",
    )

    name, sender_id = await save_service._resolve_sender(msg)

    assert name == "Ali Rezaei"
    assert sender_id == 11
    assert name != msg._chat_title


@pytest.mark.asyncio
async def test_sender_falls_back_to_username_then_to_its_own_id():
    named = FakeSourceMessage(
        sender_id=12, sender=FakeUser(12, first_name="", last_name="", username="sara"),
    )
    anonymous = FakeSourceMessage(sender_id=0, sender=None, chat_title="Origin Group")

    assert await save_service._resolve_sender(named) == ("sara", 12)
    name, sender_id = await save_service._resolve_sender(anonymous)
    assert (name, sender_id) == ("Unknown", 0)
    assert name != "Origin Group"


@pytest.mark.asyncio
async def test_a_channel_sender_shows_its_own_name_never_a_raw_identity():
    """Telethon reports the CHANNEL as the sender of a channel post."""
    channel = FakeChannel(-1001234567890, "Design Channel")
    msg = FakeSourceMessage(sender_id=channel.id, sender=channel, chat_title="Design Channel")

    name, sender_id = await save_service._resolve_sender(msg)

    assert name == "Design Channel"
    assert not name.lstrip("-").isdigit()
    assert sender_id == channel.id  # the identity is preserved separately


@pytest.mark.asyncio
async def test_a_signed_channel_post_names_the_author_not_the_channel_title():
    channel = FakeChannel(-1001234567890, "Design Channel")
    msg = FakeSourceMessage(
        sender_id=channel.id, sender=channel,
        post_author="Ali Rezaei", chat_title="Design Channel",
    )

    name, _ = await save_service._resolve_sender(msg)

    assert name == "Ali Rezaei"
    assert name != "Design Channel"


@pytest.mark.asyncio
async def test_an_unresolved_sender_keeps_its_own_id_not_the_chat_title():
    msg = FakeSourceMessage(sender_id=-100777888999, sender=None, chat_title="Origin Group")

    name, sender_id = await save_service._resolve_sender(msg)

    assert name != "Origin Group"
    assert str(sender_id) in name


@pytest.mark.asyncio
async def test_save_then_preview_shows_the_source_sender():
    """The full path: source sender → DB row → ``do_preview`` text."""
    msg = FakeSourceMessage(
        sender_id=11, sender=FakeUser(11, "Ali", "Rezaei"), chat_title="Origin Group",
    )
    result, row = await _save_and_row(msg)
    code = row["save_code"]

    with patch.object(db_client, "query_save", AsyncMock(return_value=row)):
        text = await retrieve_service.do_preview(None, OWNER, code)

    assert f"**Sender** Ali Rezaei" in text
    assert "Origin Group" not in text
    assert code in result


@pytest.mark.asyncio
async def test_a_saved_row_keeps_sender_and_origin_chat_as_distinct_values():
    channel = FakeChannel(-1001234567890, "Design Channel")
    msg = FakeSourceMessage(
        sender_id=channel.id, sender=channel, chat_id=ORIGIN_CHAT,
        post_author="Ali Rezaei", chat_title="Design Channel",
    )

    _result, row = await _save_and_row(msg)

    assert row["sender_name"] == "Ali Rezaei"
    assert row["sender_id"] == channel.id
    assert row["origin_chat_id"] == ORIGIN_CHAT
    assert row["origin_msg_id"] == 200
    # The origin chat identity is never what the Sender field displays.
    assert row["sender_name"] != "Design Channel" or row["origin_chat_id"] == channel.id


@pytest.mark.asyncio
async def test_preview_owner_isolation_is_unchanged():
    _result, row = await _save_and_row(FakeSourceMessage(sender_id=11, sender=FakeUser(11, "Ali", "Rezaei")))
    foreign = {**row, "owner_id": OTHER_OWNER}

    with patch.object(db_client, "query_save", AsyncMock(return_value=foreign)):
        text = await retrieve_service.do_preview(None, OWNER, row["save_code"])

    assert "No item found" in text
    assert "Ali Rezaei" not in text


# ── BUG #2: retrieve_save reaches the request's own chat ────────────────────


@pytest.mark.parametrize(
    "request_text",
    [
        "S0001 رو بفرست",
        "بفرست S0001",
        "سیو S0001 رو اینجا بفرست",
        "send S0001 here",
        "send S0001",
    ],
)
def test_an_explicit_save_code_with_a_send_request_routes_to_retrieve_save(request_text):
    result = parse_command_intent(request_text, has_reply=False)

    assert result.kind == "executable"
    assert result.action == "retrieve_save"
    assert result.target == "current_chat"
    assert result.save_code == "S0001"
    assert result.tool_calls == [
        {"name": "retrieve_save", "arguments": {"save_code": "S0001"}}
    ]


def test_a_replied_save_code_message_routes_a_send_request_to_retrieve_save():
    for request_text in ("بفرست", "اینو بفرست", "send this here"):
        result = parse_command_intent(request_text, has_reply=True, reply_text=SAVE_CODE_MSG)

        assert result.kind == "executable", request_text
        assert result.tool_calls == [
            {"name": "retrieve_save", "arguments": {"save_code": "S0001"}}
        ]


def test_a_send_with_no_item_reference_keeps_its_existing_outcome():
    """No save code and no save-code reply: nothing is guessed."""
    bare = parse_command_intent("اینجا بفرست", has_reply=False)
    assert bare.kind == "unsupported" and bare.tool_calls == []

    ordinary_reply = parse_command_intent("بفرست", has_reply=True, reply_text=ORDINARY_MSG)
    assert ordinary_reply.kind == "unsupported" and ordinary_reply.tool_calls == []


def test_recipient_directed_sends_stay_unsupported():
    result = parse_command_intent("اینو برای علی بفرست", has_reply=False)

    assert result.kind == "unsupported" and result.action == "send"
    assert result.tool_calls == []


def test_text_write_precedence_is_unchanged():
    result = parse_command_intent("بنویس سلام", has_reply=False)

    assert result.tool_calls == [{"name": "send_message", "arguments": {"text": "سلام"}}]


def test_the_retrieve_call_carries_only_the_save_code():
    """No destination, no chat id, no replied content ever reaches the model."""
    result = parse_command_intent(
        "بفرست", has_reply=True, reply_text=SAVE_CODE_MSG,
    )

    serialized = json.dumps(result.tool_calls)
    assert serialized == '[{"name": "retrieve_save", "arguments": {"save_code": "S0001"}}]'
    for forbidden in ("chat_id", "destination", "target_chat", "LifeOS", "Saved", CHAT):
        assert str(forbidden) not in serialized


def test_the_retrieve_tool_exposes_no_model_controllable_destination():
    ctx = ToolContext(telegram=None, owner_id=OWNER, tz_str="UTC", extra={"chat_id": CHAT})
    registry = create_default_registry(ctx)

    tool = registry.get("retrieve_save")
    assert tool is not None
    # Save V2 Part 3: the tool accepts a save code OR a name/tag query that
    # the deterministic resolver turns into candidates. It still exposes NO
    # model-controllable destination — the destination is trusted context.
    assert set(tool.parameters) == {"save_code", "query"}
    assert tool.required_arguments == ()
    assert tool.required_any_arguments == ("save_code", "query")
    for forbidden in ("chat_id", "destination", "target_chat", "recipient", "target"):
        assert forbidden not in tool.parameters


@pytest.mark.asyncio
async def test_the_trusted_request_chat_reaches_do_retrieve_end_to_end():
    """Deterministic route → ToolExecutor → tool → service, one chat only."""
    ctx = ToolContext(
        telegram=MagicMock(client=MagicMock()), owner_id=OWNER, tz_str="UTC",
        extra={"chat_id": CHAT, "request_id": "retrieve-destination"},
    )
    registry = create_default_registry(ctx)
    executor = ToolExecutor(registry, ctx)

    parsed = parse_command_intent("سیو S0001 رو اینجا بفرست", has_reply=False)
    assert parsed.tool_calls

    with patch.object(
        retrieve_service, "do_retrieve",
        AsyncMock(return_value="✅ Retrieved `S0001` to this chat."),
    ) as svc:
        results = await executor.execute_calls(
            parsed.tool_calls, owner_id=OWNER, session_id="retrieve-destination",
            context_override=ctx,
        )

    svc.assert_awaited_once()
    client, owner_id, save_code, target_chat = svc.await_args.args
    assert owner_id == OWNER
    assert save_code == "S0001"
    assert target_chat == CHAT  # the chat the request came from — nothing else
    assert results[0].success is True


@pytest.mark.asyncio
async def test_a_retrieve_request_never_builds_a_prompt_or_calls_a_provider():
    """The whole point: the AI is not consulted and receives zero context."""
    from backend.ai.conversation.context_builder import ReplyContext
    from backend.ai.engine.dispatcher import Dispatcher
    from backend.ai.engine.hooks import NOOP_HOOKS
    from backend.ai.engine.metrics import EngineMetrics
    from backend.ai.session.request import AIRequest
    from backend.ai.tools.executor import ToolExecutionResult

    mock_te = MagicMock()
    mock_te.execute_calls = AsyncMock(return_value=[
        ToolExecutionResult(
            tool_name="retrieve_save", success=True,
            message="✅ Retrieved `S0001` to this chat.",
            data={"save_code": "S0001", "chat_id": CHAT},
        ),
    ])
    mock_te._context = MagicMock()
    mock_te._context.extra = {}
    mock_te._context.telegram = None
    mock_te._context.tz_str = "UTC"
    mock_te._context.client = None

    mock_pm = MagicMock()
    mock_pm.get_active_name.return_value = "test"
    mock_pm.get_active.return_value.config.model = "m"
    mock_pm.get_active.return_value.health.return_value = {"healthy": True}
    mock_pm.get_active.return_value.chat = AsyncMock()

    mock_conv = MagicMock()
    mock_sess = MagicMock()
    mock_sess.session_id = "s"
    mock_sess.owner_id = OWNER
    mock_sess.active_provider = "test"
    mock_conv.get_session.return_value = mock_sess
    mock_conv.restore_history = AsyncMock()
    mock_conv.get_history.return_value = []

    mock_pb = MagicMock()
    dispatcher = Dispatcher(
        mock_conv, mock_pb, mock_pm, NOOP_HOOKS, EngineMetrics(), tool_executor=mock_te,
    )

    result = await dispatcher.dispatch(AIRequest(
        session_id="s", message_id=57494, owner_id=OWNER,
        user_message="بفرست", chat_id=CHAT,
        reply_context=ReplyContext(
            exists=True, message_id=57495, chat_id=CHAT, sender_id=OWNER,
            text_preview=SAVE_CODE_MSG,
        ),
    ))

    calls = mock_te.execute_calls.await_args.args[0]
    assert calls == [{"name": "retrieve_save", "arguments": {"save_code": "S0001"}}]
    assert mock_te.execute_calls.await_args.kwargs["context_override"].extra["chat_id"] == CHAT
    assert result.success is True and "Retrieved" in result.response

    mock_pm.get_active.return_value.chat.assert_not_awaited()
    mock_pb.build.assert_not_called()


# ── Unchanged behavior ─────────────────────────────────────────────────────


def test_saved_item_deletion_is_unchanged():
    explicit = parse_command_intent("سیو S0001 رو پاک کن", has_reply=False)
    assert explicit.tool_calls == [
        {"name": "delete_save", "arguments": {"save_code": "S0001"}}
    ]

    replied = parse_command_intent("delete this", has_reply=True, reply_text=SAVE_CODE_MSG)
    assert replied.tool_calls == [
        {"name": "delete_save", "arguments": {"save_code": "S0001"}}
    ]


def test_ordinary_message_deletion_is_unchanged():
    replied = parse_command_intent("اینو پاک کن", has_reply=True, reply_text=ORDINARY_MSG)
    assert replied.tool_calls == [{"name": "delete_replied", "arguments": {}}]

    counted = parse_command_intent("۱۰ پیام آخر رو پاک کن", has_reply=False)
    assert counted.action == "delete_messages"
    assert counted.tool_calls[0]["name"] == "delete"


def test_save_and_list_intents_are_unchanged():
    save = parse_command_intent("اینو سیو کن", has_reply=True)
    assert save.tool_calls == [{"name": "save", "arguments": {}}]

    listed = parse_command_intent("لیست سیوها رو بده", has_reply=False)
    assert listed.tool_calls == [{"name": "list_saves", "arguments": {}}]
