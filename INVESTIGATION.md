# Investigation — Appearance personalization for the Glass UI

Snapshot inspected: `5f8437d1` (`origin/main` at the start of the task, also the
local HEAD, clean working tree). Everything below was verified against that
source; no claim here comes from an earlier report.

## 1. How a Glass UI panel is built, rendered and edited

```
Menu (self-client, exact text)                 backend/bot/handlers/misc.py
  → helper inline panel  (inline_engine)        backend/helper/inline_engine.py
      build results via the registered builder  backend/helper/panel_registry.py
      send via the Helper Bot                   backend/helper/inline_sender.py
  → callback  panel:* / action:* / input:*      backend/helper/panels.py
      _callback_router → callback_gate (ack, FIFO turn, painter)
      _handle_panel  → registered panel fn      backend/helper/panel_registry.py
                     → ExistingPanelEditor      backend/helper/panel_edit.py
                     → Helper Bot edit by inline_message_id
  → typed input   input:*                       backend/helper/input_state.py
      ONE pending-input listener (register_input_listener) → handler
```

Facts that constrained the design:

* The **initial** panel is an inline result built by
  `panel_registry`/`inline_engine`; it never passes through
  `panels._handle_panel`. Any composition done only in `_handle_panel` would
  therefore miss the first render and appear only after the first click.
* `panels._handle_panel(client, event, panel_id, extra)` is the single funnel for
  every callback navigation that returns `(title, body, buttons)`. `extra` is
  the sub-view marker (`""` = the page's own home view; anything else = a
  confirmation, picker, pagination page, input prompt or dynamic result).
* `ExistingPanelEditor(inline_id, helper)` is the only editor used for the
  inline panel; `panel_edit.py` retries a refused inline edit through the
  self-client **only when one is passed in**. The inline identifier lives on a
  `CallbackQuery` as `query.msg_id` (`inline_message_id_from_event`).
* `register_input(...)` registers state on the ONE owner pending-input listener;
  `input_state` deletes the owner's trigger message and dispatches to the
  handler. No handler may create another listener, client or update loop.

## 2. The settings architecture that already exists

`settings_service` → `panel_settings_repository` → `panel_settings` (one
singleton row, `key = 'global'`).

* `settings_service` owns a `_DEFAULTS` map, per-key validators, a
  cache-first read and a write-through setter; a rejected DB write is logged
  and the cache keeps the value (the documented no-database behaviour).
* `panel_settings_repository` writes **real columns** (`SET <column> = …`) and
  refuses unknown keys, so adding a setting without a migration means the write
  is rejected and the value survives only in the process cache — the exact
  defect fixed for Auto-Reaction in `20261020160000_…` and re-checked here.
* `db_client.get_db()` returns `None` when Supabase is not configured, so in
  tests (and on a bare local run) every write is cache-only and honest.

## 3. The actual `panel_settings` schema and its history

The createdAt column set is defined by the canonical reconciliation snapshot
(`20260920000001_reconcile_canonical_schema.sql`, part 1 of §31.3) and extended
by four later additive migrations (Auto-Reaction was the last one before this
task). Seven settings columns already existed at `5f8437d1`
(`auto_delete_config`, `custom_emoji_enabled`, `auto_reaction_enabled`,
`auto_reaction_emoji`, …), each added by its own migration — the established
pattern was therefore "one additive migration per setting group", not "adding a
column to an old file".

Two doc-audit suites pin the shape of `DATABASE_ARCHITECTURE.md` itself:
`tests/test_database_setup_order.py` (statement-identity of the embedded parts,
counts, §30.11 list) and `tests/test_private_message_db_security.py`
(part numbering, migration/file counts). **Any new migration must be added as a
new `PART n of N` inside the ONE §31.3 setup block, and both suites' constants
must move with it.** They hard-coded 36 migrations / 14 parts.

## 4. Durable object storage that already exists

`backend/services/archive_media.py` (and the Anti-Edit archive) already store
BLOBs in **Supabase Storage** through the service-role client:
`create_bucket`/`upload`/`download`/`remove` on a configurable bucket name
(`ARCHIVE_MEDIA_BUCKET`), with a stable object key persisted in the database and
**never** the bytes or a signed URL. That is exactly the required model, so the
Appearance preview reuses the same mechanism and the same bucket by default
(`APPEARANCE_MEDIA_BUCKET`, falling back to `ARCHIVE_MEDIA_BUCKET`); Render's
local filesystem is never involved. The bucket's access model is the one the
repository already relies on: private bucket, service-role writes/reads, and the
image is re-served by the app from `/appearance/image/<version>`.

## 5. Web routing and the Guide

`backend/web/app.py` mounts the SPA/static assets **last**; explicit routes are
declared before it and already include `/guide`-style pages and API routes. New
routes therefore must be declared before the SPA fallback or they are swallowed
by it. `guide/index.html` is the existing public Guide page, which is the
natural tap target for the preview.

## 6. Telethon capability check (installed version, not assumed)

Installed Telethon 1.43 provides `InputBotInlineMessageMediaWebPage` and
`InputMediaWebPage`, and `client.edit_message(..., link_preview=…)` accepts a
`bool`/media for the outgoing-edit path. The inline **edit** path used here
(`messages.editInlineBotMessage` via the Helper Bot) cannot carry
`force_small_media` for an already-sent message through this code, so the
implemented preview uses Telegram's ordinary **web-page preview** of a public
HTTPS URL (`no_webpage=False` + URL appended). Telegram decides the layout; the
panel never claims a forced small preview.

## 7. The symbol registry

`backend/helper/symbols.py` is the single source of truth: functional,
non-emoji glyphs, no `U+FE0F`, grouped into categories with a documented-reuse
set, and `tests/test_symbols.py` enforces uniqueness. The Appearance UI needed
no new symbol: it reuses `OP_EDIT`, `PANEL_HEADING`, `MEDIA_PHOTO`, `OP_SAVE`,
`OP_STOP`, `OP_REFRESH`, `OP_DELETE`, `OP_CREATE`, `STATE_COMPLETED`,
`STATE_WARNING`, `STATE_FAILED` and `SEPARATOR` through `sym.label(...)`, so the
font pipeline never sees a new glyph and callback data is untouched.

## 8. Discovered defects and gaps this task had to handle

1. **No central text layer existed.** Every handler returned its own
   `(title, body, buttons)`; nothing could prepend a header or override one
   page's text without editing eleven handlers. The narrowest correct boundary
   is `panels._handle_panel` (callback navigation) plus
   `inline_engine._inline_router` (first render).
2. **`settings_service` could not report a rejected write.** A strict setter was
   missing, so a "saved" value could be cache-only without the caller knowing.
   `set_setting_strict` was added for the Appearance writes (the cache-only
   path stays the documented fallback when no database is configured).
3. **`_safe_edit` did not pass `link_preview`.** Test doubles and older shims
   without that keyword raised `TypeError`; a bounded fallback was added so the
   preview is best-effort and an edit still happens.
4. **`with_preview` initially appended the URL even when preview was OFF**,
   producing a preview on a "disabled" panel. Caught by the new tests; the URL
   is now appended only when `preview_active()`.
5. **Menu size.** The menu had nine entries; Appearance is the tenth. A test
   pins that the menu's `panel:*` destinations equal
   `appearance_service.MAIN_PAGE_IDS`, so eligibility can never drift from the
   real navigation.
