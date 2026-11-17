-- G4-A/F1 live hardening follow-up.
-- Explicit owner authorization was given to apply this after live catalog
-- inspection found inherited service_role defaults broader than the migration's
-- intended least-privilege grants.

-- Remove inherited/default service_role table privileges, then grant the
-- exact Evidence access model. Refs may only UPDATE revision_id.
REVOKE ALL ON TABLE public.ai_evidence_runs FROM service_role;
REVOKE ALL ON TABLE public.ai_evidence_refs FROM service_role;
REVOKE ALL ON TABLE public.ai_evidence_sources FROM service_role;
REVOKE ALL ON TABLE public.ai_evidence_revisions FROM service_role;

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.ai_evidence_runs TO service_role;
GRANT SELECT, INSERT, DELETE ON TABLE public.ai_evidence_refs TO service_role;
GRANT UPDATE (revision_id) ON TABLE public.ai_evidence_refs TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.ai_evidence_sources TO service_role;
GRANT SELECT, INSERT, DELETE ON TABLE public.ai_evidence_revisions TO service_role;

-- Trigger functions are not public APIs. Restrict execution to service_role.
REVOKE ALL ON FUNCTION public.ai_evidence_set_run_expiry() FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.ai_evidence_set_run_expiry() TO service_role;
REVOKE ALL ON FUNCTION public.ai_evidence_ref_revision_guard() FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.ai_evidence_ref_revision_guard() TO service_role;
REVOKE ALL ON FUNCTION public.ai_evidence_revision_immutable() FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.ai_evidence_revision_immutable() TO service_role;

-- Pin function name resolution. public CREATE is denied to client roles.
ALTER FUNCTION public.ai_evidence_now() SET search_path = pg_catalog, public;
ALTER FUNCTION public.ai_evidence_set_run_expiry() SET search_path = pg_catalog, public;
ALTER FUNCTION public.ai_evidence_ref_revision_guard() SET search_path = pg_catalog, public;
ALTER FUNCTION public.ai_evidence_revision_immutable() SET search_path = pg_catalog, public;
ALTER FUNCTION public.ai_evidence_archive_usage(bigint) SET search_path = pg_catalog, public;
ALTER FUNCTION public.ai_evidence_create_run(uuid, bigint, bigint, text, smallint, bigint, bigint, smallint, integer, text, integer) SET search_path = pg_catalog, public;
ALTER FUNCTION public.ai_evidence_archive_revision(uuid, uuid, bigint, bigint, bigint, text, text, text, integer, bigint) SET search_path = pg_catalog, public;
