# INVESTIGATION.md — Recurring Supabase transport `ReadError` / `BlockingIOError(errno=11)`

This document is the **complete current investigation and handoff** for the
recurring Supabase transport failure. It replaces every previous investigation in
full; earlier handoffs (the transport round at `6f93228`, the AED archive rounds,
the private-message rounds, the lifecycle-task stall round, the coloured-button
round) remain readable in Git history and are superseded here.

---

## 1. Executive Summary

**Best-supported conclusion.** The recurring production failure is a **local,
read-path socket/SSL would-block (`EAGAIN`, errno 11)** on the shared
synchronous Supabase transport (HTTP/2 enabled by its PostgREST client). It
surfaces as

```text
httpx.ReadError  <-  httpcore.ReadError  <-  BlockingIOError(errno=11)
```

and is reported by the task repository as a once-per-episode
`TASK_FALLBACK_CLASSIFIED reason=local_resource phase=read ...` line. The
failure is **immediate** (`elapsed=0.0s`), **intermittent**, and interleaved with
successful durable operations (`since_ok=0.0s`). It is **not** a transport
deadline, not executor or pool saturation, not a Supabase outage, and not a
GOAWAY/`ConnectionTerminated` reaching the read itself.

**Confidence.** The *observed mechanism* (how errno 11 becomes `ReadError`, and
which phase produced it) is **Confirmed** from dependency source, repository
source, production fields and a reproducible local mapping test. The *root cause*
(why the SSL layer returns `EAGAIN` at all) is **not proven**. The strongest
remaining explanation — OpenSSL/CPython surfacing a would-block as a fatal
`SSL_ERROR_SYSCALL` rather than a retryable `SSL_ERROR_WANT_READ` — is
**Plausible** (medium), and the deployed CPython version materially changes how
that condition is classified (see §7 and §10).

**Operationally.** Each episode makes the AI task repository serve its in-memory
fallback for a bounded **5 s** cooldown and labels the affected operations
non-durable. The durable store is demonstrably healthy immediately before the
failure (`since_ok=0.0s`), so this is a blip on a working store, not an outage.
No evidence shows data loss or a corrupted write, and no automatic retry of an
ambiguous write exists (correctly).

**New in this round.** The repository and the dependency source were re-read at
`33d55d1`; the deployed runtime discrepancy was reassessed against the
previously inspected live build logs (**Python 3.13.4 / OpenSSL 3.0.20**, not the
`render.yaml` pin of `3.11.7`); and CPython's **3.13-only change** to
`PySSL_SetError` (GH-115627 / GH-115628) was verified. That change makes an SSL
syscall `EAGAIN` surface as `BlockingIOError(11)`, where CPython 3.10–3.12
raises `SSLEOFError(errno=8)`. This is **Strongly supported** as the reason the
signature is visible in production and was never reproduced on the workspace's
3.10.12 — but it explains the *label*, not the *trigger*, and must not be called
the root cause.

**Next step.** One controlled experiment: disable HTTP/2 only, in a staging clone
of the deployed runtime, and compare the errno-11 rate against an equal baseline
window (§13). Read the next production episode's `since_teardown` field as a
free, concurrent observation (no change required).

---

## 2. Investigation Metadata

| Item | Value |
|---|---|
| Report date/time | **2026-10-19 (UTC)**; evidence collected between ~20:00Z and ~20:45Z |
| Repository | `Onlyicing1/Telegram-self-bot` (`origin`) |
| Target branch | `main` (delivery target `origin/main`) |
| Starting commit | **`33d55d12f5e6c6413d2ccda075984149ac639862`** (`docs(investigation): state what the log window and rate could not establish`, 2026-10-19T11:12:50Z) |
| Final commit | The commit that carries this file on `main`; resolve it with `git log -1 -- INVESTIGATION.md` (a file cannot contain the SHA of its own commit). The completion report states the exact SHA. |
| Delivery branch used here | `investigation-rebuild`, created from verified `origin/main`; the pre-existing local `main` (`d084e6b`, one unpushed AED commit) was **not** modified, rebased, reset or force-pushed |
| Evidence scope | Full read of the repository at `33d55d1`; installed dependency source/metadata in `.venv`; `INVESTIGATION.md` history across all branches; read-only GitHub deployment metadata; previously recorded live observations from owner-supplied logs; local test runs |
| Live Render / Supabase / Telegram | **NOT accessed in this run.** No Render shell, API credential, log stream, metric, SQL, migration, restart, deploy or environment change. No Telegram request. Previously inspected live values are labelled historical and are **not** re-verified here. |
| Application behaviour changed | **None.** Only this document is intentionally modified. |

**Confidence vocabulary used below**

| Label | Meaning |
|---|---|
| **Confirmed** | Directly established by current source, dependency source, reproducible measurement, or authoritative metadata |
| **Strongly supported** | Multiple independent observations agree, but an important causal link is unproven |
| **Plausible** | Technically possible and consistent with evidence, but insufficiently supported |
| **Contradicted (tested)** | Did not match the observed behaviour under a specific, stated test; not generalised beyond it |
| **Unknown / unverified** | Evidence is unavailable or insufficient |

---

## 3. Incident Definition

### 3.1 Exact signatures

| Signature | Level | Meaning |
|---|---|---|
| `httpx.ReadError: [Errno 11] Resource temporarily unavailable` | outer exception | httpx's mirror of a transport read failure |
| chain `ReadError < ReadError < BlockingIOError(errno=11)` | cause chain | `httpx.ReadError` caused by `httpcore.ReadError` caused by the OS/`ssl` `EAGAIN` |
| `TASK_FALLBACK_CLASSIFIED reason=local_resource phase=read exception=ReadError chain=ReadError<ReadError<BlockingIOError(errno=11) elapsed=0.0s since_ok=0.0s since_teardown=<seconds|none> message=[Errno 11] ...` | application diagnostic | one line per continuous degradation episode (field set depends on the deployed commit) |
| `ConnectionTerminated error_code:1` | separate line | an HTTP/2 GOAWAY (PROTOCOL_ERROR) rendered by h2; **not** the same exception shape as the read failure |

Historical variants of the diagnostic line (before the 2026-10-19 instrumentation
rounds) carried only `reason`, `exception` and `message`; intermediate builds
added `phase`, `elapsed`, `since_ok`, `chain`, then `since_teardown`. The field
set therefore identifies which build produced a captured line (§9).

### 3.2 What is being explained

1. Why the shared Supabase transport repeatedly fails **on the read path** with
   `EAGAIN`/errno 11 in **0.0–0.1 s**, on an otherwise healthy store.
2. What the `reason=local_resource` classification actually establishes (and
   does not).
3. Whether the separate `ConnectionTerminated error_code:1` line is causally
   related to the errno-11 read failures.
4. Which of the remaining explanations (SSL/TLS error classification, HTTP/2
   concurrency, connection reuse, runtime version, Render platform) the evidence
   supports — and which it rules out.

### 3.3 Scope boundaries

In scope: every caller of `backend.db.client.run_sync_db`, the one shared
Supabase `httpx` client, PostgREST, httpcore, HTTP/2, the socket/TLS layer, the
diagnostic classifier, and the deployed runtime identity.

Out of scope by evidence (separate transports/clients): Telethon's own
connection, the Gemini/media HTTP paths (per-call clients), AI provider HTTP
calls, and the helper bot. They are mentioned only where a log line was
previously mis-attributed.

---

## 4. Evidence Inventory

| ID | Source | Revision / timestamp | Establishes | Limitations |
|---|---|---|---|---|
| E1 | `backend/db/client.py` | `33d55d1` | One shared client; 4-worker bounded pool; 8 s transport deadline; 10 s dispatch watchdog; in-flight timing; teardown recorder; runtime identity log | Runs on the workspace, not Render |
| E2 | `backend/ai/database/task_repository.py` | `33d55d1` | `local_resource` classifier; errno set; 5 s cooldown; `phase`/`chain`/`elapsed`/`since_ok`/`since_teardown` emission | Applies to the AI task repository path only |
| E3 | `backend/requirements.txt` | `33d55d1` | `supabase==2.4.2`, `httpx==0.27.0`; `httpcore`, `h2`, `postgrest` unpinned | No lockfile → deployed transitive versions unknown |
| E4 | Installed dependency source/metadata (`.venv`) | read 2026-10-19 | `postgrest 0.16.11` hard-enables `http2=True`; `httpcore 1.0.9` maps `OSError → ReadError`; `h2 4.4.1`; `h11 0.16.0`; `anyio 4.15.1`; `gotrue 2.12.4` | Workspace Python 3.10.12 / OpenSSL 3.0.2; production may resolve different versions |
| E5 | `render.yaml`, `Procfile`, `README.md` | `33d55d1` | Declared service `lifeos`, `PYTHON_VERSION=3.11.7`, start command, health path; README instructs creating a service manually or importing the Blueprint | A manifest is not the live service configuration |
| E6 | Owner-supplied live build/startup observations | historical, previously inspected; undated in this workspace | Deployed Python **3.13.4**, OpenSSL **3.0.20**; live service metadata differed from `render.yaml` (name `Self_bot`, health/build differing) | Not re-verified in this run; exact log timestamps unavailable here |
| E7 | Production log excerpts recorded in repository history | see §5 | The errno-11 signature; `elapsed=0.0s`; `since_ok=0.0s`; a separate `ConnectionTerminated error_code:1` line | Owner-supplied excerpts; exact UTC timestamps not complete; no live stream |
| E8 | `INVESTIGATION.md` history (`3089387`, `a54877b`, `6f93228`, `c9e5203`, `8e0e401`, `33d55d1`) | 2026-09-17 → 2026-10-19 | Prior source audits, the 2026-10-10 production burst, the local probe matrix, the instrumentation rounds | Each round had a different scope and environment; superseded conclusions are labelled in §11 |
| E9 | `tests/test_task_transport_evidence.py`, `tests/test_task_fallback_classification.py`, `tests/test_task_fallback_cooldown.py`, `tests/test_local_resource_bounds.py` | `33d55d1` | Pins the diagnostic semantics: phase matrix, bounded chain, in-flight measurement, teardown recording, no retry, once-per-episode | Tests validate code shape, not production OS behaviour |
| E10 | GitHub read-only API | 2026-10-19 | Deployment records exist only for Render **preview** environments named `... - Self_bot PR #NNN` (latest 2026-10-13); no production/main deployment record; no commit statuses for `33d55d1` | Not a Render API; absence proves only that GitHub has no record |
| E11 | Render documentation, "Setting Your Python Version" | retrieved 2026-10-19 | Default Python for services created 2025-06-12 → 2026-02-11 is **3.13.4**; a `PYTHON_VERSION` env var overrides it | Explains the observed version only if the service was created in that window |
| E12 | CPython issue GH-115627 / fix GH-115628; OpenSSL `SSL_get_error` docs | retrieved 2026-10-19 | CPython 3.13 raises `OSError(errno)` for `SSL_ERROR_SYSCALL` with a zero `SSL_read_ex` return; 3.10–3.12 raise `SSLEOFError` | External authoritative sources; not a production measurement |
| E13 | Local test runs in this workspace | 2026-10-19 | Transport suites pass; full-suite result and its environment-dependent failures | Python 3.10.12; Telethon 1.34.0 (pin is 1.43.0) — see §11 |

**Evidence not available:** the Render log stream, Render service metadata/API,
post-deploy runtime logs, a `pip freeze` from the deployed build, host counters,
and the untracked probe scripts from the previous round. §15 lists these with the
exact method to obtain each.

---

## 5. Production Evidence and Timeline

All times are UTC. Every entry is an **observation supplied by the owner or
recorded in repository history**; this run did not read the Render log stream.

| # | Time | Observation | Source | What it establishes |
|---|---|---|---|---|
| P1 | 2026-09-17T08:09:50Z (first line of the excerpt) | `TASK_FALLBACK_CLASSIFIED reason=local_resource exception=ReadError message=[Errno 11] Resource temporarily unavailable` in a window where the Telegram loop was healthy (live RPC ~165–175 ms, `READY`, no stall warning) | E8, `3089387:INVESTIGATION.md` §20.1 | The errno-11 read failure was already occurring on 2026-09-17, on the task-repository/Supabase path, independently of the STT request in the same window |
| P2 | 2026-10-10 ~15:32:57–15:33:07 | Task-repository queries repeatedly failed with `ReadError: [Errno 11] Resource temporarily unavailable` (a burst, not a single line); `~15:33:11` an unrelated provider call timed out after 30 s | E8, `a54877b:INVESTIGATION.md` §5 | The failures recur in bursts across several minutes of traffic; they are contained by the repository fallback and are not on the premium-emoji path |
| P3 | 2026-10-19, exact time not recorded, after the deployment that carried `c9e5203` (the line contains fields that commit introduced) | `TASK_FALLBACK_CLASSIFIED reason=local_resource phase=read exception=ReadError chain=ReadError<ReadError<BlockingIOError(errno=11) elapsed=0.0s since_ok=0.0s`; a **separate** `ConnectionTerminated error_code:1` line | E8, `33d55d1:INVESTIGATION.md` §1 | The failure is a read-phase refusal, measured **inside** the worker as immediate (`elapsed=0.0s`), preceded by a durable success (`since_ok=0.0s`). The `since_teardown` field did not exist in that build. |
| P4 | 2026-10-13T09:54–21:05 | Render preview deployments recorded in GitHub for `Self_bot` PR #183, #184, #185 | E10 | The live service is named **`Self_bot`**, and preview environments are enabled; these are preview, not production, records |
| P5 | No record | No GitHub deployment, commit status or check run exists for `main`/`33d55d1` | E10 | Production deploy state cannot be inferred from GitHub; the `Self_bot` service may deploy outside GitHub deployment records, or be dashboard-managed |

**What the timeline establishes.** (a) The failure predates every 2026-10-19
instrumentation round by at least a month. (b) It occurs in isolated bursts on
an otherwise working store. (c) The post-instrumentation line proves the read
phase and an immediate refusal, not a timeout. (d) The separate
`ConnectionTerminated` line existed in the same deployment window, but **no
timestamped pairing has ever been recorded**. Correlation remains unmeasured;
the field added in `8e0e401` is the first mechanism that will measure it.

**What it does not establish.** The recurrence rate (events per hour), the exact
failure timestamps, the number of distinct sockets/connections involved, the
deployed dependency versions, or whether the `ConnectionTerminated` line and the
read failure belong to the same connection or even the same request.

---

## 6. Actual Request and Transport Path

### 6.1 Application dispatch (verified at `33d55d1`)

```text
async service / handler
  └─ run_sync_db(fn, ..., timeout=10)                  backend/db/client.py:223
      ├─ loop.run_in_executor(_get_db_executor(),
      │                       _timed_db_call, fn, args, kwargs)      :238-239
      │     └─ fn(*args)  →  get_db().table(...)....execute()
      └─ guarded_await(..., name="db:<fn>", timeout=10)
            backend/runtime/operation_watchdog.py:81
            └─ asyncio.wait_for(coro, 10)             :114
```

* The pool is a single `ThreadPoolExecutor(max_workers=4,
  thread_name_prefix="lifeos-supabase")` (`client.py:128-137`).
* `_timed_db_call` (`client.py:188-207`) runs **inside** the worker, measures the
  transport call, tags the raised exception with the in-flight seconds, records
  a peer/HTTP-2 termination if present in the chain, and re-raises the **same**
  exception object. Nothing is retried, wrapped or re-classified.
* `asyncio.wait_for` cancels the awaiting future at 10 s but **cannot cancel the
  thread**; a blocked transport call keeps its worker until the transport
  deadline releases it. That deadline is **per read call** (httpx `read`
  timeout), not a single 8 s budget for the whole response, so a slowly
  streaming body can outlive 8 s and be abandoned by the 10 s watchdog. When the
  watchdog fires, no in-flight duration exists and the diagnostic reports
  `elapsed=n/a` with `phase=deadline`.

### 6.2 Threads that share the one client

The 4-worker pool is **not** the only caller of the shared client. Other
subsystems use the event loop's default executor (`asyncio.to_thread`) while
calling the same synchronous client, for example:

* `backend/ai/config_store.py:123-127` (`_run_sync` → `asyncio.to_thread`),
* `backend/ai/database/usage_recorder.py:46` (`asyncio.to_thread(_write_sync, ...)`),
  whose target is the synchronous `SupabaseUsageRepository`
  (`backend/ai/database/usage_repository.py:134-160`),
* `backend/services/ghost_seen_v2.py:159` (`await asyncio.to_thread(_load_sync)`).

Consequences: the shared `httpx.Client` can be entered by **more than four
threads**, and the 4-worker bound applies only to `run_sync_db` callers. This is a
verified code fact, not a measurement of production concurrency.

### 6.3 Synchronous stack inside the worker

```text
supabase 2.4.2  SyncClient (supabase/_sync/client.py)
  └─ SyncPostgrestClient(timeout=8.0)      supabase/_sync/client.py:239-248
      └─ httpx.Client(http2=True, timeout=Timeout(8.0), default Limits)
             postgrest/_sync/client.py:43-56  (http2=True is unconditional)
          └─ HTTPTransport.send → httpcore.ConnectionPool.handle_request
              ├─ HTTP2Connection  if ALPN negotiated h2   (httpcore/_sync/connection.py:147)
              │    ├─ _write_lock  → _write_outgoing_data → SyncStream.write
              │    └─ _read_lock   → _receive_events → _read_incoming_data
              │           → SyncStream.read
              └─ HTTP11Connection otherwise
                    └─ SyncStream.read  (httpcore/_backends/sync.py:124-131)
                         ├─ self._sock.settimeout(timeout)
                         └─ self._sock.recv(max_bytes)
                              └─ SSLSocket → OpenSSL → kernel
```

**Honest boundary statements.** The ALPN negotiation on the deployed service is
**not** directly logged; HTTP/2 use is inferred from the h2-only
`ConnectionTerminated` string (E7) and from the verified `http2=True`
configuration (E4). No request id, connection id or stream id is recorded, so an
individual request cannot be tied to a specific socket after the fact.

### 6.4 Why the chain has this exact shape

`httpcore` wraps socket operations with
`map_exceptions({socket.timeout: ReadTimeout, OSError: ReadError})`
(`httpcore/_backends/sync.py:124-131`). `BlockingIOError` is an `OSError` and not
a `socket.timeout`, so it becomes `httpcore.ReadError`; httpx mirrors it as
`httpx.ReadError` (`httpx/_transports/default.py:97`). A connect-phase EAGAIN
would have become `ConnectError`; a write-phase one `WriteError`. The observed
outer class therefore **pins the failure to a read call on an established
connection**, not to `connect()` and not to an HTTP status.

---

## 7. Effective Runtime and Dependency Versions

| Component | Repository declaration | This workspace (read 2026-10-19) | Deployed (previously inspected live; historical) | Confidence |
|---|---|---|---|---|
| Python | `render.yaml`: `3.11.7` | **3.10.12** | **3.13.4** | Repository pin **Confirmed**; live value **previously observed, not re-verified here**; workspace value **Confirmed** |
| OpenSSL | not declared | **3.0.2** | **3.0.20** | Same as above |
| `supabase` | `==2.4.2` | 2.4.2 | assumed 2.4.2 | **Confirmed** pin; live install inferred from the pin |
| `httpx` | `==0.27.0` | 0.27.0 | assumed 0.27.0 | **Confirmed** pin; live install inferred |
| `postgrest` | not pinned; `supabase 2.4.2` requires `>=0.14,<0.17.0` | 0.16.11 | **unknown** | Constraint **Confirmed**; deployed version **Unknown** |
| `httpcore` | not pinned; `httpx 0.27.0` requires `1.*` | 1.0.9 | **unknown** | Constraint **Confirmed**; deployed version **Unknown** |
| `h2` | not pinned; installed via postgrest's `httpx[http2]` extra | 4.4.1 | **unknown** | Installed locally **Confirmed**; deployed version **Unknown** |
| `h11` / `hpack` / `anyio` / `gotrue` | not pinned | 0.16.0 / 4.2.0 / 4.15.1 / 2.12.4 | **unknown** | Local only |
| Telethon | `==1.43.0` | **1.34.0** | unknown | Workspace does **not** match the pin (affects tests, not the DB path) |

**Why this matters.**

1. The workspace is **three minor Python versions behind** the deployed runtime
   and uses a different OpenSSL patch level. Every local measurement in E8 and
   §11 is a 3.10.12/3.0.2 measurement unless stated otherwise.
2. CPython's SSL error classification changed in **3.13 only** (GH-115627,
   fixed by GH-115628): with `SSL_read_ex` returning 0 on error, 3.10–3.12 hit
   the `ret == 0` branch and raise `SSLEOFError`; 3.13 checks the captured
   `errno` and raises `OSError` from it. On Linux, `EAGAIN` (11) therefore
   becomes **`BlockingIOError`** on 3.13 and `SSLEOFError(errno=8)` on 3.10–3.12.
3. **Prediction (falsifiable):** the same underlying SSL syscall failure on the
   pinned `3.11.7` would be logged as `SSLEOFError(errno=8)` and classified
   `reason=unavailable` (because errno 8 is not in the local-resource set), not
   `reason=local_resource`. That prediction is the basis of alternative
   experiment A1 in §14.
4. The deployed **`postgrest`/`httpcore`/`h2` versions cannot be established from
   the repository** (no lockfile). All source claims about `http2=True` and the
   exception mapping are verified for the locally installed versions above.

---

## 8. Render Configuration Comparison

| Field | `render.yaml` (declared) | Live service (previous inspection; historical) | Assessment |
|---|---|---|---|
| Service name | `lifeos` (`render.yaml:3`) | **`Self_bot`** | **Mismatch.** GitHub deployment environments independently name the Render service `Self_bot` (P4). A Blueprint-defined service would carry the manifest name. |
| Python version | `PYTHON_VERSION=3.11.7` (`render.yaml:7-8`) | **3.13.4** | **Mismatch.** Render documents 3.13.4 as the **default** for services created 2025-06-12 → 2026-02-11. That exactly matches the observed value without any env var, i.e. the declared pin is not applied to this service. |
| OpenSSL | not declared | **3.0.20** | Consistent with a Render-built CPython 3.13.4 image; not independently verified here. |
| Start command | `python -m backend.main` (`render.yaml:5`); also `Procfile` | unknown | `Procfile` is not a Render mechanism; its presence is legacy/duplicate. |
| Build command | **absent** | reported to differ (owner-supplied) | A dashboard build command would override Render's default `pip install -r requirements.txt`. |
| Health check path | `/health` (`render.yaml:6`) | reported to differ (owner-supplied) | If the live path differs, deployment health gating differs from the manifest. |
| Repository / branch | not specified in the manifest | unknown | Cannot verify which commit is deployed. |
| Deployment record | — | no GitHub deployment/status for `main`; previews only (P4/P5) | Production deploy state is not observable from GitHub. |

**How the drift is possible (Confirmed).** `README.md:195-201` instructs the
operator to *create a new web service* and then either add variables via the
dashboard **or** import the Blueprint. A service created dashboard-first is not
bound to this `render.yaml`; its `PYTHON_VERSION` comes from the dashboard (or
from Render's default), which explains both the name and the runtime. Render's
precedence is: service env var → `.python-version` file → creation-date default
(E11). The repository has no `.python-version` and no `runtime.txt`.

**What the drift does and does not mean.** It is **Confirmed configuration
drift** and it materially changes error classification (§7). It is **not** proof
that the runtime causes the `EAGAIN`: a would-block still has to occur. The
deployed build remains the correct environment in which to test, and it cannot
be reproduced in this workspace.

---

## 9. Instrumentation Semantics

The diagnostic line is emitted by
`SupabaseTaskRepository._mark_fallback` (`backend/ai/database/task_repository.py:713-779`),
once per continuous degradation episode. Field meanings, and the limits of each:

| Field | Computed where | Meaning | What it establishes | What it does **not** establish |
|---|---|---|---|---|
| `reason` | `_classify_degradation` (`:198-210`) / `_is_local_resource_failure` (`:288-305`) | `local_resource` when the exception chain contains an `errno` in `{EAGAIN, EWOULDBLOCK, EMFILE, ENFILE, ENOMEM, ENOBUFS}`, else `unavailable` | The chain contained **a local-resource errno**; the failure is not being reported as a store outage | That the host is exhausted. The label is a **heuristic over errno**, not a measurement of fds, ports, memory or threads. For a **read-path** EAGAIN it cannot mean ephemeral-port exhaustion, because `connect()` is not involved. |
| `phase` | `_transport_phase` (`:239-262`), keyed by exception class | `read` / `write` / `connect` / `pool` / `protocol` / `deadline` / `unknown` | Which transport **stage** produced the failure | The cause of that stage's failure; a `read` phase says nothing about why the read failed |
| `exception` | outer type name | `ReadError`, `RemoteProtocolError`, … | The outer class only | The innermost cause |
| `chain` | `_describe_exception_chain` (`:264-285`), bounded to 6 links | Class names plus integer `errno` values, e.g. `ReadError<ReadError<BlockingIOError(errno=11)>` | The full cause shape, without messages | Any payload, URL, header or user data (deliberately excluded) |
| `elapsed` | `_timed_db_call` → `db_inflight_seconds` (`backend/db/client.py:188-220`) | In-flight seconds of the synchronous call, measured **inside** the worker; `n/a` when the exception was raised outside it | Whether the socket refused **immediately** (`≈0.0s`) or the transport deadline expired (`≈8s`) | Why it refused. The display is bounded and rounded to 0.1 s. |
| `since_ok` | `_last_ok_monotonic` set by `_mark_supabase_ok` (`:697-708`) | Seconds since the last successful durable operation in this repository instance, or `never` | The durable path was healthy moments before (`0.0s` = under the 0.05 s display floor) | That the **same connection** was healthy, or that the next operation will succeed |
| `since_teardown` | `seconds_since_transport_teardown` (`backend/db/client.py:164-186`) | Seconds since the last **observed** peer/HTTP-2 termination (`ConnectionTerminated`/`RemoteProtocolError`) seen in a failing DB call chain, or `none` | Whether a teardown was observed in this process shortly before the episode | `none` means *no teardown was observed on the DB dispatch path in this process*, not that none occurred. The field is process-wide, not per connection/request. |
| `message` | `str(exc)[:200]` | The OS/transport text | The human-readable text | No additional causal information; it is truncated |

**Episode gating.** The structured line is emitted only when
`_degraded_announced` is false, and that flag (with `_last_ok_monotonic`) is reset
only by a **successful durable operation**. Repeated lines therefore prove
repeated **failure → success → failure** cycles, not one continuous outage.

**The 5 s cooldown.** A classified local-resource failure arms
`_local_resource_until = now + 5.0` (`:741`). During the window,
`_run`/`_run_checked` (`:831-851`) raise `LocalResourceCooldown` **before** any
transport call, and the in-memory fallback answers. A cooldown-skipped call does
not extend the window, but it also cannot produce the success that would clear it
early: one sub-millisecond blip guarantees at least 5 s of degraded behaviour.
This is a documented consequence, not a proven production harm.

**Related diagnostics, and their traps.**

| Diagnostic | Semantics | Trap |
|---|---|---|
| `OP_TIMEOUT ... elapsed=<s> timeout=<s> runtime_state=... client_generation=... cancellation_requested=... cancellation_completed=...` (`operation_watchdog.py:127-158`) | The **dispatch** deadline fired | The implementation passes `cancel_ok=True` unconditionally (`:118-121`). A thread blocked in `recv()` cannot be cancelled; `cancellation_completed=True` is therefore a **reporting inaccuracy**, not evidence of cancellation. |
| `ASYNC_TASK_DUMP ... [PERMANENT]/[BOUNDED] ... elapsed=Ns` (`runtime/diagnostics.py`) | Observed task **age**, not stall duration | Genuine stalls are reported by different warnings (`TASK_NO_PROGRESS`, `TASK_STARVATION`, `DEADLOCK_DETECTED`, `SLOW_EVENT_HANDLER`); the dump is informational. |
| `record_event(...)` (`diagnostics.py:23-31`) | In-memory black box | Every database call site passes a hard-coded **0 ms** duration, so latency is not observable there. |
| `ai_last_db_s` (`backend/ai/diagnostics.py:166`) | Age of last `DB_OPERATION` success | `mark_success("DB_OPERATION")` is **never called**, so the value is permanently `-1`. |

**What the existing logs can and cannot distinguish.** They can distinguish
immediate refusal vs deadline (`elapsed`), read vs write/connect/pool/protocol
(`phase`), errno 11 vs fd shortage vs TLS EOF (`chain`), store outage vs local
resource (`reason`, `since_ok`), and (from `8e0e401` onward) temporal coupling to
a teardown (`since_teardown`). They **cannot** distinguish which of the remaining
within-stack producers of an SSL syscall `EAGAIN` occurred, because no socket
identifier, ALPN result, stream id, TLS state or OpenSSL error queue is logged.

---

## 10. Hypothesis Matrix

| # | Hypothesis | Supporting evidence | Contradicting evidence | Confidence | Missing proof | Next discriminating test |
|---|---|---|---|---|---|---|
| H1 | **CPython 3.13 SSL classification** makes the SSL syscall failure appear as `BlockingIOError(11)` (instead of `SSLEOFError`) | Deployed 3.13.4 vs workspace 3.10.12 vs pin 3.11.7 (E6/E5); GH-115627/GH-115628 (E12); the observed chain is exactly the 3.13 errno-based branch; local 3.10 probes never produced errno 11 | Nothing shows 3.13 **creates** the `EAGAIN`; the same syscall failure can occur on any version | **Strongly supported** for the *signature*; **not causal** for the trigger | A 3.11/3.12 run of the same workload showing `SSLEOFError(errno=8)` and `reason=unavailable` | A1 (§14) |
| H2 | **SSL/OpenSSL surfaces a would-block as fatal `SSL_ERROR_SYSCALL`** rather than retryable `SSL_ERROR_WANT_READ`; CPython raises `OSError(EAGAIN)` | OpenSSL documents `SSL_ERROR_SYSCALL` as fatal; the read-path errno 11 chain is exactly that conversion; immediate timing fits a refused read | Not reproduced locally; no OpenSSL error-queue capture; the documented `EAGAIN`-as-`WANT_READ` behaviour is the opposite | **Plausible (medium)** — leading root-cause candidate | The OpenSSL error code and BIO state at failure on the deployed build | A3 (§14) |
| H3 | **HTTP/2 concurrency**: one TLS socket is read and written by different worker threads (separate httpcore read/write locks), and concurrent use of one `SSLSocket` produces a syscall-level would-block | `postgrest 0.16.11` hard-enables `http2=True` (E4); `httpcore/_sync/http2.py:61-62,349,465` uses **separate** read/write locks; CPython states a single `SSLSocket` is not thread-safe (issue 77271 reports `BlockingIOError` under concurrent read/write); HTTP/2 error caching turns one failure into an immediate burst | Local concurrency probe on 3.10 (reader+writer, 200 trials) saw only `TimeoutError`; no production stream/connection id; HTTP/2 use is inferred, not logged | **Plausible (medium)** | A 3.13 reproduction with stream ids; a correlation between failures and concurrent streams | A2 / A4 (§14) |
| H4 | **Connection-teardown coupling**: the read fails because a sibling stream/connection was torn down (`ConnectionTerminated`) concurrently | The two signatures co-occur in the same window; httpcore caches `_read_exception` and re-raises it to sibling streams (`http2.py:433-452`), producing fast bursts; `HTTP2Connection.close()` takes no lock | Local probes: our own `close()`/`os.close(fd)`/fd-reuse during a blocked read returned clean EOF/timeout, **never** `BlockingIOError(11)`; GOAWAY produces `RemoteProtocolError` (no errno) | **Unproven**; the **direct** mechanism is **Contradicted (tested, 3.10)** | A production episode with `since_teardown` on the same line | A5 (§14) — free observation |
| H5 | **Stale/pooled connection reuse** produces the failure | One long-lived pool; keep-alive; an errored h2 connection poisons queued streams | Stale-dead-connection reuse normally yields EOF/`RemoteProtocolError`/reset, not `EAGAIN`; `since_ok=0.0s` shows the pool was working; `elapsed≈0` shows no wait | **Contradicted (weak)** | Connection age / reuse counters | A4 (§14) |
| H6 | **Deadline or executor saturation** | There are 8 s and 10 s bounds and a 4-worker pool | `elapsed=0.0s` (not ≈8 s); `phase=deadline`/`elapsed=n/a` would mark watchdog abandonment; a queued call would show pool timeout or `OP_TIMEOUT`, not errno 11 | **Contradicted (tested)** | None for this signature | Already settled |
| H7 | **Local resource exhaustion** (fds, ports, memory) | `reason=local_resource`; errno 11 is in the local set | errno 11 ≠ 24/23 (EMFILE/ENFILE); a read-path failure has no `connect()`/port binding; no host counters show pressure; the classifier never measures resources | **Contradicted** as an interpretation of this line; **Unknown** at the host level | One host sample (fds, sockets, threads, memory) at an episode | A6 (§14) |
| H8 | **Render platform/network** returns `EAGAIN` where plain Linux would block | Render runs a different kernel/netstack and may proxy connections | No public or repository evidence; no Render access | **Unknown / unverified** | Render metrics/logs or an in-region reproduction | A7 (§14) |
| H9 | **Supabase/store outage, schema or SQL** | The failure is on the DB path | `since_ok=0.0s` (a durable success moments earlier); no HTTP status, PostgREST error code or PGRST message; the chain is a socket error | **Contradicted** | None | Already settled |

### 10.1 Reading the top three honestly

**H1 explains the label, not the event.** CPython 3.13's change is confirmed and
matches the observed innermost exception exactly. But it converts an existing
syscall failure into a different exception type; it does not make a socket
refuse a read. Treating 3.13.4 as the root cause would be wrong. Its operational
relevance is real: on the pinned 3.11.7 the same event would be **mislabelled as
a store outage** (`unavailable`) rather than a local resource condition.

**H2 is the only remaining within-stack producer of errno 11.** The local probe
matrix removes close/fd-reuse/non-blocking/FIN/RST/GOAWAY as direct producers
(under 3.10). What remains is the SSL layer deciding the BIO result is a fatal
syscall error. That is consistent with all fields but has never been captured on
the deployed build (no OpenSSL error queue, no `SSL_get_error` code).

**H3 and H2 are not mutually exclusive.** Concurrent read/write on one
`SSLSocket` is a plausible way to *reach* the SSL syscall-error path; HTTP/2 is
what makes that sharing routine. Neither is proven, which is why the first
experiment toggles HTTP/2 while holding the runtime fixed.

---

## 11. Tests and Experiments Already Performed

Nothing below is presented as a production reproduction. Each row states its
environment and scope; none accessed Render, Supabase or Telegram.

| ID | When | Environment | Procedure | Observed result | Scope |
|---|---|---|---|---|---|
| T1 | 2026-09-17 | Source at `2a03813`/`4b2fe47` | Full source audit of the STT timeout and the `local_resource` line | The errno-11 line belongs to the task-repository/Supabase path; it is unrelated to the STT request; the six `[PERMANENT] elapsed=180s` lines are routine task inventory | Establishes attribution only; no runtime measurement |
| T2 | 2026-10-10 | Source at `a54877b` | Trace the premium-emoji path and the ReadError burst | The premium path never calls the DB; the burst is caught inside the repository and degrades to memory | Establishes non-causality for that feature |
| T3 | 2026-10-19 (previous round) | Workspace **CPython 3.10.12 / OpenSSL 3.0.2**; locally generated self-signed TLS pair; untracked scripts (`scratchpad/eagain_probe*.py`, **not present now**) | 11 socket/TLS scenarios around a timed read | Silent peer → `TimeoutError` at 8.008 s; FIN → `b''` 0.301 s; RST → `b''` 0.201 s; own `SSLSocket.close()` while blocked → `b''` at 8.009 s; `os.close(fd)` → `b''` at 8.008 s; close+fd reuse → `TimeoutError` 8.501 s; `settimeout(1e-9)`/`0.001` → `TimeoutError`; `settimeout(0)` TLS read → `SSLWantReadError(errno=2)`; `unwrap()` → `SSLError(errno=1)`; two concurrent readers ×400 → `SSLWantReadError` only; reader+writer ×200 → `TimeoutError` only | Rules out the tested teardown/fd-reuse/non-blocking mechanisms **as direct producers**; cannot rule out the SSL syscall-classification path; all on 3.10 |
| T4 | 2026-10-19 (this round) | Workspace 3.10.12 | Raise `BlockingIOError(11)` through httpcore's own `map_exceptions({socket.timeout: ReadTimeout, OSError: ReadError})` | `httpcore.ReadError` with `__cause__ = BlockingIOError`, errno 11 — the exact chain shape | Confirms the mapping mechanism, not its trigger |
| T5 | 2026-10-19 (this round) | Workspace 3.10.12, `.venv` | `pytest tests/test_task_transport_evidence.py tests/test_task_fallback_classification.py tests/test_task_fallback_cooldown.py tests/test_local_resource_bounds.py tests/test_task_repository.py -q` | **68 passed** (exit 0) | Diagnostic semantics and classifier pins |
| T6 | 2026-10-19 (this round) | same | `pytest tests/test_07_diagnostics.py tests/test_11_runtime_wiring.py tests/test_task_scheduler.py tests/test_task_execution.py tests/test_03_database_consistency.py tests/test_06_failure_simulation.py tests/test_22_reliability.py tests/test_24_execution_reliability.py tests/test_30_delete_timeout_hardening.py -q` | **91 passed** (exit 0) | Runtime wiring and reliability |
| T7 | 2026-10-19 (this round) | same | `pytest tests -q -p no:cacheprovider` | **6398 passed, 27 skipped, 40 failed** (exit 1) in 126.69 s | See below |
| T8 | 2026-10-19 (this round) | `.venv` metadata; GitHub API | Read installed versions; query deployments/statuses | Versions in §7; only preview deployment records, no `main` deployment/status | Inventory, not runtime behaviour |

**T7 failure accounting (not a transport result).** All 40 failures are in
`tests/test_colored_buttons.py`, whose style API requires the **pinned Telethon
1.43.0**; this workspace has **1.34.0**. No transport, task, DB or diagnostics
test failed. The previous round recorded **6438 passed / 0 failed** when the
workspace had 1.43.0; 6398 + 40 = 6438, so the two runs are consistent once the
environment mismatch is accounted for. The earlier round's claim is therefore
**not contradicted**, but it is **not reproducible in this workspace** and is not
reported here as a current pass. No test was skipped, weakened or suppressed.

**What no test has done.** No test or experiment has: (a) run the workload on
CPython 3.13.4 / OpenSSL 3.0.20; (b) reproduced `BlockingIOError(11)`; (c) captured
an OpenSSL error queue or `SSL_get_error` code; (d) measured stream/connection
concurrency at failure; (e) observed the `since_teardown` field in production.

---

## 12. Root-Cause Assessment

### 12.1 Observed symptom (Confirmed)

Intermittent, immediate (`elapsed=0.0s`), read-path `EAGAIN` on the shared
Supabase transport, surfacing as `httpx.ReadError ← httpcore.ReadError ←
BlockingIOError(errno=11)`, on a store that succeeded moments before
(`since_ok=0.0s`).

### 12.2 Confirmed mechanism

1. The failure is a **socket/SSL read** failure: `OSError` → `httpcore.ReadError`
   → `httpx.ReadError`; connect and write phases map to different classes.
2. The diagnostic is emitted **once per episode**; repeated lines are repeated
   failure→success→failure cycles.
3. The dispatch and transport deadlines (10 s / 8 s) are **not** what produced
   this line; a deadline would be `ReadTimeout` (no errno) or `phase=deadline`
   with `elapsed=n/a`.
4. The failure is **not** `ConnectionTerminated`: that is
   `RemoteProtocolError`/`phase=protocol` with no errno.
5. `reason=local_resource` is a **classifier over errno**, not a measurement of
   machine resources; errno 11 on a read is not fd or port exhaustion.
6. HTTP/2 is enabled on this client by `postgrest 0.16.11` (`http2=True`), and
the 4-worker pool shares one client whose TLS socket can be read and written
concurrently by different threads.

### 12.3 Strongly supported (but not proven causal)

* The **deployed runtime is 3.13.4 with OpenSSL 3.0.20**, while `render.yaml`
  declares 3.11.7 and this workspace runs 3.10.12/3.0.2. The 3.13-only
  `PySSL_SetError` change explains why the signature is `BlockingIOError(11)`
  here and why 3.10 experiments could not see it. It explains the **label and the
  local non-reproduction**, not the socket event.
* The service is not governed by the checked-in `render.yaml` (name and runtime
  mismatch, README's manual-creation path, no GitHub deployment record for `main`).
* The store is healthy at the moment of failure.

### 12.4 Suspected root cause (Plausible, medium)

An SSL-layer would-block reported as a fatal `SSL_ERROR_SYSCALL` (rather than
retryable `SSL_ERROR_WANT_READ`), most plausibly reached through concurrent
read/write on the single HTTP/2 TLS socket. This is the only within-stack
explanation that survives the measured negatives, and it is consistent with every
field — but it has not been observed or reproduced on the deployed build.

### 12.5 Root cause: NOT PROVEN

No cause is proven. In particular, **none** of the following may be stated as the
root cause on current evidence: Python 3.13.4, OpenSSL 3.0.20, HTTP/2 by itself,
the shared client by itself, the 4-worker pool, or Render networking. Each is a
candidate or a contributing condition; §10 states what is missing for each.

### 12.6 Operational impact

* AI task operations degrade to a **non-durable in-memory fallback for at least
  5 s** per episode; a failed write stays ambiguous and is never retried.
* Other DB callers log the error and use their own fallbacks; no evidence of data
  corruption or loss has been recorded.
* The failure is observable but its cause is not yet decidable from logs without
  the `since_teardown` field and the deployed dependency versions.

---

## 13. Recommended Next Controlled Experiment

### 13.1 Selection

**Test HTTP/2 alone, on the deployed runtime, in an isolated staging harness.**
Build a container with **CPython 3.13.4 / OpenSSL 3.0.20** and the **frozen
production dependency set** (`pip freeze` from the deployed build, or at minimum
`supabase==2.4.2`, `httpx==0.27.0`, `postgrest==0.16.x`, `httpcore==1.0.9`,
`h2==4.4.1`), then run one fixed concurrent read workload against the Supabase
REST endpoint twice: once with the PostgREST transport's `http2=True` (current
behaviour) and once with `http2=False`. Only that flag changes.

**Why this one first.** It is the only single-variable change that targets a
mechanism family still standing after §11 — concurrent read/write on one TLS
socket under HTTP/2 — and it is the only one whose implementation is a transport
flag rather than a code rewrite. It also removes the `ConnectionTerminated` class
entirely, so the two signatures can be separated. It is cheap, read-only, and reversible. It does
**not** assume the runtime is causal: the runtime is held fixed at the deployed
version precisely so the protocol is the only difference.

**Implementation note (important).** Disabling HTTP/2 is **not** an application
setting: `postgrest 0.16.11` passes `http2=True` unconditionally in its own
`create_session` (`.venv/.../postgrest/_sync/client.py:43-56`), and supabase-py
constructs that client internally. The staging harness must therefore build the
`httpx.Client` explicitly (or override `create_session` in the staging branch).
Do not patch production for this experiment.

### 13.2 Variables

| Category | Value |
|---|---|
| **Changed (one)** | PostgREST/httpx transport `http2`: `True` (A, baseline) vs `False` (B) |
| Fixed | CPython 3.13.4 + OpenSSL 3.0.20; frozen dependency versions; one shared `httpx.Client`; 4 concurrent client threads; 8 s timeout (connect/read/write/pool); TLS verification on; same Supabase URL/region/project; same query set (read-only `select` with `limit`); same request rate schedule |
| Not changed | Application code, production deployment, Supabase schema, Render configuration, env vars, retries, worker counts in production |

### 13.3 Measurements

Capture, per run, from the harness (not from production):

* total operations attempted/succeeded; failures by **class** (`ReadTimeout`,
  `ReadError`, `RemoteProtocolError`, `ConnectError`, `PoolTimeout`);
* for every `OSError`/`ReadError`, the full `__cause__` chain and errno;
* `response.http_version` for a sample (proves ALPN h2 vs HTTP/1.1);
* `ConnectionTerminated` occurrences;
* latency p50/p95/p99 and the time-to-failure distribution;
* process fd count and thread count at 1-minute intervals (host-level context).

### 13.4 Sample size and interpretation

* Run each arm for the **same operation count and duration** (e.g. 6 h / ≥200,000
  operations at 4 threads). If arm A produces fewer than **20** errno-11 events,
  extend both arms to 24 h or raise concurrency in *both* arms; a handful of
events is not interpretable.
* **Supports H3:** arm B produces **0–2** errno-11 events while A produces ≥20
  (Poisson p < 0.01), with no new failure class in B.
* **Weakens H3:** B's rate is within ~2× of A's, or B introduces a different
  error class at a similar rate. Then the protocol is not the discriminator and
  A1/A3 (§14) become the priority.
* **Inconclusive:** neither arm produces events. That outcome is itself valuable:
  it means the load/endpoint does not recreate the production condition and the
  experiment must move to the real staging service (A4) rather than a harness.

### 13.5 Rollback and risks

* **Rollback:** nothing in production is touched; delete the harness. If the
  experiment is later repeated in a staging *service*, revert the one staging
  patch and redeploy staging.
* **Risks:** PostgREST/edge rate limits and project quota; read-only queries only
  (no writes, therefore no ambiguous-completion risk); HTTP/1.1 arm has lower
  throughput per connection, so keep the request schedule identical rather than
  chasing equal throughput; do not add retries (a retried write would be
  ambiguous).
* **Do not** combine with a Python-version change. The runtime must stay 3.13.4
  for this experiment; otherwise the result cannot be attributed to HTTP/2.

---

## 14. Alternative Experiments

Ordered by diagnostic value after (§13). Each isolates one variable.

| ID | Test | Hypothesis tested | Prediction if true | Cost / risk |
|---|---|---|---|---|
| **A1** | Run the same harness on **CPython 3.11.7** with HTTP/2 fixed **on**, vs the 3.13.4 baseline | H1 (classification) | The same syscall event appears as `SSLEOFError(errno=8)` and `reason=unavailable`, not `BlockingIOError(11)`/`local_resource` | Low; separate container; proves the label change, not the trigger |
| **A2** | Same harness on 3.13.4, HTTP/2 fixed on, **1 thread vs 4 threads** | H3 (concurrency) | errno 11 appears only with concurrent streams; 1 thread produces none | Low; throughput reduced in the 1-thread arm |
| **A3** | Deterministic BIO-level harness: `ssl.SSLObject` + `MemoryBIO`, drive a read whose BIO returns `EAGAIN`, compare 3.13.4/3.0.20 vs 3.10.12/3.0.2 | H1 + H2 | 3.13 raises `BlockingIOError(11)`; 3.10 raises `SSLEOFError`. Establishes the conversion; **cannot** establish why production's BIO returns EAGAIN | Medium (custom harness) |
| **A4** | Instrument the **staging app** (not production): log `response.http_version`, and a per-request connection id via an httpcore trace hook; replay normal traffic | H3/H4 | Failures cluster on one h2 connection or on concurrent streams | Medium; staging-only code, not shipped |
| **A5** | Read the next production episode's `since_teardown` (already deployed in `8e0e401`) | H4 (teardown coupling) | `none` → no observed teardown; `0.x s` → coupling; `hours` → coincidence | Free; no change; requires waiting for an episode |
| **A6** | One host sample at the episode minute: fd count, sockets in `CLOSE_WAIT`/`TIME_WAIT`, `lifeos-supabase-*` thread count, RSS | H7 (local resources) | Stable counts rule out host exhaustion; a spike supports pressure | Low; read-only; needs Render shell/log access the current workspace lacks |
| **A7** | Request Render metrics (egress, CPU throttling, packet loss) for the episode windows; ask Render support whether the instance uses a proxy/NAT that can return `EAGAIN` | H8 (platform) | Platform-side anomaly correlated with episodes | External; owner action |
| **A8** | Wire capture (`tcpdump` + `SSLKEYLOGFILE`) in staging during a reproduction | H2/H3 | Shows truncated record, RST, or read timing at the TLS layer | High sensitivity; staging only; never production without explicit approval |

**A5 is free and should run in parallel with any experiment.** It is the only one
that uses the instrumented production build rather than a reproduction.

---

## 15. Evidence Gaps and Access Limitations

| Gap | Why it matters | Exact next method |
|---|---|---|
| **No Render access in this workspace** (no CLI, no API credential, no log stream, no metrics) | Production logs, service env vars, build command, health path, deploy commit and host counters cannot be verified | Use the Render dashboard/API from an authorized session, or ask the owner to paste the full `TASK_FALLBACK_CLASSIFIED` lines and the client-init `runtime=... ssl=...` line |
| **Deployed transitive versions unknown** (no lockfile for `postgrest`/`httpcore`/`h2`/`h11`/`anyio`) | Conclusions about `http2=True` and error mapping are verified only for the local versions; a different resolved `postgrest` could change transport behaviour | Capture the build log's pip resolution, or add a one-off startup log of `importlib.metadata.version(...)` for those packages (documentation/observability only) |
| **Exact event timestamps and rates unknown** | The 0.0–0.1 s and recurrence claims come from owner excerpts; no per-interval counts or correlation windows exist | Export the Render log window containing `TASK_FALLBACK_CLASSIFIED` and `ConnectionTerminated`; compute deltas between each pair |
| **`since_teardown` not yet observed in production** | It is the only field that can settle H4 | Wait for the next episode on a build ≥ `8e0e401`; record the full line verbatim |
| **No ALPN/connection/stream identity** | HTTP/2 use and per-connection clustering are inferred | A4 (staging instrumentation) or an httpcore trace hook in staging |
| **No OpenSSL error queue / `SSL_get_error` code** | H2 cannot be confirmed without the SSL-level error classification | A3 or a C-level `SSL_get_error` probe in a controlled harness; A8 as a fallback |
| **Workspace is 3.10.12; no 3.13 interpreter installed** | The deployed classification path cannot be reproduced here | Container/venv with CPython 3.13.4 and OpenSSL 3.0.20 (A1/A3) |
| **Prior probe scripts are absent** (`scratchpad/eagain_probe*.py` were untracked and are gone) | T3 cannot be re-run byte-for-byte | Recreate the matrix from the procedure in §11 and §17.4; record commands and outputs this time |
| **No host-level metrics** (fds, threads, memory, socket states) | H7 cannot be excluded at the host level even though errno 11 is not fd exhaustion | A6 |
| **`ai_last_db_s` is unusable** (`mark_success("DB_OPERATION")` never called) | No cheap success-age signal outside the task repository | Treat as a separate observability defect; do not change it as part of this investigation |

---

## 16. Changes Made and Not Made

### 16.1 This task

| Area | Changed? | Evidence |
|---|---|---|
| `INVESTIGATION.md` | **Yes — replaced in full** | This document |
| Application code | **No** | No edits to `backend/**` |
| Dependencies / pins | **No** | `backend/requirements.txt` untouched |
| Environment variables / secrets | **No** | Nothing read, written or exported |
| Supabase schema / SQL / migrations | **No** | No SQL executed; `supabase/**` untouched |
| Render configuration / service settings | **No** | No API call, dashboard action or deployment |
| Deployment / restart | **No** | No deploy, restart or shell |
| Telegram | **No** | No request of any kind |
| `DATABASE_ARCHITECTURE.md` or other docs | **No** | Untouched |
| Pre-existing local work | **Preserved** | Local `main` (`d084e6b`, one unpushed AED commit) was not reset, rebased, merged or force-pushed; the delivery branch was created from verified `origin/main` |

### 16.2 Already present in the repository (not added by this task)

For the record, the diagnostic fields discussed above were added by earlier
rounds and remain as delivered: `_timed_db_call` + `elapsed` (`c9e5203`),
`chain`/`phase`/`since_ok` (`c9e5203`), the runtime identity line and
`since_teardown` (`8e0e401`), plus the teardown recorder. This task **documents**
them; it does not change them.

---

## 17. Reproduction and Handoff Instructions

### 17.1 Read the current state

```bash
git fetch origin
git checkout 33d55d12f5e6c6413d2ccda075984149ac639862   # or: git checkout origin/main
sed -n '60,260p' backend/db/client.py            # timeouts, pool, _timed_db_call, teardown
grep -n 'phase\|chain\|since_ok\|since_teardown' backend/ai/database/task_repository.py
```

Verify the transport configuration in the **installed** dependency (paths are
environment-specific):

```bash
grep -n 'http2' .venv/lib/python*/site-packages/postgrest/_sync/client.py
grep -n 'OSError\|socket.timeout' .venv/lib/python*/site-packages/httpcore/_backends/sync.py
.venv/bin/python -c 'import httpx,httpcore,h2,postgrest;print(httpx.__version__,httpcore.__version__,h2.__version__,postgrest.__version__)'
```

### 17.2 Reproduce the exception mapping (no network)

```bash
.venv/bin/python - <<'PY'
import errno, socket
from httpcore._exceptions import map_exceptions, ReadError, ReadTimeout
try:
    with map_exceptions({socket.timeout: ReadTimeout, OSError: ReadError}):
        raise BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable")
except BaseException as exc:
    print(type(exc).__name__, exc, "cause=", type(exc.__cause__).__name__, exc.__cause__.errno)
PY
```

Expected: `ReadError [Errno 11] ... cause=BlockingIOError 11`. This confirms the
shape only.

### 17.3 Run the transport suites

```bash
.venv/bin/python -m pytest tests/test_task_transport_evidence.py \
  tests/test_task_fallback_classification.py tests/test_task_fallback_cooldown.py \
  tests/test_local_resource_bounds.py tests/test_task_repository.py -q
```

Expected on this tree: **68 passed**. The full suite additionally requires
Telethon **1.43.0** (this workspace has 1.34.0, which fails the 40
`test_colored_buttons.py` cases; that is an environment mismatch, not a transport
regression).

### 17.4 Rebuild the probe matrix (do this on both runtimes)

Create a local self-signed TLS server and a client that blocks in
`SSLSocket.recv`, then reproduce these conditions and record the exact exception,
errno and elapsed time:

| Scenario | Expected on 3.10/3.0.2 (previous round) | Record on 3.13.4/3.0.20 |
|---|---|---|
| silent peer, `settimeout(8)` | `TimeoutError(errno=None)` | |
| peer FIN while blocked | `b''` | |
| peer RST (SO_LINGER 0) | `b''` | |
| our own `close()` while blocked | `b''` (read not interrupted) | |
| `os.close(fd)` while blocked | `b''` | |
| close + fd reuse | `TimeoutError` | |
| `settimeout(0)` TLS read | `SSLWantReadError(errno=2)` | |
| `unwrap()` then read | `SSLError(errno=1)` | |
| two concurrent readers | `SSLWantReadError` only | |
| concurrent reader + writer | `TimeoutError` only | |

The critical missing cell is **a would-block that OpenSSL classifies as
`SSL_ERROR_SYSCALL`**; the matrix above cannot force it. Record whether 3.13
turns any of these into `BlockingIOError(11)` (prediction: it should not, unless
the SSL error-queue state differs).

### 17.5 Capture the next production episode

When the owner sees an episode, capture **verbatim and with a UTC timestamp**:

1. The full `TASK_FALLBACK_CLASSIFIED` line (all fields).
2. The nearest `[SAVE_DB] Supabase client initialised. runtime=python-... ssl=...`
   line from process start (this identifies the deployed build).
3. Any `ConnectionTerminated`, `RemoteProtocolError`, `OP_TIMEOUT`, or
   `ReadTimeout` lines within ±60 s, with timestamps.
4. Whether the process restarted between the episode and the previous success.

Decision table (mechanical, no interpretation needed):

| Observation | Conclusion | Next action |
|---|---|---|
| `phase=read elapsed=0.0s since_teardown=none` | No observed teardown coupling; immediate read refusal | Prioritise A1/A2/A3 (runtime classification + concurrency) |
| `phase=read elapsed=0.0s since_teardown=0.x–few s` | Temporal coupling with a peer/HTTP-2 termination | A4/A8; design the smallest coupling-removal change (never a blind retry) |
| `phase=read elapsed≈8s` | The deadline is being surfaced as a would-block | Host/edge latency investigation (A6/A7) |
| `phase=deadline` / `elapsed=n/a` | Watchdog abandonment; the thread is still blocked | Revisit the dispatch budget with measurements |
| `phase=write` | Write phase; completion is ambiguous | Do **not** retry; keep the non-durable label |
| `chain=...SSLEOFError(errno=8)` on a 3.11 build | Classification hypothesis (H1) confirmed | Fix the runtime/classification mismatch, then re-test |
| `chain=...BlockingIOError(errno=24/23)` | Real fd exhaustion | Host-level resource work, not transport work |

### 17.6 Verify the delivered document

```bash
git fetch origin
git rev-parse origin/main
git show origin/main:INVESTIGATION.md | sha256sum
sha256sum INVESTIGATION.md
```

The two hashes must match, and `git merge-base --is-ancestor <delivery-sha>
origin/main` must exit 0.

---

## 18. Final Conclusion

The recurring failure is **confirmed** to be an immediate, read-path socket/SSL
`EAGAIN` on the shared Supabase transport (HTTP/2 enabled), surfaced through
`httpcore.ReadError`/`httpx.ReadError` and classified as `local_resource` by a
heuristic over errno. It is **not** a deadline, not saturation, not an outage,
not `ConnectionTerminated`, and not evidence of machine-wide resource
exhaustion. The store is healthy immediately before each episode, and the
repository degrades safely to its in-memory fallback with no retry of ambiguous
writes.

The **root cause is not proven**. The strongest candidate is an SSL-layer
would-block reported as a fatal `SSL_ERROR_SYSCALL` rather than retryable
`SSL_ERROR_WANT_READ`, plausibly reached through concurrent read/write on the
single HTTP/2 TLS socket. The deployed runtime (CPython 3.13.4 / OpenSSL 3.0.20)
is **strongly supported** as the reason this appears as `BlockingIOError(11)` —
it changes the error classification, not the underlying event — and it explains
why the workspace's 3.10.12 experiments never reproduced the signature. The
`render.yaml` pin of 3.11.7 does not govern the live service; that drift is
confirmed but not causal by itself.

The single next step is the HTTP/2 on/off experiment on the deployed runtime
(§13), with the next production episode's `since_teardown` field read in
parallel (§17.5). Until one of those produces discriminating evidence, no
further code change is justified.

