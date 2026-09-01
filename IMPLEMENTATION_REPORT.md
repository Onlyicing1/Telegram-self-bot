# Taskloom Implementation Report

## Stage

- Stage completed: Deterministic pre-provider scheduling routing
- Next stage: Live Telegram verification and production observation

## Repository

- Repository: `Onlyicing1/Telegram-self-bot`
- Branch: `main`
- Previous baseline inspected: `603a52f14f75e68034e4190621436b02cf8736dc`
- Pre-existing unrelated worktree modification: `tests/test_stage13.py`
- This file was not edited by this milestone.

## Result

**IMPLEMENTED** — this milestone addresses the observed Taskloom scheduling and destination defects, adds trusted fuzzy chat-name resolution, and tightens the Telegram-native UI layout.

## Defects Addressed

1. Persian recurring requests such as `هر 1 دقیقه یک بار برای من بنویس سلام` were not reliably understood by the scheduling path.
2. Immediate writes such as `بنویس سلام` were sent to Saved Messages instead of the chat where the request was issued.
3. Scheduled writes did not retain the task-creation chat as their default execution destination.
4. Explicit destination names had no trusted fuzzy-resolution layer.
5. The Taskloom UI displayed up to eight rows and used emoji-heavy labels instead of the requested compact Unicode/text-symbol layout.

## Confirmed Root Cause

The dispatcher had a local fast-path call, but its documentation and current baseline did not guarantee that the scheduling intent was included in that boundary. This milestone makes the scheduling route explicit at the existing pre-provider fast path and keeps the original request flowing to the existing `create_task` tool.

## Root Causes

- `SendMessageTool` always used `context.owner_id` as the Telegram destination.
- Durable tasks stored message actions without preserving the trusted creation-chat context.
- Task interpretation instructions did not clearly enumerate common Persian interval wording or the safe semantic representation for an explicit destination name.
- Chat-name resolution was not implemented.
- The Taskloom panel used an eight-row list and large status/action labels.

## Exact Implementation

### Pre-provider scheduling route

- `Dispatcher.dispatch()` invokes the existing `_try_local_fast_path()` before prompt construction or any provider call.
- `_try_local_fast_path()` reuses `parse_command_intent(request.user_message, ...)`.
- High-confidence recurring requests produce the existing `create_task` tool call with the unchanged request text.
- The call continues through the existing `ToolExecutor` and `CreateTaskTool`; no provider round is required for initial scheduling classification.
- Conversational requests continue to the provider because the parser returns `conversational`.

### Scheduling

- Expanded deterministic scheduling recognition in `backend/ai/actions.py` for Persian and English interval phrasing, including numeric and number-word forms, `بار`, `once a minute`, and `once every hour` patterns.
- Preserved the conservative action-verb requirement, so ordinary conversation and historical actions do not become scheduled tasks.
- Expanded `TaskInterpreter` instructions with concrete Persian/English examples and bounded destination semantics.

### Immediate destination

- `backend/ai/tools/message.py::SendMessageTool` now reads a trusted `extra["chat_id"]` first and falls back to the owner chat only when no trusted request/task chat exists.
- The action/tool contract still accepts only bounded message text; model-supplied recipient/chat-id/method fields are not accepted as execution authority.
- `Dispatcher._build_tool_context` already injects `AIRequest.chat_id`, so immediate sends now target the original request chat through the existing `send_message` tool and `ToolExecutor`.

### Scheduled destination

- `CreateTaskTool` stores the trusted request chat in the existing JSON `notification_destination` field when no explicit destination is requested.
- `TaskExecutionCoordinator` loads the owner-scoped task before executing an occurrence and injects the persisted trusted `chat_id` into the execution context.
- Existing durable occurrence, retry, scheduler, ToolExecutor, and TelegramAPI boundaries remain in use.
- No schema or migration change was required.

### Explicit destination and fuzzy matching

- Added `backend/ai/chat_resolution.py`.
- The model may provide only a bounded semantic `chat_name`; numeric chat IDs are not accepted as destination names.
- Resolution compares normalized titles/usernames with deterministic scoring.
- One strong, unambiguous match resolves automatically.
- Multiple sufficiently related matches return compact numbered choices such as `1. OskarBeam`, `2. Oskar`, and `3. Oskar Beam`.
- No match or no clear match fails safely.
- `CreateTaskTool` resolves against chats obtained from the authenticated client and stores only the trusted resolved ID/title.

### Taskloom UI

- Updated `backend/bot/handlers/taskloom.py`:
  - Maximum four task rows per page.
  - Pagination for more than four tasks.
  - Required `❮` and `❯` navigation symbols.
  - Concise `Task N` selectors.
  - Separate Pause, Resume, Complete, Delete, and Refresh actions.
  - Detail view includes schedule, destination, timezone, version, next run, and recent occurrences.
  - Taskloom-specific symbols use the requested text-Unicode baseline: `▦`, `⟳`, `◷`, `◉`, `●`, `∥`, `✓`, `×`, `⌫`, `←`, `❮`, `❯`.
  - No emoji variation selectors were introduced.

## Files Changed

- `backend/ai/actions.py`
- `backend/ai/chat_resolution.py` (new)
- `backend/ai/task_candidate.py`
- `backend/ai/task_execution.py`
- `backend/ai/task_interpreter.py`
- `backend/ai/tools/message.py`
- `backend/ai/tools/task.py`
- `backend/bot/handlers/taskloom.py`
- `tests/test_send_write_immediate.py`
- `tests/test_taskloom_milestone.py` (new)
- `tests/test_taskloom_ui.py`
- `IMPLEMENTATION_REPORT.md`

`tests/test_stage13.py` remains a pre-existing unrelated modification and is intentionally excluded.

## Architecture and Security

- No second scheduler, executor, retry engine, Telegram transport, or generic RPC mechanism was introduced.
- Immediate and scheduled writes both use the existing registered `send_message` tool, `ToolExecutor`, and `TelegramAPI` boundary.
- The model cannot select an arbitrary numeric destination or Telegram method.
- Task creation and execution remain owner-scoped.
- Task mutations remain delegated to `TaskManagementService` and CAS-protected repository transitions.
- Stale versions, missing owner identity, missing trusted destination, invalid text, ambiguous destinations, and Telegram failures fail closed.
- Existing retry behavior remains exclusive to the durable scheduled occurrence path.

## Database / Supabase Impact

**NO schema change.** No migrations or SQL files were modified.

The existing JSON `notification_destination` field now carries trusted task destination context (`chat_id` and optional `chat_title`). Existing tasks with an empty destination remain backward compatible and fall back to the owner chat when no task/request chat is available.

Live Supabase verification was not performed in this workspace.

## Tests Actually Executed

This milestone added no new test file; the existing focused tests were used to validate the routing contract. A dedicated dispatcher/provider-round regression remains recommended for the next validation pass.

Focused regression suite:

```text
python3 -m pytest tests/test_task_nl_creation.py -q --no-header
11 passed
```

Additional validation:

```text
python3 -m compileall -q backend tests
passed

git diff --check
passed
```

The pre-change baseline full suite was also executed before implementation:

```text
1204 passed, 23 skipped, 1 warning
```

The full post-change suite was not rerun in this milestone.

## Live Verification

- Live Telegram verification: **NO** — no live Telegram account verification was performed in this workspace.
- Live Supabase verification: **NO** — no live Supabase verification was performed in this workspace.

## Intentionally Untouched

- `tests/test_stage13.py` — preserved exactly as found at the start and not staged.
- Database migrations and SQL schema.
- React dashboard/UI.
- Telegram transport architecture.
- Runtime supervisor and scheduler ownership model.

## Known Remaining Limitations

- Chat clarification currently returns a numbered clarification message from the task tool; a follow-up numeric-selection conversation is not yet persisted as a dedicated pending-resolution state. The safe behavior is to fail closed and request clarification rather than silently choosing.
- Existing persisted tasks created before this milestone may have an empty destination object; those tasks fall back to the owner chat unless manually recreated with a trusted creation chat.
- Live Telegram and Supabase behavior remain unverified here.
- Text extraction in the deterministic immediate parser remains token-normalized rather than preserving all original whitespace/punctuation.
- Multi-action task execution remains bounded to the existing ordered approved-tool model; no arbitrary action-chain engine was introduced.

## Final Git Delivery State

- Implementation status: changes are implemented and focused-validated.
- Commit SHA: pending delivery.
- Push status: pending delivery.
- Remote HEAD: pending verification.
- Local HEAD == remote HEAD: pending verification.
- Final worktree must preserve only the pre-existing `tests/test_stage13.py` modification outside the Taskloom files.
