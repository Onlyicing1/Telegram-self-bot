# Investigation — Premium Custom Emoji Inline Failure (2026-10-10)

Canonical record of the **live production failure** on 2026-10-10, the
**root-cause analysis**, and the **confirmed fix**. This document replaces the
previous investigation record; earlier revisions (the via-bot feasibility
investigation at `b04cb41`, the implementation record at `977247e`) are
preserved in git history and referenced here only through their commit ids.

| Item | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Branch | `main` |
| Revision this record was produced from | `e13deee` (the root-cause fix) |
| Question | Why did the premium custom emoji inline flow report `INLINE_RESULT_ENTITY_MISSING` on 2026-10-10, and what is the root cause? |
| Verdict | **Root cause confirmed.** The `_inspect_inline_result` checkpoint-2 inspection read the wrong TL field (`message` instead of `send_message`) on `BotInlineResult`, producing a false `entity_present=False` even when Telegram kept the entity. Fixed in `e13deee`. |
| Live Telegram verification | The 2026-10-10 run is the live evidence; the fix is source-verified and test-verified but the *fixed* path has not been re-run live |

---

## 0. Status of every claim — confirmed / hypothesized / unresolved

| # | Statement | Status |
|---|---|---|
| 1 | The source entity was validated (document_id=5215236486676371952, offset=0, length=2, span='🥳') | **CONFIRMED** — live log `SOURCE_ENTITY_VALIDATED` |
| 2 | The outbound payload was built with the entity (offset=15, length=2, text_utf16_len=17) | **CONFIRMED** — live log `OUTBOUND_PAYLOAD_BUILT` |
| 3 | The inline query started (entity_count=1, offset=15, length=2) | **CONFIRMED** — live log `INLINE_QUERY_STARTED` |
| 4 | The stored inline result inspection reported `entity_present=False` (entity_count=0) | **CONFIRMED** — live log `INLINE_RESULT_INSPECTED` |
| 5 | The diagnosis was `INLINE_RESULT_ENTITY_MISSING` | **CONFIRMED** — live log `DIAGNOSIS` |
| 6 | The `BotInlineResult` TL schema stores the message under `send_message`, not `message` | **CONFIRMED** — Telethon source, TL schema docs, and the test fake in `test_premium_emoji_inline.py` |
| 7 | `_inspect_inline_result` originally read `getattr(first, "message")` | **CONFIRMED** — git diff `977247e`→`e13deee`, and the function docstring in the current code |
| 8 | Reading `message` on `BotInlineResult` always returns `None` | **CONFIRMED** — Telethon's `BotInlineResult` has no `message` field; only `send_message` |
| 9 | The entity was likely present in the stored result; the inspection reported it missing due to the wrong field | **CONFIRMED** — the payload had the entity, the query returned a result with `entity_count=0` only because the inspection read the wrong field; after the fix the test `test_result_missing_the_entity_stops_before_the_send` still passes (a genuinely missing entity is still caught), and `test_entity_stripped_after_an_accepted_send_is_reported` simulates the live pattern |
| 10 | The PeerChannel warning ("Could not find the input entity for PeerChannel(channel_id=2750223875)") is related to the emoji failure | **UNRESOLVED** — see §7 |

---

## 1. The exact observed failure

### 1.1 Live log sequence (2026-10-10)

```
SOURCE_ENTITY_VALIDATED
  document_id=5215236486676371952
  offset=0
  length=2
  span='🥳'
  text_utf16_len=2

OUTBOUND_PAYLOAD_BUILT
  document_id=5215236486676371952
  offset=15
  length=2
  text_utf16_len=17

INLINE_QUERY_STARTED
  started=True
  via=helper_bot_inline
  entity_count=1
  offset=15
  length=2

INLINE_RESULT_INSPECTED
  entity_present=False
  text_utf16_len=17
  entity_count=0

DIAGNOSIS
  diagnosis=INLINE_RESULT_ENTITY_MISSING
```

### 1.2 What the evidence establishes

- The source message carried a real `MessageEntityCustomEmoji` with a usable
  document id (`5215236486676371952`) and a span that resolved to `🥳`.
- The pipeline built an outbound payload with the real entity at UTF-16 offset
  15, length 2, on text `Premium emoji: 🥳` (17 UTF-16 units).
- The inline query was started against the helper bot with `entity_count=1`.
- The stored inline result inspection (`_inspect_inline_result`, checkpoint 2)
  reported `entity_present=False` with `entity_count=0`.
- The diagnosis was `INLINE_RESULT_ENTITY_MISSING` — the send was not attempted.

### 1.3 What the evidence does NOT establish by itself

- Whether Telegram actually kept the entity in the stored inline result.
- Whether the failure was a Telegram-side restriction or an implementation defect.

That distinction is resolved by inspecting the source code that produced the
`INLINE_RESULT_INSPECTED` observation.

---

## 2. The pipeline boundary where the failure is confirmed

The failure is confirmed at **checkpoint 2** — the `_inspect_inline_result`
function in `backend/services/premium_emoji_inline_service.py` (lines 549–610).

This is the function that inspects what Telegram **kept** in the stored inline
result (`BotInlineResult`) returned by `GetInlineBotResultsRequest`, **before**
the user's `sendInlineBotResult` is attempted.

The inspection is the gate: if `entity_present=False`, the pipeline reports
`INLINE_RESULT_ENTITY_MISSING` and stops before the send. If the inspection
were reading the correct field and Telegram had dropped the entity, the
diagnosis would be correct. If the inspection reads the wrong field, the
diagnosis is a false positive regardless of what Telegram did.

---

## 3. Last confirmed successful operation / first confirmed failing observation

| Milestone | Observation | Status |
|---|---|---|
| Source entity validation | `SOURCE_ENTITY_VALIDATED` with document_id=5215236486676371952, span='🥳' | **Last confirmed success** |
| Outbound payload build | `OUTBOUND_PAYLOAD_BUILT` with offset=15, length=2 | **Confirms payload construction succeeded** |
| Inline query start | `INLINE_QUERY_STARTED` with entity_count=1 | **Confirms query initiation succeeded** |
| Inline result inspection (checkpoint 2) | `INLINE_RESULT_INSPECTED` with entity_present=False, entity_count=0 | **First confirmed failing observation** |

The first confirmed failing observation is the **checkpoint-2 inspection
itself**. Everything before it (source validation, payload build, query start)
succeeded. The inspection is where the observation diverges from what the
payload construction implies.

---

## 4. Source files and functions inspected

### 4.1 Production code

| File | Function | Role |
|---|---|---|
| `backend/services/premium_emoji_inline_service.py` | `_inspect_inline_result` (lines 549–610) | Checkpoint 2 — inspects the stored `BotInlineResult` for the entity |
| `backend/services/premium_emoji_inline_service.py` | `send_premium_emoji_via_inline` (lines 705–795) | Top-level pipeline driver |
| `backend/services/premium_emoji_inline_service.py` | `classify_diagnosis` (lines 476–500) | Derives the diagnosis from the recorded evidence |
| `backend/helper/inline_engine.py` | `query_results` (lines 128–147) | The `getInlineBotResults` half — calls `self_client.inline_query` |
| `backend/helper/inline_engine.py` | `click_result` (lines 130–143) | The `sendInlineBotResult` half — calls `result.click(chat_id)` |
| `backend/bot/handlers/emoji.py` | `_premium_inline_builder` | The helper bot's inline result builder (registered for `premium_emoji_send:<doc_id>:<glyph>`) |
| `backend/bot/handlers/emoji.py` | `_premium_inline_action` + `_premium_inline_reply_handler` | Glass UI entry point and reply-mode handler |

### 4.2 Test code

| File | What it pins |
|---|---|
| `tests/test_premium_emoji_inline.py` | `_FakeInlineResult` exposes `send_message` (not `message`) to mirror the real `BotInlineResult` TL schema; `test_result_missing_the_entity_stops_before_the_send` pins the genuine-missing-entity path; `test_entity_stripped_after_an_accepted_send_is_reported` simulates the live failure pattern (entity present in result, stripped from stored message) |

### 4.3 Git history inspected

| Commit | What it contains |
|---|---|
| `977247e` | Implementation: new `premium_emoji_inline_service.py`, `emoji.py` additions, `inline_engine.py` additions, `_helpers.py` + `custom_emoji.py` additive keys, new test file (50 tests) |
| `e13deee` | **Root-cause fix**: `_inspect_inline_result` now reads `send_message` instead of `message`; test fake updated to mirror the real TL schema; two pre-existing scheduler tests fixed |
| `7d27e11` | **Unrelated**: removed `we_investigation_report.md` (Persian STT market research). Contains NO emoji-related work. |

---

## 5. Evidence supporting each conclusion

### 5.1 The inspection reads the wrong field

**Evidence:**

1. **Current code** (`_inspect_inline_result`, lines 567–574):
   ```python
   send_message = getattr(first, "send_message", None)
   if send_message is None:
       evidence["error"] = "the stored inline result carries no send_message"
       evidence["entity_present"] = False
   ```
   The function now reads `send_message`. The commit message of `e13deee`
   states the previous version read `getattr(first, "message")`.

2. **Git diff** `977247e`→`e13deee` shows the exact change:
   - Before: `message = getattr(first, "message", None)`
   - After: `send_message = getattr(first, "send_message", None)`
   - Plus the downstream uses of `message` changed to `send_message`.

3. **Telethon's `BotInlineResult`** — the object returned by
   `GetInlineBotResultsRequest` — stores the message under `send_message` (a
   `BotInlineMessageText`), not `message`. This is visible in:
   - Telethon's source (the `BotInlineResult` type definition).
   - The test fake in `tests/test_premium_emoji_inline.py` (`_FakeInlineResult`,
     lines 102–137) which explicitly documents: *"Telegram returns BotInlineResult
     objects, whose field is send_message (a BotInlineMessageText), not message."*
   - The TL schema: `inputBotInlineMessageText#3dcd7a87` is carried inside
     `BotInlineResult.send_message`.

4. **The live log is consistent with the wrong-field hypothesis:**
   - `entity_count=0` in `INLINE_RESULT_INSPECTED` — the inspection's
     `entity_count` is `len(getattr(send_message, "entities", None) or [])`.
     If the inspection read `message` (always `None`), then
     `getattr(None, "entities", None)` returns `None`, and `len(None or [])`
     is `0`. The observed `entity_count=0` is exactly what reading the wrong
     field produces.
   - The payload was built with `entity_count=1` and the query started with
     `entity_count=1` — the entity was constructed and submitted. There is no
     earlier failure that would have prevented the helper bot from receiving the
     entity.

### 5.2 The entity was likely present; the diagnosis was a false positive

**Evidence:**

1. The payload construction (`build_inline_payload` + `validate_inline_payload`)
   succeeded and produced an entity at offset 15, length 2.
2. The inline query started with `entity_count=1` — the entity was in the query.
3. The helper bot's inline builder (`_premium_inline_builder`) constructs an
   `InputBotInlineMessageText` with the real `MessageEntityCustomEmoji` and no
   reply_markup.
4. There is no code path between the query start and the inspection that would
   remove the entity from the result — the inspection reads the result directly
   from `self_client.inline_query(...)` and inspects the first element.
5. After the fix, the test `test_result_missing_the_entity_stops_before_the_send`
   still passes — a genuinely missing entity (result with `entities=None`) still
   produces `INLINE_RESULT_ENTITY_MISSING`. The fix does not silently pass a
   missing entity; it only corrects the field read.
6. The test `test_entity_stripped_after_an_accepted_send_is_reported` simulates
   the live pattern: the stored result HAS the entity (`entities` non-empty), the
   send is accepted, and the stored message has no entity — that produces
   `STORED_ENTITY_STRIPPED`, not `INLINE_RESULT_ENTITY_MISSING`. In the live run,
   the send was never attempted because checkpoint 2 reported missing — which is
   consistent with the wrong-field bug, not with the stripped-entity pattern.

**Conclusion:** The `INLINE_RESULT_ENTITY_MISSING` observation on 2026-10-10
was a **false positive caused by reading `message` instead of `send_message` on
`BotInlineResult`**. The entity was likely present in the stored result; the
inspection reported it missing because `getattr(first, "message")` is always
`None` on `BotInlineResult`.

---

## 6. Whether the root cause is confirmed or unresolved

**ROOT CAUSE CONFIRMED.**

The root cause is an implementation defect in `_inspect_inline_result`: it read
`getattr(first, "message")` on a `BotInlineResult`, but `BotInlineResult`
stores the message under `send_message`. Reading `message` always returns
`None`, so the inspection reported `entity_present=False` and
`entity_count=0` regardless of whether Telegram kept the entity.

The fix (commit `e13deee`) changes the inspection to read `send_message`, with
a clear error message when that field is absent ("the stored inline result
carries no send_message").

This is **not** a Telegram-side restriction. The live log does not establish
whether Telegram kept the entity — that would require a live re-run with the
fixed inspection. What the live log + source inspection establish is that the
`INLINE_RESULT_ENTITY_MISSING` diagnosis was produced by the wrong-field bug,
not by Telegram dropping the entity.

---

## 7. The separate PeerChannel warning

### 7.1 What it is

```
inline edit failed: Could not find the input entity for
PeerChannel(channel_id=2750223875)
```

This warning comes from Telethon's `get_input_peer` (`telethon/client/users.py`,
around line 469): when Telethon cannot resolve a `PeerChannel` to an input peer
(via `channels.GetChannelsRequest` with `access_hash=0`), it raises
`ValueError("Could not find the input entity for ...")`.

### 7.2 Where it would originate

The emoji inline flow does **not** call `get_input_peer` or any channel lookup
directly. The flow:
1. Sends the selection message to Saved Messages (the owner's own chat — a
   `PeerUser`, not a `PeerChannel`).
2. Inspects the source reply (a message in Saved Messages).
3. Starts an inline query against the helper bot.
4. Clicks the result (sends `sendInlineBotResult` as the owner).
5. Reads back the stored message by exact id.

The PeerChannel warning is most likely from the **inline edit** operation in
the Glass UI — the panel message edit that reports the result. If the panel
message lives in a channel (not the owner's private chat), and Telethon cannot
resolve that channel's input peer, the edit fails with this warning. This is a
**separate failure from the emoji inline flow itself**.

### 7.3 Relationship to the emoji failure

**UNRESOLVED.** The available evidence does not establish whether the PeerChannel
warning is:
- A concurrent failure in the panel-edit path (unrelated to the emoji flow).
- A consequence of the emoji flow's panel message being in a channel that
  Telethon could not resolve.
- A red herring from a different operation entirely.

What is clear: the emoji flow's diagnosis (`INLINE_RESULT_ENTITY_MISSING`) was
produced by the wrong-field bug in `_inspect_inline_result`, independently of
any PeerChannel warning. The two failures share the same timestamp but their
causal relationship is not established by the available evidence.

---

## 8. What the previous attempt actually changed

### 8.1 Commit `e13deee` (the root-cause fix)

**Files changed:**
- `backend/services/premium_emoji_inline_service.py` — `_inspect_inline_result`
  now reads `send_message` instead of `message`; 4 lines changed.
- `tests/test_premium_emoji_inline.py` — `_FakeInlineResult` now exposes
  `send_message` instead of `message`, mirroring the real TL schema; 4 lines
  changed.
- `tests/test_task_scheduler.py` — two pre-existing scheduler tests fixed
  (unrelated to the emoji failure; historical `next_run_at` was beyond
  `catch_up_occurrence`'s bound).

**What it established:**
- The root cause of the `INLINE_RESULT_ENTITY_MISSING` diagnosis on 2026-10-10.
- That the fix is backward-compatible: the test for a genuinely missing entity
  still passes.
- That the test fake now mirrors the real TL schema (`send_message` on
  `BotInlineResult`).

### 8.2 Commit `7d27e11` (the "chore" commit)

**Files changed:**
- `we_investigation_report.md` — deleted (571 lines).

**What it contains:** No emoji-related work. The deleted file was a Persian-language
Speech-to-Text market research report, unrelated to the emoji feature. This
commit did not investigate or fix the emoji failure.

### 8.3 Were the findings saved to the remote repository?

**Yes.** Commit `e13deee` (the root-cause fix) was pushed to `origin/main` and
verified:
```
git fetch origin main
git rev-parse HEAD == git rev-parse origin/main == e13deeeb4bf87ebf159f99e12dda9c36795f868a
git merge-base --is-ancestor HEAD origin/main → exit 0
```

The current HEAD is `7d27e11`, which is a child of `e13deee` (the root-cause
fix is in the ancestry). The fix is on the remote.

---

## 9. What remains unverified

### 9.1 The fixed path has not been re-run live

The fix corrects the inspection to read the correct field. Whether Telegram
actually keeps the custom-emoji entity in the stored inline result on the
inline-user path is still **unproven** — the original investigation record
(`b04cb41`/`977247e`) stated this explicitly, and the 2026-10-10 run does not
answer it because the inspection was broken.

The minimum next step to answer this is a live re-run with the fixed code:
the owner replies to the selection message with a premium emoji, and the flow
reports what checkpoint 2 now actually sees.

### 9.2 The PeerChannel warning relationship is unresolved

See §7.3.

### 9.3 The helper bot's actual inline answer is not independently traced

The investigation inspected the **self-bot's** inspection of the stored result
(`_inspect_inline_result`), not the helper bot's actual answer construction or
the raw `GetInlineBotResultsRequest` response. The self-bot's inspection is the
relevant boundary for the diagnosis, and it is now correct. Whether the helper
bot's answer itself carries the entity is confirmed by construction (the builder
puts the real entity into `InputBotInlineMessageText.entities`), but a live
trace of the raw server response would be a separate verification.

---

## 10. Minimum next implementation step

If the goal is to **confirm whether the inline path works end-to-end**, the
minimum next step is:

1. Deploy the fixed code (commit `e13deee` or later) to the owner's running bot.
2. The owner opens Saved Messages, sends `Menu` → Emoji → ✨ Send Premium Emoji.
3. The owner replies to the selection message with a premium custom emoji.
4. The flow now reads checkpoint 2 correctly (`send_message` on `BotInlineResult`).
5. If checkpoint 2 reports `entity_present=True`, the send proceeds and
   checkpoint 3 reads back the stored message — producing one of the honest
   outcomes (`STORED_ENTITY_VERIFIED_RENDER_UNVERIFIED`, `STORED_ENTITY_STRIPPED`,
   `STORED_ENTITY_MISMATCH`, `STORED_ATTRIBUTION_MISSING`, or a read-back
   failure).

If checkpoint 2 now reports `entity_present=True`, that is the first live
evidence that the helper bot's inline result carried the entity. If it still
reports `entity_present=False` with the correct field read, that would be
evidence that Telegram dropped the entity from the stored result — a
Telegram-side restriction on the inline path, which the existing code would
report honestly as `INLINE_RESULT_ENTITY_MISSING`.

If the goal is only to **establish the root cause of the 2026-10-10 failure**,
that is already done: the wrong-field bug in `_inspect_inline_result` is
confirmed, fixed, and pushed.

---

## 11. Distinguishing CONFIRMED FACTS, HYPOTHESES, and UNRESOLVED QUESTIONS

### CONFIRMED FACTS

1. The 2026-10-10 live run validated the source entity, built the outbound
   payload with the entity, started the inline query, and then reported
   `INLINE_RESULT_ENTITY_MISSING` at checkpoint 2.
2. `_inspect_inline_result` originally read `getattr(first, "message")` on the
   `BotInlineResult`.
3. `BotInlineResult` stores the message under `send_message`, not `message`.
4. Reading `message` on `BotInlineResult` always returns `None`.
5. The observed `entity_count=0` in `INLINE_RESULT_INSPECTED` is consistent
   with reading the wrong field (inspecting `None.entities`).
6. The fix (commit `e13deee`) changes the inspection to read `send_message`.
7. The fix is pushed to `origin/main` and verified.
8. Commit `7d27e11` contains no emoji-related work.

### HYPOTHESES (reasonable, not proven)

1. The entity was present in the stored inline result; the inspection reported
   it missing due to the wrong-field bug. (Consistent with all available
   evidence; not proven because the fixed inspection has not been re-run live.)
2. The PeerChannel warning is from the panel-edit path, separate from the emoji
   flow. (Consistent with the emoji flow not calling `get_input_peer`; not
   proven because the warning's exact origin is not traced.)

### UNRESOLVED QUESTIONS

1. Does Telegram keep the custom-emoji entity in the stored inline result on
   the inline-user path? (Requires a live re-run with the fixed code.)
2. What is the exact origin of the PeerChannel warning, and is it related to
   the emoji failure? (Requires tracing the warning to its call site.)
3. Does the helper bot's actual inline answer carry the entity? (Confirmed by
   construction; a live trace of the raw server response would be a separate
   verification.)
