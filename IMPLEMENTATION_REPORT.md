# IMPLEMENTATION REPORT

## Task objective

Remove wasted Intelligent Reaction AI requests when the owner sends one
sentence as several separate Telegram messages faster than a single request
completes: at most one Intelligent Reaction AI request may be in flight at a
time, and messages arriving meanwhile must be collected and evaluated together
in the next request — with no debounce, fixed timer, fixed window, batching by
size or semantic sentence reconstruction. Then deliver the verified change to
`origin/main`.

## Root cause (established from source)

`backend/bot/handlers/intelligent_reaction.py` registers one
`events.NewMessage(outgoing=True)` handler and calls
`process_outgoing_message` once per outgoing update. Every call previously ran
its own `provider_manager.chat(...)` through `evaluate_message` with **no
shared in-flight state and no queue in the module**:

- each Telegram update runs in its own handler coroutine;
- each coroutine awaited its own provider call concurrently;
- nothing recorded that an evaluation was already running;
- nothing collected the messages that arrived in the meantime.

Rapid successive messages therefore produced one concurrent AI request each.

## Implementation details

**Queue (leader/follower, per owner).** The first eligible message becomes the
leader in `_submit` and runs the AI request in its own handler coroutine with
no delay. While that request is awaited, further messages append themselves to
the owner scope's pending list and await their own `asyncio.Future`. When the
request finishes, the leader detaches the whole pending list and evaluates it
in one request; messages arriving during that batch accumulate for the next
one, repeating until the queue is empty. No task, loop, scheduler, worker,
listener, Telegram client, provider path or executor was added.

**Atomicity.** Append, leader claim and detach are plain synchronous code with
no `await` between them, so on the single event loop the sequence cannot
interleave with another coroutine and a message can be neither taken twice nor
lost. The state is rebound per event loop, so a restart or a fresh loop cannot
inherit stale futures or a leader flag.

**Identity.** Each queued `_Item` keeps its own `owner_id`, `client`,
`chat_id`, `msg_id`, full text, replied-to context, provider manager and random
draw function. Decisions, the probability draw and the single
`reaction_service.react_to_message` call all run per message; only the catalog
read and the AI call are shared by the batch. Nothing is concatenated into a
synthetic sentence.

**AI contract.** A batch of one keeps the original single-object contract
unchanged (`{"probability", "reaction"}` via `parse_evaluation`). A batch of
two or more sends one bounded request carrying independent records labelled
`Message 1:`, `Message 2:`, … with each record's own text and its own optional
replied-to context (still no Telegram ids, chats, senders, usernames or
history), and expects
`{"results": [{"id": <label>, "probability", "reaction"}]}`. Validation
(`parse_batch_evaluation`) rejects the WHOLE answer for a non-list `results`, a
non-object entry, a missing/unusable label, an unknown label, a duplicate
label or a missing label — no message can inherit another's decision. A
per-message invalid probability or a reaction outside the Telegram catalog
fails only that message; the others still execute from their own records.
Labels are synthetic per-request numbers; Telegram ids never reach the model.

**Bounds.** At most `MAX_BATCH_MESSAGES = 20` records and about
`MAX_BATCH_PROMPT_CHARS = 12000` characters of message plus replied-to text per
request, always taking at least one record. Overflow stays queued for the next
request; text is never truncated to fit. Batch answers are bounded at
`MAX_BATCH_ANSWER_CHARS = 4000` and output tokens at `MAX_BATCH_TOKENS = 512`
(growing from 96 per requested result).

**Eligibility and guards preserved.** The single listener, owner check,
outgoing/sender proof, inline-origin exclusion, pending-panel-input exclusion,
settings gate, Auto-Reaction conflict gate, `(chat_id, msg_id)` dedup (claimed
before queueing) and the Telegram catalog validation are all unchanged.

## Exact changed files

| File | Change |
|---|---|
| `backend/services/intelligent_reaction_service.py` | Per-owner coalescing queue, batch AI contract + validation, per-message execution refactor, shared provider call helper |
| `tests/test_intelligent_reaction.py` | 14 new test functions (18 new test cases, 66 in the file); autouse fixture also resets the coalescer |
| `IMPLEMENTATION_REPORT.md` | This report |

No other file was modified. The handler, router, settings service, General
panel, migration, `DATABASE_ARCHITECTURE.md`, `ROADMAP.md` and `AGENTS.md` are
untouched. No SQL was executed and no Supabase or Render setting changed.

## Tests executed and actual results

All commands were run in this workspace with the project virtualenv:

| Command | Result |
|---|---|
| `python -m pytest tests/test_intelligent_reaction.py -q` | **66 passed** (1.67 s) |
| `python -m pytest tests -q` (full suite, current tree) | **6 failed, 7473 passed, 27 skipped** (159.86 s) |
| `python -m pytest tests -q` (full suite, pristine baseline worktree at `c43c5fa`) | **6 failed, 7455 passed, 27 skipped** (165.88 s) |
| `python -m compileall -q backend tests` | exit 0 |
| `git diff --check` | exit 0 |

The 18 new test cases cover: immediate first start; follower queueing with no
second request; several messages sharing the next request; a third batch;
absence of any timer/debounce; `max_in_flight == 1`; per-message identity;
result-to-message mapping with the message's own emoji; null isolation;
unknown/duplicate/missing/non-list/id-less batch answers; an invalid reaction
failing only its message; recovery after a failed request; cancellation
cleanup; duplicate rejection while queued; the batch bound splitting a burst
without loss; per-message probability/draw; catalog outage failing the whole
batch without an AI call; and a sustained 25-message burst with no overlapping
request and no lost or duplicated message. They synchronise on events and on
coalescer state, never on wall-clock sleeps.

## Full-suite failures and baseline status

Baseline status is **proven, not asserted**: the full suite was run in a
throwaway `git worktree` checked out at `c43c5fa` (the exact commit the
coalescing diff is applied to, before any change), and it produced the same
six failures with 7455 passing. The current tree produces exactly those six
with 7473 passing, i.e. +18 tests and **no new failure**:

1. `tests/test_aed_db_security.py::test_the_documentation_describes_the_restrictive_model`
2. `tests/test_canonical_schema_reconciliation.py::test_the_migration_is_forward_only_and_its_successors_stay_additive`
3. `tests/test_database_setup_order.py::test_the_section_does_not_pretend_the_sql_was_executed`
4. `tests/test_database_setup_order.py::test_the_waiting_answer_migration_is_absorbed_by_part_one`
5. `tests/test_private_message_db_security.py::test_the_rls_model_names_the_two_tables_as_server_only_exceptions`
6. `tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`

All six are documentation/inventory drift or order-dependent tests that touch
no file changed here; none is introduced by this work.

## Compilation and diff checks

`python -m compileall -q backend tests` → exit 0. `git diff --check` → exit 0
(no whitespace errors). The repository configures no ruff, flake8 or mypy, so
there is no separate static/type check to run.

## Cancellation and queue limitations

- Coalescing is in-memory and per process; it does not coordinate across
  processes or restarts. When the event loop itself is replaced, leftover
  queued work is reported in one bounded warning instead of being forgotten
  silently.
- The leader runs inside the handler coroutine of the message that started the
  request. If that coroutine is cancelled (disconnect, shutdown), leadership is
  released and the in-flight plus queued messages are settled with
  `failed` / "the reaction batch was interrupted"; the next message starts from
  clean state. Nothing is replayed, so no exactly-once guarantee is invented
  where the Telegram execution path cannot provide one.
- A structurally invalid batch answer fails every message in that batch
  (fail-closed) rather than guessing an assignment.
- A batch can span chats (each message still executes against its own chat and
  message id), and the per-message replied-to context still costs one Telegram
  read per message before queueing; only the AI request is coalesced.
- A duplicate or edited update is still rejected by the existing dedup guard,
  which is claimed before queueing.

## Live Telegram verification status

Not performed. No deployment, restart, model call or Telegram UI interaction
was made, and live latency was not measured; no latency improvement is claimed
for the deployed runtime. Only the request-count and coalescing behaviour is
proven, offline, against a scripted ProviderManager.

## Commit, branch and push

| Item | Value |
|---|---|
| Branch | `freebuff/f5137e20b1c5761eea04f020` |
| Implementation commit | `3a2e8abe51590c23f3149b51d62b53abb0ec4ef1` — `feat(reactions): coalesce in-flight intelligent reaction requests` |
| Report commit | the commit that contains this file, immediately after the implementation commit |
| Remote base before delivery | `61d499c3f55dbb9d3cafca7f83b535dfe49a7d48` (`origin/main`) — an ancestor of local HEAD; no remote commit was missing locally |
| Push | fast-forward from `61d499c3` to the report commit; verified after pushing with `git ls-remote origin refs/heads/main` and `git merge-base --is-ancestor` |
| `origin/main` HEAD | equals the final local HEAD (the report commit); verified against GitHub |
| Final working tree | clean apart from the gitignored `.venv/` |

A commit cannot contain its own hash, so the exact hash of the report commit is
the value recorded as `origin/main` HEAD after delivery rather than embedded
in this file; the implementation commit above is the substantive change and is
reachable from `main`.
