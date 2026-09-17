# IMPLEMENTATION REPORT — CURRENT STATE

## Latest phase — Media Processing M1.7c: the bounded STT transport of the explicit Voice/Audio path

Repository `Onlyicing1/Telegram-self-bot` · branch `main` · state as of 2026-09-17.

### Current stage

M1.7c. The live STT problem has **two separate failure classes** and this phase
addresses the second one only:

| Class | Status before this phase | Status after this phase |
|---|---|---|
| **A. Recognition quality** (`INVESTIGATION.md` §19: a transcript WAS produced, with wrong words) | open, unaffected by any local code path | **still open** — unchanged, and no claim about quality is made here |
| **B. Request timeout** (§20: NO transcript produced, because the engine's own HTTP bound expired first) | open: the bound was a PER-SOCKET-PHASE `httpx.Client(timeout=40.0)`, every failure collapsed into one sentence, and the route's audio representation was the one the model's own documentation does not demonstrate | **addressed**: ONE operation deadline, the documented `uri` representation, classified failures, one bounded retry/fallback, bounded cleanup, per-leg observability |

The phase changes the **transport, the deadline and the failure identity** of the
dedicated transcription engine. It does **not** change routing, the media
boundary, the direct-STT answer contract, the prompt/context layers, the
provider mesh, the model selection or the request's transcription fields, and it
adds no second pipeline, no second download and no dependency.

### Commit lineage

| Commit | Role | Files |
|---|---|---|
| `308938756e25360d87e8c266afeb57766ef4151f` (`docs: complete the dedicated STT timeout investigation in INVESTIGATION.md`) | **starting HEAD** (== `origin/main` when this phase began) | `INVESTIGATION.md` |
| the implementation commit of this phase (SHA in the hand-off response; `git log -1 --format=%H` re-verifies it) | M1.7c: the bounded STT transport | `backend/services/gemini_media_engine.py`, `tests/test_media_dedicated_stt.py`, `tests/test_media_gemini_engine.py`, `tests/test_media_stt_reliability.py` (new) |
| the documentation commit of this phase | synchronizes this report with the repository state after the implementation commit; no production or test change | `IMPLEMENTATION_REPORT.md` only |

| Item | Value |
|---|---|
| `INVESTIGATION.md` | **unchanged** — this phase implements; it does not re-investigate |
| Media boundary (`media_service.py`, `backend/ai/media.py`, `backend/telegram_api/media.py`) | **unchanged** |
| `media_ai_service.py` (direct-STT classifier, bypass, provenance), `dispatcher.py`, `ai_unified.py` | **unchanged** |
| `ProviderManager`, adapters, registry, `vision()`, prompt/context/memory/tool layers | **untouched** |
| Supabase / `DATABASE_ARCHITECTURE.md` / Save / Task system / Scheduler / UI | **untouched** |
| Dependencies (`backend/requirements.txt`), `render.yaml`, `Procfile`, ENV files, secrets | **unchanged** — no new package, no new credential, no new configuration variable |
| Live Telegram verification | **NOT PERFORMED** (no session, credential or traffic here) |
| Live Gemini verification | **NOT PERFORMED** — every HTTP interaction in this phase was a scripted in-process transport |

### Purpose of this phase

`INVESTIGATION.md` §20 proved, from source, exactly five things about the
timeout incident, and nothing more:

1. the owner-visible sentence is generated **locally** by
   `gemini_media_engine._post_json` from `STT_TIMEOUT_S = 40.0`;
2. that bound is a **per-socket-phase** bound (connect/write/read/pool), **not** a
   bound on the operation, and the four phases are indistinguishable afterwards;
3. the **operation deadline** (`deadline = started + 40`) was consulted only by
   the non-inline legs, raised no error of its own, and silently degraded into a
   0.5 s floor;
4. the **inline** branch sent the audio as base64 `data` — a representation the
   official documentation shows for the Interactions API but **not** for
   `gemini-3.5-transcribe`, whose own transcription guide demonstrates only the
   Files API `uri` form;
5. cleanup ran with a **fresh full per-phase** bound, so a slow `DELETE` could
   push the owner-visible failure past the boundary's own 60 s bound.

§20 also established that ProviderManager, a second LLM, retries, duplicate
requests, model fallback, event-loop blocking, executor starvation, transcoding,
MIME divergence and the `[Errno 11]` task-repository path were **not** causes.
This phase implements the fixes for (2)–(5) and nothing else.

### Exact STT architecture after implementation

```
explicit STT request  ("stt" / "sst" / "transcribe" / …; UNCHANGED, regex-free)
→ dispatcher._media_target / _try_media_analysis                 (UNCHANGED)
→ media_ai_service.answer_media_request                          (UNCHANGED)
→ media_service.resolve_media_message → analyze_media            (UNCHANGED: deterministic target,
                                                                  bounded download, MIME/container
                                                                  validation, size/duration/channel
                                                                  bounds, temp cleanup,
                                                                  to_thread(engine.transcribe),
                                                                  normalization, cap)
→ GeminiMediaEngine.transcribe                                   (CHANGED INSIDE ONLY)
     ├─ AI_GEMINI_STT_MODEL unset → Generate Content route: POST /models/<media model>:generateContent
     └─ AI_GEMINI_STT_MODEL set   → Dedicated route:      POST /v1beta/interactions
→ MediaAnalysis.content == the transcript                         (UNCHANGED)
→ dispatcher._build_fast_path_result → delivery                   (UNCHANGED: no second model)
```

Both routes now run under **one operation deadline** with per-leg derived
request timeouts and classified failures. The dedicated route's bounded transport
plan is:

```
attempt 1:  Files API start → upload, finalize → (readiness, only if not ACTIVE)
            → POST /v1beta/interactions  { "type": "audio", "uri": …, "mime_type": … }
attempt 2:  ONLY if attempt 1 failed TRANSIENTLY and ≥ STT_MIN_ATTEMPT_S of the
            deadline remains:
              payload ≤ INLINE_PAYLOAD_MAX_BYTES → inline { "type": "audio", "data": …, … }
              otherwise                          → the same URI form once more
```

Exactly ONE attempt is ever in flight; the plan is fixed **before** the operation
(`_dedicated_transports`), so it can never loop, never fan out and never depend on
runtime state.

### Exact Gemini model / endpoint / transport

| Item | Value (unchanged by this phase except where noted) |
|---|---|
| General media model | `AI_GEMINI_MEDIA_MODEL` → `AI_GEMINI_MODEL` → `gemini-3.5-flash-lite` |
| Dedicated STT model | `AI_GEMINI_STT_MODEL` only (the constant `gemini-3.5-transcribe` is never selected implicitly) |
| STT language | `AI_GEMINI_STT_LANGUAGE` (BCP-47, e.g. `fa-IR`); unset ⇒ automatic detection |
| OCR endpoint | `POST {base}/models/<media model>:generateContent` (unchanged) |
| General STT endpoint | the same Generate Content endpoint, `parts [STT_INSTRUCTION, inlineData|fileData]` (unchanged) |
| Dedicated STT endpoint | `POST https://generativelanguage.googleapis.com/v1beta/interactions`, key in the `x-goog-api-key` header (never in the URL) |
| Dedicated request body | `model`, `input:[{type:"audio", uri|data, mime_type}]`, `generation_config.transcription_config{mode:{type:"verbatim"}[, language_codes:[…]]}`, `store:false` — **unchanged field inventory** |
| **Dedicated audio representation (CHANGED)** | **primary = the Files API `uri` form** (`start` → `upload, finalize` → the returned URI), because that is the only representation the official documentation shows for this model; **`data` is only the bounded fallback** |
| Uploaded-file display name | static, non-identifying `lifeos-media` (unchanged) |
| Accepted audio containers | OGG/WAV/FLAC only, derived from the payload signature (unchanged; no MP3/M4A/AAC/WebM, no ffmpeg) |

### Timeout / deadline behaviour

| Layer | Bound | Scope |
|---|---|---|
| AI execution envelope | 240 s | whole `engine.execute` (unchanged) |
| Boundary STT bound | `media_service.STT_TIMEOUT_S` = 60 s | the awaited result of `to_thread(engine.transcribe)` (unchanged) |
| **Engine operation deadline** | `STT_OPERATION_DEADLINE_S` = **45 s** | **the whole engine operation**: upload + readiness + the transcription request + the single bounded retry/fallback |
| Derived request timeouts | `connect ≤ 10 s`, `write ≤ 20 s`, `pool ≤ 10 s`, **`read` = whatever is left of the deadline** | per leg, computed from ONE remaining-budget number |
| Deadline enforcement | `_required_budget` raises `FAILURE_DEADLINE` | a spent budget is its **own** failure — it is never turned into a tiny HTTP timeout |
| Remote cleanup | `STT_CLEANUP_TIMEOUT_S` = 5 s | the `DELETE` only; deliberately **outside** the operation deadline so cleanup can never delay the owner-visible outcome |
| Retry gate | `STT_MIN_ATTEMPT_S` = 8 s of remaining budget | a second attempt can never exceed the deadline |

Value derivation, stated instead of assumed: the boundary's own bound is 60 s and
the engine's worst case is `45 + 5 = 50 s`, leaving ~10 s of margin for the
boundary's worker-thread return path — so the owner always receives the
**engine's** precise reason rather than the boundary's generic one. The change is
therefore not "40 → larger": it replaces four independent per-phase bounds with
one total operation bound, and adds the missing deadline failure.

### Fallback / retry behaviour

* **Bounded**: `STT_MAX_ATTEMPTS = 2` (the initial attempt plus at most one
  retry/fallback), sequential only, never concurrent.
* **Transient only**: retried conditions are `httpx` timeouts and transport errors
  (`ConnectError`, `ReadError`, `WriteError`, `RemoteProtocolError`) and the HTTP
  statuses `429` and `≥ 500`.
* **Never retried** (deterministic, fail-closed on the first attempt): `401/403`,
  `404`, any other `4xx` (including a refused request), an unreadable body, a
  malformed response shape, a non-completed interaction status, an empty
  transcript, a non-audio payload, an unsupported container and a spent deadline.
* **Fallback representation**: for the dedicated route the second attempt prefers
  the inline `data` form when the payload fits the documented inline budget (it
  needs no upload at all), otherwise it repeats the URI form. This is the ONLY
  transport fallback in the system, it is decided before the operation, and it is
  covered by tests.
* **Route fallback**: none, unchanged — a failing dedicated request is **never**
  re-asked on the general model (the recognition comparison stays interpretable).
* **OCR**: unchanged single attempt (`max_attempts = 1`); the retry policy exists
  only on the STT route.

### Accuracy-related decisions

* The dedicated model, the explicit `AI_GEMINI_STT_LANGUAGE` hint, `verbatim` mode,
  the per-request `language_codes`, the absence of sampling controls, the absence
  of any second LLM and the byte-identical audio all remain exactly as M1.7 left
  them. The transcript is still returned to the owner verbatim.
* The one accuracy-relevant change is about the **contract**, not the text: the
  request now sends the audio in the representation the documentation shows for
  this model, so a possible cause of a provider-side stall or rejection is
  removed rather than papered over.
* No transcript normalization, no regex correction, no fuzzy matching, no
  "cleanup" pass, no transliteration and no invented text were added. Persian
  script, ZWNJ, code-switching and legitimate Latin tokens pass through unchanged
  (asserted by tests).
* **Recognition quality (class A) is not improved by this phase and is not
  claimed to be.** No local fixture can prove it; the owner's comparison remains
  the only measurement.

### Latency-related decisions

Removed from the critical path:

* the per-socket-phase bound that could expire while the operation had budget
  left, and the 0.5 s deadline floor (`_remaining`) that silently replaced an
  exhausted budget;
* up to 1 s of gratuitous sleep before the **first** file-readiness check — the
  readiness check now runs immediately and only waits *between* attempts;
* the unbounded-looking cleanup leg (a fresh full per-phase bound) — now a 5 s
  bound outside the deadline;
* the ambiguity between "deadline exceeded" and "socket timeout" (two different,
  explicit failures now).

Added latency, stated honestly: the dedicated route's **primary** transport is
the Files API, so a small voice note pays two extra small requests (resumable
`start` + `upload, finalize`) on the same host. That is deliberate: the URI form
is the only representation the model's documentation demonstrates, and the extra
round trips are bounded (`connect ≤ 10 s`) and sit inside the same operation
deadline. The faster inline form is still used, as the bounded fallback.

Measured locally (scripted transport, **no real network**) with a deliberate
0.25 s upload leg, i.e. the derivation itself rather than provider latency:

```
upload_start     connect=10.0  write=20.0  read=45.0000  pool=10.0
upload_finalize  connect=10.0  write=20.0  read=44.9987  pool=10.0
interaction      connect=10.0  write=20.0  read=44.7478  pool=10.0
delete           connect=5.0   write=5.0   read=5.0000   pool=5.0
```

### Reliability-related decisions

* One coherent operation deadline, per-request timeouts derived from the
  remaining budget, no nested timeout that can defeat the outer bound, and no
  error path that can silently become a shorter HTTP timeout.
* A spent deadline, a socket-phase timeout, an HTTP rejection, a malformed body, a
  stalled file-processing step and a transport failure are now **distinct** and
  are all reported with the same bounded, honest message contract (the owner still
  sees `media_stt_engine: <precise reason>`, never a credential, a payload or a
  Telegram identifier).
* Cleanup is per **attempt** (the first attempt's remote file is removed before the
  fallback runs) and can never mask a result or a failure.

### Cleanup behaviour

* Local: unchanged — the boundary owns the temporary directory and removes it on
  every path (`finally`), including timeout and cancellation.
* Remote: the uploaded file is deleted in a `finally` per attempt, with its own
  5 s bound, before any fallback uses a different representation.
* A failed delete is logged (`GEMINI_MEDIA_ENGINE_UPLOAD_CLEANUP_FAILED`), never
  raised: the API auto-expires uploaded files.

### Observability

Two bounded, non-sensitive line families (never a transcript, raw audio,
credential, chat id, message id, sender, caption or filename):

```
GEMINI_MEDIA_ENGINE_STAGE kind=speech-to-text stage=request_start attempt=1 transport=uri bytes=115 elapsed_ms=-
GEMINI_MEDIA_ENGINE_STAGE kind=speech-to-text stage=upload_finalize attempt=1 transport=- bytes=115 elapsed_ms=250
GEMINI_MEDIA_ENGINE_STAGE kind=speech-to-text stage=interaction attempt=1 transport=- bytes=0 elapsed_ms=151
GEMINI_MEDIA_ENGINE kind=speech-to-text engine=GeminiMediaEngine model=gemini-3.5-transcribe \
  transport=interactions mime=audio/ogg bytes=115 language=auto mode=verbatim chars=41 \
  elapsed_ms=403 status=ok attempts=1 deadline_s=45 failure_class=-
```

* The **stage** lines close §20.17's "request start NOT observable" gap and carry
  per-leg timings, so a stall is attributable after the fact.
* `failure_class` is one bounded token, extended with the evidence it lacked:
  `interaction_timeout:read`, `upload_timeout:connect`, `http_rejection:http=429`,
  `operation_deadline`, `malformed_response`, `file_processing`, `transport_failure`.
* `attempts`, `deadline_s`, `bytes`, `mime`, `elapsed_ms`, `status` cover PART 11's
  measurement list at the engine boundary; the boundary keeps tracing
  `stt_engine_invoked bytes=…` / `stt_engine_returned chars=…`.

### HTTP client lifetime (PART 7, evaluated — deliberately NOT changed)

The engine still builds **one client per leg** (`httpx.Client(timeout=<derived>)`)
and closes it. A shared/pooled client was evaluated against §20.15 and rejected
for now: the engine is called from the boundary's `asyncio.to_thread` workers, so
module-level client state would be cross-request mutable state (stale connections
after a failure, an unbounded pool, no deterministic shutdown) and would break the
engine's `__slots__`/no-shared-state contract that the boundary's offload assumes.
What WAS changed is the object handed to the client: it is now an
`httpx.Timeout` derived from the operation budget instead of a bare
`timeout=40.0` that quietly meant four independent bounds.

### Files changed by this phase

| File | Change |
|---|---|
| `backend/services/gemini_media_engine.py` | the failure-class vocabulary and the bounded transport helpers (`_error`, `_failure_field`, `_budget`, `_required_budget`, `_request_timeout`, `_timeout_phase`, `_dedicated_transports`, `_decode_json`); the ONE STT operation deadline and its derived phase bounds; the documented `uri`-primary dedicated transport with the single bounded `data` fallback; the bounded retry policy; `_perform` (one classified, budget-derived HTTP call); a deadline-aware, immediate-first readiness check; a bounded, deadline-independent remote cleanup; the extended `_log_run` plus the new per-leg `_trace_stage` |
| `tests/test_media_dedicated_stt.py` | the transport-plan expectations (URI primary, inline only as the fallback), the retry-attempt counts per HTTP status, the new bounded bounds assertions, the stub's separate finalize vs interaction payloads, and two new tests (transient upload → inline fallback; first attempt's file deleted before the fallback) |
| `tests/test_media_gemini_engine.py` | the STT attempt counts per HTTP status, the new bounds assertions, and a deterministic `400` for the cleanup test |
| `tests/test_media_stt_reliability.py` | **new** — 52 tests for the bounded transport, deadline arithmetic, failure classes and phases, retry/fallback rules, cleanup, observability, event-loop safety, concurrency and the audio fixtures |
| `IMPLEMENTATION_REPORT.md` | rewritten as the current-state report (this document) |

### Tests and exact results

All commands ran in a project-local Python 3.10.12 environment (`.venv`,
gitignored) from `backend/requirements.txt` plus `pytest 9.1.1` /
`pytest-asyncio`; **no network, no Gemini credential, no live Telegram**.

```
.venv/bin/python -m pytest tests/test_media_stt_reliability.py -q
  52 passed in 1.27s        (new suite)

.venv/bin/python -m pytest tests/test_media_direct_stt.py tests/test_media_ai_integration.py \
    tests/test_media_stt.py tests/test_media_stt_language.py tests/test_media_gemini_engine.py \
    tests/test_media_processing.py tests/test_media_document_extraction.py \
    tests/test_media_image_ocr.py tests/test_media_dedicated_stt.py \
    tests/test_media_stt_reliability.py -q
  516 passed in 40.76s

.venv/bin/python -m pytest tests/ -q
  3393 passed, 24 skipped, 3 warnings in 116.82s    (baseline before this phase: 3339 passed, 24 skipped)

.venv/bin/python -m py_compile backend/services/gemini_media_engine.py \
    tests/test_media_dedicated_stt.py tests/test_media_gemini_engine.py \
    tests/test_media_stt_reliability.py
  OK (no output)

git diff --check
  clean
```

What the new suite pins, per the task's list (tests assert the **actual request
bodies**, not merely that a function was called):

* the transport plan (`uri` first; `inline` only as the fallback; `uri` again above
  the inline budget; exactly two attempts — never a loop);
* the derived phase bounds, the 45 s operation deadline, the 5 s cleanup bound, the
  8 s retry gate and the "worst case fits inside 60 s" property;
* the explicit operation-deadline failure (with **no** request sent);
* the failure classes and the recorded socket phase (`connect`/`write`/`read`/
  `pool`), including the `failure_class=…` trace field;
* the bounded retry rules: transient (timeout / transport error / `429` / `5xx`)
  retried once; deterministic `4xx`, malformed bodies, non-completed statuses and
  empty transcripts never retried; no attempt without budget; attempts strictly
  sequential (an explicit start/end ordering assertion);
* the fallback representation and its skipped-upload property;
* cleanup per attempt, before the fallback, never masking a result or a failure,
  with its own bound;
* per-leg traces with elapsed times, and the absence of transcript/audio/key/URI/
  caption/filename text in every log record;
* OCR's untouched single-attempt contract next to the STT policy;
* event-loop non-blocking behaviour, bounded concurrent transcriptions (4
  operations ⇒ 4 requests, ≤ 4 threads), and no shared HTTP state on the engine;
* audio fixtures: silence (sent byte-identical, empty transcript stays empty),
  Persian + ZWNJ + code-switching text returned unchanged, a short-but-valid
  container sent unmodified, and an unsupported container refused before any
  request.

**These tests prove request construction, bounds, classification and failure
behaviour. They prove nothing about recognition quality** — no fixture in this
repository can, and none is faked.

### Live Telegram verification status

**NOT PERFORMED.** No live Telegram session was used; the direct-STT route is
exercised end-to-end (real `Dispatcher` + real `ProviderManager` + the engine over
a scripted API transport) by the existing `tests/test_media_direct_stt.py` and
`tests/test_media_dedicated_stt.py`, which still assert `provider.prompts == []`
and `result.response == TRANSCRIPT`.

### Live Gemini verification status

**NOT PERFORMED.** Every HTTP interaction in this phase was a scripted in-process
`httpx` transport. Consequently:

* whether `gemini-3.5-transcribe` accepts the **inline `data`** form for this
  project is still **[UNKNOWN]** — which is exactly why it is now only the
  fallback and not the primary path;
* whether the Files API upload leg behaves as documented on the project's tier is
  **[UNKNOWN]** (it is documented for this model, and now covered by tests);
* the real latency distribution of one Persian voice note on either route is
  **[UNKNOWN]**;
* whether the 45 s operation deadline is generous enough for the provider's real
  behaviour is **[UNKNOWN]** — it is a bound, not a measurement.

No claim of a live success is made anywhere in this report.

### Known limitations

1. **Recognition quality is untouched.** A wrong Persian word is still a wrong
   Persian word; this phase cannot and does not fix class A.
2. The primary representation now costs two extra requests per transcription. If
   the live comparison shows the inline form is accepted by this model, the
   primary/fallback order is a one-line change in `_dedicated_transports`
   (deliberately left as a decision for the live evidence, not guessed here).
3. A timeout inside a worker thread still cannot be cancelled: if the boundary's
   60 s bound fires first, the engine's request keeps running and its outcome is
   discarded (documented at `media_service.py:1194`-`1198`, unchanged by design).
4. The deadline is enforced **between** legs. A single leg cannot exceed its own
   derived bound, but a leg that is slow-but-progressing may still consume its
   whole remaining budget (that is the intent).
5. `_FILE_READY_ATTEMPTS = 5` with a 1 s delay remains the readiness policy; it is
   now deadline-aware and starts immediately, but it is still a fixed small bound.
6. No programmatic model-availability probe was added (an unavailable model
   surfaces as the honest HTTP 404 `MediaError`, unchanged).

### Deferred work

* The M1.7b **live Persian A/B/C/D comparison** (model × language) — unchanged and
  still the only path to a recognition-quality statement. The traces now make it
  easier to run: `model`, `transport`, `language`, `mode`, `bytes`, `chars`,
  `elapsed_ms`, `attempts`, `status`, `failure_class` are all on one line.
* Reordering the dedicated transport (inline primary) **if and when** live
  evidence shows the model accepts inline `data` — an explicit, evidence-gated
  follow-up.
* `INVESTIGATION.md` §19.4's optional opt-in transcript-length/attribution
  instrument (still not implemented; it would have to avoid logging content by
  default).
* A shared/pooled HTTP client, only if a live measurement shows the per-leg
  handshake matters (see PART 7 above).

### Explicit next stage — M1.7d: the live evidence pass

1. Deploy, then read the startup line
   `GEMINI_MEDIA_ENGINE_PROVISIONED … stt_model=… stt_language=…` to record which
   route is live.
2. Reply to the same Persian voice note with `.stt` several times and capture, for
   each run, the `GEMINI_MEDIA_ENGINE` line plus its `GEMINI_MEDIA_ENGINE_STAGE`
   lines and the boundary's `stt_engine_invoked` / `stt_engine_returned`.
3. If a failure occurs, the new `failure_class` field identifies the leg, the
   socket phase or the HTTP status immediately — that is the evidence §20.19 R
   asked for, and it decides whether the deadline, the representation order or the
   provider contract needs the next change.
4. Only then run the M1.7b A/B/C/D model × language comparison for the
   recognition-quality question.
