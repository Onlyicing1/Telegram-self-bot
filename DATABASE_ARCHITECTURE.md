# DATABASE_ARCHITECTURE.md — LifeOS Telegram Self-Bot

> Database reference generated from source code and migration inspection.

---

## Overview

The application uses Supabase (PostgreSQL via PostgREST) with five tables. All database access goes through the `supabase-py` client (v2.4.2) using the service-role key, which bypasses RLS. If Supabase is unavailable, an in-memory Python dict provides silent fallback.

---

## Communication Model

```
Backend (asyncio) → supabase-py (sync httpx) → PostgREST REST API → PostgreSQL
```

- No direct PostgreSQL connections
- No Supabase CLI
- No `psql`
- No Supabase Auth, Storage, Realtime, Edge Functions, or RPC

---

## Tables

### 1. `saved_items`

Stores metadata for every media save operation (forward and deep).

| Column | Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | bigserial | NO | auto | Primary key |
| `save_code` | text | NO | — | UNIQUE. Legacy format `SV-NNNNNN`, current `SNNNN` |
| `short_code` | text | YES | — | UNIQUE (partial, WHERE NOT NULL). Current format `S` + 4 digits |
| `save_type` | text | NO | `'forward'` | Values: `forward`, `deep` |
| `origin_chat_id` | bigint | YES | — | Source chat ID |
| `origin_msg_id` | bigint | YES | — | Source message ID |
| `saved_chat_id` | bigint | YES | — | Saved Messages chat ID |
| `saved_msg_id` | bigint | YES | — | Saved message ID |
| `sender_name` | text | YES | — | Original sender display name |
| `sender_id` | bigint | YES | — | Original sender Telegram ID |
| `mime_type` | text | YES | — | MIME type of media |
| `file_id` | text | YES | — | Telegram file ID |
| `file_size` | bigint | YES | — | Size in bytes |
| `media_type` | text | YES | — | Classified: Photo, Video, Animation, Audio, Voice, Sticker, Document, Unknown |
| `file_name` | text | YES | — | Original filename (added in UX redesign) |
| `tags` | text[] | YES | `'{}'` | Hashtag array |
| `caption` | text | YES | — | Generated caption (deep saves only) |
| `owner_id` | bigint | NO | — | Bot owner Telegram ID |
| `created_at` | timestamptz | YES | `now()` | Save timestamp |

**Indexes:**
- `idx_saved_items_owner` on `owner_id`
- `idx_saved_items_save_code` on `save_code`
- `idx_saved_items_created_at` on `created_at DESC`
- `idx_saved_items_short_code` (UNIQUE, partial) on `short_code WHERE NOT NULL`
- `idx_saved_items_owner_created` on `(owner_id, created_at DESC)`
- GIN trigram indexes on `caption`, `file_name`, `save_code`, `short_code`, `mime_type` (requires `pg_trgm`)

**Constraints:** `save_code` UNIQUE NOT NULL, `short_code` UNIQUE (partial), `owner_id` NOT NULL. CHECK on `save_type IN ('forward','deep')` exists only in the initial migration.

**RLS:** Enabled. SELECT policy for `anon, authenticated` with `USING (true)`. No write policies for anon/authenticated — all writes via service-role key.

---

### 2. `bio_state`

Singleton-per-owner state for the bio cron engine.

| Column | Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | bigserial | NO | auto | Primary key |
| `owner_id` | bigint | NO | — | UNIQUE — one row per owner |
| `template` | text | NO | `'🕒 {time} \| 💭 {mood}'` | Bio template with tokens |
| `mood` | text | NO | `'😊'` | Mood value |
| `custom_text` | text | NO | `''` | Freeform text token |
| `is_active` | boolean | NO | `false` | Cron running flag |
| `last_bio` | text | NO | `''` | Last rendered bio (for dedup) |
| `updated_at` | timestamptz | YES | `now()` | Last update timestamp |

**Indexes:** `idx_bio_state_owner` on `owner_id` (redundant with UNIQUE constraint).

**Constraints:** `owner_id` UNIQUE NOT NULL.

**RLS:** Enabled. SELECT-only for `anon, authenticated`. No write policies.

**Triggers:** None. `updated_at` is not auto-updated — some code paths include it manually, others do not.

---

### 3. `bot_logs`

Structured activity log.

| Column | Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | bigserial | NO | auto | Primary key |
| `owner_id` | bigint | NO | — | Bot owner Telegram ID |
| `level` | text | NO | `'INFO'` | Values: `INFO`, `WARN`, `ERROR` |
| `message` | text | NO | — | Log message |
| `context` | jsonb | YES | `'{}'` | Structured context data |
| `created_at` | timestamptz | YES | `now()` | Log timestamp |

**Indexes:** `idx_bot_logs_owner` on `owner_id`, `idx_bot_logs_created_at` on `created_at DESC`.

**Constraints:** `owner_id` NOT NULL, `level` NOT NULL. CHECK on `level IN ('INFO','WARN','ERROR')` exists only in the initial migration.

**RLS:** Enabled. SELECT-only for `anon, authenticated`. No write policies.

---

### 4. `panel_settings`

Permanent configuration storage. Single row (`key='global'`).

| Column | Type | Nullable | Default | Check Constraint |
|---|---|---|---|---|
| `key` | text | NO | — | PRIMARY KEY |
| `auto_close_enabled` | boolean | NO | `true` | — |
| `auto_close_delay_seconds` | integer | NO | `120` | 10–3600 |
| `max_deep_save_mb` | integer | NO | `50` | 1–500 |
| `delete_batch_size` | integer | NO | `100` | 1–1000 |
| `log_retention_days` | integer | NO | `7` | 1–365 |
| `panel_countdown_interval` | integer | NO | `30` | 5–120 |
| `input_timeout_seconds` | integer | NO | `120` | 10–600 |
| `updated_at` | timestamptz | YES | `now()` | — |

**RLS:** Enabled. SELECT-only for `anon, authenticated`. No write policies.

**Triggers:** None. `settings_service` manually includes `updated_at` in every upsert.

---

### 5. `bot_settings` (Legacy)

Key-value settings store. Superseded by `panel_settings` typed columns. Table not dropped.

| Column | Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `key` | text | NO | — | PRIMARY KEY |
| `value` | text | NO | — | Value as text |
| `value_type` | text | NO | `'str'` | Hint: `bool`, `int`, `str` |
| `updated_at` | timestamptz | YES | `now()` | Last update |

**Default rows:** `auto_close_enabled=true`, `panel_auto_close_seconds=120`, `max_deep_save_mb=50`, `delete_batch_size=100`, `log_cleanup_days=7`.

**RLS:** Enabled. SELECT-only for `anon, authenticated`.

---

## Relationships

No foreign keys exist between any tables. All tables share a logical `owner_id` column (Telegram user ID), but this is not a database constraint.

| Table | Cardinality |
|---|---|
| `saved_items` | Many rows per owner |
| `bio_state` | One row per owner (UNIQUE) |
| `bot_logs` | Many rows per owner |
| `panel_settings` | Single row (`key='global'`) |
| `bot_settings` | One row per setting key |

---

## Data Flow by Command

| Command | Tables Touched |
|---|---|
| `.save f` | `saved_items` (INSERT + count), `bot_logs` (INSERT) |
| `.save d` | `saved_items` (INSERT + count), `bot_logs` (INSERT), `panel_settings` (SELECT max_deep_save_mb) |
| `.preview <code>` | `saved_items` (SELECT by short_code OR save_code) |
| `.send <code>` | `saved_items` (SELECT), `bot_logs` (INSERT) |
| `.del <n>` | `panel_settings` (SELECT delete_batch_size) |
| `.del id <msgid>` | `panel_settings` (SELECT delete_batch_size) |
| `.del <code>` | `saved_items` (SELECT + DELETE), `bot_logs` (INSERT) |
| `.organize list` | `saved_items` (3x count), `bot_logs` (count), `bio_state` (SELECT) |
| `.organize clean` | `bot_logs` (DELETE), `panel_settings` (SELECT log_retention_days) |
| `.bio on/off/template/text/mood` | `bio_state` (SELECT or INSERT+SELECT, UPDATE) |
| `.bio show` | `bio_state` (SELECT) |
| `.list [n]` | `saved_items` (SELECT recent, limited) |
| `.find <text>` | `saved_items` (SELECT ILIKE search) |
| `.db clean` | `saved_items` (SELECT all + DELETE orphans), `bot_logs` (INSERT) |
| `.db stats` | `saved_items` (SELECT all), `bot_logs` (INSERT) |
| `.db vacuum` | `saved_items` (SELECT all + DELETE), `bot_logs` (INSERT) |
| Bio cron (background) | `bio_state` (SELECT every tick, UPDATE on change) |
| Settings panel | `panel_settings` (SELECT, UPDATE) |
| `GET /api/saves` | `saved_items` (paginated SELECT + count) |
| `GET /api/saves/{code}` | `saved_items` (SELECT by code) |
| `GET /api/bio` | `bio_state` (SELECT) |
| `GET /api/logs` | `bot_logs` (SELECT, limited) |
| `GET /api/settings` | `panel_settings` (SELECT all) |
| `GET /health` | None (reads in-memory state) |

---

## Migration History

| # | File | Summary |
|---|---|---|
| 1 | `20260712234229_lifeos_schema.sql` | Initial schema. `SERIAL` PKs, CHECK constraints, wide-open CRUD RLS. |
| 2 | `20260714111706_create_lifeos_tables.sql` | Authoritative. `bigserial` PKs, no CHECK constraints, SELECT-only RLS. Drops/recreates RLS policies. |
| 3 | `20260718143752_20260718_save_ux_redesign.sql.sql` | Adds `file_name`, `short_code` columns. Trigram GIN indexes. `pg_trgm` extension. |
| 4 | `20260726143924_create_panel_settings_table.sql` | Creates `panel_settings` with `key`, `auto_close_enabled`, `updated_at`. |
| 5 | `20260729213959_20260729120000_create_bot_settings_table.sql` | Creates `bot_settings` key-value table. Migrates `auto_close_enabled`. |
| 6 | `20260729221445_expand_panel_settings_typed_columns.sql` | Expands `panel_settings` with 6 typed columns + CHECK constraints. Migrates from `bot_settings`. Ensures `key='global'` row. |

If both migrations 1 and 2 run, CHECK constraints from migration 1 persist while RLS policies from migration 2 override. If only migration 2 runs, CHECK constraints are missing.

---

## Missing Pieces

- **CHECK constraints** on `save_type` and `level` missing from authoritative migration.
- **GIN index** on `tags` array column not created.
- **`updated_at` triggers** on `bio_state` and `panel_settings` not created.
- **No write RLS policies** for anon/authenticated (by design — service-role key bypasses RLS).
- **No `update_save` function** — saves are immutable except deletion.
- **`DATABASE_URL`** loaded but never consumed.

---

## Risk Analysis

| ID | Severity | Issue |
|---|---|---|
| R-1 | High | Save code generation not atomic across restarts |
| R-2 | Medium | `get_or_create_bio_state()` race condition (SELECT then INSERT) |
| R-3 | Medium | Web API hardcodes `owner_id=0` |
| R-4 | Low | No `updated_at` auto-trigger on `bio_state` |
| R-5 | Low | Missing CHECK constraints in authoritative migration |
| R-6 | Medium | Synchronous Supabase calls block event loop |
| R-7 | Low | No GIN index on `tags` array |
| R-8 | Medium | Two conflicting migrations (CHECK vs RLS) |
| R-9 | Low | No retention policy for `saved_items` |
| R-10 | Low | Save code count includes all owners |
| R-11 | Medium | RLS SELECT policies fully open |
| R-12 | Low | `clean_logs()` return count may be unreliable |

---

## Manual Setup

1. Create a Supabase project.
2. Note the Project URL and service_role key.
3. Run all 6 migration files in order via the SQL Editor.
4. Verify 5 tables exist: `saved_items`, `bio_state`, `bot_logs`, `panel_settings`, `bot_settings`.
5. Verify RLS is enabled on all tables with SELECT-only policies.
6. Set `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, and `BOT_OWNER_ID` in Render.
7. Deploy and verify with `.ping` and `.save f`.

No Supabase Auth users, Storage buckets, Edge Functions, or RPC functions need to be created.
