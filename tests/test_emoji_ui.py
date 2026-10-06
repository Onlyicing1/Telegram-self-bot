"""Emoji Library Glass UI — Phase 1 remainder (Emoji & Reaction).

Pins the panel surface: registration through the standard panel machinery,
the mother-menu entry, the main panel counters, the 2×5 library browser with
its pagination, the honest entry detail, and the import action that runs the
REAL bounded service through the self client and renders its report
verbatim — success, degraded and failed states alike.

Everything runs offline: panels are driven directly (no Telegram) and the
Telegram boundary is faked at the TL-request surface.
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
from telethon.tl.types import DocumentAttributeCustomEmoji, InputStickerSetID

from backend.bot.handlers import emoji, misc
from backend.db import client as db_client
from backend.helper import inline_engine

OWNER = 7770001
SET_A = InputStickerSetID(id=11, access_hash=1100)

_FALLBACK_KEY = "emoji_library"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    db_client._fallback[_FALLBACK_KEY] = []
    inline_engine.set_owner_id(OWNER)
    yield
    db_client._fallback[_FALLBACK_KEY] = []


def _datas(buttons) -> list[str]:
    out = []
    for row in buttons:
        for button in row:
            raw = getattr(button, "data", button)
            out.append(raw.decode("utf-8") if isinstance(raw, bytes) else str(raw))
    return out


def _texts(buttons) -> list[str]:
    return [str(getattr(button, "text", button)) for row in buttons for button in row]


def _seed(entries: list[dict[str, Any]]) -> None:
    db_client._fallback[_FALLBACK_KEY].extend(entries)


def _entry(doc_id: int, alt: str = "", msg_id: int | None = None) -> dict[str, Any]:
    return {
        "document_id": doc_id,
        "alt_text": alt,
        "source_msg_id": msg_id,
        "source": "imported",
        "owner_id": OWNER,
        "created_at": "2026-10-06T10:00:00+00:00",
    }


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── registration ──────────────────────────────────────────────────────────────


def test_registers_panels_and_the_import_action():
    emoji.register(client=None, owner_id=OWNER)
    from backend.helper.panel_registry import registry as get_registry
    from backend.helper.panels import get_action

    for panel_id in ("emoji", "emoji_library", "emoji_entry"):
        assert get_registry().get_handler(panel_id) is not None
    assert get_action("emoji_import") is not None


def test_the_mother_menu_links_the_emoji_panel():
    assert "panel:emoji" in _datas(misc._build_menu_buttons())


def test_panel_callback_data_stays_bounded():
    emoji.register(client=None, owner_id=OWNER)
    _seed([_entry(i, f"e{i}") for i in range(12)])
    _title, _body, buttons = _run(emoji._library_page_handler(None, "0"))
    for data in _datas(buttons):
        assert len(data.encode("utf-8")) <= 64


# ── main panel ────────────────────────────────────────────────────────────────


def test_main_panel_shows_library_size_and_actions():
    _seed([_entry(1), _entry(2)])
    title, body, buttons = _run(emoji._emoji_panel_handler(None, ""))
    assert title == "Emoji"
    assert "2" in body
    datas = _datas(buttons)
    assert "action:emoji_import" in datas
    assert "panel:emoji_library" in datas


def test_main_panel_empty_state_offers_the_import():
    _title, body, buttons = _run(emoji._emoji_panel_handler(None, ""))
    assert "Empty" in body
    assert "action:emoji_import" in _datas(buttons)


# ── library browser (2×5 grid) ────────────────────────────────────────────────


def test_library_first_page_shows_a_2x5_grid():
    _seed([_entry(i, f"e{i}") for i in range(12)])
    _title, _body, buttons = _run(emoji._library_page_handler(None, "0"))
    datas = _datas(buttons)
    entry_buttons = [d for d in datas if d.startswith("panel:emoji_entry:")]
    assert len(entry_buttons) == 10  # 2 columns × 5 rows
    assert "panel:emoji_entry:0:0" in entry_buttons
    assert "panel:emoji_entry:0:9" in entry_buttons
    assert "panel:emoji_entry:0:10" not in entry_buttons
    assert "panel:emoji_library:1" in datas  # the pager points at page 2
    texts = _texts(buttons)
    assert any("e0" in t for t in texts) and any("e9" in t for t in texts)


def test_library_second_page_shows_the_remainder():
    _seed([_entry(i, f"e{i}") for i in range(12)])
    _title, body, buttons = _run(emoji._library_page_handler(None, "1"))
    entry_buttons = [
        d for d in _datas(buttons) if d.startswith("panel:emoji_entry:")
    ]
    assert entry_buttons == ["panel:emoji_entry:1:0", "panel:emoji_entry:1:1"]
    assert "page 2/2" in body


def test_library_page_is_clamped_onto_the_current_total():
    _seed([_entry(1)])
    _title, _body, buttons = _run(emoji._library_page_handler(None, "99"))
    assert "panel:emoji_entry:0:0" in _datas(buttons)


def test_library_empty_state_is_honest():
    title, body, buttons = _run(emoji._library_page_handler(None, "0"))
    assert title == "Library"
    assert "Empty" in body
    assert not [d for d in _datas(buttons) if d.startswith("panel:emoji_entry:")]


def test_library_labels_fall_back_when_alt_text_is_absent():
    _seed([_entry(1, "")])
    _title, _body, buttons = _run(emoji._library_page_handler(None, "0"))
    assert "·" in _texts(buttons)


# ── entry detail ──────────────────────────────────────────────────────────────


def test_entry_detail_shows_only_what_the_row_actually_has():
    _seed([_entry(42, "😀", msg_id=1234)])
    title, body, _buttons = _run(emoji._entry_page_handler(None, "0:0"))
    assert title == "Entry"
    assert "😀" in body
    assert "42" in body
    assert "Saved Messages #1234" in body
    assert "2026-10-06" in body


def test_entry_detail_marks_set_members_honestly():
    _seed([_entry(7, "wave", msg_id=None)])
    _title, body, _buttons = _run(emoji._entry_page_handler(None, "0:0"))
    assert "Set scan" in body


def test_entry_detail_fails_honestly_on_a_stale_index():
    _seed([_entry(1)])
    _title, body, _buttons = _run(emoji._entry_page_handler(None, "0:9"))
    assert "not found" in body.lower()


# ── import action + honest report ───────────────────────────────────────────


def _doc(doc_id: int, alt: str = "😀", stickerset: Any = SET_A):
    return SimpleNamespace(
        id=doc_id,
        attributes=[DocumentAttributeCustomEmoji(alt=alt, stickerset=stickerset)],
    )


def _emoji_message(msg_id: int, doc_id: int, emoji_char: str = "😀"):
    from backend.telegram_api._helpers import utf16_length, utf16_offset
    from telethon.tl.types import MessageEntityCustomEmoji

    text = f"m{msg_id} {emoji_char}"
    return SimpleNamespace(
        id=msg_id,
        message=text,
        entities=[
            MessageEntityCustomEmoji(
                utf16_offset(text, text.index(emoji_char)),
                utf16_length(emoji_char),
                doc_id,
            )
        ],
    )


class _FakeTlClient:
    """Minimal self-client fake: the Saved Messages scan plus the two TL
    requests the enrichment needs."""

    def __init__(self, *, documents=None, sets=None):
        self._documents = documents or []
        self._sets = sets or {}

    def iter_messages(self, chat_id, **kwargs):
        async def _gen():
            yield _emoji_message(5, 20001)

        return _gen()

    async def __call__(self, request):
        if isinstance(request, GetCustomEmojiDocumentsRequest):
            wanted = set(request.document_id)
            return [d for d in self._documents if d.id in wanted]
        if isinstance(request, GetStickerSetRequest):
            ref = request.stickerset
            result = self._sets.get((ref.id, ref.access_hash))
            if result is None:
                raise RuntimeError("SET_ID_INVALID")
            return result
        raise AssertionError(type(request).__name__)


def _set_result(members):
    return SimpleNamespace(
        set=SimpleNamespace(
            id=11,
            access_hash=1100,
            title="cats",
            short_name="cats",
            count=len(members),
        ),
        documents=members,
    )


def test_import_action_runs_the_real_service_and_renders_success(monkeypatch):
    client = _FakeTlClient(
        documents=[_doc(20001, "😀", SET_A)],
        sets={(11, 1100): _set_result([_doc(20001), _doc(77, "smile")])},
    )
    monkeypatch.setattr(emoji, "get_self_client", lambda: client)
    title, body, buttons = _run(emoji._import_action(None, "", 0))
    assert title == "Emoji Import"
    assert "✓ Import complete" in body
    assert "Imported 1" in body
    assert "+1" in body  # the deduplicated set member
    _rows, total = _run(db_client.list_emoji_entries(OWNER, limit=10, offset=0))
    assert total == 2
    assert "panel:emoji_library" in _datas(buttons)


def test_import_action_reports_degradation_honestly(monkeypatch):
    client = _FakeTlClient(documents=[_doc(20001, "😀", SET_A)], sets={})
    monkeypatch.setattr(emoji, "get_self_client", lambda: client)
    _title, body, _buttons = _run(emoji._import_action(None, "", 0))
    assert "◌" in body          # degraded, not silently successful
    assert "enumeration failed" in body


def test_import_action_without_a_self_client_fails_honestly(monkeypatch):
    monkeypatch.setattr(emoji, "get_self_client", lambda: None)
    _title, body, _buttons = _run(emoji._import_action(None, "", 0))
    assert "!" in body
    assert "not connected" in body.lower()


def test_report_renderer_covers_failure_and_budget_states():
    failed = emoji._render_report(
        {"ok": False, "error": "telegram page fetch timed out after 15s"}
    )
    assert "timed out" in failed[1]
    assert "✗" in failed[1]

    budget = emoji._render_report(
        {
            "ok": True,
            "error": None,
            "degraded": False,
            "scanned_messages": 200,
            "custom_emoji_seen": 3,
            "malformed_entities": 0,
            "imported": 3,
            "duplicates": 0,
            "failed": 0,
            "library_total": 3,
            "end_reached": False,
            "hit_scan_limit": True,
            "hit_record_limit": False,
            "hit_entity_limit": False,
            "sets_resolved": 0,
        }
    )
    assert "Scan budget reached" in budget[1]
    assert "run Import again" in budget[1]


def test_the_emoji_module_adds_no_second_infrastructure():
    import inspect
    from pathlib import Path

    source = Path(inspect.getfile(emoji)).read_text()
    for forbidden in (
        "TelegramClient",
        "run_until_disconnected",
        "create_task",
        "immortal_create_task",
        "forward_messages",
        "events.NewMessage",
        "asyncio.Lock",
    ):
        assert forbidden not in source, forbidden
