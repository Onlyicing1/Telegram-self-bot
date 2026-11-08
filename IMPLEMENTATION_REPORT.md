# IMPLEMENTATION_REPORT.md — AI-scheduled bio task: preparation failure diagnosis

> **Current status report.** This revision replaces the previous report in full.
> It covers only the work for the recurring `Update Bio` task failure (task 22),
> implemented on top of `c49b635` (the commit Render is running). The
> intervening delivered work is listed in §0 so it is not lost, not restated.

| Item | Value |
|---|---|
| Task | diagnose the recurring AI-scheduled `Update Bio` failure (task 22) and fix the verified defect |
| Base commit | **`c49b635`** (`chore: reduce redundant AI success-path logging`) — the production commit; this work is on top of it |
| Task definition | **untouched** (id 22, interval 900 s, `Asia/Tehran`, `bio_set_text`, same `ai_instruction`); no Supabase/SQL access, no real bio mutation |
| New dependency | none |
| Architecture | unchanged: `TaskScheduler → TaskExecutionCoordinator → AIActionPreparator → PreparationPolicy → ToolExecutor → bio_set_text → bio_service` |
| Execution authority | unchanged: `ToolExecutor` is still the only caller of `tool.execute()` |
| Follow-up base | **`19594dd`** — precise content-policy violation codes appended in §10 |

## 0. Intervening delivered work (unchanged by this task)

Between `0d07dc1` and `c49b635`: `f835c4d` auto-diagnose provider continuation
failures, `1900016` stop FloodWait-driven reconnects, `e5727c0` bound the
continuation probe's output budget, `2d9cbb6` report probe budget and flag
truncated continuations, `9406c7b` preserve Gemini thought signatures across
tool continuations, `c49b635` reduce redundant AI success-path logging. All of
it is provider/continuation/logging side; `backend/ai/task_execution.py` and
`backend/ai/preparation_policy.py` have not changed since 2026-09-28.

---

## 1. Production evidence examined

| Fact | Value |
|---|---|
| Occurrence | `22058`, scheduled 13:37:51 UTC, started 13:37:52, finished 13:38:07, `status=failed` |
| Counts | `action_count=1`, `successful_action_count=0` |
| Classification | `error_class=unclassified` |
| Metadata | `result_metadata` and `preparation_metadata` both **empty** |
| Provider | three Gemini selection/attempt lines at 13:37:52, 13:38:05, 13:38:06 (~15 s total) |
| History | 945 `unclassified` failures; last success 2026-11-06 09:07 UTC |
| Control | task 25 (static, no `ai_instruction`) succeeds every 5 minutes |

Three provider attempts match `MAX_PREPARATION_ATTEMPTS = 3` exactly, and 15 s
is far inside `MAX_PREPARATION_SECONDS = 45`: one preparation was attempted,
rejected three times, and the occurrence failed. Empty `preparation_metadata`
is expected on failure (it is written only on a successful transition), so it
does not by itself identify the stage.

---

## 2. Verified root cause

**The occurrence is a preparation failure whose reason is destroyed before it is
persisted.** Reproduced end to end with the production instruction (§5):

* `AIActionPreparator`/`_prepare_calls` raise `TaskPreparationError` for every
  distinct failure — provider error, empty output, invalid JSON, malformed
  action structure, content-policy rejection.
* `retry.py::classify_failure` sees only a `ValueError`-derived message that
  contains none of its keywords (`timeout`/`temporar`/`cancel`/`rate limit`) and
  returns `("unknown", False, "unclassified")`.
* `handle_failure` therefore persists `error_class="unclassified"` with no
  stage, category or attempt count, and `_fail` writes `failed` with empty
  `result_metadata`.
* The three-attempt loop retried the identical failure (it stopped early only
  for the substrings `"tool name"`/`"number of actions"`), spending the whole
  preparation budget on the same unidentifiable outcome — exactly the
  production signature (3 provider attempts, failed, `action_count=1`,
  `successful_action_count=0`, empty metadata).

The verified defect is therefore the **diagnosis**: production cannot
distinguish "the provider never answered" from "the model answered prose" or
"the dialogue missed the Shikamaru Nara attribution" — three different fixes.

### 2.1 What the deterministic reproduction proved about the stages

Against the real `AIActionPreparator` with the exact stored instruction
(`update my bio, with a randomly generated dialogue, in English, at most 70
characters, from Shikamaru Nara`):

* The derived policy is **satisfiable**: `language=english`, `max_length=70`
  measured on the *visible* line (after the attribution label is removed),
  `source="Shikamaru Nara"`, `show_source=False`. A compliant
  `Shikamaru Nara: …` line — including a 70-character visible line — is accepted
  and prepared. There is **no policy bug making this instruction impossible**.
* Rejected (previously retried 3×, silently) for: missing/short-form
  attribution, Persian/Arabic/Han letters, visible length > 70, prose around the
  JSON, a wrong/extra top-level key, a wrong action count, a swapped tool name.
* **Second verified defect:** `{"arguments": {"content": …}}` for `bio_set_text`
  was accepted (`content` is a policy content field), so the tool would have run
  with `text=""` — an empty bio. Prepared arguments were never checked against
  the task's own declared arguments.

### 2.2 Ruled out (with evidence, not assumption)

* **Not the provider adapter, not the thought-signature/continuation work**: the
  preparation request carries `tools=[]`; no function call or continuation is
  involved, and Gemini's text response is parsed normally.
* **Not a timeout**: 15 s < 45 s.
* **Not a bio-mutation client problem**: `_apply_profile` uses
  `profile_scheduler._client`, set by `RuntimeSupervisor` at startup
  (`backend/runtime/supervisor.py:928`) regardless of `BIO_UPDATE_ENABLED`.
* **Not the deterministic policy alone** (§2.1): a valid dialogue passes.
* **Not the scheduler**: task 25 runs on the same scheduler; task 22
  occurrences are created and claimed every 15 minutes.
* **Not `bio_set_text` executing**: on a failed preparation the executor is never
  invoked (§5 asserts `calls == []`), consistent with
  `successful_action_count=0` and empty `result_metadata`.

### 2.3 Correlation with provider-routing history

Provider/model routing changed on 2026-11-04…11-06 and again in the commits
listed in §0, immediately before/after the last success (2026-11-06 09:07 UTC);
`preparation_policy.py` and `task_execution.py` have not changed since
2026-09-28. The timing is consistent with a provider/model-routing regression
(e.g. a selected or discovery-derived model id the API rejects, which fails
fast — matching the 1-second second/third attempt), but this **cannot be
confirmed** without the production log for occurrence 22058. That is precisely
what the fix below makes possible on the next occurrence.

---

## 3. Implemented changes

`backend/ai/task_execution.py` only (plus tests). No new module, executor,
scheduler, AI entry point or Telegram path.

### 3.1 Structured preparation failure classification

`TaskPreparationError` now carries bounded, content-free diagnostics:
`category`, `stage`, `provider_origin`, `provider_finish_reason`, `attempts`.
`PREPARATION_FAILURE_CATEGORIES` bounds the vocabulary; any unknown value falls
back to `preparation_failed` (never `unclassified`).

Every raise site in `AIActionPreparator.prepare` sets its exact stage:

| Stage | Category | Trigger |
|---|---|---|
| `provider_request` | `provider_failure` | provider raised, or returned `success=False` (origin and `finish_reason` preserved) |
| `provider_response` | `empty_response` | transport success, empty text (e.g. blocked/truncated candidate) |
| `parse` | `invalid_json` | response is not JSON or JSON is wrapped in prose |
| `structure` | `invalid_action_structure` | not `{"actions": …}`, wrong count, non-object action, swapped tool name, renamed/missing declared argument |
| `content_policy` | `content_policy_rejected` | language, visible length, attribution, quote/garbage policy |

`_prepare_calls`/`prepare_validated` now stop early on
`invalid_action_structure` (regeneration cannot fix it) instead of matching
substrings; the same 3-attempt budget is preserved for content/parse/provider
failures, and the exhausted error carries the last category/stage plus the true
attempt count.

`handle_failure` persists the diagnosis: `error_class` is the category, plus
`failure_stage`, `preparation_attempts` and — when the provider supplied them —
`provider_failure_origin` and `provider_finish_reason`. It also logs one
`TASK_PREPARATION_FAILED task_id=… category=… stage=… attempts=… origin=…` line,
so the Render log and the occurrence row agree. `prepare_ahead` logs the same
fields on `TASK_PREPARE_AHEAD_FAILED`.

**Safety:** only bounded enum-like words are stored. Generated text, prompts,
provider messages, credentials, ids and destinations are never persisted or
logged; `_bounded_provider_diagnostic` normalises to `[a-z0-9_]`, caps at 32
characters, and origins are restricted to a fixed set.

### 3.2 Declared-argument check

A prepared action must fill every argument its task's own action template
declares. The model may supply values; it may not rename or drop a declared
argument (`{"content": …}` for `bio_set_text` is now
`invalid_action_structure` instead of executing an empty bio). Extra keys are
still tolerated, and the tool name is still fixed by the task definition.

### 3.3 Deliberately unchanged

Task definition (id 22) and its persisted data, `bio_set_text`'s name and
contract, the preparation prompt and validator boundary, `PreparationPolicy`,
`present_calls` (label-free presentation), `ToolExecutor`, `classify_failure`
for every non-preparation error, scheduler/occurrence/retry/duplicate guards,
`DATABASE_ARCHITECTURE.md`.

---

## 4. Persisted failure contract

| Case | `error_class` | Extra fields |
|---|---|---|
| Provider down / rejected | `provider_failure` | `failure_stage=provider_request`, `provider_failure_origin` (e.g. `rate_limit`, `model_lookup`, `authentication`), `provider_finish_reason` |
| Empty model output | `empty_response` | `failure_stage=provider_response`, `provider_finish_reason` (e.g. `max_tokens`, `safety`) |
| Prose / fenced garbage | `invalid_json` | `failure_stage=parse`, `preparation_attempts=3` |
| Wrong shape / tool / arguments | `invalid_action_structure` | `failure_stage=structure`, `preparation_attempts=1` |
| Policy violation | `content_policy_rejected` | `failure_stage=content_policy`, `preparation_attempts=3` |
| Provider timeout | `timeout` (unchanged) | occurrence `retry_pending`, `attempt+1` |
| Any other preparation error | `preparation_failed` | `failure_stage=preparation` |

---

## 5. Tests and exact results

New file `tests/test_preparation_failure_diagnosis.py` (15 tests) drives the real
`TaskExecutionCoordinator`, `AIActionPreparator`, `PreparationPolicy` and
`ToolExecutor` with a scripted provider and the production instruction:

* valid Shikamaru dialogue → prepared, then **exactly one** `bio_set_text`
  execution with the label-free visible text
  (`[("bio_set_text", {"text": "What a drag... I'd rather watch the clouds."})]`),
  attributed line kept in the audit record, provider called with `tools=[]`;
* English-only (Persian rejected), visible-length boundary (71 rejected, 70
  accepted and executed), attribution required (bare line and short form
  rejected);
* prose-wrapped JSON → `invalid_json`; swapped tool, empty action list, renamed
  argument and an extra top-level key → `invalid_action_structure` with **one**
  provider call (no wasted regeneration);
* provider failure → `provider_failure` + `provider_failure_origin=model_lookup`
  + 3 bounded attempts; empty text → `empty_response` +
  `provider_finish_reason=max_tokens`; provider `TimeoutError` → `retry_pending`,
  `attempt=2` (unchanged contract);
* the persisted failure record never contains the dialogue, the source name or
  the instruction, and `error_class` is never `unclassified`;
* the fixed `bio_set_text` name survives a model that tries to swap it.

`tests/test_task_ai_preparation.py` was corrected in one place: the old test
pinned `error_class == "unclassified"` for a preparation failure — the defect
itself. It now asserts `preparation_failed` and
`failure_stage == "preparation"`. No assertion was removed or weakened.

| Check | Command | Result |
|---|---|---|
| New + preparation suites | `pytest tests/test_preparation_failure_diagnosis.py tests/test_task_ai_preparation.py -q` | **28 passed** |
| Broader affected suites (preparation, prepare-ahead, execution, conditional branches, retry, bio guardian, bio/username, policy) | `pytest <9 files> -q` on `c49b635` | **203 passed** |
| Full suite | `pytest -q` on `c49b635` | **7125 passed, 1 failed, 27 skipped** (138.2 s) |
| Compilation | `py_compile` on `backend/ai/task_execution.py` + both test files | exit `0` |
| Whitespace | `git diff --check` | clean (exit `0`) |
| Diff review | full `git diff` of the runtime change | reviewed; no scope drift, no second executor, no content in diagnostics |

The single failure is
`tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`
("interval advancement exceeded bound"), **pre-existing and unrelated**: it
fails identically on the untouched base tree, and no scheduler code was
modified.

---

## 6. Production evidence vs. this change

The recorded signature (3 provider attempts, `failed`, `action_count=1`,
`successful_action_count=0`, empty metadata, ~15 s) is fully explained by the
verified defect: one preparation, rejected three times, classified as
`unclassified`. Nothing in the code makes the policy itself unsatisfiable for
this instruction (§2.1), so the remaining question — which of the five stages
the provider/model actually failed — is now answered by the occurrence row
instead of being erased.

---

## 7. Not verified live

* **No production access.** No Render log, Supabase row, Telegram session or
  deployment exists in this workspace; occurrence 22058 was not re-read and no
  provider request was made.
* **The failing stage for occurrence 22058 remains unidentified** — that is the
  defect this change fixes, not something it can recover retroactively.
* **No real bio mutation was performed** (explicitly out of scope).
* If the failure is provider-side, its exact cause is not diagnosed here; the
  persisted `provider_failure_origin` will name it (`model_lookup`,
  `rate_limit`, `authentication`, …).

---

## 8. End-to-end confirmation still required

**The task is not confirmed fixed.** No real Telegram bio change has been
observed, and no scheduled occurrence has yet prepared, executed `bio_set_text`
and changed the profile. Confirmation needs one live 15-minute occurrence:

1. `status=succeeded`, `successful_action_count=1`, and
   `preparation_metadata.action.name == "bio_set_text"`;
2. if it fails again, the occurrence row now names the stage —
   `provider_failure` (with an origin), `invalid_json`, `empty_response`,
   `invalid_action_structure` or `content_policy_rejected` — which identifies
   the remaining fix directly;
3. the Telegram profile `about` actually changes. `bio_set_text` sets the bio
   `{text}` value, so the persisted bio template must contain `{text}` for the
   change to be visible; that is owner configuration and was deliberately not
   modified here.

If the next occurrence reports `content_policy_rejected`, the generator is
ignoring the enforced attribution/length contract; if it reports
`provider_failure` with `origin=model_lookup`, the configured model id is the
problem — a configuration fix, not a code one.

---

## 9. What did not change

Scheduler claiming, occurrence persistence and idempotency, retry/backoff and
`MAX_ATTEMPTS`, duplicate-execution guards, prepare-ahead, action chains,
conditions, waits, questions, the preparation prompt/JSON contract, the
deterministic content policy (language/length/attribution; nothing truncated or
fabricated), the fixed action name `bio_set_text`, `ToolExecutor` as sole
execution authority, result delivery, runtime supervision/heartbeat/recovery,
and the database schema.

---

## 10. Follow-up: the exact content-policy rule is now recorded

> Appended on top of `19594dd` (the deployed classification commit). Nothing in
> §1–§9 is changed or restated; this section covers only the violation-code
> diagnostics.

### 10.1 The remaining gap

Post-deployment occurrences `22074` and `22078` now correctly say
`error_class=content_policy_rejected`, `failure_stage=content_policy`, three
preparation attempts, zero successful actions. That is the right *category*
but not the right *rule*: `PreparationPolicyError` carried only an English
message, and `TaskPreparationError` kept `category`/`stage` only, so the
persisted record could not distinguish a missed 70-character limit from a
language violation, a missing Shikamaru attribution, an empty output or a
non-string argument. The messages were also deliberately not persisted (they
are human text, not a stable contract), so the specific rule was lost.

### 10.2 Implemented

* `backend/ai/preparation_policy.py` — new closed vocabulary
  `POLICY_VIOLATION_CODES` (+ `DEFAULT_VIOLATION_CODE =
  "other_policy_violation"`). `PreparationPolicyError` now requires a bounded
  `code`; every deterministic raise site supplies one, so the rejection knows
  its rule while the message stays human-readable and content-free.
* `backend/ai/task_execution.py` — `TaskPreparationError` accepts
  `policy_violation` (only a vocabulary member survives; anything else is
  dropped, never persisted). Every wrapping site (`AIActionPreparator.prepare`,
  `prepare_validated`, the coordinator's `_prepare_calls` and
  `_enforce_content_policy`) forwards the code, and the aggregate
  "failed after N attempts" error re-raises the last specific code.
* `handle_failure` persists it as `error_metadata["policy_violation"]` and adds
  `violation=<code>` to the existing `TASK_PREPARATION_FAILED` warning;
  `TASK_PREPARE_AHEAD_FAILED` gets the same field. The broad
  `error_class=content_policy_rejected` and `failure_stage=content_policy`
  remain unchanged for compatibility.

| Code | Meaning |
|---|---|
| `attribution_missing` | no speaker label / no separator after the requested source (e.g. a bare line) |
| `attribution_mismatch` | a speaker label exists but names another character, or a short form of the source |
| `language_mismatch` | the visible letters do not satisfy the instruction's named language (including "no letters") |
| `visible_length_exceeded` | an "at most N" contract was exceeded (71 visible chars for `N=70`) |
| `visible_length_mismatch` | an "exactly N" contract was missed in either direction |
| `empty_content` | empty/whitespace-only content, or a label with no dialogue line |
| `invalid_content_type` | a non-string value, a JSON payload, or a provider failure payload |
| `unverifiable_exact_quote` | an explicit exact-canonical-quote request (no trusted corpus/verifier) |
| `other_policy_violation` | fallback for an unknown/missing code — never an arbitrary message |

Attribution is the only place with two codes, and it is decided
deterministically: text that opens with a `Name:`-style label is
`attribution_mismatch` when the name is not the full requested source;
text that never attempts a label is `attribution_missing`.

### 10.3 Privacy guarantee

The code is a member of a closed `frozenset`. Generated dialogue, the Bio text,
the attributed line, prompts, conversation messages, provider payloads,
credentials and destinations never enter it; the existing content-free
messages (lengths, language names, the instruction-derived source name) are
unchanged, and no new field carries text. `policy_violation` is omitted
entirely for non-policy failures.

### 10.4 Tests

New `tests/test_preparation_policy_codes.py` pins the vocabulary and every
mapping (compliant Shikamaru line passes; 70 passes / 71 exceeds; exact-N
mismatch; missing vs. wrong vs. short attribution; language, empty, non-string,
JSON/failure payload, exact-quote, source-without-content-field; unknown-code
fallback; the message never contains the generated text).

`tests/test_preparation_failure_diagnosis.py` gained five end-to-end tests
through the real coordinator/preparator/policy/ToolExecutor proving the codes
reach `error_metadata` for `visible_length_exceeded`, `attribution_missing`,
`attribution_mismatch`, `language_mismatch`, `visible_length_mismatch` and
`empty_content`, that the warning log carries the code and no generated text,
and its safety allow-list was extended with the one new field. Existing
retry/failure-persistence, exactly-once `bio_set_text` and no-execution-on-
rejection assertions are unchanged.

| Check | Command | Result |
|---|---|---|
| Focused new + diagnosis suites | `pytest tests/test_preparation_policy_codes.py tests/test_preparation_failure_diagnosis.py -q` | **37 passed** |
| Affected suites (policy, source policy, preparation, execution, prepare-ahead, contract) | `pytest <7 files> -q` | **103 passed** |
| Full suite | `pytest tests -q` | **7147 passed, 27 skipped, 1 failed** (137.69 s) |
| Compilation | `py_compile` on both modules + both test files | exit `0` |
| Whitespace | `git diff --check` | clean |

The single failure is the same pre-existing
`tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`
("interval advancement exceeded bound"); no scheduler file is touched.

### 10.5 What did not change

`PreparationPolicy` rules, the 70-character limit, the English requirement, the
Shikamaru attribution requirement, the task definition/instruction/schedule/
Bio template, `present_calls`, `ToolExecutor`, retry/attempt budget, the
scheduler and every Supabase table. No second scheduler, executor or AI entry
point was added; no dependency was added.

### 10.6 Not verified

* **No production access.** Occurrences `22074`/`22078` were not re-read, no
  Render log or deployment was inspected, no SQL was run, and no Bio update was
  triggered.
* **Which rule rejected those two occurrences remains unknown** — the codes
  above are what the *next* occurrence will record; they cannot classify the
  past. The issue is **not** claimed fixed by these tests.
* No live Telegram Bio change was performed or observed.
