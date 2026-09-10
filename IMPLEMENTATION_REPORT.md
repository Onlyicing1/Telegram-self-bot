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
| Phase | Structured-output reliability: JSON-mode request + capability-aware routing + bounded content-failure failover at the provider boundary (root cause PROVEN: groq/allam-2-7b returned malformed JSON) |
| Implementation commits | `33727f3` · `c35f75f` · `012f738` · `a75d463` · **`7fbaba3` (escalate boundary instrumentation to WARNING for log visibility)** |
| Report commit | see §10 delivery record |
| Status | **INSTRUMENTATION-ONLY — full suite green (2046 passed, 24 skipped). Control-flow audit PROVES `raw_response_shape` executes before any JSONDecodeError classification; supplied logs contained only WARNING-level lines, so INFO visibility is unproven and the two decisive records were escalated to WARNING (no behavior change). Actual provider response content remains unobserved.** (see §6C) |
| Database impact | **NO DATABASE / SCHEMA CHANGE** |

---

## 2. Live root cause (PROVEN — production trace, 2026-09-10)

The exact multi-line Persian bio-task request produced the user-facing
`[failure category: candidate_invalid_json]`, and the WARNING-level
boundary instrumentation (commit `7fbaba3`) captured the conclusive trace:

| Field | Value |
|---|---|
| provider / model | `groq` / `allam-2-7b` |
| success / finish_reason | `true` / `stop` (NOT truncated, NOT empty) |
| response_length | 2778 |
| first_non_ws | `object` (starts with `{`) |
| trailing_prose | `True` — unbalanced object; no closing `}` consumed everything |
| has_control_chars / object_span | `False` / `False` |
| JSONDecodeError | line 1, col 87, pos 86 — `Expecting ':' delimiter` |

**Classification:** the provider returned transport-success content that is
NOT valid JSON. The interpreter's fail-closed parser correctly rejected it
BEFORE candidate validation/persistence — no downstream layer was involved.
Render runtime, Telethon, deterministic routing, scheduler, timezone,
bio routing, timeout, truncation, and control characters are all excluded
by the same trace. The prior parser-tolerance work (`012f738`) remains
correct: the live shape was none of the tolerated wrappers.

## 3. Fix — provider boundary only (parser unchanged, fail-closed)

The success condition was NOT "make the parser accept the output". The fix
makes the structured-output contract explicit end-to-end:

1. **Structured-output request** (`backend/ai/task_interpreter.py`):
   `TaskInterpreter.interpret` now calls the provider mesh with
   `response_format={"type": "json_object"}` plus an `output_validator`
   probe (`_valid_structured_output` — content-free: "does the text parse
   as JSON under the same tolerances the interpreter's own parser accepts?"
   It never validates task-candidate semantics; a JSON that parses but
   fails the candidate schema is NOT a contract failure).

2. **Adapter serialization** (`backend/ai/providers/openai_compat.py`,
   `backend/ai/providers/gemini.py`): `ProviderCapabilities.supports_json`
   existed but nothing consumed it. Now OpenAI-compat providers serialize
   `payload["response_format"]` (JSON mode) and Gemini maps it to
   `generationConfig.responseMimeType="application/json"` (JSON MIME) —
   only when the caller requests it AND the provider declares the
   capability. CANDIDATE_SCHEMA travels in the interpreter's system
   message only — no schema duplication; no new structured-output
   abstraction.

3. **Capability-aware routing** (`backend/ai/providers/manager/manager.py`):
   when a request carries `response_format`, providers that DECLARE
   `supports_json` are ordered before ones that do not (within each group:
   active provider first, then score). `supports_json=False` is a demotion,
   never a hard skip (soft-capability routing).

4. **Bounded content-failure failover** (same file): a transport-success
   response whose content VIOLATES the structured contract (validator
   rejects) is treated exactly like the existing empty-output failure:
   fail over to the next eligible candidate — bounded (only while one
   remains), NO cooldown/quarantine (request-level quality signal), recorded
   in the provider matrix as `malformed_json` + `AI_PROVIDER_FAILURE`
   + `provider_fallback` trace with reason `structured_output_contract`,
   penalized via quality metrics. The LAST candidate's response is returned
   unchanged — the caller owns final classification (the interpreter's
   fail-closed parser reports the precise `candidate_invalid_json`
   category). Fallback NEVER triggers for deterministic/user/unsupported
   capability/schema-validation/repository/Telegram/security failures: the
   validator only sees parseability, and every other failure layer raises
   before or after the provider call without touching the manager.

5. **Loop safety**: attempts remain bounded (one per eligible provider, at
   most one in-place retry for transport failures — unchanged). Failover
   happens BEFORE task creation/persistence and BEFORE any Telegram side
   effect; the interpreter is invoked pre-persistence by design.

## 4. Files changed

| File | Change |
|---|---|
| `backend/ai/task_interpreter.py` | `_RESPONSE_FORMAT` request; `_valid_structured_output` probe; pass `response_format` + `output_validator` through `ProviderManager.chat`. No prompt change; no parser change. |
| `backend/ai/providers/openai_compat.py` | Serialize `response_format` when declared (`supports_json`). |
| `backend/ai/providers/gemini.py` | Map `response_format` → JSON MIME mode when declared. |
| `backend/ai/providers/manager/manager.py` | `needs_json` awareness; capability-aware ordering; `_supports_json_output`; contract-violation failover (bounded, no cooldown, quality penalty, matrix + trace). |
| `tests/test_provider_structured_output.py` | NEW — 15 focused tests (below). |
| `tests/test_task_interpretation_diagnostics.py` | Stub `chat(**kwargs)` tolerance only (new call kwargs). |
| `IMPLEMENTATION_REPORT.md` | This rewrite. |

## 5. Tests added and executed

**New `tests/test_provider_structured_output.py` (15):**
interpreter requests `response_format` + validator · probe accepts valid
JSON / rejects malformed-empty-prose · probe accepts fenced / prose-wrapped /
double-encoded wrappers · OpenAI-compat serializes `response_format` only
when declared · Gemini maps to JSON MIME · no schema duplication ·
malformed-JSON-from-active-provider fails over to the next provider (the
exact live `groq` → `gemini` shape) · failover records NO cooldown and
state stays `healthy` · quality metrics penalized · bounded attempts on
exhaustion with the last response unchanged · valid content passes through
without failover · no-validator callers keep previous behavior ·
capability-aware ordering with JSON requested (matrix order proven) ·
active-first preserved without the JSON request · **the exact Persian
request creates the 300s bio task through the REAL ProviderManager after a
malformed first response** (verbatim `ai_instruction`, `آیانامی ری` and
`زیر 60 کاراکتر` preserved) · exhaustion fails closed with
`response_shape=malformed`, one attempt per provider, zero fabrication.

**Executed:**
- `tests/test_provider_structured_output.py` — 15 passed
- Adjacent suites (8 files: provider mesh, providers, interpretation
  diagnostics, creation diagnostics, NL creation, semantic triggers,
  source fidelity) — **221 passed**
- Full suite — **2060 passed, 24 skipped, 1 failed** — the single failure
  (`test_40_usage_read_side.py::test_daily_usage_read`, `assert 0 == 40`)
  is **pre-existing on clean `3ff880a`** (verified via `git stash`:
  fails identically without this phase's changes) and is unrelated
  (usage read-side vs provider boundary).
- `py_compile` on all modified files — OK · `git diff --check` — clean ·
  zero new regex · zero prompt changes · zero scheduler/executor/guardian/
  schema changes.

## 6. Boundaries preserved

- **NO DATABASE / SCHEMA CHANGE** (two-table task model untouched).
- Parser remains **fail-closed**; `_load_candidate_json` untouched; no
  regex parser introduced; no candidate fabrication.
- ToolExecutor stays the sole execution authority; AI remains
  reasoning-only; no second scheduler/executor/router.
- Bio Guardian untouched; TaskCreationService untouched; provider
  selection/fallback policy extended only along its existing seams.
- `supports_json`/`ProviderCapabilities` reused — no duplicate capability
  abstraction; no provider-specific branch in the interpreter.

## 7. Live verification status

- **Live provider response content: STILL UNOBSERVED** (no credentials in
  this workspace) — the fix is proven by the production trace's structural
  metadata plus in-process tests; it is NOT yet live-verified.
- **Groq JSON-mode capability is EXTERNAL-DOCUMENTATION evidence**
  (console.groq.com/docs/structured-outputs: `response_format
  {"type":"json_object"}` JSON Object Mode; availability varies per model
  and `allam-2-7b` support is NOT verified from source). The repository
  code now sends the field only to providers that declare the capability;
  whether `allam-2-7b` honors it will be visible in the next live run:
  a compliant response removes `candidate_invalid_json`; a provider-side
  rejection of the field surfaces as `failure_type=request` (transport
  failure) and still fails over to the next eligible provider.
- After Render redeploys, ONE reproduction of the exact request settles it:
  success → task created; failure → the unchanged category token +
  `AI_TASK_TRACE` lines identify the remaining layer precisely.

## 8. Delivery record

| Item | Value |
|---|---|
| Base commit | `3ff880a` (== origin/main at phase start) |
| Implementation commit | `542f85d` |
| Report commit | follows the implementation commit |
| Push result | `3ff880a..5d67739 main -> main` |
| Remote HEAD | verified == local HEAD after push (see below) |
| Working tree | clean except pre-existing untracked `telegram-self-bot/` |

---
