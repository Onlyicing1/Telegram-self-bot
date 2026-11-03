# AI Provider Endpoint, Configuration, Discovery, Health & Fallback Investigation

**Investigation date:** 2026-11-03
**Repository:** `Onlyicing1/Telegram-self-bot`
**Branch examined:** `main` (working branch `freebuff/a3768636b24ad53dc5f65d91`)
**Commit examined:** `909bc3ff2655ffffe73961aceafc5d3d4ac7d760` (`docs: add AI provider endpoint investigation`)
**Worktree state at start:** clean except the previously committed report itself; `909bc3f` is also `refs/heads/main` on the remote (`git ls-remote origin main`).

This report was produced **from zero**. The previous report was used only as a
list of questions and as an object of verification; every conclusion below was
re-derived by reading the current source and by executing the existing test
suite. No live provider request, credential use, Render action, database
statement, dependency change or source/test edit was performed.

---

## 1. Executive summary

Three independent defects explain the reported behaviour. None of them is an
endpoint/URL bug.

1. **An authentication-classified failure permanently removes a provider from
   routing for the lifetime of the process, and nothing in the product can
   clear it.** `mark_disabled()` writes to an untimed set; `mark_healthy()` has
   **zero call sites in `backend/`**; `record_success()` would clear the set but
   is only reached from a provider call that routing has already refused to
   make. Selecting a different provider or model, and re-applying persisted
   configuration, only switch a pointer and write a model string — they never
   touch the health tracker. Only a process restart constructs a new tracker.
   (`health.py:117,129-130,147-152,154-162,200-212`; `manager.py:461-475,644-660,978-982`; `engine.py:242-254,301-389`)

2. **Every 403 is treated as a bad API key, and the provider's own error
   identity is discarded.** Both adapters map `401` **and** `403` to `auth`, and
   the manager turns `auth` into the permanent disable above. The provider's
   `code`/`type`/`status` fields *are* extracted and stored in metadata
   (`provider_error_code`, `provider_error_type`) but never consulted for
   classification. A policy, region, plan or account-linkage 403 therefore
   locks out a working key with no recovery. (`openai_compat.py:236-267`;
   `gemini.py:171-190`; `manager.py:945-982`; `discovery.py:416-431`)

3. **The surfaces that report health read a different source than the router
   that refuses requests.** `_get_engine_info()` derives `connected` from
   `provider.health()`, which only checks "key present and enabled"; the AI
   Health panel's recovery line requires a positive remaining cooldown, which a
   disabled provider never has; the provider list comes from discovery, which
   never sees the tracker. The bot can therefore display *Connected / HEALTHY /
   available* while every chat request is skipped. (`handlers/ai.py:97-116,304-305,981-987`; `openai_compat.py:53-66`; `gemini.py:69-74`; `engine.py:142-153`; `health.py:271-276`)

A fourth, narrower defect affects model-level routing: **a 404 on the
configured model removes the provider's entire model pool from the candidate
set for one hour**, because eligibility checks the effective model before the
candidate list is built, and the lockout is exposed by neither
`health_snapshot()` nor `reset_state()`. (`manager.py:73,644-664,717-731,733-760`)

No Critical finding was identified (no data loss, privilege bypass or secret
exposure). The three defects above are High because they can silently remove
all AI capability on a single-provider deployment and are not diagnosable from
any shipped UI.

---

## 2. Scope and methodology

**In scope**

- Provider adapters actually present in this tree: `gemini`, `openai`,
  `openrouter`, `cerebras`, `mistral`, `groq`, `zai`, `sambanova`, `nvidia`,
  `cohere`, `siliconflow`, `fireworks`, `nararouter`, `cloudflare`, `dummy`,
  plus the `you` retrieval capability (`factory.py:44-61`).
- ProviderManager routing/retry/fallback/health; discovery; model discovery;
  model tester; persisted-config restore; AI handlers/web endpoints; relevant
  tests and dependencies.

**Out of scope / absent**

- **xAI / Grok as a chat provider does not exist in this repository.** `grok`
  appears only in the text-to-speech stack (`backend/services/grok_tts_engine.py`,
  `backend/ai/tts_control_plane.py`), which posts to `https://api.x.ai/v1/tts`
  and is not registered in `_PROVIDER_CLASSES`. There is no Grok chat endpoint,
  key, model, discovery entry or fallback path. This is recorded rather than
  investigated further.

**Method**

- `git fetch origin`, `git ls-remote`, `git status`, `git log` to establish the
  exact tree under examination.
- Static reading of every file cited below (no filename-only inference).
- Call-graph verification by grep for writers/readers of each state
  (`mark_disabled`, `mark_healthy`, `record_success`, `apply_selection`,
  `reset_provider_config`, `update_provider_config`,
  `sync_to_config_manager`, `telegram_required`).
- Execution of the existing offline test suite in a local virtualenv. No test
  was edited, skipped or weakened.
- No network call to any AI provider was made; `httpx` was exercised only
  through the suite's mocks.

---

## 3. Current architecture and provider call paths

The documented authority chain holds in the current code:

```
RuntimeSupervisor (runtime/supervisor.py:386-399 — get_engine(), attach_tools())
      │
      ▼
Engine.execute() → Dispatcher.dispatch()            engine.py:214,242 ; dispatcher.py:282
      │
      ▼
ProviderManager.chat()  ← sole routing authority    manager.py:103
      │
      ├─ _ordered_candidates()                       manager.py:578-611
      ├─ _skip_reason() per candidate                manager.py:644-685
      ├─ _candidate_models() model-level pool         manager.py:733-760
      ├─ _attempt_with_retry() → provider.chat()      manager.py:858-935 (call at ~878)
      └─ _apply_failure() → tracker                   manager.py:978-1012
      │
      ▼
ProviderRegistry (storage only)                      registry.py:44-118
```

Tool execution is separate and single-owner: the dispatcher's tool loop calls
`ToolExecutor.execute_calls()`, and `ToolExecutor` is the only component that
calls `tool.execute()` (`tools/executor.py:13,133,213,301`; dispatcher call at
`dispatcher.py:771,989`). `ToolRegistry` is a name-keyed allowlist with duplicate
protection (`tools/registry.py:54-64`). No AI tool module performs shell,
filesystem-write or arbitrary HTTP work: `httpx` appears only in
`discovery.py`, `model_discovery.py` and `stt_provider_probe.py`, never under
`backend/ai/tools/`.

Provider instances receive **only** a `ProviderConfig` and request arguments
(`base/config.py`, `base/contract.py`); they never receive the Telethon client,
the service layer or SQL. The manager never calls a provider for a
non-`chat` capability (`manager.py:657-660`), and `dummy` is filtered out before
any routing decision (`manager.py:113-118,121-131`).

---

## 4. Per-provider findings

### 4.1 Google Gemini

| Aspect | Current behaviour | Evidence |
|---|---|---|
| Registration | `GeminiProvider` in `_PROVIDER_CLASSES`; registered only when `AI_GEMINI_API_KEY` or `GEMINI_API_KEY` is non-empty | `factory.py:45,64-66,162-205` |
| Runtime base URL | **Hard-coded constant**, `ProviderConfig.base_url` is never read | `gemini.py:20,84,234`; `defaults.py:31-40` |
| Default model | `gemini-2.5-flash` at runtime | `defaults.py:37` |
| Discovery default model | `gemini-2.0-flash` (**retired**) | `discovery.py:122` |
| Chat endpoint | `POST {base}/models/{model}:generateContent?key=…` | `gemini.py:84` |
| Model listing | Discovery: `GET /models?key=…&pageSize=1`; model discovery paginates `pageSize=100` up to 10 pages; the provider's own `list_models()` is a second, single-page implementation | `discovery.py:386-387`; `model_discovery.py::_fetch_gemini_models`; `gemini.py:229-241` |
| Auth | API key in query string | `gemini.py:84,234` |
| Error mapping | 401/403 → `auth`; 404 → `model_not_found`; ≥500 → `server`; else `request` | `gemini.py:181-190` |
| Deprecation rewrite | `resolve_model()` rewrites `gemini-2.0-flash` → `gemini-2.5-flash` at request construction | `defaults.py:348-362`; `gemini.py:83` |

Consequences: a configured Gemini base URL (env, config object, or the model
tester's own override) can never take effect; discovery and runtime advertise
different default models; and a UI-selected retired id is silently rewritten on
the wire while remaining the id shown and persisted.

### 4.2 OpenRouter

| Aspect | Current behaviour | Evidence |
|---|---|---|
| Registration keys | `AI_OPENROUTER_API_KEY` / `OPENROUTER_API_KEY` | `factory.py:55-57,69` |
| Base URL | `https://openrouter.ai/api/v1`, overridable via `AI_OPENROUTER_BASE_URL` only | `defaults.py:~96`; `factory.py:109-121` |
| Chat | `POST {base}/chat/completions`, Bearer | `openai_compat.py:70-78` |
| Models | `GET {base}/models`, Bearer | `openai_compat.py:279-286` |
| Default model | `openrouter/auto` | `defaults.py` |
| Error mapping | 401/403 → `auth`; 404 → `model_not_found`; ≥500 → `server`; every other 4xx (including **402**) → `request` | `openai_compat.py:259-267` |

402 (out of credits) therefore takes the `request` branch, which the manager
deliberately does **not** cool down or disable (`manager.py:983-1001`). A
credit-exhausted OpenRouter stays "available" and is retried on every request
while producing per-request failures and fallback.

### 4.3 NaraRouter

| Aspect | Current behaviour | Evidence |
|---|---|---|
| Registration keys | `AI_NARAROUTER_API_KEY` / `NARAROUTER_API_KEY` | `factory.py:76` |
| Base URL | `https://router.bynara.id/v1`, overridable via `AI_NARAROUTER_BASE_URL` | `discovery.py:221`; `defaults.py` (`_nararouter_default`) |
| Chat | `POST {base}/chat/completions`, Bearer | `openai_compat.py:78` |
| Models | `GET {base}/models`, Bearer (path inferred, not documented in-repo) | `discovery.py:389`; `openai_compat.py:285` |
| Default model | `deepseek-v4-flash` | `discovery.py:222`; `defaults.py` |
| Error mapping | Inherits OpenAI-compatible mapping; 403 → `auth` → permanent disable | `openai_compat.py:261`; `manager.py:980-981` |

**`telegram_required` is not present anywhere in this repository** (zero
matches in `backend/`, `tests/`, docs other than the historical report). The
current code has no branch that could produce, parse or act on it. Any claim
about what it means is therefore an external observation, not a code fact — see
§14.

### 4.4 Other registered providers (no symptom reported)

`openai`, `groq`, `mistral`, `cerebras`, `zai`, `sambanova`, `nvidia`,
`cohere`, `siliconflow`, `fireworks`, `cloudflare`, `dummy`, `you`. All
OpenAI-compatible chat providers share the same `openai_compat.py` endpoint,
auth, error-mapping and retry behaviour described above, so every finding about
`401/403 → auth`, `402 → request`, and trailing-slash handling applies to all of
them. `cloudflare` additionally requires an account id before registration
(`factory.py:123-135,190-204`); `you` is a `web_search` capability and is
excluded from chat routing (`manager.py:657-660`).

---

## 5. Configuration and environment-variable mapping

Three separate metadata tables read the environment, and they do not agree:

| Concern | Factory (runtime) | Discovery (UI/probe) | Defaults |
|---|---|---|---|
| API key | `_ENV_KEY_MAP`: `AI_*` **and** bare alias (`factory.py:64-90`) | `env_vars`: same pairs (`discovery.py:98-100,118-120,218-220`) | empty |
| Model | `_ENV_MODEL_MAP`: `AI_*` **only** (`factory.py:92-107`) | `model_env`: `AI_*` **and** bare alias (`discovery.py:99,119,219`) | per-provider |
| Base URL | `_ENV_BASE_URL_MAP`: `AI_*` only, and **no Gemini entry** (`factory.py:109-121`) | `base_url_env`: `AI_*` and bare, **empty for Gemini/Groq/Mistral/Cerebras** (`discovery.py:100,111,121,141`) | per-provider |

Operational consequences:

- `GEMINI_MODEL`, `OPENROUTER_MODEL`, `NARAROUTER_MODEL` (bare) change what
  discovery reports as the provider's default model but **not** what the
  runtime provider config uses. The same split applies to bare
  `*_BASE_URL` variables for providers whose factory entry exists.
- For **key** variables the two layers agree, so a key set under either name is
  registered. (The historical report's broader "env-name mismatch" claim is
  therefore narrower than stated — see §13.)
- `AI_PROVIDER` selects the active provider only if it is registered and
  chat-capable (`factory.py:207-220`); an unregistered persisted provider is
  healed to the active runtime pair on restore (`engine.py:257-299`).

`ProviderConfigManager.load()` (`config_manager.py:54`) and
`sync_to_config_manager()` (`config_manager.py:190`) are defined but have **no
call sites** in `backend/` or `tests/`; the authoritative path remains
`apply_persisted_config → apply_runtime_selection → apply_selection`
(`engine.py:301-389,242-254`; `manager.py:461-475`).

---

## 6. Endpoint and model-discovery analysis

### 6.1 URL construction

- OpenAI-compatible chat: `f"{self._config.base_url}/chat/completions"`
  (`openai_compat.py:78`); models: `f"{self._config.base_url}/models"`
  (`openai_compat.py:285`).
- Discovery probe: `f"{status.base_url}/models"` with Bearer, or Gemini's
  `f"{status.base_url}/models?key=…&pageSize=1"` (`discovery.py:386-390`).
- Model discovery: `f"{base_url}/models"` (`model_discovery.py::_fetch_openai_compat_models`).
- **No `.rstrip("/")` exists anywhere under `backend/ai`** (grep for
  `rstrip("/")` returns nothing). A base URL ending in `/` produces
  `…/v1//chat/completions` and `…/v1//models`.

Whether a given gateway normalises the doubled slash is server behaviour and is
**UNKNOWN** without a live request.

### 6.2 Discovery vs runtime model values

- Discovery's Gemini default is `gemini-2.0-flash` (`discovery.py:122`);
  runtime's is `gemini-2.5-flash` (`defaults.py:37`).
- Provider selection persists the **discovery** value
  (`handlers/ai.py:1083-1093`) and then immediately persists the first model
  returned by `fetch_models`, if any. The retired id can therefore be stored,
  displayed and reported by tests, while `resolve_model()` rewrites it at
  request time (`defaults.py:348-362`; applied at `gemini.py:83` and
  `openai_compat.py:79`).
- `model_discovery` follows Gemini's `nextPageToken` to a bounded 10 pages and
  filters non-chat models; the provider's own `list_models()` does neither
  (`gemini.py:229-241`). The UI path uses `model_discovery`, not the provider
  method, so this is duplication rather than a live UI gap.

### 6.3 Test-fed production candidates

Only Test Modules `AVAILABLE` results are fed into the router's model pool
(`model_tester.py:474-543`, `manager.set_model_candidates` at
`manager.py:763-774`). Discovery **alone** never feeds production candidates.
The tester constructs a fresh provider and calls `provider.chat()` directly
(`model_tester.py:209-215`), so it neither reads nor writes manager health.

---

## 7. Authentication and HTTP error-classification analysis

| Status | OpenAI-compatible adapter | Gemini adapter | Manager effect | Discovery probe effect |
|---|---|---|---|---|
| 200 | success | success | `record_success()` (clears cooldown/quarantine/disabled if reached) | `available`, `validated=True` |
| 400/413/415/422 | `request` | `request` | no cooldown, no disable; immediate failover | `available_unverified` (kept selectable) |
| 401 | `auth` | `auth` | `mark_disabled()` — **permanent, process lifetime** | `invalid` |
| 403 | `auth` | `auth` | `mark_disabled()` — **permanent, process lifetime** | `invalid` |
| 404 | `model_not_found` | `model_not_found` | `(provider, model)` unavailable for 3600 s; provider not cooled | `available_unverified` |
| 402 | `request` | `request` (not special-cased) | no cooldown, no disable | `available_unverified` |
| 429 | `rate_limited` + `retry_after` | `rate_limited`, fixed 5 s | cooldown (clamped ≤300 s), one retry if Retry-After ≤5 s | `available_unverified` |
| 5xx | `server` | `server` | cooldown 60 s, quarantine after 5 | `available_unverified` |
| transport | `timeout` / `network` | `timeout` / `network` | cooldown 30/60 s, one retry | `available_unverified` |

Evidence: `openai_compat.py:113-136,236-267`; `gemini.py:160-190`;
`manager.py:945-1012`; `health.py:37,43-62`; `discovery.py:416-447`.

Two classification gaps are material:

1. **403 is indistinguishable from 401.** The adapters extract
   `provider_error_code`, `provider_error_type` and (OpenAI-compatible)
   `provider_request_id` (`openai_compat.py:236-257`; `gemini.py:171-179`), but
   `_failure_type_for()` ignores all of them and maps both statuses to `auth`
   (`openai_compat.py:259-267`). A 403 that means "region/plan/account policy"
   is handled exactly like "invalid key".
2. **402 has no category.** It falls through to `request`, which the manager
   intentionally never penalises (`manager.py:983-1001`). The model tester, by
   contrast, has an `INSUFFICIENT_CREDITS` status for the same condition
   (`model_tester.py:106-118`), so the UI can say "insufficient credits" while
   the router keeps treating the provider as healthy and retries it.

Discovery applies the same 401/403 rule but with a different consequence: the
provider is reported `invalid` and the Provider panel renders it as text only,
with buttons created exclusively for `available` entries
(`discovery.py:425-431`; `handlers/ai.py:304-305,322-346`). An `invalid` provider
is therefore visible but not selectable.

---

## 8. Provider health, cooldown, retry and recovery

### 8.1 States and their clearers

| State | Storage | Set by | Cleared by | Visible |
|---|---|---|---|---|
| `healthy` | implicit | default, `record_success()` | — | indirectly |
| `cooling_down` | `_cooldown_until` monotonic deadline (`health.py:130-135`) | `mark_cooling_down()` | deadline expiry (automatic) | only while remaining > 0 |
| `quarantined` | `_quarantine_until` (`health.py:43-45,184-191`) | 5th consecutive failure | deadline expiry or `record_success()` | only while remaining > 0 |
| `disabled` | `_disabled: set[str]` (`health.py:117,147-152`) | `mark_disabled()` | `mark_healthy()` (no callers) or `record_success()` (unreachable) | **no** |
| `(provider, model)` unavailable | `_unavailable_models` (`manager.py:717-731`) | `model_not_found` | 3600 s TTL only (`manager.py:73`) | **no** |

### 8.2 Why a disabled provider cannot recover

1. `mark_disabled()` adds the name to an untimed set and deletes every timer
   (`health.py:147-152`).
2. `state()` returns `disabled` **before** looking at any deadline
   (`health.py:129-130`), so no elapsed time changes the outcome.
3. `mark_healthy()` discards the name (`health.py:154-162`) but has **no call
   sites in `backend/`** — only `tests/test_17_providers.py:289,296` call it.
4. `record_success()` would discard it (`health.py:200-212`, discard at `207`),
   but it is invoked only after a successful provider call
   (`manager.py` success paths in `_attempt_with_retry`, ~`880/903/923`), and
   `_skip_reason()` refuses a disabled provider before it can be attempted
   (`manager.py:654-656`).
5. Configuration changes do not touch the tracker: `apply_selection()`
   switches the registry pointer and writes `config.default_model`
   (`manager.py:461-475`). Its callers are the web endpoints
   (`web/app.py::api_ai_set_provider`, `::api_ai_set_model`), the
   `settings_set` tool (`tools/settings.py:140,156`), the Glass actions
   (`handlers/ai.py:1087,1096,1109,1155,1186`) and the persisted restore
   (`engine.py:344`). None calls `mark_healthy`/`record_success`.
6. Replacing a provider *instance* does not help either: health is keyed by
   provider **name** in the manager (`manager.py:88-99`), and the registry
   rejects a duplicate name (`registry.py:44-50`). Only a new process creates a
   new `ProviderHealthTracker`.
7. `update_provider_config()` / `reset_provider_config()` exist
   (`manager.py:542-549`) but have **no call sites** outside their definitions.

So the message "disabled until configuration changes"
(`health.py:147-152`, log line) is not implemented by any configuration path.

### 8.3 Retry, cooldown and quarantine (working as designed)

- One immediate retry for `network`/`timeout`/`server` after a 1 s backoff
  (`manager.py:858-935`; constants at `manager.py:66-73`).
- `429`: a single retry only when `Retry-After` is positive and ≤ 5 s;
  otherwise immediate failover (`manager.py:889-916`). Cooldowns are clamped to
  300 s (`health.py:39-40,139-142`).
- Five consecutive failures quarantine for 600 s and the provider re-enters the
  pool automatically after expiry (`health.py:43-45,184-191`; `is_available`
  consults the deadline).
- `request` and `model_not_found` never cool a provider down
  (`manager.py:983-1001`); they fail over immediately.
- Terminal exhaustion returns an honest `success=False` from the manager and
  never invokes Dummy (`manager.py:1071-1106`).

### 8.4 The model-lockout amplifier

`_skip_reason()` evaluates the provider's **effective configured model** before
`_candidate_models()` is built (`manager.py:644-664` vs `733-760`). When a 404
marks that model unavailable, the whole provider is skipped — its other,
tested-healthy candidate models included — for the 3600 s TTL. Neither
`health_snapshot()` (`manager.py:1047-1049`) nor `reset_state()`
(`manager.py:1051-1069`) reports `_unavailable_models`, so no surface can
explain why a provider with a healthy key is not used.

---

## 9. Security and context-isolation findings

- **Runtime authority is preserved.** Lifecycle: `RuntimeSupervisor`
  (`supervisor.py:386-399`). Routing: `ProviderManager` (`manager.py:103`).
  Tool execution: `ToolExecutor` only (`tools/executor.py:301`). Capability
  allowlist: `ToolRegistry` (`tools/registry.py:54-64`). No second scheduler,
  executor or update loop was found in the AI provider path.
- **No arbitrary execution surface was found**: no shell/subprocess/filesystem
  writes and no unrestricted HTTP client under `backend/ai/tools/`; `httpx`
  usage is confined to discovery/model-discovery/STT probing.
- **Providers receive prompt content, not Telegram handles.** The request is a
  `messages` list plus tool definitions; provider constructors take a
  `ProviderConfig` only (`base/config.py`). No provider holds the Telethon
  client, the DB client or the service layer.
- **Reply-mode does send conversational/Telegram-derived context to the
  provider by design.** `ai_unified.py:484-520` classifies the replied message
  with `classify_message()` (pure attribute inspection, no download —
  `media.py:1-100`), then collects sender display name, chat title, chat id and
  media metadata into `ReplyContext`; that text becomes part of the prompt. This
  is intended behaviour for an owner-operated self-bot, but it means the
  selected third-party provider sees message text/captions and chat identity
  metadata for reply-mode requests. No change was made here.
- **Secret handling in the provider path is sound.** Discovery sanitises probe
  URLs (query string and API key removed — `discovery.py:_sanitize_url`,
  `_sanitize_text`), the model tester sanitises error text
  (`model_tester.py:76-84`), and no adapter logs headers or the key. Residual
  risk: the manager records up to 200 characters of provider-returned error
  text in the response matrix and up to 160 in task traces
  (`manager.py` failure matrix; `task_trace` bound), so a provider that echoed a
  credential in its error body could place it in telemetry. No such echo was
  observed in code or tests. Informational only.

---

## 10. Test commands and actual results

Environment: Python 3.12.3; a local `.venv` (gitignored at `.gitignore:25`);
`pip install -r backend/requirements.txt pytest pytest-asyncio` → pytest 9.2.0,
httpx 0.27.0. No dependency or requirement file was changed in the repository.

| # | Command | Result | Exit |
|---|---|---|---|
| 1 | `python -m pytest tests/test_17_providers.py tests/test_23_provider_mesh.py tests/test_13_model_selection.py tests/test_41_reset_cooldown.py tests/test_34_ai_model_ui.py tests/test_35_ai_retry_ux.py -q` | **118 passed**, 3 warnings, 13.96 s | 0 |
| 2 | `python -m pytest tests/test_nararouter_provider.py tests/test_model_tester.py tests/test_model_discovery.py tests/test_provider_structured_output.py tests/test_provider_tool_boundary.py tests/test_provider_system_instruction_contract.py tests/test_settings_model_key_contract.py tests/test_task_fallback_cooldown.py tests/test_media_gemini_engine.py tests/test_retry.py -q` | **208 passed**, 3 warnings, 4.99 s | 0 |
| 3 | `python -m pytest -q -p no:cacheprovider` (full suite) | **6838 passed, 27 skipped, 1 failed**, 166.73 s | 1 |
| 4 | `python -m pytest tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once -q -p no:cacheprovider` | **1 failed** (reproduced standalone) | 1 |

**The one failure is unrelated to this investigation and is not caused by any
change here** (only this Markdown file was modified): it asserts
`await scheduler.run_once(later) == 1`, gets `0`, and logs
`Task 1 was not scheduled: interval advancement exceeded bound`
(`backend/ai/task_scheduler.py:290`). It touches the task scheduler, not
providers, discovery, health or endpoints. It is reported as a pre-existing
failure, not as a pass.

Warnings observed (no action taken): `pytest-asyncio` unset
`asyncio_default_fixture_loop_scope`; `gotrue` deprecation from `supabase`;
Starlette `multipart`/`anyio` deprecations. The 27 skips were not individually
audited; `tests/conftest.py:22-38` registers opt-in `live_supabase` and
`live_telegram` markers, and no live provider/credential test was enabled.

### Coverage of the findings

| Behaviour | Pinned by a test? |
|---|---|
| 401 disables a provider, no retry | yes — `tests/test_17_providers.py:241-253` |
| Tracker-level `mark_healthy` clears disabled | yes — `tests/test_17_providers.py:285-300` (unit only; no production caller) |
| Cooldown/quarantine expiry, `record_success` clearing | yes — `tests/test_17_providers.py`, `tests/test_23_provider_mesh.py` |
| `reset_state` shape / no fabricated reset time | yes — `tests/test_41_reset_cooldown.py` |
| 403 → `auth` for NaraRouter | yes — `tests/test_nararouter_provider.py::test_error_envelope_maps_to_existing_taxonomy` |
| Discovery 401/403 → `invalid`; other statuses kept | yes — NaraRouter discovery tests |
| Health panel recovery line | yes — `tests/test_41_reset_cooldown.py` |
| **Config change clears disabled** | **no test — no such behaviour exists** |
| **Disabled provider visible in Health/engine info** | **no test — it is invisible** |
| **404 model lockout skips the whole provider** | **no test** |
| **402/credits handling** | **no test** |
| **Env alias parity (bare model vars)** | **partly** — `tests/test_settings_model_key_contract.py` covers settings keys, not env aliases |
| **Trailing-slash normalisation / Gemini base URL override** | **no test** |

---

## 11. Findings classified by severity

No **Critical** finding was identified.

### High

#### H1 — A disabled provider has no in-process recovery path

- **Evidence:** `health.py:117,129-130,147-152,154-162,200-212`;
  `manager.py:461-475,644-660,978-982`; `engine.py:242-254,301-389`;
  `registry.py:44-50`; grep shows `mark_healthy` only in tests.
- **Actual behaviour:** any `auth` failure permanently removes the provider from
  routing for the process lifetime. Selecting another provider/model, applying
  persisted config, editing provider config or re-registering the instance all
  leave the disabled set untouched. Only a restart clears it.
- **Expected behaviour:** a configuration change should clear the state the
  configuration change was meant to fix; the message "until configuration
  changes" implies this.
- **Impact:** on a single-provider deployment, one 401/403 ends all AI
  capability until the process is restarted. On multi-provider deployments, the
  user's explicitly selected provider can be silently ignored forever.
- **Verification:** construct a `ProviderManager` with a stub provider returning
  `http_status=401`, call `chat()` twice — the second call never reaches the
  provider (`test_17_providers.py:241-253` pins the first half); then call
  `apply_selection("auth", "m")` and observe `reset_state("auth")["available"]`
  is still `False`.
- **Minimal fix:** on `apply_selection()` (and only there), call a tracker
  reset for the provider whose configuration changed; optionally add an
  explicit "retry provider now" action. Keep `401` semantics; pair with H2.
- **Status after remediation (2026-11-03, commit `b203685`):** fixed. The
  tracker gained `clear_disabled()`, called by `ProviderManager.recover_provider()`,
  by a valid `update_provider_config()`, by `reset_provider_config()`, and by a
  model-writing `apply_selection()`; only the untimed disabled flag is cleared
  (cooldowns, quarantine and model-unavailability records survive). Covered by
  `tests/test_17_providers.py`.

#### H2 — All 403 responses are classified as invalid credentials

- **Evidence:** `openai_compat.py:236-267`; `gemini.py:171-190`;
  `manager.py:945-982`; `discovery.py:416-431`.
- **Actual behaviour:** 401 and 403 both become `auth`; provider `code`/`type`
  is stored but never inspected. Any policy/plan/region/account 403 therefore
  disables a working key permanently (H1).
- **Expected behaviour:** distinguish "credential rejected" (401) from
  "request forbidden by policy/entitlement" (403 + provider code), which should
  be a bounded cooldown or a configuration requirement, not an indefinite
  lockout.
- **Impact:** valid providers can be locked out with no user-visible cause; the
  Provider panel additionally labels them "Invalid Key".
- **Verification:** `tests/test_nararouter_provider.py` error-mapping table
  asserts `403 → auth`; source shows no read of `provider_error_code/type` in
  any classifier.
- **Minimal fix:** classify on provider code/type where present; add a
  `policy`/`forbidden` category with a bounded cooldown. This intentionally
  changes a pinned test and must be accompanied by updating it.
- **Status after remediation (2026-11-03, commit `b203685`):** fixed. `401`
  stays `auth`; a `403` is `auth` only when the provider's own code/type names
  a rejected credential, otherwise it becomes the new bounded `access_denied`
  category (cooldown, never a disable). Gemini keeps its documented
  `UNAUTHENTICATED` / `API_KEY_INVALID` signals. Covered by
  `tests/test_nararouter_provider.py`, `tests/test_17_providers.py` (Gemini) and
  `tests/test_cloudflare_provider.py`.

#### H3 — Health surfaces disagree with the router; `disabled` is invisible

- **Evidence:** `handlers/ai.py:97-116` (`connected` from
  `active.health()`), `handlers/ai.py:981-987` (recovery line requires
  `remaining > 0`), `health.py:271-276` (disabled reports 0),
  `openai_compat.py:53-66` / `gemini.py:69-74` (health = key + enabled only),
  `engine.py:142-153`, `handlers/ai.py:298-346` (discovery-driven provider
  panel), `web/app.py::api_ai_providers` (discovery, not tracker).
- **Actual behaviour:** a provider can be `disabled` while the AI panel says
  Connected, the Health panel says HEALTHY and renders no recovery line, and
  the provider list still advertises it as available.
- **Expected behaviour:** every health surface should read the routing state
  that actually decides requests.
- **Impact:** the failure is undiagnosable from the product; users restart
  blindly or assume the provider is up.
- **Verification:** `test_41_reset_cooldown.py` proves the panel renders only
  positive-remaining states; no test covers `disabled`.
- **Minimal fix:** include tracker state in `_get_engine_info()` and render a
  "Disabled — re-select the provider to re-enable" line when
  `state == "disabled"`.

### Medium

#### M1 — A 404 on the configured model removes the whole provider for an hour

- **Evidence:** `manager.py:73,644-664,717-731,733-760,1047-1069`.
- **Actual behaviour:** `_skip_reason()` checks the effective model before the
  candidate pool is built, so `_model_unavailable` skips the provider entirely;
  other tested models are never tried. The lockout is absent from
  `health_snapshot()` and `reset_state()`.
- **Expected behaviour:** skip only the unavailable `(provider, model)` pair and
  continue with the provider's other candidates.
- **Impact:** one stale model id can take a whole provider (and its tested free
  models) out of service for 3600 s with no explanation.
- **Verification:** register a provider with a candidate list, return 404 for
  the configured model, then call `chat()` — the candidate models are not
  attempted.
- **Minimal fix:** evaluate model availability per candidate inside the
  `_candidate_models()` loop instead of in `_skip_reason()`; expose
  `_unavailable_models` in `reset_state()`.

#### M2 — Credit exhaustion (402) is never penalised and uses a different taxonomy than the tester

- **Evidence:** `openai_compat.py:259-267`; `manager.py:983-1001`;
  `model_tester.py:106-118`.
- **Actual behaviour:** 402 → `request` → no cooldown, no disable, provider
  stays "available"; the tester reports `INSUFFICIENT_CREDITS`.
- **Expected behaviour:** a billing/credit failure is a provider-level
  availability problem with a long, visible cooldown, reported consistently.
- **Impact:** a dead provider is retried on every request; UI and router
  disagree about why.
- **Verification:** stub a 402 response and inspect `reset_state()` — state
  stays `healthy`.
- **Minimal fix:** add a `credits` failure category in both adapters and map it
  to a long cooldown; reuse it in the tester.

#### M3 — A 403 on `/models` makes the provider unselectable even when chat works

- **Evidence:** `discovery.py:377-447` (probe path and 403 → `invalid`),
  `handlers/ai.py:304-305,322-346` (invalid is text-only, no button),
  `openai_compat.py:261`/`manager.py:981` (chat 403 disables).
- **Actual behaviour:** the same status code has two consequences in two
  subsystems, and both ignore the provider's error identity. A gateway that
  guards its model-list route (while serving chat) becomes non-selectable; a
  provider whose chat route is policy-blocked remains selectable until the
  first request disables it.
- **Expected behaviour:** discovery should not invalidate a key on a 403 whose
  body does not identify an authentication problem; it already keeps every
  other non-200 status as `available_unverified` (`discovery.py:433-447`).
- **Impact:** false "Invalid Key" and missing provider choices.
- **Verification:** `tests/test_nararouter_provider.py::test_auth_rejection_marks_nararouter_invalid`
  pins the current behaviour; no test covers a policy 403.
- **Minimal fix:** require an auth-specific provider code before `invalid`;
  otherwise `available_unverified`.

#### M4 — Gemini's retired default is persisted and displayed, then silently rewritten

- **Evidence:** `discovery.py:122`; `defaults.py:32-37,348-362`;
  `gemini.py:83`; `openai_compat.py:79`; `handlers/ai.py:1083-1093`.
- **Actual behaviour:** discovery advertises `gemini-2.0-flash`; selecting the
  provider persists it; every request silently substitutes
  `gemini-2.5-flash`. Model lists, selection state and test labels keep the
  retired id.
- **Expected behaviour:** one source of truth for the default model.
- **Impact:** configuration lies about what is sent; a model reported AVAILABLE
  is not necessarily the model used; future retirements require code edits to a
  map instead of a data update.
- **Verification:** `python -c` on `resolve_model("gemini","gemini-2.0-flash")`
  returns the newer id; `discovery._PROVIDERS` returns the old one.
- **Minimal fix:** align the discovery entry with `defaults.py`; keep the
  deprecation map only for previously stored configs.

#### M5 — Model/base-URL environment aliases are read by discovery but not by the factory

- **Evidence:** `factory.py:92-121` vs `discovery.py:99-100,119-120,219-220`.
- **Actual behaviour:** a bare `GEMINI_MODEL`/`OPENROUTER_MODEL`/`*_BASE_URL`
  changes the discovery-reported default or probe target, while runtime keeps
  the `AI_*` value or default.
- **Expected behaviour:** both layers read one mapping.
- **Impact:** "I set the model/base URL and it changed in the UI but not in
  chat" — a configuration split brain. (API key variables do agree, so keys are
  not affected.)
- **Verification:** set `GEMINI_MODEL` only, construct a registry, compare
  `provider.config.default_model` with `_scan_provider()["gemini"].default_model`.
- **Minimal fix:** export one `(key, model, base_url)` table from a shared
  module and consume it in both places.

#### M6 — Test Modules bypasses the manager and cannot rehabilitate or prove routing

- **Evidence:** `model_tester.py:158-215` (fresh provider, direct `chat()`),
  `model_tester.py:474-543` (only AVAILABLE results feed candidates),
  `manager.py:763-774`.
- **Actual behaviour:** a model can test AVAILABLE while its provider is
  `disabled`; the tester never clears that state and never observes cooldowns.
  Conversely, its results are the only source of production candidate models,
  so a provider that is skipped never benefits from them.
- **Expected behaviour:** the tester should report the same eligibility the
  router would apply, or explicitly label itself as "provider-direct, bypasses
  routing".
- **Impact:** false confidence; the panel's "AVAILABLE" and the runtime's
  behaviour can contradict each other indefinitely.
- **Verification:** disable a provider, run `test_single_model` against a stub
  200 — it returns AVAILABLE; `reset_state()` stays disabled.
- **Minimal fix:** render the tracker state alongside test results (cheap), or
  route tests through the manager with an explicit health-bypass flag.

### Low

#### L1 — No trailing-slash normalisation

- **Evidence:** `openai_compat.py:78,285`; `discovery.py:386-390`;
  `model_discovery.py::_fetch_openai_compat_models`; no `rstrip("/")` in
  `backend/ai`.
- **Actual behaviour:** `…/v1/` yields `…/v1//chat/completions`.
- **Impact:** user-configured gateways may 404/redirect; **server behaviour is
  UNKNOWN** without a live request.
- **Fix:** normalise once when the config is created.

#### L2 — Gemini ignores `ProviderConfig.base_url`

- **Evidence:** `gemini.py:20,84,234`; `defaults.py:34`; no Gemini entry in
  `factory.py:109-121`.
- **Actual behaviour:** no env, config or tester override can redirect Gemini;
  the model tester's `base_url` argument is silently ineffective for it.
- **Impact:** regional/proxy endpoints are impossible; discovery and runtime
  cannot be reconciled by configuration.
- **Fix:** `base = (config.base_url or _GEMINI_BASE).rstrip("/")`.

#### L3 — Gemini `list_models()` is a second, non-paginated implementation

- **Evidence:** `gemini.py:229-241` vs `model_discovery.py::_fetch_gemini_models`.
- **Actual behaviour:** the provider method reads one page of 100 and ignores
  `nextPageToken`; the UI path uses the paginated helper.
- **Impact:** any future caller of the provider method silently sees a truncated
  catalog.
- **Fix:** delegate to one implementation, or remove the unused duplicate.

#### L4 — Discovery results outlive chat-state changes

- **Evidence:** `discovery.py:51` (300 s cache), `discovery.py:477-513`;
  `handlers/ai.py:298-346`.
- **Actual behaviour:** the provider list can keep advertising `available`
  regardless of a chat disable, and is only refreshed every 5 minutes or on an
  explicit refresh.
- **Impact:** misleading listing; contributes to H3.
- **Fix:** merge tracker state into the listing (preferred over shortening the
  cache).

### Informational

- **I1 — xAI/Grok is not an LLM provider here.** Only a TTS adapter exists
  (`services/grok_tts_engine.py`; `ai/tts_control_plane.py`). No chat key,
  model, endpoint or discovery entry. Not a defect; recorded because the brief
  asked.
- **I2 — `telegram_required` is absent from the repository.** No code can parse
  or act on it. See §14.
- **I3 — Dummy is never a production candidate** (`manager.py:113-131,1071-1106`),
  so failures are reported honestly instead of faked.
- **I4 — Architecture boundary holds** (§3, §9): single supervisor, single
  router, single tool executor, registry allowlist, no arbitrary execution
  surface.
- **I5 — No secret leakage found in the provider path**; residual telemetry
  risk noted in §9.
- **I6 — The historical report's claims remain substantively valid** (§13); only
  its epistemic framing of `telegram_required` and the breadth of the
  env-mismatch claim needed correction.

---

## 12. Previous report reconciliation

Re-verified against `909bc3f`:

**Still valid (re-derived, not copied):** the absence of any recovery path
(F1–F4); UI/routing divergence and invisible `disabled` (F5–F6); the
provider-wide 404 lockout (F7); discovery/chat 403 divergence (F8); the masked
Gemini default divergence (F9); the narrower env mapping (F10); missing
trailing-slash normalisation (F11); and the tester bypass (F12).

**Corrected / outdated:**

1. **`telegram_required` was presented as an established fact about the 403.**
   It appears **nowhere** in this repository. It must be treated as an external
   observation with unknown semantics, not as a code-verified condition. This
   report downgrades every claim that depends on it to UNKNOWN (§14).
2. **The env-mismatch finding was broader than the code supports.** API **key**
   variables agree between discovery and factory for all three providers; only
   **model** and **base-URL** variables diverge. The earlier wording implied a
   general name mismatch.
3. **Line citations drifted** within unchanged functions (e.g. the `disabled`
   check is now `health.py:129-130`, the snapshot loop `271-275`, the skip gate
   `manager.py:644-660`). Content is unchanged; citations in this report are the
   current ones.
4. **The previous report's commit header (`7d9d7b5`) is outdated.** The code is
   unchanged since then (intervening commits are documentation-only), but the
   correct reference is `909bc3f`.

**Explicitly not a defect (re-confirmed):** OpenRouter attribution headers are
absent, but they are optional; `ProviderConfigManager.load()` /
`sync_to_config_manager()` are indeed uncalled (`config_manager.py:54,190`), so
the earlier note stands.

---

## 13. Prioritized remediation plan

| Priority | Change | Why | Scope |
|---|---|---|---|
| **P1** | Clear a provider's health on an explicit configuration change (`apply_selection`), and add an explicit "retry/re-enable provider" action | H1 — without this, only a restart recovers a provider | `manager.apply_selection`, one tracker reset call, one Glass action, tests |
| **P2** | Classify 403 using the provider's own `code`/`type`; add a policy/billing category | H2, M2 — stops valid keys being disabled forever and gives credits a real state | `openai_compat._failure_type_for`, `gemini.chat`, `health.FAILURE_CATEGORIES`, `manager._apply_failure`; update the pinned 403 test deliberately |
| **P3** | Make every health surface read the tracker | H3 — makes failures diagnosable | `handlers/ai._get_engine_info` + Health panel, `engine.engine_health`, `web/api_ai_providers` merge |
| **P4** | Move model-availability check into the candidate loop; expose `_unavailable_models` in `reset_state` | M1 — one stale model must not remove a provider | `manager._skip_reason`/`_candidate_models`/`reset_state` |
| **P5** | Single shared env/defaults table; align Gemini's discovery default; make Gemini honour `base_url` | M4, M5, L2 — removes configuration split brain | new shared mapping or imports between `factory`/`discovery`; `gemini.py` |
| **P6** | Normalise base URLs; unify tester/router failure taxonomy; label the tester as routing-bypassing | L1, M6 — removes the remaining silent divergence | `openai_compat`, `discovery`, `model_tester` |

Each P-item is independently shippable. P1 and P3 together convert the current
silent, permanent failure into a visible, recoverable one without changing
routing semantics.

---

## 14. Unknowns (not verifiable without credentials, production logs or live calls)

1. **What the observed 403 actually meant.** No live request was made. Whether
   it was an invalid key, a region/plan restriction, an account-linkage
   requirement, or a guarded `/models` route is **UNKNOWN**. The repository
   contains no string or branch for `telegram_required`.
2. **Whether NaraRouter serves `/v1/models` to the deployed key class.** The
   path is inferred, not documented in-repo; the discovery probe assumes it.
3. **Whether the deployed environment sets non-`AI_` model/base-URL aliases.**
   If it does, discovery and runtime disagree in production (M5).
4. **Which state production actually hit** — the permanent `disabled` set or
   the 3600 s model lockout. Both produce "the selected provider is not used";
   only production logs (`PROVIDER_SKIPPED reason=…`,
   `PROVIDER_MODEL_UNAVAILABLE`) can distinguish them.
5. **Whether the deployed Gemini key is entitled to `gemini-2.5-flash`** and
   whether `gemini-2.0-flash` is still served for it.
6. **How each gateway treats a doubled slash** (`/v1//chat/completions`).
7. **Whether the 27 skipped tests hide any provider-relevant case.** Skip
   reasons were not individually audited; no live/credential test was enabled.

---

## 15. Verification appendix

Commands used to establish this report (all offline except `git fetch` /
`git ls-remote`):

```bash
git fetch origin && git rev-parse HEAD && git ls-remote origin main
grep -rn "mark_healthy" --include='*.py' backend tests        # 1 definition, 2 test uses
grep -rn "reset_provider_config\|update_provider_config" ...  # definitions only
grep -rn "telegram_required" ...                              # no code matches
grep -rn 'rstrip("/")' backend/ai                             # no matches
python -m venv .venv && .venv/bin/pip install -r backend/requirements.txt pytest pytest-asyncio
.venv/bin/python -m pytest <focused provider files> -q
.venv/bin/python -m pytest -q -p no:cacheprovider
```

Only this file was modified. No application code, test, dependency, schema,
environment variable, Render service or documentation other than this report was
touched.
