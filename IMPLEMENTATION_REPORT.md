# IMPLEMENTATION REPORT — CURRENT STATE

> **This is a CURRENT-STATE document.** It describes the repository as it
> exists at the tip of this phase. If code changes invalidate any section,
> update this document in the same commit.

---

## 1. Implementation metadata

| Item | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Branch | `main` |
| Base commit (this phase) | `6087d2e` (clean tree, `origin/main` equal) |
| Phase | Instrumentation-visibility investigation: why `a75d463` boundary traces were absent from the supplied 02:41 Render excerpt; minimum instrumentation-only correction |
| Implementation commits | `33727f3` · `c35f75f` · `012f738` · `a75d463` · **`7fbaba3` (escalate boundary instrumentation to WARNING for log visibility)** |
| Report commit | see §10 delivery record |
| Status | **INSTRUMENTATION-ONLY — full suite green (2046 passed, 24 skipped). Control-flow audit PROVES `raw_response_shape` executes before any JSONDecodeError classification; supplied logs contained only WARNING-level lines, so INFO visibility is unproven and the two decisive records were escalated to WARNING (no behavior change). Actual provider response content remains unobserved.** (see §6C) |
| Database impact | **NO DATABASE / SCHEMA CHANGE** |

---

## 2. The exact request under investigation

```
هر ۵ دقیقه
میخوام بیو پروفایلم رو آپدیت کنید
یه دیالوگ رندوم از کاراکتر آیانامی ری از انیمه نئون جنسیس بزاری
که زیر 60 کاراکتر باشه
```

Observed response: "I could not turn that into a safe, unambiguous schedule,
so I did not create any task. Restate it as an interval (e.g. 'every X
minutes'), a time, or a daily/weekly cadence with a clear action."

Intended semantics: recurring 5-minute interval · update Telegram bio ·
AI-generated content · character Rei Ayanami · franchise Neon Genesis
Evangelion · random dialogue · strictly under 60 characters.

---

## 3. Root cause (source-traced on `6087d2e`, i.e. AFTER commit `cce41df`)

The failure-layer audit distinguished the ten candidate conditions:

| # | Condition | Verdict |
|---|---|---|
| 7 / 8 | Deterministic routing / request string | **NOT the cause.** `parse_command_intent` routes the EXACT request to `create_task` passing the FULL multi-line text (`کنید` action verb + `هر`/`دقیقه` interval intro). `ai_unified` trigger-stripping preserves the remaining text (`split(None, 1)` keeps multi-line). Proven by tests. |
| 3 / 4 | Candidate validation | **NOT the cause for compliant output.** The exact request with a compliant candidate creates the 300s bio task (proven). Schema-violating output is rejected — which is correct behavior. |
| 5 / 6 | TaskInterpretationError / TaskUnsupportedError | The generic message is the single `CreateTaskTool._fail` for interpretation failures; `TaskUnsupportedError` already maps to the distinct "not supported yet" message. The observed message is the interpretation-failure path. |
| 2 | Malformed JSON / 1 JSON null | Possible provider-output modes — both raise `TaskInterpretationError` → the observed message. NOT distinguishable from the user message alone. |
| **F** | **Prompt/schema contradiction — REAL SOURCE DEFECT** | The timezone instruction said **"interval schedules carry no timezone field"** while `CANDIDATE_SCHEMA` **REQUIRES** the top-level `"timezone"` for every schedule type. A model reading the instruction literally omits the required field → `TaskCandidateError("candidate fields are incomplete or unsupported")` → the exact generic rejection. This wording pre-dated `cce41df` and was NOT touched by it. Additionally, the `cce41df` few-shot example contained the echoable placeholder `"timezone": "<owner timezone>"`. |
| — | **Bio-read hijack — SECOND REAL SOURCE DEFECT (English variants)** | `_BIO_QUERY_WORDS` contains `"my"`, so ANY sentence containing "my bio" — including *"change my bio to …"* — matched the deterministic `get_bio` READ branch. The English equivalent of the live request was answered with the current bio instead of task creation. |
| A | Deployed runtime ≠ GitHub main | **CANNOT be verified from this workspace** (no Render/live access). The observed message text is IDENTICAL in pre- and post-`cce41df` code, so the message alone cannot distinguish a stale deployment from provider-output compliance. `render.yaml` deploys `python -m backend.main` on push to `main` when connected. |

**Why `cce41df` did not prevent the live failure:** its prompt improvements
(addressing null-returning models) are necessary but not sufficient — the
deterministic chain was already compliant, and the two source defects above
(F timezone contradiction, bio-read hijack) were introduced earlier, were
not touched by `cce41df`, and each can produce the observed (or an equally
wrong) outcome regardless of the new prompt text.

---

## 4. Behavioral fixes (commit `33727f3`)

| File | Change |
|---|---|
| `backend/ai/task_interpreter.py` | (1) Timezone instruction reworded: the candidate's TOP-LEVEL `timezone` is REQUIRED for every schedule type; only the SCHEDULE OBJECT carries no timezone for intervals — the contradiction is gone. (2) Few-shot example now uses a concrete `"timezone": "Asia/Tehran"` (no echoable placeholder). (3) AI-GENERATED CONTENT CONTRACT teaches escaping line breaks as `\n` inside `ai_instruction` JSON strings (multi-line robustness). (4) NEW content-free `response_shape` diagnostic (`null`/`object`/`array`/`string`/`unsupported`/`malformed`) emitted on every `candidate_rejected`/`candidate_parse_error`/`candidate_parsed` AI_TASK_TRACE line — one live reproduction now classifies the exact failure mode WITHOUT exposing provider content. |
| `backend/ai/actions.py` | (1) `_has_bio_change_intent` guard: bio WRITE verbs (`change/update/replace/edit/put`, `عوض`, `تغییر`, `آپدیت`) block the deterministic `get_bio` read branch — a write request is never answered with the current bio; it stays conversational (provider/semantic path). (2) `change/update/put/replace/edit` added to `_EN_ACTION_VERBS` so English scheduling variants ("Every 5 minutes, change my bio …") route deterministically to `create_task` with the full text, matching the Persian behavior. Pure vocabulary additions — no new regex, no new parsers. |
| `tests/test_task_interpretation_diagnostics.py` | NEW — 18 failure-layer tests: exact-request routing (full multi-line text preserved), Persian word/ASCII-digit variants, English equivalent routes to create_task (hijack regression), bio write-vs-read distinction, read queries still deterministic, JSON null/malformed/array/schema-violation → distinct `response_shape` traces, unsupported envelope → distinct error, valid + fence-wrapped valid → parsed, prompt contract (no contradiction, concrete timezone, `\n` escape hint), placeholder-timezone candidate rejected, `CreateTaskTool` message mapping (generic vs unsupported vs success, persistence verified). |

---

## 5. Verification

| Check | Result |
|---|---|
| `pytest tests/test_task_interpretation_diagnostics.py -q` | **43 passed** (post-`7fbaba3`: +2 ordering/missing-metadata tests) |
| Adjacent suites incl. correlation-leak test | **52 passed** (diagnostics files) |
| `pytest tests/ -q` (full suite, post-`7fbaba3`) | **2046 passed, 24 skipped** in 63.84s |
| Adjacent routing/AI suites (7 files) | **186 passed** |
| `pytest tests/ -q` (full suite) | **2021 passed, 24 skipped, 1 warning** in 63.69s |
| `py_compile` (modified files) | OK |
| `git diff --check` | clean |
| Regex audit | no new regex (vocabulary tokens only) |
| Changed files | exactly 3 (2 backend + 1 new test) |

**Verification status legend:** routing = reproduced & fixed in tests ·
deterministic chain = reproduced & proven compliant in tests · timezone
contradiction = source-proven, fixed, pinned · bio hijack = source-proven,
fixed, pinned · **live Telegram/provider = NOT PROVEN** (no credentials in
this workspace; production classification requires one log line, §6).

---

## 6. How one live reproduction now classifies the failure

Production `AI_TASK_TRACE` (LOG_LEVEL=INFO) now distinguishes, and — since
`c35f75f` — so does the USER-FACING rejection: the generic message ends
with a bounded `[failure category: ...]` token that one live reproduction
can paste back verbatim (no log access needed):

- `stage=candidate_rejected response_shape=null` → the model returned JSON null.
- `response_shape=malformed` → malformed JSON (incl. unescaped multi-line ai_instruction).
- `response_shape=object reason=...` → schema violation (e.g. missing required
  timezone) with the exact reason.
- `response_shape=unsupported` → the unsupported-capability contract fired.
- `stage=candidate_parsed response_shape=object` → interpretation succeeded
  (then any downstream failure is NOT the interpreter).
- The user-facing reply shows `[failure category: candidate_invalid:null]`
  (provider returned JSON null), `candidate_invalid_json` (malformed JSON),
  `candidate_invalid:object` (schema violation, e.g. missing timezone),
  `timeout`, `provider_manager_unavailable`, `repository_failure`, or a
  `provider=... category=...` token (provider-side failure) — each maps to a
  distinct layer.
- If the reply has NO `[failure category: ...]` suffix, the deployed runtime
  predates `c35f75f` — a stale Render deployment (Render auto-deploys on
  push to `main` only when connected; verify the service picked up
  `fb76fc8`/`c35f75f` and restart it).

---

## 6A. `candidate_invalid_json` root cause & fix (live category evidence, commit `012f738`)

**Live evidence:** after `c35f75f` the bot's reply carried
`[failure category: candidate_invalid_json]` — a `JSONDecodeError` during
`_load_candidate_json`, BEFORE candidate validation/persistence. Semantics
are therefore NOT the cause (the deterministic chain + prompt were already
proven compliant).

**Source-traced root cause (contract mismatch):**
- `ProviderResponse.text` is ONE plain text string. Gemini joins text parts
  (`" ".join(text_parts)`), OpenAI-compat uses message content; no adapter
  adds fences or JSON envelopes. The interpreter prompt does not forbid
  prose around the object.
- The old parser accepted exactly TWO shapes: whole-text JSON, or one
  markdown-fenced block. It therefore rejected, with `JSONDecodeError`:
  (1) prose-wrapped UNFENCED JSON (`Here is the JSON: {...}`) — legitimately
  permitted by the contract; (2) the multi-line `ai_instruction` case with
  unescaped literal newlines inside the string (`json.loads` strict mode
  rejects control characters) — the most likely live shape; (3)
  double-encoded JSON (a JSON string whose content is the object).
- Truncation (Gemini `MAX_TOKENS` / OpenAI `length` `finish_reason` already
  surfaced in `response.metadata`) was indistinguishable from malformed.

**Fix (`012f738`, parser tolerance only — validation untouched):**
- `strict=False` on every parse (stdlib control-character tolerance → raw
  newlines in `ai_instruction` parse); one-level double-encoded unwrap;
  string-aware outer-brace scan (`_outer_object_span`, plain character
  scanning — zero regex) for prose-wrapped unfenced objects.
- Every parsed result STILL passes the full `parse_candidate_output`
  schema validation; genuinely unparseable output still fails closed
  (never fabricated into a candidate).
- Diagnostics (content-free): `candidate_rejected` now logs
  `json_error=JSONDecodeError line=.. col=.. pos=.. raw_len=.. truncated=..
  provider_finish_reason=..`; user-facing categories gained
  `candidate_invalid_json:truncated` and `candidate_invalid_json:empty`;
  `candidate_parsed` logs `provider_finish_reason`.
- Content-array envelopes (`{"content": [...]}`) are deliberately NOT
  auto-unwrapped: no adapter produces them (verified in gemini.py /
  openai_compat.py), and they fail validation honestly.

**Reproduced in-process** with deterministic synthetic fixtures for all ten
shapes (direct/fenced/prose+fenced/prose-unfenced/double-encoded/truncated/
raw-newlines/content-wrapper/empty/prose-only) — exact live provider
response unavailable (no credentials in this workspace).

## 6B. Instrumentation-only phase (commit `a75d463`) — observe, do not fix

**Explicitly:** this phase changed NO parser behavior, NO semantic
interpretation, NO prompts, NO validation, NO scheduling/guardian/executor/
schema, NO provider selection. It adds two content-free `AI_TASK_TRACE`
records so the NEXT live reproduction of `candidate_invalid_json` is
conclusive. Synthetic-fixture evidence (§6A, §5) is NOT live provider
evidence and is never presented as such.

**Provider audit (source-verified, not assumed):**
- Chat-eligible providers registered by `ProviderFactory`: `gemini` plus
  13 `OpenAICompatProvider` subclasses (`openai`, `openrouter`, `cerebras`,
  `mistral`, `groq`, `zai`, `sambanova`, `nvidia`, `cohere`, `siliconflow`,
  `fireworks`, `nararouter`; `you` is web-search-only, `dummy` serves
  nothing). `ProviderManager.chat` routes active-first-then-scored.
- Extraction: Gemini joins `candidates[0].content.parts[*].text`;
  OpenAI-compat uses `choices[0].message.content`. Both return a plain
  text `ProviderResponse` — no adapter emits fences/envelopes itself.
- Metadata already exposed (reused, not re-invented): `model` +
  `finish_reason` on BOTH families (Gemini: `MAX_TOKENS`/`SAFETY`/`RECITATION`;
  OpenAI-compat: `length`/`content_filter`/`stop`); `http_status` +
  `failure_type` on failures. NOT exposed by any adapter: provider request
  id, content type. Reported as unavailable rather than invented.
- TaskInterpreter receives the response directly from
  `ProviderManager.chat(...)` and reads `response.text` + `response.metadata`.

**New trace fields (emitted in `backend/ai/task_interpreter.py`):**

1. `stage=raw_response_shape` — logged immediately after the provider
   response arrives, BEFORE any parsing/transformation:
   `provider model success text_type empty first_non_ws
   {object|array|fence|quote|other|empty} starts_fence contains_fence
   leading_prose trailing_prose has_control_chars object_span raw_len
   truncated finish_reason http_status failure_type`. Character boundaries
   are logged as CLASS categories only — never the actual characters.
2. `stage=candidate_parse_error category=candidate_invalid_json` — at the
   exact point where a `JSONDecodeError` becomes the parse-failure category:
   adds `provider model` to the existing `json_error line col pos raw_len
   truncated provider_finish_reason` and the structural flags
   `first_non_ws contains_fence leading_prose trailing_prose object_span`.

**Cases distinguishable from one live run (metadata only):** A empty ·
B plain JSON object · C fenced JSON · D prose+JSON · E JSON+prose ·
F prose+fenced JSON · G JSON string containing JSON (double-encoded,
`first_non_ws=quote`) · H raw control characters · I truncated/incomplete
(`truncated=True` from error position and/or provider `finish_reason`) ·
J JSON array · K non-JSON prose · L provider failure (success=false path)
· M valid JSON that later fails schema validation (`response_shape=object`
traces, never labeled a parse error).

**Tests (instrumentation-only, +11):** every tolerance-matrix case still
passes with instrumentation active (parser result unchanged); the shape
trace is verified content-free (never the request, `bio_set_text`, or
config values) across 9 synthetic shapes; the parse-error trace carries
provider/model/JSON metadata; schema-invalid JSON reaches schema
validation and is never mislabeled `candidate_invalid_json`; the
correlation-layer leak test stays green (stage named `raw_response_shape`
to avoid substring-colliding with the manager's whitelisted
`stage=provider_response`).

**What the next live run will settle:** reproduce the exact Persian
request once; the `raw_response_shape` + `candidate_parse_error` lines
identify the real provider response shape conclusively. Only then should
any further behavioral change be considered.

## 6C. Instrumentation-visibility investigation (commit `7fbaba3`)

**Starting state (verified, not assumed):** starting HEAD = `8acc986` ==
`origin/main` (`ls-remote` equal). All prior commits (`33727f3`, `c35f75f`,
`012f738`, `a75d463`, report `8acc986`) are ancestors of `origin/main`
(`git merge-base --is-ancestor`). `a75d463` was committed 2026-09-09
23:06 UTC; the user's log excerpt is from 2026-09-10 02:41 — i.e. AFTER
the instrumentation existed on the default branch.

**Control-flow finding (source-proven):** in
`TaskInterpreter.interpret` the flow is
`provider success gate → provider_result trace → raw = response.text →
_classify_response_structure + _log_response_shape_trace → empty check →
_load_candidate_json → except JSONDecodeError → candidate_parse_error
trace`. `_log_response_shape_trace` executes UNCONDITIONALLY before any
JSON parsing — the only bypass is `response.success=false` (a different,
already-logged path). Therefore, IF the live failure ran code including
`a75d463` at INFO-visible level, BOTH `raw_response_shape` AND
`candidate_parse_error` must be present in the logs. Their total absence,
while the `[failure category: candidate_invalid_json]` reply (from
`c35f75f`'s `_fail`, logging at WARNING) DID appear, is itself evidence.

**Logging-visibility analysis (source + installed-package verified):**
- Every line quoted from the supplied excerpt (`RUNTIME_HEARTBEAT`,
  `KEEPALIVE_OK`, `ASYNC_TASK_DUMP`, `heartbeat stale`) is emitted by
  `backend/runtime/tracer.py::trace()` at **WARNING** — not by an INFO
  logger. The excerpt demonstrably shows WARNING-level `backend.*`
  records; it contains NO INFO-level record of any kind.
- `main.py` bootstraps root at WARNING but raises `backend` to INFO.
- NOT the cause (checked and excluded): uvicorn's
  `uvicorn.Config(log_level="warning")` calls `dictConfig` on
  `LOGGING_CONFIG`, which has `disable_existing_loggers: False`, no `root`
  key, and only `uvicorn`/`uvicorn.error`/`uvicorn.access` logger entries —
  verified against the installed uvicorn 0.29.0 source (`config.py`
  `configure_logging`); it does NOT lower `backend`. `LOG_LEVEL` env is
  dead config (read into `config.py` cfg, never applied to logging).
- Therefore the honest conclusion: **INFO-level visibility in the
  production log stream is unproven** (the excerpt proves WARNING
  visibility). Rather than speculate further, the two decisive records
  were escalated to WARNING — the same level as the lines the excerpt
  provably contains. No INFO record was removed; tests capture both.

**Supplied Render log fields (semantics from source, no over-reading):**
`ai_active` / `ai_stage` / `ai_last_provider_s` come from
`backend/ai/diagnostics.py` and are updated ONLY by the Dispatcher's
conversation path (`engine/dispatcher.py` `_stage/_mark_success`). The
TaskInterpreter calls `ProviderManager.chat` directly and never touches
those markers — so `ai_active=0`, `ai_stage=-`, `ai_last_provider_s=-1.0`
say NOTHING about whether a task-creation AI request ran (and `-1.0` means
"no successful PROVIDER_REQUEST recorded since boot"). `Last command: 81.9s
ago` measures the last recognized bot command, not AI activity. These
fields can neither prove nor disprove that the failed request reached the
provider. Deployment identity (Render actually serving `a75d463`+) remains
unproven from the workspace.

**Evidence categories (kept separate):** 1) source-proven: control-flow
ordering, tracer/uvicorn/bootstrap logging facts, provider extraction; 2)
synthetic: all parser-tolerance and instrumentation tests; 3) user-supplied
Render: runtime healthy, WARNING lines visible, no AI_TASK_TRACE at all; 4)
actual live provider evidence: **none — actual provider response content
remains unobserved.**

**Trace fields available for the next reproduction (now WARNING-level):**
`raw_response_shape`: request_id, provider, model, success, text_type,
empty, first_non_ws{object|array|fence|quote|other|empty}, starts_fence,
contains_fence, leading_prose, trailing_prose, has_control_chars,
object_span, raw_len, truncated, finish_reason, http_status, failure_type ·
`candidate_parse_error`: adds category, json_error, line, col, pos. No raw
response, request, ai_instruction, keys, tokens, or arbitrary metadata
values are ever logged.

## 7. Architecture boundaries preserved

`RuntimeSupervisor` · `TaskScheduler` · `TaskExecutionCoordinator` ·
`Dispatcher` · `ProviderManager` · `ToolRegistry`/`ToolExecutor` (sole
execution authority) · Telethon · AI-as-reasoning-only. Execution path
unchanged: scheduler → coordinator → prepared/validated action →
ToolExecutor → bio tool → `bio_service._apply_profile` →
`guard_bio_mutation` → Telegram. Bio guardian unchanged (one successful
mutation per rolling 60s, concurrency-safe, success-only advancement).
No second scheduler/executor/event authority; no arbitrary AI RPC/SQL/shell/
filesystem/HTTP; no regex-based intent parsing; no quote database.

---

## 8. Database impact

**NO DATABASE / SCHEMA CHANGE.** The existing `ai_tasks`/`ai_task_occurrences`
two-table model represents everything; `ai_instruction` uses its existing
column.

---

## 9. Remaining limitations / blockers

1. **Live verification NOT PROVEN — and the live rejection RECURRED after
   `33727f3` was pushed** (user reproduction: identical generic message).
   Two live-state causes remain: (A) the deployed Render runtime predates
   the fix (deployment lag/failure — plausible: the reproduction ran
   immediately after push; Render auto-deploys on push only when the
   service is connected), or (B) the production provider returns
   null/schema-violating output despite the prompt (prompt contract is
   explicit but no provider is guaranteed compliant). The `c35f75f`
   failure-category suffix makes one new live run conclusive: no suffix →
   stale deployment; `candidate_invalid:null` / `candidate_invalid:object`
   / `candidate_invalid_json` → provider output compliance; `repository_failure`
   → persistence.
2. **Provider-output compliance** cannot be guaranteed for arbitrary models;
   the diagnostics + user-facing category now classify any non-compliant
   output, and the prompt contract is explicit.
3. **Stale deployment** possibility (A) cannot be excluded from this
   workspace; if the generic message persists after deploying `c35f75f`
   WITH the trace showing `candidate_parsed`, the issue is downstream.
4. Monthly/yearly/weekend triggers remain honestly unsupported; canonical-
   quote authenticity remains unverified by design.

---

## 10. Delivery record (verified)

| Item | Value |
|---|---|
| Implementation commits | `33727f3` · `c35f75f` · `012f738` · `a75d463` · `7fbaba3` (escalate boundary instrumentation to WARNING for log visibility) |
| Base commit (visibility phase) | `8acc986` (clean tree, `origin/main` equal, ancestry of all prior commits verified) |
| Base commit (this phase) | `fb76fc8` (clean tree, `origin/main` equal) |
| Report commits | `a3dbd1b` (report update) + `(final delivery record commit — see git log)` |
| Push | `git push origin main` (non-force fast-forward); verified via `fetch` + `rev-parse` + `ls-remote` |
| Verified remote HEAD | equals local HEAD post-push (authoritative `ls-remote`) |
| Working tree | Clean except the pre-existing untracked nested clone `telegram-self-bot/` (untouched) |
