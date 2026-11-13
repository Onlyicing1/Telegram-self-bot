# IMPLEMENTATION REPORT

## Task objective

Remove wasted Intelligent Reaction AI requests when the owner sends one
sentence as several separate Telegram messages faster than a single request
completes: at most one Intelligent Reaction AI request may be in flight at a
time, messages arriving meanwhile must be evaluated together in the next
request, every message must keep its own identity and its own decision, and the
result must be delivered to `origin/main`.

## Root cause (established from source)

`backend/bot/handlers/intelligent_reaction.py` registers one
`events.NewMessage(outgoing=True)` handler and calls
`process_outgoing_message` once per outgoing update. Each call ran its own
`manager.chat(...)` with **no shared in-flight state and no queue**: every
update runs in its own handler coroutine, each awaited its own provider call
concurrently, nothing recorded that an evaluation was already running, and
nothing collected the messages that arrived meanwhile. Rapid successive
messages therefore produced one concurrent AI request each.

## Integration with the newer `main`

While this work was in progress `origin/main` advanced by three commits that
redesigned the SAME feature (`5701aed`, `098d594`, `dcad23c`): the probability
became owner configuration (`panel_settings.intelligent_reaction_probability`,
default 25) and the AI now returns only `{"reaction": <emoji|null>}`, with a
legacy `probability` field invalidating the answer. The branches had diverged,
so the delivery is a **merge** (no rebase, no force-push, no history rewrite)
that keeps that redesign and ports the coalescing onto it. Nothing from
`main` was reverted and no remote commit was lost.

## Implementation details

**Queue (leader/follower, per owner).** The first eligible message — one that
already passed the owner's probability gate and the existing eligibility gates
— becomes the leader in `_submit` and runs the AI request in its own handler
coroutine with no delay. While that request is awaited, further eligible
messages append themselves to the owner scope's pending list and await their own
`asyncio.Future`. When the request finishes, the leader detaches the whole
pending list and evaluates it in one request; messages arriving during that
batch accumulate for the next one, repeating until the queue is empty. No task,
loop, scheduler, worker, listener, Telegram client, provider path or executor
was added.

**Atomicity.** Append, leader claim and detach are plain synchronous code with
no `await` between them, so on the single event loop the sequence cannot
interleave with another coroutine and no message can be taken twice or lost.
The state is rebound per event loop, so a restart or a fresh loop cannot inherit
stale futures or a leader flag; leftover work is reported in one bounded log
line instead of being forgotten silently.

**Probability stays deterministic and per message.** The owner's percentage is
read per message and the independent draw happens **before** queueing, so a 0%
setting costs no catalog read, no AI call and no queue entry, and 100% always
passes without consuming randomness. A coalesced batch therefore contains only
messages that have already won their own probability decision; the AI never
sees or influences it.

**Identity.** Each queued `_Item` keeps its own `owner_id`, `client`,
`chat_id`, `msg_id` and bounded context (message text, coarse chat type, reply
flag, at most one replied-to text). The single-message contract is unchanged
(`{"reaction": emoji|null}` through `build_ai_messages` /
`parse_ai_reaction`). A coalesced batch sends one prompt carrying independent
records labelled with a synthetic per-request id `1..n` (never Telegram ids)
and expects `{"results": [{"id": <label>, "reaction": <emoji|null>}]}`.

**Strict batch validation.** `parse_batch_ai_reactions` rejects the WHOLE
answer for malformed JSON, unexpected top-level fields, an unexpected
per-result field (including a legacy `probability`), a non-integer/unknown id,
a duplicate id or a missing id — every message of that batch then fails as
`skipped_invalid_ai_response` and nothing is sent, because the mapping is no
longer trustworthy. A per-message `null` is `skipped_no_semantic_reaction`, an
unusable emoji is `skipped_invalid_ai_response`, and a non-catalog emoji is
`skipped_unsupported_reaction`; those fail only their own message. The emoji a
batch result names is sent to that result's own `(chat_id, msg_id)`, through
`reaction_service.react_to_message`, exactly once.

**Bounds.** At most `MAX_BATCH_MESSAGES = 20` records and about
`MAX_BATCH_PROMPT_CHARS = 12000` characters of message plus replied-to text per
request, always taking at least one record. Overflow stays queued for the next
request; text is never truncated to fit. Batch output is bounded at
`MAX_BATCH_TOKENS = 256` and `MAX_BATCH_ANSWER_CHARS = 4000`.

**Preserved gates.** The single listener, owner check, outgoing/sender proof,
inline-origin exclusion, pending-panel-input exclusion, settings gate, the
Auto-Reaction-wins conflict gate, the `(chat_id, msg_id)` dedup (now claimed
before queueing, so a duplicate update while a message waits is rejected), the
Telegram-catalog requirement and the one-attempt send are all unchanged.

## Exact changed files

| File | Change |
|---|---|
| `backend/services/intelligent_reaction_service.py` | Per-owner coalescing queue, batch prompt/parser for the react-only contract, per-message send, shared provider-call helper |
| `tests/test_intelligent_reaction.py` | 12 new test functions (19 new cases, 125 in the file); fixture also resets the coalescer |
| `AGENTS.md` | Corrected the Intelligent Reaction section (owner-set probability, AI reaction-only, coalescing) |
| `ROADMAP.md` | Coalescing bullet in the Intelligent Reaction feature notes |
| `IMPLEMENTATION_REPORT.md` | This report |

The merge also carries `main`'s own changes to the handler, General panel,
settings service, probability migration, database docs and their tests; those
are unmodified from `main`.

## Tests executed and actual results

| Command | Result |
|---|---|
| `python -m pytest tests/test_intelligent_reaction.py -q` | **125 passed** (1.01 s) |
| `python -m pytest tests -q` (merged tree) | **6 failed, 7532 passed, 27 skipped** (141.88 s) |
| `python -m pytest tests -q` (baseline worktree at `dcad23c` = `origin/main`) | **6 failed, 7513 passed, 27 skipped** (162.13 s) |
| `python -m compileall -q backend tests` | exit 0 |
| `git diff --check` | exit 0 |

The 19 new cases cover: immediate first start with a follower only queueing;
several messages sharing the next request (each mapped to its own id and
emoji); a third batch; absence of any timer/debounce in the coalescer; `null`
and unsupported results affecting only their own message; unknown, duplicate,
missing, legacy-probability, extra-field and non-JSON batch answers failing the
whole batch with nothing sent; recovery after a provider failure; cancellation
settling followers and unblocking later messages; duplicate rejection while
queued; the batch bound splitting a burst without loss; catalog outage failing
the batch with no AI call; 0% never queueing or calling the AI; and a sustained
25-message burst with one request in flight and no lost or duplicated message.
They synchronise on events and on coalescer state, never on wall-clock sleeps.

## Full-suite failures and baseline status

Baseline status is **proven, not asserted**: the full suite was run in a
throwaway worktree checked out at `dcad23c` (the exact `origin/main` commit this
merge brings in, before the coalescing code) and produced the same six failures
with 7513 passing. The merged tree produces exactly those six with 7532 passing
(+19 tests, no new failure):

1. `tests/test_aed_db_security.py::test_the_documentation_describes_the_restrictive_model`
2. `tests/test_canonical_schema_reconciliation.py::test_the_migration_is_forward_only_and_its_successors_stay_additive`
3. `tests/test_database_setup_order.py::test_the_section_does_not_pretend_the_sql_was_executed`
4. `tests/test_database_setup_order.py::test_the_waiting_answer_migration_is_absorbed_by_part_one`
5. `tests/test_private_message_db_security.py::test_the_rls_model_names_the_two_tables_as_server_only_exceptions`
6. `tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`

All six are documentation/inventory drift or order-dependent tests that touch
no file changed here.

## Compilation and diff checks

`python -m compileall -q backend tests` → exit 0. `git diff --check` → exit 0.
The repository configures no ruff, flake8 or mypy, so there is no separate
static/type check to run.

## Cancellation and queue limitations

- Coalescing is in-memory and per process; it does not coordinate across
  processes or restarts. A replaced event loop cannot inherit queue state, and
  any leftover is logged in one bounded warning.
- The leader runs inside the handler coroutine of the message that started the
  request. If it is cancelled (disconnect, shutdown), leadership is released
  and the in-flight plus queued messages settle as `skipped_ai_failure` with
  "the reaction batch was interrupted"; the next message starts cleanly.
  Nothing is replayed, so no exactly-once guarantee is claimed.
- A structurally invalid batch answer fails every message in that batch
  (fail-closed) instead of guessing an assignment.
- A batch may span chats (each message still executes against its own chat and
  message id). The replied-to text is supplied by the existing handler per
  message; only the AI request is coalesced.
- A duplicate or edited update is rejected by the dedup guard, which is claimed
  before queueing — a message waiting in the queue is already claimed.

## Live Telegram verification status

Not performed. No deployment, restart, live model call or Telegram UI
interaction was made, and live latency was not measured; no latency improvement
is claimed for the deployed runtime. Only the request-count and coalescing
behaviour is proven, offline, against a scripted ProviderManager.

## Commit, branch and push

| Item | Value |
|---|---|
| Branch | `freebuff/f5137e20b1c5761eea04f020` |
| Coalescing feature commits | `3a2e8ab` (implementation) and `da16483` (earlier report), built on `c43c5fa` |
| `main` tip merged in | `dcad23c648039908ffc952b69908ba6cd586aed0` |
| Delivery commit | the merge commit that contains this report (HEAD of `main`); a commit cannot embed its own hash, so the verified value is the `origin/main` HEAD recorded in the delivery summary |
| Push | fast-forward from `dcad23c` to the merge commit; no force, no rebase, no history rewrite |
| Verified `origin/main` HEAD | equal to the final local HEAD, confirmed with `git fetch`, `git rev-parse origin/main` and `git merge-base --is-ancestor` |
| Migrations | `20261112140000_…enabled` and `20261112160000_…probability` are **not applied**; that remains a manual owner action |
| Final working tree | clean apart from the gitignored `.venv/` |
