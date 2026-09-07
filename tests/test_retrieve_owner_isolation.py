"""Owner-isolation regression tests for saved-item retrieval.

The Retrieval/File-Retrieval audit proved that ``retrieve_service.do_retrieve``
accepted a save code and forwarded the saved media without verifying that the
``saved_items`` row belongs to the trusted owner — while do_rename/do_move/
do_delete already enforced ``row["owner_id"] == owner_id``. These tests pin
the fix: the ownership check happens BEFORE any Telegram side effect
(entity resolution or forwarding), cross-owner and missing-owner retrieval
fail closed with the established not-found wording, and the authorized path
is byte-identical to the pre-fix behavior.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.ai.tools.context import ToolContext
from backend.ai.tools.executor import ToolExecutor
from backend.ai.tools.registry import create_default_registry
from backend.db import client as db_client
from backend.services import retrieve_service


OWNER = 777
OTHER_OWNER = 999
CHAT = -100123
CODE = "S0001"


class FakeTelegram:
    def __init__(self):
        self.client = MagicMock()


def _seed_row(owner_id=OWNER, code=CODE):
    row = {
        "id": 1,
        "save_code": code,
        "save_type": "deep",
        "origin_chat_id": 100,
        "origin_msg_id": 200,
        "saved_chat_id": 300,
        "saved_msg_id": 400,
        "mime_type": "image/jpeg",
        "file_size": 1234,
        "media_type": "Photo",
        "caption": "cap",
        "owner_id": owner_id,
    }
    db_client._fallback["saved_items"].append(row)
    return row


def _ctx(owner_id=OWNER):
    return ToolContext(
        telegram=FakeTelegram(), owner_id=owner_id, tz_str="UTC",
        extra={"chat_id": CHAT, "request_id": "owner-isolation"},
    )


def _registry_executor(ctx):
    registry = create_default_registry(ctx)
    return registry, ToolExecutor(registry, ctx)


async def _run_tool(executor, ctx, name, arguments):
    results = await executor.execute_calls(
        [{"name": name, "arguments": arguments}],
        owner_id=ctx.owner_id, session_id="owner-isolation", context_override=ctx,
    )
    return results[0]


@pytest.fixture(autouse=True)
def reset_fallback():
    db_client._fallback["saved_items"] = []
    db_client._fallback["bio_state"] = {}
    db_client._fallback["bot_logs"] = []
    db_client._fallback["username_state"] = {}
    yield
    db_client._fallback["saved_items"] = []


# ── service-level ownership boundary ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_owner_can_retrieve_own_saved_item():
    _seed_row(owner_id=OWNER)
    client = MagicMock()
    client.get_input_entity = AsyncMock(side_effect=lambda e: e)
    client.forward_messages = AsyncMock(return_value=MagicMock(id=555))
    client.edit_message = AsyncMock()

    result = await retrieve_service.do_retrieve(client, OWNER, CODE, CHAT)

    assert result.startswith("✅")
    client.forward_messages.assert_awaited_once()


@pytest.mark.asyncio
async def test_cross_owner_retrieve_fails_without_telegram_side_effects():
    _seed_row(owner_id=OTHER_OWNER)
    client = MagicMock()
    client.get_input_entity = AsyncMock()
    client.forward_messages = AsyncMock()
    client.edit_message = AsyncMock()

    result = await retrieve_service.do_retrieve(client, OWNER, CODE, CHAT)

    assert result.startswith("❌")
    assert "No item found" in result
    # Fail closed: no entity resolution, no forwarding, no caption edit.
    client.get_input_entity.assert_not_awaited()
    client.forward_messages.assert_not_awaited()
    client.edit_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_owner_information_fails_closed():
    # A row without usable owner_id (e.g. legacy/corrupt row) must never be
    # retrievable: comparison against the trusted owner fails closed.
    row = _seed_row(owner_id=OWNER)
    row.pop("owner_id")
    client = MagicMock()
    client.get_input_entity = AsyncMock()
    client.forward_messages = AsyncMock()

    result = await retrieve_service.do_retrieve(client, OWNER, CODE, CHAT)

    assert result.startswith("❌")
    client.get_input_entity.assert_not_awaited()
    client.forward_messages.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_code_fails_without_telegram_side_effects():
    client = MagicMock()
    client.get_input_entity = AsyncMock()
    client.forward_messages = AsyncMock()

    result = await retrieve_service.do_retrieve(client, OWNER, "S9999", CHAT)

    assert result.startswith("❌")
    client.get_input_entity.assert_not_awaited()
    client.forward_messages.assert_not_awaited()


@pytest.mark.asyncio
async def test_cross_owner_message_is_indistinguishable_from_missing():
    """The other owner's row is not exposed: same wording as a missing code."""
    _seed_row(owner_id=OTHER_OWNER)
    result = await retrieve_service.do_retrieve(MagicMock(), OWNER, CODE, CHAT)
    missing = await retrieve_service.do_retrieve(MagicMock(), OWNER, "S9999", CHAT)
    # Same failure template, no data leak (no mime/media/sender in either).
    for message in (result, missing):
        assert message.startswith("❌ No item found for `")
        assert "Photo" not in message and "image" not in message
    assert result == f"❌ No item found for `{CODE}`"


# ── tool/executor-level destination protection unchanged ────────────────────


@pytest.mark.asyncio
async def test_tool_owner_isolation_end_to_end():
    """Full chain: executor → retrieve_save tool → service → no forward."""
    _seed_row(owner_id=OTHER_OWNER)
    ctx = _ctx(owner_id=OWNER)
    registry, executor = _registry_executor(ctx)

    result = await _run_tool(executor, ctx, "retrieve_save", {"save_code": CODE.lower()})

    assert result.success is False
    assert "No item found" in result.message
    # The fake client behind the tool context was never touched.
    ctx.telegram.client.get_input_entity.assert_not_called()
    ctx.telegram.client.forward_messages.assert_not_called()


@pytest.mark.asyncio
async def test_tool_destination_protection_unchanged():
    """Model-supplied destination stays ignored (pre-existing guarantee)."""
    _seed_row(owner_id=OWNER)
    ctx = _ctx(owner_id=OWNER)
    ctx.telegram.client.get_input_entity = AsyncMock(side_effect=lambda e: e)
    ctx.telegram.client.forward_messages = AsyncMock(return_value=MagicMock(id=555))
    ctx.telegram.client.edit_message = AsyncMock()
    registry, executor = _registry_executor(ctx)

    with patch.object(
        retrieve_service, "do_retrieve",
        AsyncMock(wraps=retrieve_service.do_retrieve),
    ) as spy:
        result = await _run_tool(
            executor, ctx, "retrieve_save",
            {"save_code": CODE, "destination": 999999, "chat_id": 999999},
        )

    assert result.success is True
    assert spy.await_args.args[3] == CHAT  # trusted context chat, not model args
    ctx.telegram.client.forward_messages.assert_awaited_once()


@pytest.mark.asyncio
async def test_tool_success_mapping_unchanged_for_authorized_owner():
    _seed_row(owner_id=OWNER)
    ctx = _ctx(owner_id=OWNER)
    ctx.telegram.client.get_input_entity = AsyncMock(side_effect=lambda e: e)
    ctx.telegram.client.forward_messages = AsyncMock(return_value=MagicMock(id=555))
    ctx.telegram.client.edit_message = AsyncMock()
    registry, executor = _registry_executor(ctx)

    result = await _run_tool(executor, ctx, "retrieve_save", {"save_code": "s0001"})

    assert result.success is True
    assert "Retrieved" in result.message
    assert result.data == {"save_code": CODE, "chat_id": CHAT}


@pytest.mark.asyncio
async def test_service_failure_mapping_unchanged():
    """The pre-existing failure conventions (missing location, DB error) hold."""
    row = _seed_row(owner_id=OWNER)
    row.pop("saved_msg_id")
    client = MagicMock()
    client.get_input_entity = AsyncMock()
    result = await retrieve_service.do_retrieve(client, OWNER, CODE, CHAT)
    assert result == "❌ Saved location data is missing for this entry."
    client.get_input_entity.assert_not_awaited()
