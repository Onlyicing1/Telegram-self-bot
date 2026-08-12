# LifeOS — Telegram Self-Bot

A production-grade **Telegram self-bot** (userbot) that turns your own
Telegram account into a personal operating system. Save anything, search
instantly, automate your profile bio and username, and keep your data
organized — all through an interactive inline-button UI driven by a
single headless Python process.

Built on **Telethon** + **Supabase** + **FastAPI** + **React**, deployed
on **Render**.

---

## Table of Contents

1. [Project Overview](#project-overview)
2. [Architecture](#architecture)
3. [Directory Structure](#directory-structure)
4. [AI Architecture](#ai-architecture)
5. [Database Architecture](#database-architecture)
6. [How AI Works](#how-ai-works)
7. [How Providers Work](#how-providers-work)
8. [How Memory Works](#how-memory-works)
9. [How Tracing Works](#how-tracing-works)
10. [How Background Workers Work](#how-background-workers-work)
11. [How Supabase Is Organized](#how-supabase-is-organized)
12. [How Deployment Works](#how-deployment-works)
13. [Environment Variables](#environment-variables)
14. [Render Deployment](#render-deployment)
15. [Render Keep-Alive / Cron](#render-keep-alive--cron)
16. [Supabase Setup](#supabase-setup)
17. [Development Workflow](#development-workflow)
18. [Repository Philosophy](#repository-philosophy)
19. [Features](#features)
20. [Commands](#commands)
21. [Troubleshooting](#troubleshooting)

---

## Project Overview

LifeOS is a **self-bot** — it operates *your own* Telegram account via
Telethon's `StringSession`. There is no separate bot account for commands.
You type commands (`.save`, `.bio`, `.help`) in any chat, and the bot
edits your message in-place with the result. Zero spam, zero new messages.

When a helper bot token is configured, the full **Inline Glass UI**
becomes available — interactive inline-button panels for every feature.

### Key Highlights

- **Headless** — runs as a single `asyncio` process, no interactive login.
- **Self-healing** — runtime supervisor with watchdog detects
  disconnections and rebuilds the client automatically.
- **Resilient** — degrades gracefully when Supabase is unavailable
  (in-memory fallback for every table).
- **Zero-spam** — all command responses edit the triggering message
  in-place.
- **Owner-only** — every command and callback is gated by a single
  permission check.
- **AI-ready** — a complete nested engine architecture with provider
  abstraction, memory tiers, and tool execution, activated by
  environment variables.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                      backend/main.py                              │
│                    (asyncio entry point)                          │
│                                                                  │
│  ┌──────────────┐  ┌───────────────┐  ┌──────────────────────┐   │
│  │  Telethon     │  │  FastAPI      │  │  Profile Scheduler   │   │
│  │  Self-Client  │  │  Web Server   │  │  (asyncio task)      │   │
│  │  (StringSess) │  │  (Uvicorn)    │  │                      │   │
│  └──────┬────────┘  └──────┬────────┘  └─────────┬────────────┘   │
│         │                  │                     │                │
│  ┌──────┴────────┐  ┌──────┴────────┐  ┌────────┴────────────┐   │
│  │  Bot Handlers  │  │  Web Routes   │  │  Bio Engine          │   │
│  │  (commands +   │  │  (/health,    │  │  Username Engine     │   │
│  │   AI handler)  │  │   /api/*)     │  │  (updaters)          │   │
│  └──────┬────────┘  └───────────────┘  └──────────────────────┘   │
│         │                                                        │
│  ┌──────┴──────────────────────────────────────────────────────┐ │
│  │                   Services Layer                              │ │
│  │  save, retrieve, delete, discover, organize,                  │ │
│  │  bio, username, settings, database                            │ │
│  └──────┬──────────────────────────────────────────────────────┘ │
│         │                                                        │
│  ┌──────┴───────┐  ┌───────────────┐  ┌──────────────────────┐   │
│  │  DB Client    │  │  Helper Bot   │  │  Runtime Supervisor   │   │
│  │  (Supabase)   │  │  (Telethon)   │  │  + Watchdog           │   │
│  └───────────────┘  └───────────────┘  └──────────────────────┘   │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────────┐│
│  │                   AI Subsystem (backend/ai/)                  ││
│  │  Engine → Dispatcher → Prompt Builder → Provider Manager      ││
│  │  Memory (short/long/permanent) · Tools · Config               ││
│  └──────────────────────────────────────────────────────────────┘│
└──────────────────────────────────────────────────────────────────┘
```

The entire application runs as a single Python `asyncio` process.
Telethon, Uvicorn, the profile scheduler, the watchdog, and the
heartbeat all share one event loop. No threads, no multiprocessing.

---

## Directory Structure

```
lifeos/
├── backend/                    # Python backend (single asyncio process)
│   ├── main.py                 # Entry point — starts everything
│   ├── config.py               # Env var loader (required + optional)
│   ├── diagnostics.py          # In-memory event log (500-entry buffer)
│   ├── health.py               # Health snapshot builder
│   │
│   ├── bot/                    # Telegram self-bot layer
│   │   ├── client.py           # Telethon StringSession client
│   │   ├── router.py           # Command router (all . commands)
│   │   └── handlers/           # Per-feature command handlers
│   │       ├── ai_cmd.py       # .ai command handler
│   │       ├── ai.py           # AI Glass Panel (settings, provider, triggers, etc.)
│   │       ├── ai_cmd.py     # .ai command handler (DEPRECATED — use triggers)
│   │       ├── ai_trigger.py # Trigger-based AI activation (default method)
│   │       ├── bio.py          # .bio command + panel
│   │       ├── database.py     # .db command + panel
│   │       ├── delete.py       # .del command + panel
│   │       ├── discover.py     # .list, .find commands
│   │       ├── guard.py        # Owner-only permission check
│   │       ├── misc.py         # .ping, .id, .help, .health, .kill
│   │       ├── organize.py     # LifeOS status panel
│   │       ├── retrieve.py     # .retrieve, .preview, .send
│   │       ├── save.py         # .save command + panel
│   │       └── username.py     # .username command + panel
│   │
│   ├── services/               # Business logic (between handlers and DB)
│   │   ├── save_service.py
│   │   ├── retrieve_service.py
│   │   ├── delete_service.py
│   │   ├── discover_service.py
│   │   ├── organize_service.py
│   │   ├── bio_service.py
│   │   ├── username_service.py
│   │   ├── database_service.py
│   │   ├── settings_service.py          # Panel settings (cache + validation)
│   │   └── panel_settings_repository.py # Raw DB access for panel_settings
│   │
│   ├── db/                     # Supabase client + CRUD
│   │   └── client.py           # Singleton client, threaded calls, fallback
│   │
│   ├── bio/                    # Bio cron engine
│   │   └── engine.py           # Template rendering + scheduler registration
│   │
│   ├── username/               # Username cron engine
│   │   └── engine.py           # Template rendering + scheduler registration
│   │
│   ├── profile/                # Shared profile scheduler
│   │   └── scheduler.py        # Per-minute cron, merges all updaters
│   │
│   ├── runtime/                # Self-healing runtime
│   │   ├── supervisor.py       # FSM-based recovery (10 states)
│   │   ├── watchdog.py         # 30s heartbeat + update staleness
│   │   ├── heartbeat.py        # Structured system snapshot
│   │   ├── tracer.py           # @trace decorator for event logging
│   │   ├── task_guard.py       # Cancelable task wrapper
│   │   ├── managed_task.py     # Supervised task lifecycle
│   │   ├── failsafe.py         # Crash boundary
│   │   ├── keepalive.py        # Keep-alive pings
│   │   └── states.py           # Runtime FSM state enum
│   │
│   ├── helper/                 # Helper bot + Glass Panel UI
│   │   ├── client.py           # Helper bot Telethon client
│   │   ├── panels.py           # Panel rendering + lifecycle
│   │   ├── panel_render.py     # Inline message rendering
│   │   ├── panel_registry.py   # Panel type registration
│   │   ├── panel_settings.py   # Settings panel
│   │   ├── panel_selftest.py   # Self-test panel
│   │   ├── panel_timer.py      # Auto-close timer
│   │   ├── callback_trace.py   # Callback tracing
│   │   ├── inline_engine.py    # Inline query engine
│   │   ├── inline_sender.py    # Inline result sender
│   │   ├── input_state.py      # Input mode state machine
│   │   ├── session_manager.py  # Per-chat panel sessions
│   │   ├── lifecycle.py        # Panel lifecycle manager
│   │   ├── pagination.py       # Paginated list rendering
│   │   ├── target_context.py   # Reply target resolution
│   │   ├── context.py          # Helper context types
│   │   ├── rpc_timeout.py      # RPC timeout guard
│   │   └── watchdog.py         # Helper bot watchdog
│   │
│   ├── telegram_api/           # Telegram API wrappers
│   │   ├── api.py              # High-level API
│   │   ├── messages.py         # Message operations
│   │   ├── media.py            # Media operations
│   │   ├── profile.py          # Profile operations
│   │   ├── entities.py         # Entity types
│   │   ├── exceptions.py       # API exceptions
│   │   └── _helpers.py         # Internal helpers
│   │
│   ├── web/                    # FastAPI web server
│   │   └── app.py              # Health check + dashboard API + SPA
│   │
│   └── ai/                     # AI subsystem (see AI Architecture below)
│       ├── __init__.py         # Public exports: Engine, AIRequest
│       ├── persistence.py      # Supabase persistence for AI tables
│       ├── engine/             # Execution engine
│       ├── providers/          # LLM provider abstraction
│       ├── conversation/       # Conversation context + history
│       ├── session/            # AIRequest (input type)
│       ├── prompt/             # Prompt building + budget
│       ├── memory/             # Three-tier memory
│       ├── tools/              # Tool registry + executor
│       ├── config/             # AI configuration + ENV loading
│       ├── runtime/            # In-memory conversation state
│       └── database/           # Repository interfaces for AI tables
│
├── src/                        # React dashboard (Vite + TypeScript)
├── sql/                        # Consolidated SQL scripts (5 core tables)
├── render.yaml                 # Render Blueprint
├── AI_MASTER_DESIGN.md         # AI architecture spec
├── DATABASE_ARCHITECTURE.md    # Complete database schema reference
├── AGENTS.md                   # Agent guidelines
└── package.json                # Frontend build config
```

---

## AI Architecture

The AI subsystem (`backend/ai/`) is a complete nested engine
architecture for conversational AI. It is **not wired into the main bot
startup by default** — it activates when `AI_ENABLED=true` and a provider
API key is configured. Without an API key, the DummyProvider returns a
deterministic placeholder.

### Single Entry Point

The **Engine** (`backend/ai/engine/engine.py`) is the ONLY public entry
point for AI execution:

```python
from backend.ai import get_engine, AIRequest

engine = get_engine()
result = await engine.execute(AIRequest(
    session_id="owner-123",
    user_message="Hello",
    owner_id=123,
))
```

### Execution Pipeline

```
AIRequest (immutable input)
    │
    ▼
Engine — the ONLY public entry point
    │
    ├── Dispatcher — 6-stage execution spine
    │     1. Conversation Runtime — get/create session, add user message
    │     2. Prompt Builder — system prompt + context + memory + budget
    │     3. Provider Manager — route to active provider, fallback chain
    │     4. Provider — call the LLM (or dummy)
    │     5. Conversation Update — add assistant response to history
    │     6. Result — build EngineResult with tokens, latency, warnings
    │
    └── EngineResult (immutable output)
```

### Layers

| Layer | Package | Responsibility |
|---|---|---|
| Engine | `engine/` | Public entry point, hooks, metrics |
| Dispatcher | `engine/dispatcher.py` | 6-stage execution spine |
| Providers | `providers/` | LLM abstraction, routing, fallback, metrics |
| Conversation | `conversation/` | Context assembly, history, state machine |
| Session | `session/` | `AIRequest` input type |
| Prompt | `prompt/` | System prompt, budget estimation, formatting |
| Memory | `memory/` | Short, long, permanent memory tiers |
| Tools | `tools/` | Tool registry, executor, context |
| Config | `config/` | ConfigManager, ENV loading, validation |
| Runtime | `runtime/` | In-memory conversation state, token estimation |
| Database | `database/` | Repository interfaces (in-memory fallbacks) |
| Persistence | `persistence.py` | Supabase persistence for AI tables |

See [AI_MASTER_DESIGN.md](AI_MASTER_DESIGN.md) for the full specification.

---

## Database Architecture

The database has **10 tables** in Supabase's `public` schema — 5 core
tables and 5 AI tables. All tables have RLS enabled. The backend uses
the service-role key (bypasses RLS). The frontend reads via the backend
API — it never touches Supabase directly.

### Core Tables (migrations applied)

| Table | Purpose |
|---|---|
| `saved_items` | Media save records with full metadata |
| `bio_state` | Bio cron engine state (singleton per owner) |
| `username_state` | Username cron engine state (singleton per owner) |
| `bot_logs` | Structured activity log |
| `panel_settings` | Glass Panel configuration (12 typed columns) |

### AI Tables (migrations not yet applied)

| Table | Purpose |
|---|---|
| `ai_sessions` | AI conversation session metadata |
| `ai_messages` | Individual AI messages |
| `ai_memories` | Three-tier memory (short, long, permanent) |
| `ai_tool_history` | Log of every tool call |
| `ai_provider_stats` | Per-provider aggregate statistics |

The AI subsystem currently operates entirely in-memory. When migrations
are added, the tables should match the schema in
[DATABASE_ARCHITECTURE.md](DATABASE_ARCHITECTURE.md).

For the complete schema reference (every column, type, index,
constraint, and RLS policy), see
[DATABASE_ARCHITECTURE.md](DATABASE_ARCHITECTURE.md).

---

## How AI Works

AI conversations are activated by **trigger words**, not commands.
Each user configures an English trigger and/or a Persian trigger via
the AI Settings panel or the dashboard. When the owner sends an
outgoing message whose **first word** matches either trigger, the AI
subsystem activates automatically.

### Trigger System

Each user can configure two trigger words:

| Trigger | Matching | Example |
|---|---|---|
| English | Case-insensitive | `Nova` |
| Persian | Exact match | `نوا` |

**Rules:**
- Both fields are optional individually.
- At least one must be set before AI can be activated.
- The two values must not be identical.
- Triggers must be single words (no spaces).
- The first word of each outgoing message is checked against the triggers.
- When a trigger matches, the trigger word is **removed** from the
  message before it is sent to the provider.
- Messages starting with `.` (dot commands) are always skipped.

**Example — English trigger `Nova`:**

```
Nova summarize this
```

Provider receives: `summarize this`

**Example — Persian trigger `نوا`:**

```
نوا این متن را خلاصه کن
```

Provider receives: `این متن را خلاصه کن`

No automatic transliteration. No guessing. The user explicitly
defines both values.

### Conversation Flow

```
Message
  ↓
First word == English Trigger (case-insensitive)
  OR
First word == Persian Trigger (exact)
  ↓
Remove trigger from message
  ↓
Load Provider (from Supabase config)
  ↓
Load Model (from Supabase config)
  ↓
Send request through AI pipeline
  ↓
Edit triggering message with response
```

### Execution Pipeline

1. The trigger handler (`ai_trigger.py`) detects a trigger match.
2. It restores the saved provider/model from Supabase.
3. It builds an `AIRequest` with the stripped message.
4. The Engine delegates to the Dispatcher, which runs 6 stages:
   - Gets or creates a conversation session for the owner
   - Builds the prompt (system prompt + conversation history + memory)
   - Routes to the active provider (with fallback chain)
   - Calls the provider's `chat()` method
   - Updates the conversation history with the response
   - Returns an `EngineResult`
5. The handler edits the triggering message with the AI response.
6. If the provider returned tool calls, the ToolExecutor runs them
   (READ_ONLY and READ_WRITE auto-execute; DANGEROUS and ADMIN_ONLY
   require owner confirmation).

### Backward Compatibility

The old `.ai <message>` command is deprecated but still works. Users
should migrate to the trigger system. The trigger system is the default
activation method.

### Provider Selection

Providers are selected via the AI Settings panel. Available providers
are auto-detected from environment variables. The selected provider
and model are persisted in the `ai_config` Supabase table and restored
on each trigger activation.

### Model Selection

Models are fetched live from the provider's API. When a provider is
selected, the model list is automatically downloaded and the first
available model is set as default. Users can change the model at any
time via the AI panel.

Without an API key, the DummyProvider returns a deterministic
placeholder — no network calls are ever made.

---

## How Providers Work

The provider layer (`backend/ai/providers/`) abstracts LLM providers
behind a single interface. The rest of the system never references a
provider by name — it calls `ProviderManager.chat()`.

### Provider Registry

All providers are registered in a `ProviderRegistry`. The
`ProviderManager` routes requests to the active provider and handles
fallback.

### Available Providers

| Provider | Status | Notes |
|---|---|---|
| `dummy` | Active (default) | Deterministic placeholder, no network |
| `gemini` | Real implementation | Google Gemini API |
| `openai` | Real implementation | OpenAI GPT, supports custom base URLs |
| `openrouter` | Real implementation | OpenRouter (100+ models via one API) |
| `cerebras` | Real implementation | Cerebras inference (OpenAI-compatible) |
| `groq` | Real implementation | Groq inference (OpenAI-compatible) |
| `mistral` | Real implementation | Mistral AI (OpenAI-compatible) |

All providers except `dummy` subclass `OpenAICompatProvider`, which
implements the OpenAI-compatible chat completions API via `httpx` with
retry, exponential backoff, and usage parsing.

### Configuration

Providers are configured via environment variables (`AI_*` prefix).
See [Environment Variables](#environment-variables) below.

### Adding a New Provider

1. Create `backend/ai/providers/<name>.py` with a class inheriting from
   `OpenAICompatProvider` (or `BaseProvider` for non-OpenAI-compatible).
2. Set `PROVIDER_NAME` and `PROVIDER_VERSION`.
3. Add defaults to `base/defaults.py`.
4. Add the class to `_PROVIDER_CLASSES` in `factory.py`.
5. Import and export it in `providers/__init__.py`.

---

## How Memory Works

The memory system (`backend/ai/memory/`) implements a three-tier
architecture:

| Tier | Retention | Storage | Purpose |
|---|---|---|---|
| Short | Per-request (RAM only) | `ShortMemory` | Scratch pad for the current turn |
| Long | 90 days | `ai_memories` table | Cross-session summaries |
| Permanent | Never expires | `ai_memories` table | Always-in-prompt facts |

The `MemoryManager` owns all three tiers and provides a single
`retrieve_for_prompt()` method that returns text blocks for the Prompt
Builder. Permanent memory is always injected. Long memory is filtered by
relevance and importance. Short memory is cleared after each turn.

When Supabase is unavailable, memories use in-memory fallbacks (data is
lost on restart).

---

## How Tracing Works

The `backend/runtime/tracer.py` module provides a `@trace` decorator
and `trace()` function that record events into the in-memory
`diagnostics.py` circular buffer (500 entries). Every traced event
captures:

- Module name
- Action
- Duration
- Result (SUCCESS / FAILED / ERROR)
- Details (error message or summary)

Events are visible via the `.logs` command and the `.kill` diagnostic
snapshot. The tracer never blocks — it writes to an in-memory list.

---

## How Background Workers Work

The bot runs several supervised background tasks, all sharing the single
event loop:

| Worker | Module | Schedule | Purpose |
|---|---|---|---|
| Profile Scheduler | `profile/scheduler.py` | Every minute at `HH:MM:00` | Merges bio + username updaters into one `UpdateProfileRequest` |
| Watchdog | `runtime/watchdog.py` | Every 30 seconds | Heartbeat RPC + update staleness detection |
| Heartbeat | `runtime/heartbeat.py` | Every 30 seconds | Structured system snapshot (memory, CPU, tasks) |
| Task Diagnostics | `runtime/supervisor.py` | Every 60 seconds | Dumps all asyncio tasks with stack traces |
| Panel Timers | `helper/panel_timer.py` | Per-panel (default 120s) | Auto-close idle panels |

All workers are supervised by the `RuntimeSupervisor`, which uses a
10-state FSM (STARTING → CONNECTING → ... → READY → DEGRADED →
RECOVERING → ...). On crash, the supervisor performs atomic recovery:
stop cron engines → stop helper → clear panels → cancel orphans →
dispose dead client → rebuild → re-register handlers → resume cron.
After 5 failed recovery attempts, the process exits with code 1 so
Render restarts it.

---

## How Supabase Is Organized

- **Schema**: `public` (default)
- **Client**: `backend/db/client.py` — singleton, initialized on first
  access. Uses `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY`.
- **Threading**: All Supabase calls run in a worker thread via
  `asyncio.to_thread()` with a 10-second timeout. The event loop never
  blocks on HTTP.
- **Fallback**: When Supabase is unavailable (missing env vars or
  connection failure), every operation degrades to in-memory storage.
  The bot never crashes.
- **RLS**: Enabled on all tables. SELECT granted to `anon` +
  `authenticated` (read-only dashboard). All writes use the service-role
  key, which bypasses RLS.
- **Panel Settings**: Uses a column-per-setting model (12 typed columns)
  with a cache-first read, write-through cache architecture.

---

## How Deployment Works

The bot deploys as a **single web service** on Render:

1. **Start command**: `python -m backend.main`
2. **Health check**: FastAPI exposes `/health` → Render probes this
3. **Auto-restart**: If the supervisor exhausts recovery attempts, it
   calls `sys.exit(1)` so Render restarts the process
4. **Dashboard**: React dashboard built with Vite, served by FastAPI
   from `dist/` if present
5. **Environment**: All secrets provided via Render's env var dashboard
   (or `render.yaml` Blueprint)

---

## Environment Variables

### Required

| Variable | Type | Description |
|---|---|---|
| `API_ID` | int | Telegram API ID from my.telegram.org |
| `API_HASH` | str | Telegram API Hash from my.telegram.org |
| `SESSION_STRING` | str | Telethon StringSession (generated offline) |
| `BOT_OWNER_ID` | int | Telegram numeric user ID of the bot owner |

### Optional — Core

| Variable | Default | Description |
|---|---|---|
| `BOT_TOKEN` | `""` | Helper bot token for Inline Glass UI |
| `SUPABASE_URL` | `""` | Supabase project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | `""` | Supabase service role key |
| `TZ` | `Asia/Tehran` | Timezone for bio/username engines |
| `PORT` | `8000` | Web server port |
| `BIO_UPDATE_ENABLED` | `false` | Auto-start bio cron on boot |
| `LOG_LEVEL` | `INFO` | Python logging level |

### Optional — AI

All AI variables are optional. AI is off by default.

| Variable | Default | Description |
|---|---|---|
| `AI_ENABLED` | `false` | Enable the AI subsystem |
| `AI_PROVIDER` | `dummy` | Active provider name |
| `AI_MODEL` | provider default | Model name |
| `AI_TEMPERATURE` | `1.0` | Sampling temperature |
| `AI_TOP_P` | `1.0` | Nucleus sampling |
| `AI_MAX_TOKENS` | `4096` | Max output tokens |
| `AI_TIMEOUT` | `30` | Request timeout (seconds) |
| `AI_RETRY_COUNT` | `3` | Retry count on failure |
| `AI_PROVIDER_FALLBACK` | `""` | Comma-separated fallback chain |
| `AI_MEMORY_RETENTION_DAYS` | `90` | Long memory retention |

### Optional — AI Provider Keys

| Variable | Description |
|---|---|
| `AI_GEMINI_API_KEY` | Gemini API key |
| `AI_OPENAI_API_KEY` | OpenAI API key |
| `AI_OPENROUTER_API_KEY` | OpenRouter API key |
| `AI_GROQ_API_KEY` | Groq API key |
| `AI_CEREBRAS_API_KEY` | Cerebras API key |
| `AI_MISTRAL_API_KEY` | Mistral API key |

### Optional — AI Provider Model Overrides

| Variable | Description |
|---|---|
| `AI_GEMINI_MODEL` | Gemini model name |
| `AI_OPENAI_MODEL` | OpenAI model name |
| `AI_OPENROUTER_MODEL` | OpenRouter model name |
| `AI_GROQ_MODEL` | Groq model name |
| `AI_CEREBRAS_MODEL` | Cerebras model name |
| `AI_MISTRAL_MODEL` | Mistral model name |

---

## Render Deployment

The bot is designed for Render's Free tier and deploys as a single web
service.

1. **Create a new web service** on Render from this repository.
2. **Set the start command**: `python -m backend.main`
3. **Set the health check path**: `/health`
4. **Add environment variables** (see above) via Render's dashboard or
   `render.yaml` Blueprint.
5. **Deploy** — Render builds and starts the process.

If the runtime supervisor exhausts recovery attempts, it calls
`sys.exit(1)` so Render restarts the process automatically.

The `render.yaml` Blueprint in the repository defines the service and
all environment variables. Import it on Render for one-click setup.

---

## Render Keep-Alive / Cron

An external cron or monitoring service can periodically send an HTTP GET
request to the deployed Render service to keep it active and monitor
availability. This does not guarantee the service will never sleep, but
it helps reduce idle spin-down and provides uptime monitoring.

### Setup

1. Create an account on a cron/monitoring service (e.g. cron-job.org,
   UptimeRobot, or similar).
2. Create a new monitoring job targeting your deployed service URL.
   Use a generic placeholder such as `https://example.com` — never put
   the real deployment URL in committed documentation.

### Common Settings

| Setting | Value |
|---|---|
| Enable job | ON |
| Save responses in job history | ON |
| Execution schedule | Every 10 minutes (every 5 minutes also acceptable) |
| Notify when execution fails | ON |
| Notify when job is disabled due to failures | ON |

### Advanced Settings

| Setting | Value |
|---|---|
| Requires HTTP authentication | OFF |
| Headers | None |
| Time zone | Asia/Tehran |
| Request method | GET |
| Request body | Empty |
| Timeout | 30 seconds |
| Treat HTTP 3xx redirects as success | ON |

### How It Works

The external monitor periodically contacts the deployed service with a
GET request. This keeps the Render web service active and lets the
monitoring service track availability over time. It does not guarantee
the service will never sleep — Render's free tier may still spin down
the instance under certain conditions.

---

## Supabase Setup

Supabase is optional but recommended for persistence across restarts.

1. **Create a Supabase project** at [supabase.com](https://supabase.com).
2. **Run the SQL scripts** in the `sql/` directory via the Supabase SQL
   editor. These create the 5 core tables with all columns, indexes,
   and RLS policies.
3. **Copy the project URL** and **service role key** from
   Settings → API.
4. **Set environment variables**:
   ```
   SUPABASE_URL=https://your-project.supabase.co
   SUPABASE_SERVICE_ROLE_KEY=your-service-role-key
   ```
5. **AI tables**: The AI tables (`ai_sessions`, `ai_messages`,
   `ai_memories`, `ai_tool_history`, `ai_config`) have migrations
   applied via Supabase. See [DATABASE_ARCHITECTURE.md](DATABASE_ARCHITECTURE.md)
   for the exact schema.

The bot works without Supabase — all operations fall back to in-memory
storage. Data does not persist across restarts when Supabase is
unavailable.

---

## Development Workflow

### Prerequisites

- Python 3.11+
- Node.js 18+ (for dashboard build)
- A Telegram account with API credentials
- A Supabase project (optional)
- A Telegram bot token from BotFather (optional — for Inline Glass UI)

### 1. Clone and Install

```bash
git clone https://github.com/Onlyicing1/Telegram-self-bot.git
cd Telegram-self-bot
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
BOT_TOKEN=your_bot_token          # Optional
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

## Repository Philosophy

- **Single source of truth**: `DATABASE_ARCHITECTURE.md` is the only
  document needed to rebuild the complete Supabase schema.
  `AI_MASTER_DESIGN.md` is the only document needed to understand the
  AI architecture.
- **No dead code**: Every file has a reason to exist. Unused modules are
  deleted, not commented out.
- **One architecture**: There is one AI execution path (Engine →
  Dispatcher → Provider). No duplicate session managers, state
  machines, or persistence layers.
- **Graceful degradation**: The bot works with or without Supabase, with
  or without a helper bot, with or without AI providers. Every external
  dependency has a fallback.
- **Single event loop**: No threads, no multiprocessing. All I/O is
  async. Supabase calls are threaded internally via `asyncio.to_thread`
  with bounded timeouts.
- **Owner-only**: Every command and callback is gated by a single
  permission check. No public access to any feature.
- **Self-documenting**: Every public module has a docstring explaining
  its responsibility, dependencies, and what it should NOT do.

---

## Features

### Inline Glass UI

Interactive inline-button panels for all commands and settings.
Replaces plain-text commands with a tap-to-navigate experience.
Requires `BOT_TOKEN` (helper bot).

### Save System

- **Forward save** (`.save f`) — forwards to Saved Messages instantly
- **Deep save** (`.save d`) — download + re-upload with rich caption
- **Link save** — save from a Telegram message link
- **Metadata persistence** — full metadata in `saved_items` table
- **Save codes** — compact codes (e.g. `S0001`)

### Delete System

- Delete last N messages, from a message ID, or by save code
- Batch deletion with configurable batch size
- Recent messages browser for visual selection

### Bio Engine

Timezone-synchronized cron that rewrites your Telegram bio every minute
using `{time}`, `{mood}`, `{text}` template tokens. State persisted in
`bio_state` table.

### Username Engine

Mirrors the Bio Engine but controls the `first_name` field. Completely
independent — separate table, separate updater, separate state.

### Scheduler

Shared per-minute profile scheduler that merges all profile updaters
into a single `UpdateProfileRequest` API call per minute.

### Runtime Supervisor

FSM-based self-healing core with 10 states, atomic recovery, and
limited retries. Signal handling for deterministic shutdown.

### Watchdog

30-second heartbeat with update staleness detection. 3 consecutive
failures → client declared dead → recovery triggered.

### Diagnostics

Event log (`.logs`), diagnostic snapshot (`.kill`), asyncio task
diagnostics, and runtime heartbeat.

### AI Assistant

Trigger-word-based activation with full conversation context, memory,
and tool execution. Configure triggers in the AI Settings panel or
the web dashboard. See [How AI Works](#how-ai-works) above.

---

## Commands

All commands use the `.` prefix. Only fire on outgoing messages.

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
| `.ai <message>` | AI assistant (deprecated — use trigger words) |

### Save Engine

| Command | Description |
|---|---|
| `.save f` | Forward save to Saved Messages |
| `.save d` | Deep save (download + re-upload) |
| `.save` | Save panel (Inline Glass UI) |

### Retrieve & Discover

| Command | Description |
|---|---|
| `.retrieve` / `.r` / `.files` | Browse saved items |
| `.preview <code>` | Show metadata for a saved item |
| `.send <code>` | Forward saved asset to current chat |
| `.list [n]` | Show recent saved items |
| `.find <text>` | Search saved items |

### Delete

| Command | Description |
|---|---|
| `.del <n>` | Delete last N outgoing messages |
| `.del id <msgid>` | Delete from message ID forward |
| `.del <code>` | Delete a saved item |
| `.del` | Delete panel |

### Bio Engine

| Command | Description |
|---|---|
| `.bio` | Bio engine panel |
| `.bio on` / `.bio off` | Start / stop bio cron |
| `.bio show` | Show bio state |
| `.bio template <tpl>` | Set bio template |
| `.bio text <text>` | Set {text} token |
| `.bio mood <mood>` | Set {mood} token |

### Username Engine

| Command | Description |
|---|---|
| `.username` | Username engine panel |
| `.username on` / `.username off` | Start / stop username cron |
| `.username show` | Show username state |
| `.username template <tpl>` | Set username template |

### Database

| Command | Description |
|---|---|
| `.db` | Database panel |
| `.db clean` | Remove orphan rows |
| `.db stats` | Database statistics |
| `.db vacuum` | Cleanup + optimize |

---

## Troubleshooting

### Bot won't start

- Check required env vars (`API_ID`, `API_HASH`, `SESSION_STRING`,
  `BOT_OWNER_ID`).
- Check that the session string is valid (regenerate if needed).

### Panels not working

- Ensure `BOT_TOKEN` is set — the Inline Glass UI requires the helper
  bot.
- Without `BOT_TOKEN`, commands fall back to plain-text edit-in-place.

### Bio or Username engine not updating

- Check that the engine is active (`.bio show` or `.username show`).
- Check that the template contains at least one token.
- Check the shared Profile Scheduler is running (visible in `.health`).
- Check for `FloodWaitError` in logs.

### Database errors

- The bot works without Supabase — all operations fall back to in-memory.
- Check `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` are correct.
- Check that all migrations have been applied (see `sql/` directory).

### Client keeps disconnecting

- The watchdog automatically detects disconnections and rebuilds.
- Check `.health` for restart count and last rebuild reason.
- Check `.kill` for a full diagnostic snapshot.

### AI not responding

- Check `AI_ENABLED=true` is set.
- Check that a provider API key is configured (e.g. `AI_OPENAI_API_KEY`).
- Check that at least one trigger word is configured (English or Persian).
- Without an API key, the DummyProvider returns a placeholder.
- Without trigger words, AI will not activate.
- Check `.health` for the AI engine status.
- The old `.ai` command still works as a fallback (deprecated).

---

## License

This project is for personal use. See the repository for details.
