# IMPLEMENTATION REPORT — CURRENT STATE

## Latest phase — Media Processing M1.8: bounded long-audio STT chunking

Repository `Onlyicing1/Telegram-self-bot` · branch `main` · state as of 2026-09-18.

### Phase identity

| Item | Value |
|---|---|
| Phase name | **Media Processing M1.8** — bounded long-audio STT chunking inside the existing media boundary |
| Starting HEAD | `b5e7c10` `feat: drop video/GIF from media processing, deliver one media response` — equal to `origin/main` at phase start |
| Implementation commit | the single phase commit that contains this report (`feat(stt): transcribe over-long audio in bounded chunks`) |
| Live Telegram verification | **NOT PERFORMED** |

### Purpose of this phase

`MAX_STT_DURATION_S` (300 s) was the longest audio the boundary would **accept**, so
a 14-minute recording could not be transcribed at all — it was refused with
`“exceeds the 300s speech-to-text bound”`. It is now the longest audio ONE
recognition may be asked to handle, and a longer recording is divided into ordered,
bounded chunks that the **unchanged** `SttEngine.transcribe(audio: bytes) -> str`
seam transcribes one at a time.

The division lives at the **orchestration boundary**: no provider adapter, no
`ProviderManager`, no new pipeline, no second download path and no second scheduler
was touched, and `transcribe(audio) -> str` is still the only engine contract — a
provider still receives ONE already-bounded payload per call.

Explicitly NOT implemented: automatic provider fallback, a provider health/cooldown
manager, credential pools, key rotation, TTS, Native Vision, video/GIF processing, new
STT or AI providers, quality benchmarking, Supabase tables or SQL, Render ENV
behaviour, any change to `ProviderManager`, any change to Telegram media target
resolution, any AI-based transcript correction, and any LLM-based chunk merge.

### Audit — what the code actually did with over-long audio

Traced before anything was changed, in the project’s source order:

* `media_service.analyze_media` → `_extract_audio_content` →
  `_validate_audio_payload(data, mime_type)`, whose duration guard raised
  `MediaError("The audio is 330s — exceeds the 300s speech-to-text bound.")` with stage
  `media_validation`. A long voice note therefore never reached an engine at all: no
  transfer-past-validation, no engine call, no partial processing.
* The engines behind the seam each already implement their own bounded multi-pass:
  `GeminiMediaEngine.transcribe` (general `generateContent` route, or the dedicated
  transcription route over the `interactions` API, both inside
  `STT_OPERATION_DEADLINE_S` = 45 s), `GroqWhisperEngine.transcribe` and
  `SpeechmaticsEngine.transcribe` — all three reconciling their passes through the
  existing `stt_consensus.reconcile_hypotheses`.
* The engine is chosen once by configuration (`stt_engine_factory.apply_stt_config` →
  `media_service.set_stt_engine`) from the registered candidates
  (`gemini:default`, `gemini:gemini-3.5-transcribe`, `groq:whisper-large-v3`,
  `groq:whisper-large-v3-turbo`, `speechmatics:standard`), and the boundary holds
  exactly ONE engine reference.
* Existing bounds: `MAX_STT_INPUT_BYTES` 20 MiB (pre-transfer), `MAX_STT_DURATION_S`
  300 s, `MAX_STT_CHANNELS` 2, `MAX_STT_SAMPLE_RATE` 48 kHz, `STT_TIMEOUT_S` 60 s,
  `MAX_STT_CHARS` = `MAX_EXTRACTED_CHARS` (the presentation ceiling).
* **No chunking existed anywhere.** `backend/tools/stt_benchmark.py` is the only other
  audio tool in the repository and it is not on the runtime path.
* **Environment facts that decided the mechanism:** `ffmpeg` is NOT present, the venv
  holds none of pydub/soundfile/numpy/av/torch, and `backend/requirements.txt` carries
  no audio dependency. A decoder-based splitter was therefore not available, and no
  heavy media stack was added for this phase.
* The media request’s own envelope is `media_ai_service.DEFAULT_ENVELOPE_S` = 240 s
  (the handler’s backstop), of which the provider call that answers an analytical media
  request reserves `PROVIDER_CALL_SAFETY_TIMEOUT_S` = 120 s. Those two established
  constants are what the new aggregate deadline is derived from.

### How the audio is divided (`backend/services/stt_chunking.py`)

One new module, standard library only (`typing` is its only import), whose entire
input is a validated payload plus two numeric ceilings — no Telegram object, no chat
id, no filename, no caption, no provider, no credential:

| Container | Divided at | Every chunk is |
|---|---|---|
| OGG/Opus, OGG/Vorbis (`audio/ogg`, `audio/opus`, `application/ogg`) | **OGG page boundaries** — the container’s own unit of framing (lacing table, granule position, CRC over itself) | the stream’s own codec header pages followed by a run of complete pages, **concatenated byte-for-byte** |
| RIFF/WAVE (`audio/wav`, `audio/x-wav`, `audio/wave`, `audio/vnd.wave`) | **frame boundaries** of its `data` chunk (plain PCM) | the source’s own pre-`data` chunks with the RIFF and `data` size fields repatched, so its duration is genuinely its own |
| anything else (incl. **FLAC**) | — | refused (`None`) |

* **Nothing is ever split on an arbitrary byte offset**, and no compressed packet is
  ever cut: an OGG chunk boundary is always a page boundary the stream itself declares,
  and a WAVE boundary always lands on a whole frame. Because every OGG page is copied
  verbatim, **no page CRC is invalidated** and no page has to be rebuilt.
* The chunk’s duration is taken from the stream’s **own granule positions** (Opus
  granules are always 48 kHz units; Vorbis granules are samples at the rate its
  identification header declares — the same rule the boundary’s reader uses), so a
  chunk is closed on the page *before* the one that would push it past the ceiling.
  Chunk 1 is therefore `[0 … 300 s]`, chunk 2 `[300 s … 600 s]`, and so on: contiguous,
  non-overlapping, and ordered.
* **Contiguous with no overlap, deliberately.** No overlap is invented “to improve
  quality”, so there is nothing to deduplicate on merge.
* **Fail-closed on everything else**: an unknown MIME, an unclean page walk (trailing
  bytes, a page whose payload runs past the file), a stream that is not Ogg Opus/Vorbis,
  a first page without the BOS flag, a multiplexed/chained stream (mixed serials), more
  chunks than the cap allows, and a **single indivisible unit** (one OGG page that alone
  spans more than the ceiling) all return `None`. The caller then raises the boundary’s
  existing deterministic refusal —
  `“The audio is Ns — exceeds the 300s speech-to-text bound and this container cannot be
  divided into shorter parts of the same format.”` (stage `media_validation`).
* **FLAC is the concrete refusal.** A FLAC frame boundary cannot be found without
  decoding subframes, and a re-headed FLAC would need STREAMINFO’s total-sample count
  and MD5 rewritten; a long FLAC is reported honestly instead of being approximated.
  This is a deliberate, documented limit, not a silent gap: **long FLAC behaves exactly
  as it did before this phase.**

### The operation around the division

`media_service._run_stt_chunked` owns the operation; the division itself runs off the
event loop (`asyncio.to_thread(stt_chunking.plan, …)`), the same pattern the boundary
already uses for document parsing:

1. the payload is validated once against the **total** ceiling, then (only if it
   exceeds one chunk) planned into ordered chunks;
2. chunks are transcribed **strictly in source order, one at a time**, always with the
   ONE engine the request selected;
3. each chunk’s transcript is normalized by the **existing**
   `_normalize_extracted_text` and appended to the ordered list;
4. the list is joined by `stt_chunking.join_transcripts` — **one newline between
   chunks, nothing else**: no bridging text, no inferred words, no translation, no
   summarization, no deduplication and no second model;
5. the single merged transcript then flows through the **unchanged**
   `_normalize_extracted_text` → `_cap_text(limit)` → `MediaAnalysis` path, so one
   request still produces ONE analysis, ONE owner-facing response and no intermediate
   chunk output.

Stages traced (content-free, `key=value`, the project’s existing trace shape):
`stt_chunks_planned`, `stt_chunk_invoked` (with the chunk index, the chunk count, the
byte count and the applied bound), `stt_chunk_returned` (with the character count, so
an EMPTY chunk is distinguishable from a failure), `stt_chunks_merged`, plus the
existing `stt_engine_invoked` / `stt_engine_returned` on the single-pass route.

### Bounds — per chunk, total, count, deadline

| Bound | Value | Enforced |
|---|---|---|
| Duration of ONE chunk | `MAX_STT_DURATION_S` = 300 s (unchanged constant, new meaning) | by the planner and by each chunk’s own granule span |
| Chunk count per request | `MAX_STT_CHUNKS` = 4 (new, explicit) | by the planner (`> max_chunks` ⇒ refusal) |
| Total duration per request | `MAX_STT_TOTAL_DURATION_S` = **`MAX_STT_CHUNKS × MAX_STT_DURATION_S`** = 1200 s (20 min) | by the duration guard, now applied against the TOTAL bound |
| Input bytes | `MAX_STT_INPUT_BYTES` = 20 MiB (**unchanged**) | before the transfer, as before |
| Aggregate recognition deadline | `STT_TOTAL_TIMEOUT_S` = 120 s (new) | one clock started before chunk 1 |
| Per-chunk call timeout | `min(STT_TIMEOUT_S, remaining aggregate)` | passed to `_run_stt` |

* The total bound is **derived, not inflated**: it is exactly the small explicit chunk
  cap times the existing per-chunk ceiling, so “300 s × a huge number” is impossible.
* The aggregate value is derived from two constants that already govern this request:
  the 240 s media envelope minus the 120 s an analytical media answer reserves for its
  provider call = 120 s for recognition — which is also exactly two per-chunk bounds.
* Temporary storage is unchanged: chunks are **in-memory slices of the already
  validated payload**, so there is no chunk file to write, leak or clean, and peak
  memory stays the payload plus ONE chunk (the plan holds byte ranges, not chunks).
* No new environment variable and no new persisted setting was introduced: these are
  architectural ceilings, not per-deployment behaviour.

### Timeouts — the three layers and their relationship

```
media request envelope        240 s   (media_ai_service.DEFAULT_ENVELOPE_S, the handler backstop)
  └── aggregate recognition   120 s   (STT_TOTAL_TIMEOUT_S)               ← new, one clock per request
        └── per chunk          60 s   (STT_TIMEOUT_S, or what is left of the 120 s)
              └── engine op    45 s   (Gemini STT_OPERATION_DEADLINE_S; its own for each other engine)
```

* Each chunk’s call stays bounded exactly as before; a three-pass configuration inside
  one chunk still runs under the engine’s own single operation deadline.
* **N chunks cannot multiply the request lifetime**: the aggregate clock is checked
  before every chunk, and a spent deadline fails the operation honestly
  (stage `media_stt_timeout`) instead of starting another chunk.
* The 120 s left outside the aggregate is what an analytical media answer needs for its
  provider call, so chunking cannot starve the answer step.

### Interaction with multi-pass / consensus, and provider selection

* No second consensus exists and none was added: each chunk goes through the SAME
  `transcribe` seam, so the engine’s configured pass count multiplies the calls **per
  chunk** (chunks × passes), never the other way around. With both caps in force the
  worst case is 4 × 3 = 12 provider calls, each still bounded by the engine’s own
  deadline and by the aggregate.
* The engine receives ONE bounded payload per call and cannot see a chunk boundary, so
  no provider adapter needed a change and none was made.
* **The selected engine stays authoritative**: every chunk uses it, a chunk failure
  fails the whole operation, and no fallback happens here (there is no health/fallback
  manager yet). The seam where a future per-chunk provider choice belongs is the single
  `engine` binding at the top of `_run_stt_chunked`.

### Zero-context and Telegram output

Unchanged and re-tested for the chunked route: the engine receives nothing but the
chunk’s audio bytes and the adapter’s existing configuration (no chat id, message id,
filename, caption, reply, history, memory or inferred target), `MediaAnalysis.
as_context_text()` still renders no Telegram metadata, and the delivery layer is
untouched — **one media request → one logical result → one controlled Telegram
representation** (M1.7). Internal chunking is never user-facing.

### Exact files changed

| File | Change |
|---|---|
| `backend/services/stt_chunking.py` | **new** — the deterministic OGG-page / RIFF-frame division, the chunk plan, and the ordered transcript join (standard library only) |
| `backend/services/media_service.py` | the per-chunk/total/count/deadline constants, `_validate_audio_payload(max_duration_s=…)`, the chunked branch in `_extract_audio_content`, and `_run_stt_chunked` |
| `tests/test_media_stt_chunking.py` | **new** — the focused regression suite |
| `IMPLEMENTATION_REPORT.md` | this section |

No other file was modified. `media_ai_service.py`, `media.py`, the provider adapters,
`ProviderManager`, the delivery layer, the panel/handler code, `requirements.txt`,
`render.yaml` and the Supabase schema are untouched.

### Tests added and changed

`tests/test_media_stt_chunking.py` (34 tests), grouped exactly as the phase’s risks:

* **SHORT** — ≤ 300 s is ONE engine call on the payload’s **own unchanged bytes**
  (`test_short_audio_is_one_call_on_its_own_unchanged_bytes`), and exactly 300 s stays on
  the single-pass route (`test_exactly_one_chunk_worth_of_audio_stays_on_the_single_pass_route`).
* **BOUNDARY** — 301 s starts the chunked route and yields two ordered chunks
  (`test_one_second_past_the_bound_starts_the_chunked_route`).
* **LONG** — 301/480/840/1200 s produce 2/2/3/4 chunks whose transcripts merge in
  strict source order (`test_a_long_recording_merges_its_chunks_in_source_order`);
  the plan’s own durations, the exact partition of the source’s pages
  (`test_the_chunks_partition_the_source_pages_exactly_once`), the page-only division
  (`test_a_long_note_is_divided_at_ogg_page_boundaries_only`), the frame-level WAVE
  division whose chunks are re-validated by `_validate_audio_payload`
  (`test_a_wav_is_divided_on_frame_boundaries_and_stays_valid_per_chunk`), and the
  verbatim byte reuse (`test_the_source_bytes_are_reused_verbatim_not_re_encoded`).
* **FAILURE** — the first, a middle and the last chunk each fail the whole operation
  with no partial transcript and no later attempt (`test_any_failing_chunk_fails_the_whole_operation`,
  parametrized `fail_at=1,2,3`), no retry on another provider
  (`test_a_failing_engine_is_never_retried_on_another_provider`), an indivisible
  over-long container and a long FLAC are refused honestly with no engine call.
* **OUTPUT** — one normalized value (`test_the_merged_transcript_is_one_normalized_value`),
  the existing ceiling’s honest truncation notice
  (`test_the_existing_output_ceiling_stays_honest_for_a_chunked_transcript`), and the
  zero-context rendering on the chunked route
  (`test_the_chunked_route_keeps_the_zero_context_rule`).
* **BOUNDS** — total duration refused before any transcription, the derivation asserted
  directly, the chunk cap enforced by the route (separately from duration), the spent
  aggregate deadline, and the per-chunk timeout formula.
* **RESOURCES** — the division runs off the event loop, no temporary artefact on success
  or failure, and cancellation propagates with cleanup.
* **MULTI-PASS** — a real `GeminiMediaEngine(stt_passes=3)` over a 2-chunk recording
  issues exactly `chunks × passes` = 6 interaction requests, uploads each chunk’s own
  bytes once, deletes each upload, and merges two chunk transcripts
  (`test_a_three_pass_engine_multiplies_per_chunk_and_stays_bounded`).
* **DEPENDENCY** — the splitter’s imports are asserted to be `__future__` + `typing`
  only (`test_the_splitter_is_standard_library_only`).

**No existing test needed to be changed.** Two of them still pin over-long audio being
refused — `tests/test_media_stt.py::test_audio_longer_than_the_duration_bound_is_refused_before_transcription`
and `tests/test_media_gemini_engine.py::test_a_container_longer_than_the_duration_bound_never_reaches_gemini`
(both 330 s) — and they now pass because their **minimal two-page fixture declares its
entire duration on its only audio page**, which is genuinely indivisible. They are
kept as the pinnacle of the refusal contract, and the positive chunking cases live in
the new suite.

### Validation results

| Run | Result |
|---|---|
| `tests/test_media_stt_chunking.py` | **34 passed** |
| Narrow media/STT suites (media processing, image OCR, document extraction, STT, dedicated STT, direct STT, multi-pass, reliability, engine, consensus, Groq, Speechmatics, provider probe, AI STT settings) | **1002 passed, 2 skipped** |
| Full suite, final tree | **3911 passed, 26 skipped** |
| Full suite without the new file (the same tree’s baseline) | **3877 passed, 26 skipped** — i.e. the 34 new tests are exactly the delta, and **no existing test changed** |
| `python -m py_compile` on both changed modules | clean |
| `git diff --check` | clean |

### Live verification status

**NOT PERFORMED.** No live Telegram voice note, no provider credential and no network
call was used; the fixtures are synthetic containers and the engines are scripted, so
nothing here claims the Persian recognition behaviour of any provider — and, as
`INVESTIGATION.md` §19/§20 already state, recognition QUALITY remains a live
measurement (`backend/tools/stt_benchmark.py`) rather than a property this phase can
assert.

### Intentionally NOT implemented

Provider fallback, provider health/cooldown, credential pools, per-chunk provider
selection, streaming/partial delivery, resumable transcription, FLAC division, MP3/
M4A/WebM support, chunk overlap, cross-chunk deduplication, LLM chunk merging,
transcript timing/diarization, TTS, and any change to OCR.

### Known limitations

1. **A chunk’s OGG granule positions are the source’s own absolute values** (and its
   page sequence numbers are the source’s), because rewriting them would mean
   recomputing the OGG page CRC — a variant the standard library does not provide
   (`zlib.crc32` is the reflected ISO-HDLC CRC) and which cannot be verified in this
   repository without a real Ogg fixture. The decoded audio of a chunk is exactly that
   chunk’s own packets; only the container’s *declared* duration reads as its position
   in the source. See the exact next stage.
2. **Codec header pages are the leading pages whose granule position is 0** — how every
   real Ogg Opus/Vorbis encoder writes the identification, comment and setup pages. An
   encoder that packed an audio packet onto a header page would have that one page
   re-emitted with each chunk (bounded, documented, and never a reason to cut a page).
3. **A single OGG page / a FLAC stream that spans more than one chunk cannot be
   divided** and is refused rather than approximated — hence the two existing 330 s
   tests still assert a refusal.
4. **A 3-pass configuration over 4 chunks can exhaust the 120 s aggregate deadline** and
   fail honestly rather than run unbounded. The default is one pass, where the aggregate
   is not the binding constraint for a 20-minute recording.
5. The total bound (20 min) is what four chunks of 300 s can cover; longer recordings
   need a delivery design the project does not have yet.

### Exact next stage

1. **Re-emit OGG chunk pages with normalized granule positions and renumbered sequence
   numbers**, which requires an OGG CRC-32 implementation plus a real Ogg Opus fixture to
   verify it against; that makes each chunk’s *declared* duration its own and removes
   limitation 1.
2. **Live verification** of a real long voice note end-to-end (one transcript, one
   message) — the provider, credential and account work an offline suite cannot do.
3. **FLAC division** only if a supported container truly needs it, and only with a real
   frame parser rather than a guess.

### Delivery

This phase starts from `b5e7c10`, the `main` tip that already equalled `origin/main`
when it began, and is delivered as ONE commit on top of it — no rebase, no force-push,
no new branch, and no unrelated file touched. The delivered commit is the tip of `main`
(`git log -1 --format=%H` re-verifies it).

---

## Previous phase — Media Processing M1.7: Video/GIF out of scope, and ONE controlled media response

Repository `Onlyicing1/Telegram-self-bot` · branch `main` · state as of 2026-09-18.

### Phase identity

| Item | Value |
|---|---|
| Phase name | **Media Processing M1.7** — explicit Video/GIF scope exclusion + single-message media delivery |
| Starting HEAD | `7e1e69a` `docs: record STT quality investigation findings` — equal to `origin/main` at phase start |
| Implementation commit | the single phase commit that contains this report (`feat: drop video/GIF from media processing, deliver one media response`) |
| Live Telegram verification | **NOT PERFORMED** |

### Purpose of this phase

Two focused corrections, both inside the existing Media Processing architecture and
nowhere else:

1. **Video and GIF are explicitly OUT of Media Processing.** The audit (below)
   found that a GIF really did enter the pipeline — it was downloaded and handed
   to the OCR engine — so the exclusion is not hypothetical.
2. **One media request produces ONE controlled Telegram response.** A single
   extracted document could previously be delivered as a burst of Telegram
   messages (one per rendered page). It is now delivered as exactly one message,
   or as ONE attached document when it cannot fit one message — never as a burst
   and never truncated.

Explicitly out of scope for this phase and NOT implemented: STT chunking, TTS,
Native Vision, new providers, credential pools, provider fallback, any change to
OCR/STT recognition quality, and any change to `MediaAnalysis` limits.

### Part 1 — what was found in the existing Video/GIF path

The audit traced every entry point for Video/GIF into the media pipeline:

- **One classifier.** `backend/ai/media.py::classify_message` is the single
  classifier and the only authority for a media type. Video/GIF reach
  `backend/services/media_service.py::analyze_media` labelled `Video`,
  `GIF` (an `image/gif` document) or `Animation` (`DocumentAttributeAnimated`) —
  Telegram's usual animated-image shape.
- **Video was already inert.** `video/mp4` is neither an extractable MIME nor an
  OCR/STT MIME, so a Video fell through to the generic
  `"No local extraction capability for Video (video/mp4)"` UNSUPPORTED result and
  was never transferred.
- **GIF was the real leak.** `"image/gif"` is listed in
  `OCR_IMAGE_MIME_TYPES`, and `provision_gemini_media_engines()` provisions the
  OCR engine on the live runtime, so `ocr_candidate = is_image_mime(mime) and
  ocr_available()` was `True` for a GIF: it was **downloaded and sent to the OCR
  engine**. `Animation` (`video/mp4`) was inert like Video.
- **A second, narrower leak.** A document carrying `DocumentAttributeVideo` whose
  MIME is `image/gif` is labelled `Video` but still matched the OCR capability by
  MIME alone, so it too reached OCR.
- Video/GIF are in `DOWNLOADABLE_MEDIA_TYPES`, so a video/GIF reply still
  resolves as a media target: the request reaches the media boundary rather than
  silently falling through to the LLM.

### How Video/GIF is now explicitly excluded

`backend/services/media_service.py`:

- New single authority: `UNPROCESSABLE_MEDIA_TYPES = frozenset({"Video", "GIF",
  "Animation"})` with the predicate `is_unprocessable(media_type)`.
- `analyze_media` refuses those types **immediately after** the
  `is_downloadable` check and **before every capability check** (OCR, STT, text,
  container extraction, the size gate and the transfer). The result is the
  boundary's existing honest `UNSUPPORTED` outcome via `_unsupported()`.
- The refusal is fully deterministic: it comes from the existing classifier's own
  label (Telegram metadata), never from model inference, and no regex was added.
- The owner-facing result is the existing convention —
  `media_ai_service.unsupported_text()` renders
  `⚠️ I can't process this Video yet.` plus the reason
  `Video is outside the Media Processing scope (video and GIF are not processed).`
  `answer_media_request` returns that answer **before** the provider branch, so
  `ProviderManager.chat` is never consulted for out-of-scope media.
- `DOWNLOADABLE_MEDIA_TYPES` was deliberately left unchanged: keeping Video/GIF
  "downloadable" is what routes a video/GIF reply into the media boundary and
  therefore to the deterministic refusal. Removing them would have made the
  request fall through to the ordinary LLM path, where the model would answer
  about media it cannot see — not a deterministic unsupported result.
- Ordinary media handling was not touched: no Video/GIF subsystem was added, no
  generic Telegram utility was deleted, and image/audio/PDF/DOCX paths are
  unchanged (pinned by tests).

### Part 2 — what caused the multi-message media output

`backend/ai/tools/delivery.py::deliver_response` paginates: `_format_chunks`
splits anything above `SAFE_LIMIT = 4000` UTF-16 units into pages, chunk 1 is
delivered with `event.edit(...)` and **every** subsequent chunk with
`event.reply(...)`.

The media boundary caps extracted text at
`MAX_EXTRACTED_CHARS = DEFAULT_MAX_CONTEXT_TOKENS (4000) × 4 = 16 000`
characters (`MAX_OCR_CHARS` and `MAX_STT_CHARS` are the same ceiling). One media
result could therefore render as up to five pages — one edited message plus up to
four `event.reply` messages generated automatically — which is the live
"six or more consecutive Telegram messages for one request" symptom.

Internal extraction chunking was **not** the cause. `_TextAccumulator` already
recombines PDF pages and DOCX blocks into ONE `MediaAnalysis.content`; the burst
was produced by the delivery layer paginating one logical result.

### The delivery change

`backend/ai/tools/delivery.py` — new `deliver_single_message()`, used only for
media answers:

1. The rendering is identical to the normal path (`process_output` →
   `format_presentation` → the durable provenance marker), so a result that fits
   one message is byte-for-byte the message the paginating path produced.
2. If `_format_chunks(...)` yields exactly ONE chunk, that chunk is edited into
   the request message exactly once (with the same reply fallback).
3. Otherwise the **COMPLETE** normalized result is sent as ONE attached
   `media-extract.txt` document, and the request message is edited into one
   deterministic notice: the character count, the attachment name, and an
   explicit "nothing was truncated or split". No page-by-page burst is emitted.
4. If the attachment itself cannot be delivered (no client / no resolvable peer),
   the existing paginating `deliver_response` is used as a last resort and logged —
   extracted content is never silently dropped.
5. Secondary notes (backup-model note, optional telemetry line) are normalized by
   the same renderer and ride with the delivered message, or with the notice in
   attachment mode, so neither mode swallows them.

`backend/bot/handlers/ai_unified.py` — `_is_media_result(result)` reads the
**existing** dispatcher stamp `result.metadata["ai_action"]["action"] ==
"media_analysis"` and routes only media answers to `deliver_single_message`
(passing the live client). Every other response — provider answers, tool results,
confirmations, failures, silent deletes — keeps `deliver_response` and its
pagination unchanged.

### Internal chunks vs. user-facing delivery

| Layer | Responsibility |
|---|---|
| `media_service` extractors | produce ONE normalized `MediaAnalysis.content` (bounded, `truncated` flag) |
| `media_ai_service` | turns that analysis into ONE `MediaAnswer` (direct STT or provider answer) |
| dispatcher | ONE `EngineResult` stamped `ai_action.action == "media_analysis"` |
| `deliver_single_message` | decides the ONE Telegram representation: one message, or one attachment + one notice |

No extractor, `MediaAnalysis` limit, OCR/STT provider call or zero-context rule
was changed to achieve this.

### Limits and edge cases

- The attachment name is the fixed `media-extract.txt`; the peer is the event's
  own `input_chat` when the event exposes one, else `event.chat_id`.
- The notice reports the **normalized character count**; the size test uses the
  same UTF-16 accounting as `SAFE_LIMIT`.
- The only case in which more than one Telegram message can still result is the
  attachment being undeliverable (logged, paginated fallback).
- No arbitrary message-count cap was introduced — the bound is representational
  (one message, or one attachment), not a count threshold.
- Media failures, the direct-STT path, the provider path, `ProviderManager`, and
  every `MediaAnalysis`/OCR/STT limit are unchanged.

### Tests added and changed

- **New:** `tests/test_media_scope_and_delivery.py` — 25 focused tests:
  the scope set is exactly `{Video, GIF, Animation}`; Video, both GIF shapes
  (`GIF` and `Animation`) and a `DocumentAttributeVideo` declaring `image/gif`
  are refused, never transferred and never reach a provisioned OCR/STT engine;
  the owner gets the deterministic refusal and the provider is never consulted;
  the dispatcher stamp routes media answers to the single-message path; photos
  still reach OCR, Voice still reaches STT, text/DOCX still extract and the
  PDF/downloadable taxonomy is unchanged; a short media result is one message, a
  many-piece internal extraction is ONE message, a large result is ONE attachment
  with the complete content and a one-line notice, an undeliverable attachment
  still delivers everything, an empty result stays a deterministic failure, and
  normal (non-media) delivery still paginates exactly as before.
- **Changed:** `tests/test_media_image_ocr.py` and
  `tests/test_media_gemini_engine.py` — the `image/gif` rows are now
  Sticker-labelled so the GIF **format** keeps its OCR-boundary coverage
  (signature corroboration, decoding, and the engine's undocumented-container
  guard) while the GIF **media type** is refused by the scope gate. The refusal
  itself is pinned in the new suite.

### Validation results

```
tests/test_media_scope_and_delivery.py ................ 25 passed
media suites (scope_and_delivery, processing, image_ocr, stt, stt_language,
              document_extraction, ai_integration, direct_stt, gemini_engine,
              transcribe_engine) ....................... 449 passed
full suite: pytest tests/ .......... 3328 passed, 24 skipped
(baseline before this phase: 3303 passed, 24 skipped → +25 tests, no test lost)
py_compile media_service / delivery / ai_unified ........ OK
git diff --check ........................................ clean
```

### Live verification status

**NOT PERFORMED.** Nothing here is a live observation: no Telegram request was
sent against this change, and the multi-message symptom is reproduced from the
code path (`MAX_EXTRACTED_CHARS` vs `SAFE_LIMIT` pagination), not measured live.

### Intentionally deferred

1. **Live verification** — reply to a video/GIF and confirm the deterministic
   refusal; send a large PDF/DOCX and confirm one attachment plus one notice.
2. **Video/GIF capability** — if it is ever wanted, `UNPROCESSABLE_MEDIA_TYPES` is
   the single place to change it; no other code knows about the scope.
3. **Reproducing the exact live six-message count** — the bound is derived from
   the code (`16 000` characters ÷ `SAFE_LIMIT`) rather than from a captured
   production transcript.

### Delivery

This phase was implemented on `7e1e69a`, but `origin/main` advanced by twelve
Speech-to-Text commits (`da05ace` … `c3d3e5e`) before the phase could be pushed.
The phase was therefore re-applied on top of `c3d3e5e` as ONE commit — no rebase,
no force-push, no new branch. The only conflict was this report, where both sides
had gained a "Latest phase": the STT phase keeps its section intact as
**Previous phase — M2.2.1**, and not a sentence of it was rewritten.

The media change is untouched by the re-application: over the six code/test files
this phase touches, `git diff c3d3e5e..HEAD` is byte-identical to
`git diff 7e1e69a..5264fea`. The delivered commit is the tip of `main`
(`git log -1 --format=%H` re-verifies it).

---

## Previous phase — M2.2.1: Speech-to-Text panel cleanup (compact control panel + nested STT Settings)

Repository `Onlyicing1/Telegram-self-bot` · branch `main` · state as of 2026-09-17.

### Current stage

M2.2.1 — a **presentation-only cleanup of AI → Media Analysis → Speech-to-Text**,
done before live testing. The screen is now a compact control panel instead of a
page of documentation: the verbose explanatory paragraphs are gone, the candidate
selection is a deterministic **two-column grid** built from the registry, and the
two bounded behavioral controls (language, recognition passes) moved one level
down into a new nested **⚙ STT Settings** panel so they no longer occupy the main
screen. **Nothing about behavior, persistence, callbacks, adapters, the probe,
the media boundary or the fallback status changed** — every action id, input id
and stored key is byte-identical to M2.2.

```
AI                                    (ai)
├── Media Analysis                    (ai_media)
│   ├── Text recognition (OCR)        (ai_media_ocr)   — unchanged (no OCR change)
│   └── Speech-to-Text                (ai_media_stt)
│       ├── Test all providers             → action:ai_stt_test_all          ← ONE global action (unchanged payload)
│       ├── two-column candidate grid      → action:ai_stt_select_candidate:<candidate-id>
│       └── ⚙ STT Settings                 → panel:ai_media_stt_settings     ← NEW navigation row
│           └── ⚙ STT Settings            (ai_media_stt_settings)            ← NEW nested panel
│               ├── Language…              → input:ai_media_stt:stt_language
│               ├── Recognition passes…    → input:ai_media_stt:stt_passes
│               └── ← Back / ⌂ Home        → panel:_nav:back / panel:_nav:home
├── Settings                          (ai_settings)      — still no STT controls
│   └── Advanced                      (ai_settings_adv)  — still no STT controls
└── … (provider, model, usage, health, details, diagnostics: unchanged)
```

| Item | Value |
|---|---|
| **Phase** | M2.2.1 — Speech-to-Text panel UI cleanup (compact panel + nested STT Settings) |
| **Starting HEAD** | `56762a9a6a27c49f814fa4e3d39efc034b026a29` — `feat(stt): make Speechmatics executable and add one global provider test` (== `origin/main`) |
| **Implementation commit** | the single commit of this phase (`git log -1 --format=%H` re-verifies it; recorded in the hand-off response) |
| **Database migration required** | **NO** — no schema change, no column, no table |
| **New environment variables** | **NONE** |
| **New dependencies** | **NONE** (`requirements.txt` untouched) |
| **Files changed** | **four** — `backend/bot/handlers/ai_stt_settings.py`, `tests/test_ai_stt_settings.py`, `tests/test_stt_provider_probe.py`, this report |
| **Behavioral changes** | **NONE** — presentation/navigation only; every callback and input payload is unchanged |
| **Fallback / health manager** | **NOT implemented** (next phase, unchanged) |
| **Live Telegram verification** | **NOT PERFORMED** (no session or traffic in this environment) |
| **Live provider verification** | **NOT PERFORMED** — neither `AI_GROQ_API_KEY` nor `AI_SPEECHMATICS_API_KEY` exists in this implementation environment; no provider is claimed healthy |
| **Recognition-quality claim** | **NONE** |

### Commit lineage

| Commit | Role |
|---|---|
| `01ff211` `feat(stt): add the bounded multi-pass STT accuracy seam` | M1.7e — the opt-in STT-only consensus seam |
| (M1.8 commit) | M1.8 — the owner-managed Gemini STT settings (`stt_model` / `stt_language` / `stt_passes`) and their runtime application |
| `4a1f9a2` `added web investigation report file` | the web research report |
| `8d3ba14` `feat(stt): establish the Speech-to-Text control plane and the Media Analysis surface` | M2.0 — the control plane |
| `9d63766` `feat(stt): add the Groq Whisper STT adapter, … and the provider test` | M2.1 — the Groq adapter, the resolver, the provider probe |
| `56762a9` `feat(stt): make Speechmatics executable and add one global provider test` | M2.2 — the Speechmatics adapter, the ONE global provider test, the secret declaration (**starting HEAD of this phase**) |
| the commit of this phase | M2.2.1 — the compact Speech-to-Text panel, the two-column candidate grid, the nested STT Settings panel and this report |

### Files changed by this phase

| File | Change |
|---|---|
| `backend/bot/handlers/ai_stt_settings.py` | the Speech-to-Text screen became a compact control panel; the candidate buttons are laid out in two columns; language/passes moved into the new `ai_media_stt_settings` panel; the long explanatory paragraphs were replaced by one short hint line |
| `tests/test_ai_stt_settings.py` | the new UI contract: one global test action, two-column grid + registry order, no settings control on the main screen, the nested panel and its values, unchanged payloads, secret/metadata hygiene, the new panel registration and Back navigation |
| `tests/test_stt_provider_probe.py` | updated to the shortened labels (via the handler's own button builder) and the shortened panel hint; the result notice still states it does not measure quality |
| `IMPLEMENTATION_REPORT.md` | this report |

**Untouched (deliberately):** every provider adapter (`groq_stt_engine.py`,
`speechmatics_stt_engine.py`, `gemini_media_engine.py`), `stt_engine_factory.py`,
`stt_control_plane.py`, `stt_provider_probe.py`, `media_service.py`,
`stt_consensus.py`, `config_store.py`, `backend/bot/handlers/ai.py`,
`backend/helper/**` (the shared panel/input/action registry and its navigation
stack are reused as-is), the provider/manager/dispatcher/tool layer, OCR,
Save / Tasks / scheduler / `RuntimeSupervisor`, `render.yaml`, `requirements.txt`,
`supabase/migrations/*.sql`, `DATABASE_ARCHITECTURE.md` and all secrets.

### The Speech-to-Text screen after this phase

| Aspect | Behavior |
|---|---|
| **State block** | `Active · <candidate>` / `Language · <language or Auto>` / `Passes · <N>`, then a numbered `Providers` list where each entry carries its own probe state and the active candidate is marked `· active`. Four lines of state, no prose |
| **One short hint** | a single italic line: `Test all = synthetic capability probe, not a quality benchmark.` — the full warning is repeated in the test result notice, so the short panel never implies a quality measurement. On the whole main screen there is at most ONE italic line and no line exceeds 80 characters (`test_the_stt_panel_is_compact` fails otherwise) |
| **Candidate grid** | deterministic two-column rows built from the candidate list in canonical registry order — a changed registry re-flows the rows with no hard-coded wrapping and never leaves a hole (only the final row may be short). Buttons are presentation-shortened (`Use Gemini` / `Use Gemini Transcribe` / `Use Groq v3` / `Use Groq Turbo` / `Use Speechmatics`) through a display-only label map whose **fallback is the registry label**, so a newly registered candidate is still offered without touching the table. The callback payload is the registered candidate id and is **never derived from the button text** |
| **Selection semantics** | unchanged — the active candidate gets no `Use` button, an unimplemented candidate gets none either, and a legacy/unresolved selection offers the whole implemented pool |
| **Global test** | exactly ONE `Test all providers` row, above the candidate rows, payload `action:ai_stt_test_all`, delegating to the existing `stt_provider_probe.test_candidates()` (no second loop in the handler), re-rendering the same panel **once** with one bounded line per candidate |
| **Nested settings** | `⚙ STT Settings` (`panel:ai_media_stt_settings`, parent `ai_media_stt`) holds `Language…` and `Recognition passes…` — the **same** registered inputs (`input:ai_media_stt:stt_language`, `input:ai_media_stt:stt_passes`), the same prompts, validation, storage keys and runtime application. It shows the current values compactly and reaches them through the shared navigation stack, so Back returns to Speech-to-Text and Home to the usual destination — no second navigation system |
| **Real states preserved** | a failed database read, an unresolved legacy model and a registered-but-unavailable active candidate are all still rendered (they are runtime facts, not documentation); no error was hidden for visual cleanliness |
| **No secrets / no metadata** | no label, body line or button names an environment variable, a credential, a transcript or a Telegram identifier (asserted by test) |

### Carried forward: the STT control plane as it stands (M2.0 → M2.2, unchanged by this phase)

The sections below describe the **current** operating architecture. This phase
changed none of it; they are kept here because they are the live contract behind
the panels above.

#### Global "Test all providers" behavior

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

#### Real replied-to audio for the probe — investigated, deliberately NOT added

The earlier task asked whether the existing architecture can safely feed a **real**
replied-to Voice/Audio message into the same bounded probe. It cannot, and no
mechanism was invented to force it:

* the global test is a **callback-query** action (`ActionHandler(event, extra,
  chat_id)`), and a button press carries no replied-to message — there is no
  deterministic media target in that scope at all;
* the only place a reply is available is the **pending-input** path, whose
  handlers receive the owner's *typed text*, not the replied-to media object;
  deriving audio from it would mean adding a new Telegram-context mechanism —
  exactly what that phase was told not to do.

So the global provider test remains a **bounded capability/transport probe**, it
says so on the panel and in its result, and **real Persian recognition
benchmarking remains a separate next step** (see Deferred work). The probe API
already accepts explicit `audio=` bytes, so that step can be built without
touching the adapters.

#### Speechmatics API integration summary

| Aspect | Decision |
|---|---|
| **API** | the official **batch v2 REST API** (`https://asr.api.speechmatics.com/v2`), verified against the published API reference and the vendor's own client source. Speechmatics transcribes **asynchronously**, so one recognition is a bounded job cycle: `POST /v2/jobs` → `GET /v2/jobs/{id}` until `done` → `GET /v2/jobs/{id}/transcript?format=txt`. No WebSocket, no management platform, no temporary-token exchange |
| **Authorization** | `Authorization: Bearer <API key>` on every leg. Never logged, never persisted, never placed in Telegram/Supabase; a provider error body is redacted before it can surface |
| **Request** | `multipart/form-data`: the `config` part is the documented JSON (`{"type":"transcription","transcription_config":{"language":…,"operating_point":…}}`) and the audio is the `data_file` part under a **static, non-identifying** name (`audio.ogg` / `audio.wav` / `audio.flac`), so an untrusted Telegram filename can never leak. No diarization and no extra output are requested |
| **Model mapping** | the registered candidate model IS the API's `operating_point` (`standard`), through an explicit table so an unregistered value can never reach the API |
| **Credential** | `AI_SPEECHMATICS_API_KEY` — the ONE declared secret. No compatibility alias was invented: the repository has no existing Speechmatics credential convention, so a second variable would have been a fabricated convention |
| **Audio input contract** | unchanged: the adapter receives the already-bounded, already-validated payload bytes from `media_service` (resolution, transfer, size/duration/channel/rate/MIME validation, cleanup and normalization all stay there). OGG/Opus, WAV and FLAC are accepted; anything else is refused locally before any request |
| **Language** | an explicit BCP-47 tag is reduced to the ISO-639-1 primary subtag the API documents (`fa-IR` → `fa`); empty means **automatic detection**, which this API spells with its own `language: "auto"` token — never a fabricated code. Nothing is translated, transliterated or forced to English; a Persian transcript returns in Persian script |
| **Recognition passes** | the owner's bounded count (`1..3`) reuses the **existing** STT-only consensus (`stt_consensus.reconcile_hypotheses`): sequential passes over the same audio, each one full job cycle, under ONE deadline. A pass is a recognition attempt, never a transport retry |
| **Bounds** | one engine operation deadline of **45 s**, inside the boundary's own `STT_TIMEOUT_S` (60 s) so the engine's precise reason always wins; connect/write/pool bounds derived from what is **left** of the deadline and re-derived for every leg; a bounded status poll; at most **2** sequential attempts and only for transient conditions |
| **Output ceiling** | `media_service.MAX_STT_CHARS` is applied in the adapter. Empty or unreadable provider output is a **failure**, never a successful empty transcription |

#### Failure classification

One closed token per failure SITE, attached to the raised `MediaError` and emitted
as the `failure_class` field of the adapter's own bounded trace line (with the
socket phase or HTTP status when one applies). The vocabulary is shared by both
adapters — `missing_credential`, `unsupported_model`, `auth`, `forbidden`,
`invalid_request`, `unsupported_audio`, `timeout`, `transport`, `rate_limit`,
`server`, `malformed_response`, `empty_transcription`, `provider_rejection`,
`operation_deadline` — so the provider probe reports the same tokens whichever
adapter failed.

Deterministic failures (a rejected credential or key, a refused payload or config,
an unreadable body, an empty transcript, a rejected/failed job) are **never**
re-sent. Transient ones (`timeout`, `transport`, `429`, `>= 500`) may repeat the
**submit** leg at most once while the deadline has room; a transient failure while
**waiting** for an already-submitted job only repeats the status read (at most
three consecutive times) and then fails — it never submits a second job, so a retry
can never silently double the translation work. Recognition quality is never
reclassified: a poor transcript is a successful provider response.

#### Current provider candidate states

| Candidate id | Provider | Execution | Credential | Test state |
|---|---|---|---|---|
| `gemini:default` | gemini | implemented (unchanged) | `AI_GEMINI_API_KEY` | not tested until probed |
| `gemini:gemini-3.5-transcribe` | gemini | implemented (unchanged) | `AI_GEMINI_API_KEY` | not tested until probed |
| `groq:whisper-large-v3` | groq | implemented (M2.1) | `AI_GROQ_API_KEY` (→ `GROQ_API_KEY`) | not tested until probed |
| `groq:whisper-large-v3-turbo` | groq | implemented (M2.1) | `AI_GROQ_API_KEY` (→ `GROQ_API_KEY`) | not tested until probed |
| `speechmatics:standard` | speechmatics | implemented (M2.2) | `AI_SPEECHMATICS_API_KEY` | not tested until probed |

Every registered candidate is selectable and testable. **An adapter existing is
not a health claim**: only a completed request that returned a non-empty
transcript is reported as passed, and none has been run here.

#### Resulting architecture

```
Telegram UI (AI → Media Analysis → Speech-to-Text → ⚙ STT Settings)
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

#### Configuration / ENV behavior

* ENV holds **secrets only**. `AI_SPEECHMATICS_API_KEY` is declared in `render.yaml`
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
| `tests/test_ai_stt_settings.py` (extended) | **`100 passed`** |
| `tests/test_stt_provider_probe.py` (updated) | **`57 passed, 2 skipped`** (the skips are the two opt-in live probes — no credential here) |
| `tests/test_groq_stt_engine.py` (unchanged, re-run) | **`90 passed`** |
| `tests/test_speechmatics_stt_engine.py` (unchanged, re-run) | **`92 passed`** |
| `tests/test_36_ai_settings_ux.py` (unchanged, re-run) | **`9 passed`** |
| the media / STT boundary suites (`test_media_stt.py`, `test_media_dedicated_stt.py`, `test_media_stt_reliability.py`, `test_media_stt_multipass.py`, `test_media_stt_language.py`, `test_media_stt_benchmark.py`, `test_stt_consensus.py`, `test_media_gemini_engine.py`, `test_media_ai_integration.py`, `test_media_direct_stt.py`, `test_media_processing.py`) | **`559 passed`** — the boundary's behavior is unchanged |
| **Full suite** | **`3852 passed, 26 skipped, 3 warnings` in 112.56 s** (`24` of the skips are pre-existing; the other `2` are the opt-in live provider probes. No test was deleted or weakened) |

New/updated coverage added by this phase: exactly one global test action and no
per-candidate Test button; the two-column candidate grid with the registry's
canonical order preserved and no hole except the last row; the active candidate
still absent from the grid and unimplemented candidates still excluded; the
shortened labels (the full candidate set is still offered); **no** settings
control on the main screen while the values stay visible; the `⚙ STT Settings`
navigation row; the nested panel's own title, current values, the two existing
input payloads and Back/Home rows; every callback and input payload unchanged;
Back from STT Settings landing back on Speech-to-Text through the shared
navigation stack; the new panel registered with parent `ai_media_stt`; the main
body staying under 500 characters with ≤ 80-character lines and at most one
italic line; the unavailable-active-candidate warning still rendering on the
compact screen (through a forged control plane, since every registered
capability currently executes); and no credential, transcript, Telegram
identifier or environment variable name appearing anywhere in either panel's
text or buttons.

**Syntax / whitespace:** `python -m py_compile` clean on every changed Python
file; `git diff --check` clean.

### Live verification status

* **Telegram:** NOT performed — the panels, grid, nested settings panel and
  navigation were exercised against the real handlers, the real registries and
  the real panel builders, but no Telegram session rendered them.
* **Groq API / Speechmatics API:** NOT performed — no credential in this
  environment, so no request was made and no reachability is claimed. The opt-in
  live test (`tests/test_stt_provider_probe.py`, skipped without a credential) is
  the safe path for an operator who has one: it uses the bounded probe payload,
  reports only state/model/elapsed/failure class, never prints or persists the
  key, and never treats a synthetic tone as proof of recognition quality.

No provider is claimed healthy merely because an adapter exists, and no
recognition-quality improvement is claimed because a provider answered.

### Intentionally NOT implemented

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
  Telegram-context mechanism was invented to force it into this line of work.
* **Any behavioral change from this phase** — no adapter, resolver, control plane,
  probe, media boundary, persistence or callback was modified; this phase is
  presentation and navigation only.
* **Any change to the dispatcher, the tool layer, `ProviderManager`, the
  scheduler, `RuntimeSupervisor` recovery, the Supabase schema,
  `DATABASE_ARCHITECTURE.md`, `render.yaml`, `requirements.txt`, OCR or the media
  boundary's execution behavior.**
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
6. The candidate button labels are shortened for the two-column grid; they are
   presentation only and the registry remains the single source of candidate
   identity. A label map that does not know a newly registered candidate falls
   back to its registry label, so no candidate can become unselectable because of
   a label.
7. The M1.8/M2.0 note stands: the `ai_config` STT columns still require the
   pending manual migration; until it is applied the settings degrade to the
   documented in-memory fallback, and `DATABASE_ARCHITECTURE.md` §7 still
   describes the superseded M1.8 semantics.
8. Recognition quality (class A) remains unmeasured and is not claimed.

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

### Exact next stage — live Telegram verification, then M2.3: real Persian recognition benchmarking

1. Open **AI → Media Analysis → Speech-to-Text** on the live account and confirm
   the compact panel, the two-column grid, the nested `⚙ STT Settings` panel and
   its `Language…` / `Recognition passes…` inputs render and persist as expected —
   this UI cleanup exists to make that walkthrough readable on a phone.
2. Run `Test all providers` with the deployment credentials in place to learn each
   candidate's transport/credential state (still not a quality measurement).
3. Feed **real** Persian voice notes through the SAME bounded probe
   (`stt_provider_probe.test_candidate(..., audio=<bounded bytes>)` already
   accepts explicit audio) and record per-candidate transcripts and timings
   **outside** the repository's deterministic suite.
4. Decide, on that evidence, which candidates are worth keeping active and in
   which order — the decision the control plane already models but deliberately
   does not act on.
5. Only after that, the ordered active → cooldown → fallback execution in front
   of the existing `media_service.set_stt_engine` seam (never a second STT
   pipeline), consuming the resolver and failure taxonomy already in place.

### Document version

This document reflects the M2.2.1 state: Speech-to-Text lives under
**AI → Media Analysis** as a compact control panel — one global `Test all
providers` action, a deterministic two-column grid of REGISTERED candidates, and
a nested **⚙ STT Settings** panel holding only the bounded language and
recognition-pass controls. All five registered candidates (Gemini ×2, Groq
Whisper ×2, Speechmatics ×1) remain executable and testable through the ONE
bounded probe that never claims health or recognition quality; the candidate →
engine resolution is one small seam in front of the **unchanged**
`media_service` STT boundary; and **automatic fallback and real Persian quality
benchmarking are still deferred.** If code changes invalidate any section, update
this document in the same commit.
