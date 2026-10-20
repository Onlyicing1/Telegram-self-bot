# IMPLEMENTATION_REPORT.md — Security phase: Private Message Management table access

**Phase:** database-security hardening of `public.private_message_config` and
`public.private_message_senders`.
**Status:** repository change COMPLETE and verified offline. **The SQL has NOT
been applied to Supabase and live access has NOT been verified.** That is an
owner action (§9).

---

## 1. Objective and scope

Remove unnecessary public Data API access to the two Private Message Management
tables while preserving the backend's existing `service_role` access and its
runtime behaviour.

In scope, and all that was done:

- one NEW additive security migration;
- its verbatim embed as the new last part of the one canonical setup block
  (`DATABASE_ARCHITECTURE.md` §31.3) plus the counts, audit rows, verification
  and reversal text that follow from it;
- the §33 security/access documentation and the §18 RLS exceptions list;
- tests that pin the new posture, the canonical embed and the counts;
- this report.

Explicitly NOT done: no SQL was executed anywhere (no `execute_sql`, no
`apply_migration`, no connection to any Supabase project); no table, column,
index, row or stored configuration was changed; `backend/db/client.py` and every
other runtime file are untouched; no environment secret was read, printed or
changed; no unrelated documentation was edited.

---

## 2. Repository state before editing

- Repository `Onlyicing1/Telegram-self-bot`, branch `main` (the working branch
  was fast-forwarded from `33d55d1` to the stated baseline and is byte-identical
  to it), baseline commit `62ec8d1db912eae9a0feb6b793bafa91cfb2b844`.
- 34 migration files existed; `DATABASE_ARCHITECTURE.md` §31.3 carried 12
  embedded parts; the two Private Message tables were created by part 10 and
  extended by part 11.
- Pre-existing unrelated workspace state was left untouched and unstaged:
  a modification to `backend/services/premium_emoji_inline_service.py` and the
  untracked `backend/test_patch_story.md`, `scratchpad/`, `telegram-self-bot/`.

---

## 3. The exposure, confirmed from source (not assumed)

`supabase/migrations/20260929000001_create_private_message_tables.sql`
executes, for BOTH tables:

```sql
GRANT SELECT ON private_message_config TO anon, authenticated;
GRANT SELECT ON private_message_senders TO anon, authenticated;
CREATE POLICY "anon_select_private_message_config" ON private_message_config FOR SELECT
    TO anon, authenticated USING (true);
CREATE POLICY "anon_select_private_message_senders" ON private_message_senders FOR SELECT
    TO anon, authenticated USING (true);
```

A holder of the project's anon key could therefore read the owner's entire
configuration row (master switch, `mode`, `allowlist`, `exceptions`,
`temporary`, `schedule`, `fixed_response_text`, the allowance and interval
settings) and every per-person counter row (`owner_id`, `sender_id`,
`ai_replies_used`, `last_ai_reply_at`, `window_started_at`). The permissive
policies are the real exposure; the grants alone would still be limited by RLS,
but neither belongs on a server-only table.

The backend already read and wrote both tables exclusively through the ONE
client built from `SUPABASE_SERVICE_ROLE_KEY`
(`backend/db/client.py::get_db`), which bypasses RLS.

---

## 4. The security change (exact SQL)

New file, generated with `npx supabase migration new
harden_private_message_table_access` (the repository's normal migration
workflow; it produced the timestamped name):

`supabase/migrations/20261020144423_harden_private_message_table_access.sql`

Executable statements, in order:

1. `ALTER TABLE private_message_config ENABLE ROW LEVEL SECURITY;`
   `ALTER TABLE private_message_senders ENABLE ROW LEVEL SECURITY;`
   (re-asserted, so a database where RLS was turned off converges too)
2. Eight idempotent policy removals —
   `DROP POLICY IF EXISTS "anon_select_private_message_config" ON private_message_config;`
   plus `anon_insert_…` / `anon_update_…` / `anon_delete_…` on the same table,
   and the same four names on `private_message_senders`.
3. `REVOKE ALL ON TABLE private_message_config FROM PUBLIC, anon, authenticated;`
   `REVOKE ALL ON TABLE private_message_senders FROM PUBLIC, anon, authenticated;`
4. One bounded `DO $$ … END $$;` block that enumerates the real columns of each
   table from `information_schema.columns` and executes
   `REVOKE ALL (…) ON TABLE public.%I FROM PUBLIC, anon, authenticated`
   (column privileges are granted independently of table privileges, so a table
   `REVOKE` alone cannot clear a legacy per-column grant).
5. `GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE private_message_config TO service_role;`
   and the same for `private_message_senders`. Nothing else is granted.
6. No `CREATE POLICY` anywhere: with RLS enabled and no applicable policy, every
   public client role is denied at the table, and `service_role` needs no policy
   because it bypasses RLS.
7. Two `COMMENT ON TABLE` statements that restate the posture in the database
   itself.
8. `NOTIFY pgrst, 'reload schema';`
9. Five read-only verification queries (client table grants, client column
   privileges, `pg_policies` count, `pg_class.relrowsecurity`, service-role
   grant count).

Additive and idempotent: no table, column, index, constraint, row or stored
value is created, dropped or rewritten; no unrelated policy or grant is named;
every removal and both CREATE-less grants re-run as no-ops.

### The four granted verbs are derived from the repository, not guessed

`backend/db/client.py` uses, on these two tables:

| Verb | Repository use |
|---|---|
| SELECT | `_get_private_message_config_sync`, `_list_private_message_senders_sync` |
| INSERT | `_save_private_message_config_sync` (row absent), the claim's counter insert |
| UPDATE | `_save_private_message_config_sync` (row present), `_claim_private_message_ai_reply_sync` CAS, `_release_private_message_ai_reply_sync` compensating CAS |
| DELETE | `_reset_private_message_counters_sync` (owner's explicit reset) |

`tests/test_private_message_db_security.py` reads those function sources and
asserts that each verb is actually used, so the grant set cannot drift from the
code.

---

## 5. Access model after the SQL is applied

| Role | `private_message_config` | `private_message_senders` |
|---|---|---|
| `PUBLIC` | no privilege (table + column) | no privilege (table + column) |
| `anon` | no privilege; no policy applies | no privilege; no policy applies |
| `authenticated` | no privilege; no policy applies | no privilege; no policy applies |
| `service_role` | SELECT, INSERT, UPDATE, DELETE | SELECT, INSERT, UPDATE, DELETE |

RLS stays ENABLED on both tables with **zero** policies; the backend is
unaffected because its client is the service role, which bypasses RLS. Nothing
in the runtime changed: no new client, access layer, scheduler, executor or
runtime loop, and no repository query was touched.

---

## 6. Canonical schema reconciliation

- The migration is embedded VERBATIM as **part 13 of 13** of the single setup
  block in §31.3, directly after part 12, with the banner
  `-- ─── PART 13 of 13 — 20261020144423_harden_private_message_table_access.sql …`.
  Every part banner was renumbered to `of 13`; the parts themselves were not
  edited (part 10 still creates the permissive policies, and part 13 visibly
  removes them, which is the honest ordering).
- Counts updated consistently and pinned by tests: **35** migration files,
  **13** embedded parts, "thirteen later additive migrations".
- §31.1 gains audit row 13 and marks the creation migration's `SELECT`-only
  boundary as SUPERSEDED; §31.1.1 gains item 11; §31.2 gains item 12 explaining
  why the hardening must run after part 10 (it `ALTER`s and `REVOKE`s those
  tables) and after parts 11–12 (so no earlier part's bytes change); §31.4
  states the expected verification results; §31.5 states the reversal and calls
  it a security regression; §31.6 states that part 13 changes access, not shape.
- §20's migration inventory gains row 25 (manual application + what stays
  exposed until it runs).
- §18's "Documented exceptions — server-only tables" now names
  `private_message_config` and `private_message_senders` alongside the vault and
  the AED tables.
- §33: the status block, the migrations/canonical-location rows, §33.4 (rewritten
  around the restrictive model and the repository-vs-live distinction), §33.5
  (manual application, the post-apply verification queries, and the fact that
  the reversal is a regression) and §33.6 (an explicit "the live project is not
  claimed hardened") are updated.

One pre-existing inconsistency was observed and deliberately NOT touched (it is
outside this phase's files): the header of
`20261017000001_create_anti_edit_delete_tables.sql` still calls itself "part 11"
of the setup script, although it is part 12. Its executable SQL is unaffected
and the tests compare statements, not prose.

---

## 7. Tests added and updated

**New:** `tests/test_private_message_db_security.py` (24 tests) pins, from the
repository's own files:

- RLS remains enabled, `FORCE`/`DISABLE ROW LEVEL SECURITY` absent;
- zero `CREATE POLICY`, exactly eight `DROP POLICY IF EXISTS`, every expected
  policy name dropped on the right table;
- `REVOKE ALL ON TABLE … FROM PUBLIC, anon, authenticated` on both tables, plus
  the column-level revoke covering exactly the two tables;
- exactly two `GRANT`s, both to `service_role`, both exactly
  `SELECT, INSERT, UPDATE, DELETE`; no `GRANT` to a client role or `PUBLIC`;
- idempotent/additive: no `CREATE/DROP TABLE`, `ADD/DROP COLUMN`,
  `ALTER COLUMN`, `DROP/CREATE INDEX`, `DROP/ADD CONSTRAINT`, `TRUNCATE`,
  `DELETE FROM`, `INSERT INTO`, `UPDATE`, no unrelated table named;
- `NOTIFY pgrst, 'reload schema'` and a read-only verification tail;
- the four verbs are the ones `backend/db/client.py` actually issues, and that
  client still reads `SUPABASE_SERVICE_ROLE_KEY` (never an anon key);
- the historical creation migration is unchanged (it still carries its own
  permissive SQL — the fix is a new file, not a rewrite of history);
- part 13 is the thirteenth and last part, is statement-identical to the
  migration, creates no policy, and part 10 still creates the policies it
  removes;
- counts: 35 migration files, 13 parts, "thirteen later additive migrations";
- §33 documents the restrictive model and separates intended repository posture
  from unverified live posture; §18 names both tables; the AED part is still
  byte-identical (the security phase changed no AED SQL).

**Updated:** `tests/test_database_setup_order.py` (order tuple + access part,
audit count 34→35, deviations, part ordering assertions, AED ordering test
renamed and retargeted) and `tests/test_private_message_scope.py`
(`part 10/11 of 13`, new `part 13 of 13` and migration-name assertions).

### Commands actually run and their real results

| Command | Result |
|---|---|
| `.venv/bin/python -m pytest tests/test_private_message_db_security.py -q` | **24 passed** |
| `.venv/bin/python -m pytest tests/test_private_message_db_security.py tests/test_database_setup_order.py tests/test_private_message_scope.py tests/test_aed_db_security.py tests/test_canonical_schema_reconciliation.py -q` | **226 passed** |
| `.venv/bin/python -m pytest tests -q -p no:randomly` (full suite) | **6480 passed, 27 skipped, 0 failed** (128s) |
| `.venv/bin/python -m py_compile` on the two changed test modules | exit 0 |
| `git diff --check` | exit 0 (no whitespace errors) |

No test failed. No database was contacted by any of these commands; every
assertion reads repository files.

---

## 8. Files changed

| File | Change |
|---|---|
| `supabase/migrations/20261020144423_harden_private_message_table_access.sql` | **NEW** — the additive security migration (§4) |
| `DATABASE_ARCHITECTURE.md` | §31 part 13 + counts/audit/order/verification/reversal; §33 status, rows, §33.4, §33.5, §33.6; §18 exceptions; §20 row 25; §1 successor-table note |
| `tests/test_private_message_db_security.py` | **NEW** — the posture, embed and count contracts (§7) |
| `tests/test_database_setup_order.py` | documented order, counts, ordering assertions |
| `tests/test_private_message_scope.py` | part-number and migration-name documentation assertions |
| `IMPLEMENTATION_REPORT.md` | this report |

Not changed: every backend module (including `backend/db/client.py`), every
other migration, every other test, every other document.

---

## 9. What remains unverified because the SQL was not applied

- **The live Supabase posture is unknown and, until the owner applies the new
  migration, still permissive.** No SQL was executed by this phase, so the anon
  key can still read both tables in the live project if the earlier setup was
  applied. The repository migration and the passing tests prove the INTENDED
  posture only.
- Whether the two tables currently exist in the live project at all is unknown
  (the creation migration is itself documented as not applied).
- Whether PostgREST's schema cache reload has taken effect is unknown.
- No runtime behaviour was exercised against a live database by this phase; it
  is a grant/policy change, and the only runtime claim made is the static one in
  §4 (the four verbs the repository issues).

---

## 10. The SQL the owner should review and apply

Standalone file (the whole migration, prose header included):

```
supabase/migrations/20261020144423_harden_private_message_table_access.sql
```

It is applied normally by running the ONE setup block in
`DATABASE_ARCHITECTURE.md` §31.3 once (it is part 13 of 13). It must run after
the part that creates the two tables.

The executable body, for review:

```sql
ALTER TABLE private_message_config ENABLE ROW LEVEL SECURITY;
ALTER TABLE private_message_senders ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "anon_insert_private_message_config" ON private_message_config;
DROP POLICY IF EXISTS "anon_update_private_message_config" ON private_message_config;
DROP POLICY IF EXISTS "anon_delete_private_message_config" ON private_message_config;
DROP POLICY IF EXISTS "anon_select_private_message_config" ON private_message_config;

DROP POLICY IF EXISTS "anon_insert_private_message_senders" ON private_message_senders;
DROP POLICY IF EXISTS "anon_update_private_message_senders" ON private_message_senders;
DROP POLICY IF EXISTS "anon_delete_private_message_senders" ON private_message_senders;
DROP POLICY IF EXISTS "anon_select_private_message_senders" ON private_message_senders;

REVOKE ALL ON TABLE private_message_config FROM PUBLIC, anon, authenticated;
REVOKE ALL ON TABLE private_message_senders FROM PUBLIC, anon, authenticated;

DO $$
DECLARE
    tbl  text;
    cols text;
BEGIN
    FOREACH tbl IN ARRAY ARRAY['private_message_config', 'private_message_senders'] LOOP
        SELECT string_agg(quote_ident(column_name), ', ' ORDER BY ordinal_position)
          INTO cols
          FROM information_schema.columns
         WHERE table_schema = 'public'
           AND table_name   = tbl;
        IF cols IS NOT NULL THEN
            EXECUTE format(
                'REVOKE ALL (%s) ON TABLE public.%I FROM PUBLIC, anon, authenticated',
                cols, tbl);
        END IF;
    END LOOP;
END $$;

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE private_message_config TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE private_message_senders TO service_role;

NOTIFY pgrst, 'reload schema';
```

(The file also re-states the two table `COMMENT`s and ends with the read-only
verification queries.)

### Verification query to run afterwards

Expected: **no rows** except the `service_role` rows of the last query, and RLS
enabled on both tables.

```sql
-- 1. No client privilege survives (expect 0 rows)
SELECT table_name, grantee, privilege_type
  FROM information_schema.role_table_grants
 WHERE table_schema = 'public'
   AND table_name IN ('private_message_config', 'private_message_senders')
   AND grantee IN ('PUBLIC', 'anon', 'authenticated');

-- 2. No client column privilege survives (expect 0 rows)
SELECT table_name, grantee, column_name, privilege_type
  FROM information_schema.column_privileges
 WHERE table_schema = 'public'
   AND table_name IN ('private_message_config', 'private_message_senders')
   AND grantee IN ('PUBLIC', 'anon', 'authenticated');

-- 3. No policy exists on either table (expect 0 rows)
SELECT tablename, policyname, roles, cmd
  FROM pg_policies
 WHERE schemaname = 'public'
   AND tablename IN ('private_message_config', 'private_message_senders');

-- 4. RLS enabled on both (expect 2 rows, both true)
SELECT relname, relrowsecurity
  FROM pg_class
 WHERE relnamespace = 'public'::regnamespace
   AND relname IN ('private_message_config', 'private_message_senders');

-- 5. service_role keeps exactly the four verbs (expect 8 rows)
SELECT table_name, privilege_type
  FROM information_schema.role_table_grants
 WHERE table_schema = 'public'
   AND table_name IN ('private_message_config', 'private_message_senders')
   AND grantee = 'service_role'
 ORDER BY 1, 2;
```

Rollback, if it must be undone (this RE-OPENS the exposure — it is a security
regression, not a repair):

```sql
GRANT SELECT ON private_message_config TO anon, authenticated;
GRANT SELECT ON private_message_senders TO anon, authenticated;
CREATE POLICY "anon_select_private_message_config" ON private_message_config FOR SELECT
    TO anon, authenticated USING (true);
CREATE POLICY "anon_select_private_message_senders" ON private_message_senders FOR SELECT
    TO anon, authenticated USING (true);
```

---

## 11. Delivery

- Baseline verified: `62ec8d1db912eae9a0feb6b793bafa91cfb2b844`, branch `main`,
  fetched from the remote before any edit.
- All work for this request is ONE commit on top of that baseline; the push is a
  fast-forward to `origin/main` (no rebase, no force-push, no history rewrite).
- The commit SHA and the remote verification (`git fetch`, `git rev-parse
  origin/main`, `git merge-base --is-ancestor`) are stated in the completion
  report for this phase.
- The working tree is clean except for the pre-existing unrelated changes listed
  in §2, which were never staged.
