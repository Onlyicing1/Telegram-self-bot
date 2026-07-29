# Database Architecture — LifeOS Telegram Self-Bot

> **Source of truth for the database schema.**
> This document describes every table, column, index, constraint, and RLS
> policy in the live Supabase project. It is verified against the actual
> database, not just the migration files.
>
> For SQL scripts, see the [`sql/`](sql/) directory.

---

## Table of Contents

1. [Overview](#1-overview)
2. [saved_items](#2-saved_items)
3. [bio_state](#3-bio_state)
4. [bot_logs](#4-bot_logs)
5. [panel_settings](#5-panel_settings)
6. [Relationships](#6-relationships)
7. [RLS Policy Model](#7-rls-policy-model)
8. [Panel Database](#8-panel-database)
9. [In-Memory Fallback](#9-in-memory-fallback)
10. [Migration History](#10-migration-history)

---

## 1. Overview

The database contains **four tables** in the `public` schema:

| Table | Purpose | PK | Rows |
|---|---|---|---|
| `saved_items` | Media save records (forward + deep) | `id` (bigserial) | One per save |
| `bio_state` | Bio cron engine state per owner | `id` (bigserial) | One per owner |
| `bot_logs` | Structured activity log | `id` (bigserial) | One per event |
| `panel_settings` | Global panel auto-close preference | `key` (text) | One row (`"global"`) |

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
| `saved_chat_id` | `bigint` | YES | — | Chat ID where saved (Saved Messages = `me`) |
| `saved_msg_id` | `bigint` | YES | — | Message ID in the saved location |
| `sender_name` | `text` | YES | — | Display name of the original sender |
| `sender_id` | `bigint` | YES | — | Telegram user ID of the sender |
| `mime_type` | `text` | YES | — | MIME type of the media |
| `file_id` | `text` | YES | — | Telegram internal file reference |
| `file_size` | `bigint` | YES | — | Size in bytes |
| `media_type` | `text` | YES | — | Classified type: Photo, Video, Audio, Voice, GIF, Sticker, Document, Unknown |
| `tags` | `text[]` | YES | `'{}'` | Array of hashtag strings |
| `caption` | `text` | YES | — | Generated caption (deep saves only) |
| `file_name` | `text` | YES | — | Original filename (added in save UX redesign migration) |
| `short_code` | `text` | YES | — | Compact code, format `SNNNN` (added in save UX redesign). Unique when non-null. |
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
| `saved_items_save_type_check` | Check | `CHECK (save_type IN ('forward', 'deep'))` |

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
  columns ordered by `created_at DESC`.
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
| `level` | `text` | NO | — | `'INFO'`, `'WARN'`, or `'ERROR'`. CHECK constraint enforced. |
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
| `bot_logs_level_check` | Check | `CHECK (level IN ('INFO', 'WARN', 'ERROR'))` |

### RLS Policies

| Policy | Command | Roles | Condition |
|---|---|---|---|
| `anon_select_bot_logs` | SELECT | `anon, authenticated` | `USING (true)` |
| `anon_update_bot_logs` | UPDATE | `anon, authenticated` | `USING (true) WITH CHECK (true)` |

> **Note:** The `anon_update_bot_logs` policy is a legacy artifact from the
> initial migration. It is not part of the authoritative schema and can be
> safely dropped.

### Typical Usage

- **Insert** — `log(owner_id, level, message, context)` inserts a row.
  Called after `.save`, `.send`, `.db clean`, `.db vacuum`, and other actions.
- **Count** — `count_logs(owner_id)` for `.organize list`.
- **List** — `list_logs(owner_id, limit)` for the dashboard `/api/logs`.
- **Clean** — `clean_logs(owner_id, days=7)` deletes rows older than 7 days.

---

## 5. panel_settings

Global panel preferences. Currently stores only the auto-close toggle.

### Columns

| Column | SQL Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `key` | `text` | NO | — | Primary key. Always `"global"`. |
| `auto_close_enabled` | `boolean` | NO | `true` | Whether panels auto-close after 120 seconds |
| `updated_at` | `timestamptz` | YES | `now()` | Last update timestamp |

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

### RLS Policies

| Policy | Command | Roles | Condition |
|---|---|---|---|
| `anon_select_panel_settings` | SELECT | `anon, authenticated` | `USING (true)` |

### Typical Usage

- **Load** — `panel_settings.load()` reads the `auto_close_enabled` value
  for key `"global"` at startup. Caches in memory.
- **Toggle** — `panel_settings.toggle_auto_close()` flips the value and
  upserts the row. The timer engine reads the in-memory cache on every
  panel init and every tick.
- **Upsert** — `panel_settings.set_auto_close_enabled(enabled)` upserts
  `{"key": "global", "auto_close_enabled": enabled, "updated_at": now()}`.

---

## 6. Relationships

There are **no foreign keys** between any tables. All four tables share a
common `owner_id` column (except `panel_settings` which uses `key`), which
represents the Telegram user ID of the bot owner. This is an application-level
logical relationship, not a database constraint.

```
saved_items.owner_id  ───┐
bio_state.owner_id    ───┼──→  (soft link to owner, no FK)
bot_logs.owner_id     ───┘

panel_settings.key    ───→  "global" (singleton, no owner link)
```

### Cardinality

- `saved_items` : `owner_id` — Many-to-one (many saves per owner)
- `bio_state` : `owner_id` — One-to-one (enforced by UNIQUE constraint)
- `bot_logs` : `owner_id` — Many-to-one (many logs per owner)
- `panel_settings` : `key` — One row total (singleton)

---

## 7. RLS Policy Model

All four tables have RLS enabled. The model is:

- **SELECT** is granted to `anon` + `authenticated` (read-only dashboard access).
  All SELECT policies use `USING (true)` — any client with the anon key can
  read all rows.
- **No INSERT, UPDATE, or DELETE policies** for anon/authenticated (except the
  legacy `anon_update_bot_logs` artifact). All writes go through the backend's
  service-role key, which bypasses RLS entirely.

This means:
- The React dashboard (using the backend API) can read data.
- The backend can read and write freely (service-role key).
- No client can write directly to the database via the anon key.

---

## 8. Panel Database

### What It Stores

The `panel_settings` table stores global panel preferences. Currently, the
only preference is `auto_close_enabled` — whether inline panels auto-close
after 120 seconds of inactivity.

### How Sessions Are Stored

Panel sessions are **not stored in the database**. They live entirely in
memory (`backend/helper/session_manager.py`). Each session is keyed by
`(chat_id, msg_id)` and contains:

- `session_id` — internal ID for logging
- `chat_id`, `msg_id` — Telegram coordinates of the inline message
- `panel_type` — current panel (e.g. `"help"`, `"settings"`)
- `nav_stack` — list of `(panel_id, extra)` tuples representing the
  navigation history. Root is always stack length 1.
- `inline_message_id` — secondary lookup key for inline messages

Sessions are cleared on close, on shutdown, and during recovery. They do not
persist across restarts.

### How Panel Settings Are Stored

The `panel_settings` table has a single row with `key = "global"`. The
`auto_close_enabled` boolean controls whether the timer engine creates
auto-close tasks for new panels. The value is loaded once at startup
(`panel_settings.load()`) and cached in memory. Toggles are persisted
immediately via upsert.

### How Navigation Is Persisted

Navigation is **not persisted**. The nav stack lives only in the in-memory
session dict. When the process restarts, all panel sessions and their
navigation state are lost. Inline messages from before the restart become
non-interactive (buttons stop working) because no session exists to handle
the callbacks.

---

## 9. In-Memory Fallback

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
the in-memory cache in `panel_settings.py` serves as the fallback
(defaulting to `auto_close_enabled = True`).

All public functions in `db/client.py` wrap their Supabase calls in
`try/except`. On any error, they log a warning and use the fallback.
The bot never crashes due to a database error.

---

## 10. Migration History

| File | Date | Description |
|---|---|---|
| `20260712234229_lifeos_schema.sql` | 2026-07-12 | Initial schema. Created tables with CHECK constraints and wide-open CRUD RLS policies. |
| `20260714111706_create_lifeos_tables.sql` | 2026-07-14 | Authoritative schema. Recreated RLS as SELECT-only. Added indexes. Lacks CHECK constraints (but they persist from the initial migration when both run). |
| `20260718143752_20260718_save_ux_redesign.sql.sql` | 2026-07-18 | Added `file_name` and `short_code` columns to `saved_items`. Added trigram indexes for full-text search. Enabled `pg_trgm` extension. |
| `20260726143924_create_panel_settings_table.sql` | 2026-07-26 | Created `panel_settings` table for global auto-close preference. |

The SQL scripts in [`sql/`](sql/) represent the **current consolidated
schema** — the result of applying all four migrations in sequence. They are
authoritative for a fresh database setup.
