# IMPLEMENTATION_REPORT.md — Premium Custom-Emoji Delivery: Logs-Only Route Diagnostic and the Verdict on Alternative Routes

This file describes **only** the current state of the repository after this task
and **replaces every previous report in full**. The inline entity-loss trace
(`98c2825`, `a54877b`, `9633c0b`, `b463552`) and the owner-authored self-send
experiment (`f22894e`, `9b34e38`) stay readable in git history.

| Item | Value |
|---|---|
| Task | Resolve the premium custom-emoji delivery blocker with **log-based verification only**: find the smallest supported route that delivers a genuine `MessageEntityCustomEmoji` **with `via_bot_id` attribution**, or establish that no such route exists |
| Project / branch | LifeOS Telegram self-bot (`Onlyicing1/Telegram-self-bot`), `main` |
| Base revision | `9b34e38` — freshly fetched and verified `= origin/main` at task start |
| Telethon | **`telethon==1.34.0`**, layer 173 (`backend/requirements.txt:1`) |
| Route verdict | **No distinct, supported route exists.** `via_bot_id` is produced by `messages.sendInlineBotResult` alone, and the entity on that path can only travel inside the bot's answer payload — the exact two constructors (`InputBotInlineMessageText`, `InputBotInlineMessageMediaAuto`) the already-attempted request's field space consists of. Every route that can carry a real entity (user-authored message, forwarding, bot-direct send) can never carry `via_bot_id`. Details and quotes: `INVESTIGATION.md` §3–§5 |
| Production code changed | **Two files, behaviour-preserving**: the inline service gains the correlated, logs-only route diagnostic; the panel stops rendering an outcome report. No route, gate, payload, entitlement, client or listener changed |
| Live Telegram verification | **Not performed** — no `API_ID`/`API_HASH`/`SESSION_STRING`/`BOT_OWNER_ID` and no `.env` in this workspace. **No Telegram request of any kind was made**: no inline query, no submission, no send, no read-back. Nothing in this report is a live observation |
| Supabase / SQL | untouched; `DATABASE_ARCHITECTURE.md` unchanged |

---

## 1. What was investigated (and what was *not* repeated)

The task's established evidence was treated as premises and **not** re-run:
the three earlier attempts already established that the source entity and the
outbound payload validate, that the helper bot's answer **was submitted** with
one custom-emoji entity, and that the result Telegram returned from
`messages.getInlineBotResults` carried `entity_count=0` — so the fail-closed
gate refused the send, `messages.sendInlineBotResult` never ran, and
`via_bot_id` was never observed.

The investigation therefore moved to the **route** and to the **constructor
space**, offline:

1. which operation can set `via_bot_id` at all (documented: only
   `messages.sendInlineBotResult`);
2. which inline-message constructors can carry `entities` at all (pinned
   Telethon: exactly `InputBotInlineMessageText` and
   `InputBotInlineMessageMediaAuto`, out of eight; the result *types* are only
   containers around one of those two);
3. which other documented routes can carry a real entity (user-authored
   `sendMessage`, forwarding, bot-direct send, layer-225 prepared inline
   messages) and why each of them fails one of the two criteria.

Result: **no distinct, supported route satisfies both criteria.** Per the task's
own rule, that is reported rather than papered over with another speculative
implementation (`INVESTIGATION.md` §4–§5).

## 2. What was implemented

The task's logging requirement, on the **existing** owner-only action — no new
button, no new route, no repeated failed request:

| # | Change | Why |
|---|---|---|
| 1 | `backend/services/premium_emoji_inline_service.py` — one **correlated, logs-only** diagnostic sequence for ONE route attempt: `PREMIUM_EMOJI_ROUTE_DIAG`, `run=<id>`, stages `ROUTE_STARTED`, `SOURCE_ENTITY_VALIDATED`, `PAYLOAD_VALIDATED`, `INLINE_RESULT_SUBMITTED`, `TELEGRAM_RESULT_INSPECTED`, `MESSAGE_SENT`, `MESSAGE_READBACK_VERIFIED`, `VIA_BOT_ATTRIBUTION_VERIFIED`, `DIAGNOSIS`. Fields are ids, counts, offsets, UTF-16 lengths, booleans, `glyph_source`, bounded exception text and one bounded summary. The record returned to the caller now carries `run_id`, and **every** attempt — including a pre-send refusal — closes with exactly one machine-readable `DIAGNOSIS` | The acceptance criteria can only be decided by Telegram's stored message; the log is where that evidence belongs |
| 2 | `backend/bot/handlers/emoji.py` — the outcome renderer (`_premium_inline_report`) and its call site are **removed**; the panel's only response is `_PREMIUM_INLINE_NOTICE`, which states no diagnosis, no entity fact, no attribution fact and never claims success. The handler logs the `run_id` so a UI action and a log sequence can be correlated | "Do not send diagnostic reports, test results, or status messages to Telegram" and "do not confuse 'the bot accepted the inline-result submission' with 'the message was sent and attribution verified'" |
| 3 | `tests/test_premium_emoji_inline.py` — the panel assertions were rewritten to the neutral notice, and 6 tests were added for the log contract (stage order under ONE `run=<id>`, a refusal still diagnosed, the entitlement facts at `ROUTE_STARTED`, a stripped stored entity diagnosed from the read-back, missing attribution diagnosed and never called success, bounded fields with no secret/document text) | The log contract is the deliverable and must be pinned |

**Unchanged on purpose:** the route, the reply-mode arming, the selection-message
targeting, `inspect_source_message`, `build_inline_result`,
`validate_inline_payload`, the fail-closed gates, the Saved-Messages-only rule,
the helper bridge, the probe service, the Library/import design, the schema.
No new client, listener, update loop, scheduler, executor or dependency; no
Unicode fallback; no retry; no Supabase/SQL.

### The log contract (what an operator greps)

```
grep "PREMIUM_EMOJI_ROUTE_DIAG" <app log> | grep "run=<id>"
```

One attempt = one `run=<id>` = one sequence ending in `stage=DIAGNOSIS`, e.g.

```
[PREMIUM_EMOJI_ROUTE_DIAG] run=… stage=ROUTE_STARTED route=messages.sendInlineBotResult owner=… destination=… document_id=… owner_premium=… document_free=… helper_bot_id=… glyph_source=document_alt
[PREMIUM_EMOJI_ROUTE_DIAG] run=… stage=SOURCE_ENTITY_VALIDATED document_id=… source_span_utf16_len=2 glyph_source=document_alt
[PREMIUM_EMOJI_ROUTE_DIAG] run=… stage=PAYLOAD_VALIDATED valid=True document_id=… offset=15 length=2 glyph_utf16_len=2 text_utf16_len=17 entity_count=1
[PREMIUM_EMOJI_ROUTE_DIAG] run=… stage=INLINE_RESULT_SUBMITTED returned_results=1 submitted_recorded=True submitted_ok=True submitted_custom_emoji_count=1 submitted_document_id_match=True
[PREMIUM_EMOJI_ROUTE_DIAG] run=… stage=TELEGRAM_RESULT_INSPECTED ok=… reason=… entity_present=… entity_count=… document_id_match=… span_match=… text_match=…
[PREMIUM_EMOJI_ROUTE_DIAG] run=… stage=MESSAGE_SENT sent=… message_id=… destination=… send_path=messages.sendInlineBotResult
[PREMIUM_EMOJI_ROUTE_DIAG] run=… stage=MESSAGE_READBACK_VERIFIED readback_ok=… entity_present=… stored_document_id=… stored_offset=… stored_length=… document_id_match=… span_match=… text_match=…
[PREMIUM_EMOJI_ROUTE_DIAG] run=… stage=VIA_BOT_ATTRIBUTION_VERIFIED via_bot_id=… expected_via_bot_id=… match=…
[PREMIUM_EMOJI_ROUTE_DIAG] run=… stage=DIAGNOSIS diagnosis=… sent=… entity_stored=… via_bot_id=… summary=…
```

The success log (`MESSAGE_SENT` / `MESSAGE_READBACK_VERIFIED` /
`VIA_BOT_ATTRIBUTION_VERIFIED` / `DIAGNOSIS`) is emitted **only after** the sent
message has been read back by its exact id; the `DIAGNOSIS` line is always the
last line of a run. Nothing in the sequence contains a session string, token,
access hash, authorization header, file reference, message body or document
text — asserted by test.

### What this task's change did not add and why that was the decision

The owner's in-chat trigger phrase (default `Nova`) **is not** sent to Telegram
during this task, and no outbound AI turn is performed. It is recorded here as
context only: during any live run of the project, a trigger text may be composed
from `backend/ai/prompt/template.py` and submitted to the selected provider — so
a per-request OpenAI-format `inline_query_content` tool description like
`"Nova inline_query: "` could appear inside that turn's tool schema if future work
chose to expose a `query_results` tool. This task did not expose such a tool and
did not perform such a turn, so the one controlled, evidence-bearing step that
would complete the acceptance criteria remains a Telegram inline-query /
submission / send / read-back — which, while designed to exercise the existing
appendix-boundary (`update.id == last_action_id`, identity gate, fail-closed),
was **not** run for lack of credentials (`§8`).

## 3. Tests

| Check | Command | Result |
|---|---|---|
| Compile | `.venv/bin/python -m compileall -q backend/services/premium_emoji_inline_service.py backend/bot/handlers/emoji.py tests/test_premium_emoji_inline.py` | **COMPILE_OK**, exit 0 |
| Focused file (offline) | `.venv/bin/python -m pytest tests/test_premium_emoji_inline.py -q` | **84 passed**, exit 0 (78 before this task: 6 added, the panel assertions rewritten) |
| Emoji-related suites (offline) | `.venv/bin/python -m pytest tests/test_premium_emoji_inline.py tests/test_premium_emoji_probe.py tests/test_emoji_ui.py tests/test_premium_emoji_self_send_route.py -q` | **205 passed, 1 skipped**, exit 0 (the skip is the pre-existing opt-in `live_telegram` test) |
| Full suite | `timeout 880 .venv/bin/python -m pytest tests/ -q -p no:cacheprovider` (exit status captured) | **5844 passed, 27 skipped, 2 warnings in 119.89s**, **EXIT=0** — baseline at `9b34e38` was 5838 passed / 27 skipped, i.e. exactly the **+6** new tests, **0 regressions** |
| Whitespace / diff sanity | `git diff --check` | exit 0 |

### Mocked/offline vs live — kept strictly separate

**Offline (green above):** the whole route diagnostic (stage order and single
correlation id for a verified run; a pre-send refusal still diagnosed with the
same id; the entitlement facts recorded at `ROUTE_STARTED`; a stripped stored
entity diagnosed from the read-back; missing attribution diagnosed and never
treated as success; bounded fields, no glyph text, no message body, no
credential-shaped field), the neutral panel contract, and every pre-existing
route/gate test.

**Live (NOT executed):** the actual inline query, submission, send and read-back
against Telegram. No mocked test in this task claims server-side behaviour, and
no live result is claimed anywhere in this repository as a result of this task.

## 4. Acceptance criteria

| Criterion (task) | Independently verified? |
|---|---|
| 1. Destination is the owner's Saved Messages | **Offline only** (destination gate + read-back check). **Live: NO** |
| 2. Stored message contains a genuine `MessageEntityCustomEmoji` | **Offline only** (a plain Unicode glyph is never accepted). **Live: NO** — the three earlier attempts observed the entity already absent from the bot's stored answer, which is a *pre-send* observation |
| 3. Document id and entity geometry match the intended payload | **Offline only** (UTF-16, unit-counted). **Live: NO** |
| 4. `via_bot_id` present and equal to the configured helper bot id | **Offline only** (the diagnostic records `via_bot_id` and `expected_via_bot_id` and marks a mismatch). **Live: NO** — `via_bot_id` has still never been observed on a message this project produced |
| 5. The success log is emitted only after the stored message was inspected | **Offline only**, pinned by stage order: the read-back and attribution stages precede the `DIAGNOSIS` line, which is always last. **Live: NO** |

No criterion is claimed as live-verified. A locally passing test, a successful
inline-answer submission and a constructed entity are each explicitly **not**
live success.

## 5. Production status, the live-run blocker, and the smallest live step

* **Nothing was deployed or exposed.** The route is exactly as it was: blocked
  at `INLINE_RESULT_ENTITY_MISSING` before the send.
* **The live step is available from the running deployment with no shell, no
  SSH and no new client**: `Menu` → Emoji → **Send Premium Emoji** → reply to the
  selection message with the custom emoji. The owner-only action is unchanged;
  only its log output changed. Read the result by `run=<id>` as shown in §2.
* **Blocker for this workspace:** no Telegram credentials exist here
  (`API_ID`/`API_HASH`/`SESSION_STRING`/`BOT_OWNER_ID` are absent and there is no
  `.env`), and `backend/config.load()` hard-fails without them — so no request
  could be issued from here. Reported, not improvised around.
* **Not claimed:** that the send fails, that attribution is impossible in
  practice, or that Telegram drops the entity for a named reason. The instrument
  now answers the first two; the third remains undocumented
  (`INVESTIGATION.md` §6).

## 6. Git status

| Step | Value |
|---|---|
| Files in this task's commit | `backend/services/premium_emoji_inline_service.py`, `backend/bot/handlers/emoji.py`, `tests/test_premium_emoji_inline.py`, `INVESTIGATION.md`, `IMPLEMENTATION_REPORT.md` |
| Commit | one commit for the task (`type: description` style, matching the repository) |
| Push | `git push origin HEAD:main` — fast-forward only; no rebase, no force-push, no history rewrite, no unrelated work discarded |
| Remote verification | after the push: `git fetch origin` → `git rev-parse HEAD` = `git rev-parse origin/main` = `git ls-remote origin refs/heads/main`; `git merge-base --is-ancestor <commit> origin/main` exit 0 |

A commit cannot contain its own hash: the commit SHA and the verified
`origin/main` SHA are reported in this task's delivery response, re-checked with
the commands above after the push.
