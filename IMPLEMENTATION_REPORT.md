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
| Phase | Diagnose and fix the STILL-OPEN production rejection of the exact multi-line Persian bio-task request; failure-layer diagnostics |
| Implementation commits | `33727f3` (`fix: remove timezone contradiction and bio-read hijack on the live request`) · `c35f75f` (`fix: make task-creation rejection carry the bounded failure category`) |
| Report commit | see §10 delivery record |
| Status | **CODE-COMPLETE — full suite green (2021 passed, 24 skipped). LIVE Telegram/provider verification: NOT PROVEN in this workspace** (no session credentials); deterministic layers proven compliant; the live rejection persisted after `33727f3` — the remaining causes are (A) stale deployed runtime or (B) provider-output compliance, and the `c35f75f` failure-category suffix now makes ONE live reproduction conclusive (see §6) |
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
| `pytest tests/test_task_interpretation_diagnostics.py -q` | **18 passed** (post-`c35f75f`: suffix contract pinned for null / malformed / schema-violation / unsupported / success) |
| Adjacent task suites (6 files, post-`c35f75f`) | **196 passed** |
| `pytest tests/ -q` (full suite, post-`c35f75f`) | **2021 passed, 24 skipped** in 63.53s |
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
| Implementation commits | `33727f3` (`fix: remove timezone contradiction and bio-read hijack on the live request`) · `c35f75f` (`fix: make task-creation rejection carry the bounded failure category`) |
| Base commit (this phase) | `fb76fc8` (clean tree, `origin/main` equal) |
| Report commit | `(filled after creation — see git log)` |
| Push | `git push origin main` (non-force fast-forward); verified via `fetch` + `rev-parse` + `ls-remote` |
| Verified remote HEAD | equals local HEAD post-push (authoritative `ls-remote`) |
| Working tree | Clean except the pre-existing untracked nested clone `telegram-self-bot/` (untouched) |
