# Implementation Report — LifeOS Telegram Self-Bot

## Execution 29 — You.com Web Search Provider (plus completion of interrupted Execution 28)

### Task / Result

Two deliverables landed in this execution:

1. **Completed and committed the interrupted Execution-28 work** that was
   already in the working tree (commit `39f9134`): streamlined Ghost Seen AI
   reply flow (no manual prompt / no disclosure step), enumerated retention
   with a `Never` option, font digit styling, profile font application, plus
   the test/migration/doc alignment that work still needed.
2. **Implemented the You.com Web Search API as a first-class retrieval
   capability** inside the existing provider architecture, exposed to the AI
   through the existing ToolRegistry path.

### Starting commit

`f7f793e` (Execution 27 report commit; origin/main verified)

### Exact files changed

Execution-28 completion commit (`39f9134`) — 12 files:
`backend/bot/handlers/ghost_seen.py`, `backend/bot/handlers/misc.py`,
`backend/helper/font_style.py`, `backend/profile/engine.py`,
`backend/services/ghost_seen_service.py`, `backend/services/settings_service.py`,
`supabase/migrations/20260823130000_ghost_seen_retention_duration.sql`,
`DATABASE_ARCHITECTURE.md`,
`tests/test_45_ghost_seen.py`, `tests/test_47_ghost_seen_entry.py`,
`tests/test_49_ghost_seen_flows.py`, `tests/test_50_font_system.py`,
`tests/test_51_execution27.py`.

You.com execution — production:

- `backend/ai/providers/you_search.py` (NEW) — `YouSearchProvider`
- `backend/ai/tools/websearch.py` (NEW) — `WebSearchTool`
- `backend/services/web_search_service.py` (NEW) — thin service bridge
- `backend/ai/providers/base/capabilities.py` — `supports_web_search` flag
- `backend/ai/providers/base/contract.py` — `CAPABILITY_KIND` + default
  `search()` contract method
- `backend/ai/providers/base/defaults.py` — `_you_default()`
- `backend/ai/providers/factory.py` — registration + `YDC_API_KEY`
  auto-detection + chat-only active-selection guard
- `backend/ai/providers/manager/manager.py` — `_capability_kind()` helper,
  non-chat skip rule in routing, `web_search()` method reusing
  guarded-await/metrics/health classification
- `backend/ai/tools/registry.py` — tool registration
- `AGENTS.md` — §9 capability note + §11 env var row

Tests: `tests/test_52_you_search.py` (NEW).

### Exact architecture changes

No second architecture. Exactly one registration path per concern:

- Provider identity: name `you`, display "You.com Search",
  `CAPABILITY_KIND = "web_search"`, `supports_web_search=True` and all chat
  flags False. Its `chat()` returns a structured NOT_IMPLEMENTED failure.
- Factory: `"you": YouSearchProvider` in `_PROVIDER_CLASSES`;
  `_ENV_KEY_MAP["you"] = ["YDC_API_KEY"]`. With the key present the factory
  auto-registers it exactly like every other provider; with the key missing/
  empty it is simply not registered. The `AI_PROVIDER` env can never select
  it as the active chat engine (chat-kind guard).
- Routing: `ProviderManager._skip_reason` excludes any non-chat capability
  kind from chat candidate scoring, so the search provider is never a chat
  fallback even when healthy.
- Retrieval path: `ProviderManager.web_search()` resolves the first
  available web-search-kind provider, runs ONE attempt via the existing
  `guarded_await` timeout wrapper, records latency/errors in the existing
  metrics registry, and classifies failures through the existing
  `_apply_failure` machinery (auth → disabled, rate limit → cooldown with
  provider-supplied Retry-After). No new retry loop, no background polling.
- AI integration: `WebSearchTool` (READ_ONLY) → `web_search_service` →
  manager → provider. Registered once in `create_default_registry`; the
  Prompt Builder picks it up automatically, so the model calls it only when
  routing decides fresh web information is needed.

### YDC_API_KEY environment variable

Read only from the runtime environment via the existing factory convention.
Missing or empty key ⇒ provider unregistered/unavailable — never a crash.
The key never appears in logs, exceptions, results metadata, health output,
telemetry, tests, or this report. Only its configured/unconfigured state is
exposed (`health()["configured"]`). Render configuration is a manual owner
action; no committed file contains or requires it.

### API endpoint & behavior

`POST https://ydc-index.io/v1/search` with headers `X-API-Key` +
`Content-Type: application/json`. Payload: `{query, count}` plus optional
validated `include_domains` and `freshness` (`day|week|month|year` or
`YYYY-MM-DDtoYYYY-MM-DD`); unsupported values are dropped server-side,
count clamped to 1–100 (default 10). Responses are normalized into
`{success, query, results[{kind,title,url,description,snippets,page_age}],
metadata{search_uuid,query,latency}, error}` — unknown response fields are
not exposed, missing fields are not fabricated, empty results are an honest
success. Non-2xx maps to auth/request/rate_limited/server categories;
429 preserves Retry-After only when the provider actually sends it;
malformed JSON/envelope fails safely; timeout/network errors map into the
existing failure taxonomy. Health reports configured-but-unverified until a
real successful search proves availability.

### Tests added

`tests/test_52_you_search.py` — 32 focused regression tests covering:
factory mapping + defaults, auto-detection with key present, graceful
absence (missing/empty), `AI_PROVIDER` cannot activate it, endpoint/method/
headers/payload, optional-field cleaning/rejection, web/news normalization,
partial + malformed responses, honest empty results, full non-2xx mapping,
Retry-After preservation (and absence), timeout/network failures, empty
query short-circuit, chat-routing exclusion, manager success/failure
classification through the existing tracker, no-Telegram-access invariant,
API-key secrecy (health/capabilities/error/log capture), tool registration
+ argument validation + source-preserving formatting, and single-registration/chat-provider invariants.

### Validation commands/results

- `.venv/bin/python -m pytest tests/test_52_you_search.py -q --asyncio-mode=auto` → **32 passed**
- Full suite `pytest tests/ -q --asyncio-mode=auto` → **901 passed, 0 failed, 2 warnings**
  (pre-existing Starlette deprecation + multipart warnings)
- `.venv/bin/python -m compileall -q backend` → PASS
- `git diff --check` → PASS
- Stale-reference search: no active `.menu`/Ghost Sink strings in backend
  (documented-dormant `startup_check.py` excepted); `web_search` registered
  exactly once; `"you"` appears once per factory map

### Database/schema impact

None. Environment-only configuration by design (secret); no migration, no
table, no Supabase change.

### Security considerations

Key sourced exclusively from env; never hardcoded/committed/logged/in
errors/in telemetry/in health endpoints. Tests use the fake value
`ydc-fake-test-key-000` only, with an explicit leakage assertion. The
provider has no Telegram imports and no message-sending surface.

### Intentionally untouched systems

Provider retry/fallback semantics for chat · token accounting · telemetry
contract · memory · Save/Delete/Retrieve engines · RuntimeSupervisor ·
watchdog · discovery catalog (`backend/ai/discovery.py`) and model-tester /
model-selector surfaces (You.com deliberately absent from LLM selection) ·
frontend (`src/`) · all other handlers · live database.

### Validation limitations

- **Live You.com verification NOT performed**: `YDC_API_KEY` is absent from
  this environment (verified via presence check only). All HTTP behavior is
  proven against mocked responses; real-endpoint proof remains an owner /
  Render action after adding the key.
- Live Telegram E2E of the AI-invoked search flow not exercised.
- No deployment performed (per task instructions).

### Commit hash

- Execution-28 completion: `39f9134`
- You.com provider implementation + report: `fb2e7cab449b3c52676b5b5dfd03f8999ecabac9`

### Push result

Pushed to `origin/main` after commit; verified via `git fetch origin` +
HEAD comparison; final working tree clean.

### Stop

Execution 29 complete. Not starting another chunk.
