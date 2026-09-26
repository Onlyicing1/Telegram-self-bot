# Todo / Agent Workflow Investigation

> **Investigation only — no code is changed by this document.** It is the
> current-state audit of the Todo / durable-task / AI-action systems, and of
> the architecture actually required for the intended **Agent Execution
> Workflow** ("search this → save the result → tag it → bring it to me at
> 18:00"). It **replaces the previous `INVESTIGATION.md` entirely** (see §2's
> note on that file's earlier Media/STT content). Producing it modified
> **only** `INVESTIGATION.md`: no Python source, migration, SQL, Supabase
> object, panel, tool, registry, executor, scheduler, Saved Items behavior or
> test was added, edited, deleted or executed.
>
> **Status of the Agent Workflow: NOT IMPLEMENTED.** Everything below is the
> verified current state plus the smallest architecture the next phase can
> implement. No claim of implementation success is made anywhere in this
> document.

---

## 1. Investigation objective

The repository has already shipped a **Todo** system (Part 1 = basic todo,
Part 2 = ordered steps). The product requirement, however, is an **Agent
Execution Workflow**: the owner says

> «پری این چیزی که میخوام رو سرچ بزن، نتیجه رو توی Saved Messages من ذخیره
> کن، تگ نتیجه سرچ رو روی آن بزن، ساعت ۶ ازت میخوامش»

and the system must durably (a) resolve the request into an ordered set of
agent **actions**, (b) execute them through the existing tool boundary,
(c) pass each action's output into the next action's input, (d) wait for a
wall-clock boundary, and (e) resume/fail/retry deterministically after a
process restart — without duplicating side effects.

The concrete mismatch to resolve before any further implementation is that
the shipped Todo work is a **hand-managed checklist** (`ai_tasks` row with
`schedule_type='todo'`, zero actions, plus an ordered `todo_steps` list that
carries completion state only), while the requirement is **executable,
durable, multi-step execution with data flow between steps**.

This document answers, from source (not from the reports):

1. what exactly exists now, component by component (§2–§9);
2. what `todo_steps` really is and is not (§4), and the exact gap (§5);
3. what the actual product model needs (§6, §16–§18) — the *smallest*
   architecture compatible with the existing project, explicitly rejecting
   overengineering (§20);
4. how action chains, result passing, failure/retry/resume, scheduling, Saved
   Items, conditional requests and conversational continuation interact with
   what already exists (§6–§15);
5. the recommended next implementation phase (§22) and the open decisions the
   owner must settle first (§23).

**Explicit non-goals of this document** (also the guard-rails for §22): no
generic workflow platform, no DAG engine, no BPM/automation builder, no
priority/category/project/calendar task-manager features, no event bus, no
microservices, no second scheduler, no second `ToolExecutor`, no second task
repository, no plugin system.

### Method and evidence discipline

Every statement below was read from the working tree at the audit revision
(§2), not from `IMPLEMENTATION_REPORT.md`. Where the report or
`DATABASE_ARCHITECTURE.md` and the source disagree, the disagreement is
**marked explicitly** (§3.4, §4.6) and the source wins. Line numbers are given
for the primary definitions; they were read at the audit HEAD and will drift
with later edits — the symbol names are the durable citation.

---

## 2. Current repository state

| Item | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Branch | `main` |
| Audit HEAD | `3f8f19703d323747aa747ef2aae1227e49d00855` (`3f8f197` — `feat(todo): add multi-step todos`) |
| HEAD == `origin/main` | yes (verified with `git fetch origin` + `git rev-parse`) |
| Working tree | clean at the start of the audit; **the only file this investigation writes is `INVESTIGATION.md`** |
| Todo commits in history | `af0a8d7` `feat(todo): add basic todo list` (Part 1) · `9f7c09b` `docs(database): reconcile canonical setup with current schema` · `3f8f197` `feat(todo): add multi-step todos` (Part 2) |
| Migrations | 29 files in `supabase/migrations/`; the two Todo ones are `20260926000001_add_todo_schedule_type.sql` and `20260927000001_add_todo_steps.sql` |
| Durable task foundation | `supabase/migrations/20260829000001_create_ai_tasks.sql` (`ai_tasks`, `ai_task_occurrences`), `20260904000001_add_event_schedule_type.sql`, `20260912000001_add_ai_task_occurrences_preparation_metadata.sql` |
| Manual Supabase application | still outstanding: the owner applies the ONE block in `DATABASE_ARCHITECTURE.md` §31.3 (parts 1–8, the two Todo parts being 7 and 8). **Nothing was executed against Supabase by this investigation.** |

### Verification actually performed in this pass (no full-suite claim)

| Command | Result |
|---|---|
| `python3 -m pytest tests/test_todo_lifecycle.py tests/test_todo_resolver.py tests/test_todo_tools.py tests/test_todo_steps.py tests/test_todo_steps_tools.py tests/test_todo_ui.py -q` | **108 passed** in 0.93 s |
| `python3 -m pytest tests/test_database_setup_order.py tests/test_canonical_schema_reconciliation.py -q` | **59 passed** in 1.22 s |
| `git diff --check` | run immediately before the commit (§24) |

`IMPLEMENTATION_REPORT.md` states the full suite result of the Part 2 phase
(**5094 passed, 26 skipped**). That number is **REPORTED, NOT RE-VERIFIED** by
this pass — only the focused Todo + DB-contract suites above were re-run, and
they pass. No test was modified by this investigation.

### Note on the previous content of this file

The `INVESTIGATION.md` that this document replaces was a **Media Processing /
Speech-to-Text** investigation (`# Media Processing — Architectural
Investigation`, ~2.4k lines, sections §1–§20). Several source docstrings and
tests cite its section numbers (e.g. `backend/services/stt_consensus.py`,
`backend/services/gemini_media_engine.py`, `backend/tools/stt_benchmark.py`,
`tests/test_media_stt_multipass.py`, `tests/test_media_stt_reliability.py`,
`tests/test_stt_consensus.py`, `tests/test_memory_tools.py` reference
“`INVESTIGATION.md` §19/§20/§13.1”). Because the task requires **this file to
become the current Todo/Agent-Workflow investigation** and forbids modifying any
other file, those citations are now **dangling**: the media/STT findings they
point at are no longer in this document. That is a documentation consequence of
this task, not a code defect; re-pointing or re-homing the STT findings is
**out of scope here** and is recorded as an open item in §23.

---

## 3. What Part 1 actually implemented

"Part 1" = the basic Todo (`af0a8d7` + the `20260926000001` migration), whose
audit conclusion is restated in `IMPLEMENTATION_REPORT.md` and verified here
against source.

### 3.1 Component inventory (labels are source-verified)

| Component | Where | State |
|---|---|---|
| `ai_tasks` (durable task definitions, incl. the `'todo'` row kind) | migration `20260829000001` + `20260926000001`; `backend/ai/database/task_repository.py` | **IMPLEMENTED** |
| `ai_task_occurrences` (per-boundary execution history) | migration `20260829000001` + `20260912000001`; same repository | **IMPLEMENTED** |
| `todo_steps` (ordered checklist children of ONE todo) | migration `20260927000001`; repository + service + tools + UI | **IMPLEMENTED** |
| Task repository (one store, owner-scoped, CAS, memory fallback) | `backend/ai/database/task_repository.py`: `TaskRepository` (:344), `InMemoryTaskRepository` (:373), `SupabaseTaskRepository` (:525) | **IMPLEMENTED** |
| Task services | `backend/ai/task_creation.py`: `TaskCreationService` (:234), `create_todo` (:250) · `backend/ai/task_management.py`: `TaskManagementService` (:303) | **IMPLEMENTED** |
| Durable-task AI tools | `backend/ai/tools/task.py` (`create_task`), `backend/ai/tools/task_management_tools.py` (`task_list`, `task_inspect`, `task_transition`, `task_delete`) | **IMPLEMENTED** |
| Todo AI tools | `backend/ai/tools/todo_tools.py`: `todo_add` (:150), `todo_find` (:291), `todo_edit` (:406), shared resolver `resolve_todo_argument` (:51) | **IMPLEMENTED** |
| Todo step AI tools | `backend/ai/tools/todo_step_tools.py`: `todo_step_add` (:131), `todo_step_list` (:272), `todo_step_transition` (:380), `todo_step_edit` (:516), `todo_step_delete` (:630), shared `resolve_step_argument` (:77) | **IMPLEMENTED** |
| Tool registry / executor wiring | `backend/ai/tools/registry.py`: `create_default_registry` (:107) registers all 54 tools (Todo tools :223–:230); `backend/ai/tools/executor.py` (`ToolExecutor`, status labels incl. todo/step tools) | **IMPLEMENTED** |
| Todo manual UI (Glass UI) | `backend/bot/handlers/todo.py`: list (:103), completed list, detail (:184), steps (:243), mutations (:339), delete (:376), inputs (:539), `register()` (:760); Menu row `📋 Todo` → `panel:todo` (`backend/bot/handlers/misc.py:77`); handler registered in `backend/bot/router.py:104` | **IMPLEMENTED** |
| Taskloom (scheduler + occurrence coordination + notifications + event triggers) | `backend/ai/task_scheduler.py`, `backend/ai/task_execution.py`, `backend/ai/task_event_dispatcher.py`, `backend/ai/task_notifications.py` | **IMPLEMENTED** |
| Action representation inside a durable task | `ai_tasks.actions` = bounded JSON array of `{name, arguments}` | **PARTIALLY IMPLEMENTED** (see §6) |
| JSON-action parse/validate layer for interactive turns | `backend/ai/actions.py` (`ACTION_NAMES` :33, `EXECUTABLE_ACTION_NAMES` :70, `ALLOWED_FIELDS` :131, step validator :1086) | **PARTIALLY IMPLEMENTED** (it maps one request to one tool call; it is not the durable task model) |
| Ordered multi-step **execution** (chain, dependencies, wait, result flow) | — | **NOT IMPLEMENTED** |
| Per-action execution state / per-action retry / resume-at-action-N | — | **NOT IMPLEMENTED** |
| Action-result storage / references between actions | — | **NOT IMPLEMENTED** (see §10) |
| Conditional branching / condition evaluation inside a durable task | — | **NOT IMPLEMENTED** (see §14) |
| Durable conversational continuation bound to a pending workflow | — | **NOT IMPLEMENTED** (in-memory only; see §15) |
| Agent Execution Workflow product model | this document only | **DOCUMENTED ONLY** |

### 3.2 The Todo row itself (source-verified shape)

`TaskCreationService.create_todo` builds the row, `TaskRepository` validates it:

* `schedule_type = 'todo'` (`TODO_SCHEDULE_TYPE`, `task_repository.py:19`), the
  enum widened by `20260926000001`;
* `schedule = {}`, `notification_destination = {}`, `actions = []`,
  `next_run_at = None` — `_validate_task_input` **rejects** a todo that carries
  a `next_run_at`, and `create()` forces `initial = None` for `'todo'` so no
  boundary is ever fabricated;
* `label` = the owner's title, whitespace-collapsed, ≤ 256 chars
  (`MAX_TODO_TITLE_CHARS = 256`, `task_creation.py:59`);
* lifecycle `active ↔ completed` through the same CAS `version` as every task
  (`set_status` :649, `reopen` :813, `edit_todo_title` :829); `pause` is
  refused for a todo; `completed → active` is granted **only** to a todo
  (`_task_status_transition_allowed`), so a completed *scheduled* task stays
  terminal.

### 3.3 What Part 1 explicitly did NOT do (and why it matters for this task)

Part 1 reused `ai_tasks` **as a store**, not as an execution engine: a todo has
zero actions and no boundary, so `list_due_tasks` (the scheduler's only task
query) can never return it. That was deliberate and is the reason the checklist
model is safe — and also the reason it cannot express the requested workflow:
an ordered "step" created by Part 2 is not an action, has no tool binding, and
is never executed.

### 3.4 Report vs source — explicit disagreements (Todo system)

| # | Claim (where) | Source truth | Verdict |
|---|---|---|---|
| D-1 | `DATABASE_ARCHITECTURE.md` §15 (`todo_steps` lifecycle semantics) and `IMPLEMENTATION_REPORT.md` (Part 2, architecture): "Supabase writes run in one transaction (a step-insert failure rolls the whole creation back)". | `TaskCreationService.create_todo` (`task_creation.py:250`) calls `create_task` (statement A) and then `create_steps` (statement B). In `SupabaseTaskRepository.create_steps` (`task_repository.py:907`) statement B is **one multi-row INSERT** (atomic per statement), but on a transport failure it **degrades to the in-memory fallback** and *returns* the fallback rows — it does not raise. The compensating `delete_task` in `create_todo` therefore runs only when an error actually propagates (validation `ValueError`/`TypeError`, or a fallback failure). | **Partially wrong.** The true guarantee is: *steps of one call are all-or-nothing within their own INSERT*, and the pair is **not** transactional on the Supabase path — the compensating delete is a code path, not a database transaction. |
| D-2 | `IMPLEMENTATION_REPORT.md` (Part 2, security/ownership): step persistence is reported "durable-vs-fallback … same as tasks". | `_fallback_note_for` in `todo_step_tools.py` implements that honestly for `todo_step_add`/`todo_step_transition`/`todo_step_edit`. **But** `TodoAddTool` (`todo_tools.py:150`) with a `steps` argument reports `data["durable"]` purely from the **task** record's `fallback_backend` — a step INSERT that degraded to memory while the todo row succeeded durably is presented as durable. | **Incomplete.** A real, narrow honesty gap in the `todo_add(steps=…)` path; not fixed here (investigation only). |
| D-3 | `IMPLEMENTATION_REPORT.md` "Validation results … Full suite 5094 passed". | Not re-run in this pass; focused Todo suite (108) and DB-contract suite (59) pass. | **REPORTED, NOT RE-VERIFIED.** |
| D-4 | `backend/ai/tools/save.py` (`return_type`, lines 115 and 249): "ToolResult with **save_code** and confirmation message in data". | The actual call sites return `result_from_service(result, data={"mode": "deep"})` (`save.py:149`, `:288`); the save code exists only inside the human-readable confirmation text produced by `save_service.build_confirmation`. | **Declared contract not delivered.** Relevant to §10: today nothing can pass a created item's identity to a next action structurally. |

---

## 4. What the current `todo_steps` implementation actually does

### 4.1 Exact schema (source: migration `20260927000001`, validated by `_validate_step_input`)

```
todo_steps
  id            bigserial   PRIMARY KEY
  task_id       bigint      NOT NULL REFERENCES ai_tasks(id) ON DELETE CASCADE
  owner_id      bigint      NOT NULL                        -- denormalized; every query filters by it
  position      integer     NOT NULL  CHECK (position > 0)  -- 1-based, never renumbered
  title         text        NOT NULL  CHECK (nonblank AND length <= 256)
  status        text        NOT NULL  CHECK (status IN ('active','completed'))  DEFAULT 'active'
  version       integer     NOT NULL  CHECK (version >= 1)  DEFAULT 1            -- CAS
  completed_at  timestamptz NULL      CHECK ((status = 'completed') = (completed_at IS NOT NULL))
  created_at    timestamptz NOT NULL DEFAULT now()
  updated_at    timestamptz NOT NULL DEFAULT now()
  UNIQUE (task_id, position)                 -- the only index
RLS ENABLED; anon/authenticated: SELECT only (no write policy); writes are service-role
```

### 4.2 Exact relationships

`ai_tasks (schedule_type='todo') 1 ── * todo_steps`, ordered by `position`.
There is **no** relationship between a step and `ai_task_occurrences`, no
relationship between steps, and no cross-todo relationship. `todo_steps` is
**not** in the canonical reconciliation snapshot; the tests enforce it as a
*successor* table (`tests/test_canonical_schema_reconciliation.py`,
`SUCCESSOR_TABLES = ("todo_steps",)`) and as part 8 of the §31.3 setup block
(`tests/test_database_setup_order.py:252`, `:368`).

### 4.3 Exact lifecycle

`active → completed` (`set_step_status`/`complete_step`, sets `completed_at`,
version+1) and `completed → active` (`reopen_step`, clears it). Rename
(`rename_step`) and delete (`delete_step`) are CAS-guarded on the **step's**
version. Service rules: adding a step is refused on a completed todo; editing
a title never touches the parent; deleting a step never renumbers survivors;
completing the **parent** is refused while any step remains (`set_status` :649)
unless the explicit `complete_todo_with_steps` (:624) or
`action_status:"completed", complete_steps:true` is used.

### 4.4 Exact service methods

`TaskManagementService`: `owner_todo`, `list_steps`, `step_progress`,
`add_steps` (:417), `get_step`, `set_step_status`, `complete_step`,
`reopen_step`, `rename_step`, `delete_step`, `resolve_step` (:508),
`step_progress_of`, `complete_todo_with_steps` (:624). Repository:
`create_steps`/`list_steps`/`get_step`/`update_step`/`delete_step`
(`task_repository.py:367–371` interface; `:481`, `:907` implementations).
Task creation: `TaskCreationService.create_todo(..., steps=…)` (:250).

### 4.5 Exact AI tools, UI and tests

* Tools: `todo_step_add`, `todo_step_list`, `todo_step_transition`,
  `todo_step_edit`, `todo_step_delete` (+ `todo_add` with `steps`,
  + `task_transition` with `complete_steps`), registered in
  `registry.create_default_registry` (54 tools total, asserted by
  `tests/test_capability_exposure_tools.py`) and reachable through the JSON
  action layer (`actions.py:1086` `_validate_todo_step_action`).
* UI: `panel:todo` list, `panel:todo_done`, `panel:todo_task` detail,
  `panel:todo_steps` steps page; actions `todo_complete`, `todo_reopen`,
  `todo_delete`, `todo_complete_all`, `todo_step_complete`,
  `todo_step_reopen`, `todo_step_delete`; inputs `todo:new`, `todo_task:edit`,
  `todo_steps:add`, `todo_steps:edit`.
* Tests: `tests/test_todo_steps.py` (23), `tests/test_todo_steps_tools.py`
  (18), `tests/test_todo_tools.py` (14), `tests/test_todo_lifecycle.py` (11),
  `tests/test_todo_resolver.py` (12), `tests/test_todo_ui.py` (30). All pass
  (108 in the focused run of §2).

### 4.6 Direct answers to the audited questions about `todo_steps`

| Question | Answer | Evidence |
|---|---|---|
| Are steps executable? | **No.** A step has no tool name, no arguments, no schedule, no occurrence. Nothing in the codebase ever calls a tool with a step as input. | migration header; `TodoStepRecord` has only id/task_id/owner_id/position/title/status/version/completed_at/timestamps |
| Can one step invoke a ToolRegistry tool? | **No.** There is no field for it and no execution path that reads one. | `_validate_step_input` (`task_repository.py`) |
| Can a step consume the previous step's result? | **No.** There is no result channel at all. | idem |
| Can a step produce durable output? | **No** (only its own `status`/`completed_at`). | idem |
| Can a step wait for a scheduled time? | **No.** Steps have no time field and no boundary; the parent todo is unscheduled by construction. | `_validate_task_input` rejects `next_run_at` on a todo |
| Can a step fail independently? | **No.** `status ∈ {active, completed}`; the CHECK forbids any other value and `_validate_step_input` rejects anything else. | `STEP_STATUSES = frozenset({"active","completed"})` (`task_repository.py:39`) |
| Do retry semantics exist for steps? | **No.** The only retry machinery in the project (`backend/ai/retry.py`, `MAX_ATTEMPTS=3`) belongs to occurrences. | `backend/ai/retry.py` |
| Does per-step execution state exist? | **No.** Only completion state. | §4.1 |
| Can the parent todo "resume" after restart? | There is nothing to resume: no execution ever started. What *does* survive a restart is the todo row and its steps (durable rows + CAS), which is why the checklist needs no crash-recovery contract. | `TaskRepository` |
| Does the implementation support an agent workflow? | **No** — not partially, not by extension. It is a checklist: an ordered list of owner-authored strings with a two-state completion flag. An agent workflow needs action binding, a result channel, per-action state and boundaries; none of those exist on `todo_steps`. | §4.1–§4.6 |

### 4.7 The concrete architectural difference (why this is not a bug in Part 2)

Part 2's own migration header states the design intent: *"A step is NOT a
task: it has no schedule, no action, no occurrence, no notification
destination and no independent identity outside its parent Todo. It cannot be
executed, claimed or scheduled."* That sentence is the definition of a
checklist, and it is **correctly implemented**. The mismatch is at the product
level: the owner's request was mapped onto the word "step" (an ordered,
individually-completable list item) while the requirement is "an action that
runs and whose result feeds the next action". Part 2 did not accidentally build
the wrong thing badly — it built a different thing well. §5 states the delta.

---

## 5. Gap between current Todo and intended Agent Workflow

| Capability the workflow needs | Exists today? | Where it *would* have to live |
|---|---|---|
| A durable item that survives restart | **Yes** | `ai_tasks` (+ `ai_task_occurrences`) |
| An ordered list of named actions | **Yes** (`ai_tasks.actions`, 1–5) | `ai_tasks.actions`, validated at creation |
| A registered tool per action | **Yes** (validated against `ToolRegistry`) | creation eligibility + execution re-check |
| Bounded per-action arguments | **Yes** (tool-declared contract; 32 768-byte array bound) | `task_contract.py`, tool `parameters` |
| Action N reads action N−1's **result** | **No** | §10 — smallest missing capability |
| Durable per-action execution state | **No** | §11 / §17 |
| Independent per-action failure + retry | **No** (whole occurrence only) | §11 |
| Resume at the failed action after restart | **No** (a retry re-runs the whole list; a *running* occurrence is deliberately failed, never retried) | §11 |
| A wait between actions ("…then at 18:00") | **No** (one boundary per occurrence; a task has one schedule) | §12 |
| Conditional next action | **No** | §14 (deferred) |
| Owner answer consumed by a pending workflow | **No** (in-memory only) | §15 (deferred) |
| Checklist UI the owner already has | **Yes** | `todo_steps` — keep it; it is not the execution engine |

**Framing that follows from the table.** The Todo work is a *different product*
from the Agent Workflow, but it is not wasted: the workflow should be built on
the **scheduled-task spine that already exists** (`ai_tasks.actions` +
`TaskScheduler` + `TaskExecutionCoordinator` + the single `ToolExecutor`), and
the checklist (`todo_steps`) should be left exactly as it is. The next phase
adds the two missing things the table names explicitly — a **result channel**
and **per-action durable state/boundaries** — inside that spine, and nothing
else.

---

## 6. Existing action model

### 6.1 What an action is

An action is `{"name": <registered tool name>, "arguments": {...}}` stored in
the `ai_tasks.actions` JSONB array (`MAX_ACTIONS = 5`, `task_repository.py:28`;
whole-array bound `MAX_PAYLOAD_BYTES = 32768`, `:29`). At creation,
`TaskCreationService.create` runs `_action_eligibility_error`
(`task_creation.py`) on **every** action, reading the Tool's *own*
declarations:

* the tool must be registered in the process `ToolRegistry` (resolved through
  the already-constructed `Engine`; if no registry exists in the process the
  check is skipped here and re-proved at execution);
* `requires_owner_confirmation(tool)` must be false (a scheduled occurrence has
  no owner to approve);
* `requires_reply_context(tool)` must be false;
* declared `required_arguments` / `required_any_arguments` must be present,
  unless the argument is a content field and the task carries an
  `ai_instruction` (then it is generated per occurrence and validated by the
  preparation policy);
* declared `enum` / `minimum` constraints are enforced via
  `_declared_constraint_error`.

Additionally, `backend/ai/task_contract.py` enforces **provenance** for
message-referencing arguments (`MESSAGE_ID_ACTION_ARGUMENTS`,
`MESSAGE_LINK_ACTION`) so a provider cannot invent a Telegram message id or
link.

### 6.2 What the model can and cannot express (the product-model questions)

| # | Question | Answer |
|---|---|---|
| 1 | Is `ai_tasks.actions` already capable of representing the workflow? | **Partially.** It is an ordered list of tool calls for **one** execution boundary. It cannot express a dependency between actions, a per-action state, a wait, or a branch. |
| 2 | Can an action already identify a ToolRegistry tool? | **Yes** — `name` is validated against the registry at creation and again at execution (`TaskExecutionCoordinator.execute`: unregistered ⇒ occurrence fails with `unregistered_action`). |
| 3 | Can an action contain bounded arguments? | **Yes** — `arguments` is a JSON object, bounded by the 32 768-byte array payload and by the tool's declared schema; there is no separate per-argument byte bound. |
| 4 | Can an action refer to the output of a previous action? | **No.** No placeholder, no reference syntax, no resolver exists. |
| 5 | Where should action execution state live? | Today only at occurrence level (`ai_task_occurrences.status`). The smallest correct place for per-action state is **inside the occurrence** (a bounded, documented JSON record) or, if it must be independently CAS-mutated, **one additive table**. Decision deferred to §17/§23 — no schema change is implied by this document. |
| 6 | Is `ai_task_occurrences` sufficient for execution history? | **Yes for what it is** — one row per (task, boundary), idempotent `occurrence_key`, attempt counter, `error_metadata`, `result_metadata`, status history. **No** for per-action history: one occurrence = one whole action list. |
| 7 | Is a separate action/step table actually required? | **Not for storage of the actions** (`ai_tasks.actions` already holds them, and `action_snapshot` freezes them per occurrence). A new table is required *only* if per-action state must be queryable/constrained per row or exceed the bounded JSON budget. |
| 8 | Can `todo_steps` be reused? | **No** — see §4.6/§4.7. Reusing it would mean redefining its status model (adding execution states), giving it an action binding and an occurrence relationship: that is a rewrite of its documented meaning, not reuse. |
| 9 | If not, what is missing? | (a) result references; (b) durable per-action state; (c) boundary splitting / waiting; (d) failure isolation + resume; (e) an AI creation path that produces such chains from a Persian request; (f) a bounded result-envelope contract. |
| 10 | Should `todo_steps` remain, evolve, or be replaced? | **Remain, unchanged.** Do not evolve it into an execution log (it has no execution semantics and its migration comments promise the opposite). Replacing it is unnecessary: the workflow lives on scheduled tasks, which already have occurrences. |
| 11 | Minimum additional durable state? | One bounded per-action record per occurrence: position (1-based), tool name, status, and a bounded result/envelope reference. Everything else already exists. |
| 12 | What must stay in application memory? | The `ToolRegistry`, the `ToolExecutor`, the dispatcher's round loop, `SessionManager`/`ConversationSession`, `PendingConfirmationStore`, the Taskloom wizard draft, provider state, and all previews. Rule: **only state needed to resume after restart belongs in the database.** |

### 6.3 Executing an action list today (the whole path)

```
TaskScheduler.run()                                  (backend/ai/task_scheduler.py)
  → repository.list_due_tasks(...)                   (status='active' AND next_run_at <= now)
  → catch_up_occurrence(parse_schedule(...))          → scheduled boundary
  → occurrence_key(task.id, scheduled)                (deterministic, idempotent)
  → repository.create_occurrence(...)                 (action_snapshot = task.actions, definition_version = task.version)
  → repository.claim_occurrence(...)                  (CAS: claimed/retry_pending/interrupted → running)
  → TaskExecutionCoordinator.execute(occurrence)      (backend/ai/task_execution.py:325)
        → context: fresh self client + extra[SCHEDULED_OCCURRENCE_EXTRA]=True
                   + extra["chat_id"] from task.notification_destination
        → [if ai_instruction] durably prepared action OR bounded preparation (tools=[] → no execution possible)
        → ToolExecutor.execute_calls(all actions, one batch)   (sole tool.execute() caller)
        → repository.transition_occurrence("succeeded" | "failed" | "retry_pending")
        → repository.advance_next_run(...)             (from the SCHEDULED boundary, never from finish time)
  → (optional) notifier.notify_persisted(...)
```

No second executor, no second scheduler, no model-visible execution authority.
The model's provider call during preparation is deliberately made with
`tools=[]`, so the provider can only *emit arguments*, never trigger a tool.

---

## 7. Existing ToolRegistry / ToolExecutor path

* `ToolRegistry` (`registry.py:32`) = the single lookup table; tools are
  registered once by `create_default_registry` (`:107`), constructed by the
  runtime supervisor with the process `ToolContext`. **54 tools** are
  registered (asserted by `tests/test_capability_exposure_tools.py`).
* `ToolExecutor` (`executor.py`) is the **sole** component that calls
  `tool.execute()`. It enforces `MAX_TOOLS_PER_TURN = 5` (`:50`), the per-tool
  timeout `TOOL_TIMEOUT_SECONDS = 10` (`:51`) unless the tool declares
  `long_running=True` (e.g. Deep Save), the owner-confirmation gate
  (`requires_owner_confirmation`), argument-object validation, registry lookup,
  tool history and audit persistence, and it never raises.
* Interactive path: `Dispatcher` (`backend/ai/engine/dispatcher.py`) runs up to
  `MAX_TOOL_ROUNDS = 3` (`:58`) provider/tool rounds **in one turn**, feeding
  each round's results back into the conversation
  (`self._conversation.add_tool_result(...)`, `metadata["tool_results"]`), with
  a verbatim short-circuit for authoritative read tools and a deferred verbatim
  read for read-then-mutate requests.
* Scheduled path: **one** batch of `execute_calls` per occurrence (above). The
  scheduled path therefore has *no* multi-round result feedback — the round
  loop is an interactive-only capability.

**Consequence for the workflow:** the only allowed way for a durable task to do
anything is a ToolRegistry lookup executed by the ToolExecutor. Any future
action-chain feature must be expressed as (a) registered tools and (b) data the
coordinator may pass between them — never as new "capabilities" the model can
reach directly. AI/tools stay stateless wrappers over `backend/services/`;
Telegram work stays in `backend/telegram_api` + services; lifecycle stays in
`RuntimeSupervisor`; orchestration stays in the `Dispatcher`; provider routing
stays in the `ProviderManager`.

---

## 8. Existing scheduler / task occurrence model

`backend/ai/task_scheduler.py`:

* one process-local `TaskScheduler` per owner, started/stopped by the runtime;
  `WAKE_INTERVAL_SECONDS = 60.0` (`:22`) is an upper bound (the loop sleeps
  until the nearest boundary via the advisory `next_run_hint`),
  `MAX_CONCURRENT_EXECUTIONS = 4` (`:31`), `MAX_RETRIES_PER_WAKE = 10` (`:16`),
  `MAX_TASKS_PER_WAKE = 10`, `MAX_SWEEPS_PER_WAKE = 20`;
* `recover()` resolves unfinished occurrences **once** per start:
  * `claimed` whose `scheduled_for` is still in the future ⇒ left untouched
    (pre-created prepare-ahead rows must execute exactly at their boundary);
  * `running` ⇒ `failed` with `error_class = restart_side_effect_uncertain`
    (**never retried** — the process may already have produced a side effect);
  * `claimed` (past due) ⇒ `interrupted` ⇒ `retry_pending` (attempt+1) or
    `failed` when attempts are exhausted;
* due retries (`list_due_retry_occurrences`) execute through the same claim +
  coordinator path;
* **prepare-ahead**: for recurring AI-assisted tasks, the next occurrence is
  pre-created and its action prepared (no side effects) within
  `PREPARE_AHEAD_HORIZON_SECONDS = 120.0` (`:37`), persisted in
  `ai_task_occurrences.preparation_metadata` as a `PreparedAction` stamped with
  the task's `definition_version`; at the boundary the coordinator
  **re-proves** the stamp (same task version, same tool name as the snapshot,
  contract-valid arguments, still policy-valid) before executing; anything
  stale is ignored and the occurrence-time path is used instead.

Occurrence statuses (`task_repository.py:18`) and transitions (`:69`):

```
claimed → {claimed, running, cancelled, expired, interrupted}
running → {running, succeeded, failed, retry_pending, cancelled, interrupted}
retry_pending → {retry_pending, running, failed, cancelled, interrupted}
interrupted → {interrupted, retry_pending, failed}
succeeded | failed | cancelled | expired → themselves only
```

Task statuses: `active, paused, completed, failed, expired, deleted`
(`TASK_STATUSES`); allowed task transitions at `task_repository.py:68` plus the
todo-only `completed → active` reopen edge.

`result_metadata` today is written **only** for a succeeded occurrence and
contains exactly: `action_count`, `successful_action_count`, `duration_ms`,
`terminal_status` (bounded by `MAX_METADATA_BYTES = 8192`,
`task_execution.py:36`). `error_metadata` carries `error_class`, `attempt`,
`action_count`, `successful_action_count`.

---

## 9. Existing Saved Items integration

| Question | Verified answer |
|---|---|
| How is a saved item identified? | By its **save code** (`S####`, `db.get_next_save_code()`), unique in `saved_items.save_code`; the row also holds `saved_chat_id`/`saved_msg_id` (the Saved Messages copy), `display_name` and `tags`. |
| How are tags stored? | `saved_items.tags` (owner tags) + `saved_items.display_name` (Save V2 part 1, migration `20260921000001`); the search indexes of `20260922000001` back the resolver. |
| How do owner tags differ from metadata tags? | Since Save V2 the **generated** `#saved*` hashtags are **presentation only** and are no longer persisted into `tags` (`save_service.py` `caption_hashtags`, and its comment on the column staying empty); the `tags` column holds only the owner's own words. In the saved Telegram caption the owner's tags render as their own `🏷 Additional tags` section (`ADDITIONAL_TAGS_PREFIX`, `save_service.py:106`), synchronized by `with_additional_tags`. Bounds/validation: `normalize_tags`, `MAX_SAVE_TAGS`. |
| How are save/search/tag operations exposed to the ToolRegistry? | `save` / `save_by_link` (create, with optional `display_name` + `tags`, honoring an explicit "no tags" refusal), `search` (saved-items keyword search → `discover_service.do_find`), `list_saves`, `retrieve_save` (re-send into the requesting chat), `preview_save` (metadata only), `delete_save`, `rename_save`, `update_save_tags` (modes `add`/`replace`/`remove` → `retrieve_service.do_edit_tags`). All owner-scoped through `backend/services/`. |
| Can an action pass a saved-item identity to the next action? | **Not today.** The identity exists (`S####`), and `retrieve_save` *returns* `data["save_code"]`, but the execution coordinator discards per-action `data` (only counts are persisted) and `save`/`save_by_link` do not even return the code in `data` (D-4 in §3.4). |
| Can existing Save V2 resolution be reused? | **Yes** — `retrieve_service.resolve_saved_items(query)` gives the deterministic 0/1/N answer used by `retrieve_save`, `rename_save`, `update_save_tags` and the AI prompt. A future TAG action addresses the item exactly like `retrieve_save` does: by `save_code` when a previous result produced one, or by `query` otherwise. |
| What ambiguity behavior exists? | 0 matches ⇒ honest not-found; 1 ⇒ act; **≥2 ⇒ candidate list and NO mutation/send**, with the model explicitly instructed to ask the owner (never to pick). `MAX_TODO_CANDIDATES = 8` mirrors the same bounded-list convention for todos. |

**Consequence.** The "save it, tag it, bring it at 18:00" half of the example is
*already fully implementable as separate tools* — what is missing is only the
**chain** (the identity flowing from save → tag → retrieve) and the **wait**.

---

## 10. Action-result propagation

### 10.1 What exists

| Layer | What it carries | Durable? |
|---|---|---|
| `ToolResult` (`backend/ai/tools/base.py:67`) | `success`, `message` (owner-facing text), `data` (free-form dict), plus tool name/latency at execution | no |
| `ToolExecutionResult` (`executor.py`) | `tool_name`, `success`, `message`, `data`, `latency_ms`, `needs_confirmation`, `error` | no |
| Interactive turn | every round's results are appended to `metadata["tool_results"]` and added to the conversation (`add_tool_result`), then rendered into the prompt's `TOOL_RESULTS` section (`backend/ai/prompt/builder.py:360`) for the next provider round (`MAX_TOOL_ROUNDS = 3`) | no (turn-scoped, in-memory) |
| Occurrence | `result_metadata` = counts only (`action_count`, `successful_action_count`, `duration_ms`, `terminal_status`); `error_metadata` = error class/attempt/counts; `preparation_metadata` = the *prepared arguments* for a single-action AI task | **yes** (bounded JSON, ≤ 8192 bytes each) |
| Task delivery | `TaskExecutionCoordinator._deliver_result` (`task_execution.py:685`) sends the **concatenated tool messages** (≤ `MAX_RESULT_DELIVERY_CHARS = 4000`) to the task's trusted destination when the definition asked (`deliver_result`) | side effect only, no stored result |

### 10.2 What is missing (the smallest missing capability)

1. **A structured result envelope per action.** Today `data` is arbitrary and
   is dropped for scheduled executions; even where it exists (`retrieve_save`),
   nothing consumes it. The smallest contract is a *bounded* envelope per
   action: `{position, tool, status, output: {…}}` where `output` holds only
   the keys the tool declares as consumable (e.g. `save_code`, `item_ids`,
   `text`, `count`), serialized within the existing 8 192-byte metadata budget
   or in the one additive table of §17.
2. **A reference syntax for an argument.** One explicit, bounded placeholder
   (e.g. a structured `{"$ref": {"step": 1, "field": "save_code"}}`) that is
   (a) validated at task creation against the declared output fields of the
   referenced tool, and (b) **resolved by the coordinator at execution time**,
   failing the occurrence closed when the reference is unresolvable. No
   arbitrary JSON blobs and no expression language.
3. **A per-action execution record** (§11/§17) so a partially completed chain
   can be inspected, resumed and retried without re-running the side effects
   that already happened.

Notes that constrain the design:

* Do **not** let the model resolve references — the model must not be able to
  invent a value that reaches a tool. References are data in the task
  definition, resolved deterministically by the coordinator.
* Do **not** widen existing metadata shapes silently: `preparation_metadata`
  is validated against an **exact** key set (`{"kind","definition_version",
  "prepared_at","action"}`); `result_metadata`/`error_metadata` accept arbitrary
  object keys within their byte bound (verified in `_validate_occurrence_input`).
  A new per-action record must therefore be a *new documented shape*, not a
  mutation of the prepared-action shape.
* Reuse the existing **bounded JSON convention** (`_bounded_metadata`,
  `MAX_METADATA_BYTES = 8192`) rather than inventing a new serialization.

---

## 11. Failure / retry / resume semantics

### 11.1 The audited scenario: Action 1 succeeds, Action 2 fails, process restarts

Today, in order:

1. `TaskExecutionCoordinator.execute` runs **all** actions in one
   `executor.execute_calls(...)` batch (`task_execution.py:325`); each tool
   call is independent — no call receives another's output.
2. If **any** action failed, the *whole occurrence* goes through
   `handle_failure` (`:741`) → `classify_failure` (`backend/ai/retry.py`):
   retryable + `can_retry(attempt)` ⇒ `retry_pending` with
   `retry_at = updated_at + retry_delay(attempt)` and `attempt+1`; otherwise
   `failed`. `successful_action_count` is recorded in `error_metadata`, but
   that count does **not** change what runs next.
3. On restart, `TaskScheduler.recover()`: an occurrence in `running` becomes
   `failed` with `error_class = restart_side_effect_uncertain`, and a past-due
   `claimed` becomes `interrupted` → `retry_pending` (attempt+1) or `failed`.

### 11.2 Answers to the section's questions

| Question | Answer |
|---|---|
| What state exists? | `ai_tasks` row (definition + `version`), one `ai_task_occurrences` row (status, attempt, `action_snapshot`, `error_metadata`, `result_metadata`, `retry_at`), plus the side effects already produced in Telegram/DB. **No per-action state.** |
| Can the workflow resume at Action 2? | **No.** There is no per-action position anywhere; the unit of state is the whole action list. |
| Can Action 1 be accidentally executed again? | **Yes — on a retry of the whole occurrence.** `retry_pending` (armed automatically for retryable failures, and by restart recovery for a past-due `claimed`) re-executes the **entire** `action_snapshot`, including actions that already succeeded. The project mitigates only the *unprovable* case (`running` after a restart is failed, never retried) — precisely to avoid duplicate side effects. The re-run risk for the succeeded-actions case is a **real, documented gap**, not an oversight: nothing in the schema records "action 1 is done". |
| Can the system distinguish pending / running / succeeded / failed / retry_pending / blocked / cancelled / completed? | At **occurrence** level: yes for `claimed` (= pending whenever unclaimed), `running`, `succeeded`, `failed`, `retry_pending`, `cancelled`, `expired`, `interrupted`. At **task** level: `active, paused, completed, failed, expired, deleted`. **`blocked` does not exist anywhere** (no state, no producer) and must not be introduced without a concrete producer. `completed` is a task status, not an occurrence status. |
| What would be required for resume-at-action-N? | Durable per-action state (position → status → result ref) + a coordinator rule ("skip actions already `succeeded` unless explicitly re-run") + a bounded answer to "what does re-running a partially executed action list mean?" for non-idempotent tools. This is the smallest missing capability of §11 and the decision of §23. |

**Safety rule that must survive any future change:** the existing
`running ⇒ failed(restart_side_effect_uncertain)` contract is deliberate and
must not be relaxed into "retry it" — an unknowable side effect must stay
unknowable, not become a duplicate Telegram mutation.

---

## 12. Scheduling semantics

### 12.1 EXISTS NOW

* `ai_tasks.schedule_type ∈ {once, interval, daily, weekly, event, todo}`;
  `next_run_at` is the due instant (`NULL` for `event` and `todo`).
* `TaskScheduler` (single scheduler, started by the runtime supervisor) claims
  due tasks, creates **one occurrence per boundary** with the deterministic
  `occurrence_key = "<task_id>:<ISO boundary>"` (and
  `"<task_id>:ev:<chat>:<msg>"` for event tasks), executes it, then advances
  `next_run_at` **from the scheduled boundary** (`catch_up_occurrence`) so a
  delayed wake never shifts the cadence.
* Recurring AI-assisted tasks get **prepare-ahead** inside a 120 s horizon.
* Retries are scheduled through `retry_at` and served by the same wake loop.
* `event` tasks are driven by `backend/ai/task_event_dispatcher.py` on incoming
  messages, resolved into the same occurrence/execution path.
* Lifecycle safety: a `todo` can never carry `next_run_at`; `paused` and
  terminal states clear it; resuming recomputes the next occurrence from the
  stored schedule.

### 12.2 REQUIRED LATER (for "ساعت ۶ ازت میخوامش")

Today a task has exactly **one** schedule and one occurrence = one *whole*
action list at one boundary. The example needs **two boundaries inside one
owner intent**: actions 1–3 now, action 4 at 18:00 — plus the *dependency* that
action 4 delivers what action 2 saved. Nothing today can express "do part of
the list, then wait, then do the rest":

* executing the whole list at 18:00 is wrong (the search + save would happen at
  18:00, not now);
* creating two independent tasks loses the dependency entirely (task B cannot
  see task A's save code);
* the prepare-ahead horizon is **not** a wait mechanism (it prepares content,
  never delays side effects).

The smallest models that could express it — to be decided by the owner (§23),
*not* to be built in this pass:

* **(a) continuation boundary** — the task records that its next execution
  boundary resumes the action list at position N (durable next-action index),
  reusing `ai_task_occurrences` (a second occurrence for the same task) and the
  existing scheduler. No new scheduler, no new table if the index fits the
  bounded metadata convention.
* **(b)** two tasks + an explicit reference the second task resolves (needs the
  same result-channel primitive as §10 anyway).

Either way: **do not add a scheduler**, **do not add a timer service**, and
**do not** turn `prepare-ahead` into a generic delayed-execution mechanism.

**Source-verified correction (Phase 3B, implemented).** The "durable
next-action index" and the *second occurrence* turned out to be unnecessary.
The wait is one optional reserved field on the action itself —
`not_before`, an ISO-8601 timestamp, preserved by the candidate boundary,
resolved and bounded at creation against the task's own timezone (stored as an
absolute UTC instant) and re-proved before any execution. The occurrence then
parks on that instant through the eligibility pair it already has:
`status="retry_pending"` + `retry_at=<boundary>`, the one non-terminal state the
schema CHECK, the claim CAS, `list_due_retry_occurrences` and the recovery
exemption all already agree on — so no new status, no new column, no second
occurrence, no migration, and the wake loop stays generic (it selects the
occurrence by instant, never by meaning). Resume needs no index either: the
stored action list is re-walked and the Phase 3A per-action run record (which
the park writes to both metadata channels) skips every action already recorded
`succeeded`. The waiting action itself stays `pending` and is never marked
succeeded by the wait, and because a parked occurrence is `retry_pending` —
never `running` — a wait can never be mistaken for a running action or resolved
as `restart_side_effect_uncertain`.

---

## 13. Ownership / security boundaries

| Boundary | Current state (source-verified) |
|---|---|
| Owner-only access | every handler is registered on outgoing messages and gated by `is_owner` (`backend/bot/handlers/guard.py`). |
| Owner scoping in persistence | every repository read/write filters by `owner_id` (including `update_step`/`delete_step`); a foreign/unknown id is indistinguishable from not-found. |
| RLS | all tables RLS-enabled; `anon`/`authenticated` have SELECT only (no write policy), including `todo_steps`; writes use the service-role client. |
| Model authority | the model can only emit tool *calls*; `ToolExecutor` is the sole caller of `tool.execute()`; tool names/arguments are validated against the Tool's own declarations before persistence and re-validated at execution (`_validate_prepared_calls`, registry look-up). |
| Scheduled context | `TaskExecutionCoordinator` writes `extra[SCHEDULED_OCCURRENCE_EXTRA]=True` from runtime state and copies `chat_id` **only** from `task.notification_destination`; the model can never choose a destination, and durable task creation from that context is refused (`scheduled_creation_error`). |
| Provenance | message ids must be grounded in the owner's request/reply (`trusted_message_ids`), links must literally occur in the request (`message_reference_provenance_error`). |
| No direct model access to | Telegram RPC (only through tools → `telegram_api`/services), SQL (no DB client is reachable from a provider), shell/filesystem (nothing exists), arbitrary HTTP (providers only; `web_search` is a capability tool), arbitrary DB operations. |
| Lifecycle | `RuntimeSupervisor` remains the single recovery authority; a scheduled execution never reconnects or rebuilds anything. |
| AI orchestration / routing | `Dispatcher` + `ProviderManager` remain the only orchestration/routing layers. |

**How a workflow can execute multiple registered tools without violating
these boundaries:** keep the chain *inside* the occurrence's action list, and
let the coordinator pass **data** (validated, bounded) between calls, while the
ToolExecutor keeps calling `tool.execute()`. No new authority is introduced:
the coordinator already holds the owner scope, the trusted context and the
occurrence; it only needs a per-action loop with fail-closed reference
resolution.

---

## 14. Question + command future requirement

Requirement (deferred): «… اگر تعطیل بود به فلانی پیام بده، اگر نبود فقط نتیجه
رو ذخیره کن» — a request that mixes a question with commands and needs a
**branch** on the answer.

What exists today:

* **Presentation of questions**: `ai_config.show_question` toggles how the
  thinking/failure text is rendered (`backend/bot/handlers/ai_unified.py`
  `_show_question_pref`, `format_thinking`, `format_failure`) — presentation
  only, no semantics.
* **Ask-the-owner within a turn**: the interactive Dispatcher runs up to 3
  provider rounds, so a model *can* read a tool result and then act on it in the
  next round inside one turn; the JSON action layer has an explicit `clarify`
  kind; the ambiguity resolvers (todos, saved items, delete scope) return
  candidate lists and refuse to guess.
* **Owner approval of a blocked action**: `PendingConfirmationStore`
  (`backend/ai/confirmation.py`) — server-created, frozen arguments, single-use,
  120 s TTL, one pending per (owner, chat); the Dispatcher re-issues the stored
  call through `ToolExecutor.execute_confirmed()`.
* **Structured completion of an incomplete request**: the Taskloom wizard
  bridge (`ai_unified._open_task_wizard`, wizard draft state in
  `backend/bot/handlers/taskloom.py`, per-owner, in memory).

What does **not** exist:

* branching inside a durable task (no condition field, no predicate evaluator,
  no alternative action list);
* inspection of a **previous action's structured result** by a later action
  (there is nothing to inspect — §10);
* conditional next action, `if/else` in the action list, natural-language
  decision **persisted** for later boundaries;
* any notion of "blocked waiting for the owner's answer" on a task/occurrence.

Conclusion: this is a **later phase**. Its natural home is the same action list
plus the result channel of §10 — a bounded condition over a **declared output
field** of a previous action (deterministic, enumerable), not an expression
language, and never a second orchestration engine. Do not build it now.

---

## 15. Conversational continuation future requirement

Requirement (deferred): the assistant asks «کجا ذخیره کنم؟» and the owner's
next message («Saved Messages.») completes the **pending workflow**.

What exists today:

* `SessionManager` / `ConversationSession` (`backend/ai/conversation/session.py`):
  RAM-only runtime bookkeeping (state machine, `current_flow`, `pending_action`,
  `last_tool`, `last_reply_target`); explicitly **not** durable, lost on restart,
  keyed by `session_id`.
* Telegram conversation history (`ai_messages` via `backend/ai/database/`) and
  reply-to-AI activation (`backend/bot/handlers/ai_unified.py`): replying to an
  AI message already continues a *conversation* with real context, and the
  replied-to content is injected as context.
* `PendingConfirmationStore` (in-memory, TTL) and `helper/input_state.py`
  (in-memory, 120 s state expiry) — both are **input flows**, not workflow
  state, and both die with the process.
* Taskloom wizard draft (in-memory, per owner, in the handler).
* The deterministic resolvers already answer with **candidate lists** when a
  reference is ambiguous — the owner's answer is what a future pending question
  would consume.

What does not exist: a durable record of "this task/occurrence is waiting for
the owner's answer (question text, expected field, which action, expiry)", and
a deterministic binding rule that routes the *next owner message* into that
record. **A workflow therefore cannot be resumed across messages after a
restart**, and even within a live process there is no workflow-level pending
question — only the conversation-level one.

Smallest shape for the later phase (documented, not built): one bounded
pending-question record owned by the task/occurrence (question, target action
position, expected argument name, created/expires, asked message id), plus a
resolution rule reusing the existing reply-to-AI/trigger entry point; the
answer becomes the argument of the pending action and the chain continues.
Reuse `ai_config`/`ai_messages`/the existing handler — **no new session store**.

---

## 16. Required architecture for the next implementation phase

The product model, in the project's own vocabulary (no new framework):

```
ai_tasks (ONE durable workflow)                       <-- reuse
  label, schedule_type, schedule/next_run_at          <-- reuse (the delivery boundary)
  actions = [ action 1 … action N ]                    <-- reuse (ordered, 1..5, validated)
  ai_instruction (optional)                            <-- reuse (per-occurrence generation)
  notification_destination                             <-- reuse (trusted destination)

ai_task_occurrences (ONE row per execution boundary)   <-- reuse
  status/attempt/retry_at/error_metadata               <-- reuse
  action_snapshot                                      <-- reuse (frozen definition)
  NEW: bounded per-action run record (position, tool, status, output refs)   <-- the gap
  NEW: optional continuation index (resume the list at position N at this boundary)

execution:
  TaskScheduler  → claim occurrence → TaskExecutionCoordinator
      → for each action in order:
            resolve arguments (deterministic, fail-closed; only earlier actions may be referenced)
            ToolExecutor.execute_calls([that call])           <-- the single executor
            persist the action's bounded result envelope + status
      → occurrence status from the aggregate outcome
      → (optional) trusted delivery of the final message(s)
```

Design rules that follow from the source audit:

1. **One workflow = one `ai_tasks` row.** The action list is already there;
   only the per-action record is new.
2. **One execution authority.** The coordinator gains a per-action loop; it
   must keep calling `ToolExecutor` (never a tool directly) and must keep the
   single-claim CAS as the duplicate guard.
3. **One scheduler.** A wait is a *boundary of the same task* (continuation
   index) or a separate task chosen by the owner — never a new timer/scheduler.
4. **References are data, resolved deterministically.** A reference may only
   name an **earlier** action and a **declared output field**; unresolvable
   references fail the occurrence closed (never guessed, never executed with a
   placeholder).
5. **Bounded everything.** ≤ 5 actions (existing), bounded arguments, bounded
   results (8 192-byte metadata budget or one row per action), bounded retries
   (existing `MAX_ATTEMPTS = 3`).
6. **Owner-only, service-role writes, RLS unchanged, no new secrets.**
7. **Checklist stays a checklist.** `todo_steps` is untouched; the workflow
   does not extend it.
8. **No model execution authority.** The model may *propose* a chain (through
   the existing candidate/creation boundary) and *generate content* where
   `ai_instruction` authorizes it; it may never resolve a reference, choose a
   destination, or bypass the registry.

---

## 17. Minimum database changes, if any

**Recommended: zero migrations for the first slice; at most ONE additive
migration if per-action state must be independently mutable.**

| Option | Change | Sufficient for | Limits |
|---|---|---|---|
| **A (recommended first)** | **None.** Store the bounded per-action run record inside the occurrence's existing `result_metadata` (free-form object, ≤ 8 192 bytes, already validated) and the failure detail in `error_metadata`. Keep the occurrence statuses as they are and derive the per-action status from the record. | Chains of ≤ 5 actions, small envelopes (`save_code`, ids, short text), resume-at-action-N, per-action retry skipping. | 8 192 bytes total; not independently CAS-mutable per action; the shape must be documented and validated by a new contract (application-only change). |
| **B (only if needed)** | ONE additive migration: a table for per-action runs under an occurrence (e.g. `ai_task_occurrence_actions`: `occurrence_id` FK → `ai_task_occurrences(id) ON DELETE CASCADE`, `owner_id`, `position`, `tool_name`, `status`, bounded `output` jsonb, CAS `version`, timestamps; `UNIQUE (occurrence_id, position)`; RLS SELECT-only; a part 9 of the §31.3 setup block). | Large results, per-action CAS, per-action history queries, > 8 192 bytes. | More surface: repository methods, fallback mirroring, canonical-doc updates, new tests. |

Explicitly **not** required / not allowed:

* no `ai_workflows` table, no `ai_workflow_steps` table, no DAG edge table;
* no changes to `ai_tasks` (its `actions` column already models the chain);
* no changes to `todo_steps` (it is a checklist; widening it would be the exact
  architectural mistake this investigation exists to prevent);
* no new statuses on existing tables without a concrete producer (notably
  `blocked` — nothing can produce it today);
* no change to the canonical reconciliation snapshot: a new table would be a
  **successor** part of §31.3, exactly as `todo_steps` was, and the frozen
  byte-identical trio stays untouched.

**No SQL was executed and no Supabase object was contacted by this
investigation.**

---

## 18. Minimum application changes, if any

If the owner selects Phase 3A (§22), the smallest set of application changes is:

1. **A bounded action-result + reference contract** — one new documented shape
   in `backend/ai/task_contract.py` (reference syntax + envelope validation +
   declared consumable output fields per tool), validated within the existing
   8 192-byte metadata convention.
2. **A per-action loop in `TaskExecutionCoordinator.execute`** — iterate the
   occurrence's `action_snapshot` in order, resolve each action's arguments
   (fail-closed), call `ToolExecutor` once per action (a reference-free list may
   keep today's single batch, which is byte-identical behavior), and persist the
   bounded per-action record through the existing repository transition (Option
   A of §17) or through the new table's repository methods (Option B).
3. **A skip/resume rule** — actions already recorded `succeeded` are skipped on
   a retry, unless the tool is declared safe to re-run (or the owner explicitly
   asks). This is the change that stops Action 1 from repeating.
4. **Creation-time validation of references** — the existing
   `_action_eligibility_error` path must additionally reject a reference to a
   later action, to a non-existent position, or to an undeclared output field.
5. **Tool output declarations** — a small, explicit list of "consumable output
   fields" for the handful of tools the workflow needs (`save`/`save_by_link`
   → `save_code`; `web_search`/`search` → bounded result text/ids;
   `retrieve_save` → delivered item; `update_save_tags` → `save_code`). This is
   a *small explicit contract*, not a plugin system; `save`'s declared
   `return_type` (D-4) shows the fix is to actually put the code in `data`.
6. **Optionally, one AI-tool/creation-path adjustment** so a natural-language
   request can produce such a chain; the safest first version is a *bounded,
   explicit* chain (the owner names the actions), validated by the existing
   candidate/creation boundary, rather than free-form multi-tool planning.
7. **A continuation boundary** (only if §12.2 option (a) is chosen) — the
   occurrence records the next action index and the scheduler creates the next
   occurrence for the same task at the new boundary. Reuses the scheduler and
   the occurrence table.
8. **Tests** — repository/service contract tests, coordinator per-action tests
   (order, reference resolution, fail-closed, skip-on-retry), restart-recovery
   tests (unstarted vs partially run vs running), and honesty tests
   (durable-vs-fallback) for any new write.

Explicitly **not** an application change: a new executor, a new scheduler, a
new task repository, a new tool registry, a new session store, a new
persistence path, or any tool that performs Telegram RPC/SQL/fs/HTTP outside
the existing services.

---

## 19. Existing components that can be reused

* `ai_tasks` + `ai_task_occurrences` (definitions, ordered `actions`,
  `action_snapshot`, attempts, `retry_at`, `error_metadata`,
  `result_metadata`, `preparation_metadata`).
* `TaskRepository` (+ `InMemoryTaskRepository` fallback + honest
  degraded-store classification), owner scoping and CAS `version` everywhere.
* `TaskCreationService` / `TaskManagementService` — creation validation
  (`_action_eligibility_error`, provenance, instruction grounding), lifecycle,
  deterministic resolvers (`resolve_todos`, `resolve_todo_target`,
  `resolve_step`), CAS updates.
* `TaskScheduler` — the single scheduler: due queries, `catch_up_occurrence`,
  deterministic `occurrence_key`, claim CAS, bounded concurrency, retries,
  restart recovery, prepare-ahead.
* `TaskExecutionCoordinator` — the single execution coordinator: trusted
  context, preparation re-proof, policy re-enforcement, failure classification,
  trusted result delivery.
* `ToolExecutor` + `ToolRegistry` + `Tool` contract (`required_arguments`,
  `required_any_arguments`, `permission_level`, `long_running`,
  `timeout_seconds`, `parameters` enums/minimums) — the only execution path,
  with its confirmation, timeout and history guarantees.
* `backend/ai/retry.py` (classification, `MAX_ATTEMPTS=3`, backoff),
  `backend/ai/preparation_policy.py` (deterministic content policy),
  `backend/ai/task_contract.py` (bounded data contracts + provenance).
* Saved Items services/tools (`save`, `save_by_link`, `search`, `list_saves`,
  `retrieve_save`, `preview_save`, `rename_save`, `update_save_tags`,
  `delete_save`) and `retrieve_service.resolve_saved_items` (0/1/N, never a
  guess) — the SAVE/TAG/RETRIEVE actions already exist as validated tools.
* `web_search` (You.com capability tool) for the SEARCH action; `search` for
  saved-item search.
* Telegram execution layer (`backend/telegram_api/*`, services), so a chain
  never needs new Telegram code.
* Glass UI machinery + Todo panels (a chain can be *inspected* in the existing
  UI style; a "workflow" surface must reuse `panel_registry`/`inline_engine`,
  not add a second UI system).
* `RuntimeSupervisor` lifecycle, `Dispatcher`, `ProviderManager`,
  `PendingConfirmationStore`, `SessionManager` (as-is; no new authority).
* Tests/conventions: fake `ToolContext`/client fixtures, honesty tests for
  fallback writes, the §31 canonical-setup ordering tests.

---

## 20. Components that must NOT be duplicated

| Must not build | Because |
|---|---|
| A second scheduler / timer service / cron loop | `TaskScheduler` is the single scheduler; `RuntimeSupervisor` owns lifecycle. |
| A second tool executor or a direct `tool.execute()` caller | `ToolExecutor` is the sole execution authority. |
| A second task repository/service/store | `TaskRepository` + its services are the single store, with owner scoping, CAS and the memory fallback. |
| A second occurrence/execution-history table | `ai_task_occurrences` is the history; add per-action *runs*, not a parallel history. |
| A generic workflow engine, DAG engine, BPM/automation builder, state-machine DSL, expression language | Not required by the requirement; the model is an ordered, bounded action list on an existing task row. |
| An event bus / message queue / microservice split | Single asyncio process by design; task events already exist where needed. |
| A plugin/marketplace system or a dynamic capability registry | Tools are registered statically in `create_default_registry`. |
| A second session/confirmation/wizard store | The in-memory conventions (`SessionManager`, `PendingConfirmationStore`, handler drafts) already cover their scopes; only *workflow* state may need durability. |
| A second UI system for workflows | Reuse `inline_engine`/`panel_registry`/`render`. |
| Extending `todo_steps` into an execution log | It is documented (migration, §15 of the DB doc, tests) as a hand-managed checklist; execution belongs to occurrences. |
| Giving the model raw RPC/SQL/fs/HTTP/DB access | §13 boundaries. |
| Premium task-manager features (priorities, projects, Kanban, calendar, analytics, deadlines, recurrence UI) | Explicitly out of scope for this product phase. |

---

## 21. Explicitly deferred work

1. Durable **result channels / references between actions** and per-action
   state — the subject of the recommended next phase (§22), not implemented now.
2. **Wait/continuation boundaries** inside one workflow ("… then at 18:00").
3. **Conditional / question+command mixing** (branching on a result) — §14.
4. **Conversational continuation of a pending workflow** across owner messages
   (and across restarts) — §15.
5. Per-action **retry/resume** semantics beyond the skip rule of §18(3).
6. Step **reordering**, priorities, categories, projects, calendar, reminders,
   recurring todos, deadlines — deliberately never part of Part 1/2.
7. Any DAG/graph dependency beyond "action N may read action < N".
8. Re-homing the media/STT findings that the previous `INVESTIGATION.md`
   carried (their citations in code/tests are dangling after this rewrite; see
   the §2 note and §23).
9. Removing or rewriting the existing checklist Todo — **it stays**.

---

## 22. Recommended next implementation phase

### Phase 3A — "Durable action chains with bounded result passing" (single boundary)

**Goal.** Make an existing durable task's ordered actions behave as a *chain*:
each action runs in order through the single `ToolExecutor`, an action may read
a **declared** output field of an **earlier** action, and the chain's per-action
state is durable so a restart never repeats a succeeded action.

**In scope (all reusing §19 components):**

1. the bounded action-result + reference contract (§18.1);
2. the per-action loop + per-action record, Option A of §17 (**no migration**),
   with the record documented and validated;
3. creation-time reference validation (earlier position, declared field);
4. the skip-on-retry rule, keeping the existing `running ⇒ failed
   (restart_side_effect_uncertain)` contract intact;
5. the small explicit tool-output declarations for the tools the workflow needs
   (starting with making `save`/`save_by_link` return `save_code` in `data`,
   which fixes D-4 as a by-product);
6. tests: ordering, reference resolution, fail-closed on an unresolvable
   reference, skip-on-retry, restart cases (unstarted / partially run /
   running), durable-vs-fallback honesty.

**Out of scope for Phase 3A (do not build):** waiting between actions,
branching/conditions, conversational continuation, any new table, any new
scheduler/executor/repository, any change to `todo_steps`, any change to
`DATABASE_ARCHITECTURE.md`'s frozen snapshot.

**Acceptance shape for the example request** (proves the model is sufficient
without over-building): one task whose actions are
`[<search tool>, save, update_save_tags(…, "tag")]` where the tag action's
`save_code` argument is a reference to the save action's declared output — the
owner sees the honest per-action report, and a restart in the middle never
repeats a succeeded action.

### Phase 3B — scheduling split ("then at 18:00") and later phases

Continuation boundary (§12.2), then §14/§15. Each phase must be approved on the
evidence of the previous one; neither is designed further here on purpose.

---

## 23. Open questions / unresolved decisions

1. **Reference syntax** — a string placeholder vs a structured `$ref` object?
   (Recommendation: structured, validated at creation, resolved only by the
   coordinator.)
2. **Per-action state storage** — Option A (occurrence `result_metadata`, no
   migration) vs Option B (one additive table). The owner's tolerance for an
   8 192-byte budget and non-CAS per-action rows decides this.
3. **Retry semantics for a partially executed chain** — skip succeeded actions
   always, or only when a tool declares itself idempotent? Which tools are
   idempotent (`update_save_tags`, `save`)? Who decides?
4. **Wait semantics** — one task with a continuation boundary, or two tasks
   that the owner must link? (Recommendation: continuation boundary, Phase 3B.)
5. **Where the owner's "now" and "later" split comes from** — must the model
   derive it from the Persian request, or must the owner confirm the split
   before creation? (Recommendation: confirm; the existing candidate/wizard
   machinery already knows how.)
6. **Ambiguity at execution time** — if a chained SEARCH returns several
   results (or a saved-item reference is ambiguous), should the chain ask the
   owner (needs §15) or fail the action? (Recommendation: fail the action
   honestly with the candidate list, until §15 exists.)
7. **Output-field catalogue** — the exact list of consumable fields per tool,
   and whether a tool may declare none (then it is a valid chain *end*).
8. **Result content** — may an action's result envelope carry user-visible text
   (bounded), or only identifiers? (Recommendation: identifiers + bounded text
   only where the tool already produces it.)
9. **Delivery boundary** — is `notification_destination.deliver_result` the
   workflow's only reporting channel, or does each action report to the owner?
10. **`todo_steps` long-term role** — keep as a checklist forever, or (much
    later, and only if the owner wants it) allow a todo to *reference* a
    workflow? The audit's recommendation is: keep them separate.
11. **UI** — how a chain is inspected in Glass UI (reuse the Todo/Taskloom
    panel style; no new UI system). Which surface shows per-action state?
12. **The dangling STT citations** (§2 note): where should the media/STT
    investigation content live now that this file is the Todo/Workflow audit?
13. **Manual Supabase step** — the two Todo parts (§31.3 parts 7–8) and any
    future part 9 remain owner-applied; nothing was executed here.

---

## 24. Exact files inspected

Read in full (or in the cited windows) at HEAD `3f8f197`:

**Documents**
* `AGENTS.md`
* `IMPLEMENTATION_REPORT.md` (head: the Part 2 multi-step section + Todo claims)
* `DATABASE_ARCHITECTURE.md` (§31.1–§31.3 audit/order/block, §15 `ai_tasks` +
  todo row shape + `todo_steps`, §16 `ai_task_occurrences`, §30.5/§30.11, and
  §15's indexes/RLS/actions paragraph)
* `README.md` (documentation index reference to `INVESTIGATION.md`)
* the previous `INVESTIGATION.md` (identified, then replaced — §2 note)

**Migrations**
* `supabase/migrations/20260926000001_add_todo_schedule_type.sql`
* `supabase/migrations/20260927000001_add_todo_steps.sql`
* the canonical reconciliation references to `20260829000001_create_ai_tasks.sql`,
  `20260904000001_add_event_schedule_type.sql`,
  `20260912000001_add_ai_task_occurrences_preparation_metadata.sql`,
  `20260921000001_add_saved_items_display_name.sql`,
  `20260922000001_add_saved_items_search_indexes.sql` (as embedded in §31.3)

**Durable task foundation / services**
* `backend/ai/database/task_repository.py`
* `backend/ai/task_creation.py`
* `backend/ai/task_management.py`
* `backend/ai/task_management_interface.py`
* `backend/ai/task_contract.py`
* `backend/ai/task_scheduler.py`
* `backend/ai/task_execution.py`
* `backend/ai/retry.py`
* `backend/ai/preparation_policy.py` (via the coordinator/contract call sites:
  `derive_policy`, `validate_prepared_arguments`, `CONTENT_FIELDS`,
  `strip_attribution_prefix`)
* `backend/ai/conversation/session.py`, `backend/ai/confirmation.py`

**AI tools / action layer**
* `backend/ai/tools/registry.py`
* `backend/ai/tools/executor.py`
* `backend/ai/tools/base.py`
* `backend/ai/tools/todo_tools.py`
* `backend/ai/tools/todo_step_tools.py`
* `backend/ai/tools/task_management_tools.py`
* `backend/ai/tools/task.py` (first 380 lines: `create_task`, wizard signal,
  trusted-origin refusal)
* `backend/ai/tools/retrieve_save.py`, `backend/ai/tools/retrieve.py`,
  `backend/ai/tools/save.py`
* `backend/ai/actions.py` (action name sets, field allow-lists, step-action
  validation, task-routing comments)
* `backend/ai/prompt/template.py` (task/todo/step vocabulary and JSON schema)
* `backend/ai/engine/dispatcher.py` (round loop, tool-result feedback,
  confirmation gating, `MAX_TOOL_ROUNDS`)
* `backend/ai/prompt/builder.py` (TOOL_RESULTS rendering; located by search)

**Telegram / UI / runtime**
* `backend/bot/handlers/todo.py`
* `backend/bot/handlers/misc.py` (Menu row), `backend/bot/router.py` (register)
* `backend/bot/handlers/ai_unified.py` (trigger/reply activation, wizard bridge,
  `show_question`, session/reply handling; located by symbol search)
* `backend/bot/handlers/taskloom.py` (wizard draft state),
  `backend/ai/task_wizard.py` (stateless wizard + draft shape)
* `backend/runtime/supervisor.py` (lifecycle authority; located by symbol
  search)
* `backend/services/save_service.py` (tag/name metadata, caption sections,
  save codes, `execute_save` stages), `backend/services/retrieve_service.py`
  (via the tool call sites: `resolve_saved_items`, `do_retrieve`,
  `do_edit_tags`, `do_preview`, `do_delete`)

**Tests**
* `tests/test_todo_steps.py`, `tests/test_todo_steps_tools.py`,
  `tests/test_todo_tools.py`, `tests/test_todo_lifecycle.py`,
  `tests/test_todo_resolver.py`, `tests/test_todo_ui.py`
* `tests/test_database_setup_order.py`,
  `tests/test_canonical_schema_reconciliation.py`
* `tests/test_capability_exposure_tools.py` (registry-count assertion),
  `tests/test_memory_tools.py`, `tests/test_tool_health_audit.py`
  (Todo-tool expectations, located by search)

**Commands run** (inspection/verification only; results in §2):
`git status`, `git rev-parse HEAD`, `git fetch origin`, `git merge --ff-only
origin/main`, `git log`, `git diff --stat`, `wc -l`, `grep`/`rg`, and the two
`python3 -m pytest` focused runs. No file other than `INVESTIGATION.md` was
written; no SQL, Supabase operation, migration, or Telegram action was
performed.
