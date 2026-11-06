# Roadmap — Dynamic Test-Verified Model Fallback

Status legend: **IMPLEMENTED + TESTED** = code in this tree with regression
coverage; **IMPLEMENTED, NOT LIVE-VERIFIED** = code exists, no real provider was
exercised; **LIMITATION** = deliberately not done / not provable here.

| Capability | Status | Where |
|---|---|---|
| Production pool sourced from Model Tester `AVAILABLE` results, every model, no cap | IMPLEMENTED + TESTED | `model_tester.proven_usable_candidates` → `ProviderManager.set_model_candidates` → `_candidate_models` |
| Configured user-selected model remains the first candidate | IMPLEMENTED + TESTED | `_candidate_models`, `_build_targets` order |
| Deterministic candidate order preserved (free-first selector, then stable leftovers) | IMPLEMENTED + TESTED | `proven_usable_candidates`, `_candidate_models` |
| Cached `AVAILABLE` results can never arm production | IMPLEMENTED + TESTED | `proven_usable_candidates` skips `cached`; snapshot payload never fed |
| Failed / untested / `NOT_ATTEMPTED` / `NOT_CONFIGURED` / discovery-only models excluded | IMPLEMENTED + TESTED | `proven_usable_candidates`, `_feed_production_candidates` |
| Strictly sequential fallback, one model at a time, first usable response wins | IMPLEMENTED + TESTED | `ProviderManager.chat` loop (no gather/tasks) |
| Same original request sent to each sequential candidate | IMPLEMENTED + TESTED | `chat()` passes `messages` unchanged |
| Bounded retry (one immediate retry per candidate) and request timeout preserved | IMPLEMENTED + TESTED | `_attempt_with_retry`, `_PROVIDER_RPC_TIMEOUT` |
| Model-level failure tracked per exact `(provider, model_id)` | IMPLEMENTED + TESTED | `ProviderHealthTracker.record_model_failure` |
| A model failure no longer disables sibling models of the same provider | IMPLEMENTED + TESTED | `_apply_failure`, `_skip_reason`, `_candidate_models` |
| Provider/account failures still take the provider down (auth, quota, opaque 429) | IMPLEMENTED + TESTED | `_apply_failure`, `classify_rate_limit_scope` |
| Provider circuit breaker retained for genuinely provider-wide outages | IMPLEMENTED + TESTED | `_escalate_if_provider_wide` (3 distinct models / 60 s) |
| Unavailable/cooling `(provider, model)` pairs removed from the pool | IMPLEMENTED + TESTED | `_candidate_models`, `_model_unavailable` |
| Mid-request eligibility re-check (no blind requests after a provider cools) | IMPLEMENTED + TESTED | `chat()` per-attempt check → `outcome: skipped` matrix entries |
| Rate-limit scope from the provider's own body (model / provider / unknown) | IMPLEMENTED, NOT LIVE-VERIFIED | `classify_rate_limit_scope`, `openai_compat` 429 metadata |
| Gemini/Cloudflare 429 scope | IMPLEMENTED for preserved error text; scope usually `unknown` → provider-wide | `gemini.py`, `cloudflare.py` |
| Honest exhaustion response with failure matrix and retry count | IMPLEMENTED + TESTED | `_fallback` |
| Tool-call and structured-output contracts across fallback | IMPLEMENTED + TESTED | `_has_no_usable_output`, `output_validator` path |
| Persisted model health across restarts | LIMITATION | in-memory, same as provider health |
| Model-level health in the Glass/Web UI | LIMITATION (not requested) | — |
| Live provider reliability of the 600+ model catalog | LIMITATION (untestable here) | — |

## Remaining work

1. Live-verify the 429 scope classifier against real OpenAI/Groq/Gemini/Cloudflare
   responses (each vendor's current error body).
2. Optionally surface per-model cooldowns in the AI diagnostics panel so an
   operator can see why a tested model is temporarily absent from the pool.
3. Re-run the Model Tester on a schedule if the AVAILABLE pool is expected to
   track provider catalog changes — the pool is only as fresh as the last run
   (a cached snapshot never arms it).
