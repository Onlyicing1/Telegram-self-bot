# IMPLEMENTATION_REPORT.md — Anti-Edit & Anti-Delete (current state + bot exclusion)

> This file describes ONLY the current state of the repository with respect to
> the **Anti-Edit / Anti-Delete archive feature**, including the Telegram-bot
> exclusion fix, and **replaces every previous report in full**. The earlier
> reports — the implementation round (`d862520`), the archive-UX round
> (`33e1fd3`) building on the Private Message Management work, and the
> inspection-only record (`cbeb6c1`, which documented the missing-module
> startup failure) — remain readable in git history. The evidence-level
> investigation record lives in `docs/investigations/anti-edit-delete-status.md`.

| Item | Value |
|---|---|
| Stage | **Complete — bot exclusion delivered (round 3)** |
| Date | 2026-10-17 |
| Project / remote | LifeOS Telegram self-bot, `Onlyicing1/Telegram-self-bot` (`origin` = `https://github.com/Onlyicing1/Telegram-self-bot.git`) |
| Branch | `main` |
| Base commit (round 3) | `33e1fd3e30937d67b5413624661764d91c21ac5f` (`fix(archive): group edited messages into one item and show user identity`) — verified `= origin/main` at task start |
| Telegraph peer classification | Verified against the installed **Telethon 1.43.0**: `telethon.tl.types.User` carries the real `bot` constructor flag (`flags.0 bot:true` in the TL schema); `events.MessageDeleted` for private/small conversations carries **no peer** (`peer=None` → `UpdateDeleteMessages`), so scoping there leans on the capture boundary and the id rules |
| Live Telegram in this workspace | **NO — not performed.** No `.env`, no credentials; no Telegram request of any kind was made |

---

## 1. Root cause of this round (verified)

The archive's scope gate — `anti_edit_delete_service.eligible()` — decided
eligibility purely on the **chat id shape** (`chat_id > 0` private) plus the
`owner` / Saved Messages / `via_bot_id` rules. **At no stage did any code read
the resolved Telegram entity's real `User.bot` flag**:

* `record_message` validated `chat_id > 0`, `is_private`, `Saved Messages` and
  `via_bot_id` only — a **bot** private chat satisfies all of them, because a
  bot's private chat id is the bot's own positive user id.
* The handler's `_resolve_peer_identity()` resolved the peer entity through
  `event.get_chat()` (Telethon's offline entity cache) **only to copy its
  `username` / display name into the row** — the classification-by-wrapper issue
  the owner saw: `@sexyboyvpn_bot` was archived as `Kind: message` with the bot
  username and name shown as the user identity, because the identity was
  *displayed* from the same entity whose `bot` flag was never *consulted*.
* The `MessageEdited` path reused the same gate, so a bot edit could create an
  archive record too; the `MessageDeleted` path used the same gate on the event's
  (rarely present) chat id.
* Persisted bot rows predated no metadata: rows carry no bot field, so browsing
  continued to serve them as ordinary human-archive items.

No username/name/id-pattern heuristic was, or is, used anywhere — the fix below
classifies strictly from the resolved Telethon `User` entity's `bot` flag.

## 2. What changed (exact files and behaviour)

| File | Change |
|---|---|
| `backend/services/anti_edit_delete_service.py` | **Scope gate (the ONE gate, shared by capture, browse and cleanup).** `eligible()` accepts a tri-state `peer_is_bot` and refuses a confirmed bot peer with reason `peer_is_bot`. **Classification.** New `is_bot_entity(entity)` — True only for a resolved `User` whose real `bot` flag is `True`; missing/unresolved/non-User entities return False = unknown (absence of evidence is never guess-evidence). **Browse-time exclusion of persisted bot rows.** New bounded `_peer_bot_cache` (512 entries, FIFO-evicted, cleared by `reset_in_memory`), `peer_bot_status(client, chat_id)` (one resolution per unique peer per process lifetime, failures stay unknown and uncached) and `filter_bot_items(client, items)` → `(filtered, hidden_count)`; hides persisted items whose peer *resolves* to a bot and keeps unresolvable ones (documented safe fallback). `record_message` / `record_deletion` accept `peer_is_bot` and forward it to the gate. Module docstring scope statement updated: HUMAN private conversations only. |
| `backend/bot/handlers/anti_edit_delete.py` | **Capture boundary.** `_resolve_peer_identity` now returns `(identity, peer_is_bot)` computed from the SAME resolved entity; the `NewMessage` and `MessageEdited` capture handlers drop a confirmed bot peer BEFORE `service.record_message`, and the `MessageDeleted` handler classifies the event's own chat entity (when a positive chat id provides one) before `service.record_deletion` — with the tri-state (`None` = unknown) forwarded. Every handler keeps its `try/except` boundary; the bot check can never crash or miss-classify. **Listing / detail.** `_render_list` and `_render_item` run `filter_bot_items` over the built items (using the live self client via `inline_engine.get_self_client()`) so persisted bot rows predating the fix no longer render; the list notes `N persisted item(s) hidden: the peer resolved as a Telegram bot` when it happens. Module docstring documents the boundary and its limitation. |
| `tests/test_anti_edit_delete.py` | **`+11` tests** (61 → 72): the classification function (`User.bot` is the only positive signal; `None`/human/non-User are not), the tri-state gate, bot private message not snapshotted, bot edit cannot create a record, bot deletion not recorded when the peer resolves, unclassifiable deletion keeps the existing fallback, service-level rejections, human message still archived next to a bot, persisted legacy bot rows hidden from listing AND detail (human rows kept, hidden-count note, direct item access yields "no longer in the archive"), the no-client / resolution-failure fallback, and the bounded cache. The test `User` stand-in now exposes the real `bot` field, mirroring Telethon 1.43.0's constructor. |

Not changed: `router.py` wiring, `misc.py` menu row, `db/client.py` helpers,
`memory_cleanup.py`, retention/media semantics, row schema (still the same
`bot_settings` JSON documents — no migration, no Supabase change,
`DATABASE_ARCHITECTURE.md` untouched), pagination, grouping, identity display,
deletion recoverability, and every previously pinned fallback.

## 3. What the feature is now (unchanged parts, for the record)

* **Capture** — three Telethon handlers on the ONE shared self client
  (`NewMessage` snapshot / `MessageEdited` version / `MessageDeleted` event),
  registered last; bounded content snapshots while present; deterministic
  `sha1` archive ids for idempotency; category classification and filter.
* **Persistence** — existing `bot_settings` key/value table, JSON documents in
  4 fixed shards, `aed_*` helpers with the repo's bounded-executor +
  in-memory-fallback discipline; max 500 rows; default OFF.
* **Browse** — one item per logical message (snapshot + edits grouped,
  `g<chat_id>:<message_id>` ids), deletions never merged, version history
  rendered chronologically, peer identity (`@username` · name · `User ID`),
  pagination (8/page), delete-row / delete-group / clear actions.
* **Scope** — private user-to-user chats with HUMAN accounts only: negative ids
  (groups/supergroups/channels), Saved Messages, `via_bot_id` messages and
  confirmed bot peers excluded at the earliest reliable boundary; bots cannot
  re-enter via edits or deletions.
* **Cleanup** — `cleanup_expired()` rides the existing `memory_cleanup` worker;
  retention 1–3650 days, default 30; unparseable timestamps are kept.
* **Media** — metadata only unless `ARCHIVE_MEDIA_BUCKET` is provisioned;
  recoverability is honest (`recoverable=True` only from a pre-deletion
  snapshot).

## 4. Verification actually executed (local only)

| Check | Result |
|---|---|
| `.venv/bin/python -m pytest tests/test_anti_edit_delete.py -q` | **72 passed** |
| `.venv/bin/python -m pytest tests/ -q` (full suite) | **6371 passed, 27 skipped, 0 failed, exit 0** (27 skips are the pre-existing opt-in/live tests) |
| `.venv/bin/python -m compileall` on the three touched files | **exit 0** |
| `git diff --check` | **clean** |
| Telethon API verification (installed 1.43.0 source) | `User.__init__` carries `bot`; `MessageDeleted.build` returns `peer=None` for `UpdateDeleteMessages` (private chats) |
| Push status | Verified after push via `git fetch origin` + `git rev-parse origin/main` + `git merge-base --is-ancestor` — see commit section in the task summary |

## 5. Not verified / limitations

* **Live Telegram verification was NOT performed** (no credentials). The fix is
  verified in source and by the local suite against synthetic events that mirror
  Telethon's shapes; the owner should confirm on a live session: a bot private
  chat (`@sexyboyvpn_bot` or any bot) produces no archive entry, a human private
  chat still does, and any pre-existing bot row disappears from the archive list
  on its next opening (with the hidden-count note).
* **Unknown peers keep the safe fallback.** A capture whose peer entity cannot
  be resolved is still archived WITHOUT any human/bot claim, and an unresolvable
  browse peer stays visible — never guessed to be human; this is pinned by tests
  and documented in both production modules and
  `docs/investigations/anti-edit-delete-status.md`.
* Bot classifications at browse time are only as good as the live entity
  resolution of the moment (cached, bounded); a bot whose entity resolves later
  is hidden on the next listing, not instantly.
* The pre-existing unrelated working-tree change
  (`backend/services/premium_emoji_inline_service.py`, untracked
  `backend/test_patch_story.md`, `scratchpad/`, `telegram-self-bot/`) was
  preserved untouched and excluded from this task's commit.
