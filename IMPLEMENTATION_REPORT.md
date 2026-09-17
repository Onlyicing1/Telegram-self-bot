# IMPLEMENTATION REPORT — CURRENT STATE

## Latest phase — Media Processing M1.7: the dedicated Gemini transcription model as a controlled STT option

Repository `Onlyicing1/Telegram-self-bot` · branch `main` · state as of 2026-09-17.

### Current stage

M1.7. The live Persian STT problem is **recognition quality**, not routing: M1.6 proved
that both `stt` and `sst` reach the direct-transcription path, and that the transcript
itself (not a second LLM) is delivered. M1.7 therefore adds the **documented dedicated
transcription model** (`gemini-3.5-transcribe`) as an **explicit, opt-in STT engine
option** inside the existing `GeminiMediaEngine` boundary, and leaves every other media
behaviour byte-for-byte intact. This phase does not fix anything by itself — it makes the
Persian comparison (model × language mode) **configurable, bounded and measurable**.

### Commit lineage

| Commit | Role | Files |
|---|---|---|
| `7e1e69a` `docs: record STT quality investigation findings` | **starting HEAD** (== `origin/main` at the time this phase began) | `INVESTIGATION.md` |
| the implementation commit of this phase (SHA in the hand-off response; `git log -2 --format=%H` re-verifies both) | M1.7: dedicated-transcription route, explicit STT model/language selection, bounded tracing, focused tests | `backend/services/gemini_media_engine.py`, `tests/test_media_gemini_engine.py`, `tests/test_media_dedicated_stt.py` (new) |
| the documentation commit of this phase | synchronizes this report with the repository state after the implementation commit; no production or test change | `IMPLEMENTATION_REPORT.md` only |

| Item | Value |
|---|---|
| Media boundary (`media_service.py`, `backend/ai/media.py`, `backend/telegram_api/media.py`) | **unchanged** — not touched by this phase |
| Direct-STT path (classifier, bypass, answer contract, delivery) | **unchanged** — `media_ai_service.py` was not modified; the classifier is still regex-free |
| ProviderManager / adapters / registry / `vision()` / fallback mesh | **untouched** |
| OCR, PDF/DOCX extraction, save system, scheduler, Supabase, deployment files | **untouched** |
| Dependencies / ENV files / secrets | **none** — no new package, no new credential, no ENV file edit |
| Live Telegram verification | **NOT PERFORMED** by this phase (no session, credential or traffic here) |
| Live Gemini request | **NOT PERFORMED** — every HTTP interaction in this phase was a scripted in-process stub |

### Purpose of this phase

The M1.5/M1.6 source audit proved that no stage **inside the repository** alters a
transcript on the direct-STT path (audio reaches Gemini byte-identical, MIME is
container-derived, extraction and normalization are value-safe, one request, no retry, no
second model). The remaining error class is the recognition itself — live Persian voice
notes came back with real-word substitutions (`دیدم`→`دیه`, `گپ`→`کپ`, `خلاصه`→`حالا`) and
one Latin token inside Persian output.

M1.7 addresses exactly that, without touching routing: the general media model
(`gemini-3.5-flash-lite`, Generate Content API, instruction-only language control) stays
the default, and the dedicated ASR model becomes selectable **explicitly** through ENV,
using **its own documented request contract**.

### Current architecture / data flow

```
explicit STT request  (UNCHANGED: is_direct_stt_request, regex-free)
→ dispatcher._media_target / _try_media_analysis            (UNCHANGED)
→ media_ai_service.answer_media_request                     (UNCHANGED)
→ media_service.resolve_media_message                       (UNCHANGED)
→ media_service.analyze_media                               (UNCHANGED: bounds, validation,
                                                             _run_stt to_thread, timeout,
                                                             normalization, cap)
→ GeminiMediaEngine.transcribe                              (CHANGED INSIDE ONLY)
     ├─ AI_GEMINI_STT_MODEL unset  → Generate Content: POST /models/<media model>:generateContent
     │                              parts [ STT_INSTRUCTION, audio ]   (M1.5c behaviour, identical)
     └─ AI_GEMINI_STT_MODEL set    → Interactions:      POST /v1beta/interactions
                                    input [ audio ] + generation_config.transcription_config
→ MediaAnalysis.content == the transcript                    (UNCHANGED)
→ dispatcher._build_fast_path_result → delivery              (UNCHANGED, no second model)
```

Both routes use the **same** `GeminiMediaEngine`, the **same** `SttEngine` seam, the same
inline-vs-Files transport decision, the same upload adapter and the same
`finally`-deletion. There is no second pipeline, no second download path, no new engine,
no new abstraction outside this module and no `ProviderManager` involvement anywhere in
STT.

### Model-selection architecture

| ENV variable | Meaning when set | Meaning when unset/blank |
|---|---|---|
| `AI_GEMINI_MEDIA_MODEL` (existing) | the general media model for OCR **and** for the general STT route | falls back to `AI_GEMINI_MODEL`, then `DEFAULT_MEDIA_MODEL` = `gemini-3.5-flash-lite` |
| `AI_GEMINI_STT_MODEL` (**new**, `STT_MODEL_ENV_VAR`) | the dedicated transcription model; `transcribe` switches to the Interactions API | **the general media route** — no substitution, no default model |
| `AI_GEMINI_STT_LANGUAGE` (**new**, `STT_LANGUAGE_ENV_VAR`) | a BCP-47 code: `language_codes` on the dedicated route, one appended instruction line on the general route | automatic language detection (multilingual behaviour preserved) |

- `resolve_stt_model()` / `resolve_stt_language()` return `("", "")` when nothing is
  configured; both values pass through the project's existing deprecation map
  (`resolve_model`), so a retired model configured in ENV cannot poison a request.
- `DEDICATED_TRANSCRIPTION_MODEL = "gemini-3.5-transcribe"` is a named constant, but it is
  **never selected implicitly**: nothing in the code switches to it, and the string in ENV
  is what activates the route.
- **OCR can never move**: `recognize()` always sends `self._model` (the general media
  model) to Generate Content with `OCR_INSTRUCTION`, whatever STT is configured.
- Engine state is still config-only: `__slots__ = (_api_key, _model, _key_env_var,
  _stt_model, _stt_language)`. No Telegram object, no chat/message id, no event-loop state.
- No new configuration system: two ENV variables following the existing
  `AI_<PROVIDER>_*` convention, read at provisioning time only.

### Dedicated-STT request behaviour (as implemented)

Request: `POST https://generativelanguage.googleapis.com/v1beta/interactions`, header
`x-goog-api-key` (never in the URL), body:

```json
{
  "model": "gemini-3.5-transcribe",
  "input": [ { "type": "audio", "data": "<base64>", "mime_type": "audio/ogg" } ],
  "generation_config": { "transcription_config": { "mode": { "type": "verbatim" } } },
  "store": false
}
```

with the uploaded-file variant `{ "type": "audio", "uri": "<files/... uri>", "mime_type": ... }`
above the inline budget, and with `"language_codes": ["fa-IR"]` added inside
`transcription_config` **only** when `AI_GEMINI_STT_LANGUAGE` is set.

Fields the API documents for this model and that this engine sends, and nothing else:

| Field | Source of the contract |
|---|---|
| `model` | Models page: `gemini-3.5-transcribe` is a **Stable** model ("Low-latency speech-to-text model with utterance-based language detection, speaker diarization, word-level timestamps, and custom vocabulary biasing") |
| `input[].type/data/mime_type/uri` | Audio-transcription + Audio-understanding guides (inline base64 or Files-API URI, with `mime_type`) |
| `generation_config.transcription_config.language_codes` | "Language detection and hints": BCP-47 codes, omitted/empty ⇒ automatic detection and code-switching |
| `generation_config.transcription_config.mode` | "Transcription modes": `"smart"` or a verbatim object `{"type": "verbatim", ...}`; verbatim is the default and is what fidelity needs |
| `store` | Interactions API overview: `store=false` opts out of the API's default server-side retention |

**Deliberately NOT sent** (each verified as either absent from this contract or actively
harmful to the experiment):

- `temperature`, `topK`, `topP`, `candidateCount`, `maxOutputTokens`,
  `system_instruction`, `response_format` — none belongs to the documented transcription
  request; sampling controls are not a reliable ASR-fidelity lever (the legacy Generate
  Content route keeps its own two, unchanged).
- `mode.diarization_mode` — single-speaker voice notes; also incompatible with custom
  vocabulary.
- `mode.timestamp_granularities` — the API documents that word timestamps **may degrade**
  accuracy; they are not needed to compare transcripts.
- `custom_vocabulary` — no demonstrated need and no concrete vocabulary list; adding it
  would confound the measurement.
- No **text instruction** on the dedicated route: the model is documented to accept the
  audio alone (and it is an ASR model, not a prompt-following multimodal model).
- `thinking_level` / background execution / `previous_interaction_id` — not part of a
  single bounded transcription.

`STT_INSTRUCTION` remains exactly as it was and is still the general route's only
language-shaping input. The multi-variable risk is therefore contained: B/C differ only in
whether `language_codes` is present, and A/D differ only by the appended language line.

### Language modes

| Mode | Dedicated route | General route |
|---|---|---|
| Automatic (default) | `language_codes` omitted ⇒ documented automatic detection / code-switching | `STT_INSTRUCTION` alone (byte-identical to M1.5c) |
| Explicit (e.g. `fa-IR`) | `transcription_config.language_codes = ["fa-IR"]` | `STT_INSTRUCTION` + one appended, deterministic sentence naming the BCP-47 code |

No language is forced for all audio: the automatic mode stays the default, and neither the
boundary nor the delivery layer knows about languages.

### Audio-format behaviour

Unchanged and **not broadened**: the boundary already validated OGG/WAV/FLAC, and
`gemini_mime_type()` still derives the MIME from the container signature
(`audio/ogg` for OGG/Opus/`application/ogg` aliases, `audio/wav` for the WAV aliases,
`audio/flac` for the FLAC aliases). No MP3/M4A/AAC/WebM was added, no ffmpeg, no local
decode/resample/conversion, no Telegram download change. The one new guard is negative:
if a non-audio payload reaches the dedicated route, the engine refuses it **before** any
request (`"The dedicated transcription model accepts audio input only."`).

### Inline vs Files API, and timeouts

- Inline base64 at or below the existing `INLINE_PAYLOAD_MAX_BYTES` (15 MiB); above it the
  **existing** Files API adapter is reused (`start` → `upload, finalize` → optional bounded
  readiness checks → the interaction references the returned `uri`), and the remote file is
  deleted in a `finally` block. A failed delete never masks the result.
- The engine's own finite bound (`STT_TIMEOUT_S = 40s`, inside the boundary's 60s) is
  applied to the interaction request as well, and uploads share one deadline
  (`_remaining`), so no request can hang.
- One media operation = one bounded HTTP operation. No retry loop was added, and
  `_generate` (Generate Content) is still never retried.

### Failure behaviour

The dedicated model failing is **final for that operation**: the engine raises the
boundary's existing `MediaError` (mapped from HTTP status by the unchanged
`_raise_for_status`: key rejected 401/403, rate limit 429, **model not found / not enabled
404**, server error ≥500, refused 400), the boundary adds its STT stage and the owner gets
the existing media failure contract. There is **no** automatic
dedicated → general → provider chain, and a test asserts that a failing dedicated request
never produces a Generate Content request. This is deliberate: an automatic fallback would
make the A/B/C/D comparison uninterpretable.

### Observability

One bounded, non-sensitive line per media operation:

```
GEMINI_MEDIA_ENGINE kind=speech-to-text engine=GeminiMediaEngine model=gemini-3.5-transcribe
  transport=interactions mime=audio/ogg bytes=35941 language=fa-IR mode=verbatim
  chars=51 elapsed_ms=1240 status=ok
```

`status=failed` is traced on every failure path (before the error propagates).
**Never logged**: transcript text, raw audio, API key, message id, chat id, sender,
caption, filename. Error messages remain sanitized and credential-redacted (`***`), and the
remote upload keeps using the static non-identifying display name `lifeos-media`.

### Direct-STT path (unchanged, re-verified)

The classifier (`media_ai_service.is_direct_stt_request`, finite form inventory, **no
regex**), the media-type gate (Voice/Audio only), the bypass branch, the honest provenance
(`provider="local"`, `model="gemini-stt-deterministic"`) and the delivery layer are
untouched. The dedicated engine now sits behind the same seam, so the direct path simply
receives a (different) transcript and still delivers it verbatim with no provider round — a
test in this phase drives the real `Dispatcher` + real `ProviderManager` + the dedicated
engine over a scripted API stub and asserts `result.response == TRANSCRIPT` with
`provider.prompts == []`, while an analytical request over the same setup still reaches the
provider exactly once.

### Files changed by this phase

| File | Change |
|---|---|
| `backend/services/gemini_media_engine.py` | the dedicated-transcription route (`_run_dedicated_transcription`, `_transcription_body`, `_transcribe_interaction`, `_transcribe_interaction_from_upload`, `_extract_interaction_text`), explicit STT model/language resolution (`resolve_stt_model`, `resolve_stt_language`, `STT_MODEL_ENV_VAR`, `STT_LANGUAGE_ENV_VAR`, `DEDICATED_TRANSCRIPTION_MODEL`, `INTERACTIONS_ENDPOINT`), the optional appended language line (`stt_instruction`), the audio-only guard, and the bounded `_log_run` tracing replacing the old single-line log |
| `tests/test_media_gemini_engine.py` | the engine's `__slots__` contract extended with the two config-only STT fields (`_stt_model`, `_stt_language`); the new ENV names added to the isolation fixture |
| `tests/test_media_dedicated_stt.py` | **new** — 60 tests (see below) |
| `IMPLEMENTATION_REPORT.md` | rewritten as the current-state report (this document) |

### Tests and exact results

All commands ran in a project-local Python 3.10.12 environment
(`.venv`, gitignored) with `pytest 9.1.1` and the repository's own
`backend/requirements.txt`; no network, no Gemini credential, no live Telegram.

```
.venv/bin/python -m pytest tests/test_media_dedicated_stt.py -q
    .................................................... 60 passed in 0.56s

.venv/bin/python -m pytest tests/test_media_direct_stt.py tests/test_media_ai_integration.py \
    tests/test_media_stt.py tests/test_media_stt_language.py tests/test_media_gemini_engine.py \
    tests/test_media_processing.py tests/test_media_document_extraction.py \
    tests/test_media_image_ocr.py tests/test_media_dedicated_stt.py -q
    462 passed in 37.85s

.venv/bin/python -m pytest tests/ -q
    3339 passed, 24 skipped, 3 warnings in 113.71s

.venv/bin/python -m py_compile backend/services/gemini_media_engine.py \
    tests/test_media_gemini_engine.py tests/test_media_dedicated_stt.py
    OK (no output)

git diff --check
    clean
```

`tests/test_media_dedicated_stt.py` covers, per the task's list:

1. dedicated model selection (`AI_GEMINI_STT_MODEL` ⇒ `gemini-3.5-transcribe`, through the
   existing deprecation map);
2. current general model selection (unset ⇒ `stt_transport == "generate_content"`,
   `stt_model == ""`);
3. OCR remains on the general media model **even with the dedicated STT model configured**
   (URL, `OCR_INSTRUCTION`, image MIME, no `interactions` request) and the `AI_GEMINI_MEDIA_MODEL`
   override still governs it;
4. STT selects the dedicated model (URL = `/v1beta/interactions`, `model` field);
5. explicit `fa-IR` ⇒ `language_codes == ["fa-IR"]`;
6. automatic mode ⇒ `language_codes` absent, traced as `language=auto`;
7. verbatim mode ⇒ `mode == {"type": "verbatim"}`);
8. absence of unverified controls — exact body key sets plus a scan for
   `temperature/topK/topP/candidateCount/maxOutputTokens/system_instruction/response_format/
   timestamp_granularities/word/diarization/custom_vocabulary/smart/thinking`, and no text
   instruction on the dedicated route;
9. OGG/Opus mapping (`audio/ogg`, `audio/opus`, `application/ogg` aliases);
10. WAV mapping (all four aliases); 11. FLAC mapping (both aliases);
12. inline payload path (`data` present, no `uri`, no upload request);
13. Files API path (resumable start, one finalize carrying the bytes, `uri` referenced);
14. remote cleanup (`deleted == [<files/...>]`, including when the interaction fails, and a
    failing delete never masking the result);
15. timeout behaviour (`ReadTimeout` ⇒ `MediaError … timed out`);
16. provider/API error normalization (401/403/429/404/500/400) with exactly one request and
    **no** Generate Content fallback; credential redaction;
17. response text extraction (documented `steps[].content[].text`, multi-step join,
    `output_text` only as a fallback, ignored `word_info` items);
18. empty transcript ⇒ honest empty (boundary reason "No speech was detected in the audio.")
    and non-completed statuses ⇒ `MediaError`;
19. direct-STT still bypasses `ProviderManager` (real `Dispatcher`, transcript delivered
    verbatim, `provider.prompts == []`, for both `stt` and the `sst` alias);
20. analytical media still uses `ProviderManager` (one prompt, ending in the analysis
    content);
21. OCR behaviour unchanged (above) plus the untouched engine bounds;
22. Telegram metadata never enters the Gemini request (caption, filename, chat id, message
    id; no `caption/filename/sender/chat_id/message_id` wording);
23. the direct-STT classifier is unchanged and regex-free (positive/negative forms, no `re`
    module in `media_ai_service`);
24. the existing media-boundary tests remain green (the 402-test media suite and the full
    3339-test suite above).

Two further guards: the engine module gains **no new import** (`module members ==
{base64, logging, os, time, httpx, media_service}`, no `re`), and its tracing carries no
transcript, audio, credential or Telegram identifier.

**These tests prove request construction, parsing, bounds and failure behaviour. They prove
nothing about recognition quality** — no fixture in this repository can, and none is
faked. That is what the live comparison is for.

### Live Telegram verification status

**NOT PERFORMED.** No live Telegram session, no live Gemini call and no live Persian voice
note were used in this phase; `INVESTIGATION.md` was not modified. The transcript quality of
`gemini-3.5-transcribe` on this project's audio is therefore **entirely unmeasured** here,
and nothing in this report should be read as a live observation.

### Free-tier and model-availability uncertainty

- `gemini-3.5-transcribe` is documented on the Gemini API models page as a **Stable**
  dedicated speech-to-text model with the endpoint `gemini-3.5-transcribe`, and the
  Audio-transcription guide documents its request contract. That is what this implementation
  is based on (read from the official docs on 2026-09-17); a general web search returned no
  results in this environment, so no third-party material was used.
- **Free-tier eligibility for this project is NOT verified.** The pricing page was not
  consulted and no billing/entitlement call was made. Availability must be verified manually
  in AI Studio / project settings before the comparison is run.
- No programmatic availability check, probe request or fake validation was added: an
  unavailable/not-enabled model surfaces as the honest `HTTP 404 … could not find the
  configured model for speech-to-text (…)` `MediaError`.
- The single request field whose acceptance is **not** demonstrated by a transcription
  example is `store: false`. It is documented as a general Interactions control ("you can opt
  out of storage for any interaction", incompatible only with background execution, which is
  never used here). If a live run rejects it with HTTP 400, the failure is loud and honest
  and the fix is to remove that one key — there is no silent fallback.

### What was intentionally NOT changed

`media_service.py` (resolution, transfer, validation, `_run_stt`, normalization, caps,
timeouts); `backend/ai/media.py`; `backend/telegram_api/media.py` (download behaviour);
`media_ai_service.py` (classifier, bypass, provenance); `backend/ai/engine/dispatcher.py`;
`ProviderManager`, every provider adapter, the registry, `vision()` and the fallback mesh;
`RuntimeSupervisor` (only the existing provisioning entry point is used, and its status dict
shape is unchanged); OCR; PDF/DOCX; the save system; the scheduler; Supabase and
`DATABASE_ARCHITECTURE.md`; `render.yaml`; `Procfile`; `backend/requirements.txt`;
unrelated commands and tests; the accepted MIME list (no MP3/M4A/AAC/WebM, no ffmpeg);
retry/fallback policy (still none); and the direct-STT answer contract.

### Exact next stage — M1.7b: the owner's live Persian comparison

Operational notes: the STT selection is read **at provisioning time** (runtime startup), so
each configuration is set in ENV and the runtime restarted between runs. Only one
configuration is ever active at a time, and one voice note produces exactly one request.

| Run | `AI_GEMINI_STT_MODEL` | `AI_GEMINI_STT_LANGUAGE` | What it exercises |
|---|---|---|---|
| A | *(unset)* | *(unset)* | `gemini-3.5-flash-lite` + Generate Content + automatic language (today's behaviour) |
| B | `gemini-3.5-transcribe` | *(unset)* | dedicated model, documented automatic language detection |
| C | `gemini-3.5-transcribe` | `fa-IR` | dedicated model with the Persian language pinned |
| D | *(unset)* | `fa-IR` | general model with the appended explicit-language line |

Procedure: for each configuration, restart with that ENV, then send the **same** Persian
voice note several times (and at least one analytical request such as
`این ویس درباره چیه؟`) by replying to it with `این رو STT کن` and `این رو SST کن`. Compare
the delivered texts and, for each run, capture the matching trace line
`GEMINI_MEDIA_ENGINE kind=speech-to-text …` (`model`, `transport`, `language`, `mode`,
`bytes`, `chars`, `elapsed_ms`, `status`) plus `media_completed` / `direct_stt_completed`.
Record which configuration reproduced or removed the observed substitutions, and write the
observation into `INVESTIGATION.md`. If run B or C fails with HTTP 404, the model is not
enabled for the project — that is a finding to record, not a code bug. Any further engine
work (instruction tuning, vocabulary, timestamps, diarization) stays out of scope until that
comparison is on record.
