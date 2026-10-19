# IMPLEMENTATION_REPORT.md — Supabase transport `ReadError`/EAGAIN, round 2 (diagnosis + one bounded diagnostic)

> This file describes ONLY the current state of the repository for this round and
> **replaces every previous report in full** — it is not an appended report.
> Earlier reports (the transport-evidence audit at `6f93228`, the lifecycle-task
> stall round at `17942f1`, the AED archive rounds, the emoji/private-message
> rounds) remain readable in git history and in `docs/investigations/`.

| Item | Value |
|---|---|
| Stage | Diagnosis + the smallest safe diagnostic that decides the remaining cause |
| Date | 2026-10-19 (11:01 UTC) |
| Project / remote | LifeOS Telegram self-bot, `Onlyicing1/Telegram-self-bot` (`origin`), branch `main` |
| Revision at task start | `6f93228b604737b3dda985627f9ef33418b7c573` (`origin/main`, verified with `git fetch` + `rev-parse`); the workspace was 2 commits behind at `89a3007` and was fast-forwarded |
| Live Telegram / Render / Supabase | **NOT touched.** No deployment, no Render shell or log stream, no `.env`, no credentials, no SQL, no migration, no restart, no environment change — and this workspace has **no Render access at all** (see §7) |
| Workspace interpreter | CPython **3.10.12**, OpenSSL 3.0.2; inspected deps: `httpx 0.27.0`, `httpcore 1.0.9`, `h2 4.4.1`, `supabase 2.4.2`, `postgrest 0.16.11`, `telethon 1.43.0` |
| Deployed interpreter | **CPython 3.11.7** (`render.yaml` → `PYTHON_VERSION: 3.11.7`) — a gap the previous round did not record |

---

## 1. Objective

Resolve — or, if the evidence does not allow it, make decidable in one pass — the
recurring production transport failure reported after the October 18–19
deployments:

```text
TASK_FALLBACK_CLASSIFIED reason=local_resource phase=read exception=ReadError
  chain=ReadError<ReadError<BlockingIOError(errno=11) elapsed=0.0s since_ok=0.0s
Supabase event task query failed; using fallback
ConnectionTerminated error_code:1                    (separate line)
```

without confusing it with the already-fixed lifecycle-task diagnostic
misclassification, without conflating `ConnectionTerminated` with `EAGAIN`, and
without presenting a hypothesis as a confirmed cause.

---

## 2. What was read before any edit

`INVESTIGATION.md`, `IMPLEMENTATION_REPORT.md`, `render.yaml`, `Procfile`,
`backend/requirements.txt`, `backend/db/client.py` (dispatch, pool, deadlines,
`_timed_db_call`/`db_inflight_seconds`, client construction),
`backend/ai/database/task_repository.py` (classification, chain walker, transport
phase, cooldown, `_run`/`_run_checked`, degradation sites),
`tests/test_task_transport_evidence.py`, `tests/test_local_resource_bounds.py`,
`tests/test_task_repository.py` — plus the installed sources of the pinned
transport: `httpcore/_backends/sync.py`, `httpcore/_sync/http2.py`,
`httpcore/_sync/connection_pool.py`, `httpx/_client.py`,
`postgrest/_sync/client.py`, `postgrest/utils.py`, and `/usr/lib/python3.10/ssl.py`.

---

## 3. Confirmed facts

| Claim | Status | Evidence |
|---|---|---|
| One shared synchronous `httpx.Client` with HTTP/2 on, dispatched from one bounded 4-worker pool through `run_sync_db` | **Confirmed** | `postgrest` builds `SyncClient(..., http2=True)`; `get_db()` passes `ClientOptions(postgrest_client_timeout=8.0)`; `_DB_MAX_WORKERS = 4`, `0 < 8.0 < 10.0` (test-pinned) |
| The failing read is `sock.recv` under `settimeout(8.0)` | **Confirmed** | `httpcore._backends.sync.SyncStream.read` is exactly `settimeout(timeout)` + `recv(max_bytes)`; `ReadError` exists nowhere else in httpcore |
| The failure is a genuine local `OSError(EAGAIN)` | **Confirmed** | `OSError(errno.EAGAIN)` *is* `BlockingIOError`; the production chain names errno **11** |
| It is not a deadline, not saturation, not fd exhaustion, not a store outage, not a peer close/RST/GOAWAY | **Confirmed / measured** | §4 (probe matrix) and `elapsed=0.0s`, `since_ok=0.0s` |
| Nothing retries anything | **Confirmed** | No `retry`/`max_retries` in `postgrest` or `httpx`; `run_sync_db` dispatches once; `httpcore` is constructed with `retries=0` |
| httpcore's `HTTP2Connection.close()` takes **no lock** and can run while another thread is inside `_read_incoming_data` | **Confirmed** | Source comment in `httpcore/_sync/http2.py`: *"this method unilaterally closes the connection, and does not have any kind of locking in place around it"*; `_response_closed()` calls it when the connection was terminated and no events remain |
| That race does **not** by itself produce the observed signature | **Confirmed by measurement** | A blocked read survives a sibling `close()`/`os.close(fd)`/fd reuse and returns a clean `b''` or the full-timeout result — never `BlockingIOError(11)` (§4) |
| The deployed interpreter differs from the one every previous conclusion was measured on | **Confirmed** | `render.yaml` pins `PYTHON_VERSION: 3.11.7`; this workspace is 3.10.12 |

**Leading mechanism (medium confidence, source-backed, not proven on the deployed
build):** the SSL layer surfaces an `EAGAIN` as a fatal `SSL_ERROR_SYSCALL`
instead of a retryable `SSL_ERROR_WANT_READ`; CPython's `_ssl.c` then raises
`OSError(EAGAIN)` → `BlockingIOError`, which httpcore maps to `ReadError`. OpenSSL
documents `SSL_ERROR_SYSCALL` as fatal, a public OpenSSL users-list report
describes precisely this misclassification on a non-blocking socket, and CPython's
socket code documents the *readiness false positive* that precedes it. Remaining
alternatives and their status: teardown coupling (now measured, contradicted as a
direct mechanism), platform/network-stack behaviour on Render (unverifiable here).

---

## 4. Probe matrix (new evidence, run in this workspace)

`scratchpad/eagain_probe.py`, `scratchpad/eagain_probe2.py` — **untracked
diagnostic scripts, deliberately not part of the repository** — build a local
self-signed TLS pair and drive each condition against a timed `SSLSocket`:

| Condition | Result | Elapsed |
|---|---|---|
| Peer silent (baseline) | `TimeoutError(errno=None)` | 8.008 s |
| Peer FIN while blocked in read | `b''` | 0.301 s |
| Peer RST while blocked in read | `b''` | 0.201 s |
| Our own `close()` from another thread while blocked | `b''` (not interrupted) | 8.009 s |
| `os.close(fd)` from another thread while blocked | `b''` | 8.008 s |
| fd closed **and reused** by a non-blocking socket, then read | `TimeoutError` | 8.501 s |
| `settimeout(1e-9)` / `settimeout(0.001)` | `TimeoutError` | ≈0 s |
| `settimeout(0)` on an `SSLSocket` | `SSLWantReadError(errno=2)` | 0 s |
| `unwrap()` then read (raw fallback) | `SSLError(errno=1)` | 0.05 s |
| Two concurrent readers sharing one `SSLSocket` (400 iters) | `SSLWantReadError` only; **0** `BlockingIOError` | — |

This is what removes the local explanations that would have justified a
transport-side change, and it is why the round ships diagnostics rather than a
behaviour change.

---

## 5. Implemented change (diagnostic only)

### 5.1 `backend/db/client.py`

* `_note_transport_teardown(exc)` — records the monotonic instant of the most
  recent **peer/HTTP-2 termination** (class names `ConnectionTerminated` /
  `RemoteProtocolError`) seen in a failure chain, at the single dispatch point
  every synchronous Supabase call already passes. The walk is capped at 8 links,
  cycle-safe, wrapped so it can never raise, and captures no payload.
* `seconds_since_transport_teardown()` — the gap, or `None` when no teardown has
  been observed in this process.
* The existing init line now carries the deployed runtime identity:
  `[SAVE_DB] Supabase client initialised. runtime=python-X.Y.Z ssl=<OpenSSL>`.

Both are bookkeeping on the failure path. The measured exception object, its
propagation, the dispatch count, the deadlines, the pool, the cooldown and the
classification are all untouched.

### 5.2 `backend/ai/database/task_repository.py`

The once-per-episode local-resource line gains one bounded field, so the two
production signatures are correlated by the code instead of by eye:

```text
TASK_FALLBACK_CLASSIFIED reason=local_resource phase=read exception=ReadError \
  chain=ReadError<ReadError<BlockingIOError(errno=11) elapsed=0.0s since_ok=0.0s \
  since_teardown=none message=[Errno 11] Resource temporarily unavailable
```

`since_teardown` is `<seconds>s` or `none` — the same bounded formatting as
`since_ok`, so the line stays numbers/enums only.

**Deliberately NOT done**

* No retry of any kind — a failed write's completion stays ambiguous.
* No suppression/relabelling: `EAGAIN` is still `reason=local_resource`, and the
  non-durable fallback label is unchanged.
* No change to the 5 s cooldown, the 8 s < 10 s ordering, the worker count, the
  HTTP/2 setting, the client lifecycle, dependencies, or any Render/env setting.
* No schema, SQL, migration or `DATABASE_ARCHITECTURE.md` change.
* No URL, key, header, token or message text in any new field.

### Why this and not a fix

The mechanism is unproven on the deployed build and the local probe matrix
removes the explanations that a transport-side change would rest on; the one
non-hypothesis-driven shape — re-issuing an ambiguous write — is forbidden. So
the round ships the two fields that make the next episode decidable (see §8),
plus the runtime identity that removes a load-bearing unknown.

---

## 6. Tests

New coverage in `tests/test_task_transport_evidence.py` (+4 tests, 12 existing
kept and updated):

| Test | Pins |
|---|---|
| `test_only_a_peer_or_http2_termination_is_recorded_as_a_teardown` | a socket-layer read failure and a timeout record **nothing**; a `RemoteProtocolError`/`ConnectionTerminated` records a real gap; the failing call is still issued exactly once |
| `test_the_recorder_is_bounded_and_never_raises_on_a_pathological_chain` | a cyclic chain and a 200-link chain neither spin nor escape the bookkeeping, and a match beyond the bound is not recorded |
| `test_the_line_reports_the_gap_since_the_last_observed_teardown` | `none` without a teardown; a real seconds gap after one, on a new episode |
| `test_the_client_init_line_carries_the_runtime_identity` | the init line names the Python/OpenSSL of the deployed build and carries no URL or key |
| `test_evidence_fields_are_bounded_and_secret_free` (extended) | `since_teardown` matches `\d+\.\ds|none`; the line stays under 400 characters and free of the injected secret |

Existing pins that remain green: the in-flight duration is measured inside the
worker and the same exception object travels; a watchdog abandonment reports
`n/a`; the phase matrix; bounded seconds formatting; an immediate would-block and
a deadline remain distinguishable; a remote HTTP/2 termination is still
`unavailable` and arms no cooldown; an ambiguous failed insert is issued exactly
once; a cooldown-skipped call adds no second line.

### Results (exact commands)

| Command | Result |
|---|---|
| `.venv/bin/python -m pytest tests/test_task_transport_evidence.py -q -p no:cacheprovider` | **16 passed** |
| `.venv/bin/python -m pytest tests/test_task_transport_evidence.py tests/test_task_fallback_classification.py tests/test_task_fallback_cooldown.py tests/test_local_resource_bounds.py tests/test_task_repository.py -q -p no:cacheprovider` | **68 passed** |
| `.venv/bin/python -m pytest tests/test_07_diagnostics.py tests/test_11_runtime_wiring.py tests/test_task_scheduler.py tests/test_task_execution.py tests/test_03_database_consistency.py tests/test_06_failure_simulation.py tests/test_22_reliability.py tests/test_24_execution_reliability.py tests/test_30_delete_timeout_hardening.py -q -p no:cacheprovider` | **91 passed** |
| `.venv/bin/python -m pytest tests -q -p no:cacheprovider` | **6438 passed, 27 skipped, 0 failed** in 124.05 s |
| `.venv/bin/python -m py_compile backend/db/client.py backend/ai/database/task_repository.py tests/test_task_transport_evidence.py` | exit 0 |
| `git diff --check` | clean |

**Baseline revalidated, not assumed.** The previous round's full run reported
`6394 passed, 27 skipped, 40 failed`, with every failure in
`tests/test_colored_buttons.py` attributed to a workspace Telethon **1.34.0** vs
the pinned **1.43.0**. The workspace now resolves **Telethon 1.43.0** (`pip list`,
matching `backend/requirements.txt`) and **all 40 now pass**: the historical
failure set is empty and no new failure exists. Accounting: 6394 + 40 (repaired
by the environment now matching the pin) + 4 (new) = 6438. The 27 skips are the
unchanged opt-in/live tests.

---

## 7. Files changed

| File | Why |
|---|---|
| `backend/db/client.py` | Record the most recent peer/HTTP-2 termination at the single dispatch point and expose the gap; log the runtime identity once when the client is built |
| `backend/ai/database/task_repository.py` | Add the bounded `since_teardown` field to the existing once-per-episode diagnostic |
| `tests/test_task_transport_evidence.py` | Four new tests + the extended secret-free/bounded pin |
| `INVESTIGATION.md`, `IMPLEMENTATION_REPORT.md` | Full replacements (this round) |

Untouched: schema, migrations, SQL, `DATABASE_ARCHITECTURE.md`, `AGENTS.md`,
`ROADMAP.md`, `backend/requirements.txt`, Render configuration, `.env`; no second
client/executor/scheduler/retry loop; the pre-existing workspace modifications
(`backend/services/premium_emoji_inline_service.py`, untracked
`backend/test_patch_story.md`, `scratchpad/`, `telegram-self-bot/`) were preserved
and not staged.

---

## 8. Production verification — what is reachable, and what is not

**No Render verification was performed, and none could be performed from this
workspace.** Checked, not assumed:

* no Render CLI is installed, `freebuff-env list` reports **no** environment keys
  set in this workspace at all (so no Render API credential exists here), and no
  Render integration is available to this session;
* GitHub is reachable through the managed credential, but that exposes **no**
  Render deploy signal for this repository: the commit-status API returns an
  empty status list for `6f93228`, `c9e5203` and `17942f1`, and the only GitHub
  deployments on record are from 2026-08-17/2026-10-13 unrelated branch deploys.

Therefore the following are **unverified by this round** and belong to the
owner's Render dashboard: whether the delivery commit is deployed; whether the
errno-11 line recurs and how often; whether `since_teardown` shows coupling to
the `ConnectionTerminated` line; and the deployed OpenSSL build (now printed by
the init line). A commit and a push are not proof of deployment or recovery.

---

## 9. Next smallest action

1. Confirm Render deployed the commit that carries this change (the init line
   prints `runtime=`/`ssl=`, so the deployed build identifies itself).
2. Read the next `TASK_FALLBACK_CLASSIFIED` line and apply the decision table in
   `INVESTIGATION.md` §5 — `since_teardown=none` (teardown family disproven →
   SSL/platform family) versus `since_teardown≈0…few s` (coupling confirmed →
   design the smallest change that removes the coupling).
3. Only then decide whether a change is warranted, and keep it to the two bounded
   options in `INVESTIGATION.md` §6 — never a blind retry of an ambiguous write.
