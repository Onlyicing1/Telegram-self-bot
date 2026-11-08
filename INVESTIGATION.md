# Investigation — Delete feature: manual reliability and the 500-message ceiling

Snapshot inspected: `809d582` (`origin/main` at the start of the task).
Everything below is verified against that source, not against a prompt description.

## 1. How a manual delete actually flows

Panel button → callback router (`backend/bot/handlers/callback_gate.py`) →
the *registered panel handler* (not the inline builder) →
`panel:*` / `input:*` routing → `backend/bot/handlers/delete.py` →
`backend/services/delete_service.py` → `delete_verified_self_messages()` →
Telethon → panel repaint.

The old Delete panel had exactly two entries (`input:del:n`, `panel:delfrom`), so
"Delete From" was the only scope picker, and it was the only place that touched
Recent Messages.

## 2. Confirmed root causes

### 2.1 Recent Messages / Delete From resolved the chat only from a reply target

`_delfrom_recent_page()` and `_delfrom_panel_handler("exec:…")` both obtained the
chat with `get_target(owner_id)` and bailed out unless `kind == "reply"`.
Consequences:

* Opening the panel from the chat without replying to something produced
  `No chat context` even though the callback itself carries the chat.
* `TargetContext` expires after 300 seconds, so a panel left open stopped working.
* When a stale target existed for a *different* chat, execution could aim at that
  chat instead of the one the panel lives in.
* The `exec:` path had no way to distinguish "chat unknown" from "0".

The panel session (`inline_chat_id`, or the message-id session for the
personal-account panel) always knows the chat; it was simply never consulted.

### 2.2 Numeric input (`del:n`) reported nothing and left the command message behind

`_del_n_input_handler()`:

* ran `delete_service.do_del_n(...)` correctly, then painted the result **only**
  if a helper client existed **and** `inline_chat_id`/`inline_msg_id` were set.
  In the self-client Glass panel that condition is false, so the deletion happened
  and the user saw nothing — which is exactly the reported "unreliable execution".
* cleaned up the typed command message with a hard-coded helper assumption, so the
  command message survived and the visible chat looked unchanged.
* had no chat fallback at all: it used the `chat_id` captured when the input was
  registered, and `0` was accepted silently.

`_del_id_input_handler()` had the same three defects (plus `do_del_id`'s unbounded
"from this id forward" semantics).

### 2.3 Three independent ceilings

| Layer | Old value | Effect |
| --- | --- | --- |
| `backend/ai/actions.py::_MAX_DELETE_COUNT` | 500 | structured action rejected `count > 500` |
| `DeleteTool.parameters()["count"]["maximum"]` | 500 | tool schema told the model 500 |
| `delete_service.select_self_owned_message_ids()` | 500 | service raised for `count > 500` |
| `do_del_n_counts()` / `do_del_last_n_real()` | 500 | direct callers rejected too |
| `_MAX_DELETE_SCAN_MESSAGES` | 1000 | All mode and semantic selection stopped at 1000 |
| `semantic._MAX_CANDIDATES` / `_MAX_DELETE_IDS` | 100 / 100 | history window and re-verify window |

### 2.4 Partial operations were reported as total failure or total success

* `delete_verified_self_messages()` raised `RuntimeError` on a batch error without
  carrying what had already been deleted; callers reported `count: 0`.
* The tool's timeout branch always said `count: 0` even when batches had succeeded.
* `do_del_n()` / `do_del_id()` built their own strings, so the count shown to the
  user could disagree with what Telegram actually deleted.
* Nothing distinguished "scan stopped at the safety bound" from "history exhausted".

### 2.5 Ownership chokepoint is sound (unchanged)

`delete_verified_self_messages()` re-fetches every id, compares `out` against
`owner_id`, and deletes in `_DELETE_VERIFY_CHUNK = 100` batches. That is the only
place that issues `delete_messages`, and it stays that way — no path was allowed
to skip it for speed.

## 3. Constraints that shaped the fix

* The self client (`inline_engine._self_client`) is the only Telegram authority;
  the helper bot may paint panels but must never delete.
* Manual paths must not depend on `TargetContext`; the chat comes from the
  callback message, then the inline session, then the message-id session, never a
  guess.
* The AI never gets raw RPC: it still goes `ToolRegistry` → `ToolExecutor` →
  `delete_service`.
* Timezone: `TZ` (default `Asia/Tehran`), the same source `backend/config.py` uses;
  no second parser was added (`split_range()` splits, `parse_time_range` parses).
* No scheduler, worker, extra client, or second deletion mechanism.

## 4. Follow-up: why manual "All my messages" hung and deleted nothing

The owner's live test overrode the first report, so the All path was traced again
at `15bc401`.

1. **No total deadline.** `_del_view("allexec")` called
   `do_del_self_filtered()` without `deadline`, and the service only honoured a
   caller-supplied one. The scan bound (50,000) and the 5s per-RPC timeout bound
   individual calls, not the operation: 20,000 candidates mean ~200 ownership
   RPCs plus ~200 delete batches, which can run for a very long time while the
   panel handler — and therefore the visible panel — waits.
2. **Verified candidates were thrown away.** `do_del_self_filtered()` returned
   immediately when `selection.error` was set. `select_self_owned_message_ids()`
   returns the ids it already collected together with that error, so one history
   RPC failure produced zero deletions instead of a partial, honest result.
3. **The candidate bound was not enforced in All mode.** `MAX_DELETE_COUNT` was
   checked only in the `count is not None` branch, so `count=None` could select
   the full 50,000-scan-bound worth of ids.
4. **The preview could stall the confirmation panel.**
   `preview_self_owned_count()` was awaited inside the panel handler with only the
   per-message 5s RPC timeout, so 200 sampled messages could delay the panel by
   minutes.
5. **No sign of life.** The callback query was acknowledged, then nothing edited
   the panel until the whole operation returned. Even a bounded operation looked
   frozen.

The ownership chokepoint itself was NOT the defect: every path already went
through `delete_verified_self_messages()`, which re-fetches and checks `out` +
`sender_id` per id in 100-id batches. The fix adds a deadline and partial-result
reporting around it without bypassing or weakening it.
