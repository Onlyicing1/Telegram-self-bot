# Gemini Thought-Signature Loss During Tool Continuation — Implementation Report

Verified against `main` at `2d9cbb634c1d686abf58ef7aa02637980bb045ab`
(production-deployed). This report replaces the previous one; it is not an
append.

---

## 1. Status at a glance

| Dimension | State |
|---|---|
| Source-level fix | **Implemented** (5 files + 1 new test file) |
| Automated verification | **Green** — 15 focused tests in the new file, 87 in the focused group, full suite 7108 passed / 1 pre-existing unrelated failure |
| Compilation | `py_compile` on every edited file: OK |
| Diff hygiene | `git diff --check`: clean |
| Deployed to Render | **No** (commit pushed to `origin/main`; deployment not observed) |
| Live Gemini continuation verified | **No** — no production request has exercised this code yet |

The production 400 is explained by the source defect below, but saying the
issue is *fixed in production* is not supported by this work: it is fixed in
source and verified with mocked HTTP only.

---

## 2. Confirmed production root cause

Production (Nov 7, 2026) logged repeated Gemini failures during real tool
continuations for `web_search`, `delete` and `list_saves`, plus the synthetic
`diag_probe`:

```
400 INVALID_ARGUMENT: Function call is missing a thought_signature in
functionCall parts. This is required for tools to work correctly...
```

The tool results had already been received, executed and returned, so the
failure occurred exactly at the *continuation request*, never at the first
turn.

Cause, from source: Google returns `thoughtSignature` as a **sibling field of**
`functionCall` inside the same `Part`, and requires it back byte-for-byte in
that same part. The response parser built a new dict containing only
`id`/`name`/`arguments`, so the signature was dropped; the continuation
serializer then emitted a bare `functionCall` part. Every Gemini tool turn
therefore replayed an unsigned call.

## 3. Adapter-specific facts established from source

- **Gemini** (`backend/ai/providers/gemini.py`): parses
  `candidates[0].content.parts[*]`; `thoughtSignature` sits at **part level**,
  beside the `functionCall` member — not inside `functionCall`. Google attaches
  it only to the **first** `functionCall` part of a parallel batch; later calls
  legitimately carry none.
- **OpenAI-compatible** (`backend/ai/providers/openai_compat.py`): sends the
  caller's `messages` list verbatim in the request body. It has no concept of a
  thought signature, so any Gemini-only key placed in a shared message would
  be transmitted to OpenAI/Groq/NaraRouter/etc. — including on ProviderManager
  fallback.

---

## 4. Implementation changes

### `backend/ai/providers/gemini.py`

- Parsing: each `functionCall` part now also carries the part-level
  `thoughtSignature` on the normalized call as `thought_signature`, copied
  verbatim (never decoded, re-encoded, trimmed or normalized). Call order and
  existing `id`s are untouched.
- Serialization: an assistant tool call with `thought_signature` emits the
  part as `{"functionCall": {...}, "thoughtSignature": <original>}` — the exact
  field Google validates.
- Missing signature: nothing is fabricated or reused. If the **first** call of
  a turn has none, one WARNING `GEMINI_THOUGHT_SIGNATURE_MISSING calls=N
  reason=provider_returned_no_signature` is emitted (count only — no values, no
  content). Later parallel calls without a signature are normal and stay
  silent.
- New class attribute `REQUIRES_NATIVE_TOOL_SIGNATURE = True` declares the
  protocol requirement to callers that build their own calls.

### `backend/ai/engine/dispatcher.py`

- `_build_continuation_messages()` copies `thought_signature` onto the
  assistant `tool_calls` entry that produced it (only when present). The
  OpenAI-shaped envelope (`id`/`type`/`function`) is otherwise byte-identical,
  so assistants built from other providers are unchanged.

### `backend/ai/providers/openai_compat.py`

- New `_openai_messages()` strips provider-private per-call fields
  (`thought_signature`) at the OpenAI-compatible boundary; every other message
  key passes through untouched. This covers both direct requests and a
  fallback provider continuing a Gemini-started turn.

### `backend/ai/providers/base/contract.py`

- `ProviderResponse` documents the normalized call shape and the optional,
  opaque, provider-private `thought_signature`, with the rule that adapters
  which do not understand it must not serialize it.

### `backend/ai/continuation_diagnostic.py`

- A provider that declares `REQUIRES_NATIVE_TOOL_SIGNATURE` is no longer sent a
  synthetic function-call continuation. The probe returns
  `UNSUPPORTED` / `native_thought_signature_required` without issuing a
  request.
- Defensive: any probe response whose bounded provider error message mentions
  `thought_signature` is classified the same way instead of
  `CONTINUATION_REJECTED`.
- Everything else about the diagnostic is unchanged: cooldown, single-flight,
  trigger categories, pair selection, `max_tokens`/`temperature` reporting,
  truncation classification and the synthetic conversation for providers whose
  protocol can be tested with it.

---

## 5. Files changed

| File | Change |
|---|---|
| `backend/ai/providers/gemini.py` | parse + replay signature, missing-signature warning, protocol declaration |
| `backend/ai/engine/dispatcher.py` | carry the signature into continuation messages |
| `backend/ai/providers/openai_compat.py` | strip provider-private call fields at the OpenAI boundary |
| `backend/ai/providers/base/contract.py` | document the normalized call contract |
| `backend/ai/continuation_diagnostic.py` | honest `UNSUPPORTED` classification for signature-only protocols |
| `tests/test_gemini_thought_signature.py` | new: 15 regression tests |
| `IMPLEMENTATION_REPORT.md` | replaced with this report |

Not touched: `ToolExecutor`, `ToolRegistry`, `ProviderManager`, fallback
policy, scheduler, task system, `DATABASE_ARCHITECTURE.md`, production provider
configuration.

---

## 6. Behavior: present vs. missing signature

- **Signature present** → replayed verbatim in its own part; the continuation
  is sent exactly as Google requires.
- **Parallel batch** → only the first call carries one (as received); the rest
  stay unsigned, in original order, with their original ids.
- **Signature absent on the first call** → no signature is invented, no other
  call's signature is borrowed; the request is still sent, so Google's own
  validation decides the outcome and the failure is reported honestly as a
  provider error. The warning above makes the cause explicit in logs.

---

## 7. Tests and results

New file `tests/test_gemini_thought_signature.py` — 15 tests covering the ten
required cases: parsing a signature, byte-for-byte preservation, dispatcher
propagation, exact replay in the outgoing request, parallel-call association
and order, distinct signatures, no fabrication/reuse, OpenAI-compatible
isolation, fallback isolation, Gemini probe classified `UNSUPPORTED`
(`native_thought_signature_required`, no request sent), defensive API-side
classification, plus the intact role/grouping/id behavior (one `user` Content
for function responses, call order preserved, ids matched).

| Run | Command | Result |
|---|---|---|
| New file | `pytest -q tests/test_gemini_thought_signature.py` (in focused group) | 15 passed |
| Focused group | `test_gemini_thought_signature.py`, `test_tool_calling_root_cause_fixes.py`, `test_10_tool_calls.py`, `test_continuation_diagnostic.py`, `test_provider_tool_boundary.py` | **87 passed** (6.43s) |
| Full suite | `pytest -q` | **7108 passed, 27 skipped, 1 failed** (141.96s) |

The single full-suite failure is the known, unrelated baseline:
`tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`
(`assert 0 == 1`, "interval advancement exceeded bound"). It is not caused by
this change and the suite is **not** fully green.

Compilation: `py_compile` on all five edited modules plus the new test file →
OK. `git diff --check` → clean. No existing assertion was weakened; the only
edited test-visible behavior is the new diagnostic classification, and the
existing diagnostic tests (37) still pass unchanged.

---

## 8. Not verified

- **No live Gemini continuation** has been performed since the change. Mocked
  HTTP proves the shape of the outbound request, not that Google accepts it.
- **No production log evidence** yet for either the continuation success or the
  new `result=UNSUPPORTED category=native_thought_signature_required` probe
  line.
- **Deployment status** after this commit is unknown; Render must deploy it
  before any live check is meaningful.
- The old behavior for ProviderManager fallback, health, cooldown and
  quarantine is preserved by construction but was not re-exercised against a
  live provider.

## 9. Is another live Telegram test required?

**Yes.** One real owner request that makes Gemini call a tool (e.g. `web_search`,
`delete` or `list_saves`) on a thinking model is needed. Success looks like: no
`400 ... missing a thought_signature` line, and the continuation round returns
normally. Additionally, the next continuation failure should log
`PROVIDER_CONTINUATION_DIAGNOSTIC ... result=UNSUPPORTED
category=native_thought_signature_required` for a Gemini pair instead of a
misleading `CONTINUATION_REJECTED`.
