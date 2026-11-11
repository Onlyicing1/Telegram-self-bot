-- Least-privilege hardening for the private self-bot database.
--
-- The application accesses Supabase only through backend/db/client.py using
-- SUPABASE_SERVICE_ROLE_KEY. The React dashboard reads through the backend API;
-- it does not use the Supabase anon key. Keep the existing SELECT-only policy
-- contract for compatibility, but remove client-side write/DDL-adjacent table
-- privileges and redundant read policies.
--
-- This migration deliberately does NOT revoke SELECT or remove the canonical
-- anon_select_* policies. Changing that public-read contract requires a
-- separate, explicitly reviewed migration.

BEGIN;

-- The current RLS policies are SELECT-only, but the live grants also include
-- INSERT/UPDATE/DELETE/TRUNCATE/REFERENCES/TRIGGER. RLS is not a substitute
-- for least-privilege SQL grants.
REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
    ON ALL TABLES IN SCHEMA public
    FROM anon, authenticated;

-- Prevent the broad table/sequence grants observed in the live default ACLs
-- from being reintroduced by future objects created by the postgres migration owner.
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
    REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
    ON TABLES FROM anon, authenticated;

REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public
    FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
    REVOKE ALL ON SEQUENCES FROM anon, authenticated;

-- Keep one canonical anon_select_* policy per table. The other live policies
-- are legacy duplicates with the same USING (true) expression, or the
-- non-canonical ai_preferences_select policy.
DO $$
DECLARE
    policy_row record;
BEGIN
    FOR policy_row IN
        SELECT schemaname, tablename, policyname
          FROM pg_policies
         WHERE schemaname = 'public'
           AND policyname NOT LIKE 'anon_select_%'
    LOOP
        EXECUTE format(
            'DROP POLICY IF EXISTS %I ON %I.%I',
            policy_row.policyname,
            policy_row.schemaname,
            policy_row.tablename
        );
    END LOOP;
END
$$;

-- Trigger functions only use pg_catalog built-ins and NEW. Pinning the search
-- path prevents object-shadowing while preserving their trigger behavior.
ALTER FUNCTION public.panel_settings_updated_at()
    SET search_path = pg_catalog;
ALTER FUNCTION public.set_updated_at()
    SET search_path = pg_catalog;
ALTER FUNCTION public.update_ghost_chats_updated_at()
    SET search_path = pg_catalog;

-- This function is invoked by the registered DDL event trigger, not by API
-- callers. Revoke its unnecessary direct execution grant; the event trigger
-- continues to execute it as its owner.
REVOKE EXECUTE ON FUNCTION public.rls_auto_enable()
    FROM PUBLIC, anon, authenticated;

NOTIFY pgrst, 'reload schema';

COMMIT;
