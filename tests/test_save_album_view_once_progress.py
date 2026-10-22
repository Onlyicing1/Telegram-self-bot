"""
Album-as-one-item, private-link and live-progress regressions for Deep Save.

The contracts under test are the OWNER's, not the previous implementation's:

  * an album is ONE saved item — one save code, ONE grouped Telegram album
    (a single album send, never N independent messages), ONE ``saved_items``
    row whose saved location is the album's first message, members in order,
    captions handled by the documented policy;
  * a failure anywhere before the album is sent leaves NOTHING in Saved
    Messages and no row, and a partial album Telegram creates is removed
    again — an album is never half-saved and never becomes several items;
  * the album's code retrieves the WHOLE album and deletes every member;
  * ``tg://openmessage?user_id=…&message_id=…`` parses and resolves as the
    URI it is (public/private HTTP links keep working), and a message that
    does not match the requested id/peer is an honest failure;
  * progress edits ONE dedicated status message created by the SELF client
    right after the input arrives (before target/link resolution) at the real
    milestone cadence, per phase and per album member, with a terminal result
    no late event can overwrite; the launching panel is not the progress
    surface and each save sends exactly one new message.

No network, no Telegram session, no Supabase: the fake client implements the
same call surface pinned Telethon 1.43.0 gives the save pipeline, including
``upload_file`` + one list ``send_file`` for a genuine grouped album.
"""
from __future__ import annotations

import inspect
import os
import tempfile

import pytest
from telethon.tl.types import (
    DocumentAttributeFilename,
    DocumentAttributeVideo,
    InputFile,
    InputMediaUploadedDocument,
    InputMediaUploadedPhoto,
    MessageMediaDocument,
    MessageMediaPhoto,
)

from backend.bot.handlers import save as save_handler
from backend.db import client as db_client
from backend.helper import inline_engine
from backend.helper import symbols as sym
from backend.helper.progress import render_progress_bar
from backend.helper.save_progress import (
    LARGE_FILE_MILESTONES,
    SMALL_FILE_LIMIT_BYTES,
    SMALL_FILE_MILESTONES,
    SaveProgressReporter,
    milestones_for_size,
    progress_percentage,
)
from backend.services import delete_service, retrieve_service, save_service
from backend.services.save_service import (
    EPHEMERAL_EXPIRING,
    EPHEMERAL_VIEW_ONCE,
    MEDIA_TYPE_ALBUM,
    VIEW_ONCE_TTL_SECONDS,
    SaveProgressEvent,
    parse_link_target,
    parse_telegram_link,
)

OWNER = 42
CHAT = -1001234567890
PANEL_CHAT = 777
PANEL_MSG = 5001
SAVED_CHAT = 999
MIB = 1024 * 1024


# ── source fakes ──


class FakeSender:
    first_name = "Test"
    last_name = "User"


class FakePhoto:
    def __init__(self, photo_id: int, size: int):
        self.id = photo_id
        self.sizes = [type("S", (), {"size": size})()]


class FakeDoc:
    def __init__(self, doc_id: int, *, mime: str = "image/jpeg", size: int = 1000, name=None):
        self.id = doc_id
        self.mime_type = mime
        self.size = size
        self.attributes = [DocumentAttributeFilename(name or f"file_{doc_id}.jpg")]


class FakeMessage:
    def __init__(
        self,
        msg_id: int,
        *,
        chat_id: int = CHAT,
        media=None,
        text: str = "",
        grouped_id: int | None = None,
        sender_id: int = 777,
        reply_to_msg_id: int | None = None,
        download_size: int | None = None,
        unknown_size: bool = False,
    ):
        self.id = msg_id
        self.chat_id = chat_id
        self.media = media
        self.text = text
        self.grouped_id = grouped_id
        self.sender_id = sender_id
        self.reply_to_msg_id = reply_to_msg_id
        self.download_size = download_size
        self.unknown_size = unknown_size

    async def get_sender(self):
        return FakeSender()


class StoredMessage:
    """One message that exists in the fake Saved Messages / destination chat."""

    def __init__(
        self,
        msg_id: int,
        chat_id=SAVED_CHAT,
        *,
        grouped_id: int | None = None,
        text: str = "",
    ):
        self.id = msg_id
        self.chat_id = chat_id
        self.grouped_id = grouped_id
        self.text = text
        self.media = None
        self.sender_id = 0
        self.reply_to_msg_id = None


def photo_message(
    msg_id: int,
    *,
    size: int = 1000,
    grouped_id=None,
    chat_id=CHAT,
    text="",
    download_size: int | None = None,
):
    return FakeMessage(
        msg_id,
        chat_id=chat_id,
        media=MessageMediaPhoto(photo=FakePhoto(msg_id, size), ttl_seconds=None),
        text=text,
        grouped_id=grouped_id,
        download_size=download_size,
    )


def document_message(
    msg_id: int,
    *,
    size: int | None = 1000,
    ttl=None,
    grouped_id=None,
    text="",
    mime="application/pdf",
    name=None,
    download_size: int | None = None,
    unknown_size: bool = False,
):
    return FakeMessage(
        msg_id,
        media=MessageMediaDocument(
            document=FakeDoc(msg_id, mime=mime, size=size, name=name),
            ttl_seconds=ttl,
        ),
        text=text,
        grouped_id=grouped_id,
        download_size=download_size,
        unknown_size=unknown_size,
    )


def view_once_message(msg_id: int, *, grouped_id=None):
    return FakeMessage(
        msg_id,
        media=MessageMediaDocument(
            document=FakeDoc(msg_id, mime="image/jpeg", size=1000),
            ttl_seconds=VIEW_ONCE_TTL_SECONDS,
        ),
        text="one time only",
        grouped_id=grouped_id,
    )


def _declared_size(message) -> int | None:
    media = message.media
    if isinstance(media, MessageMediaDocument):
        return media.document.size
    if isinstance(media, MessageMediaPhoto):
        return media.photo.sizes[-1].size
    return None


# ── client fake ──


class FakeClient:
    """The exact Telethon surface the save/retrieve/delete paths call."""

    def __init__(
        self,
        messages=(),
        *,
        window_extras=(),
        fetch_error: Exception | None = None,
        emit_callbacks: bool = True,
        download_steps=(0.25, 0.5, 0.75, 1.0),
        upload_steps=(0.25, 0.5, 0.75, 1.0),
        fail_download_ids=(),
        fail_upload_items=(),
        send_error: Exception | None = None,
        edit_error: Exception | None = None,
        album_reply_count: int | None = None,
        payload_default: int = 4096,
    ):
        self._by_id: dict = {}
        for message in messages:
            self._by_id[message.id] = message
        self._window_extras = list(window_extras)
        self.fetch_error = fetch_error
        self.emit_callbacks = emit_callbacks
        self.download_steps = tuple(download_steps)
        self.upload_steps = tuple(upload_steps)
        self.fail_download_ids = set(fail_download_ids)
        self.fail_upload_items = set(fail_upload_items)
        self.send_error = send_error
        self.edit_error = edit_error
        self.album_reply_count = album_reply_count
        self.payload_default = payload_default

        self.download_calls: list[tuple] = []
        self.upload_calls: list[tuple] = []
        self.send_calls: list[dict] = []
        self.send_message_calls: list[tuple] = []
        self.forward_calls: list[dict] = []
        self.forwarded_ids: list[list[int]] = []
        self.delete_calls: list[tuple] = []
        self.edit_calls: list[dict] = []
        self.calls: list[str] = []

        self._next_id = 600
        self._upload_count = 0
        self._sent_serial = 0

    # reads
    async def get_messages(self, chat_id=None, ids=None, **kwargs):
        if self.fetch_error is not None:
            raise self.fetch_error
        if isinstance(ids, (list, tuple)):
            found = [self._by_id.get(message_id) for message_id in ids]
            if chat_id == SAVED_CHAT:
                return found
            return found + list(self._window_extras)
        return self._by_id.get(ids)

    async def get_input_entity(self, entity):
        return entity

    async def get_entity(self, entity):
        return entity

    # writes
    async def download_media(self, message, file=None, progress_callback=None, **kwargs):
        self.calls.append("download_media")
        self.download_calls.append((message.id, progress_callback))
        if message.id in self.fail_download_ids:
            raise RuntimeError("network down")
        size = message.download_size or _declared_size(message) or self.payload_default
        payload = bytes([65 + (message.id % 26)]) * size
        if file is not None:
            os.makedirs(os.path.dirname(file), exist_ok=True)
            with open(file, "wb") as handle:
                handle.write(payload)
        if progress_callback is not None and self.emit_callbacks:
            total = None if message.unknown_size else size
            for step in self.download_steps:
                result = progress_callback(int(size * step), total)
                if inspect.isawaitable(result):
                    await result
        return file

    async def upload_file(self, file, progress_callback=None, **kwargs):
        self.calls.append("upload_file")
        self.upload_calls.append((file, progress_callback))
        self._upload_count += 1
        if self._upload_count in self.fail_upload_items:
            raise RuntimeError("upload rejected")
        size = os.path.getsize(file)
        if progress_callback is not None and self.emit_callbacks:
            for step in self.upload_steps:
                result = progress_callback(int(size * step), size)
                if inspect.isawaitable(result):
                    await result
        return InputFile(
            id=1000 + self._upload_count,
            parts=1,
            name=os.path.basename(file),
            md5_checksum="",
        )

    async def send_file(self, entity, file, caption=None, progress_callback=None, **kwargs):
        self.calls.append("send_file")
        self.send_calls.append(
            {
                "entity": entity,
                "file": file,
                "caption": caption,
                "progress_callback": progress_callback,
                "kwargs": kwargs,
            }
        )
        if self.send_error is not None:
            raise self.send_error

        if isinstance(file, (list, tuple)):
            self._sent_serial += 1
            grouped = 900000 + self._sent_serial
            created = []
            for index, _item in enumerate(file):
                self._next_id += 1
                message = StoredMessage(
                    self._next_id,
                    SAVED_CHAT,
                    grouped_id=grouped,
                    text=caption if index == 0 else "",
                )
                self._by_id[message.id] = message
                created.append(message)
            if self.album_reply_count is not None:
                return created[: self.album_reply_count]
            return created

        # Single file: pinned Telethon uploads it inside send_file and reports
        # the real per-part byte counts through this callback.
        size = os.path.getsize(file) if isinstance(file, (str, os.PathLike)) else self.payload_default
        if progress_callback is not None and self.emit_callbacks:
            for step in self.upload_steps:
                result = progress_callback(int(size * step), size)
                if inspect.isawaitable(result):
                    await result

        self._next_id += 1
        message = StoredMessage(self._next_id, SAVED_CHAT)
        self._by_id[message.id] = message
        return message

    # Pinned Telethon signatures, not ours: ``send_message(entity, message,
    # *, buttons=…)`` and ``edit_message(entity, message_id, text, *,
    # buttons=…)``. A fake that accepts the text in the ``message`` position
    # would hide the production TypeError that broke every live edit.
    async def send_message(self, entity, message="", *, buttons=None, **kwargs):
        self.calls.append("send_message")
        self._next_id += 1
        stored = StoredMessage(self._next_id, entity, text=message)
        self.send_message_calls.append(
            {
                "chat_id": getattr(stored, "chat_id", entity),
                "text": message,
                "buttons": buttons,
                "message_id": stored.id,
            }
        )
        self._by_id[stored.id] = stored
        return stored

    async def forward_messages(self, entity=None, messages=None, from_peer=None, **kwargs):
        self.calls.append("forward_messages")
        self.forward_calls.append(
            {"entity": entity, "messages": messages, "from_peer": from_peer}
        )
        ids = messages if isinstance(messages, (list, tuple)) else [messages]
        forwarded = []
        for _message_id in ids:
            self._next_id += 1
            forwarded.append(StoredMessage(self._next_id, PANEL_CHAT, text=""))
        self.forwarded_ids.append([message.id for message in forwarded])
        return forwarded if isinstance(messages, (list, tuple)) else forwarded[0]

    async def delete_messages(self, chat_id, message_ids):
        self.calls.append("delete_messages")
        self.delete_calls.append((chat_id, list(message_ids)))

    async def edit_message(self, entity, message=None, text=None, *, buttons=None, **kwargs):
        self.calls.append("edit_message")
        self.edit_calls.append(
            {"chat_id": entity, "msg_id": message, "text": text, "buttons": buttons}
        )
        if self.edit_error is not None:
            raise self.edit_error

    async def forward_messages_never(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("Deep Save must never forward")


@pytest.fixture(autouse=True)
def reset_fallback():
    db_client._fallback["saved_items"] = []
    db_client._fallback["bot_logs"] = []
    inline_engine.set_self_client(None)
    inline_engine.set_owner_id(0)
    yield
    inline_engine.set_self_client(None)
    inline_engine.set_owner_id(0)


def _rows() -> list[dict]:
    return db_client._fallback["saved_items"]


def _leftovers() -> list[str]:
    return [name for name in os.listdir(tempfile.gettempdir()) if name.startswith("lifeos_dl_")]


def _assert_no_leftover_temp_dirs():
    assert _leftovers() == [], f"leftover temp dirs: {_leftovers()}"


def _album_members(*, size=1000, grouped_id=9001, count=3, texts=("first", "second", "third")):
    return [
        photo_message(index + 11, size=size, grouped_id=grouped_id, text=texts[index])
        for index in range(count)
    ]


def _save_calls(client: FakeClient) -> list[dict]:
    return [call for call in client.send_calls]


# ── album as ONE saved item ──


@pytest.mark.asyncio
async def test_album_is_sent_as_one_album_with_one_code_and_one_row():
    first, second, third = _album_members()
    client = FakeClient([first, second, third])

    result = await save_service.execute_save(client, OWNER, second, "UTC")

    assert getattr(result, "saved", False) is True
    assert getattr(result, "save_code", "")
    assert MEDIA_TYPE_ALBUM in result

    # ONE upload request, ONE album message, in album order, never N messages.
    assert len(client.send_calls) == 1
    album_call = client.send_calls[0]
    assert isinstance(album_call["file"], list)
    assert len(album_call["file"]) == 3
    assert all(isinstance(item, InputMediaUploadedPhoto) for item in album_call["file"])
    assert len(client.upload_calls) == 3

    rows = _rows()
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["save_code"] == getattr(result, "save_code")
    assert row["media_type"] == MEDIA_TYPE_ALBUM
    assert row["save_type"] == "deep"
    assert row["origin_msg_id"] == first.id
    assert row["saved_msg_id"] == 601  # the album's FIRST destination message
    assert row["saved_chat_id"] == SAVED_CHAT
    assert row["file_size"] == 3000

    # The metadata block is written ONCE, on the album's first message.
    assert album_call["caption"].count("DEEP") == 1
    hashtag_lines = [
        line for line in album_call["caption"].splitlines() if line.startswith("#saved")
    ]
    assert len(hashtag_lines) == 1
    assert "Album · 3 items" in album_call["caption"]

    _assert_no_leftover_temp_dirs()


@pytest.mark.asyncio
async def test_album_members_keep_order_and_caption_policy():
    texts = ("first caption", "first caption", "third caption")
    first, second, third = _album_members(texts=texts)
    client = FakeClient([first, second, third])

    result = await save_service.execute_save(client, OWNER, third, "UTC")

    assert getattr(result, "saved", False) is True
    # Download order is album order (by message id), never click order.
    assert [message_id for message_id, _cb in client.download_calls] == [11, 12, 13]

    caption = client.send_calls[0]["caption"]
    # Source captions follow the documented policy: member order, each distinct
    # caption once, a repeated caption collapsed, separated by a blank line.
    assert caption.endswith("first caption\n\nthird caption")
    assert caption.count("first caption") == 1


@pytest.mark.asyncio
async def test_album_ignores_neighbouring_unrelated_messages():
    first, second, third = _album_members()
    unrelated = photo_message(14, grouped_id=7777, text="other album")
    other_chat = photo_message(15, chat_id=CHAT + 1, grouped_id=9001, text="other chat")
    client = FakeClient([first, second, third], window_extras=[unrelated, other_chat])

    result = await save_service.execute_save(client, OWNER, first, "UTC")

    assert getattr(result, "saved", False) is True
    assert [message_id for message_id, _cb in client.download_calls] == [11, 12, 13]
    assert len(client.send_calls[0]["file"]) == 3


@pytest.mark.asyncio
async def test_non_album_save_keeps_one_message_one_row():
    single = photo_message(21, text="just one")
    client = FakeClient([single])

    result = await save_service.execute_save(client, OWNER, single, "UTC")

    assert getattr(result, "saved", False) is True
    assert len(client.send_calls) == 1
    assert not isinstance(client.send_calls[0]["file"], list)
    assert len(_rows()) == 1
    assert _rows()[0]["media_type"] == "Photo"
    assert _rows()[0]["saved_msg_id"] == 601
    assert not save_service.is_album_row(_rows()[0])


@pytest.mark.asyncio
async def test_document_album_preserves_media_type_and_filename():
    first = document_message(31, grouped_id=9100, mime="video/mp4", size=2000, name="clip.mp4", text="clip")
    second = document_message(32, grouped_id=9100, mime="video/mp4", size=2000, name="clip2.mp4", text="clip2")
    client = FakeClient([first, second])

    result = await save_service.execute_save(client, OWNER, first, "UTC")

    assert getattr(result, "saved", False) is True
    items = client.send_calls[0]["file"]
    assert all(isinstance(item, InputMediaUploadedDocument) for item in items)
    names = []
    for item in items:
        names.extend(
            attr.file_name
            for attr in item.attributes
            if isinstance(attr, DocumentAttributeFilename)
        )
    assert sorted(names) == ["clip.mp4", "clip2.mp4"]
    assert items[0].mime_type == "video/mp4"
    assert len(_rows()) == 1


# ── album failure / cleanup semantics ──


@pytest.mark.asyncio
async def test_album_download_failure_saves_nothing_and_cleans_temp():
    first, second, third = _album_members()
    client = FakeClient([first, second, third], fail_download_ids={12})

    result = await save_service.execute_save(client, OWNER, first, "UTC")

    assert getattr(result, "saved", False) is False
    assert "Album not saved" in result
    assert "2 of 3" in result
    assert client.send_calls == []
    assert _rows() == []
    _assert_no_leftover_temp_dirs()


@pytest.mark.asyncio
async def test_album_upload_failure_saves_nothing():
    first, second, third = _album_members()
    client = FakeClient([first, second, third], fail_upload_items={2})

    result = await save_service.execute_save(client, OWNER, first, "UTC")

    assert getattr(result, "saved", False) is False
    assert client.send_calls == []
    assert _rows() == []
    _assert_no_leftover_temp_dirs()


@pytest.mark.asyncio
async def test_album_send_failure_saves_nothing():
    first, second, third = _album_members()
    client = FakeClient([first, second, third], send_error=RuntimeError("MEDIA_INVALID"))

    result = await save_service.execute_save(client, OWNER, first, "UTC")

    assert getattr(result, "saved", False) is False
    assert "Album not saved" in result
    assert _rows() == []
    _assert_no_leftover_temp_dirs()


@pytest.mark.asyncio
async def test_partial_album_creation_is_removed_and_never_reported_saved():
    first, second, third = _album_members()
    client = FakeClient([first, second, third], album_reply_count=2)

    result = await save_service.execute_save(client, OWNER, first, "UTC")

    assert getattr(result, "saved", False) is False
    assert "2 of 3" in result
    assert _rows() == []
    assert client.delete_calls, "the partial album must be removed again"
    deleted_chat, deleted_ids = client.delete_calls[-1]
    assert deleted_chat == "me"
    assert len(deleted_ids) == 2
    _assert_no_leftover_temp_dirs()


@pytest.mark.asyncio
async def test_album_with_ephemeral_member_is_refused_before_any_transfer():
    first = photo_message(41, grouped_id=9200, text="ok")
    secret = view_once_message(42, grouped_id=9200)
    third = photo_message(43, grouped_id=9200, text="ok")
    client = FakeClient([first, secret, third])

    result = await save_service.execute_save(client, OWNER, third, "UTC")

    assert getattr(result, "saved", False) is False
    assert "view-once" in result
    assert "file 2 of 3" in result
    assert client.download_calls == []
    assert client.upload_calls == []
    assert client.send_calls == []
    assert _rows() == []
    assert db_client._fallback["saved_items"] == []


@pytest.mark.asyncio
async def test_album_over_limit_member_is_refused_before_transfer():
    big = document_message(51, grouped_id=9300, size=200 * MIB, mime="video/mp4", name="big.mp4")
    small = photo_message(52, grouped_id=9300, text="small")
    client = FakeClient([big, small])

    result = await save_service.execute_save(client, OWNER, small, "UTC")

    assert getattr(result, "saved", False) is False
    assert "limit" in result
    assert client.download_calls == []
    assert _rows() == []


# ── view-once / expiring (single item, unchanged contract) ──


@pytest.mark.asyncio
async def test_view_once_single_item_is_refused_before_transfer():
    secret = view_once_message(61)
    client = FakeClient([secret])

    result = await save_service.execute_save(client, OWNER, secret, "UTC")

    assert getattr(result, "saved", False) is False
    assert "view-once" in result
    assert client.calls == []
    assert _rows() == []


@pytest.mark.asyncio
async def test_expiring_single_item_is_refused_with_its_ttl():
    expiring = FakeMessage(
        62,
        media=MessageMediaDocument(
            document=FakeDoc(62, mime="image/jpeg", size=100), ttl_seconds=30
        ),
        text="timer",
    )
    client = FakeClient([expiring])

    result = await save_service.execute_save(client, OWNER, expiring, "UTC")

    assert getattr(result, "saved", False) is False
    assert "30s" in result
    assert client.calls == []
    assert save_service.ephemeral_media_kind(expiring.media) == EPHEMERAL_EXPIRING


# ── retrieve / delete of an album code ──


@pytest.mark.asyncio
async def test_retrieving_an_album_code_forwards_every_member():
    first, second, third = _album_members()
    client = FakeClient([first, second, third])
    result = await save_service.execute_save(client, OWNER, first, "UTC")
    code = getattr(result, "save_code")

    forwarded = await retrieve_service.do_retrieve(client, OWNER, code, PANEL_CHAT)

    assert "Retrieved" in forwarded
    assert len(client.forward_calls) == 1
    assert client.forward_calls[0]["messages"] == [601, 602, 603]
    # The metadata block is injected on the album's FIRST forwarded message only.
    caption_edits = [call for call in client.edit_calls if call["text"]]
    assert len(caption_edits) == 1
    assert "LifeOS" in caption_edits[0]["text"]
    assert caption_edits[0]["msg_id"] == client.forwarded_ids[0][0]


@pytest.mark.asyncio
async def test_retrieving_a_single_item_keeps_the_one_message_contract():
    single = photo_message(71, text="single")
    client = FakeClient([single])
    result = await save_service.execute_save(client, OWNER, single, "UTC")
    code = getattr(result, "save_code")

    await retrieve_service.do_retrieve(client, OWNER, code, PANEL_CHAT)

    assert client.forward_calls[0]["messages"] == 601


@pytest.mark.asyncio
async def test_album_retrieval_is_honest_when_members_cannot_be_resolved():
    first, second, third = _album_members()
    client = FakeClient([first, second, third])
    result = await save_service.execute_save(client, OWNER, first, "UTC")
    code = getattr(result, "save_code")

    # Telegram kept only the anchor of the saved album.
    for member_id in (602, 603):
        client._by_id.pop(member_id, None)

    outcome = await retrieve_service.do_retrieve(client, OWNER, code, PANEL_CHAT)

    assert sym.STATE_FAILED in outcome
    assert "Nothing was forwarded" in outcome
    assert client.forward_calls == []


@pytest.mark.asyncio
async def test_deleting_an_album_code_deletes_every_member_and_the_row():
    first, second, third = _album_members()
    client = FakeClient([first, second, third])
    result = await save_service.execute_save(client, OWNER, first, "UTC")
    code = getattr(result, "save_code")

    outcome = await delete_service.do_del_code(client, OWNER, code)

    assert "Deleted" in outcome
    album_deletes = [call for call in client.delete_calls if call[0] == SAVED_CHAT]
    assert album_deletes, client.delete_calls
    assert sorted(album_deletes[-1][1]) == [601, 602, 603]
    assert _rows() == []


@pytest.mark.asyncio
async def test_deleting_an_album_with_unresolvable_members_deletes_nothing():
    first, second, third = _album_members()
    client = FakeClient([first, second, third])
    result = await save_service.execute_save(client, OWNER, first, "UTC")
    code = getattr(result, "save_code")
    for member_id in (602, 603):
        client._by_id.pop(member_id, None)

    outcome = await delete_service.do_del_code(client, OWNER, code)

    assert sym.STATE_FAILED in outcome
    assert [call for call in client.delete_calls if call[0] == SAVED_CHAT] == []
    assert len(_rows()) == 1, "the row must survive a failed album deletion"


@pytest.mark.asyncio
async def test_album_file_name_change_is_refused_without_touching_telegram():
    first, second, third = _album_members()
    client = FakeClient([first, second, third])
    result = await save_service.execute_save(client, OWNER, first, "UTC")
    code = getattr(result, "save_code")
    client.calls.clear()

    outcome = await retrieve_service.do_change_file_name(client, OWNER, code, "renamed.jpg")

    assert sym.STATE_FAILED in outcome
    assert "album" in outcome.lower()
    assert client.calls == []


@pytest.mark.asyncio
async def test_resolve_saved_album_messages_uses_grouping_not_adjacency():
    anchor = StoredMessage(701, SAVED_CHAT, grouped_id=777)
    sibling = StoredMessage(702, SAVED_CHAT, grouped_id=777)
    neighbour = StoredMessage(703, SAVED_CHAT, grouped_id=888)
    client = FakeClient([anchor, neighbour, sibling])

    members, error = await save_service.resolve_saved_album_messages(client, SAVED_CHAT, 701)

    assert error == ""
    assert [member.id for member in members] == [701, 702]


@pytest.mark.asyncio
async def test_resolve_saved_album_messages_single_anchor_is_itself():
    single = StoredMessage(801, SAVED_CHAT)
    client = FakeClient([single])

    members, error = await save_service.resolve_saved_album_messages(client, SAVED_CHAT, 801)

    assert error == ""
    assert [member.id for member in members] == [801]


@pytest.mark.asyncio
async def test_resolve_saved_album_messages_missing_anchor_is_an_error():
    client = FakeClient([])

    members, error = await save_service.resolve_saved_album_messages(client, SAVED_CHAT, 900)

    assert members == []
    assert "no longer exists" in error


# ── private links: tg://openmessage ──


def test_exact_owner_uri_parses():
    target = parse_link_target("tg://openmessage?user_id=5406707941&message_id=209974")

    assert target.error == ""
    assert target.username is None
    assert target.chat_id == 5406707941
    assert target.message_id == 209974
    assert parse_telegram_link("tg://openmessage?user_id=5406707941&message_id=209974") == (
        None, 5406707941, 209974,
    )


def test_tg_openmessage_channel_and_group_forms():
    channel = parse_link_target("tg://openmessage?channel_id=123&post=42")
    assert (channel.chat_id, channel.message_id) == (-100123, 42)

    group = parse_link_target("tg://openmessage?chat_id=456&message_id=7")
    assert (group.chat_id, group.message_id) == (-456, 7)


@pytest.mark.parametrize(
    "link",
    [
        "tg://openmessage?user_id=5406707941",
        "tg://openmessage?message_id=5",
        "tg://openmessage?user_id=abc&message_id=5",
        "tg://openmessage?user_id=1&chat_id=2&message_id=5",
        "tg://openmessage?user_id=0&message_id=5",
        "tg://openmessage",
        "tg://resolve?domain=someone",
    ],
)
def test_invalid_tg_links_are_refused_with_a_reason(link):
    target = parse_link_target(link)

    assert target.error
    assert parse_telegram_link(link) == (None, 0, 0)


def test_http_links_keep_working():
    assert parse_telegram_link("https://t.me/somechannel/42") == ("somechannel", 0, 42)
    assert parse_telegram_link("https://t.me/c/3080318802/42") == (None, -1003080318802, 42)
    assert parse_telegram_link("https://telegram.me/somechannel/7") == ("somechannel", 0, 7)
    assert parse_telegram_link("https://example.com/nothing") == (None, 0, 0)


@pytest.mark.asyncio
async def test_link_save_resolves_the_exact_owner_uri_message():
    target = photo_message(209974, chat_id=5406707941, text="private message")
    client = FakeClient([target])

    result = await save_service.execute_link_save(
        client, OWNER,
        "tg://openmessage?user_id=5406707941&message_id=209974",
        "UTC",
    )

    assert getattr(result, "saved", False) is True
    assert client.download_calls[0][0] == 209974
    assert len(_rows()) == 1
    assert _rows()[0]["origin_chat_id"] == 5406707941


@pytest.mark.asyncio
async def test_link_save_album_becomes_one_item():
    first, second, third = _album_members(grouped_id=9400)
    for member in (first, second, third):
        member.chat_id = 5406707941
    client = FakeClient([first, second, third])

    result = await save_service.execute_link_save(
        client, OWNER,
        "tg://openmessage?user_id=5406707941&message_id=12",
        "UTC",
    )

    assert getattr(result, "saved", False) is True
    assert len(client.send_calls) == 1
    assert isinstance(client.send_calls[0]["file"], list)
    assert len(client.send_calls[0]["file"]) == 3
    assert len(_rows()) == 1


@pytest.mark.asyncio
async def test_link_save_reports_a_missing_message():
    client = FakeClient([])

    result = await save_service.execute_link_save(
        client, OWNER, "tg://openmessage?user_id=1&message_id=2", "UTC"
    )

    assert sym.STATE_FAILED in result
    assert "Message not found" in result
    assert _rows() == []


@pytest.mark.asyncio
async def test_link_save_reports_lookup_exceptions():
    client = FakeClient([], fetch_error=RuntimeError("boom"))

    result = await save_service.execute_link_save(
        client, OWNER, "tg://openmessage?user_id=1&message_id=2", "UTC"
    )

    assert sym.STATE_FAILED in result
    assert "boom" in result
    assert _rows() == []


@pytest.mark.asyncio
async def test_link_save_refuses_a_message_that_is_not_the_requested_one():
    wrong = photo_message(99, chat_id=5406707941)
    client = FakeClient([])
    # The peer answers the requested id with a DIFFERENT message.
    client._by_id[209974] = wrong

    result = await save_service.execute_link_save(
        client, OWNER, "tg://openmessage?user_id=5406707941&message_id=209974", "UTC"
    )

    assert sym.STATE_FAILED in result
    assert "did not resolve" in result
    assert _rows() == []


@pytest.mark.asyncio
async def test_link_save_refuses_a_chat_mismatch():
    other_chat = photo_message(209974, chat_id=5406707942)
    client = FakeClient([other_chat])

    result = await save_service.execute_link_save(
        client, OWNER, "tg://openmessage?user_id=5406707941&message_id=209974", "UTC"
    )

    assert sym.STATE_FAILED in result
    assert "did not resolve" in result
    assert _rows() == []


@pytest.mark.asyncio
async def test_link_save_reports_a_malformed_uri_without_touching_telegram():
    client = FakeClient([])

    result = await save_service.execute_link_save(
        client, OWNER, "tg://openmessage?user_id=abc", "UTC"
    )

    assert sym.STATE_FAILED in result
    assert "Could not parse link" in result
    assert client.calls == []


@pytest.mark.asyncio
async def test_view_once_through_a_private_link_is_refused_by_the_media_policy():
    secret = view_once_message(209974)
    secret.chat_id = 5406707941
    client = FakeClient([secret])

    result = await save_service.execute_link_save(
        client, OWNER, "tg://openmessage?user_id=5406707941&message_id=209974", "UTC"
    )

    assert getattr(result, "saved", False) is False
    assert "view-once" in result
    assert client.download_calls == []
    assert _rows() == []


@pytest.mark.asyncio
async def test_save_by_link_tool_preserves_the_tg_scheme(monkeypatch):
    from backend.ai.tools import save as save_tool_module
    from backend.ai.tools.context import ToolContext

    captured: list[str] = []

    async def fake_execute_link_save(client, owner_id, link, tz_str, *, metadata=None, progress=None):
        captured.append(link)
        return "✓ linked"

    monkeypatch.setattr(save_service, "execute_link_save", fake_execute_link_save)
    context = ToolContext(
        telegram=None, owner_id=OWNER, tz_str="UTC", client=FakeClient([])
    )

    tool = save_tool_module.SaveByLinkTool(context)
    result = await tool.execute(
        context, {"link": "tg://openmessage?user_id=5406707941&message_id=209974"}
    )

    assert result.success is True
    assert captured == ["tg://openmessage?user_id=5406707941&message_id=209974"]


# ── progress: cadence + reporter ──


def test_milestone_cadence_follows_the_real_size():
    assert milestones_for_size(1000) == SMALL_FILE_MILESTONES == (50, 100)
    assert milestones_for_size(SMALL_FILE_LIMIT_BYTES) == SMALL_FILE_MILESTONES
    assert milestones_for_size(SMALL_FILE_LIMIT_BYTES + 1) == LARGE_FILE_MILESTONES == (20, 40, 60, 80, 100)
    assert milestones_for_size(None) == ()
    assert milestones_for_size(0) == ()


def test_percentage_comes_from_byte_counts_only():
    assert progress_percentage(500, 1000) == 50
    assert progress_percentage(0, 1000) == 0
    assert progress_percentage(10, None) is None
    assert progress_percentage(10, 0) is None
    assert progress_percentage(2000, 1000) == 100


def _events(*specs):
    return [
        SaveProgressEvent(
            phase=phase,
            completed_bytes=completed,
            total_bytes=total,
            item_index=index,
            item_count=count,
            finished=finished,
        )
        for phase, completed, total, index, count, finished in specs
    ]


@pytest.mark.asyncio
async def test_reporter_emits_phase_start_and_small_milestones_once():
    texts: list[str] = []

    async def edit(text):
        texts.append(text)
        return True

    reporter = SaveProgressReporter(edit)
    for event in _events(
        ("download", 0, 1000, 1, 1, False),
        ("download", 250, 1000, 1, 1, False),
        ("download", 550, 1000, 1, 1, False),
        ("download", 1000, 1000, 1, 1, True),
    ):
        await reporter.handle(event)

    assert "Downloading 0%" in texts[0]
    assert any("Downloading 50%" in text for text in texts)
    assert texts[-1].count("Downloading 100%") == 1
    assert render_progress_bar(1.0) in texts[-1]
    # No duplicate renders for repeated byte values.
    assert len(texts) == len(set(texts))


@pytest.mark.asyncio
async def test_reporter_large_file_milestones_and_unknown_size():
    texts: list[str] = []

    async def edit(text):
        texts.append(text)
        return True

    reporter = SaveProgressReporter(edit)
    total = 20 * MIB
    for step in (0.0, 0.2, 0.4, 0.6, 0.8, 1.0):
        await reporter.handle(
            SaveProgressEvent(
                phase="upload", completed_bytes=int(total * step), total_bytes=total,
                finished=step == 1.0,
            )
        )
    percentages = [text for text in texts]
    for milestone in LARGE_FILE_MILESTONES:
        assert any(f"Uploading {milestone}%" in text for text in percentages), milestone

    unknown: list[str] = []

    async def edit_unknown(text):
        unknown.append(text)
        return True

    unknown_reporter = SaveProgressReporter(edit_unknown)
    await unknown_reporter.handle(
        SaveProgressEvent(phase="download", completed_bytes=0, total_bytes=None)
    )
    assert unknown and "%" not in unknown[0]
    assert "Downloading…" in unknown[0]


@pytest.mark.asyncio
async def test_reporter_terminal_result_is_never_overwritten():
    texts: list[str] = []

    async def edit(text):
        texts.append(text)
        return True

    reporter = SaveProgressReporter(edit)
    await reporter.handle(
        SaveProgressEvent(phase="download", completed_bytes=0, total_bytes=1000)
    )
    await reporter.close("✓ done")
    assert texts[-1] == "✓ done"
    before = len(texts)

    await reporter.handle(
        SaveProgressEvent(phase="upload", completed_bytes=1000, total_bytes=1000, finished=True)
    )
    assert len(texts) == before


# ── progress: the REAL handler/reporter wiring ──


def _wire(client: FakeClient) -> None:
    inline_engine.set_self_client(client)
    inline_engine.set_owner_id(OWNER)


@pytest.mark.asyncio
async def test_reply_mode_edits_the_existing_panel_and_sends_no_message():
    target = photo_message(101, text="hello", download_size=200_000)
    reply = FakeMessage(
        102, chat_id=PANEL_CHAT, text="save this", reply_to_msg_id=101
    )
    client = FakeClient([target, reply])
    _wire(client)

    await save_handler._save_reply_wait_handler(
        "save this", PANEL_CHAT, 102, PANEL_CHAT, PANEL_MSG,
    )

    # Reply Mode reuses the panel it armed — it never adds a message.
    assert client.send_message_calls == []

    assert client.edit_calls, "progress never reached the panel"
    coords = {(call["chat_id"], call["msg_id"]) for call in client.edit_calls}
    assert coords == {(PANEL_CHAT, PANEL_MSG)}

    texts = [call["text"] for call in client.edit_calls]
    assert "Resolving the target message" in texts[0]
    for expected in (
        "Downloading 50%", "Downloading 100%", "Uploading 50%", "Uploading 100%",
    ):
        assert any(expected in text for text in texts), expected
    assert render_progress_bar(0.5) in "".join(texts)
    assert "Saved Successfully" in texts[-1]

    # The real transfer callbacks were attached, not dropped.
    assert client.download_calls and all(
        cb is not None for _id, cb in client.download_calls
    )
    assert client.send_calls[0]["progress_callback"] is not None


@pytest.mark.asyncio
async def test_link_mode_creates_the_status_message_before_resolution():
    target = photo_message(209974, chat_id=5406707941, text="hi", download_size=300_000)
    client = FakeClient([target])
    _wire(client)

    await save_handler._save_link_input_handler(
        "tg://openmessage?user_id=5406707941&message_id=209974",
        PANEL_CHAT, 103, PANEL_CHAT, PANEL_MSG,
    )

    assert len(client.send_message_calls) == 1
    status = client.send_message_calls[0]
    assert status["chat_id"] == PANEL_CHAT
    assert "Save by Link" in status["text"]
    # Created while nothing but the link is known — before any transfer.
    assert client.calls.index("send_message") < client.calls.index("download_media")

    coords = {(call["chat_id"], call["msg_id"]) for call in client.edit_calls}
    assert coords == {(status["chat_id"], status["message_id"])}
    texts = [call["text"] for call in client.edit_calls]
    assert any("Downloading 50%" in text for text in texts)
    assert any("Uploading 100%" in text for text in texts)
    assert any("Save by Link" in text for text in texts)
    assert "Saved Successfully" in texts[-1]


@pytest.mark.asyncio
async def test_album_progress_names_the_current_member():
    first, second, third = _album_members()
    client = FakeClient([first, second, third])
    _wire(client)
    reply = FakeMessage(102, chat_id=PANEL_CHAT, text="save", reply_to_msg_id=11)
    client._by_id[102] = reply

    await save_handler._save_reply_wait_handler(
        "save", PANEL_CHAT, 102, PANEL_CHAT, PANEL_MSG,
    )

    texts = [call["text"] for call in client.edit_calls]
    assert any("File 1/3" in text for text in texts)
    assert any("File 2/3" in text for text in texts)
    assert any("File 3/3" in text for text in texts)
    # Each member's own percentage is reported, never a merged global one.
    assert any("File 2/3" in text and "Downloading 50%" in text for text in texts)


@pytest.mark.asyncio
async def test_progress_edit_failures_never_break_the_save():
    target = photo_message(111, text="hello", download_size=200_000)
    reply = FakeMessage(112, chat_id=PANEL_CHAT, text="save", reply_to_msg_id=111)
    client = FakeClient([target, reply], edit_error=RuntimeError("message can't be edited"))
    _wire(client)

    await save_handler._save_reply_wait_handler(
        "save", PANEL_CHAT, 112, PANEL_CHAT, PANEL_MSG,
    )

    assert len(_rows()) == 1
    assert client.edit_calls, "the reporter still attempted its edits"


@pytest.mark.asyncio
async def test_unknown_size_progress_has_no_invented_percentage():
    target = document_message(
        131, text="hello", size=None, download_size=200_000, unknown_size=True
    )
    client = FakeClient([target])
    _wire(client)

    await save_handler._save_link_input_handler(
        "https://t.me/somechannel/131", PANEL_CHAT, 132, PANEL_CHAT, PANEL_MSG,
    )

    texts = [call["text"] for call in client.edit_calls]
    assert any("Downloading…" in text for text in texts)
    assert any("Downloading 100%" in text for text in texts)
    assert "Saved Successfully" in texts[-1]


@pytest.mark.asyncio
async def test_progress_observer_errors_never_abort_the_transfer():
    target = photo_message(141, text="hello", download_size=200_000)
    client = FakeClient([target])

    async def exploding_observer(event):
        raise RuntimeError("observer down")

    result = await save_service.execute_save(
        client, OWNER, target, "UTC", progress=exploding_observer
    )

    assert getattr(result, "saved", False) is True
    assert len(_rows()) == 1


@pytest.mark.asyncio
async def test_large_transfer_reports_all_five_milestones_per_phase():
    target = document_message(
        161, size=12 * MIB, mime="video/mp4", name="big.mp4",
        download_size=12 * MIB, text="big",
    )
    client = FakeClient([target])
    _wire(client)

    await save_handler._save_link_input_handler(
        "https://t.me/somechannel/161", PANEL_CHAT, 162, PANEL_CHAT, PANEL_MSG,
    )

    texts = [call["text"] for call in client.edit_calls]
    for milestone in LARGE_FILE_MILESTONES:
        assert any(f"Downloading {milestone}%" in text for text in texts), milestone
        assert any(f"Uploading {milestone}%" in text for text in texts), milestone
    assert "Saved Successfully" in texts[-1]


@pytest.mark.asyncio
async def test_no_self_client_means_no_status_message_and_no_progress():
    target = photo_message(151, text="hello", download_size=200_000)
    client = FakeClient([target])

    result = await save_service.execute_save(client, OWNER, target, "UTC")

    assert getattr(result, "saved", False) is True
    assert client.edit_calls == []
    assert client.send_message_calls == []
    assert await save_handler._start_status(None, PANEL_CHAT, title="Deep Save", body="x") is None
    assert await save_handler._start_status(client, 0, title="Deep Save", body="x") is None
    assert client.send_message_calls == []


# ── the REAL Telethon album boundary (offline, no session, no network) ──


@pytest.mark.asyncio
async def test_real_telethon_sends_one_multi_media_request_for_the_album():
    """Pinned Telethon 1.43 groups the album into ONE SendMultiMediaRequest.

    This exercises the library's own ``send_file`` path with pre-uploaded
    ``InputMediaUploadedPhoto`` items (exactly what ``_deep_save_album``
    builds), so "one actual album" is proven at the request level rather than
    inferred from our own call list: N ``UploadMediaRequest`` conversions and
    ONE grouped send, never N ``SendMediaRequest`` messages.
    """
    from telethon import TelegramClient
    from telethon.sessions import MemorySession
    from telethon.tl import functions, types

    client = TelegramClient(MemorySession(), 1, "0" * 32)
    requests: list = []
    photo_serial = {"n": 0}

    def _photo():
        photo_serial["n"] += 1
        return types.Photo(
            id=photo_serial["n"],
            access_hash=photo_serial["n"],
            file_reference=b"",
            date=None,
            sizes=[],
            dc_id=2,
            has_stickers=False,
        )

    async def fake_get_input_entity(entity):
        return types.InputPeerSelf()

    async def fake_parse_message_text(text, parse_mode):
        return text, []

    async def fake_call(request):
        requests.append(request)
        if isinstance(request, functions.messages.UploadMediaRequest):
            return types.MessageMediaPhoto(photo=_photo(), ttl_seconds=None)
        if isinstance(request, functions.messages.SendMultiMediaRequest):
            updates = []
            messages = []
            for index, item in enumerate(request.multi_media):
                message = types.Message(
                    id=800 + index,
                    peer_id=types.PeerUser(user_id=1),
                    date=None,
                    message=item.message,
                )
                messages.append(message)
                updates.append(
                    types.UpdateMessageID(random_id=item.random_id, id=message.id)
                )
                updates.append(
                    types.UpdateNewMessage(message=message, pts=index + 1, pts_count=1)
                )
            return types.Updates(
                updates=updates, users=[], chats=[], date=None, seq=0
            )
        raise AssertionError(f"unexpected request: {request!r}")

    client.get_input_entity = fake_get_input_entity  # type: ignore[assignment]
    client._parse_message_text = fake_parse_message_text  # type: ignore[assignment]
    original_call = TelegramClient.__call__

    async def fake_send(_self, request):
        return await fake_call(request)

    TelegramClient.__call__ = fake_send  # type: ignore[assignment]

    try:
        media = [
            InputMediaUploadedPhoto(
                file=InputFile(id=900 + index, parts=1, name=f"p{index}.jpg", md5_checksum="")
            )
            for index in range(3)
        ]
        sent = await client.send_file("me", media, caption="ALBUM CAPTION")
    finally:
        TelegramClient.__call__ = original_call  # type: ignore[assignment]
        await client.disconnect()

    send_requests = [
        request for request in requests
        if isinstance(request, functions.messages.SendMultiMediaRequest)
    ]
    assert len(send_requests) == 1
    multi = send_requests[0].multi_media
    assert len(multi) == 3
    assert all(isinstance(item, types.InputSingleMedia) for item in multi)
    assert all(isinstance(item.media, types.InputMediaPhoto) for item in multi)
    assert multi[0].message == "ALBUM CAPTION"
    assert [item.message for item in multi[1:]] == ["", ""]
    assert not any(
        isinstance(request, functions.messages.SendMediaRequest) for request in requests
    )
    assert [message.id for message in sent] == [800, 801, 802]
