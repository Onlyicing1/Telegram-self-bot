# INVESTIGATION.md — recurring Supabase transport `ReadError`/EAGAIN, round 2

This file is the **complete current investigation and handoff** and replaces every
previous investigation in full. Earlier handoffs (the transport knob audit at
`6f93228`, the AED archive rounds, the private-message rounds, the lifecycle-task
stall round, the coloured-button round) remain readable in git history and are
obsolete here.

| Item | Value |
|---|---|
| Investigation | The recurring production `ReadError(BlockingIOError(errno=11))` on the shared Supabase transport, and its (unproven) relationship to the separate HTTP/2 `ConnectionTerminated error_code:1` line |
| Date | 2026-10-19 (11:01 UTC) |
| Project / remote | LifeOS Telegram self-bot, `Onlyicing1/Telegram-self-bot` (`origin`) |
| Branch | `main` |
| Revision at task start | `6f93228b604737b3dda985627f9ef33418b7c573` (`origin/main`); the workspace was 2 commits behind (`89a3007`) and was fast-forwarded to it |
| Evidence basis | Full reads of this repository **and the installed pinned transport libraries**, 11 local socket/TLS measurements, the repository test suite, and one source-backed literature check |
| Live Telegram / Render / Supabase | **NOT accessed.** No deployment, Render shell, log stream, `.env`, credential, SQL, migration, restart or environment change. No claim of runtime recovery is made |
| Outcome | Root cause still **not proven on the deployed build**. Two narrow, safe **diagnostic** additions implemented so the next episode is decidable. No behaviour, retry, timeout, worker-count, lifecycle or transport change |

---

## 1. The production evidence, and exactly what it proves

The line the owner captured after the `6f93228` deployment:

```text
TASK_FALLBACK_CLASSIFIED reason=local_resource phase=read exception=ReadError
  chain=ReadError<ReadError<BlockingIOError(errno=11) elapsed=0.0s since_ok=0.0s
ConnectionTerminated error_code:1                 (separate line, HTTP/2 transport)
```

| Claim | Status | Basis |
|---|---|---|
| The failure is a local socket-layer would-block on the **read** path, not a PostgREST/HTTP status | **Confirmed** | `OSError(EAGAIN)` *is* `BlockingIOError`; `httpcore.ReadError` is raised only by the `map_exceptions({socket.timeout: ReadTimeout, OSError: ReadError})` wrapper around `SyncStream.read` — `self._sock.settimeout(timeout); return self._sock.recv(max_bytes)` (`httpcore/_backends/sync.py`) |
| It is **not** the 8 s transport deadline | **Confirmed** | `elapsed=0.0s` — measured inside the worker. A real deadline surfaces as `httpx.ReadTimeout` with **no errno** (re-measured locally: silent TLS peer + `settimeout(0.2)` → `TimeoutError`, `errno=None`) |
| It is **not** executor saturation or a queued dispatch | **Confirmed (absent)** | That path raises `OperationTimeoutError` → `phase=deadline`, `elapsed=n/a` |
| It is **not** fd exhaustion | **Confirmed (absent)** | `EMFILE`/`ENFILE` (24/23) are also `local_resource`, but the chain names errno **11** |
| It is **not** a Supabase/store outage | **Supported** | `since_ok=0.0s` — a durable operation succeeded within the same ~50 ms; a store outage would not be preceded by a success |
| It is **not** peer-close, RST, or GOAWAY reaching the read | **Confirmed (absently)** — measured here | Peer FIN → clean `b''`; peer RST (SO_LINGER 0) → clean `b''`; GOAWAY → `RemoteProtocolError` (`phase=protocol`, **no errno**). None produces `OSError(11)` |
| The two signatures (`ConnectionTerminated`, errno-11 read) are causally related | **Unproven — and now measured by the code rather than by eye** | Only temporal proximity was ever available; §4 makes it a field on the same log line |

---

## 2. New local measurements (the decisive negatives)

Executed in this workspace on **CPython 3.10.12 / OpenSSL 3.0.2** against a
locally generated self-signed TLS pair (`scratchpad/eagain_probe.py`,
`scratchpad/eagain_probe2.py` — untracked diagnostic scripts, not repository
artefacts):

| Condition at the moment of a timed TLS read | Result | Elapsed |
|---|---|---|
| Peer silent (baseline deadline path), `settimeout(8.0)` | `TimeoutError(errno=None)` | 8.008 s |
| Peer sends FIN while blocked in read | returns `b''` (clean EOF) | 0.301 s |
| Peer sends RST while blocked in read | returns `b''` | 0.201 s |
| **Our own `SSLSocket.close()` from another thread while blocked** | returns `b''` — the blocked read is **not** interrupted | 8.009 s |
| `os.close(fd)` from another thread while blocked | returns `b''` | 8.008 s |
| fd closed **and reused** by a fresh non-blocking socket, then read | `TimeoutError` — not EAGAIN | 8.501 s |
| `settimeout(1e-9)` / `settimeout(0.001)` + read | `TimeoutError(errno=None)` | ≈0 s |
| **`settimeout(0)` (non-blocking) `SSLSocket` read** | `SSLWantReadError(errno=2)` — **not** `BlockingIOError` | 0 s |
| `unwrap()` then read (raw fallback on a non-blocking fd) | `SSLError(errno=1)` | 0.05 s |
| Two concurrent readers sharing one `SSLSocket` (400 iterations) | `SSLWantReadError(errno=2)` only; **0** `BlockingIOError` | — |
| Concurrent reader + writer, 200 trials (previous round) | `TimeoutError` only | — |

Consequences, stated as negatives because they are what the measurements support:

1. **The teardown family cannot produce the observed signature directly.** A read
   that is in flight when the socket is closed by another thread (httpcore's
   `HTTP2Connection.close()` is source-documented as taking **no lock**, and
   `_response_closed()` can call it while a sibling stream is blocked) survives and
   returns either a clean EOF or the full timeout. Neither is `BlockingIOError(11)`.
2. **The fd-reuse family is out.** A reused fd makes the read wait, not fail.
3. **A plain non-blocking (timeout-0) socket is out** for a TLS read: on this
   interpreter an `SSLSocket` surfaces that condition as `SSLWantReadError`
   (`errno=2`), and `httpcore`'s `map_exceptions` would still map it to `ReadError`
   — but the chain would read `errno=2`, not `errno=11`.
4. What **is** left, inside the pinned stack, as the producer of
   `OSError(errno=11)` out of a read: the **SSL layer surfacing an `EAGAIN` as a
   fatal syscall error** (`SSL_ERROR_SYSCALL`) instead of a retryable
   `SSL_ERROR_WANT_READ`, which CPython's `_ssl.c` converts into
   `OSError(EAGAIN)` — i.e. `BlockingIOError` — the exact innermost link observed.
   OpenSSL's own documentation classifies `SSL_ERROR_SYSCALL` as fatal, and a
   public OpenSSL users-list report describes exactly this misclassification
   (`SSL_read()` returning `SSL_ERROR_SYSCALL` with errno 11) on a non-blocking
   socket. CPython's 3.11 socket code even documents the *readiness false
   positive* that can precede it (readiness was reported, the data was no longer
   there).

**Evaluation gap found and now closed in the logs:** the repository's previous
round measured everything on **CPython 3.10.12**, while `render.yaml` pins
`PYTHON_VERSION: 3.11.7` for the deployed service. The deployed OpenSSL build was
never recorded. Both facts are load-bearing for the hypothesis above, so the
runtime identity is now logged once when the Supabase client is created (§4).

---

## 3. Hypotheses and confidence

| # | Hypothesis | Confidence | Why |
|---|---|---|---|
| H1 | **SSL/OpenSSL read classification**: an `EAGAIN` on the shared TLS socket is reported as a fatal `SSL_ERROR_SYSCALL` rather than retryable `WANT_READ`; CPython raises `OSError(EAGAIN)`, httpcore maps it to `ReadError`. The connection is then unusable (`_connection_error`), so the *next* request opens a fresh one — consistent with `since_ok=0.0s` and with isolated episodes. | **Leading, medium** | Source-backed (OpenSSL docs + OpenSSL users-list report + CPython `_ssl.c`/`socketmodule.c`), and the only within-stack producer of errno 11 that survives §2. Not reproduced locally on 3.10/OpenSSL 3.0.2 → unproven for the deployed build |
| H2 | **Connection-teardown coupling**: the read fails because the HTTP/2 connection was torn down concurrently (unlocked `close()`), and the errno-11 rows cluster after `ConnectionTerminated`. | **Unproven; contradicted as a *direct* mechanism by §2** | The unlocked close is real and source-verified, which is why the correlation is now measured instead of assumed. §2 shows a close does not itself produce errno 11 |
| H3 | **Platform/network-stack behaviour on Render** (sandboxed netstack, proxy or edge path returning EAGAIN where plain Linux would block). | **Unverifiable from this workspace** | No credible public source found; no Render access here |
| — | Expired deadline, executor saturation, fd exhaustion, store outage, peer close/RST/GOAWAY, fd reuse | **Disproved or absent** | §1 and §2 |

`ConnectionTerminated` is **not** conflated with EAGAIN here: it is a separate,
differently-shaped failure, and the relationship between the two is explicitly
left open pending the field added in §4.

---

## 4. The two changes made (both diagnostic, neither behavioural)

### 4.1 `backend/db/client.py`

* `_note_transport_teardown(exc)` records the monotonic instant of the most recent
  **peer/HTTP-2 termination** seen in a failure chain (class names
  `ConnectionTerminated` / `RemoteProtocolError`), at the **single** point every
  synchronous Supabase call already passes (`_timed_db_call`, inside the bounded
  worker). The chain walk is capped (8 links), never raises, and captures no
  payload, URL, header or token. `seconds_since_transport_teardown()` exposes the
  gap, or `None` when no teardown has been seen in this process.
* The existing client-init log line now carries the deployed runtime identity:
  `[SAVE_DB] Supabase client initialised. runtime=python-X.Y.Z ssl=<OpenSSL>`.

### 4.2 `backend/ai/database/task_repository.py`

The once-per-episode line gains one field, so the two production signatures no
longer have to be correlated by hand from timestamps:

```text
TASK_FALLBACK_CLASSIFIED reason=local_resource phase=read exception=ReadError \
  chain=ReadError<ReadError<BlockingIOError(errno=11) elapsed=0.0s since_ok=0.0s \
  since_teardown=none message=[Errno 11] Resource temporarily unavailable
```

`since_teardown` is a bounded seconds value or `none` — the same formatting as the
existing `since_ok`, so it stays an enum/number field.

**Deliberately NOT done**

* No retry of any kind. A failed write stays ambiguous (its response never
  arrived), which is precisely why no automatic re-issue is added.
* No change to the 5 s local-resource cooldown, its skip semantics, or the
  8 s < 10 s ordering; no suppression or relabelling of the warning; `EAGAIN`
  is still `reason=local_resource`.
* No HTTP/2 disablement, no timeout change, no worker-count change, no new
  client/executor/scheduler/update loop, no client-lifecycle change.
* No schema, SQL, migration or `DATABASE_ARCHITECTURE.md` change; no Render or
  environment change; `backend/requirements.txt` untouched.
* No message text, URL, header, token or user data in any new field.

### Why no behavioural fix in this round

The brief's gate is evidence, and the mechanism is **not proven on the deployed
build**: the strongest candidate (H1) rests on library/platform behaviour that
this workspace cannot reproduce, and §2 actively removes the local explanations
that would have justified a transport-side change. Every candidate fix would be
hypothesis-driven — and the one shape that would not be (re-issuing an ambiguous
write) is explicitly forbidden. So the code keeps its semantics and gains the two
fields that decide the next step.

---

## 5. What the next production episode must show

Read the `TASK_FALLBACK_CLASSIFIED` line and decide mechanically:

| Observed | Conclusion | Next action |
|---|---|---|
| `since_teardown=none` | The errno-11 read is **not** coupled to a peer/HTTP-2 termination. H1 (SSL classification) or H3 (platform) remains; H2 is disproven. | Take the runtime identity from the init line (Python/OpenSSL of the deployed build) and, if the platform family is still live, collect one host-level sample around the episode minute (open fds, sockets in `CLOSE_WAIT`/`TIME_WAIT`, `lifeos-supabase-*` thread count) |
| `since_teardown=0.x s`…`few s` | The two signatures are temporally coupled. | Then, and only then, design the smallest change that removes the coupling (see §6) — never a blind retry |
| `phase=read elapsed≈8s` | The deadline is being surfaced as a would-block. | Host/edge latency investigation, not the client |
| `phase=deadline` / `elapsed=n/a` | Watchdog abandonment (thread still blocked). | Revisit the dispatch budget with measurements |
| `phase=write` | Write phase — ambiguous completion. | Do **not** retry; keep the non-durable label |

Simultaneously confirm that the lifecycle-task false-stall warnings stay absent
while a genuine bounded stall is still reported.

---

## 6. Bounded design options for the next round (not implemented)

1. **If `since_teardown` shows coupling:** reduce the coupling rather than retry —
   e.g. ensure a connection that has seen a termination is never reused for an
   in-flight request's read (httpcore closes it, but the *sibling* read path is
   what fails), which is a transport-shape decision (HTTP/1.1 per-request
   connections, or a smaller connection lifetime) that must be justified by that
   correlation first.
2. **If the cause stays in the socket/SSL layer:** the only retry shape worth
   considering is a **single bounded re-dispatch of a provably side-effect-free
   read** (never an insert/update/delete, whose completion stays ambiguous). It
   would have to fit inside the existing 10 s dispatch budget and stay inside the
   existing single executor.
3. **Either way:** the observed amplification should be re-examined — a single
   sub-millisecond blip guarantees the full 5 s cooldown, because every call made
   during the window is skipped *before* touching the transport, so nothing can
   produce the success that clears it early. This is documented here as a
   consequence, **not** changed: no production evidence yet shows it harming a
   task.

---

## 7. Tests and exact results (this round)

| Command | Result |
|---|---|
| `pytest tests/test_task_transport_evidence.py -q` | **16 passed** (12 existing + 4 new) |
| `pytest tests/test_task_transport_evidence.py tests/test_task_fallback_classification.py tests/test_task_fallback_cooldown.py tests/test_local_resource_bounds.py tests/test_task_repository.py -q` | **68 passed** |
| `pytest tests/test_07_diagnostics.py tests/test_11_runtime_wiring.py tests/test_task_scheduler.py tests/test_task_execution.py tests/test_03_database_consistency.py tests/test_06_failure_simulation.py tests/test_22_reliability.py tests/test_24_execution_reliability.py tests/test_30_delete_timeout_hardening.py -q` | **91 passed** |
| `pytest tests -q -p no:cacheprovider` (full suite) | **6438 passed, 27 skipped, 0 failed** in 124.05 s |
| historical failure set | **empty.** The 40 `tests/test_colored_buttons.py` failures of the previous round were the workspace's Telethon **1.34.0** versus the pinned **1.43.0**; the workspace now has **1.43.0** (verified: `pip list`), and all 40 pass. Accounting: 6394 (previous) + 40 (repaired by the environment now matching the pin) + 4 (new) = 6438 |
| `python -m py_compile backend/db/client.py backend/ai/database/task_repository.py tests/test_task_transport_evidence.py` | exit 0 |
| `git diff --check` | clean |

New tests: the teardown recorder records only peer/HTTP-2 terminations (never a
socket-layer read failure or a timeout); the recorder is bounded, cycle-safe and
never raises; the episode line reports `none` without a teardown and a real gap
after one; the init line carries the runtime identity with no URL/key. The
existing pins (single dispatch, no retry, one line per episode, `unavailable`
for a remote termination, secret-free bounded fields under 400 characters) are
kept and remain green.

---

## 8. Constraints preserved

* One shared Supabase client, one bounded 4-worker pool, one dispatch point
  (`run_sync_db`). No second client, executor, scheduler, update loop or retry
  system.
* Transport deadline (8 s) strictly below the dispatch deadline (10 s).
* Bounded local-resource cooldown and truthful non-durable fallback labelling
  unchanged; a cooldown-skipped call still adds no second line.
* No HTTP/2, timeout, worker-count or lifecycle change; no schema, SQL, migration
  or `DATABASE_ARCHITECTURE.md` change; no dependency or Render environment change.
* Diagnostics carry names, errno integers, enums and bounded numbers only.

---

## 9. Unresolved / unverified

* **The OS/SSL-level origin of `EAGAIN` on Render is unproven.** §2 rules out the
  local explanations; H1/H3 remain.
* **No production verification was performed from this workspace.** This
  environment has **no Render access at all** (no Render CLI, no Render
  credential in the environment, and no Render commit statuses on GitHub for
  `6f93228`/`c9e5203`/`17942f1`), so the deployment status, the post-deploy logs
  and any host counters must come from the owner's Render dashboard. A commit
  and push is not proof of deployment or recovery.
* **`ConnectionTerminated` correlation is now measured, not yet observed** — the
  field ships with this change and will speak only on the next episode.
* **Dependency drift:** `httpcore` and `h2` are still unpinned in
  `backend/requirements.txt`; the conclusions above rest on `httpcore 1.0.9` /
  `h2 4.4.1` / `httpx 0.27.0`. A deliberate pin remains a separate change.
* **`RuntimeSupervisor._cancel_orphan_tasks`** can still cancel uvicorn's
  lifespan task on a hard reset (pre-existing, outside this round).

---

## 10. Delivery state

Changed files: `backend/db/client.py`,
`backend/ai/database/task_repository.py`,
`tests/test_task_transport_evidence.py`, and these two documents (replaced in
full). The delivery commit is the one that carries this file on `main`; identify
it with `git log -1` on the checked-out branch, because a file cannot contain the
SHA of its own commit.
