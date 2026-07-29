# DATABASE_ARCHITECTURE.md — LifeOS Telegram Self-Bot

> **Exhaustive reverse-engineered database reference.**
> This document contains everything needed to manually recreate the
> entire Supabase project from scratch without reading source code.
>
> Repository: Always push to the repository connected to the current workspace.

---

## Table of Contents

1. [High-Level Architecture](#1-high-level-architecture)
2. [Database Overview](#2-database-overview)
3. [Complete Schema](#3-complete-schema)
4. [Relationships](#4-relationships)
5. [Data Flow](#5-data-flow)
6. [Feature Mapping](#6-feature-mapping)
7. [Supabase Services](#7-supabase-services)
8. [Environment Variables](#8-environment-variables)
9. [Required Build Order](#9-required-build-order)
10. [Missing Pieces](#10-missing-pieces)
11. [Assumptions](#11-assumptions)
12. [Manual Setup Guide](#12-manual-setup-guide)
13. [Risk Analysis](#13-risk-analysis)

---

## 1. High-Level Architecture

### Communication Model

The application communicates with Supabase exclusively through the
**PostgREST REST API** (via the `supabase-py` client library, version
`2.4.2`). There are no direct PostgreSQL connections, no Supabase CLI
usage, and no `psql` calls anywhere in the codebase.

```
┌──────────────────────────────────────────────────────────────┐
│                    Single Python Process                      │
│                      (asyncio event loop)                     │
│                                                              │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐  │
│  │ Telethon │   │ Bio Cron │   │ FastAPI  │   │  Config  │  │
│  │ Handlers │   │  Engine  │   │  Web API │   │  Loader  │  │
│  └────┬─────┘   └────┬─────┘   └────┬─────┘   └──────────┘  │
│       │              │              │                         │
│       ▼              ▼              ▼                         │
│  ┌─────────────────────────────────────────┐                 │
│  │       backend/db/client.py              │                 │
│  │  (singleton Supabase client + fallback) │                 │
│  └──────────────────┬──────────────────────┘                 │
│                     │                                        │
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
           │  └───────────────┘  │
           └─────────────────────┘
```

### Key Architectural Principles

1. **Service-role key only.** The backend authenticates to Supabase
   using the `SUPABASE_SERVICE_ROLE_KEY`, which **bypasses all RLS
   policies**. Every write and read from the backend goes through the
   service-role key.

2. **In-memory fallback.** If `SUPABASE_URL` or
   `SUPABASE_SERVICE_ROLE_KEY` is missing, or if the Supabase client
   fails to initialise, the entire database layer silently degrades to
   a Python dict in memory (`_fallback`). The bot continues to
   function with no persistence. This is a deliberate design choice,
   not an error path.

3. **Synchronous HTTP calls in async context.** The `supabase-py`
   client uses `httpx` in synchronous mode. Every `.execute()` call
   blocks the asyncio event loop for the duration of the HTTP
   round-trip. This is a known architectural trade-off, not a bug.

4. **No direct SQL.** The backend never executes raw SQL. All
   database access is via the Supabase client's query builder
   (`.table()`, `.select()`, `.insert()`, `.update()`, `.delete()`,
   `.eq()`, `.lt()`, `.order()`, `.range()`, `.limit()`,
   `.maybe_single()`).

5. **No Supabase Auth, Storage, Realtime, Edge Functions, or RPC.**
   The project uses only the PostgreSQL database via PostgREST. See
   §7 for details.

### Client Initialisation

`backend/db/client.py` function `get_db()`:

- Called as a singleton — initialised once on first access, cached in
  module-level `_client` variable.
- Checks `os.getenv("SUPABASE_URL")` and
  `os.getenv("SUPABASE_SERVICE_ROLE_KEY")`. If either is missing,
  logs a warning and returns `None`.
- If both are present, calls `supabase.create_client(url, key)` and
  stores the result. Sets `_available = True`.
- If `create_client` raises an exception, logs a warning and returns
  `None`.
- Subsequent calls return the cached client (or `None`).

### Database Warm-Up

`backend/main.py` Phase 1 calls `get_db()` and, if a client is
returned, executes a probe query: `db.table("bot_logs").select("id").limit(1).execute()`.
This verifies the database is reachable and the `bot_logs` table exists.
Failure is non-fatal — the bot continues with in-memory fallback.

---

## 2. Database Overview

### Tables

The database contains exactly **four tables**, all in the `public`
schema:

| Table | Purpose | Primary Readers | Primary Writers | Lifecycle |
|---|---|---|---|---|
| `saved_items` | Stores metadata for every media save operation (forward and deep). Each row represents one saved Telegram message with its origin coordinates, saved location, media classification, tags, and optional caption. | `.preview`, `.send` commands; `GET /api/saves`, `GET /api/saves/{code}` endpoints; `.organize list` (count only) | `.save f`, `.save d` commands (via `insert_save()`); `get_next_save_code()` (count read) | Rows are inserted on save, never updated or deleted by the application. No TTL. Grows indefinitely. |
| `bio_state` | Singleton-per-owner state for the bio cron engine. Stores the template, mood, custom text, active flag, and last-rendered bio string for deduplication. | `.bio show`, `.bio on`, `.organize list` commands; `GET /api/bio` endpoint; bio cron loop (every minute); `main.py` Phase 4 (startup resume check) | `.bio template/text/mood/on/off` commands (via `update_bio_state()`); `get_or_create_bio_state()` (initial insert); bio cron loop (via `update_bio_state()` for `last_bio`) | One row per owner. Created on first `.bio` command. Updated on every bio state change and every successful cron tick. Never deleted. |
| `bot_logs` | Structured activity log. Each row is a discrete bot event with level, message, and JSONB context. | `.organize list` (count only); `GET /api/logs` endpoint | `log()` function — called after every `.save`, `.send` command; `main.py` Phase 1 (warm-up read) | Rows inserted on bot actions. Purged by `.organize clean` (deletes entries older than the configured retention period). Otherwise grows indefinitely. |
| `panel_settings` | Permanent configuration storage for the entire helper panel system. Typed columns with CHECK constraints. Single row (`key='global'`). | Settings panel (inline UI); `GET /api/settings` endpoint; `settings_service` on startup | Settings panel toggle/input handlers (via `settings_service`); `settings_service.load_all()` on startup | One row (`key='global'`). Created by migration. Updated on every settings change. Never deleted. |

---

## 3. Complete Schema

### Migration Files

Migration files in `supabase/migrations/`:

| File | Status | Notes |
|---|---|---|
| `20260712234229_lifeos_schema.sql` | **Superseded** (initial) | Creates tables with CHECK constraints and wide-open RLS (all 4 CRUD policies for anon+authenticated). |
| `20260714111706_create_lifeos_tables.sql` | **Authoritative** | Creates tables with defaults, read-only RLS (SELECT only for anon+authenticated), additional indexes. **Lacks CHECK constraints** present in the initial migration. |
| `20260726143924_create_panel_settings_table.sql` | **Active** | Creates `panel_settings` table with `key`, `auto_close_enabled`, `updated_at`. |
| `20260729221445_expand_panel_settings_typed_columns.sql` | **Active** | Expands `panel_settings` with typed columns: `auto_close_delay_seconds`, `max_deep_save_mb`, `delete_batch_size`, `log_retention_days`, `panel_countdown_interval`, `input_timeout_seconds`. Migrates values from `bot_settings` if it exists. |

Both the initial and authoritative migrations use `CREATE TABLE IF NOT EXISTS`, so if both run in sequence, the
first one creates the tables and the second is a no-op for table
creation. However, the second migration drops and recreates RLS
policies and adds indexes. See §13 for the inconsistency implications.

The schema below documents the **authoritative** migration
(`20260714111706`), with annotations where the initial migration
differs. The `panel_settings` table is documented from its own
migrations.

---

### Table: `saved_items`

Stores metadata for both forward saves and deep saves.

| Column | SQL Type | Nullable | Default | Primary Key | Foreign Key | Unique | Index |
|---|---|---|---|---|---|---|---|
| `id` | `bigserial` | NO | `nextval()` | YES | — | — | (implicit PK index) |
| `save_code` | `text` | NO | — | — | — | YES (`UNIQUE` constraint) | `idx_saved_items_save_code` |
| `save_type` | `text` | NO | `'forward'` | — | — | — | — |
| `origin_chat_id` | `bigint` | YES | — | — | — | — | — |
| `origin_msg_id` | `bigint` | YES | — | — | — | — | — |
| `saved_chat_id` | `bigint` | YES | — | — | — | — | — |
| `saved_msg_id` | `bigint` | YES | — | — | — | — | — |
| `sender_name` | `text` | YES | — | — | — | — | — |
| `sender_id` | `bigint` | YES | — | — | — | — | — |
| `mime_type` | `text` | YES | — | — | — | — | — |
| `file_id` | `text` | YES | — | — | — | — | — |
| `file_size` | `bigint` | YES | — | — | — | — | — |
| `media_type` | `text` | YES | — | — | — | — | — |
| `tags` | `text[]` | YES | `'{}'` (empty array) | — | — | — | — |
| `caption` | `text` | YES | — | — | — | — | — |
| `owner_id` | `bigint` | NO | — | — | — | — | `idx_saved_items_owner` |
| `created_at` | `timestamptz` | YES | `now()` | — | — | — | `idx_saved_items_created_at` (DESC) |

**Indexes:**
- `idx_saved_items_owner` — `saved_items (owner_id)`
- `idx_saved_items_save_code` — `saved_items (save_code)`
- `idx_saved_items_created_at` — `saved_items (created_at DESC)`

**CHECK constraints:**
- **Authoritative migration:** NONE on `save_type`.
- **Initial migration only:** `CHECK (save_type IN ('forward', 'deep'))`
- **INFERRED:** The application code only ever inserts `'forward'` or
  `'deep'`, so the constraint is enforced at the application layer
  even when absent from the schema.

**RLS policies (authoritative migration):**
- `anon_select_saved_items` — `FOR SELECT TO anon, authenticated USING (true)`
- No INSERT, UPDATE, or DELETE policies for anon/authenticated.

**RLS policies (initial migration — superseded):**
- `anon_select_saved_items` — `FOR SELECT TO anon, authenticated USING (true)`
- `anon_insert_saved_items` — `FOR INSERT TO anon, authenticated WITH CHECK (true)`
- `anon_update_saved_items` — `FOR UPDATE TO anon, authenticated USING (true) WITH CHECK (true)`
- `anon_delete_saved_items` — `FOR DELETE TO anon, authenticated USING (true)`

---

### Table: `bio_state`

Singleton-per-owner state for the bio cron engine.

| Column | SQL Type | Nullable | Default | Primary Key | Foreign Key | Unique | Index |
|---|---|---|---|---|---|---|---|
| `id` | `bigserial` | NO | `nextval()` | YES | — | — | (implicit PK index) |
| `owner_id` | `bigint` | NO | — | — | — | YES (`UNIQUE` constraint) | `idx_bio_state_owner` |
| `template` | `text` | NO | `'🕒 {time} \| 💭 {mood}'` | — | — | — | — |
| `mood` | `text` | NO | `'😊'` | — | — | — | — |
| `custom_text` | `text` | NO | `''` (empty string) | — | — | — | — |
| `is_active` | `boolean` | NO | `false` | — | — | — | — |
| `last_bio` | `text` | NO | `''` (empty string) | — | — | — | — |
| `updated_at` | `timestamptz` | YES | `now()` | — | — | — | — |

**Indexes:**
- `idx_bio_state_owner` — `bio_state (owner_id)`

**Note:** The `owner_id` column has both a `UNIQUE` constraint and a
separate index. The UNIQUE constraint already creates an implicit
unique index, so `idx_bio_state_owner` is technically redundant.
**INFERRED:** The redundant index was likely added for explicitness or
by a tool that didn't recognise the implicit index.

**RLS policies (authoritative migration):**
- `anon_select_bio_state` — `FOR SELECT TO anon, authenticated USING (true)`
- No INSERT, UPDATE, or DELETE policies for anon/authenticated.

**RLS policies (initial migration — superseded):**
- All 4 CRUD policies wide open (same pattern as `saved_items`).

**No trigger on `updated_at`:** The `updated_at` column has a default
of `now()` but no trigger to auto-update it on row modification. The
application code manually sets `updated_at` in some update calls
(bio cron writes `"updated_at": datetime.now(tz).isoformat()`) but
not in others (`.bio template`, `.bio text`, `.bio mood`, `.bio on`,
`.bio off` do not include `updated_at` in their update dicts).

---

### Table: `bot_logs`

Structured activity log.

| Column | SQL Type | Nullable | Default | Primary Key | Foreign Key | Unique | Index |
|---|---|---|---|---|---|---|---|
| `id` | `bigserial` | NO | `nextval()` | YES | — | — | (implicit PK index) |
| `owner_id` | `bigint` | NO | — | — | — | — | `idx_bot_logs_owner` |
| `level` | `text` | NO | `'INFO'` | — | — | — | — |
| `message` | `text` | NO | — | — | — | — | — |
| `context` | `jsonb` | YES | `'{}'` (empty JSON object) | — | — | — | — |
| `created_at` | `timestamptz` | YES | `now()` | — | — | — | `idx_bot_logs_created_at` (DESC) |

**Indexes:**
- `idx_bot_logs_owner` — `bot_logs (owner_id)`
- `idx_bot_logs_created_at` — `bot_logs (created_at DESC)`

**CHECK constraints:**
- **Authoritative migration:** NONE on `level`.
- **Initial migration only:** `CHECK (level IN ('INFO', 'WARN', 'ERROR'))`
- **INFERRED:** The application code only ever inserts `'INFO'` (via
  `log()` calls in `save.py` and `retrieve.py`). No `'WARN'` or
  `'ERROR'` level entries are written by the current codebase, though
  the column and initial migration support them.

**RLS policies (authoritative migration):**
- `anon_select_bot_logs` — `FOR SELECT TO anon, authenticated USING (true)`
- No INSERT, UPDATE, or DELETE policies for anon/authenticated.

**RLS policies (initial migration — superseded):**
- All 4 CRUD policies wide open (same pattern as `saved_items`).

---

### Table: `panel_settings`

**Purpose:** Permanent configuration storage for the entire helper panel system. Uses typed columns (not key-value) so every setting has a proper SQL type, a default, and a CHECK constraint. Supabase is the single source of truth — the Settings panel reads every value from the database and writes changes back immediately.

**Cardinality:** Single row (key = `'global'`).

| Column | Type | Default | Constraints | Purpose |
|---|---|---|---|---|
| `key` | text (PK) | — | PRIMARY KEY | Singleton row key — always `'global'` |
| `auto_close_enabled` | boolean | `true` | NOT NULL | Whether inline panels auto-close after the delay |
| `auto_close_delay_seconds` | integer | `120` | NOT NULL, CHECK (10–3600) | Seconds before an inline panel auto-closes |
| `max_deep_save_mb` | integer | `50` | NOT NULL, CHECK (1–500) | Maximum file size in MB for deep saves |
| `delete_batch_size` | integer | `100` | NOT NULL, CHECK (1–1000) | Messages per `delete_messages()` API call |
| `log_retention_days` | integer | `7` | NOT NULL, CHECK (1–365) | Days of logs to retain before cleanup |
| `panel_countdown_interval` | integer | `30` | NOT NULL, CHECK (5–120) | Seconds between countdown re-renders |
| `input_timeout_seconds` | integer | `120` | NOT NULL, CHECK (10–600) | Seconds before a pending input request expires |
| `updated_at` | timestamptz | `now()` | — | Last update timestamp |

**Migration:** Created by `20260726143924_create_panel_settings_table.sql`, expanded by `20260729221445_expand_panel_settings_typed_columns.sql`. Existing installations migrate automatically — no manual editing required. Values from the legacy `bot_settings` key-value table are migrated into the typed columns if they exist.

**RLS policies:**
- SELECT granted to `anon` + `authenticated` (read-only dashboard access).
- All writes go through the backend service-role key (bypasses RLS).

---

## 4. Relationships

### Inter-Table Relationships

There are **no foreign keys** between any tables. All four tables
share a common `owner_id` column (`bigint`), which represents the
Telegram user ID of the bot owner, but this is an application-level
logical relationship, not a database constraint.

### Cardinality

- `saved_items` : `owner_id` — Many-to-one (many saves per owner)
- `bio_state` : `owner_id` — One-to-one (enforced by UNIQUE constraint)
- `bot_logs` : `owner_id` — Many-to-one (many logs per owner)
- `panel_settings` : Single row (`key='global'`), not per-owner

---

## 5. Data Flow

### Feature: `.save f` (Forward Save)

**Tables touched:** `saved_items` (INSERT + count SELECT), `bot_logs` (INSERT)

### Feature: `.save d` (Deep Save)

**Tables touched:** `saved_items` (INSERT + count SELECT), `bot_logs` (INSERT), `panel_settings` (SELECT max_deep_save_mb)

### Feature: `.preview <code>`

**Tables touched:** `saved_items` (SELECT by save_code)

### Feature: `.send <code>`

**Tables touched:** `saved_items` (SELECT by save_code), `bot_logs` (INSERT)

### Feature: `.organize list`

**Tables touched:** `saved_items` (3x count SELECT), `bot_logs` (count SELECT), `bio_state` (SELECT)

### Feature: `.organize clean`

**Tables touched:** `bot_logs` (DELETE with date filter), `panel_settings` (SELECT log_retention_days)

### Feature: `.bio on` / `.bio off` / `.bio template` / `.bio text` / `.bio mood` / `.bio show`

**Tables touched:** `bio_state` (SELECT or INSERT+SELECT, then UPDATE)

### Feature: Bio Cron Loop (background, every minute)

**Tables touched:** `bio_state` (SELECT every tick, UPDATE when bio changes)

### Feature: `.del <n>` / `.del id <msgid>`

**Tables touched:** `panel_settings` (SELECT delete_batch_size)

### Feature: `GET /api/saves`

**Tables touched:** `saved_items` (SELECT paginated + count SELECT)
**Note:** Hardcodes `owner_id=0`.

### Feature: `GET /api/saves/{save_code}`

**Tables touched:** `saved_items` (SELECT by save_code)

### Feature: `GET /api/bio`

**Tables touched:** `bio_state` (SELECT by owner_id)
**Note:** Hardcodes `owner_id=0`.

### Feature: `GET /api/logs`

**Tables touched:** `bot_logs` (SELECT ordered, limited)
**Note:** Hardcodes `owner_id=0`.

### Feature: `GET /api/settings`

**Tables touched:** `panel_settings` (SELECT all)

### Feature: `GET /health`

**Tables touched:** NONE

### Feature: Settings Panel (inline UI)

**Tables touched:** `panel_settings` (SELECT on open, UPDATE on change)

### Feature: Startup — Settings Load

**Tables touched:** `panel_settings` (SELECT all columns for `key='global'`)

---

## 6. Feature Mapping

### Command to Database Object Matrix

| Command | `saved_items` | `bio_state` | `bot_logs` | `panel_settings` |
|---|---|---|---|---|
| `.save f` | INSERT + count SELECT | — | INSERT (log) | — |
| `.save d` | INSERT + count SELECT | — | INSERT (log) | SELECT (max_deep_save_mb) |
| `.preview <code>` | SELECT by save_code | — | — | — |
| `.send <code>` | SELECT by save_code | — | INSERT (log) | — |
| `.organize list` | 3x count SELECT (all/fwd/deep) | SELECT | count SELECT | — |
| `.organize clean` | — | — | DELETE (older than retention) | SELECT (log_retention_days) |
| `.bio on` | — | SELECT or INSERT+SELECT, then UPDATE | — | — |
| `.bio off` | — | SELECT or INSERT+SELECT, then UPDATE | — | — |
| `.bio template <tpl>` | — | SELECT or INSERT+SELECT, then UPDATE | — | — |
| `.bio text <text>` | — | SELECT or INSERT+SELECT, then UPDATE | — | — |
| `.bio mood <mood>` | — | SELECT or INSERT+SELECT, then UPDATE | — | — |
| `.bio show` | — | SELECT or INSERT+SELECT | — | — |
| `.bio help` / `.bio` | — | SELECT or INSERT+SELECT | — | — |
| `.ping` | — | — | — | — |
| `.id` | — | — | — | — |
| `.del <n>` | — | — | — | SELECT (delete_batch_size) |
| `.del id <msgid>` | — | — | — | SELECT (delete_batch_size) |
| `.help` | — | — | — | — |
| Bio cron (background) | — | SELECT (every tick), UPDATE (on bio change) | — | — |
| Startup Phase 1 | — | — | SELECT (warm-up probe) | — |
| Startup Phase 4 | — | SELECT (resume check) | — | — |
| Settings panel (inline) | — | — | — | SELECT + UPDATE |
| Startup (settings load) | — | — | — | SELECT (load_all) |

### API Endpoint to Database Object Matrix

| Endpoint | `saved_items` | `bio_state` | `bot_logs` | `panel_settings` |
|---|---|---|---|---|
| `GET /health` | — | — | — | — |
| `GET /api/saves` | SELECT (paginated) + count SELECT | — | — | — |
| `GET /api/saves/{code}` | SELECT by save_code | — | — | — |
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
| `query_save()` | SELECT by save_code | — | — | — |
| `list_saves()` | SELECT (paginated) + count SELECT | — | — | — |
| `count_saves()` | SELECT (count, optional filter) | — | — | — |
| `get_bio_state()` | — | SELECT by owner_id | — | — |
| `get_or_create_bio_state()` | — | SELECT, then INSERT if not found | — | — |
| `update_bio_state()` | — | UPDATE by owner_id | — | — |
| `count_logs()` | — | — | SELECT (count) | — |
| `list_logs()` | — | — | SELECT (ordered, limited) | — |
| `clean_logs()` | — | — | DELETE (older than cutoff) | — |

---

## 7. Supabase Services

| Service | Status | Details |
|---|---|---|
| **Authentication** | **NOT USED** | No Supabase Auth is used. The bot authenticates to Telegram via Telethon StringSession. The Supabase client uses the service-role key (no user auth). No `supabase.auth` calls exist anywhere in the codebase. The frontend does not use Supabase Auth either — it reads via the backend API. |
| **Storage** | **NOT USED** | No Supabase Storage buckets are used. Media is stored in Telegram's Saved Messages, not in Supabase Storage. The `file_id` column in `saved_items` stores Telegram's internal file reference, not a Supabase Storage path. No `supabase.storage` calls exist. |
| **Buckets** | **NOT USED** | No buckets are created or referenced. |
| **RLS Policies** | **PARTIALLY USED** | RLS is enabled on all four tables. The authoritative migration creates only SELECT policies for `anon` + `authenticated` (read-only dashboard access). All writes go through the service-role key, which bypasses RLS. See §3 for full policy details. |
| **Functions (RPC)** | **NOT USED** | No Supabase RPC functions are defined or called. No `supabase.rpc()` calls exist. |
| **Realtime** | **NOT USED** | No Supabase Realtime subscriptions. The frontend polls the API every 30 seconds via `setInterval` instead. |
| **Edge Functions** | **NOT USED** | No Edge Functions are deployed or referenced. The `supabase/functions/` directory does not exist in the repository. |
| **Database (PostgreSQL)** | **USED** | The core and only Supabase service in use. Four tables accessed via PostgREST. |
| **PostgREST API** | **USED** | All database access is via the REST API through `supabase-py`. |
| **Migrations** | **USED** | Migration files in `supabase/migrations/`. Applied via the Supabase MCP `apply_migration` tool (not the Supabase CLI, which is not supported in this environment). |

---

## 8. Environment Variables

### Database-Related Environment Variables

| Variable | Required | Default | Used By | Purpose |
|---|---|---|---|---|
| `SUPABASE_URL` | No | `""` (empty) | `backend/db/client.py` `_check_available()`, `get_db()` | Supabase project URL. If empty, the bot uses in-memory fallback. Example: `https://xxxxxxxxxxxx.supabase.co` |
| `SUPABASE_SERVICE_ROLE_KEY` | No | `""` (empty) | `backend/db/client.py` `_check_available()`, `get_db()` | Supabase service-role key. Bypasses all RLS. If empty, the bot uses in-memory fallback. |
| `DATABASE_URL` | No | `""` (empty) | `backend/config.py` `load()` only | Loaded into config dict but **never consumed** by any other code. Dead variable. Intended for direct PostgreSQL connection but not implemented. |
| `BOT_OWNER_ID` | **Yes** | — | `backend/config.py`, all handlers via `owner_id` | Telegram numeric user ID of the bot owner. Used as `owner_id` in all DB writes and most reads. **Note:** The web API hardcodes `owner_id=0`, not this value. |

### Frontend-Only Environment Variables

| Variable | Required | Default | Used By | Purpose |
|---|---|---|---|---|
| `VITE_SUPABASE_URL` | No | — | `src/lib/api.ts` (not used) | Declared in AGENTS.md as frontend env. **Not referenced** in the actual `api.ts` code. Dead variable. |
| `VITE_SUPABASE_ANON_KEY` | No | — | `src/lib/api.ts` (not used) | Same — declared but not used. The frontend calls `/api/*` on the backend, which proxies to Supabase. |

### Non-Database Environment Variables (for completeness)

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `API_ID` | **Yes** | — | Telegram API ID |
| `API_HASH` | **Yes** | — | Telegram API hash |
| `SESSION_STRING` | **Yes** | — | Telethon StringSession (headless auth) |
| `TZ` | No | `Asia/Tehran` | Timezone for bio engine |
| `PORT` | No | `8000` | Web server port |
| `BIO_UPDATE_ENABLED` | No | `false` | If `true`, auto-starts bio cron on boot |
| `LOG_LEVEL` | No | `INFO` | Python logging level |
| `GHOST_ROOM_ID` | No | `""` | Unused in current code |
| `DEST_CHANNEL_ID` | No | `""` | Unused in current code |

---

## 9. Required Build Order

### Step 1: Create the Supabase Project

Create a new Supabase project in the Supabase Dashboard. Note the
project URL and the service-role key from Settings → API.

### Step 2: Apply the Authoritative Migration

Run the SQL from `20260714111706_create_lifeos_tables.sql` in the
Supabase SQL Editor. This creates `saved_items`, `bio_state`, `bot_logs`
with correct columns, types, defaults, indexes, and RLS policies.

### Step 2b: Apply the panel_settings Migration

Run the SQL from `20260726143924_create_panel_settings_table.sql`
followed by `20260729221445_expand_panel_settings_typed_columns.sql`.
This creates the `panel_settings` table with all typed columns and CHECK
constraints, and ensures the single `'global'` row exists.

### Step 3: Add Missing CHECK Constraints (Recommended)

- `ALTER TABLE saved_items ADD CONSTRAINT check_save_type CHECK (save_type IN ('forward', 'deep'));`
- `ALTER TABLE bot_logs ADD CONSTRAINT check_level CHECK (level IN ('INFO', 'WARN', 'ERROR'));`

### Step 4: Add `updated_at` Auto-Update Trigger (Recommended)

Create a trigger function that sets `updated_at = now()` and attach it
as a `BEFORE UPDATE` trigger on `bio_state`.

### Step 5: Add GIN Index on `tags` (Optional)

`CREATE INDEX idx_saved_items_tags ON saved_items USING GIN (tags);`

### Step 6: Set Environment Variables

- `SUPABASE_URL` = the Supabase project URL
- `SUPABASE_SERVICE_ROLE_KEY` = the service-role key
- `BOT_OWNER_ID` = the owner's Telegram numeric user ID

### Step 7: Verify

Deploy the application. On startup, Phase 1 logs `[1/5] Database OK`
if the warm-up probe to `bot_logs` succeeds.

---

## 10. Missing Pieces

### Missing Tables

None. The four tables (`saved_items`, `bio_state`, `bot_logs`, `panel_settings`) cover
all current application functionality.

### Missing Columns

None. All columns referenced by the application code are defined in
the migrations.

### Missing Constraints

1. **`CHECK (save_type IN ('forward', 'deep'))`** — present in the
   initial migration, **missing from the authoritative migration**.
2. **`CHECK (level IN ('INFO', 'WARN', 'ERROR'))`** — present in the
   initial migration, **missing from the authoritative migration**.

### Missing Indexes

1. **GIN index on `saved_items.tags`** — the `tags` column is a
   `text[]` array but has no GIN index.

### Missing Triggers

1. **`bio_state.updated_at` auto-update trigger** — no trigger exists
   to automatically set `updated_at` on row modification.

### Missing RLS Policies

1. **No write policies for anon/authenticated** on any table. This is
   **by design** — all writes go through the service-role key.

### Missing Functionality

1. **No `update_save` or `delete_save` function** — saves are immutable.
2. **No `delete_bio_state` function** — bio_state row cannot be deleted.
3. **No `update_log` or `delete_log` function** — only bulk `clean_logs()`.

---

## 11. Assumptions

### FACT (directly verified from source code or SQL)

1. Four tables exist: `saved_items`, `bio_state`, `bot_logs`, `panel_settings`.
2. All tables use `bigserial` primary keys (authoritative migration), except `panel_settings` which uses a text PK.
3. `saved_items.save_code` has a `UNIQUE` constraint.
4. `bio_state.owner_id` has a `UNIQUE` constraint.
5. RLS is enabled on all four tables.
6. The authoritative migration creates only SELECT policies for
   `anon` + `authenticated`.
7. The backend uses the service-role key, which bypasses RLS.
8. `get_next_save_code()` counts all rows in `saved_items` (no
   owner_id filter) and returns `SV-{count+1:06d}`.
9. `get_or_create_bio_state()` does a SELECT then INSERT (not
   atomic).
10. `update_bio_state()` updates by `owner_id` (not by `id`).
11. The web API hardcodes `owner_id=0` for all queries.
12. No foreign keys exist between any tables.
13. No Supabase Auth, Storage, Realtime, Edge Functions, or RPC are
    used.
14. The `supabase-py` client is synchronous (blocks the asyncio event
    loop).
15. The in-memory fallback uses a Python dict with keys
    `saved_items` (list), `bio_state` (dict keyed by owner_id),
    `bot_logs` (list).
16. The initial migration has CHECK constraints that the authoritative
    migration lacks.
17. The initial migration has wide-open CRUD policies that the
    authoritative migration replaces with SELECT-only.
18. `DATABASE_URL` is loaded by `config.py` but never consumed.
19. `VITE_SUPABASE_URL` and `VITE_SUPABASE_ANON_KEY` are not
    referenced in `src/lib/api.ts`.
20. The `tags` column is `text[]` with a default of `'{}'` (empty
    array).
21. The `context` column in `bot_logs` is `jsonb` with a default of
    `'{}'` (empty JSON object).
22. `bio_engine._cron_loop()` reads `bio_state` every minute and
    writes `last_bio` + `updated_at` only when the rendered bio
    changes.
23. `clean_logs()` deletes rows where `created_at < cutoff` (7 days
    ago) and returns the count of deleted rows.
24. The `save_type` column defaults to `'forward'` in the
    authoritative migration.
25. The `level` column defaults to `'INFO'` in the authoritative
    migration.
26. The `panel_settings` table has CHECK constraints on all integer
    columns, enforcing valid ranges.
27. The `panel_settings` table is the single source of truth for all
    helper panel configuration.
28. `settings_service` reads from `panel_settings` typed columns, not
    a key-value store.
29. The `panel_settings` table uses a text primary key (`key='global'`)
    rather than `bigserial`.

### INFERENCE (not directly verified — deduced from code patterns)

1. **INFERRED:** The `idx_bio_state_owner` index is redundant because
   the `UNIQUE` constraint on `owner_id` already creates an implicit
   unique index.
2. **INFERRED:** The `DATABASE_URL` variable was intended for a
   direct PostgreSQL connection that was never implemented.
3. **INFERRED:** The `VITE_SUPABASE_URL` and `VITE_SUPABASE_ANON_KEY`
   frontend env vars were intended for direct frontend-to-Supabase
   access, but the architecture changed to proxy through the backend
   API, making them dead.
4. **INFERRED:** The `GHOST_ROOM_ID` and `DEST_CHANNEL_ID` env vars
   were intended for features that were never implemented.
5. **INFERRED:** The initial migration was created first with
   wide-open RLS for development convenience, then the authoritative
   migration was created to lock down to read-only for production.
6. **INFERRED:** The `owner_id=0` hardcoding in the web API is a
   placeholder — the dashboard was designed for a single-owner bot.
7. **INFERRED:** The `_save_code_lock` (asyncio.Lock) only works
   within a single process. Restarts can produce duplicate or
   non-sequential codes.
8. **INFERRED:** The `SUPABASE_AVAILABLE` flag in `config.py`'s
   return dict was intended to be passed to other modules, but
   `db/client.py` re-checks the env vars independently.

---

## 12. Manual Setup Guide

### 12.1 Create the Project

1. Log into the Supabase Dashboard (supabase.com).
2. Click "New Project".
3. Choose an organization, enter a project name and database password.
4. Select a region close to the Render deployment region.
5. Wait for the project to provision.

### 12.2 Retrieve Credentials

1. Go to Settings → API.
2. Note the **Project URL** — this becomes `SUPABASE_URL`.
3. Note the **service_role key** — this becomes
   `SUPABASE_SERVICE_ROLE_KEY`. Keep this secret; it bypasses all RLS.
4. The **anon key** is not needed by the backend.

### 12.3 Create Tables

Go to the SQL Editor and run the authoritative migration SQL
(`20260714111706_create_lifeos_tables.sql`). Then run the
`panel_settings` migrations (`20260726143924` and `20260729221445`).
After running, verify in the Table Editor that four tables exist:
`saved_items`, `bio_state`, `bot_logs`, `panel_settings`.

### 12.4 Verify RLS

1. Go to Authentication → Policies.
2. Verify that RLS is **enabled** on all four tables.
3. Verify that each table has exactly one policy: a SELECT policy
   for `anon` and `authenticated` with `USING (true)`.
4. Verify that no INSERT, UPDATE, or DELETE policies exist for
   `anon` or `authenticated`.

### 12.5 Add Recommended Constraints (Optional)

Add CHECK constraints on `save_type` and `level` to match the initial
migration's data integrity guarantees. See §9 Step 3.

### 12.6 Add `updated_at` Trigger (Optional)

Create a trigger function and attach it to `bio_state` so that
`updated_at` is automatically set on every UPDATE. See §9 Step 4.

### 12.7 Set Environment Variables in Render

1. Go to Environment.
2. Set `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `BOT_OWNER_ID`.
3. Save and trigger a redeploy.

### 12.8 Verify Deployment

1. Check Render logs for `[1/5] Database OK`.
2. Send `.ping` via Telegram to confirm the bot is running.
3. Send `.save f` to create the first `saved_items` row.
4. Send `.organize list` to verify counts are non-zero.
5. Send `.bio on` to create the `bio_state` row and start the cron.
6. Open the dashboard URL to verify the API returns data.

### 12.9 What Does NOT Need to Be Created

- **No Supabase Auth users** — the project does not use Supabase Auth.
- **No Storage buckets** — media is stored in Telegram, not Supabase.
- **No Edge Functions** — none are deployed or referenced.
- **No RPC functions** — none are defined or called.
- **No Realtime subscriptions** — the frontend polls via HTTP.
- **No additional schemas** — everything is in the `public` schema.

---

## 13. Risk Analysis

### R-1: Save Code Generation is Not Atomic Across Restarts

**Severity:** High

`get_next_save_code()` counts existing rows and returns `SV-{count+1:06d}`.
The `asyncio.Lock` prevents concurrent saves within a single process,
but restarts between count read and insert can cause duplicate codes.

**Fix direction:** Use a PostgreSQL sequence or a dedicated counter table.

### R-2: `get_or_create_bio_state()` Race Condition

**Severity:** Medium

SELECT then INSERT is not atomic. Concurrent calls can cause UNIQUE
constraint violations on `owner_id`.

**Fix direction:** Use an UPSERT (`INSERT ... ON CONFLICT DO NOTHING`).

### R-3: Web API Hardcodes `owner_id=0`

**Severity:** Medium

All API endpoints pass `owner_id=0`, making the dashboard useless for
any owner whose ID is not `0`.

### R-4: No `updated_at` Auto-Update Trigger on `bio_state`

**Severity:** Low

The `updated_at` column is not auto-updated on modification. Manual
state changes do not include `updated_at` in their update dicts.

### R-5: Missing CHECK Constraints in Authoritative Migration

**Severity:** Low

The authoritative migration lacks CHECK constraints on `save_type`
and `level` that were present in the initial migration.

### R-6: Synchronous Supabase Calls Block the Event Loop

**Severity:** Medium

The `supabase-py` client blocks the asyncio event loop during HTTP
round-trips.

**Fix direction:** Use `supabase-py` async client or run in a thread
executor.

### R-7: No GIN Index on `tags` Array

**Severity:** Low

The `tags` column is `text[]` but has no GIN index. Tag-based queries
would require a full table scan.

### R-8: Two Conflicting Migrations

**Severity:** Medium

Two migration files exist with different schemas and RLS policies.
If both run in sequence, the initial migration's CHECK constraints
persist while the authoritative migration's RLS policies override.
If only the authoritative migration is applied, CHECK constraints
are missing.

### R-9: No Data Retention Policy for `saved_items`

**Severity:** Low

`saved_items` rows are never deleted. The table grows indefinitely.

### R-10: `saved_items` Count Includes All Owners

**Severity:** Low

`get_next_save_code()` counts ALL rows regardless of `owner_id`. In a
multi-owner scenario, save codes would be shared across owners.

### R-11: RLS SELECT Policies Are Fully Open

**Severity:** Medium

All four tables have `SELECT ... USING (true)` policies for
`anon` + `authenticated`. Anyone with the anon key can read all data.

### R-12: `clean_logs()` Return Count May Be Unreliable

**Severity:** Low

`clean_logs()` returns `len(result.data)` which may be `None` or empty
depending on the `Prefer: return=representation` header.

### Summary of Risks by Severity

| Severity | Count | IDs |
|---|---|---|
| High | 1 | R-1 |
| Medium | 4 | R-2, R-3, R-6, R-8, R-11 |
| Low | 6 | R-4, R-5, R-7, R-9, R-10, R-12 |

---

### End of Document

This document reflects the state of the repository as of the
`expand_panel_settings_typed_columns` migration. If the codebase changes
in ways that invalidate any section above, update this document.
