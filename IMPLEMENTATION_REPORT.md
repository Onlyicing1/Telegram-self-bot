# IMPLEMENTATION REPORT — CURRENT STATE

## Latest phase — Media Processing M1.6: direct STT — the transcript itself, without a second model

Repository `Onlyicing1/Telegram-self-bot` · branch `main` · state as of 2026-09-16.

### Current implementation stage and commit lineage

| Commit | Role | Files |
|---|---|---|
| `0f8e819` `feat: deliver direct STT transcripts without a second model` | the M1.6 feature: deterministic direct-STT classification and the provider bypass | `backend/services/media_ai_service.py`, `tests/test_media_direct_stt.py` (new), `IMPLEMENTATION_REPORT.md` (rewrite) |
| `14f5469` `fix: match direct-STT intent by explicit forms, add the SST alias` | the M1.6 classifier correction: regex matching replaced by an explicit finite intent-form inventory; `sst` added as an explicit `stt` alias | `backend/services/media_ai_service.py`, `tests/test_media_direct_stt.py` — `IMPLEMENTATION_REPORT.md` was **not** part of this commit |
| this documentation commit | synchronizes this report with the post-`14f5469` repository state; no production or test change | `IMPLEMENTATION_REPORT.md` only |

| Item | Value |
|---|---|
| Current HEAD at documentation time | `14f54691ae2aac9cc768b7e55e1a6c0329cea2d6` == `origin/main` |
| Media boundary | **unchanged** — `backend/services/media_service.py` was not modified by M1.6 or by the correction: deterministic resolution, the bounded transfer, validation, `_normalize_extracted_text`, `_cap_text`, the `SttEngine` seam, every timeout and cleanup path are identical, and the direct request runs through the very same `analyze_media()` call |
| Provider architecture | **unchanged** — `ProviderManager`, every adapter, the fallback mesh and the registry were not modified. `ProviderManager.chat` is *not called at all* for a deterministic direct-STT request; it is called exactly as before for every analytical media request |
| STT engine / instruction | **unchanged** — the M1.5c language/script contract (`STT_INSTRUCTION`) is byte-identical; no new engine, no new dependency, no new SDK |
| Dispatcher / delivery | **unchanged** — `_try_media_analysis`, `_build_fast_path_result`, `delivery.process_output`, `format_presentation`, `apply_presentation_provenance` and `deliver_response` were not modified |
| OCR / PDF / DOCX | **unchanged** |
| Database / ENV / deployment | **none** — no schema, migration, config, `render.yaml`, `Procfile` or dependency change; `DATABASE_ARCHITECTURE.md` untouched |
| Live Telegram verification | **NOT PERFORMED** — for either `0f8e819` or `14f5469`; M1.6b remains the next runtime step |

### Purpose of this phase

The M1.5 source audit and the value-lineage investigation proved that for an
explicit transcription request the owner-visible text is produced by the
**second** LLM: `media_ai_service` sent the transcript to `ProviderManager.chat`
under a system prompt that asks the model to *answer* — with no
preserve/verbatim clause — so the transcript could be restated, re-wrapped or
garbled on its way to Telegram. The observed live symptom: a 51-character
engine transcript became an 80-character Persian-wrapped model answer.

M1.6 removes the second model from the explicit-transcription case: the request
is classified deterministically, and the extracted transcript is delivered
**as the answer**. The `14f5469` correction fixes **only how the intent is
recognized** — deterministic explicit-form matching instead of regex — not the
Gemini transcript itself; see "What the correction does NOT do" below.

### Current architecture / data flow

```
explicit STT request ("این رو stt کن" / "این رو SST کن" / "transcribe this")
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

### Direct-STT classifier contract (post-`14f5469`)

`media_ai_service.is_direct_stt_request()` — a pure, deterministic function at
the narrowest media boundary (`media_ai_service`, which owns the request→answer
decision). It matches an **explicit finite inventory** of intent forms
(`_STT_FORMS`, 11 entries, closed on purpose — new phrasings are added with
evidence, never guessed). **No regular expression is used**: the module has no
`import re` at all, and the classifier uses no fuzzy matching, similarity
scoring, edit distance, stemming or model-based classification.

**English forms — whole-token matching** (`_STT_ENGLISH_WORDS`): the
lowercased request is split on whitespace and a form must appear as a WHOLE
token, so `stt` never matches inside `testing`/`distinct` and `sst` never
matches inside `sstt`:

- `stt`
- `sst` — see the alias note below
- `transcribe`
- `transcript`

**English multi-word form — token-joined phrase** (`_STT_ENGLISH_PHRASE`):
`speech to text` is matched against the token-joined text, so whitespace runs
cannot split it.

**SST alias — intentional, not fuzzy.** `sst` is a listed alias of `stt`,
representing the common transposition typo owners actually type: `این رو SST
کن` follows the exact same direct path as `این رو STT کن`. It is its own entry
in the inventory, matched by the same whole-token rule. It introduces **no
general typo correction**: `trnascribe`, `sttc` and every other unlisted
misspelling stay unrecognized.

**Persian forms — unchanged verbatim-substring matching**
(`_STT_PERSIAN_FORMS`): `ترانشریپ` / `ترانشریپت` (loanword), `ترانشرینگ`,
`پیاداداری` (formal transcription — honored only for request-sized texts, ≤
120 characters, so a long message *quoting* a transcription is not hijacked),
the Persian spelling-out of the acronym `سی‌تی‌ی`, and the colloquial
`متن‌بخون` / `متن‌بکن`. They match as verbatim substrings of the lowercased
request because Persian affix/ZWNJ compounding makes separate tokens of one
written word — the same behavior M1.6 shipped with, unchanged by the
correction.

**Negative (analytical / conversational — keeps the LLM path):**
`این ویس درباره چیه؟`, `این صدا چی میگه؟`, `خلاصه این ویس رو بگو`,
`این فایل صوتی رو تحلیل کن`, `متنش رو بنویس` (plain "write its text" without
the compound), `summarize this voice`, `what does the voice say`,
`save this voice note`, greetings, empty/None. Deliberately NOT matched:
`read`, `listen`, `understand`, `say`, `analyze`, `translate`, `summarize` —
broad semantic guessing is exactly what this boundary avoids.

**Media-type gate:** applied at the single branch point in
`answer_media_request` — only `Voice` and `Audio` analyses qualify. A
"transcribe this" ask over a Document/Image still goes to the model.

### What the correction does NOT do

`14f5469` changes deterministic **intent recognition only**. It does not
improve, and does not claim to improve, Gemini transcript quality: if the STT
engine returns a garbled transcript, the owner now sees that garbled
transcript directly — by design (fidelity) — with the M1.5c language/script
contract as the engine-side mitigation.

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

### Files changed by the implementation

| File | Commit | Change |
|---|---|---|
| `backend/services/media_ai_service.py` | `0f8e819`, then `14f5469` | the classifier (`is_direct_stt_request` + the finite `_STT_*` inventories), the honest-provenance direct answer, and the single bypass branch |
| `tests/test_media_direct_stt.py` | `0f8e819`, then `14f5469` | **new** — 51 tests pinning the boundary (see validation) |
| `IMPLEMENTATION_REPORT.md` | `0f8e819`, then this documentation commit | rewritten as the current-state report; **not** touched by `14f5469` |

### Validation results

**Validated during `0f8e819` (still true, re-confirmed below where noted):**
the direct execution contract over a real `Dispatcher` + real
`ProviderManager` — `result.response == TRANSCRIPT` exactly; `provider.prompts
== []`; `build_media_messages` raises if ever called;
`client.ops == ["get_messages", "download_media"]`; provenance `local` /
`gemini-stt-deterministic`; `direct_stt_completed` traced with no
`provider_call_*`; no log record contains the transcript; the analytical
requests reach the provider with the exact two-message shape; failures keep
their media identity.

**Newly executed in THIS documentation task** (Python 3.10 venv, pytest
9.1.1), after `14f5469`:

```
tests/test_media_direct_stt.py ................. 51 passed   (was 35 pre-correction)
media suites (direct_stt, ai_integration, stt, stt_language,
              gemini_engine, processing, document_extraction,
              image_ocr) ........................ 402 passed
full suite: pytest tests/ .......... 3279 passed, 24 skipped (was 3263 pre-correction)
py_compile backend/services/media_ai_service.py
           tests/test_media_direct_stt.py ................. OK
git diff --check ............................................ clean
```

The 51-test file proves, among the rest: the required positive forms
(`این رو STT کن`, `این رو SST کن`, `stt`, `sst`, `transcribe this`,
`transcript this`, `speech to text` + the Persian forms); the negatives above;
whole-token discipline (`testing`/`distinct`/`sstt`/`sttc`/`transcribed`/
`transcripts` do NOT match); no general typo correction (`trnascribe` does not
match); the finite inventory identity (`_STT_FORMS` == Persian ∪ English ∪
phrase, with `{stt, sst}` ⊆ English words); and **that the classifier uses no
regex** (`test_the_classifier_uses_no_regex` inspects the module members and
the classifier's own source). The `این رو SST کن` variant is also dispatched
end-to-end to prove it returns exactly the normalized STT content with the
provider bypassed.

### Live verification status

**NOT PERFORMED.** No live Telegram request has been sent against this
correction. M1.6b — one live verification run — remains the next step; nothing
in this report should be read as a live observation.

### Known limitations

1. **Not live-verified.** The classifier and bypass are proven by tests only;
   one live Telegram reply-to-Voice `این رو STT کن` (and `این رو SST کن`)
   request must confirm the end-to-end behavior on the deployed runtime.
2. **The engine's own transcript quality is unchanged** (see above) — the
   correction is about recognition, not recognition quality of the audio.
3. **The intent inventory is a closed list.** Unlisted phrasings (new slang,
   other languages) fall through to the analytical path — safe, never wrong;
   the list can grow with evidence.
4. **`پیاداداری` length guard is heuristic** (≤ 120 characters) and could
   mis-gate an unusually long legitimate request; it exists to prevent
   hijacking long messages that merely mention the word.
5. **No general typo correction exists by design**: only `sst` is an alias;
   every other misspelling lands on the analytical path.
6. English matching is lowercase whole-token English only; a request fully in
   another language falls to the analytical path.

### Exact next stage

**M1.6b — one live verification run** (not another architecture change):
deploy, reply to a Persian Voice note with `این رو STT کن` **and** with
`این رو SST کن`, and confirm both are recognized as direct-STT requests — the
delivered text equals the engine's `stt_engine_returned chars=N` content
(compare `media_completed` and the `AI_OUTPUT_NORMALIZED` line for the same
request id) — while an analytical request (`این ویس درباره چیه؟`) still takes
the LLM path. Record the observation in `INVESTIGATION.md` §2.1.J — this
closes the value-lineage gap the investigation left open. Any further
engine-quality work is explicitly out of scope until that run.
