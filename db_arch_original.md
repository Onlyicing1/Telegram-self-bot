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

The database contains exactly **three tables**, all in the `public`
schema:

| Table | Purpose | Primary Readers | Primary Writers | Lifecycle |
|---|---|---|---|---|
| `saved_items` | Stores metadata for every media save operation (forward and deep). Each row represents one saved Telegram message with its origin coordinates, saved location, media classification, tags, and optional caption. | `.preview`, `.send` commands; `GET /api/saves`, `GET /api/saves/{code}` endpoints; `.organize list` (count only) | `.save f`, `.save d` commands (via `insert_save()`); `get_next_save_code()` (count read) | Rows are inserted on save, never updated or deleted by the application. No TTL. Grows indefinitely. |
| `bio_state` | Singleton-per-owner state for the bio cron engine. Stores the template, mood, custom text, active flag, and last-rendered bio string for deduplication. | `.bio show`, `.bio on`, `.organize list` commands; `GET /api/bio` endpoint; bio cron loop (every minute); `main.py` Phase 4 (startup resume check) | `.bio template/text/mood/on/off` commands (via `update_bio_state()`); `get_or_create_bio_state()` (initial insert); bio cron loop (via `update_bio_state()` for `last_bio`) | One row per owner. Created on first `.bio` command. Updated on every bio state change and every successful cron tick. Never deleted. |
| `bot_logs` | Structured activity log. Each row is a discrete bot event with level, message, and JSONB context. | `.organize list` (count only); `GET /api/logs` endpoint | `log()` function — called after every `.save`, `.send` command; `main.py` Phase 1 (warm-up read) | Rows inserted on bot actions. Purged by `.organize clean` (deletes entries older than 7 days). Otherwise grows indefinitely. |

---

## 3. Complete Schema

### Migration Files

Two migration files exist in `supabase/migrations/`:

| File | Status | Notes |
|---|---|---|
| `20260712234229_lifeos_schema.sql` | **Superseded** (initial) | Creates tables with CHECK constraints and wide-open RLS (all 4 CRUD policies for anon+authenticated). |
| `20260714111706_create_lifeos_tables.sql` | **Authoritative** | Creates tables with defaults, read-only RLS (SELECT only for anon+authenticated), additional indexes. **Lacks CHECK constraints** present in the initial migration. |

Both use `CREATE TABLE IF NOT EXISTS`, so if both run in sequence, the
first one creates the tables and the second is a no-op for table
creation. However, the second migration drops and recreates RLS
policies and adds indexes. See §13 for the inconsistency implications.

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

## 4. Relationships

### Inter-Table Relationships

There are **no foreign keys** between any tables. All three tables
share a common `owner_id` column (`bigint`), which represents the
Telegram user ID of the bot owner, but this is an application-level
logical relationship, not a database constraint.

```
┌─────────────────────┐     ┌─────────────────────┐     ┌─────────────────────┐
│    saved_items      │     │     bio_state       │     │     bot_logs        │
├─────────────────────┤     ├─────────────────────┤     ├─────────────────────┤
│ id (PK)             │     │ id (PK)             │     │ id (PK)             │
│ save_code (UNIQUE)  │     │ owner_id (UNIQUE)   │     │ owner_id            │
│ save_type           │     │ template            │     │ level               │
│ origin_chat_id      │     │ mood                │     │ message             │
│ origin_msg_id       │     │ custom_text         │     │ context (JSONB)     │
│ saved_chat_id       │     │ is_active           │     │ created_at          │
│ saved_msg_id        │     │ last_bio            │     └─────────────────────┘
│ sender_name         │     │ updated_at          │
│ sender_id           │     └─────────────────────┘
│ mime_type           │
│ file_id             │      No foreign keys exist.
│ file_size           │      owner_id is a logical soft-link,
│ media_type          │      not a database constraint.
│ tags (text[])       │
│ caption             │      saved_items.owner_id  ───┐
│ owner_id            ───┼──→  (soft link to owner)   ├── bio_state.owner_id
│ created_at          │      bot_logs.owner_id   ───┘
└─────────────────────┘
```

### Dependency Diagram (Logical)

```
                    owner_id (Telegram user ID)
                           │
           ┌───────────────┼───────────────┐
           │               │               │
           ▼               ▼               ▼
     saved_items      bio_state       bot_logs
     (media saves)    (cron state)    (activity log)
           │               │               │
           │               │               │
     No dependencies  No dependencies  No dependencies
     on other tables  on other tables  on other tables
```

### Cardinality

- `saved_items` : `owner_id` — Many-to-one (many saves per owner)
- `bio_state` : `owner_id` — One-to-one (enforced by UNIQUE constraint)
- `bot_logs` : `owner_id` — Many-to-one (many logs per owner)

---

## 5. Data Flow

### Feature: `.save f` (Forward Save)

```
Telegram (owner sends ".save f" replying to a message)
    │
    ▼
save.py: save_cmd(event)
    │
    ├── is_owner(event, owner_id) → reject if not owner
    ├── reply = await event.message.get_reply_message()
    ├── save_code = await db_client.get_next_save_code()
    │       │
    │       └── db_client.get_next_save_code()
    │               ├── Acquire asyncio.Lock (_save_code_lock)
    │               ├── db.table("saved_items").select("id", count="exact").execute()
    │               ├── count = result.count
    │               └── return f"SV-{count+1:06d}"
    │
    ├── Extract metadata from reply (sender, chat_id, msg_id, media, mime, file_size, file_name, file_id)
    ├── Build tags (#saved, #saved_<type>, #saved_<year>_<month>_<day>)
    ├── client.forward_messages("me", reply)  ← Telegram API
    ├── db_client.insert_save({...})
    │       │
    │       └── db_client.insert_save(data)
    │               ├── db.table("saved_items").insert(data).execute()
    │               └── Return inserted row
    │
    ├── event.edit("📌 Forward Saved SV-NNNNNN")
    └── db_client.log(owner_id, "INFO", "Saved F SV-NNNNNN", {...})
            │
            └── db.table("bot_logs").insert(entry).execute()
```

**Tables touched:** `saved_items` (INSERT + count SELECT), `bot_logs` (INSERT)

### Feature: `.save d` (Deep Save)

```
Telegram (owner sends ".save d" replying to a message with media)
    │
    ▼
save.py: save_cmd(event)
    │
    ├── is_owner() check
    ├── reply = await event.message.get_reply_message()
    ├── save_code = await db_client.get_next_save_code()  ← saved_items count
    ├── Extract metadata
    ├── Check media exists → error if not
    ├── Check file_size ≤ 50MB → error if exceeds
    ├── Build caption (rich multi-line with all metadata)
    ├── event.edit("⬇️ Downloading…")
    ├── buf = io.BytesIO()
    ├── try:
    │     ├── client.download_media(reply, file=buf)  ← Telegram API
    │     ├── Check buf not empty → error if zero
    │     ├── buf.seek(0); buf.name = filename
    │     └── client.send_file("me", buf, caption, force_document=...)  ← Telegram API
    ├── finally: buf.close()
    ├── db_client.insert_save({...})  ← saved_items INSERT
    ├── event.edit("✅ Deep Saved SV-NNNNNN")
    └── db_client.log(owner_id, "INFO", "Saved D SV-NNNNNN", {...})  ← bot_logs INSERT
```

**Tables touched:** `saved_items` (INSERT + count SELECT), `bot_logs` (INSERT)

### Feature: `.preview <code>`

```
Telegram (owner sends ".preview SV-000001")
    │
    ▼
retrieve.py: preview(event)
    │
    ├── is_owner() check
    ├── save_code = event.pattern_match.group(1).upper()
    ├── row = db_client.query_save(save_code)
    │       │
    │       └── db_client.query_save(save_code)
    │               ├── db.table("saved_items").select("*")
    │               │       .eq("save_code", save_code.upper())
    │               │       .maybe_single()
    │               │       .execute()
    │               └── Return result.data (dict or None)
    │
    └── event.edit(_format_preview(row))  ← formatted metadata display
```

**Tables touched:** `saved_items` (SELECT by save_code)

### Feature: `.send <code>`

```
Telegram (owner sends ".send SV-000001")
    │
    ▼
retrieve.py: send_cmd(event)
    │
    ├── is_owner() check
    ├── save_code = uppercased
    ├── row = db_client.query_save(save_code)  ← saved_items SELECT
    ├── Extract saved_chat_id, saved_msg_id from row
    ├── client.forward_messages(target_chat, saved_msg_id, saved_chat_id)  ← Telegram API
    ├── event.delete()  ← remove command message
    └── db_client.log(owner_id, "INFO", "Sent SV-NNNNNN to {chat}", {...})  ← bot_logs INSERT
```

**Tables touched:** `saved_items` (SELECT by save_code), `bot_logs` (INSERT)

### Feature: `.organize list`

```
Telegram (owner sends ".organize list")
    │
    ▼
organize.py: organize(event)
    │
    ├── is_owner() check
    ├── total = db_client.count_saves(owner_id)          ← saved_items count (all)
    ├── fwd   = db_client.count_saves(owner_id, "forward") ← saved_items count (filtered)
    ├── deep  = db_client.count_saves(owner_id, "deep")    ← saved_items count (filtered)
    ├── logs  = db_client.count_logs(owner_id)           ← bot_logs count
    ├── bio   = db_client.get_bio_state(owner_id)        ← bio_state SELECT
    └── event.edit(formatted status display)
```

**Tables touched:** `saved_items` (3x count SELECT), `bot_logs` (count SELECT), `bio_state` (SELECT)

### Feature: `.organize clean`

```
Telegram (owner sends ".organize clean")
    │
    ▼
organize.py: organize(event)
    │
    ├── is_owner() check
    ├── deleted = db_client.clean_logs(owner_id, days=7)
    │       │
    │       └── db_client.clean_logs(owner_id, days)
    │               ├── cutoff = now() - 7 days
    │               ├── db.table("bot_logs").delete()
    │               │       .eq("owner_id", owner_id)
    │               │       .lt("created_at", cutoff)
    │               │       .execute()
    │               └── Return len(result.data)
    │
    └── event.edit("🧹 Cleaned N log entries older than 7 days.")
```

**Tables touched:** `bot_logs` (DELETE with date filter)

### Feature: `.bio on`

```
Telegram (owner sends ".bio on")
    │
    ▼
bio.py: bio_cmd(event)
    │
    ├── is_owner() check
    ├── state = db_client.get_or_create_bio_state(owner_id)
    │       │
    │       └── db_client.get_or_create_bio_state(owner_id)
    │               ├── get_bio_state(owner_id)  ← bio_state SELECT
    │               ├── If not found:
    │               │     ├── db.table("bio_state").insert(default).execute()  ← bio_state INSERT
    │               │     └── db.table("bio_state").select("*").eq("owner_id",...).maybe_single().execute()
    │               └── Return state dict
    │
    ├── db_client.update_bio_state(owner_id, {"is_active": True})  ← bio_state UPDATE
    ├── bio_engine.start_cron(client, owner_id, tz_str)
    └── event.edit("✅ Bio cron ON, Preview: ...")
```

**Tables touched:** `bio_state` (SELECT or INSERT+SELECT, then UPDATE)

### Feature: `.bio off`

```
Telegram (owner sends ".bio off")
    │
    ▼
bio.py: bio_cmd(event)
    │
    ├── is_owner() check
    ├── state = db_client.get_or_create_bio_state(owner_id)  ← bio_state SELECT/INSERT
    ├── db_client.update_bio_state(owner_id, {"is_active": False})  ← bio_state UPDATE
    ├── bio_engine.stop_cron()
    └── event.edit("⏹ Bio cron OFF")
```

**Tables touched:** `bio_state` (SELECT or INSERT+SELECT, then UPDATE)

### Feature: `.bio template <tpl>`

```
Telegram → bio.py → get_or_create_bio_state() → bio_state SELECT/INSERT
         → update_bio_state({"template": new_tpl}) → bio_state UPDATE
         → event.edit("✅ Template updated")
```

**Tables touched:** `bio_state` (SELECT or INSERT+SELECT, then UPDATE)

### Feature: `.bio text <text>`

```
Telegram → bio.py → get_or_create_bio_state() → bio_state SELECT/INSERT
         → update_bio_state({"custom_text": val}) → bio_state UPDATE
         → event.edit("✅ Text set")
```

**Tables touched:** `bio_state` (SELECT or INSERT+SELECT, then UPDATE)

### Feature: `.bio mood <mood>`

```
Telegram → bio.py → get_or_create_bio_state() → bio_state SELECT/INSERT
         → update_bio_state({"mood": val}) → bio_state UPDATE
         → event.edit("✅ Mood set")
```

**Tables touched:** `bio_state` (SELECT or INSERT+SELECT, then UPDATE)

### Feature: `.bio show`

```
Telegram → bio.py → get_or_create_bio_state() → bio_state SELECT/INSERT
         → render_bio(template, mood, text, tz) → (no DB)
         → event.edit(full state display)
```

**Tables touched:** `bio_state` (SELECT or INSERT+SELECT)

### Feature: `.bio help` / `.bio` (no args)

```
Telegram → bio.py → get_or_create_bio_state() → bio_state SELECT/INSERT
         → event.edit(_HELP text)
```

**Tables touched:** `bio_state` (SELECT or INSERT+SELECT)

### Feature: Bio Cron Loop (background, every minute)

```
bio_engine._cron_loop(client, owner_id, tz_str)
    │
    ├── sleep until next minute boundary
    ├── state = db_client.get_bio_state(owner_id)  ← bio_state SELECT
    ├── if not state or not is_active → return (stop loop)
    ├── new_bio = render_bio(template, mood, text, tz)
    ├── if new_bio == state["last_bio"] → continue (dedup, skip)
    ├── await client.edit_profile(about=new_bio)  ← Telegram API
    ├── except FloodWaitError → sleep, continue
    ├── db_client.update_bio_state(owner_id, {
    │       "last_bio": new_bio,
    │       "updated_at": now.isoformat()
    │   })  ← bio_state UPDATE
    └── repeat
```

**Tables touched:** `bio_state` (SELECT every tick, UPDATE when bio changes)

### Feature: Startup — Bio Cron Resume

```
main.py: main() Phase 4
    │
    ├── state = db_client.get_bio_state(cfg["OWNER_ID"])  ← bio_state SELECT
    ├── if state and state["is_active"] → bio_engine.start_cron()
    ├── elif cfg["BIO_UPDATE_ENABLED"] → bio_engine.start_cron()
    └── else → skip
```

**Tables touched:** `bio_state` (SELECT)

### Feature: Startup — Database Warm-Up

```
main.py: main() Phase 1
    │
    ├── db = db_client.get_db()
    ├── if db:
    │     └── db.table("bot_logs").select("id").limit(1).execute()  ← bot_logs SELECT
    └── else: log "using in-memory fallback"
```

**Tables touched:** `bot_logs` (SELECT limit 1 — probe only)

### Feature: `.ping`

```
Telegram → misc.py → is_owner() check → event.edit("PONG")
```

**Tables touched:** NONE

### Feature: `.id`

```
Telegram → misc.py → is_owner() check → event.edit(chat_id + msg_id info)
```

**Tables touched:** NONE

### Feature: `.del <n>` / `.del id <msgid>`

```
Telegram → delete.py → is_owner() check → client.delete_messages() (Telegram API only)
```

**Tables touched:** NONE

### Feature: `GET /api/saves`

```
HTTP GET /api/saves?limit=50&offset=0
    │
    ▼
web/app.py: list_saves(limit, offset)
    │
    └── db_client.list_saves(0, limit, offset)
            ├── db.table("saved_items").select("*")
            │       .eq("owner_id", 0)
            │       .order("created_at", desc=True)
            │       .range(offset, offset+limit-1)
            │       .execute()
            └── db.table("saved_items").select("id", count="exact")
                    .eq("owner_id", 0)
                    .execute()
```

**Tables touched:** `saved_items` (SELECT paginated + count SELECT)
**Note:** Hardcodes `owner_id=0`.

### Feature: `GET /api/saves/{save_code}`

```
HTTP GET /api/saves/SV-000001
    │
    ▼
web/app.py: get_save(save_code)
    │
    └── db_client.query_save(save_code)
            └── db.table("saved_items").select("*")
                    .eq("save_code", save_code.upper())
                    .maybe_single()
                    .execute()
```

**Tables touched:** `saved_items` (SELECT by save_code)

### Feature: `GET /api/bio`

```
HTTP GET /api/bio
    │
    ▼
web/app.py: get_bio()
    │
    └── db_client.get_bio_state(0)
            └── db.table("bio_state").select("*")
                    .eq("owner_id", 0)
                    .maybe_single()
                    .execute()
```

**Tables touched:** `bio_state` (SELECT by owner_id)
**Note:** Hardcodes `owner_id=0`.

### Feature: `GET /api/logs`

```
HTTP GET /api/logs?limit=100
    │
    ▼
web/app.py: get_logs(limit)
    │
    └── db_client.list_logs(0, limit)
            └── db.table("bot_logs").select("*")
                    .eq("owner_id", 0)
                    .order("created_at", desc=True)
                    .limit(limit)
                    .execute()
```

**Tables touched:** `bot_logs` (SELECT ordered, limited)
**Note:** Hardcodes `owner_id=0`.

### Feature: `GET /health`

```
HTTP GET /health → return {"status": "ok"}
```

**Tables touched:** NONE

---

## 6. Feature Mapping

### Command → Database Object Matrix

| Command | `saved_items` | `bio_state` | `bot_logs` |
|---|---|---|---|
| `.save f` | INSERT + count SELECT | — | INSERT (log) |
| `.save d` | INSERT + count SELECT | — | INSERT (log) |
| `.preview <code>` | SELECT by save_code | — | — |
| `.send <code>` | SELECT by save_code | — | INSERT (log) |
| `.organize list` | 3x count SELECT (all/fwd/deep) | SELECT | count SELECT |
| `.organize clean` | — | — | DELETE (older than 7 days) |
| `.bio on` | — | SELECT or INSERT+SELECT, then UPDATE | — |
| `.bio off` | — | SELECT or INSERT+SELECT, then UPDATE | — |
| `.bio template <tpl>` | — | SELECT or INSERT+SELECT, then UPDATE | — |
| `.bio text <text>` | — | SELECT or INSERT+SELECT, then UPDATE | — |
| `.bio mood <mood>` | — | SELECT or INSERT+SELECT, then UPDATE | — |
| `.bio show` | — | SELECT or INSERT+SELECT | — |
| `.bio help` / `.bio` | — | SELECT or INSERT+SELECT | — |
| `.ping` | — | — | — |
| `.id` | — | — | — |
| `.del <n>` | — | — | — |
| `.del id <msgid>` | — | — | — |
| `.help` | — | — | — |
| Bio cron (background) | — | SELECT (every tick), UPDATE (on bio change) | — |
| Startup Phase 1 | — | — | SELECT (warm-up probe) |
| Startup Phase 4 | — | SELECT (resume check) | — |

### API Endpoint → Database Object Matrix

| Endpoint | `saved_items` | `bio_state` | `bot_logs` |
|---|---|---|---|
| `GET /health` | — | — | — |
| `GET /api/saves` | SELECT (paginated) + count SELECT | — | — |
| `GET /api/saves/{code}` | SELECT by save_code | — | — |
| `GET /api/bio` | — | SELECT by owner_id | — |
| `GET /api/logs` | — | — | SELECT (ordered, limited) |

### `db/client.py` Function → Table Matrix

| Function | `saved_items` | `bio_state` | `bot_logs` |
|---|---|---|---|
| `get_db()` | — | — | — (client init) |
| `is_available()` | — | — | — (status check) |
| `log()` | — | — | INSERT |
| `get_next_save_code()` | SELECT (count) | — | — |
| `insert_save()` | INSERT | — | — |
| `query_save()` | SELECT by save_code | — | — |
| `list_saves()` | SELECT (paginated) + count SELECT | — | — |
| `count_saves()` | SELECT (count, optional filter) | — | — |
| `get_bio_state()` | — | SELECT by owner_id | — |
| `get_or_create_bio_state()` | — | SELECT, then INSERT if not found | — |
| `update_bio_state()` | — | UPDATE by owner_id | — |
| `count_logs()` | — | — | SELECT (count) |
| `list_logs()` | — | — | SELECT (ordered, limited) |
| `clean_logs()` | — | — | DELETE (older than cutoff) |

---

## 7. Supabase Services

| Service | Status | Details |
|---|---|---|
| **Authentication** | **NOT USED** | No Supabase Auth is used. The bot authenticates to Telegram via Telethon StringSession. The Supabase client uses the service-role key (no user auth). No `supabase.auth` calls exist anywhere in the codebase. The frontend does not use Supabase Auth either — it reads via the backend API. |
| **Storage** | **NOT USED** | No Supabase Storage buckets are used. Media is stored in Telegram's Saved Messages, not in Supabase Storage. The `file_id` column in `saved_items` stores Telegram's internal file reference, not a Supabase Storage path. No `supabase.storage` calls exist. |
| **Buckets** | **NOT USED** | No buckets are created or referenced. |
| **RLS Policies** | **PARTIALLY USED** | RLS is enabled on all three tables. The authoritative migration creates only SELECT policies for `anon` + `authenticated` (read-only dashboard access). All writes go through the service-role key, which bypasses RLS. See §3 for full policy details. |
| **Functions (RPC)** | **NOT USED** | No Supabase RPC functions are defined or called. No `supabase.rpc()` calls exist. |
| **Realtime** | **NOT USED** | No Supabase Realtime subscriptions. The frontend polls the API every 30 seconds via `setInterval` instead. |
| **Edge Functions** | **NOT USED** | No Edge Functions are deployed or referenced. The `supabase/functions/` directory does not exist in the repository. |
| **Database (PostgreSQL)** | **USED** | The core and only Supabase service in use. Three tables accessed via PostgREST. |
| **PostgREST API** | **USED** | All database access is via the REST API through `supabase-py`. |
| **Migrations** | **USED** | Two migration files in `supabase/migrations/`. Applied via the Supabase MCP `apply_migration` tool (not the Supabase CLI, which is not supported in this environment). |

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

### How Supabase Variables Are Used — Detailed Trace

```
config.py: load()
    │
    ├── supabase_url = os.getenv("SUPABASE_URL", "")
    ├── supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    ├── cfg["SUPABASE_URL"] = supabase_url       ← stored but NOT passed to db/client.py
    ├── cfg["SUPABASE_KEY"] = supabase_key        ← stored but NOT passed to db/client.py
    └── cfg["SUPABASE_AVAILABLE"] = bool(url and key)  ← computed but NOT used by db/client.py

db/client.py: _check_available()
    │
    └── Re-reads os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        directly from the environment — does NOT use cfg dict from config.py
```

**INFERRED:** `db/client.py` reads env vars directly from `os.getenv`
rather than receiving them from `config.py`'s return dict. This means
`config.py`'s `SUPABASE_URL`, `SUPABASE_KEY`, and
`SUPABASE_AVAILABLE` dict entries are dead code — they are computed
but never consumed by the database layer.

---

## 9. Required Build Order

If someone creates a fresh Supabase project manually, the following
order is required:

### Step 1: Create the Supabase Project

Create a new Supabase project in the Supabase Dashboard. Note the
project URL and the service-role key from Settings → API.

### Step 2: Apply the Authoritative Migration

Run the SQL from `20260714111706_create_lifeos_tables.sql` in the
Supabase SQL Editor. This creates:

1. Table `saved_items` with all columns and defaults
2. Index `idx_saved_items_owner` on `saved_items(owner_id)`
3. Index `idx_saved_items_save_code` on `saved_items(save_code)`
4. Index `idx_saved_items_created_at` on `saved_items(created_at DESC)`
5. RLS enabled on `saved_items`
6. SELECT policy `anon_select_saved_items` for anon+authenticated
7. Table `bio_state` with all columns and defaults
8. Index `idx_bio_state_owner` on `bio_state(owner_id)`
9. RLS enabled on `bio_state`
10. SELECT policy `anon_select_bio_state` for anon+authenticated
11. Table `bot_logs` with all columns and defaults
12. Index `idx_bot_logs_owner` on `bot_logs(owner_id)`
13. Index `idx_bot_logs_created_at` on `bot_logs(created_at DESC)`
14. RLS enabled on `bot_logs`
15. SELECT policy `anon_select_bot_logs` for anon+authenticated

### Step 3: Add Missing CHECK Constraints (Recommended)

The authoritative migration lacks CHECK constraints that were present
in the initial migration. For data integrity, manually add:

- `ALTER TABLE saved_items ADD CONSTRAINT check_save_type CHECK (save_type IN ('forward', 'deep'));`
- `ALTER TABLE bot_logs ADD CONSTRAINT check_level CHECK (level IN ('INFO', 'WARN', 'ERROR'));`

### Step 4: Add `updated_at` Auto-Update Trigger (Recommended)

The `bio_state.updated_at` column has no auto-update trigger. Add one
so `updated_at` is set automatically on every row modification:

- Create a trigger function that sets `updated_at = now()`
- Attach it as a `BEFORE UPDATE` trigger on `bio_state`

### Step 5: Add GIN Index on `tags` (Optional, for future tag queries)

If tag-based search is planned:

- `CREATE INDEX idx_saved_items_tags ON saved_items USING GIN (tags);`

### Step 6: Set Environment Variables

In the deployment environment (Render Dashboard or `.env`):

- `SUPABASE_URL` = the Supabase project URL
- `SUPABASE_SERVICE_ROLE_KEY` = the service-role key
- `BOT_OWNER_ID` = the owner's Telegram numeric user ID

### Step 7: Verify

Deploy the application. On startup, Phase 1 logs `[1/5] Database OK`
if the warm-up probe to `bot_logs` succeeds. The first `.save`
command should create a row in `saved_items` with `save_code =
SV-000001`. The first `.bio on` should create a row in `bio_state`
with the owner's ID.

---

## 10. Missing Pieces

### Missing Tables

None. The three tables (`saved_items`, `bio_state`, `bot_logs`) cover
all current application functionality. No code references any table
that does not exist in the migrations.

### Missing Columns

None. All columns referenced by the application code are defined in
the authoritative migration.

### Missing Constraints

1. **`CHECK (save_type IN ('forward', 'deep'))`** — present in the
   initial migration (`20260712234229`), **missing from the
   authoritative migration** (`20260714111706`). The application
   enforces this at the code level, but the database does not.

2. **`CHECK (level IN ('INFO', 'WARN', 'ERROR'))`** — present in the
   initial migration, **missing from the authoritative migration**.
   The application only inserts `'INFO'` currently, but the
   constraint would prevent invalid levels.

### Missing Indexes

1. **GIN index on `saved_items.tags`** — the `tags` column is a
   `text[]` array but has no GIN index. If tag-based queries are ever
   needed (e.g., "find all saves with #saved_photo"), a full table
   scan would result.

### Missing Triggers

1. **`bio_state.updated_at` auto-update trigger** — no trigger exists
   to automatically set `updated_at` on row modification. The bio
   cron loop manually includes `updated_at` in its update dict, but
   the `.bio template/text/mood/on/off` commands do not. This means
   `updated_at` becomes stale after manual state changes.

### Missing RLS Policies

1. **No write policies for anon/authenticated** on any table. This is
   **by design** per the authoritative migration — all writes go
   through the service-role key. However, if the frontend ever needs
   to write directly to Supabase (bypassing the backend API), it
   would be unable to do so.

### Missing Functionality

1. **No `update_save` or `delete_save` function** in `db/client.py`.
   Once a save is created, it cannot be modified or deleted through
   the application. This is by design (saves are immutable records).

2. **No `delete_bio_state` function** — the bio_state row, once
   created, cannot be deleted through the application.

3. **No `update_log` or `delete_log` function** — individual log
   entries cannot be modified. Bulk deletion via `clean_logs()` is
   the only log management function.

---

## 11. Assumptions

### FACT (directly verified from source code or SQL)

1. Three tables exist: `saved_items`, `bio_state`, `bot_logs`.
2. All tables use `bigserial` primary keys (authoritative migration).
3. `saved_items.save_code` has a `UNIQUE` constraint.
4. `bio_state.owner_id` has a `UNIQUE` constraint.
5. RLS is enabled on all three tables.
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

### INFERENCE (not directly verified — deduced from code patterns)

1. **INFERRED:** The `idx_bio_state_owner` index is redundant because
   the `UNIQUE` constraint on `owner_id` already creates an implicit
   unique index. It was likely added for explicitness.
2. **INFERRED:** The `DATABASE_URL` variable was intended for a
   direct PostgreSQL connection (via `psycopg2` or similar) that was
   never implemented. The project pivoted to the Supabase REST API.
3. **INFERRED:** The `VITE_SUPABASE_URL` and `VITE_SUPABASE_ANON_KEY`
   frontend env vars were intended for direct frontend-to-Supabase
   access, but the architecture changed to proxy through the backend
   API, making them dead.
4. **INFERRED:** The `GHOST_ROOM_ID` and `DEST_CHANNEL_ID` env vars
   were intended for features that were never implemented (possibly a
   "ghost" forwarding feature or a destination channel for saves).
5. **INFERRED:** The initial migration was created first with
   wide-open RLS for development convenience, then the authoritative
   migration was created to lock down to read-only for production. The
   CHECK constraints were accidentally omitted in the rewrite.
6. **INFERRED:** The `owner_id=0` hardcoding in the web API is a
   placeholder — the dashboard was designed for a single-owner bot
   and the owner_id was never wired through from config to the web
   layer.
7. **INFERRED:** The `_save_code_lock` (asyncio.Lock) was intended to
   prevent duplicate save codes from concurrent saves, but it only
   works within a single process. If multiple processes or restarts
   occur, the count-based approach can produce duplicate or
   non-sequential codes.
8. **INFERRED:** The `SUPABASE_AVAILABLE` flag in `config.py`'s
   return dict was intended to be passed to other modules, but
   `db/client.py` re-checks the env vars independently, making the
   flag unused.

---

## 12. Manual Setup Guide

This section describes what a human must do inside the Supabase
Dashboard to create the project from scratch. **No SQL is provided.**
Only a description of what needs to exist.

### 12.1 Create the Project

1. Log into the Supabase Dashboard (supabase.com).
2. Click "New Project".
3. Choose an organization, enter a project name and database password.
4. Select a region close to the Render deployment region (the app
   uses `oregon` in `render.yaml`, so US West is appropriate).
5. Wait for the project to provision.

### 12.2 Retrieve Credentials

1. Go to Settings → API.
2. Note the **Project URL** — this becomes `SUPABASE_URL`.
3. Note the **service_role key** — this becomes
   `SUPABASE_SERVICE_ROLE_KEY`. Keep this secret; it bypasses all RLS.
4. The **anon key** is not needed by the backend. It would only be
   needed if the frontend directly accessed Supabase (which it does
   not currently).

### 12.3 Create Tables

Go to the SQL Editor and run the authoritative migration SQL
(`20260714111706_create_lifeos_tables.sql`). This creates all three
tables with correct columns, types, defaults, indexes, and RLS
policies in a single operation.

After running, verify in the Table Editor that three tables exist:
`saved_items`, `bio_state`, `bot_logs`.

### 12.4 Verify RLS

1. Go to Authentication → Policies.
2. Verify that RLS is **enabled** on all three tables.
3. Verify that each table has exactly one policy: a SELECT policy
   for `anon` and `authenticated` with `USING (true)`.
4. Verify that no INSERT, UPDATE, or DELETE policies exist for
   `anon` or `authenticated` (writes are service-role only).

### 12.5 Add Recommended Constraints (Optional but Recommended)

In the SQL Editor, add CHECK constraints on `save_type` and `level`
to match the initial migration's data integrity guarantees. See
§9 Step 3.

### 12.6 Add `updated_at` Trigger (Optional but Recommended)

Create a trigger function and attach it to `bio_state` so that
`updated_at` is automatically set on every UPDATE. See §9 Step 4.

### 12.7 Set Environment Variables in Render

In the Render Dashboard for the `lifeos` web service:

1. Go to Environment.
2. Set `SUPABASE_URL` to the project URL from step 12.2.
3. Set `SUPABASE_SERVICE_ROLE_KEY` to the service-role key from
   step 12.2.
4. Set `BOT_OWNER_ID` to the owner's Telegram numeric user ID.
5. Save and trigger a redeploy.

### 12.8 Verify Deployment

1. After deployment, check Render logs for `[1/5] Database OK`.
2. Send `.ping` via Telegram to confirm the bot is running.
3. Send `.save f` (replying to any message) to create the first
   `saved_items` row.
4. Send `.organize list` to verify counts are non-zero.
5. Send `.bio on` to create the `bio_state` row and start the cron.
6. Open the dashboard URL to verify the API returns data.

### 12.9 What Does NOT Need to Be Created

- **No Supabase Auth users** — the project does not use Supabase
  Authentication.
- **No Storage buckets** — media is stored in Telegram, not Supabase.
- **No Edge Functions** — none are deployed or referenced.
- **No RPC functions** — none are defined or called.
- **No Realtime subscriptions** — the frontend polls via HTTP.
- **No additional schemas** — everything is in the `public` schema.

---

## 13. Risk Analysis

### Database Design Weaknesses

#### R-1: Save Code Generation is Not Atomic Across Restarts

**Severity:** High

`get_next_save_code()` counts existing rows in `saved_items` and
returns `SV-{count+1:06d}`. The `asyncio.Lock` prevents concurrent
saves within a single process from claiming the same code. However:

- If the process restarts between the count read and the subsequent
  insert, the count may be stale.
- If the insert fails (network error, constraint violation) but the
  code was already returned, the next save will get the same code
  (since the count hasn't changed), causing a UNIQUE constraint
  violation.
- If rows are ever deleted from `saved_items`, the count decreases
  and codes can be reused, violating the "sequential" guarantee.

**Fix direction:** Use a PostgreSQL sequence or a dedicated
counter table instead of counting rows.

#### R-2: `get_or_create_bio_state()` Race Condition

**Severity:** Medium

The function does a SELECT, and if no row is found, does an INSERT.
If two concurrent calls (e.g., the bio cron loop and a `.bio` command)
both see no row, both will INSERT, causing a UNIQUE constraint
violation on `owner_id`. The application catches exceptions and falls
back, but one of the two calls will fail silently.

**Fix direction:** Use an UPSERT (`INSERT ... ON CONFLICT (owner_id)
DO NOTHING`) or a PostgREST upsert.

#### R-3: Web API Hardcodes `owner_id=0`

**Severity:** Medium

All four API endpoints (`/api/saves`, `/api/saves/{code}`, `/api/bio`,
`/api/logs`) pass `owner_id=0` to the `db_client` functions. This
means:

- The dashboard shows data for `owner_id=0`, not the real owner.
- If the real owner's ID is e.g. `123456789`, the dashboard will show
  empty results because all saves are written with the real owner's
  ID.
- The `/api/saves` endpoint's `list_saves(0, ...)` will return zero
  items because no saves have `owner_id=0`.

This is a **functional bug** that makes the dashboard useless for any
owner whose ID is not `0`.

#### R-4: No `updated_at` Auto-Update Trigger on `bio_state`

**Severity:** Low

The `updated_at` column defaults to `now()` on insert but is not
auto-updated on modification. The bio cron loop manually sets
`updated_at`, but the `.bio template/text/mood/on/off` commands do
not include `updated_at` in their update dicts. This means
`updated_at` becomes stale after manual state changes, making it
unreliable for "last modified" tracking.

#### R-5: Missing CHECK Constraints in Authoritative Migration

**Severity:** Low

The authoritative migration (`20260714111706`) lacks `CHECK`
constraints on `saved_items.save_type` and `bot_logs.level` that were
present in the initial migration (`20260712234229`). If only the
authoritative migration is applied to a fresh project, invalid values
can be inserted directly into the database (bypassing the
application). The application enforces valid values at the code
level, but the database itself does not.

#### R-6: Synchronous Supabase Calls Block the Event Loop

**Severity:** Medium

The `supabase-py` client uses `httpx` in synchronous mode. Every
`.execute()` call blocks the asyncio event loop for the duration of
the HTTP round-trip. Under normal conditions (Supabase responding in
<100ms) this is invisible, but under DB latency spikes:

- Telethon may miss incoming events or delay responses.
- The bio cron may fire late (the sleep is followed by a blocking DB
  call).
- Concurrent commands may appear to queue behind each other.

**Fix direction:** Use `supabase-py` async client or run synchronous
calls in a thread executor.

#### R-7: No GIN Index on `tags` Array

**Severity:** Low

The `tags` column is `text[]` but has no GIN index. Any future
tag-based query (e.g., "find all saves tagged #saved_photo") would
require a full table scan. Not a current problem since no code queries
by tag, but a design gap.

#### R-8: Two Conflicting Migrations

**Severity:** Medium

Two migration files exist with different schemas and RLS policies.
If both are applied in sequence (which is the normal Supabase
migration behaviour):

1. The initial migration creates tables with CHECK constraints and
   wide-open CRUD policies.
2. The authoritative migration's `CREATE TABLE IF NOT EXISTS` is a
   no-op (tables already exist), so its column definitions and
   defaults are ignored.
3. The authoritative migration drops and recreates only SELECT
   policies, correctly removing the wide-open INSERT/UPDATE/DELETE
   policies.
4. The authoritative migration adds new indexes.
5. **The CHECK constraints from the initial migration persist**
   (they are not dropped).

Net result when both run: tables have CHECK constraints, read-only
RLS, and all indexes. This is actually the best outcome.

However, if **only** the authoritative migration is applied (fresh
project, initial migration skipped): tables lack CHECK constraints.
This is the scenario documented in §9 Step 3.

#### R-9: No Data Retention Policy for `saved_items`

**Severity:** Low

`saved_items` rows are never deleted by the application. The table
grows indefinitely. For a personal bot this is unlikely to be a
problem, but there is no cleanup mechanism. `bot_logs` has
`.organize clean` (7-day purge), but `saved_items` has no equivalent.

#### R-10: `saved_items` Count Includes All Owners

**Severity:** Low

`get_next_save_code()` counts ALL rows in `saved_items` regardless of
`owner_id`. In a multi-owner scenario (not currently supported, but
the schema allows it), save codes would be shared across owners,
making them non-sequential per owner. This is a design choice, not a
bug, for the current single-owner model.

#### R-11: RLS SELECT Policies Are Fully Open

**Severity:** Medium

All three tables have `SELECT ... USING (true)` policies for
`anon` + `authenticated`. This means anyone with the Supabase anon
key can read all data in all tables — every owner's saves, every
owner's bio state, every owner's logs. For a single-owner bot this is
acceptable. For multi-owner, it is a data isolation failure.

#### R-12: `clean_logs()` Return Count May Be Unreliable

**Severity:** Low

`clean_logs()` returns `len(result.data)` where `result.data` is the
list of deleted rows returned by PostgREST. The Supabase client's
return behavior for `.delete()` depends on the `Prefer:
return=representation` header, which may or may not be set by
default in `supabase-py 2.4.2`. If the header is not set,
`result.data` may be `None` or empty even though rows were deleted,
causing the reported count to be `0`.

---

### Summary of Risks by Severity

| Severity | Count | IDs |
|---|---|---|
| High | 1 | R-1 |
| Medium | 4 | R-2, R-3, R-6, R-8, R-11 |
| Low | 6 | R-4, R-5, R-7, R-9, R-10, R-12 |

---

### End of Document

This document reflects the state of the repository at commit
`141e963d8a5990cc43662b0391a56aa15a679a78`. If the codebase changes
in ways that invalidate any section above, update this document.
