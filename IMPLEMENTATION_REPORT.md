# IMPLEMENTATION_REPORT.md — Private Message Management, Phase 2

> This file describes ONLY the current state of the repository after this task. It
> **replaces every previous report in full**. The Phase-1 report ("Private
> Message Management — Phase 1, panel-first, no AI", commit `bf89042`), the emoji
> implementation (`977247e`), the checkpoint-2 fixes (`e13deee`, `9633c0b`), the
> via-bot feasibility study (`b04cb41`), the `98c2825` audit, the boundary-tracing
> task (`a54877b`), the callback-receipt task (`1bd843d`), the premium-emoji
> inline verification (`39de9cc`, `b463552`, `219c873`, `2eaaf93`) and the LifeOS
> guide website (`58aebac`) are preserved in git history and referenced here only
> by commit id.

| Item | Value |
|---|---|
| Task | Private Message Management — Phase 2: the hard human-private scope, independent actions, dedicated persistence and the AI response |
| Project | LifeOS Telegram self-bot (`Onlyicing1/Telegram-self-bot`) |
| Branch / base revision | the task branch **based on `origin/main` = `bf89042`** (the Phase-1 commit) |
| Outcome | **IMPLEMENTED + unit-TESTED offline.** A strict entity-proven scope gate, three independent closing actions with a deterministic order, dedicated tables with a one-time backfill of the Phase-1 document, and an AI reply with an enforced per-person allowance. |
| Supabase | **NOT touched — no SQL was executed, no database was modified.** The migration and the §31.3 part-10 handoff are WRITTEN, NOT APPLIED. |
| Live Telegram verification | **No** (no live session in this workspace); see §6. |

---

## 1. Findings from the source inspection

1. **The previous task's implementation was the true starting point.** The base
   commit already contained Phase 1 (`backend/services/private_message_service.py`,
   `backend/bot/handlers/private_message.py`, `backend/helper/symbols.py`, the
   `pm_*` panel family, `tests/test_private_message_management.py`,
   `tests/test_private_message_panel.py`), and the Phase-1 report explicitly
   deferred "AI integration" to **Phase 2**. The stage number used here is that
   one, not an invented one.
2. **`event.is_private` alone is not a scope check — it is the defect.**
   Telethon's `EventCommon.is_private` is `isinstance(self._chat_peer, PeerUser)`
   (verified against the installed **Telethon 1.34.0** source), so **a private
   chat with a BOT passes it**. The same source shows `is_group` is `PeerChat`
   or a non-broadcast `PeerChannel` (so a supergroup and a linked discussion
   group pass too) and `is_channel` is `PeerChannel` (true for a broadcast
   channel AND for every megagroup). The authoritative bot signal is the
   sender/channel entity's own `bot` boolean on a resolved `User` — never a
   name, username, numeric id or message text.
3. **Phase 1's scope fallback was weaker than its primary check.**
   `_is_private_event` fell back to "positive `chat_id` equal to `sender_id`"
   when `is_private` was not a bool — exactly the weaker check the task forbids.
   A `chat_id == sender_id` pair is also true for a bot dialog.
4. **The action model was mutually exclusive.** One `policy` value
   (`ignore` / `delete` / `autoreply`) chose ONE action, so delete + fixed
   response + AI could not be combined; the auto-reply also carried a 5-minute
   **in-memory** cooldown, which a restart reset (the task forbids relying on
   in-memory quota state).
5. **The persistence was a generic JSON document.** The Phase-1 state lived in
   ONE `bot_settings` row (`key = 'private_message_management'`). The task
   requires dedicated persistence with owner scoping, constraints and a safe
   backfill — so the state moved into dedicated tables while the legacy document
   is preserved as the one-time import source.
6. **The AI boundary that had to be reused.** The engine's conversation runtime
   is single-session-per-owner, so calling `Engine.execute` for a closed-PV reply
   would inject the owner's assistant history into the prompt and write this
   reply into that history — a direct violation of the context-isolation rule.
   The repository already has the sanctioned out-of-band generation pattern
   (`backend/ai/task_execution.py`: the shared ProviderManager with
   `tools=[]`), and this feature follows it exactly.

---

## 2. Implemented changes

### 2.1 The scope gate (the acceptance criterion)

`backend/services/private_message_service.classify_scope(...)` is the ONE gate;
`backend/bot/handlers/private_message.resolve_scope(...)` gathers the event's own
peer/flags and resolves BOTH entities through the existing self client
(`get_chat()`, `get_sender()`), and `enforce_incoming` consults the gate FIRST.
The verdict requires, in order: the owner id is a real user id; `is_group is not
True`; `is_channel is not True`; `is_private is True` with **both** group flags
explicitly `False` (an unknown `None` fails closed); a real `types.PeerUser`
peer; positive `chat_id`, `sender_id`, `message_id`, `peer.user_id` that all
agree; the chat is not the owner's own id; both entities resolved and each
independently proven to be a `types.User` with a matching id and a **real
boolean** `bot` flag that is `False`; and no `deleted` flag. Rejections are
named (`bot_sender`, `group_conversation`, `channel_conversation`,
`chat_type_unknown`, `not_private_chat`, `inconsistent_event`, `chat_unresolved`,
`sender_unresolved`, `sender_not_a_user`, `sender_type_unknown`,
`deleted_sender`, `own_account_chat`) and **no action of any kind follows them**.
No configuration can widen the boundary.

### 2.2 Independent actions and their deterministic order

`delete_incoming`, `fixed_response_enabled` and `ai_response_enabled` are three
independent booleans (own column, own control, own write); enabling one never
enables another. The order is fixed and tested: **delete → fixed response → AI
response**. A delete failure is recorded and does NOT suppress the responses; a
fixed-response failure never suppresses the AI reply; the fixed response has its
own explicit switch and its own text input.

### 2.3 Dedicated persistence with a safe backfill

`private_message_config` (ONE owner-scoped row: master switch, `mode`, the three
action flags, `fixed_response_text`, `ai_max_replies_per_person`,
`ai_unlimited_replies`, `ai_interval_seconds`, plus the access state the panel
already edited — `allowlist`, `exceptions`, `temporary`, `schedule`) and
`private_message_senders` (durable per-person counters, `PRIMARY KEY (owner_id,
sender_id)`). One index, RLS, SELECT-only `anon`/`authenticated` policies,
comments, `NOTIFY pgrst`, verification queries and a read-only legacy probe.
The service writes the whole document through the dedicated row and **imports the
Phase-1 document exactly once** when that row does not exist yet (the legacy
single `policy` value maps onto the independent flags; `enabled`, `mode`,
`allowlist`, `exceptions`, `temporary` and `schedule` carry over unchanged), so
nothing previously configured is discarded and the legacy row is never modified.

### 2.4 AI response, disclosures and the per-person allowance

ONE generation through the shared `ProviderManager` (`tools=[]`) with a message
list of exactly `[system contract, incoming text]` — no history, no memory, no
reply context, no identifiers, no names — and the two required disclosures are
appended by the APPLICATION after generation (plain Unicode, never emoji), so the
model can neither remove nor override them:

```
این پی‌وی در حال حاضر بسته است.
این پیام توسط هوش مصنوعی تولید و ارسال شده است.
```

The allowance is claimed by ONE conditional (CAS) increment BEFORE generation and
released by a compensating CAS when nothing was delivered, so only a DELIVERED
reply consumes it; a finite max, an explicit unlimited option and an interval
(including `0`) are all supported, per person, durable across restarts, with the
owner's explicit reset as the only clearing edge. A per-person lock plus an
exact `(chat, message)` duplicate guard make concurrent and duplicate updates
unable to spend one allowance twice.

### 2.5 Panel

`Menu → Private Messages → Closing actions`: three independent toggles, the fixed
text input, max-replies input, interval input, unlimited toggle, the live counter
summary and the reset action, plus a scope statement ("genuine human users in
direct private chats only; bots, groups, supergroups, channels, channel posts and
linked discussion groups are never affected"). Every glyph comes from
`backend/helper/symbols.py` — no emoji literal is introduced. Every action
reports what was actually persisted, and a refused write says so.

---

## 3. Tests and validation actually executed

| Command | Result |
|---|---|
| `python3 -m pytest tests/test_private_message_scope.py -q` | **62 passed** |
| `python3 -m pytest tests/test_private_message_management.py tests/test_private_message_panel.py -q` | **85 passed** |
| `python3 -m pytest tests/test_canonical_schema_reconciliation.py tests/test_database_setup_order.py tests/test_symbols.py -q` | **87 passed** |
| `python3 -m pytest tests/ -q` (full suite) | **6199 passed, 41 failed, 27 skipped** — every failure is the pre-existing `tests/test_colored_buttons.py` environment mismatch (the repo pins `telethon==1.43.0`, this sandbox has **1.34.0**, and 1.34.0 rejects the inline-button `style` field: `Button style 'primary' rejected by Telethon`). No private-message, schema or symbols test fails. |
| `python3 -m py_compile` on every changed/added Python file | clean |
| `git diff --check` | clean |

What the new suite proves through the REAL enforcement entry point (the existing
`task_events` handler → `enforce_incoming`), with real Telethon entities and a
recording Telegram spy: a human private message is eligible and runs the enabled
actions; a bot in a private chat is rejected with **every** action enabled and
unlimited, and touches no delete/send/AI/counter path; groups, supergroups,
channel posts and linked discussion groups are never touched; a missing chat or
sender entity, an entity that is not a `User`, a missing/non-boolean `bot` flag
and a deleted account are rejected; `get_chat()`/`get_sender()` raising is
rejected; a disagreeing peer/sender/chat id, an outgoing message, the owner's own
chat and an invalid owner are rejected; a message-less event is rejected;
repeated rejected events consume nothing (a following human message still gets
its first allowance); the delete/fixed/AI order is asserted; the finite limit,
the unlimited option, a `0` interval and a positive interval all behave as
configured and per person; a failed generation or delivery gives the claim back;
an unreadable counter store fails closed; duplicate and concurrent messages are
enforced once; the counters survive a restart and only the explicit reset clears
them; the AI payload is exactly two messages with `tools=[]`, no identifiers and
no display names, and the two disclosures are appended; the module adds no second
client, loop, scheduler or executor; the migration is additive and creates
exactly the two tables whose columns cover every configured setting.

**Not verified:** no test, and no part of this task, executed SQL, contacted
Supabase, started a live Telegram session or made a live provider call.

---

## 4. Confirmation: bots, groups, supergroups and channels are excluded

| Conversation / sender | Gate verdict | Actions performed |
|---|---|---|
| Genuine human, direct private chat | `eligible` | as configured |
| **Bot account, direct private chat** | `bot_sender` | **none** (proved with every action enabled + unlimited) |
| Group (`PeerChat`) | `group_conversation` | none |
| Supergroup / megagroup | `group_conversation` | none |
| Linked discussion group | `group_conversation` | none |
| Channel post (broadcast) | `channel_conversation` | none |
| Unknown chat type / flag | `chat_type_unknown` | none |
| Peer that is not `PeerUser` | `not_private_chat` | none |
| Missing chat/sender entity | `chat_unresolved` / `sender_unresolved` | none |
| Entity resolution raised | `chat_unresolved` / `sender_unresolved` | none |
| Sender is not a `User` entity | `sender_not_a_user` | none |
| `bot` flag missing or non-boolean | `sender_type_unknown` | none |
| Deleted account | `deleted_sender` | none |
| Contradictory ids / outgoing / own chat | `inconsistent_event` / (skipped) / `own_account_chat` | none |

For every rejected row the suite asserts zero deletions, zero sends, zero
generation calls and zero counter rows, **including with the feature enabled and
all three actions on**.

---

## 5. Database migration status and the manual SQL required

* **Status: WRITTEN, NEVER EXECUTED.** `supabase/migrations/20260929000001_create_private_message_tables.sql`
  exists and is embedded verbatim as **part 10 of 10** of the ONE setup block in
  `DATABASE_ARCHITECTURE.md` §31.3; §33 documents the tables, the claim/compensate
  contract, the reset policy, the backfill, the security model and the reversal.
  **No SQL was run, no Supabase connection was opened and no database was
  modified by this task.**
* **Behaviour until it is applied:** the configuration read degrades to "no row",
  which the service treats as **disabled** — nothing is deleted, no response is
  sent, no AI request is made, no counter is consumed, and the panel reports the
  refused write honestly. The feature is INERT, never partially active.
* **Manual action (owner, Supabase SQL Editor as `postgres`):** run the §31.3
  block (part 10 carries this migration's statements). The block's own queries
  verify both tables and their columns; the final read-only probe
  (`SELECT key FROM bot_settings WHERE key LIKE 'private_message%'`) shows the
  Phase-1 document, if the owner ever had one, which the application then imports
  on first read (nothing to backfill by hand).
* **Rollback SQL (manual, never executed):**

```sql
-- Manual Supabase SQL — NOT EXECUTED BY AI. Destroys this feature's
-- configuration and counters only; the Phase-1 bot_settings document survives.
DROP TABLE IF EXISTS private_message_senders;
DROP TABLE IF EXISTS private_message_config;
```

* **Backfill:** every Phase-1 value survives (mapped as described in §2.3); no
  rule is discarded and the legacy document is left untouched.

---

## 6. What remains unverified / deferred / blocked

1. **NOT VERIFIED LIVE.** The gate, the action order, the allowance and the panel
   were proven offline only. Whether live Telegram always delivers the entities
   the gate needs (a resolution failure fails closed BY DESIGN and would make the
   feature silent for that event) can only be confirmed on the live account.
2. **BLOCKED on the owner:** applying the §31.3 block (part 10); until then the
   feature is deliberately inert.
3. **DEFERRED:** per-person allowlists/exceptions beyond the existing lists, and
   any media/caption-based AI response (a message without text still gets the
   delete/fixed actions, and the AI step consumes nothing).
4. **Known limitation (documented, not hidden):** the release after a failed
   generation/delivery is itself a CAS write; if that compensation fails (the
   store goes down between the claim and the failure) the claim stays consumed
   and the service logs it honestly rather than answering twice.
5. **PRE-EXISTING, UNRELATED:** `tests/test_colored_buttons.py` (40 tests) fails
   in this environment because the installed Telethon is 1.34.0 while the
   repository pins 1.43.0; this task neither caused nor addressed it.

---

## 7. Files changed and why

```
backend/services/private_message_service.py                       edit — scope gate, independent action flags,
                                                                        AI config + disclosures, dedicated
                                                                        persistence with the legacy import,
                                                                        durable claim/release/reset wrappers
backend/bot/handlers/private_message.py                           edit — entity-proven gate, deterministic
                                                                        action order, AI reply through the
                                                                        existing ProviderManager, reworked panel
backend/db/client.py                                              edit — dedicated config read/write, legacy
                                                                        document read, CAS claim/release/reset,
                                                                        counter listing + in-memory fallbacks
supabase/migrations/20260929000001_create_private_message_tables.sql new — the two tables (WRITTEN, NOT APPLIED)
tests/test_private_message_scope.py                               new  — the scope/action/limit/failure regressions
tests/test_private_message_management.py                          edit — the new contract (actions, dedicated row,
                                                                        legacy import) with Phase-1 mode/schedule coverage kept
tests/test_private_message_panel.py                               edit — the real entities, the actions panel, the
                                                                        bot/unresolved-sender enforcement cases and the
                                                                        AI disclosure test
tests/test_canonical_schema_reconciliation.py                     edit — the two tables classified as successor tables
tests/test_database_setup_order.py                                edit — ten-part block, part-10 verification, counts
DATABASE_ARCHITECTURE.md                                          edit — §33, the part-10 embed, audit/status updates
ROADMAP.md                                                        edit — §0 for the current feature (Emoji record preserved)
IMPLEMENTATION_REPORT.md                                          edit — this report (replaced in full)
```

No unrelated refactor, no other feature's behaviour changed, no dependency added,
no environment variable added, no second client/loop/scheduler/executor, and no
destructive or unrelated command run.

---

## 8. Commit, push and remote verification

| Step | Status |
|---|---|
| `git diff --check` | Clean |
| Python compilation of every changed file | Clean |
| Focused suites | 62 + 85 + 87 passed |
| Full offline suite | 6199 passed, 27 skipped, 41 pre-existing colored-button failures (Telethon version) |
| Commit | ONE commit for this task (all of the files above) |
| Push | To `origin/main` (the repository connected to this workspace) |
| Remote verification | `git fetch origin` → `git rev-parse origin/main` → `git merge-base --is-ancestor <sha> origin/main` |

A commit cannot contain its own SHA: the commit carrying this report is the
task's single commit, created and pushed in this task, and its SHA is verified
against `origin/main` with the three commands above. Its parent is
`bf890423cb7450ed4d4733c715d545103b48559c` (the Phase-1 implementation this
report replaces). The stale local implementation written before this task
realised the true base (`pm-phase2-local-full`) was NOT pushed.

---

## 9. The exact next test the owner must run after deployment

1. Apply the §31.3 block in Supabase (part 10 creates the two tables). Without it
   the feature is inert.
2. Deploy `main`, open Telegram, send `Menu` in **Saved Messages**, open
   **Private Messages → Closing actions**, and set the mode to **Closed**.
3. Enable **Delete incoming message** only, then send a direct message from a
   **second human account**. Expected: that message is deleted and nothing is
   sent back.
4. Enable **Send fixed response** and set its text. Repeat step 3. Expected: the
   message is deleted AND the text arrives.
5. From a **bot account** (any bot's private chat — still a private chat). Send a
   message. Expected: **nothing** happens at all, and no counter moves.
6. In a **group**, in a **supergroup** and in a **channel** where the owner is a
   member, post a message. Expected: nothing happens.
7. Enable **Send AI response** with **max replies per person = 2** and
   **interval = 0**; send three direct messages from one human account. Expected:
   the first two get an AI reply ending with both disclosures, the third is
   refused by the limit, and the counter line shows 2 used by 1 person.
8. Tap **Reset AI counters** and send one more message: the AI answers again.

If a step behaves differently, the log line `PRIVATE_MESSAGE_TRACE` names the gate
verdict (`stage=out_of_scope reason=…`) or the enforcement step
(`stage=unauthorized … actions=delete:…,fixed:…,ai:…`, `stage=ai_denied
reason=…`), so the exact boundary that applied is always identifiable.
