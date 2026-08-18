# INVESTIGATION — Runtime Stability

> **Rule:** this file is the single canonical investigation report for the
> LifeOS repository. Every new investigation **fully replaces** this file —
> never append. Always base findings on actual repository source; distinguish
> confirmed facts, likely causes, suspected causes, ruled-out causes, and
> unknowns. GitHub is the source of truth for actual code.

---

## PROBLEM

The self-bot intermittently "freezes / sleeps / becomes unresponsive" in
production. The goal of this investigation is to trace the full runtime
lifecycle and determine — from source evidence — what is CONFIRMED, what is
LIKELY, what is SUSPECTED, what is RULED OUT, and what remains UNKNOWN, so a
follow-up execution agent can target the correct fix surface.

This investigation made **no production code changes**. The only repository
change is this report file.

---

## EXECUTIVE SUMMARY

The runtime is built around a single `RuntimeSupervisor`
(`backend/runtime/supervisor.py`) that owns the Telethon self-client, an
optional helper bot, a run loop, a watchdog, and a set of "immortal" background
loops (heartbeat, keepalive, failsafe, diagnostics, memory cleanup, profile
scheduler). There is a **layered** health/recovery architecture:

- `heartbeat` (30s) — snapshot + stall/invariant detection.
- `keepalive` (60s) — bounded `get_me` probe.
- `watchdog` (30s) — RPC heartbeat + loop-stall detection + recovery.
- `failsafe` (15s) — "all four signals frozen for 120s" → hard reset.

The **strongest, source-proven root cause of the intermittent freeze is
self-inflicted reconnection churn**: the heartbeat treats *normal idle* of the
account (and a legitimately disabled helper bot) as "stalled/disconnected" and
repeatedly calls `RuntimeSupervisor._trigger_reconnect()`, which performs a
full `client.disconnect()` → `client.connect()` cycle. This happens with **no
post-success cooldown**, so the bot periodically drops its own connection,
loses updates, and fails commands — the exact "falls asleep, then wakes up"
symptom.

A secondary, source-proven contributing factor is that the self-client
connection has **two owners** (`_run_loop`'s `run_until_disconnected()` and
`_trigger_reconnect`'s manual disconnect/connect), which can race.

The DB layer and the AI HTTP layer are **async-safe and bounded** (DB via
`asyncio.to_thread` + timeout; AI via `httpx.AsyncClient` + `asyncio.wait_for`),
so they are ruled out as event-loop blockers.

---

## CONFIRMED ROOT CAUSES

### RC-1 — Heartbeat fires `_trigger_reconnect()` on normal-idle / helper-disabled conditions (CONFIRMED)

`backend/runtime/heartbeat.py::_heartbeat_loop` spawns
`supervisor._trigger_reconnect()` from four separate checks, several of which
are true in perfectly healthy, quiet states:

1. **READY_BUT_DISCONNECTED invariant requires the helper even when it is disabled.**
   ```python
   if current_state == "READY" and (not self_connected or not helper_connected):
       ...
       guarded_create_task(sup._trigger_reconnect(), name="lifeos-heartbeat-invariant-recovery")
   ```
   `helper_connected` is only ever set `True` in
   `supervisor._start_helper()` (via `set_helper_connected(True)` and
   `update_heartbeat_state(helper_connected=True)`), which is only called when
   `self.helper_enabled` is true (`cfg.get("HELPER_BOT_ENABLED")`, i.e. a
   non-empty `BOT_TOKEN`). `backend/helper/client.py::build_helper` explicitly
   supports the helper being **disabled** (`returns None` when no token).
   When the helper is disabled or failed, `helper_connected` stays `False`
   forever, so this branch fires on **every** heartbeat tick (every 30s).

2. **UPDATE_PIPELINE_STALLED treats "no new Telegram updates" as a stall.**
   ```python
   if last_update > 0 and (now - last_update) > _STALL_THRESHOLD:
       if rpc_healthy:
           ... guarded_create_task(sup._trigger_reconnect(), ...)
   ```
   `last_update` is `health._last_telethon_event`, written only when a
   Telegram event actually arrives (`backend/bot/router.py::register_runtime_hooks`
   and `backend/helper/client.py::register_helper_hooks`). An idle account with
   no incoming messages produces **no** updates for arbitrarily long — that is
   normal, not a stall. `rpc_healthy` is `last_rpc` younger than 60s, and
   keepalive keeps `last_rpc` fresh, so this condition is continuously met on
   an idle account.

3. **CALLBACK_DISPATCH_STALLED treats "owner did not press a button" as a stall.**
   ```python
   if last_callback > 0 and (now - last_callback) > _STALL_THRESHOLD:
       if rpc_healthy:
           ... guarded_create_task(sup._trigger_reconnect(), ...)
   ```
   `last_callback` is written only when an inline callback is processed
   (`backend/helper/panels.py`, `backend/helper/client.py`). Not pressing an
   inline button for >90s is normal, yet this triggers a reconnect.

4. **EVENT_DISPATCH_STALLED** (updates arriving but no dispatch) is the one
   condition that is a *real* stall signal, but it is mixed in with the three
   false-positive signals above.

**Impact:** the reconnect loop runs even when nothing is wrong. This is the
deterministic mechanism behind "the bot falls asleep and then comes back".

### RC-2 — `_trigger_reconnect()` is a full disconnect+connect with no post-success cooldown (CONFIRMED)

`backend/runtime/supervisor.py::_trigger_reconnect`:

```python
await asyncio.wait_for(self.client.disconnect(), timeout=10.0)
await asyncio.sleep(1)
await asyncio.wait_for(self.client.connect(), timeout=30.0)
...
self._client_alive = True
self._consecutive_failures = 0
self._reconnect_failures = 0
set_telethon_connected(True)
update_heartbeat_state(self_connected=True)
...
return
```

- On success it resets failure counters but does **not** set
  `_recovery_cooldown_until` (only `_do_recovery`'s full-rebuild path sets the
  180s cooldown). Therefore the 30s heartbeat can call it again immediately.
- Every call drops the self-client connection for the duration of
  `disconnect()` + 1s + `connect()`. Incoming updates are lost and outgoing
  commands fail during that window.

### RC-3 — Two owners of the self-client connection can race (CONFIRMED mechanism)

- `_run_loop` owns `await self.client.run_until_disconnected()` in a loop.
- `_trigger_reconnect` owns a manual `disconnect()`/`connect()`.
- When `_trigger_reconnect` disconnects, `run_until_disconnected()` returns;
  `_run_loop` then sets `_client_alive=False`, `set_telethon_connected(False)`,
  `update_heartbeat_state(self_connected=False)`, sleeps 2s, and re-enters
  `run_until_disconnected()` (which reconnects). Meanwhile `_trigger_reconnect`
  is also reconnecting. The two reconnects can interleave, prolonging the
  offline window and re-arming the heartbeat's `not self_connected` branch.

---

## CONFIRMED FACTS

1. **CONFIRMED** — `heartbeat.py` is the only caller that spawns
   `_trigger_reconnect` on a timer from idle/stall heuristics (plus `keepalive`
   on an actual RPC timeout/failure and the `watchdog` loop on genuine RPC
   failure). The heartbeat's four triggers are described in RC-1.
2. **CONFIRMED** — `_trigger_reconnect` does a `disconnect()` + 1s sleep +
   `connect()` and no cooldown on success (RC-2).
3. **CONFIRMED** — `_run_loop` and `_trigger_reconnect` both manipulate the
   same client connection (RC-3).
4. **CONFIRMED** — DB access does not block the event loop.
   `backend/db/client.py::_run_sync` runs each Supabase call via
   `asyncio.to_thread(...)` wrapped in `guarded_await(..., timeout=10)`.
5. **CONFIRMED** — AI provider HTTP is async and bounded.
   `backend/ai/providers/gemini.py` (and the other providers) use
   `httpx.AsyncClient(timeout=...)`; the chat handlers wrap
   `engine.execute(...)` in `asyncio.wait_for(..., timeout=_AI_TIMEOUT)`
   (`backend/bot/handlers/ai_unified.py`, `ai_cmd.py`).
6. **CONFIRMED** — Save media transfer (`download_media` / `send_file`) is
   intentionally **not** wrapped in a timeout
   (`backend/services/save_service.py::execute_save`). This matches the
   requirement to never blanket-timeout large transfers; it means a stalled
   MTProto media transfer can hold that one handler task for a long time.
7. **CONFIRMED** — `operation_watchdog.attach_task` is defined but has **no
   callers** anywhere in `backend/`. The `bounded_operation.__aexit__` branch
   that converts an external `CancelledError` into `OperationTimeoutError`
   (`if exc_type is asyncio.CancelledError and self._task is not None`) is
   therefore dead — `_task` is always `None`.
8. **CONFIRMED** — `backend/diagnostics.py::recover_stalled` has **no
   callers** (repo grep finds only its definition). It is not currently a live
   second recovery authority.
9. **CONFIRMED** — `crash_diagnostics` installs signal handlers, exit-reason
   tracking, exception ring buffers, and a pre-exit `dump_crash_snapshot()` on
   every termination path (`backend/runtime/crash_diagnostics.py`,
   `backend/main.py`). A silent process death would still emit
   `PROCESS_EXIT_REASON`.
10. **CONFIRMED** — SIGTERM/SIGINT are handled via
    `loop.add_signal_handler` in `main()`, which overrides the
    `crash_diagnostics.capture_signal` handler for those two signals; SIGABRT
    and SIGQUIT still hard-exit through `capture_signal`.

---

## LIKELY CAUSES / RISKS

1. **LIKELY — the freeze is reconnect churn, not a true dead process.** RC-1 +
   RC-2 + RC-3 produce periodic disconnects on idle/helper-disabled runs. The
   symptom is "works, then unresponsive, then works" with no crash snapshot,
   which matches the reported behavior. (Production log correlation is missing,
   so this is LIKELY, not observed-and-proven end-to-end.)
2. **LIKELY — helper-disabled deployments are the worst case.** If `BOT_TOKEN`
   is unset (helper disabled), READY_BUT_DISCONNECTED fires every 30s
   unconditionally. The helper is explicitly optional in code.
3. **LIKELY — `asyncio.to_thread` threads cannot be force-cancelled.** If
   Supabase hangs past the 10s `guarded_await` timeout, `wait_for` cancels the
   `to_thread` future but the OS thread keeps running and can accumulate under
   repeated slow-DB events. Not a loop blocker, but a resource leak risk.
4. **LIKELY — a stalled media download/upload appears as "one operation
   stuck".** `execute_save` has no transfer timeout, so a stalled MTProto
   media transfer holds the handler task (not the loop) indefinitely.
5. **LIKELY — the in-process failsafe cannot detect a hard event-loop block.**
   `failsafe` runs on the same loop it is monitoring; if the loop itself is
   blocked by a synchronous call, the failsafe (and heartbeat/keepalive) also
   cannot run, so "no logs" is consistent with a blocked loop as well as with
   an idle-but-healthy process. There is no out-of-process watchdog.
6. **LIKELY — diagnostic "no more logs" is ambiguous.** Trace/log output is
   emitted synchronously through `logging` on the event loop; if the loop is
   blocked, logging stalls too. Absence of logs therefore cannot by itself
   prove process death.

---

## RULED OUT

1. **RULED OUT** — Supabase HTTP blocking the event loop (it runs in a thread
   with a 10s bound).
2. **RULED OUT** — synchronous AI provider HTTP blocking the loop (async
   `httpx.AsyncClient` + handler `wait_for`).
3. **RULED OUT** — silent process exit with no diagnostics (crash diagnostics
   emit an exit reason and snapshot on all termination paths).
4. **RULED OUT** — `operation_watchdog` CancelledError→OperationTimeoutError
   conversion corrupting shutdown (the `attach_task` prerequisite is never
   called).
5. **RULED OUT** — `diagnostics.recover_stalled` being an active second
   recovery authority (no callers).
6. **RULED OUT** — a blanket Save timeout is the fix (large transfers must be
   allowed to run long).

---

## UNKNOWN / MISSING EVIDENCE

- **Production environment values** — whether `BOT_TOKEN` (helper),
  `SUPABASE_*`, and `AI_*` are set in Render. `render.yaml` lists all of them
  as `sync: false` (set manually), so helper-enabled vs helper-disabled cannot
  be determined from the repository.
- **Actual production logs around a freeze** — no log sample was available to
  confirm the RC-1 reconnect cadence or a crash snapshot.
- **Live Telegram behavior** — no credentials/session in this environment, so
  `UpdateProfileRequest`, media transfers, and reconnect timing were not
  exercised live.
- **A hard event-loop block from a synchronous call** — the source does not
  reveal one (all known I/O is async/bounded), but without a thread/frame dump
  at the moment of a freeze it cannot be fully excluded.
- **Whether `run_until_disconnected()` + `_trigger_reconnect` actually
  interleave** in production — the race is provable from the code, but its
  live occurrence is not logged.

---

## EXACT FILES

| File | Role in this investigation |
|---|---|
| `backend/main.py` | Entry point, startup retry, signal handling, shutdown |
| `backend/runtime/supervisor.py` | `RuntimeSupervisor` — run loop, watchdog, reconnect, full recovery, hard reset, shutdown |
| `backend/runtime/heartbeat.py` | 30s snapshot + **stall/invariant detection that triggers reconnect** |
| `backend/runtime/keepalive.py` | 60s bounded `get_me` probe + reconnect trigger |
| `backend/runtime/failsafe.py` | 15s all-frozen monitor → `_hard_reset_runtime` |
| `backend/runtime/task_guard.py` | `guarded_create_task` / `immortal_create_task` |
| `backend/runtime/managed_task.py` | `ManagedTask` (self-restarting tasks) |
| `backend/runtime/operation_watchdog.py` | `bounded_operation`, `guarded_await`, `OperationTimeoutError` |
| `backend/runtime/tg_retry.py` | `tg_rpc` retry/backoff/timeout for RPCs |
| `backend/runtime/crash_diagnostics.py` | Exit reasons, exception rings, crash snapshots |
| `backend/runtime/health_check.py` | Unified health checks (read-only) |
| `backend/health.py` | Timestamps (`_last_update`, `_last_rpc`, `_last_callback`, …) |
| `backend/diagnostics.py` | Event ring, diagnostic report, dormant `recover_stalled` |
| `backend/bot/client.py` | Self-client factory (`auto_reconnect=True`) |
| `backend/bot/router.py` | `register_runtime_hooks` — writes update timestamps |
| `backend/helper/client.py` | Helper factory + helper event hooks (writes timestamps) |
| `backend/helper/watchdog.py` | Helper watchdog (marks permanent failure) |
| `backend/helper/lifecycle.py` | Panel lifecycle (single `asyncio.Lock`) |
| `backend/services/save_service.py` | `execute_save` (unbounded media transfer by design) |
| `backend/ai/engine/engine.py` | AI engine (`execute` → dispatcher) |
| `backend/ai/engine/dispatcher.py` | AI dispatcher (never raises) |
| `backend/ai/providers/*` | Async HTTP providers |
| `backend/db/client.py` | `_run_sync` (to_thread + timeout) |
| `render.yaml` | Deployment env (helper/AI keys all `sync: false`) |

---

## EXACT FUNCTIONS / CLASSES

**Supervisor** (`backend/runtime/supervisor.py`):
`start`, `_build_and_register`, `_start_helper`, `_run_loop`,
`_trigger_reconnect`, `_trigger_full_recovery`, `_do_recovery`,
`_retry_full_recovery`, `_hard_reset_runtime`, `_watchdog_loop`,
`_cancel_orphan_tasks`, `_resume_bio_cron`, `_resume_username_cron`, `stop`.

**Heartbeat** (`backend/runtime/heartbeat.py`):
`_heartbeat_loop`, `configure`, `update_state`, `start_heartbeat`,
`stop_heartbeat`.

**Keepalive** (`backend/runtime/keepalive.py`):
`_keepalive_loop`, `start_keepalive`, `stop_keepalive`.

**Failsafe** (`backend/runtime/failsafe.py`):
`_failsafe_loop`, `_all_frozen`, `start_failsafe`, `stop_failsafe`.

**Task guard** (`backend/runtime/task_guard.py`):
`guarded_create_task`, `immortal_create_task`.

**Operation watchdog** (`backend/runtime/operation_watchdog.py`):
`bounded_operation`, `guarded_await`, `OperationTimeoutError`,
`attach_task` (unused).

**DB** (`backend/db/client.py`):
`_run_sync`, `get_db`, `is_available`, and the `_sync`/`async` wrappers.

**Save** (`backend/services/save_service.py`):
`execute_save`, `execute_link_save`, `_extract_source_media`,
`_upload_kwargs_for_media`.

---

## EXECUTION PATHS

### Normal run (healthy, idle account)
```
main → supervisor.start → _build_and_register (self client)
     → _start_helper (optional) → _resume_bio/username_cron
     → start_heartbeat + start_keepalive + start_failsafe + start_diagnostics
     → _run_loop: run_until_disconnected()
heartbeat (30s): _heartbeat_loop → tick + stall checks
keepalive (60s): get_me (15s timeout)
watchdog (30s): get_me RPC check
```

### The reconnect-churn path (the freeze mechanism)
```
idle/helper-disabled
  → heartbeat READY_BUT_DISCONNECTED (helper_connected=False)
  → heartbeat UPDATE_PIPELINE_STALLED (no updates >90s, rpc_healthy=True)
  → heartbeat CALLBACK_DISPATCH_STALLED (no callback >90s, rpc_healthy=True)
  → sup._trigger_reconnect()
      → client.disconnect() → sleep(1) → client.connect()
      → (no cooldown set)
  → _run_loop.run_until_disconnected() returns on the disconnect
      → _client_alive=False, telethon_connected=False, sleep(2), re-enter
  → next heartbeat tick repeats
```

### Recovery escalation
```
trigger_reconnect fail → _trigger_full_recovery → _do_recovery
  → stop helper, cancel run task, dispose client, backoff, rebuild, resume cron
  → success sets _recovery_cooldown_until = now + 180s
recovery exhausted (5) → RuntimeState.FAILED → sys.exit(1) (Render restarts)
failsafe: all 4 signals frozen 120s → _hard_reset_runtime
```

---

## CURRENT BEHAVIOR

- A quiet account (or a deployment without a working helper bot) is
  periodically disconnected and reconnected by its own heartbeat, because
  "no activity" and "no helper" are misclassified as failures.
- During each disconnect/reconnect window the bot cannot receive updates or
  complete commands, producing the observed intermittent freeze/sleep.
- There is no post-success reconnect cooldown, so the cycle repeats on the
  next heartbeat tick.
- DB and AI I/O are bounded/async and do not block the loop.

## DESIRED BEHAVIOR

- Reconnection must be triggered only on **genuine** failure signals
  (actual RPC timeout, actual disconnect detected by the run loop, or
  state-invariant mismatch that is actually possible), never on normal
  account idle or a legitimately disabled helper.
- A single owner of the client connection must coordinate disconnect/reconnect
  so `_run_loop` and `_trigger_reconnect` cannot race.
- Reconnects must be throttled (cooldown/backoff) so a repeated false signal
  cannot churn the connection every 30s.
- Long legitimate work (large media transfers) must remain unbounded, while
  genuinely bounded operations keep their current timeouts.

---

## RECOMMENDED FIX SURFACE

*(Do NOT implement here — this is for the execution agent.)*

1. **Fix the heartbeat invariant.** `READY_BUT_DISCONNECTED` must only treat a
   missing helper as a failure when the helper is actually **enabled**
   (`supervisor.helper_enabled`). When the helper is disabled, only
   `self_connected` matters.
2. **Stop treating idle as stalled.** `UPDATE_PIPELINE_STALLED` and
   `CALLBACK_DISPATCH_STALLED` must not fire simply because an idle account
   produced no updates/callbacks for 90s. Either remove them, gate them on an
   explicit "expected traffic" signal, or require additional evidence (e.g.
   RPC failing, not RPC healthy).
3. **Add a reconnect cooldown on the lightweight path.** `_trigger_reconnect`
   should set `_recovery_cooldown_until` (or an equivalent per-reconnect
   throttle) on success so a false signal cannot cause a 30s reconnect loop.
4. **Unify connection ownership.** Have `_run_loop` be the sole driver of
   reconnect (or have `_trigger_reconnect` fully coordinate with the run loop
   so only one reconnect runs at a time and the loop does not immediately
   re-enter on the self-inflicted disconnect).
5. **Consider a bounded-but-generous media-transfer budget** only if a
   production log proves a stuck download/upload is the observed failure; do
   **not** introduce a blanket Save timeout.
6. **Remove or wire** the dormant `operation_watchdog.attach_task` path and
   `diagnostics.recover_stalled` so dead recovery code cannot later become a
   second recovery authority by accident (delete or explicitly document).

---

## REMAINING WORK

- Implement fix surface items 1–4 (highest priority) and add regression tests.
- Correlate with a real production freeze log to confirm the reconnect cadence.
- Verify whether `BOT_TOKEN` is set in production (determines how often
  READY_BUT_DISCONNECTED fires).
- Decide the fate of dormant `recover_stalled` and unused `attach_task`.
- Confirm live Telegram reconnect behavior after the fix.

---

## VALIDATION PLAN

Checks the execution agent should perform after any change:

- **Unit tests** for the heartbeat decision function: helper-disabled + READY
  must NOT trigger; idle (no updates/callbacks) must NOT trigger; genuine RPC
  failure still triggers.
- **Unit tests** that `_trigger_reconnect` success sets a cooldown/throttle.
- **Unit test** that a self-inflicted `disconnect()` does not make `_run_loop`
  and `_trigger_reconnect` both reconnect concurrently (single-owner invariant).
- **Verify** all four heartbeat triggers and their new guards by inspection.
- **Run** `py_compile` on modified files and the full `pytest` suite.
- **Live verification** of reconnect behavior only if a real session is
  available (do not claim otherwise).

---

### Verification levels for this investigation

- `SOURCE VERIFIED` ✅ (every claim above read directly from the repository files listed)
- `TEST VERIFIED` ❌ (no runtime-stability tests were run; investigation only)
- `LIVE TELEGRAM VERIFIED` ❌ (no credentials/session in this environment)
