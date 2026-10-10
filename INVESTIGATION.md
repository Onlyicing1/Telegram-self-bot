# Investigation — Where the Premium Custom-Emoji Entity Is Lost in the Helper Inline Result (2026-10-10)

Canonical, latest-only record. **This file replaces the previous `INVESTIGATION.md`
in full.** Earlier records — the checkpoint-2 field/wrapper fixes (`e13deee`,
`9633c0b`), the attribution commit (`b463552`), the forensic audit (`98c2825`),
the boundary trace (`a54877b`), the callback receipt (`1bd843d`), the feature
commit (`977247e`) and the earlier render-boundary investigation (`06eeb8d`) —
remain readable in git history and are referenced here only by commit id.

| Item | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Branch | `main` |
| Revision inspected | `39de9cc` — fetched, verified `= origin/main = git ls-remote` at task start; the workspace was fast-forwarded from `06eeb8d` to it (`--ff-only`, no rewrite) |
| Deployed implementation | the inline feature chain `977247e … b463552` (the last code commit; `39de9cc` is docs-only on top of it) |
| Telethon | **pinned `telethon==1.34.0`** (`backend/requirements.txt:1`), **layer 173** — every TL shape below was read from / round-tripped through that installed source |
| Question | After the helper bot's answer is *submitted*, where is the custom-emoji entity actually lost, and what can be done next to make delivery work? |
| Verdict | **The loss is on Telegram's side of the bot's answer, not in this application — and the previously unproven part of that claim is now correlated by entity identity, not by a count.** The registered builder answers with exactly one real `MessageEntityCustomEmoji`; that entity is provably inside the bytes of the `messages.setInlineBotResults` request; and a stored `botInlineResult` carrying the entity parses back intact through the same pinned Telethon. Telegram's *returned* `botInlineMessageText` carries the text and no entity. |
| Production code changed | **One narrow diagnostic fix** (`backend/services/premium_emoji_inline_service.py`: the "dropped by Telegram" verdict now requires the recorded submission to have carried *this emoji's* `document_id`, and the trace records that correlation). No delivery-path change, no new client/loop/listener/scheduler/executor, no fallback. |
| Live Telegram verification | **Not performed in this task** (this workspace holds no `SESSION_STRING`/`BOT_TOKEN`; `freebuff-env list` shows no credentials). Everything below is source-, byte- and test-verified against the pinned Telethon, and is labelled as such. |

---

## 1. The evidence this task started from

| # | Observed in production | Status |
|---|---|---|
| 1 | `emoji_premium_inline` callback received and dispatched; the reply resolved to the exact selection message | confirmed (log) |
| 2 | `SOURCE_ENTITY_VALIDATED` — a genuine source `MessageEntityCustomEmoji` was extracted from the owner's reply | confirmed (log) |
| 3 | `OUTBOUND_PAYLOAD_BUILT` — outbound text + entity offsets built | confirmed (log) |
| 4 | `INLINE_QUERY_STARTED` — the self account queried the helper bot | confirmed (log) |
| 5 | The returned object is `telethon.tl.custom.inlineresult.InlineResult` wrapping `telethon.tl.types.BotInlineResult` | confirmed (log) |
| 6 | `INLINE_RESULT_INSPECTED entity_present=False … entity_count=0` — the **stored** `BotInlineResult.send_message` carried **zero** entities while the text was present | confirmed (log) |
| 7 | `answer_ok=True answer_custom_emoji_count=1` — the helper bot's **own** `setInlineBotResults` submission completed and carried one custom-emoji entity | confirmed (log) |
| 8 | `DIAGNOSIS diagnosis=INLINE_RESULT_ENTITY_MISSING`; **no** `INLINE_SEND_STARTED` | confirmed (log) |

Facts 5–8 are only observable because `9633c0b` corrected checkpoint 2 to read
`results[0].result.send_message` (the wrapper itself has no `send_message`) and
`b463552` made the bot's own submission observable at all. **No send and no
exact-message read-back has ever been reached** — nothing here claims either is
broken.

---

## 2. The path, traced in the current source (pinned layer 173)

```
emoji_premium_inline (action)              backend/bot/handlers/emoji.py
  → _premium_inline_action                   sends the Saved Messages selection
                                             message, arms reply mode
  → _premium_inline_reply_handler            resolves the EXACT selection
                                             message, then
  → send_premium_emoji_via_inline          backend/services/premium_emoji_inline_service.py
      Saved-Messages-only gate → document facts (alt/free/text_color) →
      payload → build_inline_payload / validate_inline_payload / inline_query_for
  → inline_engine.query_results            backend/helper/inline_engine.py
      self_client.inline_query(helper_username, "premium_emoji_send:<doc>:<glyph>", entity=chat_id)
        → messages.getInlineBotResultsRequest      (Telegram asks the bot)
  ── helper side ─────────────────────────────────────────────────────────────
  → _inline_router                         backend/helper/inline_engine.py
      is_owner gate → "<key>:<extra>" → get_inline_builder(key)
  → _premium_inline_builder                backend/bot/handlers/emoji.py
      → build_inline_result              InputBotInlineMessageText(
                                             message = "Premium emoji: <alt>",
                                             entities = [MessageEntityCustomEmoji(15, 2, doc)])
                                         InputBotInlineResult(id="0", type="article", send_message=…)
  → _sanitize_results (reply_markup rows only) → event.answer(built)
      → messages.setInlineBotResultsRequest{query_id, results, cache_time=0, …}
  ── back on the self side ───────────────────────────────────────────────────
  → _inspect_inline_result (checkpoint 2)   reads results[0].result.send_message
  → _send_result (checkpoint 3, NEVER REACHED) → messages.sendInlineBotResult
  → _fetch_stored_message + _read_back (checkpoint 4, NEVER REACHED)
```

Registration is the production one (`emoji.register` binds
`premium_inline_service.INLINE_QUERY_KEY` through
`inline_engine.register_inline_builder`; `backend/runtime/supervisor.py` wires
`inline_engine.register_inline_handler` onto the helper bot). No second client,
loop, listener or executor exists anywhere in this path.

---

## 3. What was verified this session, and how

### 3.1 The TL schema allows entities in **both** directions — `CONFIRMED`

Read from the installed pinned Telethon 1.34.0 (**layer 173**):

* `InputBotInlineMessageText` (`0x3dcd7a87`) — `message, no_webpage, invert_media, entities, reply_markup`.
* `BotInlineMessageText` (`0x8c7f65e2`, the **stored/served** variant) — also carries
  `entities`; so an entity is representable on the way back and Telethon parses it.
* `InlineResult` (the `custom` wrapper) has **no** `send_message`; its `.message`
  property returns `self.result.send_message`, and `.result` holds the raw TL
  object — exactly what checkpoint 2 now reads.
* `MessageEntityCustomEmoji` (`0xc8cf05f8`) is present in the layer on both sides.

### 3.2 The production answer path passes results through unmodified — `CONFIRMED`

`telethon/events/inlinequery.py::InlineQuery.Event.answer` builds
`functions.messages.SetInlineBotResultsRequest(query_id=…, results=results,
cache_time=…, gallery=…, next_offset=…, private=…, switch_pm=…)` from the passed
list (`_as_future` only resolves awaitables; a TL object is returned as-is).
The repository's `_RecordingInlineEvent` in `tests/test_premium_emoji_inline.py`
mirrors that construction field-for-field, so the wire-byte assertions describe
the **real** request.

### 3.3 The submitted answer carries the entity — `CONFIRMED` (wire bytes)

`test_the_answer_carries_the_entity_into_the_set_inline_bot_results_bytes`:
`BinaryReader(bytes(request)).tgread_object()` re-reads
`MessageEntityCustomEmoji(document_id=<real source id>, offset=15, length=2)`
inside `results[0].send_message.entities` of the real
`SetInlineBotResultsRequest`.

### 3.4 The **response** shape round-trips the entity — `CONFIRMED` (new this task)

`test_a_stored_result_parsed_from_response_bytes_keeps_the_entity` builds a real
`BotInlineResult` → `BotInlineMessageText` + `MessageEntityCustomEmoji`,
serializes it, **parses it back** through `BinaryReader(...).tgread_object()`, and
feeds the parsed object to `_inspect_inline_result`: `entity_present=True`,
`document_id`/`offset`/`length` intact, `document_id_match`/`span_match` true.
So a response that **did** carry the entity would be read correctly — the live
empty result cannot be a parsing artifact of the pinned client.

### 3.5 The sanitizer is harmless — `CONFIRMED`

`inline_engine._sanitize_results` rewrites only `reply_markup.rows`; it never
touches `entities` (`test_the_sanitizer_keeps_the_entity_and_only_normalizes_button_rows`).

### 3.6 The result cache is not a factor — `CONFIRMED` by source

`_inline_router` calls `event.answer(built)` with Telethon's default
`cache_time=0` (no server-side caching), and `private=False`. A cached answer
could therefore not be substituted for this attempt's answer — consistent with
the live trace showing the bot's own answer was recorded for this query.

### 3.7 The fail-closed gates are intact — `CONFIRMED`, unchanged

The pre-send inspection still refuses an entity-less stored payload
(`INLINE_RESULT_ENTITY_MISSING`) and nothing is sent; the destination gate is
still Saved-Messages-only; `_read_back` still requires the real entity **and**
Telegram's own `via_bot_id`; no Unicode fallback exists anywhere in the module.

### 3.8 The submitted payload is correlated with the stored result by **identity** — `FIXED`

The verdict "the entity was dropped by Telegram, not by this application" was
derived from `answer_ok` plus `custom_emoji_count >= 1` — a **count**, not the
entity's identity, even though `document_ids` was already recorded. A submission
that carried a *different* custom-emoji entity would have produced the same
claim. `_entity_missing_detail(evidence, expected_document_id=…)` now also
requires the recorded submission to contain **this emoji's** `document_id`;
otherwise the cause is reported as **unproven/uncorrelated**, and the
`INLINE_RESULT_INSPECTED` trace carries `answer_document_id_match=True|False`.
This is the task's "correlate the exact submitted payload with the actual result
returned by Telegram" requirement, implemented as a bounded predicate — no
behaviour of the send path changed.

---

## 4. Statements kept separate

| # | Statement | Status |
|---|---|---|
| 1 | Our payload object contains the entity | **CONFIRMED** (§3.1, §3.3) |
| 2 | The serialized `setInlineBotResults` request contains the entity | **CONFIRMED** (§3.3) |
| 3 | A returned payload that keeps the entity would be parsed and read correctly | **CONFIRMED** (§3.4) |
| 4 | The helper bot's answer was submitted without raising | **CONFIRMED** (§1.7) |
| 5 | Telegram **stored** the entity in the returned `BotInlineResult` | **REFUTED** — the live payload was inspected through the real shape and had none (§1.6) |
| 6 | The loss therefore happens inside Telegram's processing of the bot's inline answer | **CONFIRMED** by 1–5 (application code, serialization, parsing, sanitizing and caching are all excluded) |
| 7 | **Why** Telegram drops it (entitlement? another server rule?) | **UNRESOLVED — undocumented** (§6) |
| 8 | The self account's `sendInlineBotResult` or the exact-message read-back is broken | **NOT TESTED — never reached** |
| 9 | A different documented mechanism can deliver the same artifact | **Documented but NOT live-verified** (§7.3) |

---

## 5. Platform boundary — what Telegram documents, and what it does not

| Fact | Source | Status |
|---|---|---|
| "Note that when sending messages with attached custom emojis, the messageEntityCustomEmoji entity must wrap exactly one regular emoji (the one contained in documentAttributeCustomEmoji.alt) in the related text, otherwise the server will ignore it." | <https://core.telegram.org/api/custom-emoji> (read this session) | **CONFIRMED** — this is the documented server-side *ignore* rule, and the flow satisfies it by construction: the wrapped glyph **is** the document's `alt`, validated before submission (`test_the_registered_router_answers_the_query_with_the_real_entity`) |
| `documentAttributeCustomEmoji.free` — "whether the emoji can be used by non-premium users" | same page (read this session) | **CONFIRMED** — recorded per attempt as `eligibility.document.free` and shown in the panel |
| "All users – Premium or not – can see any animated emoji. Everyone can also use all custom emoji for free in their Saved Messages chat to try them out – or to add extra flair to notes and reminders." | <https://telegram.org/blog/custom-emoji> (read this session, verbatim) | **CONFIRMED** — the documented basis for the Saved-Messages-only destination gate |
| "Allowed bots to use custom emoji in messages directly sent by the bot to private, group and supergroup chats if the owner of the bot has a Telegram Premium subscription." (Bot API 9.4, February 9, 2026) | <https://core.telegram.org/bots/api-changelog> (read this session, verbatim) | **CONFIRMED** — and scoped to messages **directly sent by the bot**, i.e. **not** to an entity a bot supplies inside an inline result |
| Bot API `MessageEntity`: for `custom_emoji`, "only bots that purchased additional usernames on Fragment can use this entity" | <https://core.telegram.org/bots/api> (via research with URL) | **CONFIRMED** (documentation wording; the Bot API also states the 9.4 addition above) |
| Any rule about custom-emoji entities **inside inline results** (`inputBotInlineMessageText.entities`), or a documented error for them | `inputBotInlineMessageText`, `BotInlineMessage`, `messages.setInlineBotResults` error list, inline-mode docs, Bot API changelog | **NOT DOCUMENTED** — the schemas carry `entities` with no custom-emoji note and the method's error list has no entity/custom-emoji error |
| Which server-side rule silently drops this bot's inline-supplied entity, and whether a Fragment-purchased username or a Premium owner would change it | — | **UNRESOLVED / undocumented** |

**What the evidence supports:** the entity leaves this application intact in a
well-formed answer, and Telegram's stored answer does not contain it. **What the
evidence does not support:** any statement about *which* server rule produces
that, or any claim that a specific purchase or flag fixes it.

---

## 6. The documented routes and their status

Three distinguishable routes can put a genuine `MessageEntityCustomEmoji` into a
Telegram message from this project (the module docstring now lists all three —
previously it asserted there were exactly two, which the research below
disproved):

1. **The helper bot sends it itself** (`messages.sendMessage` as the bot,
   `backend/telegram_api/bridge.py`) — tested by the closed POC; the entity did
   not render. Covered by the documented bot-side entitlement rules; the repo's
   §34-D decision is "no alt-text fallback, no Fragment purchase" (ROADMAP §28:
   Premium/Fragment purchases are explicitly **not a goal**).
2. **The self account sends a bot-supplied inline result** (`messages.sendInlineBotResult`) —
   this module. Blocked as observed: Telegram's returned payload carries no entity,
   so the fail-closed gate refuses to send.
3. **The owner's own account authors the entity itself**
   (`messages.sendMessage` from the self client with
   `formatting_entities=[MessageEntityCustomEmoji(...)]`) — **no bot is involved,
   so no bot entitlement is checked**, and Telegram documents the non-Premium
   allowance for exactly this case in the self chat ("Everyone can also use all
   custom emoji for free in their Saved Messages chat"). **NOT implemented and
   NOT live-verified anywhere in this repository** — every existing sender in the
   codebase is the helper bot (`bridge.py`, `premium_emoji_probe_service.py`, the
   inline path). It is recorded here as a documented alternative requiring an
   owner decision and a live test, never as a confirmed fix.

Route 3 is the only one whose eligibility does not depend on a bot account. It
is **not** implemented in this task: no application-side delivery defect is
evidenced, the task's invariants pin the inline path's `via_bot_id` verification,
and adding a second delivery mechanism without live evidence would be a
workaround rather than a fix.

---

## 7. Falsifiers — what the next live run proves, either way

The next run of `Menu → Emoji → ✨ Send Premium Emoji → reply with a genuine
Premium emoji` now records the submission **and its correlation**, so it settles
the remaining question:

| Live trace | Meaning |
|---|---|
| `INLINE_RESULT_INSPECTED entity_present=False … answer_recorded=True answer_ok=True answer_custom_emoji_count=1 answer_document_id_match=True` then `INLINE_RESULT_ENTITY_MISSING` | **This record is confirmed on the live path**: the bot submitted *this* emoji's entity and Telegram dropped it from the stored answer. The inline-result construction is then blocked by Telegram's undocumented handling, not by this repository. |
| `answer_document_id_match=False` | the recorded submission was **not correlated** with this emoji (another entity, or a racing query) — the cause stays unproven and is never attributed to Telegram. |
| `answer_recorded=True answer_ok=False` (or `answer_custom_emoji_count=0`) | an **application-side** outcome the previous evidence could not see — the answer never carried the entity; diagnose from the recorded reason. |
| `answer_recorded=False` | the answer arrived outside this process's view (e.g. a cached answer) — the cause stays unproven and must not be attributed to Telegram. |
| `entity_present=True` then `INLINE_SEND_STARTED` | the stored payload kept the entity — checkpoint 4 (`sendInlineBotResult` + exact-message read-back + `via_bot_id`) becomes reachable for the first time. |

Alongside it, the panel already records the two entitlement facts that would
explain a server-side drop — `Owner Premium: … · emoji free: …` — so the next run
also discriminates "the bot is not entitled" from "the sending account or this
emoji is restricted".

---

## 8. Remaining uncertainty (stated, not hidden)

1. **Which server-side rule drops the entity** — undocumented for an
   inline-supplied entity; the observed drop is silent (text kept, no error).
2. **Whether the owner's Premium status or the emoji's `free` flag participates**
   — recorded per attempt, not yet correlated with a live drop.
3. **`messages.sendInlineBotResult` and the exact-message read-back** — never
   reached, therefore unproven live.
4. **Rendering** — even a retained entity is only `…_RENDER_UNVERIFIED`; display
   is the viewing client's decision.
5. **Route 3 (self-authored entity to Saved Messages)** — documented, never
   exercised in this repository, requires a live session and an owner decision.

---

## 9. Scope honoured

* Only the relevant implementation points were inspected (the premium builder,
  the router, the sanitizer, checkpoint 2/3/4, and the pinned Telethon's
  `answer` / wrapper / schema shapes).
* **One production fix**, inside the boundary under study and fail-closed: the
  attribution correlation (§3.8).
* No new Telegram client, listener, update loop, scheduler, executor or
  alternate send mechanism; `inline_engine.query_results` / `click_result` keep
  their contracts.
* No fabricated entity, no Unicode fallback, no bypass of the pre-send check, no
  weakened or deleted test, no Supabase/schema/SQL change, no
  `DATABASE_ARCHITECTURE.md` / `ROADMAP.md` / `tests/test_stage13.py` change.
