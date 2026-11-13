/*
# Scheduled History state (Phase 3B)

ONE cumulative History state per (owner_id, peer_id). A row belongs to a durable
`ai_tasks` row and is deleted with it (`ON DELETE CASCADE`). The row owns the
approved baseline cursor, the scanned cursor, the summarized cursor, the bounded
cumulative summary, and the revision/version plus error metadata needed for an
atomic compare-and-set commit and restart recovery.

Lifecycle rules:

- `UNIQUE (owner_id, peer_id)` is the ownership invariant. A second schedule for
  the same owner and peer is refused while the state exists, regardless of the
  owning task's status (`active`, `paused`, `completed`, `failed`, `expired`).
- `status = 'deleted'` is accepted only as a legacy invariant-repair case: a row
  carrying it does not permanently block an explicit owner-authorized create,
  which reuses that row after resetting its cursors and summary.
- `baseline_cursor` is immutable after creation. `scanned_cursor` and
  `summarized_cursor` are Telegram message ids, are exclusive lower cursors for
  incremental retrieval, and may only advance together with a successful commit.
  `summarized_cursor <= scanned_cursor` is legal: content may be scanned (and
  explicitly skipped) without being summarized.
- The cumulative summary is text, never bytes in settings or occurrence
  metadata, and is hard-bounded to 32 KiB of UTF-8. An over-limit new summary
  must fail; the repository never truncates it.
- `revision` counts successful content revisions (including an explicit reset);
  `version` is the compare-and-set token and increments on every accepted write,
  including a recorded failure, so a stale commit can never overwrite newer
  state.

This migration is additive and idempotent. It creates no owner foreign key
because the repository has no owner table. It is service-role-only: RLS is
enabled with ZERO policies, every privilege is revoked from PUBLIC/anon/
authenticated, and exactly SELECT/INSERT/UPDATE/DELETE are granted to
service_role.

Applying this to Supabase is a manual owner action (SQL editor as `postgres`, or
the one-paste setup in DATABASE_ARCHITECTURE.md §31.3). No agent executes it.

Rollback (dependency-safe: no other object references this table):

    DROP TABLE IF EXISTS history_schedules;
*/

CREATE TABLE IF NOT EXISTS history_schedules (
    id                  bigserial    PRIMARY KEY,
    task_id             bigint       NOT NULL REFERENCES ai_tasks(id) ON DELETE CASCADE,
    owner_id            bigint       NOT NULL,
    peer_id             bigint       NOT NULL,
    status              text         NOT NULL DEFAULT 'active',
    baseline_cursor     bigint       NOT NULL DEFAULT 0,
    scanned_cursor      bigint       NOT NULL DEFAULT 0,
    summarized_cursor   bigint       NOT NULL DEFAULT 0,
    summary             text         NOT NULL DEFAULT '',
    revision            bigint       NOT NULL DEFAULT 0,
    version             integer      NOT NULL DEFAULT 1,
    last_error          text         NOT NULL DEFAULT '',
    last_error_at       timestamptz,
    last_committed_at   timestamptz,
    created_at          timestamptz  NOT NULL DEFAULT now(),
    updated_at          timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT history_schedules_owner_peer_key UNIQUE (owner_id, peer_id),
    CONSTRAINT history_schedules_peer_nonzero CHECK (peer_id <> 0),
    CONSTRAINT history_schedules_status_check CHECK (
        status IN ('active', 'paused', 'completed', 'failed', 'expired', 'deleted')
    ),
    CONSTRAINT history_schedules_baseline_nonnegative CHECK (baseline_cursor >= 0),
    CONSTRAINT history_schedules_scanned_nonnegative CHECK (scanned_cursor >= 0),
    CONSTRAINT history_schedules_summarized_nonnegative CHECK (summarized_cursor >= 0),
    CONSTRAINT history_schedules_cursor_order CHECK (
        baseline_cursor <= summarized_cursor
        AND summarized_cursor <= scanned_cursor
    ),
    CONSTRAINT history_schedules_summary_size CHECK (octet_length(summary) <= 32768),
    CONSTRAINT history_schedules_revision_nonnegative CHECK (revision >= 0),
    CONSTRAINT history_schedules_version_positive CHECK (version > 0),
    CONSTRAINT history_schedules_error_size CHECK (octet_length(last_error) <= 2048),
    CONSTRAINT history_schedules_error_pair CHECK (
        (last_error = '') = (last_error_at IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_history_schedules_task_id
    ON history_schedules (task_id);

ALTER TABLE history_schedules ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE history_schedules FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE history_schedules TO service_role;

COMMENT ON TABLE history_schedules IS
    'ONE cumulative scheduled-History state per (owner_id, peer_id), owned by an ai_tasks row and deleted with it. Service-role only: RLS is enabled with zero policies and every privilege is revoked from PUBLIC/anon/authenticated. Cursors advance only through a successful compare-and-set commit; failures never replace the previous summary or advance either cursor.';
COMMENT ON COLUMN history_schedules.baseline_cursor IS
    'Immutable exclusive lower bound chosen at creation. Messages at or below it are outside this schedule.';
COMMENT ON COLUMN history_schedules.scanned_cursor IS
    'Exclusive lower cursor through which content has been successfully processed or explicitly safely skipped.';
COMMENT ON COLUMN history_schedules.summarized_cursor IS
    'Exclusive lower cursor through which content is actually covered by the cumulative summary; never greater than scanned_cursor.';
COMMENT ON COLUMN history_schedules.summary IS
    'Cumulative summary, at most 32 KiB of UTF-8. Never truncated on write; an over-limit result fails.';
COMMENT ON COLUMN history_schedules.revision IS
    'Successful content revisions (summary/reset commits); a recorded failure does not change it.';
COMMENT ON COLUMN history_schedules.version IS
    'Compare-and-set token incremented by every accepted write, including a recorded failure.';
COMMENT ON COLUMN history_schedules.last_error IS
    'Bounded diagnostic from the last failed processing attempt; empty when no error is recorded.';
COMMENT ON COLUMN history_schedules.last_committed_at IS
    'Timestamp of the last successful summary commit; NULL until the first commit.';

NOTIFY pgrst, 'reload schema';

SELECT 'history_schedules table' AS check, count(*) AS present
FROM information_schema.tables
WHERE table_schema = 'public'
  AND table_name   = 'history_schedules';

SELECT 'history_schedules columns' AS check, count(*) AS present
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name   = 'history_schedules';

SELECT 'history_schedules policies' AS check, count(*) AS present
FROM pg_policies
WHERE schemaname = 'public'
  AND tablename  = 'history_schedules';
