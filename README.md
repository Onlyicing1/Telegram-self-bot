# LifeOS — Telegram Self-Bot

A production-grade **Telegram self-bot** (userbot) that turns your own Telegram account into a personal operating system. Save anything, search instantly, automate your profile bio, and keep your data organized — all from a single headless Python process.

Built on **Telethon** + **Supabase** + **FastAPI** + **React**, deployed on **Render**.

---

## Table of Contents

1. [What Is LifeOS?](#what-is-lifeos)
2. [Architecture](#architecture)
3. [Runtime Supervisor](#runtime-supervisor)
4. [Self Client](#self-client)
5. [Helper Bot](#helper-bot)
6. [Panel System](#panel-system)
7. [Navigation](#navigation)
8. [Callback Engine](#callback-engine)
9. [Session System](#session-system)
10. [Database Layer](#database-layer)
11. [Bio Engine](#bio-engine)
12. [Save Engine](#save-engine)
13. [Retrieve](#retrieve)
14. [Organizer](#organizer)
15. [Diagnostics](#diagnostics)
16. [Settings](#settings)
17. [Database Panel](#database-panel)
18. [Database](#database)
19. [Quick Start](#quick-start)
20. [Environment Variables](#environment-variables)
21. [Helper Bot Setup](#helper-bot-setup)
22. [Deploying on Render](#deploying-on-render)
23. [Commands](#commands)
24. [Troubleshooting](#troubleshooting)

---

## What Is LifeOS?

LifeOS is a **self-bot** — it operates *your own* Telegram account via Telethon's `StringSession`. There is no separate bot account. You type commands (`.save f`, `.bio on`, `.help`) in any chat, and the bot edits your message in-place with the result. Zero spam, zero new messages.

### Features

- **Save Engine** — Forward-save or deep-save (download + re-upload) any media to Saved Messages with full metadata.
- **Bio Engine** — A timezone-synced cron that rewrites your profile bio every minute using `{time}`, `{mood}`, `{text}` tokens.
- **Discovery** — Full-text search across captions, filenames, save codes, and MIME types.
- **Organizer** — Data overview, log cleanup, multi-message deletion.
- **Glass Panel** — Interactive inline-button panels for all commands, settings, and diagnostics.
- **Diagnostics** — Health dashboard, stalled-task recovery, event logging.

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    backend/main.py                        │
│                  (asyncio entry point)                   │
│                                                          │
│  ┌─────────────┐  ┌──────────────┐  ┌─────────────────┐  │
│  │  Telethon    │  │  FastAPI     │  │  Bio Cron       │  │
│  │  Self-Client  │  │  Web Server  │  │  Engine         │  │
│  │  (StringSess) │  │  (Uvicorn)   │  │  (asyncio task) │  │
│  └──────┬───────┘  └──────┬───────┘  └────────┬────────┘  │
│         │                 │                   │          │
│  ┌──────┴───────┐  ┌──────┴───────┐  ┌────────┴────────┐  │
│  │  Bot Handlers │  │  Web Routes  │  │  Bio Engine     │  │
│  │  (commands)    │  │  (/health,   │  │  (render_bio,   │  │
│  │                │  │   /api/*)    │  │   cron loop)    │  │
│  └──────┬───────┘  └──────────────┘  └─────────────────┘  │
│         │                                                │
│  ┌──────┴───────────────────────────────────────────────┐ │
│  │              Services Layer                           │ │
│  │  (save_service, retrieve_service, delete_service,    │ │
│  │   organize_service, bio_service, settings_service,   │ │
│  │   database_service, discover_service)                │ │
│  └──────┬───────────────────────────────────────────────┘ │
│         │                                                │
│  ┌──────┴───────┐  ┌──────────────┐  ┌─────────────────┐  │
│  │  DB Client   │  │  Helper Bot  │  │  Runtime         │  │
│  │  (Supabase)  │  │  (Telethon)  │  │  Supervisor      │  │
│  └──────────────┘  └──────────────┘  └─────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

---

## Runtime Supervisor

The runtime supervisor (`backend/runtime/supervisor.py`) manages long-running
asyncio tasks (bio cron, watchdog, heartbeat) with automatic restart on
failure. It provides:

- **Task lifecycle management** — start, stop, monitor.
- **Automatic restart** — failed tasks are restarted with exponential
  backoff.
- **Health snapshots** — exposes task state for the health dashboard.

---

## Self Client

The self-client is the main Telethon `TelegramClient` that operates the
owner's Telegram account. It uses a `StringSession` (never file-based).

- Created in `backend/bot/client.py`.
- Connects and authorizes at startup.
- Runs until disconnected via `client.run_until_disconnected()`.

---

## Helper Bot

The helper bot is a **separate** Telethon client that operates a real bot
account (via `BOT_TOKEN`). It provides:

- **Inline buttons** — the Glass Panel system uses inline keyboard buttons,
  which require a real bot account (self-bots cannot send inline buttons).
- **Panel rendering** — sends and edits messages with inline keyboards.
- **Callback handling** — processes button presses via callback queries.

The helper bot is optional — if `BOT_TOKEN` is not set, the bot falls back
to plain-text edit-in-place mode for all commands.

---

## Panel System

The Glass Panel system provides interactive inline-button panels for all
commands and settings. It replaces the old plain-text command interface.

### Panel Types

| Panel | Trigger | Purpose |
|---|---|---|
| Help | `.help` | Main menu with category buttons |
| Context | `.panel` | Actions on a replied message (save, preview) |
| Settings | Settings button in Help | View and toggle all settings |
| Health | `.health` | Health dashboard with refresh |
| Diagnostics | `.kill` | Diagnostic snapshot + recovery |
| Logs | `.logs` | Event log viewer with filters |
| Save | `.save` | Save engine panel (forward/deep) |
| Retrieve | `.preview` / `.send` | Preview and send saved items |
| Database | `.db` | Database management panel |
| Discover | `.discover` | Search saved items |

### Navigation

Panels use a hierarchical navigation model:

- **Home** — returns to the main Help menu.
- **Close** — closes the current panel.
- **Back** — returns to the parent panel.

Navigation buttons are always at the bottom of the panel.

---

## Callback Engine

The callback engine (`backend/helper/inline_engine.py`) processes button
presses. It:

1. Receives the callback query from the helper bot.
2. Parses the callback data (format: `type:panel:action`).
3. Dispatches to the registered panel handler or action handler.
4. Renders the result and edits the panel message in-place.

---

## Session System

The session system (`backend/helper/session_manager.py`) manages panel
sessions — one per chat. It tracks:

- The current panel name.
- The panel message ID (for editing).
- Auto-close timers.

---

## Database Layer

The database layer (`backend/db/client.py`) provides a Supabase client
with automatic in-memory fallback. See [DATABASE_ARCHITECTURE.md](DATABASE_ARCHITECTURE.md)
for full schema documentation.

---

## Bio Engine

The bio engine (`backend/bio/engine.py`) rewrites the owner's Telegram
profile bio every minute using a template with `{time}`, `{mood}`, `{text}`
tokens.

- Fires at each minute boundary (not a fixed interval).
- Deduplicates — skips the API call when the bio string hasn't changed.
- Handles `FloodWaitError` by sleeping the exact wait time.
- Never terminates on recoverable errors.

---

## Save Engine

The save engine (`backend/services/save_service.py`) provides two save modes:

- **Forward save** (`.save f`) — forwards the replied message to Saved
  Messages. Instant, no download.
- **Deep save** (`.save d`) — downloads media, re-uploads to Saved Messages
  with a rich caption. 50 MB hard limit.

Both modes store full metadata in the `saved_items` table.

---

## Retrieve

The retrieve system (`backend/services/retrieve_service.py`) provides:

- `.preview <code>` — show metadata for a saved item.
- `.send <code>` — forward the saved asset to the current chat.

---

## Organizer

The organizer (`backend/services/organize_service.py`) provides:

- `.organize list` — data overview (save counts, log count, bio status).
- `.organize clean` — purge logs older than 7 days.

---

## Diagnostics

The diagnostics system (`backend/diagnostics.py`) provides:

- `.kill` — diagnostic snapshot + stalled-task recovery.
- `.logs` — event log viewer with filters (errors only, last N, by module).
- Health snapshots for the health dashboard.

---

## Settings

The settings system (`backend/services/settings_service.py`) manages all
Glass Panel configuration. Settings are stored as typed columns on the
`panel_settings` table.

### Available Settings

| Setting | Type | Default | Range | Description |
|---|---|---|---|---|
| `auto_close_enabled` | boolean | `true` | — | Whether panels auto-close |
| `auto_close_delay` | integer | `120` | 5..3600 | Seconds before auto-close |
| `max_deep_save_mb` | integer | `50` | 1..500 | Max file size for deep save |
| `delete_batch_size` | integer | `100` | 1..1000 | Batch size for message deletion |
| `log_retention_days` | integer | `7` | 1..365 | Days to retain logs |
| `panel_timeout_seconds` | integer | `300` | 30..86400 | Panel timeout in seconds |
| `allow_multiple_panels` | boolean | `false` | — | Allow multiple simultaneous panels |
| `reuse_existing_panel` | boolean | `true` | — | Reuse an existing panel instead of creating new |
| `language` | string | `"en"` | non-empty | Language code |
| `diagnostics_enabled` | boolean | `true` | — | Show diagnostics info (`.kill`, `.logs`) |
| `debug_callbacks` | boolean | `false` | — | Debug callback tracing |
| `owner_only` | boolean | `true` | — | Restrict commands to owner only |

---

## Database Panel

The database panel (`.db`) provides:

- `.db clean` — remove orphan rows.
- `.db stats` — database statistics.
- `.db vacuum` — cleanup + optimize.

---

## Database

Four tables in the `public` schema:

| Table | Purpose |
|---|---|
| `saved_items` | Media save records (forward + deep) |
| `bio_state` | Singleton bio engine state per owner — template, mood, text |
| `bot_logs` | Structured activity log — level, message, JSONB context |
| `panel_settings` | Glass Panel configuration — 12 typed columns (auto-close, limits, diagnostics, etc.) |

All tables have RLS enabled. SELECT is granted to `anon` + `authenticated` (dashboard reads). All writes use the service-role key, which bypasses RLS.

---

## Quick Start

### Prerequisites

- Python 3.11+
- Node.js 18+ (for dashboard build)
- A Telegram account with API credentials
- A Supabase project (optional — bot works without it)

### 1. Clone and Install

```bash
git clone <repo-url>
cd lifeos
pip install -r backend/requirements.txt
npm install
```

### 2. Generate Session String

```python
# Run this locally ONCE to generate your session string:
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
BOT_TOKEN=your_bot_token  # Optional — for Glass Panel mode
SUPABASE_URL=your_supabase_url  # Optional
SUPABASE_SERVICE_ROLE_KEY=your_key  # Optional
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
| `BOT_TOKEN` | `""` | Helper bot token for Glass Panel mode |
| `SUPABASE_URL` | `""` | Supabase project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | `""` | Supabase service role key |
| `DATABASE_URL` | `""` | PostgreSQL connection string (unused) |
| `TZ` | `Asia/Tehran` | Timezone for bio engine and timestamps |
| `PORT` | `8000` | Web server port |
| `BIO_UPDATE_ENABLED` | `false` | Auto-start bio cron on boot |
| `LOG_LEVEL` | `INFO` | Python logging level |

---

## Helper Bot Setup

The Glass Panel system requires a **separate** Telegram bot account:

1. Create a new bot via [@BotFather](https://t.me/BotFather).
2. Copy the bot token.
3. Set `BOT_TOKEN` in your environment.
4. Start the bot — the helper bot will connect automatically.

Without `BOT_TOKEN`, the bot falls back to plain-text edit-in-place mode.

---

## Deploying on Render

1. Push code to GitHub.
2. Create a new Web Service on Render, connected to your GitHub repo.
3. Set the start command: `python -m backend.main`.
4. Set the health check path: `/health`.
5. Add all environment variables (see above).
6. Deploy.

Render will build the Python environment from `backend/requirements.txt`
and start the bot.

---

## Commands

All commands use the `.` prefix. Commands only fire on outgoing messages
(sent from the owner's own account).

### Utility

| Command | Description |
|---|---|
| `.ping` | PONG |
| `.id` | Chat & Message IDs |
| `.help` | Interactive help panel |
| `.panel` | Context panel for replied message |
| `.health` | Health dashboard |
| `.kill` | Diagnostic snapshot + recovery |
| `.logs` | Event log viewer |

### Save Engine

| Command | Description |
|---|---|
| `.save f` | Forward save to Saved Messages |
| `.save d` | Deep save (download + re-upload) |

### Retrieve

| Command | Description |
|---|---|
| `.preview <code>` | Show metadata for a saved item |
| `.send <code>` | Forward saved asset to current chat |

### Organizer

| Command | Description |
|---|---|
| `.del <n>` | Delete last n outgoing messages |
| `.del id <msgid>` | Delete from message ID |
| `.organize list` | Data overview |
| `.organize clean` | Purge old logs |

### Bio Engine

| Command | Description |
|---|---|
| `.bio on` | Start bio cron |
| `.bio off` | Stop bio cron |
| `.bio show` | Show bio state |
| `.bio template <tpl>` | Set bio template |
| `.bio text <text>` | Set {text} token |
| `.bio mood <mood>` | Set {mood} token |

### Database

| Command | Description |
|---|---|
| `.db clean` | Remove orphan rows |
| `.db stats` | Database statistics |
| `.db vacuum` | Cleanup + optimize |

---

## Troubleshooting

### Bot won't start

- Check that all required env vars are set (`API_ID`, `API_HASH`,
  `SESSION_STRING`, `BOT_OWNER_ID`).
- Check that the session string is valid (regenerate if needed).
- Check logs for connection errors.

### Panels not working

- Ensure `BOT_TOKEN` is set — Glass Panel mode requires the helper bot.
- Without `BOT_TOKEN`, commands fall back to plain-text edit-in-place.

### Database errors

- The bot works without Supabase — all operations fall back to in-memory.
- Check `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` are set correctly.
- Check that all migrations have been applied.

---

## License

This project is for personal use. See the repository for details.
