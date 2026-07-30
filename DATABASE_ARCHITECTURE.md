# DATABASE_ARCHITECTURE.md — LifeOS Telegram Self-Bot

> **Complete database reference, generated from actual source code and migration inspection.**
> This document contains everything needed to recreate the entire Supabase project from scratch.

---

## Table of Contents

1. [High-Level Architecture](#1-high-level-architecture)
2. [Database Overview](#2-database-overview)
3. [Table: saved_items](#3-table-saved_items)
4. [Table: bio_state](#4-table-bio_state)
5. [Table: bot_logs](#5-table-bot_logs)
6. [Table: panel_settings](#6-table-panel_settings)
7. [Table: bot_settings (Legacy)](#7-table-bot_settings-legacy)
8. [Relationships](#8-relationships)
9. [Data Flow](#9-data-flow)
10. [Feature Mapping](#10-feature-mapping)
11. [Supabase Services](#11-supabase-services)
12. [Environment Variables](#12-environment-variables)
13. [Migration History](#13-migration-history)
14. [Missing Pieces](#14-missing-pieces)
15. [Risk Analysis](#15-risk-analysis)
16. [Manual Setup Guide](#16-manual-setup-guide)

---

## 1. High-Level Architecture

### Communication Model

The application communicates with Supabase exclusively through the **PostgREST REST API** (via the `supabase-py` client library, version `2.4.2`). There are no direct PostgreSQL connections, no Supabase CLI usage, and no `psql` calls.

```
┌──────────────────────────────────────────────────────────────┐
│                    Single Python Process                      │
│                      (asyncio event loop)                     │
│                                                              │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐  │
│  │ Telethon │   │ Bio Cron │   │ FastAPI  │   │ Settings │  │
│  │ Handlers │   │  Engine  │   │  Web API │   │ Service  │  │
│  └────┬─────┘   └────┬─────┘   └────┬─────┘   └────┬─────┘  │
│       │              │              │              │         │
│       ▼              ▼              ▼              ▼         │
│  ┌─────────────────────────────────────────────────────┐     │
│  │       backend/db/client.py                          │     │
│  │  (singleton Supabase client + in-memory fallback)  │     │
│  └──────────────────┬──────────────────────────────────┘     │
└─────────────────────┼────────────────────────────────────────┘
                      │ HTTPS (PostgREST REST API)
                      ▼
           ┌─────────────────────┐
           │   Supabase Project  │
           │  (PostgreSQL + RLS) │
           │                     │
           │  ┌───────────────┐  │
           │  │ saved_items   │  │
           │  │ bio_state     │  │
           │  │ bot_logs      │  │
           │  │ panel_settings│  │
           │  │ bot_settings  │  │
           │  └───────────────┘  │
           └─────────────────────┘
```

### Key Principles

1. **Service-role key only.** The backend authenticates with `SUPABASE_SERVICE_ROLE_KEY`, which bypasses all RLS policies. Every read and write goes through the service-role key.

2. **In-memory fallback.** If `SUPABASE_URL` or `SUPABASE_SERVICE_ROLE_KEY` is missing, or if the client fails to initialize, all operations silently degrade to a Python dict in memory. The bot continues to function with no persistence.

3. **Synchronous HTTP calls.** The `supabase-py` client uses `httpx` in synchronous mode. Every `.execute()` call blocks the asyncio event loop.

4. **No direct SQL.** All database access is via the Supabase query builder (`.table()`, `.select()`, `.insert()`, `.update()`, `.delete()`, `.eq()`, `.or_()`, `.lt()`, `.order()`, `.range()`, `.limit()`, `.maybe_single()`, `.upsert()`, `.in_()`).

5. **No Supabase Auth, Storage, Realtime, Edge Functions, or RPC.** Only PostgreSQL via PostgREST is used.

### Client Initialization

`backend/db/client.py` function `get_db()`:

- Singleton — initialized once on first access, cached in `_client`.
- If env vars are missing, logs a warning and returns `None`.
- If `create_client()` raises, logs a warning and returns `None`.
- `is_available()` returns whether the Supabase client is active.

### Database Warm-Up

`backend/main.py` Phase 1 calls `get_db()` and, if a client is returned, executes a probe query on `bot_logs`. Failure is non-fatal.

---

## 2. Database Overview

### Tables

The database contains **five tables** in the `public` schema (four active, one legacy):

| Table | Status | Purpose | Lifecycle |
|---|---|---|---|
| `saved_items` | Active | Metadata for every media save operation | Inserted on save, never auto-updated. Grows indefinitely. |
| `bio_state` | Active | Singleton-per-owner bio cron state | One row per owner. Created on first `.bio` command. Updated on every state change and cron tick. |
| `bot_logs` | Active | Structured activity log | Inserted on bot actions. Purged by `.organize clean`. |
| `panel_settings` | Active | Permanent configuration storage (typed columns) | Single row (`key='global'`). Created by migration. Updated on every settings change. |
| `bot_settings` | Legacy | Key-value settings store (superseded by `panel_settings`) | Created by migration. Data migrated to `panel_settings`. Table not dropped. |

---

## 3. Table: saved_items

**Purpose:** Stores metadata for both forward saves and deep saves. Each row represents one saved Telegram message with origin coordinates, saved location, media classification, tags, and optional caption.

### CREATE TABLE Statement (Authoritative — migration `20260714111706`)

```sql
CREATE TABLE IF NOT EXISTS saved_items (
    id bigserial PRIMARY KEY,
    save_code text UNIQUE NOT NULL,
    save_type text NOT NULL DEFAULT 'forward',
    origin_chat_id bigint,
    origin_msg_id bigint,
    saved_chat_id bigint,
    saved_msg_id bigint,
    sender_name text,
    sender_id bigint,
    mime_type text,
    file_id text,
    file_size bigint,
    media_type text,
    tags text[] DEFAULT '{}',
    caption text,
    owner_id bigint NOT NULL,
    created_at timestamptz DEFAULT now()
);
```

### ALTER Statements (migration `20260718143752`)

```sql
ALTER TABLE saved_items
    ADD COLUMN IF NOT EXISTS file_name text,
    ADD COLUMN IF NOT EXISTS short_code text;
```

### Complete Column Reference

| Column | SQL Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | `bigserial` | NO | `nextval()` | Primary key |
| `save_code` | `text` | NO | — | UNIQUE constraint. Legacy format `SV-NNNNNN`. Current format `SNNNN`. |
| `short_code` | `text` | YES | — | Added in UX redesign migration. UNIQUE partial index (WHERE NOT NULL). Current format `S` + 4 digits. |
| `save_type` | `text` | NO | `'forward'` | Values: `'forward'` or `'deep'`. CHECK constraint only in initial migration. |
| `origin_chat_id` | `bigint` | YES | — | Telegram chat ID where the message originated |
| `origin_msg_id` | `bigint` | YES | — | Telegram message ID of the original message |
| `saved_chat_id` | `bigint` | YES | — | Chat ID where the message was saved (Saved Messages) |
| `saved_msg_id` | `bigint` | YES | — | Message ID in the saved location |
| `sender_name` | `text` | YES | — | Display name of the original sender |
| `sender_id` | `bigint` | YES | — | Telegram user ID of the sender |
| `mime_type` | `text` | YES | — | MIME type of the media |
| `file_id` | `text` | YES | — | Telegram file ID reference |
| `file_size` | `bigint` | YES | — | Size in bytes |
| `media_type` | `text` | YES | — | Classified type: Photo, Video, Animation, Audio, Voice, Sticker, Document, Unknown |
| `file_name` | `text` | YES | — | Original filename of the saved media (added in UX redesign) |
| `tags` | `text[]` | YES | `'{}'` | Array of hashtag strings |
| `caption` | `text` | YES | — | Generated caption (deep saves only) |
| `owner_id` | `bigint` | NO | — | Telegram user ID of the bot owner |
| `created_at` | `timestamptz` | YES | `now()` | Timestamp of the save |

### Indexes

```sql
-- From authoritative migration
CREATE INDEX IF NOT EXISTS idx_saved_items_owner ON saved_items (owner_id);
CREATE INDEX IF NOT EXISTS idx_saved_items_save_code ON saved_items (save_code);
CREATE INDEX IF NOT EXISTS idx_saved_items_created_at ON saved_items (created_at DESC);

-- From initial migration (may already exist)
CREATE INDEX IF NOT EXISTS idx_saved_items_owner ON saved_items (owner_id);
CREATE INDEX IF NOT EXISTS idx_saved_items_created ON saved_items (created_at DESC);

-- From UX redesign migration
CREATE UNIQUE INDEX IF NOT EXISTS idx_saved_items_short_code
    ON saved_items (short_code) WHERE short_code IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_saved_items_owner_created
    ON saved_items (owner_id, created_at DESC);

-- Trigram indexes (requires pg_trgm extension)
CREATE INDEX IF NOT EXISTS idx_saved_items_caption_trgm
    ON saved_items USING gin (caption gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_saved_items_file_name_trgm
    ON saved_items USING gin (file_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_saved_items_save_code_trgm
    ON saved_items USING gin (save_code gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_saved_items_short_code_trgm
    ON saved_items USING gin (short_code gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_saved_items_mime_trgm
    ON saved_items USING gin (mime_type gin_trgm_ops);
```

### Constraints

- `save_code` — UNIQUE NOT NULL
- `short_code` — UNIQUE (partial index, WHERE NOT NULL)
- `owner_id` — NOT NULL
- **CHECK (save_type IN ('forward', 'deep'))** — present in initial migration only, missing from authoritative migration

### RLS Policies

**Authoritative migration (active):**

```sql
ALTER TABLE saved_items ENABLE ROW LEVEL SECURITY;

CREATE POLICY "anon_select_saved_items" ON saved_items FOR SELECT
    TO anon, authenticated USING (true);
-- No INSERT, UPDATE, or DELETE policies for anon/authenticated.
```

**Initial migration (superseded):**

```sql
CREATE POLICY "anon_insert_saved_items" ON saved_items FOR INSERT
    TO anon, authenticated WITH CHECK (true);
CREATE POLICY "anon_update_saved_items" ON saved_items FOR UPDATE
    TO anon, authenticated USING (true) WITH CHECK (true);
CREATE POLICY "anon_delete_saved_items" ON saved_items FOR DELETE
    TO anon, authenticated USING (true);
```

### Triggers

None.

### Migration Notes

- The initial migration (`20260712234229`) creates the table with `SERIAL` primary key and CHECK constraints on `save_type`.
- The authoritative migration (`20260714111706`) uses `bigserial` and lacks CHECK constraints. Both use `CREATE TABLE IF NOT EXISTS`, so if both run, the first creates the table and the second is a no-op for table creation. However, the second drops and recreates RLS policies and adds indexes.
- The UX redesign migration (`20260718143752`) adds `file_name` and `short_code` columns, plus trigram indexes for search. Requires `pg_trgm` extension.

### Example Row

```json
{
  "id": 42,
  "save_code": "S0042",
  "short_code": "S0042",
  "save_type": "deep",
  "origin_chat_id": -1001234567890,
  "origin_msg_id": 8765,
  "saved_chat_id": 123456789,
  "saved_msg_id": 5432,
  "sender_name": "John Doe",
  "sender_id": 987654321,
  "mime_type": "video/mp4",
  "file_id": "1234567890123456",
  "file_size": 5242880,
  "media_type": "Video",
  "file_name": "S0042.mp4",
  "tags": ["#saved", "#saved_video", "#saved_2026", "#saved_2026_07", "#saved_2026_07_30"],
  "caption": "📦 DeepSaved\n🎙 Sender: John Doe\n...",
  "owner_id": 123456789,
  "created_at": "2026-07-30T14:22:00+03:30"
}
```

---

## 4. Table: bio_state

**Purpose:** Singleton-per-owner state for the bio cron engine. Stores the template, mood, custom text, active flag, and last-rendered bio string for deduplication.

### CREATE TABLE Statement (Authoritative — migration `20260714111706`)

```sql
CREATE TABLE IF NOT EXISTS bio_state (
    id bigserial PRIMARY KEY,
    owner_id bigint UNIQUE NOT NULL,
    template text NOT NULL DEFAULT '🕒 {time} | 💭 {mood}',
    mood text NOT NULL DEFAULT '😊',
    custom_text text NOT NULL DEFAULT '',
    is_active boolean NOT NULL DEFAULT false,
    last_bio text NOT NULL DEFAULT '',
    updated_at timestamptz DEFAULT now()
);
```

### Complete Column Reference

| Column | SQL Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | `bigserial` | NO | `nextval()` | Primary key |
| `owner_id` | `bigint` | NO | — | UNIQUE constraint — one row per owner |
| `template` | `text` | NO | `'🕒 {time} \| 💭 {mood}'` | Bio template with `{time}`, `{mood}`, `{text}` tokens |
| `mood` | `text` | NO | `'😊'` | Current mood value |
| `custom_text` | `text` | NO | `''` | Freeform text token value |
| `is_active` | `boolean` | NO | `false` | Whether the bio cron is running |
| `last_bio` | `text` | NO | `''` | Last rendered bio string (for deduplication) |
| `updated_at` | `timestamptz` | YES | `now()` | Last update timestamp |

### Indexes

```sql
CREATE INDEX IF NOT EXISTS idx_bio_state_owner ON bio_state (owner_id);
```

**Note:** The `owner_id` UNIQUE constraint already creates an implicit unique index, making `idx_bio_state_owner` technically redundant.

### Constraints

- `owner_id` — UNIQUE NOT NULL

### RLS Policies

**Authoritative migration (active):**

```sql
ALTER TABLE bio_state ENABLE ROW LEVEL SECURITY;

CREATE POLICY "anon_select_bio_state" ON bio_state FOR SELECT
    TO anon, authenticated USING (true);
-- No INSERT, UPDATE, or DELETE policies for anon/authenticated.
```

**Initial migration (superseded):**

```sql
CREATE POLICY "anon_insert_bio_state" ON bio_state FOR INSERT
    TO anon, authenticated WITH CHECK (true);
CREATE POLICY "anon_update_bio_state" ON bio_state FOR UPDATE
    TO anon, authenticated USING (true) WITH CHECK (true);
CREATE POLICY "anon_delete_bio_state" ON bio_state FOR DELETE
    TO anon, authenticated USING (true);
```

### Triggers

None. The `updated_at` column has a default of `now()` but no trigger to auto-update it on row modification. The application code manually sets `updated_at` in some update calls (bio cron writes `"updated_at": datetime.now(tz).isoformat()`) but not in others (`.bio template`, `.bio text`, `.bio mood`, `.bio on`, `.bio off` do not include `updated_at`).

### Migration Notes

- Initial migration uses `SERIAL` primary key; authoritative uses `bigserial`.
- No schema changes to `bio_state` in subsequent migrations.

### Example Row

```json
{
  "id": 1,
  "owner_id": 123456789,
  "template": "🕒 {time} | 💭 {mood} | 📝 {text}",
  "mood": "🚀",
  "custom_text": "Building LifeOS",
  "is_active": true,
  "last_bio": "🕒 14:22 | 💭 🚀 | 📝 Building LifeOS",
  "updated_at": "2026-07-30T14:22:00+03:30"
}
```

---

## 5. Table: bot_logs

**Purpose:** Structured activity log. Each row is a discrete bot event with level, message, and JSONB context.

### CREATE TABLE Statement (Authoritative — migration `20260714111706`)

```sql
CREATE TABLE IF NOT EXISTS bot_logs (
    id bigserial PRIMARY KEY,
    owner_id bigint NOT NULL,
    level text NOT NULL DEFAULT 'INFO',
    message text NOT NULL,
    context jsonb DEFAULT '{}',
    created_at timestamptz DEFAULT now()
);
```

### Complete Column Reference

| Column | SQL Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | `bigserial` | NO | `nextval()` | Primary key |
| `owner_id` | `bigint` | NO | — | Telegram user ID of the bot owner |
| `level` | `text` | NO | `'INFO'` | Values: `'INFO'`, `'WARN'`, `'ERROR'`. CHECK constraint only in initial migration. |
| `message` | `text` | NO | — | Log message text |
| `context` | `jsonb` | YES | `'{}'` | JSONB context data |
| `created_at` | `timestamptz` | YES | `now()` | Timestamp of the log entry |

### Indexes

```sql
CREATE INDEX IF NOT EXISTS idx_bot_logs_owner ON bot_logs (owner_id);
CREATE INDEX IF NOT EXISTS idx_bot_logs_created_at ON bot_logs (created_at DESC);
```

### Constraints

- `owner_id` — NOT NULL
- `level` — NOT NULL, default `'INFO'`
- **CHECK (level IN ('INFO', 'WARN', 'ERROR'))** — present in initial migration only, missing from authoritative migration

### RLS Policies

**Authoritative migration (active):**

```sql
ALTER TABLE bot_logs ENABLE ROW LEVEL SECURITY;

CREATE POLICY "anon_select_bot_logs" ON bot_logs FOR SELECT
    TO anon, authenticated USING (true);
-- No INSERT, UPDATE, or DELETE policies for anon/authenticated.
```

**Initial migration (superseded):**

```sql
CREATE POLICY "anon_insert_bot_logs" ON bot_logs FOR INSERT
    TO anon, authenticated WITH CHECK (true);
CREATE POLICY "anon_update_bot_logs" ON bot_logs FOR UPDATE
    TO anon, authenticated USING (true) WITH CHECK (true);
CREATE POLICY "anon_delete_bot_logs" ON bot_logs FOR DELETE
    TO anon, authenticated USING (true);
```

### Triggers

None.

### Migration Notes

- Initial migration uses `SERIAL` primary key; authoritative uses `bigserial`.
- No schema changes to `bot_logs` in subsequent migrations.

### Example Row

```json
{
  "id": 157,
  "owner_id": 123456789,
  "level": "INFO",
  "message": "Saved D S0042",
  "context": {
    "save_code": "S0042",
    "origin_chat_id": -1001234567890,
    "origin_msg_id": 8765
  },
  "created_at": "2026-07-30T14:22:01+00:00"
}
```

---

## 6. Table: panel_settings

**Purpose:** Permanent configuration storage for the entire helper panel system. Uses typed columns (not key-value) so every setting has a proper SQL type, a default, and a CHECK constraint. Supabase is the single source of truth.

**Cardinality:** Single row (`key = 'global'`).

### CREATE TABLE Statement (migration `20260726143924`)

```sql
CREATE TABLE IF NOT EXISTS panel_settings (
    key text PRIMARY KEY,
    auto_close_enabled boolean NOT NULL DEFAULT true,
    updated_at timestamptz DEFAULT now()
);
```

### ALTER Statements (migration `20260729221445`)

```sql
ALTER TABLE panel_settings
    ADD COLUMN IF NOT EXISTS auto_close_delay_seconds integer NOT NULL DEFAULT 120
        CHECK (auto_close_delay_seconds >= 10 AND auto_close_delay_seconds <= 3600);

ALTER TABLE panel_settings
    ADD COLUMN IF NOT EXISTS max_deep_save_mb integer NOT NULL DEFAULT 50
        CHECK (max_deep_save_mb >= 1 AND max_deep_save_mb <= 500);

ALTER TABLE panel_settings
    ADD COLUMN IF NOT EXISTS delete_batch_size integer NOT NULL DEFAULT 100
        CHECK (delete_batch_size >= 1 AND delete_batch_size <= 1000);

ALTER TABLE panel_settings
    ADD COLUMN IF NOT EXISTS log_retention_days integer NOT NULL DEFAULT 7
        CHECK (log_retention_days >= 1 AND log_retention_days <= 365);

ALTER TABLE panel_settings
    ADD COLUMN IF NOT EXISTS panel_countdown_interval integer NOT NULL DEFAULT 30
        CHECK (panel_countdown_interval >= 5 AND panel_countdown_interval <= 120);

ALTER TABLE panel_settings
    ADD COLUMN IF NOT EXISTS input_timeout_seconds integer NOT NULL DEFAULT 120
        CHECK (input_timeout_seconds >= 10 AND input_timeout_seconds <= 600);
```

### Ensure 'global' row exists

```sql
INSERT INTO panel_settings (key, auto_close_enabled)
VALUES ('global', true)
ON CONFLICT (key) DO NOTHING;
```

### Complete Column Reference

| Column | SQL Type | Nullable | Default | Constraints | Purpose |
|---|---|---|---|---|---|
| `key` | `text` | NO | — | PRIMARY KEY | Singleton row key — always `'global'` |
| `auto_close_enabled` | `boolean` | NO | `true` | — | Whether inline panels auto-close after the delay |
| `auto_close_delay_seconds` | `integer` | NO | `120` | CHECK (10–3600) | Seconds before an inline panel auto-closes |
| `max_deep_save_mb` | `integer` | NO | `50` | CHECK (1–500) | Maximum file size in MB for deep saves |
| `delete_batch_size` | `integer` | NO | `100` | CHECK (1–1000) | Messages per `delete_messages()` API call |
| `log_retention_days` | `integer` | NO | `7` | CHECK (1–365) | Days of logs to retain before cleanup |
| `panel_countdown_interval` | `integer` | NO | `30` | CHECK (5–120) | Seconds between countdown re-renders |
| `input_timeout_seconds` | `integer` | NO | `120` | CHECK (10–600) | Seconds before a pending input request expires |
| `updated_at` | `timestamptz` | YES | `now()` | — | Last update timestamp |

### Indexes

- Primary key index on `key` (implicit).

### Constraints

- `key` — PRIMARY KEY
- `auto_close_delay_seconds` — CHECK (>= 10 AND <= 3600)
- `max_deep_save_mb` — CHECK (>= 1 AND <= 500)
- `delete_batch_size` — CHECK (>= 1 AND <= 1000)
- `log_retention_days` — CHECK (>= 1 AND <= 365)
- `panel_countdown_interval` — CHECK (>= 5 AND <= 120)
- `input_timeout_seconds` — CHECK (>= 10 AND <= 600)

### RLS Policies

```sql
ALTER TABLE panel_settings ENABLE ROW LEVEL SECURITY;

CREATE POLICY "anon_select_panel_settings" ON panel_settings FOR SELECT
    TO anon, authenticated USING (true);
-- No INSERT, UPDATE, or DELETE policies for anon/authenticated.
```

### Triggers

None. The `updated_at` column has a default of `now()` but no auto-update trigger. The `settings_service` manually includes `updated_at` in every `upsert()` call.

### Migration Notes

- Created by `20260726143924` with only `key`, `auto_close_enabled`, `updated_at`.
- Expanded by `20260729221445` with 6 typed columns + CHECK constraints.
- Data migrated from `bot_settings` (if it exists) into the typed columns via a `DO $$ ... END $$` block.
- The migration is idempotent — safe to re-run.

### Example Row

```json
{
  "key": "global",
  "auto_close_enabled": true,
  "auto_close_delay_seconds": 120,
  "max_deep_save_mb": 50,
  "delete_batch_size": 100,
  "log_retention_days": 7,
  "panel_countdown_interval": 30,
  "input_timeout_seconds": 120,
  "updated_at": "2026-07-30T14:22:00+00:00"
}
```

---

## 7. Table: bot_settings (Legacy)

**Purpose:** Key-value configuration store. Superseded by `panel_settings` typed columns. The table is NOT dropped — it remains for backward compatibility but is no longer the source of truth.

### CREATE TABLE Statement (migration `20260729213959`)

```sql
CREATE TABLE IF NOT EXISTS bot_settings (
    key text PRIMARY KEY,
    value text NOT NULL,
    value_type text NOT NULL DEFAULT 'str',
    updated_at timestamptz DEFAULT now()
);
```

### Complete Column Reference

| Column | SQL Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `key` | `text` | NO | — | PRIMARY KEY |
| `value` | `text` | NO | — | Setting value stored as text |
| `value_type` | `text` | NO | `'str'` | Hint: `'bool'`, `'int'`, `'str'` |
| `updated_at` | `timestamptz` | YES | `now()` | Last update timestamp |

### Default Data Inserted

```sql
INSERT INTO bot_settings (key, value, value_type) VALUES
    ('auto_close_enabled', 'true', 'bool'),
    ('panel_auto_close_seconds', '120', 'int'),
    ('max_deep_save_mb', '50', 'int'),
    ('delete_batch_size', '100', 'int'),
    ('log_cleanup_days', '7', 'int')
ON CONFLICT (key) DO NOTHING;
```

### RLS Policies

```sql
ALTER TABLE bot_settings ENABLE ROW LEVEL SECURITY;

CREATE POLICY "anon_select_bot_settings" ON bot_settings FOR SELECT
    TO anon, authenticated USING (true);
```

### Migration Notes

- Created by `20260729213959` as a replacement for `panel_settings`.
- Data from `panel_settings.auto_close_enabled` was migrated into `bot_settings`.
- Subsequently, `20260729221445` reversed the flow: it migrated data from `bot_settings` back into `panel_settings` typed columns.
- The `settings_service` reads from `panel_settings`, not `bot_settings`.
- The table is not dropped for safety.

---

## 8. Relationships

### Inter-Table Relationships

There are **no foreign keys** between any tables. All tables share a common `owner_id` column (`bigint`), which represents the Telegram user ID of the bot owner, but this is an application-level logical relationship, not a database constraint.

### Cardinality

| Table | Cardinality | Key |
|---|---|---|
| `saved_items` : `owner_id` | Many-to-one | Many saves per owner |
| `bio_state` : `owner_id` | One-to-one | Enforced by UNIQUE constraint |
| `bot_logs` : `owner_id` | Many-to-one | Many logs per owner |
| `panel_settings` | Single row | `key='global'`, not per-owner |
| `bot_settings` | Multiple rows | One row per setting key |

---

## 9. Data Flow

### `.save f` (Forward Save)

**Tables touched:** `saved_items` (INSERT + count SELECT), `bot_logs` (INSERT)

```
Reply to message → .save f
    → get_next_save_code() → SELECT count FROM saved_items → S{count+1}
    → forward_messages("me", reply)
    → insert_save({save_code, short_code, save_type='forward', ...})
    → log(owner_id, 'INFO', 'Saved F {code}', {...})
    → edit("✅ Saved Successfully")
```

### `.save d` (Deep Save)

**Tables touched:** `saved_items` (INSERT + count SELECT), `bot_logs` (INSERT), `panel_settings` (SELECT max_deep_save_mb)

```
Reply to message → .save d
    → get_next_save_code()
    → settings_service.max_deep_save_mb() → SELECT FROM panel_settings
    → check file_size <= max_bytes
    → download_media(reply, buf)
    → send_file("me", buf, caption)
    → insert_save({save_code, short_code, save_type='deep', caption, ...})
    → log(owner_id, 'INFO', 'Saved D {code}', {...})
    → edit("✅ Saved Successfully")
```

### `.preview <code>`

**Tables touched:** `saved_items` (SELECT by short_code OR save_code)

### `.send <code>`

**Tables touched:** `saved_items` (SELECT), `bot_logs` (INSERT)

### `.del <n>`

**Tables touched:** `panel_settings` (SELECT delete_batch_size)

### `.del id <msgid>`

**Tables touched:** `panel_settings` (SELECT delete_batch_size)

### `.del <code>`

**Tables touched:** `saved_items` (SELECT + DELETE), `bot_logs` (INSERT)

### `.organize list`

**Tables touched:** `saved_items` (3x count SELECT), `bot_logs` (count SELECT), `bio_state` (SELECT)

### `.organize clean`

**Tables touched:** `bot_logs` (DELETE with date filter), `panel_settings` (SELECT log_retention_days)

### `.bio on/off/template/text/mood/show`

**Tables touched:** `bio_state` (SELECT or INSERT+SELECT, then UPDATE)

### Bio Cron Loop (background, every minute)

**Tables touched:** `bio_state` (SELECT every tick, UPDATE when bio changes)

### `.list [n]`

**Tables touched:** `saved_items` (SELECT recent, limited)

### `.find <text>`

**Tables touched:** `saved_items` (SELECT with ILIKE search)

### `.db clean`

**Tables touched:** `saved_items` (SELECT all + DELETE orphans), `bot_logs` (INSERT)

### `.db stats`

**Tables touched:** `saved_items` (SELECT all for stats), `bot_logs` (INSERT)

### `.db vacuum`

**Tables touched:** `saved_items` (SELECT all + DELETE orphans), `bot_logs` (INSERT)

### `GET /api/saves`

**Tables touched:** `saved_items` (SELECT paginated + count SELECT). Hardcodes `owner_id=0`.

### `GET /api/saves/{save_code}`

**Tables touched:** `saved_items` (SELECT by save_code)

### `GET /api/bio`

**Tables touched:** `bio_state` (SELECT by owner_id). Hardcodes `owner_id=0`.

### `GET /api/logs`

**Tables touched:** `bot_logs` (SELECT ordered, limited). Hardcodes `owner_id=0`.

### `GET /api/settings`

**Tables touched:** `panel_settings` (SELECT all)

### `GET /health`

**Tables touched:** NONE (reads in-memory health state)

### Settings Panel (inline UI)

**Tables touched:** `panel_settings` (SELECT on open, UPDATE on change)

### Startup — Settings Load

**Tables touched:** `panel_settings` (SELECT all columns for `key='global'`)

---

## 10. Feature Mapping

### Command to Database Object Matrix

| Command | `saved_items` | `bio_state` | `bot_logs` | `panel_settings` | `bot_settings` |
|---|---|---|---|---|---|
| `.ping` | — | — | — | — | — |
| `.id` | — | — | — | — | — |
| `.help` | — | — | — | — | — |
| `.save f` / `.s f` | INSERT + count SELECT | — | INSERT | — | — |
| `.save d` / `.s d` | INSERT + count SELECT | — | INSERT | SELECT (max_deep_save_mb) | — |
| `.preview <code>` | SELECT | — | — | — | — |
| `.send <code>` | SELECT | — | INSERT | — | — |
| `.del <n>` | — | — | — | SELECT (delete_batch_size) | — |
| `.del id <msgid>` | — | — | — | SELECT (delete_batch_size) | — |
| `.del <code>` | SELECT + DELETE | — | INSERT | — | — |
| `.organize list` | 3x count SELECT | SELECT | count SELECT | — | — |
| `.organize clean` | — | — | DELETE | SELECT (log_retention_days) | — |
| `.bio on` | — | SELECT or INSERT+SELECT, UPDATE | — | — | — |
| `.bio off` | — | SELECT or INSERT+SELECT, UPDATE | — | — | — |
| `.bio template/text/mood` | — | SELECT or INSERT+SELECT, UPDATE | — | — | — |
| `.bio show` | — | SELECT or INSERT+SELECT | — | — | — |
| `.list [n]` | SELECT (recent, limited) | — | — | — | — |
| `.find <text>` | SELECT (ILIKE search) | — | — | — | — |
| `.db clean` | SELECT all + DELETE | — | INSERT | — | — |
| `.db stats` | SELECT all | — | INSERT | — | — |
| `.db vacuum` | SELECT all + DELETE | — | INSERT | — | — |
| Bio cron (background) | — | SELECT (every tick), UPDATE (on change) | — | — | — |
| Settings panel (inline) | — | — | — | SELECT + UPDATE (upsert) | — |
| Startup (settings load) | — | — | — | SELECT | — |
| Startup (warm-up probe) | — | — | SELECT (LIMIT 1) | — | — |

### API Endpoint to Database Object Matrix

| Endpoint | `saved_items` | `bio_state` | `bot_logs` | `panel_settings` |
|---|---|---|---|---|
| `GET /health` | — | — | — | — |
| `GET /api/saves` | SELECT (paginated) + count | — | — | — |
| `GET /api/saves/{code}` | SELECT by code | — | — | — |
| `GET /api/bio` | — | SELECT by owner_id | — | — |
| `GET /api/logs` | — | — | SELECT (ordered, limited) | — |
| `GET /api/settings` | — | — | — | SELECT all |

### `db/client.py` Function to Table Matrix

| Function | `saved_items` | `bio_state` | `bot_logs` | `panel_settings` |
|---|---|---|---|---|
| `get_db()` | — | — | — | — (client init) |
| `is_available()` | — | — | — | — (status check) |
| `log()` | — | — | INSERT | — |
| `get_next_save_code()` | SELECT (count) | — | — | — |
| `insert_save()` | INSERT | — | — | — |
| `query_save()` | SELECT by short_code OR save_code | — | — | — |
| `list_saves()` | SELECT (paginated) + count | — | — | — |
| `list_recent_saves()` | SELECT (recent, limited) | — | — | — |
| `search_saves()` | SELECT (ILIKE search) | — | — | — |
| `delete_save()` | SELECT + DELETE | — | — | — |
| `delete_save_row()` | SELECT + DELETE | — | — | — |
| `list_all_saves()` | SELECT all | — | — | — |
| `cleanup_orphans()` | DELETE by IDs | — | — | — |
| `get_stats()` | SELECT all (computed) | — | — | — |
| `count_saves()` | SELECT (count, optional filter) | — | — | — |
| `get_bio_state()` | — | SELECT by owner_id | — | — |
| `get_or_create_bio_state()` | — | SELECT, then INSERT if not found | — | — |
| `update_bio_state()` | — | UPDATE by owner_id | — | — |
| `count_logs()` | — | — | SELECT (count) | — |
| `list_logs()` | — | — | SELECT (ordered, limited) | — |
| `clean_logs()` | — | — | DELETE (older than cutoff) | — |

### `settings_service` Function to Table Matrix

| Function | `panel_settings` |
|---|---|
| `load_all()` | SELECT * WHERE key='global' |
| `_refresh()` | SELECT * WHERE key='global' |
| `_update()` | UPSERT (key='global' + updates) |
| `get_all()` | (reads from cache) |
| `is_auto_close_enabled()` | (reads from cache) |
| `set_auto_close_enabled()` | UPSERT |
| `toggle_auto_close()` | UPSERT |
| `auto_close_delay_seconds()` | (reads from cache) |
| `set_auto_close_delay_seconds()` | UPSERT |
| `max_deep_save_mb()` | (reads from cache) |
| `set_max_deep_save_mb()` | UPSERT |
| `delete_batch_size()` | (reads from cache) |
| `set_delete_batch_size()` | UPSERT |
| `log_retention_days()` | (reads from cache) |
| `set_log_retention_days()` | UPSERT |
| `panel_countdown_interval()` | (reads from cache) |
| `set_panel_countdown_interval()` | UPSERT |
| `input_timeout_seconds()` | (reads from cache) |
| `set_input_timeout_seconds()` | UPSERT |

---

## 11. Supabase Services

| Service | Status | Details |
|---|---|---|
| **Authentication** | NOT USED | No Supabase Auth. Bot authenticates to Telegram via Telethon StringSession. Supabase client uses service-role key. |
| **Storage** | NOT USED | Media stored in Telegram Saved Messages, not Supabase Storage. `file_id` stores Telegram's internal reference. |
| **RLS Policies** | PARTIALLY USED | RLS enabled on all tables. Only SELECT policies for `anon` + `authenticated`. All writes via service-role key (bypasses RLS). |
| **Functions (RPC)** | NOT USED | No `supabase.rpc()` calls. |
| **Realtime** | NOT USED | Frontend polls API every 30 seconds via `setInterval`. |
| **Edge Functions** | NOT USED | No Edge Functions deployed. |
| **Database (PostgreSQL)** | USED | Core service. Five tables accessed via PostgREST. |
| **PostgREST API** | USED | All database access via REST API through `supabase-py`. |
| **Migrations** | USED | Migration files in `supabase/migrations/`. Applied via Supabase MCP `apply_migration` tool. |

---

## 12. Environment Variables

### Database-Related

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `SUPABASE_URL` | No | `""` | Supabase project URL. If empty, in-memory fallback. |
| `SUPABASE_SERVICE_ROLE_KEY` | No | `""` | Supabase service-role key. Bypasses RLS. If empty, in-memory fallback. |
| `DATABASE_URL` | No | `""` | Loaded by `config.py` but never consumed. Reserved for future use. |
| `BOT_OWNER_ID` | **Yes** | — | Telegram numeric user ID. Used as `owner_id` in all DB writes. Web API hardcodes `owner_id=0`. |

### Non-Database

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `API_ID` | **Yes** | — | Telegram API ID |
| `API_HASH` | **Yes** | — | Telegram API hash |
| `SESSION_STRING` | **Yes** | — | Telethon StringSession |
| `BOT_TOKEN` | No | `""` | Helper bot token. Enables inline UI. |
| `TZ` | No | `Asia/Tehran` | Timezone for bio engine |
| `PORT` | No | `8000` | Web server port |
| `BIO_UPDATE_ENABLED` | No | `false` | Auto-start bio cron on boot |
| `LOG_LEVEL` | No | `INFO` | Python logging level |
| `GHOST_ROOM_ID` | No | `""` | Unused |
| `DEST_CHANNEL_ID` | No | `""` | Unused |

---

## 13. Migration History

### Migration Files (in chronological order)

| # | File | Description |
|---|---|---|
| 1 | `20260712234229_lifeos_schema.sql` | Initial schema. Creates `saved_items`, `bio_state`, `bot_logs` with `SERIAL` PKs, CHECK constraints, and wide-open CRUD RLS policies. |
| 2 | `20260714111706_create_lifeos_tables.sql` | Authoritative. Creates same tables with `bigserial` PKs, no CHECK constraints, and SELECT-only RLS policies. Drops and recreates RLS policies. Adds indexes. Uses `CREATE TABLE IF NOT EXISTS` so table creation is a no-op if tables already exist. |
| 3 | `20260718143752_20260718_save_ux_redesign.sql.sql` | Adds `file_name` and `short_code` columns to `saved_items`. Creates partial unique index on `short_code`. Adds composite index on `(owner_id, created_at DESC)`. Adds trigram GIN indexes for ILIKE search. Enables `pg_trgm` extension. |
| 4 | `20260726143924_create_panel_settings_table.sql` | Creates `panel_settings` table with `key`, `auto_close_enabled`, `updated_at`. Enables RLS with SELECT-only policy. |
| 5 | `20260729213959_20260729120000_create_bot_settings_table.sql` | Creates `bot_settings` key-value table. Migrates `auto_close_enabled` from `panel_settings`. Inserts default settings. |
| 6 | `20260729221445_expand_panel_settings_typed_columns.sql` | Expands `panel_settings` with 6 typed columns + CHECK constraints. Migrates values from `bot_settings` into typed columns. Ensures 'global' row exists. Re-establishes RLS. |

### Migration Order Matters

If migrations are applied in order:
1. Migration 1 creates tables with CHECK constraints and wide-open RLS.
2. Migration 2 drops and recreates RLS policies (locking down to SELECT-only). Table creation is a no-op. CHECK constraints from migration 1 persist.
3. Migration 3 adds columns and indexes to `saved_items`.
4. Migrations 4-6 create and expand the settings tables.

If only migration 2 is applied (skipping migration 1), CHECK constraints on `save_type` and `level` are missing.

---

## 14. Missing Pieces

### Missing Constraints

1. **`CHECK (save_type IN ('forward', 'deep'))`** — present in initial migration, missing from authoritative migration.
2. **`CHECK (level IN ('INFO', 'WARN', 'ERROR'))`** — present in initial migration, missing from authoritative migration.

### Missing Indexes

1. **GIN index on `saved_items.tags`** — the `tags` column is `text[]` but has no GIN index for array operations.

### Missing Triggers

1. **`bio_state.updated_at` auto-update trigger** — no trigger to automatically set `updated_at` on row modification.
2. **`panel_settings.updated_at` auto-update trigger** — same. The `settings_service` manually includes `updated_at` in upserts, but a trigger would be more robust.

### Missing RLS Policies

1. **No write policies for anon/authenticated** on any table. This is by design — all writes go through the service-role key.

### Missing Functionality

1. **No `update_save` function** — saves are immutable (except deletion).
2. **No `delete_bio_state` function** — bio_state row cannot be deleted.
3. **No `update_log` function** — only bulk `clean_logs()`.
4. **No direct PostgreSQL connection** — `DATABASE_URL` is loaded but never used.

---

## 15. Risk Analysis

### R-1: Save Code Generation Not Atomic Across Restarts

**Severity:** High

`get_next_save_code()` counts rows and returns `S{count+1:04d}`. The `asyncio.Lock` prevents concurrent saves within a single process, but restarts between count and insert can cause duplicate codes. Falls back to a random alphanumeric code on collision (up to 50 attempts).

**Fix direction:** Use a PostgreSQL sequence.

### R-2: `get_or_create_bio_state()` Race Condition

**Severity:** Medium

SELECT then INSERT is not atomic. Concurrent calls can cause UNIQUE constraint violations on `owner_id`.

**Fix direction:** Use `INSERT ... ON CONFLICT DO NOTHING` (UPSERT).

### R-3: Web API Hardcodes `owner_id=0`

**Severity:** Medium

All API endpoints pass `owner_id=0`, making the dashboard useless for any owner whose ID is not `0`.

### R-4: No `updated_at` Auto-Update Trigger on `bio_state`

**Severity:** Low

`updated_at` is not auto-updated on modification. Some update calls include it manually; others do not.

### R-5: Missing CHECK Constraints in Authoritative Migration

**Severity:** Low

`save_type` and `level` lack CHECK constraints. Application code only inserts valid values, but the database does not enforce them.

### R-6: Synchronous Supabase Calls Block the Event Loop

**Severity:** Medium

The `supabase-py` client blocks the asyncio event loop during HTTP round-trips.

**Fix direction:** Use async client or run in a thread executor.

### R-7: No GIN Index on `tags` Array

**Severity:** Low

The `tags` column is `text[]` but has no GIN index. Tag-based queries would require a full table scan.

### R-8: Two Conflicting Migrations

**Severity:** Medium

Migrations 1 and 2 have different schemas and RLS policies. If both run, CHECK constraints from migration 1 persist while RLS policies from migration 2 override. If only migration 2 is applied, CHECK constraints are missing.

### R-9: No Data Retention Policy for `saved_items`

**Severity:** Low

`saved_items` rows are never deleted by the application (except via `.del <code>` or `.db clean`). The table grows indefinitely.

### R-10: `saved_items` Count Includes All Owners

**Severity:** Low

`get_next_save_code()` counts ALL rows regardless of `owner_id`. In a multi-owner scenario, save codes would be shared across owners.

### R-11: RLS SELECT Policies Are Fully Open

**Severity:** Medium

All tables have `SELECT ... USING (true)` for `anon` + `authenticated`. Anyone with the anon key can read all data.

### R-12: `clean_logs()` Return Count May Be Unreliable

**Severity:** Low

`clean_logs()` returns `len(result.data)` which may be `None` or empty depending on the `Prefer: return=representation` header.

### Summary

| Severity | Count | IDs |
|---|---|---|
| High | 1 | R-1 |
| Medium | 4 | R-2, R-3, R-6, R-8, R-11 |
| Low | 6 | R-4, R-5, R-7, R-9, R-10, R-12 |

---

## 16. Manual Setup Guide

### Step 1: Create the Supabase Project

1. Log into the Supabase Dashboard.
2. Click "New Project".
3. Choose an organization, enter a project name and database password.
4. Select a region close to the Render deployment region.
5. Wait for provisioning to complete.

### Step 2: Retrieve Credentials

1. Go to Settings → API.
2. Note the **Project URL** — this becomes `SUPABASE_URL`.
3. Note the **service_role key** — this becomes `SUPABASE_SERVICE_ROLE_KEY`. Keep this secret; it bypasses all RLS.

### Step 3: Apply Migrations

Go to the SQL Editor and run each migration file in order:

1. `20260712234229_lifeos_schema.sql`
2. `20260714111706_create_lifeos_tables.sql`
3. `20260718143752_20260718_save_ux_redesign.sql.sql`
4. `20260726143924_create_panel_settings_table.sql`
5. `20260729213959_20260729120000_create_bot_settings_table.sql`
6. `20260729221445_expand_panel_settings_typed_columns.sql`

After running, verify in the Table Editor that five tables exist: `saved_items`, `bio_state`, `bot_logs`, `panel_settings`, `bot_settings`.

### Step 4: Verify RLS

1. Go to Authentication → Policies.
2. Verify RLS is **enabled** on all tables.
3. Verify each table has a SELECT policy for `anon` and `authenticated` with `USING (true)`.
4. Verify no INSERT, UPDATE, or DELETE policies exist for `anon` or `authenticated`.

### Step 5: Add Recommended Constraints (Optional)

```sql
ALTER TABLE saved_items ADD CONSTRAINT check_save_type
    CHECK (save_type IN ('forward', 'deep'));
ALTER TABLE bot_logs ADD CONSTRAINT check_level
    CHECK (level IN ('INFO', 'WARN', 'ERROR'));
```

### Step 6: Add `updated_at` Trigger (Optional)

```sql
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER bio_state_updated_at
    BEFORE UPDATE ON bio_state
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
```

### Step 7: Add GIN Index on Tags (Optional)

```sql
CREATE INDEX IF NOT EXISTS idx_saved_items_tags
    ON saved_items USING GIN (tags);
```

### Step 8: Set Environment Variables in Render

1. Go to Environment.
2. Set `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `BOT_OWNER_ID`.
3. Save and trigger a redeploy.

### Step 9: Verify Deployment

1. Check Render logs for `Supabase client initialised.`
2. Send `.ping` via Telegram to confirm the bot is running.
3. Send `.save f` to create the first `saved_items` row.
4. Send `.organize list` to verify counts are non-zero.
5. Send `.bio on` to create the `bio_state` row and start the cron.
6. Open the dashboard URL to verify the API returns data.

### What Does NOT Need to Be Created

- No Supabase Auth users
- No Storage buckets
- No Edge Functions
- No RPC functions
- No Realtime subscriptions
- No additional schemas — everything is in the `public` schema

---

## Planned Tables

No planned tables exist. The five current tables cover all application functionality. If future features require new tables (e.g., user accounts for multi-owner support, scheduled messages, media backups), they would be added as new migrations.

---

*This document reflects the state of the repository as of the `expand_panel_settings_typed_columns` migration. If the codebase changes in ways that invalidate any section above, update this document.*
