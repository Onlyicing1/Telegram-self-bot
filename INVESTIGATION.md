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
