# AGENTS.md — LifeOS Telegram Self-Bot

> **This file is the single source of truth for the LifeOS repository.**
> Future AI sessions must read this document first before inspecting source
> files. If the code and this document disagree, the code is authoritative
> and this document must be updated.

---

## 1. Project Overview

LifeOS is a **headless Telegram self-bot** (userbot) that runs as a single
Python `asyncio` process. It uses **Telethon** with a `StringSession`
(never file-based, never interactive) to operate the owner's own Telegram
account. A **FastAPI** micro-server runs in the same process to serve
`/health` (Render/health checks) and a read-only React dashboard.

The user interface is **Glass UI first**: an inline panel system opened via
`.menu` and rendered through an optional helper bot. There is exactly ONE
text dot command (`.menu`); everything else is a panel/action/input or a
natural-language AI request addressed to the assistant (default trigger
`Nova`).

Core subsystems:

1. **RuntimeSupervisor** — the single recovery authority. Owns the self-client
   run loop, heartbeat, keepalive, failsafe, helper bot, profile scheduler,
   web server, and all reconnect/rebuild/full recovery layers.
2. **Save Engine** — Deep Save only (download → re-upload as a NEW Saved
   Messages message) with structured metadata persisted in Supabase (or an
   in-memory fallback).
3. **Profile Engines** — Bio (`about`) and Username (`first_name`), sharing a
   single parameterized `ProfileEngine` and one shared minute-boundary
   scheduler.
4. **AI Runtime** — provider abstraction (OpenAI, Gemini, OpenRouter, Groq,
   Cerebras, Mistral, Dummy), trigger/reply activation, and a tool layer that
   wraps the service layer.
5. **Utility panels** — Retrieve, Delete, Discover (list/find), Database,
   Settings, Health, and Context panels, all reached through `.menu`.

**Tech stack:** Python 3.11 · Telethon · FastAPI · Uvicorn · Supabase
(optional) · React 18 + Vite 5 + Tailwind CSS 3 (dashboard).

---

## 2. Repository Layout (backend)

```
backend/
├── main.py                          # asyncio entry point + crash diagnostics
├── config.py                        # env loader (hard-fail required, default optional)
├── requirements.txt
│
├── runtime/                         # recovery + supervision (see §4)
│   ├── supervisor.py                # RuntimeSupervisor — single recovery authority
│   ├── heartbeat.py                 # 30s snapshot + invariant checks
│   ├── keepalive.py                 # RPC keepalive pings
│   ├── failsafe.py                  # last-resort all-signals-frozen hard reset
│   ├── task_guard.py                # immortal_create_task / guarded_create_task
│   ├── operation_watchdog.py        # bounded_operation (dormant — no callers)
│   ├── tg_retry.py                  # tg_rpc helper (dormant in prod, tested)
│   ├── startup_check.py             # run_startup_checks (dormant in prod, tested)
│   ├── crash_diagnostics.py         # exit-reason + crash snapshot capture
│   ├── diagnostics.py, health_check.py, memory_cleanup.py, states.py, tracer.py
│
├── bot/
│   ├── client.py                    # Telethon self-client factory (StringSession)
│   ├── router.py                    # register_all() wires every handler
│   └── handlers/
│       ├── guard.py                 # is_owner() — single permission gate
│       ├── misc.py                  # .menu (mother panel) + settings/health/context panels
│       ├── save.py                  # Deep Save panels/actions/inputs (no forward)
│       ├── retrieve.py              # saved-items browser panels (under Save)
│       ├── delete.py                # delete panels/actions/inputs
│       ├── discover.py              # list + find panels
│       ├── database.py              # database maintenance panel
│       ├── bio.py                   # Bio profile panels
│       ├── username.py              # Username profile panels
│       ├── ai.py                    # AI config/status panels + AI trigger config inputs
│       ├── ai_unified.py            # canonical trigger/reply AI activation
│       └── organize.py              # no-op stub (moved to other panels)
│
├── profile/                         # shared Bio/Username engine
│   ├── engine.py                    # ProfileEngine (parameterized about/first_name)
│   └── scheduler.py                 # ONE shared minute-boundary scheduler
├── bio/engine.py                    # thin Bio wrapper over ProfileEngine
├── username/engine.py               # thin Username wrapper over ProfileEngine
│
├── services/                        # business logic (handlers/tools delegate here)
│   ├── save_service.py              # execute_save (Deep Save pipeline) + link save
│   ├── retrieve_service.py, delete_service.py, discover_service.py,
│   ├── database_service.py, settings_service.py, organize_service.py
│   ├── bio_service.py, username_service.py
│
├── ai/                              # AI runtime (providers, tools, conversation, memory)
│   ├── providers/                   # factory + registry + per-provider modules
│   ├── engine/                      # engine, dispatcher, hooks, metrics
│   ├── tools/                       # Tool base, registry, executor, per-domain tools
│   ├── conversation/                # session, history, context builder
│   ├── memory/                      # short/long/permanent memory
│   ├── persistence.py, diagnostics.py, config_store.py
│
├── helper/                          # Glass UI machinery
│   ├── client.py                    # optional helper bot factory
│   ├── inline_engine.py             # inline panel dispatch + self/helper refs
│   ├── inline_sender.py             # send_inline_panel + input listener
│   ├── input_state.py               # per-owner pending-input state
│   ├── panel_registry.py, panels.py, panel_render.py, lifecycle.py,
│   ├── session_manager.py, rpc_timeout.py
│
├── telegram_api/                    # typed RPC wrappers over the self client
├── db/client.py                     # Supabase singleton + in-memory fallback
├── health.py                        # runtime health timestamps/telemetry
├── diagnostics.py                   # runtime diagnostics helpers
├── observability/                   # stats, health snapshot, crash report, maintenance
└── web/app.py                       # FastAPI: /health, /api/*, static SPA
```

---

## 3. Entry Point & Lifecycle (`backend/main.py`)

`python -m backend.main` does:

1. `config.load()` — hard-exits on missing required vars.
2. Install crash diagnostics (signal handlers, exception hooks, `atexit`).
3. Create `RuntimeSupervisor(cfg)` and call `await supervisor.start()` with up
   to 5 retries (exponential backoff) before `sys.exit(1)`.
4. Wait on `supervisor.shutdown_event`; on signal, `supervisor.stop()` runs the
   deterministic shutdown of all tasks, profile scheduler, helper, and self-client.

`RuntimeSupervisor.start()` is the real startup authority: it connects and
authorizes the self client, registers handlers via `router.register_all()`,
starts the optional helper bot, resumes the profile scheduler, starts the web
server, heartbeat, keepalive, and the immortal self-client run loop.

---

## 4. Runtime Stability & Recovery

`RuntimeSupervisor` (`backend/runtime/supervisor.py`) is the **single recovery
authority**. No other module owns connection lifecycle or recovery decisions.

Key facts:

- **Single connection ownership** — `_run_loop()` runs
  `client.run_until_disconnected()`; `_trigger_reconnect()` first stops the run
  loop, then disconnects/connects, then restarts it. `_run_loop` yields (breaks)
  when it detects `_recovery_lock.locked()`, so an intentional recovery
  disconnect is never fought as an unexpected permanent death.
- **Recovery lock** — all reconnect/rebuild/full recovery transitions serialize
  through `self._recovery_lock` (`asyncio.Lock`).
- **Reconnect cooldown** — successful lightweight reconnects set a monotonic
  `_reconnect_cooldown_until` (`_RECONNECT_COOLDOWN` seconds); subsequent
  `_trigger_reconnect()` calls no-op during cooldown. This prevents reconnect
  storms.
- **Heartbeat** (`backend/runtime/heartbeat.py`) — every 30s logs a structured
  snapshot. It triggers recovery ONLY on real invariants:
  - `READY_BUT_DISCONNECTED` when the self client is down, or when the helper
    is down **and** `helper_enabled=True`. A disabled helper
    (`helper_connected=False`, `helper_enabled=False`) is a valid state and
    never triggers reconnect.
  - `EVENT_DISPATCH_STALLED` only when updates ARE arriving but no event was
    dispatched for the threshold. A quiet/idle account (no updates, no
    callbacks) is **normal** and is never treated as a stall.
- **Keepalive** pings Telegram RPCs so a healthy-idle account still proves the
  connection is alive.
- **Failsafe** (`backend/runtime/failsafe.py`) — independent last-resort
  monitor; if all four signals (loop progress, heartbeat, update, RPC) stay
  frozen past the threshold, it schedules `_hard_reset_runtime()` via
  `guarded_create_task`.

Dormant paths (source-verified, left in place because they contain useful
logic or are under test — see INVESTIGATION.md):
- `RuntimeSupervisor._watchdog_loop` (never started in `start()`).
- `backend/runtime/startup_check.run_startup_checks` (tested, not called in prod).
- `backend/diagnostics.recover_stalled` (no callers).
- `backend/runtime/tg_retry.tg_rpc` (tested, not called in prod).
- `backend/runtime/operation_watchdog.bounded_operation / attach_task` (no callers).

---

## 5. Command & Activation Reference

All handlers fire on `events.NewMessage(outgoing=True)`. Every handler calls
`is_owner(event, owner_id)` first; non-owners are silently ignored.

### Dot command (exactly one)

| Command | Pattern | Behavior |
|---|---|---|
| `.menu` | `^\.menu$` | Opens the Glass UI mother panel (inline via helper bot; falls back to edit-in-place text if the helper is unavailable). |

There are no other dot commands. Legacy text commands (`.ping`, `.help`,
`.save`, `.del`, `.bio`, `.username`, `.ai`, ...) have been removed.

### AI activation (no dot command required)

`backend/bot/handlers/ai_unified.py` is the **single canonical** AI activation
handler. It fires on every outgoing non-dot message and supports:

1. **Trigger mode** — message starts with the configured trigger word
   (default English trigger `Nova`, e.g. `Nova <text>`); the trigger is
   stripped and the rest is the prompt.
2. **Reply-aware trigger** — a reply to a message using the trigger word; the
   replied-to content is injected as context.
3. **Reply-to-AI** — replying to a known AI message with plain text activates
   the AI with that text (continuation).

The trigger/reply handler resolves the intent into **native tool calls**
(save, delete, search, ...) and executes them through the shared service
layer — it is an execution interface, never just a text response.

### Glass UI (primary interface)

`.menu` opens the mother panel. Registered top-level panels:

- **Save** (`📥`) → **Deep Save** → **Reply Mode** or **link**; plus **Retrieve**.
- **Delete** (`🗑`) → reply-from / recent / manual-id modes.
- **List** (`📋`) and **Find** (`🔍`) — browse/search saved items.
- **Database** (`🗄`) — DB maintenance.
- **AI** (`🧠`) — provider/model/settings/status/diagnostics + trigger-word config.
- **Profile** → **Bio** (`🧬`) and **Username** (`👤`).
- **Settings / General**, **Context Panel**, **Health Dashboard**.

Actions (`action:*`), panels (`panel:*`), and inputs (`input:*`) are registered
through `backend/helper` registries and dispatched by `inline_engine`.

---

## 6. Save System (Deep Save only)

There is **no Forward Save**. The Save path must never call
`client.forward_messages(...)` and must never import/emit a
`ForwardMessagesRequest`. Forwarding exists only inside retrieval
(`retrieve_service.do_retrieve` re-sends a saved asset to a chat).

Deep Save pipeline (`backend/services/save_service.py::execute_save`):

```
source message
  → download media to a safe local/temp buffer
  → validate the download (non-empty, within limits)
  → upload as a NEW message to Saved Messages
  → extract the new message metadata (saved chat/msg id)
  → persist the record in the DB (after upload)
  → return an honest result string
```

- **Glass UI flow**: `.menu` → Save → Deep Save → Reply Mode → reply to the
  target message. The handler resolves `reply_to_msg_id` to the exact target
  message and passes that object to `execute_save` — never the user's reply.
- **Reply-mode timeout**: the pending-input listener uses `timeout=None` for
  Deep Save reply mode, so large transfers are not cancelled by a blanket
  timeout. The separate 120s pending-input **state expiry** is unchanged.
- **AI Save tool**: `SaveTool` marks `long_running=True`; the
  `ToolExecutor` skips the generic 10s tool timeout for long-running tools.

**Save code format:** short, human-readable codes — `S` + 4 characters
(e.g. `S0001`, or a random `SXXXX` on collision). No `SV-NNNNNN` format.

---

## 7. Profile Engines (Bio + Username)

Bio and Username are thin wrappers over a shared, parameterized
`ProfileEngine` (`backend/profile/engine.py`):

- **Bio** updates the Telegram `about` field; state table `bio_state`; state
  key `last_bio`. Default template: `🕒 {time} | 💭 {mood}`.
- **Username** updates `first_name`; state table `username_state`; state key
  `last_name`.

`backend/profile/scheduler.py` runs **one shared minute-boundary scheduler**.
Each engine registers its active flag via `set_engine_active`; the scheduler
stops only via `stop_if_idle()` when **no** engine is active. Turning Bio OFF
does not stop Username, and vice versa.

Public wrappers (`bio/engine.py`, `username/engine.py`) preserve the
`about`/`first_name` field distinction, default templates, and public
`start/stop/is_running` interfaces.

---

## 8. Database Layer (`backend/db/client.py`)

Singleton Supabase client with an automatic in-memory fallback. Every public
function wraps its Supabase call in `try/except`; on any failure it logs a
warning and uses the in-memory fallback. The bot never crashes due to a DB
error.

- Writes go through the **service-role key** (bypasses RLS). The anon key is
  read-only and only used by the dashboard (via the backend API).
- Tables: `saved_items`, `bio_state`, `username_state`, `bot_logs` (and AI
  tables). See `DATABASE_ARCHITECTURE.md` and `supabase/migrations/`.
- `get_next_save_code()` is atomic (`asyncio.Lock`) and returns the short
  `S####` format.
- Heavy Supabase calls run via `asyncio.to_thread` with a bounded timeout so
  they cannot block the event loop.

---

## 9. AI System

- **Entry point**: trigger/reply activation (via `ai_unified.py`), which
  delegates to `_execute_ai`. Default English trigger word is `Nova`.
- **Native tools**: the dispatcher passes OpenAI-format tool definitions to
  providers (Gemini translates them to `functionDeclarations`), so the model
  emits real function calls that the `ToolExecutor` runs.
- **Providers**: `openai`, `gemini`, `openrouter`, `groq`, `cerebras`,
  `mistral`, and `dummy`. Provider selection and fallback live in
  `backend/ai/providers/`.
- **Tools**: stateless wrappers over services (`save`, `retrieve`, `delete`,
  `bio_*`, `username_*`, `organize_*`, `settings_*`, …). The `ToolExecutor`
  is the sole component that calls `tool.execute()`. The owner's message IS
  the authorization in this single-owner self-bot, so deterministic
  destructive tools (delete with an explicit count) execute directly;
  `ADMIN_ONLY`/`CONFIRMATION_REQUIRED` still require confirmation. Tools
  with `long_running=True` (Deep Save) are exempt from the generic tool
  timeout.
- **HTTP**: providers use `httpx.AsyncClient` (async) — no sync HTTP in the
  loop. The handler wraps the AI request in a bounded `wait_for`.
- **Persistence**: `backend/ai/persistence.py` + `backend/ai/database/`
  record config, sessions, usage, tool history, and provider stats with the
  same Supabase-or-fallback pattern.

---

## 10. Helper Bot (optional)

The Glass UI's inline panels are rendered through an optional helper bot
(`BOT_TOKEN` → `HELPER_BOT_ENABLED`). If no `BOT_TOKEN` is set, the helper is
disabled (`helper_enabled=False`) and the self-bot falls back to edit-in-place
text panels. A disabled helper is a **valid** runtime state — it must never be
treated as a disconnect or trigger recovery.

---

## 11. Environment Variables

### Required (hard-fail)

| Variable | Description |
|---|---|
| `API_ID` | Telegram API ID |
| `API_HASH` | Telegram API hash |
| `SESSION_STRING` | Telethon StringSession (generated offline; never logged/committed) |
| `BOT_OWNER_ID` | Numeric owner user ID |

### Optional (defaults)

| Variable | Default | Description |
|---|---|---|
| `SUPABASE_URL` | `""` | Supabase URL (empty → in-memory fallback) |
| `SUPABASE_SERVICE_ROLE_KEY` | `""` | Supabase service-role key |
| `BOT_TOKEN` | `""` | Helper bot token (empty → helper disabled) |
| `HELPER_BOT_ENABLED` | `false` | Enable the helper bot inline UI |
| `TZ` | `Asia/Tehran` | Timezone for profile engines + timestamps |
| `PORT` | `8000` | Web server port |
| `BIO_UPDATE_ENABLED` | `false` | Auto-start Bio engine on boot |
| `USERNAME_UPDATE_ENABLED` | `false` | Auto-start Username engine on boot |
| `LOG_LEVEL` | `INFO` | Logging level |
| `AI_PROVIDER` / AI keys | varies | AI provider selection + credentials (see `backend/ai/config/`) |

Frontend-only: `VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY` (unused by the
React app; the dashboard reads through the backend API).

---

## 12. API Endpoints (`backend/web/app.py`)

| Method | Path | Behavior |
|---|---|---|
| GET | `/health` | `{"status": "ok"}` |
| GET | `/api/saves` | Paginated saved items (`limit`, `offset`) |
| GET | `/api/saves/{save_code}` | Single item or 404 |
| GET | `/api/bio` | Bio state (plus username state where present) |
| GET | `/api/logs` | Recent logs |
| GET | `/` | React SPA (if `dist/` exists) or API status |
| GET | `/{path}` / `/assets/*` | SPA fallback + static assets |

Docs (`/docs`, `/redoc`) are disabled.

---

## 13. Coding Rules

1. **Match existing conventions** — read neighboring files first.
2. **No comments unless necessary** — comment the "why", never the "what".
3. **Single responsibility** — one handler per feature; services hold business
   logic; tools/handlers are thin wrappers.
4. **Edit-first / zero-spam** — command responses edit in place; the Glass UI
   edits its panel message; no new-message spam.
5. **Async everything** — no blocking calls, no threads (DB sync helpers use
   `asyncio.to_thread`).
6. **try/except at boundaries** — wrap external I/O (Telegram, DB, AI HTTP).
   `asyncio.CancelledError` must always be re-raised.
7. **No premature abstraction** — two concrete use cases before abstracting.
8. **Reuse before adding** — check existing services/utilities first.
9. **Leave the tree clean** — no orphaned files, dead exports, or commented-out
   blocks.
10. **Single recovery authority** — never add a second supervisor; route
    reconnect/rebuild through `RuntimeSupervisor`.

---

## 14. Security Rules

1. **Owner-only access** — every handler calls `is_owner`; non-owners are
   silently ignored.
2. **No hardcoded secrets** — all credentials come from `os.getenv()`.
3. **StringSession only** — never file-based, never logged.
4. **No secrets in logs**.
5. **Service-role key** for all Supabase writes; RLS enabled on tables.
6. **`.env` gitignored** — never committed.
7. **Docs endpoints disabled** in production.
8. **Outgoing-only commands** — all handlers fire on outgoing messages from the
   owner's own account.

---

## 15. Git Workflow Rules

1. **Commit format:** `type: description`. All changes for one request in ONE
   commit; then push.
2. **One concern per commit.**
3. **Never commit secrets.**
4. **Never force-push** unless explicitly authorized and explained.
5. **Verify before pushing** — compile checks + tests pass.
6. **Remote:** always push to the repository already connected to the current
   workspace; never hardcode a URL or owner name.

---

## Document Version

This document reflects the current Glass-UI-first architecture. If code changes
invalidate any section, update this document in the same commit.
