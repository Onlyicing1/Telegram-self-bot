# Database Architecture — LifeOS Telegram Self-Bot

> **Source of truth for the complete Supabase schema.**
> This document describes every table, column, index, constraint, and RLS
> policy required by the application. It is verified against the actual
> code — every column listed here is read or written by a Python module.
>
> For SQL scripts, see the [`sql/`](sql/) directory (existing tables only).
> AI tables do not yet have migrations — see §12 for details.

---

## Table of Contents

1. [Overview](#1-overview)
2. [saved_items](#2-saved_items)
3. [bio_state](#3-bio_state)
4. [username_state](#4-username_state)
5. [bot_logs](#5-bot_logs)
6. [panel_settings](#6-panel_settings)
7. [ai_sessions](#7-ai_sessions)
8. [ai_messages](#8-ai_messages)
9. [ai_memories](#9-ai_memories)
10. [ai_tool_history](#10-ai_tool_history)
11. [Relationships](#11-relationships)
12. [RLS Policy Model](#12-rls-policy-model)
13. [Panel Database](#13-panel-database)
14. [In-Memory Fallback](#14-in-memory-fallback)
15. [Migration Status](#15-migration-status)

---

## 1. Overview

The database contains **10 tables** in the `public` schema — 5 for the
core LifeOS bot and 5 for the AI subsystem.

### Core Tables (migrations applied, SQL scripts exist)

| Table | Purpose | PK | Used by |
|---|---|---|---|
| `saved_items` | Media save records (forward + deep) | `id` (bigserial) | `db/client.py`, `save_service`, `retrieve_service`, `delete_service`, `discover_service`, `database_service`, `organize_service`, `web/app` |
| `bio_state` | Bio cron engine state per owner | `id` (bigserial) | `db/client.py`, `bio_service`, `bio/engine`, `organize_service`, `web/app` |
| `username_state` | Username cron engine state per owner | `id` (bigserial) | `db/client.py`, `username_service`, `username/engine` |
| `bot_logs` | Structured activity log | `id` (bigserial) | `db/client.py` (via `log()`), nearly every service, `web/app` |
| `panel_settings` | Glass Panel configuration (column-per-setting) | `key` (text) | `panel_settings_repository`, `settings_service`, `web/app` |

### AI Tables (migrations NOT yet applied — see §15)

| Table | Purpose | PK | Used by |
|---|---|---|---|
| `ai_sessions` | AI conversation session metadata | `session_id` (text) | `ai/persistence.py` |
| `ai_messages` | Individual AI messages within a session | `id` (bigserial) | `ai/persistence.py` |
| `ai_memories` | Three-tier memory (short, long, permanent) | `id` (bigserial) | `ai/persistence.py`, `ai/database/memory_repository.py` |
| `ai_tool_history` | Log of every tool call the AI made | `id` (bigserial) | `ai/persistence.py`, `ai/database/tool_history_repository.py` |
| `ai_provider_stats` | Per-provider aggregate statistics | `(provider_name, owner_id)` | `ai/database/provider_stats_repository.py` |
| `ai_config` | Per-owner AI configuration (provider, model, triggers, settings) | `id` (bigserial) | `ai/config_store.py` |

All access goes through the Supabase PostgREST API via the `supabase-py`
client. The backend uses the **service-role key**, which bypasses RLS.
The frontend reads via the backend API — it never touches Supabase
directly.

---

## 2. saved_items

Stores metadata for every media save operation (forward save and deep save).

### Columns

| Column | SQL Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | `bigserial` | NO | `nextval(...)` | Primary key |
| `save_code` | `text` | NO | — | Compact code, format `S####` (e.g. `S0001`). Sequential with collision detection. Unique. |
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

### Indexes

| Index | Columns | Type |
|---|---|---|
| `saved_items_pkey` | `id` | btree (PK) |
| `saved_items_save_code_key` | `save_code` | btree (UNIQUE) |
| `idx_saved_items_owner` | `owner_id` | btree |
| `idx_saved_items_created_at` | `created_at` | btree |
| `idx_saved_items_save_type` | `save_type` | btree |

### CHECK Constraints

- `saved_items_save_type_check`: `save_type IN ('forward', 'deep')`

### RLS

RLS is enabled. Only SELECT is granted to `anon` + `authenticated`.

### Repository

**`backend/db/client.py`** — all CRUD via `insert_save`, `query_save`,
`list_saves`, `list_recent_saves`, `search_saves`, `delete_save_row`,
`update_save_field`, `count_saves`, `list_all_saves`, `cleanup_orphans`,
`get_stats`, `get_next_save_code`.

---

## 3. bio_state

Singleton bio engine state per owner.

### Columns

| Column | SQL Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | `bigserial` | NO | `nextval(...)` | Primary key |
| `owner_id` | `bigint` | NO | `0` | Telegram user ID of the bot owner |
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

Singleton username engine state per owner. Mirrors `bio_state` exactly
in structure, but controls the Telegram `first_name` field instead of
the `about` field. Completely independent from the Bio Engine.

### Columns

| Column | SQL Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | `bigserial` | NO | `nextval(...)` | Primary key |
| `owner_id` | `bigint` | NO | `0` | Telegram user ID of the bot owner |
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

Glass Panel configuration. Singleton row (key = `"global"`).

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
write-through cache, 12 typed getters + 12 typed setters, per-setting
validators. See [§13 Panel Database](#13-panel-database) for details.

### Removed / phantom columns

The following columns were referenced in older documentation but are
**not** in the code. They do not exist in the database:

- ~~`panel_auto_close_seconds`~~ — superseded by `auto_close_delay`
- ~~`log_cleanup_days`~~ — superseded by `log_retention_days`
- ~~`diagnostics_enabled`~~ — never implemented in `settings_service.py`

---

## 7. ai_sessions

AI conversation session metadata. One row per active AI conversation.

> **Migration status:** Not yet applied. See [§15](#15-migration-status).

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

RLS should be enabled. SELECT to `anon` + `authenticated`. All writes
via service-role key.

### Repository

**`backend/ai/persistence.py`** — `create_session`, `update_session`,
`get_session`.

**`backend/ai/database/session_repository.py`** — interface + in-memory
fallback (`SessionRecord`, `SessionRepository`).

---

## 8. ai_messages

Individual AI messages within a conversation session.

> **Migration status:** Not yet applied. See [§15](#15-migration-status).

### Columns

| Column | SQL Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | `bigserial` | NO | `nextval(...)` | Primary key |
| `session_id` | `text` | NO | — | FK → `ai_sessions.session_id` (logical, not enforced) |
| `owner_id` | `bigint` | NO | — | Telegram user ID of the bot owner |
| `role` | `text` | NO | — | `user`, `assistant`, or `system` |
| `content` | `text` | NO | — | Message content (truncated to 8000 chars on write) |
| `token_count` | `integer` | YES | `0` | Estimated token count |
| `tool_calls` | `jsonb` | YES | — | Tool calls made in this message (JSON array) |
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

RLS should be enabled. SELECT to `anon` + `authenticated`. All writes
via service-role key.

### Repository

**`backend/ai/persistence.py`** — `add_message`, `get_messages`.

**`backend/ai/database/message_repository.py`** — interface + in-memory
fallback (`MessageRecord`, `MessageRepository`).

---

## 9. ai_memories

Three-tier memory system: short, long, and permanent memories.

> **Migration status:** Not yet applied. See [§15](#15-migration-status).

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

RLS should be enabled. SELECT to `anon` + `authenticated`. All writes
via service-role key.

### Repository

**`backend/ai/persistence.py`** — `save_memory`, `query_memories`,
`delete_expired_memories`.

**`backend/ai/database/memory_repository.py`** — interface + in-memory
fallback (`MemoryRepository`, `InMemoryMemoryRepository`).

**`backend/ai/memory/`** — `MemoryManager`, `ShortMemory`, `LongMemory`,
`PermanentMemory`.

---

## 10. ai_tool_history

Log of every tool call the AI has made. Used for auditing and debugging.

> **Migration status:** Not yet applied. See [§15](#15-migration-status).

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
| `result_data` | `jsonb` | YES | — | Result data payload |
| `latency_ms` | `real` | YES | `0` | Execution latency in milliseconds |
| `created_at` | `timestamptz` | YES | `now()` | When the tool was called |

### Indexes

| Index | Columns | Type |
|---|---|---|
| `ai_tool_history_pkey` | `id` | btree (PK) |
| `idx_ai_tool_history_owner` | `owner_id` | btree |
| `idx_ai_tool_history_session` | `session_id` | btree |

### RLS

RLS should be enabled. SELECT to `anon` + `authenticated`. All writes
via service-role key.

### Repository

**`backend/ai/persistence.py`** — `record_tool_call`.

**`backend/ai/database/tool_history_repository.py`** — interface +
in-memory fallback (`ToolHistoryRecord`, `ToolHistoryRepository`).

---

## 11. Relationships

There are **no enforced foreign keys** between any tables. Each table is
independent. The following logical relationships exist (not FK constraints):

- `ai_messages.session_id` → `ai_sessions.session_id` (logical)
- `ai_tool_history.session_id` → `ai_sessions.session_id` (logical)
- `owner_id` (on `saved_items`, `bio_state`, `username_state`, `bot_logs`,
  all AI tables) links rows to the bot owner but is not a foreign key.

---

## 12. RLS Policy Model

All tables have RLS enabled. Only SELECT policies are granted to
`anon` + `authenticated` (read-only dashboard access). All writes
(INSERT/UPDATE/DELETE) go through the backend's service-role key, which
bypasses RLS entirely. There are no anon/authenticated write policies.

---

## 13. Panel Database

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

Every getter reads from an in-memory cache. The database is NEVER queried
on a button click. The cache is loaded once at startup from the DB (or
from hardcoded defaults if the DB is unavailable).

### Write-Through Cache

On any `set_*()` call, the service:
1. Validates the value against a type/range validator.
2. Writes to the DB via the repository.
3. Reloads the cache from the DB.

Cache and DB are never left inconsistent.

### Settings (12 columns on panel_settings)

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
| `update_stale_seconds` | int | `300` | 60..3600 (seconds) |

### In-Memory Fallback

If the DB is unavailable, the service uses hardcoded `_DEFAULTS` for all
12 settings. The bot continues to function normally — all panel operations
work with default values. Every Supabase call that fails logs a warning
and falls back silently.

---

## 14. In-Memory Fallback

The bot is designed to run **with or without Supabase**. When the DB is
unavailable, all operations use in-memory fallbacks:

| Table | Fallback |
|---|---|
| `saved_items` | in-memory list |
| `bio_state` | in-memory dict |
| `username_state` | in-memory dict |
| `bot_logs` | in-memory list |
| `panel_settings` | hardcoded `_DEFAULTS` dict |
| `ai_sessions` | in-memory dict (via `InMemorySessionRepository`) |
| `ai_messages` | in-memory list (via `InMemoryMessageRepository`) |
| `ai_memories` | in-memory list (via `InMemoryMemoryRepository`) |
| `ai_tool_history` | in-memory list (via `InMemoryToolHistoryRepository`) |

The bot never crashes due to a database error.

---

## 15. Migration Status

### Applied migrations (core tables)

| Migration File | Date | Description |
|---|---|---|
| `20260712234229_lifeos_schema.sql` | 2026-07-12 | Initial schema: `saved_items`, `bio_state`, `bot_logs`. |
| `20260714111706_create_lifeos_tables.sql` | 2026-07-14 | Authoritative schema: `saved_items`, `bio_state`, `bot_logs`. |
| `20260718143752_save_ux_redesign.sql` | 2026-07-18 | Added `short_code`, `origin_chat_id`, `origin_msg_id`, `sender_id` to `saved_items`. |
| `20260726143924_create_panel_settings_table.sql` | 2026-07-26 | Created `panel_settings` with 5 initial columns. |
| `20260729213959_create_bot_settings_table.sql` | 2026-07-29 | Created `bot_settings` key-value table (later superseded). |
| `20260730220000_panel_settings_column_model.sql` | 2026-07-30 | Migrated `bot_settings` into `panel_settings` as typed columns. Dropped `bot_settings`. |
| `20260730230000_panel_settings_full_13_columns.sql` | 2026-07-30 | Added remaining `panel_settings` columns. |
| `20260801215007_create_username_state_table.sql` | 2026-08-01 | Created `username_state` table. |
| `20260804145402_create_ai_tables.sql` | 2026-08-04 | Created AI tables: `ai_sessions`, `ai_messages`, `ai_memories`, `ai_tool_history`. |
| `20260805120000_create_ai_config_table.sql` | 2026-08-05 | Created `ai_config` table with provider, model, temperature, max_tokens, system_prompt, history_budget, is_configured. |
| `20260805130000_add_ai_trigger_columns.sql` | 2026-08-05 | Added `trigger_en` and `trigger_fa` columns to `ai_config` for trigger-based AI activation. |

### Missing migrations (AI tables)

The following AI tables are referenced by the code but do **not** have
migrations applied to the live database. The AI subsystem currently
operates entirely in-memory. When migrations are added, they should
create the tables exactly as described in §7–§10 above:

1. `ai_sessions` — session metadata
2. `ai_messages` — individual messages
3. `ai_memories` — three-tier memory
4. `ai_tool_history` — tool call log
5. `ai_provider_stats` — per-provider aggregate statistics
6. `ai_config` — per-owner AI configuration (provider, model, triggers)

The `ai_config` table is created by migration
`20260805120000_create_ai_config_table.sql` and later had trigger
columns added by `20260805130000_add_ai_trigger_columns.sql`.
Its complete schema:

| Column | SQL Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | `bigserial` | NO | `nextval(...)` | Primary key |
| `owner_id` | `bigint` | NO | `0` | Telegram user ID of the bot owner |
| `provider` | `text` | NO | `''` | Active provider name (e.g. `gemini`, `openai`, `dummy`) |
| `model` | `text` | NO | `''` | Active model name |
| `temperature` | `double precision` | NO | `1.0` | Sampling temperature [0.0, 2.0] |
| `max_tokens` | `integer` | NO | `4096` | Max output tokens |
| `system_prompt` | `text` | NO | `''` | Custom system prompt (empty = default) |
| `history_budget` | `integer` | NO | `4000` | Context budget in tokens |
| `is_configured` | `boolean` | NO | `false` | Whether the user completed setup |
| `trigger_en` | `text` | YES | `NULL` | English trigger word (case-insensitive matching) |
| `trigger_fa` | `text` | YES | `NULL` | Persian trigger word (exact matching) |
| `created_at` | `timestamptz` | YES | `now()` | When the config row was created |
| `updated_at` | `timestamptz` | YES | `now()` | Last update timestamp |

**Trigger validation rules (enforced in application code):**
- Both `trigger_en` and `trigger_fa` are optional individually.
- At least one must be non-empty before AI can be activated.
- The two values must not be identical (case-insensitive comparison).
- Triggers must be single words (no spaces).
- `trigger_en` matching is case-insensitive.
- `trigger_fa` matching is exact (no case folding).
- When a trigger matches, the trigger word is stripped from the
  message before being sent to the provider.

**RLS:** Enabled. Only SELECT is granted to `anon` + `authenticated`.
All writes go through the backend service-role key.

**Repository:** `backend/ai/config_store.py` — `get_config`,
`save_config`, `update_provider`, `update_model`, `update_setting`,
`record_request`, `is_configured`, `validate_triggers`,
`update_triggers`, `get_triggers`, `match_trigger`.

The `ai_provider_stats` table is defined in the repository interface
(`backend/ai/database/provider_stats_repository.py`) but not yet
referenced by `persistence.py`. Its expected schema:

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

The SQL scripts in [`sql/`](sql/) represent the **current consolidated
schema** for the 5 core tables — they are the authoritative CREATE TABLE
statements for a fresh database.
