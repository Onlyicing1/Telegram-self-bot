# Investigation — Premium Custom Emoji rendering through the Helper Bot

Canonical record of the investigation into the failed Premium Telegram Custom
Emoji POC (the `💬 Set Reaction Emoji` probe). **This revision is the closure
record**: the instrumented POC produced live production evidence, the outbound
request was traced end-to-end in the installed Telethon, and the POC's fate is
decided below.

- Repository `Onlyicing1/Telegram-self-bot`, branch `main`.
- Baseline HEAD for this pass: `1149851` (working tree clean except the
  pre-existing untracked `telegram-self-bot/` mirror).
- This pass **changed no production code and no test**: it traced the real
  request, evaluated the live evidence, and updated this record plus
  `IMPLEMENTATION_REPORT.md`. No database was contacted, **no SQL was
  executed**, no migration was touched, and **no request was sent to Telegram
  from this workspace**.
- Previous revisions of this file (the investigation-only pass at `5935ba1` and
  the instrumentation pass at `3c282c2`) are preserved in git history.

---

## 1. Question under investigation

Why did the Helper Bot display only the ordinary Unicode glyph instead of the
intended Premium Custom Emoji when the owner ran the POC (`Menu` → Emoji →
`💬 Set Reaction Emoji` → reply with a premium emoji), and is actual Premium
Custom Emoji rendering achievable through the current Helper Bot / API path (the
existing helper bot client and its Telethon MTProto send), or is that path
incapable of it?

Follow-ups this pass had to answer: does the **final outbound request** really
carry the intended entity (not just the converted Python object), does any
intermediate step drop it, and does the surviving evidence establish the
**cause** or only the **location** of the failure?

## 2. Verdict

**THE ENTITY IS DROPPED SERVER-SIDE — CONFIRMED. THE EXACT TELEGRAM RESTRICTION
IS NOT ESTABLISHED.**

- **Confirmed:** the outbound `messages.SendMessageRequest` built by the real
  (installed) Telethon 1.34.0 through this project's bridge contains the
  intended `MessageEntityCustomEmoji` — real document id, UTF-16 offset 25,
  length 2 — and those bytes are present in the serialized request; the send
  used the helper bot's client and the bot's own private peer; Telegram accepted
  it (message id `1618`); the message read back by exact id through the bot's
  own session has **no** custom-emoji entity. Nothing in our client path can
  account for that difference (§3, §4).
- **No demonstrable code defect exists**, and none was fixed (§8).
- **Likely but not proven:** the drop is the *sender-side custom-emoji
  entitlement* (a bot may use custom emoji only if it purchased additional
  usernames on Fragment, or — since Bot API 9.4 — if the bot's owner has a
  Telegram Premium subscription), enforced silently. **What is missing:** that
  rule is documented for the **Bot API**; no official documentation applies it
  to a bot's **MTProto** sends, and no official page documents silent dropping
  for ineligible senders. See §10.
- **Excluded by the live evidence:** the documented MTProto silent-ignore
  condition (the entity must wrap exactly one regular emoji matching the
  document's `alt`) — it was satisfied; the placeholder branch; and a
  renderer-only explanation (the entity was never stored).

## 3. The live production evidence

### 3.1 Owner's run (deployment logs, quoted verbatim; not produced by this agent)

```text
SOURCE_ENTITY_FOUND found=True document_id=5352934405201494110
SOURCE_ENTITY_VALIDATED document_id=5352934405201494110 offset=0 length=2 span='😵'
OUTBOUND_ENTITY_BUILT entity_type=MessageEntityCustomEmoji document_id=5352934405201494110 offset=25 length=2 text_utf16_len=27 used_placeholder=False
SEND_STARTED started=True path=helper_bot_bridge via=helper_bot_client document_id=5352934405201494110 offset=25 length=2 entity_count=1 text_utf16_len=27
BRIDGE_ENTITY_CONVERTED type=MessageEntityCustomEmoji count=1 spans=25:2:5352934405201494110
SEND_ACCEPTED accepted=True message_id=1618
READBACK_STARTED started=True via=helper_bot_client message_id=1618 exact=True
READBACK_RESULT entity_present=False text_utf16_len=27 expected_span='😵' stored_span='😵' span_match=True
DOCUMENT_ALT document_id=5352934405201494110 alt='😵' match=True
DIAGNOSIS diagnosis=ENTITY_STRIPPED_OR_MISSING
```

Reading: source entity real; outbound payload carries the live document id and
did **not** use the placeholder; Telegram accepted the send; the message fetched
back by id through the helper bot's own session carries no custom-emoji entity,
while its text and the text under the expected span match what was sent, and
that span equals the document's real `alt`. The visible result the owner
reported (an ordinary Unicode emoji) follows from the missing entity.

### 3.2 Source-level findings (verified in this workspace)

- Extraction is **entity-only** (`inspect_message` →
  `_scan_custom_emoji`): only a real `MessageEntityCustomEmoji` with a usable
  `document_id` counts; the span text comes from the entity's own UTF-16 span,
  never from the visible text.
- The payload is `PROOF_PREFIX = "Selected reaction emoji: "` (25 UTF-16 units)
  + glyph, one entity dict `{type, offset: 25, length, document_id}`;
  `used_placeholder` records the `▪` fallback — `False` in the live run.
- `dict_entities_to_tl` rebuilds the dict into
  `tl_types.MessageEntityCustomEmoji(offset, length, document_id)` and raises
  on unknown types or missing payloads — there is no discard path.
- The sender is the **helper bot over MTProto** (`helper/client.py` →
  `bridge.send_reconstructed` → `bot.send_message(…)`); there is **one** send
  path and no fallback/alternate send (`forward_messages`,
  `SendMessagesRequest` and `send_file` appear nowhere in the POC path).
- There is **no HTTP Bot API involvement**: zero `api.telegram.org` references
  in `backend/` or `src/`; `backend/requirements.txt` pins `telethon==1.34.0`.
- Installed Telethon 1.34.0 (`telethon/client/messages.py`,
  `TelegramClient.send_message`, plain-string branch) uses `formatting_entities`
  **verbatim** — the parse-mode pass is skipped when it is provided — and builds
  `messages.SendMessageRequest(…, entities=formatting_entities, …)`; nothing
  filters or rewrites the list.

## 4. The real outbound request (this pass's decisive offline check)

Previous passes proved the *converted object*; this pass captured the **request
the real `TelegramClient.send_message` builds** by driving the project's own
bridge with a real (unconnected) `TelegramClient` whose transport call was
captured, using the live values:

```
request type : SendMessageRequest            peer: InputPeerUser
message      : 'Selected reaction emoji: 😵'
entities     : [<telethon.tl.types.MessageEntityCustomEmoji>]  ctor 0xc8cf05f8
entity doc   : 5352934405201494110   (== live document id)
entity span  : offset=25 length=2    (== live trace 25/2)
reply_to     : None | no_webpage: False | reply_markup: None

final TL bytes (96 total):
6f090d28 08000000 4ca5e8dd28db0b00 00000000 01000000 …
… 15c4b51c 01000000 f805cfc8 19000000 02000000 5e8000007b71494a
            ^entities   ^ctor      ^offset 25 ^length 2 ^document_id (int64 LE)
```

The custom-emoji constructor, offset (25), length (2) and document id are in the
serialized request — the same object the MTProto sender writes. Combined with
§3.1, the entity demonstrably left our client intact and did not survive on
Telegram's side.

## 5. The send pipeline and its trace stages

The instrumented POC logs one structured line per stage (`[PREMIUM_PROBE] <STAGE>
key=value …`; the conversion stage is logged by the bridge as
`[BRIDGE] BRIDGE_ENTITY_CONVERTED …`). Nothing logged is a session string,
token, access hash, whole message or unrelated history.

| # | Stage | Emitted by | Records |
|---|---|---|---|
| 1 | `SOURCE_ENTITY_FOUND` | `inspect_message` | whether the reply carried a real custom-emoji entity (and whether its id is usable) |
| 2 | `SOURCE_ENTITY_VALIDATED` | `inspect_message` | the entity's `document_id`, `offset`, `length`, bounded span |
| 3 | `OUTBOUND_ENTITY_BUILT` | `build_proof_payload` | outbound type/id/offset/length, text length, `used_placeholder` |
| 4 | `BRIDGE_ENTITY_CONVERTED` | `bridge.py` | that conversion produced `MessageEntityCustomEmoji` objects (`offset:length:document_id`) |
| 5 | `SEND_STARTED` | `deliver_proof` | `path=helper_bot_bridge`, `via=helper_bot_client`, geometry, entity count |
| 6 | `SEND_ACCEPTED` | `deliver_proof` | whether Telegram accepted the send, the message id, or the refusal |
| 7 | `READBACK_STARTED` | `_read_back` | that the read-back targets the exact sent id via the bot's own client |
| 8 | `READBACK_RESULT` | `_read_back` | what Telegram stored: entity present?, same id/offset/length/span?, stored text length |
| 9 | `DIAGNOSIS` | `_read_back` / `deliver_proof` | the one outcome the evidence establishes (§7) |

`DOCUMENT_ALT` (`document_id`, `alt`, `match`, `error`) is emitted only when the
read-back did not retain a matching entity.

## 6. The read-back mechanism

* **Exact-id fetch only** — `bot.get_messages(peer, ids=<the id the send
  returned>)` on the **helper bot's own client**, with the same bot-session peer
  the send used. No recent-messages scan, no "latest message" inference, no
  unrelated history (pinned by tests).
* **Bounded** by the existing watchdog convention (`guarded_await`,
  `telegram:premium_probe:readback`, 30 s); a timeout is a read-back failure.
* **Read-back failure ≠ send failure** — `ok` stays the send fact while the
  diagnosis becomes `READBACK_FAILED`.
* **Inspection** uses the same entity-only scan as the source, on the returned
  message's **raw** text (`.message`), and records `entity_present`, the stored
  entity's `document_id`/`offset`/`length`/span, `document_id_match`,
  `span_match` and `stored_text_utf16_len`.
* **Document-`alt` comparison** — only when the entity was not retained and
  matching, the expected document is resolved through the existing typed wrapper
  (`telegram_api.custom_emoji.get_custom_emoji_documents`) and its `alt` is
  compared with the span that was sent. A lookup failure is recorded, never
  fatal.
* **No new infrastructure** — no second client, loop, scheduler, database or
  schema.

## 7. Diagnostic outcomes — what each proves, and what it does not

| Outcome | Evidence | Proves | Does NOT prove |
|---|---|---|---|
| `SOURCE_ENTITY_MISSING` | no real custom-emoji entity (or an unusable id) on the reply | nothing was sent | anything about Telegram |
| `OUTBOUND_ENTITY_INVALID` | `validate_proof_payload` rejected the payload | a construction defect | rendering |
| `SEND_FAILED` | the bridge raised (helper bot down, no bot chat, or Telegram refused) | Telegram's verdict on the send, with its own error text | whether the entity would have survived |
| `READBACK_FAILED` | the exact-message fetch errored, timed out or returned nothing | only that evidence could not be obtained | whether the entity was retained |
| `ENTITY_STRIPPED_OR_MISSING` | the fetched message has **no** custom-emoji entity | Telegram did not store the entity; `document_alt*` separate a span mismatch from a server-side drop | *why* (unless the alt comparison settles it) |
| `ENTITY_MISMATCH` | entity present with a different id or an invalid span | Telegram stored *an* entity, not the one sent | rendering |
| `ENTITY_RETAINED_RENDER_UNVERIFIED` | entity present with the expected id and span | Telegram stored exactly what we sent | **that a Premium emoji was displayed** |

`ENTITY_RETAINED_RENDER_UNVERIFIED` is never treated as a render success;
read-back proves retention, never display.

## 8. Is there a demonstrable code defect? — **No**

* The outbound payload validates (`validate_proof_payload` returns `""`) and the
  captured request carries the correct entity **and** geometry (§4).
* The entity type, document id, UTF-16 offset/length/span, account (helper bot)
  and peer (the bot's private chat with the owner) are all correct, and
  `messages.sendMessage` with `messageEntityCustomEmoji` entities is exactly the
  mechanism the official MTProto custom-emoji page documents.
* There is a single send path with no fallback/alternate sender, and no
  intermediate conversion, normalization or wrapper can drop the entity.

The only thing that would ever constitute a defect here is
`OUTBOUND_ENTITY_INVALID`; it has never fired in tests or in the live run. No
code was changed, and none is justified.

## 9. Tests and validation (this pass)

| Check | Command | Result |
|---|---|---|
| Compile | `python -m py_compile` on the POC service, bridge, handler and both POC test files | exit **0** |
| Focused POC suite | `python -m pytest tests/test_premium_emoji_probe.py -q` | **62 passed**, exit 0 |
| Bridge delivery suite | `python -m pytest tests/test_bridge_delivery.py -q` | **23 passed**, exit 0 |
| Emoji/Reaction regression | `python -m pytest tests/test_emoji_ui.py tests/test_emoji_ui_phase2.py tests/test_emoji_state_phase3.py tests/test_emoji_replacement_phase4.py tests/test_emoji_composition_phase5.py tests/test_reaction_phase6.py tests/test_emoji_library_import.py tests/test_emoji_category_service.py tests/test_emoji_set_enumeration.py -q` | **470 passed**, exit 0 |
| Full suite | `python -m pytest tests -q --ignore=telegram-self-bot` | **5715 passed, 26 skipped**, exit 0 (118.71 s) |
| Whitespace | `git diff --check` | exit **0** |
| Outbound-request capture | read-only script driving the real `TelegramClient.send_message` through the project's bridge | entity + geometry present in the serialized request (§4) |

`--ignore=telegram-self-bot` excludes the pre-existing untracked mirror clone
(never touched); its `tests/conftest.py` otherwise interferes with collection.

**Offline only.** No live Telegram request was made from this workspace, so the
tests and the capture prove the pipeline and the request bytes — never live
rendering. The single live data point is the owner's production run (§3.1).

## 10. Remaining uncertainty (what the evidence cannot settle)

* **U1 — the exact restriction.** The entitlement rule (Fragment username /
  bot-owner Premium) is stated in the **Bot API** documentation; the MTProto
  custom-emoji page, the `messages.sendMessage` schema page and the MTProto bots
  page contain **no** such requirement, and the schema's generic
  `PREMIUM_ACCOUNT_REQUIRED` error did not occur in our run. Whether the Bot API
  entitlement is enforced for a bot's MTProto `sendMessage` — and whether the
  enforcement is a silent drop — is **not documented and not proven**.
* **U2 — the silent-drop mechanism itself.** No official page documents what
  happens when an ineligible sender attaches a `messageEntityCustomEmoji`; the
  observed behavior (delivered message, entity removed, no error) is consistent
  with a silent drop but establishes only the fact, not the reason.
* **U3 — owner/bot account state.** The owner has no Telegram Premium; whether
  the helper bot holds a Fragment-purchased username is unknown. **No
  entitlement probing was added** (it would need new credentials/privileged
  access), and none should be added to close this POC.
* **U4 — the excluded hypotheses now have negative evidence** (see §11 table):
  the documented "entity must wrap exactly one regular emoji matching the
  document's `alt`" ignore-condition was satisfied, so it does not explain this
  run.

## 11. Hypotheses, before and after the live evidence

| # | Hypothesis | Status after the live run |
|---|---|---|
| H1 | sender-side entitlement gate (Fragment username / bot-owner Premium) enforced by the server | **Remaining, likely, unproven** — consistent with everything observed; not documented for MTProto |
| H2 | the server ignored the entity because the span did not match the document's `alt` | **Excluded** — `DOCUMENT_ALT … match=True`, span exactly one 2-unit emoji |
| H3 | the placeholder branch ran (empty span text ⇒ `▪`) | **Excluded** — `used_placeholder=False` |
| H4 | the entity was stored but the viewing client did not render it | **Excluded** — the entity was not stored at all |
| H5 | an undocumented server-side drop unrelated to entitlement | **Possible, indistinguishable from H1 with the available evidence** |

## 12. Disposition

**Close the POC.** It has produced its evidence and a reproducible diagnosis,
and the remaining variable is account-side, not code:

* Reopening it requires an eligibility change (a Fragment-purchased username for
  the helper bot, or a Premium subscription for the bot's owner). The existing
  POC is then re-runnable **unchanged** — no code work is needed — and its panel
  would report `ENTITY_RETAINED_RENDER_UNVERIFIED`, which only the owner can
  visually confirm.
* Do not add a Unicode/alt-text fallback, do not switch APIs or accounts, and do
  not wire custom emoji into the reaction workflow.
* Recorded for the owner's decision only (not tested, not built here): official
  Telegram materials state users may use custom emoji free of charge in Saved
  Messages, so an owner-account delivery path may exist. That is a product
  decision and would be a new task, not a fix for this one.

## 13. Scope and files

**Inspected:** `backend/services/premium_emoji_probe_service.py`,
`backend/bot/handlers/emoji.py` (POC section), `backend/telegram_api/bridge.py`,
`backend/telegram_api/_helpers.py`, `backend/telegram_api/entities.py`,
`backend/telegram_api/custom_emoji.py`, `backend/telegram_api/exceptions.py`,
`backend/helper/client.py`, `backend/helper/input_state.py`,
`backend/helper/inline_sender.py`, `backend/runtime/operation_watchdog.py`,
`.venv/.../telethon/client/messages.py` (Telethon 1.34.0),
`tests/test_premium_emoji_probe.py`, `tests/test_bridge_delivery.py`,
`backend/requirements.txt`, plus the official Telegram pages cited below.

**Official sources consulted (this pass and the previous one):**
<https://core.telegram.org/bots/api> (custom-emoji entitlement wording),
<https://core.telegram.org/bots/api-changelog> (Bot API 9.4, February 9, 2026),
<https://core.telegram.org/api/custom-emoji> (entity definition and the
"must wrap exactly one regular emoji … otherwise the server will ignore it"
rule), <https://core.telegram.org/method/messages.sendMessage> ("Both users and
bots can use this method"; error list), <https://core.telegram.org/api/bots>,
<https://telegram.org/faq_premium>, <https://telegram.org/blog/custom-emoji>.

**Files changed:**

| File | Change |
|---|---|
| `IMPLEMENTATION_REPORT.md` | replaced with the current-state root-cause/closure report |
| `INVESTIGATION.md` | this closure record |

**Intentionally left untouched:** the Emoji Library import/scan flow, pagination,
categories and mappings; the replacement pipeline, state/composition and
`reaction_service.py`; all Supabase/SQL and `DATABASE_ARCHITECTURE.md`; the AI,
runtime and helper-bot lifecycle layers; `ROADMAP.md` §17/§28; the POC's own
code and tests (no defect found, so no change was made); the separate Hermes
repository.
