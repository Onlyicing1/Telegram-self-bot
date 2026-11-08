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
* `backend/bot/handlers/delete.py`: `_progress_painter(event)` renders the staged
  progress panel described in §9 — measured scan counters, then the five-segment
  bar over resolved candidates — throttled to one edit per 3s, single-flight, and
  never able to fail the operation. The All confirmation wraps its preview in a
  5s `wait_for` and renders anyway ("preview unavailable") when it cannot be read.

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
* Progress is a same-panel edit every ≥3s, at most one in flight; a refused edit
  is swallowed and Telegram flood limits are not modelled beyond the per-edit
  timeout. The bar measures processed candidates, so a run stopped by the deadline
  shows its real percentage under the `Stopped` title, never completion.
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

---

## 9. Low-noise staged progress bar and honest rejected accounting (2026-11-08)

Baseline `c8e5e905`. Scope: the manual **All my messages** path only — the one
scope whose workload (≈10,000 messages) makes both the missing progress surface
and the ignored rejection count observable.

### 9.1 What the panel shows

Stage A — history scan. No percentage at all: the size of the chat is not known
until the scan ends, so any figure (including one derived from the 50,000-
message scan bound) would be invented.

```
Delete — All My Messages

Scanning chat...

Scanned: 4,000 messages
Your messages found: 2,850
```

Stage B — deletion, once the candidate count is known. Determinate, using the
existing five-segment renderer (`backend/helper/progress.py`, the same bar Save
and the model tester use):

```
Delete — Deleting

Processing 50%

▰▰▱▱▱

Deleted: 1,425 / 2,850
Rejected: 0
Remaining: 1,425
```

Stage C — the final report replaces the bar, through the panel's normal return
path (so it is painted even when every progress edit failed):

```
Delete — Completed

▰▰▰▰▰

Scanned: 10,000
Found: 2,850
Deleted: 2,850
Rejected: 0

All selected messages were processed.
```

A partial run gets the `Delete — Stopped` title, the same real counters, a
`Remaining:` line for the candidates that were never processed, the
`Delete did not finish: …` line when an exception ended it, and
`Partial result: <reason>.` The completion line appears only when every
candidate was resolved (deleted or rejected) AND nothing stopped the run. Titles
are plain Latin text: the panel renderer applies the persisted Glass UI font, so
no display-font glyph is hard-coded in the handler.

### 9.2 How the two progresses are computed

* Scan: `scanned` is the number of history messages the streaming iterator
  actually yielded; `found` (`report.considered`) is the number that passed
  ownership + range + content selection. Both are reported every
  `_DELETE_PROGRESS_EVERY` (200) messages, and `Selection.scanned` /
  `DeleteReport.scanned` carry the final figure to the report.
* Deletion: `percent = floor(100 × (deleted + rejected) / total)`, capped at 100,
  where `total` is the number of selected candidates. A candidate counts as
  **resolved** once it is deleted or rejected after a completed attempt, so the
  bar cannot advance for work that has not happened. `Remaining` is
  `total − resolved`; it is 0 only when everything was attempted.
* Deleted and rejected stay separate real counters the whole way through; the
  final `Deleted` figure is Telegram's own count, never the requested count.

### 9.3 Throttling (no edit spam)

`_progress_painter` keeps four cheap guards, all in the existing callback/panel
architecture — no scheduler, worker, task or second loop:

1. **Interval** — at most one paint per `_PROGRESS_PAINT_INTERVAL` (3s).
2. **Change required** — the rendered text is compared with the last painted
   text, so unchanged counters never produce an edit.
3. **Single flight** — a `busy` flag drops a paint while an earlier edit is in
   flight instead of queueing it; a slow Telegram edit therefore cannot build a
   backlog.
4. **Bounded, isolated edit** — each edit is wrapped in
   `asyncio.wait_for(..., _PROGRESS_EDIT_TIMEOUT)`, reduced from 5s to 3s so an
   edit cannot take longer than the interval it is reporting on. Any failure or
   timeout is logged at debug level and swallowed.

The terminal report is not a progress edit: it is the panel handler's return
value, so it is always attempted, even after a failed or timed-out progress edit.
Each candidate batch costs at most one paint attempt, and one paint attempt per
3s actually reaches Telegram.

### 9.4 Chunking and responsiveness: audit result

No batch size was changed. The audit found the pipeline already bounded and
cooperative:

* history is streamed (`_iter_messages_bounded`) and only selected integer ids
  are retained — no message objects accumulate;
* verification and deletion both run in `_DELETE_VERIFY_CHUNK` (100) batches,
  each awaited through `rpc_await` (which is a real `await`, so the event loop
  runs between batches); there is no `asyncio.gather` over deletion and no
  thread;
* the 20,000-candidate bound, the 50,000-message scan bound and the 240s
  operation deadline are unchanged.

The one measurable gap was presentation, not throughput: before this change the
panel showed nothing between the click and the final result. A new test runs a
2,000-message deletion beside a 1ms ticker and asserts the ticker advanced
(>5 turns), and another asserts every verification fetch and deletion batch is
≤100 ids with no id attempted twice. `MAX_DELETE_COUNT` and the batch size were
deliberately NOT raised: 100 is Telegram's practical batch size and larger
batches would lengthen each awaited RPC.

### 9.5 Rejected-count accounting

`do_del_self_filtered()` discarded `delete_verified_self_messages(...)`'s
rejected ids while `_on_batch()` already counted failed deletion batches. Adding
`len(_rejected)` on top would have counted every failed-delete id twice, because
that list is exactly (ownership rejections + failed-delete ids).

Fix: the chokepoint now reports the two kinds through two distinct callbacks.

* `on_rejected(chunk)` — fired once per verification chunk with the ids that
  failed ownership/verification (missing message, foreign or unresolvable
  sender, failed ownership fetch). `_on_rejected` adds `len(chunk)` to
  `report.rejected`.
* `on_batch(done, failed)` — fired per completed DELETION batch;
  `_on_batch` adds successes to `report.deleted` and failures to
  `report.rejected`.

Because an id can reach only one of the two, every rejection is counted exactly
once and the returned `_rejected` list is intentionally unused by this caller
(commented as such). Candidates that were never attempted — the deadline
stopped the operation — are counted nowhere and surface as `Remaining`, so they
are never reported as rejected. A partial exception still returns the ids that
were really deleted (the chokepoint's `deleted_ids`), and the panel reports that
exact figure with `Partial result`.

### 9.6 Tests

`tests/test_34_delete_all_execution.py` grew from 11 to 23 tests. Added:

| Property | Test |
| --- | --- |
| 10,000-message chat, streamed scan, real counters | `test_ten_thousand_message_chat_paints_real_scan_counters` |
| no fabricated scan percentage | same test (`"%" not in` every scan paint) |
| bar starts empty, advances only on resolved work, ends full | `test_progress_bar_starts_empty_and_fills_only_on_resolved_work` |
| ownership rejections counted once | `test_ownership_rejections_are_counted_exactly_once` |
| Telegram delete failures counted once | `test_telegram_delete_failure_is_counted_once` |
| unprocessed candidates are not rejections | `test_unprocessed_candidates_are_not_reported_as_rejected` |
| throttling: repeated paints inside the interval | `test_repeated_paints_inside_the_interval_edit_once` |
| unchanged counters never repaint | `test_unchanged_counters_never_repaint` |
| slow edit never stacks pending edits | `test_a_slow_panel_edit_never_stacks_pending_edits` |
| failed panel edit never aborts deletion | `test_a_failed_panel_edit_never_aborts_the_deletion` |
| batches never exceed 100 ids | `test_large_deletion_never_exceeds_the_chunk_size` |
| large deletion yields the event loop | `test_a_large_deletion_yields_control_to_the_event_loop` |
| foreign messages untouched in a large chat | `test_foreign_messages_stay_untouched_in_a_large_chat` |

The one pre-existing progress test was updated to the staged text
(`Scanning chat` / `Processing 0%` / `Deleted: n / total`) and
`tests/test_33_delete_manual_ui.py` now asserts the `Completed` title plus the
completion line. Every earlier ownership-isolation test is retained unchanged.

### 9.7 Results

```
delete suites (test_26 … test_34, test_task_durable_delete,
               test_10_tool_calls, test_19_ai_actions,
               test_colored_buttons, test_tool_health_audit) ... 299 passed
full suite .................................. 7197 passed, 27 skipped, 1 failed
py_compile (2 modules + 2 test files) ........ COMPILE_OK
git diff --check ............................ clean
```

The single failure is
`tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`
(`interval advancement exceeded bound`), which fails on the untouched baseline
`c8e5e905` as well; the scheduler is not touched by this diff.

### 9.8 Limitations / not live-verified

* No real 10,000-message Telegram deletion was performed. The fake client proves
  code behaviour (counters, throttling, batching, yields), not live Telegram
  throughput, FloodWait behaviour or edit latency.
* `_PROGRESS_EDIT_TIMEOUT = 3.0` is a judgement call: it is below the paint
  interval so an edit cannot outlive its own cadence, but a real FloodWait on
  edits (not modelled) is still swallowed rather than retried.
* The bar's granularity is one candidate, not one message: rejection detection
  and progress both advance per verification/deletion batch, so the displayed
  percentage can jump by up to 100 candidates at a time (the printed numbers are
  always exact).
* A run that hits the 20,000-candidate bound or the 240s deadline still needs
  another run; it reports the real counters and `Remaining`, never completion.

---

## 10. The Helper Bot owns the Delete panel (2026-11-08)

### 10.1 What was wrong

The Delete panel is an inline message the Helper Bot sent, but the progress and
result painter edited it through the owner's own account:

* `_edit_panel()` built `ExistingPanelEditor` with BOTH the inline identifier and
  `self_client=...`, and `ExistingPanelEditor` falls back to
  `self_client.edit_message(chat, msg)` whenever the inline edit is refused or
  absent — so the account re-edited a message it does not own;
* the painter read `event.inline_message_id`, a field a real `CallbackQuery`
  never sets (Telegram puts the identifier in `query.msg_id`), so it was always
  `None` and the self-client path was the one that actually ran;
* no panel operation token was taken, so a newer click on the same panel could
  not supersede a running deletion's writer.

### 10.2 The fix — Deep Save's exact convention

`_ProgressPainter` and `_edit_panel()` now do what
`_save_reply_wait_handler()` does:

1. `inline_message_id_from_event(event)` extracts Telegram's
   `InputBotInlineMessageID` from the callback (`query.msg_id`);
2. `begin_panel_operation(chat_id, msg_id)` claims the panel and returns the
   generation token that `ExistingPanelEditor` re-checks before every edit, so a
   newer interaction supersedes this writer instead of being overwritten;
3. ONE
   `ExistingPanelEditor(chat_id=…, msg_id=…, inline_id=…, helper=get_client(),
   self_client=None, generation=…)` is created per operation and reused for the
   scan, every deletion update and the terminal hand-over;
4. `finish_panel_operation(...)` releases the token in a `finally` when the run
   ends.

`self_client=None` is the enforcement point: the editor has no principal to fall
back to, so a refused inline edit simply stops painting — `self_client.
edit_message()` is never called, no replacement message is created, nothing is
sent into the chat, and no second editing mechanism exists. When the identifier
or the Helper Bot is missing the painter logs one bounded
`[DELETE_UI] panel edit path unavailable (inline_id=… helper=… chat=… msg=…)`
warning and the deletion keeps its own semantics (same chokepoint, bounds,
partial reporting). Input handlers receive the identifier from the pending entry
(`panels._handle_input` stores it) and skip the same way.

The terminal status is the callback's own paint of the SAME message: the handler
returns `(title, body, buttons)` and the router edits this panel through the
Helper Bot's event. No message is ever replaced or re-owned.

### 10.3 Tests (`tests/test_delete_panel_ownership.py`, 8 tests)

| Property | Test |
| --- | --- |
| every progress edit uses the Helper Bot | `test_every_progress_edit_uses_the_helper_and_the_callback_identifier` |
| the identifier passed is the callback's exact inline id | same test (`all(inline is INLINE_ID …)`) |
| the Self Client never edits the panel | same test (`client.self_edits == []`) |
| scanning, deleting and final status share one message | `test_scanning_deleting_and_final_status_share_one_panel_message` |
| a refused inline edit never falls back | `test_a_refused_inline_edit_never_falls_back_to_the_self_client` |
| no identifier ⇒ unavailable, deletion intact | `test_a_missing_inline_identifier_reports_unavailable_and_still_deletes` |
| no Helper Bot ⇒ unavailable, deletion intact | `test_a_missing_helper_reports_unavailable_and_still_deletes` |
| the panel token is released | `test_the_panel_token_is_released_when_the_operation_ends` |
| input results use the Helper Bot too | `test_input_result_edits_use_the_helper_too` |
| the chokepoint stays the only deletion path | `test_the_chokepoint_stays_the_only_deletion_path` |

`tests/test_33_delete_manual_ui.py` and `tests/test_34_delete_all_execution.py`
now install a fake Helper Bot and a real `InputBotInlineMessageID` on the fake
callback, record helper edits as panel edits, and record any self-client edit
separately so the forbidden path is observable — it stays empty.

### 10.4 Results

```
focused delete suites (test_26 … test_34, ownership, durable delete) ... 158 passed, 1 warning in 6.02s
full suite ........................................................... 1 failed, 7205 passed, 27 skipped, 3 warnings in 141.05s (0:02:21)
py_compile / compileall (2 modules + 3 test files) ................... exit 0
git diff --check ..................................................... clean
```

The single full-suite failure is still
`tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`,
which also fails on the untouched baseline `c8e5e905`; the scheduler is not
touched by this diff. These totals include §9's 23 All-path tests plus the 8
new ownership tests.

### 10.5 Not live-verified

No real Helper Bot, no real inline callback and no real `editInlineBotMessage`
call was exercised: the tests use a fake bot and a constructed
`InputBotInlineMessageID`, so they prove which principal the code addresses and
that the self-client path is never taken, not that Telegram accepts the inline
edit for a real panel. A live run must confirm the panel updates in place with
no second message and no "message can't be edited" error.
