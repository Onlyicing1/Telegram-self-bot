# INVESTIGATION

## INVESTIGATION METADATA

- Repository: LifeOS / Telegram Self-Bot
- Branch: `main`
- Current HEAD: `7abfea620e3677d19e5d44c40f1d43b78f6d04ab`
- Investigation date: 2026-08-21
- Scope: Cross-layer dead surface, public API, and contract reachability audit
- Status: Investigation-only; no production source, tests, configuration, dependencies, SQL, migrations, or protected documents were modified.

## 1. EXECUTIVE SUMMARY

The cross-layer audit combined backend public exports, dynamic and registry usage, tests, API routes, frontend callers and types, configuration consumers, data fields, deployment entry points, and operational documentation.

No surface met the complete evidence threshold for behavior-neutral deletion. No source or contract surface was removed.

## 2. SCOPE AUDITED

- Backend public functions/classes and package `__init__.py` exports
- Helper, AI, and Telegram API re-exports and compatibility aliases
- FastAPI endpoints and frontend API methods/callers
- Frontend request/response types and backend response fields
- Environment variables, config fields, derived settings, and Render startup configuration
- Dataclass/model and serialized data-contract fields
- Procfile, Render, ASGI/static serving, package scripts, and SQL/tooling contracts
- Tests, fixtures, documented operational surfaces, and dormant utilities

## 3. CANDIDATES INVESTIGATED

### `backend/helper/panel_settings.py` and helper package exports — POSSIBLY OBSOLETE

The module contains duplicate definitions of `reload` and `auto_close_delay`, and
`backend/helper/__init__.py` imports `reload` twice under the same alias. The
underlying settings functions are active: panel lifecycle/timer code and handler
settings actions consume the canonical `settings_service` behavior. The module is
explicitly described as a compatibility shim, and its exported names form an
indirect public import surface. The duplicate definitions are suspicious, but
removing or consolidating them would be an API/source change without proof that
external or historical imports do not depend on the shim. Preserved.

### `src/lib/api.ts` and FastAPI contracts — ACTIVE

All exported frontend types are consumed by components or API method signatures.
All frontend API methods have current component callers. Backend endpoints used by
the dashboard were matched to their methods and response fields. Operational and
diagnostic endpoints not called by the SPA were preserved because direct API and
operational consumers remain valid. The `SavedItem.save_type` union retains the
historical `forward` value while current deep-save writes use `deep`; database
history and retrieval behavior still expose the broader field, so this was not
proven safe to narrow.

### AI model/provider response contracts — ACTIVE

`ProviderStatus`, `ModelInfo`, `ModelTestResult`, `ModelTestSummary`,
`ModelTestResponse`, and `ProviderModels` are produced by backend discovery/test
routes and consumed by the dashboard. Legacy summary keys are explicitly retained
for compatibility in `model_tester.py`; they were not removed.

### Backend public exports — ACTIVE

`backend.ai`, `backend.helper`, and `backend.telegram_api` exports have runtime,
handler, test, or facade consumers. The Telegram facade is used by AI tools and
supervisor wiring. Helper exports are used by handlers and lifecycle code. AI
exports are used by runtime and tests. No export was proven dead across indirect
imports and public-contract use.

### Configuration and deployment contracts — ACTIVE / INTENTIONALLY DORMANT

`BOT_TOKEN`-derived helper enablement, profile boot flags, Supabase availability,
AI provider/model variables, `PORT`, Procfile, Render startup, and static serving
all participate in runtime or deployment behavior. `AI_ENABLED` is documented and
retained as a deployment/configuration contract even though current runtime
provider selection also uses provider keys and persisted config. Dormant startup
checks and `GHOST_ROOM_ID` remain intentionally retained under prior evidence.

## 4. CLASSIFICATIONS

### PROVEN DEAD

None. No complete producer-to-consumer or public-contract chain was proven absent.

### INTENTIONALLY DORMANT

- `backend/runtime/tg_retry.py`, `backend/runtime/startup_check.py`, and
  `GHOST_ROOM_ID`: tested/documented dormant surfaces preserved under prior
  investigations.
- Crash and diagnostic utilities: retained for operational and reconstruction use.
- Operational FastAPI endpoints without current SPA callers: retained as direct
  API/diagnostic surfaces.

### POSSIBLY OBSOLETE

- Duplicate definitions and duplicate import aliases in
  `backend/helper/panel_settings.py` / `backend/helper/__init__.py`: suspicious
  maintenance residue, but the compatibility module and exported names prevent a
  behavior-neutral deletion decision.
- The historical `forward` member of the frontend `SavedItem.save_type` type:
  current saves are deep-only, but persisted historical records and retrieval
  semantics prevent proving the field value impossible.
- `AI_ENABLED` documentation/configuration: not used as the sole runtime gate in
  the current path, but retained as a deployment and compatibility contract.

### ACTIVE

- Frontend/backend dashboard methods and response fields
- AI provider/model discovery and test contracts
- Public package exports and Telegram/helper facades
- Render/Procfile/ASGI startup and static asset contracts
- Supabase fallback/configuration contracts
- Handler, tool, panel, and test fixture registration surfaces

## 5. PRESERVED SURFACES

Protected architecture and operational documents, SQL/migrations, deployment
configuration, public package exports, operational endpoints, compatibility
shims, dormant tested utilities, database-facing fields, frontend response
models, and runtime configuration were preserved. No source behavior was changed.

## 6. UNKNOWN / NOT PROVEN

The audit did not prove whether any external consumer outside the repository
imports the compatibility shim, consumes operational endpoints, or depends on
legacy serialized fields. It also did not prove that historical `forward` save
records are absent from production data. Those unknowns prevent deletion or
contract narrowing.

## 7. RECOMMENDED NEXT STEP

No cleanup implementation is justified from this cross-layer audit. The helper
compatibility shim may be reviewed in a separately scoped API-compatibility task,
but it should not be altered as dead-code cleanup without an explicit public API
migration decision and evidence about external consumers.

## 8. VALIDATION

- Baseline git status, local HEAD, and `origin/main`: clean and synchronized at
  `7abfea620e3677d19e5d44c40f1d43b78f6d04ab`.
- Baseline full suite: **571 passed, 1 failed, 1 warning**.
- Export, route, frontend API/type, config, response-field, deployment, and
  compatibility-reference searches completed.
- `.venv/bin/python -m pytest tests/ -q --asyncio-mode=auto`: **571 passed, 1 failed, 1 warning**.
- Known failure unchanged:
  `tests/test_31_delete_rpc_failures.py::test_tehran_local_cutoff_is_converted_against_message_timezone`.

---

# INVESTIGATION — Semantic Duplication, Compatibility Shim, and Conflicting Definition Audit

## INVESTIGATION METADATA

- Repository: LifeOS / Telegram Self-Bot
- Branch: `main`
- Current HEAD: `39adfe4491aeeb869f86a7b0a0f48788365c66d8`
- Investigation date: 2026-08-21
- Scope: Semantic duplication, compatibility shims, and conflicting definitions
- Status: Investigation-only; no production source, tests, configuration, dependencies, SQL, migrations, or protected documents were modified.

## 1. EXECUTIVE SUMMARY

Repository-wide AST scans found exactly one module with shadowed duplicate
definitions (`backend/helper/panel_settings.py`: `reload` and
`auto_close_delay` defined twice each) and exactly one duplicate import alias
(`backend/helper/__init__.py`: `reload as reload_settings` imported twice).

Both shadowed duplicates are provably unreachable as distinct bindings (the
second identical definition always wins), so they are recorded as
PROVEN-DEAD maintenance residue. This stage is investigation-only: no
production code was modified. The surrounding compatibility shim itself and
the remaining re-exports are INTENTIONALLY DUPLICATED / COMPATIBILITY.

## 2. SCOPE AUDITED

- `backend/helper/panel_settings.py` — full definition/alias/import/export
  surface and git history
- `backend/helper/__init__.py` — package re-exports and aliases
- All direct and indirect importers of the shim and its re-exported names
- Runtime, test, and dynamic consumers of every re-exported name
- Repository-wide AST scan for duplicate top-level definitions and duplicate
  import aliases in every module
- Other explicitly labeled compatibility/legacy surfaces

## 3. CANDIDATES INVESTIGATED

### 3.1 `backend/helper/panel_settings.py` — duplicate definitions

- The module is a documented compatibility shim delegating to
  `backend.services.settings_service` (docstring: "remains as a compatibility
  shim so existing imports (backend.helper.panel_settings) continue to work").
- AST confirms `reload` defined at lines 16-17 and again at 20-21 (identical
  bodies), and `auto_close_delay` defined at lines 32-33 and again at 40-41
  (identical bodies). The later definition always shadows the earlier one, so
  the first copies are unreachable by construction.
- Git history (`git log --follow`, `git blame`): the file was introduced in
  merge commit `db927d4` already containing both duplicates — the duplication
  is original-file residue, not a later accidental edit.
- Direct importers: only `backend/helper/__init__.py`. No test imports the
  module or its names.
- Classification:
  - First `reload` and first `auto_close_delay` definitions:
    **PROVEN DEAD** (shadowed, byte-identical, unreachable; removal
    behavior-neutral). Recorded for a separate removal decision — not
    removed in this investigation-only stage.
  - Second definitions (the effective module attributes): **ACTIVE**.

### 3.2 `backend/helper/__init__.py` — duplicate import alias

- Lines 50-56 import `reload as reload_settings` twice; the second import
  rebinds the identical function object, so the first alias line is
  behavior-neutral duplication.
- AST confirms this is the only duplicate import alias in the entire backend.
- Classification: the duplicate alias line is **PROVEN DEAD** (identical
  binding, unreachable as a distinct value). The effective `reload_settings`
  export is retained.

### 3.3 Re-exported compatibility names

- `toggle_auto_close` is the only panel_settings re-export consumed by
  runtime code: `backend/bot/handlers/misc.py` imports it from
  `backend.helper` and calls it.
- `is_auto_close_enabled`, `set_auto_close_enabled`, `load_settings`, and
  `reload_settings` have no in-repository runtime consumers (runtime code
  calls `settings_service` directly). They are exported through the
  documented compatibility surface, and no proof exists that external or
  historical importers do not use them.
- Classification: **INTENTIONALLY DUPLICATED / COMPATIBILITY** — the shim's
  stated purpose is to keep existing imports working; absence of an internal
  consumer is not proof of deadness for a public export surface.

### 3.4 Other labeled compatibility surfaces (repository-wide scan)

- `backend/runtime/task_guard.py` coroutine compatibility: documented
  backward-compatible call form, active and tested. **ACTIVE / DISTINCT**.
- `backend/ai/tools/context.py` `client` field: documented backward-
  compatibility field used by tools. **ACTIVE / DISTINCT**.
- `backend/ai/model_tester.py` legacy summary keys: explicitly retained for
  compatibility with dashboard consumers. **ACTIVE / DISTINCT**.
- `backend/ai/providers/cohere.py` / defaults / discovery compat endpoint:
  real Cohere compatibility API endpoint. **ACTIVE / DISTINCT**.
- `backend/services/retrieve_service.py` legacy text-command entry points:
  documented as still working. **ACTIVE / DISTINCT**.
- No other duplicate top-level definitions or duplicate import aliases exist
  anywhere in `backend/`.

## 4. CLASSIFICATIONS

### PROVEN DEAD (recorded, not removed — investigation-only)

- `backend/helper/panel_settings.py` lines 16-17: first (shadowed) `reload`
  definition.
- `backend/helper/panel_settings.py` lines 32-33: first (shadowed)
  `auto_close_delay` definition.
- `backend/helper/__init__.py` line 56: duplicate `reload as reload_settings`
  import (identical to line 52 binding).

Evidence: AST-verified shadowing with byte-identical bodies, no possible
reachability as distinct bindings, zero test consumers, and removal would not
alter any supported behavior. These are proposed removals for a future
implementation pass; they were NOT changed here.

### INTENTIONALLY DUPLICATED / COMPATIBILITY

- `backend/helper/panel_settings.py` module as a whole (documented shim).
- Re-exported names without internal consumers (`is_auto_close_enabled`,
  `set_auto_close_enabled`, `load_settings`, `reload_settings`).

### POSSIBLY OBSOLETE

None newly identified.

### ACTIVE / DISTINCT

- Second (effective) `reload` and `auto_close_delay` definitions.
- `toggle_auto_close` re-export (consumed by `misc.py`).
- All other labeled compatibility surfaces (task_guard, ToolContext.client,
  model_tester legacy keys, Cohere compat endpoint, retrieve legacy entry
  points).

## 5. PRESERVED SURFACES

No production code was modified. The compatibility shim, all re-exports, the
effective definitions, protected documentation, SQL, migrations, deployment
configuration, and tests remain exactly as committed. The three
proven-dead duplicate bindings are documented for a separate future removal
pass because this execution is investigation-only.

## 6. UNKNOWN / NOT PROVEN

- Whether external consumers outside the repository import
  `backend.helper.panel_settings` or its unused re-exports.
- Whether any historical or generated import depends on the module-level
  attribute order or the duplicate alias lines specifically.

These unknowns support the investigation-only decision rather than an
implementation change.

## 7. RECOMMENDED NEXT STEP

A separate implementation pass may remove the three recorded proven-dead
bindings (shadowed `reload`/`auto_close_delay` copies and the duplicate
`reload_settings` alias) with py_compile + full-suite validation. The
compatibility module and its public exports should remain unless an explicit
public-API migration decision is made.

## 8. VALIDATION

- Baseline: clean tree, `HEAD`/`origin/main` synchronized at
  `39adfe4491aeeb869f86a7b0a0f48788365c66d8`.
- AST scans across all backend modules for duplicate top-level definitions
  and duplicate import aliases: only the three recorded candidates found.
- Import/consumer traces for every re-exported name completed.
- Git history/blame of `panel_settings.py` inspected (duplicates present at
  file introduction, commit `db927d4`).
- No tests were run in this investigation-only execution; no source changed.
  The previously verified baseline remains **571 passed, 1 failed, 1 warning**
  with the known Delete-service Tehran timezone failure.
