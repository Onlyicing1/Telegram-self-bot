"""Save V2 Part 3 — deterministic saved-item resolution (0 / 1 / N).

Pins the resolver contract added on top of the existing retrieval pipeline:

    owner request
      → retrieve_service.resolve_saved_items(owner_id, query)   (deterministic,
        owner-scoped INSIDE the database query, bounded, Telegram-free)
      → 0 candidates → honest not-found
      → 1 candidate  → the ONE do_retrieve authority is called exactly once
      → N candidates → NOTHING is retrieved; the owner gets a bounded list
        (buttons carry the exact presented codes, and a pending input holds
        the same codes for a numbered reply) and must choose explicitly

Also pinned here: the save-side owner metadata (display_name / tags) that the
resolver searches, the AI tool contract (query XOR save_code, trusted
destination, no leaked Telegram identity), the action-layer validation, and
the input-listener ``extra`` carry-through that the selection flow relies on.

Everything is deterministic and offline: the database boundary is the
in-memory fallback (or an explicit query recorder), Telegram is faked.
"""
from __future__ import annotations

import inspect
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.ai.tools.context import ToolContext
from backend.ai.tools.executor import ToolExecutor
from backend.ai.tools.registry import create_default_registry
from backend.db import client as db_client
from backend.helper import input_state
from backend.services import retrieve_service

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


@pytest.fixture(autouse=True)
def _clean_state():
    db_client._fallback["saved_items"] = []
    input_state.clear_all()
    yield
    db_client._fallback["saved_items"] = []
    input_state.clear_all()


class FakeTelegram:
    def __init__(self):
        self.client = MagicMock()


def _ctx(owner_id=OWNER, chat_id=CHAT):
    return ToolContext(
        telegram=FakeTelegram(), owner_id=owner_id, tz_str="UTC",
        extra={"chat_id": chat_id, "request_id": "save-v2-resolution"},
    )


def _executor(ctx):
    registry = create_default_registry(ctx)
    return registry, ToolExecutor(registry, ctx)


async def _run_tool(executor, ctx, name, arguments):
    results = await executor.execute_calls(
        [{"name": name, "arguments": arguments}],
        owner_id=ctx.owner_id, session_id="save-v2-resolution", context_override=ctx,
    )
    return results[0]


async def _resolve(query, owner_id=OWNER, limit=retrieve_service.MAX_RESOLUTION_CANDIDATES):
    return await retrieve_service.resolve_saved_items(owner_id, query, limit)


def _codes(resolution):
    return [c.save_code for c in resolution.candidates]


# ── 0-match behavior ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_zero_matches_is_a_clean_not_found():
    _seed(_row("S0001", display_name="University Weekly Schedule"))
    resolution = await _resolve("dentist appointment")

    assert resolution.status == retrieve_service.RESOLUTION_NOT_FOUND
    assert resolution.candidates == ()
    text = retrieve_service.format_resolution(resolution)
    assert "No saved item matches" in text
    assert "save code" in text


@pytest.mark.asyncio
async def test_empty_query_is_not_found_and_never_matches_everything():
    _seed(_row("S0001", display_name="anything"))
    for query in ("", "   ", "\n"):
        resolution = await _resolve(query)
        assert resolution.status == retrieve_service.RESOLUTION_NOT_FOUND


@pytest.mark.asyncio
async def test_oversized_query_is_not_found_not_a_silent_partial_match():
    _seed(_row("S0001", display_name="university"))
    resolution = await _resolve("university " + "x" * 200)
    assert resolution.status == retrieve_service.RESOLUTION_NOT_FOUND


@pytest.mark.asyncio
async def test_caption_is_never_searched():
    _seed(_row("S0001", display_name=None, caption="university semester two schedule"))
    resolution = await _resolve("university schedule")
    assert resolution.status == retrieve_service.RESOLUTION_NOT_FOUND


@pytest.mark.asyncio
async def test_legacy_synthetic_hashtags_are_not_owner_tags():
    _seed(_row("S0001", display_name=None, tags=["#saved", "#saved_photo", "#saved_2026"]))
    for query in ("saved", "#saved", "saved_photo"):
        resolution = await _resolve(query)
        assert resolution.status == retrieve_service.RESOLUTION_NOT_FOUND, query


# ── 1-match behavior ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_exact_display_name_match_is_unique():
    _seed(
        _row("S0001", display_name="University Weekly Schedule — Semester Two"),
        _row("S0002", display_name="University Transcript"),
    )
    resolution = await _resolve("University Weekly Schedule — Semester Two")

    assert resolution.status == retrieve_service.RESOLUTION_UNIQUE
    assert _codes(resolution) == ["S0001"]


@pytest.mark.asyncio
async def test_partial_display_name_requires_every_token():
    _seed(
        _row("S0001", display_name="University Weekly Schedule — Semester Two"),
        _row("S0002", display_name="University Transcript"),
        _row("S0003", display_name="Dentist Appointment"),
    )
    resolution = await _resolve("university semester two")

    assert resolution.status == retrieve_service.RESOLUTION_UNIQUE
    assert _codes(resolution) == ["S0001"]


@pytest.mark.asyncio
async def test_single_token_partial_display_name_match():
    _seed(_row("S0001", display_name="University Weekly Schedule"))
    resolution = await _resolve("university")
    assert resolution.status == retrieve_service.RESOLUTION_UNIQUE
    assert _codes(resolution) == ["S0001"]


@pytest.mark.asyncio
async def test_tags_match_by_whole_tag():
    _seed(
        _row("S0001", display_name="Weekly plan", tags=["university", "semester-2"]),
        _row("S0002", display_name="Dentist", tags=["health"]),
    )
    resolution = await _resolve("university")
    assert resolution.status == retrieve_service.RESOLUTION_UNIQUE
    assert _codes(resolution) == ["S0001"]

    multiple = await _resolve("semester-2")
    assert multiple.status == retrieve_service.RESOLUTION_UNIQUE
    assert _codes(multiple) == ["S0001"]


@pytest.mark.asyncio
async def test_a_multi_word_query_matches_one_hyphenated_tag():
    _seed(_row("S0001", display_name="Weekly plan", tags=["semester-2"]))
    resolution = await _resolve("semester 2")
    assert resolution.status == retrieve_service.RESOLUTION_UNIQUE
    assert _codes(resolution) == ["S0001"]


@pytest.mark.asyncio
async def test_arbitrary_words_are_not_treated_as_tags():
    """A token that is only a tag FRAGMENT must not match the tag."""
    _seed(_row("S0001", display_name=None, tags=["university"]))
    resolution = await _resolve("uni")
    assert resolution.status == retrieve_service.RESOLUTION_NOT_FOUND


@pytest.mark.asyncio
async def test_name_plus_tag_narrowing_is_deterministic():
    _seed(
        _row("S0001", display_name="University Weekly Schedule", tags=["semester-2"]),
        _row("S0002", display_name="University Weekly Schedule", tags=["semester-1"]),
    )
    resolution = await _resolve("university semester-1")
    assert resolution.status == retrieve_service.RESOLUTION_UNIQUE
    assert _codes(resolution) == ["S0002"]


# ── N-match behavior ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_multiple_matches_are_ambiguous_and_bounded():
    for index in range(10):
        _seed(_row(f"S{index + 1:04d}", display_name="University Schedule",
                   created_at=f"2026-09-{index + 1:02d}T10:00:00+00:00"))
    resolution = await _resolve("university schedule")

    assert resolution.status == retrieve_service.RESOLUTION_AMBIGUOUS
    assert len(resolution.candidates) == retrieve_service.MAX_RESOLUTION_CANDIDATES
    assert resolution.overflowed is True
    text = retrieve_service.format_resolution(resolution)
    assert "Multiple saved items match" in text
    assert "More matches exist than are shown" in text
    assert text.count("`S") == retrieve_service.MAX_RESOLUTION_CANDIDATES


@pytest.mark.asyncio
async def test_two_matches_are_ambiguous_without_overflow():
    _seed(_row("S0001", display_name="University Schedule", created_at="2026-09-01T10:00:00+00:00"))
    _seed(_row("S0002", display_name="University Schedule", created_at="2026-09-02T10:00:00+00:00"))

    resolution = await _resolve("university schedule")
    assert resolution.status == retrieve_service.RESOLUTION_AMBIGUOUS
    assert _codes(resolution) == ["S0002", "S0001"]  # created_at DESC
    assert resolution.overflowed is False
    assert "More matches exist" not in retrieve_service.format_resolution(resolution)


@pytest.mark.asyncio
async def test_candidate_order_is_deterministic_on_identical_timestamps():
    _seed(_row("S0002", display_name="Dup", created_at=NOW))
    _seed(_row("S0001", display_name="Dup", created_at=NOW))
    resolution = await _resolve("dup")
    assert _codes(resolution) == ["S0001", "S0002"]  # created_at tie → save_code ASC


@pytest.mark.asyncio
async def test_duplicate_names_with_different_tags_disambiguate_by_tag():
    _seed(_row("S0001", display_name="Project Plan", tags=["alpha"]))
    _seed(_row("S0002", display_name="Project Plan", tags=["beta"]))
    resolution = await _resolve("project plan alpha")
    assert _codes(resolution) == ["S0001"]


# ── normalization ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_case_whitespace_and_punctuation_are_normalized_for_comparison():
    _seed(_row("S0001", display_name="University  Weekly-Schedule"))
    for query in ("university weekly-schedule", "  UNIVERSITY   WEEKLY-SCHEDULE  "):
        resolution = await _resolve(query)
        assert resolution.status == retrieve_service.RESOLUTION_UNIQUE, query
        assert _codes(resolution) == ["S0001"]


@pytest.mark.asyncio
async def test_stored_values_are_never_rewritten_by_normalization():
    row = _row("S0001", display_name="  University  Weekly-Schedule  ", tags=["  uni  "])
    _seed(row)
    await _resolve("university weekly-schedule")
    assert row["display_name"] == "  University  Weekly-Schedule  "
    assert row["tags"] == ["  uni  "]


@pytest.mark.asyncio
async def test_persian_display_name_and_tags_resolve():
    _seed(_row("S0001", display_name="برنامه هفتگی دانشگاه", tags=["دانشگاه", "ترم-۲"]))
    assert (await _resolve("برنامه دانشگاه")).status == retrieve_service.RESOLUTION_UNIQUE
    assert (await _resolve("دانشگاه")).status == retrieve_service.RESOLUTION_UNIQUE
    assert (await _resolve("ترم-۲")).status == retrieve_service.RESOLUTION_UNIQUE
    assert (await _resolve("برنامهٔ ناموجود")).status == retrieve_service.RESOLUTION_NOT_FOUND


@pytest.mark.asyncio
async def test_persian_arabic_spelling_variants_match():
    # Stored Persian script, queried with the Arabic yeh/kaf spellings
    # (and the reverse): both fold to the same matching form.
    _seed(_row("S0001", display_name="هفتگی", tags=["کتاب"]))
    assert (await _resolve("هفتگي")).status == retrieve_service.RESOLUTION_UNIQUE
    assert (await _resolve("كتاب")).status == retrieve_service.RESOLUTION_UNIQUE

    db_client._fallback["saved_items"] = []
    _seed(_row("S0002", display_name="هفتگي", tags=["كتاب"]))
    assert (await _resolve("هفتگی")).status == retrieve_service.RESOLUTION_UNIQUE
    assert (await _resolve("کتاب")).status == retrieve_service.RESOLUTION_UNIQUE


@pytest.mark.asyncio
async def test_mixed_persian_english_name_resolves():
    _seed(_row("S0001", display_name="University برنامه هفتگی"))
    resolution = await _resolve("university برنامه")
    assert resolution.status == retrieve_service.RESOLUTION_UNIQUE
    assert _codes(resolution) == ["S0001"]


# ── save codes keep their exact pre-resolver behavior ───────────────────────

@pytest.mark.asyncio
async def test_save_code_resolves_owner_scoped_without_fuzzy_matching():
    _seed(_row("S0001", display_name="Anything"))
    assert _codes(await _resolve("s0001")) == ["S0001"]
    assert _codes(await _resolve("S0001")) == ["S0001"]
    assert (await _resolve("s9999")).status == retrieve_service.RESOLUTION_NOT_FOUND


@pytest.mark.asyncio
async def test_code_shaped_query_never_falls_through_to_display_names():
    """A code-shaped request is a CODE request — it is never name-resolved."""
    _seed(_row("S0002", display_name="S0001"))
    assert (await _resolve("S0001")).status == retrieve_service.RESOLUTION_NOT_FOUND


@pytest.mark.asyncio
async def test_foreign_owners_code_is_not_found():
    _seed(_row("S0001", display_name="other owner item", owner_id=OTHER_OWNER))
    assert (await _resolve("S0001", owner_id=OWNER)).status == retrieve_service.RESOLUTION_NOT_FOUND
    assert _codes(await _resolve("S0001", owner_id=OTHER_OWNER)) == ["S0001"]


# ── owner isolation (semantic + inside the query) ───────────────────────────

@pytest.mark.asyncio
async def test_owner_isolation_same_name_and_tag_across_two_owners():
    _seed(
        _row("S0001", display_name="University Schedule", tags=["university"], owner_id=OWNER),
        _row("S0002", display_name="University Schedule", tags=["university"], owner_id=OTHER_OWNER),
    )

    mine = await _resolve("University Schedule", owner_id=OWNER)
    assert _codes(mine) == ["S0001"]

    theirs = await _resolve("University Schedule", owner_id=OTHER_OWNER)
    assert _codes(theirs) == ["S0002"]

    assert _codes(await _resolve("university", owner_id=OWNER)) == ["S0001"]
    assert _codes(await _resolve("university", owner_id=OTHER_OWNER)) == ["S0002"]


@pytest.mark.asyncio
async def test_a_foreign_match_is_indistinguishable_from_missing():
    _seed(_row("S0001", display_name="Private Medical Record", owner_id=OTHER_OWNER))
    text = retrieve_service.format_resolution(await _resolve("private medical record", OWNER))
    assert text == (
        "🔍 No saved item matches `private medical record`. "
        "Try the saved name, one of its tags, or its save code."
    )
    assert "S0001" not in text and "Private" not in text


@pytest.mark.asyncio
async def test_owner_scoping_is_applied_inside_the_database_query():
    """The owner predicate must be part of the query, never a Python filter."""
    recorded: list[tuple] = []

    class Recorder:
        def table(self, name):
            recorded.append(("table", name))
            return self

        def select(self, columns):
            recorded.append(("select", columns))
            return self

        def eq(self, column, value):
            recorded.append(("eq", column, value))
            return self

        def or_(self, expression):
            recorded.append(("or_", expression))
            return self

        def order(self, column, desc=False):
            recorded.append(("order", column, desc))
            return self

        def limit(self, count):
            recorded.append(("limit", count))
            return self

        def execute(self):
            recorded.append(("execute",))
            return MagicMock(data=[])

    with patch.object(db_client, "get_db", lambda: Recorder()):
        await db_client.resolve_saves(
            OWNER, "mixed",
            [{"patterns": ["%university%"], "tags": ["university"]}],
            ["semester-2"], 9,
        )

    owners = [c for c in recorded if c[0] == "eq"]
    assert owners, "the resolver query must carry an owner filter"
    assert all(c == ("eq", "owner_id", OWNER) for c in owners), owners
    assert ("table", "saved_items") in recorded
    assert ("limit", 9) in recorded
    assert ("order", "created_at", True) in recorded
    assert ("order", "save_code", False) in recorded
    logic = [c[1] for c in recorded if c[0] == "or_"]
    assert logic and all("university" in expr for expr in logic)
    assert all("semester-2" in expr for expr in logic)


@pytest.mark.asyncio
async def test_owner_scoping_is_applied_in_the_in_memory_fallback_too():
    _seed(
        _row("S0001", display_name="Shared Name", owner_id=OWNER),
        _row("S0002", display_name="Shared Name", owner_id=OTHER_OWNER),
    )
    resolution = await _resolve("shared name", owner_id=OWNER)
    assert _codes(resolution) == ["S0001"]


# ── the resolver itself is Telegram-free ────────────────────────────────────

def test_resolver_signature_cannot_perform_telegram_work():
    """No client/chat/message parameter exists to act on — structurally."""
    parameters = set(inspect.signature(retrieve_service.resolve_saved_items).parameters)
    assert parameters == {"owner_id", "query", "limit"}
    source = inspect.getsource(retrieve_service.resolve_saved_items)
    for forbidden in ("forward_messages", "send_message", "edit_message", "delete_messages"):
        assert forbidden not in source


# ── AI tool: retrieve_save(query) — 1 retrieves, N never does ───────────────

@pytest.mark.asyncio
async def test_tool_query_unique_retrieves_exactly_once():
    _seed(_row("S0001", display_name="University Weekly Schedule"))
    ctx = _ctx()
    _registry, executor = _executor(ctx)

    with patch.object(
        retrieve_service, "do_retrieve",
        AsyncMock(return_value="✅ Retrieved `S0001` to this chat."),
    ) as spy:
        result = await _run_tool(executor, ctx, "retrieve_save", {"query": "university weekly schedule"})

    assert result.success is True
    spy.assert_awaited_once()
    _client, owner_id, save_code, chat_id = spy.await_args.args
    assert owner_id == OWNER and save_code == "S0001" and chat_id == CHAT
    assert result.data == {"save_code": "S0001", "chat_id": CHAT, "resolved_from": "query"}


@pytest.mark.asyncio
async def test_tool_query_ambiguous_never_retrieves_and_touches_no_telegram():
    _seed(
        _row("S0001", display_name="University Schedule"),
        _row("S0002", display_name="University Schedule", created_at="2026-09-16T10:00:00+00:00"),
    )
    ctx = _ctx()
    _registry, executor = _executor(ctx)

    with patch.object(retrieve_service, "do_retrieve", AsyncMock()) as spy:
        result = await _run_tool(executor, ctx, "retrieve_save", {"query": "university schedule"})

    spy.assert_not_awaited()
    ctx.telegram.client.forward_messages.assert_not_called()
    ctx.telegram.client.get_input_entity.assert_not_called()
    assert result.success is True
    assert "Multiple saved items match" in result.message
    assert "NOTHING was retrieved" in result.message
    assert "never choose for them" in result.message
    assert result.data["outcome"] == "ambiguous"
    assert [c["save_code"] for c in result.data["candidates"]] == ["S0002", "S0001"]


@pytest.mark.asyncio
async def test_tool_query_not_found_is_honest_and_side_effect_free():
    _seed(_row("S0001", display_name="Dentist"))
    ctx = _ctx()
    _registry, executor = _executor(ctx)

    with patch.object(retrieve_service, "do_retrieve", AsyncMock()) as spy:
        result = await _run_tool(executor, ctx, "retrieve_save", {"query": "university schedule"})

    spy.assert_not_awaited()
    assert result.success is False
    assert "No saved item matches" in result.message
    assert result.data == {"outcome": "not_found", "query": "university schedule"}


@pytest.mark.asyncio
async def test_tool_requires_exactly_one_of_code_or_query():
    _seed(_row("S0001", display_name="Dentist"))
    ctx = _ctx()
    _registry, executor = _executor(ctx)

    with patch.object(retrieve_service, "do_retrieve", AsyncMock()) as spy:
        both = await _run_tool(
            executor, ctx, "retrieve_save", {"save_code": "S0001", "query": "dentist"}
        )
        neither = await _run_tool(executor, ctx, "retrieve_save", {})

    spy.assert_not_awaited()
    assert both.success is False and "not both" in both.message
    assert neither.success is False and "is required" in neither.message


@pytest.mark.asyncio
async def test_tool_ignores_model_supplied_owner_and_destination():
    _seed(
        _row("S0001", display_name="Shared Name", owner_id=OWNER),
        _row("S0002", display_name="Shared Name", owner_id=OTHER_OWNER),
    )
    ctx = _ctx()
    _registry, executor = _executor(ctx)

    with patch.object(
        retrieve_service, "do_retrieve",
        AsyncMock(return_value="✅ Retrieved `S0001` to this chat."),
    ) as spy:
        result = await _run_tool(
            executor, ctx, "retrieve_save",
            {"query": "shared name", "owner_id": OTHER_OWNER, "chat_id": 999, "destination": 999},
        )

    assert result.success is True
    _client, owner_id, save_code, chat_id = spy.await_args.args
    assert owner_id == OWNER          # trusted context, not the model argument
    assert chat_id == CHAT            # trusted context destination
    assert save_code == "S0001"       # the OWNER's row, never the foreign one


@pytest.mark.asyncio
async def test_tool_ambiguous_payload_exposes_only_safe_candidate_fields():
    _seed(
        _row("S0001", display_name="University Schedule", tags=["university"]),
        _row("S0002", display_name="University Schedule", tags=["university"]),
    )
    ctx = _ctx()
    _registry, executor = _executor(ctx)
    result = await _run_tool(executor, ctx, "retrieve_save", {"query": "university schedule"})

    assert result.data["candidates"]
    for candidate in result.data["candidates"]:
        assert set(candidate) == {"save_code", "label"}
    for forbidden in (str(CHAT), "origin", "saved_chat", "saved_msg", "file_id", "sender", "stored caption"):
        assert forbidden not in json.dumps(result.data), forbidden
        assert forbidden not in result.message, forbidden


@pytest.mark.asyncio
async def test_tool_code_path_structured_data_is_unchanged():
    _seed(_row("S0001", display_name="Dentist"))
    ctx = _ctx()
    ctx.telegram.client.get_input_entity = AsyncMock(side_effect=lambda e: e)
    ctx.telegram.client.forward_messages = AsyncMock(return_value=MagicMock(id=555))
    ctx.telegram.client.edit_message = AsyncMock()
    _registry, executor = _executor(ctx)

    result = await _run_tool(executor, ctx, "retrieve_save", {"save_code": "s0001"})

    assert result.success is True
    assert result.data == {"save_code": "S0001", "chat_id": CHAT}  # no new keys
    ctx.telegram.client.forward_messages.assert_awaited_once()


@pytest.mark.asyncio
async def test_tool_unknown_or_foreign_code_fails_honestly_without_forgetting():
    """Through the REAL do_retrieve: unknown and foreign codes are identical."""
    _seed(_row("S0002", display_name="Foreign", owner_id=OTHER_OWNER))
    ctx = _ctx()
    _registry, executor = _executor(ctx)

    unknown = await _run_tool(executor, ctx, "retrieve_save", {"save_code": "S9999"})
    foreign = await _run_tool(executor, ctx, "retrieve_save", {"save_code": "S0002"})

    assert unknown.success is False and foreign.success is False
    assert "No item found for `S9999`" in unknown.message
    assert "No item found for `S0002`" in foreign.message
    ctx.telegram.client.forward_messages.assert_not_called()
    ctx.telegram.client.get_input_entity.assert_not_called()


# ── action-layer contract (JSON fallback path) ──────────────────────────────

def test_save_action_carries_optional_name_and_tags():
    from backend.ai.actions import KIND_EXECUTABLE, resolve_tool_calls, validate_action

    result = validate_action({
        "action": "save",
        "target": "replied_message",
        "display_name": "University Weekly Schedule",
        "tags": ["university", "semester-2"],
    })
    assert result.kind == KIND_EXECUTABLE
    assert result.display_name == "University Weekly Schedule"
    assert list(result.tags or []) == ["university", "semester-2"]
    assert resolve_tool_calls(result) == [{
        "name": "save",
        "arguments": {"display_name": "University Weekly Schedule", "tags": ["university", "semester-2"]},
    }]


def test_save_action_without_metadata_stays_byte_identical():
    from backend.ai.actions import resolve_tool_calls, validate_action

    assert resolve_tool_calls(validate_action({"action": "save"})) == [
        {"name": "save", "arguments": {}}
    ]
    assert resolve_tool_calls(validate_action({"action": "deep_save"})) == [
        {"name": "save", "arguments": {}}
    ]


def test_name_and_tags_are_rejected_outside_save_actions():
    from backend.ai.actions import KIND_INVALID, validate_action

    for action in ("retrieve_save", "search_saved_items", "delete_messages", "send"):
        payload = {"action": action, "query": "x", "text": "x", "display_name": "X", "tags": ["a"]}
        result = validate_action(payload)
        assert result.kind == KIND_INVALID, action
        assert "'display_name'/'tags'" in result.error, (action, result.error)


def test_retrieve_action_accepts_a_query_instead_of_a_code():
    from backend.ai.actions import KIND_EXECUTABLE, resolve_tool_calls, validate_action

    result = validate_action({"action": "retrieve_save", "query": "university schedule"})
    assert result.kind == KIND_EXECUTABLE
    assert result.query == "university schedule"
    assert result.save_code == ""
    assert resolve_tool_calls(result) == [
        {"name": "retrieve_save", "arguments": {"query": "university schedule"}}
    ]


def test_retrieve_action_rejects_ambiguous_and_invalid_inputs():
    from backend.ai.actions import KIND_INVALID, validate_action

    both = validate_action({"action": "retrieve_save", "save_code": "S0001", "query": "x"})
    assert both.kind == KIND_INVALID and "not both" in both.error
    neither = validate_action({"action": "retrieve_save"})
    assert neither.kind == KIND_INVALID and "'save_code' or 'query'" in neither.error
    blank = validate_action({"action": "retrieve_save", "query": "   "})
    assert blank.kind == KIND_INVALID
    oversized = validate_action({"action": "retrieve_save", "query": "x" * 200})
    assert oversized.kind == KIND_INVALID


def test_preview_and_delete_stay_code_only():
    from backend.ai.actions import KIND_INVALID, validate_action

    for action in ("preview_saved_item", "delete_saved_item"):
        result = validate_action({"action": action, "query": "university schedule"})
        assert result.kind == KIND_INVALID, action
        assert "Unknown field" in result.error


# ── the generated filter can never be altered by user text ─────────────────

def test_filter_syntax_characters_are_stripped_from_token_patterns():
    hostile = "a),(b.{c}"
    normalized = [retrieve_service._normalize_search(hostile)]
    groups = retrieve_service._token_groups([hostile], normalized)

    for group in groups:
        for pattern in group["patterns"]:
            # The tree-structural characters can never appear in a value.
            assert not set(pattern) & set(",()\\"), pattern
        for term in group["tags"]:
            assert not set(term) & set(",(){}\"\\"), term

    expression = db_client.resolve_logic_expression(db_client._resolve_condition_groups("mixed", groups, []))
    assert "display_name.ilike" in expression
    for fragment in groups[0]["patterns"]:
        assert fragment in expression


def test_logic_expression_ands_tokens_and_ors_variants():
    groups = db_client._resolve_condition_groups(
        "mixed",
        [
            {"patterns": ["%a%", "%a2%"], "tags": ["a"]},
            {"patterns": ["%b%"], "tags": ["b"]},
        ],
        [],
    )
    expression = db_client.resolve_logic_expression(groups)
    assert expression.startswith("and(")
    assert "display_name.ilike.%a%" in expression and "display_name.ilike.%a2%" in expression
    assert "tags.cs.{a}" in expression
    assert expression.count("or(") == 2
    assert "file_name.ilike.%b%" in expression


# ── manual retrieval — the SAME resolver, 0 / 1 / N ─────────────────────────

@pytest.fixture
def engine(monkeypatch):
    """The self-client/owner refs the retrieve handlers import at call time."""
    from backend.helper import inline_engine

    client = MagicMock()
    client.send_message = AsyncMock()
    client.delete_messages = AsyncMock()
    client.forward_messages = AsyncMock()
    monkeypatch.setattr(inline_engine, "_self_client", client, raising=False)
    monkeypatch.setattr(inline_engine, "_owner_id", OWNER, raising=False)
    return client


@pytest.fixture
def helper_client(monkeypatch):
    """The Glass-UI helper (so results edit in place, as in production)."""
    from backend.bot.handlers import retrieve as handler

    helper = MagicMock()
    helper.edit_message = AsyncMock()
    monkeypatch.setattr(handler, "get_client", lambda: helper)
    return helper


@pytest.fixture
def presented(monkeypatch):
    """Records every button row a panel handler presents."""
    from backend.bot.handlers import retrieve as handler
    from backend.helper import InlinePanelBuilder as RealBuilder

    created = []

    class Recording(RealBuilder):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.rows = []
            created.append(self)

        def add_row(self, text, callback_data):
            self.rows.append((text, callback_data))
            return super().add_row(text, callback_data)

    monkeypatch.setattr(handler, "InlinePanelBuilder", Recording)
    return created


def _edited_body(helper) -> str:
    """The body of the most recent in-place panel edit."""
    bodies = [
        call.args[2] if len(call.args) > 2 else ""
        for call in helper.edit_message.await_args_list
    ]
    assert bodies, "nothing was rendered in place"
    return bodies[-1]


@pytest.mark.asyncio
async def test_manual_search_unique_retrieves_immediately(engine, helper_client):
    from backend.bot.handlers import retrieve as handler

    _seed(_row("S0001", display_name="University Weekly Schedule"))
    with patch.object(
        retrieve_service, "do_retrieve",
        AsyncMock(return_value="✅ Retrieved `S0001` to this chat."),
    ) as spy:
        await handler._resolve_saved_item_request(
            "university weekly schedule", engine, OWNER, CHAT, 42, -100, 7
        )

    spy.assert_awaited_once()
    assert spy.await_args.args == (engine, OWNER, "S0001", CHAT)
    assert "Retrieved `S0001`" in _edited_body(helper_client)
    engine.delete_messages.assert_awaited_once_with(CHAT, [42])
    assert input_state.get_pending(OWNER) is None  # no needless clarification


@pytest.mark.asyncio
async def test_manual_search_zero_matches_never_retrieves(engine, helper_client, presented):
    from backend.bot.handlers import retrieve as handler

    _seed(_row("S0001", display_name="Dentist Appointment"))
    with patch.object(retrieve_service, "do_retrieve", AsyncMock()) as spy:
        await handler._resolve_saved_item_request(
            "university schedule", engine, OWNER, CHAT, 42, -100, 7
        )

    spy.assert_not_awaited()
    engine.forward_messages.assert_not_called()
    assert "No saved item matches" in _edited_body(helper_client)
    assert [row[1] for row in presented[0].rows] == ["input:retrieve:search", "panel:retrieve"]
    assert input_state.get_pending(OWNER) is None


@pytest.mark.asyncio
async def test_manual_search_ambiguous_lists_candidates_and_never_retrieves(
    engine, helper_client, presented
):
    from backend.bot.handlers import retrieve as handler

    _seed(
        _row("S0001", display_name="University Schedule"),
        _row("S0002", display_name="University Schedule", created_at="2026-09-16T10:00:00+00:00"),
    )
    with patch.object(retrieve_service, "do_retrieve", AsyncMock()) as spy:
        await handler._resolve_saved_item_request(
            "university schedule", engine, OWNER, CHAT, 42, -100, 7
        )

    spy.assert_not_awaited()
    engine.forward_messages.assert_not_called()
    body = _edited_body(helper_client)
    assert "Multiple saved items match" in body
    assert "Reply with the number or the save code" in body

    rows = presented[0].rows
    assert [row[1] for row in rows[:2]] == [
        "action:resolve_pick:S0002",
        "action:resolve_pick:S0001",
    ]
    assert rows[0][0].startswith("1. University Schedule")
    assert rows[1][0].startswith("2. University Schedule")

    # The pending state carries the EXACT presented codes, in the shown order.
    state = input_state.get_pending(OWNER)
    assert state is not None and state["panel_id"] == "retrieve"
    assert json.loads(state["extra"]) == ["S0002", "S0001"]
    assert state["timeout"] == 60.0
    assert state["chat_id"] == CHAT


@pytest.mark.asyncio
async def test_manual_search_ambiguous_candidate_detail_is_bounded(engine, helper_client, presented):
    from backend.bot.handlers import retrieve as handler

    for i in range(1, 13):
        _seed(_row(f"S{i:04d}", display_name="University Schedule",
                   created_at=f"2026-09-20T{i:02d}:00:00+00:00"))

    with patch.object(retrieve_service, "do_retrieve", AsyncMock()) as spy:
        await handler._resolve_saved_item_request(
            "university schedule", engine, OWNER, CHAT, 42, -100, 7
        )

    spy.assert_not_awaited()
    body = _edited_body(helper_client)
    assert "More matches exist than are shown" in body  # honesty about the bound

    rows = presented[0].rows
    codes = [row[1].split(":")[-1] for row in rows if row[1].startswith("action:resolve_pick:")]
    limit = retrieve_service.MAX_RESOLUTION_CANDIDATES
    assert len(codes) == limit
    assert codes == [f"S{i:04d}" for i in range(12, 12 - limit, -1)]
    assert json.loads(input_state.get_pending(OWNER)["extra"]) == codes


# ── manual retrieval — the pending-selection listener ───────────────────────

def _listener(client):
    """Register the input listener and hand back its callback."""
    from backend.helper import inline_sender

    captured = {}

    def _on(*args, **kwargs):
        def decorator(func):
            captured["listener"] = func
            return func
        return decorator

    client.on = _on
    inline_sender.register_input_listener(client, OWNER)
    return captured["listener"]


def _event(text, *, chat_id=CHAT, msg_id=99, sender_id=OWNER):
    event = MagicMock()
    event.raw_text = text
    event.chat_id = chat_id
    event.sender_id = sender_id
    event.message = MagicMock(id=msg_id)
    return event


async def _present_ambiguity(engine, helper_client):
    """Show the ambiguous list the way the owner sees it, and return the codes."""
    from backend.bot.handlers import retrieve as handler

    _seed(
        _row("S0001", display_name="University Schedule"),
        _row("S0002", display_name="University Schedule", created_at="2026-09-16T10:00:00+00:00"),
    )
    await handler._resolve_saved_item_request(
        "university schedule", engine, OWNER, CHAT, 42, -100, 7
    )
    return json.loads(input_state.get_pending(OWNER)["extra"])


@pytest.mark.asyncio
async def test_listener_numbered_reply_retrieves_the_presented_candidate(
    engine, helper_client, presented
):
    codes = await _present_ambiguity(engine, helper_client)
    listener = _listener(engine)

    with patch.object(
        retrieve_service, "do_retrieve",
        AsyncMock(return_value="✅ Retrieved to this chat."),
    ) as spy:
        await listener(_event("2"))

    spy.assert_awaited_once()
    assert spy.await_args.args == (engine, OWNER, codes[1], CHAT)
    assert codes[1] == "S0001"
    assert input_state.get_pending(OWNER) is None
    engine.delete_messages.assert_awaited_with(CHAT, [99])


@pytest.mark.asyncio
async def test_listener_accepts_persian_digits(engine, helper_client, presented):
    codes = await _present_ambiguity(engine, helper_client)
    listener = _listener(engine)

    with patch.object(retrieve_service, "do_retrieve", AsyncMock(return_value="ok")) as spy:
        await listener(_event("۲"))  # Persian two

    spy.assert_awaited_once()
    assert spy.await_args.args[2] == codes[1]


@pytest.mark.asyncio
async def test_listener_exact_code_reply_retrieves_that_candidate(engine, helper_client, presented):
    await _present_ambiguity(engine, helper_client)
    listener = _listener(engine)

    with patch.object(retrieve_service, "do_retrieve", AsyncMock(return_value="ok")) as spy:
        await listener(_event("s0002"))

    spy.assert_awaited_once()
    assert spy.await_args.args[2] == "S0002"


@pytest.mark.asyncio
async def test_listener_rejects_an_unrelated_reply_without_retrieving(
    engine, helper_client, presented
):
    await _present_ambiguity(engine, helper_client)
    listener = _listener(engine)

    with patch.object(retrieve_service, "do_retrieve", AsyncMock()) as spy:
        await listener(_event("banana"))
        await listener(_event("9"))  # out of range

    spy.assert_not_awaited()
    body = _edited_body(helper_client)
    assert "Nothing was retrieved" in body


@pytest.mark.asyncio
async def test_listener_ignores_other_chats_and_non_owners(engine, helper_client, presented):
    await _present_ambiguity(engine, helper_client)
    listener = _listener(engine)

    with patch.object(retrieve_service, "do_retrieve", AsyncMock()) as spy:
        await listener(_event("2", chat_id=-42))              # another chat
        await listener(_event("2", sender_id=OTHER_OWNER))    # another sender

    spy.assert_not_awaited()
    assert input_state.has_pending(OWNER) is True  # state untouched


@pytest.mark.asyncio
async def test_listener_expired_selection_is_rejected(engine, helper_client, presented):
    await _present_ambiguity(engine, helper_client)
    listener = _listener(engine)
    renders = helper_client.edit_message.await_count
    input_state._pending[OWNER]["created_at"] -= input_state._INPUT_TIMEOUT_S + 1

    with patch.object(retrieve_service, "do_retrieve", AsyncMock()) as spy:
        await listener(_event("2"))

    spy.assert_not_awaited()
    assert input_state.get_pending(OWNER) is None  # expired and consumed
    # Not even a rendering: an expired selection is silently refused.
    assert helper_client.edit_message.await_count == renders


@pytest.mark.asyncio
async def test_listener_stale_selection_after_deletion_fails_cleanly(
    engine, helper_client, presented
):
    codes = await _present_ambiguity(engine, helper_client)
    listener = _listener(engine)

    # The chosen candidate is deleted between showing the list and answering.
    db_client._fallback["saved_items"] = [
        row for row in db_client._fallback["saved_items"] if row["save_code"] != codes[1]
    ]

    with patch.object(retrieve_service, "do_retrieve", AsyncMock()) as spy:
        await listener(_event("2"))

    spy.assert_not_awaited()                        # no retrieval at all
    body = _edited_body(helper_client)
    assert "may have been deleted" in body
    assert codes[0] not in body                     # no fallback to the other candidate
    assert "Nothing was retrieved" in body


# ── manual retrieval — the clicked-candidate action ────────────────────────

@pytest.mark.asyncio
async def test_resolve_pick_action_retrieves_exactly_the_clicked_candidate(engine, helper_client):
    from backend.bot.handlers import retrieve as handler

    _seed(_row("S0002", display_name="University Schedule"))
    with patch.object(retrieve_service, "do_retrieve", AsyncMock(return_value="ok")) as spy:
        title, body, _buttons = await handler._resolve_pick_action(None, "S0002", CHAT)

    spy.assert_awaited_once()
    assert spy.await_args.args == (engine, OWNER, "S0002", CHAT)
    assert title == "Retrieve" and body == "ok"


@pytest.mark.asyncio
async def test_resolve_pick_action_fails_cleanly_when_the_item_is_gone(engine, helper_client):
    from backend.bot.handlers import retrieve as handler

    with patch.object(retrieve_service, "do_retrieve", AsyncMock()) as spy:
        _title, body, _buttons = await handler._resolve_pick_action(None, "S0002", CHAT)
        _title2, body2, _buttons2 = await handler._resolve_pick_action(None, "", CHAT)

    spy.assert_not_awaited()
    assert "No item found for `S0002`" in body and "Nothing was retrieved" in body
    assert "No item selected" in body2


# ── the rename/move carry-through regression (listener already popped) ─────

@pytest.mark.asyncio
async def test_rename_input_uses_the_extra_carry_through(engine, helper_client):
    from backend.bot.handlers import retrieve as handler

    input_state.set_pending(OWNER, "retrieve_item", handler._retrieve_rename_input_handler, CHAT, "p")
    input_state.clear_pending(OWNER)  # the listener pops the state before calling

    with patch.object(retrieve_service, "do_rename", AsyncMock(return_value="✅ Renamed.")) as spy:
        await handler._retrieve_rename_input_handler(
            "Semester Two", CHAT, 42, -100, 7, extra="S0001"
        )

    spy.assert_awaited_once_with(OWNER, "S0001", "Semester Two")
    assert "Renamed" in _edited_body(helper_client)
    engine.delete_messages.assert_awaited_once_with(CHAT, [42])


@pytest.mark.asyncio
async def test_rename_input_without_carry_through_never_writes(engine, helper_client):
    from backend.bot.handlers import retrieve as handler

    with patch.object(retrieve_service, "do_rename", AsyncMock()) as spy:
        await handler._retrieve_rename_input_handler("Semester Two", CHAT, 42, -100, 7, extra=None)
        await handler._retrieve_rename_input_handler("   ", CHAT, 42, -100, 7, extra="S0001")

    spy.assert_not_awaited()
    assert helper_client.edit_message.await_count == 2


@pytest.mark.asyncio
async def test_move_input_uses_the_extra_carry_through(engine, helper_client):
    from backend.bot.handlers import retrieve as handler

    with patch.object(retrieve_service, "do_move", AsyncMock(return_value="✅ Moved.")) as spy:
        await handler._retrieve_move_input_handler("unfiled", CHAT, 42, -100, 7, extra="S0001")

    spy.assert_awaited_once_with(OWNER, "S0001", None)
    assert "Moved" in _edited_body(helper_client)
