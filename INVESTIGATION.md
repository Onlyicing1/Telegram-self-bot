# INVESTIGATION — History AI-pass capacity ("89 AI passes vs 45")

Base: `7894f312eab0eaf613d5babddb3efd515527ce12`.
Status: investigation complete; the corrective implementation is described in
`IMPLEMENTATION_REPORT.md`.

## 1. Symptom

The History panel's 1,000-message summary failed instantly (no provider work):

> These 1000 messages need 89 AI passes, more than the 45 one request can run
> while paced to stay under the provider's rate limits. Try fewer messages.

## 2. Verified facts (read from the source at the base commit)

1. `backend/bot/handlers/history.py::_run_summary` calls
   `summarize_history(..., timeout_s=300.0, with_identifiers=False)`; the
   constant is `OPERATION_TIMEOUT_S = 300.0`. The AI-chat path uses
   `ai_unified._AI_EXECUTE_TIMEOUT = 240`.
2. `llm_budget(E) = max(20, E - 20)`; with a reduce pending,
   `map_budget = llm_budget - 30`.
3. `max_map_calls(B) = floor((B - 30) / 5) + 1`. For the panel:
   LLM 280 s, map 250 s, **45**. For AI chat: 220 s, 190 s, 33. Translation has no
   reduce, so its panel bound would be 51.
4. `MAP_CONCURRENCY = 2` and the map loop holds a semaphore slot for the whole
   call; starts are spaced by `call_spacing` (5 s max).
5. `_chunk_history` uses `SUMMARIZE_CHUNK_TOKEN_BUDGET =
   DEFAULT_MAX_CONTEXT_TOKENS = 4000`.
6. `_estimate_tokens` was `max(estimate_tokens(text, "English"),
   estimate_tokens(text, "Persian"))`, i.e. 2 chars/token for every string.
7. A multi-chunk summary makes `len(chunks)` map calls **plus one reduce call**;
   the gate compared `len(chunks)` with the capacity and never added the reduce.
8. `ProviderConfig` has `max_tokens=4096`, `timeout=30`, `retry_count=3` and no
   RPM/RPD/context fields. The only "rate limit" machinery is the 60 s provider
   cooldown in `providers/manager/health.py` and the in-manager retry/fallback.

## 3. Derived numbers for the observed history

- 89 chunks ⇒ 88 full chunks × 4,000 estimated tokens ≈ 352 k estimated tokens
  ⇒ ~352 estimated tokens per message ⇒ ~700 characters per message at
  2 chars/token. Synthetic 700-character histories reproduce the class
  (89–91 chunks for non-Latin text).
- Actual calls: 90 (89 + 1 reduce), not 89.
- With `MAP_CONCURRENCY = 2` and a 30 s planned call: 45 calls ≈ 675 s, far
  beyond the 250 s map budget. Completion capacity is `2 × floor(250/30) = 16`
  (± rounding). 45 starts five seconds apart is a schedule, not a completion
  bound.

## 4. What "45" actually is

A **time-envelope** derivative: `(280 s − 20 s reserve − 30 s final call) / 5 s + 1`.
It is not a provider quota. No code reads a provider RPM/RPD value, and the
providers' documented free-tier limits (e.g. 15 RPM) were a design input for the
5 s spacing, not something the gate computes.

## 5. Assumptions

- The 30 s value is the provider HTTP timeout and a planning latency; real
  summarization calls are probably much faster, but no measured latency is
  stored anywhere the service can read.
- The reduce call is one call; ProviderManager retries/fallbacks inside it are
  not visible as separate History passes, but their wall time is real.
- The observed 89 chunks came from a transcript averaging ~700 characters per
  message with a mix of Latin and non-Latin text.

## 6. Rejected approaches

| Approach | Why rejected |
| --- | --- |
| Raise `MAP_CONCURRENCY` | No measured provider headroom; a 429 triggers the 60 s cooldown and fails the rest of the operation. Forbidden by scope. |
| Remove or shorten the 5 s spacing | Same: it is the rate-limit guard, not the bottleneck. |
| Raise `SUMMARIZE_CHUNK_TOKEN_BUDGET` to 8,000 | No verified minimum context across the dynamic fallback chain (`get_model_context_length` is per model and returns 0 for most models). A fallback to a smaller model would overflow the prompt. |
| Longer panel timeout | `backend/bot/handlers/history.py` is out of scope and a 15-minute Telegram operation is a product decision. |
| Count the reduce by raising the gate to 46 | Still admits work that cannot finish; hides the real defect. |
| Add an executor/queue/worker | Forbidden by scope; ProviderManager owns execution. |
| Retry the whole summary | Duplicates ProviderManager and doubles provider load. |

## 7. What was implemented (summary)

Script-aware token estimation (Latin 4 chars/token, any non-ASCII 2
chars/token); a concurrency-aware capacity function
(`min(pacing, 2 × floor(B/30))`); honest map + reduce pass accounting; a refusal
that names the time budget and the affordable message count instead of a
non-existent provider quota. Chunk budget, concurrency, pacing, deadlines,
ProviderManager ownership and translation behaviour are unchanged.

## 8. Unresolved limitations

1. **1,000 messages remain infeasible** in one request: capacity is 16 (300 s)
   or 12 (240 s); 16 chunks hold ~352 Latin / ~176 non-Latin messages of the
   observed size.
2. No measured latency distribution, so the planning value stays at the HTTP
   bound and may under-admit fast requests.
3. No verified chain-wide context floor, so the 4,000-token chunk cannot grow.
4. The panel still offers 500 and 1,000; both are refused, now with an accurate
   explanation and an actionable "about N fit" number.

## 9. Next validation (not performed here)

- Run one 300-message summary on the deployed bot and record real call latency
  from `ProviderManager.metrics_snapshot`.
- With measured p95, decide between a longer panel envelope (product decision)
  and a verified minimum-context contract across the candidate chain.
- Re-check the panel copy so the offered counts match what one request can
  actually complete.
