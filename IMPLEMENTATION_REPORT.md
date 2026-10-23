# IMPLEMENTATION_REPORT.md — callback acknowledgement, ordering and accounting

> This file describes ONLY the current state of the repository for this task and
> **replaces every previous report in full**. Earlier work (multi-select in the
> Stash browser, view-once media, SAVE_TRACE, the Save panel rename) is
> unchanged in behaviour; its history stays in git and its contracts are still
> listed in `AGENTS.md`.

| Item | Value |
|---|---|
| Task | Diagnose and eliminate the Telegram button-callback backlog; low ack latency, zero lost valid clicks |
| Branch / base | `freebuff/99799eefb106784388e20bc8` at `7d4a838` (= `origin/main` before this change) |
| Date | 2026-10-23 |
| Live Telegram / Supabase / Render | **NOT touched.** No session, no RPC, no SQL, no migration, no `.env`, no deployment |
| Documents not modified | `DATABASE_ARCHITECTURE.md`, `README.md` |
| Interpreter | CPython 3.12 venv, `telethon==1.43.0` (pinned, `backend/requirements.txt`) |
| New dependency | **none** |

---

## 1. Symptom and confirmed root cause

**Symptom reported:** pressing ~10–15 inline buttons in quick succession makes the
interface unresponsive for a moment, after which the presses execute in a burst.

**Confirmed root cause (source + measurement, §5):** the callback path answered
Telegram *last* and let every click on the same panel run *concurrently*.

1. **The acknowledgement trailed the whole handler.** `panels.py` dispatched the
   panel/action handler, awaited its database reads, rendered, awaited the
   `editMessage` round trip, and only then — in the router's `finally` — called
   `event.answer()`. The client's progress indicator therefore stayed up for the
   entire handler, and Telegram expires that query (``BOT_RESPONSE_TIMEOUT``)
   long before a slow handler returns. Measured before the change, with a fake
   200 ms handler: **ack 210.8 ms**; with 15 clicks and a 50 ms handler:
   **ack 62.7 ms** — i.e. ack latency WAS handler latency.
2. **Clicks on the same panel ran at the same time.** Telethon dispatches each
   update as its own task (verified in the pinned source, `updates.py:289`), so
   nothing serialised them — but a panel is a state machine on ONE message. Two
   clicks that both read the view before either rendered produce two views from
   the same starting state, and the slower edit lands last. Measured before the
   change (3 concurrent clicks, the first deliberately slow): handler order
   **`['2', '3', '1']`** with **3 concurrent handlers** — the panel kept the
   FIRST click's view and the third click's render was overwritten.
3. **No duplicate-delivery suppression.** A query delivered twice ran its
   handler (and its side effects) twice.

The burst itself is the visible shape of (1)+(2): every press is answered late,
and the edits that finally arrive are the concurrent handlers finishing in
arbitrary order — a stall, then a clump of updates.

**Not the cause (checked, §2 of `INVESTIGATION.md`):** Telethon's update queue
(`sequential_updates` is left at its default `False`, so updates are dispatched
concurrently), event-loop blocking (every Supabase call goes through the bounded
4-worker `run_sync_db` pool; panel settings are cached in memory and loaded at
startup by the supervisor), and the lifecycle lock (it is only taken by
create/close, never by click dispatch).

**Not confirmed (honest boundary):** Telegram's own rapid-edit throttling is a
plausible *amplifier*. Fifteen concurrent edits to one message is exactly the
pattern official guidance warns about, and Telethon transparently sleeps up to
`flood_sleep_threshold` (60 s, `backend/bot/client.py`) on a flood answer — which
would look like this exact stall. That behaviour was **not** observed live and is
reported as a hypothesis, not a finding.

## 2. Execution path inspected

| Stage | Source |
|---|---|
| Update reception / dispatch | pinned `telethon/client/updates.py` (`_update_loop`, `_dispatch_update`, `task = self.loop.create_task(...)` line 289) |
| Client construction (no `sequential_updates`; `flood_sleep_threshold=60`) | `backend/bot/client.py`, `backend/helper/client.py` |
| Callback receipt hook (log only) | `backend/helper/client.py::register_helper_hooks` |
| Callback router, gates, dispatch | `backend/helper/panels.py::_callback_router` (line 367), `_handle_panel` (513), `_handle_action`, `_handle_input`, `_handle_navigation` |
| Panel/action/input registries | `backend/helper/panel_registry.py`, `backend/helper/panels.py` |
| Session/render state | `backend/helper/lifecycle.py`, `backend/helper/session_manager.py` |
| Panel editing (inline vs self) | `backend/helper/panel_edit.py` |
| Database dispatch | `backend/db/client.py::run_sync_db` (bounded 4-worker pool, `guarded_await` watchdog) |
| Settings read on every click | `backend/services/settings_service.py` (in-memory cache; `load_all()` at startup, `supervisor.py:178`) |
| AI handler (checked, not on this path) | `backend/bot/handlers/ai_unified.py` — registered on the **self** client for *outgoing messages*; callback queries arrive on the **helper** client |
| Tests | `tests/test_helper_callback_receipt.py`, `tests/test_task_reliability_repair.py`, and the new `tests/test_callback_backlog.py` |

## 3. What changed

| File | Change |
|---|---|
| `backend/helper/callback_gate.py` | **new** — callback admission (duplicate suppression by Telegram `query_id`), `ack()` (answer first), per-panel FIFO turns (`acquire_turn` / `release_turn`, bounded wait), and per-stage timing records (`CallbackAdmission`, `finish`, `recent_timings`) |
| `backend/helper/panels.py` | `_callback_router`: admit → **acknowledge** → gates → **per-panel turn** → re-check the panel still exists → dispatch → record outcome; the trailing `finally: await _safe_answer(event)` is gone (one answer per click, not two); every early-return gate records its outcome |
| `tests/test_callback_backlog.py` | **new** — 16 tests (§5) that also produce the before/after measurements |
| `AGENTS.md` | Glass UI section documents the callback admission contract |
| `IMPLEMENTATION_REPORT.md`, `INVESTIGATION.md` | this report and the root-cause investigation |

No new dependency, no database change, no migration, no change to Save, the AI
runtime, providers, the Taskloom scheduler, the runtime supervisor, or the
Telegram connection code. No second dispatcher, queue, executor or update loop
was introduced: Telethon still decides what runs and when, and the existing
`ToolExecutor`/service layer is untouched.

## 4. Concurrency and ordering guarantees

* **Acknowledgement first.** A delivered query is answered before any session
  lookup, database read, render or edit. Ack latency is one
  `messages.setBotCallbackAnswer` round trip, independent of the handler. A
  refused answer (expired query, disconnected helper) is recorded and logged,
  and the click is still dispatched — the answer is never a precondition.
* **One FIFO turn per panel.** Clicks on the same `(chat_id, msg_id)` run one at
  a time in arrival order through a ticket queue (no lock, no task per waiter —
  one future per waiting click). Clicks on **different** panels never wait for
  each other: a blocked panel cannot delay any other panel's click.
* **Bounded wait.** A click waits at most `DEFAULT_TURN_WAIT_S` (20 s) for its
  turn. If the turn cannot be taken it is dropped with `outcome=dropped:panel-busy`
  and a `WARNING` line naming the callback — a deliberate, traceable rejection,
  never a silent discard.
* **Closed-panel guard.** A click that queued behind another click re-checks the
  panel's session after acquiring its turn; if the earlier click closed the
  panel the queued click is dropped (`dropped:panel-closed`) and never edits a
  message that is gone.
* **Duplicate suppression.** The key is Telegram's own `query_id`, bounded to
  128 recent ids. The same query delivered twice runs its handler once (and is
  still answered); two genuine taps are two distinct query ids and both run.
  Suppression is exact, not a debounce window and not a rate limit.
* **Cancellation and failure release the turn.** `CancelledError` propagates
  after removing the waiter (or handing the granted turn on); exceptions are
  logged and the turn is released in the router's `finally`. A stuck handler can
  hold only its own panel's turn.
* **Ordering preserved where it matters.** A click's handler always sees the
  state left by every earlier click on that panel, so "next page" twice advances
  two pages and a confirm click acts on the review the owner actually saw.
  Independent panels, background flows and Telegram RPCs remain concurrent.

## 5. Measurements

Same harness, same fake RPCs (`answer` = 1 event-loop tick, `edit` = 10 ms),
same machine, CPython 3.12. "Before" = the pre-fix router at `HEAD` (`7d4a838`),
measured with the change stashed; "after" = this working tree.

| Scenario | Before | After |
|---|---|---|
| Ack latency, single click, 200 ms handler | **210.8 ms** | **0.00 ms** (one answer RPC; the 200 ms is `work_ms`) |
| Ack latency, burst of 15, 50 ms handler (max over clicks) | **62.7 ms** | **0.79 ms** |
| Handler order, 3 concurrent clicks on one panel (first one slow) | **`['2','3','1']`** (wrong) | **`['1','2','3']`** |
| Peak concurrent handlers on one panel | **3** | **1** |
| Wall time for the 15-click burst | 62.7 ms | 758.0 ms |

The last row is the deliberate cost and it is reported plainly: clicks on ONE
panel are now a sequence (15 × (50 ms handler + 10 ms edit)), because running
them concurrently is what corrupted the view and what produced the clump of
edits. What the owner perceives changes from "frozen, then a burst" to "every
press acknowledged instantly, updates landing in order". Clicks on different
panels are still fully concurrent (asserted by
`test_independent_panels_progress_while_one_is_blocked`).

## 6. How every accepted click is accounted for

Each click creates one `CallbackAdmission` and exactly one timing record. The
`outcome` is one of:

`completed` · `failed` · `cancelled` · `rejected:no-coordinates` ·
`rejected:not-owner` · `rejected:empty-data` · `rejected:no-session` ·
`rejected:unknown-prefix` · `dropped:panel-busy` · `dropped:panel-closed`

and one INFO line reports the measured stages:

```
[CALLBACK] timing data='panel:retrieve_saved' outcome=completed ack_ms=0.4
           wait_ms=0.0 work_ms=41.2 total_ms=41.6
```

`ack_ms` (receipt → answer), `wait_ms` (answer → dispatch, i.e. time queued for
the panel), `work_ms` (dispatch → edit attempted: handler work and UI response),
`total_ms`. Callback data is logged truncated to 64 characters — the same
bounded identity the router and the helper receipt hook already logged. No
message content, credential or session data is logged.

Guarantee boundary, stated precisely: a click that reaches the router is
answered, recorded, and either executed exactly once or rejected with a logged
reason. A click that never reaches the process (dropped on the wire, process
killed, helper disconnected) cannot be accounted for, and an answer refused by
Telegram is recorded as `ack_error` on the click that still runs. No claim is
made beyond that.

## 7. Validation actually run

| Check | Result |
|---|---|
| `py_compile` on `backend/helper/callback_gate.py`, `backend/helper/panels.py`, `tests/test_callback_backlog.py` | **OK** |
| `pytest tests/test_callback_backlog.py -q` | **16 passed** |
| `pytest tests/test_task_reliability_repair.py tests/test_helper_callback_receipt.py tests/test_retrieve_multi_select.py -q` | **85 passed** |
| `pytest tests -q -p no:randomly` (full suite) | **6768 passed, 27 skipped, 1 failed** |
| `git diff --check` | clean |

Focused coverage added by `tests/test_callback_backlog.py`:

* 15 clicks arriving together: each answered once, each handler run exactly
  once, 15 `completed` records, ack never waiting for the 50 ms handler;
* ack latency excludes slow work (200 ms handler, ack < 50 ms, `work_ms` ≥ 200);
* same-panel clicks execute in arrival order with no overlap, and the last
  handler's view is the one left on the panel;
* an independent panel completes while another panel's handler is blocked;
* the same query delivered twice runs the handler once but is still answered;
* two distinct clicks on the same button both run;
* a click queued for a panel closed meanwhile is dropped and edits nothing;
* a raising handler does not block the next click;
* a cancelled handler releases the turn and the next click completes;
* a refused ack is recorded, and the click still dispatches;
* an unobtainable turn drops the click with `dropped:panel-busy` after the bound;
* action dispatch still receives its `extra` and renders; `panel:_nav:noop`
  renders nothing; `panel:_nav:home` still renders the menu;
* unauthorized and unknown-panel clicks are answered and edit nothing;
* per-stage timings are monotonic and the record buffer is bounded.

Full-suite failure: `tests/test_task_scheduler.py::
test_recovered_interrupted_occurrence_is_picked_up_by_run_once`
("interval advancement exceeded bound"). It was re-run with this working tree
stashed at `HEAD` (`git stash push -u` → same failure → `git stash pop`), so it
is pre-existing and unrelated.

## 8. Limitations / not verified

* **No live Telegram verification.** No real Helper Bot answered a callback, no
  button was pressed by an owner, no message was edited on Telegram. Every RPC
  in the tests and in the measurements above is a fake with a chosen latency.
  The measured improvements are therefore the click path's own structure, and
  the production magnitude of `ack_ms` will be one real answer RPC.
* **Flood/rapid-edit throttling is unconfirmed** (§1). If production logs show
  `FloodWaitError` on `EditInlineBotMessage`, that is the amplifier to address
  next — the serialised per-panel edit rate is already the recommended remedy,
  but no claim is made that it was observed.
* **Ordering is per panel process-locally.** Two processes serving the same
  owner (not a supported deployment) would each have their own turns.
* **A handler that never returns holds its own panel's turn** until the waiter's
  bound expires; queued clicks for that panel are then dropped with a reason.
  No handler timeout/cancellation was added, because cancelling panel work
  mid-flight could interrupt a destructive action that has already started.
* Panel telemetry is in-process only (`callback_gate.recent_timings()`, 64
  records); nothing is exposed on the web API.
