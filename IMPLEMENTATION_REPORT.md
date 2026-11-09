# IMPLEMENTATION REPORT — Glass UI Appearance (Stage 1)

## 1. Implemented

### Architecture
- One new `Appearance` main-menu category (`panel:appearance`), registered
  through the existing `register_panel` / inline-builder / action / input
  registries. No second navigation system, settings framework, table, listener,
  client or update loop.
- One central composition layer
  (`backend/services/appearance_service.py::compose`) applied at the three
  existing render boundaries:
  - initial inline result (`panel_render.render`),
  - callback edit (`panels._render_and_edit`),
  - panel reuse (`lifecycle.try_reuse_panel`).
  Handlers were not edited to replace strings.
- Strict persistence: the Appearance surface uses
  `settings_service.set_setting_strict` / `set_many_strict`; a rejected
  repository write returns failure and does **not** only change the cache.

### Navigation and Unicode
- The main menu gained exactly one row: `Appearance`.
- Sections: Header Customization, Page Text Customization, Preview Image, each
  with its own sub-panel and the existing Back/Home/Close convention.
- Labels compose glyphs from `backend/helper/symbols.py`; two non-emoji Unicode
  glyphs were added: `ICON_APPEARANCE = ◑` and `OP_UPLOAD = ⇧`. No variation
  selectors, no new emoji, and callback data/identifiers/URLs are untouched by
  the font pipeline.

### Global header
- Independent global title and description overrides; edit title, edit
  description, reset title, reset description, reset both.
- Defaults are the actual per-page defaults held in code; an override is opt-in
  and reset clears it (the current customized value is never its own default).
- Bounded validation (title ≤64, description ≤500); invalid/oversized input is
  rejected without changing the previous value; rejected writes are reported.

### Per-page text
- Eligible pages are derived from the real top-level menu categories only:
  `save`, `del`, `profile`, `db`, `general`, `settings`, `cipher`, `todo`,
  `ai`, `appearance`. Sub-panels, pickers, input screens and dynamic result
  screens are not eligible.
- Each page independently supports title edit/body edit, title reset/body
  reset/full reset, and shows the effective title/description.
- Title-only and body-only overrides keep the other default. Dynamic content is
  preserved: an override replaces the page's static description, and everything
  after it remains verbatim. Duplicate leading title headings are removed when
  a customization is active. Uncustomized pages render byte-identically.
- Unknown page ids and unknown fields are rejected.

### Preview image
- Independent ON/OFF switch and stored-image reference; turning OFF never
  deletes the image, and turning ON reuses it when present. ON with no image
  shows an upload-or-skip notice and keeps the panel functional.
- Upload/replace/remove through the EXISTING pending-input listener. Handlers
  that declare `message` now receive the Telegram message; no second listener
  was registered.
- Validation parses the actual bytes (PNG/JPEG/WebP headers), enforces ≤5 MB and
  16–4096 px per side, rejects malformed/unsupported media, bounds the download
  before it starts, and cleans up a newly uploaded object if persisting its
  reference fails. Replacement removes the old object only after the new
  reference is persisted; removal clears the reference before deleting.
- Durable storage reuses Supabase Storage via `PANEL_PREVIEW_BUCKET` (default
  `panel-previews`). Only the object key (`previews/<32 hex>.<ext>`) and probed
  metadata are stored in `panel_settings`; never image bytes and never an
  expiring signed URL. No new image dependency.

### Telegram link preview
- `render(...)` uses `InputBotInlineMessageMediaWebPage` with
  `force_small_media=True` and the existing `ReplyInlineMarkup` when the switch
  is ON and an image/reference/public base exist; otherwise it uses
  `InputBotInlineMessageText` (genuine text-only).
- Public, versioned route `/preview/<version>/<panel>` serves Open Graph
  metadata (`og:title`, `og:description`, `og:image` + type/width/height,
  `og:url` = `/guide`) to Telegram and redirects a human browser to `/guide`.
  `/preview/<version>/<panel>/image` streams the stored bytes.
  The version is derived from the stored object key, so replacement changes the
  URL identity and defeats Telegram's preview cache.
- The stored image is served by the backend (service-role read); the bucket may
  stay private.

### Editing ownership
- Panel repaints use only the Helper Bot's inline identifier with
  `messages.EditInlineBotMessageRequest` (media set to `InputMediaWebPage` or
  `InputMediaEmpty`). There is no self-client edit and no self-client fallback
  in this flow; the self client only downloads the owner's upload and deletes
  that upload message. No replacement panel is ever sent.
- Callbacks are registered on the Helper Bot
  (`runtime/supervisor.py::register_callback_handlers(helper, …)`), so ordinary
  callback edits are Helper Bot edits too.

## 2. Tested and passed

- Focused new suite `tests/test_appearance.py`: **30 passed**.
  Covers navigation/registration, owner gating, defaults, global and per-page
  composition, independence, resets, invalid/oversized input, strict-write
  honesty, reload persistence, switch/image independence, version changes,
  preview media vs. text-only rendering, OG document, PNG/JPEG/WebP probing,
  malformed/oversized rejection, storage upload/remove, cleanup after a rejected
  reference, Helper-Bot-only repaint, no-inline-identifier failure, and the web
  routes (Telegram OG, human redirect, image bytes, stale version, switch OFF).
- Existing focused regression set (symbols, Todo UI, Cipher menu, colored
  buttons, Guide site, settings model contract, settings unknown key, private
  message panel, premium emoji): **316 passed**.
- Schema/inventory set (`test_database_setup_order`,
  `test_private_message_db_security`, `test_canonical_schema_reconciliation`,
  `test_appearance`): **132 passed**.
- Full suite: **7020 passed, 27 skipped, 1 failed**.
  The single failure is
  `tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`
  ("interval advancement exceeded bound"). It reproduces in isolation, touches
  no file in this change, and was already the repository's known scheduler
  failure; it is reported as pre-existing, not fixed or hidden here.
- `python -m py_compile` on every changed Python file: **passed**.
- `git diff --check`: **passed**.

## 3. Not tested

- No live Telegram client was used: small-preview rendering, tap-through,
  keyboard retention, preview-cache invalidation, and media replacement on a
  real inline message are **not live-verified**.
- No Supabase project or Storage bucket was contacted; upload/download/remove
  are tested against fakes only.
- The SQL script has not been executed (and must not be by this agent).

## 4. Requires manual owner action

1. Apply `sql/panel_settings_appearance.sql` in the Supabase SQL editor as
   `postgres`. Until then, Appearance writes fail honestly (columns absent).
2. Create the private Storage bucket (the file's header contains the exact SQL):
   `panel-previews`.
3. Set the environment:
   - `PANEL_PREVIEW_BUCKET=panel-previews`
   - `PUBLIC_BASE_URL=https://<public-service-origin>`
     (`RENDER_EXTERNAL_URL` is accepted as a fallback; without either, the panel
     warns that Telegram cannot fetch the preview).
4. Optionally add `ANON` read policy — **not required**: the backend reads with
   the service-role key and serves the bytes.

Rollback (also in the SQL file header):

```sql
ALTER TABLE panel_settings DROP COLUMN IF EXISTS appearance_preview_meta;
ALTER TABLE panel_settings DROP COLUMN IF EXISTS appearance_preview_image;
ALTER TABLE panel_settings DROP COLUMN IF EXISTS appearance_preview_enabled;
ALTER TABLE panel_settings DROP COLUMN IF EXISTS appearance_page_texts;
ALTER TABLE panel_settings DROP COLUMN IF EXISTS appearance_header_body;
ALTER TABLE panel_settings DROP COLUMN IF EXISTS appearance_header_title;
```

The script is intentionally **not** an audited `supabase/migrations/` entry, so
the canonical §31 one-paste setup and its 36-file inventory are unchanged; it is
manual setup, like the storage bucket itself.

## 5. Not live-verified

- Telegram's actual small-preview rendering and whether it honors
  `force_small_media` on every client.
- Whether an existing inline message keeps its webpage media across plain text
  callback edits on current Telegram clients (the code deliberately sets the
  media explicitly on every Appearance repaint, including `InputMediaEmpty` for
  OFF).
- Real Supabase Storage behavior (bucket permissions, object reads, replacement
  cleanup).

## 6. Deferred

- Curated image gallery, external image search, generated default image.
- Web-dashboard upload workflow.
- Multiple images, per-page preview images, rotation.
- A shared strict-write migration for the legacy panel settings.

## 7. Changed files

New:
- `backend/services/appearance_service.py`
- `backend/services/image_probe.py`
- `backend/services/preview_storage.py`
- `backend/bot/handlers/appearance.py`
- `sql/panel_settings_appearance.sql`
- `tests/test_appearance.py`

Modified:
- `backend/helper/symbols.py` (two non-emoji glyphs + registry entries)
- `backend/helper/panel_render.py` (central composition + preview media)
- `backend/helper/panels.py` (callback composition; input timeout config)
- `backend/helper/lifecycle.py` (reuse composition)
- `backend/helper/inline_engine.py` (panel-scoped render context)
- `backend/helper/inline_sender.py` (optional `message` handler kwarg)
- `backend/services/settings_service.py` (defaults/validators/strict setters)
- `backend/bot/handlers/misc.py` (one menu entry)
- `backend/bot/router.py` (register the Appearance handler)
- `backend/web/app.py` (versioned preview + image routes)
- `DATABASE_ARCHITECTURE.md` (§6 columns + manual-setup note)
- `INVESTIGATION.md`, `ROADMAP.md`, `IMPLEMENTATION_REPORT.md`

No secrets, credentials, image bytes or signed URLs were added.

## 8. Delivery state

- Starting HEAD: `fa2dfd04c3cacbb78221a512576769fd6473ddb8`.
- The delivery commit contains this report; its SHA is the repository HEAD at
  delivery and is verified against a fresh `origin/main` fetch
  (`git rev-parse origin/main` + `git merge-base --is-ancestor`).
- Working tree after delivery: clean.

## 9. Completed stage / next stage

- **Completed:** Glass UI Appearance Stage 1 (header, per-page text, owner-
  uploaded preview image, versioned Guide link preview, Helper-Bot-only edits).
- **Next:** live Telegram verification (small preview, tap-through, keyboard,
  replacement cache behavior) and, only if requested, reuse on more surfaces.
  Manual schema/bucket setup must be applied before the feature can persist or
  serve an image.
