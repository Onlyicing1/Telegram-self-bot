# IMPLEMENTATION REPORT

**Reporting date:** 2026-11-17
**Latest delivery:** G4-A / F1 — Single-Source Evidence Foundation, remediation sprint
**Base commit:** `7f02e88c469dfd569eaa43c5005964dc20021a89` (`origin/main`, parent `c8741935dd10f09face8367b9eaa5810c0df34c0`)
**Follow-up base:** `625bcf053fdb0faf8bc95e239302e9bc4d18aa11` (the first remediation commit; the Ref/Run ownership fix is committed on top of it)
**Starting HEAD:** `76aacc851483dc2ade1d2dab46cd7e2e5a714743` (docs branch, an ancestor of the target)
**Final commit:** recorded in the delivery response; this report is committed in the same commit it describes.
**Status:** G4-A/F1 database schema and security hardening migrations were applied
to the live Supabase project on 2026-11-17 and verified against PostgreSQL 17.6.
The Evidence runtime is still **not live-enabled** until the application deployment
and end-to-end service-role/provider flow are verified. No Telegram or Render
operation was performed by the database setup.

## Disposition of the six findings

| # | Finding | Disposition | Evidence |
|---|---|---|---|
| M1 | A ref did not unambiguously identify the immutable revision it produced | **FIXED** | `ai_evidence_refs.revision_id` was written by no code path. `archive_ref` now links the ref to the exact revision (created or deduplicated), validated against owner/chat/message and write-once, at both the application and database boundary. |
| M2 | Persistence failure could silently degrade to process memory | **FIXED** | `EvidenceRepository()` no longer falls back to memory when Supabase is unavailable; it holds an explicit unavailable client that raises `EvidenceStorageUnavailable`. Memory is only reachable through `allow_memory=True`/`in_memory_repository()`. |
| M3 | Quota checks could race and exceed owner limits | **FIXED (application + SQL functions)** | Run admission and archive writes are one serialized operation: an in-memory lock for the explicit test double and `pg_advisory_xact_lock` inside `ai_evidence_create_run` / `ai_evidence_archive_revision` for Supabase. Sequential live PostgreSQL quota/archive RPC probes passed; production concurrency and lock-contention stress testing remain unverified. |
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
- `supabase/migrations/20261117123943_create_evidence_foundation.sql`
  - `ai_evidence_ref_revision_guard` trigger: rejects any ref update except one
    `NULL → revision` transition; rejects a second link, a different link, a
    missing revision, a foreign owner, or a source/message mismatch.
  - `GRANT UPDATE (revision_id) ON ai_evidence_refs TO service_role` — the only
    update the table accepts (no table-level UPDATE grant).
  - No foreign key on `revision_id` by design: the approved deletion policy
    keeps a historical unresolved reference in the ref after an explicitly
    confirmed archive deletion, instead of nulling or cascading it.

### Ref/Run ownership integrity (follow-up, 2026-11-17)

**Confirmed gap.** `ai_evidence_ref_revision_guard` ran `BEFORE UPDATE` only and
selected the run's `peer_id` without ever comparing `run.owner_id` to
`NEW.owner_id`; it also returned early when `revision_id IS NULL`, so an
unlinked ref was never checked against its run at all. On the application side
the explicit memory double checked `ref.run_id`/`ref.owner_id`, but the durable
`add_refs()` built insert rows and called Supabase with no equivalent check. A
ref could therefore name an owner different from its run's owner — from a
service-role write directly, or from any future durable caller that did not
reuse `archive_ref`'s construction path. The trigger's revision branch required
the revision owner to equal `NEW.owner_id`, so a foreign ref could still only
link a revision owned by that foreign ref owner; the inconsistency was the
ref/run pair itself, not the revision.

**Database fix.** The guard is now `BEFORE INSERT OR UPDATE ON ai_evidence_refs`.
For both operations it selects `run.owner_id, run.peer_id` by `NEW.run_id`,
raises `evidence ref run does not exist` when absent, and raises
`evidence ref owner does not match its run owner` when the owners differ,
*before* the `NULL → revision` early return. All previous checks remain in the
same guard: the UPDATE-only append-only column set, the write-once rule
(`OLD.revision_id IS NOT NULL` and any different value), revision existence,
`revision_owner = NEW.owner_id`, `revision_peer = run.peer_id`, and
`revision_message = NEW.message_id`. No foreign key was added; the approved
deletion policy (a historical unresolved ref may remain after archive deletion)
is unchanged, and the revision ordering/immutability triggers are untouched.

**Repository fix.** `_validate_refs_for_run(run, refs)` rejects any ref whose
`run_id` differs from the run or whose `owner_id` differs from the run owner.
`add_refs()` calls it before either branch, so the durable path raises
`EvidenceStorageError` before issuing an insert, and the memory path applies the
same invariant. The database trigger remains the authority for direct
authorized writes.

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
- `supabase/migrations/20261117123943_create_evidence_foundation.sql`
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

`supabase/migrations/20261117123943_create_evidence_foundation.sql` (applied
to the live project; additive, no destructive statements):

- Four Evidence tables, owner/run/source/revision constraints, indexes, RLS enabled
  with zero policies, expiry and immutability triggers.
- `ai_evidence_ref_revision_guard` runs on `BEFORE INSERT OR UPDATE` and
  enforces Ref/Run owner equality plus exact revision/source consistency.
- `ai_evidence_create_run(...)` and `ai_evidence_archive_revision(...)` use
  owner-scoped advisory transaction locks for quota admission and archive writes.
- Initial grants were subsequently tightened by the hardening migration below.

`supabase/migrations/20261117124032_harden_evidence_privileges.sql` (applied):

- Revokes inherited/default table-level privileges from `service_role` on all
  four Evidence tables, then grants the exact approved table privileges.
- `ai_evidence_refs`: SELECT/INSERT/DELETE plus UPDATE on `revision_id` only.
  No table-level UPDATE and no UPDATE on any other ref column.
- `ai_evidence_revisions`: SELECT/INSERT/DELETE; no UPDATE.
- Revokes PUBLIC/anon/authenticated execution on trigger functions and grants
  execution to `service_role`.
- Pins all seven Evidence functions to `search_path = pg_catalog, public`.
- No data migration, drop, truncate or destructive row operation.

## Tests

New file `tests/test_evidence_remediation.py` (21 tests):

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
- Ref/Run ownership: a ref whose owner differs from its run owner is rejected
  by the memory repository; the durable insert path raises before touching the
  database (an exploding client proves no call); a valid same-owner ref is
  inserted with the correct `owner_id`/`run_id`; a static check of the migration
  asserts the guard runs on `INSERT OR UPDATE`, compares the run owner, keeps
  the write-once/revision/source-message checks, and adds no FK on
  `revision_id`.

`tests/test_evidence_foundation.py` updated only where the new boundary applies
(`opt_in=True` on the four existing cited-summary tests, explicit memory
repository in the helper). `tests/test_tool_health_audit.py` records the new
confirmation level for `cited_summary`.

| Check | Result |
|---|---|
| `tests/test_evidence_foundation.py` + `tests/test_evidence_remediation.py` (first remediation) | **35 passed** |
| Same two files after the ownership fix | **39 passed** |
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

## Live Supabase application and verification (2026-11-17)

Project: `telegram-self-bot` (`burfebhahuowywyfgcsc`), PostgreSQL 17.6,
region `ca-central-1`, status `ACTIVE_HEALTHY`.

Applied migration history:

- `20261117123943 / create_evidence_foundation`
- `20261117124032 / harden_evidence_privileges`

The Supabase MCP generated server-side migration timestamps. The two new
Evidence migration filenames were synchronized to those exact remote versions.
This does not reconcile the broader pre-existing migration-history drift:
the repository has 44 SQL migration files, while remote Supabase history has
8 rows; 39 local timestamps are absent remotely and 3 remote versions have no
matching local filename. This was not repaired because the older migration
states require a separate audit. Do not run bulk `supabase db push` until that
history drift is reconciled.

**Live catalog verification passed:**

- All four Evidence tables exist and have RLS enabled.
- There are zero RLS policies on the four Evidence tables, as designed for
  backend/service-role-only access.
- All three expected triggers exist and are enabled:
  run expiry, ref revision guard (INSERT OR UPDATE), and revision immutability.
- `anon`, `authenticated`, and `PUBLIC` have no Evidence table read access.
- `service_role` has SELECT/INSERT/UPDATE/DELETE on runs and sources;
  SELECT/INSERT/DELETE on refs plus UPDATE on `revision_id` only; and
  SELECT/INSERT/DELETE on revisions with no UPDATE.
- All seven Evidence functions are executable by `service_role` only;
  PUBLIC/anon/authenticated execution is revoked. All seven have the fixed
  `search_path = pg_catalog, public`.
- The security advisor no longer reports mutable search paths for Evidence
  functions. The remaining `pg_trgm`-in-public warning predates this work.
  RLS-enabled/no-policy INFO notices for Evidence are intentional.

**Live PostgreSQL probes passed; all probe rows were cleaned up:**

- Run expiry is derived from database time and the persisted TTL.
- A ref with a mismatched owner/run pair is rejected by the actual PL/pgSQL
  INSERT trigger; a same-owner ref is accepted.
- Exact revision linking succeeds once; repointing is rejected.
- Ref excerpt mutation and archived revision mutation are rejected.
- `ai_evidence_create_run` returns `created` and enforces the active-run quota.
- `ai_evidence_archive_revision` creates the first revision, deduplicates an
  identical content hash, and creates revision 2 for changed content.

**Hardening discovered during live verification:** the project had broad
pre-existing default privileges for `service_role`. The original migration's
positive GRANT statements did not remove those inherited table-level privileges.
The follow-up hardening migration explicitly revoked all privileges on the four
Evidence tables from `service_role` before granting the approved exact set.
This removed table-level UPDATE on refs and revisions; catalog checks confirm
only `revision_id` remains updatable on refs.

## Remaining unverified / not live-enabled

- The SQL probes ran against the live PostgreSQL database, but they did not
  exercise the bot's actual Supabase client credentials or PostgREST round-trip.
- Concurrent production traffic and PostgreSQL lock behavior were not stress-tested;
  the RPC smoke tests verified quota/dedupe behavior sequentially.
- No live Telegram retrieval, Evidence Explorer UI/callback, Cited Summary
  provider round-trip, or Render deployment was performed.
- `DATABASE_ARCHITECTURE.md` was not modified and still describes the pre-F1
  inventory (43 migrations on disk, 42 documented).

## Remaining owner/runtime actions

1. Deploy the code currently on `main` to the runtime environment.
2. Verify the bot uses the intended Supabase project and service-role credentials
   without exposing the secret to clients.
3. Run a controlled Cited Summary with explicit confirmation, one archive
   approval, a changed-content re-archive, and a revision deletion through the
   application—not direct SQL.
4. Keep Evidence marked **not live-enabled** until those application-level
   checks pass. M5 remains deferred until dependent records are introduced.

## Deferred work (unchanged)

- **G4-A/F2:** restart-safe follow-ups (bounded to 20 references).
- **G4-B:** Workspace-aware durable evidence; blocked by G3-B.
- **G4-C:** strict-JSON extraction and the persistent Workspace Glossary
  (blocked by G3-B); its dependent records are the M5 blocker.
- **G4-D:** threshold-gated assessments; policy-gated.
- Cross-source intelligence, semantic search, knowledge graphs, purge and any
  new scheduler/worker remain unimplemented.
