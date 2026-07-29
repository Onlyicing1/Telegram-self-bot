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
| `20260714111706_create_lifeos_tables.sql` | **Authoritative** | Creates tables with defaults, read-only RLS (SELECT only for anon+authenticated), additional indexes. |
| `20260726143924_create_panel_settings_table.sql` | **Active** | Creates `panel_settings` table with `key`, `auto_close_enabled`, `updated_at`. |
| `expand_panel_settings_typed_columns` | **Active** | Expands `panel_settings` with typed columns: `auto_close_delay_seconds`, `max_deep_save_mb`, `delete_batch_size`, `log_retention_days`, `panel_countdown_interval`, `input_timeout_seconds`. Migrates values from `bot_settings` if it exists. |

The schema below documents the **authoritative** migration
(`20260714111706`), with annotations where the initial migration
differs.

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

**RLS policies (authoritative migration):**
- `anon_select_saved_items` — `FOR SELECT TO anon, authenticated USING (true)`
- No INSERT, UPDATE, or DELETE policies for anon/authenticated.

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

**RLS policies (authoritative migration):**
- `anon_select_bio_state` — `FOR SELECT TO anon, authenticated USING (true)`
- No INSERT, UPDATE, or DELETE policies for anon/authenticated.

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

**RLS policies (authoritative migration):**
- `anon_select_bot_logs` — `FOR SELECT TO anon, authenticated USING (true)`
- No INSERT, UPDATE, or DELETE policies for anon/authenticated.

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

**Migration:** Created by `20260726143924_create_panel_settings_table.sql`, expanded by `expand_panel_settings_typed_columns`. Existing installations migrate automatically — no manual editing required. Values from the legacy `bot_settings` key-value table are migrated into the typed columns if they exist.

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

**Tables touched:** `saved_items` (INSERT + count SELECT), `bot_logs` (INSERT)

### Feature: `.preview <code>`

**Tables touched:** `saved_items` (SELECT by save_code)

### Feature: `.send <code>`

**Tables touched:** `saved_items` (SELECT by save_code), `bot_logs` (INSERT)

### Feature: `.organize list`

**Tables touched:** `saved_items` (3x count SELECT), `bot_logs` (count SELECT), `bio_state` (SELECT)

### Feature: `.organize clean`

**Tables touched:** `bot_logs` (DELETE with date filter, using `log_retention_days` from `panel_settings`)

### Feature: `.bio on` / `.bio off` / `.bio template` / `.bio text` / `.bio mood` / `.bio show`

**Tables touched:** `bio_state` (SELECT or INSERT+SELECT, then UPDATE)

### Feature: Bio Cron Loop (background, every minute)

**Tables touched:** `bio_state` (SELECT every tick, UPDATE when bio changes)

### Feature: Settings Panel (inline UI)

**Tables touched:** `panel_settings` (SELECT on open, UPDATE on change)

### Feature: Startup — Settings Load

**Tables touched:** `panel_settings` (SELECT all columns for `key='global'`)

---

## 6. Feature Mapping

### Command → Database Object Matrix

| Command | `saved_items` | `bio_state` | `bot_logs` | `panel_settings` |
|---|---|---|---|---|
| `.save f` | INSERT + count SELECT | — | INSERT (log) | — |
| `.save d` | INSERT + count SELECT | — | INSERT (log) | SELECT (max_deep_save_mb) |
| `.preview <code>` | SELECT by save_code | — | — | — |
| `.send <code>` | SELECT by save_code | — | INSERT (log) | — |
| `.organize list` | 3x count SELECT | SELECT | count SELECT | — |
| `.organize clean` | — | — | DELETE | SELECT (log_retention_days) |
| `.bio on/off/template/text/mood` | — | SELECT or INSERT+SELECT, then UPDATE | — | — |
| `.del <n>` / `.del id` | — | — | — | SELECT (delete_batch_size) |
| Settings panel (inline) | — | — | — | SELECT + UPDATE |
| Bio cron (background) | — | SELECT (every tick), UPDATE (on bio change) | — | — |
| Startup Phase 1 | — | — | SELECT (warm-up probe) | — |
| Startup (settings load) | — | — | — | SELECT (load_all) |

### API Endpoint → Database Object Matrix

| Endpoint | `saved_items` | `bio_state` | `bot_logs` | `panel_settings` |
|---|---|---|---|---|
| `GET /health` | — | — | — | — |
| `GET /api/saves` | SELECT (paginated) + count SELECT | — | — | — |
| `GET /api/saves/{code}` | SELECT by save_code | — | — | — |
| `GET /api/bio` | — | SELECT by owner_id | — | — |
| `GET /api/settings` | — | — | — | SELECT all |
| `GET /api/logs` | — | — | SELECT (ordered, limited) | — |

---

## 7. Supabase Services

| Service | Status | Details |
|---|---|---|
| **Authentication** | **NOT USED** | No Supabase Auth is used. |
| **Storage** | **NOT USED** | No Supabase Storage buckets are used. |
| **RLS Policies** | **PARTIALLY USED** | RLS is enabled on all four tables. The authoritative migration creates only SELECT policies for `anon` + `authenticated` (read-only dashboard access). All writes go through the service-role key, which bypasses RLS. |
| **Database (PostgreSQL)** | **USED** | The core and only Supabase service in use. Four tables accessed via PostgREST. |
| **PostgREST API** | **USED** | All database access is via the REST API through `supabase-py`. |
| **Migrations** | **USED** | Migration files in `supabase/migrations/`. Applied via the Supabase MCP `apply_migration` tool. |

---

## 8. Environment Variables

### Database-Related Environment Variables

| Variable | Required | Default | Used By | Purpose |
|---|---|---|---|---|
| `SUPABASE_URL` | No | `""` | `backend/db/client.py` | Supabase project URL. If empty, in-memory fallback. |
| `SUPABASE_SERVICE_ROLE_KEY` | No | `""` | `backend/db/client.py` | Supabase service-role key. Bypasses all RLS. If empty, in-memory fallback. |
| `BOT_OWNER_ID` | **Yes** | — | All handlers via `owner_id` | Telegram numeric user ID of the bot owner. |

---

## 9. Required Build Order

### Step 1: Create the Supabase Project

Create a new Supabase project in the Supabase Dashboard. Note the
project URL and the service-role key from Settings → API.

### Step 2: Apply the Authoritative Migration

Run the SQL from `20260714111706_create_lifeos_tables.sql` in the
Supabase SQL Editor. This creates `saved_items`, `bio_state`, `bot_logs`
with correct columns, types, defaults, indexes, and RLS policies.

### Step 3: Apply the panel_settings Migration

Run the SQL from `20260726143924_create_panel_settings_table.sql`
followed by the `expand_panel_settings_typed_columns` migration. This
creates the `panel_settings` table with all typed columns and CHECK
constraints.

### Step 4: Set Environment Variables

- `SUPABASE_URL` = the Supabase project URL
- `SUPABASE_SERVICE_ROLE_KEY` = the service-role key
- `BOT_OWNER_ID` = the owner's Telegram numeric user ID

### Step 5: Verify

Deploy the application. On startup, Phase 1 logs `[1/5] Database OK`
if the warm-up probe to `bot_logs` succeeds. The settings_service
loads all settings from `panel_settings` on startup.

---

## 10. Missing Pieces

### Missing Tables

None. The four tables (`saved_items`, `bio_state`, `bot_logs`, `panel_settings`) cover
all current application functionality.

### Missing Columns

None. All columns referenced by the application code are defined in
the migrations.

---

## 11. Assumptions

### FACT (directly verified from source code or SQL)

1. Four tables exist: `saved_items`, `bio_state`, `bot_logs`, `panel_settings`.
2. All tables use `bigserial` primary keys (except `panel_settings` which uses `text` PK).
3. `saved_items.save_code` has a `UNIQUE` constraint.
4. `bio_state.owner_id` has a `UNIQUE` constraint.
5. RLS is enabled on all four tables.
6. The backend uses the service-role key, which bypasses RLS.
7. `get_next_save_code()` counts all rows in `saved_items` (no owner_id filter).
8. The web API hardcodes `owner_id=0` for all queries.
9. No foreign keys exist between any tables.
10. No Supabase Auth, Storage, Realtime, Edge Functions, or RPC are used.
11. The `panel_settings` table has CHECK constraints on all integer columns.
12. The `panel_settings` table is the single source of truth for all helper panel configuration.
13. `settings_service` reads from `panel_settings` typed columns, not a key-value store.

---

## 12. Manual Setup Guide

### 12.1 Create the Project

1. Log into the Supabase Dashboard.
2. Click "New Project".
3. Choose an organization, enter a project name and database password.
4. Wait for the project to provision.

### 12.2 Retrieve Credentials

1. Go to Settings → API.
2. Note the **Project URL** — this becomes `SUPABASE_URL`.
3. Note the **service_role key** — this becomes `SUPABASE_SERVICE_ROLE_KEY`.

### 12.3 Create Tables

Go to the SQL Editor and run the authoritative migration SQL
(`20260714111706_create_lifeos_tables.sql`), then the
`20260726143924_create_panel_settings_table.sql` migration, then the
`expand_panel_settings_typed_columns` migration.

After running, verify in the Table Editor that four tables exist:
`saved_items`, `bio_state`, `bot_logs`, `panel_settings`.

### 12.4 Verify RLS

1. Go to Authentication → Policies.
2. Verify that RLS is **enabled** on all four tables.
3. Verify that each table has exactly one policy: a SELECT policy
   for `anon` and `authenticated` with `USING (true)`.
4. Verify that no INSERT, UPDATE, or DELETE policies exist for
   `anon` or `authenticated` (writes are service-role only).

### 12.5 Set Environment Variables in Render

1. Go to Environment in the Render Dashboard.
2. Set `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `BOT_OWNER_ID`.
3. Save and trigger a redeploy.

### 12.6 Verify Deployment

1. Check Render logs for `[1/5] Database OK`.
2. Send `.ping` via Telegram to confirm the bot is running.
3. Send `.save f` to create the first `saved_items` row.
4. Open the dashboard URL to verify the API returns data.

---

## 13. Risk Analysis

### R-1: Save Code Generation is Not Atomic Across Restarts

**Severity:** High

`get_next_save_code()` counts existing rows and returns `SV-{count+1:06d}`.
The `asyncio.Lock` prevents concurrent saves within a single process,
but restarts between count read and insert can cause duplicate codes.

### R-2: `get_or_create_bio_state()` Race Condition

**Severity:** Medium

SELECT then INSERT is not atomic. Concurrent calls can cause UNIQUE
constraint violations on `owner_id`.

### R-3: Web API Hardcodes `owner_id=0`

**Severity:** Medium

All API endpoints pass `owner_id=0`, making the dashboard useless for
any owner whose ID is not `0`.

### R-4: No `updated_at` Auto-Update Trigger on `bio_state`

**Severity:** Low

The `updated_at` column is not auto-updated on modification.

### R-5: Synchronous Supabase Calls Block the Event Loop

**Severity:** Medium

The `supabase-py` client blocks the asyncio event loop.

### R-6: RLS SELECT Policies Are Fully Open

**Severity:** Medium

All four tables have `SELECT ... USING (true)` policies, meaning
anyone with the anon key can read all data.

### R-7: No Data Retention Policy for `saved_items`

**Severity:** Low

`saved_items` rows are never deleted. The table grows indefinitely.

---

### End of Document

This document reflects the state of the repository as of the
`expand_panel_settings_typed_columns` migration. If the codebase
changes in ways that invalidate any section above, update this
document.
