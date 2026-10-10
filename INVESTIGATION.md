# Investigation — Premium Emoji Inline Flow: the First Missing Execution Boundary (2026-10-10)

Canonical, latest-only record of the investigation into why the owner's live
tests of the Premium custom-emoji inline flow produced **no `[PREMIUM_INLINE]`
lines in Render logs**. This document replaces the previous investigation
record in full. Earlier records — the via-bot feasibility study (`b04cb41`),
the implementation record (`977247e`), the checkpoint-2 fix record
(`e13deee`), and the 98c2825 audit — remain readable in git history and are
referenced here only by commit id.

| Item | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Branch | `main` |
| Revision this record was produced from | `98c2825` (verified equal to `origin/main` at the start of this task) |
| Question | Why did the live tests show no `[PREMIUM_INLINE]` logs, and which execution boundary is the first one not reached? |
| Verdict | **The first missing boundary is the reply dispatch (`_input_listener` → `_premium_inline_reply_handler`), established by deduction: every dispatched reply always emits exactly one first `[PREMIUM_INLINE]` line, so zero such lines means no reply ever reached the handler. No production-code defect exists on the path before that boundary; the specific state gate that stopped the reply in those tests is not established by the available evidence (each gate has its own log signature — §9).** |
| Production code changed | **None** (nothing was justified by the evidence). Two focused tests were added to pin the boundary. |
| Live Telegram verification | **Not performed in this task** (no live session in this workspace). The source-level trace is complete and test-pinned; the live failure's exact trigger remains open. |

---

## 0. Status of every claim

| # | Statement | Status |
|---|---|---|
| 1 | `[PREMIUM_INLINE]` lines come from exactly ONE place (`premium_emoji_inline_service._trace`) and can never be emitted by the ✨ button click | **CONFIRMED** — source: `_trace` at `premium_emoji_inline_service.py:118`; only caller chain is `_premium_inline_reply_handler` → `send_premium_emoji_via_inline` |
| 2 | Every reply that passes the handler's target checks emits exactly one first `[PREMIUM_INLINE]` line (`SOURCE_ENTITY_FOUND found=False` / `found=True, usable=False` / `reason="span"` / `SOURCE_ENTITY_VALIDATED`) | **CONFIRMED** — `inspect_source_message` is called unconditionally at `emoji.py:2268` and traces on all four branches (service lines 208/210/223/236); pinned by the new test `test_the_real_pending_input_listener_reaches_the_production_flow` |
| 3 | Zero `[PREMIUM_INLINE]` lines across the tests ⇒ no reply reached `_premium_inline_reply_handler` | **CONFIRMED** — deduction from #2 |
| 4 | Why the reply did not reach the handler in those tests (120 s state expiry / cleared by another panel action / wrong chat / reply never sent / click never armed) | **UNRESOLVED** — the excerpt distinguishes none of these; §9 gives the line that settles each |
| 5 | The ✨ button was clicked in the observed window | **NOT ESTABLISHED** — `last_callback_age` names no button; §4 |
| 6 | The callback receipt/dispatch is logged before the router/handler | **CONFIRMED (negative)** — receipt writes a health timestamp only; the router's INCOMING/DISPATCH lines are `debug_callbacks`-gated (default false); §4 |
| 7 | The task-repository `ReadError` burst can interrupt the premium path | **REFUTED** — not on the path; the failure is caught inside the repository and degrades to the in-memory fallback; §5 |
| 8 | The `provider:chat:nararouter` timeout is related to the missing logs | **REFUTED** — different client, different consumer, bounded and converted to a structured response; §6 |
| 9 | The `PeerChannel` warning caused the missing logs | **REFUTED** — it is a helper-bot panel-edit failure that runs AFTER the traces; it can only hide early-exit feedback; §7 |
| 10 | Checkpoint-2 reads `send_message` (the `e13deee` fix) in the current source, with tests | **CONFIRMED** — service 567–574; tests at `test_premium_emoji_inline.py:404/468/516`; §8 |
| 11 | The 2026-10-10 `INLINE_RESULT_ENTITY_MISSING` run (pre-fix deploy) was a false positive of the old field read | **CONFIRMED (prior finding, unchanged)** — the deploy that produced it predates `e13deee`; §8 |
| 12 | Telegram's live treatment of the entity on the inline path (after the fix) | **UNVERIFIED** — requires a live re-run; §10 |

---

## 1. The reported evidence

### 1.1 The live tests

The owner tested the feature (Menu → Emoji → ✨ Send Premium Emoji → reply to
the Saved Messages selection message with a genuine Premium emoji) while
monitoring Render's live logs. **No `[PREMIUM_INLINE]` logs appeared in the
provided excerpts.** Three tests were performed; no re-test was requested for
this task.

### 1.2 The 15:32–15:33 UTC excerpt (2026-10-10)

* ~15:32:46 — `Task-3898` was in Telethon's `_dispatch_update`.
* ~15:32:46 — `last_callback_age` ≈ 12 s (⇒ some callback query arrived on the
  helper client at ~15:32:34).
* ~15:32:57–15:33:07 — task-repository queries repeatedly failed with
  `ReadError: [Errno 11] Resource temporarily unavailable`.
* ~15:33:11 — the AI provider operation `provider:chat:nararouter` timed out
  after 30 s.

---

## 2. The complete execution path, boundary by boundary

The premium flow has exactly two live entry points: the ✨ button (helper-bot
callback) and the owner's reply (self-client message). Everything between them
is the pending-input machinery. The table lists every boundary, the code that
owns it, the log line it unconditionally guarantees, and its silent failure
modes.

| # | Boundary | Code (file — function/line) | Unconditional marker | Silent failure modes |
|---|---|---|---|---|
| B1 | Callback delivery to the helper client | `helper/client.py:116` `register_helper_hooks` → `_helper_callback_hook` (123) | **none** — `set_last_callback()` + `set_last_event_dispatch()` (health timestamps only) | delivery failure is invisible (only the stale age) |
| B2 | Router entry + pre-session gates | `helper/panels.py:325` `register_callback_handlers` → `_callback_router` (329); `resolve_callback_message` (87) | **none** until session resolution — INCOMING (347), REJECTs (351–379), DISPATCH (385) are all `if debug:` gated (`settings_service.py:76`: `debug_callbacks` default **false**) | unresolvable coordinates, non-owner, empty data — all rejected with no log |
| B3 | Session resolution + action dispatch | `panels.py:285` `_resolve_session`, `panels.py:512` `_handle_action` | `[CALLBACK] session lookup OK by (chat_id=…, msg_id=…) → session_id=…` (290) or the unconditional warning `[CALLBACK] session lookup FAILED: … callback will be dropped` (317); then `[CALLBACK] _handle_action: action_id='emoji_premium_inline' extra=''` (517) | a stale panel session (created before a restart) drops the callback at (317) |
| B4 | Selection message + arming | `emoji.py:2147` `_premium_inline_action`; `send_message("me", …)` (2157); `set_pending` (2178) | `[EMOJI_UI] premium inline: selection message #N sent to Saved Messages` (2186) | send exception → warning (2159) + error panel; unusable returned id/chat → error panel with **no log** (2167–2177) |
| B5 | Reply dispatch (the pending-input listener) | `inline_sender.py:75` `register_input_listener` → `_input_listener` (78) | **none** — silently returns on: not owner (80), no/expired pending (82; the 120 s state expiry at `input_state.py:24,63–67` logs `Input for owner … expired` when it fires), different chat (86), text starts with `.` (90) | any panel action/panel navigation before the reply clears the pending entry first (`panels.py:437/464/482/528`); a new `set_pending` replaces it |
| B6 | Reply handler target gates | `emoji.py:2200` `_premium_inline_reply_handler` | **none** — silent-by-design early exits: self client absent (2208), unknown selection id (2211), reply unreadable (2228/2230 log `[EMOJI_UI] premium inline: cannot read the reply`), cross-chat reply (2239), not a reply (2247), wrong target id (2254) | the panel edits that would SHOW these reasons go through `_edit_inline` (1957) → the helper bot (1965–1969) and can themselves fail (1971) — see §7 |
| B7 | **First `[PREMIUM_INLINE]` line** — source inspection | `emoji.py:2268` → `premium_emoji_inline_service.py:194` `inspect_source_message` | exactly one of `SOURCE_ENTITY_FOUND found=False` (208), `found=True, usable=False` (210), `…reason="span"` (223), or `SOURCE_ENTITY_VALIDATED` (236) | none — this line is unconditional for every dispatched reply |
| B8 | Full service pipeline (checkpoint 2 + send + read-back) | `premium_emoji_inline_service.py:704` `send_premium_emoji_via_inline`; `_inspect_inline_result` (549, reads `send_message`); `_send_result` (615); `_fetch_stored_message` (629); `_read_back` (650) | `SEND_STARTED` (730/733/742/766/792), `OUTBOUND_PAYLOAD_BUILT` (298/780), `INLINE_QUERY_STARTED` (803), `INLINE_RESULT_INSPECTED` (573/585/603), `INLINE_SEND_STARTED/ACCEPTED` (871/874/884), `READBACK_RESULT` (671/690/897), `DIAGNOSIS` (855/905) | none before B7; a mid-flow timeout (`_INLINE_CALL_TIMEOUT_S`=45 s, `_READBACK_TIMEOUT_S`=30 s) is converted to a structured failure and already has traces |
| B9 | Report edit (panel) | `emoji.py:1957` `_edit_inline` → helper bot `edit_message` (1965–1969) | `[EMOJI_UI] inline edit failed: …` on failure (1971) | the panel never shows the outcome — the artifact is invisible, but all traces already ran |

Registration facts: the input listener (B5) is registered **after**
`register_all` (`supervisor.py:237` then `242`; recovery path `926`), so for
any outgoing update it runs after every command handler; and every one of the
three registration sites requires `helper_enabled` — with no helper there is no
Glass UI at all. The ✨ action itself is registered at `emoji.py:2529`.

---

## 3. The first missing execution boundary

### 3.1 The deduction

`[PREMIUM_INLINE]` exists only from B8's service, and the service is called
only from B6's handler — after B7. B7 (`inspect_source_message`) emits exactly
one of its four lines for **every** reply that passes B6's target checks, even
a plain non-premium emoji. Therefore:

> **Zero `[PREMIUM_INLINE]` lines across the tests ⇒ no reply ever reached
> `_premium_inline_reply_handler` (B5 → B6). The first missing execution
> boundary is the reply dispatch.**

The button click (B1–B4) is *upstream* of this boundary and has its own,
different markers; its success or failure is not decided by the absence of
`[PREMIUM_INLINE]` (claim §0-1).

### 3.2 What can stop the flow exactly at that boundary

All of these are source-backed; none is established or excluded by the quoted
excerpt (§9 gives the log line that identifies each):

1. **The click never armed reply mode** — B2/B3 gates (debug-gated rejections,
   dropped session, non-owner) or B4's no-log error panel. Check the `[CALLBACK]`
   and `[EMOJI_UI] premium inline: selection message` markers.
2. **The reply was sent after the 120 s pending-state expiry** — the STATE
   window is 120 s (`input_state.py:24`) while the handler's own bound is 180 s
   (`emoji.py:2023`). This is the only fully silent, user-invisible failure
   mode of a correctly executed flow (the expiry itself logs at INFO).
3. **Another panel action/navigation cleared the pending entry first** —
   `panels.py:437/464/482/528` all call `clear_pending(owner_id)`; a second ✨
   tap, a Back/Home press, or any other button between arming and replying
   silently disarms the flow (and replaces it if it was another ✨ tap).
4. **The message was delivered in a different chat** than the selection
   message's (`inline_sender.py:86`) or started with `.` (90) — both silent.
5. **The reply reached the handler but hit a silent early-exit** (B6) — the
   panel reason may have been invisible if B9's edit failed (§7).
6. **A disconnect cancelled the update task between handlers** — Telethon
   cancels running per-update tasks on `disconnect()`
   (`.venv/…/telethon/client/telegrambaseclient.py:625–630`, called by the
   supervisor's reconnect); the projection-cancellation cascade would stop the
   update before B5 for that message. Not established by the excerpt.

### 3.3 What this finding does NOT claim

* It does not claim the reply content was wrong, or that the emoji was not
  Premium: even a non-premium reply would have produced a `SOURCE_ENTITY_*`
  line if it had dispatched.
* It does not claim the ✨ button was pressed in the observed window, nor that
  it was not. The excerpt establishes neither.
* It does not claim any specific one of §3.2's gates. Each is a hypothesis
  with an unambiguous discriminator (§9).

---

## 4. Receipt and dispatch logging — what the excerpt can and cannot prove

### 4.1 Where logging starts

* **Receipt is not logged.** `_helper_callback_hook` (`helper/client.py:123`)
  records `set_last_callback()` + `set_last_event_dispatch()` and nothing else.
* **The router's diagnostic lines are opt-in.** `_callback_router`
  (`panels.py:329`) gates `INCOMING` (347), all `REJECT:` variants (351–379)
  and `DISPATCH` (385) behind `settings_service.is_debug_callbacks()`
  (`panels.py:344`; default **false**, `settings_service.py:76,252–254`).
* **The first unconditional line of the whole click path** is
  `_resolve_session`'s `[CALLBACK] session lookup OK …` (290) or its
  `… FAILED … callback will be dropped` warning (317), followed by
  `_handle_action`'s `[CALLBACK] _handle_action: action_id='…'` (517) and, on
  success, `_premium_inline_action`'s `[EMOJI_UI] premium inline: selection
  message #N sent to Saved Messages` (2186).

Consequence: a delivered ✨ click ALWAYS yields at least the
`session lookup` + `_handle_action: action_id='emoji_premium_inline'` lines
(plus the `[EMOJI_UI]` line when the selection message was created).

### 4.2 `Task-3898` in `_dispatch_update`

Telethon dispatches each update in its own unnamed task
(`.venv/…/telethon/client/updates.py:275–282`: `self.loop.create_task(self._dispatch_update(...))`
unless `sequential_updates` is set), so asyncio's automatic name `Task-N`
identifies neither a client nor an update — `Task-3898` only proves that *some*
update-dispatch task existed on one of the two clients at that moment, exactly
as configured by default. It is not evidence of a premium callback.

### 4.3 `last_callback_age` ≈ 12 s

The age is written by ANY callback query received on the helper client (B1),
for any panel and any button. It cannot name the ✨ button, the panel, or the
action. At most it shows the helper client was receiving callbacks around
15:32:34 — nothing about this flow.

### 4.4 Deployment gate

The ✨ row and every `[PREMIUM_INLINE]`/`[EMOJI_UI] premium inline` marker
require the inline feature (`977247e`) and the checkpoint-2 fix (`e13deee`);
a deploy older than `977247e` would show neither the row nor any of the tags —
worth one glance when reading old log windows.

---

## 5. The task-repository `ReadError` — source verdict: cannot interrupt the premium path

`ReadError: [Errno 11] Resource temporarily unavailable` (`EAGAIN` from the
local socket layer) is a **Supabase/PostgREST transport failure**, and the
burst in the excerpt comes from the task repository. Source facts:

1. **The premium path never touches the repository or the DB.** The full call
   set of B4/B6/B8 is: `send_message`/`get_messages` (self client),
   `get_me` + `get_custom_emoji_documents` (typed Telegram wrappers),
   `inline_engine.query_results` (`self_client.inline_query`), `result.click`
   (`sendInlineBotResult`), and pure in-process functions. No `run_sync_db`, no
   Supabase, no task repository call exists anywhere on this path.
2. **When the repository IS called (by `task_events` on the same outgoing
   reply, a separate consumer earlier in the same update), its failures are
   contained three times over:** inside the repository (`task_repository.py`
   `_run` (726) swallows failures; `_mark_fallback` classifies the failure,
   arms a 5 s local-resource cooldown (`185`) and emits ONE
   `TASK_FALLBACK_CLASSIFIED reason=local_resource …` (670) for the episode,
   serving the in-memory fallback); outside it (`bot/handlers/task_events.py`
   wraps both consumers in `except Exception` → `TASK_*_TRACE
   stage=handler_error` warnings, "never poison the event path"); and by
   Telethon itself (per-handler `except Exception` in `_dispatch_update`,
   `updates.py:521–597`).
3. `run_sync_db` (`db/client.py:132`) dispatches the synchronous Supabase work
   through the ONE bounded pool (4 workers, `73`) with a 10 s watchdog
   (`_DB_TIMEOUT`=10.0, `57`), so a sick transport cannot consume the loop.

The `ReadError` burst therefore shows the task subsystem degrading
(truthfully, to its fallback) on some message in that window. It is not on the
premium path and cannot suppress or delay a trace: the worst case is extra
latency in an earlier handler of the same update.

---

## 6. The `provider:chat:nararouter` timeout — source verdict: unrelated

The 30 s timeout is raised by `guarded_await(provider.chat(...),
name="provider:chat:nararouter", timeout=30)` inside
`ProviderManager._call_once` (`manager.py:776,803–806`; `_PROVIDER_RPC_TIMEOUT`
= 30.0, line 65) and is converted there into a structured
`ProviderResponse(success=False, metadata={"failure_type": "timeout"})` — the
method's contract is "Never raises" (`manager.py:782`). It runs in the AI
handler (`ai_unified._execute_ai`) against the **self** client, while the
premium callback arrives on the **helper** client. There is no shared lock,
queue, executor or dependency between the two. A slow AI call is a different
coroutine on a different connection; it can neither emit nor suppress
`[PREMIUM_INLINE]` lines.

---

## 7. The `PeerChannel` warning — traced to its caller

The warning text — `inline edit failed: Could not find the input entity for
PeerChannel(channel_id=2750223875)` — matches the `<context> inline edit
failed: %s` family, whose callers are all Glass UI panel edits made **through
the helper bot**: `emoji.py:1971` (`[EMOJI_UI] inline edit failed`),
`bio.py:399/415`, `delete.py:44/64`, `discover.py:58`, `misc.py:268–378`,
`retrieve.py:235–634`, `username.py:396/412`. Each wraps
`helper.edit_message(chat_id, msg_id, …)`: Telethon's `get_input_peer` cannot
resolve a channel the **bot** has no entity/access-hash for. The callback
router's own edits take the other path (`panels.py` `_safe_edit` (36) uses
`event.edit(...)`, which does not need a fresh peer resolution) — which is why
navigation can keep working while these handler-specific edits fail.

Relationship to the missing logs: **independent and secondary.**

* It cannot cause a missing `[PREMIUM_INLINE]` line: in the reply path the
  traces (B7/B8) run BEFORE any `_finish` → `_edit_inline` (B9). The single
  exception is B6's silent early exits, where the *reason* would have been
  shown by exactly this edit — so a failing edit can additionally HIDE why the
  reply was rejected (e.g. "targets another chat / not a reply / wrong
  message"). If the live log's full line carries the `[EMOJI_UI]` prefix, its
  presence proves a reply reached B6 and exited there — worth checking (§9).
* It is not proof the flow is broken anywhere else; it is the same
  helper-bot peer-resolution weakness shared by every panel-edit handler.

---

## 8. The checkpoint-2 `send_message` fix (`e13deee`) — verified, and not the cause of the missing logs

* Current code: `_inspect_inline_result` (`premium_emoji_inline_service.py:549`)
  reads `getattr(first, "send_message", None)` (567–574) with the explanatory
  comment; a missing field yields `entity_present=False` +
  `INLINE_RESULT_INSPECTED … reason="no_send_message"` (573).
* Tests: `_FakeInlineResult` mirrors the real TL field (`test_premium_emoji_inline.py:100–116`);
  `test_result_missing_the_entity_stops_before_the_send` (468),
  `test_entity_stripped_after_an_accepted_send_is_reported` (516) and
  `test_verified_path_reports_entity_attribution_and_eligibility` (404) pin the
  three checkpoint-2/3 verdicts. All pass (see IMPLEMENTATION_REPORT.md §5).
* History: the fix is an ancestor of `origin/main` (verified
  `git merge-base --is-ancestor e13deee origin/main` → exit 0). The
  2026-10-10 `INLINE_RESULT_ENTITY_MISSING` run came from a deploy that
  predates it — that run nevertheless REACHED B7/B8
  (`SOURCE_ENTITY_VALIDATED → … → INLINE_RESULT_ENTITY_MISSING`), which
  independently proves the reply path is operational for this owner when a
  reply is actually dispatched (and B6's target checks pass for Saved Messages
  replies).
* The fix changes only what checkpoint 2 reads AFTER B7 ran, so it can neither
  cause nor cure the absence of `[PREMIUM_INLINE]` lines.

---

## 9. Decision table — the existing logs settle every boundary (no re-run needed)

Search the SAME log window for these exact tags; the first one present marks
the furthest boundary reached:

| Tag to search | If present | Meaning |
|---|---|---|
| `[CALLBACK] session lookup FAILED` | callback delivered but dropped | stale/invalid panel session — B3 rejected |
| `[CALLBACK] _handle_action: action_id='emoji_premium_inline'` | click reached the action | B3 passed (∅ ⇒ the click never reached the router with a live session, or was never made) |
| `[EMOJI_UI] premium inline: selection message #N sent` | reply mode armed for `#N` | B4 passed (∅ with B3 present ⇒ B4 bailed; the error panel said why) |
| `Input for owner … expired (timeout=120s)` | the state window closed first | B5 expired — reply (or any message) came too late |
| any other `[CALLBACK] _handle_action: …` between arming and replying | pending was cleared/replaced | B5 disarmed by that action |
| `[EMOJI_UI] premium inline: cannot read the reply` | dispatched, read failed | B6 read stage |
| `[PREMIUM_INLINE] SOURCE_ENTITY_*` | reply dispatched | B7+ ran — the flow works from here on; the outcome lines say exactly what happened |
| `[EMOJI_UI] inline edit failed:` (esp. PeerChannel) | a panel edit failed | B9 (or a B6 early-exit's feedback) invisible — not causal |

---

## 10. What remains unverified

1. **The live trigger of the three tests.** Which §3.2 gate stopped each test
   is not decided by the quoted excerpt; §9's table decides it from the
   existing logs.
2. **Whether the ✨ button was clicked at all in the observed window** — no
   provided line names the action.
3. **Telegram's live treatment of the entity after the fix.** The fixed
   checkpoint 2 has not been exercised against real Telegram; whether the
   inline path preserves the entity end-to-end is still open (the pre-fix run
   died at the old field-read bug before establishing it).
4. **Render-side log completeness** — only the quoted excerpt was reviewed.
5. Live behavior was not exercised by this task; nothing here claims the
   failure is "fixed" — the boundary analysis is source-level and complete.

---

## 11. Confirmed facts / hypotheses / unresolved

### Confirmed facts

1. `[PREMIUM_INLINE]` can only be emitted by the reply step's service calls
   (service `_trace`, line 118), never by the button click.
2. Every dispatched reply emits exactly one first `SOURCE_ENTITY_*` line
   (service 208/210/223/236; called at `emoji.py:2268`) — pinned by a new test.
3. Zero `[PREMIUM_INLINE]` ⇒ the reply never dispatched to
   `_premium_inline_reply_handler`.
4. The click path's first unconditional logs are the `[CALLBACK] session
   lookup` line(s) and `[CALLBACK] _handle_action: action_id='…'`; receipt
   itself is not logged, and INCOMING/DISPATCH are debug-gated (default off).
5. `Task-3898` is an auto-named per-update task — any update, either client.
6. `last_callback_age` names no button or action.
7. The premium path performs no DB/task-repository call; the `ReadError` burst
   is contained by repository/handler/Telethon-level catches and cannot
   interrupt it.
8. The nararouter timeout is a bounded, converted provider failure on the self
   client's AI handler — unrelated.
9. The `PeerChannel` warning is a helper-bot panel-edit failure (`_edit_inline`
   and peers), running after the traces; at most it hides early-exit reasons.
10. The `send_message` checkpoint-2 fix is present, tested, and an ancestor of
    `origin/main`; it is not implicated in the missing logs.
11. The pending-input listener is registered last (after `register_all`), only
    when the helper is enabled, and only dispatches a message sent in the
    selection message's own chat within the 120 s state window.

### Hypotheses (reasonable, not proven)

1. One of §3.2's state gates (most plausibly the 120 s expiry or a
   cleared/replaced pending entry) stopped the reply in the three tests.
2. The `PeerChannel` warning the owner saw came from the emoji flow's
   `_edit_inline` (if prefixed `[EMOJI_UI]`) and hid an early-exit reason.

### Unresolved questions

1. Which specific gate stopped each of the three tests (decidable from the
   existing logs via §9).
2. Whether the ✨ action ran in the observed window at all.
3. How Telegram treats the entity on the inline-user path, end-to-end, with
   the fixed checkpoint 2.
