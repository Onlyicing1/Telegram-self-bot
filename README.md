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

You interact through the **Inline Glass UI** — `.menu` opens the panel
system — and through the **AI assistant**: address it by name (e.g.
`Nova`) and it **executes real actions**, not just text. Responses edit
your message in-place. Zero spam, zero new messages.

**`.menu` is the only dot-prefixed text command.** All legacy dot
commands (`.ping`, `.help`, `.save`, `.del`, `.bio`, `.username`,
`.ai`, ...) have been removed in favor of the Glass UI and AI execution.

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
│   │   ├── router.py           # Handler registration (all handlers)
│   │   └── handlers/           # Glass panels + AI activation
│   │       ├── misc.py         # .menu (mother panel) + settings/health/context
│   │       ├── save.py         # Deep Save panel/actions/inputs
│   │       ├── retrieve.py     # Saved-items browser (under Save)
│   │       ├── delete.py       # Delete panel/actions/inputs
│   │       ├── discover.py     # List + Find panels
│   │       ├── database.py     # Database maintenance panel
│   │       ├── bio.py          # Bio Glass panel
│   │       ├── username.py     # Username Glass panel
│   │       ├── ai.py           # AI Glass Panel (settings, provider, triggers, etc.)
│   │       ├── ai_unified.py   # Trigger/reply AI activation (Nova)
│   │       ├── guard.py        # Owner-only permission check
│   │       └── organize.py     # no-op stub (moved to other panels)
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

1. The trigger handler (`ai_unified.py`) detects a trigger match (or a
   reply to a known AI message).
2. It restores the saved provider/model from Supabase.
3. It builds an `AIRequest` with the stripped message plus Telegram
   context (current chat, replied-to message, sender, media, ...).
4. The Engine delegates to the Dispatcher, which runs 6 stages:
   - Gets or creates a conversation session for the owner
   - Builds the prompt (system prompt + conversation history + memory)
   - Injects native tool definitions and routes to the active provider
   - Calls the provider's `chat()` method (with native function calling)
   - Runs tool calls through the ToolExecutor (save, delete, search, ...)
   - Returns an `EngineResult`
5. The handler edits the triggering message with the AI response.

### AI Execution (Nova is an execution agent, not just chat)

When the owner asks Nova to do something, the AI resolves the intent
into a **real tool call** on the existing execution layer. The AI never
touches Telegram directly — it only decides *what action* and *which
target*, then the existing service performs it:

```
Nova, save this                  → SaveTool            → execute_save()
Nova, deep save this             → SaveTool            → execute_save() (Deep Save)
Nova, save this link             → SaveByLinkTool      → execute_link_save() → execute_save()
Nova, delete this message        → DeleteRepliedTool   → delete the replied message
Nova, delete the last 5 messages → DeleteTool          → delete_service.do_del_last_n_real()
Nova, delete message ID 123      → DeleteMessageByIdTool → delete that one outgoing message
Nova, delete messages about X    → ListRecentMessagesTool → DeleteMessagesByIdsTool (bounded + validated)
Nova, what have I saved          → ListSavesTool       → discover_service.do_list()
Nova, search saved items for X   → SearchTool          → discover_service.do_find()
Nova, database status            → DatabaseStatsTool   → database_service.do_stats()
Nova, username/bio status        → UsernameShowTool / BioShowTool → existing profile-service state
```

The execution pipeline is:

```
Natural language (Persian or English)
  → AI trigger / handler
  → LOCAL deterministic fast path (high-confidence command vocabulary)
  → provider abstraction (active provider → fallback chain)
  → intent resolution (native tool call / JSON action)
  → argument + target validation
  → real execution (existing service function)
  → real result (success/failure)
  → AI response based on the REAL result
```

AI and the Glass UI converge on the **same** service functions
(`save_service`, `delete_service`, …) — the AI never reimplements a
feature and never reports fake success.

### Local deterministic fast path (provider-independent)

High-confidence command intents are executed **before** any provider
round, through the SAME `ToolExecutor`. This is an optimization for
obviously structured intents — NOT a keyword command parser replacing the
AI — and it is the reason deterministic operations keep working when
Groq/Gemini/etc. are rate-limited, misconfigured, or down:

- Read-only status/review: `list_saves`, `search`, `database_stats`,
  `get_bio` (the REAL current Telegram bio via `get_me`), `bio_show` /
  `username_show` (engine state), account-identity queries
  (`account_show` — actual `get_me` identity, requested fields only),
  plus `list_recent_messages` (real Telegram history).
- Save: `save` / `save_link` (Deep Save only).
- Delete: only when the target is unambiguous — an explicit message ID,
  an explicit multi-message count (`ده پیام آخر رو پاک کن`), or a
  replied-to message (`اینو پاک کن` while replying).

Conversational requests (`هستی؟`) and semantic requests
(`پیام‌های مربوط به دعوا رو پاک کن`) are NOT fast-pathed — they still go
through the AI so the model can reason over real chat history. A bare
"last/recent" without an explicit count is also left to the AI, because
"اخیر" in a semantic phrase like "دعوای اخیر" is not the same as
"delete the last message". This keeps destructive actions conservative.

### Target Resolution

The AI resolves the target from Telegram context instead of asking for
message IDs when the target is already clear:

| Owner says | Resolved target |
|---|---|
| "this message" / "اینو" / "این پیام" while replying | the replied-to message |
| "the last message" / "پیام آخر" | the most recent message (delete count=1) |
| "the last N messages" / "N پیام آخر" | count=N over ALL real chat messages (owner, others, Nova's own/edited) — the system deletes only the outgoing subset |
| "save this" / "اینو سیو کن" while replying | the replied-to message |
| "save this link" / "این لینک رو سیو کن" + t.me link | the linked message (Deep Save, URL preserved) |
| "delete message ID N" / "پیام با ID N رو پاک کن" | that one outgoing message by ID |
| "delete messages about X" / "پیام‌های مربوط به X رو پاک کن" | bounded candidate list → AI selects → validated delete |
| "review the last N messages" / "N پیام آخر رو ببین" | the last N REAL Telegram messages (all participants) |

Deletion follows the project's **outgoing-only** rule: only the owner's
own sent messages can be deleted. If the target genuinely cannot be
resolved, the AI asks one clarifying question — clarification is the
exception, not the default.

Persian, informal/colloquial Persian, and mixed Persian-English commands
are supported. Persian digits (۰-۹) in numeric arguments are normalized
before validation. Exact values such as usernames, URLs, and quoted text
are preserved verbatim.

### Account identity semantics (first name vs real @username)

The LifeOS "username engine" manages the account **first name**, so in
natural-language intent "یوزرنیم" / "username" means the first name:

| Owner says | Resolved tool + fields |
|---|---|
| "وضعیت یوزرنیمم رو بگو" / "یوزرنیمم چیه؟" | `account_show` `fields=["first_name"]` |
| "اسم اکانتم چیه؟" / "وضعیت اسم اکانتم رو بگو" | `account_show` `fields=["first_name"]` |
| "what is my account name?" / "show my first name" | `account_show` `fields=["first_name"]` |
| "یوزرنیم واقعی تلگرامم رو بگو" / "username تلگرامم رو بگو" | `account_show` `fields=["username"]` |
| "@username من چیه؟" / "what is my Telegram username?" | `account_show` `fields=["username"]` |

The distinction is resolved at the intent layer (`_parse_status_intent`)
before tool selection: an explicit `@` / "واقعی" / "تلگرام" / "telegram"
qualifier selects the real `@username`; without one, the request is a
first-name query. `account_show` itself validates a `fields` allowlist
(`first_name` / `last_name` / `full_name` / `username`) and **never
returns phone, account ID, session, or credential data** — the structured
`data` payload and the rendered message contain only the requested fields.

### Structured Action Contract

The AI model is an intent interpreter — never the executor. Executable
intent is resolved deterministically before the model's prose is ever
trusted:

1. Native tool call from the provider (when the model emits one).
2. Deterministic command parser
   (`backend/ai/actions.py:parse_command_intent`) over the ORIGINAL user
   message — the authoritative path for the save / deep-save / delete
   vocabulary (Persian + English, incl. negation and Persian digits).
3. JSON action object embedded in the model's text response (secondary
   fallback for cases the deterministic vocabulary does not cover).

This is why a model reply like "which message?" no longer turns a clear
command into a no-op: when the provider returns prose, the system reads
"اینو پاک کن" / "delete last 3" directly and executes the matching tool.

Every path is normalized into a strict, typed action schema and validated
locally before any execution:

```json
{"action": "save" | "deep_save" | "save_link" | "delete_messages"
          | "list_saved_items" | "search_saved_items" | "list_recent_messages"
          | "database_stats" | "bio_status" | "get_bio" | "username_status" | "account_status",
 "target": "replied_message" | "current_message" | "last_message" | "recent_messages" | "message_id",
 "count": 1..500,
 "link": "https://t.me/...",
 "message_id": <int>,
 "query": "search term"}
```

```
user message + provider output
  → deterministic command intent (original user message) OR JSON action
  → local validation (action / fields / count / target)
  → target resolution (semantic target → existing tool + args, from reply context)
  → existing ToolExecutor → service → Telegram
  → real result → response
```

- Unknown actions, unknown fields, invalid counts, and unsupported
  targets are rejected locally and never reach Telegram. There is no
  tool that lets the model invoke an arbitrary Telegram method — the
  executor is a fixed allowlist of registered tools.
- `save` / `deep_save` → the existing Deep Save executor (captions are
  always preserved). `save_link` → `execute_link_save()` (the same Deep
  Save pipeline, link preserved verbatim). `delete_messages` → `delete`
  (last N **real** messages — all participants counted, outgoing-only
  deletion), `delete_replied` (replied message), `delete_message_by_id`
  (explicit single ID), or the semantic `list_recent_messages` →
  `delete_messages_by_ids` flow.
- Read-only status/query actions map deterministically to existing tools:
  `list_saved_items` → `list_saves`, `search_saved_items` → `search`,
  `database_stats` → `database_stats`, `bio_status` / `get_bio` →
  `get_bio` (reads the actual Telegram bio through the self client),
  `username_status` → `username_show`, `account_status` → `account_show`.
  `bio_show` remains the bio-ENGINE state panel (template/mood/status) for
  explicit engine queries.
  The deterministic command parser recognizes "چه چیزایی سیو دارم؟",
  "وضعیت دیتابیس چیه؟", "وضعیت یوزرنیمم رو بگو",
  "وضعیت اسم اکانتم رو بگو", and English equivalents, so these execute
  even when the provider returns prose instead of a tool call.
- `account_show` reads the authenticated self account identity from the
  Telegram client's `get_me()` — it never depends on a provider
  hallucinating the value. **Account-identity semantics:** in this project
  casual Persian "یوزرنیم" / "username" means the account **first name**
  (the username engine updates `first_name`), so "وضعیت یوزرنیمم رو بگو" /
  "اسم اکانتم چیه؟" → `account_show` with `fields=["first_name"]`. The
  REAL Telegram `@username` is returned only when explicitly qualified
  ("@username", "یوزرنیم واقعی تلگرامم", "username تلگرامم رو بگو",
  "what is my Telegram username?") → `fields=["username"]`. `account_show`
  returns **only the requested fields** — phone number and account ID are
  never serialized (data minimization).
- Provider/model configuration is validated at request time: stale or
  deprecated models (e.g. a Gemini model that no longer exists) are
  resolved to a valid default or marked unavailable and skipped, and the
  OpenAI-compatible request builder only sends `tool_choice` when it does
  not contradict native tool calling (fixes Groq's "tool choice is none,
  but model called a tool" 400).
- `list_recent_messages` reads the **real Telegram chat through the
  active Telethon client** — all participants, chronological
  (oldest → newest) — never the AI conversation/session history. It
  exposes message IDs, sender, time, text/caption, reply target, and a
  media flag. This is the canonical Telegram-history retrieval path.
- Semantic delete is **bounded and validated**: `list_recent_messages`
  returns a bounded candidate window (default 50, max 100) of the actual
  recent chat messages, the AI selects concrete IDs it actually saw, and
  `delete_messages_by_ids` re-fetches and validates every ID
  (outgoing-only) before deleting. Invented/foreign/non-outgoing IDs are
  skipped — a vague request never escalates into an unbounded or
  arbitrary delete.
- **Self-only deletion is enforced in code, not prompt.** Every AI-driven
  chat deletion (and every `delete_service` entry point used by the
  Glass UI) funnels through
  `delete_service.delete_verified_self_messages` — the single
  ownership-verification chokepoint immediately before the Telegram
  delete API. Each candidate ID is re-fetched from the actual chat and
  must carry the server-side `out` flag AND a sender ID that matches the
  authenticated account (`client.me`/`get_me`). Fail-closed: a missing
  message, missing/unknown sender, or a sender that is not the self
  account is rejected and never reaches `client.delete_messages`. Ranges
  (last-N, from-ID, time windows, "all messages", semantic targets)
  define the candidate area; ownership verification defines what may
  actually be deleted. A replied-to message or user/AI-supplied ID is
  only ever a reference — it grants no deletion right on its own.
- `send`, `clean_chat`, and `remember` are recognized but deliberately
  return "unsupported" because no existing executor is wired — the AI
  never fabricates a result.
- If the target is genuinely ambiguous, the system asks one clarifying
  question instead of guessing.
- **Silent delete**: a successful pure-delete execution (any of
  `delete`, `delete_replied`, `delete_by_id`, `delete_message_by_id`,
  `delete_messages_by_ids`) ends without any Telegram confirmation — no
  "deleted" / "Deleted successfully" text, no follow-up message, no
  fallback reply when the delete removed the request message itself.
  The tool result stays internal (logs, conversation history,
  telemetry) and the request message is reverted to the owner's
  original text when it still exists. Failed deletes are NOT silent:
  the error reaches the user and can never read as a success
  confirmation. Non-delete AI actions are unaffected.

### Security boundary

The AI is an intent interpreter — never the executor and never an
arbitrary Telegram controller:

- Only explicitly registered tools may execute. The model cannot invent
  an action, a Telegram method, a Python/shell command, or a file
  operation at runtime; unknown tool names are rejected by the executor.
- Tools receive only the injected client/facade and runtime context —
  never `StringSession`, `API_ID`/`API_HASH`, Supabase credentials,
  provider API keys, or environment secrets. Those values never enter
  the prompt, tool results, or logs.
- Telegram message content (replied text, search results, candidate
  messages) is treated as **untrusted DATA**, never instructions. The
  system prompt forbids the model from following instructions embedded
  in message content or letting it change rules, scope, or permissions.

### Request Lifecycle, Timeouts & Concurrency

Every AI request is tracked with a single request id through these
stages and cleaned up in a `finally` block, so `ai_active` always
returns to 0:

```
AI_REQUEST_START → TELEGRAM_CHAT_RESOLVE → AI_CONFIG_LOAD → AI_PROVIDER_RESOLVE
  → AI_PROMPT_BUILD → AI_PROVIDER_REQUEST_START → AI_PROVIDER_RESPONSE
  → AI_ACTION_PARSE_START → AI_ACTION_PARSE_RESULT → AI_ACTION_VALIDATION
  → AI_TARGET_RESOLUTION → AI_EXECUTION_START → AI_EXECUTION_RESULT/ERROR
  → AI_RESPONSE_SEND_START → AI_RESPONSE_SEND_END → AI_REQUEST_END
```

For `list_recent_messages`, the real chat retrieval is logged as
`LIST_RECENT_MESSAGES_START` / `LIST_RECENT_MESSAGES_RESULT` with
`chat_id`, `requested_limit`, `returned_count`, `first_message_id`, and
`last_message_id` (message contents are never logged).

A single `AI_EXEC_TRACE` prefix spans the whole journey so one Telegram
request can be followed across provider and tool attempts without logging
secrets:

```
AI_EXEC_TRACE stage=telegram_received
AI_EXEC_TRACE stage=intent_resolved intent=... kind=...
AI_EXEC_TRACE stage=provider_selected provider=...
AI_EXEC_TRACE stage=provider_request
AI_EXEC_TRACE stage=provider_response success=... structured=...
AI_EXEC_TRACE stage=tool_selected tools=[...]
AI_EXEC_TRACE stage=tool_execute
AI_EXEC_TRACE stage=tool_result tool=... success=...
AI_EXEC_TRACE stage=telegram_response success=...
```

Tool arguments are logged as **keys only**, never values — no message
contents, API keys, `StringSession`, or credentials appear in the trace.

- Each AI request is bounded by a 60 s `wait_for`; each provider HTTP
  request is bounded by a 30 s guard.
- A transient **empty** provider response (a "thinking stall": `success`
  with no text and no tool call) is retried exactly **once** before being
  classified. This retry happens before any tool executes, so a
  destructive save/delete can never run twice from the same request.
- When the provider returns **prose** with no resolvable action (no tool
  call, no JSON, and no deterministic command match), exactly **one**
  bounded recovery retry re-asks the model to emit a structured tool
  call / JSON action. No tool has executed yet, so a destructive
  save/delete can never double-run. If the recovery still yields prose,
  the original conversational answer is kept (the format-enforcement
  nudge is never surfaced to the user).
- Provider failures are classified: `429` → cooldown (honoring
  `Retry-After`), `401/403` → disabled until config changes, `5xx` /
  network / timeout → one bounded retry (after a short backoff) then
  cooldown, and `404`/invalid model → surfaced as a deterministic config
  error without cooldown or infinite retry. Cooldown expires on a
  monotonic clock, and a provider that fails 5 times in a row is
  quarantined (circuit breaker) and automatically re-admitted after the
  quarantine window — a provider is never permanently stuck.
- Providers are chosen by capability-aware scoring (reliability, latency,
  failure/success streaks, tool-calling support), and every request
  records a failure matrix showing why each candidate was skipped,
  failed, or succeeded.
- `model_not_found` is surfaced with the provider's actual detail (the
  rejected model is identifiable in the message) instead of a generic
  "model not found" string, and the failure is logged with the selected
  provider + model so misconfiguration is diagnosable.
- Concurrency is bounded (default 4; override with
  `AI_MAX_CONCURRENCY`). Requests beyond the limit fail cleanly rather
  than piling up.
- Request telemetry (`last_request_at` / `last_latency_ms`) is a
  fire-and-forget **targeted update** — normal inference never rewrites
  the owner's AI configuration.
- A provider failure, timeout, empty output, parsing failure, tool
  failure, or Telegram delivery failure all result in a controlled error
  response and cleanup of the request state.

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

### Provider Mesh

All providers are registered in a `ProviderRegistry`. The
`ProviderManager` is a **capability-aware router**, not a fixed ordered
list: every eligible provider (active provider → configured fallback
chain → any other registered provider) is scored by capability match,
recent reliability, latency, and failure/success streaks, then tried in
score order until one succeeds. The active provider (the user's explicit
choice) is always preferred when healthy.

- A provider with **no API key**, an **invalid key**, or an **unsupported
  model** is simply skipped — it never produces a user-visible error.
- A provider that lacks **tool calling** is skipped when the request
  requires native tools.
- A provider-level **circuit breaker** (quarantine) isolates providers
  that fail repeatedly, and each request records a **failure matrix**
  (`provider_matrix`) showing why every candidate was skipped, failed,
  or succeeded — without exposing secrets.
- Provider errors are **normalized** into internal categories
  (`auth`, `rate_limited`, `model_not_found`, `timeout`, `network`,
  `server`, `request`, `tool_call`, `structured_output`,
  `empty_response`, `malformed`, `unknown`) — provider-specific strings
  never leak past the router or into Telegram.
- A "successful" response with **no usable output** (empty text AND no
  tool call, or a response whose tool calls are all malformed) is a
  request-level failover trigger: when another eligible provider is
  healthy, the SAME request is resent there. The empty provider is NOT
  cooled down (a stalled model is a quality signal, not a health
  failure) — a single-provider setup keeps the dispatcher's bounded
  nudge-retry as its recovery path. Failover never re-executes anything:
  no tool has run when the router decides.

### Execution Boundary

AI providers only parse intent. They never touch Telegram, Supabase, or
the runtime. The pipeline is:

```
Natural-language Telegram command
  → AI trigger / handler
  → provider abstraction
  → healthy AI provider (native tool call)
  → normalized execution intent (tool name + arguments)
  → LOCAL validation (registry + argument validation)
  → existing LifeOS executor (service layer)
  → Telegram / database
  → real result
```

A provider that returns an unknown tool name is rejected locally — an LLM
can never invent an action that reaches Telegram.

### Available Providers

Core (genuinely recurring free access, subject to live account/geo check):

| Provider | Model/route | Env var | Class | Role |
|---|---|---|---|---|
| Google Gemini | `gemini-2.5-flash` (configurable) | `GEMINI_API_KEY` | A | Primary |
| Mistral AI | `mistral-small-latest` (configurable) | `MISTRAL_API_KEY` | A/B | Fallback 1 |
| Z.ai / GLM | `glm-4.5-flash` (configurable) | `ZAI_API_KEY` | A/B | Fallback 2 |
| Groq | `qwen-2.5-32b` (configurable) | `GROQ_API_KEY` | A/B | Fallback 3 |

Backup / evaluation:

| Provider | Model/route | Env var | Class | Role |
|---|---|---|---|---|
| SambaNova Cloud | `DeepSeek-V3-0324` | `SAMBANOVA_API_KEY` | B | Emergency backup |
| NVIDIA NIM | `qwen/qwen2.5-72b-instruct` | `NVIDIA_API_KEY` | B | Backup/eval |
| Cohere | `command-r-plus` | `COHERE_API_KEY` | B/C | Evaluation |
| SiliconFlow | `Qwen/Qwen2.5-72B-Instruct` | `SILICONFLOW_API_KEY` | B/C | Backup/eval |
| Cerebras | `llama-3.3-70b` | `CEREBRAS_API_KEY` | B | Backup/eval |
| Fireworks AI | `accounts/fireworks/models/llama-v3p3-70b-instruct` | `FIREWORKS_API_KEY` | C | Only if recurring access |

`openai` and `openrouter` remain registered for compatibility, but are
**not** part of the recommended core order (see below).

Free-tier classification:
- **A** = genuinely recurring free access
- **B** = free but heavily restricted (low daily quota / concurrency)
- **C** = promotional/temporary credits
- **D** = requires payment/card
- **E** = not practically usable

Exact quotas are account- and model-dependent and change over time — the
model names above are configurable defaults, not guarantees. Verify the
current model name against your provider account.

### API Compatibility

- `gemini` uses Google's native `generateContent` API.
- `mistral`, `zai`, `groq`, `sambanova`, `nvidia`, `siliconflow`,
  `fireworks`, `cerebras`, `openai`, and `openrouter` use the
  OpenAI-compatible chat-completions API via `OpenAICompatProvider`.
- `cohere` uses Cohere's OpenAI-compatibility endpoint
  (`https://api.cohere.com/compatibility/v1`).

### Rate Limits, Retry & Fallback

`ProviderManager` owns the provider state machine (circuit breaker).
Providers themselves make a **single attempt** — retries, cooldown,
quarantine, and fallback are centralized:

```
healthy
  │ transient failure (network/timeout/5xx)
  ▼
retry once
  ├── success ──► healthy (failure streak reset)
  └── failure ──► cooling_down (per-category penalty)
cooling_down
  │ cooldown expires → automatically eligible again (no restart)
  ▼
next request (recovery probe)
  ├── success ──► healthy
  └── failure ──► cooling_down (consecutive failures accumulate)
                  └── ≥ 5 consecutive failures ──► quarantined (10 min)
```

- **429 / rate-limit** — reads `Retry-After`, marks the provider
  `cooling_down`, and immediately tries the next healthy provider. It is
  **never** retried in a loop.
- **5xx / network / timeout** — exactly **one** immediate retry, then
  fallback.
- **401 / 403 (auth)** — the provider is marked `disabled` until
  configuration changes; no wasted requests.
- **Different failures carry different penalties**: timeout ≈ 30 s,
  server/network ≈ 60 s, malformed/quality ≈ 120 s.
- **Quarantine** (circuit opens) after 5 consecutive failures; the
  provider re-enters the pool automatically after the quarantine window
  and is trusted again after a success.
- **Semantic uncertainty** ("I don't understand") is a successful
  clarification response, **not** a failure — the system never fails over
  to another provider to manufacture destructive intent.
- **Response quality** is scored separately from HTTP success: a `200`
  with no text and no tool call — or with only malformed tool calls —
  while tools were requested lowers the provider's reliability score
  (the same `action=none` signal seen downstream) without cooling it
  down on a single occurrence.
- **Model granularity**: a `model_not_found`/retired-model failure marks
  exactly that `(provider, model)` pair unavailable (TTL-based), never
  the whole provider — other models on the same provider stay eligible.
- **Failover is deterministic and safe**: the active provider is always
  tried first while healthy; fallback resends the IDENTICAL message list
  (Persian intent, chat/reply context, tools), and every provider
  response still passes the local action validator + ToolExecutor
  allowlist — a provider can never bypass validation or execute an
  unregistered tool.

Cooldown uses `time.monotonic()`. Concurrency is bounded per provider
(default 4; Z.ai is capped at 2). Provider HTTP requests are individually
bounded via the existing watchdog (`guarded_await`).

The router emits deterministic logs — `AI_PROVIDER_ATTEMPT`
(provider, model, attempt), `AI_PROVIDER_FAILURE` (normalized category),
`AI_PROVIDER_FAILOVER` (from provider/model to provider/model),
`ROUTER_SCORE`, `ROUTER_SELECTED`, `PROVIDER_SKIPPED`,
`PROVIDER_COOLDOWN`, `PROVIDER_QUARANTINED`, `PROVIDER_RECOVERED` — so a
routing decision can be diagnosed end-to-end (request → attempt →
category → failover → outcome) without logging credentials or secrets.

### Configuration

Providers are configured via environment variables (both `AI_*_API_KEY`
and bare `*_API_KEY` forms are accepted). See
[Environment Variables](#environment-variables) below.

Minimal setup (no real keys — fill in your own):

```env
GEMINI_API_KEY=...
MISTRAL_API_KEY=...
ZAI_API_KEY=...
GROQ_API_KEY=...
SAMBANOVA_API_KEY=...
AI_PROVIDER_FALLBACK=gemini,mistral,zai,groq,sambanova
```

OpenRouter is intentionally **not** part of the core order — the goal is
independent provider quota pools, not a single router aggregating them.

### Adding a New Provider

1. Create `backend/ai/providers/<name>.py` with a class inheriting from
   `OpenAICompatProvider` (or `BaseProvider` for non-OpenAI-compatible).
2. Set `PROVIDER_NAME` and `PROVIDER_VERSION`.
3. Add defaults to `base/defaults.py`.
4. Add the class to `_PROVIDER_CLASSES` and env maps in `factory.py`.
5. Add env loading in `config/env.py` and metadata in `discovery.py`.
6. Import and export it in `providers/__init__.py`.

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

Events are visible via the Diagnostics / Health Dashboard panels. The
tracer never blocks — it writes to an in-memory list.

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
| `USERNAME_UPDATE_ENABLED` | `false` | Auto-start username cron on boot |
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

Both `AI_*_API_KEY` and bare `*_API_KEY` forms are accepted. A provider
without a key is simply unavailable — it never breaks startup.

| Variable | Description |
|---|---|
| `GEMINI_API_KEY` / `AI_GEMINI_API_KEY` | Google Gemini API key |
| `MISTRAL_API_KEY` / `AI_MISTRAL_API_KEY` | Mistral AI API key |
| `ZAI_API_KEY` / `AI_ZAI_API_KEY` | Z.ai / GLM API key |
| `GROQ_API_KEY` / `AI_GROQ_API_KEY` | Groq API key |
| `SAMBANOVA_API_KEY` / `AI_SAMBANOVA_API_KEY` | SambaNova Cloud API key |
| `NVIDIA_API_KEY` / `AI_NVIDIA_API_KEY` | NVIDIA NIM API key |
| `COHERE_API_KEY` / `AI_COHERE_API_KEY` | Cohere API key |
| `SILICONFLOW_API_KEY` / `AI_SILICONFLOW_API_KEY` | SiliconFlow API key |
| `CEREBRAS_API_KEY` / `AI_CEREBRAS_API_KEY` | Cerebras API key |
| `FIREWORKS_API_KEY` / `AI_FIREWORKS_API_KEY` | Fireworks AI API key |
| `OPENAI_API_KEY` / `AI_OPENAI_API_KEY` | OpenAI API key (optional compat) |
| `OPENROUTER_API_KEY` / `AI_OPENROUTER_API_KEY` | OpenRouter API key (optional compat) |

### Optional — AI Provider Model Overrides

| Variable | Description |
|---|---|
| `GEMINI_MODEL` / `AI_GEMINI_MODEL` | Gemini model name |
| `MISTRAL_MODEL` / `AI_MISTRAL_MODEL` | Mistral model name |
| `ZAI_MODEL` / `AI_ZAI_MODEL` | Z.ai / GLM model name |
| `GROQ_MODEL` / `AI_GROQ_MODEL` | Groq model name |
| `SAMBANOVA_MODEL` / `AI_SAMBANOVA_MODEL` | SambaNova model name |
| `NVIDIA_MODEL` / `AI_NVIDIA_MODEL` | NVIDIA model name |
| `COHERE_MODEL` / `AI_COHERE_MODEL` | Cohere model name |
| `SILICONFLOW_MODEL` / `AI_SILICONFLOW_MODEL` | SiliconFlow model name |
| `CEREBRAS_MODEL` / `AI_CEREBRAS_MODEL` | Cerebras model name |
| `FIREWORKS_MODEL` / `AI_FIREWORKS_MODEL` | Fireworks model name |
| `OPENAI_MODEL` / `AI_OPENAI_MODEL` | OpenAI model name |
| `OPENROUTER_MODEL` / `AI_OPENROUTER_MODEL` | OpenRouter model name |

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

- **Deep Save only** — download + re-upload as a NEW Saved Messages
  message (there is no Forward Save)
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

In-memory event log, diagnostic snapshot, asyncio task diagnostics, and
runtime heartbeat (available from the Health/Dashboard panels).

### AI Assistant

Trigger-word-based activation with full conversation context, memory,
and tool execution. Configure triggers in the AI Settings panel or
the web dashboard. See [How AI Works](#how-ai-works) above.

---

## Commands

`.menu` is the **only** dot-prefixed text command. It opens the Glass UI
mother panel. Every other feature is accessed through the Glass UI
(inline buttons) or through the AI assistant (address it by name, e.g.
`Nova`).

### Glass UI (`.menu`)

| Panel | Access |
|---|---|
| Save (Deep Save) | `.menu` → **📥 Save** |
| Retrieve | `.menu` → **📥 Save** → **🔍 Retrieve** |
| Delete | `.menu` → **🗑 Delete** |
| List / Find | `.menu` → **📋 List** / **🔍 Find** |
| Database | `.menu` → **🗄 Database** |
| AI | `.menu` → **🧠 AI** |
| Bio | `.menu` → **Profile** → **🧬 Bio** |
| Username | `.menu` → **Profile** → **👤 Username** |
| Settings / Health / Context | `.menu` → respective panels |

### AI (Nova)

Activate the AI by starting a message with the trigger word (default
`Nova`), or by replying to a known AI message with plain text.

```
Nova, save this
Nova, deep save this
Nova, delete the last 10 messages
Nova, what are my saved items?
```

These are **execution requests**, not just chat. Nova resolves the
intent to a tool call, runs the existing execution function, and
reports the **real** result.

### Removed legacy dot commands

The following text commands no longer exist and are not hidden aliases:
`.ping`, `.id`, `.help`, `.health`, `.kill`, `.logs`, `.save`, `.save f`,
`.save d`, `.del`, `.retrieve`, `.preview`, `.send`, `.list`, `.find`,
`.db`, `.bio`, `.username`, `.ai`.

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

- Check that the engine is active (`.menu` → **Profile** → **Bio/Username** → **Show State**).
- Check that the template contains at least one token.
- Check the shared Profile Scheduler is running (visible in the Health Dashboard panel).
- Check for `FloodWaitError` in logs.

### Database errors

- The bot works without Supabase — all operations fall back to in-memory.
- Check `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` are correct.
- Check that all migrations have been applied (see `sql/` directory).

### Client keeps disconnecting

- The watchdog automatically detects disconnections and rebuilds.
- Check the Health Dashboard panel for restart count and last rebuild reason.
- Check the Diagnostics panel for a full snapshot.

### AI not responding

- Check that a provider API key is configured (e.g. `AI_OPENAI_API_KEY`).
- The default English trigger word is `Nova` — send `Nova <request>` to
  activate the AI. Change it in the AI Settings panel.
- Without an API key, the DummyProvider returns a placeholder.
- Check the Health Dashboard panel for the AI engine status.

---

## License

This project is for personal use. See the repository for details.
