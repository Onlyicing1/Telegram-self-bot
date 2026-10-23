# IMPLEMENTATION_REPORT.md — whole-runtime responsiveness of the callback path

> This file describes ONLY the current state of the repository for this task and
> **replaces the previous report in full**. Earlier phases (callback
> acknowledgement + per-panel ordering; the Stash page snapshot) are unchanged
> in behaviour where they still apply; their history is in git, their contracts
> are in `AGENTS.md`, and the parts this phase supersedes are marked in §3 and
> §9.

| Item | Value |
|---|---|
| Task | Whole-self-bot responsiveness: eliminate the remaining click backlog, keep every valid action |
| Branch / base | `freebuff/99799eefb106784388e20bc8` at `a4b6e4f` (= `origin/main` before this change) |
| Date | 2026-10-23 |
| Live Telegram / Supabase / Render | **NOT touched.** No session, no RPC, no SQL, no migration, no `.env`, no deployment, no Render shell |
| Documents not modified | `DATABASE_ARCHITECTURE.md`, `README.md` |
| Interpreter | CPython 3.12 venv, `telethon==1.43.0` (pinned) |
| New dependency | **none** |

---

## 1. Observed symptom and the evidence actually available

**Reported by the owner:** after rapid inline-button presses the self-bot still
freezes and then applies the presses in a burst, *after* the previous callback
optimisation shipped.

**Evidence obtainable here:** this workspace has no Render log access and no
Telegram session, so production logs could not be re-read. What was available
and is used throughout:

* the current source at `a4b6e4f` (every claim below is traced to a line of it);
* the previous phase's live-log numbers, which remain the only production
  measurements on record: `ack_ms` 160–296, `wait_ms` ≤ 352, `work_ms` 368–668,
  `total_ms` 528–1095, low CPU;
* a local harness that installs the **real** router, the **real** panel
  handlers and the **real** lifecycle/gate, with only the two network ends
  faked at the log-implied latencies (answer 180 ms, edit 250 ms, Supabase call
  120 ms).

**Reproduced locally, deterministically** (real router + real handlers, three
repetitions, `logs/bench_panel_burst.py`):

| Panel | 15 rapid clicks, before | Edits | DB calls |
|---|---|---|---|
| `menu` (no DB work) | **3943 ms** | 15 | 0 |
| `health` (no DB work) | **3946 ms** | 15 | 0 |
| `retrieve_saved` (one DB read per click) | **5753 ms** | 15 | 15 |
| 30 clicks on `menu` | **7707 ms** | 30 | 0 |

That is the reported symptom in numbers: half a second of work per press,
serialised, so a burst keeps the panel updating for seconds. It is not the
acknowledgement (that was fixed and stays at one answer RPC) and it is not CPU.

## 2. Root cause, ranked by confidence

**1 — CONFIRMED (source + measurement). Every click painted its own view inside
the panel's FIFO turn.** `_callback_router` held the per-panel turn across
`handler → render → event.edit`, and every handler returns a view. So N clicks
on one message meant N serialised Telegram edits, and every click behind the
first waited for an edit it was about to overwrite. Measured: 15 clicks where
the handler is trivial still cost 15 × 250 ms = 3.9 s, with 15 edits.

**2 — CONFIRMED (source + measurement). The previous fix coalesced only one
handler.** `SKIP_RENDER` was opt-in and only `retrieve`'s multi-select tick
returned it, so every other panel (Menu, Save, Settings, Health, Item, Taskloom,
Cipher, Emoji, …) kept the paint-per-click behaviour of (1). The mechanism was
right; its scope was one handler.

**3 — CONFIRMED (source + measurement). The lifecycle lock was held across a
Telegram round trip.** `PanelLifecycleManager.close_panel` / `timeout_panel` ran
`_cleanup_locked(edit_message=True)` inside `async with self._lock`, and
`create_panel` / `try_reuse_panel` hold the same lock across
`inline_engine.trigger` plus an edit. A Close click (or an auto-close timer)
therefore serialised every unrelated panel create/close in the process for the
length of one RPC. Measured: opening a panel while a Close's edit is in flight
took **230.6 ms** before this change.

**4 — CONFIRMED, bound-checked, not fixed (out of scope). A DB-backed panel
re-reads the database once per click inside the turn.** With ordered views on
one message that is inherent (see §5 and §7): 30 clicks on `retrieve_saved`
still cost 30 sequential reads ≈ 3.6 s of the 4.1 s total. Removing it would
mean concurrent reads for ordered views, which is the corruption phase 1
removed.

### Checked and NOT the cause (this phase, against current source)

| Candidate | Evidence |
|---|---|
| Telethon serialising updates | `telethon/client/updates.py:289` — one task per update; `sequential_updates` unset on both clients |
| Event-loop blocking (sync I/O, CPU) | Only two `time.sleep` sites in the runtime (`speechmatics_stt_engine._sleep_briefly`, `gemini_media_engine._await_file_ready`); both are inside synchronous engine methods documented and called through `asyncio.to_thread` (`stt_provider_probe.py:329`, media engines). No `requests`, `subprocess`, `.result()` or `run_until_complete` on a request path |
| Database pool saturation as the burst cause | Bounded 4-worker `run_sync_db` pool; a *saturated* pool is now covered by a test that proves it degrades to a bounded `OperationTimeoutError` and does not stop an unrelated panel from answering and painting |
| Duplicate callback delivery | Suppressed by Telegram `query_id` since phase 1 (re-observed in the harness: a probe that reused query ids had 15 clicks suppressed as duplicates) |
| Heartbeat / keepalive / failsafe | They do no database work and never take the panel turn (`heartbeat.py`, `keepalive.py`, `failsafe.py`) |
| `callback_trace` flush lock | The module is not imported by any runtime path (dead code, untouched) |
| AI handler / providers / tool executor | `ai_unified` fires on the **self** client for outgoing messages; provider calls are `httpx` awaits in their own task, behind `ProviderManager` semaphores that bound *AI* concurrency only. A slow provider cannot block a click (asserted by the saturated-pool/no-DB-panel test's shape and by the pre-existing independent-panel test) |
| Task accumulation / unbounded queues | The turn is one future per waiting click (bounded by delivered updates); painters are one per active panel and are forgotten on close; timing/duplicate stores are bounded (64/128) |

## 3. Why the previous callback optimisation was insufficient

Phase 1 stopped a click from waiting on its **own** work (acknowledgement) and
stopped two clicks from **corrupting** one panel (FIFO + `query_id`). Phase 2
removed the redundant **reads and paints of one handler** (Stash ticks). Neither
changed the structural fact that the FIFO turn spanned the Telegram edit, and
neither applied to the other ~20 panels. A burst on any non-Stash panel
therefore still cost one edit round trip per click, serialised — and on a panel
that reads the database, one read per click as well. The 15-click figure in this
phase's predecessor report (that serialising is "the deliberate trade") is
superseded: the paint was never part of the ordering guarantee.

## 4. Fixes implemented

| File | Change |
|---|---|
| `backend/helper/callback_gate.py` | **`PanelPainter`** + `painter()` / `forget_painter()`: one writer per message, one pending slot, newest view wins, never two concurrent edits on one message, `discard()` on close. `CallbackAdmission` gains `handled_at` / `paint_ms` (so `work_ms` is the handler's own cost, never a network round trip the click may not have made). `note_coalesced` retained. `pending_ahead` / `_Waiter.renders` removed (the painter subsumes them). |
| `backend/helper/panels.py` | The turn is released in a `finally` **before** the paint; dispatch helpers (`_handle_panel`, `_handle_navigation`, `_handle_action`) now *return* `(panel_id, result)` instead of painting; new `_present()` publishes the view to the painter and paints outside the turn; `_handle_input`'s prompt is painted through the same writer; new `dispatch_click()` = handler + paint for callers that already own the click (inline entry point, tests); `SKIP_RENDER` and `_will_render` removed; `close_panel` forgets the writer. |
| `backend/helper/lifecycle.py` | `_cleanup_locked` split into the synchronous `_cleanup_state_locked` (all state, no I/O) and `_close_message` (the edit + delayed delete). `close_panel` / `timeout_panel` take the lock only for the state transition. |
| `backend/bot/handlers/retrieve.py` | The Stash tick no longer needs `SKIP_RENDER`: it always builds its view and the panel writer coalesces it. Page snapshot from the previous phase unchanged. |
| `backend/helper/__init__.py` | `SKIP_RENDER` export removed. |
| tests | `tests/test_callback_latency_burst.py` reworked (painter semantics, failed paint, slow paint, closed panel, saturated pool, slow close); 11 existing suites updated from "the handler paints" to the `dispatch_click` contract; `tests/test_callback_backlog.py` assertions made independent of *who* painted. |
| `AGENTS.md` | Callback contract updated (turn covers the handler only; single-writer painting; lifecycle lock never spans an RPC). |

No new dependency, no schema change, no SQL, no migration. No second
dispatcher, scheduler, executor or update loop: `RuntimeSupervisor → Dispatcher
→ ProviderManager / ToolRegistry → ToolExecutor` is untouched, and the painter
is a slot plus the click's own await, not a task.

### Guarantees

* **Ordering:** the FIFO turn still serialises every handler on one panel, so a
  handler always sees the state left by the previous click. Ordering-sensitive
  panels (`Next`/`Prev`, Save's type selection, Taskloom steps, Cipher) keep
  their existing semantics.
* **Every accepted click is applied exactly once.** The painter can only skip a
  *paint*; state changes and side effects happen in the handler, inside the
  turn, exactly as before. A superseded view is counted (`coalesced=1`).
* **The newest view is the one on screen.** `drain()` always takes the newest
  pending view, so a stale view is never painted after a newer one.
* **No concurrent edits to one message.** One writer per message; a second click
  publishes into the slot instead of starting a rival edit.
* **A closed panel is never painted.** `forget_painter` discards the slot and
  stops the drain loop; the paint also re-checks `closed` before editing.
* **A wrapper for `_handle_input` prompts** keeps the prompt behind any in-flight
  view (the prompt is the newest view, so it lands last).
* **Failure is honest:** a raising handler is recorded as `failed` (previously
  the dispatch helpers swallowed it and reported `completed`); a raising edit is
  recorded and the panel is not wedged (next click paints).

## 5. Measurements (mocked network, identical workload before/after)

`logs/bench_panel_burst.py`: real router + real handlers, 3 repetitions per
cell, CPython 3.12, same machine. "Before" = `a4b6e4f` with this change stashed;
"after" = this tree. Network latencies are **fixed fakes** (answer 180 ms, edit
250 ms, DB call 120 ms); nothing below was measured against Telegram or
Supabase.

| Panel (work per click) | Burst | Before p50 → After p50 | Before p95 → After p95 | Δ | Edits before → after |
|---|---|---|---|---|---|
| `menu` (no DB) | 10 | 2689.6 → **682.9 ms** | 2690.0 → 683.9 | −74.6 % | 10 → **2** |
| `menu` | 15 | 3943.4 → **682.5 ms** | 3943.5 → 682.8 | −82.7 % | 15 → **2** |
| `menu` | 30 | 7706.9 → **682.5 ms** | 7711.8 → 682.7 | −91.1 % | 30 → **2** |
| `health` (no DB) | 15 | 3945.5 → **682.9 ms** | 3946.7 → 683.1 | −82.7 % | 15 → **2** |
| `retrieve_saved` (1 DB read/click) | 10 | 3896.0 → **1806.5 ms** | 3896.2 → 1808.3 | −53.6 % | 10 → **6** |
| `retrieve_saved` | 15 | 5752.7 → **2310.6 ms** | 5753.2 → 2311.5 | −59.8 % | 15 → **8** |
| `retrieve_saved` | 30 | 11327.1 → **4068.7 ms** | 11327.4 → 4316.1 | −64.1 % | 30 → **15.3** |
| `retrieve_item` (1 DB read/click) | 15 | 5753.6 → **2309.7 ms** | 5755.1 → 2311.2 | −59.9 % | 15 → **8** |
| Open a panel while a Close's edit is in flight | — | 230.6 → **0.1 ms** | — | −99.9 % | 1 → 1 |

Reading the table: on a panel whose handler does no I/O, a burst is now
**constant** (682 ms for 10, 15 and 30 clicks — one ack round trip plus two
paints), because only the paints were ever the cost. On a panel that reads the
database per click the paint saving is the same, but the serialised reads remain
(§7). The Stash multi-select path keeps its previous result (15 ticks: 684 ms,
2 edits, 0 DB reads) because the page snapshot is still in place.

The in-suite deterministic numbers (20 ms edit, 10 ms DB, `pytest -s`) remain:

```
[burst-10] total_ms=42.2  edits=2 db_reads=0 state_p50_ms=21.39 p95_ms=21.51 ack_max_ms=0.01
[burst-15] total_ms=43.0  edits=2 db_reads=0 state_p50_ms=21.93 p95_ms=22.25 ack_max_ms=0.02
[burst-30] total_ms=44.6  edits=2 db_reads=0 state_p50_ms=23.52 p95_ms=23.88 ack_max_ms=0.02
```

## 6. Tests

| Check | Result |
|---|---|
| `py_compile` on every changed module and test file | **OK** |
| `pytest tests/test_callback_latency_burst.py -q` | **21 passed** |
| `pytest` focused callback/panel suites (backlog, burst, multi-select, owner isolation, receipt, cipher, emoji, save success panel, taskloom input/editor, symbols, save engine, bio, ghost-seen, ai stt, task reliability) | **460 passed** |
| `pytest tests -q` (full suite) | **6789 passed, 27 skipped, 1 failed** |
| `git diff --check` | clean |

Coverage added/changed for this phase:

* burst of 10/15/30 ticks: every click applied once, ≤ 3 edits, 0 DB reads, final
  panel agrees with authoritative state (`_last_paint` so the assertion no longer
  assumes *which* click painted);
* painter: newest-wins, one pending slot, no second painter task, `closed`
  discard, failed paint does not wedge, a slow paint does not hold the next
  click's handler (`work_ms < 100 ms` while an edit is in flight);
* saturated DB pool: a 5th call is bounded by the watchdog
  (`OperationTimeoutError`) instead of hanging, and an unrelated panel still
  answers and paints during saturation;
* a slow Close does not block opening another panel (`< 0.5 s`, measured 0.1 ms);
* lifecycle/ordering regressions: all existing panel, navigation, Taskloom,
  Cipher, Emoji, Save-success and input-prompt suites pass unchanged apart from
  the dispatch entry point.

Full-suite failure: `tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`.
Re-run with this tree stashed at `a4b6e4f` → identical failure; with the tree
restored → identical failure. Pre-existing, unrelated to the callback path.

## 7. Remaining limitations and unverified hypotheses

* **No production evidence was available in this workspace.** The Render logs
  the owner referred to could not be read here, no Telegram RPC was made, and the
  measurements above are the application's own structure with faked network
  latency. They are *not* production performance.
* **The residual serialised read.** For panels that load from the database on
  every click, a burst still costs ≈ N × read (30 clicks ≈ 3.6 s of the 4.1 s).
  Ordered views on one message require it; removing it needs a read-only/mutating
  taxonomy or concurrent reads for ordered views, which this change deliberately
  does not introduce.
* **Telegram rapid-edit throttling** remains an unconfirmed amplifier. Edits per
  burst are down 87–93 % on I/O-free panels, which reduces the exposure but is not
  proof that a `FLOOD_WAIT` was ever returned.
* **A paint racing a close is narrowed, not eliminated.** The close edit is not
  routed through the painter; the window is one edit RPC and the outcome is a
  stale (unclickable) panel.
* **Live confirmation is missing:** that a real Helper Bot answers a 30-click
  burst, that the panel ends on the newest view, and the real `ack_ms`/`db_ms`
  must be observed in production logs. Nothing in this report should be read as
  "the live freeze is fixed".

## 8. Deliberately not done

* No second dispatcher/queue/executor and no task per click.
* No debounce window, no rate limit, no dropped clicks to make the bot look
  faster (the turn's queue is still drained in full; only paints coalesce).
* No concurrent edits to one message, and no concurrent handlers on one panel.
* No changes to Save semantics, the AI runtime, Taskloom scheduling, Supabase
  schema or `DATABASE_ARCHITECTURE.md`.

## 9. Superseded statements from earlier phases

* Phase 2's claim that a serialised burst is necessarily slower than
  `N × (read + edit)` is superseded: the edit was never part of the ordering
  guarantee, and a burst on an I/O-free panel is now constant-time.
* Phase 2's `SKIP_RENDER` opt-in is replaced by the painter (one mechanism for
  every panel). The Stash page snapshot it came with is unchanged.
* Phase 1/2's "each click's `work_ms` includes the UI response" is superseded:
  `work_ms` is now handler-only and the paint is reported separately as
  `paint_ms`.
