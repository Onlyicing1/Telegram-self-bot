# AI Tool + Task/Event Execution Investigation

## Metadata

| Field | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Branch | `main` |
| Source baseline | `c0abadeb6af0a2d773ce0e51c0b60125d4cf1eb3` |
| Scope | AI tool execution and durable time/event task execution |
| Evidence | Current source and existing in-process regression tests |
| Live status | Live Telegram, provider-network, and live Supabase execution were not performed |
| Change type | Documentation-only investigation handoff |

This document transfers the completed audit into the repository. It is not a redesign proposal. Current source is authoritative; older reports and planned architecture are not proof of runtime behavior.

## VERDICT

The core AI-tool and task/event execution architecture is implemented and connected. Interactive AI requests, time-based tasks, and Telegram event-based tasks converge on one registered-tool boundary:

```text
AI request → Dispatcher/provider selection → ToolRegistry → ToolExecutor
  → registered Tool → existing service → TelegramAPI/self-client or Supabase
  → ToolResult

create_task → TaskInterpreter → TaskCandidate → TaskCreationService → repository
TaskScheduler → TaskExecutionCoordinator → ToolExecutor
task_events → TaskEventDispatcher → TaskExecutionCoordinator → ToolExecutor
```

No second independent AI tool executor or task executor was found. No architecture replacement is justified by this audit.

## Classification summary

| Classification | Source-backed result |
|---|---|
| **CONFIRMED** | `ToolRegistry → ToolExecutor → registered tool → service` is the single AI execution authority. |
| **CONFIRMED** | Provider-native calls, validated JSON actions, and applicable deterministic routes converge on that authority. |
| **CONFIRMED** | Task creation is `create_task → TaskInterpreter → TaskCandidate → TaskCreationService`. |
| **CONFIRMED** | Time and event execution share `TaskExecutionCoordinator` and `ToolExecutor`. |
| **CONFIRMED** | Stored task actions are checked against `ToolRegistry` at execution time and fail closed if unregistered. |
| **CONFIRMED** | Event execution has bounded task/execution counts and deterministic occurrence deduplication. |
| **CONFIRMED** | Scheduled/event execution is silent unless notification or result delivery is explicitly enabled. |
| **LIKELY RISK** | Static action snapshots can become stale after a tool name or contract changes; the safe result is a failed occurrence. |
| **LIKELY RISK** | Natural-language task creation depends on provider availability, structured output, and unambiguous intent. |
| **UNKNOWN** | Live Telegram/Supabase effects, provider-network behavior, and deployment credentials/configuration. |
| **NOT FOUND** | Any AI path to arbitrary Telegram RPC, SQL, shell, filesystem access, or model-selected destinations outside trusted context. |

## 1. AI tool execution

`backend/ai/tools/registry.py` is the capability boundary:

- `ToolRegistry.register()` stores canonical names and rejects duplicates.
- `get()` and `has()` are used for lookup and validation.
- `list_schemas()` exposes descriptions, parameters, permissions, and return types.
- `create_default_registry(context)` registers the built-in tools, including task management, saved-item retrieval, and memory tools.

`backend/ai/engine/dispatcher.py::Dispatcher._build_tool_definitions()` exposes registry schemas as provider-facing function definitions. Native provider calls are parsed and validated before execution. Structured JSON actions use `parse_action_text()` → `validate_action()` → `resolve_tool_calls()` and converge on the same executor. Bounded deterministic handling does not create another execution authority.

`backend/ai/tools/executor.py` is the sole execution authority:

- `ToolExecutor.execute_calls()` enforces the per-turn limit and executes calls sequentially.
- `_execute_single()` validates argument shape, performs registry lookup, applies permission and timeout rules, contains errors, records history, and returns a structured result.
- `execute_confirmed()` is only for a previously stored and explicitly confirmed administrative call and still uses the same execution checks.
- Unknown tools return an error and are never executed.

`ProviderManager` remains provider routing/fallback authority. `Dispatcher` remains AI orchestration authority. Tools remain adapters over the existing service layer; profile/bio and other AI-assisted operations do not create a second AI path.

## 2. Task creation

| Stage | Source/function | Confirmed responsibility |
|---|---|---|
| Entry | `backend/ai/tools/task.py::CreateTaskTool.execute()` | Accepts a bounded natural-language task request. |
| Interpretation | `backend/ai/task_interpreter.py::TaskInterpreter.interpret()` | Uses the provider with `tools=[]`; ambiguity/failure creates no task. |
| Validation | `backend/ai/task_candidate.py::TaskCandidate.from_untrusted()` / `parse_candidate_output()` | Validates fields, schedules, actions, triggers, and notification data. |
| Creation | `backend/ai/task_creation.py::TaskCreationService.create()` | Revalidates schedules, computes the initial run, and persists under the authoritative owner id. |
| Storage | `backend/ai/database/task_repository.py` | Stores versioned definitions and occurrence state. |

`backend/ai/task_candidate.py::_canonicalize_action()` accepts `send`, `send_message`, `write_message`, and `send_text` only as bounded message-writing aliases and canonicalizes them to `send_message` with text only. Other registered tool names must match exactly. Destinations and owner identity are trusted runtime data, never model-generated arbitrary ids.

## 3. Time-based task execution

`backend/ai/task_scheduler.py::TaskScheduler` is the process-local scheduler. `start()` recovers interrupted occurrences and starts the loop. `run_once()` reads due tasks, parses schedules, creates/claims occurrences, and hands them to the coordinator. `_execute_claimed()` performs the repository claim and invokes the configured coordinator. Retry, recovery, catch-up, and per-wake work are bounded.

`backend/ai/task_execution.py::TaskExecutionCoordinator.execute()` verifies owner and occurrence state, obtains fresh trusted client context when available, resolves the trusted destination, validates the stored action snapshot, checks every action against `ToolRegistry`, calls the same `ToolExecutor.execute_calls()` used by interactive AI, persists the outcome, and delivers a result only when explicitly enabled.

## 4. Event-based task execution

`backend/bot/handlers/task_events.py` is registered through `backend/bot/router.py` and rides the existing Telethon update path; it does not create a second update loop.

- `task_events.register()` installs one `events.NewMessage()` handler for both directions.
- `extract_event_context()` forwards bounded event metadata and bounded text, not a raw RPC surface.
- `TaskEventDispatcher.handle_event()` evaluates a bounded set of active event tasks.
- `backend/ai/task_trigger.py::event_trigger_matches()` performs deterministic matching against resolved sender/chat/content/media/reply/direction conditions; it never calls an LLM.
- `event_occurrence_key()` creates `<task_id>:ev:<chat_id>:<message_id>`.
- Repository uniqueness/CAS handling and `_dispatch()` prevent duplicate execution on redelivery or a race.
- `MAX_EVENT_TASKS_PER_MESSAGE` and `MAX_EVENT_EXECUTIONS_PER_MESSAGE` bound work per update.
- Matched occurrences use the same `TaskExecutionCoordinator` as `TaskScheduler` and therefore the same `ToolExecutor`.

## 5. Authority and security boundaries

The confirmed ownership model is:

- `RuntimeSupervisor` — runtime lifecycle and wiring.
- `ProviderManager` — provider selection and fallback.
- `Dispatcher` — AI orchestration and tool-loop coordination.
- `ToolRegistry` — canonical capability set.
- `ToolExecutor` — sole caller of registered tool implementations.
- Domain services — business logic.
- `TelegramAPI` and the authenticated self-client — Telegram execution boundary.
- Existing database/repository layers — Supabase access and fallback behavior.

The audit found no AI-reachable path for arbitrary Telethon RPC, arbitrary SQL, shell/subprocess execution, filesystem access, or model-controlled destinations outside trusted request/task context. Event sender/chat identities are resolved from trusted runtime context; model-facing numeric identities are rejected. Some actions require AI interpretation, but interpretation remains inside the Dispatcher → registered Tool → ToolExecutor architecture.

## 6. Notification behavior

Scheduled and event-driven execution is silent by default. `backend/ai/task_interpreter.py` enables `deliver_result` or `notify_on_outcome` only when explicitly requested. `TaskExecutionCoordinator._deliver_result()` checks the persisted opt-in flag and trusted destination. The scheduler and event dispatcher use the shared notifier for persisted outcomes and do not create unsolicited status messages.

## 7. Likely risks and limitations

1. **Static action snapshot:** a task persists `name + arguments` at creation time and does not ask the AI to reinterpret the request at every occurrence. A later tool rename or contract change can fail closed as a failed occurrence.
2. **Provider-dependent interpretation:** provider failure, invalid configuration, malformed structured output, or ambiguity prevents task creation rather than creating an unsafe task.
3. **Canonical-name strictness:** outside the bounded send aliases, near-miss action names are rejected. This protects the boundary but can be user-visible.
4. **Silent outcomes:** failed scheduled/event actions are not sent to Telegram unless notification was explicitly enabled; task state, occurrences, and logs are the operational record.
5. **Bounded event fan-out:** the event path caps tasks examined and executions started per update, intentionally preventing unbounded work.

## 8. Unknown / live-unverified behavior

No live Telegram or Supabase execution was performed. The following remain unverified:

- a real Telegram event causing an event task to run;
- a real Telegram side effect from a scheduled or event task;
- Supabase persistence, occurrence uniqueness, claim/CAS transitions, and recovery against the deployed database;
- native tool-call serialization for every configured provider;
- deployment provider availability, credentials, quotas, rate limits, and fallback behavior;
- behavior during a real Telegram reconnect/rebuild while a task is running; and
- correctness for every natural-language or Persian phrasing in a production model.

Existing in-process and dummy-provider tests verify local contracts only; they do not establish these external behaviors. No live Telegram verification is claimed.

## 9. Remaining implementation work

No architecture replacement is required. Remaining work is deliberately bounded:

1. Perform a controlled live Telegram verification with an owner-scoped, reversible test task.
2. Perform opt-in live Supabase verification of task/occurrence persistence, uniqueness, claim/CAS behavior, and recovery when approved test data and credentials are available.
3. Validate provider-specific native tool behavior in deployment.
4. Add compatibility coverage only if requirements demand survival across future tool renames; never bypass registry validation or add a second executor.
5. Improve visibility, if desired, through existing task inspection, occurrence, logging, and opt-in notification boundaries.

## 10. In-process coverage referenced

- `tests/test_capability_exposure_tools.py`, `tests/test_tool_health_audit.py`, `tests/test_10_tool_calls.py` — registry exposure and execution.
- `tests/test_19_ai_actions.py`, `tests/test_new_tool_action_path.py` — validated action routing.
- `tests/test_task_nl_creation.py`, `tests/test_task_candidate_contract.py`, `tests/test_stage12_interpreter.py` — interpretation and validation.
- `tests/test_task_execution.py`, `tests/test_task_send_execution.py`, `tests/test_task_scheduler.py` — shared scheduled execution.
- `tests/test_task_trigger_events.py` — event matching, bounds, and deduplication.
- `tests/test_task_management.py`, `tests/test_task_repository.py` — lifecycle and persistence contracts.

These are source/in-process checks, not live Telegram, provider-network, or live Supabase verification.

## Delivery scope

Only `INVESTIGATION.md` is intended to change for this handoff. No backend code, tests, provider configuration, environment file, database schema, migration, or deployment setting is changed. The pre-existing untracked nested `telegram-self-bot/` checkout is unrelated and remains untouched.

---

# Phase 2 Audit — AI Task/Scheduler + Tool Execution Pipeline (2026-09-08)

A NEW source-first audit of the complete existing task/scheduler/tool pipeline on the
current `main` baseline. No code was modified during this phase. The Phase 1 findings
above were re-verified against current source; every claim below is re-derived from
the actual files, not carried forward.

## VERDICT (Phase 2)

The pipeline is implemented, connected, and internally consistent end-to-end.
Nothing is broken in the traced paths. Two confirmed architectural gaps (dormant
`ai_instruction`/`preparation_metadata` machinery; event-direction asymmetry) and
several bounded risks exist — all are limitations, not defects requiring redesign.

## Primary question matrix

| # | Question | Verdict | Evidence (path → mechanism) |
|---|---|---|---|
| A | Create durable task with registered tool action | WORKS | `ai/tools/task.py::CreateTaskTool.execute` → `ai/task_interpreter.py::TaskInterpreter.interpret` → `ai/task_candidate.py::TaskCandidate.from_untrusted` → `ai/task_creation.py::TaskCreationService.create` → `ai/database/task_repository.py::create_task` |
| B | Persist action safely | WORKS | `task_repository._validate_task_input` (bounded JSONB, owner-scoped); schema `supabase/migrations/20260829000001_create_ai_tasks.sql` matches repository validation exactly |
| C | Wake at correct scheduled time | WORKS | `ai/task_scheduler.py::TaskScheduler.run_once` → `ai/scheduling.py::parse_schedule` + `catch_up_occurrence`; 60s wake interval; DST-safe `_localize` with nonexistent-time normalization |
| D | Exactly one occurrence created and claimed | WORKS | deterministic `occurrence_key` + unique index `uq_ai_task_occurrences_task_key` + CAS claim (`claim_occurrence` updates `.eq("status", current.status)`) |
| E | Execute through the SAME ToolExecutor as interactive AI | WORKS | `ai/task_execution.py::TaskExecutionCoordinator.execute` calls `executor.execute_calls(...)` — the identical authority the Dispatcher uses |
| F | Execute the correct registered tool | WORKS | `ai/tools/executor.py::_execute_single` registry lookup; unknown tool → structured failure, never executed |
| G | AI-reasoning tools from tasks | PARTIAL — gap confirmed (see §G below) |
| H | Event-triggered tasks execute the same way | WORKS | `bot/handlers/task_events.py` → `ai/task_event_dispatcher.py::TaskEventDispatcher` → same `TaskExecutionCoordinator` |
| I | Prevent duplicate execution | WORKS | unique index + dedup-status skip in `_dispatch` + CAS claim as the final race guard |
| J | Tool failure never reported as success | WORKS | executor never raises; failed `ToolResult` → `TaskExecutionCoordinator.handle_failure` → failed/retry occurrence |
| K | Three-attempt retry contract | WORKS | `ai/retry.py` `MAX_ATTEMPTS=3`, `can_retry`, 30s→60s backoff; `retry_pending` recovery pass on scheduler start |
| L | Owner isolation preserved | WORKS | every repository operation filters `.eq("owner_id", owner_id)`; owner identity never comes from model input |
| M | Survive scheduler polling / runtime lifecycle | WORKS | scheduler owned solely by `RuntimeSupervisor` (`_start_task_scheduler` at READY, `_stop_task_scheduler` in stop); recovery pass; bounded per-wake work |
| N | Telegram execution inside the Self Bot boundary | WORKS | only `TelegramAPI`/self-client; no arbitrary RPC/SQL/shell/filesystem reachability found from any task path |

## §G — The AI-assisted tool question

**Tool inventory (37 registered via `ai/tools/registry.py::create_default_registry`):**
all bio/username tools take fully deterministic arguments (template/text/mood
strings) — no AI reasoning is needed at execution time. The same holds for send,
save, delete, retrieve, settings, memory, and task-management tools. `web_search`
is deterministic per query. **No registered tool requires AI interpretation to
execute from a static action snapshot.**

**Supporting machinery for future AI-reasoning tools exists but is dormant:**
- The `ai_tasks.ai_instruction` column, `validate_ai_instruction`, and the
  `TaskCreationService` persistence path exist — but a repository-wide search finds
  **zero runtime consumers** (no reader of `ai_instruction` anywhere outside
  validation/persistence).
- The `preparation_metadata` / `PreparedAction` contract (`ai/task_contract.py`)
  is validated, persisted, and recovered — **no runtime producer or consumer**.
- Consequence: if a future tool needs model-generated content at occurrence time,
  the *transport* exists but the *invocation path* (coordinator → ProviderManager)
  does not. This is a confirmed architectural gap, currently latent.

**Confirmation gates:** `TaskExecutionCoordinator` calls `execute_calls` without
`confirmed=True`, so an ADMIN_ONLY/CONFIRMATION_REQUIRED action returns
`needs_confirmation`, is counted as a failure, retries, and fails after 3 attempts.
No registered task-relevant tool is ADMIN_ONLY, so no false block today — but it is
a latent trap if one is registered.

**No arbitrary authority:** `ai/tools/message.py::SendMessageTool` accepts only a
bounded `text` argument; the destination comes from trusted context
(`notification_destination.chat_id` or the owner's own chat). Trigger identities are
runtime-resolved (`ai/task_trigger.py::resolve_trigger_references`); model-invented
numeric ids are rejected at creation.

## Event-direction finding

`bot/handlers/task_events.py::register` subscribes to `events.NewMessage()` (both
directions), but `ai/task_trigger.py::_has_condition` treats `direction` as a
non-condition — so a trigger with `direction: "outgoing"` alone is rejected at
creation ("needs at least one condition"). Outgoing-trigger tasks are therefore not
creatable through the AI path. Deterministic and fail-closed, no safety issue — but
a functional limitation worth knowing.

## Incomplete / risks (ranked)

1. **Dormant AI-task contract** — `ai_instruction` and `preparation_metadata` are
   validated and persisted but never executed; dead weight until wired.
2. **Outgoing-only event triggers** are not creatable (condition rule above).
3. **Static action snapshot** — a later tool rename/contract change fails closed at
   execution time (by design; no compatibility layer exists).
4. **Silent failures** — no Telegram notification unless `notify_on_outcome`/
   `deliver_result` was explicitly opted in; occurrences and logs are the record.
5. **Supabase fallback divergence** — `SupabaseTaskRepository` degrades to ONE shared
   in-memory fallback (good), but a fallback write is non-durable and lost on
   restart; `fallback_active` is surfaced for diagnostics but not user-visible.
6. **Provider-dependent creation** — interpretation failures prevent creation
   (fail-closed, correct behavior).
7. **Event list bound** — `MAX_EVENT_TASKS_PER_MESSAGE = 20`; a 21st+ active event
   task is silently never matched for that message.
8. **Encapsulation smell** — `TaskExecutionCoordinator` reaches into
   `executor._registry` (private attribute) for its fail-closed registry check.

## Ruled out

- No second scheduler, second executor, or second update loop anywhere.
- No model-invented destinations, ids, or Telegram RPC paths.
- No false-success path: every failure classifies and persists honestly.
- No schema/code drift: repository validation ↔ migrations match exactly
  (statuses, bounds, CHECK constraints, unique index).

## Phase 2 test execution (exact commands, actual results)

| Command | Result |
|---|---|
| `.venv/bin/python -m pytest tests/test_task_scheduler.py tests/test_task_execution.py tests/test_task_trigger_events.py tests/test_task_send_execution.py tests/test_task_candidate_contract.py tests/test_task_repository.py -q -p no:cacheprovider` | **133 passed in 0.48s** |
| `.venv/bin/python -m pytest tests/test_task_nl_creation.py tests/test_task_hardening.py tests/test_task_management.py tests/test_taskloom_milestone.py tests/test_task_creation_diagnostics.py tests/test_task_show_intent.py tests/test_task_contract.py -q -p no:cacheprovider` | **94 passed in 2.42s** |

227 task-focused tests, 0 failures. These are source/in-process checks only — no
live Telegram, provider-network, or live Supabase verification was performed.

## Phase 2 delivery scope

Only `INVESTIGATION.md` changed in this phase (documentation-only). No backend code,
tests, provider configuration, environment file, database schema, migration, or
deployment setting was changed. The pre-existing untracked nested `telegram-self-bot/`
checkout remains untouched.

---

# Phase 3 Implementation — Occurrence-Time AI-Assisted Task Preparation (2026-09-08)

Implements the confirmed architectural gap from Phase 2 (finding #1): the dormant
`ai_instruction` / `preparation_metadata` contract is now an ACTIVE execution path,
inside the existing single authority. No redesign, no second executor, no new
scheduler.

## What changed (exact files)

| Path | Change |
|---|---|
| `backend/ai/task_execution.py` | `TaskExecutionCoordinator` gains occurrence-time AI preparation: when the task record carries an `ai_instruction`, the coordinator resolves final tool arguments through the existing `ProviderManager` (`AIActionPreparator`) BEFORE execution, then runs the prepared calls through the SAME `ToolExecutor.execute_calls()`. Adds `AIActionPreparator` (single bounded provider round, `tools=[]`, fail-closed JSON contract), `_validate_prepared_calls` (defense-in-depth re-validation at the execution boundary), `_preparation_updates` (single-action audit record persisted to `occurrence.preparation_metadata` via the existing schema), and the optional `preparator=` constructor seam (defaults to the process ProviderManager; tests inject doubles). |
| `tests/test_task_ai_preparation.py` | NEW — 13 focused tests: static tasks never invoke a provider; AI tasks resolve arguments through the executor; prepared-tool-name swap / malformed arguments / wrong action count / provider failure / preparation timeout / missing authority all fail closed with no execution; preparation timeout maps to the retryable path (attempt 2, `retry_at` set); `preparation_metadata` audit record verified; preparator unit contract (fence tolerance, no-tools request, instruction+templates travel to the model, prose/failed-response rejection). |
| `INVESTIGATION.md` | This section. |

No other production file changed. No schema/migration change: `ai_instruction` and
`preparation_metadata` columns, their CHECK constraints, and the repository
validation already existed — this phase only adds the missing runtime consumer.

## Architecture (unchanged authority, new seam)

```text
Task / Event
  → TaskScheduler / TaskEventDispatcher
  → TaskExecutionCoordinator
      ├─ static task:  action_snapshot → ToolExecutor (zero provider calls — unchanged)
      └─ ai task:      action_snapshot → AIActionPreparator (ProviderManager, tools=[])
                          → validated PreparedAction arguments
                          → defense-in-depth re-validation
                          → ToolExecutor → registered Tool → service
```

One execution authority is preserved: `ToolExecutor` remains the sole caller of
tools. The model can only produce arguments for the task's OWN tool names, in the
task's own order, with no destination/owner/chat identity — those come from trusted
runtime context exactly as before. The provider never receives tool definitions,
so it can never trigger an execution itself.

## Fail-closed contract (verified by tests)

| Failure | Behavior |
|---|---|
| Provider failure / malformed output / prose response | Occurrence `failed`, nothing executed |
| Preparation timeout | Mapped to `TimeoutError` → existing retryable classification → `retry_pending`, attempt+1 |
| Prepared tool-name swap, extra/missing actions, non-object arguments | Rejected at the coordinator boundary BEFORE execution; occurrence `failed` |
| Unregistered template tool | Existing pre-preparation registry check fires first (`unregistered_action`) |
| No preparation authority available | `failed` (`task preparation authority is unavailable`) |
| Confirmation-gated (ADMIN_ONLY/CONFIRMATION_REQUIRED) prepared action | Unchanged pre-existing behavior: counts as failed and retries/fails — no unattended execution |

## Phase 3 test execution (exact commands, actual results)

| Command | Result |
|---|---|
| `.venv/bin/python -m pytest tests/test_task_ai_preparation.py -q -p no:cacheprovider` | **13 passed in 0.14s** |
| `.venv/bin/python -m pytest tests/test_task_execution.py tests/test_task_contract.py tests/test_task_hardening.py tests/test_task_scheduler.py tests/test_task_trigger_events.py tests/test_task_repository.py tests/test_task_send_execution.py tests/test_task_nl_creation.py tests/test_task_candidate_contract.py tests/test_task_management.py -q -p no:cacheprovider` | **184 passed in 1.59s** |
| `.venv/bin/python -m pytest tests/ -q -p no:cacheprovider` (full suite) | **1801 passed, 24 skipped, 1 warning in 64.03s** |
| `.venv/bin/python -m py_compile backend/ai/task_execution.py` | OK |

Static task behavior is pinned as unchanged by the existing suites
(`test_task_execution.py`, `test_task_hardening.py`, `test_task_scheduler.py`):
identical code path, zero provider calls.

## Remaining limitations (unchanged or newly known)

1. Multi-action AI tasks (rare) persist execution results only — the single-slot
   `preparation_metadata` schema records the prepared action for single-action
   occurrences (the dominant case). Diagnostic only; never part of the outcome.
2. Outgoing-only event triggers remain uncreatable (Phase 2 finding #2).
3. Findings #3–#8 from Phase 2 are untouched by this phase.
4. Live Telegram / provider-network / live Supabase verification remains NOT
   performed; in-process and fake-provider checks only.

## Phase 3 delivery scope

Production: `backend/ai/task_execution.py`. Tests: `tests/test_task_ai_preparation.py`
(new). Docs: `INVESTIGATION.md`. The pre-existing working-tree change to
`backend/ai/task_candidate.py` (dormant-contract persistence from an earlier session,
already covered by `tests/test_task_candidate_contract.py`) is preserved and included
in this delivery. The untracked nested `telegram-self-bot/` checkout remains
untouched.
