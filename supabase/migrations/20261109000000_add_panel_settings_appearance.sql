/*
# panel_settings: the Appearance personalization columns

The Appearance feature (Glass UI → Appearance) stores FIVE values on the
existing singleton `panel_settings` row. No new table, no key-value store and
no per-user preference system:

- `appearance_header_title`     text    NOT NULL DEFAULT ''   — global header title
- `appearance_header_body`      text    NOT NULL DEFAULT ''   — global header description
- `appearance_page_overrides`   jsonb   NOT NULL DEFAULT '{}' — page id → {title?, body?}
- `appearance_preview_enabled`  boolean NOT NULL DEFAULT false — image preview switch
- `appearance_preview_image_key` text   NOT NULL DEFAULT ''   — Storage OBJECT KEY only
                                 (never image bytes, never a signed/expiring URL)

`''` and `'{}'` mean "use the built-in default", so resetting a value stores the
empty value rather than a copy of the current customization.

`backend/services/settings_service.py` owns all five keys (`_DEFAULTS`,
validators, typed accessors), `backend/services/panel_settings_repository.py`
writes them as real columns, and `backend/services/appearance_service.py`
composes them at the panel render boundary. On a database built from the
repository's migrations the writes would otherwise be rejected (unknown
column), the values would live only in the process cache, and a restart would
silently lose them. This migration is the missing half of the documented
column-per-setting contract (DATABASE_ARCHITECTURE.md §6).

Additive and idempotent:

1. `ADD COLUMN IF NOT EXISTS` adds each column WITHOUT a default, so an
   existing column keeps every stored value untouched.
2. `SET DEFAULT` binds the final default ('' / '{}' / false).
3. `UPDATE ... WHERE <column> IS NULL` backfills only rows that have no value.
4. `SET NOT NULL` applies the final contract the backfill just satisfied.

Constraints mirror the service's own limits so a rejected value can never be
stored: display text is bounded, page overrides must be a JSON object, and the
image key is either empty or a bounded Storage path (no URL scheme).

Nothing else is created, dropped or rewritten: no index, no policy, no grant,
no other column and no other row. The table already has RLS enabled with a
SELECT-only policy for `anon`/`authenticated`; all writes go through the
backend service-role key, so no security change is needed or made.

## MANUAL SUPABASE ACTION REQUIRED

Applying this to the live Supabase project is a separate manual owner action
(Supabase SQL editor as `postgres`, or the one-paste setup in
DATABASE_ARCHITECTURE.md §31.3).

Rollback (the Appearance values then degrade to the in-memory cache and reset
on every restart — exactly the state this migration fixes; the stored preview
OBJECT itself is not deleted by this rollback):

    ALTER TABLE panel_settings DROP CONSTRAINT IF EXISTS panel_settings_appearance_header_title_check;
    ALTER TABLE panel_settings DROP CONSTRAINT IF EXISTS panel_settings_appearance_header_body_check;
    ALTER TABLE panel_settings DROP CONSTRAINT IF EXISTS panel_settings_appearance_page_overrides_check;
    ALTER TABLE panel_settings DROP CONSTRAINT IF EXISTS panel_settings_appearance_preview_image_key_check;
    ALTER TABLE panel_settings DROP COLUMN IF EXISTS appearance_preview_image_key;
    ALTER TABLE panel_settings DROP COLUMN IF EXISTS appearance_preview_enabled;
    ALTER TABLE panel_settings DROP COLUMN IF EXISTS appearance_page_overrides;
    ALTER TABLE panel_settings DROP COLUMN IF EXISTS appearance_header_body;
    ALTER TABLE panel_settings DROP COLUMN IF EXISTS appearance_header_title;
*/

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

INSERT INTO panel_settings (key)
VALUES ('global')
ON CONFLICT (key) DO NOTHING;

ALTER TABLE panel_settings DROP CONSTRAINT IF EXISTS panel_settings_appearance_header_title_check;
ALTER TABLE panel_settings
    ADD CONSTRAINT panel_settings_appearance_header_title_check
    CHECK (length(appearance_header_title) <= 1024 AND position(chr(0) in appearance_header_title) = 0);

ALTER TABLE panel_settings DROP CONSTRAINT IF EXISTS panel_settings_appearance_header_body_check;
ALTER TABLE panel_settings
    ADD CONSTRAINT panel_settings_appearance_header_body_check
    CHECK (length(appearance_header_body) <= 1024 AND position(chr(0) in appearance_header_body) = 0);

ALTER TABLE panel_settings DROP CONSTRAINT IF EXISTS panel_settings_appearance_page_overrides_check;
ALTER TABLE panel_settings
    ADD CONSTRAINT panel_settings_appearance_page_overrides_check
    CHECK (jsonb_typeof(appearance_page_overrides) = 'object');

ALTER TABLE panel_settings DROP CONSTRAINT IF EXISTS panel_settings_appearance_preview_image_key_check;
ALTER TABLE panel_settings
    ADD CONSTRAINT panel_settings_appearance_preview_image_key_check
    CHECK (appearance_preview_image_key = '' OR appearance_preview_image_key ~ '^[A-Za-z0-9][A-Za-z0-9/_.-]*$');

NOTIFY pgrst, 'reload schema';
