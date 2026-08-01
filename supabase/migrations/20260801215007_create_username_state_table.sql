/*
# Create username_state table

1. New Tables
- `username_state`
  - `id` (bigserial, primary key)
  - `owner_id` (bigint, unique — one row per bot owner)
  - `template` (text, not null, default '{time} | {mood}')
  - `mood` (text, not null, default '😊')
  - `custom_text` (text, not null, default '')
  - `is_active` (boolean, not null, default false)
  - `last_name` (text, not null, default '')
  - `updated_at` (timestamptz, default now())

2. Security
- Enable RLS on `username_state`.
- SELECT-only policy for anon + authenticated (read-only dashboard access).
- All writes go through the backend service-role key, which bypasses RLS.

3. Important Notes
- This table mirrors `bio_state` exactly in structure.
- The shared Profile Scheduler reads this table each minute and sends
  a single UpdateProfileRequest with `first_name` from this table and
  `about` from `bio_state`.
- Only one row per owner (enforced by UNIQUE constraint on owner_id).
*/
CREATE TABLE IF NOT EXISTS username_state (
    id           bigserial    PRIMARY KEY,
    owner_id     bigint       NOT NULL,
    template     text         NOT NULL DEFAULT '{time} | {mood}',
    mood         text         NOT NULL DEFAULT '😊',
    custom_text  text         NOT NULL DEFAULT '',
    is_active    boolean      NOT NULL DEFAULT false,
    last_name    text         NOT NULL DEFAULT '',
    updated_at   timestamptz  DEFAULT now()
);

ALTER TABLE username_state
    DROP CONSTRAINT IF EXISTS username_state_owner_id_key;
ALTER TABLE username_state
    ADD CONSTRAINT username_state_owner_id_key UNIQUE (owner_id);

CREATE INDEX IF NOT EXISTS idx_username_state_owner
    ON username_state (owner_id);

ALTER TABLE username_state ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "anon_select_username_state" ON username_state;
CREATE POLICY "anon_select_username_state" ON username_state FOR SELECT
    TO anon, authenticated USING (true);
