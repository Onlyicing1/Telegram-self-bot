"""M3.x — the canonical database setup: ONE complete SQL script in §31.

DATABASE_ARCHITECTURE.md §31 must give the owner exactly ONE thing to paste: a
single fenced SQL block that contains the complete ordered setup — the canonical
reconciliation snapshot first, then the five pending migrations. This module pins
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

#: The order §31 documents — verified against the sources, not assumed. The TTS
#: settings migration is a LATER additive schema change, so it is appended last
#: (the documented rule: a later change never edits the snapshot).
DOCUMENTED_ORDER = (
    RECONCILE, VAULT_PART1, VAULT_PART2, DISPLAY_NAME, SEARCH_INDEXES, TTS_SETTINGS,
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
    section = _section_31()
    table = section[: section.index("### 31.2")]
    positions = [table.index(f"`{name}`") for name in DOCUMENTED_ORDER]
    assert positions == sorted(positions), (
        "§31.1 must list the required migrations in the documented execution order"
    )


def test_every_migration_named_in_the_section_exists():
    for name in DOCUMENTED_ORDER:
        assert (MIGRATIONS_DIR / name).is_file(), f"{name} is documented but absent"


def test_the_documented_deviations_are_stated_explicitly():
    flat = _flat(_section_31())
    assert "display_name" in flat
    assert "42703" in flat, "the failure the ordering prevents must be named"
    assert "six migrations" in flat, "the added sixth migration must be acknowledged"
    assert "tts_provider" in flat, "the TTS settings columns must be acknowledged"


def test_the_section_does_not_pretend_the_sql_was_executed():
    flat = _flat(_section_31())
    assert "NOTHING has been executed against Supabase" in flat
    assert "no SQL was run" in flat
    assert "pending an owner action" in flat
