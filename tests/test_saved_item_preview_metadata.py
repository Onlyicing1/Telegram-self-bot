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
2. ``parse_command_intent`` answered an explicit save-code preview request with
   the generic recent-saves listing, so the requested row was never read.

No live Telegram, no Supabase, no providers: the DB boundary is faked exactly
where the service layer touches it, and Telegram access during preview is a
hard failure (``_TelegramTrap``).
"""
from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.ai.actions import parse_command_intent
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


@pytest.mark.parametrize(
    "request_text",
    [
        "مشخصات سیو S0001 رو بده",
        "مشخصات S0001 چیه",
        "اطلاعات سیو S0001",
        "جزئیات S0001 رو نشونم بده",
        "پیش‌نمایش S0001",
        "preview S0001",
        "show details of S0001",
        "saved item S0001 details",
    ],
)
def test_an_explicit_save_code_routes_to_the_preview_tool(request_text):
    result = parse_command_intent(request_text, has_reply=False)

    assert result.kind == "executable", request_text
    assert result.action == "preview_saved_item", request_text
    assert result.target == "saved_item", request_text
    assert result.save_code == "S0001", request_text
    assert result.tool_calls == [
        {"name": "preview_save", "arguments": {"save_code": "S0001"}}
    ], request_text


def test_preview_routing_leaves_every_existing_intent_alone():
    listed = parse_command_intent("لیست سیوها رو بده", has_reply=False)
    assert listed.action == "list_saved_items"
    assert listed.tool_calls == [{"name": "list_saves", "arguments": {}}]

    for list_request in (
        "چه چیزایی سیو دارم؟",
        "وضعیت سیوها چیه؟",
        "list my saved items",
        "saved items",
    ):
        assert parse_command_intent(list_request, has_reply=False).action == "list_saved_items", (
            list_request
        )

    deleted = parse_command_intent("سیو S0001 رو پاک کن", has_reply=False)
    assert deleted.tool_calls == [{"name": "delete_save", "arguments": {"save_code": "S0001"}}]

    retrieved = parse_command_intent("S0001 رو بفرست", has_reply=False)
    assert retrieved.tool_calls == [
        {"name": "retrieve_save", "arguments": {"save_code": "S0001"}}
    ]

    saved = parse_command_intent("اینو سیو کن", has_reply=True)
    assert saved.tool_calls == [{"name": "save", "arguments": {}}]


def test_a_preview_request_without_a_code_stays_off_the_deterministic_route():
    result = parse_command_intent("مشخصات این سیو چیه؟", has_reply=False)

    assert result.kind == "conversational"
    assert result.tool_calls == []


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


@pytest.mark.asyncio
async def test_a_preview_request_returns_the_stored_metadata_without_a_provider_round():
    """Parser → real ToolExecutor → PreviewSaveTool → service → stored row."""
    from backend.ai.engine.dispatcher import Dispatcher
    from backend.ai.engine.hooks import NOOP_HOOKS
    from backend.ai.engine.metrics import EngineMetrics
    from backend.ai.session.request import AIRequest

    ctx = ToolContext(
        telegram=MagicMock(client=_TelegramTrap()),
        owner_id=OWNER,
        tz_str="UTC",
        extra={"chat_id": CHAT, "request_id": "preview-metadata"},
    )
    executor = ToolExecutor(create_default_registry(ctx), ctx)

    mock_pm = MagicMock()
    mock_pm.get_active_name.return_value = "test"
    mock_pm.get_active.return_value.config.model = "m"
    mock_pm.get_active.return_value.health.return_value = {"healthy": True}
    mock_pm.get_active.return_value.chat = AsyncMock()

    mock_conv = MagicMock()
    session = MagicMock()
    session.session_id = "s"
    session.owner_id = OWNER
    session.active_provider = "test"
    mock_conv.get_session.return_value = session
    mock_conv.restore_history = AsyncMock()
    mock_conv.get_history.return_value = []

    mock_pb = MagicMock()
    dispatcher = Dispatcher(
        mock_conv, mock_pb, mock_pm, NOOP_HOOKS, EngineMetrics(), tool_executor=executor,
    )

    with (
        patch.object(db_client, "query_save", AsyncMock(return_value=_row())),
        patch.object(db_client, "log", AsyncMock()),
    ):
        result = await dispatcher.dispatch(AIRequest(
            session_id="s", message_id=57500, owner_id=OWNER,
            user_message="مشخصات سیو S0001 رو بده", chat_id=CHAT,
        ))

    assert result.success is True
    assert result.response == retrieve_service.format_preview(_row())
    assert "**Sender** stored-sender" in result.response
    assert "**Size** 169.7 KB" in result.response

    # No provider round, no prompt: the stored metadata is the deliverable.
    mock_pm.get_active.return_value.chat.assert_not_awaited()
    mock_pb.build.assert_not_called()
    assert result.metadata.get("finish_state") == "local_fast_path"
