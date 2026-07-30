# Database Architecture — LifeOS Telegram Self-Bot

> **Source of truth for the database schema.**
> This document describes every table, column, index, constraint, and RLS
> policy in the live Supabase project. It is verified against the actual
> database, not just the migration files.

---

## Table of Contents

1. [Overview](#1-overview)
2. [saved_items](#2-saved_items)
3. [bio_state](#3-bio_state)
4. [bot_logs](#4-bot_logs)
5. [panel_settings](#5-panel_settings)
6. [bot_settings (Legacy)](#6-bot_settings-legacy)
7. [Relationships](#7-relationships)
8. [RLS Policy Model](#8-rls-policy-model)
9. [Settings Architecture](#9-settings-architecture)
10. [In-Memory Fallback](#10-in-memory-fallback)
11. [Migration History](#11-migration-history)

---

## 1. Overview

The database contains **five tables** in the `public` schema (four active, one legacy):

| Table | Status | Purpose | PK | Rows |
|---|---|---|---|---|
| `saved_items` | Active | Media save records (forward + deep) | `id` (bigserial) | One per save |
| `bio_state` | Active | Bio cron engine state per owner | `id` (bigserial) | One per owner |
| `bot_logs` | Active | Structured activity log | `id` (bigserial) | One per event |
| `panel_settings` | Active | Global configuration (typed columns) | `key` (text) | One row (`"global"`) |
| `bot_settings` | Legacy | Key-value settings (superseded by `panel_settings`) | `key` (text) | One per setting key |

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
| `save_type` | `text` | NO | `'forward'` | `'forward'` or `'deep'`. CHECK constraint exists in initial migration only. |
| `origin_chat_id` | `bigint` | YES | — | Telegram chat ID where the message originated |
| `origin_msg_id` | `bigint` | YES | — | Telegram message ID of the original message |
| `saved_chat_id` | `bigint` | YES | — | Chat ID where saved (Saved Messages = `me`) |
| `saved_msg_id` | `bigint` | YES | — | Message ID in the saved location |
| `sender_name` | `text` | YES | — | Display name of the original sender |
| `sender_id` | `bigint` | YES | — | Telegram user ID of the sender |
| `mime_type` | `text` | YES | — | MIME type of the media |
| `file_id` | `text` | YES | — | Telegram internal file reference |
| `file_size` | `bigint` | YES | — | Size in bytes |
| `media_type` | `text` | YES | — | Classified type: Photo, Video, Animation, Audio, Voice, Sticker, Document, Unknown |
| `file_name` | `text` | YES | — | Original filename (added in save UX redesign migration) |
| `short_code` | `text` | YES | — | Compact code, format `SNNNN` (added in save UX redesign). Unique when non-null. |
| `tags` | `text[]` | YES | `'{}'` | Array of hashtag strings |
| `caption` | `text` | YES | — | Generated caption (deep saves only) |
| `owner_id` | `bigint` | NO | — | Telegram user ID of the bot owner |
| `created_at` | `timestamptz` | YES | `now()` | Timestamp of the save |

### Primary Key

- `id` (bigserial) — auto-incrementing primary key.

### Indexes

| Index Name | Type | Columns | Notes |
|---|---|---|---|
| `saved_items_pkey` | btree (unique) | `id` | PK index |
| `saved_items_save_code_key` | btree (unique) | `save_code` | UNIQUE constraint |
| `idx_saved_items_short_code` | btree (unique, partial) | `short_code WHERE short_code IS NOT NULL` | Compact code lookup |
| `idx_saved_items_owner` | btree | `owner_id` | Per-owner queries |
| `idx_saved_items_save_code` | btree | `save_code` | Legacy code lookup |
| `idx_saved_items_created` | btree | `created_at DESC` | Recent-first ordering |
| `idx_saved_items_created_at` | btree | `created_at DESC` | Duplicate of above (from two migrations) |
| `idx_saved_items_owner_created` | btree | `owner_id, created_at DESC` | Composite: per-owner recent list |
| `idx_saved_items_caption_trgm` | gin | `caption gin_trgm_ops` | Full-text ILIKE search |
| `idx_saved_items_file_name_trgm` | gin | `file_name gin_trgm_ops` | Full-text ILIKE search |
| `idx_saved_items_save_code_trgm` | gin | `save_code gin_trgm_ops` | Full-text ILIKE search |
| `idx_saved_items_short_code_trgm` | gin | `short_code gin_trgm_ops` | Full-text ILIKE search |
| `idx_saved_items_mime_trgm` | gin | `mime_type gin_trgm_ops` | Full-text ILIKE search |

### Constraints

| Constraint | Type | Definition |
|---|---|---|
| `saved_items_pkey` | Primary key | `PRIMARY KEY (id)` |
| `saved_items_save_code_key` | Unique | `UNIQUE (save_code)` |
| `saved_items_save_type_check` | Check | `CHECK (save_type IN ('forward', 'deep'))` — exists only in initial migration |

### RLS Policies

| Policy | Command | Roles | Condition |
|---|---|---|---|
| `anon_select_saved_items` | SELECT | `anon, authenticated` | `USING (true)` |

No INSERT, UPDATE, or DELETE policies for anon/authenticated. All writes go
through the service-role key.

### Typical Usage

- **Insert** — `.save f` / `.save d` calls `insert_save()`.
- **Query by code** — `.preview <code>` / `.send <code>` calls `query_save()`,
  which looks up by `short_code` OR `save_code` (case-insensitive).
- **List recent** — `.list` calls `list_recent_saves()`, selects a subset of
  columns ordered by `created_at` desc.
- **Search** — `.find <query>` calls `search_saves()`, uses trigram indexes
  for ILIKE matching across `caption`, `file_name`, `save_code`,
  `short_code`, `mime_type`.
- **Count** — `.organize list` calls `count_saves()` (optionally filtered by
  `save_type`).
- **Delete** — `.del <code>` calls `delete_save()` / `delete_save_row()`.
- **Cleanup** — `.db clean` / `.db vacuum` calls `cleanup_orphans()` to
  remove rows whose Telegram messages no longer exist.

---

## 3. bio_state

Singleton-per-owner state for the bio cron engine.

### Columns

| Column | SQL Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | `bigserial` | NO | `nextval(...)` | Primary key |
| `owner_id` | `bigint` | NO | — | Telegram user ID. Unique. |
| `template` | `text` | NO | `'🕒 {time} \| 💭 {mood}'` | Bio template with tokens |
| `mood` | `text` | NO | `'😊'` | Current mood emoji |
| `custom_text` | `text` | NO | `''` | Freeform text token value |
| `is_active` | `boolean` | NO | `false` | Whether the bio cron is running |
| `last_bio` | `text` | NO | `''` | Last rendered bio string (for deduplication) |
| `updated_at` | `timestamptz` | YES | `now()` | Last update timestamp |

### Primary Key

- `id` (bigserial)

### Indexes

| Index Name | Type | Columns | Notes |
|---|---|---|---|
| `bio_state_pkey` | btree (unique) | `id` | PK index |
| `bio_state_owner_id_key` | btree (unique) | `owner_id` | UNIQUE constraint |
| `idx_bio_state_owner` | btree | `owner_id` | Redundant with unique constraint, kept for explicitness |

### Constraints

| Constraint | Type | Definition |
|---|---|---|
| `bio_state_pkey` | Primary key | `PRIMARY KEY (id)` |
| `bio_state_owner_id_key` | Unique | `UNIQUE (owner_id)` |

### RLS Policies

| Policy | Command | Roles | Condition |
|---|---|---|---|
| `anon_select_bio_state` | SELECT | `anon, authenticated` | `USING (true)` |

### Triggers

None. The `updated_at` column has a default of `now()` but no auto-update
trigger. Some code paths include `updated_at` manually in update calls;
others do not.

### Typical Usage

- **Get** — `get_bio_state(owner_id)` selects by `owner_id`, uses `.maybe_single()`.
- **Get or create** — `get_or_create_bio_state(owner_id)` selects; if not found,
  inserts a default row.
- **Update** — `update_bio_state(owner_id, updates)` updates by `owner_id`.
  Called by `.bio on/off/template/text/mood` and by the bio cron loop
  (which updates `last_bio` and `updated_at`).
- **Startup resume** — `main.py` Phase 4 reads `is_active` to decide whether
  to auto-start the cron.

---

## 4. bot_logs

Structured activity log for bot events.

### Columns

| Column | SQL Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | `bigserial` | NO | `nextval(...)` | Primary key |
| `owner_id` | `bigint` | NO | — | Telegram user ID of the bot owner |
| `level` | `text` | NO | `'INFO'` | `'INFO'`, `'WARN'`, or `'ERROR'`. CHECK constraint exists in initial migration only. |
| `message` | `text` | NO | — | Log message text |
| `context` | `jsonb` | YES | `'{}'` | Structured context data |
| `created_at` | `timestamptz` | YES | `now()` | Timestamp of the event |

### Primary Key

- `id` (bigserial)

### Indexes

| Index Name | Type | Columns | Notes |
|---|---|---|---|
| `bot_logs_pkey` | btree (unique) | `id` | PK index |
| `idx_bot_logs_owner` | btree | `owner_id` | Per-owner queries |
| `idx_bot_logs_created` | btree | `created_at DESC` | Recent-first ordering |
| `idx_bot_logs_created_at` | btree | `created_at DESC` | Duplicate (from two migrations) |

### Constraints

| Constraint | Type | Definition |
|---|---|---|
| `bot_logs_pkey` | Primary key | `PRIMARY KEY (id)` |
| `bot_logs_level_check` | Check | `CHECK (level IN ('INFO', 'WARN', 'ERROR'))` — exists only in initial migration |

### RLS Policies

| Policy | Command | Roles | Condition |
|---|---|---|---|
| `anon_select_bot_logs` | SELECT | `anon, authenticated` | `USING (true)` |

> **Note:** The initial migration also created INSERT, UPDATE, and DELETE
> policies for anon/authenticated, but the authoritative migration dropped
> them. Only SELECT remains.

### Typical Usage

- **Insert** — `log(owner_id, level, message, context)` inserts a row.
  Called after `.save`, `.send`, `.db clean`, `.db vacuum`, and other actions.
- **Count** — `count_logs(owner_id)` for `.organize list`.
- **List** — `list_logs(owner_id, limit)` for the dashboard `/api/logs`.
- **Clean** — `clean_logs(owner_id, days)` deletes rows older than the
  configured retention period (default 7 days, configurable via `log_retention_days`).

---

## 5. panel_settings

Global configuration storage. Single row (`key='global'`). Uses typed columns
with CHECK constraints — not a key-value store. The database is the single
source of truth; an in-memory cache provides fast reads.

### Columns

| Column | SQL Type | Nullable | Default | Check Constraint | Notes |
|---|---|---|---|---|---|
| `key` | `text` | NO | — | — | Primary key. Always `"global"`. |
| `auto_close_enabled` | `boolean` | NO | `true` | — | Whether panels auto-close |
| `auto_close_delay_seconds` | `integer` | NO | `120` | 10–3600 | Seconds before auto-close |
| `max_deep_save_mb` | `integer` | NO | `50` | 1–500 | Deep save file size limit (MB) |
| `delete_batch_size` | `integer` | NO | `100` | 1–1000 | Messages per delete API call |
| `log_retention_days` | `integer` | NO | `7` | 1–365 | Log cleanup age (days) |
| `panel_countdown_interval` | `integer` | NO | `30` | 5–120 | Countdown re-render interval (seconds) |
| `input_timeout_seconds` | `integer` | NO | `120` | 10–600 | Pending input expiry (seconds) |
| `updated_at` | `timestamptz` | YES | `now()` | — | Last update timestamp |

### Primary Key

- `key` (text) — always `"global"` for the current single-owner bot.

### Indexes

| Index Name | Type | Columns | Notes |
|---|---|---|---|
| `panel_settings_pkey` | btree (unique) | `key` | PK index |

### Constraints

| Constraint | Type | Definition |
|---|---|---|
| `panel_settings_pkey` | Primary key | `PRIMARY KEY (key)` |
| `panel_settings_auto_close_delay_seconds_check` | Check | `auto_close_delay_seconds >= 10 AND <= 3600` |
| `panel_settings_max_deep_save_mb_check` | Check | `max_deep_save_mb >= 1 AND <= 500` |
| `panel_settings_delete_batch_size_check` | Check | `delete_batch_size >= 1 AND <= 1000` |
| `panel_settings_log_retention_days_check` | Check | `log_retention_days >= 1 AND <= 365` |
| `panel_settings_panel_countdown_interval_check` | Check | `panel_countdown_interval >= 5 AND <= 120` |
| `panel_settings_input_timeout_seconds_check` | Check | `input_timeout_seconds >= 10 AND <= 600` |

### RLS Policies

| Policy | Command | Roles | Condition |
|---|---|---|---|
| `anon_select_panel_settings` | SELECT | `anon, authenticated` | `USING (true)` |

### Triggers

None. The `settings_service` manually includes `updated_at` in every upsert.

### Typical Usage

- **Startup load** — `settings_service.load_all()` reads all columns for
  `key='global'` and caches them in memory.
- **Reads** — Feature modules read via typed accessors
  (e.g., `settings_service.max_deep_save_mb()`).
- **Writes** — `settings_service._update(updates)` upserts to the database,
  then refreshes the cache. Falls back to cache-only if DB unavailable.
- **Toggle** — `settings_service.toggle_auto_close()` flips `auto_close_enabled`.
- **Set** — Each setter validates the value against its range before writing.

---

## 6. bot_settings (Legacy)

Key-value settings store. Superseded by `panel_settings` typed columns.
The table is **not dropped** — it remains for backward compatibility but is
no longer the source of truth. The `settings_service` reads from
`panel_settings`, not `bot_settings`.

### Columns

| Column | SQL Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `key` | `text` | NO | — | Primary key |
| `value` | `text` | NO | — | Setting value stored as text |
| `value_type` | `text` | NO | `'str'` | Hint: `'bool'`, `'int'`, `'str'` |
| `updated_at` | `timestamptz` | YES | `now()` | Last update timestamp |

### Default Rows

| Key | Value | Type |
|---|---|---|
| `auto_close_enabled` | `true` | `bool` |
| `panel_auto_close_seconds` | `120` | `int` |
| `max_deep_save_mb` | `50` | `int` |
| `delete_batch_size` | `100` | `int` |
| `log_cleanup_days` | `7` | `int` |

### RLS Policies

| Policy | Command | Roles | Condition |
|---|---|---|---|
| `anon_select_bot_settings` | SELECT | `anon, authenticated` | `USING (true)` |

### Migration History

- Created by `20260729213959` as a replacement for `panel_settings`.
- Data from `panel_settings.auto_close_enabled` was migrated into `bot_settings`.
- Subsequently, `20260729221445` reversed the flow: it migrated data from
  `bot_settings` back into `panel_settings` typed columns.
- The table is not dropped for safety.

---

## 7. Relationships

There are **no foreign keys** between any tables. All tables share a
common `owner_id` column (except `panel_settings` and `bot_settings` which
use `key`), which represents the Telegram user ID of the bot owner. This is
an application-level logical relationship, not a database constraint.

```
saved_items.owner_id  ───┐
bio_state.owner_id    ───┼──→  (soft link to owner, no FK)
bot_logs.owner_id     ───┘

panel_settings.key    ───→  "global" (singleton, no owner link)
bot_settings.key      ───→  one row per setting key (legacy)
```

### Cardinality

- `saved_items` : `owner_id` — Many-to-one (many saves per owner)
- `bio_state` : `owner_id` — One-to-one (enforced by UNIQUE constraint)
- `bot_logs` : `owner_id` — Many-to-one (many logs per owner)
- `panel_settings` : `key` — One row total (singleton)
- `bot_settings` : `key` — One row per setting key (legacy)

---

## 8. RLS Policy Model

All five tables have RLS enabled. The model is:

- **SELECT** is granted to `anon` + `authenticated` (read-only dashboard access).
  All SELECT policies use `USING (true)` — any client with the anon key can
  read all rows.
- **No INSERT, UPDATE, or DELETE policies** for anon/authenticated. All writes
  go through the backend's service-role key, which bypasses RLS entirely.

This means:
- The React dashboard (using the backend API) can read data.
- The backend can read and write freely (service-role key).
- No client can write directly to the database via the anon key.

---

## 9. Settings Architecture

### Evolution

1. **Initial** — `panel_settings` created with only `auto_close_enabled`.
2. **Intermediate** — `bot_settings` key-value table created as a general-purpose
   settings store. Data migrated from `panel_settings` into `bot_settings`.
3. **Current** — `panel_settings` expanded with 6 typed columns + CHECK constraints.
   Data migrated from `bot_settings` back into `panel_settings`. `bot_settings`
   kept but no longer used.

### Current Architecture

```
panel_settings (DB, key='global')
    ↓
settings_service.load_all()  →  in-memory cache
    ↓
Typed accessors:
    settings_service.max_deep_save_mb()
    settings_service.auto_close_delay_seconds()
    settings_service.delete_batch_size()
    settings_service.log_retention_days()
    settings_service.panel_countdown_interval()
    settings_service.input_timeout_seconds()
    settings_service.is_auto_close_enabled()
```

- **Reads** go to the in-memory cache (fast, no DB round-trip).
- **Writes** go to the DB via upsert, then refresh the cache.
- **Validation** happens in every setter before writing.
- **Fallback** — if DB is unavailable, writes update the cache only.

### What `panel_settings.py` Does

The `backend/helper/panel_settings.py` module is a thin delegation layer.
All its functions (`load()`, `is_auto_close_enabled()`, `set_auto_close_enabled()`,
`toggle_auto_close()`) are one-liners that call `settings_service`.

---

## 10. In-Memory Fallback

If `SUPABASE_URL` or `SUPABASE_SERVICE_ROLE_KEY` is missing, or if the
Supabase client fails to initialise, the database layer silently degrades
to an in-memory Python dict:

```python
_fallback = {
    "saved_items": [],      # list of dicts
    "bio_state": {},        # dict keyed by owner_id
    "bot_logs": [],         # list of dicts
}
```

The `panel_settings` table does not have an explicit fallback entry —
the in-memory cache in `settings_service` serves as the fallback
(defaulting to all built-in defaults).

All public functions in `db/client.py` wrap their Supabase calls in
`try/except`. On any error, they log a warning and use the fallback.
The bot never crashes due to a database error.

---

## 11. Migration History

| # | File | Description |
|---|---|---|
| 1 | `20260712234229_lifeos_schema.sql` | Initial schema. Created `saved_items`, `bio_state`, `bot_logs` with `SERIAL` PKs, CHECK constraints, and wide-open CRUD RLS policies. |
| 2 | `20260714111706_create_lifeos_tables.sql` | Authoritative schema. Recreated tables with `bigserial` PKs. Dropped CRUD policies, replaced with SELECT-only RLS. Added indexes. Lacks CHECK constraints (but they persist from migration 1 when both run). |
| 3 | `20260718143752_20260718_save_ux_redesign.sql.sql` | Added `file_name` and `short_code` columns to `saved_items`. Added trigram GIN indexes for full-text search. Enabled `pg_trgm` extension. |
| 4 | `20260726143924_create_panel_settings_table.sql` | Created `panel_settings` table with `key`, `auto_close_enabled`, `updated_at`. SELECT-only RLS. |
| 5 | `20260729213959_20260729120000_create_bot_settings_table.sql` | Created `bot_settings` key-value table. Migrated `auto_close_enabled` from `panel_settings`. Inserted default settings. |
| 6 | `20260729221445_expand_panel_settings_typed_columns.sql` | Expanded `panel_settings` with 6 typed columns + CHECK constraints. Migrated values from `bot_settings` into typed columns. Ensured `key='global'` row exists. |

### Migration Order Matters

- If both migrations 1 and 2 run, CHECK constraints from migration 1 persist
  while RLS policies from migration 2 override.
- If only migration 2 is applied (skipping migration 1), CHECK constraints
  on `save_type` and `level` are missing.
- Migrations 4–6 show the settings architecture evolution: `panel_settings`
  → `bot_settings` → `panel_settings` (typed columns). The final state is
  `panel_settings` as the single source of truth with `bot_settings` retained
  but unused.
