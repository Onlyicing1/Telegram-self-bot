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
