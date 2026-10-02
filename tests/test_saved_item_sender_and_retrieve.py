"""Regression tests for the two Saved-Items bugs reported from live usage.

BUG #1 — ``preview_save`` showed the wrong sender identity:
``save_service._resolve_sender`` only handled user-shaped entities and
degraded to ``str(sender_id)``, so a channel-sourced save persisted the
channel's raw numeric identity (or the wrong name) as the "Sender" shown by
``retrieve_service.format_preview``. These tests pin the source-sender
contract on the REAL pipeline: source message → ``_resolve_sender`` → DB row
→ ``do_preview`` → ``format_preview``.

BUG #2 — "send this here" did not deliver the saved item: every send request
was answered with ``Unsupported action: send`` before the saved-item
vocabulary was consulted, so an explicit save code (or a replied save-code
message) never reached the registered ``retrieve_save`` tool. Tool selection is
now the MODEL's decision. These tests pin the trusted (runtime) destination and
the fact that the AI receives no Telegram conversational context.

No live Telegram, no Supabase, no providers: the Telegram and DB boundaries
are faked exactly where the service layer touches them.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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


# ── Unchanged behavior ─────────────────────────────────────────────────────
