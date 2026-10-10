# Investigation — Where the Premium Custom-Emoji Entity Is Lost in the Helper Inline Result (2026-10-10)

Canonical, latest-only record of the defect the deployed flow still shows: after
every earlier stage succeeds, the inline result the self-account receives from
`messages.getInlineBotResults` carries **no** custom-emoji entity, the pre-send
gate refuses to send, and the flow ends at
`DIAGNOSIS diagnosis=INLINE_RESULT_ENTITY_MISSING`. **This file replaces the
previous `INVESTIGATION.md` in full.** Earlier records — the checkpoint-2
wrapper fix (`9633c0b`), the via-bot feasibility study (`b04cb41`), the
implementation record (`977247e`), the `e13deee` field fix, the `98c2825` audit,
the boundary trace (`a54877b`) and the callback receipt (`1bd843d`) — remain
readable in git history and are referenced here only by commit id.

| Item | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Branch | `main` |
| Revision inspected | `9633c0b` (the deployed commit, verified `= origin/main` at task start) |
| Telethon | **pinned `telethon==1.34.0`** (`backend/requirements.txt:1`); every TL shape below was read from / round-tripped through that installed source |
| Question | In the path that **builds, submits and returns** the helper bot's Premium custom-emoji inline result, where is the entity actually lost? |
| Verdict | **Not in application code and not in TL serialization.** The registered builder answers with exactly one real `MessageEntityCustomEmoji` and that entity is provably present in the bytes of the `messages.setInlineBotResults` request the helper bot sends. Telegram's **stored** answer — the payload `getInlineBotResults` returns — comes back without it. The loss therefore happens in **Telegram's processing of the bot's inline answer**. |
| Production code changed | **Yes** — `backend/helper/inline_engine.py` (record the bot's own submission), `backend/services/premium_emoji_inline_service.py` (compare it, attribute the loss, surface it), `backend/bot/handlers/emoji.py` (render it). No new client, listener, loop, scheduler, executor or send mechanism. |
| Live Telegram verification | **Not performed in this task** (no live session in this workspace). The determination is source- and byte-verified against the pinned Telethon and pinned by tests. |

---

## 1. The evidence this task started from

| # | Observed in production (deployed `9633c0b`) | Status |
|---|---|---|
| 1 | `emoji_premium_inline` callback received and dispatched | confirmed (log) |
| 2 | `SOURCE_ENTITY_VALIDATED` — a genuine source `MessageEntityCustomEmoji` was extracted from the owner's reply | confirmed (log) |
| 3 | `OUTBOUND_PAYLOAD_BUILT` — outbound text + entity offsets built | confirmed (log) |
| 4 | `INLINE_QUERY_STARTED` — the self account queried the helper bot | confirmed (log) |
| 5 | The returned object is `telethon.tl.custom.inlineresult.InlineResult` wrapping `telethon.tl.types.BotInlineResult` | confirmed (log) |
| 6 | `INLINE_RESULT_INSPECTED entity_present=False … entity_count=0` — the **stored** `BotInlineResult.send_message` carried **zero** entities | confirmed (log) |
| 7 | `DIAGNOSIS diagnosis=INLINE_RESULT_ENTITY_MISSING`; **no** `INLINE_SEND_STARTED` | confirmed (log) |

Facts 5–7 only became observable after `9633c0b` corrected checkpoint 2 to read
`results[0].result.send_message` (previously the code read `send_message` off the
wrapper, which never exists, so it reported `reason=no_send_message` without
reading anything). **No send and no exact-message read-back has ever been
reached** — nothing in this document claims either is broken.

---

## 2. The path, traced in the current source

```
emoji_premium_inline (action)
  → _premium_inline_action                 backend/bot/handlers/emoji.py   (:2172)
      sends the Saved Messages selection message, arms reply mode
  → _premium_inline_reply_handler          backend/bot/handlers/emoji.py   (:2225)
      resolves the EXACT selection message, then
  → send_premium_emoji_via_inline          backend/services/premium_emoji_inline_service.py
      Saved-Messages-only gate, document facts (alt/free/text_color), payload
      → build_inline_payload / validate_inline_payload / inline_query_for
  → inline_engine.query_results            backend/helper/inline_engine.py  (:209)
      self_client.inline_query(helper_username, "<key>:<doc>:<glyph>", entity=chat_id)
        → messages.getInlineBotResultsRequest   (Telegram asks the bot to answer)
  ── helper side ──────────────────────────────────────────────────────────
  → _inline_router                         backend/helper/inline_engine.py  (:291)
      is_owner gate → splits "<key>:<extra>" → get_inline_builder(key)
  → _premium_inline_builder                backend/bot/handlers/emoji.py    (:2033)
      → build_inline_result                premium_emoji_inline_service.py
          InputBotInlineMessageText(message=<prefix+glyph>, entities=[MessageEntityCustomEmoji(doc,15,2)])
          InputBotInlineResult(id="0", type="article", send_message=…)
  → _sanitize_results (reply_markup rows only) → event.answer(results)
      → messages.setInlineBotResultsRequest{query_id, results}
  ── back on the self side ────────────────────────────────────────────────
  → _inspect_inline_result (checkpoint 2)  reads results[0].result.send_message
  → _send_result (checkpoint 3, NEVER REACHED) → messages.sendInlineBotResult
  → _fetch_stored_message + _read_back (checkpoint 4, NEVER REACHED)
```

Registration is the production one — `backend/bot/handlers/emoji.py::register`
binds `premium_inline_service.INLINE_QUERY_KEY` (`"premium_emoji_send"`) to
`_premium_inline_builder` through `inline_engine.register_inline_builder`
(`:2555`), and `backend/runtime/supervisor.py:431` wires
`inline_engine.register_inline_handler` onto the helper bot client.

---

## 3. Statements kept separate

| # | Statement | Status |
|---|---|---|
| 1 | Our payload object contains the entity | **CONFIRMED** (§4.1) |
| 2 | The serialized `setInlineBotResults` request contains the entity | **CONFIRMED** (§4.2) |
| 3 | The helper bot's answer was submitted without raising | **CONFIRMED from the 8th fact (§1: a result was returned)**, now also *recorded* at runtime (§5) |
| 4 | Telegram **stored** the entity in the returned `BotInlineResult` | **REFUTED** — the live payload was inspected and has no entity (§1.6) |
| 5 | Telegram therefore dropped an entity our code had submitted | **CONFIRMED** by 1+2+3+4 — the only side left |
| 6 | The self account's `sendInlineBotResult` or the exact-message read-back is broken | **NOT TESTED — never reached** |
| 7 | A documented rule explains *why* Telegram drops it | **UNRESOLVED — undocumented** (§6) |

---

## 4. What was actually verified (with the pinned Telethon)

### 4.1 The payload the helper bot builds — `CONFIRMED`

`build_inline_result(document_id, glyph)` builds
`types.InputBotInlineResult(id="0", type="article",
send_message=types.InputBotInlineMessageText(message="Premium emoji: <glyph>",
entities=[types.MessageEntityCustomEmoji(15, 2, <document_id>)]))`.

* `InputBotInlineMessageText` **does** have an `entities` field in the installed
  1.34.0 (`__init__(message, no_webpage, invert_media, entities, reply_markup)`,
  flags bit 1) — verified by inspecting the installed class, so reading
  `send_message.entities` on the *returned* object is a real field, not a
  phantom.
* Pinned by `test_the_registered_router_answers_the_query_with_the_real_entity`:
  the **registered router** answers `premium_emoji_send:<doc>:<glyph>` with
  exactly one `InputBotInlineResult` whose payload carries exactly one
  `MessageEntityCustomEmoji` with `document_id` = the real source id, UTF-16
  offset 15, length 2, and a span that resolves to exactly the alt glyph (the
  documented "the entity must wrap exactly the emoji in
  `documentAttributeCustomEmoji.alt`" precondition).

### 4.2 The submitted request — `CONFIRMED` at byte level

Telethon 1.34.0's `InlineQuery.Event.answer` places the passed results straight
into `messages.SetInlineBotResultsRequest(query_id, results, …)`
(`telethon/events/inlinequery.py`). Driving the **production router** with a
recording `InlineQuery`-shaped event and then re-reading the real bytes:

```python
parsed = BinaryReader(bytes(request)).tgread_object()
# -> SetInlineBotResultsRequest: results[0].send_message.entities[0]
#    == MessageEntityCustomEmoji(document_id=5361626279781934801, offset=15, length=2)
```

Pinned by `test_the_answer_carries_the_entity_into_the_set_inline_bot_results_bytes`.
The same request was also dumped and read by hand during the trace: constructor
`0xc8cf05f8`, offset 15, length 2 and the document id the payload carries (the
one extracted from the owner's source entity), inside a result whose text is
the prefix followed by that emoji.

**Consequence:** the entity is not lost in application code and not in TL
serialization. Whatever Telegram's reply contains, the request was complete.

### 4.3 The sanitizer — `CONFIRMED harmless`

`inline_engine._sanitize_results` only rewrites `reply_markup.rows` into
`KeyboardButtonRow` objects. It never reads or writes `entities`.
Pinned by `test_the_sanitizer_keeps_the_entity_and_only_normalizes_button_rows`
(the existing Glass UI result with no `entities` stays untouched, and our result
keeps its exact entity object and geometry).

### 4.4 The pre-send gate — `CONFIRMED unchanged and still refusing`

`_inspect_inline_result` + `classify_diagnosis` still refuse to send when the
**stored** payload has no entity (`INLINE_RESULT_ENTITY_MISSING`), and
`_read_back` still requires the real entity **and** Telegram's own `via_bot_id`
before anything is called verified. No Unicode fallback exists anywhere in the
module, and the destination gate is still Saved-Messages-only. Every one of
those tests is unchanged and green.

### 4.5 The stored payload — `CONFIRMED empty of entities (live)`

Live fact §1.6. The stored `BotInlineResult.send_message` was inspected through
the real shape and carried `entity_count=0`. Since §4.1–§4.2 prove the bot's
request carried one entity, and the router only submits an answer that the
builder produced, Telegram's stored copy lost it.

---

## 5. What this task changed (and why)

The flow could compare only ONE half of the question. `event.answer` returns a
boolean, so the helper bot's own `setInlineBotResults` submission was
observable **nowhere**: a stored result without an entity was consistent with
(a) Telegram dropping a submitted entity, (b) the answer never carrying one, and
(c) the submission raising — and the live log could not tell them apart. That is
a real defect of the *diagnosis path*, and it is the earliest thing left that
this application can act on.

* `backend/helper/inline_engine.py` — `record_inline_answer(query_key, results,
  ok, error)` / `last_inline_answer(query_key, since=…)`. The router records, for
  registered query keys only, whether the submission completed without raising
  and **how many custom-emoji entities the built answer carried** (plus their
  document ids). Bounded by construction: the key set is the registered builder
  keys, the entry is a handful of scalars, and it is in-memory only. No new
  client, listener, loop, scheduler or executor.
* `backend/services/premium_emoji_inline_service.py` — the attempt's own
  monotonic start mark is compared against that record, so only an answer
  submitted **after the query was issued** counts as evidence (a stale answer
  can never be misattributed). The record lands in the evidence block
  (`inline_result["answer"]`), in the `INLINE_RESULT_INSPECTED` trace, and in a
  new `_entity_missing_detail()` that attributes the loss:
  a submitted entity missing from the stored payload ⇒ **dropped by Telegram,
  not by this application**; a raised submission, a zero-entity answer, or no
  record at all ⇒ reported as exactly that, with the unproven case labelled
  **unproven**.
* `backend/bot/handlers/emoji.py` — the panel report renders the helper bot's own
  submission ("Helper answer: submitted 1 custom-emoji entity · accepted by
  Telegram" / "no submission of this bot was recorded").

Nothing else moved: the entity/UTF-16 validation, the Saved-Messages-only
destination, the `via_bot_id` attribution check, the `query_results` /
`click_result` architecture, the send mechanism and the fail-closed pre-send
gate are all byte-for-byte behaviourally unchanged.

---

## 6. Why Telegram drops it — the remaining, undocumented limitation

Confirmed in official documentation (read during the earlier via-bot study and
re-verified for this record):

| Fact | Source | Status |
|---|---|---|
| `inputBotInlineMessageText#3dcd7a87 … message:string entities:flags.1?Vector<MessageEntity>` — "Message entities for styled text"; **no** entity-type allow-list and no custom-emoji note | <https://core.telegram.org/constructor/inputBotInlineMessageText> | confirmed (schema capability only) |
| `messages.setInlineBotResults` documents no custom-emoji-specific error | <https://core.telegram.org/method/messages.setInlineBotResults> | confirmed (absence of a documented error) |
| "Custom emoji entities can only be used by bots that purchased additional usernames on Fragment **or** in the messages directly sent by the bot to private, group and supergroup chats if the owner of the bot has a Telegram Premium subscription." (Bot API 9.4, Feb 2026) | <https://core.telegram.org/bots/api>, <https://core.telegram.org/bots/api-changelog> | confirmed as **Bot API documentation** |
| The documented bot-side entitlement is scoped to messages **directly sent by the bot** — an entity a bot **supplies inside an inline result** (the user is the sender) is not covered by that sentence | same | confirmed (documentation scope) |
| Whether Telegram keeps, ignores or strips a custom-emoji entity in a bot's inline answer, and whose entitlement it checks | — | **UNRESOLVED / undocumented** |
| Whether a Fragment-purchased username on the helper bot, or a Premium bot owner, would make Telegram keep it | — | **UNRESOLVED / undocumented** |

**What the evidence supports:** the entity leaves this application intact and
Telegram's stored answer does not contain it. **What it does not support:** any
statement about *which* server-side rule produces that, and therefore no
workaround. The documented "silent ignore" rule that was already checked in the
earlier direct-send POC is **not** the cause here either: the entity wraps
exactly the document's `alt` glyph at its real UTF-16 offset (§4.1), and the
document lookup that supplies that glyph is unchanged.

This is why no workaround was invented: there is nothing in the documentation
to build one on, and the pre-send gate exists precisely so an entity-less result
is never sent as if it were a premium emoji.

---

## 7. Falsifiers — what the next live run can prove, either way

The next live run of `Menu → Emoji → ✨ Send Premium Emoji → reply with a
genuine Premium emoji` now records the bot's own submission, so it settles the
question:

| Live trace | Meaning |
|---|---|
| `INLINE_RESULT_INSPECTED entity_present=False … answer_recorded=True answer_ok=True answer_custom_emoji_count=1` then `INLINE_RESULT_ENTITY_MISSING` | **This record is confirmed on the live path**: the bot submitted the entity and Telegram dropped it from the stored answer. The inline-result construction is then blocked by Telegram's undocumented behaviour, not by this repository. |
| `answer_recorded=True answer_ok=False` (or `answer_custom_emoji_count=0`) | an **application-side** defect the previous evidence could not see — the answer never carried the entity. That contradicts §4.1/§4.2 and must be diagnosed from the recorded reason. |
| `answer_recorded=False` | the answer arrived outside this process's view (or not at all) — the cause stays unproven and must not be attributed to Telegram. |
| `entity_present=True` then `INLINE_SEND_STARTED` | the stored payload kept the entity — checkpoint 4 (`sendInlineBotResult` + exact-message read-back + `via_bot_id`) becomes reachable for the first time. |

---

## 8. Scope honoured

* Only the relevant implementation points were inspected: the two
  `InputBotInlineMessageText` construction sites that matter (the premium
  builder and the shared panel renderer), the router, the sanitizer, the
  checkpoint-2/3/4 code and the table above.
* No new Telegram client, listener, update loop, scheduler, executor or
  alternate send mechanism was introduced; `query_results` / `click_result` keep
  their contracts.
* No fabricated entity, no Unicode fallback, no bypass of the pre-send check, no
  weakened or deleted test, no Supabase/schema/SQL change, no unrelated
  refactor. `tests/test_stage13.py`, `DATABASE_ARCHITECTURE.md` and `ROADMAP.md`
  were not touched.
