# IMPLEMENTATION_REPORT.md — Save: Reply Mode edits its existing panel in place

> This file describes ONLY the current state of the repository for this task and
> **replaces every previous report in full**. Album-as-one-item, the save
> pipeline, retrieval/preview/deletion, and Save by Link are unchanged and still
> hold. Older reports remain readable in git history.

| Item | Value |
|---|---|
| Stage | UX-contract correction for Deep Save → Reply Mode (one defect only) |
| Date | 2026-10-22 |
| Project / remote | LifeOS Telegram self-bot, `Onlyicing1/Telegram-self-bot` (`origin`) |
| Base commit | `dc974939a312d7abd80d5f80cde83098c16a5ab3` (`fix(save): create the progress status message and resolve private tg:// links`) |
| Delivery | One commit, fast-forward to `origin/main` (SHA + remote equality reported in the delivery message) |
| Live Telegram / Supabase / Render | **NOT touched.** No session, no RPC, no SQL, no migration, no schema change, no `.env`, no credentials, no deployment, no Render shell |
| Document NOT modified | `DATABASE_ARCHITECTURE.md` (read only) |
| Interpreter used for verification | CPython 3.12.3 venv with `backend/requirements.txt` (**pinned Telethon 1.43.0**) + `pytest` + `pytest-asyncio` |

---

## 1. Root cause

The previous round deliberately replaced the panel-bound progress with a
**dedicated status message**. In `_save_reply_wait_handler` the flow was:

```python
reporter = await _start_status(client, chat_id, title="Deep Save", body="Resolving the target message…")
```

`_start_status` calls `self_client.send_message(chat_id, …)` and returns a
reporter bound to that **new** message. So on every Reply Mode save the owner
was left with:

* the panel showing `Deep Save — Reply Mode / Waiting for your reply...`
  forever, and
* a **second** message in the chat carrying the resolution line, the progress
  bar and the result.

That is exactly the opposite of the required UX: Reply Mode's panel *is* the
one object the owner is already looking at, so it must be the surface that
changes. The previous tests encoded the wrong contract (`"the panel that
launched the flow is never the progress surface"`, `send_message_calls == 1`),
which is why they were green while the live behaviour was wrong.

Nothing else was broken: target resolution, media handling, the album pipeline
and the transfer callbacks were already correct.

## 2. Behavior now

1. `Deep Save` → `Reply Mode` renders the existing panel:
   `Deep Save — Reply Mode` / `Waiting for your reply...` /
   `Reply to any message to save it (download → re-upload).`
2. When the owner replies to a target message, **that same message** is edited
   to `Resolving the target message…` *before* the reply/target lookup runs.
3. The existing `SaveProgressReporter` then edits the same message for every
   real download/upload milestone (shared five-segment bar, `50/100` at or below
   10 MiB, `20/40/60/80/100` above it, per phase and per album member with
   `File i/N`, no invented percentage for an unknown size).
4. The terminal success, refusal or failure **replaces the text in that same
   message**. `reporter.close()` marks the reporter terminal before rendering,
   so a late byte callback cannot overwrite the result.
5. Reply Mode **never sends a message**: `_start_status` / `send_message` are
   gone from this path, and there is no replacement-message fallback.
6. The trigger reply message is still deleted after processing, exactly as
   before.

Save by Link is untouched: it still creates its one dedicated status message
with the self client immediately after the link arrives (`_start_status`).

## 3. Implementation

### `backend/bot/handlers/save.py`

* **`_panel_coords(event, chat_id)`** — the exact `(chat_id, message_id)` of the
  panel. `resolve_callback_message` answers directly for a normal panel; an
  inline panel carries only `inline_message_id`, so the lifecycle session that
  sent it supplies the real pair the router itself edits. `(chat_id, 0)` when
  no message id can be established.
* **`_save_reply_action`** stores those coordinates in the pending entry
  (`inline_chat_id`, `inline_msg_id`) — the same pair the handler receives — so
  the arming step, the processing status, every milestone and the result all
  address one message.
* **`REPLY_PANEL_TITLE` + `_reply_wait_text(metadata=None)`** — one source for
  the panel text, shared by the plain action and the `Name & tags` step (the
  metadata echo no longer duplicates the string).
* **`_panel_editor(self_client, chat_id, msg_id)`** — returns `None` when there
  is no client or no panel coordinate, otherwise the existing
  `_status_editor` for that message (dedupe of identical renders, `not modified`
  treated as success, every other failure logged and swallowed, Telethon's
  documented `edit_message(entity, message_id, text)` shape). It never sends
  anything. The panel is the message the **owner's own account** sent — an
  inline panel result is sent by the requesting client, and the fallback panel
  the same way — so the self client is the only authority that can edit it.
* **`_save_reply_wait_handler`** builds that editor, renders
  `Resolving the target message…` immediately, binds
  `SaveProgressReporter(editor, title=REPLY_PANEL_TITLE)`, passes
  `reporter.handle` into `execute_save`, and finishes with
  `reporter.close(result)`. With no panel it logs `[SAVE_UI]` and runs the save
  without UI instead of inventing a message.
* Module docstring, `_start_status` and `_edit_status` docstrings now state the
  split: Reply Mode = existing panel, Save by Link = dedicated status message.

### Unchanged on purpose

`backend/services/save_service.py` (not touched at all), the Deep Save pipeline,
album-as-one-item (one grouped `send_file`, one save code, one `saved_items`
row, anchor + `grouped_id`), retrieval/preview/deletion of album codes,
view-once/expiry refusal, the progress renderer and cadence, the AI
`SaveTool`/`SaveByLinkTool` behavior, Save by Link's status-message contract,
`DATABASE_ARCHITECTURE.md`, and everything outside Save.

### `AGENTS.md`

§6 "Transfer progress" now documents the two surfaces precisely: Reply Mode
edits the panel it armed (coordinates captured at arming, one message for
resolution + milestones + result), Save by Link creates its one status message;
a missing panel coordinate runs the save without UI.

## 4. Changed files

| File | Change |
|---|---|
| `backend/bot/handlers/save.py` | `_panel_coords`, `_panel_editor`, `_reply_wait_text`, `REPLY_PANEL_TITLE`; Reply Mode edits the armed panel instead of creating a status message; docstrings |
| `tests/test_save_live_status_and_private_link.py` | Reply Mode expectations rewritten (no message, panel edited, coordinates carried end to end); 7 new/updated regressions; Save-by-Link authority tests retargeted |
| `tests/test_save_album_view_once_progress.py` | Reply Mode progress test rewritten to the panel contract (no `send_message`) |
| `AGENTS.md` | §6 save-progress contract |
| `IMPLEMENTATION_REPORT.md` | this report |

## 5. Tests

**Reply Mode lifecycle — `tests/test_save_live_status_and_private_link.py` (23 tests)**

| Test | Proves |
|---|---|
| `test_reply_mode_edits_the_existing_panel_before_target_lookup` | zero `send_message` calls; the panel is edited **before** the first `get_messages`; every applied edit is `(PANEL_CHAT, PANEL_MSG)`; first edit is the resolution line; terminal edit is the success result; one row |
| `test_panel_coordinates_travel_from_arming_to_the_terminal_result` | the whole contract from `_save_reply_action` → pending entry → the listener's pop-then-call → handler → panel; coordinates identical at every step |
| `test_an_inline_panel_resolves_its_message_through_the_session` | an `inline_message_id`-only callback yields the session's real `(chat_id, msg_id)` |
| `test_reply_mode_failure_result_lands_on_the_same_panel` | a gone target renders `no longer exists` on the panel, no new message, no row |
| `test_a_late_transfer_callback_cannot_overwrite_the_reply_panel_result` | after `close(result)`, a `finished` event produces no further edit and the terminal text is unchanged |
| `test_reply_mode_without_panel_coordinates_never_creates_a_message` | missing coordinates → save succeeds, zero messages, zero edits |
| `test_reply_mode_panel_is_edited_by_the_self_client_only` | a helper bot that raises on any send/edit is never reached |
| `test_link_mode_creates_the_status_message_before_parsing_the_link` | Save by Link contract unchanged (one message before the transfer) |
| `test_link_status_message_never_uses_the_helper_bot`, `test_link_status_creation_failure_still_saves_and_reports_on_the_panel` | Save by Link keeps its self-client status message, with the panel as the fallback surface |
| `test_production_edit_and_send_shapes_match_real_telethon` | `inspect.signature(TelegramClient.edit_message).bind(entity, msg_id, text)` is valid; the old `message=text` shape raises `TypeError` |
| the 12 private-`tg://` / album / signature tests | unchanged, still green (regression guard for the next task) |

**Reply Mode progress — `tests/test_save_album_view_once_progress.py` (55 tests)**

`test_reply_mode_edits_the_existing_panel_and_sends_no_message` replaces the old
"creates one status message" test: no send, every edit on
`(PANEL_CHAT, PANEL_MSG)`, resolution line first, `Downloading 50/100%` and
`Uploading 50/100%`, the shared `render_progress_bar(0.5)` glyphs, success
result last, real `download_media`/`upload_file`/`send_file` progress callbacks
attached. Album `File i/N` per-member progress, edit-failure containment,
unknown size and the request-level one-album test are unchanged.

**Measured results (this workspace, pinned Telethon 1.43.0)**

| Run | Result |
|---|---|
| `tests/test_save_live_status_and_private_link.py` | **23 passed** |
| `tests/test_save_album_view_once_progress.py` | **55 passed** |
| `tests/test_save_v2_wiring.py` + `tests/test_12_save_engine.py` | **44 + 25 passed** |
| Full suite `python -m pytest tests -q --deselect tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once` | **6630 passed, 27 skipped, 1 deselected** in 130.4 s |
| `python -m py_compile` on every changed Python file | exit 0 |
| `git diff --check` | clean |

The one deselected test
(`test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`)
still fails on unmodified code with its pre-existing date-bound reason —
`Task 1 was not scheduled: interval advancement exceeded bound` — and is
untouched by this change.

## 6. Not verified against a real Telegram account

Offline verification proves call shapes, ordering, byte-callback wiring, the
coordinate path and every failure branch. It cannot prove Telegram's
behaviour:

* the panel visibly changing in place on the owner's account (no session or
  credentials exist here), including that Telegram accepts repeated edits of a
  message that was a single inline instance;
* real download/upload progress callbacks (wired and asserted through the
  production `execute_save` path, fed by the fake client's byte counts);
* the album's grouped rendering and its retrieval/deletion on a live account;
* observable behaviour if the Glass panel auto-closes mid-transfer — the edit
  then fails, is logged, and the save still completes (by design), but this was
  not exercised against Telegram.

## 7. Intentionally deferred

* Private `tg://openmessage` link resolution: **not investigated, not changed,
  not re-verified** in this task. Its code, tests and behavior are exactly as
  committed in `dc97493`. A live private-link save remains the next separate
  task.
* Save by Link's status-message contract is preserved as-is.
