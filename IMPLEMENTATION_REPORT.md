# Implementation Report

> **This report was rebuilt from zero.** Every section below describes the
> repository as it stands after this change; no section, table, status list or
> wording was carried over from the previous report. Facts were re-verified
> against the current tree, the current test runs, and `git` before being
> written down.
>
> **Verification honesty rule:** nothing here claims the running bot was
> exercised against live Telegram or a live AI provider, and nothing here
> claims any SQL was executed. See §9 and §13 for exactly what was and was
> not verified.

---

**Stage completed: Stage 6**

**Next stage: Stage 7**

*Derivation (not guessed):* the previous report closed **Stage 5** (removal of
the deterministic semantic router) and named Stage 6 as next. This report is
Stage 6: the coordinated repair of the AI → tools contract, context delivery
and tool-use decision policy, driven by the findings recorded in
`INVESTIGATION.md`.

---

## 1. Root causes addressed

All five were verified against the current source **before** changing
anything; none was stale.

| ID | Root cause | Where it lived | Consequence |
|---|---|---|---|
| P0-1 | The provider-facing `required` list was **inferred** from the absence of a `default` in the parameter descriptor. | `Dispatcher._build_tool_definitions()` | 40 of 55 tools advertised optional parameters (tags, mode, fields, ids, alternatives of an either/or contract) as mandatory; the model fabricated placeholders the prompt forbade it to invent. |
| P0-2 | The whole-prompt ceiling (8,500 est. tokens) was the **history trim trigger**, and the 55-tool block alone exceeded it. | `PromptBuilder.build()` / `_trim_to_budget()` | History was trimmed to zero on every request: multi-turn references, corrections and follow-ups could never reach the model. |
| P1-1 | Gemini's adapter assigned `system_text = content` **per system message**. | `GeminiProvider.chat()` | Only the LAST of the four system sections survived; the merged rules/output contract were silently dropped for every Gemini-routed request. |
| P1-2 | The model was told about a subset of its own capabilities: the bounded recovery nudge enumerated a few actions, the capability sentence omitted web search / memory / translation / speech, and the JSON fallback listed neither `create_task` nor `send`. | `_ENFORCE_ACTION_NUDGE`, `SYSTEM_RULES`, `OUTPUT_INSTRUCTIONS_TEMPLATE` | Capabilities the model was never told about stayed unused; prose recovery nudged only toward the enumerated few. |
| P1-3 | Two continuation-alignment defects: the executor overflow path appended ONE result and `break`-ed (misaligning tool_call ids), and `_build_continuation_messages` zipped calls/results pairwise. | `ToolExecutor.execute_calls()`, `Dispatcher._build_continuation_messages()` | A provider receiving an assistant turn whose tool_call ids are not all answered can reject the continuation outright. |

A sixth, narrower finding: `DeleteTool`'s description never said what it does
NOT do (delete one named message / the replied message / a saved item), even
though four sibling tools cover those exact cases — a concrete overlap
ambiguity (§4).

---

## 2. Workstream A — repair the AI/tool contract and context delivery

### A1. Tool required/optional schemas — fixed at the generation boundary

The fix is at the **declaration boundary**, not 40 per-tool patches:

- `Tool` protocol gains an optional `provider_required_arguments` property
  (defaults to the tool's execution contract `required_arguments`).
- `declared_provider_required_arguments(tool)` reads it; `ToolRegistry.list_schemas()`
  now emits `"required"` (and the existing `"required_any"`) **from the tool
  declaration**.
- `Dispatcher._build_tool_definitions()` consumes that list verbatim
  (`[name for name in (schema.get("required") or []) if name in properties]`);
  `default` is never consulted again.
- `Dispatcher._render_tool_schemas()` marks required params with `*` from the
  SAME declaration, so the prompt block and the native schemas cannot disagree.

Per-tool overrides exist **only** where a flat JSON-Schema `required` list
cannot express the tool's actual contract (an either/or addressing shape).
The tool's own validation is unchanged — nothing was weakened:

| Tool | Execution contract (`required_arguments`) | Provider `required` | Why |
|---|---|---|---|
| `task_transition` | `("action", "action_status")` | `["action"]` | target addressed by id+version **or** title reference |
| `task_delete` | `("task_id", "expected_version")` | `[]` | same either/or addressing |
| `todo_step_transition` | step-status contract | `["action"]` | step/todo addressed by alternative shapes |

**Measured result (live registry, 55 tools):**

| | Before | After |
|---|---|---|
| Tools with a nonempty provider `required` | 40 (inferred) | **27 (declared)** |
| `save` | required `display_name`, `tags` | required `[]` |
| `web_search` | required `query`, `freshness`, `include_domains`, … | required `["query"]` (enum/defaults preserved) |
| `update_save_tags` | required everything without a default | `["tags", "mode"]` + `required_any: ["save_code","query"]` |
| `task_delete` | required `task_id`, `expected_version` | `[]` |
| `send_message` / `create_task` | inferred | `["text"]` / `["request"]` |

Enums, defaults, `minimum`/`maximum`, nested `properties` and `items` are all
preserved (pinned by `tests/test_tool_schema_contract.py`, which asserts the
**provider-facing** schema from the real dispatcher + real 55-tool registry,
never Python annotations). The durable-task boundary
(`task_creation._action_eligibility_error`, which reads
`declared_required_arguments` / `declared_any_arguments`) is untouched — it
still enforces exactly what each `execute()` rejects.

### A2. Prompt/token budgeting — history gets its own bounded budget

- `DEFAULT_MAX_HISTORY_TOKENS = 4000` added to `backend/ai/prompt/budget.py`,
  mirroring the documented `history_budget` default.
- `PromptBuilder.build()` now calls `_trim_history_to_budget()`: oldest-first
  trimming against the **history block's own cap**, never against the
  whole-prompt diagnostic. `[History]` rendering moved into one
  `_render_history_block()` used both for trimming and for delivery, so the
  budget is computed over exactly what is sent.
- Metadata now reports `history_entries` / `history_trimmed` / `history_tokens`
  / `history_budget_tokens`; the whole-prompt estimate is still computed and
  reported, and may legitimately exceed the ceiling because never-evictable
  categories (system instructions, the tool contract, the current request)
  are not trimmable.

**Measured result:** the real 55-tool block is 15,474 chars; before this fix
base prompt + tool block ≈ 9,349 est. tokens > 8,500 → **every** request lost
all history. After: 0/2/5/20 Persian history entries are all rendered,
`history_trimmed == 0` within the cap, and history survives alongside the full
tool catalog. Current request, reply context, Telegram surrounding window,
memory and tool contract are never trimmed.

### A3. Provider system-message mapping — all four sections delivered

- Gemini: system messages are accumulated in order and joined with `\n\n` into
  ONE `systemInstruction` (the API's actual contract), before `contents`;
  no instruction text is duplicated into `contents`.
- Gemini schema conversion is now recursive (`_to_gemini_schema`): `type`
  values are uppercased in nested `properties` and `items` too, so
  ARRAY/OBJECT/nested schemas survive; `required`, `enum`, `default`,
  `minimum`/`maximum`, `description` are preserved verbatim.
- All other chat providers are `OpenAICompatProvider` subclasses that forward
  `messages` and `tools` verbatim; the coverage test pins the full set
  (12 OpenAI-compatible classes + Gemini as the only custom mapper + `you` as
  the web-search capability, never a chat engine). No provider silently drops
  tools, renames them or reorders them.

### Continuation alignment (verified under the same workstream)

- `ToolExecutor` overflow now emits ONE bounded failure **per skipped call**
  (real tool name, index-aligned) and `continue`s instead of `break`ing.
- `_build_continuation_messages()` emits exactly one `role:"tool"` message per
  assistant `tool_calls` entry, index-based, with a synthetic bounded failure
  if a result is ever missing — the protocol stays complete and ordered.

---

## 3. Workstream B — AI tool-use decision policy

`backend/ai/prompt/template.py` now states **behavioral principles**, not
patterns. The capability sentence was replaced by a capability statement
("your capabilities are exactly the tools provided to you") plus a short
decision-principles block:

1. The model interprets the request itself — decide the goal, whether a tool
   is needed, and which one; answer from real tool data, never guesswork.
2. Current/external facts → `web_search`, and answer from its real results —
   never tell the owner to search for themselves; if it fails, say so.
3. Clarify only when the action/target is genuinely missing and no context
   supplies it; never invent an action, target, ID, name, tag, version or
   tool result.
4. A correction or challenge is a correction of the goal: reconsider and redo
   the work with tools if needed — never repeat the same answer unchanged.

Additional changes: RUNTIME_RULES now states that answering a question with a
read/retrieval tool is not an unrequested action (so retrieval is not blocked
by the "explicit request" rule); `OUTPUT` rule 8's JSON fallback list gains
`create_task` and `send`, and states honestly that the fallback covers only
the families it lists (native-tool-only capabilities — web search,
translation/summarization, memory, speech — must be reported honestly when
native tool calling is unavailable instead of inventing an action).

`_ENFORCE_ACTION_NUDGE` (the bounded one-shot recovery nudge) no longer
enumerates a subset of tools: it asks for a native tool call / JSON action for
"anything any available tool can perform", and leaves purely conversational
requests alone. The nudge still only *asks*; every structured result passes the
same local parser + validator before execution.

No keyword lists, no examples that act as routing rules, no intent taxonomy.

---

## 4. Tool-description audit

All 55 registered tools were reviewed against: name clarity,
capability-oriented description, when-to-use, what-it-does-NOT-do, parameter
clarity, overlap, and hidden behaviour.

**Only one description was changed** — `delete.py::DeleteTool`, where the
overlap with `delete_message_by_id` (one message), `delete_by_id` (from an ID
onward), `delete_replied` (the replied-to message) and `delete_save` (saved
items, not Telegram messages) was genuinely ambiguous: the tool deletes a
SCOPE and its old text never said so. The description now names the scope
explicitly and points at the sibling tools for single-target cases. Semantics
were not changed; no duplicate tools were created; no other description was
rewritten for style. The remaining near-neighbours (`retrieve_save` vs
`preview_save`, `list_saves` vs `search`, task vs todo-step lifecycle tools)
already state their "does not do" boundaries and their addressing rules.

---

## 5. Provider tool-calling contract trace (post-change)

Traced from `Dispatcher._build_tool_definitions()` → `ProviderManager` →
each adapter → back through `ToolExecutor`:

| Property | Result |
|---|---|
| All intended tools reach the provider | yes — every registered tool becomes one function definition; names/descriptions preserved verbatim |
| Required / optional fields | from the tool declaration only (A1) |
| Enums, nested objects, arrays, defaults, bounds | preserved (Gemini uppercases `type` recursively; others forward verbatim) |
| Silently removed tools | none (pinned by the provider-contract tests) |
| Native tool calling | used wherever the provider supports it; Gemini translates to `functionDeclarations` |
| Tool-call parsing / ids | ids preserved; `tool_choice` forwarded (OpenAI-compat is `auto` unless the caller sets otherwise) |
| Tool results format | one `role:"tool"` message per call id, in order, for every provider |

No second tool-calling abstraction was introduced: `ProviderManager` /
`ToolExecutor` remain the only paths.

---

## 6. Execution / continuation loop audit

The loop after a tool call is: provider tool call → `ToolRegistry` →
`ToolExecutor` → existing service → structured `ToolExecutionResult` →
`_build_continuation_messages` → provider → next decision. Verified:

- one tool call is possible, results flow back, another call is possible, and
  a final answer is produced (CASE 2, CASE 4, CASE 10);
- multi-step requests are not forced into exactly one call
  (`MAX_TOOL_ROUNDS = 3`, `MAX_TOOLS_PER_TURN = 5`, both pre-existing bounds);
- the loop cannot continue forever (bounded rounds/tools, bounded recovery
  retries);
- authorization boundaries are unchanged: `ToolExecutor` remains the sole
  caller of `tool.execute()`, permission gates and confirmation handling are
  untouched, and long-running tools stay exempt from the generic timeout.

---

## 7. Behavioral validation (the brief's cases)

`tests/test_ai_tool_decision_policy.py` drives the **real** pipeline
(`Dispatcher` → `PromptBuilder` → `ProviderManager` → `ToolRegistry` →
`ToolExecutor` → real services, patched at the service boundary) with a
scripted provider standing in for the model. It asserts what the pipeline
makes possible and what it actually executes — never that a fixed phrase
implies understanding. There is no keyword gate, regex, classifier or
special case for "One Piece", for web search, for Persian text or for any
example, in production or in tests.

| Case | What is asserted |
|---|---|
| 1 — current factual info (`قسمت بعدی وان پیس کی میاد؟`) | `web_search` is delivered with `required == ["query"]`, the current-information policy is in the system instructions, the request reaches the model verbatim, and nothing is executed before the model decides (`tool_call_count == 0`). |
| 2 — explicit search | the model's `web_search` call executes with valid arguments; the REAL tool result flows back as a protocol `tool` message with the preserved `tool_call_id`. |
| 3 — implicit search (no "search"/"web" word) | the same contract is delivered; no local keyword gate exists; nothing executes locally; the model's own answer is what the owner receives. |
| 4 — immediate multi-step (search → save → tag) | all three tools run in order through the real executor against real service boundaries; `tool_rounds == 3`; four model-decision payloads prove each result fed the next decision. |
| 5 — durable request | `create_task` is delivered and the model's chosen call executes through the single executor; the prompt states the immediate-vs-durable contract. |
| 6 — capability mention only | nothing executes; the request reaches the model verbatim; the model's explanation is returned. |
| 7 — explicit save with a replied target | the replied message travels as the save target through the real `save_service` boundary; no name/tags are invented when the model passed none. |
| 8 — genuinely ambiguous | both the structured `clarify` JSON path and a conversational clarification execute nothing and return the model's question. |
| 9 — multi-turn correction | the previous turn's question AND answer are both present in the next turn's prompt (this is exactly the history-budget fix), and the corrected request executes the model's new choice. |
| 10 — tool-result continuation | a second tool call is decided only after the first real result reached the model. |

The test harness mirrors the supervisor's wiring (one `ToolContext` carrying
the `TelegramAPI` facade, handed to both the registry factory and the
executor); a bare-client context would exercise a shape the runtime never
creates.

---

## 8. Exact files changed

### Backend (11 files)

| File | Why |
|---|---|
| `backend/ai/tools/base.py` | Adds the optional `provider_required_arguments` declaration + `declared_provider_required_arguments()` helper (declaration-based requiredness). |
| `backend/ai/tools/registry.py` | `list_schemas()` now emits `required`/`required_any` from the tool declaration for BOTH the prompt and the provider serializer. |
| `backend/ai/engine/dispatcher.py` | `_build_tool_definitions` consumes declared requiredness (no `default` inference); `_render_tool_schemas` marks required with `*`; `_build_continuation_messages` answers every tool_call id 1:1; `_ENFORCE_ACTION_NUDGE` made tool-agnostic. |
| `backend/ai/tools/executor.py` | Overflow path emits one bounded failure per skipped call, index-aligned (no `break`). |
| `backend/ai/tools/task_management_tools.py` | Either/or addressing overrides for `task_transition` (`["action"]`) and `task_delete` (`[]`). |
| `backend/ai/tools/todo_step_tools.py` | Same override for `todo_step_transition` (`["action"]`). |
| `backend/ai/tools/delete.py` | `DeleteTool` description: scope-vs-single-target boundary and sibling-tool pointer (the one concrete ambiguity found by the audit). |
| `backend/ai/prompt/budget.py` | `DEFAULT_MAX_HISTORY_TOKENS = 4000` + documented category relationship. |
| `backend/ai/prompt/builder.py` | History trimmed against its OWN budget; `[History]` rendered once for both trim and delivery; history metadata added. |
| `backend/ai/prompt/template.py` | Capability statement + decision principles; runtime rule for retrieval; JSON-fallback list gains `create_task`/`send` with an honest native-only note. |
| `backend/ai/providers/gemini.py` | All system sections joined in order into one `systemInstruction`; recursive Gemini schema type conversion. |

### Tests (6 files: 4 added, 3 edited — `test_new_tool_action_path.py` included)

- **Added** `tests/test_tool_schema_contract.py` (9 tests) — provider-facing
  schema truth from the real dispatcher + real 55-tool registry: declaration
  only, regression set of previously-wrong tools, `web_search` requires only
  `query`, enum/default/array/nested preservation, either/or alternatives not
  required.
- **Added** `tests/test_prompt_budget_architecture.py` (7 tests) — real tool
  block; dozens of history entries survive; synthetic 3,000-tool noise cannot
  evict history or the request; engine-level multi-turn history reaches the
  provider; the current request stays verbatim; tool-result continuation is
  outside the prompt budget; 8-call overflow yields 8 aligned tool messages.
- **Added** `tests/test_provider_system_instruction_contract.py` (5 tests) —
  Gemini keeps all four system canaries in order, no duplication in
  `contents`, full tool contract preserved (OBJECT/ARRAY/STRING/INTEGER +
  required/enum/default/minimum); an OpenAI-compat provider forwards messages
  and tools verbatim; coverage pins every chat provider class.
- **Added** `tests/test_ai_tool_decision_policy.py` (11 tests) — the
  behavioral cases in §7.
- **Edited** `tests/test_10_tool_calls.py` — requiredness test now proves
  declaration-based behavior (declared tool required; undeclared tool without
  a default NOT required).
- **Edited** `tests/test_context_architecture.py` — the old "tool block must
  push history out" test became "a tool catalog cannot erase history; the
  estimate still counts it".
- **Edited** `tests/test_new_tool_action_path.py` — the nudge test now pins
  the tool-agnostic contract against the real 55-tool registry (no registered
  tool name appears in the nudge).

---

## 9. Tests executed and actual results

| Command | Result |
|---|---|
| `.venv/bin/python -m pytest tests/test_tool_schema_contract.py tests/test_prompt_budget_architecture.py tests/test_provider_system_instruction_contract.py tests/test_ai_tool_decision_policy.py tests/test_new_tool_action_path.py -q` | **76 passed** |
| `.venv/bin/python -m pytest <the 14 affected AI/tool/provider/continuation suites> -q` | **310 passed** |
| `.venv/bin/python -m pytest tests/ -q` (full suite) | **5158 passed, 26 skipped** (118.17 s) |
| Stage 5 baseline for comparison | 5126 passed, 26 skipped |
| `py_compile` of every changed backend module + every touched test file | OK |
| `git diff --check` | clean (no whitespace errors) |

Note on scope: bare `pytest -q` now also tries to collect the pre-existing
untracked `telegram-self-bot/` directory (an unrelated import-path collision),
so the full suite is run as `pytest tests/ -q`. That directory is untouched
and never staged.

---

## 10. Limitations

1. **No live provider/model run.** The behavioral tests prove the pipeline
   delivers the correct contract and executes whatever the model emits; they
   do not prove a specific live model will select `web_search` for a
   current-information question. That is model behaviour, and it was not
   exercised against a real provider key here.
2. **No live Telegram run.** Services are patched at their boundaries in the
   behavioral tests; no real message was saved, retrieved or deleted.
3. **The JSON fallback path is narrower than native tool calling.** Providers
   without native tool calling can only express the action families listed in
   OUTPUT rule 8; the prompt now says so honestly rather than inventing.
4. **One tool round per request costs.** The bounded recovery nudge can add
   one provider round when the model answers a tool-capable request in prose
   (pre-existing behaviour, unchanged in count; only its wording is now
   capability-neutral).
5. **Whole-prompt ceiling remains a diagnostic.** With all 55 tools the
   estimate can exceed 8,500 tokens; the fix bounds history instead of
   pretending the ceiling is enforceable without dropping contracts. A
   real multi-model token counting layer is a separate change.

---

## 11. Intentionally not changed

- No tool was removed, renamed, duplicated, or given new semantics.
- `ToolRegistry` / `ToolExecutor` / `ProviderManager` / `RuntimeSupervisor`
  authorities are untouched.
- `task_creation._action_eligibility_error`, `actions.py` validation and the
  durable-task boundary keep their exact enforcement; only the **provider
  schema** got more accurate declarations.
- Media context isolation and Saved-Items context isolation are untouched.
- Stage 3A–3F durable-task machinery, Stage 5 removal of semantic routing,
  confirmation handling, permission levels and `MAX_TOOLS_PER_TURN` /
  `MAX_TOOL_ROUNDS` bounds are preserved (full suite green).
- Only one tool description was edited (§4); the rest of the catalog was
  audited and left alone.

---

## 12. No semantic router was introduced

**Confirmed.** This change adds no regex, keyword list, semantic classifier,
natural-language fast path, deterministic tool selection or hidden
"if the user says X, call Y" rule anywhere in the decision path. The only new
deterministic logic is schema/requiredness bookkeeping, continuation
alignment, bounded history trimming by a numeric token cap, provider schema
translation, and one description string. Every capability decision remains the
model's; deterministic code validates, bounds, authorizes and executes it
through the unchanged `ToolRegistry` → `ToolExecutor` → service path.

---

## 13. Database / schema

**No change.** No migration, no SQL, no Supabase project touched. No schema
change was genuinely required for this stage, so nothing is deferred and no
rollback SQL is needed. `DATABASE_ARCHITECTURE.md` is untouched and remains
accurate.

---

## 14. Final state

The AI now receives an **accurate** tool contract (declared requiredness,
preserved enums/nested schemas, all system sections on every provider), a
**usable** context budget (history survives next to the tool catalog; the
current request and tool results are never evicted), and a **clear decision
policy** (use tools when appropriate, answer current-information questions
from retrieval rather than deflecting to the owner, clarify only when
genuinely necessary, correct the goal when corrected). The execution path is
the one that already existed: `ToolRegistry` → `ToolExecutor` → existing
service → result → model continuation → final answer.
