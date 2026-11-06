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
