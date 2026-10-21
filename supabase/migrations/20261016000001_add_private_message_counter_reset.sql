/*
# Private Message Management: the configurable automatic per-person counter reset

Private Message Management's per-person AI allowance is durable and atomic, but
its counters only ever reset through the owner's explicit "Reset AI counters"
action. This migration adds the ONE piece of state the configurable AUTOMATIC
reset needs, so the owner can choose how often each person's used count starts
over.

    private_message_config.ai_counter_reset_enabled   the automatic-reset switch
    private_message_config.ai_counter_reset_seconds   how long one window lasts
    private_message_senders.window_started_at         the person's window anchor

## Semantics (deterministic, and stated in the panel)

The reset is a ROLLING WINDOW anchored to a person's FIRST counted reply in the
current window:

- `window_started_at` is written by the claim that delivers the person's first
  counted reply in that window. A row created before this migration has no
  window (NULL); the next counted reply starts one. A NULL window can never
  expire, so nothing changes for a person until they are answered again.
  Consequently a counter row that ALREADY sits at its ceiling with no recorded
  window is not silently forgiven by enabling this feature: it has no counted
  reply to anchor a window to, and a claim that reads NULL must not be allowed to
  treat "window unknown" as "window expired" without a NULL-safe CAS identity
  (a row with `ai_replies_used = 1` and no window could otherwise be rolled twice
  by two racing claims). Such a person stays at the ceiling until the owner uses
  the manual **Reset AI counters** action or raises the limit — a deliberate
  fail-closed choice: the feature never grants an allowance it cannot justify.
- When `ai_counter_reset_enabled` is TRUE and `now() - window_started_at` is at
  or past `ai_counter_reset_seconds`, the window has expired. The NEXT eligible
  incoming message observes the expired window, resets `ai_replies_used` to zero
  and starts a new window — all INSIDE the existing claim CAS
  (`backend/db/client.py::_claim_private_message_ai_reply_sync`), so the reset
  and the claim can never disagree. There is no periodic task, scheduler or
  background loop: expiry is evaluated only in the incoming-message quota path.
- When `ai_counter_reset_enabled` is FALSE nothing ever expires and the counters
  behave exactly as before (manual reset only). The window is still maintained,
  so enabling the feature later never loses a person's position.
- The window is DURABLE, so a process restart cannot reset a counter or restart
  anyone's window.
- An automatic reset rolls the COUNT only. `ai_max_replies_per_person`,
  `ai_unlimited_replies`, `ai_interval_seconds`, the three action flags and the
  eligible-message scope are all unaffected; the minimum interval between two
  replies to the same person is read from `last_ai_reply_at` regardless of the
  window, so a reset never shortens the anti-spam floor.

## Why the window is part of the claim CAS

`window_started_at` is read together with `ai_replies_used` and both are used as
the conditional `UPDATE ... WHERE` identity. Without the window in the filter,
two concurrent claims that both observed the same expired window could both roll
it (writing the same post-reset count), which would hand out two replies where
the limit allows one. With it, exactly one claim rolls the window; the loser's
CAS matches no row and it re-reads the new state.

## Columns

### private_message_config
- `ai_counter_reset_enabled boolean NOT NULL DEFAULT false` — OFF by default, so
  applying this migration can never start resetting anything by itself.
- `ai_counter_reset_seconds integer NOT NULL DEFAULT 86400` — one day by
  default; `CHECK (BETWEEN 60 AND 31536000)` (1 minute .. 365 days) mirrors the
  bounds the panel validates, so a value the panel rejects can never be stored.

### private_message_senders
- `window_started_at timestamptz` — nullable on purpose: NULL means "no reset
  window recorded yet" and is rolled into a real window by the next counted
  reply. Deliberately NOT `NOT NULL DEFAULT now()`: a default would silently
  backfill every existing person with an artificial window instant this
  migration cannot know.

## Backfill strategy

None is needed and none is performed. The new configuration columns take their
defaults on every existing `private_message_config` row (automatic reset OFF,
interval one day). `window_started_at` stays NULL on existing counter rows, which
is the documented "no window yet" state. No counter is reset and no reply
allowance is handed out by applying this file.

## Security

No change: both tables already have RLS enabled with SELECT-only `anon` /
`authenticated` policies, and every write goes through the backend service-role
client. Adding columns does not alter any policy, grant or row.

Idempotent: safe to run more than once. Applying it to the live Supabase project
is a separate manual owner action, and the coding agent never executes SQL.

## MANUAL SUPABASE ACTION REQUIRED

Apply this file in the Supabase SQL editor, or run the ONE complete setup script
in DATABASE_ARCHITECTURE.md §31.3, which carries this file's executable
statements verbatim as its part 11 (immediately after the two tables this file
alters). Until it is applied, saving ANY Private Message setting fails
honestly: the configuration write names the new columns, so a database without
them rejects the statement, the service keeps the previous in-memory state and
the panel reports the failed save instead of pretending the setting was stored.

Rollback (loses the automatic-reset configuration and every person's window
anchor; the counters themselves are preserved):

    ALTER TABLE private_message_config DROP CONSTRAINT IF EXISTS private_message_config_reset_interval_range;
    ALTER TABLE private_message_config DROP COLUMN IF EXISTS ai_counter_reset_seconds;
    ALTER TABLE private_message_config DROP COLUMN IF EXISTS ai_counter_reset_enabled;
    ALTER TABLE private_message_senders DROP COLUMN IF EXISTS window_started_at;

Nothing else in the schema changes: no existing table, column, index, policy,
grant or row is touched, and no table is created or dropped.
*/

ALTER TABLE private_message_config
    ADD COLUMN IF NOT EXISTS ai_counter_reset_enabled boolean NOT NULL DEFAULT false;

ALTER TABLE private_message_config
    ADD COLUMN IF NOT EXISTS ai_counter_reset_seconds integer NOT NULL DEFAULT 86400;

ALTER TABLE private_message_config
    DROP CONSTRAINT IF EXISTS private_message_config_reset_interval_range;
ALTER TABLE private_message_config
    ADD CONSTRAINT private_message_config_reset_interval_range
    CHECK (ai_counter_reset_seconds BETWEEN 60 AND 31536000);

ALTER TABLE private_message_senders
    ADD COLUMN IF NOT EXISTS window_started_at timestamptz;

COMMENT ON COLUMN private_message_config.ai_counter_reset_enabled IS
    'Automatic per-person counter reset switch. FALSE means counters change only through the owner''s explicit reset action.';
COMMENT ON COLUMN private_message_config.ai_counter_reset_seconds IS
    'Length of one automatic-reset window in seconds (60..31536000). A window starts at a person''s first counted reply and is rolled by the next eligible message after it expires.';
COMMENT ON COLUMN private_message_senders.window_started_at IS
    'Instant of this person''s first counted AI reply in the current reset window. NULL means no window is recorded yet and the next counted reply starts one. Rolled ONLY inside the claim CAS in the incoming-message quota path.';

NOTIFY pgrst, 'reload schema';

SELECT 'private_message_config reset columns' AS check, count(*) AS present
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name   = 'private_message_config'
  AND column_name IN ('ai_counter_reset_enabled', 'ai_counter_reset_seconds');

SELECT 'private_message_senders window column' AS check, count(*) AS present
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name   = 'private_message_senders'
  AND column_name IN ('window_started_at');

SELECT 'private_message_config reset constraint' AS check, count(*) AS present
FROM information_schema.table_constraints
WHERE table_schema    = 'public'
  AND table_name      = 'private_message_config'
  AND constraint_name = 'private_message_config_reset_interval_range'
  AND constraint_type = 'CHECK';
