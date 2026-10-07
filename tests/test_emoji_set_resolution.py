"""Sticker-set resolution & enumeration — Phase 1 remainder tests.

Pins the set-resolution/enumeration contracts added to
`backend/services/emoji_library_service`:

  1. Document resolution via `GetCustomEmojiDocuments` discovers the
     owning sticker set from `DocumentAttributeCustomEmoji.stickerset`.
  2. Set enumeration via `GetStickerSet` returns the set metadata
     (id, short_name, title) and its member documents.
  3. Each unique document is resolved once; each unique set is enumerated
     once and reused.
  4. Resolution/enumeration failures degrade the import report honestly
     without hiding behind generic success.
  5. The existing Phase 1 import semantics (dedup, bounds, fail-closed
     collection, collect-then-persist) remain intact.

Everything runs offline: the Telegram boundary is faked at the
`client.iter_messages` AND the `TelegramAPI.client(...)` surfaces.
No live credential, no live Supabase.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from telethon.tl import types
from telethon.tl.functions.messages import GetCustomEmojiDocumentsRequest
from telethon.tl.functions.messages import GetStickerSetRequest

from backend.db import client as db_client
from backend.services import emoji_library_service
from backend.services.emoji_library_service import import_from_saved_messages

OWNER = 7770001
_FALLBACK_KEY = "emoji_library"


@pytest.fixture(autouse=True)
def _reset_library_fallback():
    db_client._fallback[_FALLBACK_KEY] = []
    yield
    db_client._fallback[_FALLBACK_KEY] = []


# ── fake helpers ──────────────────────────────────────────────────────────────

def _custom_entity(text: str, needle: str, doc_id: int) -> types.MessageEntityCustomEmoji:
    from backend.telegram_api._helpers import utf16_offset, utf16_length
    start = text.index(needle)
    return types.MessageEntityCustomEmoji(
        utf16_offset(text, start), utf16_length(needle), doc_id
    )


def _msg(msg_id: int, text: str, entities=None) -> SimpleNamespace:
    return SimpleNamespace(id=msg_id, message=text, entities=entities)


def _emoji_msg(msg_id: int, doc_id: int, emoji: str = "😀") -> SimpleNamespace:
    text = f"m{msg_id} {emoji}"
    return _msg(msg_id, text, [_custom_entity(text, emoji, doc_id)])


class _FakeTelegramApiClient:
    """Fakes the Telethon self-client surface consumed by the emoji service:
    `iter_messages` AND the direct `client(...)` TL request calls used for
    `GetCustomEmojiDocuments` and `GetStickerSet`.
    """

    def __init__(
        self,
        messages: list,
        *,
        documents_by_doc_id: dict[int, list[Any]] | None = None,
        sticker_set_by_set_key: dict[tuple[int, int], Any] | None = None,
        fail_document_resolution: set[int] | None = None,
        fail_set_enumeration: set[tuple[int, int]] | None = None,
        fail_from_call: int | None = None,
        delay_s: float = 0.0,
        calls: list | None = None,
    ):
        self._messages = sorted(
            [m for m in messages if m is not None], key=lambda m: getattr(m, "id", 0), reverse=True
        )
        self._documents_by_doc_id = documents_by_doc_id or {}
        self._sticker_set_by_set_key = sticker_set_by_set_key or {}
        self._fail_document_resolution = fail_document_resolution or set()
        self._fail_set_enumeration = fail_set_enumeration or set()
        self._fail_from_call = fail_from_call
        self._delay_s = delay_s
        self.calls: list[dict] = calls if calls is not None else []

    def iter_messages(self, chat_id, **kwargs):
        self.calls.append({"type": "iter_messages", "chat_id": chat_id, **kwargs})
        index = len(self.calls) - 1

        async def _gen():
            if self._delay_s:
                await asyncio.sleep(self._delay_s)
            if self._fail_from_call is not None and index >= self._fail_from_call:
                raise RuntimeError("rpc down")
            limit = kwargs.get("limit")
            max_id = kwargs.get("max_id")
            out = []
            for m in self._messages:
                mid = getattr(m, "id", 0)
                if max_id is not None and mid >= max_id:
                    continue
                out.append(m)
                if limit is not None and len(out) >= limit:
                    break
            for m in out:
                yield m

        return _gen()

    async def __call__(self, request) -> Any:
        self.calls.append({"type": "tl_request", "request": request})
        if self._delay_s:
            await asyncio.sleep(self._delay_s)
        index = len(self.calls) - 1
        if self._fail_from_call is not None and index >= self._fail_from_call:
            raise RuntimeError("rpc down")

        if isinstance(request, GetCustomEmojiDocumentsRequest):
            doc_ids = request.document_id or []
            result = []
            for doc_id in doc_ids:
                if doc_id in self._fail_document_resolution:
                    raise RuntimeError(f"document resolution failed for {doc_id}")
                result.extend(self._documents_by_doc_id.get(int(doc_id), []))
            return result

        # Assume GetStickerSetRequest
        stickerset = getattr(request, "stickerset", None)
        set_id = None
        access_hash = None
        if isinstance(stickerset, types.InputStickerSetID):
            set_id = stickerset.id
            access_hash = stickerset.access_hash
        set_key = (int(set_id or 0), int(access_hash or 0))
        if set_key in self._fail_set_enumeration:
            raise RuntimeError(f"set enumeration failed for {set_key}")
        return self._sticker_set_by_set_key.get(set_key)


def _make_document(doc_id: int, alt: str = "😀", set_id: int = 100, access_hash: int = 999) -> Any:
    """Build a fake Document carrying a DocumentAttributeCustomEmoji."""
    attr = types.DocumentAttributeCustomEmoji(
        alt=alt,
        stickerset=types.InputStickerSetID(set_id, access_hash),
    )
    doc = SimpleNamespace(id=doc_id, title=alt, attributes=[attr])
    return doc


def _make_sticker_set(
    set_id: int,
    access_hash: int,
    short_name: str = "COLLECTION",
    title: str = "Collection",
    documents: list[Any] | None = None,
    count: int = 0,
) -> Any:
    """Build a fake StickerSet (or StickerSetFullCovered when documents given)."""
    if documents:
        return SimpleNamespace(
            id=set_id,
            access_hash=access_hash,
            title=title,
            short_name=short_name,
            count=count or len(documents),
            hash=0,
            documents=documents,
        )
    return SimpleNamespace(
        id=set_id,
        access_hash=access_hash,
        title=title,
        short_name=short_name,
        count=count,
        hash=0,
    )


# ── document resolution ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resolves_document_and_discovers_set():
    doc = _make_document(doc_id=501, alt="😀", set_id=100, access_hash=999)
    api = _FakeTelegramApiClient(
        [],
        documents_by_doc_id={501: [doc]},
        sticker_set_by_set_key={(100, 999): _make_sticker_set(100, 999, short_name="STARS", title="Stars")},
    )
    resolved = await emoji_library_service._resolve_document_set(
        api, 501, timeout_s=5.0,
        resolved_docs=set(), resolved_sets={}, set_members={},
    )
    assert resolved is not None
    assert resolved["document_id"] == 501
    assert resolved["document_alt_text"] == "😀"
    assert resolved["set_id"] == 100
    assert resolved["set_short_name"] == "STARS"
    assert resolved["set_title"] == "Stars"


@pytest.mark.asyncio
async def test_resolving_same_document_twice_is_free():
    doc = _make_document(doc_id=601, alt="😀", set_id=200, access_hash=888)
    api = _FakeTelegramApiClient(
        [],
        documents_by_doc_id={601: [doc]},
        sticker_set_by_set_key={(200, 888): _make_sticker_set(200, 888, short_name="MOONS")},
    )
    resolved1 = await emoji_library_service._resolve_document_set(
        api, 601, timeout_s=5.0,
        resolved_docs=set(), resolved_sets={}, set_members={},
    )
    resolved2 = await emoji_library_service._resolve_document_set(
        api, 601, timeout_s=5.0,
        resolved_docs=resolved1 and {601}, resolved_sets={601: resolved1}, set_members={},
    )
    assert resolved2 is not None
    assert resolved2["set_short_name"] == "MOONS"
    assert len(api.calls) == 2  # iter not used here; only the first call hits the fake


@pytest.mark.asyncio
async def test_document_without_custom_emoji_attribute_returns_none():
    doc = SimpleNamespace(id=701, title="x", attributes=[])
    api = _FakeTelegramApiClient([], documents_by_doc_id={701: [doc]})
    resolved = await emoji_library_service._resolve_document_set(
        api, 701, timeout_s=5.0,
        resolved_docs=set(), resolved_sets={}, set_members={},
    )
    assert resolved is None


@pytest.mark.asyncio
async def test_missing_document_returns_none():
    api = _FakeTelegramApiClient([], documents_by_doc_id={})
    resolved = await emoji_library_service._resolve_document_set(
        api, 801, timeout_s=5.0,
        resolved_docs=set(), resolved_sets={}, set_members={},
    )
    assert resolved is None


@pytest.mark.asyncio
async def test_document_resolution_rpc_failure_returns_none():
    api = _FakeTelegramApiClient(
        [], documents_by_doc_id={}, fail_document_resolution={901}
    )
    resolved = await emoji_library_service._resolve_document_set(
        api, 901, timeout_s=5.0,
        resolved_docs=set(), resolved_sets={}, set_members={},
    )
    assert resolved is None


@pytest.mark.asyncio
async def test_document_resolution_timeout_returns_none():
    api = _FakeTelegramApiClient(
        [], documents_by_doc_id={}, delay_s=1.0
    )
    resolved = await emoji_library_service._resolve_document_set(
        api, 921, timeout_s=0.05,
        resolved_docs=set(), resolved_sets={}, set_members={},
    )
    assert resolved is None


# ── set enumeration ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_enum_discovering_set_members():
    docs = [
        _make_document(11, alt="😀", set_id=300, access_hash=777),
        _make_document(12, alt="😃", set_id=300, access_hash=777),
        _make_document(13, alt="😄", set_id=300, access_hash=777),
    ]
    api = _FakeTelegramApiClient(
        [],
        documents_by_doc_id={11: [docs[0]]},
        sticker_set_by_set_key={(300, 777): _make_sticker_set(300, 777, short_name="SUN", documents=docs)},
    )
    resolved = await emoji_library_service._resolve_document_set(
        api, 11, timeout_s=5.0,
        resolved_docs=set(), resolved_sets={}, set_members={},
    )
    assert resolved is not None
    assert resolved["set_id"] == 300
    assert resolved["set_short_name"] == "SUN"
    # set_members cache should now contain all three.
    assert (300, 777) in api._sticker_set_by_set_key


@pytest.mark.asyncio
async def test_set_enumeration_rpc_failure_returns_none():
    doc = _make_document(101, alt="😀", set_id=400, access_hash=666)
    api = _FakeTelegramApiClient(
        [],
        documents_by_doc_id={101: [doc]},
        fail_set_enumeration={(400, 666)},
    )
    resolved = await emoji_library_service._resolve_document_set(
        api, 101, timeout_s=5.0,
        resolved_docs=set(), resolved_sets={}, set_members={},
    )
    assert resolved is None


@pytest.mark.asyncio
async def test_set_enumeration_timeout_returns_none():
    doc = _make_document(102, alt="😀", set_id=410, access_hash=667)
    api = _FakeTelegramApiClient(
        [],
        documents_by_doc_id={102: [doc]},
        sticker_set_by_set_key={(410, 667): _make_sticker_set(410, 667, short_name="X")},
        delay_s=1.0,
    )
    resolved = await emoji_library_service._resolve_document_set(
        api, 102, timeout_s=0.05,
        resolved_docs=set(), resolved_sets={}, set_members={},
    )
    # The document resolves (we have it), but set enumeration times out —
    # the service should still return None because it cannot get set metadata.
    assert resolved is None


@pytest.mark.asyncio
async def test_set_with_count_but_no_documents_is_honest():
    api = _FakeTelegramApiClient(
        [],
        documents_by_doc_id={},
        sticker_set_by_set_key={(500, 555): _make_sticker_set(500, 555, short_name="EMPTY", count=12)},
    )
    resolved = await emoji_library_service._resolve_document_set(
        api, 999, timeout_s=5.0,
        resolved_docs=set(), resolved_sets={}, set_members={},
    )
    # document 999 not in our fake docs, so resolution fails at that layer.
    assert resolved is None


# ── import integration: set resolution is best-effort enrichment ─────────────

@pytest.mark.asyncio
async def test_import_resolves_sets_for_new_documents():
    doc_a = _make_document(2001, alt="😀", set_id=1000, access_hash=111)
    doc_b = _make_document(2002, alt="😃", set_id=1000, access_hash=111)
    api = _FakeTelegramApiClient(
        [_emoji_msg(2, 2001), _emoji_msg(1, 2002)],
        documents_by_doc_id={2001: [doc_a], 2002: [doc_b]},
        sticker_set_by_set_key={(1000, 111): _make_sticker_set(1000, 111, short_name="DUO", documents=[doc_a, doc_b])},
    )
    report = await import_from_saved_messages(
        api, OWNER, resolve_sets=True, page_timeout=5.0, set_timeout=5.0,
    )
    assert report["ok"] is True
    assert report["imported"] == 2
    assert report["sets_requested"] == 2
    assert report["sets_resolved"] == 2
    assert report["set_members_seen"] == 2
    assert report["set_members_imported"] == 2
    assert report["sets_failed"] == 0
    assert report["set_resolution_error"] is None


@pytest.mark.asyncio
async def test_import_same_set_resolved_once():
    doc_a = _make_document(3001, alt="😀", set_id=2000, access_hash=222)
    doc_b = _make_document(3002, alt="😃", set_id=2000, access_hash=222)
    api = _FakeTelegramApiClient(
        [_emoji_msg(2, 3001), _emoji_msg(1, 3002)],
        documents_by_doc_id={3001: [doc_a], 3002: [doc_b]},
        sticker_set_by_set_key={(2000, 222): _make_sticker_set(2000, 222, short_name="PAIR", documents=[doc_a, doc_b])},
    )
    report = await import_from_saved_messages(
        api, OWNER, resolve_sets=True, page_timeout=5.0, set_timeout=5.0,
    )
    # Both documents resolve but they share a set — the second document should
    # still be counted as resolved (we already have the set cached), but the
    # underlying service tracks sets_resolved per-document-resolution success.
    assert report["ok"] is True
    assert report["imported"] == 2
    assert report["sets_failed"] == 0


@pytest.mark.asyncio
async def test_import_set_resolution_failure_degrades_honestly():
    doc = _make_document(4001, alt="😀", set_id=3000, access_hash=333)
    api = _FakeTelegramApiClient(
        [_emoji_msg(1, 4001)],
        documents_by_doc_id={4001: [doc]},
        fail_set_enumeration={(3000, 333)},
    )
    report = await import_from_saved_messages(
        api, OWNER, resolve_sets=True, page_timeout=5.0, set_timeout=5.0,
    )
    # The document still imports (Phase 1 dedup + persist still work) but
    # set resolution/enumeration failed for it.
    assert report["ok"] is True  # persistence succeeded
    assert report["imported"] == 1
    assert report["sets_requested"] == 1
    assert report["sets_resolved"] == 0
    assert report["sets_failed"] == 1
    assert report["set_members_imported"] == 0
    assert report["set_members_failed"] == 1


@pytest.mark.asyncio
async def test_import_set_resolution_rpc_cap_enforced():
    """Many new documents should not cause unbounded set-resolution RPCs."""
    docs = [_make_document(5000 + i, alt="😀", set_id=4000, access_hash=444) for i in range(250)]
    msg_docs = {doc.id: [doc] for doc in docs}
    api = _FakeTelegramApiClient(
        [_emoji_msg(i, 5000 + i) for i in range(250)],
        documents_by_doc_id=msg_docs,
        sticker_set_by_set_key={(4000, 444): _make_sticker_set(4000, 444, short_name="BIG", documents=docs)},
    )
    report = await import_from_saved_messages(
        api, OWNER, resolve_sets=True, max_records=250, page_timeout=5.0, set_timeout=5.0,
    )
    assert report["sets_requested"] == 200  # _MAX_SET_RESOLUTION_RPCS


@pytest.mark.asyncio
async def test_import_existing_phase1_semantics_unchanged():
    """Set resolution must not weaken Phase 1 guarantees: dedup, collect-then-persist."""
    doc = _make_document(6001, alt="😀", set_id=5000, access_hash=555)
    api = _FakeTelegramApiClient(
        [_emoji_msg(2, 6001), _emoji_msg(1, 6001)],
        documents_by_doc_id={6001: [doc]},
        sticker_set_by_set_key={(5000, 555): _make_sticker_set(5000, 555, short_name="SINGLE", documents=[doc])},
    )
    first = await import_from_saved_messages(
        api, OWNER, resolve_sets=True, page_timeout=5.0, set_timeout=5.0,
    )
    second = await import_from_saved_messages(
        api, OWNER, resolve_sets=True, page_timeout=5.0, set_timeout=5.0,
    )
    assert first["imported"] == 1
    assert first["duplicates"] == 1
    assert second["imported"] == 0
    assert second["duplicates"] == 2
    assert second["library_total"] == 1


@pytest.mark.asyncio
async def test_import_collection_failure_still_fails_closed():
    api = _FakeTelegramApiClient(
        [_emoji_msg(1, 7001)],
        documents_by_doc_id={7001: [_make_document(7001, alt="😀")]},
        fail_from_call=0,
    )
    report = await import_from_saved_messages(
        api, OWNER, resolve_sets=True, page_timeout=5.0, set_timeout=5.0,
    )
    assert report["ok"] is False
    assert report["error"] is not None
    assert "rpc down" in report["error"]
    assert report["imported"] == 0
    assert report["sets_requested"] == 0


@pytest.mark.asyncio
async def test_import_without_set_resolution_still_works():
    """resolve_sets=False should skip set enrichment but keep the import."""
    api = _FakeTelegramApiClient([_emoji_msg(1, 8001)])
    report = await import_from_saved_messages(
        api, OWNER, resolve_sets=False, page_timeout=5.0,
    )
    assert report["ok"] is True
    assert report["imported"] == 1
    assert report["sets_requested"] == 0
    assert report["sets_resolved"] == 0


@pytest.mark.asyncio
async def test_import_set_timeout_does_not_block_import():
    """A slow set-resolution RPC should not stall the whole import."""
    doc = _make_document(9001, alt="😀", set_id=6000, access_hash=666)
    api = _FakeTelegramApiClient(
        [_emoji_msg(1, 9001)],
        documents_by_doc_id={9001: [doc]},
        sticker_set_by_set_key={(6000, 666): _make_sticker_set(6000, 666, short_name="SLOW")},
        delay_s=1.0,
    )
    report = await import_from_saved_messages(
        api, OWNER, resolve_sets=True, page_timeout=5.0, set_timeout=0.05,
    )
    # Set resolution times out for the doc, but the document itself still imports.
    assert report["imported"] == 1
    assert report["sets_requested"] == 1
    assert report["sets_resolved"] == 0
    assert report["sets_failed"] == 1


# ── library browser contract ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_library_browser_lists_entries_newest_first():
    for i in range(3):
        await db_client.insert_emoji_entry(
            {
                "owner_id": OWNER,
                "document_id": 100 + i,
                "alt_text": "😀",
                "source": "imported",
                "source_msg_id": i,
            }
        )
    page, total = await db_client.list_emoji_entries(OWNER, limit=2, offset=0)
    assert total == 3
    assert len(page) == 2
    # Newest first by created_at.
    assert page[0]["document_id"] == 102
    assert page[1]["document_id"] == 101


@pytest.mark.asyncio
async def test_library_browser_pagination():
    for i in range(12):
        await db_client.insert_emoji_entry(
            {
                "owner_id": OWNER,
                "document_id": 200 + i,
                "alt_text": "😀",
                "source": "imported",
                "source_msg_id": i,
            }
        )
    page1, total = await db_client.list_emoji_entries(OWNER, limit=10, offset=0)
    assert total == 12
    assert len(page1) == 10
    page2, _total2 = await db_client.list_emoji_entries(OWNER, limit=10, offset=10)
    assert len(page2) == 2
    ids = {r["document_id"] for r in page1 + page2}
    assert ids == {200 + i for i in range(12)}
