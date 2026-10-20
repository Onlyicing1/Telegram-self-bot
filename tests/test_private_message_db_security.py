"""Private Message Management database security — the service-role-only posture.

The two Private Message Management tables (`private_message_config`,
`private_message_senders`) were created with the generic read-only dashboard
boundary: `GRANT SELECT … TO anon, authenticated` plus a `USING (true)` SELECT
policy on each. They hold the owner's configuration (mode, allow-list,
exception list, temporary override, weekly schedule, fixed response text) and
every per-person AI-reply counter, so that boundary exposed private material to
any holder of the project's anon key.

`20261020144423_harden_private_message_table_access.sql` is the additive
security migration that closes it: RLS stays enabled, every privilege is
revoked from `PUBLIC`/`anon`/`authenticated` (table-level AND column-level), the
permissive policies are dropped by name, and exactly SELECT/INSERT/UPDATE/DELETE
is granted to `service_role` with ZERO policies. `backend/db/client.py` builds
its one client from `SUPABASE_SERVICE_ROLE_KEY`, so the backend is unaffected.

This module pins that posture in the MIGRATION (the SQL source of truth), in
part 13 of the canonical setup block in DATABASE_ARCHITECTURE.md §31.3, and in
§33.4 — so the artifacts cannot drift and the permissive posture cannot come
back silently. Everything here reads the repository's own SQL and prose; no
database is contacted and no SQL is executed.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOC_PATH = REPO / "DATABASE_ARCHITECTURE.md"
MIGRATIONS_DIR = REPO / "supabase" / "migrations"
MIGRATION_NAME = "20261020144423_harden_private_message_table_access.sql"
CREATION_NAME = "20260929000001_create_private_message_tables.sql"
MIGRATION = (MIGRATIONS_DIR / MIGRATION_NAME).read_text(encoding="utf-8")
CREATION = (MIGRATIONS_DIR / CREATION_NAME).read_text(encoding="utf-8")
DOC = DOC_PATH.read_text(encoding="utf-8")

TABLES = ("private_message_config", "private_message_senders")

#: The eight policy names the hardening migration removes: the two permissive
#: SELECT policies the creation migration actually created, plus the three write
#: policies it already dropped defensively on each table.
PERMISSIVE_POLICIES = (
    "anon_select_private_message_config",
    "anon_insert_private_message_config",
    "anon_update_private_message_config",
    "anon_delete_private_message_config",
    "anon_select_private_message_senders",
    "anon_insert_private_message_senders",
    "anon_update_private_message_senders",
    "anon_delete_private_message_senders",
)

#: The four verbs `backend/db/client.py` uses for these two tables: the reads
#: (SELECT), the config save and the claim's counter insert (INSERT), the config
#: save and both CAS updates (UPDATE), and the owner's counter reset (DELETE).
BACKEND_VERBS = ("SELECT", "INSERT", "UPDATE", "DELETE")

SETUP_BANNER = "ONE COMPLETE SUPABASE SETUP SCRIPT"
PART_MARKER = re.compile(r"--[^\n]*\bPART (\d+) of (\d+)\b[^\n]*\n")
PM_ACCESS_PART = 13
TOTAL_PARTS = 13
TOTAL_MIGRATIONS = 35


# ─── helpers ─────────────────────────────────────────────────────────────────

def _bare(text: str) -> str:
    """The executable SQL of a file: prose comments removed.

    The migration's header documents its own reversal, which contains GRANT and
    CREATE POLICY text; only the executed statements may be asserted on.
    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(
        line for line in text.splitlines() if not line.strip().startswith("--")
    )


def _sql_blocks(text: str) -> list[str]:
    return re.findall(r"```sql\n(.*?)```", text, flags=re.S)


def _setup_block() -> str:
    hits = [b for b in _sql_blocks(DOC) if SETUP_BANNER in b]
    assert len(hits) == 1, "the ONE complete setup block must exist exactly once"
    return hits[0]


def _part(index: int) -> str:
    block = _setup_block()
    marks = list(PART_MARKER.finditer(block))
    assert marks, "the setup block must carry part banners"
    assert marks[index - 1].group(1) == str(index), f"part {index} is out of order"
    end = marks[index].start() if index < len(marks) else len(block)
    return block[marks[index - 1].end():end]


def _statements(text: str) -> list[str]:
    return [
        line.strip()
        for line in _bare(text).splitlines()
        if line.strip()
    ]


def _section(marker: str) -> str:
    start = DOC.index(marker)
    nxt = DOC.find("\n## ", start + len(marker))
    return DOC[start:] if nxt == -1 else DOC[start:nxt]


def _subsection(marker: str) -> str:
    """One numbered subsection, stopping at the next degree-2 or degree-3 heading."""
    start = DOC.index(marker)
    after = start + len(marker)
    stops = [x for x in (DOC.find("\n### ", after), DOC.find("\n## ", after)) if x != -1]
    return DOC[start: min(stops)] if stops else DOC[start:]


def _flat(section: str) -> str:
    unquoted = re.sub(r"(?m)^\s*>\s?", "", section)
    return re.sub(r"\s+", " ", unquoted)


# ─── 1. the migration: RLS stays on, and no policy is created ────────────────

def test_the_migration_keeps_rls_enabled_on_both_tables():
    body = _bare(MIGRATION)
    for table in TABLES:
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;" in body, (
            f"{table} must keep RLS enabled"
        )
    assert "DISABLE ROW LEVEL SECURITY" not in body
    assert "FORCE ROW LEVEL SECURITY" not in body, (
        "FORCE RLS is deliberately not used: service_role bypasses RLS and the "
        "table owner keeps its normal rights"
    )


def test_the_migration_creates_no_policy_at_all():
    body = _bare(MIGRATION)
    assert "CREATE POLICY" not in body, (
        "the hardened tables must have ZERO policies: with RLS enabled and no "
        "applicable policy, every public client role is denied"
    )
    assert body.count("DROP POLICY IF EXISTS") == len(PERMISSIVE_POLICIES), (
        "every permissive policy name must be dropped exactly once"
    )


def test_every_known_permissive_policy_is_dropped_by_name():
    body = _bare(MIGRATION)
    for policy in PERMISSIVE_POLICIES:
        table = policy.split("_", 2)[2]
        assert f'DROP POLICY IF EXISTS "{policy}" ON {table};' in body, (
            f"the permissive policy {policy} must be removed by name"
        )
    drops = re.findall(r'DROP POLICY IF EXISTS "([^"]+)" ON (\w+);', body)
    assert sorted(name for name, _ in drops) == sorted(PERMISSIVE_POLICIES)
    for _name, table in drops:
        assert table in TABLES, f"a DROP POLICY targets an unrelated table: {table}"


# ─── 2. the migration: no client privilege survives, service_role keeps CRUD ─

def test_every_client_role_loses_every_table_privilege():
    body = _bare(MIGRATION)
    for table in TABLES:
        assert f"REVOKE ALL ON TABLE {table} FROM PUBLIC, anon, authenticated;" in body, (
            f"PUBLIC/anon/authenticated must lose every privilege on {table}"
        )


def test_the_column_level_revoke_is_present_and_bounded():
    """A table REVOKE cannot clear a per-column grant, so the migration must too."""
    body = _bare(MIGRATION)
    assert "REVOKE ALL (%s) ON TABLE public.%I FROM PUBLIC, anon, authenticated" in body
    assert "information_schema.columns" in body
    assert "DO $$" in body and body.count("END $$;") == 1, (
        "the column revoke must be one bounded DO block"
    )
    array = re.search(r"FOREACH tbl IN ARRAY ARRAY\[(.*?)\] LOOP", body, flags=re.S)
    assert array, "the DO block must iterate the two tables explicitly"
    names = re.findall(r"'(\w+)'", array.group(1))
    assert tuple(names) == TABLES, (
        f"the column revoke must cover exactly {TABLES}, got {names}"
    )


def test_only_the_four_backend_verbs_are_granted_and_only_to_service_role():
    body = _bare(MIGRATION)
    grants = re.findall(r"GRANT ([A-Z ,]+) ON TABLE (\w+) TO ([a-z_,\s]+);", body)
    assert len(grants) == 2, f"the migration must carry exactly two GRANTs, found {grants}"
    granted = {}
    for verbs, table, role in grants:
        parsed = tuple(v.strip() for v in verbs.split(","))
        assert parsed == BACKEND_VERBS, f"{table} must grant exactly {BACKEND_VERBS}, got {verbs!r}"
        assert role.strip() == "service_role", (
            f"{table} may be granted to service_role only, got {role!r}"
        )
        granted[table] = parsed
    assert set(granted) == set(TABLES)


def test_no_client_role_is_granted_anything():
    for match in re.finditer(r"\bGRANT\b[^;]*;", _bare(MIGRATION)):
        statement = match.group(0)
        assert " anon" not in statement and "authenticated" not in statement, (
            f"a client role must never be granted anything: {statement!r}"
        )
        assert "PUBLIC" not in statement, (
            f"PUBLIC must never be granted anything: {statement!r}"
        )


# ─── 3. the migration: additive and idempotent, no unrelated object ----------

def test_the_migration_is_idempotent_and_touches_no_data_or_schema_object():
    body = _bare(MIGRATION)
    for forbidden in (
        "CREATE TABLE", "DROP TABLE", "ADD COLUMN", "DROP COLUMN", "ALTER COLUMN",
        "TRUNCATE", "DELETE FROM", "INSERT INTO", "DROP INDEX", "CREATE INDEX",
        "DROP CONSTRAINT", "ADD CONSTRAINT",
    ):
        assert forbidden not in body, f"the security migration must not contain {forbidden!r}"
    for pattern in (r"(?im)^\s*UPDATE\b", r"(?im)^\s*SET\b"):
        assert not re.search(pattern, body), "the migration must rewrite no row"
    for statement in _statements(MIGRATION):
        upper = statement.upper()
        if " DROP POLICY " in f" {upper} ":
            assert "DROP POLICY IF EXISTS" in upper, (
                f"a re-run must be a no-op: {statement!r}"
            )


def test_the_migration_touches_no_unrelated_table():
    body = _bare(MIGRATION)
    for table in TABLES:
        assert table in body
    for unrelated in ("aed_config", "aed_archive_entries", "saved_items", "bot_settings"):
        assert unrelated not in body, (
            f"the hardening must not name the unrelated table {unrelated}"
        )


def test_the_migration_reloads_the_postgrest_schema_cache():
    assert "NOTIFY pgrst, 'reload schema';" in _bare(MIGRATION)


def test_the_migration_ends_with_read_only_verification_queries():
    body = _bare(MIGRATION)
    for probe in (
        "information_schema.role_table_grants",
        "information_schema.column_privileges",
        "pg_policies",
        "c.relrowsecurity",
    ):
        assert probe in body, f"the verification queries must include {probe!r}"
    assert body.count("NOTIFY pgrst, 'reload schema';") == 1
    verification = body.split("NOTIFY pgrst, 'reload schema';", 1)[1]
    assert verification.strip(), "the migration must end with verification queries"
    assert verification.lstrip().upper().startswith("SELECT")
    for verb in (
        "INSERT", "UPDATE", "DELETE", "ALTER", "DROP", "GRANT", "REVOKE",
        "CREATE", "TRUNCATE", "COMMENT",
    ):
        assert not re.search(rf"\b{verb}\b", verification, flags=re.I), (
            f"the verification queries must be read-only, found {verb}"
        )


# ─── 4. the repository behaviour this posture must not break ----------------

def test_the_backend_uses_exactly_the_four_granted_verbs():
    """The grant set is derived from the repository's own queries, not guessed."""
    import inspect

    from backend.db import client as db_client

    for name in (
        "get_private_message_config",
        "save_private_message_config",
        "list_private_message_senders",
        "claim_private_message_ai_reply",
        "release_private_message_ai_reply",
        "reset_private_message_counters",
    ):
        assert hasattr(db_client, name), f"the repository function {name} is missing"
    source = "".join(
        inspect.getsource(getattr(db_client, name))
        for name in (
            "_get_private_message_config_sync",
            "_save_private_message_config_sync",
            "_list_private_message_senders_sync",
            "_claim_private_message_ai_reply_sync",
            "_release_private_message_ai_reply_sync",
            "_reset_private_message_counters_sync",
        )
    )
    verbs = {
        "SELECT": ".select(",
        "INSERT": ".insert(",
        "UPDATE": ".update(",
        "DELETE": ".delete(",
    }
    for verb, call in verbs.items():
        assert call in source, f"the repository uses {verb}, so it must be granted"
    assert tuple(verbs) == BACKEND_VERBS


def test_the_one_shared_client_is_still_built_from_the_service_role_key():
    """The posture assumes the service role; nothing may quietly replace it."""
    import inspect

    from backend.db import client as db_client

    source = inspect.getsource(db_client.get_db)
    assert "SUPABASE_SERVICE_ROLE_KEY" in source, (
        "the one Supabase client must keep using the service-role key"
    )
    assert "SUPABASE_ANON_KEY" not in inspect.getsource(db_client), (
        "the backend must never reach these tables through the anon key"
    )


# ─── 5. the table-creation migration is untouched, the fix is additive -------

def test_the_historical_creation_migration_still_carries_its_permissive_policies():
    """The fix is a NEW file: the old migration must not be rewritten to hide it."""
    body = _bare(CREATION)
    assert 'CREATE POLICY "anon_select_private_message_config"' in body
    assert 'CREATE POLICY "anon_select_private_message_senders"' in body
    assert "GRANT SELECT ON private_message_config TO anon, authenticated;" in body
    assert "GRANT SELECT ON private_message_senders TO anon, authenticated;" in body
    assert MIGRATION_NAME not in CREATION


# ─── 6. the canonical block embeds the migration verbatim --------------------

def test_the_private_message_access_part_is_the_thirteenth_and_last():
    block = _setup_block()
    marks = list(PART_MARKER.finditer(block))
    assert [m.group(1) for m in marks] == [str(i) for i in range(1, TOTAL_PARTS + 1)]
    assert {m.group(2) for m in marks} == {str(TOTAL_PARTS)}
    assert marks[-1].group(2) == str(TOTAL_PARTS)
    assert MIGRATION_NAME in block[marks[-1].start(): marks[-1].end()], (
        "the last part banner must name the hardening migration"
    )
    assert PM_ACCESS_PART == TOTAL_PARTS == len(_recorded_order())


def _recorded_order() -> tuple[str, ...]:
    """The migration order the setup test records (single source of truth)."""
    from tests.test_database_setup_order import DOCUMENTED_ORDER

    return tuple(DOCUMENTED_ORDER)


def test_the_canonical_part_is_statement_identical_to_the_migration():
    assert _statements(_part(PM_ACCESS_PART)) == _statements(MIGRATION), (
        "part 13 of §31.3 diverged from its migration; the block must copy it verbatim"
    )


def test_the_canonical_part_carries_no_permissive_statement():
    part = _bare(_part(PM_ACCESS_PART))
    assert "CREATE POLICY" not in part
    for table in TABLES:
        assert f"REVOKE ALL ON TABLE {table} FROM PUBLIC, anon, authenticated;" in part
        assert (
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {table} TO service_role;" in part
        )
    for match in re.finditer(r"\bGRANT\b[^;]*;", part):
        assert " anon" not in match.group(0) and "authenticated" not in match.group(0)


def test_the_setup_block_keeps_creating_the_policies_it_then_removes():
    """Part 10 stays verbatim, so the hardening is visibly the later step."""
    creation_part = _bare(_part(10))
    assert 'CREATE POLICY "anon_select_private_message_config"' in creation_part
    assert 'CREATE POLICY "anon_select_private_message_senders"' in creation_part
    assert "GRANT SELECT ON private_message_config TO anon, authenticated;" in creation_part


def test_the_documented_counts_match_the_repository():
    assert len(list(MIGRATIONS_DIR.glob("*.sql"))) == TOTAL_MIGRATIONS
    flat = _flat(_section("## 31. Canonical Database Setup"))
    assert f"all {TOTAL_MIGRATIONS} migrations" in flat
    assert f"{TOTAL_MIGRATIONS} files" in flat
    assert "twelve later additive migrations" in flat
    assert len(_recorded_order()) - 1 == 12, (
        "the snapshot plus twelve later parts is what the prose must count"
    )
    assert f"parts 1–{TOTAL_PARTS} of §31.3" in flat


# ─── 7. the documentation states the intended AND the unverified posture -----

def test_the_feature_section_describes_the_restrictive_model():
    section = _section("## 33. Private Message Management Persistence")
    flat = _flat(section)
    for guarantee in (
        "service-role only",
        "ZERO",
        "REVOKE ALL",
        "service_role",
        "column-level",
        MIGRATION_NAME,
        f"part {PM_ACCESS_PART} of {TOTAL_PARTS}",
    ):
        assert guarantee in flat, f"§33 must document {guarantee!r}"
    assert "`anon`/`authenticated` hold **SELECT only**" not in flat, (
        "no section may still claim anon/authenticated can read the Private Message tables"
    )


def test_the_documentation_separates_repository_posture_from_live_posture():
    flat = _flat(_subsection("### 33.4 Security and access model"))
    assert "Intended repository posture" in flat
    assert "Live Supabase posture is UNVERIFIED" in flat
    assert "owner action" in flat
    assert "tests/test_private_message_db_security.py" in flat


def test_each_manual_application_path_names_the_hardening_part():
    flat = _flat(_subsection("### 33.5 Manual application and reversal"))
    assert MIGRATION_NAME in flat
    assert "security regression" in flat, (
        "the reversal must be described as a regression, not a repair"
    )
    assert "does **not** claim the live Supabase project is hardened" in _flat(
        _subsection("### 33.6 What this section does not claim")
    )


def test_the_rls_model_names_the_two_tables_as_server_only_exceptions():
    section = _section("## 18. RLS Policy Model")
    assert "Documented exceptions" in section
    for table in TABLES:
        assert table in section, f"§18 must name {table} as an exception"


# ─── 8. the existing AED posture is untouched ───────────────────────────────

def test_the_hardening_does_not_touch_the_aed_tables():
    body = _bare(MIGRATION)
    for table in ("aed_config", "aed_archive_entries"):
        assert f'ON {table} ' not in body
        assert f"ON TABLE {table}" not in body
    from tests import test_aed_db_security as aed

    assert set(aed.TABLES) == {"aed_config", "aed_archive_entries"}
    assert aed._statements(aed._aed_part()) == aed._statements(aed.MIGRATION), (
        "the AED part must stay verbatim — the security phase changed no AED SQL"
    )
