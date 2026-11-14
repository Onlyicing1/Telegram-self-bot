# ROADMAP

## Latest delivery: History Phase 3B — durable state and oldest-first retrieval (audited)

Status: **Phase 3B implemented, independently audited and corrected. The
migration is NOT applied and live durable behavior is NOT verified. Phase 4 is
deferred.**
Audited delivery commit: `a12c20779bc634cb4511fb0e79443b882e8a1a85`
(Phase 3B implementation `417a6b8`, upstream parent `e2f1619`).
Phase 3A base: `62a18de`.

### Phase map

| Phase | Scope | Status |
|---|---|---|
| **3A** | Durable History state investigation and design | **Complete** |
| **3B** | Durable schema/repository, oldest-first retrieval, identifier-free payload boundary | **Implemented and audited** — migration unapplied, live behavior unverified |
| **4** | Deterministic schedule UI | **Deferred** until the owner applies the migration |
| **5** | Scheduled History action | Deferred; numbering unchanged |
| **6** | Shared History admission | Deferred; numbering unchanged |
| **7** | Live validation | Deferred; numbering unchanged |

Phase 3 remains the umbrella durable-state phase. No phase was renumbered.

### What Phase 3B actually delivers (source-verified)

- One additive migration, `supabase/migrations/20261113000001_create_history_schedules.sql`
  (part 18 of 18; inventory 42 files):
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
  is unavailable, times out, or the migration is unapplied, the operation raises
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
- Independent audit: repository/CAS/cursor review, durable-only enforcement,
  merge-integrity comparison against both parents, migration/§31.3 reconciliation.
- `IMPLEMENTATION_REPORT.md` and `INVESTIGATION.md` record the audited state.

### Not verified / blockers

- **The migration has not been applied to Supabase.** No SQL was executed and no
  live database was contacted. Live RLS, FK cascade, unique-constraint and CAS
  behavior are unverified.
- **No live Telegram behavior was tested**: no real oldest-first run, no restart
  mid-run, no deleted-message gap run.
- No scheduler/executor calls the repository or the payload yet; Phase 3B is
  foundation only.
- **Phase 4 is deferred** until the owner applies the migration and confirms the
  live durable behavior.

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

1. **Phase 4 — deterministic schedule UI**: owner wizard resolves/classifies the
   peer, captures the baseline, creates the History task and its state row through
   the existing TaskCreationService path, with a compensating delete on failure.
   The model never creates or mutates these schedules.
2. **Phase 5 — scheduled History action**: one new registered ToolExecutor action
   with no model-supplied identifiers, trusted task id, stable upper boundary,
   oldest-first catch-up, cumulative bounded summary, and one atomic CAS commit.
   Not shippable before the owner applies the migration and Phase 4 exists.
3. **Phase 6 — shared History admission**: one process-wide in-flight/start gate
   inside `history_ai_service`, because per-operation pacing does not coordinate
   bounded concurrent occurrences.
4. **Phase 7 — live validation** of a real schedule, a restart mid-run, a deletion
   of a previously processed source message, a provider failure, and a capped-peer
   refusal.

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
- Applying `20261113000001_create_history_schedules.sql` remains an owner action.
- Live durable History behavior remains unverified until that application.
