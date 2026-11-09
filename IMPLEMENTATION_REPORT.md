# Implementation Report — Glass UI Appearance Personalization (2026-11-09)

**Scope.** The Appearance category of the Glass UI: one new top-level menu entry,
three sub-panels (Header, Page Text, Preview Image), five `panel_settings`
columns, one additive migration, the preview web routes, and the tests/docs that
pin them. No other panel, callback family, client, listener or scheduler was
changed.

**Stage.** Stage 1 of the Appearance feature (owner-uploaded image, no gallery,
no web upload UI). **Status:** implemented and tested in this tree, committed and
pushed to `origin/main`; the database migration and the Storage bucket are
**not** applied to the owner's live project (manual action, §8).

## 1. Implemented — architecture

Composition happens at the smallest boundary that covers every eligible render:

```
settings_service (5 keys + strict setter)
   ↓
appearance_service   compose_text / with_preview / apply_to_inline_results
   ↓                                  ↓
panels._handle_panel            inline_engine._inline_router
(callback navigation)           (the first inline result)
   ↓                                  ↓
ExistingPanelEditor(inline_id, helper) — Helper Bot only
```

* `backend/services/appearance_service.py` (new) owns page identity
  (`MAIN_PAGE_IDS`), canonical defaults (`PAGE_DEFAULTS`), validation, text
  composition, the preview URL/meta/HTML, image sniffing/limits and the Storage
  upload/download/remove calls.
* `backend/bot/handlers/appearance.py` (new) owns the fives panels, four
  actions and five pending-input flows. It never builds a client, never
  registers a listener and never edits through the self-client.
* `settings_service` gained the five keys, their validators and typed
  accessors, plus `set_setting_strict` (a rejected DB write is reported, never
  silently cache-only, when a database is configured).
* `panels._handle_panel` applies `appearance_service.with_preview(...)` and
  threads `link_preview` through `_safe_edit`/`_present`;
  `inline_engine._inline_router` applies `apply_to_inline_results(...)`.
* `backend/web/app.py` serves `GET /appearance/preview/{version}` (HTML with
  Open Graph tags) and `GET /appearance/image/{version}` (bytes, immutable
  cache), both declared before the SPA fallback.
* `backend/bot/handlers/misc.py` adds exactly one menu row;
  `backend/bot/router.py` calls `appearance.register`.

## 2. Appearance menu and Unicode compliance

* `Menu` → **Appearance** (`panel:appearance`) → **Header Customization**
  (`panel:appearance:header`), **Page Text** (`panel:appearance:pages` → one
  page each), **Preview Image** (`panel:appearance:preview`). Every sub-panel
  uses the shared Back / Home / Close navigation.
* The main menu gained exactly this one entry (10 rows, was 9).
* No new symbol was introduced. Labels are composed with the existing registry
  (`OP_EDIT`, `PANEL_HEADING`, `MEDIA_PHOTO`, `OP_SAVE`, `OP_STOP`,
  `OP_REFRESH`, `OP_DELETE`, `OP_CREATE`, `STATE_COMPLETED`, `STATE_WARNING`,
  `STATE_FAILED`, `SEPARATOR`) via `sym.label(...)`; no emoji, no `U+FE0F`, and
  no font transformation is ever applied to callback data, page ids or URLs.

## 3. Global header and per-page customization

* `appearance_header_title` / `appearance_header_body` are shared static
  values. `''` means "built-in default", so resetting stores the empty value,
  never a copy of the current customization.
* When a header title is set it becomes the panel title, with the page's own
  title kept as one bold line beneath it (no duplicate title); the header body
  sits above the page body / live content.
* Eleven eligible pages (`menu, save, del, profile, db, general, settings,
  cipher, todo, ai, appearance`) come from `MAIN_PAGE_IDS`; a test asserts that
  set equals the menu's own `panel:*` destinations + `menu`, so it cannot drift.
* Per-page `{title?, body?}` overrides live in `appearance_page_overrides`
  (jsonb). An absent field keeps the page's default. A body override replaces a
  body that is still its untouched default and is otherwise **prepended** above
  live/dynamic content, so counters, provider state and lists stay live.
* Composition runs only for `extra == ""` (the page's home view): action
  results, confirmations, pickers, pagination and input prompts are untouched.
* Reset is per field and per page, restoring the canonical default from
  `PAGE_DEFAULTS`. Bounds: title 120, body 800 characters; blank/NUL and
  oversized input is rejected without corrupting the previous value.

## 4. Image upload, storage and toggle

* Preview switch (`appearance_preview_enabled`) and storage
  (`appearance_preview_image_key`) are independent:

  | enabled | stored | behaviour |
  |---|---|---|
  | OFF | yes | text-only, key retained |
  | OFF | no | text-only |
  | ON | yes | web-page preview |
  | ON | no | asks to upload or skip; the panel stays functional |

* Upload reuses the ONE pending-input listener (`input:appearance:image`); the
  self-client only receives the owner's media, the Helper Bot performs the
  panel edit (`ExistingPanelEditor(inline_id, helper)`, `self_client=None`).
  If the Helper Bot cannot edit, the failure is reported — there is no
  self-client fallback and no second panel message.
* Validation is content-based: PNG/JPEG/WebP magic bytes + dimension parsing,
  ≤ 5 MB, ≤ 4096 px per side, malformed/unsupported media rejected with the
  previous setting intact. The bytes are streamed from a bounded read, never
  written to disk, so there is no temp file to leak.
* The image is uploaded to Supabase Storage (`APPEARANCE_MEDIA_BUCKET`,
  falling back to `ARCHIVE_MEDIA_BUCKET`) under a versioned key. Only that
  stable object key is persisted — never bytes, never a signed URL. Replace
  uploads first and removes the old object afterwards; Remove deletes the
  object and clears the key; disabling preview deletes nothing.

## 5. Telegram preview — implementation and verification status

* Enabled preview appends the public HTTPS URL
  `$PUBLIC_BASE_URL/appearance/preview/<version>` (fallback
  `RENDER_EXTERNAL_URL`) to the composed body with `no_webpage=False`, so
  Telegram renders its own web-page preview above the text; the inline keyboard
  is untouched and no second message is created.
* `/appearance/preview/<version>` returns HTML carrying `og:title`,
  `og:description`, `og:image`, `og:image:width/height`, `og:image:type` and
  `og:url`; the tap target is the existing Guide page. `/appearance/image/<version>`
  serves the stored object with an immutable cache header. The version is part
  of the key, so a replacement changes both URLs and Telegram's preview cache
  cannot keep showing the old image.
* Disabled preview produces a genuinely text-only panel: no URL is appended at
  all, even when an image is still stored (this was a real bug the tests caught).
* **Verified:** the built HTML/meta, the URL/version derivation, the
  text-only/URL-present split, the keyboard preservation and the edit path are
  covered by tests. **Not verified:** how Telegram actually renders the preview
  card (size, crop, cache), because no live Telegram client is available here;
  the small-preview request cannot be forced through this inline edit path and
  is documented as a limitation, not claimed.

## 6. Helper-Bot-only editing

* Every repaint in the new flow goes through `_paint_input(inline_id, view)`
  → `ExistingPanelEditor(inline_id=…, helper=…)` with **no** `self_client`
  argument, so a refused inline edit surfaces as a failure instead of being
  retried by the account.
* The migration handler path uses the same editor; the account never calls
  `edit_message` for this panel and never sends a replacement panel.
* Tests assert the editor is constructed with `self_client=None`, that no
  self-client edit occurs, and that no new listener/client/loop is registered.

## 7. Files changed

New:

* `backend/services/appearance_service.py`
* `backend/bot/handlers/appearance.py`
* `supabase/migrations/20261109000000_add_panel_settings_appearance.sql`
* `tests/test_appearance_ui.py` (42 tests)

Modified:

* `backend/services/settings_service.py` (5 keys, validators, strict setter)
* `backend/helper/panels.py` (composition + `link_preview` plumbing, shim-safe)
* `backend/helper/inline_engine.py` (first-render composition)
* `backend/helper/inline_sender.py` (`event` kwarg for builders that accept it)
* `backend/bot/handlers/misc.py` (menu entry)
* `backend/bot/router.py` (registration)
* `backend/web/app.py` (preview + image routes)
* `DATABASE_ARCHITECTURE.md` (§6 columns, §30.11 list, §31.1 audit row + counts,
  §31.2 prose, §31.3 part 15 of 15)
* `tests/test_database_setup_order.py`, `tests/test_private_message_db_security.py`
  (migration/part counts: 36 → 37, 14 → 15, "thirteen" → "fourteen")
* `AGENTS.md`, `INVESTIGATION.md`, `IMPLEMENTATION_REPORT.md`, `ROADMAP.md`

## 8. Exact database / storage setup and rollback (NOT applied)

Nothing below was executed; the live Supabase project was not contacted.

**Required:** the five columns do not exist on the owner's database until the
new part is applied. Until then the repository rejects the writes and the
customization survives only in the process cache (it is lost on restart).

Apply, as `postgres` in the Supabase SQL editor — the single §31.3 block of
`DATABASE_ARCHITECTURE.md` (now parts 1–15) **or** just this migration:

```sql
-- supabase/migrations/20261109000000_add_panel_settings_appearance.sql
ALTER TABLE panel_settings ADD COLUMN IF NOT EXISTS appearance_header_title text;
ALTER TABLE panel_settings ALTER COLUMN appearance_header_title SET DEFAULT '';
UPDATE panel_settings SET appearance_header_title = '' WHERE appearance_header_title IS NULL;
ALTER TABLE panel_settings ALTER COLUMN appearance_header_title SET NOT NULL;

ALTER TABLE panel_settings ADD COLUMN IF NOT EXISTS appearance_header_body text;
ALTER TABLE panel_settings ALTER COLUMN appearance_header_body SET DEFAULT '';
UPDATE panel_settings SET appearance_header_body = '' WHERE appearance_header_body IS NULL;
ALTER TABLE panel_settings ALTER COLUMN appearance_header_body SET NOT NULL;

ALTER TABLE panel_settings ADD COLUMN IF NOT EXISTS appearance_page_overrides jsonb;
ALTER TABLE panel_settings ALTER COLUMN appearance_page_overrides SET DEFAULT '{}'::jsonb;
UPDATE panel_settings SET appearance_page_overrides = '{}'::jsonb WHERE appearance_page_overrides IS NULL;
ALTER TABLE panel_settings ALTER COLUMN appearance_page_overrides SET NOT NULL;

ALTER TABLE panel_settings ADD COLUMN IF NOT EXISTS appearance_preview_enabled boolean;
ALTER TABLE panel_settings ALTER COLUMN appearance_preview_enabled SET DEFAULT false;
UPDATE panel_settings SET appearance_preview_enabled = false WHERE appearance_preview_enabled IS NULL;
ALTER TABLE panel_settings ALTER COLUMN appearance_preview_enabled SET NOT NULL;

ALTER TABLE panel_settings ADD COLUMN IF NOT EXISTS appearance_preview_image_key text;
ALTER TABLE panel_settings ALTER COLUMN appearance_preview_image_key SET DEFAULT '';
UPDATE panel_settings SET appearance_preview_image_key = '' WHERE appearance_preview_image_key IS NULL;
ALTER TABLE panel_settings ALTER COLUMN appearance_preview_image_key SET NOT NULL;

INSERT INTO panel_settings (key) VALUES ('global') ON CONFLICT (key) DO NOTHING;

ALTER TABLE panel_settings DROP CONSTRAINT IF EXISTS panel_settings_appearance_header_title_check;
ALTER TABLE panel_settings ADD CONSTRAINT panel_settings_appearance_header_title_check
    CHECK (length(appearance_header_title) <= 1024 AND position(chr(0) in appearance_header_title) = 0);
ALTER TABLE panel_settings DROP CONSTRAINT IF EXISTS panel_settings_appearance_header_body_check;
ALTER TABLE panel_settings ADD CONSTRAINT panel_settings_appearance_header_body_check
    CHECK (length(appearance_header_body) <= 1024 AND position(chr(0) in appearance_header_body) = 0);
ALTER TABLE panel_settings DROP CONSTRAINT IF EXISTS panel_settings_appearance_page_overrides_check;
ALTER TABLE panel_settings ADD CONSTRAINT panel_settings_appearance_page_overrides_check
    CHECK (jsonb_typeof(appearance_page_overrides) = 'object');
ALTER TABLE panel_settings DROP CONSTRAINT IF EXISTS panel_settings_appearance_preview_image_key_check;
ALTER TABLE panel_settings ADD CONSTRAINT panel_settings_appearance_preview_image_key_check
    CHECK (appearance_preview_image_key = '' OR appearance_preview_image_key ~ '^[A-Za-z0-9][A-Za-z0-9/_.-]*$');

NOTIFY pgrst, 'reload schema';
```

**Storage bucket (required for upload):**

* Create a **private** bucket, name it in `APPEARANCE_MEDIA_BUCKET` (or let it
  fall back to the existing `ARCHIVE_MEDIA_BUCKET`). If neither is set, upload
  fails with an explicit "durable image storage is not configured" message and
  everything else keeps working.
* Allowed MIME types: `image/png`, `image/jpeg`, `image/webp`.
* Max size 5 MB per object (the app also enforces ≤ 4096 px per side before
  upload).
* No public policy is needed: only the backend service-role key writes, reads
  and deletes, and the image is served to Telegram by
  `/appearance/image/<version>`. Do **not** make the bucket public.
* Public HTTPS base URL: set `PUBLIC_BASE_URL` (https) — or rely on Render's
  `RENDER_EXTERNAL_URL` — so the preview page is reachable by Telegram.

**Rollback** (Appearance values then degrade to the process cache and reset on
restart; the stored preview OBJECT is not deleted by this rollback):

```sql
ALTER TABLE panel_settings DROP CONSTRAINT IF EXISTS panel_settings_appearance_header_title_check;
ALTER TABLE panel_settings DROP CONSTRAINT IF EXISTS panel_settings_appearance_header_body_check;
ALTER TABLE panel_settings DROP CONSTRAINT IF EXISTS panel_settings_appearance_page_overrides_check;
ALTER TABLE panel_settings DROP CONSTRAINT IF EXISTS panel_settings_appearance_preview_image_key_check;
ALTER TABLE panel_settings DROP COLUMN IF EXISTS appearance_preview_image_key;
ALTER TABLE panel_settings DROP COLUMN IF EXISTS appearance_preview_enabled;
ALTER TABLE panel_settings DROP COLUMN IF EXISTS appearance_page_overrides;
ALTER TABLE panel_settings DROP COLUMN IF EXISTS appearance_header_body;
ALTER TABLE panel_settings DROP COLUMN IF EXISTS appearance_header_title;
```

## 9. Tests run and exact results

Focused suites (Appearance + symbols + Taskloom input/editor UX + emoji stage 1
+ coloured buttons + guide):

```
226 passed
```

Appearance-specific file (`tests/test_appearance_ui.py`, 42 tests) and the two
shim regressions that first failed on `link_preview`:

```
44 passed
```

Documentation audits after the migration was added as part 15
(`test_database_setup_order`, `test_private_message_db_security`,
`test_canonical_schema_reconciliation`):

```
103 passed
```

Full suite, compilation and diff hygiene are recorded in §11 with the delivery
state; the numbers above are the exact pytest summaries from this tree.

## 10. Tested and passed / not tested

**Tested and passed** — menu registration and single-entry rule; eligibility
equals the menu's destinations; header title/body edit, independent reset and
full reset; bounds rejection (blank, NUL, oversized) with the old value intact;
per-page title-only / body-only / both overrides; untouched pages keep defaults;
dynamic bodies keep their live content under a prepended override; sub-views are
never composed; reset restores the real default; callback data unchanged;
strict-write honesty; reload/restart persistence through the settings service;
image accept/reject (magic bytes, dimensions, size), replace, remove, OFF keeps
the key, ON-without-image asks, failed upload leaves settings intact, temp-file
avoidance; preview URL/version/OG metadata; text-only when disabled; keyboard
preserved; edit-not-replace; Helper-Bot-only editor; no new listener/client/loop.

**Not tested** — live Telegram rendering of the preview card (no client here);
live Supabase Storage round-trip (no credentials here); the font pipeline's
rendering of the new labels on a real device.

**Requires manual owner action** — run the migration (§8); create/point the
bucket and set `APPEARANCE_MEDIA_BUCKET`/`PUBLIC_BASE_URL`.

**Deferred** — a curated gallery or generated default image; web-dashboard
upload; per-user (multi-owner) appearances; forcing `force_small_media` on the
inline edit path; live verification of Telegram's preview layout.

## 11. Delivery

One focused commit on `freebuff/dcc722156bb0a1a080890e10`, pushed to
`origin/main`; the pushed SHA and the verified remote state are reported in the
final response for this task (and, if the push is refused, the exact remaining
step is reported instead of a claim).
