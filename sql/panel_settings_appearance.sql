/*
# Glass UI Appearance columns — MANUAL SETUP
#
# File: sql/panel_settings_appearance.sql
# This is an owner-run setup script, deliberately NOT an entry in the audited
# supabase/migrations/ history: the repository's canonical one-paste setup
# (§31.3) and its 42-file inventory stay unchanged. Apply it after that setup.
#
# Glass UI Appearance columns

Three owner-controlled surfaces on the existing singleton `panel_settings` row:

- `appearance_header_title` / `appearance_header_body`
  Global header overrides. Empty string = no override (each page keeps its
  own default title/description).
- `appearance_page_texts` (jsonb)
  Per-main-page overrides, shape:
      {"save": {"title": "...", "body": "..."}, "ai": {"title": "..."}}
  Keys are validated in the application against the actual main-page registry
  (only top-level menu categories are eligible). Image bytes never live here.
- `appearance_preview_enabled` / `appearance_preview_image` /
  `appearance_preview_meta`
  Independent preview switch, the durable Supabase Storage OBJECT KEY
  (`previews/<32 hex>.<png|jpg|webp>`), and the probed image metadata
  (`{"mime": "...", "width": N, "height": N}`). The key is a reference — no
  image bytes and no expiring signed URL are stored.

Reference: backend/services/appearance_service.py (validators + accessors),
backend/services/panel_settings_repository.py (the ONLY table accessor),
backend/services/preview_storage.py (object storage).

Additive and idempotent: ADD COLUMN IF NOT EXISTS without a default first,
then SET DEFAULT, backfill only NULL rows, then SET NOT NULL. Existing stored
values are never rewritten. RLS is unchanged: the table is already
SELECT-only for anon/authenticated and all writes use the backend service-role
key.

## MANUAL SUPABASE ACTION REQUIRED

Applying this script and creating the storage bucket are owner actions.
This repository never executes SQL and never modifies a live project.

Storage bucket (Supabase Dashboard → Storage, or SQL editor):

    insert into storage.buckets (id, name, public)
    values ('panel-previews', 'panel-previews', false)
    on conflict (id) do nothing;

The bucket can stay PRIVATE: the backend reads the object with the service-role
key and serves it through `/preview/<version>/<panel>/image`. No anon policy
is required or added.

Environment:

    PANEL_PREVIEW_BUCKET=panel-previews
    PUBLIC_BASE_URL=https://<your-public-service-origin>

Rollback (the Appearance surface then reports honest storage/write failures):

    ALTER TABLE panel_settings DROP COLUMN IF EXISTS appearance_preview_meta;
    ALTER TABLE panel_settings DROP COLUMN IF EXISTS appearance_preview_image;
    ALTER TABLE panel_settings DROP COLUMN IF EXISTS appearance_preview_enabled;
    ALTER TABLE panel_settings DROP COLUMN IF EXISTS appearance_page_texts;
    ALTER TABLE panel_settings DROP COLUMN IF EXISTS appearance_header_body;
    ALTER TABLE panel_settings DROP COLUMN IF EXISTS appearance_header_title;

    -- Optional: delete the preview objects first, then the bucket.
    -- delete from storage.objects where bucket_id = 'panel-previews';
    -- delete from storage.buckets where id = 'panel-previews';
*/

ALTER TABLE panel_settings ADD COLUMN IF NOT EXISTS appearance_header_title text;
ALTER TABLE panel_settings ALTER COLUMN appearance_header_title SET DEFAULT '';
UPDATE panel_settings SET appearance_header_title = '' WHERE appearance_header_title IS NULL;
ALTER TABLE panel_settings ALTER COLUMN appearance_header_title SET NOT NULL;

ALTER TABLE panel_settings ADD COLUMN IF NOT EXISTS appearance_header_body text;
ALTER TABLE panel_settings ALTER COLUMN appearance_header_body SET DEFAULT '';
UPDATE panel_settings SET appearance_header_body = '' WHERE appearance_header_body IS NULL;
ALTER TABLE panel_settings ALTER COLUMN appearance_header_body SET NOT NULL;

ALTER TABLE panel_settings ADD COLUMN IF NOT EXISTS appearance_page_texts jsonb;
ALTER TABLE panel_settings ALTER COLUMN appearance_page_texts SET DEFAULT '{}'::jsonb;
UPDATE panel_settings SET appearance_page_texts = '{}'::jsonb WHERE appearance_page_texts IS NULL;
ALTER TABLE panel_settings ALTER COLUMN appearance_page_texts SET NOT NULL;

ALTER TABLE panel_settings ADD COLUMN IF NOT EXISTS appearance_preview_enabled boolean;
ALTER TABLE panel_settings ALTER COLUMN appearance_preview_enabled SET DEFAULT false;
UPDATE panel_settings SET appearance_preview_enabled = false WHERE appearance_preview_enabled IS NULL;
ALTER TABLE panel_settings ALTER COLUMN appearance_preview_enabled SET NOT NULL;

ALTER TABLE panel_settings ADD COLUMN IF NOT EXISTS appearance_preview_image text;
ALTER TABLE panel_settings ALTER COLUMN appearance_preview_image SET DEFAULT '';
UPDATE panel_settings SET appearance_preview_image = '' WHERE appearance_preview_image IS NULL;
ALTER TABLE panel_settings ALTER COLUMN appearance_preview_image SET NOT NULL;

ALTER TABLE panel_settings ADD COLUMN IF NOT EXISTS appearance_preview_meta jsonb;
ALTER TABLE panel_settings ALTER COLUMN appearance_preview_meta SET DEFAULT '{}'::jsonb;
UPDATE panel_settings SET appearance_preview_meta = '{}'::jsonb WHERE appearance_preview_meta IS NULL;
ALTER TABLE panel_settings ALTER COLUMN appearance_preview_meta SET NOT NULL;

-- Bounded, fail-closed data guards (application validators remain authoritative).
ALTER TABLE panel_settings DROP CONSTRAINT IF EXISTS panel_settings_appearance_header_title_check;
ALTER TABLE panel_settings
    ADD CONSTRAINT panel_settings_appearance_header_title_check
    CHECK (char_length(appearance_header_title) <= 64);

ALTER TABLE panel_settings DROP CONSTRAINT IF EXISTS panel_settings_appearance_header_body_check;
ALTER TABLE panel_settings
    ADD CONSTRAINT panel_settings_appearance_header_body_check
    CHECK (char_length(appearance_header_body) <= 500);

ALTER TABLE panel_settings DROP CONSTRAINT IF EXISTS panel_settings_appearance_page_texts_check;
ALTER TABLE panel_settings
    ADD CONSTRAINT panel_settings_appearance_page_texts_check
    CHECK (jsonb_typeof(appearance_page_texts) = 'object');

ALTER TABLE panel_settings DROP CONSTRAINT IF EXISTS panel_settings_appearance_preview_image_check;
ALTER TABLE panel_settings
    ADD CONSTRAINT panel_settings_appearance_preview_image_check
    CHECK (appearance_preview_image = '' OR appearance_preview_image ~ '^previews/[0-9a-f]{32}\.(png|jpg|webp)$');

ALTER TABLE panel_settings DROP CONSTRAINT IF EXISTS panel_settings_appearance_preview_meta_check;
ALTER TABLE panel_settings
    ADD CONSTRAINT panel_settings_appearance_preview_meta_check
    CHECK (jsonb_typeof(appearance_preview_meta) = 'object');

NOTIFY pgrst, 'reload schema';

SELECT 'panel_settings appearance columns' AS check, count(*) AS present
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name   = 'panel_settings'
  AND column_name IN (
      'appearance_header_title', 'appearance_header_body',
      'appearance_page_texts', 'appearance_preview_enabled',
      'appearance_preview_image', 'appearance_preview_meta'
  );
