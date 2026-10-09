# Investigation — Premium Custom Emoji rendering through the Helper Bot

Canonical record of the forensic investigation into the failed Premium
Telegram Custom Emoji POC (the `💬 Set Reaction Emoji` probe).

- Repository `Onlyicing1/Telegram-self-bot`, branch `main`, baseline HEAD
  `5935ba1` (working tree clean when this investigation began).
- Investigation only: **no production code was changed**, no database was
  contacted, no SQL was executed, and **no request was sent to Telegram from
  this workspace**.
- This file **replaces** the previous `INVESTIGATION.md` (the roadmap
  reconstruction), whose deliverable `ROADMAP.md` remains the authoritative
  roadmap; the replaced content is preserved in git history.

---

## 1. Question under investigation

Why did the Helper Bot display only the ordinary Unicode fallback glyph
instead of the intended Premium Custom Emoji when the owner ran the POC
(`Menu` → Emoji → `💬 Set Reaction Emoji` → reply with a premium emoji), and
is actual Premium Custom Emoji rendering achievable through the current
Helper Bot / API path (the existing helper bot client and its Telethon MTProto
send), or is that path inherently incapable of it?

## 2. Verdict and confidence

**POSSIBLE BUT ACCOUNT-DEPENDENT.**

- **High confidence that the mechanism is feasible.** Telegram's official
  Bot API changelog (February 9, 2026 — Bot API 9.4) states verbatim:
  *"Allowed bots to use custom emoji in messages directly sent by the bot to
  private, group and supergroup chats if the owner of the bot has a Telegram
  Premium subscription."* (source: <https://core.telegram.org/bots/api-changelog>,
  read during this investigation.) Our probe is exactly that case: the helper
  bot sends a direct message into its own private chat with the owner. The
  alternative official route — a bot that purchased additional usernames on
  Fragment — remains valid as well (the constraint the implementation phase
  recorded in `ROADMAP.md` §17/§28).
- **Medium confidence that the live failure was the sender-side entitlement
  gate rather than our pipeline.** The whole local pipeline — extraction, ID
  propagation, entity conversion, and the final serialized Telegram request —
  is proven correct down to the wire bytes (§4, §6). What could not be observed
  from this workspace is the one layer where the failure occurred: Telegram's
  server-side acceptance/storing/rendering decision. That decision depends on
  the helper bot owner's account state (Telegram Premium subscription or a
  Fragment username), which is not inspectable here and was never read back.
- The verdict is therefore not `YES` (rendering was never observed live) and
  not `NO` (no rejection or design flaw was proven), and not `UNKNOWN` (the
  code path is fully proven and the governing official rule is documented).

## 3. Observed behavior

### 3.1 The owner's live observation (reported, not observed by this agent)

- The helper bot's message **was delivered** and the Glass panel reported the
  send as accepted (the `✓ Telegram accepted …` path) — per the owner's
  report; this agent did not see the panel or any network response.
- The delivered message displayed **only the ordinary Unicode glyph** of the
  selected emoji instead of the Premium (sticker-rendered) custom emoji.
- The glyph seen was a real emoji glyph (the same glyph the payload carries),
  not the `▪` placeholder — argued from the report; the exact glyph string was
  never captured programmatically (§11, U1).

### 3.2 Source-level findings (verified in this workspace)

- Extraction is **entity-only**: `inspect_message`
  (`backend/services/premium_emoji_probe_service.py`) reads
  `MessageEntityCustomEmoji.document_id` (rejecting non-positive/bool ids) and
  derives `alt_text` from the entity's own UTF-16 span (`_span_text` →
  `utf16_index_at`); the visible text is never promoted to the emoji identity.
- The payload is `PROOF_PREFIX = "Selected reaction emoji: "` (25 UTF-16
  units) + glyph, with exactly one entity dict `{type:
  "MessageEntityCustomEmoji", offset: 25, length: utf16_length(glyph),
  document_id}`; when Telegram reports no alt text, the glyph falls back to
  `PLACEHOLDER_GLYPH = "▪"` with `used_placeholder=True`.
- Conversion never drops the entity: `dict_entities_to_tl`
  (`backend/telegram_api/_helpers.py`) rebuilds
  `tl_types.MessageEntityCustomEmoji(offset, length, doc_id)`; the type is in
  `SUPPORTED_ENTITY_TYPES` and any unknown type or missing/invalid
  `document_id` **raises** `TelegramAPIError` — there is no discard path.
- The sender is the **helper bot over MTProto**: `bridge.py` takes
  `bot = helper_client.get_client()` (a Telethon bot-token client,
  `backend/helper/client.py`) and calls
  `bot.send_message(peer, text, formatting_entities=…)`. There is **no HTTP
  Bot API path**: zero `api.telegram.org` references in `backend/` or `src/`,
  and `backend/requirements.txt` contains `telethon==1.34.0` with no
  python-telegram-bot / aiogram / pyTelegramBotAPI.
- No parse-mode conversion can occur: Telethon 1.34
  (`telethon/client/messages.py`, the `formatting_entities is None` branch)
  only calls `_parse_message_text` when `formatting_entities is None`; with
  entities supplied it builds `messages.SendMessageRequest(…,
  entities=formatting_entities)` verbatim.

### 3.3 Automated test results

- `tests/test_premium_emoji_probe.py` → **43 passed**, exit 0 (re-run for this
  task, §8). These tests pin the offline path including the exact outbound
  shape at the send boundary (`type(entity) is MessageEntityCustomEmoji`,
  correct `document_id`, UTF-16 `offset`/`length`, exact text, the bot's own
  peer, and that the self client sent nothing).
- Unit tests are **not** evidence of live rendering; they fake the Telegram
  surface by design.

## 4. End-to-end data path

Only as far as the evidence establishes (steps 1–8 proven, step 9 not
observable from this workspace):

1. **Launch** — `backend/bot/handlers/emoji.py`,
   `_react_premium_action` (`action:emoji_react_premium`) sends the selection
   prompt to Saved Messages with the **self client**, records its exact
   `chat_id` + message `id`, and arms the existing pending-input state
   (`set_pending`) for `_react_premium_reply_handler` (90 s handler backstop;
   the existing pending-input expiry contract is unchanged).
2. **Owner's reply** — the handler reads the reply with the self client
   (`get_messages(chat_id, ids=msg_id)`), rejects replies targeting another
   chat, non-replies, and any reply whose `reply_to_msg_id` is not the exact
   selection message id.
3. **ID extraction** — `inspect_message(reply)` returns
   `{kind, document_id, alt_text, detail}`: `custom_emoji` only for a real
   `MessageEntityCustomEmoji` with a usable `document_id`; a plain Unicode
   emoji, media-only/empty reply, or unusable id fails closed.
4. **Payload build** — `build_proof_payload(document_id, alt_text)` produces
   the text, the single entity dict, `fallback_text` and `used_placeholder`
   (§3.2).
5. **Destination resolution** — `_resolve_bot_peer(owner_id)` resolves the
   owner's peer **through the helper bot's own session** (entity cache first,
   then a bounded `iter_dialogs(limit=50)`); no chat ⇒ `E_NO_BOT_CHAT` with
   the honest instruction to press Start on the bot.
6. **Bridge conversion** — `backend/telegram_api/bridge.py::send_reconstructed`
   rebuilds the dict into TL entities via `dict_entities_to_tl` and calls
   `bot.send_message(peer, text, formatting_entities=tl_entities or None,
   reply_to=None)` inside `guarded_await` (30 s bound); Telegram rejection ⇒
   `TelegramAPIError` ⇒ the probe reports `E_SEND` with Telegram's own error.
7. **Client serialization** — Telethon 1.34 builds
   `messages.SendMessageRequest(…, entities=formatting_entities)` without any
   parse-mode pass (§3.2).
8. **Wire bytes** — verified offline by serializing the real request object
   (§6): the custom-emoji constructor, offset, length and document id are
   present in the final request bytes.
9. **Telegram server → rendering in the bot's private chat with the owner** —
   *not observable here*: whether Telegram stored the entity, stripped it, or
   stored it but rendered the fallback glyph was never read back (the POC
   deliberately never fetches the sent message).

## 5. Exact failure point

**Not proven to a single layer.** What is proven: the failure is **past our
serialization** — steps 1–8 of §4 are correct in source, pinned by tests, and
reproduced byte-exactly. The failure lives in step 9 (server-side
acceptance/entitlement/rendering), which this workspace cannot observe.
Competing hypotheses:

| # | Hypothesis | Evidence for | Evidence against | Distinguishing evidence needed |
|---|---|---|---|---|
| H1 | **Sender-side entitlement gate**: the helper bot's owner has no Telegram Premium subscription and the bot has no Fragment-purchased username, so Telegram does not render (or drops) the custom-emoji entity | Official Bot API 9.4 makes rendering conditional on the owner's Premium subscription; the repo's own recorded constraint (`ROADMAP.md` §17/§28) is the Fragment rule; the owner's account state was never checked | The send was not reported as rejected (owner saw the message + `✓`) — so if it applies, it applies as **silent non-rendering**, not an error | Owner-account check (is the bot owner Premium? does the bot have a Fragment username?) + the read-back test in §9 |
| H2 | **Server silently ignores the entity**: the official MTProto custom-emoji documentation says an entity that does not wrap exactly one regular emoji matching the document's `alt` is ignored by the server (no error) | Official doc (<https://core.telegram.org/api/custom-emoji>, read during this investigation); a silent ignore matches the observed "message arrived, glyph only" behavior | If the owner saw exactly the selected emoji's own glyph, the payload span *is* that glyph (copied from the same entity), which normally matches `alt` | Compare the sent span text against the document's `alt` on the read-back message |
| H3 | **Placeholder branch ran** (empty `alt_text` ⇒ payload text ends in `▪`) | The branch exists and is a proven latent defect for H2's rule | The owner reported a real emoji glyph, not `▪` | The exact sent text (read-back) |
| H4 | **Entity stored but not rendered client-side** (renderer/entitlement on the viewing account) | Matches "send accepted, glyph shown" | Cannot be separated from H1 without a read-back | Read-back shows the entity present ⇒ H1/H4 territory; absent ⇒ H2/H3 territory |

No local bug has been proven, so **no code fix was made or is claimed**.

## 6. Outbound request representation

Established by source and by serializing the actual request object offline
(read-only script; no credentials, no network):

| Field | Value | Status |
|---|---|---|
| `text` | `"Selected reaction emoji: " + glyph` (prefix = 25 UTF-16 units; total = 26 chars for a 2-unit glyph) | **Established** (source + serialization) |
| glyph in the live run | the source entity's own alt text | **Reported** (owner saw a real emoji glyph); exact string not captured |
| entity type | `MessageEntityCustomEmoji` — TL constructor `messageEntityCustomEmoji#c8cf05f8` | **Established** |
| `offset` | `25` (UTF-16 code units) | **Established** |
| `length` | `utf16_length(glyph)` — `2` for a supplementary-plane emoji, `1` for a BMP glyph or the `▪` placeholder | **Formula established**; the live value was not captured |
| `document_id` | copied from the incoming message's entity (int64) | **Mechanism established**; the live value is not persisted anywhere (the POC is transient by design) |
| entity count | exactly 1 | **Established** |
| `reply_to` | none | **Established** |
| sending identity | the helper bot (Telethon bot-token client, `StringSession` + `start(bot_token=…)`) | **Established** |
| destination peer | the bot's own private chat with the owner, resolved from the **bot's** session | **Established** |
| API / protocol | MTProto via Telethon 1.34 — `messages.SendMessageRequest(entities=…)`; no HTTP Bot API involved | **Established** |
| Telegram's response / stored message | — | **NOT established** (never read back; no live request from this workspace) |

Byte-level exhibit (reproduced during this task; test document id used as the
value, arbitrary):

```
… 15c4b51c 01000000 f805cfc8 19000000 02000000 8b5118034991744b
    ^null      ^vector count = 1  ^messageEntityCustomEmoji#c8cf05f8
                                    ^offset = 25 (19000000 LE)
                                              ^length = 2 (02000000 LE)
                                                        ^document_id (int64 LE)
```

The premium identity is present in the final Telegram request bytes.

## 7. Telegram constraints and eligibility

**Official Telegram documentation** (read during this investigation):

1. Bot API changelog, **February 9, 2026 / Bot API 9.4**
   (<https://core.telegram.org/bots/api-changelog>): bots may use custom emoji
   in messages **directly sent by the bot** to private, group and supergroup
   chats **if the owner of the bot has a Telegram Premium subscription**. Our
   probe sends exactly such a direct message.
2. The earlier (and still-recorded) official restriction: custom-emoji
   entities could only be used by bots that **purchased additional usernames
   on Fragment** — recorded as verified against the Bot API documentation in
   `ROADMAP.md` §17/§28 during implementation; the 9.4 change adds the
   owner-Premium condition as an additional route. Which condition(s)
   Telegram enforces for this exact send is **not established** here (§11 U3).
3. MTProto custom-emoji documentation
   (<https://core.telegram.org/api/custom-emoji>): the server **silently
   ignores** a custom-emoji entity that does not wrap exactly one regular
   emoji matching the document's `alt`. This is the documented mechanism for
   a "send succeeded but no premium render" outcome.

**Third-party sources** (supporting context only, not authoritative):
python-telegram-bot discussion #3960
(<https://github.com/python-telegram-bot/python-telegram-bot/discussions/3960>)
and StackOverflow 79326533 on Telethon premium-emoji sending
(<https://stackoverflow.com/questions/79326533/telethon-client-doesnt-send-premium-emojies>).

**Implementation inference** (labeled as such): the same server-side
entitlement rules apply to a bot's MTProto sends, not only to HTTP Bot API
calls — the entity passes through the same server; this is inferred from the
server-side wording of the official rules, not from a documented MTProto
statement.

**Unverified assumptions**: none promoted to conclusions — the owner's
Premium status, the bot's Fragment status, and Telegram's actual stored state
for this send are all unknown (§11).

## 8. Tests and live verification

Executed for this task:

| Check | Command | Result |
|---|---|---|
| Focused POC suite | `.venv/bin/python -m pytest tests/test_premium_emoji_probe.py -q` | **43 passed in 0.39 s**, exit **0** |
| Byte-level outbound exhibit | read-only Python script: `build_proof_payload` → `dict_entities_to_tl` → `SendMessageRequest` bytes | fragment `f805cfc8 19000000 02000000 <doc_id LE>` **present** (§6) |
| Bot-API-path check | `grep -rn "api.telegram.org" backend/ src/` + `backend/requirements.txt` | **0 hits**; only `telethon==1.34.0` |
| Whitespace | `git diff --check` | clean (run before commit) |

Recorded from the implementation/investigation pass at HEAD `5935ba1` (not
re-run for this documentation-only change, since no code changed):
`pytest tests -q` → **5695 passed, 26 skipped**, exit 0
(`IMPLEMENTATION_REPORT.md` §8). Note: `IMPLEMENTATION_REPORT.md` §11 still
carries a stale "41/41" row from before the two listener-dispatch tests were
added; §8 and this document's 43 are the current figures.

**Live verification: NOT performed.**

- **No live Telegram request was made** from this workspace — there are no
  credentials here (`freebuff-env list` reports no configured keys), so no
  message was ever sent and **real custom-emoji rendering was never visually
  verified by this agent**. The only live data point is the owner's report
  (§3.1).
- Unit tests fake the Telegram surface and **must not be read as proof of
  live rendering**.

## 9. Minimal next step

One decisive, bounded test — a **live read-back**, run on the deployment that
holds `SESSION_STRING` and `BOT_TOKEN` (it cannot run in this workspace):

1. Run the existing POC once: `Menu` → Emoji → `💬 Set Reaction Emoji` →
   reply to the Saved Messages selection message with a real premium emoji
   from Telegram's own picker. The panel reports `✓ … message #<id> carrying
   a REAL custom-emoji entity for document #<N>`.
2. With the **helper bot's** session, fetch that exact message
   (`get_messages(owner_id, ids=<id>)`) and inspect `message.entities` and
   the stored text.

Outcome mapping (this alone separates H1–H4):

- **Entity present, glyph still displayed** ⇒ Telegram stored it; the gap is
  the sender/viewer entitlement (H1/H4) ⇒ owner action, **no code change**:
  give the bot's owner a Telegram Premium subscription (Bot API 9.4) or buy a
  Fragment username for the bot, then re-run.
- **Entity absent** ⇒ the server silently ignored/stripped it (H2/H3) ⇒
  compare the stored span text with the document's `alt`; only if the
  placeholder path (`▪`) actually ran would a minimal code follow-up be
  justified — and it must be proven by this read-back first.
- **`E_SEND`** ⇒ Telegram's exact error text decides the next move.

No redesign of the Emoji Library, reactions, or the bridge is needed or
justified by the current evidence.

## 10. Scope and files

**Inspected during this investigation:** `backend/services/premium_emoji_probe_service.py`,
`backend/bot/handlers/emoji.py` (POC section), `backend/telegram_api/bridge.py`,
`backend/telegram_api/_helpers.py`, `backend/telegram_api/entities.py`,
`backend/helper/client.py`, `backend/helper/input_state.py`,
`backend/helper/inline_sender.py`, `.venv/.../telethon/client/messages.py`,
`tests/test_premium_emoji_probe.py`, `backend/requirements.txt`,
`IMPLEMENTATION_REPORT.md`, `ROADMAP.md` (§17/§28), plus the official Telegram
pages cited in §7.

**Files changed:**

| File | When | Why |
|---|---|---|
| `INVESTIGATION.md` | this task (docs only) | replaced with this canonical record |
| — production code — | — | **none**: the working tree was clean at `5935ba1` when this investigation began, and the investigation itself made no code change |

**Intentionally left untouched:** the Emoji Library and its import/scan flow,
pagination, categories and mappings; the replacement pipeline and
`reaction_service.py` (no evidence implicates it); all Supabase/SQL and
`DATABASE_ARCHITECTURE.md`; the AI and runtime layers; the POC panel text and
`ROADMAP.md` §17/§28 (their Fragment-only wording predates Bot API 9.4 —
updating it is a documentation follow-up, not part of this task).

## 11. Remaining uncertainty

- **U1** — The live run's exact glyph, `length`, and `document_id` were never
  captured (the POC persists nothing), so the payload actually sent on that
  run is known only by construction, not by record.
- **U2** — Whether Telegram **stored** the entity, **stripped** it, or stored
  it and rendered the fallback is unknown by design: the POC never reads the
  sent message back. This is the core gap (§9).
- **U3** — Which official eligibility condition governs this exact send
  (owner Premium per Bot API 9.4, Fragment username per the earlier rule, or
  both) is documented as requirements but not verified against our accounts;
  the owner's account states are unknown.
- **U4** — H1 vs H2 vs H3 vs H4 remain open hypotheses (§5); no local bug is
  proven, and none was "fixed".
- **U5** — The owner's report of panel success (`✓`) and message delivery was
  not observed first-hand by this agent; no network response was inspected.
- **U6** — The inference in §7 (server rules applying identically to MTProto
  bot sends) is reasoned from official server-side wording, not from a
  documented MTProto guarantee.
