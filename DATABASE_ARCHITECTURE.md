# Database Architecture — LifeOS Telegram Self-Bot

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
15. [Relationships](#15-relationships)
16. [RLS Policy Model](#16-rls-policy-model)
17. [Panel Database](#17-panel-database)
18. [In-Memory Fallback](#18-in-memory-fallback)
19. [Known Inconsistencies](#19-known-inconsistencies)
20. [Migration Status](#20-migration-status)
21. [Migration Generation Rules](#21-migration-generation-rules)

---

## 1. Overview

The database contains **13 tables** in the `public` schema — 5 core
LifeOS tables and 8 AI subsystem tables.

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

### Access Model

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
| `save_code` | `text` | NO | — | Compact code, format `SV-NNNNNN` (e.g. `SV-000001`). Sequential with collision detection. Unique. |
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
validators. See [§17 Panel Database](#17-panel-database) for details.

### Migration Status

The initial migration (`20260726143924`) created only 3 columns
(`key`, `auto_close_enabled`, `updated_at`). A later migration
(`20260730210551`) added `update_stale_seconds`. The remaining 10
columns were supposed to be added by migrations
`20260730220000_panel_settings_column_model.sql` and
`20260730230000_panel_settings_full_13_columns.sql`, but **these
migration files do not exist** in the repository. A future migration
must add all missing columns. See [§20](#20-migration-status) and
[§19](#19-known-inconsistencies).

### Removed / phantom columns

The following columns were referenced in older documentation or the
`bot_settings` transition table but are **not** in the canonical schema:

- ~~`panel_auto_close_seconds`~~ — superseded by `auto_close_delay`
- ~~`log_cleanup_days`~~ — superseded by `log_retention_days`
- ~~`diagnostics_enabled`~~ — never implemented in `settings_service.py`
- ~~`bot_settings` table~~ — key-value transition table created by
  migration `20260729213959` and intended to be migrated into
  `panel_settings` columns. The migration that would have performed the
  migration and dropped `bot_settings` does not exist. A future
  migration must drop `bot_settings` if it exists.

---

## 7. ai_config

Per-owner AI configuration. **One row per owner** — enforced by a
UNIQUE constraint on `owner_id`. This is the canonical storage for
provider selection, model selection, trigger words, and tuning
parameters.

> **Current status:** The `ai_config` table is referenced by
> `backend/ai/config_store.py` but may not exist in the live database.
> The initial migration (`20260805075707`) creates the base columns but
> does NOT include `trigger_en` or `trigger_fa`. No migration file
> exists that adds these columns. The runtime silently falls back to
> in-memory storage when the table or columns are missing. See
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
`InMemoryProviderStatsRepository`). Methods: `get_or_create`,
`record_request`, `get`, `list_all`. No Supabase-backed implementation
is wired here yet. No migration has been applied for this table.

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
`InMemoryUsageRepository`). Methods: `create`, `total_tokens`,
`daily_tokens`, `recent`. No Supabase-backed implementation is wired
here yet. No migration has been applied for this table.

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

## 15. Relationships

There are **no enforced foreign keys** between any tables. Each table is
independent. The following logical relationships exist (not FK
constraints):

- `ai_messages.session_id` → `ai_sessions.session_id` (logical)
- `ai_tool_history.session_id` → `ai_sessions.session_id` (logical)
- `ai_usage.session_id` → `ai_sessions.session_id` (logical)
- `owner_id` (on `saved_items`, `bio_state`, `username_state`,
  `bot_logs`, `ai_config`, all AI tables) links rows to the bot owner
  but is not a foreign key.

Future migrations MAY add enforced FK constraints for these logical
relationships, but it is not required for correctness.

---

## 16. RLS Policy Model

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

Tables that do not yet have RLS enabled (`ai_provider_stats`,
`ai_usage`, `ai_preferences`) MUST have RLS enabled and a SELECT policy
added in their creation migration.

---

## 17. Panel Database

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

### Settings (12 typed columns on panel_settings)

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

### In-Memory Fallback

If the DB is unavailable, the service uses hardcoded `_DEFAULTS` for
all 11 settings. The bot continues to function normally — all panel
operations work with default values. Every Supabase call that fails
logs a warning and falls back silently.

---

## 18. In-Memory Fallback

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

## 19. Known Inconsistencies

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

**Resolution [MIGRATION REQUIRED]:** A future migration must add
`trigger_en TEXT DEFAULT NULL` and `trigger_fa TEXT DEFAULT NULL` to
the `ai_config` table.

### 19.2 `ai_config` — `last_request_at` / `last_latency_ms` never persisted

**Severity:** Medium

**Problem:** `record_request()` in `config_store.py` sets
`config["last_request_at"]` and `config["last_latency_ms"]` in the
in-memory dict, but `_save_config_sync()` builds its DB payload
**without these two fields**. The columns exist in the migration
(`20260805075707`) but are never written.

**Impact:** The status panel always shows "Never" for last request
time and "—" for latency, even after successful AI calls.

**Resolution [CODE REQUIRED]:** `config_store.py` must include
`last_request_at` and `last_latency_ms` in the `_save_config_sync`
payload. This is a code fix, not a migration.

### 19.3 `panel_settings` — 10 columns missing from migrations

**Severity:** High

**Problem:** `settings_service.py` reads and writes 13 typed columns
on `panel_settings`. The applied migrations only create 4 columns:
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

**Resolution [MIGRATION REQUIRED]:** A future migration must add all
10 missing columns to `panel_settings` with the types and defaults
listed in [§6](#6-panel_settings).

### 19.4 `bot_settings` table — orphaned transition table

**Severity:** Low

**Problem:** Migration `20260729213959` created a `bot_settings`
key-value table as a transition step. A follow-up migration was
supposed to migrate its data into `panel_settings` columns and drop
it, but that migration was never created. The `bot_settings` table
may still exist in the live database with stale data.

**Resolution [MIGRATION REQUIRED]:** A future migration must drop
`bot_settings` if it exists. No code references this table.

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

**Resolution [MIGRATION REQUIRED]:** Future migrations must create
all three tables with the schemas defined in [§12](#12-ai_provider_stats),
[§13](#13-ai_usage), and [§14](#14-ai_preferences).

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

### 19.11 `ai_database/manager.py` — no Supabase implementations wired

**Severity:** Medium

**Problem:** `backend/ai/database/manager.py` instantiates only
in-memory repository implementations. The code comment says Supabase
implementations "will be injected later." Meanwhile, `persistence.py`
handles Supabase access for `ai_sessions`, `ai_messages`, `ai_memories`,
and `ai_tool_history` directly — bypassing the repository pattern.

**Resolution [CODE REQUIRED]:** This is an architectural decision, not
a migration issue. Either wire Supabase implementations into the
repository manager, or remove the repository abstraction. This document
documents both the `persistence.py` path and the repository interfaces.

---

## 20. Migration Status

### Applied Migration Files (in `supabase/migrations/`)

| # | File | Creates / Alters | Status |
|---|---|---|---|
| 1 | `20260712234229_lifeos_schema.sql` | `saved_items`, `bio_state`, `bot_logs` (initial) | Superseded by #2 |
| 2 | `20260714111706_create_lifeos_tables.sql` | `saved_items`, `bio_state`, `bot_logs` (authoritative) | Applied |
| 3 | `20260718143752_...save_ux_redesign.sql` | Added `short_code`, `file_name` to `saved_items`; trigram indexes | Applied (dead columns — see §19.7) |
| 4 | `20260726143924_create_panel_settings_table.sql` | `panel_settings` (3 columns: `key`, `auto_close_enabled`, `updated_at`) | Applied (incomplete — see §19.3) |
| 5 | `20260729213959_...create_bot_settings_table.sql` | `bot_settings` key-value table | Applied (orphan — see §19.4) |
| 6 | `20260730210551_...add_update_stale_seconds.sql` | Added `update_stale_seconds` to `panel_settings` | Applied |
| 7 | `20260801215007_create_username_state_table.sql` | `username_state` | Applied |
| 8 | `20260804145402_create_ai_tables.sql` | `ai_sessions`, `ai_messages`, `ai_memories`, `ai_tool_history` | Applied |
| 9 | `20260805075707_...create_ai_config_table.sql` | `ai_config` (base columns, no triggers) | Applied (incomplete — see §19.1) |

### Missing Migration Files (referenced in prior docs but never created)

| File | Purpose | Blocks |
|---|---|---|
| `20260730220000_panel_settings_column_model.sql` | Migrate `bot_settings` into `panel_settings` typed columns | §19.3, §19.4 |
| `20260730230000_panel_settings_full_13_columns.sql` | Add remaining 10 `panel_settings` columns | §19.3 |
| `20260805130000_add_ai_trigger_columns.sql` | Add `trigger_en` / `trigger_fa` to `ai_config` | §19.1 |

### Migrations That Must Be Generated From This Document

The following migrations do not exist yet and must be created to bring
the live database in sync with this specification:

1. **Add missing `panel_settings` columns** — add all 10 missing
   columns with types and defaults from [§6](#6-panel_settings). Drop
   `bot_settings` if it exists.

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
