"""SAVE V2 part 1 — data model + shared Save metadata.

Phase under test: the foundational Save V2 layer only. `saved_items` gains an
optional, owner-supplied ``display_name`` (its own forward-only migration), the
``tags`` column stops being filled with machine-generated hashtags, and one
shared ``SaveMetadata`` object carries both through the SAME ``execute_save``
pipeline that the manual Glass Save panel and the AI ``SaveTool`` already use.

Nothing later in Save V2 is implemented or claimed here: no search, no semantic
retrieval, no ambiguity handling, no management UI, no user-facing prompt for a
name or tags. The AI Save tool's public schema and the Save panels are
deliberately unchanged, so the tests below prove the DATA CONTRACT:
persistence, normalization, compatibility with existing rows, and owner
scoping — plus the migration's own safety (in the repository's established
"simulated execution" register, because no PostgreSQL server exists here).

The migration is validated with the reconciliation suite's simulator, so both
the canonical script and its additive successor are applied with PostgreSQL's
own semantics (CREATE IF NOT EXISTS is a no-op, ADD COLUMN IF NOT EXISTS is a
no-op when present, every unresolved table/column is a failure).
"""
from __future__ import annotations

import inspect
import re
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from telethon.tl.types import (
    DocumentAttributeFilename,
    MessageMediaDocument,
    MessageMediaPhoto,
)

from backend.db import client as db_client
from backend.services import save_service
from backend.services.save_service import (
    MAX_DISPLAY_NAME_CHARS,
    MAX_SAVE_TAGS,
    MAX_TAG_CHARS,
    SaveMetadata,
)

from tests.test_12_save_engine import (
    FakeDoc,
    FakeMessage,
    FakePhoto,
    MockClient,
    _save_code,
)
from tests.test_canonical_schema_reconciliation import (
    apply_script,
    fresh_model,
    snapshot,
    split_statements,
    strip_comments,
)

REPO = Path(__file__).resolve().parent.parent
MIGRATION_NAME = "20260921000001_add_saved_items_display_name.sql"
MIGRATION_PATH = REPO / "supabase" / "migrations" / MIGRATION_NAME
DOC = REPO / "DATABASE_ARCHITECTURE.md"

OWNER = 42
OTHER_OWNER = 4242

NAME = "University Weekly Schedule — Semester Two"


@pytest.fixture(autouse=True)
def reset_fallback():
    db_client._fallback["saved_items"] = []
    db_client._fallback["bot_logs"] = []
    yield
    db_client._fallback["saved_items"] = []
    db_client._fallback["bot_logs"] = []


def _doc_message() -> FakeMessage:
    doc = FakeDoc()
    doc.mime_type = "application/zip"
    doc.size = 777
    doc.attributes = [DocumentAttributeFilename("archive.zip")]
    return FakeMessage(media=MessageMediaDocument(document=doc, ttl_seconds=None))


def _photo_message() -> FakeMessage:
    return FakeMessage(media=MessageMediaPhoto(photo=FakePhoto(), ttl_seconds=None))


async def _save(metadata: SaveMetadata | None = None, message=None) -> tuple[str, dict]:
    """Run the real pipeline and return (confirmation, stored row)."""
    client = MockClient()
    msg = message if message is not None else _doc_message()
    if metadata is None:
        result = await save_service.execute_save(client, OWNER, msg, "UTC")
    else:
        result = await save_service.execute_save(client, OWNER, msg, "UTC", metadata=metadata)
    row = await db_client.query_save(_save_code(result))
    assert row is not None
    return result, row


# ─── the metadata contract itself ────────────────────────────────────────────

def test_save_metadata_is_one_immutable_contract():
    meta = SaveMetadata.from_raw(NAME, ["university", "semester-2"])
    assert meta.display_name == NAME
    assert meta.tags == ("university", "semester-2")
    # Frozen: the object handed to the pipeline cannot drift mid-save, and its
    # defaults are the no-metadata save (NULL name, no tags).
    assert SaveMetadata().display_name is None and SaveMetadata().tags == ()
    with pytest.raises(FrozenInstanceError):
        meta.display_name = "changed"


def test_empty_and_whitespace_names_mean_no_name():
    for raw in (None, "", "   ", "\n\t "):
        assert save_service.normalize_display_name(raw) is None


def test_display_name_is_trimmed_and_collapsed_deterministically():
    assert save_service.normalize_display_name("  Two   \n names ") == "Two names"


def test_a_name_over_the_bound_is_refused_never_truncated():
    with pytest.raises(ValueError):
        save_service.normalize_display_name("x" * (MAX_DISPLAY_NAME_CHARS + 1))
    assert save_service.normalize_display_name("x" * MAX_DISPLAY_NAME_CHARS) == "x" * MAX_DISPLAY_NAME_CHARS


def test_tags_are_trimmed_deduped_and_never_invented():
    assert save_service.normalize_tags(None) == ()
    assert save_service.normalize_tags([]) == ()
    assert save_service.normalize_tags(["   "]) == ()
    assert save_service.normalize_tags(["a", "A", " a "]) == ("a",)  # case-insensitive dedupe, first spelling wins
    # A bare string is ONE tag, never one tag per character.
    assert save_service.normalize_tags("university") == ("university",)


def test_tag_limits_are_refused_never_truncated():
    with pytest.raises(ValueError):
        save_service.normalize_tags(["x" * (MAX_TAG_CHARS + 1)])
    with pytest.raises(ValueError):
        save_service.normalize_tags([f"tag{i}" for i in range(MAX_SAVE_TAGS + 1)])


# ─── persistence through the shared pipeline ─────────────────────────────────

@pytest.mark.asyncio
async def test_save_without_metadata_still_succeeds_and_invents_nothing():
    result, row = await _save()

    assert "Saved Successfully" in result
    assert row["tags"] == []
    # The column's key is omitted entirely when the owner gave no name, which
    # is what keeps a metadata-less save working before the migration is
    # applied (PostgREST rejects an INSERT naming an unknown column).
    assert "display_name" not in row
    assert row.get("display_name") is None


@pytest.mark.asyncio
async def test_save_persists_the_display_name():
    _, row = await _save(SaveMetadata.from_raw(NAME, None))

    assert row["display_name"] == NAME
    assert row["tags"] == []


@pytest.mark.asyncio
async def test_save_persists_normalized_owner_tags():
    _, row = await _save(SaveMetadata.from_raw(None, ["university", "  semester-2 ", "University", "  "]))

    assert row["tags"] == ["university", "semester-2"]
    assert "display_name" not in row


@pytest.mark.asyncio
async def test_save_persists_name_and_tags_together():
    _, row = await _save(SaveMetadata.from_raw(NAME, ["university", "schedule"]))

    assert row["display_name"] == NAME
    assert row["tags"] == ["university", "schedule"]


@pytest.mark.asyncio
async def test_an_invalid_name_refuses_before_any_transfer():
    client = MockClient()
    oversize = SaveMetadata(display_name="x" * (MAX_DISPLAY_NAME_CHARS + 1))

    result = await save_service.execute_save(client, OWNER, _doc_message(), "UTC", metadata=oversize)

    assert result.startswith("⚠️ Nothing was saved")
    assert client.calls == []  # no download, no upload, no Telegram side effect
    assert db_client._fallback["saved_items"] == []


@pytest.mark.asyncio
async def test_display_name_is_independent_of_save_code():
    _, row = await _save(SaveMetadata.from_raw(NAME, None))
    code = row["save_code"]
    assert re.fullmatch(r"S\w{4}", code)

    renamed = await db_client.update_save_field(OWNER, code, "display_name", "Renamed Semester Two")
    after = await db_client.query_save(code)

    assert renamed is not None
    assert after["display_name"] == "Renamed Semester Two"
    assert after["save_code"] == code  # identity is never touched by naming


@pytest.mark.asyncio
async def test_two_saves_of_one_source_get_distinct_codes_and_their_own_names():
    first, _ = await _save(SaveMetadata.from_raw("First name", None))
    second, _ = await _save(SaveMetadata.from_raw("Second name", None))

    assert _save_code(first) != _save_code(second)
    assert (await db_client.query_save(_save_code(first)))["display_name"] == "First name"
    assert (await db_client.query_save(_save_code(second)))["display_name"] == "Second name"


@pytest.mark.asyncio
async def test_owner_scoping_is_preserved():
    _, row = await _save(SaveMetadata.from_raw(NAME, ["university"]))
    code = row["save_code"]

    other = [s for s in db_client._fallback["saved_items"] if s.get("owner_id") == OTHER_OWNER]
    assert other == []
    assert await db_client.query_save(code) is not None  # readable by code
    assert (await db_client.list_saves(OWNER))[0] == [row]  # owner-scoped listing
    assert (await db_client.list_saves(OTHER_OWNER))[0] == []


@pytest.mark.asyncio
async def test_existing_save_code_retrieval_still_works():
    _, row = await _save(SaveMetadata.from_raw(NAME, ["university"]))
    code = row["save_code"]

    loaded = await db_client.query_save(code.lower())  # query_save upper-cases
    assert loaded["save_code"] == code
    assert loaded["saved_msg_id"] == 600
    assert loaded["caption"]  # the caption contract is unchanged


# ─── compatibility with rows that predate Save V2 ────────────────────────────

@pytest.mark.asyncio
async def test_legacy_rows_keep_their_hashtags_and_stay_readable():
    legacy = {
        "save_code": "S0900",
        "save_type": "deep",
        "owner_id": OWNER,
        "media_type": "Document",
        "tags": ["#saved", "#saved_document", "#saved_2026"],
        "caption": "📄 S0900 · DEEP",
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    db_client._fallback["saved_items"].append(legacy)

    assert (await db_client.query_save("S0900"))["tags"] == legacy["tags"]
    listed, total = await db_client.list_saves(OWNER)
    assert total == 1 and listed[0]["tags"] == legacy["tags"]
    # A legacy row simply has no name; it is never backfilled or rewritten.
    assert "display_name" not in legacy


@pytest.mark.asyncio
async def test_new_saves_do_not_invent_tags_but_the_caption_keeps_the_hashtag_line():
    client = MockClient()
    result = await save_service.execute_save(client, OWNER, _photo_message(), "UTC")
    row = await db_client.query_save(_save_code(result))

    assert row["tags"] == []
    # The saved message looks exactly as before: presentation is unchanged.
    caption = [c for c in client.calls if c[0] == "send_file"][0][3]["caption"]
    assert "#saved" in caption and "#saved_photo" in caption


# ─── the one shared contract, used by both adapters ──────────────────────────

@pytest.mark.asyncio
async def test_both_adapters_can_supply_the_same_metadata_contract():
    """Wiring the surfaces is a LATER phase; the contract they will share is not.

    The manual panel will build its metadata from typed text; the AI tool will
    build it from an argument dict. Both spellings must produce one object with
    one meaning, and that object must be exactly what the single pipeline takes.
    """
    from_panel = SaveMetadata.from_raw("University schedule", ["university", "semester 2"])
    from_ai_arguments = SaveMetadata.from_raw(
        **{"display_name": "University schedule", "tags": ["university", "semester 2"]}
    )
    assert from_panel == from_ai_arguments

    _, manual_row = await _save(from_panel)
    _, ai_row = await _save(from_ai_arguments)
    assert manual_row["display_name"] == ai_row["display_name"]
    assert manual_row["tags"] == ai_row["tags"]

    # One metadata parameter, optional, keyword-only — and both adapters funnel
    # into the same pipeline rather than writing the table themselves.
    params = inspect.signature(save_service.execute_save).parameters
    assert list(params)[:4] == ["client", "owner_id", "reply_msg", "tz_str"]
    assert params["metadata"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["metadata"].default is None
    from backend.ai.tools import save as save_tool
    from backend.bot.handlers import save as save_handler

    for module in (save_tool, save_handler):
        assert "save_service.execute_save" in inspect.getsource(module) or (
            "execute_save" in inspect.getsource(module)
        )


# ─── the migration (simulated execution, no database is touched) ─────────────

def migration_statements() -> list[str]:
    return split_statements(MIGRATION_PATH.read_text(encoding="utf-8"))


def migration_executable() -> str:
    return strip_comments(MIGRATION_PATH.read_text(encoding="utf-8"))


def test_migration_is_a_single_additive_column_change():
    statements = [strip_comments(s) for s in migration_statements() if strip_comments(s)]
    alters = [s for s in statements if re.match(r"^ALTER TABLE", s, re.I)]

    assert len(alters) == 1, alters
    assert re.fullmatch(
        r"ALTER TABLE saved_items\s+ADD COLUMN IF NOT EXISTS display_name text",
        alters[0],
        re.I,
    ), alters[0]
    # It must not restate the table: the canonical script stays the one
    # definition, and this file is only its additive successor.
    assert not re.search(r"\bCREATE\s+TABLE\b", migration_executable(), re.I)
    assert not re.search(r"\bCREATE\s+(UNIQUE\s+)?INDEX\b", migration_executable(), re.I)


def test_migration_is_idempotent_and_converges():
    """CASE A + CASE B: current schema → apply → success; twice → identical."""
    once = apply_script(fresh_model(), migration_statements())
    twice = apply_script(apply_script(fresh_model(), migration_statements()), migration_statements())

    assert once.contains("saved_items", "display_name")
    assert snapshot(once) == snapshot(twice)


def test_migration_adds_a_nullable_text_column_with_no_default():
    schema = apply_script(fresh_model(), migration_statements())
    column = schema.tables["saved_items"].columns["display_name"]

    # Nullable with no default: NULL is the documented "the owner gave no name".
    assert column.not_null is False
    assert column.default is None
    # The simulator models presence/nullability/default (never column TYPES —
    # §30.8 states that limitation), so the declared type is read from the
    # statement the database will actually execute.
    assert "display_name text" in migration_executable()
    assert not re.search(r"display_name\s+text\s+NOT NULL", migration_executable(), re.I)


def test_migration_preserves_existing_rows_codes_and_tags():
    """CASE C + D + E: rows, save_code and the existing tags all survive."""
    schema = fresh_model()
    schema.insert_row(
        "saved_items",
        {
            "save_code": "S0001",
            "save_type": "deep",
            "owner_id": OWNER,
            "tags": ["#saved", "#saved_photo"],
            "caption": "🖼 S0001 · DEEP",
        },
    )
    schema.insert_row(
        "saved_items",
        {
            "save_code": "SAXCK",
            "save_type": "deep",
            "owner_id": OTHER_OWNER,
            "tags": [],
            "caption": "📄 SAXCK · DEEP",
        },
    )

    after = apply_script(schema, migration_statements())
    rows = after.tables["saved_items"].rows

    assert [r["save_code"] for r in rows] == ["S0001", "SAXCK"]
    assert rows[0]["tags"] == ["#saved", "#saved_photo"]
    assert rows[1]["tags"] == []
    assert [r["owner_id"] for r in rows] == [OWNER, OTHER_OWNER]
    assert [r.get("display_name") for r in rows] == [None, None]
    assert [r["caption"] for r in rows] == ["🖼 S0001 · DEEP", "📄 SAXCK · DEEP"]


def test_migration_reruns_safely_on_a_database_that_already_has_the_column():
    schema = apply_script(fresh_model(), migration_statements())
    schema.insert_row("saved_items", {"save_code": "S7", "save_type": "deep", "owner_id": OWNER})
    before = snapshot(schema)

    assert snapshot(apply_script(schema, migration_statements())) == before


def test_migration_reloads_the_postgrest_schema_cache():
    # Without this the API keeps rejecting an INSERT that names display_name.
    assert re.search(r"NOTIFY\s+pgrst\s*,\s*'reload schema'", migration_executable())


def test_migration_documents_its_contract_and_names_no_other_store():
    text = MIGRATION_PATH.read_text(encoding="utf-8")

    assert "MANUAL SUPABASE ACTION REQUIRED" in text
    assert "display_name" in text
    assert "ROLLBACK" in text.upper() or "Rollback" in text
    # It must not touch the credential vault or any other table.
    assert not re.search(r"\bapi_credential", text, re.I)
    assert not re.search(r"\bvault\.", text, re.I)
    assert set(re.findall(r"ALTER TABLE (\w+)", migration_executable())) == {"saved_items"}


def test_documentation_and_migration_status_stay_in_sync():
    doc = DOC.read_text(encoding="utf-8")

    assert MIGRATION_NAME in doc, "§20 migration status must list the new migration"
    assert re.search(r"\| `display_name` \| `text` \|", doc), "§2 must document the column"
    assert "canonical_bootstrap.sql" in doc  # the canonical copy is still referenced
