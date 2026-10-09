# Investigation — Genuine Premium Custom Emoji through a "via @bot" sending path

Canonical record of the latest completed investigation. **This file replaces the
previous `INVESTIGATION.md` in its entirety**; earlier revisions (the
Telegram message-provenance investigation at `66b9c7c`, and the Premium Custom
Emoji POC investigation/closure record ending at `3728db9`) are preserved in git
history and are referenced here only through their commit ids.

**Investigation only.** No production code, tests, configuration, dependencies,
SQL, migrations or Supabase objects were created or modified, no existing task
or subsystem was touched, and **no live Telegram request and no live Supabase
request were made**. No experiment was executed; §9 designs one.

| Item | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Branch | `main` |
| Revision inspected | `origin/main` = `3728db9` (docs: close the premium custom-emoji POC with the traced root cause) |
| Local checkout note | local `main` = `db02e13` (one pre-existing unpushed commit, merge-base with `origin/main` is `66b9c7c`); the POC artifacts described below live on `origin/main` |
| Question | Can a **non-Premium** Telegram user account send a **genuine** `MessageEntityCustomEmoji` through the existing intermediary/helper bot using a real Telegram **"via @bot"** sending path? |
| Verdict | **Technically plausible but unproven.** The attribution mechanism is documented and already implemented in this repository; the custom-emoji entity through that path is schema-supported but undocumented and never tested. |
| Previously tested path | direct helper-bot send (`messages.sendMessage` as the bot) — live-tested, entity dropped server-side |
| Changes made | this file only |

---

## 1. Question and scope

The intended experience:

1. a user **without** Telegram Premium sends a genuine Premium Custom Emoji into
   their Saved Messages;
2. the Self Bot inspects that message and identifies the requested emoji from its
   `MessageEntityCustomEmoji` (document id + UTF-16 span);
3. the Self Bot delegates message construction/sending to the existing
   intermediary helper bot;
4. the resulting message shows a Telegram **"via @nitro_selfbot"** attribution;
5. the stored outgoing message carries a genuine `MessageEntityCustomEmoji`
   Telegram accepted and stored — never an ordinary Unicode emoji and never a
   visual approximation;
6. the user account must not need to purchase Telegram Premium.

This investigation establishes whether that is possible. It does **not**
implement it, and it does not reinterpret the earlier live failure as proof that
every via-bot mechanism fails: the previously tested helper-bot bridge and the
intended via-bot message-construction path are **not** the same mechanism (§5).

Five statements are kept distinct throughout, because they are not equivalent:

| # | Statement | What it proves |
|---|---|---|
| 1 | the outgoing request contained `MessageEntityCustomEmoji` | our client built it |
| 2 | Telegram accepted the send | the message was stored |
| 3 | the **stored** message contains `MessageEntityCustomEmoji` | Telegram kept the entity |
| 4 | a Telegram client renders the message as a custom emoji | display only |
| 5 | a non-Premium account is allowed to send that emoji | product-level eligibility |

Only (3) is the decisive technical success condition; (5) is the additional
product-level condition.

---

## 2. Verified current implementation and the previously tested path

### 2.1 The previous probe (direct helper-bot send) — what was actually tested

- UI: `Menu` → Emoji → `💬 Set Reaction Emoji`
  (`_PROBE_ACTION = "emoji_react_premium"`,
  `backend/bot/handlers/emoji.py`, the POC section). The action sends a
  selection prompt to **Saved Messages with the self client**, arms the existing
  pending-input listener (`helper/inline_sender.register_input_listener`), and
  accepts only a reply to that exact selection message id.
- Inspection: `backend/services/premium_emoji_probe_service.py::
  inspect_message` → `_scan_custom_emoji` — **entity only**; a real
  `MessageEntityCustomEmoji` with a usable `document_id` is required, the span
  text comes from the entity's own UTF-16 span, and a plain Unicode reply is
  rejected with `SOURCE_ENTITY_MISSING`.
- Payload: `build_proof_payload` = `PROOF_PREFIX` ("Selected reaction emoji: ",
  25 UTF-16 units) + glyph, one entity `{type, offset: 25, length, document_id}`.
- Delivery: `deliver_proof` resolves the helper bot's own peer with the owner and
  calls `backend/telegram_api/bridge.py::send_reconstructed`, i.e.
  **`bot.send_message(peer, text, formatting_entities=[...])` — a direct
  `messages.sendMessage` whose sender is the helper bot account** and whose
  destination is the bot's private chat with the owner.
- Read-back: `bot.get_messages(peer, ids=<sent id>)` through the **bot's own**
  session, then `classify_diagnosis`.
- Live result recorded by the previous investigation
  (`3728db9`, `INVESTIGATION.md` §3.1 as it then stood): source entity found,
  `document_id=5352934405201494110`, source span `😵` at offset 0/length 2;
  outbound entity built at offset 25/length 2 with `used_placeholder=False`;
  `BRIDGE_ENTITY_CONVERTED … spans=25:2:5352934405201494110`; send accepted
  (`message_id=1618`); exact read-back showed **no** custom-emoji entity while
  the stored text and span matched; `DOCUMENT_ALT … alt='😵' match=True`;
  diagnosis `ENTITY_STRIPPED_OR_MISSING`.
- The outbound `messages.SendMessageRequest` bytes were also captured offline by
  that investigation and contain ctor `0xc8cf05f8`, offset 25, length 2 and the
  live document id — so the entity left our client intact.

**What that run established:** for *that* bot-sent message, the entity did not
survive on Telegram's side, and the documented MTProto ignore rule was satisfied
(§4.1), so the ignore rule and our own code are excluded as causes. **What it did
not establish:** the restriction responsible, and nothing about any other
mechanism, sender, or destination.

### 2.2 The via-bot machinery already exists in this repository

- `backend/helper/inline_engine.py::trigger()` — the Self Bot performs
  `self_client.inline_query(helper_username, query, entity=chat_id)` and then
  `results[0].click(chat_id)`. In installed Telethon 1.34.0
  (`backend/requirements.txt` pins `telethon==1.34.0`) these are
  `messages.GetInlineBotResultsRequest` (`telethon/client/bots.py`) and
  `messages.SendInlineBotResultRequest` (`telethon/tl/custom/inlineresult.py`).
- The helper answers `events.InlineQuery` with
  `types.InputBotInlineResult(id="0", type="article",
  send_message=types.InputBotInlineMessageText(message=..., reply_markup=...))`
  built by `inline_engine.make_result` and `helper/panel_render.py`.
- `helper/lifecycle.create_panel` and `helper/inline_sender.send_inline_panel`
  drive it; the `Menu` handler passes `event.chat_id`
  (`backend/bot/handlers/misc.py`), so panels normally land in the chat where the
  command was typed (Saved Messages in normal usage).
- **Every Glass-UI panel message is therefore already a message sent by the
  non-Premium self account with a bot attribution.** The repository reads that
  attribution explicitly: `backend/services/emoji_replacement_service.py::
  process_outgoing_message` skips any message with a valid `via_bot_id`
  (`STATUS_INLINE_ORIGIN`, "message was sent through an inline bot").
- Gap found: **both** `InputBotInlineMessageText` constructions in the repository
  pass only `message` and `reply_markup` — **no `entities=` is ever set**, so no
  custom-emoji entity has ever travelled the via-bot path.

Two observability gaps in the repository matter for the eligibility question:

| Gap | Location | Why it matters |
|---|---|---|
| `documentAttributeCustomEmoji.free` (and `text_color`) are discarded | `backend/telegram_api/custom_emoji.py::_serialize_document` returns only `{document_id, alt, set}` | `free` is the documented *"Whether this custom emoji can be sent by non-Premium users"* predicate (§4.2) — the single most relevant fact for the non-Premium goal, and it is never surfaced |
| `User.premium` is discarded | `backend/telegram_api/_helpers.py::serialize_user` | the sender's Premium status cannot currently be recorded by any code path |

Telethon 1.34.0 exposes both fields (`DocumentAttributeCustomEmoji.free`,
`User.premium`), so no dependency change is needed to read them.

---

## 3. What actually produces the "via @bot" attribution (question A)

**Conclusion: Telegram's inline-bot mechanism, executed by the *user's*
account. It is not a settable field, and a plain message send cannot request it.**

| Evidence | Source | Status |
|---|---|---|
| `messages.sendInlineBotResult` — "Send a result obtained using messages.getInlineBotResults", parameters `peer, query_id, id, …`, and **"Only users can use this method"**; its `hide_via` flag is documented as *"Whether to hide the via @botname in the resulting message"* | <https://core.telegram.org/method/messages.sendInlineBotResult> (Layer 225) | confirmed |
| The `Updates` constructors returned by that method carry `via_bot_id:flags.11?long` (`updateShortMessage`, `updateShortChatMessage`) | same method page (schema block) | confirmed |
| `message.via_bot_id` — *"ID of the inline bot that generated the message"*; it is a field **on the message**, not on `messages.sendMessage` | <https://core.telegram.org/constructor/message>, <https://core.telegram.org/method/messages.sendMessage> | confirmed |
| "As soon as the user taps on an item, it's immediately sent to the recipient"; "Messages sent with the help of your bot will show its username next to the sender's name" | <https://core.telegram.org/bots/inline> | confirmed |
| The repository already performs exactly this flow and then reads `via_bot_id` | `helper/inline_engine.py::trigger`, `services/emoji_replacement_service.py` | confirmed (code) |

Therefore: the bot supplies a result (`messages.setInlineBotResults`, Bot API
`answerInlineQuery`), the **user** selects and sends it
(`messages.sendInlineBotResult`), and Telegram authors the message as that user
and stamps `via_bot_id`. There is no path by which a user account can ask for
the attribution on an ordinary `messages.sendMessage` — no `via_bot_id` parameter
exists — and no path by which the bot itself becomes the sender of a
user-attributed message. **Adding `via_bot_id` manually is neither possible from
our side nor legitimate.**

---

## 4. Verified official Telegram / MTProto constraints

### 4.1 Custom-emoji entities

| Fact | Source | Confidence |
|---|---|---|
| `messageEntityCustomEmoji#c8cf05f8 offset:int length:int document_id:long = MessageEntity` — offset/length in UTF-16 code units | <https://core.telegram.org/api/custom-emoji>, <https://core.telegram.org/constructor/messageEntityCustomEmoji> | confirmed |
| **Silent-ignore rule:** *"when sending messages with attached custom emojis, the messageEntityCustomEmoji entity must wrap exactly one regular emoji (the one contained in documentAttributeCustomEmoji.alt) in the related text, otherwise the server will ignore it"* | <https://core.telegram.org/api/custom-emoji> | confirmed |
| *"To send a message with one or more custom emojis, create and attach messageEntityCustomEmoji entities to a message"*; the per-message cap is `appConfig.message_animated_emoji_max` | same page | confirmed |
| `messages.sendMessage` — *"Both users and bots can use this method"*; it carries `entities:flags.3?Vector<MessageEntity>` | <https://core.telegram.org/method/messages.sendMessage> | confirmed (schema, not permission) |

### 4.2 Whose entitlement matters (question C)

| Fact | Source | Confidence |
|---|---|---|
| `documentAttributeCustomEmoji.free` — *"Whether this custom emoji can be sent by non-Premium users"* | <https://core.telegram.org/constructor/documentAttributeCustomEmoji> (Layer 225) | **confirmed** — the documented, **per-emoji** eligibility predicate for a non-Premium sender |
| "Premium-only custom emojis (i.e. those where the `documentAttributeCustomEmoji.free` flag is not set)" | <https://core.telegram.org/constructor/emojiGroupPremium> | confirmed |
| Custom Emoji is a listed Telegram Premium feature | <https://telegram.org/faq_premium> ("… all Premium users receive: … Custom Emoji") | confirmed as a feature listing; **no blanket sender-Premium rule is stated for MTProto** |
| **Destination-scoped allowance:** *"All users – Premium or not – can see any animated emoji. Everyone can also use all custom emoji for free in their Saved Messages chat to try them out – or to add extra flair to notes and reminders."* | <https://telegram.org/blog/custom-emoji> (Telegram Team, Aug 12 2022) | confirmed, official |
| Bot-side rule: *"Custom emoji entities can only be used by bots that purchased additional usernames on Fragment **or** in the messages directly sent by the bot to private, group and supergroup chats if the owner of the bot has a Telegram Premium subscription. Not supported for messages sent in channel direct messages chats and on behalf of a business account."* | Bot API, Formatting options / `MessageEntityCustomEmoji` (<https://core.telegram.org/bots/api>) | confirmed **as Bot API documentation** |
| The bot-owner-Premium alternative was added in **Bot API 9.4 (February 9, 2026)**: *"Allowed bots to use custom emoji in messages directly sent by the bot to private, group and supergroup chats if the owner of the bot has a Telegram Premium subscription."* | <https://core.telegram.org/bots/api-changelog> | confirmed (read directly) |
| Whether that Bot API entitlement is enforced for a **bot's MTProto send**, and whether enforcement is a silent drop | — | **unknown / undocumented** (hypothesis H1 of the previous investigation, still unproven) |
| Whose entitlement is checked for a custom-emoji entity **supplied by a bot inside an inline result** (the bot's, the sending user's, or none) | — | **unknown / undocumented** |
| Any documented statement that a non-Premium user may or may not send a received custom emoji elsewhere (forward/copy) | — | **unknown / undocumented** |

**Inference, labelled as such:** the Bot API is a server-side wrapper over the
same MTProto send, so the bot-side eligibility plausibly is an *account* property
enforced regardless of which interface drives the send. That is the best available
explanation of the earlier live drop; it is **likely, not confirmed**, and no
official page documents silent dropping for an ineligible sender.

### 4.3 Inline results

| Fact | Source | Confidence |
|---|---|---|
| `inputBotInlineMessageText#3dcd7a87 flags:# … message:string entities:flags.1?Vector<MessageEntity> reply_markup:flags.2?ReplyMarkup` — *"Message entities for styled text"* | <https://core.telegram.org/constructor/inputBotInlineMessageText> | confirmed **schema capability** — a bot may structurally put entities, including `messageEntityCustomEmoji`, into an inline result |
| `messages.setInlineBotResults(…, results:Vector<InputBotInlineResult>, …)` is how a bot answers an inline query; the user later sends one of those results | <https://core.telegram.org/method/messages.setInlineBotResults> | confirmed |
| The response to `messages.getInlineBotResults` returns the stored `BotInlineMessageText` (with `entities`), so a client can inspect what the server kept **before** sending | <https://core.telegram.org/method/messages.getInlineBotResults>; installed Telethon `InlineResult.message → result.send_message` | confirmed (schema + client) |
| Whether the server accepts, keeps, or strips a custom-emoji entity in an inline result, and whose entitlement it checks | — | **unknown / undocumented** |
| `messages.sendInlineBotResult`'s documented error list contains **no** custom-emoji-specific error and no `PREMIUM_ACCOUNT_REQUIRED`; the earlier run also produced no error at all | method page + the recorded live trace | confirmed (absence of error) |
| Inline sticker results exist (`InlineQueryResultCachedSticker`, `botInlineMediaResult`) — a sticker/media result is **not** a `MessageEntityCustomEmoji` and cannot satisfy the success condition | <https://core.telegram.org/bots/api#inlinequeryresultcachedsticker> | confirmed |

**Saved Messages (question D).** Saved Messages is the self-chat
(`inputPeerSelf`); the official statement quoted above scopes the non-Premium
allowance to *that chat*. Two consequences, kept separate:

- **As input:** it is the documented place where a non-Premium account may
  legitimately hold a genuine premium custom-emoji entity. The earlier live run
  already proves this empirically — the owner's own reply carried a real entity
  (`SOURCE_ENTITY_FOUND … document_id=5352934405201494110`). Storage/use in the
  self-chat is **not** permission to send the emoji anywhere else.
- **As destination:** it is the only destination for which an official
  non-Premium allowance is documented. A bot can neither read nor write the
  user's Saved Messages, so for that destination the **user account must be the
  sender** — which is exactly what the via-bot path does.
- No official statement was found that Saved Messages changes what a *bot* may
  supply, nor any custom-emoji rule specific to Saved Messages in the MTProto
  documentation.

**Screenshot (question E).** The screenshot shows a message rendering a custom
emoji with a "via @nitro_selfbot" attribution. What it **does** establish: some
account produced a bot-attributed message that a client rendered as a custom
emoji. What it does **not** establish: the sender's Premium status, the
destination chat, the emoji's `free` flag, whether a genuine entity is stored
(the image shows a render, not the entity), and how the message was constructed.
`@nitro_selfbot` produced no verifiable public documentation. The observation is
therefore compatible both with a Premium sender and with a working non-Premium
via-bot path — **insufficient evidence**, and it neither confirms nor refutes
either. The same reasoning is why the earlier failure does not close the via-bot
question (§5).

---

## 5. Previous helper-bot bridge vs the intended via-bot path

| Axis | Previously tested (live, failed) | Intended via-bot path |
|---|---|---|
| MTProto method | `messages.sendMessage` (Telethon `bot.send_message`) | `messages.sendInlineBotResult` (Telethon `InlineResult.click`) |
| Who answers the bot | n/a | bot answers `messages.setInlineBotResults` with `InputBotInlineMessageText.entities` |
| **Sender of the final message** | **helper bot account** | **self user account (non-Premium)** |
| Who supplies the entity | the bot, directly in its own send | the bot, inside the inline result payload |
| Attribution | none (plain bot message) | `via_bot_id` = helper bot → "via @bot" |
| Destination actually tested | the bot's private chat with the owner | Saved Messages (the documented-friendly destination) is untested |
| Entitlement documented for this exact path | Bot API bot rule only (Fragment **or** bot-owner Premium); MTProto unstated | **nothing documented** |
| Existing repository support | `bridge.send_reconstructed` (implemented) | inline machinery implemented, but **never used with `entities=`** |
| Live evidence | entity dropped server-side, no error | **none** |

**They are not equivalent.** The sender, the method, the entity's carrier and the
destination all differ, so the previous `ENTITY_STRIPPED_OR_MISSING` result does
**not** falsify the via-bot path, and it must not be read as proof that every
via-bot mechanism fails.

---

## 6. Feasibility verdict (question 5)

**Verdict: technically plausible but unproven.** Split into independent claims:

| # | Claim | Evidence | Verdict |
|---|---|---|---|
| 1 | The "via @bot" attribution mechanism is usable here | §3 + the repository already produces it for every Glass-UI panel | **feasible and documented** (confirmed) |
| 2 | A bot can hand the server a result carrying a custom-emoji entity | schema (`inputBotInlineMessageText.entities`) — no permission statement either way | **plausible, undocumented** |
| 3 | That entity survives the non-Premium user's `sendInlineBotResult` and is stored | no documentation, no test | **unproven — the decisive unknown** |
| 4 | The non-Premium product goal, with Saved Messages as destination | official allowance for the self-chat (§4.2) + claim 3 unknown | **plausible, unproven** |
| 5 | The non-Premium product goal, in any *other* chat, for a non-`free` emoji | documented per-emoji predicate (`free`) | **blocked by a documented restriction** unless the emoji is `free` |
| 6 | A **bot sending custom emoji directly** (the old probe path) | Bot API eligibility + the observed silent drop, `free` status unknown | **blocked-by-evidence for this bot/owner today** (eligibility not met; cause likely, not confirmed) |

Nothing in the evidence establishes that non-Premium use **is** achievable; the
closest thing to positive evidence is the documented Saved Messages allowance,
which applies to the *self-chat destination* and was never observed through an
inline result.

---

## 7. Why the earlier probe cannot settle this (evidence quality)

| Conclusion | Supporting source | How the source supports it | Confidence |
|---|---|---|---|
| The entity left our client intact on the direct-send path | `3728db9` §4 (captured `SendMessageRequest` bytes: ctor, offset 25, length 2, document id) | the serialized request the real Telethon builds contains the entity | confirmed |
| Telegram did not store it for a bot-authored send | `3728db9` §3.1 (exact-id read-back through the bot's session) | the stored message has no `MessageEntityCustomEmoji` while text/span match | confirmed |
| Our code has no defect on that path | `validate_proof_payload` + single send path + 62 focused tests (`tests/test_premium_emoji_probe.py`) | a construction defect would surface as `OUTBOUND_ENTITY_INVALID`, which never fired | confirmed |
| The documented ignore rule was not the cause | `DOCUMENT_ALT … match=True`, span = exactly one 2-unit emoji | the documented precondition was satisfied | confirmed |
| The cause was the bot's entitlement | Bot API rule (§4.2) | consistent with everything observed; **not documented for MTProto**, no error was returned | **likely, unproven** |
| The via-bot path behaves the same way | — | no evidence exists for that path in either direction | **unknown** |
| A non-Premium sender may use a `free` custom emoji | `documentAttributeCustomEmoji.free` description | explicit documentation of per-emoji eligibility | confirmed (as documentation) |

---

## 8. The smallest decisive experiment (question F) — designed, **not executed**

**Hypothesis H.** A custom-emoji entity supplied by the helper bot in an inline
result survives into the stored message when the **non-Premium self account**
sends that result to **its own Saved Messages**, producing
`via_bot_id = <helper bot>`.

**Reuse only existing architecture.** `register_inline_builder` +
`InputBotInlineMessageText` (helper side), `inline_engine.trigger` (self side),
and the existing probe's entity-only inspection and honest outcome
classification. One probe-only inline builder that sets
`entities=[MessageEntityCustomEmoji(offset, length, document_id)]`, plus one
probe action that runs `trigger(self_client, "me", "<probe_key>")` and reads the
sent message back by exact id with `self_client.get_messages("me", ids=msg_id)`.
No second client, loop, scheduler, executor, listener, database write, schema,
or dependency; **no change to the production reaction flow**; the existing POC is
not reopened.

**Fixed input:** document `5352934405201494110` (alt `😵`), span wrapping exactly
that `alt` (the §4.1 ignore rule), destination Saved Messages. Exactly **one**
live send for the primary run.

**Recorded at three checkpoints, plus entitlement facts:**

1. **Set** — did the helper's `setInlineBotResults` accept the result, or raise?
   (an error here alone proves a set-time restriction);
2. **Get** — in the `getInlineBotResults` response, does
   `InlineResult.message.entities` still contain the custom-emoji entity?
   (separates "stripped from the stored result" from "stripped at send time");
3. **Send** — read back the exact sent message by id and record `via_bot_id`,
   `entity_present`, `document_id`, `offset`, `length`, UTF-16 span text, stored
   text length, `document_id_match`, `span_match`.

Plus, without assuming any of them:

- **sender Premium status:** `self_client.get_me().premium` (Telethon
  `User.premium`; not surfaced by `serialize_user` today);
- **emoji eligibility:** the live document's `free` and `text_color` flags via
  `messages.getCustomEmojiDocuments` (discarded by `_serialize_document` today —
  the probe must read the attribute itself);
- **bot capability:** `bot.get_me()` → `usernames` list (the Bot API rule speaks
  of *additional* usernames purchased on Fragment) and whether inline mode is
  enabled — recorded as supporting evidence only, never as proof;
- the destination chat id and the exact stored text/geometry.

**Optional single-variable controls (only if the primary run is ambiguous):**
(a) the same run with a document whose `free` flag is `true` — isolates the
eligibility rule from the inline mechanism; (b) the same run to a non-self
destination — isolates the Saved Messages allowance; (c) a direct self
`messages.sendMessage` carrying the entity — mechanism control (the source step
already gives partial evidence for it).

**Explicitly forbidden in the experiment:** alt-text/Unicode fallbacks, a second
client/loop/executor, reaction-flow changes, entitlement probing beyond `get_me`
and the document attributes, database writes, and reporting a retained entity as
a rendered emoji.

---

## 9. Exact success and failure criteria

| Outcome | Evidence required | Meaning |
|---|---|---|
| **Success (technical)** | the stored message contains `MessageEntityCustomEmoji` with the **exact** document_id, offset and length; the entity type is a real custom-emoji entity (not a Unicode glyph); `via_bot_id == <helper bot id>`; `get_me().premium` is false | the via-bot entity path works for a non-Premium sender into Saved Messages |
| **Success (product)** | the technical success **plus** the owner's visual confirmation of the custom-emoji render | the intended experience is achievable |
| **Falsified — path blocked** | checkpoint 2 showed the entity present, the §4.1 alt rule satisfied, the send accepted, yet checkpoint 3 has **no** entity (`ENTITY_STRIPPED_OR_MISSING`) | Telegram strips it on this path too; record whether it correlates with `free=false` |
| **Falsified earlier — bot side** | checkpoint 1 rejects the result, or checkpoint 2 returns no entity | a bot may not supply custom-emoji entities in inline results at all |
| **Inconclusive** | read-back failure, expired query, or missing message id | nothing about entitlement; re-run once, never substitute a glyph |

The read-back alone remains `ENTITY_RETAINED_RENDER_UNVERIFIED` — retention is
not display, exactly as the existing probe already states.

---

## 10. Open questions and evidence gaps

1. **U1 — whose entitlement governs** a bot-supplied custom-emoji entity in an
   inline result (bot, sending user, or none): undocumented.
2. **U2 — whether the Bot API bot rule is enforced for a bot's MTProto send**,
   and whether enforcement is a silent drop: undocumented; consistent with the
   observed run but not proven.
3. **U3 — the `free` (and `text_color`) flag of document
   `5352934405201494110`:** never read; the repository discards it. Until this is
   known, no path can be declared feasible or blocked for the intended emoji.
4. **U4 — the helper bot's capability state** (Fragment-purchased additional
   usernames): unknown; observable only as supporting evidence.
5. **U5 — the screenshot's provenance** (sender Premium status, destination,
   `free` flag, stored entity): unrecoverable from an image; `@nitro_selfbot` has
   no verifiable public documentation.
6. **U6 — forward/copy semantics:** whether a non-Premium account can propagate
   a received non-`free` custom-emoji entity into another chat: undocumented;
   would independently test the `free` semantics.
7. **U7 — how to obtain a `free=true` emoji** in practice (group emoji packs are
   documented as usable "by all users and bots in the group"; the mapping to
   `free` documents is unverified), which controls whether control (a) of §8 can
   be run.
8. **U8 — documentation accuracy in this repository:** `ROADMAP.md` §17/§28 and
   the `backend/telegram_api/bridge.py` module docstring state only the
   *Fragment half* of the bot rule and omit the Bot API 9.4 bot-owner-Premium
   alternative for messages directly sent by the bot. Recorded here only; no
   file was changed.
9. **U9 — no error-path evidence exists:** the earlier run produced a delivered
   message with the entity removed and no error; nothing documents what an
   ineligible sender should expect (silent drop vs error), so a future probe
   must record both the exception and the stored result.

---

## 11. Recommendation for the next step

1. **Do not implement the feature now, and do not reopen the closed POC** — the
   POC tested a different mechanism (§5) and its closure still holds for that
   mechanism.
2. **Next step: one separately authorized, narrowly-scoped experiment task**
   implementing exactly §8 — a probe-only inline-result builder, one live send to
   Saved Messages, the three checkpoints, and the recording of `free`,
   `premium`, and `via_bot_id`. This task authorizes investigation and design
   only, so the send was **not** performed.
3. If the experiment **succeeds**, the non-Premium via-bot experience is
   feasible for that emoji and destination, and the feature can be designed on
   the existing inline machinery (the attribution half already works today).
4. If it **fails with the checkpoints intact**, the realistic remaining options
   are: `free`-only emoji, Saved-Messages-only destinations, or changing the
   bot/owner eligibility (Fragment username or Premium) — an owner/account
   decision, not a code change.
5. Regardless of outcome, surface `free` in
   `telegram_api/custom_emoji._serialize_document` and `premium` in
   `serialize_user` when (and only when) such a task is authorized — both are
   observation-only and require no dependency change.

---

## 12. Scope, files and validation

**Inspected (read-only):** `INVESTIGATION.md`, `IMPLEMENTATION_REPORT.md`,
`ROADMAP.md`, `backend/services/premium_emoji_probe_service.py`,
`backend/bot/handlers/emoji.py` (POC section), `backend/telegram_api/bridge.py`,
`backend/telegram_api/_helpers.py`, `backend/telegram_api/custom_emoji.py`,
`backend/telegram_api/entities.py`, `backend/helper/client.py`,
`backend/helper/inline_engine.py`, `backend/helper/inline_sender.py`,
`backend/helper/lifecycle.py`, `backend/helper/panel_render.py`,
`backend/helper/input_state.py`, `backend/services/emoji_replacement_service.py`,
`backend/bot/handlers/misc.py`, `tests/test_premium_emoji_probe.py` (62 tests),
`tests/test_bridge_delivery.py`, `backend/requirements.txt`, and the installed
Telethon 1.34.0 sources (`telethon/client/bots.py`,
`telethon/tl/custom/inlineresult.py`, `telethon/tl/types/__init__.py`).

**Official sources read directly this session:**
<https://core.telegram.org/api/custom-emoji>,
<https://core.telegram.org/constructor/documentAttributeCustomEmoji>,
<https://core.telegram.org/constructor/inputBotInlineMessageText>,
<https://core.telegram.org/method/messages.sendInlineBotResult>,
<https://core.telegram.org/bots/api-changelog> (Bot API 9.4),
<https://telegram.org/blog/custom-emoji>,
<https://telegram.org/faq_premium>,
plus <https://core.telegram.org/bots/api> (bot entitlement wording) and
<https://core.telegram.org/bots/inline> corroborated through search/agent
readings rather than a line-by-line parse of the rendered page.

**Files changed by this delivery:** `INVESTIGATION.md` only.

**Intentionally untouched:** all production code, all tests (including
`tests/test_stage13.py`), all configuration and dependencies, all SQL and
migrations, `DATABASE_ARCHITECTURE.md`, `IMPLEMENTATION_REPORT.md`,
`ROADMAP.md`, the emoji/reaction pipeline, the AI/runtime/helper layers.

**Validation:** `git diff --check` clean; the delivery diff contains only
`INVESTIGATION.md`; no test suite was run because no code changed.

**Not verified in this session:** no live Telegram or Supabase call; no live
experiment; the `free` and `premium` values of the accounts/emoji involved are
still unknown (U3, U5).
