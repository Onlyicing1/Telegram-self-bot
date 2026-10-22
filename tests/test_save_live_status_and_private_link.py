"""
Live Save defects: the progress STATUS message and private ``tg://`` links.

Both regressions here were invisible to the previous fake-client tests, so the
fakes in this file copy the parts of pinned Telethon 1.43.0 that actually
failed in production:

  * ``edit_message(entity, message_id, text, *, buttons=…)`` — passing the text
    as ``message`` binds it to the message-id parameter and raises ``TypeError``
    (a fake that accepted that shape hid it);
  * peer resolution — ``get_input_entity`` / ``get_entity`` cannot resolve an
    arbitrary id (a ``StringSession`` persists no entity cache), which is why a
    parsed ``tg://openmessage?user_id=…`` link still failed to save.

The contracts asserted here are the owner's:

  * Reply Mode edits the panel it armed, in place: the "Waiting for your
    reply..." message shows target resolution, the transfer milestones and the
    terminal result, and the save NEVER adds a message to the chat;
  * Save by Link creates ONE dedicated status message with the self client
    right after the link arrives, before it is parsed or resolved, and every
    later update edits that same chat id + message id;
  * the exact `(chat_id, message_id)` captured when Reply Mode was armed is the
    one used for every edit, a missing panel coordinate runs the save with no
    UI instead of a second message, and a late callback can never overwrite the
    terminal state;
  * a private link resolves through ordinary authenticated APIs (cache →
    entity → the account's own dialog list → Telegram's peer-less lookup),
    always validated by peer AND message id, with a distinct honest failure
    for unknown peer, missing message, lookup error and id/peer mismatch.
"""
from __future__ import annotations

import inspect

import pytest
from telethon import TelegramClient

from backend.bot.handlers import save as save_handler
from backend.db import client as db_client
from backend.helper import inline_engine
from backend.services import save_service

from tests.test_save_album_view_once_progress import (
    CHAT,
    FakeClient,
    FakeMessage,
    PANEL_CHAT,
    PANEL_MSG,
    photo_message,
)

OWNER = 42
PEER = 5406707941
MSG_ID = 209974


@pytest.fixture(autouse=True)
def reset_state():
    db_client._fallback["saved_items"] = []
    db_client._fallback["bot_logs"] = []
    inline_engine.set_self_client(None)
    inline_engine.set_owner_id(0)
    yield
    inline_engine.set_self_client(None)
    inline_engine.set_owner_id(0)


def _rows() -> list[dict]:
    return db_client._fallback["saved_items"]


def _wire(client) -> None:
    inline_engine.set_self_client(client)
    inline_engine.set_owner_id(OWNER)


# ── the progress status message ──


class OrderingClient(FakeClient):
    """Records the ORDER of sends against reads, plus failed edits."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.order: list[str] = []
        self.applied_edits: list[dict] = []
        self.helper_used = False

    async def send_message(self, entity, message="", *, buttons=None, **kwargs):
        self.order.append("send_message")
        return await super().send_message(entity, message, buttons=buttons, **kwargs)

    async def get_messages(self, chat_id=None, ids=None, **kwargs):
        self.order.append("get_messages")
        return await super().get_messages(chat_id, ids, **kwargs)

    async def edit_message(self, entity, message=None, text=None, *, buttons=None, **kwargs):
        self.order.append("edit_message")
        await super().edit_message(entity, message, text, buttons=buttons, **kwargs)
        self.applied_edits.append({"chat_id": entity, "msg_id": message, "text": text})


@pytest.mark.asyncio
async def test_reply_mode_edits_the_existing_panel_before_target_lookup():
    target = photo_message(101, text="hello", download_size=200_000)
    reply = FakeMessage(102, chat_id=PANEL_CHAT, text="save", reply_to_msg_id=101)
    client = OrderingClient([target, reply])
    _wire(client)

    await save_handler._save_reply_wait_handler(
        "save", PANEL_CHAT, 102, PANEL_CHAT, PANEL_MSG
    )

    # The panel already on screen is the progress AND result surface: no new
    # message is created for Reply Mode, at any point of the save.
    assert client.send_message_calls == []

    # Edited BEFORE the reply/target lookup, so resolution delay is visible.
    assert client.order[0] == "edit_message"
    assert client.order.index("edit_message") < client.order.index("get_messages")

    coords = {(call["chat_id"], call["msg_id"]) for call in client.applied_edits}
    assert coords == {(PANEL_CHAT, PANEL_MSG)}
    texts = [call["text"] for call in client.applied_edits]
    assert "Resolving the target message" in texts[0]
    assert any("Downloading 100%" in text for text in texts)
    assert "Saved Successfully" in texts[-1]
    assert len(_rows()) == 1


@pytest.mark.asyncio
async def test_reply_mode_failure_result_lands_on_the_same_panel():
    reply = FakeMessage(302, chat_id=PANEL_CHAT, text="save", reply_to_msg_id=301)
    client = OrderingClient([reply])  # the replied-to message is gone
    _wire(client)

    await save_handler._save_reply_wait_handler(
        "save", PANEL_CHAT, 302, PANEL_CHAT, PANEL_MSG
    )

    assert client.send_message_calls == []
    last = client.applied_edits[-1]
    assert (last["chat_id"], last["msg_id"]) == (PANEL_CHAT, PANEL_MSG)
    assert "no longer exists" in last["text"]
    assert _rows() == []


@pytest.mark.asyncio
async def test_a_late_transfer_callback_cannot_overwrite_the_reply_panel_result(monkeypatch):
    created: dict = {}
    real_reporter = save_handler.SaveProgressReporter

    def _capture(edit, **kwargs):
        reporter = real_reporter(edit, **kwargs)
        created["reporter"] = reporter
        return reporter

    monkeypatch.setattr(save_handler, "SaveProgressReporter", _capture)

    target = photo_message(111, text="hello", download_size=200_000)
    reply = FakeMessage(112, chat_id=PANEL_CHAT, text="save", reply_to_msg_id=111)
    client = OrderingClient([target, reply])
    _wire(client)

    await save_handler._save_reply_wait_handler(
        "save", PANEL_CHAT, 112, PANEL_CHAT, PANEL_MSG
    )

    terminal = client.applied_edits[-1]["text"]
    assert "Saved Successfully" in terminal
    applied = len(client.applied_edits)

    # A callback that arrives after the result can no longer touch the panel.
    await created["reporter"].handle(
        save_service.SaveProgressEvent(
            phase="upload", completed_bytes=10, total_bytes=100,
            item_index=1, item_count=1, finished=True,
        )
    )
    assert len(client.applied_edits) == applied
    assert client.applied_edits[-1]["text"] == terminal


@pytest.mark.asyncio
async def test_reply_mode_without_panel_coordinates_never_creates_a_message():
    target = photo_message(401, text="hello", download_size=200_000)
    reply = FakeMessage(402, chat_id=PANEL_CHAT, text="save", reply_to_msg_id=401)
    client = OrderingClient([target, reply])
    _wire(client)

    await save_handler._save_reply_wait_handler("save", PANEL_CHAT, 402, 0, 0)

    # No panel to edit means no UI — never a replacement status message.
    assert client.send_message_calls == []
    assert client.applied_edits == []
    assert len(_rows()) == 1


@pytest.mark.asyncio
async def test_reply_mode_panel_is_edited_by_the_self_client_only(monkeypatch):
    """An inline panel is sent BY THE OWNER's account — never by the bot."""

    class ForbiddenHelper:
        async def edit_message(self, *args, **kwargs):  # pragma: no cover
            raise AssertionError("the Reply Mode panel is never the helper bot's")

        async def send_message(self, *args, **kwargs):  # pragma: no cover
            raise AssertionError("Reply Mode must not send any message")

    monkeypatch.setattr(save_handler, "get_client", lambda: ForbiddenHelper())

    target = photo_message(511, text="hello", download_size=200_000)
    reply = FakeMessage(512, chat_id=PANEL_CHAT, text="save", reply_to_msg_id=511)
    client = OrderingClient([target, reply])
    _wire(client)

    await save_handler._save_reply_wait_handler(
        "save", PANEL_CHAT, 512, PANEL_CHAT, PANEL_MSG
    )

    assert client.send_message_calls == []
    assert {(call["chat_id"], call["msg_id"]) for call in client.applied_edits} == {
        (PANEL_CHAT, PANEL_MSG)
    }
    assert "Saved Successfully" in client.applied_edits[-1]["text"]
    assert len(_rows()) == 1


@pytest.mark.asyncio
async def test_link_mode_creates_the_status_message_before_parsing_the_link():
    target = photo_message(MSG_ID, chat_id=PEER, text="hi", download_size=300_000)
    client = OrderingClient([target])
    _wire(client)

    await save_handler._save_link_input_handler(
        f"tg://openmessage?user_id={PEER}&message_id={MSG_ID}",
        PANEL_CHAT, 103, PANEL_CHAT, PANEL_MSG,
    )

    status = client.send_message_calls[0]
    assert status["chat_id"] == PANEL_CHAT
    assert "Save by Link" in status["text"]
    assert "Resolving the link" in status["text"]
    assert client.order[0] == "send_message"
    assert len(client.send_message_calls) == 1

    coords = {(call["chat_id"], call["msg_id"]) for call in client.applied_edits}
    assert coords == {(status["chat_id"], status["message_id"])}


@pytest.mark.asyncio
async def test_link_status_message_never_uses_the_helper_bot(monkeypatch):
    target = photo_message(209974, chat_id=PEER, text="hi", download_size=200_000)
    client = OrderingClient([target])
    _wire(client)

    class ForbiddenHelper:
        async def edit_message(self, *args, **kwargs):  # pragma: no cover
            raise AssertionError("the link status message is the self client's")

        async def send_message(self, *args, **kwargs):  # pragma: no cover
            raise AssertionError("the status message must be sent by the self client")

    monkeypatch.setattr(save_handler, "get_client", lambda: ForbiddenHelper())

    await save_handler._save_link_input_handler(
        f"tg://openmessage?user_id={PEER}&message_id={MSG_ID}",
        PANEL_CHAT, 113, PANEL_CHAT, PANEL_MSG,
    )

    assert len(_rows()) == 1
    assert len(client.send_message_calls) == 1


@pytest.mark.asyncio
async def test_link_status_creation_failure_still_saves_and_reports_on_the_panel():
    target = photo_message(MSG_ID, chat_id=PEER, text="hi", download_size=200_000)
    client = OrderingClient([target])
    _wire(client)

    async def refusing_send(entity, message="", **kwargs):
        raise RuntimeError("chat is read-only")

    client.send_message = refusing_send  # type: ignore[assignment]

    await save_handler._save_link_input_handler(
        f"tg://openmessage?user_id={PEER}&message_id={MSG_ID}",
        PANEL_CHAT, 123, PANEL_CHAT, PANEL_MSG,
    )

    assert len(_rows()) == 1, "the save itself must not depend on its UI"
    assert client.applied_edits, "the fallback result edit still reaches the panel"
    assert client.applied_edits[-1]["chat_id"] == PANEL_CHAT
    assert client.applied_edits[-1]["msg_id"] == PANEL_MSG
    assert "Saved Successfully" in client.applied_edits[-1]["text"]


@pytest.mark.asyncio
async def test_terminal_result_replaces_the_status_text_and_ignores_late_events():
    client = OrderingClient([])
    _wire(client)

    reporter = await save_handler._start_status(
        client, PANEL_CHAT, title="Deep Save", body="Resolving…"
    )
    assert reporter is not None

    await reporter.handle(
        save_service.SaveProgressEvent(
            phase="download", completed_bytes=50, total_bytes=100,
            item_index=1, item_count=1,
        )
    )
    await reporter.close("**DONE — saved**")
    terminal_index = len(client.applied_edits)

    # A late callback from the transfer can no longer touch the message.
    await reporter.handle(
        save_service.SaveProgressEvent(
            phase="upload", completed_bytes=90, total_bytes=100,
            item_index=1, item_count=1, finished=True,
        )
    )

    assert len(client.applied_edits) == terminal_index
    assert "DONE — saved" in client.applied_edits[-1]["text"]
    assert len(client.send_message_calls) == 1


@pytest.mark.asyncio
async def test_panel_coordinates_travel_from_arming_to_the_terminal_result(monkeypatch):
    """The whole Reply Mode contract, from the button to the result."""
    from backend.helper import input_state

    target = photo_message(601, text="hello", download_size=200_000)
    reply = FakeMessage(602, chat_id=PANEL_CHAT, text="save", reply_to_msg_id=601)
    client = OrderingClient([target, reply])
    _wire(client)
    input_state.clear_pending(OWNER)

    class _Event:
        chat_id = PANEL_CHAT
        message_id = PANEL_MSG
        inline_message_id = None

    _title, body, _buttons = await save_handler._save_reply_action(
        _Event(), "", PANEL_CHAT
    )
    assert "Waiting for your reply" in body

    pending = input_state.get_pending(OWNER)
    assert (pending["inline_chat_id"], pending["inline_msg_id"]) == (
        PANEL_CHAT, PANEL_MSG
    )

    # Exactly what the input listener does: pop FIRST, then call the handler.
    entry = input_state.clear_pending(OWNER)
    await entry["handler"](
        "ignored", PANEL_CHAT, 602, entry["inline_chat_id"], entry["inline_msg_id"]
    )

    assert client.send_message_calls == []
    assert {(call["chat_id"], call["msg_id"]) for call in client.applied_edits} == {
        (PANEL_CHAT, PANEL_MSG)
    }
    assert "Saved Successfully" in client.applied_edits[-1]["text"]
    assert len(_rows()) == 1


def test_an_inline_panel_resolves_its_message_through_the_session():
    """An inline callback carries only ``inline_message_id`` — find the pair."""
    from backend.helper.lifecycle import get_lifecycle

    sessions = get_lifecycle().sessions
    msg_id = 987654
    sessions.create(
        PANEL_CHAT, msg_id, panel_type="menu", inline_message_id="inline-under-test"
    )

    class _InlineEvent:
        chat_id = PANEL_CHAT
        message_id = None
        inline_message_id = "inline-under-test"

    try:
        assert save_handler._panel_coords(_InlineEvent(), PANEL_CHAT) == (
            PANEL_CHAT, msg_id
        )
    finally:
        sessions.destroy(PANEL_CHAT, msg_id)


def test_production_edit_and_send_shapes_match_real_telethon():
    """The exact bug: the text was passed as ``message`` (the message id)."""
    edit = inspect.signature(TelegramClient.edit_message)
    edit.bind(None, PANEL_CHAT, 601, "text", buttons=[])  # production shape
    with pytest.raises(TypeError):
        edit.bind(None, PANEL_CHAT, 601, message="text")

    send = inspect.signature(TelegramClient.send_message)
    send.bind(None, PANEL_CHAT, "text", buttons=[])  # production shape


# ── private tg:// resolution ──


def _dialog_entity(peer_id: int):
    """A REAL Telethon user entity, the way ``iter_dialogs`` supplies one."""
    from telethon.tl.types import User

    return User(id=peer_id, access_hash=1, first_name="Peer")


class Dialog:
    def __init__(self, peer_id: int):
        self.entity = _dialog_entity(peer_id)


class PeerResolutionClient(FakeClient):
    """Pinned-Telethon peer semantics: an unknown id CANNOT be resolved.

    ``get_input_entity``/``get_entity`` only know the session cache, a contact
    or a public username; ``get_messages`` needs that entity (or none at all,
    for Telegram's peer-less lookup). This is exactly why a parsed
    ``tg://openmessage?user_id=…`` link still failed live.
    """

    def __init__(
        self,
        *args,
        cached_peers=(),
        contact_peers=(),
        usernames=(),
        dialogs=(),
        by_chat=None,
        global_by_id=None,
        read_error: Exception | None = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.cached_peers = set(cached_peers)
        self.contact_peers = set(contact_peers)
        self.usernames = set(usernames)
        self._dialogs = list(dialogs)
        self.by_chat = by_chat or {}
        self.global_by_id = global_by_id or {}
        self.read_error = read_error
        self.reads: list[tuple] = []
        self.dialog_scans = 0

    async def get_input_entity(self, entity):
        if entity in self.cached_peers:
            return entity
        raise ValueError(f"Could not find the input entity for PeerUser({entity})")

    async def get_entity(self, entity):
        if isinstance(entity, str):
            if entity in self.usernames:
                return f"username:{entity}"
            raise ValueError(f"No user has username {entity}")
        if entity in self.contact_peers:
            return entity
        raise ValueError(f"Could not find the input entity for PeerUser({entity})")

    async def iter_dialogs(self, limit=None):
        for dialog in self._dialogs:
            self.dialog_scans += 1
            if limit is not None and self.dialog_scans > limit:
                return
            yield dialog

    async def get_messages(self, entity=None, ids=None, **kwargs):
        if self.read_error is not None:
            raise self.read_error
        self.reads.append((entity, ids))
        if entity is None:
            if isinstance(ids, (list, tuple)):
                return [self.global_by_id.get(message_id) for message_id in ids]
            return self.global_by_id.get(ids)
        key = entity if isinstance(entity, str) else getattr(entity, "id", entity)
        chat = self.by_chat.get(key)
        if chat is None:
            return None
        if isinstance(ids, (list, tuple)):
            return [chat.get(message_id) for message_id in ids]
        return chat.get(ids)


def _private_message(msg_id: int = MSG_ID, *, chat_id: int = PEER, **kwargs):
    return photo_message(msg_id, chat_id=chat_id, text="private", **kwargs)


@pytest.mark.asyncio
async def test_owner_uri_resolves_when_the_peer_is_unknown_to_the_session():
    """The owner's exact URI: not a contact, not cached — still readable."""
    target = _private_message()
    client = PeerResolutionClient([target], global_by_id={MSG_ID: target})

    result = await save_service.execute_link_save(
        client, OWNER,
        f"tg://openmessage?user_id={PEER}&message_id={MSG_ID}", "UTC",
    )

    assert getattr(result, "saved", False) is True, result
    assert len(_rows()) == 1
    assert _rows()[0]["origin_chat_id"] == PEER
    assert client.download_calls[0][0] == MSG_ID
    # The peer-less lookup is what made it readable, and it was validated.
    assert (None, MSG_ID) in client.reads


@pytest.mark.asyncio
async def test_peer_is_resolved_from_the_accounts_own_dialog_list():
    target = _private_message(chat_id=PEER)
    client = PeerResolutionClient(
        [target],
        dialogs=[Dialog(PEER)],
        by_chat={PEER: {MSG_ID: target}},
    )

    result = await save_service.execute_link_save(
        client, OWNER,
        f"tg://openmessage?user_id={PEER}&message_id={MSG_ID}", "UTC",
    )

    assert getattr(result, "saved", False) is True, result
    assert client.dialog_scans == 1
    # The read went through the FULL entity the dialog list supplied, not a
    # bare id (which is what the session cannot resolve).
    first_entity, _ids = client.reads[0]
    assert getattr(first_entity, "id", None) == PEER
    assert getattr(first_entity, "access_hash", None) == 1
    assert len(_rows()) == 1


@pytest.mark.asyncio
async def test_dialog_scan_is_bounded(monkeypatch):
    monkeypatch.setattr(save_service, "LINK_DIALOG_SCAN_LIMIT", 10)
    client = PeerResolutionClient(
        [], dialogs=[Dialog(1000 + index) for index in range(50)]
    )

    result = await save_service.execute_link_save(
        client, OWNER,
        f"tg://openmessage?user_id={PEER}&message_id={MSG_ID}", "UTC",
    )

    assert "Could not reach the chat" in result
    assert client.dialog_scans <= 11
    assert _rows() == []


@pytest.mark.asyncio
async def test_unresolvable_peer_is_reported_as_such_and_saves_nothing():
    client = PeerResolutionClient([])

    result = await save_service.execute_link_save(
        client, OWNER,
        f"tg://openmessage?user_id={PEER}&message_id={MSG_ID}", "UTC",
    )

    assert "Could not reach the chat in that link" in result
    assert "open that chat once in Telegram" in result
    assert client.download_calls == []
    assert _rows() == []


@pytest.mark.asyncio
async def test_missing_message_in_a_resolvable_chat_is_reported_distinctly():
    client = PeerResolutionClient([], cached_peers={PEER}, by_chat={PEER: {}})

    result = await save_service.execute_link_save(
        client, OWNER,
        f"tg://openmessage?user_id={PEER}&message_id={MSG_ID}", "UTC",
    )

    assert "Message not found at that link" in result
    assert client.download_calls == []
    assert _rows() == []


@pytest.mark.asyncio
async def test_lookup_failure_is_reported_as_a_lookup_failure():
    client = PeerResolutionClient(
        [], cached_peers={PEER}, read_error=RuntimeError("FLOOD_WAIT_30")
    )

    result = await save_service.execute_link_save(
        client, OWNER,
        f"tg://openmessage?user_id={PEER}&message_id={MSG_ID}", "UTC",
    )

    assert "Could not read the linked message" in result
    assert "FLOOD_WAIT_30" in result
    assert _rows() == []


@pytest.mark.asyncio
async def test_peer_less_lookup_message_from_another_chat_is_never_saved():
    other = _private_message(chat_id=777000)
    client = PeerResolutionClient([], global_by_id={MSG_ID: other})

    result = await save_service.execute_link_save(
        client, OWNER,
        f"tg://openmessage?user_id={PEER}&message_id={MSG_ID}", "UTC",
    )

    assert "did not resolve to the requested chat" in result
    assert client.download_calls == []
    assert _rows() == []


@pytest.mark.asyncio
async def test_peer_less_lookup_with_the_wrong_id_is_never_saved():
    wrong = _private_message(MSG_ID - 1, chat_id=PEER)
    client = PeerResolutionClient([], global_by_id={MSG_ID: wrong})

    result = await save_service.execute_link_save(
        client, OWNER,
        f"tg://openmessage?user_id={PEER}&message_id={MSG_ID}", "UTC",
    )

    assert "did not resolve to the requested message" in result
    assert client.download_calls == []
    assert _rows() == []


@pytest.mark.asyncio
async def test_album_through_an_unresolvable_peer_is_one_album_item():
    members = [
        _private_message(12, grouped_id=9400),
        _private_message(13, grouped_id=9400),
        _private_message(14, grouped_id=9400),
    ]
    for member in members:
        member.input_chat = "peer-handle"
    client = PeerResolutionClient(
        members,
        global_by_id={12: members[0]},
        by_chat={"peer-handle": {12: members[0], 13: members[1], 14: members[2]}},
    )

    result = await save_service.execute_link_save(
        client, OWNER, f"tg://openmessage?user_id={PEER}&message_id=12", "UTC",
    )

    assert getattr(result, "saved", False) is True, result
    assert getattr(result, "media_type", "") == "Album"
    # The album window was read through the message's OWN peer.
    assert ("peer-handle", list(range(3, 22))) in client.reads
    assert len(client.send_calls) == 1
    assert len(client.send_calls[0]["file"]) == 3
    assert len(_rows()) == 1


@pytest.mark.asyncio
async def test_public_and_private_http_links_keep_their_ordinary_path():
    channel_post = photo_message(42, chat_id=-100123, text="post")
    channel_client = PeerResolutionClient(
        [channel_post],
        usernames={"somechannel"},
        by_chat={"username:somechannel": {42: channel_post}},
    )
    public = await save_service.execute_link_save(
        channel_client, OWNER, "https://t.me/somechannel/42", "UTC",
    )
    assert getattr(public, "saved", False) is True, public
    assert channel_client.reads[0][0] == "username:somechannel"

    private_post = photo_message(43, chat_id=-1003080318802, text="post")
    private_client = PeerResolutionClient(
        [private_post],
        cached_peers={-1003080318802},
        by_chat={-1003080318802: {43: private_post}},
    )
    private = await save_service.execute_link_save(
        private_client, OWNER, "https://t.me/c/3080318802/43", "UTC",
    )
    assert getattr(private, "saved", False) is True, private
    assert private_client.dialog_scans == 0, "a cached peer needs no dialog scan"


@pytest.mark.asyncio
async def test_tg_scheme_is_not_refused_but_view_once_is_refused_by_media_policy():
    from backend.services.save_service import VIEW_ONCE_TTL_SECONDS
    from telethon.tl.types import MessageMediaDocument

    from tests.test_save_album_view_once_progress import FakeDoc

    view_once = FakeMessage(
        MSG_ID,
        chat_id=PEER,
        media=MessageMediaDocument(
            document=FakeDoc(MSG_ID, mime="image/jpeg", size=1000),
            ttl_seconds=VIEW_ONCE_TTL_SECONDS,
        ),
        text="one time only",
    )
    client = PeerResolutionClient([view_once], global_by_id={MSG_ID: view_once})

    result = await save_service.execute_link_save(
        client, OWNER,
        f"tg://openmessage?user_id={PEER}&message_id={MSG_ID}", "UTC",
    )

    assert "view-once" in result
    assert "tg://" not in result
    assert client.download_calls == []
    assert _rows() == []
