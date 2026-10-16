/*
# Private Message Management: the two dedicated persistence tables

Private Message Management (the closed-PV policy) stores its state in its own
tables instead of the generic `bot_settings` key/value document it used before,
and persists ONE durable counter row per human sender. This migration creates
both tables in their FINAL shape. Like every successor migration it is
forward-only and additive: it creates two tables, their constraints, one index,
RLS policies, grants and comments, and it touches no existing table, column,
row, policy or grant.

    private_message_config    the owner's configuration AND the access state the
                              panel edits (ONE row per owner)
    private_message_senders   durable per-person AI-reply counters + timing state

## Columns

### private_message_config
- `owner_id`                  — PK: exactly ONE configuration row per owner.
- `enabled`                   — the master switch; NOT NULL DEFAULT `false`, so
                                applying this migration can never start
                                enforcing anything by itself.
- `mode`                      — `open` / `whitelist_only` / `closed`.
- `delete_incoming`           — INDEPENDENT action flag: delete the eligible
                                incoming message.
- `fixed_response_enabled`    — INDEPENDENT action flag: send the configured
                                fixed response. Its own flag and its own
                                control — never implied by another action.
- `fixed_response_text`       — the configured fixed response text (plain text,
                                ≤ 512 characters; empty means nothing to send).
- `ai_response_enabled`       — INDEPENDENT action flag: generate and send an AI
                                response.
- `ai_max_replies_per_person` — the finite per-person AI allowance (1..10000).
- `ai_unlimited_replies`      — the explicit unlimited option: when TRUE the
                                allowance is not enforced.
- `ai_interval_seconds`       — minimum seconds between two AI replies to the
                                SAME person; `0` is a supported no-delay value.
- `allowlist`                 — `bigint[]`: the user ids `whitelist_only` lets
                                through (Phase 1 semantics, unchanged).
- `exceptions`                — `bigint[]`: the user ids `closed` lets through.
- `temporary`                 — `jsonb`: the active temporary override
                                (`mode`, `activated_at`, `expires_at`) or NULL.
- `schedule`                  — `jsonb`: `enabled`, IANA `timezone` and the
                                ordered rules (days + `HH:MM` + mode).
- `updated_at`                — durable timestamp.

### private_message_senders
- `owner_id`          — owning Telegram user. Every repository read/write
                        filters by it.
- `sender_id`         — the POSITIVE Telegram user id of the human sender. A
                        bot, group, supergroup or channel can never reach this
                        table: only an event that passed the scope gate is ever
                        counted, and the gate rejects a private chat with a bot.
- `ai_replies_used`   — durable count of DELIVERED AI replies to that person.
                        Incremented by one bounded conditional update (CAS) and
                        given back by the compensating CAS when generation or
                        delivery failed, so only a delivered reply consumes the
                        allowance.
- `last_ai_reply_at`  — when the last AI reply to that person was delivered; the
                        interval check reads it.
- `updated_at`        — durable timestamp.
- `PRIMARY KEY (owner_id, sender_id)` — at most one counter row per owner and
                        person; the primary key is also the claim CAS identity
                        and the duplicate-insert backstop.

## Counter-reset policy (explicit, predictable)

Counters NEVER reset implicitly: no TTL, no time window, no reset on process
restart and no reset on a configuration change. The ONLY edge is the owner's
explicit "Reset AI counters" panel action, which deletes that owner's counter
rows (application code, not this migration). This migration seeds no row.

## Backfill strategy for existing configuration

The Phase-1 configuration lived in ONE `bot_settings` row
(`key = 'private_message_management'`, `value` = a JSON document). That document
is NOT abandoned and NOT deleted: on the first read after this migration the
service sees no dedicated row, reads the legacy document, translates it
(`sanitize_state`, which maps the single legacy `policy` value onto the
independent action flags — `delete` → `delete_incoming`, `autoreply` →
`fixed_response_enabled` + `fixed_response_text`, `ignore` → nothing enabled —
and carries `enabled`, `mode`, `allowlist`, `exceptions`, `temporary` and
`schedule` over unchanged) and immediately writes the dedicated row. So the
import happens exactly once, every previously configured rule survives, and the
legacy document stays in `bot_settings` as the owner left it. This migration
itself reads no other table, copies nothing and deletes nothing; the read-only
probe at the end lets the owner see the legacy document with their own eyes.

## Security

RLS is enabled on both tables and `anon`/`authenticated` receive SELECT only —
the same read-only dashboard boundary every other table has. Every write goes
through the backend service-role client, and every repository query filters by
`owner_id`, so one owner can never read or mutate another owner's rows. The AI
has no tool that reaches either table: the scope gate, the configuration and the
counters are deterministic service code, and the AI never decides eligibility.

Idempotent: safe to run more than once. Applying it to the live Supabase project
is a separate manual owner action.

## MANUAL SUPABASE ACTION REQUIRED

Apply this file (it is part 10 of the ONE complete setup script in
DATABASE_ARCHITECTURE.md §31.3, so it is normally applied by running that block
once). Until it is applied the feature is INERT: the configuration read degrades
to "no row", which the service treats as disabled, so nothing is deleted, no
response is sent and no counter is consumed — the panel reports the failed write
honestly instead of pretending a setting was saved.

Rollback (destroys the configuration and the per-person counters — the feature
then reports honestly that its durable store is missing and stays OFF; the
Phase-1 `bot_settings` document is NOT touched, so the previous configuration is
still there):

    DROP TABLE IF EXISTS private_message_senders;
    DROP TABLE IF EXISTS private_message_config;

Nothing else in the schema changes: no existing table, column, index, policy,
grant or row is touched.
*/

CREATE TABLE IF NOT EXISTS private_message_config (
    owner_id                  bigint      PRIMARY KEY,
    enabled                   boolean     NOT NULL DEFAULT false,
    mode                      text        NOT NULL DEFAULT 'open',
    delete_incoming           boolean     NOT NULL DEFAULT false,
    fixed_response_enabled    boolean     NOT NULL DEFAULT false,
    fixed_response_text       text        NOT NULL DEFAULT '',
    ai_response_enabled       boolean     NOT NULL DEFAULT false,
    ai_max_replies_per_person integer     NOT NULL DEFAULT 10,
    ai_unlimited_replies      boolean     NOT NULL DEFAULT false,
    ai_interval_seconds       integer     NOT NULL DEFAULT 0,
    allowlist                 bigint[]    NOT NULL DEFAULT '{}',
    exceptions                bigint[]    NOT NULL DEFAULT '{}',
    temporary                 jsonb,
    schedule                  jsonb,
    updated_at                timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT private_message_config_mode_valid
        CHECK (mode IN ('open', 'whitelist_only', 'closed')),
    CONSTRAINT private_message_config_max_replies_range
        CHECK (ai_max_replies_per_person BETWEEN 1 AND 10000),
    CONSTRAINT private_message_config_interval_range
        CHECK (ai_interval_seconds BETWEEN 0 AND 86400),
    CONSTRAINT private_message_config_fixed_text_length
        CHECK (length(fixed_response_text) <= 512)
);

CREATE TABLE IF NOT EXISTS private_message_senders (
    owner_id         bigint      NOT NULL,
    sender_id        bigint      NOT NULL,
    ai_replies_used  integer     NOT NULL DEFAULT 0,
    last_ai_reply_at timestamptz,
    updated_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_id, sender_id),
    CONSTRAINT private_message_senders_used_nonnegative
        CHECK (ai_replies_used >= 0),
    CONSTRAINT private_message_senders_sender_positive
        CHECK (sender_id > 0)
);

CREATE INDEX IF NOT EXISTS idx_private_message_senders_owner_updated
    ON private_message_senders (owner_id, updated_at DESC);

ALTER TABLE private_message_config ENABLE ROW LEVEL SECURITY;
ALTER TABLE private_message_senders ENABLE ROW LEVEL SECURITY;

GRANT SELECT ON private_message_config TO anon, authenticated;
GRANT SELECT ON private_message_senders TO anon, authenticated;

DROP POLICY IF EXISTS "anon_insert_private_message_config" ON private_message_config;
DROP POLICY IF EXISTS "anon_update_private_message_config" ON private_message_config;
DROP POLICY IF EXISTS "anon_delete_private_message_config" ON private_message_config;
DROP POLICY IF EXISTS "anon_select_private_message_config" ON private_message_config;
CREATE POLICY "anon_select_private_message_config" ON private_message_config FOR SELECT
    TO anon, authenticated USING (true);

DROP POLICY IF EXISTS "anon_insert_private_message_senders" ON private_message_senders;
DROP POLICY IF EXISTS "anon_update_private_message_senders" ON private_message_senders;
DROP POLICY IF EXISTS "anon_delete_private_message_senders" ON private_message_senders;
DROP POLICY IF EXISTS "anon_select_private_message_senders" ON private_message_senders;
CREATE POLICY "anon_select_private_message_senders" ON private_message_senders FOR SELECT
    TO anon, authenticated USING (true);

COMMENT ON TABLE private_message_config IS
    'ONE Private Message Management configuration row per owner. The scope (genuine human users in direct private chats only) is NOT configurable: the delete, fixed-response and AI-response actions each have their own independent enable flag, the feature defaults to OFF, and ai_interval_seconds = 0 is a supported no-delay value.';
COMMENT ON COLUMN private_message_config.ai_max_replies_per_person IS
    'Finite per-person AI allowance (1..10000). ai_unlimited_replies = TRUE disables the limit; the per-person counters live in private_message_senders.';
COMMENT ON COLUMN private_message_config.ai_interval_seconds IS
    'Minimum seconds between two AI replies to the SAME person. 0 means no delay.';
COMMENT ON COLUMN private_message_config.allowlist IS
    'User ids that whitelist_only mode lets through, in ascending order (Phase 1 semantics, unchanged).';
COMMENT ON COLUMN private_message_config.exceptions IS
    'User ids that closed mode lets through, in ascending order (Phase 1 semantics, unchanged).';
COMMENT ON COLUMN private_message_config.temporary IS
    'The active temporary override: {mode, activated_at, expires_at} or NULL. It stops applying by itself at expires_at — no ticker is involved.';
COMMENT ON COLUMN private_message_config.schedule IS
    'The weekly schedule: {enabled, timezone (IANA), rules: [{id, days, start, end, mode}]}. A rule whose end is at or before its start spans midnight.';
COMMENT ON TABLE private_message_senders IS
    'Durable per-person AI-reply counters for Private Message Management. Only an event that passed the human-private scope gate is ever counted. Counters are never reset implicitly: the owner''s explicit reset action is the only edge.';
COMMENT ON COLUMN private_message_senders.ai_replies_used IS
    'Count of DELIVERED AI replies to this person. Claimed by one conditional (CAS) increment before generation and given back by the compensating CAS when no reply was delivered, so only a delivered reply consumes the allowance.';
COMMENT ON COLUMN private_message_senders.last_ai_reply_at IS
    'When the last AI reply was delivered; the configured interval check reads this instant.';

NOTIFY pgrst, 'reload schema';

SELECT 'private message tables' AS check, count(*) AS present
FROM information_schema.tables
WHERE table_schema = 'public'
  AND table_name IN ('private_message_config', 'private_message_senders');

SELECT 'private_message_config columns' AS check, count(*) AS present
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name   = 'private_message_config'
  AND column_name IN ('enabled', 'mode', 'delete_incoming',
                      'fixed_response_enabled', 'fixed_response_text',
                      'ai_response_enabled', 'ai_max_replies_per_person',
                      'ai_unlimited_replies', 'ai_interval_seconds',
                      'allowlist', 'exceptions', 'temporary', 'schedule');

SELECT 'private_message_senders columns' AS check, count(*) AS present
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name   = 'private_message_senders'
  AND column_name IN ('owner_id', 'sender_id', 'ai_replies_used',
                      'last_ai_reply_at', 'updated_at');

-- Read-only legacy probe: the Phase-1 document this feature reads ONCE as its
-- backfill source. A row here means the import will carry that configuration
-- over on first read (nothing is modified by this query).
SELECT 'legacy private_message settings' AS check, key
FROM bot_settings
WHERE key LIKE 'private_message%';
