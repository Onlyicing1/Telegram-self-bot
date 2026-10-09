# IMPLEMENTATION_REPORT.md — Current Execution Report

> **This file describes ONLY the current state after the latest task.** It is not
> a changelog and no older report is kept below it.
>
> **This task changed no production code and no test.** It is the root-cause
> investigation and closure decision for the Premium Custom Emoji POC. **No
> SQL was executed, no database was contacted, no migration was touched, and
> `DATABASE_ARCHITECTURE.md` was not modified.**

---

## 1. Task covered

**Final Premium Custom Emoji POC — root cause and closure (2026-10-09).**

The owner's production run produced the read-back evidence the previous pass
instrumented, and it is decisive about *where* the custom-emoji entity stops
working. This task traced the real outbound request through the installed
Telethon, examined every intermediate step for a defect, established what the
official documentation does and does not prove, and decided the POC's fate.

**Result: no code defect exists; the entity is dropped server-side; the exact
Telegram restriction is not established by available evidence. No code change
was made. The POC is recommended for closure (§9).**

## 2. The production evidence (owner's live run)

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

What this establishes:

* the reply really carried a custom-emoji entity (2 UTF-16 units, span `'😵'`);
* the outbound payload carried the real document id and the placeholder branch
  did **not** run (`used_placeholder=False`);
* Telegram **accepted** the send and assigned message id `1618`;
* the message fetched back by id through the helper bot's own session contains
  **no** `MessageEntityCustomEmoji`, while its text is the sent text (27 UTF-16
  units) and the text under the expected span equals the sent span, which in
  turn equals the document's real `alt` (`match=True`).

The stored message therefore lost the entity **after** our client's request.

## 3. Trace of the actual outbound request (source-proven)

Followed the real path — POC service → `send_reconstructed` → the helper bot's
Telethon client → MTProto — using the **installed Telethon 1.34.0**:

1. **Which exact method performs the final send.**
   `backend/telegram_api/bridge.py::send_reconstructed` calls
   `bot.send_message(peer, text, formatting_entities=tl_entities or None,
   reply_to=…)` on `helper_client.get_client()` — the optional helper bot's
   Telethon (`bot-token`) client — inside `guarded_await`
   (`telegram:bridge:send_reconstructed`, 30 s). There is **one** send path in
   the POC and **no** fallback/alternate send: `grep` over the POC path finds
   only this call, and `forward_messages` / `SendMessagesRequest` /
   `send_file` appear nowhere in it.
2. **Whether the final request receives the intended entity.**
   In installed Telethon 1.34.0 (`telethon/client/messages.py`,
   `TelegramClient.send_message`, the plain-string branch):

   ```python
   if formatting_entities is None:
       message, formatting_entities = await self._parse_message_text(message, parse_mode)
   ...
   request = functions.messages.SendMessageRequest(
       peer=entity,
       message=message,
       entities=formatting_entities,
       ...
   )
   ```

   `formatting_entities` is used **verbatim**; the parse-mode pass is skipped
   entirely when it is provided, and nothing filters, rewrites or re-parses the
   list. The client then `await self(request)`s that exact object.

   Verified offline against the current source with the **live values** by
   driving the project's real bridge with a real (unconnected) `TelegramClient`
   whose transport call was captured:

   ```
   request type : SendMessageRequest          peer: InputPeerUser
   message      : 'Selected reaction emoji: 😵'
   entities     : [<telethon.tl.types.MessageEntityCustomEmoji>]  ctor 0xc8cf05f8
   entity doc   : 5352934405201494110  (== live document id)
   entity span  : offset=25 length=2   (== live trace 25/2)
   reply_to     : None | no_webpage: False | reply_markup: None

   final TL bytes (96): …15c4b51c01000000 f805cfc8 19000000 02000000 5e8000007b71494a
                       ^entities vector  ^ctor      ^offset 25 ^length 2 ^document_id
   ```

   The custom-emoji constructor, the offset (25), the length (2) and the
   document id are present **in the serialized request** — this is the object
   Telethon's MTProto sender writes, not merely a converted Python object.
3. **Whether any intermediate step drops or replaces the entity.** No.
   `_helpers.dict_entities_to_tl` rebuilds the dict into the real TL type and
   **raises** on anything unknown; the bridge forwards the list unchanged;
   `premium_emoji_probe_service` performs no further transformation; no
   wrapper, normalizer or alternate sender exists on this path.
4. **Which account/client and peer.** The **helper bot** sends (never the self
   client), to the bot's own private chat with the owner, resolved from the
   bot's session (`_resolve_bot_peer`). The live trace confirms
   `path=helper_bot_bridge via=helper_bot_client` and a delivered message.
5. **Whether this is a supported mechanism.** Yes: attaching
   `messageEntityCustomEmoji` entities to `messages.sendMessage` is exactly
   what the official MTProto custom-emoji page documents, and Telethon 1.34.0
   supports the entity type (`SendMessageRequest.entities` is a
   `Vector<MessageEntity>`).

So the outbound construction is **correct**; `BRIDGE_ENTITY_CONVERTED` was
never the weak point, and the byte-level capture now proves the request itself.
The live read-back independently shows the entity was nevertheless not stored.

## 4. Root-cause conclusion

### Confirmed (evidence-backed)

* **The failure is server-side.** The entity is present in the final serialized
  `messages.SendMessageRequest`; Telegram accepted the send; the exact message
  read back from Telegram has no custom-emoji entity. Nothing in our client
  path can account for the difference.
* **No demonstrable code defect exists** in the source path: correct entity
  type, document id, UTF-16 offset/length/span, correct account (helper bot),
  correct peer (the bot's private chat with the owner), correct method
  (`messages.sendMessage` with entities), single send path, no fallback.

### Likely (consistent, NOT proven)

* **The entity was silently ignored because the sending bot is not entitled to
  send custom emoji.** The official Bot API documentation states this
  entitlement for bots ("Custom emoji entities can only be used by bots that
  purchased additional usernames on Fragment **or** in the messages directly
  sent by the bot to private, group and supergroup chats if the owner of the
  bot has a Telegram Premium subscription"), and the live behavior — message
  delivered, entity gone, no error — matches a silent entitlement drop. This is
  consistent with the owner having no Telegram Premium and no known Fragment
  username for the helper bot.

  **What is NOT proven:** the entitlement rule is documented for the **Bot
  API**; no official Telegram documentation states that it applies to a bot's
  **MTProto** sends (the `messages.sendMessage` schema merely says "Both users
  and bots can use this method" and the MTProto custom-emoji page contains no
  entitlement wording), and no official page documents that an ineligible
  sender's entity is silently dropped rather than rejected. No error was
  returned in the live run, and the generic `PREMIUM_ACCOUNT_REQUIRED` error
  listed for `messages.sendMessage` does not state that it covers custom-emoji
  entities. **The exact restriction therefore remains unestablished.**

### Excluded by the live evidence

* **The documented MTProto silent-ignore condition.** The custom-emoji page says
  the entity "must wrap exactly one regular emoji (the one contained in
  `documentAttributeCustomEmoji.alt`) … otherwise the server will ignore it".
  The live run satisfied it: the entity wrapped exactly one emoji (`length=2`),
  the stored span equals the sent span, and the sent span equals the document's
  resolved `alt` (`match=True`).
* **The placeholder branch** (`used_placeholder=False`).
* **A renderer-only problem** (H4 from the earlier investigation): the entity
  was never stored, so no client could render it.

## 5. What a code fix would require — and why none was made

There is no defect to correct. Every alternative that could conceivably change
the outcome is out of this task's scope and/or unsupported:

* switching to a different API, client or bot account — not a fix, a redesign
  (`AGENTS.md` §13.10 single-client rule; task forbids it);
* sending from the owner's user account — a different product decision, not a
  correction of this POC;
* buying a Fragment username or a Premium subscription — an account/ownership
  decision, and per §4 its applicability to MTProto is not established;
* an alt-text/Unicode fallback — explicitly prohibited (it would conceal the
  failure and misreport a Unicode glyph as a Premium emoji);
* changing the entity's offsets/length/span or the document id — the geometry
  is proven correct and matching.

Therefore: **no production code changed; no test changed; no speculative
instrumentation added.** The existing read-back and diagnostic classifications
already answer the question and are left exactly as they are.

## 6. Files changed

| File | Change |
|---|---|
| `IMPLEMENTATION_REPORT.md` | replaced with this current-state report |
| `INVESTIGATION.md` | updated to record the live evidence, the excluded hypotheses and the closure conclusion |

No other file changed. `git status --short` before the commit lists exactly
these two documents; the pre-existing untracked `telegram-self-bot/` mirror is
untouched.

## 7. Tests and validation actually executed

All commands run in this workspace against the current tree (no code changed,
so no test was added, weakened or suppressed):

| Check | Command | Result |
|---|---|---|
| Compile | `.venv/bin/python -m py_compile` on the POC service, bridge, handler and both POC test files | exit **0** |
| Focused POC suite | `.venv/bin/python -m pytest tests/test_premium_emoji_probe.py -q` | **62 passed**, exit 0 |
| Bridge delivery suite | `.venv/bin/python -m pytest tests/test_bridge_delivery.py -q` | **23 passed**, exit 0 |
| Emoji/Reaction regression | `.venv/bin/python -m pytest tests/test_emoji_ui.py tests/test_emoji_ui_phase2.py tests/test_emoji_state_phase3.py tests/test_emoji_replacement_phase4.py tests/test_emoji_composition_phase5.py tests/test_reaction_phase6.py tests/test_emoji_library_import.py tests/test_emoji_category_service.py tests/test_emoji_set_enumeration.py -q` | **470 passed**, exit 0 |
| Full suite | `.venv/bin/python -m pytest tests -q --ignore=telegram-self-bot` | **5715 passed, 26 skipped**, exit 0 (118.71 s) |
| Whitespace | `git diff --check` | exit **0** |
| Outbound-request capture | read-only script: real `TelegramClient.send_message` through the project's bridge → captured `SendMessageRequest` → serialized | entity + geometry present in the bytes (§3) |

`--ignore=telegram-self-bot` excludes the pre-existing untracked mirror clone in
the workspace (never touched by this task); its own `tests/conftest.py`
otherwise interferes with collection.

**These are offline results only. They do not verify live Telegram rendering and
are not claimed to.**

## 8. Live Telegram behavior

**Not tested by the coding agent.** This workspace has no `SESSION_STRING`, no
`BOT_TOKEN` and no Telegram account, so no request was sent and no live read-back
was performed here. The live evidence in §2 is the owner's production run,
quoted verbatim from the deployment logs; this agent neither observed nor
reproduced it.

Remaining owner-side verification: none is required to close the POC. If the
owner decides to revisit it after an eligibility change (§9), the existing POC
is re-runnable as-is and will print the diagnosis; no code work is needed.

## 9. Disposition: close the POC

**Recommendation: close the Premium Custom Emoji POC as blocked by a
server-side (account/platform) restriction, not by our code.** Do not expand it,
do not add a fallback, do not wire custom emoji into the reaction workflow.

* The POC has done its job: it produced the decisive evidence (§2, §3) and a
  reproducible diagnosis, offline-pinned by tests.
* The remaining variable is not code. If the helper bot later gains a
  Fragment-purchased username, or the bot's owner subscribes to Telegram
  Premium, the POC can be re-run unchanged: `ENTITY_RETAINED_RENDER_UNVERIFIED`
  would then show in the panel, and only the owner can confirm the visual render.
* Reopening it without such a change would repeat the same evidence.

One documented, *unverified* fact is recorded for the record only, and is a
product decision outside this task: official Telegram materials state that users
can use custom emoji free of charge **in Saved Messages**, so an owner-account
delivery path may exist. This POC never tested it, no such path was built, and
establishing it would be a new (design) task — not a fix for this one.

## 10. Delivery metadata

| Item | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` (connected workspace remote; never hardcoded into code) |
| Branch | `main` |
| Baseline before this pass | `1149851` (`docs: re-verify the official custom-emoji rules for the probe diagnosis`) |
| This pass's commit | the scoped `docs:` commit carrying this report and `INVESTIGATION.md` |
| Push | `git push origin main` — fast-forward only; no rebase, no force-push, no history rewrite |
| Working tree | clean except the pre-existing untracked `telegram-self-bot/` mirror (never touched) |

## 11. Push verification

Verified against the live remote after the push (`git fetch origin`,
`git rev-parse HEAD`, `git rev-parse origin/main`,
`git ls-remote origin refs/heads/main`, `git status --short`); the result is
recorded in the final report of this pass. No success is claimed unless the
remote branch actually contains the commit.
