# Implementation Report — Dynamic Test-Verified Model Fallback

## 1. Summary

Production fallback is now driven entirely by live Model Tester evidence: every
model whose latest diagnostic result is `AVAILABLE` joins the router's candidate
pool for its provider, with no fixed count and no hardcoded backup list. For one
user request the manager walks that pool strictly one model at a time, returns
the first usable response and never contacts another model afterwards. Failures
are attributed at the scope their own evidence supports — a dead or throttled
model cools down that `(provider, model)` pair, while auth, account quota and
opaque rate limits still take the whole provider down.

## 2. Exact files changed

| File | Change |
|---|---|
| `backend/ai/providers/manager/manager.py` | Model-scoped failure handling, pool filtering of unavailable pairs, per-attempt eligibility re-check, sibling-based escalation, model success reset |
| `backend/ai/providers/manager/health.py` | Per-`(provider, model)` health state + `classify_rate_limit_scope()` |
| `backend/ai/providers/openai_compat.py` | 429 metadata now carries the provider error code/type/message and model (used for scope) |
| `tests/test_dynamic_model_fallback.py` | NEW — 22 regression tests driving the real `ProviderManager.chat()` |
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
| 429 the body does not explain | provider (conservative) | cooldown; sibling models are NOT fired at a possibly shared quota |
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

## 6. Exact tests executed and results

| Command | Result |
|---|---|
| `.venv/bin/python -m pytest tests/test_dynamic_model_fallback.py -q` | **29 passed** |
| focused group: `test_dynamic_model_fallback.py`, `test_23_provider_mesh.py`, `test_24_execution_reliability.py`, `test_17_providers.py`, `test_provider_structured_output.py`, `test_provider_tool_boundary.py`, `test_provider_system_instruction_contract.py`, `test_task_fallback_classification.py`, `test_task_fallback_cooldown.py`, `test_13_model_selection.py` | **158 passed** in 10.52 s |
| full suite: `.venv/bin/python -m pytest tests -q --tb=line -p no:cacheprovider` | **6980 passed, 27 skipped, 2 failed** in 135.04 s |
| `.venv/bin/python -m py_compile` on every changed Python file + the new test | exit 0 |
| `git diff --check` | exit 0 (no whitespace errors) |

The two full-suite failures are unrelated to this change:

1. `tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`
   — pre-existing failure, reproduced on the untouched baseline.
2. `tests/test_private_message_scope.py::test_concurrent_messages_from_one_person_cannot_exceed_the_limit`
   — load-dependent timing flake in an untouched module; it passes 3/3 when run
   in isolation.

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
