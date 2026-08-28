# Implementation Report — Ghost Seen v2 AI Reply Execution Hardening

## Task objective

Apply the approved Ghost Seen v2 production-hardening changes to the
existing Stage 6 AI Reply flow and deliver them to GitHub. The user-facing
flow is unchanged:

`AI Reply → Context (1/5/10/20) → Disclosure (Yes/No) → automatic generation → automatic delivery`

No prompt input, prompt preview, provider/model selector, Send button, or
extra confirmation step was added. The changes harden the execution
boundary between Context/Disclosure → Engine → Dispatcher → ProviderManager
→ validated AI result → Telegram `send_reply`, and add sender identity to
the private-chat viewer.

## Repository / branch

- Repository: https://github.com/Onlyicing1/Telegram-self-bot
- Branch: `main` (local `main` tracked against `origin/main`)

## Files changed

Implementation (6 files, all pre-existing worktree changes from the
approved Ghost Seen v2 execution):

- `backend/bot/handlers/ghost_seen_v2.py` — hardened `_run_ai_reply`.
- `backend/services/ghost_seen_v2.py` — sender identity in the viewer.
- `backend/ai/engine/dispatcher.py` — per-provider-call execution
  diagnostics + failure-type normalization.
- `backend/ai/diagnostics.py` — correlated request-facts store
  (bounded) + `ai_last_request` snapshot surface.
- `tests/test_60_ghost_seen_v2_nav_search_perf.py` — removed the obsolete
  "no AI paths" regression (the AI Reply path is now a real, tested path).
- `tests/test_63_ghost_seen_v2_stage8.py` — Stage 8 regression coverage.

Documentation (1 file):

- `IMPLEMENTATION_REPORT.md` — replaced with this report.

`INVESTIGATION.md` was restored from HEAD (it had been deleted in the
working tree); it is byte-identical to the pushed Phase 1 + Phase 2 content
and is not part of the commit diff.

## What was implemented

### 1. Bounded AI generation timeout with cancellation containment

`backend/bot/handlers/ghost_seen_v2.py → _run_ai_reply`

- The Engine `execute` call runs as a shielded task under the existing
  `_AI_TIMEOUT_S = 45.0` bound (`asyncio.wait_for(asyncio.shield(task), ...)`).
- On timeout the engine task is cancelled and given a bounded
  `_AI_CANCEL_GRACE_S = 0.1` grace window; a `_consume_late_engine_task`
  done-callback drains any cancellation-resistant late result so it can
  never reach `send_reply`.
- No Telegram delivery can occur after the timeout: the delivery path is
  only reached inside the same bounded execution, and the state is cleared
  exactly once on the timeout path.

### 2. Duplicate-execution isolation preserved

- The existing per-chat `_ai_states` + `_ai_locks` architecture is
  retained (no parallel state system added). The Stage 8 regression
  `test_duplicate_rejection_does_not_clear_active_operation` proves a
  duplicate callback cannot consume or invalidate the state of an
  already-running execution.

### 3. Provider result validation before delivery

Distinct, honest failure classifications (recorded in request facts and
returned to the user):

- engine result `success=False` → `engine_result_failure`
- non-string response → `invalid_response_type`
- empty/whitespace-only response → `empty_response`
- reply over the `_TELEGRAM_TEXT_LIMIT` (4096) → `response_oversized`
  (no silent truncation)

Delivery success is only reported when `send_reply` actually succeeds;
generation success and delivery success are tracked as separate facts
(`delivery_reached`, `delivery_succeeded`, `final_failure_reason`).

### 4. Honest failure/fallback behavior

- Provider/Engine failures never produce a fake successful reply; the
  failure reason is propagated into the request facts and the user is
  told "✕ Couldn't generate the reply." / "✕ Couldn't send the reply."
  as appropriate.
- No Ghost Seen-specific provider fallback was added — the existing
  Engine → Dispatcher → ProviderManager fallback mesh remains the single
  provider path (`_provider_chat` instruments that existing path only).
- The AI request still executes with `allow_tools=False` (tool calls are
  disabled for Ghost Seen AI Reply; the general owner AI path retains tool
  access).

### 5. State cleanup on every terminal path

`_clear_ai_state` runs on: success, generation failure, delivery failure,
invalid/stale selection, timeout, cancellation, and the unhandled-exception
path. Stale selections cannot leak into a later Ghost Seen session.

### 6. Race-condition revalidation before delivery

Before `send_reply` the handler revalidates that the source chat is still
allowed, the reply target is still the original selected real Telegram
message ID, and the selection was not changed mid-generation
(`selection_changed_before_delivery`).

### 7. Structured diagnostics (existing infrastructure reused)

`backend/ai/diagnostics.py` gains a bounded (32-entry) correlated
request-facts store fed by `register_start(details=...)` /
`update_request(...)` / `set_stage(...)`; `snapshot()` exposes
`ai_last_request`. `backend/ai/engine/dispatcher.py` records per-provider-
call facts through the existing `_provider_manager.chat` path (start /
complete / failure / cancelled, elapsed, call count, failure type,
fallback used/exhausted, provider matrix size) and enriches the final
result metadata (`provider_call_count`, `provider_elapsed_s`,
`provider_failure_type`, `fallback_exhausted`, `provider_matrix_size`).
The handler correlates request ID, source chat, selected message ID,
context count, disclosure, provider/model, stage, provider timing, timeout
occurrence, cancellation state, engine result status/response length,
delivery reached/succeeded, and final failure reason. No message content,
credentials, session strings, or API keys are logged or recorded.

### 8. Sender identity in the private-chat viewer

`backend/services/ghost_seen_v2.py` adds `ViewerMessage.outgoing` (via
Telethon's direction bit `_message_is_outgoing`) and renders each line as
`You (outgoing): …` vs `{name} (incoming): …` so incoming vs outgoing
messages are unambiguous.

## Tests executed

- Ghost Seen v2 suite (`tests/test_52…test_64`): **175 passed** in 0.55s.
- Full repository test suite (`tests/`): **981 passed, 23 skipped** in
  31.02s. The 23 skips are pre-existing (legacy `ghost_seen_service`
  tests), unrelated to this change.
- `compileall` over `backend/` + `tests/`: clean (exit 0).
- `git diff --check`: PASS (no whitespace errors).

Test results are real — reported from the actual runs above. No frontend
(TypeScript) files changed, so TypeScript validation was not applicable.

## Final implementation state

All six implementation files are staged in one commit with this report.
`INVESTIGATION.md` is restored and byte-identical to the pushed Phase 1 +
Phase 2 handoff (no diff). Security boundaries preserved: `allow_tools=False`
for Ghost Seen AI Reply, owner-only access, no new Telegram RPC or SQL
execution surface, no credentials in logs or docs.

## Commit / delivery

- Implementation commit: `3faac6a88b819e28b6bea48aef94f223b1fc9123` —
  `fix: harden ghost seen v2 ai reply execution boundary` (7 files,
  +772/−276).
- This report is carried by a small follow-up metadata commit that fills
  in the verified delivery facts below (same pattern as the database
  architecture delivery).

---

## Delivery verification

- Commit: `3faac6a88b819e28b6bea48aef94f223b1fc9123` —
  `fix: harden ghost seen v2 ai reply execution boundary`.
- Push result: pushed to `origin/main` (`b160adc..3faac6a`), exit 0.
- Remote verification: after `git fetch origin main`, `local HEAD ==
  origin/main == git ls-remote origin HEAD == 3faac6a…`; `git show
  origin/main:IMPLEMENTATION_REPORT.md` confirms the pushed report is
  this document.
- Final working-tree state: `main` in sync with `origin/main`; all 6
  implementation files and this report are committed; `INVESTIGATION.md`
  is restored to the pushed Phase 1 + Phase 2 handoff (no diff); no
  unrelated files were touched.

---

# Delivery — Phase 3 P0 Schema Reconciliation Migrations

**Date:** 2026-08-27 · **Base commit:** `470769c` (`docs: reset
investigation to phase 3 decision-queue verification`) · **Requirements
source:** `INVESTIGATION.md` Phase 3, decision-queue items 1, 2, 4, 5.

## Files changed

- `supabase/migrations/20260827000001_add_missing_panel_settings_columns.sql`
  (new) — adds the 10 missing typed `panel_settings` columns with types
  and defaults from `settings_service._DEFAULTS`, CHECK constraints
  mirroring `settings_service._VALIDATORS`, and ensures the singleton
  `key = 'global'` row exists.
- `supabase/migrations/20260827000002_add_ai_config_trigger_columns.sql`
  (new) — adds nullable `trigger_en` / `trigger_fa` text columns to
  `ai_config`, un-blocking the whole AI-config upsert that currently
  fails on every save because the payload always contains both keys.
- `supabase/migrations/20260827000003_create_ai_usage_table.sql` (new) —
  creates `ai_usage` (bigserial PK, `token_source`, owner + created_at
  indexes) with RLS enabled and a SELECT-only policy for anon +
  authenticated.
- `supabase/migrations/20260827000004_create_ai_provider_stats_table.sql`
  (new) — creates `ai_provider_stats` with the composite PRIMARY KEY
  `(provider_name, owner_id)` that is the exact conflict target of the
  writer's `on_conflict="provider_name,owner_id"` upsert; RLS enabled
  with a SELECT-only policy.
- `DATABASE_ARCHITECTURE.md` (modified) — §1, §4, §6, §7, §12, §13, §18,
  §19.1, §19.3, §19.8 and §20 statuses updated from "no migration" /
  "MIGRATION REQUIRED" to the concrete migration files, each marked
  **pending manual application**. No spec content was changed.

No Python, TypeScript, or configuration code was modified. The writers
(`SupabaseUsageRepository.create`,
`SupabaseProviderStatsRepository.record_request`, `config_store`,
`panel_settings_repository`) already send payloads that match these
schemas, so no code change is required — once the migrations are applied
manually, persistence resumes working.

## Verification performed

- Migration 000001 vs `backend/services/settings_service.py`: the 10
  column names/types/defaults match `_DEFAULTS` exactly; CHECK ranges
  (5–3600, 1–500, 1–1000, 1–365, 30–86400, non-empty `language`) match
  `_VALIDATORS` exactly; `ON CONFLICT (key)` resolves against
  `key text PRIMARY KEY` from the base migration `20260726143924`.
- Migration 000002 vs `backend/ai/config_store.py`: the save payload
  always includes both trigger keys and normalizes empty strings to
  `None` (`config.get(...) or None`), matching the nullable columns.
- Migration 000003 vs `backend/ai/database/usage_repository.py`: the
  insert payload keys (`owner_id, session_id, provider, model,
  prompt_tokens, completion_tokens, total_tokens, latency_ms,
  token_source, created_at`) are all covered; the writer never sends
  `id` (bigserial is correct); `count()` reads `id`; readers use
  `total_tokens` / `created_at` / `owner_id`.
- Migration 000004 vs `backend/ai/database/provider_stats_repository.py`:
  every `ProviderStatsRecord.as_dict()` key is a column; `last_request_at`
  nullable matches the `None` branch of `as_dict()`; the composite PK is
  the upsert conflict target.
- `DATABASE_ARCHITECTURE.md` claims ("generated from §12/§13") were
  re-checked against the spec tables — column-by-column accurate.
- Tests: targeted persistence surfaces (`test_03_database_consistency`,
  `test_33_ai_telemetry`, `test_42_dashboard_font`,
  `test_44_database_stats`, `test_10_tool_calls`): **63 passed** in
  5.86s. Full repository suite (`tests/`): **981 passed, 23 skipped** in
  31.69s — identical to the recorded HEAD baseline; the 23 skips are the
  pre-existing legacy `ghost_seen_service` tests, unrelated to this
  change. `git diff --check`: PASS.

## Explicit boundary

- All four migrations are **additive and idempotent** — no `DROP`, no
  destructive statements, no RLS weakening (SELECT-only read policies as
  documented in §18).
- No SQL was executed against any database (no Postgres tooling in this
  environment; applying the migrations to the live Supabase project
  remains a separate manual owner action, as recorded in
  `DATABASE_ARCHITECTURE.md` §20).
- Phase 3 drop decisions (`ghost_chats`, orphan `ghost_seen_retention_*`
  columns, `ai_preferences`, legacy tests) remain owner decisions and
  were intentionally not touched.
- `INVESTIGATION.md` was not modified (it is the Phase 3-only canonical
  handoff by explicit instruction); this report is the delivery record.

## Delivery verification

- Implementation commit: `775b010` — `fix: add p0 schema reconciliation
  migrations` (6 files, +320/−26).
- Push result: pushed to `origin/main` (`470769c..775b010`), exit 0.
- Final working-tree state: `main` in sync with `origin/main`; the four
  migration files, `DATABASE_ARCHITECTURE.md`, and this report are
  committed; no unrelated files were touched.

---

# Phase 4 — Database Architecture Verification

## Objective

Verify the Phase 3 migration delivery against the current repository, source
writers, architecture documentation, and available database tooling without
inventing live Supabase state or performing destructive operations.

## Base commit

`1efb119` — `docs: record schema migration delivery verification`.

## Repository state verified

- Current branch: `main`, tracking `origin/main`.
- HEAD was clean before the Phase 4 audit.
- The four Phase 3 migration files exist at the paths reported in Phase 3.
- `DATABASE_ARCHITECTURE.md` documents those migrations as pending manual
  application, rather than claiming they are live.
- No contradiction requiring a repository correction was found.

## Files inspected

- `AGENTS.md`
- `DATABASE_ARCHITECTURE.md`
- `INVESTIGATION.md`
- `IMPLEMENTATION_REPORT.md`
- `supabase/migrations/20260827000001_add_missing_panel_settings_columns.sql`
- `supabase/migrations/20260827000002_add_ai_config_trigger_columns.sql`
- `supabase/migrations/20260827000003_create_ai_usage_table.sql`
- `supabase/migrations/20260827000004_create_ai_provider_stats_table.sql`
- `backend/services/settings_service.py`
- `backend/services/panel_settings_repository.py`
- `backend/ai/config_store.py`
- `backend/ai/database/usage_repository.py`
- `backend/ai/database/provider_stats_repository.py`
- related AI database manager/usage-recorder and migration sources

## Findings

### Migration-to-source contract matrix

| Migration | Table / columns | Application writer and exact contract | Constraints / indexes / RLS | Potential mismatch | Verdict |
|---|---|---|---|---|---|
| `000001` | `panel_settings`; adds `auto_close_delay`, `max_deep_save_mb`, `delete_batch_size`, `log_retention_days`, `panel_timeout_seconds`, `allow_multiple_panels`, `reuse_existing_panel`, `language`, `debug_callbacks`, `owner_only` | `settings_service` typed defaults and validators consume all ten exact names; repository upserts the typed settings and the migration also ensures `key='global'` | Integer/boolean/text types and defaults match source; CHECK ranges match validators; singleton row uses existing `key` primary key; no RLS alteration | None found in the repository contract | PASS |
| `000002` | `ai_config.trigger_en`, `ai_config.trigger_fa`, nullable text | `config_store._save_config_sync` includes both keys in every upsert and converts empty values to NULL | Nullable text, default NULL; existing table RLS is not changed | Live application status cannot be inferred from the file | PASS — pending live application |
| `000003` | `ai_usage`: bigserial `id`, owner/session/provider/model, token counts, latency, token source, timestamp | `SupabaseUsageRepository.create` inserts `owner_id, session_id, provider, model, prompt_tokens, completion_tokens, total_tokens, latency_ms, token_source, created_at`; it does not send `id`; readers/count use the documented columns | Primary key and owner/created indexes; RLS enabled with SELECT-only anon/authenticated policy; service-role writes remain separate | Repository evidence cannot prove the live table exists | PASS — pending live application |
| `000004` | `ai_provider_stats`: provider/owner composite key plus request counters, token counters, latency, timestamps | `SupabaseProviderStatsRepository.record_request` upserts the complete `ProviderStatsRecord.as_dict()` payload with conflict target `provider_name,owner_id`; nullable `last_request_at` matches source | Composite primary key supplies the upsert uniqueness; RLS enabled with SELECT-only policy; no destructive operation | Repository evidence cannot prove live policy state | PASS — pending live application |

The migration SQL is ordered after the existing 20260826/20260827 migration
series and is additive/idempotent. The new tables use `IF NOT EXISTS`; column
adds use `IF NOT EXISTS`; policy recreation is explicit and limited to the new
AI tables. No migration drops tables, columns, or indexes and none weakens an
existing RLS policy.

### Architecture-document audit

`DATABASE_ARCHITECTURE.md` accurately records the four migration paths, the
schema contracts, the RLS intent, and the **pending manual application** status.
`INVESTIGATION.md` Phase 3 and the architecture document agree that destructive
cleanup and live-data-dependent decisions remain blocked. No documentation
edit was necessary during this phase.

### Live database gate

Live Supabase inspection was **not available** through the repository tooling
in this environment. Direct environment/database inspection was blocked by the
workspace security boundary, and no safe authenticated Supabase inspection path
was available. Therefore this phase does not claim whether the migrations have
been applied, what RLS policies currently exist, or whether any rows/columns
contain data.

The following remain unverified: live RLS posture; application status of all
four migrations; `ghost_chats` row count; non-empty `saved_items.short_code`
and `file_name`; obsolete `panel_settings` values; obsolete `bot_settings`
seed rows; and whether dropping `ghost_chats` would lose data.

## Changes implemented

Investigation/verification only. No application or schema implementation was
performed in Phase 4, and `DATABASE_ARCHITECTURE.md` required no correction.

## Database safety

- Live Supabase: unavailable; repository credentials or safe inspection tooling
  were not exposed.
- SQL execution: none.
- Destructive operations: none.
- RLS changes: none.
- Data modification: none.

## Tests actually executed

- `python3 -m pytest tests/test_03_database_consistency.py tests/test_33_ai_telemetry.py tests/test_42_dashboard_font.py tests/test_44_database_stats.py tests/test_10_tool_calls.py -q --no-header` — **63 passed**, one existing warning.
- `python3 -m pytest tests/ -q --no-header` — **981 passed, 23 skipped**, one existing warning.
- `python3 -m compileall -q backend tests` — passed (exit 0).
- `git diff --check` — passed (exit 0).

## Previous recorded tests

The Phase 3 delivery report also records 63 targeted passes and 981 passes /
23 skips. Those are historical records; the results above were executed again
in Phase 4 and are reported separately.

## Remaining blockers

1. Live Supabase access is required before claiming the four migrations are
   applied or before changing their live-status documentation.
2. Live RLS verification remains required.
3. Live-data checks remain required before any destructive decision involving
   `ghost_chats`, `saved_items.short_code`, `saved_items.file_name`, obsolete
   `panel_settings` columns, or obsolete `bot_settings` seed rows.
4. Product/owner decisions remain required for the previously gated orphan
   settings, `ai_preferences`, Ghost Seen destination configuration, AI
   retention, dashboard font surface, and legacy skipped-test disposition.

## Commit / delivery

This Phase 4 report update is the only repository change. Commit and remote
verification details are recorded after delivery.

---

# Canonical Database Bootstrap — Full Contract Audit Delivery

## Objective

Produce ONE canonical, self-contained Supabase/PostgreSQL bootstrap script
that establishes the complete database state required by CURRENT application
code, derived from a repository-wide database contract audit — not from
prior reports or documentation prose.

## Base commit

`30bb3a426c2ec419be9d8f43373d85ce27d77099` (`origin/main`, clean tree).

## Repository state verified

- Every `.table(` call site enumerated across `backend/`, `tests/`, `src/`:
  **60 call sites, zero `.rpc(` calls** (no functions/triggers required).
- All 16 migrations under `supabase/migrations/` read chronologically.
- Writer/read payloads traced at source level: `backend/db/client.py`,
  `backend/services/save_service.py`, `backend/services/settings_service.py`,
  `backend/services/panel_settings_repository.py`,
  `backend/services/ghost_seen_v2.py`, `backend/ai/persistence.py`,
  `backend/ai/config_store.py`,
  `backend/ai/database/usage_repository.py`,
  `backend/ai/database/provider_stats_repository.py`, `backend/web/app.py`.
- Table inventory: **14 tables = 13 code-active** (`saved_items`,
  `bio_state`, `username_state`, `bot_logs`, `panel_settings`,
  `bot_settings`, `ai_config`, `ai_sessions`, `ai_messages`, `ai_memories`,
  `ai_tool_history`, `ai_usage`, `ai_provider_stats`) **+ 1 legacy**
  (`ghost_chats`, zero code references, owner-gated drop).

## Findings (headline corrections vs prior reports)

- `username_state` **has** a migration (`20260801215007`); the earlier
  "migration-less" claim was an indexing artifact.
- `saved_items.file_name`/`short_code` have **no live writer** — the current
  Deep-Save payload in `save_service.py` omits both (preserved additively).
- Migration-history conflicts: `20260712234229` vs `20260714111706`
  (`save_type`/`bot_logs.level` CHECKs removed by the later file; a raw
  chronological replay leaves `anon_update_bot_logs` alive);
  `20260823130000` performs a destructive days→seconds column transition
  that the canonical script does not replay.
- Legacy columns preserved additively: `saved_items.short_code`/`file_name`,
  `panel_settings.update_stale_seconds`,
  `panel_settings.ghost_seen_retention_seconds`,
  `ai_tool_history.result_data`.
- Seeds: only `panel_settings('global')` is code-required; the five
  `bot_settings` legacy defaults are retained `ON CONFLICT DO NOTHING`;
  `ghost_seen_allowed_chats` is runtime-created and deliberately NOT seeded.

## Changes implemented

- `DATABASE_ARCHITECTURE.md`: new **"Canonical Supabase Bootstrap SQL (Full
  Database Contract Audit)"** section — application↔database contract
  matrix, migration-history reconciliation findings, and ONE complete fenced
  SQL block (510 lines: 14 tables, 28 indexes, RLS enabled with SELECT-only
  anon policies on every table, `BEGIN`/`COMMIT`), plus explicit boundaries
  and uncertainty statements.
- `supabase/canonical_bootstrap.sql`: standalone copy of the canonical block,
  verified **byte-identical** to the doc's fenced block programmatically.
- Implementation note: the file-edit tool layer was out of sync with
  `DATABASE_ARCHITECTURE.md` (seven anchored edit attempts failed against
  byte-verified content, proven via `od`). The section was spliced
  programmatically and the result re-verified: exactly one ` ```sql ` fence,
  byte-identity with the standalone file, and correct
  table/index/policy/seed counts.

## Database safety

- Live Supabase was **NOT accessible**; no live state is claimed.
- **No SQL was executed** against any database (no PostgreSQL tooling in the
  environment; validation was static: contract cross-check + block
  completeness checks).
- No destructive operations; no RLS weakening — the script enforces the
  documented SELECT-only anon boundary and drops only anon WRITE policies
  that contradict it. No data was modified.

## Tests actually executed (this phase, this environment)

- `python3 -m pytest tests/ -q` → **981 passed, 23 skipped, 1 warning in
  31.35s** (the 23 skips are the pre-existing legacy `ghost_seen_service`
  suite; identical to the `30bb3a4` baseline).
- `python3 -m compileall -q backend tests` → OK.
- `git diff --check` → clean.
- SQL execution against a live/test database: **not performed** (no
  PostgreSQL/Supabase tooling available; no parser installed).

## Previous recorded tests

- `775b010` delivery: full suite 981 passed, 23 skipped (recorded then, not
  re-run as part of that phase's evidence).

## Remaining blockers (owner-gated, unchanged)

`ghost_chats` drop (requires live-data check), `saved_items.short_code`/
`file_name` drops, orphan `panel_settings` columns, `ai_preferences`,
`GHOST_SEEN_DESTINATION_*` configuration, AI-table retention policy, live
RLS-posture verification, legacy skipped-test disposition.

## Commit / delivery

- **Commit:** `ebd513b` — `docs: add canonical supabase bootstrap sql and
  full contract audit` (3 files: `DATABASE_ARCHITECTURE.md` +608,
  `supabase/canonical_bootstrap.sql` +510 new, `IMPLEMENTATION_REPORT.md`
  +106; 1224 insertions total).
- **Push:** succeeded — `30bb3a4..ebd513b  main -> main` on
  `https://github.com/Onlyicing1/Telegram-self-bot.git`.
- **Remote verification:** post-push `git fetch` + `origin/main` comparison
  and working-tree cleanliness check recorded in the follow-up verification
  entry below.

---

# Audit — Restart Persistence of Dashboard Font & Ghost Seen Allow-List

**Date:** 2026-08-28 · **Base commit:** `1a4c955` (`docs: record canonical
bootstrap delivery verification`) · **Scope:** verify the full WRITE →
DATABASE → RESTART → READ → RESTORE → CONSUMPTION lifecycle for the
dashboard/profile font and the Ghost Seen allowed-chat list; fix only what
the audit proved broken.

## Verdicts

1. **Dashboard/Profile font survives a full restart: YES (code + DB
   contract verified; live DB application of the migrations remains the
   owner's pending manual step).**
   Selection (`misc.py::_font_set_action` / web `PATCH /api/settings`) →
   validation (`value in FONT_KEYS`, 23 keys — `"default"` included) →
   `panel_settings_repository.update_field('dashboard_font', key)` →
   `UPDATE panel_settings SET dashboard_font=…, updated_at=… WHERE key=
   'global'` → schema `dashboard_font text NOT NULL DEFAULT 'default'`
   + CHECK on the same 23 keys (`20260823120000`) → restart:
   `RuntimeSupervisor.start()` calls `settings_service.load_all()`
   (supervisor.py:177) which hydrates the cache before panels render →
   consumed by `panel_render.py`, `profile/engine.py`, and
   `GET /api/settings` → frontend. `load_all()` never writes defaults
   back, so no restart-time overwrite exists.
2. **Ghost Seen allowed-chat list survives a full restart: YES after the
   fixes below; previously PARTIALLY** — the write/load/restore chain
   existed (`allow_chat`/`disallow_chat` → `bot_settings` key
   `ghost_seen_allowed_chats` JSON array; preload at `register()`;
   union-restore enforced by `is_chat_allowed()`), but two races could
   silently lose persisted chats.
3. WRITE both states: yes (schema payloads verified against the current
   migrations/writers).
4. READ both back after restart: yes (startup hydration for font;
   preload + awaited in-flight load for the allow-list).
5. `DATABASE_ARCHITECTURE.md`: accurate at the schema level; updated
   (§19.4, §25.2) with the verified restart lifecycles and race fixes.
6. Schema/code mismatches: none for these two features (the known
   panel-settings column gap is already covered by migration
   `20260827000001`, pending live application).
7. RLS/permission problems: none — both writes go through the
   service-role key; anon/authenticated policies are SELECT-only.
8. Startup-order/default-value problems: none for the font (cache-first
   with startup hydration). For the allow-list, two races (fixed, below).
9. Tests proving restart persistence: previously the font had a
   service-level reload test but no write→restart→read chain test, and
   the allow-list had NONE. Added (see Changes).
10. Changed: `backend/services/ghost_seen_v2.py` (2 race fixes),
    `tests/test_65_ghost_seen_v2_restart_persistence.py` (new, 5 tests),
    `tests/test_42_dashboard_font.py` (+2 tests), `DATABASE_ARCHITECTURE.md`
    (+2 verified-lifecycle sections).

## Fixes implemented (Ghost Seen only — minimal, in-architecture)

- **Race 1 (partial-list overwrite):** `_ensure_allowed_loaded_async()`
  previously set its loaded-flag before the DB read completed, so a
  Manage toggle landing during the startup preload could persist the
  still-empty/partial in-memory set over the persisted allow-list,
  silently dropping other allowed chats across the restart. Now the
  loader is a single shared `asyncio.Task` and every concurrent caller
  awaits it (`tests/test_65…::test_toggle_during_initial_load_persists_full_list`).
- **Race 2 (stale overwrite):** `_persist_allowed_to_db()` spawned an
  unsynchronized thread per toggle with the value snapshot taken at call
  time; out-of-order thread completion could leave an older list in the
  DB. Writes are now serialized through a `threading.Lock` and the set
  is snapshotted inside the lock, so the last finishing write always
  carries the latest in-memory state
  (`tests/test_65…::test_rapid_toggles_never_persist_stale_list`).
- `reset_allowed_chats()` (test hook) also clears the shared load task.
- No API, schema, handler, or product behavior changed; the daemon-thread
  persist (never blocking the toggle callback) is retained.

## Tests actually executed NOW (this phase)

- `python3 -m pytest tests/test_65_ghost_seen_v2_restart_persistence.py
  tests/test_42_dashboard_font.py tests/test_48_font_panel.py -q` →
  **25 passed**.
- Full suite `python3 -m pytest tests/ -q` → **988 passed, 23 skipped**
  (baseline 981 + 7 new tests; skips are the pre-existing legacy
  `ghost_seen_service` tests, untouched).
- `python3 -m compileall -q backend tests` → OK.
- `git diff --check` → PASS.

## Previous recorded tests (not re-run as proof)

- Baseline full-suite results from the canonical-bootstrap delivery
  (`981 passed, 23 skipped` at `ebd513b`/`1a4c955`) — superseded above
  by the fresh run including the new tests.

## Live database / runtime boundary

- No live Supabase access in this environment; no SQL executed; no live
  runtime (Telegram) restart exercised. Verdicts are code + schema
  contract verifications, simulated at the persistence boundary with
  fakes. Live state (whether migrations `20260823120000` +
  `20260827000001` are already applied) remains an owner-side check.

## Delivery verification

- **Commit:** `c205caa253dcd08c09d00efa19efab97273e815f` — `fix: make
  ghost seen allow-list restart persistence race-free` (5 files,
  +419/−22).
- **Push:** succeeded — `1a4c955..c205caa  main -> main`.
- **Remote verification:** post-push `git fetch` + `git rev-parse HEAD
  origin/main` + `git ls-remote origin main` all equal `c205caa…`.
- **Final working-tree state:** `main` in sync with `origin/main`,
  clean; no unrelated files touched.

# Ghost Seen Durable Persistence Verification — 2026-08-28

## Objective

Verified and documented the complete Ghost Seen allow-list lifecycle: enable → database write → restart → database read → in-memory restoration → enforcement.

## Repository state and contract

Audited current HEAD `128f4883b90b304b88c5c6982202dec34e3b1657`. The active contract is `bot_settings(key, value, value_type, updated_at)`, using `key = 'ghost_seen_allowed_chats'` and a JSON array of integer Telegram chat IDs in `value`.

`backend/services/ghost_seen_v2.py` performs the write through `_persist_allowed_to_db()` and the read through `_ensure_allowed_loaded_async()`. `allow_chat()` and `disallow_chat()` mutate the runtime set and persist the complete sorted set. Startup registration schedules the async loader; browser, manage, and toggle paths await it before consuming the allow-list. The loader unions decoded IDs into `_allowed_chats`, and `is_chat_allowed()` enforces the restored set.

## Database verification

The repository migration `supabase/migrations/20260729213959_20260729120000_create_bot_settings_table.sql` and `supabase/canonical_bootstrap.sql` both define the required `bot_settings` schema, primary key, RLS, and SELECT policy. The canonical SQL does not repurpose `ghost_chats`.

The earlier architecture text incorrectly described `bot_settings` as removable. That stale statement was corrected to mark the proposal superseded because current Ghost Seen code depends on the table.

## Changes

- Corrected the contradictory `bot_settings` statement in `DATABASE_ARCHITECTURE.md`.
- No application code, migrations, or canonical SQL changes were necessary; the prior race-safe implementation and restart regression tests already cover the persistence boundary.
- No new Ghost Seen table or persistence mechanism was introduced.

## Verification actually executed

- `python3 -m pytest tests/test_65_ghost_seen_v2_restart_persistence.py -q --no-header` — 5 passed.
- `python3 -m pytest tests/ -q --no-header` — 988 passed, 23 skipped, 1 warning.
- `python3 -m compileall -q backend tests` — passed.
- `git diff --check` — passed.

Live Supabase and Telegram runtime access were not used. SQL was not executed against any database. The live presence of `bot_settings` remains to be confirmed separately; the repository schema and code contract agree.

## Safety

No credentials, tokens, session strings, or historical data were added. No destructive SQL was run, no RLS policy was weakened, and no database data was modified.

## Delivery

Pending commit and push of this report plus the architecture correction.

# Complete Application-Code ↔ Database-Schema Compatibility Audit — 2026-08-28

## Objective and scope

Audited current HEAD `effdb70748aaafa0e92d7109a4eefa8b28dd1da1` against `DATABASE_ARCHITECTURE.md`, `supabase/canonical_bootstrap.sql`, all tracked migrations, backend persistence code, startup hydration, web readers, and database-focused tests.

## Findings

Active tables verified: `saved_items`, `bio_state`, `username_state`, `bot_logs`, `panel_settings`, `bot_settings`, `ai_config`, `ai_sessions`, `ai_messages`, `ai_memories`, `ai_tool_history`, `ai_usage`, and `ai_provider_stats`. `ghost_chats` is legacy/owner-gated with no current consumer. No `.rpc()` or raw SQL execution paths were found.

All active reads, inserts, updates, deletes, filters, pagination, JSON serialization, defaults, nullable fields, CHECK values, and the `ai_provider_stats` conflict target `provider_name,owner_id` were cross-checked against the canonical SQL. No active code ↔ canonical SQL payload mismatch was found.

Ghost Seen persists the complete sorted integer-ID set in `bot_settings.value` under `ghost_seen_allowed_chats`; startup hydration restores `_allowed_chats`, and `is_chat_allowed()` consumes it. Dashboard font persists through `panel_settings.dashboard_font` and is restored by `settings_service.load_all()` during supervisor startup. Bio/Username, Save, and AI persistence/telemetry contracts also match the canonical schema.

Historical migration-status prose still contains older pending/proposed descriptions for objects resolved by Phase 3. Those are documentation drift in historical sections, not active schema incompatibilities; the canonical SQL and current contract matrix are authoritative. No production code, migration, or canonical SQL change was required.

## Safety and live-state boundary

RLS remains enabled with anonymous SELECT-only policy posture; service-role writes are preserved. No destructive SQL, live SQL execution, RLS weakening, credentials, historical data changes, arbitrary SQL execution, or arbitrary Telegram execution was introduced. Live Supabase and live Telegram runtime state were not accessed.

## Validation actually executed

- `python3 -m pytest tests/test_65_ghost_seen_v2_restart_persistence.py -q --no-header` — 5 passed.
- `python3 -m pytest tests/ -q --no-header` — 988 passed, 23 skipped, 1 warning.
- `python3 -m compileall -q backend tests` — passed.
- `git diff --check` — passed.

## Delivery

Pending commit and push of this audit report.

# Canonical Database Architecture Specification — 2026-08-28

## Objective

Reconciled `DATABASE_ARCHITECTURE.md` into an explicit canonical database construction specification for the current application, while preserving the verified canonical SQL block and avoiding application/schema redesign.

## Changes

- Added a clearly marked final canonical contract section covering all application tables, persistent-state ownership, RLS posture, required seeds, Ghost Seen storage, dashboard font storage, and transient-state boundaries.
- Preserved the existing complete 510-line SQL block and verified it remains byte-identical to `supabase/canonical_bootstrap.sql`.
- No migrations, application code, live database, or canonical standalone SQL file were changed.

## Included contract

The specification covers `saved_items`, `bio_state`, `username_state`, `bot_logs`, `panel_settings`, `bot_settings`, `ai_config`, `ai_sessions`, `ai_messages`, `ai_memories`, `ai_tool_history`, `ai_usage`, `ai_provider_stats`, and compatibility-preserved `ghost_chats`. Ghost Seen uses `bot_settings.key='ghost_seen_allowed_chats'` with a JSON array of integer chat IDs. Dashboard font uses `panel_settings.dashboard_font` with the built-in 23-key CHECK. Service-role writes and anonymous/authenticated SELECT-only RLS policies are preserved.

## Validation actually executed

- `python3 -m pytest tests/ -q --no-header` — 988 passed, 23 skipped, 1 warning.
- `python3 -m pytest tests/test_03_database_consistency.py tests/test_33_ai_telemetry.py tests/test_44_database_stats.py tests/test_65_ghost_seen_v2_restart_persistence.py -q --no-header` — 40 passed.
- `python3 -m compileall -q backend tests` — passed.
- `git diff --check` — passed.
- Canonical fenced SQL vs `supabase/canonical_bootstrap.sql` — byte-identical.

Live Supabase access and SQL execution were not performed. No credentials or historical application data were added, and no destructive SQL or RLS weakening occurred.

## Delivery

## 2026-08-28 — Durable Ghost Seen persistence fix

### Objective
Ensure Ghost Seen allow-list changes are durably written before the UI confirms the toggle, while preserving restart restoration and race safety.

### Root cause
The previous `allow_chat()`/`disallow_chat()` path launched a daemon persistence thread and returned immediately. Database exceptions were logged only, so the in-memory UI could show chats as allowed even when `bot_settings` was unavailable or the write had not completed. The observed `ghost_chats.allowed=false` rows are not evidence of the active contract: current Ghost Seen code does not read or write `ghost_chats`.

### Contract and lifecycle
The active durable state is `bot_settings(key text primary key, value text not null, value_type text not null default 'str', updated_at timestamptz)`, with `key='ghost_seen_allowed_chats'` and `value` as a JSON array of integer Telegram chat IDs. Manage toggles call the async `allow_chat_and_persist()` / `disallow_chat_and_persist()` path; the full sorted set is written via update-or-insert. Startup registration schedules `_ensure_allowed_loaded_async()`, which reads the same row, decodes JSON, and restores `_allowed_chats`; `is_chat_allowed()` consumes that set. `ghost_chats` remains legacy and is not a second source of truth.

### Changes
- `backend/services/ghost_seen_v2.py`: made durable persistence awaitable and boolean-result based; unavailable/failed database writes return `False` and are not reported as successful. Existing serialized write locking and shared startup-load task remain intact.
- `backend/bot/handlers/ghost_seen_v2.py`: awaits persistence and renders an explicit failure instead of a successful manage state when the write fails.
- `tests/test_65_ghost_seen_v2_restart_persistence.py`: updated lifecycle tests for the awaited write boundary.
- `tests/test_66_ghost_seen_v2_persistence_failures.py`: added write-failure and malformed-JSON safety coverage.
- `tests/test_58_ghost_seen_v2_manage_bounded.py`: adjusted handler tests to model successful persistence while retaining runtime-state assertions.
- `DATABASE_ARCHITECTURE.md` and `supabase/canonical_bootstrap.sql`: existing `bot_settings` contract verified; no schema change or duplicate Ghost Seen table required.

### Verification
- Targeted Ghost Seen/manage tests: **28 passed**.
- Full suite: **990 passed, 23 skipped, 1 warning**.
- `python3 -m compileall -q backend tests`: passed.
- `git diff --check`: passed.
- Canonical SQL synchronization: verified byte-identical between the fenced block and `supabase/canonical_bootstrap.sql`.
- Live Supabase inspection: not performed; SQL was not executed against any database.
- Security: service-role writes and existing SELECT-only RLS policies unchanged; no credentials, arbitrary SQL, or Telegram RPC surface introduced.

### Delivery
Delivered in commit `8679e1881104f03967e9f1b11784cb43864eeb3e` (`fix: confirm ghost seen allow-list persistence`). Push to `origin/main` succeeded. Local HEAD, `origin/main`, and remote `refs/heads/main` match; working tree is clean.
