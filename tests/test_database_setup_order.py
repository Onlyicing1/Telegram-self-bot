"""M3.x — the canonical database setup order and the combined setup block.

DATABASE_ARCHITECTURE.md §31 answers one operational question: in what order do
the pending migrations have to be applied, and what exactly does the owner paste?
Its §31.3 block is transcribed from the migration files rather than written from
memory, so two properties have to hold and neither is covered by any other test:

1. **Statement fidelity and order.** Every step of the §31.3 block is the
   executable text of the migration named in its banner, statement for
   statement, and the steps appear in the documented order (Vault PART 1 →
   Vault PART 2 → `saved_items.display_name` → the Save V2 search indexes).
2. **Dependency validity.** `20260922000001_add_saved_items_search_indexes.sql`
   indexes `saved_items.display_name`, which is created by
   `20260921000001_add_saved_items_display_name.sql` and **not** by the
   reconciliation snapshot (§30.4). Applying the index before the column fails
   with `42703`, so the display-name step must precede the index step. This is
   the one place where the documented order deviates from the order that was
   originally requested, and the reason is pinned here so it cannot be
   "simplified" away later.

The reconciliation snapshot (`20260920000001_…`) is deliberately **not** inside
the combined block: it is byte-frozen, it exists in exactly three byte-identical
copies and §31 declares it as step 0 (§30.5). These tests assert that the block
never duplicates it, so §30's exactly-one-embed property stays intact.

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

#: The order §31 documents — verified against the sources, not assumed.
DOCUMENTED_ORDER = (RECONCILE, VAULT_PART1, VAULT_PART2, DISPLAY_NAME, SEARCH_INDEXES)

#: Steps A–D of the combined block, in the order the block must contain them.
BLOCK_STEPS = {VAULT_PART1: "A", VAULT_PART2: "B", DISPLAY_NAME: "C", SEARCH_INDEXES: "D"}

#: ASCII substring of the block's banner comment, deliberately not its dashes.
BLOCK_MARKER = "canonical database setup, steps A"

DOC = DOC_PATH.read_text(encoding="utf-8")


# ─── parsing helpers (same conventions as the other documentation tests) ──────


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


def _flat(section: str) -> str:
    """Prose assertions must not depend on blockquote markers or line wrapping."""
    unquoted = re.sub(r"(?m)^\s*>\s?", "", section)
    return re.sub(r"\s+", " ", unquoted)


def _setup_block() -> str:
    hits = [b for b in _sql_blocks(DOC) if BLOCK_MARKER in b]
    assert len(hits) == 1, (
        "DATABASE_ARCHITECTURE.md must embed the combined setup block exactly once"
    )
    return hits[0]


def _step_spans(block: str) -> dict[str, str]:
    """The text of each step, keyed by the migration its banner names."""
    marks = list(re.finditer(r"--[^\n]*\bSTEP ([A-D])\b[^\n]*\n", block))
    assert [m.group(1) for m in marks] == ["A", "B", "C", "D"], (
        "the combined block must carry steps A, B, C and D in that order"
    )
    by_letter = {letter: name for name, letter in BLOCK_STEPS.items()}
    spans: dict[str, str] = {}
    for i, mark in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(block)
        spans[by_letter[mark.group(1)]] = block[mark.end():end]
    return spans


# ─── 1. the block itself ──────────────────────────────────────────────────────


def test_the_documentation_carries_exactly_one_combined_setup_block():
    block = _setup_block()
    assert block.strip(), "the combined setup block must not be empty"
    assert "NOTIFY pgrst, 'reload schema'" in block
    for name in BLOCK_STEPS:
        assert name in block, f"{name} must be named in the block"


def test_every_migration_named_in_the_block_exists():
    for name in DOCUMENTED_ORDER:
        assert (MIGRATIONS_DIR / name).is_file(), f"{name} is documented but absent"


@pytest.mark.parametrize("name", list(BLOCK_STEPS))
def test_every_step_is_statement_identical_to_its_migration(name: str):
    """A step is a transcription of its migration, not a paraphrase of it."""
    step = _statements(_step_spans(_setup_block())[name])
    source = _statements(_migration(name))
    assert step, f"step {BLOCK_STEPS[name]} of the combined block is empty"
    assert step == source, (
        f"step {BLOCK_STEPS[name]} ({name}) diverged from the migration; "
        "the combined block must copy the migration's statements verbatim"
    )


def test_the_block_never_duplicates_the_byte_frozen_reconciliation_snapshot():
    """§30 owns the one byte-identical embed; the block must reference it, not copy it."""
    block = _setup_block()
    assert "Canonical Supabase Bootstrap & Reconciliation" not in block
    assert "CREATE TABLE IF NOT EXISTS saved_items" not in block
    assert "CREATE POLICY" not in block


def test_the_block_is_additive_only():
    """The combined block may create/alter, but never destroy or rewrite data.

    The two DELETEs it does carry are the Vault cleanup paths of the credential
    functions: both are keyed to one row/secret, so they are asserted to stay
    bounded rather than forbidden.
    """
    statements = _statements(_setup_block())
    joined = "\n".join(statements)
    for forbidden in ("DROP TABLE", "DROP COLUMN", "DROP SCHEMA", "TRUNCATE"):
        assert forbidden not in joined, f"the combined setup block must not contain {forbidden}"
    for target in ("DELETE FROM vault.secrets", "DELETE FROM public.api_credentials"):
        assert target in joined, f"the credential cleanup path must stay intact: {target}"
    # A DELETE may wrap across lines, so the bound is proved per statement.
    deletes = re.findall(r"(?is)DELETE FROM.*?;", joined)
    assert deletes, "the credential cleanup paths must still be present"
    for statement in deletes:
        assert re.search(r"(?i)\bWHERE\b", statement), (
            f"every DELETE must be filtered to specific rows: {statement!r}"
        )
    for table in ("saved_items", "ai_config", "panel_settings", "bot_settings"):
        assert f"DELETE FROM public.{table}" not in joined
        assert f"UPDATE {table}" not in joined
        assert f"UPDATE public.{table}" not in joined


def test_the_block_creates_no_secret_value():
    """It may create the Vault-backed *plumbing*; it must never carry a key."""
    block = _setup_block()
    assert "vault.create_secret(" in block, "the create function must stay intact"
    for pattern in (r"sk-[A-Za-z0-9]", r"AIza[0-9A-Za-z_-]{10}", r"[A-Za-z0-9]{32,}-key-"):
        assert not re.search(pattern, block), f"a raw secret leaked into the block: {pattern}"


# ─── 2. the documented order ──────────────────────────────────────────────────


def test_the_audited_order_is_recorded_in_the_audit_table():
    section = _section_31()
    table = section[: section.index("### 31.2")]
    positions = [table.index(f"`{name}`") for name in DOCUMENTED_ORDER]
    assert positions == sorted(positions), (
        "§31.1 must list the required migrations in the documented execution order"
    )


def test_the_documented_order_matches_the_block_order():
    block = _setup_block()
    steps = [name for name in DOCUMENTED_ORDER if name in BLOCK_STEPS]
    positions = [block.index(name) for name in steps]
    assert positions == sorted(positions), "steps A–D must follow the documented order"


def test_the_snapshot_is_documented_as_step_zero_not_as_a_block_step():
    section = _section_31()
    assert RECONCILE in section, "the reconciliation migration must stay documented"
    assert "§30.5" in section, "and it must point at the frozen block that carries it"
    assert RECONCILE not in _setup_block(), (
        "the frozen snapshot is step 0 and is never copied into the block"
    )


def test_the_documented_deviations_are_stated_explicitly():
    section = _flat(_section_31())
    assert "display_name" in section
    assert "depends on" in section or "dependency" in section
    assert "42703" in section, "the failure the ordering prevents must be named"
    assert "five migrations" in section, "the added fifth migration must be acknowledged"


# ─── 3. the dependency the order exists for ───────────────────────────────────


def test_the_index_migration_indexes_a_column_the_snapshot_does_not_create():
    indexes = _migration(SEARCH_INDEXES)
    assert re.search(
        r"CREATE INDEX IF NOT EXISTS idx_saved_items_display_name_trgm\s+"
        r"ON saved_items USING gin \(display_name gin_trgm_ops\);",
        indexes,
    ), "the search-index migration must still index saved_items.display_name"

    for name in (RECONCILE, DISPLAY_NAME):
        creates = re.search(r"ADD COLUMN IF NOT EXISTS display_name text", _migration(name))
        if name == DISPLAY_NAME:
            assert creates, "the display-name migration must still create the column"
        else:
            assert not creates, (
                "if the snapshot ever creates display_name the ordering rationale "
                "in §31.2 must be rewritten — do not leave both true"
            )


def test_the_display_name_step_precedes_the_index_step():
    block = _setup_block()
    assert block.index(DISPLAY_NAME) < block.index(SEARCH_INDEXES), (
        "the index step needs display_name to exist already"
    )
    spans = _step_spans(block)
    assert _statements(spans[DISPLAY_NAME]) == _statements(_migration(DISPLAY_NAME))
    assert _statements(spans[SEARCH_INDEXES]) == _statements(_migration(SEARCH_INDEXES))


def test_the_vault_management_step_needs_the_vault_part_one_step():
    """PART 2 filters on public.api_credentials, which PART 1 creates."""
    assert "public.api_credentials" in _migration(VAULT_PART2)
    assert "CREATE TABLE IF NOT EXISTS public.api_credentials" in _migration(VAULT_PART1)
    block = _setup_block()
    assert block.index(VAULT_PART1) < block.index(VAULT_PART2)


# ─── 4. honest reporting ──────────────────────────────────────────────────────


def test_the_section_does_not_pretend_the_sql_was_executed():
    section = _flat(_section_31())
    assert "NOTHING has been executed against Supabase" in section
    assert "no SQL was run" in section
    assert "pending an owner action" in section


def test_every_step_banner_names_a_repository_migration():
    block = _setup_block()
    for name, letter in BLOCK_STEPS.items():
        assert re.search(rf"--[^\n]*STEP {letter} — {re.escape(name)}", block), (
            f"step {letter} must name {name} in its banner"
        )
