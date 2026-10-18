"""Anti-Edit / Anti-Delete database security — the restrictive service-role-only posture.

The two AED tables (`aed_config`, `aed_archive_entries`) hold the body of the
owner's PRIVATE conversations. The first revision of their migration defined them
with the generic read-only dashboard boundary — `GRANT SELECT … TO anon,
authenticated` plus a `USING (true)` SELECT policy on each table — which would
have let any holder of the anon key read private message bodies.

This module pins the corrected definition in the MIGRATION (the SQL source of
truth) and in part 11 of the canonical setup block in DATABASE_ARCHITECTURE.md
§31.3, so the two artifacts cannot drift apart and the permissive posture cannot
come back silently. Everything here reads the repository's own SQL and prose; no
database is contacted.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOC_PATH = REPO / "DATABASE_ARCHITECTURE.md"
MIGRATION_NAME = "20261017000001_create_anti_edit_delete_tables.sql"
MIGRATION = (REPO / "supabase" / "migrations" / MIGRATION_NAME).read_text(encoding="utf-8")
DOC = DOC_PATH.read_text(encoding="utf-8")

TABLES = ("aed_config", "aed_archive_entries")

#: The permissive policies the first revision created and that must be removed
#: by name, idempotently.
LEGACY_SELECT_POLICIES = ("anon_select_aed_config", "anon_select_aed_archive_entries")

#: The four verbs `backend/db/client.py` actually uses for AED: the config/entry
#: reads (SELECT), the upserts (INSERT + UPDATE) and the bounded retention delete
#: (DELETE). Nothing more may be granted.
BACKEND_VERBS = ("SELECT", "INSERT", "UPDATE", "DELETE")

SETUP_BANNER = "ONE COMPLETE SUPABASE SETUP SCRIPT"
PART_MARKER = re.compile(r"--[^\n]*\bPART (\d+) of (\d+)\b[^\n]*\n")
AED_PART = 11


# ─── helpers ─────────────────────────────────────────────────────────────────

def _sql_blocks(text: str) -> list[str]:
    return re.findall(r"```sql\n(.*?)```", text, flags=re.S)


def _setup_block() -> str:
    hits = [b for b in _sql_blocks(DOC) if SETUP_BANNER in b]
    assert len(hits) == 1, "the ONE complete setup block must exist exactly once"
    return hits[0]


def _aed_part() -> str:
    block = _setup_block()
    marks = list(PART_MARKER.finditer(block))
    index = AED_PART - 1
    assert marks, "the setup block must carry part banners"
    assert marks[index].group(1) == str(AED_PART), (
        f"part {AED_PART} must be the Anti-Edit / Anti-Delete migration"
    )
    end = marks[index + 1].start() if index + 1 < len(marks) else len(block)
    return block[marks[index].end():end]


def _statements(text: str) -> list[str]:
    """Statement lines with the migrations' prose comments removed."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("--")
    ]


def _section(marker: str) -> str:
    start = DOC.index(marker)
    nxt = DOC.find("\n## ", start + len(marker))
    return DOC[start:] if nxt == -1 else DOC[start:nxt]


def _section_34() -> str:
    return _section("## 34. Anti-Edit / Anti-Delete Persistence")


# ─── 1. the migration: RLS with zero policies ────────────────────────────────

def test_the_migration_enables_rls_and_creates_no_policy():
    for table in TABLES:
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;" in MIGRATION, (
            f"{table} must have RLS enabled"
        )
    assert "CREATE POLICY" not in MIGRATION, (
        "an AED table must have ZERO policies: no role — anon, authenticated or "
        "owner-facing — may match one"
    )
    assert "FORCE ROW LEVEL SECURITY" not in MIGRATION, (
        "FORCE RLS is deliberately not used: service_role bypasses RLS in Supabase "
        "and the table owner keeps its normal rights"
    )
    assert "DISABLE ROW LEVEL SECURITY" not in MIGRATION


def test_both_tables_revoke_every_client_privilege():
    for table in TABLES:
        assert f"REVOKE ALL ON TABLE {table} FROM PUBLIC, anon, authenticated;" in MIGRATION, (
            f"every client role must lose every privilege on {table}"
        )


def test_the_column_level_revoke_is_present_and_bounded():
    """A table REVOKE cannot clear a per-column grant, so the migration must too."""
    assert "REVOKE ALL (%s) ON TABLE public.%I FROM PUBLIC, anon, authenticated" in MIGRATION, (
        "the column-level revoke must revoke PUBLIC/anon/authenticated per column"
    )
    for table in TABLES:
        assert f"'{table}'" in MIGRATION, f"the column revoke must cover {table}"
    assert "information_schema.columns" in MIGRATION, (
        "the column revoke must enumerate the table's real columns"
    )
    assert "DO $$" in MIGRATION and MIGRATION.count("END $$;") == 1, (
        "the column revoke must be one bounded DO block"
    )


def test_only_the_four_backend_verbs_are_granted_and_only_to_service_role():
    grants = re.findall(
        r"GRANT ([A-Z ,]+) ON TABLE (\w+) TO ([a-z_,\s]+);", MIGRATION
    )
    assert len(grants) == 2, f"the migration must carry exactly two GRANTs, found {grants}"
    granted = {table: tuple(v.strip() for v in verbs.split(",")) for verbs, table, role in grants}
    for verbs, table, role in grants:
        assert tuple(v.strip() for v in verbs.split(",")) == BACKEND_VERBS, (
            f"{table} must grant exactly {BACKEND_VERBS}, got {verbs!r}"
        )
        assert role.strip() == "service_role", (
            f"{table} may be granted to service_role only, got {role!r}"
        )
    assert set(granted) == set(TABLES)


def test_no_client_role_is_granted_anything():
    for match in re.finditer(r"\bGRANT\b[^;]*;", MIGRATION):
        statement = match.group(0)
        assert " anon" not in statement and "authenticated" not in statement, (
            f"a client role must never be granted anything: {statement!r}"
        )
        assert "PUBLIC" not in statement, (
            f"PUBLIC must never be granted anything: {statement!r}"
        )


def test_the_legacy_permissive_policies_are_dropped_by_name():
    for policy in LEGACY_SELECT_POLICIES:
        table = policy[len("anon_select_"):]
        assert f'DROP POLICY IF EXISTS "{policy}" ON {table};' in MIGRATION, (
            f"the legacy permissive policy {policy} must be removed by name"
        )
    drops = re.findall(r'DROP POLICY IF EXISTS "([^"]+)" ON (\w+);', MIGRATION)
    assert drops, "the migration must drop the legacy policies"
    for _name, table in drops:
        assert table in TABLES, f"a DROP POLICY targets an unrelated table: {table}"


# ─── 2. the migration: the schema, constraints and index are unchanged ───────

def test_the_schema_constraints_index_and_verification_are_intact():
    assert "CREATE TABLE IF NOT EXISTS aed_config (" in MIGRATION
    assert "CREATE TABLE IF NOT EXISTS aed_archive_entries (" in MIGRATION
    for guarantee in (
        "CHECK (retention_days BETWEEN 1 AND 3650)",
        "kind IN ('snapshot', 'edit', 'delete')",
        "CHECK (chat_id > 0)",
        "CHECK (message_id > 0)",
        "CHECK (length(body) <= 4096)",
        "idx_aed_archive_entries_owner_captured",
        "FROM information_schema.columns",
        "FROM bot_settings",
        "NOTIFY pgrst, 'reload schema'",
    ):
        assert guarantee in MIGRATION, f"the security fix removed {guarantee!r}"


# ─── 3. the canonical documentation agrees with the migration ────────────────

def test_the_canonical_part_is_statement_identical_to_the_migration():
    assert _statements(_aed_part()) == _statements(MIGRATION), (
        "part 11 of §31.3 diverged from its migration; the block must copy it verbatim"
    )


def test_the_canonical_part_carries_no_permissive_statement():
    part = _aed_part()
    assert "CREATE POLICY" not in part, "the canonical block must create no AED policy"
    for table in TABLES:
        assert f"REVOKE ALL ON TABLE {table} FROM PUBLIC, anon, authenticated;" in part
        assert (
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {table} TO service_role;" in part
        )
    assert "FORCE ROW LEVEL SECURITY" not in part
    for match in re.finditer(r"\bGRANT\b[^;]*;", part):
        assert " anon" not in match.group(0) and "authenticated" not in match.group(0)


def test_the_documentation_describes_the_restrictive_model():
    section = _section_34()
    for guarantee in (
        "ZERO policies",
        "REVOKE ALL ON TABLE",
        "service_role",
        "column-level",
        "34.7",
    ):
        assert guarantee in section, f"§34 must document {guarantee!r}"
    assert "`anon`/`authenticated` granted SELECT only (the same" not in DOC, (
        "no section may still claim anon/authenticated can read the AED tables"
    )
    rls_section = _section("## 18. RLS Policy Model")
    assert "Documented exceptions" in rls_section, (
        "§18 must name the server-only tables that are exempt from the read-only "
        "dashboard boundary"
    )
    for table in TABLES:
        assert table in rls_section, f"§18 must name {table} as an exception"


def test_the_inventory_rows_state_the_restrictive_posture():
    row = [line for line in DOC.splitlines() if MIGRATION_NAME in line and line.startswith("| 11 |")]
    assert len(row) == 1, "§31.1 must carry exactly one inventory row for the AED migration"
    assert "service-role-only" in row[0], "§31.1 must describe the service-role-only posture"
    assert "SELECT-only" not in row[0], "§31.1 must not claim a client read path"
