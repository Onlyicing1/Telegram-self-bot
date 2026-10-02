"""Regression tests for Saved-Item PREVIEW metadata authority.

Preview must return the metadata that was ALREADY STORED for the item named by
the save code. It is never a place that composes, re-fetches, or repairs a
metadata snapshot:

    save_code
      → retrieve_service.load_saved_item(save_code, owner_id)
          → db_client.query_save(save_code)          (the persisted row)
          → owner isolation + row identity
      → retrieve_service.format_preview(row)
      → the owner sees the stored values

The two defects these tests pin:

1. ``preview_save`` was not a verbatim read tool, so a native tool call ran a
   continuation provider round and the MODEL re-stated the metadata in its own
   words — freshly composed values instead of the stored ones.
2. A save-code preview request was answered with the generic recent-saves
   listing, so the requested row was never read. Tool selection is now the
   MODEL's decision: it reads the save code and emits ``preview_save``.

No live Telegram, no Supabase, no providers: the DB boundary is faked exactly
where the service layer touches it, and Telegram access during preview is a
hard failure (``_TelegramTrap``).
"""
from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.ai.tools.context import ToolContext
from backend.ai.tools.executor import ToolExecutionResult, ToolExecutor
from backend.ai.tools.registry import create_default_registry
from backend.db import client as db_client
from backend.services import retrieve_service


OWNER = 777
OTHER_OWNER = 999
CHAT = -100123


# One complete persisted row. Every value is distinctive so a field read from
# the wrong column (or from another layer) cannot pass unnoticed.
STORED_ROW = {
    "id": 41,
    "save_code": "S0001",
    "owner_id": OWNER,
    "media_type": "Photo",
    "mime_type": "image/jpeg",
    "file_size": 173_800,
    "file_id": "AQAD-stored-file-id",
    "sender_name": "stored-sender",
    "sender_id": 4242,
    "origin_chat_id": -1009999,
    "origin_msg_id": 4321,
    "saved_chat_id": OWNER,
    "saved_msg_id": 77,
    "caption": "stored caption",
    "created_at": "2026-09-15T10:08:00+00:00",
}


def _row(**overrides):
    return {**STORED_ROW, **overrides}


class _TelegramTrap:
    """Reading the stored row never needs Telegram; touching it must fail."""

    def __getattr__(self, name):  # pragma: no cover - only reached on a bug
        raise AssertionError(f"preview touched Telegram: {name}")


async def _preview(row_or_error, save_code: str = "S0001", owner_id: int = OWNER):
    """Run the real ``do_preview`` against a faked DB boundary."""
    if isinstance(row_or_error, Exception):
        lookup = AsyncMock(side_effect=row_or_error)
    else:
        lookup = AsyncMock(return_value=row_or_error)
    with (
        patch.object(db_client, "query_save", lookup),
        patch.object(db_client, "log", AsyncMock()),
    ):
        text = await retrieve_service.do_preview(_TelegramTrap(), owner_id, save_code)
        return text, lookup


# ── the persisted row IS the preview's source of truth ──────────────────────


@pytest.mark.asyncio
async def test_preview_renders_each_field_from_its_persisted_column():
    text, lookup = await _preview(_row())

    lookup.assert_awaited_once_with("S0001")
    assert text == retrieve_service.format_preview(_row())
    for fragment in (
        "**Type** Photo",
        "**Format** `image/jpeg`",
        "**Size** 169.7 KB",
        "**Sender** stored-sender",
        "**Saved** 2026-09-15 10:08",
        "`S0001`",
    ):
        assert fragment in text, fragment


@pytest.mark.asyncio
async def test_preview_is_read_only_and_never_mutates_the_stored_row():
    row = _row()
    writes = {
        name: AsyncMock()
        for name in ("insert_save", "update_save_field", "delete_save_row", "delete_save")
    }
    with patch.object(db_client, "query_save", AsyncMock(return_value=row)):
        with patch.object(db_client, "log", AsyncMock()):
            with patch.object(db_client, "insert_save", writes["insert_save"]):
                with patch.object(db_client, "update_save_field", writes["update_save_field"]):
                    with patch.object(db_client, "delete_save_row", writes["delete_save_row"]):
                        with patch.object(db_client, "delete_save", writes["delete_save"]):
                            text = await retrieve_service.do_preview(
                                _TelegramTrap(), OWNER, "S0001"
                            )

    for name, mock in writes.items():
        mock.assert_not_awaited()
    assert row == _row()  # the persisted record was not rewritten in place
    assert "stored-sender" in text


@pytest.mark.asyncio
async def test_preview_does_not_re_fetch_or_re_derive_metadata_from_telegram():
    """A booby-trapped client proves no Telegram round-trip happens."""
    text, _ = await _preview(_row())

    assert "**Sender** stored-sender" in text
    assert "**Size** 169.7 KB" in text
    # The service path that produces the preview text contains no Telegram RPC.
    assert "get_messages" not in inspect.getsource(retrieve_service.do_preview)
    assert "get_entity" not in inspect.getsource(retrieve_service.do_preview)


@pytest.mark.asyncio
async def test_load_saved_item_returns_the_same_persisted_row_object():
    row = _row()
    with patch.object(db_client, "query_save", AsyncMock(return_value=row)):
        loaded = await retrieve_service.load_saved_item("s0001", OWNER)

    assert loaded is row


@pytest.mark.asyncio
async def test_preview_is_produced_by_the_shared_owner_scoped_lookup():
    """Non-vacuous guard: preview reads THROUGH ``load_saved_item``.

    Replacing the lookup result proves preview cannot silently build its own
    metadata snapshot instead of reading the stored row.
    """
    with patch.object(retrieve_service, "load_saved_item", AsyncMock(return_value=None)):
        text = await retrieve_service.do_preview(_TelegramTrap(), OWNER, "S0001")

    assert text == "❌ No item found for `S0001`"


# ── historical / incomplete metadata is reported, never manufactured ────────


@pytest.mark.asyncio
async def test_a_historical_sender_value_is_displayed_exactly_as_stored():
    """Old rows keep their stored value — preview never repairs history."""
    legacy = _row(sender_name="-1001234567")
    text, _ = await _preview(legacy)

    assert "**Sender** -1001234567" in text


@pytest.mark.asyncio
async def test_the_origin_chat_is_never_substituted_for_the_sender():
    text, _ = await _preview(_row(sender_name=None))

    assert "**Sender** —" in text
    assert str(STORED_ROW["origin_chat_id"]) not in text


@pytest.mark.asyncio
async def test_missing_metadata_is_honest_instead_of_fabricated():
    empty = _row(
        media_type=None, mime_type=None, file_size=None,
        sender_name=None, created_at=None,
    )
    text, _ = await _preview(empty)

    assert "**Type** —" in text
    assert "**Format** `—`" in text
    assert "**Size** —" in text
    assert "**Sender** —" in text
    assert "**Saved** —" in text
    for fabricated in ("stored-sender", "image/jpeg", "169.7", "2026-"):
        assert fabricated not in text


# ── owner isolation ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_preview_never_reads_another_owners_row():
    log = AsyncMock()
    text, _ = await _preview(_row(owner_id=OTHER_OWNER))
    assert text == "❌ No item found for `S0001`"

    with (
        patch.object(db_client, "query_save", AsyncMock(return_value=_row(owner_id=OTHER_OWNER))),
        patch.object(db_client, "log", log),
    ):
        text = await retrieve_service.do_preview(_TelegramTrap(), OWNER, "S0001")

    assert text == "❌ No item found for `S0001`"
    log.assert_not_awaited()


@pytest.mark.asyncio
async def test_preview_refuses_a_row_that_belongs_to_another_code():
    text, _ = await _preview(_row(save_code="S0009"))

    assert text == "❌ No item found for `S0001`"
    assert "stored-sender" not in text


@pytest.mark.asyncio
async def test_a_db_failure_is_reported_as_a_failure_not_empty_metadata():
    text, _ = await _preview(RuntimeError("boom"))

    assert text == "❌ DB error: boom"

    with patch.object(db_client, "query_save", AsyncMock(side_effect=RuntimeError("boom"))):
        with pytest.raises(RuntimeError):
            await retrieve_service.load_saved_item("S0001", OWNER)


# ── the Glass UI item panel uses the same authoritative row ────────────────


@pytest.mark.asyncio
async def test_the_item_panel_renders_the_owner_scoped_stored_row():
    from backend.bot.handlers import retrieve as retrieve_handler

    with patch.object(
        retrieve_service, "load_saved_item", AsyncMock(return_value=_row())
    ) as lookup:
        title, body, buttons = await retrieve_handler._retrieve_item_panel_handler(None, "id:S0001")

    assert title == "Item Preview"
    assert body == retrieve_service.format_preview(_row())
    assert lookup.await_args.args[0] == "S0001"
    assert buttons  # the item still offers Retrieve/Rename/Move/Delete


@pytest.mark.asyncio
async def test_the_item_panel_never_renders_a_foreign_or_missing_row():
    from backend.bot.handlers import retrieve as retrieve_handler

    with patch.object(retrieve_service, "load_saved_item", AsyncMock(return_value=None)):
        _title, body, _buttons = await retrieve_handler._retrieve_item_panel_handler(None, "id:S0001")

    assert body == "❌ No item found for `S0001`"


# ── deterministic routing: one item's code never becomes the listing ───────


# ── the preview result is delivered verbatim, not re-composed ──────────────


def test_preview_save_is_a_verbatim_read_tool():
    from backend.ai.engine.dispatcher import Dispatcher, _VERBATIM_READ_TOOLS

    assert "preview_save" in _VERBATIM_READ_TOOLS

    ok = [ToolExecutionResult(
        tool_name="preview_save", success=True, message="**Sender** stored-sender",
    )]
    assert Dispatcher._read_results_authoritative([{"name": "preview_save"}], ok) is True

    failed = [ToolExecutionResult(
        tool_name="preview_save", success=False, message="", error="db down",
    )]
    assert Dispatcher._read_results_authoritative([{"name": "preview_save"}], failed) is False
