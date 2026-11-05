# Investigation — Persistent Test Modules Results + Explicit Full Refresh

Scope: source-level investigation that shaped this feature. No production system was
queried; every finding below was read from the repository at the implementation commit.

## 1. How Test Modules is opened

- There is no dedicated `panel:*` for Test Modules. It is an ACTION button
  (`action:ai_test_models`) rendered in the AI panels (`_ai_main_panel_handler`, both
  the unconfigured and ready branches) and in the model-pick confirmation view.
- `_ai_test_models_action` used to launch the streaming diagnostic on EVERY tap:
  set `_test_running = True`, `guarded_create_task(run_streaming_test(...))`, return a
  "Starting the model test run" view. Opening the panel therefore always spent a full
  provider-catalog diagnostic — the root cause of this feature.
- A second tap while running rendered "Already running" and started nothing. That flag
  is the existing concurrency guard and is reused unchanged.

## 2. How the batch and streaming runs are produced

- `backend/ai/model_tester.py` exposes two twins: `test_all_models` (batch, used by the
  web endpoint) and `test_all_models_streaming` (Test Modules), both built on
  `_build_targets` + `test_single_model`.
- Both return the same payload keys: `success`, `tested_at`, `partial`, `providers`,
  `models`, `results`, `summary`.
- `_build_targets` selects every chat-capable candidate; the only truncation is the
  explicit `MODEL_TEST_GLOBAL_TEST_BUDGET` (default 60, 0 = uncapped), applied once to
  the flat target list with a fair round-robin. `_MODELS_IN_RESPONSE` (30/provider) is a
  DISPLAY cap only.
- Results are classified deterministically (`AVAILABLE`, `NOT_CONFIGURED`,
  `AUTH_ERROR`, `RATE_LIMITED`, `INSUFFICIENT_CREDITS`, `TIMEOUT`, `INVALID_MODEL`,
  `BLOCKED`, `PROVIDER_ERROR`, `UNKNOWN_ERROR`), then annotated with `is_free` from
  discovery pricing metadata.
- After a run, `_feed_production_candidates` publishes only `AVAILABLE` results through
  `ProviderManager.set_model_candidates`. That is the ONE place a diagnostic result can
  influence production routing.

## 3. How results are rendered and paged

- `ai_test_progress.run_streaming_test` drives a coalescing edit guardian over the
  launching panel, keeps the last payload in the module-level `_last_test_payload`, and
  renders the terminal view through `ai._render_test_results`.
- `_render_test_results` builds: summary chip line, authoritative "Current:" pair,
  provider-grouped usable list, compact failed list (≤8 lines), adaptive 2/3-column
  usable grid (12/18 per page) plus pagination, and the action rows.
- `action:ai_test_page:<n>` (`_ai_test_page_action`) re-renders another page from
  `_last_test_payload` and never re-runs the tester.
- `action:ai_test_details` ("All Results") renders every result from
  `_last_test_payload` — previously unbounded.
- Consequence: the pager and All Results already work from a payload dict, so a
  snapshot only has to produce a payload-shaped dict; no second renderer is needed.

## 4. Durable document persistence available today

- `backend/db/client.py` owns a Supabase-or-in-memory singleton. `bot_settings` is a
  key/value table (`key`, `value`, `value_type`, `updated_at`) already used by
  Ghost Seen and (historically) by Anti-Edit/Anti-Delete and Private Messages.
- `_bot_settings_get_state_sync` returns `(document, readable)` so a caller can tell an
  outage from a missing document; `_bot_settings_put_sync` performs one upsert and
  returns success. Both are wrapped by `aed_get_document_state`/`aed_put_document`,
  which are AED-named but otherwise generic.
- `run_sync_db` dispatches the synchronous PostgREST call to a bounded 4-worker pool
  with a 10s application deadline and an 8s transport deadline, so a document read or
  write cannot block the event loop.
- Decision: add two neutrally named wrappers (`settings_document_state`,
  `settings_document_write`) over the same helpers instead of reusing AED-named public
  functions, and store the snapshot as ONE bounded document. No migration, no new
  table, no SQL — the schema in `DATABASE_ARCHITECTURE.md` is untouched.

## 5. Why one document instead of shards

- The AED archive shards because it grows without bound (one row per observed edit or
  delete). A diagnostic result set is bounded by the provider catalog (today ~615
  models, hard-capped here at 1000 normalized results).
- A single upsert is atomic: a failed write leaves the previous complete snapshot
  intact. A sharded layout would need generation keys or cleanup to avoid a torn
  "new shards + old meta" state, which is more machinery than a bounded payload needs.
- Worst-case size is bounded by `MAX_DOCUMENT_BYTES` (512 KiB) with deterministic
  trimming and a `counts.truncated` marker.

## 6. Cache-correctness questions answered in source

- **AVAILABLE and production fallback.** `proven_usable_candidates` is the only path
  into the runtime model pool. Cached results are never fed (the open path never calls
  `_feed_production_candidates`), and the function now also skips any result carrying
  `cached: True`, so a cache read cannot arm production even if a caller makes a
  mistake.
- **New / removed models.** The run's catalog is the union of discovery metadata,
  tested pairs and provider defaults. A catalog entry with no result is surfaced as
  `NOT_TESTED`; a result whose pair is absent from the catalog is kept as `historical`
  and excluded from the current-catalog list and from the usable grid.
- **Exact identity.** Model ids are used verbatim (no display-name matching); the pair
  `(provider, model_id)` is the identity everywhere.
- **NOT_ATTEMPTED.** No such status exists in the runtime; "never targeted" is the
  absence of a result. `NOT_TESTED` is therefore the honest cached representation, and
  `NOT_CONFIGURED` / `TIMEOUT` keep their existing meanings.
- **Partial runs.** `test_all_models*` sets `partial=True` when the overall timeout
  leaves targets pending and records TIMEOUT for each. The persistence step refuses to
  save a partial payload, so an interrupted refresh cannot masquerade as a complete
  diagnostic; cancellation propagates without writing.
- **Credential changes.** API keys are read from the environment per probe
  (`discovery._get_env`); there is no revision/version token exposed anywhere. No
  fingerprinting was invented (it would be a new credential-derived value persisted in
  a document). The UI instead always stamps cached views with their last-test time and
  never calls them live verification.

## 7. UI constraints discovered

- `AGENTS.md` §16 and `tests/test_symbols.py` forbid emoji in panel labels; glyphs come
  from `backend/helper/symbols.py` via `sym.label`. The requested refresh action is
  therefore `↻ Refresh All Models` (`sym.OP_REFRESH`), not the emoji spelling.
- Existing tests pin `action:ai_test_models` in the AI panel and in the results view,
  and pin `_ai_test_models_action` launching a streaming run when nothing is saved.
  Keeping that id as the OPEN action (cached when possible, initial run when nothing is
  saved) preserves those contracts; the explicit refresh is a NEW
  `action:ai_test_refresh`.
- No message-size truncation exists in the helper layer, so All Results was given an
  explicit row bound instead of relying on an implicit limit.
