# Investigation — Premium Emoji Inline Flow: Receipt Coverage and the Earliest Unobserved Boundary (2026-10-10)

Canonical, latest-only record of the investigation into why the owner's live
tests of the Premium custom-emoji inline flow produced **no `[PREMIUM_INLINE]`
lines in Render logs**, what the `last_callback_age` counter actually covers,
and which diagnostic was added to make the earliest unobservable stage
observable. This document replaces the previous investigation record in full.
Earlier records — the via-bot feasibility study (`b04cb41`), the
implementation record (`977247e`), the checkpoint-2 fix record (`e13deee`),
the 98c2825 audit, and the boundary-tracing task (`a54877b`) — remain readable
in git history and are referenced here only by commit id.

| Item | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Branch | `main` |
| Revision this record was produced from | `a54877b` (verified = `origin/main` at the start of this task) + this task's changes |
| Question | Why do live tests still show no `[PREMIUM_INLINE]` lines; what does `last_callback_age` cover; which boundary is the earliest one not observable — and what is the smallest diagnostic that distinguishes the possible failure points? |
| Verdict | **(1)** `last_callback_age` is **receipt-level only**: its two writers run at callback delivery and at router entry, before every gate — it cannot distinguish a delivered callback from a handler being invoked, and it cannot name the button. **(2)** The earliest unobserved boundary was **callback receipt itself** (delivery to the helper client carried no log). **(3)** The smallest evidence-based diagnostic — ONE bounded `[CALLBACK] received …` line per delivered callback in the helper hook — is **implemented** (this task). **(4)** The previously deduced first missing boundary of the flow stands: the reply dispatch (no reply ever reached the reply handler in the recorded tests). **(5)** The repository `ReadError` has no call relationship with this path and cannot interrupt it. |
| Production code changed | **Yes — the one receipt line** (`backend/helper/client.py`), as the smallest diagnostic. No behavioral change beyond logging. |
| Live Telegram verification | **Not performed in this task** (no live session in this workspace); the receipt line has not yet been observed live. |

---

## 0. Status of every claim

| # | Statement | Status |
|---|---|---|
| 1 | `last_callback_age` is written in exactly TWO places, both on the helper client and both BEFORE any filter/matching: the receipt hook (`helper/client.py:129`) and the router's entry (`panels.py:334`) | **CONFIRMED** — exhaustive grep + `health.py:206` (`_last_callback = time.time()`); heartbeat derives the age at `heartbeat.py:147/153/173` |
| 2 | The counter therefore proves "a callback query was delivered (and entered the router)" — it can NOT distinguish delivery from handler invocation, name the button, or prove any stage after router entry | **CONFIRMED** — source: both writers are pre-gate; no other writer exists |
| 3 | Callback receipt is now logged: ONE bounded `[CALLBACK] received data='…' sender_id=… chat_id=… msg_id=… inline_msg_id='…'` line per delivered callback (`helper/client.py:144–152`) | **IMPLEMENTED in this task**, pinned by `tests/test_helper_callback_receipt.py` |
| 4 | `[PREMIUM_INLINE]` can only be emitted by the reply step; every dispatched reply emits exactly one first `SOURCE_ENTITY_*` line | **CONFIRMED** (prior task, unchanged) — service `_trace` (118); four variants at 208/210/223/236; pinned by `test_the_real_pending_input_listener_reaches_the_production_flow` |
| 5 | Zero `[PREMIUM_INLINE]` across the recorded tests ⇒ no reply reached `_premium_inline_reply_handler` (the reply dispatch is the flow's first missing boundary) | **CONFIRMED** (deduction, §3) |
| 6 | The ✨ button was — or was not — clicked in the observed windows | **NOT ESTABLISHED by the timestamps** (explicitly not inferred; §4) — the new receipt line settles it from the next run |
| 7 | The repository `ReadError` burst can interrupt the premium path through a concrete call relationship | **REFUTED** — no call relationship exists; failures stay in the bounded DB thread pool (§5) |
| 8 | The `provider:chat:nararouter` timeout is related | **REFUTED** (prior finding, unchanged — §6) |
| 9 | The `PeerChannel` warning caused the missing logs | **REFUTED** (prior finding, unchanged — §7) |
| 10 | The `e13deee` checkpoint-2 `send_message` fix is present, tested, and unrelated to the missing logs | **CONFIRMED** (prior finding, unchanged — §8) |

---

## 1. The reported evidence

### 1.1 The live tests

The owner tested the feature (Menu → Emoji → ✨ Send Premium Emoji → reply to
the Saved Messages selection message with a genuine Premium emoji) while
monitoring Render logs. Across the tests, **no `[PREMIUM_INLINE]` logs
appeared**. No re-test was requested before completing this source-level
investigation.

### 1.2 The 15:32–15:33 UTC excerpt (2026-10-10)

* ~15:32:46 — `Task-3898` was in Telethon's `_dispatch_update`.
* ~15:32:46 — `last_callback_age` ≈ 12 s.
* ~15:32:57–15:33:07 — task-repository queries repeatedly failed with
  `ReadError: [Errno 11] Resource temporarily unavailable`.
* ~15:33:11 — `provider:chat:nararouter` timed out after 30 s.

### 1.3 The 17:07–17:08 UTC excerpt (2026-10-10) — the new evidence

* The runtime reports `runtime_state=READY`, `self_connected=True`,
  `helper_connected=True`.
* 17:07:42 — `last_callback_age` ≈ 31 s.
* 17:08:12 — `last_callback_age` ≈ 61 s.
* `last_update_age` and `last_event_age` remained low.
* Multiple task-repository queries failed with `ReadError: [Errno 11]`.
* Keepalive checks succeeded; reported event-loop latency was low.
* Still no `[PREMIUM_INLINE]` entries.

**What this establishes (and nothing more):**

* `last_callback_age` ≈ 31 s at 17:07:42 ⇒ the last helper callback arrived
  ≈ 17:07:11. The age grew by exactly the elapsed 30 s to ≈ 61 s at 17:08:12
  ⇒ **no new helper callback arrived in that 61 s window** (the counter is
  receipt-level, §4). If the owner clicked ✨ inside that window, the callback
  was not delivered — but whether the click happened inside the window is NOT
  established, and the timestamp names no button. This is deliberately not
  read as "the Premium Emoji callback was received or rejected".
* `last_update_age` / `last_event_age` being low is **not** helper-callback
  evidence: those timestamps are written by hooks on BOTH clients (self-client
  `NewMessage`/`MessageEdited` hooks at `router.py:42/56/66`, helper raw hook,
  self update hooks), so they only show the runtime is receiving traffic and
  dispatching updates generally.
* Keepalive RPCs succeeding with low loop latency shows the event loop is not
  blocked — consistent with the `ReadError` failures living inside the bounded
  DB thread pool (§5), not on any client's dispatch path.

---

## 2. The complete execution path, boundary by boundary

| # | Boundary | Code (file — function/line) | Unconditional marker | Silent failure modes |
|---|---|---|---|---|
| B1 | Callback delivery to the helper client | `helper/client.py:116` `register_helper_hooks` → `_helper_callback_hook` (126; registered FIRST, `supervisor.py:415`) | **NEW (this task):** `[CALLBACK] received data='…' sender_id=… chat_id=… msg_id=… inline_msg_id='…'` (144–152) + `set_last_callback()` (129) | none anymore — every delivered callback is now named |
| B2 | Router entry + pre-session gates | `panels.py:325` `register_callback_handlers` → `_callback_router` (329); `resolve_callback_message` (87) | **none** until session resolution — INCOMING (347), REJECTs (351–379), DISPATCH (385) are `debug_callbacks`-gated (default false, `settings_service.py:76`); `set_last_callback()` at entry (334) | unresolvable coordinates / non-owner / empty data are rejected silently — but each now has the B1 receipt line before it |
| B3 | Session resolution + action dispatch | `panels.py:285` `_resolve_session`, `panels.py:512` `_handle_action` | `[CALLBACK] session lookup OK …` (290) or `… FAILED … callback will be dropped` (317); then `[CALLBACK] _handle_action: action_id='emoji_premium_inline' extra=''` (517) | a stale panel session (created before a restart) drops at (317) |
| B4 | Selection message + arming | `emoji.py:2147` `_premium_inline_action`; `send_message("me", …)` (2157); `set_pending` (2178) | `[EMOJI_UI] premium inline: selection message #N sent to Saved Messages` (2186) | send exception → warning (2159); unusable returned id/chat → error panel with no log (2167–2177) |
| B5 | Reply dispatch (pending-input listener) | `inline_sender.py:75` `register_input_listener` → `_input_listener` (78) | **none** — silent returns on: not owner (80), no/expired pending (82; the 120 s state expiry logs `Input for owner … expired` when it fires), different chat (86), dot-prefix (90) | any panel action/navigation clears pending first (`panels.py:437/464/482/528`); a new `set_pending` replaces it |
| B6 | Reply handler target gates | `emoji.py:2200` `_premium_inline_reply_handler` | **none** — silent-by-design early exits (later list); unreadable reply logs at 2230 | the panel edits that would SHOW the reasons go through the helper bot and can themselves fail (§7) |
| B7 | **First `[PREMIUM_INLINE]` line** — source inspection | `emoji.py:2268` → service `inspect_source_message` (194) | exactly one of `SOURCE_ENTITY_FOUND found=False` (208), `found=True, usable=False` (210), `…reason="span"` (223), `SOURCE_ENTITY_VALIDATED` (236) | none — unconditional for every dispatched reply |
| B8 | Full service pipeline (checkpoint 2 + send + read-back) | service `send_premium_emoji_via_inline` (704) etc. | `SEND_STARTED`, `OUTBOUND_PAYLOAD_BUILT`, `INLINE_QUERY_STARTED`, `INLINE_RESULT_INSPECTED`, `INLINE_SEND_STARTED/ACCEPTED`, `READBACK_RESULT`, `DIAGNOSIS` | none before B7 |
| B9 | Report edit (panel) | `emoji.py:1957` `_edit_inline` → helper bot `edit_message` | `[EMOJI_UI] inline edit failed: …` on failure (1971) | outcome invisible in the panel; traces already ran |

Registration facts (unchanged, re-verified): only TWO `CallbackQuery`
handlers exist in the whole backend — the B1 hook (`client.py:125`) and the B2
router (`panels.py:328`); the hook is registered first (`supervisor.py:415`
then `432–433`), and neither builder carries a data/chat/user filter, so both
run for EVERY delivered callback. The ✨ action is registered at
`emoji.py:2529`.

---

## 3. The flow's first missing boundary (from the recorded tests)

`[PREMIUM_INLINE]` exists only from the service, and the service is called
only by the reply handler, after `inspect_source_message` — which emits
exactly one of its four first lines for **every** reply that passes the
handler's target checks (even a plain non-premium emoji). Therefore zero
`[PREMIUM_INLINE]` lines across the recorded tests ⇒ **no reply ever reached
`_premium_inline_reply_handler`: the reply dispatch (B5 → B6) was the first
missing execution boundary** of those tests. The candidate state gates and
their discriminators are in §10. The button click (B1–B4) is upstream and has
its own markers.

---

## 4. `last_callback_age` — exactly what it covers (`latest`)

**Writers (exhaustive):** `backend/health.py:206` `set_last_callback()` is the
only setter of `_last_callback`; it is called from exactly two places, both on
the **helper** client:

1. `backend/helper/client.py:129` — inside `_helper_callback_hook`, the FIRST
   registered `CallbackQuery` handler (`register_helper_hooks`, line 116;
   registered at `supervisor.py:415`). Fires for EVERY delivered callback
   query — any button, any panel, non-owner clicks included, even callbacks
   whose message cannot be resolved.
2. `backend/helper/panels.py:334` — at the top of `_callback_router`
   (`register_callback_handlers`, line 325), BEFORE the owner / coordinates /
   data / session gates and before any handler matching.

**Derivation:** the heartbeat reads `get_last_callback()`
(`heartbeat.py:147`) and logs `last_callback_age` (`153`, `173`) as a string
age every 30 s; `health.snapshot()` exposes `last_callback_s`
(`health.py:300`); diagnostics print the same age (`diagnostics.py:242`).

**Answers to the task's question:**

* **Which callback types?** ALL `CallbackQuery` updates delivered to the
  helper client. Neither registering builder filters by data, chat, or user.
* **Which execution stages?** Delivery (stage 1) and router entry (stage 2) —
  both BEFORE filtering, matching, session resolution and handler invocation.
* **Can it distinguish an incoming callback from a handler being invoked?**
  **No.** It is never written from inside any action/panel/input handler, so
  a fresh age proves delivery-and-router-entry at most; it cannot prove the
  ✨ handler ran, cannot identify the button, and cannot prove the router got
  past its gates (the writer sits before them).
* **Can it prove NO callback arrived in a window?** Yes, for the helper
  client: monotonic age growth across samples (31 s → 61 s over a 30 s
  interval, 17:07:42 → 17:08:12) proves no new helper callback in that
  window — which is exactly why, if the ✨ click happened inside it, the click
  was not delivered. It cannot say whether the click happened.

**RESOLVED in this task:** the missing per-callback identity. B1 now logs one
bounded receipt line per delivered callback (§9), so the same counter's
information ("something arrived") is upgraded to "THIS callback arrived, with
its data and coordinates".

---

## 5. The repository `ReadError` — no call relationship with this path

Re-verified against the current code, with the new evidence:

1. **No call relationship exists.** The full call set of B1–B4/B6–B8 is:
   `send_message`/`get_messages` (self client), `get_me`,
   `get_custom_emoji_documents` (typed Telegram wrappers),
   `inline_engine.query_results` (`self_client.inline_query`),
   `result.click` (`sendInlineBotResult`), health timestamp setters
   (in-memory), and pure functions. **Nothing on this path calls
   `run_sync_db`, Supabase, or the task repository** — so no `ReadError` can
   originate inside it.
2. **Where the repository IS called** (task consumers like `task_events` on
   the self client), failures are contained three times over: inside the
   repository (`task_repository.py` `_run` (726) swallows; `_mark_fallback`
   classifies, arms a 5 s local-resource cooldown (185), logs ONE
   `TASK_FALLBACK_CLASSIFIED reason=local_resource` (670) per episode and
   serves the in-memory fallback); at the handler boundary
   (`bot/handlers/task_events.py` wraps both consumers in `except Exception`);
   and by Telethon's per-handler `except Exception`
   (`telethon/client/updates.py:521–597`).
3. **Path separation:** `run_sync_db` (`db/client.py:132`) dispatches through
   its own bounded pool (4 workers, `73`; 10 s watchdog, `57`) named
   `lifeos-supabase` threads — not the event loop, not either client's
   dispatch. The 17:07–17:08 evidence matches this: keepalive RPCs succeeded,
   loop latency stayed low, while the repository errors were confined to
   those worker threads.

**Verdict: the `ReadError` burst is a degrading task subsystem, not a cause
and not a courier. It cannot affect the callback path through any concrete
call relationship.**

---

## 6. The `provider:chat:nararouter` timeout — unchanged verdict: unrelated

Raised by `guarded_await(provider.chat(...), name="provider:chat:nararouter",
timeout=30)` inside `ProviderManager._call_once` (`manager.py:776,803–806`;
`_PROVIDER_RPC_TIMEOUT` = 30.0, line 65), converted there into a structured
`ProviderResponse(success=False, metadata={"failure_type": "timeout"})` —
"Never raises" (`manager.py:782`). It runs in the AI handler on the **self**
client; the callback path runs on the **helper** client. No shared lock,
queue, executor or dependency.

---

## 7. The `PeerChannel` warning — unchanged verdict: independent/secondary

The `<context> inline edit failed: … Could not find the input entity for
PeerChannel(channel_id=…)` family (`emoji.py:1971`, `bio.py:399/415`,
`delete.py:44/64`, `discover.py:58`, `misc.py:268–378`, `retrieve.py:235–634`,
`username.py:396/412`) is Glass UI panel edits made **through the helper bot**
(`helper.edit_message`), where Telethon's `get_input_peer` cannot resolve a
channel the bot has no entity/access-hash for. The router's own edits use
`event.edit(...)` (`panels.py:46` `_safe_edit`) and are not affected. It runs
AFTER the traces (B9) and cannot cause a missing `[PREMIUM_INLINE]`; at most
it hides a B6 early-exit reason.

---

## 8. The checkpoint-2 `send_message` fix (`e13deee`) — unchanged: verified, unrelated

Current code reads `getattr(first, "send_message", None)`
(`premium_emoji_inline_service.py:567–574`); tests pin the three checkpoint-2/3
verdicts (`test_premium_emoji_inline.py:404/468/516`); the commit is an
ancestor of `origin/main`. It changes only what checkpoint 2 reads AFTER B7
ran and cannot cause or cure missing `[PREMIUM_INLINE]` lines.

---

## 9. Earliest unobserved boundary → implemented diagnostic

**Earliest unobserved boundary:** callback **receipt** (B1) — before this
task, delivery carried no log at all; the only related signal was the
aggregate, identity-less `last_callback_age` (§4), and the router's first
unconditional line (B3) requires a resolvable session. So the failure could
not be split into "never delivered" vs "delivered, dropped before the first
log".

**Implemented change (smallest, evidence-based — production):** one bounded
receipt line in `_helper_callback_hook` (`backend/helper/client.py:144–152`):

```
[CALLBACK] received data='<data, ≤64 chars>' sender_id=<id> chat_id=<id> msg_id=<id> inline_msg_id='<id>'
```

* Additive only: the health timestamps keep their exact behavior; the log is
  wrapped in its own `try/except` so the hook still can never raise; fields
  are bounded (Telegram caps callback data at 64 bytes) and carry no secret.
* It fires for EVERY delivered callback — before B2's gates — so it covers
  the exact blind spot: non-delivery vs pre-session drops vs wrong button.

**What it distinguishes on the next run:**

| Observation | Conclusion |
|---|---|
| No `[CALLBACK] received` around the click time | the click was never delivered (or never made) — B1 |
| `received data='…'` that is NOT the premium action | a different button was clicked / stale panel |
| `received` with `action:emoji_premium_inline`, then no `session lookup …` line | dropped at B2's pre-session gates — coordinates/owner/data visible in the same line |
| `received` + `session lookup FAILED` | stale panel session (B3) |
| `received` + `_handle_action: action_id='emoji_premium_inline'` + `[EMOJI_UI] … selection message` | click path fully succeeded → look at B5/B6 next |

**Why the reply side was NOT additionally instrumented (smallest-change
discipline):** the listener's only unbounded silent gate is "no pending while
a message arrives" — instrumenting it would require logging EVERY owner
message, which is spam by construction and violates the project's logging
discipline. The reply side already has bounded discriminators (`Input for
owner … expired`; the arming `[EMOJI_UI]` line; the §10 table). One receipt
line at the earliest boundary is the minimal change that distinguishes the
possible failure points; deeper boundaries are reached with the existing
unconditional lines.

---

## 10. Decision table — the logs settle every boundary (no re-run needed for past windows; the receipt line applies to the next one)

Search the same window for these exact tags; the first one present marks the
furthest boundary reached:

| Tag to search | If present | Meaning |
|---|---|---|
| `[CALLBACK] received data='…'` | callback delivered (new, from this task's deploy onward) | B1 passed; `data` names the button |
| `[CALLBACK] session lookup FAILED` | delivered but dropped | stale/invalid panel session — B3 |
| `[CALLBACK] session lookup OK …` | session resolved | B2/B3 passed |
| `[CALLBACK] _handle_action: action_id='emoji_premium_inline'` | click reached the action | B3 passed |
| `[EMOJI_UI] premium inline: selection message #N sent` | reply mode armed for `#N` | B4 passed |
| `Input for owner … expired (timeout=120s)` | state window closed first | B5 expired — the reply came too late |
| any other `[CALLBACK] _handle_action: …` between arming and replying | pending was cleared/replaced | B5 disarmed |
| `[EMOJI_UI] premium inline: cannot read the reply` | dispatched, read failed | B6 |
| `[PREMIUM_INLINE] SOURCE_ENTITY_*` | reply dispatched | B7+ ran — outcome lines say what happened |
| `[EMOJI_UI] inline edit failed:` | a panel edit failed | B9 (or hidden B6 feedback) — not causal |

---

## 11. What remains unverified

1. **The live trigger of the recorded tests** — no provided line names the
   ✨ button; the receipt line is the instrument for the next run, it cannot
   retroactively name the 15:32/17:07 callbacks.
2. **Whether the ✨ button was clicked (and when) in the recorded windows.**
3. **Telegram's live treatment of the entity after the fix** — still open
   (checkpoint 2 fixed but not exercised end-to-end live).
4. **The new receipt line itself has not been observed live** (no live
   session here); it is source-verified and test-pinned only.
5. Render-side log completeness beyond the quoted excerpts.

---

## 12. Confirmed facts / hypotheses / unresolved

### Confirmed facts

1. `set_last_callback()` has exactly two callers — receipt hook
   (`helper/client.py:129`) and router entry (`panels.py:334`) — both on the
   helper client and both before every gate; the heartbeat's
   `last_callback_age` derives from it.
2. The counter cannot distinguish delivery from handler invocation, cannot
   name the button, and monitors no stage after router entry.
3. Only two `CallbackQuery` handlers exist (hook, router); the hook is
   registered first; neither filters by data/chat/user.
4. Monotonic age growth 31 s → 61 s (17:07:42 → 17:08:12) proves no new
   helper callback arrived in that 61 s window; `Task-N`/`last_callback_age`
   still name no button, and nothing was inferred beyond that.
5. A new bounded `[CALLBACK] received …` receipt line is implemented at B1
   and pinned by tests; the hook still never raises and the health timestamps
   are unchanged.
6. Zero `[PREMIUM_INLINE]` across the recorded tests ⇒ the reply never
   reached the reply handler (reply dispatch = the flow's first missing
   boundary in those tests).
7. The repository `ReadError` has NO call relationship with the callback
   path; its failures are contained and confined to the bounded DB pool.
8. The nararouter timeout and the `PeerChannel` warning remain unrelated
   (different client/consumer; post-trace panel-edit artifact).
9. The `e13deee` checkpoint-2 fix is present, tested, on `origin/main`, and
   not implicated.

### Hypotheses (reasonable, not proven)

1. One of the §3 state gates stopped the reply dispatch in the recorded tests
   (the §10 table decides it from the existing logs).
2. The `PeerChannel` warning (if `[EMOJI_UI]`-prefixed) hid a B6 early-exit
   reason.

### Unresolved questions

1. Which gate stopped each recorded test (decidable from existing logs).
2. Whether the ✨ action ran in the observed windows.
3. How Telegram treats the entity end-to-end on the inline path (live).
