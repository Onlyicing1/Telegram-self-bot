# INVESTIGATION

## INVESTIGATION METADATA

| Field | Value |
|---|---|
| Investigation date | 2026-08-20 |
| Branch | `main` |
| Current HEAD | `db927d4` (`Merge pull request #182 from Onlyicing2/main`) |
| Investigation-only status | YES — no production code, tests, dependencies, config, UI, or database schema were modified |
| Scope | Full dead-code / duplication / architecture audit of the Telegram self-bot (`backend/`), AI subsystem, runtime/recovery, helper/Glass UI, profile engines, services, web app, frontend, config/env, dependencies, SQL/Supabase usage, documentation, and tests |

This document **completely replaces** the previous `INVESTIGATION.md`. It is investigation evidence/handoff only. It is **not** proof that any cleanup has been performed — the current Git source remains the authority for what code actually exists.

Classification used throughout:

- **CONFIRMED** — directly proven by source/call-site/runtime evidence found in this audit.
- **LIKELY** — strongly indicated but not fully proven (e.g. dynamic import paths, unused-at-runtime only).
- **UNKNOWN** — insufficient repository evidence.

---

## 1. EXECUTIVE SUMMARY

The repository is a Python 3.11 Telethon self-bot with a FastAPI dashboard and a React/Vite frontend, deployed via `render.yaml`/`Procfile` (`python -m backend.main`). The runtime is a single `asyncio` process. `RuntimeSupervisor` is genuinely the central recovery authority: it builds the self-client, registers handlers, starts the optional helper bot, resumes Bio/Username cron, starts the web server, and starts the heartbeat/keepalive/failsafe/diagnostics/memory-cleanup loops. The Bio/Username engines are clean thin wrappers over one shared `ProfileEngine` and one shared scheduler — this part of the architecture is healthy and matches `AGENTS.md`.

The largest technical debt is concentrated in **two places**:

1. **The AI subsystem carries a large amount of parallel/legacy infrastructure.** There are two conversation/session/history layers (`ai/conversation/` vs `ai/runtime/`), two AI-configuration systems (`ai/config/` `ConfigManager` vs `ai/config_store.py`), and a 13-provider registry where several providers (zai, sambanova, nvidia, cohere, siliconflow, fireworks) are not provisioned in `render.yaml`. A handful of modules are only imported by tests.
2. **The runtime/recovery layer has accumulated dormant and duplicated monitoring code.** The supervisor's own `_watchdog_loop` (RPC heartbeat, helper restart, loop-stall restart, memory-pressure GC) is never started; its responsibilities are duplicated by the active `heartbeat.py`/`keepalive.py`/`failsafe.py` loops. `managed_task.py` and `helper/watchdog.py` are fully orphaned. `tg_retry.py` and `startup_check.py` are test-only.

There are also stray artifacts at the repo root (`supervisor_content.txt`, `vite.config.ts.timestamp-*.mjs`, `remote_readme.md`) and dead/duplicated environment variables (`DEST_CHANNEL_ID`, `DATABASE_URL`, `GHOST_ROOM_ID`).

How much is healthy: the Save/Delete/Retrieve/Discover/Database/Settings/Bio/Username service+handler+tool layers are largely active, single-authority, and well covered by tests. The final self-only Delete ownership chokepoint is intact (confirmed in `delete_service.py` + ownership tests) and must not be touched.

---

## 2. KEEP

These systems are active, architecturally justified, and must NOT be removed.

- **`backend/runtime/supervisor.py` — `RuntimeSupervisor`** — single recovery authority; owns client/helper/web/run-loop lifecycle. Keep. (Its dormant `_watchdog_loop` is reported separately in sections 7/8.)
- **`backend/main.py` / `backend/config.py`** — deterministic entry point and env loader. Keep (config has dead keys, see section 12).
- **`backend/runtime/task_guard.py`** — `guarded_create_task` / `immortal_create_task`; used everywhere. Keep.
- **`backend/runtime/heartbeat.py`, `keepalive.py`, `failsafe.py`, `states.py`, `tracer.py`, `crash_diagnostics.py`, `memory_cleanup.py`** — active, started by `supervisor.start()`. Keep.
- **`backend/runtime/diagnostics.py`** — active diagnostics loop (started by supervisor). Distinct from the top-level event-log module.
- **`backend/diagnostics.py`** — active in-memory event log (`record_event`, `filter_events`); imported by ~20 modules. Keep.
- **`backend/health.py`** — active runtime health/telemetry store (17 importers). Keep.
- **`backend/bot/`** — `client.py`, `router.py`, and handlers `guard`, `misc`, `save`, `retrieve`, `delete`, `discover`, `database`, `bio`, `username`, `ai`, `ai_unified`. All wired through `router.register_all`. Keep (see section 7 for the `organize.py` no-op stub).
- **`backend/services/`** — `save_service`, `retrieve_service`, `delete_service`, `discover_service`, `database_service`, `settings_service`, `bio_service`, `username_service`. Active, each has a handler and/or AI tool caller. Keep.
- **`backend/profile/engine.py` + `backend/profile/scheduler.py`** — shared parameterized engine + single minute-boundary scheduler. Keep.
- **`backend/bio/engine.py`, `backend/username/engine.py`** — thin wrappers. Keep (correct boundary layer, not duplication).
- **`backend/ai/engine/`** (`engine.py`, `dispatcher.py`, `hooks.py`, `metrics.py`, `result.py`) — the canonical AI execution spine. Keep.
- **`backend/ai/tools/`** — `base`, `registry`, `executor`, `context`, and the per-domain tools (`save`, `delete`, `retrieve`, `bio`, `username`, `database`, `settings`, `account`, `semantic`). `create_default_registry` wires ~34 tools. Keep (see section 6 for the `organize` half-migration).
- **`backend/ai/providers/`** — `base/`, `factory.py`, `openai_compat.py`, `dummy/`, `gemini.py`, `openai.py`, `openrouter.py`, and the OpenAI-compatible wrappers. Active. Keep (see section 6 for the un-provisioned providers).
- **`backend/ai/session/request.py` (`AIRequest`)** — canonical input type. Keep.
- **`backend/ai/context/reply_resolver.py`** — active reply-context resolution for trigger/reply activation. Keep.
- **`backend/ai/config_store.py`** — the authoritative Supabase-backed AI config used by handlers/web/chat. Keep.
- **`backend/ai/persistence.py`, `backend/ai/diagnostics.py`, `backend/ai/database/`** — active (persistence called by `runtime/manager.py` and `tools/executor.py`; diagnostics called by dispatcher/heartbeat/ai_unified; repositories used by runtime + observability + maintenance).
- **`backend/helper/`** — `client`, `panels`, `panel_render`, `panel_registry`, `inline_engine`, `inline_sender`, `input_state`, `lifecycle`, `session_manager`, `callback_trace`, `target_context`, `context`, `panel_settings`, `panel_timer`, `rpc_timeout`. Active. Keep (see section 7 for the three orphan helper files).
- **`backend/telegram_api/`** — typed RPC wrappers (`api`, `messages`, `media`, `entities`, `exceptions`, `_helpers`) used by the AI `TelegramAPI` tool context and supervisor wiring. Keep.
- **`backend/web/app.py`** — FastAPI server + read-only dashboard API + SPA static serving. Keep.
- **`backend/db/client.py`** — Supabase singleton + in-memory fallback. Keep.
- **`backend/observability/`** — `runtime_status`, `ai_stats`, `db_stats`, `health_snapshot`, `performance`, `maintenance` are consumed by web endpoints and/or runtime. Keep (see section 7 for `crash_report.py`).

---

## 3. KEEP BUT CLEAN

Active systems that work but carry unnecessary complexity or duplication.

- **AI configuration is duplicated across two systems.**
  - System A: `backend/ai/config/` — `ConfigManager`, `AIConfig`, `ConfigSnapshot`, `defaults.py`, `env.py`, `manager.py`, `schema.py`, `validation.py` (in-RAM, env-driven).
  - System B: `backend/ai/config_store.py` — Supabase-backed config used by the handlers, web app, `ai_unified.py`, and `model_tester.py`.
  - Evidence: `backend/ai/config/` is imported from production code only by `backend/ai/runtime/report.py::_check_configuration()` (health check). The actual runtime config flows through `config_store.py` plus direct `os.getenv` reads in `providers/factory.py`, `discovery.py`, and `model_discovery.py`.
  - Classification: KEEP BUT CLEAN (or REVIEW). Do not delete `ai/config/` yet; first route the report health check to `config_store` and delete `env.py`'s dead Claude/GLM keys (section 12).
- **Two conversation/session/history layers.**
  - `backend/ai/conversation/` — `conversation.py` (`ConversationManager`), `session.py` (`ConversationSession`, `SessionManager`), `history.py` (`HistoryEntry`, `HistoryManager`), `context_builder.py`, `state.py`.
  - `backend/ai/runtime/` — `manager.py` (`ConversationManager`), `session.py` (`RuntimeSession`), `history.py` (`ConversationHistory`), `registry.py`, `tokens.py`, `report.py`.
  - Evidence: the engine/dispatcher use **`ai/runtime/manager.py`**. `ai/conversation/context_builder.py`, `history.py`, and `state.py` are still imported by the dispatcher and prompt builder, but `ai/conversation/conversation.py` is referenced only by `conversation/__init__.py` and `tests/conftest.py`. `ai/conversation/session.py` is imported by `context_builder.py` and `conversation.py`.
  - Classification: KEEP BUT CLEAN — consolidate the two managers/history types; `conversation/conversation.py` is the strongest remove candidate within the active layer.
- **Provider-management has several overlapping layers** — `ProviderFactory`, `ProviderRegistry` (`registry/registry.py`), `ProviderManager` (`manager/manager.py`), `ProviderConfigManager` (`manager/config_manager.py`), `manager/health.py`, `manager/metrics.py`. Each has a distinct call site, so this is justified layering, but it is a lot of surface area for a single-owner bot. KEEP BUT CLEAN.
- **Health/snapshot responsibility is split across three modules** — `backend/health.py` (timestamp/state store), `backend/runtime/health_check.py` (`unified_snapshot` + `_CHECKS`), `backend/observability/health_snapshot.py` (thin wrapper). `tests/test_08_observability.py::test_no_duplicated_health_checks` already enforces the boundary, so this is intentional but still spread out. KEEP BUT CLEAN.
- **Diagnostics naming collision** — `backend/diagnostics.py` (event log) vs `backend/runtime/diagnostics.py` (periodic loop) vs `backend/runtime/crash_diagnostics.py` (crash snapshot + exit reason). Distinct responsibilities, but the names invite confusion. KEEP BUT CLEAN (consider renaming `runtime/diagnostics.py`).
- **Pagination is re-implemented inline** in `bot/handlers/ai.py` (Prev/Next buttons for the model list) even though `helper/pagination.py` exists (unused). Consolidate onto one helper. KEEP BUT CLEAN.

---

## 4. REMOVE CANDIDATES

For each candidate: exact path, purpose, why it appears obsolete, imports/usages, runtime reachability, tests, dependencies, evidence, confidence, and removal risk.

### 4.1 `backend/runtime/managed_task.py`
- **Purpose:** `ManagedTask` — a supervised asyncio task with its own watchdog that restarts the task on unexpected exit.
- **Why obsolete:** the runtime uses `guarded_create_task` / `immortal_create_task` (`runtime/task_guard.py`) everywhere. `ManagedTask` duplicates that responsibility.
- **Imports/usages:** `grep -rn "ManagedTask|managed_task" backend/ tests/` returns only the module's own definition. No importer anywhere.
- **Runtime reachability:** none. Not imported by the supervisor or any handler.
- **Tests:** none.
- **Dependencies:** none beyond stdlib + `task_guard`/`tracer`.
- **Evidence:** CONFIRMED (zero importers).
- **Confidence:** CONFIRMED.
- **Removal risk:** Low. Only risk is if a future refactor intends to adopt it; it is documented in `README.md` directory tree, which would need updating.

### 4.2 `backend/helper/watchdog.py`
- **Purpose:** helper-bot watchdog loop. Its docstring claims it "delegates to the RuntimeSupervisor", but the code is a self-contained loop that checks `helper.client.get_client()` and marks a permanent failure after 3 consecutive disconnects.
- **Why obsolete:** `start()` is never called; no module imports it. Helper health/recovery is actually handled by the supervisor's own logic + heartbeat invariant (section 8).
- **Imports/usages:** `grep` for `helper.watchdog`/`import watchdog` finds no importers; the only `"lifeos-helper-watchdog"` strings are task names in supervisor/heartbeat, not references to this file.
- **Runtime reachability:** none.
- **Tests:** none.
- **Dependencies:** none.
- **Evidence:** CONFIRMED (no `start()` caller, no importer).
- **Confidence:** CONFIRMED.
- **Removal risk:** Low. Note: helper recovery ownership is a real concern, but this file is not the mechanism in use.

### 4.3 `backend/helper/pagination.py`
- **Purpose:** reusable `build_pagination_row` / `paginate` for inline panels.
- **Why obsolete:** zero importers; `bot/handlers/ai.py` builds pagination inline.
- **Imports/usages:** none.
- **Runtime reachability:** none.
- **Tests:** none.
- **Evidence:** CONFIRMED.
- **Confidence:** CONFIRMED.
- **Removal risk:** Low.

### 4.4 `backend/helper/panel_selftest.py`
- **Purpose:** panel self-test helper (imports `settings_service`).
- **Why obsolete:** zero importers; no panel/action wires it.
- **Imports/usages:** none (only its own import of `settings_service`).
- **Runtime reachability:** none.
- **Tests:** none.
- **Evidence:** CONFIRMED.
- **Confidence:** CONFIRMED.
- **Removal risk:** Low.

### 4.5 `backend/runtime/tg_retry.py`
- **Purpose:** `tg_rpc` — bounded/retried Telegram RPC helper.
- **Why obsolete/dormant:** only `tests/test_06_failure_simulation.py` imports it. Production RPC paths use `telegram_api/` + `operation_watchdog.guarded_await` instead.
- **Imports/usages:** tests only.
- **Runtime reachability:** none in production.
- **Tests:** yes — `tests/test_06_failure_simulation.py` (timeout/retry/cancel).
- **Evidence:** CONFIRMED dormant.
- **Confidence:** CONFIRMED (dormant in prod; AGENTS.md already documents this).
- **Removal risk:** Low, but removing it means also removing its tests. It is "useful logic under test" per AGENTS.md — REVIEW before deleting.

### 4.6 `backend/runtime/startup_check.py`
- **Purpose:** `run_startup_checks(cfg)` — filesystem/network/env startup validation returning a `StartupReport`.
- **Why obsolete/dormant:** only `tests/test_06_failure_simulation.py` calls it; `main.py`/supervisor never invoke it. It is also the only production consumer of `GHOST_ROOM_ID`.
- **Imports/usages:** tests only.
- **Runtime reachability:** none.
- **Tests:** yes (missing-env + valid-env).
- **Evidence:** CONFIRMED dormant.
- **Confidence:** CONFIRMED.
- **Removal risk:** Low, but delete its tests too. AGENTS.md documents it as dormant.

### 4.7 `backend/runtime/operation_watchdog.py` (partial)
- **Purpose:** `guarded_await` (active) + `bounded_operation` context manager + `attach_task` (dead).
- **Why partially obsolete:** `guarded_await` is imported by 6 modules. `bounded_operation` and `attach_task` appear only in the module's own docstring/example — no callers.
- **Evidence:** CONFIRMED for the dead parts; AGENTS.md documents `bounded_operation`/`attach_task` as having no callers.
- **Confidence:** CONFIRMED.
- **Removal risk:** Low for `bounded_operation`/`attach_task` only. Keep `guarded_await`.

### 4.8 `backend/observability/crash_report.py`
- **Purpose:** crash-report generation.
- **Why obsolete:** only `tests/test_08_observability.py` imports it; production crash handling lives in `backend/runtime/crash_diagnostics.py`.
- **Evidence:** LIKELY (production import not found; test-only).
- **Confidence:** LIKELY (verify before deleting).
- **Removal risk:** Low-Medium — confirm it isn't intended as the dashboard "crash report" surface before removal.

### 4.9 `backend/bot/handlers/organize.py`
- **Purpose:** no-op stub ("Organizer — removed … kept as a no-op stub so the router import doesn't break").
- **Why obsolete:** `register()` is `pass`; all Organizer functionality moved elsewhere (AI `organize` tools + `organize_service` still exist).
- **Imports/usages:** imported by `router.py` and registered in `register_all`.
- **Runtime reachability:** yes (register no-op).
- **Tests:** none.
- **Evidence:** CONFIRMED stub.
- **Confidence:** CONFIRMED.
- **Removal risk:** Low, but requires deleting the `organize` entry from `router.py` (a small production edit). This is a "remove after wiring cleanup" item.

### 4.10 `backend/ai/conversation/conversation.py` (and its `SessionManager`/`HistoryManager` wrappers)
- **Purpose:** legacy `ConversationManager` over `SessionManager`/`HistoryManager`.
- **Why obsolete:** the active engine/dispatcher use `backend/ai/runtime/manager.py::ConversationManager`. `conversation/conversation.py` is imported only by `conversation/__init__.py` re-export and `tests/conftest.py`.
- **Evidence:** LIKELY (still re-exported from package `__init__`, so a static "no importer" claim is not literally true, but no runtime consumer).
- **Confidence:** LIKELY.
- **Removal risk:** Medium — consolidate tests and the `__init__` re-exports first.

### 4.11 Root artifact: `supervisor_content.txt`
- **Purpose:** a stray plain-text copy of an earlier version of `backend/runtime/supervisor.py`.
- **Why obsolete:** not referenced by any code; `grep -rn "supervisor_content"` finds no hits.
- **Evidence:** CONFIRMED.
- **Confidence:** CONFIRMED.
- **Removal risk:** None (pure artifact).

### 4.12 Root artifact: `vite.config.ts.timestamp-1786730483267-5bfa955febabd8.mjs`
- **Purpose:** Vite-generated timestamped config bundle (build/dev artifact).
- **Why obsolete:** a transient file Vite emits; the real config is `vite.config.ts`.
- **Evidence:** CONFIRMED.
- **Confidence:** CONFIRMED.
- **Removal risk:** None.

### 4.13 Root doc: `remote_readme.md`
- **Purpose:** an older README variant (965 lines).
- **Why obsolete:** not referenced by code; only the previous `INVESTIGATION.md` mentions it. `README.md` is the canonical README.
- **Evidence:** CONFIRMED (doc-only).
- **Confidence:** CONFIRMED.
- **Removal risk:** None (documentation only; reconcile any unique content into README first).

---

## 5. DUPLICATE SYSTEMS

### 5.1 AI configuration — `ai/config/` vs `ai/config_store.py`
- **Implementation A:** `backend/ai/config/` (`ConfigManager`, `AIConfig`, `env.py`, `defaults.py`, `schema.py`, `validation.py`).
- **Implementation B:** `backend/ai/config_store.py` (Supabase-backed get/save/update).
- **Responsibility:** AI configuration source of truth.
- **Actual call paths:** B is used by `bot/handlers/ai.py`, `bot/handlers/ai_unified.py`, `web/app.py`, `model_tester.py`, and the tool context. A is used only by `ai/runtime/report.py::_check_configuration()` (health check) and re-exported via `ai/config/__init__.py`. Provider key/model env vars are additionally read directly in `providers/factory.py`, `discovery.py`, and `model_discovery.py`.
- **Authoritative:** `config_store.py` (runtime) — `ai/config/` is a legacy in-RAM/env config layer.
- **Can A be removed?** After re-pointing `report.py::_check_configuration()` to `config_store` (and removing `env.py`'s dead Claude/GLM entries), likely yes.
- **Confidence:** CONFIRMED duplication; removal LIKELY safe after the one call-site change.

### 5.2 Conversation/session/history — `ai/conversation/` vs `ai/runtime/`
- **Implementation A:** `backend/ai/conversation/` (`ConversationManager`, `ConversationSession`/`SessionManager`, `HistoryEntry`/`HistoryManager`, `context_builder`, `state`).
- **Implementation B:** `backend/ai/runtime/` (`ConversationManager`, `RuntimeSession`, `ConversationHistory`, `registry`, `tokens`, `report`, `session`).
- **Responsibility:** in-memory conversation session + bounded history.
- **Actual call paths:** engine/dispatcher use B (`ai/runtime/manager.py`). A's `context_builder.py` is still imported by the dispatcher, prompt builder, `ai_unified.py`, and `session/request.py`; A's `state.py` and `history.py` types are also imported by the dispatcher.
- **Authoritative:** `ai/runtime/` for session/history storage; `ai/conversation/context_builder.py` for context assembly. `ai/conversation/conversation.py` (`ConversationManager`) is non-authoritative.
- **Can A's manager be removed?** `conversation/conversation.py` likely; the rest of `conversation/` must be carefully disentangled from `context_builder`.
- **Confidence:** CONFIRMED duplication; per-file removal LIKELY.

### 5.3 Recovery watchdog logic — supervisor `_watchdog_loop` vs active loops
- **Implementation A:** `RuntimeSupervisor._watchdog_loop` (in-class: `get_me` RPC heartbeat, helper restart, loop-stall restart, `/proc` memory-pressure GC, `_trigger_reconnect`/`_trigger_full_recovery`).
- **Implementation B:** `runtime/heartbeat.py` (invariant + dispatch-stall recovery), `runtime/keepalive.py` (RPC timeout recovery), `runtime/failsafe.py` (hard reset).
- **Responsibility:** detecting failure and triggering recovery.
- **Actual call paths:** A is **never started** (`start()` calls `start_heartbeat/start_keepalive/start_failsafe`, not `_watchdog_loop`). B is active.
- **Authoritative:** B.
- **Can A be removed?** Yes — `_watchdog_loop` and the `_last_watchdog_tick` bookkeeping it uses are dormant. Remove carefully so `_consecutive_failures` (also bumped by keepalive) stays consistent.
- **Confidence:** CONFIRMED dormant duplicate.

### 5.4 Helper recovery — `helper/watchdog.py` vs supervisor/heartbeat
- **Implementation A:** `backend/helper/watchdog.py` (`start()` never called).
- **Implementation B:** supervisor `_start_helper` (startup) + `_do_recovery`/`_hard_reset_runtime` (restart on full recovery) + heartbeat `READY_BUT_DISCONNECTED` invariant (triggers `_trigger_reconnect` when helper is enabled but disconnected).
- **Authoritative:** B (but see section 8: helper restart on a *lightweight* reconnect is a REVIEW gap).
- **Can A be removed?** Yes.
- **Confidence:** CONFIRMED.

### 5.5 Diagnostics modules — names overlap, responsibilities differ
- `backend/diagnostics.py` (event log) vs `backend/runtime/diagnostics.py` (loop) vs `backend/runtime/crash_diagnostics.py` (crash snapshot).
- These are **not** true duplicates (distinct roles), but the naming is confusing. Report as KEEP BUT CLEAN, not remove.

### 5.6 SQL sources — `sql/` vs `supabase/migrations/`
- **Implementation A:** `sql/` (README + `bio_state.sql`, `bot_logs.sql`, `panel_settings.sql`, `persist_active_state.sql`, `saved_items.sql`, `username_state.sql`).
- **Implementation B:** `supabase/migrations/` (9 timestamped migrations).
- **Responsibility:** schema definition.
- **Authoritative:** `supabase/migrations/` (applied migrations). `sql/persist_active_state.sql` has **no corresponding migration** — it appears to be an abandoned/older consolidated table.
- **Confidence:** CONFIRMED drift (`persist_active_state`); overall `sql/` directory LIKELY stale duplicate.

---

## 6. HALF-IMPLEMENTED SYSTEMS

1. **Organize feature is half-migrated.** `bot/handlers/organize.py` is a no-op stub, but `ai/tools/organize.py` (`OrganizeListTool`, `OrganizeCleanTool`) and `services/organize_service.py` still exist and are wired into the AI tool registry. So Organize works only through AI tool calls, not through any Glass UI panel. State: handler stub + live service/tool. Missing connection: either a real panel or full removal of the panel entry.
2. **Helper watchdog claims delegation it does not perform.** `helper/watchdog.py` docstring says "delegates to RuntimeSupervisor", but the code is a standalone loop that is never started and never references the supervisor. It is both dormant and misdescribed.
3. **`sql/persist_active_state.sql`** defines a table with no migration in `supabase/migrations/`. Likely an abandoned persistence experiment. Missing: a migration, or confirmation it is obsolete.
4. **Six providers are registered but not provisioned for deploy.** `factory.py` registers `zai`, `sambanova`, `nvidia`, `cohere`, `siliconflow`, `fireworks`, but `render.yaml` declares keys only for gemini/openai/openrouter/groq/cerebras/mistral. These six are reachable in code only if someone manually sets their env vars in Render; they are effectively half-wired for the production deploy.
5. **`ai/config/env.py` references providers that do not exist.** It reads `AI_CLAUDE_API_KEY`/`AI_CLAUDE_MODEL` and `AI_GLM_API_KEY`/`AI_GLM_MODEL`, but there is no `claude` or `glm` provider in `factory.py`. Dead configuration in a partially-used loader.
6. **Frontend `SavedItem.save_type` still models `'forward' | 'deep'`** (`src/lib/api.ts`), but the backend is Deep Save only (forward exists only for retrieval). Stale frontend model.
7. **Observability API endpoints have no frontend consumer.** `web/app.py` exposes `/api/status`, `/api/ai/stats`, `/api/db/stats`, `/api/health/snapshot`, `/api/performance`, `/api/diagnostics/events`, `/api/maintenance`, `/api/settings`, and `/api/ai/models/{provider}` — none are called by `src/lib/api.ts`. They are backend-only/debug surfaces.

---

## 7. DEAD CODE / ORPHANS

### Confirmed dead (no production importer/caller)

| Path | Evidence |
|---|---|
| `backend/runtime/managed_task.py` | 0 importers |
| `backend/helper/watchdog.py` | 0 importers, `start()` never called |
| `backend/helper/pagination.py` | 0 importers |
| `backend/helper/panel_selftest.py` | 0 importers |
| `backend/runtime/tg_retry.py` | test-only |
| `backend/runtime/startup_check.py` | test-only |
| `backend/runtime/operation_watchdog.py` (`bounded_operation`, `attach_task`) | only self-referenced; `guarded_await` is active |
| `RuntimeSupervisor._watchdog_loop` + `_last_watchdog_tick` | never started; duplicated by heartbeat/keepalive |
| `supervisor_content.txt` | 0 references; duplicate of supervisor source |
| `vite.config.ts.timestamp-*.mjs` | Vite artifact |
| `remote_readme.md` | doc-only, 0 code references |
| `backend/bot/handlers/organize.py` | no-op stub (`register` = `pass`) |
| `backend/ai/config/env.py` Claude/GLM blocks | `AI_CLAUDE_*`/`AI_GLM_*` read, but no such providers |

### Likely dead (needs one more verification)

| Path | Evidence |
|---|---|
| `backend/observability/crash_report.py` | only `tests/test_08_observability.py` imports it |
| `backend/ai/conversation/conversation.py` (`ConversationManager`) | only package `__init__` re-export + `tests/conftest.py` |
| `backend/ai/conversation/session.py` (`SessionManager`) | only used by `context_builder.py` (type) and `conversation.py` |
| `sql/persist_active_state.sql` | table absent from `supabase/migrations/` |

### Dormant-but-tested (documented in AGENTS.md, left intentionally)

- `backend/runtime/tg_retry.py` (`tg_rpc`)
- `backend/runtime/startup_check.py` (`run_startup_checks`)
- `backend/runtime/operation_watchdog.py` (`bounded_operation`, `attach_task`)

These are "dormant in prod, tested" — flag for a conscious keep-or-remove decision rather than silent deletion.

---

## 8. RUNTIME / RECOVERY AUDIT

**Current architecture (active):**

- `main.py` → `RuntimeSupervisor.start()`
  - `build_client` → `register_all` → `_wire_ai_tools`
  - `_start_helper` (if helper_enabled)
  - `_resume_bio_cron` / `_resume_username_cron`
  - `_start_web_server`
  - `start_heartbeat` / `start_keepalive` / `start_failsafe` / `start_diagnostics` / `start_memory_cleanup`

- **Heartbeat** (30s): snapshot + `READY_BUT_DISCONNECTED` invariant + `EVENT_DISPATCH_STALLED` detection → `_trigger_reconnect`.
- **Keepalive** (60s): `get_me` RPC → on timeout/failure bumps `_consecutive_failures` and triggers `_trigger_reconnect`.
- **Failsafe** (15s check / 120s freeze): if heartbeat/update/RPC/dispatch timestamps all freeze → `_hard_reset_runtime`.
- **Reconnect/rebuild/full recovery** serialize through `RuntimeSupervisor._recovery_lock` with reconnect/full cooldowns; `_run_loop` yields to recovery when the lock is held. This is correct and matches the single-authority rule.

**Findings:**

1. **CONFIRMED — `RuntimeSupervisor._watchdog_loop` is dormant.** It is a substantial duplicate of the active loops (its own `get_me` RPC heartbeat, helper restart, loop-stall restart, and `/proc` memory-pressure GC). It is never started. Remove it, or rename/clarify if any of its behaviors (helper restart on lightweight reconnect, loop-stall restart) are still wanted. `_last_watchdog_tick` and the watchdog-only `_consecutive_failures` logic are part of this dead path (note: keepalive also increments `_consecutive_failures`).
2. **CONFIRMED — `helper/watchdog.py` is orphaned** (no `start()` caller). Helper recovery currently relies on: (a) `_start_helper` at boot, (b) full recovery `_do_recovery`/`_hard_reset_runtime` restarting the helper, and (c) the heartbeat invariant triggering `_trigger_reconnect`.
3. **REVIEW — helper restart on a *lightweight* reconnect.** `_trigger_reconnect` reconnects only the **self** client; it does not restart the helper. If the helper bot dies while enabled, the heartbeat raises `READY_BUT_DISCONNECTED` → `_trigger_reconnect` → self reconnects but the helper stays down → invariant persists → reconnect cooldown → eventually a full recovery (which does restart the helper). This is eventually self-healing but is an indirect path. Worth a targeted review (not this investigation).
4. **CONFIRMED — `managed_task.py` is unused**; task lifecycle is provided by `task_guard.py`.
5. **CONFIRMED — `tg_retry.py` and `startup_check.py` are test-only**; production RPC safety comes from `telegram_api/*` + `operation_watchdog.guarded_await`.

No second supervisor exists. No new supervisor or recovery architecture should be introduced. The correct cleanup is to **consolidate the dormant in-supervisor watchdog and orphan helper watchdog into the three active loops**, not to add more monitors.

Distinction (per audit requirement): the heartbeat/keepalive/failsafe are **protection + diagnostics** — they detect symptoms and trigger the supervisor's recovery methods. They do not themselves prove a root cause; root-cause logging is captured by `tracer.py`/`crash_diagnostics.py`.

---

## 9. AI AUDIT

**Active and correct:** `Engine`/`Dispatcher` execution spine; `AIRequest`; deterministic fast path (`actions.py` → ToolExecutor without a provider round); `ToolRegistry`/`ToolExecutor` with `long_running` exemption; `ToolContext` carrying the single `TelegramAPI` wrapper; provider factory/manager/registry; `openai_compat` base; memory tiers; repository manager; `config_store`; `ai_unified.py` trigger/reply activation; `reply_resolver`.

**Redundant / obsolete / incomplete:**

1. **Two AI config systems** (section 5.1): `ai/config/` (`ConfigManager` + `env.py`) vs `ai/config_store.py`. `ai/config/` is effectively a health-check-only legacy layer.
2. **Two conversation/session/history layers** (section 5.2): `ai/conversation/` vs `ai/runtime/`. `conversation/conversation.py` (`ConversationManager`) is legacy.
3. **Dead provider env entries** in `ai/config/env.py`: `AI_CLAUDE_*`, `AI_GLM_*` — no Claude/GLM providers exist.
4. **Six providers not provisioned in `render.yaml`** (section 6.4) — registered in code, not configurable in the declared deploy.
5. **No arbitrary-access risk found** — AI tools go through `telegram_api.TelegramAPI` and the service layer; the Self Bot remains the only Telegram execution authority; no tool grants SQL/shell/credential access. This is healthy and must be preserved.

**Do not redesign the AI architecture.** Cleanup scope is limited to: dedupe config, consolidate conversation managers, drop the dead Claude/GLM env entries, and decide the fate of un-provisioned providers.

---

## 10. DELETE / SAVE / PROFILE AUDIT

### Delete
- Current semantic/structural delete path: `ai/actions.py` → `ai/semantic_delete.py` → `ai/tools/delete.py` + `ai/tools/semantic.py` → `services/delete_service.py`.
- Legacy Delete paths: the Glass UI `bot/handlers/delete.py` + `delete_service.do_del_last_n_real`/`do_del_by_id` remain active. No duplicate/obsolete delete service found — there is one service with several mode entry points (reply-from, recent, manual-id, by-message-id, semantic).
- **Ownership chokepoint intact:** `delete_service.py` re-validates ownership immediately before Telegram deletion; tests `test_27_delete_ownership.py`, `test_28_delete_regression.py`, `test_29_delete_expansion.py`, `test_30_delete_timeout_hardening.py`, `test_31_delete_rpc_failures.py`, `test_32_semantic_delete.py` cover it. **Do not modify this boundary.**
- Finding (cosmetic): `delete_service` has many entry-point helpers; consolidate if maintainability demands it, but there is no proven dead Delete path.

### Save
- Deep Save only: `services/save_service.py::execute_save` downloads → re-uploads as a NEW Saved Messages message → persists. No `forward_messages` for saving.
- `forward_messages` appears **only** in `services/retrieve_service.py` (correct: re-sending a saved asset) and in the `telegram_api` wrappers. Confirmed no Forward Save remnant in the save path.
- Save entry points: `bot/handlers/save.py` (Glass UI), `ai/tools/save.py` (`SaveTool`/`SaveByLinkTool`). Both call the same `save_service`. No duplicate persistence path found.
- Finding (frontend drift): `src/lib/api.ts::SavedItem.save_type` still allows `'forward'` — stale model only.

### Profile
- Healthy: `bio/engine.py` and `username/engine.py` are thin wrappers over `profile/engine.py::ProfileEngine`; both register with the single `profile/scheduler.py`. Turning one engine off does not stop the other (`stop_if_idle`). Matches AGENTS.md.
- `profile/scheduler.py` sends ONE `UpdateProfileRequest` per minute. No duplicate scheduler found.
- No obsolete profile utility found. Bio/Username service + tool + handler layers are all active.

---

## 11. PANEL / UI AUDIT

- Glass UI machinery (`helper/panels.py`, `panel_render.py`, `panel_registry.py`, `inline_engine.py`, `inline_sender.py`, `input_state.py`, `lifecycle.py`, `session_manager.py`, `callback_trace.py`, `target_context.py`) is active.
- **Orphan helper files:** `helper/pagination.py`, `helper/panel_selftest.py`, `helper/watchdog.py` (section 4).
- **No-op panel handler:** `bot/handlers/organize.py` (registered no-op).
- **Frontend:** `src/App.tsx` uses `SavedItems`, `BioStatus`, `AIConfigPanel`, `LogViewer`; `TriggerConfig` is used by `AIConfigPanel`. No disconnected frontend component.
- **Backend routes without frontend consumers:** the observability endpoints in section 6.7.
- **Stale frontend type:** `SavedItem.save_type: 'forward' | 'deep'` (section 6.6).

---

## 12. CONFIG / ENV AUDIT

Dead or duplicated configuration (no secret values disclosed):

| Variable | Where defined | Consumed by | Status |
|---|---|---|---|
| `DEST_CHANNEL_ID` | `backend/config.py` | nothing | CONFIRMED dead |
| `DATABASE_URL` | `backend/config.py` | nothing | CONFIRMED dead |
| `GHOST_ROOM_ID` | `backend/config.py` | only dormant `runtime/startup_check.py` + its test | CONFIRMED dead-at-runtime |
| `AI_CLAUDE_API_KEY` / `AI_CLAUDE_MODEL` | `ai/config/env.py` | no Claude provider | CONFIRMED dead |
| `AI_GLM_API_KEY` / `AI_GLM_MODEL` | `ai/config/env.py` | no GLM provider | CONFIRMED dead |
| `HELPER_BOT_ENABLED` | `render.yaml` / config docs | `config.py` derives `HELPER_BOT_ENABLED` from `BOT_TOKEN` presence; the explicit env var is not read | LIKELY dead/duplicated flag |
| `AI_PROVIDER` / `AI_ENABLED` / `AI_*_MODEL` / `AI_*_API_KEY` | `render.yaml`, `factory.py`, `discovery.py`, `ai/config/env.py` | read in two different layers (factory/discovery vs `ai/config/env.py`) | CONFIRMED duplicated consumption (not dead) |
| `BIO_UPDATE_ENABLED` / `USERNAME_UPDATE_ENABLED` / `TZ` / `PORT` / `LOG_LEVEL` | `config.py`, `render.yaml` | active | Keep |

Note on `render.yaml`: it declares the six core providers' keys but omits `zai/sambanova/nvidia/cohere/siliconflow/fireworks` keys, which are registered in `factory.py` — config drift (section 6.4).

---

## 13. DEPENDENCY AUDIT

`backend/requirements.txt`:

```
telethon==1.34.0
fastapi==0.111.0
uvicorn[standard]==0.29.0
supabase==2.4.2
aiofiles==23.2.1
httpx==0.27.0
```

| Package | Evidence | Confidence |
|---|---|---|
| `telethon` | core runtime, used broadly | Keep |
| `fastapi` + `uvicorn` | `web/app.py` | Keep |
| `supabase` | `db/client.py` | Keep |
| `httpx` | AI providers (`openai_compat`, `model_discovery`, `model_tester`) | Keep |
| `aiofiles` | **no import found** in `backend/` or `tests/` (deep save uses `asyncio.to_thread`/temp buffers) | LIKELY unused — verify against save_service temp-file handling before removal |

Frontend `package.json`: `react`, `react-dom` + dev deps (`@vitejs/plugin-react`, `tailwindcss`, `postcss`, `autoprefixer`, `typescript`, `vite`, `@types/*`). All appear used by the Vite/Tailwind build. No obsolete frontend dependency identified.

No dependency is currently imported only by dead code except possibly `aiofiles` (to be verified). **No dependency was removed or modified.**

---

## 14. DATABASE / SUPABASE USAGE AUDIT

Inspection only — no SQL executed, no migrations run, no Supabase mutation.

**Tables referenced by `db/client.py` and services (active):**
- `saved_items` — save/retrieve/delete/discover services + `/api/saves`.
- `bio_state` — Bio engine + `/api/bio`.
- `username_state` — Username engine.
- `bot_logs` — `/api/logs` + `record_event` path.
- `panel_settings` — `settings_service` / `panel_settings_repository`.

**AI tables (migration `20260804145402_create_ai_tables.sql` + `20260805075707_*ai_config*`)** — session/message/memory/tool-history/provider-stats/usage/config repositories in `ai/database/` + `config_store.py`. Active when Supabase is configured; fall back to in-memory otherwise.

**Findings:**
- `sql/persist_active_state.sql` defines a table with **no matching migration** and no runtime consumer — LIKELY abandoned (do not delete from Supabase on this evidence alone; DB cleanup is a separate task).
- `sql/` appears to be an older consolidated-SQL mirror of `supabase/migrations/`; treat as documentation drift (section 15).
- No code references a nonexistent table name (all tables referenced by code exist in migrations).
- The in-memory fallback is the intended degrade path; no DB crash path found.

---

## 15. DOCUMENTATION AUDIT

### `README.md` (1477 lines)
- Mostly accurate on the Glass-UI-first, Deep-Save-only, single-`.menu` model.
- **Stale items:**
  - Directory tree lists `backend/runtime/watchdog.py`, `backend/telegram_api/profile.py`, and `backend/helper/*` files that do not exist (`runtime/watchdog.py` does not exist; `telegram_api/profile.py` does not exist).
  - Tree still implies a `runtime/watchdog.py` "30s heartbeat + update staleness" module that is actually now split across `heartbeat.py`/`keepalive.py`/`failsafe.py`.
  - `SavedItem.save_type`/forward-save references in some sections conflict with Deep-Save-only reality.
  - Provider list does not cover all 13 registered providers.
- Classification: DOCUMENTATION FIX.

### `AGENTS.md`
- Accurate on the core runtime/recovery/save/profile/delete architecture and the single-recovery-authority rule.
- **Drift:** the repository layout section is incomplete — it omits many files that actually exist (`ai/actions.py`, `ai/prompt/`, `ai/runtime/`, `ai/session/`, `ai/context/`, `ai/discovery.py`, `ai/model_discovery.py`, `ai/model_tester.py`, `ai/persian.py`, `ai/semantic_delete.py`, the extra providers, `telegram_api/` subpackage, `helper/{pagination,panel_selftest,panel_timer,panel_settings,target_context,callback_trace,context}.py`, `runtime/managed_task.py`, `services/panel_settings_repository.py`).
- Classification: DOCUMENTATION FIX.

### `AI_MASTER_DESIGN.md` (2349 lines)
- A large "V1/V1.1/V1.2 Draft" design doc dated 2026-08-03. Much of it describes the intended AI architecture; in places it describes a stricter Tool-Layer boundary than the code implements (e.g. §29.4 forbids AI importing runtime internals, but `supervisor.py::_wire_ai_tools` constructs `TelegramAPI(self.client)` and passes the raw client into `ToolContext`).
- Classification: DOCUMENTATION FIX / REVIEW (living spec vs implemented code drift).

### `DATABASE_ARCHITECTURE.md`
- Describes "10 tables" (5 core + 5 AI). Should be cross-checked against `supabase/migrations/`; `sql/persist_active_state.sql` is not reflected. Classification: DOCUMENTATION FIX.

### `OBSERVABILITY.md`, `PRODUCTION_CHECKLIST.md`, `PRODUCTION_VERIFICATION.md`, `FREEBUFF_PRE_PUSH_VERIFY.md`
- Not deeply audited here, but `OBSERVABILITY.md` should be checked against the now-dormant supervisor watchdog and the active heartbeat/keepalive/failsafe split. Classification: REVIEW.

### `remote_readme.md`
- Older README duplicate. Classification: REMOVE CANDIDATE (doc).

---

## 16. TEST AUDIT

Test files present: `test_01` … `test_15`, `test_17` … `test_32` (there is **no `test_16`** — numbering gap), plus `test_model_discovery.py` and `test_model_tester.py`.

- **Tests covering dormant code:**
  - `tests/test_06_failure_simulation.py` — `tg_retry.tg_rpc` and `startup_check.run_startup_checks` (both dormant in prod).
  - `tests/test_08_observability.py` — imports `observability/crash_report.py` (test-only in prod).
- **Tests covering the legacy conversation layer:** `tests/conftest.py` fixture `conversation_manager` instantiates `ai/conversation/conversation.py::ConversationManager` (legacy manager).
- **Strong coverage of active critical systems:** Delete ownership/regression/timeout/RPC-failure/semantic tests (`test_27` … `test_32`), save engine (`test_12`), bio/username (`test_15`), providers (`test_17`), fast path (`test_25`), runtime wiring (`test_11`). This is healthy.
- **Missing/weak coverage:**
  - No tests for `helper/pagination.py`, `helper/panel_selftest.py`, `helper/watchdog.py`, `managed_task.py` (they are dead).
  - No test proves the supervisor's dormant `_watchdog_loop` (it is never started).
  - No frontend test suite at all (Vite React components untested).

No test was modified or deleted.

---

## 17. PRIORITIZED CLEANUP PLAN

Do NOT implement this plan now.

**Phase 1 — highest-risk unnecessary/duplicated systems**
1. Decide and remove the dormant `RuntimeSupervisor._watchdog_loop` (+ `_last_watchdog_tick`), after confirming which of its behaviors are still wanted (esp. helper restart and loop-stall restart).
2. Resolve helper-recovery ownership: either wire helper restart into `_trigger_reconnect` or explicitly rely on full recovery; delete `helper/watchdog.py`.
3. Remove `managed_task.py` (or adopt it everywhere — do not keep both task supervisors).
4. Consolidate AI config: point `runtime/report.py::_check_configuration()` at `config_store`; retire `ai/config/`; drop Claude/GLM env entries.

**Phase 2 — safe cleanup**
5. Delete confirmed dead files/artifacts: `supervisor_content.txt`, `vite.config.ts.timestamp-*.mjs`, `helper/pagination.py`, `helper/panel_selftest.py`, `helper/watchdog.py`, `runtime/managed_task.py`.
6. Remove dead env keys from `config.py` (`DEST_CHANNEL_ID`, `DATABASE_URL`, `GHOST_ROOM_ID` after removing/retiring `startup_check.py`).
7. Remove dormant-but-tested modules (`tg_retry.py`, `startup_check.py`) together with their tests, or explicitly keep them.
8. Remove dead parts of `operation_watchdog.py` (`bounded_operation`, `attach_task`).
9. Remove `bot/handlers/organize.py` no-op + its router entry (decide Organize AI-tool fate separately).

**Phase 3 — architecture consolidation**
10. Consolidate `ai/conversation/` vs `ai/runtime/` session/history managers.
11. Decide the fate of the 6 un-provisioned providers and reconcile `render.yaml`.
12. Reconcile `sql/` with `supabase/migrations/` (esp. `persist_active_state.sql`).

**Phase 4 — documentation cleanup**
13. Update `README.md` directory tree (remove nonexistent `runtime/watchdog.py`, `telegram_api/profile.py`, add missing files).
14. Update `AGENTS.md` repository layout to match reality.
15. Reconcile `AI_MASTER_DESIGN.md` tool-boundary claims with `supervisor.py::_wire_ai_tools`.
16. Remove/reconcile `remote_readme.md`.

**Phase 5 — optimization**
17. Verify `aiofiles` usage; remove if unused.
18. Frontend: fix `SavedItem.save_type`, decide observability endpoint consumers.

---

## 18. SAFE TO REMOVE VS NEEDS VERIFICATION

### SAFE / HIGH-CONFIDENCE REMOVE CANDIDATES

- `supervisor_content.txt` (root artifact)
- `vite.config.ts.timestamp-*.mjs` (root artifact)
- `remote_readme.md` (doc duplicate)
- `backend/runtime/managed_task.py`
- `backend/helper/watchdog.py`
- `backend/helper/pagination.py`
- `backend/helper/panel_selftest.py`
- `RuntimeSupervisor._watchdog_loop` + `_last_watchdog_tick` (dormant)
- Dead parts of `backend/runtime/operation_watchdog.py` (`bounded_operation`, `attach_task` — keep `guarded_await`)
- Dead env keys in `config.py`: `DEST_CHANNEL_ID`, `DATABASE_URL` (and `GHOST_ROOM_ID` once `startup_check.py` is retired)
- Dead Claude/GLM env reads in `ai/config/env.py`

### DO NOT TOUCH YET / NEEDS MORE EVIDENCE

- `backend/runtime/tg_retry.py` and `backend/runtime/startup_check.py` (dormant but tested — conscious keep/remove decision first)
- `backend/observability/crash_report.py` (test-only; confirm it is not a planned dashboard surface)
- `backend/ai/config/` package (health-check-only today; must re-point `report.py` first)
- `backend/ai/conversation/conversation.py` + `session.py` (need consolidation with `ai/runtime/` and test migration)
- `sql/persist_active_state.sql` and the broader `sql/` directory (schema cleanup is a separate, careful task)
- The 6 un-provisioned providers (`zai/sambanova/nvidia/cohere/siliconflow/fireworks`) — confirm intent before removal
- `aiofiles` dependency — verify no async file IO before removal
- **The Delete ownership chokepoint** — do not touch under any circumstance
- **RuntimeSupervisor as the single recovery authority** — do not replace or duplicate

---

## 19. UNKNOWN / MISSING EVIDENCE

1. **Live runtime behavior** — this is a static audit; no Telegram connection, Supabase query, or live recovery was exercised. Claims about runtime reachability are based on call-site tracing, not live observation.
2. **Whether the 6 un-provisioned providers are intentionally usable** (env vars set manually in Render outside `render.yaml`) — cannot be determined from the repo.
3. **Whether `aiofiles` is used** via a dynamic/indirect path not caught by `grep` — flagged LIKELY unused, needs one direct check of `save_service` temp-file code.
4. **`AI_ENABLED` semantics** — read by `ai/config/env.py` (legacy) but not by the runtime `config_store`; the effective enablement is `provider/model/trigger` presence. Exact intent is UNKNOWN.
5. **`HELPER_BOT_ENABLED` explicit env var** — `config.py` derives it from `BOT_TOKEN`; whether the explicit flag should still be honored is UNKNOWN.
6. **`sql/persist_active_state`** — whether any deployed DB actually has this table is UNKNOWN (no DB access).
7. **Test pass state at HEAD** — not run during this investigation; test coverage was inferred from imports, not execution.

---

## 20. RECOMMENDED NEXT INVESTIGATION

**Single highest-value next investigation:** trace the **helper-bot failure/recovery ownership end-to-end** (from `_start_helper` → heartbeat `READY_BUT_DISCONNECTED` → `_trigger_reconnect` (self-only) → full recovery) and produce a definitive decision on whether helper restart must be added to the lightweight reconnect path or left to full recovery. This is the one area where a dormant watchdog (`_watchdog_loop`), an orphan watchdog (`helper/watchdog.py`), and an active invariant (heartbeat) all overlap on the same responsibility, and it is a live reliability question that static reachability alone cannot fully resolve.

Do not implement this investigation now.
