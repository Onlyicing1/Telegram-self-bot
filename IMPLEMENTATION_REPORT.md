# Implementation Report

> **This report was rebuilt from zero.** Every section below was generated from a
> fresh audit of the current repository at commit `d80ee36` — the source files,
> the tests, the migrations, the canonical SQL, and the git history. The previous
> report (7575 lines at `d80ee36`) was treated as obsolete historical material:
> no section, paragraph, table, status list, or wording was preserved from it.
> Where a fact appears here, it was re-verified against the current tree before
> being written down.
>
> **Scope:** documentation only. No Python source, test, migration, SQL file,
> configuration, prompt, or runtime behavior was modified. The only file this
> rebuild changes is `IMPLEMENTATION_REPORT.md` itself. (**Update,
> 2026-09-30:** Part 4 of §3 and §13 document the regex-command-routing
> removal implemented AFTER this rebuild — they carry their own provenance.)
>
> **Verification honesty rule:** nothing in this report claims that the running
> bot was exercised against live Telegram or that any SQL was executed against
> the live Supabase project. See §12 for exactly what was and was not verified.

**Audit method.** For each claim in this report the evidence is one of:
(a) the current source tree (file + symbol cited), (b) a test run executed
against the current HEAD during this audit, (c) `git log` / `git show` on the
current history, or (d) a repository document (`AGENTS.md`,
`DATABASE_ARCHITECTURE.md`) cross-checked against the code it describes. Claims
that could not be grounded in one of these are marked as such or omitted.

---

## 1. Current Project State

LifeOS is a single-process Python asyncio Telegram **self-bot** (userbot) built
on Telethon with a `StringSession`. The owner's own account is the interface:
exactly one text command (`Menu`) opens the Glass UI inline panel system, and a
natural-language AI request (trigger word, default `Nova`) activates the AI
runtime. A FastAPI server in the same process serves `/health` and a read-only
React dashboard.

The subsystems that exist today, all in the `backend/` tree:

| Subsystem | Location | State |
|---|---|---|
| Runtime supervision & recovery | `backend/runtime/` (`RuntimeSupervisor`, heartbeat, keepalive, failsafe, task guard, operation watchdog) | Implemented; single recovery authority |
| Deep Save engine | `backend/services/save_service.py` + `backend/bot/handlers/save.py` | Implemented (no forward-save path) |
| Retrieve / Delete / Discover / Database panels | `backend/bot/handlers/`, `backend/services/` | Implemented |
| Profile engines (Bio + Username) | `backend/profile/engine.py`, `backend/profile/scheduler.py` | Implemented; one shared minute-boundary scheduler |
| AI runtime | `backend/ai/providers/`, `backend/ai/engine/`, `backend/ai/tools/`, `backend/ai/conversation/`, `backend/ai/memory/` | Implemented; 55 registered tools |
| **Durable task system (Taskloom)** | `backend/ai/task_*.py`, `backend/ai/database/task_repository.py`, `backend/ai/tools/question.py`, `backend/ai/tools/todo_step_tools.py` | Implemented through Part 3F; the subject of §3–§7 |
| Helper bot (optional inline renderer) | `backend/helper/` | Implemented; disabled is a valid state |
| Web server + dashboard | `backend/web/app.py`, `src/` (React) | Implemented |
| Credential Vault (control plane) | `backend/ai/` credential tooling + `api_credentials` schema | Schema + RPC delivered; no secret ever created; live-verification deferred |
| TTS | frozen at commit `ee5967f` | **Deferred** — hidden from the UI, provider fallback not delivered |
| STT | control plane only (candidate research, M2.3 fallback, M2.4 credential pool, M1.8 chunking) | **Frozen** at the M-line; no live recognition loop verified |

The newest engineering arc is the durable task system ("Todo / Taskloom"),
which grew in seven commits from a basic todo list to a bounded agent-workflow
executor with durable chains, time waits, conditional branching, and durable
question→answer continuation. Its current capabilities and boundaries are what
this report documents in most detail (§3, §5, §6, §7).

---

## 2. Current Repository / Commit

| Item | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Branch | `main` |
| Local HEAD at audit time | `d80ee369ed8151242c7bd90dda3b67d0db8ddece` |
| `origin/main` at audit time | `d80ee369ed8151242c7bd90dda3b67d0db8ddece` (equal — verified after `git fetch origin`) |
| Working tree | clean except the untracked nested repository `telegram-self-bot/` (pre-existing, never staged) |

Commit history relevant to the audited work (verified with `git show -s
--format="%h %ad %s" --date=short` against the current history — none of these
hashes was taken from a prior document):

| Commit | Date | Subject |
|---|---|---|
| `d80ee36` | 2026-09-30 | docs: refresh implementation report (the stale report this file replaces) |
| `679ae09` | 2026-09-29 | feat(todo): add bounded conversational continuation — **Part 3F** |
| `58086a6` | 2026-09-28 | feat(todo): add multi-turn question continuation — **Part 3E** |
| `16519fb` | 2026-09-28 | feat(todo): add durable question-answer continuation — **Part 3D** |
| `e702c18` | 2026-09-26 | feat(todo): add bounded conditional branching — **Part 3C** |
| `f73af60` | 2026-09-26 | feat(todo): add durable action-chain waiting — **Part 3B** |
| `83c5abd` | 2026-09-26 | feat(todo): add durable action chains — **Part 3A** |
| `7c0476d` | 2026-09-26 | docs(todo): investigate agent workflow architecture |
| `3f8f197` | 2026-09-26 | feat(todo): add multi-step todos (Part 2: `todo_steps`) |
| `af0a8d7` | 2026-09-26 | feat(todo): add basic todo list (Part 1) |
| `9f7c09b` | earlier | docs(database): reconcile canonical setup with current schema |

Earlier history (Save V2, TTS freeze, credential vault, STT control plane) is
summarized in §11 where it defines what is deferred.

---

## 3. Implemented Phases

The Taskloom arc is one workflow model, extended phase by phase. One workflow
is ONE row in `ai_tasks`; each scheduled run is ONE occurrence row in
`ai_task_occurrences`; a workflow's behavior is an ordered list of at most
`MAX_ACTIONS = 5` actions (`backend/ai/task_contract.py`, line 10). Every phase
below re-uses the same three execution authorities:

- **ONE `TaskScheduler`** (`backend/ai/task_scheduler.py`) — the only
  boundary-claiming scheduler; started by the supervisor.
- **ONE `TaskExecutionCoordinator`** (`backend/ai/task_execution.py`) — the
  single authority that walks a claimed occurrence through its actions.
- **ONE `ToolExecutor`** (`backend/ai/tools/executor.py`) — the only component
  that ever calls `tool.execute()`.

Statuses below use four labels: **IMPLEMENTED** (code + tests, no material
gap), **IMPLEMENTED WITH LIMITATIONS** (implemented; bounded scope or a real
gap documented), **DEFERRED** (planned, deliberately not built now),
**NOT IMPLEMENTED** (does not exist).

### Part 3A — Multi-step action chains — **IMPLEMENTED**

- **Commit:** `83c5abd` (2026-09-26).
- **Objective:** let one durable workflow execute up to five registered tool
  actions in order, durably, instead of a single notification action.
- **Implementation:** the chain lives in the task's `actions` JSON (max 5,
  max payload 32,768 bytes — `MAX_ACTIONS`, `MAX_PAYLOAD_BYTES` in
  `task_contract.py`). `TaskExecutionCoordinator._execute_chain`
  (`task_execution.py`, line 695) walks the actions in order and executes each
  through the `ToolExecutor`. Before executing, the coordinator re-proves the
  chain against the occurrence's durable per-action run records (stored under
  the `actions` key in BOTH `result_metadata` and `error_metadata`): an action
  already recorded `succeeded` is never replayed — a retry resumes at the
  first action that did not succeed.
- **Result references (`$ref`):** one argument value may be a reference of the
  shape `{"$ref": {"action": N, "field": "F"}}`, resolved at execution from the
  bounded run record of an EARLIER action of the SAME occurrence. Validation
  (`task_contract.py`) enforces: exactly one `$ref` per whole argument value;
  `N` must be a strictly earlier position; `F` must be one of the producing
  tool's declared `consumable_output_fields` (at most 3 fields, each ≤128
  characters — `MAX_ACTION_OUTPUT_FIELDS`, `MAX_ACTION_OUTPUT_TEXT_CHARS`);
  every unknown, out-of-range, or undeclared reference fails closed at
  creation and again before execution. Currently chainable outputs:
  `save` → `save_code`; `web_search` → `top_title`, `top_url`; `ask_owner` →
  `answer` (Part 3D).
- **Message-grounding guard:** actions whose arguments address a literal
  Telegram message (`delete_message_by_id`, `delete_by_id`,
  `delete_messages_by_ids`, `save_by_link`) are additionally constrained:
  the referenced message must be grounded in trusted provenance — the owner's
  triggering message, the message they replied to, or a number the owner
  explicitly typed (`trusted_message_ids`,
  `message_reference_provenance_error` in `task_contract.py`). A model-invented
  message ID is refused before persistence.
- **Recursive-creation guard:** a claimed scheduled occurrence cannot create
  further durable tasks (`scheduled_creation_error`) — the workflow graph
  cannot recurse.
- **Persistence/database impact:** no schema change. Run records live inside
  the occurrence row's existing metadata columns.
- **Tests:** `tests/test_task_action_chains.py` — 38 collected at current HEAD
  (audited run), including retry-resume, reference resolution, provenance
  refusal, and registry-boundary checks.
- **Limitations:** max 5 actions; no loops; sequential execution only; a
  failed action stops the chain (later actions never run).

### Part 3B — Durable time wait — **IMPLEMENTED**

- **Commit:** `f73af60` (2026-09-26).
- **Objective:** let a chain pause until a wall-clock instant, surviving
  restarts, without a new status and without consuming an attempt.
- **Implementation:** an action may carry `not_before`, a bounded ISO-8601
  instant (a naive value is interpreted in the task's configured timezone;
  the stored value is absolute UTC; a `not_before` may not precede a previous
  action's boundary and may not sit more than 10 years ahead — validated at
  creation, re-proved before execution). When the chain reaches a future
  boundary, `_park_at_wait` (`task_execution.py`, line 1003) parks the
  occurrence on the EXISTING eligibility pair `retry_pending` + `retry_at`
  (real wall clock) — no new status, no new table, no attempt consumed. The
  scheduler's normal due-retry query wakes it when due; the walk resumes at
  the waiting action.
- **Persistence/database impact:** none. `retry_at` already existed.
- **Tests:** `tests/test_task_durable_wait.py` — 43 collected at current HEAD.
- **Limitations:** one wait per action (it is an action property, not a
  separate step type); resolution is to the minute of the scheduler's poll
  cadence; no calendar/recurrence semantics beyond what the scheduler already
  provides.

### Part 3C — Conditional branching — **IMPLEMENTED**

- **Commit:** `e702c18` (2026-09-26).
- **Objective:** let one workflow take one bounded binary decision based on an
  earlier action's result, deterministically and restart-safely.
- **Implementation:** at most ONE action of a chain may be a condition —
  `{"condition": {"source": {"action": N, "field": F}, "operator": "equals"
  | "not_equals", "value": <text|boolean|number|null>}}` (`CONDITION_OPERATORS`
  = `{"equals", "not_equals"}`, `task_contract.py` line 665; `validate_condition`
  line 703). It is the ONE action entry that declares no tool call; every
  action AFTER it declares `branch`: `"true"` or `"false"` — one contiguous
  run each, the true run first, both non-empty. No nesting, no loops, no
  parallel branches. Exactly one branch runs; the condition's own bounded run
  record carries the durable result (`{"matched": …, "selected_branch":
  "true"|"false"}`) and is persisted BEFORE either branch runs, so a restart
  resumes the SAME branch, never re-evaluates the condition and never starts
  the other one — the non-selected branch's actions are marked `skipped`
  (`BRANCH_SKIPPED_STATUS`, the one non-execution status run records may
  carry) at every durable write. Gating precedes the wait check; a condition
  failure runs NEITHER branch; a failed action in the selected branch never
  falls back to the other branch. Conditions and per-occurrence generated
  content (`ai_instruction`) are mutually exclusive.
- **Persistence/database impact:** none. Branch state lives in the run records.
- **Tests:** `tests/test_task_conditional_branches.py` — 96 collected at
  current HEAD (the largest phase suite).
- **Limitations:** one condition per chain; two operators only; branches are
  static (chosen at creation, not discovered at runtime); the compared value
  is bounded plain data, not a second query.

### Part 3D — Durable question → answer continuation — **IMPLEMENTED**

- **Commit:** `16519fb` (2026-09-28).
- **Objective:** let a workflow stop, ask the owner ONE question, and resume
  durably when the owner answers — with exact correlation, duplicate
  protection, and restart survival.
- **Implementation:**
  - ONE new occurrence status `waiting_answer` (`WAITING_ANSWER_STATUS`,
    `task_contract.py` line 992; repository `OCCURRENCE_STATUSES` includes it
    in `backend/ai/database/task_repository.py` line 24). It may be entered
    only from `running` and only with a validated `pending_question` record
    (repository-enforced, fail-closed without it).
  - The question is a REGISTERED tool call: `ask_owner`
    (`QUESTION_TOOL`, `backend/ai/tools/question.py::AskOwnerTool`), whose
    single argument `question` is bounded plain text ≤512 characters
    (`MAX_QUESTION_CHARS`). The tool's declared chainable output is
    `("answer",)` (`ANSWER_FIELD`).
  - `_park_at_question` (`task_execution.py`, line 1072) writes the durable
    `pending_question` record (≤1024 bytes serialized —
    `MAX_PENDING_QUESTION_BYTES`) into BOTH metadata channels, leaves the
    question's run record `pending` (no attempt consumed), and parks the
    occurrence. The scheduler, the due-retry query, and the recovery query all
    exclude `waiting_answer` rows by construction, so nothing re-asks the
    question and no clock can fire it.
  - The ONLY edge out is the owner's reply to the EXACT question message.
    `backend/ai/task_answers.py::TaskAnswerResolver` (wired into the existing
    task-events handler `backend/bot/handlers/task_events.py` — there is no
    second update loop) gates on sender identity, correlates
    `chat_id + reply_to_msg_id` against the stored ACTIVE checkpoint, and
    performs ONE CAS: `resume_waiting_for_answer`
    (`task_repository.py` line 973 for the Supabase variant, line 506 for the
    in-memory fallback) flips `waiting_answer → retry_pending` with a real
    wall-clock `retry_at`, persists the normalized bounded answer (plain text
    ≤128 characters — `MAX_ANSWER_CHARS`) in the pending-question record, and
    flips THAT question's run record to `succeeded` with output
    `{"answer": …}`. A duplicate or racing reply loses the CAS and mutates
    nothing; a late reply to an already-answered checkpoint matches no parked
    row; one message consumes at most one parked occurrence
    (`MAX_RESUMES_PER_MESSAGE = 1`, `task_answers.py` line 55). Guards for
    `question_resend_refused` and `answered_question_record_invalid` exist so
    a malformed park cannot be resurrected into a fake answer.
  - Downstream actions consume the answer as structured data only, through the
    Part 3A reference (`{"$ref": {"action": N, "field": "answer"}}`) or a
    Part 3C condition source — never through chat history.
- **Persistence/database impact:** exactly one schema change — migration
  `20260927020000_add_waiting_answer_status.sql` widens the
  `ai_task_occurrences_status_check` constraint. No new table, column, index,
  policy, or grant (see §8 for its application status).
- **Tests:** `tests/test_task_durable_answer.py` — 29 collected at current
  HEAD.
- **Limitations:** one question may be waiting at a time in 3D's own model
  (relaxed by 3E within bounds); answers are plain text only; no timeout,
  reminder, or re-ask exists for an unanswered question; the question message
  itself is a normal Telegram message the owner must explicitly reply to.

### Part 3E — Multi-question continuation — **IMPLEMENTED**

- **Commit:** `58086a6` (2026-09-28).
- **Objective:** let ONE chain contain SEVERAL questions, asked and answered
  in an order the chain itself defines.
- **Implementation:** SEVERAL actions of a chain may be `ask_owner` calls —
  the chain walk simply stops at the first question it reaches, so a later
  question can never be SENT before an earlier one is ANSWERED (at most ONE
  question is ever waiting — the park write replaces the stored record when a
  resumed chain reaches the next question). Each answer lives in its OWN
  question action's run record, so downstream actions consume a SPECIFIC
  question's answer via its position: the 3A reference or a 3C condition
  naming that action. The interpreter prompt (`backend/ai/task_interpreter.py`,
  QUESTIONS block around line 613) teaches exactly this contract: several
  `ask_owner` actions are allowed, only ONE is ever waiting, questions
  INTERLEAVE freely with registered actions in the one ordered chain, and
  answers are referenced as `{'action': N, 'field': 'answer'}`.
- **Persistence/database impact:** none (no schema change beyond 3D's).
- **Tests:** `tests/test_task_multi_question.py` — 31 collected at current
  HEAD.
- **Limitations:** still at most one WAITING question; the number of questions
  is bounded by `MAX_ACTIONS = 5`; no answer history beyond the run records.

### Part 3F — Bounded conversational continuation — **IMPLEMENTED**

- **Commit:** `679ae09` (2026-09-29).
- **Objective:** confirm and pin the full interleaved model — registered
  actions and questions alternate freely in one chain (ACTION → QUESTION →
  ANSWER → ACTION, QUESTION → ANSWER → QUESTION, any bounded combination) —
  with explicit guarantees against replanning, context leakage, and
  authority drift.
- **Implementation:** no new mechanism was added; 3F pins the composition.
  Registered actions and questions interleave freely in one chain; the answer
  of each question resumes the SAME occurrence into the next ALREADY-DEFINED
  action and reaches it only as structured data through the 3A reference / 3C
  condition mechanisms; nothing ever invents new actions. Verified guarantees
  (each pinned by a named test in `tests/test_task_conversational_continuation.py`):
  - mixed action/question chains are the valid creation shape
    (`test_a_mixed_chain_of_actions_and_questions_is_the_valid_shape`);
  - ACTION → QUESTION → ANSWER → ACTION, QUESTION → ANSWER → QUESTION, and
    longer interleavings run in order (`test_action_question_answer_action_continues_in_order`,
    `test_question_answer_question_answer_action`,
    `test_action_question_answer_question_answer_action`);
  - each answer reference consumes its own question's answer
    (`test_multiple_answer_references_each_consume_their_own_question`);
  - exact Telegram correlation: unrelated chats, wrong senders, wrong tasks,
    and late replies are refused (`test_unrelated_and_wrong_chat_replies_are_ignored`,
    `test_wrong_sender_wrong_task_and_late_replies_are_refused`);
  - duplicate protection: one reply is consumed once, the action runs once
    (`test_a_duplicate_reply_is_accepted_once_and_the_action_runs_once`), and
    a reply to a second parked occurrence consumes only that occurrence;
  - restart recovery: a restart while a question is pending never re-asks
    (`test_a_restart_while_a_question_is_pending_never_re_asks`); a restart
    after an answer resumes at the next action; a restart between actions
    parks on the wait and never replays; a restart while the SECOND question
    is pending preserves both the answered and the pending records;
  - branch and wait compatibility: an answer feeds an existing 3C condition
    and a branch's question drives the branch's action
    (`test_a_question_answer_feeds_the_existing_condition`,
    `test_a_branch_question_and_its_answer_drive_the_branch_action`); a wait
    after questions parks on the clock, not on a question; a failed
    downstream action never reopens the answered question; a retryable
    downstream failure resumes at the failed action;
  - no dynamic replanning: the recorded action snapshot IS the whole workflow
    (`test_no_dynamic_replanning_the_recorded_snapshot_is_the_whole_workflow`);
    the execution context carries only structured workflow data — no chat
    history enters the execution context
    (`test_the_execution_context_carries_only_structured_workflow_data`);
  - authority boundaries hold: the registry refuses unresolvable mixed chains
    and the `ToolExecutor` remains the sole execution authority
    (`test_the_tool_registry_boundary_refuses_unresolvable_mixed_chains`,
    `test_the_tool_executor_remains_the_sole_execution_authority`);
  - an end-to-end composite (search → question → save → question → deliver)
    runs through the real tool set (`test_end_to_end_search_question_save_question_deliver`).
- **Persistence/database impact:** none.
- **Tests:** 26 collected at current HEAD, all listed behaviors above.
- **Limitations:** continuation is bounded by the chain's creation-time
  snapshot; there is no free-form dialogue inside an occurrence — the owner's
  answer is data (≤128 chars) consumed by later actions, not a conversation
  the AI improvises from.

### Part 4 — Regex-command routing removal — **IMPLEMENTED**

- **Change:** `fix: remove regex based tool command routing` (2026-09-30),
  implementing INVESTIGATION.md §24.10 (the §24 regex/command-parsing audit).
- **Architectural reason:** the intended boundary is "AI reasons and proposes
  structured actions; the runtime validates and executes." Regex participated
  in **command/intent detection** at four points, so a character pattern —
  not the model's structured proposal and not an equality check — could steer
  what the runtime executes next. The §24 audit found no execution-boundary
  bypass (every route already converged on `ToolRegistry` → `ToolExecutor`),
  so this phase removes the regex from *detection* without touching the
  execution boundary.
- **Previous flow → why unsafe → replacement flow** (one entry per removed
  route):
  1. **Clock-anchored scheduling intent** (`ai/actions.py`
     `_has_future_clock_request`): previous flow — three regexes
     (`\d{1,2}:\d{2}`, `ساعت\s*\d`, `\bat\s+\d{1,2}\b`) over digit-normalized
     text decided whether a future-anchored request routes to `create_task`;
     unsafe — a character pattern, not a structured proposal, made the
     routing decision (and glued forms like "at 5pm" silently escaped it);
     replacement — token-adjacency detection on the same `_tokenize` stream
     (`_words_contain_clock_anchor` / `_text_has_clock_anchor`: clock word +
     digit, `at` + number, `am`/`pm` after a digit, `H:MM` / `H` `:` `MM`
     triples). Same decisions, no regex.
  2. **create_task completeness gate** (`ai/tools/task.py`): previous flow —
     the pre-provider gate kept its own inline `re.search(r"\d{1,2}:\d{2}")`
     plus duplicated vocabulary imports; unsafe — the gate and the parser
     could disagree about what proves a clock (two implementations of one
     decision); replacement — the gate imports the SAME
     `_text_has_clock_anchor` helper; the gate and the deterministic parser
     can no longer disagree, and no regex runs before the provider call.
  3. **Save-code shape** (`ai/actions.py` `_SAVE_CODE_RE` vs
     `_SAVE_CODE_TOKEN_RE`/`_SAVE_CODE_RANDOM_TOKEN_RE`/`
     _SAVE_CODE_CANONICAL_RE`): previous flow — two independent shape
     declarations validated the same `S####` artifact; unsafe —
     inconsistent validation between the JSON-action validator and the token
     classifier; replacement — one declared shape contract (consolidated,
     documented next to its token twin). These shape regexes REMAIN by
     design: fixed-format artifact matching is exactly what regex is for
     (§24.8), and the retrieval resolver keeps its owner-typography variants.
  4. **`Menu` command** (`bot/handlers/misc.py`): previous flow — the only
     Telethon `pattern=r"^Menu$"` regex in the codebase matched the single
     text command; unsafe — a regex was the router for the one command, a
     mechanism the rest of the command surface (trigger words) already
     replaced with equality; replacement — pattern-less outgoing handler with
     an exact-equality guard (`raw_text == "Menu"`), mirroring
     `config_store.match_trigger`. Only the literal word opens the panel; the
     decorative Glass UI font is render-time only.
- **AI-hallucinated invocation protections (verified, plus new pins):** text
  mentioning a tool name resolves no tool call (`parse_command_intent` on
  "I want to know what the todo_add tool does" → zero calls); keyword strings
  without the deterministic vocabulary reach the provider as conversation;
  malformed tool requests are rejected by the executor's structured
  `malformed_arguments` failure (never executed with fake `{}`);
  unknown tools are refused by the registry (`not_found`) and by the JSON
  action contract (`KIND_INVALID: Unknown action`); `ToolExecutor` remains
  the sole caller of `tool.execute()` — no second parser, no second
  dispatcher, no parallel execution path was added.
- **Affected components:** `backend/ai/actions.py`, `backend/ai/tools/task.py`,
  `backend/bot/handlers/misc.py`, `AGENTS.md` (§5 command row), tests
  `test_50_font_system.py` / `test_51_execution27.py` (updated to pin the
  equality guard), new `tests/test_regex_routing_removal.py` (26 tests).
- **Persistence/database impact:** none.
- **Tests:** full suite **5383 passed, 26 skipped** at the change HEAD
  (baseline 5357 + 26 new); focused: the 9 touched suites (actions, font,
  execution27, NL creation/interval, semantic triggers/completeness,
  hardening, tool-health) all pass.
- **Limitations:** detection regex is removed only where the §24 audit named
  it (command/intent routing); shape/artifact, JSON-extraction, resilience,
  provenance, policy, and security regex intentionally remain (§24.8). The
  deterministic token vocabulary of `parse_command_intent` is unchanged data,
  not regex, and stays authoritative for the narrow save/delete/send
  vocabulary exactly as before.

### Prior phases the Taskloom arc builds on — **IMPLEMENTED**

- **Part 1 (basic todo list, `af0a8d7`):** the unscheduled `schedule_type =
  'todo'` kind of `ai_tasks` (`20260926000001_add_todo_schedule_type.sql`
  widened the CHECK for it), with the durable task tables, scheduler, and
  occurrence lifecycle.
- **Part 2 (multi-step todos, `3f8f197`):** the `todo_steps` table
  (`20260927000001_add_todo_steps.sql`) — the ordered steps of ONE todo
  (`ON DELETE CASCADE`, `UNIQUE(task_id, position)`, ≤256-char titles,
  `active|completed` CHECK, CAS `version`), with five dedicated tools
  (`todo_step_add/list/transition/edit/delete`, registered in
  `backend/ai/tools/todo_step_tools.py`) and an atomic multi-row insert with
  a compensating CAS delete. Steps are NOT tasks: no schedule, no occurrence,
  never returned by task queries.
- **Prepare-ahead:** recurring AI-assisted tasks prepare (generate +
  validate, NO side effects) their next action within a bounded horizon
  before the boundary and persist it in the occurrence's
  `preparation_metadata` (migration `20260912000001`); the boundary re-proves
  it (same task version, same tool, policy-valid) and executes exactly once.
  Generated content is governed by the deterministic policy in
  `backend/ai/preparation_policy.py` (language/length derived from the
  instruction; fail-closed, never truncated). Occurrences are idempotent
  (deterministic `occurrence_key`), claimed via CAS, retries bounded
  (`MAX_ATTEMPTS = 3`); recovery exempts only future `claimed` occurrences
  and resolves anything past-due through the interrupted → retry/failed
  contract, so restart can never duplicate executions.

---

## 4. Current Runtime Architecture

All of the following was verified to exist in the current tree (class symbols
located by direct search; file paths relative to `backend/`):

**Process & supervision.** `python -m backend.main` loads config, installs
crash diagnostics, and starts the `RuntimeSupervisor`
(`runtime/supervisor.py`, class at line 85). The supervisor is the single
recovery authority: it owns the self-client run loop, heartbeat, keepalive,
failsafe, helper bot, profile scheduler, web server, and the Taskloom
scheduler, and it serializes all reconnect/rebuild/full-recovery transitions
through one recovery lock with a reconnect cooldown. No other module owns
connection lifecycle.

**Taskloom wiring.** `RuntimeSupervisor._start_task_scheduler`
(`runtime/supervisor.py` line 464) constructs the ONE `TaskExecutionCoordinator`
(`ai/task_execution.py`, class at line 381) and the ONE `TaskScheduler`
(`ai/task_scheduler.py`, class at line 48), then starts the scheduler loop.
Shutdown (`_stop_task_scheduler`) runs in the deterministic stop path. The
answer side is wired into the existing outgoing-message event flow:
`backend/bot/handlers/task_events.py` extracts reply context and hands it to
the supervisor-configured `TaskAnswerResolver` — there is no second update
loop and no second executor.

**AI request path.** Outgoing owner messages hit `ai_unified.py` (trigger /
reply-to-AI activation). `ai/engine/dispatcher.py` (class `Dispatcher`, line
142) resolves the request through `ai/providers/manager/manager.py`
(class `ProviderManager`, line 76), which owns provider selection, fallback,
and health. Providers receive OpenAI-format tool definitions from the
registry (Gemini translates them to `functionDeclarations`), so the model
emits real function calls. Provider HTTP is async (`httpx.AsyncClient`).

**Tool boundary.** `ai/tools/registry.py` is the single public access point
for AI-callable tools; the default registry currently registers **55 tools**
(verified: 55 `.register(` call sites; pinned by
`tests/test_tool_health_audit.py` line 169
`assert len(registry.list()) == 55` and
`tests/test_capability_exposure_tools.py` line 476). The `ToolExecutor`
(`ai/tools/executor.py`, class at line 129) is the sole component that calls
`tool.execute()`; it enforces permission levels, the generic per-tool timeout
(with `long_running=True` tools such as Deep Save exempt), and records tool
history. Destructive deterministic tools execute on the owner's message as
the authorization, with `ADMIN_ONLY`/`CONFIRMATION_REQUIRED` levels still
requiring confirmation.

**Task interpretation.** `ai/task_interpreter.py` translates a natural-language
request into the bounded task candidate (actions, condition, questions,
references, schedule) under the contract validators in `ai/task_contract.py`.
The interpreter prompt teaches the full Part 3F contract (interleaved
questions, one waiting at a time, `$ref` answers). Interpretation is
deterministic on the provider output boundary: the recorded action snapshot is
the whole workflow — no dynamic replanning exists anywhere in the execution
path.

**Persistence layer.** `ai/database/task_repository.py` is the task/occurrence
repository: an in-memory fallback plus a Supabase client variant with the
same interface; every public call wraps its remote access in try/except and
degrades to the fallback (the bot never crashes on a DB error). Status
transitions are enumerated and enforced (`_ALLOWED_TASK_TRANSITIONS`,
`_ALLOWED_OCCURRENCE_TRANSITIONS`); the claim is a CAS; the question-resume is
a CAS.

**Restart/recovery behavior (summary).** A restart leaves parked waits
(`retry_pending` + future `retry_at`) and parked questions (`waiting_answer`)
untouched — both are excluded from the recovery query, which exempts only
future `claimed` occurrences; anything past-due resolves through the
interrupted → retry/failed contract. A running occurrence carries the
`restart_side_effect_uncertain` contract. Recovered chains resume at the
first action whose run record is not `succeeded`.

---

## 5. Todo / Agent Execution Model

The current model, in one place:

1. **One workflow = one `ai_tasks` row.** `schedule_type` selects the kind
   (`event`-family schedules, or `'todo'` for unscheduled). The row carries
   the ordered `actions` snapshot (≤5), optionally `ai_instruction`
   (per-occurrence generated content, policy-gated) — mutually exclusive with
   a condition action.
2. **One run = one `ai_task_occurrences` row.** Deterministic
   `occurrence_key` makes scheduling idempotent; the claim is a CAS
   (`claim_occurrence` accepts `claimed`/`retry_pending`/`interrupted`);
   attempts are bounded (`MAX_ATTEMPTS = 3`).
3. **One chain walker.** `TaskExecutionCoordinator._execute_chain` walks the
   snapshot: for each position, in order — gating (contract re-proof) → wait
   check (`not_before` → park on `retry_pending`/`retry_at`) → question check
   (`ask_owner` → park on `waiting_answer` with a durable
   `pending_question`) → condition evaluation (persist the selected branch
   BEFORE executing it; mark the other branch `skipped`) → tool execution via
   the `ToolExecutor` → durable per-action run record written to BOTH
   metadata channels. A failed action stops the chain; later actions never
   run. An action already `succeeded` is never replayed.
4. **One resume authority per park kind.** Time parks resume through the
   scheduler's due-retry query; question parks resume ONLY through the
   `TaskAnswerResolver` CAS on the owner's correlated reply. Nothing else can
   move a parked row.
5. **One delivery boundary.** Results are delivered to the owner's chat
   through the existing Telegram send boundary; questions are sent by the
   `ask_owner` tool through the same boundary and correlated by
   `chat_id + reply_to_msg_id`.

What the model deliberately does NOT have: loops, parallel branch execution,
dynamic replanning, workflow mutation after creation, cross-task data
visibility, and any second scheduler/executor. (See §10 for the limitation
consequences.)

### 5.1 Bounded proactive multi-action planning (per-request authorization)

The dispatcher/interpreter path gained ONE capability: when the owner's own
message explicitly authorizes additional useful work, the AI may plan a small
ordered action chain toward the stated goal instead of only the literally
named action — as ONE coherent task with multiple validated actions, never as
repeated `create_task` calls. This is bounded, user-authorized proactive
multi-action execution within the existing Todo/ToolExecutor architecture.

- **Authorization is per-request and fail-closed.**
  `backend/ai/proactive.py` is the single detector
  (`has_proactive_authorization`): a conservative Persian/English phrase
  vocabulary (e.g. «چیزهای مرتبط دیگه‌ای», «هر کار کوچیکی», "use your
  judgment", "do what is needed") matched over normalized tokens; bare
  delegation ("خودت انجام بده"), opinion prompts, and permission-adjacent
  social phrasing are deliberately NOT authorization. There is no persisted
  preference, no global mode, and no schema: the flag lives exactly as long
  as the request that carried it (ZWNJ is removed rather than spaced so
  «دیگه‌ای» ≡ «دیگهای» ≡ «دیگه ای").
- **Durable create_task path** — `CreateTaskTool` proves authorization from
  the owner's raw message (`extra["proactive_authorized"]`, set by
  `Dispatcher._build_tool_context`, with the distilled request as fallback)
  and passes it to `TaskInterpreter.interpret`, which appends
  `PROACTIVE_EXPANSION_INSTRUCTIONS` (six hard rules a–f: goal-related only,
  1..5 actions unchanged, `create_task` NEVER inside actions, registered
  names only, ordered dependencies, all other contracts unchanged, no safety
  bypass). Trace lines carry `proactive_authorized=true/false`.
- **Conversational path** — the Dispatcher inserts ONE
  `PROACTIVE_AUTHORIZED_RULES` system message before the user input and
  records `metadata["proactive_authorized"]`; a plain request produces
  byte-identical messages to before. `RUNTIME_RULES_TEMPLATE` gained one
  PER-REQUEST bullet so the base prompt states the default (perform ONLY the
  asked operations).
- **Bounds unchanged** — `MAX_ACTIONS = 5`, `MAX_TOOLS_PER_TURN = 5`,
  `MAX_TOOL_ROUNDS = 3`, the candidate schema's `maxItems: 5`, confirmation
  gating (`settings_set` stays ADMIN_ONLY), and the recursive-creation
  backstop (`scheduled_creation_error`) all hold under authorization.
- **Prohibited under authorization** — unrelated work, new side-effect
  categories (raw Telegram RPC, SQL, filesystem, HTTP, shell), touching
  unrelated saved items/messages/account state, repeated `create_task` for
  one goal, and bypassing confirmation gates. Ambiguous requests still return
  NULL / ask ONE clarifying question.
- **Regression suite** — `tests/test_proactive_action_chains.py` (24 tests):
  detector positives/negatives and fail-closed behavior; Mode A (no
  expansion) vs Mode B (one request → one task, one provider call, ordered
  ≤5 actions); bounds and eligibility pins (unregistered tool, `settings_set`
  confirmation); recursion-forbid pins; dispatcher rules-message insertion
  and ordered tool execution; an AST import scan proving `proactive.py`
  imports no scheduler/executor/registry/dispatcher/bot module.

---

## 6. Durable Question / Answer Model

The question side of the contract, consolidated (all symbols verified in
`backend/ai/task_contract.py`, `backend/ai/task_answers.py`,
`backend/ai/database/task_repository.py`, `backend/ai/tools/question.py`):

| Contract element | Value / rule |
|---|---|
| Question tool | `ask_owner` (`QUESTION_TOOL`), registered in the 55-tool registry |
| Question text | plain text, ≤512 chars (`MAX_QUESTION_CHARS`) |
| Park status | `waiting_answer` — the ONE status added for this model |
| Park payload | validated `pending_question` record (≤1024 bytes), written to BOTH `result_metadata` and `error_metadata` |
| Attempt cost | none — the question's run record stays `pending` while parked |
| Scheduler interaction | scheduler, due-retry, and recovery queries all exclude `waiting_answer` |
| Only resume edge | owner replies to the EXACT question message |
| Correlation | sender identity + `chat_id` + `reply_to_msg_id` against the ACTIVE checkpoint |
| Answer normalization | plain text ≤128 chars (`MAX_ANSWER_CHARS`); bounded persisted form |
| Resume CAS | `resume_waiting_for_answer`: `waiting_answer → retry_pending`, real wall-clock `retry_at`; flips the question run to `succeeded` with `{"answer": …}` |
| Duplicate / race | loser of the CAS mutates nothing; one message consumes ≤1 parked occurrence (`MAX_RESUMES_PER_MESSAGE = 1`) |
| Answer consumption | only as structured data via `$ref {"action": N, "field": "answer"}` or a condition source naming that action |
| Waiting bound | at most ONE question waiting at any time; a later question is sent only after the earlier one is answered |
| Malformed-park guards | `question_resend_refused`, `answered_question_record_invalid` — a broken park cannot be resurrected into a fake answer |
| Restart behavior | parked question survives restart (excluded from recovery); answered checkpoint never re-activates |
| Expiry | none — no automatic timeout, reminder, or re-ask (§10) |

---

## 7. Multi-Turn / Conversational Continuation

The Part 3F composition, stated as the system's actual capability:

- **ACTION → QUESTION → ANSWER → ACTION** — supported; the answer resumes the
  same occurrence at the next already-defined action.
- **QUESTION → ANSWER → QUESTION** — supported; the walk stops at the first
  question, and the park write replaces the stored checkpoint when the resumed
  chain reaches the next one.
- **Multiple bounded questions** — supported (bounded by `MAX_ACTIONS = 5`
  total actions in the chain); each answer is stored in its own question's
  run record and referenced by position.
- **Answer references** — supported through the single existing mechanism
  (`$ref` / condition source); no second data-flow mechanism exists.
- **Exact Telegram correlation** — chat + reply target + sender, verified per
  reply; wrong chat/sender/task/late replies are refused.
- **Duplicate protection** — CAS-based; one reply, one consumption, one
  execution.
- **Restart recovery** — pending questions survive restarts and are never
  re-asked; answered checkpoints never reactivate; waits park on the clock.
- **Branch compatibility** — answers feed conditions; branch questions run
  only in the selected branch; the non-selected branch never asks.
- **Wait compatibility** — a `not_before` after questions parks on the clock,
  never on a question; a question may not be reached before a wait elapses.
- **Context isolation** — the execution context carries only structured
  workflow data; no chat history ever enters the execution context, so the
  model cannot improvise from conversation.
- **Bounded continuation** — the workflow is the creation-time snapshot;
  answers are data (≤128 chars) consumed by later actions, not prompts that
  extend the workflow.
- **No dynamic replanning** — nothing in the execution path can add, remove,
  or reorder actions; a test pins the snapshot as the whole workflow.

This is a *bounded conversational workflow* engine, not an open-ended chat
loop: the owner answers concrete questions, the chain continues concretely.

---

## 8. Database State

Four distinct things must not be confused; they are stated separately.

**(a) Repository schema — migration files.** `supabase/migrations/` currently
contains **30 files** (verified by listing the directory during this audit).
The ones that define the durable task system and its phases:

| Migration | Establishes | Introduced by |
|---|---|---|
| `20260829000001_create_ai_tasks.sql` | `ai_tasks`, `ai_task_occurrences` (base durable-task schema) | Taskloom groundwork |
| `20260904000001_add_event_schedule_type.sql` | `schedule_type = 'event'` family | scheduling |
| `20260912000001_add_ai_task_occurrences_preparation_metadata.sql` | `preparation_metadata` column | prepare-ahead |
| `20260920000001_reconcile_canonical_schema.sql` | canonical reconciliation snapshot (all 16 tables, final-form constraints incl. the occurrence status CHECK) | DB repair arc |
| `20260926000001_add_todo_schedule_type.sql` | `schedule_type = 'todo'` (Part 1) | `af0a8d7` |
| `20260927000001_add_todo_steps.sql` | `todo_steps` table (Part 2) | `3f8f197` |
| `20260927020000_add_waiting_answer_status.sql` | widens ONLY the `ai_task_occurrences_status_check` to admit `waiting_answer` (Part 3D) | `16519fb` |

**Schema impact per phase (verified against the migrations):** Parts 3A, 3B,
3C, 3E, and 3F required **zero schema change** — they live entirely in the
existing JSON metadata columns and the existing status values. Part 3D
required exactly ONE change: the status CHECK widening above. Part 1 added
the `todo` schedule type (two widened constraints); Part 2 added the one new
`todo_steps` table.

**(b) Canonical SQL.** The canonical setup is ONE block — §31.3 of
`DATABASE_ARCHITECTURE.md` ("ONE COMPLETE SUPABASE SETUP SCRIPT"), parts 1–8:
the reconciliation snapshot plus the seven pending migrations (Vault ×2, Save
V2 ×2, TTS settings, Todo schedule type, Todo steps). Its identity is pinned
by tests: `tests/test_canonical_schema_reconciliation.py` proves the two
repository copies (`supabase/canonical_bootstrap.sql` and the migration
`20260920000001_reconcile_canonical_schema.sql`) are byte-identical and that
the §31.3 embed is statement-identical to the migration;
`tests/test_database_setup_order.py` proves §31 contains exactly ONE setup
block, that each part is statement-identical to its migration file, in
`DOCUMENTED_ORDER`, with no placeholder text, and that the Vault management
functions are physically present.

Where the `waiting_answer` widening fits: it is **not** one of the eight
embedded parts, because part 1 (the reconciliation snapshot) already re-adds
the occurrence status CHECK in final form — i.e. the final-form constraint
already includes `waiting_answer`. `DATABASE_ARCHITECTURE.md` §31.1
classifies `20260927020000_add_waiting_answer_status.sql` as additive and
states that the block equals the effective final state of all 30 migrations.
For an EXISTING database whose status constraint predates the widening,
applying the widening migration directly is the documented path; the
migration is idempotent in shape (`NOTIFY pgrst` at the end, no destructive
statements).

**(c) Migration files vs. canonical.** Migration files are the historical,
per-change record; the canonical script is the convergent whole. The
repository's tests enforce that the two never disagree: successors of the
snapshot must be additive-only (no `DROP TABLE`, `TRUNCATE`, `DELETE FROM`,
or `DROP COLUMN`), and the snapshot is never regenerated to absorb them.

**(d) Live Supabase state.** **NOT VERIFIED — see §12.** Nothing in this
audit executed any SQL against the live project, inspected the live catalog,
or confirmed which migrations the live database has absorbed. Per the
repository's own documentation, every object remains pending an owner action
(paste §31.3 for a fresh database; apply the additive successors for an
existing one). Consequences already encoded in the application: until the
`waiting_answer` widening exists live, the repository's status validation
refuses the park write (fail closed) — a question chain reports the park
honestly instead of pretending a question was stored, and every
scheduled/waiting/branch behavior keeps working unchanged.

Other live-code-relevant tables (from the same canonical contract): the 16
canonical tables include `saved_items`, `bio_state`, `username_state`,
`bot_settings`, `panel_settings`, `ai_config`, `ai_sessions`, `ai_messages`,
`ai_memories`, `ai_tool_history`, `ai_usage`, `ai_provider_stats`,
`ai_tasks`, `ai_task_occurrences`, `api_credentials` (+ Vault objects),
`todo_steps`. Known dead columns with no live writer (documented, owner-gated
cleanup proposed in `DATABASE_ARCHITECTURE.md` §30.10):
`saved_items.file_name`, `saved_items.short_code`.

---

## 9. Test Coverage and Verification

All numbers below come from runs executed against the current HEAD
(`d80ee36`) during this audit, or from direct inspection of the named files.
No test number is inherited from any prior document.

**Focused phase suites (the Taskloom arc):**

| Suite | File | Collected at HEAD |
|---|---|---|
| Part 3A action chains | `tests/test_task_action_chains.py` | 38 |
| Part 3B durable wait | `tests/test_task_durable_wait.py` | 43 |
| Part 3C conditional branches | `tests/test_task_conditional_branches.py` | 96 |
| Part 3D durable answer | `tests/test_task_durable_answer.py` | 29 |
| Part 3E multi-question | `tests/test_task_multi_question.py` | 31 |
| Part 3F conversational continuation | `tests/test_task_conversational_continuation.py` | 26 |
| **Total (6 suites, run together)** | | **263 collected, 263 passed** (run during this audit: `263 passed in 0.95s`) |

Note: per-file `grep -c "def test_"` counts (30/32/41/29/31/26 definitions)
differ from collected totals where parametrization expands cases — the
collected numbers above are the authoritative ones.

**Full suite.** `.venv/bin/python -m pytest tests/ -q` at HEAD `d80ee36`:
**5357 passed, 26 skipped** in ~118 s. (For continuity with prior phases'
full-suite runs, the same totals were recorded at `679ae09`; the run during
this audit confirms the totals still hold at the docs-only commit `d80ee36`.)

**Contract pins.** Tool registry size pinned at exactly 55
(`test_tool_health_audit.py` line 169;
`test_capability_exposure_tools.py` line 476). Canonical trio byte-identity
and the ONE setup block pinned as described in §8.

**Static checks (this audit).** `python -m py_compile` on the task-system
modules (`task_contract`, `task_execution`, `task_answers`,
`task_scheduler`, `task_interpreter`, `database/task_repository`,
`tools/question`, `tools/registry`, `tools/executor`, `task_creation`): clean.
`git diff --check`: clean.

**Environment note.** Local verification ran on CPython 3.10.12
(`.venv`); production declares Python 3.11.7 (`render.yaml`,
`PYTHON_VERSION`). The suite passes on the local interpreter; production
parity is asserted by configuration, not by a run in this environment.

**Explicitly not covered by any run in this audit:** live Telegram delivery
of a question/answer round-trip; live Supabase behavior of any migration; the
scheduler against a real clock in production. See §12.

---

## 10. Current Limitations

Only limitations verified to exist in the current code are listed. Each is a
deliberate bound of the contract unless noted as a gap.

**Workflow bounds (by design):**
- `MAX_ACTIONS = 5` — a chain has at most five actions, questions included.
- Sequential execution only; no parallel branches or concurrent actions.
- No loops, no recursion (a scheduled occurrence cannot create tasks —
  `scheduled_creation_error`), no workflow mutation after creation, no
  dynamic replanning (the snapshot is the whole workflow).
- One condition per chain; operators limited to `equals` / `not_equals`;
  branches are static and chosen from the creation-time definition.
- Cross-task and cross-occurrence data visibility: none. `$ref` reaches only
  an earlier action of the same occurrence; unknown references fail closed.

**Question/answer bounds (by design):**
- Answers are plain text, ≤128 characters; there is no multimodal, structured,
  or file-based answer type.
- Questions are plain text, ≤512 characters; the pending record ≤1024 bytes.
- At most ONE question waiting at any time.
- `MAX_RESUMES_PER_MESSAGE = 1` — one reply consumes at most one parked
  occurrence.

**Genuine gaps (not scope, actual missing behavior):**
- **No timeout / reminder for an unanswered question.** A parked
  `waiting_answer` occurrence has no clock: nothing in the scheduler or the
  repository expires it, re-asks it, or notifies the owner again. The only
  edges out are the owner's correlated reply (CAS) and a bounded cancel path
  in recovery. A question the owner never answers parks indefinitely.
- **Message-grounded actions are the only provenance guard for Telegram
  targets** — actions like `delete_by_id` accept only grounded message IDs;
  this is a protection, but it also means a workflow cannot act on a message
  ID it discovered at runtime from a tool result (no tool currently declares
  a message-ID output field).
- **Python version skew:** local verification runs 3.10.12, production
  declares 3.11.7 — a parity risk if 3.11-only behavior is ever relied on.

**Other subsystems' documented limitations (current tree):**
- Save V2: `saved_items.file_name` / `short_code` remain dead columns (no
  live writer); cleanup is owner-gated and destructive, deliberately NOT
  executed.
- Credential Vault: schema, RPC pool, and five management functions are
  delivered and pinned by tests, but **no Vault secret was ever created** and
  the panel has never been exercised against the live project.
- TTS: frozen (`ee5967f`), hidden from the user interface; provider fallback
  and its credential pool deferred; migration `20260923000001` pending
  owner-side application; never live-verified.
- STT: control plane only (candidate research, M2.3 fallback, M2.4 credential
  pool, M1.8 chunking at 300 s × 4 = 1200 s); frozen at the M-line; no live
  recognition loop was ever run.

---

## 11. Genuinely Remaining / Deferred Work

Determined from the current tree, not inherited from any prior list.

**IMPLEMENTED** (no pending engineering work; only the owner actions of §12
remain before live use): Parts 3A–3F as described in §3; Part 1 and Part 2
todos; prepare-ahead; the durable-task repository contract; the answer
resolver and its handler wiring.

**IMPLEMENTED WITH LIMITATIONS** (works as built; bounded):
- Taskloom chains — bounded as listed in §10 (5 actions, one condition, one
  waiting question, text answers, no timeout).
- Credential Vault control plane — complete in schema/tests, unused in
  practice because no secret exists yet.
- STT control plane — fallback, pool, and chunking exist; the live
  transcription loop was never exercised.

**DEFERRED** (deliberately not built now; documented decisions, not bugs):
- **TTS** — frozen and hidden from the UI; deferred work: provider fallback,
  credential-pool integration, unhide, and live verification.
- **STT beyond the frozen M-line** — real-provider quality evaluation and a
  live recognition loop remain future work.
- **Live verification of everything** — the owner-side actions in §12.

**NOT IMPLEMENTED** (does not exist in the current tree):
- Dynamic replanning / self-modifying workflows.
- Parallel or looped chain execution.
- Multimodal answers, answer attachments, or voice answers.
- Question timeouts, reminders, or automatic re-asking.
- Any second scheduler, executor, or update loop (prohibited by design and
  absent by construction).
- Workflow mutation after creation; cross-task data flow.

**Owner-gated database work still pending** (from the repository's own
documentation; nothing here has been executed by the coding agent):
- Apply §31.3 (the ONE setup block) on a fresh database — parts 1–8 — or, on
  an existing database, ensure the additive successors are applied, including
  `20260927020000_add_waiting_answer_status.sql` for question parking.
- Optionally review the destructive cleanup proposals of
  `DATABASE_ARCHITECTURE.md` §30.10 (dead columns, legacy tables) on a
  backup, if ever desired.

---

## 12. Live Verification Status

Stated plainly, per verification kind:

| Kind | Status | Evidence |
|---|---|---|
| Focused phase test suites (3A–3F) | **Verified** | Run during this audit at HEAD `d80ee36`: 263 collected, 263 passed |
| Full test suite | **Verified** | Run during this audit at HEAD `d80ee36`: 5357 passed, 26 skipped, ~118 s |
| Contract pins (55 tools, canonical byte-identity, ONE setup block) | **Verified** | Test files cited in §4/§8/§9 pass as part of the full suite; pin lines inspected directly |
| `py_compile` on task-system modules | **Verified** | Clean during this audit |
| `git diff --check` | **Verified** | Clean during this audit |
| **Live Telegram verification** (sending a real question, replying, resuming the chain end-to-end on the owner's account) | **NOT PERFORMED — ever, by any phase** | No phase log, commit, or document records a live Telegram round-trip; none is claimed here |
| **Live Supabase verification** (schema inspection, migration application, live CRUD against the real project) | **NOT PERFORMED — ever, by any phase** | `DATABASE_ARCHITECTURE.md` §30.8/§30.12 states no database was contacted; nothing in this audit touched SQL against any server |
| Production-parity runtime check (Render, Python 3.11.7) | **NOT PERFORMED** | No production deployment was inspected or exercised |

The Taskloom phases are therefore **implemented and test-verified at the unit
and integration-simulation level**, with the live end-to-end path
(Telegram delivery, live schema, live scheduler clock) deliberately left as
owner-side verification work.

---

## 13. Final Current State

- Repository `Onlyicing1/Telegram-self-bot`, branch `main`, HEAD
  `6bec69488d4da873b0e05694c05ef426c250063f` (`fix: remove regex based tool
  command routing`), equal to `origin/main` — re-verified against the GitHub
  remote on 2026-10-01 (`git fetch origin`, `git rev-parse origin/main`,
  `git merge-base --is-ancestor … origin/main`): **the commit is present on
  `origin/main` (delivery state: pushed; no push was required).** This
  supersedes the earlier `d80ee369ed8151242c7bd90dda3b67d0db8ddece` recorded
  as final state in the previous revision (and as audit-time state in §2) —
  delivery status comes only from a current remote verification, never from an
  older conversational claim such as "the work was not pushed". The tree is
  clean except the pre-existing untracked `telegram-self-bot/`.
- **Part 4 (2026-09-30):** regex-command routing removal implemented
  (INVESTIGATION.md §24.10): clock-anchored intent detection, the
  `create_task` completeness gate, and the `Menu` command are decided on
  tokens/equality, one save-code shape contract; the full suite passes at
  5383. Detection-level regex is gone; shape/extraction/resilience/security
  regex intentionally remains (§24.8 of INVESTIGATION.md).
- **Part 5 (2026-10-01): bounded proactive multi-action planning** — a
  per-request, fail-closed authorization detector (`backend/ai/proactive.py`)
  lets ONE owner request expand into ONE coherent task with an ordered,
  validated action chain (≤5 actions, all bounds unchanged) when — and only
  when — the owner's own message explicitly authorizes additional useful
  work. No schema, no migration, no new scheduler/executor/dispatcher, no
  persisted preference; see §5.1. New suite `tests/test_proactive_action_chains.py`
  (24 tests); full suite at this feature: **5407 passed, 26 skipped**.
- The durable task system (Taskloom) is complete through **Part 3F**:
  bounded multi-action chains with result references (3A), durable time waits
  (3B), single conditional branching with durable branch selection (3C),
  durable question→answer continuation on the `waiting_answer` status (3D),
  multi-question interleaving (3E), and the pinned bounded
  conversational composition with no replanning and full context isolation
  (3F). All six phases carry dedicated test suites; the six suites total 263
  tests, all passing; the full suite of 5357 tests passes.
- The architecture invariants hold in code: one scheduler, one coordinator,
  one `ToolExecutor`, one answer CAS, per-action durable run records in both
  metadata channels, fail-closed validation everywhere, and no second
  recovery authority.
- The database contract is fully specified in the repository (30 migrations;
  canonical trio byte-identical; ONE setup block pinned by tests). Parts
  3A/3B/3C/3E/3F added no schema; 3D added exactly one status widening.
  Live Supabase remains untouched by the coding agent — applying the schema
  and verifying live behavior are owner actions.
- Deferred subsystems remain deferred and documented: TTS (frozen, hidden),
  STT beyond the control plane, Vault secret provisioning, and all live
  verification.
- This report was rebuilt from zero out of the current repository state; the
  previous report's content was fully replaced, not edited.
