"""Health tests for the Saved-Items management capability.

Covers the two AI-facing management operations that already existed in the
self-bot's service layer (used by the retrieve panels) but were not reachable
through the AI ToolRegistry path:

  - ``preview_save``  → ``retrieve_service.do_preview``
  - ``delete_save``   → ``retrieve_service.do_delete``

Every test drives the REAL chain: ToolRegistry → ToolExecutor (permission
gate, argument handling) → tool → authoritative existing service
(``retrieve_service``) → a faked service/Telegram boundary. No live Telegram,
no Supabase, no providers. Owner identity is proven to come from the trusted
``ToolContext``, never from tool arguments.
"""
from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.ai.tools.base import requires_owner_confirmation
from backend.ai.tools.context import ToolContext
from backend.ai.tools.executor import ToolExecutor
from backend.ai.tools.registry import create_default_registry
from backend.ai.tools.retrieve_save import DeleteSaveTool, PreviewSaveTool


OWNER = 777
CHAT = -100123

_PREVIEW_TEXT = (
    "**Photo** `S0001`\n\n"
    "**Type** Photo\n"
    "**Format** `image/jpeg`\n"
    "**Size** 12.0 KB\n"
    "**Sender** Owner\n"
    "**Saved** 2026-01-02 03:04"
)


class FakeTelegram:
    """Minimal TelegramAPI stand-in exposing the wrapped client."""

    def __init__(self, client=None):
        self.client = client if client is not None else MagicMock()


def make_chain(*, with_client=True, extra=None):
    telegram = FakeTelegram() if with_client else None
    ctx = ToolContext(
        telegram=telegram,
        owner_id=OWNER,
        tz_str="UTC",
        extra={"chat_id": CHAT, "request_id": "saved-items-audit", **(extra or {})},
    )
    registry = create_default_registry(ctx)
    return registry, ctx, ToolExecutor(registry, ctx)


async def run_tool(executor, ctx, name, arguments):
    results = await executor.execute_calls(
        [{"name": name, "arguments": arguments}],
        owner_id=OWNER,
        session_id="saved-items-audit",
        context_override=ctx,
    )
    return results[0]


# ── Registration / contract ─────────────────────────────────────────────────


def test_saved_item_management_tools_are_registered():
    registry, _ctx, _executor = make_chain()

    delete_tool = registry.get("delete_save")
    assert delete_tool is not None
    assert delete_tool.permission_level.value == "dangerous"
    assert delete_tool.safe is False
    assert delete_tool.required_arguments == ("save_code",)
    assert "save_code" in delete_tool.parameters

    preview_tool = registry.get("preview_save")
    assert preview_tool is not None
    assert preview_tool.permission_level.value == "read_only"
    assert preview_tool.safe is True
    assert preview_tool.required_arguments == ("save_code",)
    assert "save_code" in preview_tool.parameters


def test_registry_has_no_duplicate_names():
    registry, _ctx, _executor = make_chain()
    names = registry.list_names()
    assert len(names) == len(set(names))
    assert "delete_save" in names and "preview_save" in names
    # The pre-existing saved-item operations are untouched by this capability.
    for unchanged in ("save", "save_by_link", "search", "list_saves", "retrieve_save"):
        assert unchanged in names


def test_delete_save_executes_directly_like_the_other_dangerous_tools():
    """DANGEROUS never waits for a confirmation round-trip in this self-bot.

    The destructive effect is bounded inside the service (owner-scoped lookup
    before any DB row or Saved Messages message is removed).
    """
    tool = DeleteSaveTool(ToolContext(telegram=FakeTelegram(), owner_id=OWNER, tz_str="UTC"))
    assert requires_owner_confirmation(tool) is False


@pytest.mark.asyncio
async def test_saved_item_tools_are_provider_schema_visible():
    registry, _ctx, _executor = make_chain()
    from backend.ai.engine.dispatcher import Dispatcher

    dispatcher = object.__new__(Dispatcher)
    dispatcher.set_tool_registry(registry)
    definitions = Dispatcher._build_tool_definitions(dispatcher)
    by_name = {d["function"]["name"]: d for d in definitions}

    for name in ("delete_save", "preview_save"):
        assert name in by_name
        params = by_name[name]["function"]["parameters"]
        assert params["type"] == "object"
        assert "save_code" in params["properties"]


def test_tool_module_keeps_the_service_boundary():
    """No direct DB, Supabase or Telethon access from the management tools."""
    import backend.ai.tools.retrieve_save as module

    source = inspect.getsource(module)
    for forbidden in (
        "import telethon",
        "from telethon",
        "backend.db",
        "db_client",
        "supabase",
        "iter_messages",
        "send_message(",
    ):
        assert forbidden not in source, forbidden
    # The tools delegate to the existing service layer only.
    assert "retrieve_service.do_preview" in source
    assert "retrieve_service.do_delete" in source


# ── preview_save ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_preview_save_reads_metadata_through_the_service():
    from backend.services import retrieve_service

    registry, ctx, executor = make_chain()
    with patch.object(
        retrieve_service, "do_preview", AsyncMock(return_value=_PREVIEW_TEXT)
    ) as svc:
        result = await run_tool(executor, ctx, "preview_save", {"save_code": "s0001"})

    svc.assert_awaited_once()
    args = svc.await_args.args
    # (self_client, owner_id, save_code) — the model's lower-case echo is
    # canonicalized at the tool boundary, owner identity comes from context.
    assert args[1] == OWNER and args[2] == "S0001"
    assert result.success is True
    assert "**Format** `image/jpeg`" in result.message
    assert result.data == {"save_code": "S0001"}


@pytest.mark.asyncio
async def test_preview_save_owner_comes_from_context_not_arguments():
    from backend.services import retrieve_service

    registry, ctx, executor = make_chain()
    with patch.object(
        retrieve_service, "do_preview", AsyncMock(return_value=_PREVIEW_TEXT)
    ) as svc:
        await run_tool(
            executor, ctx, "preview_save",
            {"save_code": "S0001", "owner_id": 999999},
        )
    assert svc.await_args.args[1] == OWNER


@pytest.mark.asyncio
async def test_preview_save_needs_no_telegram_client():
    """A metadata read is DB-only: no client must not be a hard failure."""
    from backend.services import retrieve_service

    registry, ctx, executor = make_chain(with_client=False)
    with patch.object(
        retrieve_service, "do_preview", AsyncMock(return_value=_PREVIEW_TEXT)
    ) as svc:
        result = await run_tool(executor, ctx, "preview_save", {"save_code": "S0001"})
    svc.assert_awaited_once()
    assert svc.await_args.args[0] is None
    assert result.success is True


@pytest.mark.asyncio
async def test_preview_save_failure_paths_are_honest():
    from backend.services import retrieve_service

    registry, ctx, executor = make_chain()

    # Missing code — never reaches the service.
    with patch.object(retrieve_service, "do_preview", AsyncMock(return_value=_PREVIEW_TEXT)) as svc:
        missing = await run_tool(executor, ctx, "preview_save", {})
    svc.assert_not_awaited()
    assert missing.success is False and "save code" in missing.message

    # Malformed code (non-alphanumeric) — never reaches the service.
    with patch.object(retrieve_service, "do_preview", AsyncMock(return_value=_PREVIEW_TEXT)) as svc2:
        malformed = await run_tool(executor, ctx, "preview_save", {"save_code": "S-001"})
    svc2.assert_not_awaited()
    assert malformed.success is False

    # Unknown item — the service's honest failure is not masked as success.
    with patch.object(
        retrieve_service, "do_preview",
        AsyncMock(return_value="❌ No item found for `S9999`"),
    ):
        unknown = await run_tool(executor, ctx, "preview_save", {"save_code": "S9999"})
    assert unknown.success is False and "No item found" in unknown.message

    # Service exception — surfaced, never swallowed.
    with patch.object(
        retrieve_service, "do_preview", AsyncMock(side_effect=RuntimeError("db down"))
    ):
        broken = await run_tool(executor, ctx, "preview_save", {"save_code": "S0001"})
    assert broken.success is False and "db down" in broken.message


# ── delete_save ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_save_removes_the_item_through_the_service():
    from backend.services import retrieve_service

    registry, ctx, executor = make_chain()
    with patch.object(
        retrieve_service, "do_delete",
        AsyncMock(return_value="✅ Deleted `S0001`"),
    ) as svc:
        result = await run_tool(executor, ctx, "delete_save", {"save_code": "s0001"})

    svc.assert_awaited_once()
    args = svc.await_args.args
    assert args[1] == OWNER and args[2] == "S0001"
    assert result.success is True and "Deleted" in result.message
    assert result.data == {"save_code": "S0001"}


@pytest.mark.asyncio
async def test_delete_save_is_not_the_message_deletion_path():
    """Saved-items deletion must never route through the message deleters."""
    from backend.services import delete_service, retrieve_service

    registry, ctx, executor = make_chain()
    with patch.object(
        retrieve_service, "do_delete", AsyncMock(return_value="✅ Deleted `S0001`")
    ):
        with patch.object(
            delete_service, "delete_verified_self_messages",
            AsyncMock(return_value=([], [])),
        ) as message_delete:
            with patch.object(
                delete_service, "do_del_self_filtered",
                AsyncMock(return_value=(0, 0, None)),
            ) as filtered_delete:
                result = await run_tool(executor, ctx, "delete_save", {"save_code": "S0001"})

    message_delete.assert_not_awaited()
    filtered_delete.assert_not_awaited()
    assert result.success is True


@pytest.mark.asyncio
async def test_delete_save_owner_comes_from_context_not_arguments():
    from backend.services import retrieve_service

    registry, ctx, executor = make_chain()
    with patch.object(
        retrieve_service, "do_delete", AsyncMock(return_value="✅ Deleted `S0001`")
    ) as svc:
        await run_tool(
            executor, ctx, "delete_save",
            {"save_code": "S0001", "owner_id": 999999},
        )
    assert svc.await_args.args[1] == OWNER


@pytest.mark.asyncio
async def test_delete_save_failure_paths_are_honest():
    from backend.services import retrieve_service

    registry, ctx, executor = make_chain()

    with patch.object(retrieve_service, "do_delete", AsyncMock(return_value="✅ ok")) as svc:
        missing = await run_tool(executor, ctx, "delete_save", {})
        malformed = await run_tool(executor, ctx, "delete_save", {"save_code": "S 001"})
    svc.assert_not_awaited()
    assert missing.success is False and malformed.success is False
    assert "Nothing was deleted" in missing.message

    with patch.object(
        retrieve_service, "do_delete",
        AsyncMock(return_value="❌ No item found for `S9999`"),
    ):
        unknown = await run_tool(executor, ctx, "delete_save", {"save_code": "S9999"})
    assert unknown.success is False and "No item found" in unknown.message

    with patch.object(
        retrieve_service, "do_delete", AsyncMock(side_effect=RuntimeError("db down"))
    ):
        broken = await run_tool(executor, ctx, "delete_save", {"save_code": "S0001"})
    assert broken.success is False and "db down" in broken.message


@pytest.mark.asyncio
async def test_delete_save_without_a_client_is_an_honest_failure():
    from backend.services import retrieve_service

    registry, ctx, executor = make_chain(with_client=False)
    with patch.object(
        retrieve_service, "do_delete", AsyncMock(return_value="✅ Deleted `S0001`")
    ) as svc:
        result = await run_tool(executor, ctx, "delete_save", {"save_code": "S0001"})
    svc.assert_not_awaited()
    assert result.success is False
    assert "not deleted" in result.message


# ── action contract (model JSON output path) ────────────────────────────────


def test_action_contract_resolves_saved_item_management_actions():
    from backend.ai.actions import resolve_tool_calls, validate_action

    preview = validate_action({"action": "preview_saved_item", "save_code": "s0001"})
    assert preview.kind == "executable"
    assert preview.save_code == "S0001"
    assert preview.target == "saved_item"
    assert resolve_tool_calls(preview) == [
        {"name": "preview_save", "arguments": {"save_code": "S0001"}}
    ]

    delete = validate_action({"action": "delete_saved_item", "save_code": "s0001"})
    assert delete.kind == "executable"
    assert delete.target == "saved_item"
    assert resolve_tool_calls(delete) == [
        {"name": "delete_save", "arguments": {"save_code": "S0001"}}
    ]

    # retrieve_save is unchanged by the shared saved-item validation.
    retrieve = validate_action({"action": "retrieve_save", "save_code": "s0001"})
    assert retrieve.kind == "executable" and retrieve.target == "current_chat"
    assert resolve_tool_calls(retrieve) == [
        {"name": "retrieve_save", "arguments": {"save_code": "S0001"}}
    ]


def test_action_contract_rejects_missing_code_and_unknown_fields():
    from backend.ai.actions import KIND_INVALID, validate_action

    assert validate_action({"action": "delete_saved_item"}).kind == KIND_INVALID
    assert validate_action({"action": "preview_saved_item", "save_code": 12}).kind == KIND_INVALID
    assert validate_action(
        {"action": "delete_saved_item", "save_code": "S1", "count": 5}
    ).kind == KIND_INVALID
    assert validate_action({"action": "delete_saved_items", "save_code": "S1"}).kind == KIND_INVALID


def test_save_code_is_rejected_for_unrelated_actions():
    from backend.ai.actions import KIND_INVALID, validate_action

    result = validate_action({"action": "list_saved_items", "save_code": "S0001"})
    assert result.kind == KIND_INVALID
    assert "save_code" in result.error


def test_model_json_action_is_mapped_to_the_registered_tools():
    from backend.ai.actions import parse_action_text

    parsed = parse_action_text('{"action": "delete_saved_item", "save_code": "s0001"}')
    assert parsed.kind == "executable"
    assert parsed.tool_calls == [{"name": "delete_save", "arguments": {"save_code": "S0001"}}]

    preview = parse_action_text('{"action": "preview_saved_item", "save_code": "S0002"}')
    assert preview.kind == "executable"
    assert preview.tool_calls == [{"name": "preview_save", "arguments": {"save_code": "S0002"}}]


# ── deterministic routing ───────────────────────────────────────────────────


def test_saved_item_delete_is_routed_deterministically():
    from backend.ai.actions import parse_command_intent

    for text in (
        "سیو S0001 رو پاک کن",
        "سیو S۰۰۰۱ رو حذف کن",
        "آیتم ذخیره‌شده S0001 رو پاک کن",
        "delete saved item S0001",
    ):
        result = parse_command_intent(text)
        assert result.kind == "executable", text
        assert result.action == "delete_saved_item", text
        assert result.save_code == "S0001", text
        assert result.tool_calls == [
            {"name": "delete_save", "arguments": {"save_code": "S0001"}}
        ], text


def test_saved_item_route_requires_an_explicit_save_code():
    from backend.ai.actions import parse_command_intent

    for text in (
        "سیوهامو پاک کن",
        "همه سیوهام رو حذف کن",
        "این سیو رو پاک کن",
        "delete my saved items",
    ):
        result = parse_command_intent(text)
        assert result.action != "delete_saved_item", text


def test_existing_intents_keep_their_routing():
    """The new saved-item route must not capture any existing intent."""
    from backend.ai.actions import parse_command_intent

    messages = parse_command_intent("۱۰ پیام آخر رو پاک کن")
    assert messages.action == "delete_messages"
    assert messages.tool_calls[0]["name"] == "delete"

    english_messages = parse_command_intent("delete the last 3 messages")
    assert english_messages.action == "delete_messages"

    replied = parse_command_intent("اینو سیو کن", has_reply=True)
    assert replied.action in ("save", "deep_save")
    assert replied.tool_calls == [{"name": "save", "arguments": {}}]

    listed = parse_command_intent("لیست سیوها رو بده")
    assert listed.action == "list_saved_items"
    assert listed.tool_calls == [{"name": "list_saves", "arguments": {}}]

    # A semantic/topic deletion is still a message deletion (or handed to the
    # provider), never an item deletion.
    semantic = parse_command_intent("پیام‌های مربوط به سیو رو پاک کن")
    assert semantic.action != "delete_saved_item"


def test_ordinary_words_are_never_read_as_save_codes():
    from backend.ai.actions import _extract_save_code

    assert _extract_save_code(["save", "saved", "semantic", "s4h"]) == "S4H"
    assert _extract_save_code(["save", "saved", "semantic"]) is None
