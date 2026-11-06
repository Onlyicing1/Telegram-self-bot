# Investigation — Actual Root Cause of AI Tool-Calling Failures

**Repository:** `Onlyicing1/Telegram-self-bot`
**Baseline inspected:** `760d2a758b7cb0fbfcb3edabefcaca875cbee728` (= `HEAD` of this worktree, verified with `git rev-parse HEAD`; the tree was already at the deployed commit, nothing newer was read)
**Type:** investigation only. **No production file was modified.** This report is the only new file.
**Method:** source trace of the complete pipeline + two executed experiments (outgoing provider payload capture, and a real `Dispatcher` round-limit/bookkeeping harness). Commands and their outputs are quoted in §H so every claim is reproducible.

---

## A. Executive diagnosis

The failure is **not** tool availability, tool registration, schema construction for the native API, or `ToolExecutor` validation. Tools are registered (55), built into native definitions, and sent on every request (`tools` + `tool_choice=auto` verified in the captured payload). The executor executes and reports correctly; a malformed-argument response is *not* silently turned into `{}`.

The failure is **two independent defects in the layer *after* a tool has been selected**, plus one prompt-contract regression and one misclassification path:

1. **Provider response → continuation serialization (Gemini adapter).** The Gemini adapter rebuilds tool results as `role: "model"` turns, one `Content` per result (`backend/ai/providers/gemini.py:196-205`). Google's contract requires function responses in a **`user`** turn, and — when the model returned several `functionCall` parts — **all** `functionResponse` parts in **one** `Content` in the next turn, positionally matched. The adapter violates both rules on every Gemini tool round-trip. **This is the earliest confirmed failing boundary**: the tool executes successfully and the *continuation* is unserializable, so the model never sees its own results and the request ends without the requested operation being reported. This is exactly the "a tool can execute successfully while the overall AI request still fails" case in the brief.

2. **Dispatcher prompt/tool contract (parameter rendering).** `Dispatcher._render_tool_schemas` reads `parameters["properties"]` (`dispatcher.py:2084`) but `ToolRegistry.list_schemas()` returns **flat** `{param: descriptor}` maps (`registry.py:86-107`). Measured: **55/55** registered tools are flat, and **0/55** prompt lines list a single parameter. The model's textual `[Available Tools]` block therefore advertises `save()`, `create_task()`, `delete_save()`, … with **no argument names at all**, while the legend claims `name* = required`. Only the native JSON schema carries the parameters.

3. **Dispatcher premature termination (verbatim-read short-circuit).** `_read_results_authoritative` (`dispatcher.py:1943`) + `_VERBATIM_READ_TOOLS` (`dispatcher.py:89`) end the request after a successful read-only round so the model cannot paraphrase. The carve-out that fixed the *live* "pause task 11" regression only covers `task_list`/`task_inspect` (`dispatcher.py:98-113`). For the other four read tools (`get_bio`, `preview_save`, `translate_history`, `summarize_history`) a two-step request is still silently truncated to its read step.

4. **Dispatcher misclassification (false "round limit / not executed").** Any exception in the round's post-execution bookkeeping (`dispatcher.py:858` `add_tool_result`, or the confirmation gate / summarizer, all inside the same `try`) sets `round_execution_failed` (`:920`) and routes the round into the exhaustion handler (`:1048-1074`), which reports `tool_rounds_exhausted`, `pending_tool_calls` — and drops the round's executed results from `metadata["tool_results"]`. The handler then tells the owner **"Tool round limit reached — N pending tool call(s) were not executed"** (`ai_unified.py:845`) even though the tools *did* run. Reproduced (§H, S2/S3).

**Earliest confirmed failing boundary:** for Gemini-routed requests, the **provider→dispatcher continuation serialization** (defect 1) — nothing downstream can recover from it. For OpenAI-compatible providers the continuation is byte-correct; there the earliest proven *behavioral* boundary is the **prompt tool-contract regression** (defect 2), which degrades tool/argument selection before any request is sent.

**Multiple independent defects:** yes — four. Defects 1 and 3 are "stops prematurely / never completes"; defects 2 and 4 are "behaves inconsistently" (wrong arguments, misleading outcome). They are not the same bug and fixing one does not fix another.

---

## B. Root-cause table

### RC-1 — Gemini continuation sends function responses in the wrong role and in separate turns

| Field | Content |
|---|---|
| Root cause | Tool results are serialized as `{"role": "model", "parts":[{"functionResponse": …}]}` **once per tool result**. The Gemini contract puts `functionResponse` parts in a **`user`** (client→server) turn, and requires *all* responses for a multi-`functionCall` turn to be in **one** `Content`, in the same order. |
| Location | `backend/ai/providers/gemini.py:196-205` (tool branch) together with `backend/ai/engine/dispatcher.py:2119-2170` (`_build_continuation_messages`, one `role:"tool"` message per call) |
| Trigger | Any request served by `gemini` whose first round returns one or more `functionCall` parts (i.e. every Gemini tool call). Two or more calls in the round additionally triggers the "one Content per response" violation. |
| Production evidence | No Gemini-specific failure line exists in the captures supplied (the tool-lifecycle events are provider-agnostic; `AI_TOOL_CONTINUATION_COMPLETED … status=provider_failure error=request` is the line such a request would emit). Direct log match is **not available**; the code-vs-contract violation is measured. |
| Existing behavior | Measured outgoing payload for a 2-call round: `user(text)`, `model(functionCall, functionCall)`, `model(functionResponse)`, `model(functionResponse)` (§H, experiment 2). |
| Expected behavior | `user(text)`, `model(functionCall, functionCall)`, `user(functionResponse, functionResponse)` — one client turn, order preserved, `id` echoed when the provider supplies one. |
| Impact | Every Gemini tool workflow: the tool result never reaches the model, so the request either fails at the continuation (provider `request`/400 class) or continues without its own data. Task creation, save, delete, search, todo, bio — all of them — on the Gemini route. |
| Test gap | Three tests *assert the broken shape*: `tests/test_provider_system_instruction_contract.py:113` pins `["user","model","model","user"]`; `tests/test_10_tool_calls.py:107` and `tests/test_11_runtime_wiring.py:553` only check that a `functionResponse` part exists. All 23 tests in those two files pass today (§H), so the suite actively defends the defect. |
| Confidence | **Confirmed** (code vs. documented contract + measured payload). The exact API rejection text is **not live-verified** here (no provider credentials in this workspace). |

Supporting contracts (fetched during this investigation):
- Google, *Function calling with the Gemini API*: function responses are sent as `{"role": "user", "parts": [{"functionResponse": …}]}`.
- Same page: *"When the model returns multiple functionCall parts in a single response, you must return all corresponding functionResponse parts together in a single Content object in the next turn. Don't interleave … across separate turns"*, and responses must be in the **same order** as the calls, with `name` matched.
- `Content.role`: *"Must be either 'user' or 'model'"*; the `functionResponse` part is described as client-to-server context for the next model turn.

### RC-2 — Prompt tool block advertises zero parameters for every tool

| Field | Content |
|---|---|
| Root cause | `_render_tool_schemas` looks for a full JSON schema (`parameters["properties"]`) while the registry supplies a **flat** parameter map, so `props` is always `{}` and every tool is rendered as `name()`. `required` (computed from the declaration) therefore marks nothing. |
| Location | `backend/ai/engine/dispatcher.py:2069-2103` (read at `:2084`), fed by `dispatcher.py:453` from `backend/ai/tools/registry.py:86-107` |
| Trigger | Every request (`tools_allowed=True`), i.e. all interactive chat. |
| Production evidence | Indirect but consistent: the captures show tool *selection* working while argument/prose behavior varies, and `[Available Tools]` is the only place the model can learn parameter names when it answers in JSON-action form. No log line prints the block, so this cannot be read off a log — it is measured from the code (§H, experiment 1). |
| Existing behavior | `55` prompt lines, **`0`** contain a parameter; e.g. `- create_task() — Create a durable scheduled task …`. Native schema is correct: `{"type":"object","properties":{"request":{…}},"required":["request"]}`. |
| Expected behavior | `- create_task(request*(string)) — …`, i.e. the same declared parameters/requiredness the native schema carries (the function's own docstring claims exactly this invariant). |
| Impact | Tool-argument formation and tool selection quality, worst for (a) providers/models with weak native function calling, (b) the JSON-action recovery path where the model must *write* an action object with argument names, (c) any two-alternative contract (`task_*` id-vs-query) where names are the only discriminator. |
| Test gap | No test asserts the rendered block's content; `tests/test_provider_system_instruction_contract.py` covers provider-side schema conversion only. The regression is invisible as long as tests stop at `list_schemas()`. |
| Confidence | **Confirmed** (measured: 55/55 flat, 0/55 rendered). |

### RC-3 — Verbatim-read short-circuit truncates multi-step requests

| Field | Content |
|---|---|
| Root cause | A round that executed only successful `_VERBATIM_READ_TOOLS` returns the tool text and `break`s; the model never gets a continuation. The CAS carve-out exists only for `task_list`/`task_inspect`. |
| Location | `backend/ai/engine/dispatcher.py:89-98` (`_VERBATIM_READ_TOOLS`), `:101-113` (`_read_round_may_continue`), `:893-916` (short-circuit + deferred path), `:1943-1968` (`_read_results_authoritative`) |
| Trigger | Model's first round = exactly one or more calls, all in `{get_bio, preview_save, translate_history, summarize_history}`, all successful, and the owner's request had a second step ("preview S0001 **then rename it**", "show my bio **then set it to …**", "summarize the last 50 **and save it**"). |
| Production evidence | The code itself records the identical live regression for `task_list` in its comment at `dispatcher.py:101-113` ("Live regression: 'pause task 11' … the requested mutation was never requested again"). The same mechanism remains for the other four tools. |
| Existing behavior | After the read round, `response.text = _summarize_tool_results(...)`, `break` — no continuation, no second tool round. |
| Expected behavior | Same treatment the CAS read tools already receive: hold the verbatim result, allow exactly one continuation; deliver the verbatim text only if the continuation does not continue the request. |
| Impact | Every read→act request whose read is `get_bio`/`preview_save`/`translate_history`/`summarize_history`: the action silently never happens, and the owner sees a successful read (so it does not look like an error). |
| Test gap | Existing dispatcher tests exercise single-purpose reads and the two CAS tools; no test covers "read tool → second-step mutation" for the other four. |
| Confidence | **Confirmed** by source + the in-code record of the same failure mode. |

### RC-4 — Round bookkeeping failure is reported as "round limit / calls not executed", and the executed results are dropped

| Field | Content |
|---|---|
| Root cause | The per-round `try` that wraps execution, history writes, the confirmation gate and the summarizer sets `round_execution_failed` on **any** exception (`:919-921`). The post-loop handler then takes the `elif round_execution_failed …` branch (`:1068-1074`), which (a) never checks whether the calls actually ran, (b) sets `tool_rounds_exhausted`, (c) has already lost the round's `tool_results` because `metadata["tool_results"]` is only assigned *after* the inner loop (`:861`) — an exception before that line leaves it unset, and (d) `finish_state` becomes `tool_rounds_exhausted` (`:1239`) before the `response.text` branch. |
| Location | `backend/ai/engine/dispatcher.py:760-925` (try/except), `:1048-1074` (salvage decision), `:1239` (finish state), `backend/bot/handlers/ai_unified.py:845` (owner-facing sentence), `:422-425` (`_describe_empty_result`) |
| Trigger | Any exception raised after tools executed but before `last_round_executed = True`: the conversation history write (`add_tool_result`), the pending-confirmation store, or result summarization. Also the "no executor attached" branch. |
| Production evidence | **Exact wording match** with supplied evidence A: `tool_selected` → `tool_execute` → `Tool round limit reached — 1 pending tool call(s) were not executed`. Reproduced deterministically in §H S2/S3 with the real `Dispatcher`: `executions=['call1_0']`, `tool_results` count `0`, `rounds_exhausted=True`, `finish_state=tool_rounds_exhausted`, warning `tool_round_limit_reached: 1 pending tool call(s) after 0 round(s)`. |
| Existing behavior | Reports a limit that was never reached, claims unexecuted calls that ran, hides the real results, and appends the misleading sentence to the owner's answer. |
| Expected behavior | Distinguish "round limit reached with genuinely undispatched calls" from "the round's post-execution step failed". On bookkeeping failure, keep `all_tool_results` in metadata, do not claim a limit, and surface the bookkeeping failure class (`tool_execution_round_N`) as the finish state. |
| Impact | Misdiagnosis of every tool request whose history/confirmation bookkeeping raises: the owner is told nothing ran while the side effect already happened (e.g. a task was created, a message was sent), and the tool result is missing from telemetry/diagnostics. |
| Test gap | No test injects a failure into the post-execution bookkeeping, and no test distinguishes the round-limit path from the exception path; `test_ai_tool_lifecycle_observability.py` only drives the happy paths. |
| Confidence | **Confirmed** (reproduced). Note: the *pure* round-limit path is correct at this commit — §H S1 shows 4 batches / 4 executions with `pending_calls_executed_at_limit=True`. |

### RC-5 (contributing, design-level) — the scavenged round is never shown to the model

| Field | Content |
|---|---|
| Root cause | Calls salvaged at the boundary are executed with no follow-up provider round, so their results reach only the deterministic `_summarize_tool_results` text. |
| Location | `backend/ai/engine/dispatcher.py:1076-1149` |
| Trigger | A 4th distinct tool batch produced by the 3rd continuation (MAX_TOOL_ROUNDS = 3). |
| Production evidence | The old capture's "executed without an additional round" class; current code warns instead of claiming non-execution. |
| Expected behavior | Intended trade-off (bounded loop, no unbounded continuation). Correct as designed, but it means a request needing 4 dependent model turns completes with a raw result dump. |
| Impact | Long chains (read → create → confirm) degrade to a summary; no extra round is possible. |
| Test gap | Covered (`test_ai_tool_lifecycle_observability.py`), not a defect. |
| Confidence | Confirmed as **by design**; ranked low. |

---

## C. Complete pipeline trace (verified)

```
Telegram (outgoing owner message)
└─ bot/handlers/ai_unified.py:register → ai_unified_handler
   ├─ trigger / reply-to-AI gate; AIRequest(allow_tools defaults True)      (:86 request.py)
   └─ _execute_ai → engine.execute → Dispatcher.dispatch                    (dispatcher.py:213)
      ├─ tools_allowed = request.allow_tools (True; only ghost_seen_v2 sets False)
      ├─ tool_definitions = _build_tool_definitions()   ← 55 native fns, correct schema (:2023)
      ├─ tool_block = _render_tool_schemas(list_schemas())  ← RC-2: 0/55 parameters (:453, :2069)
      ├─ _build_messages → [system, system, system, system, user]           (:2104)
      ├─ ProviderManager.chat(messages, tools=tool_definitions)             (manager.py:134)
      │   ├─ _skip_reason: provider-level capability only (supports_tools)  (:755)
      │   ├─ _candidate_models: configured model first, then tested pool    (:914)
      │   └─ provider.chat(...)  ── two adapters ──
      │        ├─ OpenAICompatProvider: messages untouched, tools + tool_choice=auto (openai_compat.py:114-131)
      │        └─ GeminiProvider: system→systemInstruction; tool msgs→role "model"  ← RC-1 (gemini.py:196)
      ├─ _reject_disabled_tool_calls (no-op when tools_allowed)
      ├─ empty-response nudge retry (one bounded retry, no tool executed yet)
      ├─ _apply_structured_action if no native call (JSON action → tool calls)
      └─ for round in 1..3:                                               (:760)
         ├─ execute_calls → ToolExecutor._execute_single                  (executor.py:150/251)
         │    ├─ name/malformed/args-type/registry/confirmation gates (structured failures)
         │    ├─ long_running exemption (save) ; 10s default ; tool.timeout_seconds
         │    └─ ToolResult → ToolExecutionResult (1 per call, incl. index ≥ 5)
         ├─ add_tool_result → conversation history     ← exception here ⇒ RC-4
         ├─ last_round_executed = True                                    (:867)
         ├─ confirmation gate → break (pending owner approval)
         ├─ structured-action short-circuit → break
         ├─ verbatim-read short-circuit → break  (RC-3 for 4 of 6 read tools)
         ├─ redundant search → break
         └─ _build_continuation_messages(assistant tool_calls + 1 tool msg per call)  (:2119)
              → ProviderManager.chat again  ⇒ Gemini: RC-1 breaks here
```

Where data can be lost, and what the evidence says:

| Boundary | Can lose… | Verified status |
|---|---|---|
| Tool availability | all tools | healthy — 55 definitions built and sent |
| Native schema | parameters | healthy for OpenAI-compat & Gemini (schema converted recursively, `required` from declaration) |
| Prompt block | parameter names | **broken (RC-2)** |
| Gemini continuation | the whole tool result | **broken (RC-1)** |
| OpenAI-compat continuation | nothing observed | healthy — one `tool` message per call, ids/names preserved (measured) |
| Round short-circuits | the *next* step | **broken for 4 read tools (RC-3)** |
| Round bookkeeping | results + correct outcome | **broken (RC-4)** |
| Salvage at the limit | the model's chance to react | correct by design, exec-once verified |

---

## D. Provider compatibility matrix (actual adapters)

| Provider (adapter) | Tool request format | Tool response parsing | Argument handling | Continuation format (what the adapter sends) | Confirmed issues |
|---|---|---|---|---|---|
| OpenAI-compatible family (`openai_compat.py`; openai, openrouter, groq, cerebras, mistral, zai, sambanova, nvidia, siliconflow, fireworks, nararouter) | `tools` verbatim, `tool_choice=auto` (overridable), `response_format` when requested | `choices[0].message.tool_calls[]`; id/name preserved; `arguments` JSON-decoded; `malformed_arguments`+`arguments_error` set on bad JSON/type | dict or JSON string; anything else flagged, never executed as `{}` | messages passed through unchanged: assistant `tool_calls[{id,type,function{name,arguments(json)}}]` + one `role:"tool"` message per call with `tool_call_id` + `name` + JSON content | none confirmed |
| `cloudflare.py` (subclass of the above) | identical (Workers AI OpenAI-compatible surface) | inherited | inherited | inherited | none confirmed |
| `gemini.py` | tools → `tools:[{functionDeclarations:[…]}]`; types recursively uppercased; multi system messages joined into one `systemInstruction` | `candidates[0].content.parts`; text parts joined with a space; `functionCall` → `{id,name,args}` | `args` dict passed through | **tool message → `role:"model"` `Content` with one `functionResponse`** | **RC-1** (wrong role, one turn per result, no id echo) |
| `dummy` | never routed in production | – | – | – | none |
| `you_search` | `CAPABILITY_KIND="web_search"`, never a chat candidate (`_skip_reason`) | – | – | – | none |

Capability gating is **provider-level only** (`_supports_tools`, `manager.py:787`). A model-level tool capability is never consulted, so a candidate from the tested pool that cannot do function calling still receives `tools`; it fails as a `request` error and the router advances. Not a root cause, but it is why the same model can look "unreliable" only on tool requests.

---

## E. Ranked root causes

Ranked by (1) production impact, (2) frequency, (3) number of workflows affected, (4) evidence strength.

**Confirmed defects**

1. **RC-1 — Gemini continuation serialization.** Silent end-to-end failure of *every* Gemini tool workflow; the tool runs, the answer never comes. Frequency: every Gemini tool round; strength: code vs. documented contract + measured payload.
2. **RC-2 — Prompt tool block drops all 55 tools' parameters.** Affects every interactive request on every provider; directly degrades which tool is chosen and which arguments are formed; quantified (0/55).
3. **RC-3 — Verbatim-read short-circuit truncates two-step requests.** Deterministic premature stop for 4 of the 6 read tools; the same defect already produced a live regression for `task_list` (recorded in code).
4. **RC-4 — Bookkeeping exception presented as "round limit / not executed", results dropped.** Explains the exact supplied production sentence and hides real side effects; frequency depends on history/confirmation writes.

**Contributing, by design (not defects to fix blind):**

5. RC-5 — salvaged round has no model turn, so a 4-batch chain ends in a raw summary.
6. Model-level tool capability is never checked (provider-level only), so non-tool models consume a candidate attempt.

**Hypotheses (unverified — need the experiment named, not a fix):**

- **H1 — Gemini/Cerebras 400 from a *nested* schema keyword.** Gemini's OpenAPI `Schema` subset does not accept every JSON-Schema keyword a tool may declare (`minLength`, `maxLength`, `pattern`, `default`). Not observed in the 55 tools' top level, but any tool adding one would turn a valid tool request into a provider `request` failure. *Experiment:* send every one of the 55 declarations to Gemini's `:generateContent` with a stub key and diff the 400 bodies.
- **H2 — `task_interpreter` truncation.** The interpreter tolerates fenced / prose-wrapped / double-encoded JSON (`task_interpreter.py:371-415`), and its classification already distinguishes `candidate_invalid_json:truncated`. Unhandled shapes are therefore **not** parser gaps; the remaining `JSONDecodeError` source is a **token-truncated** response (long multi-action task, `finish_reason=MAX_TOKENS`). *Experiment:* replay one failing production request with `max_tokens` raised and compare `raw_response_shape … truncated=` in the same request id.
- **H3 — provider switch mid-loop (OpenAI→Gemini or Gemini→OpenAI) for continuation rounds.** The manager may serve the continuation from a different candidate; the continuation messages are provider-agnostic but Gemini-origin calls carry empty ids. *Experiment:* force a tool round on provider A and a continuation on provider B in a local harness and inspect the second payload (already possible with the harness in §H).

---

## F. Minimal implementation plan (nothing implemented)

For each defect: the smallest correction, its files, the regression test, and what must not change.

### F.1 RC-1 — Gemini continuation (highest value)

- **File:** `backend/ai/providers/gemini.py`, tool-message branch (`:196-205`).
- **Change:** accumulate `functionResponse` parts from **all consecutive `role:"tool"` messages** and emit **one** `{"role": "user", "parts": [...]}` content after the `model` turn; keep the incoming order; include `id` in each `functionResponse` when the corresponding tool message carries a non-empty `tool_call_id`.
- **Do not change:** system-instruction joining, schema conversion, request/response plumbing, retry/fallback ownership.
- **Regression tests (new):** (a) single call → contents `[user(text), model(functionCall), user(functionResponse)]`; (b) two calls → exactly one `user` turn with two `functionResponse` parts in call order; (c) ids echoed when present, omitted when empty.
- **Tests to correct:** `tests/test_provider_system_instruction_contract.py:113` and `tests/test_11_runtime_wiring.py:553` currently pin the broken shape and must be updated with the fix, not left as-is.

### F.2 RC-2 — prompt tool block

- **File:** `backend/ai/engine/dispatcher.py`, `_render_tool_schemas` (`:2069-2103`).
- **Change:** read the same flat map `_build_tool_definitions` already handles: if `"properties"` is present use it, else treat `parameters` itself as the property map (mirror `:2040-2052`). No registry change, no schema change.
- **Do not change:** `list_schemas()` shape, native definitions, permission badges, `required` computation.
- **Regression test:** render the real default registry and assert (i) every tool with declared parameters lists them, (ii) `create_task` renders `request*`, (iii) `*` only appears on declared-required names.

### F.3 RC-3 — read-then-act

- **File:** `backend/ai/engine/dispatcher.py`, `_read_round_may_continue` (`:101-113`) / short-circuit (`:893`).
- **Change:** replace the hard-coded `_CAS_READ_TOOLS` set with the **existing deferred mechanism** already used for CAS reads (`verbatim_read_pending`): every `_VERBATIM_READ_TOOLS` round gets one continuation; if that continuation produces no tool call the verbatim text is delivered unchanged. No new state, no extra round budget.
- **Do not change:** the anti-paraphrase guarantee (verbatim text still wins when the continuation is empty), `MAX_TOOL_ROUNDS`, structured-action short-circuit.
- **Regression test:** `preview_save` → `rename_save` two-round request executes the rename; a `get_bio`-only request still returns the bio verbatim with no second round.

### F.4 RC-4 — outcome classification

- **Files:** `backend/ai/engine/dispatcher.py` (`:861` move `metadata["tool_results"]` assignment inside the result loop; `:1068-1074` gate the exhaustion branch on "calls not dispatched"; `:1239` prefer a text/tool-results outcome over `tool_rounds_exhausted`), and the owner-facing sentence in `backend/bot/handlers/ai_unified.py:845`.
- **Change:** set `last_round_executed` immediately after `execute_calls` returns (before history writes); record executed results even when bookkeeping fails; report a distinct state (e.g. `tool_bookkeeping_failed`) instead of `tool_rounds_exhausted`; only claim "not executed" when they were not dispatched.
- **Do not change:** salvage policy, confirmation round-trip, round accounting, telemetry field names consumed elsewhere (add, don't rename).
- **Regression test:** inject a failure into `add_tool_result` and assert (i) the tool ran exactly once, (ii) `metadata["tool_results"]` holds the result, (iii) no `tool_rounds_exhausted`, (iv) the owner text does not say "not executed".

Order of work: **F.1 → F.2 → F.3 → F.4** (F.1 is the only defect that makes a whole provider path silently unusable; F.2 is a one-function fix with a large selection effect).

---

## G. Explicit non-findings (investigated, ruled out)

| Hypothesis | Verdict | Evidence |
|---|---|---|
| Tools are not built or not sent | **Ruled out** | 55 definitions built (`_build_tool_definitions`) and measured in the outgoing OpenAI-compat payload: `tools: 3` (sliced), `tool_choice: "auto"` |
| `tools_allowed` is false for chat | **Ruled out** | `AIRequest.allow_tools` defaults `True` (`request.py:86`); the only `False` call sites are `ghost_seen_v2.py:442` and tests |
| Tool registry is incomplete / names mismatch | **Ruled out** | `create_default_registry` registers all 55 used by the model; unknown names return a structured `not_found` result without aborting the round (§H S4) |
| Malformed JSON arguments become a valid `{}` invocation | **Ruled out** at this commit | `openai_compat.py:180-205` sets `malformed_arguments`/`arguments_error`; executor rejects before lookup (`executor.py:283-297`) |
| Permission gate blocks normal tools | **Ruled out** | only `ADMIN_ONLY`/`CONFIRMATION_REQUIRED` are blocked (`base.py::requires_owner_confirmation`); all chat tools used in the traces are `READ_*` |
| Tool timeout too short globally | **Ruled out** for `create_task`/`save` | `create_task` declares `timeout_seconds = 45`, `save` is `long_running=True` (§ used by executor `:364-368`) |
| Round-limit salvage drops pending calls | **Ruled out** for the pure limit path | §H S1: 4 batches, 4 executions, `pending_calls_executed_at_limit=True`; only the exception path misfires (RC-4) |
| Salvage re-executes already-run calls | **Ruled out** | the `round_execution_failed` branch precedes salvage, so executed calls are not re-run (S2/S3 show one execution each) |
| Continuation ids/roles broken for OpenAI-compat providers | **Ruled out** | measured payload: assistant `tool_calls` (ids preserved) + one `tool` message per call with matching `tool_call_id` and `name` |
| Structured-action path can overwrite a real tool result | **Ruled out** | structured path only fires when `not response.tool_calls`; after execution it clears `tool_calls` and the deterministic summary is used |
| Empty provider response becomes `success=False` with tool calls | **Ruled out** | adapters return `success=True` with `tool_calls`; `success=False` responses carry no calls (all failure branches) |
| `MAX_TOOL_ROUNDS` too small for a normal create_task flow | **Not supported** | a single tool round + one continuation suffices; the 3-round budget is only reached by chains, which the salvage completes |
| Logging/observability hides the failure | **Ruled out** | required lifecycle events exist at INFO; the supplied capture proves INFO lines reach Render (`TASK_OCCURRENCE_PERSIST_CREATE_SUCCESS`, `AI_TOOL_EXECUTION_*`) |
| Scheduled task execution proves chat execution is healthy | **Not the same path** | the scheduled path reaches `ToolExecutor` directly with `id=-` (no `AIRequest`); it exercises neither `_build_continuation_messages` nor the Gemini continuation branch, so a green scheduled send says nothing about RC-1/RC-2/RC-3 |

Not claimed healthy merely for absence of errors in one excerpt: the Gemini continuation, the prompt block, the read short-circuit and the bookkeeping classification are the four areas whose health the supplied captures cannot establish — three of the four are now proven defective from source.

---

## H. Reproducible evidence (commands actually run)

**Baseline**
```
$ git rev-parse HEAD          → 760d2a758b7cb0fbfcb3edabefcaca875cbee728
```
Dependencies installed from `backend/requirements.txt` into a local `.venv` (no production change).

**Experiment 1 — schema surfaces (RC-2)**
```
registered tools: 55
flat-parameter tools: 55 of 55
native create_task params: {"type":"object","properties":{"request":{...}},"required":["request"]}
prompt-block lines: 55   with any parameter listed: 0
   - save() — Deep-save a message ...
   - create_task() — Create a durable scheduled task ...
```

**Experiment 2 — continuation payloads (RC-1)**
Real `Dispatcher._build_continuation_messages` output fed to each adapter with a mock transport:
```
continuation message roles: [system, user, assistant, tool(create_task), tool(send_message)]

GEMINI contents:
   user  ['text']
   model ['functionCall','functionCall']
   model ['functionResponse']      ← should be one user turn with both responses
   model ['functionResponse']

OPENAI-COMPAT messages:
   system / user / assistant(tool_calls) / tool(tool_call_id,name) / tool(tool_call_id,name)
   tool_choice: auto  tools: 3
```

**Experiment 3 — round-limit + bookkeeping (RC-4, RC-5)**
Real `Dispatcher` + real `ProviderManager` + real `ToolExecutor` + counting tool:
```
S1 (3 rounds + 4th continuation batch):
   executions: ['call1_0','call2_0','call3_0','call4_0']      ← exactly once each
   rounds_exhausted: None | executed_at_limit: True | finish_state: text

S2 (1 call, then conversation history write raises):
   executions: ['call1_0']                                    ← the tool DID run
   tool_results: 0 | rounds_exhausted: True | finish_state: tool_rounds_exhausted
   warnings: ['tool_execution_round_1: history write failed',
              'tool_round_limit_reached: 1 pending tool call(s) after 0 round(s) (MAX_TOOL_ROUNDS=3)']
   text: "ran call1_0" + handler appends "Tool round limit reached — 1 pending tool call(s) were not executed"

S3 (2 calls in one round, history write raises): executions: ['call1_0','call1_1'], rounds_exhausted: True

S4 (unregistered tool): executions: [] , structured not_found result, finish_state: text
```

**Experiment 4 — the suite guards the defect**
```
$ .venv/bin/python -m pytest tests/test_provider_system_instruction_contract.py tests/test_10_tool_calls.py -q
23 passed
```
Both files assert the current Gemini shape (`["user","model","model","user"]`, "some part contains functionResponse"), so they pass while RC-1 is live.

---

## I. What is still not established

- No Gemini/OpenAI request was sent in this workspace (no credentials): RC-1 is proven against the documented contract and the measured payload, **not** against a live 400 body. The experiment in H2/F.1 is the confirmation.
- RC-4's production trigger (which bookkeeping write raised) cannot be identified from the supplied excerpts; the *classification* defect is proven regardless of which write it was.
- The supplied captures cannot tell which provider served the failing interactive requests; if they were Gemini-routed, RC-1 is sufficient on its own to explain "tool selected, tool executed, request incomplete".
