# Investigation — Direct Owner-Authored Premium Custom-Emoji Send: Controlled Proof of Concept (2026-10-11)

Canonical, latest-only record. **This file replaces the previous
`INVESTIGATION.md` in full.** Everything it superseded — the inline route audit
and its three live attempts (`98c2825` → `a54877b` → `9633c0b` → `b463552` →
`39de9cc` → `219c873` → `ac55042` → `f22894e` → `9b34e38` → `2eaaf93`) and the
earlier probe boundary traces — stays readable in git history and is referenced
here only where this task's reasoning needs it.

| Item | Value |
|---|---|
| Repository / branch | `Onlyicing1/Telegram-self-bot`, `premium-emoji-attribution` (tracks `origin/main`) |
| Revision inspected | `2eaaf93` — freshly fetched and verified `= origin/main` at task start |
| Telethon | **pinned `telethon==1.34.0`** (`backend/requirements.txt:1`) |
| Question | Can the owner's **own account**, sending directly (`messages.sendMessage` carrying a real `MessageEntityCustomEmoji`) to its **own Saved Messages**, preserve a genuine custom-emoji entity — and can the application prove it from the **read-back message** rather than from the request it built? |
| Verdict | **Not yet answerable live.** The direct path existed only as a test-suite experiment; it now exists as an isolated, owner-triggered diagnostic (§5) whose full verification is implemented and offline-tested. The decisive Telegram send/read-back is **pending the live run** (§7) — this workspace holds no credentials and the deployment exposes no shell. |
| Production code changed | **Two files**: the new direct service (`backend/services/premium_emoji_direct_service.py`) and one diagnostic action in the Emoji panel (`backend/bot/handlers/emoji.py`). The production inline route and its fail-closed checks are **untouched (zero-line change)**. |
| Live Telegram verification | **Not performed in this workspace** — no `API_ID`/`API_HASH`/`SESSION_STRING`/`BOT_OWNER_ID` and no `.env`; `backend/config.load()` hard-fails on exactly those. **No Telegram request of any kind was made here** (§8). |

---

## 1. Where this task starts (production evidence, taken as given)

Three consecutive live attempts on the deployed revision used the existing
`messages.sendInlineBotResult` route and produced the same correlated sequence:

```
SOURCE_ENTITY_VALIDATED
PAYLOAD_VALIDATED valid=True
INLINE_RESULT_SUBMITTED submitted_ok=True
TELEGRAM_RESULT_INSPECTED ok=True entity_present=False entity_count=0
DIAGNOSIS diagnosis=INLINE_RESULT_ENTITY_MISSING sent=no
```

Three facts follow, and this task treats them as premises rather than
re-testing them:

* The **final inline send has never run** — the fail-closed gate refuses it
  before `messages.sendInlineBotResult` is invoked.
* **`via_bot_id` has never been observed** on any message this project produced.
* **Why Telegram's stored inline answer carries no entity is not established.**
  No entitlement, Premium, `free`-flag or `alt`-rule cause is claimed; the task
  forbids claiming Telegram universally strips custom-emoji entities.

The direct route is a **different mechanism** from the inline route: the owner's
own account authors the entity itself, no bot participates, and therefore no
`via_bot_id` can exist on it. It can isolate the one question the inline route
cannot answer — whether a direct owner-authored send preserves the entity — and
it deliberately does not try to satisfy the inline route's attribution
criterion (`§4`).

## 2. What already existed, and what was missing

The current branch's only direct-send code is
`tests/test_premium_emoji_self_send_route.py` (`f22894e`, `9b34e38`): a
**test-suite-only** experiment that pins the same payload geometry, the real
`SendMessageRequest` round-trip and a fail-closed stored-message verifier, and
ships an **opt-in live test** (`LIFEOS_LIVE_PREMIUM_EMOJI_SELF_SEND=1` plus
credentials) that builds its own client and scans Saved Messages for a source
emoji.

That module is not a runtime path. It cannot be triggered from the running bot,
it deliberately exposes no production surface, and its live harness requires
credentials in the environment it runs in. What this task asked for — and what
was missing — is exactly:

1. an **owner-triggered diagnostic path in the live runtime** that uses the
   **existing configured self-client** (never a second client, never a copied
   session);
2. one controlled send of a genuine `MessageEntityCustomEmoji` **directly to the
   owner's Saved Messages**, restricted to that destination;
3. **read-back verification of the exact sent message**, by id, that decides the
   question from Telegram's own returned message;
4. all of it isolated from the production inline route, with no retry, no
   Unicode fallback, and fail-closed behaviour everywhere else.

A previous, **never-pushed** attempt exists on the local `main` branch
(`6298b99`) with a same-shaped implementation. It was inspected as
"existing direct-send test code": its design was reviewed and its geometry,
verification and UI concepts were kept, while this task's implementation was
written against the current branch with corrections — most importantly, the new
service **does not touch `premium_emoji_inline_service.py` at all**, and the
stored message's own chat is now part of the read-back verdict. Nothing from
that unpushed attempt is treated as verified; the current branch is the
authority.

## 3. The route, and its documented basis

```
the owner's real custom-emoji entity (document id + UTF-16 geometry)
  → the document's own ``alt`` text (Telegram's documented rule)
    → ONE plainly labelled message to the owner's own Saved Messages,
      sent by the owner's own client (``messages.sendMessage``,
      ``formatting_entities=[MessageEntityCustomEmoji(...)]``)
        → the EXACT stored message fetched back by id
          → entity preservation and bot attribution judged SEPARATELY
```

* **Entity construction** — "To send a message with one or more custom emojis,
  create and attach `messageEntityCustomEmoji` entities to a message."
  (<https://core.telegram.org/api/custom-emoji>).
* **The `alt` rule** — "the `messageEntityCustomEmoji` entity must wrap exactly
  one regular emoji (the one contained in `documentAttributeCustomEmoji.alt`)
  in the related text, otherwise the server will ignore it." (same page). The
  builder resolves the glyph from the document's own `alt` when the lookup
  succeeds, records the fallback (`glyph_source`), and sends nothing when
  neither the `alt` nor the source span is usable.
* **Saved Messages allowance** — "Everyone can also use all custom emoji for
  free in their Saved Messages chat" (<https://telegram.org/blog/custom-emoji>).
  This is also why the destination is restricted: the test refuses any
  destination that is not the owner's own account
  (`UNSUPPORTED_DESTINATION`), and the read-back records the stored message's
  own chat (`destination_match`).
* **Pinned Telethon 1.34.0** — `send_message(..., formatting_entities=[...])`
  forwards the entities into `messages.SendMessageRequest`; the rebuilt TL
  entity is checked against the validated payload (exactly one
  `MessageEntityCustomEmoji`, same document id, same offset/length) before the
  send, and the request generator assigns a per-request `random_id`, so two
  sends can never collapse into one server-side duplicate.

## 4. What this route can and cannot satisfy

| Criterion | Direct route | Inline route (production) |
|---|---|---|
| Stored message contains a genuine `MessageEntityCustomEmoji` | **this is the open question** — the live run decides it | blocked before the send (`INLINE_RESULT_ENTITY_MISSING`) |
| Document id + UTF-16 geometry match the sent entity | verified from the read-back by id | verified from the read-back by id (unchanged) |
| Destination is the owner's Saved Messages | enforced pre-send **and** checked on the stored message's own chat | enforced as before (`UNSUPPORTED_DESTINATION`) |
| `via_bot_id` attribution | **impossible by construction** — recorded as not applicable, and an unexpected value is flagged; never claimed as satisfied | required (`via_bot_match`), fail-closed; **unchanged, not weakened** |
| Entity preservation vs. attribution | judged as two separate fields (`entity_preserved` vs `attribution`) | judged as separate verdicts (unchanged) |

The task explicitly states this path deliberately bypasses the helper bot and
must not be required to produce `via_bot_id`; the inline route's attribution
checks are not touched by this task (the inline service's diff is empty).

## 5. The controlled path implemented (what exists now)

* **Trigger** — `Menu` → Emoji → **"🧪 Live test · direct self-chat"**
  (action `emoji_premium_direct`, owner-only like every Glass UI action). The
  action sends one selection message to Saved Messages and arms reply mode on
  exactly that message (`set_pending`, `extra = selection message id`).
* **Source resolution** — the owner replies to **that exact message** with the
  Premium emoji. The reply is accepted only when it targets the recorded
  selection id in Saved Messages; `inspect_source_message` extracts the entity
  (`document_id` + UTF-16 span) or fails closed with a precise diagnosis
  (`SOURCE_ENTITY_MISSING`) and sends nothing.
* **Pre-send validation chain** — owner id → destination (`chat_id == owner_id`,
  Saved Messages only) → usable document id → glyph resolved from the
  document's `alt` (fallback: source span) → payload built by the production
  builder/validator → real TL entity rebuilt and matched against the payload.
  Any failure sends nothing.
* **The send** — exactly **one** `messages.sendMessage` by the existing self
  client, carrying the rebuilt `MessageEntityCustomEmoji`; no retry loop, no
  recurring sender, no second client, no helper-bot inline query or click.
* **The read-back** — the exact returned message id is fetched back through the
  existing bounded wrapper (`get_messages(chat_id, ids=message_id)`); the
  verdict is computed from that stored message: message text, stored chat,
  entity presence, document id, UTF-16 offset/length/span, `via_bot_id`.
* **Diagnoses** — `STORED_ENTITY_VERIFIED_RENDER_UNVERIFIED` (the only positive
  verdict; still never a render claim), `STORED_ENTITY_STRIPPED`,
  `STORED_ENTITY_MISMATCH`, `STORED_DESTINATION_MISMATCH`,
  `STORED_UNEXPECTED_ATTRIBUTION`, `READBACK_FAILED`, `DIRECT_SEND_FAILED`
  (records the exact error class), plus the shared pre-send verdicts.
* **Isolation** — nothing else in the project calls the direct service; no
  caller falls back to it; no database, no AI, no scheduler, no listener;
  logs are bounded stage lines (`[PREMIUM_DIRECT] …`) containing ids, offsets,
  lengths, booleans and bounded exception text only.

## 6. The verification contract (what the read-back proves — and what it cannot)

The verdict is computed **only** from the message Telegram returns for the
exact sent id. A successful API call, an outgoing request object or a locally
constructed entity are **not** success; a visible Unicode emoji is never the
entity; a message that was not read back proves nothing about the entity.

What only Telegram can answer: whether the server **keeps** the entity on a
direct owner-authored send to Saved Messages. Every offline test in this
repository uses fakes at the Telegram boundary and claims no server behaviour
(§8).

## 7. Falsifiers — what the live run decides

| Live observation (panel / log `DIAGNOSIS`) | What it means |
|---|---|
| `STORED_ENTITY_VERIFIED_RENDER_UNVERIFIED` | Telegram kept the genuine entity on the owner's own message to Saved Messages — the direct path **works** for the artifact; whether the viewing client renders it is still for the owner to confirm visually |
| `STORED_ENTITY_STRIPPED` | the send was accepted but the stored message carries no entity — the direct path does **not** preserve it |
| `STORED_ENTITY_MISMATCH` | a stored entity exists but its document id or UTF-16 span differs from what was sent |
| `STORED_UNEXPECTED_ATTRIBUTION` | the fetched message carries `via_bot_id` — impossible for a direct owner-authored send, so the artifact is not the one this route created |
| `STORED_DESTINATION_MISMATCH` | the fetched message's own chat is not the owner's chat — not the artifact (defensive; the fetch is already scoped to the destination peer) |
| `DIRECT_SEND_FAILED` | Telegram rejected the send itself — record the exact error class; do **not** claim the route is unsupported |
| `READBACK_FAILED` | the send happened but the message could not be read back — nothing about the entity is proven |

The task's rule is honoured either way: the direct path is never declared
unsupported unless the observed evidence justifies it, and the production
inline route is never silently replaced.

## 8. Live verification: not performed, and why

This workspace contains **no** Telegram credentials and no `.env`;
`backend/config.load()` hard-fails without `API_ID`, `API_HASH`,
`SESSION_STRING`, `BOT_OWNER_ID`. Render Free offers no shell. Therefore:

* **no Telegram request of any kind was made in this workspace** — the live
  send/read-back has **not** been run;
* all test evidence in this task is **offline** (fakes at the Telegram
  boundary); no offline result is called a live result;
* the live run must be triggered from the running deployment through the
  owner-only action described in §5 — the code path is exactly what the task
  asked for: no copied credentials, no second client, no new update loop.

## 9. Scope honoured

* No second Telegram client, session, listener, update loop, scheduler,
  executor or dependency; the existing configured self-client is used.
* The production inline route (`premium_emoji_inline_service.py`), its
  fail-closed checks, its attribution requirement and its panel behaviour are
  **byte-for-byte unchanged**.
* No Supabase, no SQL, no schema/migration, no `DATABASE_ARCHITECTURE.md`
  change; no Library/import, AI, or unrelated code change.
* One message per run, no retry loop, no recurring sender, no Unicode
  fallback, no fabricated entity, no arbitrary destination.
* Logs and the panel report carry no session strings, credentials, tokens or
  private message contents — bounded ids, offsets, lengths, booleans and
  exception classes only.
