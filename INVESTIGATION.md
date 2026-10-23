# INVESTIGATION — Telegram button-callback backlog

| Item | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Worktree branch | `freebuff/99799eefb106784388e20bc8` |
| **HEAD at investigation start** | **`7d4a8381088c9f292f533a909469e9a83e3b94c8`** (`fix(retrieve): route multi-select ticks through an action`), equal to the remote tip this clone fetched (`git rev-parse FETCH_HEAD` → `7d4a838…`; the clone keeps no local `origin/main` ref) |
| Working tree at start | clean |
| Date | 2026-10-23 (UTC) |
| Symptom under investigation | ~10–15 rapid inline-button presses leave the UI unresponsive for a moment, then the presses execute in a burst |
| Scope | the callback path only: update reception → acknowledgement → dispatch → action execution → message edit, plus the pinned Telethon release's dispatch behaviour |
| Explicitly out of scope | Save pipeline semantics, the AI runtime, Taskloom scheduling, Supabase schema, Render, any live account |
| **Status** | **Root cause CONFIRMED for three application defects; one amplifier hypothesis left explicitly UNCONFIRMED.** Implementation and tests are in `IMPLEMENTATION_REPORT.md`; nothing was sent to Telegram, no credential was read, no SQL was executed |

### Method and its limits

* Every repository claim was re-read from the working tree at `7d4a838` (or, for
  the "before" numbers, from the same tree with the fix stashed).
* The pinned client library `telethon==1.43.0` was read **in the workspace venv**
  (`telethon/client/updates.py`, `telethon/client/messages.py`) — the same wheel
  deployment resolves, not a description of it.
* Timing evidence comes from a harness that installs the **real** callback
  router (captured through the same stubbed `client.on` the repository's other
  helper tests use) and drives it with fake events whose `answer()`/`edit()`
  latencies the harness chooses. It measures the application's structure, never
  a network.
* Consequence of that limit: the numbers prove *what the click path waits on*.
  They cannot prove *what Telegram returns for these panels* — which is why the
  rapid-edit throttling question stays open.

---

## 1. Executive summary

### Verdict: **CONFIRMED — three application defects, measured**

| # | Finding | Status |
|---|---|---|
| 1 | The callback query was acknowledged **after** the handler (session lookup, DB read, render, edit round trip). Ack latency = handler latency: **210.8 ms** with a fake 200 ms handler, **62.7 ms** in a 15-click burst with a 50 ms handler | **CONFIRMED** — source + measurement |
| 2 | Clicks on the **same panel** ran concurrently and reordered: with the first of three clicks deliberately slow, handler order was **`['2','3','1']`**, peak **3** concurrent handlers, and the panel kept the *first* click's view | **CONFIRMED** — source + measurement |
| 3 | A query delivered twice would run its handler — and its side effects — twice; the router had no identity for the query at all | **CONFIRMED** — source |
| 4 | Telegram's rapid-edit throttling (and Telethon's transparent `FLOOD_WAIT` sleep up to `flood_sleep_threshold=60`) amplifies the stall into exactly this "freeze then burst" shape | **UNCONFIRMED HYPOTHESIS** — plausible, unmeasured |
| 5 | Telethon serialises updates, or the event loop is blocked, or a lock serialises clicks | **RULED OUT** — see §2 |

Defects 1+2 together produce the reported symptom without needing any of them to
be a "slow network": every press is answered late, and the edits that do arrive
carry whichever concurrent handler finished last — a stall followed by a clump.

---

## 2. What was ruled out, and how

**Telethon does not serialise updates.** `telethon/client/updates.py` builds one
task per update when `sequential_updates` is false (the default), at line 289:

```python
task = self.loop.create_task(self._dispatch_update(updates_to_dispatch.popleft()))
```

and `_update_loop` does not wait for those tasks. Both client factories in this
repository (`backend/bot/client.py`, `backend/helper/client.py`) omit
`sequential_updates`, so the default holds for the helper bot that receives the
callbacks. A slow handler therefore does **not** delay the next click's
delivery — which is why no queue/backpressure defect exists to fix on that side.

**The event loop is not blocked by the click path.** The only I/O on the path is
(a) the database and (b) Telegram. Every Supabase call goes through
`backend/db/client.py::run_sync_db`, which runs the synchronous client on a
dedicated 4-worker pool with a watchdog timeout — not on the loop. Panel settings
are read from an in-memory cache (`settings_service`), warmed at startup by
`supervisor.py:178`, so no settings query happens on a click. `is_owner()` is a
comparison of `event.sender_id`. No `time.sleep`, no synchronous HTTP and no
CPU-heavy work sits between update receipt and the edit.

**The lifecycle lock is not on this path.** `PanelLifecycleManager._lock` is
taken by `create_panel`, `try_reuse_panel`, `close_panel`, timer expiry and
shutdown. Click dispatch takes no lock at all — which is the other half of
defect 2: nothing ordered two clicks on one panel.

**The AI handler is irrelevant here.** `backend/bot/handlers/ai_unified.py`
registers on the **self** client and fires on outgoing messages; callback queries
arrive on the **helper** client. It was inspected because the task named it, and
then set aside on evidence.

---

## 3. The measured defects

### 3.1 Acknowledgement behind the handler

`panels.py::register_callback_handlers` dispatched to `_handle_panel` /
`_handle_action` / `_handle_input` and answered in the router's `finally`. The
panel handler performs the database read and the `editMessage` round trip; the
answer therefore carried all of it. Telegram expires a callback query
(`BOT_RESPONSE_TIMEOUT`); past that point even a correct answer is refused, and
the click has already done its work invisibly.

### 3.2 Concurrent, unordered handlers on one panel

Each click is its own task, so N clicks on one message are N concurrent handlers
that each read the *current* session and render a view from it, and then N
concurrent edits race. Two `Next` taps both read page 1; the slower edit lands
last. The harness reproduced the reordering on the pre-fix router:

```
[before ordering] order=['2', '3', '1'] peak_concurrent_handlers=3
```

The same mechanism explains why a burst looks like a clump of updates: the edits
arrive when their handlers finish, not in the order the buttons were pressed.

### 3.3 No duplicate-delivery identity

Nothing in the router read `event.query.query_id`, the only identifier that
distinguishes "this query again" from "the owner pressed the same button again".

---

## 4. The open hypothesis (not a finding)

Telegram publishes no edit-rate quota, but community guidance warns that very
frequent edits of one message are throttled, and this repository sets
`flood_sleep_threshold=60` on both clients, so Telethon **sleeps inside the call**
on a flood answer and retries transparently. Fifteen concurrent edits of one
message is precisely that pattern, and a set of handlers sleeping out a flood
window would produce a freeze followed by a burst — the reported symptom exactly.

It is left unconfirmed because it needs production evidence
(`FloodWaitError` / `FLOOD_WAIT_*` on `EditInlineBotMessage` in the runtime logs).
The chosen fix removes the concurrent-edit pattern anyway (one edit at a time per
panel), which is the recommended remedy regardless; the report says so without
claiming the throttle was observed.

---

## 5. Why the fix takes the shape it does

* **Answer first, work second** — the acknowledgement is the only thing the
  client waits on. It is now the first await in the router
  (`callback_gate.ack`), before every gate that can do I/O. A refused answer is
  recorded and the click still runs, because the answer is a courtesy, not a
  precondition.
* **One FIFO turn per panel** — correctness requires ordering on one message
  (a panel is a state machine; "next" twice must advance twice), and Telegram's
  own guidance for repeated edits of one message is to serialise them. The turn
  is keyed by `(chat_id, msg_id)` only, so independent panels stay concurrent —
  no global lock, no queue, no second dispatcher.
* **Ticket queue, not a lock, and not a task per waiter** — `asyncio.Lock` plus
  `wait_for` has an acquire-at-timeout edge that can leave the lock held. The
  gate instead parks one future per waiting click (bounded by the clicks already
  delivered), grants the oldest, and on timeout removes itself *or* hands the
  already-granted turn on. Nothing is spawned, nothing leaks.
* **Bounded wait instead of unbounded growth** — the 20 s bound turns "the panel
  is stuck" into a logged, traceable drop (`dropped:panel-busy`) rather than an
  ever-growing backlog of stale clicks. Every other outcome is recorded too.
* **`query_id` for identity** — exact suppression for a re-delivered query, and
  none for a genuine double tap. No debounce window, no rate limit, no click
  discarded to make the bot look faster.
* **No handler timeout** — cancelling a handler mid-flight could interrupt a
  destructive action that has already started; the turn is released on
  exception and on cancellation, and a handler that never returns can only hold
  its own panel.

---

## 6. Historical investigations (kept, superseded, not relied on)

* **2026-10-22 — "can the Helper Bot edit an existing inline panel
  asynchronously?"** That investigation's verdict was *UNRESOLVED* because the
  repository captured no runtime evidence that Telegram hands the Helper Bot an
  inline-message identifier for these panels. Its verified library facts (the
  pinned client supports `messages.editInlineBotMessage` through
  `edit_message(InputBotInlineMessageID, …)`, and the Helper Bot — as the
  message's `via_bot` — is the principal that may use it) remain true and are
  what `backend/helper/panel_edit.py::ExistingPanelEditor` is built on today; the
  Save progress/finished panels use that path with a generation token and fall
  back to the owner's own `editMessage`. Whether Telegram *supplies* the
  identifier for these panels is still proven only in production, not by unit
  tests — this task neither relies on it nor changes it.
* Earlier Save-pipeline reports (view-once media saved as ordinary media,
  SAVE_TRACE instrumentation, deep-save/album contracts, the Stash rename and
  Stash multi-select) describe behaviour that this change does not touch. They
  live in git history and their current contracts are summarised in `AGENTS.md`.

---

## 7. Residual risk

* The per-panel turn adds up to one handler's duration of latency to a *later*
  click on the same panel. That is inherent to ordering; the click is still
  acknowledged immediately.
* The duplicate memory is process-local and bounded (128 query ids); a restart
  forgets it. Telegram does not re-deliver a callback after a restart, so this
  is a non-issue in the documented deployment (one process).
* Live proof — that a real Helper Bot answers under a 15-click burst and that the
  final panel shows the last click's view — requires the owner's Telegram
  session. That is listed as unverified in `IMPLEMENTATION_REPORT.md` §8 and must
  not be reported as verified until it is observed.
