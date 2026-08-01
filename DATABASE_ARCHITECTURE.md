# Database Architecture — LifeOS Telegram Self-Bot

> **Source of truth for the database schema.**
This document describes every table, column, index, constraint, and RLS
policy in the live Supabase project. It is verified against the actual
database, not just the migration files.
>
> For SQL scripts, see the [`sql/`](sql/) directory.

---

## Table of Contents

1. [Overview](#1-overview)
2. [saved_items](#2-saved_items)
3. [bio_state](#3-bio_state)
4. [username_state](#4-username_state)
5. [bot_logs](#5-bot_logs)
6. [panel_settings](#6-panel_settings)
7. [Relationships](#7-relationships)
8. [RLS Policy Model](#8-rls-policy-model)
9. [Panel Database](#9-panel-database)
10. [In-Memory Fallback](#10-in-memory-fallback)
11. [Migration History](#11-migration-history)

---

## 1. Overview

The database contains **five tables** in the `public` schema:

| Table | Purpose | PK | Rows |
|---|---|---|---|
| `saved_items` | Media save records (forward + deep) | `id` (bigserial) | One per save |
| `bio_state` | Bio cron engine state per owner | `id` (bigserial) | One per owner |
| `username_state` | Username cron engine state per owner | `id` (bigserial) | One per owner |
| `bot_logs` | Structured activity log | `id` (bigserial) | One per event |
| `panel_settings` | Glass Panel configuration (column-per-setting) | `key` (text) | One row (`"global"`) |

All access goes through the Supabase PostgREST API via the `supabase-py` client.
The backend uses the **service-role key**, which bypasses RLS. The frontend
reads via the backend API — it never touches Supabase directly.

---

## 2. saved_items

Stores metadata for every media save operation (forward save and deep save).

### Columns

| Column | SQL Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | `bigserial` | NO | `nextval(...)` | Primary key |
| `save_code` | `text` | NO | — | Legacy code, format `SV-NNNNNN`. Unique. |
| `save_type` | `text` | NO | — | `'forward'` or `'deep'`. CHECK constraint enforced. |
| `origin_chat_id` | `bigint` | YES | — | Telegram chat ID where the message originated |
| `origin_msg_id` | `bigint` | YES | — | Telegram message ID of the original message |
| `saved_chat_id` | `bigint` | YES | — | Telegram chat ID where the message was saved (Saved Messages = `"me"`) |
| `saved_msg_id` | `bigint` | YES | — | Telegram message ID of the saved message in Saved Messages |
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

---

## 3. bio_state

Singleton bio engine state per owner.

### Columns

| Column | SQL Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | `bigserial` | NO | `nextval(...)` | Primary key |
| `owner_id` | `bigint` | NO | `0` | Telegram user ID of the bot owner |
| `template` | `text` | NO | `'{time} | {mood} | {text}'` | Bio template with tokens |
| `mood` | `text` | NO | `'neutral'` | Current mood value |
| `custom_text` | `text` | NO | `'LifeOS'` | Custom text for `{text}` token |
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
| `template` | `text` | NO | `'{time} | {mood}'` | Username template with tokens |
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

---

## 6. panel_settings

Glass Panel configuration. Singleton row (key = `"global"`).

### Columns

| Column | SQL Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `key` | `text` | NO | — | Primary key, always `"global"` |
| `auto_close_enabled` | `boolean` | NO | `true` | Whether panels auto-close |
| `panel_auto_close_seconds` | `integer` | NO | `120` | Seconds before auto-close |
| `max_deep_save_mb` | `integer` | NO | `50` | Max file size for deep save |
| `delete_batch_size` | `integer` | NO | `100` | Batch size for message deletion |
| `log_cleanup_days` | `integer` | NO | `7` | Days to retain logs |
| `auto_close_delay` | `integer` | NO | `120` | Auto-close delay in seconds |
| `panel_timeout_seconds` | `integer` | NO | `300` | Panel timeout in seconds |
| `allow_multiple_panels` | `boolean` | NO | `false` | Allow multiple simultaneous panels |
| `reuse_existing_panel` | `boolean` | NO | `true` | Reuse an existing panel instead of creating new |
| `language` | `text` | NO | `'en'` | Language code |
| `diagnostics_enabled` | `boolean` | NO | `true` | Show diagnostics info (`.kill`, `.logs`) |
| `debug_callbacks` | `boolean` | NO | `false` | Debug callback tracing |
| `owner_only` | `boolean` | NO | `true` | Restrict commands to owner only |
| `updated_at` | `timestamptz` | DEFAULT | `now()` | Last update timestamp |

### Indexes

| Index | Columns | Type |
|---|---|---|
| `panel_settings_pkey` | `key` | btree (PK) |

### RLS

RLS is enabled. Only SELECT is granted to `anon` + `authenticated`.

---

## 7. Relationships

There are no foreign key relationships between tables. Each table is
independent. The `owner_id` column (present on `saved_items`, `bio_state`,
`username_state`, and `bot_logs`) links rows to the bot owner but is not
a foreign key.

---

## 8. RLS Policy Model

All five tables have RLS enabled. Only SELECT policies are granted to
`anon` + `authenticated` (read-only dashboard access). All writes
(INSERT/UPDATE/DELETE) go through the backend's service-role key, which
bypasses RLS entirely. There are no anon/authenticated write policies.

---

## 9. Panel Database

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

| Column                  | Type    | Default | Range/Constraint        |
|-------------------------|---------|---------|-------------------------|
| auto_close_enabled      | bool    | true    | must be boolean         |
| auto_close_delay        | int     | 120     | 5..3600 (seconds)       |
| max_deep_save_mb        | int     | 50      | 1..500 (MB)             |
| delete_batch_size       | int     | 100     | 1..1000                 |
| log_retention_days      | int     | 7       | 1..365 (days)           |
| panel_timeout_seconds   | int     | 300     | 30..86400 (seconds)     |
| allow_multiple_panels   | bool    | false   | must be boolean         |
| reuse_existing_panel    | bool    | true    | must be boolean         |
| language                | str     | "en"    | non-empty string        |
| diagnostics_enabled     | bool    | true    | must be boolean         |
| debug_callbacks        | bool    | false   | must be boolean         |
| owner_only              | bool    | true    | must be boolean         |

### Accessors

The service exposes 12 typed getters + 12 typed setters:

  `.is_auto_close_enabled()`, `.auto_close_delay()`, `.max_deep_save_mb()`,
  `.delete_batch_size()`, `.log_retention_days()`, `.panel_timeout_seconds()`,
  `.is_allow_multiple_panels()`, `.is_reuse_existing_panel()`,
  `.language()`, `.is_diagnostics_enabled()`,
  `.is_debug_callbacks()`, `.is_owner_only()`. These read from the
  cache — no DB round-trip.

### Validators

Each setting has its own validator enforcing type and range constraints:

| Setting | Validator |
|---|---|
| auto_close_enabled | must be boolean |
| auto_close_delay | int 5..3600 |
| max_deep_save_mb | int 1..500 |
| delete_batch_size | int 1..1000 |
| log_retention_days | int 1..365 |
| panel_timeout_seconds | int 30..86400 |
| allow_multiple_panels | must be boolean |
| reuse_existing_panel | must be boolean |
| language | non-empty string |
| diagnostics_enabled | must be boolean |
| debug_callbacks | must be boolean |
| owner_only | must be boolean |

### In-Memory Fallback

If the DB is unavailable, the service uses hardcoded `_DEFAULTS` for all
12 settings. The bot continues to function normally — all panel operations
work with default values. Every Supabase call that fails logs a warning
and falls back silently.

---

## 10. In-Memory Fallback

The bot is designed to run **with or without Supabase**. When the DB is
unavailable, all operations use in-memory fallbacks:

- `saved_items` → in-memory list
- `bio_state` → in-memory dict
- `username_state` → in-memory dict
- `bot_logs` → in-memory list
- `panel_settings` → hardcoded `_DEFAULTS` dict

The bot never crashes due to a database error.

---

## 11. Migration History

| Migration File | Date | Description |
|---|---|---|
| `20260712234229_lifeos_schema.sql` | 2026-07-12 | Initial schema: `saved_items`, `bio_state`, `bot_logs`. |
| `20260714111706_create_lifeos_tables.sql` | 2026-07-14 | Authoritative schema: `saved_items`, `bio_state`, `bot_logs` with final column definitions. |
| `20260718143752_20260718_save_ux_redesign.sql.sql` | 2026-07-18 | Added `short_code`, `origin_chat_id`, `origin_msg_id`, `sender_id` columns to `saved_items`. |
| `20260726143924_create_panel_settings_table.sql` | 2026-07-26 | Created `panel_settings` table with 5 initial columns. |
| `20260729213959_20260729120000_create_bot_settings_table.sql` | 2026-07-29 | Created `bot_settings` key-value table (later superseded). |
| `20260730220000_panel_settings_column_model.sql` | 2026-07-30 | Migrated `bot_settings` data into `panel_settings` as typed columns. Added `auto_close_delay`, `max_deep_save_mb`, `delete_batch_size`, `log_retention_days`. Dropped `bot_settings`. |
| `20260730230000_panel_settings_full_13_columns.sql` | 2026-07-30 | Added `panel_timeout_seconds`, `allow_multiple_panels`, `reuse_existing_panel`, `language`, `diagnostics_enabled`, `debug_callbacks`, `owner_only` columns. Introduced PanelSettingsRepository layer. |
| `20260801215007_create_username_state_table.sql` | 2026-08-01 | Created `username_state` table for the Username Engine. Mirrors `bio_state` structure but controls `first_name`. Independent persistence — Bio Engine data untouched. |

The SQL scripts in [`sql/`](sql/) represent the **current consolidated
schema** — they are the authoritative CREATE TABLE statements for a fresh
database. The migration files in `supabase/migrations/` are the historical
record of all changes applied to the live database. To recreate the database
from scratch, run all migrations in sequence. They are
authoritative for a fresh database setup.
