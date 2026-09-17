# IMPLEMENTATION REPORT — CURRENT STATE

## Latest phase — M2.2: Speechmatics STT adapter + ONE global provider test

Repository `Onlyicing1/Telegram-self-bot` · branch `main` · state as of 2026-09-17.

### Current stage

M2.2 — **the Speechmatics transcription capability and the provider-test UX
cleanup.** `speechmatics:standard`, the candidate M2.0 registered as *capability
only* and M2.1 left unimplemented, is now **executable and selectable** through
its own batch adapter, and the Speech-to-Text panel no longer carries one Test
button per candidate: it has **ONE bounded `Test all providers` control** that
probes every executable candidate in the registry's canonical order and refreshes
the panel once. Nothing moved back into AI → Settings / Advanced, no model is
typed by the owner, and **automatic fallback is still NOT implemented.**

```
AI                                    (ai)
├── Media Analysis                    (ai_media)
│   ├── Text recognition (OCR)        (ai_media_ocr)   — unchanged (no OCR change)
│   └── Speech-to-Text                (ai_media_stt)
│       ├── TEST ALL PROVIDERS             → action:ai_stt_test_all            ← M2.2 (ONE global action)
│       ├── pick a REGISTERED candidate    → action:ai_stt_select_candidate:<candidate-id>
│       ├── Language…                      → input:ai_media_stt:stt_language
│       └── Recognition passes…            → input:ai_media_stt:stt_passes
├── Settings                          (ai_settings)      — still no STT controls
│   └── Advanced                      (ai_settings_adv)  — still no STT controls
└── … (provider, model, usage, health, details, diagnostics: unchanged)
```

| Item | Value |
|---|---|
| **Phase** | M2.2 — Speechmatics STT adapter + ONE global provider test control |
| **Starting HEAD** | `9d63766b1630b8e589a1de01d7be120db02d1d90` — `feat(stt): add the Groq Whisper STT adapter, the candidate → engine resolver and the provider test` (== `origin/main`) |
| **Implementation commit** | the single commit of this phase (`git log -1 --format=%H` re-verifies it; recorded in the hand-off response) |
| **Database migration required** | **NO** — no schema change; `ai_config` is untouched (same three STT columns, same defaults, same upsert, same NULL convention) |
| **New tables / columns** | **NONE** |
| **New environment variables** | **ONE secret: `AI_SPEECHMATICS_API_KEY`** (`sync: false` in `render.yaml`). No behavioral variable was added — model/language/passes stay Telegram-managed |
| **New dependencies** | **NONE** — the project's existing `httpx` stack; no Speechmatics client package, no Whisper/PyTorch/ONNX/ffmpeg |
| **STT execution path** | The **existing** seam (`SttEngine` / `set_stt_engine` / `get_stt_engine` / `stt_available` + `media_service`) is unchanged; the M2.1 **candidate → engine resolver** gained one more provider branch |
| **Provider adapters added** | **Speechmatics batch transcription ONLY.** The Groq and Gemini routes are untouched |
| **Automatic fallback** | **NOT implemented** (next phase) |
| **Live Telegram verification** | **NOT PERFORMED** (no session or traffic in this environment) |
| **Live provider verification** | **NOT PERFORMED** — neither `AI_GROQ_API_KEY` nor `AI_SPEECHMATICS_API_KEY` exists in this implementation environment, so no request was made and no provider is claimed healthy or reachable |
| **Recognition-quality claim** | **NONE** — a passing capability probe is a transport/credential/response-path fact, never a claim that Persian recognition is good |

### Commit lineage

| Commit | Role |
|---|---|
| `01ff211` `feat(stt): add the bounded multi-pass STT accuracy seam` | M1.7e — the opt-in STT-only consensus seam |
| (M1.8 commit) | M1.8 — the owner-managed Gemini STT settings (`stt_model` / `stt_language` / `stt_passes`) and their runtime application |
| `4a1f9a2` `added web investigation report file` | the web research report |
| `8d3ba14` `feat(stt): establish the Speech-to-Text control plane and the Media Analysis surface` | M2.0 — the control plane |
| `9d63766` `feat(stt): add the Groq Whisper STT adapter, … and the provider test` | M2.1 — the Groq adapter, the resolver, the per-candidate probe (**starting HEAD of this phase**) |
| the commit of this phase | M2.2 — the Speechmatics adapter, the ONE global provider test, the secret declaration and this report |

### Files changed by this phase

| File | Change |
|---|---|
| `backend/services/speechmatics_stt_engine.py` **(new)** | the Speechmatics batch transcription adapter: one synchronous `SttEngine` (`SpeechmaticsBatchEngine`) that creates a job, waits for it under ONE deadline and fetches the plain-text transcript, plus the closed failure-class taxonomy |
| `backend/services/stt_engine_factory.py` | one more provider branch in the resolver, and the apply path is now ONE provider-agnostic `_apply_adapter` (the resolver is the only place an engine is built) |
| `backend/ai/stt_control_plane.py` | `speechmatics:standard` flips `implemented=False → True` with an updated note. **Registry order, identity and storage semantics unchanged** |
| `backend/ai/stt_provider_probe.py` | now **provider-agnostic**: it no longer imports a provider module; the bounded failure class is read off the raised failure and the missing-credential reason comes from the resolver |
| `backend/bot/handlers/ai_stt_settings.py` | the per-candidate Test buttons are gone; ONE `Test all providers` row sits above the candidate rows; one bounded multi-candidate notice replaces the per-provider message; the panel text states plainly that this is a synthetic-tone capability probe |
| `render.yaml` | `AI_SPEECHMATICS_API_KEY` declared with `sync: false` (the only configuration change) |
| `tests/test_speechmatics_stt_engine.py` **(new)** | 92 tests for the adapter (endpoint, auth, multipart, job cycle, language, classification, bounds, retries, ceiling, isolation, secret hygiene) |
| `tests/test_stt_provider_probe.py` | the global-test UI/probe coverage, the Speechmatics resolver coverage and two opt-in live probes |
| `tests/test_ai_stt_settings.py` | every registered candidate is selectable; the unimplemented-candidate guards are covered through a forged capability; the registration assertion covers the new action |
| `IMPLEMENTATION_REPORT.md` | this report |

**Untouched (deliberately):** `backend/services/media_service.py` (the seam, every
bound and the whole execution path), `backend/services/groq_stt_engine.py`,
`backend/services/gemini_media_engine.py`, `backend/services/stt_consensus.py`
(reused as-is), `backend/ai/providers/**` (including `groq.py`,
`openai_compat.py` and `factory.py`), `backend/ai/providers/manager/**`,
`backend/ai/engine/dispatcher.py`, the tool layer, `backend/ai/media.py`,
`backend/telegram_api/media.py`, `backend/ai/config_store.py`, the OCR path,
`backend/bot/handlers/ai.py`, the panel/input/action infrastructure,
Save / Tasks / scheduler / `RuntimeSupervisor`, `requirements.txt`, `Procfile`,
`supabase/migrations/*.sql`, `DATABASE_ARCHITECTURE.md`, ENV files and all
secrets.

### Speechmatics API integration summary

| Aspect | Decision |
|---|---|
| **API** | the official **batch v2 REST API** (`https://asr.api.speechmatics.com/v2`), verified against the published API reference and the vendor's own client source. Speechmatics transcribes **asynchronously**, so one recognition is a bounded job cycle: `POST /v2/jobs` → `GET /v2/jobs/{id}` until `done` → `GET /v2/jobs/{id}/transcript?format=txt`. No WebSocket, no management platform, no temporary-token exchange |
| **Authorization** | `Authorization: Bearer <API key>` on every leg. Never logged, never persisted, never placed in Telegram/Supabase; a provider error body is redacted before it can surface |
| **Request** | `multipart/form-data`: the `config` part is the documented JSON (`{"type":"transcription","transcription_config":{"language":…,"operating_point":…}}`) and the audio is the `data_file` part under a **static, non-identifying** name (`audio.ogg` / `audio.wav` / `audio.flac`), so an untrusted Telegram filename can never leak. No diarization and no extra output are requested — the recognition control variables |
| **Model mapping** | the registered candidate model IS the API's `operating_point` (`standard`), through an explicit table so an unregistered value can never reach the API |
| **Credential** | `AI_SPEECHMATICS_API_KEY` — the ONE declared secret. No compatibility alias was invented: the repository has no existing Speechmatics credential convention (`backend/ai/providers/factory.py` declares none), so a second variable would have been a fabricated convention |
| **Audio input contract** | unchanged: the adapter receives the already-bounded, already-validated payload bytes from `media_service` (resolution, transfer, size/duration/channel/rate/MIME validation, cleanup and normalization all stay there). OGG/Opus, WAV and FLAC — the containers the batch API documents **and** the boundary corroborates — are accepted; anything else is refused locally before any request. The boundary's accepted list was not broadened, no second download path and no second Telegram boundary were added |
| **Language** | an explicit BCP-47 tag is reduced to the ISO-639-1 primary subtag the API documents (`fa-IR` → `fa`); empty means **automatic detection**, which this API spells with its own `language: "auto"` token — never a fabricated code. Nothing is translated, transliterated or forced to English; a Persian transcript returns in Persian script |
| **Recognition passes** | the owner's bounded count (`1..3`) reuses the **existing** STT-only consensus (`stt_consensus.reconcile_hypotheses`): sequential passes over the same audio, each one full job cycle, under ONE deadline. A pass is a recognition attempt, never a transport retry, and a deterministic failure stops the loop |
| **Bounds** | one engine operation deadline of **45 s**, inside the boundary's own `STT_TIMEOUT_S` (60 s) so the engine's precise reason always wins; connect/write/pool bounds derived from what is **left** of the deadline and re-derived for every leg; a bounded status poll; at most **2** sequential attempts and only for transient conditions |
| **Output ceiling** | `media_service.MAX_STT_CHARS` is applied in the adapter, so the engine can never return more text than the boundary would keep. Empty or unreadable provider output is a **failure**, never a successful empty transcription |

### Failure classification

One closed token per failure SITE, attached to the raised `MediaError` and emitted
as the `failure_class` field of the adapter's own bounded trace line (with the
socket phase or HTTP status when one applies). The vocabulary is the same one the
Groq adapter already uses — `missing_credential`, `unsupported_model`, `auth`,
`forbidden`, `invalid_request`, `unsupported_audio`, `timeout`, `transport`,
`rate_limit`, `server`, `malformed_response`, `empty_transcription`,
`provider_rejection`, `operation_deadline` — so the provider probe reports the
same tokens whichever adapter failed.

Deterministic failures (a rejected credential or key, a refused payload or config,
an unreadable body, an empty transcript, a rejected/failed job) are **never**
re-sent. Transient ones (`timeout`, `transport`, `429`, `>= 500`) may repeat the
**submit** leg at most once while the deadline has room; a transient failure while
**waiting** for an already-submitted job only repeats the status read (at most
three consecutive times) and then fails — it never submits a second job, so a
retry can never silently double the translation work. Recognition quality is never
reclassified: a poor transcript is a successful provider response.

### Global "Test all providers" behavior

| Aspect | Behavior |
|---|---|
| **Control** | exactly ONE button row, `Test all providers` → `action:ai_stt_test_all`, rendered **above** the candidate rows. No candidate has its own Test button, including the active one |
| **What it tests** | every **implemented** candidate, **sequentially**, in the registry's canonical order — the probe owns that order (`stt_provider_probe.test_candidates()`), so the handler adds no second loop |
| **Unimplemented candidates** | reported as `not available` and **never sent a request** |
| **Credential-less providers** | reported as `no credential` and **never sent a request**; credential presence is a separate state from a successful request and never a pass |
| **Rendering** | ONE notice listing every candidate's bounded state (`test passed · <ms>` / `no credential` / `test failed · <failure class>` / `not available`), rendered once on top of the same Speech-to-Text panel — never a message per provider |
| **Payload** | the probe's bounded in-process WAV (1 s, 16 kHz mono PCM16), validated against the media boundary's own audio contract. It contains no speech |
| **Quality claim** | none, and the notice says so: it is explicitly a capability probe that does not measure recognition quality |
| **Persistence** | none — process-local observations only; no `ai_config` write, no new column, no new table, and a restart returns every candidate to `not tested` |
| **Ordering determinism** | the registry order is a tuple literal, so the probe order is identical on every process |

### Real replied-to audio for the probe — investigated, deliberately NOT added

The task asked whether the existing architecture can safely feed a **real**
replied-to Voice/Audio message into the same bounded probe. It cannot, and no
mechanism was invented to force it:

* the global test is a **callback-query** action (`ActionHandler(event, extra,
  chat_id)`), and a button press carries no replied-to message — there is no
  deterministic media target in that scope at all;
* the only place a reply is available is the **pending-input** path, whose
  handlers receive the owner's *typed text*, not the replied-to media object;
  deriving audio from it would mean adding a new Telegram-context mechanism —
  exactly what this phase was told not to do.

So the global provider test remains a **bounded capability/transport probe**, it
says so on the panel and in its result, and **real Persian recognition
benchmarking remains a separate next step** (see Deferred work). The probe API
already accepts explicit `audio=` bytes, so that step can be built without
touching the adapters.

### Current provider candidate states

| Candidate id | Provider | Execution | Credential | Test state |
|---|---|---|---|---|
| `gemini:default` | gemini | implemented (unchanged) | `AI_GEMINI_API_KEY` | not tested until probed |
| `gemini:gemini-3.5-transcribe` | gemini | implemented (unchanged) | `AI_GEMINI_API_KEY` | not tested until probed |
| `groq:whisper-large-v3` | groq | implemented (M2.1) | `AI_GROQ_API_KEY` (→ `GROQ_API_KEY`) | not tested until probed |
| `groq:whisper-large-v3-turbo` | groq | implemented (M2.1) | `AI_GROQ_API_KEY` (→ `GROQ_API_KEY`) | not tested until probed |
| `speechmatics:standard` | speechmatics | **implemented (M2.2)** | **`AI_SPEECHMATICS_API_KEY`** | not tested until probed |

Every registered candidate is now selectable and testable. **An adapter existing
is not a health claim**: only a completed request that returned a non-empty
transcript is reported as passed, and none has been run here.

### Resulting architecture

```
Telegram UI (AI → Media Analysis → Speech-to-Text)
    ↓  a registered candidate id + language + passes
persisted owner configuration  (existing ai_config row, existing 3 keys)
    ↓
STT CONTROL PLANE  (backend/ai/stt_control_plane.py — configuration only)
    ↓                                    ↘
candidate → engine resolver               provider probe (on demand, bounded,
(backend/services/stt_engine_factory.py)  one request per candidate, registry order)
    ↓  gemini → GeminiMediaEngine · groq → GroqWhisperEngine(model)
       speechmatics → SpeechmaticsBatchEngine(operating_point)
the EXISTING media boundary seam (media_service.set_stt_engine)
    ↓
POST {base}/jobs → poll → GET /jobs/{id}/transcript?format=txt
```

The `SttEngine` protocol, `set_stt_engine()`, `get_stt_engine()` and
`stt_available()` are **unchanged**; the media service is unaware of Telegram UI
configuration; and no owner id, chat id, message id, sender, caption, filename,
reply text, history or memory can reach an adapter (verified by test: the seam
takes `bytes` and nothing else, and each engine holds no such state).
**Automatic fallback is NOT implemented** — one selected candidate runs, and when
it cannot run the boundary stays fail-closed rather than silently transcribing
with a different model.

### Configuration / ENV behavior

* ENV holds **secrets only**. `AI_SPEECHMATICS_API_KEY` was added to `render.yaml`
  with `sync: false`; `AI_GROQ_API_KEY` (already declared) is reused, never
  duplicated.
* No behavioral variable exists for either transcription provider — no
  `AI_*_STT_MODEL`, `AI_*_STT_LANGUAGE`, `AI_*_STT_PASSES` or an API-base
  override. Model selection, language and passes remain Telegram-controlled and
  persisted through the existing `ai_config` row.
* **Render ENV is not a database**: changing the STT candidate, language or pass
  count needs no redeploy and no restart.

### Tests and exact results

| Suite | Result |
|---|---|
| `tests/test_speechmatics_stt_engine.py` (new) | **`92 passed`** |
| `tests/test_stt_provider_probe.py` (extended) | **`56 passed, 2 skipped`** (the skips are the two opt-in live probes — no credential here) |
| `tests/test_ai_stt_settings.py` (extended) | **`90 passed`** |
| `tests/test_groq_stt_engine.py` (unchanged, re-run) | **`90 passed`** |
| `tests/test_36_ai_settings_ux.py` (unchanged, re-run) | **`9 passed`** |
| the media / STT boundary suites (`test_media_stt.py`, `test_media_dedicated_stt.py`, `test_media_stt_reliability.py`, `test_media_stt_multipass.py`, `test_media_stt_language.py`, `test_media_stt_benchmark.py`, `test_stt_consensus.py`, `test_media_gemini_engine.py`, `test_media_ai_integration.py`, `test_media_direct_stt.py`, `test_media_processing.py`) | **`559 passed`** — the boundary's behavior is unchanged |
| **Full suite** | **`3841 passed, 26 skipped, 3 warnings` in 111.89 s** (`24` of the skips are pre-existing; the other `2` are the opt-in live provider probes, Groq and Speechmatics. No test was deleted or weakened) |

The new/updated coverage includes: the adapter's model list equalling the
registered Speechmatics candidates; an unregistered model refused; a missing
credential reported without a request; the credential variable being the ONE
declared name; no behavioral ENV variable; the documented batch base URL and
`POST /jobs`; the bearer header; multipart `config` + `data_file` and the static
remote filename; the config shape (type / language / operating_point, and nothing
else); the audio containers recognised and an undocumented one refused locally
without a request; empty input; `fa-IR` → `fa` and automatic → the provider's own
`auto`; create → poll → transcript with `format=txt`; running-until-done polling;
a rejected/failed job; an empty transcript as a failure; malformed create/status
bodies; the transcript ceiling; every HTTP classification (401/403/404/422/429/5xx
and the three 400 families); the timeout phase; transport failures on every leg;
the bounded submit retry versus a deterministic failure; the bounded status
re-reads that never re-submit the job; the operation deadline (including "spent
deadline starts no request" and a per-leg derived timeout); passes defaulting to
one and one job per pass; the engine holding no Telegram state; no Telegram
identifier in any request; the credential and the transcript never logged; an
error body unable to echo the key; no chat-completion surface and no provider
module imported; no Speechmatics client dependency; the resolver building the
Speechmatics engine only from a registered candidate; a missing credential failing
closed with **no** Gemini/Groq substitution (and the reverse directions);
the probe being provider-agnostic; the global action delegating to the probe's own
multi-candidate run in registry order with one result per candidate; unimplemented
candidates never requested; credential presence still not a pass; exactly one
`Test all providers` button and no per-candidate Test buttons; the button sitting
above the candidate rows; the active candidate having no `Use` button and the
others having one; an unimplemented candidate having no button at all; and the
notice never carrying a credential or a transcript.

**Syntax / whitespace:** `python -m py_compile` clean on every changed and new
Python file; `git diff --check` clean.

### Live verification status

* **Telegram:** NOT performed — the panels, buttons, actions and notices were
  exercised against the real handlers, the real registries and the real panel
  builders, but no Telegram session rendered them.
* **Groq API:** NOT performed — no `AI_GROQ_API_KEY` in this environment.
* **Speechmatics API:** NOT performed — no `AI_SPEECHMATICS_API_KEY` in this
  environment, so no request was made and no reachability is claimed. The opt-in
  live test (`tests/test_stt_provider_probe.py`, skipped without a credential) is
  the safe path for an operator who has one: it uses the bounded probe payload,
  reports only state/model/elapsed/failure class, never prints or persists the
  key, and never treats a synthetic tone as proof of recognition quality.

Speechmatics is **not** claimed healthy merely because the adapter exists, and no
recognition-quality improvement is claimed because a provider answered.

### Intentionally NOT implemented in this phase

* **Automatic fallback / failover / cooldown / retry orchestration across
  providers** — the control plane can represent the ordered pool, the resolver
  resolves exactly ONE selected candidate, and no second candidate is ever tried.
  Explicitly deferred, because the tested providers' Persian quality is still
  unmeasured: failover cannot fix recognition quality.
* **Any health manager, failure counters, ranking or persistent health state** —
  observations stay process-local; no `ai_config` column, no Supabase table.
* **Real Persian recognition benchmarking** — the probe payload is a synthetic
  tone and is never presented as a quality benchmark.
* **A real replied-to audio feed for the probe** (see above) — no new
  Telegram-context mechanism was invented to force it into this phase.
* **Any change to recognition quality, the STT consensus logic, OCR, the media
  boundary's execution behavior, the dispatcher, the tool layer,
  `ProviderManager`, the scheduler, `RuntimeSupervisor` recovery, the Supabase
  schema, `DATABASE_ARCHITECTURE.md` or the Render configuration beyond the one
  secret declaration.**
* **Any new dependency, any local Whisper/PyTorch/ONNX/ffmpeg stack, any second
  media download path or Telegram media boundary, and any behavioral ENV
  variable.**

### Known limitations

1. No automatic fallback: if the selected candidate fails, the media operation
   reports the classified failure; the next candidate is not tried (by design,
   next phase).
2. Speechmatics transcribes **asynchronously**, so a job that outlives the 45 s
   engine deadline fails honestly with `operation_deadline` even though the
   provider might have finished later. Long audio on the Telegram side (the
   boundary admits up to 300 s) can therefore exceed the deadline; the control
   plane, not the adapter, decides which candidate to use.
3. `language: "auto"` is the provider's documented automatic mode; Speechmatics
   may **reject** a job whose language it cannot identify confidently, which
   surfaces honestly as `provider_rejection`.
4. Probe results are process-local by design; a restart forgets them, and
   `credential_missing` is reported independently of reachability.
5. The synthetic probe payload contains no speech, so a healthy provider reports
   `failed` + `empty_transcription` (or a provider-side no-speech rejection)
   until the probe is run with real-speech audio. This never claims quality and
   never fabricates a transcript.
6. The M1.8/M2.0 note stands: the `ai_config` STT columns still require the
   pending manual migration; until it is applied the settings degrade to the
   documented in-memory fallback, and `DATABASE_ARCHITECTURE.md` §7 still
   describes the superseded M1.8 semantics.
7. Recognition quality (class A) remains unmeasured and is not claimed.

### Deferred work

* The **STT health/fallback manager** that consumes these capabilities:
  active candidate → provider health → cooldown → next active candidate →
  bounded retry/failover → honest failure, in front of the existing seam.
* **Real Persian recognition benchmarking** on 30–50 real voice messages across
  the registered candidates (the step that must come *before* automatic fallback).
* Persisted (if ever wanted) health/cooldown state — deliberately absent today.
* Per-owner fallback re-ranking.
* Applying the pending `ai_config` migration and the documentation-only
  `DATABASE_ARCHITECTURE.md` §7 semantics refresh.

### Exact next stage — M2.3: real Persian recognition benchmarking

1. Feed **real** Persian voice notes through the SAME bounded probe
   (`stt_provider_probe.test_candidate(..., audio=<bounded bytes>)` already
   accepts explicit audio) and record per-candidate transcripts and timings
   **outside** the repository's deterministic suite.
2. Decide, on that evidence, which candidates are worth keeping active and in
   which order — the decision the control plane already models but deliberately
   does not act on.
3. Only after that, the ordered active → cooldown → fallback execution in front
   of the existing `media_service.set_stt_engine` seam (never a second STT
   pipeline), consuming this phase's resolver and failure taxonomy.

### Document version

This document reflects the M2.2 state: Speech-to-Text is configured under
**AI → Media Analysis** by picking a REGISTERED candidate; all five registered
candidates (Gemini ×2, Groq Whisper ×2, Speechmatics ×1) are executable, and all
are testable through **ONE** bounded `Test all providers` control that reports
each candidate's own state without ever claiming health or recognition quality;
the candidate → engine resolution is one small seam in front of the **unchanged**
`media_service` STT boundary; the only configuration change is the
`AI_SPEECHMATICS_API_KEY` deployment secret; **automatic fallback and real Persian
quality benchmarking are deferred to the next phases.** If code changes
invalidate any section, update this document in the same commit.
