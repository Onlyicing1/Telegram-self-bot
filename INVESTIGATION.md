# INVESTIGATION — Model Tester → Candidate Pool → ProviderManager routing

_Base commit: `16071f6a4264fc4d062540f201b099b83e137204` (`main`), the deployed
commit in the production evidence. This is a source-driven investigation of the
production routing failure on that commit; the applied fix and its tests are
described in `IMPLEMENTATION_REPORT.md`._

Labels used throughout:

- **Verified in source** — read directly in the named file/function.
- **Verified by tests** — exercised by a deterministic test in `tests/`.
- **Inference** — a conclusion drawn from verified pieces, not itself observed.
- **Unresolved** — needs the live Render deployment or owner action.

> The prior P0 History & Event Intelligence specification that occupied this
> file is preserved in Git history at `ca0399b2156999a065444f31a31a58bb38841663`
> (`git show ca0399b:INVESTIGATION.md`). `ROADMAP.md` pointed here for it; that
> pointer is corrected there.

## 1. The question

History Summary failed with `AI_CANDIDATES_EXHAUSTED` after three provider/model
attempts, while the Test Modules page had shown models AVAILABLE. The logs
showed `models=1` for Gemini, NaraRouter and OpenAI and `models=0` for Groq,
SambaNova and Cohere.

Two possibilities had to be separated:

1. Test Modules proved only one model per provider, so the router had nothing
   else to try; or
2. Test Modules proved more models, but the production routing run did not have
   them.

The logs alone cannot distinguish these. The source does.

## 2. Candidate lifecycle — verified in source

### 2.1 Where the production candidate list lives

`ProviderManager.__init__` creates one in-memory dict
(`backend/ai/providers/manager/manager.py`):

```python
self._model_candidates: dict[str, list[str]] = {}
```

There is no database read or write in `ProviderManager`, no constructor
argument that seeds it, and no file/env source for it. The pool exists only in
the running process.

### 2.2 How it is populated

The only caller of `ProviderManager.set_model_candidates()` in the repository is
`backend/ai/model_tester.py::_feed_production_candidates()` (verified by
repository-wide reference search). It is called at the end of:

- `test_all_models()` — `backend/ai/model_tester.py:729` area;
- `test_all_models_streaming()` — `backend/ai/model_tester.py:942` area.

No startup path (`backend/main.py`, `backend/runtime/`, `backend/ai/engine/`)
references `set_model_candidates`, `_model_candidates`, or the saved Test
snapshot. `get_engine()` lazily constructs the single `Engine` and
`ProviderManager`; it does not restore a candidate pool.

Consequences, all verified in source:

- Opening the Test Modules page does not change production routing.
- Reading a saved snapshot does not change production routing.
- Only a completed Test Modules run **in that process** arms the pool.
- A process restart (including any Render redeploy or instance recycle) starts
  with an empty pool.
- RuntimeSupervisor startup applies the persisted AI provider configuration
  (`_apply_ai_config_at_boot`), but never publishes any candidate models.

### 2.3 What a Test Modules run publishes

`proven_usable_candidates()` (`model_tester.py`) collects only results whose
`status == "AVAILABLE"`, skips `cached` snapshot results, removes the provider's
configured default model (the router already tries it first), orders free models
first, and keeps every remaining model. There is no per-provider cap.

`_feed_production_candidates()` then, for every chat provider that has a key,
calls `set_model_candidates(name, feed.get(name, []))`. An empty list **clears**
that provider's entry (`set_model_candidates` pops the key). A provider with no
key or a non-chat capability is skipped (its old entry, if any, is untouched).

So a complete run does publish every AVAILABLE model to ProviderManager — but
only in the process that ran it. This is the gap.

### 2.4 Partial runs

Both test entry points accept an overall deadline. When it fires, unattempted
targets are classified `NOT_ATTEMPTED` and the run reports `partial=True`, but
before this fix it still called `_feed_production_candidates()`. That call
replaces each tested provider's list with that run's smaller AVAILABLE set and
clears providers with none. A timeout could therefore shrink a good pool even
inside the process that had one. This is a second, independent defect.

### 2.5 Saved Test snapshot

`backend/ai/test_snapshot.py` persists the last snapshot under the generic
`bot_settings` document key `ai_test_snapshot:v1` and explicitly documents that
cached results are not proof of availability; `proven_usable_candidates()`
ignores cached results. Production routing never loads this document. Storing a
display snapshot and publishing routing candidates are separate acts, and before
this fix the latter was never durable.

## 3. How ProviderManager builds a routing run

`ProviderManager.chat()`:

1. Enumerates registered providers, excluding Dummy
   (`_ordered_candidates(include_all=True)`).
2. Skips a provider for non-chat capability, health/circuit state, unhealthy
   provider, or a required tool capability (`_skip_reason`).
3. For each remaining provider, gets its model list from
   `_candidate_models(provider)`:
   - configured `default_model` first;
   - then every pool entry for that provider, de-duplicated, no cap;
   - then removes pairs currently marked unavailable (`_MODEL_UNAVAILABLE_TTL =
     3600.0`, plus model-level health cooling).
4. Emits `ROUTER_SCORE provider=... models=len(...)`, then sorts all
   `(provider, model)` pairs by capability match, active-provider preference,
   and score.
5. Iterates every pair; one immediate retry is allowed for transient failures
   (`_MAX_RETRIES = 1`, `_RETRY_BACKOFF_SECONDS = 1.0`). A failed pair is cooled
   down/marked unavailable and the loop continues with the next pair. After three
   distinct models of one provider fail within `_PROVIDER_OUTAGE_WINDOW = 60.0`,
   the provider circuit opens (`_escalate_if_provider_wide`).
6. If no pair succeeds, it returns the `AI_CANDIDATES_EXHAUSTED` diagnostic
   (`manager.py:1370` area) with the failure matrix.

The model-level fallback is complete **given the list it receives**. It is not
the defect: there is no hidden truncation, no "first model per provider only"
behavior, and the candidate list is iterated in full. The defect is that the
list was incomplete after a restart.

## 4. Why production showed `models=1` and `models=0`

`_candidate_models()` returns:

- `[configured_default]` plus pool entries, de-duplicated, minus unavailable
  pairs;
- if the configured model itself is currently unavailable and the pool is empty,
  it returns `[]`;
- if there is no configured model and no pool, it returns `[""]` (one blank
  candidate, later resolved by the existing effective-model path).

Therefore, in the observed run:

- **Gemini, NaraRouter, OpenAI `models=1`:** each had its configured default and
  an empty pool. The pool was empty because the process had not run a completed
  Test Modules pass since it started (or a partial pass had cleared it). This is
  consistent with the restart gap above and does **not** prove that Test Modules
  found only one model.
- **Groq, SambaNova, Cohere `models=0`:** these providers passed eligibility
  checks (otherwise they would have logged `PROVIDER_SKIPPED` instead), so they
  had a configured default that was already marked unavailable, and no pool
  candidate to replace it. The log cannot distinguish a permanent
  `model_not_found` from a cooldown; both use the same pair-level filter. With a
  populated pool, any sibling candidate would have stayed eligible.
- **OpenRouter disabled:** excluded by configuration/health, not by candidates.
- **You.com:** its class capability kind is retrieval, not chat
  (`CAPABILITY_KIND`; `_skip_reason` returns `capability=...`), so it is never a
  chat candidate. History Summary's auxiliary call passes `tools=[]`
  (`backend/services/history_ai_service.py`), so it does **not** require web
  search; the provider would be excluded either way.

No capability requirement was weakened: History Summary is a plain completion
and its actual request already has no tool requirement.

## 5. Test Modules vs History Summary consistency

| Aspect | Test Modules | History Summary routing |
| --- | --- | --- |
| Provider registry/config | same `Engine`/`ProviderManager` | same |
| Credentials | same `discovery`/provider config | same, via manager skip reasons |
| Model identifiers | provider catalog + configured models | configured model + fed/proven pool |
| Capability gate | `is_chat_capable`, key presence, test request | `_skip_reason` (chat kind, health, tools) |
| Availability proof | live test request per model | the routing attempt itself |
| Candidate selection | publishes AVAILABLE set | `_candidate_models` merges default + pool |

The only intended difference is availability timing: a test result is proof for
the moment of the test, not a guarantee later. Production must still re-prove
each candidate on use.

## 6. Root cause

**Primary:** Test Modules published its proven candidate set only into an
in-memory `ProviderManager` dict. Nothing persisted it, and RuntimeSupervisor
startup never restored or refreshed it, so every process restart/redeploy left
production routing with at most the configured default model per provider. The
`models=1` log lines and the three-attempt exhaustion are the direct
consequence; the fallback loop was behaving correctly over the list it was
given.

**Secondary:** An incomplete Test Modules run (overall timeout/cancellation)
still published its partial AVAILABLE set, which could replace a larger complete
pool with a smaller one and clear providers not reached by that run.

## 7. Fix contract (implemented)

- Persist the COMPLETE candidate feed as one bounded `bot_settings` JSON
  document (`ai_model_candidates:v1`) — the same generic document entry point as
  the Test snapshot, so no schema change.
- Restore it once at boot from `RuntimeSupervisor`, after the persisted AI
  configuration is applied and before the update run loop starts.
- Treat the document as a candidate list, never as live availability:
  - only a complete run writes it;
  - documents older than 24 hours are refused;
  - malformed/unreadable documents are explicit statuses;
  - only providers registered in the current process are populated;
  - every restored candidate is still re-proven by the normal routing attempt,
    and the existing cooldown/unavailable rules remove failures.
- A partial Test Modules run publishes nothing, in memory or durably.
- ProviderManager remains the only router/fallback authority; no scheduler,
  worker, parallel routing path, schema, SQL, or database architecture change.

## 8. Evidence summary

| Claim | Evidence |
| --- | --- |
| Pool is memory-only | `ProviderManager.__init__`; no DB/file access in the class |
| Only Test Modules feeds it | repository-wide caller search for `set_model_candidates` |
| Startup never restores it | no references in `backend/main.py`, `backend/runtime/`, `backend/ai/engine/` |
| Snapshot is display-only | `test_snapshot.py` contract; cached results excluded by `proven_usable_candidates` |
| No per-provider cap | `_candidate_models` loop; existing 20-model regression test |
| Fallback iterates all pairs | `chat()` loop, retry and cooldown paths; existing fallback tests |
| `models=0` needs an unavailable configured model | `_candidate_models` return paths; blank default yields `[""]`, not `[]` |
| Partial runs used to publish | `test_all_models*` called the feed regardless of `pending` |
| You.com is not a chat provider | `CAPABILITY_KIND` on `YouSearchProvider`; `_skip_reason` |
| History passes no tools | `history_ai_service.py` calls `manager.chat(messages, tools=[])` |

## 9. Unresolved / not verified live

- Whether the actual Render process had run Test Modules since its last restart
  (the source proves the pool could not survive that restart either way).
- The exact prior failure category (404 vs cooldown) behind Groq/SambaNova/Cohere
  `models=0`; the per-pair unavailable map is in-memory and not logged.
- That the deployed Supabase `bot_settings` document accepts the new key; this
  uses the existing generic document API and needs a live check after deploy.
- That a post-deploy History Summary actually falls through multiple restored
  candidates; requires the owner's live request.
