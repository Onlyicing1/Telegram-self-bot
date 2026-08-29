# Database Architecture — LifeOS Telegram Self-Bot

> **Canonical specification:** This document defines the final PostgreSQL/Supabase contract for the current application. Historical notes below are retained only where needed to explain compatibility; the canonical SQL block and final table contracts are authoritative.


> **Canonical database specification.**
> This document is the single source of truth for every table, column,
> index, constraint, and RLS policy in the Supabase database. Future
> migrations MUST be generated from this document — no schema change is
> valid unless it is reflected here first.
>
> Every column listed here is read or written by a Python module in the
> repository. Columns that exist in migrations but are never touched by
> code are listed in a dedicated "Dead Columns" subsection and marked for
> removal in a future migration.

---

## Table of Contents

1. [Overview](#1-overview)
2. [saved_items](#2-saved_items)
3. [bio_state](#3-bio_state)
4. [username_state](#4-username_state)
5. [bot_logs](#5-bot_logs)
6. [panel_settings](#6-panel_settings)
7. [ai_config](#7-ai_config)
8. [ai_sessions](#8-ai_sessions)
9. [ai_messages](#9-ai_messages)
10. [ai_memories](#10-ai_memories)
11. [ai_tool_history](#11-ai_tool_history)
12. [ai_provider_stats](#12-ai_provider_stats)
13. [ai_usage](#13-ai_usage)
14. [ai_preferences](#14-ai_preferences)
15. [ai_tasks](#15-ai_tasks)
16. [ai_task_occurrences](#16-ai_task_occurrences)
17. [Relationships](#17-relationships)
18. [RLS Policy Model](#18-rls-policy-model)
19. [Panel Database](#19-panel-database)
20. [In-Memory Fallback](#20-in-memory-fallback)
21. [Known Inconsistencies](#21-known-inconsistencies)
22. [Migration Status](#22-migration-status)
23. [Migration Generation Rules](#23-migration-generation-rules)
24. [Ghost Seen / Ghost PV](#24-ghost-seen--ghost-pv)
25. [Self Bot Persistent State Inventory](#25-self-bot-persistent-state-inventory)
26. [Hermes Integration Boundary & Corrected Architecture](#26-hermes-integration-boundary--corrected-architecture)
27. [Font System Persistence](#27-font-system-persistence)
28. [Current vs Proposed Status Matrix](#28-current-vs-proposed-status-matrix)

---

## 1. Overview

The database contains **13 tables** in the `public` schema — 5 core
LifeOS tables and 8 AI subsystem tables. Two additional tables exist in
migrations (`bot_settings`, `ghost_chats`). `ai_usage` and
`ai_provider_stats` have migration files (`20260827000003` /
`20260827000004`) pending manual application; `ai_preferences` is
specified here and implemented in code but has **no applied migration**.
The exact
status of every table is in [§19](#19-known-inconsistencies),
[§20](#20-migration-status), and [§23](#23-self-bot-persistent-state-inventory).

### Core Tables

| Table | Purpose | PK | Used by |
|---|---|---|---|
| `saved_items` | Media save records (forward + deep) | `id` (bigserial) | `db/client.py`, `save_service`, `retrieve_service`, `delete_service`, `discover_service`, `database_service`, `organize_service`, `web/app` |
| `bio_state` | Bio cron engine state per owner | `id` (bigserial) | `db/client.py`, `bio_service`, `bio/engine`, `organize_service`, `web/app` |
| `username_state` | Username cron engine state per owner | `id` (bigserial) | `db/client.py`, `username_service`, `username/engine` |
| `bot_logs` | Structured activity log | `id` (bigserial) | `db/client.py` (via `log()`), nearly every service, `web/app` |
| `panel_settings` | Glass Panel configuration (column-per-setting) | `key` (text) | `panel_settings_repository`, `settings_service`, `web/app` |

### AI Tables

| Table | Purpose | PK | Used by |
|---|---|---|---|
| `ai_config` | Per-owner AI configuration (provider, model, triggers, settings) | `id` (bigserial) | `ai/config_store.py`, `bot/handlers/ai_trigger.py`, `bot/handlers/ai.py`, `web/app` |
| `ai_sessions` | AI conversation session metadata | `session_id` (text) | `ai/persistence.py`, `ai/database/session_repository.py` |
| `ai_messages` | Individual AI messages within a session | `id` (bigserial) | `ai/persistence.py`, `ai/database/message_repository.py` |
| `ai_memories` | Three-tier memory (short, long, permanent) | `id` (bigserial) | `ai/persistence.py`, `ai/database/memory_repository.py` |
| `ai_tool_history` | Log of every tool call the AI made | `id` (bigserial) | `ai/persistence.py`, `ai/database/tool_history_repository.py` |
| `ai_provider_stats` | Per-provider aggregate statistics | `(provider_name, owner_id)` | `ai/database/provider_stats_repository.py` |
| `ai_usage` | Per-request token usage log | `id` (bigserial) | `ai/database/usage_repository.py` |
| `ai_preferences` | Per-owner AI personality and behavior preferences | `owner_id` (bigint) | `ai/database/preferences_repository.py` |

### Additional Tables in Migrations / Specified But Unmigrated

| Table | Migration | Code usage | Status |
|---|---|---|---|
| `bot_settings` | `20260729213959` | `ghost_seen_v2` (key `ghost_seen_allowed_chats`) | **Live** — NOT orphaned. See §19.4. |
| `ghost_chats` | `20260822090000` | none | **Orphaned** — no code reads or writes it. See §19.12. |
| `ai_usage` | `20260827000003` | `SupabaseUsageRepository` + `usage_recorder` | Migration created, **pending manual application** (§13). |
| `ai_provider_stats` | `20260827000004` | `SupabaseProviderStatsRepository` + `usage_recorder` | Migration created, **pending manual application** (§12). |
| `ai_preferences` | none | interface + in-memory only | Specified (§14), **no producer, no migration**. |

### Access Model

All access goes through the Supabase PostgREST API via the `supabase-py`
client. The backend uses the **service-role key**, which bypasses RLS.
The frontend reads via the backend API — it never touches Supabase
directly.

---

## 2. saved_items

Stores metadata for every media save operation. Deep Save is the ONLY
save method in the current code (`save_service.py::execute_save` always
writes `save_type='deep'`); the `'forward'` value exists only for the
column CHECK constraint and legacy rows. `forward_messages` exists
exclusively inside retrieval (`retrieve_service.do_retrieve`), never in
the Save path (see AGENTS.md §6).

### Columns

| Column | SQL Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | `bigserial` | NO | `nextval(...)` | Primary key |
| `save_code` | `text` | NO | — | Compact code, format `S` + 4 chars (e.g. `S0001`; random `SXXXX` on collision), generated by `db/client.py::get_next_save_code`. Unique. The legacy `SV-NNNNNN` format was retired by the short-code redesign; historical rows keep their original codes. |
| `save_type` | `text` | NO | — | `'forward'` or `'deep'`. CHECK constraint enforced. |
| `origin_chat_id` | `bigint` | YES | — | Telegram chat ID where the message originated |
| `origin_msg_id` | `bigint` | YES | — | Telegram message ID of the original message |
| `saved_chat_id` | `bigint` | YES | — | Telegram chat ID where the message was saved |
| `saved_msg_id` | `bigint` | YES | — | Telegram message ID of the saved message |
| `sender_name` | `text` | YES | — | Display name of the original sender |
| `sender_id` | `bigint` | YES | — | Telegram user ID of the original sender |
| `mime_type` | `text` | YES | — | MIME type of the media (e.g. `image/jpeg`) |
| `file_id` | `text` | YES | — | Telegram file ID |
| `file_size` | `bigint` | YES | — | File size in bytes |
| `media_type` | `text` | YES | — | Human-readable media type label (Photo, Video, etc.) |
| `tags` | `text[]` | YES | — | Array of tags (e.g. `{#saved, #saved_photo}`) |
| `caption` | `text` | YES | — | Caption attached to the saved message |
| `owner_id` | `bigint` | NO | `0` | Telegram user ID of the bot owner |
| `created_at` | `timestamptz` | NO | `now()` | When the save was created |

### Dead Columns (exist in migrations, never used by code)

| Column | SQL Type | Added by | Notes |
|---|---|---|---|
| `short_code` | `text` | `20260718143752` | Never read or written by any Python module. Orphan trigram indexes depend on it. Must be dropped in a future cleanup migration along with its dependent indexes. |
| `file_name` | `text` | `20260718143752` | Never included in the insert payload built by `save_service.py`. Orphan trigram indexes depend on it. Must be dropped in a future cleanup migration. |

### Indexes

| Index | Columns | Type | Notes |
|---|---|---|---|
| `saved_items_pkey` | `id` | btree (PK) | |
| `saved_items_save_code_key` | `save_code` | btree (UNIQUE) | |
| `idx_saved_items_owner` | `owner_id` | btree | |
| `idx_saved_items_created_at` | `created_at` | btree | |
| `idx_saved_items_save_type` | `save_type` | btree | |
| `idx_saved_items_owner_created` | `(owner_id, created_at)` | btree | Added by migration `20260718143752`. Composite index for `list_saves` + `list_recent_saves` queries. |

### Dead Indexes (depend on dead columns, should be dropped)

| Index | Columns | Type | Notes |
|---|---|---|---|
| `idx_saved_items_caption_trgm` | `caption` | GIN (trigram) | Depends on `pg_trgm` extension. Only useful if full-text search on `caption` is implemented — it is not. |
| `idx_saved_items_file_name_trgm` | `file_name` | GIN (trigram) | Depends on dead `file_name` column. |
| `idx_saved_items_save_code_trgm` | `save_code` | GIN (trigram) | Redundant — `save_code` already has a unique btree index. |
| `idx_saved_items_short_code_trgm` | `short_code` | GIN (trigram) | Depends on dead `short_code` column. |
| `idx_saved_items_mime_type_trgm` | `mime_type` | GIN (trigram) | No search query uses trigram on `mime_type`. |

### CHECK Constraints

- `saved_items_save_type_check`: `save_type IN ('forward', 'deep')`

### RLS

RLS is enabled. Only SELECT is granted to `anon` + `authenticated`.
All writes go through the backend service-role key.

### Repository

**`backend/db/client.py`** — all CRUD via `insert_save`, `query_save`,
`list_saves`, `list_recent_saves`, `search_saves`, `delete_save_row`,
`update_save_field`, `count_saves`, `list_all_saves`, `cleanup_orphans`,
`get_stats`, `get_next_save_code`.

**`backend/services/save_service.py`** — builds the insert payload with
all live columns, calls `insert_save`.

---

## 3. bio_state

Singleton bio engine state per owner. One row per owner (enforced by
UNIQUE constraint on `owner_id`).

### Columns

| Column | SQL Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | `bigserial` | NO | `nextval(...)` | Primary key |
| `owner_id` | `bigint` | NO | `0` | Telegram user ID of the bot owner. UNIQUE. |
| `template` | `text` | NO | `'🕒 {time} \| 💭 {mood}'` | Bio template with `{time}`, `{mood}`, `{text}` tokens |
| `mood` | `text` | NO | `'😊'` | Current mood value |
| `custom_text` | `text` | NO | `''` | Custom text for `{text}` token |
| `is_active` | `boolean` | NO | `false` | Whether the bio cron is running |
| `last_bio` | `text` | YES | — | Last successfully rendered bio string |
| `updated_at` | `timestamptz` | NO | `now()` | Last update timestamp |

### Indexes

| Index | Columns | Type |
|---|---|---|
| `bio_state_pkey` | `id` | btree (PK) |
| `bio_state_owner_id_key` | `owner_id` | btree (UNIQUE) |

### RLS

RLS is enabled. Only SELECT is granted to `anon` + `authenticated`.

### Repository

**`backend/db/client.py`** — `get_bio_state`, `get_or_create_bio_state`,
`update_bio_state`.

**Defaults from code** (`get_or_create_bio_state`):
template `'🕒 {time} | 💭 {mood}'`, mood `'😊'`, custom_text `''`,
is_active `false`, last_bio `''`.

---

## 4. username_state

Singleton username engine state per owner. Mirrors `bio_state` in
structure but controls the Telegram `first_name` field instead of the
`about` field. Completely independent from the Bio Engine.

### Columns

| Column | SQL Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | `bigserial` | NO | `nextval(...)` | Primary key |
| `owner_id` | `bigint` | NO | `0` | Telegram user ID of the bot owner. UNIQUE. |
| `template` | `text` | NO | `'{time} \| {mood}'` | Username template with `{time}`, `{mood}`, `{text}` tokens |
| `mood` | `text` | NO | `'😊'` | Current mood value |
| `custom_text` | `text` | NO | `''` | Custom text for `{text}` token |
| `is_active` | `boolean` | NO | `false` | Whether the username cron is running |
| `last_name` | `text` | NO | `''` | Last successfully rendered username string |
| `updated_at` | `timestamptz` | NO | `now()` | Last update timestamp |

### Indexes

| Index | Columns | Type |
|---|---|---|
| `username_state_pkey` | `id` | btree (PK) |
| `username_state_owner_id_key` | `owner_id` | btree (UNIQUE) |
| `idx_username_state_owner` | `owner_id` | btree |

### RLS

RLS is enabled. Only SELECT is granted to `anon` + `authenticated`.

### Repository

**`backend/db/client.py`** — `get_username_state`,
`get_or_create_username_state`, `update_username_state`.

**Defaults from code** (`get_or_create_username_state`):
template `'{time} | {mood}'`, mood `'😊'`, custom_text `''`,
is_active `false`, last_name `''`.

---

## 5. bot_logs

Structured activity log.

### Columns

| Column | SQL Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | `bigserial` | NO | `nextval(...)` | Primary key |
| `owner_id` | `bigint` | NO | `0` | Telegram user ID of the bot owner |
| `level` | `text` | NO | `'INFO'` | Log level: `INFO`, `WARN`, `ERROR` |
| `message` | `text` | NO | — | Log message |
| `context` | `jsonb` | YES | — | Structured context (JSON) |
| `created_at` | `timestamptz` | NO | `now()` | When the log entry was created |

### Indexes

| Index | Columns | Type |
|---|---|---|
| `bot_logs_pkey` | `id` | btree (PK) |
| `idx_bot_logs_owner` | `owner_id` | btree |
| `idx_bot_logs_created_at` | `created_at` | btree |

### RLS

RLS is enabled. Only SELECT is granted to `anon` + `authenticated`.

### Repository

**`backend/db/client.py`** — `log`, `count_logs`, `list_logs`,
`clean_logs`.

---

## 6. panel_settings

Glass Panel configuration. Singleton row (key = `"global"`). Uses a
**column-per-setting** model — each setting is a real typed column, not
a key-value store.

### Columns

| Column | SQL Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `key` | `text` | NO | — | Primary key, always `"global"` |
| `auto_close_enabled` | `boolean` | NO | `true` | Whether panels auto-close |
| `auto_close_delay` | `integer` | NO | `120` | Auto-close delay in seconds |
| `max_deep_save_mb` | `integer` | NO | `50` | Max file size for deep save (MB) |
| `delete_batch_size` | `integer` | NO | `100` | Batch size for message deletion |
| `log_retention_days` | `integer` | NO | `7` | Days to retain logs |
| `panel_timeout_seconds` | `integer` | NO | `300` | Panel timeout in seconds |
| `allow_multiple_panels` | `boolean` | NO | `false` | Allow multiple simultaneous panels |
| `reuse_existing_panel` | `boolean` | NO | `true` | Reuse an existing panel instead of creating new |
| `language` | `text` | NO | `'en'` | Language code |
| `debug_callbacks` | `boolean` | NO | `false` | Debug callback tracing |
| `owner_only` | `boolean` | NO | `true` | Restrict commands to owner only |
| `dashboard_font` | `text` | NO | `'default'` | Glass UI / dashboard font key — one of `FONT_KEYS` in `backend/helper/font_style.py` (`DASHBOARD_FONTS` == `FONT_KEYS`); never free-text CSS |
| `ghost_seen_retention_seconds` | `bigint` | NO | `2592000` | Ghost Seen registry retention window in seconds (30 min–365 days); presets include sub-day durations |
| `update_stale_seconds` | `integer` | NO | `300` | Update staleness threshold (seconds) |
| `updated_at` | `timestamptz` | YES | `now()` | Last update timestamp |

### Indexes

| Index | Columns | Type |
|---|---|---|
| `panel_settings_pkey` | `key` | btree (PK) |

### RLS

RLS is enabled. Only SELECT is granted to `anon` + `authenticated`.

### Repository

**`backend/services/panel_settings_repository.py`** — `load`,
`update_field`, `update_fields`, `reload`.

**`backend/services/settings_service.py`** — cache-first reads,
write-through cache, **12 typed getters + 12 typed setters**
(`auto_close_enabled`, `auto_close_delay`, `max_deep_save_mb`,
`delete_batch_size`, `log_retention_days`, `panel_timeout_seconds`,
`allow_multiple_panels`, `reuse_existing_panel`, `language`,
`debug_callbacks`, `owner_only`, `dashboard_font`), per-setting
validators. See [§17 Panel Database](#17-panel-database) for details.

> **Note:** `ghost_seen_retention_seconds` and `update_stale_seconds`
> are migrated columns with **no accessor in `settings_service` and no
> production consumer** — configured-but-unconsumed until a consumer is
> implemented (see §19.13, §19.14).

### Migration Status

The initial migration (`20260726143924`) created only 3 columns
(`key`, `auto_close_enabled`, `updated_at`). A later migration
(`20260730210551`) added `update_stale_seconds`. The remaining 10
columns were supposed to be added by migrations
`20260730220000_panel_settings_column_model.sql` and
`20260730230000_panel_settings_full_13_columns.sql`, but **these
migration files do not exist** in the repository. Migration
`20260827000001_add_missing_panel_settings_columns.sql` now adds all
10 missing columns (idempotent, with CHECK constraints mirroring the
`settings_service` validators) — pending manual application. Migration
`20260823120000_add_dashboard_font_and_ghost_seen_settings.sql`
(idempotent) covers `dashboard_font` and (originally)
`ghost_seen_retention_days`; it was applied to the live database and
verified by the project owner. The follow-up migration
`20260823130000_ghost_seen_retention_duration.sql` (idempotent)
replaces the days column with `ghost_seen_retention_seconds`
(backfilling `days × 86400`) to support sub-day retention windows; it
is pending manual application. See [§20](#20-migration-status) and
[§19](#19-known-inconsistencies).

### Removed / phantom columns

The following columns were referenced in older documentation or the
`bot_settings` transition table but are **not** in the canonical schema:

- ~~`panel_auto_close_seconds`~~ — superseded by `auto_close_delay`
- ~~`log_cleanup_days`~~ — superseded by `log_retention_days`
- ~~`diagnostics_enabled`~~ — never implemented in `settings_service.py`
- ~~`bot_settings` table~~ — this earlier removal proposal is superseded.
  The current Ghost Seen implementation actively requires `bot_settings`
  for the `ghost_seen_allowed_chats` durable allow-list; do not drop it
  until a replacement persistence design and data migration are implemented.

---

## 7. ai_config

Per-owner AI configuration. **One row per owner** — enforced by a
UNIQUE constraint on `owner_id`. This is the canonical storage for
provider selection, model selection, trigger words, and tuning
parameters.

> **Current status:** The `ai_config` table is referenced by
> `backend/ai/config_store.py` but may not exist in the live database.
> The initial migration (`20260805075707`) creates the base columns but
> does NOT include `trigger_en` or `trigger_fa`. Migration
> `20260827000002_add_ai_config_trigger_columns.sql` (idempotent) now
> adds them — pending manual application. Until it is applied, the
> runtime silently falls back to in-memory storage when the table or
> columns are missing. See
> [§19 Known Inconsistencies](#19-known-inconsistencies).

### Columns

| Column | SQL Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | `bigserial` | NO | `nextval(...)` | Primary key |
| `owner_id` | `bigint` | NO | `0` | Telegram user ID of the bot owner. UNIQUE. One config row per owner. |
| `provider` | `text` | NO | `''` | Active provider name (e.g. `gemini`, `openai`, `openrouter`, `groq`, `mistral`, `cerebras`, `dummy`) |
| `model` | `text` | NO | `''` | Active model name |
| `temperature` | `double precision` | NO | `1.0` | Sampling temperature [0.0, 2.0] |
| `max_tokens` | `integer` | NO | `4096` | Max output tokens |
| `system_prompt` | `text` | NO | `''` | Custom system prompt (empty = default) |
| `history_budget` | `integer` | NO | `4000` | Context budget in tokens |
| `is_configured` | `boolean` | NO | `false` | Whether the user completed setup |
| `trigger_en` | `text` | YES | `NULL` | English trigger word (case-insensitive matching). NULL = not set. |
| `trigger_fa` | `text` | YES | `NULL` | Persian trigger word (exact matching). NULL = not set. |
| `last_request_at` | `timestamptz` | YES | `NULL` | Timestamp of the last AI request. **Currently never persisted** — see [§19](#19-known-inconsistencies). |
| `last_latency_ms` | `real` | YES | `NULL` | Latency of the last AI request in ms. **Currently never persisted** — see [§19](#19-known-inconsistencies). |
| `created_at` | `timestamptz` | YES | `now()` | When the config row was created |
| `updated_at` | `timestamptz` | YES | `now()` | Last update timestamp |

### Indexes

| Index | Columns | Type |
|---|---|---|
| `ai_config_pkey` | `id` | btree (PK) |
| `ai_config_owner_id_key` | `owner_id` | btree (UNIQUE) |

### RLS

RLS is enabled. Only SELECT is granted to `anon` + `authenticated`.
All writes go through the backend service-role key.

### Trigger Validation Rules (enforced in application code)

- Both `trigger_en` and `trigger_fa` are optional individually (NULL or
  empty string).
- At least one must be non-empty before AI can be activated.
- The two values must not be identical (case-insensitive comparison).
- Triggers must be single words (no spaces).
- `trigger_en` matching is case-insensitive.
- `trigger_fa` matching is exact (no case folding).
- When a trigger matches, the trigger word is stripped from the message
  before being sent to the provider.

### Repository

**`backend/ai/config_store.py`** — `get_config`, `save_config`,
`update_provider`, `update_model`, `update_setting`, `record_request`,
`is_configured`, `validate_triggers`, `update_triggers`,
`get_triggers`, `match_trigger`.

**In-memory fallback:** `_fallback_config` dict keyed by `owner_id`.
Used when the DB is unavailable. All reads return fallback data; all
writes update the fallback dict. The fallback is also updated on
successful DB writes so the in-memory state stays consistent.

### Defaults from code (`_DEFAULT_CONFIG`)

```
provider: "", model: "", temperature: 1.0, max_tokens: 4096,
system_prompt: "", history_budget: 4000, is_configured: False,
trigger_en: "", trigger_fa: ""
```

---

## 8. ai_sessions

AI conversation session metadata. One row per active AI conversation.

### Columns

| Column | SQL Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `session_id` | `text` | NO | — | Primary key. Format: `owner-{owner_id}` |
| `owner_id` | `bigint` | NO | — | Telegram user ID of the bot owner |
| `provider` | `text` | YES | — | Active provider name |
| `model` | `text` | YES | — | Active model name |
| `status` | `text` | NO | `'active'` | Session status: `active`, `closed`, `error` |
| `total_tokens` | `integer` | YES | `0` | Cumulative token count |
| `message_count` | `integer` | YES | `0` | Number of messages in session |
| `created_at` | `timestamptz` | YES | `now()` | When the session was created |
| `updated_at` | `timestamptz` | YES | `now()` | Last update timestamp |

### Indexes

| Index | Columns | Type |
|---|---|---|
| `ai_sessions_pkey` | `session_id` | btree (PK) |
| `idx_ai_sessions_owner` | `owner_id` | btree |

### RLS

RLS is enabled. Only SELECT is granted to `anon` + `authenticated`.
All writes go through the backend service-role key.

### Repository

**`backend/ai/persistence.py`** — `create_session`, `update_session`,
`get_session`. Only inserts `session_id` and `owner_id` on creation;
other fields are updated via `update_session`.

**`backend/ai/database/session_repository.py`** — interface +
in-memory fallback (`SessionRecord`, `SessionRepository`,
`InMemorySessionRepository`). `SessionRecord` fields: `session_id`,
`owner_id`, `provider`, `model`, `status`, `total_tokens`,
`message_count`. No Supabase-backed implementation is wired here yet.

---

## 9. ai_messages

Individual AI messages within a conversation session.

### Columns

| Column | SQL Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | `bigserial` | NO | `nextval(...)` | Primary key |
| `session_id` | `text` | NO | — | FK → `ai_sessions.session_id` (logical, not enforced) |
| `owner_id` | `bigint` | NO | — | Telegram user ID of the bot owner |
| `role` | `text` | NO | — | `user`, `assistant`, or `system` |
| `content` | `text` | NO | — | Message content (truncated to 8000 chars on write) |
| `token_count` | `integer` | YES | `0` | Estimated token count |
| `tool_calls` | `jsonb` | YES | `'[]'` | Tool calls made in this message (JSON array). **Defined in `MessageRecord` but not written by `persistence.py` and not in the applied migration.** See [§19](#19-known-inconsistencies). |
| `provider` | `text` | YES | — | Provider that generated this message |
| `model` | `text` | YES | — | Model that generated this message |
| `created_at` | `timestamptz` | YES | `now()` | When the message was created |

### Indexes

| Index | Columns | Type |
|---|---|---|
| `ai_messages_pkey` | `id` | btree (PK) |
| `idx_ai_messages_session` | `session_id` | btree |
| `idx_ai_messages_owner` | `owner_id` | btree |

### RLS

RLS is enabled. Only SELECT is granted to `anon` + `authenticated`.
All writes go through the backend service-role key.

### Repository

**`backend/ai/persistence.py`** — `add_message`, `get_messages`.
Inserts: `session_id`, `owner_id`, `role`, `content` (truncated),
`token_count`, `provider`, `model`. Does NOT insert `tool_calls`.

**`backend/ai/database/message_repository.py`** — interface +
in-memory fallback (`MessageRecord`, `MessageRepository`,
`InMemoryMessageRepository`). `MessageRecord` fields include
`tool_calls` and `metadata` — neither is written by `persistence.py`
nor created by the applied migration. See [§19](#19-known-inconsistencies).

---

## 10. ai_memories

Three-tier memory system: short, long, and permanent memories.

### Columns

| Column | SQL Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | `bigserial` | NO | `nextval(...)` | Primary key |
| `owner_id` | `bigint` | NO | — | Telegram user ID of the bot owner |
| `tier` | `text` | NO | — | `short`, `long`, or `permanent` |
| `category` | `text` | NO | — | `fact`, `preference`, `context`, `summary`, `instruction` |
| `content` | `text` | NO | — | Memory text (truncated to 8000 chars on write) |
| `importance` | `real` | YES | `0.5` | Score 0.0–1.0 (higher = more relevant) |
| `expires_at` | `timestamptz` | YES | — | When the memory expires (NULL = never) |
| `metadata` | `jsonb` | YES | — | Arbitrary extra metadata |
| `created_at` | `timestamptz` | YES | `now()` | When the memory was created |

### Indexes

| Index | Columns | Type |
|---|---|---|
| `ai_memories_pkey` | `id` | btree (PK) |
| `idx_ai_memories_owner_tier` | `(owner_id, tier)` | btree |
| `idx_ai_memories_importance` | `importance` | btree |

### RLS

RLS is enabled. Only SELECT is granted to `anon` + `authenticated`.
All writes go through the backend service-role key.

### Repository

**`backend/ai/persistence.py`** — `save_memory`, `query_memories`,
`delete_expired_memories`.

**`backend/ai/database/memory_repository.py`** — interface +
in-memory fallback (`MemoryRepository`, `InMemoryMemoryRepository`).
Filters by: `owner_id`, `tier`, `category`, `importance` (gte),
query text (substring), `expires_at`.

**`backend/ai/memory/`** — `MemoryManager`, `ShortMemory`,
`LongMemory`, `PermanentMemory`.

---

## 11. ai_tool_history

Log of every tool call the AI has made. Used for auditing and debugging.

### Columns

| Column | SQL Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | `bigserial` | NO | `nextval(...)` | Primary key |
| `owner_id` | `bigint` | NO | — | Telegram user ID of the bot owner |
| `session_id` | `text` | YES | — | AI session ID |
| `tool_name` | `text` | NO | — | Name of the tool called |
| `arguments` | `jsonb` | YES | — | Arguments passed to the tool |
| `result_success` | `boolean` | YES | `false` | Whether the tool succeeded |
| `result_message` | `text` | YES | — | Result message (truncated to 2000 chars) |
| `result_data` | `jsonb` | YES | `'{}'` | Result data payload. **Defined in `ToolHistoryRecord` and migration but never written by `persistence.py`.** See [§19](#19-known-inconsistencies). |
| `latency_ms` | `real` | YES | `0` | Execution latency in milliseconds |
| `created_at` | `timestamptz` | YES | `now()` | When the tool was called |

### Indexes

| Index | Columns | Type |
|---|---|---|
| `ai_tool_history_pkey` | `id` | btree (PK) |
| `idx_ai_tool_history_owner` | `owner_id` | btree |
| `idx_ai_tool_history_session` | `session_id` | btree |

### RLS

RLS is enabled. Only SELECT is granted to `anon` + `authenticated`.
All writes go through the backend service-role key.

### Repository

**`backend/ai/persistence.py`** — `record_tool_call`. Inserts:
`owner_id`, `session_id`, `tool_name`, `arguments`, `result_success`,
`result_message` (truncated), `latency_ms`. Does NOT insert
`result_data`.

**`backend/ai/database/tool_history_repository.py`** — interface +
in-memory fallback (`ToolHistoryRecord`, `ToolHistoryRepository`,
`InMemoryToolHistoryRepository`). `ToolHistoryRecord` fields include
`result_data` — not written by `persistence.py`.

---

## 12. ai_provider_stats

Per-provider aggregate statistics. One row per (provider, owner) pair.

### Columns

| Column | SQL Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `provider_name` | `text` | NO | — | Provider name (part of composite PK) |
| `owner_id` | `bigint` | NO | `0` | Owner ID (part of composite PK) |
| `total_requests` | `integer` | NO | `0` | Total requests made |
| `successful_requests` | `integer` | NO | `0` | Successful requests |
| `failed_requests` | `integer` | NO | `0` | Failed requests |
| `total_prompt_tokens` | `integer` | NO | `0` | Cumulative prompt tokens |
| `total_completion_tokens` | `integer` | NO | `0` | Cumulative completion tokens |
| `avg_latency_ms` | `real` | NO | `0` | Average latency |
| `last_request_at` | `timestamptz` | YES | — | Last request timestamp |
| `updated_at` | `timestamptz` | NO | `now()` | Last update |

### Indexes

| Index | Columns | Type |
|---|---|---|
| `ai_provider_stats_pkey` | `(provider_name, owner_id)` | btree (composite PK) |

### RLS

RLS should be enabled. Only SELECT is granted to `anon` +
`authenticated`. All writes go through the backend service-role key.

### Repository

**`backend/ai/database/provider_stats_repository.py`** — interface +
in-memory fallback (`ProviderStatsRecord`, `ProviderStatsRepository`,
`InMemoryProviderStatsRepository`) plus a Supabase-backed implementation
(`SupabaseProviderStatsRepository`) wired into `RepositoryManager` when
Supabase is available. Updates come from
`backend/ai/database/usage_recorder.py` (read-modify-write upsert per
(provider, owner)). Methods: `get_or_create`, `record_request`, `get`,
`list_all`. Migration `20260827000004_create_ai_provider_stats_table.sql`
has been generated from this section (the composite PK matches the
writer's upsert conflict target) — pending manual application.

---

## 13. ai_usage

Per-request token usage log. One row per AI API call.

### Columns

| Column | SQL Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | `bigserial` | NO | `nextval(...)` | Primary key |
| `owner_id` | `bigint` | NO | — | Telegram user ID of the bot owner |
| `session_id` | `text` | YES | — | AI session ID |
| `provider` | `text` | YES | — | Provider name |
| `model` | `text` | YES | — | Model name |
| `prompt_tokens` | `integer` | YES | `0` | Prompt token count |
| `completion_tokens` | `integer` | YES | `0` | Completion token count |
| `total_tokens` | `integer` | YES | `0` | Total token count |
| `latency_ms` | `real` | YES | `0` | Request latency in milliseconds |
| `token_source` | `text` | YES | `NULL` | `actual` / `estimated` / `unavailable` — the honesty label for the token counts (never fabricated) |
| `created_at` | `timestamptz` | YES | `now()` | When the usage was recorded |

### Indexes

| Index | Columns | Type |
|---|---|---|
| `ai_usage_pkey` | `id` | btree (PK) |
| `idx_ai_usage_owner` | `owner_id` | btree |
| `idx_ai_usage_created_at` | `created_at` | btree |

### RLS

RLS should be enabled. Only SELECT is granted to `anon` +
`authenticated`. All writes go through the backend service-role key.

### Repository

**`backend/ai/database/usage_repository.py`** — interface +
in-memory fallback (`UsageRecord`, `UsageRepository`,
`InMemoryUsageRepository`) plus a Supabase-backed implementation
(`SupabaseUsageRepository`) wired into `RepositoryManager` when Supabase
is available. Writes come from `backend/ai/database/usage_recorder.py`,
which persists the normalized `AIExecutionRecord` exactly once per
request. Methods: `create`, `total_tokens`, `daily_tokens`, `recent`.
Migration `20260827000003_create_ai_usage_table.sql` has been generated
from this section (bigserial `id` — the Supabase writer never sends
`id`) — pending manual application.

---

## 14. ai_preferences

Per-owner AI personality and behavior preferences. One row per owner.

### Columns

| Column | SQL Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `owner_id` | `bigint` | NO | — | Primary key. Telegram user ID of the bot owner. |
| `language` | `text` | NO | `'en'` | Preferred language |
| `personality` | `text` | NO | `'helpful'` | Personality mode |
| `response_style` | `text` | NO | `'concise'` | Response style |
| `custom_instructions` | `text` | NO | `''` | Custom system instructions |
| `auto_memory` | `boolean` | NO | `true` | Whether auto-memory is enabled |
| `auto_tools` | `boolean` | NO | `true` | Whether auto-tools are enabled |
| `metadata` | `jsonb` | YES | `'{}'` | Arbitrary extra metadata |
| `created_at` | `timestamptz` | YES | `now()` | When the preference row was created |
| `updated_at` | `timestamptz` | YES | `now()` | Last update timestamp |

### Indexes

| Index | Columns | Type |
|---|---|---|
| `ai_preferences_pkey` | `owner_id` | btree (PK) |

### RLS

RLS should be enabled. Only SELECT is granted to `anon` +
`authenticated`. All writes go through the backend service-role key.

### Repository

**`backend/ai/database/preferences_repository.py`** — interface +
in-memory fallback (`PreferencesRecord`, `PreferencesRepository`,
`InMemoryPreferencesRepository`). Methods: `get_or_create`, `update`,
`get`. No Supabase-backed implementation is wired here yet. No
migration has been applied for this table.

---

## 15. ai_tasks

Durable owner-scoped AI task definitions for the future scheduler. This table exists in the repository migration `supabase/migrations/20260829000001_create_ai_tasks.sql`; live Supabase application is a separate manual deployment step and has not been verified.

| Column | Type | Nullable | Default / constraint |
|---|---|---:|---|
| `id` | `bigserial` | NO | PRIMARY KEY |
| `owner_id` | `bigint` | NO | Authenticated Telegram owner; no owner FK |
| `label` | `text` | NO | Nonblank; max 256 enforced by repository |
| `status` | `text` | NO | `'active'`; CHECK `active`, `paused`, `completed`, `failed`, `expired`, `deleted` |
| `version` | `integer` | NO | `1`; CHECK `> 0` |
| `schedule_type` | `text` | NO | CHECK `once`, `interval`, `daily`, `weekly` |
| `schedule` | `jsonb` | NO | Max 16,384 bytes by migration |
| `timezone` | `text` | NO | Explicit IANA identifier validated by application |
| `next_run_at` | `timestamptz` | YES | UTC due instant |
| `actions` | `jsonb` | NO | JSON array, 1–5 actions, max 32,768 bytes |
| `notification_destination` | `jsonb` | NO | Max 4,096 bytes; explicit owner-scoped destination |
| `created_at` | `timestamptz` | NO | `now()` |
| `updated_at` | `timestamptz` | NO | `now()` |
| `terminal_at` | `timestamptz` | YES | Terminal lifecycle timestamp |

Indexes are `idx_ai_tasks_status_next_run (status, next_run_at)` and `idx_ai_tasks_owner_updated (owner_id, updated_at DESC)`. There is no trigger or SQL schedule logic. Task version edits and lifecycle validation are repository/application responsibilities. Actions are bounded JSON; no action or step table exists.

RLS is enabled. The migration grants SELECT to `anon` and `authenticated` and adds no public write policy. Backend access uses the service-role client and repository methods must still filter by `owner_id`. Retain task definitions until explicit owner deletion/terminal cleanup policy is approved.

## 16. ai_task_occurrences

Durable occurrence/attempt history for `ai_tasks`. Repository migration state exists in the same migration file; live Supabase deployment is manual and unverified.

| Column | Type | Nullable | Default / constraint |
|---|---|---:|---|
| `id` | `bigserial` | NO | PRIMARY KEY |
| `task_id` | `bigint` | NO | REFERENCES `ai_tasks(id)` ON DELETE RESTRICT |
| `owner_id` | `bigint` | NO | Denormalized owner; no owner FK |
| `occurrence_key` | `text` | NO | Nonblank; unique with `task_id` |
| `definition_version` | `integer` | NO | CHECK `> 0` |
| `action_snapshot` | `jsonb` | NO | JSON array, 1–5 actions, max 32,768 bytes |
| `scheduled_for` | `timestamptz` | NO | UTC scheduled instant |
| `attempt` | `smallint` | NO | `1`; CHECK 1–3 |
| `status` | `text` | NO | `'claimed'`; CHECK `claimed`, `running`, `succeeded`, `failed`, `retry_pending`, `cancelled`, `expired`, `interrupted` |
| `claimed_at` | `timestamptz` | YES | Claim timestamp |
| `started_at` | `timestamptz` | YES | Start timestamp |
| `finished_at` | `timestamptz` | YES | Finish timestamp |
| `retry_at` | `timestamptz` | YES | Required by migration for `retry_pending` |
| `error_metadata` | `jsonb` | NO | `'{}'`; object, max 8,192 bytes |
| `result_metadata` | `jsonb` | NO | `'{}'`; object, max 8,192 bytes |
| `created_at` | `timestamptz` | NO | `now()` |
| `updated_at` | `timestamptz` | NO | `now()` |

Indexes are unique `uq_ai_task_occurrences_task_key (task_id, occurrence_key)`, `idx_ai_task_occurrences_owner_scheduled (owner_id, scheduled_for DESC)`, and `idx_ai_task_occurrences_task_scheduled (task_id, scheduled_for DESC)`. No `(status, retry_at)` index is present because the current repository does not issue that query. The unique index prevents duplicate durable occurrences, not duplicate Telegram side effects.

RLS is enabled. SELECT is granted to `anon` and `authenticated`; no public write policy is added. The task FK uses `ON DELETE RESTRICT`, preserving history. Occurrences snapshot `definition_version` and `action_snapshot`; later task edits do not rewrite history. Recommended retention is bounded terminal history (initially 90 days, subject to operations approval).

## 17. Relationships

The task foundation is the one intentional enforced relationship. The
following logical relationships exist without foreign keys:

- `ai_messages.session_id` → `ai_sessions.session_id` (logical)
- `ai_tool_history.session_id` → `ai_sessions.session_id` (logical)
- `ai_usage.session_id` → `ai_sessions.session_id` (logical)
- `ai_task_occurrences.owner_id` must match `ai_tasks.owner_id` by repository validation; there is no owner FK.
- `owner_id` on existing tables links rows to the bot owner but is not a foreign key.

`ai_task_occurrences.task_id` → `ai_tasks.id` is enforced by the task
migration with `ON DELETE RESTRICT`. Other relationships remain logical;
future changes require separate review.

---

## 18. RLS Policy Model

All tables have RLS enabled. Only SELECT policies are granted to
`anon` + `authenticated` (read-only dashboard access). All writes
(INSERT/UPDATE/DELETE) go through the backend's service-role key,
which bypasses RLS entirely. There are no anon/authenticated write
policies.

### Required Policies per Table

Every table must have exactly one SELECT policy:

```sql
CREATE POLICY "<table>_select" ON <table> FOR SELECT
  TO anon, authenticated USING (true);
```

The `USING (true)` is acceptable because:
1. All data belongs to a single owner (single-tenant self-bot).
2. The dashboard is read-only and has no sign-in screen.
3. All writes go through the backend service-role key (bypasses RLS).

Tables that do not yet have RLS enabled (`ai_preferences`) MUST have
RLS enabled and a SELECT policy added in their creation migration.
`ai_usage` and `ai_provider_stats` receive RLS + SELECT policies in
their creation migrations (`20260827000003` / `20260827000004`).

---

## 19. Panel Database

The Glass Panel system uses a **column-per-setting** model on the
`panel_settings` table. Each setting is a real typed column — no
key-value store, no JSONB blobs.

### Architecture

```
Supabase (panel_settings table)
  ↓
PanelSettingsRepository  (raw DB access — backend/services/panel_settings_repository.py)
  ↓
PanelSettingsService     (cache + validation — backend/services/settings_service.py)
  ↓
Glass Panel (reads via get_*(), writes via set_*())
```

### Cache-First Reads

Every getter reads from an in-memory cache. The database is NEVER
queried on a button click. The cache is loaded once at startup from
the DB (or from hardcoded defaults if the DB is unavailable).

### Write-Through Cache

On any `set_*()` call, the service:
1. Validates the value against a type/range validator.
2. Writes to the DB via the repository.
3. Reloads the cache from the DB.

Cache and DB are never left inconsistent.

### Settings (12 wired columns + `ghost_seen_retention_seconds` migrated-but-unwired)

| Column | Type | Default | Range/Constraint |
|---|---|---|---|
| `auto_close_enabled` | bool | `true` | must be boolean |
| `auto_close_delay` | int | `120` | 5..3600 (seconds) |
| `max_deep_save_mb` | int | `50` | 1..500 (MB) |
| `delete_batch_size` | int | `100` | 1..1000 |
| `log_retention_days` | int | `7` | 1..365 (days) |
| `panel_timeout_seconds` | int | `300` | 30..86400 (seconds) |
| `allow_multiple_panels` | bool | `false` | must be boolean |
| `reuse_existing_panel` | bool | `true` | must be boolean |
| `language` | str | `"en"` | non-empty string |
| `debug_callbacks` | bool | `false` | must be boolean |
| `owner_only` | bool | `true` | must be boolean |
| `dashboard_font` | str | `"default"` | one of `FONT_KEYS` |
| `ghost_seen_retention_seconds` | int | `2592000` | `0` (Never) or 300..31536000 (5 min..365 days) |

`ghost_seen_retention_seconds` has **no accessor in `settings_service`
and no production consumer** (see §19.13); it is listed here because
the column exists and the Glass UI presets are tested, not because it
is wired. If the DB is unavailable, the service uses hardcoded
`_DEFAULTS` for all 12 wired settings. The bot continues to function
normally — all panel operations work with default values. Every
Supabase call that fails logs a warning and falls back silently.

---

## 20. In-Memory Fallback

The bot is designed to run **with or without Supabase**. When the DB
is unavailable, all operations use in-memory fallbacks:

| Table | Fallback mechanism |
|---|---|
| `saved_items` | in-memory list in `db/client.py` |
| `bio_state` | in-memory dict in `db/client.py` |
| `username_state` | in-memory dict in `db/client.py` |
| `bot_logs` | in-memory list in `db/client.py` |
| `panel_settings` | hardcoded `_DEFAULTS` dict in `settings_service.py` |
| `ai_config` | `_fallback_config` dict in `config_store.py` |
| `ai_sessions` | `InMemorySessionRepository` in `database/manager.py` |
| `ai_messages` | `InMemoryMessageRepository` in `database/manager.py` |
| `ai_memories` | `InMemoryMemoryRepository` in `database/manager.py` |
| `ai_tool_history` | `InMemoryToolHistoryRepository` in `database/manager.py` |
| `ai_provider_stats` | `InMemoryProviderStatsRepository` in `database/manager.py` |
| `ai_usage` | `InMemoryUsageRepository` in `database/manager.py` |
| `ai_preferences` | `InMemoryPreferencesRepository` in `database/manager.py` |

The bot never crashes due to a database error. Every Supabase call that
fails logs a warning and falls back silently.

> **Design concern:** The silent fallback means schema problems are
> invisible at runtime. A table can be missing or a column can be absent
> and the bot will appear to function normally — but data is not
> persisted. The `ai_config` table is the most affected: trigger words
> and provider/model selections set by the user are lost on restart if
> the table or columns are missing. See [§19](#19-known-inconsistencies).

---

## 21. Known Inconsistencies

This section documents every discrepancy between the repository code,
the applied migrations, and this specification. Future migrations
MUST resolve all items marked **[MIGRATION REQUIRED]**.

### 19.1 `ai_config` — trigger columns not in any migration file

**Severity:** High

**Problem:** `config_store.py` reads and writes `trigger_en` and
`trigger_fa` columns on `ai_config`. The base migration
(`20260805075707`) creates `ai_config` but does NOT include these
columns. A migration file named `20260805130000_add_ai_trigger_columns`
was referenced in prior documentation but **does not exist** in the
`supabase/migrations/` directory.

**Impact:** When the bot tries to save trigger words to the database,
the Supabase API will either error (column does not exist) or silently
ignore the fields. The in-memory fallback catches the error, so the
bot continues running, but trigger words are lost on restart.

**Resolution [MIGRATION CREATED]:** Migration
`20260827000002_add_ai_config_trigger_columns.sql` adds
`trigger_en TEXT DEFAULT NULL` and `trigger_fa TEXT DEFAULT NULL` to
the `ai_config` table — pending manual application.

### 19.2 `ai_config` — `last_request_at` / `last_latency_ms` (RESOLVED in code)

**Severity:** Resolved

**Status:** `config_store.py::_save_config_sync` now includes
`last_request_at` and `last_latency_ms` in the DB payload, and
`record_request()` performs a targeted update of only those two columns.
The columns exist in the migration (`20260805075707`). No action
remaining for this item.

### 19.3 `panel_settings` — 10 columns missing from migrations

**Severity:** High

**Problem:** `settings_service.py` reads and writes 12 typed settings
columns on `panel_settings` (11 base settings + `dashboard_font`).
The base applied migrations only create 4 columns:
`key`, `auto_close_enabled`, `updated_at`, `update_stale_seconds`.
The remaining 10 columns (`auto_close_delay`, `max_deep_save_mb`,
`delete_batch_size`, `log_retention_days`, `panel_timeout_seconds`,
`allow_multiple_panels`, `reuse_existing_panel`, `language`,
`debug_callbacks`, `owner_only`) were supposed to be added by
migrations `20260730220000_panel_settings_column_model.sql` and
`20260730230000_panel_settings_full_13_columns.sql`, but **neither
file exists** in the repository.

**Impact:** When `panel_settings_repository.load()` tries to `SELECT *`
from the table, it will only get 4 columns. The missing columns will
be absent from the response dict. The `settings_service` cache-first
approach will fall back to hardcoded defaults for all missing columns.
Settings changes by the user are not persisted.

**Resolution [MIGRATION CREATED]:** Migration
`20260827000001_add_missing_panel_settings_columns.sql` adds all
10 missing columns to `panel_settings` with the types and defaults
listed in [§6](#6-panel_settings) (plus CHECK constraints mirroring the
`settings_service` validators) — pending manual application.

Migration `20260823120000_add_dashboard_font_and_ghost_seen_settings.sql`
now exists and covers `dashboard_font` and (originally)
`ghost_seen_retention_days`; it was applied to the live database and
verified by the owner. `20260823130000_ghost_seen_retention_duration.sql`
then migrates that setting to `ghost_seen_retention_seconds`
(idempotent; backfills and drops the days column) — pending manual
application. The remaining columns still have no migration file.

### 19.4 `bot_settings` table — NOT orphaned; Ghost Seen allow-list lives here

**Severity:** Corrected (previous doc claim was wrong)

**Problem:** This document previously claimed `bot_settings` had no
code references and should be dropped. That is false: Ghost Seen v2
(`backend/services/ghost_seen_v2.py`) actively reads and writes the
key `ghost_seen_allowed_chats` (JSON array of chat IDs) — loaded once
per process by `_ensure_allowed_loaded_async()` and persisted on every
Manage toggle by `_persist_allowed_to_db()`. It is the **only durable
store for the Ghost Seen per-chat privacy allow-list**.

**Verified restart lifecycle (persistence audit):** toggles run
`Manage → _toggle_permission_action → await _ensure_allowed_loaded_async()
→ allow_chat()/disallow_chat() → _persist_allowed_to_db()` (UPDATE, or
INSERT on first write, of the JSON array at `key =
'ghost_seen_allowed_chats'`). After a restart the handler `register()`
fires a background preload, and Browser/Manage open plus every toggle
await the SAME single in-flight load task; the loaded JSON list is
unioned into the runtime set and enforced by `is_chat_allowed()`.
Persistence races are closed: concurrent callers await the in-flight
load (a toggle can never persist a partial list over the persisted one)
and persist writes are serialized with an in-lock snapshot (the DB can
never end on an out-of-order stale value). Regression coverage:
`tests/test_65_ghost_seen_v2_restart_persistence.py`.

The table still has no `owner_id` (single-tenant assumption) and stores
a JSON blob where a relational representation is appropriate, but it
cannot be dropped until the allow-list is migrated to `ghost_chats`
(§22, §24, §20 item 8/9).

### 19.5 `ai_messages.tool_calls` — three-way mismatch

**Severity:** High

**Problem:** Three sources disagree about the `tool_calls` column:
- `MessageRecord` in `message_repository.py` defines it as a field.
- This document lists it as a column.
- The applied migration (`20260804145402`) does NOT create it.
- `persistence.py` does NOT write it.

**Impact:** If a Supabase-backed `MessageRepository` is ever wired up,
inserts will fail because the column does not exist in the database.

**Resolution [MIGRATION REQUIRED]:** A future migration must add
`tool_calls JSONB DEFAULT '[]'` to `ai_messages`. Code in
`persistence.py` should also be updated to populate it — but that is a
code fix, not a migration.

### 19.6 `ai_tool_history.result_data` — never written

**Severity:** Low

**Problem:** The `result_data` column exists in the migration and in
`ToolHistoryRecord`, but `persistence.py` never includes it in the
insert payload. It defaults to `'{}'` and is never populated.

**Resolution [CODE REQUIRED]:** `persistence.py` should include
`result_data` in its insert payload if available. This is a code fix,
not a migration.

### 19.7 `saved_items.short_code` / `saved_items.file_name` — dead columns

**Severity:** Low

**Problem:** Migration `20260718143752` added `short_code` and
`file_name` columns to `saved_items` and created 5 trigram GIN indexes
on them. No Python code ever reads or writes `short_code`. No Python
code ever writes `file_name` (the insert payload from `save_service.py`
does not include it). These columns and their dependent indexes are
dead weight.

**Resolution [MIGRATION REQUIRED]:** A future migration should drop
the trigram indexes and the dead columns. This must be done carefully —
dropping columns is a destructive operation that should only be
performed after confirming no data of value exists in them.

### 19.8 `ai_provider_stats` / `ai_usage` / `ai_preferences` — no migrations

**Severity:** Medium

**Problem:** Three AI tables have repository interfaces and in-memory
implementations but no migration has been applied for any of them.
The runtime operates entirely in-memory for these tables.

**Resolution [MIGRATION REQUIRED]:** The code now wires Supabase-backed
implementations for `ai_provider_stats` and `ai_usage` (via
`RepositoryManager` + `usage_recorder.py`); `ai_preferences` remains
interface + in-memory only. The runtime degrades to in-memory behavior
when Supabase is unavailable or the tables are missing. Migrations for
`ai_provider_stats` (`20260827000004`) and `ai_usage` (`20260827000003`)
have been generated and are pending manual application;
`ai_preferences` ([§14](#14-ai_preferences)) still has no migration
(see §20, item 6).

### 19.9 AI configuration persistence is non-deterministic

**Severity:** High

**Problem:** The AI configuration flow is:
1. User selects a provider → `config_store.save_config()` writes to DB.
2. If the DB write fails (table missing, column missing, network error),
   the error is caught, a warning is logged, and the in-memory fallback
   is updated.
3. On the next read, `config_store.get_config()` reads from the DB.
4. If the DB read fails, it falls back to the in-memory dict.
5. On restart, the in-memory dict is lost. The DB has no data. The
   user's configuration is gone.

This means configuration persistence depends on whether the database
is available and has the correct schema. If the schema is wrong, the
bot appears to work but does not persist anything.

**Resolution:** Resolve issues 19.1 and 19.3 (add missing columns via
migrations). After that, the configuration flow will be deterministic:
writes either succeed (data persists) or fail (error is visible).

### 19.10 `ai_messages.telegram_msg_id` — optional column for persistent reply resolution

**Severity:** Low (enhancement, not a bug)

**Problem:** The `ReplyResolver` (`backend/ai/context/reply_resolver.py`)
maps Telegram message IDs to full AI response content so that when the
owner replies to a previous AI message, the full untruncated AI text is
injected as high-priority context. Currently this mapping is stored
in-memory only (bounded LRU, 500 entries). On restart, all mappings are
lost — the owner can only get reply-aware context for AI messages
produced during the current process lifetime.

**Current behavior:** In-memory resolution works for active sessions.
This is the primary use case (the owner replies to a recent AI message
within the same session). The 200-character `text_preview` is used as
a fallback when the in-memory mapping is not found.

**Optional future schema change:** Add a `telegram_msg_id` column to
`ai_messages` so the resolver can fall back to a DB lookup when the
in-memory map misses. This would enable reply-aware context across
restarts.

**Required schema change (if implemented):**

| Table | Column | Type | Nullable | Default | Index | Purpose |
|---|---|---|---|---|---|---|
| `ai_messages` | `telegram_msg_id` | `bigint` | YES | `NULL` | `idx_ai_messages_tg_msg` (btree on `telegram_msg_id`) | Stores the Telegram message ID that contains the AI response. Enables the ReplyResolver to look up full AI content by Telegram message ID across restarts. |

**Why it is required:** Without this column, the in-memory resolver
cannot be backed by persistent storage. The column is nullable because
not all AI messages have a Telegram message ID (e.g. internal/system
messages). The index enables fast lookup by `telegram_msg_id` when
resolving a reply.

**Important:** This schema change is NOT required for the current
reply-aware context feature to work. The in-memory resolver is the
primary mechanism. This column would only add cross-restart persistence
as a fallback. Application code must NOT depend on this column existing —
the resolver must always handle the case where the column is absent.

### 19.11 `ai_database/manager.py` — partial Supabase wiring

**Severity:** Medium

**Problem:** `backend/ai/database/manager.py` wires Supabase-backed
implementations for **three** repositories when Supabase env vars are
present: `memory` (`SupabaseMemoryRepository` → `ai_memories`),
`provider_stats` (`SupabaseProviderStatsRepository` →
`ai_provider_stats`), and `usage` (`SupabaseUsageRepository` →
`ai_usage`). The remaining four (`session`, `message`, `preferences`,
`tool_history`) are still in-memory only. Separately, `persistence.py`
handles `ai_sessions`, `ai_messages`, `ai_tool_history` directly —
bypassing the repository pattern.

**Impact:** `ai_usage` and `ai_provider_stats` have **no applied
migration**, so their Supabase writes currently fail and degrade to
logged warnings (see §19.8). `ai_memories` has a live repository path
via the Engine, but nothing writes memories automatically (auto-memory
is off by default).

**Resolution [CODE REQUIRED]:** Either wire the remaining Supabase
implementations into the repository manager or remove the repository
abstraction.

### 19.12 `ghost_chats` — migrated table with no code consumer

**Severity:** High (schema hygiene + Ghost Seen correctness)

**Problem:** Migration `20260822090000` created `ghost_chats`
(`chat_id`, `display_name`, `last_preview`, `last_message_at`,
`unread_count`, timestamps) for the former "Ghost Room" design.
**No Python module reads or writes this table** — the only references
are skipped legacy tests in `tests/test_51_execution27.py`. The legacy
`backend/services/ghost_seen_service.py` that targeted it was removed
in the Ghost Seen v2 rebuild. The live Ghost Seen v2 feature persists
its per-chat privacy allow-list in `bot_settings` instead (§19.4).

**Impact:** Dead table today, and the allow-list lives in a legacy
key-value table without owner scoping.

**Resolution [MIGRATION + CODE REQUIRED]:** Repurpose `ghost_chats` as
the authoritative per-chat Ghost Seen state table (additive `owner_id`
+ `allowed` columns), migrate the `bot_settings` blob into it, then
drop `bot_settings`. See §22 and §24.

### 19.13 `ghost_seen_retention_seconds` — configured but unconsumed

**Severity:** Medium

**Problem:** The `panel_settings.ghost_seen_retention_seconds` column
(migration `20260823130000`, pending application) has **no accessor in
`settings_service.py`, no Glass UI panel, no retention job, and no code
consumer anywhere in `backend/`**. Only tests and this document
reference it. The legacy `ghost_seen_service.apply_retention` /
`delete_expired_rows` that would have consumed it was removed.

**Impact:** The setting is persisted but cannot be changed or read by
production code; no registry retention is enforced.

**Resolution:** Either wire the setting (accessor + Glass UI presets
already exist in tests) and implement a bounded retention job targeting
`ghost_chats` only (never Telegram RPC), or remove the column. The
schema is additive and correct as designed.

### 19.14 `panel_settings.update_stale_seconds` — migrated but never read

**Severity:** Low

**Problem:** Migration `20260730210551` added `update_stale_seconds`
(default 300). The heartbeat uses a hardcoded `_STALL_THRESHOLD = 90.0`
(`backend/runtime/heartbeat.py`) and never reads the setting; no other
module does either. The migration comment claiming "The watchdog reads
this value via settings_service on every tick" is false.

**Resolution [CODE REQUIRED]:** Either consume the setting in the
heartbeat invariant check or drop the column. Note the setting default
(300s) differs from the hardcoded threshold (90s) — wiring it changes
recovery behavior and must be tested.

### 19.15 `GHOST_SEEN_DESTINATION_CHAT_ID` / `_NAME` — defined, unused

**Severity:** Low

**Problem:** `backend/config.py` loads
`GHOST_SEEN_DESTINATION_CHAT_ID` / `GHOST_SEEN_DESTINATION_CHAT_NAME`
and `ghost_seen_v2.py` exposes `get_destination_chat_id()` /
`get_destination_chat_name()`. **No production code calls these
getters** — only tests. INVESTIGATION.md documents them as "for future
notification flows".

**Impact:** Dead environment configuration. Manual Reply and AI Reply
always deliver to the source private chat. If the destination flow is
never built, remove the env vars and getters; if it is built, it is
configuration (not database state) and must never override the source
chat as the AI Reply destination.

### 19.16 `dashboard_font` — backend 23-key list vs frontend 4-key list

**Severity:** Medium (UX, not schema)

**Problem:** The authoritative allow-list is `FONT_KEYS` (23 keys) in
`backend/helper/font_style.py`, enforced by `settings_service` and the
`panel_settings_dashboard_font_check` constraint. The web dashboard
(`src/App.tsx`) hardcodes 4 options (`default`, `system`, `mono`,
`serif`), of which `system` and `serif` are **not** valid backend keys
— the API rejects them, and any Glass-UI-selected key outside the 4
falls back to `default` in the dashboard.

**Impact:** The two surfaces can disagree about the active font after a
restart (dashboard shows its own fallback; Telegram panels keep the
persisted key).

**Resolution [CODE REQUIRED]:** Derive dashboard options from
`/api/settings` plus a shared key→CSS-stack map, or accept only backend
keys. Schema is correct; this is a frontend/backend contract fix.

### 19.17 `ai_preferences` — specified, in-memory only, no producer

**Severity:** Medium

**Problem:** `ai_preferences` has a full schema (§14) and an interface
+ `InMemoryPreferencesRepository`, but **no migration and no code path
that writes it**. `Dispatcher._load_preferences` reads
`preferences_repository.get_or_create(owner_id)` (in-memory only) and
falls back to `PreferencesContext()` defaults. No Glass UI or tool
writes preferences.

**Impact:** Personality/behavior preferences cannot persist across
restarts; every process start uses defaults.

**Resolution:** Either (a) add the migration + a Supabase-backed
repository + a Glass UI write path, or (b) mark the table speculative
and remove the interface until a producer exists.

### 19.18 `saved_items.save_code` — documentation drift (SV-NNNNNN → S####)

**Severity:** Low (documentation only)

**Problem:** §2 and the frontend type claimed codes like `SV-000001`.
The live generator `db/client.py::get_next_save_code` returns `S` + 4
characters (`S0001`, or a random `SXXXX` on collision) and writes only
`save_code` — the legacy `short_code` column (migration
`20260718143752`) is never written. Save type is always `'deep'`; the
`'forward'` value exists only for legacy rows and the CHECK constraint.

**Resolution:** §2 corrected in this revision. No schema change
required; `short_code`/`file_name` cleanup stays §19.7.

### 19.19 `ai_sessions` — primary-key drift between migration and spec

**Severity:** Low

**Problem:** The applied migration uses `id bigserial PRIMARY KEY` +
`session_id text UNIQUE`; this document (§8) and `persistence.py`
address rows by `session_id`. `ai_messages` / `ai_tool_history` /
`ai_usage` reference `session_id` logically (no FK).

**Resolution:** Acceptable as-is (unique `session_id` is the effective
key); optionally promote `session_id` to PK in a future cleanup
migration. No code change needed.

---

## 20. Migration Status

### Applied Migration Files (in `supabase/migrations/`)

| # | File | Creates / Alters | Status |
|---|---|---|---|
| 1 | `20260712234229_lifeos_schema.sql` | `saved_items`, `bio_state`, `bot_logs` (initial) | Superseded by #2 |
| 2 | `20260714111706_create_lifeos_tables.sql` | `saved_items`, `bio_state`, `bot_logs` (authoritative) | Applied |
| 3 | `20260718143752_...save_ux_redesign.sql` | Added `short_code`, `file_name` to `saved_items`; trigram indexes | Applied (dead columns — see §19.7) |
| 4 | `20260726143924_create_panel_settings_table.sql` | `panel_settings` (3 columns: `key`, `auto_close_enabled`, `updated_at`) | Applied (incomplete — see §19.3) |
| 5 | `20260729213959_...create_bot_settings_table.sql` | `bot_settings` key-value table | Applied (live consumer — see §19.4) |
| 6 | `20260730210551_...add_update_stale_seconds.sql` | Added `update_stale_seconds` to `panel_settings` | Applied |
| 7 | `20260801215007_create_username_state_table.sql` | `username_state` | Applied |
| 8 | `20260804145402_create_ai_tables.sql` | `ai_sessions`, `ai_messages`, `ai_memories`, `ai_tool_history` | Applied |
| 9 | `20260805075707_...create_ai_config_table.sql` | `ai_config` (base columns, no triggers) | Applied (incomplete — see §19.1) |
| 10 | `20260822090000_create_ghost_chats_table.sql` | `ghost_chats` table for Ghost Seen | Applied (verified by owner) |
| 11 | `20260823120000_add_dashboard_font_and_ghost_seen_settings.sql` | Added `dashboard_font`, `ghost_seen_retention_days` to `panel_settings` | Applied (verified by owner) |
| 12 | `20260823130000_ghost_seen_retention_duration.sql` | Replaces `ghost_seen_retention_days` with `ghost_seen_retention_seconds` (idempotent backfill + drop) | Pending manual application |

### Missing Migration Files (referenced in prior docs but never created)

| File | Purpose | Blocks |
|---|---|---|
| `20260730220000_panel_settings_column_model.sql` | Obsolete plan to migrate `bot_settings` into `panel_settings` — superseded by the column-per-setting model; `bot_settings` now hosts `ghost_seen_allowed_chats` | — |
| `20260730230000_panel_settings_full_13_columns.sql` | Add remaining 10 `panel_settings` columns | §19.3 |
| `20260805130000_add_ai_trigger_columns.sql` | Add `trigger_en` / `trigger_fa` to `ai_config` | §19.1 |
| *(never created)* `create_ai_usage_table.sql` | Create `ai_usage` from §13 | §19.8 |
| *(never created)* `create_ai_provider_stats_table.sql` | Create `ai_provider_stats` from §12 | §19.8 |
| *(never created)* `create_ai_preferences_table.sql` | Create `ai_preferences` from §14 | §19.8, §19.17 |

### Migrations That Must Be Generated From This Document

The following migrations do not exist yet and must be created to bring
the live database in sync with this specification:

1. **Add missing `panel_settings` columns** — add all 10 missing
   columns with types and defaults from [§6](#6-panel_settings). Do NOT
   drop `bot_settings` — it is a live consumer of Ghost Seen allow-list
   state (see §19.4).

2. **Add `trigger_en` / `trigger_fa` to `ai_config`** — add
   `trigger_en TEXT DEFAULT NULL` and `trigger_fa TEXT DEFAULT NULL`.

3. **Add `tool_calls` to `ai_messages`** — add
   `tool_calls JSONB DEFAULT '[]'`.

4. **Create `ai_provider_stats`** — full schema from [§12](#12-ai_provider_stats).

5. **Create `ai_usage`** — full schema from [§13](#13-ai_usage).

6. **Create `ai_preferences`** — full schema from [§14](#14-ai_preferences).

7. **Drop dead `saved_items` columns and indexes** — drop `short_code`,
   `file_name`, and all 5 trigram GIN indexes. (Low priority — only
   after confirming no data of value exists.)

8. **Correct `ghost_chats`** — the table already exists (applied,
   migration #10). The required migration adds `owner_id` and `allowed`
   and backfills from `bot_settings.ghost_seen_allowed_chats`. See
   [§22](#22-ghost-seen--ghost-pv) and §24.

9. **Drop `bot_settings`** — only after the §20 item 8 backfill is
   applied and Ghost Seen v2 reads/writes `ghost_chats` (code change
   paired with the migration). No code may reference `bot_settings`
   afterwards.

10. **Retention wiring (no schema change)** — implement a consumer for
    `ghost_seen_retention_seconds` (see §19.13) or remove the column.

---

## 22. Ghost Seen / Ghost PV (ghost_chats + bot_settings)

### 22.1 Current runtime state (source-verified)

Ghost Seen v2 (`backend/services/ghost_seen_v2.py` +
`backend/bot/handlers/ghost_seen_v2.py`) keeps the following state:

| State | Storage today | Survives restart? | Should it persist? |
|---|---|---|---|
| Per-chat privacy allow-list (`_allowed_chats`) | `bot_settings` key `ghost_seen_allowed_chats` (JSON array of chat IDs) | Yes (Supabase) / No (in-memory fallback) | **Yes** — authoritative per-chat permission |
| Message selection (`_selections`) | in-memory `dict[int, set[int]]` | No | No — transient UI state; never persisted (privacy) |
| Pending reply input (`_reply_states`) | in-memory `dict` | No | No — transient |
| AI Reply candidate state (`_ai_states` / `_ai_locks`, handler) | in-memory `dict` | No | No — transient |
| Manage directory cache (`_manage_directory`) | in-memory list + 60s TTL | No | No — cache |
| Browser/viewer page & query | lifecycle session nav stack | No | No — transient |
| `ghost_chats` registry rows | **unused table** | — | Designed for the corrected per-chat model (§24) |

The only durable Ghost Seen state is the allow-list. The `ghost_chats`
table exists in the live database but nothing writes it (§19.12). The
former `GHOST_ROOM_ID` output-routing env no longer exists anywhere in
the code; `GHOST_SEEN_DESTINATION_CHAT_ID/_NAME` are defined but unused
(§19.15).

### 22.2 Corrected table: `ghost_chats` — authoritative per-chat Ghost PV state

`ghost_chats` becomes the single authoritative table for per-chat Ghost
Seen state. The change is **additive-only** (no column is dropped):

| Column | Type | Nullable | Default | Purpose | Source requirement |
|---|---|---|---|---|---|
| `chat_id` | `bigint` | NO | — | Telegram chat ID (PK) | existing column |
| `owner_id` | `bigint` | NO | `0` | Owner scoping (single-tenant) | new — every other table carries `owner_id`; required for ownership consistency (§24) |
| `allowed` | `boolean` | NO | `false` | Ghost Seen opt-in permission for this chat | new — replaces the `bot_settings.ghost_seen_allowed_chats` JSON blob |
| `display_name` | `text` | NO | `''` | Last known display name | existing column |
| `last_preview` | `text` | NO | `''` | Last message preview (≤160 chars, PII-minimized) | existing column (write path optional — v2 currently renders live previews) |
| `last_message_at` | `timestamptz` | YES | — | Last activity | existing column |
| `unread_count` | `integer` | NO | `0` | Unread badge | existing column |
| `created_at` / `updated_at` | `timestamptz` | YES | `now()` | Timestamps | existing columns |

**Indexes:** keep `idx_ghost_chats_last_message`; add
`idx_ghost_chats_owner_allowed ON ghost_chats (owner_id) WHERE allowed`.

**RLS:** unchanged — single SELECT policy for `anon`, `authenticated`
USING `(true)`; all writes go through the service-role key.

**Migration path (one additive migration, see §20 items 8–9):**
`ADD COLUMN owner_id bigint NOT NULL DEFAULT 0`;
`ADD COLUMN allowed boolean NOT NULL DEFAULT false`; backfill from
`bot_settings.ghost_seen_allowed_chats`; index; afterwards drop
`bot_settings`. Paired code change (future, not part of this spec):
`ghost_seen_v2._ensure_allowed_loaded_async()` and
`_persist_allowed_to_db()` read/write this table instead of
`bot_settings`.

> **Proportionality note:** No per-message Ghost PV table is proposed.
> The feature processes messages but persists none of their content;
> selection/reply/AI-candidate state is explicitly transient. Message
> content never enters the database.

---

## 21. Migration Generation Rules

When generating new Supabase migrations from this document:

1. **This document is authoritative.** Every table, column, type,
   default, index, and constraint listed here must be created exactly
   as specified. If the code and this document disagree, this document
   wins — the code must be fixed to match.

2. **One migration per logical change.** Do not combine unrelated
   schema changes in a single migration. Each migration should address
   one item from the "Migrations That Must Be Generated" list in
   [§20](#20-migration-status).

3. **Always use `IF NOT EXISTS` / `IF EXISTS`.** Migrations must be
   idempotent — safe to re-run. Use `ADD COLUMN IF NOT EXISTS` (inside
   a `DO $$ ... END $$` block for older Postgres versions) and
   `DROP COLUMN IF EXISTS` / `DROP INDEX IF EXISTS`.

4. **Enable RLS on every new table.** Add one SELECT policy for
   `anon, authenticated` using `USING (true)` — this is a single-tenant
   self-bot, so all data is intentionally readable by the dashboard.

5. **Never use `DROP TABLE` or `DELETE` data** without explicit
   confirmation. The `bot_settings` drop and the dead-column drops in
   §19.7 are exceptions, documented and justified here.

6. **Never use `FOR ALL` in RLS policies.** Write separate policies per
   CRUD verb. For this project, only SELECT policies are needed (all
   writes go through the service-role key).

7. **Use `auth.uid()` for ownership checks** only in multi-user apps.
   This is a single-tenant self-bot — `USING (true)` is correct for
   SELECT policies.

8. **Test migrations against this document.** After generating a
   migration, verify every column, type, default, and index matches
   the specification in this document.

9. **Update this document before writing the migration.** If a schema
   change is needed, first update this document, then generate the
   migration from the updated spec. Never generate a migration that
   contradicts this document.

10. **Document the migration in §20.** After generating a migration,
    add it to the "Applied Migration Files" table with its file name,
    date, and a one-line description.

---

## 23. Self Bot Persistent State Inventory

Source-verified restart-survival matrix for every feature. "Storage"
is where the state lives today; "Must survive restart?" answers what
the actual source requires (not what would be nice).

| Feature | Persistent state | Storage today | Must survive restart? | Gap |
|---|---|---|---|---|
| Save / Deep Save | saved item metadata (code, origin, saved location, sender, media, tags, caption) | `saved_items` | **Yes** — retrieval/delete/discover/stats read it | none (schema drift only, §19.18) |
| Save codes | sequential counter + collision check | computed from `saved_items` count + unique check | **Yes** — codes must be stable | none |
| Bio automation | template, mood, custom_text, `is_active`, `last_bio` | `bio_state` | **Yes** — `ProfileEngine.updater` reads `is_active`/tokens from DB each minute | none |
| Username automation | template, mood, custom_text, `is_active`, `last_name` | `username_state` | **Yes** — same engine path | none |
| Profile scheduler runtime | active-engine flags, running task | in-memory (`profile/scheduler.py`) | No — restarted on boot from `BIO_UPDATE_ENABLED` / `USERNAME_UPDATE_ENABLED` + DB `is_active` rows | none |
| Panel / global settings | 12 typed settings | `panel_settings` (4 applied columns + `dashboard_font`; 10 columns missing §19.3) | **Yes** — settings must survive restart | §19.3 |
| Glass UI / dashboard font | selected font key | `panel_settings.dashboard_font` (CHECK on 23 keys) | **Yes** — persisted; key-surface mismatch §19.16 | §19.16 |
| Ghost Seen retention window | `ghost_seen_retention_seconds` | `panel_settings` column (migration pending) | **Yes** (intended) — but no consumer exists | §19.13 |
| Heartbeat stall threshold | `update_stale_seconds` | `panel_settings` column (applied) | No consumer | §19.14 |
| Ghost Seen allow-list | per-chat opt-in | `bot_settings.ghost_seen_allowed_chats` (JSON) | **Yes** — privacy permissions must survive restart | §19.4, §19.12 |
| Ghost Seen transient state | selections, reply input, AI candidate, page/query | in-memory | **No** — transient by design; never persisted | none |
| Ghost Seen registry rows | per-chat last-preview/unread metadata | `ghost_chats` (unused) | Only if the registry feature is built (retention setting implies intent) | §19.12, §24 |
| AI configuration | provider, model, temperature, max_tokens, system_prompt, history_budget, is_configured, triggers, last-request stats | `ai_config` | **Yes** — restored on boot | §19.1 (trigger columns missing) |
| AI sessions / messages / tool history | conversation history + audit | `ai_sessions`, `ai_messages`, `ai_tool_history` | **Yes** (per design) — written by `ai/persistence.py` | §19.5 (tool_calls col), §19.6 (result_data) |
| AI memories | short/long/permanent tiers | `ai_memories` (Supabase repo wired; no automatic writer) | **Yes** (per design) — opt-in writes only | none |
| AI usage + provider stats | per-request usage, per-provider aggregates | `ai_usage`, `ai_provider_stats` (repos wired) | **Yes** (per design) — tables not migrated | §19.8 |
| AI preferences | personality / behavior | in-memory only | **Yes** (per design) — no producer, no migration | §19.17 |
| AI reply context map | Telegram msg-id → full AI content | in-memory LRU (`reply_resolver.py`) | Optional enhancement (cross-restart reply resolution) | §19.10 |
| Structured logs | activity log | `bot_logs` | **Yes** (bounded by `log_retention_days`) | none |
| Runtime health telemetry | heartbeat/stats/diagnostics | in-memory (`health.py`, `diagnostics.py`) | No — by design | none |

Every "Yes" row is backed by a concrete source requirement; every
"No" row is transient UI/runtime state that must NOT be persisted.

---

## 24. Hermes Integration Boundary & Corrected Architecture

> **Scope note:** The supplied Hermes Runtime architecture document is
> NOT present in this repository (zero references to "hermes" exist in
> the tree). Hermes source was NOT inspected — nothing below claims it
> was. This section defines the boundary using the actual Telegram Self
> Bot source as the sole authority, and treats Hermes-owned persistence
> as a contract to be agreed with the Hermes side, not implemented here.

### A. Existing database architecture

See §1–§21. Summary: 13 live/specified tables (`saved_items`,
`bio_state`, `username_state`, `bot_logs`, `panel_settings`,
`ai_config`, `ai_sessions`, `ai_messages`, `ai_memories`,
`ai_tool_history`, `ai_provider_stats`, `ai_usage`, `ai_preferences`)
plus `bot_settings` (live, §19.4) and `ghost_chats` (orphaned,
§19.12). All access via the Supabase service-role key; SELECT-only RLS;
in-memory fallback everywhere.

### B. Existing Self Bot persistent state NOT correctly in the database

1. **Ghost Seen allow-list** — persisted, but in a legacy key-value
   table (`bot_settings`) with no owner scoping and a JSON blob
   (§19.4). Corrected representation: `ghost_chats` rows with
   `owner_id` + `allowed` (§22.2).
2. **Ghost Seen registry metadata** (`display_name`, `last_preview`,
   `last_message_at`, `unread_count`) — table exists, never written.
   Either write it (registry/retention feature) or keep it dormant;
   the retention setting (§19.13) implies the feature is intended.
3. **AI usage / provider stats / preferences** — implemented in code,
   no applied migrations (§19.8, §19.17).
4. **Settings columns without consumers** — `ghost_seen_retention_seconds`
   (§19.13), `update_stale_seconds` (§19.14).

### C. Proposed new persistent state (all additive; each justified by source)

| Proposal | Source requirement | Table / column |
|---|---|---|
| `ghost_chats.owner_id` | owner scoping — every other table carries `owner_id` | new column |
| `ghost_chats.allowed` | replaces `bot_settings.ghost_seen_allowed_chats` JSON blob; the allow-list is per-chat relational state | new column |
| `ai_messages.tool_calls` | `MessageRecord` defines it; migration missing | new column (§19.5) |
| `ai_config.trigger_en/fa` | `config_store` reads/writes them; migration missing | new columns (§19.1) |
| `ai_usage`, `ai_provider_stats`, `ai_preferences` tables | Supabase repositories are wired; tables missing | create (§19.8) |
| `panel_settings` 10 missing columns | `settings_service` reads/writes them | add columns (§19.3) |

Nothing else is proposed. Specifically NOT proposed: per-message Ghost
PV storage, Telegram message content storage, execution/action logs
beyond `ai_tool_history`, provider credentials, session strings.

### D. Hermes-owned state (external contract — not implemented here)

Per the supplied architecture reference, Hermes may own: proposals,
durable task state, reasoning history, learning artifacts. None of
this exists in this repository and none is implemented here. Hermes
must NEVER own Telegram session/credentials or become the executor of
Telegram operations.

### E. Self-Bot-owned state

Everything in §A–§C: Telegram feature state, per-chat permissions,
save/profile/settings/AI configuration, execution history, audit logs.
The Self Bot is the sole authority on what actually happened on
Telegram (send/deliver results, message IDs, timestamps).

### F. Cross-system references / synchronization

- Join keys are **Telegram-native identifiers**: `owner_id` (bigint,
  Telegram user ID), `chat_id` (bigint), `message_id` (bigint).
- Hermes may reference Self Bot rows by these IDs; the Self Bot is
  authoritative for them. Hermes proposal IDs may reference back into
  Hermes-owned tables only.
- No bidirectional FK constraints; logical references only (existing
  convention).
- No synchronization daemon is proposed. If Hermes needs proposal
  state shared with the Self Bot, the Self Bot consumes it as read-only
  input and validates before any Telegram execution.

### G. Security boundaries

1. The Self Bot remains the **sole Telegram Execution Authority**. The
   database stores durable state and history only — it never executes
   Telegram actions, calls providers, or contains business logic.
2. Hermes/AI may reason, propose, classify, and return candidate
   actions, but cannot directly execute arbitrary Telegram operations.
3. **No credentials in the database**: no `SESSION_STRING`, `API_HASH`,
   provider API keys, `BOT_TOKEN`. AI/Telegram credentials stay in
   environment variables only.
4. **No AI SQL execution**: no shell executor, no arbitrary SQL
   executor, no Telegram method executor are added anywhere.
5. `is_owner` remains the single permission gate for every handler;
   the DB is never a trust boundary for Telegram actions.

### H. RLS / ownership

- Single-tenant self-bot: every table keeps exactly one SELECT policy
  for `anon, authenticated` USING `(true)`; all writes go through the
  service-role key (bypasses RLS).
- `owner_id` must exist on every table that holds owner-scoped data.
  `ghost_chats` gains it (§22.2); `bot_settings` is migrated away from;
  `panel_settings` is a global singleton (no `owner_id`, by design).
- No anon/authenticated write policies anywhere.

### I. Migration strategy

Follow §21 rules: doc-first, one logical change per migration,
idempotent (`IF NOT EXISTS`), RLS SELECT-only, log in §20. Priority
order: (1) §19.1 trigger columns + §19.3 panel_settings columns;
(2) `ai_usage` + `ai_provider_stats` (+ decide `ai_preferences`);
(3) `ghost_chats` correction + `bot_settings` drop (§20 items 8–9);
(4) §19.5/§19.6 column additions; (5) §19.7 dead-column cleanup.
Every migration is additive or explicitly documented destructive
(§19.7, §20 item 9) — never a surprise drop.

### J. Open decisions / unknowns

1. **`ai_preferences`**: wire fully (migration + producer + UI) or
   remove until a producer exists (§19.17).
2. **Ghost Seen registry**: is the dormant `ghost_chats` preview/unread
   metadata feature (and its retention setting) going to be built? If
   yes, the retention consumer (§19.13) is required; if no, drop the
   setting and keep the table as permission-only.
3. **`GHOST_SEEN_DESTINATION_CHAT_ID/_NAME`**: build the notification
   flow or remove (§19.15).
4. **`update_stale_seconds`**: wire into the heartbeat or drop (§19.14).
5. **Dashboard font surface**: align frontend options with the backend
   23-key allow-list (§19.16).
6. **`ai_sessions` PK** promotion to `session_id` (§19.19).
7. **`ai_messages.telegram_msg_id`** cross-restart reply resolution
   (§19.10) — optional enhancement, not required.
8. **Hermes-side schema** — cannot be verified from this repository;
   must be agreed with the Hermes repository owner.

### Per-table rationale for the corrected/proposed objects

Every column in §22.2 and §C answers "what actual source-code
requirement caused this field to exist?"; anything without a source
requirement is marked **REQUIRES SOURCE VERIFICATION** rather than
invented. The only schema object in this revision that is not yet
backed by a live writer is `ghost_chats.last_preview` / `unread_count`
— they are preserved (existing columns, no reason to drop) and flagged
under decision J.2.

---

## 25. Font System Persistence (current, verified)

### 25.1 What the font system actually is

The Glass UI font system is a **code-defined enumerated transform
registry**, not a database of font definitions:

- **Definitions:** `backend/helper/font_style.py` — `FontDef(key, label,
  convert, has_digit_glyphs)`; one authoritative allow-list `FONT_KEYS`
  (23 keys: `default`, `serif_bold`, `serif_italic`, `serif_bold_italic`,
  `sans`, `sans_bold`, `sans_italic`, `sans_bold_italic`, `script`,
  `script_bold`, `fraktur`, `fraktur_bold`, `double_struck`, `mono`,
  `small_caps`, `circled`, `circled_dark`, `fullwidth`, `parenthesized`,
  `underline`, `strikethrough`, `overline`, `wavy_underline`).
- **Identifier/slug:** the `key` string (e.g. `sans_bold`) IS the stable
  slug; there is no separate slug field.
- **Display name:** `FontDef.label` (e.g. "Sans Bold"), rendered
  self-demonstrating via `font_option_label()`.
- **Font definition:** a deterministic per-character Unicode transform;
  no external font resources, no CSS, no free-text input.
- **Enabled state:** every registry entry is always available; there is
  no per-font enable/disable state in the source.
- **Default selection:** the `default` key; `normalize_font_key()`
  falls back to it for any invalid key.
- **Ordering:** registry order (default first) — `FONT_KEYS` tuple order.
- **Metadata / versioning:** none. No version field, no custom fonts, no
  per-owner groupings.

### 25.2 What persists

Only ONE value is user-selected durable state: the active font key.

| Storage | Column / type | Constraint | Consumer |
|---|---|---|---|
| `panel_settings.dashboard_font` | `text NOT NULL DEFAULT 'default'` | CHECK against the 23-key allow-list (migration `20260823120000`) | Glass UI panel rendering (`helper/panel_render.py`), Bio/Username profile rendering (`profile/engine.py`), web dashboard (`src/App.tsx`) |

Read/write path: `settings_service.dashboard_font()` /
`set_dashboard_font()` → `panel_settings_repository` (write-through
cache). The dashboard maps the key to a fixed CSS stack; the Glass UI
maps it to the letter transform.

**Verified restart lifecycle (persistence audit):** selection
(`font_set` panel action or `PATCH /api/settings`) → validator
(`value in FONT_KEYS`) → `panel_settings_repository.update_field(
'dashboard_font', key)` (UPDATE … WHERE key = 'global') → on restart
`RuntimeSupervisor.start()` calls `settings_service.load_all()`
(`backend/runtime/supervisor.py`), hydrating the cache from
`panel_settings` before panels render → `dashboard_font()` serves the
restored key to `panel_render`, `profile/engine`, and
`GET /api/settings`. `load_all()` never writes defaults back to the DB,
so a restart cannot overwrite the persisted value. Caveat (pre-existing,
§19.3): if the DB write fails (e.g. migration not yet applied live),
`set_setting` degrades to the in-memory cache and still reports success
— the value sticks for the session only. Regression:
`tests/test_42_dashboard_font.py` (write-reaches-DB + restart restore +
startup-hydration guard).

### 25.3 Gaps / decisions

- **Frontend key-surface mismatch** — §19.16 (dashboard hardcodes 4
  keys; `system`/`serif` are rejected by backend validation).
- **PROPOSED:** none. No font table is needed; font definitions are
  code, and the single selected key is already a typed column. Adding a
  `fonts` table (slug/definition/order/version) would duplicate
  `font_style.py` with no source requirement.

---

## 26. Current vs Proposed Status Matrix

Legend: **CURRENT (verified)** = read/written by live code today.
**PROPOSED** = specified here; no migration applied and/or no live
producer yet.

| Domain / table | Status | Live writer/reader | Migration applied? |
|---|---|---|---|
| `saved_items` | CURRENT | `db/client.py`, `save_service`, retrieve/delete/discover/organize, `web/app` | Yes |
| `bio_state` | CURRENT | `db/client.py`, `profile/engine.py` (Bio) | Yes |
| `username_state` | CURRENT | `db/client.py`, `profile/engine.py` (Username) | Yes |
| `bot_logs` | CURRENT | `db/client.py::log` | Yes |
| `panel_settings` | CURRENT (12 accessors; 10 columns missing) | `settings_service` + `panel_settings_repository` | Partial — §19.3 |
| `ai_config` | CURRENT (trigger columns missing) | `config_store` | Partial — §19.1 |
| `ai_sessions` | CURRENT | `ai/persistence.py` | Yes |
| `ai_messages` | CURRENT (tool_calls col missing) | `ai/persistence.py` | Partial — §19.5 |
| `ai_memories` | CURRENT (repo wired; no auto writer) | `SupabaseMemoryRepository` via Engine | Yes |
| `ai_tool_history` | CURRENT | `ai/persistence.py`, `ai/tools/executor.py` | Yes |
| `bot_settings` | CURRENT (allow-list) | `ghost_seen_v2` | Yes |
| `ghost_chats` | **PROPOSED (corrected)** | no live writer today | Table exists; §22.2 correction not applied |
| `ai_usage` | **PROPOSED** | `SupabaseUsageRepository` + `usage_recorder` (wired; fails silently without table) | No |
| `ai_provider_stats` | **PROPOSED** | `SupabaseProviderStatsRepository` (wired) | No |
| `ai_preferences` | **PROPOSED** | interface + in-memory only; no producer | No |
| Ghost Seen allow-list | CURRENT (via `bot_settings`) → PROPOSED migration to `ghost_chats.allowed` | `ghost_seen_v2` | Backfill = §20 item 8 |
| Ghost Seen transient state | Intentionally NOT persisted (CURRENT design decision) | in-memory | n/a |
| Ghost Seen watermark / exclusions / delays | Do NOT exist in source; deliberately NOT added (would be REQUIRES SOURCE VERIFICATION) | — | n/a |
| Font definitions | CURRENT, code-only (`font_style.py`) | — | n/a |
| Font selection | CURRENT | `panel_settings.dashboard_font` | Yes |
| `ai_config.last_request_at` / `last_latency_ms` | CURRENT (resolved §19.2) | `config_store` | Yes (columns) |
| Scheduler active flags | CURRENT (runtime) + durable per-engine `is_active` in `bio_state` / `username_state` | `profile/scheduler.py` + engines | Yes |

---

## Final Canonical Contract

The canonical database consists of the following public-schema tables: `saved_items`, `bio_state`, `username_state`, `bot_logs`, `panel_settings`, `bot_settings`, `ai_config`, `ai_sessions`, `ai_messages`, `ai_memories`, `ai_tool_history`, `ai_usage`, `ai_provider_stats`, and the compatibility-preserved legacy table `ghost_chats`. The complete definitions, defaults, constraints, indexes, RLS policies, and seeds are in the single SQL block in the next section.

All tables use RLS. `anon` and `authenticated` have SELECT-only policies with `USING (true)`; they have no INSERT, UPDATE, or DELETE policies. Backend writes use the service-role client. No foreign keys are required: identifier relationships are intentionally application-level.

### Persistent state contract

| State | Storage | Durable behavior |
|---|---|---|
| Saved item metadata | `saved_items` | Inserted after Deep Save and read by retrieval, search, delete, and dashboard paths |
| Bio automation | `bio_state` keyed by `owner_id` | Loaded/updated by the ProfileEngine |
| Username automation | `username_state` keyed by `owner_id` | Loaded/updated by the ProfileEngine |
| Global settings | `panel_settings` row `key='global'` | Required singleton is seeded; dashboard font is `dashboard_font text NOT NULL DEFAULT 'default'` with the 23-key CHECK |
| Ghost Seen enabled chats | `bot_settings` row `key='ghost_seen_allowed_chats'`, `value text` | JSON array of integer Telegram chat IDs; loaded at startup and consumed by `is_chat_allowed()` |
| AI configuration | `ai_config` keyed by `owner_id` | Provider/configuration and trigger fields persist across startup |
| AI sessions/messages/memories/tool history | respective AI tables | Persisted where current repositories write/read them |
| AI usage/provider aggregates | `ai_usage`, `ai_provider_stats` | Per-request telemetry and `(provider_name, owner_id)` aggregate upsert |

Ghost Seen does not persist messages, selections, reply state, pagination, locks, or temporary viewer state. `ghost_chats` is legacy compatibility storage and is not the Ghost Seen allow-list source.

### Required seeds

`panel_settings(key='global')` is required. The canonical SQL inserts it with `ON CONFLICT DO NOTHING`. Five legacy `bot_settings` compatibility rows are also inserted. The Ghost Seen allow-list row is intentionally not seeded: the first toggle creates it, and its initial logical value is an empty list. No historical application data or credentials are fabricated.

## Canonical Supabase Bootstrap SQL (Full Database Contract Audit)

**Audited revision:** `30bb3a426c2ec419be9d8f43373d85ce27d77099` (`origin/main`).

**Method:** repository-wide enumeration of every Supabase/PostgREST access
(`git grep` across `backend/`, `tests/`, `src/`: **60 `.table()` call sites,
zero `.rpc()` calls**), then payload-level tracing of every writer/reader into
the 16 chronological migrations under `supabase/migrations/`. The current
source code — not this document's earlier prose, not migration comments, and
not prior reports — is the authority this script was derived from.

### Application ↔ database contract matrix

| Table | Live code path (evidence) | Operations | Status |
|---|---|---|---|
| `saved_items` | `backend/db/client.py` (insert/query/list/search/delete/count/stats); payload built in `backend/services/save_service.py` | INSERT; SELECT `*`/projected; DELETE; COUNT; filters `owner_id`, `save_code`, `id IN (…)`; ORDER `created_at DESC`; range pagination | CURRENT |
| `bio_state` | `backend/db/client.py` (`get_or_create_bio_state`/`update_bio_state`/`get_bio_state`) | SELECT/INSERT/UPDATE keyed by `owner_id` | CURRENT |
| `username_state` | `backend/db/client.py` (`get_or_create_username_state`/…); migration `20260801215007` | SELECT/INSERT/UPDATE keyed by `owner_id` | CURRENT |
| `bot_logs` | `backend/db/client.py` `log`/`list_logs`/`count_logs`/`clean_logs`; `backend/runtime/startup_check.py` (dormant) | INSERT; SELECT/COUNT/DELETE by `owner_id` + `created_at` cutoff | CURRENT |
| `panel_settings` | `backend/services/panel_settings_repository.py` (sole accessor) | SELECT/UPDATE where `key='global'` | CURRENT |
| `bot_settings` | `backend/services/ghost_seen_v2.py` (`ghost_seen_allowed_chats` KV) | SELECT/INSERT/UPDATE by `key` | CURRENT |
| `ai_config` | `backend/ai/config_store.py` | SELECT/INSERT/UPDATE keyed by `owner_id` (manual upsert flow) | CURRENT |
| `ai_sessions` | `backend/ai/persistence.py` (create/update/get session) | INSERT/UPDATE/SELECT keyed by `session_id` | CURRENT |
| `ai_messages` | `backend/ai/persistence.py` (`add_message`/`get_messages`) | INSERT; SELECT by `session_id` ORDER `created_at` ASC | CURRENT |
| `ai_memories` | `backend/ai/persistence.py` (save/query/delete/delete_expired/count) | INSERT/SELECT/DELETE/COUNT; filters `owner_id`/`tier`/`category`/`importance` | CURRENT |
| `ai_tool_history` | `backend/ai/persistence.py::record_tool_call` | INSERT only | CURRENT |
| `ai_usage` | `backend/ai/database/usage_repository.py::SupabaseUsageRepository` | INSERT/SELECT/COUNT; filters `owner_id`, `created_at` range | CURRENT |
| `ai_provider_stats` | `backend/ai/database/provider_stats_repository.py::SupabaseProviderStatsRepository` | UPSERT `on_conflict="provider_name,owner_id"`; SELECT; COUNT | CURRENT |
| `ghost_chats` | **none** — zero `.table("ghost_chats")` references in `backend/`, `tests/`, `src/` | — | LEGACY (owner-gated drop) |

### Migration-history reconciliation applied by the canonical script

1. **Duplicate first migration superseded.** `20260712234229_lifeos_schema.sql`
   and `20260714111706_create_lifeos_tables.sql` both create
   `saved_items`/`bio_state`/`bot_logs`. The later file is authoritative: it
   removes the `save_type IN ('forward','deep')` CHECK, the
   `bot_logs.level IN ('INFO','WARN','ERROR')` CHECK, and the broad anon
   INSERT/UPDATE/DELETE policies. A raw chronological replay leaves
   `anon_update_bot_logs` alive (the later file never drops it). The canonical
   script follows the later intent and explicitly drops all anon write
   policies on every table.
2. **`ghost_seen_retention_days` is transient.** Created by `20260823120000`,
   then dropped by `20260823130000` (a destructive `DROP COLUMN` inside a
   conditional DO block). The canonical script establishes only the final
   state (`ghost_seen_retention_seconds`) and never replays the drop.
3. **Legacy columns preserved additively (no live writer in current code):**
   `saved_items.file_name` and `saved_items.short_code` (the current Deep-Save
   payload in `save_service.py` writes neither),
   `panel_settings.update_stale_seconds` (its migration comment claims a
   watchdog consumer; none exists in code),
   `panel_settings.ghost_seen_retention_seconds` (zero Python consumers), and
   `ai_tool_history.result_data` (never inserted). All are kept so historical
   rows and rollback paths stay valid; removal remains owner-gated.
4. **`username_state` is fully migrated** (`20260801215007`). An earlier audit
   note claiming it was migration-less was an indexing artifact; corrected here.
5. **Seeds.** The only row current code *requires* is the `panel_settings`
   singleton (`key='global'` — repository updates target it and silently
   no-op without it). The five `bot_settings` default rows from
   `20260729213959` are consumed by no current reader; they are retained
   verbatim (`ON CONFLICT DO NOTHING`) for migration fidelity and classified
   legacy. `ghost_seen_allowed_chats` is created at runtime by
   `ghost_seen_v2` and is deliberately NOT seeded.
6. **No functions, triggers, or RPC endpoints are required** — the codebase
   contains zero `.rpc(` calls.
7. **Extension.** `pg_trgm` is required by the `saved_items` trigram indexes
   backing the `search_saves` ILIKE paths.

A byte-identical copy of this script also lives at
`supabase/canonical_bootstrap.sql` for convenience; **this section is the
canonical reference.**

### The script (single copy-pasteable block — Supabase SQL Editor)

```sql
-- ============================================================================
-- LifeOS / Telegram Self-Bot — Canonical Supabase Bootstrap & Reconciliation
--
-- Generated from a repository-wide database contract audit of commit
-- 30bb3a426c2ec419be9d8f43373d85ce27d77099 (origin/main).
--
-- Properties
--   * Establishes the COMPLETE database state required by CURRENT code:
--     13 code-active tables + 1 legacy table (ghost_chats, preserved).
--   * Safe on a FRESH database; idempotent/reconciling on an existing one
--     (IF NOT EXISTS / DO-guarded constraints / ON CONFLICT DO NOTHING).
--   * Additive only: no DROP TABLE, no DROP COLUMN, no data destruction.
--     The only intentional drops are anon WRITE policies that contradict
--     the documented SELECT-only dashboard boundary.
--   * Security model: ALL writes use the service-role key (bypasses RLS);
--     anon + authenticated get SELECT-only (read-only dashboard).
--   * No functions/triggers/RPC endpoints are created — the codebase has
--     zero .rpc() calls.
--   * Historical application data (saved items, AI sessions/messages/
--     memories/usage/stats, logs, allow-list values) is intentionally NOT
--     fabricated. Only deterministic, project-defined seed rows are inserted.
-- ============================================================================

BEGIN;

-- ─── 1. Extensions ──────────────────────────────────────────────────────────
-- pg_trgm powers the saved_items trigram indexes used by the
-- db_client.search_saves caption/save_code/mime_type ILIKE paths.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ─── 2. saved_items ─────────────────────────────────────────────────────────
-- Writer: backend/db/client.py (insert/query/list/search/delete/count/stats)
-- Payload: backend/services/save_service.py::execute_save
-- save_type has NO CHECK: 20260714111706 supersedes the ('forward','deep')
-- CHECK from 20260712234229.
-- file_name / short_code are legacy columns (no live writer) kept additively.
CREATE TABLE IF NOT EXISTS saved_items (
    id              bigserial    PRIMARY KEY,
    save_code       text         NOT NULL UNIQUE,
    save_type       text         NOT NULL DEFAULT 'forward',
    origin_chat_id  bigint,
    origin_msg_id   bigint,
    saved_chat_id   bigint,
    saved_msg_id    bigint,
    sender_name     text,
    sender_id       bigint,
    mime_type       text,
    file_id         text,
    file_size       bigint,
    media_type      text,
    tags            text[]       DEFAULT '{}',
    caption         text,
    file_name       text,
    short_code      text,
    owner_id        bigint       NOT NULL,
    created_at      timestamptz  DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_saved_items_owner            ON saved_items (owner_id);
CREATE INDEX IF NOT EXISTS idx_saved_items_save_code        ON saved_items (save_code);
CREATE INDEX IF NOT EXISTS idx_saved_items_created_at       ON saved_items (created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_saved_items_short_code
    ON saved_items (short_code) WHERE short_code IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_saved_items_owner_created    ON saved_items (owner_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_saved_items_caption_trgm     ON saved_items USING gin (caption gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_saved_items_file_name_trgm   ON saved_items USING gin (file_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_saved_items_save_code_trgm   ON saved_items USING gin (save_code gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_saved_items_short_code_trgm  ON saved_items USING gin (short_code gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_saved_items_mime_trgm        ON saved_items USING gin (mime_type gin_trgm_ops);

ALTER TABLE saved_items ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_insert_saved_items" ON saved_items;
DROP POLICY IF EXISTS "anon_update_saved_items" ON saved_items;
DROP POLICY IF EXISTS "anon_delete_saved_items" ON saved_items;
DROP POLICY IF EXISTS "anon_select_saved_items" ON saved_items;
CREATE POLICY "anon_select_saved_items" ON saved_items FOR SELECT
    TO anon, authenticated USING (true);

-- ─── 3. bio_state ───────────────────────────────────────────────────────────
-- Writer/reader: backend/db/client.py (bio state + Bio ProfileEngine)
CREATE TABLE IF NOT EXISTS bio_state (
    id           bigserial    PRIMARY KEY,
    owner_id     bigint       NOT NULL UNIQUE,
    template     text         NOT NULL DEFAULT '🕒 {time} | 💭 {mood}',
    mood         text         NOT NULL DEFAULT '😊',
    custom_text  text         NOT NULL DEFAULT '',
    is_active    boolean      NOT NULL DEFAULT false,
    last_bio     text         NOT NULL DEFAULT '',
    updated_at   timestamptz  DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_bio_state_owner ON bio_state (owner_id);

ALTER TABLE bio_state ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_insert_bio_state" ON bio_state;
DROP POLICY IF EXISTS "anon_update_bio_state" ON bio_state;
DROP POLICY IF EXISTS "anon_delete_bio_state" ON bio_state;
DROP POLICY IF EXISTS "anon_select_bio_state" ON bio_state;
CREATE POLICY "anon_select_bio_state" ON bio_state FOR SELECT
    TO anon, authenticated USING (true);

-- ─── 4. username_state ──────────────────────────────────────────────────────
-- Writer/reader: backend/db/client.py (username state + Username ProfileEngine)
CREATE TABLE IF NOT EXISTS username_state (
    id           bigserial    PRIMARY KEY,
    owner_id     bigint       NOT NULL UNIQUE,
    template     text         NOT NULL DEFAULT '{time} | {mood}',
    mood         text         NOT NULL DEFAULT '😊',
    custom_text  text         NOT NULL DEFAULT '',
    is_active    boolean      NOT NULL DEFAULT false,
    last_name    text         NOT NULL DEFAULT '',
    updated_at   timestamptz  DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_username_state_owner ON username_state (owner_id);

ALTER TABLE username_state ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_insert_username_state" ON username_state;
DROP POLICY IF EXISTS "anon_update_username_state" ON username_state;
DROP POLICY IF EXISTS "anon_delete_username_state" ON username_state;
DROP POLICY IF EXISTS "anon_select_username_state" ON username_state;
CREATE POLICY "anon_select_username_state" ON username_state FOR SELECT
    TO anon, authenticated USING (true);

-- ─── 5. bot_logs ────────────────────────────────────────────────────────────
-- Writer/reader: backend/db/client.py log/list_logs/count_logs/clean_logs
-- (also backend/runtime/startup_check.py, dormant).
-- level has NO CHECK: 20260714111706 supersedes the ('INFO','WARN','ERROR')
-- CHECK from 20260712234229.
CREATE TABLE IF NOT EXISTS bot_logs (
    id         bigserial    PRIMARY KEY,
    owner_id   bigint       NOT NULL,
    level      text         NOT NULL DEFAULT 'INFO',
    message    text         NOT NULL,
    context    jsonb        DEFAULT '{}',
    created_at timestamptz  DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_bot_logs_owner      ON bot_logs (owner_id);
CREATE INDEX IF NOT EXISTS idx_bot_logs_created_at ON bot_logs (created_at DESC);

ALTER TABLE bot_logs ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_insert_bot_logs" ON bot_logs;
DROP POLICY IF EXISTS "anon_update_bot_logs" ON bot_logs;
DROP POLICY IF EXISTS "anon_delete_bot_logs" ON bot_logs;
DROP POLICY IF EXISTS "anon_select_bot_logs" ON bot_logs;
CREATE POLICY "anon_select_bot_logs" ON bot_logs FOR SELECT
    TO anon, authenticated USING (true);

-- ─── 6. panel_settings ──────────────────────────────────────────────────────
-- Sole accessor: backend/services/panel_settings_repository.py (key='global').
-- Column set = the 12 settings_service._DEFAULTS settings + the two
-- legacy-orphan columns (update_stale_seconds, ghost_seen_retention_seconds)
-- preserved additively. CHECK ranges mirror settings_service._VALIDATORS.
CREATE TABLE IF NOT EXISTS panel_settings (
    key                          text        PRIMARY KEY,
    auto_close_enabled           boolean     NOT NULL DEFAULT true,
    auto_close_delay             integer     NOT NULL DEFAULT 120,
    max_deep_save_mb             integer     NOT NULL DEFAULT 50,
    delete_batch_size            integer     NOT NULL DEFAULT 100,
    log_retention_days           integer     NOT NULL DEFAULT 7,
    panel_timeout_seconds        integer     NOT NULL DEFAULT 300,
    allow_multiple_panels        boolean     NOT NULL DEFAULT false,
    reuse_existing_panel         boolean     NOT NULL DEFAULT true,
    language                     text        NOT NULL DEFAULT 'en',
    debug_callbacks              boolean     NOT NULL DEFAULT false,
    owner_only                   boolean     NOT NULL DEFAULT true,
    dashboard_font               text        NOT NULL DEFAULT 'default',
    update_stale_seconds         integer     NOT NULL DEFAULT 300,
    ghost_seen_retention_seconds bigint      NOT NULL DEFAULT 2592000,
    updated_at                   timestamptz DEFAULT now()
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'panel_settings_auto_close_delay_check') THEN
        ALTER TABLE panel_settings ADD CONSTRAINT panel_settings_auto_close_delay_check
            CHECK (auto_close_delay BETWEEN 5 AND 3600);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'panel_settings_max_deep_save_mb_check') THEN
        ALTER TABLE panel_settings ADD CONSTRAINT panel_settings_max_deep_save_mb_check
            CHECK (max_deep_save_mb BETWEEN 1 AND 500);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'panel_settings_delete_batch_size_check') THEN
        ALTER TABLE panel_settings ADD CONSTRAINT panel_settings_delete_batch_size_check
            CHECK (delete_batch_size BETWEEN 1 AND 1000);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'panel_settings_log_retention_days_check') THEN
        ALTER TABLE panel_settings ADD CONSTRAINT panel_settings_log_retention_days_check
            CHECK (log_retention_days BETWEEN 1 AND 365);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'panel_settings_panel_timeout_seconds_check') THEN
        ALTER TABLE panel_settings ADD CONSTRAINT panel_settings_panel_timeout_seconds_check
            CHECK (panel_timeout_seconds BETWEEN 30 AND 86400);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'panel_settings_language_check') THEN
        ALTER TABLE panel_settings ADD CONSTRAINT panel_settings_language_check
            CHECK (length(btrim(language)) > 0);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'panel_settings_dashboard_font_check') THEN
        ALTER TABLE panel_settings ADD CONSTRAINT panel_settings_dashboard_font_check
            CHECK (dashboard_font IN (
                'default', 'serif_bold', 'serif_italic', 'serif_bold_italic',
                'sans', 'sans_bold', 'sans_italic', 'sans_bold_italic',
                'script', 'script_bold', 'fraktur', 'fraktur_bold',
                'double_struck', 'mono', 'small_caps', 'circled',
                'circled_dark', 'fullwidth', 'parenthesized', 'underline',
                'strikethrough', 'overline', 'wavy_underline'
            ));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'panel_settings_ghost_seen_retention_seconds_check') THEN
        ALTER TABLE panel_settings ADD CONSTRAINT panel_settings_ghost_seen_retention_seconds_check
            CHECK (ghost_seen_retention_seconds = 0
                   OR ghost_seen_retention_seconds BETWEEN 300 AND 31536000);
    END IF;
END $$;

-- Required singleton row: repository updates target key='global' and
-- silently no-op without it.
INSERT INTO panel_settings (key) VALUES ('global')
ON CONFLICT (key) DO NOTHING;

ALTER TABLE panel_settings ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_insert_panel_settings" ON panel_settings;
DROP POLICY IF EXISTS "anon_update_panel_settings" ON panel_settings;
DROP POLICY IF EXISTS "anon_delete_panel_settings" ON panel_settings;
DROP POLICY IF EXISTS "anon_select_panel_settings" ON panel_settings;
CREATE POLICY "anon_select_panel_settings" ON panel_settings FOR SELECT
    TO anon, authenticated USING (true);

-- ─── 7. bot_settings ────────────────────────────────────────────────────────
-- Live consumer: backend/services/ghost_seen_v2.py (ghost_seen_allowed_chats
-- KV row, created at runtime — deliberately NOT seeded here).
-- The five seed rows below are legacy defaults from migration 20260729213959,
-- consumed by no current reader; retained verbatim for migration fidelity.
CREATE TABLE IF NOT EXISTS bot_settings (
    key         text        PRIMARY KEY,
    value       text        NOT NULL,
    value_type  text        NOT NULL DEFAULT 'str',
    updated_at  timestamptz DEFAULT now()
);

INSERT INTO bot_settings (key, value, value_type) VALUES
    ('auto_close_enabled', 'true', 'bool'),
    ('panel_auto_close_seconds', '120', 'int'),
    ('max_deep_save_mb', '50', 'int'),
    ('delete_batch_size', '100', 'int'),
    ('log_cleanup_days', '7', 'int')
ON CONFLICT (key) DO NOTHING;

ALTER TABLE bot_settings ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_insert_bot_settings" ON bot_settings;
DROP POLICY IF EXISTS "anon_update_bot_settings" ON bot_settings;
DROP POLICY IF EXISTS "anon_delete_bot_settings" ON bot_settings;
DROP POLICY IF EXISTS "anon_select_bot_settings" ON bot_settings;
CREATE POLICY "anon_select_bot_settings" ON bot_settings FOR SELECT
    TO anon, authenticated USING (true);

-- ─── 8. ai_config ───────────────────────────────────────────────────────────
-- Writer/reader: backend/ai/config_store.py (get/save/record_request).
-- trigger_en / trigger_fa are nullable text; the writer normalizes empty
-- strings to NULL. UNIQUE(owner_id) backs the writer's select-then-
-- insert/update flow.
CREATE TABLE IF NOT EXISTS ai_config (
    id              bigserial    PRIMARY KEY,
    owner_id        bigint       NOT NULL UNIQUE,
    provider        text         DEFAULT '',
    model           text         DEFAULT '',
    temperature     real         DEFAULT 1.0,
    max_tokens      integer      DEFAULT 4096,
    system_prompt   text         DEFAULT '',
    history_budget  integer      DEFAULT 4000,
    is_configured   boolean      DEFAULT false,
    trigger_en      text         DEFAULT NULL,
    trigger_fa      text         DEFAULT NULL,
    last_request_at timestamptz,
    last_latency_ms real         DEFAULT 0,
    created_at      timestamptz  DEFAULT now(),
    updated_at      timestamptz  DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ai_config_owner ON ai_config (owner_id);

ALTER TABLE ai_config ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_insert_ai_config" ON ai_config;
DROP POLICY IF EXISTS "anon_update_ai_config" ON ai_config;
DROP POLICY IF EXISTS "anon_delete_ai_config" ON ai_config;
DROP POLICY IF EXISTS "anon_select_ai_config" ON ai_config;
CREATE POLICY "anon_select_ai_config" ON ai_config FOR SELECT
    TO anon, authenticated USING (true);

-- ─── 9. ai_sessions ─────────────────────────────────────────────────────────
-- Writer/reader: backend/ai/persistence.py (create/update/get_session).
CREATE TABLE IF NOT EXISTS ai_sessions (
    id            bigserial    PRIMARY KEY,
    session_id    text         NOT NULL UNIQUE,
    owner_id      bigint       NOT NULL,
    provider      text         DEFAULT '',
    model         text         DEFAULT '',
    status        text         DEFAULT 'active',
    total_tokens  integer      DEFAULT 0,
    message_count integer      DEFAULT 0,
    created_at    timestamptz  DEFAULT now(),
    updated_at    timestamptz  DEFAULT now()
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_sessions_status_check') THEN
        ALTER TABLE ai_sessions ADD CONSTRAINT ai_sessions_status_check
            CHECK (status IN ('active', 'completed', 'error', 'closed'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_ai_sessions_owner      ON ai_sessions (owner_id);
CREATE INDEX IF NOT EXISTS idx_ai_sessions_session_id ON ai_sessions (session_id);

ALTER TABLE ai_sessions ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_insert_ai_sessions" ON ai_sessions;
DROP POLICY IF EXISTS "anon_update_ai_sessions" ON ai_sessions;
DROP POLICY IF EXISTS "anon_delete_ai_sessions" ON ai_sessions;
DROP POLICY IF EXISTS "anon_select_ai_sessions" ON ai_sessions;
CREATE POLICY "anon_select_ai_sessions" ON ai_sessions FOR SELECT
    TO anon, authenticated USING (true);

-- ─── 10. ai_messages ────────────────────────────────────────────────────────
-- Writer/reader: backend/ai/persistence.py (add_message/get_messages).
CREATE TABLE IF NOT EXISTS ai_messages (
    id          bigserial    PRIMARY KEY,
    session_id  text         NOT NULL,
    owner_id    bigint       NOT NULL,
    role        text         NOT NULL DEFAULT 'user',
    content     text         NOT NULL DEFAULT '',
    token_count integer      DEFAULT 0,
    provider    text         DEFAULT '',
    model       text         DEFAULT '',
    created_at  timestamptz  DEFAULT now()
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_messages_role_check') THEN
        ALTER TABLE ai_messages ADD CONSTRAINT ai_messages_role_check
            CHECK (role IN ('system', 'user', 'assistant', 'tool'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_ai_messages_session ON ai_messages (session_id);
CREATE INDEX IF NOT EXISTS idx_ai_messages_owner   ON ai_messages (owner_id);
CREATE INDEX IF NOT EXISTS idx_ai_messages_created ON ai_messages (created_at DESC);

ALTER TABLE ai_messages ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_insert_ai_messages" ON ai_messages;
DROP POLICY IF EXISTS "anon_update_ai_messages" ON ai_messages;
DROP POLICY IF EXISTS "anon_delete_ai_messages" ON ai_messages;
DROP POLICY IF EXISTS "anon_select_ai_messages" ON ai_messages;
CREATE POLICY "anon_select_ai_messages" ON ai_messages FOR SELECT
    TO anon, authenticated USING (true);

-- ─── 11. ai_memories ────────────────────────────────────────────────────────
-- Writer/reader: backend/ai/persistence.py
-- (save/query/delete_expired/delete/count_memories).
CREATE TABLE IF NOT EXISTS ai_memories (
    id          bigserial    PRIMARY KEY,
    owner_id    bigint       NOT NULL,
    tier        text         NOT NULL DEFAULT 'long',
    category    text         NOT NULL DEFAULT 'context',
    content     text         NOT NULL,
    importance  real         DEFAULT 0.5,
    expires_at  timestamptz,
    metadata    jsonb        DEFAULT '{}',
    created_at  timestamptz  DEFAULT now()
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_memories_tier_check') THEN
        ALTER TABLE ai_memories ADD CONSTRAINT ai_memories_tier_check
            CHECK (tier IN ('short', 'long', 'permanent'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_memories_category_check') THEN
        ALTER TABLE ai_memories ADD CONSTRAINT ai_memories_category_check
            CHECK (category IN ('fact', 'preference', 'context', 'summary', 'instruction'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_ai_memories_owner      ON ai_memories (owner_id);
CREATE INDEX IF NOT EXISTS idx_ai_memories_tier       ON ai_memories (tier);
CREATE INDEX IF NOT EXISTS idx_ai_memories_owner_tier ON ai_memories (owner_id, tier);

ALTER TABLE ai_memories ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_insert_ai_memories" ON ai_memories;
DROP POLICY IF EXISTS "anon_update_ai_memories" ON ai_memories;
DROP POLICY IF EXISTS "anon_delete_ai_memories" ON ai_memories;
DROP POLICY IF EXISTS "anon_select_ai_memories" ON ai_memories;
CREATE POLICY "anon_select_ai_memories" ON ai_memories FOR SELECT
    TO anon, authenticated USING (true);

-- ─── 12. ai_tool_history ────────────────────────────────────────────────────
-- Writer: backend/ai/persistence.py::record_tool_call (INSERT only).
-- result_data is migration-defined but never inserted by current code
-- (legacy-preserved column).
CREATE TABLE IF NOT EXISTS ai_tool_history (
    id             bigserial    PRIMARY KEY,
    owner_id       bigint       NOT NULL,
    session_id     text         DEFAULT '',
    tool_name      text         NOT NULL,
    arguments      jsonb        DEFAULT '{}',
    result_success boolean      DEFAULT false,
    result_message text         DEFAULT '',
    result_data    jsonb        DEFAULT '{}',
    latency_ms     real         DEFAULT 0,
    created_at     timestamptz  DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ai_tool_history_owner   ON ai_tool_history (owner_id);
CREATE INDEX IF NOT EXISTS idx_ai_tool_history_created ON ai_tool_history (created_at DESC);

ALTER TABLE ai_tool_history ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_insert_ai_tool_history" ON ai_tool_history;
DROP POLICY IF EXISTS "anon_update_ai_tool_history" ON ai_tool_history;
DROP POLICY IF EXISTS "anon_delete_ai_tool_history" ON ai_tool_history;
DROP POLICY IF EXISTS "anon_select_ai_tool_history" ON ai_tool_history;
CREATE POLICY "anon_select_ai_tool_history" ON ai_tool_history FOR SELECT
    TO anon, authenticated USING (true);

-- ─── 13. ai_usage ───────────────────────────────────────────────────────────
-- Writer/reader: SupabaseUsageRepository
-- (backend/ai/database/usage_repository.py).
-- id is NOT sent by the writer (bigserial); token_source carries the honesty
-- label verbatim (actual / estimated / unavailable).
CREATE TABLE IF NOT EXISTS ai_usage (
    id                bigserial    PRIMARY KEY,
    owner_id          bigint       NOT NULL,
    session_id        text,
    provider          text,
    model             text,
    prompt_tokens     integer      DEFAULT 0,
    completion_tokens integer      DEFAULT 0,
    total_tokens      integer      DEFAULT 0,
    latency_ms        real         DEFAULT 0,
    token_source      text         DEFAULT NULL,
    created_at        timestamptz  DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ai_usage_owner      ON ai_usage (owner_id);
CREATE INDEX IF NOT EXISTS idx_ai_usage_created_at ON ai_usage (created_at);

ALTER TABLE ai_usage ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_insert_ai_usage" ON ai_usage;
DROP POLICY IF EXISTS "anon_update_ai_usage" ON ai_usage;
DROP POLICY IF EXISTS "anon_delete_ai_usage" ON ai_usage;
DROP POLICY IF EXISTS "anon_select_ai_usage" ON ai_usage;
CREATE POLICY "anon_select_ai_usage" ON ai_usage FOR SELECT
    TO anon, authenticated USING (true);

-- ─── 14. ai_provider_stats ──────────────────────────────────────────────────
-- Writer/reader: SupabaseProviderStatsRepository
-- (backend/ai/database/provider_stats_repository.py) which upserts with
-- on_conflict="provider_name,owner_id" — the composite PRIMARY KEY below IS
-- that conflict target.
CREATE TABLE IF NOT EXISTS ai_provider_stats (
    provider_name            text        NOT NULL,
    owner_id                 bigint      NOT NULL DEFAULT 0,
    total_requests           integer     NOT NULL DEFAULT 0,
    successful_requests      integer     NOT NULL DEFAULT 0,
    failed_requests          integer     NOT NULL DEFAULT 0,
    total_prompt_tokens      integer     NOT NULL DEFAULT 0,
    total_completion_tokens  integer     NOT NULL DEFAULT 0,
    avg_latency_ms           real        NOT NULL DEFAULT 0,
    last_request_at          timestamptz,
    updated_at               timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (provider_name, owner_id)
);

ALTER TABLE ai_provider_stats ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_insert_ai_provider_stats" ON ai_provider_stats;
DROP POLICY IF EXISTS "anon_update_ai_provider_stats" ON ai_provider_stats;
DROP POLICY IF EXISTS "anon_delete_ai_provider_stats" ON ai_provider_stats;
DROP POLICY IF EXISTS "anon_select_ai_provider_stats" ON ai_provider_stats;
CREATE POLICY "anon_select_ai_provider_stats" ON ai_provider_stats FOR SELECT
    TO anon, authenticated USING (true);

-- ─── 15. ghost_chats (LEGACY — preserved, owner-gated removal) ──────────────
-- Zero .table("ghost_chats") references in current backend/tests/src code;
-- the live Ghost Seen allow-list is the bot_settings KV row. The table is
-- kept additively per the no-destruction rule; dropping it remains an owner
-- decision gated on a live-data check.
CREATE TABLE IF NOT EXISTS ghost_chats (
    chat_id         bigint       PRIMARY KEY,
    display_name    text         NOT NULL DEFAULT '',
    last_preview    text         NOT NULL DEFAULT '',
    last_message_at timestamptz,
    unread_count    integer      NOT NULL DEFAULT 0,
    created_at      timestamptz  DEFAULT now(),
    updated_at      timestamptz  DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ghost_chats_last_message
    ON ghost_chats (last_message_at DESC);

ALTER TABLE ghost_chats ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_insert_ghost_chats" ON ghost_chats;
DROP POLICY IF EXISTS "anon_update_ghost_chats" ON ghost_chats;
DROP POLICY IF EXISTS "anon_delete_ghost_chats" ON ghost_chats;
DROP POLICY IF EXISTS "anon_select_ghost_chats" ON ghost_chats;
CREATE POLICY "anon_select_ghost_chats" ON ghost_chats FOR SELECT
    TO anon, authenticated USING (true);

COMMIT;
```

### Script boundaries & uncertainty

- **Derived from current code**, not from this document's earlier prose
  (which the script supersedes where the two disagree).
- **Safe on a fresh database**; idempotent as a reconciliation pass on an
  existing database created from the migration history.
- **Live Supabase state was NOT inspected** (no authenticated live access in
  the audit environment) and the script has **NOT been executed** against any
  database. Validation was static: contract cross-check against every
  `.table()` call site plus the repository test suite.
- **No historical/user data is fabricated.** Only the deterministic
  `panel_settings('global')` singleton and the five legacy `bot_settings`
  default rows are seeded.
- **Owner-gated legacy decisions remain unchanged:** dropping `ghost_chats`,
  dropping `saved_items.short_code`/`file_name`, dropping the orphan
  `panel_settings` columns, implementing `ai_preferences`, AI-table retention
  policy, and live RLS-posture verification.
- The five legacy `bot_settings` seed rows are consumed by no current reader
  (retained for migration fidelity, not invention).
