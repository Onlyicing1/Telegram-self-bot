# INVESTIGATION.md — Runtime DB `Errno 11` degradation and lifecycle tasks reported as stalls

This file records **only** the latest completed investigation and **replaces
every previous investigation in full**. Earlier handoffs (colored-button
semantics, the symbol registry, premium-emoji rounds, AED archive rounds,
private-message rounds) remain readable in git history and are obsolete here.

| Item | Value |
|---|---|
| Investigation | Post-deployment runtime diagnosis, two independent workstreams |
| Date | 2026-10-19 |
| Project / remote | LifeOS Telegram self-bot, `Onlyicing1/Telegram-self-bot` (`origin`) |
| Delivery branch | `main` (the investigation commit `17942f1` is on `origin/main`) |
| Base revision at investigation start | `3f0ef80b60d65c3a10582b6e86d6be4a06190c17` (`= origin/main`, verified with `git fetch` + `git rev-parse`) |
| Resulting fix commit | `17942f167591618c4ba9111d15f2f7cffc7d789d` — `fix(runtime): stop reporting lifecycle tasks as stalls and record the DB failure chain` |
| Evidence basis | Full source reads, the installed dependency sources, local reproductions, and the repository test suite. **No live environment was touched** |
| Live Telegram / Render / Supabase | **NOT accessed.** No deployment, Render shell, log stream, `.env`, credential, SQL, migration, restart or environment change. No claim of runtime recovery is made |

Detailed implementation notes are in `IMPLEMENTATION_REPORT.md` at commit
`17942f1`; this document is the self-contained handoff.

---

## 1. Questions investigated

Two symptoms appeared after deployment. They were investigated **separately**;
no shared cause was assumed, and none was found.

* **A — DB degradation.** Repeated `httpx`/`httpcore`
  `ReadError: [Errno 11] Resource temporarily unavailable` raised inside
  `backend.ai.database.task_repository`, each followed by
  `TASK_FALLBACK_CLASSIFIED reason=local_resource` and a switch to the
  in-memory task repository, while the process stayed `READY` with both
  Telegram clients connected. A separate `ConnectionTerminated` HTTP/2 error
  was also observed.
* **B — false stall reports.** After the earlier identity fix `58fa664`,
  `TASK_NO_PROGRESS` / `TASK_STARVATION` still named two long-lived lifecycle
  tasks: `Task-1` (coroutine `main`, awaiting the process shutdown event) and
  `Task-27` (coroutine `main` inside uvicorn's lifespan machinery).

---

## 2. Workstream A — confirmed facts about the DB path

All rows below were verified in source or in the installed packages, not
inferred from the log text.

| Claim | Status | Evidence |
|---|---|---|
| One reusable `ThreadPoolExecutor` with 4 workers | **Confirmed** | `backend/db/client.py::_get_db_executor` — `ThreadPoolExecutor(max_workers=4, thread_name_prefix="lifeos-supabase")`, created once under `_db_executor_lock`; every call goes through `run_sync_db` |
| 8 s transport deadline below a 10 s dispatch deadline | **Confirmed** | `_DB_HTTP_TIMEOUT = 8.0` → `ClientOptions(postgrest_client_timeout=…)`; `_DB_TIMEOUT = 10.0` → `guarded_await(..., timeout=10.0)` in `run_sync_db`; the `0 < 8 < 10` ordering is test-pinned |
| `ClientOptions(postgrest_client_timeout=…)` exists in the pinned library | **Confirmed** | `supabase 2.4.2` exports `ClientOptions` with `postgrest_client_timeout` (default 120); `get_db()` builds the single shared client with `8.0` |
| 5 s local-resource cooldown | **Confirmed** | `_LOCAL_RESOURCE_COOLDOWN_SECONDS = 5.0` in `backend/ai/database/task_repository.py`; the gate is in `_run`/`_run_checked`; a skipped call raises `LocalResourceCooldown` with `reason=local_resource`; a skipped call never extends the window; a successful durable operation clears it |
| Failure degrades to the in-memory repository | **Confirmed** | Each repository method catches, calls `_degrade`/`_mark_fallback`, and returns data annotated with `fallback_backend` / `fallback_reason` — non-durable and truthfully labelled |
| Supabase REST traffic uses HTTP/2 | **Confirmed** | `postgrest 0.16.11::SyncPostgrestClient.create_session` builds `SyncClient(..., http2=True)`; `h2 4.4.1` installed. `ConnectionTerminated` is an h2 class, i.e. the same transport |
| A socket read timeout would surface as `ReadError [Errno 11]` | **Disproved** | `httpcore._backends.sync.SyncStream.read` maps `socket.timeout → ReadTimeout` and other `OSError → ReadError`. A local TLS reproduction (§4) produced `TimeoutError` with `errno=None` |

### What the errno means in this exact chain

`httpx.ReadError` wraps `httpcore.ReadError`, which wraps the raw `OSError`
raised by `SyncStream.read` → `SSLSocket.recv`. `OSError(errno.EAGAIN, …)`
constructs the `BlockingIOError` subclass (verified locally), so
`[Errno 11]` is a socket-layer "would block / no data currently available" —
not an HTTP status, not a PostgREST error, and not a response from the store.

### Actual execution/data flow for a task write

```
AI call site / scheduler / execution coordinator
  → repository method (SupabaseTaskRepository._run / _run_checked)
  → cooldown gate?  (inside 5 s window → LocalResourceCooldown(reason=local_resource))
  → run_sync_db(...) in backend/db/client.py
  → loop.run_in_executor(one 4-worker pool, lifeos-supabase-*)
  → guarded_await(..., timeout=10 s)   [backend/runtime/operation_watchdog.py]
  → synchronous supabase-py → postgrest 0.16.11 → httpx over HTTP/2
      transport deadline pinned at 8 s
  → on failure: _classify_degradation walks __cause__/__context__
      errno EAGAIN/EMFILE/ENFILE, thread-start RuntimeError → local_resource
      ReadTimeout / ConnectTimeout / ECONNRESET / RemoteProtocolError → unavailable
  → _degrade / _mark_fallback → InMemoryTaskRepository + fallback annotations
  → TASK_FALLBACK_CLASSIFIED (once per episode) then per-operation warning
```

`guarded_await` bounds the *awaiting coroutine*; a thread blocked inside the
synchronous HTTP call cannot be cancelled, which is why the transport deadline
must be strictly shorter than the dispatch deadline.

### Hypothesis discrimination

| Hypothesis | Failure that would surface | Observed |
|---|---|---|
| Local socket read returned `EAGAIN` | `httpx.ReadError` ← `BlockingIOError(errno=11)` | **Yes — the production text** |
| Executor saturation / dispatch budget exceeded | `OperationTimeoutError`, no errno → `unavailable` | No |
| Transport read deadline expired | `httpx.ReadTimeout`, no errno → `unavailable` | No — disproved by the reproduction |
| File-descriptor exhaustion | `EMFILE`/`ENFILE` (24/23) → also `local_resource`, different errno | No |
| Remote HTTP/2 termination | `RemoteProtocolError` ← `ConnectionTerminated`, no errno → `unavailable` | Seen as a **separate** line, not as the Errno-11 chain |
| Peer reset / closed socket | `ECONNRESET` (104) / `EBADF` (9) → `unavailable` | No |

### Cooldown: bounded, and now measured

The cooldown limits durable re-attempts to at most one per 5 s window while an
episode lasts. A skipped call is served from the in-memory fallback with
`reason=local_resource`, is never re-labelled an outage, and does not extend the
window. A test drives **180 calls across 90 simulated seconds** with a
monotonic-clock shim (no sleeping) and asserts **≤ 19** durable attempts instead
of 180.

### Conclusion for A

* **Confirmed:** the failure is a *local socket-layer would-block*, not a
  Supabase outage, not an expired read deadline, and not executor saturation.
  Classification and cooldown already behave as designed.
* **Not established (unproven):** *why* the local socket layer returns `EAGAIN`
  on Render. Settling that needs host-level data this workspace cannot collect
  (fd/socket/thread counters, egress/NAT behaviour, gVisor syscall behaviour,
  h2 reuse pattern). It is recorded as unproven rather than guessed.
* **Diagnostic gap that was real:** `TASK_FALLBACK_CLASSIFIED` recorded only the
  outer exception type and its message, which cannot distinguish a read
  would-block from a connect-phase failure, an fd shortage, a timeout, or an
  h2 termination. This ambiguity had survived two earlier phases.

---

## 3. Workstream A — the one narrow change made

In `backend/ai/database/task_repository.py`, the once-per-episode
`TASK_FALLBACK_CLASSIFIED` warning now includes
`chain=<names+errno>`. `_describe_exception_chain` walks
`__cause__`/`__context__`, emits **class names and integer errno values only**,
is capped at `_MAX_CHAIN_LINKS = 6`, and guards against cycles. No message, URL,
header, token or user data is logged. Nothing is retried, suppressed, or
re-classified; fallback semantics are unchanged.

---

## 4. Local reproduction used to separate timeout from would-block

Performed outside the repository (nothing in the source or tests depends on it):

```
self-signed TLS server accepts, then never writes
client: wrap_socket → settimeout(0.2) → recv(1024)
→ TimeoutError  errno=None   (isinstance(socket.timeout) is True)
```

`httpcore` maps that to `httpx.ReadTimeout`, so a `ReadError` carrying
`errno=11` cannot be an expired read deadline. The matrix is test-pinned in
`tests/test_task_fallback_classification.py`: `ReadError`/`WriteError` +
`EAGAIN` → `local_resource`; `ReadTimeout` / `ConnectTimeout` → `unavailable`;
`ECONNRESET` → `unavailable`; `EMFILE` → `local_resource`.

---

## 5. Workstream B — confirmed root cause

### Actual classification flow

```
_dump_tasks()
  → for each pending asyncio.Task: t.get_coro()
  → coroutine's OWN outermost frame (cr_frame)
  → coro_name (__name__) + await_loc (co_filename:f_lineno)
  → _is_permanent_task(name, coro_name, await_loc)
      name in _PERMANENT_TASK_NAMES
      or coro_name in _PERMANENT_CORO_EXACT / matches _PERMANENT_CORO_PATTERNS
      or await_loc's source file matches _PERMANENT_SOURCE_MARKERS
      or (coro_name == "main" and source == backend/main.py)
  → permanent tasks are excluded from stalled / starvation / deadlock /
    slow-handler reports
  → per-task identity state (_prev_stacks, _stack_unchanged_count,
    _task_first_seen) keyed by the task object, pruned each dump
```

### Why the two tasks were classified BOUNDED

`_is_permanent_task` originally had three signals: runtime-assigned task names,
coroutine-name patterns, and a **`telethon/`-only** source marker.

* **`Task-1`** — runtime name `Task-1` (not assigned by LifeOS), coroutine
  `main`, await location `backend/main.py:…`. `"main"` matched no coroutine
  pattern and the source was not under `telethon/` → BOUNDED.
* **`Task-27`** — coroutine `main`, await location
  `…/uvicorn/lifespan/on.py:…`; uvicorn creates it with
  `loop.create_task(self.main())`, so it gets an auto-generated `Task-N` name.
  `uvicorn`/`serve` existed only as *coroutine-name* patterns, not source
  markers → BOUNDED.

Reproduced against the pre-fix `backend/runtime/diagnostics.py` restored from
`HEAD`: **4 failed / 17 passed**, with the production output:

```
TASK_NO_PROGRESS — … Task: Task-1 / Task: Task-27
TASK_STARVATION — 2 tasks unchanged across 3+ dumps:
  STARVATION: Task-1 unchanged for 3 dumps (0s)
  STARVATION: Task-27 unchanged for 3 dumps (0s)
  Stack trace: File ".../uvicorn/lifespan/on.py", line 3, in main
```

### Why both waits are expected

* `backend/main.py::main` awaits `supervisor.start()`; `start_diagnostics()` is
  called **inside** that method (`backend/runtime/supervisor.py:201`). No dump
  can therefore observe `main` during startup — whenever diagnostics runs,
  `main` can only be parked on `await supervisor.shutdown_event.wait()` until
  shutdown. `asyncio.run()` names the entry task `Task-N`, so its own source
  file is the only reliable identifier.
* `uvicorn/lifespan/on.py::main` is library machinery that parks on its receive
  queue until server shutdown. The app's uvicorn task is separately named
  `lifeos-web-server`, already permanent by name.

### Fix (classification only, in `backend/runtime/diagnostics.py`)

* `_PERMANENT_SOURCE_MARKERS` gains `"uvicorn/lifespan/"`, matched against the
  coroutine's **own** frame.
* A narrow rule treats a task as permanent only when its coroutine is exactly
  `main` **and** that coroutine's own source file is the process entrypoint
  `backend/main.py` (`_source_file` strips the `:line` suffix first).
* `serve` moved from the substring tuple into the **exact** set
  `_PERMANENT_CORO_EXACT`, because as a substring it also matched application
  coroutines such as `_observe_durable_ceiling` and any `_preserve_*`, silently
  hiding a genuine stall there.
* **Server-side control:** an application coroutine named `main` in a different
  file remains BOUNDED; an application coroutine awaiting a lifecycle-style
  shutdown wait is still reported as `TASK_NO_PROGRESS`/`TASK_STARVATION`.

**Why the process task was not simply renamed:** it would also have to be added
to `RuntimeSupervisor._cancel_orphan_tasks`' protected name set, because that
method cancels every pending task whose name is not protected. Renaming without
that change would let a hard reset cancel the process lifecycle task itself. The
source-based classification rule is smaller and precise.

Preserved unchanged: the task-object identity registry and pruning from
`58fa664`, counter/elapsed accounting, the three-dump starvation threshold,
deadlock and slow-handler detection, and detection of real bounded stalls.
This was **classification only** — no runtime recovery, scheduling, timeout or
retry behaviour was altered.

**Conclusion for B — established:** colour of the defect is in the classifier's
source-marker whitelist, not in the tasks. Both reported tasks are deliberate
process/library waits; the classifier could not identify them by the signals it
had. `Task-1` and `Task-27` are now `[PERMANENT]` and produce no warning.

---

## 6. Evidence: tests and exact results

| Command | Result |
|---|---|
| `pytest tests/test_runtime_diagnostics_classification.py tests/test_task_fallback_classification.py tests/test_task_fallback_cooldown.py tests/test_local_resource_bounds.py -q` | **57 passed** |
| The diagnostics test file against the **pre-fix** `diagnostics.py` | **4 failed / 17 passed** — reproduces `STARVATION: Task-1` / `Task-27` verbatim; the app-side control passed before and after |
| Above plus `tests/test_07_diagnostics.py tests/test_11_runtime_wiring.py tests/test_task_repository.py tests/test_task_scheduler.py tests/test_task_execution.py` | **119 passed** |
| `pytest tests -q -p no:cacheprovider` (full suite) | **6382 passed, 27 skipped, 40 failed** in 124.39 s |
| `pytest tests/test_colored_buttons.py -q` | **40 failed / 12 passed** — the entire failure set: `Button.inline() got an unexpected keyword argument 'style'`, i.e. workspace Telethon **1.34.0** vs pinned **1.43.0**. Pre-existing, unrelated, not fixed |
| `python3 -m py_compile` on both changed modules and the three test files | exit 0 |
| `git diff --check` | clean |

Baseline accounting: the previous baseline was `6370 passed`; this phase adds
**12 tests** (6 diagnostics, 5 classification/chain, 1 cooldown) → `6382`.
No new failures were introduced. The 27 skips are unchanged opt-in/live tests.

**Post-fix reruns on the final committed tree:** focused **57 passed**,
broader relevant set **119 passed**, full suite with the same 40 pre-existing
failures, `py_compile` exit 0, `git diff --check` clean, both modules import
cleanly.

---

## 7. Files changed by the investigation (commit `17942f1`)

| File | Why |
|---|---|
| `backend/runtime/diagnostics.py` | Fix the proven permanent/bounded misclassification; remove the `serve` substring over-match |
| `backend/ai/database/task_repository.py` | Add the bounded, secret-free exception chain to the once-per-episode fallback diagnostic |
| `tests/test_runtime_diagnostics_classification.py` | Regression coverage for both lifecycle tasks, the entrypoint rule, app-side controls, exact `serve` |
| `tests/test_task_fallback_classification.py` | Read/Write error shape, timeout/ECONNRESET/EMFILE discrimination, chain bound and logged field |
| `tests/test_task_fallback_cooldown.py` | Deterministic 90-second episode proving the cooldown bounds durable attempts |
| `IMPLEMENTATION_REPORT.md` | Full-replacement implementation report |

Deliberately **not** changed: no schema, migration or SQL, no
`DATABASE_ARCHITECTURE.md`, no second client/executor/scheduler/retry loop, no
worker-count or timeout change, no Render or environment change, no
user-facing behaviour.

---

## 8. Constraints and architectural decisions that shaped this work

* **One shared Supabase client and one bounded 4-worker pool.** Every DB surface
  (DB layer, task repository, AI persistence) dispatches through `run_sync_db`;
  concurrency and socket count stay bounded regardless of callers.
* **Transport deadline strictly below the dispatch deadline.** The watchdog can
  abandon the awaiting coroutine, but only the transport deadline releases the
  blocked thread.
* **No automatic retry for the Errno-11 path.** Writes are non-idempotent and
  rely on CAS/unique keys; a retry needs a separate, carefully designed change.
  The 5 s cooldown is the only rate control, and it is scoped to the durable
  task path rather than process-wide.
* **No new task name for the process entry task.** Renaming it would require
  widening `_cancel_orphan_tasks`' protected set or a hard reset would cancel
  the process lifecycle task.
* **Classification remains source/frame based.** An application coroutine always
  keeps its own frame file, so the entrypoint and `uvicorn/lifespan/` rules
  cannot capture real application work.

---

## 9. Unresolved / unverified

* **The OS-level cause of `EAGAIN` on Render is unproven** (see §2).
* **`ConnectionTerminated` correlation with the Errno-11 bursts is unproven.**
  It is known to be a separate line from the same HTTP/2 transport, but whether
  it coincides with the bursts in time has not been measured.
* **No retry for a terminated pooled HTTP/2 connection** exists, deliberately.
* **`RuntimeSupervisor._cancel_orphan_tasks` still cancels uvicorn's lifespan
  task** on a hard reset (auto-named and unprotected). That is pre-existing
  behaviour, outside this phase; the new classification only stops reporting it
  as a stalled bounded task.
* **`backend/runtime/heartbeat.py`'s own permanent-name counter** still counts
  only assigned names, so its `bounded_tasks` telemetry can be two higher than
  the diagnostics' count. It produces no warning; left unchanged to avoid
  duplicating the classifier in a second runtime-critical loop.
* **No production verification.** No deployment, Render shell, log stream, live
  Telegram session or Supabase connection was used. The commit is not proof of
  deployment or recovery.

---

## 10. Precise next step

When the next episode occurs, read the `TASK_FALLBACK_CLASSIFIED` line's
`chain=` field. From it, decide between:

1. `ReadError(errno=11)` ← `BlockingIOError` — confirm the read-phase
   would-block and pursue host-level socket/fd metrics.
2. `RemoteProtocolError` ← `ConnectionTerminated` — connection reuse/expiry, a
   separate problem from the local errno.
3. `ReadTimeout` / `OperationTimeoutError` — a latency/budget problem, not
   `EAGAIN`.
4. `ReadError(errno=24)` or `errno=23` — descriptor exhaustion, not the current
   symptom.

Alongside that line, collect host-level counters around the same minute (open
fds, sockets in `CLOSE_WAIT`/`TIME_WAIT`, `lifeos-supabase-*` thread count).
Render Free has no shell, so this requires an existing metric/log surface or a
platform metric, not code. Separately confirm whether the lifecycle-task
warnings have disappeared from `ASYNC_TASK_DUMP` / `TASK_STARVATION` while a
genuine bounded stall is still reported.

---

## 11. Delivery state of this documentation change

`INVESTIGATION.md` is the only file changed by the documentation commit. The
investigation's code commit is `17942f1`, verified present on `origin/main`;
the current `origin/main` HEAD is recorded in the final task report after the
push, because a file cannot contain the SHA of its own commit.
