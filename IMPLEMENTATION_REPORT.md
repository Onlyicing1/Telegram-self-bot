# IMPLEMENTATION REPORT — CURRENT STATE

## Latest phase — M2.1: Groq Whisper Speech-to-Text adapter + provider test capability

Repository `Onlyicing1/Telegram-self-bot` · branch `main` · state as of 2026-09-17.

### Current stage

M2.1 — **the Groq Whisper transcription capability and the provider-test seam.**
The two Groq candidates the M2.0 control plane already registered as
*capability only* are now **executable and testable**. Speech-to-Text stays
where M2.0 put it; nothing moved back into AI → Settings / Advanced, and no model
is typed by the owner.

```
AI                                    (ai)
├── Media Analysis                    (ai_media)
│   ├── Text recognition (OCR)        (ai_media_ocr)   — unchanged (no OCR change)
│   └── Speech-to-Text                (ai_media_stt)
│       ├── pick a REGISTERED candidate   → action:ai_stt_select_candidate:<candidate-id>
│       ├── TEST a REGISTERED candidate   → action:ai_stt_test_candidate:<candidate-id>   ← M2.1
│       ├── Language…                     → input:ai_media_stt:stt_language
│       └── Recognition passes…           → input:ai_media_stt:stt_passes
├── Settings                          (ai_settings)      — still no STT controls
│   └── Advanced                      (ai_settings_adv)  — still no STT controls
└── … (provider, model, usage, health, details, diagnostics: unchanged)
```

| Item | Value |
|---|---|
| **Phase** | M2.1 — Groq Whisper STT adapter + provider test capability |
| **Starting HEAD** | `8d3ba14a4fe8985adf616942bf772ae5c4f82272` — `feat(stt): establish the Speech-to-Text control plane and the Media Analysis surface` (== `origin/main`) |
| **Implementation commit** | the single commit of this phase (`git log -1 --format=%H` re-verifies it; recorded in the hand-off response) |
| **Database migration required** | **NO** — no schema change; `ai_config` is untouched (same three columns, same defaults, same upsert) |
| **New tables / columns** | **NONE** |
| **New environment variables** | **NONE** (`AI_GROQ_API_KEY` already exists in `render.yaml` and is used as-is; `GROQ_API_KEY` stays the compatibility fallback) |
| **New dependencies** | **NONE** — the project's existing `httpx` stack; no Whisper/PyTorch/ONNX/ffmpeg |
| **Provider adapters added** | **Groq Whisper transcription ONLY.** Speechmatics: **not** implemented. The Groq **chat** provider is untouched |
| **STT execution path** | The **existing** seam (`SttEngine` / `set_stt_engine` / `get_stt_engine` / `stt_available` + `media_service`) is unchanged; a minimal **candidate → engine resolver** was added in front of it |
| **Automatic fallback** | **NOT implemented** (next phase) |
| **Live Telegram verification** | **NOT PERFORMED** (no session or traffic in this environment) |
| **Live Groq API test** | **NOT PERFORMED** — no `AI_GROQ_API_KEY` exists in the implementation environment; the opt-in live test is present, skipped, and no credential is faked |

### Commit lineage

| Commit | Role |
|---|---|
| `01ff211` `feat(stt): add the bounded multi-pass STT accuracy seam` | M1.7e — the opt-in STT-only consensus seam |
| (M1.8 commit) | M1.8 — the owner-managed Gemini STT settings (`stt_model` / `stt_language` / `stt_passes`) and their runtime application |
| `4a1f9a2` `added web investigation report file` | the web research report |
| `8d3ba14` `feat(stt): establish the Speech-to-Text control plane and the Media Analysis surface` | M2.0 — the control plane (**starting HEAD of this phase**) |
| the commit of this phase | M2.1 — the Groq Whisper STT adapter, the candidate → engine resolver, the provider test capability and this report |

### Files changed by this phase

| File | Change |
|---|---|
| `backend/services/groq_stt_engine.py` **(new)** | the Groq Whisper transcription adapter: one synchronous `SttEngine` (`GroqWhisperEngine`) speaking `POST {base}/audio/transcriptions` with multipart form data, plus the closed failure-class taxonomy |
| `backend/services/stt_engine_factory.py` **(new)** | the ONE candidate → engine resolver (`build_engine`, `apply_stt_config`): a registered candidate becomes **its own provider's** engine, and anything unrunnable leaves the boundary fail-closed |
| `backend/ai/stt_provider_probe.py` **(new)** | the capability-specific provider test: a bounded probe per registered candidate, honest credential/reachability/empty-transcript states, process-local observations |
| `backend/ai/stt_control_plane.py` | the two Groq candidates flip `implemented=False → True`; documentation now points at the resolver and the probe. **Registry order, identity and storage semantics unchanged** |
| `backend/bot/handlers/ai_stt_settings.py` | the Speech-to-Text panel gains one bounded **Test** action per executable candidate and shows each candidate's probe state; the runtime hand-off now goes through the resolver |
| `backend/runtime/supervisor.py` | startup applies the persisted selection through `stt_engine_factory.apply_stt_config` (provider + model + reason in the trace) instead of the Gemini-only conversion |
| `tests/test_groq_stt_engine.py` **(new)** | 90 tests for the adapter (endpoint, multipart, models, language, parsing, classification, bounds, ceiling, isolation) |
| `tests/test_stt_provider_probe.py` **(new)** | 39 tests for the resolver, the provider test, the panel action and the opt-in live probe |
| `tests/test_ai_stt_settings.py` | the two Groq candidates are asserted as selectable; the unimplemented-candidate case now uses the one candidate that is still unimplemented; the registered-actions assertion covers both STT actions |
| `IMPLEMENTATION_REPORT.md` | this report |

**Untouched (deliberately):** `backend/services/media_service.py` (the seam,
every bound and the whole execution path), `backend/services/gemini_media_engine.py`,
`backend/services/stt_consensus.py` (reused as-is), `backend/ai/providers/**`
(including `groq.py` and `factory.py`), `backend/ai/providers/manager/**`,
`backend/ai/engine/dispatcher.py`, the tool layer, `backend/ai/media.py`,
`backend/ai/config_store.py`, `backend/ai/config/**`, the OCR path,
`backend/bot/handlers/ai.py`, the panel/input/action infrastructure,
Save / Tasks / scheduler / `RuntimeSupervisor` recovery, `requirements.txt`,
`render.yaml`, `Procfile`, `supabase/migrations/*.sql`,
`DATABASE_ARCHITECTURE.md`, ENV files and all secrets.

### Groq API integration design

| Aspect | Decision |
|---|---|
| **Endpoint** | `POST https://api.groq.com/openai/v1/audio/transcriptions` — the dedicated OpenAI-compatible **transcription** API. Chat completions are never used, and `backend/ai/providers/groq.py` is neither imported nor altered (asserted by test) |
| **Request** | `multipart/form-data`: the audio part is named `file`, with a **static, non-identifying** remote filename (`audio.ogg` / `audio.wav`) so an untrusted Telegram filename can never leak; text fields are `model` (from the registry only), `response_format=json`, and `language` **only** when the owner configured one |
| **Response** | the documented `{"text": ...}` field only. No word timestamps and no diarization are requested (the documented accuracy control variables) |
| **Credential** | `AI_GROQ_API_KEY`, then the existing `GROQ_API_KEY` compatibility fallback — resolved through `os.getenv` at build time, never stored, never logged, and read from ENV only (never Telegram/Supabase). The same pair `backend/ai/providers/factory.py` already declares |
| **Containers** | OGG/Opus (the Telegram voice-note container) and WAV. The boundary also admits FLAC, which Groq's transcription API does not document, so it is refused **locally** with `unsupported_audio` before any request. The accepted list was not broadened |
| **Audio input contract** | unchanged: the adapter receives the already-bounded, already-validated payload bytes from `media_service` (resolution, transfer, size/duration/channel/rate/MIME validation, cleanup and normalization all stay there). No second download path, no second Telegram boundary, no new media type |
| **Language** | an explicit BCP-47 tag is reduced to the ISO-639-1 primary subtag the API expects (`fa-IR` → `fa`); empty means **automatic** and sends no `language` field. Nothing is translated, transliterated or forced to English; a Persian transcript returns in Persian script |
| **Recognition passes** | the owner's bounded count (`1..3`) runs **sequential passes over the same audio** under ONE operation deadline, reconciled by the **existing** `stt_consensus.reconcile_hypotheses`; a pass is a recognition attempt, never a transport retry |
| **Bounds** | one operation deadline of **45 s**, comfortably inside the boundary's own `STT_TIMEOUT_S` (60 s) so the engine's precise reason always wins over the boundary's generic one; connect/write/pool bounds derived from what is **left** of the deadline; at most **2** sequential attempts and only for transient conditions (timeout, transport, 429, ≥500) with budget left |
| **Output ceiling** | `media_service.MAX_STT_CHARS` (the project prompt budget) is applied in the adapter, so the engine can never return more text than the boundary would keep. Empty or malformed provider output is a **failure**, never a successful empty transcription |
| **Dependencies** | `httpx` only — the same stack every provider adapter already uses; `requirements.txt` is unchanged |

### Model candidates implemented

| Candidate id | Provider | Model sent to Groq | State after this phase |
|---|---|---|---|
| `groq:whisper-large-v3` | groq | `whisper-large-v3` | **implemented** — selectable + testable |
| `groq:whisper-large-v3-turbo` | groq | `whisper-large-v3-turbo` | **implemented** — selectable + testable |
| `gemini:default` | gemini | *(provider default)* | implemented (unchanged) |
| `gemini:gemini-3.5-transcribe` | gemini | `gemini-3.5-transcribe` | implemented (unchanged) |
| `speechmatics:standard` | speechmatics | — | **still registered, not implemented** — never selectable, never probed |

The model list is **the registry**: the adapter refuses any identifier outside
its two models, and the resolver refuses any candidate outside the registry, so a
typed model string can never reach the provider on either path.

### Failure classification

One closed token per failure SITE, attached to the raised `MediaError` and emitted
as the `failure_class` field of the adapter's own bounded trace line (with the
socket phase or HTTP status when one applies):

| Token | Raised when |
|---|---|
| `missing_credential` | neither `AI_GROQ_API_KEY` nor `GROQ_API_KEY` is configured (build time — nothing is sent) |
| `unsupported_model` | the requested model is not one of the two registered Groq models |
| `auth` | HTTP 401 |
| `forbidden` | HTTP 403 |
| `invalid_model` | HTTP 404, or HTTP 400 whose provider error code names the model |
| `unsupported_audio` | HTTP 400 whose provider error code names the audio/file/media, or a container the boundary admitted but Groq does not document (refused locally) |
| `timeout` | an `httpx` timeout, tagged with the socket phase (`connect` / `write` / `pool` / `read`) |
| `transport` | connection/read/write/protocol failure |
| `rate_limit` | HTTP 429 (transient → at most one bounded retry) |
| `server` | HTTP ≥ 500 (transient → at most one bounded retry) |
| `malformed_response` | an unreadable body, a non-object shape, or a missing/non-string `text` |
| `empty_transcription` | a 200 response carrying no transcript — a FAILURE, never a successful empty transcription |
| `provider_rejection` | any other 4xx |
| `operation_deadline` | the 45 s operation budget was already spent before a leg could start |

Deterministic failures (4xx other than 429, an unreadable body, an empty
transcript, a refused container) are **never** re-sent. Recognition quality is
never reclassified: a poor or grammatically odd transcript is a **successful**
provider response, and no quality score is invented in this phase.

### Test capability

`backend/ai/stt_provider_probe.py`, surfaced as one **Test** button per
executable candidate on AI → Media Analysis → Speech-to-Text:

| State | Meaning |
|---|---|
| `not_tested` | no probe has run in this process (the default, and the state after a restart) |
| `not_implemented` | registered capability with no execution path here — **no request is made** |
| `credential_missing` | the provider has no credential on this deployment — **no request is made** |
| `passed` | the provider answered with a **non-empty transcript** (this is the ONLY success) |
| `failed` | the provider was reached and failed, or the probe could not complete; a bounded `failure_class` says how |

* **A credential existing is NOT health.** Only a completed request that returned
  a non-empty transcript is `passed`; credential presence is its own state, and a
  candidate is never marked healthy because an adapter or a key exists.
* The probe drives the **same resolver** (`stt_engine_factory.build_engine`) and
  the **same engine** the runtime uses, so it measures the real candidate path.
* The payload is a deterministic, bounded WAV (1 s, 16 kHz mono PCM16) generated
  in-process from the standard library — no bundled binary, no dependency, inside
  the boundary's own audio contract (asserted by test against
  `media_service._validate_audio_payload`). A live operator can pass real-speech
  audio instead.
* A synthetic tone carries no speech, so a healthy provider honestly returns
  `failed` + `empty_transcription` with an explanatory detail; the probe never
  invents a transcript and never claims quality.
* **Process-local, never persisted** — no `ai_config` column, no new table, no
  write of any kind: a health observation is not configuration, and a restart
  returns every candidate to `not_tested` rather than replaying a stale claim.
  The later fallback/cooldown manager may **consume** these observations; it must
  not treat them as durable state.
* Bounded and sequential: one probe at a time, in the registry's canonical order,
  each inside a 50 s outer bound (the engine's own 45 s deadline normally wins).
* Probes carry **no** credential, transcript, Telegram identifier or payload
  into any log: the trace reports provider, model, state, failure class, elapsed
  time and transcript **length** only.

### Resulting architecture

```
Telegram UI (AI → Media Analysis → Speech-to-Text)
    ↓  a registered candidate id + language + passes
persisted owner configuration  (existing ai_config row, existing 3 keys)
    ↓
STT CONTROL PLANE  (backend/ai/stt_control_plane.py — configuration only)
    ↓                                    ↘
candidate → engine resolver               provider probe (on demand, bounded)
(backend/services/stt_engine_factory.py)  (backend/ai/stt_provider_probe.py)
    ↓  gemini → GeminiMediaEngine, groq → GroqWhisperEngine(model)
the EXISTING media boundary seam (media_service.set_stt_engine)
    ↓
backend/services/groq_stt_engine.py  →  POST /openai/v1/audio/transcriptions
```

The `SttEngine` protocol, `set_stt_engine()`, `get_stt_engine()` and
`stt_available()` are **unchanged**; the media service is unaware of Telegram UI
configuration; and no owner id, chat id, message id, sender, caption, filename,
reply text, history or memory can reach the adapter (verified by test: the seam
takes `bytes` and nothing else, and the engine holds no such state).
**Automatic fallback is NOT implemented** — one selected candidate runs, and when
it cannot run the boundary stays fail-closed rather than silently transcribing
with a different model.

### Configuration / ENV behavior

* ENV holds **secrets only**: `AI_GROQ_API_KEY` (already declared in
  `render.yaml`, not duplicated) with `GROQ_API_KEY` as the existing fallback.
* Model selection, language and recognition passes remain **Telegram-controlled**
  and persisted through the existing AI configuration — no
  `AI_GROQ_STT_MODEL` / `AI_GROQ_STT_LANGUAGE` / `AI_GROQ_STT_PASSES` and no
  other behavioral variable was introduced (asserted by test).
* `AI_SPEECHMATICS_API_KEY` was **not** added: Speechmatics is not implemented in
  the self-bot, and the research mentioning it is not evidence that it is.
* Provider capability and credential discovery stay separate: the registry knows
  what can run, the probe reports whether this deployment can actually run it.

### Tests and exact results

| Suite | Result |
|---|---|
| `tests/test_groq_stt_engine.py` (new) | **`90 passed`** |
| `tests/test_stt_provider_probe.py` (new) | **`39 passed, 1 skipped`** (the skip is the opt-in live Groq probe — no credential here) |
| `tests/test_ai_stt_settings.py` (extended) | **`87 passed`** |
| `tests/test_36_ai_settings_ux.py`, `tests/test_11_runtime_wiring.py`, `tests/test_33_ai_telemetry.py`, `tests/test_ai_presentation_redesign.py` | **`119 passed`** |
| the nine media / STT boundary suites (`test_media_stt.py`, `test_media_dedicated_stt.py`, `test_media_stt_reliability.py`, `test_media_stt_multipass.py`, `test_media_stt_language.py`, `test_media_stt_benchmark.py`, `test_stt_consensus.py`, `test_media_gemini_engine.py`, `test_media_ai_integration.py`) | **`442 passed`** (byte-identical to the M2.0 count — the boundary is untouched) |
| the generic Groq chat-provider suites (`tests/test_52_you_search.py`, `tests/test_model_tester.py`) and the AI behaviour suites (`test_02_ai_flow`, `test_19_ai_actions`, `test_ai_state_consistency`, `test_ai_menu_state_consistency`, `test_ai_provenance`, `test_34_ai_model_ui`, `test_35_ai_retry_ux`) | **`243 passed`** |
| **Full suite** | **`3729 passed, 25 skipped, 3 warnings` in 111.03 s** (`24` of the skips are pre-existing; `1` is the new opt-in live probe. No test was deleted, weakened or skipped) |

The new suites cover, among others: both Groq models resolving to their own
model; the two registered models being exactly the adapter's model list; an
arbitrary model identifier refused at **both** the adapter and the resolver; the
credential variables matching the project's existing convention; the
`AI_GROQ_API_KEY` → `GROQ_API_KEY` fallback; the credential never logged (and
redacted from an echoing error body); the documented endpoint, bearer header,
multipart construction, static remote filename and the absence of
timestamps/diarization/prompt/temperature; explicit Persian (`fa-IR` → `fa`,
returned in Persian script, untranslated) and automatic language; success parsing;
empty and malformed responses as failures; every classification token
(401/403/404/429/5xx/400-families/timeout-with-phase/transport/deadline); the
bounded retry (transient retried once, deterministic never re-sent); the
operation deadline staying inside the boundary's bound and starting no request
when spent; the shared character ceiling; the existing consensus seam for
multi-pass recognition; the seam taking `bytes` and nothing else; no Telegram
metadata injectable; the adapter not importing the chat provider or its manager;
no behavioral ENV variable; no new dependency; the adapter not importing the
probe or the resolver (one-way dependency); the resolver failing closed without a
credential and never substituting another provider; a legacy value keeping its
previous engine; the probe's states being distinct (untested / not implemented /
credential missing / passed / failed); a failed probe carrying its deterministic
failure class; the probe payload satisfying the boundary's own audio contract; the
probe running in canonical order; observations staying process-local; the probe
module importing no Telegram/handler layer; the panel offering a Test action for
every executable candidate (and none for the unimplemented one); and the
panel/notice wording never implying health from configuration alone.

**Syntax / whitespace:** `python -m py_compile` clean on every changed and new
Python file; `git diff --check` clean. `pytest` from the repository root collects
cleanly (the probe module is deliberately **not** named `*_test.py`, so pytest
cannot mistake it for a test module).

### Live verification status

* **Telegram:** NOT performed — the panels, buttons, action and notices were
  exercised against the real handlers, the real registries and the real panel
  builders, but no Telegram session rendered them.
* **Groq API:** NOT performed. No `AI_GROQ_API_KEY` was available in this
  implementation environment, so no request was made and no success is claimed.
  The opt-in live test (`tests/test_stt_provider_probe.py`, skipped without a
  credential) is the safe path for an operator who has one: it uses the bounded
  probe payload, reports only state/model/elapsed/failure class, never prints or
  persists the key, and never treats a synthetic tone as proof of recognition
  quality.

Neither status may be reported as success, and Groq is **not** claimed healthy
merely because the adapter exists.

### Intentionally NOT implemented in this phase

* **Automatic fallback / failover / cooldown / retry orchestration across
  providers** — the control plane can represent the ordered pool, the resolver
  resolves exactly ONE selected candidate, and no second candidate is ever tried.
* **Speechmatics** (no adapter, no key, no request) — still a registered,
  non-selectable capability.
* **Per-owner candidate re-ranking**, a persisted health/cooldown store, and any
  `ai_config` column or Supabase table for probe results.
* **Any change to recognition quality, STT consensus logic, OCR, the media
  boundary's execution behavior, the dispatcher, the tool layer,
  `ProviderManager`, the scheduler, `RuntimeSupervisor` recovery, the Supabase
  schema, `DATABASE_ARCHITECTURE.md` or the Render configuration.**
* **Any new dependency, any local Whisper/PyTorch/ONNX/ffmpeg stack, any second
  media download path or Telegram media boundary, and any new ENV variable.**

### Known limitations

1. No automatic fallback: if the selected candidate fails, the media operation
   reports the classified failure; the next candidate is not tried (by design,
   next phase).
2. Probe results are process-local by design; a restart forgets them, and
   `credential_missing` is reported independently of reachability.
3. The synthetic probe payload contains no speech, so a healthy provider reports
   `failed` + `empty_transcription` until the probe is run with real-speech audio.
   This never claims quality and never fabricates a transcript.
4. Groq rate limits (documented 20 requests/minute, 2 000/day on the free plan)
   are surfaced as `rate_limit` and bounded by one retry, but no cooldown state
   exists yet.
5. `groq:*` containers are limited to OGG/Opus and WAV, the two Groq documents
   that the boundary corroborates.
6. The M1.8/M2.0 notes stand: the `ai_config` STT columns still require the
   pending manual migration; until it is applied the settings degrade to the
   documented in-memory fallback, and `DATABASE_ARCHITECTURE.md` §7 still
   describes the superseded M1.8 semantics.
7. Recognition quality (class A) remains unmeasured and is not claimed.

### Deferred work

* The **STT health/fallback manager** that consumes these capabilities:
  active candidate → provider health → cooldown → next active candidate →
  bounded retry/failover → honest failure, in front of the existing seam.
* Persisted (if ever wanted) health/cooldown state — deliberately absent today.
* The **Speechmatics adapter** and its credential declaration.
* Per-owner fallback re-ranking.
* Applying the pending `ai_config` migration and the documentation-only
  `DATABASE_ARCHITECTURE.md` §7 semantics refresh.
* Class A (recognition quality) measurement — `INVESTIGATION.md` §19 stays open.

### Exact next stage — M2.2: the STT health/fallback manager

1. Add the ordered **active → fallback** execution in front of the existing
   `media_service.set_stt_engine` seam, using this phase's resolver (never a
   second STT pipeline) and the probe's closed failure taxonomy.
2. Add the bounded **cooldown** state for `rate_limit` / `timeout` / `transport`
   failures, with the deterministic failure classes failing over immediately and
   an honest failure emitted when every candidate is exhausted.
3. Only after that, consider persisting the ordering/cooldown state and
   re-ranking it from Telegram.

### Document version

This document reflects the M2.1 state: Speech-to-Text is configured under
**AI → Media Analysis** by picking a REGISTERED candidate; the two Groq Whisper
candidates are now executable through a dedicated adapter on
`POST {base}/audio/transcriptions` (multipart, OpenAI-compatible) and testable
through a bounded, credential-honest, process-local provider probe; the
candidate → engine resolution is one small seam in front of the **unchanged**
`media_service` STT boundary; no schema, ENV, dependency or chat-provider change
was made; **automatic fallback and the Speechmatics adapter are deferred to the
next phase.** If code changes invalidate any section, update this document in the
same commit.
