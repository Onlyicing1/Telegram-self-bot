# IMPLEMENTATION_REPORT.md — callback latency and burst behaviour

> This file describes ONLY the current state of the repository for this task and
> **replaces the previous report in full**. Earlier phases (callback
> acknowledgement + per-panel FIFO ordering, multi-select in the Stash browser,
> view-once media, SAVE_TRACE, the Save panel rename) are unchanged in behaviour;
> their history is in git, their contracts are in `AGENTS.md`, and the previous
> phase's verified findings are kept in §9 of this file.

| Item | Value |
|---|---|
| Task | Eliminate the remaining callback backlog: reduce end-to-end button latency, keep every valid click |
| Branch / base | `freebuff/99799eefb106784388e20bc8` at `5b34a3a` (= `origin/main` before this change) |
| Date | 2026-10-23 |
| Live Telegram / Supabase / Render | **NOT touched.** No session, no RPC, no SQL, no migration, no `.env`, no deployment |
| Documents not modified | `DATABASE_ARCHITECTURE.md`, `README.md` |
| Interpreter | CPython 3.12 venv, `telethon==1.43.0` (pinned, `backend/requirements.txt`) |
| New dependency | **none** |

---

## 1. Confirmed root cause

The previous phase fixed *when* a click is answered and *in what order* clicks on
one panel run. It left the cost of each click untouched, and the production
numbers say that cost is the problem: `ack_ms` 160–296, `wait_ms` ≤ 352,
`work_ms` 368–668, `total_ms` 528–1095, with low CPU.

**Confirmed cause: every click performed avoidable network round trips inside
the per-panel critical section, and every click performed its own edit.**

1. **A Stash tick cost four database round trips** — measured in source, not
   assumed. `_toggle_selected_item` called `retrieve_service.load_saved_item`
   (→ `db_client.query_save`, one Supabase query), then re-rendered the page via
   `_saved_page` → `db_client.list_saves`, which is itself TWO queries
   (`select("*") … range(...)` plus a `count="exact"`), i.e. **4 HTTP round
   trips per tick**. At ~120 ms per Supabase call from Render that alone is
   ~480 ms of `work_ms`, which matches the observed 368–668 ms.
2. **Every click rendered**, so N clicks produced N sequential
   `editMessage`/`editInlineBotMessage` round trips on ONE message — the
   "burst" the owner sees, and the exact pattern Telegram's rapid-edit handling
   penalises.
3. **The FIFO turn therefore had a long queue.** With ~0.5 s of work per click,
   the click behind it waits that long (`wait_ms` ≤ 352 ms observed), and a
   burst of 30 is a 15 s tail (measured: §4).
4. **The turn could be lost while waiting.** A fixed `DEFAULT_TURN_WAIT_S` bound
   drops a click that is queued behind clicks that are themselves making
   progress — a valid interaction lost for being 30th rather than wrong.

Not the cause (re-checked against the current source): event-loop blocking (the
Supabase layer is the bounded 4-worker `run_sync_db` pool), the AI handler (on
the self client's *outgoing message* path), the lifecycle lock, settings reads
(in-memory cache warmed at startup), and Telethon's dispatcher (`sequential_updates`
is not set; each update is its own task, verified at `updates.py:289`).

## 2. What changed

| File | Change | Why |
|---|---|---|
| `backend/helper/callback_gate.py` | new per-click stage counters (`ack_rpc_ms`, `db_calls/db_ms`, `edit_calls/edit_ms`, `state_ms`, `coalesced`), task→admission binding (`begin`/`note_*`), `pending_ahead()`, `turn_deadline()` (wait scales with the queue, capped at 120 s), `_Waiter.renders` | lets a click state what it actually cost, and lets a handler know a newer click is already queued to render |
| `backend/helper/panels.py` | `SKIP_RENDER` sentinel honoured by `_render_and_edit`; `_will_render()` classification; `acquire_turn(..., renders=…)`; `_safe_edit` reports its edit round trip; the router binds the admission after the ack | render coalescing without touching ordering; edit accounting at the ONE edit path |
| `backend/db/client.py` | `run_sync_db` attributes its round trip to the click being served (lazy import inside `try/except`, no-op for every other caller) | database cost is measured at the ONE dispatcher, not guessed per handler |
| `backend/bot/handlers/retrieve.py` | bounded per-owner page snapshot (`_snapshot_*`, 60 s TTL, 8 pages, invalidated on new session / cancel / bulk delete / failed lookup); `_saved_page(..., fresh=)`; ticks validate and label from the presented page and render from it; a tick with a queued click returns `SKIP_RENDER` | removes all four round trips per tick and all redundant edits |
| `backend/helper/__init__.py` | exports `SKIP_RENDER` | handler-facing API |
| `tests/test_callback_latency_burst.py` | **new** — 17 tests over the real router + real retrieve handlers (§5) | the burst, the savings, ordering, staleness, ownership, accounting |
| `tests/test_retrieve_multi_select.py` | the autouse fixture also clears the page snapshot | test hygiene: the snapshot is process state |
| `AGENTS.md` | callback admission contract updated (coalescing, snapshot trust model, instrumentation) | single source of truth |

No new dependency, no schema change, no migration, no SQL. No second
dispatcher, queue, executor, update loop or scheduler: Telethon still decides
what runs and when, and `RuntimeSupervisor → Dispatcher → ProviderManager /
ToolRegistry → ToolExecutor` is untouched.

## 3. Correctness guarantees

* **Every accepted click is applied exactly once, in arrival order.** The
  coalescing decision changes only whether a click *paints*, never whether it
  *acts*: `_toggle_selected_item` mutates authoritative state first, and only
  then decides to skip its render.
* **A skipped render is always replaced by a real one.** `SKIP_RENDER` is
  returned only when `pending_ahead() > 0` — i.e. a *live, accepted* click is
  already queued for the same panel. That counter deliberately ignores queued
  clicks that will not edit the panel (`panel:_nav:noop`, `panel:_nav:close`,
  anything unknown), and a coalescing refusal/notice (e.g. the 40-item cap)
  always renders so the owner sees it. A single click with nothing behind it
  always renders (`test_a_single_click_always_renders_its_own_view`).
* **No click is dropped for being queued.** The turn deadline is now
  `max(DEFAULT_TURN_WAIT_S, 20 s) + 2 s × (clicks ahead)`, capped at 120 s
  (Telegram's own callback lifetime). A 30-click burst is well inside the 20 s
  floor given §4's numbers; `dropped:panel-busy` is now reachable only by a
  genuinely stuck handler, and is still logged with its callback data.
* **Stale views cannot win.** The per-panel FIFO turn (previous phase) is
  unchanged, so a click's handler always sees the state left by earlier clicks,
  and only the newest click edits the message.
* **The snapshot cannot cross owners or invent items.** A snapshot entry is
  produced only by `db_client.list_saves(owner_id, …)` for that owner; a tick
  may only select a code the presented page contained; stale entries expire
  (60 s) and are dropped when the list changes (new session, cancel, bulk
  delete, failed lookup). `retrieve_service` still re-verifies owner + existence
  for **every** code before any bulk side effect, so the worst a stale snapshot
  can do is let the owner tick an item that has just vanished — which the bulk
  action then reports as a failure (`test_a_stale_snapshot_is_bounded_by_bulk_time_reverification`).
* **Duplicate delivery stays suppressed** by Telegram's `query_id`; two genuine
  taps remain two queries and both apply (both asserted).
* **Failures and cancellation still release the turn**, and the task→admission
  maps are pruned so a stray binding cannot outlive its click.

## 4. Performance

### 4.1 Comparable before/after benchmark

`logs/bench_callback_burst.py` (gitignored) drives the **production** router,
the **production** retrieve panel/action handlers and the **production**
selection state. Only the two ends are faked, at the latency in the live logs:
Telegram `answer` 180 ms, Telegram `edit` 250 ms, database call 120 ms. "Before"
is `HEAD` (`5b34a3a`) with the change stashed; "after" is this tree. 5
repetitions each, CPython 3.12, same machine.

| Burst | Before p50 | After p50 | Before p95 | After p95 | Δ p50 |
|---|---|---|---|---|---|
| 10 clicks | **5105.8 ms** | **684.0 ms** | 5113.8 ms | 684.9 ms | **−86.6 %** |
| 15 clicks | **7567.2 ms** | **683.4 ms** | 7570.4 ms | 684.2 ms | **−91.0 %** |
| 30 clicks | **14957.1 ms** | **684.8 ms** | 14966.7 ms | 685.4 ms | **−95.4 %** |

(Two independent post-fix runs agreed within ±1 ms; the table is the second one.
The "before" figures were each reproduced twice as well.)

| Burst | Edits before → after | DB calls before → after |
|---|---|---|
| 10 | 10.00 (1.00/click) → **2.00 (0.20/click)** | 20.00 (2.00/click) → **0.00** |
| 15 | 15.00 (1.00/click) → **2.00 (0.13/click)** | 30.00 (2.00/click) → **0.00** |
| 30 | 30.00 (1.00/click) → **2.00 (0.07/click)** | 60.00 (2.00/click) → **0.00** |

The two remaining edits are the first click's view and the last click's view
(the latter is the one that shows the final state); everything between them is
coalesced. The post-fix curve is **flat in burst size** (684.0 → 684.8 ms for
10 → 30 clicks): total time no longer grows with the number of presses.

Honest note on the "before" DB count: the benchmark charges one unit per
`db_client` function call. In production `list_saves` issues **two** SQL queries,
so the real pre-fix cost was 4 HTTP round trips per tick, and the measured
"before" numbers are therefore conservative (they understate the production
gap).

### 4.2 Deterministic in-suite measurements (fast bounds)

`tests/test_callback_latency_burst.py` uses 20 ms edits and 10 ms DB calls and
prints, per burst, the wall time, edits, DB reads and per-click state-application
latency (`pytest -s`):

```
[burst-10] total_ms=42.2  edits=2 db_reads=0 state_p50_ms=21.39 p95_ms=21.51 ack_max_ms=0.01
[burst-15] total_ms=43.0  edits=2 db_reads=0 state_p50_ms=21.93 p95_ms=22.25 ack_max_ms=0.02
[burst-30] total_ms=44.6  edits=2 db_reads=0 state_p50_ms=23.52 p95_ms=23.88 ack_max_ms=0.02
```

Against the pre-fix cost model used by the same test file, a 15-click burst was
15 × (2 × 10 ms DB + 20 ms edit) = 750 ms, so the regression bound
(`elapsed < 400 ms`, `edits ≤ 3`, `db_reads == 0`) fails long before the old
behaviour could return unnoticed.

### 4.3 Per-click instrumentation now emitted in production

```
[CALLBACK] timing data='action:retrieve_toggle:S0007' outcome=completed
  ack_ms=182.5 ack_rpc_ms=182.5 wait_ms=252.1 work_ms=0.1 total_ms=434.8
  db=0 db_ms=0.0 edits=0 edit_ms=0.0 state_ms=0.00 coalesced=1 backlog=3
```

Stages: receipt→ack (`ack_ms`), the answer RPC itself (`ack_rpc_ms`), ack→dispatch
(`wait_ms`, the time queued for the panel), dispatch→edit (`work_ms`), the whole
click (`total_ms`), plus the click's own `db` calls and ms, `edits` and ms,
`state_ms`, whether its render was coalesced, and the backlog it observed.
Counts are observed at the ONE dispatcher (`run_sync_db`) and the ONE edit path
(`_safe_edit`), so they are measurements, not estimates. Callback data is logged
truncated to 64 characters; no message content, credential or session data.

Ack latency is unchanged by this phase: it is one Telegram answer RPC
(`ack_rpc_ms`), and it is measured separately from the click's work precisely so
a network round trip is never presented as application latency.

## 5. Validation actually run

| Check | Result |
|---|---|
| `py_compile` on every changed module + both touched test files | **OK** |
| `pytest tests/test_callback_latency_burst.py -q` | **17 passed** |
| `pytest tests/test_callback_backlog.py tests/test_callback_latency_burst.py tests/test_retrieve_multi_select.py tests/test_retrieve_owner_isolation.py tests/test_symbols.py tests/test_colored_buttons.py -q` | **158 passed** |
| `pytest tests -q` (full suite) | **6785 passed, 27 skipped, 1 failed** |
| `git diff --check` | clean |

Coverage added by `tests/test_callback_latency_burst.py`:

* bursts of **10, 15 and 30** distinct valid ticks through the real router:
  each answered once, one `completed` record each, every transition applied in
  arrival order, final selection = the parity of the burst, **0 database reads**
  during the burst, ≤ 3 edits, and the last panel state agreeing with the
  authoritative selection (marks per row + the "N selected" counter);
* coalescing is explicit: ≥ 10 of 15 clicks record `coalesced`, and total edits
  ≤ 3;
* a lone tick always renders, with `edit_calls == 1` and `coalesced == 0`;
* duplicate delivery applies the tick once (and is still answered); two genuine
  taps both apply;
* a code outside the snapshot falls back to the verified lookup, reports
  "no longer in your saved items", discards the stale snapshot and re-reads the
  page without offering the vanished row; a code *inside* a stale snapshot costs
  no read and is re-verified by the service before any side effect;
* another owner never sees or toggles a snapshot row (verified lookup, refusal);
* a tick never opens an item panel and never leaves the Stash view;
* selection and the Send-Selected review agree after a burst;
* a failing lookup does not block the next click;
* the gate only counts queued clicks that will render (a queued noop cannot
  supersede a view), the render classification is exact, and the turn deadline
  scales then caps;
* with the **real** bounded DB dispatcher, a click's record reports
  `db_calls == 0` for a presented row and `edit_calls == 1`.

Full-suite failure: `tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`.
Re-run with this working tree stashed at `HEAD` (`5b34a3a`): **identical
failure**, and with the tree restored: identical failure. Pre-existing and
unrelated to the callback path.

## 6. Guarantee boundary (stated precisely)

A click that reaches the router is answered, recorded, and then either applied
exactly once or rejected with a logged reason. A click that never reaches the
process (dropped on the wire, process killed, helper disconnected) cannot be
accounted for. An answer refused by Telegram is recorded as `ack_error` on the
click that still runs. Coalescing can only skip a *paint* that a queued accepted
click is about to perform; it can never skip a state change.

## 7. Limitations / not verified

* **No live Telegram verification.** No real Helper Bot answered a callback, no
  button was pressed by an owner, no message was edited on Telegram, and no
  Supabase call was made. All measurements are application-level with fakes at
  chosen latencies; the production magnitudes will differ, especially `ack_ms`
  (one real answer RPC) and `db_ms` (real Supabase latency).
* **Telegram's rapid-edit throttling remains unconfirmed.** This phase reduces
  edits per burst by ~93 %, which addresses the pattern if it is being
  penalised, but no `FloodWaitError` was observed, so it is not claimed as a
  cause.
* **The snapshot is process-local and time-bounded.** A row deleted elsewhere
  stays selectable for up to 60 s; the bulk operation reports it honestly and
  nothing is transferred or deleted wrongly. Two processes serving one owner (not
  a supported deployment) would each keep their own snapshot.
* **Coalescing assumes a queued click completes.** If the click queued behind a
  tick fails before rendering, that panel keeps the previous view until the next
  click (state stays authoritative). Bounded and self-healing, not eliminated.
* **`wait_ms` still grows with burst size** for the clicks at the tail of a very
  large burst (30 clicks ≈ 0.4 s of queue behind the first edit). Reducing it
  further would mean accepting concurrent edits to one message, which is the
  corruption the previous phase removed.
* Panel telemetry stays in-process (`callback_gate.recent_timings()`, 64
  records); nothing is exposed on the web API.

## 8. Deliberately not done

* No render coalescing for arbitrary panels — only handlers whose view is a
  pure projection of state opt in (`SKIP_RENDER`), so a confirmation, error or
  sub-view always paints.
* No handler timeouts, no debounce window, no rate limit, no click debouncing.
* No change to rename / move / tag, which are single-item because each needs a
  per-item value.
* No caching of `query_save` results beyond the page snapshot; bulk operations
  keep re-reading every row.

## 9. Previous phase (kept: verified at `5b34a3a`)

Acknowledgement first (before any session lookup, database read, render or
edit), one FIFO turn per `(chat_id, msg_id)` with different panels never waiting
for each other, duplicate suppression by Telegram `query_id`, and one timing
record per click. Verified then: ack 210.8 ms → 0.0 ms with a 200 ms handler;
handler order `['2','3','1']` → `['1','2','3']`; peak concurrent handlers on one
panel 3 → 1. That phase's full-suite result was 6752 passed / 27 skipped / 1
failed (same pre-existing Taskloom test). Its contract is in `AGENTS.md` §5 and
its investigation in `INVESTIGATION.md`.
