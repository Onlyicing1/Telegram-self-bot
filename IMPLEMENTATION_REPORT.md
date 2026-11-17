# IMPLEMENTATION REPORT

**Reporting date:** 2026-11-18
**Delivery:** G4-A / F2 — Evidence Delivery Readiness (application layer)
**Base commit:** `6ac156eb34d284eccaaaa031e15454357459e8d1` (verified equal to
`origin/main` by `git fetch` + `git rev-parse origin/main` + ancestry check)
**Status:** **Delivery-layer defects fixed and mock-verified. Evidence is still
NOT live-enabled.** No Telegram, provider, Render or Supabase operation was
performed for this delivery, and no SQL was executed against the live database.

---

## 1. Starting state (verified against source, not against the previous report)

| Item | Verified state |
|---|---|
| Branch / HEAD | `freebuff/4c6cc6c006eef5ad6cf723c4` at `6ac156e` |
| `origin/main` | `6ac156eb34d284eccaaaa031e15454357459e8d1` — identical to HEAD; the documented commit is present |
| Working tree | clean before editing |
| Evidence migrations | `20261117123943_create_evidence_foundation.sql`, `20261117124032_harden_evidence_privileges.sql` (44 files in `supabase/migrations/`) |
| F1 implementation | `backend/evidence/{models,constants,links,repository,service}.py`, `backend/bot/handlers/evidence.py`, `backend/ai/tools/evidence.py`, History-panel Cited Summary in `backend/bot/handlers/history.py` |
| Existing Evidence tests | 39 tests: **38 passed, 1 failed** |

**The one failing test at HEAD** was
`test_evidence_remediation::test_migration_guard_covers_insert_and_checks_the_run_owner`.
It read `supabase/migrations/20261116120000_create_evidence_foundation.sql`, a
filename that the migration reconciliation renamed to
`20261117123943_create_evidence_foundation.sql`. The test asserted the correct
database guarantee; only its path was stale. The file has been corrected and the
assertions are unchanged.

**The Explorer shipped broken in four independent ways.** None of them was
visible to the existing tests, because no test rendered the confirmation,
success or deletion surfaces:

1. `_confirm_view` built its confirm button with an undefined name (`key`), so
   the archive-confirmation panel raised `NameError`. That panel is the only
   Explorer path that can create an archive revision.
2. Every success notice used `sym.STATE_SUCCESS`, which does not exist in the
   symbol registry. The durable write succeeded and then the notice raised
   `AttributeError`, so a completed archive or deletion was reported as a
   failure panel.
3. The source-delete and revision-delete callback payloads were 66–107 bytes.
   Telegram's `callback_data` limit is 64 bytes and
   `helper.context.truncate_callback_data` silently truncates longer values, so
   the opaque ids in those buttons arrived cut and could never resolve.
4. `observed_state` could never be revalidated: `update_source_observation`
   existed but had no caller, so the approved `live`/`edited`/`deleted`/
   `inaccessible` states and "exact-source revalidation" were unreachable.

## 2. What was verified and needed no change

- **Single runtime authority.** `RuntimeSupervisor` and the dispatcher are
  untouched. `backend/bot/router.py` still registers `evidence.register(client,
  owner_id)`; that function registers one panel and its actions and adds no
  client, listener, scheduler, executor or update loop.
- **Tool path.** `cited_summary` and `archive_evidence` reach
  `ToolRegistry` → `ToolExecutor` only, both at
  `PermissionLevel.CONFIRMATION_REQUIRED` (never auto-executed), both thin
  shims over `get_evidence_service()`.
- **Owner/session/chat scope.** Every run, source and revision read is
  owner-filtered in the repository; a ref must match its run's owner and the
  run's peer/message (application check plus the `BEFORE INSERT OR UPDATE`
  guard). Expiry is compared with `ai_evidence_now()` (database time), never a
  process clock.
- **Fail closed.** `EvidenceRepository()` installs `_UnavailableClient` when
  Supabase is missing; every durable call raises
  `EvidenceStorageUnavailable`. Memory mode is reachable only through
  `allow_memory=True`/`in_memory_repository()`.
- **Cited Summary consent.** `run_cited_summary` refuses without
  `opt_in=True` before any history read, run creation or provider call. The
  History panel's toggle is consumed per run (cleared after the Run action, the
  link path and on a new chat selection); the AI tool passes consent only for
  the single confirmed call.
- **Structured output.** `_extract_json` accepts strict JSON only (no fence
  stripping, no prose, exactly `summary` + `citations`), rejects an unknown or
  fabricated key against the run's own refs, and deletes the half-built run on
  every parse/validation/provider failure.
- **Provider projection.** The prompt contains the bounded excerpts, opaque
  `E<n>` keys and the owner's focus only. Tests assert the chat id, owner id,
  message ids, `t.me/`/`tg://` and the peer name never appear.
- **Links.** `backend/evidence/links.py` is the only link builder; provider
  output is never a source of a URL or identifier.
- **Ordinary summaries.** The non-cited panel path still calls
  `history_ai_service.summarize_history` unchanged.
- **Quotas.** Run admission and archive writes are one serialized operation
  (`ai_evidence_create_run` / `ai_evidence_archive_revision` with
  `pg_advisory_xact_lock`; one lock in the test double). Quota refuses; nothing
  valid is evicted. Revisions have no UPDATE grant and an immutability trigger;
  refs may only take one write-once `revision_id` link.
- **Privacy isolation.** Media Analysis and Saved Items are untouched, and no
  Evidence record is written into `ToolResult.data`, tool arguments,
  `ai_tool_history`, conversation history or `EngineResult.metadata`.

## 3. Fixes in this delivery

| # | Defect | Fix |
|---|---|---|
| D1 | `_confirm_view` raised `NameError` (`key` undefined) | Uses `candidate.key`; covered by a render + payload test |
| D2 | `sym.STATE_SUCCESS` does not exist; every success notice crashed after the write | Uses `sym.STATE_COMPLETED`, the registry's success glyph, as every other handler does; the commit/delete/re-check paths are now driven end-to-end in tests |
| D3 | Delete callbacks exceeded Telegram's 64-byte limit and were silently truncated | Compact action ids (`evidence_delsrc`, `evidence_delsrc_ok`, `evidence_delsrc_all`, `evidence_delrev`, `evidence_delrev_ok`, `evidence_delrev_all`); revision callbacks carry the opaque revision id and re-resolve their source through the owner-scoped service. A test walks every rendered button and asserts ≤64 UTF-8 bytes and no truncated opaque id |
| D4 | No exact-source revalidation existed | `EvidenceService.revalidate_source` reads the exact message through the existing `telegram_api.messages.get_message` boundary and records `live`/`edited`/`deleted`/`inaccessible` (hash comparison against the stored revisions). It never rewrites archived text, hash or revision number. Explorer gains a "Re-check source" action |
| D5 | `archive_ref(approved=False)` could still write the ref's revision link before raising, contradicting its documented contract | Consent is checked before any write; a refused request writes nothing |
| D6 | `update_source_observation` could not tell a failed durable write from a success | Returns the stored row, validates the state value, and raises `EvidenceStorageError` when no row was written |
| D7 | The archive list was silently capped at 20 sources with no pagination | Offset pagination (10 per page, one look-ahead row, no cached page state) and revision pagination (10 per page) |
| D8 | Stale migration filename in the migration-contract test | Path corrected; assertions unchanged |
| D9 | The Explorer's gate bypassed the canonical guard and allowed everyone when `_OWNER_ID` was 0 | Uses `backend.bot.handlers.guard.is_owner`, like every other handler |

**No schema change, no new migration, no SQL executed, no
`DATABASE_ARCHITECTURE.md` change, no migration-history reconciliation.**

## 4. Tests actually executed

All commands ran with a local virtualenv created from
`backend/requirements.txt` (Python 3.12.3, pytest 9.2.0). No live service was
contacted by any test.

| Command | Result |
|---|---|
| `python -m pytest tests/test_evidence_foundation.py tests/test_evidence_remediation.py tests/test_evidence_delivery.py -q` | **51 passed** (18 + 21 + 12) |
| `python -m pytest tests/ -q -k "evidence or history or panel or registry or symbol or tool"` | **1430 passed, 4 skipped, 6557 deselected** |
| `python -m pytest tests/ -q` | **7960 passed, 27 skipped, 4 failed** in 150s |
| `python -m compileall -q backend tests` | exit 0 |
| `git diff --check` | exit 0 (no whitespace errors) |

`tests/test_evidence_delivery.py` (new, 12 tests) covers: every Explorer view
renders and every button fits the 64-byte callback limit with no truncated
opaque id; the confirm payloads name the exact run and key; archive/revision
pagination navigation; the canonical owner gate; revalidation for all four
states with the archived revision proven unchanged; owner-scoped revalidation
and the no-Telegram case; the re-check action; the commit/source-delete/
revision-delete success paths; unapproved archiving writing neither revision nor
ref link; bounded archive paging; and the durable RPC contract.

**The four full-suite failures are pre-existing and unrelated.** They reproduce
identically on a pristine `git archive HEAD` checkout of `6ac156e`:

- `test_database_architecture_doc::test_every_migration_file_is_classified_in_the_audit_section`
- `test_database_setup_order::test_the_audited_order_is_recorded_in_the_audit_table`
- `test_private_message_db_security::test_the_documented_counts_match_the_repository`

These three are documentation-inventory assertions that fail because
`DATABASE_ARCHITECTURE.md` was not updated when the two Evidence migrations were
added (it still records 42 migrations; the repository has 44). This task
explicitly forbids modifying that document, so they are reported, not fixed.

- `test_task_scheduler::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`
  is the known time-dependent failure (its fixed 2026-09-10 schedule has passed
  the wall clock).

## 5. Tests not executed

- **Nothing ran against a live Supabase/PostgREST instance in this delivery.**
  The durable-path tests use a recording stand-in and compare the repository's
  RPC argument maps against the migration's SQL signatures; they do not execute
  the SQL functions, advisory locks, grants, RLS or triggers.
- No Telegram client, provider round-trip, Render deployment, restart or
  real-user callback was exercised.
- No concurrency stress test ran against PostgreSQL. The concurrent-quota and
  concurrent-dedupe tests use the in-memory double's single lock; they are not
  evidence of database-level concurrency safety.

## 6. Database status

The two Evidence migrations remain applied to the live project
(`burfebhahuowywyfgcsc`) and were verified on 2026-11-17: four tables, RLS with
zero policies, no client-role access, exact service-role grants, restricted
function execution with fixed `search_path`, owner/run consistency, revision
immutability and sequential RPC smoke tests. This delivery changed no database
object and executed no SQL.

**No schema defect was found, so no new migration is required.**

**Historical migration drift is unchanged:** 44 repository files vs 8 remote
history rows; 39 local timestamps absent remotely and 3 remote versions without
a matching local filename. Do not run a bulk `supabase db push`.

## 7. Render and runtime status

Not deployed. `render.yaml` and `Procfile` still start `python -m backend.main`
on Python 3.11 with the health check at `/health`. The Evidence runtime needs no
new environment variable: it reads only the existing `SUPABASE_URL`,
`SUPABASE_SERVICE_ROLE_KEY` and `BOT_OWNER_ID`. No variable value was read or
printed.

**Remaining owner-operated steps (not performed here):**

1. Deploy the current `main` to Render and confirm `/health` and a normal
   `Menu` panel.
2. Confirm the deployed process reached the live Supabase project (a run creation
   succeeding is the signal; the in-memory fallback does not exist for Evidence).
3. From History: select a chat, enable Cited Summary, run it, and confirm one
   active run appears in the Explorer with citations.
4. In the Explorer: open the run, confirm one archive suggestion, verify the
   revision appears, press "Re-check source", then edit and delete the source
   message in Telegram and re-check again (`edited` then `deleted`).
5. Delete a revision and a source through the confirmation panels and confirm
   the reported impact matches what was removed.
6. Confirm an ordinary (non-cited) History summary is unchanged.
7. Restart the service and confirm the active run is still listed and still
   expires on database time.

## 8. Remaining blockers and known limitations

1. **Runtime is unverified.** Every G4-A delivery claim in this report is
   application-level and mock-verified only.
2. **M5 (dependent-record deletion) remains deferred.** F1 has no dependent
   record class; `count_*_dependents` are explicit zero stubs, so a cascade
   confirmation cannot yet remove anything. Before any dependent table exists
   (G4-C), the preflight and the delete must become one database transaction
   that returns the dependent list. The durable path therefore cannot distinguish
   "no dependents" from "not implemented".
3. **The per-run 32 KiB text limit refuses rather than shrinking the window.**
   Fifty long messages can exceed it; the owner must lower the requested count.
   The error is honest but does not propose a window.
4. **Concurrency safety is verified only up to the SQL design.** The advisory
   lock, the single transaction and the unique constraints are inspected and
   applied, but no live concurrent probe was run.
5. **The three DATABASE_ARCHITECTURE.md inventory failures** above stay until
   that document is updated by an authorized change.

## 9. Deferred work (explicitly out of scope)

- **G4-A/F2 as defined in `ROADMAP.md`** — restart-safe follow-ups on the same
  run and contract — remains **not implemented**. This delivery is the G4-A
  delivery-readiness pass; it does not add follow-ups.
- **G4-B** Workspace-aware durable Evidence — deferred (needs G3-A/G3-B).
- **G4-C** strict-JSON extraction and the persistent Workspace Glossary —
  deferred; the Glossary is blocked until G3-B.
- **G4-D** descriptive and threshold-gated subjective assessments — deferred;
  no threshold or person-level policy is approved.
- Scheduled History citations, cross-chat/cross-Workspace browsing, retention or
  quota changes, and new AI retrieval architecture — not started, by instruction.

## 10. Discrepancies recorded

- The transferred context described the transferred Evidence snapshot as
  complete except for runtime verification. Source review found the four
  Explorer defects in §1; the prior report's statement that the Explorer was
  "fixed" (M6) referred only to removing module-global caches.
- `ROADMAP.md` defines **G4-A/F2** as restart-safe follow-ups. The task named
  this delivery "G4-A/F2 — Evidence Delivery Readiness". Both facts are recorded
  rather than reconciled by rewriting the roadmap identity: the roadmap row is
  unchanged and still deferred, and this work is documented as the G4-A
  delivery-readiness pass.
- The prior report claimed 35 Evidence tests in the remediation sprint; the
  repository contains 39 at `6ac156e` (38 passing). It now contains 51.
