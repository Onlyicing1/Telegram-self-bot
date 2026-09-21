"""Save V2 Part 4 — saved-item management (rename + tag editing).

Part 4 makes a saved item *manageable* on BOTH surfaces:

    manual panel                                AI
      retrieve_item → ✏ Rename → name             rename_save (display_name)
      retrieve_item → 🏷 Tags   → one line         update_save_tags (tags + mode)
      retrieve_item → 🗑 Delete (already existed)   delete_save (already existed)
              ↓                                            ↓
        retrieve_service.do_rename / do_edit_tags  ← the ONE service authority
              ↓
        saved_items.display_name / saved_items.tags  (metadata only)

Pinned here:

* the target is resolved ONCE, by the Part 3 resolver reused through
  ``resolve_management_target`` — never a second search, never a guess among
  candidates;
* a rename changes ONLY the item's own name, and a tag edit only the item's
  own owner tags — the saved Telegram message and its identifiers are never
  touched, and legacy ``#saved*`` values are preserved;
* every write is confirmed by re-reading the owner's row, so a silently
  ignored write is reported as a failure instead of a success;
* owner scoping is enforced inside the service, and a foreign item is
  indistinguishable from a missing one;
* the model can never smuggle an unexpected field, an identity, or an
  ambiguous selection into a management execution.

Everything is offline and deterministic: the database boundary is the project's
in-memory fallback, Telegram is faked.
"""
from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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


def _row(save_code, *, owner_id=OWNER, display_name=None, tags=None, media_type="Document",
         file_name=None, created_at=NOW, caption="stored caption"):
    return {
        "id": abs(hash(save_code)) % 10_000,
        "save_code": save_code,
        "owner_id": owner_id,
        "display_name": display_name,
        "tags": list(tags or []),
        "media_type": media_type,
        "mime_type": "application/pdf",
        "file_size": 1200,
        "file_name": file_name,
        "caption": caption,
        "created_at": created_at,
        "saved_chat_id": OWNER,
        "saved_msg_id": 400,
        "origin_chat_id": -1009999,
        "origin_msg_id": 4321,
    }


def _seed(*rows):
    for row in rows:
        db_client._fallback["saved_items"].append(row)


def _stored(save_code: str) -> dict:
    """The row exactly as the database holds it now."""
    return next(
        r for r in db_client._fallback["saved_items"]
        if r.get("save_code") == save_code
    )


@pytest.fixture(autouse=True)
def _clean_state():
    db_client._fallback["saved_items"] = []
    input_state.clear_all()
    yield
    db_client._fallback["saved_items"] = []
    input_state.clear_all()


class FakeTelegram:
    def __init__(self, client=None):
        self.client = client if client is not None else MagicMock()


def _ctx(owner_id=OWNER, chat_id=CHAT):
    return ToolContext(
        telegram=FakeTelegram(), owner_id=owner_id, tz_str="UTC",
        extra={"chat_id": chat_id, "request_id": "save-v2-management"},
    )


def _chain(ctx=None):
    ctx = ctx or _ctx()
    registry = create_default_registry(ctx)
    return registry, ctx, ToolExecutor(registry, ctx)


async def _run_tool(executor, ctx, name, arguments):
    results = await executor.execute_calls(
        [{"name": name, "arguments": arguments}],
        owner_id=ctx.owner_id, session_id="save-v2-management", context_override=ctx,
    )
    return results[0]


# ── the shared resolution contract ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_save_code_target_resolves_to_exactly_that_owner_item():
    _seed(_row("S0001", display_name="University Schedule"))

    target = await retrieve_service.resolve_management_target(OWNER, save_code="s0001")

    assert target.status == retrieve_service.TARGET_OK
    assert target.save_code == "S0001"
    assert target.message == ""


@pytest.mark.asyncio
async def test_a_foreign_or_missing_code_is_indistinguishable():
    _seed(_row("S0001", owner_id=OTHER_OWNER, display_name="Their Item"))

    foreign = await retrieve_service.resolve_management_target(OWNER, save_code="S0001")
    missing = await retrieve_service.resolve_management_target(OWNER, save_code="S9999")

    assert foreign.status == missing.status == retrieve_service.TARGET_NOT_FOUND
    # A foreign item is reported EXACTLY like a missing one — never as
    # "someone else's item" — and neither resolution carries a code.
    assert foreign.message == "❌ No item found for `S0001`"
    assert missing.message == "❌ No item found for `S9999`"
    assert foreign.save_code == missing.save_code == ""


@pytest.mark.asyncio
async def test_a_unique_name_resolves_through_the_part3_resolver():
    _seed(_row("S0001", display_name="University Weekly Schedule"))

    target = await retrieve_service.resolve_management_target(
        OWNER, query="university weekly schedule"
    )

    assert target.status == retrieve_service.TARGET_OK
    assert target.save_code == "S0001"


@pytest.mark.asyncio
async def test_an_ambiguous_name_is_never_narrowed():
    _seed(
        _row("S0001", display_name="University Schedule"),
        _row("S0002", display_name="University Schedule", created_at="2026-09-16T10:00:00+00:00"),
    )

    target = await retrieve_service.resolve_management_target(
        OWNER, query="university schedule"
    )

    assert target.status == retrieve_service.TARGET_AMBIGUOUS
    assert target.save_code == ""
    assert "Multiple saved items match" in target.message
    assert "Reply with the number or the save code" in target.message


@pytest.mark.asyncio
async def test_zero_matches_is_an_honest_not_found():
    _seed(_row("S0001", display_name="Dentist Appointment"))

    target = await retrieve_service.resolve_management_target(OWNER, query="university")

    assert target.status == retrieve_service.TARGET_NOT_FOUND
    assert "No saved item matches" in target.message


@pytest.mark.asyncio
async def test_tag_based_targets_resolve_unique_or_ambiguous():
    _seed(
        _row("S0001", display_name="Schedule A", tags=["university", "semester-2"]),
        _row("S0002", display_name="Schedule B", tags=["university"]),
    )

    unique = await retrieve_service.resolve_management_target(OWNER, query="semester-2")
    assert unique.status == retrieve_service.TARGET_OK
    assert unique.save_code == "S0001"

    ambiguous = await retrieve_service.resolve_management_target(OWNER, query="university")
    assert ambiguous.status == retrieve_service.TARGET_AMBIGUOUS
    assert [c.save_code for c in ambiguous.resolution.candidates] == ["S0001", "S0002"]


@pytest.mark.asyncio
async def test_both_or_neither_addressing_is_refused():
    both = await retrieve_service.resolve_management_target(
        OWNER, save_code="S0001", query="university"
    )
    neither = await retrieve_service.resolve_management_target(OWNER)

    assert both.status == retrieve_service.TARGET_INVALID
    assert "not both" in both.message
    assert neither.status == retrieve_service.TARGET_INVALID
    assert "required" in neither.message


@pytest.mark.asyncio
async def test_target_resolution_reuses_the_resolver_and_never_searches_twice():
    """The management path must call the ONE Part 3 resolver, not a second one."""
    _seed(_row("S0001", display_name="University Schedule"))
    with patch.object(
        retrieve_service, "resolve_saved_items",
        AsyncMock(wraps=retrieve_service.resolve_saved_items),
    ) as spy:
        target = await retrieve_service.resolve_management_target(OWNER, query="university schedule")

    spy.assert_awaited_once()
    assert spy.await_args.args[0] == OWNER
    assert target.save_code == "S0001"


def test_target_resolution_cannot_perform_telegram_work():
    """No Telegram identity, client or destination reaches the resolver."""
    params = inspect.signature(retrieve_service.resolve_management_target).parameters
    assert list(params) == ["owner_id", "save_code", "query"]
    for forbidden in ("client", "chat_id", "message_id", "sender", "caption", "file_name"):
        assert forbidden not in params


# ── rename ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rename_persists_the_owner_name_and_touches_nothing_else():
    _seed(_row("S0001", display_name="Old Name", tags=["university"]))
    before = dict(_stored("S0001"))

    result = await retrieve_service.do_rename(OWNER, "s0001", "  University   Weekly Schedule  ")

    assert result.startswith("✅ Renamed `S0001`")
    after = _stored("S0001")
    assert after["display_name"] == "University Weekly Schedule"  # trimmed + collapsed
    # Only the name changed: identity, Telegram location, file and tags are intact.
    for key in ("id", "save_code", "saved_chat_id", "saved_msg_id", "origin_chat_id",
                "origin_msg_id", "media_type", "mime_type", "file_size", "caption", "tags"):
        assert after[key] == before[key], key


@pytest.mark.asyncio
async def test_rename_refuses_an_empty_or_oversized_name_without_writing():
    _seed(_row("S0001", display_name="Old Name"))

    for bad in ("", "   ", "x" * (save_service.MAX_DISPLAY_NAME_CHARS + 1)):
        result = await retrieve_service.do_rename(OWNER, "S0001", bad)
        assert result.startswith("⚠️ Nothing was renamed"), result

    assert _stored("S0001")["display_name"] == "Old Name"


@pytest.mark.asyncio
async def test_rename_of_a_missing_or_foreign_item_writes_nothing():
    _seed(_row("S0001", owner_id=OTHER_OWNER, display_name="Their Item"))

    missing = await retrieve_service.do_rename(OWNER, "S9999", "Mine")
    foreign = await retrieve_service.do_rename(OWNER, "S0001", "Mine")

    assert missing.startswith("❌ No item found")
    assert foreign.startswith("❌ No item found")
    assert _stored("S0001")["display_name"] == "Their Item"


@pytest.mark.asyncio
async def test_rename_reports_failure_when_the_write_is_not_stored():
    """A write that the database silently ignores must never be a success."""
    _seed(_row("S0001", display_name="Old Name"))
    with patch.object(db_client, "update_save_field", AsyncMock(return_value=None)):
        result = await retrieve_service.do_rename(OWNER, "S0001", "New Name")

    assert result.startswith("❌")
    assert "not stored" in result
    assert _stored("S0001")["display_name"] == "Old Name"


# ── tag editing ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tags_can_be_added_replaced_and_removed():
    _seed(_row("S0001", tags=["university"]))

    added = await retrieve_service.do_edit_tags(
        OWNER, "S0001", retrieve_service.TAG_OP_ADD, ["semester-2"]
    )
    assert "university, semester-2" in added
    assert retrieve_service._owner_tags(_stored("S0001")) == ("university", "semester-2")

    replaced = await retrieve_service.do_edit_tags(
        OWNER, "S0001", retrieve_service.TAG_OP_REPLACE, ["archive"]
    )
    assert "archive" in replaced
    assert retrieve_service._owner_tags(_stored("S0001")) == ("archive",)

    removed = await retrieve_service.do_edit_tags(
        OWNER, "S0001", retrieve_service.TAG_OP_REMOVE, ["archive"]
    )
    assert retrieve_service._owner_tags(_stored("S0001")) == ()
    assert removed.startswith("✅")


@pytest.mark.asyncio
async def test_replacing_with_an_empty_list_clears_every_tag():
    _seed(_row("S0001", tags=["university", "semester-2"]))

    result = await retrieve_service.do_edit_tags(
        OWNER, "S0001", retrieve_service.TAG_OP_REPLACE, []
    )

    assert result.startswith("✅")
    assert "no tags" in result
    assert retrieve_service._owner_tags(_stored("S0001")) == ()
    assert _stored("S0001")["tags"] == []


@pytest.mark.asyncio
async def test_legacy_hashtags_are_preserved_and_never_treated_as_owner_tags():
    _seed(_row("S0001", tags=["#saved", "#saved_photo", "university"]))

    await retrieve_service.do_edit_tags(
        OWNER, "S0001", retrieve_service.TAG_OP_REPLACE, ["archive"]
    )

    stored = _stored("S0001")["tags"]
    assert stored[:2] == ["#saved", "#saved_photo"]
    assert retrieve_service._owner_tags(_stored("S0001")) == ("archive",)
    # ...and a legacy hashtag is never a searchable target.
    assert (await retrieve_service.resolve_saved_items(OWNER, "#saved_photo")).status == (
        retrieve_service.RESOLUTION_NOT_FOUND
    )


@pytest.mark.asyncio
async def test_add_dedupes_case_insensitively_and_keeps_the_owners_spelling():
    _seed(_row("S0001", tags=["University"]))

    result = await retrieve_service.do_edit_tags(
        OWNER, "S0001", retrieve_service.TAG_OP_ADD, ["university", "  Semester 2  "]
    )

    assert result.startswith("✅")
    assert retrieve_service._owner_tags(_stored("S0001")) == ("University", "Semester 2")


@pytest.mark.asyncio
async def test_tag_limits_are_the_shared_save_limits_and_refuse_before_writing():
    _seed(_row("S0001", tags=[f"tag{i}" for i in range(save_service.MAX_SAVE_TAGS)]))

    too_many = await retrieve_service.do_edit_tags(
        OWNER, "S0001", retrieve_service.TAG_OP_ADD, ["one-more"]
    )
    too_long = await retrieve_service.do_edit_tags(
        OWNER, "S0001", retrieve_service.TAG_OP_ADD, ["x" * (save_service.MAX_TAG_CHARS + 1)]
    )

    assert too_many.startswith("⚠️")
    assert too_long.startswith("⚠️")
    assert retrieve_service._owner_tags(_stored("S0001")) == tuple(
        f"tag{i}" for i in range(save_service.MAX_SAVE_TAGS)
    )


@pytest.mark.asyncio
async def test_the_shared_normalizer_is_the_one_authority_on_tags():
    _seed(_row("S0001", tags=["university"]))
    with patch.object(
        save_service, "normalize_tags", wraps=save_service.normalize_tags
    ) as spy:
        await retrieve_service.do_edit_tags(
            OWNER, "S0001", retrieve_service.TAG_OP_ADD, ["semester-2"]
        )
    assert spy.called


@pytest.mark.asyncio
async def test_removing_tags_that_are_not_there_changes_nothing():
    _seed(_row("S0001", tags=["university"]))

    result = await retrieve_service.do_edit_tags(
        OWNER, "S0001", retrieve_service.TAG_OP_REMOVE, ["dentist"]
    )

    assert result.startswith("⚠️ Nothing was changed")
    assert retrieve_service._owner_tags(_stored("S0001")) == ("university",)


@pytest.mark.asyncio
async def test_an_unknown_or_empty_operation_is_refused():
    _seed(_row("S0001", tags=["university"]))

    unknown = await retrieve_service.do_edit_tags(OWNER, "S0001", "toggle", ["x"])
    empty_add = await retrieve_service.do_edit_tags(
        OWNER, "S0001", retrieve_service.TAG_OP_ADD, []
    )

    assert unknown.startswith("⚠️ Nothing was changed")
    assert empty_add.startswith("⚠️ Nothing was changed")
    assert retrieve_service._owner_tags(_stored("S0001")) == ("university",)


@pytest.mark.asyncio
async def test_tag_edit_owner_isolation_and_confirmation():
    _seed(_row("S0001", owner_id=OTHER_OWNER, tags=["theirs"]))

    foreign = await retrieve_service.do_edit_tags(
        OWNER, "S0001", retrieve_service.TAG_OP_REPLACE, ["mine"]
    )

    assert foreign.startswith("❌ No item found")
    assert _stored("S0001")["tags"] == ["theirs"]


@pytest.mark.asyncio
async def test_a_tag_write_that_is_not_stored_is_reported_as_failure():
    _seed(_row("S0001", tags=["university"]))
    with patch.object(db_client, "update_save_field", AsyncMock(return_value=None)):
        result = await retrieve_service.do_edit_tags(
            OWNER, "S0001", retrieve_service.TAG_OP_REPLACE, ["archive"]
        )

    assert result.startswith("❌")
    assert "not stored" in result
    assert retrieve_service._owner_tags(_stored("S0001")) == ("university",)


# ── AI tools ───────────────────────────────────────────────────────────────


def test_the_management_tools_are_registered_with_a_safe_contract():
    registry, _ctx_, _executor = _chain()

    rename = registry.get("rename_save")
    assert rename is not None
    assert rename.permission_level.value == "read_write"
    assert rename.safe is True
    assert rename.required_arguments == ("display_name",)
    assert rename.required_any_arguments == ("save_code", "query")
    assert "display_name" in rename.parameters and "query" in rename.parameters

    tags = registry.get("update_save_tags")
    assert tags is not None
    assert tags.permission_level.value == "read_write"
    assert tags.safe is True
    assert tags.required_arguments == ("tags", "mode")
    assert tags.required_any_arguments == ("save_code", "query")
    assert {"save_code", "query", "tags", "mode"} <= set(tags.parameters)


def test_the_management_tools_are_provider_schema_visible():
    registry, _ctx_, _executor = _chain()
    from backend.ai.engine.dispatcher import Dispatcher

    dispatcher = object.__new__(Dispatcher)
    dispatcher.set_tool_registry(registry)
    by_name = {d["function"]["name"]: d for d in Dispatcher._build_tool_definitions(dispatcher)}

    for name in ("rename_save", "update_save_tags"):
        assert name in by_name
    rename_params = by_name["rename_save"]["function"]["parameters"]
    assert rename_params["required"] == ["display_name"]
    tags_params = by_name["update_save_tags"]["function"]["parameters"]
    assert set(tags_params["required"]) == {"tags", "mode"}


@pytest.mark.asyncio
async def test_ai_rename_resolves_and_writes_through_the_service():
    _seed(_row("S0001", display_name="University Schedule"))
    registry, ctx, executor = _chain()

    result = await _run_tool(
        executor, ctx, "rename_save", {"query": "university schedule", "display_name": "Semester Two"}
    )

    assert result.success is True
    assert result.data["save_code"] == "S0001"
    assert _stored("S0001")["display_name"] == "Semester Two"
    assert registry.get("rename_save") is not None


@pytest.mark.asyncio
async def test_ai_rename_of_an_ambiguous_target_writes_nothing():
    _seed(
        _row("S0001", display_name="University Schedule"),
        _row("S0002", display_name="University Schedule", created_at="2026-09-16T10:00:00+00:00"),
    )
    _registry, ctx, executor = _chain()

    result = await _run_tool(
        executor, ctx, "rename_save", {"query": "university schedule", "display_name": "New"}
    )

    assert result.data["outcome"] == "ambiguous"
    assert "never choose for them" in result.message
    assert [c["save_code"] for c in result.data["candidates"]] == ["S0002", "S0001"]
    assert _stored("S0001")["display_name"] == "University Schedule"
    assert _stored("S0002")["display_name"] == "University Schedule"


@pytest.mark.asyncio
async def test_ai_rename_requires_a_name():
    _seed(_row("S0001", display_name="University Schedule"))
    _registry, ctx, executor = _chain()

    with patch.object(retrieve_service, "do_rename", AsyncMock()) as spy:
        result = await _run_tool(
            executor, ctx, "rename_save", {"save_code": "S0001", "display_name": "   "}
        )

    spy.assert_not_awaited()
    assert result.success is False
    assert "name" in result.message
    assert _stored("S0001")["display_name"] == "University Schedule"


@pytest.mark.asyncio
async def test_ai_tag_edit_applies_the_explicit_mode():
    _seed(_row("S0001", tags=["university"]))
    _registry, ctx, executor = _chain()

    added = await _run_tool(
        executor, ctx, "update_save_tags",
        {"save_code": "S0001", "tags": ["semester-2"], "mode": "add"},
    )
    assert added.success is True
    assert retrieve_service._owner_tags(_stored("S0001")) == ("university", "semester-2")

    cleared = await _run_tool(
        executor, ctx, "update_save_tags",
        {"query": "university", "tags": [], "mode": "replace"},
    )
    assert cleared.success is True
    assert retrieve_service._owner_tags(_stored("S0001")) == ()


@pytest.mark.asyncio
async def test_ai_tag_edit_rejects_a_bad_mode_or_shape_without_writing():
    _seed(_row("S0001", tags=["university"]))
    _registry, ctx, executor = _chain()

    with patch.object(retrieve_service, "do_edit_tags", AsyncMock()) as spy:
        bad_mode = await _run_tool(
            executor, ctx, "update_save_tags",
            {"save_code": "S0001", "tags": ["a"], "mode": "toggle"},
        )
        bad_tags = await _run_tool(
            executor, ctx, "update_save_tags",
            {"save_code": "S0001", "tags": "university", "mode": "add"},
        )

    spy.assert_not_awaited()
    assert bad_mode.success is False and "mode" in bad_mode.message
    assert bad_tags.success is False and "list" in bad_tags.message
    assert retrieve_service._owner_tags(_stored("S0001")) == ("university",)


@pytest.mark.asyncio
async def test_ai_tag_edit_of_an_ambiguous_target_lists_candidates_and_writes_nothing():
    _seed(
        _row("S0001", display_name="Schedule A", tags=["university"]),
        _row("S0002", display_name="Schedule B", tags=["university"]),
    )
    _registry, ctx, executor = _chain()

    result = await _run_tool(
        executor, ctx, "update_save_tags",
        {"query": "university", "tags": ["x"], "mode": "add"},
    )

    assert result.data["outcome"] == "ambiguous"
    assert [c["save_code"] for c in result.data["candidates"]] == ["S0001", "S0002"]
    assert retrieve_service._owner_tags(_stored("S0001")) == ("university",)
    assert retrieve_service._owner_tags(_stored("S0002")) == ("university",)


@pytest.mark.asyncio
async def test_model_supplied_identity_is_ignored_by_both_management_tools():
    _seed(_row("S0001", display_name="Old", tags=["university"]))
    _registry, ctx, executor = _chain()

    await _run_tool(
        executor, ctx, "rename_save",
        {"save_code": "S0001", "display_name": "New", "owner_id": OTHER_OWNER, "chat_id": 1},
    )
    await _run_tool(
        executor, ctx, "update_save_tags",
        {"save_code": "S0001", "tags": ["archive"], "mode": "replace", "owner_id": OTHER_OWNER},
    )

    row = _stored("S0001")
    assert row["owner_id"] == OWNER
    assert row["display_name"] == "New"
    assert retrieve_service._owner_tags(row) == ("archive",)


# ── action contract (the JSON fallback path) ───────────────────────────────


def test_rename_action_resolves_to_the_registered_tool():
    from backend.ai.actions import resolve_tool_calls, validate_action

    by_code = validate_action(
        {"action": "rename_saved_item", "save_code": "s0001", "display_name": "Semester Two"}
    )
    assert by_code.kind == "executable"
    assert by_code.target == "saved_item"
    assert by_code.save_code == "S0001" and by_code.display_name == "Semester Two"
    assert resolve_tool_calls(by_code) == [
        {"name": "rename_save",
         "arguments": {"display_name": "Semester Two", "save_code": "S0001"}}
    ]

    by_query = validate_action(
        {"action": "rename_saved_item", "query": "university schedule", "display_name": "X"}
    )
    assert resolve_tool_calls(by_query) == [
        {"name": "rename_save", "arguments": {"display_name": "X", "query": "university schedule"}}
    ]


def test_tag_action_resolves_to_the_registered_tool():
    from backend.ai.actions import resolve_tool_calls, validate_action

    result = validate_action({
        "action": "update_saved_item_tags",
        "save_code": "s0001",
        "tags": ["university", "semester-2"],
        "mode": "ADD",
    })

    assert result.kind == "executable"
    assert result.mode == "add"  # normalized, never inferred
    assert resolve_tool_calls(result) == [{
        "name": "update_save_tags",
        "arguments": {"tags": ["university", "semester-2"], "mode": "add", "save_code": "S0001"},
    }]

    cleared = validate_action({
        "action": "update_saved_item_tags", "query": "university", "tags": [], "mode": "replace",
    })
    assert resolve_tool_calls(cleared) == [{
        "name": "update_save_tags",
        "arguments": {"tags": [], "mode": "replace", "query": "university"},
    }]


def test_management_actions_reject_missing_or_mismatched_payloads():
    from backend.ai.actions import KIND_INVALID, validate_action

    invalid_payloads = [
        {"action": "rename_saved_item", "save_code": "S1"},                        # no name
        {"action": "rename_saved_item", "save_code": "S1", "display_name": "   "},  # blank name
        {"action": "rename_saved_item", "save_code": "S1", "display_name": "X", "tags": ["a"]},
        {"action": "rename_saved_item"},                                            # no target
        {"action": "rename_saved_item", "save_code": "S1", "query": "x", "display_name": "X"},
        {"action": "update_saved_item_tags", "save_code": "S1", "mode": "add"},      # no tags
        {"action": "update_saved_item_tags", "save_code": "S1", "tags": "a", "mode": "add"},
        {"action": "update_saved_item_tags", "save_code": "S1", "tags": ["a"]},      # no mode
        {"action": "update_saved_item_tags", "save_code": "S1", "tags": ["a"], "mode": "toggle"},
        {"action": "update_saved_item_tags", "save_code": "S1", "tags": [], "mode": "add"},
        {"action": "update_saved_item_tags", "save_code": "S1", "tags": [], "mode": "remove"},
        {"action": "update_saved_item_tags", "save_code": "S1", "tags": ["a"], "mode": "add",
         "display_name": "X"},
        {"action": "update_saved_item_tags", "tags": ["a"], "mode": "add"},          # no target
        {"action": "update_saved_item_tags", "tags": ["a"], "mode": "add",
         "save_code": "S1", "count": 2},                                             # unknown field
    ]
    for payload in invalid_payloads:
        assert validate_action(payload).kind == KIND_INVALID, payload

    # ...and the ONE form that clears every tag stays valid.
    assert validate_action({
        "action": "update_saved_item_tags", "save_code": "S1", "tags": [], "mode": "replace",
    }).kind == "executable"


def test_unrelated_actions_still_cannot_carry_save_metadata():
    from backend.ai.actions import KIND_INVALID, validate_action

    for action in ("retrieve_save", "preview_saved_item", "delete_saved_item",
                   "search_saved_items", "delete_messages", "send", "list_saved_items"):
        payload = {"action": action, "save_code": "S1", "display_name": "X", "tags": ["a"]}
        result = validate_action(payload)
        assert result.kind == KIND_INVALID, action
        assert "'display_name'/'tags'" in result.error, (action, result.error)


def test_a_model_json_action_is_mapped_to_the_management_tools():
    from backend.ai.actions import parse_action_text

    parsed = parse_action_text(
        '{"action": "rename_saved_item", "save_code": "s0001", "display_name": "Semester Two"}'
    )
    assert parsed.kind == "executable"
    assert parsed.tool_calls == [{
        "name": "rename_save",
        "arguments": {"display_name": "Semester Two", "save_code": "S0001"},
    }]

    tagged = parse_action_text(
        '{"action": "update_saved_item_tags", "save_code": "S0001", "tags": ["a"], "mode": "add"}'
    )
    assert tagged.tool_calls == [{
        "name": "update_save_tags",
        "arguments": {"tags": ["a"], "mode": "add", "save_code": "S0001"},
    }]


# ── manual panel ───────────────────────────────────────────────────────────


@pytest.fixture
def engine(monkeypatch):
    from backend.helper import inline_engine

    client = MagicMock()
    client.send_message = AsyncMock()
    client.delete_messages = AsyncMock()
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


@pytest.mark.parametrize("line,expected", [
    ("university, semester-2", (retrieve_service.TAG_OP_REPLACE, ("university", "semester-2"))),
    ("+university, +semester-2", (retrieve_service.TAG_OP_ADD, ("university", "semester-2"))),
    ("-university", (retrieve_service.TAG_OP_REMOVE, ("university",))),
    ("-", (retrieve_service.TAG_OP_REPLACE, ())),
    ("none", (retrieve_service.TAG_OP_REPLACE, ())),
    ("بدون", (retrieve_service.TAG_OP_REPLACE, ())),
])
def test_the_tag_line_grammar_is_explicit(line, expected):
    from backend.bot.handlers.retrieve import parse_tags_line

    assert parse_tags_line(line) == expected


@pytest.mark.parametrize("line", ["", "   ", "+a, -b", "+", "-,-"])
def test_unreadable_tag_lines_are_refused(line):
    from backend.bot.handlers.retrieve import parse_tags_line

    with pytest.raises(ValueError):
        parse_tags_line(line)


@pytest.mark.asyncio
async def test_the_item_panel_shows_the_stored_name_and_tags_and_offers_both_edits(engine):
    from backend.bot.handlers import retrieve as handler

    _seed(_row("S0001", display_name="University Schedule", tags=["university", "semester-2"]))

    result = await handler._retrieve_item_panel_handler(None, "id:S0001")

    assert result is not None
    title, body, buttons = result
    assert title == "Item Preview"
    assert "University Schedule" in body
    assert "**Tags** university, semester-2" in body
    data = [
        b.data.decode() for row in buttons for b in row
        if getattr(b, "data", None) is not None
    ]
    assert "input:retrieve_item:rename:S0001" in data
    assert "input:retrieve_item:tags:S0001" in data
    assert "action:retrieve_item_exec:S0001" in data
    assert "action:retrieve_item_delete:S0001" in data


@pytest.mark.asyncio
async def test_the_tags_row_without_an_item_reports_not_found(engine):
    from backend.bot.handlers import retrieve as handler

    result = await handler._retrieve_item_panel_handler(None, "id:S9999")

    assert result is not None
    assert "No item found" in result[1]


@pytest.mark.asyncio
async def test_the_manual_tag_input_writes_through_the_service(engine, helper_client):
    from backend.bot.handlers import retrieve as handler

    _seed(_row("S0001", tags=["university"]))

    await handler._retrieve_tags_input_handler(
        "archive, semester-2", CHAT, 42, -100, 7, extra="S0001"
    )

    assert retrieve_service._owner_tags(_stored("S0001")) == ("archive", "semester-2")
    assert "Tags for `S0001`" in _edited_body(helper_client)
    engine.delete_messages.assert_awaited_once_with(CHAT, [42])


@pytest.mark.asyncio
async def test_the_manual_tag_input_can_clear_every_tag(engine, helper_client):
    from backend.bot.handlers import retrieve as handler

    _seed(_row("S0001", tags=["university", "semester-2"]))

    await handler._retrieve_tags_input_handler("none", CHAT, 42, -100, 7, extra="S0001")

    assert retrieve_service._owner_tags(_stored("S0001")) == ()
    assert "no tags" in _edited_body(helper_client)


@pytest.mark.asyncio
async def test_the_manual_tag_input_without_carry_through_never_writes(engine, helper_client):
    from backend.bot.handlers import retrieve as handler

    _seed(_row("S0001", tags=["university"]))

    await handler._retrieve_tags_input_handler("archive", CHAT, 42, -100, 7, extra=None)

    assert retrieve_service._owner_tags(_stored("S0001")) == ("university",)
    assert "No item selected" in _edited_body(helper_client)


@pytest.mark.asyncio
async def test_the_manual_tag_input_refuses_an_unreadable_line(engine, helper_client):
    from backend.bot.handlers import retrieve as handler

    _seed(_row("S0001", tags=["university"]))

    await handler._retrieve_tags_input_handler("+a, -b", CHAT, 42, -100, 7, extra="S0001")

    assert retrieve_service._owner_tags(_stored("S0001")) == ("university",)
    assert "Nothing was changed" in _edited_body(helper_client)


@pytest.mark.asyncio
async def test_the_manual_rename_input_persists_the_name(engine, helper_client):
    from backend.bot.handlers import retrieve as handler

    _seed(_row("S0001", display_name="Old Name"))

    await handler._retrieve_rename_input_handler(
        "Semester Two", CHAT, 42, -100, 7, extra="S0001"
    )

    assert _stored("S0001")["display_name"] == "Semester Two"
    assert "Renamed `S0001`" in _edited_body(helper_client)
    engine.delete_messages.assert_awaited_once_with(CHAT, [42])


@pytest.mark.asyncio
async def test_the_manual_rename_input_cannot_touch_another_owners_item(engine, helper_client):
    from backend.bot.handlers import retrieve as handler

    _seed(_row("S0001", owner_id=OTHER_OWNER, display_name="Their Name"))

    await handler._retrieve_rename_input_handler("Mine", CHAT, 42, -100, 7, extra="S0001")

    assert _stored("S0001")["display_name"] == "Their Name"
    assert "No item found" in _edited_body(helper_client)


@pytest.mark.asyncio
async def test_the_manual_delete_action_uses_the_same_owner_scoped_lookup(engine, helper_client):
    """Deletion was already reachable; Part 4 only confirms its contract holds."""
    from backend.bot.handlers import retrieve as handler

    _seed(_row("S0001", display_name="University Schedule"))
    with patch.object(
        retrieve_service, "do_delete", AsyncMock(return_value="✅ Deleted `S0001`")
    ) as spy:
        await handler._retrieve_item_delete_action(None, "S0001", CHAT)

    spy.assert_awaited_once()
    assert spy.await_args.args[1:] == (OWNER, "S0001")


# ── regressions (Parts 1–3 and Save V1 stay intact) ─────────────────────────


def test_part1_metadata_contract_is_unchanged():
    """The ONE SaveMetadata contract still governs name and tags."""
    assert save_service.SaveMetadata.from_raw("  A  B ", ["x", "X", " y "]).display_name == "A B"
    assert save_service.SaveMetadata.from_raw("  A  B ", ["x", "X", " y "]).tags == ("x", "y")
    assert save_service.SaveMetadata().insert_fields() == {"tags": []}
    with pytest.raises(ValueError):
        save_service.normalize_display_name("x" * (save_service.MAX_DISPLAY_NAME_CHARS + 1))


def test_part2_save_wiring_is_unchanged():
    """The AI Save tool still exposes, and travels with, the optional metadata."""
    registry, _ctx_, _executor = _chain()
    save = registry.get("save")
    assert {"display_name", "tags"} <= set(save.parameters)
    assert registry.get("save_by_link").parameters["tags"]["type"] == "array"


@pytest.mark.asyncio
async def test_part3_resolver_behavior_is_unchanged():
    _seed(_row("S0001", display_name="University Schedule", tags=["university"]))

    unique = await retrieve_service.resolve_saved_items(OWNER, "university schedule")
    assert unique.status == retrieve_service.RESOLUTION_UNIQUE
    assert [c.save_code for c in unique.candidates] == ["S0001"]

    # The resolver is still read-only: no management field was written.
    assert _stored("S0001")["display_name"] == "University Schedule"
    assert retrieve_service._owner_tags(_stored("S0001")) == ("university",)


def test_save_v1_and_retrieval_tools_are_untouched():
    registry, _ctx_, _executor = _chain()
    for name, required in (
        ("save", None),                 # declared through requires_reply_context
        ("retrieve_save", ()),
        ("preview_save", ("save_code",)),
        ("delete_save", ("save_code",)),
    ):
        tool = registry.get(name)
        assert tool is not None, name
        declared = tool.required_arguments
        assert (declared or ()) == (required or ()), name
    assert registry.get("retrieve_save").required_any_arguments == ("save_code", "query")
    # Delete stays code-only: a fuzzy name can never reach it from a model string.
    from backend.ai.actions import KIND_INVALID, validate_action

    assert validate_action({"action": "delete_saved_item", "query": "x"}).kind == KIND_INVALID
