# IMPLEMENTATION_REPORT.md — Anti-Edit & Anti-Delete

> This file describes ONLY the current state of the Anti-Edit / Anti-Delete task
> and **replaces its previous contents in full**. The earlier reports — the
> Private Message Management report (commits `2c758d3` / `30ced36`, building on
> `bf89042` / `e3548dd` / `e8dd2c8` / `702c1d1`), the inspection-only anti-edit
> report (commit `cbeb6c1`) and the implementation report (commit `d862520`) —
> remain readable in git history. The still-relevant findings are carried
> forward in section 5. The evidence-level investigation record lives in
> `docs/investigations/anti-edit-delete-status.md`.

| Item | Value |
|---|---|
| Stage | **Implementation — complete (round 2: archive UX)** |
| Date | 2026-10-17 |
| Project / remote | LifeOS Telegram self-bot, `Onlyicing1/Telegram-self-bot` (`origin` = `https://github.com/Onlyicing1/Telegram-self-bot.git`) |
| Branch | `main` |
| Base commit (round 2) | `d862520ab75a6ab66886244bd61805fca4d178d5` (`feat(archive): implement the missing anti-edit/anti-delete handler and service`) |
| Clone type | Shallow (grafted) |
| Outcome | **Fixed (local verification).** The archive list now shows ONE item per logical message with a unified version history, and item details show a human-readable peer identity instead of bare numeric ids. The full repository test suite passes locally. **Not yet verified against the deployed Render service or live Telegram** — see section 7. |

---

## 1. Root cause of round 1 (verified, unchanged)

The wiring commits `dfca937` (memory-cleanup hook) and `330ef39` (router import +
registration) reached `main`, but the modules they reference were never
committed:

* `backend/bot/router.py` line 20 imports `anti_edit_delete` from
  `backend.bot.handlers`, and line 129 registers
  `anti_edit_delete.register(client, owner_id, tz_str)`.
* `backend/runtime/memory_cleanup.py` line 58 imports
  `backend.services.anti_edit_delete_service` and line 60 calls
  `anti_edit_delete_service.cleanup_expired()`.
* `backend/runtime/supervisor.py` imports the router at load time, so startup
  raised `ImportError: cannot import name 'anti_edit_delete' from
  'backend.bot.handlers'` before any handler registered.

This was a **missing-module** failure — not a wrong import path and not a
circular import. It was repaired in round 1 (`d862520`), which created the two
missing modules.

## 2. What round 1 implemented (unchanged)

| File | Change |
|---|---|
| `backend/bot/handlers/anti_edit_delete.py` | **New.** The `register(client, owner_id, tz_str)` entry point (the exact symbol the router imports). Registers three Telethon handlers on the ONE shared self-client — `events.NewMessage` (bounded content snapshot while present), `events.MessageEdited` (each edit version) and `events.MessageDeleted` (deletion event) — plus the Glass UI panels `archive` / `archive_list` / `archive_item` / `archive_cats` / `archive_confirm`, the `aed_*` actions, and the `retention` input. |
| `backend/services/anti_edit_delete_service.py` | **New.** The single owner of archive state: config load/sanitize/persist, the scope gate, category classification, idempotent capture (deterministic `sha1` archive ids), honest deletion recording, bounded browsing/filter/pagination, row delete/clear, `cleanup_expired()` (the symbol `memory_cleanup` calls), and optional media-object storage. |
| `backend/db/client.py` | **Appended** (`+162`): `aed_get_document`, `aed_put_document`, `aed_load_archive`, `aed_save_archive` — durable JSON persistence in the EXISTING `bot_settings` table, with the same bounded-executor + in-memory-fallback discipline as every other db helper. |
| `backend/bot/handlers/misc.py` | **`+1` line**: the mother-menu row `("Anti-Edit / Anti-Delete", "panel:archive", PRIMARY)`. |
| `tests/test_anti_edit_delete.py` | **New.** 36 tests covering the startup import contract, the registration surface, configuration, the scope gate, capture, honest recoverability, browsing, retention cleanup, media honesty, and every panel/action/input. |

## 3. What round 2 changed (the reported issues)

Two user-reported defects in the archive UX were fixed. Both are **presentation
and read-model** changes; **no new table, migration, dependency, or background
task was added**, and the stored rows were not rewritten.

| File | Change |
|---|---|
| `backend/services/anti_edit_delete_service.py` | **Grouping read-model.** Added `KIND_MESSAGE` (a presentation-only kind, never persisted), `GROUP_ID_PREFIX`, `group_id(chat_id, message_id)`, `build_items(rows)`, `filter_items(items, ...)`, `get_item(items, browse_id)` and `delete_group(chat_id, message_id)`. `build_items` collapses every `snapshot`/`edit` row of one `(chat_id, message_id)` into a single item whose `versions` are sorted newest-first and whose `version_count` is the observed history length. **Deletion rows are deliberately NOT collapsed** — a deletion stays its own independent item with its own recoverability. **Peer identity.** Rows now carry bounded `peer_id` / `peer_username` / `peer_name`; `clean_username`, `clean_peer_name`, `peer_identity`, `describe_peer` and `_identity_fields` were added; `record_message` accepts an optional `identity` mapping and `record_deletion` copies the identity from the pre-deletion content. |
| `backend/bot/handlers/anti_edit_delete.py` | **Grouped list + detail.** `_render_list` builds items, filters and pages them, and labels each button with the version count (and a `deleted` marker). `_render_item` / `_render_item_body` show `User: @username · Name · User ID: <peer>` plus the retained technical `Chat ID` / `Message ID` line, and render the **full version history** (numbered chronologically, `v1` = original) for a message. The new `_delete_group_action` (registered as `aed_delete_group`) deletes a message's whole snapshot/edit history; the item detail offers it instead of the single-row delete. **Identity capture.** `_resolve_peer_identity(event, chat_id)` reads the peer from the event's own chat entity and is passed to `record_message` for both `NewMessage` and `MessageEdited`. |
| `tests/test_anti_edit_delete.py` | **`+25` tests** (36 to 61): grouping into one button, many edits, distinct messages/chats not merged, version history in the detail, latest-content label, pagination over items, category filtering, independent deletion event, group delete (with and without a deletion event), malformed group-delete input, duplicate-deletion guard, outgoing-peer-vs-owner identity, incoming identity, username display, numeric fallback, entity-resolution failure, legacy rows without identity, persistence across reload, non-user entity, owner isolation, and the media fallback/indicator. |

## 4. Design decisions (and their evidence)

* **Grouping is a read-model, not a schema change.** Every observed version keeps
  its own row (so history, recoverability and idempotency are unchanged); only
  browsing collapses them. The grouped id is `g<chat_id>:<message_id>` — a
  leading `g` cannot collide with a bare hex row id, so both id spaces stay
  unambiguous.
* **A deletion is never merged into a message item.** A deletion is an
  independent archive event with its own recoverability; merging it would either
  hide the deletion or misrepresent the content history.
* **The peer, never the author.** In a private chat the chat entity *is* the
  other participant, while an outgoing message's `sender_id` is the OWNER. The
  identity therefore comes from `event.get_chat()` (and the positive `chat_id`),
  never from `sender_id`. Telethon populates the chat from its in-memory entity
  cache, so this adds no RPC on the normal path; a missing entity degrades to the
  numeric `peer_id` instead of blocking capture (`_resolve_peer_identity` never
  raises). Verified against the installed Telethon 1.43.0 source
  (`telethon/events/common.py::EventCommon._set_client`,
  `telethon/events/newmessage.py::NewMessage.Event._set_client`,
  `telethon/events/messageedited.py`, `telethon/events/messagedeleted.py`).
* **Identity is bounded and never inferred.** A username is dropped unless it is
  a well-formed `@name`; a display name is whitespace-normalized and truncated;
  nothing is guessed. `peer_identity` falls back to `chat_id` for rows captured
  before these fields existed, so the numeric id is always shown even for legacy
  data.
* **The technical identifiers are retained.** The `Chat ID` / `Message ID` line
  is kept alongside the human-readable identity for troubleshooting; this is a
  display change, not a removal of data.
* **No new database table and no new migration.** `DATABASE_ARCHITECTURE.md` is a
  protected, documented contract, and
  `tests/test_canonical_schema_reconciliation.py::test_no_migration_creates_a_table_that_the_documentation_does_not_classify`
  asserts that every `CREATE TABLE` in `supabase/migrations/` is classified in
  that document. The archive reuses the **existing** `bot_settings` key/value
  table (the same store `ghost_seen_v2` uses), as JSON documents split across a
  small, fixed number of shards.
* **Deletion recoverability is honest.** A deletion row is `recoverable=True`
  only when the content was actually observed and stored beforehand; otherwise
  the row records the event with an empty body and `recoverable=False`.
* **Scope is applied everywhere** (capture, browse, cleanup): private
  user-to-user chats only — groups/supergroups/channels (negative ids), Saved
  Messages (`chat_id == owner_id`) and inline-bot-origin messages are excluded.
* **Media durability remains a documented limitation.** No durable media store
  exists in this repository; media is metadata-only by default, with an optional
  bounded upload when `ARCHIVE_MEDIA_BUCKET` is provisioned.
* **Retention rides the existing worker.** `cleanup_expired()` is called by
  `backend/runtime/memory_cleanup.py`; no second scheduler or task was added.
* **Default is OFF**, matching the repository convention for capture features.

## 5. Findings carried forward (still relevant)

* The feature was wired but unimplemented at `cbeb6c1`; the two modules were
  missing and the bot could not start. Repaired in `d862520`.
* `backend/bot/handlers/__init__.py` is a comment-only file and re-exports
  nothing — the `from backend.bot.handlers import anti_edit_delete` form works
  because it imports the submodule by name, which is why adding the module was
  the correct fix and no `__init__.py` change is needed.
* `INVESTIGATION.md` (tracked) documents a **different, completed** task (Inline
  Button Color Semantics, commit `2805a88`); it was not touched.
* `docs/investigations/anti-edit-delete-status.md` and older reports remain in
  place and unmodified.

## 6. Verification actually executed (local only)

* `python -m py_compile backend/services/anti_edit_delete_service.py
  backend/bot/handlers/anti_edit_delete.py` -> **clean**.
* `git diff --check` -> **clean** (no whitespace errors).
* `python -m pytest tests/test_anti_edit_delete.py -q` -> **61 passed**.
* `python -m pytest tests/test_symbols.py -q` -> **24 passed** (the symbol-registry
  invariants still hold; no new glyph was introduced).
* `python -m pytest tests/ -q` -> **6360 passed, 27 skipped, 0 failed** (full
  repository suite; requires `pytest-asyncio`, which the STT/TTS tests rely on).
* Tests exercise the REAL handler registrations, the REAL service and the REAL
  `bot_settings`-backed persistence path, with a fake key/value table and a
  recording self client. **No live Telegram, no helper bot, no Supabase.**

## 7. Not done / out of scope / not yet verified

* **Not verified against the deployed Render service.** Nothing was deployed,
  no Render shell was used, and the running service's log was not inspected.
  The round-1 startup `ImportError` is verified fixed **in source and by
  import/compile locally**, not on Render.
* **Not verified against live Telegram.** No real edit or deletion was captured
  end to end; grouping and identity are verified against the real code paths with
  synthetic events that mirror Telethon's event shapes.
* **Existing stored rows are not back-filled.** Rows captured before round 2 have
  no `peer_username`/`peer_name`; they display the numeric `User ID` (which equals
  the chat id in a private chat) and are otherwise unchanged.
* No live Telegram, no deployment, no Render shell, no SQL, no Supabase call.
* No database migration was added and no Supabase schema was modified.
* The optional `ARCHIVE_MEDIA_BUCKET` storage path is **not exercised** against a
  real bucket (none is provisioned); its code path is inert without one.

## 8. Next stage

`Complete` for round 2. The remaining verification that only the owner can
perform is a live check on the deployed service: enable the feature, edit and
delete a message in a private chat, and confirm the archive list shows one item
with a version count and the detail shows `@username` / `User ID`. Any further
enhancement — a normalized archive table with a documented migration, identity
back-fill, or a provisioned media bucket — is a separate, owner-authorized task.
