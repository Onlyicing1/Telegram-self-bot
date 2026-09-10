# IMPLEMENTATION REPORT — CURRENT STATE

> **This is a CURRENT-STATE document.** It describes the repository as it
> exists at the tip of this phase. If code changes invalidate any section,
> update this document in the same commit.

---

## 1. Implementation metadata

| Item | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Branch | `main` |
| Starting HEAD | `7f28bdbf0a7a3c0878063920a087d8df2915a3c0` (== `origin/main` at phase start, clean tree) |
| Phase | (1) MODEL-level fallback candidate pool for production AI requests; (2) Test Modules redesigned as a compact Taskloom-like Unicode panel with coalesced edits; (3) removal of the hidden 6-model-per-provider test cap; (4) corrective patch: no per-provider production pool cap + Dummy removed from production fallback entirely; **(5) corrective patch: discovery display cap no longer leaks into the production candidate feed** |
| Status | **IMPLEMENTED — full suite green (2083 passed, 24 skipped, 0 failed)** |
| Database impact | **NO DATABASE / SCHEMA CHANGE** |
| Delivery record | see §9 |

---

## 2. Fixed bug — hidden 6-model-per-provider test cap

**The bug:** `backend/ai/model_tester.py` carried
`_MAX_PER_PROVIDER = int(os.getenv("MODEL_TEST_MAX_PER_PROVIDER", "6"))`,
and `_build_targets(...)` applied `candidates[:max_per_provider]`. Discovery
could surface dozens of real chat-capable models, but a complete Test Modules
run silently tested only six per provider — directly conflicting with the
requirement that Test Modules evaluate ALL eligible real/free models that
are actually available and compatible.

**The fix (not just a bigger number):**

- The per-provider cap was REMOVED entirely. Every chat-capable candidate is
  eligible; no hidden truncation remains anywhere in the test path.
- The ONLY truncation is an explicit, documented GLOBAL diagnostic budget:
  `_GLOBAL_TEST_BUDGET = int(os.getenv("MODEL_TEST_GLOBAL_TEST_BUDGET", "60"))`.
  Rationale: discovery may surface hundreds of candidates; a complete run
  must not spend unbounded API calls. `MODEL_TEST_GLOBAL_TEST_BUDGET=0`
  removes the cap entirely — a resource tradeoff chosen explicitly via env,
  never an accident of a per-provider slice.
- The budget is applied FAIRLY: every provider's configured/default model is
  kept first, the remainder is distributed round-robin across providers in
  discovery order, so one large catalog can never push a later provider out
  of a complete run.
- Unchanged bounded resources: per-model timeout (8s), overall run timeout
  (60s), bounded concurrency (`MODEL_TEST_CONCURRENCY`, semaphore), and the
  payload display cap `_MODELS_IN_RESPONSE` (response metadata only — never
  affects which models are tested).

**Regression tests:** `test_test_all_models_covers_full_discovery_and_filters`
(42 candidates tested across 2 providers — impossible under the old cap),
`test_test_all_models_global_budget_is_explicit_not_per_provider` (fair
round-robin distribution, both providers represented),
`test_streaming_twin_covers_full_discovery_and_feeds_candidate_pool` (12 of
12 candidates; old cap would have produced 6).

## 3. Production fallback — model level, bounded, deterministic

`backend/ai/providers/manager/manager.py` (the EXISTING ProviderManager — no
second registry, router, or discovery system):

1. **Pool construction** (`_candidate_models`): for EVERY eligible provider,
   `[configured model] + EVERY discovery-fed candidate model` (free-first,
   deduplicated, configured model never demoted). There is **NO per-provider
   cap** — the complete eligible real/free model set enters the pool
   (corrective patch removed the former `_MODEL_CANDIDATE_LIMIT = 6`).
   Bounded request EXECUTION is a separate, explicit policy: one attempt per
   candidate, one immediate retry for transient failures, per-request RPC
   timeout — never a hidden truncation of the pool before the policy runs.
2. **Feeding** (`set_model_candidates`): the Test Modules discovery pass
   feeds the pool — one discovery pass serves both diagnostics and the
   production router (Test Modules bug fixed here too: the feed previously
   passed raw dicts to a `ModelInfo`-typed selector and silently died; it
   now rebuilds the dataclass). Runtime-only; rebuilt on the next pass.
3. **Routing**: eligible pool is `(score, provider, model)` tuples —
   MODEL level. Deterministic order:
   `needs_json capability group → active-first → score`. The active
   provider/model stays first when eligible. A dead (provider, model) pair
   fails over to the next MODEL candidate.
4. **Model-unavailable marking**: 404 `model_not_found` marks the
   `(provider, model)` pair unavailable for a 1h TTL (including the
   `requested_model` override from the pool); `_skip_reason` reports it in
   the matrix.
5. **Model override plumbing**: `model` kwarg forwarded through
   `_call_once`/`_attempt_with_retry` to the adapters (explicit payloads;
   nothing leaks into HTTP bodies). `requested_model` stamped in metadata.
6. **Preserved semantics**: active-first; health/cooldown/quarantine; ONE
   bounded immediate retry per candidate for transient failures; JSON
   capability demotion; structured-contract failover (malformed JSON from a
   transport-success response is provider-fault failover — no cooldown,
   quality-penalized, and the LAST candidate's response is returned
   unchanged so the caller's fail-closed parser owns final classification);
   deterministic/user/repository/Telegram/security failures never trigger
   content failover (the validator only sees parseability; permanent
   config/request errors advance deterministically instead).**Dummy exclusion (ABSOLUTE — corrective patch, regression-tested):** the
   Dummy provider is removed from the production fallback path ENTIRELY.
   `_ordered_candidates()` never returns it (even when registered or active),
   `ProviderManager.chat()` never selects it (even as the active name), the
   terminal "no real provider configured" case returns a manager-built
   honest failure (`reason=no_provider_configured`), and `_fallback()` — the
   exhaustion path — builds its own `success=False` response with the
   preserved failure matrix, errors, and retry count, invoking NO provider.
   `vision`/`stream` crash paths likewise return manager-built honest
   failures (`provider_name=""`). Dummy remains ONLY as a development/test
   double (`backend/ai/providers/dummy/provider.py`). Tests:
   `test_dummy_never_invoked_even_when_registered_and_active` (active+dummy
   registered, zero Dummy calls),
   `test_dummy_only_registry_returns_manager_built_not_configured`,
   `test_dummy_never_enters_production_candidate_pool`,
   `test_dummy_only_terminal_when_all_real_candidates_exhausted`
   (honest exhaustion: `provider_name == ""`, matrix + retry count intact,
   `ai_retry_count == 2` preserved).

## 3A. Corrective patch — discovery display cap no longer leaks into production

**The bug (source-verified on `origin/main`):** `_build_targets(...)` built
`discovered_models` from `models[:_MODELS_IN_RESPONSE]` (30 per provider),
and `test_all_models_streaming` converted THAT capped list into `ModelInfo`
objects for `ProviderManager.set_model_candidates(...)`. A fully
discovered, eligible model positioned past index 29 could never reach the
production fallback pool — the display cap silently determined production
eligibility.

**The fix — three independent data flows from ONE discovery result:**

```
complete provider discovery
  ├──→ COMPLETE chat-capable ModelInfo list per provider
  │      (complete_models — the ONLY source for set_model_candidates)
  ├──→ explicit global diagnostic execution budget
  │      (MODEL_TEST_GLOBAL_TEST_BUDGET — bounds Test Modules targets only)
  └──→ _MODELS_IN_RESPONSE display cap
         (response payload metadata only — never read by the feed)
```

- `_build_targets` now returns `(targets, discovered_models, complete_models)`;
  `complete_models` is the deduped, chat-capable-filtered FULL discovery list
  per provider (real `ModelInfo` objects — no dict→dataclass rebuild needed).
- `test_all_models_streaming` feeds `complete_models` into
  `set_model_candidates`; `_MODELS_IN_RESPONSE` remains ONLY in the response
  payload path. `test_all_models` (batch/dashboard) ignores the complete
  list — dashboard display semantics unchanged.
- `MODEL_TEST_GLOBAL_TEST_BUDGET` is untouched and still bounds diagnostic
  TEST EXECUTION only; it never touches the production feed.
- No new hard-coded model count; no second discovery request; no other
  truncation exists on the discovery→pool path (web `/api/ai/models`
  `[:30]` is a display endpoint that never feeds the pool; the model
  picker paginates its own display fetch).

**Regression tests:** `test_production_feed_exceeds_display_cap_and_payload_stays_bounded`
(50 models → all 50 fed, payload ≤ 30), `test_global_budget_limits_test_execution_not_production_feed`
(budget 5 → 5 tested, 50 fed), `test_multiple_providers_each_feed_complete_sets`
(two providers × 40 models → both feeds complete), `test_late_discovered_model_selected_by_production_fallback`
(40-model pool, first 31 fail → the model at index 31 actually serves the
request through the real `ProviderManager`).

## 4. Test Modules UX — compact Unicode panel

Vocabulary (no colorful emoji anywhere in the Test Modules surfaces):

| Mark | Meaning |
|---|---|
| ▰ / ▱ | filled / empty progress segment (exactly FIVE segments, 20 pp each) |
| ✓ | success |
| × | failure |
| … | testing / timeout |
| ◇ | provider/model line |
| ★ | selected |
| ◌ / · | running / not-configured |

- Launch (`action:ai_test_models`): non-blocking. Sets `_test_running`,
  starts ONE guarded background run, returns a short "… Starting" view.
- Progress view: `▰▰▰▱▱  n/total` + the latest completed `◇ provider · model ✓/×`
  line. Content-free and compact — details stay behind the existing
  results/details navigation.
- Final view: the canonical `_render_test_results` body (shared verbatim by
  the batch path — the two entry points cannot drift), now Unicode-only.
  All previous buttons preserved (pick-model, Re-run, All Results,
  Pick Model, Overview, nav).

## 5. Progress edit guardian (`_PanelEditGuardian`)

Focused, Test-Modules-only coalescing layer (no generic Telegram framework):

- **Coalescing**: submissions are state updates; a single serialized chain
  task renders at most ONE progress edit per 12s window, always the NEWEST
  submitted state (a newer submission cancels the pending window wait —
  never stale). The FIRST progress edit renders immediately (epoch-0
  timestamp) for fast feedback after the launch view.
- **No duplicate renders**: identical (text + button-label) renders are
  skipped against the last state actually applied by Telegram.
- **No edit storms**: edits are strictly serialized; responses can never
  overtake requests.
- **Terminal state is never lost**: the final render bypasses the progress
  window, is applied exactly once (dedupe-guarded), and on Telegram errors
  retries a bounded 2 times with backoff before logging an error. A
  crashing tester still renders the `× Test run failed` terminal view.
- **No stranded lock**: `guardian.done` is set when the terminal render
  settles OR when the chain task dies (done-callback), so awaiting callers
  can never hang; `_test_running` is cleared in `finally` on success,
  failure, cancellation, and unexpected exception alike.

## 6. Concurrent-run protection

The module-level `_test_running` flag spans the WHOLE background run (set by
the handler before the task starts; cleared in `run_streaming_test`'s
`finally`):

1. First tap starts exactly one run.
2. A second tap while running does NOT start another run —
   `test_ai_test_models_second_tap_while_running_is_rejected` proves the
   tester is never invoked twice (`assert_not_called`).
3. The second tap renders the existing "Already running" panel.
4. The flag clears on success, failure, cancellation, and unexpected
   exception (`finally`; proven by
   `test_streaming_tester_error_path_renders_terminal_failure`).
5. A failed run cannot leave the module locked.
6. Callback handlers run on the single asyncio loop; the flag is set and
   checked synchronously inside the handler before any await, so two
   callbacks cannot interleave between check and set.
7. Guardian death cannot strand the flag (done-callback + `finally`).

## 7. Pagination & capability/free-model eligibility

- **Pagination**: the model selector already paginates (`_MODEL_PAGE_SIZE`,
  two-column grid, index+hash callbacks, clamped pages) — now regression-
  tested with 60 candidates:
  `test_model_selector_paginates_large_candidate_sets`.
- **Free eligibility**: `is_free` is metadata-driven ONLY (`_is_free_pricing`:
  both prompt and completion priced $0; a "free" name never sets it). The
  pool feeds candidates through `order_models_for_selector` (free-first,
  alphabetical within groups, graceful degradation without metadata). No
  hard-coded free-model list exists anywhere.
- **Capability filtering**: candidates pass `capability_kind == "chat"`,
  health/availability, tool support when tools are required, and JSON
  capability is routed capability-first when structured output is required
  (demotion, not hard skip). A model that cannot satisfy the requested
  contract is outranked; a transport-success but contract-violating output
  fails over per §3.

## 8. Tests and verification

- **Corrective-patch tests (this phase)**: `test_candidate_models_complete_and_ordered`
  (40 discovery models all enter the pool — old cap would truncate at 6),
  `test_production_pool_not_truncated_to_six_models` (ten candidates all
  eligible, bounded execution intact: 404s attempted once each, live model
  served second), `test_multiple_providers_contribute_complete_model_sets`
  (two providers contribute complete sets; deterministic matrix order;
  model-level failover across providers), `test_active_configured_model_stays_first_in_complete_pool`,
  `test_dummy_never_invoked_even_when_registered_and_active`,
  `test_dummy_only_registry_returns_manager_built_not_configured`;
  updated Dummy-contract tests (test_02, test_06, test_11, test_34, test_52,
  test_task_nl_creation).
- **Discovery-feed corrective tests (this phase)**:
  `test_production_feed_exceeds_display_cap_and_payload_stays_bounded`,
  `test_global_budget_limits_test_execution_not_production_feed`,
  `test_multiple_providers_each_feed_complete_sets`,
  `test_late_discovered_model_selected_by_production_fallback`.
- **Focused suites** (provider mesh, model tester, structured output,
  runtime wiring, ai flow, failure simulation, tool calls, model UI,
  you-search, NL creation): **all green**.
- **Full suite**: **2083 passed, 24 skipped, 0 failed**.
- `python -m py_compile` on all changed Python files — OK.
- `git diff --check` — clean.
- **Live Telegram verification: NOT performed** (no credentials in this
  workspace; this phase is source-verified and test-verified only). The
  next live Test Modules run arms the production pool; the exact Persian
  bio-task request remains the end-to-end probe for structured-output
  failover.

## 9. Delivery record

| Item | Value |
|---|---|
| Starting HEAD (feed-fix patch) | `f0220d5e388ed2470800ed021fc1ffb60a741d64` (== origin/main at start) |
| Feed-fix commit | `c96c8bd4f2b69306aed5b085ed6ba43280e4bcad` — `fix: keep production candidate feed complete independent of display cap` (4 files, 312+/34−) |
| Push result | `f0220d5..c96c8bd  main -> main` (exit 0) |
| Remote HEAD verification | `git fetch origin` + `git rev-parse origin/main` == `c96c8bd4f2b69306aed5b085ed6ba43280e4bcad` == local HEAD; `git show --stat origin/main` contains exactly the 4 feed-fix files |
| Working tree | clean after commit (pre-existing untracked stray clone `telegram-self-bot/` deliberately left untouched — unrelated to this phase) |
| Live Telegram verification | **NOT performed** (no credentials in this workspace) — source- and test-verified only; the exact Persian bio-task request remains the end-to-end probe |

---
