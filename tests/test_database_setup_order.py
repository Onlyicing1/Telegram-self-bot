"""M3.x — the canonical database setup: ONE complete SQL script in §31.

DATABASE_ARCHITECTURE.md §31 must give the owner exactly ONE thing to paste: a
single fenced SQL block that contains the complete ordered setup — the canonical
reconciliation snapshot first, then every later additive migration. This module pins
that property so the document cannot regress to a two-stage workflow (a "step 0"
block plus a second block) or a shell `cat` command.

The block is transcribed from the migration files, statement for statement, with
the migrations' prose comments removed so the artifact is pure runnable SQL. The
comparison therefore strips SQL comments before comparing.

No database is contacted here; every assertion reads the repository's own files.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DOC_PATH = REPO / "DATABASE_ARCHITECTURE.md"
MIGRATIONS_DIR = REPO / "supabase" / "migrations"

RECONCILE = "20260920000001_reconcile_canonical_schema.sql"
VAULT_PART1 = "20260919000001_create_api_credential_vault.sql"
VAULT_PART2 = "20260919000002_credential_vault_management.sql"
DISPLAY_NAME = "20260921000001_add_saved_items_display_name.sql"
SEARCH_INDEXES = "20260922000001_add_saved_items_search_indexes.sql"
TTS_SETTINGS = "20260923000001_add_ai_config_tts_settings.sql"
TODO_SCHEDULE_TYPE = "20260926000001_add_todo_schedule_type.sql"
TODO_STEPS = "20260927000001_add_todo_steps.sql"
EMOJI_TABLES = "20260928000001_create_emoji_reaction_tables.sql"
PRIVATE_MESSAGE_TABLES = "20260929000001_create_private_message_tables.sql"
PRIVATE_MESSAGE_COUNTER_RESET = "20261016000001_add_private_message_counter_reset.sql"
ANTI_EDIT_DELETE_TABLES = "20261017000001_create_anti_edit_delete_tables.sql"
PRIVATE_MESSAGE_ACCESS = "20261020144423_harden_private_message_table_access.sql"
AUTO_REACTION_SETTINGS = "20261020160000_add_panel_settings_auto_reaction_settings.sql"

#: The order §31 documents — verified against the sources, not assumed. The TTS
#: settings migration, the Todo schedule type, the Todo steps table, the Emoji &
#: Reaction tables, the Private Message Management tables, the Private Message
#: counter reset, the Anti-Edit / Anti-Delete tables, the Private Message access
#: hardening and the Auto-Reaction columns are LATER additive schema changes, so
#: they are appended last, newest last (the documented rule: a later change never
#: edits the snapshot). The counter-reset part is the one part that alters a table
#: an earlier part creates, so it must follow the Private Message tables it
#: extends; the access-hardening part must follow them too, because it revokes the
#: grants and drops the policies that part created.
DOCUMENTED_ORDER = (
    RECONCILE, VAULT_PART1, VAULT_PART2, DISPLAY_NAME, SEARCH_INDEXES, TTS_SETTINGS,
    TODO_SCHEDULE_TYPE, TODO_STEPS, EMOJI_TABLES, PRIVATE_MESSAGE_TABLES,
    PRIVATE_MESSAGE_COUNTER_RESET, ANTI_EDIT_DELETE_TABLES, PRIVATE_MESSAGE_ACCESS,
    AUTO_REACTION_SETTINGS,
)

#: The banner that marks the ONE complete deployment block.
SETUP_BANNER = "ONE COMPLETE SUPABASE SETUP SCRIPT"

PART_MARKER = re.compile(r"--[^\n]*\bPART (\d+) of (\d+)\b[^\n]*\n")

#: The five Vault PART 2 management functions that must be physically present.
VAULT_FUNCTIONS = (
    "api_credential_list",
    "api_credential_create",
    "api_credential_replace_secret",
    "api_credential_update",
    "api_credential_delete",
)

#: Placeholder / two-stage / shell text the deployment block must never contain.
FORBIDDEN_IN_BLOCK = (
    "see §30.5",
    "paste §30.5",
    "paste it first",
    "after step 0",
    "cat supabase/",
    "run separately",
    "[truncated]",
    "...",
    "same as above",
    "omitted",
)

DOC = DOC_PATH.read_text(encoding="utf-8")


# ─── parsing helpers ─────────────────────────────────────────────────────────

def _sql_blocks(text: str) -> list[str]:
    return re.findall(r"```sql\n(.*?)```", text, flags=re.S)


def _statements(text: str) -> list[str]:
    """The executable statement lines of a SQL text (comments and blanks removed)."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("--")
    ]


def _migration(name: str) -> str:
    return (MIGRATIONS_DIR / name).read_text(encoding="utf-8")


def _section_31() -> str:
    marker = "## 31. Canonical Database Setup"
    assert marker in DOC, "DATABASE_ARCHITECTURE.md must carry the setup section §31"
    start = DOC.index(marker)
    nxt = DOC.find("\n## ", start + len(marker))
    return DOC[start:] if nxt == -1 else DOC[start:nxt]


def _section(marker: str) -> str:
    start = DOC.index(marker)
    nxt = DOC.find("\n## ", start + len(marker))
    return DOC[start:] if nxt == -1 else DOC[start:nxt]


def _flat(section: str) -> str:
    """Prose assertions must not depend on blockquote markers or line wrapping."""
    unquoted = re.sub(r"(?m)^\s*>\s?", "", section)
    return re.sub(r"\s+", " ", unquoted)


def _setup_block() -> str:
    hits = [b for b in _sql_blocks(DOC) if SETUP_BANNER in b]
    assert len(hits) == 1, (
        "DATABASE_ARCHITECTURE.md must embed the ONE complete setup block exactly once"
    )
    return hits[0]


def _is_complete_deployment_block(block: str) -> bool:
    """A *complete* deployment block establishes the canonical schema end to end."""
    return (
        "CREATE TABLE IF NOT EXISTS saved_items" in block
        and "CREATE TABLE IF NOT EXISTS ai_config" in block
        and "missing_canonical_column" in block
    )


def _part_spans(block: str) -> dict[str, str]:
    """The text of each part, keyed by the migration its banner names."""
    marks = list(PART_MARKER.finditer(block))
    total = len(DOCUMENTED_ORDER)
    assert [m.group(1) for m in marks] == [str(i) for i in range(1, total + 1)], (
        f"the setup block must carry parts 1–{total} in that order"
    )
    assert {m.group(2) for m in marks} == {str(total)}, (
        "every part banner must state the same total"
    )
    spans: dict[str, str] = {}
    for i, mark in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(block)
        spans[DOCUMENTED_ORDER[i]] = block[mark.end():end]
    return spans


# ─── 1. exactly one complete deployment block, inside §31 ────────────────────

def test_the_document_carries_exactly_one_setup_block():
    block = _setup_block()
    assert block.strip(), "the setup block must not be empty"
    assert "NOTIFY pgrst, 'reload schema'" in block
    assert SETUP_BANNER in _section_31(), "the setup block must live in §31"


def test_no_second_complete_deployment_block_exists():
    complete = [b for b in _sql_blocks(DOC) if _is_complete_deployment_block(b)]
    assert len(complete) == 1, (
        "there must be exactly ONE complete deployment SQL block; found "
        f"{len(complete)}"
    )
    assert complete[0] == _setup_block(), "the only complete block must be the §31.3 block"


def test_the_bootstrap_section_no_longer_carries_a_second_executable_block():
    section = _section("## Canonical Supabase Bootstrap SQL")
    assert "§31.3" in section, (
        "the bootstrap section must point at §31.3 for the executable artifact"
    )
    assert not any(
        _is_complete_deployment_block(b) for b in _sql_blocks(section)
    ), "the bootstrap section must not keep a second copy of the canonical script"


# ─── 2. the block contains all five migrations, in order, verbatim ───────────

@pytest.mark.parametrize("name", list(DOCUMENTED_ORDER))
def test_every_part_is_statement_identical_to_its_migration(name: str):
    """Every part is a transcription of its migration, not a paraphrase of it."""
    part = _statements(_part_spans(_setup_block())[name])
    source = _statements(_migration(name))
    assert part, f"{name} is empty inside the setup block"
    assert part == source, (
        f"{name} diverged from its migration; the block must copy the migration's "
        "statements verbatim"
    )


def test_every_part_banner_names_its_migration_in_order():
    block = _setup_block()
    for index, name in enumerate(DOCUMENTED_ORDER, start=1):
        assert re.search(
            rf"--[^\n]*PART {index} of {len(DOCUMENTED_ORDER)} — {re.escape(name)}",
            block,
        ), f"part {index} must name {name} in its banner"


def test_the_canonical_reconciliation_sql_is_physically_present():
    part = _part_spans(_setup_block())[RECONCILE]
    for needle in (
        "BEGIN;",
        "COMMIT;",
        "CREATE TABLE IF NOT EXISTS saved_items",
        "CREATE TABLE IF NOT EXISTS ai_config",
        "CREATE TABLE IF NOT EXISTS ai_tasks",
        "ai_config_stt_passes_range",
        "missing_canonical_column",
    ):
        assert needle in part, f"the reconciliation SQL is missing {needle!r}"


def test_the_five_vault_management_functions_are_physically_present():
    part = _part_spans(_setup_block())[VAULT_PART2]
    for fn in VAULT_FUNCTIONS:
        assert f"FUNCTION public.{fn}" in part, f"{fn} is missing from the setup block"
    for guarantee in ("SECURITY DEFINER", "SET search_path = ''", "OWNER TO postgres", "GRANT EXECUTE"):
        assert guarantee in part, f"the Vault part must preserve {guarantee}"


def test_the_display_name_column_precedes_its_index():
    block = _setup_block()
    assert "ADD COLUMN IF NOT EXISTS display_name text" in block
    assert "CREATE INDEX IF NOT EXISTS idx_saved_items_display_name_trgm" in block
    assert "idx_saved_items_tags" in block
    assert block.index(DISPLAY_NAME) < block.index(SEARCH_INDEXES), (
        "the display-name migration must be applied before the index that needs it"
    )
    spans = _part_spans(block)
    assert "ADD COLUMN IF NOT EXISTS display_name text" in spans[DISPLAY_NAME]
    assert "idx_saved_items_display_name_trgm" in spans[SEARCH_INDEXES]
    assert "CREATE EXTENSION IF NOT EXISTS pg_trgm" in spans[SEARCH_INDEXES]


def test_the_todo_schedule_type_part_carries_both_constraints():
    """The basic Todo part is complete in the block."""
    spans = _part_spans(_setup_block())
    assert TODO_SCHEDULE_TYPE in spans, "the Todo schedule type must stay embedded"
    part = spans[TODO_SCHEDULE_TYPE]
    assert "DROP CONSTRAINT IF EXISTS ai_tasks_schedule_type_check" in part
    assert "'todo'" in part, "the widened schedule_type enum must name the Todo type"
    assert "DROP CONSTRAINT IF EXISTS ai_tasks_actions_count" in part
    assert "jsonb_array_length(actions) BETWEEN 0 AND 5" in part, (
        "only a Todo row may store no action"
    )
    assert "jsonb_array_length(actions) BETWEEN 1 AND 5" in part, (
        "every scheduled row must keep the 1-5 action guarantee"
    )


def test_the_todo_steps_part_carries_the_whole_table_before_the_newest_change():
    """The multi-step table stays embedded, whole, before the newest add-on."""
    spans = _part_spans(_setup_block())
    assert TODO_STEPS in spans, "the Todo steps table must stay embedded"
    assert list(spans).index(TODO_STEPS) < list(spans).index(EMOJI_TABLES), (
        "the Emoji tables are newer, so they close the block after todo_steps"
    )
    part = spans[TODO_STEPS]
    for needle in (
        "CREATE TABLE IF NOT EXISTS todo_steps",
        "REFERENCES ai_tasks(id) ON DELETE CASCADE",
        "CONSTRAINT todo_steps_task_position_key",
        "UNIQUE (task_id, position)",
        "CHECK (status IN ('active', 'completed'))",
        "ENABLE ROW LEVEL SECURITY",
        "anon_select_todo_steps",
        "NOTIFY pgrst, 'reload schema'",
    ):
        assert needle in part, f"the Todo steps part is missing {needle!r}"
    # The parent table must exist before the steps table can reference it.
    assert block_index(spans, TODO_STEPS) > block_index(spans, RECONCILE), (
        "the steps table must come after the snapshot that creates ai_tasks"
    )


def test_the_anti_edit_delete_part_carries_the_two_tables():
    """The Anti-Edit / Anti-Delete tables stay embedded, whole, just before the
    part that hardens the Private Message tables — which must follow the part
    that creates those tables and their policies."""
    spans = _part_spans(_setup_block())
    assert list(spans)[-1] == AUTO_REACTION_SETTINGS, (
        "the newest additive migration (the Auto-Reaction columns) closes the block"
    )
    order = list(spans)
    assert order[-2] == PRIVATE_MESSAGE_ACCESS, (
        "the Private Message access hardening must follow the tables it hardens"
    )
    assert order.index(PRIVATE_MESSAGE_ACCESS) == order.index(ANTI_EDIT_DELETE_TABLES) + 1
    assert order.index(ANTI_EDIT_DELETE_TABLES) == order.index(PRIVATE_MESSAGE_COUNTER_RESET) + 1
    assert order.index(PRIVATE_MESSAGE_COUNTER_RESET) == order.index(PRIVATE_MESSAGE_TABLES) + 1, (
        "the Private Message tables must stay before the parts that alter and harden them"
    )
    part = spans[ANTI_EDIT_DELETE_TABLES]
    for table in ("aed_config", "aed_archive_entries"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in part, (
            f"the Anti-Edit / Anti-Delete part is missing the {table} table"
        )
    for guarantee in (
        "enabled",
        "categories",
        "retention_days",
        "kind IN ('snapshot', 'edit', 'delete')",
        "CHECK (chat_id > 0)",
        "idx_aed_archive_entries_owner_captured",
        "ENABLE ROW LEVEL SECURITY",
        'DROP POLICY IF EXISTS "anon_select_aed_config" ON aed_config;',
        'DROP POLICY IF EXISTS "anon_select_aed_archive_entries" ON aed_archive_entries;',
        "REVOKE ALL ON TABLE aed_config FROM PUBLIC, anon, authenticated;",
        "REVOKE ALL ON TABLE aed_archive_entries FROM PUBLIC, anon, authenticated;",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE aed_config TO service_role;",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE aed_archive_entries TO service_role;",
        "NOTIFY pgrst, 'reload schema'",
        "FROM information_schema.tables",
        "FROM information_schema.columns",
        "FROM bot_settings",
    ):
        assert guarantee in part, f"the Anti-Edit / Anti-Delete part is missing {guarantee!r}"
    assert "CREATE POLICY" not in part, "the AED tables must carry no policy at all"


def test_the_private_message_part_carries_the_two_tables():
    """The Private Message Management tables stay embedded, whole."""
    spans = _part_spans(_setup_block())
    assert PRIVATE_MESSAGE_TABLES in spans, "the Private Message tables must stay embedded"
    part = spans[PRIVATE_MESSAGE_TABLES]
    for table in ("private_message_config", "private_message_senders"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in part, (
            f"the Private Message part is missing the {table} table"
        )
    for guarantee in (
        "ai_max_replies_per_person",
        "ai_unlimited_replies",
        "ai_interval_seconds",
        "delete_incoming",
        "fixed_response_enabled",
        "ai_response_enabled",
        "PRIMARY KEY (owner_id, sender_id)",
        "idx_private_message_senders_owner_updated",
        "ENABLE ROW LEVEL SECURITY",
        "anon_select_private_message_config",
        "anon_select_private_message_senders",
        "NOTIFY pgrst, 'reload schema'",
        "FROM information_schema.tables",
        "FROM information_schema.columns",
        "FROM bot_settings",
    ):
        assert guarantee in part, f"the Private Message part is missing {guarantee!r}"


# ─── 5. the Private Message FINAL schema (tables + counter reset) ──────────────
#
# The two Private Message migrations are the ONE place that feature's final shape
# lives: part 10 creates the two tables, part 11 alters them with the automatic
# counter reset. These assertions are structural (a parsed column/constraint
# model), so they cannot be satisfied by a keyword that appears in a comment —
# and they compare the canonical block against the migrations themselves.

_PM_TABLES = ("private_message_config", "private_message_senders")
_TABLE_CONSTRAINT_PREFIXES = (
    "CONSTRAINT ", "PRIMARY KEY", "UNIQUE", "CHECK", "FOREIGN KEY",
)


def _sql_statements(text: str) -> list[str]:
    """Top-level statements of a SQL text with comments removed and space folded."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = "\n".join(
        line for line in text.splitlines() if not line.strip().startswith("--")
    )
    assert "$$" not in text, "this parser does not handle dollar-quoted bodies"
    statements: list[str] = []
    buf: list[str] = []
    in_quote = False
    depth = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if in_quote:
            buf.append(ch)
            if ch == "'":
                if i + 1 < len(text) and text[i + 1] == "'":
                    buf.append("'")
                    i += 2
                    continue
                in_quote = False
            i += 1
            continue
        if ch == "'":
            in_quote = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == ";" and depth == 0:
            statements.append(" ".join("".join(buf).split()))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = " ".join("".join(buf).split())
    if tail:
        statements.append(tail)
    return [statement for statement in statements if statement]


def _top_level_items(body: str) -> list[str]:
    """Split a CREATE TABLE body on its top-level commas."""
    items: list[str] = []
    buf: list[str] = []
    in_quote = False
    depth = 0
    i = 0
    while i < len(body):
        ch = body[i]
        if in_quote:
            buf.append(ch)
            if ch == "'":
                if i + 1 < len(body) and body[i + 1] == "'":
                    buf.append("'")
                    i += 2
                    continue
                in_quote = False
            i += 1
            continue
        if ch == "'":
            in_quote = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            items.append(" ".join("".join(buf).split()))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = " ".join("".join(buf).split())
    if tail:
        items.append(tail)
    return [item for item in items if item]


def private_message_schema(sql: str) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    """``(table -> {column: definition}, constraint name -> CHECK body)``.

    ``CREATE TABLE IF NOT EXISTS`` declares the table; every
    ``ALTER TABLE … ADD COLUMN IF NOT EXISTS`` adds the columns a later additive
    migration contributed, exactly as PostgreSQL would converge on them.
    """
    columns: dict[str, dict[str, str]] = {}
    constraints: dict[str, str] = {}
    for statement in _sql_statements(sql):
        create = re.fullmatch(
            r"CREATE TABLE IF NOT EXISTS (\w+) \((.*)\)", statement, flags=re.S
        )
        if create:
            table, body = create.group(1), create.group(2)
            if table not in _PM_TABLES:
                continue
            declared = columns.setdefault(table, {})
            for item in _top_level_items(body):
                if item.upper().startswith(_TABLE_CONSTRAINT_PREFIXES):
                    continue
                name, _, definition = item.partition(" ")
                declared[name] = definition
            continue
        added = re.fullmatch(
            r"ALTER TABLE (\w+) ADD COLUMN IF NOT EXISTS (\w+) (.+)", statement, flags=re.S
        )
        if added and added.group(1) in _PM_TABLES:
            table, name, definition = added.groups()
            columns.setdefault(table, {})[name] = definition
            continue
        constraint = re.fullmatch(
            r"ALTER TABLE (\w+) ADD CONSTRAINT (\w+) CHECK \((.+)\)", statement, flags=re.S
        )
        if constraint and constraint.group(1) in _PM_TABLES:
            constraints[constraint.group(2)] = constraint.group(3)
    return columns, constraints


def _canonical_pm_schema():
    spans = _part_spans(_setup_block())
    return private_message_schema(
        spans[PRIVATE_MESSAGE_TABLES] + "\n" + spans[PRIVATE_MESSAGE_COUNTER_RESET]
    )


def _migration_pm_schema():
    return private_message_schema(
        "\n".join(
            _migration(name)
            for name in (PRIVATE_MESSAGE_TABLES, PRIVATE_MESSAGE_COUNTER_RESET)
        )
    )


def test_the_canonical_sql_reaches_the_private_message_effective_schema():
    """The block's final Private Message schema equals the migrations' final schema."""
    canonical_columns, canonical_constraints = _canonical_pm_schema()
    migration_columns, migration_constraints = _migration_pm_schema()
    assert set(canonical_columns) == set(_PM_TABLES)
    for table in _PM_TABLES:
        assert canonical_columns[table] == migration_columns[table], (
            f"{table}: the block and its migrations disagree on the final columns"
        )
    assert canonical_constraints == migration_constraints, (
        "the block and its migrations disagree on the Private Message CHECK constraints"
    )


def test_the_counter_reset_columns_are_declared_with_their_final_definitions():
    columns, _ = _canonical_pm_schema()
    config = columns["private_message_config"]
    senders = columns["private_message_senders"]
    assert config["ai_counter_reset_enabled"] == "boolean NOT NULL DEFAULT false", (
        "the automatic reset must default to OFF"
    )
    assert config["ai_counter_reset_seconds"] == "integer NOT NULL DEFAULT 86400", (
        "the reset interval must default to one day"
    )
    # The window anchor is NULLABLE on purpose: NULL means "no window recorded
    # yet", and a default would backfill an instant the migration cannot know.
    assert senders["window_started_at"] == "timestamptz"
    assert "NOT NULL" not in senders["window_started_at"]
    assert "DEFAULT" not in senders["window_started_at"]


def test_the_reset_interval_check_constraint_carries_the_panel_range():
    from backend.services import private_message_service as svc

    _, constraints = _canonical_pm_schema()
    body = constraints["private_message_config_reset_interval_range"]
    parsed = re.fullmatch(
        r"ai_counter_reset_seconds BETWEEN (\d+) AND (\d+)", body
    )
    assert parsed, f"unexpected CHECK body: {body!r}"
    low, high = int(parsed.group(1)), int(parsed.group(2))
    assert low == svc.MIN_AI_COUNTER_RESET_SECONDS == 60
    assert high == svc.MAX_AI_COUNTER_RESET_SECONDS == 365 * 24 * 60 * 60


def test_the_final_schema_covers_every_column_the_repository_writes():
    from backend.db import client as db_client

    columns, _ = _canonical_pm_schema()
    config = columns["private_message_config"]
    missing = [name for name in db_client._PM_CONFIG_COLUMNS if name not in config]
    assert missing == [], (
        "the configuration write names columns the canonical SQL does not create: "
        f"{missing}"
    )
    for name in ("owner_id", "updated_at"):
        assert name in config, f"private_message_config is missing {name}"
    senders = columns["private_message_senders"]
    for name in (
        "owner_id", "sender_id", "ai_replies_used", "last_ai_reply_at",
        "window_started_at", "updated_at",
    ):
        assert name in senders, f"private_message_senders is missing {name}"


def test_the_counter_reset_columns_are_not_folded_into_the_snapshot_or_part_10():
    """The columns arrive as the later ALTER migration, never by editing part 1/10."""
    snapshot = _part_spans(_setup_block())[RECONCILE]
    for name in ("ai_counter_reset_enabled", "ai_counter_reset_seconds", "window_started_at"):
        assert name not in snapshot, (
            f"{name} belongs to the counter-reset migration, not the frozen snapshot"
        )
    tables_part = _part_spans(_setup_block())[PRIVATE_MESSAGE_TABLES]
    for name in ("ai_counter_reset_enabled", "ai_counter_reset_seconds", "window_started_at"):
        assert name not in tables_part, (
            f"{name} must stay in part 11, exactly as its own migration adds it"
        )


def test_the_counter_reset_part_is_idempotent_and_additive():
    part = _part_spans(_setup_block())[PRIVATE_MESSAGE_COUNTER_RESET]
    statements = _sql_statements(part)
    assert statements, "the counter-reset part must not be empty"
    for statement in statements:
        upper = statement.upper()
        if " ADD COLUMN " in f" {upper} ":
            assert "ADD COLUMN IF NOT EXISTS" in upper, (
                f"a re-run must be a no-op: {statement!r}"
            )
        if " DROP CONSTRAINT " in f" {upper} ":
            assert "DROP CONSTRAINT IF EXISTS" in upper, (
                f"a re-run must be a no-op: {statement!r}"
            )
        for forbidden in ("DROP TABLE", "DROP COLUMN", "TRUNCATE", "DELETE FROM"):
            assert forbidden not in upper, (
                f"the counter-reset part must stay additive: {statement!r}"
            )
    # It ALTERs the two tables part 10 creates and creates none of its own.
    assert "CREATE TABLE" not in "\n".join(statements).upper()
    assert "ALTER TABLE private_message_config" in part
    assert "ALTER TABLE private_message_senders" in part


def test_the_emoji_part_carries_the_five_tables():
    """The Emoji & Reaction persistence keeps all five of its tables."""
    spans = _part_spans(_setup_block())
    part = spans[EMOJI_TABLES]
    for table in (
        "emoji_library",
        "emoji_categories",
        "emoji_mappings",
        "emoji_state",
        "emoji_chat_overrides",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in part, (
            f"the Emoji & Reaction part is missing the {table} table"
        )
    for guarantee in (
        "ADD COLUMN IF NOT EXISTS is_custom",
        "ADD COLUMN IF NOT EXISTS source_category_ids",
        "idx_emoji_library_owner_created",
        "idx_emoji_categories_owner_created",
        "idx_emoji_mappings_owner_category_created",
        "ENABLE ROW LEVEL SECURITY",
        "anon_select_emoji_library",
        "anon_select_emoji_chat_overrides",
        "NOTIFY pgrst, 'reload schema'",
        "FROM information_schema.tables",
        "FROM information_schema.columns",
    ):
        assert guarantee in part, f"the Emoji & Reaction part is missing {guarantee!r}"
    assert "CREATE TABLE IF NOT EXISTS todo_steps" in spans[TODO_STEPS]


def block_index(spans: dict[str, str], name: str) -> int:
    """Position of one part within the documented order (for ordering claims)."""
    order = list(spans)
    return order.index(name)


def test_the_vault_part_two_follows_part_one():
    block = _setup_block()
    assert "CREATE TABLE IF NOT EXISTS public.api_credentials" in _part_spans(block)[VAULT_PART1]
    assert "public.api_credentials" in _part_spans(block)[VAULT_PART2]
    assert block.index(VAULT_PART1) < block.index(VAULT_PART2)


# ─── 3. no placeholder, no two-stage workflow, no shell ──────────────────────

def test_the_setup_block_has_no_placeholder_or_two_stage_text():
    block = _setup_block().lower()
    for token in FORBIDDEN_IN_BLOCK:
        assert token.lower() not in block, f"the setup block must not contain {token!r}"
    assert not re.search(r"<[a-z_]+>", block), "the setup block must not use a placeholder"


def test_the_section_has_no_two_stage_workflow_and_no_shell_command():
    section = _section_31()
    flat = _flat(section)
    for phrase in (
        "paste it first",
        "after step 0",
        "cat supabase/",
        "run separately",
        "same as above",
        "paste §30.5",
        "see §30.5",
    ):
        assert phrase not in flat, f"§31 must not describe the old workflow: {phrase!r}"
    assert "```bash" not in section, "§31 must not present a shell workflow"
    assert "No other SQL block in this document needs to be executed manually" in flat


def test_the_setup_block_is_additive_only():
    joined = "\n".join(_statements(_setup_block()))
    for forbidden in ("DROP TABLE", "DROP COLUMN", "DROP SCHEMA", "TRUNCATE", "DELETE FROM public.saved_items"):
        assert forbidden not in joined, f"the setup block must not contain {forbidden}"
    for target in ("DELETE FROM vault.secrets", "DELETE FROM public.api_credentials"):
        assert target in joined, f"the credential cleanup path must stay intact: {target}"
    deletes = re.findall(r"(?is)DELETE FROM.*?;", joined)
    assert deletes, "the credential cleanup paths must still be present"
    for statement in deletes:
        assert re.search(r"(?i)\bWHERE\b", statement), (
            f"every DELETE must be filtered to specific rows: {statement!r}"
        )


def test_the_setup_block_creates_no_secret_value():
    block = _setup_block()
    assert "vault.create_secret(" in block, "the create function must stay intact"
    for pattern in (r"sk-[A-Za-z0-9]", r"AIza[0-9A-Za-z_-]{10}", r"[A-Za-z0-9]{32,}-key-"):
        assert not re.search(pattern, block), f"a raw secret leaked into the block: {pattern}"


# ─── 4. the audited order and honest reporting ───────────────────────────────

def test_the_audited_order_is_recorded_in_the_audit_table():
    """§31.1 names every embedded migration, and its PART table keeps the order.

    The audit now also classifies the 22 pre-reconciliation migrations, so the
    order contract is checked against the table of the eleven EMBEDDED parts
    (the first table in §31.1), not against the whole prose.
    """
    section = _section_31()
    audit = section[: section.index("### 31.2")]
    assert "all 36 migrations" in _flat(audit) or "36 files" in audit, (
        "the audit must state that every repository migration was classified"
    )
    assert len(list(MIGRATIONS_DIR.glob("*.sql"))) == 36, (
        "the audited count must equal the migrations actually in the repository"
    )
    embedded_table = audit[audit.index("| Order | Migration") :]
    positions = [embedded_table.index(f"`{name}`") for name in DOCUMENTED_ORDER]
    assert positions == sorted(positions), (
        "§31.1 must list the embedded migrations in the documented execution order"
    )


def test_every_migration_named_in_the_section_exists():
    for name in DOCUMENTED_ORDER:
        assert (MIGRATIONS_DIR / name).is_file(), f"{name} is documented but absent"


def test_the_documented_deviations_are_stated_explicitly():
    flat = _flat(_section_31())
    assert "display_name" in flat
    assert "42703" in flat, "the failure the ordering prevents must be named"
    assert "thirteen later additive migrations" in flat, (
        "the later migrations must be acknowledged"
    )
    assert "add_todo_schedule_type" in flat, "the Todo schedule type must be acknowledged"
    assert "add_todo_steps" in flat, "the Todo steps table must be acknowledged"
    assert "create_emoji_reaction_tables" in flat, "the Emoji & Reaction tables must be acknowledged"
    assert "create_private_message_tables" in flat, "the Private Message tables must be acknowledged"
    assert "private_message_senders" in flat, "the per-person counters must be acknowledged"
    assert "add_private_message_counter_reset" in flat, (
        "the Private Message counter reset must be acknowledged"
    )
    assert "create_anti_edit_delete_tables" in flat, "the Anti-Edit / Anti-Delete tables must be acknowledged"
    assert "aed_archive_entries" in flat, "the AED archive table must be acknowledged"
    assert "harden_private_message_table_access" in flat, (
        "the Private Message access hardening must be acknowledged"
    )
    assert "add_panel_settings_auto_reaction_settings" in flat, (
        "the Auto-Reaction migration must be acknowledged"
    )
    assert "auto_reaction_enabled" in flat, "the Auto-Reaction columns must be acknowledged"
    assert "tts_provider" in flat, "the TTS settings columns must be acknowledged"


def test_the_section_does_not_pretend_the_sql_was_executed():
    flat = _flat(_section_31())
    assert "NOTHING has been executed against Supabase" in flat
    assert "no SQL was run" in flat
    assert "pending an owner action" in flat


# ─── 5. counts and the manual-action list must match the block ───────────────

def test_the_later_part_count_matches_the_block():
    """14 parts are the snapshot plus THIRTEEN later parts, and the prose says so."""
    total = len(DOCUMENTED_ORDER)
    assert total == 14
    flat = _flat(_section_31())
    assert "thirteen later additive migrations" in flat
    assert f"parts 2–{total}" in flat
    assert "twelve later additive migrations" not in flat, (
        "the snapshot is not one of the later parts"
    )


def test_the_manual_action_section_names_every_embedded_part():
    """§30.11 must not send the owner back to individual migration files."""
    section = _section("### 30.11 Manual Supabase action still required")
    flat = _flat(section)
    assert "§31.3" in section
    for name in DOCUMENTED_ORDER[1:]:
        assert name in section, f"§30.11 must name {name} as part of the one paste"
    assert "nine pending" not in flat
    assert "parts 4, 5, 7, 8 and 9" not in flat, (
        "every later part must be listed, never a stale subset"
    )
    assert "thirteen" in flat


def test_the_waiting_answer_migration_is_absorbed_by_part_one():
    """Its widened CHECK lives in the snapshot, so it needs no part of its own."""
    snapshot = _part_spans(_setup_block())[RECONCILE]
    assert "waiting_answer" in snapshot, (
        "part 1 must re-add the status CHECK in its final form"
    )
    status = _section("## 20. Migration Status")
    row = next(
        line
        for line in status.splitlines()
        if "20260927020000_add_waiting_answer_status.sql" in line
    )
    assert "PART 1 of §31.3" in row, (
        "the inventory must say the snapshot already establishes it"
    )
    assert "owner action required" not in row
    assert "**36** files" in _flat(status), (
        "the audited file count must be the current one (36)"
    )


def test_the_auto_reaction_columns_arrive_in_their_own_additive_part():
    """The two Auto-Reaction columns are additive and OFF by default.

    They were the one code-required column pair no migration created; part 14 is
    the additive migration that closes that gap without touching the snapshot.
    """
    spans = _part_spans(_setup_block())
    part = spans[AUTO_REACTION_SETTINGS]
    for column, definition in (
        ("auto_reaction_enabled", "boolean"),
        ("auto_reaction_emoji", "text"),
    ):
        assert f"ADD COLUMN IF NOT EXISTS {column} {definition};" in part, (
            f"part 14 must add {column} as {definition}"
        )
    assert "ALTER COLUMN auto_reaction_enabled SET DEFAULT false;" in part
    assert "ALTER COLUMN auto_reaction_emoji SET DEFAULT '';" in part
    for column in ("auto_reaction_enabled", "auto_reaction_emoji"):
        assert f"ALTER COLUMN {column} SET NOT NULL;" in part
    assert part.count("WHERE auto_reaction_enabled IS NULL") == 1
    assert part.count("WHERE auto_reaction_emoji IS NULL") == 1
    # OFF by default: nothing in the part can turn a reaction on.
    assert "SET DEFAULT true" not in part
    assert "= true" not in part
    assert "DELETE FROM" not in part and "DROP COLUMN" not in part, (
        "the Auto-Reaction part must stay additive"
    )
    # The snapshot and every earlier part stay untouched by it.
    for name in DOCUMENTED_ORDER[:-1]:
        assert "auto_reaction" not in spans[name], (
            f"{name} must not carry the Auto-Reaction columns"
        )
    assert "auto_reaction_enabled" in _migration(AUTO_REACTION_SETTINGS)
    section_6 = _flat(_section("## 6. panel_settings"))
    assert AUTO_REACTION_SETTINGS in section_6, (
        "§6 must attribute the columns to their migration"
    )
