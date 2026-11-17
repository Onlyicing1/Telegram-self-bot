# IMPLEMENTATION REPORT

**Reporting date:** 2026-11-17
**Latest delivery:** G4-A / F1 — Single-Source Evidence Foundation, remediation sprint
**Base commit:** `7f02e88c469dfd569eaa43c5005964dc20021a89` (`origin/main`, parent `c8741935dd10f09face8367b9eaa5810c0df34c0`)
**Starting HEAD:** `76aacc851483dc2ade1d2dab46cd7e2e5a714743` (docs branch, an ancestor of the target)
**Final commit:** recorded in the delivery response; this report is committed in the same commit it describes.
**Status:** remediation implemented offline and focused-tested. The additive migration
has been edited in place but **remains unapplied**. No SQL, Supabase, Telegram,
Render, provider or deployment operation was performed by this delivery.

## Disposition of the six findings

| # | Finding | Disposition | Evidence |
|---|---|---|---|
| M1 | A ref did not unambiguously identify the immutable revision it produced | **FIXED** | `ai_evidence_refs.revision_id` was written by no code path. `archive_ref` now links the ref to the exact revision (created or deduplicated), validated against owner/chat/message and write-once, at both the application and database boundary. |
| M2 | Persistence failure could silently degrade to process memory | **FIXED** | `EvidenceRepository()` no longer falls back to memory when Supabase is unavailable; it holds an explicit unavailable client that raises `EvidenceStorageUnavailable`. Memory is only reachable through `allow_memory=True`/`in_memory_repository()`. |
| M3 | Quota checks could race and exceed owner limits | **FIXED (application + SQL functions)** | Run admission and archive writes are one serialized operation: an in-memory lock for the explicit test double and `pg_advisory_xact_lock` inside `ai_evidence_create_run` / `ai_evidence_archive_revision` for Supabase. PostgreSQL concurrency itself remains unverified (no database was available). |
| M4 | Cited Summary could be produced without explicit per-request opt-in | **FIXED** | `run_cited_summary(..., opt_in=False)` now refuses to run without explicit opt-in; both call sites assert it. The `cited_summary` AI tool is `CONFIRMATION_REQUIRED`, so the executor cannot auto-run it and a confirmation is never remembered. The History link path now clears the toggle like the Run path. |
| M5 | Dependency checks were not atomic at the deletion operation | **DEFERRED WITH BLOCKER** | F1 has no dependent table. `count_revision_dependents`/`count_source_dependents` are still explicit zero stubs on the durable repository, so the approved dependent preflight cannot exist yet. See the blocker below. |
| M6 | Explorer used global mutable state for owner-specific results | **FIXED** | All module-level result caches were removed; every view receives the data its own handler loaded, and every opaque id is re-resolved through an owner-filtered repository call. |

## What changed

### M1 — exact revision association

- `backend/evidence/repository.py`
  - `link_ref_revision(owner_id, run_id, ordinal, revision_id)` performs a
    write-once link. Memory mode validates that the revision exists for the
    owner and that its source matches the run's peer and the ref's message;
    durable mode uses `UPDATE ... WHERE owner_id = ? AND run_id = ? AND ordinal = ?
    AND revision_id IS NULL` and treats an existing identical link as idempotent.
  - `archive_atomic(...)` returns the created or deduplicated revision id, and
    `get_revision` is used by the service to confirm it before linking.
- `backend/evidence/service.py`
  - `archive_ref` links the ref on both the `created` and the raced `unchanged`
    path, through `_link_ref`, which re-reads the source and rejects a
    revision that does not belong to the run's peer/message.
- `supabase/migrations/20261116120000_create_evidence_foundation.sql`
  - `ai_evidence_ref_revision_guard` trigger: rejects any ref update except one
    `NULL → revision` transition; rejects a second link, a different link, a
    missing revision, a foreign owner, or a source/message mismatch.
  - `GRANT UPDATE (revision_id) ON ai_evidence_refs TO service_role` — the only
    update the table accepts (no table-level UPDATE grant).
  - No foreign key on `revision_id` by design: the approved deletion policy
    keeps a historical unresolved reference in the ref after an explicitly
    confirmed archive deletion, instead of nulling or cascading it.

### M2 — fail closed without durable storage

- `EvidenceRepository.__init__(..., allow_memory: bool = False)`:
  `db=None` now resolves `get_db()` once; if it is unavailable the repository
  installs `_UnavailableClient`, whose `table`/`rpc` raise
  `EvidenceStorageUnavailable`. No evidence method reaches the in-memory store.
- `is_memory` reports the explicit mode, not a missing client; test seams and
  `in_memory_repository()` are the only way into memory mode.
- `_call` re-raises `EvidenceStorageError` unchanged so the bounded message
  reaches the service/tool boundary instead of an `AttributeError` wrapper.
- `archive_usage` now raises on an unexpected RPC shape instead of returning
  `0/0` (an over-quota archive can no longer pass on a malformed response).
- `EvidenceService()` and `get_evidence_service()` are durable-only;
  `reset_evidence_service_for_tests()` uses the explicit memory double.

### M3 — atomic quota admission

- `backend/evidence/repository.py`
  - `create_run(run, max_active=..., now=...)` is atomic in both modes. Memory:
    one lock covers the expired sweep, the count and the insert. Durable: the
    opportunistic sweep (`delete_expired_runs`, unchanged) runs first, then
    `ai_evidence_create_run` locks the owner and performs count + insert in one
    transaction. A valid run is never evicted.
  - `archive_atomic(...)` is one operation: source lookup, same-hash dedupe,
    quota check, revision number and insert. Memory uses the same lock;
    durable calls `ai_evidence_archive_revision`.
- `supabase/migrations/20261116120000_create_evidence_foundation.sql`
  - `ai_evidence_create_run(...) RETURNS jsonb` and
    `ai_evidence_archive_revision(...) RETURNS jsonb`, both serialized with
    `pg_advisory_xact_lock(hashtextextended('ai_evidence:' || owner, 0))`.
    Limits are passed from `backend/evidence/constants.py`; no policy number is
    duplicated in SQL. `status` is `created`, `unchanged` or `quota`.
  - Both functions are revoked from `PUBLIC/anon/authenticated` and granted to
    `service_role` only.

### M4 — opt-in at the execution boundary

- `backend/evidence/service.py`: `run_cited_summary` requires `opt_in=True`;
  without it the call raises `EvidenceValidationError` before any history read,
  run creation or provider call.
- `backend/ai/tools/evidence.py`: `CitedSummaryTool` is
  `PermissionLevel.CONFIRMATION_REQUIRED` and `safe=False`, so `ToolExecutor`
  refuses to auto-execute it and only the Dispatcher's explicit confirmation
  round-trip executes the frozen call. It passes `opt_in=True` because the
  owner confirmed that exact call. Tool description updated accordingly.
- `backend/bot/handlers/history.py`: the panel passes `opt_in=bool(cited)` and
  clears `_STATE.cited` after launching, including the "Summarize Message Link"
  path, so a toggle never authorizes a later request.
- Privacy boundary unchanged: `ToolResult` still carries only the summary,
  opaque `run_id` and bounded counts; no message/chat/owner ids, links, hashes,
  excerpts or citation keys cross it, and the provider projection is unchanged.

### M6 — Explorer state

- `backend/bot/handlers/evidence.py`: removed `_runs_sync_cache`,
  `_run_bundle_cache`, `_suggestions_cache`, `_candidate_cache`,
  `_sources_cache`, `_source_cache` and `_revisions_cache` (including the
  `try/except NameError` reads and `globals()` writes). Views now take the
  bundle/source/revisions/suggestions/usage their handler just loaded. Every
  loader still resolves the opaque id through `EvidenceService`, which filters
  by `_OWNER_ID`; a foreign or deleted id fails closed.

### M5 — blocker recorded, no feature expansion

F1 has no dependent record class (`ai_workspace_knowledge` is G4-C), so there is
nothing a dependency check could find. The existing deletion operations are
single owner-scoped statements (`get_*` → `count_*` → `DELETE ... WHERE
owner_id = ? AND id = ?`) and are already atomic at the row level; the
`count_*_dependents` methods return `0` on the durable repository by design.
**Blocker:** before any dependent record exists, the dependency preflight and
the delete must move into one database function/transaction (taking the owner
lock) and return the dependent list for the confirmation. Until then, no
dependent deletion feature may be introduced.

## Migration changes

`supabase/migrations/20261116120000_create_evidence_foundation.sql` (still
**unapplied**, additive, no destructive statements):

- `ai_evidence_ref_revision_guard` trigger + the column-level
  `GRANT UPDATE (revision_id)` (M1).
- `ai_evidence_create_run(...)` and `ai_evidence_archive_revision(...)`, with
  `REVOKE`/`GRANT` to `service_role` (M3).
- Rollback block and table comments updated.
- No data migration, no drop, no truncate, no `DELETE FROM`.

## Tests

New file `tests/test_evidence_remediation.py` (17 tests):

- M1: exact link on create; changed content links a new revision without
  repointing old refs; missing/foreign/mismatched revision rejected; write-once.
- M2: memory requires explicit opt-in; unavailable Supabase raises instead of
  writing; the service never reports a run when storage is unavailable.
- M3: 12 concurrent run admissions against a cap of 5 → exactly 5 accepted;
  8 concurrent archives against a cap of 4 → exactly 4 written; 6 concurrent
  same-hash archives → one revision, five `unchanged`.
- M4: no opt-in refuses with no run and no provider call; opt-in does not carry
  into the next request; the tool is confirmation-required and not auto-safe.
- M6: no module-level result caches exist; views render only their own bundle;
  a foreign owner's run id and a missing source id are rejected; a stale
  callback after run deletion fails closed.

`tests/test_evidence_foundation.py` updated only where the new boundary applies
(`opt_in=True` on the four existing cited-summary tests, explicit memory
repository in the helper). `tests/test_tool_health_audit.py` records the new
confirmation level for `cited_summary`.

| Check | Result |
|---|---|
| `tests/test_evidence_foundation.py` + `tests/test_evidence_remediation.py` | **35 passed** |
| Registry/schema/confirmation/document group (9 files) | **288 passed** |
| Full suite (once, after all fixes) | **7947 passed, 27 skipped, 1 failed** |

The single full-suite failure is
`tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`.
It also fails in isolation and is unrelated to Evidence: the test fixes a task
`next_run_at` at 2026-09-10 and then asks the scheduler to run at
`datetime.now() + 60s` (November 2026), so the interval catch-up guard rejects
the advancement ("interval advancement exceeded bound") before the recovered
occurrence can be claimed. The previously reported
`test_40_usage_read_side.py::test_daily_usage_read` now passes. No test was
skipped, suppressed or weakened; the nine-file group above is green.

## Compilation and diff checks

- `python -m compileall -q` over every changed Python module and both Evidence
  test files: passed.
- `git diff --check`: passed.
- Final diff reviewed for scope: only the Evidence foundation, the Explorer, the
  History opt-in boundary, the migration, the new regression file, one registry
  expectation and the two canonical documents.

## Git delivery

- Commit: a single commit on top of `7f02e88`, pushed normally to `origin/main`.
- Remote verification and the exact SHA are stated in the delivery response;
  this committed file cannot contain the hash of the commit that contains it.
- Working tree clean after the commit; no rebase, force push or history rewrite.

## Not executed / not verified

- No SQL was executed, applied or rolled back; the live Supabase schema and the
  new functions, trigger, column grant, RLS and PostgREST RPC shapes are
  unverified against a real database.
- PostgreSQL concurrency, advisory locks, transaction isolation and the
  `jsonb` function results were not exercised; the concurrency tests prove the
  application-level serialization of the explicit memory double only.
- No live Telegram retrieval, Explorer render/callback, link opening, provider
  Cited Summary or Render/deployment operation was performed.
- `DATABASE_ARCHITECTURE.md` was not modified and still describes the pre-F1
  inventory (43 migrations on disk, 42 documented).

## Owner actions before use

1. Review the migration diff (three new SQL functions, one ref trigger, one
   column grant) and apply it manually through the normal Supabase process.
2. After application verify: the four tables and zero policies; the four
   functions executable by `service_role` only; the run-expiry and revision
   immutability triggers; the ref revision guard; `GRANT UPDATE (revision_id)`
   and no other `ai_evidence_refs` update privilege.
3. Run one controlled Cited Summary (with the confirmation), one archive
   confirmation, one re-archive of changed content, and one revision deletion.
4. Treat the runtime as **not live-enabled** until step 2 is complete.

## Deferred work (unchanged)

- **G4-A/F2:** restart-safe follow-ups (bounded to 20 references).
- **G4-B:** Workspace-aware durable evidence; blocked by G3-B.
- **G4-C:** strict-JSON extraction and the persistent Workspace Glossary
  (blocked by G3-B); its dependent records are the M5 blocker.
- **G4-D:** threshold-gated assessments; policy-gated.
- Cross-source intelligence, semantic search, knowledge graphs, purge and any
  new scheduler/worker remain unimplemented.
