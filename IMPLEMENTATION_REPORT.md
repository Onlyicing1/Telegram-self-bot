# Current Implementation Report

## Repository and Delivery State

| Field | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Branch | `main` |
| Prior HEAD before this fix | `280357f4a0fa9f018f94456e4b406f7be1b0490a` |
| Implementation commit | `c11a42e17ee913d51c113a45c3e96690e12b3ca6` |
| Report commit | this documentation commit |
| Local HEAD | see "Final Git Delivery Record" below |
| `origin/main` | see "Final Git Delivery Record" below |
| Local HEAD == remote HEAD | see "Final Git Delivery Record" below |

## Current Implementation State

**IMPLEMENTED** - Durable scheduled message execution is now end-to-end
executable. A natural-language scheduling request ("هر 1 دقیقه یک بار برای
من بنویس سلام") creates a durable task whose message-writing action is
canonicalized to the registered `send_message` tool; the scheduler executes
that action through the existing `ToolExecutor` / `TelegramAPI` boundary,
and the occurrence persists the real outcome (succeeded / retry_pending /
failed under the existing retry policy).

## Exact Defect

The previous implementation fixed natural-language task CREATION and
durable persistence, but the persisted message-writing action had no
registered execution tool. A scheduled task such as "every 1 minute write
hello" could be created successfully, then fail at execution time with
`unregistered_action` when the `TaskScheduler` claimed its occurrence and
`TaskExecutionCoordinator` checked the action name against the tool
registry. The tool registry exposed no generic send tool.

## Root Cause

The task-execution chain requires every layer to agree on the same action
name and payload contract:

```text
candidate action (model-produced name/arguments)
  -> TaskCandidate validation (name non-empty only)
  -> persisted in ai_tasks.actions
  -> occurrence action_snapshot
  -> TaskExecutionCoordinator: registry lookup
  -> ToolExecutor -> Tool -> service -> TelegramAPI
```

Two gaps existed:

1. **No registered message-send tool.** The `ToolRegistry` (built by
   `create_default_registry`) had no send/message tool, so the coordinator's
   registry check failed for any message-writing action. The only Telegram
   side-effect path was the task-outcome notification sender
   (`TelegramAPI(self.client).send_message(owner, message)`), which was not
   exposed to task-action execution.
2. **Action names were not canonicalized at creation.** The interpreter's
   model emits the action name freely (e.g. `send` with `content`), and
   `TaskCandidate.from_untrusted` only required a non-blank name. Creation
   and execution therefore could disagree on the action contract, and a
   model could even smuggle destination/method fields into the persisted
   action.

## Exact Implementation

### 1. Registered deterministic message-send tool (new)

`backend/ai/tools/message.py` - `SendMessageTool`:

- Registered as `send_message` in `create_default_registry` and visible to
  providers only through the existing registry.
- Accepts ONLY a bounded `text` argument (1..4096 chars). No destination,
  chat id, recipient, method name, or RPC instruction is accepted.
- Resolves the destination from TRUSTED runtime context: the owner's own
  Saved Messages chat (`context.owner_id`), the same destination the
  existing task-outcome notification sender uses. Never from model output.
- Performs the actual Telegram operation through the existing
  `TelegramAPI.send_message` facade (`backend/telegram_api/messages.py`),
  which owns bounded timeouts, flood-wait handling, and structured errors.
- A Telegram failure returns `ToolResult(success=False, ...)`; it flows
  through the existing `TaskExecutionCoordinator.handle_failure` retry
  boundary. It is never reported as success.

### 2. Canonical action contract at creation

`backend/ai/task_candidate.py` - `TaskCandidate.from_untrusted` now
normalizes every action through `_canonicalize_action`:

- Message-send aliases (`send`, `send_message`, `write_message`,
  `send_text`) normalize to the registered `send_message` tool.
- Text aliases (`text`, `content`, `message`, `body`) normalize to the
  single `text` argument; the text must be non-blank and <= 4096 chars,
  otherwise the candidate is rejected (nothing is created).
- Any injected destination/chat/recipient/method fields are stripped and
  never persisted.
- Non-send actions are preserved unchanged; unknown action names still fail
  safely at execution time via the coordinator's registry check.

### 3. Interpreter guidance

`backend/ai/task_interpreter.py` - the candidate instructions now tell the
model to use exactly the action name `send_message` with a single `text`
argument for any message-writing action, and never to include owner
identity, chat ids, or destinations.

### 4. Registry / executor consistency

- `backend/ai/tools/registry.py`: registered `SendMessageTool`.
- `backend/ai/tools/executor.py`: added the `send_message` status label.
- `tests/test_20_advanced_execution.py`: refined the "no arbitrary
  Telegram/exec tools" invariant. `send_message` is the single deliberate,
  bounded, fixed-destination exception; the test now asserts the tool
  exists, exposes only the `text` parameter, and stays READ_ONLY/READ_WRITE.
  All arbitrary-RPC/exec/shell/file/secret names remain forbidden.

## Architecture Preserved

- Single `TaskScheduler` polling loop; no second scheduler or worker.
- `TaskExecutionCoordinator` remains the execution/failure authority.
- `TaskRepository` remains the durable persistence/CAS authority.
- `ToolRegistry` / `ToolExecutor` remain the registered execution boundary;
  the new tool is executed only through the executor.
- Existing retry policy (`FailureClass`, `classify_failure`, `can_retry`,
  `retry_delay`, `MAX_ATTEMPTS=3`) reused; no duplicate retry logic.
- Existing `TelegramAPI` facade reused; no parallel Telegram transport, no
  arbitrary Telegram RPC, no shell/SQL/persisted-code execution.
- Owner identity comes from trusted runtime context only.
- Destructive actions unchanged; `send_message` sends only to the owner's
  own Saved Messages chat.

## Retry / Failure Behavior

- A retryable Telegram failure (`TelegramTimeoutError` etc.) transitions the
  occurrence to `retry_pending` with `attempt + 1` and the existing backoff.
- Attempts stay bounded by `MAX_ATTEMPTS = 3`; the third failed attempt
  transitions the occurrence to terminal `failed`. No fourth attempt is
  possible.
- A successful send transitions the occurrence to `succeeded` with bounded
  result metadata (`terminal_status: "succeeded"`).

## Security / Ownership

- Destination is fixed to the owner's own chat; the model can never supply
  a chat id, recipient, or Telegram method.
- Candidate validation strips injected destination fields at creation.
- The tool verifies owner identity from context and refuses to run without
  it.
- No arbitrary Telegram RPC, no arbitrary method dispatch, no shell, no SQL,
  no persisted-code execution, no provider bypass.

## Database / Supabase

- **No schema change.** No new tables, columns, or migrations.
- The canonical `send_message` action persists in the existing
  `ai_tasks.actions` jsonb array (bounded by existing payload constraints).
- Supabase behavior is unchanged; the existing Supabase-or-fallback
  repository pattern applies.

## Tests Actually Executed

New file `tests/test_task_send_execution.py` (15 tests):

1. `test_send_alias_canonicalized_to_registered_tool`
2. `test_send_message_alias_with_text_preserved`
3. `test_injected_destination_is_stripped_not_persisted`
4. `test_empty_send_text_rejects_candidate`
5. `test_non_send_actions_unchanged`
6. `test_send_message_is_registered_in_default_registry`
7. `test_executor_dispatches_send_message_to_owner`
8. `test_send_message_tool_ignores_argument_destination`
9. `test_send_message_tool_rejects_missing_text`
10. `test_send_message_tool_fails_without_owner_context`
11. `test_create_task_tool_persists_canonical_send_message_action`
12. `test_scheduler_executes_scheduled_message_and_persists_success`
13. `test_send_failure_reaches_retry_boundary_then_terminal_failure`
14. `test_ordinary_conversation_is_not_scheduled`
15. `test_immediate_send_is_not_a_scheduled_task`

Results (actually run):

| Suite | Result |
|---|---|
| New `test_task_send_execution.py` | **15 passed** |
| Task/actions focused set (16 files) | **149 passed** |
| `tests/test_20_advanced_execution.py` | **32 passed** |
| Full suite `tests/` | **1187 passed, 23 skipped, 1 warning** |
| `python3 -m compileall -q backend tests` | **passed** |
| `git diff --check` | **passed** |

## Live Verification Status

**LIVE TELEGRAM VERIFICATION: NOT AVAILABLE** - no live Telegram account /
credentials are available in this workspace, so no real Telegram send was
performed. Execution correctness is proven by deterministic mocked
execution tests (mocked `TelegramAPI.send_message` verifying the exact
text and the trusted owner destination).

**LIVE SUPABASE VERIFICATION: NOT AVAILABLE** - persistence is verified
against the in-memory repository; the Supabase path is unchanged and was not
exercised against a live database.

## Files Changed

| File | Why |
|---|---|
| `backend/ai/tools/message.py` (new) | Registered deterministic `send_message` execution tool |
| `backend/ai/task_candidate.py` | Canonicalize message-send actions; strip injected destinations |
| `backend/ai/task_interpreter.py` | Instruct the model to emit the canonical `send_message`/`text` action |
| `backend/ai/tools/registry.py` | Register `SendMessageTool` |
| `backend/ai/tools/executor.py` | Add `send_message` status label |
| `tests/test_task_send_execution.py` (new) | 15 focused regression tests for the complete execution path |
| `tests/test_20_advanced_execution.py` | Refine the arbitrary-tool invariant for the deliberate bounded exception |

## Intentionally Untouched Files

- `tests/test_stage13.py` - pre-existing unrelated modification, preserved
  exactly, unstaged and uncommitted.
- `backend/ai/task_scheduler.py`, `backend/ai/task_execution.py`,
  `backend/ai/retry.py`, `backend/ai/database/task_repository.py`,
  `backend/ai/actions.py` - no change needed; the existing execution/retry/
  persistence boundaries were reused as-is.
- No migrations, SQL, Supabase schema, or configuration were changed.

## Known Limitations

- Scheduled message execution sends to the owner's own Saved Messages chat
  (the existing notification destination). Sending to an arbitrary chat is
  intentionally not supported - it would require a destination model the
  current architecture does not define.
- Immediate (non-scheduled) "send/write" requests remain unchanged: the
  deterministic fast path still returns the recognized-but-unwired `send`
  result, and the provider path may now use `send_message` (owner's Saved
  Messages only) if it chooses to call the registered tool.
- No schema change was needed; the existing schema is sufficient.

## Final Git Delivery Record

- Implementation commit: `c11a42e17ee913d51c113a45c3e96690e12b3ca6`
- Push status: SUCCESS (verified via `git push`, `git fetch`,
  `git rev-parse`, and `git ls-remote`)
- Local HEAD == origin/main: see the verification note below
- Final working-tree status: only the pre-existing unrelated unstaged
  modification `M tests/test_stage13.py` (preserved exactly, not staged or
  committed).

> Verification note: this report is committed after the implementation
> push; the "Final Git Delivery Record" fields (report commit SHA, final
> local/remote HEAD equality) are recorded with the actual values after the
> report push completes.
