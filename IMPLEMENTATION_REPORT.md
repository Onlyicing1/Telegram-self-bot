# IMPLEMENTATION_REPORT.md — Current Execution Report

> **This file describes ONLY the current state after the latest task.** It is not
> a changelog and no older report is kept below it.
>
> **This task changed no SQL and contacted no database.** Supabase stays
> manual-only and the Emoji & Reaction schema is untouched.

---

## 1. Task / stage covered

**POC — "Premium Custom Emoji: Self Bot extracts the real identity, the Helper
Bot renders it" (2026-10-08).**

A *proof of concept only*, deliberately narrow. The request was to prove ONE
technical capability and explicitly NOT to build the Premium Emoji Library
redesign:

> Can the Self Bot receive a real Telegram Premium/Custom Emoji from a user's
> reply in Saved Messages, extract the REAL custom-emoji identity, and then make
> the EXISTING HELPER TELEGRAM BOT display that same Premium Custom Emoji in a
> bot-generated message?

**Result: the technical capability is IMPLEMENTED AND PROVEN OFFLINE.** The
whole chain exists in code and is pinned by tests: a real
`MessageEntityCustomEmoji` on the owner's reply → its real `document_id` → the
existing helper-bot bridge → a message the HELPER BOT sends carrying a real
`MessageEntityCustomEmoji`. **Live Telegram rendering was NOT verified** (no
live Telegram account is available to the coding agent) — see §9 and §11.

## 2. What the POC does (end to end)

1. **Launch.** The Emoji panel (Glass UI) gains ONE new action:
   `💬 Set Reaction Emoji` (`action:emoji_react_premium`). Nothing else in the
   Emoji/Reaction UI changed.
2. **Deterministic selection message.** The self client sends a plain-text
   instruction message to **Saved Messages** (`client.send_message("me", …)`):
   *"Reply to this message with the Premium Emoji you want to use. Only a reply
   to THIS message is accepted."* The returned message's **exact** `id` and
   `chat_id` are recorded — carried into the existing pending-input `extra`
   slot. There is no "latest message", no history scan, no keyword matching.
3. **Waiting.** The existing pending-input reply mode is armed (the same
   machinery Deep Save and the Phase 6 reaction flow already use) — **no new
   listener, no new panel, no new input registration**.
4. **Deterministic resolution.** The owner's reply is read back and accepted
   **only** when it is an outgoing message in that Saved Messages chat whose
   `reply_to_msg_id` equals the recorded selection message id, and it does not
   target another chat. Anything else is refused with an honest panel line.
5. **Entity extraction (the source of truth).** `inspect_message()` walks the
   message's real entities and accepts only a real
   `MessageEntityCustomEmoji` with a usable `document_id`. The visible glyph,
   the alt text, the caption and the message text are never used to decide
   whether a Premium emoji was sent.
6. **Helper-bot rendering.** `deliver_proof()` builds the bot message payload
   and hands it to the **existing** bridge (`backend/telegram_api/bridge.py`),
   which sends it from the **helper bot's own client** as a NEW bot message.
   The self client never renders the Premium emoji.

## 3. Exact files changed

| File | Change |
|---|---|
| `backend/services/premium_emoji_probe_service.py` | **NEW** — the POC's deterministic layer: entity inspection, helper-bot payload building, bridge delivery, honest result codes |
| `backend/bot/handlers/emoji.py` | Main panel button + the POC action and its reply handler (one new section, placed after the existing shared edit helpers); no existing behaviour changed |
| `backend/telegram_api/bridge.py` | ONE optional keyword-only parameter added: `resolved_peer=` on `send_reconstructed` (an already-resolved input peer for a destination the self client cannot express as the bot's peer). Default `None` ⇒ every existing caller keeps the unchanged same-destination resolution |
| `tests/test_premium_emoji_probe.py` | **NEW** — 41 focused offline tests |
| `IMPLEMENTATION_REPORT.md` | This document |

Nothing else changed: `git status --short` before the commit lists exactly these
files (one modified module, one modified bridge, two new files, this report).

Explicitly **not** changed: the Emoji Library import/scanning pipeline, sticker-set
scanning, library pagination, the category/mapping model, the emoji replacement
pipeline, the existing reaction behaviour, `reaction_service.py`,
`backend/db/client.py`, `DATABASE_ARCHITECTURE.md`, `ROADMAP.md`, `AGENTS.md`,
every migration, the `RuntimeSupervisor`, the dispatcher, the provider manager,
the `ToolExecutor`, the AI architecture, media processing and the helper-bot
client/lifecycle modules.

## 4. Exact Telegram entity representation used

**Telethon side (what the self client reads from the reply):**

```
telethon.tl.types.MessageEntityCustomEmoji
    .offset        # UTF-16 code units, Telegram's entity currency
    .length        # UTF-16 code units
    .document_id   # the REAL Telegram custom-emoji document id
```

**Bridge side (what the helper bot sends), the project's existing serialized
entity dict — rebuilt by `_helpers.dict_entities_to_tl` into the real TL type
for `send_message(formatting_entities=…)`:**

```python
{
    "type": "MessageEntityCustomEmoji",
    "offset": <utf16 units of "Selected reaction emoji: ">,
    "length": <utf16 units of the fallback glyph>,
    "document_id": <the REAL document id from the reply's entity>,
}
```

The bot's message text is `"Selected reaction emoji: <glyph>"` and the entity
covers exactly the `<glyph>` span. The glyph is only the entity's *underlying
text*; the rendered emoji comes from `document_id` alone. When Telegram reports
no alt text, a plainly non-emoji placeholder (`▪`) becomes that underlying text
so the entity always has a ≥1-unit span — `used_placeholder` records that, and
the id is still the real one.

## 5. How the Self Bot extracts the custom-emoji identity

`premium_emoji_probe_service.inspect_message(message)` →

* iterates `message.entities` and accepts **only** an instance of
  `telethon.tl.types.MessageEntityCustomEmoji`;
* requires a usable `document_id` (a positive `int`, never a bool); an entity
  with an unusable id returns `kind = "none"` with the honest reason
  (`"…carries a custom-emoji entity with an unusable document id"`);
* derives the alt text **from the entity's own UTF-16 span** using the existing
  `utf16_index_at` helper — a corrupt/out-of-range span yields `""`, never a
  clamped guess;
* otherwise returns `kind = "unicode"` (the reply's own visible text — *not* a
  Premium emoji) or `kind = "none"` (empty/media-only reply), each with the
  reason. **Nothing is inferred from the glyph, the text, the caption, a
  filename or a filename-like field.**

## 6. How the Helper Bot renders it

`premium_emoji_probe_service.deliver_proof(self_client, owner_id, document_id,
alt_text)`:

1. refuses an invalid owner (`E_OWNER`) or an invalid document id
   (`E_NO_CUSTOM_EMOJI`) **before** any Telegram call;
2. refuses honestly when the helper bot is not connected (`E_NO_HELPER`) — with
   **no** alt-text fallback send;
3. resolves the destination through the **helper bot's own session** (its entity
   cache first, then a bounded 50-dialog scan of the bot's own dialogs) — the
   destination is the helper bot's private chat with the owner, whose access
   hash only the bot's session has. No chat ⇒ `E_NO_BOT_CHAT` with the honest
   instruction to open the bot and press Start;
4. calls the existing bridge `send_reconstructed(self_client, owner_id, text,
   entities=[…], resolved_peer=…)` — **exactly one attempt**, marked
   `long_running`-free, no retry and no different representation;
5. returns an honest result dict: `ok` is True only when Telegram accepted the
   bot's send; every failure carries a stable code (`E_SEND`), the exact
   Telegram error text, and the payload that was (or would have been) sent.

The helper bot — never the self client — builds and sends the message that
carries the custom-emoji entity.

**Capability boundary (unchanged from the project's recorded, verified
constraint — `ROADMAP.md` §17):** Telegram only lets a bot use custom-emoji
entities if it purchased additional usernames on Fragment. The POC therefore
does not promise rendering: it constructs the correct entity, sends it once, and
reports Telegram's verdict as-is. A rejection surfaces as `E_SEND` with
Telegram's own error; it is never converted into a "success" with a Unicode
glyph.

## 7. Focused tests executed

`tests/test_premium_emoji_probe.py` — **41 tests, all passing**, covering the
eight required cases plus the whole offline path:

| # | Required case | Test(s) |
|---|---|---|
| 1 | A Telegram Custom Emoji entity is correctly recognized | `test_a_real_custom_emoji_entity_is_recognized_and_its_id_is_extracted`, `…_wins_over_the_neighbouring_text` |
| 2 | The real custom emoji ID is extracted | `test_the_document_id_comes_from_the_entity_not_from_the_glyph`, `test_the_alt_text_is_read_from_the_entity_span_in_utf16_units` |
| 3 | A normal Unicode emoji is NOT a Custom Emoji | `test_a_plain_unicode_emoji_is_not_a_custom_emoji`, `…_bold_entity_with_a_glyph…`, `test_a_unicode_emoji_reply_is_refused_and_never_sent_as_premium` |
| 4 | Missing entities fail closed | `test_an_empty_reply_has_no_custom_emoji`, `test_a_media_only_reply_has_no_custom_emoji`, `test_a_custom_emoji_entity_with_an_unusable_id_fails_closed`, `test_a_corrupt_entity_span_yields_no_alt_text_and_still_fails_closed` |
| 5 | A reply to the wrong message is rejected | `test_a_reply_to_a_different_message_is_rejected`, `test_a_reply_in_another_chat_is_rejected`, `test_a_non_reply_is_rejected` |
| 6 | The exact selection message id is respected | `test_the_action_sends_the_selection_message_and_records_its_exact_id`, `test_an_unknown_selection_id_is_refused_before_any_read`, `test_the_selection_reply_is_delivered_by_the_helper_bot_end_to_end` |
| 7 | The helper-bot payload carries the correct entity/identifier | `test_the_payload_carries_the_real_custom_emoji_entity`, `test_the_helper_bot_sends_the_real_custom_emoji_entity`, `test_the_helper_bot_peer_can_come_from_its_own_dialog_list` |
| 8 | The visible fallback text alone is NOT proof | `test_an_unavailable_helper_bot_reports_failure_and_sends_no_fallback`, `test_a_rejected_send_is_reported_without_any_glyph_substitution`, `test_the_success_panel_never_claims_the_premium_render_by_itself`, `test_the_failure_panel_says_the_entity_was_not_rendered` |

Plus honest-failure tests (no self client, failed selection send, unusable
selection id, unreadable reply), registration through the existing registry, and
architecture pins (no second client/loop/scheduler/listener, no AI/recovery
imports, no database/schema surface, the UI holds no send logic and the service's
only delivery is the existing bridge). The Telegram surface is faked; no live
Telegram account and no Supabase call is required.

## 8. Test results (this pass, 2026-10-08)

| Check | Command | Result |
|---|---|---|
| Focused POC tests | `python -m pytest tests/test_premium_emoji_probe.py -q` | **41 passed**, exit 0 |
| Emoji/Reaction regression | `python -m pytest tests/test_emoji_ui.py tests/test_emoji_ui_phase2.py tests/test_emoji_state_phase3.py tests/test_emoji_replacement_phase4.py tests/test_emoji_composition_phase5.py tests/test_reaction_phase6.py tests/test_bridge_delivery.py tests/test_emoji_library_import.py tests/test_emoji_category_service.py tests/test_emoji_set_enumeration.py -q` | **492 passed**, exit 0 |
| Full suite | `python -m pytest tests -q` | **5693 passed, 26 skipped**, exit 0 (119.90s) — the previous run was 5652 passed, i.e. +41 = exactly this POC's tests |
| Compile | `python -m py_compile` on all four changed Python files | clean |
| Whitespace | `git diff --check` | clean |
| Scope | `git status --short` | only the four changed files + this report |

No check was skipped, weakened or suppressed; nothing is reported as passing
that was not run.

## 9. Live Telegram verification — NOT performed

**Live Telegram rendering was not verified.** The coding agent has no live
Telegram account, no `SESSION_STRING` and no `BOT_TOKEN` in this workspace, so
no message was ever sent to Telegram and no Premium emoji was ever rendered.
Unit tests cannot prove that Telegram renders a custom emoji, and this report
does not claim they do.

The owner can perform the live check in five steps:

1. `Menu` → **Emoji** → `💬 Set Reaction Emoji`.
2. Open Saved Messages; the selection message is there.
3. Reply to **that** message with a real Premium/Custom Emoji from Telegram's
   own picker (not a copy-pasted Unicode glyph).
4. Watch the helper bot's private chat with the owner: it receives
   `Selected reaction emoji: …` whose emoji must be the **Premium** one.
5. The Glass panel reports the outcome: `✓ Telegram accepted …` plus the
   document id, or `✗` with Telegram's exact error.

The test is **successful only if the bot's message shows the real Premium
custom emoji**. A plain glyph there is a failure.

## 10. Known limitations (honest)

1. **Live rendering unverified** (§9) — the one thing only Telegram can prove.
2. **The helper bot's Fragment capability is the hard constraint.** A plain bot
   cannot reliably use custom-emoji entities; if the helper bot has not bought
   an additional username on Fragment, Telegram will reject the send and the POC
   will report `E_SEND` with Telegram's own error. That is the exact technical
   boundary, reported — **not** hidden behind a Unicode fallback.
3. **The bot needs a private chat with the owner** (open the bot, press Start)
   for the demo message; otherwise the POC reports `E_NO_BOT_CHAT` honestly. The
   bot cannot post the proof into Saved Messages itself (a bot cannot write
   there), so the demo lands in the bot's own chat with the owner.
4. **The existing 120 s pending-input state expiry applies** (unchanged
   contract): the owner replies within it, or launches the action again.
5. **Transient by design:** the selected emoji is not persisted, not imported
   into the library and not applied as a reaction — the POC proves the
   rendering boundary only.
6. The alt-text placeholder (`▪`) is used only when Telegram reports no alt
   text, purely so the entity has an underlying span; it never substitutes for
   the custom emoji identity.

## 11. Success / failure statement against the request

| Requirement | State |
|---|---|
| User can launch the POC | ✅ `💬 Set Reaction Emoji` in the Emoji panel |
| A deterministic Saved Messages selection message is created | ✅ recorded exact `chat_id` + message `id` |
| The user can reply to that exact message with a Premium emoji | ✅ implemented (live step owner-run) |
| The Self Bot extracts the REAL Telegram custom-emoji identifier | ✅ from `MessageEntityCustomEmoji.document_id`, entity-only |
| The helper bot receives the actual custom-emoji identity | ✅ one serialized entity dict → real TL entity |
| The helper bot constructs a proper custom-emoji entity | ✅ `MessageEntityCustomEmoji(offset, length, document_id)` via the existing bridge |
| The resulting helper-bot message is capable of rendering it | ✅ constructed and sent **once**; rendering itself needs the owner's live check + the Fragment capability |
| Focused tests pass | ✅ 41/41 (full suite 5693 passed, 26 skipped) |
| No unrelated architecture modified | ✅ scope table in §3 |
| Live rendering proven | ❌ **not verified** — no live Telegram available |

Nothing in the request's "DO NOT TOUCH" list was modified. Explicitly:

* **No Emoji Library import redesign was performed.** The import/scan path,
  sticker-set scanning, pagination, categories, mappings and the replacement
  pipeline are byte-for-byte unchanged.
* **No Supabase schema change was performed.** No migration was added or edited,
  no SQL was executed, no database was contacted, and
  `DATABASE_ARCHITECTURE.md` is unchanged.
* The existing reaction system is unchanged; the POC is an additive sibling
  action, and it never converts a Unicode glyph into a "Premium" success.

## 12. Remaining work IF the POC is accepted

1. Owner runs the live check (§9) and records the outcome (especially whether
   the helper bot's Fragment capability allows the entity).
2. If accepted: decide the real reaction-emoji UX that builds on this proof
   (library picker vs. this reply-mode probe) and wire the selected identity
   into `reaction_service` — the POC deliberately does not.
3. If the helper bot cannot use custom-emoji entities, that Fragment
   ownership/cost decision is the owner's (ROADMAP §17/§34-D); the POC must not
   be extended with a Unicode fallback unless that mode is explicitly approved.
4. Optional cleanup when the capability is proven: remove the placeholder-glyph
   path if live alt text always exists.

## 13. Delivery metadata

| Item | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` (connected workspace remote; never hardcoded into code) |
| Branch | `main` |
| This pass's commit | the `feat:` commit carrying this POC (this report included) — it is the current tip of `origin/main` (`git log -1`) |
| Push | `git push origin main` — fast-forward only; no rebase, no force-push, no history rewrite |
| Working tree | clean |

## 14. Push verification

Verified against the live remote after the push with the four commands the task
names:

```
git fetch origin main
git rev-parse HEAD
git rev-parse origin/main
git ls-remote origin refs/heads/main
git status --short
```

`HEAD == origin/main == ls-remote refs/heads/main` at the POC commit, working
tree clean with no untracked task files.
