# Implementation Report — Dynamic Test-Verified Model Fallback

## 1. Summary

Production fallback is now driven entirely by live Model Tester evidence: every
model whose latest diagnostic result is `AVAILABLE` joins the router's candidate
pool for its provider, with no fixed count and no hardcoded backup list. For one
user request the manager walks that pool strictly one model at a time, returns
the first usable response and never contacts another model afterwards. Failures
are attributed at the scope their own evidence supports — a dead or throttled
model cools down that `(provider, model)` pair, while auth and account quota
evidence take the whole provider down. An opaque 429 no longer disables the
provider on its own: it cools only the pair that produced it and spends ONE
sibling probe for that invocation; a second 429 is what opens the circuit.

## 2. Exact files changed

| File | Change |
|---|---|
| `backend/ai/providers/manager/manager.py` | Model-scoped failure handling, pool filtering of unavailable pairs, per-attempt eligibility re-check, sibling-based escalation, model success reset |
| `backend/ai/providers/manager/health.py` | Per-`(provider, model)` health state + `classify_rate_limit_scope()` |
| `backend/ai/providers/openai_compat.py` | 429 metadata now carries the provider error code/type/message and model (used for scope) |
| `backend/ai/engine/dispatcher.py` | Aggregates `model_fallback_used` / `fallback_to_model` across the empty-retry and every continuation round |
| `backend/bot/handlers/ai_unified.py` | Failure notice states `backup model tried` for a model backup and `backup tried` for a provider failover |
| `tests/test_dynamic_model_fallback.py` | 38 regression tests driving the real `ProviderManager.chat()` plus one real `Dispatcher` tool-continuation run |
| `INVESTIGATION.md`, `ROADMAP.md`, `IMPLEMENTATION_REPORT.md` | Replaced for this task |

No other file was modified. No Supabase/SQL change, no `DATABASE_ARCHITECTURE.md`
change, no schema/migration, no second router/executor/scheduler.

## 3. How the dynamic AVAILABLE pool reaches production

```
test_all_models()                                  # full diagnostic, all chat models
  → proven_usable_candidates(results, …)           # status == AVAILABLE, cached skipped
  → _feed_production_candidates(results, …)        # existing ProviderManager contract
  → ProviderManager.set_model_candidates(provider, models)
  → _candidate_models(provider)                    # configured model first, then each fed id
  → chat() sequential candidate loop
```

- Eligibility is a Test Modules RESULT, never a discovery side effect: a model
  that was never tested, was skipped (`NOT_ATTEMPTED`), was unconfigured, or
  failed is never fed.
- A result carrying `cached: true` is excluded inside `proven_usable_candidates()`
  and the persisted snapshot payload is never passed to the feed at all, so a
  stored diagnostic can never arm production.
- The provider's configured/default model stays the first candidate and is
  dropped from the feed (no duplicate attempt).
- Candidate ordering is the pre-existing deterministic one: configured model,
  then the free-first discovery selector order, then stable alphabetical ids
  without discovery metadata. `_candidate_models()` has no cap.

**Eligible candidates:** as many as the diagnostic proved AVAILABLE — one
provider can contribute dozens. The new test publishes a **20-model** pool and
asserts all 20 distinct models are contacted in order; the manager contains no
limit of two or three.

## 4. How sequential execution and first-response-wins are enforced

`ProviderManager.chat()` builds the ordered `eligible` list once, then runs a
single `for … await self._attempt_with_retry(...)` loop. There is no
`asyncio.gather`, no `ensure_future`, no speculative request anywhere in the
path. Each candidate is awaited to completion before the next one is considered.

- On a usable `success=True` response the method returns immediately; the loop
  is never re-entered, so later candidates receive nothing.
- Empty or contract-violating success responses fail over only while another
  candidate remains, exactly as before.
- A new per-attempt eligibility check skips (and records as `skipped` in the
  failure matrix) any candidate whose provider stopped being available or whose
  pair became unavailable while this same request was running.
- Tool calls are never executed here: the executor boundary is untouched, and
  `_note_response_quality` / the dispatcher's own retry remain the only
  quality-recovery paths.

The Model Tester's own concurrency (bounded semaphore, independent targets) was
deliberately NOT serialised.

## 5. Model-level vs provider-level failures

| Failure | Scope | Effect |
|---|---|---|
| `model_not_found` (404) | `(provider, model)` | pair unavailable for 3600 s; provider untouched |
| `timeout` / `network` / `server` / other transient WITH sibling candidates | `(provider, model)` | pair cooled down with the existing per-category penalty; provider stays healthy |
| same transient WITH no sibling model | provider | pre-existing provider-wide penalty (nothing to protect) |
| 3 DISTINCT models of one provider failing inside 60 s | provider | `_escalate_if_provider_wide` opens the existing circuit breaker |
| `auth` (401/403) | provider | disabled until configuration changes (unchanged) |
| 429 with provider/account evidence (`insufficient_quota`, billing/credit markers) | provider | cooldown honoring Retry-After (unchanged) |
| 429 the body does not explain, first occurrence with an eligible sibling | `(provider, model)` + one probe | tentative pair cooldown; exactly ONE distinct sibling model is probed; provider stays healthy |
| 429 the body does not explain, second 429 (or no eligible sibling) | provider | provider-wide cooldown; the provider's remaining models are not contacted |
| 429 naming THIS model with a model-level limit phrase | `(provider, model)` | pair cooled down; sibling models remain eligible |
| `request` (4xx invalid request) | none | unchanged: no cooldown, deterministic advance |

`_skip_reason()` no longer skips a whole provider because its configured model
is unavailable; `_candidate_models()` removes the dead/cooling pair so the
provider's other models stay in the pool. A served request clears both the
provider's failure streak and that model's cooldown.

Rate-limit scope is decided only from the provider's own response:
`classify_rate_limit_scope()` reads an explicit `rate_limit_scope` if an adapter
ever supplies one, otherwise the preserved error code/type/message. It returns
`model` only when the text names the exact requested model AND contains a
model-level limit phrase (Groq's documented shape); quota/billing/account markers
return `provider`; everything else is `unknown`, which is treated as
provider-wide.

**Model identity across timeout/exception failures (targeted fix).** The model an
attempt actually asks for is carried explicitly rather than re-derived from
response metadata: `_attempt_with_retry` computes `current_model = model or
self._effective_model(provider)` once and passes it to every `_apply_failure()`
call (initial failure, transient retry failure, short-Retry-After retry
failure), and `_call_once()`'s own `asyncio.TimeoutError` and generic-exception
handlers now record `requested_model` in their metadata. Before this fix a
non-default candidate that timed out or raised had no model in its metadata, so
`_apply_failure()` fell back to the provider's configured default model and
cooled down the WRONG `(provider, model_id)` pair — leaving the failing backup
eligible and disabling the healthy primary. A configured-default attempt still
carries no `requested_model`, so an override never silently relabels it.

**Ambiguous-429 probe and backup reuse.** The probe budget is a
`_AmbiguityProbe` created INSIDE one `chat()` invocation — never a manager
field, global counter or sticky session flag — so each call starts with a fresh
budget while every cooldown already recorded stays effective. On an unknown-scope
429 the manager cools the attempted pair tentatively and lets the candidate loop
continue to the next eligible model of that provider. If that sibling also
answers 429 the budget is already spent and the normal provider-wide cooldown
applies, so the rest of that provider's models are skipped by the in-loop
eligibility check. A non-429 sibling failure is classified normally (model
cooldown, with the existing escalation only when distinct models keep failing).
A successful sibling clears only its own model state and keeps the provider
healthy, so later independent calls — including the Dispatcher's continuation
rounds after tool execution — build their candidate pool from that same,
still-eligible model. `_MAX_RETRIES` now drives the retry loop itself: at most
that many SAME-MODEL retries (one today); candidate fallback never spends it,
and `MAX_TOOL_ROUNDS` remains a separate execution bound.

**Fallback telemetry.** A success whose model differs from the first attempted
candidate carries `fallback`, `fallback_from_model`, `fallback_to_model` and
`model_fallback` even when the provider name did not change, `model` names the
serving model, and `candidates_attempted` counts distinct attempted candidates.
`ai_retry_count` still reports only same-model retries, so a model fallback
never inflates the visible retry count. The Dispatcher OR-merges
`fallback_used` / `model_fallback_used` and keeps `fallback_to_model` across the
initial request, its bounded empty retry and every tool continuation.

## 6. Exact tests executed and results

| Command | Result |
|---|---|
| `.venv/bin/python -m pytest tests/test_dynamic_model_fallback.py -q` | **38 passed** in 0.82 s |
| focused group: `test_dynamic_model_fallback.py`, `test_23_provider_mesh.py`, `test_24_execution_reliability.py`, `test_17_providers.py`, `test_provider_structured_output.py`, `test_provider_tool_boundary.py`, `test_provider_system_instruction_contract.py`, `test_task_fallback_classification.py`, `test_task_fallback_cooldown.py`, `test_13_model_selection.py` | **167 passed** in 10.64 s |
| retry UX + media AI: `test_35_ai_retry_ux.py`, `test_media_ai_integration.py` | **60 passed** in 0.96 s |
| full suite: `.venv/bin/python -m pytest tests -q --tb=line -p no:cacheprovider` | **6990 passed, 27 skipped, 1 failed** in 135.42 s |
| `.venv/bin/python -m py_compile` on every changed Python file + the new test | exit 0 |
| `git diff --check` | exit 0 (no whitespace errors) |

The two full-suite failures are unrelated to this change:

1. `tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`
   — pre-existing failure, reproduced on the untouched baseline.

The previously observed load-dependent
`tests/test_private_message_scope.py::test_concurrent_messages_from_one_person_cannot_exceed_the_limit`
flake did not recur in this run.

A 12-file variant of the focused group that also includes
`test_11_runtime_wiring.py` reports one further failure
(`test_ai_test_models_action_launches_streaming_run`). That test passes alone
and the same order-dependent failure reproduces on an untouched `git archive`
copy of HEAD, so it is pre-existing test-order pollution, not a regression.

New coverage (all asserting real calls, call order, requested model ids and
maximum concurrent candidate calls):

1. 20-model AVAILABLE pool is not truncated.
2. Configured primary model is attempted first.
3. Remaining candidates follow the deterministic order.
4. The same original request object reaches every candidate.
5. A failed first model is followed by exactly one next model.
6. A successful first model blocks every later candidate.
7. A successful third candidate blocks the fourth and later ones.
8. Candidate requests never overlap (`max_active == 1`).
9. A model timeout does not disable sibling models (provider stays healthy).
10. `model_not_found` marks only the correct `(provider, model_id)` pair.
11. Provider-wide auth failure keeps its provider-wide behaviour.
12. Provider/account quota 429 cools the provider; siblings are not contacted.
13. Model-scoped 429 keeps siblings eligible.
14. Ambiguous 429 is handled conservatively (provider cooldown, no blind requests).
15. Cached AVAILABLE results cannot activate candidates.
16. Failed/untested/discovery-only models never enter the pool.
17. Bounded retry (exactly one) and the 30 s RPC timeout are preserved.
18. Cross-provider fallback still works with fallback metadata.
19. Tool-call and structured-output behaviour survives fallback.
20. Exhausting every candidate returns the honest failure response.
21. Scope classifier: Groq model message → `model`; `insufficient_quota`/Gemini quota → `provider`; opaque → `unknown`.
22. A provider with only one candidate keeps the pre-existing provider-wide penalty.

Added for the model-identity fix (real `chat()` exceptions, not scripted
failures):

23. `_call_once()` preserves `requested_model` on a real `asyncio.TimeoutError`
    and on a real exception, and never invents one for a configured-default call.
24. A backup model that really raises a timeout cools ONLY that pair; the
    configured primary stays `healthy` and the provider stays `healthy`.
25. A backup model that raises a general exception is attributed to that pair.
26. Initial attempt + bounded retry both failing keep the failure on that model.
27. The short Retry-After retry failing (real timeout on the retry) is
    attributed to the attempted backup model, keeping model-scoped limits
    out of the provider circuit.
28. The configured primary still cools down under its own id, with the backup
    left eligible.
29. A sibling model stays eligible after a backup cooldown, and the next
    request is served by the primary rather than by a globally cooled provider.

Added for the ambiguous-429 probe, backup reuse and telemetry:

30. An opaque 429 cools only the attempted pair, probes ONE sibling, and keeps
    the provider healthy; the serving model and fallback metadata are exact.
31. A second 429 during the probe cools the provider and the third sibling is
    never contacted; a later call does not contact the cooling provider at all.
32. A probe sibling failing with a non-429 (real timeout) gets ordinary
    classification: provider healthy, both failed pairs cooling, next sibling used.
33. An opaque 429 with no eligible sibling keeps the provider-wide penalty.
34. Each `chat()` invocation gets a fresh probe budget; a cleared pair cooldown
    is probed again, so the budget is not a sticky provider flag.
35. Model-scoped 429s on five models do not cap the pool: the sixth model serves.
36. Two attempted models with one transient retry report `ai_retry_count == 1`
    and `candidates_attempted == 2` — fallback does not inflate retries.
37. The visible failure notice says `backup model tried` for a model backup and
    never leaks the provider body, status code or an opaque error string.
38. A real `Dispatcher` run: the primary gets an opaque 429, the healthy backup
    serves the initial request AND two tool continuations, calls never overlap,
    each tool round executes exactly once (distinct ids), and the result carries
    `fallback_used`, `model_fallback_used`, `fallback_to_model` and retry_count 0.

## 7. Untested behaviour

- No live provider call was made from this environment; every provider in the
  tests is a scripted double.
- The rate-limit classifier is validated against documented/observed error
  shapes, not against a fresh response from each vendor.
- Cloudflare Workers AI 429 scope is not parsed beyond its error fields, so it
  normally resolves to `unknown` → provider-wide.
- Model-level cooldown TTLs (30–120 s) are the existing per-category penalties,
  not measured provider recovery times.
- The escalation threshold (3 distinct models / 60 s) is a policy choice; no
  corpus of real outages was used to tune it.

## 8. Live verification status

- **Live provider verification: none.** No key was available and no network
  request was issued. Passing tests prove routing, ordering and attribution
  logic, not provider reliability.
- **Live Telegram verification: none.** Nothing in the UI or handler layer was
  changed by this task; the router is exercised only through `chat()`.

Being `AVAILABLE` means a model answered a diagnostic request at test time. It
is evidence of availability then, never a guarantee of quota or permanent
availability, and this change does not attempt to make it one.

## 9. Known limitations

1. Model and provider health are in-memory; a restart clears every cooldown.
2. A model that cools down silently disappears from the pool — there is no UI
   surface that explains why a tested model is temporarily absent.
3. Scope detection is text/code based; providers that change their 429 body
   shape fall back to the conservative provider-wide branch.
4. The pool is only as fresh as the last diagnostic run; because cached results
   never arm it, a long-idle deployment keeps only the pool from its last live
   run (plus the configured model).
5. An ambiguous 429 costs one extra probe request when a sibling is eligible —
   the price of not disabling healthy models on unproven scope evidence.

## 10. Remaining work

1. Live-verify the 429 classifier per vendor.
2. Optionally surface per-model cooldown state in the AI diagnostics panel.
3. Re-run the Model Tester on a schedule if the AVAILABLE pool must track
   catalog changes.

## 11. Delivery

Committed as one scoped commit on top of the previous feature commit, pushed to
the connected repository's `main`, then verified with `git fetch`,
`git rev-parse` and `git merge-base --is-ancestor` against the remote head; the
working tree was confirmed clean afterwards.

---

## 12. Follow-up — AI model-fallback logging / observability

This section describes a **separate, logging-only follow-up** to §§1–11. It
changes no fallback behaviour; §§1–11 remain the record of the fallback feature.

### 12.1 Confirmed root cause

The AI and fallback events were **not filtered — they were never generated on
the path that ran**. `logging.basicConfig(level=WARNING, stream=stdout)` plus
`getLogger("backend").setLevel(INFO)` puts every `backend.*` logger at INFO with
propagation on and exactly one root handler, and INFO records emitted from the
real `manager`, `dispatcher`, `ai_unified` and `tracer` loggers all reach stdout
(measured, §12.2). The AED records in the capture came through that same path.

What was missing, by source:

1. `task_trace("provider_fallback" | "provider_selection" |
   "provider_fallback_exhausted", …)` is a **no-op unless a `create_task`
   request is bound** (`backend/ai/task_trace.py`: `if binding is None: return`),
   so for ordinary chat traffic the fallback and exhaustion transitions were
   discarded by design.
2. `AI_PROVIDER_FAILOVER` was emitted only **after a fallback succeeded**, so a
   fallback that started and then failed produced no "from → to" record.
3. The chat activation handler returned silently at every non-match gate at any
   level, and the Model Tester path
   (`model_tester._test_one_model` → `provider_inst.chat(...)`) bypasses
   ProviderManager/Dispatcher entirely with no per-model record of its own.
4. `LOG_LEVEL` was documented and set in `render.yaml` but read by no code.

### 12.2 Evidence supporting the diagnosis

- Reproduced `main.py`'s exact logging configuration and emitted INFO from
  `backend.ai.providers.manager.manager`, `backend.ai.engine.dispatcher`,
  `backend.bot.handlers.ai_unified`, `backend.tracer` and
  `backend.bot.handlers.anti_edit_delete`: all reached stdout, effective level
  INFO, `propagate=True`, one root handler.
- `grep` over `backend/` finds **no** `dictConfig`, `addFilter`, `addHandler`,
  `logging.disable`, `StreamHandler` or stdout redirection; `backend/main.py` is
  the only logging configuration in the project.
- The 2026-11-06 capture contains no `[TRACE]` line, while
  `[TRACE] AI_TRIGGER_MATCHED` is WARNING-level and emitted before any AI work.

### 12.3 Files changed (logging only)

| File | Change |
|---|---|
| `backend/main.py` | `_configure_logging()` + `_resolve_level()`: `LOG_LEVEL` now configures the `backend` namespace (default INFO); root stays WARNING; idempotent, no second handler |
| `backend/ai/providers/manager/manager.py` | `AI_RATE_LIMIT_CLASSIFIED`, `AI_FALLBACK_START` (model vs provider, before the replacement is attempted), `AI_CANDIDATES_EXHAUSTED` |
| `backend/ai/providers/manager/health.py` | Cooldown lines now carry the sanitized `category` (`PROVIDER_COOLDOWN`, `PROVIDER_MODEL_COOLDOWN`) |
| `backend/ai/engine/dispatcher.py` | `AI_RESULT` (final serving provider/model, success only) and `AI_CONTINUATION_FALLBACK` (round + transition) |
| `backend/bot/handlers/ai_unified.py` | DEBUG `AI_ACTIVATION_SKIPPED reason=…` at the silent activation gates |
| `backend/ai/model_tester.py` | DEBUG per-model result + one INFO `MODEL_TEST_COMPLETE` summary |
| `tests/test_ai_fallback_logging.py` | New: 13 observability regression tests |
| `INVESTIGATION.md`, `IMPLEMENTATION_REPORT.md` | This follow-up section |

No fallback ordering, 429 classification, cooldown value, retry limit, sibling
probe policy, `MAX_TOOL_ROUNDS`, ToolRegistry/ToolExecutor behaviour, provider
selection or provider/dispatcher ownership was modified. No new scheduler,
executor, listener or logging framework; no Supabase, SQL, Render or env change.

### 12.4 Events now observable

| Transition | Record | Level |
|---|---|---|
| Request started | `AI_REQUEST_START id= owner=` (pre-existing) | INFO |
| Candidate selected | `ROUTER_SELECTED provider= model= score=` (pre-existing) | INFO |
| Attempt started | `AI_PROVIDER_ATTEMPT provider= model= attempt=1` (pre-existing) | INFO |
| Same-model retry | `AI_PROVIDER_ATTEMPT … attempt=2 reason=transient\|rate_limit_window` (pre-existing, unchanged semantics) | INFO |
| 429 classified + scope | `AI_RATE_LIMIT_CLASSIFIED provider= model= scope= retry_after=` | INFO |
| Model cooldown | `PROVIDER_MODEL_COOLDOWN provider= model= category= seconds=` | INFO |
| Provider-wide cooldown | `PROVIDER_COOLDOWN provider= category= seconds=` | INFO |
| Model fallback started | `AI_FALLBACK_START kind=model from= to= reason=` | INFO |
| Provider fallback started | `AI_FALLBACK_START kind=provider from= to= reason=` | INFO |
| Fallback served | `AI_PROVIDER_FAILOVER … model_fallback=` (pre-existing, success-only) | INFO |
| Candidate exhausted | `AI_CANDIDATES_EXHAUSTED failed= skipped= error_entries= category=` | WARNING |
| Final AI result | `AI_RESULT id= success= provider= model= finish_state= model_fallback= provider_fallback= fallback_to_model= candidates_attempted= failure_type=` | INFO |
| Continuation fallback | `AI_CONTINUATION_FALLBACK id= round= kind= from= to=` | INFO |
| Model test (diagnostic) | `MODEL_TEST_RESULT …` / `MODEL_TEST_COMPLETE tested= available= …` | DEBUG / INFO |
| Activation declined | `AI_ACTIVATION_SKIPPED reason=no_trigger_match\|no_prompt_text` | DEBUG |

`AI_RESULT` prints `provider=- model=-` when the request failed, so a failed
candidate is never readable as the answer's model. Only one record per real
transition: one `AI_FALLBACK_START` per advance, one `AI_CONTINUATION_FALLBACK`
only for a round that actually fell back, one `AI_RESULT` per request.

### 12.5 Tests executed and exact results

| Command | Result |
|---|---|
| `.venv/bin/python -m pytest tests/test_ai_fallback_logging.py -q` | **13 passed** in 0.72 s |
| focused: `test_dynamic_model_fallback.py test_17_providers.py test_23_provider_mesh.py test_24_execution_reliability.py test_41_reset_cooldown.py test_13_model_selection.py test_ai_fallback_logging.py` | **144 passed, 3 warnings** in 13.59 s (exit 0) |
| full suite: `.venv/bin/python -m pytest -q` | **7003 passed, 27 skipped, 1 failed** in 137.07 s |
| `.venv/bin/python -m compileall -q backend tests` | exit 0 |
| `git diff --check` | exit 0 (no whitespace errors) |

The single failure is
`tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`.
It is **pre-existing**: it fails alone in this tree and fails identically in a
clean `git worktree` at the untouched baseline `fa2dfd0` (`1 failed, 10 passed`).
It is unrelated to logging and was not touched.

### 12.6 Fallback behaviour unchanged

The 144-test focused run above is the same provider/fallback suite recorded in
§6 and §12.5 and passes with no assertion changes: ordering, same-model retry,
429 scope, model cooldowns, the one-sibling probe, exhaustion and continuation
behaviour are all identical. Every change in §12.3 is a logging call, a log
format argument, or the level resolution in `main.py`.

### 12.7 Live verification not performed

- **No deployed Render run and no live provider request.** This environment has
  no Render shell access and no service log stream, so whether these lines
  appear in the Render view cannot be claimed — only the next deployed test can
  answer it.
- A viewer-side log filter cannot be excluded from source; if the earlier capture
  was filtered by the reader, that is a viewing artifact this change does not
  alter.
- Which AI path the operator's fallback test used (chat activation vs panel
  Model Tester) is not recorded anywhere; both are now observable.

### 12.8 Remaining limitations

1. `AI_ACTIVATION_SKIPPED` and `MODEL_TEST_RESULT` are DEBUG: at the deployed
   `LOG_LEVEL=INFO` they stay invisible unless the level is raised, which is
   deliberate (one line per outgoing message / per tested model).
2. The root logger deliberately stays at WARNING, so third-party records below
   WARNING remain invisible even at `LOG_LEVEL=DEBUG`.
3. Log lines state transitions; they do not add a request-correlation id where
   one does not already exist (`AI_REQUEST_START`'s `id` is the existing one and
   is reused).
4. Reading these events still requires the Render log view; no panel or endpoint
   renders them.
