# Investigation — Genuine Premium Custom Emoji through a "via @bot" sending path

Canonical record of this feature's investigation **and its implementation
state**. **This file replaces the previous `INVESTIGATION.md` in its entirety**;
earlier revisions (the Telegram message-provenance investigation at `66b9c7c`,
the Premium Custom Emoji POC investigation/closure record ending at `3728db9`,
and the via-bot feasibility investigation ending at `b04cb41`) are preserved in
git history and are referenced here only through their commit ids and their
still-relevant evidence.

| Item | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Branch | `main` |
| Revision this record was produced from | `b04cb41` (the parent of the implementation commit; the implementation itself is `feat(emoji): send the premium custom emoji through the inline bot`) |
| Question | Can a **non-Premium** Telegram account send a **genuine** `MessageEntityCustomEmoji` through the existing helper bot using Telegram's real inline-bot ("via @bot") sending path? |
| Verdict | **Implemented and automated-tested; NOT live-verified.** The path is reachable from the Glass UI, the entity survives our construction and the repository's own parsing, and the outcome is classified honestly. Only a real Telegram run can decide whether Telegram keeps the entity. |
| Live Telegram verification | **none in this session** — no client, no request, no message was sent or read |
| Visual (owner) verification | **not performed** — it is the owner's test, described in `IMPLEMENTATION_REPORT.md` §7 |

---

## 0. Status of every claim — implemented / tested / live-verified

The five statements are kept strictly apart, because they are not equivalent:

| # | Statement | Status |
|---|---|---|
| 1 | the outbound request contains a real `MessageEntityCustomEmoji` | **implemented + automated-tested** (`build_inline_result`, `validate_inline_payload`) |
| 2 | the helper bot's inline result is accepted by Telegram *with* the entity | **implemented + recorded, NOT proven** — checkpoint 2 is inspected at runtime (`inline_result`), no live value exists yet |
| 3 | the **stored** message contains the entity (the decisive technical condition) | **implemented + recorded, NOT proven** — checkpoint 3 is read back by exact id, no live run has happened |
| 4 | a Telegram client renders it as a custom emoji | **not verifiable by any code** — owner's visual check only |
| 5 | a non-Premium account is allowed to do this | **unproven** — the owner's `premium` flag is observed and reported per run; Saved Messages is the documented allowance and the only supported destination |

**Automated tests prove the implementation, never Telegram's verdict.** The
test suite uses fakes for every Telegram surface; a mocked success is not
evidence that a real send keeps the entity.

---

## 1. Question and scope

The intended experience:

1. a user **without** Telegram Premium places a genuine Premium Custom Emoji
   into their Saved Messages;
2. the Self Bot inspects that message and identifies the emoji from its
   `MessageEntityCustomEmoji` (document id + UTF-16 span), never from the
   visible glyph;
3. the Self Bot delegates result construction to the existing helper bot;
4. the **owner's own account** sends that result through Telegram's inline
   mechanism, producing a real "via @bot" attribution;
5. the stored message carries a genuine `MessageEntityCustomEmoji` Telegram
   accepted and stored — never an ordinary Unicode emoji and never a visual
   approximation;
6. the user account does not need Telegram Premium.

Two distinct Telegram mechanisms can carry a custom-emoji entity, and only the
second one is this feature (§5):

* the **helper bot sends the message itself** (`messages.sendMessage` as the
  bot) — live-tested by the closed POC and **entity dropped server-side**;
* the **user account sends a bot-supplied inline result**
  (`messages.sendInlineBotResult`) — the mechanism this repository already
  uses for every Glass UI panel, and the one implemented here.

The decisive success condition is (3) above — the *stored* message — plus the
product-level condition (5).

---

## 2. The previously tested path (direct helper-bot send) — preserved evidence

The closed POC (`backend/services/premium_emoji_probe_service.py`, commits
`aed86aa` … `3728db9`) is still in the repository as a **diagnostic** (its Glass
UI row is now labelled `🧪 POC · helper-bot sent emoji` and is no longer the
feature's entry point). Its recorded live run:

| Item | Recorded value |
|---|---|
| Source | owner's own reply in Saved Messages carrying a real entity: `document_id=5352934405201494110`, span `😵` at offset 0/length 2 |
| Outbound payload | `PROOF_PREFIX` (25 UTF-16 units) + glyph; entity offset 25, length 2, `used_placeholder=False` |
| Trace | `BRIDGE_ENTITY_CONVERTED … spans=25:2:5352934405201494110` |
| Send | accepted (`message_id=1618`) through `backend/telegram_api/bridge.py::send_reconstructed`, i.e. **`bot.send_message` = `messages.sendMessage` as the helper bot** |
| Read-back | the bot's own session fetched the exact id; the stored text and span matched, **no custom-emoji entity** |
| Document check | `DOCUMENT_ALT … alt='😵' match=True` — the documented "wrap exactly the alt emoji" precondition was satisfied |
| Diagnosis | `ENTITY_STRIPPED_OR_MISSING` |
| Offline capture | the serialized `SendMessageRequest` (ctor `0xc8cf05f8`, offset 25, length 2, the live document id) proves the entity left our client intact |
| Verdict for that mechanism | **blocked by evidence**: a bot-authored `messages.sendMessage` did not retain the entity for this bot/owner, with no error raised |

The documented MTProto ignore rule (the entity must wrap exactly one regular
emoji — the one in `documentAttributeCustomEmoji.alt`) was satisfied in that
run, so neither our construction nor that rule explains the drop. The Bot API
bot rule (Fragment-purchased additional usernames, or — since Bot API 9.4 —
bot-owner Premium for messages the bot sends directly) explains it plausibly,
**not provably** for MTProto.

**What that run does NOT establish:** anything about the inline-user path —
different sender, different method, different carrier of the entity, different
destination (§5).

---

## 3. The implemented path (inline-user send) — what exists now

| Component | Role |
|---|---|
| `backend/services/premium_emoji_inline_service.py` (new) | the whole production pipeline: source inspection, payload construction + validation, inline-result construction, the query/send/read-back drive, evidence recording, diagnosis |
| `backend/bot/handlers/emoji.py` (extended) | the ONE user-facing action `emoji_premium_inline` (`✨ Send Premium Emoji`), the deterministic Saved Messages selection message, the reply-mode handler, the registered inline builder, the honest report rendering. The old POC row remains, clearly labelled as a diagnostic, below the production entry |
| `backend/helper/inline_engine.py` (extended) | `query_results` (the `getInlineBotResults` half of `trigger`), `click_result` (the `sendInlineBotResult` half) and `inline_unavailable_reason` — the two halves exposed so the stored inline result can be inspected **between** them. `trigger` keeps its exact public contract and is now implemented on top of them |
| `backend/telegram_api/custom_emoji.py` (extended) | `_serialize_document` now also reports `free` (`documentAttributeCustomEmoji.free`, the documented per-emoji non-Premium predicate) and `text_color` — additive keys |
| `backend/telegram_api/_helpers.py` (extended) | `serialize_user` now also reports `premium` (`User.premium`) — additive key |
| `tests/test_premium_emoji_inline.py` (new) | 50 offline tests pinning the pipeline, its failure modes and the entry point |

### 3.1 Data flow, exactly as implemented

```
owner's reply to the exact selection message (Saved Messages)
  inspect_source_message()        → real MessageEntityCustomEmoji only;
                                    document_id + UTF-16 offset/length + span
  get_custom_emoji_documents()    → alt, free, text_color (Telegram's own)
  build_inline_payload()          → text = PREFIX + glyph,
                                    entity offset = utf16(PREFIX),
                                    length = utf16(glyph), document_id
  validate_inline_payload()       → fails closed; the payload is checked
                                    against itself before submission
  helper: _premium_inline_builder → InputBotInlineMessageText(message, entities)
                                    = the REAL entity, no reply_markup
  inline_engine.query_results()   → getInlineBotResults (checkpoint 2:
                                    entity_present / id / offset / length)
  inline_engine.click_result()    → sendInlineBotResult AS THE OWNER
                                    (via_bot_id is Telegram's own; never set)
  self_client.get_messages(id)    → checkpoint 3: entity_present,
                                    document_id/offset/length match,
                                    span match, via_bot_id match
  classify_diagnosis()            → ONE of the honest outcomes below
```

### 3.2 The outcomes the code distinguishes

| Diagnosis | Evidence it states |
|---|---|
| `SOURCE_ENTITY_MISSING` | the source message carries no usable custom-emoji entity (or its glyph cannot be resolved) — nothing was sent |
| `UNSUPPORTED_DESTINATION` | the destination is not the owner's Saved Messages — the documented non-Premium allowance is scoped to the self chat |
| `OUTBOUND_PAYLOAD_INVALID` | the payload failed its own pre-submission validation |
| `INLINE_UNAVAILABLE` | the helper bot / its inline username is not available |
| `INLINE_RESULT_REJECTED` | the inline query failed or returned nothing (checkpoint 1) |
| `INLINE_RESULT_ENTITY_MISSING` | Telegram stored the bot's result **without** the entity (checkpoint 2) |
| `INLINE_SEND_FAILED` | `sendInlineBotResult` raised or returned no message id |
| `READBACK_FAILED` | the exact message could not be read back (reported separately from the send) |
| `STORED_ENTITY_STRIPPED` | the result had the entity, the send was accepted, the **stored** message has none (checkpoint 3) |
| `STORED_ENTITY_MISMATCH` | the stored entity differs in document id or UTF-16 span |
| `STORED_ATTRIBUTION_MISSING` | the entity is stored, but `via_bot_id` is absent or is not the helper bot |
| `STORED_ENTITY_VERIFIED_RENDER_UNVERIFIED` | the decisive technical condition holds — **retention is not display** |

A retained entity is never reported as a rendered emoji, and no failure path
builds or sends an ordinary Unicode emoji.

---

## 4. Verified official Telegram / MTProto constraints (unchanged evidence)

### 4.1 Custom-emoji entities

| Fact | Source | Confidence |
|---|---|---|
| `messageEntityCustomEmoji#c8cf05f8 offset:int length:int document_id:long` — offset/length in UTF-16 code units | <https://core.telegram.org/api/custom-emoji> | confirmed |
| **Silent-ignore rule:** the entity must wrap exactly one regular emoji (the one in `documentAttributeCustomEmoji.alt`) in the related text, otherwise the server ignores it | <https://core.telegram.org/api/custom-emoji> | confirmed |
| `messages.sendMessage` carries `entities:flags.3?Vector<MessageEntity>`; both users and bots may call it | <https://core.telegram.org/method/messages.sendMessage> | confirmed (schema) |

### 4.2 Whose entitlement matters

| Fact | Source | Confidence |
|---|---|---|
| `documentAttributeCustomEmoji.free` — "whether this custom emoji can be sent by non-Premium users" | <https://core.telegram.org/constructor/documentAttributeCustomEmoji> | confirmed |
| **Destination-scoped allowance:** "Everyone can also use all custom emoji for free in their Saved Messages chat" | <https://telegram.org/blog/custom-emoji> | confirmed, official |
| Bot-side rule: custom-emoji entities in messages **sent directly by the bot** require Fragment-purchased additional usernames, or (Bot API 9.4, Feb 2026) a bot owner with Premium | <https://core.telegram.org/bots/api>, <https://core.telegram.org/bots/api-changelog> | confirmed as Bot API documentation |
| Whether that bot rule is enforced for a bot's **MTProto** send, and whether enforcement is a silent drop | — | **unknown / undocumented** |
| Whose entitlement is checked for a custom-emoji entity **supplied by a bot inside an inline result** (the bot's, the sending user's, or none) | — | **unknown / undocumented — the decisive gap** |

### 4.3 Inline results

| Fact | Source | Confidence |
|---|---|---|
| `inputBotInlineMessageText#3dcd7a87 flags:# … message:string entities:flags.1?Vector<MessageEntity> reply_markup:flags.2?ReplyMarkup` | <https://core.telegram.org/constructor/inputBotInlineMessageText> | confirmed — a bot may structurally place entities, including a custom-emoji entity, in a result |
| `messages.sendInlineBotResult` — "Send a result obtained using messages.getInlineBotResults"; **"Only users can use this method"**; `hide_via` hides "via @botname" | <https://core.telegram.org/method/messages.sendInlineBotResult> | confirmed |
| `message.via_bot_id` is a field **on the message** ("ID of the inline bot that generated the message"); it is not a `sendMessage` parameter | <https://core.telegram.org/constructor/message> | confirmed |
| `getInlineBotResults` returns the stored `BotInlineMessageText` (with `entities`) — a client can inspect what the server kept **before** sending | <https://core.telegram.org/method/messages.getInlineBotResults> | confirmed |
| Whether the server accepts/keeps a custom-emoji entity in an inline result, and whose entitlement it checks | — | **unknown / undocumented** |
| The installed client's actual surface (Telethon 1.34.0, pinned in `backend/requirements.txt`): `InputBotInlineMessageText(message, no_webpage, invert_media, entities=None, reply_markup=None)`; `InlineResult.click()` → `SendInlineBotResultRequest(peer, query_id, id, …)` → `_get_response_message` | installed `telethon` source, inspected this session | confirmed (code) |

---

## 5. The two mechanisms, side by side (updated)

| Axis | Direct bot send (POC, tested, failed) | Inline-user send (**implemented here**) |
|---|---|---|
| MTProto method | `messages.sendMessage` (Telethon `bot.send_message`) | `messages.sendInlineBotResult` (Telethon `InlineResult.click`) |
| Who supplies the entity | the bot, in its own send | the bot, inside the inline result payload |
| **Sender of the final message** | helper bot account | **the owner's own (non-Premium) account** |
| Attribution | none | Telegram's own `via_bot_id` → "via @bot" |
| Destination tested | the bot's private chat with the owner | Saved Messages (the documented allowance) |
| Existing repository support | `bridge.send_reconstructed` | `inline_engine.query_results` / `click_result`, `premium_emoji_inline_service`, the registered inline builder |
| Live evidence | entity dropped server-side, no error | **none** — checkpoint 2/3 recording is implemented for the owner's run |
| Verdict | blocked by evidence for that mechanism | **implemented; Telegram's verdict still unknown** |

They are **not equivalent**: sender, method, carrier and destination all
differ, so the earlier `ENTITY_STRIPPED_OR_MISSING` result neither falsifies
nor supports the inline path.

---

## 6. Feasibility verdict, restated after implementation

| # | Claim | Status |
|---|---|---|
| 1 | The "via @bot" attribution mechanism is usable here | **feasible and documented** — the repository already produces it for every panel, and `via_bot_id` is read back on our message |
| 2 | A bot can hand the server a result carrying a custom-emoji entity | **implemented; acceptance unproven** — checkpoint 2 records the truth |
| 3 | That entity survives the non-Premium user's `sendInlineBotResult` and is stored | **implemented; unproven — the decisive unknown** |
| 4 | The non-Premium product goal with Saved Messages as the destination | **implemented; unproven** (1 + 2 + 3 + the owner's `premium=false`) |
| 5 | The same goal in any other chat, for a non-`free` emoji | **refused by design** (`UNSUPPORTED_DESTINATION`) unless Telegram's own behavior establishes otherwise |
| 6 | A bot sending custom emoji directly (the old POC path) | **blocked by evidence** for this bot/owner today |

---

## 7. Success and failure criteria — and where each is recorded

| Outcome | Evidence required | Recorded as |
|---|---|---|
| **Success (technical)** | the stored message contains `MessageEntityCustomEmoji` with the exact document id, offset and length; `via_bot_id == helper bot id`; `get_me().premium` observed false | `diagnosis=STORED_ENTITY_VERIFIED_RENDER_UNVERIFIED`, `verified=True`, plus `readback` and `eligibility` |
| **Success (product)** | technical success **plus** the owner's visual confirmation | the owner's own report — never claimed by code |
| **Telegram-side restriction (result)** | checkpoint 2 has no entity | `INLINE_RESULT_ENTITY_MISSING` — nothing is sent |
| **Telegram-side restriction (send)** | checkpoint 2 has the entity, the send is accepted, checkpoint 3 has none | `STORED_ENTITY_STRIPPED` (with `readback.span_match` and the document's `alt` for diagnosis) |
| **Implementation/attribution defect** | stored entity with a different id/span; or `via_bot_id` missing/mismatched | `STORED_ENTITY_MISMATCH` / `STORED_ATTRIBUTION_MISSING` |
| **Inconclusive** | read-back failure, exception, no message id | `READBACK_FAILED` / `INLINE_SEND_FAILED` / `INLINE_RESULT_REJECTED` |

---

## 8. Open questions and evidence gaps (status after this task)

| # | Gap | Status |
|---|---|---|
| U1 | Whose entitlement governs a bot-supplied custom-emoji entity in an inline result | **still undocumented** — the owner's run records the facts needed to interpret a failure |
| U2 | Whether the Bot API bot rule is enforced for MTProto bot sends, and whether enforcement is silent | **still undocumented** (unchanged; only the direct-send mechanism is implicated) |
| U3 | The `free` flag of the emoji involved | **now surfaced** — `_serialize_document` reports `free`/`text_color` and the report shows them per run |
| U4 | The helper bot's Fragment capability state | **partially observable** — the bot's id/username are recorded; Fragment purchases are not readable through this API |
| U5 | The screenshot's provenance from the earlier investigation | **unrecoverable** — unchanged |
| U6 | Forward/copy semantics for a non-Premium account | **untouched** — out of scope |
| U7 | How to obtain a `free=true` emoji in practice | **unchanged**; `free` is now visible, so the owner's first run will answer it for the emoji used |
| U8 | `ROADMAP.md` §17/§28 and `bridge.py`'s docstring state only the Fragment half of the bot rule | **recorded, not changed** — documentation honesty item, unrelated to this feature's path |
| U9 | No error-path evidence exists for an ineligible sender | **addressed structurally** — both the exception and the stored result are recorded on every run |

---

## 9. Scope, files and validation of this delivery

**Implemented:** `backend/services/premium_emoji_inline_service.py` (new),
`backend/bot/handlers/emoji.py` (action + builder + report),
`backend/helper/inline_engine.py` (`query_results` / `click_result` /
`inline_unavailable_reason`; `trigger` contract unchanged),
`backend/telegram_api/custom_emoji.py` (`free`, `text_color`),
`backend/telegram_api/_helpers.py` (`premium`),
`tests/test_premium_emoji_inline.py` (new, 50 tests),
`tests/test_emoji_set_enumeration.py` (the two document-shape pins updated for
the additive keys, plus one new flag test).

**Intentionally untouched:** the reaction pipeline (`reaction_service`,
`emoji_react`), the replacement pipeline and the AI/runtime/helper layers, the
POC service and its 62 pinned tests (`tests/test_premium_emoji_probe.py`),
`tests/test_stage13.py`, `DATABASE_ARCHITECTURE.md`, all SQL/migrations, all
dependencies, `backend/requirements.txt`.

**Validation actually performed in this session:** `python -m py_compile` on
every changed module; the focused modules (163 tests across
`test_premium_emoji_inline`, `test_premium_emoji_probe`,
`test_bridge_delivery`, `test_emoji_set_enumeration`) green; the full suite run
twice (`5765 passed, 26 skipped, 1 failed` — the single failure is the
pre-existing load-sensitive diagnostics flake, reproduced on a pristine
worktree at the same revision with a **different** test in the same module);
`git diff --check` clean; the complete diff inspected.

**Not verified:** no live Telegram call, no message sent, no read-back on a
real account, no Supabase call. Nothing in this document claims otherwise.
