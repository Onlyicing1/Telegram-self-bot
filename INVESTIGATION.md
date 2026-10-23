# INVESTIGATION — whole-runtime responsiveness of the Telegram self-bot

| Item | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Worktree branch | `freebuff/99799eefb106784388e20bc8` |
| **HEAD at investigation start** | **`a4b6e4f1479c172a824b3084233b9d108c08d90d`** (`perf(callbacks): cut redundant round trips and coalesce burst renders`), equal to the remote tip this clone fetched (`git rev-parse FETCH_HEAD`) |
| Working tree at start | clean |
| Date | 2026-10-23 (UTC) |
| Symptom under investigation | the freeze/backlog after rapid inline-button clicks persisted after the phase-1/2 callback work |
| Scope | the complete runtime, with measurements concentrated on the click path (update → ack → turn → handler → database/Telegram → paint) |
| Explicitly out of scope | Save pipeline semantics, the AI runtime's own behaviour, Taskloom scheduling, Supabase schema, Render, any live account |
| **Status** | **Two application defects CONFIRMED for the remaining burst cost, one secondary lock defect CONFIRMED and fixed, one residual cost CONFIRMED and left in place (documented).** One amplifier hypothesis remains UNCONFIRMED |

### Method and its limits

* Every source claim was re-read from the working tree at `a4b6e4f` (and, for
  "before" numbers, from the same tree with this change stashed).
* **No production evidence was accessible in this workspace**: Render logs could
  not be read and no Telegram session exists. The phase-1 live numbers
  (`ack_ms` 160–296, `wait_ms` ≤ 352, `work_ms` 368–668, `total_ms` 528–1095, low
  CPU) are used only as the latency budget for the fakes.
* Timing evidence comes from `logs/bench_panel_burst.py` and
  `tests/test_callback_latency_burst.py`, which install the **real** callback
  router, the **real** panel handlers and the **real** gate/lifecycle, and fake
  only the two network ends at fixed latencies.
* Consequence: the numbers prove what a click waits on and how many round trips
  it makes. They do not prove what Telegram or Supabase answer.

---

## 1. The runtime latency map (Phase 2)

```
Telegram update ──► helper-bot client (Telethon, one task per update)
                    │
                    ├─ guard: is_owner (integer compare)
                    ├─ callback_gate.admit  (query_id dedupe, bounded 128)
                    ├─ callback_gate.ack    ── 1 Telegram RPC (the spinner)
                    ├─ resolve session      (in-memory registry)
                    ├─ supersede background writer (in-memory)
                    ├─ PER-PANEL TURN (FIFO future queue)
                    │   ├─ handler: services + db_client.run_sync_db (4 threads)
                    │   └─ returns the view          ◄── turn released here
                    └─ PAINTER (one per message): newest view → event.edit
                                                      ── 0..1 Telegram RPC
```

| Stage | Where | Cost observed |
|---|---|---|
| Update receipt / dispatch | `telethon/client/updates.py:289` | concurrent, per-update task |
| Acknowledgement | `callback_gate.ack` | one RPC (`ack_rpc_ms`), never waits for work |
| Turn wait | `callback_gate.acquire_turn` | 0 for an idle panel; grew with the queue (was the visible freeze) |
| Handler | panel/action handlers → services | 0 ms (pure views) to ~120 ms (one Supabase read) |
| Database | `db_client.run_sync_db` (4 workers, 10 s watchdog, 8 s transport) | ~120 ms per call on Render; **inside** the turn |
| Telegram edit | `panels._safe_edit` | ~250 ms |
| Paint | `callback_gate.PanelPainter` | outside the turn since this change |

Global/shared primitives audited: `lifecycle._lock` (panel create/close — **was**
held across an RPC), `_painters` (one per message), `_turns` (one per message),
`_save_code_lock` (save-code allocation only), `db._db_executor_lock` (pool
construction), provider semaphores (AI only), Taskloom semaphores (task
occurrences only), `callback_trace._flush_lock` (unused module).

---

## 2. Confirmed findings

| # | Finding | Evidence | Status |
|---|---|---|---|
| 1 | The FIFO turn spanned the Telegram edit, so N clicks on one panel meant N serialised edits and every click waited for an edit it would overwrite. 15 clicks with a trivial handler = 3.9 s, 15 edits | source (`_callback_router`) + benchmark | **CONFIRMED, FIXED** |
| 2 | The phase-2 coalescing was opt-in for ONE handler (`retrieve`'s tick); every other panel kept (1) | source (`SKIP_RENDER` only returned by `_toggle_selected_item`) + benchmark on `menu`/`health` | **CONFIRMED, FIXED** (replaced by a general writer) |
| 3 | `PanelLifecycleManager.close_panel`/`timeout_panel` performed the closing edit **inside** `_lock`, and `create_panel`/`try_reuse_panel` hold it across the inline trigger + edit, so a Close (or auto-close timer) serialised unrelated panel operations process-wide | source (`lifecycle.py`) + measurement: opening a panel during a slow close waited **230.6 ms** | **CONFIRMED, FIXED** |
| 4 | A panel that reads the database per click pays one read per click **inside** the turn, so a burst still costs ≈ N × read (30 clicks ≈ 3.6 s) | benchmark | **CONFIRMED, NOT FIXED** — ordered views require it; documented in §5 |
| 5 | Handlers that raised were logged but reported `completed` (the dispatch helpers swallowed the exception) | source | **CONFIRMED, FIXED** — a raising handler is now `failed` |

## 3. Explicitly ruled out (this phase, current source)

* **Telethon serialising updates** — `updates.py:289` creates a task per update;
  neither client sets `sequential_updates`. A slow handler cannot delay the next
  click's delivery, which the burst benchmarks show directly.
* **Event-loop blocking** — the only `time.sleep` calls in the runtime
  (`speechmatics_stt_engine._sleep_briefly`, `gemini_media_engine._await_file_ready`)
  live in synchronous engine methods that are invoked through `asyncio.to_thread`
  (`ai/stt_provider_probe.py:329` and the media-engine boundaries). No
  `requests`, `subprocess`, `run_until_complete` or `.result()` on a request path.
* **Database-pool saturation as the cause of the freeze** — the pool is bounded
  (4 workers) with a 10 s watchdog and an 8 s transport deadline. A saturated
  pool now has a test proving it degrades to a bounded `OperationTimeoutError`
  and that an unrelated panel still answers and paints meanwhile. Saturation
  would show as `db_ms`, which the timing line reports.
* **Heartbeat/keepalive/failsafe** — no database work, no panel turn.
* **`callback_trace`** — the module is not imported anywhere in the runtime.
* **AI handler / providers / tool executor** — `ai_unified` listens on the
  **self** client for outgoing messages; provider and tool calls are `httpx`
  awaits inside their own task, bounded by AI-only semaphores. They cannot sit in
  the click path.
* **Duplicate delivery** — suppressed by `query_id` since phase 1; the benchmark
  harness re-confirmed it (reused query ids had 15 clicks suppressed, which is
  why the harness resets the gate per cell).
* **Unbounded queues / task leaks** — one future per waiting click (bounded by
  delivered updates), one painter per active message (forgotten on close), bounded
  timing (64) and duplicate (128) stores.

## 4. Open hypothesis (not a finding)

Telegram rapid-edit throttling: frequent edits of one message are widely reported
to be rate-limited, and both clients set `flood_sleep_threshold=60`, so Telethon
sleeps inside the call on a flood answer. Thirty edits of one message in a few
seconds is exactly that pattern. This change cuts edits per burst by 87–93 % on
I/O-free panels, which removes most of the exposure — but no `FLOOD_WAIT` was
observed here, so it is not claimed as a cause.

## 5. Why the fix takes this shape

* **Paint is a projection, not state.** The turn must order *state* (handlers
  read the panel's session/nav and may write it). A paint is a pure function of
  the view the handler just built, so it does not need the turn and must not hold
  it.
* **One writer per message, newest view wins.** Edits to one message must not run
  concurrently (two in flight can land out of order); a queued view is worthless
  the moment a newer one exists. A one-slot painter gives both: serialised
  writes, newest wins, and N clicks cost at most two edits.
* **No task per click.** The first click's task drives the drain loop and picks
  up whatever the queue published; a second click publishes into the slot and
  returns. Nothing is spawned, nothing is queued beyond one slot, and a click
  whose view is superseded is still applied exactly once.
* **The turn remains FIFO and per panel.** Different panels never wait for each
  other, and a handler always sees the state left by the previous click.
* **The lifecycle lock guards state only.** Resource release is synchronous; the
  closing edit is a courtesy to the client and moved outside the lock, so one
  panel's close cannot serialise another panel's create.
* **Observability before and after.** `ack_rpc_ms` separates the answer RPC from
  application latency, `work_ms` is handler-only, `paint_ms` is the paint, and
  `db`/`edits`/`coalesced` state what the click actually cost — measured at the
  one dispatcher (`run_sync_db`) and the one edit path (`_safe_edit`), never
  estimated.

---

## 6. Historical investigations (kept, superseded, not relied on)

* **2026-10-23 — phase 2 (round-trip reduction + Stash snapshot).** Its confirmed
  findings (4 DB round trips per Stash tick; one edit per tick) and its Stash page
  snapshot stand. Its *cost* conclusion — that a serialised burst is inherently
  `N × (read + edit)` — is superseded: the edit was never part of ordering, and a
  burst on an I/O-free panel is now constant-time. Its `SKIP_RENDER` opt-in is
  replaced by the panel writer.
* **2026-10-23 — phase 1 (acknowledgement + ordering).** Ack-first, one FIFO turn
  per panel, `query_id` duplicate suppression: all still in force and still
  verified (ack is one RPC; handlers on one panel never overlap). Its replay of
  Telethon's dispatch (`updates.py:289`) was re-checked against the pinned wheel.
* **2026-10-22 — "can the Helper Bot edit an existing inline panel
  asynchronously?"** Verdict was UNRESOLVED (no runtime evidence that Telegram
  hands the Helper Bot an inline-message identifier for these panels). Its library
  facts stand and are what `backend/helper/panel_edit.py::ExistingPanelEditor`
  relies on. This task neither depends on nor changes it.
* Earlier Save-pipeline reports (view-once media, SAVE_TRACE, deep-save/album
  contracts, the Stash rename and multi-select) describe behaviour this change
  does not touch; their current contracts are in `AGENTS.md`.

---

## 7. Residual risk

* **A skipped paint is only as good as the next paint.** If the click queued
  behind a view fails before publishing, the panel keeps the previous view until
  the next click. State stays authoritative and every panel reads state, so no
  operation is affected — the visible marks can lag.
* **A paint racing a close is narrowed, not eliminated** (the close edit is not
  routed through the painter; window = one edit RPC; outcome = a stale,
  unclickable panel).
* **The residual read-per-click** keeps DB-backed bursts linear in N. Reducing it
  needs a read-only/mutating handler taxonomy or concurrent reads for ordered
  views — deliberately not introduced here.
* **Live proof is missing.** That a real client sees a 30-click burst answered
  immediately and the panel ending on the newest view must be observed in
  production; the report does not claim it.
