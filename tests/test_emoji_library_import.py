"""Emoji Library & Saved Messages import — Phase 1 (Emoji & Reaction).

Pins the Phase 1 contracts (ROADMAP §7/§8/§9, IMPLEMENTATION_REPORT):

  1. Entity-based extraction from the PHASE 0 serialized representation
     (`serialize_message` dicts): `MessageEntityCustomEmoji` only, with
     UTF-16 span resolution through `utf16_index_at` (fail closed), exact
     Unicode alt-text preservation, and document-id validation.
  2. Plain Unicode emoji and unrelated entity types are never imported;
     messages without custom-emoji entities contribute nothing.
  3. Import semantics: first import persists, duplicates across messages and
     repeat imports are counted and never stored twice, malformed payloads
     are skipped honestly, collection/timeout/persistence failures fail
     closed with an honest report, and pagination is deterministic
     (newest-first, exclusive `max_id` cursor, explicit bounds).
  4. Persistence runs through the project's existing db/client.py
     Supabase-or-in-memory-fallback pattern (no second DB layer), and the
     import is idempotent at the `(owner_id, document_id)` level.
  5. Context isolation (no `backend.ai` imports) and architecture
     constraints (no second client/loop/scheduler/executor, no forwarding).

Everything runs offline: the Telegram boundary is faked at the
`client.iter_messages` surface (yielding real Telethon entities that flow
through Phase 0 `serialize_message`) and persistence is the project's
in-memory fallback or a faked Supabase table — no live credential.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from telethon.tl.types import MessageEntityBold, MessageEntityCustomEmoji

from backend.db import client as db_client
from backend.services import emoji_library_service
from backend.services.emoji_library_service import (
    extract_custom_emoji_records,
    import_from_saved_messages,
)
from backend.telegram_api._helpers import (
    serialize_message,
    utf16_length,
    utf16_offset,
)

OWNER = 7770001

_FALLBACK_KEY = "emoji_library"


@pytest.fixture(autouse=True)
def _reset_library_fallback():
    db_client._fallback[_FALLBACK_KEY] = []
    yield
    db_client._fallback[_FALLBACK_KEY] = []


# ── helpers ──────────────────────────────────────────────────────────────────

def _custom_entity(text: str, needle: str, doc_id: int) -> MessageEntityCustomEmoji:
    start = text.index(needle)
    return MessageEntityCustomEmoji(
        utf16_offset(text, start), utf16_length(needle), doc_id
    )


def _msg(msg_id: int, text: str, entities=None) -> SimpleNamespace:
    return SimpleNamespace(id=msg_id, message=text, entities=entities)


def _emoji_msg(msg_id: int, doc_id: int, emoji: str = "😀") -> SimpleNamespace:
    text = f"m{msg_id} {emoji}"
    return _msg(msg_id, text, [_custom_entity(text, emoji, doc_id)])


def _multi_emoji_msg(msg_id: int, doc_ids: list[int], emoji: str = "😀") -> SimpleNamespace:
    text = f"m{msg_id}"
    entities = []
    for doc_id in doc_ids:
        text += f" {emoji}"
        start = text.rindex(emoji)
        entities.append(
            MessageEntityCustomEmoji(utf16_offset(text, start), utf16_length(emoji), doc_id)
        )
    return _msg(msg_id, text, entities)


def _dict_entity(
    doc_id: Any = 1001,
    offset: Any = 0,
    length: Any = 2,
    etype: str = "MessageEntityCustomEmoji",
    include_doc: bool = True,
    **extra: Any,
) -> dict:
    ent: dict[str, Any] = {"type": etype, "offset": offset, "length": length}
    if include_doc:
        ent["document_id"] = doc_id
    ent.update(extra)
    return ent


def _dict_msg(msg_id: Any, text: str, entities: list) -> dict:
    return {"id": msg_id, "text": text, "entities": entities}


class _FakeSelfClient:
    """Fakes the Telethon self-client surface `messages.iter_messages`
    consumes: `iter_messages(chat_id, **kwargs)` returning an async
    generator that yields newest-first and honours `limit`/`max_id`."""

    def __init__(self, messages: list, *, fail_from_call: int | None = None, delay_s: float = 0.0):
        self._messages = sorted(
            messages, key=lambda m: getattr(m, "id", 0), reverse=True
        )
        self._fail_from_call = fail_from_call
        self._delay_s = delay_s
        self.calls: list[dict] = []

    def iter_messages(self, chat_id, **kwargs):
        self.calls.append({"chat_id": chat_id, **kwargs})
        index = len(self.calls) - 1
        delay = self._delay_s
        messages = self._messages
        fail_from = self._fail_from_call

        async def _gen():
            if delay:
                await asyncio.sleep(delay)
            if fail_from is not None and index >= fail_from:
                raise RuntimeError("rpc down")
            limit = kwargs.get("limit")
            max_id = kwargs.get("max_id")
            out = []
            for m in messages:
                mid = getattr(m, "id", 0)
                if max_id is not None and mid >= max_id:
                    continue
                out.append(m)
                if limit is not None and len(out) >= limit:
                    break
            for m in out:
                yield m

        return _gen()


class _FakeQuery:
    """Chainable stand-in for the supabase-py table query builder."""

    def __init__(self, store: dict):
        self._store = store
        self._mode = None
        self._payload = None

    def select(self, *args, **kwargs):
        self._mode = "select"
        return self

    def insert(self, payload):
        self._mode = "insert"
        self._payload = payload
        return self

    def eq(self, *args, **kwargs):
        return self

    def order(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def range(self, *args, **kwargs):
        return self

    def maybe_single(self):
        return self

    def execute(self):
        if self._mode == "insert":
            if self._store.get("insert_error"):
                raise RuntimeError(self._store["insert_error"])
            self._store["rows"].append(dict(self._payload))
            return SimpleNamespace(data=[dict(self._payload)], count=None)
        if self._store.get("select_error"):
            raise RuntimeError(self._store["select_error"])
        rows = [{"document_id": r.get("document_id")} for r in self._store["rows"]]
        return SimpleNamespace(data=rows, count=len(rows))


class _FakeDb:
    def __init__(self, store: dict):
        self._store = store

    def table(self, name):
        return _FakeQuery(self._store)


# ── extraction: valid records ────────────────────────────────────────────────

def test_extract_valid_custom_emoji_record():
    text = "hi 😀 bye"
    result = extract_custom_emoji_records(
        _dict_msg(42, text, [_dict_entity(offset=3, length=2, doc_id=555001)])
    )
    assert result.malformed == 0
    assert result.entity_limit_hit is False
    assert result.records == [
        {
            "document_id": 555001,
            "alt_text": "😀",
            "source_msg_id": 42,
            "source": "imported",
        }
    ]


def test_extract_alt_preserved_across_utf16_spans():
    text = "x🫪y 😀 done"
    emoji_a = "🫪"
    emoji_b = "😀"
    offset_a = utf16_offset(text, text.index(emoji_a))
    offset_b = utf16_offset(text, text.index(emoji_b))
    result = extract_custom_emoji_records(
        _dict_msg(
            7,
            text,
            [
                _dict_entity(offset=offset_a, length=utf16_length(emoji_a), doc_id=11),
                _dict_entity(offset=offset_b, length=utf16_length(emoji_b), doc_id=12),
            ],
        )
    )
    assert result.malformed == 0
    assert [r["document_id"] for r in result.records] == [11, 12]
    assert [r["alt_text"] for r in result.records] == [emoji_a, emoji_b]


def test_extract_surrounding_text_is_not_part_of_alt():
    text = "سلام 😀 جهان"
    offset = utf16_offset(text, text.index("😀"))
    result = extract_custom_emoji_records(
        _dict_msg(1, text, [_dict_entity(offset=offset, length=2, doc_id=9)])
    )
    assert result.records[0]["alt_text"] == "😀"


def test_extract_multiple_entities_in_one_message():
    text = "😀 😀"
    first = utf16_offset(text, 0)
    second = utf16_offset(text, 2)
    result = extract_custom_emoji_records(
        _dict_msg(
            3,
            text,
            [
                _dict_entity(offset=first, length=2, doc_id=101),
                _dict_entity(offset=second, length=2, doc_id=102),
            ],
        )
    )
    assert [r["document_id"] for r in result.records] == [101, 102]
    assert result.malformed == 0


def test_extract_accepts_digit_string_document_id():
    result = extract_custom_emoji_records(
        _dict_msg(1, "😀", [_dict_entity(doc_id="555001", offset=0, length=2)])
    )
    assert result.records[0]["document_id"] == 555001


def test_extract_source_msg_id_is_none_when_missing_or_invalid():
    for bad_id in (None, 0, -3, "abc"):
        result = extract_custom_emoji_records(
            _dict_msg(bad_id, "😀", [_dict_entity(offset=0, length=2)])
        )
        assert result.records[0]["source_msg_id"] is None, bad_id


# ── extraction: exclusion & malformed ────────────────────────────────────────

def test_extract_ignores_plain_unicode_emoji_without_entity():
    result = extract_custom_emoji_records(_dict_msg(1, "😀🚀🎉", []))
    assert result.records == []
    assert result.malformed == 0


def test_extract_ignores_unrelated_entity_types():
    entities = [
        _dict_entity(etype="MessageEntityBold", include_doc=False, offset=0, length=2),
        _dict_entity(
            etype="MessageEntityTextUrl", include_doc=False, offset=0, length=2, url="https://x"
        ),
        _dict_entity(etype="MessageEntityEmoji", include_doc=False, offset=0, length=2),
        {"nonsense": True},
    ]
    result = extract_custom_emoji_records(_dict_msg(1, "😀", entities))
    assert result.records == []
    assert result.malformed == 0


def test_extract_missing_document_id_is_malformed():
    result = extract_custom_emoji_records(
        _dict_msg(1, "😀", [_dict_entity(include_doc=False, offset=0, length=2)])
    )
    assert result.records == []
    assert result.malformed == 1


@pytest.mark.parametrize("bad_doc", ["abc", None, True, 0, -7, 3.5])
def test_extract_invalid_document_id_is_malformed(bad_doc):
    result = extract_custom_emoji_records(
        _dict_msg(1, "😀", [_dict_entity(doc_id=bad_doc, offset=0, length=2)])
    )
    assert result.records == []
    assert result.malformed == 1


@pytest.mark.parametrize(
    "offset,length",
    [(-1, 2), (0, 0), (0, -2), ("x", 2), (0, "y"), (True, 2), (0, False)],
)
def test_extract_invalid_span_offsets_are_malformed(offset, length):
    result = extract_custom_emoji_records(
        _dict_msg(1, "😀", [_dict_entity(offset=offset, length=length)])
    )
    assert result.records == []
    assert result.malformed == 1


def test_extract_span_beyond_text_fails_closed():
    result = extract_custom_emoji_records(
        _dict_msg(1, "ab", [_dict_entity(offset=99, length=2)])
    )
    assert result.records == []
    assert result.malformed == 1


def test_extract_mid_surrogate_span_fails_closed():
    result = extract_custom_emoji_records(
        _dict_msg(1, "😀", [_dict_entity(offset=1, length=2)])
    )
    assert result.records == []
    assert result.malformed == 1


def test_extract_empty_text_with_entity_is_malformed():
    result = extract_custom_emoji_records(
        _dict_msg(1, "", [_dict_entity(offset=0, length=2)])
    )
    assert result.records == []
    assert result.malformed == 1


def test_extract_minimal_messages_without_entities_or_id():
    for message in ({}, {"id": 5}, {"text": "hi", "entities": None}):
        result = extract_custom_emoji_records(message)
        assert result.records == []
        assert result.malformed == 0
        assert result.entity_limit_hit is False


def test_extract_entity_processing_bound():
    text = " ".join(["😀"] * 150)
    entities = []
    for i in range(150):
        entities.append(
            {
                "type": "MessageEntityCustomEmoji",
                 "offset": utf16_offset(text, i * 2), "length": 2,
                 "document_id": 9000 + i}
        )
    result = extract_custom_emoji_records(_dict_msg(1, text, entities))
    assert len(result.records) == emoji_library_service.MAX_ENTITIES_PER_MESSAGE
    assert result.entity_limit_hit is True


def test_extraction_consumes_phase0_serialized_message():
    text = "hi 😀"
    msg = _msg(5, text, [MessageEntityCustomEmoji(3, 2, 424242)])
    serialized = serialize_message(msg)
    assert serialized["entities"][0]["type"] == "MessageEntityCustomEmoji"
    result = extract_custom_emoji_records(serialized)
    assert result.records == [
        {
            "document_id": 424242,
            "alt_text": "😀",
            "source_msg_id": 5,
            "source": "imported",
        }
    ]


# ── import: first run, records, persistence ──────────────────────────────────

@pytest.mark.asyncio
async def test_first_import_collects_and_persists_records():
    client = _FakeSelfClient(
        [_emoji_msg(2, 1001), _multi_emoji_msg(1, [1001, 1002])]
    )
    report = await import_from_saved_messages(client, OWNER)

    assert report["ok"] is True
    assert report["error"] is None
    assert report["storage"] == "memory"
    assert report["pages"] == 1
    assert report["scanned_messages"] == 2
    assert report["custom_emoji_seen"] == 3
    assert report["malformed_entities"] == 0
    assert report["imported"] == 2
    assert report["duplicates"] == 1
    assert report["failed"] == 0
    assert report["library_total"] == 2
    assert report["end_reached"] is True
    assert report["hit_scan_limit"] is False
    assert report["hit_record_limit"] is False
    assert report["hit_entity_limit"] is False
    assert (
        report["custom_emoji_seen"]
        == report["imported"] + report["duplicates"] + report["failed"]
    )

    rows, total = await db_client.list_emoji_entries(OWNER)
    assert total == 2
    assert {r["document_id"] for r in rows} == {1001, 1002}
    for row in rows:
        assert row["owner_id"] == OWNER
        assert row["alt_text"] == "😀"
        assert row["source"] == "imported"
        assert row["source_msg_id"] in (1, 2)
        assert row["created_at"]
        datetime_text = row["created_at"]
        assert "T" in datetime_text

    ids = await db_client.list_emoji_document_ids(OWNER)
    assert sorted(ids) == [1001, 1002]


@pytest.mark.asyncio
async def test_repeated_import_is_idempotent():
    client = _FakeSelfClient(
        [_emoji_msg(2, 1001), _multi_emoji_msg(1, [1001, 1002])]
    )
    first = await import_from_saved_messages(client, OWNER)
    second = await import_from_saved_messages(client, OWNER)

    assert first["imported"] == 2
    assert second["ok"] is True
    assert second["imported"] == 0
    assert second["custom_emoji_seen"] == 3
    assert second["duplicates"] == 3
    assert second["failed"] == 0
    assert second["library_total"] == 2

    _, total = await db_client.list_emoji_entries(OWNER)
    assert total == 2
    assert len(db_client._fallback[_FALLBACK_KEY]) == 2


@pytest.mark.asyncio
async def test_dedup_against_preexisting_library_entries():
    stored = await db_client.insert_emoji_entry(
        {
            "owner_id": OWNER,
            "document_id": 9001,
            "alt_text": "😀",
            "source": "imported",
            "source_msg_id": 99,
        }
    )
    assert stored is not None

    client = _FakeSelfClient([_multi_emoji_msg(5, [9001, 9002])])
    report = await import_from_saved_messages(client, OWNER)

    assert report["imported"] == 1
    assert report["duplicates"] == 1
    assert report["library_total"] == 2
    assert len(db_client._fallback[_FALLBACK_KEY]) == 2


@pytest.mark.asyncio
async def test_malformed_entities_do_not_block_valid_ones():
    client = _FakeSelfClient(
        [
            _msg(
                3,
                "😀",
        [
            MessageEntityCustomEmoji(0, 2, None),
            MessageEntityCustomEmoji(0, 2, 7007),
        ],
            )
        ]
    )
    report = await import_from_saved_messages(client, OWNER)
    assert report["malformed_entities"] == 1
    assert report["imported"] == 1
    assert report["ok"] is True
    ids = await db_client.list_emoji_document_ids(OWNER)
    assert ids == [7007]


# ── import: pagination & bounds ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pagination_uses_deterministic_max_id_cursor():
    client = _FakeSelfClient([_emoji_msg(i, 1000 + i) for i in range(1, 6)])
    report = await import_from_saved_messages(client, OWNER, page_size=2)

    assert report["pages"] == 3
    assert report["scanned_messages"] == 5
    assert report["imported"] == 5
    assert report["end_reached"] is True
    assert report["hit_scan_limit"] is False
    assert client.calls == [
        {"chat_id": "me", "limit": 2},
        {"chat_id": "me", "limit": 2, "max_id": 4},
        {"chat_id": "me", "limit": 2, "max_id": 2},
    ]


@pytest.mark.asyncio
async def test_scan_bound_limits_messages_scanned():
    client = _FakeSelfClient([_emoji_msg(i, 1000 + i) for i in range(1, 6)])
    report = await import_from_saved_messages(client, OWNER, max_messages=2)

    assert report["scanned_messages"] == 2
    assert report["imported"] == 2
    assert report["hit_scan_limit"] is True
    assert report["end_reached"] is False
    assert report["ok"] is True


@pytest.mark.asyncio
async def test_scan_bound_clamps_nonpositive_values_to_one():
    client = _FakeSelfClient([_emoji_msg(i, 1000 + i) for i in range(1, 6)])
    report = await import_from_saved_messages(client, OWNER, max_messages=0, page_size=0)

    assert report["scanned_messages"] == 1
    assert report["hit_scan_limit"] is True


@pytest.mark.asyncio
async def test_record_bound_stops_collection():
    client = _FakeSelfClient([_multi_emoji_msg(1, [301, 302, 303])])
    report = await import_from_saved_messages(client, OWNER, max_records=2)

    assert report["imported"] == 2
    assert report["custom_emoji_seen"] == 2
    assert report["hit_record_limit"] is True
    assert report["end_reached"] is False
    assert report["ok"] is True
    assert (
        report["custom_emoji_seen"]
        == report["imported"] + report["duplicates"] + report["failed"]
    )
    assert len(db_client._fallback[_FALLBACK_KEY]) == 2


@pytest.mark.asyncio
async def test_entity_bound_reported_through_import():
    text = " ".join(["😀"] * 101)
    entities = []
    for i in range(101):
        entities.append(
            MessageEntityCustomEmoji(utf16_offset(text, i * 2), 2, 6000 + i)
        )
    client = _FakeSelfClient([_msg(1, text, entities)])
    report = await import_from_saved_messages(client, OWNER)

    assert report["hit_entity_limit"] is True
    assert report["custom_emoji_seen"] == 100
    assert report["imported"] == 100
    assert report["ok"] is True


@pytest.mark.asyncio
async def test_empty_saved_messages_reports_clean_noop():
    client = _FakeSelfClient([])
    report = await import_from_saved_messages(client, OWNER)

    assert report["ok"] is True
    assert report["error"] is None
    assert report["pages"] == 1
    assert report["scanned_messages"] == 0
    assert report["imported"] == 0
    assert report["end_reached"] is True
    assert report["library_total"] == 0


@pytest.mark.asyncio
async def test_inaccessible_and_entityless_messages_are_skipped():
    client = _FakeSelfClient([_emoji_msg(3, 7001), None, SimpleNamespace(id=7)])
    report = await import_from_saved_messages(client, OWNER)

    assert report["ok"] is True
    assert report["scanned_messages"] == 3
    assert report["custom_emoji_seen"] == 1
    assert report["imported"] == 1
    assert report["end_reached"] is True


# ── import: failure paths ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_collection_api_failure_fails_closed():
    client = _FakeSelfClient([_emoji_msg(i, 1000 + i) for i in range(1, 4)], fail_from_call=0)
    report = await import_from_saved_messages(client, OWNER)

    assert report["ok"] is False
    assert report["error"] is not None
    assert "rpc down" in report["error"]
    assert report["pages"] == 0
    assert report["scanned_messages"] == 0
    assert report["imported"] == 0
    assert report["library_total"] == 0
    assert db_client._fallback[_FALLBACK_KEY] == []
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_collection_timeout_reported_not_raised():
    client = _FakeSelfClient([_emoji_msg(1, 1001)], delay_s=1.0)
    report = await import_from_saved_messages(client, OWNER, page_timeout=0.05)

    assert report["ok"] is False
    assert report["error"] is not None
    assert "timed out" in report["error"]
    assert report["imported"] == 0
    assert db_client._fallback[_FALLBACK_KEY] == []


@pytest.mark.asyncio
async def test_durable_read_failure_aborts_before_scan(monkeypatch):
    store = {"rows": [], "select_error": "relation does not exist"}
    monkeypatch.setattr(db_client, "get_db", lambda: _FakeDb(store))
    client = _FakeSelfClient([_emoji_msg(1, 1001)])
    report = await import_from_saved_messages(client, OWNER)

    assert report["ok"] is False
    assert report["error"] is not None
    assert "aborted" in report["error"]
    assert report["library_total"] is None
    assert report["storage"] == "supabase"
    assert report["pages"] == 0
    assert client.calls == []


@pytest.mark.asyncio
async def test_persistence_failure_is_reported_honestly(monkeypatch):
    store = {"rows": [], "insert_error": "insert exploded"}
    monkeypatch.setattr(db_client, "get_db", lambda: _FakeDb(store))
    client = _FakeSelfClient([_multi_emoji_msg(1, [401, 402])])
    report = await import_from_saved_messages(client, OWNER)

    assert report["ok"] is False
    assert report["error"] is None
    assert report["failed"] == 2
    assert report["imported"] == 0
    assert report["duplicates"] == 0
    assert report["library_total"] == 0
    assert report["storage"] == "supabase"
    assert (
        report["custom_emoji_seen"]
        == report["imported"] + report["duplicates"] + report["failed"]
    )


@pytest.mark.asyncio
async def test_supabase_storage_label_and_insert_payload(monkeypatch):
    store = {"rows": [{"owner_id": OWNER, "document_id": 1001}]}
    monkeypatch.setattr(db_client, "get_db", lambda: _FakeDb(store))
    client = _FakeSelfClient([_multi_emoji_msg(1, [1001, 1002])])
    report = await import_from_saved_messages(client, OWNER)

    assert report["ok"] is True
    assert report["storage"] == "supabase"
    assert report["imported"] == 1
    assert report["duplicates"] == 1
    assert report["library_total"] == 2
    assert len(store["rows"]) == 2
    inserted = store["rows"][-1]
    assert set(inserted) == {
        "document_id",
        "alt_text",
        "source_msg_id",
        "source",
        "owner_id",
        "created_at",
    }


# ── persistence layer itself ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_insert_emoji_entry_is_unique_per_owner_and_document():
    row = {
        "owner_id": OWNER,
        "document_id": 555,
        "alt_text": "😀",
        "source": "imported",
        "source_msg_id": 1,
    }
    first = await db_client.insert_emoji_entry(dict(row))
    second = await db_client.insert_emoji_entry(dict(row))
    other_owner = await db_client.insert_emoji_entry({**row, "owner_id": OWNER + 1})

    assert first is not None
    assert second is None
    assert other_owner is not None
    assert len(db_client._fallback[_FALLBACK_KEY]) == 2


@pytest.mark.asyncio
async def test_list_emoji_entries_paginates_and_counts_total():
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
    page2, total2 = await db_client.list_emoji_entries(OWNER, limit=2, offset=2)
    assert total2 == 3
    assert len(page2) == 1
    ids_seen = {r["document_id"] for r in page + page2}
    assert ids_seen == {100, 101, 102}


# ── context isolation & architecture ────────────────────────────────────────

def test_context_isolation_no_ai_module_imports():
    source = Path(emoji_library_service.__file__).read_text()
    tree = ast.parse(source)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    assert not any(
        name == "backend.ai" or name.startswith("backend.ai.") for name in imported
    )
    assert not any(
        "conversation" in name or "history" in name or "memory" in name
        for name in imported
    )


@pytest.mark.asyncio
async def test_context_isolation_extraction_needs_no_conversation_state():
    result = extract_custom_emoji_records(
        {"text": "😀", "entities": [_dict_entity(offset=0, length=2)]}
    )
    assert len(result.records) == 1
    assert result.records[0]["source_msg_id"] is None

    client = _FakeSelfClient([_msg(1, "😀", [MessageEntityCustomEmoji(0, 2, 123)])])
    report = await import_from_saved_messages(client, OWNER)
    assert report["imported"] == 1


def test_no_second_client_loop_scheduler_or_forwarding():
    source = Path(emoji_library_service.__file__).read_text()
    for forbidden in (
        "TelegramClient",
        "run_until_disconnected",
        "create_task",
        "immortal_create_task",
        "forward_messages",
        "SendMessagesRequest",
    ):
        assert forbidden not in source, forbidden

    params = inspect.signature(import_from_saved_messages).parameters
    assert list(params) == [
        "client",
        "owner_id",
        "max_messages",
        "page_size",
        "max_records",
        "page_timeout",
        "set_timeout",
        "resolve_sets",
    ]


# ── Phase 0 regression (entity serialization round-trip) ─────────────────────

def test_phase0_serialization_roundtrip_preserves_custom_emoji_payload():
    text = "a😀b"
    msg = _msg(9, text, [MessageEntityCustomEmoji(1, 2, 777000111)])
    serialized = serialize_message(msg)
    ent = serialized["entities"][0]
    assert ent == {
        "type": "MessageEntityCustomEmoji",
        "offset": 1,
        "length": 2,
        "document_id": 777000111,
    }
    assert serialized["text"] == text
    bold = serialize_message(_msg(10, "x", [MessageEntityBold(0, 1)]))
    assert bold["entities"][0]["type"] == "MessageEntityBold"
