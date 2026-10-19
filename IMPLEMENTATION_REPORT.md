# IMPLEMENTATION_REPORT.md — Runtime DB `Errno 11` diagnosis + the remaining lifecycle-task false positives

> This file describes ONLY the current state of the repository for this phase and
> **replaces every previous report in full** — it is not an appended report.
> Earlier reports (the task-stall identity round at `58fa664`, the AED security
> and archive rounds, the emoji/private-message rounds) remain readable in git
> history and in `docs/investigations/`.

| Item | Value |
|---|---|
| Stage | Diagnosis + one confirmed classification defect fixed |
| Date | 2026-10-19 |
| Project / remote | LifeOS Telegram self-bot, `Onlyicing1/Telegram-self-bot` (`origin`) |
| Branch | `main` |
| Base commit | `3f0ef80b60d65c3a10582b6e86d6be4a06190c17` — `git fetch origin` → `git rev-parse origin/main` = this, and the local tree was fast-forwarded to it (it was four commits ahead of the previous workspace HEAD `702c1d1`; merged `--ff-only`, not rebased) |
| Live Telegram / Render / Supabase | **NOT touched.** No deployment, no Render shell, no `.env`, no credentials, **no SQL**, no Supabase connection, no restart, no Render/environment change |
| Workspace interpreter | CPython **3.10.12** (the workspace has no venv); `telethon 1.34.0` installed vs `1.43.0` pinned by `backend/requirements.txt` — the only suites affected are the pre-existing button-colour tests (§5) |

---

## 1. Objective

Two post-deployment symptoms were to be investigated **independently**:

* **A** — repeated `httpx`/`httpcore` `ReadError: [Errno 11] Resource temporarily
  unavailable` inside `backend.ai.database.task_repository`, followed by
  `TASK_FALLBACK_CLASSIFIED reason=local_resource` and the in-memory fallback,
  while the process stayed `READY` with both clients connected; a separate
  `ConnectionTerminated` HTTP/2 error was also seen.
* **B** — `TASK_NO_PROGRESS` / `TASK_STARVATION` after `58fa664` still naming
  long-lived lifecycle tasks: `Task-1` (`main` awaiting
  `supervisor.shutdown_event.wait()` in `backend/main.py`) and `Task-27`
  (`main` awaiting uvicorn lifespan machinery in `uvicorn/lifespan/on.py`).

No assumption was made that they share a cause. They do not.

Files read in full before any edit: `IMPLEMENTATION_REPORT.md`,
`INVESTIGATION.md`, `backend/runtime/diagnostics.py`,
`tests/test_runtime_diagnostics_classification.py`, `backend/db/client.py`,
`backend/ai/database/task_repository.py`, `tests/test_task_fallback_cooldown.py`,
`tests/test_task_fallback_classification.py`, `tests/test_local_resource_bounds.py`,
`backend/runtime/operation_watchdog.py`, `backend/ai/task_event_dispatcher.py`,
`backend/ai/task_scheduler.py`, `backend/ai/database/manager.py`,
`backend/runtime/task_guard.py`, `backend/runtime/heartbeat.py`,
`backend/main.py`, `backend/runtime/supervisor.py` (start, web-server and
orphan-cancel paths) — plus the installed `supabase 2.4.2` / `postgrest 0.16.11`
/ `httpcore 1.0.9` / `h2 4.4.1` sources named in §3.

---

## 2. Confirmed facts about the DB path (A) — source and package verified

| Claim in the brief | Verified? | Evidence |
|---|---|---|
| One reusable `ThreadPoolExecutor`, 4 workers | **Yes** | `backend/db/client.py::_get_db_executor` — `ThreadPoolExecutor(max_workers=4, thread_name_prefix="lifeos-supabase")`, created once under `_db_executor_lock`; `run_sync_db` dispatches every call through it |
| 8 s transport timeout, 10 s dispatch timeout | **Yes** | `_DB_HTTP_TIMEOUT = 8.0` passed to `ClientOptions(postgrest_client_timeout=…)`; `_DB_TIMEOUT = 10.0` → `guarded_await(..., timeout=10.0)`; `0 < 8 < 10` is test-pinned |
| `ClientOptions(postgrest_client_timeout=…)` is real in the pinned library | **Yes** | `supabase 2.4.2` exports `ClientOptions` with `postgrest_client_timeout: Union[int, float, httpx.Timeout] = 120`; `get_db()` builds the one shared client with `8.0` |
| 5 s local-resource cooldown in `SupabaseTaskRepository` | **Yes** | `_LOCAL_RESOURCE_COOLDOWN_SECONDS = 5.0`; the gate lives in `_run`/`_run_checked`; a skipped call raises `LocalResourceCooldown` carrying `reason=local_resource`; skipped calls never extend the deadline; a successful durable op clears it |
| In-memory fallback on failure | **Yes** | every repository method catches, calls `_degrade`/`_mark_fallback`, and returns `InMemoryTaskRepository` data annotated `fallback_backend` / `fallback_reason` (non-durable, truthfully labelled) |
| Supabase REST traffic is HTTP/2 | **Yes** | `postgrest 0.16.11::SyncPostgrestClient.create_session` constructs `SyncClient(..., http2=True)`; `h2 4.4.1` is installed. `ConnectionTerminated` is h2's own event/exception class, so it belongs to this same transport |
| A read timeout on an SSL socket surfaces as `ReadError(OSError(EAGAIN))` | **No — disproved** | `httpcore._backends.sync.SyncStream.read` maps `socket.timeout → ReadTimeout` and `OSError → ReadError`. Reproduced locally (a TLS server that never writes, client `settimeout(0.2)`, see §3): CPython raises `TimeoutError` with `errno=None`, i.e. `httpx.ReadTimeout`, **never** `ReadError [Errno 11]` |

### What `[Errno 11]` means in this exact chain

`httpx.ReadError("[Errno 11] …")` wraps `httpcore.ReadError`, which wraps the raw
`OSError` from `SyncStream.read` → `SSLSocket.recv`. `OSError(EAGAIN)` is
constructed as the **`BlockingIOError`** subclass (verified:
`type(OSError(errno.EAGAIN, …)) is BlockingIOError`), so the observed text is a
socket-layer "would block / no data available right now", not an HTTP status, not
a PostgREST error, and not a store response.

The discrimination the brief asked for, with the exception type each hypothesis
would actually produce:

| Hypothesis | Expected surfaced failure | Observed? |
|---|---|---|
| Socket read returned `EAGAIN` (local would-block) | `httpx.ReadError` ← `BlockingIOError(errno=11)` | **Yes — the production text** |
| Executor saturation / queued call beyond the dispatch budget | `OperationTimeoutError` (no errno) → `reason=unavailable` | No |
| Transport read deadline expired | `httpx.ReadTimeout` (no errno) → `reason=unavailable` | No (disproved by the reproduction above) |
| File-descriptor exhaustion | `EMFILE`/`ENFILE` (24/23) — also `local_resource`, but a different errno | No |
| Remote HTTP/2 connection termination | `RemoteProtocolError` ← `ConnectionTerminated` (no errno) → `reason=unavailable` | Seen as a **separate** line, not as the Errno-11 chain |
| Peer reset / closed socket | `ECONNRESET`(104) / `EBADF`(9) → `unavailable` | No |

So: the production `ReadError [Errno 11]` is **not** evidence that Supabase was
unavailable, **not** an expired timeout, and **not** executor saturation. It is
a would-block returned by the local socket layer under the synchronous Supabase
HTTP call. What the repository *cannot* establish is **why** the local socket
layer produces it on Render: that needs host-level data (fd/socket/thread
counters, egress/NAT behaviour, gVisor-level syscall behaviour, and the h2
connection-reuse pattern) which this workspace cannot collect. It is reported as
**unproven**, not guessed at.

### Cooldown — verified, and now measured deterministically

By construction it bounds durable re-attempts to at most one per 5 s window
while the episode lasts; a skipped call is served from the in-memory fallback
with `reason=local_resource`, never re-labelled as an outage, and never extends
the window. A new test drives **180 calls over 90 simulated seconds** (a
monotonic-clock shim, no sleeping) and asserts **≤ 19** durable attempts instead
of 180. No worker count, timeout, retry, Render setting or cooldown scope was
changed: an automatic retry is unsafe for this repository's non-idempotent
inserts, and a process-wide breaker would change every DB surface's degradation
semantics.

### The one narrow change on A

`TASK_FALLBACK_CLASSIFIED` recorded only the outer exception's type and text. That
cannot distinguish a *read* would-block (`ReadError`) from a *connect*-phase one,
an fd shortage (different errno) or a timeout (`ReadTimeout`, no errno), which is
exactly the ambiguity that kept this symptom unresolved across two phases. The
once-per-episode line now also prints `chain=<names+errno>` — **class names and
integer errno only**, capped at 6 links: no message, URL, header, token or user
data. Nothing is retried or suppressed; classification and fallback semantics are
unchanged.

---

## 3. Local reproduction used to distinguish timeout from would-block

Run outside the repository (no source or test depends on it):

```
TLS server (self-signed) accepts, then never writes
client: wrap_socket → settimeout(0.2) → recv(1024)
→ TimeoutError errno=None   (isinstance socket.timeout → True)
```

`httpcore`'s map turns that into `httpx.ReadTimeout`, so a `ReadError` carrying
`errno=11` cannot be a read deadline. The classification matrix in §2 is pinned
by tests in `tests/test_task_fallback_classification.py`:
`ReadError`/`WriteError` + `EAGAIN` → `local_resource`; `ReadTimeout` /
`ConnectTimeout` → `unavailable`; `ECONNRESET` → `unavailable`; `EMFILE` →
`local_resource`.

---

## 4. Workstream B — confirmed defect in the permanent/bounded classifier

### How the classification is actually computed

`_dump_tasks()` → `t.get_coro()` → the coroutine's **own** outermost frame
(`cr_frame`) → `coro_name` (`__name__`) and `await_loc`
(`co_filename:f_lineno`) → `_is_permanent_task(name, coro_name, await_loc)`.
Permanent tasks are excluded from `stalled`, `_detect_starvation`,
`_detect_deadlocks` and `_detect_slow_handlers`; the per-task identity registry
(`_prev_stacks`, `_stack_unchanged_count`, `_task_first_seen`, keyed by the task
object, pruned each dump) is untouched.

### Why the two lifecycle tasks were classified BOUNDED

`_is_permanent_task` had three signals: the runtime's assigned task names, the
coroutine-name patterns, and a **`telethon/`-only** source marker.

* `Task-1` — name `Task-1` (not assigned by the runtime), coroutine `main`,
  await location `backend/main.py:…`. `"main"` matches none of
  `run_until_disconnected`, `_heartbeat_loop`, `_update_loop`, …; the source was
  not inside `telethon/` → **BOUNDED**.
* `Task-27` — coroutine `main`, await location
  `…/uvicorn/lifespan/on.py:…` (uvicorn's detached `LifespanOn.main`, created by
  `LifespanOn.startup()` with an auto-generated `Task-N` name). No name match, no
  pattern match, and `uvicorn`/`serve` were only *coroutine-name* patterns — not
  source markers → **BOUNDED**.

Reproduced against the pre-fix module (same tests, original
`backend/runtime/diagnostics.py` restored from `HEAD`): **4 failed / 17 passed**,
with the exact production output:

```
TASK_NO_PROGRESS — … Task: Task-1 / Task: Task-27
TASK_STARVATION — 2 tasks unchanged across 3+ dumps:
  STARVATION: Task-1 unchanged for 3 dumps (0s)
  STARVATION: Task-27 unchanged for 3 dumps (0s)
  Stack trace: File "/app/.venv/lib/.../uvicorn/lifespan/on.py", line 3, in main
```

### Why they are expected waits (not blocked application work)

* `backend/main.py::main` awaits `supervisor.start()`, and `start_diagnostics()`
  is called **inside** that method (verified at
  `backend/runtime/supervisor.py:201`). Therefore no dump can observe `main`
  during startup: whenever diagnostics runs, `main` can only be parked on
  `await supervisor.shutdown_event.wait()` — the deliberate process lifecycle
  wait — until shutdown. The process entry task is created by `asyncio.run()`
  with an auto-generated name, so its own source file is the only reliable
  identifier.
* `uvicorn/lifespan/on.py::main` is library machinery: uvicorn creates it with
  `loop.create_task(self.main())` and it parks on its receive queue until the
  server shuts down. The app's uvicorn task is separately named
  `lifeos-web-server` (already permanent by name).

### Fix (classification only)

| File | Change |
|---|---|
| `backend/runtime/diagnostics.py` | `_PERMANENT_SOURCE_MARKERS` gains `uvicorn/lifespan/` (library machinery, matched by the coroutine's own frame, not by name). A new narrow rule classifies a task permanent only when its coroutine is exactly `main` **and** that coroutine's own source file is the process entrypoint `backend/main.py` (`_source_file` strips the `:line` suffix first). `serve` moved from the substring tuple to an **exact** name set (`_PERMANENT_CORO_EXACT`), because as a substring it also matched application coroutines such as `_observe_durable_ceiling` and any `_preserve_*`, silently hiding a real stall there. |
| `tests/test_runtime_diagnostics_classification.py` | Six new tests: the two production tasks are permanent (compiled with the real frame file names, not fake frames); an application coroutine named `main` in another file stays bounded; an app coroutine that awaits a lifecycle-style shutdown wait is still reported as `TASK_NO_PROGRESS`/`TASK_STARVATION`; `serve` exact vs `_observe_*`/`_preserve_*`; and an end-to-end four-dump run of the two production tasks producing no warning at all. |

**Why the task is not simply renamed.** Naming it `lifeos-main` would also
require adding it to `RuntimeSupervisor._cancel_orphan_tasks`' protected set —
that method cancels every pending task whose name is not protected, so renaming
the process entry task without that change would let a hard reset cancel the
process lifecycle task itself. The classification-only rule is the smaller and
safer fix, and it is precise: an application coroutine always keeps its own frame
file, so it cannot be caught by the entrypoint rule.

Preserved: the task-object identity registry and pruning from `58fa664`,
counter/elapsed accounting, the three-dump starvation threshold, the deadlock and
slow-handler reports, and real bounded-stall detection (asserted by both the
pre-existing tests and the new controls).

---

## 5. Tests and exact results

| Command | Result |
|---|---|
| `pytest tests/test_runtime_diagnostics_classification.py tests/test_task_fallback_classification.py tests/test_task_fallback_cooldown.py tests/test_local_resource_bounds.py -q` | **57 passed** |
| the same diagnostics file against the **pre-fix** `diagnostics.py` | **4 failed, 17 passed** — the four new failure-mode tests, reproducing `STARVATION: Task-1` / `Task-27` verbatim; the "app coroutine awaiting a shutdown wait is reported" control passed before and after |
| `pytest … + tests/test_07_diagnostics.py tests/test_11_runtime_wiring.py tests/test_task_repository.py tests/test_task_scheduler.py tests/test_task_execution.py -q` | **119 passed** |
| `pytest tests -q -p no:cacheprovider` (full suite) | **6382 passed, 27 skipped, 40 failed** in 124.39 s |
| `pytest tests/test_colored_buttons.py -q` | **40 failed, 12 passed** — the entire failure set of the full run, identical to the recorded baseline; failures are `TypeError: Button.inline() got an unexpected keyword argument 'style'` / `KeyboardButtonCallback` lacking the style field, i.e. the workspace's **Telethon 1.34.0** vs the pinned **1.43.0**. Confirmed pre-existing and unrelated; not fixed here |
| `python3 -m py_compile backend/runtime/diagnostics.py backend/ai/database/task_repository.py tests/test_runtime_diagnostics_classification.py tests/test_task_fallback_classification.py tests/test_task_fallback_cooldown.py` | exit 0 |
| `git diff --check` | clean |

Accounting: the previous baseline recorded `6370 passed`; this phase adds
**12** tests (6 diagnostics, 5 classification/chain, 1 cooldown) → `6382`.
**No newly introduced failures.** The 40 failures are the same pre-existing
button-colour failures; 27 skips are unchanged opt-in/live tests.

---

## 6. Files changed

| File | Why |
|---|---|
| `backend/runtime/diagnostics.py` | Fix the proven permanent/bounded misclassification of the two lifecycle tasks; remove the `serve` substring over-match |
| `backend/ai/database/task_repository.py` | Add the bounded, secret-free exception-chain field to the once-per-episode fallback diagnostic (the only ambiguity left in the Errno-11 evidence) |
| `tests/test_runtime_diagnostics_classification.py` | Regression coverage for the two lifecycle tasks, the entrypoint rule, the app-side controls, and exact `serve` |
| `tests/test_task_fallback_classification.py` | `ReadError`/`WriteError` shape, timeout/ECONNRESET/EMFILE discrimination, chain content bound and the logged chain field |
| `tests/test_task_fallback_cooldown.py` | Deterministic 90-second episode proving the cooldown bounds durable attempts |
| `IMPLEMENTATION_REPORT.md` | This report (full replacement) |

Not changed: no schema, no migration, no SQL, no `DATABASE_ARCHITECTURE.md`, no
second client/executor/scheduler/retry loop, no worker count or timeout change,
no Render or environment change, no user-facing behaviour.

---

## 7. Production status — NOT verified

* No deployment was triggered, no Render shell/log access was used, no service
  was restarted, and no live Telegram/Supabase session exists in this workspace.
  A successful commit and push is **not** proof of deployment or runtime
  recovery.
* The two warnings are expected to disappear from the next
  `ASYNC_TASK_DUMP`/`TASK_STARVATION` body (the tasks will be listed as
  `[PERMANENT]`), and any genuine bounded stall must still be reported.
* The `Errno 11` condition itself is untouched: the change makes the **next
  episode decidable**, it does not remove the failure. If a task write lands in
  such a window it is still degraded to the in-memory fallback and honestly
  labelled non-durable.

### Follow-up evidence that would settle the remaining question

1. The next `TASK_FALLBACK_CLASSIFIED` line's `chain=` field: `ReadError` vs
   `RemoteProtocolError` vs `ReadTimeout`, and which errno, at which frequency.
2. Host-level counters around the episode (open fds, sockets in `CLOSE_WAIT`/
   `TIME_WAIT`, thread count of `lifeos-supabase-*`) — Render Free has no shell,
   so this needs an existing metric/log surface or a platform metric, not code.
3. Whether `ConnectionTerminated` correlates with the Errno-11 bursts (same
   minute) or is independent connection expiry.

---

## 8. Remaining known limitations

* **The OS-level origin of `EAGAIN` is unproven.** The repository can prove it is
  a local socket-layer would-block (not a timeout, not saturation, not a store
  error) and can prove the cooldown bounds the retry rate, but not *which* local
  resource/condition produced it on Render.
* **No retry for a terminated pooled HTTP/2 connection.** Deliberate: the
  repository's writes are non-idempotent and rely on CAS/unique keys; a retry
  path needs a separate, carefully designed change.
* **`RuntimeSupervisor._cancel_orphan_tasks` still cancels uvicorn's lifespan
  task** on a hard reset (it is auto-named and unprotected). That is existing
  behaviour, outside this phase; the new classification only stops reporting it
  as a stalled bounded task.
* **`heartbeat.py`'s own permanent-name counter** still counts only assigned
  names, so its `bounded_tasks` telemetry is two higher than the diagnostics'
  count (it produces no warning). Left unchanged to avoid duplicating the
  classifier in a second runtime-critical loop.
* **The 40 button-colour failures** remain (Telethon version mismatch in this
  workspace) and are unrelated to this phase.
