"""
Emoji Replacement Reconstruction — Phase 4 (Emoji & Reaction, ROADMAP §15–§24).

Pins the Phase 4 contracts:

  1. Transformer: structural mapping-key replacement only (never regex /
     keyword routing), multi-emoji in one pass, unmapped emoji and ordinary
     text byte-identical, entity spans rebased in UTF-16 units, an entity
     that covers a replaced span still covers it, existing custom-emoji
     entities never rewritten, and every unsafe combination (unknown entity
     type, unresolvable offsets, partial overlap, unbounded replacement
     count) fails the whole transformation closed.
  2. Resolution boundary: the service consumes ONLY the Phase 3
     ``resolve_effective_category`` — OFF, no category, and a deleted
     category all mean no transformation and no Telegram side effect.
  3. Reconstruction: send-first ordering through the EXISTING bridge
     (same destination, reply target preserved), the original is deleted
     only after delivery succeeded, and a failed delivery leaves the
     original untouched with an honest status (no alt-text fallback, no
     fabricated success, no §34-D resolution).
  4. Owner boundary: only owner-authored messages are processed; foreign
     authors, inline-bot origin (the Glass UI panel machinery), the bridge
     bot's own author, pending panel input, and media messages are skipped.
  5. Loop prevention: a reconstructed message cannot re-enter the pipeline
     and an original cannot be reconstructed twice.
  6. Architecture: one outgoing handler on the existing update path, no
     second client/loop/scheduler/executor, no ``backend.ai`` import, no
     regex, no Phase 5/6 surface.

Everything runs offline: the Telegram surface is faked at the helper-bot /
self-client boundary the bridge already consumes. No live Telegram behavior
is claimed anywhere.
"""
from __future__ import annotations

import ast
import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import backend.helper.client as helper_client
from backend.bot.handlers import emoji_replacement
from backend.db import client as db_client
from backend.helper import input_state
from backend.services import emoji_category_service as cat_service
from backend.services import emoji_replacement_service as repl
from backend.services import emoji_state_service as state_service
from backend.services import emoji_transformer
from backend.services.emoji_transformer import (
    E_INVALID_INPUT,
    E_MALFORMED_ENTITIES,
    E_TOO_MANY_REPLACEMENTS,
    E_UNSAFE_ENTITY,
    E_UNSUPPORTED_ENTITY,
    MAX_REPLACEMENTS,
    transform_message,
    usable_mappings,
)
from backend.telegram_api._helpers import (
    serialize_message,
    utf16_length,
    utf16_offset,
)

OWNER = 424242
OTHER = 991199
BOT_ID = 777000

CHAT = -1001234567890
CHAT2 = -1009998887771

KEY = "🫪"          # the owner's "simple" emoji
OTHER_KEY = "🗣"
DOC = 42001


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for key in (
        "emoji_library", "emoji_categories", "emoji_mappings",
        "emoji_state", "emoji_chat_overrides",
    ):
        db_client._fallback[key] = [] if key not in ("emoji_state",) else {}
    repl.reset_loop_guard()
    input_state.clear_all()
    yield
    for key in (
        "emoji_library", "emoji_categories", "emoji_mappings",
        "emoji_state", "emoji_chat_overrides",
    ):
        db_client._fallback[key] = [] if key not in ("emoji_state",) else {}
    repl.reset_loop_guard()
    input_state.clear_all()


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── telegram-level fakes (the surface the bridge already consumes) ───────────


class _FakeBot:
    """Mimics the helper-bot Telethon surface the bridge consumes."""

    def __init__(self, *, fail: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail = fail
        self._next_id = 9001

    def is_connected(self) -> bool:
        return True

    async def send_message(self, peer, text, *, formatting_entities=None, reply_to=None):
        self.calls.append({
            "peer": peer, "text": text,
            "formatting_entities": formatting_entities, "reply_to": reply_to,
        })
        if self.fail is not None:
            raise self.fail
        message = SimpleNamespace(
            id=self._next_id, chat_id=peer[1] if isinstance(peer, tuple) else 0,
            sender_id=BOT_ID, text=text, message=None, date=None, media=None,
            reply_to=None, out=False, entities=list(formatting_entities or []),
            inline_message_id=None, peer_id=None,
        )
        message.id = self._next_id
        self._next_id += 1
        return message


class _FakeSelfClient:
    """Self-client surface used by the bridge (peer resolution) and deletion."""

    def __init__(self, *, delete_fails: bool = False) -> None:
        self.resolved: list[Any] = []
        self.deleted: list[tuple[Any, tuple[int, ...]]] = []
        self.delete_fails = delete_fails

    async def get_input_entity(self, chat_id):
        self.resolved.append(chat_id)
        return ("peer", chat_id)

    async def delete_messages(self, chat_id, msg_ids):
        self.deleted.append((chat_id, tuple(msg_ids)))
        if self.delete_fails:
            raise RuntimeError("delete refused")
        return len(msg_ids)


@pytest.fixture
def bot(monkeypatch):
    fake = _FakeBot()
    monkeypatch.setattr(helper_client, "_client", fake)
    monkeypatch.setattr(helper_client, "_bot_id", BOT_ID)
    return fake


@pytest.fixture
def self_client():
    return _FakeSelfClient()


def _message(
    msg_id: int = 11,
    text: str = "",
    entities: list[Any] | None = None,
    *,
    chat_id: int = CHAT,
    sender_id: int = OWNER,
    out: bool = True,
    reply_to: int | None = None,
    media: Any = None,
    via_bot_id: int | None = None,
):
    return SimpleNamespace(
        id=msg_id,
        chat_id=chat_id,
        sender_id=sender_id,
        text=text,
        message=None,
        date=None,
        media=media,
        reply_to=SimpleNamespace(reply_to_msg_id=reply_to) if reply_to else None,
        out=out,
        entities=entities or [],
        via_bot_id=via_bot_id,
    )


async def _process(self_client, message, *, owner_id: int = OWNER):
    return await repl.process_outgoing_message(
        owner_id=owner_id,
        client=self_client,
        message=serialize_message(message),
        via_bot_id=getattr(message, "via_bot_id", None),
    )


# ── category / library seeding helpers ───────────────────────────────────────


def _lib(document_id: int = DOC, alt: str = "😀", owner: int = OWNER) -> dict:
    row = {
        "owner_id": owner,
        "document_id": document_id,
        "alt_text": alt,
        "source": "imported",
        "source_msg_id": 5,
        "created_at": "2026-10-06T10:00:00+00:00",
    }
    db_client._fallback["emoji_library"].append(row)
    return row


def _cat(name: str = "002") -> dict:
    result = _run(cat_service.create_category(OWNER, name))
    assert result["ok"], result
    return result["category"]


def _map(category_id: int, simple: str, document_id: int = DOC) -> None:
    result = _run(cat_service.create_mapping(OWNER, category_id, simple, document_id))
    assert result["ok"], result


def _active(category_id: int, *, chat_id: int | None = None) -> None:
    _run(state_service.set_replacement_enabled(OWNER, True))
    if chat_id is None:
        _run(state_service.set_global_default_category(OWNER, category_id))
    else:
        _run(state_service.set_chat_override(OWNER, chat_id, category_id))


@pytest.fixture
def wired():
    """Library + active category + one mapping, replacement ON."""
    _lib(DOC, "😀")
    cat = _cat()
    _map(cat["id"], KEY)
    _active(cat["id"])
    return cat


# ── 1. transformer: mapping-table replacement ────────────────────────────────

MAPPINGS = {KEY: {"document_id": DOC, "alt_text": "😀"}}


def test_transform_replaces_one_mapped_emoji():
    result = transform_message(f"hi {KEY}", [], MAPPINGS)
    assert result["ok"] is True
    assert result["changed"] == 1
    assert result["text"] == "hi 😀"
    assert result["entities"] == [
        {"type": "MessageEntityCustomEmoji", "offset": 3, "length": 2, "document_id": DOC},
    ]


def test_transform_same_key_maps_consistently_in_one_message():
    result = transform_message(f"{KEY} x {KEY}", [], MAPPINGS)
    assert result["changed"] == 2
    assert result["text"] == "😀 x 😀"
    assert [e["offset"] for e in result["entities"]] == [0, utf16_offset("😀 x ", 4)]
    assert all(e["document_id"] == DOC for e in result["entities"])


def test_transform_multiple_distinct_keys_in_one_pass():
    _lib(43001, "👋")
    result = transform_message(
        f"{KEY} a {OTHER_KEY}",
        [],
        {KEY: {"document_id": DOC, "alt_text": "😀"},
         OTHER_KEY: {"document_id": 43001, "alt_text": "👋"}},
    )
    assert result["changed"] == 2
    assert result["text"] == "😀 a 👋"
    assert [e["document_id"] for e in result["entities"]] == [DOC, 43001]


def test_transform_unmapped_emoji_passes_through_unchanged():
    result = transform_message(f"{KEY} 👋", [], MAPPINGS)
    assert result["text"] == "😀 👋"       # 👋 has no mapping: byte-identical
    assert result["changed"] == 1
    assert len(result["entities"]) == 1    # no entity is invented for 👋


def test_transform_ordinary_text_is_untouched():
    result = transform_message("hello world", [], MAPPINGS)
    assert result["ok"] is True
    assert result["changed"] == 0
    assert result["text"] == "hello world"


def test_transform_without_mappings_changes_nothing():
    result = transform_message(f"{KEY}", [], {})
    assert result["ok"] is True
    assert result["changed"] == 0
    assert result["text"] == KEY


def test_transform_is_deterministic_across_runs():
    first = transform_message(f"a {KEY} b {KEY}", [], MAPPINGS)
    second = transform_message(f"a {KEY} b {KEY}", [], MAPPINGS)
    assert first == second


def test_transform_longest_key_wins_at_a_position():
    family = "👨👩👧"
    result = transform_message(
        family,
        [],
        {
            "👨": {"document_id": 1, "alt_text": "A"},
            family: {"document_id": 2, "alt_text": "B"},
        },
    )
    assert result["changed"] == 1
    assert result["text"] == "B"
    assert result["entities"][0]["document_id"] == 2


# ── 1b. transformer: entity offsets ─────────────────────────────────────────


def _bold(offset: int, length: int) -> dict:
    return {"type": "MessageEntityBold", "offset": offset, "length": length}


def _tl_bold(offset: int, length: int):
    """A real Telethon entity object (what ``serialize_message`` consumes)."""
    from telethon.tl import types as tl_types

    return tl_types.MessageEntityBold(offset, length)


def test_transform_preserves_bold_covering_the_replaced_span():
    text = f"hi {KEY}!"
    entities = [_bold(0, utf16_length(text))]
    result = transform_message(text, entities, MAPPINGS)
    assert result["ok"] is True
    new_bold = result["entities"][0]
    assert new_bold["type"] == "MessageEntityBold"
    assert new_bold["offset"] == 0
    assert new_bold["length"] == utf16_length("hi 😀!")


def test_transform_recomputes_following_entity_offsets_when_alt_is_longer():
    # key occupies 2 UTF-16 units; its alt occupies 3 → +1 shift afterwards.
    result = transform_message(
        f"{KEY}x", [_bold(2, 1)], {KEY: {"document_id": DOC, "alt_text": "abc"}},
    )
    assert result["ok"] is True
    assert result["text"] == "abcx"
    assert result["entities"][0] == {
        "type": "MessageEntityBold", "offset": 3, "length": 1,
    }


def test_transform_recomputes_following_entity_offsets_when_alt_is_shorter():
    key = "⭐️"  # 2 UTF-16 units → alt "x" is 1 → −1 shift afterwards.
    result = transform_message(
        f"{key}x", [_bold(2, 1)], {key: {"document_id": DOC, "alt_text": "x"}},
    )
    assert result["ok"] is True
    assert result["text"] == "xx"
    assert result["entities"][0] == {
        "type": "MessageEntityBold", "offset": 1, "length": 1,
    }


def test_transform_mixed_script_offsets_stay_correct():
    text = f"سلام {KEY} دنیا"
    entities = [_bold(utf16_offset(text, text.index("دنیا")), utf16_length("دنیا"))]
    # The alt is one UTF-16 unit LONGER than the key, so everything after the
    # replaced span must shift by exactly that delta.
    result = transform_message(text, entities, {KEY: {"document_id": DOC, "alt_text": "😀!"}})
    assert result["ok"] is True
    expected_text = "سلام 😀! دنیا"
    assert result["text"] == expected_text
    assert result["entities"][0]["offset"] == utf16_offset(expected_text, expected_text.index("دنیا"))
    assert result["entities"][0]["offset"] == utf16_offset(text, text.index("دنیا")) + 1
    assert result["entities"][0]["length"] == utf16_length("دنیا")


def test_transform_preserves_entity_payloads():
    text = f"{KEY} link"
    entities = [{
        "type": "MessageEntityTextUrl",
        "offset": utf16_offset(text, text.index("link")),
        "length": utf16_length("link"),
        "url": "https://example.test",
    }]
    result = transform_message(text, entities, {KEY: {"document_id": DOC, "alt_text": "abc"}})
    assert result["text"] == "abc link"
    assert result["entities"][0]["url"] == "https://example.test"
    assert result["entities"][0]["offset"] == utf16_offset("abc link", "abc link".index("link"))
    assert result["entities"][0]["length"] == utf16_length("link")


def test_transform_does_not_mutate_the_caller_entities():
    entities = [_bold(0, utf16_length(f"x{KEY}"))]
    snapshot = [dict(e) for e in entities]
    transform_message(f"x{KEY}", entities, MAPPINGS)
    assert entities == snapshot


# ── 1c. transformer: custom emoji + fail-closed combinations ────────────────


def test_transform_never_rewrites_an_existing_custom_emoji_entity():
    text = f"😀 ok {KEY}"
    entities = [{
        "type": "MessageEntityCustomEmoji",
        "offset": 0, "length": 2, "document_id": 999,
    }]
    result = transform_message(text, entities, {
        "😀": {"document_id": DOC, "alt_text": "😀"},
        KEY: {"document_id": 43002, "alt_text": "🗿"},
    })
    assert result["ok"] is True
    assert result["changed"] == 1                 # only the trailing key
    assert result["text"] == "😀 ok 🗿"
    preserved = [e for e in result["entities"] if e["document_id"] == 999]
    assert preserved and preserved[0]["offset"] == 0 and preserved[0]["length"] == 2


def test_transform_bounds_mapped_spans():
    text = KEY * (MAX_REPLACEMENTS + 1)
    result = transform_message(text, [], MAPPINGS)
    assert result["ok"] is False
    assert result["error"] == E_TOO_MANY_REPLACEMENTS


def test_transform_fails_closed_on_partial_entity_overlap():
    result = transform_message(
        f"xy {KEY}",
        [_bold(0, 1)],                              # only "x" of the "xy" key
        {"xy": {"document_id": DOC, "alt_text": "😀"}},
    )
    assert result["ok"] is False
    assert result["error"] == E_UNSAFE_ENTITY


def test_transform_fails_closed_on_unsupported_entity_type():
    result = transform_message(f"{KEY}", [
        {"type": "MessageEntityNonsense", "offset": 0, "length": 2},
    ], MAPPINGS)
    assert result["ok"] is False
    assert result["error"] == E_UNSUPPORTED_ENTITY


def test_transform_fails_closed_on_unresolvable_offsets():
    result = transform_message(f"{KEY}", [_bold(0, 99)], MAPPINGS)
    assert result["ok"] is False
    assert result["error"] == E_MALFORMED_ENTITIES


def test_transform_fails_closed_on_mid_surrogate_offset():
    result = transform_message(f"{KEY}", [_bold(1, 1)], MAPPINGS)
    assert result["ok"] is False
    assert result["error"] == E_MALFORMED_ENTITIES


def test_transform_rejects_empty_text():
    result = transform_message("", [], MAPPINGS)
    assert result["ok"] is False
    assert result["error"] == E_INVALID_INPUT


def test_usable_mappings_drops_unresolvable_entries():
    table = usable_mappings({
        "ok": {"document_id": 5, "alt_text": "x"},
        "no-alt": {"document_id": 5, "alt_text": ""},
        "no-doc": {"document_id": 0, "alt_text": "x"},
        "bad-doc": {"document_id": "5", "alt_text": "x"},
        "": {"document_id": 5, "alt_text": "x"},
        "not-dict": "x",
    })
    assert table == {"ok": (5, "x")}


def test_transform_mapping_with_unusable_entry_leaves_text_untouched():
    result = transform_message(f"{KEY}", [], {
        KEY: {"document_id": 0, "alt_text": "😀"},
    })
    assert result["ok"] is True
    assert result["changed"] == 0
    assert result["text"] == KEY


# ── 2. resolution boundary (Phase 3 is the only authority) ──────────────────


def test_transformer_defers_to_the_called_in_resolution(monkeypatch, self_client):
    seen: list[tuple[int, int]] = []

    async def _record(owner_id, chat_id):
        seen.append((owner_id, chat_id))
        return None

    monkeypatch.setattr(state_service, "resolve_effective_category", _record)
    outcome = _run(_process(self_client, _message(text=f"hi {KEY}")))
    assert seen == [(OWNER, CHAT)]
    assert outcome["status"] == repl.STATUS_NO_CATEGORY
    assert outcome["replaced"] == 0


def test_replacement_off_means_no_transformation_and_no_side_effect(bot, self_client):
    _lib(DOC, "😀")
    cat = _cat()
    _map(cat["id"], KEY)
    _run(state_service.set_global_default_category(OWNER, cat["id"]))  # toggle stays OFF
    outcome = _run(_process(self_client, _message(text=f"hi {KEY}")))
    assert outcome["status"] == repl.STATUS_NO_CATEGORY
    assert bot.calls == []
    assert self_client.deleted == []


def test_no_effective_category_means_no_side_effect(bot, self_client):
    _lib(DOC, "😀")
    cat = _cat()
    _map(cat["id"], KEY)
    outcome = _run(_process(self_client, _message(text=f"hi {KEY}")))
    assert outcome["status"] == repl.STATUS_NO_CATEGORY
    assert bot.calls == []


def test_deleted_category_fails_closed(bot, self_client):
    _lib(DOC, "😀")
    cat = _cat()
    _map(cat["id"], KEY)
    _active(cat["id"])
    _run(cat_service.delete_category(OWNER, cat["id"]))
    outcome = _run(_process(self_client, _message(text=f"hi {KEY}")))
    assert outcome["status"] == repl.STATUS_NO_CATEGORY
    assert bot.calls == []
    assert self_client.deleted == []


def test_replacement_service_never_reimplements_resolution():
    source = Path(repl.__file__).read_text()
    for forbidden in ("get_global_default_category", "get_chat_override", "replacement_enabled"):
        assert forbidden not in source, forbidden


# ── 3. reconstruction through the existing bridge ───────────────────────────


def test_global_default_category_drives_the_replacement(bot, self_client, wired):
    outcome = _run(_process(self_client, _message(text=f"hi {KEY}")))
    assert outcome["status"] == repl.STATUS_REPLACED
    assert outcome["replaced"] == 1
    assert outcome["deleted"] is True
    assert bot.calls[0]["text"] == "hi 😀"
    assert self_client.resolved == [CHAT]           # same destination
    assert self_client.deleted == [(CHAT, (11,))]   # the original, exactly once


def test_per_chat_override_drives_the_replacement_and_global_is_untouched(bot, self_client):
    _lib(DOC, "😀")
    _lib(43003, "🗿")
    global_cat, override_cat = _cat("Global"), _cat("PerChat")
    _map(global_cat["id"], KEY, DOC)
    _map(override_cat["id"], KEY, 43003)
    _run(state_service.set_replacement_enabled(OWNER, True))
    _run(state_service.set_global_default_category(OWNER, global_cat["id"]))
    _run(state_service.set_chat_override(OWNER, CHAT, override_cat["id"]))

    _run(_process(self_client, _message(text=KEY)))
    assert bot.calls[0]["formatting_entities"][0].document_id == 43003

    _run(_process(self_client, _message(msg_id=12, text=KEY, chat_id=CHAT2)))
    assert bot.calls[1]["formatting_entities"][0].document_id == DOC


def test_reply_relationship_is_preserved(bot, self_client, wired):
    _run(_process(self_client, _message(text=KEY, reply_to=555)))
    assert bot.calls[0]["reply_to"] == 555


def test_message_without_reply_sends_without_one(bot, self_client, wired):
    _run(_process(self_client, _message(text=KEY)))
    assert bot.calls[0]["reply_to"] is None


def test_replacement_passes_custom_emoji_entities_to_the_bridge(bot, self_client, wired):
    from telethon.tl import types as tl_types

    _run(_process(self_client, _message(text=f"a {KEY}")))
    built = bot.calls[0]["formatting_entities"]
    assert len(built) == 1
    assert type(built[0]) is tl_types.MessageEntityCustomEmoji
    assert built[0].document_id == DOC
    assert built[0].offset == 2 and built[0].length == 2


def test_bold_formatting_travels_with_the_reconstructed_message(bot, self_client, wired):
    text = f"hi {KEY}!"
    _run(_process(self_client, _message(text=text, entities=[
        _tl_bold(0, utf16_length(text)),
    ])))
    built = bot.calls[0]["formatting_entities"]
    assert {type(e).__name__ for e in built} == {"MessageEntityBold", "MessageEntityCustomEmoji"}
    assert bot.calls[0]["text"] == "hi 😀!"


# ── 3b. fail-closed reconstruction ─────────────────────────────────────────


def test_no_mapped_emoji_leaves_the_message_untouched(bot, self_client, wired):
    outcome = _run(_process(self_client, _message(text="nothing to replace")))
    assert outcome["status"] == repl.STATUS_NO_EMOJI
    assert bot.calls == []
    assert self_client.deleted == []


def test_unmapped_emoji_only_message_is_untouched(bot, self_client, wired):
    outcome = _run(_process(self_client, _message(text="👋 🌍")))
    assert outcome["status"] == repl.STATUS_NO_EMOJI
    assert bot.calls == []


def test_category_without_mappings_is_untouched(bot, self_client):
    _lib(DOC, "😀")
    cat = _cat()
    _active(cat["id"])
    outcome = _run(_process(self_client, _message(text=f"hi {KEY}")))
    assert outcome["status"] == repl.STATUS_NO_MAPPINGS
    assert bot.calls == []


def test_mapping_with_deleted_library_entry_leaves_the_emoji_untouched(bot, self_client):
    _lib(DOC, "😀")
    cat = _cat()
    _map(cat["id"], KEY)
    _active(cat["id"])
    db_client._fallback["emoji_library"].clear()   # the referenced entry is gone
    outcome = _run(_process(self_client, _message(text=f"hi {KEY}")))
    assert outcome["status"] == repl.STATUS_NO_MAPPINGS
    assert bot.calls == []


def test_incomplete_mapping_listing_fails_closed(monkeypatch, bot, self_client, wired):
    async def _partial(owner_id, category_id, limit=50, offset=0):
        return [], 7

    monkeypatch.setattr(db_client, "list_emoji_mappings", _partial)
    outcome = _run(_process(self_client, _message(text=KEY)))
    assert outcome["status"] == repl.STATUS_NO_MAPPINGS
    assert "incomplete" in outcome["error"]
    assert bot.calls == []


def test_media_message_is_never_reconstructed(bot, self_client, wired):
    outcome = _run(_process(self_client, _message(
        text=f"hi {KEY}", media=SimpleNamespace(kind="photo"),
    )))
    assert outcome["status"] == repl.STATUS_MEDIA
    assert bot.calls == []
    assert self_client.deleted == []


def test_unsafe_entity_combination_is_not_reconstructed(bot, self_client):
    _lib(DOC, "😀")
    cat = _cat()
    _map(cat["id"], "hi")
    _active(cat["id"])
    outcome = _run(_process(self_client, _message(
        text="hi there", entities=[_tl_bold(0, 1)],  # covers only half the key
    )))
    assert outcome["status"] == repl.STATUS_UNSAFE
    assert outcome["error"] == E_UNSAFE_ENTITY
    assert bot.calls == []
    assert self_client.deleted == []


def test_bridge_unavailable_leaves_the_original_untouched(monkeypatch, self_client, wired):
    monkeypatch.setattr(helper_client, "_client", None)
    outcome = _run(_process(self_client, _message(text=KEY)))
    assert outcome["status"] == repl.STATUS_BRIDGE_UNAVAILABLE
    assert self_client.deleted == []


def test_delivery_failure_leaves_the_original_untouched(monkeypatch, self_client, wired):
    fake = _FakeBot(fail=RuntimeError("CUSTOM_EMOJI_INVALID"))
    monkeypatch.setattr(helper_client, "_client", fake)
    monkeypatch.setattr(helper_client, "_bot_id", BOT_ID)
    outcome = _run(_process(self_client, _message(text=KEY)))
    assert outcome["status"] == repl.STATUS_FAILED
    assert "CUSTOM_EMOJI_INVALID" in outcome["error"]
    assert self_client.deleted == []
    # No speculative entitlement work: exactly one attempt, no alt-text retry.
    assert len(fake.calls) == 1


def test_delete_failure_is_reported_as_an_honest_duplicate(monkeypatch, bot, wired):
    client = _FakeSelfClient(delete_fails=True)
    outcome = _run(_process(client, _message(text=KEY)))
    assert outcome["status"] == repl.STATUS_REPLACED_UNDELETED
    assert outcome["deleted"] is False
    assert outcome["sent_message_id"] == 9001
    assert "not deleted" in outcome["error"]


def test_bridge_send_that_returns_no_message_id_still_reports_honestly(
    monkeypatch, self_client, wired,
):
    class _NoIdBot(_FakeBot):
        async def send_message(self, peer, text, *, formatting_entities=None, reply_to=None):
            self.calls.append({"peer": peer, "text": text})
            return None

    monkeypatch.setattr(helper_client, "_client", _NoIdBot())
    monkeypatch.setattr(helper_client, "_bot_id", BOT_ID)
    outcome = _run(_process(self_client, _message(text=KEY)))
    assert outcome["status"] == repl.STATUS_REPLACED
    assert outcome["sent_message_id"] is None


# ── 4. owner boundary ───────────────────────────────────────────────────────


def test_foreign_author_is_never_processed(bot, self_client, wired):
    outcome = _run(_process(self_client, _message(
        text=KEY, sender_id=OTHER, out=False,
    )))
    assert outcome["status"] == repl.STATUS_OWNER_BOUNDARY
    assert bot.calls == []


def test_non_outgoing_message_is_never_processed(bot, self_client, wired):
    outcome = _run(_process(self_client, _message(text=KEY, out=False)))
    assert outcome["status"] == repl.STATUS_OWNER_BOUNDARY
    assert bot.calls == []


def test_invalid_owner_is_rejected(bot, self_client, wired):
    outcome = _run(_process(self_client, _message(text=KEY), owner_id=0))
    assert outcome["status"] == repl.STATUS_SKIPPED_INVALID
    assert bot.calls == []


def test_inline_bot_origin_message_is_skipped(bot, self_client, wired):
    outcome = _run(_process(self_client, _message(text=f"😀 **Emoji** {KEY}", via_bot_id=BOT_ID)))
    assert outcome["status"] == repl.STATUS_INLINE_ORIGIN
    assert bot.calls == []


def test_bridge_bot_authored_message_is_skipped(bot, self_client, wired):
    outcome = _run(_process(self_client, _message(text=KEY, sender_id=BOT_ID)))
    assert outcome["status"] == repl.STATUS_BRIDGE_ORIGIN
    assert bot.calls == []


def test_pending_panel_input_is_skipped(bot, self_client, wired):
    input_state.set_pending(OWNER, "panel_x", _noop_handler, CHAT, "prompt")
    outcome = _run(_process(self_client, _message(text=KEY)))
    assert outcome["status"] == repl.STATUS_PENDING_INPUT
    assert bot.calls == []


def test_message_in_another_chat_is_not_blocked_by_a_pending_input(bot, self_client, wired):
    input_state.set_pending(OWNER, "panel_x", _noop_handler, CHAT, "prompt")
    outcome = _run(_process(self_client, _message(msg_id=12, text=KEY, chat_id=CHAT2)))
    assert outcome["status"] == repl.STATUS_REPLACED
    assert len(bot.calls) == 1


async def _noop_handler(*args, **kwargs):
    return None


def test_message_without_text_is_skipped(bot, self_client, wired):
    outcome = _run(_process(self_client, _message(text="")))
    assert outcome["status"] == repl.STATUS_NO_TEXT
    assert bot.calls == []


def test_message_with_unusable_ids_is_skipped(bot, self_client, wired):
    outcome = _run(_process(self_client, _message(msg_id=0, text=KEY)))
    assert outcome["status"] == repl.STATUS_SKIPPED_INVALID
    assert bot.calls == []


# ── 5. loop prevention ──────────────────────────────────────────────────────


def test_reconstructed_message_cannot_reenter_the_pipeline(bot, self_client, wired):
    first = _run(_process(self_client, _message(text=KEY)))
    assert first["status"] == repl.STATUS_REPLACED
    sent_id = first["sent_message_id"]

    # As the self client actually observes it: an INCOMING message authored
    # by the bridge bot.
    reentered = _run(_process(self_client, _message(
        msg_id=sent_id, text="😀", sender_id=BOT_ID, out=False,
    )))
    assert reentered["status"] == repl.STATUS_BRIDGE_ORIGIN
    assert len(bot.calls) == 1            # no recursive reconstruction
    assert len(self_client.deleted) == 1


def test_recorded_reconstruction_is_skipped_even_for_the_owner_identity(bot, self_client, wired):
    first = _run(_process(self_client, _message(text=KEY)))
    reentered = _run(_process(self_client, _message(
        msg_id=first["sent_message_id"], text="😀",
    )))
    assert reentered["status"] == repl.STATUS_BRIDGE_ORIGIN
    assert len(bot.calls) == 1


def test_the_same_original_is_never_reconstructed_twice(bot, self_client, wired):
    _run(_process(self_client, _message(text=KEY)))
    again = _run(_process(self_client, _message(text=KEY)))
    assert again["status"] == repl.STATUS_DUPLICATE
    assert len(bot.calls) == 1
    assert len(self_client.deleted) == 1


def test_loop_guard_is_bounded():
    for i in range(repl.GUARD_MAX + 50):
        repl._guard_add(repl._bridge_origins, (CHAT, i + 1), time.monotonic())
    assert len(repl._bridge_origins) <= repl.GUARD_MAX


def test_loop_guard_entries_expire():
    repl._bridge_origins[(CHAT, 5)] = time.monotonic() - repl.GUARD_TTL_S - 1
    assert repl._guard_has(repl._bridge_origins, (CHAT, 5), time.monotonic()) is False


def test_loop_prevention_registries_are_in_memory_only():
    source = Path(repl.__file__).read_text()
    for forbidden in ("saved_items", "bot_logs", "INSERT INTO", "events."):
        assert forbidden not in source, forbidden


# ── 6. handler + architecture ───────────────────────────────────────────────


class _CapturingClient:
    """Captures ``on()`` registrations; delegates everything else to an
    optional real self-client surface (peer resolution + deletion)."""

    def __init__(self, inner: Any = None) -> None:
        self.handlers: list[tuple[Any, Any]] = []
        self._inner = inner

    def on(self, builder):
        def _decorator(callback):
            self.handlers.append((builder, callback))
            return callback

        return _decorator

    def __getattr__(self, name):
        inner = self.__dict__.get("_inner")
        if inner is None:
            raise AttributeError(name)
        return getattr(inner, name)


class _FakeEvent:
    def __init__(self, message) -> None:
        self.message = message
        self.sender_id = message.sender_id
        self.chat_id = message.chat_id
        self.raw_text = message.text


def _registered_handler():
    client = _CapturingClient()
    emoji_replacement.register(client, OWNER)
    assert len(client.handlers) == 1
    return client.handlers[0]


def test_handler_registers_exactly_one_outgoing_listener():
    builder, _callback = _registered_handler()
    assert builder.outgoing is True
    assert builder.incoming is False


def test_handler_is_the_only_telegram_listener_in_the_module():
    source = Path(emoji_replacement.__file__).read_text()
    assert source.count("@client.on(events.NewMessage(outgoing=True))") == 1
    for forbidden in ("events.MessageEdited", "events.Raw", "events.CallbackQuery", "TelegramClient"):
        assert forbidden not in source, forbidden


def test_handler_ignores_non_owner_events(monkeypatch):
    _builder, callback = _registered_handler()
    calls: list[Any] = []

    async def _record(**kwargs):
        calls.append(kwargs)
        return {}

    monkeypatch.setattr(emoji_replacement, "process_outgoing_message", _record)
    _run(callback(_FakeEvent(_message(text=KEY, sender_id=OTHER))))
    assert calls == []


def test_handler_runs_owner_messages_through_the_service(monkeypatch):
    _builder, callback = _registered_handler()
    calls: list[dict] = []

    async def _record(**kwargs):
        calls.append(kwargs)
        return {}

    monkeypatch.setattr(emoji_replacement, "process_outgoing_message", _record)
    message = _message(text=f"hi {KEY}", via_bot_id=None)
    _run(callback(_FakeEvent(message)))
    assert len(calls) == 1
    assert calls[0]["owner_id"] == OWNER
    assert calls[0]["message"]["text"] == f"hi {KEY}"
    assert calls[0]["message"]["chat_id"] == CHAT
    assert calls[0]["via_bot_id"] is None


def test_handler_swallows_service_errors(monkeypatch):
    _builder, callback = _registered_handler()

    async def _boom(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(emoji_replacement, "process_outgoing_message", _boom)
    _run(callback(_FakeEvent(_message(text=KEY))))  # must not raise


def test_handler_propagates_cancellation(monkeypatch):
    _builder, callback = _registered_handler()

    async def _cancel(**kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(emoji_replacement, "process_outgoing_message", _cancel)
    with pytest.raises(asyncio.CancelledError):
        _run(callback(_FakeEvent(_message(text=KEY))))


def test_router_registers_the_replacement_handler_last():
    from backend.bot import router

    source = Path(router.__file__).read_text()
    assert source.count('emoji_replacement"') == 1
    assert '"emoji_replacement", lambda: emoji_replacement.register(client, owner_id)' in source


def _imports_of(module_path: str) -> list[str]:
    tree = ast.parse(Path(module_path).read_text())
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


NEW_MODULES = (
    emoji_transformer.__file__,
    repl.__file__,
    emoji_replacement.__file__,
)


def test_phase4_modules_import_no_ai_and_no_second_infrastructure():
    for module_path in NEW_MODULES:
        imported = _imports_of(module_path)
        assert not any(n == "backend.ai" or n.startswith("backend.ai.") for n in imported), module_path
        for forbidden in (
            "backend.profile.scheduler",
            "backend.runtime.supervisor",
            "backend.runtime.task_guard",
            "backend.ai.tools.executor",
            "backend.helper.inline_engine",
        ):
            assert forbidden not in imported, (module_path, forbidden)


def test_phase4_modules_add_no_second_loop_scheduler_or_executor():
    for module_path in NEW_MODULES:
        source = Path(module_path).read_text()
        for forbidden in (
            "TelegramClient",
            "run_until_disconnected",
            "create_task",
            "immortal_create_task",
            "guarded_create_task",
            "asyncio.Lock",
            "forward_messages",
            "SendMessagesRequest",
        ):
            assert forbidden not in source, (module_path, forbidden)
    # The single listener lives in the handler module and is the existing
    # outgoing update path — the service/transformer register nothing at all.
    assert "events." not in Path(repl.__file__).read_text()
    assert "events." not in Path(emoji_transformer.__file__).read_text()


def test_phase4_modules_use_no_regex_or_keyword_routing():
    for module_path in NEW_MODULES:
        source = Path(module_path).read_text()
        assert "\nimport re\n" not in source, module_path
        assert "re.match" not in source and "re.search" not in source and "re.sub" not in source


def test_phase4_modules_contain_no_phase5_or_phase6_surface():
    for module_path in NEW_MODULES:
        source = Path(module_path).read_text().lower()
        for forbidden in (
            "sendreactionrequest", "reactionemoji", "custom_compose",
            "compose_reference", "is_custom", "notification", "analytics",
        ):
            assert forbidden not in source, (module_path, forbidden)


def test_service_deletes_through_the_existing_telegram_facade():
    source = Path(repl.__file__).read_text()
    assert "from backend.telegram_api.messages import delete_messages" in source
    assert "from backend.telegram_api.bridge import" in source


def test_end_to_end_owner_message_becomes_a_bridge_delivery(bot, self_client, wired):
    # The full Phase 4 path with the real serializer, real transformer, real
    # bridge and real deletion facade — only Telegram itself is faked.
    client = _CapturingClient(self_client)
    emoji_replacement.register(client, OWNER)
    _builder, callback = client.handlers[0]
    message = _message(text=f"hi {KEY}", reply_to=42)
    _run(callback(_FakeEvent(message)))

    assert len(bot.calls) == 1
    assert bot.calls[0]["text"] == "hi 😀"
    assert bot.calls[0]["reply_to"] == 42
    assert self_client.resolved == [CHAT]
    assert self_client.deleted == [(CHAT, (11,))]
