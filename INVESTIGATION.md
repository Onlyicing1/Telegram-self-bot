# INVESTIGATION.md — Recurring Supabase `ReadError` / `EAGAIN` (errno 11)

This file records the **current** investigation only, and replaces every earlier
report in full. The previous content (Glass UI inline-button colour semantics,
committed as `2805a88182ffb002c26f93617a2b08a65d52141c` on 2026-10-15 for branch
`feat/emoji-panel-single-diagnostic-button`) was an unrelated, already-delivered
task and remains readable in git history. It contained no EAGAIN material.

| Item | Value |
|---|---|
| Investigation date | 2026-10-19 (this session) |
| Checked-out branch | `feat/private-privacy-aed-persistence` |
| Local HEAD | `05c06588bb699c594c35e620a7d4eecbb967e116` (2026-10-17 23:32 UTC) |
| `origin/main` (fresh `git ls-remote`, this session) | `13f9a1cdade1668a72ef5e69b85b1eb571503ff9` |
| Local `origin/main` tracking ref | `89a3007` — **stale**, not refreshed by this session |
| Working tree | only the two pre-existing modifications `IMPLEMENTATION_REPORT.md` and `tests/test_database_setup_order.py` (not touched, not committed) |
| Verified locally on | Python **3.12.3**, OpenSSL **3.0.13** (project `.venv`) |
| Production runtime (owner-supplied, **not verified here**) | Python **3.13.4**, OpenSSL **3.0.20** |
| `render.yaml` declares | `PYTHON_VERSION: 3.11.7` |
| Live production access | **none** — no Render dashboard, no log stream, no Supabase credentials, no shell on the host |
| Changed in this task | this report only; no production code, dependency, schema, secret or deployment change |

---

## 1. What is verified right now

### 1.1 Dependency facts (read from the installed tree)

`backend/requirements.txt` pins `supabase==2.4.2`, `httpx==0.27.0`. The resolved
installed set in `.venv` this session:

```
postgrest 0.16.11   httpcore 1.0.9   h2 4.4.1   h11 0.16.0   anyio 4.15.1
```

`h2` and `httpcore` are **not** pinned by this repository; they are transitive.
Production's resolved versions are unknown (no `pip freeze` from the live
service). The local reproduction below therefore describes *this* resolution,
not a proven one-to-one copy of production.

### 1.2 HTTP/2 is not a choice this application makes

PostgREST hard-codes it:

```
.venv/.../postgrest/_sync/client.py:43   def create_session(...)
.venv/.../postgrest/_sync/client.py:56       http2=True,
```

`SyncPostgrestClient` exposes no `http2` argument, and `supabase.create_client`
does not forward one. The only Supabase client construction in the repository is
`backend/db/client.py:106` (`create_client(url, key, ClientOptions(postgrest_client_timeout=8.0))`).
There is no repository code path that can turn HTTP/2 off — the grep for
`http2` across `backend/` returns nothing.

### 1.3 Concurrency is deliberately 4 threads over one shared client

`backend/db/client.py`:

* `_DB_TIMEOUT = 10.0` (line 64), `_DB_HTTP_TIMEOUT = 8.0` (line 74)
* `_DB_MAX_WORKERS = 4` (line 80), one `ThreadPoolExecutor` (lines 124–136)
* `run_sync_db()` dispatches every synchronous Supabase call onto that pool
  (lines 139–158)
* `_client` is a single module-level singleton (line 38 / `get_db()` line 89)

So up to four threads issue `postgrest … .execute()` **on the same
`httpx.Client`, and therefore on the same HTTP/2 connection**.

### 1.4 The library's locking does not protect that connection

`httpcore/_sync/http2.py` (1.0.9):

```
:61  self._read_lock  = Lock()
:62  self._write_lock = Lock()
:349 with self._read_lock:            # _receive_events → self._h2_state.receive_data(...)
:465 with self._write_lock:           # _write_outgoing_data → self._h2_state.data_to_send()
:135 self._events[stream_id] = []     # no lock
:411 del self._events[stream_id]      # no lock
```

Read and write are guarded by **different** locks, but both mutate the *same*
`h2.connection.H2Connection` object, which is not thread-safe. The `_events`
dict is mutated with no lock at all. `HTTP11Connection` (`connection.py:66`)
instead has one `_request_lock` for the whole exchange, which is why the
HTTP/1.1 path cannot exhibit the same interference.

`httpcore/_backends/sync.py:126–129` remains the single site that converts a
socket `OSError` into `ReadError` (`sock.settimeout(timeout); sock.recv(...)`),
so the production `errno 11` was produced by a read on an already-open socket,
not by connect, DNS or TLS setup.

---

## 2. Evidence collected in this session (actually run)

All runs are local, loopback, and read-only. Nothing was deployed, no Supabase
resource was contacted, no production setting was touched. Harness lives outside
the repository (`/tmp/eagain_probe/`) and is described in §5.3 so it can be
rebuilt; it is deliberately **not** committed.

### 2.1 Local matrix — real postgrest `execute()` path, one TLS server

Server: one hybrid TLS listener, self-signed, ALPN `["h2","http/1.1"]`, answers
every request with `200 application/json` and `[]` after a fixed 2 ms delay.
Client: `postgrest.SyncPostgrestClient` (unmodified → HTTP/2) or a subclass whose
`create_session` is byte-identical except `http2=False`; query
`from_("ai_tool_history").select("*").limit(1).execute()`; timeout 8 s; 6 s per arm.
Errors are capped at 25 per thread, so error counts are lower bounds.

| Arm | Client sharing | Protocol negotiated | Requests OK | Failures | All failures < 100 ms | Failure chain |
|---|---|---|---|---|---|---|
| A | one shared client, 4 threads | **h2** | 1 463 | **99** | yes | `RemoteProtocolError → RemoteProtocolError` ("Server disconnected") |
| B | one shared client, 4 threads | http/1.1 | 4 947 | 0 | — | — |
| C | one client **per thread**, 4 threads | h2 | 4 594 | 0 | — | — |
| D | one shared client, 1 thread | h2 | 1 648 | 0 | — | — |

Longer supporting runs on the same harness: shared h1, 4 threads, 30 s →
**23 518 OK / 0 errors**; shared h2, 4 threads, 10 s → 2 479 OK / 98 errors;
shared h2, **2** threads, 6 s → 88 OK / 50 errors.

With `httpcore`/`h2` debug logging the h2 arm shows the connection being torn
down mid-flight and the client state machine corrupted:

```
ProtocolError('Trailers must have END_STREAM set.')            x21
KeyError(3) / KeyError(5) / KeyError(7) …                      x>10
  at httpcore/_sync/http2.py:411  del self._events[stream_id]
LocalProtocolError -> ProtocolError -> AssertionError           x6
WriteError -> WriteError -> SSLZeroReturnError[errno=6]         x14
WriteError -> WriteError -> SSLEOFError[errno=8]                x7
```

### 2.2 External control — a real production HTTP/2 endpoint, no test server

Same concurrency shape, plain `httpx.Client(timeout=8.0, http2=…)`, 4 threads,
15 s, against `https://www.cloudflare.com/cdn-cgi/trace`:

| Arm | Requests OK | Failures | Failure latency | Chain |
|---|---|---|---|---|
| http2=True, one shared client | — (stopped by cap) | **99** | **1.7 – 2.9 ms** | `RemoteProtocolError` ("Server disconnected") |
| http2=False, one shared client | 6 258 | 0 | — | — |

This is the important control: the failure reproduces against a real,
independent, production HTTP/2 origin with no hand-written server in the path.
It is not an artifact of the local test server, and it is not rate limiting
(the HTTP/1.1 arm made 6 258 requests to the same host and path in the same
window with zero failures).

### 2.3 Negative control — the bare single-socket race is not sufficient

`httpcore`'s own `SyncStream` over one TLS socket, no HTTP framing: one thread
looping `read(65536, timeout=8.0)` while another loops `write(256 B, timeout=8.0)`,
3 ms server delay, 10 s →

```
reads 3 168   writes 9 445   errors 0
```

A read-only control produced exactly one expected `ReadTimeout` after 8.0 s.
So "two threads each call `settimeout()` before a syscall on the same socket",
on its own, did **not** produce `BlockingIOError(EAGAIN)` at this scale on
Python 3.12.3 / OpenSSL 3.0.13.

### 2.4 What the three results together establish

1. The production structure — **one shared client + HTTP/2 + 4 threads** — is a
   reproducible failure precondition on this library resolution, on loopback and
   against a real production origin. Removing *either* HTTP/2 (arm B) or client
   sharing (arm C) removes it; removing concurrency (arm D) removes it too.
2. The failures are **instantaneous** (1.7–100 ms), not 8 s. That matches the
   reported production timing (0.0–0.1 s) far better than any timeout,
   saturation or slow-query explanation.
3. The **exact production signature was not reproduced**: this session saw
   `RemoteProtocolError`/h2 state corruption, never
   `ReadError ← BlockingIOError(errno=11)`. The errno flavour — which syscall
   loses the race inside a connection that another thread is tearing down — is
   therefore still unexplained.
4. The bare `settimeout()`+`recv`/`send` race on one socket is **not**
   sufficient by itself at this scale, so the explanation must involve the
   connection being closed/replaced while a request is in flight, not merely
   concurrent socket calls.

---

## 3. Why these failures were not noticed before

The existing regression tests (`tests/test_local_resource_bounds.py`,
`tests/test_task_fallback_classification.py`) pin the *dispatch* rules — bounded
pool size, transport deadline shorter than the watchdog, and "a local resource
error is not evidence of a store outage". They mock `run_sync_db` or use fake
clients, so they never open a socket and cannot observe a shared HTTP/2
connection being torn down. `backend/db/client.py`'s own comment (lines 76–79)
assumes the bounded pool keeps "the number of sockets the single shared httpx
client must keep alive" bounded — which is true for HTTP/1.1 and false for
HTTP/2, where four workers share **one** socket.

---

## 4. Hypothesis comparison (weighted, not equal)

| Explanation | Verdict | Basis |
|---|---|---|
| **Shared HTTP/2 connection mutated by 4 threads** | **Primary — supported** | Reproducible on loopback and on a real production h2 origin; disappears with http/1.1 or per-thread clients; matches the instant-failure timing; the httpcore lock split (§1.4) is a concrete defect |
| Python 3.13.4 / OpenSSL 3.0.20 runtime behavior | **Not tested here; cannot yet be excluded** | Production raises a different errno than this 3.12.3/3.0.13 stack produces. Could explain the *flavour* of the error, not the precondition. `render.yaml` (3.11.7) disagrees with the reported live version — a real configuration defect, but not shown to be causal |
| Connection-pool / keepalive lifecycle | **Secondary, untested** | Pool churn is visible (many connections opened per arm) and a GOAWAY/teardown is what turns a healthy request into an instant failure, but http/1.1 through the same pool is clean |
| Local resource pressure / thread starvation | **Not supported** | Failures at 2–26 ms with idle CPU, 4 threads, and a clean read-only control |
| Timeouts too tight / retry absence | **Excluded** | Failures never approach the 8 s transport deadline or the 10 s watchdog |
| Supabase-side behavior (GOAWAY, edge resets) | **Plausible contributor, unmeasured** | The real-origin control shows the client-side defect exists independently, so Supabase is not required to explain the mechanism; it can still shape which errno surfaces |

---

## 5. The single next experiment

### 5.1 Objective

Decide, with one measurement, whether the production `errno 11` comes from the
shared multiplexed connection (Primary) or whether it needs the production
runtime (Python 3.13.4 / OpenSSL 3.0.20) on top of that structure.

### 5.2 Experiment — same matrix, real Supabase REST endpoint, read-only

Run the §2.1 harness against the project's own PostgREST endpoint instead of
loopback. It is a separate local process: no production code, setting, secret,
schema or deployment is changed, and it issues only `GET …?select=…&limit=1`.

**Requirement (unavailable in this workspace):** `SUPABASE_URL` and one
Supabase key with `SELECT` on an existing table (e.g. `ai_tool_history`) —
service-role or anon, supplied to the probe process only, never written to the
repository or to chat. Without it this experiment **cannot be executed**; the
next-best feasible alternative was executed instead and is reported in §2.

* **Baseline (unchanged):** installed `httpx 0.27.0`, `postgrest 0.16.11`,
  `httpcore 1.0.9`, `h2 4.4.1`; timeout 8.0 s; 4 worker threads; the same query
  string; the same machine and network; 60 s per arm.
* **Single variable:** client sharing/protocol, exactly as in §2.1 —
  A) one shared client, `http2=True`; B) one shared client, `http2=False`;
  C) one client per thread, `http2=True`. A is production-identical.
* **Held constant:** endpoint, path, query, headers, timeout, thread count,
  duration, interpreter, library versions, measurement code.
* **Measurements per arm:** requests OK / failed; full `__cause__` chain of
  every failure including `errno`; per-failure elapsed time in ms; count of
  failures under 100 ms; number of TLS connections opened; number of
  `RemoteProtocolError` / `ReadError` / `WriteError` / `LocalProtocolError`.
* **Load bound:** keep the total under ~12 000 requests (4 arms × 60 s at the
  observed ~800 rps loopback ceiling is *not* expected on a real RTT; expect
  hundreds per arm). Stop early if either arm exceeds 100 failures.
* **Outcome map:**
  1. A reproduces `ReadError ← BlockingIOError(errno=11)` (sub-100 ms) while B
     and C stay clean → **Primary confirmed end-to-end**; the next task is the
     deliberate fix (stop forcing one HTTP/2 connection across the pool —
     per-thread client or HTTP/1.1 for PostgREST), with a regression test.
  2. A fails, but with the §2.1 signature and no `errno=11` → the structural
     precondition is confirmed and the errno remains runtime-specific; the next
     discriminator becomes a **Python 3.13.4 + OpenSSL 3.0.20 replay of the same
     harness** (single variable: interpreter), which is also local.
  3. All three arms clean over the full window → the real endpoint alone is not
     sufficient; go directly to the 3.13.4/3.0.20 replay before changing
     anything.
  4. A and B fail identically → HTTP/2 is not the differentiator; pivot to
     connection lifecycle (GOAWAY/keepalive) using the same harness and its
     per-connection request counts.
* **Limitations:** a negative result does not clear the structure (the trigger
  may need hours of idle/burst cycling); a positive result proves the client-side
  mechanism but still not *which* production deployment produced the logged
  traceback.
* **Rollback:** nothing to roll back — the probe is a separate process with no
  writes. Terminate it, delete its temporary virtualenv, confirm no repository
  file changed.

### 5.3 Reproduction recipe (the executed part)

The harness is not committed. It was three small scripts under `/tmp/eagain_probe/`:

1. `server.py` — asyncio TLS server, self-signed cert, `set_alpn_protocols(["h2","http/1.1"])`,
   branches on `selected_alpn_protocol()`; h2 side uses `h2.connection.H2Connection`,
   responds after a fixed delay with `200 application/json` + `[]`; HTTP/1.1 side
   answers the same payload and keeps the connection alive.
2. `client.py` — `SyncPostgrestClient(base_url, verify=False)` for the HTTP/2 arm;
   `Http1PostgrestClient(SyncPostgrestClient)` overriding only `create_session`
   with `http2=False` for the other arm; N threads looping
   `from_("ai_tool_history").select("*").limit(1).execute()`; records cause
   chains and elapsed time; `--own-client` builds one client per thread.
3. `socket_race.py` — `httpcore._backends.sync.SyncStream` over one
   `ssl`-wrapped socket, one reader thread and/or one writer thread, error
   histogram by exception chain and errno.

---

## 6. Conclusion

* **Verified:** HTTP/2 is forced on for every Supabase call by
  `postgrest 0.16.11`; one shared client is used by four worker threads; a
  shared HTTP/2 client under 2+ threads fails within milliseconds against both a
  local server and a real production HTTP/2 origin, while HTTP/1.1 and
  per-thread clients are clean over thousands of requests.
* **Not verified:** the production traceback itself never appeared locally; the
  production runtime (3.13.4 / 3.0.20) and production transitive versions were
  not inspectable; `render.yaml`'s `3.11.7` contradicts the reported live
  version and should be reconciled independently of this investigation.
* **Selected next step:** the read-only Supabase-endpoint matrix in §5.2. It
  changes one variable, is reversible, needs no production change, and each of
  its four outcomes names the next action. It has **not** been run — the
  credential it needs is not available in this workspace; §2 is the executed
  next-best alternative.
* **Not done, deliberately:** no production setting changed, HTTP/2 not disabled
  in production, no retries added, no timeout altered, no dependency upgraded.
