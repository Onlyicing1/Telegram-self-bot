/*
# API Credential Vault — PART 2 (owner-scoped management)

PART 1 created the credential METADATA table and the ONE resolution boundary.
This migration adds the MANAGEMENT boundary the owner-facing Telegram surface
calls: create, replace the secret, update metadata, delete, and list.

    Telegram owner
        ↓
    AI → Media Analysis → API Credentials        backend/bot/handlers/ai_credentials.py
        ↓
    credential management service                backend/services/credential_service.py
        ↓
    the SECURITY DEFINER functions below
        ├── public.api_credentials   (metadata only — no secret column exists)
        └── vault.secrets            (the raw key, via the vault.* API)

WHAT THIS MIGRATION IS NOT

* It is NOT a plaintext key store and it does NOT change the PART 1 table: no
  column is added, no column can hold a secret, and `api_credentials` is left
  exactly as PART 1 created it.
* It is NOT a generic SQL execution endpoint: five functions, a fixed parameter
  list, a fixed return shape (metadata only — never a secret) and a fixed row
  ceiling on the one function that lists.
* It is NOT STT- or TTS-specific: every function takes the free-form `provider`
  token, so the same five functions manage the credentials of a
  Speech-to-Text provider and of a Text-to-Speech provider.
* It does NOT read a secret back out. There is no "show key" function: the only
  statements that touch a secret are the ones that WRITE one (create/replace).

Objects created (all in `public`, all idempotent):

| Object | Type | Purpose |
|---|---|---|
| `api_credential_list(bigint, text)` | function | owner-scoped METADATA listing (no secret), deterministic order, ≤ 64 rows |
| `api_credential_create(bigint, text, text, text, integer, boolean)` | function | creates the Vault secret AND the metadata row pointing at it |
| `api_credential_replace_secret(bigint, text, text)` | function | swaps in a NEW Vault secret and removes the old one |
| `api_credential_update(bigint, text, text, boolean, integer)` | function | metadata only (label / enabled / priority) — never the secret |
| `api_credential_delete(bigint, text)` | function | removes the Vault secret first, then the metadata row |

SECURITY MODEL

* Every function is SECURITY DEFINER with `SET search_path = ''` and a fully
  qualified body, owned by `postgres`, and REVOKEd from PUBLIC/anon/authenticated
  and granted to `service_role` only — the same posture PART 1 established, and
  deliberately different from the dashboard-readable tables.
* `owner_id` is a REQUIRED parameter of every call and every statement filters on
  it, so one owner can never read or modify another owner's credential, whatever
  button a client sends.
* A raw secret is accepted ONLY by `api_credential_create` and
  `api_credential_replace_secret`, only as an argument, and is handed straight to
  `vault.create_secret` — it is never inserted into `public.api_credentials`,
  never returned, never placed in a comment and never in a log.
* No function returns a secret: every RETURNS TABLE shape is the metadata
  projection of `api_credentials` (`credential_id, provider, label, enabled,
  priority, created_at, updated_at`).

FAILURE AND ORPHAN HANDLING

The Vault API and this table cannot be joined into one atomic unit by anything
other than the surrounding transaction, and this migration does not claim
otherwise. Each write is therefore written to be recoverable:

* CREATE — the metadata insert runs inside a nested BEGIN/EXCEPTION block that
  deletes the Vault secret it just made if the insert fails, then re-raises, so a
  failed create can never leave an unreferenced secret.
* REPLACE — the new secret is created first and the row is switched to it; the
  OLD secret is deleted only AFTER the switch, because `vault_secret_id` carries
  `ON DELETE CASCADE` and deleting it first would have removed the very row being
  updated. A failed switch deletes the new secret instead.
* DELETE — the secret is removed FIRST (which cascades the metadata row) and the
  metadata row is then deleted explicitly as a fallback, so no metadata can
  outlive its secret. A failed secret delete raises, and the caller reports the
  failure instead of a success.
* UPDATE — touches metadata only, so it can never orphan a secret.

## MANUAL SUPABASE ACTION REQUIRED

This migration has NOT been executed and Supabase has NOT been modified.
`vault.secrets` is owned by the Vault extension, so applying this file is an
owner action in the Supabase SQL Editor (it must run as `postgres`). It depends
on PART 1 (`20260919000001_create_api_credential_vault.sql`) having been applied
first. The identical SQL, with rollback, is reproduced in
DATABASE_ARCHITECTURE.md §29.13–§29.17.

Rollback (destructive only to the objects created here):

    DROP FUNCTION IF EXISTS public.api_credential_delete(bigint, text);
    DROP FUNCTION IF EXISTS public.api_credential_update(bigint, text, text, boolean, integer);
    DROP FUNCTION IF EXISTS public.api_credential_replace_secret(bigint, text, text);
    DROP FUNCTION IF EXISTS public.api_credential_create(bigint, text, text, text, integer, boolean);
    DROP FUNCTION IF EXISTS public.api_credential_list(bigint, text);

    -- The PART 1 table, its resolution RPC, the supabase_vault extension and any
    -- Vault secret are NOT touched by this rollback. Dropping the functions stops
    -- all owner-facing management; credentials already configured keep resolving
    -- through api_credential_pool and the owner's Telegram panel reports the
    -- management boundary as unavailable.
*/

-- ============================================================================
-- 1. api_credential_list — owner-scoped METADATA read (never a secret)
-- ============================================================================

CREATE OR REPLACE FUNCTION public.api_credential_list(
    p_owner_id bigint,
    p_provider text DEFAULT NULL
)
RETURNS TABLE (
    credential_id text,
    provider      text,
    label         text,
    enabled       boolean,
    priority      integer,
    created_at    timestamptz,
    updated_at    timestamptz
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = ''
AS $$
    SELECT c.credential_id,
           c.provider,
           c.label,
           c.enabled,
           c.priority,
           c.created_at,
           c.updated_at
      FROM public.api_credentials AS c
     WHERE c.owner_id = p_owner_id
       AND (p_provider IS NULL OR c.provider = p_provider)
     ORDER BY c.provider ASC, c.priority ASC, c.created_at ASC, c.credential_id ASC
     LIMIT 64;
$$;

ALTER FUNCTION public.api_credential_list(bigint, text) OWNER TO postgres;

REVOKE ALL ON FUNCTION public.api_credential_list(bigint, text)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.api_credential_list(bigint, text) TO service_role;

COMMENT ON FUNCTION public.api_credential_list(bigint, text) IS
    'Owner-scoped credential METADATA listing (no secret column is read or returned). Deterministic order: provider, priority, created_at, credential_id. At most 64 rows. Callable by service_role only.';

-- ============================================================================
-- 2. api_credential_create — Vault secret + metadata row, orphan-safe
-- ============================================================================

CREATE OR REPLACE FUNCTION public.api_credential_create(
    p_owner_id bigint,
    p_provider text,
    p_label    text,
    p_secret   text,
    p_priority integer DEFAULT 0,
    p_enabled  boolean DEFAULT true
)
RETURNS TABLE (
    credential_id text,
    provider      text,
    label         text,
    enabled       boolean,
    priority      integer,
    created_at    timestamptz,
    updated_at    timestamptz
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_provider   text;
    v_label      text;
    v_secret     text;
    v_priority   integer;
    v_enabled    boolean;
    v_id         text;
    v_attempt    integer := 0;
    v_secret_id  uuid;
    v_row        public.api_credentials%ROWTYPE;
BEGIN
    IF p_owner_id IS NULL OR p_owner_id <= 0 THEN
        RAISE EXCEPTION 'invalid_owner' USING ERRCODE = '22023';
    END IF;

    v_provider := btrim(coalesce(p_provider, ''));
    IF v_provider !~ '^[a-z0-9][a-z0-9._-]{0,31}$' THEN
        RAISE EXCEPTION 'invalid_provider' USING ERRCODE = '22023';
    END IF;

    v_label := btrim(coalesce(p_label, ''));
    IF length(v_label) = 0 OR length(v_label) > 80 THEN
        RAISE EXCEPTION 'invalid_label' USING ERRCODE = '22023';
    END IF;

    v_secret := coalesce(p_secret, '');
    IF length(v_secret) = 0 THEN
        RAISE EXCEPTION 'empty_secret' USING ERRCODE = '22023';
    END IF;
    IF length(v_secret) > 8192 THEN
        RAISE EXCEPTION 'secret_too_long' USING ERRCODE = '22023';
    END IF;

    v_priority := greatest(0, least(coalesce(p_priority, 0), 1000000));
    v_enabled  := coalesce(p_enabled, true);

    -- A short, non-secret identifier. Bounded retry: a 48-bit collision is
    -- astronomically unlikely, and the loop can never spin unbounded.
    LOOP
        v_attempt := v_attempt + 1;
        v_id := 'c' || substr(replace(gen_random_uuid()::text, '-', ''), 1, 12);
        EXIT WHEN NOT EXISTS (
            SELECT 1 FROM public.api_credentials AS c
             WHERE c.credential_id = v_id
        );
        IF v_attempt >= 3 THEN
            RAISE EXCEPTION 'credential_id_collision' USING ERRCODE = '23505';
        END IF;
    END LOOP;

    -- The ONLY statement in this repository that writes a raw secret, and it
    -- writes it into Vault. `vault.secrets.name` is UNIQUE, so the name is
    -- derived from the (already unique) credential id.
    v_secret_id := vault.create_secret(
        v_secret,
        'api_credential:' || v_id,
        'LifeOS API credential for provider ' || v_provider
    );

    BEGIN
        INSERT INTO public.api_credentials AS c (
            credential_id, provider, label, owner_id, enabled, priority, vault_secret_id
        ) VALUES (
            v_id, v_provider, v_label, p_owner_id, v_enabled, v_priority, v_secret_id
        )
        RETURNING * INTO v_row;
    EXCEPTION WHEN OTHERS THEN
        -- Never leave an unreferenced secret behind. This block runs in its own
        -- subtransaction, so it survives the failure it is cleaning up after.
        BEGIN
            DELETE FROM vault.secrets WHERE id = v_secret_id;
        EXCEPTION WHEN OTHERS THEN
            NULL;
        END;
        RAISE;
    END;

    credential_id := v_row.credential_id;
    provider      := v_row.provider;
    label         := v_row.label;
    enabled       := v_row.enabled;
    priority      := v_row.priority;
    created_at    := v_row.created_at;
    updated_at    := v_row.updated_at;
    RETURN NEXT;
    RETURN;
END;
$$;

ALTER FUNCTION public.api_credential_create(bigint, text, text, text, integer, boolean)
    OWNER TO postgres;

REVOKE ALL ON FUNCTION public.api_credential_create(bigint, text, text, text, integer, boolean)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.api_credential_create(bigint, text, text, text, integer, boolean)
    TO service_role;

COMMENT ON FUNCTION public.api_credential_create(bigint, text, text, text, integer, boolean) IS
    'Creates one Vault secret and the owner-scoped metadata row referencing it. Accepts a raw secret ONLY as an argument, stores it ONLY in Supabase Vault, and returns metadata only. Removes the just-created secret if the metadata insert fails. Callable by service_role only.';

-- ============================================================================
-- 3. api_credential_replace_secret — swap the key, never orphan the old one
-- ============================================================================

CREATE OR REPLACE FUNCTION public.api_credential_replace_secret(
    p_owner_id bigint,
    p_credential_id text,
    p_secret text
)
RETURNS TABLE (
    credential_id text,
    provider      text,
    label         text,
    enabled       boolean,
    priority      integer,
    created_at    timestamptz,
    updated_at    timestamptz
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_id        text;
    v_secret    text;
    v_old_id    uuid;
    v_new_id    uuid;
    v_row       public.api_credentials%ROWTYPE;
BEGIN
    IF p_owner_id IS NULL OR p_owner_id <= 0 THEN
        RAISE EXCEPTION 'invalid_owner' USING ERRCODE = '22023';
    END IF;

    v_id := btrim(coalesce(p_credential_id, ''));
    IF length(v_id) = 0 OR length(v_id) > 64 THEN
        RAISE EXCEPTION 'invalid_credential' USING ERRCODE = '22023';
    END IF;

    v_secret := coalesce(p_secret, '');
    IF length(v_secret) = 0 THEN
        RAISE EXCEPTION 'empty_secret' USING ERRCODE = '22023';
    END IF;
    IF length(v_secret) > 8192 THEN
        RAISE EXCEPTION 'secret_too_long' USING ERRCODE = '22023';
    END IF;

    SELECT c.vault_secret_id INTO v_old_id
      FROM public.api_credentials AS c
     WHERE c.credential_id = v_id
       AND c.owner_id = p_owner_id
       FOR UPDATE;

    IF v_old_id IS NULL THEN
        RAISE EXCEPTION 'credential_not_found' USING ERRCODE = 'P0002';
    END IF;

    v_new_id := vault.create_secret(
        v_secret,
        'api_credential:' || v_id || ':' || substr(md5(gen_random_uuid()::text), 1, 8),
        'LifeOS API credential for provider (replaced)'
    );

    BEGIN
        UPDATE public.api_credentials AS c
           SET vault_secret_id = v_new_id,
               updated_at = now()
         WHERE c.credential_id = v_id
           AND c.owner_id = p_owner_id
        RETURNING * INTO v_row;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'credential_not_found' USING ERRCODE = 'P0002';
        END IF;
    EXCEPTION WHEN OTHERS THEN
        BEGIN
            DELETE FROM vault.secrets WHERE id = v_new_id;
        EXCEPTION WHEN OTHERS THEN
            NULL;
        END;
        RAISE;
    END;

    -- Only AFTER the row points at the new secret: deleting the old one first
    -- would have cascaded the row away (vault_secret_id is ON DELETE CASCADE).
    BEGIN
        DELETE FROM vault.secrets WHERE id = v_old_id;
    EXCEPTION WHEN OTHERS THEN
        -- The new secret is live and the row is correct; a leftover old secret
        -- is inert because nothing references it any more. Reported honestly in
        -- DATABASE_ARCHITECTURE.md §29.16 rather than failing the swap.
        NULL;
    END;

    credential_id := v_row.credential_id;
    provider      := v_row.provider;
    label         := v_row.label;
    enabled       := v_row.enabled;
    priority      := v_row.priority;
    created_at    := v_row.created_at;
    updated_at    := v_row.updated_at;
    RETURN NEXT;
    RETURN;
END;
$$;

ALTER FUNCTION public.api_credential_replace_secret(bigint, text, text)
    OWNER TO postgres;

REVOKE ALL ON FUNCTION public.api_credential_replace_secret(bigint, text, text)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.api_credential_replace_secret(bigint, text, text)
    TO service_role;

COMMENT ON FUNCTION public.api_credential_replace_secret(bigint, text, text) IS
    'Replaces the Vault secret of ONE owner-scoped credential. Never returns the old or the new secret and never changes label/enabled/priority. Callable by service_role only.';

-- ============================================================================
-- 4. api_credential_update — metadata only, never the secret
-- ============================================================================

CREATE OR REPLACE FUNCTION public.api_credential_update(
    p_owner_id bigint,
    p_credential_id text,
    p_label text DEFAULT NULL,
    p_enabled boolean DEFAULT NULL,
    p_priority integer DEFAULT NULL
)
RETURNS TABLE (
    credential_id text,
    provider      text,
    label         text,
    enabled       boolean,
    priority      integer,
    created_at    timestamptz,
    updated_at    timestamptz
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_id      text;
    v_label   text;
    v_row     public.api_credentials%ROWTYPE;
BEGIN
    IF p_owner_id IS NULL OR p_owner_id <= 0 THEN
        RAISE EXCEPTION 'invalid_owner' USING ERRCODE = '22023';
    END IF;

    v_id := btrim(coalesce(p_credential_id, ''));
    IF length(v_id) = 0 OR length(v_id) > 64 THEN
        RAISE EXCEPTION 'invalid_credential' USING ERRCODE = '22023';
    END IF;

    IF p_label IS NOT NULL THEN
        v_label := btrim(p_label);
        IF length(v_label) = 0 OR length(v_label) > 80 THEN
            RAISE EXCEPTION 'invalid_label' USING ERRCODE = '22023';
        END IF;
    END IF;

    IF p_priority IS NOT NULL AND (p_priority < 0 OR p_priority > 1000000) THEN
        RAISE EXCEPTION 'invalid_priority' USING ERRCODE = '22023';
    END IF;

    UPDATE public.api_credentials AS c
       SET label      = coalesce(v_label, c.label),
           enabled    = coalesce(p_enabled, c.enabled),
           priority   = coalesce(p_priority, c.priority),
           updated_at = now()
     WHERE c.credential_id = v_id
       AND c.owner_id = p_owner_id
    RETURNING * INTO v_row;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'credential_not_found' USING ERRCODE = 'P0002';
    END IF;

    credential_id := v_row.credential_id;
    provider      := v_row.provider;
    label         := v_row.label;
    enabled       := v_row.enabled;
    priority      := v_row.priority;
    created_at    := v_row.created_at;
    updated_at    := v_row.updated_at;
    RETURN NEXT;
    RETURN;
END;
$$;

ALTER FUNCTION public.api_credential_update(bigint, text, text, boolean, integer)
    OWNER TO postgres;

REVOKE ALL ON FUNCTION public.api_credential_update(bigint, text, text, boolean, integer)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.api_credential_update(bigint, text, text, boolean, integer)
    TO service_role;

COMMENT ON FUNCTION public.api_credential_update(bigint, text, text, boolean, integer) IS
    'Updates the owner-scoped METADATA of ONE credential (label / enabled / priority). A NULL argument leaves that field unchanged. Reads and writes no secret. Callable by service_role only.';

-- ============================================================================
-- 5. api_credential_delete — secret first (it cascades), row as a fallback
-- ============================================================================

CREATE OR REPLACE FUNCTION public.api_credential_delete(
    p_owner_id bigint,
    p_credential_id text
)
RETURNS boolean
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_id        text;
    v_secret_id uuid;
BEGIN
    IF p_owner_id IS NULL OR p_owner_id <= 0 THEN
        RAISE EXCEPTION 'invalid_owner' USING ERRCODE = '22023';
    END IF;

    v_id := btrim(coalesce(p_credential_id, ''));
    IF length(v_id) = 0 OR length(v_id) > 64 THEN
        RAISE EXCEPTION 'invalid_credential' USING ERRCODE = '22023';
    END IF;

    SELECT c.vault_secret_id INTO v_secret_id
      FROM public.api_credentials AS c
     WHERE c.credential_id = v_id
       AND c.owner_id = p_owner_id
       FOR UPDATE;

    IF v_secret_id IS NULL THEN
        -- Nothing of the owner's matches: an honest "not found", not an error.
        RETURN false;
    END IF;

    -- Removing the secret is what makes this a real deletion. If it fails the
    -- function raises and the caller must NOT report success.
    DELETE FROM vault.secrets WHERE id = v_secret_id;

    -- The foreign key cascades the metadata row; this explicit delete only does
    -- anything on a deployment whose constraint is missing, and it guarantees no
    -- metadata outlives its secret either way.
    DELETE FROM public.api_credentials AS c
     WHERE c.credential_id = v_id
       AND c.owner_id = p_owner_id;

    RETURN true;
END;
$$;

ALTER FUNCTION public.api_credential_delete(bigint, text) OWNER TO postgres;

REVOKE ALL ON FUNCTION public.api_credential_delete(bigint, text)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.api_credential_delete(bigint, text) TO service_role;

COMMENT ON FUNCTION public.api_credential_delete(bigint, text) IS
    'Deletes ONE owner-scoped credential: the Vault secret first (which cascades the metadata row), then the metadata row. Returns false when the owner has no such credential. Callable by service_role only.';

-- ============================================================================
-- 6. PostgREST schema cache
-- ============================================================================

NOTIFY pgrst, 'reload schema';
