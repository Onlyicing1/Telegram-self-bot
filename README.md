# LifeOS — Telegram Self-Bot

A production-grade **Telegram self-bot** (userbot) that turns your own Telegram account into a personal operating system. Save anything, search instantly, automate your profile bio, and keep your data organized — all from a single headless Python process.

Built on **Telethon** + **Supabase** + **FastAPI** + **React**, deployed on **Render**.

---

## Table of Contents

1. [What Is LifeOS?](#what-is-lifeos)
2. [Architecture](#architecture)
3. [Self Bot](#self-bot)
4. [Helper Bot](#helper-bot)
5. [Supabase](#supabase)
6. [Quick Start](#quick-start)
7. [Environment Variables](#environment-variables)
8. [Creating the Helper Bot with BotFather](#creating-the-helper-bot-with-botfather)
9. [Enabling Inline Mode](#enabling-inline-mode)
10. [Deploying on Render](#deploying-on-render)
11. [Commands](#commands)
12. [Troubleshooting](#troubleshooting)
13. [Update Instructions](#update-instructions)
14. [Recovery Instructions](#recovery-instructions)

---

## What Is LifeOS?

LifeOS is a **self-bot** — it operates *your own* Telegram account via Telethon's `StringSession`. There is no separate bot account. You type commands (`.save f`, `.bio on`, `.help`) in any chat, and the bot edits your message in-place with the result. Zero spam, zero new messages.

### Features

- **Save Engine** — Forward-save or deep-save (download + re-upload) any media to Saved Messages with full metadata.
- **Bio Engine** — A timezone-synced cron that rewrites your profile bio every minute using `{time}`, `{mood}`, `{text}` tokens.
- **Discovery** — Full-text search across captions, filenames, save codes, and MIME types.
- **Organizer** — Data overview, log cleanup, multi-message deletion.
- **Health Dashboard** — `.health` shows process, Telegram, watchdog, bio cron, memory, CPU, uptime, and more.
- **Interactive Help** — `.help` opens a numbered menu; reply with a number to navigate, reply `0` to go back.
- **Helper Bot** (optional) — A secondary bot token for inline keyboards, callback queries, and interactive menus.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                    Python asyncio process                     │
│                                                               │
│  ┌─────────────┐  ┌─────────────┐  ┌────────────────────┐     │
│  │  Self Bot    │  │ Helper Bot  │  │  FastAPI + Uvicorn  │     │
│  │ (Telethon    │  │ (Telethon   │  │  /health  /api/*    │     │
│  │  StringSess) │  │  Bot Token) │  │  React dashboard    │     │
│  └──────┬───────┘  └──────┬──────┘  └─────────┬──────────┘     │
│         │                 │                   │                │
│         │  commands +     │  inline keyboards │  HTTP API      │
│         │  business logic │  + callbacks      │                │
│         └─────────────────┴───────────────────┘                │
│                          │                                    │
│                   ┌──────┴───────┐                            │
│                   │  Bio Cron    │                            │
│                   │  Engine      │                            │
│                   └──────┬───────┘                            │
│                          │                                    │
│                   ┌──────┴───────┐                            │
│                   │  Supabase    │ ← service-role key           │
│                   │  (optional)  │   in-memory fallback         │
│                   └──────────────┘                            │
└──────────────────────────────────────────────────────────────┘
                         │
                   ┌─────┴──────┐
                   │  React     │ ← dark Material 3 dashboard
                   │  Dashboard │   polls /api/* every 30s
                   └────────────┘
```

**Single event loop.** The self-bot, helper bot, bio cron, and web server all share one asyncio loop. No threads, no multiprocessing. Clean shutdown cancels every task before disconnect.

### Repository Structure

```
backend/
├── main.py              # asyncio entry point — startup + shutdown
├── config.py            # env var loader
├── bot/                 # Self-bot layer (the brain)
│   ├── client.py        # Telethon StringSession client factory
│   ├── router.py        # registers all command handlers
│   └── handlers/        # .ping, .save, .bio, .help, .health, etc.
├── helper/              # Helper bot layer (inline UI)
│   ├── client.py        # Bot token client factory
│   └── panels.py        # InlinePanelBuilder + callback router
├── bio/
│   └── engine.py        # Bio cron loop
├── db/
│   └── client.py        # Supabase singleton + in-memory fallback
└── web/
    └── app.py           # FastAPI — /health, /api/*, SPA serving

src/                     # React dashboard (TypeScript + Vite + Tailwind)
supabase/migrations/      # SQL migrations
```

---

## Self Bot

The **self-bot** is the brain of LifeOS. It connects to Telegram using your own account credentials (API ID, API Hash, and a StringSession) via Telethon. It processes all commands, manages the bio cron, saves media, and handles business logic.

### How It Works

1. You generate a `StringSession` once on your local machine (see [Quick Start](#quick-start)).
2. The session string is stored as an environment variable (`SESSION_STRING`).
3. On startup, Telethon connects using this session — no interactive login, no file on disk.
4. You type commands (`.save f`, `.bio on`, etc.) in any chat on your phone or desktop.
5. The bot edits your message in-place with the result. No new messages are sent.

### Key Properties

- **Owner-only:** Every command checks `is_owner(event, owner_id)`. Non-owner messages are silently ignored.
- **Edit-first:** All responses edit the triggering message. Zero spam.
- **Headless:** No interactive prompts. The session string encodes the auth key.
- **Auto-reconnect:** 5 retries, 2s delay, 60s flood-sleep threshold.

---

## Helper Bot

The **helper bot** is an optional secondary Telegram client that uses a **bot token** (from BotFather) instead of a user session. It handles inline UI only:

- Inline keyboards (buttons under messages)
- Callback queries (button presses)
- Inline menus and interactive panels
- Editing inline messages

The self-bot remains the brain. The helper bot is purely a presentation layer for interactive Telegram UI elements that require a bot token.

### When Is It Needed?

Most LifeOS commands work without the helper bot — they use the edit-first policy. The helper bot is needed only when you want **inline buttons** (e.g., tap a button to confirm a save, navigate a menu, or toggle the bio engine).

### How It Integrates

- If `BOT_TOKEN` is set, `main.py` starts the helper bot in Phase 3.5 (after self-bot handlers, before the web server).
- If `BOT_TOKEN` is not set, the helper bot is skipped — the self-bot works normally without inline UI.
- The helper bot uses the same `is_owner` permission gate as the self-bot.
- Future commands can request an inline panel via `InlinePanelBuilder` and `register_panel()`.

### Architecture

```
Self Bot (brain)              Helper Bot (UI)
     │                              │
     │  .save f (command)           │
     ├─────────────────────►        │
     │                              │  sends inline keyboard
     │                              ├────────────────────►
     │                              │  user taps button
     │                              │◄────────────────────
     │  callback query              │
     │  routed to panel handler     │
     │◄─────────────────────        │
```

---

## Supabase

LifeOS uses [Supabase](https://supabase.com/) (hosted PostgreSQL) for data persistence. Three tables store saved items, bio state, and structured logs.

### When Supabase Is Available

- `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` are both set.
- All writes go through the service-role key, which bypasses RLS.
- Data persists across restarts.

### When Supabase Is NOT Available

- Env vars are missing or the connection fails.
- All operations fall back to an in-memory dict.
- Data does NOT persist across restarts — but the bot continues to work.

### Tables

| Table | Purpose |
|---|---|
| `saved_items` | Media save records — save code, type, origin, metadata, tags |
| `bio_state` | Singleton bio engine state per owner — template, mood, text |
| `bot_logs` | Structured activity logs — level, message, JSONB context |

All tables have RLS enabled. SELECT is granted to `anon` + `authenticated` (dashboard reads). All writes use the service-role key.

---

## Quick Start

### Prerequisites

- Python 3.11+
- Node.js 18+ (for the dashboard)
- A Telegram account with API credentials from [my.telegram.org](https://my.telegram.org)
- A Supabase project (optional — the bot works without it)

### Step 1: Get Telegram API Credentials

1. Go to [my.telegram.org](https://my.telegram.org) and log in.
2. Click **API development tools**.
3. Create an application — you'll get an **API ID** (number) and an API Hash (string).
4. Save these — you'll need them for `API_ID` and `API_HASH`.

### Step 2: Generate SESSION_STRING

You must generate a Telethon `StringSession` **once** on your local machine:

```bash
pip install telethon
python -c "
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

api_id   = int(input('API_ID: '))
api_hash = input('API_HASH: ')

with TelegramClient(StringSession(), api_id, api_hash) as client:
    print('\\n--- SESSION_STRING ---')
    print(client.session.save())
    print('--- copy the line above ---')
"
```

Copy the output string — this is your `SESSION_STRING`.

### Step 3: Find Your Telegram User ID

Your `BOT_OWNER_ID` is your numeric Telegram user ID. You can find it by messaging [@userinfobot](https://t.me/userinfobot) on Telegram.

### Step 4: Local Development

```bash
# Clone the repo
git clone <your-repo-url>
cd lifeos

# Backend
pip install -r backend/requirements.txt

# Frontend
npm install
npm run build    # builds to dist/ (served by FastAPI)

# Set environment variables (see below)
export API_ID=123456
export API_HASH=your_api_hash
export SESSION_STRING=your_session_string
export BOT_OWNER_ID=123456789

# Run the bot
python -m backend.main
```

The dashboard is available at `http://localhost:8000`.

---

## Environment Variables

### Required (bot won't start without these)

| Variable | Description |
|---|---|
| `API_ID` | Telegram API ID from [my.telegram.org](https://my.telegram.org) |
| `API_HASH` | Telegram API Hash from [my.telegram.org](https://my.telegram.org) |
| `SESSION_STRING` | Telethon StringSession (generated in Step 2 above) |
| `BOT_OWNER_ID` | Your Telegram numeric user ID (from [@userinfobot](https://t.me/userinfobot)) |

### Optional — Helper Bot

| Variable | Default | Description |
|---|---|---|
| `BOT_TOKEN` | `""` | Bot token from BotFather. If set, the helper bot starts and inline UI is enabled. If empty, inline UI is disabled. |

### Optional — Supabase

| Variable | Default | Description |
|---|---|---|
| `SUPABASE_URL` | `""` | Supabase project URL. Empty = in-memory fallback. |
| `SUPABASE_SERVICE_ROLE_KEY` | `""` | Supabase service role key. Empty = in-memory fallback. |

### Optional — General

| Variable | Default | Description |
|---|---|---|
| `GHOST_ROOM` | `""` | Reserved for future use. |
| `DATABASE_URL` | `""` | PostgreSQL connection string (backup, currently unused). |
| `TZ` | `Asia/Tehran` | Timezone for bio engine and timestamps. |
| `PORT` | `8000` | Web server port (Render sets this automatically). |
| `BIO_UPDATE_ENABLED` | `false` | Set to `true` to auto-start bio cron on boot. |
| `LOG_LEVEL` | `INFO` | Python logging level. |

> **Note:** Supabase is optional. Without it, the bot uses in-memory storage and all commands still work — but data won't persist across restarts.

---

## Creating the Helper Bot with BotFather

The helper bot requires a bot token from BotFather. This is separate from your self-bot session.

1. Open Telegram and message [@BotFather](https://t.me/BotFather).
2. Send `/newbot`.
3. Choose a **name** for your bot (e.g., "LifeOS Helper").
4. Choose a **username** for your bot (must end in `bot`, e.g., `lifeos_helper_bot`).
5. BotFather will give you a **bot token** that looks like `123456789:ABCdefGHIjklMNOpqrSTUvwxYZ`.
6. Copy this token — this is your `BOT_TOKEN`.

### Set the Bot Token

Add `BOT_TOKEN` to your environment variables:

```bash
export BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrSTUvwxYZ
```

Or in the Render dashboard, add a new environment variable:
- **Key:** `BOT_TOKEN`
- **Value:** your bot token

### Security

- Never commit the bot token to your repository.
- The bot token is stored as an environment variable only.
- The helper bot uses the same `is_owner` permission gate as the self-bot — only you can interact with it.

---

## Enabling Inline Mode

Inline Mode is what lets the self-bot insert helper-bot-authored messages (with
inline buttons) into **any** chat — Saved Messages, private chats, and groups —
without the helper bot needing to be a member of that chat. This is the official
Telegram Inline Bot flow:

1. The self-bot calls `client.inline_query(@helper_bot, "help")`.
2. The helper bot answers with an `InlineQueryResultArticle` containing the
   menu text and category buttons.
3. The self-bot clicks the first result to insert it into the current chat.
4. The message appears from **your account** with a small "via @bot" attribution.
5. Callback button presses route back to the helper bot.

The helper bot **never** sends messages directly into chats. All UI is delivered
via Inline Mode so it works everywhere.

### Required BotFather Settings

The helper bot **must** have Inline Mode enabled, or `.help` will fall back to a
plain-text menu (no buttons).

1. Open [@BotFather](https://t.me/BotFather) on Telegram.
2. Send `/setinline`.
3. Select your helper bot from the list.
4. Send a placeholder message when prompted (e.g., `Search LifeOS...`).
5. BotFather confirms: **"Inline mode enabled for @your_bot_username."**

You can verify at any time by sending `/setinline` again.

### Required Environment Variables

| Variable | Required? | Purpose |
|---|---|---|
| `BOT_TOKEN` | **Yes** for inline UI | Bot token from BotFather (see [Creating the Helper Bot](#creating-the-helper-bot-with-botfather)) |
| `API_ID`, `API_HASH`, `SESSION_STRING`, `BOT_OWNER_ID` | Yes (always) | Self-bot credentials |

No extra variable is needed for the bot username — LifeOS reads it
automatically from Telegram when the helper bot starts.

### How to Test Inline Mode

1. Ensure `BOT_TOKEN` is set and the bot is running.
2. In **any** chat (including Saved Messages), type:
   ```
   .help
   ```
3. You should see the interactive help panel appear as a message from your
   account (with "via @bot"). The original `.help` command is deleted only
   **after** the panel is successfully inserted.
4. Tap category buttons to navigate. Tap **Close** to remove the panel.

If you see a plain text menu instead of buttons, Inline Mode is not enabled —
repeat the BotFather steps above.

### Common Mistakes

| Symptom | Cause | Fix |
|---|---|---|
| `.help` shows plain text, no buttons | Inline Mode not enabled in BotFather | Run `/setinline` on @BotFather |
| `.help` command disappears and nothing replaces it | Old code used `helper.send_message()` which fails in Saved Messages | Update to latest code — new flow uses `inline_query` + `click` |
| "via @bot" attribution is missing | Message was sent as a normal edit, not via inline mode | Ensure the helper bot is connected and Inline Mode is enabled |
| Inline query returns 0 results | Helper bot not started, or InlineQuery handler not registered | Check logs for "Helper bot online" on startup |
| `PeerIdInvalidError` in logs | Helper bot tried to send to a chat it can't reach | This is the old architecture — update to the inline-query flow |

### Why "via @bot" Appears

When a message is sent via Inline Mode, Telegram adds a "via @bot_username"
attribution under the sender's name. This is standard Telegram behavior for all
inline bots (like @gif, @pic, @vote). It cannot be removed — it's how Telegram
identifies which bot generated the inline result. The message is still sent from
**your account**, not from the bot.

---

## Deploying on Render

LifeOS is designed for [Render](https://render.com)'s Free tier.

### Step 1: Push to GitHub

Push your repository to GitHub. Render connects to your GitHub repo.

### Step 2: Create a Web Service

1. Go to [render.com](https://render.com) and sign in.
2. Click **New** → **Web Service**.
3. Connect your GitHub repository.
4. Render reads `render.yaml` automatically — or set:
   - **Environment:** Python
   - **Start Command:** `python -m backend.main`
   - **Health Check Path:** `/health`

### Step 3: Add Environment Variables

In the Render dashboard, add all environment variables:

| Variable | Required? | How to set |
|---|---|---|
| `API_ID` | Yes | From my.telegram.org |
| `API_HASH` | Yes | From my.telegram.org |
| `SESSION_STRING` | Yes | Generated in Step 2 of Quick Start |
| `BOT_OWNER_ID` | Yes | Your Telegram user ID |
| `BOT_TOKEN` | No | From BotFather (enables inline UI) |
| `SUPABASE_URL` | No | From Supabase dashboard |
| `SUPABASE_SERVICE_ROLE_KEY` | No | From Supabase dashboard |
| `TZ` | No | Default: `Asia/Tehran` |
| `BIO_UPDATE_ENABLED` | No | Default: `false` |
| `LOG_LEVEL` | No | Default: `INFO` |

### Step 4: Deploy

1. Click **Create Web Service**.
2. Render builds the Python environment from `backend/requirements.txt`.
3. Render starts `python -m backend.main`.
4. The health check hits `/health` — it must return 200.
5. The Telethon client connects and the bot is live.

### Step 5: Build the Dashboard (Optional)

The React dashboard is not built automatically by Render. To serve it:

```bash
# Locally
npm install
npm run build    # produces dist/
```

Ensure `dist/` is available to the Python process. The FastAPI app checks for `dist/` at startup and mounts it as static files if present.

---

## Commands

All commands use the `.` prefix. All commands only fire on your own outgoing messages. Every response edits the triggering message in-place.

### Utility

| Command | Description |
|---|---|
| `.ping` | Edits message to `PONG` |
| `.id` | Shows Chat ID + Message ID |
| `.help` | Opens interactive inline help panel (via Inline Mode — works in any chat) |
| `.health` | Full health dashboard (process, Telegram, watchdog, bio, memory, CPU, uptime) |

### Save Engine (reply to a message first)

| Command | Description |
|---|---|
| `.save f` / `.s f` | Forward save — instant, no download |
| `.save d` / `.s d` | Deep save — download + re-upload with rich caption |

### Discovery

| Command | Description |
|---|---|
| `.list` | Show 10 recent saves |
| `.list 20` | Show 20 recent saves |
| `.find vacation` | Search by caption, filename, code, or MIME |

### Retrieval

| Command | Description |
|---|---|
| `.preview S391` | Show metadata for a save code |
| `.r S391` / `.retrieve S391` | Alias for `.preview` |
| `.send S391` | Forward the saved asset into this chat |

### Organizer

| Command | Description |
|---|---|
| `.del 5` | Delete last 5 outgoing messages |
| `.del id 12345` | Delete all messages from ID 12345 forward |
| `.del S391` | Delete saved item from the index |
| `.organize list` | Data overview (saves, logs, bio status) |
| `.organize clean` | Purge logs older than 7 days |

### Bio Engine

| Command | Description |
|---|---|
| `.bio on` | Start the bio cron |
| `.bio off` | Stop the bio cron |
| `.bio template 🕒 {time} \| {mood} \| {text}` | Set the bio template |
| `.bio text Working` | Set the `{text}` token value |
| `.bio mood 😊` | Set the `{mood}` token value |
| `.bio show` | Inspect full bio state |
| `.bio help` | Token reference |

### Database

| Command | Description |
|---|---|
| `.db clean` | Remove orphan rows |
| `.db stats` | Database statistics |
| `.db vacuum` | Cleanup + optimize |

### Diagnostics

| Command | Description |
|---|---|
| `.kill` | Snapshot + stalled-task recovery |
| `.logs` | Recent events (last 20) |
| `.logs 50` | Last 50 events |
| `.logs errors` | Errors only |
| `.logs module <name>` | Filter by module |

---

## Troubleshooting

### Bot won't start

**Problem:** `sys.exit(1)` on startup.

**Cause:** A required environment variable is missing (`API_ID`, `API_HASH`, `SESSION_STRING`, or `BOT_OWNER_ID`).

**Fix:** Check that all four required variables are set in your environment or Render dashboard.

### Session not authorized

**Problem:** `RuntimeError: Telethon session is not authorized.`

**Cause:** The `SESSION_STRING` is invalid or expired.

**Fix:** Regenerate the session string on your local machine (see [Quick Start](#quick-start)) and update the environment variable.

### Helper bot fails to start

**Problem:** Warning log: `Helper bot failed: ... — inline UI disabled`

**Cause:** `BOT_TOKEN` is set but invalid, or the bot token doesn't match a BotFather bot.

**Fix:**
1. Verify the token with BotFather: send `/token` to [@BotFather](https://t.me/BotFather), select your bot, and compare.
2. Update `BOT_TOKEN` with the correct token.
3. Redeploy.

### Bot starts but commands don't respond

**Problem:** You send `.ping` but nothing happens.

**Cause:** Your `BOT_OWNER_ID` doesn't match your Telegram user ID.

**Fix:** Find your correct user ID via [@userinfobot](https://t.me/userinfobot) and update `BOT_OWNER_ID`.

### Bio cron not updating

**Problem:** `.bio on` works but bio doesn't change.

**Cause:** The rendered bio string may be identical to the current one (deduplication), or FloodWait is active.

**Fix:**
1. Run `.bio show` to check the last rendered bio.
2. Change the template or text/mood tokens so the rendered string differs.
3. Wait — FloodWait errors are auto-slept and retried.

### Health check fails on Render

**Problem:** Render marks the service as unhealthy.

**Cause:** The FastAPI server isn't responding on `/health`.

**Fix:**
1. Check Render logs for Python errors.
2. Ensure `PORT` is set (Render sets this automatically).
3. The `/health` endpoint returns `{"status": "ok"}` — if it's not, check for import errors.

### Database not persisting

**Problem:** Saved items disappear after restart.

**Cause:** Supabase env vars are not set, so the bot is using in-memory fallback.

**Fix:** Set `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` in your environment.

---

## Update Instructions

### Updating the Bot Code

1. Pull the latest code from your repository:
   ```bash
   git pull origin main
   ```

2. Install any new dependencies:
   ```bash
   pip install -r backend/requirements.txt
   npm install
   ```

3. Rebuild the dashboard:
   ```bash
   npm run build
   ```

4. Restart the bot:
   ```bash
   python -m backend.main
   ```

### Updating on Render

1. Push your changes to GitHub.
2. Render automatically detects the push and redeploys.
3. If auto-deploy is off, click **Manual Deploy** in the Render dashboard.

### Updating Environment Variables

1. Go to the Render dashboard.
2. Select your LifeOS service.
3. Click **Environment**.
4. Add or update variables.
5. Save — Render redeploys automatically.

### Updating the Session String

If your session expires:

1. Regenerate the `SESSION_STRING` locally (see [Quick Start](#quick-start)).
2. Update the `SESSION_STRING` environment variable in Render.
3. Redeploy.

---

## Recovery Instructions

### If the Bot Crashes

The bot is designed to never crash, but if it does:

1. Check Render logs for the error.
2. Common causes:
   - **Missing env vars:** Add them and redeploy.
   - **Invalid session:** Regenerate `SESSION_STRING`.
   - **Supabase down:** The bot falls back to in-memory — no action needed.
3. Render auto-restarts the service on crash.

### If the Bio Cron Stops

1. Run `.bio show` to check the state.
2. If `is_active` is `True` but the cron isn't running:
   - Run `.bio off` then `.bio on` to restart.
3. If the issue persists, run `.kill` to collect diagnostics and recover stalled tasks.

### If Telethon Disconnects

The auto-reconnect system handles this:
- 5 retries with 2s delay.
- If all retries fail, the watchdog forces a disconnect and reconnect cycle.
- Check `.health` for `restart_count` and `telethon_connected` status.

### If the Helper Bot Disconnects

1. The helper bot has the same auto-reconnect as the self-bot.
2. If it fails to start, the self-bot continues without inline UI.
3. Check `.health` or Render logs for helper bot errors.
4. Verify `BOT_TOKEN` is correct.

### Full Reset

If everything is broken and you need a clean start:

1. Stop the bot.
2. Regenerate `SESSION_STRING` locally.
3. Clear the database (optional — or let it keep existing data).
4. Update all environment variables.
5. Restart: `python -m backend.main`.

---

## Project Philosophy

- **Never crash.** Every external operation degrades gracefully.
- **Zero spam.** Every command edits the triggering message in-place.
- **Owner-only.** One permission gate. Non-owner messages are silently ignored.
- **Deterministic.** Bio cron fires at minute boundaries. Save codes are atomic.
- **Single process.** One asyncio loop. No threads, no multiprocessing.
- **Data safety.** Never `DROP` tables or `DELETE` columns. Migrations are additive.

---

## Credits

- **[Telethon](https://github.com/LonamiWebs/Telethon)** — Telegram MTProto library
- **[FastAPI](https://fastapi.tiangolo.com/)** — Web framework
- **[Supabase](https://supabase.com/)** — PostgreSQL backend
- **[React](https://react.dev/)** + **[Vite](https://vitejs.dev/)** — Dashboard
- **[Tailwind CSS](https://tailwindcss.com/)** — Styling
- **[Render](https://render.com/)** — Hosting

---

## License

This project is for personal use. See the repository for details.
