# IMPLEMENTATION_REPORT — the owner's selected model is now the model that is used

> Replaces every previous report in full. Scope: the Gemini model-selection path
> (chat **and** Media/STT), its regression tests, and these two documents. No
> other behaviour was changed.
> §9 additionally covers the Test Modules diagnostic-coverage
> change (2026-11-05).

| Item | Value |
|---|---|
| Stage | Fix: remove the per-model substitution table that rewrote the owner's selection at request construction |
| Date | 2026-11-05 (UTC) |
| Project / remote | LifeOS Telegram self-bot, `Onlyicing1/Telegram-self-bot` (`origin`), branch `main` |
| Base commit | `fd547c9f1134d481d626e253f4fd313c43a85929` — `fix(ai): page the All Results diagnostics instead of a rejected oversized edit` |
| Root cause | `backend/ai/providers/base/defaults.py::resolve_model` / `DEPRECATED_MODELS` rewrote a configured model id (`gemini-2.0-flash` → `gemini-2.5-flash`) inside the request builder and in every layer that named a model, so the outbound request, router bookkeeping and the model tester disagreed with the persisted selection (`INVESTIGATION.md` §1–2) |
| Production files changed | `backend/ai/providers/base/defaults.py`, `backend/ai/providers/gemini.py`, `backend/ai/providers/openai_compat.py`, `backend/ai/providers/manager/manager.py`, `backend/ai/model_tester.py`, `backend/services/gemini_media_engine.py` |
| Tests | `tests/test_gemini_model_selection.py` (new, 13 tests) + 5 existing suites updated to the same contract |
| Interpreter | CPython 3.12 venv (`/tmp/repoenv`), project `backend/requirements.txt` (Telethon 1.43.0) + `pytest 9.2.0` + `pytest-asyncio` |
| Live Telegram / Render / provider API | **NOT used.** No session, no RPC, no deployment, no Render shell, no credential, no live Gemini request |
| Not touched | SQL / Supabase, `DATABASE_ARCHITECTURE.md`, runtime-behaviour environment migration, the direct-STT architecture, provider fallback/retry/cooldown, `ToolRegistry`/`ToolExecutor`, `RuntimeSupervisor` lifecycle, discovery/selection caching, unrelated providers |

## 1. Root cause

One helper — a pure data lookup — was the interception point for every model id in
the system:

```python
# backend/ai/providers/base/defaults.py  (before)
DEPRECATED_MODELS: dict[str, str] = {"gemini-2.0-flash": "gemini-2.5-flash"}
def resolve_model(provider_name: str, model: str) -> str: ...
```

Its five callers were: `GeminiProvider.chat` (the URL actually called),
`OpenAICompatProvider.chat` (same helper, every OpenAI-compatible provider),
`ProviderManager._effective_model` (candidate list + unavailable-model marking),
`model_tester.test_single_model` and `_build_targets`/`proven_usable_candidates`
(test target + reported id), and `gemini_media_engine` (`stt_settings_from`,
`resolve_stt_model`, `resolve_media_model`). A selection saved as `gemini-2.0-flash`
therefore persisted unchanged, was displayed unchanged, and was **requested** as
`gemini-2.5-flash`, with only a `WARNING` log line as evidence.

## 2. Production changes

### 2.1 `backend/ai/providers/base/defaults.py` — remove the table

`DEPRECATED_MODELS` and `resolve_model` are deleted, replaced by a comment block
stating the contract: a configured model id is used **verbatim** everywhere, and a
retired/unknown id must fail **as that id** (the provider's own error, attributed to
the model the owner chose) instead of being rewritten into a model nobody selected.
Nothing else in the module changed — the provider defaults (category 2, "initial
setup") stay.

### 2.2 `backend/ai/providers/gemini.py` — send the selected id

```python
model = str(kwargs.get("model") or self._config.default_model or "").strip()
url = f"{_GEMINI_BASE}/models/{model}:generateContent?key=..."
```

The provider's existing 404 → `failure_type="model_not_found"` mapping (which already
carries `metadata["model"]` and `metadata["endpoint"]`) is now the honest report for a
retired id, and the provider is not disabled by a per-model 404.

### 2.3 `backend/ai/providers/openai_compat.py` — same rule

Identical change for every OpenAI-compatible provider (`model` in the JSON payload),
so the fix is not Gemini-shaped: a provider-specific rewrite never existed after this
change, for any provider.

### 2.4 `backend/ai/providers/manager/manager.py` — the effective model is the configured model

`_effective_model` returns `(provider.config.default_model or "").strip()`. The router
therefore builds its candidate list, marks unavailable models and explains skips using
the **same** id it will request. `apply_selection` is unchanged: it still sets
`config.default_model` on the registered provider and still refuses an unregistered
provider (returning `False` and logging). ProviderManager keeps sole ownership of
selection, fallback order, retry and cooldown — model-level failover to *other
candidates* is intact; only each candidate's id is now literal.

### 2.5 `backend/ai/model_tester.py` — test what would be sent

`test_single_model` no longer resolves: it reports `model` = the id it requested, and
`_build_targets` keeps each id as discovered/selected (`replace(...)` and the
`dataclasses.replace` import are gone). The "provider default only when the live
catalogue contains it" rule is unchanged, so a phantom default still never becomes a
target; the curated fallback catalogue is still the list when discovery fails. A newly
released model id is a target with no code change.

### 2.6 `backend/services/gemini_media_engine.py` — both resolvers are verbatim

`stt_settings_from(config)`, `resolve_stt_model()` and `resolve_media_model()` return
the configured value unchanged. `resolve_media_model()` falls back to
`DEFAULT_MEDIA_MODEL` **only** when neither `AI_GEMINI_MEDIA_MODEL` nor the chat model
env value is set. The dedicated transcription model is still an explicit owner choice
and still disjoint from the media/chat model.

## 3. Resulting flow

**Chat:** picker → `config_store.save_config(owner, {provider, model})` →
`Engine.apply_persisted_config` → `ProviderManager.apply_selection` (sets
`provider.config.default_model`) → `_effective_model` (same id) → `GeminiProvider.chat`
→ `POST /v1beta/models/<the selected id>:generateContent`. Every hop is a no-op for the
id itself; there is no substitution step left to fail.

**Media/STT:** AI → Settings → Speech → persisted `stt_model`/`stt_language` →
`stt_control_plane.stt_settings_from` → `stt_engine_factory.apply_stt_config` →
`GeminiMediaEngine(stt_model=…)` → `POST /v1beta/interactions` with `{"model": …}`
(or, with no selection, the documented general media route
`/models/<media model>:generateContent`). `RuntimeSupervisor` re-applies the persisted
settings, so a changed selection takes effect **without a process restart**; direct STT
still never calls `ProviderManager`.

## 4. Tests

New: `tests/test_gemini_model_selection.py` (13 tests) — real `config_store`, real
`Engine`/`ProviderManager`/`GeminiProvider`, real `stt_engine_factory` →
`GeminiMediaEngine`; only HTTP and the Telegram owner lookup are fakes. Every chat test
asserts the **URL** the provider was asked to call, not a helper's return value.

*Discovery / selection* — dynamic catalogue from `/models`; a never-hardcoded id
(`gemini-future-9-pro`) is selectable, persisted and applied to the provider instance;
a persisted selection survives a boot whose env default differs (the env value never
reaches the wire); the request names the selection; `_build_targets` keeps ids verbatim.
*Failure honesty* — a retired selection is requested **as itself**, returns the
provider's 404, is never swapped for `gemini-2.5-flash`, and the stored selection is
left untouched; an unregistered provider/model selection fails with `False` and leaves
the active provider alone; owner A and owner B never see each other's model.
*Testing* — `test_single_model` and `_build_targets` both use the exact id the picker
shows.
*Media/STT* — persisted `stt_model` → engine → `/interactions` body `{"model": <selected>}`;
a separate chat model never becomes the transcription model; an explicit STT selection
is never replaced by the media default; no selection keeps the documented
`generate_content` default route; changing the selection re-provisions a new engine
in place; the direct-STT path never touches `ProviderManager.chat`.

Updated existing suites (same contract, old assertions replaced):
`tests/test_gemini_model_discovery.py`, `tests/test_model_tester.py`,
`tests/test_24_execution_reliability.py`, `tests/test_media_dedicated_stt.py`,
`tests/test_media_gemini_engine.py`.

Commands and results (all from the repository root, `PYTHONPATH=.`):

| Command | Result |
|---|---|
| `pytest tests/test_gemini_model_selection.py -q -p no:cacheprovider` | **13 passed** |
| `pytest tests/test_gemini_model_discovery.py tests/test_model_tester.py tests/test_24_execution_reliability.py -q -p no:cacheprovider` | **77 passed** |
| `pytest` on the provider/config/runtime/UI selection suites (`test_17_providers`, `test_23_provider_mesh`, `test_34_ai_model_ui`, `test_36_ai_settings_ux`, `test_provider_*`, `test_cloudflare_provider`, `test_nararouter_provider`, `test_ai_state_consistency`, `test_02_ai_flow`, `test_18_ai_execution_agent`) | **224 passed** |
| `pytest tests/test_media_gemini_engine.py tests/test_media_dedicated_stt.py` + every other `tests/test_*stt*.py` (15 suites) | **980 passed, 2 skipped** |
| `pytest tests -q -p no:cacheprovider` (full suite) | **6902 passed, 27 skipped, 1 failed** |
| `python -m compileall -q backend tests` | exit **0** |
| `git diff --check` | exit **0** (no whitespace errors) |

The single full-suite failure is
`tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`,
which is **pre-existing and unrelated**: it reproduces on a clean `git archive` of the
base commit `fd547c9` and touches no file in this change.

## 5. Hardcode audit

No hardcoded model id remains in any generic discovery, selection, testing or provider
execution path. The remaining ids are listed with their classification in
`INVESTIGATION.md` §4; in summary they are all **category 2 (initial setup default)** or
presentation labels, and each one is used only while the owner has made no choice:

| Remaining id | Where | Why it is legitimate |
|---|---|---|
| `gemini-2.5-flash` | `backend/ai/providers/base/defaults.py` (`_gemini_default`), `backend/ai/discovery.py` provider entry | the boot default for an unconfigured provider; `apply_persisted_config` always overwrites it with the owner's stored selection |
| `gemini-2.5-flash`, `gemini-2.5-pro` | `backend/ai/model_discovery.py::_FALLBACK_CATALOG` | degraded-mode catalogue used **only** when the live `/models` call fails; a live result always replaces it, so new models need no code change |
| `gemini-3.5-flash-lite` | `backend/services/gemini_media_engine.py::DEFAULT_MEDIA_MODEL` | media default applied only when no media model is configured |
| `gemini-3.5-transcribe` | `backend/services/gemini_media_engine.py::DEDICATED_TRANSCRIPTION_MODEL` | owner-selectable candidate; an explicit `stt_model` always wins |
| TTS ids | `backend/ai/tts_control_plane.py`, `backend/services/gemini_tts_engine.py` | a different setting (TTS), each value only a default/candidate |
| `gemini-stt-deterministic` | `backend/services/media_ai_service.py` | not a model id — a label on the deterministic direct-STT answer (no request is built) |

`resolve_model` / `DEPRECATED_MODELS` have no remaining reference in `backend/`. The
only textual mentions are the regression test that asserts their absence and the dated
historical report `docs/investigations/ai-provider-endpoint-investigation.md`, which is
a point-in-time document for commit `909bc3f` and was deliberately not rewritten.

## 6. Git

| Item | Value |
|---|---|
| Branch | `main` (no branch, PR, rebase or history rewrite) |
| Commit | one commit for this task, parent `fd547c9f1134d481d626e253f4fd313c43a85929` (`git log --oneline -1` after the commit) |
| Push | `origin/main`, fast-forward, verified after the push with `git fetch origin` + `git rev-parse origin/main` + `git merge-base --is-ancestor HEAD origin/main` (exit 0) |
| Working tree | clean after the commit (the pre-existing untracked `telegram-self-bot/` directory was never touched) |

No force-push, no rebase, no `rebase --skip`, no second branch, no PR, no history
rewrite, and no unrelated file in the commit.

## 7. Not live-verified

The deployed Telegram runtime was **not** exercised. Specifically, none of the
following was confirmed against a live deployment, and no claim here depends on it:

* that a real `POST /v1beta/models/<id>:generateContent` with the owner's real
  credential succeeds or fails as expected (the tests fake the HTTP boundary);
* that the live `/v1beta/models` catalogue returns the ids the tests synthesise
  (discovery itself is covered by the existing paginated-fetch tests);
* that the AI → Settings → Speech panel renders the new selection in a live chat, and
  that re-applying a changed STT selection re-provisions the engine mid-run on the
  deployed process (the re-provisioning path is covered at the factory/engine level,
  not through a live `RuntimeSupervisor` start);
* that Telegram's edit/acknowledgement behaviour around the AI panels is unchanged
  (untouched by this change; covered by the existing callback-gate suites).

## 8. User action

No configuration action is required for the fix to take effect: the code now uses
whatever provider and model are already persisted.

What the owner should know / may want to do:

* If `ai_config` currently stores a retired id (for example `gemini-2.0-flash`), the
  runtime will now request **that** id and the provider will answer
  `404 model_not_found`. The fix is to re-select a current model once in
  **Menu → AI → Model** (the panel now lists exactly the discovered catalogue); the
  system will no longer silently move the owner onto another model.
* The same applies to a dedicated transcription model: if
  `AI_GEMINI_STT_MODEL` / the Speech panel points at a retired id, transcription will
  fail with a clear provider error instead of being quietly rerouted; select a current
  transcription model in **Menu → AI → Settings → Speech**.
* A live Telegram confirmation (one chat request with the chosen model, plus one voice
  message if dedicated STT is enabled) is recommended — it is the only check that can
  confirm deployed behaviour, and it was not performed here.

---

## 9. Test Modules diagnostic coverage (2026-11-05)

> Scope: the Test Modules diagnostic only. This section is folded into the same
> task increment as the model-selection fix above and reconciles it with the
> diagnostics work that landed on `main` meanwhile (provider-attributed failure
> fields, `backend/ai/redaction.py`, All Results paging, Cloudflare provider).

### 9.1 Root cause

`backend/ai/model_tester.py` coupled target selection to things a diagnostic
must not depend on:

1. **The owner's selection.** `_build_targets()` read `get_config()` and, for the
   active provider, inserted the selected model first and ordered the rest around
   it, so the target set and its order followed the runtime selection.
2. **A global budget.** `_GLOBAL_TEST_BUDGET` (`MODEL_TEST_GLOBAL_TEST_BUDGET`,
   default 60) sliced the flat target list, so a catalog with more than 60
   eligible models could never be fully tested and the cut-off fell on whichever
   providers came last.
3. **A phantom/default shortcut.** A provider without an API key contributed only
   its `default_model` as `NOT_CONFIGURED`, dropping the rest of its known
   catalog.

`_MODELS_IN_RESPONSE` — a response/display limit — was also applied while
building targets, so a display cap doubled as an execution cap.

### 9.2 Target selection now (`_build_targets`)

* `get_config` is no longer imported or read; `active_config` is accepted for
  call-site compatibility and ignored, so selection cannot add, drop or reorder
  a target.
* Every provider with `capability_kind == "chat"` is included, configured or
  not. Non-chat capabilities (`you` → web search) keep their own path and
  produce no chat targets.
* With a key → that provider's live discovery result. Without a key, or when
  discovery fails / returns nothing usable → `fallback_models(name)`, the one
  existing `_FALLBACK_CATALOG` (the same list `fetch_models` itself falls back
  to). No second catalog, no duplicated provider metadata.
* Every `is_chat_capable()` model is scheduled; a `(provider, model)` pair is
  scheduled at most once. Ids are used verbatim (`strip()` only), so nothing is
  renamed or substituted.
* No provider default is ever *added*; a default that is a real catalog member
  is only moved to the front of that provider's list (ordering, not coverage).
* Nothing truncates: no budget, per-provider cap, slice or priority queue.
  `_TEST_CONCURRENCY` bounds in-flight tests and the per-model timeout bounds
  each one. `_MODELS_IN_RESPONSE` caps the `models` payload only — the target
  list and the production feed are complete.

### 9.3 Unconfigured providers

Scheduled like any other, then answered offline: `test_single_model()` sees no
key and returns `NOT_CONFIGURED` / `API key not configured in environment` with
`latency_s` and `http_status` `None`. No provider client is constructed and no
discovery or chat request is made (asserted by tests that make both raise).

### 9.4 Specialized (non-chat) models

`is_chat_capable()` excludes dedicated transcription (`transcribe`), speech
synthesis (`-tts`, `tts_`, `speech`), audio/video generation (`sora`, `veo`) and
the pre-existing embedding / image / moderation / realtime / audio / search
families. Gemini Interactions-only models stay filtered through
`supportedGenerationMethods`. They are not targets of the generic chat probe and
not reported as broken chat models; no modality-specific path was invented
because none existed.

### 9.5 Honest deadlines

`overall_timeout` defaults to `None` (run to completion). When a caller imposes
one, unreached targets get the new `NOT_ATTEMPTED` status (`partial: true`,
summary bucket `not_attempted`) instead of `TIMEOUT`/a provider response;
`summary.tested` excludes both `not_configured` and `not_attempted`. Only
`AVAILABLE` results feed `proven_usable_candidates()`, so unattempted,
unconfigured, timed-out and failed models never become production candidates.

### 9.6 UI

`NOT_ATTEMPTED` is rendered with the pending mark `…` (plain Unicode, inside the
already-documented status vocabulary). All Results paging from `main` — complete
diagnostics packed into ≤3500-character pages — is kept unchanged; it pages the
whole result list, so the complete run (including every `NOT_CONFIGURED` entry)
stays reachable.

### 9.7 Coverage measured in this environment

No provider API key exists here, so discovery could not run live and the
diagnostic exercised the unconfigured path end to end:

| | |
|---|---|
| Chat providers in the registry | 13 (`openrouter, openai, nararouter, groq, mistral, zai, sambanova, cohere, siliconflow, gemini, cerebras, nvidia, fireworks`) plus `you` (web search → 0 chat targets) |
| Eligible chat-capable models discovered | **39** |
| Models actually tested | **0** (no key available) |
| `NOT_CONFIGURED` results | **39** |
| Targets left untested | **0**, `partial: false` |
| Per provider | openrouter 4 · openai 5 · nararouter 4 · groq 3 · mistral 3 · zai 3 · sambanova 3 · cohere 3 · siliconflow 3 · gemini 2 · cerebras 2 · nvidia 2 · fireworks 2 |

No key was available, so this does **not** show any model answering; it shows
complete, selection-independent target construction and the offline
`NOT_CONFIGURED` path for all 39 catalog models.

### 9.8 Tests

`tests/test_model_tester.py` gained target-construction/execution cases:
selection independence (no config read; identical targets under two different
selections), complete configured-provider coverage, the whole fallback catalog of
an unconfigured provider with no client and no network, a 75-model catalog, 120
models across three providers, a 100-model early catalog that cannot starve later
providers, no duplicate pairs, no phantom default, the exact id recorded as
passed to `test_single_model()`, specialized models excluded, batch/streaming
parity, cancellation propagation, unattempted targets kept out of the production
feed, and one-provider-failure isolation; its budget/timeout/display-cap tests
were rewritten. `tests/test_model_discovery.py` gained the specialized-model
capability case.

Two upstream tests encoded the behaviour this task forbids and now assert the new
contract instead of being deleted:
`test_an_explicit_selection_is_always_tested_even_if_stale` (renamed
`test_an_explicit_selection_never_changes_the_target_set`) and
`test_targets_keep_discovered_and_selected_ids_unrenamed` (now asserts a
configured id outside the catalog is not injected, ids stay verbatim, no
duplicates).

### 9.9 Results

| Check | Result |
|---|---|
| Focused (`test_model_tester.py`, `test_model_discovery.py`) | 63 passed |
| Gemini discovery/selection + Test Modules UI + focused | 136 passed |
| Full suite | **6916 passed, 27 skipped, 1 failed** |
| `python -m compileall -q backend tests` | exit 0 |
| `git diff --check` | clean |

The single failure is
`tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`
("interval advancement exceeded bound"). It reproduces unchanged at `HEAD` in a
detached worktree without this change, touches no file here, and its 10 sibling
scheduler tests pass: pre-existing, reported rather than hidden.

### 9.10 Limitations

* No API key: no live provider was contacted, so only fallback catalogs were
  exercised and no real latency was measured.
* Catalogs larger than 60 models are tested with `fetch_models` stubbed because
  no key exists; the scheduling path around it is real code under test.
* No Telegram client: All Results paging was checked by calling the handler with
  a 200-result payload, not on a live message.
