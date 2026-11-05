# INVESTIGATION — the selected Gemini model was rewritten before the request

## Metadata

| Item | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` (`origin`), branch `main` |
| `origin/main` at investigation time | `fd547c9f1134d481d626e253f4fd313c43a85929` — `fix(ai): page the All Results diagnostics instead of a rejected oversized edit` (`git log --oneline -1`, worktree clean) |
| Investigation date | 2026-11-05 (UTC) |
| Question | Does the model the owner selects in the AI panel survive discovery, testing, persistence and execution — and what actually appears in the outbound Gemini request? |
| Paths examined separately | **(A)** chat model: discovery → tester → UI → `ai_config` → `ProviderManager` → `GeminiProvider.chat`; **(B)** Gemini Media/STT: `stt_*` settings → `STTControlPlane` → `stt_engine_factory` → `GeminiMediaEngine` → `/interactions` |
| Not touched | SQL / Supabase, `DATABASE_ARCHITECTURE.md`, Render, any live Telegram session, any live provider request |

## 1. Verdict

The selection contract was intact end to end — discovery, the picker, `config_store`,
`Engine.apply_persisted_config`, `ProviderManager.apply_selection` and the request
builder all passed the owner's id through **verbatim** — with exactly **one**
exception that broke it at the last possible moment:

`backend/ai/providers/base/defaults.py` held a per-model rewrite table

```python
DEPRECATED_MODELS: dict[str, str] = {"gemini-2.0-flash": "gemini-2.5-flash"}

def resolve_model(provider_name: str, model: str) -> str: ...
```

and every layer that named a model consulted it. A configured `gemini-2.0-flash`
was silently turned into `gemini-2.5-flash` **inside the request builder**, so the
wire request (and the router's notion of the "effective" model, and the model
tester's target list, and the media/STT resolvers) disagreed with the id the owner
had selected and the id still stored in `ai_config`. The UI kept showing the old
id, the provider kept requesting a different one, and no error ever mentioned it.

This is a **runtime override / architectural violation** (category 4) in generic
provider code, not a protocol constant, not a setup default, not a legacy
compatibility shim that anything else depended on: `resolve_model` was a pure data
lookup used *only* to substitute ids, and deleting it removed the behaviour
entirely with no other caller to repair.

Nothing in the repository still references `resolve_model` or `DEPRECATED_MODELS`
outside `INVESTIGATION.md`/`IMPLEMENTATION_REPORT.md` and the regression test that
asserts their absence.

## 2. Path A — chat model, stage by stage

| # | Stage | Where | Verbatim? |
|---|---|---|---|
| 1 | Discovery | `backend/ai/model_discovery.py::fetch_models` → `GET /v1beta/models?pageSize=100` (paginated, `generateContent` only) | ✅ catalogue comes from the API; no static registry decides what is selectable |
| 2 | Fallback catalogue | `_FALLBACK_CATALOG` (used **only** when discovery fails/returns nothing usable; `get_last_fetch_source() == "fallback"`) | n/a — a documented degraded catalogue, not an override of a selection |
| 3 | Test targets | `backend/ai/model_tester.py::_build_targets` | ❌ called `resolve_model` on the owner's selection, the provider default and every discovered id |
| 4 | Test request | `model_tester.py::test_single_model` | ❌ called `resolve_model` again before asking the provider |
| 5 | Picker | `backend/bot/handlers/ai.py::_ai_pick_model_action` → `config_store.save_config(owner, {"provider", "model"})` | ✅ exact ids; owner-scoped, `ADMIN_ONLY` confirmation unchanged |
| 6 | Persistence | `backend/ai/config_store.py` (`ai_config`, in-memory fallback) | ✅ stores what it was given |
| 7 | Boot application | `Engine.apply_persisted_config` → `ProviderManager.apply_selection(provider, model)` → `provider.config.default_model = model` | ✅ exact id is applied to the provider instance |
| 8 | Router / effective model | `manager.py::_effective_model`, `_candidate_models`, `_mark_model_unavailable`, `_skip_reason` | ❌ `_effective_model` returned the **substituted** id, so failover bookkeeping attributed failures to a model the owner never chose |
| 9 | Request | `GeminiProvider.chat` (`/models/{model}:generateContent`) | ❌ `model = resolve_model(self.name, kwargs.get("model") or self.config.default_model)` — the actual substitution point |

So the substitution was not local: it existed in five places (steps 3, 4, 8, 9 and
step 10 below) precisely because it was centralised as a helper. Removing the helper
fixed all of them at once, and each call site now does the only thing left to do —
`str(value or "").strip()`.

`OpenAICompatProvider.chat` shared the same helper (`openai_compat.py`), which is
why the bug was provider-agnostic rather than Gemini-only.

### What did **not** break the contract (verified, left alone)

* `config_store` is the single owner-specific configuration authority; no second
  config source exists and no environment variable can override a stored selection
  (`apply_persisted_config` writes the persisted value over the provider instance
  regardless of `AI_GEMINI_MODEL`). Regression-covered: a persisted selection
  survives a boot whose env default differs.
* `ProviderManager` remains the sole owner of selection, fallback order, retry and
  cooldown. Model-level failover to *other configured candidates* is an explicit
  part of that contract and was **not** changed: only the id each candidate is
  requested as is now verbatim.
* Nothing caches a model id in a way that could outlive a selection; the discovery
  cache is keyed by provider+credential and is separate from the selection.
* An unavailable model already fails honestly: `GeminiProvider` maps a 404 to
  `failure_type="model_not_found"` with `metadata["model"]` and `metadata["endpoint"]`,
  surfaces it to the owner and does not disable the provider.

### Why test success and the reported symptom could coexist

The tester was reporting on the *substituted* id (`test_single_model` returned the
resolved id in `result["model"]`), so a UI showing `gemini-2.0-flash` could be told
"AVAILABLE" for a request that went to `gemini-2.5-flash` — the same class of
mismatch the owner observed. This is why the tests had to assert the id in the
**outbound URL**, not the id a helper returned.

## 3. Path B — Gemini Media/STT

| # | Stage | Where | Finding |
|---|---|---|---|
| 1 | Settings UI | `backend/bot/handlers/ai_stt_settings.py`, `ai.py` (AI → Settings → Speech) | ✅ owner-selectable candidate list + explicit "dedicated transcription model" toggle |
| 2 | Persistence | `ai_config`: `stt_model`, `stt_language`, `stt_passes` (`config_store._DEFAULTS`) | ✅ owner-specific, same authority as the chat model |
| 3 | Control plane | `backend/ai/stt_control_plane.py` → `stt_settings_from(config)` | ❌ resolved the configured id through `resolve_model("gemini", …)` |
| 4 | Provisioning | `backend/runtime/supervisor.py::_apply_persisted_stt_settings` → `stt_engine_factory.apply_stt_config` → `apply_stt_settings` | ✅ the **persisted** selection (not an env default) is what reaches the engine, and a re-apply re-provisions the engine in place — no process restart |
| 5 | Engine | `backend/services/gemini_media_engine.py::build_gemini_media_engine` / `resolve_media_model` / `resolve_stt_model` | ❌ both resolvers passed configured values through `resolve_model`; the static `DEFAULT_MEDIA_MODEL` is used only when *nothing* is configured |
| 6 | Request | `_transcribe_interaction` → `/interactions` with `{"model": …}`; the general media route → `/models/{model}:generateContent` | ✅ the engine sends its own configured `stt_model`/`model` verbatim |

The chat model and the STT model are **intentionally separate settings**: `stt_model`
selects the transcription model, the "media model" serves OCR/captions, and the chat
model is neither. `stt_model` unset means the documented default route (the general
media model answering `STT_INSTRUCTION`, `stt_transport == "generate_content"`), which
the tests pin as the initial-setup behaviour. Direct STT does not call
`ProviderManager` at all and still does not.

`DEFAULT_MEDIA_MODEL = "gemini-3.5-flash-lite"` and
`DEDICATED_TRANSCRIPTION_MODEL = "gemini-3.5-transcribe"` are **setup defaults /
registry entries**, not overrides: the former only applies when neither
`AI_GEMINI_MEDIA_MODEL` nor the chat model is configured, and the latter is only a
candidate the owner must explicitly select. Both are kept.

## 4. Hardcoded model-id inventory (current source)

| Location | Value | Classification | Verdict |
|---|---|---|---|
| `backend/ai/providers/base/defaults.py` (deleted) | `gemini-2.0-flash → gemini-2.5-flash` | **4. Runtime override** | **removed** — the root cause |
| `backend/ai/providers/base/defaults.py::_gemini_default()` | `gemini-2.5-flash` | 2. Initial setup default | kept — used only when nothing is selected (`apply_persisted_config` always wins) |
| `backend/ai/discovery.py` provider entry `default_model` | `gemini-2.5-flash` | 2. Initial setup default (advertised default, same value as above) | kept |
| `backend/ai/model_discovery.py::_FALLBACK_CATALOG["gemini"]` | `gemini-2.5-flash`, `gemini-2.5-pro` | 2. Initial setup default (degraded-mode catalogue) | kept — only consulted when the live `/models` call fails; the live API result always wins, so a new model needs no code change |
| `backend/services/gemini_media_engine.py::DEFAULT_MEDIA_MODEL` | `gemini-3.5-flash-lite` | 2. Initial setup default | kept — applied only when no media model is configured |
| `backend/services/gemini_media_engine.py::DEDICATED_TRANSCRIPTION_MODEL` | `gemini-3.5-transcribe` | 2. Initial setup default (owner-selectable candidate) | kept — an explicit `stt_model` always takes precedence |
| `backend/ai/tts_control_plane.py`, `backend/services/gemini_tts_engine.py` | TTS model ids | 2. Initial setup default / explicit candidate registry for a different (TTS) setting | kept — unrelated to chat/STT selection, each value is only used until the owner picks another |
| `backend/bot/handlers/ai_stt_settings.py` label map | `gemini:gemini-3.5-transcribe` | Presentation label for one candidate | kept |
| `backend/services/media_ai_service.py::_DIRECT_STT_MODEL` | `gemini-stt-deterministic` | Not a model id — a label on the deterministic direct-STT answer (no HTTP request is built) | kept |
| `backend/ai/providers/gemini.py`, `openai_compat.py`, `manager/manager.py`, `model_tester.py` | — | — | **no model id remains in any generic discovery/selection/testing/execution path** |

Grep evidence: `grep -rn "resolve_model\|DEPRECATED_MODELS" --include=*.py backend/`
returns nothing; the only hits are the assertion in
`tests/test_gemini_model_discovery.py` that the symbols are gone, and the dated
historical report under `docs/investigations/` (a point-in-time document for commit
`909bc3f`, deliberately not rewritten).

## 5. Why there was no "second" hidden problem

The audit also checked, and cleared: duplicated config sources (`ai_config` only),
environment migration of selection (`AI_GEMINI_MODEL` is a default, never a stored
value's replacement), cross-owner leakage (`config_store` keys on the owner id;
`_heal_phantom_config` only touches the booting owner's own record and does not
rewrite model ids), and stale caches (the discovery cache is credential+provider
keyed and never holds a selection). The `_heal_phantom_config` path is a documented
provider-registration repair and was left untouched.

## 6. Evidence

Every claim above is backed by a test that exercises the real boundary and asserts
the **outbound request** rather than a helper's return value — see
`IMPLEMENTATION_REPORT.md` §4 (`tests/test_gemini_model_selection.py`) for the exact
commands and results. No live Telegram session, deployment or provider credential was
used at any point.
