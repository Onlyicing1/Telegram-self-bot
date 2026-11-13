# ROADMAP

## Latest delivery: Peer Classification Phase 1 — reusable foundation

Status: **implemented, tested, pushed.** Base: `ecc3779814f6bc0a45588cb35629c3aa5288550c`.

A single pure classifier now answers what kind of Telegram peer a resolved
entity is, and the four places that used to answer it differently consume it.

- `backend/telegram_api/peers.py`: `PeerType` (`private_user`, `private_bot`,
  `basic_group`, `supergroup`, `broadcast_channel`, `unknown`) and
  `classify_entity(entity)`.
- Real Telethon types via `isinstance`; `User.bot`, `Channel.broadcast`,
  `Channel.megagroup`, `Channel.gigagroup` are the only authorities. A gigagroup
  is a supergroup variant (no separate type). No titles, usernames, id patterns,
  Dialog booleans or AI output. Unknown fails closed.
- Migrated: History chat-picker label, General → Chat & Message IDs type,
  Ghost Seen user/private eligibility (its own exclusions kept), Anti-Edit/Delete
  `is_bot_entity` (its safe fallback kept).
- Not persisted and not cached: the type is derived from the currently resolved
  entity on every classification.

### Verified results

| Check | Result |
| --- | --- |
| New classifier + call-site suites (7 files) | 311 passed |
| Full suite | 7,659 passed, 6 failed, 27 skipped |
| The same 6 tests on a clean `ecc3779` worktree | 6 failed — identical, pre-existing |
| `py_compile` / `git diff --check` | clean |

### Completed

- `PeerType` + pure `classify_entity` with a strict flag order and fail-closed
  unknown handling.
- Four call sites migrated without changing their feature policy, compatibility
  doubles, or safe fallbacks.
- `tests/test_peer_classification.py` (41 tests) with real Telethon entities,
  negative/partial inputs, purity checks and per-call-site coverage.
- Reports replaced (`IMPLEMENTATION_REPORT.md`, `INVESTIGATION.md`, this file).

---

## Next stage: Phase 2 — History per-peer policy

Planned, **not started**:

- `backend/services/history_policy.py` mapping `PeerType` to the product caps
  (`broadcast_channel` 300, `private_bot` 300, `private_user` 1000,
  `basic_group` 1000, `supergroup` 1000, `unknown` refuses).
- Resolve/classify the selected peer BEFORE applying the count; never silently
  clamp a chosen count; explain an over-cap choice explicitly.
- Keep the independent AI feasibility gate (300 messages is not automatically
  feasible at the current envelope).
- History panel count choices must reflect the cap for the selected peer without
  changing retrieval, cursors, pagination or delivery.

No History behaviour change ships in Phase 1.

---

## Later phases (deferred, in order)

1. **Phase 3 — persistence**: owner-applied manual migration for a dedicated
   `history_schedules` state table (peer, both cursors, bounded summary,
   revision, status, CAS version). `ai_task_occurrences.result_metadata` stays
   out of summary storage (8 KiB hard bound, occurrence lifecycle).
2. **Phase 4 — deterministic schedule UI**: owner wizard creates the task and its
   state row through the existing TaskCreationService/TaskManagementService; the
   model never creates or mutates these schedules.
3. **Phase 5 — scheduled History action**: one new registered ToolExecutor tool
   with no model-supplied identifiers, a stable upper-bound cursor, oldest-first
   catch-up and an atomic summary + checkpoint commit.
4. **Phase 6 — shared History admission**: one process-wide in-flight/start gate
   inside `history_ai_service`, because per-operation pacing does not coordinate
   the scheduler's bounded concurrent occurrences.
5. **Phase 7 — live validation** of a real schedule, a restart mid-run, a
   deferral and a capped-peer refusal.

### Deferred evidence-backed constraints (unchanged)

- 1,000-message single-request summaries remain infeasible at 240–300 s with
  concurrency 2 and 4,000-token chunks (~352 Latin / ~176 non-Latin messages).
- No verified chain-wide context floor, so the chunk budget must not grow.
- `ASSUMED_CALL_LATENCY_S = 30` is a planning value; use
  `ProviderManager.metrics_snapshot` before changing pacing.

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
Phase 1 base commit.
