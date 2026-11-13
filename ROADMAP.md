# ROADMAP

## Latest delivery: History Peer Policy Phase 2 — per-peer count limits

Status: **implemented, tested, pushed.** Base: `ffe72fd0bce9286e3d3d6583614b3b243bfd4fdf`
(Phase 1).

A deterministic product policy now decides how many messages one manual History
request may cover per peer type, the panel offers only the permitted counts, stale
callbacks are refused, and the policy is enforced at the service boundary and on
the linked-message path.

- `backend/services/history_policy.py`: `maximum_for`, `choices_for`,
  `validate_count`, `validate_choice`, `UNKNOWN_REFUSAL`.
- Exact mapping: `private_user` 1000 (100/250/500/1000), `private_bot` 300
  (100/250/300), `basic_group` 1000 (100/250/500/1000), `supergroup` 1000
  (100/250/500/1000), `broadcast_channel` 300 (100/250/300), `unknown` refused.
- The 300-message caps are HARD: over-limit requests are refused, never silently
  clamped. `unknown`/unresolvable peers never fall back to the 1,000 policy.
- The actual current entity is resolved and classified on selection, on every count
  callback, on every manual run, and for linked messages. Dialog flags, titles,
  usernames, id patterns and cached selections are never used.
- The panel hides 500/1000 for capped peers; stale callbacks are rejected against
  the current peer with the real limit. Existing navigation, focus, link, inline
  editing and delivery are unchanged.
- The service gate resolves → classifies → validates → only then retrieves; the
  existing AI capacity gate remains independent. Bypassing the panel cannot bypass
  the policy.

### Verified results

| Check | Result |
| --- | --- |
| New policy suite | 78 passed |
| Policy + panel + AI tools + capacity + service + Phase 1 classifier | 300 passed |
| Full suite | 7,750 passed, 6 failed, 27 skipped |
| The same 6 tests at the Phase 1 base | 6 failed — identical, pre-existing |
| `py_compile` / `git diff --check` | clean |

### Completed

- `history_policy.py` with the exact per-peer hard maximum and offered choices.
- Dynamic panel choices based on the classified selected peer; invalid choices
  hidden, not disabled.
- Stale/invalid callback rejection with an honest limit explanation and no AI call.
- Service-boundary enforcement in `history_ai_service._prepare` (summary and
  translation) and linked-message/`ending_at` pre-validation.
- `history_service.classify_chat` on the existing entity-resolution boundary.
- `tests/test_history_policy.py` plus updated panel, AI-tool and capacity tests;
  Phase 1 classifier tests retained.
- Reports replaced/updated (`IMPLEMENTATION_REPORT.md`, `INVESTIGATION.md`, this
  file).

---

## Next stage: Phase 3 — schema/repository investigation and planning

Status: **next, not started.**

- Investigate the durable History state shape (peer, both cursors, bounded summary,
  revision, status, CAS version) and its repository contract.
- Produce an implementation plan before writing any migration; no SQL may be
  applied without the owner's explicit approval.
- Keep `ai_task_occurrences.result_metadata` out of summary storage (hard 8 KiB
  bound, occurrence lifecycle ownership).
- Confirm the oldest-first retrieval prerequisite before any scheduled processing
  (see `INVESTIGATION.md`; still unfixed).

---

## Later phases (deferred, in order)

1. **Phase 4 — deterministic schedule UI**: owner wizard creates the task and its
   state row through the existing TaskCreationService/TaskManagementService; the
   model never creates or mutates these schedules.
2. **Phase 5 — scheduled History action**: one new registered ToolExecutor tool
   with no model-supplied identifiers, a stable upper-bound cursor, oldest-first
   catch-up and an atomic summary + checkpoint commit.
3. **Phase 6 — shared History admission**: one process-wide in-flight/start gate
   inside `history_ai_service`, because per-operation pacing does not coordinate
   the scheduler's bounded concurrent occurrences.
4. **Phase 7 — live validation** of a real schedule, a restart mid-run, a
   deferral and a capped-peer refusal.

### Deferred evidence-backed constraints (unchanged)

- 1,000-message single-request summaries remain infeasible at 240–300 s with
  concurrency 2 and 4,000-token chunks (~352 Latin / ~176 non-Latin messages).
  A 300-message product allowance does not guarantee AI feasibility.
- No verified chain-wide context floor, so the chunk budget must not grow.
- `ASSUMED_CALL_LATENCY_S = 30` is a planning value; use
  `ProviderManager.metrics_snapshot` before changing pacing.

---

## Previous delivery (complete, unchanged): Peer Classification Phase 1

Base `ecc3779`: `PeerType` + pure `classify_entity` in
`backend/telegram_api/peers.py`, migrated into the History picker label, General →
Chat & Message IDs, Ghost Seen eligibility and Anti-Edit bot detection. Strict
entity-flag precedence, fail-closed `unknown`, no persistence or cache. 41 new
tests; reports in the commit history. It remains valid and was not modified by
Phase 2.

---

## Previous delivery (complete, unchanged): History AI-pass capacity

Base `7894f31`: script-aware token estimation, concurrency-aware map capacity,
map + reduce call accounting, and an honest refusal naming the AI time budget and
the affordable message count. Chunk budget, concurrency, pacing, deadlines and
ProviderManager ownership were unchanged. See the commit history for its full
report; it remains valid.

---

## Open items carried over (untouched by this delivery)

- Supabase public `SELECT` exposure and the two server-only table exceptions
  (`test_aed_db_security`, `test_private_message_db_security`).
- Canonical schema reconciliation / migration-history documentation
  (`test_canonical_schema_reconciliation`, `test_database_setup_order`).
- The recovered-interrupted-occurrence scheduler test
  (`test_task_scheduler::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`).

These are the six pre-existing suite failures, reproduced identically at the
Phase 2 base commit.
