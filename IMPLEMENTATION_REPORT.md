# IMPLEMENTATION_REPORT.md — Anti-Edit & Anti-Delete database security correction

> This file describes ONLY the current state of the repository with respect to
> the Anti-Edit / Anti-Delete (AED) database **security** correction and
> **replaces every previous report in full** — it is not an appended report.
> The earlier reports (the dedicated-database-foundation round, the
> peer-resolution reliability round, the bot-exclusion round, the archive-UX
> round and the implementation round) remain readable in git history. Evidence
> records live in `docs/investigations/`.

| Item | Value |
|---|---|
| Stage | **Implementation — AED restrictive grant/RLS model delivered (security round)** |
| Date | 2026-10-17 |
| Project / remote | LifeOS Telegram self-bot, `Onlyicing1/Telegram-self-bot` (`origin`) |
| Branch | `main` (work delivered from `fix/aed-db-security`, fast-forwarded into `main`) |
| Base commit | `c54a92cb8de379aff0426a0953462cdd98724c93` — verified `= origin/main` at task start |
| Working tree at start | clean in the task worktree; the shared checkout carried the unrelated pre-existing `M INVESTIGATION.md` and untracked agent directories |
| Live Telegram / Render / Supabase | **NOT touched.** No `.env`, no credentials, no deployment access. **No SQL was executed against Supabase by this task** and the live project was not modified |

---

## 1. Objective and scope

The AED archive stores the **body of the owner's private conversations** in two
dedicated tables (`aed_config`, `aed_archive_entries`). The repository defined
those tables with the generic read-only dashboard boundary — a `GRANT SELECT …
TO anon, authenticated` on each table plus a permissive
`FOR SELECT TO anon, authenticated USING (true)` policy. Anything holding the
anon key could therefore read private message bodies.

This round corrects the **repository's** definition so that applying the
migration (or the ONE canonical setup script in `DATABASE_ARCHITECTURE.md`
§31.3) can never recreate that public read path. It is a source-of-truth fix,
**not** a live database migration: the owner configured the live project
manually, and no SQL of this repository was run against it.

Scope limits honoured: no table schema, constraint, index, comment, retention
semantic, legacy-shard fallback or feature-scope change; no runtime code change;
no new dependency, secret, bucket or environment variable; no second migration
for the same schema; no unrelated table or global Supabase setting touched.

---

## 2. The original defect (repository state at `c54a92c`)

`supabase/migrations/20261017000001_create_anti_edit_delete_tables.sql` (and its
verbatim copy in part 11 of the setup block) contained:

```sql
GRANT SELECT ON aed_config TO anon, authenticated;
GRANT SELECT ON aed_archive_entries TO anon, authenticated;

CREATE POLICY "anon_select_aed_config" ON aed_config FOR SELECT
    TO anon, authenticated USING (true);
CREATE POLICY "anon_select_aed_archive_entries" ON aed_archive_entries FOR SELECT
    TO anon, authenticated USING (true);
```

Root cause: the AED migration was written by copying the Private Message
Management migration's access block, which itself copies the read-only dashboard
convention used by every catalogue-style table (`saved_items`, `bot_logs`, …).
For a table whose rows are private message bodies that convention is wrong: the
`USING (true)` policy plus the `anon` SELECT grant is a public read path. The
documentation repeated the same claim in §31.1 (inventory row 11), §34.4 and §20
(Migration Status row 23), so the defect was described, not flagged.

---

## 3. The corrected security model

For **both** `aed_config` and `aed_archive_entries`:

| Aspect | Definition |
|---|---|
| RLS | `ALTER TABLE <t> ENABLE ROW LEVEL SECURITY;` on both tables |
| Policies | **ZERO.** No SELECT/INSERT/UPDATE/DELETE policy is created for any role (no public, no authenticated, no owner-facing, no dashboard policy) |
| `PUBLIC` / `anon` / `authenticated` | `REVOKE ALL ON TABLE <t> FROM PUBLIC, anon, authenticated;` plus a bounded `DO` block that revokes the **column-level** privileges of every column of both tables (`REVOKE ALL (%s) ON TABLE public.%I FROM PUBLIC, anon, authenticated`) — a table `REVOKE` alone cannot clear a per-column grant |
| `service_role` | `GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE <t> TO service_role;` — exactly the four verbs `backend/db/client.py` uses for AED (reads, upserts, bounded retention delete). The inspection of `_get_aed_config_sync` / `_save_aed_config_sync` / `_aed_*_entries_*` showed no `TRUNCATE`, `REFERENCES`, `TRIGGER` or any other privilege is needed |
| Legacy permissive policies | Removed by name, idempotently: `DROP POLICY IF EXISTS "anon_select_aed_config" ON aed_config;` and `DROP POLICY IF EXISTS "anon_select_aed_archive_entries" ON aed_archive_entries;` (plus the never-created insert/update/delete variants, kept as defensive drops) |
| FORCE ROW LEVEL SECURITY | Deliberately **not** used. `service_role` bypasses RLS in Supabase and the table owner keeps its normal rights, so forcing RLS would only add a failure mode without changing the access model |
| Effect | `anon` and `authenticated` have no table privilege, no column privilege and no matching policy on either table; the backend keeps exactly the access it needs through `service_role` |

Everything else is byte-identical: both `CREATE TABLE` statements, every
constraint (`kind`/`category`/`chat_id > 0`/`message_id > 0`/`body` length/
retention range), the read index `idx_aed_archive_entries_owner_captured`, every
`COMMENT`, `NOTIFY pgrst`, both verification queries and the read-only legacy
`bot_settings` probe.

---

## 4. Files changed

| File | Change | Reason |
|---|---|---|
| `supabase/migrations/20261017000001_create_anti_edit_delete_tables.sql` | Security block replaced: the two `GRANT SELECT … TO anon, authenticated` and the two `CREATE POLICY … USING (true)` statements are gone; the legacy policies are dropped by name; `REVOKE ALL … FROM PUBLIC, anon, authenticated` is added for both tables plus the bounded column-level revoke `DO` block; `GRANT SELECT, INSERT, UPDATE, DELETE … TO service_role` is added. Prose updated: the header's access sentence, a rewritten `## Security` section, and a note in `## MANUAL SUPABASE ACTION REQUIRED` that re-running the file on manually created tables converges them onto the restrictive posture | The migration is the SQL source of truth |
| `DATABASE_ARCHITECTURE.md` | §31.3 part 11 replaced with the corrected statements (statement-identical to the migration); §31.1 inventory row 11 now states the service-role-only posture; §20 Migration Status row 23 likewise; §18 gained a **Documented exceptions — server-only tables** note naming `api_credentials` and the two AED tables; §34.4's owner-isolation bullet rewritten; **new §34.7** documents the defect, the corrected model and its consequences | The document is the canonical database specification; leaving the permissive text would keep the defect documented as intended behaviour |
| `tests/test_aed_db_security.py` | **NEW (11 tests).** Pins the migration's RLS/zero-policy/grant model, the revokes (table- and column-level), the exact service-role verb set, the absence of any client grant, the by-name removal of the legacy policies, the `FORCE` decision, the untouched schema/constraints/index/verification statements, the statement-identity of part 11 with its migration, and the documentation's description of the restrictive model | Make the regression impossible to reintroduce silently |
| `tests/test_database_setup_order.py` | The AED part's guarantee list now asserts the two `DROP POLICY IF EXISTS …` statements, both `REVOKE ALL …` statements and both `GRANT … TO service_role` statements, and asserts `CREATE POLICY` is absent from the part | The setup-order test is the contract that ties the block to the migrations; it must pin the corrected statements |

No runtime code, handler, service, provider, dependency or environment variable
changed. No new migration file was added (the existing AED migration is
corrected in place, which is safe because the Supabase migration history is
empty and the migration has never been applied by this repository).

---

## 5. Migration ⇄ documentation synchronization

* `tests/test_database_setup_order.py::test_every_part_is_statement_identical_to_its_migration`
  proves part 11 of §31.3 is the migration's statements verbatim (comments
  stripped); it passes after the change.
* `tests/test_aed_db_security.py::test_the_canonical_part_is_statement_identical_to_the_migration`
  re-proves the same property for the AED part specifically, and adds
  `test_the_canonical_part_carries_no_permissive_statement`, which fails if any
  `CREATE POLICY`, any `GRANT … TO anon/authenticated` or any
  `FORCE ROW LEVEL SECURITY` reappears in the block.
* The canonical setup model is unchanged: still ONE paste-ready block in §31.3,
  still 11 parts in their documented order, no second apply step, no competing
  script. Part 11 remains the last part and the migration remains the SQL source
  of truth.

---

## 6. Tests actually run (exact commands and results)

All commands were run in the task worktree (`/home/daytona/codebase/.aedsec`)
with the repository virtualenv (`../.venv/bin/python`).

| Command | Result |
|---|---|
| `python -m pytest tests/test_aed_db_security.py -q` | **11 passed** |
| `python -m pytest tests/test_anti_edit_delete.py -q` | **95 passed** |
| `python -m pytest tests/test_database_setup_order.py tests/test_canonical_schema_reconciliation.py -q` | **65 passed** |
| `python -m pytest tests/test_aed_db_security.py tests/test_database_setup_order.py tests/test_canonical_schema_reconciliation.py -q` | **76 passed** |
| `python -m pytest tests -q` (full suite) | **6367 passed, 27 skipped, 40 failed** in 139.84s |
| `python -m py_compile tests/test_aed_db_security.py tests/test_database_setup_order.py` | OK (exit 0) |
| `git diff --check` | OK (exit 0, no whitespace errors) |

**The 40 failures are pre-existing and unrelated.** Every one of them is in
`tests/test_colored_buttons.py`; running that file against a pristine
`origin/main` worktree (`c54a92c`) produced the identical **40 failed, 12
passed**. No test that touches AED, the database schema, the setup block or the
documentation fails.

### SQL verification limits

No PostgreSQL server, `psql` binary or SQL parser is available in this
workspace, and executing SQL against Supabase is prohibited for this task.
Verification of the new statements is therefore static — the repository's own
SQL contract tests plus a structural check (balanced `$$` dollar quotes: exactly
2; zero `CREATE POLICY`; exactly the two intended `GRANT` lines; exactly the two
intended table-level `REVOKE` lines; no `FORCE ROW LEVEL SECURITY`). The SQL has
**not** been executed anywhere.

---

## 7. Live Supabase status (observed vs. repository-only)

* **Direct observations reported by the owner (not produced by this task):** the
  two AED tables already exist in the live project; RLS is enabled on both;
  there are no RLS policies; the column-level privileges show `service_role`
  only; both tables currently hold zero rows; the Supabase migration history is
  empty.
* **Repository-only work (this task):** the corrected migration, the corrected
  canonical block and §34/§18 prose, plus the new regression tests. These are
  source changes only.
* **Explicitly:** no SQL was executed against Supabase, no migration-history
  record was created or fabricated, no grant, policy, table or row was changed,
  and this task did not connect to Supabase at all. The corrected repository
  definition has **not** been applied to the live project by this task.
* Because the live project already matches the restrictive posture, no manual
  action is required to fix the live database. The repository fix exists so that
  a future application of the migration or of §31.3 cannot reintroduce the
  public read path. The canonical block's part 11 is itself safe to re-run: it
  contains no `CREATE TABLE` change for existing tables, drops the legacy
  policies if any survived, and ends in the service-role-only posture.

---

## 8. Commit and remote verification

| Item | Value |
|---|---|
| Commit | recorded in the delivery section below after the push |
| Branch | `main` |
| Remote | `origin` = `https://github.com/Onlyicing1/Telegram-self-bot.git` |
| Push / remote `main` HEAD / ancestry | verified with `git fetch origin`, `git rev-parse origin/main`, `git merge-base --is-ancestor` — see the delivery block appended at the end of this report |

---

## 9. Remaining limitations and unresolved items

* The corrected migration and the canonical block have not been executed against
  any database (no Postgres available; Supabase execution is out of scope). The
  SQL is reviewed statically and pinned by tests only.
* The column-level `DO` block revokes every column privilege of the two tables
  from `PUBLIC`/`anon`/`authenticated`. It cannot revoke a grant made to some
  other, unmodelled role; no such role exists in this project's model.
* `postgres` (the table owner) keeps its normal rights — FORCE RLS was
  deliberately not added, matching the documented Supabase service_role model.
* The other tables (`saved_items`, `bot_logs`, emoji tables, Private Message
  tables, …) keep the read-only dashboard convention unchanged; this task did
  not alter the project-wide model, only the AED exception and its documentation.
* Live Telegram behaviour, the Render deployment and the live database were not
  verified — this task is a repository correction with no runtime change.
