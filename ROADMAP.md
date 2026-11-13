# ROADMAP

## Latest delivery: History Phase 3A — durable state investigation and design

Status: **Phase 3A complete. Phase 3B not started. Nothing implemented or
deployed.**
Base: `62a18de07f1bc5bf8b51c36438f66ad8c4f4d498` (Phase 2).

Phase 3A was deliberately investigation-only. It established, from current source,
what the History path persists (nothing), what is lost on restart, why the
existing task/occurrence fields cannot hold a cumulative summary, and the design of
a dedicated durable state table and repository contract. `INVESTIGATION.md` is the
source-backed deliverable.

### Phase map

| Phase | Scope | Status |
|---|---|---|
| **3A** | Durable History state investigation and design | **Complete** |
| **3B** | Durable schema/repository implementation and its required oldest-first retrieval prerequisite | **Next, not started** |
| **4** | Deterministic schedule UI | Deferred; numbering unchanged |
| **5** | Scheduled History action | Deferred; numbering unchanged |
| **6** | Shared History admission | Deferred; numbering unchanged |
| **7** | Live validation | Deferred; numbering unchanged |

Phase 3 remains the umbrella durable-state phase: 3A is the completed
investigation/design stage and 3B is the implementation stage. No existing phase
was renumbered.

### Verified findings

- History retrieval and summarization are one-shot. The summary exists only in the
  edited Telegram panel message; no application row, cursor or summary survives a
  restart.
- `ai_task_occurrences.result_metadata` is bounded at 8 KiB and belongs to the
  occurrence lifecycle; `notification_destination.chat_id` is the delivery
  destination, not the source peer. Neither is suitable for History state.
- The current collector scans newest→oldest and keeps the newest subset
  (`history_service._collect`), so 60 eligible messages with `count=10` can return
  51–60 and omit 1–50. **This defect remains unfixed and is a blocking prerequisite
  for Phase 3B and scheduled processing.**
- Every peer classification is live; a stored peer type can only be an
  informational snapshot, never policy authority.
- A new migration cannot be added alone: `DATABASE_ARCHITECTURE.md` is the schema
  authority and the suite requires every migration's table to be classified and the
  canonical §31.3 setup block to match. The document is protected in Phase 3A and
  was not modified.

### Proposed design (not applied)

- One dedicated `history_schedules` table, one row per History task and per
  `(owner_id, peer_id)`, linked to `ai_tasks(id) ON DELETE CASCADE`.
- Fields: owner, peer id, informational peer-type snapshot, baseline message id,
  scanned and summarized cursors, bounded cumulative summary (32 KiB), summary
  revision, state status, last success, bounded error diagnostic, CAS version and
  timestamps.
- `last_scanned_message_id` is the durable retrieval cursor; it advances only with
  a successful atomic commit covering summarized content or explicit safe skips.
  `last_summarized_message_id` records content coverage and never exceeds it.
- Summary plus both cursors, revision and version commit in one CAS update.
  A stale writer gets zero rows and discards its result; it never overwrites a newer
  revision. No runtime in-memory fallback is allowed for durable checkpoints.
- Service-role-only RLS (zero policies, no client grants) because the summary is
  private chat content.
- No raw messages, sender ids, titles/usernames or provider raw output are stored.
  Deleted Telegram messages never erase a previously committed summary; they are
  not recoverable.

### Completed

- Source-verified current History pipeline, bounds, provenance filtering, persistence
  gaps and restart behavior.
- Verified `ai_tasks` / `ai_task_occurrences` constraints, repository CAS patterns,
  scheduler recovery/claim behavior and task deletion flow.
- Field-by-field justification, proposed table DDL (unapproved), repository API,
  atomic commit/CAS semantics, 14-case cursor/failure table, retention, privacy and
  AI-isolation requirements.
- Verified migration count/numbering (41 files; latest
  `20261112160000`) and the documentation/test prerequisites for a future
  migration.
- `IMPLEMENTATION_REPORT.md`, `INVESTIGATION.md` and this roadmap updated.
- Owner-approved phase map (3A complete, 3B next, Phases 4–7 unchanged) and the
  one-state-per-peer ownership lifecycle recorded.

No production Python, test, migration or Supabase object was changed. No SQL was
executed. No live Telegram/provider behavior was tested.

### Approved ownership and lifecycle (V1)

- Exactly one cumulative History state per `(owner_id, peer_id)`.
- `UNIQUE (owner_id, peer_id)` is retained. It is deliberately **not** replaced by
  an active-only uniqueness constraint.
- Active, paused, completed, failed and expired tasks retain ownership of their
  History state.
- A second History schedule for the same peer is **refused** while that state
  exists, including when the owning task is paused or terminal. The refusal must
  direct the owner to resume or delete the existing schedule.
- Pausing retains the summary and both cursors.
- Physical task deletion cascades to the History state, allowing a new schedule
  afterward.
- Any future replacement/transfer operation must be explicit and owner-authorized.
- Legacy `status='deleted'` is an invariant-repair case; it must not silently block
  a peer forever.

---

## Next stage: Phase 3B — durable schema/repository and retrieval prerequisite (not started)

Phase 3B implements the Phase 3A design. It does not change the numbering or scope
of Phase 4–7.

1. **Owner approval and protected document update.** Update
   `DATABASE_ARCHITECTURE.md` with the `history_schedules` section, §22/§25
   classification, canonical §31.3 part 1 SQL and the service-role-only RLS
   exception. This must happen before the migration; the suite enforces it.
2. **Manual migration.** Add the idempotent
   `20261113120000_create_history_schedules.sql` (candidate name) after approval.
   The owner applies it in the Supabase SQL editor; no agent executes SQL.
3. **Repository.** Add `HistoryStateRepository` + Supabase implementation +
   test-only in-memory fake. No runtime fallback; unavailable storage fails closed.
4. **Oldest-first retrieval.** Fix the collector contract before any scheduled
   execution, using a stable upper bound, exclusive lower cursor and
   `reverse=True` via the existing facade.
5. **Creation-path contract.** Expose the approved second-schedule refusal and the
   task+state compensating delete for the Phase 4 wizard. Phase 3B does not build
   the wizard or scheduled action.
6. **Implementation tests** listed in `INVESTIGATION.md` §14.
7. **Structural verification** after the owner applies the migration: table,
   PK/FK/unique/check constraints, RLS, grants, and documentation/setup-order
   reconciliation. Live scheduled validation remains Phase 7.

### Blocking prerequisites for Phase 3B

- `DATABASE_ARCHITECTURE.md` approval/update (protected in Phase 3A), including
  the service-role-only RLS exception, and confirmation of the Phase 3B migration
  number before the file is added.
- Oldest-first retrieval fix and tests (Phase 3B).

### Open product decisions for later phases

- Start baseline (Phase 4), prior-summary AI input and summary-limit escape hatch
  (Phase 5), and peer-migration re-pointing (`INVESTIGATION.md` §16).

---

## Later phases (deferred, in order)

1. **Phase 4 — deterministic schedule UI**: owner wizard resolves/classifies the
   peer, captures the baseline, creates the History task and its state row through
   the existing TaskCreationService path, with a compensating delete on failure. The
   model never creates or mutates these schedules.
2. **Phase 5 — scheduled History action**: one new registered ToolExecutor action
   with no model-supplied identifiers, trusted task id, stable upper boundary,
   oldest-first catch-up, cumulative bounded summary, and one atomic CAS commit.
   Not shippable before the retrieval prerequisite and Phase 3B are complete.
3. **Phase 6 — shared History admission**: one process-wide in-flight/start gate
   inside `history_ai_service`, because per-operation pacing does not coordinate the
   scheduler's bounded concurrent occurrences.
4. **Phase 7 — live validation** of a real schedule, a restart mid-run, a deletion
   of a previously processed source message, a provider failure, and a capped-peer
   refusal.

### Deferred evidence-backed constraints (unchanged)

- 1,000-message single-request summaries remain infeasible at 240–300 s with
  concurrency 2 and 4,000-token chunks (~352 Latin / ~176 non-Latin messages).
  A 300-message product allowance does not guarantee AI feasibility.
- No verified chain-wide context floor, so the chunk budget must not grow.
- `ASSUMED_CALL_LATENCY_S = 30` is a planning value; use
  `ProviderManager.metrics_snapshot` before changing pacing.
- `ai_task_occurrences.result_metadata` stays out of summary storage (8 KiB bound,
  occurrence lifecycle ownership).

---

## Previous delivery (complete, unchanged): History Peer Policy Phase 2

Base `ffe72fd`: deterministic `history_policy` over the Phase 1 `PeerType`,
per-peer caps (bot/channel 300, user/group/supergroup 1000), dynamic panel count
choices, stale-callback rejection, service-boundary and linked-message enforcement.
78 new policy tests; full suite 7,750 passed with the same 6 pre-existing failures.
See the commit history and `IMPLEMENTATION_REPORT.md` history for details. It
remains valid.

---

## Previous delivery (complete, unchanged): Peer Classification Phase 1

Base `ecc3779`: `PeerType` + pure `classify_entity` in
`backend/telegram_api/peers.py`, migrated into the History picker label, General →
Chat & Message IDs, Ghost Seen eligibility and Anti-Edit bot detection. Strict
entity-flag precedence, fail-closed `unknown`, no persistence or cache. 41 tests.
See the commit history. It remains valid and was not modified by Phase 2 or this
investigation.

---

## Previous delivery (complete, unchanged): History AI-pass capacity

Base `7894f31`: script-aware token estimation, concurrency-aware map capacity,
map + reduce call accounting, and an honest refusal naming the AI time budget and
the affordable message count. Chunk budget, concurrency, pacing, deadlines and
ProviderManager ownership were unchanged. See the commit history; it remains valid.

---

## Open items carried over (untouched by this delivery)

- Supabase public `SELECT` exposure and the two server-only table exceptions
  (`test_aed_db_security`, `test_private_message_db_security`).
- Canonical schema reconciliation / migration-history documentation
  (`test_canonical_schema_reconciliation`, `test_database_setup_order`).
- The recovered-interrupted-occurrence scheduler test
  (`test_task_scheduler::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`).

These are the six pre-existing suite failures, reproduced identically at the
Phase 2 base commit. Phase 3A changed only documentation and did not run or alter
those tests.
