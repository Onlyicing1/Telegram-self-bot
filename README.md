# LifeOS — Telegram Self-Bot

A production-grade **Telegram self-bot** (userbot) that turns your own Telegram account into a personal operating system. Save anything, search instantly, automate your profile bio and username, and keep your data organized — all through an interactive inline-button UI driven by a single headless Python process.

Built on **Telethon** + **Supabase** + **FastAPI** + **React**, deployed on **Render**.

---

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Features](#features)
   - [Inline Glass UI](#inline-glass-ui)
   - [Save System](#save-system)
   - [Delete System](#delete-system)
   - [Bio Engine](#bio-engine)
   - [Username Engine](#username-engine)
   - [Scheduler](#scheduler)
   - [Runtime Supervisor](#runtime-supervisor)
   - [Watchdog](#watchdog)
   - [Diagnostics](#diagnostics)
   - [Helper Bot](#helper-bot)
   - [Supabase Support](#supabase-support)
   - [Render Deployment](#render-deployment)
4. [Database](#database)
5. [Quick Start](#quick-start)
6. [Environment Variables](#environment-variables)
7. [Helper Bot Setup](#helper-bot-setup)
8. [Commands](#commands)
9. [Troubleshooting](#troubleshooting)

---

## Overview

LifeOS is a **self-bot** — it operates *your own* Telegram account via Telethon's `StringSession`. There is no separate bot account for commands. You type commands (`.save`, `.bio`, `.help`) in any chat, and the bot edits your message in-place with the result. Zero spam, zero new messages.

When a helper bot token is configured, the full **Inline Glass UI** becomes available — interactive inline-button panels for every feature, replacing plain-text commands with a tap-to-navigate interface.

### Key Highlights

- **Headless** — runs as a single `asyncio` process, no interactive login.
- **Self-healing** — a runtime supervisor with watchdog automatically detects disconnections and rebuilds the client.
- **Resilient** — degrades gracefully when Supabase is unavailable (in-memory fallback).
- **Zero-spam** — all command responses edit the triggering message in-place.
- **Owner-only** — every command and callback is gated by a single permission check.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    backend/main.py                            │
│                  (asyncio entry point)                        │
│                                                              │
│  ┌──────────────┐  ┌───────────────┐  ┌──────────────────┐   │
│  │  Telethon     │  │  FastAPI      │  │  Profile         │   │
│  │  Self-Client  │  │  Web Server   │  │  Scheduler       │   │
│  │  (StringSess) │  │  (Uvicorn)    │  │  (asyncio task)  │   │
│  └──────┬────────┘  └──────┬────────┘  └────────┬─────────┘   │
│         │                  │                    │             │
│  ┌──────┴────────┐  ┌──────┴────────┐  ┌───────┴──────────┐   │
│  │  Bot Handlers  │  │  Web Routes   │  │  Bio Engine       │   │
│  │  (commands)    │  │  (/health,    │  │  Username Engine  │   │
│  │                │  │   /api/*)     │  │  (updaters)       │   │
│  └──────┬────────┘  └───────────────┘  └──────────────────┘   │
│         │                                                    │
│  ┌──────┴──────────────────────────────────────────────────┐ │
│  │              Services Layer                               │ │
│  │  save_service, retrieve_service, delete_service,         │ │
│  │  bio_service, username_service, settings_service,        │ │
│  │  database_service, discover_service                      │ │
│  └──────┬──────────────────────────────────────────────────┘ │
│         │                                                    │
│  ┌──────┴───────┐  ┌───────────────┐  ┌──────────────────┐   │
│  │  DB Client    │  │  Helper Bot   │  │  Runtime          │   │
│  │  (Supabase)   │  │  (Telethon)   │  │  Supervisor       │   │
│  └───────────────┘  └───────────────┘  └──────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

The entire application runs as a single Python `asyncio` process. Telethon, Uvicorn, the profile scheduler, the watchdog, and the heartbeat all share one event loop. No threads, no multiprocessing.

---

## Features

### Inline Glass UI

The Glass Panel system provides interactive inline-button panels for all commands and settings. It replaces the old plain-text command interface with a tap-to-navigate experience.

- **Panel types**: Help menu, Context panel, Settings, Health dashboard, Diagnostics, Logs, Save, Retrieve, Database, Discover, Bio Engine, Username Engine.
- **Navigation**: hierarchical Back / Home / Close navigation with session tracking per chat.
- **Auto-close**: panels automatically close after a configurable timeout (default 120 seconds).
- **Input mode**: panels that need user input (save codes, bio text, settings values) transition to an input state and listen for the owner's next message.
- **Template builder**: visual sequential template builder for Bio and Username engines — tap variables to append them in order.
- **Fallback**: when no helper bot is configured, all commands fall back to plain-text edit-in-place mode.

### Save System

The Save Engine provides two modes for preserving media to Saved Messages with full metadata:

- **Forward save** (`.save f`) — forwards the replied message to Saved Messages instantly using Telegram's native forward API. No download required.
- **Deep save** (`.save d`) — downloads the media, re-uploads to Saved Messages with a rich caption (sender, chat ID, message ID, timestamp, media type, MIME, size, filename, tags). Enforces a configurable file size limit (default 50 MB).
- **Link save** — save media from a Telegram message link (`https://t.me/channel/123` or `https://t.me/c/123/456`).
- **Metadata persistence** — every save records full metadata in the `saved_items` Supabase table: save code, type, origin/saved chat and message IDs, sender info, MIME type, file ID, file size, media type, tags, caption, and timestamp.
- **Save codes** — compact, human-readable codes (e.g. `S0001`) generated atomically with collision detection.

### Delete System

Multi-mode message deletion with batch processing:

- **Delete last N** (`.del <n>`) — deletes the last N outgoing messages in the current chat (1–500 range).
- **Delete from message ID** (`.del id <msgid>`) — deletes all outgoing messages from a given message ID forward.
- **Delete by save code** (`.del <code>`) — deletes a saved item's Telegram message and its database row in one operation.
- **Reply mode** — reply to any message to select it as the deletion starting point.
- **Recent messages browser** — paginated inline list of recent outgoing messages for visual selection.
- **Batch deletion** — deletes in configurable batch sizes (default 100) to avoid hitting Telegram API limits.

### Bio Engine

A timezone-synchronized cron that rewrites your Telegram profile bio ("about" field) every minute using a template with `{time}`, `{mood}`, and `{text}` tokens.

- Fires exactly at each minute boundary (not a fixed interval).
- Deduplicates — skips the API call when the bio string hasn't changed.
- Handles `FloodWaitError` by sleeping the exact wait time.
- Never terminates on recoverable errors — retries on the next tick.
- Self-stopping — if `is_active` becomes `False` in the database, the loop exits on the next tick.
- Fully inline — configure template, mood, text, and on/off state through the Glass UI.
- State persisted in the `bio_state` Supabase table (one row per owner).

### Username Engine

Mirrors the Bio Engine exactly, but controls the Telegram `first_name` field instead of the `about` field.

- Completely independent from the Bio Engine — each registers a separate updater.
- Never shares runtime state with the Bio Engine.
- Same template tokens: `{time}`, `{mood}`, `{text}`.
- Same deduplication, flood-wait handling, and self-stopping behavior.
- State persisted in the `username_state` Supabase table (one row per owner).
- Fully inline configuration through the Glass UI.

### Scheduler

A shared **Profile Scheduler** (`backend/profile/scheduler.py`) fires once per minute at `HH:MM:00` and calls all registered profile updaters in a single pass.

- Both the Bio Engine and Username Engine register updaters with the scheduler.
- The scheduler collects each engine's desired profile fields and sends a **single** `UpdateProfileRequest` to Telegram per minute — never more.
- Adding a new profile engine is as simple as calling `register_updater`.
- Exponential backoff with jitter on crashes; automatic restart.
- Bounded API timeouts (30 seconds) to prevent event-loop stalls.

### Runtime Supervisor

The `RuntimeSupervisor` (`backend/runtime/supervisor.py`) is the self-healing core that owns every runtime coroutine.

- **FSM states**: STARTING → CONNECTING → AUTHORIZING → REGISTERING → READY → DEGRADED → RECOVERING → REBUILDING → STOPPING → FAILED.
- **Atomic recovery**: lock-protected, single execution. Recovery sequence: stop cron engines → stop helper → clear panel state → cancel orphan tasks → dispose dead client → rebuild → re-register handlers → resume cron engines → verify with heartbeat.
- **Limited retries**: exponential backoff with jitter. After 5 failed recovery attempts, the process exits with code 1 so Render restarts it automatically.
- **Exactly one active self-client** at all times — a new client is only created after the old one is fully disposed.
- **Signal handling**: SIGTERM/SIGINT triggers deterministic shutdown of all tasks, cron engines, helper bot, and self-client.

### Watchdog

A dedicated watchdog task runs every 30 seconds and performs a real RPC (`get_me`) as the heartbeat.

- 3 consecutive heartbeat failures → client declared dead → recovery triggered.
- **Update staleness detection**: if no Telegram updates arrive for a configurable threshold (default 300 seconds) while RPC is still healthy, the update-receive loop is declared stalled and the client is rebuilt.
- Separately tracks heartbeat age, last RPC latency, last command, last update, last callback, and last event dispatch.
- The watchdog never interferes with recovery in progress — it skips checks while the recovery lock is held.

### Diagnostics

Comprehensive diagnostics for debugging and monitoring:

- **Event log** (`.logs`) — in-memory circular buffer of 500 events with module, action, duration, result, and details. Filterable by errors only or by module.
- **Diagnostic snapshot** (`.kill`) — full system snapshot: process info, runtime state, Telethon status, helper status, supervisor task states, bio engine, database, event loop tasks with stack traces, and recent events. Includes stalled-task recovery.
- **Asyncio task diagnostics** — dumps all running tasks with coroutine names, await points, awaited objects, and elapsed waiting times every 60 seconds. Detects event-loop stalls, deadlocks, task starvation, and slow event handlers.
- **Runtime heartbeat** — structured system snapshot every 30 seconds with memory, CPU, task count, event loop latency, update queue size, and age tracking for all critical timestamps.

### Helper Bot

A **separate** Telethon client that operates a real bot account (via `BOT_TOKEN`). It enables the full Inline Glass UI.

- **Inline buttons** — the Glass Panel system uses inline keyboard buttons, which require a real bot account (self-bots cannot send inline buttons).
- **Callback handling** — processes button presses via callback queries with session management, navigation stacks, and input-state tracking.
- **Panel lifecycle** — a `PanelLifecycleManager` owns all panel resources (sessions, timers, render caches, input state) with a single cleanup path for every exit route.
- **Optional** — if `BOT_TOKEN` is not set, the bot falls back to plain-text edit-in-place mode for all commands.
- **Auto-reconnect** — the helper bot supervisor reconnects with exponential backoff. After repeated failures, it marks the helper as permanently failed and stops retrying.

### Supabase Support

Supabase is the optional but recommended persistence layer. The bot is designed to run **with or without it**.

- **When available**: all data persists across restarts. The backend uses the service-role key, which bypasses RLS for all writes.
- **When unavailable**: all operations degrade to in-memory storage. The bot continues to function normally — every command works. Data does not persist across restarts.
- **Tables**: `saved_items`, `bio_state`, `username_state`, `bot_logs`, `panel_settings`.
- **RLS**: enabled on all tables. Only SELECT is granted to `anon` + `authenticated` (read-only dashboard access). No write policies for anon/authenticated.
- **Threaded DB calls**: all Supabase operations run in a thread with a bounded timeout (10 seconds) via `asyncio.to_thread()`, so the event loop never blocks on slow or stalled HTTP responses.
- **Settings service**: panel configuration is stored as typed columns on the `panel_settings` table with a cache-first read, write-through cache architecture, and per-setting validators.

### Render Deployment

The bot is designed for Render's Free tier and deploys as a single web service.

- **Procfile**: `web: python -m backend.main` — the start command.
- **Health check**: the FastAPI server exposes `/health` which returns the runtime health snapshot.
- **Environment variables**: all secrets are provided via Render's environment variable dashboard. The `render.yaml` Blueprint defines the service and all env vars.
- **Auto-restart**: if the runtime supervisor exhausts recovery attempts, it calls `sys.exit(1)` so Render restarts the process.
- **Dashboard**: the React dashboard is built with Vite and served by FastAPI from `dist/` if present. It polls the API every 30 seconds.

---

## Database

Five tables in the `public` schema:

| Table | Purpose |
|---|---|
| `saved_items` | Media save records (forward + deep) with full metadata |
| `bio_state` | Singleton-per-owner bio engine state — template, mood, text, is_active |
| `username_state` | Singleton-per-owner username engine state — template, mood, text, is_active |
| `bot_logs` | Structured activity log — level, message, JSONB context |
| `panel_settings` | Glass Panel configuration — 13 typed columns (auto-close, limits, diagnostics, etc.) |

The Bio Engine and Username Engine have **completely independent persistence** — separate tables, separate state, separate updaters. They never share runtime state. The only coupling is that the shared Profile Scheduler merges their outputs into a single `UpdateProfileRequest` API call per minute.

All tables have RLS enabled. SELECT is granted to `anon` + `authenticated` (dashboard reads). All writes use the service-role key, which bypasses RLS.

For the full schema reference, see [DATABASE_ARCHITECTURE.md](DATABASE_ARCHITECTURE.md).

---

## Quick Start

### Prerequisites

- Python 3.11+
- Node.js 18+ (for dashboard build)
- A Telegram account with API credentials
- A Supabase project (optional — bot works without it)
- A Telegram bot token from BotFather (optional — for Inline Glass UI)

### 1. Clone and Install

```bash
git clone <repo-url>
cd lifeos
pip install -r backend/requirements.txt
npm install
```

### 2. Generate Session String

Run this locally **once** to generate your session string:

```python
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

api_id = YOUR_API_ID
api_hash = 'YOUR_API_HASH'

with TelegramClient(StringSession(), api_id, api_hash) as client:
    print(client.session.save())
```

Copy the printed string — this is your `SESSION_STRING`.

### 3. Configure Environment

Create a `.env` file (or set env vars on Render):

```env
API_ID=12345
API_HASH=your_api_hash
SESSION_STRING=your_session_string
BOT_OWNER_ID=123456789
BOT_TOKEN=your_bot_token          # Optional — for Inline Glass UI
SUPABASE_URL=your_supabase_url    # Optional
SUPABASE_SERVICE_ROLE_KEY=your_key # Optional
TZ=Asia/Tehran
```

### 4. Run

```bash
python -m backend.main
```

### 5. Build Dashboard (Optional)

```bash
npm run build
```

The built dashboard is served by FastAPI at `/`.

---

## Environment Variables

### Required

| Variable | Type | Description |
|---|---|---|
| `API_ID` | int | Telegram API ID from my.telegram.org |
| `API_HASH` | str | Telegram API Hash from my.telegram.org |
| `SESSION_STRING` | str | Telethon StringSession (generated offline) |
| `BOT_OWNER_ID` | int | Telegram numeric user ID of the bot owner |

### Optional

| Variable | Default | Description |
|---|---|---|
| `BOT_TOKEN` | `""` | Helper bot token for Inline Glass UI |
| `SUPABASE_URL` | `""` | Supabase project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | `""` | Supabase service role key |
| `DATABASE_URL` | `""` | PostgreSQL connection string (unused) |
| `TZ` | `Asia/Tehran` | Timezone for bio/username engines and timestamps |
| `PORT` | `8000` | Web server port |
| `BIO_UPDATE_ENABLED` | `false` | Auto-start bio cron on boot |
| `LOG_LEVEL` | `INFO` | Python logging level |

---

## Helper Bot Setup

The Inline Glass UI requires a **separate** Telegram bot account:

1. Create a new bot via [@BotFather](https://t.me/BotFather).
2. Copy the bot token.
3. Set `BOT_TOKEN` in your environment.
4. Start the bot — the helper bot connects automatically and inline panels become available.

Without `BOT_TOKEN`, all commands fall back to plain-text edit-in-place mode.

---

## Commands

All commands use the `.` prefix. Commands only fire on outgoing messages (sent from the owner's own account).

### Utility

| Command | Description |
|---|---|
| `.ping` | PONG |
| `.id` | Chat & Message IDs |
| `.help` | Interactive help panel (Inline Glass UI) |
| `.panel` | Context panel for replied message |
| `.health` | Health dashboard |
| `.kill` | Diagnostic snapshot + recovery |
| `.logs` | Event log viewer |
| `.logs 50` | Last 50 events |
| `.logs errors` | Errors only |

### Save Engine

| Command | Description |
|---|---|
| `.save f` | Forward save to Saved Messages |
| `.save d` | Deep save (download + re-upload) |
| `.save` | Save panel (Inline Glass UI) |

### Retrieve & Discover

| Command | Description |
|---|---|
| `.retrieve` / `.r` / `.files` | Browse saved items (Inline Glass UI) |
| `.preview <code>` | Show metadata for a saved item |
| `.send <code>` | Forward saved asset to current chat |
| `.list [n]` | Show recent saved items (default 10) |
| `.find <text>` | Search saved items by code, filename, caption, or MIME |

### Delete

| Command | Description |
|---|---|
| `.del <n>` | Delete last N outgoing messages |
| `.del id <msgid>` | Delete from message ID forward |
| `.del <code>` | Delete a saved item (Telegram message + DB row) |
| `.del` | Delete panel (Inline Glass UI) |

### Bio Engine

| Command | Description |
|---|---|
| `.bio` | Bio engine panel (Inline Glass UI) |
| `.bio on` | Start bio cron |
| `.bio off` | Stop bio cron |
| `.bio show` | Show bio state |
| `.bio template <tpl>` | Set bio template |
| `.bio text <text>` | Set {text} token |
| `.bio mood <mood>` | Set {mood} token |

### Username Engine

| Command | Description |
|---|---|
| `.username` | Username engine panel (Inline Glass UI) |
| `.username on` | Start username cron |
| `.username off` | Stop username cron |
| `.username show` | Show username state |
| `.username template <tpl>` | Set username template |
| `.username text <text>` | Set {text} token |
| `.username mood <mood>` | Set {mood} token |

### Database

| Command | Description |
|---|---|
| `.db` | Database panel (Inline Glass UI) |
| `.db clean` | Remove orphan rows |
| `.db stats` | Database statistics |
| `.db vacuum` | Cleanup + optimize |

---

## Troubleshooting

### Bot won't start

- Check that all required env vars are set (`API_ID`, `API_HASH`, `SESSION_STRING`, `BOT_OWNER_ID`).
- Check that the session string is valid (regenerate if needed).
- Check logs for connection errors.

### Panels not working

- Ensure `BOT_TOKEN` is set — the Inline Glass UI requires the helper bot.
- Without `BOT_TOKEN`, commands fall back to plain-text edit-in-place.

### Bio or Username engine not updating

- Check that the engine is active (`.bio show` or `.username show`).
- Check that the template contains at least one token (`{time}`, `{mood}`, `{text}`).
- Check that the shared Profile Scheduler is running (visible in `.health`).
- Check for `FloodWaitError` in logs — Telegram may be rate-limiting profile updates.

### Database errors

- The bot works without Supabase — all operations fall back to in-memory.
- Check that `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` are set correctly.
- Check that all migrations have been applied.

### Client keeps disconnecting

- The watchdog automatically detects disconnections and rebuilds the client.
- Check `.health` for the restart count and last rebuild reason.
- Check `.kill` for a full diagnostic snapshot.
- If recovery fails repeatedly, the process exits and Render restarts it.

---

## License

This project is for personal use. See the repository for details.
