# IMPLEMENTATION REPORT — AI candidate pool survives restart

## 1. Scope and production evidence

Deployed commit: `16071f6a4264fc4d062540f201b099b83e137204` (`main`).

A History Summary request failed with `AI_CANDIDATES_EXHAUSTED` after:

- Gemini / `gemini-3.1-flash-lite` → API 503;
- NaraRouter / `deepseek-v4-flash` → rate limited;
- OpenAI / `gpt-4o` → rate limited.

Routing logs showed `models=1` for Gemini, NaraRouter and OpenAI, `models=0` for
Groq, SambaNova and Cohere, OpenRouter disabled and You.com excluded. The
investigation is in `INVESTIGATION.md`; this report covers what was changed and
what was actually verified.

## 2. Root cause

1. `ProviderManager._model_candidates` is an in-memory dict.
2. It is fed only by `backend/ai/model_tester.py::_feed_production_candidates()`
   at the end of a Test Modules run in the same process.
3. Startup (`RuntimeSupervisor`) applies the persisted provider configuration
   but never restores or refreshes candidate models; the saved Test snapshot is
   explicitly display-only and was never read by routing.
4. Consequently every restart/redeploy starts with an empty pool, so each
   provider contributes only its configured default model. A provider whose
   configured model is already unavailable contributes zero candidates
   (`models=0`). The fallback loop itself iterates every pair it is given and
   was not truncating anything.
5. A second defect: a partial Test Modules run (overall timeout/cancellation)
   still published its incomplete AVAILABLE set, which could shrink a complete
   in-memory pool.

## 3. Implemented changes

### `backend/ai/candidate_pool.py` (new)

Durable, bounded publication of the proven candidate feed:

- one `bot_settings` JSON document at `ai_model_candidates:v1`, through the
  existing generic `settings_document_write` / `settings_document_state` API —
  no new table, column, migration, SQL, or `DATABASE_ARCHITECTURE.md` change;
- schema version, `published_at`, provider→model lists (ordered), bounded to 32
  providers × 256 models × 200 chars, de-duplicated, `dummy` excluded;
- provider entries with an empty list are persisted deliberately so a complete
  run with no AVAILABLE model clears the restored entry;
- refuse documents that are missing, unreadable, malformed, unsupported,
  future-dated beyond clock skew, or older than `MAX_AGE_S = 24 h`;
- `restore(manager)` populates only providers currently registered in the
  process and returns an explicit status (`restored`, `missing`, `stale`,
  `unavailable`, `malformed`, `no_registered_provider`) for boot logging;
- a persistence failure returns `False`; it never breaks the live run, which
  has already been armed in memory.

The document is a candidate list, not an availability guarantee. Restored
candidates are still attempted by the normal router, and a failing pair is
cooled down or marked unavailable by the existing `ProviderManager` rules.

### `backend/ai/model_tester.py`

- `_feed_production_candidates(..., partial=False)` now publishes nothing when
  `partial=True` (logs `CANDIDATE_POOL_SKIPPED reason=partial_run`) and returns
  the exact feed it applied (including empty providers), or `None` when it did
  not publish.
- Both `test_all_models()` and `test_all_models_streaming()` pass
  `partial=bool(pending)` and, only when the feed is not `None`, call
  `candidate_pool.publish(feed)`.
- Complete-run publication to ProviderManager is unchanged (all AVAILABLE,
  free-first, configured model excluded, no cap); only durability and
  partial-run safety were added.

### `backend/runtime/supervisor.py`

- `_build_and_register()` now awaits `_restore_ai_model_candidates()` after
  `_apply_ai_config_at_boot()` and before the update run loop starts, so a
  production request cannot be served before the restore attempt.
- The restore is bounded by `CANDIDATE_RESTORE_TIMEOUT_S = 10 s`; a timeout,
  unreadable document or any exception is logged and never blocks startup.
  RuntimeSupervisor remains the sole lifecycle owner — no new task, loop,
  worker or scheduler is created.

### `tests/test_model_candidate_pool.py` (new, 13 tests)

Covers:

- publish/load round-trip, ordering, empty-provider entries, durable-write
  failure reporting;
- missing vs unreadable vs malformed vs unsupported documents;
- stale document refused and fresh document restores every model in order;
- unregistered providers ignored (no stale references);
- repeated restore does not duplicate candidates;
- a complete feed arms every eligible provider and clears providers with none;
- a partial feed preserves the existing pool and a partial test run publishes
  nothing;
- restored candidates are actually attempted after the configured model fails:
  the configured model's transient retry runs, then the restored sibling model
  succeeds (this exercises the real `ProviderManager.chat()` loop);
- the supervisor boot hook calls `candidate_pool.restore(manager)`.

## 4. Verification actually executed

All commands were run in this workspace with the project's `.venv`
(Python 3.12.3).

| Check | Command | Result |
| --- | --- | --- |
| Focused routing/model tester | `pytest tests/test_model_candidate_pool.py tests/test_dynamic_model_fallback.py tests/test_model_tester.py -q` | **100 passed** |
| History/supervisor/provider-manager/fallback selection | `pytest tests -q --ignore=tests/test_runtime_session_terminal.py -k "history or supervisor or provider_manager or dynamic_model"` | **214 passed** |
| Full suite | `pytest tests -q --ignore=tests/test_runtime_session_terminal.py` | **7584 passed, 6 failed, 27 skipped** (144 s) |
| Compilation | `python -m py_compile backend/ai/candidate_pool.py backend/ai/model_tester.py backend/runtime/supervisor.py tests/test_model_candidate_pool.py` | passed |
| Whitespace/diff | `git diff --check` | passed |

The 6 full-suite failures are the unchanged pre-existing baseline, not
regressions:

- `tests/test_aed_db_security.py::test_the_documentation_describes_the_restrictive_model`
- `tests/test_canonical_schema_reconciliation.py::test_the_migration_is_forward_only_and_its_successors_stay_additive`
- `tests/test_database_setup_order.py::test_the_section_does_not_pretend_the_sql_was_executed`
- `tests/test_database_setup_order.py::test_the_waiting_answer_migration_is_absorbed_by_part_one`
- `tests/test_private_message_db_security.py::test_the_rls_model_names_the_two_tables_as_server_only_exceptions`
- `tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`

`tests/test_runtime_session_terminal.py` was excluded because it is a
pre-existing **untracked** file (present before this task) whose collection
errors; it is unrelated and was not modified or staged.

## 5. Behavioral contract after this change

- A complete Test Modules run still arms the live manager immediately and now
  also persists the exact feed.
- After a restart, RuntimeSupervisor restores that feed once, before updates can
  run, for providers registered in that process.
- A restored candidate is only a candidate: routing re-proves it with a real
  request; a dead pair is cooled down/marked unavailable and the existing
  fallback continues to the next model.
- A feed older than 24 hours is not restored; startup logs the reason and
  routing behaves as before (configured models only).
- A partial/timed-out test run changes neither the live pool nor the durable
  document.

## 6. Remaining limitations and live behavior not verified

- **No Render deployment or live History Summary request was performed.** The
  production path was verified by source tracing and deterministic tests only.
- The actual Supabase `bot_settings` write/read of `ai_model_candidates:v1` was
  not exercised against the live database; it uses the same generic document
  API as the existing Test snapshot.
- Whether the observed Render process had run Test Modules since its last
  restart is not knowable from the logs. The source proves the in-memory pool
  could not survive that restart regardless.
- The exact prior failure category (permanent `model_not_found` vs cooldown)
  for the `models=0` providers is not logged; the pair-level unavailable map is
  in-memory.
- The 24-hour freshness window is a deliberate safety bound, not a live
  availability proof. A model can go down after a fresh test.

## 7. Required owner action after deploy

1. Deploy this commit.
2. Run **Test Modules once to completion** in the deployed process. The old
   process never had durable publication, so the first complete post-deploy run
   is what creates `ai_model_candidates:v1`. Until then, startup restore reports
   `missing` and routing is unchanged.
3. Send a History Summary request and inspect the `ROUTER_SCORE` /
   `AI_FALLBACK_START` / `CANDIDATE_POOL_RESTORE` logs. A restart followed by
   another History Summary must show the restored `models=N` counts without
   re-running Test Modules.
4. If more than 24 hours pass before the next restart, refresh with another
   complete Test Modules run; an older document is intentionally refused.
