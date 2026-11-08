# Implementation Report — Delete repair and expansion

Base: `809d582` (`origin/main`). All numbers below were produced in this workspace
(Python 3.12.3, Telethon 1.43.0, pytest 9.2.0) against the final tree.

## 1. Manual UI (`backend/bot/handlers/delete.py`, rewritten)

Chat resolution: `_chat_for(event)` uses, in order, the callback message's chat,
the inline session, and the message-id session. It returns `0` only when nothing
knows the chat, and that case is shown as `No chat context` — never a guessed id and
never `TargetContext` for panel work. The reply target is used only by the explicit
reply mode.

Panel home now offers: `Last 10 / 25 / 50 / 100 / 250 / 500`, `Custom amount...`,
`All my messages`, `Delete from a message...`, `Enter a message ID...`,
`Delete by date range...`, plus reply mode and Back.

`Delete From` = live preview pages (`_delfrom_recent_page`, 8 messages per page,
`Previous`/`Next`), each entry showing id, timestamp and a truncated text/type
preview. Picking one opens `_delfrom_pick`, which distinguishes
`Delete this one message` from `Delete from this message forward`, and each choice
goes through `_delfrom_confirm` (green confirm button, Back) before
`_delfrom_exec`. "Forward" means newer messages (higher ids), matching the existing
`min_id` semantics used by `do_del_id_counts`.

Results are painted by `_edit_panel()`, which tries the helper inline panel and
falls back to the self client, so the typed-count path can no longer delete silently
while showing nothing. `_delete_command_message()` removes the typed command via the
self client (with honest failure logging).

Date range: `del:range` accepts `from..to` / `from-to` / two space-separated values;
`split_range()` only splits, and the values are parsed by the existing
timezone-aware `delete_service.parse_time_range` with `_configured_tz()` — the same
`TZ` source as `backend/config.py`. No second parser exists.

## 2. Service (`backend/services/delete_service.py`)

* `DeleteReport`: `requested`, `considered`, `deleted`, `rejected`, `complete`,
  `reason`, `deleted_ids`, `rejected_ids`, `scope`. It is filled from real Telegram
  results and is attached to every path (manual, tool, semantic).
* `Selection` dataclass returned by `select_self_owned_message_ids()`.
* `_iter_messages_bounded(client, chat_id, *, deadline, stats)` streams history with
  a limit, an optional deadline, and counters — memory stays proportional to the
  selected ids, not to the chat.
* The 500 cap is gone from `select_self_owned_message_ids`, `do_del_n_counts`,
  `do_del_last_n_real`, `do_del_n`, `do_del_id`, and the AI layers. Counts up to
  `MAX_DELETE_COUNT = 20000` are accepted; the scan stops at
  `_MAX_DELETE_SCAN_MESSAGES = 50000` or `_DELETE_OPERATION_DEADLINE_SECONDS = 240`,
  and either stop is reported as partial with a reason, never as success.
* `delete_verified_self_messages()` keeps `_DELETE_VERIFY_CHUNK = 100` and
  `_DELETE_RPC_TIMEOUT_SECONDS = 5.0`; it gained an `on_batch` callback so completed
  batches survive cancellation/deadline, and its `RuntimeError` now carries
  `deleted_ids` / `rejected_ids`.
* New helpers: `preview_self_owned_count()` and `recent_self_message_previews()`
  back the All-mode preview and the Recent pages; both are read-only.

## 3. AI Delete (`backend/ai/tools/delete.py`, `semantic.py`, `actions.py`)

* `DeleteTool` schema/validation use `MAX_DELETE_COUNT` (lazy import, so the tool
  layer keeps no private copy of the number) instead of 500.
* Tool deadlines: `_DELETE_TOOL_TIMEOUT_SECONDS = 180`,
  `_DELETE_OPERATION_TIMEOUT_SECONDS = 150`; the executor's outer `wait_for`
  remains. Timeouts and stopped batches now report the real `deleted` count with
  `partial: True` and a `DELETE_PARTIAL` log line.
* `DeleteByIdTool` reports partial deletion instead of `count: 0`.
* Semantic: `_MAX_CANDIDATES = 200`, `_MAX_DELETE_IDS = 500`. Ids outside the
  verified window, malformed ids and non-positive ids are returned in `rejected`
  instead of disappearing; a mid-operation failure returns the ids that were
  actually deleted.
* The semantic contract is untouched: the model may only pass ids it received from
  `list_recent_messages`, and every id is re-fetched and ownership-checked by the
  chokepoint.
* `actions._MAX_DELETE_COUNT = 20000` mirrors the service (equality asserted in
  tests).

## 4. Limits and timeouts (final)

| Bound | Value | Why |
| --- | --- | --- |
| `MAX_DELETE_COUNT` | 20000 | candidates per operation; a memory/UX bound, not a UI ceiling |
| `_MAX_DELETE_SCAN_MESSAGES` | 50000 | history scan bound; reported as partial when hit |
| `_DELETE_OPERATION_DEADLINE_SECONDS` | 240 | deliberate operation deadline |
| `_DELETE_RPC_TIMEOUT_SECONDS` | 5.0 | per-RPC protection (unchanged) |
| `_DELETE_VERIFY_CHUNK` | 100 | ownership verification/deletion batch (unchanged) |
| `_DELETE_PREVIEW_SAMPLE` | 200 | preview sampling |
| AI tool / operation | 180 / 150 | tool-call bounds, partial reporting on expiry |
| semantic candidates / ids | 200 / 500 | bounded model windows |

## 5. Tests

New suite `tests/test_33_delete_manual_ui.py`, 27 tests driving real production
functions (real `_del_panel_handler` / `_delfrom_*` / input handlers, real
`panels._handle_input` and `register_input_listener`, real service deletion against a
fake Telethon client):

| Requirement | Test |
| --- | --- |
| 1 manual numeric executes | `test_numeric_input_executes_the_expected_deletion` |
| 2 numeric above 500 | `test_numeric_input_supports_values_above_500` |
| 3 recent works without reply ctx | `test_recent_messages_work_without_reply_target_context` |
| 4 pagination | `test_recent_messages_paginate` |
| — no guessed chat | `test_recent_page_requires_the_chat_the_panel_lives_in` |
| 5 single selection | `test_selecting_single_message_deletes_only_that_message` |
| 6 boundary range | `test_selecting_boundary_deletes_only_the_intended_self_range` |
| 7 message id without reply ctx | `test_manual_message_id_input_works_without_reply_context` |
| — foreign/malformed id input | `test_manual_message_id_input_rejects_foreign_and_malformed_ids` |
| 8 all beyond 1000 | `test_all_mode_scans_beyond_1000_messages` |
| 9 all deletes owner only | `test_all_mode_deletes_only_authenticated_owner_messages` |
| 10 >500 bounded batches | `test_more_than_500_messages_are_processed_in_bounded_batches` |
| 11 partial failures honest | `test_partial_batch_failure_is_reported_honestly` |
| 12 no duplicate on timeout | `test_timeout_does_not_duplicate_deletion_or_claim_completion` |
| 13 deadline / no false success | `test_operation_deadline_reports_partial_history_scan` |
| 14 date range timezone | `test_date_range_respects_the_configured_timezone` |
| — no second parser | `test_split_range_does_not_parse_times_itself` |
| 15 semantic ids from history | `test_semantic_ids_must_come_from_the_history_tool` |
| 16 expanded semantic batch | `test_semantic_delete_supports_the_expanded_batch` |
| 17 rejected ids | `test_foreign_missing_and_malformed_ids_remain_rejected` |
| 18 chokepoint mandatory | `test_every_manual_path_reaches_the_ownership_chokepoint` |
| 19 replied deletion | `test_replied_message_deletion_remains_correct` |
| 20 AI-authored messages eligible | `test_ai_generated_self_messages_remain_eligible` |
| 21 AI silence contract | `test_ai_delete_silence_contract_is_intact` |
| — UI completeness | `test_home_panel_offers_every_required_quick_action` |
| — confirmation required | `test_quick_and_all_require_explicit_confirmation` |
| — one bound for AI and manual | `test_ai_and_manual_share_one_count_bound` |

Existing tests updated only where they asserted the removed ceiling or old buttons:
`test_30_delete_timeout_hardening.py` (scan limit is now the service constant),
`test_32_semantic_delete.py` (same), `test_10_tool_calls.py` and
`test_19_ai_actions.py` (oversized count is `MAX_DELETE_COUNT + 1`),
`test_tool_health_audit.py`, `test_colored_buttons.py` (new Delete labels).

## 6. Exact results

```
tests/test_33_delete_manual_ui.py .......................... 27 passed
focused batch (test_10, test_19, test_colored_buttons, test_33, test_27–test_32,
               test_14_tool_honesty_glass, test_tool_health_audit,
               test_saved_items_ai_management, test_17) .... 317 passed in 10.33s
full suite ........................................... 7174 passed, 27 skipped,
                                                       1 failed in 136.00s
py_compile (5 changed modules) ........................ COMPILE_OK
git diff --check ...................................... clean
```

The single full-suite failure is
`tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`,
which was already failing on the untouched base commit before any edit here
(`interval advancement exceeded bound` in `task_scheduler`). It is unrelated to
Delete and was left untouched per scope control.

## 7. Follow-up fix (live failure): manual "All my messages"

The first implementation made All reachable but not safe to run: the owner's live
test hung and deleted nothing.

### Confirmed root causes

1. `_del_view("allexec")` called `do_del_self_filtered()` with no deadline, and the
   service only honoured a caller-supplied one, so the 50,000-message scan and
   every verification/deletion batch ran with no total bound. A 5s per-RPC
   timeout is not an operation deadline: ~200 verification RPCs plus ~200 delete
   RPCs could occupy the better part of an hour while the panel sat on the click.
2. `do_del_self_filtered()` returned as soon as `selection.error` was set,
   discarding the ids the scan had already verified — zero deletions after any
   history error.
3. `MAX_DELETE_COUNT` was documented as the per-operation candidate bound but
   enforced only when `count` was passed; All mode could collect the whole scan
   bound (50,000 ids) before deleting.
4. `preview_self_owned_count()` was awaited inside the panel handler with only the
   per-RPC 5s timeout per sampled message, so the confirmation panel could take
   minutes to appear (200 × 5s worst case).
5. Nothing told the owner work had started: the callback was acked, then the panel
   stayed unchanged for the whole operation.

### Exact code changes

* `delete_service.do_del_self_filtered`: establishes
  `deadline = time.monotonic() + _DELETE_OPERATION_DEADLINE_SECONDS` when the caller
  passes none (a shorter AI deadline still wins) and applies it to BOTH selection
  and the ownership chokepoint.
* `delete_service.delete_verified_self_messages`: keyword-only `deadline` and
  `stopped` dict. Every verification chunk and delete batch is preceded by a
  monotonic check; on expiry the remaining ids are left untouched (never reported
  as ownership rejections) and `stopped["reason"]` records why. An awaitable
  `on_batch` result is awaited, so batch-boundary progress works without a second
  task or loop.
* `delete_service._iter_messages_bounded`: the per-page RPC timeout is clamped to
  the operation's remaining budget.
* `delete_service.select_self_owned_message_ids`: enforces `MAX_DELETE_COUNT` when
  `count is None`, returning `complete=False` with `candidate limit (N) reached`,
  and reports `scanned`/`considered` through a new `progress` callback every
  `_DELETE_PROGRESS_EVERY` messages.
* `do_del_self_filtered`: a failed scan no longer discards verified ids — they are
  processed through the chokepoint inside the remaining budget and the scan error
  is returned so the panel reports a partial result. A scan that produced no ids
  at all still attempts no deletion (unresolved identity, missing boundary, etc.).
* `do_del_id_counts`: passes its existing deadline/stop record into the chokepoint.
* `preview_self_owned_count`: its own 3s wall clock (`_DELETE_PREVIEW_SECONDS`).
* `backend/bot/handlers/delete.py`: `_progress_painter(event)` paints a throttled
  (3s), individually bounded (5s) `Working — scanned N · M of yours verified ·
  K deleted` status on the same panel, starting before the scan and never able to
  fail the operation. The All confirmation wraps its preview in a 5s `wait_for`
  and renders anyway ("preview unavailable") when it cannot be read.

### Tests added

`tests/test_34_delete_all_execution.py` — 11 tests using the real panel handler and
real service against a deterministic fake client: self-applied total deadline; slow
history returns a partial result; a mid-scan error still deletes the verified ids;
a failure before any candidate deletes nothing; the candidate bound; the history
scan bound; a deadline mid-deletion keeps real counts; no id is deleted twice;
foreign messages stay untouched on the error path; the preview is bounded and still
confirms; progress is painted and the final result is exact.

`tests/test_33_delete_manual_ui.py::test_every_manual_path_reaches_the_ownership_chokepoint`
now dates its messages into the range, because a scope with zero candidates
legitimately performs no deletion and therefore calls no chokepoint.

### Results

```
tests/test_34_delete_all_execution.py ................. 11 passed in 1.90s
focused batch (test_27–test_34, tool/AI/colored/saved) ... 309 passed in 8.36s
full suite ..................... 7185 passed, 27 skipped, 1 failed in 137.01s
py_compile (both changed modules) ...................... COMPILE_OK
git diff --check ...................................... clean
```

The one failure is
`tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`,
which failed on the untouched base commit before any Delete edit; the scheduler is
not touched by this diff.

### Remaining limitations

* The deadline bounds one operation to 240s of wall clock. A chat with more than
  `MAX_DELETE_COUNT` (20,000) of the owner's messages needs more than one run, and
  each run says so honestly instead of pretending it finished.
* Progress is a same-panel text edit every ≥3s; a refused edit is swallowed and
  Telegram flood limits are not modelled beyond the per-edit timeout.
* Only the All path gained the new deadline/partial/progress coverage; the
  quick-count, Delete From, message-ID, date-range and AI paths keep their existing
  suites, which still pass.

## 8. Not verified against live Telegram

Nothing here has been exercised against a real account: no real self-client, no real
`messages.deleteMessages` permission check, no real large-chat history, and no real
callback timing. Chat resolution, the deadline, partial handling and progress are
verified only by the fake-client tests above.

A live smoke test is still required after deploy: run All on a chat with more than
1000 owner messages and confirm the panel shows the Working line, ends with real
deleted/rejected counts, a second run continues from what remains, and no message
from another participant is ever deleted.
