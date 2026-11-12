# IMPLEMENTATION REPORT — Intelligent Reaction: owner-configured probability

## 1. Task objective

Integrate the corrected Intelligent Reaction implementation into `main`, finalize
its database migration against the real `main` schema history, update the
authorized database documentation, validate, commit, push to `origin/main` and
verify the pushed commit independently on GitHub.

## 2. Initial workspace and branch state

- Repository: `Onlyicing1/Telegram-self-bot`, worktree
  `/workspace/chats/cbe37986dff39854615f867f`.
- Starting branch: `freebuff/cbe37986dff39854615f867f`.
- Starting HEAD: `1d0ed3afe8e2cac93f65073de62211713cf932a6`.
- The remote-tracking ref was missing locally (`origin/main` was ambiguous), so
  `git fetch origin main` was run first and `refs/remotes/origin/main` was created
  from `FETCH_HEAD`: **`c43c5fa43ee882cfdceaf9d1e1c677a795923638`**
  (`feat(reactions): add intelligent per-message reactions`).
- `git rev-list --left-right --count HEAD...origin/main` → `0 18`: the local
  branch had **no** commits of its own and was 18 commits behind `main`, so the
  integration is a clean fast-forward, not a merge.
- Pre-existing uncommitted work in the tree (from earlier, unrelated session
  hardening): `backend/bot/client.py`, `backend/main.py`,
  `backend/runtime/supervisor.py`, `backend/runtime/crash_diagnostics.py`, plus
  the untracked `tests/test_runtime_session_terminal.py`. It was preserved
  untouched (see §11), never staged, and never discarded.

## 3. Integration strategy

`main` already contained an Intelligent Reaction implementation, but with the
**old contract**: the AI returned its own `probability`, and the migration
`20261112140000_add_panel_settings_intelligent_reaction.sql` added only
`intelligent_reaction_enabled`. Because a stale copy must never overwrite newer
main files, the work proceeded as:

1. Back up the draft files by explicit path (`/tmp/ir-work/`), remove them, and
   move the unrelated tracked edits into `git stash` (`stash@{0}`), leaving only
   the untracked startup test file in the tree.
2. `git merge --ff-only origin/main` → HEAD became `c43c5fa` with no conflicts.
3. Re-implement the corrected contract **onto** main's tree, reusing main's
   wiring (router registration, handler registration, settings service, General
   panel, mutual exclusion, migration conventions) and replacing only the
   behaviour that the contract changes.
4. One implementation commit (`5701aed`) on a fast-forwarded history; this
   report is its direct child, and both are pushed to `main`.

No rebase, reset, force-push or history rewrite was used.

## 4. Migration correction and compatibility with main

- `main` already owns `intelligent_reaction_enabled` through
  `20261112140000_add_panel_settings_intelligent_reaction.sql`, which the owner
  previously reported as applied to Supabase. That file is **unchanged**.
- The earlier draft (which re-added both columns) was discarded. The delivered
  migration is probability-only:
  `supabase/migrations/20261112160000_add_panel_settings_intelligent_reaction_probability.sql`.
- It never touches the enabled column, never resets a stored value, adds no
  table/index/policy/grant, and is idempotent: add column without default → bind
  `DEFAULT 25` → backfill only `NULL`s → `SET NOT NULL` → (re)create the 0–100
  CHECK after the backfill.
- Ordering: timestamp `20261112160000` sorts after the enabled migration, so on
  a fresh or partially migrated database it runs last.

## 5. Implementation changes

- `backend/services/intelligent_reaction_service.py` — rewritten engine:
  eligibility (owner-authored outgoing, text required, inline-origin, pending
  panel input, duplicate guard), mutual exclusion with Auto-Reaction,
  `probability_passes` (0 never/100 always/one independent draw otherwise),
  Telegram-only catalog snapshot (`reaction_source_service.load_reactions`, same
  snapshot for prompt and validation, fail-closed), bounded context builder,
  strict `parse_ai_reaction` (`{"reaction": …}` only; duplicate keys, extra
  fields, legacy `probability`, malformed or non-catalog emoji all rejected), one
  bounded AI call (20 s timeout, JSON response format), one
  `reaction_service.react_to_message` attempt, and one stable outcome status per
  evaluation (`reacted`, `telegram_send_failed`, `skipped_*`).
- `backend/bot/handlers/intelligent_reaction.py` — passes the coarse chat type,
  explicit reply flag and at most one replied-to text; logs no ids or content.
- `backend/services/settings_service.py` — new key
  `intelligent_reaction_probability` (default 25, validator 0–100, typed getter
  with safe fallback, strict setter via `set_setting_strict` so a rejected write
  never claims success); existing enabled key and mutual exclusion unchanged.
- `backend/bot/handlers/misc.py` — General panel shows `Reaction Probability:
  N%`, exposes a `Set Reaction Probability` button
  (`input:general:intelligent_reaction_probability`) and a validating input
  handler that reports a rejected write honestly.
- `supabase/migrations/20261112160000_add_panel_settings_intelligent_reaction_probability.sql`
  — the probability-only migration.
- `tests/test_intelligent_reaction.py` — replaced the old AI-probability suite
  with 103 tests for the corrected contract.
- `DATABASE_ARCHITECTURE.md`, `ROADMAP.md` — documented state (§7).
- Count-pinned tests updated to the verified inventory:
  `tests/test_database_architecture_doc.py`, `tests/test_database_setup_order.py`,
  `tests/test_private_message_db_security.py` (40 → 41 files, 39 → 40 schema
  migrations, 20 → 21 typed accessors, `**41** files` in §22). No assertion was
  weakened; only the stale numbers changed.

## 6. Functional contract (as implemented)

- Setting `intelligent_reaction_probability`, default `25`, integer `0–100`,
  edited in the General panel, read for every eligible message (no restart).
- `0%`: no catalog request and no AI call. `100%`: always passes. `1–99%`:
  exactly one independent draw (`draw * 100 < percent`). No counters, quotas,
  windows, streaks, carryover or compensation. The AI never decides probability.
- AI contract: `{"reaction": "<supported emoji>"}` or `{"reaction": null}`;
  anything else — malformed JSON, extra/repeated fields, unsupported emoji, or a
  legacy `probability` — is rejected and nothing is sent.
- Catalog: Telegram only, via `reaction_source_service` /
  `GetAvailableReactionsRequest`; same snapshot for the prompt and validation; no
  hardcoded list or fallback; unavailable catalog fails closed.
- Context: owner's message text (≤1000 chars), at most one replied-to text
  (≤500 chars), chat type in `private|group|supergroup|channel|unknown`, explicit
  `is_reply`. No ids, usernames, titles, media metadata, history or memory.

## 7. DATABASE_ARCHITECTURE.md updates (authorized sections only)

- §31 intro and §31.1 heading: 39 → **40 schema migrations**.
- §31.1 inventory paragraph: `41 files: 40 schema migrations … all 40 migrations`.
- §31.1 classification sentence for the probability migration: additive,
  post-reconciliation, not embedded (so parts 1–17 keep their numbers).
- §6 `panel_settings`: Intelligent Reaction row reworded to the owner-probability
  contract, plus a new `intelligent_reaction_probability` row
  (`integer`, NOT NULL, default 25, CHECK 0–100, creating migration);
  accessor count 20 → **21 typed getters + 21 typed setters**, setting columns
  22 → **23**.
- §22: repository inventory **41 files** (40 schema + 1 hardening), both
  Intelligent Reaction migrations committed and **not applied**, migration
  history still 3 records.
- Verified count: `supabase/migrations/*.sql` = **41** files
  (`git ls-tree` on the delivered commit), matching the documented numbers and
  the updated count tests.

## 8. Tests actually run

- Focused: `.venv/bin/python -m pytest tests/test_intelligent_reaction.py -q`
  → **103 passed**.
- Documentation/migration suites:
  `tests/test_database_architecture_doc.py`, `tests/test_database_setup_order.py`,
  `tests/test_private_message_db_security.py` → all passing except the
  pre-existing doc-drift failures listed in §9.
- Full suite:
  `.venv/bin/python -m pytest tests/ -q --ignore=tests/test_runtime_session_terminal.py`
  → **7510 passed, 6 failed, 27 skipped** in 141.80 s.
  The ignored file is the untracked startup-hardening test from unrelated
  in-progress work; it does not collect against this base (see §11).
- `python -m py_compile` on every edited Python file → exit 0.
- `git diff --check` → exit 0 (no whitespace errors).

## 9. Remaining test failures — verified pre-existing

Each was reproduced on a clean `c43c5fa` worktree (`git worktree add /tmp/base2
c43c5fa`) before being attributed to main; none touches a file changed here.

1. `tests/test_database_setup_order.py::test_the_section_does_not_pretend_the_sql_was_executed`
   — §31 no longer contains the literal "NOTHING has been executed against
   Supabase" (main's wording says "no SQL was executed against any database").
2. `tests/test_database_setup_order.py::test_the_waiting_answer_migration_is_absorbed_by_part_one`
   — §22 no longer carries a row for
   `20260927020000_add_waiting_answer_status.sql` (StopIteration).
3. `tests/test_private_message_db_security.py::test_the_rls_model_names_the_two_tables_as_server_only_exceptions`
   — §18 no longer contains "Documented exceptions".
4. `tests/test_aed_db_security.py::test_the_documentation_describes_the_restrictive_model`
   — §34 wording drifted from the test's required phrases.
5. `tests/test_canonical_schema_reconciliation.py::test_the_migration_is_forward_only_and_its_successors_stay_additive`
   — the test flags `TRUNCATE` in
   `20261111143543_harden_public_schema_privileges.sql`, where it appears inside a
   `REVOKE … TRUNCATE …` privilege list (false positive in main's test).
6. `tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`
   — scheduler recovery flakiness observed before the Intelligent Reaction work
   (unchanged by this delivery).

Fixing 1–5 would require rewriting documentation sections belonging to other
features (outside the authorized scope) or changing another feature's test; they
are reported, not hidden, and no assertion was weakened.

## 10. Manual Supabase SQL (prepared, NOT executed)

Application SQL — exactly the executable statements of
`supabase/migrations/20261112160000_add_panel_settings_intelligent_reaction_probability.sql`:

```sql
ALTER TABLE panel_settings ADD COLUMN IF NOT EXISTS intelligent_reaction_probability integer;
ALTER TABLE panel_settings ALTER COLUMN intelligent_reaction_probability SET DEFAULT 25;
UPDATE panel_settings SET intelligent_reaction_probability = 25 WHERE intelligent_reaction_probability IS NULL;
ALTER TABLE panel_settings ALTER COLUMN intelligent_reaction_probability SET NOT NULL;
ALTER TABLE panel_settings DROP CONSTRAINT IF EXISTS panel_settings_intelligent_reaction_probability_check;
ALTER TABLE panel_settings ADD CONSTRAINT panel_settings_intelligent_reaction_probability_check
    CHECK (intelligent_reaction_probability >= 0 AND intelligent_reaction_probability <= 100);
```

Rollback SQL — **DESTRUCTIVE**: it destroys the owner's stored percentage, which
then falls back to the in-memory default of 25 on the next load:

```sql
ALTER TABLE panel_settings
    DROP CONSTRAINT IF EXISTS panel_settings_intelligent_reaction_probability_check;
ALTER TABLE panel_settings DROP COLUMN IF EXISTS intelligent_reaction_probability;
```

Database status, stated precisely:

- Migration file created: **yes** (probability-only, idempotent).
- SQL reviewed against main's migration history: **yes**.
- SQL syntax validated: **by inspection only** (no database was touched).
- Applied to Supabase: **NO**.
- Live schema verified: **NO**.

**Migration prepared and documented; NOT applied to Supabase.** No Supabase tool,
SQL execution or schema modification was performed by this task, and no claim of
live verification is made.

## 11. Files intentionally untouched

- `supabase/migrations/20261112140000_add_panel_settings_intelligent_reaction.sql`
  (already applied; must never be rewritten).
- All other `supabase/migrations/*.sql`.
- `INVESTIGATION.md` (its subject — the Model Tester caption overflow — is
  unchanged and unrelated).
- Every other database-documentation section not listed in §7.
- Unrelated pre-existing work: `backend/bot/client.py`, `backend/main.py`,
  `backend/runtime/supervisor.py`, `backend/runtime/crash_diagnostics.py`
  (preserved in `git stash@{0}`), and the untracked
  `tests/test_runtime_session_terminal.py` (left in the worktree, not committed).

## 12. Delivery

- Implementation commit: **`5701aed1a53dd6c3e32d55753d1f652950e65f2b`**
  (`feat(reactions): make Intelligent Reaction probability owner-configured`).
- This report is the direct child commit of that implementation commit and ships
  in the same push.
- Target branch: **`main`**. The local branch was renamed to `main` before the
  push; the push is a normal fast-forward from `c43c5fa` (no force).
- Push result and the independently verified `origin/main` SHA (`git fetch`,
  `git rev-parse origin/main`, `git ls-remote origin refs/heads/main`) are
  recorded in the task response for this delivery, because a commit cannot
  contain its own SHA.
- Final working tree: clean except the explicitly identified untracked
  startup-hardening test file and the preserved `git stash`.

## 13. Live verification limitations

No Render deployment was performed and no live Telegram action was exercised.
Panel rendering, provider calls and Telegram sends are verified only by the
offline test suite (scripted provider and Telegram fakes); runtime behaviour on
the deployed bot and the applied database schema remain owner actions.
