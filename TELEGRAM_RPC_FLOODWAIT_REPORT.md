# TELEGRAM_RPC_FLOODWAIT_REPORT.md — Telegram FloodWait / private-scope reliability repair

| Item | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Branch / baseline | `main` at `0d07dc16a5c76fc8abafec21b0df32622e2cbbf2` (local was fast-forwarded from `21f3df0`; no rebase, no reset) |
| Date | 2026-11-06 |
| Telethon | **1.43.0** (`backend/requirements.txt`), `FloodWaitError.seconds` is the server-provided wait |
| Scope | keepalive FloodWait handling, private-message scope prefetch/entity resolution, cancellation *investigation* only |
| Not touched | AI ProviderManager, provider adapters, Dispatcher, ToolExecutor, fallback/continuation policy, Supabase/SQL/schema, `DATABASE_ARCHITECTURE.md`, existing AI investigation docs, dependencies, deployment |
| Live Telegram / production credentials | **none used** — all verification is in-process with fakes |

## 1. Root cause confirmed from source

### 1.1 Keepalive treated a rate limit as a broken connection

`backend/runtime/keepalive.py` had one catch-all:

```python
except Exception as exc:
    trace_exception("KEEPALIVE_FAILED", exc, ...)
    logger.warning("KEEPALIVE_FAILED — %s ...", type(exc).__name__)
    if not sup._recovery_lock.locked():
        guarded_create_task(sup._trigger_reconnect(), name="lifeos-keepalive-recovery")
```

`telethon.errors.FloodWaitError` is not a connection failure; it is Telegram
telling the client to stop calling for N seconds. Because it was caught by
`except Exception`, every keepalive tick that hit a FloodWait logged
`KEEPALIVE_FAILED — FloodWaitError` and scheduled `_trigger_reconnect()`, which
drops/recreates the connection (and, as §6 shows, cancels in-flight handler
tasks). That is the observed `KEEPALIVE_FAILED — FloodWaitError` +
`WATCHDOG_RECOVERY reason=reconnect` pair.

### 1.2 Private scope resolution spent two entity RPCs per update, with no suppression

`backend/bot/handlers/private_message.py::resolve_scope()` ran

```python
chat_entity = await _resolve_entity(event, "get_chat")
sender_entity = await _resolve_entity(event, "get_sender")
```

unconditionally, for **every** update that reached `enforce_incoming()`
(groups, channels, outgoing messages and contradictory events included), and a
failure of the first did not prevent the second. `_resolve_entity` caught every
exception and logged `PRIVATE_MESSAGE_TRACE stage=<getter>_unresolved
error=FloodWaitError`, then the next update repeated both calls — one warning
per incoming update while the same FloodWait stayed active. Nothing recorded
that Telegram had asked for a wait.

The classifier itself (`private_message_service.classify_scope`) already
requires `peer_user_id == chat_id == sender_id`, both entities to be real
`types.User` with the matching id, a real boolean `bot` flag that is `False`,
and `deleted is not True`. The defect was therefore purely in *when and how
many* entities were resolved — not in the security rules.

## 2. Exact before / after behavior

### Keepalive

| Situation | Before | After |
|---|---|---|
| `get_me()` raises `FloodWaitError(120)` | `KEEPALIVE_FAILED`, reconnect scheduled | `KEEPALIVE_FLOODWAIT — … keeping the connection, NOT reconnecting`; monotonic deadline = now + 120s (clamped to 1h); no reconnect |
| Next probe inside the wait | another `get_me()` (and often another FloodWait/reconnect) | **no `get_me()` call at all**; `KEEPALIVE_FLOODWAIT_SKIP` trace at debug; probe returns `floodwait_skip` |
| Wait expires | n/a | next interval probes normally |
| RPC timeout | reconnect | unchanged (reconnect) |
| Any other exception (connection/session) | reconnect | unchanged (reconnect) |
| Cancellation | re-raised | unchanged; no dedicated wait task exists, so cancelling the keepalive task during the suppressed interval propagates from the ordinary loop sleep |

### Private message scope

| Situation | Before | After |
|---|---|---|
| Group / channel / unknown chat type / outgoing / contradictory ids / missing message id | `ensure_loaded_async()`, then **two** entity getters | pure identity prefilter returns before configuration load and before **any** getter |
| Genuine direct private chat | `get_chat()` **and** `get_sender()` (2 RPCs) | exactly **one** authoritative `get_sender()`; the same verified `User` is passed as both chat and sender entity |
| First getter fails (non-FloodWait) | second getter still called | no fallback RPC; rejection (`sender_unresolved`) |
| Getter raises FloodWait | logged per update, retried next update | deadline recorded (bounded 1–3600 s), **no second getter**, one warning; subsequent messages return `entity_floodwait` with zero RPC |
| Wait expires | n/a | resolution resumes; enforcement behaves exactly as before |
| Bot / deleted / non-User / mismatched id | rejected | rejected (classifier unchanged in substance) |
| Duplicate / redelivered message | enforced once | unchanged (`already_handled` mark before actions) |

## 3. Files changed

| File | Change |
|---|---|
| `backend/runtime/keepalive.py` | explicit `FloodWaitError` branch, bounded monotonic `_floodwait_until`, `floodwait_remaining()`, `_probe()` split out for deterministic testing; timeout/exception reconnect paths preserved |
| `backend/services/private_message_service.py` | `identity_rejection()` (pure prefilter, one source of truth also used by `classify_scope`), `SCOPE_ENTITY_FLOODWAIT`, `note_entity_floodwait()` / `entity_floodwait_remaining()` / `clear_entity_floodwait()`, reset on `reset_in_memory()` |
| `backend/bot/handlers/private_message.py` | zero-I/O prefilter before config load; FloodWait deadline recording in `_resolve_entity`; single authoritative `get_sender` lookup; suppression check before any RPC |
| `tests/test_keepalive_floodwait.py` (new) | 7 regression tests |
| `tests/test_private_message_scope.py` | `_event` records which getters ran and can raise `FloodWaitError`; 10 new tests; 2 existing handler tests updated to the single-authoritative-lookup contract (classifier-level unresolved-entity assertion kept) |
| `TELEGRAM_RPC_FLOODWAIT_REPORT.md` (new) | this report |

No other file was modified. `TOOL_CALLING_ROOT_CAUSE_INVESTIGATION.md` and
`IMPLEMENTATION_REPORT.md` are untouched historical records.

## 4. FloodWait handling policy

1. **Classification** — `FloodWaitError` is a rate limit, never a connection
   verdict. It is caught before the generic `Exception` branch in both the
   keepalive and entity resolution.
2. **No reconnect** — a FloodWait never calls `_trigger_reconnect()`, never
   touches the recovery lock, and never disconnects the client.
3. **No retry during the wait** — the deadline is monotonic
   (`time.monotonic()`), process-local, and checked *before* the RPC. Keepalive
   skips its probe; entity resolution returns an out-of-scope verdict.
4. **Bounded** — `1 <= wait <= 3600 s`; a server value of a day is re-probed
   hourly rather than silencing the feature for a day. A shorter later value
   can never shorten an active deadline (`max()`).
5. **No blocking / no new task** — nothing sleeps for the wait duration. The
   keepalive keeps its existing 60 s loop sleep; the message path performs no
   sleep at all and creates no task or scheduler.
6. **Cancellation-safe** — `asyncio.CancelledError` is re-raised in both
   paths; there is no inner `wait_for` around the deadline.
7. **Structured, transition-based logging** — one
   `KEEPALIVE_FLOODWAIT` / `PRIVATE_MESSAGE_TRACE stage=entity_floodwait`
   warning per observed wait; suppressed probes log at debug/trace, not one
   warning per incoming update.
8. **Preserved recovery** — timeout and genuine RPC/connection exception
   branches, recovery lock checks, cooldown, `guarded_create_task` usage and
   single-run-loop ownership are unchanged.

## 5. Entity-resolution optimization and its security proof

**Optimization.** In a direct private chat, Telegram's peer, chat and sender
are the same user: the prefilter proves
`peer_user_id == chat_id == sender_id` (all positive, not the owner) and
`is_private is True`, `is_group is False`, `is_channel is False`, with a
positive message id. Under those facts, `get_sender()` returns the one
authoritative `types.User` for that id; passing it as both `chat_entity` and
`sender_entity` to the unchanged classifier cannot lose a check, because every
entity check is a function of that same user id.

**Proof obligations, all still enforced:**
* real `types.User` required — handler passes
  `chat_is_user = sender_is_user = isinstance(entity, types.User)`; a non-User
  entity is rejected (`not_private_chat`).
* entity id must match — classifier compares the entity's `id` to `chat`
  twice; the single entity must equal both.
* bots rejected — `_bot_flag` must be a real `bool`; `True` → `bot_sender`;
  a missing/non-bool flag → `sender_type_unknown` (never assumed human).
* deleted users rejected — `deleted is True` → `deleted_sender`.
* malformed / contradictory — prefilter rejects missing ids, non-positive
  ids, bool-as-id, mismatched peer/chat/sender, and the owner's own chat
  before any RPC; the classifier keeps the same checks for direct callers.
* unresolved entities never eligible — a `None`/exception/FloodWait result is
  a rejection, never a weaker check and never a second getter.
* no policy action on an unverified event — `enforce_incoming` returns `True`
  (message continues through normal handlers) for every rejection; the
  unchanged action path only runs after `verdict.eligible`.
* exactly-once — the `(chat_id, message_id)` mark is still registered before
  actions and released only when actions never ran.

**Why `get_sender` (not `get_chat`).** The production symptom was
`stage=get_sender_unresolved error=FloodWaitError`, i.e. the sender lookup is
the RPC that floods. The sender entity is also the one whose bot/deleted flags
the classifier trusts. The chat getter is no longer called at all, so its
failure can no longer trigger a second RPC; the classifier-level
`chat_unresolved` assertion is retained for direct callers and covered by a
test.

## 6. Cancellation ownership findings (investigation only — no code change)

Verified against Telethon **1.43.0** installed in this workspace:

| Fact | Evidence |
|---|---|
| `run_until_disconnected()` does **not** own the update loop; it awaits the client's disconnected state | `telethon/client/updates.py:65` |
| The update loop is a separate client-owned task created at connect time | `telethon/client/telegrambaseclient.py:619` (`_updates_handle = loop.create_task(_update_loop())`) |
| Each dispatched update runs in its **own** task, tracked in `client._event_handler_tasks` | `telethon/client/updates.py:289-291` |
| `client.disconnect()` **cancels and awaits** those per-update handler tasks | `telethon/client/telegrambaseclient.py:748-753` |
| `RuntimeSupervisor._stop_run_loop()` cancels only `self._run_task` (`lifeos-run`) | `backend/runtime/supervisor.py:564-572` |
| The AI request is awaited inside the Telethon handler coroutine — no detached AI task owns it | `backend/bot/handlers/ai_unified.py:1014` handler → `:770` `await asyncio.wait_for(engine.execute(...))` → `backend/ai/engine/dispatcher.py:300` `await self._provider_manager.chat(...)` |

**Conclusion.** Cancelling `lifeos-run` (the `TASK_CANCELLED task=lifeos-run`
line) cannot by itself cancel an in-flight AI request: `run_until_disconnected`
is not the parent of the update-dispatch tasks, and `_stop_run_loop` does not
touch `client._event_handler_tasks`. However, `_trigger_reconnect()` calls
`client.disconnect()` while holding the recovery lock, and Telethon's
`disconnect()` explicitly cancels every in-flight event-handler task — which is
the coroutine awaiting the provider. That is a **real, source-confirmed
mechanism** by which a FloodWait-driven reconnect can surface as
`AI_PROVIDER_CALL_CANCELLED ... call=continuation_1`.

It is **not** proven that this is what happened in production: the logs are
adjacent in time, not causally linked, and this phase did not reproduce a
disconnect during a continuation. Fixing keepalive's FloodWait handling removes
one trigger of that disconnect, but it does not repair or explain the AI
cancellation itself. That requires a separate investigation (task identity at
cancel time, whether the cancel came from `disconnect()` or from the provider
timeout, and how continuations should behave across a reconnect). No shielding,
detached task, or lifecycle change was introduced here.

## 7. Tests run and exact results

| Command | Result |
|---|---|
| `pytest tests/test_keepalive_floodwait.py -q` | **7 passed** |
| `pytest tests/test_private_message_scope.py -q` | **125 passed** |
| `pytest tests -q` (full suite) | **7056 passed, 27 skipped, 1 failed**, 144.05 s |
| `python -m py_compile` on the three changed modules + two test files | exit 0 |
| `git diff --check` | clean |

New keepalive coverage: FloodWait does not reconnect; next probe suppressed
until expiry; suppression expires; wait bounded even for 999999 s; genuine
connection error reconnects; timeout reconnects unchanged; disconnected client
not probed; cancellation during the suppressed interval propagates.

New private-scope coverage: zero entity lookups for group/channel/unknown/
outgoing/contradictory/message-less events; exactly one authoritative lookup
for a valid private message; first failure does not cause a second RPC;
FloodWait suppresses further lookups and actions; suppression expires and
resolution resumes; deadline bounded/monotonic; one FloodWait warning for six
messages; classifier still rejects an unresolved chat entity.

### Pre-existing failure (not caused by this change)

`tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`
fails on its own with `Task 1 was not scheduled: interval advancement exceeded
bound` (`assert 0 == 1`). No scheduler file is in this change
(`git diff --name-only` contains no `task_scheduler`); the failure is the
known date-bound scheduler test and was deselected in the previous phase's
full-suite run for the same reason.

## 8. Not verified

* No live Telegram session, no API credentials, no production log capture, and
  no Render shell access. The real FloodWait durations, the real entity-cache
  behaviour of the production `StringSession`, and the exact production
  response to entity lookups are therefore unverified.
* The cancellation mechanism in §6 is proven from Telethon/source structure,
  **not** reproduced from production logs; no claim is made about the actual
  cause of the observed AI cancellation.
* The Glass UI was not opened; only the handler/classifier and keepalive logic
  are exercised (in-process).
* No live Supabase; the private-message tests use the existing fake store.

## 9. Remaining risks

1. **Fail-open during suppression.** While the entity-resolution FloodWait
   deadline is active, private-message enforcement does not run for any
   sender (no delete, no reply, no AI). This is the existing behavior for an
   unresolved scope and is bounded to at most one hour; it is logged once at
   warning and at debug per suppressed event. If a shorter, per-chat policy were
   ever required, it would need a design change (out of scope).
2. **`get_sender` remains the fallible call.** Using the sender as the single
   authoritative entity means the RPC that flooded in production is still
   attempted once after each deadline (not during it). A future change could
   prefer a cached `get_chat` when Telethon's peer cache is warm, but that is a
   model-ID/entity-cache investigation, not proven here.
3. **Hourly re-probe of long waits.** A FloodWait longer than 3600 s is
   re-probed hourly; each re-probe may log one warning and extend the deadline.
   This is deliberate (bounded) and does not reconnect.
4. **Other RPC errors still reconnect.** Only `FloodWaitError` is exempted from
   the keepalive reconnect path; 5xx/transport/session errors keep their
   previous behavior. If a non-FloodWait server-side rate limit exists, it
   would still reconnect — not observed in the logs, not addressed here.
5. **Cancellation of AI continuations remains unexplained.** Removing the
   FloodWait→reconnect trigger reduces one path to `disconnect()`, but the AI
   cancellation needs its own investigation; this phase does not claim the
   “AI Rate limited” issue is fixed.
