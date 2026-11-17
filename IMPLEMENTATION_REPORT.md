# IMPLEMENTATION REPORT

**Reporting date:** 2026-11-16
**Latest delivery:** G4-A / F1 — Single-Source Evidence Foundation
**Base:** `c8741935dd10f09face8367b9eaa5810c0df34c0` (`origin/main`)
**Status:** Implemented offline, focused-tested and packaged with an **unapplied**
additive migration. The live Supabase schema has not been changed; no SQL,
Supabase, Telegram, Render, provider or deployment operation was performed by
this delivery. G4-A/F2 remains deferred.

## Implemented

- Durable **Active Evidence Runs** with bounded reference snapshots, an optional
  bounded summary, owner scope and a per-run TTL selected at creation (default
  7 days, 1–30 days). TTL is persisted and expiry is checked against
  authoritative database time (`ai_evidence_now()`), not a process clock.
  Expired runs immediately fail closed for retrieval/Explorer and are never
  reconstructed from Telegram, the archive, summaries or conversation history.
- **Opportunistic expired-run deletion only** inside an existing authorized
  admission (`create_run`). There is no scheduler, worker, cron, cleanup loop,
  tombstone or promised deadline. Rows may remain physically stored if no later
  authorized operation touches them; logical expiry still denies access.
- Active limits: 1,000-character excerpt, 50 refs/run, 20 refs/follow-up
  (reserved for F2), 32 KiB normalized excerpt text/run, 20 active runs/owner.
  A valid run is never evicted. At capacity, expired runs are removed first and
  an admission is rejected explicitly if capacity remains unavailable.
- **Cited Summary** opt-in from the History & Intelligence panel and from the
  explicit AI tool `cited_summary`. Ordinary `summarize_history` remains
  unchanged. The provider receives only opaque local keys (`E1…E50`) plus
  bounded excerpts; the summary is strict JSON (`summary` + `citations`) and any
  malformed, incomplete, extra-field, unknown-key or empty-citation output
  fails closed with no free-text/tagged fallback.
- **Owner-only Evidence Explorer** panel with active-run pages, locally
  constructed Telegram links, bounded excerpts and citation resolution. It does
  not call a provider, create a listener or create a second Telegram client.
- **Archive suggestions** only from refs reviewed in the current active run,
  maximum 10, each with a reason. Every archival requires explicit owner
  confirmation. No background or account-wide scanning exists.
- **Immutable archive**: stable source identity `(owner, peer, message_id)` and
  append-only revisions. Same content hash is a no-op; changed content returns
  an approval-required candidate and creates a new revision only after explicit
  approval. No mutable “latest” pointer and no archived-text rewrite on
  Telegram edits/deletes. Archive quotas are 5,000 revisions or 50 MiB per
  owner, whichever first; reaching one rejects the write without eviction.
- **Deletion**: revision/source deletion requires explicit confirmation;
  dependent records block it by default. A separate `cascade_confirmed`
  operation is required for dependents with an impact explanation. Active-run
  excerpt snapshots are not deleted by archive deletion and remain until their
  own TTL. No purge operation was invented.
- **Repository/service/tool/UI boundaries** preserve `RuntimeSupervisor`,
  `Dispatcher`, `ProviderManager`, `ToolRegistry`/`ToolExecutor`, the Telegram
  self-client boundary, the History retrieval service and the existing
  summarization engine. No second scheduler, executor, listener, update loop,
  Telegram client or evidence format was added.
- Scheduled History remains body-only and citation-free; it has no evidence
  path attached.

## Exact files changed

New implementation and test files:

- `backend/evidence/__init__.py`
- `backend/evidence/constants.py`
- `backend/evidence/links.py`
- `backend/evidence/models.py`
- `backend/evidence/repository.py`
- `backend/evidence/service.py`
- `backend/ai/tools/evidence.py`
- `backend/bot/handlers/evidence.py`
- `supabase/migrations/20261116120000_create_evidence_foundation.sql`
- `tests/test_evidence_foundation.py`

Existing files changed:

- `backend/ai/tools/registry.py` — registers `cited_summary` and
  `archive_evidence`.
- `backend/ai/tools/executor.py` — status labels only.
- `backend/bot/handlers/history.py` — Cited Summary toggle, creation-time TTL
  control, local citation rendering and Explorer entry point.
- `backend/bot/router.py` — registers the owner-only Evidence panel without a
  listener.
- `ROADMAP.md`, `LIFEOS_IDEAS.md` — actual F1 status and closed decisions.
- `IMPLEMENTATION_REPORT.md` — this current-state report.
- `tests/test_tool_health_audit.py`, `tests/test_tool_schema_contract.py`,
  `tests/test_capability_exposure_tools.py`, `tests/test_memory_tools.py` —
  registry count/permission expectations updated for the two new tools.
- `tests/test_canonical_schema_reconciliation.py`,
  `tests/test_database_architecture_doc.py`, `tests/test_database_setup_order.py`,
  `tests/test_private_message_db_security.py` — the migration inventory now
  accounts for the owner-authorized F1 artifact while
  `DATABASE_ARCHITECTURE.md` remains untouched (read-only) per the task.

## Migration artifact and schema status

- File:
  `supabase/migrations/20261116120000_create_evidence_foundation.sql`.
- **NOT applied.** No SQL was executed and no Supabase object was modified or
  created. The live schema does not yet contain these tables.
- The artifact creates:
  - `ai_evidence_runs`
  - `ai_evidence_refs`
  - `ai_evidence_sources`
  - `ai_evidence_revisions`
  - `ai_evidence_now()` (authoritative database time)
  - `ai_evidence_archive_usage(bigint)` (owner-scoped count/byte usage)
  - run-expiry trigger and revision immutability trigger
- Schema conventions: additive/idempotent, RLS enabled with zero policies,
  PUBLIC/anon/authenticated privileges revoked, service-role-only grants,
  no owner foreign key, append-only refs/revisions (no UPDATE grants and an
  UPDATE-blocking revision trigger), database-time `expires_at` trigger,
  `NOTIFY pgrst`, verification selects.
- `DATABASE_ARCHITECTURE.md` was **not modified**.

## Security and privacy boundaries

- The Evidence Envelope does not enter generic `ToolResult.data`/`message`,
  tool arguments, `ai_tool_history`, audit records, `ai_messages`, conversation
  or continuation prompts, fallback rendering or PromptBuilder tool history.
  `cited_summary` returns a generated summary plus only opaque `run_id` and
  bounded requested/processed/ref/citation counts; `archive_evidence` takes
  only an opaque run id and an ordinal ref and returns no source/revision ids,
  hashes, links or message ids.
- Provider projections contain only `E1…E50` and bounded excerpts. Telegram
  message ids, sender ids, owner ids, Workspace ids, chat names, URLs, content
  hashes and unrelated conversation context are not sent.
- Telegram links are constructed locally from validated stored `peer_id` and
  `message_id`; provider output never determines a link, source id, revision id,
  count, existence, archival, deletion or retention decision.
- No Evidence tool sends a Telegram message. The panel renders through the
  existing Helper Bot panel infrastructure; no new listener/client exists.
- Archive revisions are append-only and owner-approved; same-hash is a no-op;
  changed content requires approval; archive deletion does not mutate active-run
  snapshots.

## Tests actually executed

Environment: Python 3.12.3, isolated `.venv/g4a-env` with
`backend/requirements.txt`, `pytest` and `pytest-asyncio`. No live services.

| Check | Result |
|---|---|
| `tests/test_evidence_foundation.py` | 18 passed |
| Focused Evidence + History/AI/Scheduled History suites (`test_evidence_foundation`, `test_history_service`, `test_history_panel`, `test_history_ai_tools`, `test_history_schedules`) | **214 passed** |
| Registry/schema/tool exposure tests (`test_tool_health_audit`, `test_tool_schema_contract`, `test_capability_exposure_tools`, `test_memory_tools`) | **111 passed** |
| F1 migration/document inventory exceptions | **15 passed** after accounting for the authorized, unapplied F1 artifact |
| Full suite (final run) | **7929 passed, 27 skipped, 2 failed in 150.72s**. Both failures were reproduced unchanged on base `c874193` and are pre-existing/time-dependent: `test_40_usage_read_side.py::test_daily_usage_read` and `test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`. No F1 test or History/AI test failed. The F1 migration/document inventory tests pass by explicitly accounting for the authorized, unapplied F1 artifact while keeping `DATABASE_ARCHITECTURE.md` read-only. |

The focused tests are unit/mocked-repository tests. No live Supabase, Telegram,
Render or provider integration test was run.

## Compilation and diff checks

- `python -m compileall -q` over the new/changed Python modules: passed.
- `python -m py_compile` over the new/changed modules: passed.
- `git diff --check`: passed (no whitespace errors).
- The full suite must be re-run after the final doc/test edits; see the final
  delivery report for the post-edit result.

## Commit and remote delivery

- Branch: `main`.
- Commit SHA: recorded in the final delivery message (this report is committed
  in the same commit it describes).
- Remote: to be verified after the normal authenticated push.
- Working tree: to be reported after the final check.

## Not verified live

- No SQL was applied; the live schema, RLS, triggers, functions and grants are
  unverified against Supabase.
- No live Telegram retrieval, panel render/callback, self-client operation or
  link opening was performed.
- No live provider Cited Summary call was made.
- No Render/deployment, background job or scheduler was run.
- No archive or deletion operation was performed against real data.

## Deferred work

- **G4-A/F2:** restart-safe follow-ups on the same run/contract, bounded to 20
  references; not bundled into F1.
- **G4-B:** Workspace-aware durable evidence; blocked by G3-B and the real
  Workspace model.
- **G4-C:** strict-JSON extraction and persistent Workspace Glossary; glossary
  storage blocked until G3-B.
- **G4-D:** descriptive/threshold-gated subjective assessments; policy-gated.
- Cross-source intelligence, semantic search, knowledge graphs, a purge
  operation and any new scheduler/worker are not implemented.

## Exact next action required from the owner

1. Review
   `supabase/migrations/20261116120000_create_evidence_foundation.sql`.
2. Apply it manually to the intended Supabase project through the owner’s normal
   SQL-editor/manual migration process (the agent must not execute it).
3. After application, verify the four tables, RLS/zero policies, service-role
   grants, `ai_evidence_now()`, `ai_evidence_archive_usage(bigint)`, the run
   expiry trigger and the revision immutability trigger.
4. Then exercise one owner-only Cited Summary and one archive confirmation in a
   controlled Telegram session. Until step 2 is complete, the runtime should be
   treated as **not live-enabled** even though the code and tests are delivered.
