# IMPLEMENTATION REPORT — CURRENT STATE

> **This is a CURRENT-STATE document.** It describes the repository as it
> exists at the tip of this phase. If code changes invalidate any section,
> update this document in the same commit.

---

## 1. Implementation metadata

| Item | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Branch | `main` |
| Starting HEAD | `011863e9ce779d57c7de0423b40156a98d034cd2` (== `origin/main` at phase start) |
| Phase | **Truthful degraded-store classification** — a transient LOCAL resource error (`[Errno 11]`) was reported to the owner as "Supabase unavailable" |
| Status | **IMPLEMENTED — full suite green (2156 passed, 24 skipped, 0 failed)** |
| Database impact | **NONE** (no schema, migration, RLS, table, or configuration change) |
| Live Render verification | **NOT performed** (no production credentials/telemetry access in this workspace) — see §7 |
| Delivery record | see §8 |

---

## 2. Production evidence (as reported, not re-derived)

```
14:30:54.316  TASK_OCCURRENCE_PERSIST_CREATE_ATTEMPT repository=SupabaseTaskRepository owner_id=7283627550 task_id=2
14:30:54.417  TASK_OCCURRENCE_PERSIST_CREATE_SUCCESS repository=SupabaseTaskRepository occurrence_id=27
14:30:54.986  AI record_tool_call failed: [Errno 11] Resource temporarily unavailable
14:30:54.986  Supabase occurrence read failed; using fallback: [Errno 11] Resource temporarily unavailable
```

Same health sample: `Last RPC: 59.6s ago`, `RPC latency: 169.5ms`, `Last Telethon event: 0.1s ago`,
`KEEPALIVE_OK latency_ms=171.2 gen=1`.

Facts these lines prove:

1. **Supabase was not disconnected.** A Supabase write on the same client
   succeeded 0.57 s before the failures.
2. The two failures are **concurrent and unrelated** operations (an audit
   write and an occurrence read) that failed in the **same millisecond** with
   the **same** error — a shared, local cause, not two independent store
   problems.
3. The runtime was otherwise healthy (Telethon events, RPC, keepalive).

---

## 3. Root cause

### 3.1 Where the error is raised

Both failing operations execute the **synchronous Supabase HTTP call in a
worker thread** through `asyncio.to_thread`:

| Failing operation | Await path |
|---|---|
| `AI record_tool_call` | `ToolExecutor._execute_single` → `guarded_create_task(persistence.record_tool_call(...))` → `persistence._run_sync` → `asyncio.wait_for(asyncio.to_thread(_record_tool_call_sync))` |
| occurrence read | `SupabaseTaskRepository.get_occurrence` → `_run` → `asyncio.wait_for(asyncio.to_thread(fn))` → `postgrest` sync client → `httpx.Client` |

`[Errno 11] Resource temporarily unavailable` is `os.strerror(11)` (`EAGAIN`).
The classification was proven from source, not assumed:

| Candidate | Verdict | Proof |
|---|---|---|
| Thread-pool / thread-creation exhaustion | **No** | CPython raises `RuntimeError("can't start new thread")` when a thread cannot start, with **no errno** attached (verified in this workspace's CPython 3.10: the binary contains `can't start new thread` and contains no errno path for it). `asyncio.to_thread` → `run_in_executor` → `ThreadPoolExecutor` cannot surface `EAGAIN`. |
| `subprocess` / `fork` (`BlockingIOError` EAGAIN is the classic fork failure) | **No** | The whole backend contains no `subprocess` / `Popen` / `os.fork` / `posix_spawn` usage. |
| PostgREST / SQL / database error | **No** | `postgrest`/`supabase` surface those as `APIError`/`HTTPStatusError`; and `httpcore/_backends/sync.py::SyncNetworkBackend.connect_tcp` maps only `socket.timeout → ConnectTimeout` and **`OSError → ConnectError`** — so a raw `OSError(errno=11)` from the socket layer is surfaced as a transport error whose message is the OS string. |
| **Local OS socket-layer resource failure** | **Yes** | `socket.create_connection()` (socket acquisition / `connect()`, including auto-binding an ephemeral port — a documented `EAGAIN` cause) inside the worker thread's Supabase HTTPS request. |

So the originating function is the **local socket layer under the synchronous
Supabase HTTP call** (reached through `backend/ai/persistence.py::_run_sync`
and `backend/ai/database/task_repository.py::_run`); it is **not** evidence
that the durable store is unavailable. The exact syscall (`socket()` vs
`connect()`) cannot be distinguished from the log line alone — the honest
statement is "local socket-layer resource error".

### 3.2 Why `record_tool_call` failed in the same way

`record_tool_call` is already **fire-and-forget and decoupled**: the executor
spawns it with `guarded_create_task(..., name="ai:record-tool-call")` and
returns the tool result regardless. Its failure therefore never blocked or
changed task execution. It failed only because it independently opened its own
HTTP connection in the same instant and hit the same local shortage. **No
coupling needed fixing**; §5 adds a regression test that pins the decoupling.

### 3.3 The actual defect (what was wrong in the code)

`SupabaseTaskRepository` converted **every** exception from a Supabase
operation into the degraded state (`_mark_fallback()`), and every user-facing
surface then printed:

```
⚠️ Memory fallback — Supabase unavailable
(tasks may be missing, and anything created now is not durable).
```

The degradation itself is correct (the write really did fall back to memory, so
it really is non-durable). The **claim** was wrong: a transient local resource
error is not a Supabase outage, and the loud "Supabase unavailable" note made a
healthy store look dead.

### 3.4 Answering the remaining investigation questions

- **Was task #3 durable when the owner received "Task #3 created"?** No — the
  durable insert failed with the same transport error, the create degraded to
  the shared in-memory store, and the non-durable warning was therefore
  correct. Only its attribution was wrong.
- **`Task #3` vs `task_id=2`?** Not a separate defect. `2` is the durable task
  the occurrence (id 27) belongs to; `3` is the degraded store's provisional id
  (the fallback is floored above the highest durable id observed, so it cannot
  reuse `2`).
- **Why did the UI say "Supabase unavailable"?** Because the create returned
  `fallback_backend` and the surface rendered the single, unconditional
  `FALLBACK_NOTE`.

---

## 4. Exact fix (smallest production-safe change)

The fallback boundary is unchanged — **every** failure still degrades
(resilience preserved). Only the **reason** is now classified, from evidence,
and only the **attribution** in user-facing text changes.

### 4.1 `backend/ai/database/task_repository.py`

- `_LOCAL_RESOURCE_ERRNOS` = `{EAGAIN/EWOULDBLOCK, EMFILE, ENFILE, ENOMEM, ENOBUFS}`.
- `_is_local_resource_failure(exc)` walks the whole `__cause__`/`__context__`
  chain (because `httpx` wraps the raw `OSError`) and reports whether the
  failure is a local resource shortage.
- `FALLBACK_REASON_UNAVAILABLE` / `FALLBACK_REASON_LOCAL_RESOURCE`, plus a
  `fallback_reason` property and `_mark_fallback(exc)` /
  `_degrade_to_fallback(exc)`. Every call site now passes the exception.
  `_mark_supabase_ok()` clears the reason again.
- `_annotate_fallback` / `_annotate_deletion` / the degraded `create_task`
  annotate `fallback_reason` next to the existing `fallback_backend`
  (observability annotations only — never written to the database).
- `TaskDeletionResult` carries `fallback_reason`.
- New structured diagnostic `TASK_FALLBACK_CLASSIFIED reason=… exception=…
  message=…` whenever the reason is `local_resource`, because the existing
  per-operation warning prints only the OS string — the exact gap that made the
  live misclassification undecidable from logs.

### 4.2 Truthful notes

- `backend/ai/task_management_interface.py`: added `FALLBACK_RESOURCE_NOTE`
  ("… a local resource error prevented the durable store from being reached
  …") and `fallback_note(reason)`, which returns the original
  `FALLBACK_NOTE` only for a real durable-store failure. `list_text` and
  `inspect_text` use it.
- `backend/ai/task_management.py`: `TaskListSnapshot.fallback_reason` (default
  `""`), populated from the repository in the same single snapshot read.
- `backend/ai/tools/task.py` (create), `backend/ai/tools/task_management_tools.py`
  (transition + delete), `backend/bot/handlers/taskloom.py` (delete): render the
  truthful note and expose `fallback_reason` in the result data. A local
  resource error no longer produces "Supabase unavailable".

### 4.3 Preserved semantics (unchanged)

- a genuine Supabase failure still enters the same fallback path and still
  shows the original "Supabase unavailable" note;
- in-memory results are still explicitly **non-durable** (`fallback_active`,
  `fallback_backend`, `durable=False`) — a local resource error still degrades
  the same way, it is only attributed truthfully;
- durable success is still durable and clears the degraded state;
- owner isolation, CAS/version rules, task-id policy, delete semantics, audit
  logging and diagnostics are untouched.
- No new scheduler, worker, database authority, Supabase client, fallback
  system, retry, or timeout was added.

---

## 5. Tests

New focused regression file — `tests/test_task_fallback_classification.py`
(10 tests, in-process only):

| Requirement | Test |
|---|---|
| A local resource errno is not a store outage | `test_local_resource_errno_is_not_evidence_of_a_store_outage` |
| The wrapped `__cause__` chain is walked | `test_the_wrapped_cause_chain_is_walked` |
| A real transport failure keeps the Supabase note | `test_genuine_store_failure_keeps_the_supabase_note` |
| A local resource error never claims Supabase is unavailable | `test_local_resource_failure_never_claims_supabase_is_unavailable` |
| The exact production read path (occurrence read) | `test_occurrence_read_classification_matches_the_production_failure` |
| Degraded create stays non-durable, attributed truthfully | `test_local_resource_create_is_non_durable_with_truthful_attribution` |
| A genuine create failure keeps the Supabase note | `test_genuine_create_failure_keeps_the_non_durable_supabase_note` |
| The real `CreateTaskTool` message path | `test_create_message_attributes_a_local_resource_error_truthfully` |
| Durable success stays durable and clears the reason | `test_a_successful_durable_read_is_durable_and_clears_the_reason` |
| `record_tool_call` failure cannot mark task persistence unavailable | `test_audit_persistence_failure_cannot_mark_task_persistence_unavailable` |

Existing task tests were not weakened; their degraded fixtures raise
non-resource errors (`RuntimeError`), so they keep the original
`FALLBACK_NOTE` behaviour and remain green.

---

## 6. What was NOT changed

No change to: Supabase schema/migrations/RLS/tables, ProviderManager, provider
routing, Test Modules, model discovery, TaskScheduler architecture,
RuntimeSupervisor, Telethon client architecture, Bio Guardian, preparation
semantics, task creation/scheduling semantics, audit logging, or delete
semantics.

---

## 7. Verification and limitations

- `python -m py_compile` on every changed Python file — OK.
- `git diff --check` — clean.
- Focused suites (`task_fallback_classification`, `task_repository`,
  `task_hardening`, `task_list_consistency`, `task_durable_delete`,
  `task_management`, `taskloom_ui`): **91 passed**.
- **Full suite (`pytest tests`): `2156 passed, 24 skipped, 0 failed`**
  (previous tip: 2146 passed / 24 skipped).

**Live Render verification: NOT performed.** What remains unverified in
production:

1. That the observed `[Errno 11]` is in fact the socket-layer `EAGAIN`
   (the new `TASK_FALLBACK_CLASSIFIED` line will name the exception type on the
   next occurrence).
2. The owner's manual probe: create the task again while the store is healthy
   → the create must be reported **durable with no fallback note**; on a
   transient local error the note must read "a local resource error prevented
   the durable store from being reached", never "Supabase unavailable".

**Remaining limitation:** the fix corrects classification and attribution; it
does **not** remove the local resource shortage itself (e.g. ephemeral-port or
fd/thread contention in the container). If `TASK_FALLBACK_CLASSIFIED
reason=local_resource` recurs, the next step would be host-level socket
accounting — deliberately out of scope here, since the architecture forbids a
second Supabase client/pool or blanket retries.

---

## 8. Delivery record

| Item | Value |
|---|---|
| Starting HEAD | `011863e9ce779d57c7de0423b40156a98d034cd2` (== `origin/main` at start) |
| Change set | `backend/ai/database/task_repository.py`, `backend/ai/task_management.py`, `backend/ai/task_management_interface.py`, `backend/ai/tools/task.py`, `backend/ai/tools/task_management_tools.py`, `backend/bot/handlers/taskloom.py` + new `tests/test_task_fallback_classification.py` + this report |
| Commit | `fix: stop reporting local resource errors as Supabase unavailability` — pushed to `origin/main`, remote SHA verified after push |
| Previous phase (record) | `011863e` — `feat: improve model testing visibility and proven fallback pool` |
| Working tree | pre-existing untracked stray clone `telegram-self-bot/` deliberately left untouched; no schema/migration files changed |
| Live Render / Telegram verification | **NOT performed** — the owner runs the §7 probes manually |
