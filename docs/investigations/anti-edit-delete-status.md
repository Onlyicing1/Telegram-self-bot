# Anti-Edit / Anti-Delete — Investigation Status

> Dedicated investigation record for the Anti-Edit / Anti-Delete feature.
> It was created because the repository's canonical `INVESTIGATION.md` documents
> a **different, completed** task (Inline Button Color Semantics, commit
> `2805a88`) and was deliberately **not** overwritten, so its unrelated evidence
> is preserved. This file describes **only** the current investigation — the
> Telegram-bot exclusion — and **replaces every previous status in full**; the
> earlier statuses (the missing-module startup failure at `330ef39`, later
> resolved by `d862520`, and the grouping/identity UX round `33e1fd3`) remain
> readable in git history. See `IMPLEMENTATION_REPORT.md` for the task report.

| Item | Value |
|---|---|
| Date | 2026-10-17 |
| Project / remote | LifeOS Telegram self-bot, `Onlyicing1/Telegram-self-bot` (`origin` = `https://github.com/Onlyicing1/Telegram-self-bot.git`) |
| Branch / HEAD | `main` @ `33e1fd3e30937d67b5413624661764d91c21ac5f` (verified `= origin/main` at task start) |
| Reported symptom | A Telegram **bot** account (`@sexyboyvpn_bot`) appeared in the private-chat archive as a normal archived item, rendered as `Kind: message` with the bot's username and display name shown as the user identity |
| Runtime / Telethon | Python 3.13-layered repo; installed **Telethon 1.43.0** (.venv) — checked directly |
| Investigation method | Full source trace of the event path (`events.NewMessage` → `MessageEdited` → `MessageDeleted` → gate → record → persist → browse → grouping → detail), plus direct inspection of the installed Telethon API. No live Telegram, no deployment, no SQL, no Supabase. |
| Live verification | **None.** No credentials; no Telegram request of any kind was made. |

---

## 1. Verified root cause

`backend/services/anti_edit_delete_service.py::eligible` — the ONE scope gate
shared by capture, browse and cleanup — decided eligibility from the **chat id
shape only**: `chat_id > 0` (private), not the owner (Saved Messages), not
`via_bot_id`. A private chat with a **bot** passes all of those checks because a
bot's private chat id is the bot's own positive user id. Nothing in the gate,
in `record_message` / `record_deletion`, or in the handlers ever read the
resolved peer entity's `User.bot` flag:

* `backend/bot/handlers/anti_edit_delete.py::_resolve_peer_identity` resolved
  the peer via `event.get_chat()` but used the entity ONLY to copy the
  `username` / display name into the row — the same entity that proves
  `bot=True` was displayed, not classified. That is exactly the observed
  rendering (`@sexyboyvpn_bot`, bot display name, `Kind: message` grouping).
* The `MessageEdited` handler reused the same gate → a bot edit could create an
  archive record; the `MessageDeleted` handler used the same gate on the event's
  rarely-present chat id.
* Persisted rows carry no bot metadata, so nothing could exclude them at browse
  time either.

## 2. Telethon facts verified against the installed API

* `telethon.tl.types.User.__init__` carries the real `bot` constructor flag
  (`flags.0 bot:true` in the TL schema) — the authoritative peer-vs-bot signal,
  available on every resolved User entity.
* `telethon/events/messagedeleted.py::MessageDeleted.build` returns
  **`peer=None`** for `types.UpdateDeleteMessages` (private/small chats) —
  Telegram does not say where such messages were deleted — so a deletion event
  for a private chat yields no peer and (with an empty chat id) is already
  excluded by the gate's positive-id rule; one-to-one scoping there depends on
  the capture boundary and on Telegram's own channel-id rule
  (`UpdateDeleteChannelMessages` → negative).
* `events.NewMessage` / `MessageEdited` events resolve their chat entity offline
  from Telethon's own entity cache, so the classification adds no RPC on the
  normal capture path.

## 3. Resolution (implemented, no heuristics anywhere)

* **Classification is the resolved entity only**:
  `service.is_bot_entity(entity)` is True only for a `User`-typed entity whose
  real `bot` flag is `True`. Missing, unresolved and non-User entities return
  False = **unknown** — absence of evidence is never treated as evidence of a
  human; no username/name/id-pattern/content heuristic exists.
* **Earliest boundary**: each capture handler classifies the event's own chat
  entity and drops a confirmed bot peer BEFORE `record_message` /
  `record_deletion`; the tri-state is forwarded, and the gate additionally
  refuses `peer_is_bot=True` (`peer_is_bot` reason). A bot snapshot, a bot edit,
  and a bot deletion (where a peer resolves) can no longer create any row.
* **Persisted legacy rows**: browse-time `service.filter_bot_items` (with the
  live self client, bounded per-peer resolution cache) hides items whose peer
  resolves to a bot, in both the list and the item panels; the list states the
  hidden count. Rows whose peer cannot be resolved stay visible — the existing
  safe fallback, never a guessed-human claim. No row is rewritten and no schema
  changes.
* **Human behaviour preserved**: snapshot/edit grouping, version history,
  identity display, pagination, deletion events, the
  entity-resolution-failure fallback (capture still succeeds, identity degrades
  to the numeric id), owner isolation, retention, and the media honesty rules
  are all unchanged and still pinned by the existing 61 tests plus 11 new ones.

## 4. Not verified / limitations

* **Live Telegram verification was NOT performed.** The owner should confirm on
  a live session that a bot private chat no longer produces archive entries,
  a human private chat still does, and any pre-fix bot row disappears from the
  archive list on its next opening.
* An unknown peer (capture-time entity unresolved) is archived WITHOUT a human
  or bot claim; at browse time it is hidden only if a later live resolution
  proves it a bot. This is the documented, tested fallback — not a misclaim.
* No database, deployment, Render shell or Supabase access was used.
