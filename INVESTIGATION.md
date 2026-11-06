# Investigation — Dynamic Test-Verified Production Model Fallback

Scope: how a user request travels from the AI Engine into `ProviderManager`, what
decides a model's production eligibility, and how failures are attributed to a
model vs. a provider. Everything below is source-verified in this tree; nothing
is inferred from intent or documentation alone.

---

## 1. The routing path (unchanged boundaries)

```
ai_unified / task_execution
        │
        ▼
Engine → Dispatcher._call_provider()          backend/ai/engine/dispatcher.py:282
        │        await self._provider_manager.chat(provider_messages, **kwargs)
        ▼
ProviderManager.chat()                        backend/ai/providers/manager/manager.py
        │  1. enumerate providers (active first, then AI_PROVIDER_FALLBACK,
        │     then every other registered non-dummy provider)
        │  2. _skip_reason() per provider, _score() per provider
        │  3. _candidate_models() per provider → the MODEL-level pool
        │  4. sort (JSON capability, active first, score)
        │  5. sequential loop → _attempt_with_retry() per (provider, model)
        ▼
provider.chat(messages, model=…)             backend/ai/providers/openai_compat.py
                                                                  gemini.py, cloudflare.py
```

`ProviderManager.chat()` is the only production caller of `provider.chat()`. The
loop is a plain `for … await …` — there is no `asyncio.gather`, no task creation,
no speculative request. One user request therefore reaches exactly one model at a
time and stops at the first usable response.

## 2. What already fed the pool (and what did not)

- `backend/ai/model_tester.py::test_all_models()` runs the complete diagnostic
  (every chat model of every chat provider, bounded concurrency, no budget) and
  then calls `_feed_production_candidates(results, …)`.
- `proven_usable_candidates()` keeps ONLY `status == "AVAILABLE"` results, skips
  every result carrying `cached: True`, drops the provider's configured model
  (the router already tries it first), and orders the remainder with the same
  deterministic free-first selector used by the UI.
- `_feed_production_candidates()` publishes that map through the existing
  `ProviderManager.set_model_candidates(provider, models)` contract.
- `_candidate_models()` returns configured model first, then EVERY fed model,
  de-duplicated and empty-free — deliberately no per-provider cap.

So the dynamic, all-AVAILABLE pool was already wired. This task did **not** add a
second feed: it hardened eligibility and failure attribution around that pool.

## 3. Defects this task corrects

### 3.1 A model failure disabled the whole provider

`_apply_failure()` recorded every `timeout` / `network` / `server` /
`rate_limited` failure through `ProviderHealthTracker.record_failure(name, …)`,
i.e. at PROVIDER scope. One slow model therefore put a provider with dozens of
tested-available models into cooldown, and `_skip_reason()` then skipped the
provider entirely.

`_skip_reason()` also skipped the whole provider when its CONFIGURED model was
marked unavailable:

```python
model = self._effective_model(provider)
if model and self._model_unavailable(name, model):
    return f"model={model}:unavailable"
```

That turned a single dead model id into a provider outage.

### 3.2 Unavailable pairs were not removed from the pool

`_candidate_models()` never consulted `_unavailable_models`, so a model already
proved dead by a 404 stayed in the candidate list on subsequent requests (it was
only skipped when it happened to be the configured model).

### 3.3 Rate-limit scope was not established

Both `openai_compat.py` and `gemini.py` returned `failure_type="rate_limited"`
with `http_status=429` and `retry_after`. Neither adapter reported what the 429
was about. `openai_compat` discarded the error body for 429 entirely (it only
kept `request_id`), so no consumer could distinguish

| Provider evidence | Actual scope | Source |
|---|---|---|
| OpenAI `code: insufficient_quota` / "exceeded your current quota … billing" | account/project | `openai_compat._error_fields` |
| Groq "Rate limit reached for model \`<id>\` … requests per min (RPM)" | that model | Groq error message |
| Gemini 429 "You exceeded your current quota …" (free-tier per-project metric) | project/account | `gemini._error_fields` |
| Cloudflare Workers AI 429 (account-scoped endpoint, no model field) | account (not established in body) | `cloudflare.py` |
| Any 429 with no recognizable body | **unknown** | — |

Before this task all five were handled identically at provider scope. That is
safe for account quotas but wrong for per-model limits, and it is also the reason
sibling models were never tried after a model-specific 429.

### 3.4 Mid-request eligibility was never re-proved

The candidate list is built ONCE before the loop. A provider disabled or cooled
down by candidate #1 was still asked for candidates #2..#N, because only the
pre-loop `_skip_reason()` had checked health.

## 4. Decisions

1. **Model-level state belongs to the existing health tracker.** `ProviderHealthState`
   / `ProviderHealthTracker` gained a parallel model clock
   (`_model_until`, `_model_failures`, `is_model_available`,
   `record_model_failure`, `record_model_success`). No new manager, router or
   executor; `ProviderManager` still owns the decision and calls it.
2. **Scope follows evidence, never assumption.** `classify_rate_limit_scope()`
   in `manager/health.py` returns `model` only when the body names THIS model
   and describes a model-level limit, `provider` for quota/billing/account
   markers, and `unknown` otherwise. An ambiguous 429 gets the bounded middle
   ground: the attempted pair is cooled tentatively and ONE distinct sibling
   model of the same provider is probed per `chat()` invocation. A second 429
   (or the absence of an eligible sibling) is the evidence that opens the
   provider circuit.
3. **Transient failures are model-scoped only when there is a sibling to
   protect.** With a single candidate model, the pre-existing provider-wide
   penalty is kept (nothing else could be spared).
4. **Provider-wide evidence still opens the existing circuit breaker.** Auth
   disables the provider; provider-scope/unknown 429 cools it down; three
   DISTINCT models of one provider failing inside 60 s escalates the transient
   failure to the provider circuit (`PROVIDER_OUTAGE_MODELS`,
   `PROVIDER_OUTAGE_WINDOW`).
5. **Eligibility is re-proved per attempt.** The in-loop check skips a candidate
   whose provider stopped being available, or whose pair became unavailable, and
   records it in the failure matrix (`outcome: skipped`).
6. **`model_not_found` keeps its 3600 s pair TTL** (`_unavailable_models`) — a
   permanent configuration error for that id, never a provider penalty.
7. **A model fallback is telemetry, not a provider change.** A success whose
   model differs from the FIRST attempted candidate carries `fallback`,
   `fallback_from_model`, `fallback_to_model` and `model_fallback` even when the
   provider name is unchanged; `model` names the serving model and
   `candidates_attempted` counts the pool work. `ai_retry_count` stays the
   SAME-MODEL retry count, so candidate fallback never inflates it.
8. **`_MAX_RETRIES` is the same-model retry budget.** The attempt loop spends
   it (one retry today); candidate fallback never consumes it, so reaching a
   20-model pool does not require retrying one model 20 times.
9. **The ambiguity probe budget is invocation-local.** `_AmbiguityProbe` is
   created inside `chat()` and never stored on the manager: a new call starts
   fresh, bounded only by the cooldowns previous calls recorded.

## 5. What is NOT established

- No live provider was contacted in this environment. The scope classifier is
  verified against the documented/observed error shapes listed in §3.3, not
  against a fresh 429 from each vendor.
- Cloudflare's 429 body is not parsed for a model field; it therefore starts in
  the `unknown` branch and, when a sibling is eligible, costs one extra probe
  request before the provider is treated as limited.
- Model and provider health state is in-memory (process lifetime), exactly like
  the pre-existing provider state; a restart clears every cooldown.
- The ambiguous-429 probe is one EXTRA provider request; it is bounded per
  invocation and by the candidate pool, never by a global counter.
- The escalation threshold (3 distinct models / 60 s) is a policy choice, not a
  measurement of any provider's failure correlation.
- No UI surface shows model-level cooldowns; only the router and its logs do.

## 6. Current test coverage of this area

`tests/test_dynamic_model_fallback.py` (38 tests) drives the real
`ProviderManager.chat()` with scripted providers that record `(provider, model,
messages)` and concurrent overlap: 20-model pool completeness, primary-first
order, deterministic candidate order, request identity across candidates,
first-usable-wins, no fan-out, model-vs-provider health, the three rate-limit
scopes, the bounded ambiguous-429 probe (sibling success, second 429, non-429
probe failure, no sibling, fresh per-invocation budget, cooldown respect),
cached/untested exclusion, bounded retry, same-model-retry vs model-fallback
telemetry, cross-provider fallback, tool-call/structured-output preservation,
honest exhaustion, and a real `Dispatcher` run where the healthy backup serves
the initial fallback plus two tool continuations with no overlapping calls and
no replayed tool.

Existing suites that also cover this path and still pass: `test_23_provider_mesh.py`,
`test_24_execution_reliability.py`, `test_17_providers.py`,
`test_provider_structured_output.py`, `test_provider_tool_boundary.py`,
`test_provider_system_instruction_contract.py`,
`test_task_fallback_classification.py`, `test_task_fallback_cooldown.py`,
`test_13_model_selection.py`, `test_model_tester.py`,
`test_model_tester_snapshot.py`.

---

## 7. Follow-up investigation — why AI / fallback events were missing from the Render capture

Scope of this section: **observability only**. No fallback decision, candidate
ordering, 429 classification, cooldown duration, retry budget, sibling-probe
policy, provider selection or dispatcher responsibility was changed by it.

### 7.1 What the capture itself establishes

The 2026-11-06 capture (162 lines, ~10 s, one real fallback test executed inside
that window) contains only `backend.bot.handlers.anti_edit_delete` records, and
**not one `[TRACE]` line**.

That matters because `[TRACE] AI_TRIGGER_MATCHED` is emitted at **WARNING** by
`backend/bot/handlers/ai_unified.py` on every matched activation, before any AI
work (`trace("AI_TRIGGER_MATCHED", …)`), and `backend/runtime/tracer.py` logs
every trace event at WARNING. Its absence means the chat activation gate never
matched during that window — it does **not** mean the records were suppressed.

### 7.2 Confirmed root cause: the events were never generated, not filtered

Measured, not argued. The exact configuration from `backend/main.py` was
reproduced and INFO records were emitted from the real AI module loggers:

| Probe | Result |
|---|---|
| `logging.basicConfig(level=WARNING, stream=stdout)` + `getLogger("backend").setLevel(INFO)` | INFO from `backend.ai.providers.manager.manager`, `backend.ai.engine.dispatcher`, `backend.bot.handlers.ai_unified`, `backend.tracer` **and** `backend.bot.handlers.anti_edit_delete` all reached stdout |
| Effective level per logger | `INFO` for every `backend.*` logger |
| `logger.propagate` | `True` for all of them |
| Root handlers | exactly 1 |

`ai_unified.py`, `manager/manager.py` and `engine/dispatcher.py` already emitted
`AI_REQUEST_START`, `ROUTER_SELECTED`, `AI_PROVIDER_ATTEMPT`,
`AI_PROVIDER_FAILURE`, `PROVIDER_MODEL_COOLDOWN`, `PROVIDER_COOLDOWN` and
`AI_PROVIDER_RESPONSE` at INFO/WARNING. Nothing in the configuration filtered
them: there is **no** `dictConfig`, `addFilter`, `addHandler`, `logging.disable`
or stdout redirection anywhere in `backend/` — `backend/main.py` is the only
module that configures logging at all.

So the missing records were never generated on the path that actually ran. Three
concrete, source-verified reasons:

1. **The transition events did not exist outside a task trace.**
   `task_trace("provider_fallback", …)`, `task_trace("provider_selection", …)`
   and `task_trace("provider_fallback_exhausted", …)` all go through
   `backend/ai/task_trace.py`, whose `task_trace()` returns immediately when no
   `create_task` request is bound (`if binding is None: return`). Ordinary chat
traffic is unbound by design, so the model-fallback and candidate-exhaustion
traces the operator was looking for were silently discarded.
2. **The fallback success line was success-only.** `AI_PROVIDER_FAILOVER` was
   logged inside `if provider_fallback or model_fallback:` *after*
   `response.success`, so a fallback that started and then failed produced no
   "from → to" record at all.
3. **Two execution paths produced no evidence of their own.** The chat path had
   silent early returns at every activation gate (no trigger match, empty prompt
   text, dot prefix, non-owner) at **any** level, and the Model Tester diagnostic
   (`backend/ai/model_tester.py::_test_one_model`) awaits
   `provider_inst.chat(...)` on a factory-built provider **directly** —
   ProviderManager and Dispatcher are not in that call graph, and the tester had
   no per-model record, so a "fallback test" run there could not emit the events
   even in principle.

A fourth, real defect: `LOG_LEVEL` is documented (AGENTS.md §11) and set in
`render.yaml` (`INFO`) but **no code read it** — the level was hardcoded.

### 7.3 Hypotheses this rules out / leaves open

| Hypothesis | Status |
|---|---|
| 2. Wrong or overly restrictive level | **Refuted** (§7.2) |
| 3. Logger names / hierarchy | **Refuted** — every logger is under `backend`, effective INFO |
| 4. Propagation disabled / handlers wrong | **Refuted** — propagate=True, one root handler |
| 5. Config initialised too late / overwritten | **Refuted** — `main.py` configures at import; nothing else configures logging |
| 6. Custom handler / filter / formatter suppression | **Refuted** — none exists in `backend/` |
| 7. Destination Render does not capture | **Refuted** for stdout logging (the AED INFO lines are captured) |
| 1. Paths do not emit the events | **Confirmed** (§7.2, three cases) |
| 8. Deployed runtime differs from this tree | **Not verifiable from here** — no Render access |
| 9. The capture was filtered before reaching the reader | **Cannot be excluded** — a viewer-side filter is not observable from source |

### 7.4 What the observability fix adds

Every event is emitted where the transition is decided, reuses existing metadata,
and carries no prompt text, response body, key or credential.

| Event | Where | Level |
|---|---|---|
| `AI_RATE_LIMIT_CLASSIFIED provider= model= scope= retry_after=` | `manager._apply_failure` | INFO |
| `PROVIDER_MODEL_COOLDOWN provider= model= category= seconds=` (model-scoped) | `health.mark_model_cooling` | INFO |
| `PROVIDER_COOLDOWN provider= category= seconds=` (provider-wide) | `health.mark_cooling_down` | INFO |
| `AI_FALLBACK_START kind=model\|provider from= to= reason= attempt= pool=` | `manager._log_fallback_start` | INFO |
| `AI_CANDIDATES_EXHAUSTED failed= skipped= error_entries= category=` | `manager._fallback` | WARNING |
| `AI_RESULT id= success= provider= model= finish_state= …` | `dispatcher.dispatch` | INFO |
| `AI_CONTINUATION_FALLBACK id= round= kind= from= to=` | dispatcher continuation round | INFO |
| `MODEL_TEST_RESULT …` / `MODEL_TEST_COMPLETE …` | `model_tester` | DEBUG / INFO |
| `AI_ACTIVATION_SKIPPED reason=` | `ai_unified` activation gates | DEBUG |

`AI_RESULT` reports `provider=`/`model=` only on success, so a failed candidate
can never be read as the serving model. `AI_FALLBACK_START` distinguishes a
model fallback (same provider, different model) from a provider fallback (another
provider); a same-model retry emits none and remains visible as
`AI_PROVIDER_ATTEMPT attempt=2 reason=…`.

### 7.5 Tests

`tests/test_ai_fallback_logging.py` (13 tests, all passing) asserts: attempt and
selection events and their levels; model-scoped 429 → classification + model
cooldown with no provider cooldown; provider quota → distinguishable provider
cooldown; ambiguous-429 sibling probe → observable `kind=model` fallback;
same-model retry is not a model fallback; provider vs model fallback
distinguishability; exhaustion counts and sanitized category; the final serving
model; a failed candidate never reported as serving; continuation fallback with
its round; no key/prompt/response body in any record; and no duplicate handler or
duplicate record after repeated configuration.

The three Dispatcher tests drive the **real** `ProviderManager` (a `MagicMock` is
not a substitute: `Dispatcher` treats a non-`ProviderManager` argument as a
registry and wraps it).

### 7.6 What is still not established

- No deployed-Render run was performed or claimable from this environment: no
  Render shell access, no log stream. Whether these events now appear in the
  service's log view can only be answered by the next live test.
- The capture being viewer-filtered (hypothesis 9) cannot be disproved from
  source; if it was, the fix does not change that viewing behaviour.
- Which of the two AI paths the operator's "fallback test" used is not recorded
  anywhere; both paths are now observable, so the next test answers it either way.
- Cooldown/level values are unchanged, so log volume is added only at real state
  transitions (one line per fallback, one per provider attempt, one per request
  result), plus per-model DEBUG that is invisible at the deployed `LOG_LEVEL=INFO`.

## B.14 AED log noise and the AI tool lifecycle (2026-11-06)

Investigated at the then-current `main` tip `6958985`.

**Finding 1 — the AED log lines had three distinct sources, all in AED code.**
The `[AED] … skipped: peer is not a confirmed human` lines were emitted by the
handlers in `backend/bot/handlers/anti_edit_delete.py`, not by the service gate;
the service produced the DEBUG "edit-shaped update ignored" lines and the
WARNING/INFO configuration, flood-wait, media and retention lines. Removing
them required no change to the classification itself: the tri-state
(`True` / `False` / `None`) from the resolved `User.bot` flag already existed and
is kept intact.

**Finding 2 — the tool lifecycle was observable at the batch level only.**
Before this change the Dispatcher logged one start/end pair per tool batch and
one line per result (`AI_TOOL_EXECUTION_START/END`, `AI_EXECUTION_RESULT/ERROR`),
while the `ToolExecutor` logged only timeouts and exceptions. Consequently a
call that never reached the executor (unknown tool, malformed arguments,
confirmation gate, absent executor) produced no line distinguishing it from a
call that ran and failed, and a multi-call batch could not be split per call.
The executor is the only place that knows the per-call outcome, which is why the
per-call events are emitted there rather than reconstructed in the Dispatcher.

**Finding 3 — `request_id` was already available at the executor boundary.**
`Dispatcher._build_tool_context` is the single construction site for the
per-request `ToolContext` and already sets `extra["request_id"]` (used by
`DELETE_TIMEOUT`). No new id, context, or propagation channel was needed; only
`provider`/`model` labels were added to that same dict.

**Finding 4 — a continuation round is not an executor retry.** The tool loop's
round number is the continuation index, while the executor has no retry budget;
the new `round` field therefore comes from the loop (1 for the initial round,
n+1 for the nth continuation).

Not investigated / unchanged: provider HTTP retry and fallback behaviour, tool
selection quality, Taskloom scheduling.
