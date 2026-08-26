# Phase 1 — Database Discovery Report

**Project:** Telegram Self-Bot / LifeOS
**Repository:** `https://github.com/Onlyicing1/Telegram-self-bot`
**Branch:** `main`
**Phase:** 1 of N — codebase database discovery (investigation only)
**Date:** 2026-08-26

> This report is an independent derivation of the database requirements
> from the **actual repository source code**. It does **not** reconcile
> against `DATABASE_ARCHITECTURE.md` (a later phase), and it makes **no**
> reference to any external/Hermes project — Hermes is not implemented or
> connected in this repository and is out of scope. Where the repository
> cannot prove a requirement, the finding is explicitly marked
> `REQUIRES SOURCE VERIFICATION` or `UNCERTAIN`.
>
> **Phase boundary:** no application code, schema, migration, or
> `DATABASE_ARCHITECTURE.md` was modified. The only repository change is
> this file.

---

## 1. Investigation Scope

The entire backend Python tree (`backend/`, ~330 files), every Supabase
migration (`supabase/migrations/`, 12 files), the web API
(`backend/web/app.py`), the React dashboard read paths
(`src/lib/api.ts`, `src/App.tsx`), and the test suite headers
(`tests/`, used only to confirm which persistence behaviours are covered)
were inspected.

The investigation was **execution-flow-driven**: for each feature, the
data it produces, consumes, and requires to survive a restart was traced
from handlers → services → repository/database layer → table, rather
than only searching for files named "database" or "supabase".

Evidence is cited as `path → function/class` throughout. Every conclusion
is labelled with one of:

- **EXISTING FACT** — directly observable in the current repository.
- **DATABASE REQUIREMENT** — something the implementation genuinely
  requires.
- **ARCHITECTURAL RECOMMENDATION** — a proposed way to model a
  requirement (recommendation only, not implemented).
- **FUTURE/OPTIONAL IDEA** — may be useful later, not required by the
  current implementation.
- **UNCERTAIN / REQUIRES SOURCE VERIFICATION** — the repository does not
  provide enough evidence to decide.

---

## 2. Primary Evidence Sources

| Area | File(s) |
|---|---|
| DB access layer | `backend/db/client.py` (only direct Supabase client factory + core-table access: `saved_items`, `bio_state`, `username_state`, `bot_logs`) |
| Glass panel settings | `backend/services/panel_settings_repository.py`, `backend/services/settings_service.py` |
| Ghost Seen v2 | `backend/services/ghost_seen_v2.py`, `backend/bot/handlers/ghost_seen_v2.py` |
| Save/Retrieve/Delete/Discover | `backend/services/save_service.py`, `retrieve_service.py`, `delete_service.py`, `discover_service.py`, `organize_service.py` |
| Profile engines | `backend/profile/engine.py`, `profile/scheduler.py`, `bio/engine.py`, `username/engine.py`, `services/bio_service.py`, `services/username_service.py` |
| AI config | `backend/ai/config_store.py`, `backend/ai/config/*` |
| AI persistence | `backend/ai/persistence.py`, `backend/ai/database/*.py` (repository modules), `backend/ai/runtime/manager.py`, `backend/ai/engine/dispatcher.py`, `backend/ai/engine/engine.py`, `backend/ai/tools/executor.py` |
| Provider layer | `backend/ai/providers/manager/manager.py`, `manager/health.py`, `registry/registry.py` |
| Font system | `backend/helper/font_style.py` |
| Maintenance/observability | `backend/services/database_service.py`, `backend/observability/db_stats.py`, `backend/runtime/memory_cleanup.py` |
| Runtime (non-DB state) | `backend/helper/input_state.py`, `helper/panel_timer.py`, `helper/session_manager.py`, `helper/inline_engine.py`, `backend/runtime/*` |
| Migrations | `supabase/migrations/*.sql` (12 files) |
| Web API | `backend/web/app.py` |
| Dashboard read paths | `src/lib/api.ts`, `src/App.tsx` |

---

## 3. Codebase Areas Investigated

1. **Database client & core tables** — `backend/db/client.py`:
   `saved_items` (insert/query/list/search/delete/stats), `bio_state`,
   `username_state`, `bot_logs` (log/read/clean), in-memory fallback,
   save-code generation.
2. **Save System (Deep Save)** — `backend/services/save_service.py` +
   `backend/bot/handlers/save.py` + `backend/ai/tools/save.py`.
3. **Retrieve / List / Find / Delete** — `retrieve_service.py`,
   `discover_service.py`, `delete_service.py` + handlers + AI tools.
4. **Database maintenance** — `backend/services/database_service.py`
   (orphan find/clean, stats, vacuum), `backend/bot/handlers/database.py`.
5. **Profile / Bio / Username automation** — `backend/profile/engine.py`,
   `backend/profile/scheduler.py`, `bio/engine.py`, `username/engine.py`,
   `bio_service.py`, `username_service.py`, `bio_state`/`username_state`
   accessors in `db/client.py`.
6. **Ghost Seen / Ghost PV v2** — `backend/services/ghost_seen_v2.py`
   (allow-list, selections, reply states, viewer, browser, Manage),
   `backend/bot/handlers/ghost_seen_v2.py` (actions, AI Reply flow).
7. **Font system** — `backend/helper/font_style.py` (23-key registry),
   `settings_service.dashboard_font`/`set_dashboard_font`,
   `panel_settings.dashboard_font` column + CHECK constraint.
8. **Glass panel settings** — `settings_service.py` (12 typed settings),
   `panel_settings_repository.py` (singleton `key='global'` row).
9. **AI subsystem** — config (`ai_config`), sessions (`ai_sessions`),
   messages (`ai_messages`), memories (`ai_memories`), tool history
   (`ai_tool_history`), usage (`ai_usage`), provider stats
   (`ai_provider_stats`), preferences (`ai_preferences`), runtime
   ConversationManager, dispatcher usage recording, memory manager.
10. **Provider management** — `backend/ai/providers/manager/manager.py`
    (fallback chain, health tracker), `manager/health.py`, `registry/`.
11. **Scheduler** — `backend/profile/scheduler.py` (minute-boundary, RAM).
12. **Helper/panel runtime state** — `input_state.py`, `panel_timer.py`,
    `session_manager.py`, `inline_engine.py` (all in-memory).
13. **Web dashboard** — `backend/web/app.py` endpoints reading the DB.
14. **Runtime stability** — `backend/runtime/*` (heartbeat, failsafe,
    keepalive — telemetry only, no durable DB state beyond `bot_logs`).
15. **All 12 migrations** — full schema as currently defined.

---

## 4. Existing Database Usage

### 4.1 Database clients / wrappers

- **`backend/db/client.py`** — the only direct Supabase client factory
  (`get_db()`, singleton, service-role key) and the only access layer for
  `saved_items`, `bio_state`, `username_state`, `bot_logs`. All calls run
  through `asyncio.to_thread` with a bounded timeout (`_run_sync` →
  `runtime.operation_watchdog.guarded_await`). A full in-memory fallback
  (`_fallback`) mirrors every table when Supabase is unavailable.
  **EXISTING FACT:** there is exactly one database authority for the core
  tables; no competing access layer exists.
- **`backend/ai/config_store.py`** — sole accessor for `ai_config`
  (select-by-owner, insert, update; all wrapped in try/except with
  in-memory fallback).
- **`backend/ai/persistence.py`** — sole accessor for `ai_sessions`,
  `ai_messages`, `ai_memories`, `ai_tool_history` (sync helpers wrapped
  in `asyncio.to_thread` with a bound).
- **`backend/ai/database/*.py`** — repository layer over the same tables:
  `usage_repository.py` (`ai_usage`), `provider_stats_repository.py`
  (`ai_provider_stats`), `memory_repository.py` (`ai_memories` via
  `persistence`), plus in-memory-only repos for session/message/
  preferences/tool-history in `manager.py` (`RepositoryManager`).
  **EXISTING FACT / INCONSISTENCY:** `ai_usage` and `ai_provider_stats`
  repositories are real Supabase writers but have **no migration**;
  `ai_preferences` repository is instantiated **in-memory only**.
- **`backend/services/panel_settings_repository.py`** — the ONLY module
  touching `panel_settings` (table name via constant `_TABLE`).

### 4.2 Complete table inventory

Complete inventory of every table name referenced anywhere in the
repository (source: grep of `.from_(` / `.table(` across `backend/` plus
migration files):

| Table | Referenced by | Migration exists? | Live code consumer? |
|---|---|---|---|
| `saved_items` | `db/client.py` | yes (3 migrations) | yes |
| `bio_state` | `db/client.py` | yes | yes |
| `username_state` | `db/client.py` | yes | yes |
| `bot_logs` | `db/client.py`, `runtime/startup_check.py` | yes | yes |
| `bot_settings` | `services/ghost_seen_v2.py` (key `ghost_seen_allowed_chats`) | yes | yes (one key only) |
| `panel_settings` | `services/panel_settings_repository.py` | yes (4 migrations) | yes (12 settings + font + 2 unconsumed columns) |
| `ai_config` | `ai/config_store.py` | yes | yes |
| `ai_sessions` | `ai/persistence.py` (via `ai/runtime/manager.py`) | yes | yes |
| `ai_messages` | `ai/persistence.py` (via `ai/runtime/manager.py`) | yes | yes |
| `ai_memories` | `ai/persistence.py`, `ai/database/memory_repository.py` | yes | yes |
| `ai_tool_history` | `ai/persistence.py` (via `ai/tools/executor.py`) | yes | yes |
| `ai_usage` | `ai/database/usage_repository.py` (via `usage_recorder.py` ← `dispatcher.py`) | **no** | yes (fails silently if table missing) |
| `ai_provider_stats` | `ai/database/provider_stats_repository.py` (via `usage_recorder.py`) | **no** | yes (fails silently if table missing) |
| `ghost_chats` | **none** (migration only) | yes | **no — orphaned** |
| `ai_preferences` | `ai/database/preferences_repository.py` (dispatcher) | **no** | in-memory only |

**Summary:** 12 tables exist in migrations (11 consumed by live code,
1 orphaned); 2 tables are written by live code but never migrated
(`ai_usage`, `ai_provider_stats`); 1 repository is in-memory only
(`ai_preferences`). 15 distinct table names total.

### 4.3 Complete migration inventory (12 files)

| Migration | Tables/columns created | Notes |
|---|---|---|
| `20260712234229_lifeos_schema.sql` | `saved_items`, `bio_state`, `bot_logs` | RLS: full CRUD granted to `anon`/`authenticated` |
| `20260714111706_create_lifeos_tables.sql` | same 3 tables (`IF NOT EXISTS`) | RLS: SELECT-only granted to `anon`/`authenticated` — **conflicts with the migration above** |
| `20260718143752_20260718_save_ux_redesign.sql.sql` | `saved_items.file_name`, `short_code`; pg_trgm GIN indexes | Comments claim "new saves get a short_code" — **not true in current code** (dead columns) |
| `20260726143924_create_panel_settings_table.sql` | `panel_settings(key, auto_close_enabled, updated_at)` | singleton settings row |
| `20260729213959_20260729120000_create_bot_settings_table.sql` | `bot_settings(key, value, value_type)` | Comments claim it "replaces panel_settings" — **contradicts current code** |
| `20260730210551_20260730235000_add_update_stale_seconds.sql` | `panel_settings.update_stale_seconds` (default 300) | **zero consumers in code** |
| `20260801215007_create_username_state_table.sql` | `username_state` | |
| `20260804145402_create_ai_tables.sql` | `ai_sessions`, `ai_messages`, `ai_memories`, `ai_tool_history` | |
| `20260805075707_20260805120000_create_ai_config_table.sql.sql` | `ai_config` | |
| `20260822090000_create_ghost_chats_table.sql` | `ghost_chats` | **zero code consumers** (orphaned) |
| `20260823120000_add_dashboard_font_and_ghost_seen_settings.sql` | `panel_settings.dashboard_font` (+CHECK), `ghost_seen_retention_days` | |
| `20260823130000_ghost_seen_retention_duration.sql` | `panel_settings.ghost_seen_retention_seconds` (+CHECK, drops `days` column) | **zero consumers in code** |

### 4.4 Web/API database consumers

`backend/web/app.py` (source-verified route list):

- DB-backed read endpoints: `GET /api/saves` (`db_client.list_saves`),
  `GET /api/saves/{save_code}` (`query_save`), `GET /api/bio`
  (`get_bio_state` + username state), `GET /api/settings`
  (`settings_service.get_all` — `panel_settings`-backed), `GET /api/logs`
  (`list_logs`), `GET /api/ai/config` (`config_store`), `GET
  /api/ai/stats` (`usage_reader` — reads `ai_usage`/`ai_provider_stats`,
  which are **unmigrated**, so analytics degrade), `GET /api/db/stats`
  (`observability/db_stats.database_statistics`).
- DB-backed write endpoints: `POST /api/ai/provider`, `POST
  /api/ai/model`, `POST /api/ai/triggers` (write `ai_config`),
  `POST /api/ai/test-models`.
- Runtime-only (no DB): `/api/status`, `/api/health/snapshot`,
  `/api/performance`, `/api/diagnostics/events`, `/api/maintenance`,
  `/api/ai/providers`, `/api/ai/models`, `/api/ai/models/{provider_name}`.

The dashboard is a **read-only consumer**; no dashboard state is
persisted. **EXISTING FACT.**

---

## 5. Database-Dependent Features

Classification legend:

- **DATABASE_REQUIRED** — the feature cannot fulfil its durable contract
  across a process restart without the persisted state, or the current
  implementation explicitly depends on persisted state.
- **DATABASE_USEFUL** — persistence is used today and adds real
  audit/analytics/operational value, but the feature degrades gracefully
  without it.
- **DATABASE_NOT_REQUIRED** — the state is explicitly transient runtime
  state and should not be persisted.
- **UNCERTAIN** — the source is insufficient to determine the correct
  persistence model.

### 5.1 Save System (Deep Save) — **DATABASE_REQUIRED**

- **Evidence:** `backend/services/save_service.py::execute_save` builds a
  `payload` with `save_code`, `save_type='deep'`, `origin_chat_id`,
  `origin_msg_id`, `saved_chat_id`, `saved_msg_id`, `sender_name`,
  `sender_id`, `mime_type`, `file_id`, `file_size`, `media_type`, `tags`,
  `caption`, `owner_id`, `created_at` and calls
  `db_client.insert_save(payload)`; failure to persist logs `"row NOT in
  database"`. The retrieval boundary is the mapping from the human
  `S####` code to the Telegram Saved Messages chat/message IDs.
- **Data requiring persistence:** one row per Deep Save; the code ->
  Saved Messages location mapping must survive restart or Retrieve/Delete/
  Find cannot work.
- **Ownership:** owner-scoped (`owner_id` on every row).
- **Lifecycle:** created on each save; read by retrieve/list/find/stats;
  field update; deleted by Delete engine / orphan cleanup; never expires
  (owner-managed retention).

### 5.2 Retrieve / List / Find — **DATABASE_REQUIRED**

- **Evidence:** `backend/services/retrieve_service.py`,
  `backend/services/discover_service.py`, `db_client.query_save`,
  `list_saves`, `list_recent_saves`, `search_saves` (ILIKE over
  caption/save_code/mime_type).
- **Data:** read-side of `saved_items`. **Ownership:** owner-scoped.
  **Lifecycle:** reads only.

### 5.3 Delete System — **DATABASE_REQUIRED**

- **Evidence:** `backend/services/delete_service.py` +
  `db_client.delete_save`/`delete_save_row`/`cleanup_orphans` (delete by
  `owner_id` + `save_code`, bulk delete by `id` list).
- **Ownership:** owner-scoped deletes only. **Lifecycle:** rows deleted
  explicitly; **no tombstone/history table exists in code** (delete is
  destructive by design).

### 5.4 Database maintenance (orphan clean / stats / vacuum) — **DATABASE_REQUIRED**

- **Evidence:** `backend/services/database_service.py::find_orphans`
  (reads all saves, checks each `saved_chat_id`/`saved_msg_id` against
  Telegram, collects orphans), `do_clean`, `do_stats` (`get_stats`),
  `do_vacuum` — all require `saved_items` + `bot_logs` persistence.

### 5.5 Bio automation — **DATABASE_REQUIRED**

- **Evidence:** `backend/services/bio_service.py` +
  `db_client.get_or_create_bio_state` / `update_bio_state`; row stores
  `template`, `mood`, `custom_text`, `is_active`, `last_bio`,
  `updated_at`. `backend/profile/engine.py` reads state each minute and
  deduplicates against `last_bio` before persisting — the `is_active`
  flag and `last_bio` dedup value must survive restart.
- **Ownership:** one row per owner (`owner_id` UNIQUE). **Lifecycle:**
  lazily created on first start; updated per minute (only when the
  rendered value changes); never deleted by code.

### 5.6 Username automation — **DATABASE_REQUIRED**

- **Evidence:** same pattern as Bio — `username_service.py`,
  `db_client.get_or_create_username_state` / `update_username_state`
  (`template`, `mood`, `custom_text`, `is_active`, `last_name`); dedup
  against `last_name`. **Ownership:** one row per owner. **Lifecycle:**
  as Bio.

### 5.7 Profile scheduler — **DATABASE_USEFUL** (durable half is the state tables)

- **Evidence:** `backend/profile/scheduler.py` is a pure in-memory
  minute-boundary task (`_task`, `_updaters`, `_active_engines`); it
  holds no durable data. The durable enable/disable contract lives in
  `bio_state.is_active` / `username_state.is_active`, persisted by the
  engines on start/stop (`bio_service.py::start/stop`).
- **Note:** boot auto-start comes from env
  `BIO_UPDATE_ENABLED`/`USERNAME_UPDATE_ENABLED` (`config.py`); after
  boot the persisted `is_active` flags are the source of truth for the
  per-minute loop. **The scheduler itself needs no table.**

### 5.8 Ghost Seen / Ghost PV — per-chat allow-list — **DATABASE_REQUIRED**

- **Evidence:** `backend/services/ghost_seen_v2.py`:
  - `_ensure_allowed_loaded_async` reads `bot_settings` key
    `ghost_seen_allowed_chats` (JSON array of chat IDs) once per process;
  - `_persist_allowed_to_db` writes the full set back (update-or-insert)
    on every `allow_chat` / `disallow_chat`.
  - Privacy model is **explicit opt-in**: `is_chat_allowed` returns False
    until loaded; `resolve_allowed_chats` never enumerates dialogs
    (O(allowed) via `get_entity`).
- **Data requiring persistence:** the set of allowed private chat IDs —
  this is the **only durable Ghost Seen state**. It currently lives in a
  `bot_settings` key-value row (single global row; the bot is
  single-owner).
- **Ownership:** owner-scoped (single-owner bot; key is not
  owner-partitioned). **Lifecycle:** created on first allow; updated on
  every toggle; read at process start; never expires.

### 5.9 Ghost Seen — selection / reply / AI state — **DATABASE_NOT_REQUIRED**

- **Evidence:** `ghost_seen_v2.py` module state is explicitly transient:
  `_selections: dict[chat_id, set[msg_id]]`, `_reply_states: dict`,
  `_manage_directory` (TTL cache), `_allowed_loaded` flag. The handler's
  AI Reply flow (`backend/bot/handlers/ghost_seen_v2.py`) keeps locks/
  states in memory and clears them on every terminal path.
- **Reasoning:** message selections and pending reply targets are
  session-scoped UI state; nothing in the code reads them back after a
  restart. Persisting them would invent a requirement. **No per-message
  Ghost table is proposed.**

### 5.10 Ghost Seen retention / destination config — **UNCERTAIN**

- **Evidence:** `panel_settings.ghost_seen_retention_seconds` exists in
  migrations (`20260823130000`) but **no backend code reads it** (grep
  for `ghost_seen_retention` in `backend/` returns nothing; it is absent
  from `settings_service._DEFAULTS`/`_VALIDATORS`).
  `GHOST_SEEN_DESTINATION_CHAT_ID/_NAME` are loaded in `config.py` and
  surfaced by `ghost_seen_v2.get_destination_chat_id/get_destination_
  chat_name`, but those functions have **zero callers** in `backend/`.
- **Why UNCERTAIN:** persistence exists but nothing consumes it — the
  feature that would require these values is not connected in this
  repository. `REQUIRES SOURCE VERIFICATION`.

### 5.11 Font system — registry **DATABASE_NOT_REQUIRED**; selection **DATABASE_REQUIRED**

- **Evidence:** `backend/helper/font_style.py` defines the 23-key font
  registry in code (`_FONT_REGISTRY` — key, label, transform,
  `has_digit_glyphs`). No DB stores font *definitions*; they cannot
  change at runtime and must not be duplicated into a table.
- The **selected font** is persisted: `settings_service.set_dashboard_font`
  → `panel_settings.dashboard_font` (validated against `DASHBOARD_FONTS =
  FONT_KEYS`); consumed by `dashboard_font()` (cache-first) for the
  dashboard and the profile engines (`profile/engine.py::render` →
  `apply_font`).
- **Ownership:** global (one `panel_settings` row). **Lifecycle:**
  updated on selection; read on every render.

### 5.12 Glass panel settings — **DATABASE_REQUIRED**

- **Evidence:** `backend/services/settings_service.py` — 12 typed settings
  (`auto_close_enabled`, `auto_close_delay`, `max_deep_save_mb`,
  `delete_batch_size`, `log_retention_days`, `panel_timeout_seconds`,
  `allow_multiple_panels`, `reuse_existing_panel`, `language`,
  `debug_callbacks`, `owner_only`, `dashboard_font`) with validators,
  write-through cache → `panel_settings_repository` (singleton row
  `key='global'`). Loaded once at startup (`load_all`).
- **Ownership:** global row. **Lifecycle:** read on every panel render
  (from cache), written on setting change, never deleted.

### 5.13 AI configuration — **DATABASE_REQUIRED**

- **Evidence:** `backend/ai/config_store.py` — one `ai_config` row per
  owner (`provider`, `model`, `temperature`, `max_tokens`,
  `system_prompt`, `history_budget`, `is_configured`, `trigger_en`,
  `trigger_fa`, `last_request_at`, `last_latency_ms`). Read by
  `backend/bot/handlers/ai_unified.py` on every AI activation; written by
  the setup wizard and `record_request` (latency-only targeted update).
- **Ownership:** per-owner (UNIQUE `owner_id`). **Lifecycle:** created by
  setup, updated on config change + every request telemetry update,
  never deleted.

### 5.14 AI sessions & messages — **DATABASE_REQUIRED**

- **Evidence:** `backend/ai/runtime/manager.py` (ConversationManager):
  every added message persists via `persistence.add_message`
  (fire-and-forget `guarded_create_task`, `_add_message`), and
  `restore_history` rebuilds RAM history from `persistence.get_messages`
  after a restart — **explicit restart-continuity requirement**.
  `ai_sessions` rows are created/updated via
  `persistence.create_session/update_session`.
- **Ownership:** owner-scoped (`owner_id` + `session_id`). **Lifecycle:**
  created per conversation, messages appended, restored on restart,
  **no expiry/cleanup code exists** for old sessions/messages.

### 5.15 AI memory — **DATABASE_REQUIRED**

- **Evidence:** `backend/ai/engine/engine.py` uses
  `get_repository_manager().memory`; `SupabaseMemoryRepository` delegates
  to `persistence._save_memory_sync/_query_memories_sync/
  _delete_expired_memories_sync/_count_memories_sync` (`ai_memories`:
  owner_id, tier, category, content, importance, expires_at, metadata).
  `backend/runtime/memory_cleanup.py` prunes expired rows.
- **Ownership:** owner-scoped. **Lifecycle:** created on memory save,
  read for context injection, expired rows deleted (expiry-driven),
  explicit delete by id.

### 5.16 AI tool history — **DATABASE_USEFUL**

- **Evidence:** `backend/ai/tools/executor.py` calls
  `persistence.record_tool_call` (audit: tool name, args, result,
  latency). **Value:** audit/debug/analytics only; a failure is logged
  and does not affect execution. **No retention/cleanup code exists.**

### 5.17 AI usage tracking — **DATABASE_USEFUL** (currently unmigrated)

- **Evidence:** `backend/ai/engine/dispatcher.py` schedules
  `usage_recorder.record_usage` → `SupabaseUsageRepository.create`
  (`ai_usage`: owner_id, session_id, provider, model, prompt/completion/
  total tokens, latency_ms, token_source). Read side: `usage_reader.py`
  (total/daily/recent/summary), surfaced in `backend/web/app.py::GET
  /api/ai/stats` and the Database panel stats.
- **Critical fact:** `ai_usage` has **no migration** — writes degrade to
  logged failures when the table is absent (safe, but analytics are
  then empty).
- **Lifecycle:** append-only today; **no retention/cleanup code exists.**

### 5.18 Provider stats — **DATABASE_USEFUL** (currently unmigrated)

- **Evidence:** `provider_stats_repository.py::record_request` does a
  read-modify-write upsert on `ai_provider_stats` (`on_conflict=
  provider_name,owner_id`). Same unmigrated status as `ai_usage`.
- **Lifecycle:** upsert per request; no cleanup.

### 5.19 AI preferences — **UNCERTAIN**

- **Evidence:** `backend/ai/database/preferences_repository.py` defines
  `ai_preferences` semantics (language, personality, response_style,
  custom_instructions, auto_memory, auto_tools) and is invoked by
  `backend/ai/engine/dispatcher.py` (`get_or_create` for default
  personality wiring). But the manager instantiates
  **InMemoryPreferencesRepository only**, there is **no migration** and
  **no UI/handler writes preferences**.
- **Why UNCERTAIN:** the interface exists and the dispatcher reads it, but
  no durable producer exists, so the schema cannot be derived from code
  with confidence. `REQUIRES SOURCE VERIFICATION`.

### 5.20 Provider health / cooldown / fallback — **DATABASE_NOT_REQUIRED**

- **Evidence:** `backend/ai/providers/manager/manager.py` holds
  `_health = ProviderHealthTracker(...)`, `_fallback_chain`, cooldown
  state — all in-memory runtime health (`manager/health.py`). Nothing
  reads provider health from a DB; it is intentionally ephemeral (a
  persisted stale quarantine would be worse than none).

### 5.21 Provider registry / capabilities — **DATABASE_NOT_REQUIRED**

- **Evidence:** provider registry, model lists, and capabilities are
  code-defined (`providers/registry/`, `providers/base/capabilities.py`).

### 5.22 `bot_logs` — **DATABASE_USEFUL**

- **Evidence:** `db_client.log` appends rows; `list_logs`/`count_logs`/
  `clean_logs(owner_id, days)` exist; `log_retention_days` (default 7)
  is consumed by `backend/services/organize_service.py` to trim old
  logs. Degrades gracefully when the DB is down.
- **Lifecycle:** append + periodic age-based deletion.

### 5.23 Panel input state / timers / panel sessions — **DATABASE_NOT_REQUIRED**

- **Evidence:** `backend/helper/input_state.py` (`_pending` dict, 120s
  expiry), `backend/helper/panel_timer.py` (`_panels` dict, RAM),
  `helper/session_manager.py` — all transient UI state keyed by
  chat/message IDs. Lost on restart by design; nothing reads them back.

### 5.24 Owner / user identity — **DATABASE_NOT_REQUIRED** as a separate table

- **Evidence:** `BOT_OWNER_ID` (`config.py` REQUIRED) + `is_owner` guard
  (`backend/bot/handlers/guard.py`). Every owner-owned table is scoped by
  an `owner_id bigint` column (Telegram user ID); there is **no users
  table and no code path needing one** — the self-bot is single-owner.

### 5.25 Runtime supervision / diagnostics — **DATABASE_NOT_REQUIRED**

- **Evidence:** heartbeat, failsafe, keepalive, crash diagnostics
  (`backend/runtime/*`, `backend/diagnostics.py`) write only in-memory/
  log telemetry (`bot_logs` optional). No durable state.

---

## 6. Features That Do Not Need Database Persistence

| Feature | Why persistence is unnecessary (source evidence) |
|---|---|
| Ghost Seen message selections / reply states | Explicitly transient UI state (`_selections`, `_reply_states` in `ghost_seen_v2.py`); cleared on every terminal path; nothing restores them |
| Ghost Seen manage-directory cache | TTL cache (`_MANAGE_DIRECTORY_TTL_S = 60`); rebuilt from Telegram |
| Provider health / cooldown / quarantine | Ephemeral runtime health (`providers/manager/health.py`); self-heals; must NOT survive restart or stale quarantine would stick |
| Provider registry / model lists / capabilities | Code-defined constants (`providers/registry/`, `base/capabilities.py`) |
| Font definitions (`_FONT_REGISTRY`) | Code-defined; cannot change at runtime |
| Panel input listeners / timers | 120s-expiry input state and RAM panel timers are per-session UI state |
| Conversation runtime sessions (RAM) | Rebuilt/restored from `ai_messages`; the in-RAM registry itself is transient |
| Trigger-word cache (`ai_unified._trigger_cache`, 30s TTL) | Cache; source of truth is `ai_config` |
| Diagnostics ring / crash snapshots | In-memory telemetry; `bot_logs` is the optional durable sink |
| Owner identity | Single owner from env (`BOT_OWNER_ID`); `owner_id` column everywhere is sufficient |

---

## 7. Proposed / Required Database Entities

Format: **Entity — Status** (`EXISTING` = migrated and code-wired,
`EXISTING/ORPHAN` = migrated but unconsumed, `PROPOSED` = required by
code but not yet migrated, `PROPOSED/OPTIONAL` = defensible but needs a
decision, `UNCERTAIN` = do not design yet). Columns are derived strictly
from the code paths that read or write them. No schema is being applied
in this phase — this documents the requirements a future schema must
satisfy.

### 7.1 `saved_items` — EXISTING (keep as-is)

- **Purpose:** the Save/Retrieve/Delete/Find/Database-maintenance core —
  one row per Deep Save mapping a human `S####` code to the Telegram
  Saved Messages location. **Source:** `save_service.py::execute_save`
  (write), `retrieve_service.py`/`discover_service.py`/`delete_service.py`
  /`database_service.py` (read/delete).
- **Columns (verified against migration + insert payload):**

| Column | Type (suggested) | Null | Default | Purpose |
|---|---|---|---|---|
| `id` | bigserial | NO | — | PK |
| `save_code` | text | NO | — | UNIQUE human code (`S####`, e.g. `S0001`); lookup key |
| `save_type` | text | NO | `'forward'` | CHECK (`forward`,`deep`); **code always writes `'deep'`** |
| `origin_chat_id` | bigint | YES | — | source chat of the saved message |
| `origin_msg_id` | bigint | YES | — | source message ID |
| `saved_chat_id` | bigint | YES | — | Saved Messages chat ID (orphan check) |
| `saved_msg_id` | bigint | YES | — | Saved Messages message ID (orphan check) |
| `sender_name` | text | YES | — | display name |
| `sender_id` | bigint | YES | — | Telegram user ID |
| `mime_type` | text | YES | — | search field |
| `file_id` | text | YES | — | Telegram file reference |
| `file_size` | bigint | YES | — | stats |
| `media_type` | text | YES | — | stats grouping (`Photo`, `Video`, …) |
| `tags` | text[] | YES | `'{}'` | auto tags |
| `caption` | text | YES | — | search field |
| `file_name` | text | YES | — | **migrated, never written by code** (dead) |
| `short_code` | text | YES | — | **migrated, never written by code** (dead) |
| `owner_id` | bigint | NO | — | ownership scope |
| `created_at` | timestamptz | YES | `now()` | ordering |

- **Keys:** PK `id`; UNIQUE `save_code`; UNIQUE partial `short_code`.
- **Indexes justified by access:** `(owner_id)`; `(owner_id,
  created_at DESC)` (recent-list path); `(save_code)` (code lookup);
  GIN trigram on caption/file_name/save_code/short_code/mime_type
  (search path).
- **Lifecycle:** insert per save; read by code/stats; single-field
  update; delete by owner+code or bulk by id; no expiry.
- **Ownership:** owner.

### 7.2 `bio_state` — EXISTING (keep as-is)

- **Purpose:** durable per-owner Bio automation state (template, mood,
  active flag, `last_bio` dedup). **Source:** `bio_service.py` +
  `profile/engine.py` + `db/client.py`.
- **Columns:** `id` (PK), `owner_id` (UNIQUE NOT NULL), `template`
  (default `🕒 {time} | 💭 {mood}`), `mood`, `custom_text`, `is_active`,
  `last_bio`, `updated_at`.
- **Index:** `(owner_id)`. **Lifecycle:** lazy create, per-minute
  updates, never deleted. **Ownership:** owner.

### 7.3 `username_state` — EXISTING (keep as-is)

- Mirror of `bio_state` with `last_name`; single row per owner.
  **Source:** `username_service.py` + `db/client.py`.

### 7.4 `bot_logs` — EXISTING (keep as-is)

- **Purpose:** structured log sink (7-day default retention via
  `log_retention_days`, consumed by `organize_service.py`).
  **Source:** `db/client.py` (log/list/count/clean),
  `runtime/startup_check.py`.
- **Columns:** `id` (PK), `owner_id`, `level` (CHECK INFO/WARN/ERROR),
  `message`, `context` (jsonb), `created_at`.
- **Indexes:** `(owner_id)`, `(created_at DESC)`. **Lifecycle:** append +
  age-based delete. **Ownership:** owner.

### 7.5 `panel_settings` — EXISTING (keep; two columns unconsumed)

- **Purpose:** singleton global Glass/dashboard settings
  (column-per-setting, `key='global'`). **Source:**
  `settings_service.py` + `panel_settings_repository.py`.
- **Columns (12 wired):** `key` (PK), `auto_close_enabled`,
  `auto_close_delay`, `max_deep_save_mb`, `delete_batch_size`,
  `log_retention_days`, `panel_timeout_seconds`, `allow_multiple_panels`,
  `reuse_existing_panel`, `language`, `debug_callbacks`, `owner_only`,
  `dashboard_font` (CHECK against 23 font keys), `updated_at`.
- **Columns (unconsumed):** `update_stale_seconds` (migrated
  `20260730210551`, zero readers), `ghost_seen_retention_seconds`
  (migrated `20260823130000`, zero readers).
- **Lifecycle:** loaded at boot into cache; write-through on change.
  **Ownership:** global.

### 7.6 `bot_settings` — EXISTING (used for ONE key; migration intent stale)

- **Purpose:** key-value store. Actual usage: only
  `ghost_seen_allowed_chats` (JSON list of allowed chat IDs), read at
  boot, written on every allow/disallow. **Source:**
  `ghost_seen_v2.py::_ensure_allowed_loaded_async` /
  `_persist_allowed_to_db`.
- **Columns:** `key` (PK), `value` (text NOT NULL), `value_type`
  (default `str`), `updated_at`.
- **Lifecycle:** upsert on toggle. **Ownership:** global (single owner).

### 7.7 `ai_config` — EXISTING (keep as-is)

- **Purpose:** per-owner AI setup (provider/model/params/triggers) +
  last-request telemetry. **Source:** `ai/config_store.py`,
  `bot/handlers/ai_unified.py`, `bot/handlers/ai.py`,
  `web/app.py` POST endpoints.
- **Columns:** `id` (PK), `owner_id` (UNIQUE), `provider`, `model`,
  `temperature`, `max_tokens`, `system_prompt`, `history_budget`,
  `is_configured`, `trigger_en`, `trigger_fa`, `last_request_at`,
  `last_latency_ms`, `created_at`, `updated_at`.
- **Index:** `(owner_id)`. **Lifecycle:** created by setup wizard,
  updated on change + `record_request` (latency-only); never deleted.
  **Ownership:** owner.

### 7.8 `ai_sessions` — EXISTING (keep as-is)

- **Purpose:** conversation session metadata. **Source:**
  `ai/persistence.py` + `ai/runtime/manager.py` (ConversationManager).
- **Columns:** `id` (PK), `session_id` (UNIQUE), `owner_id`, `provider`,
  `model`, `status` (CHECK active/completed/error/closed),
  `total_tokens`, `message_count`, `created_at`, `updated_at`.
- **Indexes:** `(owner_id)`, `(session_id)`. **Lifecycle:** created via
  `persistence.create_session`, updated on message add; no cleanup code.
  **Ownership:** owner.

### 7.9 `ai_messages` — EXISTING (keep as-is)

- **Purpose:** per-session conversation history — the restart-recovery
  source (`restore_history` rebuilds RAM from it). **Source:**
  `ai/persistence.py` (`_add_message_sync`, `_get_messages_sync`).
- **Columns:** `id` (PK), `session_id`, `owner_id`, `role` (CHECK
  system/user/assistant/tool), `content` (truncated to 8000 chars at
  write), `token_count`, `provider`, `model`, `created_at`.
- **Indexes:** `(session_id)`, `(owner_id)`, `(created_at DESC)`.
  **Lifecycle:** append-only; restore on session recovery; no cleanup.
  **Ownership:** owner.

### 7.10 `ai_memories` — EXISTING (keep as-is)

- **Purpose:** three-tier (short/long/permanent) memory for context
  injection. **Source:** `ai/persistence.py`,
  `ai/database/memory_repository.py`, `ai/engine/engine.py`,
  `runtime/memory_cleanup.py`.
- **Columns:** `id` (PK), `owner_id`, `tier` (CHECK), `category` (CHECK),
  `content` (≤8000 chars), `importance` (real), `expires_at`
  (timestamptz, nullable), `metadata` (jsonb), `created_at`.
- **Indexes:** `(owner_id)`, `(tier)`, `(owner_id, tier)`. **Lifecycle:**
  insert on memory save; query by owner/tier/category/importance; delete
  expired by tier; explicit delete by id. **Ownership:** owner.

### 7.11 `ai_tool_history` — EXISTING (keep as-is)

- **Purpose:** audit log of AI tool calls. **Source:**
  `ai/tools/executor.py` → `persistence.record_tool_call`.
- **Columns:** `id` (PK), `owner_id`, `session_id`, `tool_name`,
  `arguments` (jsonb), `result_success`, `result_message` (≤2000),
  `result_data` (jsonb), `latency_ms`, `created_at`.
- **Indexes:** `(owner_id)`, `(created_at DESC)`. **Lifecycle:**
  append-only; no cleanup code. **Ownership:** owner.

### 7.12 `ai_usage` — PROPOSED (code-wired, **no migration exists**)

- **Purpose:** per-request token/latency records for analytics
  (`usage_reader.py`, `GET /api/ai/stats`, Database panel).
  **Source:** `dispatcher.py` → `usage_recorder.py` →
  `SupabaseUsageRepository.create`.
- **Columns (from `SupabaseUsageRepository.create`):**

| Column | Type (suggested) | Null | Purpose |
|---|---|---|---|
| `id` | uuid/text | NO | PK (uuid4 generated in `usage_recorder`) |
| `owner_id` | bigint | NO | scope |
| `session_id` | text | NO | join to `ai_sessions` |
| `provider` | text | NO | provider name |
| `model` | text | NO | model |
| `prompt_tokens` | integer | NO | provider-reported input tokens |
| `completion_tokens` | integer | NO | provider-reported output tokens |
| `total_tokens` | integer | NO | sum |
| `latency_ms` | real | NO | request latency |
| `token_source` | text | NO | honesty label: actual/estimated/unavailable |
| `created_at` | timestamptz | NO | record time |

- **Indexes (justified by access):** `(owner_id, created_at DESC)`
  (recent list), `(owner_id, created_at)` (daily window), `(owner_id)`
  (total/count).
- **Lifecycle:** append-only; needs an explicit retention decision.
  **Ownership:** owner. **Why it exists:** three live consumers.

### 7.13 `ai_provider_stats` — PROPOSED (code-wired, **no migration exists**)

- **Purpose:** per-(provider, owner) aggregates (success/failure/tokens/
  latency) for analytics. **Source:**
  `provider_stats_repository.py::record_request` (upsert),
  `usage_reader.provider_stats`.
- **Columns (from `ProviderStatsRecord.as_dict`):** `provider_name`
  (text), `owner_id` (bigint), `total_requests`, `successful_requests`,
  `failed_requests`, `total_prompt_tokens`, `total_completion_tokens`,
  `avg_latency_ms`, `last_request_at`, `updated_at`.
- **Keys:** UNIQUE `(provider_name, owner_id)` (upsert
  `on_conflict="provider_name,owner_id"`).
- **Indexes:** the unique constraint covers the lookups.
- **Lifecycle:** upsert per request. **Ownership:** owner.

### 7.14 `ai_preferences` — PROPOSED/OPTIONAL (in-memory only today)

- **Purpose:** per-owner AI preferences (language, personality,
  response_style, custom_instructions, auto_memory, auto_tools).
  **Evidence:** `preferences_repository.py` + dispatcher
  `get_or_create()`; manager uses `InMemoryPreferencesRepository`.
- **Status:** **REQUIRES SOURCE VERIFICATION** — no durable producer, no
  migration, no UI. Recommend deferring the schema until a real producer
  exists.

### 7.15 `ghost_chats` — EXISTING/ORPHAN → decision needed

- **Purpose today:** none — migrated (`20260822090000`) with **zero code
  consumers** (grep of `backend/` for `ghost_chats` returns nothing).
  **EXISTING FACT.**
- **Why it exists in the repo:** the legacy Ghost Room design (per the
  migration comment); the current implementation persists the allow-list
  in `bot_settings.ghost_seen_allowed_chats`.
- **Two defensible directions (ARCHITECTURAL RECOMMENDATION, not a
  schema change):**
  1. **Repurpose** as the per-chat Ghost PV privacy table: add `owner_id`
     (bigint NOT NULL) and `allowed` (bool NOT NULL DEFAULT false), keep
     `chat_id` + display metadata, PK `(owner_id, chat_id)`, backfill
     from `bot_settings.ghost_seen_allowed_chats`, then retire the KV
     key. This matches the real query pattern (`is_chat_allowed(chat_id)`
     existence check, `resolve_allowed_chats` per-owner listing).
  2. **Drop** the table and keep the `bot_settings` KV key.
- **Explicitly NOT proposed:** a per-message Ghost table. Message
  selections/previews/reply/AI state are transient in the code; nothing
  requires message persistence.

### 7.16 `panel_settings` unconsumed additions (retention/stall) — UNCERTAIN

- `update_stale_seconds` and `ghost_seen_retention_seconds` are migrated
  but have zero readers. No columns or indexes are proposed until a
  consumer exists.

---

## 8. Query and Access Patterns

Derived from the actual code (function → pattern). Only indexes justified
by these patterns are proposed in §7; no "just in case" indexes.

| Pattern | Code evidence |
|---|---|
| Owner-scoped lookup by `save_code` | `db_client._query_save_sync` → `eq("save_code", code).maybe_single()` |
| Owner-scoped recent list + count | `_list_saves_sync` → `eq(owner_id).order(created_at, desc).range()` + `count="exact"` |
| Owner-scoped search (caption/code/mime ILIKE) | `_search_saves_sync` → `or_("caption.ilike.%q%,save_code.ilike.%q%,mime_type.ilike.%q%")` |
| Owner-scoped delete by code / bulk by id | `_delete_save_sync`, `_cleanup_orphans_sync` → `eq(owner_id).eq(save_code)` / `.in_("id", ids)` |
| Save-code existence check | `_is_code_free_sync` → `eq("save_code", code).limit(1)` |
| Per-owner state singleton | `bio_state`/`username_state`/`ai_config` → `eq("owner_id").maybe_single()` |
| Session message replay (restart recovery) | `persistence._get_messages_sync` → `eq("session_id").order(created_at, asc).limit()` |
| Session message count | `persistence._add_message_sync` → `select(id, count="exact").eq("session_id")` |
| Memory query by tier/category/importance | `persistence._query_memories_sync` → `eq(owner_id).eq(tier).eq(category).gte(importance).order(importance, desc)` |
| Expired memory sweep | `_delete_expired_memories_sync` → `eq("tier").lt("expires_at", now)` |
| Usage totals / daily window / recent | `usage_repository` → `eq(owner_id)`; `gte/lte created_at`; `order created_at desc limit` |
| Provider stats upsert | `provider_stats_repository` → `upsert(on_conflict="provider_name,owner_id")` |
| KV settings by key | `ghost_seen_v2` → `eq("key", "ghost_seen_allowed_chats").maybe_single()` |
| Singleton panel settings row | `panel_settings_repository` → `eq("key", "global").maybe_single()` |
| Log read + age-based delete | `db_client` → `eq(owner_id).order(created_at, desc)` / `eq(owner_id).lt(created_at, cutoff)` |

---

## 9. Entity Relationships

```
owner (BOT_OWNER_ID env, single)
 ├── saved_items          (owner_id)            ── save_code UNIQUE
 ├── bio_state            (owner_id UNIQUE)
 ├── username_state       (owner_id UNIQUE)
 ├── bot_logs             (owner_id)
 ├── ai_config            (owner_id UNIQUE)
 ├── ai_sessions          (owner_id, session_id UNIQUE)
 │     ├── ai_messages    (owner_id, session_id) ── session_id → ai_sessions.session_id
 │     ├── ai_tool_history(owner_id, session_id) ── session_id → ai_sessions.session_id (loose)
 │     └── ai_usage       (owner_id, session_id) ── session_id → ai_sessions.session_id (loose)
 ├── ai_memories          (owner_id)
 ├── ai_provider_stats    (owner_id, provider_name UNIQUE pair)
 └── ghost_chats*         (owner_id + chat_id — proposed; no consumer today)

global (no owner)
 ├── panel_settings       (key='global' singleton)
 └── bot_settings         (key KV; ghost_seen_allowed_chats)
```

- The schema deliberately uses **no foreign keys** (documented pattern in
  every migration: "independent (no foreign keys) to match the existing
  schema pattern"). `session_id` joins are application-level.
- `owner_id` is the universal scope column; there is no owner table.
- `saved_chat_id`/`saved_msg_id` reference Telegram entities, not rows.

---

## 10. Code-Level Database Inconsistencies

Reported **without fixing** (Phase 1 boundary):

1. **`ai_usage` / `ai_provider_stats` are written in production code but
   have no migration.** `dispatcher.py` schedules
   `usage_recorder.record_usage` → `SupabaseUsageRepository.create`
   (`ai_usage`) and `SupabaseProviderStatsRepository.record_request`
   (`ai_provider_stats`), but no file under `supabase/` creates either
   table. When the tables are absent the writes fail silently (logged),
   so the Database-panel "AI usage rows" statistic reports
   `Unavailable` (`database_service._ai_database_counts`) and the
   dashboard AI stats are empty — yet the code clearly expects the
   tables.
2. **`ai_preferences` has a repository + dispatcher consumer but is
   in-memory only.** `manager.py` always instantiates
   `InMemoryPreferencesRepository`; there is no migration and no durable
   producer. The dispatcher's `get_or_create()` returns fresh defaults
   on every restart.
3. **`ghost_chats` is fully orphaned.** Migrated with a doc comment
   referencing the architecture doc, but zero references exist in
   `backend/` code. The live Ghost Seen privacy state is stored in
   `bot_settings.ghost_seen_allowed_chats` instead.
4. **`bot_settings` migration intent contradicts current code.** Migration
   `20260729213959` states *"Replaces the panel_settings table"* and
   *"settings_service reads from bot_settings going forward"*, but the
   current `settings_service.py` reads/writes `panel_settings` columns via
   `panel_settings_repository.py`, and `bot_settings` is used only for the
   Ghost Seen allow-list key. Documentation-vs-code drift (migration
   comments are documentation).
5. **`panel_settings.update_stale_seconds` and
   `ghost_seen_retention_seconds` are migrated but unconsumed.** The
   `update_stale_seconds` migration claims the watchdog reads the value
   via `settings_service` on every tick — no such read exists (grep
   returns nothing; both columns are absent from
   `settings_service._DEFAULTS`/`_VALIDATORS`, so there is not even a
   typed accessor).
6. **`saved_items.short_code` and `file_name` are dead columns.** The
   save payload (`save_service.py`) never writes them; lookups use
   `save_code` only. The `20260718143752` migration comments claim
   *"new saves get a short_code"* — not true in current code.
7. **`saved_items.save_type` CHECK allows `'forward'` but the code only
   ever writes `'deep'`** (Deep Save only per AGENTS.md). The CHECK
   constraint is wider than the implemented contract.
8. **Save-code generation counts across all owners.**
   `db_client._count_saves_sync` issues `select(id, count="exact")` with
   **no** `owner_id` filter, so the sequential `S####` sequence is shared
   globally. Harmless in a single-owner bot, but an ownership-semantics
   inconsistency worth recording.
9. **Two migrations create the same three tables with conflicting RLS
   posture.** `20260712234229` grants anon/authenticated full CRUD on
   `saved_items`/`bio_state`/`bot_logs`; `20260714111706` grants
   SELECT-only. Both are `IF NOT EXISTS`, so the surviving policy set
   depends on apply order — the schema as documented is ambiguous.
10. **`ai_sessions.message_count` is computed with a nested count query**
    inside the same call that updates the row
    (`persistence._add_message_sync`) — a read-your-writes hazard if the
    insert and count diverge; also redundant with `ai_messages` (can be
    derived). Minor.
11. **`GHOST_SEEN_DESTINATION_CHAT_ID/_NAME` are loaded in `config.py`
    and surfaced by `ghost_seen_v2.get_destination_chat_id/name` but
    have zero callers** — dead configuration in the runtime config
    contract.
12. **Migration filename double-extension artifacts:** two migrations end
    in `.sql.sql` (`20260718143752_20260718_save_ux_redesign.sql.sql`,
    `20260805075707_20260805120000_create_ai_config_table.sql.sql`) —
    cosmetic, but they make the migration inventory harder to trust.
13. **Duplicate timestamp helpers:** `backend/bio/engine.py::_get_tz`
    (used by `save_service`/`database_service`) vs
    `backend/profile/engine.py::get_tz` and
    `backend/profile/scheduler.py::get_tz` — three near-identical
    timezone fallback implementations. Not a DB defect, but a
    maintenance-cost duplication.

---

## 11. Unresolved Questions

1. **`ghost_chats` intent** — is the migrated table meant to become the
   per-chat Ghost PV privacy store (needs `owner_id` + `allowed` + code
   rewiring), or should it be removed? No code answers this.
   (`REQUIRES SOURCE VERIFICATION` — no consumer exists.)
2. **`ghost_seen_retention_seconds` / `update_stale_seconds` intent** —
   who is the intended consumer? Neither `settings_service` nor any
   runtime module reads them. The migration comments describe features
   that are not connected in this repository.
3. **`ai_usage` / `ai_provider_stats` schema** — the write-side columns
   are fully specified by the repositories, but there is no migration to
   confirm types/defaults/indexes against. Whether to create them now is
   a later-phase decision.
4. **`ai_preferences` schema** — interface exists, durable producer does
   not. Cannot confidently design columns beyond the dataclass fields.
5. **`bot_settings` vs `panel_settings`** — which is the intended general
   settings store? Migration intent says `bot_settings`; code says
   `panel_settings`. A consolidation decision is required before either
   is touched.
6. **Retention/expiry policy** — `ai_sessions`, `ai_messages`,
   `ai_tool_history`, `ai_usage`, `ai_provider_stats` have no cleanup
   code; `bot_logs` has 7-day default cleanup. Is unbounded AI-table
   growth acceptable?
7. **Multi-owner semantics** — everything is keyed by `owner_id` with no
   users table and RLS policies that expose all rows to anon/
   authenticated dashboard reads. For a single-owner self-bot this is
   consistent, but the boundary is implicit, not enforced.
8. **RLS policy drift (item 9 above)** — which policy set is the
   production truth?
9. **`save_type`/`short_code`/`file_name`** — keep the wider CHECK and
   dead columns for forward-compat, or tighten? (Future phase; code only
   needs `save_type='deep'` and `save_code`.)

---

## 12. Phase 1 Findings / Recommendations

### 12.1 Findings (all EXISTING FACT)

- The repository has **one database authority** for core tables
  (`db/client.py`) and separate, consistent accessors for `ai_config`,
  the AI persistence tables, and `panel_settings`. No competing access
  layer exists.
- **11 of 12 migrated tables are live**; `ghost_chats` is orphaned.
- **2 tables are written by production code but never migrated**
  (`ai_usage`, `ai_provider_stats`); **1 repository is in-memory only**
  (`ai_preferences`).
- The **only durable Ghost Seen state** is the per-chat privacy
  allow-list, stored in `bot_settings.ghost_seen_allowed_chats`.
- Font **definitions live in code**; only the **selection** persists
  (`panel_settings.dashboard_font`).
- AI conversation continuity depends on `ai_messages` (restore-after-
  restart is an explicit code path).

### 12.2 Recommendations (ARCHITECTURAL RECOMMENDATION — not implemented)

- **Keep unchanged (compat constraints):** `saved_items`, `bio_state`,
  `username_state`, `bot_logs`, `panel_settings` (12 wired columns),
  `ai_config`, `ai_sessions`, `ai_messages`, `ai_memories`,
  `ai_tool_history`.
- **Create (code already writes them):**
  - `ai_usage` — columns/indexes per §7.12, append-only, with an
    explicit retention decision.
  - `ai_provider_stats` — per §7.13, UNIQUE `(provider_name, owner_id)`.
- **Decide (no code dependency either way yet):**
  - `ghost_chats` — either repurpose as the owner-scoped per-chat Ghost
    PV table (§7.15) and retire the `bot_settings` KV key, or drop it.
  - `ai_preferences` — defer until a durable producer exists.
  - `panel_settings.update_stale_seconds` /
    `ghost_seen_retention_seconds` — keep the columns (already shipped)
    but do not design around them until a consumer exists.

### 12.3 Security boundary (verified)

- **No credentials in the database.** The DB stores no Telegram session
  strings, no Telegram API credentials, no bot tokens, no provider API
  keys. Secrets live in env (`config.py` REQUIRED vars: `API_ID`,
  `API_HASH`, `SESSION_STRING`, `BOT_OWNER_ID`; provider keys via
  `os.getenv`); no `db/client.py` or repository insert payload contains
  them. **EXISTING FACT.**
- **Ownership semantics:** every owner-owned table carries an
  `owner_id bigint` column; single-owner identity comes from
  `BOT_OWNER_ID` + the `is_owner` handler guard. There is no users table.
- **RLS posture:** migrations grant `anon`/`authenticated` access; all
  writes go through the service-role key (`db/client.py`). Two
  core-table migrations carry **conflicting** RLS grants (see §10.9).
- **Execution authority:** the database stores state/history only. All
  Telegram execution remains in the Self Bot runtime (typed wrappers in
  `telegram_api/`). There is **no arbitrary Telegram RPC executor** and
  **no AI-accessible SQL executor** in the tool layer — AI tools are
  domain wrappers over services (save, retrieve, delete, bio, username,
  organize, settings, web_search).

---

## 13. Executive Summary

- **Database-dependent features identified:** **25** investigated areas
  classified as: **12 DATABASE_REQUIRED** (Save, Retrieve/List/Find,
  Delete, DB maintenance, Bio, Username, Ghost Seen allow-list, Glass
  panel settings, AI config, AI sessions, AI messages, AI memory — plus
  font *selection*), **5 DATABASE_USEFUL** (profile scheduler's durable
  half, AI tool history, AI usage, provider stats, `bot_logs`),
  **2 UNCERTAIN** (Ghost retention/destination config, AI preferences),
  and **7 DATABASE_NOT_REQUIRED** (Ghost selection/reply/AI state, font
  *definitions*, provider health, provider registry, panel input/timers,
  owner identity as a table, runtime supervision).
- **Entities:** **16** documented — **11 EXISTING** live tables,
  **1 EXISTING/ORPHAN** (`ghost_chats`), **2 PROPOSED** (`ai_usage`,
  `ai_provider_stats` — code-wired, never migrated), **1
  PROPOSED/OPTIONAL** (`ai_preferences`), **1 UNCERTAIN** (unconsumed
  `panel_settings` columns). No per-message Ghost table is proposed —
  the code explicitly keeps Ghost selections/reply/AI state transient.
- **Most important database requirements:** (1) `saved_items` is the
  Save/Retrieve/Delete backbone keyed by `S####` codes; (2) the Ghost
  Seen **allow-list** is the only durable Ghost state (currently a
  `bot_settings` KV key); (3) font *definitions* stay in code while only
  the *selection* persists (`panel_settings.dashboard_font`); (4) AI
  config/sessions/messages/memories are the durable AI core with an
  explicit restart-recovery path.
- **Most important inconsistencies:** `ai_usage` + `ai_provider_stats`
  written by production code but **never migrated**; `ghost_chats`
  orphaned; `bot_settings` migration intent vs actual `panel_settings`
  usage; two unconsumed `panel_settings` columns; dead
  `saved_items.short_code`/`file_name`; conflicting RLS posture across
  the two core-table migrations.
- **Most important unresolved questions:** the intended consumer for the
  two unconsumed panel columns, the fate of `ghost_chats`/`bot_settings`
  consolidation, `ai_preferences` schema, and the missing AI-table
  retention policy.

---

## 14. Phase Boundary

This phase performed **investigation and reporting only**:

- ✅ No application code changed (handlers, services, repositories,
  runtime).
- ✅ No database schema changed; no migration created or applied; no SQL
  executed.
- ✅ No `DATABASE_ARCHITECTURE.md` reconciliation performed (a later
  phase).
- ✅ No external/Hermes repository inspected; nothing was assumed from
  another repository. Items that depend on other systems are recorded as
  `REQUIRES SOURCE VERIFICATION` / unresolved (§11).
- ✅ The only repository change from this phase is this file —
  `INVESTIGATION.md`, the investigation handoff artifact.

The next phase (schema reconciliation against `DATABASE_ARCHITECTURE.md`
and the confirmed fixes from §10/§12) is **not** part of this phase.

*End of Phase 1.*
