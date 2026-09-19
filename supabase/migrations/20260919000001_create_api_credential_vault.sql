/*
# API Credential Vault — PART 1 (credential metadata + Vault-backed resolution)

Creates the ONE secret-storage architecture the whole application can share:
a metadata table for credential BOOKKEEPING and one SECURITY DEFINER function
that resolves a provider's credentials by decrypting them from Supabase Vault.

    ENV credentials  ─┐
                      ├─→ backend/ai/credential_source.py ─→ bounded credential pool
    Vault credentials ┘        (this migration's api_credential_pool RPC)

WHAT THIS MIGRATION IS NOT

* It is NOT a plaintext key store. `api_credentials` has **no column that can
  hold a secret** — the only secret-bearing reference is `vault_secret_id`, a
  foreign key into `vault.secrets(id)`. The raw key never leaves Vault except
  inside the SECURITY DEFINER function below, which hands it straight to the
  backend's service-role client.
* It is NOT STT-specific. The table and the RPC are keyed by a free-form
  `provider` token, so a future Text-to-Speech provider (the M3.0 OpenAI
  adapter, for example) or any other AI/media provider reuses this same table
  and this same function instead of a second secret architecture.
* It is NOT a generic SQL execution endpoint: one function, one parameter
  (plus an optional owner filter), a fixed return shape and a fixed row ceiling.

Objects created (all in `public`, all idempotent):

| Object | Type | Purpose |
|---|---|---|
| `api_credentials` | table | credential METADATA only (provider, label, enabled, priority, owner, Vault mapping) |
| `idx_api_credentials_provider_order` | index | backs the deterministic single-provider read |
| `idx_api_credentials_owner` | index | backs the owner-scoped read |
| `uq_api_credentials_vault_secret` | unique index | one Vault secret maps to exactly ONE credential (a duplicate mapping would make rotation a no-op) |
| `api_credential_pool(text, bigint)` | function | the ONE resolution boundary: provider (+ optional owner) → ordered, ENABLED, decrypted credentials |
| `stt_credential_pool(text, bigint)` | function | deprecated compatibility alias of the same contract, kept for the M2.4 documented name |

SECURITY MODEL

* `anon` / `authenticated` get NOTHING: the table is REVOKEd and has RLS enabled
  with NO policy, and the functions are REVOKEd from PUBLIC/anon/authenticated and
  granted to `service_role` only. That is deliberately DIFFERENT from the other
  tables in this repository (which grant anon SELECT for the read-only
  dashboard); credential bookkeeping has no dashboard consumer, so it is not
  exposed at all.
* The functions are SECURITY DEFINER with `SET search_path = ''` and a fully
  qualified body, so a caller cannot hijack resolution through `search_path`, and
  the function does not need to grant the backend any privilege on `vault.*`.
* `owner_id` is NOT NULL and the resolution function accepts an OPTIONAL
  `p_owner_id`; the backend passes it only when an owner context exists, so
  single-owner deployments keep the documented M2.4 call exactly.

APPLICATION CONSUMER

`backend/ai/credential_source.py` — ONE `db.rpc("api_credential_pool",
{"p_provider": provider}).execute()` per provider, dispatched on the existing
bounded Supabase pool with a 5 s ceiling. A missing function, a refusal, a
malformed row, an empty secret and a bad identifier each contribute nothing, so
the runtime degrades to the provider's ENV credential instead of failing.

## MANUAL SUPABASE ACTION REQUIRED

This migration has NOT been executed and Supabase has NOT been modified.
`vault.secrets` is owned by the Vault extension, so applying this file is an
owner action in the Supabase SQL Editor (it must run as `postgres`). The
identical SQL, with rollback, is reproduced in DATABASE_ARCHITECTURE.md §29.

Rollback (destructive only to the objects created here):

    DROP FUNCTION IF EXISTS public.stt_credential_pool(text, bigint);
    DROP FUNCTION IF EXISTS public.api_credential_pool(text, bigint);
    DROP TABLE IF EXISTS public.api_credentials;

    -- The supabase_vault extension is NOT dropped: it is a shared Supabase
    -- extension and may serve other secrets. Vault secrets referenced by
    -- dropped metadata rows are NOT deleted either — remove them explicitly in
    -- the Vault UI/SQL only when you are sure nothing else uses them.
*/

-- ============================================================================
-- 1. Supabase Vault — the ONLY place a raw secret may live
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS supabase_vault WITH SCHEMA vault;

-- ============================================================================
-- 2. api_credentials — credential METADATA. No secret column exists here.
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.api_credentials (
    credential_id    text        PRIMARY KEY,
    provider         text        NOT NULL,
    label            text        NOT NULL,
    owner_id         bigint      NOT NULL,
    enabled          boolean     NOT NULL DEFAULT true,
    priority         integer     NOT NULL DEFAULT 0,
    vault_secret_id  uuid        NOT NULL
                                 REFERENCES vault.secrets(id) ON DELETE CASCADE,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT api_credentials_id_format
        CHECK (credential_id ~ '^[A-Za-z0-9._-]{1,64}$'),
    CONSTRAINT api_credentials_provider_format
        CHECK (provider ~ '^[a-z0-9][a-z0-9._-]{0,31}$'),
    CONSTRAINT api_credentials_label_not_blank
        CHECK (length(btrim(label)) > 0 AND length(label) <= 80),
    CONSTRAINT api_credentials_owner_positive
        CHECK (owner_id > 0),
    CONSTRAINT api_credentials_priority_range
        CHECK (priority BETWEEN 0 AND 1000000)
);

CREATE INDEX IF NOT EXISTS idx_api_credentials_provider_order
    ON public.api_credentials (provider, priority, created_at, credential_id);
CREATE INDEX IF NOT EXISTS idx_api_credentials_owner
    ON public.api_credentials (owner_id, provider);
CREATE UNIQUE INDEX IF NOT EXISTS uq_api_credentials_vault_secret
    ON public.api_credentials (vault_secret_id);

ALTER TABLE public.api_credentials ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE public.api_credentials FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.api_credentials TO service_role;

COMMENT ON TABLE public.api_credentials IS
    'API credential METADATA for every provider. Stores NO secret: the key lives in Supabase Vault and is referenced by vault_secret_id. Service-role only.';
COMMENT ON COLUMN public.api_credentials.credential_id IS
    'Stable, non-secret identifier (1-64 chars, [A-Za-z0-9._-]). This is the only credential name that may appear in a log line.';
COMMENT ON COLUMN public.api_credentials.provider IS
    'Provider token shared with the runtime registry (e.g. gemini, groq, speechmatics, openai).';
COMMENT ON COLUMN public.api_credentials.label IS
    'Non-secret human label for the owner.';
COMMENT ON COLUMN public.api_credentials.priority IS
    'Deterministic ordering; a LOWER value is tried first. Ties fall back to created_at then credential_id.';
COMMENT ON COLUMN public.api_credentials.vault_secret_id IS
    'Reference to vault.secrets(id). The raw key exists only in Vault; deleting the secret cascades this metadata row.';

-- ============================================================================
-- 3. api_credential_pool — the ONE resolution boundary (provider-generic)
-- ============================================================================

CREATE OR REPLACE FUNCTION public.api_credential_pool(
    p_provider text,
    p_owner_id bigint DEFAULT NULL
)
RETURNS TABLE (
    credential_id text,
    secret        text,
    priority      integer,
    enabled       boolean
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = ''
AS $$
    SELECT c.credential_id,
           d.decrypted_secret AS secret,
           c.priority,
           c.enabled
      FROM public.api_credentials AS c
      JOIN vault.decrypted_secrets AS d
        ON d.id = c.vault_secret_id
     WHERE c.provider = p_provider
       AND c.enabled
       AND (p_owner_id IS NULL OR c.owner_id = p_owner_id)
     ORDER BY c.priority ASC, c.created_at ASC, c.credential_id ASC
     LIMIT 8;
$$;

ALTER FUNCTION public.api_credential_pool(text, bigint) OWNER TO postgres;

REVOKE ALL ON FUNCTION public.api_credential_pool(text, bigint)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.api_credential_pool(text, bigint) TO service_role;

COMMENT ON FUNCTION public.api_credential_pool(text, bigint) IS
    'Returns the ordered, ENABLED credentials of ONE provider with each secret decrypted from Supabase Vault. At most 8 rows. Callable by service_role only.';

-- ============================================================================
-- 4. stt_credential_pool — deprecated alias of the same contract
-- ============================================================================
-- The M2.4 documentation named this function. It is kept as a thin alias so an
-- installation that already implemented the older name keeps working; the
-- application calls api_credential_pool directly. Remove this alias in a later
-- phase, once no deployment relies on the old name.

CREATE OR REPLACE FUNCTION public.stt_credential_pool(
    p_provider text,
    p_owner_id bigint DEFAULT NULL
)
RETURNS TABLE (
    credential_id text,
    secret        text,
    priority      integer,
    enabled       boolean
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = ''
AS $$
    SELECT * FROM public.api_credential_pool(p_provider, p_owner_id);
$$;

ALTER FUNCTION public.stt_credential_pool(text, bigint) OWNER TO postgres;

REVOKE ALL ON FUNCTION public.stt_credential_pool(text, bigint)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.stt_credential_pool(text, bigint) TO service_role;

COMMENT ON FUNCTION public.stt_credential_pool(text, bigint) IS
    'Deprecated compatibility alias of api_credential_pool. Use api_credential_pool.';

-- ============================================================================
-- 5. PostgREST schema cache
-- ============================================================================

NOTIFY pgrst, 'reload schema';
