"""Focused regressions for album Deep Save, view-once media and live progress.

Scope:
  * an album source saves EVERY resolved member through the one Deep Save
    pipeline, with one Saved Messages message and one ``saved_items`` row per
    item, no duplicates and no forwarding;
  * partial album failure is reported honestly (never "the whole album
    succeeded") and temporary files are removed on every path;
  * view-once (``ttl_seconds == 2147483647``) and other expiring TTLs are
    refused BEFORE ``download_media`` / ``send_file`` and produce no row,
    while ordinary photos and documents keep saving;
  * the five-segment progress bar is the SAME renderer the model tester uses,
    the milestone cadence is driven by the real transfer size (50/100 at or
    below 10 MiB, 20/40/60/80/100 above it) per phase, the existing status
    message is edited in place, and a terminal result can never be replaced by
    a late progress event.

No network, no Telegram session, no Supabase: the fake client reports the same
``progress_callback(current, total)`` contract pinned Telethon 1.43.0 awaits.
"""
from __future__ import annotations

import inspect
import os
import tempfile

import pytest
from telethon.tl.types import (
    DocumentAttributeFilename,
    MessageMediaDocument,
    MessageMediaPhoto,
)

from backend.db import client as db_client
from backend.helper.progress import SEGMENTS, render_progress_bar
from backend.helper.save_progress import (
    LARGE_FILE_MILESTONES,
    SMALL_FILE_LIMIT_BYTES,
    SMALL_FILE_MILESTONES,
    SaveProgressReporter,
    milestones_for_size,
    progress_percentage,
)
from backend.services import save_service
from backend.services.save_service import (
    EPHEMERAL_EXPIRING,
    EPHEMERAL_VIEW_ONCE,
    VIEW_ONCE_TTL_SECONDS,
    SaveProgressEvent,
)

CHAT = -1001234567890
MIB = 1024 * 1024


# ── fakes ──


class FakeSender:
    first_name = "Test"
    last_name = "User"


class FakePhoto:
    def __init__(self, photo_id: int, size: int):
        self.id = photo_id
        self.sizes = [type("S", (), {"size": size})()]


class FakeDoc:
    def __init__(self, doc_id: int, *, mime: str = "image/jpeg", size: int = 1000):
        self.id = doc_id
        self.mime_type = mime
        self.size = size
        self.attributes = [DocumentAttributeFilename(f"file_{doc_id}.jpg")]


class FakeMessage:
    def __init__(
        self,
        msg_id: int,
        *,
        chat_id: int = CHAT,
        media=None,
        text: str = "source text",
        grouped_id: int | None = None,
        sender_id: int = 777,
    ):
        self.id = msg_id
        self.chat_id = chat_id
        self.media = media
        self.text = text
        self.grouped_id = grouped_id
        self.sender_id = sender_id
        self.reply_to_msg_id = None

    async def get_sender(self):
        return FakeSender()


class FakeSent:
    def __init__(self, chat_id="me", msg_id=600, media=None):
        self.chat_id = chat_id
        self.id = msg_id
        self.media = media


def photo_message(msg_id: int, *, size: int = 1000, grouped_id=None, chat_id=CHAT, text="photo"):
    return FakeMessage(
        msg_id,
        chat_id=chat_id,
        media=MessageMediaPhoto(photo=FakePhoto(msg_id, size), ttl_seconds=None),
        text=text,
        grouped_id=grouped_id,
    )


def document_message(msg_id: int, *, size: int = 1000, ttl=None, grouped_id=None, text="doc"):
    return FakeMessage(
        msg_id,
        media=MessageMediaDocument(
            document=FakeDoc(msg_id, mime="application/pdf", size=size),
            ttl_seconds=ttl,
        ),
        text=text,
        grouped_id=grouped_id,
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


def _declared_size(message) -> int:
    media = message.media
    if isinstance(media, MessageMediaDocument):
        return media.document.size
    if isinstance(media, MessageMediaPhoto):
        return media.photo.sizes[-1].size
    return 0


class FakeClient:
    """Minimal client honoring the exact methods the save pipeline calls."""

    def __init__(
        self,
        messages=(),
        *,
        window_extras=(),
        special_messages=None,
        fetch_error: Exception | None = None,
        emit_callbacks: bool = True,
        download_steps=(0.25, 0.5, 0.75, 1.0),
        upload_steps=(0.25, 0.5, 0.75, 1.0),
        fail_download_ids=(),
        fail_upload_calls=(),
        payload_size: int = 400,
    ):
        self._by_id = {message.id: message for message in messages}
        self._special = dict(special_messages or {})
        self._window_extras = list(window_extras)
        self.fetch_error = fetch_error
        self.emit_callbacks = emit_callbacks
        self.download_steps = tuple(download_steps)
        self.upload_steps = tuple(upload_steps)
        self.fail_download_ids = set(fail_download_ids)
        self.fail_upload_calls = set(fail_upload_calls)
        self.payload_size = payload_size
        self.calls: list[tuple] = []
        self.album_windows: list[list[int]] = []
        self._upload_count = 0
        self._sent_id = 600

    async def get_messages(self, chat_id, ids):
        if self.fetch_error is not None:
            raise self.fetch_error
        if isinstance(ids, (list, tuple)):
            self.album_windows.append(list(ids))
            found = [self._by_id.get(message_id) for message_id in ids]
            return found + list(self._window_extras)
        if ids in self._special:
            return self._special[ids]
        return self._by_id.get(ids)

    async def download_media(self, message, file=None, progress_callback=None, **kwargs):
        self.calls.append(("download_media", message.id))
        if message.id in self.fail_download_ids:
            raise RuntimeError("network down")
        payload = bytes([65 + (message.id % 26)]) * self.payload_size
        if file is not None:
            os.makedirs(os.path.dirname(file), exist_ok=True)
            with open(file, "wb") as handle:
                handle.write(payload)
        if progress_callback is not None and self.emit_callbacks:
            total = _declared_size(message) or len(payload)
            for step in self.download_steps:
                result = progress_callback(int(total * step), total)
                if inspect.isawaitable(result):
                    await result
        return file

    async def send_file(self, entity, file, caption=None, progress_callback=None, **kwargs):
        self._upload_count += 1
        size = os.path.getsize(file)
        self.calls.append(("send_file", entity, os.path.basename(file), size))
        if self._upload_count in self.fail_upload_calls:
            raise RuntimeError("upload rejected")
        if progress_callback is not None and self.emit_callbacks:
            for step in self.upload_steps:
                result = progress_callback(int(size * step), size)
                if inspect.isawaitable(result):
                    await result
        self._sent_id += 1
        return FakeSent(msg_id=self._sent_id)

    async def send_message(self, entity, text):
        self.calls.append(("send_message", entity, text))
        self._sent_id += 1
        return FakeSent(msg_id=self._sent_id)

    async def delete_messages(self, chat_id, message_ids):
        self.calls.append(("delete_messages", chat_id, list(message_ids)))

    async def forward_messages(self, *args, **kwargs):
        self.calls.append(("forward_messages", args, kwargs))
        raise AssertionError("Deep Save must never forward")


class FakeHelper:
    def __init__(self):
        self.edits: list[tuple] = []

    async def edit_message(self, chat_id, msg_id, text, buttons=None):
        self.edits.append((chat_id, msg_id, text, buttons))


@pytest.fixture(autouse=True)
def reset_fallback():
    db_client._fallback["saved_items"] = []
    db_client._fallback["bot_logs"] = []
    yield


def _rows() -> list[dict]:
    return db_client._fallback["saved_items"]


def _assert_no_leftover_temp_dirs():
    leftovers = [name for name in os.listdir(tempfile.gettempdir()) if name.startswith("lifeos_dl_")]
    assert leftovers == [], f"leftover temp dirs: {leftovers}"


def _collector():
    edits: list[str] = []

    async def _edit(text: str) -> bool:
        edits.append(text)
        return True

    return edits, _edit


def _call_names(client: FakeClient) -> list[str]:
    return [call[0] for call in client.calls]


# ── album save ──


@pytest.mark.asyncio
async def test_album_saves_every_intended_member_with_its_own_identity():
    first = photo_message(11, grouped_id=9001, text="first")
    second = photo_message(12, grouped_id=9001, text="second")
    third = photo_message(13, grouped_id=9001, text="third")
    unrelated = photo_message(14, grouped_id=7777, text="other album")
    other_chat = photo_message(15, chat_id=CHAT + 1, grouped_id=9001)
    client = FakeClient([first, second, third], window_extras=[unrelated, other_chat])

    result = await save_service.execute_save(client, 42, second, "UTC")

    assert result.startswith("✓")
    assert "3 of 3" in result
    assert [call for call in client.calls if call[0] == "download_media"] and [
        call[1] for call in client.calls if call[0] == "download_media"
    ] == [11, 12, 13]
    assert len([call for call in client.calls if call[0] == "send_file"]) == 3

    rows = _rows()
    assert len(rows) == 3
    assert sorted(row["origin_msg_id"] for row in rows) == [11, 12, 13]
    assert len({row["saved_msg_id"] for row in rows}) == 3
    assert len({row["save_code"] for row in rows}) == 3
    by_source = {row["origin_msg_id"]: row for row in rows}
    # Each row references the NEW message its own upload created (601..603 in
    # upload order), never a sibling's destination.
    assert [by_source[mid]["saved_msg_id"] for mid in (11, 12, 13)] == [601, 602, 603]
    assert "first" in by_source[11]["caption"]
    assert "second" in by_source[12]["caption"]
    assert "third" in by_source[13]["caption"]
    assert f"{CHAT}/11" in by_source[11]["caption"]
    assert 14 not in by_source and 15 not in by_source
    assert "forward_messages" not in _call_names(client)
    _assert_no_leftover_temp_dirs()


@pytest.mark.asyncio
async def test_album_members_are_never_duplicated():
    first = photo_message(21, grouped_id=555)
    second = photo_message(22, grouped_id=555)
    client = FakeClient(
        [first, second],
        # A duplicate of the selected target plus an unrelated neighbour.
        window_extras=[second, photo_message(23, grouped_id=999)],
    )

    result = await save_service.execute_save(client, 42, second, "UTC")

    assert "2 of 2" in result
    downloads = [call[1] for call in client.calls if call[0] == "download_media"]
    assert downloads == [21, 22]
    assert len(_rows()) == 2
    assert len({row["saved_msg_id"] for row in _rows()}) == 2


@pytest.mark.asyncio
async def test_album_partial_download_failure_is_reported_honestly():
    first = photo_message(31, grouped_id=44)
    second = photo_message(32, grouped_id=44)
    third = photo_message(33, grouped_id=44)
    client = FakeClient([first, second, third], fail_download_ids={32})

    result = await save_service.execute_save(client, 42, first, "UTC")

    assert result.startswith("◬")
    assert "2 of 3" in result
    assert "3 of 3" not in result
    assert "Item 2" in result
    assert sorted(row["origin_msg_id"] for row in _rows()) == [31, 33]
    assert "forward_messages" not in _call_names(client)
    _assert_no_leftover_temp_dirs()


@pytest.mark.asyncio
async def test_album_partial_upload_failure_is_reported_honestly():
    first = photo_message(41, grouped_id=45)
    second = photo_message(42, grouped_id=45)
    third = photo_message(43, grouped_id=45)
    client = FakeClient([first, second, third], fail_upload_calls={2})

    result = await save_service.execute_save(client, 42, third, "UTC")

    assert result.startswith("◬")
    assert "2 of 3" in result
    assert sorted(row["origin_msg_id"] for row in _rows()) == [41, 43]
    _assert_no_leftover_temp_dirs()


@pytest.mark.asyncio
async def test_album_total_failure_never_claims_success():
    first = photo_message(51, grouped_id=46)
    second = photo_message(52, grouped_id=46)
    client = FakeClient([first, second], fail_download_ids={51, 52})

    result = await save_service.execute_save(client, 42, first, "UTC")

    assert result.startswith("×")
    assert "0 of 2" in result
    assert _rows() == []
    _assert_no_leftover_temp_dirs()


@pytest.mark.asyncio
async def test_album_resolution_failure_never_silently_saves_one_item():
    first = photo_message(61, grouped_id=47)
    client = FakeClient([first], fetch_error=RuntimeError("flood wait"))

    result = await save_service.execute_save(client, 42, first, "UTC")

    assert result.startswith("×")
    assert "could not be resolved" in result
    assert "download_media" not in _call_names(client)
    assert "send_file" not in _call_names(client)
    assert _rows() == []


@pytest.mark.asyncio
async def test_a_non_album_message_never_fetches_neighbours():
    message = photo_message(71)
    client = FakeClient([message])

    result = await save_service.execute_save(client, 42, message, "UTC")

    assert "Saved Successfully" in result
    assert client.album_windows == []
    assert [call[1] for call in client.calls if call[0] == "download_media"] == [71]


# ── view-once / expiring media ──


def test_view_once_marker_and_expiring_ttls_are_distinguished():
    assert save_service.media_ttl_seconds(view_once_message(1).media) == VIEW_ONCE_TTL_SECONDS
    assert save_service.ephemeral_media_kind(view_once_message(1).media) == EPHEMERAL_VIEW_ONCE

    expiring = document_message(2, ttl=30)
    assert save_service.media_ttl_seconds(expiring.media) == 30
    assert save_service.ephemeral_media_kind(expiring.media) == EPHEMERAL_EXPIRING

    assert save_service.ephemeral_media_kind(photo_message(3).media) is None
    assert save_service.media_ttl_seconds(photo_message(3).media) is None


@pytest.mark.asyncio
async def test_view_once_media_is_refused_before_any_transfer():
    message = view_once_message(81)
    client = FakeClient([message])

    result = await save_service.execute_save(client, 42, message, "UTC")

    assert result.startswith("×")
    assert "view-once" in result
    assert client.calls == []  # no download, no upload, no forward
    assert _rows() == []
    _assert_no_leftover_temp_dirs()


@pytest.mark.asyncio
async def test_expiring_media_is_refused_with_its_own_reason():
    message = document_message(82, ttl=30)
    client = FakeClient([message])

    result = await save_service.execute_save(client, 42, message, "UTC")

    assert result.startswith("×")
    assert "30s" in result
    assert "view-once" not in result
    assert client.calls == []
    assert _rows() == []


@pytest.mark.asyncio
async def test_ordinary_photo_and_document_still_save():
    photo = photo_message(83)
    document = document_message(84)

    photo_result = await save_service.execute_save(FakeClient([photo]), 42, photo, "UTC")
    document_result = await save_service.execute_save(FakeClient([document]), 42, document, "UTC")

    assert "Saved Successfully" in photo_result
    assert "Saved Successfully" in document_result
    assert sorted(row["origin_msg_id"] for row in _rows()) == [83, 84]


@pytest.mark.asyncio
async def test_a_view_once_album_member_fails_without_blocking_the_others():
    first = photo_message(91, grouped_id=66)
    one_time = view_once_message(92, grouped_id=66)
    third = photo_message(93, grouped_id=66)
    client = FakeClient([first, one_time, third])

    result = await save_service.execute_save(client, 42, first, "UTC")

    assert result.startswith("◬")
    assert "2 of 3" in result
    assert "view-once" in result
    downloads = [call[1] for call in client.calls if call[0] == "download_media"]
    assert downloads == [91, 93]  # the view-once member was never touched
    assert sorted(row["origin_msg_id"] for row in _rows()) == [91, 93]


# ── progress: bar, cadence, real bytes ──


def test_progress_bar_is_the_shared_five_segment_renderer():
    from backend.bot.handlers import ai_test_progress

    assert SEGMENTS == 5
    for fraction in (0.0, 0.01, 0.2, 0.4, 0.5, 0.79, 0.8, 1.0):
        bar = render_progress_bar(fraction)
        assert bar == ai_test_progress.render_progress_bar(fraction)
        assert len(bar) == 5
        assert set(bar) <= {"▰", "▱"}

    assert render_progress_bar(0.0) == "▱▱▱▱▱"
    # The existing renderer's own fill calculation: int(fraction * 5).
    assert render_progress_bar(0.5) == "▰▰▱▱▱"
    assert render_progress_bar(0.6) == "▰▰▰▱▱"
    assert render_progress_bar(1.0) == "▰▰▰▰▰"


def test_milestone_cadence_is_derived_from_the_actual_size():
    assert milestones_for_size(SMALL_FILE_LIMIT_BYTES) == SMALL_FILE_MILESTONES
    assert milestones_for_size(SMALL_FILE_LIMIT_BYTES - 1) == SMALL_FILE_MILESTONES
    assert milestones_for_size(SMALL_FILE_LIMIT_BYTES + 1) == LARGE_FILE_MILESTONES
    assert milestones_for_size(None) == ()
    assert milestones_for_size(0) == ()
    assert SMALL_FILE_MILESTONES == (50, 100)
    assert LARGE_FILE_MILESTONES == (20, 40, 60, 80, 100)


def test_percentage_comes_from_the_callback_byte_counts():
    assert progress_percentage(50, 100) == 50
    assert progress_percentage(20, 100) == 20
    assert progress_percentage(0, 100) == 0
    assert progress_percentage(100, 100) == 100
    assert progress_percentage(5, 0) is None
    assert progress_percentage(5, None) is None


@pytest.mark.asyncio
async def test_small_file_reports_50_and_100_for_download_and_upload():
    message = photo_message(101, size=2000)
    client = FakeClient([message], download_steps=(0.25, 0.5, 0.75, 1.0),
                        upload_steps=(0.25, 0.5, 0.75, 1.0))
    edits, edit = _collector()
    reporter = SaveProgressReporter(edit)

    result = await save_service.execute_save(client, 42, message, "UTC", progress=reporter.handle)
    await reporter.close(result)

    progress_texts = edits[:-1]
    assert "Downloading 50%" in "\n".join(progress_texts)
    assert "Downloading 100%" in "\n".join(progress_texts)
    assert "Uploading 50%" in "\n".join(progress_texts)
    assert "Uploading 100%" in "\n".join(progress_texts)
    assert "▰▰▱▱▱" in "\n".join(progress_texts)
    assert "▰▰▰▰▰" in "\n".join(progress_texts)
    download_index = next(i for i, t in enumerate(progress_texts) if "Downloading 50%" in t)
    download_done = next(i for i, t in enumerate(progress_texts) if "Downloading 100%" in t)
    upload_index = next(i for i, t in enumerate(progress_texts) if "Uploading 50%" in t)
    assert download_index < download_done < upload_index
    assert edits[-1] == result


@pytest.mark.asyncio
async def test_large_download_reports_20_40_60_80_100_while_small_upload_keeps_its_own_cadence():
    # The declared source size is large; the local file the fake writes is
    # small, so the two phases must be measured independently.
    message = document_message(102, size=12 * MIB)
    client = FakeClient(
        [message],
        payload_size=4096,
        download_steps=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
        upload_steps=(0.5, 1.0),
    )
    edits, edit = _collector()
    reporter = SaveProgressReporter(edit)

    result = await save_service.execute_save(client, 42, message, "UTC", progress=reporter.handle)
    await reporter.close(result)

    download_texts = [text for text in edits if "Downloading" in text]
    upload_texts = [text for text in edits if "Uploading" in text]
    for milestone in (20, 40, 60, 80, 100):
        assert any(f"Downloading {milestone}%" in text for text in download_texts), milestone
    assert not any("Downloading 50%" in text for text in download_texts)
    assert any("Uploading 50%" in text for text in upload_texts)
    assert any("Uploading 100%" in text for text in upload_texts)
    assert not any("Uploading 20%" in text for text in upload_texts)


@pytest.mark.asyncio
async def test_album_progress_is_reported_per_file():
    first = photo_message(111, grouped_id=77, size=1000)
    second = photo_message(112, grouped_id=77, size=1000)
    third = photo_message(113, grouped_id=77, size=1000)
    client = FakeClient([first, second, third])
    edits, edit = _collector()
    reporter = SaveProgressReporter(edit)

    result = await save_service.execute_save(client, 42, first, "UTC", progress=reporter.handle)
    await reporter.close(result)

    for index in (1, 2, 3):
        assert any(f"File {index}/3" in text for text in edits), index
    # No combined album percentage exists: each edit names one file.
    assert all("File 1/3" in text or "File 2/3" in text or "File 3/3" in text
               for text in edits[:-1])


@pytest.mark.asyncio
async def test_repeated_callback_values_do_not_repeat_edits():
    edits, edit = _collector()
    reporter = SaveProgressReporter(edit)
    total = 2000

    for current in (1000, 1000, 1000, 1999, 2000, 2000):
        await reporter.handle(
            SaveProgressEvent("download", current, total, item_index=1, item_count=1)
        )

    texts = [text for text in edits if "Downloading" in text]
    assert sum("Downloading 50%" in text for text in texts) == 1
    assert sum("Downloading 100%" in text for text in texts) == 1
    assert len(texts) == 2

    await reporter.handle(
        SaveProgressEvent("download", total, total, item_index=1, item_count=1, finished=True)
    )
    assert len([text for text in edits if "Downloading" in text]) == 2


@pytest.mark.asyncio
async def test_progress_edit_failure_never_aborts_the_save():
    message = photo_message(121, size=1000)
    client = FakeClient([message])

    async def _boom(_text):
        raise RuntimeError("telegram edit failed")

    reporter = SaveProgressReporter(_boom)
    result = await save_service.execute_save(client, 42, message, "UTC", progress=reporter.handle)
    assert not await reporter.close(result)

    assert "Saved Successfully" in result
    assert len(_rows()) == 1
    _assert_no_leftover_temp_dirs()


@pytest.mark.asyncio
async def test_a_terminal_result_cannot_be_overwritten_by_a_late_progress_event():
    edits, edit = _collector()
    reporter = SaveProgressReporter(edit)

    await reporter.close("terminal result")
    await reporter.handle(
        SaveProgressEvent("upload", 1000, 2000, item_index=1, item_count=1)
    )
    await reporter.handle(
        SaveProgressEvent("upload", 2000, 2000, item_index=1, item_count=1, finished=True)
    )

    assert edits == ["terminal result"]
    assert reporter.closed is True


@pytest.mark.asyncio
async def test_unknown_size_transfer_never_fabricates_a_percentage():
    edits, edit = _collector()
    reporter = SaveProgressReporter(edit)

    await reporter.handle(SaveProgressEvent("download", 0, None))
    await reporter.handle(SaveProgressEvent("download", 500, None))
    assert len(edits) == 1
    assert "Downloading…" in edits[0]
    assert "%" not in edits[0]

    await reporter.handle(
        SaveProgressEvent("download", 0, None, item_index=1, item_count=1, finished=True)
    )
    assert len(edits) == 2
    assert "Downloading 100%" in edits[1]
    assert "▰▰▰▰▰" in edits[1]


@pytest.mark.asyncio
async def test_a_transfer_without_callbacks_still_ends_truthfully():
    message = photo_message(131, size=1000)
    client = FakeClient([message], emit_callbacks=False)
    edits, edit = _collector()
    reporter = SaveProgressReporter(edit)

    result = await save_service.execute_save(client, 42, message, "UTC", progress=reporter.handle)
    await reporter.close(result)

    assert "Saved Successfully" in result
    assert any("Downloading 100%" in text for text in edits)
    assert any("Uploading 100%" in text for text in edits)
    assert edits[-1] == result
    assert len(_rows()) == 1


# ── handler wiring: the SAME status message gets progress and the result ──


@pytest.mark.asyncio
async def test_reply_mode_edits_its_panel_message_with_progress_and_result(monkeypatch):
    from backend.bot.handlers import save as save_handler
    from backend.helper import inline_engine, input_state

    target = photo_message(555, size=1000, text="target text")
    reply = FakeMessage(444)
    reply.reply_to_msg_id = 555
    client = FakeClient([target], special_messages={444: reply})
    helper = FakeHelper()

    monkeypatch.setattr(inline_engine, "_owner_id", 12345)
    monkeypatch.setattr(inline_engine, "_self_client", client)
    monkeypatch.setattr(save_handler, "get_client", lambda: helper)
    input_state.clear_pending(12345)

    await save_handler._save_reply_wait_handler("ignored", CHAT, 444, 222, 333)

    targets = {(chat_id, msg_id) for chat_id, msg_id, _text, _buttons in helper.edits}
    assert targets == {(222, 333)}  # one status message, edited in place
    joined = "\n".join(text for _chat, _msg, text, _buttons in helper.edits)
    assert "Downloading 50%" in joined
    assert "Uploading 100%" in joined
    assert "Saved Successfully" in helper.edits[-1][2]
    assert "forward_messages" not in _call_names(client)
    assert ("delete_messages", CHAT, [444]) in client.calls


@pytest.mark.asyncio
async def test_link_save_edits_its_panel_message_with_progress_and_result(monkeypatch):
    from backend.bot.handlers import save as save_handler
    from backend.helper import inline_engine, input_state

    message = photo_message(42, size=1000, chat_id=-100123456)
    client = FakeClient([message])
    helper = FakeHelper()

    monkeypatch.setattr(inline_engine, "_owner_id", 12345)
    monkeypatch.setattr(inline_engine, "_self_client", client)
    monkeypatch.setattr(save_handler, "get_client", lambda: helper)
    input_state.clear_pending(12345)

    await save_handler._save_link_input_handler(
        "https://t.me/c/123456/42", CHAT, 444, 222, 333
    )

    targets = {(chat_id, msg_id) for chat_id, msg_id, _text, _buttons in helper.edits}
    assert targets == {(222, 333)}
    joined = "\n".join(text for _chat, _msg, text, _buttons in helper.edits)
    assert "Downloading 50%" in joined
    assert "Uploading 50%" in joined
    assert "Saved Successfully" in helper.edits[-1][2]
    assert len(_rows()) == 1
    assert "forward_messages" not in _call_names(client)


@pytest.mark.asyncio
async def test_link_save_also_saves_the_whole_album(monkeypatch):
    from backend.bot.handlers import save as save_handler
    from backend.helper import inline_engine, input_state

    members = [
        photo_message(42, grouped_id=88, chat_id=-100123456),
        photo_message(43, grouped_id=88, chat_id=-100123456),
        photo_message(44, grouped_id=88, chat_id=-100123456),
    ]
    client = FakeClient(members)
    helper = FakeHelper()

    monkeypatch.setattr(inline_engine, "_owner_id", 12345)
    monkeypatch.setattr(inline_engine, "_self_client", client)
    monkeypatch.setattr(save_handler, "get_client", lambda: helper)
    input_state.clear_pending(12345)

    await save_handler._save_link_input_handler(
        "https://t.me/c/123456/43", CHAT, 444, 222, 333
    )

    assert sorted(row["origin_msg_id"] for row in _rows()) == [42, 43, 44]
    assert "3 of 3" in helper.edits[-1][2]
    assert any("File 2/3" in text for _chat, _msg, text, _buttons in helper.edits)


@pytest.mark.asyncio
async def test_handler_without_a_helper_keeps_the_previous_contract(monkeypatch):
    from backend.bot.handlers import save as save_handler
    from backend.helper import inline_engine, input_state

    message = photo_message(43, size=1000, chat_id=-100123456)
    client = FakeClient([message])

    monkeypatch.setattr(inline_engine, "_owner_id", 12345)
    monkeypatch.setattr(inline_engine, "_self_client", client)
    monkeypatch.setattr(save_handler, "get_client", lambda: None)
    input_state.clear_pending(12345)

    await save_handler._save_link_input_handler(
        "https://t.me/c/123456/43", CHAT, 444, 0, 0
    )

    # No helper-backed status message exists, so no progress UI is invented.
    assert len(_rows()) == 1
    assert ("delete_messages", CHAT, [444]) in client.calls
