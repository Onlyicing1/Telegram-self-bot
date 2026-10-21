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

---

## 12. Follow-up pass — the one-paste setup reconciled against the current inventory

**Baseline:** `origin/main` `f432493` (the hardening commit above already carried
part 13 and its counts). This pass re-audited the whole artifact instead of
trusting that state.

**What was audited.** All **35** files in `supabase/migrations/`, the complete
§31.3 block, both byte-identical canonical copies
(`supabase/canonical_bootstrap.sql` == `20260920000001_reconcile_canonical_schema.sql`,
confirmed with `cmp`), and the four database documentation test modules. Every
part's statement identity was re-proved against its migration; every table the
backend addresses (24 `.table()` names) and every RPC it calls is created by the
block; every literal write payload compared against the block's declared
columns.

**What was wrong (documentation only).**

1. §31 intro counted "thirteen later additive migrations" — the block has 13
   parts, i.e. the snapshot plus **twelve** later ones.
2. The block banner still called the twelve later parts "pending migrations",
   which reads as a second, unapplied step.
3. §31.6 claimed "the last six parts add no foreign key" — part 8 creates
   `todo_steps.task_id → ai_tasks(id) ON DELETE CASCADE`.
4. §30.11 (the manual-action section) listed "nine pending migrations" and said
   only parts 4, 5, 7, 8 and 9 carried the later files, omitting parts 6, 10, 11,
   12 and 13.
5. §20's row for `20260927020000_add_waiting_answer_status.sql` still said
   "NOT APPLIED — owner action required / until it runs the park write is
   refused", although part 1 re-adds that status CHECK in final form (§31.1);
   the same table's footnote still counted **34** files.
6. Thirteen per-column/per-table notes (§1, §2, §6, §7, §12, §13, §19) said a
   migration was "pending manual application", implying the owner must apply it
   separately from §31.3; each now names the part that carries it.

**What was NOT invented.** `settings_service` writes
`panel_settings.auto_reaction_enabled` / `auto_reaction_emoji` and
`auto_reaction_service` reads them, but **no migration file creates those two
columns**. §6 previously told the owner to paste a snippet for them, i.e. a
second schema step outside the one block. No migration was fabricated here (the
request limits this pass to documentation and tests, and no schema object may be
added speculatively): the pair is now documented as the one application-required
exception — §6 marks both columns "no migration creates it", §31.6 states the
block cannot establish them, and §30.11 item 4 names them instead of claiming
nothing is missing. Generating the additive migration is the owner's next call.

**Tests.** Two existing assertions that pinned the wrong count ("thirteen later
additive migrations") were corrected, and four narrow tests were added: the later
part count matches the block, §30.11 names every embedded part and no stale
subset, the waiting-answer migration is recorded as absorbed by part 1, and the
unmigrated Auto-Reaction pair stays documented while no part invents it.

**Verification.** `pytest tests/test_database_setup_order.py
tests/test_canonical_schema_reconciliation.py tests/test_private_message_db_security.py
tests/test_aed_db_security.py` → 112 passed. Full suite → 6484 passed, 27
skipped (the sandbox had Telethon 1.34.0 instead of the pinned 1.43.0, which
fails 40 unrelated colored-button assertions; installing the pinned version made
the suite green). `git diff --check` clean. **No SQL was executed and Supabase
was not contacted.**

> **Superseded by §13.** That pass deliberately left the Auto-Reaction pair as
> the block's one documented exception. §13 generated exactly the additive
> migration §12 asked the owner to decide on, so the pair is an exception no
> longer: it is part 14 of the same one-paste block.

---

## 13. Persistence round — AED, Private Message Management, Auto-Reaction

**Baseline:** `d33dbe27956aaf3819b27ae2ce416856f9de968f` (= `origin/main`, verified
with `git fetch` + `git rev-parse`), branch `db-persistence-audit`. The audit
that named the defects is `INVESTIGATION.md` (replaced in full by that round);
this section records only the fixes and their verification.

**The report.** The owner reports that parts of **AED**, **Private Message
Management** and the **Auto-Reaction setting** reset after updates/redeploys.
Five defects were confirmed from source; none was a "table does not exist"
failure, and no row deletion exists anywhere in the repository.

### 13.1 Defects and fixes

| # | Defect (feature) | Fix |
|---|---|---|
| 1 | AED never read the pre-migration `bot_settings` store once the dedicated tables became readable, so the first restart after the migration showed the defaults and hid every archived row (AED) | `_import_legacy_store` imports the legacy archive **and** configuration once, in that order, into the dedicated tables |
| 2 | All three AED capture handlers read the synchronous cache switch before the asynchronous preload finished, so messages/edits/deletions arriving in the startup window were dropped (AED) | each handler awaits `ensure_loaded_async()` before `is_enabled()` |
| 3 | A failed read was indistinguishable from “no row”: the defaults were loaded, marked loaded, and then persisted over the real row. For PM the failed read also fell through to the legacy document and wrote that over the dedicated row (AED + PM) | `aed_load_config_state`, `aed_get_document_state` and `read_private_message_config` return `(value, readable)`; an unreadable store leaves the feature fail-closed, is never marked loaded, and is retried at most every 5 s |
| 4 | No migration created `panel_settings.auto_reaction_enabled` / `auto_reaction_emoji`, although `settings_service` wrote both, so the write was rejected and the value lived only in the process cache (Auto-Reaction) | new additive migration, embedded as part 14 of §31.3 (§13.3) |
| 5 | PM reported a durable save while writing nothing when no owner was configured (PM) | `_write_state_row` returns `False` when there is no owner; every mutation awaits the load and is refused while the stored state is unknown |

The import's safety properties, in the order it enforces them: it runs only in
`dedicated` mode and only while the owner has **no** dedicated configuration row
(that row is the migration marker, so a second start — or an owner who later
clears the archive — never re-imports); rows are imported only when the
dedicated archive is empty; an unreadable legacy source aborts and is retried
rather than importing defaults over a stored configuration; nothing is deleted.

### 13.2 Files

Source: `backend/db/client.py` (three `(value, readable)` readers; the existing
single-value helpers delegate, so no caller changed meaning),
`backend/services/anti_edit_delete_service.py`,
`backend/services/private_message_service.py`,
`backend/bot/handlers/anti_edit_delete.py`.
Schema: `supabase/migrations/20261020160000_add_panel_settings_auto_reaction_settings.sql`.
Tests: `tests/test_auto_reaction_schema.py` (new),
`tests/test_anti_edit_delete.py`, `tests/test_private_message_management.py`,
`tests/test_database_setup_order.py`, `tests/test_private_message_db_security.py`,
`tests/test_private_message_scope.py`, `tests/test_private_message_panel.py`.
Documentation: `DATABASE_ARCHITECTURE.md`, `ROADMAP.md`, `INVESTIGATION.md` and
this report. No dependency, secret or unrelated feature was touched, and no SQL
was executed against any database.

### 13.3 The migration and the canonical block

`20261020160000_add_panel_settings_auto_reaction_settings.sql` is additive and
idempotent per column: `ADD COLUMN IF NOT EXISTS` (no default, so an existing
column keeps every value) → `SET DEFAULT` (`false` / `''`) → `UPDATE … WHERE
<col> IS NULL` → `SET NOT NULL`, then `NOTIFY pgrst` and one read-only
column-count query. It creates no table, index, constraint, policy, grant or row,
and it cannot enable a reaction: `false` is the OFF default and the file carries
no `= TRUE` assignment. It is embedded verbatim as **part 14 of 14** of §31.3;
the block now comprises the snapshot plus **thirteen** later additive migrations,
and the migration inventory row, §6, §30.11, §31.6 and the reconciliation
narrative were updated to say so. The two byte-identical canonical copies
(`supabase/canonical_bootstrap.sql` and
`20260920000001_reconcile_canonical_schema.sql`) embed part 1 only and were
correctly left unchanged.

### 13.4 Verification

- Targeted: `pytest tests/test_auto_reaction_schema.py tests/test_anti_edit_delete.py
tests/test_private_message_management.py -q` → **148 passed**;
  `pytest tests/test_database_setup_order.py
tests/test_canonical_schema_reconciliation.py tests/test_private_message_db_security.py
tests/test_aed_db_security.py tests/test_private_message_scope.py
tests/test_private_message_panel.py -q` → **281 passed**.
- Full suite: `pytest -q` → **6500 passed, 27 skipped** (CPython 3.10.12,
  `telethon 1.43.0` as pinned, `supabase 2.4.2`), the new module included.
- The new suite executes the shipped migration statement by statement against a
  column/row model with PostgreSQL semantics (a re-run is a no-op, an existing
  value — even a non-default one — survives, only NULL cells are backfilled, a
  surviving NULL would fail `SET NOT NULL`) and refuses any statement form it
does not model, so the migration cannot silently grow an unverified step. It
  also pins the OFF default and the real-column write path through
  `panel_settings_repository`.

### 13.5 Limits — what is NOT proven

1. **No SQL was executed and Supabase was not contacted.** The migration's
   semantics are verified against the model above, not against PostgreSQL.
2. **The migration is not applied to the live project.** Until the owner runs
   §31.3 (or this file) the Auto-Reaction pair still lives only in the cache;
   the code now reports that write failure instead of pretending success.
3. **Live restart behaviour was not observed.** “The archive survives a
   redeploy”, “the PM policy survives a redeploy” and “the legacy import ran”
   remain untested claims until a restart is observed on the deployment.
4. **The AED import runs against the real store for the first time in
   production.** It is idempotent and marker-gated, but the first start after
   the migration is the only chance to verify it; the log line
   `[AED] imported … legacy archive row(s)` / `[AED] legacy configuration
   imported into aed_config` is the evidence to look for.
5. **Not addressed (out of scope, unchanged):** a transient store failure still
   makes the feature fail closed until the next retry succeeds — that is the
   intended trade (never overwrite a stored configuration with defaults), not a
   regression.

---

## 14. Navigation round — the Cipher category

**Baseline:** `fe462940cc953950eb1f434687a2a8d67a8a8fc8` (= `origin/main`, verified
with `git fetch` + `git rev-parse`), read from a detached worktree at that
commit. The shared checkout's pre-existing branch and working-tree edits were
never touched.

**Objective.** Group the private-message features into one first-class category,
**Cipher** (subtitle *Private Communications*), so the mother panel stops
listing each of them separately — without changing any feature's handler,
service, persistence, permissions or execution path.

### 14.1 Classification, from source

Moved under Cipher:

- **Private Messages** (`backend/bot/handlers/private_message.py`, the
  `panel:pm` family): effective mode, closing actions (delete / fixed response /
  AI response), allowlist, exceptions, temporary override, schedule and its rule
  editor, the per-person AI reply limit and interval, the durable AI counters
  and their automatic reset window. This is the PV policy itself.
- **Ghost Seen** (`backend/bot/handlers/ghost_seen_v2.py`,
  `panel:ghost_seen_v2`): the private-chat browser, the message viewer, the
  reply / AI-reply actions and the per-chat allow-list (`Permissions`). Every
  one of those panels is scoped to private chats.

Intentionally left outside Cipher:

- **Anti-Edit / Anti-Delete** (`panel:archive`): its capture gate is indeed
  private-chat-only (`eligible()` rejects every non-private chat), but its
  primary purpose is an independent edit/deletion archive — not private-message
  policy, PV sender access or PV reply behaviour. It is the adjacent feature the
  task says to leave alone unless the source makes it part of the PV set; it
  does not, so it stays top-level.
- **Auto-Reaction** (inside `General`): it reacts to the owner's own outgoing
  messages in every chat, not to private messages specifically.
- **Save, Delete, Database, AI, Profile, Todo**: general-purpose features with
  no PV-specific behaviour.

### 14.2 The category and the navigation change

- New `cipher` panel, registered in `misc.py` as a child of `menu` with title
  `Cipher` and the description `Private Communications`.
- The mother panel now shows one `Cipher` button where `Ghost Seen` and
  `Private Messages` used to be listed; every other row and callback value is
  unchanged.
- `pm` and `ghost_seen_v2` were re-parented from `menu` to `cipher`, so Back from
  either — with an empty nav stack — returns to Cipher. Their ids, titles,
  sub-panel parents, actions, inputs, services and persistence are untouched, so
  a callback that already existed still resolves to the same handler.
- Because the panels themselves did not change, the shared renderer still
  injects the standard Back/Home/Close row for them, and Home still resets to
  the mother panel.
- No icon was added: the mother panel uses no icon convention for categories,
  and the project's symbol registry is functional (geometric) rather than
  decorative — a shield glyph would have introduced a new visual language.

### 14.3 Files

- `backend/bot/handlers/misc.py` — the `Cipher` category buttons, handler,
  inline builder and registration; the two removed menu rows.
- `backend/bot/handlers/private_message.py` — parent `menu` → `cipher`, docstring
  navigation note.
- `backend/bot/handlers/ghost_seen_v2.py` — parent `menu` → `cipher`.
- `tests/test_cipher_category.py` (new) — the menu, the category, reachability,
  parent chain, Back/Home, duplicate-free callbacks and the no-mutation check.
- `tests/test_private_message_panel.py`, `tests/test_colored_buttons.py`,
  `tests/test_60_ghost_seen_v2_nav_search_perf.py` — updated assertions only.
- `AGENTS.md` §5 and this report.

No service, database client, migration, SQL, callback action, input or
dependency was changed.

### 14.4 Verification

- Focused: `pytest tests/test_cipher_category.py tests/test_private_message_panel.py
  tests/test_colored_buttons.py -q` → **119 passed** (17 of them the new module).
- Ghost Seen / emoji / AED / Todo batch: **352 passed, 1 failed** — the failure
  was `test_60_ghost_seen_v2_nav_search_perf` still asserting the old `menu`
  parent, which is exactly the navigation this round changes; the assertion was
  updated to `cipher`, and the two Ghost Seen navigation modules then passed
  (**31 passed**).
- Full suite: `pytest -q` → **6517 passed, 27 skipped** (CPython 3.10.12,
  Telethon 1.43.0 as pinned).
- `python -m py_compile` on the three changed modules and the new test module →
  OK; `git diff --check` → clean.
- The new module drives the real registry, the real panel handlers and the real
  `_handle_navigation`; only the final edit boundary is stubbed, so no Telegram
  call is made.

### 14.5 Limits

1. **No live helper-bot/inline rendering was observed.** The category is
   verified through the same render/finalize path the callback router uses, but
   not on a running account.
2. **No database was contacted.** This round changes no schema and reads/writes
   no stored configuration; the no-mutation check covers the in-memory PV state.
3. **Back/Home were driven directly**, not through a real callback query; the
   data values themselves are asserted unchanged.

---

## 15. Navigation round — Anti-Edit / Anti-Delete under Cipher

**Baseline:** `bdcb1d16e83be376bec3543bc8656ef40490f1da` (= `origin/main`, verified
with `git fetch` + `git rev-parse`), read from a detached worktree at that
commit. The shared checkout's pre-existing branch and working-tree edits were
never touched.

**Objective.** Move the existing **Anti-Edit / Anti-Delete** entry from the
mother panel into the existing **Cipher** category. Cipher is a panel
organization category: the move changes where the entry is reached from, not
what the feature does or which chats it captures.

### 15.1 The change

- `misc.py::_build_cipher_buttons` gained the existing entry
  `("Anti-Edit / Anti-Delete", "panel:archive")`; the label is unchanged.
- `misc.py::_build_menu_buttons` lost its `Anti-Edit / Anti-Delete` row, so the
  feature is not duplicated and every other top-level entry stays where it was.
- `anti_edit_delete.py` registers its root panel with `parent="cipher"` instead
  of `parent="menu"`, so Back from the archive returns to Cipher when the nav
  stack is empty. Its panel id and title, the `archive_list` / `archive_item` /
  `archive_cats` / `archive_confirm` parents, all `aed_*` actions, the
  `retention` input, the capture handlers, service calls and persistence are
  unchanged.

No callback id, action, input, service, database file, migration or
documentation about the schema was changed.

### 15.2 Files

- `backend/bot/handlers/misc.py` — one row moved between two button builders.
- `backend/bot/handlers/anti_edit_delete.py` — panel parent `menu` → `cipher`.
- `tests/test_cipher_category.py` — the category now registers AED too: its menu
  absence, presence/label inside Cipher, parent chain, sub-panel parents,
  Back-from-archive and the untouched AED configuration are asserted.
- `tests/test_anti_edit_delete.py` — the parent assertion and the old
  “mother menu opens the archive” test now assert the Cipher route.
- `AGENTS.md` §5 and this report.

### 15.3 Verification

- Focused: `pytest tests/test_cipher_category.py tests/test_anti_edit_delete.py
  tests/test_colored_buttons.py tests/test_private_message_panel.py -q` →
  **223 passed** (`tests/test_cipher_category.py` alone: **20 passed**).
- Full suite: `pytest -q` → **6520 passed, 27 skipped** (CPython 3.10.12,
  Telethon 1.43.0 as pinned).
- `python -m py_compile` on `misc.py` and `anti_edit_delete.py` → OK;
  `git diff --check` → clean.

### 15.4 Limits

1. **No live Telegram panel was rendered.** The move is verified through the
   real registry, panel handlers, `_finalize_panel` and `_handle_navigation`,
   not on a running account.
2. **No database was contacted.** The no-mutation assertion covers the in-memory
   AED configuration only; nothing in this round reads or writes a table.
3. Capture behaviour (new message / edit / deletion) was not exercised by this
   change; the existing AED tests that cover it still pass in the full suite.
