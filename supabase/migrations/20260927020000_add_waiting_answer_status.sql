/*
# Durable question/answer continuation: the `waiting_answer` occurrence status

A durable task (Todo Part 3D) may pause because it needs ONE specific answer
from the owner: the chain reaches its single `ask_owner` action, the question
is sent to the owner's chat, and the occurrence must then park until the
owner's correlated reply (an explicit reply to the exact sent question) is
durably consumed.

`retry_pending` + `retry_at` is deliberately NOT used for this park. A time
boundary (Phase 3B) is ELIGIBILITY: the wake loop may and must consume the row
as soon as `retry_at` passes. A question has NO clock — only the owner's
correlated reply may resume the workflow. Parking a question on the retry pair
would let the wake loop re-execute the chain at the next poll, re-asking the
question every wake. So Part 3D introduces the ONE new occurrence status:
`waiting_answer`.

This migration widens exactly the `ai_task_occurrences_status_check`
constraint (created by the 20260829000001 tasks migration and re-added by the
20260920000001 schema-repair migration) and nothing else: no new
table, no new column, no index, no policy, no grant. Existing rows are
unchanged (the widened enum accepts every row the old constraint accepted).

The application contract for the new status (see
`backend/ai/task_contract.py` and `backend/ai/database/task_repository.py`):

  - `waiting_answer` may only be entered from `running` and only carries a
    validated `pending_question` record in `result_metadata`
    (repository-enforced);
  - the ONLY edge out is the answer-resume CAS
    (`resume_waiting_for_answer`: `waiting_answer → retry_pending(now)`),
    which the owner's correlated reply drives;
  - no scheduler query (`list_due_retry_occurrences`), no recovery query
    (`list_recoverable_occurrences`), no event-duplicate guard and no claim
    CAS ever selects or consumes a `waiting_answer` row: the parked
    workflow survives any number of restarts untouched.

Manual application (Supabase SQL editor) — this is the only step; the coding
agent never executes SQL against Supabase:

    ALTER TABLE ai_task_occurrences DROP CONSTRAINT IF EXISTS ai_task_occurrences_status_check;
    ALTER TABLE ai_task_occurrences ADD CONSTRAINT ai_task_occurrences_status_check
        CHECK (status IN ('claimed', 'running', 'succeeded', 'failed', 'retry_pending',
                          'cancelled', 'expired', 'interrupted', 'waiting_answer'));

    NOTIFY pgrst, 'reload schema';

Rollback (only when no occurrence is parked on a question):

    ALTER TABLE ai_task_occurrences DROP CONSTRAINT IF EXISTS ai_task_occurrences_status_check;
    ALTER TABLE ai_task_occurrences ADD CONSTRAINT ai_task_occurrences_status_check
        CHECK (status IN ('claimed', 'running', 'succeeded', 'failed', 'retry_pending',
                          'cancelled', 'expired', 'interrupted'));
*/

ALTER TABLE ai_task_occurrences DROP CONSTRAINT IF EXISTS ai_task_occurrences_status_check;
ALTER TABLE ai_task_occurrences ADD CONSTRAINT ai_task_occurrences_status_check
    CHECK (status IN ('claimed', 'running', 'succeeded', 'failed', 'retry_pending',
                      'cancelled', 'expired', 'interrupted', 'waiting_answer'));
