# IMPLEMENTATION_REPORT.md — Supabase transport-failure evidence (diagnosis + one bounded diagnostic)

> This file describes ONLY the current state of the repository for this phase and
> **replaces every previous report in full** — it is not an appended report.
> Earlier reports (the lifecycle-task stall round at `17942f1`, the AED security
> and archive rounds, the emoji/private-message rounds) remain readable in git
> history and in `docs/investigations/`.

| Item | Value |
|---|---|
| Stage | Diagnosis + the smallest safe diagnostic that decides the remaining cause |
| Date | 2026-10-19 |
| Project / remote | LifeOS Telegram self-bot, `Onlyicing1/Telegram-self-bot` (`origin`) |
| Branch | `main` |
| Base revision | `aee3e163e914eae72cafa922eb3d38fd7a3ca697` (`INVESTIGATION.md` commit); relevant code commit `17942f167591618c4ba9111d15f2f7cffc7d789d` |
| Live Telegram / Render / Supabase | **NOT touched.** No deployment, no Render shell or log stream, no `.env`, no credentials, no SQL, no migration, no restart, no environment change |
| Workspace interpreter | CPython **3.10.12**; inspected dependencies `supabase 2.4.2`, `postgrest 0.16.11`, `httpx 0.27.0`, `httpcore 1.0.9`, `h2 4.4.1` |

---

## 1. Objective

Resolve the recurring Supabase task-repository transport failures seen after the
October 19 deployment — `TASK_FALLBACK_CLASSIFIED reason=local_resource
exception=ReadError`, `chain=ReadError<ReadError<BlockingIOError(errno=11)`,
repeated `Supabase event task query failed; using fallback`, and a separate HTTP/2
`ConnectionTerminated error_code:1` — **without** confusing them with the
already-fixed lifecycle-task diagnostic misclassification, and **without**
presenting a hypothesis as a confirmed cause.

---

## 2. What was read before any edit

`INVESTIGATION.md`, `IMPLEMENTATION_REPORT.md`, `backend/db/client.py`,
`backend/ai/database/task_repository.py` (classification, chain, cooldown,
`_run`/`_run_checked`, degradation sites), `backend/runtime/operation_watchdog.py`,
`backend/runtime/diagnostics.py`, `backend/ai/task_event_dispatcher.py`,
`backend/ai/database/manager.py`, `backend/requirements.txt`,
`tests/test_task_fallback_classification.py`,
`tests/test_task_fallback_cooldown.py`, `tests/test_local_resource_bounds.py`,
`tests/test_runtime_diagnostics_classification.py`, `tests/test_task_repository.py`
— plus the installed sources of the sync transport path:
`postgrest/_sync/client.py`, `postgrest/utils.py`,
`httpcore/_backends/sync.py`, `httpcore/_sync/http2.py` and
`httpcore/_sync/connection.py`.

---

## 3. Confirmed facts (source and measurement, not inference)

| Claim | Status | Evidence |
|---|---|---|
| One shared synchronous `httpx.Client` with **HTTP/2 enabled** | **Confirmed** | `postgrest 0.16.11::SyncPostgrestClient.create_session` → `SyncClient(..., http2=True)`; `get_db()` builds it once with `ClientOptions(postgrest_client_timeout=8.0)` |
| The only read primitive is `settimeout(timeout)` + `recv` | **Confirmed** | `httpcore/_backends/sync.py::SyncStream.read`; `map_exceptions({socket.timeout: ReadTimeout, OSError: ReadError})` |
| The read timeout is a constant 8 s per read, not a shrinking deadline | **Confirmed** | `httpcore/_sync/http2.py::_read_incoming_data` → `timeouts.get("read")`; `_DB_HTTP_TIMEOUT = 8.0` |
| A read **timeout** surfaces as `ReadTimeout`, never `ReadError(11)` | **Confirmed** | Reproduction: silent TLS peer + `settimeout(0.2)` → `TimeoutError`, `errno=None` |
| `OSError(EAGAIN)` *is* `BlockingIOError` — the production innermost link | **Confirmed** | `type(OSError(errno.EAGAIN, …)) is BlockingIOError`; plain non-blocking `recv` → `BlockingIOError(11)`; `SSLSocket` in non-blocking mode → `SSLWantReadError` (`errno=2`) |
| CPython runs a *timed* socket over a non-blocking fd | **Confirmed** | `fcntl(F_GETFL)` shows `O_NONBLOCK` set with `settimeout(0.6)` before and during `recv`; `SO_RCVTIMEO` stays `(0,0)` |
| A closed/GOAWAY/remote-terminated pooled connection cannot produce `EAGAIN` | **Confirmed** | `b""` → `RemoteProtocolError("Server disconnected")`; GOAWAY → `RemoteProtocolError(ConnectionTerminated)`; reset → `ECONNRESET(104)`; `is_available()` is false once `_connection_error` is set |
| No retry exists in the path | **Confirmed** | no `retry`/`max_retries` in `postgrest` or `httpx`; `run_sync_db` dispatches once |
| The 4-worker pool is bounded and the two deadlines are ordered | **Confirmed, unchanged** | `_DB_MAX_WORKERS = 4`, `0 < 8.0 < 10.0` (test-pinned) |
| Concurrent SSL read+write on the same socket produces `EAGAIN` | **Disproved here** | 200 trials (one thread blocked in `recv`, one `send`ing): 198 `TimeoutError`, zero `BlockingIOError` |

**Therefore:** the production failure is a local socket-layer **would-block on the
read path** of the shared HTTP/2 transport — not a Supabase outage, not an expired
read deadline, not executor saturation, not fd exhaustion, and not the HTTP/2
termination. The OS-level reason Render's socket layer returns `EAGAIN` remains
**unproven**; a plausible mechanism (a timed read running over a non-blocking fd,
where OpenSSL reports an `EAGAIN` as `SSL_ERROR_SYSCALL` and CPython raises
`BlockingIOError`) is recorded as a hypothesis only.

---

## 4. Implemented change (diagnostic only)

### 4.1 `backend/db/client.py`

`run_sync_db` now dispatches through a tiny wrapper that measures the **in-flight
duration inside the worker** and attaches it to the failure that comes out:

```python
_DB_INFLIGHT_ATTR = "_lifeos_db_inflight_seconds"

def _timed_db_call(fn, args, kwargs):
    started = time.monotonic()
    try:
        return fn(*args, **kwargs)
    except BaseException as exc:
        try:
            setattr(exc, _DB_INFLIGHT_ATTR, time.monotonic() - started)
        except Exception:
            pass
        raise
```

* The **same exception object** propagates; nothing is wrapped, retried,
  delayed or swallowed.
* The measurement is taken inside the worker, so executor queueing is excluded —
  it is time the transport itself spent.
* `db_inflight_seconds(exc)` returns the value, or `None` when the failure came
  from the watchdog rather than the transport (a blocked thread has no duration
  yet; reporting one would be fabrication).
* `functools` became unused and was removed.

### 4.2 `backend/ai/database/task_repository.py`

The once-per-episode `TASK_FALLBACK_CLASSIFIED` line now carries three additional
fields, all names/enums/bounded numbers:

```
TASK_FALLBACK_CLASSIFIED reason=local_resource phase=read exception=ReadError \
  chain=ReadError<ReadError<BlockingIOError(errno=11) elapsed=0.0s since_ok=0.5s \
  message=[Errno 11] Resource temporarily unavailable
```

| Field | Implementation |
|---|---|
| `phase` | `_transport_phase(exc)` walks the same chain and maps **class names** to `read` / `write` / `connect` / `pool` / `protocol` / `deadline`, else `unknown` |
| `elapsed` | `_format_evidence_seconds(db_inflight_seconds(exc))` → `"8.0s"`, `"0.0s"`, capped at one day, or `"n/a"` |
| `since_ok` | `self._last_ok_monotonic`, set by `_mark_supabase_ok` (the existing success path), formatted the same way, or `"never"` |

Why this is the right amount of change: `chain=` proved *which* local condition
failed, and only the **timing** now separates the two remaining explanations —
a transport deadline that expired (`elapsed ≈ 8 s`) from a socket that refused
the read immediately (`elapsed ≈ 0 s`). `phase` separates a read from a
connect/write and a peer termination from a deadline; `since_ok` records whether
the durable path was healthy moments earlier.

**Deliberately NOT done**

* No retry of any kind (writes are ambiguous after a failed response; the
  repository's inserts rely on CAS/unique keys and are not idempotent).
* No suppression, relabelling or downgrade of the warning; `EAGAIN` is still
  `reason=local_resource`, not an outage.
* No HTTP/2 disablement, no timeout change, no worker-count change, no new
  client/executor/scheduler/update loop.
* No change to the 5 s cooldown, its skip semantics, or the 8 s < 10 s ordering.
* No schema, SQL, migration or `DATABASE_ARCHITECTURE.md` change.
* No message text, URL, header, token or user data in the new fields.

---

## 5. Tests

New file `tests/test_task_transport_evidence.py` (12 tests):

| Test | Pins |
|---|---|
| `test_inflight_duration_is_measured_in_the_worker_and_the_call_is_issued_once` | duration is real, measured in the worker, the same exception object travels, exactly one dispatch |
| `test_a_successful_dispatch_carries_no_failure_evidence` | a healthy call returns normally |
| `test_the_dispatch_deadline_reports_no_transport_duration` | an `OperationTimeoutError` is `n/a` (never a fabricated socket duration); the blocked thread is released |
| `test_phase_matrix_covers_every_pinned_transport_shape` | `ReadError`/`ReadTimeout` → read, connect, write, pool, `RemoteProtocolError`/`ConnectionTerminated` → protocol, `OperationTimeoutError` → deadline, else unknown |
| `test_seconds_formatting_is_bounded_and_honest` | bounded formatting; `None`, negative and non-numeric → `n/a` |
| `test_an_immediate_would_block_and_a_deadline_are_distinguishable` | identical errno-11 chains, different `elapsed` (`<0.1 s` vs `≥0.25 s`) |
| `test_the_line_reports_the_gap_since_the_last_durable_success` | `since_ok` is a real gap after a success |
| `test_a_repository_that_never_succeeded_says_so` | `since_ok=never` |
| `test_remote_h2_termination_stays_unavailable_and_skips_the_local_line` | `ConnectionTerminated` → `unavailable`, cooldown not armed, no local episode line, failure still reported |
| `test_an_ambiguous_failed_insert_is_issued_exactly_once` | exactly one `insert` is issued for a failed write; the record stays non-durable |
| `test_a_cooldown_skipped_call_adds_no_second_evidence_line` | one line per episode; the cooldown is never extended by a skipped call |
| `test_evidence_fields_are_bounded_and_secret_free` | the evidence fields are enums/numbers only, a URL/token in the exception never reaches the line, and the line stays under 400 characters |

### Results

| Command | Result |
|---|---|
| `pytest tests/test_task_transport_evidence.py -q` | **12 passed** |
| `pytest … test_task_fallback_classification.py test_task_fallback_cooldown.py test_local_resource_bounds.py test_task_repository.py -q` | **64 passed** |
| `pytest tests/test_07_diagnostics.py tests/test_11_runtime_wiring.py tests/test_task_scheduler.py tests/test_task_execution.py tests/test_03_database_consistency.py tests/test_06_failure_simulation.py tests/test_22_reliability.py tests/test_24_execution_reliability.py tests/test_30_delete_timeout_hardening.py -q` | **91 passed** |
| `pytest tests -q -p no:cacheprovider` (full suite) | **6394 passed, 27 skipped, 40 failed** in 123.97 s |
| failure set of the full run | **all 40 in `tests/test_colored_buttons.py`** — `Button.inline() got an unexpected keyword argument 'style'` (workspace Telethon **1.34.0** vs pinned **1.43.0**). Pre-existing and unrelated; not fixed here |
| `python3 -m py_compile backend/db/client.py backend/ai/database/task_repository.py tests/test_task_transport_evidence.py` | exit 0 |
| `git diff --check` | clean |

Baseline `6382 passed` + 12 new = `6394`. **No newly introduced failures.**

---

## 6. Files changed

| File | Why |
|---|---|
| `backend/db/client.py` | Measure the in-flight duration of one synchronous transport call inside the bounded worker and expose it on the failure (diagnostic attribute only) |
| `backend/ai/database/task_repository.py` | Add `phase`, `elapsed` and `since_ok` to the existing once-per-episode diagnostic; track the last durable success |
| `tests/test_task_transport_evidence.py` | New regression coverage: in-flight evidence, phase matrix, HTTP/2 termination, no write retry, no duplicate lines, secret-free bounded fields |
| `IMPLEMENTATION_REPORT.md` | This report (full replacement) |
| `INVESTIGATION.md` | Full-replacement handoff for the next session/episode |

Not changed: no schema, no migration, no SQL, no `DATABASE_ARCHITECTURE.md`, no
`AGENTS.md`, no second client/executor/scheduler/retry loop, no worker count or
timeout, no Render or environment change, no user-facing behaviour,
`backend/requirements.txt` untouched.

---

## 7. Production verification — NOT performed

No deployment was triggered, no Render shell/log stream was used, no service was
restarted, and no live Telegram/Supabase session exists in this workspace. A
commit and push is **not** proof of deployment or recovery.

Specifically, the following are **unverified**:

* whether Render has deployed the commit carrying this change;
* whether `BlockingIOError(errno=11)` recurs, and at what rate;
* whether `ConnectionTerminated` recurs, and whether it coincides in time with
  the errno-11 rows;
* whether the durable task operations succeed more often (the `since_ok` field
  will make the fallback rate readable);
* that the lifecycle-task false-stall warnings stay absent while genuine bounded
  stalls are still detected.

**What the next episode must show** (also in `INVESTIGATION.md` §5): `elapsed ≈
8 s` ⇒ the transport deadline is being surfaced as a would-block (latency
problem); `elapsed ≈ 0 s` ⇒ the socket refused the read immediately (socket/fd
state problem); `phase=protocol` ⇒ a peer termination; `phase=deadline` ⇒ watchdog
abandonment. Only after that is known should any behavioural change be designed —
and a **read-only** bounded re-dispatch is the only retry shape worth
considering, because a failed write is ambiguous.

---

## 8. Remaining uncertainty and manual action

* The OS-level cause of `EAGAIN` on Render is **unproven**; this phase makes it
  decidable without guessing.
* `httpcore` and `h2` are **unpinned** in `backend/requirements.txt`; the
  conclusions here rest on `httpcore 1.0.9` / `h2 4.4.1`. A dependency pin is a
  deliberate, separate change (it alters the deployed stack) and is recommended
  as a follow-up so a later rebuild cannot silently change transport semantics.
* **Manual actions:** (1) confirm the Render deploy of this commit and capture the
  next `TASK_FALLBACK_CLASSIFIED` line; (2) collect host counters for the same
  minute (open fds, `CLOSE_WAIT`/`TIME_WAIT` sockets, `lifeos-supabase-*`
  threads) — Render Free has no shell, so use an existing metric/log surface;
  (3) decide on any follow-up fix only from that evidence.
* The 40 button-colour failures remain (workspace Telethon version) and are
  unrelated to this phase.
