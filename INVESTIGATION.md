# Investigation — Premium Custom Emoji Inline Flow: Earliest Confirmed Failure (2026-10-13)

Canonical, latest-only record. **This file replaces every previous
`INVESTIGATION.md` in full.** The earlier direct-send POC report and the inline
route audits remain readable in git history; they are referenced here only where
this diagnosis needs them.

| Item | Value |
|---|---|
| Repository / branch | `Onlyicing1/Telegram-self-bot`, `main` (tracks `origin/main`) |
| Revision inspected | `34d7dc3b24d39beccc91368c66cabf2db34f5b6f` — freshly fetched and verified `= origin/main` at task start |
| Telethon | **pinned `telethon==1.34.0`** (`backend/requirements.txt:1`; installed copy inspected) |
| Question | Where does the Premium custom-emoji inline flow actually fail, and is it a defect in this application or a Telegram-side restriction? |
| Verdict | **Confirmed**: the failure is **Telegram-side**, and it is the **documented bot custom-emoji entitlement restriction**. The helper bot authors the inline result; because the helper bot has no Fragment-purchased additional username and does not satisfy the bot-owner-Premium allowance, Telegram stores the bot's inline result with the custom-emoji entity **removed** (`entity_count=0`, text otherwise intact), so the fail-closed pre-send gate refuses. This is **not** a send-path defect in this application. |
| What changed | One **diagnosis-level** refinement: when the helper bot's own registered answer **provably** carried this emoji's entity (recorded, no error, matching `document_id`) and Telegram still stored none, the outcome is now the honest, documented **`INLINE_BOT_NOT_ENTITLED`** instead of the neutral `INLINE_RESULT_ENTITY_MISSING`. No validation, gate, send or read-back behaviour was weakened. |
| Live Telegram verification | **Not performed in this workspace** — no `API_ID`/`API_HASH`/`SESSION_STRING`/`BOT_OWNER_ID` and no `.env`; `backend/config.load()` hard-fails without them and Render Free exposes no shell. **No Telegram request of any kind was made here.** |

---

## 1. Where this task starts — the actual current state

The inline flow's three live attempts (`98c2825` → … → `2eaaf93`) and the later
direct-send test (`34d7dc3`) are the historical record. Two log labels appear in
that history and must **not** be treated as interchangeable:

* `[PREMIUM_INLINE] INLINE_RESULT_INSPECTED entity_present=False reason=no_send_message`
  — this came from the **wrapper bug** (`e13deee`): checkpoint 2 read the wrong
  layer, `getattr(results[0], "send_message", None)` on the Telethon
  `custom.InlineResult` **wrapper**, which has no `send_message` (only
  `.result.send_message`). That bug is **fixed** in the current source
  (`_inspect_inline_result` reads `.result.send_message`); it is **not** the
  current cause.
* `DIAGNOSIS diagnosis=INLINE_RESULT_ENTITY_MISSING sent=no` — the current-code
  verdict. After the wrapper fix the stored result was inspected **successfully**
  (`ok=True`) but carried `entity_present=False entity_count=0` while the text
  still matched, i.e. Telegram stored the result **without** the entity.

The current source is authoritative: `_inspect_inline_result` resolves the raw
`BotInlineResult`/`BotInlineMediaResult` off the wrapper, finds a real
`BotInlineMessageText` payload (so `ok=True`, not `no_send_message`), scans it for
a `MessageEntityCustomEmoji`, and — finding none — classifies the entity-loss
outcome. The **send has never run** on this route: the gate refuses before
`messages.sendInlineBotResult`, and `via_bot_id` has never been observed on any
message this project produced.

## 2. The decisive facts (source- and document-supported)

1. **This application's side is proven correct.** The helper bot's registered
   answer builds a genuine `InputBotInlineResult` whose
   `InputBotInlineMessageText` carries a real `MessageEntityCustomEmoji`
   (`document_id`, UTF-16 offset/length). This is pinned against the real
   Telethon classes and the **wire bytes**:
   `BinaryReader(bytes(request)).tgread_object()` round-trips the entity inside
   the exact `messages.setInlineBotResults` request the bot sends
   (`tests/test_premium_emoji_inline.py::test_the_answer_carries_the_entity_into_the_set_inline_bot_results_bytes`).
   The response parser is not the loss either: a stored `BotInlineResult` parsed
   from bytes keeps its entity
   (`…::test_a_stored_result_parsed_from_response_bytes_keeps_the_entity`).
2. **Telegram is the side that drops it.** The bot's own submission is recorded
   per attempt (`inline_engine.record_inline_answer` /
   `last_inline_answer`); the live evidence pairs a **recorded, error-free,
   identity-matching** submission with a stored result that has **no** entity.
3. **The drop is documented, not mysterious.** Telegram's Bot API states:
   *"Custom emoji entities can only be used by bots that purchased additional
   usernames on Fragment or in the messages directly sent by the bot to private,
   group and supergroup chats if the owner of the bot has a Telegram Premium
   subscription."*
   (<https://core.telegram.org/bots/api>). The helper bot authors the inline
   result and has no Fragment-purchased additional username, and an inline result
   delivered by the user (stamped `via_bot_id`) is not a message "directly sent by
   the bot". Under this rule Telegram ignores the entity, which is exactly the
   observed `entity_count=0` with intact text.

## 3. Confirmed root cause vs. hypotheses

| Claim | Status |
|---|---|
| The **inline result the helper bot authors** loses its custom-emoji entity **inside Telegram**, while the text survives | **CONFIRMED** (live `entity_present=False entity_count=0` + recorded bot submission; parser excluded) |
| The mechanism is the **documented bot custom-emoji entitlement restriction** (Fragment username / bot-owner Premium) | **CONFIRMED as the documented rule** that governs this case; the exact Telegram-internal code path is not observable, so this is stated as the documented cause, not an inferred internal trace |
| The earlier `reason=no_send_message` label still reflects the current failure | **REFUTED** — that was the pre-fix wrapper bug; current inspection succeeds (`ok=True`) |
| The current failure is a defect in this application's send/validation path | **REFUTED** — the send never runs (gate refuses first); the payload, entity, UTF-16 geometry and request bytes are all correct offline |
| The direct owner-authored route (`premium_emoji_direct_service`) | **Separate mechanism**, not required to satisfy `via_bot_id`; still awaiting its own live run — out of scope for this diagnosis |

## 4. The smallest evidence-based change made

The code already attributed a proven drop to Telegram in
`INLINE_RESULT_ENTITY_MISSING`/`_entity_missing_detail`. The remaining honest gap
was that `classify_diagnosis` reported the same neutral constant for a **proven,
correlated** drop as for an **unproven** one. The change adds the documented,
machine-readable **`INLINE_BOT_NOT_ENTITLED`** verdict, emitted **only** when the
bot's own answer provably carried this emoji:

```
inline_result.ok == True
  and inline_result.entity_present == False
  and _answer_proves_the_bot_submitted_this_emoji(answer, expected_document_id)
      => INLINE_BOT_NOT_ENTITLED        # documented entitlement restriction
  otherwise
      => INLINE_RESULT_ENTITY_MISSING   # neutral; cause unproven
```

This is a **diagnosis refinement, not a bypass**: it does not send anything, does
not weaken a check, and changes no send/read-back behaviour. It removes the
misleading "entity missing" label only where the evidence proves the entity was
submitted and lost inside Telegram.

## 5. Why no send-path fix is applicable

A genuine Telegram restriction cannot be fixed in application code without either
(a) purchasing a Fragment username for the helper bot — explicitly a non-goal and
a cost/ownership decision outside this task (`ROADMAP.md` §17, §28) — or (b)
authoring the entity from the **owner's own account** (the direct route), which is
a **different** delivery mechanism that by construction cannot carry `via_bot_id`.
The inline route's own acceptance criterion (`via_bot_id` = helper bot) therefore
cannot be met while the bot lacks entitlement. Reporting that precisely is the
correct, fail-closed outcome.

## 6. Files changed

| File | Change |
|---|---|
| `backend/services/premium_emoji_inline_service.py` | Added `INLINE_BOT_NOT_ENTITLED`; added `_answer_proves_the_bot_submitted_this_emoji`; `classify_diagnosis` emits the new verdict only for a proven correlated drop; `expected_document_id` recorded in the inline-result evidence; `outcome_summary` and `_entity_missing_detail` name the documented entitlement rule. No send/gate/read-back behaviour changed. |
| `tests/test_premium_emoji_inline.py` | Updated the proven-drop test to expect `INLINE_BOT_NOT_ENTITLED`; added `test_the_entitlement_verdict_needs_the_bot_own_correlated_submission`; added the new label to the panel-leak guard; refreshed the header contract. |

## 7. Tests actually executed

| Check | Command | Result |
|---|---|---|
| Compile | `python -m compileall -q backend/services/premium_emoji_inline_service.py tests/test_premium_emoji_inline.py` | **COMPILE OK**, exit 0 |
| Focused | `python -m pytest tests/test_premium_emoji_inline.py -q` | **85 passed**, exit 0 |
| Emoji suites | `python -m pytest tests/test_premium_emoji_inline.py tests/test_premium_emoji_direct.py tests/test_premium_emoji_probe.py tests/test_premium_emoji_self_send_route.py tests/test_emoji_ui.py -q` | **256 passed, 1 skipped**, exit 0 |
| Full suite | `python -m pytest tests -q` | **5895 passed, 27 skipped**, exit 0 (baseline `34d7dc3`: 5894 passed / 27 skipped → exactly **+1** net new test, 0 regressions) |
| Whitespace | `git diff --check` | exit 0 |

## 8. Live verification — not performed, and why

This workspace has no Telegram credentials and no `.env`;
`backend/config.load()` hard-fails on `API_ID`/`API_HASH`/`SESSION_STRING`/
`BOT_OWNER_ID`, and the Render deployment exposes no shell. Therefore **no
Telegram request was made here**, no offline result is called a live result, and
end-to-end acceptance is **not** claimed.

## 9. Scope honoured

* No second Telegram client, session, listener, update loop, scheduler,
  executor, or dependency.
* All existing security requirements preserved: owner-only self-client, the
  existing inline click/send mechanism, the Saved-Messages-only restriction, real
  `MessageEntityCustomEmoji` only (no fabricated entity, no Unicode fallback),
  `document_id` and UTF-16 offset/length validation, `via_bot_id` verification on
  the read-back, fail-closed on every empty/malformed/unsupported input, and no
  bypass of validation.
* No Supabase, no SQL, no schema/migration, no `DATABASE_ARCHITECTURE.md`
  change; no AI, scheduler or unrelated code change.
