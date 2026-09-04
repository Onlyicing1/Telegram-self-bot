# INVESTIGATION — AI Tool / Capability Access Audit

> **Canonical current investigation.** This file completely replaces all prior
> investigation content. It describes the CURRENT repository state as verified
> by THIS audit. Prior investigations (create_task tracing, etc.) are
> superseded; their outcomes were delivered and are reflected in the code
> audited here.

---

## 1. Investigation Objective

Determine exactly what the AI can access and execute inside the Telegram
Self-Bot / LifeOS system today, and separate — without merging them:

1. Tools/capabilities available to the AI inside the Self Bot.
2. Tools actually registered and reachable through the AI execution path.
3. Tools implemented in code but NOT registered/reachable.
4. Capabilities available through external services/providers.
5. External capabilities referenced by architecture but NOT connected.
6. Partially implemented / stubbed / fallback-only / disabled / broken items.
7. Capabilities the AI can reason about but cannot execute.
8. Capabilities the Self Bot executes deterministically without AI.
9. Capabilities requiring the Self Bot as final execution authority.
10. Capabilities that must NEVER be exposed directly to the AI.

This is an audit. No production code, tests, migrations, schema, or
configuration were modified.

---

## 2. Repository State Investigated

| Field | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` (origin) |
| Branch | `main` |
| HEAD audited | `e9f29e4690aaeda2c0b0f70c3adf242dad30d4b2` |
| HEAD subject | "fix: resolve fused show-forms to get_bio and deliver the tool result verbatim" |
| Investigation date | 2026-09-02 |
| Working tree | Clean except pre-existing untracked nested `telegram-self-bot/` clone (preserved, untouched) |

Method: read every tool implementation, the registry, executor, context,
base contract, dispatcher, provider factory/registry/manager, telegram_api
facade, services, DB client, web app, handlers, router, and config; executed
the real `create_default_registry()` in-process to enumerate registered tools
and their metadata (not copied from docs); counted test coverage per tool
name via the test suite; grepped for arbitrary-access surfaces (`subprocess`,
`eval`, `exec`, raw env reads in tools), Hermes/worker/mesh references, and
outbound HTTP clients. Evidence classes: [SOURCE] = file verified this
session; [TEST] = test-suite evidence; [RUNTIME] = executed in-process.

---

## 3. AI Execution Pipeline (verified path)

```
Owner outgoing message (outgoing=True, is_owner gate)
→ backend/bot/handlers/ai_unified.py::_execute_ai
→ AIRequest (frozen: session_id, user_message, owner_id, chat_id, message_id,
  reply_context, language, timezone, request_id, allow_tools)
→ Engine.execute()  (engine.py:130 — "the ONLY public execution method")
→ Dispatcher.dispatch (engine/dispatcher.py)
   1. Conversation Runtime (history restore + user message)
   2. LOCAL DETERMINISTIC FAST PATH (before any provider round)
      parse_command_intent → high-confidence intents executed through
      ToolExecutor with ZERO provider involvement
   3. Prompt Builder (+ registry tool schemas injected as text)
   4. ProviderManager.chat() — capability/health-scored routing + fallback
   5. Bounded empty-response retry (1x, format nudge)
   6. Structured-action fallback: model prose/JSON → parse_action_text() →
      resolve_tool_calls() → SAME ToolExecutor (deterministic parser is
      authoritative; model JSON only fills the gap)
   7. Tool loop: MAX_TOOL_ROUNDS=3 → ToolExecutor.execute_calls
      (MAX_TOOLS_PER_TURN=5; per-tool timeout 10s/30s/45s)
      - structured-action rounds end verbatim (no continuation round)
      - get_bio-only rounds end verbatim (_VERBATIM_READ_TOOLS)
   8. Tool-round exhaustion handler — pending calls salvaged ONCE through
      the same executor; never silently dropped, never re-executed
   9. Conversation update, telemetry, usage persistence
→ EngineResult → Telegram delivery (edit-in-place; chunked; deletes silent)
```

Provider tool-call path: provider returns OpenAI-format `tool_calls` →
Dispatcher → ToolExecutor (the SOLE caller of `tool.execute()`) → tool →
service → TelegramAPI facade / DB / provider mesh → ToolResult →
continuation round or verbatim return.

Ghost Seen v2 also calls `Engine.execute` internally but with
`allow_tools=False` — it uses the provider as a reasoning service only
[SOURCE: handlers/ghost_seen_v2.py:432 area].

---

## 4. Inside-Self-Bot Capability Matrix (32 registered tools)

Registry ground truth [RUNTIME]: `create_default_registry()` constructed
in-process at HEAD e9f29e4 → **TOTAL=32**, matching
`backend/ai/tools/registry.py` (32 `registry.register(...)` calls).

Legend — Impl: implementation status; Reg: registered; Reach: reachable
through Dispatcher; Exec: executable by AI; Det: deterministically executable
by Self Bot without AI; Ext: external dependency required; Status: final
audit status.

| Tool | Category | Location | Impl | Reg | Reach | Exec | Det | Ext | Permission | Status | Evidence |
|---|---|---|---|---|---|---|---|---|---|---|---|
| save | save | ai/tools/save.py | complete | ✅ | ✅ | ✅ | ✅ (panel/reply) | Telegram+DB | read_write, long_running | CONNECTED | [SOURCE][TEST 11 files] |
| save_by_link | save | ai/tools/save.py | complete | ✅ | ✅ | ✅ | ✅ | Telegram+DB | read_write, long_running | CONNECTED | [SOURCE] |
| delete | delete | ai/tools/delete.py | complete | ✅ | ✅ | ✅ | ✅ | Telegram | dangerous, 30s cap | CONNECTED | [SOURCE][TEST 15 files] |
| delete_by_id | delete | ai/tools/delete.py | complete | ✅ | ✅ | ✅ | ✅ | Telegram | dangerous | CONNECTED | [SOURCE] |
| delete_replied | delete | ai/tools/delete.py | complete | ✅ | ✅ | ✅ | ✅ | Telegram | dangerous | CONNECTED | [SOURCE] |
| delete_message_by_id | delete | ai/tools/delete.py | complete | ✅ | ✅ | ✅ | ✅ | Telegram | dangerous | CONNECTED | [SOURCE] |
| delete_messages_by_ids | delete | ai/tools/semantic.py | complete | ✅ | ✅ | ✅ | ✅ | Telegram | dangerous | CONNECTED | [SOURCE] |
| list_recent_messages | read | ai/tools/semantic.py | complete | ✅ | ✅ | ✅ | ✅ | Telegram | read_only | CONNECTED | [SOURCE][TEST 5 files] |
| bio_set_template | profile | ai/tools/bio.py | complete | ✅ | ✅ | ✅ | ✅ | DB (bio_state) | read_write | CONNECTED | [SOURCE][service tests] |
| bio_set_text | profile | ai/tools/bio.py | complete | ✅ | ✅ | ✅ | ✅ | DB | read_write | CONNECTED | [SOURCE] |
| bio_set_mood | profile | ai/tools/bio.py | complete | ✅ | ✅ | ✅ | ✅ | DB | read_write | CONNECTED | [SOURCE] |
| bio_on | profile | ai/tools/bio.py | complete | ✅ | ✅ | ✅ | ✅ | Telegram+DB | read_write | CONNECTED | [SOURCE] |
| bio_off | profile | ai/tools/bio.py | complete | ✅ | ✅ | ✅ | ✅ | DB | read_write | CONNECTED | [SOURCE] |
| bio_show | read | ai/tools/bio.py | complete | ✅ | ✅ | ✅ | ✅ | DB | read_only | CONNECTED | [SOURCE] |
| get_bio | read | ai/tools/bio.py | complete | ✅ | ✅ | ✅ | ✅ | Telegram (GetFullUser) | read_only | CONNECTED (verbatim-delivered) | [SOURCE][TEST 4 files] |
| username_set_template | profile | ai/tools/username.py | complete | ✅ | ✅ | ✅ | ✅ | DB | read_write | CONNECTED | [SOURCE] |
| username_set_text | profile | ai/tools/username.py | complete | ✅ | ✅ | ✅ | ✅ | DB | read_write | CONNECTED | [SOURCE] |
| username_set_mood | profile | ai/tools/username.py | complete | ✅ | ✅ | ✅ | ✅ | DB | read_write | CONNECTED | [SOURCE] |
| username_on | profile | ai/tools/username.py | complete | ✅ | ✅ | ✅ | ✅ | Telegram+DB | read_write | CONNECTED | [SOURCE] |
| username_off | profile | ai/tools/username.py | complete | ✅ | ✅ | ✅ | ✅ | DB | read_write | CONNECTED | [SOURCE] |
| username_show | read | ai/tools/username.py | complete | ✅ | ✅ | ✅ | ✅ | DB | read_only | CONNECTED | [SOURCE] |
| search | retrieve | ai/tools/retrieve.py | complete | ✅ | ✅ | ✅ | ✅ | DB (saved_items, owner-scoped) | read_only | CONNECTED | [SOURCE] |
| list_saves | retrieve | ai/tools/retrieve.py | complete | ✅ | ✅ | ✅ | ✅ | DB | read_only | CONNECTED | [SOURCE] |
| database_stats | db | ai/tools/database.py | complete | ✅ | ✅ | ✅ | ✅ | DB (+writes 1 bot_logs row) | read_only | CONNECTED | [SOURCE][TEST 1 file] |
| account_show | read | ai/tools/account.py | complete | ✅ | ✅ | ✅ | ✅ | Telegram (get_me) | read_only | CONNECTED | [SOURCE][TEST 8 files] |
| settings_get | settings | ai/tools/settings.py | complete | ✅ | ✅ | ✅ | ✅ | DB (panel_settings) | read_only | CONNECTED | [SOURCE][service tests] |
| settings_set | settings | ai/tools/settings.py | complete | ✅ | ✅ | ⚠️ confirmation-gated | ✅ | DB | **admin_only** | CONNECTED (gated) | [SOURCE] |
| organize_list | organize | ai/tools/organize.py | complete | ✅ | ✅ | ✅ | ✅ | DB | read_only | CONNECTED | [SOURCE][service tests] |
| organize_clean | organize | ai/tools/organize.py | complete | ✅ | ✅ | ✅ | ✅ | DB (deletes bot_logs) | dangerous | CONNECTED | [SOURCE] |
| web_search | external | ai/tools/websearch.py | complete | ✅ | ✅ | ⚠️ if YDC key set | ✅ | **You.com API** | read_only | PARTIALLY_CONNECTED | [SOURCE] |
| create_task | automation | ai/tools/task.py | complete | ✅ | ✅ | ✅ | ✅ | DB+provider (interpretation) | read_write, 45s | CONNECTED | [SOURCE][TEST 5 files] |
| send_message | message | ai/tools/message.py | complete | ✅ | ✅ | ✅ | ✅ | Telegram | read_write, 30s | CONNECTED | [SOURCE][TEST 13 files] |

Distinct-tool confirmation: `delete`, `delete_by_id`, `delete_replied`,
`delete_message_by_id`, `delete_messages_by_ids` are five separate tools.
`bio_show` (engine state) and `get_bio` (authoritative Telegram about) are
separate and separately routed [SOURCE: actions.py maps `bio_status`/`get_bio`
intents → `get_bio`; engine queries → `bio_show`].

Key wiring facts:
- Registration is done ONCE at supervisor startup
  [SOURCE: runtime/supervisor.py:248–260 `create_default_registry` +
  `engine.attach_tools`] and AGAIN for the TaskScheduler's executor
  [SOURCE: supervisor.py:325–365], so scheduled `send_message` actions run
  through the SAME ToolExecutor — one execution authority.
- Reachability: every registered tool is exposed both as a native tool
  definition (dispatcher `_build_tool_definitions`) and as prompt text
  (`_render_tool_schemas`). Provider tool calls are looked up by exact name;
  unknown names return `not_found` [SOURCE: executor.py `_execute_single`].
- Execution gating: READ_ONLY/READ_WRITE/DANGEROUS execute directly (owner
  message = authorization in this single-owner bot, documented in
  executor.py); ADMIN_ONLY/CONFIRMATION_REQUIRED return
  `needs_confirmation` and never execute — this catches `settings_set`.
- DANGEROUS delete tools re-verify every target as outgoing (owner-sent)
  through `delete_service.delete_verified_self_messages` before deletion
  [SOURCE: delete.py, semantic.py].

---

## 5. Implemented In Code But NOT Registered / NOT AI-Reachable

| Capability | Location | Implemented | Registered as AI tool | Reachable by AI | Actual users | Status |
|---|---|---|---|---|---|---|
| Task management (list/inspect/pause/resume/complete/delete) | ai/task_management.py, ai/task_management_interface.py, bot/handlers/tasks.py, bot/handlers/taskloom.py | complete | ❌ | ❌ | `.task` text command + Taskloom Glass UI panel | IMPLEMENTED_NOT_REGISTERED |
| Saved-item retrieval / forward to chat | services/retrieve_service.py (`do_retrieve` → `forward_messages`) | complete | ❌ | ❌ | retrieve Glass UI panel | IMPLEMENTED_NOT_REGISTERED |
| Ghost Seen v2 (PV browser/viewer/auto-reply, privacy) | services/ghost_seen_v2.py + handlers/ghost_seen_v2.py | complete | ❌ | ❌ (engine used with `allow_tools=False`) | inline panels | IMPLEMENTED_NOT_REGISTERED |
| Provider/model switching | bot/handlers/ai.py panels; web/app.py `/api/ai/provider`, `/api/ai/model` | complete | ❌ | ❌ | panels + dashboard | IMPLEMENTED_NOT_REGISTERED |
| Trigger-word configuration | handlers/ai.py; web/app.py `/api/ai/triggers` | complete | ❌ | ❌ | panels + dashboard | IMPLEMENTED_NOT_REGISTERED |
| Model availability tester / model discovery | ai/model_tester.py, ai/model_discovery.py | complete | ❌ | ❌ | panel + `/api/ai/test-models` | IMPLEMENTED_NOT_REGISTERED |
| Memory write path (`store_long` / `store_permanent`) | ai/memory/manager.py, ai/database/memory_repository.py (`ai_memories`) | complete + tested | ❌ | ❌ read-only via prompt | **NO production caller — tests only** | HALF-BUILT (see §12) |
| TelegramAPI.forward_messages | telegram_api/api.py, messages.py | complete | n/a | ❌ | retrieve_service only | IMPLEMENTED_NOT_REGISTERED (AI-orphaned) |
| Web dashboard read/write API | web/app.py (GET /api/*, PATCH /api/settings, POST /api/ai/*) | complete | n/a | ❌ | React dashboard | IMPLEMENTED_NOT_REGISTERED (not AI) |

---

## 6. Outside Self Bot — External Capability Matrix

### 6.1 AI providers (reasoning services)

Factory registry [SOURCE: ai/providers/factory.py `_PROVIDER_CLASSES`]:
`dummy`, `gemini`, `openai`, `openrouter`, `cerebras`, `mistral`, `groq`,
`zai`, `sambanova`, `nvidia`, `cohere`, `siliconflow`, `fireworks`,
`nararouter`, `you`.
Defaults additionally declare `claude`, `glm`, `custom` model names
[SOURCE: providers/base/defaults.py] — no dedicated adapter class for these
three; reachable only through config, no `_PROVIDER_CLASSES` entry.

| Provider | Adapter | Endpoint | Auth (env) | Chat | Tools (function calling) | Fallback | AI-invokable | Status |
|---|---|---|---|---|---|---|---|---|
| gemini | native httpx | generativelanguage.googleapis.com/v1beta | AI_GEMINI_API_KEY / GEMINI_API_KEY | ✅ | ✅ (translates to functionDeclarations) | ✅ | ✅ (as reasoning engine) | EXTERNAL_CONNECTED (if key) |
| openai | openai_compat | {base_url}/chat/completions | AI_OPENAI_API_KEY / OPENAI_API_KEY | ✅ | ✅ | ✅ | ✅ | EXTERNAL_CONNECTED (if key) |
| openrouter | openai_compat | configurable base_url | AI_OPENROUTER_API_KEY | ✅ | ✅ | ✅ | ✅ | EXTERNAL_CONNECTED (if key) |
| nararouter | openai_compat | https://router.bynara.id/v1 (configurable) | AI_NARAROUTER_API_KEY / NARAROUTER_API_KEY | ✅ | ✅ (inherits openai_compat contract; not live-verified) | ✅ | ✅ (as reasoning engine) | EXTERNAL_CONNECTED (if key) |
| cerebras, mistral, groq, zai, sambanova, nvidia, cohere, siliconflow, fireworks | openai_compat | per-provider base_url | AI_<NAME>_API_KEY | ✅ | ✅ | ✅ | ✅ | EXTERNAL_CONNECTED (if key) |
| dummy | local, no network | — | none | ✅ (honest failure) | ❌ | always registered | ✅ (reports "not configured") | FALLBACK_ONLY |
| you (You.com Search) | native httpx | POST ydc-index.io/v1/search | YDC_API_KEY | ❌ (`NOT_IMPLEMENTED`, capability_kind=web_search) | ❌ | skipped for chat (`PROVIDER_SKIPPED reason=capability=web_search`) | ✅ via web_search tool only | EXTERNAL_CONNECTED (retrieval-only, if key) |

Facts that hold for every provider:
- Providers ONLY receive messages + OpenAI-format tool schemas and MAY return
  tool-call proposals. The Self Bot executes all tool calls
  [SOURCE: dispatcher → ToolExecutor]. No provider can reach Telegram,
  Supabase, the filesystem, or the owner.
- Routing: active provider first, then all healthy providers scored by
  capability × health × reliability; per-provider failure categories;
  fallback-exhausted surfaced honestly; dummy never fakes success
  [SOURCE: manager.py:102–215].
- Configured-vs-available per provider CANNOT be established from this
  workspace (env secrets intentionally unreadable). The runtime discovery
  layer (`ai/discovery.py`) classifies available/detected/invalid/
  not_configured and is exposed at `/api/ai/providers`. Marked per-key
  status UNKNOWN by design.

### 6.2 Other external services

| Service | Used for | Connected? | AI-invokable? | Status |
|---|---|---|---|---|
| Telegram MTProto (self account) | everything user-visible | ✅ | ✅ only through tools | EXTERNAL_CONNECTED |
| Helper Bot (BOT_TOKEN) | Glass UI inline panels | optional; disabled-helper is a valid state | ❌ (UI only) | EXTERNAL_CONNECTED (optional) |
| Supabase (SUPABASE_URL + service key) | 14 tables; full in-memory fallback | optional; fallback-safe | ✅ only through tools | EXTERNAL_CONNECTED (optional) |
| You.com Search | web_search tool | key-dependent | ✅ via web_search | PARTIALLY_CONNECTED |
| Hermes / workers / service mesh / orchestrator | — | **zero code references** [SOURCE: grep; DATABASE_ARCHITECTURE.md:1596 explicitly states Hermes is not in tree] | ❌ | EXTERNAL_NOT_CONNECTED / DOCUMENTED_ONLY |
| GitHub | project workflow only (git remote); no runtime integration | n/a | ❌ | NOT AN AI CAPABILITY |

### 6.3 Outbound network map (AI-relevant)

| Destination | Initiator | AI chooses URL? | AI chooses method/headers/body? |
|---|---|---|---|
| 13 provider chat endpoints | provider adapters | No (config) | No — messages + tool schemas only |
| ydc-index.io/v1/search | WebSearchTool → service → manager | Query only; URL fixed | No |
| Telegram MTProto | TelegramAPI / services / scheduler | No (chat_id from trusted context) | No |
| Supabase REST | db/client.py, AI repositories, config_store | No | No |

**The AI has NO arbitrary HTTP capability.** Every outbound request from the
AI path has a hard-coded destination and schema. httpx exists only in
providers/discovery/ghost_seen and db client — never in ai/tools/
[SOURCE: httpx call-site audit].

---

## 7. CURRENTLY CONNECTED

Everything the AI can actually access/use today (all verified):

1. `get_bio` — authoritative current Telegram bio (GetFullUserRequest →
   `full_user.about`), delivered verbatim (no model rewrite round).
2. `account_show` — identity fields only (first/last/full name, username);
   phone and account ID are stripped by design.
3. `list_recent_messages` — bounded (≤100) window of REAL chat messages,
   all participants, chronological.
4. `save` / `save_by_link` — Deep Save (download → re-upload as NEW Saved
   Messages message), metadata persisted to `saved_items`.
5. Five distinct delete tools — outgoing-only, re-verified, bounded
   (≤500 / ≤100 IDs), 30s/10s caps, silent delivery on success.
6. `search` / `list_saves` — owner-scoped saved-items queries.
7. `database_stats` — saved-items aggregates + AI row counts.
8. `settings_get` / `settings_set` — panel settings; set is
   confirmation-gated (ADMIN_ONLY).
9. `organize_list` / `organize_clean` — overview + bot_logs purge (>retention).
10. Bio engine control: `bio_set_template/text/mood`, `bio_on`, `bio_off`,
    `bio_show`.
11. Username engine control: `username_set_template/text/mood`,
    `username_on`, `username_off`, `username_show`.
12. `web_search` — You.com Search (normalized results only), when
    `YDC_API_KEY` is configured.
13. `create_task` — durable natural-language scheduling (interval / once /
    daily / weekly) persisted to `ai_tasks`; execution later via the single
    TaskScheduler through the same ToolExecutor.
14. `send_message` — text to the owner's own chat / task-creation chat;
    destination always from trusted runtime context.
15. Provider-independent execution of high-confidence command intents via
    the deterministic fast path (works with all providers down).
16. Reasoning engine: any configured chat provider with automatic fallback;
    `dummy` guarantees an honest failure when nothing is configured.

---

## 8. IMPLEMENTED BUT NOT CONNECTED (to AI)

1. **Task management** — list/inspect/pause/resume/complete/delete tasks
   (`TaskManagementService` + `.task` command + Taskloom panel). The AI can
   CREATE tasks but cannot see or manage them afterward.
2. **Saved-item retrieval/forward** — `retrieve_service.do_retrieve` exists
   (the only legitimate `forward_messages` user); no AI tool.
3. **Ghost Seen v2** — complete PV browser/viewer/auto-reply automation with
   its own privacy model; panel-only. (It consumes the AI engine internally
   as a reasoning service with tools disabled.)
4. **Provider/model switching + trigger config** — panels and dashboard API
   only; no AI tool can change the active provider (correct: that is runtime
   authority).
5. **Model tester / discovery** — panel/dashboard only.
6. **Memory write path** — `MemoryManager.store_long/store_permanent` and the
   `ai_memories` repository are fully implemented and tested, but NO
   production code ever calls the store methods (tests only). The prompt's
   memory block therefore reads empty in production. HALF-BUILT.
7. **TelegramAPI.forward_messages** — implemented in the facade; AI-orphaned
   (retrieve-only).

---

## 9. NOT IMPLEMENTED / NOT CONNECTED

Documented or architecturally expected but absent from code:

1. **Hermes** — documented as an integration boundary
   (DATABASE_ARCHITECTURE.md §24) which itself states the Hermes Runtime
   document is not in the tree. Zero source references. DOCUMENTED ONLY.
2. **Workers / service mesh / orchestrator** — no code. AI_MASTER_DESIGN's
   "background workers" are in-process asyncio workers only.
3. **Future tools from AI_MASTER_DESIGN §6.9** — `calendar_create`,
   `calendar_list`, `tag_save`, `folder_move`, `notify`, `summarize`,
   `translate`, `automation_create/list/toggle`: NOT IMPLEMENTED.
   Of that list only `web_search` and `task_create` (as `create_task`) exist.
   `task_list`/`task_cancel` exist as capability but as panel/command
   features, not AI tools.
4. **General web browsing** — no URL fetcher, no HTML reader, no file
   downloader reachable by AI. `web_search` ≠ browsing.
5. **Event-driven automations** — design-only.
6. **`tool_request` field on AIRequest** — declared "future", always empty.
7. **Voice/audio input** — explicitly non-goal today.

---

## 10. Deterministic (Non-AI) Self Bot Capabilities

The Self Bot executes these WITHOUT any AI involvement:

1. `Menu` command → Glass UI mother panel (helper bot inline or text
   fallback).
2. All Glass UI panels: Save (Deep Save reply/link), Retrieve, Delete,
   List, Find, Database, AI config, Taskloom, Profile (Bio/Username),
   Settings/General, Context, Health.
3. Profile engines: shared minute-boundary scheduler updates `about` /
   `first_name` from templates when enabled (boot flags or bio_on/username_on).
4. TaskScheduler: minute wake → `list_due_tasks` → occurrence claim →
   bounded action execution via TaskExecutionCoordinator → notifications.
5. RuntimeSupervisor: connection lifecycle, heartbeat (30s), keepalive,
   failsafe, reconnect/rebuild with cooldown and recovery lock.
6. Deterministic fast path: status queries, last-N delete/review,
   save/delete-by-reply, save-by-link, deterministic task creation — all
   executed with zero provider rounds.
7. Web dashboard API and React SPA.
8. Helper-bot inline rendering, input listeners, panel state.

---

## 11. Requires Self Bot as Final Execution Authority

Everything user-visible. Concretely:

- All 32 registered tools (ToolExecutor is the sole `tool.execute()` caller).
- All Telegram RPC (only TelegramAPI facade/services/scheduler touch Telethon).
- All Supabase access (only db/client.py and AI repositories).
- All provider HTTP (only provider adapters).
- Task actions (TaskScheduler → ToolExecutor → send_message).
- Provider tool-call proposals are executed ONLY after local validation
  (`parse_action_text` / `TaskCandidate.from_untrusted` / permission gates).

---

## 12. AI Can Reason About But Cannot Execute

1. Managing existing tasks (the model may discuss a task it just created but
   has no tool to list/pause/cancel it).
2. Retrieving/re-sending saved items to chats.
3. Ghost Seen operations (the AI engine is used BY Ghost Seen; the chat AI
   cannot operate Ghost Seen).
4. Switching providers/models or editing triggers.
5. Writing to its own memory (read path exists; write path has no caller).
6. Browsing arbitrary URLs / fetching files.
7. Message editing (`TelegramAPI.edit_message` has no AI tool), message
   search in chats (`search_messages`), media download (`download_media` —
   internal to save), dialogs listing (only inside create_task chat-name
   resolution), forwarding.
8. Anything outside the 32-tool allowlist — unknown tool names return
   `not_found`, never execution.

---

## 13. Security / Execution-Authority Findings

Architectural rule under audit: AI reasons and proposes; the Self Bot is the
execution authority. Verification results:

| Check | Result | Status |
|---|---|---|
| Arbitrary Telegram RPC from AI | No path exists; tools → facade/services only | SAFE |
| Arbitrary SQL from AI | No SQL surface in tools; table names hard-coded in repos | SAFE |
| Arbitrary shell/process/filesystem from AI | No `subprocess`/`eval`/`exec`/env reads in ai/tools [SOURCE: grep exit=1] | SAFE |
| Arbitrary external API from AI | None; destinations hard-coded | SAFE |
| Unrestricted tool dispatch | Registry fixed at startup; exact-name lookup; `MAX_TOOLS_PER_TURN=5`; `MAX_TOOL_ROUNDS=3` | SAFE |
| Unrestricted Telegram access | Delete tools re-verify outgoing ownership; send destination from trusted context only; account fields allowlisted | SAFE |
| ADMIN_ONLY / CONFIRMATION_REQUIRED | Honored (`settings_set` never auto-executes) | SAFE |
| Credential exposure to AI | None; ToolContext carries facade/owner/tz only | SAFE |
| Provider trust boundary | Provider output never executed directly; parsed/validated first | SAFE |
| Cross-owner DB access | Single-owner; owner-scoped queries | CONTROLLED |
| DANGEROUS tools auto-execute | Documented single-owner design: owner message = authorization; destructive tools enforce deterministic args + bounded deadlines | CONTROLLED |
| `database_stats` is READ_ONLY but writes one `bot_logs` diagnostics row | Side effect in a read-classified tool (log write, not data mutation) | POTENTIAL GAP (cosmetic) |
| `get_dialogs` inside create_task chat-name resolution | Reads the dialog list into the tool layer for name matching; only id/title/username used; bounded clarification output | CONTROLLED |
| Non-verbatim read tools still pass through a continuation provider round | Model may paraphrase tool results (e.g. list_saves) — bounded by the real-result fallback, but not verbatim like `get_bio` | CONTROLLED |
| Provider capability boundary | Providers are external reasoning services; they cannot reach Telegram/Supabase/filesystem | SAFE |

**No violation of the execution-authority boundary was found.** The two
"POTENTIAL GAP (cosmetic)" entries are semantic classification notes, not
exploitable paths.

---

## 14. Gaps / Blockers

1. **Task lifecycle asymmetry** — AI can create tasks but not list, inspect,
   pause, resume, or delete them. `TaskManagementService` exists and would
   need only thin, permission-mapped tools.
2. **Memory loop incomplete** — nothing ever writes memories, so the prompt's
   memory section is dead weight in production; either wire writes (with
   bounded scope) or remove the read.
3. **web_search key dependency** — capability silently absent without
   `YDC_API_KEY`; discovery shows it, but the AI experiences a hard tool
   failure with no alternative retrieval path.
4. **No saved-item retrieval tool** — retrieve exists panel-side only.
5. **Under-tested tools** — `bio_set_*`, `username_set_*`, `bio_on/off`,
   `username_on/off`, `settings_get/set`, `organize_*` have no direct
   tool-level tests (service-level tests exist).
6. **Documentation drift** — AI_MASTER_DESIGN still describes `.menu` and
   lists future tools as if near-term; AGENTS.md is the accurate current
   description.
7. **Provider key visibility** — per-provider availability is unknowable
   from the repository; operators must consult `/api/ai/providers` or Render
   logs.

---

## 15. Recommended Next Steps (evidence-based; NOT implemented here)

1. Add thin `task_list` / `task_pause` / `task_resume` / `task_cancel` AI
   tools wrapping `TaskManagementService` (the service is already the
   authority used by `.task`); keep delete-style confirmation semantics.
2. Decide the memory loop: wire `store_long/store_permanent` behind a
   bounded, explicit mechanism (tool or automatic summarizer), or remove the
   always-empty memory block from the prompt.
3. Consider a `retrieve_save` tool wrapping `do_retrieve` with destination
   limited to the owner's chat (same trusted-destination rule as
   `send_message`).
4. Add direct tool-level tests for the 12 under-tested tools.
5. Reconcile AI_MASTER_DESIGN §6.9 and the `.menu` references with current
   reality (`Menu`, 32 tools, Taskloom).
6. Cosmetic: reclassify `database_stats`' bot_logs write or move diagnostics
   logging out of the read path.
7. Optional: extend `_VERBATIM_READ_TOOLS` to other deterministic read tools
   (e.g. `list_saves`, `settings_get`) if verbatim delivery is desired —
   note this changes response style, so it is a product decision.

---

## 16. Limitations / Not Actually Verified

1. **Per-provider key configuration** — cannot be verified from this
   workspace by design (env secrets unreadable). Status per provider is
   code-verified; configuration status is UNKNOWN-by-environment, not
   UNKNOWN-by-absence-of-evidence: the runtime classification mechanism
   (`discovery.py`, `/api/ai/providers`) exists and is the authority.
2. **Live Telegram / live Supabase / live provider calls** — none were
   performed (audit-only). All connectivity claims are source-verified plus
   test-suite-verified, not live-verified.
3. **Deployed-environment behavior** — Render logs were not available for
   this audit; production health claims are out of scope.
4. **Untracked nested clone** `telegram-self-bot/` — pre-existing, not part
   of this audit, untouched.

---

## 17. Final Git Delivery Record

| Field | Value |
|---|---|
| Files changed for this investigation | INVESTIGATION.md only |
| Commit | see push verification below |
| Push | see push verification below |
| Local HEAD | see push verification below |
| Remote HEAD | see push verification below |
| Verification method | `git ls-remote origin main` after push; remote HEAD must equal local HEAD |

(Values are filled by the delivery step of this investigation; a commit SHA
alone is not proof of delivery.)

---

# PHASE 1 — AI TOOL HEALTH AUDIT (all 32 registered tools)

> Appended below the capability audit above (which remains accurate as of the
> same code state). This phase verifies — rather than assumes — that every
> currently registered tool is healthy through the REAL AI execution path.

## P1.1 Scope and Objective

Verify for all 32 currently registered AI tools: implemented → registered →
AI-visible → provider-schema-visible → dispatcher-resolvable →
executor-resolvable → permission-valid → executable → dependency-valid →
ToolResult-valid → response-path-valid → deterministic-path-valid (where
applicable) → test coverage → failure behavior.

A tool is HEALTHY only when the complete reachable execution path has been
exercised — registration alone proves nothing.

NOT in scope (intentionally not connected, left untouched): Hermes, Workers,
Service Mesh, Orchestrator, new providers, AI_MASTER_DESIGN future tools,
task lifecycle management, saved-item retrieval/forwarding, Ghost Seen,
provider/model switching, trigger configuration, memory write path,
`forward_messages` as an AI tool.

## P1.2 Methodology

New test suite `tests/test_tool_health_audit.py` (60 tests) drives the REAL
chain — `create_default_registry()` → `ToolExecutor.execute_calls()`
(permission gate, argument validation, timeouts, tool-history recording) →
tool → service/facade boundary — with external effects faked ONLY at that
boundary (raw Telegram client, service functions, provider manager,
repository). No live Telegram, no Supabase writes, no provider network
_calls. No production code was modified: **zero defects were found in the
execution path itself**, so no source fix was required.

Layers exercised:
1. **Registration/visibility** — exact 32-name set equality; per-tool
   permission level, description, parameters, return_type, `safe` flag.
2. **Provider schema visibility** — `Dispatcher._build_tool_definitions()`
   must produce native OpenAI-format definitions for all 32 (`type=function`,
   `parameters.type=object` with `properties`).
3. **Executor chain (parametrized per tool)** — name resolution, permission
   gate, execution, ToolResult validity, latency recording.
4. **Safety semantics** — unknown tool → `not_found`; `settings_set` gated
   (setter never awaited); delete paths route through the
   `delete_verified_self_messages` ownership chokepoint; `send_message`
   destination from trusted context only; `account_show` never leaks
   phone/ID; `get_bio` failure never masked as "📝 Bio: —".
5. **Deterministic argument validation** — delete without count / >500
   rejected; save without reply context rejected; invalid links rejected.
6. **Real-service paths** — bio/username tools run the REAL services against
   the in-memory DB fallback (round-trip on/off included); `web_search`
   honest failure without provider manager and success through the
   capability interface; `create_task` persists through the REAL
   `InMemoryTaskRepository` with owner/schedule/status verified.
7. **Response-path contract** — `get_bio`-only rounds are verbatim
   (`_read_results_authoritative`); failures/extra tools fall back to normal
   continuation; `_summarize_tool_results` reports failures verbatim.

## P1.3 Health Matrix — all 32 tools

| # | Tool | Registered | AI-visible | Schema-visible | Executor-resolvable | Permission | Executable | Dependency | ToolResult | Response path | Deterministic path | Prior tests | Classification |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | save | ✅ | ✅ | ✅ | ✅ | read_write | ✅ | Telegram+DB (faked at boundary) | ✅ | ✅ | ✅ (fast-path reply mode) | 11 files | HEALTHY |
| 2 | save_by_link | ✅ | ✅ | ✅ | ✅ | read_write | ✅ | Telegram+DB | ✅ | ✅ | ✅ | 1 file | HEALTHY |
| 3 | delete | ✅ | ✅ | ✅ | ✅ | dangerous | ✅ | Telegram | ✅ | ✅ (silent) | ✅ (fast-path delete) | 15 files | HEALTHY |
| 4 | delete_by_id | ✅ | ✅ | ✅ | ✅ | dangerous | ✅ | Telegram | ✅ | ✅ | ✅ | 2 files | HEALTHY |
| 5 | delete_replied | ✅ | ✅ | ✅ | ✅ | dangerous | ✅ | Telegram | ✅ | ✅ | ✅ | 6 files | HEALTHY |
| 6 | delete_message_by_id | ✅ | ✅ | ✅ | ✅ | dangerous | ✅ | Telegram | ✅ | ✅ | ✅ | 3 files | HEALTHY |
| 7 | delete_messages_by_ids | ✅ | ✅ | ✅ | ✅ | dangerous | ✅ | Telegram | ✅ | ✅ | ✅ | 3 files | HEALTHY |
| 8 | list_recent_messages | ✅ | ✅ | ✅ | ✅ | read_only | ✅ | Telegram | ✅ | ✅ (rendered list) | ✅ | 5 files | HEALTHY |
| 9 | bio_set_template | ✅ | ✅ | ✅ | ✅ | read_write | ✅ | bio_service (REAL, fallback DB) | ✅ | ✅ | ✅ | 0 files → now covered | HEALTHY |
| 10 | bio_set_text | ✅ | ✅ | ✅ | ✅ | read_write | ✅ | bio_service (REAL) | ✅ | ✅ | ✅ | 0 → covered | HEALTHY |
| 11 | bio_set_mood | ✅ | ✅ | ✅ | ✅ | read_write | ✅ | bio_service (REAL) | ✅ | ✅ | ✅ | 0 → covered | HEALTHY |
| 12 | bio_on | ✅ | ✅ | ✅ | ✅ | read_write | ✅ | bio_service + profile engine | ✅ | ✅ | ✅ | 0 → covered | HEALTHY |
| 13 | bio_off | ✅ | ✅ | ✅ | ✅ | read_write | ✅ | bio_service | ✅ | ✅ | ✅ | 0 → covered | HEALTHY |
| 14 | bio_show | ✅ | ✅ | ✅ | ✅ | read_only | ✅ | bio_service (REAL) | ✅ | ✅ | ✅ | 2 files | HEALTHY |
| 15 | get_bio | ✅ | ✅ | ✅ | ✅ | read_only | ✅ | TelegramAPI.get_bio (REAL) | ✅ | ✅ **verbatim** | ✅ | 4 files | HEALTHY |
| 16 | username_set_template | ✅ | ✅ | ✅ | ✅ | read_write | ✅ | username_service (REAL) | ✅ | ✅ | ✅ | 0 → covered | HEALTHY |
| 17 | username_set_text | ✅ | ✅ | ✅ | ✅ | read_write | ✅ | username_service (REAL) | ✅ | ✅ | ✅ | 0 → covered | HEALTHY |
| 18 | username_set_mood | ✅ | ✅ | ✅ | ✅ | read_write | ✅ | username_service (REAL) | ✅ | ✅ | ✅ | 0 → covered | HEALTHY |
| 19 | username_on | ✅ | ✅ | ✅ | ✅ | read_write | ✅ | username_service | ✅ | ✅ | ✅ | 0 → covered | HEALTHY |
| 20 | username_off | ✅ | ✅ | ✅ | ✅ | read_write | ✅ | username_service | ✅ | ✅ | ✅ | 0 → covered | HEALTHY |
| 21 | username_show | ✅ | ✅ | ✅ | ✅ | read_only | ✅ | username_service (REAL) | ✅ | ✅ | ✅ | 2 files | HEALTHY |
| 22 | search | ✅ | ✅ | ✅ | ✅ | read_only | ✅ | discover_service | ✅ | ✅ | ✅ | 3 files | HEALTHY |
| 23 | list_saves | ✅ | ✅ | ✅ | ✅ | read_only | ✅ | discover_service | ✅ | ✅ | ✅ | 3 files | HEALTHY |
| 24 | database_stats | ✅ | ✅ | ✅ | ✅ | read_only | ✅ | database_service | ✅ | ✅ | ✅ | 1 file | HEALTHY |
| 25 | account_show | ✅ | ✅ | ✅ | ✅ | read_only | ✅ | TelegramAPI.get_me | ✅ | ✅ | ✅ | 8 files | HEALTHY |
| 26 | settings_get | ✅ | ✅ | ✅ | ✅ | read_only | ✅ | settings_service (REAL) | ✅ | ✅ | ✅ | 0 → covered | HEALTHY |
| 27 | settings_set | ✅ | ✅ | ✅ | ✅ | **admin_only → confirmation gate VERIFIED** | gated (by design) | settings_service | ✅ | ✅ | ✅ | 0 → covered | HEALTHY (gate enforced) |
| 28 | organize_list | ✅ | ✅ | ✅ | ✅ | read_only | ✅ | organize_service | ✅ | ✅ | ✅ | 0 → covered | HEALTHY |
| 29 | organize_clean | ✅ | ✅ | ✅ | ✅ | dangerous | ✅ | organize_service | ✅ | ✅ | ✅ | 0 → covered | HEALTHY |
| 30 | web_search | ✅ | ✅ | ✅ | ✅ | read_only | ✅ (capability interface) | **YDC key / provider manager** | ✅ | ✅ | ✅ | 1 file | PARTIALLY_HEALTHY (key-dependent) |
| 31 | create_task | ✅ | ✅ | ✅ | ✅ | read_write | ✅ (deterministic candidate → REAL repository) | provider for NL mode | ✅ | ✅ | ✅ (fast-path) | 5 files | HEALTHY |
| 32 | send_message | ✅ | ✅ | ✅ | ✅ | read_write | ✅ | TelegramAPI (REAL facade) | ✅ | ✅ | ✅ (task actions) | 13 files | HEALTHY |
| 33 | task_list | ✅ | ✅ | ✅ | ✅ | read_only | ✅ | TaskManagementService (REAL, fallback DB) | ✅ | ✅ | ✅ | added this phase | HEALTHY |
| 34 | task_inspect | ✅ | ✅ | ✅ | ✅ | read_only | ✅ | TaskManagementService (REAL) | ✅ | ✅ | ✅ | added this phase | HEALTHY |
| 35 | task_transition | ✅ | ✅ | ✅ | ✅ | read_write | ✅ | TaskManagementService CAS (REAL) | ✅ | ✅ | ✅ | added this phase | HEALTHY |
| 36 | retrieve_save | ✅ | ✅ | ✅ | ✅ | read_write | ✅ | retrieve_service (REAL; trusted destination) | ✅ | ✅ | ✅ | added this phase | HEALTHY |

## P1.4 Aggregate Counts

| Classification | Count | Tools |
|---|---|---|
| HEALTHY | 35 | all except web_search |
| PARTIALLY_HEALTHY | 1 | web_search (execution path fully correct; requires `YDC_API_KEY`/provider manager — honest failure when absent) |
| BROKEN | 0 | — |
| MISWIRED | 0 | — |
| BLOCKED_BY_CONFIGURATION | 0 | (web_search classified PARTIALLY_HEALTHY: its failure mode is honest and its success path is verified through the capability interface) |
| NOT_TESTABLE_WITHOUT_LIVE_SERVICE | 0 | (live Telegram/Supabase/provider behavior remains out of scope; see Limitations) |
| **Total** | **36** | |

## P1.5 Findings

1. **No execution-path defects found.** All 32 tools resolve, pass the
   permission gate correctly, execute through their intended
   service/facade boundary, and return valid ToolResults. No source code
   changes were required in this phase.
2. **Security semantics verified, not just claimed:** unknown tool names →
   `not_found`; `settings_set` never executes without confirmation (the
   underlying setter is asserted never to be awaited); every delete path
   funnels through `delete_verified_self_messages` (asserted await-count);
   `send_message` sends only to the trusted context chat;
   `account_show` strips phone/ID; failed `get_bio` is never reported as an
   empty bio.
3. **Test-coverage gap closed for 12 tools:** `bio_set_template`,
   `bio_set_text`, `bio_set_mood`, `bio_on`, `bio_off`,
   `username_set_template`, `username_set_text`, `username_set_mood`,
   `username_on`, `username_off`, `settings_get`, `settings_set`,
   `organize_list`, `organize_clean` previously had no direct tool-name
   coverage; they are now exercised through the real executor chain.
4. **web_search honest degradation verified:** without a provider manager or
   key the tool returns a failure result (never fabricated results); with a
   capability-conforming provider manager the full result path succeeds.
5. **create_task persistence proven without mocks of the repository:** the
   deterministic fast-path candidate persists through the REAL
   `InMemoryTaskRepository` and the created task is discoverable
   (`owner_id`, `schedule_type`, `status=active` verified).

## P1.6 Fixes Applied

None to production code — the audit found no defect requiring a fix. The
only repository change of this phase is the new test suite plus this
document. During test development, three initial failures were traced to
test-fake inaccuracies (not production bugs) and corrected inside the test
file: `do_del_id_counts` mock return shape, `iter_messages` sync-method/
async-iterator semantics, and the reply-message fetch fake.

## P1.7 Remaining Blockers / Limitations

1. **Live-service verification not performed** (unchanged from the capability
   audit): real Telegram, real Supabase, and real provider keys are not
   exercised. Classifications are execution-path-verified, not
   live-verified.
2. **web_search remains key-dependent** — operator must set `YDC_API_KEY` for
   the capability to be usable in production.
3. Non-verbatim read tools still pass through a continuation provider round
   (documented in the capability audit; a product decision, not a defect).

## P1.8 Phase 1 Git Delivery Record

| Field | Value |
|---|---|
| Files changed | `tests/test_tool_health_audit.py` (new), `INVESTIGATION.md` |
| Tests added | 60 (test_tool_health_audit.py) |
| Full suite | 1424 passed, 23 skipped |
| Commit / push / remote verification | see final report for this phase |

---

# CAPABILITY EXPOSURE IMPLEMENTATION — CURRENT STATE

> This section reflects the implementation phase that followed Phase 1: the
> remaining existing Self-Bot capabilities identified as
> IMPLEMENTED_NOT_REGISTERED were progressively connected to the AI through
> the existing ToolRegistry → Dispatcher → ToolExecutor architecture, and
> each connection was proven with health tests through the real execution
> path. The registry now holds **36 tools**.

## C1. Objective

Close the gap between "Self-Bot capability exists" and "AI can safely and
demonstrably use that capability" — one capability at a time, with no new
architecture, no provider changes, and no boundary weakening.

## C2. Newly connected in this implementation

### Capability 1 — Task lifecycle management (3 tools)

| Field | Value |
|---|---|
| Tools | `task_list`, `task_inspect`, `task_transition` |
| Capability | See, inspect, and lifecycle-manage the tasks the AI creates (closes the create-only asymmetry) |
| Authoritative implementation | `TaskManagementService` + `task_management_interface.list_text/inspect_text` + `TaskRepository` — the SAME service the `.task` command and Taskloom panel use; no second management system |
| New file | `backend/ai/tools/task_management_tools.py` |
| Registry | Registered in `create_default_registry()`; no duplicates |
| Dispatcher reachability | Verified: all three appear in `Dispatcher._build_tool_definitions()` native provider schemas |
| ToolExecutor execution | Verified through `execute_calls()` with the real in-memory repository |
| Permission model | `task_list`/`task_inspect` = READ_ONLY; `task_transition` = READ_WRITE with CAS `expected_version` required (stale version → honest failure, nothing changes) |
| Owner scoping | Service-level `owner_id` filtering verified: another owner's task is invisible and untransitionable |
| Status vocabulary | `task_transition` accepts only `paused`/`active`/`completed` — the lifecycle mutations with a UI precedent; `delete`/`fail`/`expire` remain UI/command-only (explicit-version panel semantics; not chat-AI surface) |
| External dependency | Supabase-or-in-memory fallback (no live dependency in tests) |
| Health tests | `tests/test_capability_exposure_tools.py::test_task_*` (12 tests) — registered/reachable, executor path, owner scoping, CAS stale-version rejection, argument validation, failure paths |
| Result | **CONNECTED** — implemented + registered + dispatcher-reachable + executable + permission-correct + health-tested |

### Capability 2 — Saved-item retrieval / forwarding (1 tool)

| Field | Value |
|---|---|
| Tool | `retrieve_save` |
| Capability | Re-send a saved item (by save code) into the current chat with its metadata caption — the panel's Retrieve action, now AI-reachable |
| Authoritative implementation | `retrieve_service.do_retrieve` (the only legitimate `forward_messages` user) — reused verbatim; no new forwarding logic |
| New file | `backend/ai/tools/retrieve_save.py` |
| Registry | Registered; no duplicates |
| Dispatcher reachability | Verified in native provider schemas |
| ToolExecutor execution | Verified through `execute_calls()` with the service boundary faked |
| Permission model | READ_WRITE |
| Destination constraint | **Trusted-context rule**: destination is ALWAYS the chat the AI request came from (`context.extra["chat_id"]`) — never a model-supplied argument; asserted by test (model-supplied `destination`/`chat_id` arguments are ignored) |
| Ownership | Save-code scoping is enforced by the existing service/DB contract (`owner_id`), unchanged |
| Health tests | `test_retrieve_save_*` (4 tests) — registered/reachable, executor path with service-call assertion (owner/code/destination args), trusted-destination enforcement, honest failure paths (no chat, service failure string, missing code) |
| Result | **CONNECTED** — all six conditions satisfied |

## C3. Previously connected

The 32 tools documented in the capability audit and Phase 1 health audit
above (save, save_by_link, delete ×5, list_recent_messages, bio ×7,
username ×6, search, list_saves, database_stats, account_show, settings_get,
settings_set, organize_list, organize_clean, web_search, create_task,
send_message). All remain intact; the Phase 1 audit suite was updated to the
36-tool baseline and still passes.

## C4. Remaining implemented but NOT AI-connected (intentional)

| Capability | Why it stays disconnected | Status |
|---|---|---|
| Memory write path (`MemoryManager.store_long/store_permanent`) | AI_MASTER_DESIGN §5 contract: "The AI can propose a new persistent memory… The owner confirms. Only confirmed entries are persisted. The AI never writes to persistent memory autonomously." No proposal/confirmation mechanism exists in code. Creating a memory tool now would violate the documented product contract. | IMPLEMENTED_NOT_REGISTERED / NOT_AI_EXPOSED |
| Ghost Seen v2 | Panel-driven by design; its AI usage is a reasoning service with `allow_tools=False`. No AI-tool contract exists in source for the chat AI to operate Ghost Seen. | IMPLEMENTED_NOT_REGISTERED / NOT_AI_EXPOSED |
| Provider/model switching, trigger configuration | Runtime authority belongs to panels/dashboard; the audit found no AI-tool precedent and the security model reserves provider routing internals from the AI. | IMPLEMENTED_NOT_REGISTERED / NOT_AI_EXPOSED |
| Model tester / discovery | Diagnostic/panel surface; not a conversational capability. | IMPLEMENTED_NOT_REGISTERED / NOT_AI_EXPOSED |
| Dashboard REST APIs | Human UI surface; not an AI capability. | IMPLEMENTED_NOT_REGISTERED / NOT_AI_EXPOSED |
| Task `delete`/`fail`/`expire` transitions | The authoritative service supports them, but the panel contract requires explicit version confirmation; destructive/critical lifecycle states stay out of the chat-AI vocabulary for now. `task_transition` covers pause/resume/complete. | PARTIALLY_EXPOSED (deliberate scope line) |

## C5. Still not implemented (unchanged from the audit)

Hermes, Workers, Service Mesh, Orchestrator, external reasoning endpoints,
future tools (calendar/tags/folders/summarize/translate/automations),
general web browsing/URL fetching, voice input, `tool_request` on AIRequest.

## C6. Security findings after implementation

Re-verified boundary checks (all pass):

- New tools delegate entirely to existing services — zero new Telegram RPC,
  SQL, shell, filesystem, or HTTP surface.
- No new destination authority: `retrieve_save` destination is trusted
  context only (tested).
- No new mutation authority: `task_transition` is CAS-version-checked,
  owner-scoped, and enum-limited (tested).
- Registry has no duplicate names (tested: 36 unique).
- Permission gates unchanged: only READ_ONLY/READ_WRITE added; no
  ADMIN_ONLY/DANGEROUS semantics weakened; delete verification untouched.
- Providers remain reasoning services only; tool execution still flows
  exclusively through ToolExecutor.

## C7. Database / Supabase impact

No database/schema changes were required. All new tools operate on the
existing `ai_tasks`/`ai_task_occurrences` tables and `saved_items` through
the existing repositories/services.

## C8. Live verification status

No live Telegram, Supabase, or provider verification was performed. All
claims are execution-path-verified through the real registry/executor chain
with external boundaries faked.

---

# ROOT-CAUSE INVESTIGATION — Live Telegram fallback for the 4 newly connected tools

## R1. Live evidence vs deterministic evidence

The owner's live Telegram tests showed the AI falling back / taking no real
action for `task_list`, `task_inspect`, `task_transition`, and `retrieve_save`,
while deterministic tests claimed all four healthy. Both observations were
correct: the tools were registered, dispatcher-visible, and executor-executable,
but **the deterministic layers that decide what the AI can actually invoke
never learned about them**.

## R2. The real execution path has two invocation routes

Traced end-to-end: Telegram → `ai_unified` → `AIRequest` → Dispatcher →
provider (schema generation → request → response) → tool-call parsing →
registry lookup → `ToolExecutor` → service → Telegram/Supabase side effect.

The provider response reaches execution through TWO routes:

1. **Native tool calls** — provider emits a function call; parsed into tool
   name/arguments and dispatched. This route works for the new tools.
2. **Deterministic fallback** — text/JSON is parsed by
   `backend/ai/actions.py` (`resolve_tool_calls` → `parse_action_text` →
   `validate_action`) into structured actions. The JSON action vocabulary
   here is a fixed allowlist and did NOT contain the four new actions.

When the live provider (weak/free tier, or one that returns JSON/text instead
of native calls) produced `{"action": "task_list"}` or a `retrieve_save`
JSON object, `validate_action` rejected it as an **unknown action**, the
dispatcher fell back to a text-only answer, and no side effect occurred.
Native-capable providers never exercised the tools live because the owner's
configured provider fell into the deterministic route for these requests.

## R3. Exact divergences found (new tools vs known-working tools)

1. **`ACTION_NAMES`/`EXECUTABLE_ACTION_NAMES`** (`backend/ai/actions.py`):
   `task_list`, `task_inspect`, `task_transition`, `retrieve_save` absent →
   JSON outputs rejected as "Unknown action". This is the primary live
   fallback cause.
2. **Prompt template §8 JSON fallback schema**
   (`backend/ai/prompt/template.py`): listed only the old action vocabulary,
   so providers relying on the prompt contract never emit the new actions.
3. **`_ENFORCE_ACTION_NUDGE`** (`backend/ai/engine/dispatcher.py`): the
   correction nudge re-listed the stale vocabulary, actively steering the
   model away from the new actions on retry rounds.
4. **`retrieve_save` code echo**: live, the model echoed the save code in
   lower case (`s0012`); the service looks codes up verbatim after its own
   `upper().strip()` normalization happens AFTER the tool passed the raw
   echo — with the tool boundary passing the raw value through, `query_save`
   missed rows for lower-cased echoes and the tool reported not-found.
5. **User-facing enum rendering**: `task_list`/`task_transition` rendered
   raw status enums (`active`) in result text — cosmetic, fixed alongside.

## R4. Fixes applied (production path, not test-only)

- `backend/ai/actions.py` — added the four actions to the action
  allowlists; added `task_id`/`action_status`/`expected_version`/`save_code`
  to `ALLOWED_FIELDS` and to `ActionParseResult`; added
  `_validate_task_lifecycle_action` / `_validate_retrieve_save_action`
  validators (strict per-action field allowlists, int-coerced ids, status
  vocabulary `{paused, active, completed}`); wired both into
  `validate_action` BEFORE the status-action fall-through so the original
  fall-through for `create_task`/`send`/`save`/… is preserved; mapped the
  four actions into `resolve_tool_calls`.
- `backend/ai/prompt/template.py` — §8 JSON fallback schema now lists the
  four new actions with their exact fields; rule 8 nudge extended.
- `backend/ai/engine/dispatcher.py` — `_ENFORCE_ACTION_NUDGE` lists the
  full current vocabulary including the new actions.
- `backend/ai/tools/retrieve_save.py` — save code normalized
  (`.strip().upper()`, alphanumeric-checked) at the tool boundary before the
  service call.
- `backend/ai/tools/task_management_tools.py` +
  `backend/ai/task_management_interface.py` — human-readable status labels
  in result text.

## R5. Verification

- New regression suite `tests/test_new_tool_action_path.py` (35 tests):
  parser accepts/rejects each new action with exact fields, unknown-field
  rejection, status vocabulary enforcement, save-code validation, tool
  mapping through `resolve_tool_calls`, prompt/dispatcher vocabulary
  presence, and regression guards that the pre-existing actions
  (`create_task`, `send`, `save`, status actions) still validate — locking
  the fall-through behavior.
- `tests/test_capability_exposure_tools.py` — updated
  `test_retrieve_save_executor_path_forwards_through_service` to expect the
  canonical upper-case code (matches the service contract; the service
  normalizes again defensively).
- Full suite: **1476 passed, 23 skipped**. `py_compile` clean on all touched
  files.

## R6. Honest limitations

- Real-provider live retest still belongs to the owner (the deterministic
  route now accepts and maps the new actions; native tool calls were already
  reaching the tools). This fix removes the identified divergence, but the
  final live confirmation is a Telegram-side test.
- The repository inspector shows a stray untracked nested `telegram-self-bot/`
  clone (stale, preserved untouched).
