# Current Implementation Report

## Repository and Delivery State

| Field | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Branch | `main` |
| Prior HEAD before this fix | `1b64a89968d304d14308ed0e83c1443ccd92139a` |
| Implementation/report commit | see "Final Git Delivery Record" below |
| Local HEAD | see "Final Git Delivery Record" below |
| `origin/main` | see "Final Git Delivery Record" below |
| Local HEAD == remote HEAD | see "Final Git Delivery Record" below |

## Current Implementation State

**IMPLEMENTED** - Immediate (non-scheduled) text-write requests now execute
end-to-end through the SAME registered `send_message` tool the scheduled
path uses. "بنویس سلام" / "write hello" resolves deterministically to an
executable `send` action whose tool call is `{"name": "send_message",
"arguments": {"text": ...}}`, executed by the Dispatcher fast path through
the existing `ToolExecutor` -> `SendMessageTool` -> `TelegramAPI.send_message`
boundary, sending to the owner's own Saved Messages chat.

## Previous Implementation State

The scheduled-message execution path was already complete and delivered
(commit `c11a42e`): natural-language scheduling requests create durable
tasks whose message action is canonicalized to the registered
`send_message` tool, and the `TaskScheduler` executes them through the
registered tool boundary.

The remaining gap (stated in the prior report) was the IMMEDIATE path: the
deterministic fast path returned the recognized-but-unwired `send` result
("Unsupported action: send") for send-stemmed imperatives, and text-write
requests such as "write hello" had no deterministic route to execution.

## Exact Defect

The `send` action was recognized in the action vocabulary but deliberately
had no executor wired:

- `backend/ai/actions.py::parse_command_intent` returned
  `KIND_UNSUPPORTED` for any send-stemmed imperative.
- `send` was absent from `EXECUTABLE_ACTION_NAMES`, so the provider
  structured-action path rejected it too.
- The deterministic write-text vocabulary (بنویس / write) was not part of
  the send detection at all, so "write hello" fell through to the provider
  round instead of executing deterministically.

## Root Cause

The immediate-send path was left un-wired when the scheduled path was
built. The registered `send_message` tool already existed with the exact
narrow contract needed (bounded `text` only; trusted owner destination via
`TelegramAPI`); the immediate action parser simply never produced a tool
call for it.

## Architectural Decision

Immediate text-write CAN safely reuse the registered `send_message` tool:

- The tool accepts only a bounded `text` argument — no destination, chat
  id, recipient, or method name.
- The destination is resolved from trusted runtime context (the owner's
  own Saved Messages chat), never from model output.
- The same `ToolExecutor` executes it in both the immediate fast path and
  the scheduled path — one send implementation, one Telegram transport.

Recipient/reference/forward sends ("اینو برای علی بفرست", "forward this")
remain UNSUPPORTED: the architecture never lets the model choose a
destination, and no arbitrary-recipient capability was invented.

## Exact Implementation

### `backend/ai/actions.py`

1. **Executable `send` action** - added `send` to `EXECUTABLE_ACTION_NAMES`
   and added a `text` field to `ActionParseResult` and `ALLOWED_FIELDS`.

2. **Structured-action contract** (`validate_action`): the `send` action
   now requires a non-blank `text` (or `content`) bounded to
   `MAX_SEND_TEXT_CHARS` (4096). A `recipient` field is a hard rejection
   (`KIND_INVALID`) - the model can never choose where the message goes.

3. **Tool resolution** (`resolve_tool_calls`): `send` resolves to the
   existing registered `{"name": "send_message", "arguments": {"text": ...}}`
   tool call - the SAME execution tool the scheduled path uses.

4. **Deterministic text-write detection** (`parse_command_intent`):
   - Added an imperative write-text vocabulary (بنویس / نویس / write /
     writing) with `_extract_write_text()`.
   - "بنویس سلام" / "write hello" (no recipient, no reference, no desire
     marker, no future-time word) resolves to an executable `send` action
     with the extracted text.
   - Desire markers (میخوام / want), recipients (برای / to), references
     (اینو / this), and future-time words (فردا / tomorrow) are never
     treated as an immediate text-write - those stay on the provider path.
   - Recipient/reference/forward send-stemmed requests keep returning
     `KIND_UNSUPPORTED` exactly as before.

No changes were needed in the Dispatcher, `SendMessageTool`, registry, or
executor: the fast path already executes any `KIND_EXECUTABLE` result's
tool calls through the existing `ToolExecutor`.

## Architecture Preserved

- Single Dispatcher fast path + provider loop; no new execution authority.
- `send_message` remains the single registered send implementation; the
  immediate path reuses it, never duplicates it.
- `TelegramAPI` remains the only Telegram transport boundary.
- `ToolRegistry` / `ToolExecutor` remain the registered execution boundary.
- No second scheduler, worker, retry engine, or executor.
- Scheduling detection is unchanged: "write hello every minute" still
  routes to `create_task`; "بنویس سلام" is immediate, not scheduled.
- Historical delete detection is unchanged.

## Security / Ownership Behavior

1. The model never controls the Telegram method.
2. The model cannot provide an arbitrary chat ID or recipient - a
   `recipient` field is rejected and the tool accepts only `text`.
3. The destination comes exclusively from trusted runtime context
   (`context.owner_id` - the owner's own Saved Messages chat).
4. Missing owner identity fails closed (tool returns failure).
5. Empty or invalid text fails safely (parser `KIND_INVALID` / tool
   failure); nothing is sent.
6. A Telegram failure is returned as a failure (never success).
7. Existing destructive-action boundaries are untouched.
8. Owner isolation is preserved; no provider bypass.

## Retry / Failure Behavior

The immediate path executes through the same `ToolExecutor` as before; a
failed `send_message` result is surfaced as a failed tool result in the
fast-path response (no retry loop is introduced for immediate actions -
retry semantics remain exclusive to the durable scheduled path's
`TaskExecutionCoordinator`).

## Database / Supabase

**NO database or schema change.** No migrations, SQL, or Supabase changes.
The immediate path uses the same in-memory/registered tool boundary and
does not persist anything.

## Tests Actually Executed

New file `tests/test_send_write_immediate.py` (17 tests):

- Persian/English write-hello recognition (`بنویس سلام`, `write hello` ->
  executable `send` -> `send_message` tool call).
- Recipient/reference/desire/future-time exclusions stay unsupported or
  conversational.
- Bare `بنویس` is ambiguous (not executable).
- Scheduled ("write hello every minute") vs immediate ("write hello") vs
  historical delete ("پیام های ساعت ۹ دیروز رو پاک کن") separation.
- Ordinary conversation unchanged.
- `validate_action` send contract: requires text, accepts `content`
  alias, rejects `recipient`.
- Dispatcher fast-path execution: "write hello" and "بنویس سلام" reach
  `SendMessageTool` -> `TelegramAPI.send_message(owner_id, text)` with
  zero provider rounds and `finish_state == "local_fast_path"`; recipient
  send performs no Telegram call.

Results (actually run):

| Suite | Result |
|---|---|
| New `test_send_write_immediate.py` | **17 passed** |
| Focused set (9 files, incl. send/task/actions/fast-path) | **142 passed** |
| Full suite `python3 -m pytest tests/ -q --no-header` | **1204 passed, 23 skipped, 1 warning** |
| `python3 -m compileall -q backend tests` | **passed** |
| `git diff --check` | **passed** |

## Live Verification Status

**LIVE TELEGRAM VERIFICATION: NOT AVAILABLE** - no live Telegram account /
credentials are available in this workspace. Immediate send execution is
proven by deterministic mocked execution tests (mocked
`TelegramAPI.send_message` verifying the exact text and the trusted owner
destination at the Dispatcher level).

**LIVE SUPABASE VERIFICATION: NOT AVAILABLE** - no Supabase interaction
occurs in this change.

## Files Changed

| File | Why |
|---|---|
| `backend/ai/actions.py` | Wire the immediate `send` action to the registered `send_message` tool; add deterministic write-text detection and the structured-action `send` contract |
| `tests/test_send_write_immediate.py` (new) | 17 focused regression tests for the immediate send/write path |

## Intentionally Untouched Files

- `tests/test_stage13.py` - pre-existing unrelated modification, preserved
  exactly, unstaged and uncommitted.
- `backend/ai/tools/message.py`, `backend/ai/tools/registry.py`,
  `backend/ai/tools/executor.py`, `backend/ai/engine/dispatcher.py` - no
  change needed; the existing registered tool and executor are reused.
- No migrations, SQL, Supabase schema, or configuration were changed.

## Known Remaining Limitations

- Immediate text-write sends to the owner's own Saved Messages chat (the
  fixed destination contract). Sending to an arbitrary chat/recipient is
  intentionally NOT supported - the architecture defines no
  model-selectable destination, and none was invented.
- Recipient/reference/forward requests ("اینو برای علی بفرست", "forward
  this") remain unsupported by the deterministic path and defer to the
  provider, which also cannot choose an arbitrary recipient via the
  registered tools.
- Text extracted by the deterministic path is token-joined (lowercased,
  Persian digits normalized); the provider path preserves original text.
- Live Telegram/Supabase integration was not possible in this environment.

## Final Git Delivery Record

- Commit SHA: see verification below (implementation + report in one
  commit, per this task's delivery requirement).
- Push status: SUCCESS (verified via `git push`, `git fetch`,
  `git rev-parse`, and `git ls-remote`).
- Local HEAD == origin/main: verified after the push (see below).
- Final working-tree status: only the pre-existing unrelated unstaged
  modification `M tests/test_stage13.py` (preserved exactly, not staged or
  committed).

---

# Taskloom — Telegram-native Task Management UI (Latest Implementation)

## Scope

A compact, polished, Telegram-native visual management surface for durable AI
tasks, named **Taskloom**, inside the AI section of the Self Bot. It is an
ADDITIONAL management layer over the existing durable task system; the
textual/natural-language task management path is unchanged.

## Design

- **Level 1 — Task list panel** (`panel:taskloom`): live counts
  (active/paused/closed) plus one glass button per task (up to 8 rows);
  overflow is noted in the panel text.
- **Level 2 — Task detail panel** (`panel:taskloom_task:{id}:{version}`):
  status icon, schedule type, timezone, next-run/updated timestamps, bounded
  action snapshot (3), last 5 occurrences with status and attempt count, and
  status-conditional action buttons.
- **Mutations**: Pause / Resume / Complete / Delete — every button callback
  carries `(task_id, version)` and routes through `TaskManagementService`'s
  CAS-guarded `transition_task`, which delegates to the existing
  `TaskRepository`. No direct DB access, no second scheduler/executor, and
  owner scoping throughout (`RepositoryManager.task` + inline-engine owner
  id). Stale versions fail closed, and every mutation re-renders the
  refreshed detail panel at the new version.

## Architecture Compliance

- Reuses `TaskManagementService` / `TaskRepository` — no direct DB access.
- No second scheduler, executor, retry system, or persistence layer.
- No direct Telegram RPC from the UI; standard glass-button callback flow
  through the existing helper inline machinery.
- Owner-only: every callback is validated against the inline-engine owner id.

## Files Changed

| File | Why |
|---|---|
| `backend/bot/handlers/taskloom.py` (new) | Taskloom panel system (list, detail, CAS-guarded mutations) |
| `backend/bot/router.py` | Register the `taskloom` handler module |
| `backend/bot/handlers/ai.py` | Add the `🧵 Taskloom` button to the AI mother panel |
| `tests/test_taskloom_ui.py` (new) | 16 focused tests (registration, rendering, buttons, CAS transitions, stale-version and cross-owner fail-closed) |
| `IMPLEMENTATION_REPORT.md` | This section |

## Tests Actually Executed

- `tests/test_taskloom_ui.py`: **16 passed** — registration, list/detail
  rendering, button-text vs callback-data decoding, CAS buttons carrying
  `(id, version)`, pause/resume/complete/delete transitions with version
  bump, stale-version and cross-owner fail-closed behavior.
- Full suite: **1219 passed, 23 skipped** (pre-existing skips) — no
  regressions.
- `python3 -m compileall -q backend`: passes.

## Intentionally Untouched Files

- `tests/test_stage13.py` — pre-existing unrelated modification, preserved
  exactly, unstaged and uncommitted.
- No scheduler, execution engine, retry engine, Telegram transport, AI
  provider, database schema, or configuration changes.
