# IMPLEMENTATION_REPORT.md — Supabase transport: HTTP/1.1 for the shared client

> This file describes **only** the current state of the repository for this
> round and **replaces every previous report in full**. Earlier reports (the AED
> archive rounds, the private-message rounds, the lifecycle-stall round, the
> Supabase EAGAIN investigation rounds) remain readable in Git history.

| Item | Value |
|---|---|
| Stage | **Implementation** (fix + regression tests + reports), delivered on `main` |
| Date | 2026-10-19 (UTC) |
| Repository / remote | `Onlyicing1/Telegram-self-bot`, branch `main` |
| Revision at task start | `13f9a1cdade1668a72ef5e69b85b1eb571503ff9` (`origin/main`, verified with `git fetch` + `rev-parse` before any edit) |
| Final commit | The commit that carries this file on `main`; resolve with `git log -1 --format=%H -- IMPLEMENTATION_REPORT.md`. The exact SHA, the push result and the remote verification are stated in the completion response. |
| Workspace runtime | CPython **3.12.3**, OpenSSL **3.0.13**; `supabase 2.4.2`, `postgrest 0.16.11`, `httpx 0.27.0`, `httpcore 1.0.9`, `h2 4.4.1`, `telethon 1.43.0` |
| Live Render / Supabase / Telegram | **Not touched:** no deployment, no Render API/shell/log stream, no `.env`, no credential, no SQL, no migration, no restart, no environment change |

---

## 1. Objective

Stop the recurring production transport failure

```text
ReadError <- ReadError <- BlockingIOError(errno=11)   (elapsed=0.0s)
```

by removing the configuration in which it is reproducible — concurrent use of one
HTTP/2-multiplexed socket by the shared Supabase client's worker threads — with
the smallest safe change, regression tests, no retries, and no change to
timeouts, dependencies, schema, secrets or deployment configuration.

---

## 2. Root cause, with its confidence

**Confirmed (source + measurement).** The shared Supabase client spoke HTTP/2
because `postgrest`'s session factory hard-codes `http2=True`
(`postgrest/_sync/client.py::create_session`), and supabase-py constructs that
client internally. HTTP/2 multiplexes all concurrent requests onto one TLS
connection; `httpcore` reads and writes it under two *separate* locks
(`_read_lock`, `_write_lock`) over h2 state whose `_events` bookkeeping is
mutated without a lock. `backend/db/client.py` dispatches up to `_DB_MAX_WORKERS
= 4` threads through that one client.

Measured on this stack (earlier round, CPython 3.10.12 / OpenSSL 3.0.2): shared
client + HTTP/2 + 4 threads → 99 failures / 1,463; shared client + HTTP/1.1 + 4
threads → 0 / 4,947; per-thread clients + HTTP/2 → 0 / 4,594; one thread + HTTP/2
→ 0 / 1,648. Reproduced against a real production HTTP/2 origin (99 failures at
1.7–2.9 ms) and clean over HTTP/1.1 (0 / 6,258) on the same origin.

**Not proven.** Why the socket layer returns `EAGAIN` at all. The exact
production chain (`BlockingIOError(errno=11)`) was never reproduced locally —
local failures were `RemoteProtocolError` and h2 state errors. The fix removes
the reproducible condition; it does not claim to explain the final syscall, and
no retry or masking was added.

---

## 3. The change

### 3.1 New: `backend/db/transport.py`

| Symbol | Role |
|---|---|
| `HTTP1Session(httpx.Client)` | PostgREST's session with `http2=False`, plus `aclose()` (postgrest closes its session through that name; httpx's sync client only defines `close()`) |
| `HTTP1PostgrestClient(SyncPostgrestClient)` | Overrides `create_session` to build that session with the same base URL, headers, timeout, `verify` and `follow_redirects=True` |
| `HTTP1SupabaseClient(SupabaseClient)` | Overrides the PostgREST factory, so the pin survives the library's lazy construction **and** its rebuild on an auth-state change |
| `create_http1_client(url, key, options=None)` | Drop-in for `supabase.create_client`: same validation, auth headers, sub-clients; only the transport differs. Logs `Supabase transport=http/1.1 (HTTP/2 disabled)` |
| `install_http1_transport()` | Installs that factory on the public `supabase.create_client` entry point (idempotent) |

`http2=False` is the decisive flag: `httpcore` builds its ALPN list as
`["http/1.1", "h2"] if http2 else ["http/1.1"]` and only uses `HTTP2Connection`
when h2 was negotiated, so this session cannot multiplex.

### 3.2 Changed: `backend/db/__init__.py`

Imports `transport` and calls `install_http1_transport()`. A missing transport
dependency is swallowed (with a warning), because `get_db()` already degrades to
the in-memory fallback when it cannot build a client.

### 3.3 Why the factory install, and not an edit in `get_db()`

`get_db()` is the single construction call site, and it imports `create_client`
from the `supabase` package **at call time**. Installing the public factory at the
one place every `backend.db.*` import passes through therefore keeps a single
construction boundary — and leaves `backend/db/client.py` (3,180 lines of
dispatch, pool, deadlines and diagnostics) completely untouched. The install is a
replacement of a **public** function, not a patch of a private internal, it is
idempotent, and a regression test asserts it is in place. The alternative —
editing the call inside `client.py` — would have duplicated the construction
boundary and changed a large file for a one-line change.

### 3.4 Old vs new behaviour

| Aspect | Before | After |
|---|---|---|
| Transport | HTTP/2 (hard-coded, unavoidable from this repo) | HTTP/1.1 only |
| Sockets per concurrency | one multiplexed TLS connection | one pooled connection per in-flight request |
| Client, worker pool (`4`), transport deadline (`8 s`), dispatch budget (`10 s`) | — | **unchanged** |
| Headers, timeout object, redirects, verification | — | **unchanged** |
| PostgREST request path and response parsing | — | **unchanged** |
| Classification, fallback, 5 s cooldown, no retry | — | **unchanged** |
| `get_db()`, `run_sync_db()`, service interfaces | — | **unchanged** |

---

## 4. Tests

`tests/test_supabase_transport_http1.py` (new, 10 tests) — real `supabase` client,
real `postgrest` client, real `httpcore` pool, real local HTTP server and real
sockets; no mocked transport, no credentials:

* construction path is HTTP/1.1 only (`_http2 is False`, `_http1 is True`);
* the factory is installed and `get_db()`, with its own unmodified call, returns
  an HTTP/1.1 client;
* the stock library client still reports `_http2 is True` — the assertion is
  discriminating;
* the pin survives a PostgREST client rebuild;
* a real request travels and parses (`/rest/v1/...`, `apikey`,
  `Accept-Profile`);
* 4 threads × 25 requests on one client → 100 responses, 0 exceptions;
* a real transport failure raises (never a false success), and through
  `SupabaseTaskRepository` it degrades and is classified `unavailable`;
* missing credentials still fall back; pool size and timeout relationship
  unchanged.

**No existing test was modified.** The two suites that patch
`supabase.create_client` still intercept, because that is exactly the installed
factory.

---

## 5. Validation results (actual)

| Check | Result |
|---|---|
| `pytest tests/test_supabase_transport_http1.py -q` | **10 passed** |
| `pytest` on the 10 focused transport/DB/runtime files | **147 passed** |
| `pytest tests -q -p no:cacheprovider` | **6447 passed, 27 skipped, 1 failed** |
| `python -m py_compile` on the changed Python files | clean |
| `git diff --check` | clean |

**The single failure** is
`tests/test_private_message_panel.py::test_the_schedule_panel_names_the_next_transition`.
It asserts the panel says `rule 1 start` for a rule of 22:00–23:00 UTC; the run
was at 22:12 UTC, inside that window, where the next transition is the rule's
end. Verified to fail **on the clean tree too** (this change stashed, same
result), it imports only the private-message service/panel modules, and it is
reported as a failure rather than a pass. It was deliberately left alone.

---

## 6. Delivery

| Item | Value |
|---|---|
| Branch | `main` (the required destination) |
| Local branch created from | verified `origin/main` = `13f9a1c` at task start |
| Scope of the diff | 3 code/test files (`backend/db/transport.py` new, `backend/db/__init__.py`, `tests/test_supabase_transport_http1.py` new) plus the two fully replaced reports |
| Shell `git push` | **Failed — no credentials** in the sandbox (`could not read Username for 'https://github.com'`) |
| Delivery channel | The authenticated GitHub integration (Git Data API), reported in the completion response with `origin/main` verification |
| Pre-existing unrelated work | Preserved; untouched by this change |

No rebase, no force-push, no history rewrite, no reset of unrelated work.

---

## 7. Deployment and live verification

**Not verified in this round.** Whether Render deployed the commit, its terminal
status, its startup logs, and whether production episodes stop were **not
observed** — this workspace has no Render access and no Supabase credential, and
none was requested.

To confirm from the deployed build, with no further change:

1. The log must contain `Supabase transport=http/1.1 (HTTP/2 disabled)`.
2. If an episode still appears, `phase` / `elapsed` / `chain` / `since_teardown`
   remain the deciders.
3. `ConnectionTerminated error_code:1` should no longer be produced by this
   client, because h2 is never negotiated.

A successful deployment or a `live` status would **not** by itself prove the
recurring error stopped.

---

## 8. Remaining risks and open items

| Item | Status |
|---|---|
| Syscall-level cause of the original `EAGAIN` | **Not proven**; the fix removes the reproducible configuration, not a proven syscall cause |
| `errno=11` reproduction | **Never reproduced** here; not claimed |
| Deployed runtime | Previously observed CPython 3.13.4 / OpenSSL 3.0.20; this workspace is 3.12.3 / 3.0.13. The fix does not depend on the runtime |
| `render.yaml` pins `3.11.7`, live logs previously showed 3.13.4 | Configuration drift, documented in `INVESTIGATION.md`; **not** changed here |
| Unpinned transitive dependencies (`postgrest`, `httpcore`, `h2`) | The HTTP/1.1 pin is asserted by tests; a dependency that renames the factory would fail those tests rather than silently revert |
| Factory install scope | Process-wide: any other importer of `supabase.create_client` would also receive the HTTP/1.1 factory. In this single-process, single-owner bot that is the intended construction path, and it is covered by a test |
| HTTP/1.1 throughput | One connection per in-flight request instead of one multiplexed socket; the pool is bounded at 4 workers, so at most 4 connections are in use. No throughput regression was measured, and none is claimed |
