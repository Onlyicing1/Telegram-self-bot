# IMPLEMENTATION REPORT — History AI-pass capacity (accurate execution model)

Base commit: `7894f312eab0eaf613d5babddb3efd515527ce12` (`main`).
Delivered commit: the commit that contains this file — verify with
`git log -1 --format=%H -- IMPLEMENTATION_REPORT.md`.

This report replaces the previous pagination report. Pagination itself is unchanged.

## 1. Exact root cause

The 1,000-message History summary was refused before any provider call with:

> These 1000 messages need 89 AI passes, more than the 45 one request can run
> while paced to stay under the provider's rate limits.

Two independent defects produced that sentence:

1. **The pass count was wrong (undercounted).** `_chunk_history` produced 89 map
   chunks; the map-reduce operation then makes **one reduce call** as well, so
   the real number of provider calls was 90. The gate counted only map chunks.
2. **The capacity number was not a completion bound.** `max_map_calls(250)` = 45
   comes from start spacing only: `(250 - 30) / 5 + 1`. It ignores
   `MAP_CONCURRENCY = 2`. With two calls in flight and a planned call duration of
   30 s, the phase can *complete* `2 * floor(250 / 30) = 16` calls inside 250 s —
   45 calls would need ≈ 675 s. The gate therefore admitted workloads it could
   not finish, and reported the 45 as a provider rate-limit quota, although no
   RPM/RPD value is read anywhere in the codebase (the only provider limits are
   the HTTP timeout, retry count and the in-process cooldown).

A third, contributing defect made the situation worse without being the cause:

3. **Token estimation charged every message the Persian worst case.** `_estimate_tokens`
   returned `max(en, fa)` — 2 characters per token for all text, including
   English. A Latin transcript was therefore cut into twice as many chunks as its
   documented 4-chars/token ratio requires.

## 2. Old and new formulas

| Quantity | Old | New |
| --- | --- | --- |
| LLM budget | `max(20, E - 20)` | unchanged |
| Map budget (reduce reserved) | `budget - 30` | unchanged |
| Pacing bound | `max_map_calls(B) = floor((B-30)/5)+1` | unchanged (kept as the pacing half) |
| Concurrency bound | *not modelled* | `MAP_CONCURRENCY * floor(B / 30)` |
| Capacity used by the gate | pacing bound only | `min(pacing, concurrency)`, at least 1 |
| Pass count | `len(chunks)` (reduce ignored) | `chunks + 1` when `chunks > 1` |
| Token estimate | `max(en, fa)` = 2 chars/token always | ASCII → 4 chars/token; any non-ASCII → 2 chars/token |
| Chunk budget | 4000 estimated tokens | unchanged (see §5) |
| Refusal wording | "more than N one request can run … provider's rate limits" | names the AI time budget, the real call count, and how many messages fit |

`ASSUMED_CALL_LATENCY_S = 30` remains a **planning** value (the provider HTTP
bound, already used for pacing). It is not claimed as a worst-case guarantee:
the hard bound stays `call_timeout` (finite, ≤ 120 s, ≤ remaining budget) and
the map-phase deadline. A call slower than planned consumes the budget and fails
honestly instead of overrunning it.

## 3. Numerical comparison — the text the field reported

Synthetic history with the observed shape: 1,000 messages, ~700 characters each
(≈ 700 k characters total), panel path (`with_identifiers=False`, 4,000-token
chunks, 300 s envelope → LLM budget 280 s, map budget 250 s).

| Metric | Before | After |
| --- | --- | --- |
| Capacity reported by the gate | 45 | **16** (pacing 45 ∩ concurrency `2 × 8`) |
| Chunks, English text | 88 | **46** (script-aware estimate) |
| Provider calls, English | 88 (+1 uncounted) | **47** (46 map + 1 reduce) |
| Chunks, Persian/Arabic text | 89 | **91** (same 2-chars/token estimate) |
| Provider calls, Persian | 89 (+1 uncounted) | **92** |
| Outcome, 1,000 messages | refused, wrong reason | **refused, accurate reason** |
| Largest accepted request, English | — | **352 messages** (16 chunks) |
| Largest accepted request, non-Latin | — | **176 messages** (16 chunks) |

AI-chat envelope (240 s → LLM 220 s, map 190 s): pacing 33, capacity **12**;
affordable 264 Latin / 132 non-Latin messages.

**The 1,000-message option is not feasible in one request under the current
envelope, concurrency and 4,000-token chunk policy.** That is arithmetic, not a
missing optimisation: 16 calls × 4,000 estimated tokens = 64 k estimated tokens,
i.e. roughly 350 Latin or 175 Persian messages of the observed size. Making
1,000 feasible needs a longer envelope (≈ 900 s → capacity ≈ 56 → 46 chunks fit)
or a verified larger chunk budget; neither is safe with today's evidence and
both are listed as deferred, not implemented.

## 4. Exact implementation changes

### `backend/ai/prompt/budget.py`

- Added `estimate_tokens_script_aware(text)`:
  - empty → 0;
  - `text.isascii()` → ceiling `len/4` (documented English ratio);
  - any non-ASCII character → ceiling `len/2` for the **whole** string.
  Mixed text is never weighted: one non-Latin character keeps the conservative
  branch, so a mixed chunk cannot be underestimated by combining ratios.
  Accented Latin (`café`) is non-ASCII and therefore conservative.

### `backend/services/history_ai_service.py`

- `_estimate_tokens` now delegates to `estimate_tokens_script_aware`.
- Added `map_call_capacity(budget_s)` = `max(1, min(max_map_calls(B),
  MAP_CONCURRENCY * floor(B / 30)))`, with the concurrency half documented as a
  completion bound, not a start schedule.
- Added `required_ai_calls(chunk_count)`: `0 → 0`, `1 → 1`, `n > 1 → n + 1`.
  ProviderManager retries/fallbacks stay inside one call and are never counted
  as additional History-level passes.
- Added `_affordable_message_count(...)`: binary search over the already-fetched
  history using the **same chunker**, so the refusal can state how many messages
  one request accepts (no extra Telegram read, no provider call).
- Added `_capacity_refusal(...)`: one message builder for summarize and
  translate. It reports required calls (map + reduce), the capacity, the AI time
  budget, the pacing/concurrency settings, and the affordable message count; it
  no longer mentions provider rate limits.
- `summarize_history`: computes `reduce_calls = 1 if len(chunks) > 1 else 0`,
  gates on `map_call_capacity(map_budget)`, and passes both into the refusal.
  The reduce reservation (`map_budget = budget - 30`) is unchanged, and the
  reduce call itself still runs under the operation deadline.
- `translate_history`: same capacity function and refusal builder; translation
  prompts, ids, ordering and output assembly are unchanged.

### Tests

- `tests/test_history_capacity.py` (new, 20 tests): script-aware estimation for
  English / Persian / Arabic / mixed / empty / accented; Latin-vs-non-Latin
  chunk counts; the chunk budget pinned at the policy value and the absence of
  any context-metadata lookup; oversized single message preserved whole;
  map/reduce accounting; reduce reservation; concurrency-aware capacity
  (250 s → 16, never above pacing, degrades to 1); the 1,000-message class
  refused before any provider call with accurate wording; the reported
  affordable count re-run and accepted; an accepted workload making exactly its
  counted calls; provider failure delegated to ProviderManager (one
  `manager.chat` call); translation identity/output unchanged; owner
  instruction present in every system prompt.
- `tests/test_history_ai_tools.py` (4 adjustments, no assertion weakened):
  the 500-message map-reduce test now uses real-sized messages and the longer
  envelope its 20+ calls need; two refusals assert the new, accurate wording
  (and the absence of the false "rate limits" claim); the tighter-envelope
  deadline test uses four chunks, the number 120 s can actually finish.

### Unchanged on purpose

`backend/bot/handlers/history.py` (panel pagination, delivery, `OPERATION_TIMEOUT_S`),
`MAP_CONCURRENCY = 2`, 5 s pacing, `SUMMARIZE_CHUNK_TOKEN_BUDGET = 4000`,
ProviderManager routing/fallback/retry/cooldown, translation prompts and output.

## 5. Context-budget assumptions and the evidence for keeping 4,000

- The only context metadata in the project is
  `backend/ai/model_discovery.py::get_model_context_length(provider, model)`
  (in-memory cache, `0` = unknown). It is **per model**, and
  `ProviderManager`'s candidate chain is private and dynamic: any fallback
  candidate can serve a call.
- There is no chain-wide minimum, no `context_length` on `ProviderConfig`, and
  no configured floor; most discovered models report `0`.
- A chunk sized for the active model's context would therefore be unsafe the
  moment a fallback to a smaller-context model happened — exactly the failure
  the project's safety policy exists to prevent.
- Per the task's own rule ("if there is no reliable way to establish a safe
  larger context budget across fallback candidates, do not blindly increase the
  chunk size"), the budget stays at `DEFAULT_MAX_CONTEXT_TOKENS = 4000`. A test
  pins this and asserts the service does not consult model context metadata.

A single message larger than the chunk budget is still its own chunk, never
split and never truncated (tested); it is bounded only by the model's own limit,
which is the pre-existing behaviour of the summarization path.

## 6. Map/Reduce call accounting

| Situation | Map calls | Reduce calls | Logical provider calls |
| --- | --- | --- | --- |
| No text message | 0 | 0 | 0 |
| 1 chunk | 1 | 0 | 1 |
| n > 1 chunks | n | 1 | n + 1 |
| Provider retry/fallback | — | — | still 1 logical call (inside `manager.chat`) |

Time cost of retries is not pretended to be zero: every attempt runs under the
same phase deadline, and the reduce reservation is one planned call (30 s), not
zero. A pathological multi-candidate fallback can consume the reservation; the
deadline then ends the operation with an honest timeout rather than overrunning
the caller's envelope.

## 7. Concurrency and deadline accounting

- `map_call_capacity` = smaller of the pacing bound and
  `MAP_CONCURRENCY × floor(map_budget / 30)`.
- `_map` still holds a semaphore slot for the whole call and every call is
  `wait_for`-bounded by `call_timeout` (≤ 120 s, ≤ remaining deadline).
- The map phase keeps its own deadline (`map_deadline`), strictly earlier than
  the operation deadline when a reduce follows.
- The reduce call is bounded by the remaining operation budget, not by the
  reservation.
- No deadline, pacing interval or concurrency value was removed or relaxed.

## 8. Tests executed and actual results

Environment: fresh venv, `PYTHONPATH=.`, `pytest -q -p no:cacheprovider`, worktree
at `7894f31` plus this change. No network, no Telegram, scripted provider inside
the real `ProviderManager`.

| Command | Result |
| --- | --- |
| `pytest tests/test_history_capacity.py tests/test_history_ai_tools.py tests/test_history_panel.py tests/test_history_service.py tests/test_17_providers.py tests/test_23_provider_mesh.py tests/test_dynamic_model_fallback.py tests/test_ai_fallback_logging.py` | **277 passed** |
| `pytest tests` (full suite) | **7,618 passed, 6 failed, 27 skipped** in 170 s |
| Same 6 tests on a clean worktree at `7894f31` (no changes) | **6 failed, 46 passed** — identical list |

Pre-existing failures (unrelated: documentation/DB security/scheduler tests,
identical at the base commit):

- `test_aed_db_security.py::test_the_documentation_describes_the_restrictive_model`
- `test_canonical_schema_reconciliation.py::test_the_migration_is_forward_only_and_its_successors_stay_additive`
- `test_database_setup_order.py::test_the_section_does_not_pretend_the_sql_was_executed`
- `test_database_setup_order.py::test_the_waiting_answer_migration_is_absorbed_by_part_one`
- `test_private_message_db_security.py::test_the_rls_model_names_the_two_tables_as_server_only_exceptions`
- `test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`

Compilation: `python -m py_compile backend/services/history_ai_service.py
backend/ai/prompt/budget.py backend/services/history_service.py` — OK.
Whitespace: `git diff --check` — clean.

## 9. What was not verified live

- No deployed bot run: no real Telegram history, no real provider latency
  measurement. Every number above comes from the deterministic model and the
  synthetic 700-character histories.
- Real p50/p95 call latency is unmeasured; `ASSUMED_CALL_LATENCY_S = 30` is the
  provider HTTP bound, deliberately conservative. If production latency is much
  lower, the capacity model under-admits (safe direction); this is the first
  thing to measure before any envelope change.
- The panel's 500/1,000 buttons still exist (handler untouched by scope) and will
  be refused with the new message at the current envelope.

## 10. Remaining limitations

1. **1,000 messages cannot be summarized in one request** at 240–300 s,
   concurrency 2 and 4,000-token chunks. Documented, not worked around.
2. The capacity model is a planning bound, not a guarantee of any individual
   call's latency; the deadline remains the hard stop.
3. No verified cross-fallback context floor, so the chunk budget cannot safely
   grow (see §5).
4. `_affordable_message_count` runs the chunker over a binary search of the
   history only when a request is refused (bounded, no I/O), but it is still
   O(n log n) over up to 1,000 messages.
5. Deferred next steps (not implemented): measure real latencies from
   `ProviderManager.metrics_snapshot`; consider a longer panel envelope with the
   product owner; or introduce a verified minimum context across the candidate
   chain before raising the chunk budget.
