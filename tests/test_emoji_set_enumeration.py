"""Sticker-set resolution / enumeration — Phase 1 remainder (Emoji & Reaction).

Pins the enrichment contracts added on top of the message-level Phase 1
slice:

  1. Facade (``backend.telegram_api.custom_emoji``): document ids resolve to
     ``{document_id, alt, set}`` through the REAL Telethon TL surface —
     ``messages.GetCustomEmojiDocumentsRequest`` and
     ``messages.GetStickerSetRequest`` — with honest absence (a document
     Telegram did not return is simply missing; one without a custom-emoji
     attribute or a usable set identity contributes nothing and no metadata
     is ever fabricated) and normalized errors (TelegramAPIError /
     TelegramTimeoutError).
  2. Service enrichment (``_enumerate_sets``): best-effort, bounded and
     deterministic — each unique candidate document resolved once (chunked
     RPCs), each unique set enumerated exactly once, duplicates counted and
     never persisted twice, explicit set/set-member budgets, and every
     failure reported (``error``) instead of raised.
  3. Import integration: the full ``import_from_saved_messages`` run persists
     message-level records AND deduplicated set members, reports
     ``degraded``/``set_error`` honestly, and keeps the existing
     collect-then-persist / fail-closed semantics intact.

Everything runs offline: the Telegram boundary is faked at the
``client(...)`` TL-request surface with real Telethon TL types.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from telethon.tl.functions.messages import (
    GetCustomEmojiDocumentsRequest,
    GetStickerSetRequest,
)
from telethon.tl.types import (
    DocumentAttributeCustomEmoji,
    InputStickerSetID,
    InputStickerSetShortName,
)

from backend.db import client as db_client
from backend.services import emoji_library_service
from backend.services.emoji_library_service import (
    MAX_SET_MEMBERS_PER_SET,
    MAX_SETS_PER_IMPORT,
    _enumerate_sets,
    import_from_saved_messages,
)
from backend.telegram_api import custom_emoji as facade
from backend.telegram_api.custom_emoji import (
    MAX_DOCUMENTS_PER_CALL,
    get_custom_emoji_documents,
    get_sticker_set,
)
from backend.telegram_api.exceptions import TelegramAPIError, TelegramTimeoutError

OWNER = 7770001
SET_A = InputStickerSetID(id=11, access_hash=1100)
SET_B = InputStickerSetID(id=22, access_hash=2200)

_FALLBACK_KEY = "emoji_library"


@pytest.fixture(autouse=True)
def _reset_library_fallback():
    db_client._fallback[_FALLBACK_KEY] = []
    yield
    db_client._fallback[_FALLBACK_KEY] = []


# ── fake Telegram TL boundary ─────────────────────────────────────────────────


def _doc(doc_id: int, alt: str = "😀", stickerset: Any = SET_A):
    return SimpleNamespace(
        id=doc_id,
        attributes=[DocumentAttributeCustomEmoji(alt=alt, stickerset=stickerset)],
    )


class _FakeTlClient:
    """Fakes the self client at the ``client(request)`` TL surface plus
    ``iter_messages`` for the Saved Messages scan."""

    def __init__(
        self,
        messages: list | None = None,
        documents: list | None = None,
        sets: dict[Any, Any] | None = None,
        *,
        docs_error: Exception | None = None,
        set_errors: dict[Any, Exception] | None = None,
        docs_timeout: bool = False,
        fail_from_call: int | None = None,
    ):
        self._messages = sorted(messages or [], key=lambda m: getattr(m, "id", 0), reverse=True)
        self._documents = documents or []
        self._sets = sets or {}
        self._docs_error = docs_error
        self._set_errors = set_errors or {}
        self._docs_timeout = docs_timeout
        self._fail_from_call = fail_from_call
        self.docs_calls: list[list[int]] = []
        self.set_calls: list[Any] = []

    def iter_messages(self, chat_id, **kwargs):
        async def _gen():
            if self._fail_from_call is not None:
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

    async def __call__(self, request):
        if isinstance(request, GetCustomEmojiDocumentsRequest):
            self.docs_calls.append(list(request.document_id))
            if self._docs_timeout:
                raise asyncio.TimeoutError()
            if self._docs_error is not None:
                raise self._docs_error
            wanted = set(request.document_id)
            return [d for d in self._documents if d.id in wanted]
        if isinstance(request, GetStickerSetRequest):
            ref = request.stickerset
            key = getattr(ref, "short_name", None) or (
                ref.id, ref.access_hash
            )
            self.set_calls.append(key)
            if key in self._set_errors:
                raise self._set_errors[key]
            result = self._sets.get(key)
            if result is None:
                raise TelegramAPIError("SET_NOT_FOUND")
            return result
        raise AssertionError(f"unexpected TL request: {type(request).__name__}")


def _set_result(set_id: int, short_name: str, members: list):
    return SimpleNamespace(
        set=SimpleNamespace(
            id=set_id, access_hash=set_id * 100, title=short_name, short_name=short_name, count=len(members)
        ),
        documents=members,
    )


def _msg(msg_id: int, text: str, entities: list):
    return SimpleNamespace(id=msg_id, message=text, entities=entities)


def _custom_entity(text: str, needle: str, doc_id: int):
    from backend.telegram_api._helpers import utf16_length, utf16_offset

    from telethon.tl.types import MessageEntityCustomEmoji

    return MessageEntityCustomEmoji(
        utf16_offset(text, text.index(needle)), utf16_length(needle), doc_id
    )


def _emoji_message(msg_id: int, doc_id: int, emoji: str = "😀"):
    text = f"m{msg_id} {emoji}"
    return _msg(msg_id, text, [_custom_entity(text, emoji, doc_id)])


# ── A. facade: document resolution ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_facade_resolves_custom_emoji_document_with_set_identity():
    client = _FakeTlClient(documents=[_doc(555001, "😀", SET_A)])
    result = await get_custom_emoji_documents(client, [555001])
    assert result == [{"document_id": 555001, "alt": "😀", "set": {"kind": "id", "id": 11, "access_hash": 1100}}]
    assert client.docs_calls == [[555001]]


@pytest.mark.asyncio
async def test_facade_short_name_set_identity_is_preserved():
    client = _FakeTlClient(documents=[_doc(7001, "x", InputStickerSetShortName(short_name="cats"))])
    result = await get_custom_emoji_documents(client, [7001])
    assert result[0]["set"] == {"kind": "short_name", "short_name": "cats"}


@pytest.mark.asyncio
async def test_facade_absent_document_is_simply_missing():
    client = _FakeTlClient(documents=[])
    result = await get_custom_emoji_documents(client, [9999])
    assert result == []


@pytest.mark.asyncio
async def test_facade_document_without_custom_emoji_attribute_is_not_a_custom_emoji():
    sticker_doc = SimpleNamespace(id=8001, attributes=[])
    client = _FakeTlClient(documents=[sticker_doc])
    result = await get_custom_emoji_documents(client, [8001])
    assert result == []


@pytest.mark.asyncio
async def test_facade_document_with_unusable_set_identity_reports_none():
    client = _FakeTlClient(documents=[_doc(8101, "y", None)])
    result = await get_custom_emoji_documents(client, [8101])
    assert result == [{"document_id": 8101, "alt": "y", "set": None}]


@pytest.mark.asyncio
async def test_facade_invalid_ids_are_dropped_and_clamped_to_the_call_bound():
    client = _FakeTlClient(documents=[_doc(i) for i in range(1, MAX_DOCUMENTS_PER_CALL + 10)])
    ids = [0, -5, "abc", None, *range(1, MAX_DOCUMENTS_PER_CALL + 10)]
    result = await get_custom_emoji_documents(client, ids)
    assert len(client.docs_calls) == 1
    assert len(client.docs_calls[0]) == MAX_DOCUMENTS_PER_CALL
    assert len(result) == MAX_DOCUMENTS_PER_CALL


@pytest.mark.asyncio
async def test_facade_api_error_is_normalized():
    client = _FakeTlClient(docs_error=RuntimeError("FLOOD_WAIT_X"))
    with pytest.raises(TelegramAPIError):
        await get_custom_emoji_documents(client, [1])


@pytest.mark.asyncio
async def test_facade_timeout_is_normalized():
    client = _FakeTlClient(docs_timeout=True)
    with pytest.raises(TelegramTimeoutError):
        await get_custom_emoji_documents(client, [1])


@pytest.mark.asyncio
async def test_facade_get_sticker_set_returns_bounded_member_dicts():
    client = _FakeTlClient(
        sets={(11, 1100): _set_result(11, "cats", [_doc(1, "a"), _doc(2, "b")])}
    )
    info = await get_sticker_set(client, {"kind": "id", "id": 11, "access_hash": 1100})
    assert info["short_name"] == "cats"
    assert info["members"] == [
        {"document_id": 1, "alt": "a"},
        {"document_id": 2, "alt": "b"},
    ]
    assert client.set_calls == [(11, 1100)]


@pytest.mark.asyncio
async def test_facade_get_sticker_set_rejects_unusable_identity():
    client = _FakeTlClient()
    with pytest.raises(TelegramAPIError):
        await get_sticker_set(client, {"kind": "weird"})
    with pytest.raises(TelegramAPIError):
        await get_sticker_set(client, "not-a-dict")
    assert client.set_calls == []


@pytest.mark.asyncio
async def test_facade_get_sticker_set_failure_is_normalized():
    client = _FakeTlClient(set_errors={(11, 1100): RuntimeError("SET_ID_INVALID")})
    with pytest.raises(TelegramAPIError):
        await get_sticker_set(client, {"kind": "id", "id": 11, "access_hash": 1100})


# ── B. service: bounded set enumeration ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_enumeration_collects_new_members_as_library_records():
    client = _FakeTlClient(
        documents=[_doc(5001, "😀", SET_A)],
        sets={(11, 1100): _set_result(11, "cats", [_doc(1, "a"), _doc(2, "b")])},
    )
    candidates = {5001: {"document_id": 5001, "alt_text": "😀", "source_msg_id": 1, "source": "imported"}}
    result = await _enumerate_sets(client, candidates, set(), 5.0, 100)
    assert result["error"] is None
    assert result["documents_resolved"] == 1
    assert result["sets_resolved"] == 1
    assert result["set_members_seen"] == 2
    assert sorted(result["set_candidates"]) == [1, 2]
    assert result["set_candidates"][1] == {
        "document_id": 1,
        "alt_text": "a",
        "source_msg_id": None,
        "source": "imported",
    }


@pytest.mark.asyncio
async def test_enumeration_resolves_each_set_once_across_documents():
    client = _FakeTlClient(
        documents=[_doc(6001, "a", SET_A), _doc(6002, "b", SET_A), _doc(6003, "c", SET_A)],
        sets={(11, 1100): _set_result(11, "cats", [_doc(1)])},
    )
    candidates = {6001: {}, 6002: {}, 6003: {}}
    await _enumerate_sets(client, candidates, set(), 5.0, 100)
    assert len(client.docs_calls) == 1  # chunked into one batch
    assert client.set_calls == [(11, 1100)]  # the shared set enumerated exactly once


@pytest.mark.asyncio
async def test_enumeration_counts_duplicates_across_scan_library_and_members():
    client = _FakeTlClient(
        documents=[_doc(7001, "a", SET_A)],
        sets={(11, 1100): _set_result(
            11, "cats", [_doc(1), _doc(1), _doc(2), _doc(7001)]
        )},
    )
    candidates = {7001: {"document_id": 7001}}
    existing = {2}
    result = await _enumerate_sets(client, candidates, existing, 5.0, 100)
    assert result["set_members_seen"] == 4
    assert result["set_duplicates"] == 3  # repeated member + library hit + scan candidate
    assert sorted(result["set_candidates"]) == [1]


@pytest.mark.asyncio
async def test_enumeration_reports_unresolved_and_setless_documents():
    client = _FakeTlClient(
        documents=[_doc(7101, "a", SET_A), _doc(7102, "b", None)],
        sets={(11, 1100): _set_result(11, "cats", [_doc(1)])},
    )
    candidates = {7101: {}, 7102: {}, 7199: {}}
    result = await _enumerate_sets(client, candidates, set(), 5.0, 100)
    assert result["documents_resolved"] == 2
    assert result["unresolved_documents"] == 1  # Telegram did not return 7199
    assert result["documents_without_set"] == 1  # no usable set identity
    assert result["error"] is None
    assert list(result["set_candidates"]) == [1]  # only the real set member


@pytest.mark.asyncio
async def test_enumeration_failure_is_reported_not_raised():
    client = _FakeTlClient(
        documents=[_doc(7201, "a", SET_A), _doc(7202, "b", SET_B)],
        sets={
            (11, 1100): _set_result(11, "ok", [_doc(1)]),
            (22, 2200): None,  # SET_NOT_FOUND via the fake
        },
    )
    candidates = {7201: {}, 7202: {}}
    result = await _enumerate_sets(client, candidates, set(), 5.0, 100)
    assert result["sets_resolved"] == 1
    assert result["error"] is not None
    assert "sticker set enumeration failed" in result["error"]
    assert sorted(result["set_candidates"]) == [1]  # the successful set kept its members


@pytest.mark.asyncio
async def test_enumeration_document_resolution_failure_stops_cleanly():
    client = _FakeTlClient(docs_error=RuntimeError("RPC down"))
    result = await _enumerate_sets(client, {7301: {}}, set(), 5.0, 100)
    assert result["error"] is not None
    assert "custom emoji document resolution failed" in result["error"]
    assert result["documents_resolved"] == 0
    assert result["sets_resolved"] == 0


@pytest.mark.asyncio
async def test_enumeration_is_bounded_per_set_count():
    documents = [_doc(9000 + i, str(i % 10), InputStickerSetID(id=i, access_hash=i * 10)) for i in range(MAX_SETS_PER_IMPORT + 5)]
    sets = {
        (i, i * 10): _set_result(i, f"s{i}", [_doc(8000 + i)])
        for i in range(MAX_SETS_PER_IMPORT + 5)
    }
    client = _FakeTlClient(documents=documents, sets=sets)
    candidates = {9000 + i: {} for i in range(MAX_SETS_PER_IMPORT + 5)}
    result = await _enumerate_sets(client, candidates, set(), 5.0, 1000)
    assert len(client.set_calls) == MAX_SETS_PER_IMPORT
    assert result["sets_resolved"] == MAX_SETS_PER_IMPORT
    assert result["hit_set_limit"] is True


@pytest.mark.asyncio
async def test_enumeration_is_bounded_per_set_members():
    members = [_doc(10000 + i, str(i)) for i in range(MAX_SET_MEMBERS_PER_SET + 50)]
    client = _FakeTlClient(
        documents=[_doc(9999, "x", SET_A)],
        sets={(11, 1100): _set_result(11, "big", members)},
    )
    result = await _enumerate_sets(client, {9999: {}}, set(), 5.0, 100000)
    assert result["hit_set_member_limit"] is True
    assert len(result["set_candidates"]) == MAX_SET_MEMBERS_PER_SET


@pytest.mark.asyncio
async def test_enumeration_respects_the_set_record_budget():
    client = _FakeTlClient(
        documents=[_doc(11001, "x", SET_A)],
        sets={(11, 1100): _set_result(11, "cats", [_doc(1), _doc(2), _doc(3)])},
    )
    result = await _enumerate_sets(client, {11001: {}}, set(), 5.0, 2)
    assert len(result["set_candidates"]) == 2
    assert result["hit_set_member_limit"] is True


@pytest.mark.asyncio
async def test_enumeration_is_deterministic_in_first_appearance_order():
    documents = [_doc(12001, "a", SET_A), _doc(12002, "b", SET_B)]
    sets = {
        (11, 1100): _set_result(11, "cats", [_doc(1)]),
        (22, 2200): _set_result(22, "dogs", [_doc(2)]),
    }
    runs = [
        await _enumerate_sets(_FakeTlClient(documents=documents, sets=sets), {12001: {}, 12002: {}}, set(), 5.0, 100)
        for _ in range(3)
    ]
    assert all(r == runs[0] for r in runs[1:])
    assert list(runs[0]["set_candidates"]) == [1, 2]


# ── C. import integration: scan + enrichment + persistence ────────────────────


@pytest.mark.asyncio
async def test_import_persists_scan_records_and_deduplicated_set_members():
    client = _FakeTlClient(
        messages=[_emoji_message(5, 13001, "😀")],
        documents=[_doc(13001, "😀", SET_A)],
        sets={(11, 1100): _set_result(11, "cats", [_doc(13001, "😀"), _doc(77, "smile")])},
    )
    report = await import_from_saved_messages(client, OWNER)
    assert report["ok"] is True
    assert report["error"] is None
    assert report["degraded"] is False
    assert report["imported"] == 1          # the scanned message record
    assert report["set_imported"] == 1      # member 77; 13001 is a dedup hit
    assert report["set_duplicates"] == 1
    assert report["sets_resolved"] == 1
    rows, total = await db_client.list_emoji_entries(OWNER, limit=10, offset=0)
    assert total == 2
    ids = {r["document_id"] for r in rows}
    assert ids == {13001, 77}
    member = next(r for r in rows if r["document_id"] == 77)
    assert member["source_msg_id"] is None  # set members carry no message origin
    assert member["alt_text"] == "smile"
    # the counters invariant from the report docstring
    assert report["custom_emoji_seen"] + report["set_members_seen"] == (
        report["imported"] + report["duplicates"] + report["failed"]
        + report["set_imported"] + report["set_duplicates"] + report["set_failed"]
    )


@pytest.mark.asyncio
async def test_import_repeat_run_keeps_dedup_for_enriched_members():
    client = _FakeTlClient(
        messages=[_emoji_message(5, 14001, "😀")],
        documents=[_doc(14001, "😀", SET_A)],
        sets={(11, 1100): _set_result(11, "cats", [_doc(14001), _doc(88, "wave")])},
    )
    first = await import_from_saved_messages(client, OWNER)
    assert first["ok"] is True
    assert first["set_imported"] == 1
    second = await import_from_saved_messages(client, OWNER)
    assert second["imported"] == 0
    assert second["set_imported"] == 0
    # The scan candidate is a durable-library duplicate, so no new candidate
    # reaches enrichment at all — nothing is re-resolved, nothing re-imported.
    assert second["duplicates"] == 1
    assert second["documents_resolved"] == 0 and second["sets_resolved"] == 0
    assert second["set_duplicates"] == 0
    _rows, total = await db_client.list_emoji_entries(OWNER, limit=10, offset=0)
    assert total == 2


@pytest.mark.asyncio
async def test_import_reports_degraded_when_enrichment_fails():
    client = _FakeTlClient(
        messages=[_emoji_message(5, 15001, "😀")],
        documents=[_doc(15001, "😀", SET_A)],
        set_errors={(11, 1100): RuntimeError("CHAT_ADMIN_REQUIRED")},
    )
    report = await import_from_saved_messages(client, OWNER)
    assert report["ok"] is True                     # message-level import succeeded
    assert report["degraded"] is True               # enrichment honestly degraded
    assert report["set_error"] is not None
    assert "sticker set enumeration failed" in report["set_error"]
    assert report["sets_resolved"] == 0
    assert report["imported"] == 1                  # the scan record still persisted
    _rows, total = await db_client.list_emoji_entries(OWNER, limit=10, offset=0)
    assert total == 1


@pytest.mark.asyncio
async def test_import_degraded_when_documents_cannot_be_resolved():
    client = _FakeTlClient(messages=[_emoji_message(5, 16001, "😀")], documents=[])
    report = await import_from_saved_messages(client, OWNER)
    assert report["degraded"] is True
    assert report["unresolved_documents"] == 1
    assert report["imported"] == 1
    assert report["set_imported"] == 0


@pytest.mark.asyncio
async def test_import_persistence_failure_stays_visible_in_the_report():
    store: dict[str, Any] = {"rows": [], "insert_error": "durable write down"}
    client = _FakeTlClient(
        messages=[_emoji_message(5, 17001, "😀")],
        documents=[_doc(17001, "😀", SET_A)],
        sets={(11, 1100): _set_result(11, "cats", [_doc(1, "a")])},
    )
    original_get_db = db_client.get_db
    db_client.get_db = lambda: _FakeSupabase(store)
    try:
        report = await import_from_saved_messages(client, OWNER)
    finally:
        db_client.get_db = original_get_db
    assert report["ok"] is False
    assert report["failed"] == 1
    assert report["set_failed"] == 1
    assert report["imported"] == 0 and report["set_imported"] == 0


class _FakeSupabase:
    """Minimal Supabase stand-in whose every insert fails and every count read works."""

    def __init__(self, store: dict):
        self._store = store

    def table(self, name):
        return _FailingQuery(self._store)


class _FailingQuery:
    def __init__(self, store: dict):
        self._store = store

    def select(self, *a, **k):
        return self

    def insert(self, payload):
        self._payload = payload
        return self

    def eq(self, *a, **k):
        return self

    def order(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def range(self, *a, **k):
        return self

    def execute(self):
        if hasattr(self, "_payload"):
            raise RuntimeError(self._store["insert_error"])
        return SimpleNamespace(data=[], count=0)


@pytest.mark.asyncio
async def test_import_scan_failure_still_aborts_before_persistence():
    client = _FakeTlClient(
        messages=[_emoji_message(5, 18001, "😀")],
        fail_from_call=0,
        documents=[_doc(18001, "😀", SET_A)],
    )
    report = await import_from_saved_messages(client, OWNER)
    assert report["ok"] is False
    assert "rpc down" in report["error"]
    assert report["pages"] == 0
    assert report["imported"] == 0 and report["set_imported"] == 0
    _rows, total = await db_client.list_emoji_entries(OWNER, limit=10, offset=0)
    assert total == 0
