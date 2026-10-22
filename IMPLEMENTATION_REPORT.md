# IMPLEMENTATION_REPORT.md — Save: live progress lifecycle + private `tg://` links

> This file describes ONLY the current state of the repository for this task and
> **replaces every previous report in full**. The album-as-one-item contract from
> `c127d82` is untouched and still holds; this round fixes the two defects that
> still failed against the owner's real account. Older reports remain readable in
> git history and in `docs/investigations/`.

| Item | Value |
|---|---|
| Stage | Corrective implementation + regression tests (two defects only) |
| Date | 2026-10-22 |
| Project / remote | LifeOS Telegram self-bot, `Onlyicing1/Telegram-self-bot` (`origin`) |
| Base commit | `c127d82aeb115a303e8a07339ed89b69cf182bfe` (`fix(save): save an album as one item, support tg:// links, show real progress`) |
| Delivery | One commit, fast-forward to `origin/main` (SHA + remote equality reported in the delivery message) |
| Live Telegram / Supabase / Render shell | **NOT touched.** No session, no RPC, no SQL, no migration, no schema change, no `.env`, no credentials, no deployment, no Render shell |
| Document NOT modified | `DATABASE_ARCHITECTURE.md` (read only) |
| Interpreter used for verification | CPython 3.12.3 venv with `backend/requirements.txt` (**pinned Telethon 1.43.0**) + `pytest` + `pytest-asyncio` |

---

## 1. Root causes (source-verified against pinned Telethon 1.43.0)

### A. Progress could never be displayed

Two independent faults, both fatal on their own:

1. **The edit call itself was invalid.** `save.py` called

   ```python
   await self_client.edit_message(chat_id, msg_id, message=rendered, buttons=buttons)
   ```

   Pinned Telethon's signature is
   `edit_message(entity, message=None, text=None, *, …)` (`client/messages.py:1085`),
   where the **second positional parameter is the message id** and the new text is
   `text`. `msg_id` therefore bound to `message` positionally while
   `message=rendered` was supplied again as a keyword → every call raised
   `TypeError: got multiple values for argument 'message'`. The reporter caught
   and logged that exception (by design: "progress must never abort a save"), so
   the save succeeded and *nothing at all* appeared on Telegram. The previous
   round's tests passed only because the test fake accepted the text in the
   `message` slot, i.e. the fake encoded the bug.
2. **There was no status message, and the surface was wrong.** Progress was bound
   to the panel message that launched the input flow — an inline result sent by
   the owner's account — so it covered neither link resolution nor target lookup,
   and `_save_reply_action` stored `inline_msg_id=0` whenever the panel id could
   not be resolved, silently disabling progress entirely.

### B. Private `tg://openmessage` links parsed but could not be saved

`parse_link_target` was correct: the owner's exact URI
`tg://openmessage?user_id=5406707941&message_id=209974` parses to
`(username=None, chat_id=5406707941, message_id=209974)`. The failure was one
step later, in **peer resolution**:

* `get_messages(peer, ids=…)` resolves the peer first
  (`_IDsIter._init` → `client.get_input_entity(entity)`,
  `client/messages.py:290`).
* `get_input_entity(5406707941)` (`client/users.py:354`) can only answer from the
  message-box entity cache, the session's stored entities, or — for a `PeerUser`
  — `users.GetUsersRequest` with `access_hash=0`, which Telegram honours only for
  contacts. Otherwise it raises
  `ValueError: Could not find the input entity for PeerUser(5406707941) …`
  (`users.py:476`).
* The session here is a **`StringSession`, which persists no entities**, so after
  a restart an id is resolvable only if a live update or a dialog fetch happened
  to cache it. The old fallback (`get_entity(chat_id)`) took the *same* code path
  and raised the same `ValueError`, which the handler wrapped into
  `Could not resolve link: Could not find the input entity for PeerUser(…)`.

So the link was parsed, accepted, logged as supported — and then failed on the
account's own entity cache. Telegram does not expose an arbitrary user id, so no
`tg://`-specific workaround exists; the resolution path itself had to become
complete. Reproduced truthfully offline by the new `PeerResolutionClient`
(`get_input_entity`/`get_entity` raise `ValueError` for an unknown id, exactly as
pinned Telethon does).

## 2. Implementation

### `backend/bot/handlers/save.py` — the status-message lifecycle

* **`_start_status(self_client, chat_id, *, title, body)`** sends ONE dedicated
  Loading/status message with the **self client** into the chat the owner just
  typed in, immediately after the input arrives and **before** reply/target
  resolution (`"Resolving the target message…"`) or link parsing
  (`"Resolving the link…"`). It keeps the message's **own** `chat_id` + `id` from
  the send result and returns a `SaveProgressReporter` bound to them. No reply
  markup is attached: the message is sent by a user account, which cannot carry a
  bot keyboard, and a rejected send would hide progress again. `None` only when
  there is no client/chat or Telegram refused the send.
* **`_status_editor(client, chat_id, msg_id)`** is the one edit callable: it
  renders through the standard path (`render_edit`, shared five-segment bar) and
  calls `edit_message(chat_id, msg_id, text)` — the documented shape. It skips a
  render identical to the last applied one, treats Telegram's "not modified" as
  success, and logs-and-swallows every other edit failure so a UI problem can
  never abort a transfer.
* Both handlers (`_save_reply_wait_handler`, `_save_link_input_handler`) create
  the status first, pass `reporter.handle` into `execute_save` /
  `execute_link_save`, and finish with `reporter.close(result)`, so the terminal
  success/refusal/failure **replaces the status text in the same message**.
  `_edit_status` (panel edit via the self client) remains only as the fallback
  when no status message could be created.
* `_edit_status`, and the pre-existing caption handler, now also use the correct
  `edit_message` argument shape.

### `backend/services/save_service.py` — complete, validated link resolution

* **`LinkResolutionError`** with kinds `peer_unresolved`, `message_missing`,
  `lookup_failed` — the caller can no longer collapse four different failures
  into one message.
* **`_resolve_link_peer`** resolves the peer through ordinary authenticated APIs
  only, in order: (1) the username for `https://t.me/<username>/…`; (2) the
  session/entity cache; (3) explicit entity resolution (contact / public
  channel); (4) **the account's own dialog list** (`_dialog_entity`), bounded by
  `LINK_DIALOG_SCAN_LIMIT = 500` dialogs and stopping at the first match. The
  dialog list is the account's own surface — it neither widens access nor uses a
  second API — and it is what makes a chat resolvable again after a restart with
  a `StringSession`.
* **`_fetch_linked_message`** (1) reads the requested id inside the resolved peer;
  (2) when the peer stays unknown (or the read yields nothing), reads the same id
  through Telegram's **peer-less lookup** (`get_messages(None, ids=…)`,
  `InputMessageID`), which needs no entity; that result is returned to the caller
  for validation, so a message from another chat/slot is reported as a mismatch
  and never saved; (3) otherwise raises the distinct honest error.
* **`execute_link_save`** validates `id` AND `chat_id` of whatever came back
  (unchanged, now also the safety net for the peer-less path) and maps each
  `LinkResolutionError` kind to its own actionable owner-facing text:
  unknown chat → "open that chat once in Telegram so it appears in the chat list,
  then send the link again"; missing message → "may have been deleted, or this
  account may not have access"; lookup failure → the Telegram reason verbatim.
  Malformed URIs still fail in the parser and touch no Telegram API. Nothing is
  refused merely for using `tg://`, and view-once/expiring media is still refused
  by the MEDIA policy (tested through a `tg://` link).
* **`resolve_album_messages`** reads the album window through the message's own
  `input_chat` when Telegram supplied one (falling back to the numeric chat id),
  so a link-resolved album — whose bare peer id may be unresolvable — resolves
  its members and is still saved as ONE album (one code, one row).

### Unchanged on purpose

Album-as-one-item (one grouped `send_file`, one save code, one `saved_items` row,
anchor = first destination message, `grouped_id` resolution), retrieval /
preview / deletion of album codes, the Deep Save pipeline itself, the progress
cadence & renderer, the AI `SaveTool`/`SaveByLinkTool` behavior, `DATABASE_ARCHITECTURE.md`,
and everything outside Save.

### `AGENTS.md`

§6 now documents the status-message lifecycle (created before resolution, edited
in place by the self client, terminal result replaces it) and the complete
`tg://` resolution chain with its distinct failure kinds.

## 3. Changed files

| File | Change |
|---|---|
| `backend/bot/handlers/save.py` | `_start_status` + `_status_editor` replace the panel-bound `_progress_reporter`; both input handlers create the status first and close with the terminal result; correct `edit_message` shape; module docstring |
| `backend/services/save_service.py` | `LinkResolutionError`, `_dialog_entity`, `_resolve_link_peer`, rewritten `_fetch_linked_message`, `_link_failure_text`, distinct failure mapping in `execute_link_save`, album window read through `input_chat` |
| `tests/test_save_live_status_and_private_link.py` | **NEW** — 17 tests for both defects |
| `tests/test_save_album_view_once_progress.py` | fake client now mirrors the real `send_message`/`edit_message` signatures; progress assertions moved to the status message; docstring updated |
| `tests/test_save_v2_wiring.py` | fake client gained the real `get_input_entity`/`get_entity` surface link resolution uses |
| `AGENTS.md` | §6 save-progress + link-resolution contract |
| `IMPLEMENTATION_REPORT.md` | this report |

## 4. Tests

**New file — `tests/test_save_live_status_and_private_link.py` (17 tests)**

| Area | Assertions |
|---|---|
| Status creation | Reply Mode creates exactly ONE status message in the submitted chat, with "Deep Save"/"Resolving…" text, **before** the reply lookup (recorded call order); Save by Link creates it before parsing/resolving the link |
| One message, one target | every applied edit addresses the created message's own `(chat_id, msg_id)`; the launching panel `(PANEL_CHAT, PANEL_MSG)` is never edited; `send_message` count stays 1 for the whole save |
| Client authority | a monkeypatched helper bot that raises on any send/edit is never reached — the self client does all of it |
| Terminal state | `close("**DONE — saved**")` is the last applied edit and a later progress event produces no further edit |
| Failure containment | a refusing `send_message` still saves the item and renders the result on the panel |
| Signature truth | `inspect.signature(TelegramClient.edit_message).bind(...)` proves the production shape is valid and the old `message=text` shape raises `TypeError`; `send_message` shape likewise |
| Private link | the owner's exact URI saves when the peer is unknown to the session (peer-less lookup) and its `origin_chat_id` is the requested user; the peer is resolved from a REAL Telethon `User` entity supplied by the dialog list and the read goes through that entity; the dialog scan is bounded (10) and stops; unresolvable peer → "Could not reach the chat … open that chat once in Telegram"; missing message → "Message not found"; read exception → "Could not read the linked message: FLOOD_WAIT_30"; peer-less message from another chat → "did not resolve to the requested chat"; wrong id → "did not resolve to the requested message"; all of them write no row and download nothing |
| Album + regressions | a linked album through an unresolvable peer resolves through the message's own `input_chat` and is ONE album send / ONE row; public username and `/c/` links keep their ordinary path (`/c/` needs no dialog scan); a `tg://` view-once message is refused by the media policy, not by the scheme |

**Measured results (this workspace, pinned Telethon 1.43.0)**

| Run | Result |
|---|---|
| `tests/test_save_live_status_and_private_link.py` | **17 passed** |
| `tests/test_save_album_view_once_progress.py` | **55 passed** |
| Save/delete/retrieve/preview/symbol/AI-action subset (23 files) | **639 passed** |
| Full suite `python -m pytest tests -q --deselect tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once` | **6624 passed, 27 skipped, 1 deselected** in 130 s |
| `python -m py_compile` on every changed module + test file | exit 0 |
| `git diff --check` | clean |

The single deselected test
(`test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`)
fails **identically on unmodified `c127d82`** (verified by `git stash -u`, re-run
on the clean tree, restore): it is a pre-existing, date-dependent task-scheduler
bound failure ("interval advancement exceeded bound") unrelated to Save.

## 5. Not verified against a real Telegram account

Offline verification proves the call shapes, the ordering, the byte-callback
wiring and every failure branch; it cannot prove Telegram's responses:

* an actual status message appearing in the owner's chat and being edited in
  place by the owner's own account (no session/credentials exist here);
* a real `tg://openmessage?user_id=5406707941&message_id=209974` read, including
  how much of it is served by the dialog-list warm-up versus the peer-less
  `InputMessageID` lookup (Telegram's server-side coverage of the latter for
  private chats is documented as a global lookup by Telethon but was not
  exercised live);
* real download/upload byte callbacks (the real `progress_callback` wiring is
  asserted through the production `execute_save`/`execute_link_save` paths with
  the fake client's byte-count callbacks, not with Telegram);
* the album's grouped rendering, retrieval and deletion on a live account.

## 6. Remaining limitations

* A private user id that is **neither** cached, **nor** a contact, **nor** within
  the first `LINK_DIALOG_SCAN_LIMIT` (500) dialogs, **nor** reachable through the
  peer-less lookup cannot be read by this account — Telegram does not expose it.
  That case returns the actionable "open that chat once in Telegram" failure
  rather than a false claim of support. No access rule is bypassed.
* The dialog scan costs up to five `messages.GetDialogs` pages and only runs when
  the cache and explicit entity resolution both miss.
* A status message is one extra message per save, in the chat the owner typed in;
  it is intentionally not deleted, because it carries the result.
* The status message carries no inline keyboard (user accounts cannot attach bot
  keyboards), so the Home shortcut from the Glass panels is absent there.
* `StringSession` still persists no entities; every fresh process pays the
  resolution cost of the first link to a given chat.
