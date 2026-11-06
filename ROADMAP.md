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
| **Continuation diagnostic**: a continuation failure classified `request`/`tool_call` probes that exact `(provider, model)` with a synthetic assistant-tool-call + tool-response conversation | IMPLEMENTED + TESTED | `continuation_diagnostic.py`, hook in `dispatcher.py` continuation round |
| Probe uses the same provider config/credential path as production (a separate provider instance from the live config) | IMPLEMENTED + TESTED | `_probe_instance` → `ProviderFactory.create_provider` |
| Probe never runs a tool, reads owner/Telegram content, or writes history | IMPLEMENTED + TESTED | fixed synthetic messages, `ToolExecutor` untouched |
| Probe cannot alter provider health, cooldown, quarantine, metrics or candidate eligibility | IMPLEMENTED + TESTED | separate instance, no manager call |
| ONE probe per `(provider, model)` per 30 min, single-flight, 12 s timeout, never on the response path | IMPLEMENTED + TESTED | monotonic cooldown, `guarded_create_task` |
| Classification: `CONTINUATION_ACCEPTED` / `CONTINUATION_REJECTED` / `AUTH_ERROR` / `RATE_LIMITED` / `MODEL_NOT_FOUND` / `TIMEOUT` / `UNSUPPORTED` / `UNKNOWN` | IMPLEMENTED + TESTED | `classify_probe_response` |
| `PROVIDER_CONTINUATION_DIAGNOSTIC` event (id, provider, model, result, category, latency, cooldown/skipped reason) | IMPLEMENTED + TESTED | `continuation_diagnostic._log_event` |
| Probe result influences routing / auto-repair | LIMITATION (deliberately not done) | diagnostic only |
| Live confirmation against real Gemini/OpenAI/Groq continuation endpoints | LIMITATION (no live provider here) | — |

## Continuation diagnostic — trigger, safety, tests

**Trigger.** Only a multi-turn continuation round that the router itself
recorded as `failed` with category `request` or `tool_call` (the pair and
category come from the manager's own failure matrix). `auth`, `rate_limited`,
`quota`, `model_not_found`, `timeout`, `network` and `server` never probe — the
classification is already meaningful and an extra call would only spend quota.

**Safety.** Synthetic fixed conversation only (no owner text, tool arguments,
tool results, Telegram ids or history); no registered tool and never the
`ToolExecutor`; a separate provider instance built from the live provider's own
config (same credential/endpoint, never the production HTTP client); no write
to health, cooldowns, quarantine, metrics or the candidate pool; one probe per
pair per 30 min, single-flight, 12 s strict timeout, scheduled with the
existing `guarded_create_task` — no new scheduler, worker or loop, and the
user's response path never awaits it.

**Tests.** `tests/test_continuation_diagnostic.py` — 26 focused tests covering
the trigger, cooldown, independent pairs, in-flight suppression, the
no-probe categories, accepted/rejected/classification matrix, timeout,
multiple tool responses preserving the adapter contract, no tool execution, no
private content, probe-failure isolation, unchanged health/metrics, and
secret-free logging.

**Status.** IMPLEMENTED + TESTED in this tree; NOT LIVE-VERIFIED (no deployed
run and no real provider request). Source inspection independently identified
the Gemini continuation defect (`gemini.py` emitted one `role: "model"` Content
per tool result instead of ONE `user` Content per call batch, in call order) —
the strongest source-backed candidate for the observed Gemini `request`
failure. That defect was repaired upstream in `0d07dc1` before this branch
merged, so the probe is now the live way to confirm the repair holds for each
`(provider, model)` pair.

## Remaining work

1. Live-verify the 429 scope classifier against real OpenAI/Groq/Gemini/Cloudflare
   responses (each vendor's current error body).
2. Optionally surface per-model cooldowns in the AI diagnostics panel so an
   operator can see why a tested model is temporarily absent from the pool.
3. Re-run the Model Tester on a schedule if the AVAILABLE pool is expected to
   track provider catalog changes — the pool is only as fresh as the last run
   (a cached snapshot never arms it).
4. Read `PROVIDER_CONTINUATION_DIAGNOSTIC` on the next real continuation
   failure to confirm the `0d07dc1` Gemini repair holds live; a
   `CONTINUATION_REJECTED` for any OTHER `(provider, model)` pair is the new
   evidence that pair's serialization or capability is the defect.
5. Optionally surface the latest per-pair diagnostic result in the AI
   diagnostics panel so it does not require reading Render logs.
