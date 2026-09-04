# LifeOS — Telegram Self-Bot

A production-grade **Telegram self-bot** (userbot) that turns your own
Telegram account into a personal operating system. Save anything, search
instantly, automate your profile bio and username, and keep your data
organized — all through an interactive inline-button UI driven by a
single headless Python process.

Built on **Telethon** + **Supabase** + **FastAPI** + **React**, deployed
on **Render**.

## Key Highlights

- **Headless** — runs as a single `asyncio` process, no interactive login.
- **Self-healing** — the runtime supervisor detects disconnections and
  rebuilds the client automatically (heartbeat, keepalive, failsafe).
- **Resilient** — degrades gracefully when Supabase is unavailable
  (in-memory fallback for every table).
- **Zero-spam** — all responses edit the triggering message in-place.
- **Owner-only** — every command and callback is gated by a single
  permission check.
- **AI execution agent** — address the assistant by name (default
  `Nova`) and it executes real actions (save, delete, search, profile)
  through the existing service layer — never fake success.

`.menu` is the only dot-prefixed text command. All legacy dot commands
(`.ping`, `.help`, `.save`, `.del`, `.bio`, `.username`, `.ai`, ...) have
been removed in favor of the Glass UI and AI execution.

## Architecture

The entire application runs as a single Python `asyncio` process.
Telethon, Uvicorn, the profile scheduler, and the heartbeat all share
one event loop. No threads, no multiprocessing.

```
backend/main.py  (asyncio entry point)
│
├── Telethon Self-Client (StringSession) ── Bot Handlers (Glass UI + AI activation)
├── FastAPI Web Server (Uvicorn)         ── Web Routes (/health, /api/*, dashboard SPA)
├── Profile Scheduler (per-minute task)  ── Bio Engine + Username Engine
│
└── Services Layer ── save · retrieve · delete · discover · organize
                     bio · username · settings · database
                     │
                     ├── DB Client (Supabase + in-memory fallback)
                     ├── Helper Bot (Glass UI inline panels)
                     ├── Runtime Supervisor (single recovery authority:
                     │   heartbeat · keepalive · failsafe)
                     └── AI Subsystem (backend/ai/)
                         Engine → Dispatcher → Prompt Builder → Provider Manager
                         Memory (short/long/permanent) · Tools · Config
```

## Repository Structure

```
lifeos/
├── backend/                    # Python backend (single asyncio process)
│   ├── main.py                 # Entry point — starts everything
│   ├── config.py               # Env var loader (required + optional)
│   ├── diagnostics.py          # In-memory event log (500-entry buffer)
│   ├── health.py               # Health snapshot builder
│   ├── bot/                    # Telegram self-bot layer
│   │   ├── client.py           # Telethon StringSession client
│   │   ├── router.py           # Handler registration (all handlers)
│   │   └── handlers/           # Glass panels + AI activation
│   ├── services/               # Business logic (handlers/tools delegate here)
│   ├── db/client.py            # Supabase singleton + in-memory fallback
│   ├── bio/engine.py           # Bio cron engine (about field)
│   ├── username/engine.py      # Username cron engine (first_name field)
│   ├── profile/                # Shared per-minute profile scheduler
│   ├── runtime/                # Self-healing runtime
│   │   ├── supervisor.py       # RuntimeSupervisor — single recovery authority
│   │   ├── heartbeat.py        # 30s structured system snapshot
│   │   ├── keepalive.py        # RPC keep-alive pings
│   │   ├── failsafe.py         # Last-resort hard reset on total freeze
│   │   ├── operation_watchdog.py # guarded_await bounded-operation utility
│   │   ├── task_guard.py       # immortal/guarded task wrappers
│   │   ├── tracer.py           # [TRACE] event logging
│   │   └── ...                 # states, diagnostics, crash_diagnostics, ...
│   ├── helper/                 # Helper bot + Glass Panel UI machinery
│   ├── telegram_api/           # Typed RPC wrappers over the self client
│   ├── observability/          # Health snapshot, stats, maintenance
│   ├── web/app.py              # FastAPI: /health, /api/*, dashboard SPA
│   └── ai/                     # AI subsystem (see AI_MASTER_DESIGN.md)
│
├── src/                        # React dashboard (Vite + TypeScript)
├── sql/                        # Per-table SQL setup scripts (core tables)
├── supabase/migrations/        # Supabase migration history (all tables)
├── tests/                      # pytest suite (--asyncio-mode=auto)
├── render.yaml                 # Render Blueprint
├── Procfile                    # Process definition
└── package.json                # Frontend build config
```

## Using the Bot

### Glass UI (`.menu`)

`.menu` opens the mother panel. Every feature is reached through inline
buttons (requires `BOT_TOKEN`; without it, panels fall back to
edit-in-place text).

| Panel | Access |
|---|---|
| Save (Deep Save) | `.menu` → **📥 Save** |
| Retrieve | `.menu` → **📥 Save** → **🔍 Retrieve** |
| Delete | `.menu` → **🗑 Delete** |
| List / Find | `.menu` → **📋 List** / **🔍 Find** |
| Database | `.menu` → **🗄 Database** |
| AI | `.menu` → **🧠 AI** |
| Bio / Username | `.menu` → **Profile** → **🧬 Bio** / **👤 Username** |
| Settings / Health / Context | `.menu` → respective panels |

### AI Assistant (Nova)

Activate the AI by starting a message with the trigger word (default
`Nova`, configurable), or by replying to a known AI message with plain
text:

```
Nova, save this
Nova, deep save this
Nova, delete the last 10 messages
Nova, what are my saved items?
```

These are **execution requests**, not just chat. Nova resolves the
intent to a native tool call, runs the existing service function, and
reports the **real** result. Deterministic destructive tools are
validated locally (outgoing-only deletion is enforced in code at a
single ownership-verification chokepoint). The full execution contract,
provider mesh, memory tiers, and security boundary are specified in
[AI_MASTER_DESIGN.md](AI_MASTER_DESIGN.md).

## Features

- **Save System** — Deep Save only (download → re-upload as a NEW Saved
  Messages message; there is no Forward Save), link save, metadata
  persistence, compact save codes (`S0001`).
- **Delete System** — delete last N messages, from an ID, by reply, or
  via a recent-messages browser; ownership-verified and batched.
- **Bio Engine** — timezone-synchronized cron rewriting the Telegram
  `about` field every minute from `{time}`/`{mood}`/`{text}` templates.
- **Username Engine** — same engine family for the `first_name` field;
  shares one minute-boundary scheduler with Bio.
- **Runtime Supervisor** — FSM-based self-healing core; heartbeat,
  keepalive, and failsafe run under it as the single recovery authority.
- **Diagnostics** — in-memory event log, health snapshot, asyncio
  task dumps.
- **Web Dashboard** — read-only React SPA served by FastAPI from
  `dist/` when built.

## Environment Variables

### Required

| Variable | Description |
|---|---|
| `API_ID` | Telegram API ID from my.telegram.org |
| `API_HASH` | Telegram API Hash from my.telegram.org |
| `SESSION_STRING` | Telethon StringSession (generated offline) |
| `BOT_OWNER_ID` | Telegram numeric user ID of the owner |

### Optional — Core

| Variable | Default | Description |
|---|---|---|
| `BOT_TOKEN` | `""` | Helper bot token for the Inline Glass UI |
| `SUPABASE_URL` | `""` | Supabase project URL (empty → in-memory fallback) |
| `SUPABASE_SERVICE_ROLE_KEY` | `""` | Service role key (bypasses RLS for writes) |
| `TZ` | `Asia/Tehran` | Timezone for profile engines |
| `PORT` | `8000` | Web server port |
| `BIO_UPDATE_ENABLED` | `false` | Auto-start Bio engine on boot |
| `USERNAME_UPDATE_ENABLED` | `false` | Auto-start Username engine on boot |
| `LOG_LEVEL` | `INFO` | Python logging level |

### Optional — AI

AI is off by default. Both `AI_*_API_KEY` and bare `*_API_KEY` forms are
accepted; a provider without a key is simply unavailable.

| Variable | Default | Description |
|---|---|---|
| `AI_ENABLED` | `false` | Enable the AI subsystem |
| `AI_PROVIDER` | `dummy` | Active provider name |
| `AI_PROVIDER_FALLBACK` | `""` | Comma-separated fallback chain |
| Provider keys | — | `GEMINI_API_KEY`, `GROQ_API_KEY`, `MISTRAL_API_KEY`, `ZAI_API_KEY`, `CEREBRAS_API_KEY`, `SAMBANOVA_API_KEY`, `NVIDIA_API_KEY`, `COHERE_API_KEY`, `SILICONFLOW_API_KEY`, `FIREWORKS_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `NARAROUTER_API_KEY` (NaraRouter OpenAI-compatible gateway, base URL `https://router.bynara.id/v1`, default model `deepseek-v4-flash`) |
| Model overrides | — | `<PROVIDER>_MODEL` per provider (e.g. `GEMINI_MODEL`) |

The full variable reference lives in `backend/config.py` and
`backend/ai/config/`.

## Deployment (Render)

1. Create a new web service on Render from this repository.
2. Start command: `python -m backend.main`
3. Health check path: `/health`
4. Add environment variables via Render's dashboard or import the
   `render.yaml` Blueprint.
5. Deploy. If the supervisor exhausts recovery attempts it exits with
   code 1 so Render restarts the process automatically.

An external cron/monitoring service (e.g. cron-job.org, UptimeRobot)
can GET `/health` every ~10 minutes to reduce idle spin-down and track
uptime. This reduces — but does not eliminate — free-tier sleeping.

## Supabase Setup

Supabase is optional; without it everything falls back to in-memory
storage (data lost on restart).

1. Create a project at [supabase.com](https://supabase.com).
2. Apply the schema: run the scripts in `sql/` (core tables) or apply
   everything in `supabase/migrations/` (all tables incl. AI).
3. Copy the project URL and service-role key from Settings → API into
   `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY`.

The complete schema reference (columns, indexes, constraints, RLS) is
[DATABASE_ARCHITECTURE.md](DATABASE_ARCHITECTURE.md).

## Development Workflow

**Prerequisites:** Python 3.11+, Node.js 18+, Telegram API credentials,
optionally a Supabase project and a BotFather bot token.

```bash
# 1. Install
pip install -r backend/requirements.txt
npm install

# 2. Generate a session string (run once, locally)
python - <<'PY'
from telethon.sync import TelegramClient
from telethon.sessions import StringSession
api_id = YOUR_API_ID
api_hash = 'YOUR_API_HASH'
with TelegramClient(StringSession(), api_id, api_hash) as client:
    print(client.session.save())
PY

# 3. Configure environment (.env or export vars — see section above)

# 4. Run
python -m backend.main

# 5. Build the dashboard (optional; served by FastAPI at /)
npm run build
```

Run the test suite with:

```bash
python -m pytest tests/ --asyncio-mode=auto
```

## Documentation Map

| Document | Contents |
|---|---|
| [AGENTS.md](AGENTS.md) | Authoritative architecture summary, command reference, coding & security rules |
| [AI_MASTER_DESIGN.md](AI_MASTER_DESIGN.md) | Full AI subsystem specification (engine, providers, memory, tools, budgets, failure recovery) |
| [DATABASE_ARCHITECTURE.md](DATABASE_ARCHITECTURE.md) | Complete database schema reference (every table, column, index, RLS policy) |
| [INVESTIGATION.md](INVESTIGATION.md) | Repository audit: dead-code/duplication findings and cleanup history |
| [IMPLEMENTATION_REPORT.md](IMPLEMENTATION_REPORT.md) | Canonical execution record for every cleanup pass |

## Troubleshooting

**Bot won't start** — check required env vars (`API_ID`, `API_HASH`,
`SESSION_STRING`, `BOT_OWNER_ID`); regenerate the session string if it
expired.

**Panels not working** — the Inline Glass UI requires `BOT_TOKEN`;
without it, panels fall back to plain-text edit-in-place.

**Bio/Username not updating** — check the engine is active
(`.menu` → Profile → Show State), the template has at least one token,
and the scheduler is running (Health Dashboard panel); look for
`FloodWaitError` in logs.

**Database errors** — the bot works without Supabase (in-memory
fallback); verify `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` and that
migrations are applied.

**Client keeps disconnecting** — the supervisor detects disconnections
(heartbeat/failsafe) and rebuilds automatically; check restart count and
last rebuild reason in the Health Dashboard panel.

**AI not responding** — configure at least one provider key (e.g.
`GEMINI_API_KEY`); send `Nova <request>` (default trigger); without any
key the DummyProvider returns a placeholder. Check AI status in the
Health Dashboard panel.

---

## License

This project is for personal use. See the repository for details.
