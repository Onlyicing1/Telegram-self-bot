# IMPLEMENTATION_REPORT.md — tool-calling root causes RC-1 … RC-4

> **Current status report.** This revision replaces the previous report in full.
> The earlier delivered phase (AED log silence + end-to-end AI tool lifecycle
> observability) is unchanged in the repository and is summarised in §1 only as
> context. The investigation that named these four defects is
> `TOOL_CALLING_ROOT_CAUSE_INVESTIGATION.md`; it is kept as the historical
> record and is not rewritten here.

| Item | Value |
|---|---|
| Base commit | `760d2a7` (deployed baseline = current `main`); work continues **on top of** it, never a reset/downgrade |
| Task | implement the four proven defects RC-1 … RC-4 |
| Date | 2026-11-06 |
| New dependency | **none** |
| Architecture | unchanged: `RuntimeSupervisor → Dispatcher → ProviderManager / ToolRegistry → ToolExecutor → service` |
| Tool execution authority | unchanged: `ToolExecutor` is the only executor; no new executor, scheduler, loop or continuation engine; no re-execution path added |
| Provider selection / routing / fallback / cooldown | untouched (`ProviderManager`) |
| Telegram RPC / SQL / Supabase schema / `DATABASE_ARCHITECTURE.md` | untouched |
| Live provider calls | **none** — this workspace has no Gemini/OpenAI credentials (§7) |

---

## 1. Earlier delivered phase (unchanged by this task)

Delivered at `95f3336`, `9a6c30f`, `8807560`, `760d2a7`: every Anti-Edit /
Anti-Delete log line was deleted (not downgraded) from
`backend/bot/handlers/anti_edit_delete.py` and
`backend/services/anti_edit_delete_service.py`, and the complete AI tool
lifecycle became observable through `backend/ai/tools/lifecycle.py`
(`AI_TOOL_REQUEST_RECEIVED` … `AI_TOOL_CONTINUATION_COMPLETED`, one line per
transition, bounded fields, no argument values or result content). That work is
not modified here; its tests still pass (§6).

---

## 2. The four defects, before and after

| # | Defect | Measured before | After |
|---|---|---|---|
| RC-1 | Gemini continuation serialised every `functionResponse` as its own `model` turn | `user(text) → model(functionCall, functionCall) → model(functionResponse) → model(functionResponse)` | `user(text) → model(functionCall, functionCall) → user(functionResponse, functionResponse)`, ids echoed in call order |
| RC-2 | `Dispatcher._render_tool_schemas` read `parameters["properties"]`, but `ToolRegistry.list_schemas()` returns a **flat** parameter map | 55 tools, 55 flat maps, **0/55** prompt entries exposing any parameter | every declared parameter rendered with type, declared requiredness and bounded description |
| RC-3 | The verbatim-read short-circuit only deferred for the hard-coded `task_list`/`task_inspect` subset | `get_bio`, `preview_save`, `translate_history`, `summarize_history` requests stopped after the read | every verbatim read defers its text for **one** continuation round, so the requested follow-up action is still requested and executed |
| RC-4 | Execution was conflated with result recording: a tool that ran but whose `add_tool_result()` raised was reported as never executed | `tool_results: 0`, `finish_state=tool_rounds_exhausted`, owner told “1 pending tool call(s) were not executed” | execution is recorded before any history/metadata write; a recording failure is its own state (`tool_result_recording_failed`) with an accurate sentence |

---

## 3. Implementation

### RC-1 — `backend/ai/providers/gemini.py` (`GeminiProvider.chat`, message mapping)

Function responses are no longer emitted per message. Consecutive `role:"tool"`
messages accumulate into `pending_responses` and are flushed by
`_flush_tool_responses()` as **one** `{"role": "user", "parts": [...]}` Content
immediately before the next non-response turn (and once after the loop, so a
trailing batch is never dropped). Order is the incoming order, which is the
provider's call order.

* Each `functionResponse` carries `id` when the tool message has a non-empty
  `tool_call_id`; the id is omitted when the provider never supplied one.
* The reconstructed `functionCall` now also carries the assistant tool call's
  `id` (Gemini's `FunctionCall.id`), so call ↔ response matching is explicit.
* The existing `response` envelope (`{"name", "content"}`) and JSON decoding of
  the tool content are unchanged, as are system-instruction joining, schema
  conversion and request/response plumbing.
* Provider-independent code (`Dispatcher`, `ToolExecutor`) is untouched by this
  item; the `role:"tool"` message format it emits is shared with the
  OpenAI-compatible adapters and still correct for them.

Measured payload after the change (two calls, real adapter, mocked transport):

```
user   ['text']                                    ids=[None]
model  ['functionCall', 'functionCall']            ids=['c1', 'c2']
user   ['functionResponse', 'functionResponse']    ids=['c1', 'c2']
```

### RC-2 — `backend/ai/engine/dispatcher.py` (`_render_tool_schemas`, `_render_param`)

`_render_tool_schemas` now reads the same two shapes `_build_tool_definitions`
already handled: if `parameters` carries `"properties"` it is used (with that
schema's `required`); otherwise `parameters` itself is the property map and
requiredness comes from the tool's declared `required`. No registry contract
change, no hard-coded tool or parameter names, permission badges unchanged.

`_render_param` renders `name*(type: description)`: `*` only for a **declared**
required parameter, the descriptor's type, and its description
whitespace-normalised and bounded to 60 characters.

Measured against the real default registry:

```
tools: 55 | with parameters: 44            (11 tools genuinely declare none)
rendered tool lines: 55 | empty signatures: 11 (exactly the parameterless tools)
block size: 22,444 chars
create_task:  - create_task(request*(string: The full natural-language task request, e.g. 'every 30 minu…))
preview_save: - preview_save(save_code*(string: The save code of the item to preview (from search/list_save…))
```

The 0/55 failure is gone. The prompt block is larger than before by design: it
now carries the argument contract that was previously absent. The repository's
prompt-budget tests still pass (§6).

### RC-3 — `backend/ai/engine/dispatcher.py` (tool loop, verbatim-read short-circuit)

`_CAS_READ_TOOLS` and `_read_round_may_continue` were removed. A round whose
successful calls are all verbatim read tools now always sets
`verbatim_read_pending` and lets the normal continuation path run — the same
mechanism that previously applied only to the task tools. When the continuation
produces a tool call, the read was a prerequisite and the loop continues; when
it produces none, the tool's own text replaces the response verbatim and the
loop ends (anti-paraphrase guarantee intact). No new state, no extra round
budget (`MAX_TOOL_ROUNDS` unchanged), structured-action short-circuit and the
confirmation gate untouched.

Cost: a read-only request now costs one additional provider round-trip (the same
cost `task_list` already paid). `tests/test_current_bio_determinism.py` pinned
“no continuation at all” and was corrected to the real contract: exactly one
continuation is attempted and its restyled text must never reach the owner.

### RC-4 — `backend/ai/engine/dispatcher.py` (round bookkeeping, salvage, exhaustion) and `backend/bot/handlers/ai_unified.py`

* `tool_rounds_executed` and `last_round_executed` are set **immediately after**
  `ToolExecutor.execute_calls()` returns, before any `add_tool_result()` or
  metadata write. The exhaustion handler therefore always knows whether the
  final round's calls ran.
* Each history write is wrapped: a failure sets `tool_result_recording_failed`,
  appends a `tool_history_round_<n>` warning, and the round continues.
  `metadata["tool_results"]` still holds the real results.
* The salvage path (pending batch executed once at the round limit) got the same
  treatment plus a `salvage_executed` flag: if a post-execution step fails, the
  request reports *executed, not recorded* instead of *not executed*; if the
  executor never ran, the genuine “pending tool call(s) were not executed”
  outcome is produced exactly as before. No call is ever executed twice.
* `backend/bot/handlers/ai_unified.py` gained `_tool_execution_note(metadata)`,
  the single place the owner-facing note is built. It emits the round-limit
  sentence only for `tool_rounds_exhausted`, and a separate, true sentence for
  `tool_result_recording_failed` (“The action ran, but its result could not be
  saved to history.”).

Three distinguishable outcomes now exist:

| State | Metadata | Owner text |
|---|---|---|
| Never executed (not dispatched) | `tool_rounds_exhausted`, `pending_tool_calls` | “… N pending tool call(s) were not executed.” |
| Executed, result not recorded | `tool_result_recording_failed` (+ `pending_calls_executed_at_limit` when relevant) | “The action ran, but its result could not be saved to history.” |
| Executed and recorded | neither flag | the tool result itself, no note |

Genuine round-limit behaviour, the multi-batch salvage and the “never
re-execute, never discard” policy are unchanged.

---

## 4. Files changed

| File | Change |
|---|---|
| `backend/ai/providers/gemini.py` | RC-1: one `user` Content per function-response batch, call order preserved, ids echoed on calls and responses |
| `backend/ai/engine/dispatcher.py` | RC-2: `_render_tool_schemas` reads the flat schema shape (new `_render_param`); RC-3: `_CAS_READ_TOOLS`/`_read_round_may_continue` removed, deferral for every verbatim read; RC-4: execution recorded before bookkeeping, recording failures isolated, salvage/exhaustion classification corrected |
| `backend/bot/handlers/ai_unified.py` | RC-4: `_tool_execution_note` builds the truthful owner-facing note |
| `tests/test_tool_calling_root_cause_fixes.py` | **new** — 15 focused regression tests (§5) |
| `tests/test_provider_system_instruction_contract.py` | corrected the Gemini expectation that pinned `["user","model","model","user"]` |
| `tests/test_10_tool_calls.py` | single-call Gemini test now asserts the exact role sequence |
| `tests/test_11_runtime_wiring.py` | dispatcher→Gemini continuation now asserts `["user","model","user"]` and a `functionResponse` in the user turn |
| `tests/test_task_reliability_repair.py` | the `_read_round_may_continue` classification test (which pinned `get_bio → False`) was replaced by the deferral-scope contract |
| `tests/test_current_bio_determinism.py` | “verbatim without continuation” became “verbatim, one continuation, restyle rejected” |
| `TOOL_CALLING_ROOT_CAUSE_INVESTIGATION.md` | investigation record (unchanged content), now tracked |

No other file was touched: no routing, no provider policy, no executor, no
schema, no `DATABASE_ARCHITECTURE.md`, no unrelated cleanup.

---

## 5. Regression tests added

`tests/test_tool_calling_root_cause_fixes.py` (15 tests, no network):

* **RC-1** — single call → `["user","model","user"]` with ids on call and
  response; two calls → ONE `user` Content with both responses in call order
  and matching ids, no dropped/duplicated response; a response batch is flushed
  before the following user turn; ids omitted when the call had none; a trailing
  batch is still flushed.
* **RC-2** — the real default registry renders every declared parameter (and
  no tool with parameters renders an empty signature); `*` appears exactly on
  declared-required parameters; parameterless tools render `name()`;
  types/descriptions are preserved; the full `{"properties": …}` shape still
  renders.
* **RC-3** — `preview_save` → `rename_save`: the rename actually executes
  (two executor batches, three provider calls) and the read text does not
  replace the answer; a `get_bio`-only request returns the tool's text verbatim
  with exactly one continuation and the model's own wording rejected.
* **RC-4** — `add_tool_result` raising after a successful execution: the tool
  runs exactly once, `metadata["tool_results"]` holds the result,
  `tool_result_recording_failed` is set, no `tool_rounds_exhausted`/
  `pending_tool_calls`, `finish_state="text"`, and the owner note never says
  “were not executed”; calls that genuinely never ran are still reported as not
  executed (and never retried); the 4th batch executed at the round limit with a
  failing history write keeps `pending_calls_executed_at_limit` and the honest
  note.

---

## 6. Verification actually run

All commands run from the repository root with `.venv/bin/python` (CPython 3.12,
dependencies installed from `backend/requirements.txt`).

| Check | Command | Result |
|---|---|---|
| Focused new suite | `.venv/bin/python -m pytest tests/test_tool_calling_root_cause_fixes.py -q` | **15 passed** |
| RC-3-corrected file + new suite | `.venv/bin/python -m pytest tests/test_current_bio_determinism.py tests/test_tool_calling_root_cause_fixes.py -q` | **22 passed** |
| Broader affected suites | `.venv/bin/python -m pytest tests/test_provider_system_instruction_contract.py tests/test_10_tool_calls.py tests/test_11_runtime_wiring.py tests/test_task_reliability_repair.py tests/test_saved_item_preview_metadata.py tests/test_task_list_consistency.py tests/test_history_ai_tools.py tests/test_tool_health_audit.py tests/test_context_architecture.py tests/test_24_execution_reliability.py tests/test_ai_tool_lifecycle_observability.py tests/test_provider_tool_boundary.py -q` | **293 passed** |
| Full suite | `.venv/bin/python -m pytest -q` | **7042 passed, 1 failed, 27 skipped** (137.61 s) |
| Compilation | `.venv/bin/python -m py_compile` on `backend/ai/providers/gemini.py`, `backend/ai/engine/dispatcher.py`, `backend/bot/handlers/ai_unified.py`, `tests/test_tool_calling_root_cause_fixes.py` | exit `0` |
| Whitespace / conflict markers | `git diff --check` | clean (exit `0`) |
| Full diff reviewed | `git diff` (runtime modules) and the new/changed test files | reviewed; no weakened assertion, no duplicated execution path, no unrelated edit |

The single full-suite failure is
`tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`
(“Task 1 was not scheduled: interval advancement exceeded bound”). It is
**pre-existing and unrelated**: the same test fails at the untouched base commit
`760d2a7`, verified in a detached `git worktree` of `HEAD` without this change.
Nothing in `backend/ai/task_scheduler.py` (or its dependencies) was modified.

---

## 7. Live provider verification

**Not performed.** This workspace has no Gemini or OpenAI credential and the
task forbids creating accounts or exposing credentials. Everything above is
unit-level verification against the adapters' real request-construction code
with a mocked transport plus the documented Google contract — not a live
`generateContent` response. In particular, the RC-1 claim is “the payload now
matches Gemini's documented shape (user turn, grouped, id-echoed)”, not “a live
Gemini request returned 200”.

---

## 8. Limitations / what is not proven

* No live Gemini/OpenAI call, so no server-side acceptance of the corrected
  continuation payload was observed (§7).
* The production trigger of RC-4 (which bookkeeping write raised) is still
  unidentified; the classification defect and its fix are proven regardless of
  which write it was.
* RC-2 changes only what the prompt *shows*; whether a given model then forms
  better arguments is a model-behaviour question. The measured fact is that all
  55 tools' declared parameters are now rendered (was 0/55).
* RC-3 keeps the existing cost model: a read-only request now spends one
  additional provider round-trip. That is intentional (it is what makes
  read-then-act possible) and bounded by `MAX_TOOL_ROUNDS`.
* The prompt tool block grew to ~22.4 k characters; the repository's
  prompt-budget tests pass, but the extra tokens are a real, deliberate cost.

---

## 9. Deferred (deliberately unchanged)

* **RC-5** (from the investigation): a call batch executed by the round-limit
  salvage has no model turn afterwards, so a four-batch chain still ends in the
  raw tool summary instead of a synthesised answer. Not part of RC-1 … RC-4 and
  not changed.
* Model-level tool capability is still not checked (provider-level only in
  `ProviderManager`); a non-tool model still consumes a candidate attempt.
* Hypotheses H1 (nested Gemini schema keywords), H2 (`task_interpreter`
  truncation) and H3 (provider switch mid-loop) remain unverified experiments,
  not fixes.
* The task-execution coordinator still reaches `ToolExecutor` without a
  Dispatcher `request_id` (pre-existing; not touched).

---

## 10. What did **not** change

Tool registration, permission/confirmation semantics, argument validation,
execution order, timeouts, long-running exemptions, tool history persistence,
provider selection/fallback/retry/cooldown, continuation round limit, salvage
policy, structured-action path, verbatim/anti-paraphrase guarantee, Telegram
delivery, Supabase schema and `DATABASE_ARCHITECTURE.md`.
