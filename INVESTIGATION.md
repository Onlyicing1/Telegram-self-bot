# INVESTIGATION.md — recurring Supabase task-repository transport failures

This file is the **complete current investigation and handoff** and replaces every
previous investigation in full. Earlier handoffs (colored-button semantics, the
symbol registry, premium-emoji rounds, AED archive rounds, private-message
rounds, the lifecycle-task stall round) remain readable in git history and are
obsolete here.

| Item | Value |
|---|---|
| Investigation | Supabase task-repository `ReadError [Errno 11]` bursts and the separate HTTP/2 `ConnectionTerminated` failure |
| Date | 2026-10-19 |
| Project / remote | LifeOS Telegram self-bot, `Onlyicing1/Telegram-self-bot` (`origin`) |
| Base revision | `aee3e163e914eae72cafa922eb3d38fd7a3ca697` (`origin/main`; this documentation commit) |
| Relevant code before this phase | `17942f167591618c4ba9111d15f2f7cffc7d789d` |
| Evidence basis | Full source reads of this repository **and the installed pinned libraries**, plus local socket/TLS reproductions and the repository test suite |
| Live Telegram / Render / Supabase | **NOT accessed.** No deployment, Render shell, log stream, `.env`, credential, SQL, migration, restart or environment change. No claim of runtime recovery is made |
| Outcome | Root cause **not** proven at the OS level. One narrow, safe **diagnostic** change implemented so the next production episode is decidable. No behaviour, retry, timeout, worker-count or transport change |

---

## 1. What the production evidence now proves

The chain field added in `17942f1` did its job. Production now reports:

```
TASK_FALLBACK_CLASSIFIED reason=local_resource exception=ReadError
  chain=ReadError<ReadError<BlockingIOError(errno=11)
Supabase event task query failed; using fallback
ConnectionTerminated error_code:1            (separate line, HTTP/2 transport)
```

That chain is **shape evidence**, and it settles several hypotheses outright:

| Claim | Status | Basis |
|---|---|---|
| The failure is a local socket-layer would-block, not a PostgREST/HTTP status | **Confirmed** | `OSError(errno.EAGAIN)` *is* `BlockingIOError`; the innermost link is that raw OS error, so nothing above it produced a response |
| It is **not** an expired HTTP read deadline | **Confirmed (disproved as the explanation)** | `httpcore._backends.sync.SyncStream.read` maps `socket.timeout → ReadTimeout`; a real deadline expiry surfaces as `httpx.ReadTimeout` with **no errno**. Reproduced locally: a TLS peer that never writes + `settimeout(0.2)` → `TimeoutError`, `errno=None` |
| It is **not** executor saturation or a queued dispatch | **Confirmed (absent)** | That path raises `OperationTimeoutError` with no errno → `reason=unavailable`; production says `local_resource` with errno 11 |
| It is **not** file-descriptor exhaustion | **Confirmed (absent)** | `EMFILE`/`ENFILE` (24/23) also classify `local_resource`, but the chain names errno **11** |
| It is **not** the HTTP/2 termination | **Confirmed (separate)** | `ConnectionTerminated` is an `h2` event; httpcore raises `RemoteProtocolError(event)` — **no errno**, `phase=protocol`, `reason=unavailable`. It is a different line from the errno-11 chain, and whether the two coincide in time is still unmeasured |
| Classification, cooldown and fallback attribution behave as designed | **Confirmed** | 5 s bounded local-resource cooldown, skipped calls never extend it, a success clears it, in-memory results are labelled non-durable with `reason=local_resource` |

What is **still unproven** is *why* the local socket layer returns `EAGAIN` on
Render. That is an OS/network-stack question, and it is recorded as unproven
rather than guessed at.

---

## 2. Pinned-stack facts (source-verified, not assumed)

Installed and inspected in this workspace: `supabase 2.4.2`, `postgrest 0.16.11`,
`httpx 0.27.0`, `httpcore 1.0.9`, `h2 4.4.1` (`backend/requirements.txt` pins
`supabase==2.4.2` and `httpx==0.27.0`; `httpcore`/`h2` arrive transitively).

1. **One synchronous client, HTTP/2 on.**
   `postgrest/_sync/client.py::SyncPostgrestClient.create_session` builds
   `SyncClient(..., timeout=timeout, http2=True)`, i.e. a bare `httpx.Client`
   subclass. `backend/db/client.py::get_db` passes
   `ClientOptions(postgrest_client_timeout=8.0)`.
2. **The read primitive.**
   `httpcore/_backends/sync.py::SyncStream.read` is exactly
   `self._sock.settimeout(timeout); return self._sock.recv(max_bytes)` inside
   `map_exceptions({socket.timeout: ReadTimeout, OSError: ReadError})`.
   So *only* a real `OSError` can produce `ReadError`, and its errno is preserved
   as the innermost link of the chain we log.
3. **Where the errno comes from.** `httpcore/_sync/http2.py::_read_incoming_data`
   calls `network_stream.read(READ_NUM_BYTES, timeouts["read"])` — a constant
   per-read timeout (8.0 s here), not a shrinking deadline. A would-block at the
   syscall level therefore has exactly two possible timings: immediate
   (`elapsed ≈ 0`) or after the timeout (`elapsed ≈ 8`).
4. **CPython implements a positive socket timeout over a NON-BLOCKING fd.**
   Measured: with `settimeout(0.6)` the fd has `O_NONBLOCK` set (`fcntl
   F_GETFL`) both before and during `recv`; `SO_RCVTIMEO` stays `(0,0)`.
   Consequently every Supabase read runs over a non-blocking TLS fd, and an
   `SSL_read` EAGAIN that OpenSSL reports as `SSL_ERROR_SYSCALL` (rather than
   `SSL_ERROR_WANT_READ`) is translated by CPython into `OSError(11)` →
   **`BlockingIOError`**, which is precisely the observed innermost link.
   A plain non-blocking `socket.recv` gives the same class
   (`BlockingIOError`, errno 11); an `SSLSocket` in pure non-blocking mode gives
   `SSLWantReadError` with **errno 2**. The production chain is the former.
5. **Connection reuse cannot manufacture `EAGAIN`.**
   A pooled connection that the peer closed cleanly yields
   `RemoteProtocolError("Server disconnected")` (read returned `b""`); a GOAWAY
   yields `RemoteProtocolError(ConnectionTerminated)`; a reset yields
   `ECONNRESET` (104). None of these is `EAGAIN`. Also, `HTTP2Connection
   .is_available()` is `False` once `_connection_error` is set, so a poisoned
   connection is not handed out again.
6. **No retry exists anywhere in this path.** Neither `postgrest` nor `httpx`
   contains retry logic (grep: no `retry`/`max_retries`), and `run_sync_db`
   dispatches once. A failed write therefore stays ambiguous — which is why no
   retry is added here.

### Local reproductions used above

| Experiment | Result |
|---|---|
| TLS peer accepts, never writes; client `settimeout(0.2)`, `recv` | `TimeoutError`, `errno=None` (→ `ReadTimeout`) |
| TLS peer accepts, silent; client `settimeout(0)`, `recv` | `SSLWantReadError`, `errno=2` |
| Plain `socketpair`, peer silent, `setblocking(False)`, `recv` | `BlockingIOError`, `errno=11` |
| TLS fd flags with `settimeout(0.6)` before and during `recv` | `O_NONBLOCK` set in both, `SO_RCVTIMEO=(0,0)` |
| 200 trials: one thread blocked in `SSLSocket.recv`, one thread `send`ing on the same socket | 198 `TimeoutError`, 0 `BlockingIOError` |

---

## 3. Concurrency question (is the shared client / bounded pool invalid?)

Investigated because the same synchronous client is dispatched from a bounded
4-worker pool:

* `httpx.Client` is documented as shareable across threads; `httpcore` guards a
  connection with a separate read lock and write lock, and reads/writes from
  different threads are the intended HTTP/2 pattern (multiplexed streams).
* Both `SyncStream.read` and `SyncStream.write` call `sock.settimeout(...)` on
  the same fd, but with the **same** value here (read = write = 8.0 from
  `ClientOptions`), so no thread can flip the other's mode.
* The 200-trial concurrency reproduction above produced `TimeoutError` only —
  never `BlockingIOError`, never a corrupted-SSL error.
* The pool is bounded: `_DB_MAX_WORKERS = 4`, `_DB_HTTP_TIMEOUT = 8.0 <
  _DB_TIMEOUT = 10.0` (test-pinned), so at most four sockets exist on the shared
  client and no thread outlives its transport deadline.

**Conclusion:** nothing in the current design is proven invalid, and no
evidence justifies a second client, a bigger/smaller pool, a timeout change or
serialisation. The brief's constraint to preserve this architecture is
consistent with the evidence.

---

## 4. The one change made (diagnostic, not behavioural)

`chain=` answered *what* failed. It cannot answer *when in the call* it failed,
and that is the last thing separating the two remaining explanations of
`BlockingIOError(errno=11)`:

* the 8 s read transport deadline expired and the platform surfaced it as a
  syscall would-block → the call was in flight for ~8 s;
* the TLS/socket layer refused the read immediately (non-blocking fd / would-
  block or an `SSL_ERROR_SYSCALL` EAGAIN) → the call failed in milliseconds.

So the once-per-episode `TASK_FALLBACK_CLASSIFIED` line now also carries:

| Field | Meaning | Why it decides |
|---|---|---|
| `elapsed` | in-flight seconds of the failing transport call, measured **inside** the shared worker (executor queueing excluded); `n/a` when the failure came from the watchdog, not the transport | `≈8.0s` ⇒ deadline; `≈0.0s` ⇒ immediate refusal |
| `phase` | transport stage derived from class names only: `read`, `write`, `connect`, `pool`, `protocol`, `deadline`, `unknown` | separates a read from a connect/write, and a peer termination from a deadline |
| `since_ok` | seconds since the last successful durable operation in this process (`never` if none) | a failure 0.5 s after a success is a transient blip, not an unreachable store |

All three are numbers or lower-case enums. **Nothing** is retried, delayed,
re-classified, suppressed or relabelled; the cooldown, the dispatch budget, the
transport deadline, the worker count and the fallback semantics are untouched.
Implementation detail and exact code are in `IMPLEMENTATION_REPORT.md`.

---

## 5. What the next production episode must show

Read the `TASK_FALLBACK_CLASSIFIED` line and decide mechanically:

| Observed | Conclusion | Next action |
|---|---|---|
| `phase=read elapsed=7.5s…8.0s chain=…BlockingIOError(errno=11)` | the transport deadline is being reported as a syscall would-block — a latency/platform artefact on the read path | pursue host/edge latency (Render egress, Supabase edge), not the client |
| `phase=read elapsed=0.0s…0.1s chain=…BlockingIOError(errno=11)` | the socket refused the read immediately — non-blocking-fd / `SSL_ERROR_SYSCALL` path | capture host counters around the same minute (open fds, sockets in `CLOSE_WAIT`/`TIME_WAIT`, `lifeos-supabase-*` threads) and, if available, an `strace`-level sample |
| `phase=protocol` + `ConnectionTerminated` | peer/HTTP-2 GOAWAY — connection expiry, a different problem | correlate timestamps with the errno rows; consider connection lifetime, never a blind retry |
| `phase=deadline` / `elapsed=n/a` | watchdog abandonment (thread still blocked) — the client cannot keep up | revisit the dispatch budget with measurements |
| `phase=write` | the write phase, so response ambiguity is even stronger | do **not** retry; keep the non-durable label |
| `since_ok=` small (≪ the episode) | the durable path was healthy moments earlier | treat as transient, keep the cooldown |

Simultaneously confirm that the lifecycle-task false-stall warnings stay absent
(`Task-1`/`Task-27` no longer appear as `TASK_NO_PROGRESS`/`TASK_STARVATION`)
while a genuine bounded stall is still reported.

---

## 6. Tests and exact results (this phase)

| Command | Result |
|---|---|
| `pytest tests/test_task_transport_evidence.py -q` | **12 passed** (new file) |
| `pytest tests/test_task_transport_evidence.py tests/test_task_fallback_classification.py tests/test_task_fallback_cooldown.py tests/test_local_resource_bounds.py tests/test_task_repository.py -q` | **64 passed** |
| `pytest tests/test_07_diagnostics.py tests/test_11_runtime_wiring.py tests/test_task_scheduler.py tests/test_task_execution.py tests/test_03_database_consistency.py tests/test_06_failure_simulation.py tests/test_22_reliability.py tests/test_24_execution_reliability.py tests/test_30_delete_timeout_hardening.py -q` | **91 passed** |
| `pytest tests -q -p no:cacheprovider` (full suite) | **6394 passed, 27 skipped, 40 failed** in 123.97 s |
| failure set of the full run | **all 40 in `tests/test_colored_buttons.py`** — `Button.inline() got an unexpected keyword argument 'style'`, i.e. workspace Telethon **1.34.0** vs pinned **1.43.0**. Pre-existing, unrelated, not fixed here |
| `python3 -m py_compile backend/db/client.py backend/ai/database/task_repository.py tests/test_task_transport_evidence.py` | exit 0 |
| `git diff --check` | clean |

Accounting: baseline `6382 passed` + **12** new tests = `6394`. No new failures;
the 27 skips are the unchanged opt-in/live tests.

---

## 7. Constraints preserved

* One shared Supabase client, one bounded 4-worker pool, one dispatch point
  (`run_sync_db`). No second client, executor, scheduler, update loop or retry
  system.
* Transport deadline (8 s) strictly below the dispatch deadline (10 s) — the
  measurement is taken inside the worker and never changes either deadline.
* Bounded local-resource cooldown (5 s) and truthful in-memory fallback
  labelling unchanged; a cooldown-skipped call still raises
  `LocalResourceCooldown`/`reason=local_resource` and emits no second line.
* No retry of writes (ambiguous completion), no suppression of the warning, no
  relabelling of `EAGAIN` as an outage.
* No HTTP/2 disablement, no timeout/worker-count change.
* No schema, SQL, migration or `DATABASE_ARCHITECTURE.md` change.
* No credential, token, URL, header or message content in the diagnostics —
  names, errno integers, enums and bounded numbers only.

---

## 8. Unresolved / unverified

* **The OS-level origin of `EAGAIN` on Render is unproven.** The repository can
  prove it is a local socket-layer would-block on the read path (not a timeout,
  not saturation, not a store error, not the HTTP/2 termination) but not which
  local condition produces it.
* **`ConnectionTerminated` correlation with the errno-11 bursts is unproven**;
  only the separation of the two signatures is proven.
* **No production verification.** Nothing was deployed or observed. A
  successful commit is not proof of deployment or recovery.
* **Dependency drift:** `httpcore` and `h2` are not pinned in
  `backend/requirements.txt`; the versions examined here are `httpcore 1.0.9`
  and `h2 4.4.1`, so a later rebuild may resolve a different transport
  implementation and change the semantics these conclusions rest on. Not
  changed in this phase (it alters the deployed stack and is not needed to
  answer the question); recommended as a separate, deliberate dependency pin.
* **`RuntimeSupervisor._cancel_orphan_tasks` still cancels uvicorn's lifespan
  task** on a hard reset (auto-named, unprotected). Pre-existing, outside this
  phase.
* **The 40 button-colour failures** remain (workspace Telethon version) and are
  unrelated.

---

## 9. Manual action required

1. Confirm Render deployed the commit that carries this change and read the next
   `TASK_FALLBACK_CLASSIFIED` line — the table in §5 decides the remaining cause
   mechanically. This workspace cannot reach Render.
2. Collect host-level counters for the same minute as the episode (open fds,
   sockets in `CLOSE_WAIT`/`TIME_WAIT`, `lifeos-supabase-*` thread count). Render
   Free has no shell; this needs an existing metric surface or platform metric.
3. Decide, once §5 identifies the cause, whether a single bounded **read-only**
   re-dispatch is worth designing (writes must stay non-retried). Not designed
   here, because the cause is not established and a second attempt would need to
   fit inside the existing 10 s dispatch budget.

---

## 10. Delivery state

The code and test change is in `backend/db/client.py`,
`backend/ai/database/task_repository.py` and
`tests/test_task_transport_evidence.py`; this document and
`IMPLEMENTATION_REPORT.md` are replaced in full. The commit that delivers them is
identified with `git log -1` on the checked-out branch (`origin/main`), because a
file cannot contain the SHA of its own commit.
