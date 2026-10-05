# Implementation Report

> **Scope:** this report describes the work performed in the current change
> set only. It is rewritten (not appended) each time; historical narrative
> lives in git history. Nothing here claims live-Telegram verification —
> see §5 for exactly what was and was not verified.

**Feature: Emoji & Reaction — Phase 0 (code slice) completed.**

**Next: Phase 0 completion requires the owner decisions §34-D (bridge
capability / Fragment username) and §34-E (mapping persistence shape);
after that, Phase 1 — Library & Import (ROADMAP §32).**

Phase 0 of the current-feature roadmap (`ROADMAP.md` §32) contains three
items: entity-aware serialization (§22), the §34-D decision, and the §34-E
decision. The two decisions belong to the repository owner and remain OPEN;
the implementable slice — the entity-aware serialization prerequisite plus
the minimal send-only Bot bridge needed by the later reconstruction phase —
is what this change set delivers.

---

## 1. What was implemented

### 1.1 Entity-aware serialization (`backend/telegram_api/_helpers.py`)

The documented ROADMAP §22 gap: `serialize_message` dropped entities
entirely, making any later emoji-span transformation impossible without a
representation to transform.

- `serialize_message` now emits an `entities` key: a list of plain dicts
  (`type`, `offset`, `length`, plus `url` / `document_id` / `user_id` /
  `language` payload where present). Offsets/lengths stay in **UTF-16 code
  units** exactly as Telegram sent them — the currency the transformer must
  operate in. Types serialize by their TL class name
  (`MessageEntityBold`, `MessageEntityCustomEmoji`, `MessageEntityTextUrl`, …).
- `utf16_length(text)` — text length in UTF-16 code units (emoji = 2).
- `utf16_offset(text, index)` / `utf16_index_at(text, offset)` — lossless
  conversions between Python character indices and UTF-16 offsets.
  `utf16_index_at` **fails closed**: a `ValueError` is raised for
  out-of-range offsets and for offsets that fall inside a surrogate pair
  (mid-emoji) — a corrupt entity is never silently clamped.
- `dict_entities_to_tl(client, entities)` — the inverse mapping: plain-dict
  entities become real Telethon `MessageEntity` objects for
  `send_message(formatting_entities=...)`. Simple offset/length types,
  `TextUrl`, `Pre`, `CustomEmoji` (document_id validated as an int) and
  `MentionName` (user resolved through the **target** client's entity cache,
  because the bridge bot resolves against its own session) are supported.
  Unknown types and missing payloads raise `TelegramAPIError` — an entity is
  never silently dropped.

### 1.2 Send-only Bot bridge (`backend/telegram_api/bridge.py`, new file)

The minimal delivery step ROADMAP §17 requires, built on the existing
optional helper bot (`backend/helper/client.py`) per §34-C's default:

- `send_reconstructed(client, chat_id, text, entities=None,
  reply_to_msg_id=None)` — sends through the helper bot to the SAME
  destination (peer resolved via the **self** client), passing rebuilt TL
  entities as `formatting_entities=` (verified present on Telethon 1.34.0's
  `send_message`; providing them bypasses re-parsing, which is what keeps
  the transformation entity-faithful) and `reply_to=` threading.
- `bridge_available()` / `bridge_bot_id()` — the loop-prevention sender
  surface (ROADMAP §24) for later phases.
- Send-only by construction: no event handlers, no update loop, no polling.
- Honesty rules: raises `TelegramAPIError` when the helper bot is not
  connected, refuses to send an empty message, fails BEFORE any send on
  unknown/invalid entities, wraps generic send failures as
  `TelegramAPIError` and timeouts as `TelegramTimeoutError` — consistent
  with the rest of `backend/telegram_api` and with the send-first
  reconstruction ordering (§15/§28), under which a failed send must leave
  the original message intact.
- Capability constraint carried, not hidden: bots can attach
  custom-emoji entities only with a Fragment-purchased additional username
  (Telegram Bot API constraint, ROADMAP §17/§28). This module sends the
  entities as-is and surfaces any server rejection; capability fallback
  (alt-text degradation) is a later-phase transformer/reconstructor
  decision (§34-D still OPEN).

## 2. Files changed

| File | Change |
|---|---|
| `backend/telegram_api/_helpers.py` | Added `entities` serialization to `serialize_message`; added `utf16_length` / `utf16_offset` / `utf16_index_at` / `dict_entities_to_tl` / `_resolve_input_user` / `_serialize_entity_list` |
| `backend/telegram_api/bridge.py` | NEW — send-only Bot bridge (`send_reconstructed`, `bridge_available`, `bridge_bot_id`) |
| `tests/test_bridge_delivery.py` | NEW — 22 focused tests (§3) |
| `ROADMAP.md` | Phase-0 slice status updates (§2, §5.4, §17, §22, §30, §32, §35, header) — no scope changes, no completed-status inflation |
| `IMPLEMENTATION_REPORT.md` | This rewrite |

No other file was touched: no handler registration, no dispatcher/executor
change, no schema/SQL, no `DATABASE_ARCHITECTURE.md`, no `AGENTS.md`.

## 3. Tests executed and actual results

All commands run with the project venv (`/home/daytona/codebase/.venv`),
`PYTHONPATH=.` from the repository root; exit statuses captured:

1. `pytest tests/test_bridge_delivery.py -q` → **22 passed** (0.42s).
   Coverage: UTF-16 helpers (incl. mid-surrogate `ValueError`,
   out-of-range rejection, round-trip), `serialize_message` entity output
   (incl. missing-attribute tolerance), `dict_entities_to_tl` round-trip
   (simple + custom-emoji + TextUrl + Pre), fail-closed unknown/missing
   payload, mention-name resolution through the target client
   (`assert_awaited_once_with(99)`), bridge same-destination delivery with
   entity rebuild and `reply_to` propagation, no-entity send,
   empty-message refusal, unavailable-bot error, unknown-entity
   fail-before-send, generic-failure and timeout normalization, and the
   `bridge_bot_id` / `bridge_available` accessors.
2. `pytest tests/test_telegram_chat_context.py
   tests/test_save_v2_telegram_sync.py -q` → **71 passed** (regression
   around the touched facade).
3. `pytest tests/ -q` → **5180 passed, 26 skipped** (121s; baseline at the
   merged remote state was 5158 passed — the delta is exactly the 22 new
   tests; no regressions).
4. `compileall backend/telegram_api/` → clean; `py_compile` on both changed
   modules → clean.

## 4. Limitations

- **No live Telegram verification.** The bridge is unit-tested against fake
  clients mimicking the consumed Telethon surface only. Same-destination
  delivery, `formatting_entities` acceptance, custom-emoji capability, and
  flood behavior are unverified live (ROADMAP §31 checklist remains open).
- The bridge has no flood/retry policy of its own beyond Telethon's
  existing `flood_sleep_threshold` on the helper client (ROADMAP §17 item
  left unchecked deliberately).
- `serialize_message` media/caption entity handling: captions' entities
  serialize with the media message but no media-aware reconstruction path
  exists yet (Phase 4 scope).
- No consumer of the new `entities` key exists yet — Phase 4's transformer
  is the intended consumer; the key is additive so existing callers are
  unaffected (regression suite confirms).

## 5. Intentionally not changed / deferred

- No §34 decision was resolved: D (bridge capability mode) and E
  (persistence shape) remain OPEN and are now the explicit Phase 0
  remainder; A/B/C/F/G/H/I keep their recorded default proposals.
- No library/category/mapping/transformer/reconstructor/reaction code
  (Phases 1–6).
- No handler wiring into `backend/bot/router.py` — the bridge is dormant
  until the Phase 4 reconstruction flow calls it.
- Supabase schema untouched; `DATABASE_ARCHITECTURE.md` untouched;
  `AGENTS.md` untouched (the bridge adds no new architectural authority).
- Architecture compliance: one supervisor, one dispatcher, one
  ToolRegistry/ToolExecutor, one update loop — unchanged; no semantic or
  regex routing introduced; the bridge is a leaf send utility, not an
  executor.

## 6. Git status

- Committed and pushed to `origin/main`; remote HEAD verified equal to
  local HEAD (see the commit message and ROADMAP footer for the phase
  context).
- Working tree clean after the push; no unrelated pre-existing work touched.
