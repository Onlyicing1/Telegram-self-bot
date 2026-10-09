# Investigation — Premium Custom Emoji rendering through the Helper Bot

Canonical record of the forensic investigation into the failed Premium
Telegram Custom Emoji POC (the `💬 Set Reaction Emoji` probe), **now
instrumented**: the POC carries a nine-stage structured trace and reads the
exact message it sent back through the helper bot's own session, so the next
live run produces a decisive diagnosis instead of an interpretation.

- Repository `Onlyicing1/Telegram-self-bot`, branch `main`.
- Baseline HEAD for this pass: `06eeb8d` (the workspace was fast-forwarded from
  `3b0e476` to `origin/main` before editing — the POC commits `aed86aa`,
  `5935ba1`, `06eeb8d` and the Phase 3–6 emoji work are included; no rebase, no
  history rewrite).
- This pass **changed code**: the trace, the read-back, the diagnosis
  classification and their tests. No database was contacted, **no SQL was
  executed**, no migration was touched, and **no request was sent to Telegram
  from this workspace**.
- Prior revision of this file (the investigation-only pass at `5935ba1`, which
  changed no code) is preserved in git history; its finding — *the code path is
  proven to the wire bytes; the one unobserved layer is Telegram's stored
  state* — is carried forward here and is what this pass instrumented.

---

## 1. Question under investigation

Why did the Helper Bot display only the ordinary Unicode fallback glyph
instead of the intended Premium Custom Emoji when the owner ran the POC
(`Menu` → Emoji → `💬 Set Reaction Emoji` → reply with a premium emoji), and
is actual Premium Custom Emoji rendering achievable through the current Helper
Bot / API path (the existing helper bot client and its Telethon MTProto send),
or is that path inherently incapable of it?

The follow-up question this pass had to answer offline: **can the POC itself
produce evidence that distinguishes those possibilities, without a live
account?** Yes — by reading the exact sent message back and classifying only
what it shows (§5, §6).

## 2. Verdict and confidence

**POSSIBLE BUT ACCOUNT-DEPENDENT; the deciding observation is now built in.**

- **High confidence the mechanism is feasible.** Telegram's official Bot API
  changelog (February 9, 2026 — Bot API 9.4) states verbatim: *"Allowed bots to
  use custom emoji in messages directly sent by the bot to private, group and
  supergroup chats if the owner of the bot has a Telegram Premium
  subscription."* (source: <https://core.telegram.org/bots/api-changelog>, read
  during the previous pass.) Our probe is exactly that case: the helper bot
  sends a direct message into its own private chat with the owner. The
  alternative official route — a bot that purchased additional usernames on
  Fragment — remains valid as well (the constraint recorded in `ROADMAP.md`
  §17/§28).
- **Medium confidence the live failure was the sender-side entitlement gate
  rather than our pipeline.** Every local stage is proven in source and by
  tests (§4, §9) and the final request bytes carry the custom-emoji
  constructor, offset, length and document id (§7). What could not be observed
  from this workspace is the one layer where the failure occurred: Telegram's
  server-side accept/store/render decision.
- **No local code defect was demonstrated, so none was "fixed".** This pass
  added instrumentation only; the send algorithm (same bridge, same single
  attempt, same entities, no fallback) is unchanged (§8).
- **Account eligibility remains UNRESOLVED** (§10, §11): the owner has no
  Telegram Premium; whether the helper bot holds a Fragment-purchased username
  is unknown; the Bot API 9.4 owner-Premium rule and the earlier Fragment rule
  are documented, but which one governs this exact MTProto bot send is not
  verified against the real accounts — and no entitlement probing was added
  (that would need new credentials/privileged access).

The verdict is therefore not `YES` (rendering was never observed live), not
`NO` (no rejection, and no design flaw, was proven), and not `UNKNOWN` (the
code path is fully proven and the governing official rules are documented).

## 3. Observed behavior

### 3.1 The owner's live observation (reported, not observed by this agent)

- The helper bot's message **was delivered** and the Glass panel reported the
  send as accepted (the `✓ Telegram accepted …` path) — per the owner's report;
  this agent did not see the panel or any network response.
- The delivered message displayed **only the ordinary Unicode glyph** of the
  selected emoji instead of the Premium (sticker-rendered) custom emoji.
- The glyph seen was a real emoji glyph (the same glyph the payload carries),
  not the `▪` placeholder — argued from the report; the exact glyph string was
  never captured programmatically (U1).

### 3.2 What the previous pass established in source (unchanged)

- Extraction is **entity-only**: `inspect_message`
  (`backend/services/premium_emoji_probe_service.py`) reads
  `MessageEntityCustomEmoji.document_id` (rejecting non-positive/bool ids) and
  derives the span text from the entity's own UTF-16 span; the visible text is
  never promoted to the emoji identity.
- The payload is `PROOF_PREFIX = "Selected reaction emoji: "` (25 UTF-16
  units) + glyph, with exactly one entity dict
  `{type: "MessageEntityCustomEmoji", offset: 25, length: utf16_length(glyph),
  document_id}`; when Telegram reports no span text, the underlying text falls
  back to `PLACEHOLDER_GLYPH = "▪"` with `used_placeholder=True`.
- Conversion never drops the entity: `dict_entities_to_tl`
  (`backend/telegram_api/_helpers.py`) rebuilds
  `tl_types.MessageEntityCustomEmoji(offset, length, doc_id)`; any unknown type
  or missing/invalid `document_id` **raises** `TelegramAPIError` — there is no
  discard path.
- The sender is the **helper bot over MTProto**: `bridge.py` takes
  `bot = helper_client.get_client()` (a Telethon bot-token client) and calls
  `bot.send_message(peer, text, formatting_entities=…)`. There is **no HTTP Bot
  API path**: zero `api.telegram.org` references in `backend/` or `src/`, and
  `backend/requirements.txt` contains `telethon==1.34.0` with no
  python-telegram-bot / aiogram / pyTelegramBotAPI.
- No parse-mode conversion can occur: Telethon 1.34
  (`telethon/client/messages.py`) only calls `_parse_message_text` when
  `formatting_entities is None`; with entities supplied it builds
  `messages.SendMessageRequest(…, entities=formatting_entities)` verbatim.
- **The POC never read the sent message back** — that was the single
  unobserved layer, and the gap this pass closed.

## 4. The send pipeline and its trace stages

`backend/services/premium_emoji_probe_service.py` emits one structured line per
stage through `_trace()` (tag `[PREMIUM_PROBE]`, `logger.info`, `stage
key=value …`); the conversion stage is emitted by the bridge itself (tag
`[BRIDGE]`). Nothing logged is session data, a token, an access hash, a whole
message or unrelated history — only ids, offsets, lengths, bounded quoted spans
and honest reason strings.

| # | Stage | Emitted by | Fields | What it proves |
|---|---|---|---|---|
| 1 | `SOURCE_ENTITY_FOUND` | `inspect_message` | `found`, `usable`, `document_id` | whether the owner's reply really carried a `MessageEntityCustomEmoji` (the visible glyph is never the source of truth) |
| 2 | `SOURCE_ENTITY_VALIDATED` | `inspect_message` | `document_id`, `offset`, `length`, `span` (bounded) | the exact entity geometry extracted from the reply |
| 3 | `OUTBOUND_ENTITY_BUILT` | `build_proof_payload` (+ `valid`, `issue` when it fails validation) | `entity_type`, `document_id`, `offset`, `length`, `text_utf16_len`, `used_placeholder` | the exact payload handed to the bridge, and whether the placeholder branch ran |
| 4 | `BRIDGE_ENTITY_CONVERTED` | `backend/telegram_api/bridge.py` | `type=MessageEntityCustomEmoji`, `count`, `spans` (`offset:length:document_id`) | the dict→TL conversion really produced Telethon `MessageEntityCustomEmoji` objects, with the expected geometry |
| 5 | `SEND_STARTED` | `deliver_proof` | `started`, `path=helper_bot_bridge`, `via=helper_bot_client`, geometry, `entity_count` | which sender/client path is used (the existing helper-bot bridge) |
| 6 | `SEND_ACCEPTED` | `deliver_proof` | `accepted`, `message_id`, `error` (on refusal) | whether Telegram accepted the send and the returned message id |
| 7 | `READBACK_STARTED` | `_read_back` | `started`, `via=helper_bot_client`, `message_id`, `exact=True`, `reason` when it cannot start | that the read-back targets THE EXACT sent id through the bot's own session |
| 8 | `READBACK_RESULT` | `_read_back` | `fetched`, `entity_present`, `document_id`, `document_id_match`, `offset`, `length`, `span_match`, `expected_span`/`stored_span` (bounded), `text_utf16_len` | what Telegram actually stored for that message |
| 9 | `DIAGNOSIS` | `_read_back` / `deliver_proof` | `diagnosis` | the ONE outcome the evidence establishes (§6) |

`DOCUMENT_ALT` (`document_id`, `alt`, `match`, `error`) is emitted only when the
read-back did **not** retain a matching entity (§5).

## 5. The read-back mechanism

Implemented in `premium_emoji_probe_service._read_back`, after a successful
send:

1. **Exact-id fetch only.** `_fetch_sent_message(bot, peer, message_id)` calls
   the **helper bot's own client** — `bot.get_messages(peer, ids=<the id the
   send returned>)`, with `peer` being the same bot-session peer the send used
   (`_resolve_bot_peer`). There is no recent-messages scan, no "latest message"
   inference and no unrelated history read: the tests pin
   `bot.readbacks == [{"peer": ("bot-peer", OWNER), "ids": 4321}]` and
   `dialog_scans == 0`.
2. **Bounded** by the project's existing watchdog convention:
   `guarded_await(..., name="telegram:premium_probe:readback",
   timeout=30.0)`. A timeout is an `OperationTimeoutError` (an
   `asyncio.TimeoutError` subclass) and is reported as a read-back failure.
3. **Fetch failure ≠ send failure.** A missing message, a raised error or a
   timeout sets `readback.ok = False` + `readback.error`, leaves `ok` (the send
   fact) alone, and classifies `READBACK_FAILED`.
4. **Inspection of the returned message** uses the same entity-only scan as the
   source (`_scan_custom_emoji`): the returned object's **raw** text
   (`.message`, not the client-rendered `.text`) and its real `entities`.
   Recorded: `entity_present`, the stored entity's `document_id`/`offset`/
   `length`/span text, `document_id_match`, `span_match` (offset **and** length
   **and** span text equal to what was sent), and `stored_text_utf16_len`.
5. **Document-alt comparison** (only when the entity was not retained and
   matching): the expected document is resolved through the **existing** typed
   wrapper `backend/telegram_api.custom_emoji.get_custom_emoji_documents`
   (`messages.GetCustomEmojiDocumentsRequest`, bounded at 30 s, normalized to
   `TelegramAPIError`), and its `alt` is compared with the span that was sent.
   This is the comparison the H2/H3 hypotheses need: it separates "the span did
   not match the document" from "the span matched but the entity was still
   dropped". A lookup failure records `document_alt_error` and changes nothing
   about the diagnosis.
6. **No new infrastructure**: no second client, no update loop, no scheduler,
   no database, no schema — the read-back is one bounded call on the client the
   bridge already uses.

`deliver_proof` returns a stable result dict: `ok` (send fact), `error`,
`detail`, `message_id`, `diagnosis`, `readback` (all keys always present) and
the payload keys (`text`, `entities`, `entity`, `fallback_text`,
`used_placeholder`). `readback_summary(readback)` renders the same evidence as
one honest sentence for the Glass panel.

## 6. Diagnostic outcomes — what each proves, and what it does not

| Outcome | Evidence | Proves | Does NOT prove | Next action |
|---|---|---|---|---|
| `SOURCE_ENTITY_MISSING` | no real `MessageEntityCustomEmoji` (or an unusable id) on the owner's reply | the source path never produced a Premium identity; nothing was sent | anything about Telegram | investigate the selection/input path (reply target, picker) |
| `OUTBOUND_ENTITY_INVALID` | `validate_proof_payload` rejected the payload | a construction defect in the payload (type/id/offset/length/span/entity count) | rendering | fix the demonstrated construction defect — this is the "minimal code fix" branch |
| `SEND_FAILED` | the bridge raised (helper bot down, no bot chat, or Telegram refused) | Telegram's verdict on the send, with its own error text | whether the entity would have been retained had the send succeeded | read the error: `E_SEND`'s Telegram text names the restriction |
| `READBACK_FAILED` | the exact-message fetch errored, timed out, or returned nothing | nothing about the entity — only that the evidence could not be obtained | whether the entity was retained | re-run; check helper-bot connectivity/permissions |
| `ENTITY_STRIPPED_OR_MISSING` | the fetched message has **no** custom-emoji entity | Telegram did not store the entity (silently ignored / dropped) | *why* — the `document_alt` fields carry the discriminator | if `document_alt_match` is false → the entity's underlying text did not match the document's `alt` (construction/alt issue); if true → server-side entitlement (H1) territory |
| `ENTITY_MISMATCH` | entity present but a different `document_id`, or an invalid/mismatched span | Telegram stored *an* entity, not the one we sent | rendering | investigate the exact mismatch (wrong id substituted, span shifted) |
| `ENTITY_RETAINED_RENDER_UNVERIFIED` | entity present with the expected id **and** span | Telegram stored exactly the entity we sent | **that the owner's client displayed a Premium emoji** — the client, not the read-back, decides that | owner looks at the bot's message; if it still shows a plain glyph, this is the eligibility/rendering limitation (H1/H4), not a code defect |

`ENTITY_RETAINED_RENDER_UNVERIFIED` is deliberately **not** called a success:
read-back can establish retention, never display.

## 7. Outbound request representation

Established by source, by tests, and (previous pass) by serializing the real
request object offline (read-only script; no credentials, no network):

| Field | Value | Status |
|---|---|---|
| `text` | `"Selected reaction emoji: " + glyph` (prefix = 25 UTF-16 units) | **Established** |
| entity type | `MessageEntityCustomEmoji` — TL constructor `messageEntityCustomEmoji#c8cf05f8` | **Established** |
| `offset` / `length` | `25` / `utf16_length(glyph)` | **Established** |
| `document_id` | copied from the incoming message's entity (int64) | **Established** (per run) |
| entity count | exactly 1 | **Established** |
| `reply_to` | none | **Established** |
| sending identity | the helper bot (Telethon bot-token client) | **Established** |
| destination peer | the bot's own private chat with the owner, from the **bot's** session | **Established** |
| API / protocol | MTProto via Telethon 1.34 — `messages.SendMessageRequest(entities=…)`; no HTTP Bot API | **Established** |
| Telegram's response / stored message | read back by id in the POC and classified by `DIAGNOSIS` | **Observable on the next live run** (not observed here) |

Byte-level exhibit from the previous pass (test document id used as the value,
arbitrary):

```
… 15c4b51c 01000000 f805cfc8 19000000 02000000 8b5118034991744b
    ^null      ^vector count = 1  ^messageEntityCustomEmoji#c8cf05f8
                                    ^offset = 25 (19000000 LE)
                                              ^length = 2 (02000000 LE)
                                                        ^document_id (int64 LE)
```

The premium identity is present in the final Telegram request bytes.

## 8. Is there a demonstrable code defect?

**No — and none was invented.** Findings from this pass:

- The outbound payload is now validated against itself before it is sent
  (`validate_proof_payload`): entity type, real document id, usable UTF-16
  offset/length, the span covering exactly the fallback text, exactly one
  entity. A payload that fails any of these is **never sent** and is classified
  `OUTBOUND_ENTITY_INVALID`. Offline, the builder produces valid payloads for
  every tested input (real alt text and the placeholder branch).
- The span text sent is the **entity's own** span from the owner's reply, so it
  is the same text Telegram attached to that document for that sender; when
  Telegram reports no span text, the placeholder `▪` becomes the underlying
  text and `used_placeholder=True` records it. That is the only branch that
  could plausibly conflict with the server's "wrap exactly one regular emoji
  matching the document's alt" rule, and the read-back now measures it instead
  of assuming it (`document_alt`, `document_alt_match`,
  `used_placeholder`).
- The conversion produces real `MessageEntityCustomEmoji` objects (pinned by
  tests and by the new `BRIDGE_ENTITY_CONVERTED` line); there is no discard
  path.

Therefore: **no fix beyond instrumentation was made**, and the send algorithm
is unchanged (`send-first`, one attempt, the existing bridge, no glyph
fallback). If the payload validation ever fires in production, that *is* the
demonstrable defect and it fails closed — but it has not fired in any test.

## 9. Tests and live verification

Executed for this pass:

| Check | Command | Result |
|---|---|---|
| Compile | `.venv/bin/python -m py_compile` on all four changed Python files | **clean** |
| Focused POC suite | `.venv/bin/python -m pytest tests/test_premium_emoji_probe.py -q` | **62 passed**, exit 0 (was 43; +19 read-back/diagnosis/trace tests) |
| Bridge suite | `.venv/bin/python -m pytest tests/test_bridge_delivery.py -q` | **23 passed**, exit 0 (+1: the conversion trace fires for custom-emoji entities only and an unrelated formatting-only send is unchanged) |
| Emoji/Reaction regression | `.venv/bin/python -m pytest tests/test_emoji_ui.py tests/test_emoji_ui_phase2.py tests/test_emoji_state_phase3.py tests/test_emoji_replacement_phase4.py tests/test_emoji_composition_phase5.py tests/test_reaction_phase6.py tests/test_bridge_delivery.py tests/test_emoji_library_import.py tests/test_emoji_category_service.py tests/test_emoji_set_enumeration.py tests/test_premium_emoji_probe.py -q` | **555 passed**, exit 0 |
| Full suite | `.venv/bin/python -m pytest tests -q --ignore=telegram-self-bot` | **5715 passed, 26 skipped**, exit 0 (119.73 s). Pre-task baseline 5695 + 19 + 1 = 5715 — every added test accounted for |
| Whitespace | `git diff --check` | exit 0 |

`--ignore=telegram-self-bot` excludes the pre-existing untracked mirror clone
in the workspace (never touched by this task); its own `tests/conftest.py`
otherwise interferes with collection.

The new tests cover, with fakes only (**no live Telegram, no credentials**):
a successful send followed by a read-back that retains the expected entity; a
read-back with no entity (with and without a matching document `alt`, and with
the alt lookup failing); a mismatched `document_id`; an invalid span; a send
failure; a read-back failure and a read-back timeout; a message returned as
nothing; a send with no message id; correct exact-id targeting with no dialog
scan; the classification table; outbound validation; the panel texts; and the
full trace (all nine stages present, no token/hash text logged).

**Live verification: NOT performed.**

- **No live Telegram request was made** from this workspace — there are no
  credentials here, so no message was ever sent, no read-back ever ran against
  Telegram and **real custom-emoji rendering was never visually verified by
  this agent**. The only live data point is the owner's report (§3.1).
- The read-back itself is offline-tested; its *live* output is exactly what the
  next owner run will produce and record via `DIAGNOSIS`.
- Unit tests fake the Telegram surface and **must not be read as proof of live
  rendering**.

## 10. Remaining uncertainty

- **U1** — The live run's exact glyph, `length` and `document_id` were never
  captured (the POC persists nothing); from this pass on, the trace and the
  panel record them for every run.
- **U2** — Whether Telegram **stored** the entity, **stripped** it, or stored
  it and rendered the fallback is decided by the next live run's read-back
  (`ENTITY_RETAINED_RENDER_UNVERIFIED` vs `ENTITY_STRIPPED_OR_MISSING` vs
  `ENTITY_MISMATCH`). It cannot be decided from this workspace.
- **U3 — account eligibility remains UNRESOLVED.** Which official condition
  governs this exact send (owner Premium per Bot API 9.4, a Fragment-purchased
  bot username per the earlier rule, or both) is documented as a requirement
  but not verified against the real accounts; the owner's Telegram Premium
  status ("no Premium") and the bot's Fragment status (unknown) are unchanged
  by this pass. **No entitlement probing was added** — that would require new
  credentials or privileged access.
- **U4** — H1/H2/H3/H4 (§3.2 and below) remain open; no local bug is proven and
  none was "fixed".

Hypotheses and the evidence that now distinguishes them:

| # | Hypothesis | Distinguishing evidence (now produced automatically) |
|---|---|---|
| H1 | sender-side entitlement gate (owner Premium / Fragment username) | `ENTITY_RETAINED_RENDER_UNVERIFIED` with a plain glyph still shown, **or** `ENTITY_STRIPPED_OR_MISSING` with `document_alt_match = true` |
| H2 | the server silently ignored the entity (span ≠ document `alt`) | `ENTITY_STRIPPED_OR_MISSING` with `document_alt_match = false` |
| H3 | the placeholder branch ran (empty span text ⇒ `▪`) | `used_placeholder = true` in the `OUTBOUND_ENTITY_BUILT` line |
| H4 | the entity was stored but not rendered by the viewing client | `ENTITY_RETAINED_RENDER_UNVERIFIED` — retention proven, display still unproven |

## 11. The exact remaining live step (owner-run)

One bounded run on the deployment that holds `SESSION_STRING` and `BOT_TOKEN`:

1. `Menu` → **Emoji** → `💬 Set Reaction Emoji`.
2. Open Saved Messages and reply to **that** selection message with a real
   Premium/Custom Emoji from Telegram's own picker (not a copy-pasted Unicode
   glyph).
3. Read the Glass panel: it prints the read-back summary and
   `Diagnosis <OUTCOME>` — and the logs carry the nine stages
   (`SOURCE_ENTITY_*`, `OUTBOUND_ENTITY_BUILT`, `BRIDGE_ENTITY_CONVERTED`,
   `SEND_STARTED`, `SEND_ACCEPTED`, `READBACK_STARTED`, `READBACK_RESULT`,
   `DIAGNOSIS`).
4. Act on the outcome per §6: a payload/construction outcome is a code fix; a
   stripped-or-retained outcome with a matching `alt` points at sender
   eligibility, which is the owner's account decision, **not** a code change.

No redesign of the Emoji Library, the reaction system or the bridge is needed
or justified by the current evidence.

## 12. Scope and files

**Inspected:** `backend/services/premium_emoji_probe_service.py`,
`backend/bot/handlers/emoji.py` (POC section), `backend/telegram_api/bridge.py`,
`backend/telegram_api/_helpers.py`, `backend/telegram_api/entities.py`,
`backend/telegram_api/custom_emoji.py`, `backend/telegram_api/exceptions.py`,
`backend/helper/client.py`, `backend/helper/input_state.py`,
`backend/helper/inline_sender.py`, `backend/runtime/tracer.py`,
`backend/runtime/operation_watchdog.py`,
`.venv/.../telethon/client/messages.py`, `tests/test_premium_emoji_probe.py`,
`tests/test_bridge_delivery.py`, `backend/requirements.txt`, `ROADMAP.md`
(§17/§28), plus the official Telegram pages cited in §2.

**Files changed:**

| File | Change |
|---|---|
| `backend/services/premium_emoji_probe_service.py` | the nine-stage trace, the exact-id read-back, the outbound payload validation, the document-`alt` comparison, the diagnosis classification and `readback_summary` |
| `backend/bot/handlers/emoji.py` | the POC panel now prints the read-back summary and the diagnosis (the UI still holds no send logic) |
| `backend/telegram_api/bridge.py` | ONE conditional log line (`BRIDGE_ENTITY_CONVERTED`) when a custom-emoji entity was reconstructed; no behavior change for any caller |
| `tests/test_premium_emoji_probe.py` | +19 read-back/diagnosis/trace/panel tests |
| `tests/test_bridge_delivery.py` | +1 test pinning the trace's custom-emoji-only blast radius |
| `INVESTIGATION.md`, `IMPLEMENTATION_REPORT.md` | this record and the current-state report |

**Intentionally left untouched:** the Emoji Library import/scan flow,
pagination, categories and mappings; the replacement pipeline and
`reaction_service.py`; all Supabase/SQL and `DATABASE_ARCHITECTURE.md`; the AI
and runtime layers; `ROADMAP.md` §17/§28 (their Fragment-only wording predates
Bot API 9.4 — a documentation follow-up, not part of this task).
