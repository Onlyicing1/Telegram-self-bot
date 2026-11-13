# ROADMAP

## Latest delivery: History AI-pass capacity — accurate execution model

Status: **implemented, tested, awaiting live validation.**
Base: `7894f312eab0eaf613d5babddb3efd515527ce12`.

The History panel refused a 1,000-message summary with a sentence that claimed a
provider rate-limit quota (45) and undercounted the work (89 map passes, reduce
ignored). The real constraint is the request's own AI time budget under the hard
5 s pacing and `MAP_CONCURRENCY = 2`. This delivery makes the arithmetic honest:

- script-aware token estimation (Latin 4 chars/token, any non-ASCII 2
  chars/token; mixed text stays conservative);
- capacity = `min(pacing, MAP_CONCURRENCY × floor(map_budget / 30))` — 16 for the
  300 s panel envelope, 12 for the 240 s AI-chat envelope;
- pass count includes the reduce call (n map + 1 merge);
- refusal before any provider call, stating the AI budget, the required calls
  and how many messages one request accepts (~352 Latin / ~176 non-Latin at
  300 s);
- no change to pacing, concurrency, chunk budget, deadlines, ProviderManager or
  translation behaviour.

### Verified results

| Check | Result |
| --- | --- |
| Focused History + provider/fallback suites (8 files) | 277 passed |
| Full suite | 7,618 passed, 6 failed, 27 skipped |
| The same 6 failures on a clean `7894f31` worktree | 6 failed, 46 passed — identical, pre-existing |
| Compile / `git diff --check` | clean |

### Completed

- Root cause established from source: mis-modelled completion capacity + the
  reduce call not counted + a false rate-limit attribution.
- `estimate_tokens_script_aware` in `backend/ai/prompt/budget.py`; the History
  service uses it.
- `map_call_capacity`, `required_ai_calls`, `_affordable_message_count`,
  `_capacity_refusal` in `backend/services/history_ai_service.py`.
- New `tests/test_history_capacity.py` (20 deterministic tests) plus four
  adjusted tests in `tests/test_history_ai_tools.py`.
- Canonical reports replaced (`IMPLEMENTATION_REPORT.md`, this file,
  `INVESTIGATION.md`).

### Deferred (do not implement without new evidence)

1. **1,000-message single request.** Infeasible at 240–300 s with concurrency 2
   and 4,000-token chunks: 16 calls hold ~352 Latin / ~176 non-Latin messages of
   the observed size. Options, both requiring product/architecture decisions:
   a longer panel envelope (≈ 900 s gives capacity ≈ 56, enough for 46 Latin
   chunks) or a verified minimum context across the candidate chain that would
   justify a larger chunk budget.
2. **Larger chunk budget.** `get_model_context_length` is per model, the
   fallback chain is private and dynamic, and most models report `0`. No
   speculative registry, no blind 8,000-token increase.
3. **Latency-based planning.** `ASSUMED_CALL_LATENCY_S = 30` is the provider HTTP
   bound; real latencies are unmeasured. Use `ProviderManager.metrics_snapshot`
   before touching concurrency, pacing or the envelope.
4. **Panel copy.** The 500 and 1,000 options remain and are refused with the new
   message; aligning the offered counts with what one request can finish is
   panel work, out of this delivery's scope.

### Next required validation

- One live 300-message summary on the deployed bot: confirm the accepted
  workload completes, the refusal never fires for it, and record real call
  latency.
- One live refusal (500 or 1,000) to confirm the message renders correctly in
  Telegram and the "About N messages fit" number is truthful for a real
  transcript.

---

## Open items carried over (untouched by this delivery)

These were already open before this work and are **not** part of it:

- Supabase public `SELECT` exposure and the two server-only table exceptions
  (tracked by `test_aed_db_security`, `test_private_message_db_security`).
- Canonical schema reconciliation / migration-history documentation
  (`test_canonical_schema_reconciliation`, `test_database_setup_order`).
- The recovered-interrupted-occurrence scheduler test
  (`test_task_scheduler::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`).

They are the six pre-existing suite failures listed in
`IMPLEMENTATION_REPORT.md` §8 and reproduce unchanged at the base commit.
