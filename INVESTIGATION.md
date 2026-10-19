# INVESTIGATION.md — Recurring Supabase `ReadError` / `BlockingIOError(errno=11)`

This document is the **complete current investigation** of the recurring Supabase
transport failure. It replaces every previous investigation in full; earlier
rounds remain readable in Git history and are cited only where their measurements
still stand. It records the evidence that justified the transport change now on
`main` (`backend/db/transport.py`), what that change does and does not establish,
and what would confirm it in production.

---

## 1. Executive Summary

**What was established.** Every Supabase call in this process goes through one
shared synchronous `httpx` client, and that client was forced to speak **HTTP/2**
by the pinned `postgrest` library (`http2=True` is hard-coded). HTTP/2 multiplexes
all concurrent requests onto **one** TLS connection, and `httpcore` guards that
connection with two *independent* locks over h2 state that is not thread-safe.
The database layer dispatches up to **four** worker threads through that one
client. Under concurrent load this configuration produced immediate transport
failures (milliseconds, not deadlines) in every measurement taken — on a local
TLS server and on an independent production HTTP/2 origin — while the same load
over **HTTP/1.1**, with **one client per thread**, or with **one thread** produced
none.

**What was done.** The shared client is now constructed on an HTTP/1.1-only
PostgREST transport. The pin is installed once, when the `backend.db` package
loads, on the public `supabase.create_client` factory that `get_db()` already
calls; `get_db()` itself is unchanged. Nothing else moved: same single client,
same bounded four-worker pool, same 8 s transport deadline below the 10 s
dispatch budget, same headers/timeout/redirect/verify behaviour, same PostgREST
request path, same fallback, cooldown, classification and no-retry semantics.

**Confidence.** The *failure mechanism family* (concurrent use of one multiplexed
connection under HTTP/2) is **Confirmed** by source and by reproducible
measurement. The *root cause of the errno itself* is **not proven**: the exact
production signature `ReadError <- ReadError <- BlockingIOError(errno=11)` was
**never reproduced locally** — local failures surfaced as `RemoteProtocolError`
and h2 state errors. The fix targets the configuration in which the failure is
reproducible and removes it; it does not claim to have explained the final
syscall.

**Verification.** Implemented and validated locally only (10 new regression
tests; 147 focused tests; 6447 of 6448 active tests, the one failure being a
pre-existing wall-clock-dependent panel test — §11). The deployed Render service
was **not** observed after the change (§12).

---

## 2. Metadata

| Item | Value |
|---|---|
| Report date | **2026-10-19 (UTC)** |
| Repository | `Onlyicing1/Telegram-self-bot` (`origin`) |
| Target branch | `main` (delivery target `origin/main`) |
| Starting commit | **`13f9a1cdade1668a72ef5e69b85b1eb571503ff9`** |
| Final commit | The commit that carries this file on `main`; resolve with `git log -1 --format=%H -- INVESTIGATION.md`. The exact SHA is stated in the completion response. |
| Workspace runtime | CPython **3.12.3**, OpenSSL **3.0.13** |
| Installed transport | `supabase 2.4.2`, `postgrest 0.16.11`, `httpx 0.27.0`, `httpcore 1.0.9`, `h2 4.4.1`, `h11 0.16.0`; `telethon 1.43.0` |
| Live Render / Supabase / Telegram | **Not accessed in this round.** No deployment, no Render shell/API/log stream, no `.env`, no credential, no SQL, no migration, no restart, no environment change. |

---

## 3. The Failure Being Explained

```text
TASK_FALLBACK_CLASSIFIED reason=local_resource phase=read exception=ReadError
  chain=ReadError<ReadError<BlockingIOError(errno=11) elapsed=0.0s since_ok=0.0s
```

| Property | Status | Basis |
|---|---|---|
| Read phase, not connect/write/pool | **Confirmed** | `httpcore` maps `OSError → ReadError` only in `SyncStream.read`; connect/write map to `ConnectError`/`WriteError` |
| Immediate, not a deadline | **Confirmed** | `elapsed=0.0s`; a deadline is `ReadTimeout` (no errno) or `phase=deadline` with `elapsed=n/a` |
| Not a store outage | **Confirmed** | `since_ok=0.0s`; no HTTP status or PostgREST code in the chain |
| Not `ConnectionTerminated` | **Confirmed** | That is `RemoteProtocolError`, `phase=protocol`, no errno |
| `reason=local_resource` | **A classifier over errno**, not a measurement of machine resources | `task_repository._is_local_resource_failure` |
| Recurs in bursts on a healthy store | Historical observation | Production excerpts recorded in earlier rounds |

---

## 4. The Verified Transport Path

### 4.1 Dispatch

```text
async caller
  └─ run_sync_db(fn, timeout=10)                      backend/db/client.py
      └─ loop.run_in_executor(_get_db_executor(), _timed_db_call, ...)
            pool: ThreadPoolExecutor(max_workers=4, prefix="lifeos-supabase")
            inside the worker: fn(...) → get_db().table(...)....execute()
```

`_DB_MAX_WORKERS = 4`, `_DB_HTTP_TIMEOUT = 8.0`, `_DB_TIMEOUT = 10.0`
(`0 < 8.0 < 10.0`, test-pinned). `_timed_db_call` measures in-flight time inside
the worker; nothing retries, delays or re-raises a different object.

### 4.2 The stack (before this change)

```text
supabase 2.4.2  SyncClient
  └─ SyncPostgrestClient(timeout=8.0)
      └─ httpx.Client(http2=True, timeout=8.0)         ← hard-coded by postgrest
          └─ httpcore.ConnectionPool
              └─ HTTP2Connection  (when ALPN negotiated h2)
                   ├─ _write_lock → _write_outgoing_data → socket write
                   └─ _read_lock  → _receive_events      → socket read
```

| Fact | Source |
|---|---|
| `http2=True` is unconditional in PostgREST's session factory | `postgrest/_sync/client.py::create_session` |
| supabase-py builds that PostgREST client internally, lazily, and rebuilds it on an auth-state change | `supabase/_sync/client.py` (`postgrest` property, `_init_postgrest_client`, `_listen_to_auth_events`) |
| h2 is offered in ALPN only when the pool's `http2` flag is set, and `HTTP2Connection` is used only when h2 was negotiated | `httpcore/_sync/connection.py` |
| Reads and writes use **separate** locks while `_events` is mutated without one | `httpcore/_sync/http2.py` |
| A read `OSError` becomes `ReadError`; httpx mirrors it | `httpcore/_backends/sync.py`, `httpx/_transports/default.py` |

### 4.3 Why this configuration fails

With HTTP/2 every concurrent request of the shared client occupies a **stream**
on one TLS socket. Four worker threads therefore read and write one socket
concurrently, under locks that do not cover the shared h2 state. When that state
is stepped on, the connection is torn down while other requests are mid-flight,
and the affected requests fail **immediately** — the observed `elapsed≈0` shape.
HTTP/1.1 removes the condition rather than the symptom: a connection is checked
out to exactly one request at a time, so no socket is ever read and written by
two threads.

---

## 5. Measurements That Decided the Fix

Taken in the earlier round of this investigation and preserved with their
original context: environment **CPython 3.10.12 / OpenSSL 3.0.2**, the real
`postgrest` sync path. They are **not** re-run here and are not claimed as
current measurements.

| Configuration | Result |
|---|---|
| One shared client, **HTTP/2**, 4 threads | **99 failures / 1,463 OK** |
| One shared client, **HTTP/1.1**, 4 threads | **0 / 4,947** (longer 30 s run: **0 / 23,518**) |
| **One client per thread**, HTTP/2 | **0 / 4,594** |
| One shared client, HTTP/2, **1 thread** | **0 / 1,648** |
| Real production HTTP/2 origin (Cloudflare) | **99 failures at 1.7–2.9 ms** |
| Same origin over HTTP/1.1 | **0 / 6,258** |
| Negative control: bare `SyncStream` read+write on one TLS socket | 3,168 reads / 9,445 writes, **0** errors |

**Established:** the failure belongs to *shared HTTP/2 multiplexing under
concurrency*; it is not caused by the test server, by rate limiting, or by a mere
socket read/write race.

**Not established:** `errno=11`. Local failures were `RemoteProtocolError` and h2
state errors (`Trailers must have END_STREAM set.`, `KeyError` on `_events`).
Which errno surfaces depends on where in the socket layer the teardown lands, and
the deployed interpreter (previously observed as CPython 3.13.4 / OpenSSL 3.0.20)
was not available here.

---

## 6. Alternatives Considered

| Option | Why not chosen |
|---|---|
| **HTTP/1.1 for the shared client** *(chosen)* | The only configuration measured clean with the existing pool, interfaces and semantics; one transport flag and nothing else |
| One client per thread | Also measured clean, but requires per-thread state for every caller (including `asyncio.to_thread` users outside the pool), multiplies pools and sockets, and changes the client lifecycle |
| Serialize DB calls behind one lock | Removes the concurrency the application relies on; couples every caller to a new lock with no evidence that serialization is needed |
| Retry on `ReadError` | An ambiguous write would be issued twice; the repository already treats a failed write as ambiguous |
| Change timeouts | The failure is immediate (`elapsed=0.0s`); no timeout participated |
| Pin/upgrade Python or OpenSSL | May explain *which errno* is raised, not why the socket refuses a read; a deployment risk that is not needed to remove the measured configuration |
| Upgrade `postgrest`/`httpcore` | Unproven that any available version fixes it, and unpinned upgrades are out of scope |
| Swap `postgrest.session` after construction | Public attributes only, but supabase-py rebuilds its PostgREST client on an auth-state change, silently restoring `http2=True` |

---

## 7. The Implemented Fix

**New `backend/db/transport.py`**

* `HTTP1Session(httpx.Client)` — PostgREST's session with `http2=False`, plus
  `aclose()` (postgrest closes its session through that name; httpx's sync client
  only defines `close()`).
* `HTTP1PostgrestClient(SyncPostgrestClient)` — overrides `create_session` to
  build that session with the same base URL, headers, timeout, `verify` and
  `follow_redirects=True`.
* `HTTP1SupabaseClient(SupabaseClient)` — overrides the PostgREST factory, so the
  pin survives the library's lazy construction **and** its rebuild on an
  auth-state change.
* `create_http1_client(url, key, options=None)` — a drop-in for
  `supabase.create_client`: same validation, auth headers and sub-clients, only
  the transport differs. It logs `Supabase transport=http/1.1 (HTTP/2 disabled)`
  so a deployed build states its transport.
* `install_http1_transport()` — installs that factory on the public
  `supabase.create_client` entry point (idempotent).

**Changed `backend/db/__init__.py`** — imports `transport` and calls
`install_http1_transport()`; a missing dependency is swallowed there because
`get_db()` already falls back.

**Why the factory install rather than an edit in `get_db()`.** `get_db()` is the
single construction call site and it imports `create_client` from the `supabase`
package *at call time*, so installing the public factory there keeps one
construction boundary instead of duplicating it. `backend/db/client.py` is
therefore **unchanged** — its worker pool, deadlines, diagnostics and fallback
logic are exactly as delivered before. The pin is a public-function replacement,
not a patch of a private internal, and a regression test asserts it is installed.

| Behaviour | Before | After |
|---|---|---|
| Transport | HTTP/2 (hard-coded by postgrest) | **HTTP/1.1 only** (`http2=False`; ALPN offers only `http/1.1`) |
| Concurrent sockets | All requests multiplexed on one TLS connection | One pooled connection per in-flight request |
| Client / pool / deadlines / diagnostics / fallback | — | **unchanged** |
| Headers, timeout, redirects, verification | — | **unchanged** |
| PostgREST request path, response parsing | — | **unchanged** |
| Error classification, cooldown, no retry | — | **unchanged** |

---

## 8. Regression Tests

`tests/test_supabase_transport_http1.py` (10 tests; real client, real PostgREST,
real `httpcore` pool, real local HTTP server — no mocked transport):

1. construction path is HTTP/1.1 only (`_http2 is False`, `_http1 is True`;
   timeout, redirects, base URL, `Authorization`/`apiKey`/`Accept-Profile`
   unchanged);
2. the factory is installed and `get_db()` — with its own unmodified call —
   returns an HTTP/1.1 client;
3. the stock library client still reports `_http2 is True`, so (1) is
   discriminating;
4. the pin survives a PostgREST client rebuild (the auth-state path);
5. a real request travels and parses (`/rest/v1/saved_items?select=%2A…`,
   `apikey`, `Accept-Profile`);
6. 4 threads × 25 real requests on one client → 100 responses, 0 exceptions;
7. a peer that closes without responding raises (never a success);
8. the same failure through `SupabaseTaskRepository` degrades and is classified
   `unavailable` (no false success, no local-resource claim);
9. missing credentials still fall back;
10. `_DB_MAX_WORKERS == 4`, `0 < 8.0 < 10.0`, executor size 4.

No existing test was modified: the two suites that patch
`supabase.create_client` still intercept, because that is exactly the installed
factory.

---

## 9. What the Fix Does Not Claim

* `errno=11` was **not** reproduced, and is not claimed.
* The SSL-layer reason the original read was refused is **not** known.
* Production is **not** claimed fixed: no deployment or live call was observed
  in this round.
* Classification, timeouts, retries, pools, schema, secrets, dependencies and
  deployment configuration are untouched. `render.yaml` still pins
  `PYTHON_VERSION=3.11.7` while live logs previously showed 3.13.4 — a separate
  drift item, deliberately not bundled here.

---

## 10. Reproduction

```bash
.venv/bin/python -c "
import supabase
from backend.db import transport
from supabase import ClientOptions
c = transport.create_http1_client('https://example.supabase.co','aaa.bbb.ccc',ClientOptions(postgrest_client_timeout=8.0))
s = c.postgrest.session
print(supabase.create_client is transport.create_http1_client, type(s).__name__, s._transport._pool._http1, s._transport._pool._http2)"
```

Expected: `True HTTP1Session True False`.

---

## 11. Verification Results (this workspace, 2026-10-19)

| Check | Command | Result |
|---|---|---|
| New regression file | `pytest tests/test_supabase_transport_http1.py -q` | **10 passed** |
| Focused transport/DB/runtime set (10 files) | see §11.1 | **147 passed** |
| Full suite | `pytest tests -q -p no:cacheprovider` | **6447 passed, 27 skipped, 1 failed** |
| Compilation | `python -m py_compile` on the changed Python files | clean |
| Whitespace | `git diff --check` | clean |

### 11.1 The one failing test

`tests/test_private_message_panel.py::test_the_schedule_panel_names_the_next_transition`
asserts the panel says `rule 1 start` for a rule of **22:00–23:00 UTC**; runs at
22:02 and 22:12 UTC (inside that window) see the rule's 23:00 *end* as the next
transition. It is wall-clock dependent and **fails on the clean tree as well**
(verified by stashing this change and re-running it: `1 failed`), imports only
the private-message service/panel modules, and is reported as a failure rather
than a pass. It is not fixed here because it is unrelated.

---

## 12. Confirming the Fix in Production

1. The build's log should contain `Supabase transport=http/1.1 (HTTP/2 disabled)`
   in addition to the existing `[SAVE_DB] Supabase client initialised` line.
2. If an episode still occurs, `phase` / `elapsed` / `chain` / `since_teardown`
   in `TASK_FALLBACK_CLASSIFIED` remain the deciders.
3. `ConnectionTerminated error_code:1` should no longer be produced by this
   client, because h2 is never negotiated.

**Not verified:** the deployed commit, the deployment's terminal status, startup
logs, and whether production episodes stop.

---

## 13. Remaining Unknowns

| Unknown | How to close it |
|---|---|
| The syscall-level reason `EAGAIN` was returned | Deployed-runtime capture (OpenSSL error queue / `SSL_get_error`) |
| Whether production behaves differently on 3.13.4 / OpenSSL 3.0.20 | Post-fix production observation |
| Deployed transitive versions (`postgrest`/`httpcore`/`h2`) | The build log's pip resolution |
| `render.yaml` (`3.11.7`) vs live runtime | Reconcile separately; not bundled here |

---

## 14. Changes Made / Not Made

| Area | Changed |
|---|---|
| `backend/db/transport.py` | **Added** — HTTP/1.1 client + install hook |
| `backend/db/__init__.py` | **Changed** — installs the factory on import |
| `tests/test_supabase_transport_http1.py` | **Added** — 10 regression tests |
| `backend/db/client.py` | **Unchanged** |
| Existing tests | **Unchanged** |
| `INVESTIGATION.md`, `IMPLEMENTATION_REPORT.md` | **Replaced in full** |
| Dependencies, `render.yaml`, secrets, Supabase schema/SQL, `DATABASE_ARCHITECTURE.md` | **Unchanged** |
