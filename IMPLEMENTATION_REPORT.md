# IMPLEMENTATION_REPORT.md — Current Execution Report

**This file describes ONLY the current state of the repository after the last
executed task.** It replaces every previous report in full; earlier reports
(the Emoji & Reaction Phases 1–6, the canonical-database reconciliation, the
premium-emoji POC and its closure) are preserved in git history.

| Item | Value |
|---|---|
| Task | Implement the genuine Premium **custom-emoji-via-inline-bot** feature end to end |
| Project | LifeOS Telegram self-bot (`Onlyicing1/Telegram-self-bot`), branch `main` |
| Base revision | `b04cb41` (the via-bot feasibility investigation) |
| Status | **Implemented and automated-tested. Live Telegram verification: NOT performed.** |
| Supabase | **not touched** — no SQL executed, no schema change, no migration, no request |
| Supabase execution status | unchanged: the schema remains **manual-only**, applied by the project owner |

---

## 1. What was implemented

A real, user-accessible feature that sends a genuine
`MessageEntityCustomEmoji` through Telegram's **inline-bot** mechanism, so the
final message is sent by the **owner's own (non-Premium) account** with
Telegram's own `via_bot_id` attribution — not by the helper bot.

### 1.1 The user-facing entry point

`Menu` → **Emoji** → **✨ Send Premium Emoji** (`action:emoji_premium_inline`).

1. The bot sends a **selection message** to the owner's Saved Messages
   ("Reply to this message with the Premium Emoji you want to send through the
   inline bot. Only a reply to THIS message is accepted.") and arms the
   existing pending-input listener with that message id.
2. The owner **replies to that exact message** with the Premium emoji.
3. The flow reads **that** message, extracts the real entity, has the helper
   bot answer an inline query with the entity, sends the result as the owner,
   reads the exact stored message back and reports what Telegram actually
   stored.

The source is therefore **explicit and deterministic**: the owner's reply to
the recorded selection message id — never "the last media message", never an
unrelated earlier message, never a guess.

The old POC row is no longer the feature's interface: it moved below the
production entry and is relabelled `🧪 POC · helper-bot sent emoji`. It stays
registered because it remains the repository's only live evidence for the
*direct bot-send* mechanism and its 62 tests still pin it.

### 1.2 Data flow

```
reply (exact selection message, Saved Messages)
  └─ premium_emoji_inline_service.inspect_source_message
       entity-only: real MessageEntityCustomEmoji + usable document_id
                    + UTF-16 offset/length + the span text it covers
  └─ get_custom_emoji_documents(self_client, [document_id])
       Telegram's own alt, free, text_color (free/alt are new, additive)
  └─ build_inline_payload + validate_inline_payload
       text = "Premium emoji: " + glyph ; entity offset = utf16(prefix),
       length = utf16(glyph), real document_id ; span must cover exactly the
       glyph ; validated BEFORE submission, fails closed
  └─ helper bot: emoji._premium_inline_builder (registered for
       "premium_emoji_send:<document_id>:<glyph>")
       InputBotInlineMessageText(message=text, entities=[MessageEntityCustomEmoji])
  └─ inline_engine.query_results(self_client, chat_id=Saved Messages, query)
       getInlineBotResults  →  CHECKPOINT 2: does the STORED result still
                               carry the entity? (id/offset/length/span)
  └─ inline_engine.click_result(...)
       sendInlineBotResult  →  the OWNER's account sends; via_bot_id is
                               Telegram's, never set by us
  └─ self_client.get_messages(chat_id, ids=<exact id>)
       CHECKPOINT 3: entity present? document id / offset / length match?
                     span match? via_bot_id == helper bot?
  └─ classify_diagnosis → ONE honest outcome + the full evidence record
```

The same flow renders a report in the Glass UI panel (path, destination,
message id, expected vs stored geometry, attribution, owner Premium status,
document `free`/`text_color`, helper bot identity, and the honest sentence).

### 1.3 The outcomes (no success is claimed on a send alone)

`SOURCE_ENTITY_MISSING`, `UNSUPPORTED_DESTINATION`,
`OUTBOUND_PAYLOAD_INVALID`, `INLINE_UNAVAILABLE`, `INLINE_RESULT_REJECTED`,
`INLINE_RESULT_ENTITY_MISSING`, `INLINE_SEND_FAILED`, `READBACK_FAILED`,
`STORED_ENTITY_STRIPPED`, `STORED_ENTITY_MISMATCH`,
`STORED_ATTRIBUTION_MISSING`, `STORED_ENTITY_VERIFIED_RENDER_UNVERIFIED`.

Only the last one sets `verified=True`, and even then the report says
retention is not display — the visual render is the owner's confirmation, and
that requires the actual sender's Premium status to be observed as false,
which the record shows as `eligibility.owner_premium`.

### 1.4 Boundaries honoured

* Saved Messages is the **only** supported destination (the documented
  non-Premium allowance); anything else is refused up front — no arbitrary
  destination is claimed to work.
* No Unicode fallback, no sticker/visual approximation, no fabricated
  `via_bot_id`, no direct bot send as a substitute.
* No second client, update loop, scheduler, executor, lifecycle manager or
  helper framework: the self client and the existing helper bot are used as
  they are, and the inline query/send go through `backend.helper.inline_engine`
  — the same flow every Glass UI panel uses.
* No new dependency (Telethon 1.34.0 already exposes
  `InputBotInlineMessageText.entities`, `DocumentAttributeCustomEmoji.free` /
  `.text_color` and `User.premium`).
* The reaction pipeline, the replacement pipeline, the AI/runtime/helper
  layers, the POC service and `tests/test_stage13.py` are untouched.

---

## 2. Exact files changed

| File | Change |
|---|---|
| `backend/services/premium_emoji_inline_service.py` | **new** — the production pipeline: source inspection, payload build/validate, inline-result build, `query_results`/`click_result` drive, checkpoint 2 + 3 evidence records, `classify_diagnosis`, `outcome_summary` |
| `backend/bot/handlers/emoji.py` | the `emoji_premium_inline` action, the deterministic selection message, the reply-mode handler, `_premium_inline_builder` (registered inline builder), `_premium_inline_report`, the main-panel entry (first row) and the POC row relabelled |
| `backend/helper/inline_engine.py` | `inline_unavailable_reason()`, `query_results()`, `click_result()` — the two halves of `trigger`, exposed so the stored inline result can be inspected between them; `trigger` reimplemented on top of them with an **unchanged** public contract |
| `backend/telegram_api/custom_emoji.py` | `_serialize_document` now also reports `free` and `text_color` (additive) |
| `backend/telegram_api/_helpers.py` | `serialize_user` now also reports `premium` (additive) |
| `tests/test_premium_emoji_inline.py` | **new** — 50 offline tests for the pipeline and its failure modes |
| `tests/test_emoji_set_enumeration.py` | the two document-shape pins updated for the additive `free`/`text_color` keys + one new test that both flags survive |
| `IMPLEMENTATION_REPORT.md` | this report (replaced in full) |
| `INVESTIGATION.md` | replaced in full with the current canonical record (mechanisms, preserved POC evidence, official constraints, implementation status, gap status) |

No SQL, no migration, no `DATABASE_ARCHITECTURE.md` change, no `ROADMAP.md`
change, no dependency change, no configuration change.

---

## 3. Tests and validation actually executed

| Check | Command | Result |
|---|---|---|
| Syntax | `python -m py_compile` on every changed module | clean |
| Focused | `pytest -q tests/test_premium_emoji_inline.py` | **50 passed** |
| Focused group | `pytest -q tests/test_premium_emoji_inline.py tests/test_premium_emoji_probe.py tests/test_bridge_delivery.py tests/test_emoji_set_enumeration.py` | **163 passed** |
| Emoji regression | `pytest -q tests/test_emoji_ui.py tests/test_emoji_library_import.py tests/test_emoji_replacement_phase4.py` (with the above) | **226 passed** |
| Full suite | `pytest -q` | **5765 passed, 26 skipped, 1 failed** |
| Baseline control | the same full suite on a pristine `git worktree` at the same revision | **5714 passed, 26 skipped, 1 failed** |
| Whitespace | `git diff --check` | clean |
| Diff review | the complete diff inspected | only the files above |

**The one failure is pre-existing and unrelated.** It is in
`tests/test_runtime_diagnostics_classification.py`, which fails only under
full-suite load and with a *different* test each run: on this tree
`test_task_scheduler_wait_is_not_starvation`, on the pristine worktree
`test_a_genuinely_unchanged_bounded_task_is_still_detected`. The module passes
in isolation on both trees (11 passed) and no file this task changed is
imported by it. It is load/order-sensitive task-starvation timing, not a
regression, and it was not "fixed" by weakening it.

**Coverage of the required failure modes** (all in
`tests/test_premium_emoji_inline.py`): valid source entity; missing entity;
unusable document id; unresolvable span; non-custom entities ignored; UTF-16
offset correctness (including a divergent-alt case); exact alt-span
validation; payload validation per broken shape; inline-result construction
with the real entity; entity preservation through the stored-result parse;
missing entity in the stored result (send not attempted); accepted send with
the entity missing from the read-back; stored entity with a mismatched id;
stored entity with a wrong span; `via_bot_id` missing; `via_bot_id`
inconsistent; query exception; zero results; send exception; send with no
message; read-back exception; read-back with no message; helper unavailable;
unsupported destination; invalid owner; unresolvable glyph; owner Premium
observable/unknown; **no Unicode fallback ever**; summaries never claiming a
render; UI registration and the primary panel entry; the full reply-mode flow
(happy + stripped); `trigger` contract preserved; serializer backward
compatibility.

**What the tests do NOT prove:** they use fakes for every Telegram surface. A
mocked success is not evidence that a real Telegram send keeps the entity, and
no test claims otherwise.

---

## 4. Live Telegram behaviour

**Not exercised in this session.** This workspace has no Telegram account, no
session string and no helper bot connection; nothing was sent, and no message
was read back. The implementation records exactly the facts needed to interpret
the owner's first real run, and no live result is claimed, inferred or
fabricated anywhere in the code or in these reports.

Known Telegram-side uncertainty (unchanged by this task, documented in
`INVESTIGATION.md`): whether the server accepts a custom-emoji entity in an
**inline result**, whose entitlement it checks, and whether it keeps the entity
through the non-Premium user's `sendInlineBotResult`. The earlier live failure
(a bot's **direct** `messages.sendMessage`) does not answer any of these — it
tested a different mechanism (different sender, method, carrier and
destination).

---

## 5. Deployment state

* The code is deployable as-is: no schema step, no new dependency, no
  configuration, no migration. The feature becomes reachable on the owner's
  next deployment of `main` (the helper bot must be connected and have a public
  `@username`, which inline mode already requires for every panel).
* The feature is reachable **only** through the Glass UI entry point above; it
  cannot be triggered by a bare message and it sends nothing on its own.
* Delivery metadata (commit sha, `origin/main` verification) is recorded in §8
  after the push.

---

## 6. Remaining limitations and known risks

1. **Telegram's verdict is unknown** until the owner runs it on a real
   account. The code reports it truthfully in either direction.
2. **Saved Messages only.** Other destinations are refused by design because
   only the self chat has a documented non-Premium allowance.
3. **`free` is evidence, not proof.** The emoji's `free` flag is now reported
   per run, but it does not by itself predict whether an inline result keeps
   the entity.
4. **The helper bot must be connected and have a username.** Without one the
   feature reports `INLINE_UNAVAILABLE` with the same three-way diagnosis the
   panels use; it never falls back to another delivery method.
5. **Rendering is not verifiable by code.** `verified=True` means the entity is
   stored with the right geometry and attribution; the owner must look at the
   message.
6. **The pre-existing diagnostics flake** under full-suite load (§3) is
   reported, not silenced.

---

## 7. Exact owner test procedure (deployed revision)

1. Deploy `main` and make sure the helper bot is configured/connected (it is
   the same bot every panel already uses).
2. In Telegram, open **Saved Messages** and type `Menu`.
3. Tap **Emoji** → **✨ Send Premium Emoji**.
4. A message appears in Saved Messages: *"Reply to this message with the
   Premium Emoji you want to send through the inline bot. Only a reply to THIS
   message is accepted."*
5. **Reply to that message** with the Premium custom emoji (tap the emoji in
   your own Saved Messages and use Reply, or send any message that contains a
   genuine Premium custom emoji and reply to it).
6. Watch the panel: it is edited with the report.

**Expected on success:** the panel title becomes `Send Premium Emoji ✓`, the
report shows `Diagnosis STORED_ENTITY_VERIFIED_RENDER_UNVERIFIED`, `Path:
messages.sendInlineBotResult`, `Message: #<id>`, `Stored message: real
MessageEntityCustomEmoji … id match: yes · span match: yes`, `Attribution:
via_bot_id = <helper bot id> (matches the helper bot)`, `Owner Premium: no`,
and the emoji's `free` value. A message reading `Premium emoji: <emoji>` with a
"via @<helper bot>" line appears in Saved Messages. **Look at it**: only your
eyes can confirm the emoji renders as a custom emoji.

**Expected on failure (each is a real, actionable diagnosis):**

* `SOURCE_ENTITY_MISSING` — the reply carried no custom-emoji entity (or you
  replied with a plain Unicode emoji); nothing was sent.
* `UNSUPPORTED_DESTINATION` — the reply was not in Saved Messages.
* `INLINE_UNAVAILABLE` — the helper bot is not connected or has no username.
* `INLINE_RESULT_REJECTED` / `INLINE_RESULT_ENTITY_MISSING` — Telegram refused
  the bot's result or stored it without the entity; nothing was sent.
* `INLINE_SEND_FAILED` — the send itself was rejected (the error text is shown).
* `STORED_ENTITY_STRIPPED` — the result had the entity and Telegram accepted
  the send, but the stored message has none: the documented "the server
  ignores it" behaviour on this path.
* `STORED_ATTRIBUTION_MISSING` — the entity is stored but `via_bot_id` is
  absent/not the helper bot.
* `READBACK_FAILED` — the message was accepted but could not be read back.

**Evidence to send back:** the panel report text as it appears (it contains the
diagnosis, the message id, the expected and stored document id/offset/length,
the `via_bot_id`, your Premium status and the emoji's `free` flag), plus a
screenshot of the stored message in Saved Messages. If the failure is
`STORED_ENTITY_STRIPPED`, also say whether the emoji's `free` was `yes` — that
single fact distinguishes the per-emoji eligibility rule from a blanket
inline-path restriction.

---

## 8. Delivery metadata

| Item | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Branch | `main` |
| Base revision | `b04cb41` |
| Implementation commit | `feat(emoji): send the premium custom emoji through the inline bot` (see the metadata commit below for the exact sha) |
| Push verification | `git fetch origin main` → `git rev-parse HEAD` == `git rev-parse origin/main` == `git ls-remote origin refs/heads/main` |
| Supabase | not touched (no SQL, no schema, no migration, no request) |

The exact commit sha and the verified `origin/main` sha are recorded in the
follow-up metadata commit, per this repository's convention of never asserting
delivery state from memory.
