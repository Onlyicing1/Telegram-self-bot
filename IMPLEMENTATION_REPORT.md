# IMPLEMENTATION_REPORT.md — Anti-Edit & Anti-Delete

> This file describes ONLY the current state of the Anti-Edit / Anti-Delete task
> and **replaces its previous contents in full**. The earlier reports — the
> Private Message Management report (commits `2c758d3` / `30ced36`, building on
> `bf89042` / `e3548dd` / `e8dd2c8` / `702c1d1`) and the inspection-only
> anti-edit report (commit `cbeb6c1`) — remain readable in git history. Nothing
> from them is contradicted here; the still-relevant findings are carried forward
> in §4. The evidence-level investigation record lives in
> `docs/investigations/anti-edit-delete-status.md`.

| Item | Value |
|---|---|
| Stage | **Implementation — complete** |
| Date | 2026-10-17 |
| Project / remote | LifeOS Telegram self-bot, `Onlyicing1/Telegram-self-bot` (`origin` = `https://github.com/Onlyicing1/Telegram-self-bot.git`) |
| Branch | `main` |
| Base commit | `cbeb6c11cdb0bce48a7b5cfd18a2a0b299a19050` (`docs(archive): document anti-edit-delete implementation status`) |
| Clone type | Shallow (grafted) |
| Outcome | **Fixed.** The two missing modules now exist; the startup `ImportError` is gone; the feature captures, archives, browses and expires; the full repository test suite passes. |

---

## 1. Root cause (verified, unchanged)

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
circular import.

## 2. What was implemented

| File | Change |
|---|---|
| `backend/bot/handlers/anti_edit_delete.py` | **New.** The `register(client, owner_id, tz_str)` entry point (the exact symbol the router imports). Registers three Telethon handlers on the ONE shared self-client — `events.NewMessage` (bounded content snapshot while present), `events.MessageEdited` (each edit version) and `events.MessageDeleted` (deletion event) — plus the Glass UI panels `archive` / `archive_list` / `archive_item` / `archive_cats` / `archive_confirm`, the `aed_*` actions, and the `retention` input. Handlers are thin; all logic lives in the service. |
| `backend/services/anti_edit_delete_service.py` | **New.** The single owner of archive state: config load/sanitize/persist, the scope gate, category classification, idempotent capture (deterministic `sha1` archive ids), honest deletion recording, bounded browsing/filter/pagination, row delete/clear, `cleanup_expired()` (the symbol `memory_cleanup` calls), and optional media-object storage. |
| `backend/db/client.py` | **Appended** (`+162`): `aed_get_document`, `aed_put_document`, `aed_load_archive`, `aed_save_archive` — durable JSON persistence in the EXISTING `bot_settings` table, with the same bounded-executor + in-memory-fallback discipline as every other db helper. |
| `backend/bot/handlers/misc.py` | **`+1` line**: the mother-menu row `("Anti-Edit / Anti-Delete", "panel:archive", PRIMARY)` (the row `330ef39` claimed but never delivered). |
| `tests/test_anti_edit_delete.py` | **New.** 36 tests: the startup import contract, the full registration surface, configuration, the scope gate, snapshot/edit/deletion capture, honest recoverability, browsing, retention cleanup, media honesty, and every panel/action/input. |

## 3. Design decisions (and their evidence)

* **No new database table and no new migration.** `DATABASE_ARCHITECTURE.md` is a
  protected, documented contract, and
  `tests/test_canonical_schema_reconciliation.py::test_no_migration_creates_a_table_that_the_documentation_does_not_classify`
  asserts that every `CREATE TABLE` in `supabase/migrations/` is classified in
  that document. A new archive table would therefore have required editing the
  protected document and adding a migration part — outside the smallest safe
  change set. The archive instead reuses the **existing** `bot_settings`
  key/value table (the same store `ghost_seen_v2` uses), as JSON documents split
  across a small, fixed number of shards so no read/write grows without bound.
* **Deletion recoverability is honest.** A deletion row is `recoverable=True`
  only when the content was actually observed and stored beforehand (its
  snapshot); otherwise the row records the event with an empty body and
  `recoverable=False`. Nothing is ever fabricated.
* **Scope is applied everywhere** (capture, browse, cleanup): private
  user-to-user chats only — groups/supergroups/channels (negative ids), Saved
  Messages (`chat_id == owner_id`) and inline-bot-origin messages are excluded.
* **Media durability is a documented limitation.** No durable media store exists
  in this repository (`save_service` deletes its temp download; no Supabase
  Storage bucket is used anywhere). Media is therefore metadata-only by default;
  when the owner provisions a bucket and sets `ARCHIVE_MEDIA_BUCKET`, a bounded
  snapshot is uploaded and its object path recorded. The service never claims
  durable media archival it cannot deliver.
* **Retention rides the existing worker.** `cleanup_expired()` is called by
  `backend/runtime/memory_cleanup.py`; no second scheduler or task was added.
* **Default is OFF**, matching the repository convention for capture-style
  features; enabling is an explicit owner action.

## 4. Findings carried forward (still relevant)

* The feature was wired but unimplemented at `cbeb6c1`; the two modules were
  missing and the bot could not start.
* `backend/bot/handlers/__init__.py` is a comment-only file and re-exports
  nothing — the `from backend.bot.handlers import anti_edit_delete` form works
  because it imports the submodule by name, which is why adding the module is the
  correct fix and no `__init__.py` change is needed.
* `INVESTIGATION.md` (tracked) documents a **different, completed** task (Inline
  Button Color Semantics, commit `2805a88`); it was not touched.
* `docs/investigations/anti-edit-delete-status.md` and older reports remain in
  place and unmodified.

## 5. Verification actually executed

* `python -c "import backend.runtime.supervisor"` → **OK** (previously raised the
  `ImportError`; this is the startup path).
* `python -m py_compile` on every changed module → clean.
* `python -m pytest tests/test_anti_edit_delete.py -q` → **36 passed**.
* `python -m pytest tests/ -q` → **6335 passed, 27 skipped, 0 failed** (full
  repository suite; requires `pytest-asyncio`, which the STT/TTS tests rely on).

## 6. Not done / out of scope

* No live Telegram, no deployment, no Render shell, no SQL, no Supabase call.
* No database migration was added and no Supabase schema was modified.
* The optional `ARCHIVE_MEDIA_BUCKET` storage path is **not exercised** against a
  real bucket (none is provisioned); its code path is inert without one.

## 7. Next stage

`Complete`. Any future enhancement — a normalized archive table with a documented
migration, or a provisioned media bucket — is a separate, owner-authorized task.
