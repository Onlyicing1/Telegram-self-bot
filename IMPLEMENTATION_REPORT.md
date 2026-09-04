# IMPLEMENTATION REPORT

## 1. IMPLEMENTATION METADATA

| Field | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Branch | `main` |
| Confirmation round-trip commit | `c5d29f7` (`feat: add AI confirmation round-trip for owner-only tools`) |
| Model/provider runtime-switch commit | `c5d29f7` + new commit `fix: apply AI model and provider changes at runtime` |
| Implementation date | 2026-09-04 |
| Task/chunks | (1) AI confirmation round-trip for ADMIN_ONLY / CONFIRMATION_REQUIRED tools; (2) fix AI model/provider runtime switching |
| Work type | implementation |
| Final implementation status | **COMPLETE** — full test suite (`1683 passed, 23 skipped`), compile check, and diff hygiene pass; **live Telegram / live Supabase verification NOT performed** (no credentials/runtime in this workspace); **no database schema change, no migration** |

This is the single current-state report. It records only behavior and
validation established from the current source, the committed diffs
(`c5d29f7`, and the model/provider runtime-switch commit), and commands
actually run.

### Implementation summary

- **Chunk 1 — confirmation round-trip (committed `c5d29f7`):** an interactive
  owner-confirmation round-trip that lets ADMIN_ONLY /
  CONFIRMATION_REQUIRED registered tools complete through the AI execution
  path after explicit owner approval. Before it, `settings_set` (ADMIN_ONLY)
  was the only registered AI tool that could be selected but never executed —
  `ToolExecutor` correctly returned `needs_confirmation` and the Dispatcher
  had no way to consume an owner approval and re-issue the call.
- **Chunk 2 — model/provider runtime switching (this task, uncommitted at
  report time):** fixes the confirmed user-facing bug that the AI could
  receive and confirm a model/provider change request but the ACTIVE model
  never changed. `SettingsSetTool` was writing provider/model into
  `settings_service` (panel_settings) — a store the AI runtime never reads —
  so the change "succeeded" (settings_get showed it) yet the next AI request
  was still served by the old provider/model.
- **Current status by label:**
  - **IMPLEMENTED** — source present in `c5d29f7` + the runtime-switch commit.
  - **TESTED** — focused suites `tests/test_confirmation_roundtrip.py` (63
    tests) and `tests/test_settings_runtime_switch.py` (15 tests) plus the
    full suite (`1683 passed, 23 skipped`).
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
`settings_service`, `ai_unified.py`, the web API, task scheduler/coordinator,
database layer, and `INVESTIGATION.md`.

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

### 7.3 Executed (this task)

- Focused: `tests/test_settings_runtime_switch.py` → **15 passed**
- Adjacent: `tests/test_confirmation_roundtrip.py`,
  `tests/test_tool_health_audit.py`, `tests/test_10_tool_calls.py`,
  `tests/test_13_model_selection.py`, `tests/test_17_providers.py` →
  **171 passed**
- Full suite `.venv/bin/python -m pytest tests/ -q -p no:cacheprovider` →
  **1683 passed, 23 skipped** (61.98s)
- `.venv/bin/python -m compileall -q backend tests` → **passed**
  (chunk 1 session; chunk 2 touched only one module + one test file, both
  exercised by the suites above)
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
  gating + confirmation consumption + history hygiene.
- `backend/ai/tools/executor.py` — MODIFIED (chunk 1): `execute_confirmed()`
  and the out-of-band `confirmed` bypass.
- `backend/ai/tools/settings.py` — MODIFIED (chunk 2): key-ownership routing
  to `config_store` + `apply_runtime_selection`; validation of AI keys;
  `settings_get` reads the real AI store.

**Test files**
- `tests/test_confirmation_roundtrip.py` — NEW (chunk 1): 63 tests.
- `tests/test_settings_runtime_switch.py` — NEW (chunk 2): 15 tests.

**Database files**
- none.

**Documentation files**
- `IMPLEMENTATION_REPORT.md` — this report (full replacement).

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
| Full regression suite | `1683 passed, 23 skipped` |
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
- **Provider selection through `settings_set` requires a known provider
  name** (validated against the discovery catalog) and a chat-capability
  provider; a provider with no API key configured is persisted but the
  runtime apply is best-effort (the chat entry point re-applies on the next
  request; if no key exists the request fails over deterministically).
  This mirrors the existing web/glass selection behavior.
- **`settings_set` values are typed as strings** (provider schema); numeric
  keys (`temperature`, `max_tokens`, `history_budget`) are coerced and
  validated on the tool side.
- **Live-environment verification not performed.** No live Telegram run of
  the full «تأیید» round-trip and no live model/provider switch occurred in
  this workspace.
- **`INVESTIGATION.md` connectivity matrix predates both chunks** (35 of 36
  / `settings_set` non-executable) and was intentionally not updated here.

---

## 14. GIT DELIVERY

| Item | Value |
|---|---|
| Chunk 1 commit | `c5d29f7` (`feat: add AI confirmation round-trip for owner-only tools`) |
| Chunk 2 commit | `fix: apply AI model and provider changes at runtime` (this task) |
| Branch | `main` |
| Push result | pushed to `origin/main` (no force-push) |
| Remote verification | `git fetch origin` then `git rev-parse HEAD` == `git rev-parse origin/main` |
| Docs commit (this report) | committed together with chunk 2 per repository workflow |
| Working tree | only the pre-existing untracked `telegram-self-bot/` nested clone remains; untouched by this task |