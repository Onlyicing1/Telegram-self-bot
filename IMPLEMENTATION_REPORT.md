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

---

## 11. Automatic provider continuation diagnostics

### 11.1 What the evidence supported before any change

The Model Tester answers "is this model reachable" with one request:
`provider_inst.chat([{"role": "user", "content": "ping"}], model=model_id)`
(`model_tester.py`, its own `ProviderConfig`, `max_tokens=5`). It never sends an
assistant tool-call turn or a tool result, so it cannot observe the shape the
dispatcher sends after a tool runs. The five requests in the Render capture all
started successfully on Gemini and failed **in the continuation**, which is
exactly the blind spot: an initial round and a continuation round differ only in
that assistant/tool-result structure.

Source read for this phase: `model_tester.py`, `manager.py`, `health.py`,
`metrics.py`, `base/contract.py`, `base/exceptions.py`, `gemini.py`,
`openai_compat.py`, `dispatcher.py`, `factory.py`, and the provider/fallback
provider tool-boundary and tool-calling tests.

**Verified serialization asymmetry (source + vendor contract).** The dispatcher's
continuation messages are OpenAI-shaped: assistant `tool_calls`
(`id`/`type: function`/`function.arguments` as a JSON string) followed by one
`role: "tool"` message per call carrying `tool_call_id`, `name` and a JSON result
body (`Dispatcher._build_continuation_messages`). `openai_compat.py` posts that
array unchanged, which is the documented OpenAI shape. `gemini.py` translates it
into `contents`: the assistant turn becomes `role: "model"` with
`functionCall` parts, and **each tool message becomes a separate
`role: "model"` Content with a `functionResponse` part**. Google's contract
requires the `functionResponse` turn to be a *client* turn (`role: "user"`, or
the legacy `"function"`), and requires the responses for parallel calls to be
returned together in ONE Content, in call order. The adapter therefore emits a
malformed continuation for any tool-using round on Gemini. That is a
source-backed, reproducible candidate for "Gemini continuation failure
classified as `request`" (HTTP 400 request validation) while initial Gemini
requests succeed.

**That defect was repaired upstream while this branch was being finished.**
`0d07dc1` ("repair tool-call continuation, prompt schemas and outcome
bookkeeping", already on `origin/main`) rewrites the adapter so responses
accumulate into ONE `role: "user"` Content per call batch, in call order, with
the answered call ids echoed. This branch was merged with that upstream work
rather than rebasing onto it, so the diagnostic below is now the live way to
confirm that repair holds for each `(provider, model)` pair — it is not a
pending fix in this change.

Other observed failures already carry meaningful classifications and are not
continuation-specific: OpenAI/NaraRouter `rate_limited`, Groq `auth`,
SambaNova/Cohere/NaraRouter `model_not_found`. The production evidence does not
establish one single provider defect, and this phase does not claim one.

**Credential/config parity.** Production providers are built by
`ProviderFactory.create_registry()` from `AI_*`/vendor env keys and handed
`ProviderConfig` instances; the Model Tester reads the same env source
(`_get_env`) and builds its own config — same key, different request profile
(`max_tokens=5`, 8 s timeout, no tools). The diagnostic below reuses the LIVE
registered provider's own `ProviderConfig`, so it uses the exact credential,
endpoint and model resolution of the failing request.

**Configured model cannot bypass the pool.** `_candidate_models()` puts the
provider's configured model first, then every discovery-fed candidate
(deduplicated, free-first). A configured default is candidate #1, never a
shortcut around the proven pool.

### 11.2 Exact implementation

New module `backend/ai/continuation_diagnostic.py`:

- `schedule_continuation_diagnostic(manager, response, request_id=…)` reads the
  manager's own `provider_matrix`, takes the FIRST `failed` entry whose
  normalized `category` is `request` or `tool_call`, and schedules at most one
  probe for that exact `(provider, model)`. Nothing qualifies for `auth`,
  `rate_limited`, `quota`, `model_not_found`, `timeout`, `network`, `server`, or
  a response with no failed matrix entry.
- `run_continuation_probe()` builds a **separate** provider instance from
  `replace(live_provider.config, extra=dict(...))` via the same
  `ProviderFactory.create_provider()` path and sends the fixed synthetic
  conversation: one user turn, one assistant turn with TWO OpenAI-format
  `tool_calls`, two `tool` turns with matching `tool_call_id`/`name` and JSON
  bodies, plus the same single synthetic tool declaration production sends with
  a continuation round. Model is passed verbatim as the request's `model`
  kwarg.
- `classify_probe_response()` emits exactly one of `CONTINUATION_ACCEPTED`,
  `CONTINUATION_REJECTED`, `AUTH_ERROR`, `RATE_LIMITED`, `MODEL_NOT_FOUND`,
  `TIMEOUT`, `UNSUPPORTED`, `UNKNOWN`. A transport success with no text and no
  tool call is `CONTINUATION_REJECTED` (`empty_response`), never "accepted".
- Bounds: 30-minute monotonic cooldown per pair (`PROBE_COOLDOWN_SECONDS`),
  single-flight via a closed set, 12 s strict `asyncio.wait_for`, one existing
  `guarded_create_task` per probe (no scheduler/worker/loop), and the caller
  never awaits it.
- No production side effects: the probe never calls `ProviderManager`, so
  health, cooldowns, quarantine, metrics and candidate eligibility are
  untouched; `ToolExecutor`/`ToolRegistry` are never imported or called; no
  history write; no user/Telegram content.

`ProviderManager` change (one line of metadata): the `failed` matrix entry now
carries `"category": <normalized failure type>`. This makes the exact failed
pair and its category available to a consumer without re-deriving either. No
routing, ordering, cooldown, retry, probe-policy or ownership change.

`Dispatcher` change (one call): after a continuation round's response is
classified, `schedule_continuation_diagnostic(self._provider_manager,
cont_response, request_id=rid)` is invoked. It cannot alter the round's result
and cannot delay the reply.

### 11.3 Exact files changed

| File | Change |
|---|---|
| `backend/ai/continuation_diagnostic.py` | NEW — synthetic continuation probe, cooldown/single-flight, classification, `PROVIDER_CONTINUATION_DIAGNOSTIC` |
| `backend/ai/engine/dispatcher.py` | ONE import + ONE scheduling call in the continuation round |
| `backend/ai/providers/manager/manager.py` | Failure-matrix entries carry the normalized `category` (+ reuse it for the existing log/trace) |
| `tests/test_continuation_diagnostic.py` | NEW — 26 focused tests |
| `ROADMAP.md` | Capability, trigger, safety bounds, tests, status, limitations |
| `IMPLEMENTATION_REPORT.md` | This section |

No provider adapter, selection/fallback policy, Model Tester, ToolExecutor,
RuntimeSupervisor, Supabase schema or `DATABASE_ARCHITECTURE.md` change.

### 11.4 Observability

One event per probe, exactly the required fields:

```
PROVIDER_CONTINUATION_DIAGNOSTIC id=<request id> provider=<name> model=<id> \
  result=<CLASSIFICATION> category=<normalized category> latency_s=<float> \
  cooldown_s=1800 skipped=<->
```

A suppressed probe logs the same event at DEBUG with
`result=SKIPPED skipped=cooldown|in_flight`. The line carries no key, header,
tool argument, tool result, owner message or Telegram identifier; the category
is the provider's own normalized taxonomy value, never an error body. The event
name is unique to this probe, so it cannot be confused with a production
provider call (the adapter's own per-request warning may also appear — that is
the real request the probe made).

### 11.5 Focused test results

`tests/test_continuation_diagnostic.py` → **26 passed** in 0.83 s, covering:
qualifying failure schedules exactly one probe; repeated failure inside the
cooldown does not probe again; different `(provider, model)` pairs have
independent state and each probes for its own model; an in-flight probe is not
duplicated; `auth`/`rate_limited`/`quota`/`model_not_found` never probe; a
matrix with no failed entry never probes; accepted and rejected synthetic
continuations classify correctly; the timeout path; an unregistered provider is
`UNSUPPORTED`; two tool responses preserve the adapter contract (ids, order,
JSON-string arguments, tool definitions, verbatim model); no `ToolExecutor` call
is possible; no user/Telegram content is present; a crashing probe cannot raise
into the caller; health and metrics snapshots are byte-identical before/after;
the log carries neither the API key nor an echoed `Bearer` value nor the
synthetic prompt; and the manager's matrix really carries the category the
trigger reads.

Focused provider/continuation group (`test_continuation_diagnostic`,
`test_dynamic_model_fallback`, `test_23_provider_mesh`,
`test_provider_tool_boundary`, `test_tool_calling_root_cause_fixes`,
`test_10_tool_calls`, `test_17_providers`, `test_ai_fallback_logging`,
`test_24_execution_reliability`, `test_13_model_selection`,
`test_41_reset_cooldown`) → **205 passed**, 19.46 s, exit 0.
`tests/test_tool_calling_root_cause_fixes.py` did not exist when this phase
started; it arrived with the upstream merge (`0d07dc1`) and is included in the
run above.

### 11.6 Full suite

`.venv/bin/python -m pytest -q` → **7082 passed, 27 skipped, 1 failed** in
140.89 s. The single failure is
`tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`,
which is pre-existing and unrelated (it fails identically on the untouched
baseline `fa2dfd0`); it was not touched.

### 11.7 Compilation and diff checks

`py_compile` on the new module, `dispatcher.py`, `manager.py` and the new test
module → exit 0. `git diff --check` → exit 0 (no whitespace errors).

### 11.8 Live verification not performed

- No deployed Render run and no real provider request: whether Gemini actually
  rejects this continuation shape, and whether the probe reports
  `CONTINUATION_REJECTED` for it, is NOT verified here.
- The five-request capture does not record which round each provider failure
  came from beyond "continuation", nor the response body of Gemini's `request`
  failure; the serialization defect is source + vendor-contract evidence, not a
  captured rejection.
- The probe's own quota cost is real (one small request per pair per 30 min) and
  is not observable from here.

### 11.9 What the next production request should show

On the next continuation failure, expect one
`PROVIDER_CONTINUATION_DIAGNOSTIC` line per failing pair naming
`provider=gemini model=<id>` with either `result=CONTINUATION_ACCEPTED` (the
`0d07dc1` repair works for that pair, so a production failure there is
request-specific — e.g. tool arguments/result size — not the contract) or
`result=CONTINUATION_REJECTED category=request` (that pair still rejects the
continuation contract, which is the new evidence to act on). A repeated
failure within 30 minutes emits only
`result=SKIPPED skipped=cooldown` at DEBUG. No provider cooldown, health
snapshot or candidate pool entry changes as a result.

### 11.10 Generation budget visibility and truncation (follow-up)

Every `PROVIDER_CONTINUATION_DIAGNOSTIC` line now also carries
`max_tokens=64 temperature=0.0` (from `PROBE_MAX_TOKENS` / `PROBE_TEMPERATURE`,
which are also the values the copied probe config uses), including the DEBUG
`result=SKIPPED` lines, so a capped probe is readable from the log alone.

A successful response whose adapter reports an output-token finish reason
(`length` from the OpenAI-compatible envelope, `MAX_TOKENS` from Gemini) is now
`CONTINUATION_INCONCLUSIVE category=output_truncated` instead of an acceptance
or a rejection: the probe's own budget ended the turn, so it cannot settle the
contract question. Everything else is unchanged — content-filter reasons, empty
text and providers that expose no finish reason classify exactly as before, and
truncation is never inferred from response length or from the adapters'
synthesized "Response truncated due to token limit." sentence.
