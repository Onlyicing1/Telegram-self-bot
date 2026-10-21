# IMPLEMENTATION_REPORT.md — Anti-Edit / Anti-Delete: reaction-only updates are never edits

> This file describes ONLY the current state of the repository for this task and
> **replaces every previous report in full** — it is not an appended report.
> Earlier reports (the Supabase transport-evidence round at `6f93228`, the
> lifecycle-task stall round at `17942f1`, the AED security/archive rounds)
> remain readable in git history and in `docs/investigations/`.

| Item | Value |
|---|---|
| Stage | Defect fix + regression tests |
| Date | 2026-10-21 |
| Project / remote | LifeOS Telegram self-bot, `Onlyicing1/Telegram-self-bot` (`origin`) |
| Branch / base | `feat/private-message-phase2` @ `6f93228b604737b3dda985627f9ef33418b7c573` (= `origin/main` at task start) |
| Code commit | `0e56e8771f4a589b5185a079bd710e86987ce57c` |
| Live Telegram / Render / Supabase | **NOT touched.** No session, no RPC, no deployment, no `.env`, no credentials, no SQL, no migration, no schema change |
| Workspace interpreter | CPython 3.12 libraries present; workspace **Telethon 1.34.0**, pinned **Telethon 1.43.0** (`backend/requirements.txt`) — the pinned wheel was downloaded from PyPI and inspected directly |

---

## 1. Objective

A reaction added to a message (add / change / remove) must never produce an AED
edit entry, must never increase the visible edit/version count, and must never
make an unchanged message appear in the archive — while genuine content edits,
genuine deletions and their recovery semantics keep working.

---

## 2. Source audit (read before any edit)

**Repository** — branch, HEAD, working tree and `origin/main` were inspected
first: the tree was clean apart from the three files this task changes; no
unrelated pre-existing work existed in the workspace at task start.

**Application code** — `backend/bot/handlers/anti_edit_delete.py` (the three
capture handlers), `backend/services/anti_edit_delete_service.py` (scope gate,
`record_message`, `record_deletion`, version grouping, persistence),
`tests/test_anti_edit_delete.py`, `IMPLEMENTATION_REPORT.md`,
`docs/investigations/anti-edit-delete-status.md`, `backend/bot/router.py`
(registration order), `backend/runtime/memory_cleanup.py` (the only cleanup
hook).

**Pinned Telethon 1.43.0** (wheel downloaded and unpacked, not assumed):
`telethon/events/messageedited.py`, `telethon/events/newmessage.py`,
`telethon/events/common.py`, `telethon/client/updates.py::_dispatch_update`,
`telethon/_updates/messagebox.py`, `telethon/tl/custom/chatgetter.py`, and the
TL constructors `UpdateEditMessage`, `UpdateEditChannelMessage`,
`UpdateMessageReactions`, `MessageReactions`, `ReactionCount`, `Message`.

---

## 3. Verified root cause

**The AED edit path treated "an edit-shaped update arrived" as "the content
changed".**

1. **Only one code path can write `kind="edit"`.** A full-tree search shows
   `record_message(..., kind=KIND_EDIT)` is called from exactly one place: the
   `events.MessageEdited` handler. Nothing else records an edit version.

2. **Telethon maps only two update types onto that event.** Pinned 1.43.0
   `MessageEdited.build` returns an event only for
   `types.UpdateEditMessage` / `types.UpdateEditChannelMessage`; it does not
   inspect `UpdateMessageReactions`, and no code in `_updates/messagebox.py`
   synthesises an edit event from a reaction change. Executed directly against
   the real types:

   ```
   events.MessageEdited.build(UpdateMessageReactions(...))  -> None
   events.MessageEdited.build(UpdateEditMessage(...))       -> Event
   ```

   A plain reaction notification therefore never reached the AED handler at all.
   Any AED edit row must have come from an **edit-shaped** update that carried
   an **unchanged message**.

3. **The handler never checked whether anything changed.** `record_message`
   computed the edit version as `edit_date or date` and stored the row
   unconditionally. Consequently, for a message Telegram has never edited,
   `edit_date` is `None`, the version fell back to the message `date`, and
   `make_id(kind="edit", version=date)` differs from the snapshot's
   `make_id(kind="snapshot", version=date)` — so the deterministic idempotency
   key could not suppress it. Result: a second row with byte-identical content,
   `version_count` 2, the item rendered as edited, and (when no snapshot existed)
   an unchanged message appearing in the archive for the first time.

4. **`edit_date` is the correct discriminator, and reactions are not.** Telegram
   stamps `edit_date` on every content edit (it is the "edited" label) and never
   on a reaction change: reaction state travels in `Message.reactions` /
   `updateMessageReactions` (`core.telegram.org/api/reactions`), not in the
   message content. A message merely *having* reactions is therefore never a
   signal — a genuinely edited message may carry them.

**Reproduced in-process (not mocked):** the same real
`UpdateEditMessage(Message(...))` for an unchanged, reaction-only message,
delivered through the real `events.MessageEdited` builder and the real handler,
produced `['delete', 'edit', 'snapshot']` on the pre-fix service and produces no
edit row after the fix (see §5).

---

## 4. Implementation changes (exact)

`backend/services/anti_edit_delete_service.py`

* **`is_content_edit(msg)`** — True only when the message carries Telegram's own
  `edit_date`. No reaction inspection, no content guesswork.
* **`content_signature(value)`** — a bounded fingerprint of the *content*
  (category, body, caption flag, media presence/type/name/size). It accepts a
  live message or a stored archive row, so an incoming edit is compared with the
  newest observed version using only fields the archive already stores.
  `Message.reactions` is deliberately absent.
* **`_edit_advanced(previous, edited_at)`** — True when the update's stamp is
  strictly newer than the stored version's (`_iso` output is UTC ISO-8601, so it
  orders lexicographically; an unedited snapshot has no stamp, so any stamp is
  newer).
* **`record_message`** — for `kind=KIND_EDIT`:
  1. the version is the `edit_date` **only** (the `or date` fallback is gone); a
     message without a stamp is a non-content state update and is dropped before
     any row is built (bounded debug log: chat id + message id only, never
     content);
  2. inside the existing serialization lock, a version is written only when the
     content signature differs from the newest stored version **or** the stamp
     advanced — an unchanged message therefore stays invisible and its version
     count stays put.
* Module docstring updated to state the contract.

`backend/bot/handlers/anti_edit_delete.py` — the module docstring now documents
that only **content** edit versions are recorded and that the service classifies
the update; no handler logic changed (the handler stays thin, the service remains
the single owner of archive state).

**Unchanged on purpose:** the confirmed-human-private-peer gate, inline-bot and
bot exclusions, group/Saved-Messages exclusion, idempotency keys, snapshot
capture, deletion recording and recoverability, browse grouping/filters,
retention/cleanup, persistence (dedicated tables and the legacy shard fallback),
and the update/handler registration. No second client, loop, scheduler or
executor; no schema, SQL, Supabase or `DATABASE_ARCHITECTURE.md` change;
`INVESTIGATION.md` untouched.

---

## 5. Tests (real Telethon update/event types, real handler, real service)

Added to `tests/test_anti_edit_delete.py` (section 16, 15 tests). They build
**real** `telethon.tl.types.Message`, `UpdateEditMessage`,
`UpdateMessageReactions`, `MessageReactions`, `ReactionEmoji`,
`MessageMediaPhoto` objects and pass them through the **real**
`events.MessageEdited.build(...)` before invoking the registered handler, so the
classification under test is the production one.

| Test | Asserts |
|---|---|
| `test_the_reaction_update_never_builds_a_message_edited_event` | `UpdateMessageReactions` → `MessageEdited.build` is `None` (no dispatch, nothing archived); a real edit update still builds an event |
| `test_a_reaction_on_an_archived_message_creates_no_edit_version` | snapshot only; `version_count` 1; list renders no version count |
| `test_an_edit_shaped_reaction_update_cannot_create_a_version` | edit-shaped update, real `Message`, reaction state, `edit_date=None` → event **is** produced, **no** edit row (**the defect reproduction**) |
| `test_an_unchanged_message_stays_invisible_after_a_reaction` | no snapshot + reaction-only edit-shaped update → archive empty, zero items |
| `test_adding_and_changing_reactions_never_increments_the_version_count` | add/change/remove across both update shapes → `version_count` 1, body unchanged |
| `test_reaction_state_never_participates_in_the_content_signature` | identical content + different reactions → identical signature; different text → different signature |
| `test_a_genuine_text_edit_is_still_captured_with_its_original_snapshot` | real text edit **with reactions attached** → edit row, `version_count` 2, original + edited bodies, list shows "2 versions" |
| `test_a_genuine_caption_edit_on_media_is_still_captured` | real `MessageMediaPhoto` caption edit → edit row, category photos, `has_caption` |
| `test_an_edit_that_advances_the_stamp_is_captured_even_with_identical_text` | genuine re-edit with identical text → still captured (stamp advanced) |
| `test_a_stale_unchanged_edit_update_cannot_re_add_a_version` | unchanged content with an older stamp → no new version |
| `test_a_redelivered_edit_is_recorded_once` | idempotency preserved |
| `test_reactions_do_not_disturb_deletion_recovery` | snapshot → reaction updates → deletion: no edit row, deletion `recoverable=True`, body intact, `recovered_from` = the snapshot |
| `test_the_reaction_only_classification_is_boundary_safe` | no stamp → not an edit; missing/`None` message never raises; `edit_date` present → edit |

**Measured results**

| Run | Result |
|---|---|
| Focused AED suite (`tests/test_anti_edit_delete.py`) | **108 passed** |
| Same 15 new tests against the **pre-fix** service (`git stash` of the two changed source files) | **7 failed, 3 passed, 98 deselected** — the defect tests fail exactly as reported (`['delete','edit','snapshot']`, version count 2, an unarchived message becoming visible) |
| Adjacent regression suites (`test_anti_edit_delete`, `test_private_message_management`, `test_private_message_scope`, `test_database_setup_order`) | **296 passed** |
| Full suite (`python3 -m pytest -q`) | **6407 passed, 27 skipped, 40 failed** |
| `python3 -m py_compile` on the three changed files | exit 0 |
| `git diff --check` | clean |

**The 40 full-suite failures are pre-existing and unrelated:** every one is in
`tests/test_colored_buttons.py`, which requires `Button.inline(style=...)` — the
workspace has **Telethon 1.34.0** installed while the project pins **1.43.0**.
No AED test fails, and none of the changed files is touched by that suite.

Existing bot / group / Saved Messages / inline-bot-origin / unresolved-peer
exclusion tests, snapshot-only visibility tests, persistence tests and the
dedicated/legacy store tests all still pass inside the 108.

---

## 6. Not verified / limitations

* **Live Telegram behaviour was NOT observed.** No session, credentials,
  deployment or account was available; nothing here was confirmed against a real
  account. The fix is deliberately independent of *which* update Telegram emits
  for a reaction: a documented `updateMessageReactions` never reaches the handler
  (verified: it builds no event), and any edit-shaped update that carries no
  content change is now rejected by the service. The owner should confirm on a
  live session that reacting leaves the archive list and version count
  unchanged, and that a genuine edit still records.
* The exact server-side reason Telegram sent an edit-shaped update for the
  observed reaction was not captured (no update log was available from the
  account). It is not required by the fix, whose classification is content- and
  stamp-based, but it remains unobserved.
* Rows already archived as spurious edits before this fix are **not** rewritten
  or deleted — no data migration, SQL or schema change was permitted. They age
  out through the existing retention cleanup or the owner's "Clear archive".
* Everything above is in-process verification with real Telethon types; no
  production, Render, Supabase or network call was made.
