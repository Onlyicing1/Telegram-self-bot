# IMPLEMENTATION REPORT — History Phase 3A: durable state investigation and design

## 1. Delivery status

**Phase 3A is complete; Phase 3B is next and not started.** This was investigation
and design only. It did not implement durable History, scheduled History, a
repository, a migration or any runtime behavior. It did not modify production
Python, tests, database migrations or protected architecture documents.

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
| Local HEAD before Phase 3A edits | `62a18de07f1bc5bf8b51c36438f66ad8c4f4d498` (Phase 2) |
| Freshly fetched `FETCH_HEAD` at Phase 3A start | `62a18de07f1bc5bf8b51c36438f66ad8c4f4d498` |
| Documentation amendment base | `4877562284ded2b56593d202023f679805610455` |
| Freshly fetched `FETCH_HEAD` at amendment | `4877562284ded2b56593d202023f679805610455` |
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
  omit older eligible messages after a cursor. The existing test
  `test_translate_requests_exactly_the_requested_count_oldest_to_newest` actually
  asserts that `count=10` over 50 messages sends ids 41–50, so the current suite
  pins this behavior. This is the known defect and remains **unfixed**; the five
  History test files (191 test functions) are the baseline for a deliberate future
  update.
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
17. approved resolutions, open decisions and evidence gaps.

Core proposed shape (not applied): one `history_schedules` row per History task and
per `(owner_id, peer_id)`, linked to `ai_tasks(id) ON DELETE CASCADE`, with
baseline/scanned/summarized message ids, a bounded cumulative summary, summary
revision, status, bounded diagnostics, and a CAS `version`. The summary and both
cursors commit in one CAS update; the previous summary is never erased by a
deleted source message; 32 KiB is a hard, enforced ceiling with no silent
truncation. The table is proposed as service-role-only RLS because it contains
private chat content.

### Approved Phase 3A resolutions

- **Phase map:** Phase 3A (investigation/design) is complete. Phase 3B (durable
  schema/repository plus its retrieval prerequisite) is next and not started.
  Phase 4 (deterministic schedule UI), Phase 5 (scheduled History action), Phase 6
  (shared admission) and Phase 7 (live validation) keep their existing numbers and
  scopes.
- **One state per peer:** exactly one cumulative History state per
  `(owner_id, peer_id)`; `UNIQUE (owner_id, peer_id)` is retained. An active-only
  uniqueness constraint is rejected.
- **Lifecycle:** active, paused, completed, failed and expired tasks retain
  ownership. A second History schedule for the peer is refused while the state
  exists, including after pause or a terminal status. Pausing retains the summary
  and cursors. Physical task deletion cascades the state and allows a new schedule
  afterward. Any replacement/transfer is explicit and owner-authorized. Legacy
  `status='deleted'` is an invariant-repair case, not a silent permanent block.

**No proposed SQL was executed, and it is marked in `INVESTIGATION.md` as
unapproved design material.**

## 5. Files changed

| File | Change |
|---|---|
| `INVESTIGATION.md` | Phase 3A source-backed investigation/design, approved ownership lifecycle, open decisions, Phase 3B sequence and tests |
| `ROADMAP.md` | Phase 3A complete; Phase 3B next; Phases 4–7 unchanged; approved one-state-per-peer lifecycle |
| `IMPLEMENTATION_REPORT.md` | replaced with this truthful Phase 3A investigation-only report |

No other file was modified. `DATABASE_ARCHITECTURE.md`, `AI_MASTER_DESIGN.md`,
production code, tests and `supabase/migrations/` were untouched.

## 6. Validation performed

Because this is documentation-only, no code tests were fabricated or run.

| Check | Result |
|---|---|
| `git status --short` before Phase 3A edits | clean; Phase 2 HEAD verified |
| `cloud_git fetch` at Phase 3A start | `FETCH_HEAD == 62a18de…` |
| `git status --short` before this amendment | clean; `HEAD == FETCH_HEAD == 4877562…` |
| Source review | History service/AI service/policy, Telegram facade, handler, task schema/repository, scheduler/coordinator, provenance, five History test files, migration files and relevant `DATABASE_ARCHITECTURE.md` sections |
| Migration count/numbering | 41 files; latest `20261112160000`; no `20261113*`; no History table |
| `git diff --check` | clean |
| Final diff review | documentation-only; no production/test/migration/protected-doc changes |
| Deployment/SQL/Supabase | not performed |
| Live Telegram/provider test | not performed |

There is no claim that a live schema, table, RLS policy or grant exists.

## 7. Intentionally not implemented (and why)

- No `history_schedules` migration: `DATABASE_ARCHITECTURE.md` is protected in
  Phase 3A, and the repository requires the document update before the Phase 3B
  migration.
- No repository/service/tool: Phase 3A is design-only; Phase 3B has not started.
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

The Phase 3A design was delivered in commit
`70a8aab86f63e8c6b2e0ddb69526f3b57e1f95e8` on top of `62a18de`. After that push, a
fresh fetch verified `HEAD == FETCH_HEAD == 70a8aab…`, and
`git ls-remote origin refs/heads/main` reported the same SHA. Follow-up
documentation commits `7073702…` and `4877562…` completed the field/retention
table and corrected caption/privacy wording.

This approved-decisions documentation update is the current remote head; its exact
SHA is reported in the delivery response because a commit cannot include its own
hash before it exists. All commits are descendants of Phase 2 `62a18de` and Phase
1 `ffe72fd`. No force-push, rebase, reset or history rewrite was or will be used.

## 9. Limitations and open evidence

- The live Supabase inventory/28-table audit and migration history are documented
  facts from the repository, not re-verified against the live project in this
  phase.
- The proposed table is not deployed; no runtime consumer exists.
- The prior cumulative summary as AI input for compaction needs owner approval
  under the strict privacy wording (`INVESTIGATION.md` §10/§16).
- One state per peer, retained through active/paused/completed/failed/expired, is
  approved; physical deletion cascades it. Start baseline, prior-summary AI input,
  peer-migration re-pointing, the summary-limit escape hatch, the RLS exception,
  migration numbering and manual/scheduled overlap remain open in
  `INVESTIGATION.md` §16.
- Telegram content that was never successfully processed and later became
  unavailable cannot be recovered by this design, and the design does not claim
  otherwise.
- Phase 3B must update the protected schema document before adding the migration;
  otherwise the canonical-schema/setup-order tests will correctly fail.
