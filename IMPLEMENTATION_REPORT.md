# IMPLEMENTATION REPORT — CURRENT STATE

## Latest phase — Media Processing M1.6: direct STT — the transcript itself, without a second model

Repository `Onlyicing1/Telegram-self-bot` · branch `main` · implementation date 2026-09-16.

| Item | Value |
|---|---|
| Starting HEAD | `526011a5c4bead520e4e59770ac66da1e86555e1` (`origin/main` — the STT value-lineage investigation record) |
| Implementation commit | recorded after push (see the git delivery section) |
| Files changed | `backend/services/media_ai_service.py` (+~90 — the classifier, the direct answer, one branch) · `tests/test_media_direct_stt.py` (**new**, 35 tests) · `IMPLEMENTATION_REPORT.md` (this rewrite) |
| Media boundary | **unchanged** — `backend/services/media_service.py` was not modified: deterministic resolution, the bounded transfer, validation, `_normalize_extracted_text`, `_cap_text`, the `SttEngine` seam, every timeout and cleanup path are identical, and the direct request runs through the very same `analyze_media()` call |
| Provider architecture | **unchanged** — `ProviderManager`, every adapter, the fallback mesh and the registry were not modified. `ProviderManager.chat` is *not called at all* for a deterministic direct-STT request; it is called exactly as before for every analytical media request |
| STT engine / instruction | **unchanged** — the M1.5c language/script contract (`STT_INSTRUCTION`) is byte-identical; no new engine, no new dependency, no new SDK |
| Dispatcher / delivery | **unchanged** — `_try_media_analysis`, `_build_fast_path_result`, `delivery.process_output`, `format_presentation`, `apply_presentation_provenance` and `deliver_response` were not modified. The direct answer flows through the existing fast-path result and the existing deterministic delivery layer |
| OCR / PDF / DOCX | **unchanged** |
| Database / ENV / deployment | **none** — no schema, migration, config, `render.yaml`, `Procfile` or dependency change; `DATABASE_ARCHITECTURE.md` untouched |
| Live Telegram verification | **NOT PERFORMED** — requires one live reply-to-Voice `این رو stt کن` request on the deployed runtime |

### Purpose of this phase

The M1.5 source audit and the value-lineage investigation proved that for an
explicit transcription request the owner-visible text is produced by the
**second** LLM: `media_ai_service` sent the transcript to `ProviderManager.chat`
under a system prompt that asks the model to *answer* — with no
preserve/verbatim clause — so the transcript could be restated, re-wrapped or
garbled on its way to Telegram. The observed live symptom: a 51-character
engine transcript became an 80-character Persian-wrapped model answer.

This phase removes the second model from the explicit-transcription case: the
request is classified deterministically, and the extracted transcript is
delivered **as the answer**.

### Direct-STT behavior

Path for a high-confidence explicit transcription request on Voice/Audio media:

```
explicit STT request ("این رو stt کن")
→ dispatcher._media_target / _try_media_analysis   (UNCHANGED)
→ media_ai_service.answer_media_request            (UNCHANGED signature)
→ media_service.resolve_media_message              (UNCHANGED)
→ media_service.analyze_media                      (UNCHANGED — bounded transfer, validation,
                                                    STT engine, _normalize_extracted_text, _cap_text)
→ MediaAnalysis.content == the answer              (NEW — no provider round)
→ dispatcher._build_fast_path_result               (UNCHANGED)
→ delivery.process_output → deliver_response       (UNCHANGED)
```

- The answer text is `MediaAnalysis.content` — the engine's transcript after
  the existing whitespace normalization and the 16 000-character cap. No
  wrapper, no preamble, no quotation marks, no commentary is added by the
  media boundary.
- `ProviderManager.chat` is never called; no second model, no prompt build, no
  tool schema, no fallback mesh. `direct_stt_completed chars=…` is traced
  instead of `provider_call_started/completed`.
- Honest provenance: `MediaAnswer(provider="local",
  model="gemini-stt-deterministic")` — the STT engine that ran is named, and
  no chat provider is claimed. `fallback_used` stays `False`.
- The transcript is **not logged**: the trace records only bounded character
  counts (`chars=`), as before.
- Empty extraction keeps the honest "no readable content" answer; engine,
  download, validation and resolution failures keep the exact existing
  media-failure contract (staged `MediaError` → `❌ Media processing failed: …`).

### Detection behavior

`media_ai_service.is_direct_stt_request()` — a pure, deterministic function at
the narrowest media boundary (`media_ai_service`, which owns the request→answer
decision):

- **Positive (high-confidence transcription asks):** Latin
  `transcribe`, `transcript`, `stt`, `speech to text` (word-bounded, so
  "transcript" cannot match inside another word); Persian
  `ترانشریپ` / `ترانشریپت` (loanword), `ترانشرینگ`, `پیاداداری` (formal
  transcription — honored only for request-sized texts, ≤ 120 characters, so a
  long message *quoting* a transcription is not hijacked), the Persian
  spelling-out of the acronym `سی‌تی‌ی`, and the colloquial
  `متن‌بخون` / `متن‌بکن` ("write the text out").
- **Negative (analytical / conversational — keeps the LLM path):**
  `این ویس درباره چیه؟`, `این صدا چی میگه؟`, `خلاصه این ویس رو بگو`,
  `این فایل صوتی رو تحلیل کن`, `متنش رو بنویس` (plain "write its text" without
  the compound), `summarize this voice`, `what does the voice say`,
  `save this voice note`, greetings, empty/None.
- The media-type gate is applied at the single branch point in
  `answer_media_request`: only `Voice` and `Audio` analyses qualify. A
  "transcribe this" ask over a Document/Image still goes to the model.
- Deliberately NOT matched: `read`, `listen`, `understand`, `say`, `analyze`,
  `translate`, `summarize`, and every Persian analytical verb — broad semantic
  guessing is exactly what this phase avoids.

### Analytical-media behavior

**Unchanged.** Any request that is not an explicit transcription ask — or any
direct-STT wording over non-audio media — continues:
`STT/OCR/PDF/DOCX → MediaAnalysis → build_media_messages (two-message input) →
ProviderManager.chat(messages, tools=[]) → MediaAnswer → delivery`, with the
same zero-Telegram-context guarantee and the same budget/timeout bounds.

### ProviderManager bypass scope

`ProviderManager.chat` is bypassed **only** when
`is_direct_stt_request(request_text)` is `True` **and**
`analysis.media_type ∈ {Voice, Audio}` **and** the analysis has extracted
content — all three conditions, evaluated at one branch in
`answer_media_request`. Every other media request, and every failure, keeps the
existing provider path or the existing failure contract.

### Tests and exact results

New file `tests/test_media_direct_stt.py` (35 tests) proves the architectural
boundary, not any fixed transcript value:

| Group | Proof |
|---|---|
| A. Classification (17) | the Persian and English explicit STT phrasings above classify direct; the analytical/conversational list classifies not-direct; `None`/empty is not direct |
| B. Direct execution (7) | over a real `Dispatcher` + real `ProviderManager` with a scripted provider: `result.response == TRANSCRIPT` exactly; `provider.prompts == []`; `build_media_messages` raises if ever called; `client.ops == ["get_messages", "download_media"]`; provenance `local` / `gemini-stt-deterministic`; `direct_stt_completed` traced with no `provider_call_*`; no log record contains the transcript |
| B'. Normalization/cap (2) | a > `MAX_STT_CHARS` transcript arrives truncated with `…` at ≤ limit, whitespace-collapsed; multi-line input keeps its single blank line |
| C. Analytical regression (3) | `این ویس درباره چیه؟` / `این صدا چی میگه؟` / `خلاصه …` all reach the provider with the exact two-message shape (`system` = `MEDIA_ANALYSIS_SYSTEM_PROMPT`, `user` = request + `Content:` transcript); the same engine serves both paths over the same audio |
| D. Failure (3) | exploding engine → `media_failure_stage == media_stt_engine`, no provider call; empty extraction → honest no-content answer, no fabrication; `analyze_media` raising → media failure identity preserved |
| E. Gate (1) | the classifier itself stays media-agnostic; the audio-only gate is the branch point |

Exact runs (Python 3.10 venv, pytest 9.1.1):

```
tests/test_media_direct_stt.py ................................  [100%] 35 passed
media suites (direct_stt, ai_integration, stt, stt_language, gemini_engine,
              processing, document_extraction, image_ocr) ....... 386 passed
full suite: pytest tests/ ......................... 3263 passed, 24 skipped
py_compile backend/services/media_ai_service.py ................. OK
git diff --check .............................................. clean
```

### Known limitations

1. **Not live-verified.** The classifier and bypass are proven by tests only;
   one live Telegram `این رو stt کن` reply-to-Voice request must confirm the
   end-to-end behavior on the deployed runtime.
2. **The engine's own transcript quality is unchanged.** If Gemini returns a
   garbled transcript, the owner now sees that garbled transcript directly —
   by design (fidelity), with the M1.5c language/script contract as the
   engine-side mitigation.
3. **Persian detection is a closed token list.** Unlisted phrasings (e.g. new
   slang for transcription) fall through to the analytical path — safe, never
   wrong; the list can grow with evidence.
4. **`پیاداداری` length guard is heuristic** (≤ 120 characters) and could
   mis-gate an unusually long legitimate request; it exists to prevent
   hijacking long messages that merely mention the word.
5. Latin matching is word-bounded lowercase English only; a request fully in
   another language falls to the analytical path.

### Exact next stage

**M1.6b — one live verification run**: deploy, reply to a Persian Voice note
with `این رو stt کن`, and confirm the delivered text equals the engine's
`stt_engine_returned chars=N` content (compare `media_completed` and the
`AI_OUTPUT_NORMALIZED` line for the same request id). Record the observation in
`INVESTIGATION.md` §2.1.J — this closes the value-lineage gap the
investigation left open. Any further engine-quality work (e.g. larger STT
model, multi-request validation) is explicitly out of scope until that run.
