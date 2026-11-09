# INVESTIGATION — Glass UI Appearance Personalization

> Source-verified against the repository at `main` / local HEAD
> `fa2dfd04c3cacbb78221a512576769fd6473ddb8` (branch
> `freebuff/99799eefb106784388e20bc8`, clean tree before this change).
> Code is authoritative; this document records what the code actually does.

## 1. Repository and delivery state at the start

- Branch: `freebuff/99799eefb106784388e20bc8`.
- HEAD: `fa2dfd04c3cacbb78221a512576769fd6473ddb8`
  (`fix(ai): probe one sibling model on an ambiguous rate limit`).
- Working tree: clean; no pre-existing modified/untracked files in the project
  root (only the ignored virtualenv and caches).
- `origin/main` is not represented by a local remote-tracking ref in this
  environment; delivery is verified after pushing with a fresh fetch.
- `AGENTS.md` is the project's single source of truth; it was read in full
  before source inspection.

## 2. The actual Glass UI path

1. `Menu` is the only text command (`backend/bot/handlers/misc.py`), matched by
   exact equality on the raw outgoing text.
2. `send_inline_panel` (`backend/helper/inline_sender.py`) delegates to
   `PanelLifecycleManager.create_panel` (`backend/helper/lifecycle.py`).
3. `inline_engine.trigger` issues `client.inline_query(helper_username, query)`
   from the **self client**; the Helper Bot answers through
   `inline_engine.register_inline_handler`, and the self client sends
   `results[0]` with `result.click(chat_id)`.
4. The sent message therefore belongs to the owner's account and carries the
   Helper Bot as `via_bot`; its buttons belong to the Helper Bot.
5. `register_callback_handlers(helper, owner_id)` is wired in
   `backend/runtime/supervisor.py` **on the Helper Bot**, so every callback
   `event.edit(...)` is a Helper Bot edit of its own inline message.
6. There are two render boundaries:
   - initial inline result: `panel_render.render(...)`, called by the
     registered inline builders;
   - callback edits: `panels._render_and_edit(...)` → `render_edit(...)`.
   `lifecycle.try_reuse_panel(...)` is a third, rarely used path that also
   renders a panel.
7. `panels._finalize_panel` injects the standard Back/Home/Close row; root
   render adds Close only. Sub-panel navigation is registry-driven
   (`panel_registry.py`).

## 3. Main-menu categories (the eligible pages)

`misc._build_menu_buttons` is the authoritative menu. It contains:

`save`, `del`, `profile`, `db`, `general`, `settings`, `cipher`, `todo`, `ai`.

`list`, `find`, `emoji`, `context` and `health` are registered with
`parent="menu"` but are **not** in the menu buttons, so they are not
top-level categories and are not eligible. The new `appearance` category is the
only menu addition.

Default title/description pairs were read from the real handlers:

| Page | Title | Static description |
|---|---|---|
| save | Save | Deep Save downloads the message and re-uploads it as a new Saved Messages message. |
| del | Delete | Choose a deletion mode: |
| profile | Profile | Choose a section: |
| db | Database | Choose an action: |
| general | General | Tap a button to execute instantly. |
| settings | Settings | — (body is live status) |
| cipher | Cipher | Private Communications / Choose a section: |
| todo | Todo | — (body is live counts/list) |
| ai | AI | — (body is live provider/model state) |
| appearance | Appearance | Personalize the Glass UI header, page text, and preview image. |

Several bodies repeat their title as a leading heading (`**General**`,
`**AI**`, `▣ **Todo**`). Composition removes that duplicate when an override
is active, so the rendered panel never shows the title twice.

## 4. Settings architecture and the real schema

`settings_service` → `panel_settings_repository` → `panel_settings` is the only
settings path. `panel_settings` is a singleton row (`key = 'global'`) with one
typed column per setting; the repository is the only module that touches the
table.

- `settings_service._DEFAULTS` and `_VALIDATORS` are the application contract.
- `settings_service.load_all()` is cache-first; `set_setting()` writes through
  the repository and refreshes the cache.
- **Known defect:** `set_setting()` returns `True` and caches the value even
  when `repo.update_field` returns `False` (a rejected write / missing column).
  That is a phantom success. The Appearance feature therefore uses new strict
  setters (`set_setting_strict`, `set_many_strict`) that return `False` and do
  not cache when the repository rejects the write.
- Migrations: `20260726143924_create_panel_settings_table.sql`,
  `20260827000001_add_missing_panel_settings_columns.sql`,
  `20261020160000_add_panel_settings_auto_reaction_settings.sql`.
- `supabase/canonical_bootstrap.sql` §6 is the consolidated schema.
- `sql/panel_settings.sql` is an older standalone snippet and does not describe
  the current column set; it was not used as authority.

The per-page map is stored as JSONB (`appearance_page_texts`) because the
number of eligible pages is a registry concern, not a schema concern; the
service validates every key against `appearance_service.MAIN_PAGES` and rejects
unknown ids.

## 5. Durable object storage

- The only existing object-storage use is
  `backend/services/anti_edit_delete_service.py`: optional Supabase Storage via
  `ARCHIVE_MEDIA_BUCKET`, gated by `media_storage_available()`.
- There is no preview bucket and no other durable media store.
- Render's local filesystem is ephemeral; it is not used for persistence.
- The project has **no image-processing dependency** (Pillow is absent), so
  image validation is implemented with a dependency-free header parser for
  PNG/JPEG/WebP (`backend/services/image_probe.py`).
- `preview_storage.py` reuses the same Supabase client with a separate bucket
  (`PANEL_PREVIEW_BUCKET`, default `panel-previews`). Only an object key plus
  probed metadata is persisted; the bucket may remain private because the
  backend serves the bytes through a public route.

## 6. Link preview requirements vs. the installed Telethon

Verified against the installed Telethon **1.43.0** (not assumed):

- `InputBotInlineMessageMediaWebPage(message, url, invert_media=None,
  force_large_media=None, force_small_media=None, optional=None, entities=None,
  reply_markup=None)` exists and accepts a keyboard.
- `InputBotInlineMessageText(message, no_webpage=None, invert_media=None,
  entities=None, reply_markup=None)` is the current text-only constructor.
- `messages.EditInlineBotMessageRequest(id, no_webpage=None, invert_media=None,
  message=None, media=None, reply_markup=None, entities=None)` accepts
  `InputMediaWebPage(url, force_large_media, force_small_media, optional)` and
  `InputMediaEmpty()`.
- `force_small_media` is a request flag; Telegram is not guaranteed to honour
  it on every client.

The Guide is a static mount at `/guide`
(`StaticFiles(html=True)`), so a metadata route cannot live beneath it. The
preview document is therefore `/preview/<version>/<panel>`; Telegram crawlers
receive Open Graph tags, while a normal browser is redirected to `/guide`.
The version is derived from the stored object key, so replacing the image
changes the URL and defeats Telegram's per-URL cache.

## 7. Unicode symbol system

`backend/helper/symbols.py` is the single registry; glyphs must be non-emoji
Unicode marks with no variation selector, unique within their category, and
labels are composed with `sym.label`. Two glyphs were added:
`ICON_APPEARANCE = ◑` and `OP_UPLOAD = ⇧`, both unused and non-emoji.
Callback data, identifiers and URLs are never font-transformed.

## 8. Design decisions

1. **One central composition layer** (`appearance_service.compose`) applied at
   the three existing render boundaries; no per-handler string replacement.
2. **Static description replacement, dynamic body preserved.** A page override
   replaces the registered static description; everything after it is kept
   verbatim. A page with no override is returned byte-identical.
3. **Global header overrides are opt-in.** Empty means each page keeps its own
   default; resetting clears the override rather than freezing the current
   value.
4. **Strict persistence.** Appearance writes never report a cache-only success.
5. **Helper Bot is the only editor.** Repaints use the inline identifier with
   `EditInlineBotMessageRequest`; the self client only downloads the upload and
   deletes the upload message.
6. **Single pending-input listener.** Media support is an extra keyword passed
   to handlers that declare `message`; no new listener, client or loop.

## 9. Known limits

- Live Telegram rendering (small preview, tap-through, keyboard retention) is
  not exercised by unit tests and remains owner-verified.
- The bucket and migration are manual owner actions; the application reports
  honest failures until they are applied.
- `panel_settings`'s legacy phantom-success behaviour still exists for other
  settings; only the Appearance surface uses the strict path in this phase.
