# IMPLEMENTATION REPORT

## 1. IMPLEMENTATION METADATA

| Field | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Branch | `main` |
| Starting HEAD | `4069797399ed8a1ab49502792b7ba69c38ae9868` (`fix: accept new AI tool actions on the deterministic fallback route`) |
| Implementation commit (final HEAD at delivery) | `7aee8d1d7c2d9743d389029dfa4fb877641b2afa` |
| Report commit | the single commit immediately following `7aee8d1` on `main`, containing ONLY this report rewrite; its SHA is the final HEAD |
| Report recovery | the dropped create_task/AI_TASK_TRACE report rewrite (`48ee64d`, orphaned by a failed rebase, never pushed) was recovered from git evidence and reconciled into this current-state report as the subsection at the end of §4; the remote report text is otherwise preserved verbatim |
| Implementation date | 2026-09-03 |
| Task/chunk | Forensic root-cause analysis and minimal fix for real AI tool connectivity — `task_list` reproduction case |
| Work type | investigation + implementation |
| Final implementation status | **COMPLETE WITH LIMITATIONS** — root cause proven and fixed with deterministic validation; live Telegram verification outstanding (see §10, §14) |

## 2. OBJECTIVE

The task required diagnosing why AI tools that are registered and
deterministically healthy still fail through the real Telegram AI execution
path, using the reproduction request **"لیست تسک رو ببین"** (`task_list`
capability), and fixing the proven production failure minimally.

- **Primary production symptom** (USER-SUPPLIED PRODUCTION EVIDENCE from
  Render runtime logs and Telegram screenshots in earlier conversation
  context): the request entered the AI, showed an "Inspecting the task
  list" narration phase, then performed a retry/fallback sequence and
  returned an error instead of executing the registered `task_list` tool.
  `ai_last_provider_s ~ 60s` — the handler ceiling.
- **Primary affected path**: `Dispatcher._try_local_fast_path` →
  `backend/ai/actions.py::parse_command_intent` → `ToolExecutor` →
  `task_list` / `task_inspect` (`TaskManagementService`).
- The four capability-exposure tools (`task_list`, `task_inspect`,
  `task_transition`, `retrieve_save`) were registered, schema-visible, and
  executor-executable before this task. Registration was therefore NOT the
  failure; this task proves where the real path diverged and fixes it.

## 3. ROOT CAUSE

**Root cause (proven):** `parse_command_intent` — the deterministic intent
parser that runs BEFORE any provider round — had **no task vocabulary**.
"لیست تسک رو ببین" fell through the entire command vocabulary and returned
`kind=conversational`, so a deterministic READ request was routed to the
provider tool loop. With the configured free-tier model returning prose
instead of a structured tool call, the bounded retry → structured-action
fallback → error chain ran to the handler ceiling.

- **Exact component/module:** `backend/ai/actions.py` →
  `parse_command_intent()` (and the fast path that consumes it,
  `backend/ai/engine/dispatcher.py::_try_local_fast_path`, lines 342–355
  and 1187).
- **Failure mechanism:** no parser branch recognized task-show requests →
  `ActionParseResult(kind=KIND_CONVERSATIONAL)` → dispatcher skips the
  local fast path → provider rounds spent on a request that never needed
  one → prose response → retry/fallback → error at the 60s handler ceiling.
- **Failure layer classification:** deterministic intent resolution
  (pre-registry). The ToolRegistry, Dispatcher tool-schema generation,
  ToolExecutor, permission gates, and the underlying
  `TaskManagementService` were source-verified healthy.
- **Evidence proving the root cause:**
  1. Deterministic reproduction: `parse_command_intent("لیست تسک رو ببین")`
     returned `conversational` — exactly matching the production outcome
     (assertion captured in `tests/test_task_show_intent.py` before the
     `_has_task_mention` correction below).
  2. Source trace: `dispatcher.py` executes `parse_command_intent` before
     any provider round and executes its `tool_calls` through the SAME
     `ToolExecutor` as the provider loop — so a deterministic resolution
     removes the entire provider dependency.
  3. Registry proof: `create_default_registry(ToolContext)` → 36 tools;
     `task_list` and `task_inspect` resolve by name (no `not_found` risk).
- **Secondary defect found during validation:** the first cut of the new
  `_has_task_mention` helper required `len(w) > len(stem)`, which EXCLUDED
  the bare token "تسک" — the reproduction test for the exact production
  sentence failed. Corrected to plain prefix matching (`w.startswith("تسک")`
  / `w.startswith("کار")`). This is recorded because it demonstrates the
  reproduction test is a true production repro, not an expectation rewrite.

## 4. EXACT IMPLEMENTATION CHANGES

### `backend/ai/actions.py` — deterministic task-show resolution

- **Module/function:** new private helpers `_has_task_mention`,
  `_extract_task_id`, `_parse_task_show_intent`; new module vocabularies
  `_TASK_STEMS` (`تسک`, `کار`), `_EN_TASK_WORDS` (`task`, `tasks`, `todo`,
  `todos`), `_TASK_LIST_VERBS` (`لیست`, `لیستش`, `بده`, `ببین`, `نشون`,
  `نمایش`, `list`, `show`).
- **Previous behavior:** any task-show request returned
  `KIND_CONVERSATIONAL` from `parse_command_intent`; the request proceeded
  to provider rounds.
- **New behavior:** a task-show request returns `KIND_EXECUTABLE` with
  concrete `tool_calls`: `{"name": "task_list", "arguments": {}}` for a
  bare list/show request, or `{"name": "task_inspect", "arguments":
  {"task_id": N}}` when an explicit task number is present (Persian digits
  normalized by the existing `_tokenize`/`_parse_number` machinery).
- **Dispatch position:** the new branch runs at the END of
  `parse_command_intent`, AFTER the scheduling intent (so recurring
  requests stay on `create_task`), AFTER save/delete/send intents, AFTER
  the recent-messages branch, and AFTER `_parse_status_intent` (so
  saved-items/database/bio/account requests that merely contain the word
  "کار" never route here). `_has_task_mention` uses plain prefix matching
  so ZWNJ-clitic forms ("تسک‌هام", "تسک‌های") and the bare token "تسک"
  both match; `has_list_verb` reuses the existing `_is_show_verb_token`
  plus `_STATUS_WORDS`/`_SAVE_LIST_WORDS` membership as secondary triggers.
- **Reason:** deterministic READ intents must not depend on provider tool
  selection (the established reliability guarantee of the local fast path).
- **Architectural implications:** none — no new tool, no new executor, no
  registry/permission change. The fast path executes the resolved calls
  through the existing `ToolExecutor` with unchanged permission semantics;
  `task_list`/`task_inspect` are READ_ONLY.

### Correction within this change

`_has_task_mention` was corrected during validation from a
`len(w) > len(stem)` guard to plain `startswith` matching after the
reproduction test failed on the exact production sentence (§3).

### `tests/test_task_show_intent.py` — NEW regression suite (15 tests)

- Persian show/list forms including ZWNJ clitics ("تسک‌هام", "تسک‌های") →
  `task_list`; English "show my tasks" / "list my tasks" → `task_list`.
- Explicit task id (Persian "تسک ۳" and English "task 12") →
  `task_inspect` with the parsed id.
- Negative guards: bare "تسک" stays conversational; scheduling intent wins
  ("هر 1 دقیقه یک بار بنویس سلام" → `create_task`); "لیست سیوها رو بده" →
  `list_saved_items`; "وضعیت بایو چیه" → `get_bio`.

The `create_task`/`AI_TASK_TRACE` implementation changes recovered into §4
(`2ab0db8`, `6566ea7`) are already part of origin/main history and are
documented there rather than re-listed in this table.

### `IMPLEMENTATION_REPORT.md`

Replaced entirely with this single current-state report (the previous file
was an append-log of six earlier phases; per the reporting mandate it is
superseded — the earlier phases remain documented in their delivery commits
and in INVESTIGATION.md).

### Recovered current-state documentation — `create_task` interpretation fix + `AI_TASK_TRACE` observability (commits `2ab0db8`, `6566ea7`, in origin/main history)

The documentation of the already-delivered `create_task` observability work,
which was lost when its report rewrite was dropped during a failed rebase
(`48ee64d`, not in remote history), is restored here because the remote
current-state report did not cover it. The descriptions below were verified
against the code at the current HEAD (incl. the later `create_task` commits
`50a16da`, `8274b4a`, `df23029`, `49f9422`) before inclusion.

**Proven root cause of the original no-diagnostic `create_task` failures:**
`backend/ai/task_interpreter.py` → `TaskInterpreter.interpret()`. Providers
frequently wrap the compliant JSON candidate in markdown fences (` ``` `/` ```json `);
the original code called `json.loads(raw)` directly, the `JSONDecodeError`
collapsed into `TaskInterpretationError`, and the tool boundary's anonymous
`except (…, Exception)` returned a generic message while logging nothing —
production showed `tool=create_task success=False` with zero diagnostics.
Failure layer: AI interpretation parsing (not registry/executor/dispatcher/
repository). Evidence: the source investigation at commit `c40af4a` proved
the interpretation wrapper (branch 5 of seven `ToolResult(success=False)`
branches in `CreateTaskTool.execute`) was the active one by timing (~5.8 s),
single provider attempt, and absence of persistence log lines.

**Exact changes (all verified present at current HEAD):**

- `backend/ai/task_interpreter.py` — `_load_candidate_json(raw)`: retries
  extraction against a markdown-fenced block (`_JSON_BLOCK_RE`) when direct
  `json.loads` fails; candidate validation remains with
  `parse_candidate_output`. `interpret(..., request_id=...)` threads the
  correlation id into `AI_TASK_TRACE stage=interpretation_start`, and no
  longer collapses concrete provider failures into a generic message:
  `response.success is False` now logs `stage=provider_result success=false`
  (provider, attempted-provider list, providers-tried count, category
  `all_providers_failed` or the concrete `failure_type`/`error_type`,
  200-char sanitized detail) and raises a structured error that survives
  into the tool trace; provider success logs provider/model/fallback/latency;
  candidate rejections log `TASK_INTERPRET_REJECTED` with detail; timeout is
  converted with cause preserved; `stage=interpretation_end` reports
  schedule type, action count, provider, latency.
- `backend/ai/tools/task.py` — `_classify_interpretation_failure` maps
  failures to bounded sanitized categories (`timeout`,
  `candidate_invalid_json`, `candidate_invalid`, structured
  `provider=… category=… detail=…` propagation, `unexpected_exception`);
  `_label_hash` gives a SHA-256 12-hex log-safe label fingerprint; `_trace`/
  `_fail` emit one `AI_TASK_TRACE` record per stage and one terminal
  `create_task_failed` record (`failed_stage`, category, exception class,
  sanitized detail, `elapsed_ms`, `persisted=false`) while keeping every
  user-facing message unchanged. Stage coverage: `create_task_received`,
  `create_task_validation_start/end` (deterministic vs `nl_interpretation`),
  `create_task_provider_resolution`, `create_task_interpretation_start`,
  `interpretation_failed`, `create_task_destination_resolution`
  (chat-name/current-chat), `create_task_definition_validation_start/end`,
  `create_task_repository_create_start/result`, and terminal
  `create_task_success` (task id, version, `fallback_used`, final backend,
  elapsed ms).
- `backend/ai/engine/dispatcher.py` (`2ab0db8`) — the fast-path
  `stage=tool_result` log now includes the static tool message (first 160
  chars) instead of only `tool` and `success`.
- `backend/ai/database/task_repository.py` (`6566ea7`) — the Supabase
  create fallback no longer silently returns the in-memory result: it logs
  `create_task_fallback_start`/`create_task_fallback_result` (reason, task
  id, version) and annotates the record with `fallback_backend`
  (observability attribute, not a schema column).
- `tests/test_task_nl_creation.py` — updated ambiguity-failure expectations
  for the new trace stages plus an `AI_TASK_TRACE` lifecycle test class
  (success-path stage sequence, terminal-failure records, fallback-backend
  annotation, request-id correlation, structured provider-failure
  propagation); 20 test functions.

**Validation recorded for that phase** (executed at HEAD `6566ea7`, before
the later remote `create_task` commits): focused suites
`test_task_nl_creation.py`, `test_task_contract.py`, `test_task_repository.py`
→ 34 passed; `compileall -q backend tests` passed; `git diff --check`
passed. Live Telegram verification of the trace chain remained NOT PROVEN
then and is still outstanding.

## 5. FILES CHANGED

| File | Category | Purpose |
|---|---|---|
| `backend/ai/actions.py` | runtime code | Deterministic task-show intent resolution → `task_list` / `task_inspect` tool calls (§4) |
| `tests/test_task_show_intent.py` | test | NEW — 15 regression tests: production sentence repro, clitic/English forms, task-id → inspect, negative guards |
| `IMPLEMENTATION_REPORT.md` | documentation | This report (single current-state document replacing the append-log) |

Per `git diff`/`git status` at delivery: exactly these three files; no
other tracked file changed.

**Files inspected but intentionally unchanged:**
`backend/ai/engine/dispatcher.py` (fast path + ToolExecutor wiring verified
correct — no change needed), `backend/ai/tools/registry.py` (36 tools,
`task_list`/`task_inspect` already registered),
`backend/ai/tools/task_management_tools.py` (executor-healthy),
`backend/ai/providers/manager/manager.py` (fallback chain — ruled out as
root cause, §8), `backend/bot/handlers/ai_unified.py`, `INVESTIGATION.md`
(current capability audit remains accurate; the fast path it documents is
the path this fix extends), `backend/ai/prompt/template.py`,
`backend/ai/task_management.py`, `backend/ai/task_creation.py`.

## 6. AI TOOL CONNECTIVITY IMPACT

**Tool: `task_list`**
- Permission: READ_ONLY — UNCHANGED
- Implementation: complete (`backend/ai/tools/task_management_tools.py`) — UNCHANGED
- Registry: registered (36-tool registry verified in-process) — UNCHANGED
- Provider exposure: native schema + prompt schemas — UNCHANGED
- Provider request / call generation: UNCHANGED (providers still receive the schema and may still propose the call)
- Dispatcher: NOW deterministically resolvable via the local fast path (previously only via provider selection) — **CHANGED (fix)**
- ToolExecutor: sole execution authority — UNCHANGED
- Underlying service: `TaskManagementService.list_tasks` — UNCHANGED
- Result propagation: fast-path summary path — UNCHANGED
- Real Telegram verification: NOT VERIFIED (§10)
- Final status: **PROVEN CONNECTED through the deterministic route (unit + in-process integration); live Telegram NOT VERIFIED**

**Tool: `task_inspect`** — identical profile to `task_list`, resolved with
`{"task_id": N}` from an explicit id; final status: **PROVEN CONNECTED
through the deterministic route; live Telegram NOT VERIFIED**.

**Tools: `task_transition`, `retrieve_save`** — UNCHANGED by this task
(their deterministic JSON-action acceptance was delivered in commit
`4069797` and is covered by `tests/test_new_tool_action_path.py`, which
passes unchanged). Status: **UNCHANGED**.

The connectivity failure for `task_list` was NOT in the registry, provider
exposure, call generation, or executor — it was in intent resolution one
stage before the registry (§3).

## 7. BEHAVIOR CHANGED

### Added
- Deterministic recognition of read-only task-show intents in
  `parse_command_intent`, producing executable `task_list`/`task_inspect`
  tool calls with zero provider involvement.

### Changed
- Task-show requests no longer enter the provider tool loop; they resolve
  on the local fast path (no narration/retry/fallback chain, no 60s
  ceiling exposure for this request class).
- `_has_task_mention` stem matching (corrected during validation, §3).

### Preserved
- All existing intents and their ordering (scheduling, save/delete/send,
  recent-messages, status queries) — asserted by the negative-guard tests
  and by the unchanged 1491-test suite.
- ToolRegistry membership, permission levels, ToolExecutor authority,
  provider fallback semantics, user-visible tool result formats.

### Not Proven
- Live Telegram behavior of the new branch on Render (§10).

## 8. FALLBACK / RETRY BEHAVIOR

- **Fallback occurred in production** (USER-SUPPLIED evidence): after the
  conversational fall-through, the provider route ended in retry/fallback.
- **What caused fallback:** the provider returning prose with no structured
  tool call for a request whose deterministic resolution was missed one
  stage earlier — i.e. **fallback merely exposed another defect**; it was
  the symptom carrier, not the cause.
- **Fallback root cause: RULED OUT.** Source-verified: ProviderManager
  fallback operates after a conversational fall-through has already
  happened; the same request resolves without any provider round once the
  parser branch exists. No ProviderManager code was changed.
- **Tool definitions survived retry/fallback:** yes by construction —
  schemas are regenerated from the registry for every request
  (`_build_tool_definitions` / `_render_tool_schemas`), and the nudge text
  carries the full vocabulary (delivered in `4069797`); not modified here.
- **Request/context survived fallback:** yes — the parser re-reads the
  original `request.user_message` on every recovery pass (verified at
  `dispatcher.py:1187` and `:1396`).
- **Provider response normalization:** not implicated; no change.

## 9. VALIDATION

Only actually-executed validation is listed.

| Validation | Exact command | Exact result |
|---|---|---|
| Reproduction (BEFORE the `_has_task_mention` correction) | `python3 -m pytest tests/test_task_show_intent.py -x -q` | **1 failed** — `AssertionError: 'لیست تسک رو ببین' -> conversational` (true production repro) |
| Focused suite (after fix) | `python3 -m pytest tests/test_task_show_intent.py -q` | **15 passed** |
| Affected suites | `python3 -m pytest tests/test_19_ai_actions.py tests/test_25_fast_path.py tests/test_new_tool_action_path.py tests/test_task_show_intent.py tests/test_capability_exposure_tools.py tests/test_tool_health_audit.py -q` | **170 passed** |
| Full suite | `python3 -m pytest tests/ -q` | **1491 passed, 23 skipped, 1 warning** |
| Compile check | `python3 -m py_compile backend/ai/actions.py tests/test_task_show_intent.py` | **passed** (PY_COMPILE_OK) |
| Whitespace check | `git diff --check` | **passed** (no output) |
| In-process end-to-end chain | `create_default_registry(ToolContext(...))` + `parse_command_intent(...)` scripted run | 36 registered tools; `'لیست تسک رو ببین'` → `executable / task_list / [{'name': 'task_list', 'arguments': {}}]`; `'تسک ۳ رو نشون بده'` → `task_inspect {'task_id': 3}`; `'show my tasks'` → `task_list`; both names resolve in the real registry |

Classification: **UNIT TESTED** (focused suite) + **INTEGRATION TESTED**
(in-process real parser → real registry; real fast-path consumption of
`parse_command_intent` is source-verified and covered by
`tests/test_25_fast_path.py`, which passes unchanged). **LIVE TELEGRAM
VERIFIED: NO.** No mock-only "healthy" claim is made beyond this scope.

## 10. REAL TELEGRAM VERIFICATION STATUS

**Live Telegram verification: NOT VERIFIED** — no deployed Self-Bot or live
Telegram access was available in this workspace; no live test was performed
by this agent.

- **What the user had already observed** (USER-SUPPLIED PRODUCTION
  EVIDENCE): "لیست تسک رو ببین" → narration → retry/fallback → error; no
  real `task_list` execution; `ai_last_provider_s ~ 60s` on Render.
- **What the existing evidence established:** the failure is real and
  reproducible deterministically; the tools themselves were never the
  problem (§6).
- **What a future production verification must prove:** on live Telegram,
  `Nova لیست تسک رو ببین` (or the owner's trigger) produces
  `AI_EXEC_TRACE stage=intent_resolved intent=task_list kind=executable`
  followed by `stage=tool_execute tools=['task_list']` and
  `stage=tool_result tool=task_list success=True` — with the actual task
  list delivered — and latency far below the handler ceiling. Until that is
  observed, live behavior remains **NOT PROVEN**.

## 11. DATABASE / SUPABASE IMPACT

- Database changed: **NO**
- Schema changed: **NO**
- Migrations created: **NO**
- Supabase configuration changed: **NO**
- Live Supabase verification: **NO** (no database interaction in this change)

Database/Supabase impact: **NONE.**

## 12. SECURITY / ARCHITECTURE BOUNDARIES

All boundaries preserved:

- **Self Bot remains execution authority** — the deterministic branch only
  selects an EXISTING registered tool; Telegram side effects still flow
  through `TaskManagementService` → existing repositories.
- **ToolRegistry remains the capability allowlist** — no tool added,
  removed, or re-permissioned (36 tools before and after, verified
  in-process).
- **ToolExecutor remains the sole execution authority** — the fast path
  executes through the same `ToolExecutor.execute_calls` as the provider
  loop (source-verified, `dispatcher.py:1273`).
- **No arbitrary Telegram RPC / SQL / shell / HTTP** — the change is a
  pure intent-recognition vocabulary addition inside `actions.py`; zero new
  I/O surface.
- **Permission/ownership checks intact** — `task_list`/`task_inspect` are
  READ_ONLY and owner-scoped exactly as before; the fast path cannot
  bypass permission gating because it uses the same executor.
- **Destructive-operation verification intact** — untouched; the new
  branch handles only read-only requests.

## 13. INTENTIONALLY NOT CHANGED

- **Hermes / Workers / Service Mesh / Orchestrator** — zero code references
  in the tree; explicitly out of scope.
- **Provider architecture, ProviderManager, new providers** — fallback was
  ruled out as root cause (§8); routing unchanged.
- **UI/panels, Taskloom visuals** — outside scope.
- **Unrelated task functionality** — `task_transition`, `retrieve_save`,
  `create_task` untouched.
- **RuntimeSupervisor, Telegram execution authority, scheduler** — proven
  unrelated to the defect.
- **Database/schema** — no interaction.
- **`INVESTIGATION.md`** — its current capability audit remains accurate;
  this fix extends the deterministic fast path it already documents, and
  the full detail lives in this report.

## 14. LIMITATIONS / UNCERTAINTIES

- **No live Telegram access / credentials** — the production fix is proven
  deterministically, not on the deployed bot.
- **No live provider credentials** — the prose-returning free-tier model
  behavior (openrouter) could not be reproduced locally; it is irrelevant
  to the fixed path (zero provider rounds) but relevant to any request
  that still falls through conversationally.
- **Production log lines were not re-inspected in this session** — the
  symptom is taken from the user-supplied evidence in prior conversation
  context (the current task's evidence section was empty); the
  deterministic reproduction is this session's primary evidence.
- **`_TASK_LIST_VERBS` is a bounded vocabulary** — Persian phrasings
  outside the listed verbs/stems remain conversational by design; the
  negative guards deliberately keep the branch narrow.
- **Full-suite runtime environment** — Python 3.10 in this workspace vs
  3.11 target; 23 skips are pre-existing environment skips, unchanged.

## 15. REMAINING WORK / BLOCKERS

- `task_list` / `task_inspect` require live Telegram verification on Render
  (§10) — the only outstanding proof for "fixed in production".
- Optional follow-up (not started): extend deterministic resolution to
  task-transition phrasings ("تسک ۳ رو متوقف کن" → `task_transition`),
  which currently remain on the provider/JSON-action route.

## 16. GIT DELIVERY

| Field | Value |
|---|---|
| Starting HEAD | `4069797399ed8a1ab49502792b7ba69c38ae9868` |
| Implementation commit | `7aee8d1d7c2d9743d389029dfa4fb877641b2afa` — `fix: resolve Persian task-show requests to task_list deterministically` (files: `backend/ai/actions.py`, `tests/test_task_show_intent.py`) |
| Push result | **SUCCESS** — `4069797..7aee8d1  main -> main` (no force push) |
| Remote verification | `git ls-remote origin main` returned `7aee8d1d7c2d9743d389029dfa4fb877641b2afa` — identical to local HEAD after the implementation push |
| Report commit | the single commit immediately following `7aee8d1` on `main`, containing ONLY this report rewrite; pushed in the same delivery task, same verification method |
| Branch | `main` |
| Local HEAD == origin/main | YES (verified via `git ls-remote` after push; the report commit's SHA is the final HEAD, checkable with `git log -2 --oneline`) |
| Final working-tree state | clean of tracked modifications at delivery; the pre-existing untracked `telegram-self-bot/` nested clone (an ancestor-commit clone, preserved untouched through every prior phase) remains the only untracked entry |

## 17. FINAL REPOSITORY STATE

- **Implementation status:** COMPLETE WITH LIMITATIONS (live Telegram
  verification outstanding)
- **Root cause status:** PROVEN — missing deterministic task-show
  vocabulary in `parse_command_intent`; fixed minimally in
  `backend/ai/actions.py`
- **Affected tools:** `task_list`, `task_inspect` (deterministic route
  added); `task_transition`, `retrieve_save` unchanged
- **Remaining blockers:** live Telegram verification of the fixed path
- **Database impact:** NONE
- **Live Telegram verification:** NOT VERIFIED (user's prior failure
  evidence stands as the last live observation)
- **Final commit:** `7aee8d1d7c2d9743d389029dfa4fb877641b2afa`
  (implementation) + the report commit carrying this file
- **Remote HEAD:** identical to local HEAD (verified via `git ls-remote
  origin main` after push)
- **Working tree:** no tracked modifications; pre-existing untracked
  `telegram-self-bot/` clone preserved
