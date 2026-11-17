/*
# G4-A / F1 — Single-Source Evidence Foundation

This migration creates the durable Active Evidence Run store and the immutable,
owner-approved Evidence Archive. It is additive and idempotent.

Retention classes:

- `ai_evidence_runs` + `ai_evidence_refs`: owner-scoped temporary snapshots.
  TTL is selected at creation (1–30 days, default 7), persisted, and enforced by
  application checks against authoritative database time. Expiry denies access
  immediately; no scheduler, tombstone or background worker exists. Physical
  rows may be deleted opportunistically by an already-authorized operation.
- `ai_evidence_sources` + `ai_evidence_revisions`: owner-approved archive. A
  source is stable identity only (owner, chat, message), never evidence text.
  Revisions are append-only immutable snapshots. The same content hash is a
  no-op; changed content is a new candidate revision requiring explicit owner
  approval. No mutable "latest" pointer exists and refs identify an exact
  revision where one is attached.

Security:

- RLS is enabled with ZERO policies (backend/service-role authorization only).
- PUBLIC/anon/authenticated have no table or function privileges.
- `ai_evidence_revisions` is granted SELECT/INSERT/DELETE but never UPDATE; a
  trigger also refuses UPDATE. `ai_evidence_refs` is likewise append-only.
- There is no owner foreign key because the repository has no owner table.
- The archive has no automatic expiry and no automatic archival.

Applying this to Supabase is a manual owner action. No agent executes SQL.

Rollback (dependency-safe, newest first):

    DROP TRIGGER IF EXISTS ai_evidence_revisions_immutable ON ai_evidence_revisions;
    DROP FUNCTION IF EXISTS ai_evidence_revision_immutable();
    DROP FUNCTION IF EXISTS ai_evidence_archive_usage(bigint);
    DROP FUNCTION IF EXISTS ai_evidence_now();
    DROP TABLE IF EXISTS ai_evidence_revisions;
    DROP TABLE IF EXISTS ai_evidence_sources;
    DROP TABLE IF EXISTS ai_evidence_refs;
    DROP TABLE IF EXISTS ai_evidence_runs;
    DROP FUNCTION IF EXISTS ai_evidence_set_run_expiry();
*/

-- ── authoritative database time and run expiry ─────────────────────────────

CREATE OR REPLACE FUNCTION ai_evidence_now()
RETURNS timestamptz
LANGUAGE sql
STABLE
AS $$
    SELECT now();
$$;

REVOKE ALL ON FUNCTION ai_evidence_now() FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION ai_evidence_now() TO service_role;

CREATE TABLE IF NOT EXISTS ai_evidence_runs (
    id                  uuid         PRIMARY KEY,
    owner_id            bigint       NOT NULL,
    peer_id             bigint       NOT NULL,
    peer_name           text         NOT NULL DEFAULT '',
    ttl_days            smallint     NOT NULL,
    first_message_id    bigint       NOT NULL,
    last_message_id     bigint       NOT NULL,
    ref_count           smallint     NOT NULL DEFAULT 0,
    text_bytes          integer      NOT NULL DEFAULT 0,
    summary             text         NOT NULL DEFAULT '',
    created_at          timestamptz  NOT NULL DEFAULT now(),
    expires_at          timestamptz  NOT NULL,
    CONSTRAINT ai_evidence_runs_peer_nonzero CHECK (peer_id <> 0),
    CONSTRAINT ai_evidence_runs_peer_name_size CHECK (char_length(peer_name) <= 128),
    CONSTRAINT ai_evidence_runs_ttl_range CHECK (ttl_days BETWEEN 1 AND 30),
    CONSTRAINT ai_evidence_runs_first_positive CHECK (first_message_id > 0),
    CONSTRAINT ai_evidence_runs_last_positive CHECK (last_message_id > 0),
    CONSTRAINT ai_evidence_runs_message_order CHECK (first_message_id <= last_message_id),
    CONSTRAINT ai_evidence_runs_ref_count_range CHECK (ref_count BETWEEN 0 AND 50),
    CONSTRAINT ai_evidence_runs_text_bytes_range CHECK (text_bytes BETWEEN 0 AND 32768),
    CONSTRAINT ai_evidence_runs_summary_size CHECK (octet_length(summary) <= 32768),
    CONSTRAINT ai_evidence_runs_expiry_after_creation CHECK (expires_at > created_at)
);

CREATE OR REPLACE FUNCTION ai_evidence_set_run_expiry()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF NEW.created_at IS DISTINCT FROM OLD.created_at
           OR NEW.ttl_days IS DISTINCT FROM OLD.ttl_days
           OR NEW.expires_at IS DISTINCT FROM OLD.expires_at THEN
            RAISE EXCEPTION 'evidence run TTL is immutable after creation';
        END IF;
        RETURN NEW;
    END IF;

    -- The TTL is persisted and always derived from authoritative database
    -- time. An INSERT cannot choose an earlier created_at/expiry and an UPDATE
    -- cannot extend it later.
    NEW.created_at := now();
    NEW.expires_at := NEW.created_at + make_interval(days => NEW.ttl_days);
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS ai_evidence_runs_set_expiry ON ai_evidence_runs;
CREATE TRIGGER ai_evidence_runs_set_expiry
    BEFORE INSERT OR UPDATE ON ai_evidence_runs
    FOR EACH ROW EXECUTE FUNCTION ai_evidence_set_run_expiry();

CREATE INDEX IF NOT EXISTS idx_ai_evidence_runs_owner_active
    ON ai_evidence_runs (owner_id, expires_at);
CREATE INDEX IF NOT EXISTS idx_ai_evidence_runs_owner_created
    ON ai_evidence_runs (owner_id, created_at DESC);

CREATE TABLE IF NOT EXISTS ai_evidence_refs (
    run_id              uuid         NOT NULL REFERENCES ai_evidence_runs(id) ON DELETE CASCADE,
    owner_id            bigint       NOT NULL,
    ordinal             smallint     NOT NULL,
    message_id          bigint       NOT NULL,
    excerpt             text         NOT NULL DEFAULT '',
    content_hash        char(64)     NOT NULL,
    has_media           boolean      NOT NULL DEFAULT false,
    excerpt_truncated   boolean      NOT NULL DEFAULT false,
    revision_id         uuid,
    CONSTRAINT ai_evidence_refs_pkey PRIMARY KEY (run_id, ordinal),
    CONSTRAINT ai_evidence_refs_ordinal_range CHECK (ordinal BETWEEN 1 AND 50),
    CONSTRAINT ai_evidence_refs_message_positive CHECK (message_id > 0),
    CONSTRAINT ai_evidence_refs_excerpt_size CHECK (char_length(excerpt) <= 1000),
    CONSTRAINT ai_evidence_refs_hash_shape CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ai_evidence_refs_run_message_key UNIQUE (run_id, message_id)
);

CREATE INDEX IF NOT EXISTS idx_ai_evidence_refs_owner_run
    ON ai_evidence_refs (owner_id, run_id);

-- ── archive: stable source identity + append-only revisions ─────────────────

CREATE TABLE IF NOT EXISTS ai_evidence_sources (
    id                  uuid         PRIMARY KEY,
    owner_id            bigint       NOT NULL,
    peer_id             bigint       NOT NULL,
    message_id          bigint       NOT NULL,
    observed_state      text         NOT NULL DEFAULT 'live',
    first_seen_at       timestamptz  NOT NULL DEFAULT now(),
    last_observed_at    timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT ai_evidence_sources_peer_nonzero CHECK (peer_id <> 0),
    CONSTRAINT ai_evidence_sources_message_positive CHECK (message_id > 0),
    CONSTRAINT ai_evidence_sources_state_check CHECK (
        observed_state IN ('live', 'edited', 'deleted', 'inaccessible')
    ),
    CONSTRAINT ai_evidence_sources_identity UNIQUE (owner_id, peer_id, message_id)
);

CREATE INDEX IF NOT EXISTS idx_ai_evidence_sources_owner_seen
    ON ai_evidence_sources (owner_id, last_observed_at DESC);

CREATE TABLE IF NOT EXISTS ai_evidence_revisions (
    id                  uuid         PRIMARY KEY,
    source_id           uuid         NOT NULL REFERENCES ai_evidence_sources(id) ON DELETE CASCADE,
    owner_id            bigint       NOT NULL,
    revision_no         integer      NOT NULL,
    content_hash        char(64)     NOT NULL,
    text                text         NOT NULL DEFAULT '',
    approval_reference  text         NOT NULL DEFAULT 'owner-confirmed',
    captured_at         timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT ai_evidence_revisions_no_positive CHECK (revision_no > 0),
    CONSTRAINT ai_evidence_revisions_text_size CHECK (char_length(text) <= 1000),
    CONSTRAINT ai_evidence_revisions_hash_shape CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ai_evidence_revisions_approval_size CHECK (char_length(approval_reference) <= 128),
    CONSTRAINT ai_evidence_revisions_number_key UNIQUE (source_id, revision_no),
    CONSTRAINT ai_evidence_revisions_hash_key UNIQUE (source_id, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_ai_evidence_revisions_owner_source
    ON ai_evidence_revisions (owner_id, source_id, revision_no);

CREATE OR REPLACE FUNCTION ai_evidence_revision_immutable()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'archived evidence revisions are immutable';
END;
$$;

DROP TRIGGER IF EXISTS ai_evidence_revisions_immutable ON ai_evidence_revisions;
CREATE TRIGGER ai_evidence_revisions_immutable
    BEFORE UPDATE ON ai_evidence_revisions
    FOR EACH ROW EXECUTE FUNCTION ai_evidence_revision_immutable();

CREATE OR REPLACE FUNCTION ai_evidence_archive_usage(p_owner_id bigint)
RETURNS TABLE(record_count bigint, byte_count bigint)
LANGUAGE sql
STABLE
AS $$
    SELECT
        count(*)::bigint AS record_count,
        COALESCE(sum(octet_length(text)), 0)::bigint AS byte_count
    FROM ai_evidence_revisions
    WHERE owner_id = p_owner_id;
$$;

REVOKE ALL ON FUNCTION ai_evidence_archive_usage(bigint) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION ai_evidence_archive_usage(bigint) TO service_role;

-- ── service-role-only access ────────────────────────────────────────────────

ALTER TABLE ai_evidence_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE ai_evidence_refs ENABLE ROW LEVEL SECURITY;
ALTER TABLE ai_evidence_sources ENABLE ROW LEVEL SECURITY;
ALTER TABLE ai_evidence_revisions ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE ai_evidence_runs FROM PUBLIC, anon, authenticated;
REVOKE ALL ON TABLE ai_evidence_refs FROM PUBLIC, anon, authenticated;
REVOKE ALL ON TABLE ai_evidence_sources FROM PUBLIC, anon, authenticated;
REVOKE ALL ON TABLE ai_evidence_revisions FROM PUBLIC, anon, authenticated;

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE ai_evidence_runs TO service_role;
GRANT SELECT, INSERT, DELETE ON TABLE ai_evidence_refs TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE ai_evidence_sources TO service_role;
GRANT SELECT, INSERT, DELETE ON TABLE ai_evidence_revisions TO service_role;

COMMENT ON TABLE ai_evidence_runs IS
    'Temporary owner-scoped Active Evidence Runs. TTL is immutable, persisted and enforced with database time; expiry denies access immediately. No scheduler or cleanup loop exists.';
COMMENT ON TABLE ai_evidence_refs IS
    'Bounded excerpt snapshots for one Active Run. Append-only; archive deletion never deletes these rows.';
COMMENT ON TABLE ai_evidence_sources IS
    'Stable archive source identity (owner, chat, message). Contains no evidence text. Telegram edits/deletes may update observed_state only.';
COMMENT ON TABLE ai_evidence_revisions IS
    'Immutable, owner-approved archive snapshots. Same content hash is a no-op; changed content requires a new approval. No mutable latest pointer exists.';

NOTIFY pgrst, 'reload schema';

SELECT 'ai_evidence_runs table' AS check, count(*) AS present
FROM information_schema.tables
WHERE table_schema = 'public' AND table_name = 'ai_evidence_runs';

SELECT 'ai_evidence_columns' AS check, count(*) AS present
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name IN (
      'ai_evidence_runs', 'ai_evidence_refs',
      'ai_evidence_sources', 'ai_evidence_revisions'
  );

SELECT 'ai_evidence_policies' AS check, count(*) AS present
FROM pg_policies
WHERE schemaname = 'public'
  AND tablename IN (
      'ai_evidence_runs', 'ai_evidence_refs',
      'ai_evidence_sources', 'ai_evidence_revisions'
  );
