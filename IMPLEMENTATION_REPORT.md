# IMPLEMENTATION REPORT — CURRENT STATE

## Latest phase — Media Processing M1.7e: the bounded multi-pass STT accuracy seam

Repository `Onlyicing1/Telegram-self-bot` · branch `main` · state as of 2026-09-17.

### Current stage

M1.7e. The two live STT problem classes stay separate, and this phase acts on the
FIRST one only — **without claiming it is solved**:

| Class | Status before this phase | Status after this phase |
|---|---|---|
| **A. Recognition quality** (`INVESTIGATION.md` §19: a transcript WAS produced, but with wrong words; the same Persian voice note came back differently across runs) | open. §19 proved the repository's own code contributes **no** run-to-run variance, and that the difference must originate at or before the engine's return — i.e. at or inside the provider. No local mechanism acted on it. | **addressed by an opt-in mechanism, NOT declared fixed.** The engine can now recognise the same bytes up to three times under ONE deadline and reconcile the hypotheses with a pure STT-only consensus, and an operator benchmark exists that measures whether repeated passes differ at all and whether consensus improves WER/CER. The seam is OFF by default, and **no accuracy improvement is claimed** — that claim is reserved for the live benchmark (M1.7f). |
| **B. Request timeout** (`INVESTIGATION.md` §20: no transcript at all, because the engine's own HTTP bound expired) | addressed by M1.7c (one operation deadline, documented `uri` representation, classified failures, one bounded retry/fallback, bounded cleanup) | **unchanged** — the multi-pass route reuses that deadline and that discipline rather than adding a second one. |

The phase adds **one new accuracy mechanism, one operator tool and no new
architecture**: the media boundary, the deterministic target resolution, the
direct-STT answer contract, the prompt/context layers, the provider mesh, the
route selection, the request field inventory (PART 1 of M1.7c) and every existing
dependency are untouched.

### Commit lineage

| Commit | Role | Files |
|---|---|---|
| `5b3f6d78a8456581b02161443348347c0d5d5b85` (`fix(stt): bound the dedicated transcription operation to one deadline`) | **starting HEAD** (== `origin/main` when this phase began; the working tree was clean) | (M1.7c) |
| the implementation commit of this phase (SHA in the hand-off response; `git log -1 --format=%H` re-verifies it) | M1.7e: the bounded multi-pass accuracy seam, the STT-only consensus, the repeat-run benchmark and this report | `backend/services/stt_consensus.py` (new), `backend/services/gemini_media_engine.py`, `backend/tools/__init__.py` (new), `backend/tools/stt_benchmark.py` (new), `tests/test_stt_consensus.py` (new), `tests/test_media_stt_multipass.py` (new), `tests/test_media_stt_benchmark.py` (new), `tests/test_media_dedicated_stt.py`, `tests/test_media_gemini_engine.py`, `tests/test_media_stt_reliability.py`, `IMPLEMENTATION_REPORT.md` |

| Item | Value |
|---|---|
| `INVESTIGATION.md` | **unchanged** — the evidence base is §19 (class A) and §20 (class B); this phase implements, it does not re-investigate |
| Media boundary (`media_service.py`, `backend/ai/media.py`, `backend/telegram_api/media.py`), `media_ai_service.py`, `dispatcher.py`, `ai_unified.py` | **unchanged** |
| `ProviderManager`, adapters, registry, `ToolRegistry`, `ToolExecutor`, `RuntimeSupervisor`, `vision()`, prompt/context/memory/tool layers | **untouched** |
| Supabase / `DATABASE_ARCHITECTURE.md` / Save / Task system / Scheduler / OCR / video / UI | **untouched** |
| Dependencies (`backend/requirements.txt`), `render.yaml`, `Procfile`, ENV files, secrets | **unchanged** — no new package, no new credential, no new required variable |
| Live Telegram verification | **NOT PERFORMED** (no session, credential or traffic here) |
| Live Gemini verification | **NOT PERFORMED** — every HTTP interaction in this phase was a scripted in-process transport |

### Purpose of this phase (what §19 proved, and what follows from it)

`INVESTIGATION.md` §19 established, from source, five facts that decided this
phase's design:

1. the direct-STT path delivers the engine's transcript **verbatim** — no
   normalization, no second model, no ProviderManager round (`provider.prompts == []`
   is asserted by `tests/test_media_direct_stt.py`);
2. the audio reaches the provider **byte-identical** (no decode/re-encode, no
   transcoding step exists in the tree);
3. for the SAME audio and the SAME request, **nothing in the repository varies
   between runs** — so the observed difference came from the provider side;
4. therefore the only remaining lever inside this repository is **which request
   is sent** (model, language, mode — M1.5c/M1.7, all already exercised) and
   **what is done with more than one answer** (this phase);
5. the transcript **content** is never captured anywhere, which is why §19.4 could
   not separate "the model misrecognized" from "the code altered the text" — and
   why this phase adds an operator benchmark instead of a second logging channel.

### Investigation results this phase added (design evidence, not live evidence)

**1. Do repeated passes actually produce useful independent information? — NOT
YET PROVEN, and the repository now records exactly what would decide it.**

* On the **general route** the request carries `temperature=0.0` (M1.5c,
  unchanged). A deterministic-decoding request is *expected* to return identical
  text for identical bytes, i.e. three passes would agree and the consensus would
  be a byte-exact no-op. That possibility is now recorded rather than assumed:
  the consensus line reports `changed=`/`dropped=`, and `0/0` on repeated live
  runs means "no independent information".
* On the **dedicated route** the documented transcription contract carries no
  sampling control at all (M1.7 deliberately introduces none), so that model's
  determinism for one audio file is **[UNKNOWN]** from here.
* §19.4's two differing transcripts are **not** proof of provider stochasticity on
  one route: the dedicated route landed *between* the two observations (`da05ace`),
  and neither run recorded its route, model or language. Establishing that the
  runs shared a route now costs nothing — `GEMINI_MEDIA_ENGINE` carries `model`,
  `transport`, `language`, `mode`, `bytes`, `chars`, `attempts`, `stt_passes` and
  `failure_class`.

**2. Is STT-only consensus (Design A) technically feasible? — Yes, and it has a
hard minimum pass count of THREE.**

The rule is a strict majority per aligned position. With **two** hypotheses every
column is either 2-0 (unanimous ⇒ nothing to change) or 1-1 (a tie ⇒ resolves to
the scaffold), so a two-pass result is *byte-identical to the first pass by
construction*: two passes buy a second model call and can change nothing. That is
a property of the rule, not tuning, and it is now asserted directly
(`test_two_passes_are_byte_identical_to_the_first`). Three passes are the smallest
count that can outvote one dissenting reading (2 of 3). Two is still accepted
because the repeat-run consistency measurement of the benchmark needs it.

**3. Is model-based reconciliation (Design B) admissible? — Evaluated and NOT
implemented.**

It would put a **second language model** inside the direct-STT path, which
`INVESTIGATION.md` §19.2/§19.3 and the M1.6 contract forbid, and it would create
the exact failure mode this task warns about: a model asked to "resolve
disagreements" can rewrite freely, invent fluent content and silently change the
speaker's words. It is deferred, not rejected forever: it becomes worth
reconsidering only if the live benchmark shows STT-only consensus leaves a
significant residual disagreement rate **and** a bounded, non-rewriting
reconciliation contract (disagreement spans only, unanimous tokens frozen, no
context) can be specified and tested first.

**4. Hallucination risk of the implemented mechanism, and how it is bounded.**

| Risk | Mitigation in the code |
|---|---|
| Inventing words | every emitted token is a hypothesis' own surface token; a word only one reading produced is never emitted (insertions are unrepresentable); `test_every_emitted_token_exists_in_at_least_one_hypothesis` |
| Rewriting/cleaning the transcript | the consensus module never emits its comparison key; unanimity returns the first pass **byte-exact**; there is no second model, prompt or glossary anywhere |
| Preferring a longer/more "natural" reading | the scaffold is the **median-length** reading, never the longest; a strict majority decides each position, and a tie keeps the scaffold |
| Losing a word the majority heard | found by this phase's own tests as a real defect of the first design: with pass 1 as the word grid, a length-mangled first reading made two agreeing readings' words "insertions" and dropped a word both of them heard (the §19.1 example lost `خلاصه`). The grid is therefore the median-length reading, with a regression test (`test_a_first_pass_with_a_mangled_length_cannot_set_the_word_grid`) |
| Correlated errors producing a confident wrong majority | **not mitigable inside one model**: two correlated mis-recognitions outvote one correct reading. This is the fundamental limit of multi-pass, it is stated in the module docstring, and it is the reason the seam is opt-in and benchmark-gated |
| A transport failure masquerading as a recognition result | a failed pass contributes no hypothesis; pass accounting is separate from attempt accounting (see below) |

### Exact STT architecture after implementation

```
explicit STT request  ("stt" / "sst" / "transcribe" / …; UNCHANGED)
→ dispatcher._media_target / _try_media_analysis                (UNCHANGED)
→ media_ai_service.answer_media_request                         (UNCHANGED)
→ media_service.resolve_media_message → analyze_media           (UNCHANGED: deterministic target, bounded
                                                                 download, MIME/container/stream validation,
                                                                 to_thread(engine.transcribe), cap, cleanup)
→ GeminiMediaEngine.transcribe                                  (the seam: configuration decides the route)
     ├─ AI_GEMINI_STT_PASSES unset / 1  → the single-pass routes exactly as M1.7c:
     │      AI_GEMINI_STT_MODEL unset ⇒ POST /models/<model>:generateContent
     │      AI_GEMINI_STT_MODEL set   ⇒ POST /v1beta/interactions (Files API uri, inline fallback)
     └─ AI_GEMINI_STT_PASSES = 2 or 3  → the bounded multi-pass route:
            ONE upload (dedicated) / inline-or-one-upload (general)
            → N sequential POSTs against that same input, ONE deadline
            → reconcile_hypotheses([...])  (pure, STT-only)
→ MediaAnalysis.content == the (reconciled) transcript           (UNCHANGED)
→ dispatcher._build_fast_path_result → delivery                  (UNCHANGED: no second model)
```

### The reconciliation rule, exactly

`backend/services/stt_consensus.py` — pure, dependency-free, `reconcile_hypotheses(hypotheses) -> ConsensusResult`:

1. **Scaffold** — the alignment word grid is the **median-length** non-empty
   hypothesis (lowest index on a length tie); with fewer than three hypotheses it
   is the first non-empty one, so a two-pass run is byte-identical to pass 1; an
   empty hypothesis is never the scaffold (it has no grid).
2. **Votes** — one vote per scaffold position per hypothesis. A word aligned to
   the position is a vote for that word; **the absence of a word is a vote too**;
   a replacement of a *different length* is alignment-ambiguous and votes nothing;
   a word the scaffold does not have is an **insertion and can never be emitted**.
3. **Decision** — a strict majority (`votes > total/2`) wins; the scaffold's own
   surface token is kept when it is in the winning group; when the winning group
   is "no word" the position is **dropped**; a tie, a lone dissent or an
   all-different column keeps the scaffold's token (insufficient evidence).
4. **Output** — rebuilt from the scaffold's own separators, so nothing is
   re-spaced; when no position was overridden the scaffold string is returned
   **byte-exact**.
5. **Comparison key** (alignment only, never emitted) — NFC, the `ي→ی`/`ك→ک`
   variants and the hamza-alef forms, `ة`/`ۀ`→`ه`, the harakat and tatweel
   removed, the zero-width family removed (so `میکند` ≡ `میکند`), Persian/Arabic
   digits ≡ ASCII digits, and punctuation stripped from a token's ends. **`آ` is
   deliberately NOT folded into `ا`** (`آب` ≠ `اب`), and casing is left alone.
6. **Counters** — `hypotheses`, `positions`, `changed`, `dropped` are bounded
   integers with no transcript content, so they are safe to log and are what makes
   a live multi-pass run measurable.

### Configuration

| Variable | Default | Values | Meaning |
|---|---|---|---|
| `AI_GEMINI_STT_PASSES` | unset → **1** | `1`..`3` | recognition passes per explicit STT operation. Clamped (a value above the ceiling becomes 3, a non-integer becomes 1 with a bounded warning that never echoes the value); an explicit constructor argument always wins. **Unset means the single-pass routes of M1.7c, byte-identical.** |

### Deadline, retry and pass behaviour

| Question | Answer |
|---|---|
| Operation deadline | **unchanged**: `STT_OPERATION_DEADLINE_S` = 45 s inside the boundary's `STT_TIMEOUT_S` = 60 s. A multi-pass run does **not** get a second or a longer deadline. |
| Per-leg timeouts | derived from the remaining budget exactly as M1.7c (`connect ≤ 10 s`, `write ≤ 20 s`, `pool ≤ 10 s`, `read` = whatever is left) |
| How many passes actually run | the configured count is a **ceiling**; a pass starts only with ≥ `STT_MIN_ATTEMPT_S` (8 s) of budget left, so a slow provider yields fewer hypotheses and the operation still succeeds honestly (fewer hypotheses ⇒ the consensus degrades to the best available reading; one hypothesis ⇒ byte-identical to M1.7c) |
| Can three passes fit 45 s? | only if the provider answers within roughly 12 s per pass (3 × ~12 s + the one upload). Whether that matches the provider's real Persian latency is **[UNKNOWN]** — the benchmark measures it. The documented response is **not** to inflate the deadline silently: the options are (a) keep 1 pass, or (b) raise the deadline deliberately, with the boundary's 60 s bound as the hard ceiling. |
| Retries per pass | **none** on the multi-pass route: the configured passes *are* the recovery budget, so a transient failure consumes a pass instead of multiplying requests. Total provider requests ≤ the configured pass count (3), never 6. |
| A transport retry as a recognition pass? | impossible by construction: a pass is appended to the hypotheses **only** when it returned text, and `attempts` (provider requests) is logged separately from `passes`/`hypotheses` |
| Deterministic failures inside a multi-pass run | stop the loop at once (the same request is never re-sent **even once**); if hypotheses already exist, the reconciled result of what did arrive is returned; if none exist, the operation fails closed with the **first** failure recorded |
| Transport fallback | the dedicated route keeps M1.7c's contract: upload once (the documented `uri` form) and fall back to the inline form only on a **transient upload failure** with the payload inside the inline budget; a deterministic upload failure fails closed. The general route keeps its own M1.5c rule (inline inside the budget, one upload above it). Exactly one upload per operation either way. |
| Cleanup | one remote delete per operation, in `finally`, with its own 5 s bound **outside** the deadline; a failed delete is logged, never raised; the inline paths upload nothing and delete nothing |

### Observability added

```
GEMINI_MEDIA_ENGINE_STAGE kind=speech-to-text stage=consensus_start attempt=1 transport=uri bytes=115 elapsed_ms=-
GEMINI_MEDIA_ENGINE_STAGE kind=speech-to-text stage=pass attempt=1 transport=uri bytes=115 elapsed_ms=412
GEMINI_MEDIA_ENGINE_STAGE kind=speech-to-text stage=pass_failed attempt=2 transport=uri bytes=115 elapsed_ms=88
GEMINI_MEDIA_ENGINE_CONSENSUS model=gemini-3.5-transcribe transport=uri mime=audio/ogg bytes=115 \
  passes=3 hypotheses=2 positions=8 changed=2 dropped=0 elapsed_ms=1417
GEMINI_MEDIA_ENGINE kind=speech-to-text engine=GeminiMediaEngine model=… transport=interactions \
  mime=audio/ogg bytes=115 language=auto mode=verbatim chars=41 elapsed_ms=1502 status=ok \
  attempts=3 deadline_s=45 failure_class=- stt_passes=3
```

Every field is bounded operational metadata: no transcript, no audio, no
credential, no chat id, no sender, no filename, no caption. `changed`/`dropped`
answer the question §19.4 could not: whether repeated passes differed and whether
the consensus had to intervene at all.

### Context isolation (verified, not merely intended)

* `reconcile_hypotheses` has exactly **one** parameter (`hypotheses`) — there is
  no slot for a chat id, sender, username, filename, caption, reply, previous
  message, memory or session; asserted by `test_the_signature_accepts_hypotheses_and_nothing_else`.
* The engine calls it with a list of `str` and nothing else
  (`test_the_reconciler_receives_only_the_hypotheses`).
* The multi-pass requests contain no caption, filename, chat id or owner id
  (`test_no_telegram_metadata_reaches_the_model_or_the_reconciler`), and the
  dedicated route still sends no text instruction at all.
* The consensus module imports no I/O, network, provider, model or Telegram layer
  (`test_the_module_imports_no_io_network_or_model_layer`).

### Accuracy-related decisions

* No normalization, no regex correction, no dictionary, no fuzzy matching, no
  transliteration and no "cleanup" pass was added anywhere. Persian script, ZWNJ,
  code-switching and legitimate Latin tokens pass through unchanged (asserted).
* The transcript is still returned to the owner verbatim, still without
  ProviderManager, and still without a second LLM.
* The **only** accuracy mechanism added is hypothesis reconciliation, and it can
  only substitute or delete inside the scaffold's own word grid with a strict
  majority behind it.
* **No claim is made that multi-pass recognition improves Persian accuracy.** The
  benchmark has not been run (no credential, no audio fixture in the repository),
  and this report contains no fabricated numbers.

### Latency / API-call / quota implications (stated, not discovered later)

| Configuration | HTTP requests per operation (dedicated route) | Model calls |
|---|---|---|
| `passes=1` (default, unchanged) | 4 = upload `start` + `upload, finalize` + 1 interaction + 1 delete | 1 |
| `passes=3` | 6 = upload `start` + `upload, finalize` + 3 interactions + 1 delete | 3 |
| `passes=3`, general route, small payload | 3 interactions (no upload, no delete) | 3 |

* The upload is shared, so the extra passes cost a **model call** each, not an
  upload each — the explicit reason the upload is prepared once (a per-pass upload
  would have been 3 × 2 extra requests).
* The general route's small-payload path uploads nothing at all (inline).
* Cost/quota: N× the transcription calls of the single pass, on the project's
  existing tier. Rate limiting is unchanged and honest: a 429 is a transient
  failure of that pass (not retried in place), bounded by the pass count.

### Files changed by this phase

| File | Change | Lines |
|---|---|---|
| `backend/services/gemini_media_engine.py` | the pass-count configuration (`STT_PASSES_ENV_VAR`, `STT_MAX_PASSES`, `resolve_stt_passes`), the `stt_passes` constructor/property, the dispatch in `transcribe`, the bounded `_run_consensus` / `_prepare_consensus_input` / `_inline_item` / `_uri_item`, the consensus trace line, and `stt_passes=` on the outcome line | +262 / −5 |
| `backend/services/stt_consensus.py` | **new** — the pure STT-only consensus (`reconcile_hypotheses`, `ConsensusResult`, `comparison_key`) | 331 |
| `backend/tools/stt_benchmark.py` | **new** — the opt-in repeat-run benchmark (pure WER/CER/edit-count/script metrics, API-call accounting, a scripted-engine-injectable runner, a CLI) | 390 |
| `backend/tools/__init__.py` | **new** — package marker ("operator tools, never imported by the runtime") | 1 |
| `tests/test_stt_consensus.py` | **new** — 44 tests for the reconciliation rule | 391 |
| `tests/test_media_stt_multipass.py` | **new** — 44 tests for the engine seam | 621 |
| `tests/test_media_stt_benchmark.py` | **new** — 32 tests for the benchmark's metrics, accounting and loader | 299 |
| `tests/test_media_stt_reliability.py` | the `__slots__` assertion gains the new field | +1 |
| `tests/test_media_dedicated_stt.py` | the same | +1 |
| `tests/test_media_gemini_engine.py` | the same, plus its comment | +4 / −3 |
| `IMPLEMENTATION_REPORT.md` | rewritten as the current-state report (this document) | — |

No other file changed; `backend/requirements.txt`, the boundary, the provider
mesh, the dispatcher, the handlers, Supabase and the runtime are untouched.

### Tests and exact results

All commands ran in a project-local Python 3.10.12 environment (`.venv`,
gitignored) from `backend/requirements.txt` plus `pytest 9.1.1` /
`pytest-asyncio`; **no network, no Gemini credential, no live Telegram**.

```
.venv/bin/python -m pytest tests/test_stt_consensus.py -q
  44 passed in 0.07s          (new: the reconciliation rule)
.venv/bin/python -m pytest tests/test_media_stt_multipass.py -q
  44 passed in 0.39s          (new: the engine seam)
.venv/bin/python -m pytest tests/test_media_stt_benchmark.py -q
  32 passed in 0.26s          (new: the benchmark harness, hermetically)
.venv/bin/python -m pytest tests/test_stt_consensus.py tests/test_media_stt_multipass.py \
    tests/test_media_stt_benchmark.py tests/test_media_stt_reliability.py \
    tests/test_media_dedicated_stt.py tests/test_media_gemini_engine.py \
    tests/test_media_direct_stt.py tests/test_media_stt.py tests/test_media_stt_language.py \
    tests/test_media_ai_integration.py tests/test_media_processing.py \
    tests/test_media_document_extraction.py tests/test_media_image_ocr.py -q
  636 passed in 40.12s
.venv/bin/python -m pytest tests/ -q
  3513 passed, 24 skipped, 3 warnings in 112.63s
  (baseline before this phase: 3393 passed, 24 skipped — this phase adds 120 tests)
.venv/bin/python -m py_compile backend/services/gemini_media_engine.py \
    backend/services/stt_consensus.py backend/tools/stt_benchmark.py backend/tools/__init__.py \
    tests/test_stt_consensus.py tests/test_media_stt_multipass.py tests/test_media_stt_benchmark.py
  OK (no output)
git diff --check
  clean
```

What the new suites pin, against the task's own checklist:

* **consensus behaviour** — identical hypotheses; a two-hypothesis disagreement;
  a three-hypothesis majority; all three disagreeing; an insertion in one
  hypothesis (never emitted); a deletion/minority deletion; a substitution; the
  §19.1 Persian example (resolved to the majority reading, byte-exact); mixed
  Persian/English tokens; punctuation differences; ZWNJ and Persian/Arabic letter
  and digit variants; an empty hypothesis (and a majority of empty ones); a word
  the majority heard never being dropped; no token ever invented; the median grid;
  determinism; purity; a single-parameter signature; non-string inputs ignored;
* **the engine seam** — the default single-pass route unchanged; environment
  parsing/clamping/warning without echoing the value; explicit configuration
  beating the environment; one upload for three passes; three sequential
  interactions against that one uploaded file; one delete after the last pass;
  the reconciled (majority) transcript; each leg's timeout still derived from the
  one deadline; a spent deadline sending nothing; a pass needing budget to start;
* **retry vs pass separation** — a transient pass failure counted as a failed pass
  (`passes=3 hypotheses=2`) and never as a hypothesis; a persistent 503 bounded at
  exactly three requests (no per-pass retry); a deterministic 400 stopping the
  loop at the first request; a good pass followed by a deterministic failure still
  returning its transcript; the first failure being the reported one;
* **transport fallback** — a transient upload failure falling back to the inline
  form once, with the same bytes; a deterministic upload failure failing closed
  with no interaction request and no file to delete;
* **routes** — the dedicated route never touching `generateContent`; the general
  route running the consensus with the STT instruction and one upload when the
  payload is large; the OCR seam never running the consensus; an empty payload
  sending nothing; a non-audio payload refused before any request;
* **isolation and observability** — the reconciler receiving only hypotheses; no
  Telegram metadata in any request or log; one outcome line per operation; the
  consensus line carrying counts but no content; sequential calls; no retained
  HTTP state; bounded elapsed time;
* **the benchmark** — edit counts, WER/CER, script buckets and repeat-run
  consistency over known inputs (Persian included, no quality claim); per-leg API
  accounting through real `httpx` clients; the report shape, requested pass counts
  and `repeat`; failures recorded as data; the local-file loader's bounds.

**These tests prove request construction, bounds, pass/retry separation,
reconciliation and failure behaviour. They prove nothing about recognition
quality** — no fixture in this repository can, and none is faked.

### Benchmark methodology (designed and implemented, NOT run)

```bash
# one local audio file, the operator's own human-verified transcript
.venv/bin/python -m backend.tools.stt_benchmark \
    --audio var/voice-1.ogg --reference var/voice-1.txt \
    --passes 1,2,3 --repeat 3 --json var/voice-1.report.json
```

* the **same original bytes**, the same MIME (container-derived), the same model,
  language configuration and verbatim mode across every configuration — the only
  variable is the number of passes;
* per configuration it records: the transcript, the elapsed time, the exact number
  of API calls per leg, the empty-output state and any failure;
* `--repeat` measures **repeat-run consistency** (the share of repeated single
  passes returning the most common transcript) — the precondition the consensus
  needs — plus the script buckets (Arabic vs Latin letters) that catch the
  romanization failure without needing a reference;
* with `--reference` it reports **WER, CER and the substitution/deletion/insertion
  counts** against a human-verified transcript;
* **no results are reported here** because no live run has happened. A single
  sample's WER describes that sample; the tool says so in its own output.

### Live Telegram verification status

**NOT PERFORMED.** No live Telegram session was used. The direct-STT route remains
end-to-end covered over a scripted transport by `tests/test_media_direct_stt.py`
and `tests/test_media_dedicated_stt.py`, which still assert `provider.prompts == []`
and `result.response == TRANSCRIPT`.

### Live Gemini verification status

**NOT PERFORMED.** Consequently the following remain **[UNKNOWN]** and are not
claimed anywhere:

* whether repeated passes on either route return the SAME text (the precondition
  for the whole mechanism) — the general route's `temperature=0.0` suggests they
  will, and the dedicated route's contract has no sampling control at all;
* whether the dedicated model accepts the inline `data` form (M1.7c's fallback);
* whether three passes fit inside the 45 s deadline for a real Persian voice note;
* the real latency distribution, and therefore the real cost of the seam.

### Known limitations

1. **No accuracy claim.** The mechanism exists, is bounded and is tested; whether
   it helps Persian recognition is unmeasured.
2. **Correlated errors.** All hypotheses come from the same model over the same
   bytes, so a repeated mistake wins a majority. Multi-pass can only correct a
   mis-recognition the other passes did not repeat.
3. **No recovery of a word the grid lacks.** A word no scaffold position covers is
   an insertion and is never emitted; with three readings the median grid makes
   that harmless, but a word only ONE reading heard can never be added.
4. **Three passes are the ceiling**, and they may not fit the deadline; the
   realized count is not guaranteed (by design — the deadline wins).
5. Two passes cannot change anything (documented, tested) — `AI_GEMINI_STT_PASSES=2`
   spends a second call for the repeat-run measurement, not for accuracy.
6. The alignment is a word diff: a repeated token can pair with the wrong
   occurrence, which is why a single dissenting vote never changes anything.
7. The engine is constructed per provisioning call and, since M1.7c, the
   pass-count configuration is read in the constructor when the caller does not
   state it (the factory does not pass it), so the startup `…_PROVISIONED` line
   still shows the model and language but **not** the pass count — the pass count
   appears on every media operation line instead (`stt_passes=`).
8. A timeout inside a worker thread still cannot be cancelled (unchanged, M1.7c
   limitation 3).

### Deferred work

* **Design B (model-based reconciliation)** — deferred until the live benchmark
  demonstrates a residual disagreement rate that STT-only consensus cannot
  resolve, and only with a non-rewriting, span-bounded, context-free contract.
* Reading a **sampling/determinism control** for the dedicated route: none is
  documented, so none is sent (no invented fields).
* Promoting the multi-pass route to the default: an evidence-gated decision for
  after M1.7f, never before.
* `INVESTIGATION.md` §19.4's optional transcript-capture instrument — still not
  implemented, and still only acceptable as an explicit opt-in that never logs
  content by default.

### Explicit next stage — M1.7f: the live evidence pass for class A

1. Deploy with `AI_GEMINI_STT_PASSES` **unset**, and confirm the existing
   single-pass route is unchanged (`stt_passes=1` on every media line).
2. Run the benchmark on 5–10 real Persian voice notes with human-verified
   references: `--passes 1,2,3 --repeat 3`. Record, per note: repeat-run
   consistency, WER/CER at 1/2/3 passes, per-leg API calls and elapsed times.
3. Decide from those numbers, and only from them: if consistency is ~1.0, the
   mechanism is a no-op on this provider and the seam stays off (documented as
   such); if consistency is low and 3-pass WER/CER improves without introducing
   words that were never spoken, enable `AI_GEMINI_STT_PASSES=3` — and, if three
   passes cannot fit 45 s, decide explicitly between keeping one pass and raising
   the deadline inside the boundary's bound.
4. Any accuracy statement in `INVESTIGATION.md` §19 is updated **only** from that
   measurement.

### Document version

This document reflects the M1.7e architecture: the bounded, opt-in multi-pass STT
accuracy seam (STT-only consensus), the unchanged bounded transport of M1.7c, and
the operator benchmark that must measure quality before any claim about it is
made. If code changes invalidate any section, update this document in the same
commit.
