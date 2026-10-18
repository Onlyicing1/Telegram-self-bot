# IMPLEMENTATION_REPORT.md — Task Runtime Database Failures: diagnosis + the stall-diagnostics identity defect

> This file describes ONLY the current state of the repository for the **task
> runtime database-failure diagnosis** phase and **replaces every previous
> report in full** — it is not an appended report. Earlier reports (the
> AED security round, the AED database foundation, the peer-resolution,
> bot-exclusion and archive-UX rounds, the emoji/private-message rounds) remain
> readable in git history and in `docs/investigations/`.

| Item | Value |
|---|---|
| Stage | **Diagnosis + one confirmed defect fixed** (task runtime DB failures) |
| Date | 2026-10-18 |
| Project / remote | LifeOS Telegram self-bot, `Onlyicing1/Telegram-self-bot` (`origin`) |
| Branch | `main` (work done in a detached worktree at `origin/main`, pushed with `git push origin HEAD:main`) |
| Base commit | `69364c7194fb4fd409df66b900058e20eccb15e8` — verified `= origin/main` at task start (`git fetch origin` + `git rev-parse origin/main`) |
| Live Telegram / Render / Supabase | **NOT touched.** No deployment access, no Render shell, no `.env`, no credentials, **no SQL**, no Supabase connection, no restart, no Render config change |
| Actionable production log lines | 2 of 4 are the existing protections working as designed; 2 of 4 had a **provable, reproduced defect in the diagnostics' identity model** (fixed here) |

---

## 1. Objective and scope

Identify the evidence-based root cause of four recurring production symptoms —
`TASK_FALLBACK_CLASSIFIED reason=local_resource … [Errno 11]`,
`Supabase event task query failed; using fallback: <ConnectionTerminated …>`,
`TASK_NO_PROGRESS` and `TASK_STARVATION` — and apply only the smallest
correction the repository can prove is missing.

Nothing about the runtime's shape was changed: no second scheduler, executor,
Supabase client, update loop or retry loop; no schema change; no
`DATABASE_ARCHITECTURE.md` change; no environment/Render change. The single
repository authority, bounded concurrency, truthful fallback classification,
cancellation behaviour and existing task semantics are all preserved (and
re-pinned by the tests listed in §6).

Files inspected (current versions at `origin/main` = `69364c7`):
`backend/ai/database/task_repository.py`, `backend/db/client.py`,
`backend/runtime/operation_watchdog.py`, `backend/runtime/diagnostics.py`,
`backend/ai/task_event_dispatcher.py`, `backend/ai/task_scheduler.py`,
`backend/ai/database/manager.py`, `backend/ai/task_execution.py`,
`backend/runtime/task_guard.py`, `backend/runtime/heartbeat.py`,
`backend/health.py`, `backend/bot/router.py`, `backend/bot/handlers/task_events.py`,
`backend/ai/persistence.py`, the four focused suites named in §6, plus
`IMPLEMENTATION_REPORT.md` and `INVESTIGATION.md`.

---

## 2. What each log line actually is (provenance, on `origin/main`)

| Log line | Exact producer | Introduced by (all verified ancestors of `origin/main`) |
|---|---|---|
| `TASK_FALLBACK_CLASSIFIED reason=… exception=… message=…` | `SupabaseTaskRepository._mark_fallback` (`backend/ai/database/task_repository.py`) — the once-per-episode structured diagnostic | `2551970`, extended by `4b20653` (2026-09-11) |
| `Supabase event task query failed; using fallback: <exception>` | same file, `list_event_tasks` → `_degrade(…)` (the per-call-site wording) | `40b57e0` / earlier |
| `TASK_NO_PROGRESS — N bounded tasks unchanged since last dump` | `backend/runtime/diagnostics.py::_dump_tasks` | `3893d77` (permanent/bounded classification, 2026-08-12); bounded by `4b20653` |
| `TASK_STARVATION — N tasks unchanged across 3+ dumps` | `backend/runtime/diagnostics.py::_detect_starvation` | `3893d77` |

Consequence for the first question in the brief: **these lines cannot predate
the protections.** The bounded 4-worker pool and 8 s transport deadline
(`40b57e0`), the `local_resource` classification (`2551970`), the 5 s
local-resource cooldown and the once-per-episode diagnostic (`4b20653`), and
the permanent-vs-bounded diagnostics classification (`3893d77`) are all present
in `origin/main`'s history — the observed lines are the *post-protection*
behaviour of that code.

**Deployed-commit evidence is missing and is reported as such.** No commit
marker exists in the repository (`grep` for `RENDER_GIT_COMMIT`/`GIT_COMMIT`/
`BUILD_SHA`/`__version__` in `backend/` → nothing), `render.yaml`/`Procfile`
carry no commit pin, the reported lines carry no timestamps in the brief, and
this workspace holds no Render credential (`freebuff-env list` → no keys), so
the deployed commit and the log timestamps cannot be established read-only from
here. The four log lines are, however, reproducible only by code that contains
the protections above, which is what makes the deployment-vs-`main` question
moot for this phase.

---

## 3. The DB failure path, traced (and why it contains no provable defect)

```
Telegram update
  → bot/handlers/task_events.py::_task_event_handler
  → TaskEventDispatcher.handle_event            (ai/task_event_dispatcher.py)
  → SupabaseTaskRepository.list_event_tasks     (ai/database/task_repository.py)
  → repository._run(fn)                         ← cooldown gate lives here
  → backend.db.client.run_sync_db               (single bounded dispatch)
        loop.run_in_executor(_get_db_executor())   # ThreadPoolExecutor(max_workers=4)
        → guarded_await(..., timeout=10s)          # runtime/operation_watchdog.py
              postgrest/httpx sync call, transport deadline 8s (= _DB_HTTP_TIMEOUT)
  → exception classification → _mark_fallback/_degrade → in-memory fallback result
  → 0 executions + bounded TASK_EVENT_TRACE warnings (never raises into the event path)
```

1. **Local OS resource exhaustion (symptom 1).** The transport error is
   `httpx`/`httpcore`'s wrapper around `OSError(11)`; `_is_local_resource_failure`
   walks the whole `__cause__`/`__context__` chain and matches
   `{EAGAIN/EWOULDBLOCK, EMFILE, ENFILE, ENOMEM, ENOBUFS}`. The line's
   `reason=local_resource` is proof the walk succeeded — i.e. the classification
   that exists to stop EAGAIN being reported as a Supabase outage *worked*. The
   failure then arms the bounded 5 s cooldown (no durable call is re-issued
   inside it, the window is never extended by a skipped call, a successful
   durable operation clears it) and emits the diagnostic once per episode.
   **This is the protection, not the defect.**
2. **A peer-terminated pooled HTTP/2 connection (symptom 2).** Installed
   `postgrest==0.16.11` creates its session with `http2=True`
   (`SyncPostgrestClient.create_session`), so Supabase REST traffic runs over
   TLS+HTTP/2. A GOAWAY from the peer is rendered by `h2` as
   `<ConnectionTerminated error_code:…, last_stream_id:…, additional_data:…>`
   and surfaced by httpcore as a protocol error carrying exactly that text —
   which is the string in the log line. httpx performs **no** retry, so one dead
   pooled connection fails one operation; with no errno in the chain it is
   classified `FALLBACK_REASON_UNAVAILABLE` (truthful: the durable store did not
   answer this request), the read degrades to the in-memory fallback
   (**non-durable**) and the caller/panel is told so (`fallback_active` /
   `fallback_reason` / the rendered fallback note). A transient transport
   termination therefore does **not** mean Supabase is down, and it never
   reaches the durable write path as a silent success.
3. **No defect was provable anywhere else on this path**: the pool is bounded at
   four workers and reused (threads are stable across 24 calls); the transport
   deadline is strictly below the dispatch budget; a timed-out dispatch emits one
   `OP_TIMEOUT` and cannot be cancelled inside the thread, so the code pins the
   transport shorter than the watchdog; `CancelledError` is re-raised at every
   boundary; `_run`'s cooldown skip carries its own honest reason so a skipped
   call can never be re-labelled as an outage; a genuine store failure is never
   suppressed (every attempt stays visible — an intentional, test-pinned
   contract); and `RepositoryManager` still owns the one task repository, so the
   process degrades to exactly one in-memory fallback. All of it is re-verified
   by the suites in §6.
4. `TASK_NO_PROGRESS` / `TASK_STARVATION` are **not** produced by this path: they
   come from the 60 s diagnostics dump, and §4 shows the defect that made them
   fire for tasks that were not stalled at all.

Deliberately **not** done, and why: no retry was added to `run_sync_db` (a
re-dispatch is unsafe for this repository's non-idempotent inserts, whose
exactly-once guarantees rest on CAS/unique keys — and a retry path is out of
scope by the brief); the local-resource cooldown was **not** broadened into a
process-wide gate (that would change every DB surface's degradation semantics
and its honesty contract); and no recovery behaviour was touched.

---

## 4. The confirmed defect: stall diagnostics identified tasks by NAME

`backend/runtime/diagnostics.py` kept its three per-task registries — the
previous stack, the unchanged-dump counter and the first-seen instant — **under
the task name**. A task name is reusable: the runtime re-creates bounded tasks
under a stable name precisely when things are unhealthy
(`lifeos-keepalive-recovery`, `lifeos-heartbeat-recovery`,
`lifeos-failsafe-retry`, `lifeos-recovery-retry`, the `*:preload_state` tasks
re-spawned on a rebuild, `panel:delayed-delete`, `lifeos-callback-trace-send`).
Keyed by name, a finished task's counter and first-seen instant were inherited
by the **next** task created with that name, so:

* a task that had just started was reported as `TASK_NO_PROGRESS` after a
  **single** dump (`Unchanged dumps: 2`/`3` inherited from the dead task);
* it reached the `TASK_STARVATION` threshold with **fewer dumps than the
  three-dump contract**, and its reported `(elapsed)` time belonged to the
  already-finished task (`unchanged for 3 dumps (0s)`).

Reproduced before the fix (focused test, fixed clocks, no live anything):

```
WARNING backend.diagnostics_loop TASK_NO_PROGRESS — 1 bounded tasks unchanged since last dump:
  Task: lifeos-keepalive-recovery
  Elapsed waiting: 0s
  Unchanged dumps: 2            <-- inherited by a task created milliseconds earlier
WARNING backend.diagnostics_loop TASK_STARVATION — 1 tasks unchanged across 3+ dumps:
  STARVATION: lifeos-keepalive-recovery unchanged for 3 dumps (0s)
```

That is exactly the recurring pair in the production logs, and it is why those
two lines — in a runtime that is neither stalled nor deadlocked — **do not prove
a stall**. The DB-failure episode only makes it more likely: recovery attempts
and preloads are re-created under those names while the DB path is failing.

---

## 5. The fix (smallest change that removes the false report)

| File | Change | Reason |
|---|---|---|
| `backend/runtime/diagnostics.py` | `_prev_stacks`, `_stack_unchanged_count` and `_task_first_seen` are keyed by the **pending `asyncio.Task` object** instead of its name; `task_info`/`elapsed`, `_detect_starvation`, `_detect_deadlocks` and `_detect_slow_handlers` follow the same key; per-task state is pruned against the **pending task set** (so a finished task's counters can be neither reported forever nor inherited); `stalled`/`permanent`/`bounded` sets are task-based. The permanent/bounded classification, the loop-latency stall check, the report layout, the 60 s interval and the three-dump starvation threshold are unchanged | "Unchanged" must mean **the same task** unchanged. Identity, not a reusable name, owns the counter — and two concurrent tasks sharing a name keep separate state and separate report entries, so a genuine stall is still reported for the task that really is stalled |
| `tests/test_runtime_diagnostics_classification.py` | Three new tests: (1) a name reused by a new task starts from zero, is not reported after one dump, and **is** still reported for three dumps of its own existence; (2) two concurrent same-name tasks are tracked per task; (3) the deadlock report still names the blocked task (the edited detector's only reachable shape: a task created directly from `Event.wait()`) | Both stall tests **fail against the previous implementation** and pass after it; the 12 pre-existing diagnostics tests pass in both states |

The fix touches only the report path. It removes a false warning and never
suppresses a real one: a bounded task that is genuinely unchanged for three
dumps of its own existence is still reported — asserted in the same tests.

Also documented, deliberately **not** changed (latent, no live false positive
today): a task created through `backend/runtime/task_guard.py` reports the
wrapper's coroutine name (`_wrapper`/`_immortal_wrapper`) and `task_guard.py` as
its await location, because a task's `cr_frame` is its **outermost** coroutine
frame (verified empirically). `_PERMANENT_CORO_PATTERNS`' `uvicorn`/`serve`
entries and the `telethon/` source marker can therefore never match a
wrapper-created task. Every permanent runtime loop carries a permanent *name*,
so the classification is correct in practice; changing it would mean changing a
core runtime module for no reproduced symptom. The residual
`asyncio.to_thread` DB paths that still bypass the shared bounded pool
(`backend/ai/config_store.py::_run_sync`, `backend/ai/database/usage_reader.py`,
`usage_recorder.py`, `backend/services/ghost_seen_v2.py`,
`backend/ai/tools/memory.py`, `backend/ai/engine/dispatcher.py`) are likewise
left as a documented follow-up: they are bounded by the loop's default executor
and there is no evidence they produced any of the four lines.

---

## 6. Tests actually run (commands and real results)

All commands were run in the task worktree (`/home/daytona/codebase/.taskrt`, a
detached checkout of `origin/main` = `69364c7`) with the repository virtualenv
(`../.venv/bin/python`, CPython 3.10.12; the workspace venv carries
`telethon 1.34.0` while `backend/requirements.txt` pins `1.43.0`, and neither the
change nor the tests depend on Telethon).

| Command | Result |
|---|---|
| `python -m pytest tests/test_task_fallback_classification.py tests/test_task_fallback_cooldown.py tests/test_runtime_diagnostics_classification.py tests/test_local_resource_bounds.py -q` (before the change) | **42 passed** — the existing protections are intact |
| `python -m pytest tests/test_07_diagnostics.py tests/test_11_runtime_wiring.py tests/test_task_fallback_classification.py tests/test_task_fallback_cooldown.py tests/test_local_resource_bounds.py tests/test_task_repository.py tests/test_task_scheduler.py tests/test_task_execution.py tests/test_runtime_diagnostics_classification.py -q` | **106 passed** |
| `python -m pytest tests/test_runtime_diagnostics_classification.py -q` (with the fix) | **15 passed** (12 pre-existing + 3 new) |
| same suite against the **pre-fix** `backend/runtime/diagnostics.py` | **2 failed** — the two new stall tests fail exactly on the production symptom (`assert 'lifeos-keepalive-recovery' not in {'lifeos-keepalive-recovery'}`), the 12 pre-existing tests pass |
| `python -m pytest tests -q` (full suite, final state) | **6370 passed, 27 skipped, 40 failed** in 124 s |
| `python -m pytest tests/test_colored_buttons.py -q` in a pristine `origin/main` worktree | **40 failed, 12 passed** — the identical, pre-existing failure set (pending colored-button expectations, unrelated to this phase; nothing in the task-runtime, DB, watchdog or diagnostics area fails) |
| `python -m py_compile backend/runtime/diagnostics.py tests/test_runtime_diagnostics_classification.py` | OK (exit 0) |
| `git diff --check` | clean |

---

## 7. Production verification — NOT performed (and what would verify it)

The fix is **unverified in production**: no deployment was triggered, no Render
shell or log access was used, no live Telegram/Supabase session exists here, and
no service was restarted (all out of scope by the brief). What would confirm it:

1. The `ASYNC_TASK_DUMP` lines must stop reporting a task whose `Elapsed waiting`
   is smaller than the interval it has existed for, and must keep reporting the
   same task when it is *genuinely* unchanged for three dumps of its own
   existence.
2. `TASK_NO_PROGRESS`/`TASK_STARVATION` bodies should name tasks whose reported
   `Unchanged dumps` is consistent with that task's own lifetime.
3. For the DB lines, the deployed commit (Render's build log or the runtime's own
   `RENDER_GIT_COMMIT`, neither of which is readable from here) is needed to
   correlate the four lines with a deployment; the brief's lines carry no
   timestamps.

---

## 8. Commit, push and remote verification

| Item | Value |
|---|---|
| Code commit | `58fa664296c1d2d86682072210965d1b1b12d098` — `fix(runtime): key task stall diagnostics by task, not by task name` |
| Base commit | `69364c7194fb4fd409df66b900058e20eccb15e8` (previous `origin/main`) |
| Push | `git push origin HEAD:main` → `69364c7..58fa664  HEAD -> main` |
| Remote `main` after the push | `git fetch origin` → `git rev-parse origin/main` = `58fa664296c1d2d86682072210965d1b1b12d098` = local `HEAD` |
| Ancestry | `git merge-base --is-ancestor 58fa664 origin/main` → exit 0 (`ANCESTRY OK`) |
| Working tree | only the two delivered files changed in the task worktree; the shared checkout keeps its pre-existing `M INVESTIGATION.md` and untracked agent directories (untouched) |
| Documentation follow-up | a second `docs(report)` commit records this delivery block (its own SHA can only exist after the code commit) |

---

## 9. Unresolved limitations / missing evidence

* **Deployed commit unknown** (no commit marker in the repo, no Render
  credential here, no timestamps in the reported lines). The logs are consistent
  with post-protection code, but that correlation is inference, not a
  measurement.
* **The OS-level origin of `EAGAIN` is not provable from the repository.** The
  classification ("local resource, not a store outage") is the codebase's own
  documented reading of `[Errno 11]` and is test-pinned; proving *which* local
  resource ran short (ephemeral ports, file descriptors, memory) needs
  host-level metrics (`ss -s`, `/proc/<pid>/limits`, fd counts, thread counts)
  that this workspace cannot collect.
* **No retry was added for transient pooled-connection deaths** (GOAWAY). The
  degradation is honest (non-durable, reported), but a task write attempted in
  exactly that window is not durable; a safe design would need read-only retry
  semantics, which is a larger, riskier change than this phase's brief allows.
* **Latent diagnostics blind spots are documented, not fixed** (§5): the
  wrapper frames hide the real coroutine from the coro-name/source signals, and
  the residual `asyncio.to_thread` DB paths still bypass the single bounded
  pool.
* `tests/test_colored_buttons.py` keeps its 40 pre-existing failures (identical
  on a pristine `origin/main`); they are unrelated to this phase but are not
  fixed here.
