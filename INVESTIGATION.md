# Forensic Runtime Investigation — Why an Immediate Multi-Action Request Still Becomes `create_task`

> **Status:** investigation only. No production code, no test, no schema, and no
> provider contract was modified. This document reports what the current source
> proves; it does **not** claim the runtime issue is fixed.
>
> This file **completely replaces** the previous investigation content. It
> describes the repository as inspected now, not a historical narrative.

---

## 1. Investigation title

**Root-cause analysis of the `"Creating task..."` misroute of a Persian immediate
multi-action workflow: `_is_scheduling_intent()` treats an attributive cadence
noun as a schedule, and `parse_command_intent()` therefore hard-selects
`create_task` before any prompt, provider, or authorization stage is reached.**

---

## 2. Repository / branch / HEAD inspected

| Field | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Worktree | `/home/daytona/codebase/.m14` (branch worktree) |
| Branch | `m14-stt` |
| HEAD inspected | `916536cabd58fd0418465ff41e58205beec3269f` |
| `origin/main` at inspection | `916536cabd58fd0418465ff41e58205beec3269f` (identical) |
| Working tree at inspection | clean |
| Recent commits | `916536c` (intent-routing boundary fix) ← `c69df41` ← `4c7a4c7` (bounded proactive multi-action planning) ← `614db2d` ← `6bec694` (regex tool routing removed) ← `c402284` |

The `916536c` commit is the commit that added the intent-routing regression
suite (`tests/test_intent_routing_boundary.py`, 51 tests) and the
"Immediate workflows vs durable tasks" prompt contract. This investigation
explains why that suite is green while production still misroutes.

**Investigation method.** Source reading plus three throwaway, read-only probe
scripts executed against the current checkout and then deleted. The probes
imported the real modules and constructed the real `Dispatcher`,
`ToolRegistry`, `ToolExecutor`, and `CreateTaskTool`. They made no network call,
no Telegram call, and no Supabase write. Their verbatim output is quoted
throughout as evidence.

---

## 3. Problem statement

Automated intent-routing tests assert that an immediate multi-action request
must **not** become a durable `create_task` request. Production disagrees: the
real Telegram runtime displayed `"Creating task..."` for

```
اول یه سرچ بزن و پنج انیمه برتر در حال پخش جدید رو پیدا کن، بعد نتیجه رو سیو کن، و بعد تگ بزن انیمه های هفتگی
```

("First run a search and find the five best currently-airing new anime, then
save the result, then tag the weekly anime.")

That request expresses **no** schedule, cadence, interval, or recurrence. The
investigation must explain how `create_task` is nevertheless selected while the
regression suite passes.

---

## 4. Observed production behavior

The user-visible symptom is the status text `Creating task...`, which in
production is the rendered form of the string
`"🗓 Creating task..."` (`backend/ai/tools/executor.py:76`,
`_STATUS_LABELS["create_task"]`).

This exact reproduction was driven through the real dispatch spine in this
workspace — real `Dispatcher`, real `create_default_registry`, real
`ToolExecutor`, real `CreateTaskTool`, with only `CreateTaskTool.execute`
stubbed at the task-service boundary and a provider that **raises** if it is
ever consulted:

```
A. EXACT PRODUCTION STRING THROUGH THE REAL DISPATCH SPINE
  PRODUCTION string
      provider calls      : 0
      finish_state        : local_fast_path
      ai_action           : {'action': 'create_task', 'kind': 'executable', 'target': 'schedule'}
      CreateTaskTool.execute calls : [{}]
      status labels sent  : ['🗓 Creating task...']
      'Creating task...' shown to owner : True
      stages              : ['conversation_runtime', 'local_fast_path_tool_execution']
```

Three facts follow directly and are not inferential:

1. `provider calls: 0` — **no LLM was consulted.**
2. `finish_state: local_fast_path`, and the stage list contains
   `local_fast_path_tool_execution` but **not** `prompt_builder`,
   `provider_manager`, or `provider`.
3. The status label `🗓 Creating task...` is emitted by the real executor for
   the real registered `create_task` tool.

The same probe run against **the paraphrase the regression suite actually
asserts on** (`"اول سرچ کن، بعد نتیجه رو سیو کن، بعد تگ بزن."`) behaves
completely differently:

```
B. THE PARAPHRASE THE REGRESSION SUITE ACTUALLY ASSERTS ON
  suite paraphrase
      provider calls      : 2
      finish_state        : provider_failure
      ai_action           : None
      CreateTaskTool.execute calls : []
      status labels sent  : []
      'Creating task...' shown to owner : False
      stages              : ['conversation_runtime', 'prompt_builder', 'provider_manager', 'provider', 'conversation_update']
```

(`provider_failure` here is only the probe's exploding provider standing in for
a real model; the meaningful part is that the request reached the provider
stages and never entered the local fast path.)

The **only** difference between the two strings is the trailing token
`هفتگی` ("weekly") in the owner's phrase `انیمه های هفتگی` — the *weekly anime*,
i.e. a descriptive attribute of the content, not a cadence request.

---

## 5. Expected semantic behavior

For the observed message the correct behavior is an **immediate workflow**:
run the web search now, save the result now, update the saved item's tags now —
three consecutive real tool calls in this turn.

It must **not** produce a durable/scheduled task, because the message contains
no recurrence, interval, anchor time, or event trigger. The discriminating
signal is *where the cadence word sits grammatically*: `هفتگی` modifies the noun
`انیمه های` (the anime), whereas in a durable request the same word would
modify the **action** ("`هفتگی` این کار را انجام بده").

A second, independent expectation applies to the capability-mention class:
"I was thinking about using the save feature for my notes" must not execute
`save`; only an explicit imperative ("Save this.") may.

---

## 6. Exact runtime call path

The observed production request traverses these layers, in this order:

```
Telegram outgoing message
  └─ bot/router.py register_all()  →  bot/handlers/ai_unified.py  (canonical AI activation)
       └─ builds AIRequest (ai_unified.py:~720-737)
            └─ engine.execute(request, status_callback=_status_callback)   (ai_unified.py:752-754)
                 └─ Dispatcher.dispatch()                                   (engine/dispatcher.py:205)
                      ├─ conversation runtime                              (stage conversation_runtime)
                      ├─ _try_local_fast_path(request, ...)                 (dispatcher.py:423-428 → :1520)
                      │    └─ parse_command_intent(user_message, ...)       (actions.py:2718)
                      │         └─ _is_scheduling_intent(words)             (actions.py:2745 → :2653)
                      │              └─ any(w in _FA_RECUR_WORDS ...)      (actions.py:2674)  ← ROOT CAUSE
                      │         └─ returns ActionParseResult(
                      │                kind=KIND_EXECUTABLE,
                      │                action="create_task",
                      │                target="schedule",
                      │                tool_calls=[{"name":"create_task", ...}])   (actions.py:2746-2753)
                      │    └─ kind != conversational → does not return None
                      │    └─ kind == "executable" and tool_calls present   (dispatcher.py:1579)
                      │    └─ NOT delete_messages → no ambiguity guard
                      │    └─ _build_tool_context(request)                  (dispatcher.py:1622 → :1270)
                      │         └─ parse_command_intent AGAIN              (dispatcher.py:1301)
                      │         └─ deterministic_task_candidate = None
                      │              (no "هر/every/each" + time-unit + write marker → :1332-1372)
                      │    └─ ToolExecutor.execute_calls(tool_calls, ...)   (dispatcher.py:1633 → tools/executor.py:148)
                      │         └─ status_callback("🗓 Creating task...")  (executor.py:180-185)
                      │              └─ ai_unified._status_callback        (ai_unified.py:740-745)
                      │                   └─ event.edit(format_status(...))   ← the observed text
                      │         └─ CreateTaskTool.execute(...)             (tools/task.py:147)
                      │              └─ completeness gate reuses
                      │                 _is_scheduling_intent(..., require_action_verb=False)
                      │                 → True again                        (tools/task.py:200-203)
                      │              └─ no deterministic candidate → mode="nl_interpretation"
                      │              └─ TaskInterpreter → TaskCreationService → TaskRepository
                      └─ returns _build_fast_path_result(finish_state="local_fast_path")
                           ── prompt build, provider, structured-JSON fallback,
                              proactive planner and the tool loop are NEVER entered
```

The prompt contract added in `916536c` ("Immediate workflows vs durable
tasks", `backend/ai/prompt/template.py:85`) is **never rendered for this
request**, because `_try_local_fast_path` returns before
`_stage("PROMPT_BUILD")` at `dispatcher.py:447`.

---

## 7. Relevant files and responsibilities

| File | Responsibility | Relevance |
|---|---|---|
| `backend/bot/handlers/ai_unified.py` | Canonical Telegram AI activation; builds `AIRequest`; owns `_status_callback` (`:740-745`) which edits the Telegram message with the executor's status label; Taskloom wizard bridge for incomplete `create_task` results (`:138-153`, `:775-795`) | Renders the observed text; never inspects intent |
| `backend/ai/engine/dispatcher.py` | The single dispatch spine. `dispatch()` `:205`; local fast path call `:423-428`; `_try_local_fast_path` `:1520`; `_build_tool_context` `:1270-1330`; `_apply_structured_action` `:1928`; provider+tool loop `:753` (`MAX_TOOL_ROUNDS = 3`, `:59`) | Owns the ordering that makes the boundary authoritative |
| `backend/ai/actions.py` | Deterministic intent boundary. `_FA_RECUR_WORDS` `:2551`; `_EN_RECUR_WORDS` `:2555`; `_FA_ACTION_VERBS` `:2570`; `_is_scheduling_intent` `:2653` (unanchored membership test `:2674`); `parse_command_intent` `:2718`; scheduling→`create_task` branch `:2745-2753`; `validate_action` `:317`; `resolve_tool_calls` `:1456` (`create_task` at `:1486`); `parse_action_text` `:1689` | **Sole origin of the misroute** |
| `backend/ai/proactive.py` | Fail-closed per-request proactive authorization phrase detector (`_AUTHORIZATION_PHRASES` `:44`; `has_proactive_authorization`) | Not involved in this request (returns `False`) |
| `backend/ai/tools/task.py` | `CreateTaskTool` (`name` `:101`); description `:108-115`; deterministic completeness gate reusing `_is_scheduling_intent` `:200-203`; `deterministic_task_candidate` consumption `:333` | Downstream **amplifier**: the same false positive passes the tool's own completeness gate |
| `backend/ai/tools/executor.py` | Sole caller of `tool.execute()`; `_STATUS_LABELS` `:60-103` (`"create_task": "🗓 Creating task..."` `:76`); `execute_calls` `:148`; `MAX_TOOLS_PER_TURN = 5` | Emits the observed label; no create_task-specific veto |
| `backend/ai/tools/registry.py` | `create_default_registry` builds the one registry; `create_task` is always advertised to the provider | Makes the provider a live second entry point |
| `backend/ai/prompt/template.py` | System rules incl. "Immediate workflows vs durable tasks" (`:85`), runtime rules (`:113`, `:116`), output rule 3 (`:127`) | Correct contract, **not consulted** on this path |
| `backend/ai/task_interpreter.py`, `task_creation.py`, `task_contract.py` | Durable task boundary: interpret NL → `TaskCandidate` → persist. `create_task` is forbidden inside a task's `actions` (`task_interpreter.py:171`) | Correct; recursion guard unrelated to this case |
| `backend/ai/media.py`, `backend/ai/session/request.py` | Media classification; request dataclass | Not involved |
| `tests/test_intent_routing_boundary.py` | 51-test intent-routing boundary suite added in `916536c` | **The suite under scrutiny — see §14/§15** |
| `tests/test_task_nl_creation.py` | Positive `create_task` routing tests + full `Dispatcher.dispatch` coverage | Closest existing coverage of the branch that fires |
| `tests/test_proactive_action_chains.py` | Real `TaskInterpreter`→`TaskCreationService`→`InMemoryTaskRepository`; real `Dispatcher`→`ToolExecutor`; `ScriptedProvider` | Proves the executor *can run* a chain — not that routing *chooses* correctly |
| `tests/test_25_fast_path.py` | Real `Dispatcher` + real `ProviderManager` (fake provider) + mocked executor; asserts `provider.calls == 0` on the fast path | Proves the fast path is provider-independent — the very mechanism that caused this |
| `IMPLEMENTATION_REPORT.md` | Prior delivery report (1025 lines) | Read; not modified by this investigation |
| `INVESTIGATION.md` | This document | Replaced completely |

---

## 8. Immediate-vs-durable decision points

There are **two** places in the current implementation where this distinction
is made, and only the first is reachable for the observed request.

**Decision point 1 — deterministic, pre-provider (the one that fired).**
`_is_scheduling_intent()` in `backend/ai/actions.py:2653`, called from
`parse_command_intent()` at `:2745`. Its decisive test is:

```python
# backend/ai/actions.py:2674
if any(w in _FA_RECUR_WORDS or w in _EN_RECUR_WORDS for w in words):
    return True
```

This is a **whole-message, position-independent token membership test**. It
asks "does the cadence vocabulary appear anywhere?" — not "does the cadence word
govern this action?". It is gated only by `_has_action_verb(words)`
(`:2672`), which the immediate verbs in the request itself satisfy.

**Decision point 2 — model-driven, provider round (never reached).**
The prompt contract in `backend/ai/prompt/template.py:85` tells the model to
separate the two concepts and to "NEVER turn an immediate workflow into
`create_task`". This is a *semantic* decision delegated to the provider. It is
rendered only at `dispatcher.py:447` (`PROMPT_BUILD`), i.e. only when the
fast path returned `None`.

**Verdict on authority.** For this request, decision point 1 is final and
decision point 2 never runs. The distinction is therefore **deterministic, not
model-driven**, on the observed path — and it is deterministic in the wrong
place, with a test that cannot tell "weekly anime" from "every week".

---

## 9. `create_task` entry points

Five distinct, real entry points exist. Only the first produced the observed
behavior; all five terminate at the same `ToolExecutor`.

| # | Entry point | Location | Reachable for the observed request? |
|---|---|---|---|
| 1 | **Deterministic fast path** — `parse_command_intent` scheduling branch returns a literal `create_task` tool call, executed before any provider round | `actions.py:2745-2753` → `dispatcher.py:1624` | **YES — this is the observed path** (proven: `provider.calls: 0`) |
| 2 | **Structured JSON fallback** — when the deterministic parser returns `conversational`, the model's own JSON object is parsed and validated | `dispatcher.py:1954-1960` (`_apply_structured_action`) → `parse_action_text` `actions.py:1689` → `validate_action:317` → `resolve_tool_calls:1486` | No — gated behind a `conversational` verdict, which entry point 1 suppressed |
| 3 | **Provider native tool call** — `create_task` is in the registry and advertised in the tool schemas sent to the provider | `tools/registry.py` `create_default_registry` → `dispatcher.py` `_render_tool_schemas` → `executor.py:148` | No — no provider round occurred |
| 4 | **Proactive planner** — `PROACTIVE_AUTHORIZED_RULES` inserted when authorized | `dispatcher.py:503`, `proactive.py:44` | No — `has_proactive_authorization` returns `False` for this message; also unreachable behind the fast path |
| 5 | **Task-scope planning** — `TaskInterpreter` proactive expansion inside a task's actions | `task_interpreter.py:171` | No — `create_task` is explicitly forbidden inside a task's `actions` |

Entry points 2–5 are **not** needed to explain the symptom. Entry point 1 alone
is sufficient and is proven sufficient by direct execution.

---

## 10. Provider / native-tool path

`create_task` is registered in `create_default_registry()` and its schema is
therefore always part of the tool block rendered at `dispatcher.py:455`
(`self._render_tool_schemas(self._tool_registry.list_schemas())`). The tool
description itself is well-scoped (`tools/task.py:108-115`: "a natural-language
request describing an interval, daily/weekly cadence, or one-time time").

The provider tool loop (`dispatcher.py:753`, `MAX_TOOL_ROUNDS = 3`) executes
whatever the model emits through the same `ToolExecutor.execute_calls`, subject
only to `MAX_TOOLS_PER_TURN = 5` and the permission gate. **There is no
code-level check anywhere in the dispatcher or executor that a `create_task`
call is warranted.** A `grep` for `create_task` across
`backend/ai/engine/dispatcher.py` returns only two hits, both of which *build*
task context (`:1289` comment, `:1306` candidate construction) — never a veto.
In the executor, the only hit is the status label at `:76`.

Consequence: on any path where the provider *is* reached, the prompt contract is
the **sole** barrier between an immediate workflow and a durable task, and it is
unenforced in code.

---

## 11. Structured JSON fallback path

`_apply_structured_action` (`dispatcher.py:1928`) runs the deterministic parser
**first** and only falls through to the model's own JSON when the deterministic
result is `conversational`:

```python
# backend/ai/engine/dispatcher.py:1954-1960
result = parse_command_intent(request.user_message, ...)
if result.kind == "conversational":
    result = parse_action_text(text)
```

The fallback is independently capable of producing `create_task`, because
`"create_task"` is present in both `ACTION_NAMES` (`actions.py:38`) and
`EXECUTABLE_ACTION_NAMES` (`actions.py:75`), and `validate_action`
(`actions.py:317`) returns `KIND_EXECUTABLE` for it. Proven by probe:

```
C. STRUCTURED JSON FALLBACK (parse_action_text) - entry point 2
  'create_task' in ACTION_NAMES          : True
  'create_task' in EXECUTABLE_ACTION_NAMES: True
  provider prose/JSON  : {"action": "create_task", "request": "اول یه سرچ بزن ..."}
  parsed kind          : executable
  parsed action        : create_task
  resolved tool_calls  : [{'name': 'create_task', 'arguments': {'request': 'اول یه سرچ بزن ...'}}]
```

So a model that emits a native `create_task` **and** a model that emits
`{"action": "create_task", ...}` are both accepted. On the observed request
this path is dormant purely because entry point 1 already claimed the turn.

---

## 12. Proactive planner path

`has_proactive_authorization()` (`proactive.py:44`) is a fail-closed,
token-phrase detector whose vocabulary is limited to explicit
initiative-authorization phrases ("هر کاری لازمه", "خودت تصمیم بگیر",
"as you see fit", …). Probed for the observed message:

```
B. PROACTIVE AUTHORIZATION
  has_proactive_authorization: False
```

and for every case string tested, including the durable variant, it is `False`.

The planner is doubly inapplicable here:

1. It is evaluated at `dispatcher.py:503`, **after** the fast path already
   returned — it is structurally unreachable on this path.
2. Even when authorized, `PROACTIVE_AUTHORIZED_RULES` is a *bounded
   extra-work* contract, and it explicitly carries
   `"never call create_task repeatedly"` (asserted by
   `test_proactive_rules_bound_extra_work_and_do_not_enable_task_storage`).

The proactive path therefore **cannot** be the cause.

---

## 13. Authorization path

Two authorization mechanisms exist, and neither converted this request:

**Proactive authorization** — `has_proactive_authorization` → `False` (above);
surfaced as `extra["proactive_authorized"]` at `dispatcher.py:1291` and as a
conditional system message at `dispatcher.py:503-515`. It is purely additive
(permits bounded extra work); it never reclassifies an intent as durable.

**Tool permission level** — `CreateTaskTool.permission_level` is
`PermissionLevel.READ_WRITE` (`tools/task.py:129`), and `safe` is `True`
(`:136`). `ToolExecutor.execute_calls` (`executor.py:148`) therefore runs the
call with **no** `needs_confirmation` gate. The comment in the source states the
design premise: "The owner's message IS the authorization in this single-owner
self-bot."

**Deterministic task candidate** — `_build_tool_context` re-runs
`parse_command_intent` at `dispatcher.py:1301` and, when the verdict is
`create_task`/`executable`, tries to synthesize a provider-free interval
candidate (`dispatcher.py:1332-1372`). For the observed message this returns
`None` (no `هر`/`every`/`each` + time-unit + write marker), so
`extra["deterministic_task_candidate"]` is unset and `CreateTaskTool` proceeds
in `nl_interpretation` mode (`tools/task.py:333-340`) — i.e. it calls the
provider to interpret the anime request as a durable task.

**Authorization conclusion:** no permission layer can convert an immediate
workflow into a durable task, and none did. The misclassification happened
strictly upstream, in the intent vocabulary.

---

## 14. Test coverage analysis

The green suite that motivated this investigation is
`tests/test_intent_routing_boundary.py` (51 tests, added in `916536c`). Its own
module docstring states its scope honestly:

> "These tests exercise the DECISION BOUNDARY (what the local parser resolves,
> and what the prompt contract tells the model the tool surface means). They
> never inject a correct tool call and assert the executor runs it."

Audit of the layers each relevant test actually touches:

| Test / suite | Layer under test | Provider | Real `Dispatcher` | Real registry | Real executor | `create_task` reachable | Could pass while production misroutes? |
|---|---|---|---|---|---|---|---|
| `test_intent_routing_boundary.py::test_immediate_multi_action_is_not_routed_to_create_task` (`:152`) | `parse_command_intent` only | n/a | no | no | no | yes (asserts absence) | **YES** |
| `::test_immediate_workflow_tags_make_the_parser_yield_to_the_model` (`:165`) | `parse_command_intent` + `save_metadata_requested` + `has_proactive_authorization` | n/a | no | no | no | yes (asserts absence) | **YES** |
| `::test_immediate_request_is_not_a_scheduling_intent` (`:246`) | `_is_scheduling_intent` **directly** | n/a | no | no | no | yes (asserts `False`) | **YES** |
| `::test_durable_weekly_request_is_left_to_the_provider_not_invented_locally` (`:219`) | `_is_scheduling_intent` + `parse_command_intent` | n/a | no | no | no | yes (asserts `False`) | **YES** |
| `::test_prompt_contract_separates_immediate_workflow_from_durable_task` (`:180`) | string presence in `template.py` | n/a | no | no | no | no | **YES** — asserts a string exists, not that it is rendered |
| `::test_the_whole_tool_surface_still_terminates_at_one_registry` (`:275`) | `create_default_registry` lookup | n/a | no | yes | no | yes (presence only) | partially |
| `test_task_nl_creation.py::test_interval_request_routes_to_create_task` (`:72`) | `parse_command_intent` **positive** branch | n/a | no | no | no | **yes, asserted `True`** | No — genuinely covers the firing branch |
| `test_task_nl_creation.py` full-dispatcher tests | real `Dispatcher.dispatch` | fake provider | **yes** | yes | yes | yes | partially |
| `test_25_fast_path.py` | real `Dispatcher` + real `ProviderManager`; mocked executor | fake | **yes** | no | no (MagicMock) | n/a | No — but it *proves* the fast path runs with `provider.calls == 0` |
| `test_proactive_action_chains.py` (24 tests) | real `TaskInterpreter`→`TaskCreationService`→`InMemoryTaskRepository`; real `Dispatcher`→`ToolExecutor` | `ScriptedProvider` | **yes** | **yes** | **yes** | yes | **YES** — the model's choice is scripted, so routing is never tested |

Directly answering Q10/Q11/Q12:

- **Q10 — Are tests covering the actual production path?** Partially. Several
  suites use the real `Dispatcher`, real registry and real executor, but the
  intent-routing suite that carries the "must not become `create_task`" claim
  exercises only `parse_command_intent` in isolation, with no dispatcher, no
  registry, and no executor.
- **Q11 — Does any test reproduce the natural-language request while mocking
  only the provider boundary?** **No.** The exact production string appears
  nowhere in the repository. The suite's `IMMEDIATE_MULTI_ACTION` list
  (`:144-147`) contains only two paraphrases:
  - `"این رو سیو کن و بعد تگش کن."`
  - `"اول سرچ کن، بعد نتیجه رو سیو کن، بعد تگ بزن."`

  Both omit `هفتگی`. The docstring of
  `test_immediate_workflow_tags_make_the_parser_yield_to_the_model` (`:167-170`)
  asserts *"This is the exact path the anime request took"* while passing the
  second paraphrase — the anime request is named but never reproduced.
- **Q12 — Does any such test prove `create_task` cannot be selected later?**
  **No.** Nothing asserts anything about entry points 2–5, and nothing drives
  the full `dispatch()` with an attributive-cadence input. The suite's own
  framing ("what the local parser resolves") is narrower than the claim the
  suite is being used to support.

---

## 15. False-confidence / coverage gaps

**Gap 1 — the decisive function has no positive test, and its only two direct
assertions are negative.** A repository-wide search shows `_is_scheduling_intent`
is asserted in exactly two places, both in
`tests/test_intent_routing_boundary.py` (`:229` and `:249`), and **both assert
`False`**. The function that caused this incident is never asserted `True` in
that suite, and the two strings it is checked against contain no cadence token
at all. A function whose only assertions are "returns False for these inputs
that obviously have no cadence word" cannot detect "returns True for an
immediate input that happens to contain a cadence word used attributively."

**Gap 2 — the suite's own docstring documents the dormancy assumption as if it
were safety.** `test_durable_weekly_request_is_left_to_the_provider_not_invented_locally`
(`:219-233`) states that `_is_scheduling_intent` "fails closed here" and
therefore cannot invent a schedule. The probe shows this reasoning is inverted:
the function is *over*-permissive, not fail-closed. The test's conclusion (that
the durable request is not resolved locally) is correct for its input, but the
stated mechanism is not the safety property it appears to be — the same
function is what made the immediate request durable.

**Gap 3 — prompt-contract tests assert strings, not behavior.** Three tests
(`:180`, `:193`, `:200`) assert that specific sentences exist in
`template.py`. They prove the contract was *written*. They cannot prove the
contract is *rendered*, and on the observed path it is not rendered at all.

**Gap 4 — "the executor can execute an action chain" is treated as routing
coverage.** `test_proactive_action_chains.py` uses the real
`TaskInterpreter`, `TaskCreationService`, `InMemoryTaskRepository`,
`Dispatcher` and `ToolExecutor`, and its own docstring claims "Nothing is faked
except the provider response itself." That is accurate and still
insufficient: with a `ScriptedProvider`, the model *chose* `create_task` in the
script. The test proves the downstream machinery works **given** a task
selection. It says nothing about **whether** an incoming Persian sentence
selects one. These are not equivalent claims, and only the first is tested.

**Gap 5 — no adversarial-cadence test class exists.** No test anywhere in
`tests/` uses `هفتگی` in an intent-routing assertion. The only `هفتگی`
occurrences in the suite are unrelated
(`test_task_semantic_triggers.py:201` — a duration-mapping fixture;
`test_save_v2_resolution.py:301-324` — save-item display-name fixtures). The
entire class "cadence word used as a content attribute" is untested.

**Gap 6 — no end-to-end assertion on the status label.** Nothing asserts that
the executor's `create_task` status label is reachable, or unreachable, for a
given intent class. The observable that actually appeared in Telegram
(`"🗓 Creating task..."`) has no test in any form.

**Gap 7 — the positive-branch suite only uses genuinely-scheduled strings.**
`test_task_nl_creation.py:72-85` correctly asserts
`parse_command_intent` → `create_task` for
`"برنامه ریزی کن هر ۱ ساعت همه پیام های این چت رو پاک کن"` and its interval
sibling. Those inputs really are schedules. No test pairs a *true positive* and
a *look-alike false positive* to pin down the boundary between them — which is
exactly the distinction that failed.

### The second reported problem: capability/topic mentions

At the deterministic boundary, capability mentions are currently handled
correctly. Probed:

```
CAPABILITY mention (no execution)   kind='conversational'
  "داشتم فکر می‌کردم از قابلیت ذخیره برای یادداشت‌هام استفاده کنم"
CAPABILITY mention EN (no execution) kind='conversational'
  "I was thinking about using the save feature for my notes"
PLAIN immediate save  "این رو سیو کن"        kind='clarify' action='save'
PLAIN immediate save EN "Save this."         kind='clarify' action='save'
```

So the *only* current enforcement of "a capability mention is not an
executable request" is:

1. the deterministic parser abstaining, and
2. the prompt's output rule 3 (`template.py:127`, asserted at
   `test_output_instructions_forbid_storing_work_the_owner_wanted_now`).

**There is no code-level mechanism anywhere that converts a capability mention
into an executable tool call — and equally, there is no code-level mechanism
that forbids it.** Once a request is `conversational`, the model is free to
emit `save` or `create_task` as a native tool call (entry point 3) or as a JSON
action (entry point 2), and the executor will run either with no re-check
(`save` and `create_task` are both `READ_WRITE`, `safe`, and the single-owner
premise treats the owner's message as the authorization). The defense for this
class is prompt-only, exactly like the defense for immediate-vs-durable on the
provider path.

Note the compounding risk: because `_is_scheduling_intent` is also the
completeness gate *inside* `CreateTaskTool` (`tools/task.py:200-203`), a
capability mention that happened to contain a cadence word would pass that gate
as well — the two layers are self-consistent because they share the same
over-permissive function, and neither can catch the other.

---

## 16. Root cause

**Conclusively established from source and from direct execution of the real
components.**

**Root cause.** `_is_scheduling_intent()` decides "this message requests a
schedule" with a position-independent membership test over the whole message's
token list:

```python
# backend/ai/actions.py:2674
if any(w in _FA_RECUR_WORDS or w in _EN_RECUR_WORDS for w in words):
    return True
```

`"هفتگی"` ("weekly") is a member of `_FA_RECUR_WORDS` (`actions.py:2551-2554`).
In the observed message the token sequence is
`… انیمه های هفتگی` — `هفتگی` is an **attributive adjective describing the
anime**, not a recurrence instruction. The test cannot distinguish the two
because it never looks at where the word sits or what it modifies. The
co-occurring action-verb requirement at `:2672` is satisfied by the request's
own immediate verbs (`بزن` in "تگ بزن" and `سیو` are both in `_FA_ACTION_VERBS`,
`actions.py:2570-2575`).

**The exact state transition responsible.** `parse_command_intent` moves from
"examining the message" to a hard, provider-independent commitment at
`actions.py:2745-2753`: it returns
`ActionParseResult(kind=KIND_EXECUTABLE, action="create_task",
target="schedule", tool_calls=[{"name": "create_task", …}])`. Proven by probe:

```
A. DETERMINISTIC INTENT BOUNDARY (parse_command_intent)
  kind      : executable
  action    : create_task
  tool_calls: ['create_task']
```

and the isolating A/B, which removes that single token and nothing else:

```
  PRODUCTION (observed)                        kind='executable' action='create_task' target='schedule'
  PRODUCTION minus 'hafti'                     kind='conversational'
  PRODUCTION, 'hafti' -> 'mahane' (monthly)   kind='executable' action='create_task' target='schedule'
  'قیمت هفتگی رو بگو'  ("tell me the weekly price")   scheduling=True  boundary=executable
  'این هفته انیمه ها رو بگو' ("tell me this week's anime") scheduling=False boundary=conversational
```

The last two lines show the false positive is general, not specific to anime:
*any* sentence containing `هفتگی` plus an action verb is misread as a schedule,
while the genuinely temporal `این هفته` is correctly not.

**The component that emits the observed message.** `ToolExecutor.execute_calls`
(`executor.py:148`) iterates the single `create_task` call, looks up
`_STATUS_LABELS["create_task"] = "🗓 Creating task..."` (`:76`) and awaits
`status_callback(label)` (`:180-185`). In production that callback is
`ai_unified._status_callback` (`ai_unified.py:740-745`), which calls
`event.edit(format_status(display_prompt, status, show_question))` — the exact
text the owner saw.

**Why no later stage could save it (Q15).** The intent boundary is **not
bypassed, overwritten, or ignored — it is the final authority, and it is the
component that made the error.** `_try_local_fast_path` is called at
`dispatcher.py:423-428`, *before* prompt construction, and returns a
non-`None` `EngineResult`, which `dispatch()` returns immediately at `:426-427`.
Concretely, on this request:

- the prompt contract in `template.py:85` is **not rendered** (no
  `PROMPT_BUILD` stage — `_stage("PROMPT_BUILD")` at `dispatcher.py:447` is
  never reached);
- the provider is **not called** (`provider calls: 0`);
- the structured JSON fallback is **not reached** (it is behind a
  `conversational` verdict);
- the proactive planner is **not reached** and would have been `False` anyway;
- no authorization layer re-checks the classification.

The prompt-based immediate-vs-durable rule added in `916536c` is therefore
**not connected** to the decision for this class of input. It governs only
messages the deterministic boundary abstains on. The fix committed in `916536c`
improved the contract and added a paraphrase-level regression test; it did not
touch — and could not have touched — the function that actually decided this
request's fate.

**Not claimed.** This investigation does not claim to have observed the live
Telegram server; the reproduction is a local execution of the current source
through the real dispatcher, registry and executor. It does not claim any
provider behavior in production beyond the fact that no provider round occurs
on this path (which is a property of the code path, not of the model). It does
not claim the issue is fixed.

---

## 17. Proposed remediation options

Documentation only — **none of these was implemented.**

**Option A — Require the cadence word to govern the action (narrowest,
recommended).** Tighten `_is_scheduling_intent` so the bare-membership branch
at `actions.py:2674` no longer decides on its own. Minimal, evidence-backed
shape: require a cadence token to be in a scheduling *position* — adjacent to an
interval intro (`هر` + time unit), or paired with a plan/explicit-schedule
marker — rather than anywhere in the message. This is the same adjacency
discipline the module already uses for the clock anchor
(`_has_future_clock_request`, `actions.py:2620-2633`) and would stay
token-based, consistent with the no-regex-in-routing rule. Expected effect on
the observed request: `_is_scheduling_intent` → `False`,
`parse_command_intent` → `conversational`, request reaches the provider, and
the `template.py:85` contract becomes the governing rule as designed.

**Option B — Require multi-step ordering words to be treated as order, never as
schedule.** The `اول / بعد / و بعد` sequence in the message is a strong
immediate-workflow marker. A rule that sequencing words veto the bare cadence
branch would fix this class specifically. Narrower than A, but more
special-purpose and easier to overfit to this one message.

**Option C — Model-driven decision for the whole class.** Drop the deterministic
scheduling branch from `parse_command_intent` entirely and let the provider +
prompt contract decide. This is architecturally the cleanest split, but it
regresses the documented reliability guarantee that recurring requests work
without any provider round (`dispatcher.py:410-419`) — and durable task creation
would then depend on every provider being healthy. Not recommended alone.

**Option D — Add a code-level guard on the provider path.** Independently of
A/B/C, a bounded post-model check in the tool loop rejecting a `create_task`
call whose originating message has no *anchored* schedule expression would make
the immediate-vs-durable distinction enforced in code rather than by prompt
wording. It would also close the capability-mention gap documented in §15.
Higher surface area; must not become a second intent system.

**Option E — Test-only remediation (necessary regardless).** Add a regression
class to `tests/test_intent_routing_boundary.py` that (a) uses the exact
production string, (b) pairs each true positive with an attributive look-alike
false positive, (c) asserts `_is_scheduling_intent` in **both** directions, and
(d) drives the real `Dispatcher` with a provider that fails the test if called,
asserting the tool loop is entered. Without (d) the suite can stay green
through any prompt-only change.

**Recommended sequencing:** A + E together, then re-evaluate D independently.

---

## 18. Explicitly rejected approaches

| Rejected | Why |
|---|---|
| Adding regex-based command/intent routing | Directly contradicts the recorded architectural decision (`6bec694` "remove regex based tool command routing"); the module documents that no regex participates in command/intent routing. Rejected by the project's own rules. |
| Removing existing routing | Explicitly forbidden by the investigation's no-fix rule, and it would regress the provider-independent fast path. |
| A second dispatcher / executor / planner / permission system | Violates the single-recovery-authority and single-executor rules. The dispatcher and `ToolExecutor` are correct here; the defect is in a vocabulary predicate. |
| Changing the provider contract or tool schemas | Would not affect the observed path — no provider is called. |
| Database / Supabase changes | No schema or persistence defect is implicated. The misroute happens before any repository call. |
| Weakening `CreateTaskTool`'s completeness gate | The gate is not the origin; it shares the same predicate. Relaxing or tightening it alone changes nothing. |
| Adding a global "always ask before creating a task" confirmation | `create_task` is intentionally `READ_WRITE`/`safe` under the single-owner premise; a blanket gate would be a new permission system and would break the documented direct-creation path. |
| Rewriting `parse_command_intent`'s scheduling branch wholesale | The branch is correct for genuine schedules (`test_task_nl_creation.py` proves the positive case). The defect is the *looseness* of one predicate, not the branch's existence. |
| Treating this as a provider/prompt-tuning problem | The provider is never called on this path. Prompt wording cannot influence a turn that ends before prompt construction. |

---

## 19. Files that must NOT be changed during the eventual fix

Unless the eventual fix produces evidence that one of these is genuinely
implicated, remediation should be confined to:

- `backend/ai/actions.py` — specifically `_is_scheduling_intent` (`:2653-2699`)
  and its vocabulary at `:2551-2568`.
- `tests/test_intent_routing_boundary.py` — extend, do not weaken or delete.

The following must **not** be touched:

- `backend/ai/prompt/template.py` — the immediate-vs-durable contract is
  already correct; the defect is that it is not consulted.
- `backend/ai/tools/executor.py` — `_STATUS_LABELS` and `execute_calls` behaved
  exactly as designed.
- `backend/ai/tools/registry.py` and `backend/ai/tools/task.py` — the
  `create_task` tool, its description, its permission level and its recursion
  guard are all sound.
- `backend/ai/proactive.py` — fail-closed and correct; it was never the cause.
- `backend/ai/task_interpreter.py`, `task_creation.py`, `task_contract.py`,
  `backend/ai/database/**` — the durable-task boundary and persistence are not
  implicated.
- `backend/runtime/supervisor.py` and the rest of `backend/runtime/**` — the
  single recovery authority is out of scope.
- `backend/bot/handlers/ai_unified.py` — the handler only renders what the
  engine reports; it makes no intent decision.
- Provider implementations under `backend/ai/providers/**` — never reached.
- `IMPLEMENTATION_REPORT.md` — historical delivery record, not a source of truth
  for this defect.

---

## 20. Exact next implementation target

**Target function:** `_is_scheduling_intent()` in
`backend/ai/actions.py:2653`, and specifically the unanchored membership test
at `backend/ai/actions.py:2674`:

```python
if any(w in _FA_RECUR_WORDS or w in _EN_RECUR_WORDS for w in words):
    return True
```

**Target test file:** `tests/test_intent_routing_boundary.py`.

**First implementation step.** Make the bare-cadence branch at `actions.py:2674`
conditional on the cadence token actually governing the action, using the
module's existing token-adjacency discipline (the pattern already applied by
`_has_future_clock_request`, `actions.py:2620-2633`) — no regex, no new parser,
no new vocabulary module, and no change to the surrounding `parse_command_intent`
branch structure.

**First test step, before any production edit.** Add to
`tests/test_intent_routing_boundary.py`:

1. the exact production string as a parametrized case, expected
   `KIND_CONVERSATIONAL` with no `tool_calls`;
2. attributive look-alikes (`قیمت هفتگی رو بگو`, `اسم انیمه های هفتگی رو بگو`)
   expected `conversational`;
3. retained true positives (`هر ۱ ساعت …`, `هر پنج دقیقه …`, `برنامه ریزی کن …`)
   expected `executable` / `create_task` — proving the fix does not weaken the
   durable path;
4. `_is_scheduling_intent` asserted in **both** directions;
5. one end-to-end `Dispatcher.dispatch` case with a provider that fails the test
   if it is called, asserting the request reaches the provider stages — so the
   test can no longer pass while production short-circuits.

**Definition of done for that change:** the new cases pass; the existing
boundary suite, `test_task_nl_creation.py`, `test_task_nl_interval_creation.py`,
`test_25_fast_path.py`, `test_proactive_action_chains.py` and the full suite stay
green; the production string reaches the provider instead of the local fast
path; and the durable examples still route to `create_task`.

**Explicitly not part of this task:** the code change itself. This document is
the investigation result only.
