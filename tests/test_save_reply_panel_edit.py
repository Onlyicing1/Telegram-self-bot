"""Reply Mode drives the EXISTING inline panel — never a second message.

The contract under test:

  * the inline identifier Telegram gives the Helper Bot at click time is
    captured, carried through the pending input, and used to edit that exact
    panel after the callback has returned;
  * the panel shows a loading line BEFORE target resolution, then the real
    transfer milestones, then the terminal result — all on one message;
  * no progress/result message is ever sent, on either edit path;
  * a panel without an identifier is edited through its chat/message id by the
    account that sent it, and a panel that cannot be addressed at all runs the
    save with no UI rather than inventing a surface;
  * the keyboard is left untouched on both paths (no ``buttons`` argument);
  * a newer Save operation on the same panel wins, and a stale one cannot
    overwrite it;
  * the owner's reply is deleted after the terminal result, and only it.

The fakes implement pinned Telethon 1.43.0's shapes (``edit_message(entity,
message=None, text=None, *, buttons=None)``); they prove application behaviour
ONLY — nothing here proves Telegram accepts the real inline edit.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from telethon import events
from telethon.tl import types

from backend.bot.handlers import save as save_handler
from backend.db import client as db_client
from backend.helper import inline_engine, input_state, panel_edit
from backend.helper.inline_sender import register_input_listener
from backend.helper.panel_edit import ExistingPanelEditor

from tests.test_save_album_view_once_progress import (
    PANEL_CHAT,
    PANEL_MSG,
    FakeClient,
    FakeMessage,
    photo_message,
)

OWNER = 42
REPLY_ID = 602
TARGET_ID = 601


# ── fakes ──


class RecordingHelper:
    """The Helper Bot surface the panel editor is allowed to use."""

    def __init__(self, error: Exception | None = None):
        self.error = error
        self.edits: list[str] = []
        self.calls: list[dict] = []

    async def edit_message(self, entity, message=None, text=None, *, buttons=None, **kwargs):
        self.calls.append(
            {
                "entity": entity,
                "message": message,
                "text": text,
                "buttons": buttons,
                "kwargs": kwargs,
            }
        )
        self.edits.append(text)
        if self.error is not None:
            raise self.error
        return True

    async def send_message(self, *args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("Reply Mode must never send a message")


class NewArmingDuringUpload(FakeClient):
    """A second Save claims the same panel while the first one transfers."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.rearmed = False

    async def send_file(self, entity, file, caption=None, progress_callback=None, **kwargs):
        if not self.rearmed:
            self.rearmed = True
            panel_edit.begin_panel_operation(PANEL_CHAT, PANEL_MSG)
        return await super().send_file(
            entity, file, caption=caption,
            progress_callback=progress_callback, **kwargs,
        )


class _ListenerClient:
    def __init__(self):
        self.handler = None

    def on(self, builder):
        def decorator(func):
            self.handler = func
            return func

        return decorator


class ClickDuringUpload(FakeClient):
    """The owner clicks the panel while its Save is still transferring."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.clicked = False

    async def send_file(self, entity, file, caption=None, progress_callback=None, **kwargs):
        if not self.clicked:
            self.clicked = True
            panel_edit.supersede_panel_operation(PANEL_CHAT, PANEL_MSG)
        return await super().send_file(
            entity, file, caption=caption,
            progress_callback=progress_callback, **kwargs,
        )


class _CallbackClient:
    def __init__(self):
        self.handlers: dict = {}

    def on(self, builder):
        def decorator(func):
            self.handlers[type(builder).__name__] = func
            return func

        return decorator


# ── helpers ──


def _inline_id(msg_id: int = PANEL_MSG):
    return types.InputBotInlineMessageID64(
        dc_id=2, owner_id=OWNER, id=msg_id, access_hash=99
    )


def _inline_click(msg_id: int = PANEL_MSG):
    """A REAL Telethon event built from the inline callback update."""
    update = types.UpdateInlineBotCallbackQuery(
        query_id=1,
        user_id=OWNER,
        msg_id=_inline_id(msg_id),
        chat_instance=5,
        data=b"action:save_reply",
    )
    return events.CallbackQuery.build(update)


def _message_click():
    """A REAL Telethon event for an ordinary (non-inline) message click."""
    update = types.UpdateBotCallbackQuery(
        query_id=2,
        user_id=OWNER,
        peer=types.PeerUser(OWNER),
        msg_id=PANEL_MSG,
        chat_instance=5,
        data=b"action:save_reply",
    )
    return events.CallbackQuery.build(update)


def _target_and_reply(*, target_id: int = TARGET_ID, reply_to: int | None = TARGET_ID):
    target = photo_message(target_id, text="hello", download_size=200_000)
    reply = FakeMessage(
        REPLY_ID, chat_id=PANEL_CHAT, text="save", reply_to_msg_id=reply_to
    )
    return target, reply


def _wire(client):
    inline_engine.set_self_client(client)
    inline_engine.set_owner_id(OWNER)


def _helper_calls_texts(helper: RecordingHelper) -> list[str]:
    return [call["text"] for call in helper.calls]


# ── fixtures ──


@pytest.fixture(autouse=True)
def reset_state():
    db_client._fallback["saved_items"] = []
    db_client._fallback["bot_logs"] = []
    input_state.clear_all()
    panel_edit.reset_panel_operations()
    inline_engine.set_self_client(None)
    inline_engine.set_owner_id(0)
    yield
    input_state.clear_all()
    panel_edit.reset_panel_operations()
    inline_engine.set_self_client(None)
    inline_engine.set_owner_id(0)


# ── 1. capturing the identifier ──


def test_the_inline_identifier_is_read_from_a_real_inline_click():
    event = _inline_click()

    captured = panel_edit.inline_message_id_from_event(event)

    assert isinstance(captured, types.InputBotInlineMessageID64)
    assert captured is event.query.msg_id
    assert (captured.id, captured.dc_id, captured.access_hash) == (PANEL_MSG, 2, 99)
    assert event.via_inline is True


def test_an_ordinary_message_click_yields_no_inline_identifier():
    event = _message_click()

    assert panel_edit.inline_message_id_from_event(event) is None
    assert event.via_inline is False


def test_arming_carries_the_identifier_and_the_panel_coordinates():
    inline_engine.set_owner_id(OWNER)

    _title, body, _buttons = _run(save_handler._save_reply_action(_inline_click(), "", PANEL_CHAT))

    pending = input_state.get_pending(OWNER)
    assert "Waiting for your reply" in body
    assert isinstance(pending["inline_id"], types.InputBotInlineMessageID64)
    assert pending["inline_id"].id == PANEL_MSG
    assert pending["inline_chat_id"] == PANEL_CHAT
    assert pending["inline_msg_id"] == PANEL_MSG


def test_arming_on_a_plain_message_panel_carries_no_identifier():
    inline_engine.set_owner_id(OWNER)

    _run(save_handler._save_reply_action(_message_click(), "", PANEL_CHAT))

    assert input_state.get_pending(OWNER)["inline_id"] is None


def test_the_input_listener_hands_the_identifier_only_to_handlers_that_declare_it():
    client = _ListenerClient()
    register_input_listener(client, OWNER)
    received: dict = {}

    async def declaring(text, chat_id, msg_id, inline_chat_id, inline_msg_id, *, inline_id=None):
        received["declaring"] = inline_id

    async def plain(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
        received["plain"] = True

    event = SimpleNamespace(
        chat_id=PANEL_CHAT,
        raw_text="save",
        sender_id=OWNER,
        message=SimpleNamespace(id=REPLY_ID),
    )

    input_state.set_pending(
        OWNER, "save_reply", declaring, PANEL_CHAT, "p",
        inline_chat_id=PANEL_CHAT, inline_msg_id=PANEL_MSG,
        inline_id=_inline_id(), timeout=None,
    )
    _run(client.handler(event))
    assert received["declaring"].id == PANEL_MSG

    input_state.set_pending(
        OWNER, "save_reply", plain, PANEL_CHAT, "p",
        inline_chat_id=PANEL_CHAT, inline_msg_id=PANEL_MSG,
        inline_id=_inline_id(), timeout=None,
    )
    _run(client.handler(event))
    assert received["plain"] is True


# ── 2. the panel is the surface ──


def test_the_existing_panel_is_edited_through_the_inline_identifier(monkeypatch):
    inline_id = _inline_id()
    helper = RecordingHelper()
    target, reply = _target_and_reply()
    client = FakeClient([target, reply])
    _wire(client)
    monkeypatch.setattr(save_handler, "get_client", lambda: helper)

    _run(
        save_handler._save_reply_wait_handler(
            "save", PANEL_CHAT, REPLY_ID, PANEL_CHAT, PANEL_MSG, inline_id=inline_id
        )
    )

    assert helper.calls, "the Helper Bot must perform the panel edits"
    for call in helper.calls:
        assert call["entity"] is inline_id, "every edit addresses that exact panel"
        assert call["message"] is None
        assert isinstance(call["text"], str) and call["text"]
    # A status edit carries no markup, so the keyboard it found survives. Only
    # the FINISHED panel replaces it, with the next actions of this save.
    assert all(call["buttons"] is None for call in helper.calls[:-1])
    assert len(helper.calls[-1]["buttons"]) == 5
    assert client.edit_calls == []
    assert client.send_message_calls == []
    assert len(db_client._fallback["saved_items"]) == 1


def test_loading_progress_and_the_final_result_land_on_that_one_panel(monkeypatch):
    inline_id = _inline_id()
    helper = RecordingHelper()
    target, reply = _target_and_reply()
    client = FakeClient([target, reply])
    _wire(client)
    monkeypatch.setattr(save_handler, "get_client", lambda: helper)

    _run(
        save_handler._save_reply_wait_handler(
            "save", PANEL_CHAT, REPLY_ID, PANEL_CHAT, PANEL_MSG, inline_id=inline_id
        )
    )

    texts = _helper_calls_texts(helper)
    assert "Resolving the target message" in texts[0]
    assert any("Downloading 50%" in text for text in texts)
    assert any("Uploading 100%" in text for text in texts)
    assert "Saved Successfully" in texts[-1]
    # The result text is rendered once (closing the reporter), then the SAME
    # terminal text is written again only to attach the panel's next actions.
    assert texts[-1] == texts[-2]
    assert all("Saved Successfully" not in text for text in texts[:-2])


def test_a_failed_save_reports_its_failure_on_the_same_panel(monkeypatch):
    async def exploding_save(*args, **kwargs):
        raise RuntimeError("upload rejected")

    inline_id = _inline_id()
    helper = RecordingHelper()
    target, reply = _target_and_reply()
    client = FakeClient([target, reply])
    _wire(client)
    monkeypatch.setattr(save_handler, "get_client", lambda: helper)
    monkeypatch.setattr(save_handler.save_service, "execute_save", exploding_save)

    _run(
        save_handler._save_reply_wait_handler(
            "save", PANEL_CHAT, REPLY_ID, PANEL_CHAT, PANEL_MSG, inline_id=inline_id
        )
    )

    assert "upload rejected" in _helper_calls_texts(helper)[-1]
    assert client.send_message_calls == []


def test_a_message_that_is_not_a_reply_reports_on_the_panel(monkeypatch):
    inline_id = _inline_id()
    helper = RecordingHelper()
    target, reply = _target_and_reply(reply_to=None)
    client = FakeClient([target, reply])
    _wire(client)
    monkeypatch.setattr(save_handler, "get_client", lambda: helper)

    _run(
        save_handler._save_reply_wait_handler(
            "save", PANEL_CHAT, REPLY_ID, PANEL_CHAT, PANEL_MSG, inline_id=inline_id
        )
    )

    assert "not a reply" in _helper_calls_texts(helper)[-1].lower()
    assert client.send_message_calls == []
    assert [call[1] for call in client.delete_calls] == [[REPLY_ID]]


def test_no_status_or_result_message_is_ever_sent(monkeypatch):
    inline_id = _inline_id()
    helper = RecordingHelper()
    target, reply = _target_and_reply()
    client = FakeClient([target, reply])
    _wire(client)
    monkeypatch.setattr(save_handler, "get_client", lambda: helper)

    _run(
        save_handler._save_reply_wait_handler(
            "save", PANEL_CHAT, REPLY_ID, PANEL_CHAT, PANEL_MSG, inline_id=inline_id
        )
    )

    assert client.send_message_calls == []
    assert "send_message" not in client.calls


def test_the_reply_is_deleted_once_after_the_terminal_result(monkeypatch):
    inline_id = _inline_id()
    helper = RecordingHelper()
    target, reply = _target_and_reply()
    client = FakeClient([target, reply])
    _wire(client)
    monkeypatch.setattr(save_handler, "get_client", lambda: helper)

    _run(
        save_handler._save_reply_wait_handler(
            "save", PANEL_CHAT, REPLY_ID, PANEL_CHAT, PANEL_MSG, inline_id=inline_id
        )
    )

    assert client.delete_calls == [(PANEL_CHAT, [REPLY_ID])]
    assert TARGET_ID not in {msg_id for _chat, ids in client.delete_calls for msg_id in ids}


# ── 3. fallbacks and missing identifiers ──


def test_a_refused_inline_edit_falls_back_to_the_panels_own_coordinates(monkeypatch):
    helper = RecordingHelper(error=RuntimeError("MESSAGE_ID_INVALID"))
    target, reply = _target_and_reply()
    client = FakeClient([target, reply])
    _wire(client)
    monkeypatch.setattr(save_handler, "get_client", lambda: helper)

    _run(
        save_handler._save_reply_wait_handler(
            "save", PANEL_CHAT, REPLY_ID, PANEL_CHAT, PANEL_MSG,
            inline_id=_inline_id(),
        )
    )

    # The refused inline edit is attempted ONCE, then the same message is
    # updated through its chat/message id — never a new message.
    assert len(helper.calls) == 1, "one refused inline edit, then the fallback"
    assert client.edit_calls
    for call in client.edit_calls:
        assert (call["chat_id"], call["msg_id"]) == (PANEL_CHAT, PANEL_MSG)
        assert call["buttons"] is None
    assert "Saved Successfully" in client.edit_calls[-1]["text"]
    assert client.send_message_calls == []


def test_a_panel_without_an_identifier_is_edited_by_the_account_that_sent_it(monkeypatch):
    helper = RecordingHelper()
    target, reply = _target_and_reply()
    client = FakeClient([target, reply])
    _wire(client)
    monkeypatch.setattr(save_handler, "get_client", lambda: helper)

    _run(
        save_handler._save_reply_wait_handler(
            "save", PANEL_CHAT, REPLY_ID, PANEL_CHAT, PANEL_MSG
        )
    )

    assert helper.calls == [], "no identifier means no inline edit"
    assert client.edit_calls
    assert {(c["chat_id"], c["msg_id"]) for c in client.edit_calls} == {
        (PANEL_CHAT, PANEL_MSG)
    }
    assert client.send_message_calls == []
    assert "Saved Successfully" in client.edit_calls[-1]["text"]


def test_a_panel_that_cannot_be_addressed_runs_the_save_without_ui(monkeypatch):
    helper = RecordingHelper()
    target, reply = _target_and_reply()
    client = FakeClient([target, reply])
    _wire(client)
    monkeypatch.setattr(save_handler, "get_client", lambda: helper)

    _run(save_handler._save_reply_wait_handler("save", PANEL_CHAT, REPLY_ID, 0, 0))

    assert helper.calls == []
    assert client.edit_calls == []
    assert client.send_message_calls == [], "no UI is never a new message"
    assert len(db_client._fallback["saved_items"]) == 1


def test_not_modified_is_not_treated_as_a_failure():
    helper = RecordingHelper(error=RuntimeError("MessageNotModifiedError: content is not modified"))
    editor = ExistingPanelEditor(
        chat_id=PANEL_CHAT, msg_id=PANEL_MSG, inline_id=_inline_id(),
        helper=helper, self_client=None,
    )

    assert _run(editor("same")) is True


def test_an_editor_without_any_target_is_unavailable():
    editor = ExistingPanelEditor()

    assert editor.available is False
    assert editor.path == "unavailable"
    assert _run(editor("text")) is False


# ── 4. staleness and concurrency ──


def test_a_stale_generation_cannot_write_to_the_panel():
    client = FakeClient([])
    old_generation = panel_edit.begin_panel_operation(PANEL_CHAT, PANEL_MSG)
    editor = ExistingPanelEditor(
        chat_id=PANEL_CHAT, msg_id=PANEL_MSG, self_client=client,
        generation=old_generation,
    )
    assert editor.available is True

    panel_edit.begin_panel_operation(PANEL_CHAT, PANEL_MSG)  # a newer Save

    assert _run(editor("stale progress")) is False
    assert client.edit_calls == []


def test_a_newer_arming_supersedes_an_in_flight_save(monkeypatch):
    helper = RecordingHelper()
    target, reply = _target_and_reply()
    client = NewArmingDuringUpload([target, reply])
    _wire(client)
    monkeypatch.setattr(save_handler, "get_client", lambda: helper)

    _run(
        save_handler._save_reply_wait_handler(
            "save", PANEL_CHAT, REPLY_ID, PANEL_CHAT, PANEL_MSG, inline_id=_inline_id()
        )
    )

    assert client.rearmed is True
    assert len(db_client._fallback["saved_items"]) == 1, "the save still completes"
    assert all("Saved Successfully" not in text for text in _helper_calls_texts(helper)), (
        "the superseded save must not overwrite the panel's newer state"
    )


def test_a_click_on_the_panel_supersedes_the_background_writer():
    generation = panel_edit.begin_panel_operation(PANEL_CHAT, PANEL_MSG)
    client = FakeClient([])
    editor = ExistingPanelEditor(
        chat_id=PANEL_CHAT, msg_id=PANEL_MSG, self_client=client,
        generation=generation,
    )

    assert panel_edit.supersede_panel_operation(PANEL_CHAT, PANEL_MSG) is True
    assert _run(editor("progress")) is False
    assert client.edit_calls == []

    # A panel no background flow owns is untouched by an ordinary click.
    assert panel_edit.supersede_panel_operation(1, 2) is False


def test_the_callback_router_supersedes_a_background_writer(monkeypatch):
    from backend.helper import panels as panels_module
    from backend.helper.lifecycle import get_lifecycle

    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        panel_edit,
        "supersede_panel_operation",
        lambda chat_id, msg_id: calls.append((chat_id, msg_id)) or True,
    )

    client = _CallbackClient()
    panels_module.register_callback_handlers(client, OWNER)
    sessions = get_lifecycle().sessions
    sessions.create(OWNER, PANEL_MSG, panel_type="menu")
    try:
        event = events.CallbackQuery.build(
            types.UpdateBotCallbackQuery(
                query_id=7,
                user_id=OWNER,
                peer=types.PeerUser(OWNER),
                msg_id=PANEL_MSG,
                chat_instance=5,
                data=b"panel:_nav:noop",
            )
        )
        _run(client.handlers["CallbackQuery"](event))
    finally:
        sessions.destroy(OWNER, PANEL_MSG)

    assert calls == [(OWNER, PANEL_MSG)]


def test_a_click_during_the_transfer_stops_the_stale_save_writing(monkeypatch):
    helper = RecordingHelper()
    target, reply = _target_and_reply()
    client = ClickDuringUpload([target, reply])
    _wire(client)
    monkeypatch.setattr(save_handler, "get_client", lambda: helper)

    _run(
        save_handler._save_reply_wait_handler(
            "save", PANEL_CHAT, REPLY_ID, PANEL_CHAT, PANEL_MSG, inline_id=_inline_id()
        )
    )

    assert client.clicked is True
    assert len(db_client._fallback["saved_items"]) == 1, "the save still completes"
    assert all("Saved Successfully" not in text for text in _helper_calls_texts(helper))


def test_arming_reply_mode_stops_the_panels_auto_close_timer():
    from backend.helper.lifecycle import get_lifecycle

    async def scenario():
        lifecycle = get_lifecycle()
        lifecycle.sessions.create(PANEL_CHAT, PANEL_MSG, panel_type="menu")
        lifecycle.timers.start(None, PANEL_CHAT, PANEL_MSG, owner_id=OWNER)
        await asyncio.sleep(0)  # let the timer task actually start
        assert lifecycle.timers.has_timer(PANEL_CHAT, PANEL_MSG)
        inline_engine.set_owner_id(OWNER)

        await save_handler._save_reply_action(_inline_click(), "", PANEL_CHAT)

        has_timer = lifecycle.timers.has_timer(PANEL_CHAT, PANEL_MSG)
        lifecycle.sessions.destroy(PANEL_CHAT, PANEL_MSG)
        lifecycle.timers.stop_all()
        return has_timer

    assert _run(scenario()) is False


def test_the_terminal_generation_is_released_for_the_next_save(monkeypatch):
    generation = panel_edit.begin_panel_operation(PANEL_CHAT, PANEL_MSG)
    assert panel_edit.panel_operation_is_current(PANEL_CHAT, PANEL_MSG, generation)

    panel_edit.finish_panel_operation(PANEL_CHAT, PANEL_MSG, generation)

    assert not panel_edit.panel_operation_is_current(PANEL_CHAT, PANEL_MSG, generation)


# ── 5. pending state lifecycle ──


def test_expired_pending_input_never_starts_a_save():
    client = _ListenerClient()
    register_input_listener(client, OWNER)
    called: list = []

    async def handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
        called.append(text)

    input_state.set_pending(
        OWNER, "save_reply", handler, PANEL_CHAT, "p",
        inline_chat_id=PANEL_CHAT, inline_msg_id=PANEL_MSG,
        inline_id=_inline_id(), timeout=None,
    )
    input_state._pending[OWNER]["created_at"] = time.monotonic() - 121

    assert input_state.get_pending(OWNER) is None
    _run(
        client.handler(
            SimpleNamespace(
                chat_id=PANEL_CHAT,
                raw_text="save",
                sender_id=OWNER,
                message=SimpleNamespace(id=REPLY_ID),
            )
        )
    )
    assert called == []
    assert db_client._fallback["saved_items"] == []


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)
