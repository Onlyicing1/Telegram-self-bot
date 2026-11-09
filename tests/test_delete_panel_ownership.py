"""The Delete Glass panel belongs to the Helper Bot — it is its ONLY editor.

Same contract Deep Save already follows:

* ``inline_message_id_from_event(event)`` is the one handle that can still edit
  the inline panel Telegram created for the Helper Bot;
* ``begin_panel_operation`` / ``finish_panel_operation`` make this operation the
  panel's writer for its duration;
* ``ExistingPanelEditor`` is built WITHOUT a ``self_client``, so a refused
  inline edit can never be retried through the owner's own account, no
  replacement message is ever sent, and a panel without an identifier is simply
  reported as unavailable while the deletion keeps its own semantics.

Every test drives the real panel handler / real ``delete_service`` against a
deterministic fake Telegram client, a fake Helper Bot and a real inline
identifier, so the editing principal is observable.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from telethon.tl.types import InputBotInlineMessageID

from backend.bot.handlers import delete as delete_ui
from backend.helper import callback_gate, inline_engine, input_state, panel_edit, target_context
from backend.services import delete_service

OWNER = 111
CHAT = -100
PANEL_MSG = 500
COMMAND_MSG = 900
INLINE_ID = InputBotInlineMessageID(dc_id=2, id=7, access_hash=99)


class Message:
    def __init__(self, mid, *, out=True, sender_id=OWNER, text=""):
        self.id = mid
        self.out = out
        self.sender_id = sender_id
        self.message = text
        self.text = text
        self.date = None
        self.media = None
        self.reply_to_msg_id = None


class FakeClient:
    def __init__(self, messages):
        self.messages = {message.id: message for message in messages}
        self.me = SimpleNamespace(id=OWNER)
        self.deleted: list[int] = []
        self.batches: list[list[int]] = []
        #: Panel edits attempted with the owner's own account — must stay empty.
        self.self_edits: list[tuple] = []
        self.fail_edits = False

    async def iter_messages(self, chat_id, **kwargs):
        limit = kwargs.get("limit")
        emitted = 0
        for mid in sorted(self.messages, reverse=True):
            yield self.messages[mid]
            emitted += 1
            if limit is not None and emitted >= limit:
                return

    async def get_messages(self, chat_id, ids):
        if isinstance(ids, (list, tuple)):
            return [self.messages.get(mid) for mid in ids]
        return self.messages.get(ids)

    async def delete_messages(self, chat_id, ids):
        batch = list(ids) if isinstance(ids, (list, tuple)) else [ids]
        self.batches.append(batch)
        for mid in batch:
            self.messages.pop(mid, None)
        self.deleted.extend(batch)

    async def edit_message(self, *args, **kwargs):
        self.self_edits.append((args, kwargs))


class FakeHelper:
    """The Helper Bot, and the only principal allowed to edit this panel."""

    def __init__(self, client: FakeClient) -> None:
        self._client = client
        self.inline_ids: list = []
        self.edits: list[str] = []

    async def edit_message(self, inline_id, text=None, buttons=None, **kwargs):
        self.inline_ids.append(inline_id)
        if self._client.fail_edits:
            raise RuntimeError("panel edit refused")
        self.edits.append(text)


class FakeEvent:
    def __init__(
        self, *, chat_id=CHAT, message_id=PANEL_MSG, inline_identifier=INLINE_ID,
    ):
        self.chat_id = chat_id
        self.message_id = message_id
        self.inline_message_id = None
        self.id = message_id
        self.out = True
        # What a CallbackQuery on an inline panel actually carries.
        self.query = SimpleNamespace(msg_id=inline_identifier)


def install(monkeypatch, messages, *, helper=None) -> FakeClient:
    client = FakeClient(messages)
    client.helper = helper if helper is not None else FakeHelper(client)
    monkeypatch.setattr(inline_engine, "_self_client", client)
    monkeypatch.setattr(delete_ui, "get_client", lambda: client.helper)
    return client


@pytest.fixture(autouse=True)
def _isolated():
    input_state.clear_all()
    target_context.clear_all()
    panel_edit.reset_panel_operations()
    for key in list(callback_gate._painters):
        callback_gate.forget_painter(*key)
    yield
    input_state.clear_all()
    target_context.clear_all()
    panel_edit.reset_panel_operations()


async def run_all(client, report=None):
    """Drive the frozen capability directly, through the same helper-owned
    painter the old ``allexec`` handler used. Returns ``(title, body, error)``."""
    report = report or delete_service.DeleteReport(
        requested="all of your messages in this chat",
    )
    paint = delete_ui._progress_painter(FakeEvent())
    error = None
    try:
        await paint({})
        _considered, _deleted, error = await delete_service.do_del_self_filtered(
            client, CHAT, report=report, progress=paint,
        )
    finally:
        paint.close()
    title, body = delete_ui._all_result_text(report, error=error)
    return title, body, error


@pytest.mark.asyncio
async def test_every_progress_edit_uses_the_helper_and_the_callback_identifier(monkeypatch):
    monkeypatch.setattr(delete_ui, "_PROGRESS_PAINT_INTERVAL", 0.0)
    client = install(monkeypatch, [Message(i) for i in range(1, 601)])

    await run_all(client)

    assert client.helper.edits, "the panel must be painted"
    assert all(inline is INLINE_ID for inline in client.helper.inline_ids)
    assert client.self_edits == []
    assert any("Scanning chat" in text for text in client.helper.edits)
    assert any("Processing" in text for text in client.helper.edits)


@pytest.mark.asyncio
async def test_scanning_deleting_and_final_status_share_one_panel_message(monkeypatch):
    monkeypatch.setattr(delete_ui, "_PROGRESS_PAINT_INTERVAL", 0.0)
    client = install(monkeypatch, [Message(i) for i in range(1, 601)])

    title, body, _error = await run_all(client)

    # Every stage was written to the SAME inline message the callback named:
    # one identifier, no replacement message, nothing sent to the chat.
    assert client.helper.inline_ids
    assert all(inline is INLINE_ID for inline in client.helper.inline_ids)
    assert title == "Delete — Completed"
    assert "Deleted: 600" in body
    assert "All selected messages were processed." in body
    assert not any("send" in name for name in vars(client.helper))


@pytest.mark.asyncio
async def test_a_refused_inline_edit_never_falls_back_to_the_self_client(monkeypatch):
    monkeypatch.setattr(delete_ui, "_PROGRESS_PAINT_INTERVAL", 0.0)
    client = install(monkeypatch, [Message(i) for i in range(1, 401)])
    client.fail_edits = True

    title, body, _error = await run_all(client)

    assert client.helper.inline_ids, "the Helper Bot was attempted"
    assert client.helper.edits == []
    assert client.self_edits == []      # never re-owned, never re-tried elsewhere
    assert len(client.deleted) == 400    # the deletion itself is unaffected
    assert title == "Delete — Completed"
    assert "Deleted: 400" in body


@pytest.mark.asyncio
async def test_a_missing_inline_identifier_reports_unavailable_and_still_deletes(
    monkeypatch, caplog
):
    monkeypatch.setattr(delete_ui, "_PROGRESS_PAINT_INTERVAL", 0.0)
    client = install(monkeypatch, [Message(i) for i in range(1, 301)])

    with caplog.at_level(logging.WARNING, logger="backend.bot.handlers.delete"):
        report = delete_service.DeleteReport(
            requested="all of your messages in this chat",
        )
        paint = delete_ui._progress_painter(FakeEvent(inline_identifier=None))
        try:
            await delete_service.do_del_self_filtered(
                client, CHAT, report=report, progress=paint,
            )
        finally:
            paint.close()
        title, body = delete_ui._all_result_text(report)
        _buttons = None

    assert client.helper.inline_ids == []
    assert client.helper.edits == []
    assert client.self_edits == []
    assert len(client.deleted) == 300
    assert title == "Delete — Completed"
    assert "panel edit path unavailable" in caplog.text


@pytest.mark.asyncio
async def test_a_missing_helper_reports_unavailable_and_still_deletes(monkeypatch, caplog):
    client = FakeClient([Message(i) for i in range(1, 301)])
    monkeypatch.setattr(inline_engine, "_self_client", client)
    monkeypatch.setattr(delete_ui, "get_client", lambda: None)

    with caplog.at_level(logging.WARNING, logger="backend.bot.handlers.delete"):
        report = delete_service.DeleteReport(
            requested="all of your messages in this chat",
        )
        paint = delete_ui._progress_painter(FakeEvent())
        try:
            await delete_service.do_del_self_filtered(
                client, CHAT, report=report, progress=paint,
            )
        finally:
            paint.close()
        title, body = delete_ui._all_result_text(report)

    assert client.self_edits == []
    assert len(client.deleted) == 300
    assert title == "Delete — Completed"
    assert "panel edit path unavailable" in caplog.text


@pytest.mark.asyncio
async def test_the_panel_token_is_released_when_the_operation_ends(monkeypatch):
    monkeypatch.setattr(delete_ui, "_PROGRESS_PAINT_INTERVAL", 60.0)
    client = install(monkeypatch, [Message(i) for i in range(1, 31)])

    await run_all(client)

    assert (CHAT, PANEL_MSG) not in panel_edit._generations


@pytest.mark.asyncio
async def test_input_result_edits_use_the_helper_too(monkeypatch):
    client = install(monkeypatch, [Message(i) for i in range(1, 21)])

    await delete_ui._del_n_input_handler(
        "3", CHAT, COMMAND_MSG, CHAT, PANEL_MSG, inline_id=INLINE_ID,
    )

    assert any(inline is INLINE_ID for inline in client.helper.inline_ids)
    assert any("Deleted 3" in text for text in client.helper.edits)
    assert client.self_edits == []
    # Only the typed command message was deleted, through the deletion API.
    assert [mid for mid in client.deleted if mid != COMMAND_MSG] == [20, 19, 18]


@pytest.mark.asyncio
async def test_the_chokepoint_stays_the_only_deletion_path(monkeypatch):
    client = install(monkeypatch, [Message(i) for i in range(1, 51)])
    calls: list[list[int]] = []

    async def _spy(chosen_client, chat_id, ids, **kwargs):
        calls.append(list(ids))
        return [], list(ids)

    monkeypatch.setattr(delete_service, "delete_verified_self_messages", _spy)

    await run_all(client)

    assert calls and all(isinstance(ids, list) for ids in calls)
    assert client.deleted == []


@pytest.mark.asyncio
async def test_the_frozen_all_callbacks_edit_nothing(monkeypatch):
    # The freeze: neither legacy callback touches the panel nor any message.
    client = install(monkeypatch, [Message(i) for i in range(1, 51)])

    for extra in ("all", "allexec"):
        title, body, buttons = await delete_ui._del_panel_handler(FakeEvent(), extra)

        assert client.helper.inline_ids == []
        assert client.helper.edits == []
        assert client.self_edits == []
        assert client.deleted == []
        assert title == "Delete"
        assert "Nothing was deleted" in body
