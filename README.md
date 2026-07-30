# LifeOS — Telegram Self-Bot

A headless Telegram userbot that runs as a single Python `asyncio` process on Render. It uses **Telethon** with a `StringSession` to operate the owner's own Telegram account, plus a separate **helper bot** (optional) that powers interactive inline UI panels. A **FastAPI** micro-server runs in the same process for health checks and a read-only React dashboard.

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Architecture Overview](#2-architecture-overview)
3. [Directory Structure](#3-directory-structure)
4. [Runtime Architecture](#4-runtime-architecture)
5. [Helper Panel System](#5-helper-panel-system)
6. [Telegram Self Architecture](#6-telegram-self-architecture)
7. [StringSession Architecture](#7-stringsession-architecture)
8. [Supabase Architecture](#8-supabase-architecture)
9. [Database Tables](#9-database-tables)
10. [Panel Settings](#10-panel-settings)
11. [Command Reference](#11-command-reference)
12. [Installation](#12-installation)
13. [Deployment](#13-deployment)
14. [Environment Variables](#14-environment-variables)
15. [Troubleshooting](#15-troubleshooting)
16. [Known Limitations](#16-known-limitations)
17. [Future Architecture](#17-future-architecture)

---

## 1. Project Overview

LifeOS provides four subsystems plus a diagnostics layer:

| Subsystem | Purpose |
|---|---|
| **Save Engine** | Forward-save or deep-save (download + re-upload) any media to Saved Messages with structured metadata stored in Supabase. |
| **Bio Engine** | A timezone-synchronized cron that rewrites the owner's Telegram profile bio every minute using `{time}`, `{mood}`, and `{text}` tokens. |
| **Organizer** | Data overview, log cleanup, orphan cleanup, database statistics, and multi-message deletion. |
| **Diagnostics** | Health dashboard, event log, diagnostic snapshot with stalled-task recovery. |
| **Discover** | Browse recent saves and search by caption, filename, code, or MIME type. |

The bot also serves a dark-themed React dashboard at the root URL, polling the backend API every 30 seconds.

**Tech stack:** Python 3.11 · Telethon 1.34 · FastAPI 0.111 · Uvicorn 0.29 · Supabase 2.4 · React 18 + Vite 5 + Tailwind CSS 3.

---

## 2. Architecture Overview

```
┌─────────────────────────────────────────────────────┐
│              Single Python Process                   │
│              (asyncio event loop)                    │
│                                                      │
│  ┌──────────────────┐    ┌──────────────────────┐    │
│  │ RuntimeSupervisor │    │  Telethon Self-Client │    │
│  │ (watchdog +       │    │  (StringSession)      │    │
│  │  recovery)        │    │  @client.on(events…)   │    │
│  └────────┬─────────┘    └──────────┬─────────────┘    │
│           │                         │                  │
│           │    ┌────────────────────┼──────────────┐   │
│           │    │ Handler Modules    │              │   │
│           │    │ misc save retrieve │              │   │
│           │    │ delete organize bio│              │   │
│           │    │ discover database  │              │   │
│           │    └────────────────────┘              │   │
│           │                                        │   │
│  ┌────────▼─────────┐  ┌────────────┐  ┌─────────┐ │   │
│  │ Helper Bot Client │  │ Bio Cron   │  │ FastAPI │ │   │
│  │ (BOT_TOKEN, inline│  │ Engine     │  │ Web API │ │   │
│  │  UI panels)       │  │ (per-min)  │  │ + SPA   │ │   │
│  └──────────────────┘  └────────────┘  └─────────┘ │   │
│                                                      │
│  ┌──────────────────────────────────────────────┐   │
│  │ Service Layer (business logic)                │   │
│  │ save_service  retrieve_service  delete_service│   │
│  │ bio_service  organize_service  settings_service│  │
│  │ database_service  discover_service             │   │
│  └──────────────────────┬───────────────────────┘   │
│                         │                            │
│  ┌──────────────────────▼───────────────────────┐   │
│  │ db/client.py (Supabase singleton + fallback) │   │
│  └──────────────────────┬───────────────────────┘   │
└─────────────────────────┼───────────────────────────┘
                          │ HTTPS (PostgREST)
                          ▼
               ┌─────────────────────┐
               │   Supabase Project  │
               │  (PostgreSQL + RLS) │
               └─────────────────────┘
```

### Key Principles

- **Single asyncio process.** Telethon, Uvicorn, bio cron, helper bot, and watchdog all share one event loop.
- **Service-role key only.** The backend authenticates to Supabase with the service-role key, bypassing all RLS.
- **In-memory fallback.** If Supabase is unavailable, the bot degrades to a Python dict in memory and continues functioning.
- **Edit-first policy.** Command responses edit the triggering message in-place. No new messages are sent (zero-spam).
- **Self-healing.** A `RuntimeSupervisor` monitors the Telethon client with real RPC heartbeats and rebuilds it automatically on failure.

---

## 3. Directory Structure

```
project/
├── Procfile                           # Render start command
├── render.yaml                        # Render Blueprint
├── package.json                       # Frontend deps
├── vite.config.ts                     # Vite config
├── tailwind.config.js                 # Tailwind theme
├── postcss.config.js
├── tsconfig.json / tsconfig.app.json
├── index.html                         # Vite HTML entry
├── .env
│
├── backend/
│   ├── __init__.py
│   ├── main.py                        # Entry point — creates RuntimeSupervisor
│   ├── config.py                      # Env var loader
│   ├── diagnostics.py                 # Event history + .kill diagnostic report
│   ├── health.py                      # Runtime health telemetry
│   ├── requirements.txt               # Python deps
│   │
│   ├── bot/
│   │   ├── __init__.py
│   │   ├── client.py                  # Telethon self-client factory
│   │   ├── router.py                  # Handler registration
│   │   └── handlers/
│   │       ├── __init__.py
│   │       ├── guard.py               # is_owner permission gate
│   │       ├── misc.py                # .ping .id .help .panel .health .kill .logs
│   │       ├── save.py                # .save f / .save d / .save
│   │       ├── retrieve.py            # .preview .send .retrieve .r
│   │       ├── delete.py              # .del <n> / .del id / .del <code>
│   │       ├── organize.py            # .organize list / .organize clean
│   │       ├── bio.py                 # .bio sub-commands
│   │       ├── discover.py            # .list / .find
│   │       └── database.py            # .db clean / .db stats / .db vacuum
│   │
│   ├── bio/
│   │   ├── __init__.py
│   │   └── engine.py                  # Bio cron loop
│   │
│   ├── db/
│   │   ├── __init__.py
│   │   └── client.py                  # Supabase singleton + in-memory fallback
│   │
│   ├── helper/
│   │   ├── __init__.py                # Re-exports public API
│   │   ├── client.py                  # Helper bot client factory
│   │   ├── inline_engine.py           # Inline mode trigger + query handler
│   │   ├── inline_sender.py           # send_inline_panel + input listener
│   │   ├── panels.py                  # Callback router, nav, session lifecycle
│   │   ├── panel_render.py            # Render inline results / edit buttons
│   │   ├── panel_settings.py          # Compatibility shim → settings_service
│   │   ├── panel_timer.py             # Auto-close countdown timer
│   │   ├── panel_selftest.py          # Deterministic panel pipeline test
│   │   ├── input_state.py             # Pending input state
│   │   ├── pagination.py              # Reusable paginator
│   │   ├── session_manager.py         # Panel session + nav stack
│   │   ├── target_context.py          # Reply target abstraction
│   │   ├── context.py                 # Callback data encoding/truncation
│   │   ├── callback_trace.py          # Callback trace logger
│   │   └── watchdog.py                # Helper bot watchdog
│   │
│   ├── runtime/
│   │   ├── __init__.py
│   │   ├── supervisor.py              # RuntimeSupervisor — self-healing watchdog
│   │   ├── managed_task.py            # Supervised asyncio task wrapper
│   │   └── states.py                  # RuntimeState enum
│   │
│   ├── services/
│   │   ├── __init__.py
│   │   ├── save_service.py           # Save business logic
│   │   ├── retrieve_service.py       # Retrieve business logic
│   │   ├── delete_service.py         # Delete business logic
│   │   ├── organize_service.py       # Organize business logic
│   │   ├── bio_service.py            # Bio business logic
│   │   ├── settings_service.py       # Settings (reads/writes panel_settings)
│   │   ├── database_service.py       # DB maintenance (orphans, stats, vacuum)
│   │   └── discover_service.py       # List + search business logic
│   │
│   └── web/
│       ├── __init__.py
│       └── app.py                    # FastAPI app
│
├── src/                               # React dashboard
│   ├── main.tsx
│   ├── App.tsx
│   ├── index.css
│   ├── lib/api.ts
│   └── components/
│       ├── SavedItems.tsx
│       ├── BioStatus.tsx
│       └── LogViewer.tsx
│
└── supabase/migrations/
    ├── 20260712234229_lifeos_schema.sql
    ├── 20260714111706_create_lifeos_tables.sql
    ├── 20260718143752_20260718_save_ux_redesign.sql.sql
    ├── 20260726143924_create_panel_settings_table.sql
    ├── 20260729213959_20260729120000_create_bot_settings_table.sql
    └── 20260729221445_expand_panel_settings_typed_columns.sql
```

---

## 4. Runtime Architecture

### RuntimeSupervisor (`backend/runtime/supervisor.py`)

The `RuntimeSupervisor` owns every runtime coroutine and provides self-healing:

- **Heartbeat:** A watchdog task runs every 30 seconds, performing a real RPC (`client.get_me()`) as the heartbeat.
- **Failure detection:** 3 consecutive heartbeat failures declare the client dead and trigger atomic recovery.
- **Recovery sequence:** stop bio cron → stop helper → clear inline panel state → cancel run task → cancel orphan tasks → dispose dead client → backoff sleep → build new client → re-register handlers → re-wire helper/inline/bio → verify with fresh heartbeat → restart run loop.
- **Max 5 recovery attempts** with exponential backoff (base 2s, max 300s, 30% jitter). All attempts exhausted → `sys.exit(1)` so Render restarts the process.

### Startup (5 phases)

1. **Config validation** — `config.load()` hard-exits on missing required vars.
2. **RuntimeSupervisor.start()** — connects and authorizes the Telethon client via `build_client()`.
3. **Handler registration** — `register_all()` wires every command handler onto the client.
4. **Helper bot + bio cron + web server** — starts the helper bot (if `BOT_TOKEN` is set), resumes the bio cron (if active), launches Uvicorn as a background task.
5. **Run loop** — `asyncio.wait()` on `client.run_until_disconnected()` and `shutdown_event`.

### Shutdown (SIGTERM/SIGINT)

1. `shutdown_event.set()`
2. Stop bio cron (`stop_cron()`)
3. Stop helper bot watchdog
4. Signal Uvicorn to exit (`should_exit = True`)
5. Cancel all remaining asyncio tasks
6. Disconnect Telethon client
7. Disconnect helper bot

### RuntimeState FSM (`backend/runtime/states.py`)

| State | Meaning |
|---|---|
| `STARTING` | Process boot, config loaded |
| `CONNECTING` | Telethon client connecting |
| `AUTHORIZING` | Session authorization check |
| `REGISTERING` | Command handler registration |
| `READY` | Fully operational |
| `DEGRADED` | Partially operational |
| `RECOVERING` | Attempting reconnect |
| `REBUILDING` | Rebuilding TelegramClient from scratch |
| `STOPPING` | Shutdown in progress |
| `FAILED` | Unrecoverable, process will exit |

### ManagedTask (`backend/runtime/managed_task.py`)

A supervised asyncio task wrapper with automatic restart. Wraps a factory callable called on every restart. If the task exits unexpectedly, a watchdog respawns it after a configurable delay.

### Health Telemetry (`backend/health.py`)

Module-level global state written by the supervisor and read by the FastAPI `/health` endpoint. Tracks: `runtime_state`, `telethon_connected`, `supervisor_ok`, `bio_cron_ok`, `helper_connected`, `restart_count`, `rpc_latency_ms`, `watchdog_ok`, timestamps for heartbeat/command/telethon events/bio updates, and task states.

### Diagnostics (`backend/diagnostics.py`)

In-memory circular event log (`deque(maxlen=500)`). Every significant bot action calls `record_event(module, action, duration_ms, result, details)`. The `.kill` command assembles a full diagnostic report and attempts stalled-task recovery.

---

## 5. Helper Panel System

The helper panel system provides interactive inline UI panels via a separate helper bot. When the owner sends a command like `.save` or `.bio` without arguments, the self-bot triggers inline mode on the helper bot, auto-sends the first result, and the helper bot's callback query handler manages all subsequent button interactions.

### Components

| Component | File | Role |
|---|---|---|
| **Helper bot client** | `helper/client.py` | Creates a `TelegramClient` using a bot token. Returns `None` if `BOT_TOKEN` is not set — inline UI is silently disabled. |
| **Inline engine** | `helper/inline_engine.py` | The self-bot calls `trigger()` to invoke `client.inline_query(bot_username, query)` and auto-send the first result. The helper bot's `InlineQuery` handler routes to registered builders. |
| **Inline sender** | `helper/inline_sender.py` | `send_inline_panel()` checks if a panel already exists for the chat. If yes, edits the existing message. If no, triggers inline mode, sends the result, creates a session, and starts the auto-close timer. |
| **Panel system** | `helper/panels.py` | The callback router. Routes `panel:`, `action:`, and `input:` callback data to registered handlers. Manages navigation (Back, Home, Close). |
| **Panel renderer** | `helper/panel_render.py` | Builds `InputBotInlineResult` objects and converts button layouts for both inline results and edit-message calls. |
| **Panel timer** | `helper/panel_timer.py` | Single-message auto-close countdown. Edits the same inline message at the configured interval with the remaining seconds, then closes. Only active when `auto_close_enabled` is `true`. |
| **Session manager** | `helper/session_manager.py` | Owns all panel session state. Keyed by `(chat_id, msg_id)` with `inline_message_id` as a secondary lookup. Stores a navigation stack of `(panel_id, extra)` tuples. |
| **Input state** | `helper/input_state.py` | Manages pending text input. When a user taps an "Enter Code" button, a pending input is set. The next outgoing message in the same chat is captured as the input value. Has a configurable timeout. |
| **Target context** | `helper/target_context.py` | Abstracts how a panel resolves its target message (reply target). Expires after 5 minutes. |
| **Pagination** | `helper/pagination.py` | Reusable Previous/Next row builder for paginated panels. |
| **Callback trace** | `helper/callback_trace.py` | Instruments every callback with a unique trace ID and logs to Saved Messages. |
| **Watchdog** | `helper/watchdog.py` | Monitors the helper bot. If disconnected, logs warnings. After 3 consecutive failures, marks permanently failed. |
| **Self-test** | `helper/panel_selftest.py` | Deterministic verification of the inline panel pipeline. |

### Panel Lifecycle

```
Create → Render → Wait → Action/Input → Update → Back/Home/Close → Destroy
```

1. **Create:** `send_inline_panel()` triggers inline mode, sends the first result, creates a session.
2. **Render:** The panel handler returns `(title, body, buttons)`. Navigation buttons (Back, Home, Close) are appended automatically.
3. **Wait:** The user taps a button. The callback router dispatches to the panel/action/input handler.
4. **Action/Input:** Actions execute instantly and re-render. Inputs set a pending state and prompt for text.
5. **Update:** The same inline message is edited with new content.
6. **Back/Home/Close:** Navigation pops/pushes the nav stack. Close destroys the session and edits the message to "Panel closed".
7. **Destroy:** Session cleared, timer stopped, render cache cleared.

### Root Menu Rules

- Root menu (nav stack length 1): only Close button.
- Submenus (nav stack length > 1): Back, Home, and Close buttons.

---

## 6. Telegram Self Architecture

### Self-Client (`backend/bot/client.py`)

`build_client(api_id, api_hash, session_string)` creates a `TelegramClient` with `StringSession`. Connection parameters tuned for Render Free tier:

| Parameter | Value |
|---|---|
| `auto_reconnect` | `True` |
| `connection_retries` | 5 |
| `retry_delay` | 2s |
| `flood_sleep_threshold` | 60s |
| `system_version` | `4.16.30-vxCUSTOM` |
| `device_model` | `LifeOS` |

Bounded timeouts: connect 30s, authorize 15s, get_me 15s.

### Handler Registration (`backend/bot/router.py`)

`register_all(client, owner_id, tz_str)` registers runtime hooks first (for health telemetry), then each handler module in sequence: `misc`, `save`, `retrieve`, `delete`, `organize`, `bio`, `discover`, `database`. If one module fails to register, the error is logged and the remaining modules still register.

### Permission Gate (`backend/bot/handlers/guard.py`)

`is_owner(event, owner_id)` returns `True` only if `event.sender_id == owner_id`. Every handler calls this first. Non-owner messages are silently ignored. Also tracks `last_command` timestamp for health telemetry.

### Outgoing-Only Commands

All handlers fire on `events.NewMessage(outgoing=True)`. Commands must be sent from the owner's own account. This is the self-bot model — there is no separate bot account for commands.

---

## 7. StringSession Architecture

The Telethon session is stored as an environment variable (`SESSION_STRING`), never written to disk. The session string encodes the authentication key and must be generated once on a local machine.

### Generation

Run the following on a local machine with Telethon installed:

```python
from telethon import TelegramClient
from telethon.sessions import StringSession

client = TelegramClient(StringSession(), API_ID, API_HASH)
client.start(phone="+...")
print(client.session.save())
```

Copy the printed string and set it as the `SESSION_STRING` environment variable. The session must never be committed to the repository or logged.

### Validation

On startup, `build_client()` calls `client.connect()` then `client.is_user_authorized()`. If the session is invalid, a `RuntimeError` is raised with instructions to regenerate. The `RuntimeSupervisor` catches this and may trigger a full rebuild.

---

## 8. Supabase Architecture

### Communication

The application communicates with Supabase exclusively through the PostgREST REST API via the `supabase-py` client (v2.4.2). No direct PostgreSQL connections, no Supabase CLI, no `psql`.

### Client Initialization (`backend/db/client.py`)

`get_db()` is a singleton — initialized once on first access. If `SUPABASE_URL` or `SUPABASE_SERVICE_ROLE_KEY` is missing, returns `None` and all operations fall back to in-memory storage.

### In-Memory Fallback

```python
_fallback = {"saved_items": [], "bio_state": {}, "bot_logs": []}
```

Every public function wraps its Supabase call in `try/except`. On any error, it logs a warning and uses the fallback. The bot never crashes due to a database error.

### Services Used

| Service | Status |
|---|---|
| PostgreSQL (via PostgREST) | **Used** — 4 tables |
| Authentication | Not used |
| Storage | Not used |
| Realtime | Not used |
| Edge Functions | Not used |
| RPC Functions | Not used |

### RLS Model

RLS is enabled on all tables. Only SELECT policies are granted to `anon` + `authenticated` (read-only dashboard access). All writes go through the backend's service-role key, which bypasses RLS entirely.

---

## 9. Database Tables

### `saved_items`

**Purpose:** Stores metadata for every media save operation (forward and deep). Each row represents one saved Telegram message.

| Column | Type | Nullable | Default |
|---|---|---|---|
| `id` | `bigserial` | NO | `nextval()` |
| `save_code` | `text` | NO | — |
| `short_code` | `text` | YES | — |
| `save_type` | `text` | NO | `'forward'` |
| `origin_chat_id` | `bigint` | YES | — |
| `origin_msg_id` | `bigint` | YES | — |
| `saved_chat_id` | `bigint` | YES | — |
| `saved_msg_id` | `bigint` | YES | — |
| `sender_name` | `text` | YES | — |
| `sender_id` | `bigint` | YES | — |
| `mime_type` | `text` | YES | — |
| `file_id` | `text` | YES | — |
| `file_size` | `bigint` | YES | — |
| `media_type` | `text` | YES | — |
| `file_name` | `text` | YES | — |
| `tags` | `text[]` | YES | `'{}'` |
| `caption` | `text` | YES | — |
| `owner_id` | `bigint` | NO | — |
| `created_at` | `timestamptz` | YES | `now()` |

**Relationships:** No foreign keys. `owner_id` is a logical link to the bot owner.

**Save code format:** `S` + zero-padded 4-digit sequential (e.g. `S0001`). Legacy format was `SV-NNNNNN`. Both `save_code` and `short_code` are unique; lookups try `short_code` first, then fall back to `save_code`.

---

### `bio_state`

**Purpose:** Singleton-per-owner state for the bio cron engine.

| Column | Type | Nullable | Default |
|---|---|---|---|
| `id` | `bigserial` | NO | `nextval()` |
| `owner_id` | `bigint` | NO | — |
| `template` | `text` | NO | `'🕒 {time} \| 💭 {mood}'` |
| `mood` | `text` | NO | `'😊'` |
| `custom_text` | `text` | NO | `''` |
| `is_active` | `boolean` | NO | `false` |
| `last_bio` | `text` | NO | `''` |
| `updated_at` | `timestamptz` | YES | `now()` |

**Relationships:** `owner_id` has a UNIQUE constraint — one row per owner.

---

### `bot_logs`

**Purpose:** Structured activity log.

| Column | Type | Nullable | Default |
|---|---|---|---|
| `id` | `bigserial` | NO | `nextval()` |
| `owner_id` | `bigint` | NO | — |
| `level` | `text` | NO | `'INFO'` |
| `message` | `text` | NO | — |
| `context` | `jsonb` | YES | `'{}'` |
| `created_at` | `timestamptz` | YES | `now()` |

**Relationships:** No foreign keys. `owner_id` is a logical link.

---

### `panel_settings`

**Purpose:** Permanent configuration storage for the helper panel system. Single row (`key='global'`).

| Column | Type | Nullable | Default | Constraints |
|---|---|---|---|---|
| `key` | `text` | NO | — | PRIMARY KEY |
| `auto_close_enabled` | `boolean` | NO | `true` | — |
| `auto_close_delay_seconds` | `integer` | NO | `120` | CHECK (10–3600) |
| `max_deep_save_mb` | `integer` | NO | `50` | CHECK (1–500) |
| `delete_batch_size` | `integer` | NO | `100` | CHECK (1–1000) |
| `log_retention_days` | `integer` | NO | `7` | CHECK (1–365) |
| `panel_countdown_interval` | `integer` | NO | `30` | CHECK (5–120) |
| `input_timeout_seconds` | `integer` | NO | `120` | CHECK (10–600) |
| `updated_at` | `timestamptz` | YES | `now()` | — |

**Relationships:** None. Single-row table.

---

## 10. Panel Settings

All configurable settings are stored in the `panel_settings` table as typed columns. The database is the single source of truth; the in-memory cache in `settings_service` is refresh-only.

### Settings

| Setting | Default | Range | Used By |
|---|---|---|---|
| `auto_close_enabled` | `true` | bool | Panel timer — whether panels auto-close |
| `auto_close_delay_seconds` | `120` | 10–3600 | Panel timer — seconds before auto-close |
| `max_deep_save_mb` | `50` | 1–500 | Save service — deep save file size limit |
| `delete_batch_size` | `100` | 1–1000 | Delete service — messages per `delete_messages()` call |
| `log_retention_days` | `7` | 1–365 | Organize service — days before log cleanup |
| `panel_countdown_interval` | `30` | 5–120 | Panel timer — seconds between countdown re-renders |
| `input_timeout_seconds` | `120` | 10–600 | Input state — seconds before pending input expires |

### How the Helper Reads Settings

1. **Startup:** `settings_service.load_all()` reads the `panel_settings` row from the DB and populates an in-memory cache. If the DB is unavailable, hardcoded defaults are used.
2. **Runtime reads:** Feature modules call typed accessors (e.g. `settings_service.max_deep_save_mb()`). The accessor calls `_ensure_loaded()` (which calls `load_all()` if not yet loaded) then reads from the cache. No DB round-trip per read.

### How Updates Are Written

1. The Settings panel (inline UI) or text command calls a typed setter (e.g. `settings_service.set_max_deep_save_mb(100)`).
2. The setter validates the range. If invalid, returns `False`.
3. If valid, calls `_update()` which:
   - Writes to the DB via `upsert()` (includes `updated_at`).
   - Calls `_refresh()` to re-read the DB row into the cache.
   - Returns `True`.
4. If the DB is unavailable, updates the in-memory cache directly.

---

## 11. Command Reference

All commands use the `.` prefix. All commands fire on `events.NewMessage(outgoing=True)`. Every handler calls `is_owner()` first.

### General (`misc.py`)

| Command | Behavior |
|---|---|
| `.ping` | Edits the triggering message to `PONG`. |
| `.id` | Edits message with Chat ID + Msg ID. If replying, also shows Reply Msg ID + Sender ID. |
| `.help` | Opens an interactive inline help panel (requires helper bot). Falls back to plain-text edit if no helper bot. |
| `.panel` | Opens a context panel for the replied message (Forward Save, Deep Save, Preview). Requires helper bot. |
| `.health` | Shows a full health dashboard (inline panel or plain text). Displays process, Telegram, supervisor, watchdog, bio cron, heartbeat, restarts, running tasks, database, and uptime status. |
| `.kill` | Diagnostic snapshot + stalled-task recovery (inline panel or plain text). |
| `.logs` | View recent diagnostic events (inline panel or plain text). Supports `.logs <n>`, `.logs errors`, `.logs 50`. |

### Save Engine (`save.py`)

| Command | Behavior |
|---|---|
| `.save f` / `.s f` | Forward save. Forwards the replied message to Saved Messages. Records metadata in DB. |
| `.save d` / `.s d` | Deep save. Downloads media (up to `max_deep_save_mb`), re-uploads to Saved Messages with a rich caption. Records full metadata in DB. |
| `.save` / `.s` | Opens an inline panel to choose Forward or Deep save. Requires helper bot. |

### Retrieve (`retrieve.py`)

| Command | Behavior |
|---|---|
| `.preview <code>` / `.retrieve <code>` / `.r <code>` | Shows stored metadata for a save code. |
| `.send <code>` | Forwards the saved asset from Saved Messages to the current chat. Deletes the triggering command on success. |
| `.preview` / `.send` | Opens an inline panel to enter a code. Requires helper bot. |

### Delete (`delete.py`)

| Command | Behavior |
|---|---|
| `.del <n>` | Deletes the last `n` outgoing messages in the current chat (1–500 range). |
| `.del id <msgid>` | Deletes all messages from `<msgid>` forward in the current chat. Batch-deletes in chunks of `delete_batch_size`. |
| `.del <code>` | Deletes a saved item: removes the Telegram message from Saved Messages and the DB row. |
| `.del` | Opens an inline panel to choose deletion mode. Requires helper bot. |

### Organizer (`organize.py`)

| Command | Behavior |
|---|---|
| `.organize list` | Shows structured overview: total/forward/deep save counts, log count, bio engine status + template. |
| `.organize clean` | Purges `bot_logs` entries older than `log_retention_days`. |
| `.organize` | Opens an inline panel to choose list or clean. Requires helper bot. |

### Bio Engine (`bio.py`)

| Command | Behavior |
|---|---|
| `.bio help` | Shows token reference and command list. |
| `.bio template <tpl>` | Sets the bio template. Supports `{time}`, `{mood}`, `{text}` tokens. |
| `.bio text <text>` | Sets the `{text}` token value. |
| `.bio mood <mood>` | Sets the `{mood}` token value. |
| `.bio on` | Sets `is_active=True`, starts the cron engine, shows a preview. |
| `.bio off` | Sets `is_active=False`, stops the cron engine. |
| `.bio show` | Shows full bio state: status, template, mood, text, last bio, preview, server time. |
| `.bio` | Opens an inline panel with all bio actions. Requires helper bot. |

### Discover (`discover.py`)

| Command | Behavior |
|---|---|
| `.list [n]` | Shows recent saves (default 10, max 50). Displays code, icon, filename, media type, date. |
| `.find <text>` | Searches saves by caption, filename, code, or MIME type. Returns up to 20 matches. |
| `.list` / `.find` | Opens an inline panel. Requires helper bot. |

### Database (`database.py`)

| Command | Behavior |
|---|---|
| `.db clean` | Removes orphan rows from `saved_items` (entries whose Telegram message no longer exists). |
| `.db stats` | Shows database statistics: total items, breakdown by type, size estimate, oldest/newest save dates. |
| `.db vacuum` | Cleanup orphans + report (index optimization is skipped since PostgREST is used). |
| `.db` | Opens an inline panel to choose clean/stats/vacuum. Requires helper bot. |

---

## 12. Installation

### Prerequisites

- Python 3.11+
- Node.js 18+ (for frontend build)
- A Telegram account with API credentials from [my.telegram.org](https://my.telegram.org)
- A Supabase project (optional — bot runs without it)
- A Telegram bot token from [@BotFather](https://t.me/BotFather) (optional — for inline UI panels)

### Local Setup

1. **Clone the repository:**

   ```bash
   git clone https://github.com/Onlyicing18/Telegram-self-bot.git
   cd Telegram-self-bot
   ```

2. **Generate a StringSession:**

   ```python
   from telethon import TelegramClient
   from telethon.sessions import StringSession

   client = TelegramClient(StringSession(), YOUR_API_ID, YOUR_API_HASH)
   client.start(phone="+your_phone_number")
   print(client.session.save())
   ```

   Copy the output string.

3. **Set environment variables** (see [§14](#14-environment-variables)).

4. **Install Python dependencies:**

   ```bash
   pip install -r backend/requirements.txt
   ```

5. **Apply database migrations** (if using Supabase):

   Run the SQL from each file in `supabase/migrations/` in the Supabase SQL Editor, in order:
   - `20260712234229_lifeos_schema.sql`
   - `20260714111706_create_lifeos_tables.sql`
   - `20260718143752_20260718_save_ux_redesign.sql.sql`
   - `20260726143924_create_panel_settings_table.sql`
   - `20260729213959_20260729120000_create_bot_settings_table.sql`
   - `20260729221445_expand_panel_settings_typed_columns.sql`

6. **Build the frontend (optional):**

   ```bash
   npm install
   npm run build
   ```

   This produces `dist/` which FastAPI serves as static files.

7. **Run the bot:**

   ```bash
   python -m backend.main
   ```

---

## 13. Deployment

### Render

The project is configured for Render via two files:

- **`Procfile`**: `web: python -m backend.main`
- **`render.yaml`**: Render Blueprint defining the web service, Python environment, health check path (`/health`), and environment variables.

### Deployment Flow

1. Push code to GitHub (connected to Render).
2. Render builds the Python environment from `backend/requirements.txt`.
3. Render starts `python -m backend.main`.
4. Health check hits `/health` — must return 200.
5. The Telethon client connects and the bot is live.

### Frontend Build

The React dashboard is not built automatically by Render. To serve it, run `npm run build` locally (produces `dist/`) or add a build step in Render. The FastAPI app checks for `dist/` at startup and mounts it as static files if present.

---

## 14. Environment Variables

### Required (hard-fail if missing)

| Variable | Type | Description |
|---|---|---|
| `API_ID` | int | Telegram API ID from my.telegram.org |
| `API_HASH` | str | Telegram API Hash from my.telegram.org |
| `SESSION_STRING` | str | Telethon StringSession (generated offline) |
| `BOT_OWNER_ID` | int | Telegram numeric user ID of the bot owner |

### Optional

| Variable | Default | Description |
|---|---|---|
| `BOT_TOKEN` | `""` | Helper bot token from BotFather. Enables inline UI panels. |
| `SUPABASE_URL` | `""` | Supabase project URL. If empty, in-memory fallback. |
| `SUPABASE_SERVICE_ROLE_KEY` | `""` | Supabase service-role key. Bypasses RLS. |
| `DATABASE_URL` | `""` | Loaded but not consumed. Reserved for future use. |
| `TZ` | `Asia/Tehran` | Timezone for bio engine and timestamps. |
| `PORT` | `8000` | Web server port. Render sets this automatically. |
| `BIO_UPDATE_ENABLED` | `false` | If `true`, auto-starts bio cron on boot regardless of DB state. |
| `LOG_LEVEL` | `INFO` | Python logging level. |
| `GHOST_ROOM_ID` | `""` | Unused in current code. |
| `DEST_CHANNEL_ID` | `""` | Unused in current code. |

---

## 15. Troubleshooting

### Bot won't start

- **Missing required env vars:** The process exits with `[FATAL] Missing required environment variables: ...`. Set all four required vars.
- **Invalid session:** `RuntimeError: Telethon session is not authorized.` Regenerate `SESSION_STRING` on a local machine.
- **Helper bot login failed:** `RuntimeError: Helper bot login failed.` Check that `BOT_TOKEN` is a valid bot token from BotFather.

### Inline panels not working

- Inline UI requires `BOT_TOKEN` to be set. Without it, commands fall back to plain-text edits.
- Check that the helper bot username is correctly resolved at startup (look for `Helper bot connected: @username` in logs).
- If the helper bot disconnects, the watchdog attempts recovery. After 3 consecutive failures, it marks the helper as permanently failed.

### Database not persisting

- If `SUPABASE_URL` or `SUPABASE_SERVICE_ROLE_KEY` is not set, the bot uses in-memory fallback. Data does not persist across restarts.
- Check logs for `Supabase env vars not set — using in-memory fallback.` or `Supabase init failed`.

### Bio not updating

- Verify `is_active=True` with `.bio show`.
- Check that the timezone is valid. Invalid timezones fall back to UTC.
- The cron deduplicates — if the rendered bio string hasn't changed, the API call is skipped.
- `FloodWaitError` is caught and slept. Check logs for `Bio FloodWait Ns — sleeping.`

### Health check failing

- The `/health` endpoint returns a snapshot of runtime state. If `status` is `down`, check `telethon_connected` and `supervisor_ok`.
- If the supervisor has exhausted all recovery attempts, it calls `sys.exit(1)` and Render restarts the process.

---

## 16. Known Limitations

1. **Save code generation is not atomic across restarts.** `get_next_save_code()` counts rows and returns `S{count+1}`. The `asyncio.Lock` prevents concurrent saves within a single process, but restarts between count and insert can cause duplicate codes. Falls back to a random alphanumeric code on collision.

2. **`get_or_create_bio_state()` race condition.** SELECT then INSERT is not atomic. Concurrent calls can cause UNIQUE constraint violations on `owner_id`.

3. **Web API hardcodes `owner_id=0`.** All API endpoints pass `owner_id=0`, making the dashboard only useful for testing. This is a placeholder for the single-owner bot design.

4. **No `updated_at` auto-update trigger on `bio_state`.** The column has a default of `now()` but no trigger. Some update calls include `updated_at` manually; others do not.

5. **Missing CHECK constraints in the authoritative migration.** The initial migration has `CHECK (save_type IN ('forward', 'deep'))` and `CHECK (level IN ('INFO', 'WARN', 'ERROR'))` but the authoritative migration lacks them.

6. **Synchronous Supabase calls block the event loop.** The `supabase-py` client uses `httpx` in synchronous mode, blocking the asyncio loop during HTTP round-trips.

7. **No GIN index on `tags` array.** The `tags` column is `text[]` but has no GIN index for array operations.

8. **`saved_items` grows indefinitely.** Rows are never deleted by the application (except via `.del <code>` or `.db clean`). No TTL or retention policy.

9. **Two conflicting migrations.** The initial and authoritative migrations have different schemas and RLS policies. If both run in sequence, the initial migration's CHECK constraints persist while the authoritative migration's RLS policies override.

10. **RLS SELECT policies are fully open.** All tables have `SELECT ... USING (true)` for `anon` + `authenticated`. Anyone with the anon key can read all data.

11. **`GHOST_ROOM_ID` and `DEST_CHANNEL_ID` are unused.** Loaded by `config.py` but never consumed by any code.

12. **`DATABASE_URL` is loaded but never consumed.** Intended for direct PostgreSQL connection but not implemented.

---

## 17. Future Architecture

### Potential Improvements

- **PostgreSQL sequence for save codes:** Replace the count-based `get_next_save_code()` with a PostgreSQL sequence to guarantee atomicity across restarts.
- **UPSERT for bio_state:** Replace the SELECT-then-INSERT pattern with `INSERT ... ON CONFLICT DO NOTHING` to eliminate race conditions.
- **Async Supabase client:** Switch to the async `supabase-py` client or run synchronous calls in a thread executor to avoid blocking the event loop.
- **GIN index on tags:** Add `CREATE INDEX ... USING GIN (tags)` for efficient array queries.
- **`updated_at` trigger:** Add a `BEFORE UPDATE` trigger on `bio_state` to auto-set `updated_at`.
- **Multi-owner support:** Replace the hardcoded `owner_id=0` in the web API with proper authentication.
- **Direct PostgreSQL connection:** Use `DATABASE_URL` for a connection pool, reducing PostgREST overhead.
- **Realtime dashboard:** Replace 30-second polling with Supabase Realtime subscriptions.
- **Storage integration:** Use Supabase Storage for media backups instead of Telegram Saved Messages.
