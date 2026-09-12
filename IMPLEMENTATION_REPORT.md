# IMPLEMENTATION REPORT — CURRENT STATE

> **This is a CURRENT-STATE document.** It describes the repository as it
> exists at the tip of this phase. If code changes invalidate any section,
> update this document in the same commit. Previous phase reports are kept
> verbatim below under `ARCHIVE - PREVIOUS PHASE REPORTS`.

---

## Phase - Underspecified NL task requests route to the Taskloom wizard + honest empty-response delivery

### Metadata

| Item | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Branch | `main` |
| Starting HEAD | `21968206b3329f87fb1047b85439a778880d98ca` |
| Phase | **Deterministic task-creation completeness gate + whitespace-response delivery fix** |
| Status | **IMPLEMENTED and delivered** - focused, relevant, and full in-process suites green |
| Database impact | **NONE** (no schema, migration, RLS, table, index, or SQL file touched) |
| Live Render/Telegram verification | **NOT performed** (no production session or telemetry access in this workspace) |

### Exact production problem and source-traced causes

**Live reproduction:** the owner sent `یه تسک برای بیو بساز`. The wizard did not open; a task
was persisted to Supabase (`task_id=9`, `action_names=bio_set_text`, `schedule_type=interval`,
`fallback_reason=""`), the AI answer was normalized to a whitespace-only value, and the request
message was left as the header-only shell (`
────────────
🤖 پری`).

1. **The tool had no deterministic completeness boundary before the provider.**
   `backend/ai/tools/task.py::CreateTaskTool._execute` set
   `data.open_taskloom_wizard=true` only for `candidate_invalid*` and
   `unsupported_capability`. `backend/ai/task_interpreter.py` instructs the model to return a
   candidate whenever it recognises an action (returning `null` only for pure chit-chat), and
   `backend/ai/task_candidate.py::TaskCandidate.from_untrusted` validates only *structure*.
   For an underspecified request the model therefore invented `schedule_type=interval`,
   `schedule={"seconds": 120}` and a content action, the candidate validated, and it was
   persisted. There was no check that the owner's own request had actually expressed the
   required creation fields.
2. **A whitespace-only AI response was delivered as a broken shell.**
   `backend/ai/tools/delivery.py::deliver_response` gated only on `if not response_text:`, so a
   whitespace-only string (`" "`) passed. `process_output` then raised `ValueError`, the
   `AI_OUTPUT_NORMALIZATION_FALLBACK` warning was logged, and the header-only message was still
   sent to the owner.

### Exact implementation

1. **Deterministic completeness gate** (runs *before* any provider call), in
   `CreateTaskTool._execute`. It reuses the existing conservative scheduling vocabulary in
   `backend/ai/actions.py` — no second parser, no phrase table. A request is considered to have
   expressed a schedule when any of these hold: `_is_scheduling_intent(words,
   require_action_verb=False)`, a clock anchor (`ساعت` / `am` / `pm` / `at` / `H:MM`), an
   event intent, or a bare `number + time-unit` pair. When none holds, the tool returns without
   calling the provider:

   ```
   ToolResult(success=False,
              message="I need a few structured choices for this task — pick them in the creation form below.",
              data={"open_taskloom_wizard": True, "wizard_reason": "incomplete_request"})
   ```

   This is the **same** structured signal the previously delivered delivery-layer bridge already
   consumes, so `backend/bot/handlers/ai_unified.py` opens the **existing** Taskloom wizard
   (`taskloom_new`) for the owner. No new wizard, no new persistence path: the wizard still
   converges `TaskDraft -> build_candidate() -> TaskCandidate.from_untrusted() ->
   TaskCreationService -> TaskRepository`.
2. **`backend/ai/actions.py`** — added `annually` to `_EN_RECUR_WORDS`, added
   `_FA_CLOCK_WORDS` / `_EN_CLOCK_WORDS` (time-of-day anchors), and added the
   `require_action_verb: bool = True` flag to `_is_scheduling_intent` so a bare cadence
   expression still proves a schedule. Command routing behaviour is unchanged (default `True`).
3. **`backend/bot/handlers/ai_unified.py`** — `_wizard_notice` renders a dedicated
   `incomplete_request` notice so the text fallback (used only when the panel cannot be sent)
   stays actionable.
4. **`backend/ai/tools/delivery.py`** — `deliver_response` now treats a non-`str` or
   whitespace-only response as **no** response (the existing honest `AI returned no response.`
   path) instead of shelling out. The normalization fallback warning now carries a
   content-free `nonempty_after_strip=` classification and never echoes raw AI output or
   secrets. The `ValueError` is not hidden.

### Behaviour split (critical distinction preserved)

- **A — Complete NL request** (e.g. `هر ۲ دقیقه بیو رو آپدیت کن به یه دیالوگ رندوم از آیانامی ری`):
  passes the gate, uses the existing `TaskInterpreter -> TaskCandidate -> TaskCreationService ->
  TaskRepository` path unchanged.
- **B — Underspecified request** (e.g. `یه تسک برای بیو بساز`): never reaches the provider, no
  task is created, the existing Taskloom creation wizard is opened. No untrusted value is
  prefilled.

### Files changed

| File | Change |
|---|---|
| `backend/ai/tools/task.py` | deterministic pre-provider completeness gate + `incomplete_request` signal |
| `backend/ai/actions.py` | `annually`; clock-word sets; `require_action_verb` parameter |
| `backend/ai/tools/delivery.py` | whitespace-only response is `no response`; content-free fallback classification |
| `backend/bot/handlers/ai_unified.py` | `incomplete_request` wizard notice |
| `tests/test_task_wizard_nl_bridge.py` | live-reproduction + invented-schedule + delivery regression tests |
| `tests/test_task_nl_interval_creation.py` | ambiguous request now asserts the wizard signal, no task |
| `tests/test_task_semantic_triggers.py` | genuine-ambiguity cases assert no task + wizard signal |
| `IMPLEMENTATION_REPORT.md` | this phase report |

### Test results

- Focused: `tests/test_task_wizard_nl_bridge.py tests/test_task_wizard.py tests/test_task_semantic_triggers.py tests/test_task_nl_interval_creation.py` — **160 passed**
- Full suite: `pytest tests -q` — **2254 passed, 24 skipped, 0 failed**
- `py_compile` for every changed Python file — **OK**
- `git diff --check` — **clean**

Regression coverage added: `test_live_underspecified_request_never_creates_and_signals_the_wizard`,
`test_invented_schedule_for_underspecified_request_is_never_persisted`,
`test_english_underspecified_request_signals_the_wizard`,
`test_whitespace_only_ai_response_is_not_delivered_as_a_shell`,
`test_normalization_failure_is_logged_and_still_delivered`,
`test_incomplete_request_delivery_preference`, plus the preserved panel-send / fallback-hint
tests. No existing validation, source-attribution, language, schedule, persistence, or Bio
Guardian behaviour was weakened.

### Limitations

- The completeness gate is deterministic and conservative: a request that expresses *a*
  schedule but leaves content/constraints unstated still goes through the interpreter, which may
  fill optional fields. Only the structurally required schedule boundary is enforced here.
- **Live Telegram/Render verification was NOT performed in this coding workspace.**

---

## Phase - Taskloom creation bridge, local-resource cooldown, and runtime diagnostics correctness

### Metadata

| Item | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Branch | `main` |
| Starting HEAD | `bf192d887e9121b7dc4b5cd0be00ddc1fd2c3908` |
| Phase | **Taskloom UX bridge + EAGAIN pressure protection + false-positive diagnostics fix** |
| Status | **IMPLEMENTED and delivered** - focused and full in-process suites green |
| Database impact | **NONE** (no schema, migration, RLS, table, index, or SQL file touched) |
| Live Render/Telegram verification | **NOT performed** (no production session or telemetry access in this workspace) |
| Delivery record | implementation commit `4b20653ac9045f6d0c31aa75a0e11717b7f5a3dd` is already on `origin/main`; final delivery metadata update pending |

### Exact production problems and source-traced causes

1. **Natural-language Taskloom requests stopped before the existing wizard.**
   `CreateTaskTool` caught `TaskUnsupportedError` and returned only a refusal, while
   candidate-level interpretation failures returned the generic rejection. Neither
   result carried a signal to the Telegram delivery layer, so the already-implemented
   `taskloom_new` panel was unreachable from the natural-language path.
2. **Persistent local EAGAIN caused an event-query storm.**
   `TaskEventDispatcher.handle_event()` calls `SupabaseTaskRepository.list_event_tasks()`
   for every incoming Telegram event. The repository correctly classified the local
   transport error, but each event immediately retried the same failing Supabase
   operation and emitted another warning pair. There was no repository-level local
   resource protection state.
3. **Runtime diagnostics classified deliberately long-lived work as bounded.**
   The classifier knew only a partial set of LifeOS names/coroutine patterns. Telethon's
   auto-named `_update_loop`, `_recv_loop`, and `_send_loop`, the named Taskloom/profile
   schedulers, helper/web wrappers, and stale state for completed tasks were therefore
   eligible for `TASK_NO_PROGRESS` / `TASK_STARVATION` despite fresh event/RPC and loop
   telemetry.
4. **The health heartbeat timestamp was write-once.**
   `backend.health.mark_started()` initialized `_last_heartbeat`, but no runtime loop
   refreshed it. The heartbeat loop reported progress through `tick_loop()` without
   updating the canonical health timestamp, so `heartbeat_age_s`, `process_alive`, and
   the failsafe signal could remain stale during healthy runtime. The 15-second stale
   threshold was also shorter than the 30-second heartbeat interval.

### Exact implementation

**Natural-language -> existing Taskloom wizard.** `CreateTaskTool` now marks only
unsupported-capability and candidate-level interpretation failures with
`data.open_taskloom_wizard=true`; provider, timeout, and persistence failures remain
ordinary failures. `ai_unified` consumes that structured result, resets the existing
per-owner `taskloom_new` draft, and opens it through the existing
`send_inline_panel`/panel lifecycle. Opening the wizard creates no task and does not
prefill untrusted values. If the helper panel cannot be sent, the normal response
includes an actionable `Menu -> Taskloom -> + New task` hint. Fully representable NL
requests continue through the existing `TaskInterpreter -> TaskCandidate ->
TaskCreationService -> TaskRepository` path.

**Bounded local-resource cooldown.** `SupabaseTaskRepository` now owns a monotonic,
5-second cooldown after a confirmed local-resource failure. During the window its
existing in-memory fallback is served without dispatching another Supabase request.
The deadline is not extended by skipped calls, expires automatically, and is cleared
immediately by any successful durable operation. Genuine Supabase/store failures do
not enter the cooldown and remain observable on every attempted operation. The
existing `local_resource`/`unavailable` classification and non-durable fallback
honesty are unchanged. Warning output is deduplicated to one classification/request
pair per continuous local-resource episode; no retry loop, client, pool, scheduler,
or persistence path was added.

**Diagnostics and health.** Permanent classification now covers the actual runtime
names, Taskloom/profile schedulers, helper/web wrappers, and Telethon transport/update
coroutines and source frames. Starvation now considers only tasks still pending in
the current dump, pruning completed-task state. Genuine bounded unchanged-task and
event-loop-latency detection remain active. `health.set_heartbeat()` is the single
canonical timestamp writer after startup and is called by the existing heartbeat
loop; the stale threshold is 90 seconds, longer than the 30-second beat interval but
still bounded for real failure detection.

### Exact files changed

| File | Change |
|---|---|
| `backend/ai/tools/task.py` | structured wizard signal for unsupported/incomplete task interpretation |
| `backend/bot/handlers/ai_unified.py` | opens the existing owner-scoped Taskloom wizard, with text fallback |
| `backend/bot/handlers/taskloom.py` | exposes the existing wizard query/reset entry for the bridge |
| `backend/ai/database/task_repository.py` | bounded local-resource cooldown and episode-scoped warning deduplication |
| `backend/runtime/diagnostics.py` | correct permanent-loop classification and live-task starvation filtering |
| `backend/health.py` | canonical heartbeat writer and interval-aligned stale threshold |
| `backend/runtime/heartbeat.py` | refreshes the canonical heartbeat timestamp through the existing loop |
| `tests/test_task_wizard_nl_bridge.py` | 9 focused NL bridge/owner/no-persistence tests |
| `tests/test_task_fallback_cooldown.py` | 8 cooldown, recovery, classification, and event-pressure tests |
| `tests/test_runtime_diagnostics_classification.py` | 11 diagnostics and heartbeat tests |
| `tests/test_task_fallback_classification.py` | updates existing recovery assertion for the bounded window |
| `IMPLEMENTATION_REPORT.md` | this current-state phase report |

No provider, NaraRouter, model-testing, scheduler architecture, ToolExecutor,
Supabase schema/migration, or unrelated UI file was changed.

### Test results

- Focused Taskloom bridge: `tests/test_task_wizard_nl_bridge.py` - **9 passed**.
- Focused local-resource protection: `tests/test_task_fallback_cooldown.py` - **8 passed**; existing `tests/test_task_fallback_classification.py` - **10 passed**.
- Focused diagnostics/health: `tests/test_runtime_diagnostics_classification.py` - **11 passed**.
- Combined relevant regression selection: **141 passed**.
- Full suite: `.venv/bin/python -m pytest tests -q` - **2248 passed, 24 skipped, 0 failed** (3 dependency deprecation warnings only).
- `py_compile` - **OK** for every changed/added Python file.
- `git diff --check` - **clean**.

### Remaining limitations

1. Live Render/Telegram verification was not performed in this coding workspace. The
   next deployed runtime must confirm that the new `TASK_FALLBACK_CLASSIFIED` episode
   behavior and `TASK_EVENT_TRACE` volume match the in-process contract.
2. The cooldown intentionally serves the existing non-durable fallback while the
   local condition persists. It prevents request amplification; it does not make
   fallback data durable or claim that a failed durable read succeeded.
3. The NL bridge opens a blank structured wizard rather than guessing values from an
   unsupported request. This is deliberate: only explicit owner selections become
   the durable candidate.

### Delivery record

| Item | Value |
|---|---|
| Starting HEAD | `bf192d887e9121b7dc4b5cd0be00ddc1fd2c3908` |
| Final implementation commit | `4b20653ac9045f6d0c31aa75a0e11717b7f5a3dd` — `fix: bridge Taskloom clarification and bound runtime diagnostics` |
| Push | already present on `origin/main`; no push required |
| Remote verification | `HEAD` == `origin/main` == `git ls-remote origin refs/heads/main` == `4b20653ac9045f6d0c31aa75a0e11717b7f5a3dd`; `git rev-list --left-right --count HEAD...origin/main` = `0 0`; working tree clean after this report update |

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
