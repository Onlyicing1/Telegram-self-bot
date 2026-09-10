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
| Starting HEAD | `dfeebf4643ef4b1724d82b58bf34c7bc7d7e1ba9` (== `origin/main` at phase start) |
| Phase | (1) Scheduled **bio update persisted as `send_message`** — deterministic bio-action fidelity fix; (2) **Test Modules UI** converted from colorful emoji to plain Unicode symbols |
| Status | **IMPLEMENTED — full suite green (2090 passed, 24 skipped, 0 failed)** |
| Database impact | **NO DATABASE / SCHEMA CHANGE** |
| Delivery record | see §7 |

---

## 2. ISSUE 1 — a scheduled Bio update persisted as `send_message`

### 2.1 Canonical tool name (source of truth)

The registered production bio-write tool is **`bio_set_text`**
(`backend/ai/tools/registry.py` → `BioSetTextTool`, name `bio_set_text`,
delegating to `bio_service.do_text` → the shared `_apply_profile` real
mutation via `UpdateProfileRequest(about=...)`). There is **no** registered
`set_bio` tool in production: `set_bio` appears only as a *fake* tool name in
test fixtures (`tests/test_task_source_fidelity.py`,
`tests/test_task_prepare_ahead.py`). This phase therefore uses the real
registered name — `bio_set_text` — and does not invent a new tool.

### 2.2 Exact root cause (source-verified)

The task-creation pipeline had **no deterministic guard** tying an explicit
bio-update request to a bio tool:

1. `TaskInterpreter` (`backend/ai/task_interpreter.py`) *asks* the model for
   `bio_set_text` on bio requests, but the candidate schema leaves
   `actions[].name` a free-form string. A model that answers with the generic
   message action therefore passes validation.
2. `TaskCandidate._canonicalize_action` (`backend/ai/task_candidate.py`)
   normalizes only message-write aliases (`send`, `send_message`,
   `write_message`, `send_text`) → `send_message`; every other action name is
   trusted verbatim. Nothing repaired the misclassification.
3. The deterministic high-confidence scheduler shortcut
   `Dispatcher._build_deterministic_task_candidate` only ever builds
   `{"name": "send_message", ...}` for an interval + write-verb request, and its
   result **bypasses the semantic interpreter entirely**
   (`extra["deterministic_task_candidate"]` → `CreateTaskTool` uses it as-is).

Result: a clear bio update could be persisted with
`actions: [{"name": "send_message", "arguments": {"text": ...}}]`. The
occurrence path then executes the task's own tool names, so every run sent a
chat message instead of updating the bio.

### 2.3 Exact fix (minimal, one boundary)

A deterministic **profile-fidelity gate** was added at the single authoritative
creation boundary — `CreateTaskTool._execute`
(`backend/ai/tools/task.py`), beside the existing `ai_instruction`
source-fidelity gate:

- It runs only when the **ORIGINAL request** both names the bio and asks to
  change it. Detection reuses the existing deterministic bio vocabulary in
  `backend/ai/actions.py` (`_tokenize`, `_has_bio_mention`,
  `_has_bio_change_intent`, `_write_text_present`) — it is a narrow repair, not
  a keyword-only replacement of the semantic interpreter.
- When it matches, an action whose name is `send_message` (the only name the
  message vocabulary can produce) is renamed to the canonical registered
  `bio_set_text`, keeping its bounded `text` argument untouched.
- One correlated `AI_TASK_TRACE stage=create_task_bio_action_gate` record is
  emitted when a repair happens.
- A request that genuinely sends a message is completely untouched, and every
  other action name is untouched.

The gate covers **both** creation paths (semantic and deterministic) because
both flow through `CreateTaskTool._execute`. The deterministic shortcut itself
was deliberately left alone — no change to scheduler/dispatcher semantics is
required, and the gate repairs its only possible bio misclassification.

### 2.4 Preserved behavior (unchanged)

- `set_bio`/`bio_set_text` remains the **existing execution authority**
  (`BioSetTextTool` → `bio_service`); no new Bio executor.
- No second scheduler, no ToolExecutor bypass, no Telegram execution moved into
  the AI layer.
- The existing **AI-generated-content contract**: the verbatim request still
  becomes `ai_instruction` for content-constrained bio tasks
  (`create_task_ai_instruction_gate`), and preparation/validation still happens
  in `TaskExecutionCoordinator`.
- The **Bio Guardian / rolling 60-second** protection is untouched.
- `send_message` semantics are untouched for message tasks.
- Ambiguous requests still fail closed (no guessing).
- `bio_set_text` (with `text`) renders through the existing bio template
  (`{text}` token) and applies through `_apply_profile`.

### 2.5 Tests added (`tests/test_task_source_fidelity.py`, Part C)

| Test | Reproduces |
|---|---|
| `test_persian_bio_update_misclassified_as_message_persists_bio_tool` | Clear Persian bio request + a `send_message` candidate → persisted action is `bio_set_text` |
| `test_english_bio_update_misclassified_as_message_persists_bio_tool` | Same for a clear English bio request |
| `test_plain_message_task_still_persists_send_message` | A real message task still persists `send_message` |
| `test_bio_request_keeps_verbatim_ai_instruction_when_action_repaired` | Repairing the tool name preserves the verbatim `ai_instruction` (source/length constraints intact) |
| `test_deterministic_message_write_candidate_for_bio_request_is_repaired` | The deterministic shortcut's `send_message` candidate for a bio request is repaired, with **zero provider calls** |
| `test_bio_occurrence_executes_bio_tool_not_message_tool` | The resulting occurrence reaches `bio_set_text` through the real `TaskExecutionCoordinator` → `ToolExecutor` path, and never `send_message` |

---

## 3. ISSUE 2 — Test Modules UI uses plain Unicode symbols

The AI / Test Modules panel family was converted from colorful emoji to plain
Unicode marks. The compact Taskloom-like layout, the five-segment progress bar,
the `_PanelEditGuardian` coalescing, pagination, concurrency protection, the
diagnostic budget, and the production candidate-feed logic are **unchanged**.

| Mark | Meaning | Mark | Meaning |
|---|---|---|---|
| `◉` | AI / intelligence | `↻` | refresh / retry / re-run |
| `◈` | provider | `»` | start chat / enter |
| `◇` | model | `▸` | test / execute (verb) |
| `●` / `○` | connected / offline | `▰` / `▱` | filled / empty progress (exactly five segments) |
| `✓` | success / available | `←` | back |
| `×` | failure / error | `⌂` | home |
| `!` | warning / problem | `⋯` / `…` | running / testing |

Exact changes:

- **`backend/bot/handlers/ai.py`** — every emoji in the AI panel module:
  `🧠→◉`, `🤖→◇`, `🔄`/`🔁→↻` (refresh/retry) and `→◈` (provider),
  `⬅→←`, `🏠→⌂`, `⚠️→!`, `✅→✓`, `❌→×`, `💬→»`, `🎫/🧵→⌗`,
  `📈→▤`, `🩺`/`❤️→✚`, `🔍`/`🔎→⌕`, `⚙️→⚙`, `📖→☰`, `🔧→⊞`, and
  `p.icon` is no longer rendered (the plain provider mark `◈` is used instead).
  Panel-registration titles were converted with the same vocabulary.
- **`backend/ai/model_tester.py`** — the unknown-provider fallback icon `❓`
  → `◈` (two sites).
- **`backend/bot/handlers/ai_test_progress.py`** — already Unicode-only
  (five-segment `▰▱` bar, `✓/×/…`, `◇`); unchanged.
- The Test Modules launch/results/details views already used
  `✓ × … ◇ ↻ ⌕ ≡ ⌂` and were left as-is apart from the shared provider mark.

Not touched (out of scope, shared/unrelated UI): the shared nav helpers in
`backend/helper/panels.py` used by non-AI panels, and the provider
**catalog metadata** icons in `backend/ai/discovery.py` (still consumed by the
web dashboard's `/api/ai/*` payload). The AI panels no longer render those
catalog emoji.

### 3.1 Tests added / changed for the Unicode UI

- **Changed** `tests/test_13_model_selection.py` (AI panel title `🧠 AI` →
  `◉ AI`) and `tests/test_34_ai_model_ui.py` (model panel title `🤖 Model` →
  `◇ Model`, two sites).
- **Added** `tests/test_23_provider_mesh.py::test_ai_and_test_modules_panels_use_no_colorful_emoji`
  — renders the AI main panel, the provider panel (with an emoji carrying
  catalog icon) and the pick-model panel, then sweeps title + body + every
  button label for the forbidden emoji set. It also proves the provider panel
  no longer renders the catalog emoji icon.
- Existing Unicode regression kept green:
  `test_unicode_progress_and_status_marks_only` (five-segment bar, status
  marks, `_render_test_results` sweep).

---

## 4. Preserved architecture from the previous phases (still current)

- **Production fallback** — model-level, bounded, deterministic: active
  provider/model first, then the complete discovery-fed eligible real/free
  model pool (no per-provider cap), health/cooldown/quarantine, model-not-found
  TTL, one bounded retry per candidate, structured-contract failover; total
  real-candidate exhaustion returns an honest `success=False` (the Dummy
  provider is **never** in production routing or fallback).
- **Discovery feed** — one discovery pass feeds the complete production
  candidate pool independently of the `_MODELS_IN_RESPONSE` display cap and of
  `MODEL_TEST_GLOBAL_TEST_BUDGET`.
- **Test Modules** — non-blocking launch, `_test_running` concurrency guard,
  `_PanelEditGuardian` coalescing (12 s window, newest state, dedupe,
  serialized, terminal retry), exact five-segment progress, pagination,
  diagnostics budget, canonical `_render_test_results`.

---

## 5. Database impact

**None.** No schema, table, migration, RLS, or config change. The bio fix is a
pure action-name fidelity correction at the existing creation boundary; the
persisted task payload shape is unchanged.

---

## 6. Tests and verification

- Focused suites: task source fidelity, NL creation, prepare-ahead, candidate
  contract, provider mesh, model tester, model selection, model UI, AI settings
  UX, runtime wiring, tool honesty glass — **all green**.
- **Full suite: `2090 passed, 24 skipped, 0 failed`** (previous tip: 2083
  passed; +6 new Issue 1 tests; two existing UI tests updated, one new Unicode
  sweep test added).
- `python -m py_compile` on every changed Python file — OK.
- `git diff --check` — clean.
- **Live Telegram verification: NOT performed** (no credentials in this
  workspace). The bio fix is source- and test-verified only; the end-to-end
  probe remains a live scheduled bio task followed by a `Taskloom → Task #N`
  inspection showing `bio_set_text` and an actual Telegram bio change.

---

## 7. Delivery record

| Item | Value |
|---|---|
| Starting HEAD | `dfeebf4643ef4b1724d82b58bf34c7bc7d7e1ba9` (== origin/main at start) |
| Change commit | `772390709061a84d8b8a585ff4fe488dd6039a16` — `fix: persist scheduled bio updates as the bio tool and de-emoji the AI panel` (8 files) |
| Push result | `dfeebf4..7723907  main -> main` (exit 0) |
| Remote HEAD verification | `git fetch origin` + `git rev-parse origin/main` == `772390709061a84d8b8a585ff4fe488dd6039a16` == local HEAD; `git show --stat origin/main` lists exactly the 8 phase files |
| Report commit | _pending_ |
| Working tree | pre-existing untracked stray clone `telegram-self-bot/` deliberately left untouched |
| Live Telegram verification | **NOT performed** (no credentials in this workspace) |
