# IMPLEMENTATION_REPORT.md — Save Reply Mode drives the existing Helper inline panel

> This file describes ONLY the current state of the repository for this task and
> **replaces every previous report in full**. Album-as-one-item, the save
> pipeline, retrieval/preview/deletion, Save by Link and the AI Save tools are
> unchanged and still hold. Older reports remain readable in Git history.

| Item | Value |
|---|---|
| Stage | Reply Mode presentation: the existing panel is the loading/progress/result surface, edited by the Helper Bot when Telegram gives it the handle |
| Date | 2026-10-22 |
| Project / remote | LifeOS Telegram self-bot, `Onlyicing1/Telegram-self-bot` (`origin`) |
| Base commit | `f4708f7118f9d965b166d5a56f85cfa9adb7053e` (`docs: record helper inline panel edit investigation`) |
| Investigation | `INVESTIGATION.md` (verdict **UNRESOLVED**: the library path is confirmed, the Telegram-side identifier delivery is not) |
| Delivery | One commit, fast-forward to `origin/main` (SHA + remote equality reported in the delivery message) |
| Live Telegram / Supabase / Render | **NOT touched.** No session, no RPC, no SQL, no migration, no schema change, no `.env`, no credentials, no deployment, no Render shell |
| Documents not modified | `DATABASE_ARCHITECTURE.md` (read only), `README.md` |
| Interpreter used for verification | CPython 3.12 venv with `backend/requirements.txt` (**pinned Telethon 1.43.0**) + `pytest` + `pytest-asyncio` |

---

## 1. What was missing

`INVESTIGATION.md` established that Reply Mode already edited its panel in
place, but only ever through the **self client**, and that the repository threw
away the one handle a Helper Bot needs for that same message:

* `trigger()` returned `""` for the inline identifier because it read
  `Message.inline_message_id`, an attribute pinned Telethon 1.43.0 does not have
  (`backend/helper/inline_engine.py:265`);
* the pending Reply Mode entry carried only `inline_chat_id` / `inline_msg_id`
  integers (`backend/helper/input_state.py`), and
* `Session.inline_message_id` was permanently `""`, so the session's inline
  index was dead too.

The Helper Bot is the account Telegram lets edit a message that was sent after
a user picked its inline result (`messages.editInlineBotMessage`), and it is the
presentation owner of these panels. Nothing captured that identifier, so it
could never present the save.

## 2. Behavior now

1. `Menu` → `Save` → `Deep Save` → `Reply Mode` renders the existing panel:
   `Deep Save — Reply Mode` / `Waiting for your reply...`.
2. Arming Reply Mode captures Telegram's inline-message identifier (when the
   click carried one), claims the panel for this operation, and stops the
   panel's auto-close countdown so the panel can serve as the workflow surface.
3. The owner replies to the target. The self-bot resolves the target and runs
   `execute_save` exactly as before — it remains the only executor.
4. **The same panel message** is edited to `Resolving the target message…`
   before the reply/target lookup, then for every real download/upload
   milestone, then to the terminal success/refusal/failure text.
5. The owner's reply message is deleted after the terminal result, exactly as
   before; the target is never deleted.
6. **No progress or result message is ever sent** on this path. If the panel
   cannot be addressed at all, the save runs with no UI rather than producing a
   substitute message.

Save by Link is untouched: it still creates its one dedicated status message
with the self client (`_start_status`).

## 3. Implementation

### New: `backend/helper/panel_edit.py`

One narrow module for "edit the existing panel from a background flow":

* `inline_message_id_from_event(event)` — returns `event.query.msg_id` only when
  it is an `InputBotInlineMessageID` / `InputBotInlineMessageID64`, i.e. only
  for an inline-flavoured callback. It never derives an identifier from
  chat/message ids, because Telegram offers no lookup that could produce one.
* `ExistingPanelEditor` — the edit callable the reporter binds to:
  * **Helper path** (`inline_id` + helper client):
    `helper.edit_message(inline_id, text=rendered)` → Telethon's
    `messages.editInlineBotMessage`. No CallbackQuery is involved, so it works
    after the original callback has returned.
  * **Self path** (`chat_id` + `msg_id` + self client):
    `self_client.edit_message(chat_id, msg_id, rendered)` → `messages.editMessage`
    on the same message, by the account that sent it.
  * The inline path is tried first; one refusal (invalid/deleted identifier,
    permission error) marks it unusable and the remaining updates go through the
    panel's own coordinates — still the same message, still no send.
  * **The keyboard is never touched.** `buttons` is never passed, so MTProto
    leaves the existing `reply_markup` in place. Passing `buttons=[]` would send
    a `ReplyKeyboardMarkup(rows=[])` (verified against the pinned client) — the
    wrong markup type — so the editor deliberately does not.
  * Identical renders are skipped, `not modified` counts as success, every other
    failure is logged and swallowed (progress never aborts a save).
* Per-panel **generation tokens**: `begin_panel_operation`,
  `panel_operation_is_current`, `finish_panel_operation`,
  `supersede_panel_operation`. The newest Save on a panel is its only writer.

### `backend/bot/handlers/save.py`

* `_save_reply_action` captures `inline_message_id_from_event(event)`, claims
  the panel, stops its countdown (`_keep_panel_as_progress_surface`) and stores
  `inline_id` alongside `inline_chat_id` / `inline_msg_id` in the pending entry.
* `_save_metadata_input_handler` (the optional `Name & tags` step) forwards the
  identifier it received and stops the timer before arming Reply Mode.
* `_reply_handler_with_metadata` forwards `inline_id` to the wait handler.
* `_save_reply_wait_handler` starts a new operation generation, builds
  `ExistingPanelEditor` (helper = `get_client()`), writes the loading line,
  binds `SaveProgressReporter(editor, …)`, renders the terminal result, releases
  the generation, then deletes the reply.
* `_panel_editor` is gone (superseded by the editor); `_status_editor` remains
  for Save by Link only, with its docstring corrected.

### `backend/helper/input_state.py`

`set_pending(..., inline_id=None)` and a matching entry field — the identifier
travels with the pending input exactly like `extra`, so it survives the 120 s
window in the same structure that already carries the panel coordinates.

### `backend/helper/inline_sender.py`

The input listener passes `inline_id=` only to handlers that declare it (the
same `inspect.signature` mechanism already used for `extra`), so every other
input handler is called exactly as before.

### `backend/helper/panels.py`

* `_handle_input` stores the identifier carried by the click that opened the
  input, so a Reply Mode armed through the `Name & tags` step keeps it.
* `_callback_router` calls `supersede_panel_operation(chat_id, msg_id)` before
  dispatch: a click on a panel a background flow is still editing becomes the
  panel's new owner, and the older flow stops writing instead of overwriting it.
  It is a no-op for every panel without a background operation.

### Ownership boundary (unchanged)

| Responsibility | Owner |
|---|---|
| Panel presentation (loading / progress / terminal text) | Helper Bot, via its inline identifier |
| Panel edit fallback when no identifier reached the handler | self client, same message, its own `(chat_id, msg_id)` |
| Target resolution, `execute_save`, persistence, media, reply deletion | self-bot only (`save_service`, unchanged) |

No second executor, scheduler, update loop or Save implementation was added.
`ToolExecutor` and the service layer are untouched.

## 4. Changed files

| File | Change |
|---|---|
| `backend/helper/panel_edit.py` | **new** — identifier extraction, `ExistingPanelEditor`, per-panel generations |
| `backend/bot/handlers/save.py` | capture + carry the identifier, use the editor, stop the countdown at arming, drop `_panel_editor`, docstrings |
| `backend/helper/input_state.py` | `inline_id` field on the pending entry |
| `backend/helper/inline_sender.py` | pass `inline_id` to handlers that declare it |
| `backend/helper/panels.py` | carry the identifier from an input click; supersede a background writer on any click |
| `tests/test_save_reply_panel_edit.py` | **new** — 24 focused tests |
| `AGENTS.md` | §6 transfer-progress contract (no longer "self client is the only editor") |
| `IMPLEMENTATION_REPORT.md` | this report |

## 5. Tests

New file `tests/test_save_reply_panel_edit.py`, **24 tests**:

| Area | Tests |
|---|---|
| Identifier capture | read from a REAL `UpdateInlineBotCallbackQuery` event; absent for a `UpdateBotCallbackQuery`; carried by arming; absent when there is none; listener passes it only to handlers that declare it (5) |
| Panel is the surface | helper performs every edit on that exact identifier; loading → progress → terminal on one message; failure result on the same panel; not-a-reply on the same panel; no `send_message` at all; reply deleted once, target not deleted (6) |
| Fallbacks | refused inline edit falls back to the panel's coordinates after ONE attempt; no identifier → the sending account edits it; no addressable panel → save runs without UI; `not modified` is success; an editor with no target is unavailable (5) |
| Staleness / concurrency | stale generation cannot write; a newer arming supersedes an in-flight save; a click supersedes the background writer; the router invokes that supersede; a click mid-transfer stops stale writes; terminal generation is released (6) |
| Lifecycle | arming stops the panel's auto-close timer; expired pending input never starts a save (2) |

The fakes implement pinned Telethon 1.43.0's argument shapes
(`edit_message(entity, message=None, text=None, *, buttons=None)`), and the
callback events are **real Telethon event objects** built from real update
TLObjects — but none of this proves Telegram accepts the requests (§6).

**Measured results (this workspace, pinned Telethon 1.43.0, CPython 3.12)**

| Command | Result |
|---|---|
| `python -m pytest tests/test_save_reply_panel_edit.py -q` | **24 passed** in 0.92 s |
| `python -m pytest tests/test_save_live_status_and_private_link.py tests/test_save_album_view_once_progress.py tests/test_save_v2_wiring.py tests/test_save_v2_metadata.py tests/test_save_v2_resolution.py tests/test_helper_callback_receipt.py tests/test_12_save_engine.py tests/test_save_reply_panel_edit.py -q` | **260 passed** in 1.84 s |
| `python -m pytest tests -q` (full suite) | **6654 passed, 1 failed, 27 skipped** in 131 s |
| `python -m py_compile` on every changed Python file | exit 0 |
| `git diff --check` | clean |

The single failure is
`tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`
(`Task 1 was not scheduled: interval advancement exceeded bound`). It is
**pre-existing**: the same test fails on a pristine export of the base commit
(`git archive HEAD` into a clean directory, without these changes), and it is
unrelated to Save or panels.

## 6. Not verified against Telegram

Automated tests prove application behaviour only. **No live Telegram request was
made and no session or credential was used**, so the following are explicitly
**not verified**:

* that Telegram delivers the panel click to the Helper Bot as
  `UpdateInlineBotCallbackQuery` carrying an `InputBotInlineMessageID` — this is
  `UNKNOWN` (U1) in `INVESTIGATION.md` and decides whether the Helper Bot is the
  account that performs the edits;
* that Telegram accepts `messages.editInlineBotMessage` from the Helper Bot
  minutes later, on a panel in a chat the bot may not be a member of;
* that a real account shows the panel changing in place with its keyboard
  intact and still clickable;
* real download/upload byte callbacks through the live MTProto stack (the
  production `execute_save` path is exercised, fed by a fake client's counts);
* the actual error Telegram returns when the panel was deleted or the
  identifier is stale (the code handles any exception, but the code/class is
  unobserved).

What the implementation guarantees regardless of that unknown: the panel is the
only surface, it is edited in place, no extra message is created, and the save
runs correctly with or without a usable identifier.

## 7. Required manual verification

With the real bot (not from this clone):

1. `Menu` → `Save` → `Deep Save` → `Reply Mode`; confirm the panel asks for a
   reply and that log line `[SAVE_UI] reply mode panel edit path=…` reports
   `helper-inline` (identifier captured) or `self-chat` (not captured).
2. Reply to a media message; confirm **the panel itself** first shows
   `Resolving the target message…`, then the progress bar, then the terminal
   result — and that the chat contains **no second status message**.
3. Confirm the panel's buttons are still present and still work after the
   terminal edit, and that clicking one mid-save stops further progress edits
   instead of flickering.
4. Confirm the replied message is gone afterwards and the target still exists
   (and is in Saved Messages).
5. Repeat with a save that runs longer than the old 120 s auto-close delay, to
   confirm the panel is no longer closed or deleted mid-transfer.
6. If step 1 reported `self-chat`, the Helper-Bot path is unavailable for these
   panels: record it and close U1 as unsupported; the shipped UX is unaffected.

## 8. Limits left in place deliberately

* `Pending input` still expires after 120 s, so the owner must reply within that
  window; this task did not change pending-input lifetime.
* Navigating away from a panel mid-save supersedes the save's writes (by
  design); the terminal result is then not rendered, while the save still
  completes and is recorded.
* Private `tg://openmessage` link resolution, provider selection, unrelated
  panels, the database schema and Save by Link are untouched.
