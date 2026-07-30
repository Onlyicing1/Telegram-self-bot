# LifeOS — Telegram Self-Bot

A headless Telegram **userbot** that operates your own Telegram account via Telethon's `StringSession`. It runs as a single Python `asyncio` process on Render, with a FastAPI health-check server and an optional React dashboard.

---

## What It Does

LifeOS turns your Telegram account into a personal operating system with five subsystems:

| Subsystem | Description |
|---|---|
| **Save Engine** | Forward or deep-save (download + re-upload) media to Saved Messages with structured metadata. |
| **Bio Engine** | A timezone-synced cron that rewrites your profile bio every minute using `{time}`, `{mood}`, `{text}` tokens. |
| **Discover** | Browse recent saves and search by caption, filename, save code, or MIME type. |
| **Organizer** | Data overview, log cleanup, and multi-message deletion. |
| **Diagnostics** | Health dashboard, event log, and stalled-task recovery via `.kill`. |

An optional **helper bot** (separate BotFather token) provides interactive inline UI panels for all commands.

---

## Tech Stack

- **Backend:** Python 3.11, Telethon 1.34, FastAPI 0.111, Uvicorn 0.29, Supabase 2.4
- **Frontend:** React 18, Vite 5, Tailwind CSS 3
- **Database:** Supabase (PostgreSQL via PostgREST) with in-memory fallback
- **Deployment:** Render (Free tier)

---

## Quick Start

### Prerequisites

- Python 3.11+
- Node.js 18+ (for dashboard build)
- Telegram API credentials from [my.telegram.org](https://my.telegram.org)
- A Supabase project (optional — bot works without it)
- A bot token from [@BotFather](https://t.me/BotFather) (optional — for inline panels)

### Steps

1. **Clone:**

   ```bash
   git clone https://github.com/Onlyicing18/Telegram-self-bot.git
   cd Telegram-self-bot
   ```

2. **Generate a StringSession** on a local machine:

   ```python
   from telethon import TelegramClient
   from telethon.sessions import StringSession

   client = TelegramClient(StringSession(), API_ID, API_HASH)
   client.start(phone="+your_number")
   print(client.session.save())
   ```

3. **Install Python deps:**

   ```bash
   pip install -r backend/requirements.txt
   ```

4. **Set environment variables** (see [Environment Variables](#environment-variables)).

5. **Apply database migrations** (if using Supabase) — run each file in `supabase/migrations/` in order via the Supabase SQL Editor.

6. **Build the dashboard** (optional):

   ```bash
   npm install && npm run build
   ```

7. **Run:**

   ```bash
   python -m backend.main
   ```

---

## Architecture

```
Single Python Process (asyncio event loop)
├── RuntimeSupervisor (watchdog + self-healing)
├── Telethon Self-Client (StringSession)
│   └── Command Handlers (misc, save, retrieve, delete, organize, bio, discover, database)
├── Helper Bot Client (optional, BOT_TOKEN)
│   └── Inline Panel System (callback routing, navigation, auto-close timer)
├── Bio Cron Engine (per-minute bio updates)
├── FastAPI Web Server (health check + read-only API)
│   └── React Dashboard (if dist/ exists)
└── Service Layer (business logic)
    └── db/client.py (Supabase singleton + in-memory fallback)
```

### Key Design Principles

- **Single asyncio process.** All subsystems share one event loop.
- **Service-role key.** Backend writes to Supabase via the service-role key, bypassing RLS.
- **In-memory fallback.** If Supabase is unavailable, the bot degrades to a Python dict and continues functioning.
- **Edit-first policy.** Command responses edit the triggering message — zero new messages sent.
- **Self-healing.** A `RuntimeSupervisor` performs real RPC heartbeats every 30 seconds and rebuilds the client atomically on failure (max 5 attempts, then `sys.exit(1)` so Render restarts).

---

## Runtime Supervisor

The `RuntimeSupervisor` owns every runtime coroutine and provides self-healing:

- **Heartbeat:** Every 30 seconds, performs a real RPC (`client.get_me()`).
- **Failure detection:** 3 consecutive heartbeat failures trigger atomic recovery.
- **Recovery sequence:** Stop bio cron → stop helper → clear all panel state → cancel tasks → dispose dead client → backoff sleep → build new client → re-register handlers → restart helper/bio → verify with fresh heartbeat → resume run loop.
- **Backoff:** Exponential, base 2s, max 300s, 30% jitter. Max 5 attempts.
- **State machine:** STARTING → CONNECTING → AUTHORIZING → REGISTERING → READY (or DEGRADED/RECOVERING/REBUILDING/STOPPING/FAILED).

### ManagedTask

A supervised asyncio task wrapper with automatic restart. If the task exits unexpectedly, a watchdog respawns it after a configurable delay.

---

## Helper Panel System

When `BOT_TOKEN` is set, the bot gains interactive inline UI panels. Sending a command like `.save` without arguments opens a panel with buttons. The self-bot triggers inline mode on the helper bot, auto-sends the first result, and the helper bot's callback handler manages all subsequent button interactions.

### Panel Lifecycle

```
Create → Render → Wait → Action/Input → Update → Back/Home/Close → Destroy
```

- **Navigation:** Root menu shows only Close. Submenus show Back, Home, and Close.
- **Auto-close:** Panels auto-close after a configurable delay with a countdown timer.
- **Input capture:** When a user taps an "Enter Code" button, the next outgoing message in that chat is captured as the input value.
- **Session management:** Each panel has a navigation stack of `(panel_id, extra)` tuples.

### Components

| Component | Role |
|---|---|
| `helper/client.py` | Creates the helper bot Telethon client. Returns `None` if `BOT_TOKEN` is unset. |
| `helper/inline_engine.py` | Triggers inline mode and handles `InlineQuery` events on the helper bot. |
| `helper/inline_sender.py` | Orchestrates panel creation/updates. Listens for pending input. |
| `helper/panels.py` | Central callback router with navigation stack and render dedup. |
| `helper/panel_render.py` | Builds inline results and edit-message button layouts. |
| `helper/panel_timer.py` | Auto-close countdown timer. |
| `helper/session_manager.py` | Panel session state with nav stack. |
| `helper/input_state.py` | Pending text input tracking with timeout. |
| `helper/target_context.py` | Reply target abstraction (5-minute expiry). |
| `helper/pagination.py` | Reusable Prev/Next paginator. |
| `helper/context.py` | Callback data encoding (64-byte limit). |
| `helper/callback_trace.py` | Per-callback trace ID logger. |
| `helper/watchdog.py` | Helper bot health monitor (3 failures → permanent failure). |
| `helper/panel_selftest.py` | Deterministic pipeline verification. |

---

## Command Reference

All commands use the `.` prefix and fire on outgoing messages only. Non-owner messages are silently ignored.

### General

| Command | Behavior |
|---|---|
| `.ping` | Edits your message to `PONG`. |
| `.id` | Shows Chat ID + Msg ID. If replying, also shows Reply Msg ID + Sender ID. |
| `.help` | Opens an interactive help panel (or plain text without helper bot). |
| `.panel` | Opens a context panel for the replied message. |
| `.health` | Shows full health dashboard. |
| `.kill` | Diagnostic snapshot + stalled-task recovery. |
| `.logs` | View recent diagnostic events. Supports `.logs <n>`, `.logs errors`. |

### Save Engine

| Command | Behavior |
|---|---|
| `.save f` / `.s f` | Forward save — forwards replied message to Saved Messages. |
| `.save d` / `.s d` | Deep save — downloads media, re-uploads to Saved Messages with rich caption. |
| `.save` / `.s` | Opens inline panel to choose forward or deep. |

### Retrieve

| Command | Behavior |
|---|---|
| `.preview <code>` / `.r <code>` | Shows stored metadata for a save code. |
| `.send <code>` | Forwards the saved asset to the current chat. Deletes command on success. |

### Delete

| Command | Behavior |
|---|---|
| `.del <n>` | Deletes last `n` outgoing messages (1–500). |
| `.del id <msgid>` | Deletes from message ID forward. Batched by `delete_batch_size`. |
| `.del <code>` | Deletes a saved item (Telegram message + DB row). |

### Organizer

| Command | Behavior |
|---|---|
| `.organize list` | Shows save counts, log count, bio status. |
| `.organize clean` | Purges logs older than `log_retention_days`. |

### Bio Engine

| Command | Behavior |
|---|---|
| `.bio on` | Activates bio cron, shows preview. |
| `.bio off` | Deactivates bio cron. |
| `.bio template <tpl>` | Sets bio template (`{time}`, `{mood}`, `{text}` tokens). |
| `.bio mood <mood>` | Sets mood value. |
| `.bio text <text>` | Sets custom text value. |
| `.bio show` | Shows full bio state. |
| `.bio help` | Shows token reference and command list. |

### Discover

| Command | Behavior |
|---|---|
| `.list [n]` | Shows recent saves (default 10, max 50). |
| `.find <text>` | Searches saves by caption, filename, code, or MIME type. |

### Database Maintenance

| Command | Behavior |
|---|---|
| `.db clean` | Removes orphan rows from `saved_items`. |
| `.db stats` | Shows database statistics. |
| `.db vacuum` | Cleanup orphans + report. |

---

## Settings

All configurable values are stored in the `panel_settings` table (single row, `key='global'`). The database is the source of truth; an in-memory cache provides fast reads.

| Setting | Default | Range | Used By |
|---|---|---|---|
| `auto_close_enabled` | `true` | bool | Panel auto-close toggle |
| `auto_close_delay_seconds` | `120` | 10–3600 | Panel auto-close delay |
| `max_deep_save_mb` | `50` | 1–500 | Deep save file size limit |
| `delete_batch_size` | `100` | 1–1000 | Messages per delete API call |
| `log_retention_days` | `7` | 1–365 | Log cleanup age |
| `panel_countdown_interval` | `30` | 5–120 | Countdown re-render interval |
| `input_timeout_seconds` | `120` | 10–600 | Pending input expiry |

Settings are read at runtime via typed accessors with range validation. Updates write to the DB via upsert, then refresh the cache.

---

## Web API

| Method | Path | Returns |
|---|---|---|
| GET | `/health` | Runtime health snapshot |
| GET | `/api/saves?limit=&offset=` | Paginated saved items |
| GET | `/api/saves/{code}` | Single saved item |
| GET | `/api/bio` | Bio engine state |
| GET | `/api/settings` | All settings |
| GET | `/api/logs?limit=` | Recent log entries |
| GET | `/` | React dashboard (if built) or status JSON |

Swagger/ReDoc docs are disabled in production.

---

## Environment Variables

### Required

| Variable | Description |
|---|---|
| `API_ID` | Telegram API ID from my.telegram.org |
| `API_HASH` | Telegram API Hash from my.telegram.org |
| `SESSION_STRING` | Telethon StringSession (generated offline) |
| `BOT_OWNER_ID` | Your Telegram numeric user ID |

### Optional

| Variable | Default | Description |
|---|---|---|
| `BOT_TOKEN` | `""` | Helper bot token. Enables inline panels. |
| `SUPABASE_URL` | `""` | Supabase project URL. Empty = in-memory fallback. |
| `SUPABASE_SERVICE_ROLE_KEY` | `""` | Supabase service-role key. Bypasses RLS. |
| `DATABASE_URL` | `""` | Loaded but not consumed. Reserved for future use. |
| `TZ` | `Asia/Tehran` | Timezone for bio engine and timestamps. |
| `PORT` | `8000` | Web server port. |
| `BIO_UPDATE_ENABLED` | `false` | Auto-start bio cron on boot. |
| `LOG_LEVEL` | `INFO` | Python logging level. |

---

## Deployment on Render

1. Connect your GitHub repo to Render.
2. Render builds from `backend/requirements.txt`.
3. Render starts `python -m backend.main` (per `Procfile`).
4. Health check hits `/health` — must return 200.
5. The Telethon client connects and the bot is live.

The React dashboard is not built automatically. Run `npm run build` locally to produce `dist/`, or add a build step in Render.

---

## Troubleshooting

| Problem | Solution |
|---|---|
| Bot won't start | Check all 4 required env vars are set. |
| Invalid session | Regenerate `SESSION_STRING` on a local machine. |
| Inline panels not working | Set `BOT_TOKEN`. Without it, commands fall back to plain text. |
| Database not persisting | Set `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`. Without them, data is in-memory only. |
| Bio not updating | Check `.bio show` — verify `is_active=true` and timezone is valid. |
| Health check failing | If supervisor exhausted recovery attempts, it calls `sys.exit(1)` and Render restarts. |

---

## Known Limitations

1. Save code generation counts rows — not atomic across restarts (falls back to random codes on collision).
2. `get_or_create_bio_state()` has a SELECT-then-INSERT race condition.
3. Web API hardcodes `owner_id=0` — dashboard is for testing only.
4. No `updated_at` auto-update trigger on `bio_state`.
5. Synchronous Supabase calls block the event loop.
6. No GIN index on `tags` array column.
7. `saved_items` grows indefinitely — no retention policy.
8. RLS SELECT policies are fully open (`USING (true)`) — anyone with the anon key can read all data.
9. `DATABASE_URL`, `GHOST_ROOM_ID`, and `DEST_CHANNEL_ID` are loaded but never consumed.
