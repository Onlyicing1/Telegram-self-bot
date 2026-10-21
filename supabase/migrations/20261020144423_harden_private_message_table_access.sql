/*
# Private Message Management: service-role-only access to the two tables

`20260929000001_create_private_message_tables.sql` gave the two Private Message
Management tables the generic read-only dashboard boundary: `GRANT SELECT … TO
anon, authenticated` plus a permissive `USING (true)` SELECT policy on each
table. That boundary is right for the dashboard's own data, but both of these
tables hold server-only material:

    private_message_config    the owner's configuration: the master switch, the
                              mode, the allow-list, the exception list, the
                              temporary override, the weekly schedule and the
                              fixed response text
    private_message_senders   the owner's per-person AI-reply counters, the last
                              reply instant and the reset-window anchor

Any client holding the project's anon key could read all of it. This migration
closes that path. It is forward-only and additive: it changes NO table, NO
column, NO index, NO row and NO stored configuration, and it touches no
unrelated policy or grant.

## What it does (and nothing else)

1. RLS stays ENABLED on both tables (re-asserted, so a database where it was
   turned off converges too).
2. Every permissive policy the table-creation migration created is dropped BY
   NAME, idempotently — the two SELECT policies that actually existed and the
   three write policies that migration already dropped defensively
   (`anon_insert_*`, `anon_update_*`, `anon_delete_*`). Dropping them by name
   also removes them from a database that applied an earlier revision of the
   setup block.
3. Every privilege is REVOKED from `PUBLIC`, `anon` and `authenticated` — table
   level AND column level (a table `REVOKE` alone would leave a legacy
   per-column grant intact).
4. `service_role` receives exactly the four verbs the repository uses, on both
   tables: SELECT (`get_private_message_config`,
   `list_private_message_senders`), INSERT (`save_private_message_config`,
   the claim's counter insert), UPDATE (`save_private_message_config`, the
   claim CAS, the compensating release CAS) and DELETE
   (`reset_private_message_counters`). Nothing else is granted.
5. NO policy is created. With RLS enabled and zero policies, every public
   client role is denied at the table by construction; `service_role` bypasses
   RLS, which is why it needs no policy.
6. `NOTIFY pgrst, 'reload schema'`, so PostgREST drops the read path it cached.
7. Read-only verification queries at the end report the posture that was just
   established (zero client grants, zero policies, RLS on, the four
   service-role grants present, every column privilege cleared).

## Why the backend is unaffected

`backend/db/client.py` builds ONE Supabase client from
`SUPABASE_SERVICE_ROLE_KEY`, and every Private Message repository function
(`get_private_message_config`, `save_private_message_config`,
`list_private_message_senders`, `claim_private_message_ai_reply`,
`release_private_message_ai_reply`, `reset_private_message_counters`) uses it.
The service role bypasses RLS and keeps the four granted verbs, so no runtime
change is required and none is made here. The AI has no tool that reaches
either table, so no tool loses a capability.

## Applied to a database that has not run the table-creation migration

Both tables must exist for the grants and policies to be addressed, so this
migration is meant to run after `20260929000001` — it is the NEXT part of the
canonical setup block (§31.3), directly after the counter-reset part. On a
database where the tables do not exist the `ALTER`/`REVOKE`/`GRANT` statements
fail with "relation does not exist"; nothing is destroyed and nothing is
partially changed by that failure.

Idempotent: safe to run more than once. Applying it to the live Supabase project
is a separate manual owner action, and the coding agent never executes SQL.

## MANUAL SUPABASE ACTION REQUIRED

Apply this file (it is part 13 of the ONE complete setup script in
DATABASE_ARCHITECTURE.md §31.3, so it is normally applied by running that block
once). Until it is applied, the live posture is UNCHANGED and the exposure is
still open: the repository's SQL is the intended posture, not the observed one.

Reversal (restores the pre-hardening read boundary — it deliberately brings the
public read path BACK, so it is a security regression and not a repair):

    GRANT SELECT ON private_message_config TO anon, authenticated;
    GRANT SELECT ON private_message_senders TO anon, authenticated;
    CREATE POLICY "anon_select_private_message_config" ON private_message_config FOR SELECT
        TO anon, authenticated USING (true);
    CREATE POLICY "anon_select_private_message_senders" ON private_message_senders FOR SELECT
        TO anon, authenticated USING (true);

Nothing else in the schema changes: no table, column, index, row, constraint or
stored value is touched.
*/

ALTER TABLE private_message_config ENABLE ROW LEVEL SECURITY;
ALTER TABLE private_message_senders ENABLE ROW LEVEL SECURITY;

-- Service-role only: no client role may read the owner's configuration, the
-- allow/exception lists, the schedule or a person's counters. Drop every
-- permissive policy the table-creation migration created, by name, so a re-run
-- on a database that still carries them ends in the restrictive posture.
DROP POLICY IF EXISTS "anon_insert_private_message_config" ON private_message_config;
DROP POLICY IF EXISTS "anon_update_private_message_config" ON private_message_config;
DROP POLICY IF EXISTS "anon_delete_private_message_config" ON private_message_config;
DROP POLICY IF EXISTS "anon_select_private_message_config" ON private_message_config;

DROP POLICY IF EXISTS "anon_insert_private_message_senders" ON private_message_senders;
DROP POLICY IF EXISTS "anon_update_private_message_senders" ON private_message_senders;
DROP POLICY IF EXISTS "anon_delete_private_message_senders" ON private_message_senders;
DROP POLICY IF EXISTS "anon_select_private_message_senders" ON private_message_senders;

REVOKE ALL ON TABLE private_message_config FROM PUBLIC, anon, authenticated;
REVOKE ALL ON TABLE private_message_senders FROM PUBLIC, anon, authenticated;

-- Column privileges are granted independently of table privileges, so a table
-- REVOKE cannot clear a legacy grant that was made per column.
DO $$
DECLARE
    tbl  text;
    cols text;
BEGIN
    FOREACH tbl IN ARRAY ARRAY['private_message_config', 'private_message_senders'] LOOP
        SELECT string_agg(quote_ident(column_name), ', ' ORDER BY ordinal_position)
          INTO cols
          FROM information_schema.columns
         WHERE table_schema = 'public'
           AND table_name   = tbl;
        IF cols IS NOT NULL THEN
            EXECUTE format(
                'REVOKE ALL (%s) ON TABLE public.%I FROM PUBLIC, anon, authenticated',
                cols, tbl);
        END IF;
    END LOOP;
END $$;

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE private_message_config TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE private_message_senders TO service_role;

COMMENT ON TABLE private_message_config IS
    'ONE Private Message Management configuration row per owner. Service-role only: RLS is enabled with zero policies and every privilege is revoked from PUBLIC/anon/authenticated, so no client role can read the allow-list, exceptions, schedule or fixed response. The scope (genuine human users in direct private chats only) is NOT configurable: the delete, fixed-response and AI-response actions each have their own independent enable flag, the feature defaults to OFF, and ai_interval_seconds = 0 is a supported no-delay value.';
COMMENT ON TABLE private_message_senders IS
    'Durable per-person AI-reply counters for Private Message Management. Service-role only: RLS is enabled with zero policies and every privilege is revoked from PUBLIC/anon/authenticated. Only an event that passed the human-private scope gate is ever counted. Counters are never reset implicitly: the owner''s explicit reset action is the only edge, plus the optional automatic rolling window when ai_counter_reset_enabled is TRUE.';

NOTIFY pgrst, 'reload schema';

SELECT 'private_message client grants' AS check, count(*) AS present
FROM information_schema.role_table_grants
WHERE table_schema = 'public'
  AND table_name   IN ('private_message_config', 'private_message_senders')
  AND grantee      IN ('PUBLIC', 'anon', 'authenticated');

SELECT 'private_message client column grants' AS check, count(*) AS present
FROM information_schema.column_privileges
WHERE table_schema = 'public'
  AND table_name   IN ('private_message_config', 'private_message_senders')
  AND grantee      IN ('PUBLIC', 'anon', 'authenticated');

SELECT 'private_message policies' AS check, count(*) AS present
FROM pg_policies
WHERE schemaname = 'public'
  AND tablename  IN ('private_message_config', 'private_message_senders');

SELECT 'private_message rls enabled' AS check, count(*) AS present
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
  AND c.relname IN ('private_message_config', 'private_message_senders')
  AND c.relrowsecurity;

SELECT 'private_message service_role grants' AS check, count(*) AS present
FROM information_schema.role_table_grants
WHERE table_schema = 'public'
  AND table_name   IN ('private_message_config', 'private_message_senders')
  AND grantee      = 'service_role';
