# IMPLEMENTATION REPORT — CURRENT STATE

> **This is a CURRENT-STATE document.** It describes the repository as it
> exists at the tip of this phase. If code changes invalidate any section,
> update this document in the same commit. Previous phase reports are kept
> verbatim below under `ARCHIVE - PREVIOUS PHASE REPORTS`.

---

## Phase - Structured Taskloom task-creation wizard

### Metadata

| Item | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Branch | `main` |
| Starting HEAD | `5325a4065bf29ac1f3379de9d744917f82e0b460` (`main` == `origin/main` at phase start) |
| Phase | **Taskloom creation UX + content-constraint contract** - structured wizard converging on the existing TaskCandidate/TaskCreationService path, plus English language support and label-free speaker presentation in the deterministic preparation policy |
| Status | **IMPLEMENTED** - full suite green (2220 passed, 24 skipped, 0 failed); focused wizard suite 34 passed |
| Database impact | **NONE** (no schema, migration, RLS, table, index, or SQL file touched; the wizard writes through the existing `ai_tasks` columns only) |
| Live Telegram verification | **NOT performed** (no live Telegram session/telemetry access in this workspace) |
| Delivery record | see "Delivery record" |

### Exact problem

A recurring Bio task created from natural language executed successfully, but
the generated bio was not in the requested language and the visible bio was
forced to carry a literal speaker label (`Ayanami Rei: ...`):

    هر ۲ دقیقه بیو رو آپدیت کن به یه دیالوگ رندوم از آیانامی ری که زیر 60 کاراکتر باشه

Two distinct causes, both traced in source:

1. **No language was named, so no language was enforced - and English was not
   representable at all.** `backend/ai/preparation_policy.py` derives the content
   policy from the instruction only. It recognized `persian`, `chinese`, and
   `arabic` word lists; there was **no English vocabulary**, so a request for
   English content could not produce `derive_policy(...).language == "english"`,
   and the only way to obtain English was to leave the language unconstrained
   (the model then answered in Persian).
2. **Source identity validation and user-visible formatting were the same
   thing.** `_check_attribution` requires the generated line to OPEN with the
   requested source name and a separator, and the validated text is exactly what
   executes - so the owner's bio was forced to display a speaker label.
3. **Task creation had no structured path.** The only entry points were the
   natural-language interpreter (`.task` / `create_task` tool) and the Taskloom
   panels, which could list/inspect/pause/resume/complete/delete but not create,
   so every task parameter had to survive one natural-language sentence.

### Source-traced root cause

- `backend/ai/preparation_policy.py::derive_policy` - language detection had no
  English branch, `PreparationPolicy.language` had no `english` state, and
  `_check_language` had no English script rule.
- `backend/ai/preparation_policy.py::_check_attribution` - the attribution parse
  both validated and defined the executed content; no presentation concept
  existed anywhere in the contract (task -> `ai_instruction` -> occurrence
  preparation -> `ToolExecutor`), so "verify the speaker" inevitably meant "print
  the speaker".
- The Taskloom handler exposed no creation entry point at all.

### Exact implementation

**a) Structured creation wizard (`backend/ai/task_wizard.py`, new).**
Stateless deterministic contract: `TaskDraft` -> `build_candidate()` produces the
**same** candidate dict the natural-language path produces, validated by the same
`TaskCandidate.from_untrusted` and persisted by the same
`TaskCreationService` -> `TaskRepository`. Actions come from their registered
execution contract (`bio_set_text`, `username_set_text`, `send_message`);
generated content is offered only where the tool contract allows it
(`send_message` requires bounded non-blank text, so it stays static).
`build_instruction()` composes the durable `ai_instruction`, and
`_instruction_problem()` **round-trips it through `derive_policy`** before any
candidate is returned, so a constraint the policy cannot represent is a hard
error instead of a silently unenforced review line.

**b) Wizard UI (`backend/bot/handlers/taskloom.py`).** Four steps over the
existing Glass UI inline-panel/callback/input infrastructure:
`Action -> Content -> Content details -> Schedule -> Review -> Create`, plus a
`+ New task` row on the Taskloom list. Inputs (`source`, `maxlen`, `text`,
`interval`, `daily`, `weekly`, `once`, `tz`) use the existing
`register_input`/pending-input flow; drafts are per-owner in-process UI state.

**c) English language policy.** `_ENGLISH_WORDS = ("انگلیسی", "english")`,
`language = "english"`, and a `_check_language` English branch: at least one
Latin letter and no Persian/Arabic/Han letters. No silent default - an
unspecified language still imposes nothing.

**d) Label-free speaker presentation.** `PreparationPolicy.speaker_label`
(default `True`) is derived from explicit markers (`without a speaker label`,
`بدون نام گوینده`, ...). Attribution is still validated on the generated line at
every point; `validate_content` keeps its "never rewrites content" contract and
applies the language/length checks to the VISIBLE text;
`backend/ai/task_execution.py::present_calls` - called once, after the final
boundary validation and immediately before `ToolExecutor` - removes the verified
opening label from the copies handed to the executor, while the validated calls
(the ones persisted as occurrence metadata and re-validated later) stay intact.
The model is therefore still forced to attribute the line (identity proof kept)
and the owner sees only the dialogue.

### Files changed

| File | Change |
|---|---|
| `backend/ai/task_wizard.py` | **new** - draft -> `ai_instruction` -> candidate contract, parsing/validation, review rendering |
| `backend/bot/handlers/taskloom.py` | wizard panels/actions/inputs + `+ New task` entry |
| `backend/ai/preparation_policy.py` | English language support; `speaker_label`; label-free length/language checks on the visible text; `strip_attribution_prefix` |
| `backend/ai/task_execution.py` | `present_calls` presentation step at the execution boundary |
| `tests/test_task_wizard.py` | **new** - 34 focused tests |
| `IMPLEMENTATION_REPORT.md` | this report |

No other file was touched. No Supabase schema/migration/SQL change. No new
scheduler, executor, repository, or persistence path.

### Wizard flow

    Taskloom -> + New task
      Step 1 Action            Bio update | Username update | Write a message
      Step 2 Content           AI-generated (fresh each run) | Static text (same every run)
      Step 3 Content details    Character/source . Language . Maximum length . Speaker label
                               (static mode: the exact text)
      Step 4 Schedule           Once | Every N minutes | Daily | Weekly  + Timezone
      Review                    every row derived from the built candidate
      [Create task]             TaskCreationService -> TaskRepository (durable)

The review screen is rendered from `build_candidate()`, never from the draft, so
it cannot display a value the persisted candidate does not contain.

### Required vs optional fields

- **Required:** action; content mode; static text (static mode only); schedule
  type AND its parameter (interval minutes / `HH:MM` / weekday+`HH:MM` /
  `YYYY-MM-DD HH:MM`); timezone (defaults to the owner's configured timezone -
  no second timezone source of truth). Creation without a valid schedule is
  impossible; a `once` start must be in the future.
- **Optional (AI-generated mode):** source/character, language, maximum length,
  speaker-label presentation. An unset field imposes nothing; no fabricated
  default is inserted.

### Language-contract behaviour

| Selection | Durable `ai_instruction` fragment | `derive_policy` result |
|---|---|---|
| English | `in English` | `language == "english"` |
| Persian | `in Persian` | `language == "persian"` |
| Arabic | `in Arabic` | `language == "arabic"` |
| Chinese | `in Chinese` | `language == "chinese"` |
| not set | *(absent)* | `language is None` (unchanged semantics) |

Example composed instruction for the reported request:
`update my bio, with a randomly generated dialogue, in English, at most 59 characters, without a speaker label, from Ayanami Rei`.
The deterministic validator remains the final authority; the model's self-report
is never trusted.

### Bio source/format behaviour

- Source identity is still proven deterministically (opening attribution with
  the full requested name + separator); drifting speakers, short forms, in-text
  mentions, and generic text are still rejected and regenerated.
- With label-free presentation selected, the executed bio is
  `Don't be afraid. You are not alone.` while the validated line was
  `Ayanami Rei: Don't be afraid. You are not alone.` - validation and visible
  formatting are separated, neither weakened.
- Maximum length remains an INCLUSIVE maximum (`at most N characters`, matching
  the policy's existing `max` branch). The pre-existing "under N"/"below N"
  semantics (maximum of N-1) are untouched, and the label is excluded from the
  counted length only when it will not be displayed.

### Test results

- Focused: `tests/test_task_wizard.py` - **34 passed**.
- Content-policy/execution regression files unchanged and green:
  `tests/test_preparation_policy_source.py`, `tests/test_task_ai_preparation.py`,
  `tests/test_task_source_fidelity.py`, `tests/test_task_prepare_ahead.py`,
  `tests/test_taskloom_ui.py`.
- Full suite: `.venv/bin/python -m pytest tests -q` - **2220 passed, 24 skipped, 0 failed**.
- `py_compile` - OK for every changed/added Python file.
- `git diff --check` - clean.

### Live verification status

**Live Telegram verification was NOT performed** (no live session/credentials in
this workspace). In-process verification covered the whole wizard -> candidate ->
`TaskCreationService` -> repository path against the real code with an
`InMemoryTaskRepository`.

### Remaining limitations (honest)

1. **Event-triggered schedules are not offered by the wizard.** `once`,
   `interval`, `daily`, and `weekly` are supported; a Telegram-event trigger
   needs dialog-name resolution at creation time and remains on the
   natural-language path (documented, not faked).
2. **The composed instruction is English-phrased.** `derive_policy` also accepts
   the Persian markers, but the wizard's wording is not localized; the generated
   CONTENT language is fully controlled by the language field.
3. **Wizard drafts are in-process UI state** (like every other pending panel
   input): a process restart discards an unconfirmed draft. Nothing durable is
   created until the owner presses Create.
4. **Source names that themselves contain a language word** (e.g. "English
   Rose") are rejected by the round-trip guard rather than silently imposing an
   unintended language constraint.

### Delivery record

| Item | Value |
|---|---|
| Starting HEAD | `5325a4065bf29ac1f3379de9d744917f82e0b460` |
| Final implementation commit | `feat: add structured task creation wizard` — `be45990e5e0eadceb1c6932e69e2816231c76dcf` |
| Push | `5325a40..be45990  main -> main` (no force) |
| Remote verification | `HEAD` == `origin/main` == `git ls-remote origin refs/heads/main` == `be45990e5e0eadceb1c6932e69e2816231c76dcf`; `git rev-list --left-right --count HEAD...origin/main` = `0 0`; working tree clean |

---

# ARCHIVE - PREVIOUS PHASE REPORTS (verbatim)


## 1. Implementation metadata

| Item | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Branch | `main` |
| Starting HEAD | `ec933458546d21b2eca7d9ce6aa15016ac435a3d` (`== origin/main` at phase start) |
| Phase | **Bounded synchronous-Supabase resource lifecycle** — the previous phase's truthful classification is preserved unchanged; this phase bounds the resource that produced the local `EAGAIN` |
| Status | **IMPLEMENTED — full suite green (2186 passed, 24 skipped, 0 failed)** |
| Database impact | **NONE** (no schema, migration, RLS, table, index, or migration change; no SQL file touched) |
| Live Render verification | **NOT performed** (no production credentials/telemetry access in this workspace) — see §14 |
| Delivery record | see §16 |

---

## 2. Production symptoms (as reported)

1. `TASK_FALLBACK_CLASSIFIED reason=local_resource exception=ReadError message=[Errno 11] Resource temporarily unavailable` on Supabase task/occurrence reads.
2. `AI record_tool_call failed: [Errno 11] Resource temporarily unavailable`.
3. `Supabase occurrence read failed; using fallback: [Errno 11] Resource temporarily unavailable`.
4. In the **same** process, ~0.5s earlier: `TASK_OCCURRENCE_PERSIST_CREATE_SUCCESS repository=SupabaseTaskRepository occurrence_id=27`.

The same runtime sample simultaneously showed a **healthy** Telegram side
(`Last update: 0.3s ago`, `Last Telethon event: 0.1s ago`,
`KEEPALIVE_OK latency_ms=171.2`). Supabase was therefore **not** down, and the
event loop was **not** stalled. The failures were local to the process's own
synchronous HTTP sockets.

---

## 3. Exact root cause

Two provable resource-lifecycle defects existed on the **one** shared
synchronous Supabase client. Neither is a Supabase problem, and together they
turned an occasional slow call into recurring local socket failures.

**(a) The transport deadline was ~15× the application's own dispatch budget.**
The single shared client was created with `create_client(url, key)` and no
options, so it inherited **supabase-py's postgrest default of 120s**
(`postgrest/constants.py: DEFAULT_POSTGREST_CLIENT_TIMEOUT = 120`). Every
caller, however, abandons and degrades at **10s** (`backend/db/client._DB_TIMEOUT`,
`task_repository.DB_TIMEOUT`, `persistence._DB_TIMEOUT`).

A worker thread blocked inside a synchronous `recv()` **cannot be cancelled** —
`asyncio.wait_for` only abandons the awaiting coroutine. So on every slow store
the thread stayed inside the socket call for up to **120s**, holding its pooled
connection, long after the application had given up. Each new dispatch was then
pushed onto a fresh worker and a fresh connection, so a slow store increased
live socket/thread usage instead of shedding it.

**(b) Nothing bounded concurrent synchronous Supabase dispatch, and the audit
path fanned out without limit.** `backend/db/client._run_sync`,
`SupabaseTaskRepository._run`/`_run_checked` and `persistence._run_sync` each
called `asyncio.to_thread`, drawing from the event loop's **shared default
executor** (`max_workers = min(32, cpu_count + 4)` — measured as **32** in this
workspace) which every other subsystem also uses, with no cap of its own.
`ToolExecutor`, the dispatcher and the conversation manager additionally created
**one unmanaged background task per tool call / AI request / message**, and
nothing counted how many audit writes were in flight.

**Proven error shape.** `httpcore`'s sync socket layer maps *any* `OSError` from
`recv()` to `ReadError` (`httpcore/_backends/sync.py:` `exc_map = {socket.timeout: ReadTimeout, OSError: ReadError}`).
A `BlockingIOError` with `errno == EAGAIN` renders as exactly
`[Errno 11] Resource temporarily unavailable`. Reproduced in-process:

```
A) recv on a non-blocking socket -> ('BlockingIOError', 11, '[Errno 11] Resource temporarily unavailable')
```

That matches the production `exception=ReadError message=[Errno 11] Resource
temporarily unavailable` byte for byte, and confirms the previous phase's
classification: a **local OS socket-layer condition**, not a store outage.

**What is NOT proven here.** The exact upstream trigger that leaves a socket in
that state at that instant (a socket-timeout race on a shared connection, an
fd reused after a leaked client closed it, or plain kernel/conntrack pressure on
the Render instance) is not determinable from source alone. This phase therefore
fixes the defect that is **proven** — unbounded retention and amplification of
socket/thread resources on the one shared client — rather than guessing at the
trigger. See §14 for the exact live evidence still required.

---

## 4. Evidence from source

| Fact | Source |
|---|---|
| One shared Supabase client, created once | `backend/db/client.py::get_db()` (`_initialised` singleton) |
| postgrest transport deadline defaults to 120s | `.venv/…/postgrest/constants.py` → `DEFAULT_POSTGREST_CLIENT_TIMEOUT = 120`, consumed by `supabase/lib/client_options.py` |
| App-level budget is 10s | `db/client._DB_TIMEOUT`, `task_repository.DB_TIMEOUT`, `persistence._DB_TIMEOUT` |
| Any socket `OSError` on read becomes `ReadError` | `.venv/…/httpcore/_backends/sync.py` (`SyncStream.read`) |
| `EAGAIN` renders exactly as the production text | in-process reproduction of `BlockingIOError(11, …)` |
| Unbounded dispatch, three independent sites | `db/client._run_sync`, `task_repository._run`/`_run_checked`, `persistence._run_sync` (all `asyncio.to_thread`) |
| Unbounded audit fan-out | `tools/executor.py` (per tool call), `engine/dispatcher.py` (per request), `runtime/manager.py` (per message) |
| Default executor size 32 here | measured: `cpu_count 48 → min(32, 52) = 32` |
| Dependency not present | `h2` is not in `backend/requirements.txt`, so production is HTTP/1.1 (no shared multiplexed socket assumption is made anywhere in the fix) |

---

## 5. Why the previous classification fix was insufficient

The previous phase (`2551970`) made the *reporting* truthful: a local `EAGAIN`
is now labelled `local_resource` and is no longer rendered as
"Supabase unavailable". That is correct and is preserved (a regression test
pins it), but it is attribution only — it left the process still able to pin
worker threads and pooled sockets for twelve times longer than the application
was willing to wait, and still able to create unbounded concurrent dispatches
and audit tasks. The symptom stayed truthful **and** recurring.

---

## 6. Exact files changed

| File | Change |
|---|---|
| `backend/db/client.py` | Pinned the shared client's transport deadline; added the one bounded reusable dispatch (`run_sync_db`) + deterministic pool shutdown |
| `backend/ai/database/task_repository.py` | `_run` / `_run_checked` dispatch through `run_sync_db` |
| `backend/ai/persistence.py` | `_run_sync` dispatches through `run_sync_db`; added bounded, counted `schedule_audit` |
| `backend/ai/tools/executor.py` | Audit write goes through `schedule_audit` (removed now-unused import) |
| `backend/ai/engine/dispatcher.py` | Usage persistence goes through `schedule_audit`; docstring corrected |
| `backend/ai/runtime/manager.py` | Message persistence goes through `schedule_audit` |
| `backend/runtime/supervisor.py` | Deterministic DB-pool shutdown in `stop()` |
| `tests/test_local_resource_bounds.py` | **New** — 12 focused regression tests |

No file outside this list was modified. No provider, NaraRouter, model
discovery, Test Modules, Telegram UI, Save, Bio/Username, task-parsing,
scheduler or schema file was touched.

---

## 7. Exact behavioral changes

1. **One bounded, owned, reusable pool for synchronous Supabase work.**
   `db/client.run_sync_db(fn, *args, timeout=…)` runs the call on a dedicated
   `ThreadPoolExecutor(max_workers=4, thread_name_prefix="lifeos-supabase")`.
   Threads are **reused across calls** and the pool is created lazily under a
   lock. `shutdown_db_executor()` releases it deterministically; a later caller
   transparently re-creates it, so no in-flight persistence path can observe
   "cannot schedule new futures after shutdown".
2. **The transport deadline is pinned below the application's dispatch budget.**
   The shared client is now built with
   `ClientOptions(postgrest_client_timeout=_DB_HTTP_TIMEOUT)` where
   `_DB_HTTP_TIMEOUT = 8.0 < _DB_TIMEOUT = 10.0`. A synchronous call can
   therefore no longer hold its thread and connection for 120s after the
   operation that owns it has already degraded.
3. **Task and AI persistence use that one pool.**
   `SupabaseTaskRepository._run`/`_run_checked` and `persistence._run_sync` no
   longer draw from the loop's shared default executor.
4. **Audit persistence is bounded and counted.**
   `persistence.schedule_audit(factory, name=…)` schedules best-effort audit
   work only while fewer than `_AUDIT_MAX_INFLIGHT = 8` records are in flight;
   on saturation it **drops** the record, increments `audit_dropped()`, logs
   `AI audit persistence saturated — dropped record name=… inflight=… dropped_total=…`,
   and returns `False`. `factory` is only invoked when the record is actually
   scheduled, so a drop never leaves an unawaited coroutine behind.

Nothing else changed: fallback honesty, `durable` flags, owner isolation, task
ids, occurrence uniqueness, version/CAS semantics, deletion semantics, bounded
attempts and transition rules are untouched and their suites stay green.

---

## 8. Task persistence impact

Every task/occurrence read, write, transition and delete now runs on the single
bounded pool with the same `DB_TIMEOUT` budget and the same error handling,
including the `_run_checked` variant that distinguishes a real row removal from
a swallowed transport failure. Concurrency is capped at 4 instead of being
drawn unboundedly from a 32-worker shared pool, and a slow store can no longer
retain a thread for 120s.

Fallback semantics are unchanged: a local resource errno still classifies as
`FALLBACK_REASON_LOCAL_RESOURCE`, a genuine transport failure still classifies
as `FALLBACK_REASON_UNAVAILABLE`, and both still degrade to the in-memory
fallback as non-durable.

---

## 9. `record_tool_call` impact

`record_tool_call` remains fire-and-forget and **decoupled**: the primary
execution path never awaits it, never reads its result, and cannot fail because
of it. What changed is that the dispatch is now bounded and counted instead of
being an unbounded `guarded_create_task(...)` per tool call, and the write
itself runs on the same bounded Supabase pool. Under saturation the record is
dropped (counted + logged) rather than queued without limit.

---

## 10. Resource lifecycle impact

| Resource | Before | After |
|---|---|---|
| Worker threads for Supabase HTTP | drawn per call from the loop's shared default executor (32 here), also used by every other subsystem | one named pool of 4, reused across calls |
| Thread/connection retention on a slow store | up to 120s (transport default) after a 10s application deadline | ≤ 8s transport deadline, below the 10s dispatch budget |
| Sockets kept alive by concurrent Supabase calls | unbounded with dispatch rate | ≤ 4 concurrent calls on the one shared client |
| Background audit/persistence tasks | one unmanaged task per tool call / request / message | hard in-flight bound of 8, with counted drops |
| Pool shutdown | none (default executor, implicit) | deterministic `shutdown_db_executor()` in `Supervisor.stop()` |

---

## 11. Tests and results

New file `tests/test_local_resource_bounds.py` — **12 tests, all passing**:

- transport deadline is strictly below every dispatch budget (`db`, task, persistence);
- the shared client is created with an explicit `postgrest_client_timeout == _DB_HTTP_TIMEOUT` (**would fail on the old code**, which inherited 120s), plus an explicit `< 120` assertion;
- 24 sequential dispatches are served by ≤ `_DB_MAX_WORKERS` reused threads;
- **concurrency regression**: 3 × `_DB_MAX_WORKERS` concurrent dispatches whose real peak is asserted `== _DB_MAX_WORKERS` — verified to **fail on the old implementation** (measured peak **12** of 12 with `asyncio.to_thread`, i.e. unbounded relative to the pool bound);
- deterministic shutdown, and re-creation afterwards (no "cannot schedule new futures after shutdown");
- the task repository and the AI persistence layer each dispatch through `run_sync_db` with their own timeout;
- audit scheduling: bounded, counted drop, factory never invoked for a dropped record, in-flight returns to 0, failures never reach the caller;
- a **real `ToolExecutor` run** still returns `success=True` while the audit path is saturated;
- a failed `record_tool_call` leaves task persistence durable and `fallback_reason == ""`;
- `OSError(EAGAIN)` still classifies as `FALLBACK_REASON_LOCAL_RESOURCE` (previous phase preserved).

Results:

```
focused (tests/test_local_resource_bounds.py):  12 passed
full suite (pytest tests -q):                   2186 passed, 24 skipped, 0 failed
```

The EAGAIN *shape* is simulated (this workspace never actually exhausts the
kernel); the **dispatch, retention and lifecycle logic under test is the real
production code**.

---

## 12. `py_compile` result

`python -m py_compile` on every changed file (plus the new test) — **OK**.

---

## 13. `git diff --check` result

**Clean** (no whitespace errors). Diff scope verified: 7 modified files + 1 new
test file; `git status --porcelain` contains no SQL, migration or schema path.

---

## 14. Live verification status

**Live Render verification: NOT performed.** No production credentials or
telemetry access exist in this workspace, and no claim of production behaviour
is made.

What would confirm the fix in production, in order of value:

1. **No recurrence of the classification line.** The
   `TASK_FALLBACK_CLASSIFIED reason=local_resource …` / `Supabase occurrence
   read failed; using fallback: [Errno 11] …` pair should stop appearing while
   the task system keeps creating and running tasks durably.
2. **Transport deadline observed.** A slow store should now log postgrest
   timeouts (`ReadTimeout`) at roughly the new ~8s transport deadline, followed
   by a truthful degradation — no 120s thread retention.
3. **Audit saturation, if it happens at all**, is now visible as
   `AI audit persistence saturated — dropped record name=… inflight=8 dropped_total=…`
   instead of silent growth.
4. If `[Errno 11]` **still** appears after this deploy, the trigger is outside
   this fix's scope and is then provable from the log context: the new bounded
   pool + 8s transport deadline means the remaining candidates are a
   socket-timeout race or a leaked non-Supabase client reusing the fd (see §15,
   item 3).

---

## 15. Remaining limitations

1. **The trigger is still unproven.** The fix removes the *retention and
   amplification* defect that made the condition recurring and self-worsening.
   It does not name the single line that puts a socket in the non-blocking state
   that yields `EAGAIN`. That requires the live evidence above.
2. **Other `asyncio.to_thread` call sites remain** (`usage_reader`,
   `usage_recorder`, `config_store`, `ghost_seen_v2`, `database_service`,
   `tools/memory`, `runtime/memory_cleanup`). They were converted in an earlier
   draft of this phase and **deliberately reverted**: `tests/test_40_usage_read_side.py::test_no_direct_supabase_access_outside_repository_layer`
   enforces that those layers reach data only through the repository layer and
   must not import `backend.db.client`, and routing them through `run_sync_db`
   would violate that documented separation. They are rate-bounded per request
   rather than unbounded multipliers, so they are not the fan-out the fix
   targets; converting them is a separate architectural decision for the
   repository layer, not this phase.
3. **Proven socket/FD leak, out of this phase's scope.** The AI providers'
   `shutdown()` (`openai_compat.py`, `gemini.py`, `you_search.py`) sets
   `self._http_client = None` **without closing** the `httpx.AsyncClient`, and
   `model_tester.test_single_model` calls it in a `finally` for every
   provider × model test — one abandoned connection pool per test. Closing it
   requires an `await` (`aclose()`), i.e. a contract change to
   `BaseProvider.shutdown()`, and provider architecture / Test Modules are
   explicitly excluded from this phase. **Recommended as the next phase.**
4. **`_DB_MAX_WORKERS = 4`** is a deliberate bound, not a tuned value. If
   Supabase round-trip latency ever exceeds the dispatch budget often enough to
   queue work visibly, the correct response is to re-tune this single constant
   together with `_DB_HTTP_TIMEOUT`, not to raise thread counts ad hoc.

---

## 16. Delivery record

| Item | Value |
|---|---|
| Implementation commit | `40b57e0d9fc6a21f60b31fddb1c67eb8d12f025a` — `fix: bound the synchronous Supabase resource lifecycle` |
| Files in commit | `backend/db/client.py`, `backend/ai/database/task_repository.py`, `backend/ai/persistence.py`, `backend/ai/tools/executor.py`, `backend/ai/engine/dispatcher.py`, `backend/ai/runtime/manager.py`, `backend/runtime/supervisor.py`, `tests/test_local_resource_bounds.py`, `IMPLEMENTATION_REPORT.md` |
| Push | `ec93345..40b57e0  main -> main` (no force) |
| Local HEAD | `40b57e0d9fc6a21f60b31fddb1c67eb8d12f025a` |
| `origin/main` | `40b57e0d9fc6a21f60b31fddb1c67eb8d12f025a` |
| `git ls-remote origin refs/heads/main` | `40b57e0d9fc6a21f60b31fddb1c67eb8d12f025a` |
| `git rev-list --left-right --count HEAD...origin/main` | `0 0` |
| Working tree | clean (`git status --short` empty) |
| Pre-commit checks | `py_compile` OK · `git diff --check` clean · full suite 2186 passed / 24 skipped / 0 failed |
