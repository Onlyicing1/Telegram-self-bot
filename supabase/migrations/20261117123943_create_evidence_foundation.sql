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
  trigger also refuses UPDATE. `ai_evidence_refs` is append-only except one
  write-once `revision_id` link (column-level grant + guard trigger) so a
  reference resolves to the exact immutable revision it produced.
- There is no owner foreign key because the repository has no owner table.
- The archive has no automatic expiry and no automatic archival.

Applying this migration requires explicit owner authorization. Do not apply it automatically.

Rollback (dependency-safe, newest first):

    DROP TRIGGER IF EXISTS ai_evidence_revisions_immutable ON ai_evidence_revisions;
    DROP FUNCTION IF EXISTS ai_evidence_revision_immutable();
    DROP TRIGGER IF EXISTS ai_evidence_refs_revision_guard ON ai_evidence_refs;
    DROP FUNCTION IF EXISTS ai_evidence_ref_revision_guard();
    DROP FUNCTION IF EXISTS ai_evidence_archive_revision(uuid, uuid, bigint, bigint, bigint, text, text, text, integer, bigint);
    DROP FUNCTION IF EXISTS ai_evidence_create_run(uuid, bigint, bigint, text, smallint, bigint, bigint, smallint, integer, text, integer);
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

-- A reference belongs to exactly one owner and one run of that same owner.
-- On INSERT it cannot point at another owner's run; on UPDATE every column is
-- immutable except a single NULL -> revision_id link, and that revision must
-- belong to the same owner/chat/message. No row can ever be repointed.
CREATE OR REPLACE FUNCTION ai_evidence_ref_revision_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    run_owner        bigint;
    run_peer         bigint;
    revision_owner   bigint;
    revision_peer    bigint;
    revision_message bigint;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF NEW.run_id IS DISTINCT FROM OLD.run_id
           OR NEW.owner_id IS DISTINCT FROM OLD.owner_id
           OR NEW.ordinal IS DISTINCT FROM OLD.ordinal
           OR NEW.message_id IS DISTINCT FROM OLD.message_id
           OR NEW.excerpt IS DISTINCT FROM OLD.excerpt
           OR NEW.content_hash IS DISTINCT FROM OLD.content_hash
           OR NEW.has_media IS DISTINCT FROM OLD.has_media
           OR NEW.excerpt_truncated IS DISTINCT FROM OLD.excerpt_truncated THEN
            RAISE EXCEPTION 'evidence refs are append-only except revision linkage';
        END IF;
        IF OLD.revision_id IS NOT NULL
           AND NEW.revision_id IS DISTINCT FROM OLD.revision_id THEN
            RAISE EXCEPTION 'an evidence ref revision link is write-once';
        END IF;
    END IF;

    SELECT run.owner_id, run.peer_id
      INTO run_owner, run_peer
      FROM ai_evidence_runs run
     WHERE run.id = NEW.run_id;
    IF run_owner IS NULL THEN
        RAISE EXCEPTION 'evidence ref run does not exist';
    END IF;
    IF run_owner <> NEW.owner_id THEN
        RAISE EXCEPTION 'evidence ref owner does not match its run owner';
    END IF;

    IF NEW.revision_id IS NULL THEN
        RETURN NEW;
    END IF;

    SELECT rev.owner_id, src.peer_id, src.message_id
      INTO revision_owner, revision_peer, revision_message
      FROM ai_evidence_revisions rev
      JOIN ai_evidence_sources src ON src.id = rev.source_id
     WHERE rev.id = NEW.revision_id;
    IF revision_owner IS NULL THEN
        RAISE EXCEPTION 'evidence ref revision does not exist';
    END IF;
    IF revision_owner <> NEW.owner_id
       OR revision_peer IS DISTINCT FROM run_peer
       OR revision_message <> NEW.message_id THEN
        RAISE EXCEPTION 'evidence ref revision does not match this owner/source';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS ai_evidence_refs_revision_guard ON ai_evidence_refs;
CREATE TRIGGER ai_evidence_refs_revision_guard
    BEFORE INSERT OR UPDATE ON ai_evidence_refs
    FOR EACH ROW EXECUTE FUNCTION ai_evidence_ref_revision_guard();

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

-- ── atomic quota admission (application calls these, never a scheduler) ─────
--
-- Both functions serialize on one advisory lock per owner. The quota check and
-- the write are therefore one transaction: two concurrent requests can never
-- both pass the check. The limits are passed in from the approved application
-- policy rather than duplicated here.

CREATE OR REPLACE FUNCTION ai_evidence_create_run(
    p_id uuid,
    p_owner_id bigint,
    p_peer_id bigint,
    p_peer_name text,
    p_ttl_days smallint,
    p_first_message_id bigint,
    p_last_message_id bigint,
    p_ref_count smallint,
    p_text_bytes integer,
    p_summary text,
    p_max_active integer
) RETURNS jsonb
LANGUAGE plpgsql
AS $$
DECLARE
    stored  ai_evidence_runs;
    active  integer;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended('ai_evidence:' || p_owner_id::text, 0));

    -- The caller's authorized admission has already swept expired runs
    -- opportunistically; this lock makes the count and the insert one decision,
    -- so concurrent admissions can never exceed the cap. A still-valid run is
    -- never evicted here.
    SELECT count(*) INTO active
      FROM ai_evidence_runs
     WHERE owner_id = p_owner_id AND expires_at > now();
    IF active >= p_max_active THEN
        RETURN jsonb_build_object('status', 'quota');
    END IF;

    INSERT INTO ai_evidence_runs (
        id, owner_id, peer_id, peer_name, ttl_days,
        first_message_id, last_message_id, ref_count, text_bytes, summary
    ) VALUES (
        p_id, p_owner_id, p_peer_id, p_peer_name, p_ttl_days,
        p_first_message_id, p_last_message_id, p_ref_count, p_text_bytes, p_summary
    )
    RETURNING * INTO stored;

    RETURN jsonb_build_object('status', 'created', 'run', to_jsonb(stored));
END;
$$;

REVOKE ALL ON FUNCTION ai_evidence_create_run(uuid, bigint, bigint, text, smallint, bigint, bigint, smallint, integer, text, integer) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION ai_evidence_create_run(uuid, bigint, bigint, text, smallint, bigint, bigint, smallint, integer, text, integer) TO service_role;

CREATE OR REPLACE FUNCTION ai_evidence_archive_revision(
    p_id uuid,
    p_source_id uuid,
    p_owner_id bigint,
    p_peer_id bigint,
    p_message_id bigint,
    p_content_hash text,
    p_text text,
    p_approval_reference text,
    p_max_records integer,
    p_max_bytes bigint
) RETURNS jsonb
LANGUAGE plpgsql
AS $$
DECLARE
    source_row   ai_evidence_sources;
    revision_row ai_evidence_revisions;
    records      bigint;
    bytes        bigint;
    next_no      integer;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended('ai_evidence:' || p_owner_id::text, 0));

    SELECT * INTO source_row
      FROM ai_evidence_sources
     WHERE owner_id = p_owner_id
       AND peer_id = p_peer_id
       AND message_id = p_message_id;

    IF FOUND THEN
        SELECT * INTO revision_row
          FROM ai_evidence_revisions
         WHERE owner_id = p_owner_id
           AND source_id = source_row.id
           AND content_hash = p_content_hash;
        IF FOUND THEN
            RETURN jsonb_build_object(
                'status', 'unchanged',
                'source_id', source_row.id,
                'revision_id', revision_row.id,
                'revision_no', revision_row.revision_no,
                'content_hash', revision_row.content_hash
            );
        END IF;
    END IF;

    SELECT count(*), COALESCE(sum(octet_length(text)), 0)
      INTO records, bytes
      FROM ai_evidence_revisions
     WHERE owner_id = p_owner_id;
    IF records >= p_max_records
       OR bytes + octet_length(p_text) > p_max_bytes THEN
        RETURN jsonb_build_object('status', 'quota');
    END IF;

    IF source_row.id IS NULL THEN
        INSERT INTO ai_evidence_sources (id, owner_id, peer_id, message_id)
        VALUES (p_source_id, p_owner_id, p_peer_id, p_message_id)
        RETURNING * INTO source_row;
    END IF;

    SELECT COALESCE(max(revision_no), 0) + 1 INTO next_no
      FROM ai_evidence_revisions
     WHERE owner_id = p_owner_id AND source_id = source_row.id;

    INSERT INTO ai_evidence_revisions (
        id, source_id, owner_id, revision_no, content_hash, text, approval_reference
    ) VALUES (
        p_id, source_row.id, p_owner_id, next_no, p_content_hash, p_text, p_approval_reference
    )
    RETURNING * INTO revision_row;

    UPDATE ai_evidence_sources
       SET observed_state = CASE WHEN next_no = 1 THEN 'live' ELSE 'edited' END,
           last_observed_at = now()
     WHERE id = source_row.id AND owner_id = p_owner_id;

    RETURN jsonb_build_object(
        'status', 'created',
        'source_id', source_row.id,
        'revision_id', revision_row.id,
        'revision_no', revision_row.revision_no,
        'content_hash', revision_row.content_hash
    );
END;
$$;

REVOKE ALL ON FUNCTION ai_evidence_archive_revision(uuid, uuid, bigint, bigint, bigint, text, text, text, integer, bigint) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION ai_evidence_archive_revision(uuid, uuid, bigint, bigint, bigint, text, text, text, integer, bigint) TO service_role;

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
-- The ONLY update a ref can ever accept: its write-once revision link.
GRANT UPDATE (revision_id) ON TABLE ai_evidence_refs TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE ai_evidence_sources TO service_role;
GRANT SELECT, INSERT, DELETE ON TABLE ai_evidence_revisions TO service_role;

COMMENT ON TABLE ai_evidence_runs IS
    'Temporary owner-scoped Active Evidence Runs. TTL is immutable, persisted and enforced with database time; expiry denies access immediately. No scheduler or cleanup loop exists.';
COMMENT ON TABLE ai_evidence_refs IS
    'Bounded excerpt snapshots for one Active Run. Append-only except a write-once revision_id link; archive deletion never deletes these rows.';
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
