"""Save V2 — Telegram saved-media synchronization (filename + Additional tags).

The live-test finding this suite pins: a saved item's metadata lived ONLY in
``saved_items``. Renaming or re-tagging changed the row while the actual Saved
Messages message kept the old filename and had no owner-tag section at all.

Two separate layers are synchronized, and they are never mixed:

    owner tag edit                          file-name change
      ↓ do_edit_tags(client=…)                ↓ do_change_file_name(client, …)
      ↓                                       ↓
    _sync_saved_caption  ← ONE caption        re-upload the SAME content with
      authority (edit_message, no re-send)    only DocumentAttributeFilename
      ↓                                       replaced
    saved_items.tags + caption                saved_items.saved_msg_id /
                                              file_name / file_id / caption
                                              → then delete the OLD message

Pinned here:

* the ACTUAL Telegram document filename changes (not just ``display_name``);
* the generated ``#saved*`` system hashtags and the original source text are
  byte-preserved, and owner tags live in their own labelled caption section;
* owner tags never enter the system hashtag line, and the system hashtags never
  enter ``saved_items.tags``;
* a tag-only change never downloads or re-uploads the media;
* the replacement message is created FIRST, the row is CONFIRMED, and only then
  is the previous message deleted — a failure anywhere before that leaves the
  original message and row untouched;
* media type, attributes and MIME survive the filename replacement;
* ``display_name`` and the Telegram file name stay uncoupled;
* the manual panel and the AI tools reach the SAME service operations.

Everything is offline and deterministic: the database boundary is the project's
in-memory fallback and Telegram is faked — no live credential is required.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from telethon.tl.types import (
    DocumentAttributeAudio,
    DocumentAttributeFilename,
    DocumentAttributeVideo,
    MessageMediaDocument,
    MessageMediaPhoto,
)

from backend.ai.actions import KIND_INVALID, KIND_EXECUTABLE, validate_action
from backend.ai.tools.context import ToolContext
from backend.ai.tools.executor import ToolExecutor
from backend.ai.tools.registry import create_default_registry
from backend.db import client as db_client
from backend.helper import input_state
from backend.services import retrieve_service, save_service

OWNER = 777
OTHER_OWNER = 999
CHAT = -100123
NOW = "2026-09-15T10:08:00+00:00"
OLD_MSG_ID = 400
NEW_MSG_ID = 401


def _caption(file_name="University_Week_2.pdf", tags=()):
    return save_service.build_caption(
        save_code="S0001",
        sender="Test User",
        chat_id=-1009999,
        msg_id=4321,
        dt=__import__("datetime").datetime(2026, 9, 15, 10, 8),
        media_type="Document",
        mime="application/pdf",
        file_size=1200,
        file_name=file_name,
        tags=save_service.caption_hashtags(
            "Document", __import__("datetime").datetime(2026, 9, 15, 10, 8)
        ),
        owner_tags=tags,
    ) + "\n\nsource text the owner saved"


def _row(save_code="S0001", *, owner_id=OWNER, display_name=None, tags=None,
         file_name="University_Week_2.pdf", caption=None, media_type="Document",
         saved_msg_id=OLD_MSG_ID, mime_type="application/pdf", created_at=NOW):
    # The stored caption of a real save carries the owner's tag section; a
    # legacy ``#saved*`` value in the column is caption decoration from older
    # versions and is NOT an owner tag, so it never renders as one.
    owner_tags = [t for t in (tags or []) if not str(t).startswith("#")]
    return {
        "id": abs(hash(save_code)) % 10_000,
        "save_code": save_code,
        "owner_id": owner_id,
        "display_name": display_name,
        "tags": list(tags or []),
        "media_type": media_type,
        "mime_type": mime_type,
        "file_size": 1200,
        "file_id": "stored-file-id",
        "file_name": file_name,
        "caption": _caption(file_name, owner_tags) if caption is None else caption,
        "created_at": created_at,
        "saved_chat_id": OWNER,
        "saved_msg_id": saved_msg_id,
        "origin_chat_id": -1009999,
        "origin_msg_id": 4321,
    }


def _seed(*rows):
    for row in rows:
        db_client._fallback["saved_items"].append(row)


def _stored(save_code: str) -> dict:
    return next(
        r for r in db_client._fallback["saved_items"] if r.get("save_code") == save_code
    )


@pytest.fixture(autouse=True)
def _clean_state():
    db_client._fallback["saved_items"] = []
    input_state.clear_all()
    yield
    db_client._fallback["saved_items"] = []
    input_state.clear_all()


# ── Telegram fakes ─────────────────────────────────────────────────────────

class FakeDoc:
    def __init__(self, *, file_name="University_Week_2.pdf", mime="application/pdf",
                 attributes=None, doc_id=9999, size=1200):
        self.id = doc_id
        self.mime_type = mime
        self.size = size
        self.attributes = (
            attributes
            if attributes is not None
            else [DocumentAttributeFilename(file_name)]
        )


class FakeSent:
    def __init__(self, media, chat_id=OWNER, msg_id=NEW_MSG_ID):
        self.media = media
        self.chat_id = chat_id
        self.id = msg_id


class FakeSavedClient:
    """The self client's slice used by the synchronizer, with recorded calls."""

    def __init__(self, message, *, download=b"pdf-bytes", edit_error=None,
                 send_error=None, download_error=None, delete_error=None,
                 send_returns=None):
        self.message = message
        self.download_bytes = download
        self.edit_error = edit_error
        self.send_error = send_error
        self.download_error = download_error
        self.delete_error = delete_error
        self.send_returns = send_returns
        self.downloads: list = []
        self.uploads: list = []
        self.edits: list = []
        self.deletes: list = []

    async def get_input_entity(self, chat_id):
        return chat_id

    async def get_messages(self, peer, ids=None):
        return self.message

    async def download_media(self, message, file=None):
        self.downloads.append(file)
        if self.download_error:
            raise self.download_error
        with open(file, "wb") as fh:
            fh.write(self.download_bytes)
        return file

    async def send_file(self, entity, path, caption=None, **kwargs):
        self.uploads.append({"entity": entity, "path": path, "caption": caption, **kwargs})
        if self.send_error:
            raise self.send_error
        if self.send_returns is not None:
            return self.send_returns
        return FakeSent(MessageMediaDocument(
            document=FakeDoc(file_name=kwargs.get("file_name_used") or "uploaded.pdf"),
            ttl_seconds=None,
        ))

    async def edit_message(self, peer, msg_id, text=None):
        self.edits.append({"peer": peer, "msg_id": msg_id, "text": text})
        if self.edit_error:
            raise self.edit_error
        return MagicMock()

    async def delete_messages(self, peer, ids):
        self.deletes.append({"peer": peer, "ids": list(ids)})
        if self.delete_error:
            raise self.delete_error
        return True


def _document_message(file_name="University_Week_2.pdf", *, attributes=None,
                      mime="application/pdf"):
    doc = FakeDoc(file_name=file_name, mime=mime, attributes=attributes)
    return type("Msg", (), {"id": OLD_MSG_ID, "message": _caption(file_name),
                            "media": MessageMediaDocument(document=doc, ttl_seconds=None)})()


# ── AI plumbing ────────────────────────────────────────────────────────────

class FakeTelegram:
    def __init__(self, client):
        self.client = client


def _ctx(client):
    return ToolContext(
        telegram=FakeTelegram(client), owner_id=OWNER, tz_str="UTC",
        extra={"chat_id": CHAT, "request_id": "save-v2-sync"},
    )


def _chain(client):
    ctx = _ctx(client)
    registry = create_default_registry(ctx)
    return registry, ctx, ToolExecutor(registry, ctx)


async def _run_tool(executor, ctx, name, arguments):
    results = await executor.execute_calls(
        [{"name": name, "arguments": arguments}],
        owner_id=ctx.owner_id, session_id="save-v2-sync", context_override=ctx,
    )
    return results[0]


def _plain(text: str) -> str:
    """Drop the caption's system-hashtag line for structural comparisons."""
    return "\n".join(
        line for line in text.split("\n") if not line.strip().startswith("#saved")
    )


def _without_tags_line(text: str) -> str:
    """The caption with its owner-tag section removed (all else untouched)."""
    return "\n".join(
        line for line in text.split("\n")
        if not line.strip().startswith(save_service.ADDITIONAL_TAGS_PREFIX)
    )


# ── caption section primitives ─────────────────────────────────────────────

def test_with_additional_tags_adds_replaces_and_removes_only_that_line():
    base = _caption()
    added = save_service.with_additional_tags(base, ["university", "semester-2"])
    assert "🏷 Additional tags: #university #semester-2" in added.split("\n")
    assert added != base

    replaced = save_service.with_additional_tags(added, ["archive"])
    assert "🏷 Additional tags: #archive" in replaced.split("\n")
    assert "university" not in replaced

    removed = save_service.with_additional_tags(added, [])
    assert base == removed
    assert "Additional tags" not in removed


def test_caption_transforms_preserve_the_hashtags_and_the_source_text():
    base = _caption()
    with_tags = save_service.with_additional_tags(base, ["university"])
    renamed = save_service.with_file_name(with_tags, "New_Name.pdf")

    # The generated system hashtag line is intact and still the last metadata
    # line, and the original source text is byte-identical.
    for caption in (with_tags, renamed):
        assert "#saved" in caption
        assert caption.endswith("\n\nsource text the owner saved")
        assert "#saved_document" in caption
    assert "📄 New_Name.pdf" in renamed.split("\n")
    assert "University_Week_2.pdf" not in renamed
    assert "🏷 Additional tags: #university" in renamed.split("\n")
    # The header line keeps its own icon — only the file-name SECTION moved.
    assert renamed.split("\n")[0] == base.split("\n")[0]


def test_a_source_text_line_that_looks_like_metadata_is_never_rewritten():
    base = save_service.build_caption(
        save_code="S0001", sender="S", chat_id=1, msg_id=2,
        dt=__import__("datetime").datetime(2026, 9, 15), media_type="Document",
        mime="application/pdf", file_size=10, file_name="a.pdf", tags=["#saved"],
    ) + "\n\n🏷 Additional tags: #not-ours\n📄 not-ours.pdf"
    updated = save_service.with_additional_tags(base, ["mine"])

    assert updated.endswith("🏷 Additional tags: #not-ours\n📄 not-ours.pdf")
    lines = updated.split("\n")
    assert lines.count("🏷 Additional tags: #mine") == 1


def test_the_caption_transforms_are_idempotent():
    base = _caption()
    once = save_service.with_additional_tags(base, ["a"])
    assert save_service.with_additional_tags(once, ["a"]) == once
    named = save_service.with_file_name(once, "x.pdf")
    assert save_service.with_file_name(named, "x.pdf") == named


def test_a_saved_item_caption_separates_owner_tags_from_system_hashtags():
    caption = save_service.build_caption(
        save_code="S0001", sender="S", chat_id=1, msg_id=2,
        dt=__import__("datetime").datetime(2026, 9, 15), media_type="Document",
        mime="application/pdf", file_size=10, file_name="a.pdf",
        tags=save_service.caption_hashtags("Document", __import__("datetime").datetime(2026, 9, 15)),
        owner_tags=["university", "semester-2"],
    )
    lines = caption.split("\n")
    assert "🏷 Additional tags: #university #semester-2" in lines
    assert any("#saved_document" in line for line in lines)
    # The owner section is its own line — never merged into the hashtag line.
    assert "#university" not in next(l for l in lines if l.strip().startswith("#saved"))


# ── a save writes the owner section ────────────────────────────────────────

@pytest.mark.asyncio
async def test_execute_save_writes_the_owner_section_and_keeps_tags_owner_only():
    sent_captions: list[str] = []

    class Client:
        async def send_message(self, entity, text):
            sent_captions.append(text)
            return FakeSent(None, chat_id=OWNER, msg_id=NEW_MSG_ID)

    class Reply:
        chat_id = -1009999
        id = 4321
        media = None
        text = "the source text"
        sender_id = OWNER

        async def get_sender(self):
            return type("E", (), {"first_name": "Source", "last_name": "Sender"})()

    result = await save_service.execute_save(
        Client(), OWNER, Reply(), "UTC",
        metadata=save_service.SaveMetadata.from_raw("My Schedule", ["university"]),
    )

    assert "Saved Successfully" in result
    caption = sent_captions[-1]
    assert "🏷 Additional tags: #university" in caption.split("\n")
    assert "the source text" in caption
    row = _stored(row_code(result))
    assert save_service.normalize_tags(row["tags"]) == ("university",)
    assert not any(str(t).startswith("#") for t in row["tags"])
    assert "🏷 Additional tags: #university" in row["caption"]
    assert "#saved" in row["caption"]


def row_code(result: str) -> str:
    return result.split("`")[1]


# ── tag synchronization (caption only, never a re-upload) ──────────────────

@pytest.mark.asyncio
async def test_adding_owner_tags_edits_only_the_additional_tags_section():
    row = _row(tags=["university"])
    before = row["caption"]
    _seed(row)
    client = FakeSavedClient(_document_message())

    result = await retrieve_service.do_edit_tags(
        OWNER, "S0001", retrieve_service.TAG_OP_ADD, ["semester-2"], client=client
    )

    assert result.startswith("✅")
    assert len(client.edits) == 1
    edited = client.edits[0]["text"]
    assert "🏷 Additional tags: #university #semester-2" in edited.split("\n")
    # Only that section moved: every other line is byte-identical.
    assert _without_tags_line(edited) == _without_tags_line(before)
    assert edited != before
    assert edited.endswith("\n\nsource text the owner saved")
    assert "#saved_document" in edited
    assert retrieve_service._owner_tags(_stored("S0001")) == ("university", "semester-2")
    assert _stored("S0001")["caption"] == edited
    # A tag-only change never touches the media.
    assert client.downloads == [] and client.uploads == [] and client.deletes == []


@pytest.mark.asyncio
async def test_replacing_and_clearing_owner_tags_rewrite_only_that_section():
    _seed(_row(tags=["university", "semester-2"]))
    client = FakeSavedClient(_document_message())

    await retrieve_service.do_edit_tags(
        OWNER, "S0001", retrieve_service.TAG_OP_REPLACE, ["archive"], client=client
    )
    assert "🏷 Additional tags: #archive" in client.edits[-1]["text"].split("\n")
    assert "university" not in client.edits[-1]["text"]

    await retrieve_service.do_edit_tags(
        OWNER, "S0001", retrieve_service.TAG_OP_REPLACE, [], client=client
    )
    cleared = client.edits[-1]["text"]
    assert "Additional tags" not in cleared
    assert cleared == _caption()
    assert retrieve_service._owner_tags(_stored("S0001")) == ()


@pytest.mark.asyncio
async def test_a_tag_edit_uses_the_live_caption_when_the_row_stores_none():
    _seed(_row(caption=""))
    client = FakeSavedClient(_document_message())

    await retrieve_service.do_edit_tags(
        OWNER, "S0001", retrieve_service.TAG_OP_REPLACE, ["archive"], client=client
    )

    edited = client.edits[-1]["text"]
    # Built from the message's own caption — never from nothing.
    assert "📄 University_Week_2.pdf" in edited
    assert "#saved" in edited
    assert "🏷 Additional tags: #archive" in edited.split("\n")
    assert edited.endswith("source text the owner saved")


@pytest.mark.asyncio
async def test_a_tag_edit_refuses_rather_than_writing_when_the_message_cannot_be_edited():
    row = _row(tags=["university"])
    before = row["caption"]
    _seed(row)
    client = FakeSavedClient(_document_message(), edit_error=RuntimeError("flood"))

    result = await retrieve_service.do_edit_tags(
        OWNER, "S0001", retrieve_service.TAG_OP_REPLACE, ["archive"], client=client
    )

    assert result.startswith("❌")
    assert retrieve_service._owner_tags(_stored("S0001")) == ("university",)
    assert _stored("S0001")["caption"] == before


@pytest.mark.asyncio
async def test_a_tag_edit_without_a_client_changes_only_the_stored_metadata():
    _seed(_row(tags=["university"]))

    result = await retrieve_service.do_edit_tags(
        OWNER, "S0001", retrieve_service.TAG_OP_REPLACE, ["archive"]
    )

    assert result.startswith("✅")
    assert retrieve_service._owner_tags(_stored("S0001")) == ("archive",)


@pytest.mark.asyncio
async def test_legacy_system_hashtags_are_never_treated_as_owner_tags():
    _seed(_row(tags=["#saved", "#saved_document", "university"]))
    client = FakeSavedClient(_document_message())

    assert retrieve_service._owner_tags(_stored("S0001")) == ("university",)
    await retrieve_service.do_edit_tags(
        OWNER, "S0001", retrieve_service.TAG_OP_REPLACE, ["archive"], client=client
    )

    stored = _stored("S0001")["tags"]
    assert "#saved" in stored and "#saved_document" in stored
    assert retrieve_service._owner_tags({"tags": stored}) == ("archive",)


@pytest.mark.asyncio
async def test_owner_tag_normalization_and_bounds_are_the_save_rules():
    _seed(_row())
    client = FakeSavedClient(_document_message())

    await retrieve_service.do_edit_tags(
        OWNER, "S0001", retrieve_service.TAG_OP_REPLACE,
        ["University", "university", "  semester-2  "], client=client,
    )
    assert retrieve_service._owner_tags(_stored("S0001")) == ("University", "semester-2")

    refused = await retrieve_service.do_edit_tags(
        OWNER, "S0001", retrieve_service.TAG_OP_REPLACE, ["x" * 41], client=client
    )
    assert refused.startswith("⚠️")
    assert retrieve_service._owner_tags(_stored("S0001")) == ("University", "semester-2")


# ── file-name synchronization (real re-upload) ─────────────────────────────

@pytest.mark.asyncio
async def test_the_file_name_change_reuploads_with_the_new_filename_attribute():
    _seed(_row())
    client = FakeSavedClient(_document_message())

    result = await retrieve_service.do_change_file_name(
        client, OWNER, "S0001", "University_Weekly_Schedule_Semester_2.pdf"
    )

    assert result.startswith("✅")
    assert "University_Weekly_Schedule_Semester_2.pdf" in result
    upload = client.uploads[0]
    names = [a.file_name for a in upload["attributes"] if isinstance(a, DocumentAttributeFilename)]
    assert names == ["University_Weekly_Schedule_Semester_2.pdf"]
    assert upload["caption"] is not None
    assert _stored("S0001")["file_name"] == "University_Weekly_Schedule_Semester_2.pdf"
    assert "📄 University_Weekly_Schedule_Semester_2.pdf" in _stored("S0001")["caption"]


@pytest.mark.asyncio
async def test_the_replacement_preserves_media_type_attributes_and_mime():
    doc = FakeDoc(
        file_name="clip.mp4", mime="video/mp4",
        attributes=[
            DocumentAttributeVideo(duration=5, w=1, h=1),
            DocumentAttributeFilename("clip.mp4"),
        ],
    )
    message = type("Msg", (), {
        "id": OLD_MSG_ID, "message": _caption("clip.mp4"),
        "media": MessageMediaDocument(document=doc, ttl_seconds=None),
    })()
    _seed(_row(media_type="Video", file_name="clip.mp4", mime_type="video/mp4"))
    client = FakeSavedClient(message)

    await retrieve_service.do_change_file_name(client, OWNER, "S0001", "lesson.mp4")

    upload = client.uploads[0]
    assert upload["force_document"] is False
    assert upload["mime_type"] == "video/mp4"
    assert any(isinstance(a, DocumentAttributeVideo) for a in upload["attributes"])
    names = [a.file_name for a in upload["attributes"] if isinstance(a, DocumentAttributeFilename)]
    assert names == ["lesson.mp4"]


@pytest.mark.asyncio
async def test_the_file_name_change_keeps_the_owner_tags_section():
    _seed(_row(tags=["university"]))
    client = FakeSavedClient(_document_message())

    await retrieve_service.do_change_file_name(client, OWNER, "S0001", "New.pdf")

    caption = client.uploads[0]["caption"]
    assert "🏷 Additional tags: #university" in caption.split("\n")
    assert "📄 New.pdf" in caption.split("\n")
    assert caption.endswith("\n\nsource text the owner saved")
    assert "#saved_document" in caption


@pytest.mark.asyncio
async def test_the_row_is_repointed_and_the_old_message_is_deleted_last():
    _seed(_row())
    order: list[str] = []
    client = FakeSavedClient(_document_message())

    real_send = client.send_file
    real_delete = client.delete_messages
    real_update = db_client.update_save_fields

    async def send_file(*args, **kwargs):
        order.append("send_file")
        return await real_send(*args, **kwargs)

    async def update_fields(owner_id, code, fields):
        order.append("db_update")
        return await real_update(owner_id, code, fields)

    async def delete_messages(peer, ids):
        order.append("delete_old")
        return await real_delete(peer, ids)

    client.send_file = send_file
    client.delete_messages = delete_messages
    with patch.object(db_client, "update_save_fields", update_fields):
        result = await retrieve_service.do_change_file_name(
            client, OWNER, "S0001", "Renamed.pdf"
        )

    assert result.startswith("✅")
    assert order == ["send_file", "db_update", "delete_old"]
    row = _stored("S0001")
    assert row["saved_msg_id"] == NEW_MSG_ID
    assert row["file_name"] == "Renamed.pdf"
    assert client.deletes[0]["ids"] == [OLD_MSG_ID]


@pytest.mark.asyncio
async def test_a_failed_reupload_preserves_the_original_message_and_row():
    _seed(_row())
    client = FakeSavedClient(_document_message(), send_error=RuntimeError("upload failed"))

    result = await retrieve_service.do_change_file_name(client, OWNER, "S0001", "Renamed.pdf")

    assert result.startswith("❌")
    assert client.deletes == []
    row = _stored("S0001")
    assert row["saved_msg_id"] == OLD_MSG_ID
    assert row["file_name"] == "University_Week_2.pdf"


@pytest.mark.asyncio
async def test_a_failed_download_preserves_the_original_message_and_row():
    _seed(_row())
    client = FakeSavedClient(_document_message(), download_error=RuntimeError("no net"))

    result = await retrieve_service.do_change_file_name(client, OWNER, "S0001", "Renamed.pdf")

    assert result.startswith("❌")
    assert client.uploads == [] and client.deletes == []
    assert _stored("S0001")["file_name"] == "University_Week_2.pdf"


@pytest.mark.asyncio
async def test_a_row_that_cannot_be_confirmed_keeps_the_original_message():
    _seed(_row())
    client = FakeSavedClient(_document_message())

    with patch.object(db_client, "update_save_fields", AsyncMock(return_value=None)):
        result = await retrieve_service.do_change_file_name(
            client, OWNER, "S0001", "Renamed.pdf"
        )

    assert result.startswith("❌")
    assert client.deletes == []
    assert _stored("S0001")["saved_msg_id"] == OLD_MSG_ID
    assert _stored("S0001")["file_name"] == "University_Week_2.pdf"
    # The replacement was deliberately left alone: a failed confirmation read
    # must never destroy a message the row may already point at.
    assert len(client.uploads) == 1


@pytest.mark.asyncio
async def test_a_photo_or_a_document_without_a_file_name_is_refused():
    photo_message = type("Msg", (), {
        "id": OLD_MSG_ID, "message": "cap",
        "media": MessageMediaPhoto(photo=type("P", (), {"id": 1})(), ttl_seconds=None),
    })()
    _seed(_row(media_type="Photo", file_name=None, mime_type="image/jpeg"))
    client = FakeSavedClient(photo_message)

    refused = await retrieve_service.do_change_file_name(client, OWNER, "S0001", "x.jpg")
    assert refused.startswith("❌")
    assert client.uploads == [] and client.deletes == []

    bare_message = type("Msg", (), {
        "id": OLD_MSG_ID, "message": "cap",
        "media": MessageMediaDocument(
            document=FakeDoc(attributes=[DocumentAttributeAudio(duration=3, voice=True)]),
            ttl_seconds=None,
        ),
    })()
    client = FakeSavedClient(bare_message)
    refused = await retrieve_service.do_change_file_name(client, OWNER, "S0001", "x.ogg")
    assert refused.startswith("❌")
    assert client.uploads == []


@pytest.mark.asyncio
async def test_a_file_name_change_reports_when_the_old_copy_could_not_be_removed():
    _seed(_row())
    client = FakeSavedClient(_document_message(), delete_error=RuntimeError("flood"))

    result = await retrieve_service.do_change_file_name(client, OWNER, "S0001", "Renamed.pdf")

    assert result.startswith("✅")
    assert "could not be removed" in result
    assert _stored("S0001")["saved_msg_id"] == NEW_MSG_ID


@pytest.mark.asyncio
async def test_an_unchanged_or_invalid_file_name_changes_nothing():
    _seed(_row())
    client = FakeSavedClient(_document_message())

    same = await retrieve_service.do_change_file_name(
        client, OWNER, "S0001", "University_Week_2.pdf"
    )
    assert same.startswith("⚠️")
    assert client.uploads == []

    for bad in ("", "   ", "a/b.pdf", "a\\b.pdf", "x" * 121):
        refused = await retrieve_service.do_change_file_name(client, OWNER, "S0001", bad)
        assert refused.startswith("⚠️"), bad
    assert client.uploads == []
    assert _stored("S0001")["file_name"] == "University_Week_2.pdf"


@pytest.mark.asyncio
async def test_the_file_name_change_is_owner_scoped():
    _seed(_row(owner_id=OTHER_OWNER))
    client = FakeSavedClient(_document_message())

    foreign = await retrieve_service.do_change_file_name(client, OWNER, "S0001", "Mine.pdf")
    missing = await retrieve_service.do_change_file_name(client, OWNER, "S9999", "Mine.pdf")

    assert foreign == "❌ No item found for `S0001`"
    assert missing == "❌ No item found for `S9999`"
    assert client.downloads == [] and client.uploads == [] and client.deletes == []
    assert _stored("S0001")["file_name"] == "University_Week_2.pdf"


@pytest.mark.asyncio
async def test_a_display_name_rename_stays_metadata_only():
    _seed(_row(display_name="Old"))
    client = FakeSavedClient(_document_message())

    result = await retrieve_service.do_rename(OWNER, "S0001", "Semester Two")

    assert result.startswith("✅")
    row = _stored("S0001")
    assert row["display_name"] == "Semester Two"
    # The Telegram layer is untouched: the display name is not a file name.
    assert row["file_name"] == "University_Week_2.pdf"
    assert row["saved_msg_id"] == OLD_MSG_ID
    assert row["caption"] == _caption()
    assert client.downloads == [] and client.uploads == [] and client.edits == []


# ── AI surface ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_ai_rename_tool_changes_the_actual_file_name():
    _seed(_row(display_name="University Schedule"))
    client = FakeSavedClient(_document_message())
    _registry, ctx, executor = _chain(client)

    result = await _run_tool(
        executor, ctx, "rename_save",
        {"save_code": "S0001", "file_name": "University_Weekly_Schedule_Semester_2.pdf"},
    )

    assert result.success is True
    assert result.data["file_name"] == "University_Weekly_Schedule_Semester_2.pdf"
    assert _stored("S0001")["file_name"] == "University_Weekly_Schedule_Semester_2.pdf"
    # A file-name-only request never renames the item.
    assert _stored("S0001")["display_name"] == "University Schedule"


@pytest.mark.asyncio
async def test_the_ai_rename_tool_reports_both_layers_independently():
    _seed(_row(display_name="Old"))
    client = FakeSavedClient(_document_message(), send_error=RuntimeError("upload failed"))
    _registry, ctx, executor = _chain(client)

    result = await _run_tool(
        executor, ctx, "rename_save",
        {"save_code": "S0001", "display_name": "New", "file_name": "New.pdf"},
    )

    assert result.success is False
    assert "❌" in result.message
    assert "✅" in result.message  # the label half still succeeded
    assert _stored("S0001")["display_name"] == "New"
    assert _stored("S0001")["file_name"] == "University_Week_2.pdf"


@pytest.mark.asyncio
async def test_the_ai_rename_tool_needs_a_name_or_a_file_name():
    _seed(_row())
    client = FakeSavedClient(_document_message())
    _registry, ctx, executor = _chain(client)

    with patch.object(retrieve_service, "do_change_file_name", AsyncMock()) as spy:
        result = await _run_tool(executor, ctx, "rename_save", {"save_code": "S0001"})

    spy.assert_not_awaited()
    assert result.success is False
    assert "file name" in result.message
    assert client.uploads == []


@pytest.mark.asyncio
async def test_the_ai_tag_tool_synchronizes_the_saved_message_caption():
    _seed(_row(tags=["university"]))
    client = FakeSavedClient(_document_message())
    _registry, ctx, executor = _chain(client)

    result = await _run_tool(
        executor, ctx, "update_save_tags",
        {"save_code": "S0001", "tags": ["semester-2"], "mode": "add"},
    )

    assert result.success is True
    assert len(client.edits) == 1
    assert "🏷 Additional tags: #university #semester-2" in client.edits[0]["text"].split("\n")
    assert client.uploads == []  # a tag change never re-uploads


@pytest.mark.asyncio
async def test_the_ai_tools_still_ignore_model_supplied_identity():
    _seed(_row(display_name="Old"))
    client = FakeSavedClient(_document_message())
    _registry, ctx, executor = _chain(client)

    await _run_tool(
        executor, ctx, "rename_save",
        {"save_code": "S0001", "file_name": "New.pdf", "owner_id": OTHER_OWNER},
    )

    assert _stored("S0001")["owner_id"] == OWNER
    assert _stored("S0001")["file_name"] == "New.pdf"


@pytest.mark.asyncio
async def test_an_ambiguous_target_never_reaches_the_telegram_sync():
    _seed(
        _row("S0001", display_name="Schedule"),
        _row("S0002", display_name="Schedule", created_at="2026-09-16T10:00:00+00:00"),
    )
    client = FakeSavedClient(_document_message())
    _registry, ctx, executor = _chain(client)

    result = await _run_tool(
        executor, ctx, "rename_save", {"query": "schedule", "file_name": "New.pdf"}
    )

    assert result.data["outcome"] == "ambiguous"
    assert client.downloads == [] and client.uploads == [] and client.deletes == []


# ── action contract ────────────────────────────────────────────────────────

def test_the_action_contract_carries_a_file_name_only_for_a_rename():
    rename = validate_action(
        {"action": "rename_saved_item", "save_code": "S0001", "file_name": "New.pdf"}
    )
    assert rename.kind == KIND_EXECUTABLE
    assert rename.file_name == "New.pdf" and rename.display_name == ""

    both = validate_action(
        {"action": "rename_saved_item", "save_code": "S0001",
         "display_name": "New", "file_name": "New.pdf"}
    )
    assert both.kind == KIND_EXECUTABLE
    assert both.display_name == "New" and both.file_name == "New.pdf"

    empty = validate_action({"action": "rename_saved_item", "save_code": "S0001"})
    assert empty.kind == KIND_INVALID
    assert "file_name" in empty.error


@pytest.mark.parametrize("payload", [
    {"action": "update_saved_item_tags", "save_code": "S0001", "tags": ["a"],
     "mode": "add", "file_name": "New.pdf"},
    {"action": "save", "target": "replied_message", "file_name": "New.pdf"},
    {"action": "delete_saved_item", "save_code": "S0001", "file_name": "New.pdf"},
])
def test_unrelated_actions_cannot_carry_a_file_name(payload):
    result = validate_action(payload)
    assert result.kind == KIND_INVALID
    assert "file_name" in result.error


def test_a_file_name_rename_resolves_to_the_registered_tool():
    from backend.ai.actions import parse_action_text

    parsed = parse_action_text(
        '{"action": "rename_saved_item", "save_code": "s0001", "file_name": "New.pdf"}'
    )
    assert parsed.kind == "executable"
    assert parsed.tool_calls == [{
        "name": "rename_save",
        "arguments": {"file_name": "New.pdf", "save_code": "S0001"},
    }]

    label_only = parse_action_text(
        '{"action": "rename_saved_item", "save_code": "s0001", "display_name": "Semester Two"}'
    )
    assert label_only.tool_calls == [{
        "name": "rename_save",
        "arguments": {"display_name": "Semester Two", "save_code": "S0001"},
    }]


# ── manual panel ───────────────────────────────────────────────────────────

@pytest.fixture
def engine(monkeypatch):
    from backend.helper import inline_engine

    client = FakeSavedClient(_document_message())
    client.send_message = AsyncMock()
    client.delete_messages = AsyncMock(side_effect=client.delete_messages)
    monkeypatch.setattr(inline_engine, "_self_client", client, raising=False)
    monkeypatch.setattr(inline_engine, "_owner_id", OWNER, raising=False)
    return client


@pytest.fixture
def helper_client(monkeypatch):
    from backend.bot.handlers import retrieve as handler

    helper = MagicMock()
    helper.edit_message = AsyncMock()
    monkeypatch.setattr(handler, "get_client", lambda: helper)
    return helper


def _edited_body(helper) -> str:
    bodies = [
        call.args[2] if len(call.args) > 2 else ""
        for call in helper.edit_message.await_args_list
    ]
    assert bodies, "nothing was rendered in place"
    return bodies[-1]


@pytest.mark.asyncio
async def test_the_item_panel_shows_the_current_file_name_and_offers_the_edit(engine):
    from backend.bot.handlers import retrieve as handler

    _seed(_row())

    _title, _body, buttons = await handler._retrieve_item_panel_handler(None, "id:S0001")

    data = [
        b.data.decode() for row in buttons for b in row
        if getattr(b, "data", None) is not None
    ]
    assert "input:retrieve_item:filename:S0001" in data
    labels = [getattr(b, "text", "") for row in buttons for b in row]
    # The row shows the CURRENT Telegram file name (truncated when long).
    assert any("University_Week" in label for label in labels)


@pytest.mark.asyncio
async def test_the_manual_file_name_input_changes_the_actual_file_name(engine, helper_client):
    from backend.bot.handlers import retrieve as handler

    _seed(_row())

    await handler._retrieve_filename_input_handler(
        "Semester_2_Schedule.pdf", CHAT, 42, -100, 7, extra="S0001"
    )

    assert _stored("S0001")["file_name"] == "Semester_2_Schedule.pdf"
    assert "File name for `S0001`" in _edited_body(helper_client)
    assert engine.uploads[0]["attributes"]


@pytest.mark.asyncio
async def test_the_manual_tag_input_synchronizes_the_saved_caption(engine, helper_client):
    from backend.bot.handlers import retrieve as handler

    _seed(_row(tags=["university"]))

    await handler._retrieve_tags_input_handler(
        "archive, semester-2", CHAT, 42, -100, 7, extra="S0001"
    )

    assert retrieve_service._owner_tags(_stored("S0001")) == ("archive", "semester-2")
    assert "🏷 Additional tags: #archive #semester-2" in engine.edits[-1]["text"].split("\n")
    assert engine.uploads == []


@pytest.mark.asyncio
async def test_the_manual_file_name_input_without_an_item_reports_it(engine, helper_client):
    from backend.bot.handlers import retrieve as handler

    await handler._retrieve_filename_input_handler("x.pdf", CHAT, 42, -100, 7, extra="")

    assert "No item selected" in _edited_body(helper_client)
    assert engine.uploads == []


# ── regressions ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_resolver_contract_is_unchanged_by_the_sync():
    _seed(_row("S0001", display_name="University Schedule", tags=["university"]))

    by_name = await retrieve_service.resolve_saved_items(OWNER, "university schedule")
    by_tag = await retrieve_service.resolve_saved_items(OWNER, "university")
    by_code = await retrieve_service.resolve_saved_items(OWNER, "s0001")

    assert by_name.status == by_tag.status == by_code.status == retrieve_service.RESOLUTION_UNIQUE
    assert by_name.candidates[0].save_code == "S0001"
    assert by_tag.candidates[0].save_code == "S0001"
    assert by_code.candidates[0].save_code == "S0001"


@pytest.mark.asyncio
async def test_the_save_code_and_stored_row_identity_survive_both_syncs():
    _seed(_row(tags=["university"]))
    client = FakeSavedClient(_document_message())

    code_before = _stored("S0001")["save_code"]
    row_id_before = _stored("S0001")["id"]
    await retrieve_service.do_edit_tags(
        OWNER, "S0001", retrieve_service.TAG_OP_ADD, ["semester-2"], client=client
    )
    await retrieve_service.do_change_file_name(client, OWNER, "S0001", "Renamed.pdf")

    row = _stored("S0001")
    assert row["save_code"] == code_before
    assert row["id"] == row_id_before
    assert row["origin_chat_id"] == -1009999 and row["origin_msg_id"] == 4321
    assert retrieve_service._owner_tags(row) == ("university", "semester-2")
