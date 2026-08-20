# INVESTIGATION

## INVESTIGATION METADATA

| Field | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Branch | `main` |
| Current HEAD | `767b32bcee796a6a8c6e30e5d7c3ab751d2e69e4` (`767b32b`) |
| Remote state at investigation start | `HEAD` and `origin/main` aligned at `767b32b` |
| Investigation date | 2026-08-20 |
| Scope | Current Telegram runtime, AI execution, providers, Delete, Save, Profile, database/management tools, security, tests, and documentation drift |
| Status | INVESTIGATION + IMPLEMENTATION |
| Production code changed | Yes — bounded deterministic semantic Delete (parser, predicate layer, service matcher) |
| Database/Supabase changed | No |
| Live Telegram verification | Not performed |
| Test evidence | 572 repository tests passing (519 prior + 53 new semantic-Delete tests in `tests/test_32_semantic_delete.py`); every prior Delete ownership/regression/timeout test still passes unchanged. This report does not claim live Telegram reliability from mocks. |

This document completely replaces the previous `INVESTIGATION.md`. It is based on the current tracked source, current documentation, recent commit history, and the available test evidence. Prior findings are retained only when the current source still proves them.

Classification used throughout:

- **CONFIRMED** — directly shown by current source, tests, or recorded logs.
- **LIKELY** — strongly indicated by the source but not proven by a live integration run.
- **UNKNOWN** — the repository does not provide enough evidence.

---

## 1. EXECUTIVE SUMMARY

The repository is a Python 3.11 Telethon self-bot with a FastAPI dashboard and React/Vite frontend. The backend is a single asyncio process. `RuntimeSupervisor` owns the self-client lifecycle, optional helper bot, recovery, health monitors, web server, and shared profile scheduler. The current source is materially newer than parts of the documentation and the previous investigation.

The current AI execution boundary is substantially real:

```text
outgoing Telegram message
  → owner/trigger/reply detection in ai_unified.py
  → immutable AIRequest with chat/message/reply context
  → Dispatcher
  → deterministic local parser when confidence is high
     OR provider prompt/native tool call/structured action
  → local action validation
  → fixed ToolRegistry + ToolExecutor
  → service layer / Telegram facade / database
  → real tool result
  → silent delete or edit-in-place response
```

**CONFIRMED — the AI is not an unrestricted Telegram controller.** Providers emit text or normalized tool calls. Registered tools are the only executable operations, `ToolExecutor` validates the tool name/arguments/permission level, and services perform the actual side effect.

**CONFIRMED — self-only Delete enforcement exists at the execution boundary.** `delete_service.delete_verified_self_messages()` re-fetches candidate messages immediately before deletion and requires both Telegram's `out` flag and a sender ID matching the authenticated account ID. Unknown identity, missing messages, foreign senders, and non-outgoing messages fail closed.

**CONFIRMED — the recent Delete timeout defect was addressed in the current commit series.** The current Delete service uses a 1,000-message scan cap, 5-second per-RPC guards, 100-message verification/deletion chunks, bounded retry for ownership reads, and a 25-second operation deadline inside a 30-second Delete tool deadline. The recorded current test result is 519 passing tests, including 84 Delete-focused tests.

**CONFIRMED — current-request handling changed and is now self-inclusive for scoped Delete.** The handler captures the original Telegram message ID before provider/config work. The self-owned selector can include that message when it is inside last-N, all, time, filtered, or anchor scope. For an “until this message” request, the same ID is used as the boundary. Silent delivery avoids creating a replacement confirmation.

**CONFIRMED — semantic Delete is still the least complete part of the AI execution surface.** There are two paths:

1. Direct topic requests recognized by `parse_command_intent()` can use the local self-only text filter in `delete_service` without a provider round-trip.
2. Ambiguous/search-style semantic requests use `list_recent_messages` so the provider selects concrete IDs, then `delete_messages_by_ids` re-validates them.

The second path exposes only a bounded recent window, currently up to 100 messages, and relies on provider interpretation of short text previews. There is no embedding/indexed semantic search implementation. Therefore a request such as “messages with two English words” can fail because of intent extraction, insufficient retrieval, text-preview limits, or provider reasoning. The exact live failure boundary is **UNKNOWN** without the production request trace and the actual chat history.

**CONFIRMED — several prior investigation findings are obsolete.** The current heartbeat explicitly treats a disabled helper as valid and does not classify a quiet account as an event stall. The current `failsafe.py` imports `guarded_create_task`; the previously reported missing-import `NameError` is not present at `767b32b`.

**CONFIRMED — one runtime reliability gap remains:** `RuntimeSupervisor._watchdog_loop()` is still defined but is not started by `RuntimeSupervisor.start()`. The active monitors are heartbeat, keepalive, failsafe, diagnostics, and memory cleanup. Whether the dormant watchdog is intentionally retired or an incomplete migration is **UNKNOWN**; it must not be assumed to be active.

**CONFIRMED — the recommended semantic-Delete slice is now implemented and tested.** The next task in the previous investigation (bounded semantic Delete for real Telegram history) was completed: deterministic Persian/English normalization and tokenization, exact N-word / exact N-English-word structural predicates, composition with count/time/boundary scopes, and a pure matcher wired into the existing self-only bounded selector. See §5.6, §17, §20, and §21 for the exact surface, files, tests, and results.

**Recommended next task:** harden the provider-backed semantic read path — give `ListRecentMessagesTool` the same per-page RPC guard used by the Delete service selector — and then reconcile legacy Delete-by-ID/panel entry points so every Delete path shares identical current-request anchor semantics. This closes the remaining Delete-surface gaps flagged in §12. The dormant watchdog/recovery path remains a separate reliability task and must not be reactivated incidentally.

---

## 2. CURRENT ARCHITECTURE

### 2.1 Process and entry points

**CONFIRMED:** The backend entry point is `python -m backend.main`.

Current startup path:

```text
backend.main.main()
  → config.load()
  → install crash diagnostics
  → RuntimeSupervisor(cfg)
  → supervisor.start() with startup retry policy
  → wait on shutdown_event
  → supervisor.stop()
```

`backend/config.py` loads required Telegram identity variables and optional Supabase, helper, timezone, profile, port, logging, and AI configuration. `backend/bot/client.py::build_client()` constructs the Telethon self-client with `StringSession`, connects, authorizes, and resolves `get_me()` under bounded startup waits.

`backend/main.py` also installs signal handlers and uses `guarded_create_task()` for signal-triggered shutdown. The session string and credentials are configuration inputs; the current source does not expose them to the AI tool context.

### 2.2 Runtime ownership

`backend/runtime/supervisor.py::RuntimeSupervisor` is the active lifecycle owner. Its current responsibilities include:

- `_build_and_register()` — build the self-client, register handlers, and wire AI tools.
- `_start_helper()` / `_start_helper_loop()` — optional helper bot.
- `_resume_bio_cron()` and `_resume_username_cron()` — resume profile engines.
- `_start_web_server()` — run Uvicorn/FastAPI.
- `_run_loop()` — await `client.run_until_disconnected()`.
- `_trigger_reconnect()` — bounded lightweight reconnect under `_recovery_lock`.
- `_trigger_full_recovery()` / `_do_recovery()` — rebuild and re-register after failure.
- `_hard_reset_runtime()` — last-resort rebuild path.
- `stop()` — deterministic shutdown.

Current runtime constants include a 60-second reconnect cooldown, 180-second full-recovery cooldown, bounded connect/register/RPC waits, and a maximum recovery-attempt policy.

### 2.3 Telegram and helper clients

The self-client is the authenticated owner account. A helper bot is optional and is only built when helper configuration enables it. The helper renders Glass UI inline panels; when disabled, the system uses the existing fallback/edit behavior.

`backend/bot/router.py::register_all()` registers runtime hooks and feature handlers in isolation:

```text
runtime hooks
  → misc
  → save
  → retrieve
  → delete
  → organize
  → bio
  → discover
  → database
  → username
  → ai settings panel
  → ai_unified
```

Handlers are registered on the self-client and the owner gate is centralized in `backend/bot/handlers/guard.py::is_owner()`.

### 2.4 UI and web surface

The Telegram interface is Glass UI first. `backend/helper/` contains panel registries, lifecycle/session management, inline rendering, input state, pagination, callback tracing, and RPC timeout helpers.

The dashboard is a React 18/Vite/Tailwind application under `src/`. It calls the backend API through `src/lib/api.ts`. The current frontend has Saves, Bio Engine, AI, and Logs views. `backend/web/app.py` serves health, saves, bio, logs, AI provider/model/config/test endpoints, and the built SPA when available.

### 2.5 Business services

Handlers and AI tools delegate to services rather than implementing domain logic themselves:

- `backend/services/save_service.py`
- `retrieve_service.py`
- `delete_service.py`
- `discover_service.py`
- `database_service.py`
- `settings_service.py`
- `organize_service.py`
- `bio_service.py`
- `username_service.py`

The AI tools and Glass UI therefore converge on the same service functions for the domains that are wired.

---

## 3. AI EXECUTION ARCHITECTURE

### 3.1 Activation and request capture

**CONFIRMED:** `backend/bot/handlers/ai_unified.py` is the canonical natural-language AI activation path.

It supports:

- English trigger matching, case-insensitive.
- Persian trigger matching, exact according to the configured trigger.
- Reply-aware trigger mode.
- Reply-to-known-AI continuation without requiring a trigger.
- Skipping dot-prefixed messages through the handler’s activation rules.

`_execute_ai()` captures `event.chat_id` and `event.message.id` before configuration loading, provider selection, status edits, or provider inference. It creates an `AIRequest` containing `chat_id`, `message_id`, `reply_context`, `timezone`, owner ID, and a request ID.

This is important for Delete: provider response messages cannot replace the original request as the execution anchor.

### 3.2 AIRequest and context

`backend/ai/session/request.py::AIRequest` is a frozen dataclass. Relevant fields are:

- `session_id`
- `user_message`
- `owner_id`
- `chat_id`
- `message_id`
- `reply_context`
- `timestamp`
- `language`
- `timezone`
- `metadata`
- `request_id`

`backend/ai/engine/dispatcher.py::_build_context()` assembles conversation history, memory, runtime context, preferences, reply context, and tool context for prompt construction. The conversation history is AI session history, not Telegram chat history. Real Telegram history is obtained only by Telegram-aware tools such as `ListRecentMessagesTool` or Delete service selectors.

### 3.3 Prompt and tool schemas

Prompt files:

- `backend/ai/prompt/template.py`
- `backend/ai/prompt/builder.py`
- `backend/ai/prompt/serializer.py`
- `backend/ai/prompt/formatter.py`
- `backend/ai/prompt/validator.py`
- `backend/ai/prompt/budget.py`

The Dispatcher injects the registry’s tool schemas into the prompt and also sends provider-native OpenAI-style function definitions where supported. `ToolRegistry.list_schemas()` is converted by `Dispatcher._build_tool_definitions()` into object/function schemas.

The default system prompt states that the assistant calls tools, never performs actions directly, responds concisely, and asks for clarification when uncertain. Retrieved Telegram content is intended to be data rather than instructions. This prompt rule is useful, but it is not the final security boundary; the execution layer is.

### 3.4 Intent resolution order

`backend/ai/actions.py` contains the deterministic action contract:

- `ActionParseResult`
- `extract_json_object()`
- `validate_action()`
- `parse_action_text()`
- `parse_command_intent()`
- `resolve_tool_calls()`

The current Dispatcher can resolve actions through:

1. Native provider tool calls.
2. Deterministic parsing of the original user message.
3. JSON/structured action extraction from provider text.
4. One bounded action-format recovery retry when prose does not resolve.

Unknown actions, invalid fields, unsupported targets, invalid counts, and malformed tool arguments are rejected before execution.

The deterministic parser contains Persian/English vocabulary, Persian digit coercion, count parsing, all/time/boundary/query scope recognition, save/delete recognition, account-status semantics, and the first-name versus real-`@username` distinction.

### 3.5 Local fast path

`Dispatcher._try_local_fast_path()` runs before provider inference for high-confidence actions. Current examples include:

- Status and account queries.
- Save/reply and save-by-link.
- Explicit Delete IDs.
- Explicit Delete counts.
- Replied-message Delete.
- Current-message/anchor and explicit range scopes.

Conversational requests and ambiguous semantic requests are intentionally left to the provider path unless the parser can safely convert a direct topic request into a local Delete query.

**CONFIRMED:** the fast path uses the same `ToolExecutor` as provider-generated calls. It does not create a second execution architecture.

### 3.6 Tool execution and authorization

`backend/ai/tools/base.py` defines tool contracts, `PermissionLevel`, and `ToolResult`. `backend/ai/tools/registry.py` creates the fixed tool allowlist. Current registered tools include:

- Save: `save`, `save_link`.
- Delete: `delete`, `delete_by_id`, `delete_replied`, `delete_message_by_id`, `list_recent_messages`, `delete_messages_by_ids`.
- Profile: Bio and Username setters/status tools.
- Saved content: `search`, `list_saves`.
- Database/status: `database_stats`.
- Account: `account_show`.
- Settings: `settings_get`, `settings_set`.
- Organize: `organize_list`, `organize_clean`.

`ToolExecutor._execute_single()` performs:

```text
name/argument shape checks
  → registry lookup
  → permission-level decision
  → bounded tool execution (or explicit long-running exemption)
  → ToolResult capture
  → history/telemetry recording
```

`MAX_TOOLS_PER_TURN` is 5. Default non-long-running tool timeout is 10 seconds. Tools may expose a tool-specific `timeout_seconds`; Delete exposes a 30-second deadline and internally uses a stricter 25-second operation deadline. Deep Save is explicitly marked long-running and does not use the generic 10-second wrapper.

The current executor auto-executes `READ_ONLY`, `READ_WRITE`, and `DANGEROUS` tools because the owner’s outgoing self-bot message is treated as authorization. `ADMIN_ONLY` and `CONFIRMATION_REQUIRED` remain blocked pending confirmation.

**SECURITY CONFIRMED:** no registered tool accepts an arbitrary Telegram method name, shell command, Python expression, or unrestricted database query.

### 3.7 Result and response delivery

The Dispatcher records real tool results and does not fabricate successful side effects. `backend/bot/handlers/ai_unified.py` uses `backend/ai/tools/delivery.py::deliver_response()` for normal AI replies.

Pure Delete tool names are recognized by `_is_silent_delete()`. On a successful pure Delete result, the handler does not send a new Telegram message. It best-effort edits the original event back to the display prompt; if the request itself was deleted, the edit fails harmlessly. This preserves zero-spam behavior.

A Delete failure is not silently treated as success: it reaches the error path and is rendered/edited as an error when possible.

### 3.8 Provider failure interaction

Provider failures are normalized into controlled `EngineResult`/`ProviderResponse` errors. The Dispatcher has bounded empty-response and action-format recovery paths. A provider failure can still prevent an ambiguous semantic request from reaching a tool if no deterministic local interpretation exists; this is the principal reason direct semantic Delete handling was added.

---

## 4. PROVIDER / FALLBACK ARCHITECTURE

### 4.1 Registered providers

`backend/ai/providers/factory.py` registers these provider classes when their keys are available:

- `dummy`
- `gemini`
- `openai`
- `openrouter`
- `cerebras`
- `mistral`
- `groq`
- `zai`
- `sambanova`
- `nvidia`
- `cohere`
- `siliconflow`
- `fireworks`

Most use `OpenAICompatProvider`; Gemini has its own provider implementation and translates native tool definitions for Gemini’s API shape. The `dummy` provider is always present as the terminal fallback.

The actual active provider set is environment-dependent. Which keys/models are configured in production is **UNKNOWN** from source-only inspection because secrets and production environment values were not read.

### 4.2 Provider manager

`backend/ai/providers/manager/manager.py::ProviderManager` is the routing authority. Its current behavior is:

```text
active provider
  → configured fallback chain
  → other registered non-dummy providers
  → capability/health/model filtering
  → reliability/latency scoring
  → bounded attempt
  → at most one immediate retry for transient failure
  → next candidate
  → controlled failure response through dummy fallback metadata
```

Providers requiring native tools are skipped when they do not advertise tool/function-call capability. Unconfigured, unhealthy, unavailable-model, disabled, and cooling-down providers are skipped with a reason recorded in a failure matrix.

### 4.3 Timeouts, retries, cooldowns

`ProviderManager._call_once()` uses `guarded_await()` with a 30-second provider RPC timeout. `_attempt_with_retry()` retries at most once for `network`, `timeout`, and `server` categories after a one-second backoff.

`backend/ai/providers/manager/health.py::ProviderHealthTracker` uses monotonic cooldown deadlines:

- Rate limits honor `retry_after` where present.
- Auth failures disable a provider until configuration changes.
- 404/model-not-found is marked unavailable for the provider/model TTL and is not hammered every request.
- Transient failures receive category-specific cooldowns.
- Five consecutive failures open a 600-second quarantine.
- Success clears failure state and restores provider availability.
- Per-provider semaphores bound concurrency; default concurrency is 4 with provider-specific overrides.

**CONFIRMED:** the routing layer has bounded provider retries and does not retry forever.

**LIKELY:** real provider availability will vary significantly by API key, model, quota, and tool-calling support. Source and unit tests cannot prove production provider health.

### 4.4 Known provider limitations

- `AI_ENABLED` defaults to disabled and `dummy` is the default provider.
- The active model is environment/config selected and may be overridden per provider.
- Model discovery/test endpoints exist, but a successful model-discovery request does not prove future inference availability.
- Provider-specific tool-call compatibility is mediated by capabilities and normalization, but live behavior for every provider/model combination is **UNKNOWN**.
- A semantic Delete request that depends on provider reasoning can still fail if every eligible tool-capable provider is unavailable. Deterministic Delete scopes avoid that dependency.

---

## 5. DELETE ARCHITECTURE

### 5.1 Tool and service path

Current deterministic path:

```text
owner outgoing message
  → ai_unified.py captures original chat_id/message_id
  → AIRequest(request_id, chat_id, message_id, timezone, reply_context)
  → Dispatcher.parse_command_intent / local fast path
  → DeleteTool arguments
  → ToolExecutor timeout/permission boundary
  → delete_service.do_del_self_filtered()
  → bounded Telegram history scan
  → self-owned candidate selection
  → delete_verified_self_messages()
  → re-fetch ownership verification
  → chunked client.delete_messages()
  → ToolResult
  → silent Telegram delivery for pure successful Delete
```

Reply-target path:

```text
reply context
  → DeleteRepliedTool
  → bounded get_messages()
  → outgoing precheck
  → delete_verified_self_messages()
  → final sender/out verification
  → delete_messages()
```

Semantic/provider path:

```text
natural-language topic request
  → provider or deterministic semantic interpretation
  → ListRecentMessagesTool, when provider path is used
  → provider selects concrete IDs it actually saw
  → DeleteMessagesByIdsTool
  → delete_verified_self_messages()
  → Telegram deletion
```

### 5.2 Retrieval bounds and timeout state

`backend/services/delete_service.py` defines:

- `_MAX_DELETE_SCAN_MESSAGES = 1000`
- `_DELETE_VERIFY_CHUNK = 100`
- `_DELETE_RPC_TIMEOUT_SECONDS = 5.0`

`_iter_messages_bounded()` wraps each `__anext__()` call with `rpc_await()`. It never requests an unbounded Delete history. `delete_verified_self_messages()` re-fetches ownership in chunks of 100 and deletes verified IDs in chunks of 100.

Safe ownership reads receive one bounded retry for transport/timeout errors. Delete batch failures are accumulated while subsequent verified batches are attempted, then a deterministic error is returned to the tool.

`DeleteTool.execute()` applies `_DELETE_OPERATION_TIMEOUT_SECONDS = 25` around the service operation and exposes `timeout_seconds = 30` to `ToolExecutor`. This is a bounded implementation, not an unlimited timeout exemption.

**CONFIRMED:** the previous production “Tool 'delete' timed out” path was caused by unbounded/insufficiently guarded Delete history/RPC work being caught by the generic executor deadline. The current commit series adds bounds at the service boundary and tests the timeout/partial-failure behavior.

**UNKNOWN:** live Telegram cancellation behavior under a stuck lower-level socket cannot be proven by mocked tests alone.

### 5.3 Ownership enforcement

`delete_service._resolve_me_id()` uses cached `client.me` when available and otherwise a bounded `get_me()` call. `_is_self_owned()` requires:

1. message exists;
2. authenticated account ID is resolved;
3. message has Telegram `out=True`;
4. message has a sender ID;
5. sender ID equals authenticated account ID.

The final `delete_verified_self_messages()` call re-fetches every candidate from Telegram immediately before deletion. This is the authoritative security boundary. A foreign message, bot/admin message, system message, stale ID, missing sender, missing identity, or unknown ownership is rejected and never reaches `client.delete_messages()`.

AI-generated responses remain eligible because eligibility uses Telegram metadata, not message text, formatting, reply structure, or AI markers.

### 5.4 Current request and anchor semantics

`_execute_ai()` captures the original request ID before status edits/provider work. `Dispatcher._build_tool_context()` forwards it as `extra["request_message_id"]` and forwards `request_id`, `chat_id`, and reply metadata.

Current `DeleteTool` behavior:

- Last-N/all/time/filtered scopes may include the request if it is self-owned and inside the scope.
- `until_message` uses a supplied `boundary_id`, replied message boundary, or the original request ID as the boundary.
- The request is not excluded by a generic “current message” filter in the current scoped Delete path.
- The final ownership gate still applies to it.

`ListRecentMessagesTool` also intentionally exposes the active request if it is inside the bounded recent window.

**CONFIRMED:** the AI response cannot become the original anchor because the request ID is captured before provider execution.

**LIKELY limitation:** explicit legacy paths such as `DeleteByIdTool`/`do_del_id_counts()` have different range semantics from the scoped `DeleteTool`; a full request-ID inclusion audit across every legacy panel/ID path is still warranted before claiming identical semantics for all Delete entry points.

### 5.5 Last-N, all, time, and boundary selection

`select_self_owned_message_ids()` scans newest-first and applies:

- count limit, maximum 500 for count-based requests;
- optional local time floor and cutoff;
- optional message boundary;
- optional text query;
- self ownership before semantic text selection;
- current request participation when in scope;
- bounded early stopping when count/time range is satisfied.

A missing boundary does not expand to whole-chat deletion. Invalid times/counts/IDs return controlled errors. Bare `HH:MM` values are interpreted as local wall-clock time in the configured timezone; the source includes a deterministic Tehran UTC+03:30 fallback when system tzdata is unavailable.

### 5.6 Semantic Delete — deterministic predicate layer (implemented)

There are now three paths, with deterministic rules covering the previously failing structural requests.

**Deterministic normalization/tokenization (`backend/ai/semantic_delete.py`) — CONFIRMED by source + tests.**

- `normalize_text()` folds Arabic glyph variants to Persian (`ي→ی`, `ك→ک`, `ة→ه`, hamza-alef variants → `ا`), normalizes Persian/Arabic-Indic digits to ASCII, strips Arabic diacritics, removes apostrophes, lowercases, and removes zero-width characters (ZWNJ/ZWJ/ZWSP/BOM).
- `tokenize()` applies the same character normalization but treats zero-width characters as word separators — the conventional Persian word-segmentation treatment.
- Word-count semantics are defined precisely: an English lexical word is a pure ASCII-letter token; a Persian lexical word contains Persian/Arabic letters; total words are tokens containing at least one letter; pure digit runs, emoji, and punctuation are never words.
- No embeddings, no vector index, no external semantic service, no provider dependency.

**Deterministic structural predicate parsing (`parse_structural_predicate`) — CONFIRMED by source + tests.**

- Recognizes exact N-word and exact N-English-word predicates in Persian and English, digits or number words, with and without ZWNJ (`دو کلمه‌ای`, `دوکلمهای`, `2-word`, `two word`, `exact 3-word English`), including Persian compound numbers (`بیست و پنج کلمه‌ای` = 25). Out-of-range/ambiguous counts (e.g. 150 words) return `None` so the caller clarifies instead of guessing.
- Critical regression fixed: in `parse_command_intent`, a number immediately followed by a word marker (`کلمه`/`word`/`واژه`) is a word count, never a positional deletion count — “پاک کن پیام‌های دو کلمه‌ای انگلیسی رو” previously parsed as delete-2 and now parses as the exact-two-English-words predicate.
- Deterministic predicates execute through the existing local fast path with **no provider round-trip** (verified by `test_semantic_delete_runs_fast_path_without_provider`).

**Direct deterministic topic path (unchanged, now normalized) — CONFIRMED by source + tests.**

- `parse_command_intent()` topic requests (`مربوط به X`, `about X`) still resolve to `DeleteTool(mode="filtered", query=...)`; the service now matches the query and message text on the normalized form so script variants/digits/zero-width characters do not defeat an exact content match.

**Semantic + scope composition — CONFIRMED by source + tests.**

- The parsed predicate composes with count (`پاک کن ۱۰ پیام دو کلمه‌ای انگلیسی`), time (`... امروز رو پاک کن` → `after_time="today"`), and boundary (`تا این پیام ...` → `until_message`) scopes through the existing `select_self_owned_message_ids` selector, which applies self ownership BEFORE any text predicate and remains capped at 1,000 messages with per-RPC `rpc_await` guards.
- Final deletion still goes exclusively through `delete_verified_self_messages()` (re-fetch + `out=True` + sender-ID equality). The current request stays eligible inside scope; AI-generated self messages stay eligible; foreign/missing-sender/unknown-identity messages are rejected.

**Provider-backed semantic path (unchanged fallback) — LIKELY still weaker per-page RPC protection.**

- `ListRecentMessagesTool` remains bounded (limit ≤ 100) and its IDs still pass through the final ownership chokepoint, but its `iter_messages` loop does not reuse the Delete service’s `rpc_await` wrapper per page. It is a READ_ONLY path; a hanging page is caught by the executor’s generic bounded tool timeout, but aligning per-page guards is the next recommended task.

**Remaining limitations (UNKNOWN / not covered by this work):** deep linguistic coverage of every Persian colloquial spelling, guaranteed retrieval beyond the bounded recent window, and live Telegram behavior — none of which mocked tests can prove.

### 5.7 Silent behavior

`ai_unified.py::_is_silent_delete()` recognizes pure Delete tool results. On successful pure deletion it does not use `deliver_response()` and does not send “Deleted,” “Done,” or an equivalent confirmation. This preserves the established zero-spam behavior, including when the request message was deleted.

A failed Delete is not silent success; it follows the error path.

### 5.8 Delete test evidence

Focused tests currently cover ownership, parser/regression behavior, scopes, semantic handling, current-request inclusion, AI-generated self messages, timeout bounds, batching, partial failure, Tehran cutoff handling, repeated requests, and silent delivery:

- `tests/test_26_silent_delete.py`
- `tests/test_27_delete_ownership.py`
- `tests/test_28_delete_regression.py`
- `tests/test_29_delete_expansion.py`
- `tests/test_30_delete_timeout_hardening.py`
- `tests/test_31_delete_rpc_failures.py`

Recorded current result: 84 Delete-focused tests and 519 repository tests passed. These are mocked/unit-level guarantees; no live Telegram deletion was performed during this investigation.

---

## 6. SAVE ARCHITECTURE

### 6.1 Deep Save

`backend/services/save_service.py::execute_save()` is the current Deep Save implementation:

```text
source/reply message
  → allocate short save code
  → inspect media and metadata
  → create safe temporary file when media exists
  → download_media()
  → verify file exists/non-empty
  → send_file() to Saved Messages as a new message
  → extract new uploaded metadata
  → persist saved_items row
  → return honest confirmation/failure
```

Captions preserve original text and include structured save metadata. Media type, MIME type, size, filename, file ID, timestamps, tags, source chat/message IDs, and Saved Messages IDs are recorded where available.

**CONFIRMED:** the Save service does not use `forward_messages()` for Save. `forward_messages()` appears in `retrieve_service.py` for sending an already-saved asset back to a target chat, which is a retrieval operation rather than Save.

`SaveTool.long_running = True` prevents the generic 10-second AI tool timeout from cancelling a legitimate large media transfer. The Glass UI Deep Save reply listener uses an unbounded listener timeout, with separate pending-input expiry retained by the helper state system.

### 6.2 Save by link

`execute_link_save()` parses Telegram links, resolves the target message, and routes it into the same Deep Save pipeline. Link-origin metadata is preserved. The exact behavior depends on Telegram entity resolution and the target message being accessible.

### 6.3 Persistence and concurrency

`backend/db/client.py` uses a singleton Supabase client when URL/service-role configuration is available and an in-memory fallback otherwise. Public database calls are async wrappers around synchronous Supabase work through `asyncio.to_thread()` with a 10-second database timeout.

`get_next_save_code()` uses `_save_code_lock` and generates the current short `S####` style code, with collision handling. Writes are performed after successful Telegram upload; a database failure can leave the uploaded Saved Messages message present without a complete database record, and the service reports this honestly.

### 6.4 Save limitations

- Supabase-unavailable fallback is process-memory-only and is lost on restart.
- A successful upload followed by DB failure can produce an orphaned Saved Messages asset; the result reports the partial state, but automatic reconciliation is a management concern.
- Media transfer remains intentionally long-running; end-to-end production timing under large files is **UNKNOWN**.
- The old `README.md` feature/command sections contain stale legacy references to Forward Save and dot commands even though current source/AGENTS rules describe Deep Save only. `remote_readme.md` is older still and should not be treated as current behavior.

---

## 7. PROFILE ARCHITECTURE

### 7.1 Shared engine

`backend/profile/engine.py::ProfileEngine` is parameterized by the Telegram field and state keys.

- Bio wrapper updates Telegram `about`, persists `bio_state.last_bio`.
- Username wrapper updates Telegram `first_name`, persists `username_state.last_name`.

Wrappers:

- `backend/bio/engine.py`
- `backend/username/engine.py`

Services:

- `backend/services/bio_service.py`
- `backend/services/username_service.py`

### 7.2 Shared scheduler

`backend/profile/scheduler.py` is one minute-boundary scheduler. It registers independent updaters, tracks each engine’s active flag, collects updates, and sends one profile update where possible. `stop_if_idle()` stops only when neither engine is active.

The scheduler uses a bounded Telegram API timeout, retry/backoff handling, timezone conversion, FloodWait handling, supervised task creation, and cancellation cleanup.

**CONFIRMED:** turning Bio off is not supposed to stop Username, and vice versa.

### 7.3 Current profile behavior

Bio supports template/text/mood/state operations. Username uses the same generalized shape but writes `first_name`, not Telegram’s real `@username`. The AI account/status tools distinguish casual “username/account name” requests from explicitly qualified real Telegram `@username` requests. `AccountShowTool` allows only `first_name`, `last_name`, `full_name`, and `username`, and excludes phone/session/credential fields.

The future idea of dynamic Bio mood/text changes and first-name mode changes is not a completed feature. Current source has template, mood, text, and periodic updater primitives, but it does not prove the requested future policy that First Name must change only the mood/mode component while never changing its text component.

**Status:** current Bio/First Name engines are implemented; future dynamic policy is **MISSING/FUTURE**, not a regression.

---

## 8. DATABASE / MANAGEMENT TOOLS

### 8.1 Core database layer

`backend/db/client.py` provides Supabase-or-memory CRUD for saved items, Bio state, Username state, bot logs, and related settings. Heavy synchronous calls are moved to a worker thread with bounded timeout. The backend uses service-role writes; the dashboard reads through backend API routes.

`backend/ai/persistence.py`, `backend/ai/config_store.py`, and `backend/ai/database/` provide AI persistence/config/repository interfaces with in-memory fallbacks. The repository contains schema references and migrations for core data; whether every documented AI table is applied to the user’s live Supabase project is **UNKNOWN** and must not be changed by an agent.

### 8.2 Management/service inventory

| Capability | Source | State | Evidence |
|---|---|---|---|
| List saved items | `discover_service.do_list`, `ListSavesTool` | IMPLEMENTED | Uses DB list path and bounded result formatting. |
| Search saved items | `discover_service.do_find`, `SearchTool` | IMPLEMENTED | Searches saved metadata/caption fields through DB layer. |
| Retrieve/preview saved item | `retrieve_service.do_preview`, `do_retrieve` | IMPLEMENTED | Preview and resend paths exist; resend uses Telegram forwarding for retrieval. |
| Rename saved item | `retrieve_service.do_rename` | PARTIALLY IMPLEMENTED | Service exists; AI registration for this management action is not present in the current tool registry. |
| Move saved item | `retrieve_service.do_move` | PARTIALLY IMPLEMENTED | Service exists; current AI tool registry does not expose a corresponding tool. |
| Delete saved item record/asset | `retrieve_service.do_delete`, DB delete functions | IMPLEMENTED via service/UI | AI exposure is not the same as chat-message Delete and is not a general arbitrary DB delete. |
| Database statistics | `database_service.do_stats`, `DatabaseStatsTool` | IMPLEMENTED | Read-only status path. |
| Orphan cleanup | `database_service.find_orphans`, `do_clean`, `OrganizeCleanTool`/Glass UI paths | PARTIALLY IMPLEMENTED | Service/UI capability exists; exact live Telegram-vs-DB reconciliation behavior needs deployment validation. |
| Vacuum/maintenance | `database_service.do_vacuum` | IMPLEMENTED in service/UI scope | Not exposed as a separate current AI tool in `ToolRegistry`. |
| Panel/settings management | `settings_service`, `SettingsGetTool`, `SettingsSetTool` | IMPLEMENTED | Typed settings validation/cache path exists. |
| Bio/Username state | Profile services and tools | IMPLEMENTED | State reads and mutating tools exist. |
| Arbitrary SQL execution | None | MISSING by design | No AI SQL executor is registered; agents must not add one or touch user Supabase directly. |

### 8.3 Database constraints for future agents

No investigation or execution agent may run SQL against the user’s Supabase project, alter migrations, or introduce an AI SQL tool as a convenience. A schema requirement must be documented and handed to the user for manual application.

---

## 9. RUNTIME / RELIABILITY

### 9.1 Active long-lived tasks

The current runtime intentionally has long-lived asyncio tasks:

- Telethon self-client run loop.
- Optional helper-client run loop.
- FastAPI/Uvicorn server.
- Heartbeat every 30 seconds.
- Keepalive every 60 seconds.
- Failsafe monitor every 15 seconds.
- Diagnostics/task snapshot loop.
- Memory cleanup worker.
- Shared profile scheduler when Bio or Username is active.
- Helper panel/watchdog tasks when helper UI is enabled.

Long-lived `await` points in Telethon/Uvicorn/task-guard stack traces are not by themselves failures. They are expected for these workers.

### 9.2 Current heartbeat behavior

`backend/runtime/heartbeat.py` records structured runtime snapshots and checks real invariants. Current source explicitly handles helper state:

- Self disconnected while READY is a recovery condition.
- Helper disconnected is a recovery condition only when `helper_enabled` is true.
- Disabled helper (`helper_enabled=False`) is valid.
- A quiet account with no incoming updates/callbacks is not automatically treated as a dispatch stall when there is no evidence that updates are arriving.

This contradicts the old investigation’s claim that a disabled helper or idle account necessarily causes reconnect churn. That old claim is obsolete at `767b32b`.

### 9.3 Keepalive and failsafe

Keepalive probes `client.get_me()` under a 15-second wait and requests supervisor recovery on failure. Failsafe checks loop progress, heartbeat, update, and RPC signals. It calls `guarded_create_task()` for hard reset; the current import exists, so the old missing-import `NameError` finding is obsolete.

### 9.4 Dormant paths

**CONFIRMED:** `RuntimeSupervisor._watchdog_loop()` remains defined but is not started by `start()`. It contains additional stale-loop/RPC/helper/memory checks, but current runtime startup starts heartbeat/keepalive/failsafe/diagnostics instead.

**CONFIRMED:** `runtime/startup_check.py::run_startup_checks()` is defined but is not part of the normal `main()`/`RuntimeSupervisor.start()` path.

**CONFIRMED:** `backend/runtime/tg_retry.py::tg_rpc()` is a tested helper with no production caller in the current Telegram API path; active bounded RPC calls use `helper/rpc_timeout.py::rpc_await()` or `operation_watchdog.guarded_await()`.

**CONFIRMED:** `operation_watchdog.bounded_operation` is defined and documented, but the current production code primarily uses `guarded_await`; a source grep does not prove `bounded_operation` is an active protection layer.

These are dormant/duplicated-looking paths, not evidence that the runtime currently runs two supervisors.

### 9.5 Lock and task observations

- `_recovery_lock` serializes recovery transitions.
- Task guards name and supervise long-lived/background tasks.
- AI concurrency is bounded by a semaphore, default 4, with a bounded acquire wait.
- Tool execution is sequential within a turn and capped at five calls.
- Supabase sync calls are moved through `asyncio.to_thread()` and bounded.
- Provider requests use async HTTP and bounded waits.

**LIKELY risk:** the dormant watchdog and duplicated runtime helper modules increase maintenance and observability ambiguity. Re-activating a dormant loop without reconciling ownership would risk a second recovery authority, so this requires a deliberate follow-up rather than an opportunistic change.

### 9.6 Production evidence boundary

The recorded Delete production trace showed the runtime READY and connected while `ToolExecutor` timed out. Current source and tests address the Delete boundary, but no current live trace was available during this investigation. Whether production has additional Telethon cancellation/socket behavior beyond the mocked tests is **UNKNOWN**.

---

## 10. SECURITY

### 10.1 Owner access

Current handlers are outgoing/self-bot handlers and use the centralized owner gate. Helper callbacks also receive owner/context checks through the helper lifecycle path. Non-owner events are intended to be silently ignored.

### 10.2 Tool allowlist

The AI can only call registered tools. Unknown tool names, malformed argument objects, unsupported action names, invalid counts, invalid fields, and unsupported targets do not reach Telegram.

There is no registered arbitrary Telegram method executor, shell executor, file executor, or SQL executor.

### 10.3 Delete security

The Delete security invariant is execution-layer self ownership, not prompt/schema trust. The final gate requires server `out=True` plus sender ID equality with authenticated `get_me()` identity. AI-generated messages are not special-cased as foreign. Unknown ownership fails closed.

This is the strongest confirmed security property in the current AI action layer.

### 10.4 Retrieved-message prompt injection

Telegram message text is passed as data to semantic/history tools and the prompt instructs the model not to follow embedded instructions. This reduces prompt-injection risk, but the model still chooses candidate IDs in the provider semantic path. The final Delete gate prevents foreign-message deletion even if the model is manipulated.

For non-Delete read/write tools, prompt-injection resistance depends more heavily on schemas and service validation. A complete adversarial audit of every tool argument is **UNKNOWN**.

### 10.5 Secrets and identity

Tool contexts contain the Telegram client/facade, owner ID, timezone, chat/reply metadata, and request metadata. Provider keys, StringSession, API hash, Supabase service-role key, and other environment secrets are not intentionally injected into prompts or tool results. The account identity tool allowlist excludes phone/session/credential data.

No secrets were read or logged during this investigation.

---

## 11. CONFIRMED FACTS

1. Current HEAD is `767b32b` on `main`.
2. The backend source tree is present and contains the Telethon/FastAPI/LifeOS implementation described by current source.
3. `backend.main` is the backend entry point.
4. `RuntimeSupervisor` owns the active runtime lifecycle and recovery transitions.
5. `bot.router.register_all()` wires runtime hooks, Glass UI handlers, AI settings, and `ai_unified`.
6. AI requests carry immutable chat/message/request context through `AIRequest`.
7. The original Telegram request ID is captured before provider/config/status work.
8. Provider output reaches a fixed ToolRegistry/ToolExecutor boundary before side effects.
9. The provider manager uses capability filtering, scoring, bounded retry, cooldown, and quarantine.
10. `dummy` is always registered as the terminal provider fallback.
11. Delete has a final self-only verification chokepoint.
12. Delete history scans are capped at 1,000 messages in the service selector.
13. Delete verification/deletion is chunked at 100 IDs.
14. Delete per-RPC work is guarded by a 5-second RPC timeout in the hardened service path.
15. Delete has a 25-second operation deadline and a 30-second tool timeout.
16. Current scoped Delete can include the active request when it is within scope.
17. “Until this message” can use the original request as the boundary.
18. Pure successful Delete does not send a replacement Telegram confirmation.
19. AI-generated outgoing messages are eligible by actual Telegram ownership metadata.
20. Direct semantic topic Delete can use a local self-only text filter.
21. Provider-backed semantic Delete uses a bounded recent-message tool and final ID re-validation.
22. Deep Save downloads and re-uploads a new Saved Messages message; Save does not use forwarding.
23. Retrieval may use `forward_messages()` to resend a saved asset; this is not Forward Save.
24. Bio and Username use one parameterized ProfileEngine and one shared scheduler.
25. Username engine writes Telegram `first_name`, not the real `@username` field.
26. Supabase access has in-memory fallback and bounded threaded sync calls.
27. AI management does not include arbitrary SQL execution.
28. The current heartbeat treats disabled helper and naturally quiet accounts more carefully than the old investigation claimed.
29. The current failsafe imports `guarded_create_task`; the old missing-import claim is obsolete.
30. `RuntimeSupervisor._watchdog_loop()` remains defined but is not started by `start()`.
31. Recorded current test evidence is 572 repository tests passing, including the 53 new semantic-Delete tests in `tests/test_32_semantic_delete.py`.
32. Tests are mocked/unit-level and do not prove live Telegram behavior.
33. `backend/ai/semantic_delete.py` provides deterministic Persian/English normalization, tokenization, exact N-word / N-English-word predicate parsing, and a pure matcher — with no embeddings, vector index, external service, or provider dependency.
34. “دو کلمه‌ای” and equivalent English forms are parsed as word-count predicates, never as positional deletion counts.
35. Structural predicates compose with existing count/time/boundary scopes and execute through the provider-independent local fast path.
36. The Delete service selector matches topic queries on the normalized text form and applies a pure semantic matcher only after self ownership.
37. `delete_verified_self_messages()` remains the single final deletion authority for the semantic path; the current request and AI-generated self messages stay eligible while foreign/unknown messages are rejected.
38. A hanging history page on the deterministic semantic path returns the same controlled bounded timeout as the hardened Delete service (verified by test).

---

## 12. LIKELY FINDINGS / RISKS

1. **CONFIRMED — deterministic structural semantic Delete is implemented.** The remaining semantic gap is now provider-dependent ambiguity (unstructured requests) and retrieval depth, not the two-English-word class of requests.
2. **LIKELY — unstructured semantic requests can still be provider-sensitive.** Requests without a topic marker or structural predicate still rely on a tool-capable provider to inspect bounded recent previews and choose IDs.
3. **LIKELY — `ListRecentMessagesTool` still has weaker per-page RPC protection than the hardened Delete service selector.** It applies a message limit, but its direct `client.iter_messages()` loop does not reuse `_iter_messages_bounded()`/`rpc_await()`; this is the recommended next task.
4. **LIKELY — legacy Delete-by-ID/panel paths do not share every scoped current-request semantic rule.** They still converge on ownership verification, but their candidate range and request inclusion behavior differ.
5. **LIKELY — the word-count semantics are a project-defined contract, not universal Persian lexicography.** ZWNJ is treated as a separator for counting (standard segmentation), and mixed-script tokens are classified conservatively; a user expecting different boundaries could disagree on edge cases.
6. **LIKELY — dormant watchdog/recovery code increases future regression risk.** It is not currently a second active supervisor, but it creates ambiguity about which checks are authoritative.
7. **LIKELY — documentation drift will mislead future agents.** `remote_readme.md` is clearly older; parts of `README.md` still retain legacy command/feature text even though `AGENTS.md` and current handlers describe Glass UI/natural-language operation.
8. **LIKELY — Supabase fallback can silently lose AI/panel persistence across process restarts.** This is intentional graceful degradation but should be visible in operational status.
9. **LIKELY — a successful Telegram upload followed by DB failure can leave an asset without a complete saved-items row.** The Save service reports this state, but reconciliation is a management concern.

---

## 13. UNKNOWN / MISSING EVIDENCE

1. Whether every configured production provider currently has a valid key, quota, and tool-capable model.
2. Whether every production model override resolves to a live provider model after the latest external provider changes.
3. Whether real Telegram cancellation always interrupts a stuck Telethon operation within the local `wait_for` deadline.
4. Whether production semantic Delete failures are parser, retrieval, provider, or tool-execution failures without a request-specific trace.
5. ~~Whether the user’s actual two-English-word requirement means exactly two tokens, two-word substring, language detection, or a broader semantic category.~~ **RESOLVED by definition:** the implemented contract is exactly N lexical tokens of the requested script after deterministic normalization (see §5.6). Still UNKNOWN: whether every colloquial Persian spelling variant the user produces lands inside the tokenizer’s coverage.
6. Whether every documented Supabase AI migration has been applied to the user’s live project.
7. Whether the dormant `_watchdog_loop` is intentionally retired or was accidentally omitted from startup.
8. Whether legacy Glass UI Delete-by-ID paths should include the current request under every requested range semantics.
9. Whether all helper callback authorization paths have been exercised against non-owner callback payloads in a live helper bot.
10. Whether live Delete batching stays within Telegram FloodWait limits for the user’s actual chat volume.
11. No live Telegram, Render, or Supabase environment test was run during this investigation.

---

## 14. EXACT FILES

### Runtime and entry

- `backend/main.py`
- `backend/config.py`
- `backend/bot/client.py`
- `backend/bot/router.py`
- `backend/runtime/supervisor.py`
- `backend/runtime/heartbeat.py`
- `backend/runtime/keepalive.py`
- `backend/runtime/failsafe.py`
- `backend/runtime/task_guard.py`
- `backend/runtime/operation_watchdog.py`
- `backend/runtime/tg_retry.py`
- `backend/runtime/diagnostics.py`
- `backend/runtime/tracer.py`
- `backend/health.py`
- `backend/helper/rpc_timeout.py`

### AI core

- `backend/ai/session/request.py`
- `backend/ai/engine/engine.py`
- `backend/ai/engine/dispatcher.py`
- `backend/ai/engine/result.py`
- `backend/ai/actions.py`
- `backend/ai/tools/base.py`
- `backend/ai/tools/context.py`
- `backend/ai/tools/registry.py`
- `backend/ai/tools/executor.py`
- `backend/ai/tools/delete.py`
- `backend/ai/tools/semantic.py`
- `backend/ai/tools/save.py`
- `backend/ai/tools/retrieve.py`
- `backend/ai/tools/database.py`
- `backend/ai/tools/settings.py`
- `backend/ai/tools/account.py`
- `backend/ai/tools/bio.py`
- `backend/ai/tools/username.py`
- `backend/ai/tools/organize.py`
- `backend/ai/tools/delivery.py`
- `backend/ai/prompt/template.py`
- `backend/ai/prompt/builder.py`
- `backend/ai/prompt/serializer.py`
- `backend/ai/prompt/formatter.py`
- `backend/ai/prompt/validator.py`
- `backend/ai/prompt/budget.py`
- `backend/ai/conversation/context_builder.py`
- `backend/ai/conversation/history.py`
- `backend/ai/conversation/session.py`
- `backend/ai/runtime/manager.py`
- `backend/ai/runtime/session.py`
- `backend/ai/memory/manager.py`

### AI providers

- `backend/ai/providers/factory.py`
- `backend/ai/providers/registry/registry.py`
- `backend/ai/providers/manager/manager.py`
- `backend/ai/providers/manager/health.py`
- `backend/ai/providers/manager/metrics.py`
- `backend/ai/providers/manager/config_manager.py`
- `backend/ai/providers/base/contract.py`
- `backend/ai/providers/base/capabilities.py`
- `backend/ai/providers/base/config.py`
- `backend/ai/providers/base/exceptions.py`
- `backend/ai/providers/openai_compat.py`
- `backend/ai/providers/openai.py`
- `backend/ai/providers/gemini.py`
- `backend/ai/providers/groq.py`
- `backend/ai/providers/openrouter.py`
- `backend/ai/providers/cerebras.py`
- `backend/ai/providers/mistral.py`
- `backend/ai/providers/zai.py`
- `backend/ai/providers/sambanova.py`
- `backend/ai/providers/nvidia.py`
- `backend/ai/providers/cohere.py`
- `backend/ai/providers/siliconflow.py`
- `backend/ai/providers/fireworks.py`
- `backend/ai/providers/dummy/provider.py`
- `backend/ai/config/env.py`
- `backend/ai/config/defaults.py`
- `backend/ai/config/manager.py`
- `backend/ai/config/validation.py`
- `backend/ai/config_store.py`

### Handlers and Telegram facade

- `backend/bot/handlers/guard.py`
- `backend/bot/handlers/ai_unified.py`
- `backend/bot/handlers/ai.py`
- `backend/bot/handlers/delete.py`
- `backend/bot/handlers/save.py`
- `backend/bot/handlers/retrieve.py`
- `backend/bot/handlers/discover.py`
- `backend/bot/handlers/database.py`
- `backend/bot/handlers/bio.py`
- `backend/bot/handlers/username.py`
- `backend/telegram_api/api.py`
- `backend/telegram_api/messages.py`
- `backend/telegram_api/media.py`
- `backend/telegram_api/profile.py`
- `backend/telegram_api/entities.py`

### Services/profile/database

- `backend/services/delete_service.py`
- `backend/services/save_service.py`
- `backend/services/retrieve_service.py`
- `backend/services/discover_service.py`
- `backend/services/database_service.py`
- `backend/services/settings_service.py`
- `backend/services/organize_service.py`
- `backend/services/bio_service.py`
- `backend/services/username_service.py`
- `backend/profile/engine.py`
- `backend/profile/scheduler.py`
- `backend/bio/engine.py`
- `backend/username/engine.py`
- `backend/db/client.py`
- `backend/ai/persistence.py`
- `backend/ai/database/manager.py`

### Tests

- `tests/test_17_providers.py`
- `tests/test_18_ai_execution_agent.py`
- `tests/test_19_ai_actions.py`
- `tests/test_20_advanced_execution.py`
- `tests/test_21_execution_status.py`
- `tests/test_23_provider_mesh.py`
- `tests/test_24_execution_reliability.py`
- `tests/test_25_fast_path.py`
- `tests/test_26_silent_delete.py`
- `tests/test_27_delete_ownership.py`
- `tests/test_28_delete_regression.py`
- `tests/test_29_delete_expansion.py`
- `tests/test_30_delete_timeout_hardening.py`
- `tests/test_31_delete_rpc_failures.py`
- `tests/test_12_save_engine.py`
- `tests/test_15_bio_username.py`
- `tests/test_03_database_consistency.py`
- `tests/test_11_runtime_wiring.py`
- `tests/test_22_reliability.py`

### Documentation

- `AGENTS.md`
- `README.md`
- `remote_readme.md`
- `AI_MASTER_DESIGN.md`
- `DATABASE_ARCHITECTURE.md`
- `OBSERVABILITY.md`
- `PRODUCTION_CHECKLIST.md`
- `PRODUCTION_VERIFICATION.md`

---

## 15. EXACT FUNCTIONS / CLASSES

### Runtime

- `main()`
- `config.load()`
- `build_client()`
- `RuntimeSupervisor.start()`
- `RuntimeSupervisor._build_and_register()`
- `RuntimeSupervisor._run_loop()`
- `RuntimeSupervisor._trigger_reconnect()`
- `RuntimeSupervisor._trigger_full_recovery()`
- `RuntimeSupervisor._hard_reset_runtime()`
- `RuntimeSupervisor._watchdog_loop()`
- `RuntimeSupervisor.stop()`
- `_heartbeat_loop()`
- `_keepalive_loop()`
- `_failsafe_loop()`
- `guarded_create_task()`
- `immortal_create_task()`
- `guarded_await()`
- `rpc_await()`

### AI and intent

- `AIRequest`
- `Engine.execute()`
- `Dispatcher.execute()`
- `Dispatcher._try_local_fast_path()`
- `Dispatcher._apply_structured_action()`
- `Dispatcher._build_context()`
- `Dispatcher._build_tool_context()`
- `Dispatcher._build_tool_definitions()`
- `Dispatcher._build_continuation_messages()`
- `parse_command_intent()`
- `validate_action()`
- `parse_action_text()`
- `resolve_tool_calls()`
- `ToolRegistry.register()`
- `create_default_registry()`
- `ToolExecutor.execute_calls()`
- `ToolExecutor._execute_single()`
- `deliver_response()`
- `ai_unified.register()`
- `ai_unified._execute_ai()`
- `ai_unified._is_silent_delete()`

### Providers

- `ProviderFactory.create_registry()`
- `ProviderFactory.create_manager()`
- `ProviderRegistry.register()`
- `ProviderRegistry.switch_provider()`
- `ProviderManager.chat()`
- `ProviderManager._ordered_candidates()`
- `ProviderManager._skip_reason()`
- `ProviderManager._score()`
- `ProviderManager._call_once()`
- `ProviderManager._attempt_with_retry()`
- `ProviderManager._apply_failure()`
- `ProviderHealthTracker.state()`
- `ProviderHealthTracker.record_failure()`
- `ProviderHealthTracker.record_success()`

### Delete

- `DeleteTool.execute()`
- `DeleteRepliedTool.execute()`
- `DeleteByIdTool.execute()`
- `DeleteMessageByIdTool.execute()`
- `ListRecentMessagesTool.execute()`
- `DeleteMessagesByIdsTool.execute()`
- `_telegram_rpc()`
- `_iter_messages_bounded()`
- `_resolve_me_id()`
- `_is_self_owned()`
- `delete_verified_self_messages()`
- `_parse_cutoff()`
- `select_self_owned_message_ids()`
- `do_del_self_filtered()`
- `do_del_self_last_n()`
- `do_del_n_counts()`
- `do_del_last_n_real()`
- `do_del_id_counts()`

### Save/profile/database

- `execute_save()`
- `execute_link_save()`
- `get_next_save_code()`
- `ProfileEngine`
- `ProfileEngine.render()`
- `ProfileEngine.updater()`
- `ProfileEngine.start_cron()`
- `ProfileEngine.stop_cron()`
- `profile.scheduler._cron_loop()`
- `profile.scheduler.stop_if_idle()`
- `database_service.find_orphans()`
- `database_service.do_clean()`
- `database_service.do_stats()`
- `database_service.do_vacuum()`
- `discover_service.do_list()`
- `discover_service.do_find()`
- `retrieve_service.do_preview()`
- `retrieve_service.do_retrieve()`

---

## 16. EXECUTION PATH DIAGRAMS

### 16.1 Normal deterministic AI action

```text
Telegram outgoing message
  → ai_unified trigger/reply decision
  → capture event.chat_id + event.message.id
  → AIRequest
  → Dispatcher conversation/context setup
  → parse_command_intent(original text)
  → local fast path when high-confidence
  → ToolExecutor
  → registered Tool.execute()
  → service layer
  → TelegramAPI/client or DB
  → ToolResult
  → EngineResult
  → edit-in-place or silent delete
```

### 16.2 Provider-native action

```text
AIRequest
  → prompt builder + tool schemas
  → ProviderManager capability-aware routing
  → provider chat/function call
  → normalized tool call
  → ToolExecutor validation/permission/timeout
  → service/tool execution
  → continuation provider round only when needed
  → real result summary
  → delivery
```

### 16.3 Delete scope

```text
Delete natural language
  → parser/provider structured action
  → DeleteTool(mode/count/time/boundary/query)
  → bounded real Telegram history
  → self-owned candidate selection
  → final re-fetch per candidate batch
  → out=True + sender_id == authenticated self ID
  → delete_messages in batches of 100
  → ToolResult
  → no visible success message
```

### 16.4 Semantic Delete

```text
semantic text request
  → direct local query OR provider semantic path
  → bounded current-chat message window
  → content/relevance interpretation
  → concrete IDs
  → final ownership re-fetch
  → self-only delete
```

### 16.5 Save

```text
reply/source message
  → Deep Save service
  → download media if present
  → validate local file
  → send_file as NEW Saved Messages message
  → extract uploaded metadata
  → insert saved_items
  → honest result/edit
```

### 16.6 Runtime startup/recovery

```text
main
  → config/crash hooks
  → RuntimeSupervisor.start
  → build/register self client
  → optional helper
  → profile resume
  → web server
  → heartbeat/keepalive/failsafe/diagnostics
  → supervised Telethon run loop
  → shutdown or bounded recovery ladder
```

---

## 17. COMPLETED WORK

The following is genuinely implemented in current source and supported by current tests/source inspection:

- Telethon StringSession self-client startup and owner gating.
- Glass UI panel/action/input architecture.
- Optional helper bot with fallback behavior.
- Deep Save download/re-upload pipeline.
- Save-by-link routing through Deep Save.
- Supabase-or-memory core persistence.
- Shared Bio/First Name ProfileEngine and scheduler.
- Natural-language AI trigger/reply activation.
- Immutable request context with original Telegram message ID.
- Provider abstraction with capability-aware routing and fallback health state.
- Native tool registry and controlled ToolExecutor.
- Deterministic Persian/English action parsing for supported scopes.
- Self-only Delete enforcement at final execution boundary.
- AI-generated outgoing message eligibility for Delete.
- Delete all/time/boundary/last-N/filter scope foundation.
- Delete history/RPC bounds and chunked deletion.
- Bounded Delete partial-failure result handling.
- Silent successful pure Delete delivery.
- Saved-item list/search/status tools.
- Database status and profile status tools.
- Account identity field allowlist and first-name/real-username distinction.
- Structured AI/provider/runtime diagnostics.
- Focused Delete/provider/execution regression test coverage.
- Deterministic Persian/English text normalization and tokenization for Delete matching (`backend/ai/semantic_delete.py`).
- Exact N-word / exact N-English-word structural predicates parsed deterministically from natural Persian/English requests, including digit and number-word forms and ZWNJ variants.
- Word-count numbers can no longer be misread as positional deletion counts (the previous “delete 2 messages” hijack of “پیام‌های دو کلمه‌ای” is fixed).
- Semantic predicates compose with existing count/time/boundary scopes through the existing self-only bounded selector.
- Topic queries are matched on the normalized text form (script variants/digits/zero-width characters no longer defeat exact content matches).
- Provider-independent structural Delete execution through the local fast path.
- Controlled bounded failure for a hanging history page on the semantic path (same guarantee as the hardened Delete service).

---

## 18. REMAINING WORK

### Highest-impact behavior

- **DONE (this work):** deterministic semantic Delete for structural predicates — multilingual normalization, exact token/word-count predicates, normalized topic matching, and composition with the existing bounded count/time/boundary scopes.
- Strengthen `ListRecentMessagesTool` RPC/time bounds to match the hardened Delete service path (recommended next task).
- Audit every legacy Delete UI/ID path for consistent request-anchor/current-request semantics.
- Decide whether additional unstructured semantic classes (no topic marker, no structural predicate) should gain deterministic local handling or stay on the bounded provider fallback.

### Reliability

- Decide the status of the dormant `RuntimeSupervisor._watchdog_loop()` and either formally retire/document it or deliberately integrate its checks through the single supervisor authority.
- Add runtime integration tests for actual startup task inventory, recovery lock behavior, and monitor lifecycle.
- Validate live Telegram RPC cancellation/FloodWait behavior under safe staging conditions.

### Providers

- Validate configured production provider/model combinations using the existing model-test/status surface.
- Keep provider-specific capability and model defaults synchronized with live upstream availability.
- Add production-like tests for provider matrix behavior when all tool-capable providers are unavailable.

### Persistence/management

- Document the exact migration status of AI tables without executing database changes.
- Improve visibility of in-memory fallback and upload-with-DB-failure states.
- Decide which service-level management operations should become AI tools; do not expose SQL.

### Documentation

- Reconcile stale command/feature sections in `README.md` and `remote_readme.md` with current source.
- Keep `AGENTS.md`, README, and investigation reports aligned when architecture changes.

### Future profile automation

- No implementation now. Future work must define how dynamic mood/text changes preserve the existing Bio template and how Username/First Name changes preserve the immutable text component.

---

## 19. RECOMMENDED NEXT TASK

### NEXT TASK (completed by this work): Finish bounded semantic Delete resolution for real Telegram history

The semantic-Delete slice defined by the previous investigation is **implemented and tested**:

1. Supported semantic predicates are now defined precisely — normalized topic matching and “exactly N words / exactly N English words.”
2. Retrieval stays inside the existing RPC-bounded, 1,000-message-capped selector.
3. Persian/English text is normalized and tokenized deterministically before matching (`backend/ai/semantic_delete.py`).
4. Predicates apply only to messages in the current chat.
5. Self ownership is applied before any text predicate; `delete_verified_self_messages()` remains the final authority.
6. Current-request ID/anchor semantics are preserved (request remains eligible in scope; never derived from an AI response).
7. Ambiguous requests return controlled clarification; hanging history returns a controlled bounded failure.

### NEXT TASK: Harden the provider-backed semantic read path and unify legacy Delete anchor semantics

1. Give `ListRecentMessagesTool` per-page `rpc_await()`/timeout guards matching the Delete service selector (currently bounded by its message limit and the executor tool deadline, but not per-page).
2. Audit legacy Delete-by-ID/panel paths (`DeleteByIdTool`, `do_del_id_counts`, Glass UI delete actions) so every Delete entry point shares identical current-request inclusion and boundary semantics.
3. Decide whether additional unstructured semantic classes should gain deterministic local handling.

This should happen before profile automation or additional management features. It must not reactivate `_watchdog_loop()` incidentally; runtime monitor reconciliation remains a separate, explicitly scoped reliability task.

---

## 20. VALIDATION PLAN

For the recommended semantic Delete task:

### Parser/action tests

- Persian topic Delete with informal spelling and mixed Persian/English.
- English topic Delete.
- Exact two-word and exact N-word predicates (Persian/English, digits/number words, ZWNJ variants, compound numbers).
- Time + topic / N + topic / boundary + topic combinations.
- Count + structural predicate, time + structural predicate, boundary + structural predicate.
- Unsupported/ambiguous wording returns controlled clarification rather than guessed deletion.
- Word-count numbers are never misread as positional deletion counts.

### Retrieval/context tests

- Current chat ID is used; another chat is never searched.
- Retrieval is capped (selector limit 1,000; `ListRecentMessagesTool` ≤ 100).
- Every history page/RPC on the deterministic path has a timeout.
- Current request ID is captured before any provider/status edit.
- Provider output cannot replace the original anchor.
- Message previews are bounded and do not expose unnecessary content in logs.

### Security tests

- Self-authored manual message selected.
- AI-authored self message selected.
- Foreign user message excluded (even when its text matches the predicate).
- Unknown sender/account identity fails closed.
- Model-supplied foreign ID is rejected by final verification.
- Mixed candidate set reaches Telegram deletion only with verified self IDs.

### Execution/reliability tests

- Normal semantic Delete completes under the bounded tool deadline.
- Hanging history page returns a controlled timeout.
- Provider unavailable does not block deterministic semantic predicates (fast path, zero provider calls).
- Invalid/malformed semantic predicates fail closed with a controlled result.
- Large candidate sets are chunked and do not create untracked background tasks.
- Repeated semantic Delete requests do not duplicate deletion.

### Silent delivery tests

- Successful pure Delete produces no Telegram reply/send.
- Deleting the request message does not create a replacement “Deleted” message.
- Failed Delete remains visible as an edited controlled error.

### Executed validation (this work)

All commands below passed:

```text
.venv/bin/python -m pytest tests/test_19_ai_actions.py -q            → 38 passed
.venv/bin/python -m pytest tests/test_25_fast_path.py -q             → 5 passed
.venv/bin/python -m pytest tests/test_26_silent_delete.py tests/test_27_delete_ownership.py tests/test_28_delete_regression.py tests/test_29_delete_expansion.py tests/test_30_delete_timeout_hardening.py tests/test_31_delete_rpc_failures.py tests/test_32_semantic_delete.py -q  → 137 passed
.venv/bin/python -m pytest tests/test_12_save_engine.py tests/test_15_bio_username.py tests/test_03_database_consistency.py -q → 54 passed
.venv/bin/python -m pytest tests/ -q                                → 572 passed
```

Source-level checks:

- `git diff --check` → clean (no whitespace errors).
- Only intended Delete/semantic files changed: `backend/ai/actions.py`, `backend/ai/tools/delete.py`, `backend/services/delete_service.py`, new `backend/ai/semantic_delete.py`, new `tests/test_32_semantic_delete.py`.
- No secrets present in the diff.
- No SQL/Supabase mutations introduced.
- No unbounded destructive history iteration; no new background deletion tasks.
- `HEAD`/`origin/main` push only after explicit delivery.

Live verification remains separate and must use a controlled test chat with known self/foreign messages. Mocked tests must never be reported as proof of live Telegram reliability.

---

## 21. HANDOFF FOR EXECUTION AGENT

### Known root causes / current state

- The historical Delete timeout came from unbounded or insufficiently guarded Telegram history/RPC work running inside the generic tool deadline. Current code adds service-level RPC guards, scan/batch bounds, a Delete-specific operation deadline, and partial-failure handling. Do not restore the old unbounded path.
- Current scoped Delete can include the active request message when it is inside the requested range. Do not reintroduce a blanket request exclusion.
- “Until this message” must use the original request ID captured before provider/status work. Do not derive the anchor from an AI response.
- Self-only deletion is enforced in `delete_verified_self_messages()`. Never move security into prompt text, parser-only checks, or a generic permission layer.
- AI-generated messages are normal self-account outgoing Telegram messages and must remain eligible.
- **DONE (this work):** deterministic structural semantic Delete — exact N-word / N-English-word predicates and normalized topic matching — now executes locally with no provider round-trip, through the same bounded self-only selector and final chokepoint.
- Ambiguous/unstructured semantic requests still use bounded history plus provider-selected concrete IDs; that provider read path is the next hardening target.

### Exact implementation surface (this work)

Implemented files:

- `backend/ai/semantic_delete.py` (NEW)
  - `normalize_text()` / `tokenize()` / `count_words()` — deterministic Persian/English normalization and lexical word counting.
  - `StructuralPredicate`, `parse_structural_predicate()`, `spec_from_dict()`, `build_matcher()` / `build_matcher_from_dict()` — pure parsing + matching, no I/O.
- `backend/ai/actions.py`
  - `parse_command_intent()` — structural-predicate branch; word-count numbers are excluded from positional count extraction (`_is_word_count_marker` guard in `_extract_count()`).
  - `validate_action()` / `resolve_tool_calls()` / `parse_action_text()` — `semantic` field validation and forwarding (delete_messages only; malformed → `KIND_INVALID`).
- `backend/ai/tools/delete.py`
  - `DeleteTool` — new `semantic` argument, schema, validation (invalid → controlled failure), and matcher wired into `do_del_self_filtered(match=...)`.
- `backend/services/delete_service.py`
  - `select_self_owned_message_ids()` / `do_del_self_filtered()` — new `match` callable; topic queries matched on the normalized text form.

Unchanged by design (verified, not modified): `backend/ai/engine/dispatcher.py`, `backend/ai/tools/semantic.py`, `backend/bot/handlers/ai_unified.py`, `delete_verified_self_messages()`, Save/Bio/Username/Profile/UI/runtime-supervisor code.

Tests:

- `tests/test_32_semantic_delete.py` (NEW, 53 tests) — normalization/tokenization, predicate parsing, matcher semantics, parser integration, structured JSON validation, DeleteTool execution, ownership/foreign/identity rejection, bounded retrieval and RPC timeout, provider-independent fast path, silent success, repeated-execution safety.
- Existing focused Delete/action tests were NOT weakened; all still pass unchanged.

### Desired behavior (now implemented)

```text
natural Persian/English request
  → deterministic structured Delete intent where possible (topic and/or exact N-word)
  → bounded current-chat retrieval (1,000-cap, per-RPC timeout)
  → normalized semantic/content predicate
  → self-owned filter
  → final Telegram re-fetch and ownership verification
  → bounded chunked deletion
  → honest ToolResult
  → silent success
```

Unsupported/ambiguous structures return a safe controlled result or clarification. Never guess a broad deletion range.

### Constraints (still binding for follow-up work)

- Do not modify Save, Bio, Username, provider routing, fallback architecture, Supabase schema, UI, buttons, or runtime supervisor as part of semantic Delete follow-ups.
- Do not add SQL or execute Supabase operations.
- Do not add arbitrary Telegram method execution.
- Do not weaken self-only deletion.
- Do not create background deletion tasks that outlive the request.
- Do not use unbounded `iter_messages()` for destructive selection.
- Do not log message contents, session strings, phone numbers, API keys, or database secrets.
- Preserve silent successful Delete behavior.

### Required validation (executed)

- Focused semantic/Delete tests first: 53 new tests in `tests/test_32_semantic_delete.py` → 53 passed.
- Existing Delete ownership, timeout, batching, current-request, and silent tests → all passed unchanged.
- Relevant AI/provider/fast-path tests → passed.
- Save/Bio/Username/Database regression tests → passed.
- Full `tests/` suite → 572 passed.
- `git diff --check` → clean; Delete-only diff review confirmed no secrets, no SQL, no unbounded iteration, no background tasks.
- Commit and push only when explicitly requested by the delivery task.

### Final handoff conclusion

The current system has a real execution boundary and a strong Delete ownership chokepoint. The previously recommended semantic-Delete slice is now implemented and verified by source + mocked tests (deterministic Persian/English normalization, exact N-word / N-English-word predicates, normalized topic matching, composition with count/time/boundary scopes — all through the existing bounded self-only selector and delete_verified_self_messages() chokepoint, with silent success and controlled failure preserved). The next implementation should harden the provider-backed semantic read path (ListRecentMessagesTool) and reconcile legacy Delete anchor semantics while preserving those boundaries. Do not repeat the broad architecture investigation unless new source changes invalidate this report.
