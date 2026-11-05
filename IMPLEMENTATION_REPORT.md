# Implementation Report — Persistent Test Modules Results + Explicit Full Refresh

Status: **IMPLEMENTED + UNIT-TESTED** (no live provider verification, no live Telegram verification).

## 1. Objective

The Test Modules diagnostic discovers and probes every eligible chat-capable model of
every configured provider (the owner reports ~615 models). Opening the panel used to
start that live run every single time. The feature now:

1. loads the most recently SAVED diagnostic snapshot when the panel is opened,
2. renders it as cached results (clearly labelled), with the last successful
   full-test timestamp,
3. runs the existing complete diagnostic only for an explicit **Refresh All Models**
   action, and for the very first visit when no snapshot exists.

This is a diagnostic-RESULT cache. It never caches provider responses used by normal
AI inference and it never changes provider selection, fallback, retry or cooldown.

## 2. Source findings (root cause)

- `backend/ai/model_tester.py` produced a rich payload
  (`tested_at`, `partial`, `providers`, `models`, `results`, `summary`) and published
  AVAILABLE results to `ProviderManager.set_model_candidates` — but persisted nothing.
- `backend/bot/handlers/ai.py::_ai_test_models_action` (the "Test Modules" button in
  the AI panel) ALWAYS launched `ai_test_progress.run_streaming_test`, so merely
  opening the panel spent a full provider-catalog diagnostic.
- `_last_test_payload` was a module-level dict — it survived only until restart.
- The panel's pager (`action:ai_test_page`) and "All Results"
  (`action:ai_test_details`) already read that payload, so they needed a
  snapshot-shaped payload rather than a new storage path.
- Durable JSON documents already exist in the EXISTING `bot_settings` table
  (`aed_get_document_state` / `aed_put_document`); no new table, migration or SQL was
  needed.
- `NOT_ATTEMPTED` (upstream) means an imposed overall deadline ended the run before
  a target was tried; `NOT_TESTED` is the cached-reconciliation mark for a catalog
  model with no stored result. Both stay distinct from `NOT_CONFIGURED` and from a
  provider failure.
- Credential changes are read from the environment on every run; there is no
  credential revision/version exposed by the architecture, so no cache
  invalidation by credential revision is implemented (see Known limitations).

## 3. Implementation

### 3.1 New module — `backend/ai/test_snapshot.py`

- `SCHEMA_VERSION = 1`; `SNAPSHOT_KEY = "ai_test_snapshot:v1"`.
- `build_snapshot(payload)` normalizes the runtime payload into a bounded record:
  provider identity (name/display name/icon/status/has_key/validated/default model/
  capability kind), catalog metadata (`id`, `name`, `provider`, `context_length`,
  `is_free`), and one normalized result per `(provider, model_id)` with `status`,
  sanitized bounded `error`, `latency_s`, `http_status`, `retry_after`, `error_type`,
  `provider_code`, `finish_reason`, `is_free`, `tested_at`.
- Redaction uses the tester's existing `sanitize_error_message` (the ONE sanitizer).
  Provider env-var names and base URLs are never stored.
- Bounds: `MAX_RESULTS=1000`, `MAX_HISTORICAL=500`, `MAX_MODELS=4000`,
  `MAX_PROVIDERS=64`, `MAX_ERROR_CHARS=200`, `MAX_DOCUMENT_BYTES=512 KiB`. An
  oversized document is trimmed deterministically with `counts.truncated = True`.
- `load_snapshot()` returns a `SnapshotLoad` whose `state` is exactly one of
  `ok` / `missing` / `malformed` / `unsupported_version` / `unavailable`. A read
  failure is `unavailable`, never `missing`.
- `save_snapshot(snapshot)` performs ONE upsert (atomic) and returns `False` when the
  durable write did not land, leaving the previous complete snapshot untouched.
- `snapshot_to_payload(snapshot, refresh_running=…)` produces the payload shape the
  existing renderers already consume, with `cached: True` on the payload and every
  result, plus `snapshot_last_completed_at`, `snapshot_complete`,
  `catalog_count`, `result_count`, `historical_count` and `historical_results`.
- `format_timestamp()` renders the stored UTC instant (`YYYY-MM-DD HH:MM UTC`).

### 3.2 Storage helpers — `backend/db/client.py`

Two generic wrappers over the EXISTING `bot_settings` document helpers:

- `settings_document_state(key) -> (document, readable)`
- `settings_document_write(key, value) -> bool`

They reuse `_bot_settings_get_state_sync` / `_bot_settings_put_sync` (the same
Supabase-or-in-memory fallback used by every other durable document). No new table,
no migration, no SQL executed, `DATABASE_ARCHITECTURE.md` unchanged.

### 3.3 UI — `backend/bot/handlers/ai.py`

- `action:ai_test_models` = **open Test Modules**. It loads the snapshot and renders
  it; only `missing` (or a snapshot with no results) starts the initial complete
  diagnostic. `malformed` / `unsupported_version` / `unavailable` render an honest
  notice and NEVER start a run.
- New `action:ai_test_refresh` = **Refresh All Models**, the only path that spends
  provider calls from the panel. The existing `_test_running` flag still serialises
  runs, so a second tap renders "Already running" and launches nothing.
- Cached presentation: `Cached snapshot · last full test <ts> · N results ·
  catalog C · complete|partial`; per-result markers `·cached` on usable rows and the
  `(CACHED)` suffix on statuses (`AVAILABLE (CACHED)`, `RATE_LIMITED (CACHED)`, …).
- The results view keeps the existing grid, provider grouping, summary chips and
  pagination, and adds the `↻ Refresh All Models` row plus a `≡ Saved Results` row.
- All Results keeps its upstream self-paging (`_detail_pages`, the message-size fix)
  and adds historical (removed-model) results as a bounded appendix
  (`_TEST_HISTORICAL_LIMIT = 20`). Cached statuses keep their `(CACHED)` suffix on
  every page; action rows are Refresh All Models, Saved Results and the existing page
  navigation.
- New status glyph `NOT_TESTED` uses the registry mark `·`.

### 3.4 Run completion — `backend/bot/handlers/ai_test_progress.py`

After a run, `_persist_test_snapshot(payload)`:

- marks `cached=False`,
- skips persistence for a `partial` run (`snapshot_skipped_partial=True`) so a partial
  or interrupted refresh can never overwrite a complete snapshot,
- otherwise builds and saves the snapshot and reports `snapshot_saved` plus the
  stored `last_completed_at`,
- a failed write is reported as `snapshot_saved=False`; the panel then shows the
  fresh in-memory results with an explicit "Results were not saved" warning.

Cancellation propagates as before (`CancelledError` re-raised), so nothing is written
and the previous snapshot stays authoritative.

### 3.5 Production safety — `backend/ai/model_tester.py`

- `proven_usable_candidates()` now ignores every result carrying `cached: True`, so a
  stored AVAILABLE can never re-enter the runtime model pool on a cache read. Cached
  payloads are never passed to `_feed_production_candidates()` at all.
- `_build_summary()` gains `not_tested` beside the upstream `not_attempted` bucket
  and excludes both from `tested`; `NOT_CONFIGURED`, `NOT_ATTEMPTED` and `TIMEOUT` keep
  their existing, distinct meanings.

## 4. Cache reconciliation and invalidation rules

- Matching is EXACT on `(provider, model_id)`; display names never match.
- The run's catalog is discovery metadata ∪ tested pairs ∪ each provider's
  configured/default model, deduped by exact pair.
- A catalog model with no result is `NOT_TESTED` (never usable, never a failure).
- A result whose pair is not in the catalog is kept as `historical`, shown only in
  All Results with a `historical` marker — never as a currently discovered model.
- Duplicate pairs collapse to the first record.
- A complete refresh replaces the snapshot; a partial run, a cancelled run and a
  failed write leave the previous complete snapshot intact.
- Credential revision invalidation: not available (see Known limitations); the UI
  always labels stored data as cached and shows its timestamp, so old results are
  never presented as live verification.

## 5. Files changed

| File | Change |
|---|---|
| `backend/ai/test_snapshot.py` | NEW — versioned snapshot build/validate/save/load/reconcile/payload |
| `backend/db/client.py` | generic `settings_document_state` / `settings_document_write` |
| `backend/bot/handlers/ai.py` | cached open, explicit refresh action, cached labels, bounded All Results, registration |
| `backend/bot/handlers/ai_test_progress.py` | persist completed runs, partial/write-failure provenance |
| `backend/ai/model_tester.py` | cached results excluded from the production feed; `NOT_TESTED` summary bucket |
| `tests/test_model_tester_snapshot.py` | NEW — 36 focused regression tests |
| `tests/test_11_runtime_wiring.py` | launch test waits for the run (which now includes a DB write) instead of a fixed number of event-loop turns |
| `IMPLEMENTATION_REPORT.md`, `ROADMAP.md`, `INVESTIGATION.md` | replaced with this feature's canonical state |

Remote `main` had advanced while this feature was being implemented (provider-attributed
failure diagnostics, `NOT_ATTEMPTED`, paged All Results, Cloudflare provider). It was
integrated with an ordinary merge commit — no rebase, no force-push, no history
rewriting — and the snapshot layer composes with those changes: cached `(CACHED)`
statuses live inside the paged All Results view, and both summary buckets
(`not_attempted`, `not_tested`) exist side by side.

## 6. Test results (exact)

- Post-merge focused group (`tests/test_model_tester_snapshot.py`,
  `tests/test_model_tester.py`, `tests/test_test_modules_grid.py`,
  `tests/test_test_modules_details_callback.py`, `tests/test_11_runtime_wiring.py`,
  `tests/test_13_model_selection.py`, `tests/test_14_tool_honesty_glass.py`):
  **170 passed**.
- New snapshot suite: **36 passed**.
- Full suite after merging remote `main`
  (`.venv/bin/python -m pytest tests -q`):
  **6951 passed, 27 skipped, 2 failed** in 141.85 s.
  Failures: the pre-existing
  `tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`
  (reproduced on the unchanged baseline, unrelated), and
  `tests/test_private_message_scope.py::test_concurrent_messages_from_one_person_cannot_exceed_the_limit`,
  which passes 3/3 in isolation (load-dependent timing flake, unrelated). Before the
  merge the same suite was **6825 passed, 27 skipped, 1 failed** (136.86 s) — only the
  pre-existing scheduler failure.
- `python -m py_compile` on every changed module and the new test file: exit 0.
- `git diff --check`: clean.

## 7. Not tested / not live-verified

- No live provider was called. The ~615-model catalog was NOT tested, and no claim is
  made that it was.
- No live Telegram client or helper bot was used; panel rendering is verified only
  through the handler output (text + buttons) in unit tests.
- Supabase was not contacted. Durable persistence is verified through the real
  `bot_settings` document code path with a fake store; a real deploy is unverified.
- Credential-change detection and automatic invalidation are not implemented.
- The in-memory fallback (`SUPABASE_URL` unset) cannot survive a process restart —
  that is the pre-existing behaviour of every document in `bot_settings`.

## 8. Known limitations

1. A snapshot can only be as fresh as its `last full test` stamp; the UI states that
   fact on every cached view.
2. `MAX_DOCUMENT_BYTES`/`MAX_RESULTS` truncate an extreme catalog (visible via
   `counts.truncated`, though no UI line surfaces that flag yet).
3. Reopening after a credential change shows the old timestamps until the owner
   presses Refresh All Models.
4. All Results shows at most 60 current + 20 historical rows per view (the usable
   grid paginates every model).
