# AI Provider Endpoints & Runtime Configuration — Diagnosis Report

**Scope:** read-only investigation of reported Gemini, OpenRouter and NaraRouter failures.

No application file, test, dependency, configuration or unrelated documentation
was modified to produce this report; no commit, Render action, database statement
or live provider request was made. No API key, token or other secret value was
read, printed or logged. Line citations refer to the tree at commit `7d9d7b5`.

---

## 1. Executive diagnosis

| # | Root cause | Confidence | Evidence |
|---|---|---|---|
| 1 | **NaraRouter's 403 (`telegram_required`) is classified as an authentication failure and permanently disables the provider for the process lifetime** — including the claim "until configuration changes", which nothing implements. A valid key cannot recover without a restart. | **High** (classification and disable path confirmed in source; the 403 body itself unverified) | `backend/ai/providers/openai_compat.py:259-261`, `backend/ai/providers/manager/manager.py:978-981`, `backend/ai/providers/manager/health.py:147`, `backend/ai/providers/manager/manager.py:644` |
| 2 | **Discovery and factory disagree about which env names count.** Discovery accepts `GEMINI_MODEL`, `OPENROUTER_MODEL`, `OPENROUTER_BASE_URL`, `NARAROUTER_BASE_URL`; the factory reads **only** `AI_*`. Setting only the non-prefixed alias changes what the UI/probe reports but not what the runtime sends. | **High** | `backend/ai/discovery.py:96-105,116-122,216-224` vs `backend/ai/providers/factory.py:92-127,180-188` |
| 3 | **Gemini's runtime default model differs from discovery's** (`gemini-2.5-flash` vs retired `gemini-2.0-flash`), and the discovery value is what gets persisted on provider selection. It works only by accident because a deprecation map rewrites exactly that id at request time. | **High** | `backend/ai/providers/base/defaults.py:32-37`, `backend/ai/discovery.py:122`, `backend/ai/providers/base/defaults.py:349`, `backend/bot/handlers/ai.py:1083-1093` |
| 4 | **Gemini ignores `ProviderConfig.base_url` entirely** (hard-coded base), so no base-URL override — env, config or UI — can ever take effect for Gemini, and discovery/runtime base URLs cannot be reconciled by configuration. | **High** | `backend/ai/providers/gemini.py:20,84,234` |
| 5 | **No trailing-slash normalization** on configured base URLs, producing `https://host/v1//chat/completions`. | **Medium** (code path confirmed; whether each gateway rejects it is unverified) | `backend/ai/providers/factory.py:180-188`, `backend/ai/providers/openai_compat.py:78` |
| 6 | **Endpoint/URL construction itself is not the problem** for Gemini or OpenRouter: both use documented paths, methods and auth styles. | **High** | §2, §4 |
| 7 | `ProviderConfigManager.load()` / `sync_to_config_manager` are **never called**; the authoritative path is `apply_persisted_config → apply_selection`. | **High** | no callers of `load(` on the config manager |
| 8 | OpenRouter attribution headers (`HTTP-Referer`, `X-Title`) are absent, but they are **optional**, so this is not a confirmed defect. | Low / unverified | no hits in `backend/` |

Most likely explanation of the reported symptoms: **two distinct faults, not one
shared URL bug.** NaraRouter fails on a policy 403 that the code treats as "bad
key" and then permanently disables; Gemini/OpenRouter fail on **model-id
persistence** (retired Gemini id, or a stale/unknown OpenRouter model chosen from
an earlier discovery list) or on a **non-`AI_` env alias that only half the
system reads**.

---

## 2. Provider comparison table

| Provider | Discovery Base URL | Runtime Base URL | Discovery Endpoint | Chat Endpoint | Discovery Model | Runtime Model | Authentication | Confirmed issues | Unverified assumptions |
|---|---|---|---|---|---|---|---|---|---|
| **Gemini** | `https://generativelanguage.googleapis.com/v1beta` (`discovery.py:121`) | same, hard-coded (`gemini.py:20`); `ProviderConfig.base_url` ignored | `GET /v1beta/models?key=…&pageSize=1` (`discovery.py:386`) | `POST /v1beta/models/{model}:generateContent?key=…` (`gemini.py:84`) | `gemini-2.0-flash` (`discovery.py:122`) — retired | `gemini-2.5-flash` (`defaults.py:37`) | API key in query string (native Gemini style) | default-model divergence; base URL not configurable; deprecated id persisted | whether the deployed key's project has `gemini-2.5-flash` enabled / geo access |
| **OpenRouter** | `https://openrouter.ai/api/v1` (`discovery.py:96-105`) | `https://openrouter.ai/api/v1` (`defaults.py:92-97`), overridable via `AI_OPENROUTER_BASE_URL` only | `GET /api/v1/models`, Bearer (`discovery.py:389`) | `POST /api/v1/chat/completions`, Bearer (`openai_compat.py:73,78`) | `openrouter/auto` | same as default or `AI_OPENROUTER_MODEL` | `Authorization: Bearer` | non-`AI_` alias mismatch; trailing-slash risk; no attribution headers | whether `openrouter/auto` routes to a free model for this account |
| **NaraRouter** | `https://router.bynara.id/v1` (`discovery.py:216-224`) | `https://router.bynara.id/v1` (`defaults.py:242-247`) | `GET /v1/models`, Bearer (`discovery.py:389`) | `POST /v1/chat/completions`, Bearer (`openai_compat.py:78`) | `deepseek-v4-flash` | same, or `AI_NARAROUTER_MODEL` | `Authorization: Bearer` | 403 → `auth` → permanent disable; `/models` path assumed, not documented in-repo | meaning of `telegram_required`; whether `/models` is offered to this key class |

Discovery's Gemini probe sends the key as a query parameter and sets no header
(`discovery.py:386-387`); every other provider's probe uses Bearer
(`discovery.py:389-390`). That asymmetry is correct for Gemini, not a bug.

---

## 3. Runtime configuration trace

```
Render env
  ├─ AI_PROVIDER ──────────────► factory.create_registry(): registry.switch_provider(active)   factory.py:196-207
  ├─ <KEY vars> (AI_* or legacy)► for provider in _ENV_KEY_MAP: first non-empty wins           factory.py:154-168
  ├─ AI_<P>_MODEL  (AI_ only!)  ► provider_config.default_model                               factory.py:177-181
  └─ AI_<P>_BASE_URL (AI_ only) ► provider_config.base_url                                    factory.py:183-188
            │
            ▼
     get_provider_default(P) (defaults.py) → ProviderConfig(api_key, model, base_url, enabled=True)
            │
            ▼
     ProviderRegistry ─► ProviderManager ─► provider instance owns THAT ProviderConfig

persisted ai_config row
  └─ provider, model ─► engine.apply_persisted_config()            engine.py:301-341
        └─ apply_runtime_selection(provider, model)                engine.py:242-254
              └─ ProviderManager.apply_selection()                 manager.py:461-479
                    ├─ registry.switch_provider(provider)
                    └─ provider.config.default_model = model      ← same object the request reads
  └─ temperature / max_tokens / timeout ─► provider.config         engine.py:363-370

UI selection
  └─ save_config(provider, model) + _apply_runtime_selection()     handlers/ai.py:1083-1093

request time
  dispatcher → ProviderManager.chat(messages, **kwargs)            dispatcher.py:282
    → model pool: provider.config.default_model first, then discovery-fed candidates
                                                                   manager.py:733-757
    → provider.chat(..., model=<candidate>)                        manager.py:799-806
        ├─ gemini.py:83-84         url = f"{_GEMINI_BASE}/models/{model}:generateContent?key=…"
        └─ openai_compat.py:77-78  url = f"{self._config.base_url}/chat/completions"
            └─ httpx POST with {"Authorization": f"Bearer {key}"} openai_compat.py:70-79
```

Consequences:

- **Discovery never configures the runtime.** It is a probe/display layer
  consumed by the AI panel, the web API and the model tester
  (`model_tester.py:558,714`); its only runtime influence is feeding **model
  ids** into the candidate pool via `set_model_candidates`
  (`model_tester.py:540`, `manager.py:760-770`).
- **A stale or incorrect model id chosen from discovery becomes a runtime 404**,
  cached as `(provider, model)` unavailable for 3600 s (`manager.py:73`).
- **Provider-level `enabled` is set by the factory** from key presence
  (`factory.py:168-169`). `ProviderConfigManager.load()` — the only code that
  would overwrite `enabled` from a snapshot — is never called, so a configured
  key always yields an enabled provider.

---

## 4. Confirmed defects (source-verified)

| ID | Defect | Location | Effect |
|---|---|---|---|
| **C1** | Gemini default model is `gemini-2.0-flash` in discovery but `gemini-2.5-flash` in runtime defaults. | `discovery.py:122` vs `defaults.py:37` | UI advertises a retired id; provider selection persists it (`handlers/ai.py:1083`). Survives only because `DEPRECATED_MODELS` rewrites it (`defaults.py:349`, applied at `gemini.py:83`). |
| **C2** | `GeminiProvider` builds URLs from the module constant, never from `self._config.base_url`. | `gemini.py:20,84,234` | Any Gemini endpoint override is silently inert. |
| **C3** | Factory model/base-URL maps contain only `AI_*` names, while discovery accepts both forms. | `factory.py:92-127` vs `discovery.py:96-105,119,216-224` | With only `GEMINI_MODEL` / `OPENROUTER_MODEL` / `OPENROUTER_BASE_URL` / `NARAROUTER_BASE_URL` set, discovery reports the intended value while the runtime keeps its default. |
| **C4** | `403` is classified as `auth` at runtime and 401/403 as `invalid` in discovery. | `openai_compat.py:259-261`, `discovery.py:59` | A policy/entitlement 403 is indistinguishable from a bad key. |
| **C5** | `auth` → `mark_disabled()`; only `record_success` clears it, but disabled providers are skipped, so success is unreachable. No config update calls `mark_healthy`. | `manager.py:978-981`, `health.py:147-155`, `manager.py:644-651` | NaraRouter (or any provider answering 403) stays dead for the whole process despite the "until configuration changes" comment (`health.py:68`). |
| **C6** | Base URLs are only `.strip()`-ed; no trailing-slash normalization, and the URL is `f"{base}/chat/completions"`. | `factory.py:180,188`, `openai_compat.py:78,285` | A configured base ending in `/` produces `//chat/completions` / `//models`. |
| **C7** | `ProviderConfigManager.load()` and `sync_to_config_manager()` have no caller anywhere in `backend/`. | `config_manager.py:66-88,235-260` | The documented snapshot→provider-config path is dead code; persistence works only through `apply_persisted_config`. |
| **C8** | Gemini's discovery probe and runtime model both embed the API key in the URL query. Mitigated (not fixed) by `_sanitize_text`/`_sanitize_url`, which redact it before logging. | `discovery.py:386`, `gemini.py:84` | No leak in the trace line; residual risk only in non-traced third-party logging. Not a failure cause. |

---

## 5. Suspected defects (need further verification)

| ID | Hypothesis | Why it is not confirmed |
|---|---|---|
| **S1** | NaraRouter's `/v1/models` returns `403 telegram_required` for keys that do work on `/v1/chat/completions`, i.e. discovery "invalid" is a false negative caused by probing a guarded discovery route. | The string `telegram_required` has **zero matches** in the repository; it is a historical observation only. Source cannot establish whether it is auth, entitlement, or a Telegram-account gate. |
| **S2** | NaraRouter requires an account/Telegram link distinct from the API key, so chat also 403s. | Same missing evidence; no source or doc in-repo describes the gateway's policy. |
| **S3** | Gemini returns HTTP 400 `API_KEY_INVALID` rather than 401/403, so a bad key is classified `request` (never `auth`), producing a generic failure and no disable. | Code path is real (`gemini.py:186-205`), but which status Google returns for this key/project is unverified. |
| **S4** | The persisted model is an id the account cannot use → 404 → `model_not_found` → 1-hour `(provider, model)` lockout that looks like a provider outage. | `manager.py:73,733-757,978-1005`; depends on the actual persisted row, not inspectable here. |
| **S5** | OpenRouter attribution headers are required for some accounts. | OpenRouter documents them as optional; none are sent (`openai_compat.py:68-75`). No repo evidence of enforcement. |
| **S6** | The fallback chain (`AI_PROVIDER_FALLBACK`) is unset, so a single auth-disabled provider plus an unusable default leaves only "All AI providers failed". | `manager.py:1126-1133`; env value unknown. |

---

## 6. Existing test coverage and gaps

**Executed offline (mocked HTTP; no network, no account):**

```
.venv/bin/python -m pytest tests/test_nararouter_provider.py tests/test_17_providers.py \
  tests/test_model_discovery.py tests/test_13_model_selection.py \
  tests/test_23_provider_mesh.py tests/test_24_execution_reliability.py -q
→ 137 passed, 3 warnings in 11.87s   (exit 0)
```

Covered:

- NaraRouter defaults (`tests/test_nararouter_provider.py:77-82`), env key
  auto-registration (`:86-95`), `AI_NARAROUTER_MODEL` /
  `AI_NARAROUTER_BASE_URL` overrides (`:98-107`) — only the `AI_` spelling.
- Discovery probe classification: 200 → validated, 307/429/500/503 →
  available-unverified, 401/403 → invalid (`:440-467`); sanitized probe trace
  (`:500-560`).
- `403 → auth` at runtime is pinned by test (`:255-256`); changing that
  classification requires updating this expectation.
- Pagination, free-first ordering, fallback catalog (`tests/test_model_discovery.py`),
  z.ai URL construction (`tests/test_17_providers.py:127-128`), deprecated-model
  resolution (`tests/test_24_execution_reliability.py:88-119`).

Gaps (no test exists):

1. No assertion of Gemini's chat URL or that `ProviderConfig.base_url` is
   honoured — C2 is untested.
2. No test that discovery's `default_model` equals the runtime default — C1 is
   untested.
3. No test that a non-`AI_` alias reaches the factory — C3 is untested.
4. No trailing-slash test — C6 is untested.
5. No test that a 403 policy body does not permanently disable a provider, nor
   that a config change re-enables it — C5 is untested, and `:255-256`
   actively encodes the current behaviour.
6. No test comparing discovery base URL with runtime base URL for the same
   provider.
7. `ProviderConfigManager.load()` has no test, consistent with being uncalled.

---

## 7. Minimal recommended fixes (grouped by file — not implemented)

**`backend/ai/discovery.py`**

- Single source for Gemini: set `default_model` to `gemini-2.5-flash`, or import
  it from `get_provider_default("gemini").default_model` instead of duplicating
  the literal (fixes C1).
- Accept and announce only names the factory actually reads, or hand the alias
  lists to the factory — one owner, not two.

**`backend/ai/providers/factory.py`**

- Reuse discovery's `env_vars` / `model_env` / `base_url_env` alias lists (or
  add the non-`AI_` fallbacks) so both layers resolve identically (fixes C3).
- `provider_config.base_url = base_url.rstrip("/")` (fixes C6).

**`backend/ai/providers/gemini.py`**

- `base = (self._config.base_url or _GEMINI_BASE).rstrip("/")` in both `chat()`
  and `list_models()` (fixes C2 with zero behaviour change when unset).

**`backend/ai/providers/openai_compat.py`**

- Normalize `self._config.base_url.rstrip("/")` before composing URLs.
- Keep the 401/403 classification for now, but surface the provider's own error
  `code` / `type` in the failure so a policy 403 is distinguishable from a bad
  key in logs.

**`backend/ai/providers/manager/manager.py` + `health.py`**

- Make the documented "disabled until configuration changes" true: on
  `update_provider_config` / `apply_selection` / explicit user reset, call
  `health.mark_healthy(name)` for that provider, or downgrade `403` to a bounded
  cooldown instead of a permanent disable. Do not do both blindly; see S1.

**`backend/ai/providers/manager/config_manager.py`**

- Either wire `load()` / `sync_to_config_manager()` into boot or delete them;
  today they are dead code implying a synchronization that does not exist (C7).
  Deleting is the smaller, safer change.

Explicitly **not** recommended now: rewriting provider routing, adding a second
retry/health authority, adding Gemini base-URL env support while `gemini.py`
still ignores it, or changing the 403 mapping before S1 is verified.

---

## 8. Safe verification plan (no secrets, no deploy)

1. **Offline first.** Re-run the command in §6 plus the Cloudflare provider test
   suite after any fix. No network needed.
2. **Read the sanitized probe trace.** Look for
   `PROVIDER_DISCOVERY_PROBE provider=nararouter probe_url=https://router.bynara.id/v1/models http_status=403 classification=invalid validated=False`
   (`discovery.py:534-553`). It contains no key and no body.
3. **Distinguish discovery 403 from chat 403** by comparing a discovery run with
   one chat attempt: if `/models` is 403 but chat succeeds, S1 is confirmed and
   the fix belongs in the discovery probe, not in auth classification.
4. **Check runtime state, not just the error text:** `provider-health: '<p>'
   disabled until configuration changes` and `PROVIDER_MODEL_UNAVAILABLE
   provider=… model=…` (`health.py:152`, `manager.py:778`) tell you whether a
   failure was auth-disable or model-404 — the two look identical in the chat
   reply.
5. **Verify the model actually sent.** Confirm the provider response metadata
   carries the resolved model id and not a stale one.
6. **Key hygiene:** never paste a key into chat or a report; re-enter it only in
   the provider dashboard or the Render env store.
7. **Isolate per provider** using `AI_PROVIDER` and a single configured key so
   one disabled provider cannot mask another's result.

---

## 9. Final verdict

The evidence points to **multiple interacting causes, not a single endpoint bug**:

1. **Authentication/access classification (primary for NaraRouter):** a 403
   policy response is mapped to `auth` and then to a permanent, unrecoverable
   in-process disable (C4 + C5). This alone can explain "NaraRouter always
   fails" with a valid key.
2. **Environment/configuration mismatch (primary for Gemini/OpenRouter):**
   discovery and factory resolve different env names, and Gemini's
   discovery/runtime default models differ (C1 + C3), so what the UI reports is
   not necessarily what the runtime sends.
3. **Model identifier mismatch (secondary, high impact):** a persisted
   retired/stale model id yields 404 → `model_not_found` → 1-hour per-model
   lockout (S4), which reads as a provider outage. Gemini's retired default is
   masked only by the deprecation map.
4. **Endpoint construction is not implicated** for these three providers:
   Gemini uses the documented native `:generateContent` path with query-key
   auth; OpenRouter and NaraRouter use the shared OpenAI-compatible
   `/chat/completions` with Bearer auth. The only endpoint-level defects are the
   non-configurable Gemini base URL (C2) and the missing trailing-slash
   normalization (C6).
5. **Error classification/fallback contributes.** 5xx/429 retries are bounded
   and sensible, but the classifier cannot distinguish "wrong key" from
   "account/route restriction", which is the most damaging ambiguity here.

No claim is made that any provider "works": a configured key proves neither
validity nor entitlement, and no live request was made during this
investigation.
