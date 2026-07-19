# AGENTS.md — LifeOS Telegram Self-Bot

> **This file is the single source of truth for the LifeOS repository.**
> Future AI sessions must read this document first before inspecting source
> files. If the code and this document disagree, the code is authoritative
> and this document must be updated.

---

## 1. Project Overview

LifeOS is a **headless Telegram self-bot** (userbot) that runs as a single
Python `asyncio` process on Render's Free tier. It uses **Telethon** with a
`StringSession` (never file-based, never interactive) to operate the owner's
own Telegram account. A **FastAPI** micro-server runs in the same process to
satisfy Render's HTTP health check and serve a read-only React dashboard.

The bot provides four subsystems:

1. **Save Engine** — forward-save or deep-save (download + re-upload) any media
   to Saved Messages with structured metadata stored in Supabase.
2. **Bio Engine** — a timezone-synchronized cron that rewrites the owner's
   Telegram profile bio every minute using a template with `{time}`, `{mood}`,
   and `{text}` tokens.
3. **Organizer** — data overview, log cleanup, and multi-message deletion.
4. **Utility** — ping, chat/message ID lookup, help.

**Tech stack:** Python 3.11 · Telethon 1.34 · FastAPI 0.111 · Uvicorn 0.29 ·
Supabase 2.4 (optional) · React 18 + Vite 5 + Tailwind CSS 3 (dashboard).

---

## 2. Repository Tree

```
project/
├── AGENTS.md                          # THIS FILE — authoritative architecture doc
├── Procfile                           # Render start command: python -m backend.main
├── render.yaml                        # Render Blueprint (service def + env vars)
├── package.json                       # Frontend: React 18 + Vite 5 + Tailwind 3
├── vite.config.ts                     # Vite config — builds to dist/, proxies /api → :8000
├── tailwind.config.js                 # Tailwind theme — Material 3 dark CSS variables
├── postcss.config.js                  # PostCSS: tailwindcss + autoprefixer
├── tsconfig.json                      # TS base config (ES2020, strict)
├── tsconfig.app.json                  # TS app config (noEmit, extends base)
├── index.html                         # Vite HTML entry — loads Inter font + main.tsx
├── .env                               # Frontend env (VITE_SUPABASE_URL, VITE_SUPABASE_ANON_KEY)
├── .gitignore                         # Ignores: node_modules, dist, .env, __pycache__, etc.
│
├── backend/                           # Python backend (the actual application)
│   ├── __init__.py
│   ├── main.py                        # asyncio entry point — startup + shutdown orchestration
│   ├── config.py                      # Env var loader — required vars hard-fail, optional default
│   ├── requirements.txt               # Python deps (telethon, fastapi, uvicorn, supabase, etc.)
│   │
│   ├── bot/                           # Telegram bot layer
│   │   ├── __init__.py
│   │   ├── client.py                  # Telethon client factory (StringSession, connect, authorize)
│   │   ├── router.py                  # register_all() — wires every handler onto the client
│   │   └── handlers/                  # One file per command group
│   │       ├── __init__.py
│   │       ├── guard.py               # is_owner() — single permission gate
│   │       ├── misc.py                # .ping, .id, .help
│   │       ├── save.py                # .save f, .save d (forward + deep save)
│   │       ├── retrieve.py            # .preview <code>, .send <code>
│   │       ├── delete.py              # .del <n>, .del id <msgid>
│   │       ├── organize.py            # .organize list, .organize clean
│   │       └── bio.py                 # .bio help/template/text/mood/on/off/show
│   │
│   ├── bio/                           # Bio cron engine
│   │   ├── __init__.py
│   │   └── engine.py                  # Cron loop, render_bio(), start/stop/is_running
│   │
│   ├── db/                            # Database layer
│   │   ├── __init__.py
│   │   └── client.py                  # Supabase singleton + in-memory fallback
│   │
│   └── web/                           # FastAPI web server
│       ├── __init__.py
│       └── app.py                     # /health, /api/* endpoints, static SPA serving
│
├── src/                               # React dashboard (TypeScript)
│   ├── main.tsx                       # React root — StrictMode + createRoot
│   ├── App.tsx                        # Dashboard shell — tabs, polling, error/loading states
│   ├── index.css                      # Tailwind directives + Material 3 dark color vars
│   ├── lib/
│   │   └── api.ts                     # Typed fetch wrappers + TS interfaces
│   └── components/
│       ├── SavedItems.tsx             # Saved items list — media type badges, tags, metadata
│       ├── BioStatus.tsx              # Bio engine status — template, mood, active state
│       └── LogViewer.tsx              # Log viewer — level filters, color-coded entries
│
└── supabase/
    └── migrations/
        ├── 20260712234229_lifeos_schema.sql    # Initial schema (superseded by below)
        └── 20260714111706_create_lifeos_tables.sql  # Authoritative: saved_items, bio_state, bot_logs
```

---

## 3. Module Responsibilities

### `backend/main.py` — Entry Point & Lifecycle

The sole asyncio entry point. Orchestrates the entire startup and shutdown
sequence. Runs via `python -m backend.main` (see `Procfile`).

**Startup (5 phases, strict sequential):**
1. Config validation via `config.load()` — hard-exits on missing required vars.
2. Database warm-up — pings `bot_logs` table; continues on failure.
3. Telethon client connect + authorize via `build_client()`.
4. Command handler registration via `register_all()` — exactly once.
5. Bio cron resume — starts if `is_active=True` in DB or `BIO_UPDATE_ENABLED=true`.
6. Uvicorn web server — background asyncio task.

**Shutdown (4 phases, on SIGTERM/SIGINT):**
A. Bio cron cancelled via `bio_engine.stop_cron()`.
B. Uvicorn `should_exit = True`.
C. All remaining asyncio tasks cancelled + awaited (zero orphans).
D. Telethon disconnected cleanly.

**Main loop:** `asyncio.wait()` on two tasks — `client.run_until_disconnected()`
and `shutdown.wait()`. Whichever completes first triggers shutdown.

### `backend/config.py` — Environment Loader

Validates environment variables. **Required vars** (`API_ID`, `API_HASH`,
`SESSION_STRING`, `BOT_OWNER_ID`) cause `sys.exit(1)` if missing. **Optional
vars** get sensible defaults. Supabase availability is computed as a boolean
from whether both `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` are set.

Returns a dict consumed by `main.py`.

### `backend/bot/client.py` — Telethon Client Factory

`build_client(api_id, api_hash, session_string)` creates a `TelegramClient`
with `StringSession` (headless, never interactive). Connection parameters
tuned for Render Free tier:
- `auto_reconnect=True`
- `connection_retries=5`
- `retry_delay=2`
- `flood_sleep_threshold=60` (auto-sleep on Telegram flood responses up to 60s)

Calls `client.connect()`, then `client.is_user_authorized()` — raises
`RuntimeError` if the session is invalid. Logs the connected user identity.

### `backend/bot/router.py` — Handler Registration

`register_all(client, owner_id, tz_str)` calls `.register()` on each handler
module in sequence: `misc`, `save`, `retrieve`, `delete`, `organize`, `bio`.
Each module's `register()` function decorates the Telethon client with
`@client.on(events.NewMessage(...))` handlers. Registration happens exactly
once during startup Phase 3.

### `backend/bot/handlers/guard.py` — Permission Gate

`is_owner(event, owner_id)` — the single source of truth for permission
checks. Returns `True` only if `event.sender_id == owner_id`. Every handler
calls this before executing any logic. Non-owner messages are silently
ignored (no response, no error).

### `backend/bot/handlers/misc.py` — Utility Commands

Registers `.ping`, `.id`, `.help`. See §6 for command details.

### `backend/bot/handlers/save.py` — Save Engine

Registers `.save f` (forward) and `.save d` (deep). The most complex handler.
See §7 for full architecture.

### `backend/bot/handlers/retrieve.py` — Retrieval Commands

Registers `.preview <code>` and `.send <code>`. Queries the DB for a saved
item by its `save_code` and either displays metadata or forwards the saved
asset to the current chat.

### `backend/bot/handlers/delete.py` — Message Deletion

Registers `.del <n>` and `.del id <msgid>`. Deletes the owner's outgoing
messages in the current chat. Batch-deletes in chunks of 100. Enforces
1-500 range for count-based deletion.

### `backend/bot/handlers/organize.py` — Data Organizer

Registers `.organize list` and `.organize clean`. Lists save/log/bio counts
or purges logs older than 7 days.

### `backend/bot/handlers/bio.py` — Bio Command Handler

Registers `.bio` with sub-commands: `help`, `template`, `text`, `mood`, `on`,
`off`, `show`. Manages bio state in the DB and starts/stops the cron engine.
See §8 for full architecture.

### `backend/bio/engine.py` — Bio Cron Engine

The cron loop that rewrites the Telegram profile bio. See §8 for full
architecture.

### `backend/db/client.py` — Database Layer

Supabase singleton client with automatic in-memory fallback. See §9 for full
architecture.

### `backend/web/app.py` — FastAPI Web Server

Micro-server with health check and read-only API endpoints. See §11 for
endpoint reference. Also serves the built React SPA from `dist/` if it
exists.

### `src/` — React Dashboard

A dark-themed Material 3 React dashboard that polls the backend API every 30
seconds. See §12 for frontend architecture.

---

## 4. Startup Sequence

```
python -m backend.main
    │
    ├── config.load()
    │     ├── Check REQUIRED env vars → sys.exit(1) if missing
    │     └── Return config dict
    │
    ├── Install SIGTERM/SIGINT handlers → shutdown event
    │
    ├── Phase 1: Database warm-up
    │     ├── db_client.get_db() → Supabase client or None
    │     ├── If DB: SELECT bot_logs LIMIT 1 (warm-up ping)
    │     └── On failure: log warning, continue
    │
    ├── Phase 2: Telethon connect
    │     ├── build_client(API_ID, API_HASH, SESSION_STRING)
    │     ├── client.connect()
    │     ├── client.is_user_authorized() → RuntimeError if not
    │     └── client.get_me() → log identity
    │
    ├── Phase 3: Register handlers
    │     └── register_all(client, OWNER_ID, TZ)
    │           ├── misc.register(client, owner_id)
    │           ├── save.register(client, owner_id, tz_str)
    │           ├── retrieve.register(client, owner_id)
    │           ├── delete.register(client, owner_id)
    │           ├── organize.register(client, owner_id)
    │           └── bio.register(client, owner_id, tz_str)
    │
    ├── Phase 4: Bio cron resume
    │     ├── db_client.get_bio_state(OWNER_ID)
    │     ├── If is_active=True → bio_engine.start_cron()
    │     ├── elif BIO_UPDATE_ENABLED=true → bio_engine.start_cron()
    │     └── else → skip
    │
    ├── Phase 5: Web server (background task)
    │     └── asyncio.create_task(_run_web(PORT))
    │
    └── Main loop: asyncio.wait(tg_task, shutdown_task)
          └── FIRST_COMPLETED → enter shutdown sequence
```

---

## 5. Shutdown Sequence

```
SIGTERM or SIGINT received
    │
    ├── A. Stop bio cron
    │     └── bio_engine.stop_cron() → cancel _task, set _task=None
    │
    ├── B. Signal web server
    │     └── _uvicorn_server.should_exit = True
    │
    ├── C. Cancel all remaining asyncio tasks
    │     ├── Gather all tasks except current
    │     ├── task.cancel() for each
    │     └── asyncio.gather(*pending, return_exceptions=True)
    │
    └── D. Disconnect Telethon
          └── await client.disconnect()
```

**Guarantee:** Zero orphaned tasks. All asyncio tasks are explicitly cancelled
and awaited before the process exits.

---

## 6. Command Reference — Every Command and Its Behavior

All commands use the `.` prefix. All commands only fire on
`events.NewMessage(outgoing=True)` — i.e., messages sent by the owner's own
account. Every handler calls `is_owner(event, owner_id)` before executing.

### Utility Commands (`misc.py`)

| Command | Pattern | Behavior |
|---|---|---|
| `.ping` | `^\.ping$` | Edits the triggering message to `PONG`. No new message sent. |
| `.id` | `^\.id$` | Edits message with Chat ID + Msg ID. If replying to a message, also shows Reply Msg ID + Reply Sender ID. |
| `.help` | `^\.help$` | Edits message with the full command reference (all commands listed). |

### Save Engine (`save.py`)

| Command | Pattern | Behavior |
|---|---|---|
| `.save f` | `^\.save (f\|d)$` | **Forward save.** Must reply to a message. Forwards the replied message to Saved Messages ("me"). Records metadata in DB (save_code, origin chat/msg, sender, media type, tags). Edits trigger with confirmation and save code. |
| `.save d` | `^\.save (f\|d)$` | **Deep save.** Must reply to a message with media. Downloads media to a `BytesIO` buffer (50 MB hard limit), re-uploads to Saved Messages with a rich caption (sender, chat ID, msg ID, timestamp, media type, MIME, size, filename, tags). Records full metadata in DB. Closes buffer in `finally`. Edits trigger with confirmation. |

**Save code format:** `SV-NNNNNN` (zero-padded sequential, e.g., `SV-000001`).

**Media type detection:** Maps MIME types to human-readable labels (Photo,
Video, Audio, Voice, GIF, Sticker, Document, Unknown).

**Tag generation:** `#saved`, `#saved_<type>`, `#saved_<year>`,
`#saved_<year>_<month>`, `#saved_<year>_<month>_<day>`.

### Retrieval (`retrieve.py`)

| Command | Pattern | Behavior |
|---|---|---|
| `.preview <code>` | `^\.preview\s+(\S+)$` | Queries DB for save code (case-insensitive, uppercased). Edits message with formatted metadata: save code, type, media, MIME, size, sender, origin chat/msg, timestamp, tags. Returns error if not found. |
| `.send <code>` | `^\.send\s+(\S+)$` | Queries DB, forwards the saved asset from Saved Messages to the current chat. Deletes the triggering command message on success. Logs the send action. |

### Deletion (`delete.py`)

| Command | Pattern | Behavior |
|---|---|---|
| `.del <n>` | `^\.del(?:\s+(.+))?$` | Deletes the last `n` outgoing messages in the current chat (1-500 range). Deletes the command message first, then iterates `iter_messages(from_user="me")`. Batch-deletes. |
| `.del id <msgid>` | `^\.del(?:\s+(.+))?$` | Deletes all messages from `<msgid>` forward in the current chat. Deletes the command message first, then iterates `iter_messages(min_id=msgid-1)`. Batch-deletes in chunks of 100. |

**Batch size:** 100 messages per `delete_messages()` call.

### Organizer (`organize.py`)

| Command | Pattern | Behavior |
|---|---|---|
| `.organize list` | `^\.organize\s+(list\|clean)$` | Edits message with structured overview: total/forward/deep save counts, log entry count, bio engine status + template. |
| `.organize clean` | `^\.organize\s+(list\|clean)$` | Purges `bot_logs` entries older than 7 days. Edits message with count of deleted entries. |

### Bio Engine (`bio.py`)

| Command | Pattern | Behavior |
|---|---|---|
| `.bio` or `.bio help` | `^\.bio(?:\s+(.+))?$` | Shows token reference and command list. |
| `.bio template` | (no space after "template") | Shows current template without changing it. |
| `.bio template <tpl>` | | Sets the bio template. Supports `{time}`, `{mood}`, `{text}` tokens. Cannot be empty. |
| `.bio text <text>` | | Sets the `{text}` token value. |
| `.bio mood <mood>` | | Sets the `{mood}` token value. |
| `.bio on` | | Sets `is_active=True` in DB, starts the cron engine, shows a preview of the rendered bio. |
| `.bio off` | | Sets `is_active=False` in DB, stops the cron engine. |
| `.bio show` | | Shows full bio state: status, template, mood, text, last rendered bio, preview, current server time. |

---

## 7. Save System Architecture

### Forward Save (`.save f`)

```
Reply to message → .save f
    │
    ├── Get reply message
    ├── Generate save_code (SV-NNNNNN, atomic via asyncio.Lock)
    ├── Extract metadata: sender, chat_id, msg_id, media type, MIME, file_id, file_size
    ├── Build tags (#saved, #saved_<type>, #saved_<year>_<month>_<day>)
    ├── client.forward_messages("me", reply)
    │     └── Capture saved_chat_id + saved_msg_id from forwarded message
    ├── db_client.insert_save({...})
    └── event.edit("📌 Forward Saved SV-NNNNNN")
```

**No download.** Forward save is instant — it uses Telegram's native forward
API. Only metadata is stored.

### Deep Save (`.save d`)

```
Reply to message with media → .save d
    │
    ├── Get reply message
    ├── Check media exists → error if not
    ├── Check file_size ≤ 50 MB → error if exceeds
    ├── Generate save_code
    ├── Extract metadata + build caption + tags
    ├── event.edit("⬇️ Downloading…")
    ├── buf = io.BytesIO()
    ├── try:
    │     ├── client.download_media(reply, file=buf)
    │     ├── Check buf not empty → error if zero bytes
    │     ├── buf.seek(0); buf.name = filename
    │     └── client.send_file("me", buf, caption, force_document=...)
    ├── finally: buf.close()  ← always, zero memory leaks
    ├── db_client.insert_save({...})
    └── event.edit("✅ Deep Saved SV-NNNNNN")
```

**Hard size limit:** 50 MB (`_MAX_DEEP_BYTES = 50 * 1024 * 1024`). Enforced
before any download is attempted.

**Memory safety:** `BytesIO` buffer is always closed in a `finally` block,
even on cancellation or exception.

**Empty-download guard:** If the download produces a zero-byte buffer, the
operation is aborted with an error message.

**Caption format:** Rich multi-line caption with sender, chat ID, msg ID,
timestamp, media type, MIME, size, filename, and tags.

### Save Code Generation

`get_next_save_code()` is an async function that uses `asyncio.Lock` to ensure
atomicity. It counts existing rows in `saved_items` (or fallback list) and
returns `SV-{count+1:06d}`. The lock prevents race conditions when multiple
saves happen concurrently.

---

## 8. Bio Engine Architecture

### Overview

The bio engine periodically rewrites the owner's Telegram profile bio ("about"
field) using a template with tokens. It runs as a single asyncio task that
fires exactly at each minute boundary.

### Key Components (`backend/bio/engine.py`)

**`_get_tz(tz_str)`** — Resolves a timezone string via `zoneinfo.ZoneInfo`.
On any error (invalid timezone, missing tzdata), falls back to UTC and logs
a warning. Never crashes.

**`render_bio(template, mood, text, tz_str)`** — Renders the bio string by
replacing `{time}` with current `HH:MM`, `{mood}` with the mood value, and
`{text}` with the custom text. Uses the resolved timezone.

**`_seconds_to_next_minute(tz)`** — Calculates seconds until the next minute
boundary. Used to sleep precisely so the cron fires at `xx:xx:00`.

**`_cron_loop(client, owner_id, tz_str)`** — The main loop:
```
while True:
    sleep until next minute boundary
    try:
        state = db_client.get_bio_state(owner_id)
        if not state or not is_active → stop loop (return)
        new_bio = render_bio(template, mood, text, tz)
        if new_bio == last_bio → skip (deduplication)
        try:
            await client.edit_profile(about=new_bio)
        except FloodWaitError → sleep(fwe.seconds + 1), continue
        except CancelledError → raise
        except other → log warning, continue (retry next minute)
        db_client.update_bio_state(last_bio=new_bio, updated_at=now)
    except CancelledError → raise
    except other → log warning, continue (retry next minute)
```

**`start_cron(client, owner_id, tz_str)`** — Idempotent. If a task already
exists and is not done, returns immediately. Otherwise creates a new asyncio
task. Stored in module-level `_task`.

**`stop_cron()`** — Cancels the running task and sets `_task = None`.

**`is_running()`** — Returns `True` if `_task` exists and is not done.

### Design Guarantees

- **Fires exactly at xx:xx:00** — sleeps to the next minute boundary, not a
  fixed interval.
- **Deduplication** — skips the Telegram API call when the rendered string
  hasn't changed since the last confirmed update.
- **FloodWait handling** — catches `FloodWaitError`, sleeps the exact number
  of seconds + 1, then continues the loop.
- **Never terminates on errors** — all non-cancellation errors are logged as
  warnings and the loop retries next minute.
- **Single task** — `start_cron` is idempotent; only one updater can exist.
- **Self-stopping** — if `is_active` becomes `False` in the DB, the loop
  exits on the next tick.

---

## 9. Database Layer

### `backend/db/client.py`

**Singleton pattern:** `get_db()` initializes the Supabase client on first
call. If env vars are missing or initialization fails, returns `None` and all
operations fall back to in-memory storage.

**In-memory fallback:** A module-level dict with three keys:
```python
_fallback = {"saved_items": [], "bio_state": {}, "bot_logs": []}
```

Every public function wraps its Supabase call in `try/except`. On any error
(network, missing table, DNS failure), it logs a warning and uses the
in-memory fallback. The bot never crashes due to a database error.

### Public Functions

| Function | Supabase Table | Behavior |
|---|---|---|
| `log(owner_id, level, message, context)` | `bot_logs` | Inserts a log entry. Silent on failure. |
| `get_next_save_code()` | `saved_items` | Returns `SV-NNNNNN` based on row count. Uses `asyncio.Lock`. |
| `insert_save(data)` | `saved_items` | Inserts a save record. Falls back to appending to list. |
| `query_save(save_code)` | `saved_items` | Queries by save_code (uppercased). Uses `.maybe_single()`. |
| `list_saves(owner_id, limit, offset)` | `saved_items` | Paginated list ordered by `created_at` desc. Returns `(items, total)`. |
| `count_saves(owner_id, save_type?)` | `saved_items` | Count, optionally filtered by save_type. |
| `get_bio_state(owner_id)` | `bio_state` | Queries by owner_id. Uses `.maybe_single()`. |
| `get_or_create_bio_state(owner_id)` | `bio_state` | Gets state or creates with defaults. |
| `update_bio_state(owner_id, updates)` | `bio_state` | Updates state by owner_id. |
| `count_logs(owner_id)` | `bot_logs` | Count of log entries for owner. |
| `list_logs(owner_id, limit)` | `bot_logs` | Recent logs ordered by `created_at` desc. |
| `clean_logs(owner_id, days)` | `bot_logs` | Deletes logs older than `days`. Returns count deleted. |

---

## 10. Supabase Fallback Behavior

The bot is designed to run **with or without Supabase**. This is a core
architectural principle, not an afterthought.

### When Supabase is available

- `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` are both set in the
  environment.
- `get_db()` returns a real Supabase client.
- All writes go through the **service-role key**, which bypasses RLS.
- Data persists across restarts.

### When Supabase is NOT available

- Env vars are missing or initialization fails.
- `get_db()` returns `None`.
- All operations use the in-memory `_fallback` dict.
- Data does NOT persist across restarts.
- The bot continues to function normally — all commands work.
- Every Supabase call that fails logs a warning and falls back silently.

### RLS Policy Model

All three tables have RLS enabled. Only SELECT policies are granted to
`anon` + `authenticated` (read-only dashboard access). All writes
(INSERT/UPDATE/DELETE) go through the backend's service-role key, which
bypasses RLS entirely. There are no anon/authenticated write policies in
the authoritative migration.

### Database Schema

Three tables (defined in `20260714111706_create_lifeos_tables.sql`):

**`saved_items`** — Media save records with save_code, save_type (forward/deep),
origin/saved chat+msg IDs, sender info, MIME type, file_id, file_size,
media_type, tags (text array), caption, owner_id, created_at.

**`bio_state`** — Singleton per owner (owner_id is UNIQUE). Template, mood,
custom_text, is_active, last_bio, updated_at.

**`bot_logs`** — Structured logs with owner_id, level (INFO/WARN/ERROR),
message, context (JSONB), created_at.

---

## 11. API Endpoints

The FastAPI server (`backend/web/app.py`) exposes these endpoints:

| Method | Path | Auth | Behavior |
|---|---|---|---|
| GET | `/health` | None | Returns `{"status": "ok"}`. Used by Render health check. |
| GET | `/api/saves` | None | Paginated list of saved items. Query params: `limit` (default 50), `offset` (default 0). Returns `{"items": [...], "total": N}`. |
| GET | `/api/saves/{save_code}` | None | Single save item by code. 404 if not found. |
| GET | `/api/bio` | None | Current bio engine state. Returns `{}` if not initialized. |
| GET | `/api/logs` | None | Recent log entries. Query param: `limit` (default 100). Returns `{"logs": [...]}`. |
| GET | `/` | None | If `dist/` exists: serves the React SPA. Otherwise: `{"status": "LifeOS API running — no UI build found"}`. |
| GET | `/{path}` | None | SPA fallback — serves `index.html` for any path if `dist/` exists. |
| GET | `/assets/*` | None | Static assets from Vite build. |

**Note:** The API endpoints currently hardcode `owner_id=0` for queries. This
is a known limitation — the dashboard is designed for a single-owner bot.

**Docs disabled:** `docs_url=None, redoc_url=None` — Swagger/ReDoc are turned
off in production.

---

## 12. Frontend Architecture

### Stack

React 18 + TypeScript + Vite 5 + Tailwind CSS 3. Dark Material 3 theme using
CSS custom properties defined in `src/index.css`.

### Component Tree

```
main.tsx
  └── App.tsx (root)
        ├── Header (sticky, tab navigation, refresh button)
        ├── Error banner (conditional)
        ├── Loading state (conditional)
        └── Tab content:
              ├── SavedItems.tsx    (tab: "saves")
              ├── BioStatus.tsx     (tab: "bio")
              └── LogViewer.tsx     (tab: "logs")
```

### Data Flow

1. `App.tsx` calls `api.saves()`, `api.bio()`, `api.logs()` in parallel on
   mount and every 30 seconds (`setInterval`).
2. `src/lib/api.ts` provides typed fetch wrappers hitting `/api/*` endpoints.
3. TypeScript interfaces (`SavedItem`, `BioState`, `BotLog`) mirror the
   backend DB schema.
4. State is managed with `useState` + `useCallback`. No external state
   library.

### Color System (CSS Variables in `index.css`)

```css
--surface:           #0f1117  (app background)
--surface-container: #161b27  (cards)
--surface-variant:   #1e2535  (hover, badges)
--on-surface:        #e2e8f0  (primary text)
--on-surface-variant:#8b9bb4  (secondary text)
--outline-variant:   #2a3347  (borders)
--primary:           #4d9eff  (accent, links, active tab)
--on-primary:        #001933  (text on primary)
--error:             #f87171  (errors)
```

### Vite Config

- Build output: `dist/` (served by FastAPI's static file mount).
- Dev server proxy: `/api` → `http://localhost:8000` (for local development
  with the Python backend running separately).

---

## 13. Environment Variables

### Required (hard-fail if missing)

| Variable | Type | Description |
|---|---|---|
| `API_ID` | int | Telegram API ID from my.telegram.org |
| `API_HASH` | str | Telegram API Hash from my.telegram.org |
| `SESSION_STRING` | str | Telethon StringSession (generated offline, see README.md) |
| `BOT_OWNER_ID` | int | Telegram numeric user ID of the bot owner |

### Optional (with defaults)

| Variable | Default | Description |
|---|---|---|
| `SUPABASE_URL` | `""` | Supabase project URL. If empty, in-memory fallback. |
| `SUPABASE_SERVICE_ROLE_KEY` | `""` | Supabase service role key. If empty, in-memory fallback. |
| `DATABASE_URL` | `""` | PostgreSQL connection string (backup, currently unused in code). |
| `TZ` | `Asia/Tehran` | Timezone string for bio engine and timestamps. |
| `PORT` | `8000` | Web server port. Render sets this automatically. |
| `GHOST_ROOM_ID` | `""` | Unused in current code. |
| `DEST_CHANNEL_ID` | `""` | Unused in current code. |
| `BIO_UPDATE_ENABLED` | `false` | If `true`, auto-starts bio cron on boot regardless of DB state. |
| `LOG_LEVEL` | `INFO` | Python logging level. |

### Frontend-only

| Variable | Description |
|---|---|
| `VITE_SUPABASE_URL` | Supabase URL for frontend (in `.env`). Currently unused by the React app. |
| `VITE_SUPABASE_ANON_KEY` | Supabase anon key for frontend. Currently unused by the React app. |

### SESSION_STRING Generation

Must be generated once on a local machine (see `README.md` for the helper
script). The session string encodes the auth key and must never be committed
to the repository or logged.

---

## 14. Render Deployment

### Configuration Files

- **`Procfile`**: `web: python -m backend.main` — tells Render how to start.
- **`render.yaml`**: Render Blueprint defining the web service, Python env,
  health check path (`/health`), and all environment variables.

### Render Blueprint (`render.yaml`)

```yaml
services:
  - type: web
    name: lifeos
    env: python
    startCommand: python -m backend.main
    healthCheckPath: /health
    envVars:
      - key: PYTHON_VERSION
        value: 3.11.7
      - key: API_ID            # sync: false (set manually in dashboard)
      - key: API_HASH          # sync: false
      - key: SESSION_STRING    # sync: false
      - key: BOT_OWNER_ID      # sync: false
      - key: SUPABASE_URL      # sync: false
      - key: SUPABASE_SERVICE_ROLE_KEY  # sync: false
      - key: TZ
        value: Asia/Tehran
      - key: BIO_UPDATE_ENABLED
        value: "false"
      - key: LOG_LEVEL
        value: INFO
```

### Deployment Flow

1. Push code to GitHub (connected to Render).
2. Render builds the Python environment from `backend/requirements.txt`.
3. Render starts `python -m backend.main`.
4. Health check hits `/health` — must return 200.
5. The Telethon client connects and the bot is live.

### Frontend Build (Optional)

The React dashboard is not built automatically by Render. To serve it:
1. Run `npm run build` locally (produces `dist/`).
2. Ensure `dist/` is available to the Python process (or build it in a
   Render build step). The FastAPI app checks for `dist/` at startup and
   mounts it as static files if present.

---

## 15. Logging Philosophy

### Configuration

```python
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logging.getLogger("backend").setLevel(logging.INFO)
logging.getLogger("telethon").setLevel(logging.WARNING)
```

### Principles

- **Root level is WARNING** — suppresses noisy library output.
- **`backend` namespace is INFO** — application events are visible.
- **`telethon` namespace is WARNING** — Telethon's internal chatter is
  suppressed; only warnings and errors surface.
- **All logs go to stdout** — Render captures stdout for log access.
- **Structured bot logs** — significant bot actions (saves, sends) are also
  written to the `bot_logs` DB table with structured context, viewable in the
  dashboard.
- **No secrets logged** — credentials, session strings, and API keys are
  never logged or printed.

---

## 16. Error Handling Philosophy

### Core Principle

**The bot must never crash.** Every external operation (Telegram API, DB,
network) is wrapped in error handling that degrades gracefully.

### Patterns

1. **Database operations** — every Supabase call is in a `try/except`. On
   failure, log a warning and fall back to in-memory storage. Never propagate
   to the caller.

2. **Telegram API calls** — handlers wrap API calls in `try/except`. On
   failure, edit the triggering message with an error emoji + message.
   `FloodWaitError` is caught specifically in the bio cron and slept off.

3. **Bio cron loop** — `asyncio.CancelledError` is always re-raised. All
   other exceptions are logged as warnings and the loop retries on the next
   tick. The loop never terminates on a recoverable error.

4. **Deep save** — `BytesIO` buffer is closed in a `finally` block.
   `asyncio.CancelledError` is re-raised. Empty downloads are detected and
   rejected.

5. **Startup** — only missing required env vars cause a hard exit
   (`sys.exit(1)`). Database warm-up failure, bio cron resume failure, and
   other non-critical errors are logged and skipped.

6. **Shutdown** — `client.disconnect()` is wrapped in `try/except` to ensure
   a clean exit even if Telethon is in a bad state.

7. **Edit-first policy** — all command responses edit the triggering message
   in-place. No new messages are sent (zero-spam policy). Error feedback also
   edits the trigger.

---

## 17. Performance Rules

1. **Bio cron fires at minute boundaries** — sleeps to the next `xx:xx:00`,
   not a fixed interval. This minimizes API calls and ensures deterministic
   timing.

2. **Bio deduplication** — if the rendered bio string hasn't changed since
   the last update, the Telegram API call is skipped entirely.

3. **Batch deletion** — `.del` commands delete in batches of 100 to avoid
   hitting Telegram API limits.

4. **Save code atomicity** — `asyncio.Lock` ensures no duplicate save codes
   under concurrent saves.

5. **Deep save size limit** — 50 MB hard limit checked before any download,
   preventing memory exhaustion on Render Free tier.

6. **BytesIO always closed** — no memory leaks from deep save buffers.

7. **Single asyncio process** — no threading, no multiprocessing. Everything
   is cooperative async. Telethon + Uvicorn + Bio cron all share one event
   loop.

8. **Frontend polling** — 30-second interval, parallel fetches. No
   websockets or SSE.

9. **Auto-reconnect** — Telethon auto-reconnects with 5 retries, 2s delay.
   FloodWait responses up to 60s are auto-slept.

---

## 18. Security Rules

1. **Owner-only access** — every command handler calls `is_owner(event,
   owner_id)` before executing. Non-owner messages are silently ignored
   (no response, no error, no log).

2. **No hardcoded secrets** — all credentials come from `os.getenv()`.
   Nothing is committed to the repository.

3. **StringSession** — session is stored as an env var, never written to
   disk. No `.session` file exists on the server.

4. **No secrets in logs** — credentials, session strings, and API keys are
   never logged or printed.

5. **Service-role key** — Supabase writes use the service-role key which
   bypasses RLS. The anon key is only used by the frontend (and currently
   unused — the dashboard reads via the backend API).

6. **RLS enabled** — all three DB tables have RLS. Only SELECT is granted
   to anon/authenticated. No write policies for anon/authenticated.

7. **`.env` is gitignored** — never committed.

8. **Docs disabled** — FastAPI Swagger/ReDoc endpoints are disabled in
   production (`docs_url=None, redoc_url=None`).

9. **Outgoing-only commands** — all handlers fire on
   `events.NewMessage(outgoing=True)`. Commands must be sent from the
   owner's own account.

---

## 19. Coding Rules

1. **Match existing conventions** — before writing code, read neighboring
   files and match naming, layout, error handling, and import style.

2. **No comments unless necessary** — only add comments for non-obvious
   "why" (hidden constraints, subtle invariants, bug workarounds). Never
   comment the "what" — well-named code does that.

3. **Single responsibility** — each handler module handles one command
   group. Each function does one thing.

4. **Edit-first policy** — command responses edit the triggering message,
   never send new messages.

5. **Async everything** — all I/O is async. No blocking calls. No threads.

6. **try/except at boundaries** — wrap external I/O (Telegram API, DB).
   Trust internal code. Don't add error handling for impossible states.

7. **No premature abstraction** — don't create abstractions without at
   least two concrete use cases. Three similar lines is better than a
   forced abstraction.

8. **Reuse before adding** — check for existing utilities before writing
   new ones.

9. **Leave the tree clean** — when moving or replacing code, delete what
   it replaced. No orphaned files, dead exports, or commented-out blocks.

10. **File organization by cohesion** — things that change together belong
    together. A long file with one purpose is fine; a short file mixing
    concerns is not.

---

## 20. File Modification Rules

1. **Never modify source files unless the task requires it.** If asked to
   document, create new files. If asked to fix a bug, change only what's
   needed.

2. **Read before writing.** Always understand existing code before
   modifying it. Use the file structure in this document as a guide.

3. **Don't add features beyond the task.** A bug fix doesn't need
   surrounding cleanup. A documentation task doesn't need code changes.

4. **Don't introduce backwards-compatibility shims.** Change the code
   directly. No `_old` variants, no renamed unused vars, no `// removed`
   comments.

5. **Don't delete user data.** Never `DROP` tables, `DELETE` columns,
   change column types, or rename tables in migrations.

6. **Always enable RLS** on new tables. Write 4 separate policies (one
   per CRUD verb), never `FOR ALL`.

7. **Keep files at a manageable size.** Split when a file becomes hard to
   navigate or mixes unrelated concerns.

8. **Run `npm run build`** to verify frontend changes compile.

---

## 21. Git Workflow Rules

1. **Commit message format:** `type: description` (e.g., `docs: add
   authoritative AGENTS.md`, `fix: close BytesIO in deep save finally block`).
   **All changes for one request MUST be in a single commit.** Never split
   one task into multiple commits. Finish all work first, then create one
   commit, then push.

2. **One concern per commit.** Don't mix documentation, features, and fixes
   in a single commit.

3. **Never commit secrets.** `.env` is gitignored. Never stage it. Never
   paste session strings or API keys into commits.

4. **Never force-push** unless absolutely required and clearly explained.
   Prefer rebase + normal push.

5. **Verify before pushing.** Ensure the build passes and the commit
   contains only intended changes.

6. **Remote:** Always push to the repository already connected to the
   current workspace. Never hardcode a repository URL or owner name.

7. **Don't push broken or incomplete work.** A shipped feature beats
   several half-built ones.

---

## 22. AI Agent Rules

1. **Read this file first.** AGENTS.md is the single source of truth. Do
   not re-scan the entire repository when this document already explains
   the architecture.

2. **Don't modify source files unless explicitly asked.** Documentation
   tasks create new files. Bug fixes change only the relevant code.

3. **Don't rebuild the project** unless changes were made that affect the
   build. If only documentation changed, no build is needed.

4. **Don't inspect external services** (Render, Supabase dashboards, logs)
   unless the task requires it. The architecture is documented here.

5. **Match existing conventions.** Before writing code, read neighboring
   files. Match naming, style, error handling, and import patterns.

6. **Use the TodoWrite tool** to plan and track multi-step tasks. Mark
   items completed as soon as they're done.

7. **Report faithfully.** If a build, test, or push fails, say so. Don't
   claim success when output shows failure.

8. **Don't add features beyond the request.** A documentation task doesn't
   need code changes. A commit task doesn't need refactoring.

9. **Verify file existence and location** after creating files. Don't
   assume the write succeeded without checking.

10. **Never guess URLs.** Use URLs provided by the user. Never hardcode
    a repository URL — always push to the connected workspace repository.

11. **Commit only what was asked.** If asked to commit one file, stage and
    commit only that file. Don't `git add .` indiscriminately.

12. **Don't re-analyze the repository** if this document has already been
    read. Use it as the reference. Only read source files when this document
    is insufficient for the task.

13. **Single-commit rule.** ALL modifications for one request MUST be
    committed into ONE SINGLE COMMIT. Never create one commit per file.
    Never create one commit per small change. Finish every task first,
    then create exactly ONE commit, then push.

---

## 23. Telethon Lifecycle

### Connection

1. `TelegramClient` is created with `StringSession(session_string)` — no
   file-based session, no interactive login.
2. `client.connect()` establishes the connection.
3. `client.is_user_authorized()` verifies the session is valid. Raises
   `RuntimeError` if not — the session must be regenerated offline.
4. `client.get_me()` confirms identity and logs it.

### Running

- `client.run_until_disconnected()` runs as an asyncio task. It blocks
  until the connection is lost or the client is disconnected.
- Incoming events (NewMessage) trigger registered handlers.
- `auto_reconnect=True` handles network blips transparently.
- `flood_sleep_threshold=60` makes Telethon auto-sleep on flood responses
  up to 60 seconds without raising an exception.

### Disconnection

- On shutdown, `client.disconnect()` is called in a `try/except`.
- All asyncio tasks are cancelled before disconnect.
- The process exits cleanly after disconnect completes.

### Handler Registration

- Handlers are registered exactly once during startup Phase 3.
- Each handler is a decorated function inside a `register()` function:
  ```python
  @client.on(events.NewMessage(outgoing=True, pattern=r"^\.command$"))
  async def handler(event):
      if not is_owner(event, owner_id):
          return
      # ... logic ...
  ```
- The `register()` function closes over `client`, `owner_id`, and
  (optionally) `tz_str`, making them available to the handler without
  globals.

---

## 24. Owner Permission Model

### Single Owner

The bot is designed for a **single owner** — the one person whose Telegram
account the bot operates. The owner's Telegram user ID is provided via the
`BOT_OWNER_ID` environment variable.

### Permission Gate

`backend/bot/handlers/guard.py` contains the sole permission check:

```python
def is_owner(event, owner_id: int) -> bool:
    return bool(event.sender_id and event.sender_id == owner_id)
```

- Every handler calls `is_owner(event, owner_id)` as its first action.
- If the sender is not the owner, the handler returns immediately — no
  response, no error, no log. Silent rejection.
- The `event.sender_id` truthiness check ensures `None`/`0` sender IDs
  are rejected.

### Why `outgoing=True`?

All handlers fire on `events.NewMessage(outgoing=True)`. This means commands
must be sent from the owner's own Telegram account. Since the bot operates
the owner's account via StringSession, the owner sends commands to any chat
(including Saved Messages) and the bot responds. This is the self-bot model
— there is no separate bot account.

### No Multi-User Support

The permission model is binary: you are the owner or you are ignored. There
is no role hierarchy, no admin list, no allowlist. The `BOT_OWNER_ID` is a
single integer, not a list.

---

## 25. Git Workspace Recovery

1. NEVER assume the repository is missing just because `.git` is not visible.
2. If any git command fails with:
   - "fatal: not a git repository"
   - missing `.git`
   - missing remote
   then STOP and attempt repository recovery BEFORE reporting failure.
3. Recovery procedure (mandatory):
   Step 1:
   Check whether the Bolt workspace is connected to GitHub.
   Step 2:
   If Bolt shows "Synced to GitHub" or a connected repository in the UI, treat that repository as the source of truth.
   Step 3:
   Restore the local git metadata from the connected repository instead of giving up.
   Step 4:
   After recovery verify:
   - `.git` exists
   - `git status`
   - `git remote -v`
   - current branch
   - latest commit
   Step 5:
   Continue the original task exactly where it stopped.
4. NEVER tell the user to reconnect GitHub before attempting the recovery procedure.
5. NEVER claim "there is no repository" unless the recovery procedure has already failed.
6. NEVER stop only because `.git` disappeared.
7. If the connected repository already contains the requested changes, report that instead of trying to recreate commits.
8. When a workspace reset occurs, recover first, continue second, explain last.
9. Pushing is mandatory whenever the repository is connected and the current task requires a push.
10. Do NOT create forks, new repositories, or new remotes if the Bolt workspace is already connected to GitHub.
11. Always prefer restoring the existing workspace over rebuilding it.
12. These rules have higher priority than any previous Git workflow instructions in AGENTS.md.

---

## Document Version

This document reflects the state of the repository as of the commit that
introduced it. If the codebase changes in ways that invalidate any section
above, update this document in the same commit.
