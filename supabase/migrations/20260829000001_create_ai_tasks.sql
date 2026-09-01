/*
# Durable AI task scheduler state

Creates the two task-system tables required by the reviewed contract.
Scheduling and execution remain application responsibilities; these tables
store owner-scoped definitions and durable occurrence state only.
*/

CREATE TABLE IF NOT EXISTS ai_tasks (
    id                         bigserial    PRIMARY KEY,
    owner_id                   bigint       NOT NULL,
    label                      text         NOT NULL,
    status                     text         NOT NULL DEFAULT 'active'
                               CHECK (status IN ('active', 'paused', 'completed', 'failed', 'expired', 'deleted')),
    version                    integer      NOT NULL DEFAULT 1
                               CHECK (version > 0),
    schedule_type              text         NOT NULL
                               CHECK (schedule_type IN ('once', 'interval', 'daily', 'weekly')),
    schedule                   jsonb        NOT NULL,
    timezone                   text         NOT NULL,
    next_run_at               timestamptz,
    actions                    jsonb        NOT NULL
                               CHECK (jsonb_typeof(actions) = 'array'),
    notification_destination   jsonb        NOT NULL,
    ai_instruction              text,
    created_at                 timestamptz  NOT NULL DEFAULT now(),
    updated_at                 timestamptz  NOT NULL DEFAULT now(),
    terminal_at                timestamptz,
    CONSTRAINT ai_tasks_label_not_blank CHECK (length(btrim(label)) > 0),
    CONSTRAINT ai_tasks_actions_count CHECK (jsonb_array_length(actions) BETWEEN 1 AND 5),
    CONSTRAINT ai_tasks_payload_size CHECK (octet_length(actions::text) <= 32768),
    CONSTRAINT ai_tasks_schedule_size CHECK (octet_length(schedule::text) <= 16384),
    CONSTRAINT ai_tasks_destination_size CHECK (octet_length(notification_destination::text) <= 4096),
    CONSTRAINT ai_tasks_ai_instruction_size CHECK (ai_instruction IS NULL OR (length(btrim(ai_instruction)) > 0 AND octet_length(ai_instruction) <= 16384))
);

CREATE INDEX IF NOT EXISTS idx_ai_tasks_status_next_run
    ON ai_tasks (status, next_run_at);
CREATE INDEX IF NOT EXISTS idx_ai_tasks_owner_updated
    ON ai_tasks (owner_id, updated_at DESC);

ALTER TABLE ai_tasks ENABLE ROW LEVEL SECURITY;

-- Backend access uses the service-role key. No public write policy is added.
DROP POLICY IF EXISTS "anon_select_ai_tasks" ON ai_tasks;

GRANT SELECT ON ai_tasks TO anon, authenticated;

CREATE TABLE IF NOT EXISTS ai_task_occurrences (
    id                    bigserial    PRIMARY KEY,
    task_id               bigint       NOT NULL
                          REFERENCES ai_tasks(id) ON DELETE RESTRICT,
    owner_id              bigint       NOT NULL,
    occurrence_key        text         NOT NULL,
    definition_version    integer      NOT NULL CHECK (definition_version > 0),
    action_snapshot       jsonb        NOT NULL
                          CHECK (jsonb_typeof(action_snapshot) = 'array'),
    scheduled_for         timestamptz  NOT NULL,
    attempt               smallint     NOT NULL DEFAULT 1
                          CHECK (attempt BETWEEN 1 AND 3),
    status                text         NOT NULL DEFAULT 'claimed'
                          CHECK (status IN ('claimed', 'running', 'succeeded', 'failed', 'retry_pending', 'cancelled', 'expired', 'interrupted')),
    claimed_at            timestamptz,
    started_at            timestamptz,
    finished_at           timestamptz,
    retry_at              timestamptz,
    error_metadata        jsonb        NOT NULL DEFAULT '{}'
                          CHECK (jsonb_typeof(error_metadata) = 'object'),
    result_metadata       jsonb        NOT NULL DEFAULT '{}',
    preparation_metadata  jsonb        NOT NULL DEFAULT '{}'
                          CHECK (jsonb_typeof(preparation_metadata) = 'object'),
    created_at            timestamptz  NOT NULL DEFAULT now(),
    updated_at            timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT ai_task_occurrences_key_not_blank CHECK (length(btrim(occurrence_key)) > 0),
    CONSTRAINT ai_task_occurrences_action_count CHECK (jsonb_array_length(action_snapshot) BETWEEN 1 AND 5),
    CONSTRAINT ai_task_occurrences_payload_size CHECK (octet_length(action_snapshot::text) <= 32768),
    CONSTRAINT ai_task_occurrences_error_size CHECK (octet_length(error_metadata::text) <= 8192),
    CONSTRAINT ai_task_occurrences_result_size CHECK (octet_length(result_metadata::text) <= 8192),
    CONSTRAINT ai_task_occurrences_preparation_size CHECK (octet_length(preparation_metadata::text) <= 8192),
    CONSTRAINT ai_task_occurrences_retry_state CHECK (
        (status = 'retry_pending' AND retry_at IS NOT NULL)
        OR (status <> 'retry_pending')
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_task_occurrences_task_key
    ON ai_task_occurrences (task_id, occurrence_key);
CREATE INDEX IF NOT EXISTS idx_ai_task_occurrences_owner_scheduled
    ON ai_task_occurrences (owner_id, scheduled_for DESC);
CREATE INDEX IF NOT EXISTS idx_ai_task_occurrences_task_scheduled
    ON ai_task_occurrences (task_id, scheduled_for DESC);

ALTER TABLE ai_task_occurrences ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "anon_select_ai_task_occurrences" ON ai_task_occurrences;
GRANT SELECT ON ai_task_occurrences TO anon, authenticated;
