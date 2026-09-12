# IMPLEMENTATION REPORT — CURRENT STATE

> **This is a CURRENT-STATE document.** It describes the repository as it
> exists at the tip of the LATEST phase (Taskloom input/commit UX + external
> call efficiency). Earlier phase reports are preserved verbatim below, in
> most-recent-first order. If code changes invalidate any section, update this
> document in the same commit.

---

# CURRENT PHASE — Taskloom input/commit UX + external-call efficiency

## 1. Objective

**Part A — Taskloom input/commit UX.** When a Taskloom step asks for a
variable (interval, daily time, weekly time, timezone, text, source, maximum
length, font), the entered value must visibly COMMIT into the current draft and
the step must re-render with that value, instead of leaving the owner in the
ambiguous "still being entered" state. Back must always mean "the previous
logical step in the current wizard/editor", never "Taskloom home"; Back and
Cancel must stay distinct.

**Part B — external-call efficiency.** Render shows rising service-initiated
outbound traffic. Before changing anything, trace every external call one AI
request actually makes; fix only CONFIRMED redundant work and leave required
work in place. No cache layer, retry loop, second scheduler/executor or
provider change.

## 2. Implementation phase

`Phase: Taskloom input/commit UX + external-call efficiency`
(starting HEAD `067897b45c7f56d288949d9c4024ff24f032c1c1`).

Source-first: the current GitHub source at that HEAD was traced before any
edit; older phase reports were used as context only.

---

## 3. PART A — Taskloom input / commit UX

### 3.1 The traced input lifecycle

`input:<panel>:<field>` callback → `helper.panels._handle_input` renders the
field PROMPT (`_INPUT_PROMPTS[field]`) with a footer → the owner replies →
`inline_sender`'s pending-input listener hands the text to the registered
handler `taskloom._wizard_input_handler(field)` → validate →
`draft.updated(...)` (or `notice = "× …"` on failure) → `_store(draft)` →
`taskloom._wizard_finish(notice, …)` re-renders the panel message.

### 3.2 CONFIRMED defects (all three reproduced in-process)

**A1. A committed field never left the prompt when the helper bot was
disabled.** `_wizard_finish` closed the input ONLY through the helper bot:

```python
helper = get_client()
if helper and inline_chat_id and inline_msg_id:      # helper disabled ⇒ NOTHING runs
    await helper.edit_message(...)
if _self_client:
    await _self_client.delete_messages(chat_id, [msg_id])
```

A disabled helper (`BOT_TOKEN` unset) is a documented VALID runtime state
(AGENTS §10), and the fallback is the self client's edit-in-place text panel.
Without it the value WAS committed to the draft and the owner's reply WAS
deleted, but the panel message kept showing the input prompt — the exact
"it still looks like I am entering the value" symptom.

**A2. The shared input prompt had no Back and no real discard.** Its first row
was `("Cancel", "panel:<owner>")`: the label said Cancel but the target merely
re-opened the owning panel, i.e. a Back that preserves. There was no control
that actually discarded, and no row the wizard could label "← Back".

**A3. The EDIT hub had no Back at all.** Its only exits were
`✕ Cancel edit` (discard → task detail) and `❌ Close`, so the required chain
`edit hub → Back → task detail` was not representable and Back (preserve) and
Cancel (discard) were conflated into one control.

### 3.3 Exact fix

- `_wizard_finish` now edits the panel message through the helper bot when it
exists and through `inline_engine._self_client` when it does not (the
documented text-panel fallback); if the rich panel edit raises it retries with
the notice-only edit, so a commit is never silent.
- `helper/panels._handle_input` renders the prompt's first row as
  `("← Back", input_cfg.get("back") or f"panel:{panel_id}")` — it re-opens the
owning panel, which renders its CURRENT step from live state with the draft
  intact — and appends any `extra_rows` the panel declares.
- Every Taskloom field input declares
  `extra_rows = (("✕ Cancel", "action:taskloom_wizard:cancel"),)`, which really
  DISCARDS the draft through the existing mode-aware cancel (edit → the edited
  task's detail, create → the Taskloom list).
- The EDIT hub gains `← Back` → `action:taskloom_wizard:leave`, which returns
to the edited task's DETAIL view and PRESERVES the draft (an in-place action,
so the nav stack is untouched); `✕ Cancel edit` remains the only discard.

### 3.4 Committed navigation semantics (exact)

| control | target | draft |
|---|---|---|
| field input `← Back` | `panel:taskloom_new` → the step that opened the input | preserved |
| field input `✕ Cancel` | `action:taskloom_wizard:cancel` → detail (edit) / list (create) | discarded |
| hub `← Back` | `action:taskloom_wizard:leave` → edited task's detail | preserved |
| hub `✕ Cancel edit` | `action:taskloom_wizard:cancel` → edited task's detail | discarded |
| step `← Back` (create) | ACTION ← CONTENT ← DETAILS ← SCHEDULE ← REVIEW | preserved |
| step `← Back` (edit) | always the edit hub | preserved |

No wizard/editor step emits `panel:_nav:back`. An input never navigates the
wizard: after a successful reply the draft step is unchanged and the step is
re-rendered with the committed value; after a failed reply the step is
unchanged too, the rest of the draft survives, and the error is shown.

---

## 4. PART B — external-call audit

### 4.1 Call graph — one ordinary trigger/reply AI message

Before provider selection (`ai_unified.register`):

| step | call | external? |
|---|---|---|
| trigger resolve (`_load_triggers`, 60 s TTL) | `ai_config` SELECT via `get_config` | 1 read on a cold cache, 0 warm |
| reply-to-AI sniff (reply only) | Telegram `get_reply_message()` | 1 **← was 2** |
| reply-resolver lookup | in-process | 0 |
| config restore (`_restore_config` → `apply_persisted_config`) | `ai_config` SELECT via `get_config` | 1 **← was a 2nd read** |
| reply-context extraction (reply only) | Telegram `get_reply_message()` | 1 **← was a 2nd fetch** |
| media classify | in-process | 0 |

Context construction (`ContextBuilder` / `PromptBuilder`):

| step | call | external? |
|---|---|---|
| conversation history (`get_history(n=20)`) | in-process session registry | no |
| memory (`retrieve_for_prompt`) | in-process memory tiers (`memory/manager.py` only reads its own stores) | no |
| preferences (`_load_preferences` → `get_or_create`) | in-process — only `InMemoryPreferencesRepository` exists | no |
| prompt assembly + token budget | pure | no |

Execution and persistence: provider HTTP (1 + the existing fallback contract);
tool rounds through the single `ToolExecutor` (Supabase/Telegram only as the
tool requires); `_persist_usage` and `_add_message` → `persistence.schedule_audit`
(bounded, durable audit writes — REQUIRED, kept); `record_request` (a TARGETED
`ai_config` UPDATE, not a config rewrite — kept).

Independent of the AI path, every incoming Telegram message reaches
`TaskEventDispatcher.handle_event` → one `ai_tasks` SELECT (owner + active +
`schedule_type=event`, `limit=10`).

### 4.2 Findings

| # | finding | class | action |
|---|---|---|---|
| 1 | `ai_config` read TWICE per cold-cache request: `get_triggers()` is implemented as `get_config()` (`config_store.py:242`), then `apply_persisted_config` read it again | **CONFIRMED** | FIXED — the trigger resolve returns its snapshot and the restore reuses it |
| 2 | the replied Telegram message was fetched TWICE per reply-triggered request (reply-to-AI sniff + reply-context extraction) | **CONFIRMED** | FIXED — the object is fetched once and threaded through |
| 3 | with the helper disabled the panel stayed on the prompt after a commit (A1 above) | **CONFIRMED** | FIXED |
| 4 | memory retrieval, preferences loading and prompt assembly make NO network call | **CONFIRMED** (verified in source) | kept |
| 5 | `record_request` does not rewrite the config; `_persist_usage`/`_add_message` go through the bounded `schedule_audit` path | **CONFIRMED** (verified in source) | kept |
| 6 | the per-message `ai_tasks` event query is one PostgREST round trip per Telegram message | **LIKELY** | kept — REQUIRED for event-triggered tasks, already owner/status/type filtered and `limit`ed; avoiding it would need a cache, which is out of scope |
| 7 | which caller dominates Render's service-initiated bandwidth | **UNKNOWN** | not claimed; no instrumentation was added (a per-request counter would itself add log traffic) |

### 4.3 Changes (Part B)

Only the two CONFIRMED duplicates were removed:

- `backend/bot/handlers/ai_unified.py` — `_load_triggers` returns the
  `ai_config` snapshot it read; `_execute_ai`/`_restore_config` thread it into
  `apply_persisted_config`; the activation handler fetches the replied message
  ONCE (sentinel `_REPLY_UNFETCHED` distinguishes "not fetched" from "no
  reply") and passes it to `_extract_reply_context`.
- `backend/ai/engine/engine.py` — `apply_persisted_config(owner_id, config=None)`
  accepts the caller's snapshot; omitted ⇒ it reads, so this stays the single
  restore entry point. Nothing is cached across requests.

---

## 5. Files changed

`backend/bot/handlers/taskloom.py` · `backend/helper/panels.py` ·
`backend/bot/handlers/ai_unified.py` · `backend/ai/engine/engine.py` ·
`tests/test_taskloom_input_ux.py` (new, 15) ·
`tests/test_external_call_efficiency.py` (new, 9) ·
`tests/test_taskloom_editor_ux.py` · `tests/test_task_wizard_nl_bridge.py` ·
`tests/test_18_ai_execution_agent.py` · `IMPLEMENTATION_REPORT.md`.

`tests/test_task_wizard_nl_bridge.py` and `tests/test_18_ai_execution_agent.py`
were adjusted only because their local `_restore_config` stubs still had the
old one-argument signature — the contract they assert is unchanged.

## 6. Tests

Part A (`tests/test_taskloom_input_ux.py`, 15) — prompt row contract for every
field; the daily input commits into the draft and the SAME step renders
`Schedule: Daily at 23:30` / `Timezone: Asia/Tehran`; a valid input never
leaves the panel on the prompt (helper bot, and the self-client fallback);
helper failure still ends the input with the notice; invalid input keeps the
step + the rest of the draft + the error; Back from a field input returns to
the step with `interval=5`/`tz=Europe/Berlin` intact; Cancel from a field input
discards (create → list, edit → detail, version unchanged); several inputs in
sequence lose nothing and Review matches the persisted weekly task; the edit
hub's Back returns to the task detail with the draft preserved while Cancel
discards; a field input's Back inside an edit re-opens the editor and never the
Taskloom home list.

Part B (`tests/test_external_call_efficiency.py`, 9) — every assertion is a
CALL COUNT on the real path:

| call | before | after |
|---|---|---|
| Telegram `get_reply_message` per reply-triggered request | 2 | **1** |
| `ai_config` read per cold-cache request | 2 | **1** |
| `ai_config` read on a warm trigger cache | 0 | 0 |
| `ai_tasks` event query per incoming message | 1 | 1 (required) |

Plus: a snapshot is never cached across requests (the next request re-reads),
`apply_persisted_config(snapshot)` applies the provider/model without reading
while `apply_persisted_config()` reads exactly once, and the reply-independent
path still fetches the reply when the caller has none.

## 7. Verification

- `tests/test_taskloom_input_ux.py` **15 passed** ·
  `tests/test_external_call_efficiency.py` **9 passed**
- `test_taskloom_editor_ux.py` 34 · `test_task_wizard.py` 35 ·
  `test_bio_source_display.py` 51 · `test_task_wizard_nl_bridge.py` 15 — all green
- focused Taskloom/wizard/source/reliability suites **270 passed**
- full suite `pytest tests -q` — **2428 passed, 24 skipped, 0 failed**
- `py_compile` for every changed Python file **OK**; `git diff --check` **clean**
- the diff is limited to the four source files and the four test files above

## 8. Schema impact

**NONE** — no migration, SQL, schema file, table or column touched. Part A
stores nothing new (the draft is per-owner in memory, unchanged); Part B only
reuses an already-fetched value inside one request.

## 9. Not verified / limitations

1. **Render production traffic reduction was NOT verified.** No production
   session or bandwidth measurement exists in this workspace, so only the
   source-level call counts above are proven. The two removed calls are the
   ones the audit could confirm; the graph alone was not used as evidence.
2. **Live Telegram verification was NOT performed** — there is no production
   self-bot/helper session here. Every Taskloom contract above was driven
   in-process through the real `panels` dispatcher, the real wizard handlers
   and a real in-memory repository.
3. The per-message `ai_tasks` event query must be kept: suppressing it would
   require a cache this phase is explicitly forbidden from adding.
4. An edit draft left via the hub's Back stays pending for that SAME task and
   is resumed by the next "✎ Edit" — that is the documented Back-preserves
   semantics; `✕ Cancel edit` is the way to drop it.

## 10. Delivery

- Implementation commit (source + tests):
  `fix: commit Taskloom field inputs and dedupe per-request external calls` —
  `2967c18` (`git log` holds the full SHA).
- Report/docs commit: `docs: record the Taskloom input UX and call-efficiency
  phase` — `60770c0`.
- Both were pushed to `origin/main` (no force, no rebase, no unrelated files).
- Remote verification after the push:
  `git rev-parse HEAD` == `git rev-parse origin/main` ==
  `git ls-remote origin refs/heads/main` ==
  `60770c0eb494ad630c3e29eb0310a2d1a75a32f5`;
  `git rev-list --left-right --count HEAD...origin/main` = `0 0`; working tree
  clean. The only files touched by this phase are the four source files and the
  four test files listed in §5 plus this report.

---

# PREVIOUS PHASE — Bio Taskloom “Show source” EDIT propagation

## 1. Objective

One reported edit bug: open an existing Bio task in Taskloom → Edit →
Content details → change **Show source** from Yes to No. The details step shows
No, but **Review still shows Yes**, the save persists the old value, and the
next scheduled Bio occurrence still renders the speaker attribution.

## 2. Implementation phase

`Phase: Bio Taskloom “Show source” edit-state propagation`
(starting HEAD `5aa47f5166ca757386bb830b36b2d51936e44535`).

Source-first: the current GitHub source at that HEAD was traced end to end
before any edit; the previous phase reports were treated as context only.

## 3. Root cause (CONFIRMED, reproduced in-process)

**Exact propagation point where the value was lost: the panel ENTRY, not the
toggle, the Review renderer or persistence.**

`backend/bot/handlers/taskloom.py::_wizard_panel()` treated the stored
`edit:<task_id>` query as “start (or restart) the edit from the stored task”
and called `task_wizard.draft_from_task()` on **every** dispatch:

```python
if extra.startswith("edit:"):
    return await _start_edit(extra[len("edit:"):])   # rebuilds from the STORED task
```

The wizard panel is re-dispatched with that exact stored extra by the shared
panel infrastructure — `panels._handle_navigation("back")` re-invokes the
previous panel with its stored `(panel_id, extra)` from the nav stack, and any
repaint / inline re-render of `panel:taskloom_new:edit:<id>` does the same. So
any re-entry mid-edit silently rebuilt the whole draft from the durable
definition, discarding every pending change — including the `show_source`
toggle — and resetting the step back to the edit hub. Review (and the save)
then faithfully reported the **stored** value.

Deterministic pre-fix reproduction (the same sequence the user reported):

```
after toggle           : False  (step: details)
after panel re-entry   : True   (step: edit)      <-- draft rebuilt from the stored task
review shows           : **Show source:** Yes   <-- the reported symptom
```

Every other layer was verified correct at that HEAD and is unchanged:
`_wizard_apply("show_source", …)` + the `instruction_problem` round-trip
guard, `review_lines()` (derived from `build_candidate()`),
`TaskManagementService.update_definition()` (CAS, version +1),
`derive_policy()` and `task_execution.present_calls()`. The bug was the draft
being thrown away before those layers ever saw the new value.

## 4. Exact fix

`_wizard_panel(extra="edit:<task_id>")` now **RESUMES an in-progress edit of the
SAME task** and only reads the stored definition when an edit genuinely starts
(no draft, or a draft for a different task):

```python
if extra.startswith("edit:"):
    raw = extra[len("edit:"):]
    try:
        task_id = int(raw)
    except (TypeError, ValueError):
        task_id = 0
    existing = _drafts.get(_owner())
    if task_id and existing is not None and existing.editing_task_id == task_id:
        return _wizard_render(existing)   # the draft is the source of truth
    return await _start_edit(raw)
```

Unchanged and still the only explicit re-prefill: the hub's
`⟳ Reload from task` (`action:taskloom_wizard:reload` → `_start_edit`). Cancel
still discards the draft, and `＋ New task` (`:new`) still starts fresh — so
the five layers now agree:

| layer | source of truth |
|---|---|
| A — editor state | the in-progress draft |
| B — Review | `review_lines(build_candidate(draft))` (already correct) |
| C — durable definition | `update_definition` CAS write of the built `ai_instruction` |
| D — next occurrence policy | `derive_policy(task.ai_instruction)` |
| E — Telegram output | `present_calls(calls, task.ai_instruction)` |

## 5. Files changed

| File | Change |
|---|---|
| `backend/bot/handlers/taskloom.py` | `_wizard_panel` resumes an in-progress draft for the same task instead of re-prefilling from the stored definition |
| `tests/test_bio_source_display.py` | **10 new** Bio edit-propagation regression tests (exact reported sequence, reverse direction, step/field preservation, real `panel:` dispatcher path, persistence + policy + `present_calls`, explicit reload, cancel/reopen, save/reopen, switching task) |
| `tests/test_taskloom_editor_ux.py` | **4 new** generic editor-resume tests (same-query re-entry, real panel router, fresh session still prefills, `:new` still starts fresh) |

## 6. Behaviour changed

- Toggling **Show source** in an edit survives every subsequent editor step and
  every panel re-entry (`details → schedule → review → save`).
- Review shows the selected value; the persisted `ai_instruction` omits the
  `show the source name` clause when OFF; `derive_policy().show_source` is
  `False`; `present_calls()` strips the verified attribution prefix, so the Bio
  carries only the dialogue line. Flipping it back ON keeps the attribution.
- **Back / re-entry never discards the draft** (the reported bug); Cancel still
  discards it, and Reload still re-prefills from the stored task.
- No change to: source-fidelity/attribution validation, language or length
  validation, schedule semantics, CAS/versioning, Bio Guardian, prepare-ahead,
  `ToolExecutor`, providers, scheduler, or the database schema.

## 7. Tests

- `tests/test_bio_source_display.py` — **51 passed** (41 existing + 10 new)
- `tests/test_taskloom_editor_ux.py` — **34 passed** (30 existing + 4 new)
- focused Taskloom / wizard / source-fidelity suites — **143 passed**
- full suite `pytest tests -q` — **2404 passed, 24 skipped, 0 failed**
- `py_compile` on every changed file — **OK**; `git diff --check` — **clean**
- Regression validity: the pre-fix implementation of `_wizard_panel` was
  replayed verbatim against the new assertions and reproduces the reported
  symptom (draft `True`, step reset, Review `Yes`), so the new tests fail under
  the old code.

## 8. Verification status

- In-process verification: **performed** (handler, wizard, panel router,
  service, repository, preparation policy and execution presentation).
- **Live Telegram verification: NOT performed** — no production self-bot
  session or helper bot is available in this workspace.
- **Supabase schema impact: NONE** — no migration, no SQL, no schema file, no
  column or table touched. The display flag continues to travel inside the
  existing durable `ai_instruction`.

## 9. Remaining limitations

1. The fix guarantees the draft survives a re-entry of the SAME editor; the
   live trigger frequency (how often the nav stack re-dispatches the stored
   `edit:<id>` extra in production) was not observed from this workspace.
2. A definition requiring an EXACT character length is still refused by the
   editor rather than edited (unchanged, deliberate).

## 10. Delivery

- Implementation commit (source + tests):
  `fix: keep the Taskloom edit draft across panel re-entry` — `f2e8e19`
  (`git log` holds the full SHA).
- Report/docs commit: `docs: record the Bio Taskloom show-source edit
  propagation fix`.
- Both were pushed to `origin/main` (no force, no rebase, no unrelated files).
- Remote verification after the push:
  `git rev-parse HEAD` == `git rev-parse origin/main` ==
  `git ls-remote origin refs/heads/main`;
  `git rev-list --left-right --count HEAD...origin/main` = `0 0`; working tree
  clean. The only files touched by this phase are the three listed in §5 plus
  this report.

---

# PREVIOUS PHASE — Taskloom EDITOR UX + task-list reliability

## 1. Objective

**Part A — editor UX.** Entering Edit from a task detail must behave like a
coherent Telegram-native EDITOR: stay clearly in edit mode, prefill the stored
definition, let the owner jump straight to the one field they want, keep Back
meaning "previous EDIT step" (never a panel-stack pop and never a jump to a
menu), let Cancel discard the draft, and return a successful save to the edited
task's detail view.

**Part B — task-list reliability.** Find and fix the actual cause of the
intermittent «Taskloom sometimes does not show my existing tasks».

## 2. Implementation phase

`Phase: Taskloom editor UX + task-list reliability`
(starting HEAD `fd530b88e8e235704b9e424805214fe0c03086d5`).

Source-first: every conclusion below was traced in the current source at that
HEAD before any edit. The previous phase report was treated as context only.

## 3. Root causes

### 3.1 CONFIRMED — the list was read TWICE per render, and a degraded read was
published as the authoritative list

`_taskloom_panel()` derived the ROWS from one repository read
(`service.list_tasks()`) and the STATUS COUNTERS from a second, later read
(`service.counts()`). `SupabaseTaskRepository.list_tasks()` returns the
degraded in-memory FALLBACK rows on **any** failure (and inside the bounded
local-resource cooldown it skips the durable call entirely and returns the
fallback). Two consequences, both reachable exactly when the runtime is in the
`[Errno 11]`/EAGAIN state the production logs show:

1. **Torn panel** — if the store degrades between the two reads, the panel
   renders the durable rows with the degraded read's all-zero counters.
2. **False empty list** — if the FIRST read degrades, the panel renders the
   (usually empty) memory fallback as the genuine `_No tasks yet._` state while
   the owner's durable tasks still exist. **This is the reported symptom.**

The repository already computed the truthful signal (`fallback_active` /
`fallback_reason`) and the interface layer already had `fallback_note()`. The
panel simply never consulted the marker, and never bound it to a single read.

### 3.2 CONFIRMED — the entry point could resurrect an abandoned draft

The task list's `＋ New task` button sent the BARE panel query
(`panel:taskloom_new`). The bare query RESUMES the owner's current draft — which
is the right behaviour for the shared input prompt's Cancel, but from the task
list it meant an abandoned edit draft could be re-rendered as a new-task form.

### 3.3 CONFIRMED — the editor reused the LINEAR creation chain and its exit
was a panel jump

- Edit opened at the creation chain's first step and each step's Back walked
  the CREATION order, so the owner was forced through unrelated steps to reach
  one field.
- `✕ Cancel` while editing was `panel:taskloom` — it jumped to the Taskloom
  LIST and left the assembled edit draft alive in `_drafts`.
- `update_definition` recomputed `next_run_at` on **every** edit, so correcting
  the content of a recurring task silently pushed its next boundary a whole
  interval out.

### 3.4 Ruled out (investigated, not the cause)

- **The wizard does not contain a stack-popping Back.** `panel:_nav:back` in the
  current source is emitted only by `taskloom._nav()` — the footer of the task
  LIST and task DETAIL panels, where "pop to the parent panel" is the correct
  behaviour — and by the generic `backend/helper/panels.py` nav helper. The
  wizard/editor renders `_wizard_footer()` and emits **no** `_nav:back` at any
  step. What was actually wrong is §3.3 (the wrong Back *target* and the wrong
  Cancel semantics inside the editor), which is what got fixed.
- Not a UI refresh/rendering defect: every callback re-reads the list.
- Not lossy or mis-scoped reads: `list_tasks` is owner-scoped and returns every
  non-deleted row.
- Not a stale cache: no task-list cache exists.
- Not stale pagination on deletion: delete already returns to page 0.
- Not the `preparation_metadata` schema mismatch: that concerns
  `ai_task_occurrences` transitions and does not affect `ai_tasks` reads (see
  the previous phase, §6).

## 4. Exact fix

### 4.1 Task list (Part B)

| Change | File |
|---|---|
| `TaskListSnapshot.counts()` — per-status counters derived from **this** snapshot's rows | `backend/ai/task_management.py` |
| `_taskloom_panel` performs ONE `service.snapshot()` read; rows, counters and the degraded marker all describe it | `backend/bot/handlers/taskloom.py` |
| A degraded snapshot is never the genuine empty state: `_Task list unavailable — the durable store could not be read._` + the truthful `fallback_note(reason)` | `backend/bot/handlers/taskloom.py` |
| A degraded snapshot that DOES return rows is annotated as non-durable | `backend/bot/handlers/taskloom.py` |
| An out-of-range page clamps onto the nearest valid page instead of rendering empty | `backend/bot/handlers/taskloom.py` |

The panel's displayed counters now come from the same non-deleted collection as
the rows, so a legacy `deleted` row can no longer contribute to any displayed
total. `TaskManagementService.counts()` is unchanged for callers that want its
own read.

### 4.2 Editor (Part A)

| Change | File |
|---|---|
| `TaskDraft.label`; edit keeps the STORED label so an untouched field is never rewritten | `backend/ai/task_wizard.py` |
| `draft_from_task` REFUSES a definition this editor cannot faithfully reproduce (an exact-length contract) instead of silently rewriting it | `backend/ai/task_wizard.py` |
| `STEP_EDIT` = the editor HUB: summarises action / schedule / content / source+display / font; each row jumps straight to the field step it names | `backend/bot/handlers/taskloom.py` |
| `_wizard_back_step()` returns the hub for **every** edit step, so Back is always "previous EDIT step" | `backend/bot/handlers/taskloom.py` |
| Mode-aware footer: editing → `✕ Cancel edit` (action, pops the draft, returns to the task DETAIL); creating → `✕ Cancel` (`panel:taskloom`); `❌ Close` always `panel:_nav:close` | `backend/bot/handlers/taskloom.py` |
| New `reload` action ("⟳ Reload from task") re-prefills from the stored task on explicit request; the stale-save notice points at it | `backend/bot/handlers/taskloom.py` |
| `＋ New task` → `panel:taskloom_new:**new**` so the explicit entry always starts a FRESH draft | `backend/bot/handlers/taskloom.py` |
| `update_definition` recomputes `next_run_at` ONLY when the schedule really changed | `backend/ai/task_management.py` |

## 5. Taskloom navigation semantics (exact)

| Surface / step | `← Back` | Cancel | Close |
|---|---|---|---|
| Task list | `panel:_nav:back` (stack pop — correct here) | — | `panel:_nav:close` |
| Task detail | `panel:_nav:back` (stack pop) | — | `panel:_nav:close` |
| Wizard · Action (create) | *none* | `panel:taskloom` | `panel:_nav:close` |
| Wizard · Content (create) | `step:action` | `panel:taskloom` | `panel:_nav:close` |
| Wizard · Content details (create) | `step:content` | `panel:taskloom` | `panel:_nav:close` |
| Wizard · Schedule (create) | `step:details` | `panel:taskloom` | `panel:_nav:close` |
| Wizard · Review (create) | `step:schedule` | `panel:taskloom` | `panel:_nav:close` |
| **Editor** · hub | *none* (it is the root of the editor) | `action:cancel` → task **detail** | `panel:_nav:close` |
| **Editor** · Content details | `step:edit` → hub | `action:cancel` → detail | `panel:_nav:close` |
| **Editor** · Schedule | `step:edit` → hub | `action:cancel` → detail | `panel:_nav:close` |
| **Editor** · Review | `step:edit` → hub | `action:cancel` → detail | `panel:_nav:close` |
| Shared input prompt | *none* (it is a sub-view) | `panel:taskloom_new` (resumes the draft) | `panel:_nav:close` |

Invariants asserted by tests: no wizard/editor step emits `panel:_nav:back`;
every editor field step has exactly one Back targeting the hub; an input
submission re-renders the SAME step and keeps the draft; Cancel is not Back and
Back is not Cancel.

## 6. Edit flow (exact)

```
Task detail → ✎ Edit  (panel:taskloom_new:edit:<task_id>)
  → _start_edit: service.inspect(owner, id) → draft_from_task(task, step=STEP_EDIT)
      prefill comes ONLY from the STORED definition; nothing is inferred
  → EDIT HUB   Action · Schedule · Content · Show source · Font
      rows: ✎ Content · ⟳ Schedule · ✔ Review & save · ⟳ Reload from task
  → field step (input/selection) → back to the hub or straight to Review
  → Review  → ✔ Save changes
  → TaskManagementService.update_definition(id, expected_version, candidate, now)
      CAS: version + 1 exactly once; only when it succeeds
      future never-started (claimed) occurrences of the old version discarded
      next_run_at recomputed ONLY when the schedule changed
  → the edited task's DETAIL view (never a menu, never a second task)
```

Stale save: the WHOLE draft is kept, the owner stays in the editor at Review,
the notice names the recovery path, and `⟳ Reload from task` adopts the current
version on explicit request. Nothing is ever prefilled silently from an
untrusted source, and no durable write is attempted on Back or Cancel.

## 7. Task-list correctness contract (after the fix)

1. One render = one repository read; the rows and the counters describe it.
2. A degraded read is never rendered as the genuine empty state, and never as
   an authoritative list: it carries the truthful note (local resource vs. store
   outage are attributed differently, never collapsed).
3. A successful durable read clears the degraded marker and shows the durable
   rows again (immediate recovery).
4. Owner isolation, deletion, empty-store and paging behaviour are unchanged;
   an out-of-range page clamps instead of rendering empty.
5. Non-durable (memory) rows are marked as such.

## 8. Files changed

| File | Change |
|---|---|
| `backend/ai/task_management.py` | `TaskListSnapshot.counts()`; `update_definition` recomputes the boundary only when the schedule changed |
| `backend/ai/task_wizard.py` | `TaskDraft.label`; `STEP_EDIT`; edit prefill keeps the stored label; refuses an unrepresentable definition |
| `backend/bot/handlers/taskloom.py` | one-snapshot list read; honest degraded rendering; page clamp; `:new` entry; editor hub; `_wizard_back_step`; mode-aware footer; `cancel` + `reload` actions; stale-save recovery notice |
| `tests/test_taskloom_editor_ux.py` | **new** — 30 editor UX tests |
| `tests/test_task_list_reliability.py` | **new** — 15 task-list reliability tests |
| `tests/test_task_wizard.py` | the list entry now asserts the explicit `:new` callback |

No provider, scheduler, executor, guardian, diagnostics, migration or schema file
was touched.

## 9. Tests

- `pytest tests/test_taskloom_editor_ux.py -q` — **30 passed**
- `pytest tests/test_task_list_reliability.py -q` — **15 passed**
- focused Taskloom / wizard / NL-bridge / source-display / repository /
  cooldown / management / reliability suites — **264 passed** (including the
  45 new editor + list tests)
- full suite `pytest tests -q` — **2390 passed, 24 skipped, 0 failed**
- `py_compile` OK for every changed file; `git diff --check` clean

**The torn-state regression is deterministic and would FAIL under the old
implementation**: `_FlappingRepository` returns the durable rows on read #1 and
an empty degraded list on any later read, reproducing a degradation that begins
between the two reads. The old panel rendered rows with all-zero counters and
two repository reads; the test asserts `repo.reads == 1`, `● 3 active` **and** 3
task rows together.

Covered (Part A): editor opens on the hub with `editing_task_id`/version; hub
rows jump straight to each field and never offer the creation-only steps;
prefill of action/mode/source/language/max-length/schedule/timezone/label/font;
Back from every editor step targets the hub; no `panel:_nav:back` at any editor
step; footer is `Cancel edit` + `Close`; six input paths keep their step, the
task and the draft and re-render the editor; Cancel discards without touching
the durable task; Back/Cancel leave version, actions, schedule and `next_run_at`
untouched; save returns to the DETAIL view, bumps the version exactly once and
never creates a second task; stale save keeps the whole draft and the recovery
path; reload adopts the current version and then saves; a one-field edit leaves
source/language/length/display/label intact; a content edit does not move the
boundary while a schedule edit does; send-message text edit keeps its canonical
font; username edit keeps its action; a foreign task is not reachable; an
unrepresentable definition is refused.

Covered (Part B): one read per render; a degradation between reads cannot hide
the durable rows; a healthy read lists the durable store; a durable read after a
store failure restores the list and clears the marker; a degraded read is never
the genuine empty state; local-resource degradation never claims "Supabase
unavailable"; a genuine store failure is distinguishable from zero tasks; a
degraded list with rows is marked non-durable; healthy empty stores (memory and
Supabase) still show the genuine empty state; owner isolation; deletion; page
clamp; paging reaches every task exactly once.

## 10. Verification status

- Unit/in-process verification: **performed** (see §9).
- Live Telegram verification: **NOT performed** — no production session or
  self-bot client is available in this workspace. The behaviour was proven at
  the Taskloom handler, wizard, service and repository boundaries, including a
  deterministic in-process reproduction of the torn/degraded list.
- Supabase schema impact: **NONE** — no migration, no SQL, no schema file; the
  `ai_tasks` / `ai_task_occurrences` model is unchanged.

## 11. Remaining limitations

1. The editor exposes the fields the existing task definition can carry
   (content, source/display, language, maximum length, schedule, timezone,
   font). Changing the ACTION of an existing task is deliberately not offered
   (it is not representable without rewriting the whole definition).
2. A definition requiring an EXACT character length is refused with an explicit
   message rather than edited, because the editor can only express a maximum —
   refusing beats silently changing the requirement.
3. The list still shows the in-memory fallback rows while the store is degraded;
   they are now truthfully labelled, but a memory-only task is still not durable
   (unchanged repository semantics).
4. Live production behaviour of the list under a real EAGAIN episode was not
   observed from this workspace; the reproduction is in-process.

## 12. Delivery

- Implementation commit: `fix: harden Taskloom edit UX and task list
  reliability` — `9f97f01dc52028cc63f5bf70a8cf3f3e79a005ec`
- Push: `fd530b8..9f97f01 main -> main` (no force, no rebase)
- `git rev-parse HEAD` == `git rev-parse origin/main` ==
  `git ls-remote origin refs/heads/main` == `9f97f01…`
- `git rev-list --left-right --count HEAD...origin/main` = `0 0`
- Working tree clean; the only files touched are the seven listed in §8
  (3 source, 3 test, this report). No schema/migration/provider/scheduler/
  executor/guardian/diagnostics file was modified.

---

# PREVIOUS PHASE — separate bio source ATTRIBUTION from source DISPLAY

## 1. Objective

Make source DISPLAY an explicit, opt-in property of a Bio task instead of an
implicit consequence of naming a source:

- a Bio task may carry a semantic source/character (e.g. Rei Ayanami) that
  constrains generation **without** its name appearing in the bio;
- source **identity** validation is unchanged (the generated line must still
  prove attribution to the requested source);
- the default is **not** to display the source;
- Taskloom exposes a Bio-only **“Show source?”** option (No / Yes, default No);
- natural-language creation can set it semantically.

## 2. Implementation phase

`Phase: bio source-display separation`
(starting HEAD `fc6879cfa7add680ef0190e2d6dfad7187b16adf`).

## 3. Root cause (traced from source)

Source identity and source display already existed as two concepts but with
the **wrong default polarity**:

- `backend/ai/preparation_policy.py` derived
  `speaker_label = not _contains_any(instruction, _LABEL_FREE_MARKERS)` —
  i.e. the label was rendered **unless** the instruction contained a negation
  (“without a speaker label”). Naming a source therefore implied printing it,
  and the only vocabulary for the *positive* request did not exist at all, so
  “show the source name” was not representable.
- `backend/bot/handlers/taskloom.py` exposed the same double negative as a
  “Speaker label: Show/Hide” toggle whose default was **Shown**, so the
  wizard could not express the owner's actual intent.
- `backend/ai/task_interpreter.py` had no contract for source display, so the
  model was never told that a source is a generation constraint only.

Rendered result in production: `Ayanami Rei: Don't be afraid…` in the bio.

## 4. Exact semantic change

The presentation flag was renamed to match its real meaning and inverted to
default **OFF**;

| | before | after |
|---|---|---|
| policy field | `speaker_label: bool = True` | `show_source: bool = False` |
| draft field | `hide_speaker_label: bool = False` | `show_source: bool = False` |
| vocabulary | “without a speaker label” (hide) | + explicit **show** markers |

The instruction is still the single durable semantic carrier
(`ai_instruction`), exactly like language and maximum length. The deterministic
`derive_policy` is the authority:

```
show_source = bool(source)
              and any show marker
              and no hide marker      # explicit hide always wins
```

Because display is OFF by default, the model can never enable it merely by
naming a source; only an explicit owner request in the (verbatim) instruction
can, and a request that asks for hidden output always wins.

## 5. Behaviour changed

- **Default behaviour** — a source-bearing task's executed content is the
dialogue line alone: `present_calls` removes the validated opening
attribution before the ToolExecutor. Applies to manual, AI, scheduled,
prepare-ahead and retry paths (one code path).
- **Explicit show** — `show the source name` / `اسمش هم اولش باشه` /
`منبع رو نمایش بده` / `with the source name` keeps the attribution in the
executed content, and the length bound then covers the rendered text.
- **Explicit hide** — `اسمش رو ننویس` / `don't show the source` / legacy
`without a speaker label` keeps it off and overrides an incidental show
phrase.
- **Language/length** — still deterministic; when the label is hidden the
contract is applied to the **visible** text (unchanged rule, new default).
- **Source fidelity** — unchanged: `_check_attribution` still requires the
generated line to open with the requested source (full name, spoken order,
separator). Drift, short forms, in-text mentions and generic text remain
rejected and regenerated within the existing bounded attempt budget.

## 6. AI / natural-language behaviour

The interpreter prompt now carries a `SOURCE DISPLAY (default OFF)` contract:
filing a source does not imply rendering it; the model must not invent a
show/hide request; explicit owner wording is preserved **verbatim** in
`ai_instruction` (which the existing deterministic creation gate already
forces when the request derives a content policy). No new candidate field,
no second source of truth: the display choice travels in the same durable
`ai_instruction` as source/language/length.

## 7. Taskloom behaviour

The “Content details” step for a Bio task now shows `Show source: Yes/No` and
one Bio-only toggle button whose label names the value it will set
(`Show source: Yes` when it is currently No). Entering a new source resets the
flag to the safe default. **Edit** prefills the stored value, preserves it
when nothing is changed, and can flip it either way through the existing CAS
`update_definition` path (version +1, boundary recomputed, unstarted
occurrences discarded). Review shows `Show source: Yes/No`.

## 8. Persistence behaviour

The flag is part of the durable semantic definition — the text of
`ai_instruction` — so **no database/schema change was made or is required**.
`draft_from_task` recovers it from the stored instruction, and the wizard's
round-trip guard (`instruction_problem`) rejects any selection the policy
cannot represent, so review can never show an unenforced value.

## 9. Files changed

| File | Change |
|---|---|
| `backend/ai/preparation_policy.py` | `show_source` (default False); explicit show/hide vocabularies; `describe()` and `validate_content()` use the new default |
| `backend/ai/task_execution.py` | `present_calls` gates on `policy.show_source` |
| `backend/ai/task_wizard.py` | `TaskDraft.show_source`; `SHOW_SOURCE_CLAUSE`; round-trip guard; edit prefill; review row |
| `backend/bot/handlers/taskloom.py` | Bio-only “Show source?” option (default No) |
| `backend/ai/task_interpreter.py` | `SOURCE DISPLAY (default OFF)` prompt contract + schema description |
| `tests/test_task_wizard.py` | new default polarity; show-source assertion |
| `tests/test_task_source_fidelity.py` | execution assertions updated to the label-free default (fidelity/guardian tests unchanged) |
| `tests/test_bio_source_display.py` | **new** — 41 focused behaviour tests |

## 10. Tests

- `pytest tests/test_bio_source_display.py -q` — **41 passed**
- related Taskloom / task / source-fidelity / repository suites — **467 passed**
- full suite `pytest tests -q` — **2345 passed, 24 skipped, 0 failed**
- `py_compile` OK for every changed file; `git diff --check` clean

Covered: source alone ⇒ `show_source=False`; explicit show ⇒ True (Persian and
English); explicit hide ⇒ False and overrides show; show marker without a
source is inert; identity/language/length validation unchanged; boundary,
prepare-ahead and per-parameter parametrised display; Taskloom default No,
No→Yes, Yes→No, Bio-only toggle, review value, persistence, edit prefill,
edit preserve/flip via CAS; Bio Guardian still shared by the label-free path;
static non-Bio tasks unaffected.

## 11. Verification status

- Unit/in-process verification: **performed** (see §10).
- Live Telegram verification: **NOT performed** — no production session or
  self-bot client is available in this workspace. The change was proven at the
  deterministic policy, presentation, Taskloom UI, repository and execution
  boundaries.
- Supabase schema impact: **NONE** (no migration, no SQL, no schema file).

## 12. Remaining limitations

1. The “Show source?” toggle is offered for **Bio** only (as specified). The
   underlying policy is shared, so a Username AI task with a source is also
   label-free by default — it has no UI to opt in.
2. The display flag is even meaningful only with a named source; a standalone
   “منبع رو نمایش بده” carries no source and is therefore inert by design.
3. Natural-language *creation* can set the flag; NL **definition editing**
   does not exist in this architecture (list/inspect/transition/delete only),
   so changing the flag on an existing task goes through the Taskloom Edit
   flow.
4. `_extract_source`'s existing trailing-token behaviour (e.g. a verb not in
   its stop vocabulary can join the source phrase) is unchanged and out of
   scope for this phase.

## 13. Delivery

- Implementation commit: `feat: separate bio source attribution from source
  display` — `28d9f0893db6f84003bf3377f7f80e150c62d5eb`
- Push: `fc6879c..28d9f08 main -> main` (no force, no rebase)
- `git rev-parse HEAD` == `git rev-parse origin/main` ==
  `git ls-remote origin refs/heads/main` == `28d9f08…`
- `git rev-list --left-right --count HEAD...origin/main` = `0 0`
- Working tree clean; the only files touched are the nine listed in §9
  (5 source, 3 test, this report). No schema/migration/provider/scheduler/
  executor/guardian file was modified.

---

# ARCHIVE — superseded phase reports

## 1. Objective

Repair seven independently reported Taskloom/task-system defects from the
actual source, without redesigning the architecture:

1. scheduled tasks execute late / irregularly / not at all;
2. natural-language task management (create/pause/resume/delete/list/inspect)
   does not work reliably from the first message;
3. Taskloom wizard **Back** leaves the wizard and jumps to Taskloom home;
4. manually opened tasks have no **Edit** option;
5. send-message editing must keep the project's existing Unicode/font
   capability;
6. all recurring Bio task executions fail with a PostgREST schema error
   (`PGRST204 … 'preparation_metadata' column …`);
7. the long-lived runtime coroutines in the watchdog dumps must not be
   reported as starvation.

## 2. Implementation phase

`Phase: task-scheduler / first-message task management / Taskloom edit repair`
(starting HEAD `40b3fb7d3a0e79f44d7cf435d0238777083e168d`).

## 3. Root causes (each traced from source)

### 3.1 Scheduling latency, missing runs, starvation (problem 1)

`TaskScheduler.run_once()` processed due tasks **sequentially**, and each
task's `next_run_at` was advanced only **after** its full execution
(`TaskExecutionCoordinator.execute` = up to `MAX_EXECUTION_SECONDS = 60 s`,
plus AI preparation of up to `3 × MAX_PREPARATION_SECONDS = 45 s`). So:

* a slow task blocked every other due task in the same wake (head-of-line
  blocking → multi-minute lateness, and the recurring boundaries of the
  blocked tasks were served late);
* `run()` slept a fixed `WAKE_INTERVAL_SECONDS = 60 s` *after* the sweep, so
  even a fast sweep could not serve a boundary that landed just after it;
* `MAX_TASKS_PER_WAKE = 10` with a single batch per wake meant a task due
  behind ten others waited a whole poll interval (starvation);
* a task was re-selected while its execution was still running because its
  boundary had not moved yet.

The advance itself was already **correct**: `catch_up_occurrence()` advances
from the persisted SCHEDULED boundary, never from the execution finish time,
so the cadence was not being shifted — it was being *delayed*.

### 3.2 First-message task management (problem 2)

`Dispatcher._read_results_authoritative()` treated a round that executed only
`task_list` as final: it replaced the response with the verbatim tool output
and **broke the tool loop immediately** (`_VERBATIM_READ_TOOLS = {get_bio,
task_list}`). But `task_transition` / `task_delete` require the task's CURRENT
version, which only `task_list`/`task_inspect` can supply. Therefore
"pause task 11" resolved to a single `task_list` round, printed the list, and
the mutation was never requested again — while "show my tasks" (the reason the
verbatim rule exists) must still deliver the tool output untouched.

A second, smaller defect: `TaskTransitionTool` only read the `action`
argument, while the JSON-action contract (and the prompt's own examples) use
`action_status`; and a stale version produced no current version for the
follow-up round.

### 3.3 Wizard Back (problem 3)

`_wizard_render()` appended the generic panel navigation (`_nav(builder)` →
`panel:_nav:back` / `panel:_nav:home`) to **every** wizard step, and
`panels._finalize_panel()` injects those same buttons whenever a panel
supplies none. `panel:_nav:back` pops the panel nav stack, whose previous
frame is the Taskloom list — so the wizard's own "← Back" and the shared
footer Back both jumped out of the wizard (and `_handle_input`'s prompt
offered the same stack-popping Back while a field was being entered).

### 3.4 No task editing (problem 4)

The Taskloom detail panel had pause/resume/complete/delete/refresh only, and
`TaskManagementService` had no definition-edit operation — `set_status()`
writes status, never schedule/actions/instruction.

### 3.5 Unicode/font capability (problem 5)

The canonical display-font registry is `backend/helper/font_style.py`
(`FONT_KEYS`, `apply_font`), used by the Glass UI/dashboard only. Nothing
carried a font choice into a scheduled `send_message` definition, and
`TaskCandidate._canonicalize_action()` deliberately dropped every argument
except `text`.

### 3.6 `PGRST204 'preparation_metadata'` (problem 6)

`20260829000001_create_ai_tasks.sql` creates `ai_task_occurrences` with
`CREATE TABLE IF NOT EXISTS`, and `preparation_metadata` was added to **that
same file** in commit `164ccc2`. On the production database the table already
existed, so the modified `CREATE TABLE IF NOT EXISTS` was a no-op and the
column was never created. The application then failed every occurrence
transition that carried the column.

That is not cosmetic:

* `TaskExecutionCoordinator.execute()` writes the terminal state together
  with the (diagnostic) `preparation_metadata` audit record on **one** update;
* with the column missing, the durable write of the *status* failed too, the
  occurrence stayed `running`, recovery converted it to `interrupted →
  retry_pending`, and the already-completed Telegram side effect could be
  executed again;
* `prepare_ahead()` could not persist its prepared action either, so every
  prepared boundary was re-prepared (or replayed), generating the repeated
  warning pairs in the Render logs.

### 3.7 Watchdog classification (problem 7)

Already correct at HEAD: `backend/runtime/diagnostics.py` classifies
`lifeos-task-scheduler`, `lifeos-profile-scheduler`, `lifeos-run`,
`lifeos-helper` and the Telethon `_update_loop` / `_recv_loop` / `_send_loop` /
`mtprotosender` coroutines as PERMANENT and excludes them from
`TASK_NO_PROGRESS` / `TASK_STARVATION`, and `backend/health.py::set_heartbeat`
is the live writer of `_last_heartbeat` (called every heartbeat tick). **No
change was needed**; the existing tests (`tests/test_07_diagnostics.py`,
`tests/test_runtime_diagnostics_classification.py`) cover it and pass.

## 4. Files changed

| File | Change |
|---|---|
| `backend/ai/task_scheduler.py` | bounded concurrent execution, batch sweeps, sleep-until-nearest-due |
| `backend/ai/database/task_repository.py` | `next_run_hint()`, `discard_unstarted_occurrences()`, schema-drift-safe occurrence transition |
| `backend/ai/engine/dispatcher.py` | defer the verbatim short-circuit for CAS-read rounds |
| `backend/ai/tools/task_management_tools.py` | accept `action_status`, report the current version on a stale CAS |
| `backend/bot/handlers/taskloom.py` | wizard-owned navigation footer, Edit entry + edit mode, font field |
| `backend/helper/panels.py` | input prompt no longer offers a panel-stack Back |
| `backend/ai/task_wizard.py` | edit-mode draft (`draft_from_task`), font field + preview |
| `backend/ai/task_management.py` | `update_definition()` (CAS definition edit + future-occurrence invalidation) |
| `backend/ai/task_creation.py` | shared `initial_next_run()` (one boundary calculation for create + edit) |
| `backend/ai/task_candidate.py` | allow-list-validated `font` on the message action |
| `backend/ai/tools/message.py` | bounded `font` parameter; canonical transform at send time |
| `supabase/migrations/20260912000001_add_ai_task_occurrences_preparation_metadata.sql` | **new** idempotent column repair |
| `DATABASE_ARCHITECTURE.md` | records the migration and the drift |
| `tests/test_task_reliability_repair.py` | **new** 49 focused regressions |
| `tests/test_20_advanced_execution.py`, `tests/test_task_list_consistency.py` | updated to the repaired, still-safety-preserving contract |

## 5. Behaviour changed

### 5.1 Scheduler timing semantics

* Every due task in a wake is served: sweeps repeat in batches of
  `MAX_TASKS_PER_WAKE` up to `MAX_SWEEPS_PER_WAKE` (200 due tasks/wake), so a
  task due behind ten others is no longer deferred a poll interval.
* Within a sweep, tasks run with bounded concurrency
  (`MAX_CONCURRENT_EXECUTIONS = 4`) via the existing per-occurrence claim CAS
  — one slow execution no longer serializes the others.
* `run()` sleeps `min(WAKE_INTERVAL_SECONDS, max(MIN_WAKE_SECONDS, time to the
  nearest known boundary))` using the new advisory `next_run_hint()`; failures
  of the hint (or no active task) fall back to the plain 60 s poll, so retries
  can never be starved.
* The boundary advance keeps its exact previous position in the per-task
  sequence (create occurrence → execute → advance → prepare-ahead). **This is
  load-bearing**: `advance_next_run` bumps the task's CAS version, and a
  durably prepared action is stamped with the version that must still be
  current when the boundary executes.
* `catch_up_occurrence()` remains the only boundary calculation; a delayed
  wake never shifts the cadence to the execution time.

### 5.2 First-message task-management semantics

* A round that executed **only** `task_list`/`task_inspect` now gets one
  continuation round instead of the immediate verbatim short-circuit, so
  `task_list → task_transition` / `task_delete` can complete in one request.
  If that continuation produces no tool call, the verbatim tool output is
  delivered exactly as before (the anti-paraphrase guarantee is unchanged;
  `tests/test_task_list_consistency.py` still proves the fabricated narration
  never reaches the owner).
* `TaskTransitionTool` accepts `action` **and** `action_status`.
* A stale CAS now reports the task's current version
  (`current_version` in `data`, `… retry with expected_version=N` in the
  message) so the next round can finish deterministically.
* Destructive safety is unchanged: owner scoping, required `task_id` +
  `expected_version`, `CAS` transitions, real row-removal deletion, and
  rejection of a missing/ambiguous target all still hold.

### 5.3 Taskloom Back / input semantics

* The wizard renders its **own** footer (`✕ Cancel` → Taskloom,
  `❌ Close` → close panel) and never emits `panel:_nav:back`, so the shared
  finalizer cannot inject a stack-popping Back.
* Back is always the wizard's previous step, rendered as a draft step change:

  | Step | Back goes to |
  |---|---|
  | Action | *(no Back — Cancel is the explicit exit)* |
  | Content | Action |
  | Content details | Content (AI) / Action (static) |
  | Schedule | Content details |
  | Review | Schedule |

* Submitting an input updates only that field, keeps `draft.step`, and
  re-renders the same step; unrelated draft values survive.
* The shared input prompt no longer offers a panel-stack Back (its `Cancel`
  already returns to the owning panel).
* `Cancel` still returns to Taskloom; `Close` closes; **Back never does
  either**.

### 5.4 Edit semantics

* The task detail panel has `✎ Edit` → the SAME wizard, prefilled from the
  STORED definition only (`draft_from_task`), then persisted through the SAME
  CAS update path: `TaskManagementService.update_definition()` →
  `repository.update_task(expected_version=…)`.
* Only representable definitions can be edited; event-triggered or
  unknown-action tasks are refused with an explicit message (no silent
  conversion).
* Every successful edit: uses `expected_version`, increments the version
  exactly once (`update_task` is the only writer), recomputes `next_run_at`
  from the NEW schedule through the shared `initial_next_run()`, keeps the
  task id and the stored destination when the edit chose none, and discards
  **future, never-started** occurrences (`discard_unstarted_occurrences`)
  so the next boundary runs the new definition instead of a stale snapshot.
* Started/terminal occurrences are never touched: history stays an immutable
  snapshot of its own `definition_version` / `action_snapshot`.
* A stale form (the task changed after the wizard opened) writes nothing and
  says so.

### 5.5 Unicode/font behaviour

* The message action carries an optional `font` key validated against the
  canonical registry (`is_valid_font`); the stored text stays RAW.
* The send tool applies `font_style.apply_font(text, key)` at execution time,
  so scheduled execution is deterministic and re-editing a styled task never
  double-styles it. No second Unicode subsystem was introduced.

### 5.6 Bio execution path and schema honesty

* The durable status transition is retried **without** the optional
  `preparation_metadata` audit field when (and only when) PostgREST reports an
  unknown-column/PGRST204 error **and** the status actually changes. The
  durable state is then persisted truthfully and the dropped diagnostics field
  is reported once per episode
  (`TASK_OCCURRENCE_AUDIT_FIELD_DROPPED … durable_state_transition=persisted`).
* The repository is *not* marked degraded by that case, so a healthy store is
  not reported as unavailable.
* A drift on any other (required) column is never stripped: the transition
  degrades honestly (`fallback_active=True`, non-durable) exactly as before.
* `prepare_ahead()`'s same-status write (which carries only the prepared
  action) is never retried without the field — dropping it would claim a
  durable preparation that never happened.
* Bio Guardian policy, preparation policy, source attribution, language and
  length validation, attempt limits, occurrence uniqueness and the fallback
  contract are unchanged.

## 6. `preparation_metadata` schema status

* The repository migration **does** declare the column
  (`20260829000001_create_ai_tasks.sql`), but only inside
  `CREATE TABLE IF NOT EXISTS`; the production table predates it, so the live
  database is **behind** the repository schema. This was **not** verified
  against the live database from this workspace.
* Repository-side repair added (idempotent, safe to apply twice):
  `supabase/migrations/20260912000001_add_ai_task_occurrences_preparation_metadata.sql`.
* **Manual SQL the operator must apply** (Supabase SQL editor):

```sql
ALTER TABLE ai_task_occurrences
    ADD COLUMN IF NOT EXISTS preparation_metadata jsonb NOT NULL DEFAULT '{}';

ALTER TABLE ai_task_occurrences
    DROP CONSTRAINT IF EXISTS ai_task_occurrences_preparation_metadata_object;
ALTER TABLE ai_task_occurrences
    ADD CONSTRAINT ai_task_occurrences_preparation_metadata_object
    CHECK (jsonb_typeof(preparation_metadata) = 'object');

ALTER TABLE ai_task_occurrences
    DROP CONSTRAINT IF EXISTS ai_task_occurrences_preparation_size;
ALTER TABLE ai_task_occurrences
    ADD CONSTRAINT ai_task_occurrences_preparation_size
    CHECK (octet_length(preparation_metadata::text) <= 8192);

NOTIFY pgrst, 'reload schema';
```

* **Rollback** (only if the column is truly unused):

```sql
ALTER TABLE ai_task_occurrences DROP CONSTRAINT IF EXISTS ai_task_occurrences_preparation_size;
ALTER TABLE ai_task_occurrences DROP CONSTRAINT IF EXISTS ai_task_occurrences_preparation_metadata_object;
ALTER TABLE ai_task_occurrences DROP COLUMN IF EXISTS preparation_metadata;
```

* Database/migration impact: **one additive migration**, no schema
  restructuring, no new table, no index change, no RLS change, no data
  mutation. No SQL was executed against the user's Supabase instance from
  this workspace.

## 7. Tests

Added `tests/test_task_reliability_repair.py` — **49 focused behavioural
tests**:

* scheduler: no early execution, cadence preserved after a late wake, 25 due
  tasks in one wake, concurrency proof (measured overlap + elapsed bound), a
  definition edit that changes only the future schedule, `retry_at` honoured,
  bounded sleep-until-nearest-boundary;
* first-message management: read→mutate continuation executes the requested
  transition, read-only request still verbatim, JSON `action_status` mapping,
  missing/ambiguous targets rejected, stale CAS reports the current version
  (transition + delete), `action_status` accepted by the tool;
* wizard: no `panel:_nav:back` on any step, exactly one Back per step with the
  correct target, Cancel/Close differ from Back, every input keeps its step
  and the rest of the draft, Back from Review preserves the draft, input
  prompt has no stack-popping Back;
* edit: `✎ Edit` entry present, end-to-end wizard edit updates the SAME task
  (version +1, no second task), stale form refused, future unstarted
  occurrences discarded while history is preserved, review shows the font,
  raw text + font preserved, invalid font rejected, canonical transform
  applied at send time, `draft_from_task` prefill + refusal of
  unrepresentable schedules;
* schema/fallback: missing audit column keeps the durable transition (and is
  logged), missing required column is never stripped, same-status write is not
  stripped, genuine store failure still classifies as `unavailable`, local
  resource failure still classifies as `local_resource`, `next_run_hint` is
  advisory and never degrades.

Test results actually executed:

* `pytest tests/test_task_reliability_repair.py -q` → **49 passed**
* `pytest tests -q` → **2300 passed, 24 skipped, 0 failed** (67 s)
* `py_compile` on every changed Python file → **OK**
* `git diff --check` → **clean**

Two existing tests were updated, both because the *contract they assert was
deliberately repaired* (not weakened):

* `tests/test_task_list_consistency.py` — the task-list round now costs one
  continuation round; the assertion still proves the fabricated narration
  never reaches the owner.
* `tests/test_20_advanced_execution.py` — the `send_message` guard now expects
  `{text, font}` and additionally asserts the font enum is EXACTLY the
  canonical registry, i.e. no arbitrary value can reach Telegram.

## 8. Live verification status

**Live Telegram / Render verification was NOT performed.** There is no
production session available in this workspace. Everything above was verified
in-process (unit/behavioural tests) against the real dispatcher, scheduler,
repository, wizard and tool boundaries. The production Supabase schema is
**not** verified; the manual SQL in §6 remains outstanding, and the
application-side schema-drift tolerance is what keeps task execution honest
until it is applied.

## 9. Remaining limitations

1. A pure "list my tasks" request now costs one extra provider round (the
   deferral is the price of read→mutate requests finishing in one message).
2. Edit mode covers Bio / Username / send-message definitions with
   once/interval/daily/weekly schedules. Event-triggered tasks and
   unknown-action tasks must still be recreated (and say so honestly).
3. An interval whose stored value is not a whole number of minutes must be
   re-entered during an edit (the wizard's unit is minutes).
4. If the durable store is genuinely unreachable at the moment a Telegram
   action has already succeeded, the terminal state still cannot be persisted
   and recovery may retry that occurrence; this phase removes the *observed*
   cause (the audit column) but cannot make a two-system commit atomic.
5. Preparing ahead keeps the previous per-task order (execute → advance →
   prepare) because the prepared-action version contract depends on it; a very
   slow task can therefore still delay the *next batch* of the same wake, but
   no longer the tasks in its own batch, and no longer the following wake.

## 10. Delivery

| Item | Value |
|---|---|
| Starting HEAD | `40b3fb7d3a0e79f44d7cf435d0238777083e168d` |
| Implementation commit | `c9a08ae9f4f92e0771e20166b05639cb4d79f95d` — *fix: repair task scheduling, first-message task management, taskloom edit and occurrence persistence* |
| Push result | `40b3fb7..c9a08ae  main -> main` (no force, no rebase) |
| `origin/main` | `c9a08ae9f4f92e0771e20166b05639cb4d79f95d` |
| `git ls-remote origin refs/heads/main` | `c9a08ae9f4f92e0771e20166b05639cb4d79f95d` |
| `HEAD...origin/main` | `0 0` |
| Working tree | clean (`## main...origin/main`) |

This report section was updated in a follow-up docs commit that records the
implementation SHA above; no source file changed in it.
