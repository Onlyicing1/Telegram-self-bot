# Phase 3 — Database Decision-Queue Resolution & Pre-Migration Source Verification

**Project:** Telegram Self-Bot / LifeOS
**Repository:** `https://github.com/Onlyicing1/Telegram-self-bot`
**Branch:** `main`
**Phase:** 3 of N — resolution of the Phase 2 decision queue against current source
**Date:** 2026-08-27
**Base commit under investigation:** `9b4d565` (`docs: record ghost seen v2 delivery verification in implementation report`)

> This phase continues the database architecture investigation. Per the
> approved phase-reset rule, `INVESTIGATION.md` now contains **only this
> Phase 3 report**; Phase 1 (database discovery) and Phase 2 (database
> architecture reconciliation) were completed and delivered earlier and are
> NOT reproduced here. Their decision queue (`§14` unresolved questions,
> `§15` decision queue, `§16` recommended order) is the input to this phase.
>
> **Phase boundary:** investigation and reporting only. No application code,
> no migrations, no SQL, no Supabase state, no UI, and no tests were
> modified. The only repository change is this file.

---

## 1. Phase 3 Scope

Phase 2 ended with a decision queue that had to be resolved **before any
implementation**. Several items were left open because the evidence at that
time did not uniquely determine the answer (e.g. the `ai_usage.id`
contract, the `ghost_chats` repurpose-vs-drop question). Since Phase 2, the
repository received the Ghost Seen v2 AI-reply execution-hardening work
(commits `3faac6a`, `9b4d565`), which touched the AI dispatcher, AI
diagnostics, and the Ghost Seen v2 handler/service.

Phase 3 therefore re-verifies every open decision-queue item against the
**current** source tree at `9b4d565` and, where the code now provides a
determinative answer, records that resolution with exact file/line
evidence. Specifically:

1. Re-confirm the P0 schema gaps (`panel_settings` missing columns,
   `ai_config` trigger columns) still exist and measure their actual
   blast radius on the write paths.
2. Determine the `ai_usage` / `ai_provider_stats` table situation and the
   exact writer payload contracts, resolving the `ai_usage.id` question.
3. Resolve the `ghost_chats` repurpose-vs-drop decision from live code.
4. Re-verify the P2 cleanup items (dead `saved_items` columns, unconsumed
   `panel_settings` columns, `bot_settings` seed rows) in the current tree.
5. Re-verify the interface-only entities (`ai_preferences`,
   `ai_messages.tool_calls`) and the dashboard font key surface.
6. Identify any NEW discrepancies introduced or exposed by the Ghost Seen
   v2 hardening commits.

Out of scope: implementing any of the resolved decisions; verifying
live Supabase state (impossible from the repository alone); redesigning
the architecture.

---

## 2. Sources Inspected

| Area | File(s) / evidence |
|---|---|
| Panel settings service | `backend/services/settings_service.py` (`_DEFAULTS`, `_VALIDATORS`, `set_setting`) |
| Panel settings repository | `backend/services/panel_settings_repository.py` (`load`, `update_field`) |
| AI config persistence | `backend/ai/config_store.py` (`_save_config_sync`, `update_triggers`, trigger defaults) |
| AI persistence (direct writes) | `backend/ai/persistence.py` (`_create_session_sync`, `_add_message_sync`, `_save_memory_sync`, `_record_tool_call_sync`) |
| AI repository layer | `backend/ai/database/manager.py` (`RepositoryManager`, `get_repository_manager`), `usage_repository.py` (`SupabaseUsageRepository.create`), `usage_recorder.py` (`record_usage`, `_write_sync`, uuid id), `provider_stats_repository.py` (`on_conflict` upsert), `message_repository.py` (`MessageRecord.tool_calls`), `preferences_repository.py`, `__init__.py` table map |
| Ghost Seen v2 persistence | `backend/services/ghost_seen_v2.py` (allowed-chat load/persist via `bot_settings`, `GHOST_SEEN_DESTINATION_*` getters), `backend/bot/handlers/ghost_seen_v2.py` |
| Core DB access | `backend/db/client.py` (`save_code` paths, saved-items search projections) |
| Config | `backend/config.py` (`GHOST_SEEN_DESTINATION_CHAT_ID/_NAME` env reads) |
| Fonts | `backend/helper/font_style.py` (`_FONT_REGISTRY`, `FONT_KEYS`), `src/App.tsx` (`DASHBOARD_FONT_OPTIONS`, `applyFont`, save/rollback flow) |
| Web API | `backend/web/app.py` (`PATCH /api/settings` → `settings_service.set_setting`) |
| Migrations (all 12 re-read) | `supabase/migrations/` — esp. `20260718143752_*_save_ux_redesign.sql.sql`, `20260726143924_create_panel_settings_table.sql`, `20260729213959_*_create_bot_settings_table.sql`, `20260730210551_*_add_update_stale_seconds.sql`, `20260804145402_create_ai_tables.sql`, `20260805075707_*_create_ai_config_table.sql.sql`, `20260822090000_create_ghost_chats_table.sql`, `20260823120000_add_dashboard_font_and_ghost_seen_settings.sql`, `20260823130000_ghost_seen_retention_duration.sql` |
| Tests (reference resolution only) | `tests/test_51_execution27.py` (imports `backend.services.ghost_seen_service`) |
| Recorded test state | `IMPLEMENTATION_REPORT.md` at `9b4d565` (full-suite result for HEAD: 981 passed / 23 skipped) |

---

## 3. Investigation Method

1. **Decision-queue-driven tracing.** Each Phase 2 decision item was traced
   end-to-end in the current tree: writer function → payload → Supabase
   table accessor → migration DDL, and (where relevant) the failure/fallback
   branch on schema mismatch.
2. **Payload-contract extraction.** For every table the AI repository layer
   claims to persist, the exact insert/upsert payload was read from the
   Supabase-backed repository class (not the dataclass), because only that
   class reaches the network.
3. **Reference cross-checks.** Repo-wide greps (excluding `__pycache__`)
   for every claimed-dead or claimed-live symbol
   (`trigger_en|trigger_fa`, `ghost_chats`, `bot_settings`,
   `short_code|file_name`, `update_stale_seconds`,
   `ghost_seen_retention_days|ghost_seen_retention_seconds`,
   `GHOST_SEEN_DESTINATION`, `ai_preferences`, `tool_calls`), with each hit
   classified as runtime, test-only, or artifact-only.
4. **Migration-comment verification.** Each migration's behavioral comment
   was checked against the code it claims to describe; mismatches are
   recorded as documentation drift.
5. **Test-import resolution.** Legacy test files' imports were resolved to
   confirm which of them target modules that no longer exist.
6. **Honesty constraints.** No test run was performed in this investigation
   environment (no `pytest` installed); the recorded HEAD-state suite result
   from `IMPLEMENTATION_REPORT.md` (`9b4d565`) is cited as the test-state
   evidence and labeled as such. Every conclusion below cites the exact
   source location that proves it.

---

## 4. Findings

### 4.1 P0 — `panel_settings` is missing exactly 10 typed columns (RE-CONFIRMED, blast radius refined)

`settings_service._DEFAULTS` defines **12 typed settings**
(`backend/services/settings_service.py`): `auto_close_enabled`,
`auto_close_delay`, `max_deep_save_mb`, `delete_batch_size`,
`log_retention_days`, `panel_timeout_seconds`, `allow_multiple_panels`,
`reuse_existing_panel`, `language`, `debug_callbacks`, `owner_only`,
`dashboard_font`.

Schema columns that actually exist for `panel_settings`:

- `key`, `auto_close_enabled`, `updated_at`
  (`20260726143924_create_panel_settings_table.sql`)
- `dashboard_font`, `ghost_seen_retention_days`
  (`20260823120000_add_dashboard_font_and_ghost_seen_settings.sql`)

→ **Missing: exactly 10 columns** — `auto_close_delay`, `max_deep_save_mb`,
`delete_batch_size`, `log_retention_days`, `panel_timeout_seconds`,
`allow_multiple_panels`, `reuse_existing_panel`, `language`,
`debug_callbacks`, `owner_only`.

Write-path blast radius (refined beyond Phase 2's "does not persist"):

1. Panel or dashboard calls `settings_service.set_setting(key, value)`.
2. `repo.update_field` issues `UPDATE panel_settings SET <key>=... `
   → PostgREST rejects the unknown column (PGRST204) → exception is caught
   → `update_field` returns `False`
   (`panel_settings_repository.py::update_field`).
3. `set_setting` then takes the fallback branch: `_cache[key] = value` and
   **returns `True`** (`settings_service.py::set_setting`) — the caller
   (Glass panel or `PATCH /api/settings`) is told the write succeeded.

**Net:** 10 of 12 typed settings are silently RAM-only; every restart or
`load_all()` reverts them to defaults, and the UI reports success for
writes that can never persist. Only `auto_close_enabled` and
`dashboard_font` persist today. This is the highest user-visible
correctness gap and the first P0 migration.

### 4.2 P0 — `ai_config.trigger_en` / `trigger_fa`: the gap is larger than Phase 2 recorded (RE-CONFIRMED, blast radius enlarged)

Phase 2 recorded this as "AI trigger words are lost on restart." The
current source shows the blast radius is the **entire `ai_config` row**:

- `config_store._save_config_sync`
  (`backend/ai/config_store.py`) always includes
  `"trigger_en": config.get("trigger_en", "") or None` and
  `"trigger_fa": ... or None` in the upsert payload — the keys are present
  on **every** save, not only when the owner edits triggers.
- The `ai_config` table (`20260805075707_*_create_ai_config_table.sql.sql`)
  has **no trigger columns**.
- PostgREST rejects the whole insert/update on the unknown column → the
  `except` branch stores the config in `_fallback_config` (RAM) and
  **returns `True`** — a dishonest success signal, same pattern as §4.1.

**Net:** with Supabase configured, AI configuration persistence
(provider, model, temperature, max_tokens, system_prompt, history_budget,
is_configured) is currently **non-functional in its entirety**; the config
silently degrades to RAM on every save. The fix is one migration adding
`trigger_en text` + `trigger_fa text` (nullable) — no code change
required for the happy path.

### 4.3 P0 — `ai_usage` and `ai_provider_stats` have no migrations; the id contract is now determinable (RE-CONFIRMED + RESOLVED)

Facts (current tree):

- No migration creates `ai_usage`, `ai_provider_stats`, or
  `ai_preferences`. The only AI-tables migration
  (`20260804145402_create_ai_tables.sql`, 112 lines) creates exactly four
  tables: `ai_sessions`, `ai_messages`, `ai_memories`, `ai_tool_history`.
- `get_repository_manager()` (`backend/ai/database/manager.py`) wires
  `SupabaseUsageRepository` and `SupabaseProviderStatsRepository`
  whenever `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` are set, so these
  writers **do run against Supabase in production**.
- `SupabaseUsageRepository.create`
  (`backend/ai/database/usage_repository.py`) inserts this exact payload:
  `owner_id, session_id, provider, model, prompt_tokens,
  completion_tokens, total_tokens, latency_ms, token_source, created_at`.
  **`id` is NOT sent.** The `UsageRecord.id` (a `uuid4` string, generated
  in `usage_recorder.py::_write_sync`) exists only on the in-memory path.
- `SupabaseProviderStatsRepository` upserts with
  `on_conflict="provider_name,owner_id"`
  (`provider_stats_repository.py`), so the table must carry a
  `UNIQUE (provider_name, owner_id)` constraint.
- `usage_recorder.record_usage` is invoked by the dispatcher after every
  execution (`guarded_create_task`), failures are logged and swallowed.

Resolutions forced by this evidence:

- **`ai_usage.id` (Phase 2 §14.9): RESOLVED — bigserial PK.** The Supabase
  writer never sends `id`; a `bigserial` primary key matches every other
  AI table and requires no code change. The uuid `UsageRecord.id` stays an
  in-memory-only detail. The migration must include the `token_source`
  column the writer sends (absent from Phase 2's column notes).
- **`ai_provider_stats`:** migration must define
  `UNIQUE (provider_name, owner_id)` to satisfy the upsert conflict target.
- **Net effect today:** every AI usage / provider-stats write fails with
  "relation does not exist", is logged, and is discarded — **AI usage and
  provider statistics are never persisted** even with Supabase fully
  configured. (Sessions, messages, memories, tool history DO persist via
  `persistence.py` + existing tables.)

### 4.4 P1 — `ghost_chats`: RESOLVED BY CODE — the table is orphaned; drop (not repurpose)

- The Ghost Seen v2 runtime persists its allowed-chat set in the
  `bot_settings` KV key `ghost_seen_allowed_chats`
  (`backend/services/ghost_seen_v2.py`, load/update/insert around
  lines 137–191). This is the **only** production `bot_settings` consumer
  in the entire backend (repo-wide grep).
- The `ghost_chats` table (`20260822090000_create_ghost_chats_table.sql`)
  has **zero references** in `backend/` or `src/`. The only references in
  the repository are in `tests/test_51_execution27.py`, which imports
  `backend.services.ghost_seen_service` — a module that **no longer
  exists** (`ls` confirms; see §4.10). Those references are test-side
  artifacts of removed code, not runtime usage.
- Therefore Phase 2's open question ("repurpose as the per-chat Ghost PV
  table, or drop and keep the KV key?") is answered by the code itself:
  **the KV key IS the live mechanism; the table has no consumer.** The
  decision resolves to **drop `ghost_chats`** (after a one-time live-data
  check, since live tables cannot be inspected from the repository).
  The paired Phase 2 note "`bot_settings` retirement follows" does NOT
  follow: `bot_settings` remains live as the Ghost Seen allow-list store.

### 4.5 P1 — `ai_messages.tool_calls`: downgraded — no live writer needs the column

- The migration's `ai_messages` table has no `tool_calls` column.
- `MessageRecord.tool_calls` exists only in the in-memory repository
  (`message_repository.py`); **no Supabase message repository is wired**
  (`RepositoryManager` uses `InMemoryMessageRepository` unconditionally).
- The production writer, `persistence._add_message_sync`
  (`backend/ai/persistence.py`), inserts
  `session_id, owner_id, role, content, provider, model, token_count` —
  no `tool_calls`.
- **Resolution:** the column is required by nothing live. Add it only if a
  Supabase message repository is ever wired; otherwise drop the field from
  `MessageRecord` when the interface is next touched. This item moves from
  P1 to "no action required today."

### 4.6 P1 — `ai_preferences`: CONFIRMED interface-only (unchanged, low priority)

- `PreferencesRepository` has no Supabase implementation;
  `RepositoryManager` always uses `InMemoryPreferencesRepository`.
- No migration exists for `ai_preferences`; no durable producer exists.
- The only consumer, `Dispatcher._load_preferences`
  (`backend/ai/engine/dispatcher.py`), reads the in-memory defaults and
  its docstring explicitly says the table "does not exist yet."
- No user-visible impact. The wire-vs-remove decision gate stands; it is a
  P3 cleanup, not a correctness gap.

### 4.7 P2 — Unconsumed `panel_settings` columns: RE-CONFIRMED, one Phase 2 naming correction

- **`update_stale_seconds`** (`20260730210551_*_add_update_stale_seconds.sql`):
  zero Python references (repo-wide grep). The migration's comment —
  *"The watchdog reads this value via settings_service on every tick"* —
  is **false against current code**: the heartbeat/watchdog uses its own
  hardcoded threshold; `settings_service` has no accessor for it. This is
  documentation drift inside a migration file and must not be trusted as
  a spec.
- **`ghost_seen_retention_days`**
  (`20260823120000_add_dashboard_font_and_ghost_seen_settings.sql`): zero
  Python references. **Phase 2 correction:** Phase 2 recorded the orphan
  setting name as `ghost_seen_retention_seconds`; the actual schema column
  is `ghost_seen_retention_days` (integer, CHECK 1..365). The name
  `ghost_seen_retention_seconds` survives only in skipped legacy tests
  (§4.10). The Ghost Seen retention feature itself is **not implemented**
  in the current runtime (`grep retention` in the Ghost Seen v2 handler
  and service returns nothing).
- Both columns are orphans. The wire-or-drop decision gate stands; wiring
  `update_stale_seconds` into the heartbeat would change recovery behavior
  (default 300 vs hardcoded ~90) and is subject to the single-recovery-
  authority rule (`AGENTS.md` §4) — it must route through
  `RuntimeSupervisor`, never a second watchdog.

### 4.8 P2 — `bot_settings` seed rows: RE-CONFIRMED dead

The `bot_settings` migration seeds five KV keys — `auto_close_enabled`
(including a "Migrate auto_close_enabled from panel_settings" DO-block),
`panel_auto_close_seconds`, `max_deep_save_mb`, `delete_batch_size`,
`log_cleanup_days`. **None has any reader** in the current backend; panel
settings live in `panel_settings` via `settings_service`, and the only
`bot_settings` consumer is the Ghost Seen allow-list key (§4.4). The seed
rows and the migration DO-block are legacy of a superseded settings
design. Cleanup belongs to the implementation phase that also settles
`ghost_chats` (§4.4), so `bot_settings` retention is decided once, not
twice.

### 4.9 P2 — Dead `saved_items` columns and indexes: RE-CONFIRMED

- The save-UX migration
  (`20260718143752_*_save_ux_redesign.sql.sql`) adds `file_name`,
  `short_code`, and five trigram indexes (`caption`, `file_name`,
  `save_code`, `short_code`, `mime_type`).
- Current code (`backend/db/client.py`) reads/writes only
  `save_code`, `caption`, `mime_type`, `save_type`, `media_type`,
  `created_at` (list/search/stats projections at lines ~316, ~349–394).
- `short_code` and `file_name` have **zero** Python DB references — the
  `file_name` hits in `backend/ai/media.py` are an unrelated AI-media
  dataclass field never written to `saved_items`.
- **Dead schema:** columns `saved_items.short_code`, `saved_items.file_name`
  and the indexes `idx_saved_items_short_code_trgm`,
  `idx_saved_items_file_name_trgm` (the `mime_type`/`caption`/`save_code`
  trigram indexes serve live `ilike` search paths and stay). Column drops
  must follow a live-data check (repository cannot prove the columns are
  empty).

### 4.10 NEW — Legacy skipped tests target deleted modules and dead settings

- `tests/test_51_execution27.py` (and the other pre-existing skipped Ghost
  Seen tests — 23 skips recorded for HEAD in `IMPLEMENTATION_REPORT.md`)
  imports `backend.services.ghost_seen_service`, which **no longer exists**
  (confirmed by `ls`), and asserts against
  `settings_service.ghost_seen_retention_seconds()`, which **does not
  exist** in `settings_service.py`, and against the `ghost_chats` table
  (§4.4).
- These tests can never execute (collection-time skip) and encode the
  behavior of removed code. They are not harmfully failing, but they give
  a false impression of retention-feature coverage and keep dead symbols
  (`ghost_seen_retention_seconds`, `ghost_chats`) "referenced" in the
  tree. A future implementation phase should either delete them or rewrite
  them against `ghost_seen_v2`.
- Environment note: this investigation environment has no `pytest`; the
  981-passed/23-skipped figure is cited from the recorded verification run
  at `3faac6a`/`9b4d565` and was not re-executed here.

### 4.11 §14.10 — Dashboard font key surface: RE-CONFIRMED with exact overlap

- Backend allow-list: `FONT_KEYS` from `backend/helper/font_style.py`
  (`_FONT_REGISTRY` — 23 keys: default, serif_bold, serif_italic,
  serif_bold_italic, sans, sans_bold, sans_italic, sans_bold_italic,
  script, script_bold, fraktur, fraktur_bold, double_struck, mono,
  small_caps, circled, circled_dark, fullwidth, parenthesized, underline,
  strikethrough, overline, wavy_underline), mirrored 1:1 by the DB CHECK
  constraint on `panel_settings.dashboard_font`.
- Frontend options (`src/App.tsx::DASHBOARD_FONT_OPTIONS`): exactly 4 keys
  — `default`, `system`, `mono`, `serif`.
- **Overlap: {default, mono}.** Selecting `system` or `serif` fails
  backend validation → `set_setting` returns False → `PATCH /api/settings`
  responds 400 → the frontend reverts to the previous font (honest
  rollback, no corruption) — but half of the visible UI choices are
  unusable, and 21 backend keys are unreachable from the dashboard.
- This is a **code-only** alignment fix (no schema change): extend the
  frontend option list to the backend keys, or trim the backend allow-list.
  Direction is a product/UX decision (§6).

### 4.12 §14.7 — `GHOST_SEEN_DESTINATION_*`: RE-CONFIRMED dead configuration surface

- `backend/config.py` reads `GHOST_SEEN_DESTINATION_CHAT_ID` /
  `GHOST_SEEN_DESTINATION_CHAT_NAME`; `backend/services/ghost_seen_v2.py`
  defines the two getters (lines ~32–37). **Nothing consumes the getters**
  — no notification flow exists. The env vars + getters are dead surface;
  decision gate (build the flow vs remove) stands.

### 4.13 §19.19 — `ai_sessions` PK promotion: unchanged, no code pressure

`ai_sessions` has `id bigserial PRIMARY KEY` + `UNIQUE session_id`; all
writers and readers address rows by `session_id`
(`persistence.py`). Promoting `session_id` to PK remains optional with no
code pressure — keep in P3.

### 4.14 No new regressions from the Ghost Seen v2 hardening commits

The hardening work (`3faac6a`) touched execution plumbing (handler AI-reply
flow, dispatcher instrumentation, `ai/diagnostics.py` request-facts store)
and **introduced no new database tables, columns, or Supabase writers**;
the diagnostics store is bounded in-RAM. Phase 2's findings are therefore
not invalidated by the current HEAD; the only new finding this phase adds
is the legacy-test drift (§4.10).

---

## 5. Verified Conclusions

Decisions that current source evidence now settles (each is an input to a
future implementation phase; nothing was implemented here):

1. **`panel_settings` P0 migration** — add the 10 missing typed columns
   (§4.1). This is the single highest-value fix: it restores persistence
   for 10 of 12 panel settings and removes the dishonest-success fallback
   path for those keys.
2. **`ai_config` trigger migration** — add nullable `trigger_en` /
   `trigger_fa` (§4.2). This restores **all** AI config persistence, not
   just triggers.
3. **`ai_usage.id` contract** — RESOLVED: bigserial PK; the Supabase writer
   never sends `id` (§4.3). Migration must include `token_source` and the
   writer's exact payload fields.
4. **`ai_provider_stats` migration** — must define
   `UNIQUE (provider_name, owner_id)` to satisfy the writer's upsert
   conflict target (§4.3).
5. **`ghost_chats`** — RESOLVED: drop (the `bot_settings` KV key is the
   live allow-list mechanism; the table has zero runtime consumers)
   (§4.4). `bot_settings` itself stays.
6. **`ai_messages.tool_calls`** — not required by any live writer; no
   action today (§4.5).
7. **Dead schema inventory** (drop candidates, each after a live-data
   check): `saved_items.short_code`, `saved_items.file_name`,
   `idx_saved_items_short_code_trgm`, `idx_saved_items_file_name_trgm`
   (§4.9); `panel_settings.update_stale_seconds`,
   `panel_settings.ghost_seen_retention_days` unless the owner chooses to
   wire them (§4.7); `bot_settings` seed rows + legacy DO-block (§4.8);
   `GHOST_SEEN_DESTINATION_*` env surface if the notification flow is not
   built (§4.12).
8. **Dashboard font surface** — code-only fix; current overlap is
   {default, mono} between a 4-key frontend and a 23-key backend (§4.11).
9. **Migration safety order** (carried from Phase 2 §16, unchanged by this
   phase): live RLS verification first, then P0 migrations
   (`panel_settings` → `ai_config` → `ai_usage`/`ai_provider_stats`),
   then decision-gated drops (`ghost_chats`, dead columns) in the same
   cleanup phase that disposes of the `bot_settings` seeds.

---

## 6. Unresolved Items

These remain open because the repository cannot decide them:

1. **Live RLS posture** — whether both core migrations were applied to the
   production Supabase project (and in which order) is unprovable from the
   repository; it gates all migrations and must be verified against the
   live project first.
2. **`update_stale_seconds` + `ghost_seen_retention_days`** — wire into
   runtime or drop. Wiring `update_stale_seconds` changes recovery
   behavior (300 default vs ~90 hardcoded) and must respect the single
   recovery authority (`RuntimeSupervisor`). Owner product decision.
3. **`ai_preferences`** — wire fully (migration + producer) or remove from
   the spec. No producer exists; in-memory defaults only. Owner decision.
4. **`GHOST_SEEN_DESTINATION_*`** — build the destination-notification
   flow or remove the env vars/getters. Owner decision.
5. **AI-table retention policy** — `ai_sessions`, `ai_messages`,
   `ai_tool_history` (and the future `ai_usage`) have no cleanup path.
   Product decision on bounded growth.
6. **`save_code` scoping** — global sequence vs owner-scoped. Practically
   single-owner; cosmetic unless multi-owner is ever planned.
7. **Dashboard font direction** — extend the frontend to the 23 backend
   keys, or trim the backend allow-list to a small curated set. UX
   decision (§4.11).
8. **Legacy skipped tests** (`ghost_seen_service` imports) — delete or
   rewrite against `ghost_seen_v2` (§4.10). Cleanup decision for the next
   implementation phase.
9. **Live-data checks before any column/table drop** — `ghost_chats`,
   `saved_items.short_code/file_name`, and the dead
   `panel_settings`/`bot_settings` rows cannot be inspected from the
   repository; each drop must be preceded by a live count/backup.

---

## 7. Explicit Phase Boundary

This phase performed **decision-queue resolution and reporting only**.
The following were NOT changed:

- ❌ No application code changed (Python, TypeScript/React).
- ❌ No migration created, modified, or deleted; no SQL written or executed.
- ❌ No live Supabase state inspected or mutated (no tables, columns,
  indexes, RLS, rows, or policies touched).
- ❌ No production behavior changed; no ownership semantics, Ghost Seen
  behavior, or AI behavior changed.
- ❌ `DATABASE_ARCHITECTURE.md` not modified in this phase.
- ❌ `IMPLEMENTATION_REPORT.md` not modified (investigation-only phase).
- ❌ No tests were executed in this environment (no `pytest` available);
  the recorded HEAD-state suite result was cited as evidence, not re-run.
- ❌ Phase 1 and Phase 2 content was not reproduced here (approved reset);
  their decisions are consumed, not restated.
- ✅ The only repository modification is this file, `INVESTIGATION.md`,
  reset to contain exactly this Phase 3 report.

*End of Phase 3.*
