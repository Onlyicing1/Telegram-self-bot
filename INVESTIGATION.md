# Investigation — Alternative Premium Custom-Emoji Delivery Route: the Owner-Authored Self-Chat Message (2026-10-10)

Canonical, latest-only record. **This file replaces the previous
`INVESTIGATION.md` in full.** The inline entity-loss investigation it superseded —
the boundary trace and the attribution work (`98c2825`, `a54877b`, `9633c0b`,
`b463552`, `219c873`, `ac55042`) — remains readable in git history and is
summarised only as far as this task depends on it.

| Item | Value |
|---|---|
| Repository / branch | `Onlyicing1/Telegram-self-bot`, `main` |
| Revision inspected | `ac55042` — fetched and verified `= origin/main = git ls-remote` at task start (no newer commits existed) |
| Telethon | **pinned `telethon==1.34.0`**, layer 173 (`backend/requirements.txt:1`) |
| Question | Can a route **other than the helper-bot inline result** deliver a genuine `MessageEntityCustomEmoji` to the owner's Saved Messages, and can that be tested decisively? |
| Verdict | **The owner-authored `messages.sendMessage` route is documented, representable and testable, and it is the only route in which no bot participates — so no bot entitlement is checked. It has one structural difference from the inline route that must be stated rather than relaxed: a self-authored message CANNOT carry `via_bot_id`.** A controlled test for it is implemented and offline-verified (§6); whether Telegram **keeps** the entity on a self-authored Saved Messages message is **NOT yet established**, because this workspace holds no live session. |
| Production code changed | **None.** The route exists only as an isolated, opt-in test-suite experiment (`tests/test_premium_emoji_self_send_route.py`) plus the `live_telegram` marker registration in `tests/conftest.py`. Nothing is wired into the bot. |
| Live Telegram verification | **Not performed** (no `SESSION_STRING` in this workspace — `freebuff-env list` shows no credentials). The blocker and the exact run command are reported in §6/§9. |

---

## 1. Where this task starts

Three live attempts on the deployed revision produced the same result: the source
`MessageEntityCustomEmoji` was extracted and validated, the outbound text /
document id / UTF-16 geometry were validated, the helper bot's inline answer was
submitted with one custom-emoji entity, and the inspected result Telegram
returned had `entity_count=0` — text and document id matching, entity absent —
so the fail-closed gate refused the send (`INLINE_RESULT_ENTITY_MISSING`).
`messages.sendInlineBotResult` and the `via_bot_id` read-back were **never
reached**, and nothing here claims either is broken.

Already established by source and byte-level verification (previous task, not
repeated): the entity leaves this application intact inside a well-formed
`messages.setInlineBotResults` request; the pinned Telethon parses a stored
payload that keeps the entity; the sanitizer, the cache (`cache_time=0`) and the
client-side shapes are all excluded. The loss is inside Telegram's processing of
the bot's inline answer, and *which* server rule produces it is undocumented.

That is why this task changes the **route**, not the attempt.

## 2. The route investigated

**`messages.sendMessage` from the existing authenticated owner account with
`peer = inputPeerSelf` and `entities = [messageEntityCustomEmoji(...)]`** — a
message the owner authors themselves, in their own Saved Messages chat, with no
bot anywhere in the path.

Why this one:

* it is the **only** route in which the sender is the owner's own account *and*
  no bot supplies the entity, so the documented bot-side entitlement (Fragment
  username; Bot API 9.4 owner-Premium for direct bot sends) is not consulted;
* Telegram documents the non-Premium allowance for exactly this destination
  (§3), which is why Saved Messages is the one permitted target;
* it reuses the project's existing authenticated client factory, its existing
  custom-emoji lookup facade and its existing UTF-16 machinery — no new client,
  listener, loop, scheduler, executor or dependency;
* the artifact is inspectable: the sent message id is known, so the stored
  message can be read back by exact id and its entity verified.

Alternatives considered and **not** selected:

* **helper bot sends it directly** (`messages.sendMessage` as the bot,
  `backend/telegram_api/bridge.py`) — the closed POC's route; covered by the
  documented bot entitlement and explicitly out of the approved behaviour
  (ROADMAP §28/§34-D: no Fragment purchase, no alt-text fallback);
* **the inline route** — the route that fails today (§1);
* **forwarding the source message** — a documented user-account operation that
  would not re-validate the entity, but it delivers the *original* message
  (with forward metadata) rather than a controlled artifact with known
  geometry, and the repository forbids `forward_messages` in its Save pipeline
  (AGENTS.md §6). Recorded here as an option, deliberately not implemented.

## 3. Documented evidence (all read this session)

| Fact | Source | Status |
|---|---|---|
| "To send a message with one or more custom emojis, create and attach messageEntityCustomEmoji entities to a message." | <https://core.telegram.org/api/custom-emoji> | **CONFIRMED** — the documented construction for a user-authored message |
| "…the messageEntityCustomEmoji entity must wrap exactly one regular emoji (the one contained in documentAttributeCustomEmoji.alt) in the related text, otherwise the server will ignore it." | same page | **CONFIRMED** — the one documented *ignore* rule; the experiment satisfies it by construction (the glyph is the document's own `alt` when resolvable, otherwise the source span, recorded as `glyph_source`) |
| "Custom emoji documents will contain documentAttributeCustomEmoji … whether the emoji can be used by non-premium users (free)" | same page | **CONFIRMED** — per-document flag, recorded per run |
| "All users – Premium or not – can see any animated emoji. Everyone can also use all custom emoji for free in their Saved Messages chat to try them out – or to add extra flair to notes and reminders." | <https://telegram.org/blog/custom-emoji> | **CONFIRMED** — the documented basis for targeting Saved Messages, and the documented allowance for a non-Premium owner |
| `inputPeerSelf` — "Defines the current user." | <https://core.telegram.org/constructor/inputPeerSelf> | **CONFIRMED** — the peer that denotes the user's own chat (Saved Messages) |
| `message#95ef6f2b … via_bot_id:flags.11?long … entities:flags.7?Vector<MessageEntity>` | <https://core.telegram.org/constructor/message> | **CONFIRMED** — `via_bot_id` is an **optional** flag field (present only when a message was sent through a bot), and a stored message can carry `entities` back to the reader |
| Whether Telegram silently strips a custom-emoji entity from a **user-authored** message, and which account property (Premium, the document's `free` flag) would govern it | — | **NOT DOCUMENTED** — only a live read-back can decide it (§8) |

## 4. Pinned-Telethon evidence (established offline, in this repository)

| Fact | How it was verified |
|---|---|
| `telethon==1.34.0` `send_message(…, formatting_entities=…)` accepts `MessageEntityCustomEmoji` | signature inspection (`TelegramClient.send_message`), pinned by `test_the_pinned_telethon_forwards_formatting_entities_into_the_request` |
| That call forwards the entities into the real request | `inspect.getsource(TelegramClient.send_message)` → `functions.messages.SendMessageRequest(…, entities=formatting_entities, …)` — pinned by the same test |
| The request round-trips the entity through its own bytes | `BinaryReader(bytes(request)).tgread_object()` → `SendMessageRequest` with `InputPeerSelf`, the exact text and `MessageEntityCustomEmoji(doc, offset, length)` — `test_the_real_send_request_round_trips_the_entity` |
| Duplicate-send protection exists by construction | Telethon generates a fresh `random_id` per request (`SendMessageRequest.__init__`), pinned by `test_two_requests_never_share_a_random_id` |

**Explicitly a local fact, not a Telegram verdict:** serialization proves the
request *can* represent the entity; it says nothing about what Telegram stores.
That distinction is preserved in both the code comments and the reports.

## 5. What this route can and cannot satisfy

| Acceptance criterion (task §4) | How the route addresses it | Status |
|---|---|---|
| Target is the owner's Saved Messages | peer is `inputPeerSelf` **by construction** (the route can never address another chat); the stored message must additionally show `chat_id == owner_id` | implemented + offline-tested |
| Stored message contains a genuine `MessageEntityCustomEmoji` | the read-back scans the stored message's entities; a plain glyph is never accepted | implemented + offline-tested |
| Stored document id matches the source emoji | asserted | implemented + offline-tested |
| Offsets/lengths correct in UTF-16 units | the production geometry builder/validator is reused; asserted on the stored entity | implemented + offline-tested |
| Visible text and entity span consistent | text equality + exact offset/length/span equality | implemented + offline-tested |
| Inspected **after** Telegram accepted and stored it | the read-back is by the exact message id returned by the send; nothing is concluded pre-submission | implemented (live run pending) |
| **`via_bot_id` attribution verified independently** | **This route cannot produce it.** `via_bot_id` is the optional `flags.11` field of `message`, present only for messages sent through a bot; a self-authored message has none. The experiment therefore asserts its **absence** (any value fails closed as an anomaly) and reports the limitation instead of silently dropping the check | implemented + offline-tested |

**Stated consequence:** the inline route's `via_bot_id` criterion is a property
of the *inline* route, not of the artifact. A self-authored delivery path is a
**different artifact** and must be accepted or rejected on its own terms — it is
not a drop-in replacement for the inline route's acceptance criteria.

## 6. The controlled test (implemented; live run pending)

**Where it lives:** `tests/test_premium_emoji_self_send_route.py` — isolated in
the test suite, exactly as the task requires ("prefer an isolated test that
follows the existing test conventions"; "do not expose an unverified route to
normal production behaviour"). No production module changes.

**The exact operation and its side effects** (task §5.1–5.4, §5.8):

1. build the payload with the **production** builder/validator
   (`premium_emoji_inline_service.build_inline_payload` / `validate_inline_payload`)
   using this route's own identifiable prefix
   `LifeOS premium emoji self-send test: `;
2. discover a **known source message** carrying a genuine custom-emoji entity
   (bounded scan of the last 100 Saved Messages, read through the production
   `inspect_source_message`), or take a pinned document id from
   `LIFEOS_LIVE_PREMIUM_EMOJI_DOC_ID`;
3. resolve the glyph Telegram's documented rule requires — the document's own
   `alt` when resolvable, else the source span (`glyph_source` recorded);
4. send **exactly ONE** message: `send_message(inputPeerSelf, text,
   formatting_entities=[MessageEntityCustomEmoji(offset, length, document_id)])`;
5. read the stored message back **by its exact id** (`get_messages(peer, ids=…)`)
   and verify every acceptance criterion;
6. **no retry on any path** — a payload, send, read-back or verification failure
   is returned as an honest verdict, so one run can never leave more than the
   single message it attempted; the message is left in place as the evidence
   (identifiable by its prefix; delete it manually if desired).

**Bounds and isolation:** one message, two target calls (send + read-back) inside
the production `guarded_await` watchdog, one bounded history scan; the existing
client factory (`backend.bot.client.build_client`) and the existing custom-emoji
facade are reused; no new client, listener, update loop, scheduler, executor,
persistence or dependency; no Supabase/SQL; Saved Messages is the only
destination that can be addressed.

**Run it (it skips honestly everywhere else):**

```
LIFEOS_LIVE_PREMIUM_EMOJI_SELF_SEND=1 \
  API_ID=… API_HASH=… SESSION_STRING=… BOT_OWNER_ID=… \
  pytest tests/test_premium_emoji_self_send_route.py -m live_telegram -v -s
```

The explicit opt-in flag is required **in addition to** credentials: a live send
to the owner's account must never happen merely because the suite runs somewhere
a session is configured.

**Why this workspace cannot run it:** no `API_ID`/`API_HASH`/`SESSION_STRING`
exists here (`freebuff-env list` → no credentials). Per the task's own rule, the
blocker is reported rather than improvised around.

## 7. The `PeerChannel` entity-resolution warning

Source inspection bounds it:

* the **only** `PeerChannel` reference in the backend is `_peer_to_id` in
  `backend/telegram_api/_helpers.py` — and it has **no callers** anywhere in the
  backend;
* the premium-inline path never resolves a channel peer: it reads the owner's
  reply in **Saved Messages** (the destination gate refuses anything else) and
  its inline query peer is that same Saved Messages id;
* the only path that resolves the **panel** peer (which may well live in a
  channel/group) is the helper bot's `edit_message` call behind `_edit_inline`,
  whose failures are caught and logged as a bounded warning — a UI-editing
  concern on a *different* client and a *different* call path.

**Conclusion (source-established, not asserted about the live log):** the
warning belongs to the panel-edit path; it cannot change what Telegram stored
for the helper bot's inline answer, which is where the entity is lost. It is not
offered as an explanation for `entity_count=0`.

## 8. Falsifiers — what a live run decides

| Live result | Meaning |
|---|---|
| `entity_present=True`, `document_id_match=True`, `span_match=True`, `chat_id == owner_id`, `via_bot_id is None` | **The alternative route works**: a genuine custom-emoji entity survives a self-authored Saved Messages send. The next step is a product decision (an owner-authored delivery path or a one-shot tool), not a chat-routing change. |
| `entity_present=False` (`entity_missing`) | Telegram also strips a **user-authored** custom-emoji entity in Saved Messages — the artifact cannot be produced by this account at all, and the remaining documented lever is the account-level Premium entitlement, an owner decision. |
| `document_id_mismatch` / `span_mismatch` | Telegram stored an entity that is not the one sent (a real, reportable platform behaviour difference, not a harness bug — the harness is offline-proven). |
| `not_saved_messages` / an unexpected `via_bot_id` | the artifact is not what this route claims; fail closed and investigate before any product use. |

A live failure here is necessarily Telegram's answer, not the harness: the
sequence (identity → discovery → glyph → one send → verify) is itself covered
offline against a recording fake client (§4 of `IMPLEMENTATION_REPORT.md`).

## 9. Remaining uncertainty and the smallest next experiment

1. **Whether Telegram keeps the entity on a self-authored Saved Messages
   message — unknown, and the single remaining question.** The smallest
   experiment is exactly the one implemented: one opt-in live run of
   `tests/test_premium_emoji_self_send_route.py -m live_telegram`, read-back by
   exact id.
2. **Nothing was changed in production**, so no deployed behaviour changed and
   no route is exposed before it is proven.
3. **Attribution is inherently unavailable on this route** (§5) — any future
   product use must state that, not paper over it.
4. **The inline route's own blocker is unchanged and unexplained** (Telegram's
   undocumented drop of a bot-supplied inline entity); this task deliberately
   did not attempt it again.

## 10. Scope honoured

* No production file changed: no delivery route, gate, entity validation,
  UTF-16 geometry, Saved-Messages-only rule or security boundary was weakened or
  bypassed; no Unicode fallback, no retry, no duplicate sends, no new client /
  listener / loop / scheduler / executor / dependency, no Supabase or SQL
  (`DATABASE_ARCHITECTURE.md`, `ROADMAP.md` and `tests/test_stage13.py`
  untouched).
* The experiment is isolated in the test suite and inert by default: it is
  disabled without an explicit opt-in flag **and** credentials, and the suite is
  green with it skipped.
