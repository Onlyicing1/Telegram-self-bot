# IMPLEMENTATION REPORT

## 1. IMPLEMENTATION METADATA

| Field | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Branch | `main` |
| Confirmation round-trip commit | `c5d29f7` (`feat: add AI confirmation round-trip for owner-only tools`) |
| Model/provider runtime-switch commit | `c5d29f7` + `8ac3779` (`fix: apply AI model and provider changes at runtime`) |
| State-consistency commit | `fix: make AI provider/model state consistent end-to-end` (chunk 3) |
| Menu-vs-runtime consistency commit | `fix: stop AI menu from displaying unappliable provider/model` (chunk 4) |
| Web provider-switch model commit | `fix: persist the provider default model on web provider switch` (chunk 5, this task) |
| Implementation date | 2026-09-04/05 |
| Task/chunks | (1) AI confirmation round-trip for ADMIN_ONLY / CONFIRMATION_REQUIRED tools; (2) fix AI model/provider runtime switching; (3) make AI provider/model state consistent end-to-end; (4) fix the AI menu displaying a provider/model the runtime does not serve; (5) make the web `/api/ai/provider` writer persist provider + default model atomically so the persisted model can never go stale after a provider change |
| Work type | implementation |
| Final implementation status | **COMPLETE** — full test suite (`1695 passed, 23 skipped`), compile check, and diff hygiene pass; **live Telegram / live Supabase verification NOT performed** (no credentials/runtime in this workspace); **no database schema change, no migration** |

This is the single current-state report. It records only behavior and
validation established from the current source, the committed diffs
(`c5d29f7`, `8ac3779`, the state-consistency commit, the chunk-4
menu-vs-runtime commit, and the chunk-5 web-writer commit), and commands
actually run.

### Implementation summary

- **Chunk 1 — confirmation round-trip (committed `c5d29f7`):** an interactive
  owner-confirmation round-trip that lets ADMIN_ONLY /
  CONFIRMATION_REQUIRED registered tools complete through the AI execution
  path after explicit owner approval. Before it, `settings_set` (ADMIN_ONLY)
  was the only registered AI tool that could be selected but never executed —
  `ToolExecutor` correctly returned `needs_confirmation` and the Dispatcher
  had no way to consume an owner approval and re-issue the call.
- **Chunk 2 — model/provider runtime switching (committed `8ac3779`):**
  fixes the confirmed user-facing bug that the AI could receive and confirm
  a model/provider change request but the ACTIVE model never changed.
  `SettingsSetTool` was writing provider/model into `settings_service`
  (panel_settings) — a store the AI runtime never reads — so the change
  "succeeded" (settings_get showed it) yet the next AI request was still
  served by the old provider/model.
- **Chunk 3 — end-to-end provider/model state consistency (this task):**
  the live bot still showed divergent state — the AI menu displayed one
  model/provider while the AI's own runtime context claimed
  `provider=dummy` and settings_get reported yet another value. Root causes
  (source-proven): (a) `RuntimeSession.active_provider` defaults to
  `"dummy"` and `ConversationManager.set_provider` had ZERO production
  callers, so the prompt's [Runtime Context] block was rendered from a
  never-updated session value; (b) the persisted `ai_config` was applied to
  the runtime only on the first chat request — at boot the engine's active
  provider stayed at the ENV default (`AI_PROVIDER` or `dummy`), so
  engine-read surfaces (AI menu state, health) disagreed with config-read
  surfaces (menu identity, settings_get); (c) `temperature`/`max_tokens`
  persisted in `config_store` never reached the active provider's runtime
  config — the object the provider reads at request time. Fix: ONE shared
  restore (`engine.apply_persisted_config`) applied at boot AND before every
  chat request, a runtime context built from the ProviderManager (the single
  authoritative runtime state), and session sync wired for the first time.
- **Chunk 4 — AI menu must never display a provider/model the runtime does
  not serve (this task):** live bug — the Self Bot AI menu showed
  `cohere / command-a-plus-05-2026` while the AI's runtime context and the
  actually-served requests used `groq / gpt-oss-20b`. Source-proven root
  cause: the persisted `ai_config` can reference a provider that is NOT
  registered in this process's `ProviderManager` (no API key in ENV), and
  `ProviderManager.apply_selection` returns `False` without switching for
  unregistered providers — a failure every writer silently swallowed. The
  menu/health panels displayed the persisted pair first, so they showed an
  unappliable phantom while the AI truthfully reported the ProviderManager
  pair. Fix: (a) `apply_persisted_config` now HEALS a phantom persisted
  pair to the ACTIVE runtime pair; (b) `settings_set` and the web
  `/api/ai/provider` endpoint refuse to persist a provider that is not
  registered; (c) the AI menu + health panels display the effective
  ProviderManager pair (persisted config only as fallback when the engine
  has no info).
- **Chunk 5 — web provider switch must not leave the persisted model stale
  (this task):** `POST /api/ai/provider` (the dashboard writer) was the only
  provider-change writer that did NOT persist the new provider's default
  model: `settings_set` and the glass provider action persist provider +
  default model atomically, while the web endpoint called
  `config_store.update_provider(owner_id, provider)` with no model — the OLD
  provider's model stayed in `ai_config` even though the runtime applied
  (and the endpoint response claimed) the new default. The next per-request
  `apply_persisted_config` then re-applied that stale persisted model onto
  the runtime, flipping the served model back to one the new provider may
  not offer — the same persisted-vs-runtime divergence class as chunks 3–4.
  Fix: the web endpoint now persists provider + default model in ONE atomic
  write (`update_provider(owner_id, provider, default_model)`), identical
  to the other writers, so persistence, runtime, and the per-request
  restore can never disagree after a dashboard provider change.
- **Current status by label:**
  - **IMPLEMENTED** — source present in `c5d29f7` + `8ac3779` + chunks 3–5.
  - **TESTED** — focused suites `tests/test_confirmation_roundtrip.py` (63
    tests), `tests/test_settings_runtime_switch.py` (15 tests),
    `tests/test_ai_state_consistency.py` (5 tests), and
    `tests/test_ai_menu_state_consistency.py` (7 tests) plus the full suite
    (`1695 passed, 23 skipped`).
  - **INTEGRATED** — proven in-process through the real
    provider → registry → ToolExecutor path (service boundary faked where
    the suite convention requires it) and, for the runtime switch, through
    the REAL Engine → Dispatcher → ProviderManager path with scripted
    providers.
  - **LIVE VERIFIED** — **NOT PERFORMED** (no Telegram/Supabase runtime in
    this workspace).
  - **UNVERIFIED** — everything that requires a live environment (see §12).

---

## 2. OBJECTIVE

The preceding AI Tool Connectivity investigation (`INVESTIGATION.md`)
established, from current source:

- 36 registered AI tools.
- 35 classified `REAL_CONNECTED`.
- `settings_set` was the single `PROVIDER_REACHABLE_BUT_NOT_EXECUTABLE`
  tool: `SettingsSetTool.permission_level == PermissionLevel.ADMIN_ONLY`,
  `ToolExecutor` returned `needs_confirmation`, and the Dispatcher had no
  confirmation round-trip / re-issue mechanism.

Chunk 1 goal: add the smallest production-safe, **generic** confirmation
round-trip (not a `settings_set`-specific special case) while preserving the
execution architecture:

```
owner request → AI interpretation / tool selection
  → ToolRegistry
  → ToolExecutor            (sole execution authority)
  → needs_confirmation
  → explicit owner confirmation
  → validated re-issue through ToolExecutor
  → ToolResult
  → AI response
```

Chunk 2 goal (this task): fix the confirmed user-facing bug — the AI can
receive and confirm a model/provider change request, but the active model
does NOT actually change. The system appeared to succeed before the final
step: settings_set → persisted → runtime configuration → ProviderManager →
next AI request.

---

## 3. ROOT CAUSE (source-proven)

### 3.1 Confirmation round-trip gap (fixed by `c5d29f7`)

1. `SettingsSetTool.permission_level` is `PermissionLevel.ADMIN_ONLY`
   (`backend/ai/tools/settings.py`).
2. `ToolExecutor._is_auto_executable()` auto-executes only
   READ_ONLY / READ_WRITE / DANGEROUS tools; ADMIN_ONLY and
   CONFIRMATION_REQUIRED are excluded.
3. `_execute_single()` therefore returned
   `ToolExecutionResult(success=False, needs_confirmation=True,
   error="confirmation_required")` **without calling `tool.execute()`**.
4. The Dispatcher recorded the blocked outcome as a tool failure, had no
   pending-approval state, produced no confirmation-request prompt, and had
   no path that consumed an owner's approval reply and re-issued the call.

Result: the AI flow stopped at the confirmation boundary. This was a
**feature gap, not a security gap** — the gate itself was correct.

### 3.2 Model/provider runtime switching (fixed by this task) — CONFIRMED

The exact source-proven chain, before this fix:

1. AI selects `settings_set` with `{"key": "provider"|"model", "value": ...}`.
2. Owner confirms; `ToolExecutor.execute_confirmed()` reaches
   `SettingsSetTool.execute()` (commit `c5d29f7`).
3. `SettingsSetTool.execute()` called `settings_service.set_setting(key,
   value)` — the **Glass-panel settings system** (`panel_settings` table),
   whose typed columns are panel concerns only (`language`, `dashboard_font`,
   timeouts, ...). There is **no `provider`/`model` column and no validator**
   for them (`_DEFAULTS`/`_VALIDATORS` in
   `backend/services/settings_service.py`).
4. `set_setting("provider", ...)` therefore took the unvalidated path:
   `repo.update_field("provider", ...)` fails against Supabase (no such
   column) → returns `False` → `set_setting` falls back to
   `_cache[key] = value; return True` — a **phantom success**: only the
   in-memory panel cache holds the value, and nothing that matters reads it.
5. The AI runtime's authoritative provider/model lives in a DIFFERENT store:
   `backend/ai/config_store.py` (`ai_config` table). At the start of EVERY AI
   request, `ai_unified._restore_config(owner_id)` reads `config_store`
   and re-applies it via
   `engine.apply_runtime_selection(provider, model)` →
   `ProviderManager.apply_selection()` (switches the registry's active
   provider and writes `config.default_model` on the live provider instance).
6. The settings_set path never touched `config_store`, never called
   `apply_runtime_selection`, and nothing else re-reads `panel_settings` for
   AI keys — so the next AI request was still served by the OLD
   provider/model.
7. `settings_get` returned the phantom cached value, which is why the change
   **looked** successful end-to-end.

Confirmation of the disconnect (grep-verified): no `config_store` import or
call exists anywhere under `backend/ai/tools/` or `backend/services/`; the
only writer of `ai_config.provider/model` is the web API + glass actions
(`config_store.update_provider/update_model` + `_apply_runtime_selection`).

The same phantom-write defect applied to the other AI-runtime keys
(`temperature`, `max_tokens`, `system_prompt`, `history_budget`,
`trigger_en`, `trigger_fa`), which are also not panel_settings columns.

The reported `Supabase event task query failed; using fallback:
[Errno 11] Resource temporarily unavailable` warnings are **not** part of
this bug: they come from the task repository's Supabase→in-memory fallback
and do not touch the model/provider path.

### 3.3 End-to-end state divergence (fixed by this task) — CONFIRMED

Live observation: the AI menu showed one model/provider, the AI's runtime
context claimed `provider=dummy`, and settings_get returned yet another
pair. Source-proven causes, each independently confirmed:

1. **The prompt's [Runtime Context] was never truthful.**
   `RuntimeSession.active_provider` defaults to `"dummy"`
   (`backend/ai/runtime/session.py`) and
   `ConversationManager.set_provider` — the designed sync API — had zero
   production callers (grep-verified). `Dispatcher._build_context` rendered
   `session.active_provider` directly into the context the model sees
   (`dispatcher.py`), so every AI response could claim `provider=dummy`
   while the ProviderManager was serving groq/gemini/… — exactly the live
   `AI: enabled (provider=dummy, requests=5, responses=4, turn=17)` line.
2. **No boot-time application of the persisted config.** The engine's
   active provider is selected from ENV (`ProviderFactory.create_manager`,
   `AI_PROVIDER` or `"dummy"`). The persisted `ai_config` was applied only
   on the first chat request (`ai_unified._restore_config`), so before that
   moment every engine-read surface (AI menu state, health panels) could
   disagree with every config-read surface (menu identity, settings_get).
3. **temperature/max_tokens never reached the runtime.** `settings_set`
   persisted them into `config_store`, but the providers read
   `ProviderConfig.temperature/max_tokens` at request time
   (`openai_compat.py`), and nothing copied the persisted values onto the
   live provider config — the same phantom-divergence class as chunk 2's
   provider/model bug.

### 3.4 AI menu displayed a provider/model the runtime does not serve
(fixed by this task) — CONFIRMED

Live observation: the AI menu showed `cohere / command-a-plus-05-2026`
while the AI's runtime context and the actually-served requests used
`groq / gpt-oss-20b`. Source-proven chain:

1. **Registration is ENV-gated.** `ProviderFactory.create_registry`
   (`backend/ai/providers/factory.py`) registers ONLY providers whose API
   key exists in the process ENV (`if not api_key: continue`). Cohere has
   no `AI_COHERE_API_KEY` in the Render environment (`render.yaml` defines
   none), so `cohere` is never registered in the runtime registry.
2. **Writers validate by name only.** `settings_set`
   (`backend/ai/tools/settings.py::_set_ai_config`) and the web API
   (`backend/web/app.py::api_ai_set_provider`) validate `provider` against
   `discovery.get_provider_info(name)` — metadata for EVERY supported
   provider regardless of key — so an unregistered provider was persisted
   into `config_store` (`ai_config`).
3. **Apply failure is silent.** `ProviderManager.apply_selection`
   (`manager.py`) returns `False` WITHOUT switching when
   `self._registry.has(provider)` is False; `engine.apply_runtime_selection`
   returns that `False`; `engine.apply_persisted_config` (chunk 3) IGNORED
   the return value and continued (session/temperature sync), so the phantom
   survived boot AND every per-request restore.
4. **The menu displays persisted state as if it were active.**
   `_ai_main_panel_handler` / `_ai_health_panel_handler`
   (`backend/bot/handlers/ai.py`) resolved
   `config.get("provider") or engine_info["provider"]` — persisted config
   FIRST — so they rendered the unappliable pair (cohere).
5. **The AI truthfully reports the runtime.** `Dispatcher._build_context`
   (chunk 3) reads `ProviderManager.get_active_name()` +
   `get_provider_config(...).default_model` (groq / gpt-oss-20b) — what the
   AI answers and what the Details telemetry records as served.

Result: menu = persisted intent (cohere), AI = effective runtime (groq),
with no reconciliation anywhere. The runtime was NOT wrong and the menu was
NOT reading a cache — the menu was reading the persisted configuration
while the runtime read the ProviderManager, and the persisted provider
could never be applied.

---

## 4. IMPLEMENTED CHANGES

### 4.1 `backend/ai/confirmation.py` — NEW (chunk 1, production)

Bounded, in-memory owner-approval state. Follows the repository's existing
bounded in-memory convention (`backend/helper/input_state.py`): process
memory, monotonic expiry, sync operations (atomic with respect to the asyncio
event loop — no lock needed for the create/take race), `clear_all()` for
tests. No persistence, no database table, no migration, no new persistence
authority.

- `PendingConfirmation` — frozen dataclass: `confirmation_id` (uuid hex),
  `owner_id`, `chat_id`, `session_id`, `tool_name`, `arguments`
  (a defensive copy of the ORIGINAL validated tool-call arguments),
  `created_at`, `expires_at`, `expired` property (monotonic TTL, fails
  closed).
- `PendingConfirmationStore` — dict keyed by `(owner_id, chat_id)`:
  - `create(...)` — stores an entry or returns `None` when an ACTIVE entry
    already exists for the scope (never silently overwrites); an EXPIRED
    entry is replaced.
  - `take(...)` — single-use consume: removes the entry BEFORE returning it,
    so a replay finds nothing; distinguishes "consumed active entry",
    "purged expired entry", and "nothing existed".
  - `pending_count()` / `clear_all()`.
- `CONFIRMATION_TTL_S = 120.0` — interactive approval lifetime, mirroring the
  existing pending-input convention (120 s).
- `_EXPLICIT_CONFIRMATIONS` — the COMPLETE bounded set of explicit
  confirmation phrases (Persian: بله، آره، اره، بلی، تایید/تائید/تأیید and
  their «... میکنم» forms incl. «بله تأیید میکنم» / «آره تایید میکنم»;
  English: yes / yeah / yep / confirm / confirmed / approve / approved /
  i confirm / go ahead). Exact normalized full-message match only.
- `normalize_confirmation_text()` — NFKC, ZWNJ → space (so تایید‌میکنم
  equals تایید میکنم), tatweel removed, only alphanumerics + whitespace kept
  (punctuation/emoji cannot break the match), whitespace collapsed, ASCII
  casefolded.
- `is_explicit_confirmation()` — True only for an exact match against the
  bounded phrase set. Never a substring/keyword scan.
- `confirmation_request_text(tool_name, arguments)` — deterministic prompt
  showing the exact frozen tool name and sorted arguments, plus the expiry
  and reply instructions («تأیید», «بله», "yes").
- `CONFIRMATION_ALREADY_PENDING_TEXT` / `_expired_text()` — deterministic
  already-pending / expired answers.

### 4.2 `backend/ai/engine/dispatcher.py` — MODIFIED (chunk 1, production)

- `Dispatcher.__slots__` and `__init__`: new `_confirmation_store`
  (`PendingConfirmationStore()`), owned per Dispatcher instance.
- `_gate_confirmation_results(request, tool_calls, exec_results)` — NEW.
  Called after every tool-execution batch (each provider tool round, the
  round-limit salvage batch, and the local deterministic fast path). It
  collects results with `needs_confirmation`, stores ONLY the FIRST blocked
  call (tool name + a copy of its arguments) in the store, and returns the
  deterministic text to show. The caller sets
  `metadata["confirmation_pending"] = True`, clears the round's tool calls,
  and stops the loop — no continuation provider round sees the blocked result
  and no follow-up round can re-request the action on its own.
- `_try_consume_confirmation(request, rid, status_callback, start, metadata)`
  — NEW. Runs in `dispatch()` after the conversation-runtime stage and
  BEFORE any provider round, only when tools are allowed and a ToolExecutor
  is attached. `take()`s the pending entry for `(owner_id, chat_id)` only on
  an exact explicit confirmation phrase; rebuilds the tool call EXCLUSIVELY
  from the frozen `PendingConfirmation` and executes it via
  `ToolExecutor.execute_confirmed()`; records the real outcome and returns a
  deterministic `EngineResult` (`kind="confirmed"` / `"expired"` /
  `"confirmed_error"`).
- History hygiene: `needs_confirmation` outcomes are skipped when recording
  tool outcomes into conversation history.

### 4.3 `backend/ai/tools/executor.py` — MODIFIED (chunk 1, production)

- `execute_confirmed(call, owner_id, session_id, status_callback,
  context_override)` — NEW public method. Executes ONE tool call whose
  confirmation boundary is satisfied. Only the Dispatcher calls it, and only
  with a call rebuilt from a consumed `PendingConfirmation`. Delegates to
  `_execute_single(..., confirmed=True)`.
- `_execute_single(...)` — gained a keyword-only `confirmed: bool = False`.
  The permission gate is bypassed ONLY when `confirmed=True`; that flag is
  out-of-band and never provider-visible. Every other executor guarantee is
  unchanged on the confirmed path: registry lookup, malformed/non-object
  arguments rejection, timeout / `long_running` handling, history recording,
  persistence task, error containment (never raises).

### 4.4 `backend/ai/tools/settings.py` — MODIFIED (chunk 2, production)

The fix for the model/provider runtime-switching bug:

- Module docstring now states the store ownership rule: panel keys →
  `settings_service` (panel_settings); AI runtime keys → `config_store`
  (ai_config).
- `_AI_CONFIG_KEYS` — explicit bounded frozenset of the AI-runtime keys:
  `provider`, `model`, `temperature`, `max_tokens`, `system_prompt`,
  `history_budget`, `trigger_en`, `trigger_fa`. Anything not in this set
  keeps the existing `settings_service` path (panel keys unchanged).
- `_set_ai_config(context, key, value)` — NEW async router for AI keys:
  - `provider`: normalized to lowercase, validated against
    `discovery.get_provider_info()` (must be a KNOWN provider and
    `capability_kind == "chat"` — a web-search-only provider such as `you`
    is rejected); persists via `config_store.update_provider(owner_id,
    provider, default_model)`; then `_apply_runtime_selection(provider,
    default_model)`.
  - `model`: must be a non-empty string ≤ 200 chars; requires an already
    configured provider (fails closed with "set a provider first");
    persists via `config_store.update_model`; then
    `_apply_runtime_selection(provider, model)`.
  - `temperature`: float, must be 0.0–2.0 (mirrors the panel input rule).
  - `max_tokens` / `history_budget`: positive integers.
  - `system_prompt`: string.
  - `trigger_en` / `trigger_fa`: single word (no spaces); must differ from
    the other trigger (case-insensitive).
  - Every validation failure returns `ToolResult(success=False)` BEFORE any
    write — nothing is persisted, the active runtime is never corrupted.
- `_apply_runtime_selection(provider, model)` — NEW module helper using the
  SAME authoritative runtime path as the web API, glass actions, and chat
  entry points (`engine.apply_runtime_selection` →
  `ProviderManager.apply_selection`). Lazy import (no import cycle with the
  engine). Failures are logged, never raised: the persisted `config_store`
  remains the source of truth and `ai_unified._restore_config` re-applies it
  before the next request anyway.
- `SettingsGetTool.execute()` — AI keys now read from `config_store` (the
  real value), panel keys still read `settings_service`.
- `SettingsSetTool.execute()` — AI keys route to `_set_ai_config`, panel
  keys keep the unchanged `settings_service.set_setting` path (existing
  tests that patch `settings_service.set_setting` with `key="language"`
  still pass unchanged).

### 4.5 `tests/test_confirmation_roundtrip.py` — NEW (chunk 1, test code)

63 tests pinning the store/recognition contracts, the executor permission
gate, and the full dispatcher AI path (see §7).

### 4.6 `tests/test_settings_runtime_switch.py` — NEW (chunk 2, test code)

15 tests pinning store routing, validation/fail-closed behavior, and —
critically — runtime propagation through the REAL Engine → Dispatcher →
ProviderManager path (see §7).

**Intentionally NOT changed by this task:** `ToolRegistry` (no new tool
registered, no registration API change), provider schemas (`settings_set` is
still advertised as before; auto-execution is still refused until the owner
confirms), provider factory/registry/manager, `config_store`,
`settings_service`, the web API, task scheduler/coordinator, database layer,
and `INVESTIGATION.md`.

### 4.7 End-to-end state consistency — MODIFIED (chunk 3, production)

- `backend/ai/engine/engine.py` — `apply_persisted_config(owner_id)` NEW:
  the ONE shared restore, called at boot AND before every chat request.
  Provider/model → `apply_runtime_selection` (the existing authoritative
  path); temperature/max_tokens → written onto the active provider's OWN
  config object (the one the provider reads at request time); conversation
  session sync via `ConversationManager.set_provider` (wired for the first
  time); system prompt applied. Failures logged, never raised; the persisted
  `config_store` remains the source of truth.
- `backend/runtime/supervisor.py` — `_apply_ai_config_at_boot()` NEW, called
  from `_build_and_register` right after AI tool wiring (and therefore on
  every reconnect/rebuild): applies the persisted config so the runtime and
  the config-read surfaces agree from the first second of the process.
- `backend/bot/handlers/ai_unified.py` — `_restore_config` now delegates to
  the shared `apply_persisted_config` (identical behavior for provider/model
  + system prompt; adds temperature/max_tokens + session sync).
- `backend/ai/engine/dispatcher.py` — `_build_context` builds the
  [Runtime Context] provider/model from `ProviderManager`
  (`get_active_name()` + the active provider's `default_model`) — the single
  authoritative runtime state — with a session-value fallback only for test
  doubles. The model is now also visible in the prompt line.
- `backend/ai/conversation/context_builder.py` — `RuntimeContext` gains
  `active_model`.
- `backend/ai/prompt/builder.py` — the `AI: enabled (...)` runtime line now
  renders `model=<active model>`.

### 4.8 Menu-vs-runtime provider/model consistency — MODIFIED (chunk 4,
production)

- `backend/ai/engine/engine.py` — `apply_persisted_config` now checks the
  return value of `apply_runtime_selection`; on failure (persisted provider
  not registered at runtime) it calls NEW `_heal_phantom_config()`: reads
  the ACTIVE ProviderManager pair (`get_active_name()` + the active
  provider's `default_model`), rewrites `config_store` to that pair (the
  ProviderManager remains the single authoritative runtime state — no new
  authority), logs a clear warning, and continues the restore with the
  effective pair. Idempotent: after one heal the persisted config equals the
  runtime, so no further writes occur.
- `backend/ai/tools/settings.py` — `_set_ai_config` provider branch now
  requires the provider to be REGISTERED in the runtime
  (`get_engine().provider_manager.list_providers()`); an unregistered
  provider (known to discovery but with no API key in this process's ENV)
  returns `ToolResult(success=False)` with an honest message and is NEVER
  persisted.
- `backend/web/app.py` — `/api/ai/provider` applies the same registration
  guard (HTTP 400, before `update_provider`) so the dashboard cannot create
  a phantom pair either.
- `backend/bot/handlers/ai.py` — NEW `_effective_pair(engine_info, config)`:
  the AI main panel and health panel display the ProviderManager pair
  (engine_info) FIRST — the same authoritative runtime state the AI request
  path and runtime context read — falling back to persisted config only
  when the engine reports nothing (`""`/`"—"`). The health panel's
  `configured` check now also accepts an engine-reported active provider.

### 4.9 Web provider switch persists provider + default model — MODIFIED
(chunk 5, production)

- `backend/web/app.py` — `POST /api/ai/provider` now persists the new
  provider's DEFAULT model together with the provider
  (`config_store.update_provider(owner_id, provider, default_model)`),
  mirroring `settings_set` (`_set_ai_config`) and the glass provider action,
  which already write provider + default model atomically. Previously the
  web writer persisted only the provider, so `ai_config.model` kept the OLD
  provider's model while the runtime applied (and the endpoint response
  claimed) the new default — and the next per-request
  `apply_persisted_config` flipped the runtime back to the stale model.
  The registration guard (chunk 4) is unchanged.

---

## 5. CONFIRMATION FLOW (as implemented)

Actual names/data structures, verified in source:

```
owner request ("change model to X")
  → Dispatcher.dispatch(AIRequest)
  → provider round selects settings_set (native tool call)
  → ToolExecutor.execute_calls() → _execute_single(confirmed=False)
  → permission gate: ADMIN_ONLY not auto-executable
  → ToolExecutionResult(needs_confirmation=True, error="confirmation_required")
    (tool.execute() is NOT called)
  → Dispatcher._gate_confirmation_results()
  → PendingConfirmationStore.create(owner_id, chat_id, session_id,
        tool_name, frozen arguments)          # one active per owner+chat
  → deterministic prompt returned as the AI response
    (confirmation_request_text: "⚠️ Owner approval required … settings_set …")

owner replies «تأیید» / «بله» / "yes" to that message
  → next Dispatcher.dispatch() reaches _try_consume_confirmation()
    (before any provider round; exact normalized phrase match only)
  → PendingConfirmationStore.take(owner_id, chat_id)   # single-use
      · expired entry → deterministic "expired, nothing executed" answer
      · no entry      → normal conversational flow continues
  → call rebuilt ONLY from the frozen PendingConfirmation
      {name: entry.tool_name, arguments: dict(entry.arguments)}
  → ToolExecutor.execute_confirmed(call, context_override=…)
  → _execute_single(confirmed=True)
  → registry lookup → tool.execute(ctx, frozen_arguments) → ToolResult
  → real service side effect, exactly once
  → outcome recorded in conversation history
  → deterministic EngineResult (kind="confirmed")
```

### 5.1 Model/provider propagation (chunk 2, as implemented)

```
confirmed settings_set(key="model", value="model-b")
  → SettingsSetTool.execute → _set_ai_config
  → config_store.update_model(owner_id, "model-b")   # ai_config persisted
  → _apply_runtime_selection(provider, "model-b")
  → engine.apply_runtime_selection → ProviderManager.apply_selection
  → registry active provider unchanged; provider.config.default_model = "model-b"

next AI request ("hello")
  → ai_unified._restore_config(owner_id) (belt: re-applies config_store)
  → Dispatcher → ProviderManager.chat → active provider serves request
    with provider.config.default_model == "model-b"
```

Provider switch is identical with `update_provider` + `apply_selection`
switching the registry's active provider to the requested one.

### 5.2 End-to-end state consistency (chunk 3, as implemented)

```
persisted config (ai_config) ─┐
                             ├→ engine.apply_persisted_config(owner_id)
boot: supervisor._apply_ai_config_at_boot()   (also on every reconnect)
chat: ai_unified._restore_config(owner_id)    (before EVERY request)
  → apply_runtime_selection(provider, model)   # existing authoritative path
  → ProviderManager.apply_selection: registry active provider switched +
    provider.config.default_model written on the LIVE instance
  → temperature/max_tokens copied onto provider.config (the object the
    provider reads at request time)
  → ConversationManager.create_session + set_provider(owner, provider, model)
    → RuntimeSession.active_provider/active_model truthful (no more "dummy")
  → system prompt applied

every request:
  → Dispatcher._build_context reads ProviderManager.get_active_name() +
    get_provider_config(name).default_model → [Runtime Context] shows the
    REAL serving provider/model (model rendered in the AI: enabled line)
```

Surfaces that now agree: persistent AI configuration, ProviderManager active
provider, active provider's effective model, AI request execution, AI runtime
context, settings_get, the Self Bot AI menu/status panel, and health/status
surfaces that read the engine.

### 5.3 Menu-vs-runtime consistency (chunk 4, as implemented)

```
phantom persisted pair (provider not registered: no API key in ENV)
  boot: supervisor._apply_ai_config_at_boot()
  chat: ai_unified._restore_config(owner_id)     # before EVERY request
    → apply_persisted_config(owner_id)
    → apply_runtime_selection(provider, model) → ProviderManager.apply_selection
        → registry.has(provider) == False → returns False, NO switch
    → _heal_phantom_config(engine, owner_id, config, provider)
        → active = ProviderManager.get_active_name()      # e.g. groq
        → model  = active provider's config.default_model # e.g. gpt-oss-20b
        → config_store.save_config(owner_id, {provider: active, model: model})
        → restore continues with the EFFECTIVE pair
    → config_store == ProviderManager from now on (idempotent)

new writes are prevented at the boundary:
  settings_set(provider=...)  → registration check → reject (not persisted)
  POST /api/ai/provider       → registration check → HTTP 400 (not persisted)

menu/health rendering:
  _effective_pair(engine_info, config)   # ProviderManager FIRST
    → displays groq / gpt-oss-20b (same pair the AI runtime context reports
      and the next request is served with); persisted config only as a
      fallback when the engine has no info
```

### 5.4 Web provider-switch writer (chunk 5, as implemented)

```
dashboard "Select" on a REGISTERED provider
  → POST /api/ai/provider {provider: "gemini"}
    → registration check (registered → passes)
    → config_store.update_provider(owner_id, "gemini", "gemini-default")
        # ONE atomic write: provider AND its default model (was: no model,
        # so ai_config.model kept the OLD provider's model)
    → engine.apply_runtime_selection → ProviderManager.apply_selection
        → active provider = gemini; provider.config.default_model = "gemini-default"
    → response {"provider": "gemini", "model": "gemini-default"}

next AI request (or boot): ai_unified._restore_config → apply_persisted_config
  → reads persisted (gemini, "gemini-default") == runtime → no flip-back
```

Identical semantics to `settings_set(provider=...)` and the glass provider
action: provider + default model are persisted and applied together, so no
writer can leave the previous provider's model behind.

---

## 6. SECURITY MODEL

Tied to source behavior, not to test results:

- **Owner scoping** — confirmations are keyed by `(owner_id, chat_id)`;
  `take()` from another chat or another owner finds nothing. The AI
  activation path itself remains owner-only (`is_owner`) and unchanged.
- **Confirmation ownership** — the entry is created server-side from the
  blocked provider call; the reply can only consume it.
- **Token/state validation** — no client-supplied token exists; the entry is
  looked up by the trusted `(owner_id, chat_id)` of the incoming request.
- **Expiration** — 120 s monotonic TTL (`CONFIRMATION_TTL_S`); `expired`
  fails closed and the store purges the entry on consume attempt.
- **Single-use / replay protection** — `take()` removes the entry BEFORE
  returning it; a replay finds nothing and cannot execute twice.
- **Tool-name validation** — the frozen tool name is re-validated against
  `ToolRegistry` on every confirmed execution; unregistered names fail
  closed (`error="not_found"`).
- **Argument preservation/validation** — arguments are a defensive copy
  frozen at `create()`; the confirmation reply cannot modify them.
  Malformed/non-object arguments still fail closed on the confirmed path.
- **ToolRegistry authority** — unchanged and authoritative: a confirmation
  can never conjure a tool that is not registered.
- **ToolExecutor authority** — still the SOLE component that calls
  `tool.execute()`. `execute_confirmed()` is the only bypass of the
  auto-execution gate, and only the Dispatcher calls it with a consumed
  pending record; the `confirmed` flag is out-of-band, never
  provider-visible.
- **Self Bot execution authority** — unchanged; tools still delegate to the
  service layer, which owns Telegram RPC.
- **Model/provider values** — bounded strings; `provider` must match a known
  registered provider name with chat capability (validated via
  `discovery.get_provider_info` before any write), `model` is a plain string
  that only ever becomes an API parameter (max 200 chars). No value can
  trigger code execution, environment mutation, filesystem writes, or
  arbitrary HTTP. Invalid values are rejected BEFORE any write (fail
  closed); the active runtime configuration is never corrupted.
- **No new configuration authority** — the fix reuses the existing
  `config_store` persistence and the existing
  `engine.apply_runtime_selection` path (the same one the web API, glass
  actions, and chat entry points use). No second ProviderManager, no second
  provider-selection architecture.
- **ADMIN_ONLY unchanged** — a provider/model change still requires the
  exact explicit confirmation phrase; there is no automatic confirmation and
  no weakening of the permission boundary.

Boundary confirmations (all source-verified unchanged): the implementation
introduces **no** arbitrary tool execution (registry still required), **no**
arbitrary Telegram RPC, **no** SQL, **no** shell, **no** HTTP, and **no**
code execution surface.

---

## 7. TESTING

**Tests added:** `tests/test_confirmation_roundtrip.py` — 63 tests (chunk 1);
`tests/test_settings_runtime_switch.py` — 15 tests (chunk 2).
**Tests modified:** none. **Relevant existing suites re-run:** unchanged.

### 7.1 Confirmation round-trip tests (63)

- **Store contract (UNIT):** one pending per `(owner, chat)` scope; `create()`
  never overwrites an active entry; independent scopes (different chat /
  different owner); `take()` is single-use and returns frozen arguments;
  expired entries fail closed and can be replaced.
- **Recognition (UNIT):** the full bounded phrase set (Persian spellings
  incl. ZWNJ variants, English words, punctuation/emoji tolerance); ambiguous
  or conversational acknowledgements are never confirmations.
- **Executor gate (INTEGRATION, in-process):** `settings_set` first attempt →
  `needs_confirmation`, service never called; `execute_confirmed()` runs the
  ORIGINAL call exactly once through the real registry with the service
  faked; unknown tool and malformed-arguments calls fail closed even when
  "confirmed".
- **Full AI path (AI-PATH, in-process):** real provider→registry→executor
  chain with the service boundary faked — pending created + prompt text
  returned; explicit confirmation consumes the pending and executes the
  original call exactly once; replay does not double-execute; confirmation
  reply cannot change arguments; expired confirmation does not execute;
  cross-chat and cross-owner confirmations are ignored; ambiguous
  acknowledgement never executes; READ_ONLY `settings_get` still executes
  directly; conversational "بله" with nothing pending stays conversational.

### 7.2 Model/provider runtime-switch tests (15)

- **Confirmation boundary:** an AI-key `settings_set` first attempt returns
  `needs_confirmation` and touches nothing (config_store unchanged).
- **Persistence:** confirmed `model`/`provider` writes reach
  `config_store` (the authoritative AI store); `settings_get` reads the REAL
  value for AI keys and the panel value for panel keys.
- **Panel-key regression:** `key="language"` still routes to
  `settings_service.set_setting` (patched and asserted).
- **Model switch (runtime propagation, the core requirement):** REAL
  `Engine` over a REAL `ProviderManager`/`ProviderRegistry` with scripted
  providers; `settings_set(model="model-b")` via the real confirmed executor
  path → `config_store` persisted, `provider.config.default_model ==
  "model-b"` on the LIVE provider instance, active provider unchanged, and a
  NEW request through `engine.execute()` is served by prov-a with
  `model-b` (asserted from the provider's recorded serving model — not from
  `settings_get`).
- **Provider switch:** `settings_set(provider="gemini")` → active provider
  becomes `gemini` (stub registered under that name), its default model
  matches the persisted one, and the NEXT request is served by `gemini`.
- **Invalid values fail closed:** empty model, unknown provider, non-chat
  capability provider (`you`), model with no configured provider, out-of-range
  temperature, non-positive max_tokens, multi-word / colliding triggers — all
  return failure with the config AND the live runtime state unchanged.
- **Routing boundary:** `_AI_CONFIG_KEYS` is explicit, bounded, and disjoint
  from `settings_service._DEFAULTS`.

### 7.3 State-consistency tests (chunk 3) — `tests/test_ai_state_consistency.py` (5 tests)

- **Shared restore switches the runtime AND the next request:**
  `apply_persisted_config` over a REAL Engine/ProviderManager with scripted
  providers — persisted `prov-b`/`persisted-model` become the active
  provider's runtime pair, and a NEW request through `engine.execute()` is
  served by exactly that pair (asserted from the provider's recorded
  serving model).
- **Session sync:** the owner's `RuntimeSession.active_provider` /
  `active_model` no longer stay at the `"dummy"` defaults after restore
  (the designed `set_provider` API is wired for the first time).
- **temperature/max_tokens runtime sync:** persisted values land on the
  live provider config object the provider reads at request time.
- **Runtime context truthfulness:** a full dispatch through the REAL
  Engine with a capture prompt builder proves the [Runtime Context] the
  model sees carries the ProviderManager's active provider AND model.
- **Prompt rendering:** the rendered `AI: enabled (...)` line includes
  `model=<active model>`.

### 7.4 Menu-vs-runtime consistency tests (chunks 4–5) —
`tests/test_ai_menu_state_consistency.py` (7 tests)

- Phantom persisted pair healed to the ACTIVE ProviderManager pair by
  `apply_persisted_config` (boot/per-request restore), idempotent on a
  second restore, runtime never corrupted.
- AI menu MAIN panel renders the effective runtime pair (groq /
  gpt-oss-20b), NOT the persisted phantom (cohere / command-a-plus-05-2026),
  and the NEXT real request through the same engine is served by exactly
  that pair — the full `config_store → ProviderManager → request → menu`
  chain exercised with the real engine and the real menu handler.
- AI HEALTH panel renders the effective runtime pair.
- Menu falls back to the persisted pair when no engine exists (regression
  guard for the no-engine case).
- `settings_set` rejects an unregistered provider (nothing persisted); a
  registered provider still persists and applies (positive control).
- Web API `/api/ai/provider` rejects an unregistered provider with HTTP 400
  BEFORE persisting (`update_provider` never called).
- Web API `/api/ai/provider` switch to a REGISTERED provider persists the
  provider's DEFAULT model together with the provider (never the old
  provider's model); the runtime serves exactly the persisted pair; and a
  subsequent `apply_persisted_config` restore cannot flip the runtime back
  to a stale model (chunk 5 regression).

### 7.5 Executed (this task)

- Focused (chunks 4–5): `tests/test_ai_menu_state_consistency.py` → **7 passed**
- Focused (chunk 3): `tests/test_ai_state_consistency.py` → **5 passed**
- Focused (chunk 2): `tests/test_settings_runtime_switch.py` → **15 passed**
- Adjacent: `tests/test_13_model_selection.py`, `tests/test_model_tester.py`,
  `tests/test_34_ai_model_ui.py`, `tests/test_36_ai_settings_ux.py` →
  **60 passed** (web provider/model endpoints + AI menu/details UX; the two
  web provider-endpoint tests were updated to register `openai` in the
  patched engine because the endpoint now validates runtime registration)
- Full suite `python3 -m pytest tests/ -q -p no:cacheprovider` →
  **1695 passed, 23 skipped** (61.89s)
- `python3 -m py_compile` on every changed module → **passed**
- `git diff --check` → **passed**

Categories:
- UNIT TESTS — store scoping/single-use/expiry, phrase recognition, routing
  boundary, validation rules.
- INTEGRATION TESTS (in-process) — executor gate + `execute_confirmed`
  through the real registry; tool-level persistence/fail-closed behavior
  with the real `config_store` in-memory fallback.
- AI-PATH TESTS (in-process) — dispatcher-level provider→registry→executor
  rounds (chunk 1); REAL Engine→Dispatcher→ProviderManager propagation of a
  confirmed model/provider switch into the NEXT request (chunk 2).
- LIVE TELEGRAM TESTS — **NOT PERFORMED**.
- LIVE SUPABASE TESTS — **NOT PERFORMED** (no DB code path changed).

---

## 8. AI CONNECTIVITY IMPACT

- **`settings_set` is now executable through AI** — after an explicit owner
  confirmation, the frozen call re-enters `ToolExecutor.execute_confirmed`
  and reaches the real setting store (chunk 1).
- **ADMIN_ONLY confirmation is now supported** (proven via `settings_set`).
- **CONFIRMATION_REQUIRED tools** share the identical executor gate and the
  identical confirmed bypass; no tool currently registers
  `CONFIRMATION_REQUIRED`, so the mechanism is generic but exercised only
  through ADMIN_ONLY today.
- **REAL_CONNECTED count:** all **36 of 36** registered tools are
  AI-executable end-to-end (35 auto-executed + `settings_set` via
  confirmation) — in-process-verified. `INVESTIGATION.md` still carries the
  pre-chunk matrix (35 `REAL_CONNECTED`, `settings_set`
  `PROVIDER_REACHABLE_BUT_NOT_EXECUTABLE`) because it was intentionally not
  modified by these chunks.
- **Runtime switching (chunk 2):** `settings_set` on `provider`/`model` (and
  the other `_AI_CONFIG_KEYS`) now writes the store the runtime actually
  reads and pushes the selection into the live `ProviderManager` — the next
  AI request is served by the requested provider/model. Provider schemas and
  the ToolRegistry are unchanged.
- **ToolExecutor behavior changed:** new `execute_confirmed()` entry point
  plus the `confirmed` bypass (chunk 1); nothing else.
- **Dispatcher behavior changed:** blocked outcomes become pending owner
  approvals, and exact confirmation replies are consumed deterministically
  before any provider round (chunk 1).
- **Tool behavior changed:** `SettingsGetTool`/`SettingsSetTool` now route by
  key ownership (chunk 2); everything else unchanged.

---

## 9. TASK AUTOMATION IMPACT

**Task execution remains unchanged.** The `TaskExecutionCoordinator`
(`backend/ai/task_execution.py:123`) still invokes
`ToolExecutor.execute_calls()` — the `confirmed` flag defaults to `False`, so
a stored ADMIN_ONLY / CONFIRMATION_REQUIRED action would still be refused by
the gate exactly as before. No task-facing confirmation flow was added (owner
confirmation is interactive and chat-scoped by design; unattended scheduled /
event-triggered execution cannot confirm). No change to the task scheduler,
event dispatcher, occurrence/repository layer, coordinator, or the settings
tools' task-adjacent behavior.

---

## 10. TELEGRAM / SUPABASE IMPACT

- **Telegram API unchanged** — no new RPC, no new call sites beyond the
  existing tool → service layer; no new Telegram side effects.
- **Supabase schema unchanged** — no table, column, index, or RLS change.
- **No migration created.**
- **Supabase repositories unchanged** — confirmation state is deliberately
  in-memory only (bounded, process-scoped); the model/provider fix writes
  only to the EXISTING `ai_config` table through the existing `config_store`
  module.

---

## 11. FILES CHANGED

**Production files**
- `backend/ai/confirmation.py` — NEW (chunk 1): pending-confirmation store,
  recognition, prompt/answer text.
- `backend/ai/engine/dispatcher.py` — MODIFIED (chunk 1): pending-approval
  gating + confirmation consumption + history hygiene; (chunk 3): runtime
  context built from the ProviderManager (provider + model).
- `backend/ai/tools/executor.py` — MODIFIED (chunk 1): `execute_confirmed()`
  and the out-of-band `confirmed` bypass.
- `backend/ai/tools/settings.py` — MODIFIED (chunk 2): key-ownership routing
  to `config_store` + `apply_runtime_selection`; validation of AI keys;
  `settings_get` reads the real AI store.
- `backend/ai/engine/engine.py` — MODIFIED (chunk 3): NEW
  `apply_persisted_config(owner_id)` — the single shared config restore
  (boot + per-request). (chunk 4): phantom-config heal — `apply_runtime_selection`
  failures now rewrite the persisted pair to the ACTIVE ProviderManager pair
  (NEW `_heal_phantom_config`).
- `backend/ai/tools/settings.py` — MODIFIED (chunk 4): provider registration
  guard in `_set_ai_config` (unregistered providers are rejected, never
  persisted).
- `backend/web/app.py` — MODIFIED (chunk 4): `/api/ai/provider` rejects
  unregistered providers with HTTP 400 before persisting. (chunk 5):
  `/api/ai/provider` persists the new provider's DEFAULT model together
  with the provider (`update_provider(owner_id, provider, default_model)`)
  so the persisted pair can never diverge from the applied pair.
- `backend/bot/handlers/ai.py` — MODIFIED (chunk 4): NEW `_effective_pair`;
  AI main + health panels display the ProviderManager pair first.
- `backend/runtime/supervisor.py` — MODIFIED (chunk 3): NEW
  `_apply_ai_config_at_boot()` applied after AI tool wiring on every
  connect/rebuild.
- `backend/bot/handlers/ai_unified.py` — MODIFIED (chunk 3):
  `_restore_config` delegates to `apply_persisted_config`.
- `backend/ai/conversation/context_builder.py` — MODIFIED (chunk 3):
  `RuntimeContext.active_model`.
- `backend/ai/prompt/builder.py` — MODIFIED (chunk 3): `model=` rendered in
  the AI runtime line.

**Test files**
- `tests/test_confirmation_roundtrip.py` — NEW (chunk 1): 63 tests.
- `tests/test_settings_runtime_switch.py` — NEW (chunk 2): 15 tests.
- `tests/test_ai_state_consistency.py` — NEW (chunk 3): 5 tests.
- `tests/test_ai_menu_state_consistency.py` — NEW (chunk 4): 6 tests;
  (chunk 5) +1 regression test for the web provider-switch writer (7 tests).
- `tests/test_13_model_selection.py` / `tests/test_model_tester.py` —
  MODIFIED (chunk 4): web provider-endpoint tests register `openai` in the
  patched engine (the endpoint now validates runtime registration).

**Database files**
- none.

**Documentation files**
- `IMPLEMENTATION_REPORT.md` — this report (updated for all five chunks).

Not part of this implementation and intentionally untouched: `INVESTIGATION.md`,
`README.md`, database/sql files, and the pre-existing untracked
`telegram-self-bot/` nested clone in the working tree.

---

## 12. VALIDATION CLASSIFICATION

| Area | Classification |
|---|---|
| Pending store scoping / single-use / expiry | UNIT TESTED |
| Explicit-confirmation recognition (Persian/English, ZWNJ, ambiguity) | UNIT TESTED |
| Executor permission gate (ADMIN_ONLY refused, service never called) | INTEGRATION TESTED (in-process, real registry) |
| `execute_confirmed` original-call replay (frozen args, exactly once) | INTEGRATION TESTED (in-process, real registry, service faked) |
| Dispatcher AI path: pending creation → prompt → consume → re-issue | AI-PATH TESTED (in-process, provider + service faked) |
| Cross-chat / cross-owner / replay / expiry / argument-immutability | AI-PATH TESTED (in-process) |
| settings_set persistence for AI keys (config_store is the real store) | INTEGRATION TESTED (in-process, real config_store fallback) |
| Model switch → live provider config → NEXT request served by new model | AI-PATH TESTED (in-process, REAL Engine → Dispatcher → ProviderManager, scripted providers) |
| Provider switch → active provider + NEXT request served by new provider | AI-PATH TESTED (in-process, REAL Engine path) |
| Invalid model/provider/temperature/tokens/triggers fail closed, runtime uncorrupted | UNIT/INTEGRATION TESTED |
| Panel keys keep the settings_service path | INTEGRATION TESTED (patched boundary) |
| READ_ONLY tools unaffected (`settings_get` direct) | AI-PATH TESTED (in-process) |
| Task scheduler / event automation path | UNCHANGED — coordinator still calls `execute_calls()`; full suite green |
| `apply_persisted_config` (boot + per-request): provider/model + temperature/max_tokens + session + prompt | AI-PATH TESTED (in-process, REAL Engine → ProviderManager, scripted providers) |
| Runtime context shows the REAL active provider/model (no more `dummy`) | AI-PATH TESTED (in-process, full dispatch + capture prompt builder) |
| Session sync (`set_provider` wired; `active_provider`/`active_model` truthful) | INTEGRATION TESTED (in-process) |
| Prompt renders `model=` in the `AI: enabled` line | UNIT TESTED (real PromptBuilder) |
| Phantom persisted pair healed to the ACTIVE runtime pair (idempotent) | AI-PATH TESTED (in-process, REAL Engine → ProviderManager + real config_store fallback) |
| AI menu main panel renders the effective runtime pair (not the phantom) | AI-PATH TESTED (in-process, REAL menu handler + REAL Engine; next request served by the displayed pair) |
| AI health panel renders the effective runtime pair | AI-PATH TESTED (in-process, real handler) |
| Menu falls back to persisted config when no engine exists | AI-PATH TESTED (in-process) |
| `settings_set` rejects an unregistered provider (nothing persisted) | INTEGRATION TESTED (in-process, real registry/executor + config_store fallback) |
| Web `/api/ai/provider` rejects an unregistered provider (400, nothing persisted) | INTEGRATION TESTED (TestClient) |
| Web `/api/ai/provider` switch to a registered provider persists provider + default model and survives the per-request restore | INTEGRATION TESTED (TestClient + real config_store fallback + REAL ProviderManager) |
| Full regression suite | `1695 passed, 23 skipped` |
| Compile check / `git diff --check` | passed |
| Live Telegram confirmation round-trip | **NOT LIVE VERIFIED** |
| Live Telegram model/provider switch | **NOT LIVE VERIFIED** |
| Live Supabase behavior | **NOT LIVE VERIFIED** (no DB path changed) |

---

## 13. LIMITATIONS & REMAINING WORK

Source-supported limitations of the current implementation:

- **In-memory, process-scoped confirmation state.** Pending approvals live
  on the `Dispatcher` instance and are lost on process restart or an engine
  rebuild. Consistent with the existing bounded pending-input convention —
  no cross-restart confirmation persistence.
- **One active pending per (owner, chat).** When several gated calls arrive
  together, only the first is stored; the others are answered with an
  explicit "NOT scheduled" notice.
- **120-second confirmation TTL.** Expired approvals fail closed and require
  a fresh request.
- **Exact-phrase recognition only.** Confirmation is deliberately not
  free-form/LLM-judged.
- **Consumption requires the AI dispatch path in the same chat.** A
  confirmation reply reaches `_try_consume_confirmation()` only when
  dispatched as an AI request in the owner's chat.
- **CONFIRMATION_REQUIRED not exercised by a real tool.** The code path is
  identical to ADMIN_ONLY, but only `settings_set` (ADMIN_ONLY) uses a gate
  level today.
- **Provider selection requires a REGISTERED provider.** `settings_set`
  and the web `/api/ai/provider` reject providers whose API key is not
  present in this process's ENV (never persisted). The Telegram glass
  provider panel already lists only key-validated providers. A provider
  whose key is REMOVED from ENV between selection and a later deploy is
  healed: `apply_persisted_config` rewrites the persisted pair to the
  ACTIVE runtime pair at boot and before every request.
- **`settings_set` values are typed as strings** (provider schema); numeric
  keys (`temperature`, `max_tokens`, `history_budget`) are coerced and
  validated on the tool side.
- **temperature/max_tokens are runtime-synced, not persisted per provider.**
  The restore copies them onto the active provider's live config; a provider
  switch re-applies the stored values through the same restore. Direct
  per-provider config edits (`update_provider_config`) remain separate.
- **The runtime context is only as fresh as the last restore.**
  `apply_persisted_config` runs at boot and before every chat request;
  between restores the displayed context reflects the last applied state
  (the ProviderManager remains the authoritative source read at build time).
- **Live-environment verification not performed.** No live Telegram run of
  the full «تأیید» round-trip and no live model/provider switch occurred in
  this workspace.
- **Provider switches always persist the provider's DEFAULT model.**
  `settings_set`, the glass provider action, and the web `/api/ai/provider`
  all write provider + default model in one atomic `update_provider` call —
  no writer can leave the previous provider's model behind in `ai_config`.
- **`INVESTIGATION.md` connectivity matrix predates both chunks** (35 of 36
  / `settings_set` non-executable) and was intentionally not updated here.

---

## 14. GIT DELIVERY

| Item | Value |
|---|---|
| Chunk 1 commit | `c5d29f7` (`feat: add AI confirmation round-trip for owner-only tools`) |
| Chunk 2 commit | `8ac3779` (`fix: apply AI model and provider changes at runtime`) |
| Chunk 3 commit | `fix: make AI provider/model state consistent end-to-end` |
| Chunk 4 commit | `fix: stop AI menu from displaying unappliable provider/model` |
| Chunk 5 commit | `fix: persist the provider default model on web provider switch` (this task) |
| Branch | `main` |
| Push result | pushed to `origin/main` (no force-push) |
| Remote verification | `git fetch origin` then `git rev-parse HEAD` == `git rev-parse origin/main` |
| Docs commit (this report) | committed together with the implementation per repository workflow |
| Working tree | only the pre-existing untracked `telegram-self-bot/` nested clone remains; untouched by this task |