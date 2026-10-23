# INVESTIGATION — Telegram button-callback latency and bursts

| Item | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Worktree branch | `freebuff/99799eefb106784388e20bc8` |
| **HEAD at investigation start** | **`5b34a3ae04f53d11a97bdd58f5f5ff0779291715`** (`fix(callbacks): acknowledge clicks first and order them per panel`), equal to the remote tip this clone fetched (`git rev-parse FETCH_HEAD` → `5b34a3a…`; the clone keeps no local `origin/main` ref) |
| Working tree at start | clean |
| Date | 2026-10-23 (UTC) |
| Symptom under investigation | ~10–30 rapid inline-button presses still feel unresponsive and land late, although acknowledgement and per-panel ordering were already fixed |
| Scope | the callback path only: update reception → acknowledgement → dispatch → handler work (database, Telegram edits) → render, plus the Stash multi-select tick path |
| Explicitly out of scope | Save pipeline semantics, the AI runtime, Taskloom scheduling, Supabase schema, Render, any live account |
| **Status** | **Root cause CONFIRMED — cost per click, not delivery, ordering or CPU.** Implementation, measurements and tests are in `IMPLEMENTATION_REPORT.md`. Nothing was sent to Telegram, no credential was read, no SQL was executed |

### Method and its limits

* Every repository claim was re-read from the working tree at `5b34a3a` (and, for
  the "before" numbers, from the same tree with the fix stashed).
* Timing evidence comes from `logs/bench_callback_burst.py` (gitignored) and from
  `tests/test_callback_latency_burst.py`. Both install the **real** callback
  router, the **real** retrieve panel/action handlers and the **real** selection
  state, and drive them with fake events whose `answer()`/`edit()` and database
  latencies the harness chooses. They measure the application's structure, never
  a network.
* Consequence of that limit: the numbers prove *what a click waits on* and *how
  many round trips it makes*. They cannot prove what Telegram returns for these
  panels, and they are not a substitute for a live click.

---

## 1. Executive summary

### Verdict: **CONFIRMED — each click paid for network round trips it did not need**

| # | Finding | Status |
|---|---|---|
| 1 | A Stash tick cost **four database round trips**: `load_saved_item` → `query_save`, then `_saved_page` → `list_saves`, which is itself TWO queries (`select("*") … range()` + `count="exact"`). At the ~120 ms/call implied by the production `work_ms` (368–668 ms) that is ~480 ms of avoidable wait per press | **CONFIRMED** — source, and the tick path re-read line by line |
| 2 | **Every** tick performed its own `editInlineBotMessage`, so N presses produced N sequential Telegram edits of ONE message — the visible "burst", and exactly the pattern rapid-edit throttling targets | **CONFIRMED** — source + measurement |
| 3 | Because (1) and (2) sit inside the per-panel FIFO turn, the queue behind a click was ~0.5 s per earlier click: 30 presses ≈ **15 s** of tail (measured 14.96 s pre-fix) | **CONFIRMED** — measurement |
| 4 | A click could be **dropped as `dropped:panel-busy`** for being queued behind clicks that were themselves making progress, because the turn bound was a fixed 20 s while the queue was not bounded by work done | **CONFIRMED** — source (`DEFAULT_TURN_WAIT_S`) |
| 5 | Telethon serialises updates / the event loop is blocked / a global lock serialises clicks | **RULED OUT (re-verified)** — §3 |
| 6 | Telegram's rapid-edit throttling (`FLOOD_WAIT` + Telethon sleeping up to `flood_sleep_threshold=60`) is an amplifier | **UNCONFIRMED HYPOTHESIS** — §4. This phase cuts edits per burst by ~93 %, but no flood answer was observed |

### What the previous phase already fixed (verified then, unchanged now)

Acknowledged-first (`ack` before any session lookup, database read, render or
edit), one FIFO turn per `(chat_id, msg_id)` with independent panels never
waiting, duplicate suppression by Telegram `query_id`, and one timing record per
click. Measured at the time: ack 210.8 ms → 0.0 ms with a 200 ms handler; handler
order `['2','3','1']` → `['1','2','3']`; peak concurrent handlers on one panel
3 → 1.

### What this phase changes about the earlier conclusion

The earlier phase concluded that serialising clicks on one panel was the cost of
correctness (15-click burst: 62.7 ms → 758 ms in that harness). That conclusion
was **incomplete, not wrong**: the serialised work itself was the defect. With
the redundant reads removed and intermediate renders coalesced, a 30-click burst
now costs the same as a 10-click burst (684.7 ms vs 683.6 ms at production-like
latencies, versus 14.96 s vs 5.11 s before). Ordering is still per-panel and still
strict; nothing concurrent edits one message.

---

## 2. The hot path, re-read line by line

`panel:retrieve_saved:select` → `Menu` → Save → Stash → Select Multiple. Each
tick is `action:retrieve_toggle:<CODE>` (an action, never a panel query, so Back
cannot re-run a toggle).

Pre-fix, one tick:

```
_callback_router            segments, session lookup, supersede, acquire turn
  _handle_action            clear_pending
    _retrieve_toggle_action
      _toggle_selected_item
        retrieve_service.load_saved_item     → db query_save          (1 RTT)
        selection_state.toggle               (local)
        _render_selection
          _saved_page → db_client.list_saves → page query + count    (2 RTT)
          build rows (8)
    _render_and_edit → render_edit → _safe_edit → event.edit         (1 RTT)
```

4 network round trips per press, all inside the panel's critical section, for a
state change that is entirely local (a set of save codes).

Two further observations from the same read:

* `_safe_edit` already skips an edit whose `(text, buttons)` is unchanged, but
  consecutive ticks always differ (the marks move), so that guard never applied
  during a burst.
* Nothing in the path needed the database to *identify* the item: the button
  already carried the exact save code, and the page that produced it had been
  read (and owner-filtered) moments earlier.

---

## 3. What was ruled out, and how

**Telethon does not serialise updates.** `telethon/client/updates.py` builds one
task per update when `sequential_updates` is false (the default), at line 289:

```python
task = self.loop.create_task(self._dispatch_update(updates_to_dispatch.popleft()))
```

and `_update_loop` does not await those tasks. Both client factories
(`backend/bot/client.py`, `backend/helper/client.py`) omit `sequential_updates`.

**The event loop is not blocked.** Every Supabase call runs on the bounded
4-worker `run_sync_db` pool with a watchdog; panel settings come from an
in-memory cache warmed at startup (`supervisor.py:178`); `is_owner()` is an
integer comparison. Production CPU was low in the sampled window, which is
consistent with waiting on I/O rather than burning it.

**No global lock on the click path.** `PanelLifecycleManager._lock` is taken only
by create/reuse/close/timer/shutdown. The gate's turn is keyed by
`(chat_id, msg_id)`; two panels never wait for each other (asserted by
`test_independent_panels_progress_while_one_is_blocked`).

**The AI handler is not on this path.** `backend/bot/handlers/ai_unified.py`
registers on the **self** client and fires on outgoing messages; callback queries
arrive on the **helper** client.

**Ack latency is not the problem any more.** It is one answer RPC
(`ack_rpc_ms`), issued before any work. The production `ack_ms` of 160–296 ms is
that round trip plus dispatch, and this phase does not (and cannot honestly)
claim to have reduced it.

---

## 4. The open hypothesis (not a finding)

Telegram publishes no edit-rate quota, but frequent edits of one message are
widely reported to be throttled, and both clients set
`flood_sleep_threshold=60`, so Telethon **sleeps inside the call** on a flood
answer and retries transparently. Thirty edits of one message in ~15 s is
precisely that pattern, and handlers sleeping out a flood window would produce a
freeze followed by a burst.

It stays unconfirmed because it needs production evidence (`FloodWaitError` /
`FLOOD_WAIT_*` on `EditInlineBotMessage` in the runtime logs). The fix reduces
edits per burst by ~93 % and never issues two edits concurrently on one message,
which is the recommended remedy regardless; the report does not claim the
throttle was observed.

---

## 5. Why the fix takes the shape it does

* **Remove the round trips the click did not need.** A tick only needs the rows
  that built the page the owner is looking at, so those rows are kept as a
  bounded, owner-scoped, 60 s snapshot and reused to validate and label the exact
  code the button carried. Ownership and item identity are unchanged: the
  snapshot can only contain rows this owner's own `list_saves` returned, and the
  service layer still re-verifies every code before any bulk side effect.
* **Never let a snapshot outlive the truth.** It expires, and it is discarded
  explicitly on a new selection session, on cancel, after a bulk delete, and
  whenever a lookup contradicts it (a code the page offered that the database no
  longer has). The view is then read fresh so it cannot keep offering a dead row.
* **Coalesce renders, not clicks.** A click whose state change is applied, and
  which observes an accepted click already queued for the same panel, returns
  `SKIP_RENDER`: the queued click paints the newest state. Only handlers whose
  view is a pure projection of state may return it (a confirmation, an error and
  a sub-view always paint). The decision uses `pending_ahead()`, which counts
  only queued clicks that *will* edit the panel — a queued noop cannot supersede
  a view.
* **Keep the order.** The FIFO turn is unchanged. Coalescing happens *inside* a
  click's turn, so ordering and "only the newest click edits" still hold.
* **Make the bound about progress, not about luck.** The turn deadline is now
  the 20 s floor plus 2 s per queued click, capped at Telegram's own 120 s
  callback lifetime, so the tail of a burst is not dropped for being queued
  behind clicks that are progressing. `dropped:panel-busy` remains for a
  genuinely stuck handler and is still logged with its callback data.
* **Measure what a click costs.** `run_sync_db` (the ONE dispatcher) and
  `_safe_edit` (the ONE edit path) report their round trip to the click being
  served; the tick reports its own state-mutation time. That is why the timing
  line can state `db=`, `db_ms=`, `edits=`, `edit_ms=`, `state_ms=`,
  `coalesced=` and `backlog=` instead of inviting a guess.
* **No second dispatch authority.** No new task, queue, executor, scheduler or
  update loop: Telethon still decides what runs and when, and the coalescing
  decision is a synchronous read of a queue that already exists.

---

## 6. Historical investigations (kept, superseded, not relied on)

* **2026-10-23 — callback acknowledgement and ordering (phase 1).** Its three
  findings (ack behind the handler; concurrent unordered handlers on one panel;
  no `query_id` identity) were confirmed and fixed at `5b34a3a`. Its *cost*
  conclusion — that a serialised burst is necessarily slower — was superseded by
  this phase, which showed the serialised work was itself redundant. Its measured
  ordering facts (Telethon task-per-update, `updates.py:289`) still hold.
* **2026-10-22 — "can the Helper Bot edit an existing inline panel
  asynchronously?"** That investigation's verdict was *UNRESOLVED* because the
  repository captured no runtime evidence that Telegram hands the Helper Bot an
  inline-message identifier for these panels. Its verified library facts (the
  pinned client supports `messages.editInlineBotMessage` through
  `edit_message(InputBotInlineMessageID, …)`, and the Helper Bot — as the
  message's `via_bot` — is the principal that may use it) remain true and are
  what `backend/helper/panel_edit.py::ExistingPanelEditor` is built on today.
  This task neither relies on it nor changes it.
* Earlier Save-pipeline reports (view-once media saved as ordinary media,
  SAVE_TRACE instrumentation, deep-save/album contracts, the Stash rename and
  Stash multi-select) describe behaviour this change does not touch. They live in
  git history and their current contracts are summarised in `AGENTS.md`.

---

## 7. Residual risk

* **A skipped paint is only as good as the next click.** If the click queued
  behind a tick fails before rendering, that panel keeps the previous view until
  the next click. State stays authoritative, and the review/bulk panels always
  read state, so no operation is affected — but the visible marks can lag. This
  is bounded and self-healing, not eliminated.
* **The snapshot is a cache with a trust model, not a source of truth.** A row
  deleted elsewhere stays tickable for up to 60 s; the bulk operation re-verifies
  it and reports the failure honestly. Two processes serving one owner (not a
  supported deployment) would each hold their own snapshot.
* **`wait_ms` still grows with the tail of a large burst** (30 clicks ≈ 0.4 s
  behind the first edit). Removing that would require concurrent edits to one
  message, which is the corruption phase 1 removed.
* **Live proof is missing.** That a real Helper Bot answers under a 30-click
  burst, that the panel ends on the last click's view, and what the real
  `ack_ms`/`db_ms` are, must be observed in production logs; they are listed as
  unverified in `IMPLEMENTATION_REPORT.md` §7 and must not be reported as
  verified until observed.
