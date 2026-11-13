# IMPLEMENTATION REPORT — History Phase 3: durable state investigation and design

## 1. Delivery status

**This was investigation and design only.** It did not implement durable History,
scheduled History, a repository, a migration or any runtime behavior. It did not
modify production Python, tests, database migrations or protected architecture
documents.

Explicitly:

- **No production Python code was changed.**
- **No tests were changed.**
- **No migration was created.**
- **No SQL was executed.**
- **No Supabase project was contacted or modified.**
- **No live Telegram or AI-provider behavior was tested.**
- **No table was created or altered.**
- **Scheduled History is not implemented.**
- **The oldest-first retrieval defect was not fixed.**

The source-backed deliverable is `INVESTIGATION.md`; `ROADMAP.md` was updated and
this report was replaced.

## 2. Verified base and repository state

| Item | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Branch | `freebuff/peer-classification` (workspace branch) |
| Local HEAD before edits | `62a18de07f1bc5bf8b51c36438f66ad8c4f4d498` (Phase 2) |
| Freshly fetched `FETCH_HEAD` | `62a18de07f1bc5bf8b51c36438f66ad8c4f4d498` |
| Phase 1 ancestry | `ffe72fd0bce9286e3d3d6583614b3b243bfd4fdf` is an ancestor |
| Working tree before edits | clean |
| Live deployment | none performed |

The Phase 2 report was treated as context only; the current source was inspected
directly.

## 3. Source findings that drove the design

- The History path is completely one-shot. `history_service` retrieves and
  normalizes; `history_ai_service` chunks and calls `ProviderManager`; the handler
  edits the original panel message. It never writes a database row, cursor or
  summary.
- A delivered summary exists only as Telegram panel text. There is no application
  state to read it back, continue from it, or retain it if delivery fails.
- `_STATE` (selected chat/count/focus/peer type) and result-page state are
  in-memory; a restart loses all of them.
- No History table or repository exists in source, migrations or tests.
- Message ids are retrieval bounds, translation keys and display metadata; they are
  not durable content storage. Deleted messages simply disappear from later reads.
- `ai_task_occurrences.result_metadata` is 8 KiB
  (`ai_task_occurrences_result_size`) and is occurrence-lifecycle-owned;
  `MAX_METADATA_BYTES = 8192`. It is not a cumulative summary store.
- `TaskExecutionCoordinator` sets `extra["chat_id"]` from
  `notification_destination.chat_id`. That is the delivery destination, not an
  authoritative History source.
- The current collector (`history_service._collect`) scans newest→oldest and keeps
  the newest `target` after trimming (`collected[:target]`), then reverses. It can
  omit older eligible messages after a cursor. This is the known defect and remains
  **unfixed**.
- Classification (`history_service.classify_chat` → Phase 1 `classify_entity`) is
  live and performed through the existing entity resolution boundary. No stored
  type can be authoritative.
- The existing task repository uses CAS (`.eq("version", …)` or
  `.eq("status", …)`) and has an in-memory fallback. A durable History checkpoint
  must not use that fallback, because a RAM cursor would be lost on restart and
  could permanently skip content.
- The repository has 41 migration files (40 schema + the privilege-hardening
  migration); the latest is
  `20261112160000_add_panel_settings_intelligent_reaction_probability.sql`.
  `DATABASE_ARCHITECTURE.md` is the schema authority, and the suite requires every
  migration's table to be classified and §31.3 to match the canonical setup.
  This is why the design precedes any migration.

## 4. Design deliverable

`INVESTIGATION.md` contains the complete design:

1. current architecture/data flow and exact bounds;
2. persistence limitations (`ai_tasks`, occurrences, destination);
3. proposed `history_schedules` table;
4. field-by-field justification;
5. repository API and method contracts;
6. owner scoping/RLS/grants;
7. atomic summary + checkpoint commit;
8. CAS/version semantics;
9. restart/failure recovery;
10. deleted/inaccessible message behavior;
11. retention and 32 KiB compaction policy;
12. AI context isolation;
13. oldest-first retrieval prerequisite and required contract;
14. migration strategy and verified numbering;
15. exact implementation sequence;
16. required tests;
17. open decisions and evidence gaps.

Core proposed shape (not applied): one `history_schedules` row per History task and
per `(owner_id, peer_id)`, linked to `ai_tasks(id) ON DELETE CASCADE`, with
baseline/scanned/summarized message ids, a bounded cumulative summary, summary
revision, status, bounded diagnostics, and a CAS `version`. The summary and both
cursors commit in one CAS update; the previous summary is never erased by a
deleted source message; 32 KiB is a hard, enforced ceiling with no silent
truncation. The table is proposed as service-role-only RLS because it contains
private chat content.

**No proposed SQL was executed, and it is marked in `INVESTIGATION.md` as
unapproved design material.**

## 5. Files changed

| File | Change |
|---|---|
| `INVESTIGATION.md` | replaced with the Phase 3 source-backed investigation/design |
| `ROADMAP.md` | Phase 3 investigation marked complete; implementation next; unresolved retrieval prerequisite recorded |
| `IMPLEMENTATION_REPORT.md` | replaced with this truthful investigation-only report |

No other file was modified. `DATABASE_ARCHITECTURE.md`, `AI_MASTER_DESIGN.md`,
production code, tests and `supabase/migrations/` were untouched.

## 6. Validation performed

Because this is documentation-only, no code tests were fabricated or run.

| Check | Result |
|---|---|
| `git status --short` before edits | clean; Phase 2 HEAD verified |
| `cloud_git fetch` | `FETCH_HEAD == 62a18de…` |
| Source review | History service/AI service/policy, Telegram facade, handler, task schema/repository, scheduler/coordinator, provenance, migration files and relevant `DATABASE_ARCHITECTURE.md` sections |
| Migration count/numbering | 41 files; latest `20261112160000`; no `20261113*`; no History table |
| `git diff --check` | clean |
| Final diff review | documentation-only; no production/test/migration/protected-doc changes |
| Deployment/SQL/Supabase | not performed |
| Live Telegram/provider test | not performed |

There is no claim that a live schema, table, RLS policy or grant exists.

## 7. Intentionally not implemented (and why)

- No `history_schedules` migration: `DATABASE_ARCHITECTURE.md` is protected in this
  phase, and the repository requires the document update before the migration.
- No repository/service/tool: this phase is design-only.
- No scheduled History: it depends on the approved migration, repository,
  oldest-first retrieval and task-execution wiring.
- No oldest-first retrieval fix: explicitly deferred, but recorded as a blocking
  prerequisite.
- No retention/compaction implementation: only the 32 KiB policy and refusal
  behavior are designed.
- No AI context/tool changes: the future scheduled action must use
  `with_identifiers=False`, trusted task id and no message/task/checkpoint ids in
  prompts; current manual behavior is unchanged.
- No changes to `ai_tasks`, `ai_task_occurrences`, `notification_destination`,
  `ProviderManager`, pacing/capacity, TaskScheduler, TaskExecutionCoordinator,
  RuntimeSupervisor or ToolExecutor.

## 8. Commit and remote verification

This documentation-only change is delivered as the single Phase 3 commit on top of
`62a18de`. Because a commit cannot include its own hash before it exists, the exact
SHA is reported in the delivery response and verified after the push with a fresh
fetch (`HEAD == FETCH_HEAD`, new commit descendant of `62a18de`). No force-push,
rebase, reset or history rewrite was or will be used.

## 9. Limitations and open evidence

- The live Supabase inventory/28-table audit and migration history are documented
  facts from the repository, not re-verified against the live project in this
  phase.
- The proposed table is not deployed; no runtime consumer exists.
- The prior cumulative summary as AI input for compaction needs owner approval
  under the strict privacy wording (`INVESTIGATION.md` §10/§16).
- One schedule per peer, start baseline, task-deletion retention, and peer-migration
  re-pointing are product decisions recorded in `INVESTIGATION.md` §16.
- Telegram content that was never successfully processed and later became
  unavailable cannot be recovered by this design, and the design does not claim
  otherwise.
- Phase 3 implementation must update the protected schema document before adding
  the migration; otherwise the canonical-schema/setup-order tests will correctly
  fail.
