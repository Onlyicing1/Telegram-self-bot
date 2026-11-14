# ROADMAP

## Latest delivery: History Phase 4 + prerequisite Phase 5 action — owner creation and scheduled checkpoint

Status: **Phase 4's owner wizard/task+state creation and the prerequisite
registered Phase 5 action are implemented and focused-tested. The Phase 3B
migration remains APPLIED to the connected Supabase project (remote version
`20261114180758`). Live Telegram execution, restart/catch-up validation and
provider-failure validation remain unverified; Phase 6/7 stay deferred.**
The owner chose to move the registered action and trusted task-id plumbing ahead
of Phase 4 as a prerequisite; no phase was renumbered. Phase 3B audited commit:
`a12c20779bc634cb4511fb0e79443b882e8a1a85` (implementation `417a6b8`, upstream
parent `e2f1619`). Phase 3A base: `62a18de`.

### Phase map

| Phase | Scope | Status |
|---|---|---|
| **3A** | Durable History state investigation and design | **Complete** |
| **3B** | Durable schema/repository, oldest-first retrieval, identifier-free payload boundary | **Implemented and audited** — migration applied (remote `20261114180758`) |
| **4** | Deterministic schedule UI | **Implemented** — owner wizard, trusted creation path and compensating deletion |
| **5** | Scheduled History action | **Prerequisite implemented** — registered action, trusted task id, bounded catch-up, CAS commit; live validation deferred |
| **6** | Shared History admission | Deferred; numbering unchanged |
| **7** | Live validation | Deferred; numbering unchanged |

Phase 3 remains the umbrella durable-state phase. The owner changed only the
delivery order: the Phase 5 action/trusted task-id plumbing was required before
Phase 4 creation, then Phase 4 completed. No phase was renumbered.

### What this delivery adds

- **Registered action:** `scheduled_history` is one `ToolExecutor` action in the
  existing registry. It uses the coordinator-injected trusted task id from
  `ToolContext.extra`; peer, baseline, cursors and prior summary come only from
  the durable `history_schedules` row. It accepts no peer/task/message id from
  arguments and never creates or mutates a schedule.
- **Owner-only creation gate:** `TaskCreationService.create()` rejects
  `scheduled_history` unless the trusted owner-wizard action set is explicitly
  passed. The model/natural-language candidate path cannot create the action.
- **Phase 4 creation:** the History panel opens a deterministic wizard for the
  selected, re-classified peer. The owner chooses hourly/daily/weekly cadence,
  new-only vs explicit backlog baseline, and optional focus. Creation uses the
  existing `TaskCandidate → TaskCreationService → TaskRepository` path, writes
  exactly one task and one peer state row, refuses a live duplicate, refuses
  before any Telegram work when the manager reports durable storage unavailable,
  and compensates by deleting the exact created task when state creation fails.
- **Bounded execution:** one stable upper boundary per run, oldest-first batches
  of at most the peer policy cap, cumulative summary through the existing
  identifier-free History AI boundary, and one CAS commit for summary plus both
  cursors. Media/AI-provenance-only batches may advance the scanned cursor but
  never replace the summary; failures record bounded metadata without moving the
  summary or cursors.
- **No new scheduler, executor, repository, Telegram client, listener, update
  loop or persistence path.** The existing scheduler/coordinator/executor and
  durable-only repository remain the only execution and state paths.

### What Phase 3B actually delivers (source-verified)

- One additive migration, `supabase/migrations/20261114180758_create_history_schedules.sql`
  (part 18 of 18; inventory 42 files), applied to the connected project with
  remote version `20261114180758`:
  `task_id → ai_tasks(id) ON DELETE CASCADE`, `UNIQUE (owner_id, peer_id)`,
  immutable `baseline_cursor`, monotonic `scanned_cursor`/`summarized_cursor`
  with `baseline <= summarized <= scanned`, a 32 KiB UTF-8-bounded `summary`,
  `revision` + CAS `version`, bounded error metadata, status CHECK, one task-id
  index, RLS enabled with zero policies and `SELECT/INSERT/UPDATE/DELETE` granted
  only to `service_role`.
- Six repository primitives on the existing `TaskRepository` hierarchy:
  `create_history_schedule`, `get_history_schedule`,
  `get_history_schedule_by_peer`, `commit_history_success`,
  `record_history_failure`, `reset_history_summary`.
- **Durable-only production semantics.** Every History read/write is a single
  owner-scoped (and, for writes, CAS-versioned) durable operation. When the store
  is unavailable, times out, or the table is absent, the operation raises
  `HistoryStorageUnavailable` and writes nothing. Process memory is never
  substituted; `InMemoryTaskRepository` is an explicitly injected test fake only.
- Bounded oldest-first retrieval: optional `reverse` on
  `messages.iter_messages`/`TelegramAPI.iter_messages` (default unchanged), plus
  `fetch_oldest_history`/`HistoryBatch` with a stable upper bound, exclusive
  lower cursor, defensive sort/dedup and conservative `has_more`.
- `scheduled_summary_payload` — the identifier-free AI boundary (message bodies
  plus the owner's instruction only). No production caller in Phase 3B.
- No scheduler, executor, lifecycle hook, UI, parser, notification, catch-up
  policy, second Telegram client or second update loop.

### Independent audit corrections (this delivery)

1. **Durable-only violation fixed.** The six Supabase History methods caught
   every exception and returned memory-fallback records, so an outage or an
   unapplied migration could report a successful checkpoint that no restart
   could see. They now fail closed (`HistoryStorageUnavailable`); creation also
   refuses when the owning task could only be read from memory. Six regression
   tests cover reads, writes, CAS writes, creation and the structural guard that
   no History method references the fallback.
2. **Merge residue removed.** The merge left three files from the pre-upstream
   Appearance implementation (`backend/services/image_probe.py`,
   `backend/services/preview_storage.py`, `sql/panel_settings_appearance.sql`);
   upstream never had them and nothing references them. They were deleted.
3. **Documentation reconciled.** `ROADMAP.md` (this file), `DATABASE_ARCHITECTURE.md`
   (§16.1, §22, §30 inventory row) and `IMPLEMENTATION_REPORT.md` no longer claim
   an in-memory History fallback. `INVESTIGATION.md` records the audit findings.
4. **Scheduler failure classified.** `test_recovered_interrupted_occurrence_is_picked_up_by_run_once`
   fails identically at `a12c207`, `417a6b8` and `e2f1619`; the scheduler code
   and test are untouched by Phase 3B — demonstrably pre-existing.

### Completed

- Phase 3A source-verified design and approved ownership/lifecycle contract.
- Phase 3B migration, repository, retrieval, payload boundary and focused tests.
- Phase 5 prerequisite: trusted task-id injection, registered
  `scheduled_history` action, bounded incremental checkpoint service, stable
  upper boundary and CAS commit.
- Phase 4: owner-only schedule wizard, deterministic cadences/baseline, task
  and state creation through the existing service, duplicate refusal and
  compensating deletion.
- Focused tests cover owner gating, creation/compensation, cursor/CAS behavior,
  safe-skip, provider failure metadata, trusted task-id injection and the UI
  creation path.
- Independent audit of Phase 3B: repository/CAS/cursor review, durable-only
  enforcement, merge-integrity comparison against both parents,
  migration/§31.3 reconciliation.
- `IMPLEMENTATION_REPORT.md` and `INVESTIGATION.md` record the audited state.

### Applied migration and verification (2026-11-14)

- The migration is **applied** to the connected Supabase project. Its live
  migration-history record is version `20261114180758`, name
  `create_history_schedules`; the repository filename was reconciled from
  `20261113000001` to match that recorded version. The SQL is byte-identical.
- Owner-performed live checks passed for the table structure, constraints, RLS
  configuration, grants and sequence privileges.
- A transactional CAS probe ran against the live table and was **rolled back**;
  it left no test row behind.
- No coding agent executed SQL, applied a migration, repaired the migration
  history or modified any Supabase object.

### Not verified / blockers

- **Live scheduled execution is not verified**: no real scheduled occurrence has
  run against a live Telegram chat; the action and UI are covered by focused
  tests with injected retrieval/provider doubles.
- **No live restart/catch-up run, deleted-message gap run, provider-failure run
  or capped-peer refusal was exercised.** These remain Phase 7 work.
- **Task and state are two durable writes**: state failure is compensated by
  deleting the exact created task, but this is not a single cross-table
  transaction. A crash between the two writes can leave an owner-created task
  without its state; the action then fails closed and reports the missing state.
- The pre-existing scheduler recovery test failure recorded below remains
  unrelated and untouched.

### Deferred evidence-backed constraints (unchanged)

- 1,000-message single-request summaries remain infeasible at 240–300 s with
  concurrency 2 and 4,000-token chunks (~352 Latin / ~176 non-Latin messages).
- No verified chain-wide context floor, so the chunk budget must not grow.
- `ASSUMED_CALL_LATENCY_S = 30` is a planning value; measure
  `ProviderManager.metrics_snapshot` before changing pacing or concurrency.
- `ai_task_occurrences.result_metadata` stays out of summary storage (8 KiB
  bound, occurrence lifecycle ownership).

---

## Appearance (merged from the upstream line, preserved)

The upstream Appearance delivery in the merge is preserved byte-for-byte:
`backend/bot/handlers/appearance.py`, `backend/services/appearance_service.py`
and its three suites (`test_appearance_delivery.py`,
`test_appearance_gallery.py`, `test_appearance_image_cache.py`). Its
`tests/test_appearance.py` was removed by the upstream line itself and replaced
by those suites; the audit confirmed no Appearance coverage was lost.

Implemented: one Appearance category, shared header overrides, per-main-page
title/body overrides, and a preview image with an independent switch, delivered
as the panel's own photo with a versioned `/appearance/image/<version>` rendition,
backed by a durable Storage object key and a ten-image bounded gallery.

Still required (Stage 2): live Telegram verification of small-preview rendering,
tap-through and keyboard preservation on current clients; optional strict-write
hardening for the remaining panel settings.

Explicitly out of scope: a generated default image, web-dashboard upload,
multiple selected images, per-page preview images, scheduled rotation, or any
second renderer/settings table/listener/client/update loop.

---

## Later phases (deferred, in order)

1. **Phase 6 — shared History admission**: one process-wide in-flight/start gate
   inside `history_ai_service`, because per-operation pacing does not coordinate
   bounded concurrent occurrences.
2. **Phase 7 — live validation** of a real schedule, a restart mid-run, a deletion
   of a previously processed source message, a provider failure, and a capped-peer
   refusal.

Completed sequencing note: Phase 5's action and trusted task-id plumbing were
moved ahead as prerequisites, then Phase 4's owner wizard and task/state creation
were completed. No phase was renumbered.

---

## Previous delivery (complete, unchanged): History Peer Policy Phase 2

Base `ffe72fd`: deterministic `history_policy` over the Phase 1 `PeerType`,
per-peer caps (bot/channel 300, user/group/supergroup 1000), dynamic panel count
choices, stale-callback rejection, service-boundary and linked-message enforcement.
78 new policy tests. See the commit history and the report history.

---

## Previous delivery (complete, unchanged): Peer Classification Phase 1

Base `ecc3779`: `PeerType` + pure `classify_entity` in
`backend/telegram_api/peers.py`, migrated into the History picker label,
General → Chat & Message IDs, Ghost Seen eligibility and Anti-Edit bot detection.
Strict entity-flag precedence, fail-closed `unknown`, no persistence or cache.
41 tests.

---

## Previous delivery (complete, unchanged): History AI-pass capacity

Base `7894f31`: script-aware token estimation, concurrency-aware map capacity,
map + reduce call accounting, and an honest refusal naming the AI time budget and
the affordable message count. Chunk budget, concurrency, pacing, deadlines and
ProviderManager ownership were unchanged.

---

## Open items carried over (untouched by this delivery)

- `tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`
  (“interval advancement exceeded bound”) — the only full-suite failure at
  `a12c207`, reproduced identically at both parents; not caused by Phase 3B.
- Live scheduled History execution and live Telegram behavior remain unverified
  (the migration itself is applied).
