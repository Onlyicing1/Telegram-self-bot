# Implementation Report

> **Scope:** this report describes the repository AFTER the current change
> set only. It is rewritten (not appended) each time; historical narrative
> lives in git history. Nothing here claims live-Telegram or live-Supabase
> verification — see §7 for exactly what was and was not verified.

**Feature: Emoji & Reaction — Phase 4 COMPLETE (Reconstruction).**

**Current phase: Phase 4 — Reconstruction IMPLEMENTED + TESTED (offline
only).**
**Next: Phase 5 — Custom Category (ROADMAP §26) / Phase 6 — Reactions (§27) /
Phase 7 — Hardening, plus the owner's live-validation checklist (§31).
§34-D (premium-visual delivery mode) stays OPEN and was NOT resolved; §34-B,
§34-C and §34-G were implemented as their documented default proposals with
owner confirmation still pending; §34-A and §34-E remain as Phase 3 / Phase 2
left them.**

This change set delivers ONLY Phase 4 on top of the Phase 0–3 state: the
deterministic entity-safe transformer, the reconstruction service (resolution
→ transform → bridge delivery → delete-original), the single outgoing handler
wired through the existing router, structural loop prevention, and focused
tests. Custom composition, reactions, AI tools, media/caption reconstruction,
new schedulers/loops/executors, new tables and new env vars are explicitly
NOT part of it.

---

## 1. Objective

Give the owner's own outgoing Telegram messages the premium-emoji form their
active category maps them to, without an AI in the loop, without a second
update loop, and without ever putting the original message at risk:

```
owner-authored outgoing message (existing self-client update path)
  → Phase 3 resolve_effective_category(owner, chat)      [the ONLY authority]
  → mapping table (reference rows → live library entries)
  → entity-safe transformation (only exact mapping keys change)
  → bridge delivery through the existing helper-bot bridge
  → delete the original — only after the new message exists
  → structural loop prevention (no re-entry)
```

## 2. What was implemented

### 2.1 Transformer (`backend/services/emoji_transformer.py`, NEW)

Pure, Telegram-free, AI-free. Input: message text, the serialized entity dicts
(`serialize_message` shape, UTF-16 units) and the resolved mapping table.
Output: `{ok, changed, text, entities, error}`.

- **Structural eligibility only** — a span is replaced because the owner's
  mapping table names it exactly. No regex, no keyword routing, no
  natural-language inference, no chat history, no AI, no Telegram call.
- **Emoji-only invariant (§16)** — text outside replaced spans is copied
  verbatim; unmapped emoji and ordinary text are byte-identical; attachment
  is one `MessageEntityCustomEmoji` per replaced span, covering the library
  entry's own `alt_text`.
- **Multi-emoji (§20)** — all mapped spans change in one pass; spans are
  disjoint by construction (longest key wins at each position, scan continues
  after it), the same key maps consistently, and `MAX_REPLACEMENTS` (200)
  fails the whole transformation closed instead of rewriting an unbounded
  message.
- **Entity safety (§22)** — entity char ranges are resolved through the Phase
  0 UTF-16 helpers; an entity that fully covers a replaced span keeps
  covering it (recomputed in UTF-16 units by the span delta), an entity
  entirely outside is rebased, and an entity that PARTIALLY overlaps a
  replaced span fails the whole transformation closed. Existing
  `MessageEntityCustomEmoji` spans are never rewritten (matches inside them
  are skipped).
- **Fail closed** — unknown/unsupported entity type, unresolvable or
  mid-surrogate offsets, partial overlap, empty text, or an exceeded
  replacement bound all return `ok=False` with an error code; the caller then
  leaves the message untouched. Unusable mapping entries (missing library
  entry, blank alt text, invalid document id) are dropped, so their emoji
  stays untouched rather than being replaced by fabricated content.
- The caller's text and entity dicts are never mutated.

### 2.2 Reconstruction service (`backend/services/emoji_replacement_service.py`, NEW)

`process_outgoing_message(owner_id, client, message, via_bot_id=None)` — the
single deterministic pipeline. It returns an honest outcome dict
(`status`, `replaced`, `sent_message_id`, `deleted`, `error`) and never
fabricates a success.

- **Resolution boundary** — `emoji_state_service.resolve_effective_category`
  is the ONLY source of the category; the service never re-implements the
  §13/§14 order and holds no toggle/category logic of its own.
- **Mapping table** — `list_emoji_mappings` for the effective category
  (bounded by `MAX_MAPPINGS`); an incomplete listing fails the whole message
  closed; every row is resolved to its live library entry and a row whose
  entry no longer exists (or has no alt text) is dropped.
- **Send-first ordering (§15 / §34-B default)** — the transformed message is
  delivered through `telegram_api.bridge.send_reconstructed` (the existing
  send-only helper-bot bridge) to the SAME chat, carrying the original's
  reply target when present; the original is deleted through the existing
  `telegram_api.messages.delete_messages` facade only AFTER that succeeded.
- **Honest outcomes** — `replaced` (deleted), `replaced_undeleted` (delivered
  but the delete failed — worst case an honest duplicate, never a loss),
  `failed` (delivery rejected — original untouched, exactly one attempt, no
  alt-text retry), plus `skipped_*` for every no-op path (invalid input, not
  owner-authored, bridge origin, inline-bot origin, duplicate, pending panel
  input, media, empty text, no effective category, no usable mappings, no
  mapped emoji, unsafe reconstruction, bridge unavailable).
- **Media (§23)** — a message with media is left untouched (fail closed); the
  cross-account media re-send mechanism remains an open investigation and was
  not guessed at.
- **No §34-D resolution** — custom-emoji entitlement is not probed and no
  degradation content is fabricated; a capability rejection is an honest
  failure with the original intact.
- **Loop prevention (§24)** — outgoing-only processing; bridge-bot author id
  suppression (checked first); inline-bot-origin (`via_bot_id`, i.e. the Glass
  UI panel machinery) exclusion; pending panel input left to the existing
  input machinery; two bounded in-memory registries (`GUARD_MAX` = 512,
  `GUARD_TTL_S` = 900) keyed by `(chat_id, message_id)` recording the messages
  this pipeline produced and the originals it already reconstructed. No
  visible marker, no heuristic, no second listener, no durable loop state.

### 2.3 Handler + router wiring

- `backend/bot/handlers/emoji_replacement.py` (NEW) registers exactly ONE
  `events.NewMessage(outgoing=True)` listener on the self client it is given,
  gates on `is_owner`, serializes the message with the real
  `serialize_message`, passes `via_bot_id` through, swallows unexpected
  service errors and re-raises `CancelledError`. Nothing is ever reported
  back into the chat (no fake success, no spam).
- `backend/bot/router.py` registers it LAST in the existing handler list, so
  every pre-existing handler keeps its first claim on the owner's message
  before the reconstruction step runs. No second client, no second update
  loop, no scheduler, no executor, no polling.

### 2.4 Entity support set (`backend/telegram_api/_helpers.py`, extended)

`SUPPORTED_ENTITY_TYPES` is now the single authoritative set of entity types
the dict representation can carry (simple types + TextUrl, Pre, CustomEmoji,
MentionName); `dict_entities_to_tl` refuses anything outside it. The
transformer checks against the same set, so "supported" has one definition.
No behavior change for existing callers (same error message).

### 2.5 Phase 3 boundary re-pinned (`tests/test_emoji_state_phase3.py`)

The Phase 3 suite's phase-boundary test is no longer "Phase 4 does not exist";
it now pins that the Phase 3 surface itself must stay free of execution logic
(no transformation, reconstruction, delivery, deletion, or Phase 4 imports)
while Phase 4 lives in separate modules. Assertions were strengthened, not
weakened. No other Phase 0–3 test was modified.

## 3. Exact files changed

| File | Change |
|---|---|
| `backend/services/emoji_transformer.py` | NEW — deterministic, pure, entity-safe transformer |
| `backend/services/emoji_replacement_service.py` | NEW — resolution → mapping table → transform → bridge delivery → delete-original, honest outcomes, loop prevention |
| `backend/bot/handlers/emoji_replacement.py` | NEW — the single outgoing handler |
| `backend/bot/router.py` | Registers the replacement handler LAST on the existing update path |
| `backend/telegram_api/_helpers.py` | NEW `SUPPORTED_ENTITY_TYPES` (single source of truth) + the rebuild refusal now consults it |
| `tests/test_emoji_replacement_phase4.py` | NEW — 73 focused tests |
| `tests/test_emoji_state_phase3.py` | Phase-boundary test re-pinned to the post-Phase-4 architecture (strengthened) |
| `ROADMAP.md` | Current-state updates (§1–§3, §6, §7, §15–§24, §28–§35) |
| `IMPLEMENTATION_REPORT.md` | This rewrite |

**Intentionally untouched:** every AI module (`backend/ai/**`), the tool
registry/executor/provider/dispatcher, `backend/runtime/**` (no supervisor,
scheduler or executor change), `backend/db/client.py` (no new table, no new
function), `backend/services/emoji_state_service.py` and
`backend/services/emoji_category_service.py` (Phase 3/2 behavior preserved),
`backend/services/emoji_library_service.py`, `backend/bot/handlers/emoji.py`
(Glass UI unchanged), `backend/telegram_api/bridge.py` (used as-is), schema/
SQL files, `DATABASE_ARCHITECTURE.md`, `AGENTS.md`, `config.py`, requirement
files, the frontend.

## 4. Tests added

`tests/test_emoji_replacement_phase4.py` — **73 tests**, covering:

- **Transformer** — one/multiple mapped spans, same-key consistency, unmapped
  passthrough, ordinary text untouched, no mappings, determinism, longest-key
  wins on a ZWJ sequence, replacement bound, caller inputs never mutated.
- **Entity offsets** — covering bold preserved and recomputed for longer and
  shorter alt text, mixed Persian/Latin shift, entity payload preservation.
- **Entity fail-closed cases** — partial overlap, unsupported type,
  out-of-range offsets, mid-surrogate offsets, empty text, unusable mapping
  entries, existing custom-emoji entities never rewritten.
- **Resolution boundary** — the service calls only
  `resolve_effective_category`; OFF / no category / deleted category ⇒ no
  Telegram side effect; the service never re-implements resolution.
- **Reconstruction** — global default and per-chat override drive delivery,
  same destination, reply preserved/absent, custom-emoji + bold entities
  reach the bridge, media skipped, unsafe combinations skipped, delete
  happens only after a successful delivery, delete failure ⇒
  `replaced_undeleted`, missing message id, incomplete mapping listing.
- **Failure honesty** — bridge unavailable, delivery rejection with exactly
  one attempt (no alt-text retry).
- **Owner boundary** — foreign author, non-outgoing, invalid owner,
  inline-bot origin, bridge-bot author, pending panel input (another chat is
  still processed), empty text, unusable ids.
- **Loop prevention** — reconstructed message cannot re-enter, registry
  blocks the owner identity, no double reconstruction, bounded registries,
  TTL expiry, in-memory only.
- **Handler + architecture** — exactly one outgoing listener, non-owner
  ignored, `via_bot_id` passed through, service errors swallowed, cancellation
  re-raised, router registers it last; AST import audits (no `backend.ai`, no
  scheduler/supervisor/task-guard/executor/inline-engine), no
  `TelegramClient`/`create_task`/`immortal_create_task`/`forward_messages`/
  `asyncio.Lock`, no `events.` in the service or transformer, no regex/keyword
  routing, no Phase 5/6 surface, deletion through the existing facade, and one
  end-to-end path exercising the real serializer + transformer + bridge +
  delete facade.

## 5. Tests actually executed and exact results

All commands run with the project venv (`/home/daytona/codebase/.venv`), exit
statuses captured, cwd `/home/daytona/codebase/.m14` (source-inspection tests
resolve paths relative to cwd):

1. `python -m pytest tests/test_emoji_replacement_phase4.py -q` → **73
   passed** (0.50s).
2. `python -m pytest tests/test_emoji_replacement_phase4.py
   tests/test_emoji_state_phase3.py tests/test_emoji_category_service.py
   tests/test_emoji_ui.py tests/test_emoji_ui_phase2.py
   tests/test_emoji_library_import.py tests/test_emoji_set_enumeration.py
   tests/test_bridge_delivery.py -q` → **332 passed** (1.06s) — Phase 0–3
   suites + the Phase 4 slice, no regressions (259 before + 73 new).
3. `python -m pytest tests/ -q` → **5490 passed, 26 skipped, 3 warnings**
   (119.10s) = Phase 3 baseline 5417 + 73 new — no regressions anywhere.
4. `py_compile` on all seven changed/added Python files → clean.
5. `git diff --check` → clean (no whitespace errors).
6. Architecture audit: the Phase 4 modules contain no `TelegramClient`,
   `create_task`, `immortal_create_task`, `forward_messages`, `asyncio.Lock`,
   `events.` (outside the single handler), no `backend.ai` import, no regex
   and no Phase 5/6 surface — pinned by the tests above rather than by a
   manual grep.

## 6. Architecture boundaries preserved

- **One update loop** — the replacement handler is registered on the existing
  self client through `backend/bot/router.py`, exactly like every other
  handler; no second client, no polling, no new loop.
- **One scheduler / one executor / one recovery authority** — untouched;
  nothing in the change set imports or creates one.
- **No keyword or semantic routing** — activation is explicit (Phase 3 state)
  and detection is exact mapping-table matching.
- **No AI authority** — no `backend.ai` import anywhere in the new code; the
  emoji pipeline cannot reach a model, and models cannot reach Telegram RPC.
- **Existing execution paths only** — delivery through
  `telegram_api.bridge`, deletion through `telegram_api.messages`, storage
  through `db/client.py`, UI input state through `helper/input_state.py`.
- **No new persistence** — no table, no column, no migration file, no env var.
- **Fail-closed posture** — every uncertainty (state, mapping, entity,
  capability, bridge, deletion) ends with the owner's original message intact
  and an honest status, never with a fabricated result.

## 7. Telegram / live verification status — NOT live-verified

- **No live Telegram**: no real outgoing message was ever processed, no real
  bridge delivery, no real deletion, no real panel opened. Every Telegram
  interaction was exercised through fakes at the exact boundary the bridge and
  the Telegram facade already consume.
- **No live Supabase**: everything ran against the in-memory fallback; the
  physical `emoji_library`, `emoji_categories`, `emoji_mappings`,
  `emoji_state`, `emoji_chat_overrides` tables still do not exist anywhere
  (MANUAL-ONLY schema documented in the Phase 1–3 reports, never executed).
- **Custom-emoji delivery is unproven** — whether the helper bot may attach
  `MessageEntityCustomEmoji` depends on the unresolved §34-D capability
  question. The code path sends the entities and reports a rejection
  honestly; it does not claim the premium visual lands.
- **Loop prevention, entity math and fail-closed ordering are proven at the
  unit level only** — no live echo of a reconstructed message was observed.

## 8. Limitations (honest)

1. **Text-only** — a message with media (with or without caption) is never
   reconstructed; captions are consequently not transformed either.
2. **Owner-visible failure surfacing is NOT implemented** — a failed
   reconstruction leaves the original untouched and logs a structured outcome;
   the owner is not notified and the Replacement panel does not display the
   last outcome. (ROADMAP §28 records this as open.)
3. **Deletion is a visible side effect** — the original message is deleted
   after delivery, so the reconstructed message carries a new message id and
   timestamp (documented §23 limitation); a failed delete leaves an honest
   duplicate.
4. **Replying to a deleted message** — once the original is deleted, any reply
   somebody sent to it shows as a reply to a deleted message; this is
   inherent to the delete+re-send design mandated by §15.
5. **Panel/AI interaction** — an owner message that both activates the AI
   (trigger word) and contains a mapped emoji is processed by the AI handler
   first and reconstructed afterwards; no coordination between the two was
   added (the replacement runs last deliberately).
6. **flood/rate adequacy** — unverified; a rate rejection is an honest failed
   outcome.
7. **Mapping keys are not emoji-classified** — the transformer replaces
   exactly what the owner's mapping table names (deterministic, no emoji
   classifier). A mapping whose key is ordinary text would therefore replace
   that text; that is the owner's explicit configuration, not inferred intent.

## 9. Unresolved owner decisions

| §34 | Status after Phase 4 |
|---|---|
| A — active category scope | Implemented at Phase 3 (default proposal); owner confirmation still OPEN |
| B — reconstruction ordering | Default proposal IMPLEMENTED in Phase 4 (send-first); owner confirmation still OPEN |
| C — bridge bot identity | Default proposal IMPLEMENTED in Phase 4 (reuse helper bot, no new env var); owner confirmation still OPEN |
| D — premium-visual delivery mode | **STILL OPEN and NOT resolved.** The boundary exists (entities sent as-is, rejection reported honestly, original untouched); the alt-text degradation was deliberately NOT implemented and no entitlement probing was added. |
| E — mapping persistence shape | As Phase 2 left it (default proposal implemented, owner sign-off open) |
| F — custom composition semantics | Untouched (Phase 5) |
| G — loop-prevention mechanism | Default proposal IMPLEMENTED in Phase 4 (sender-id structural check + inline-origin exclusion + bounded registries; no visible marker); owner confirmation still OPEN |
| H — conflict UI wording | Resolved at Phase 2 (unchanged) |
| I — notification behavior | Normal send used (no silent flag); optional confirmation untouched |

## 10. Remaining work

- **Phase 5** — Custom category composition (§26).
- **Phase 6** — Reactions (`telegram_api/reactions.py`, service, Glass UI
  action) — no code exists yet.
- **Phase 7** — hardening: media/caption reconstruction (§23), owner-visible
  failure surfacing (§28), flood/rate validation (§17), destination-type
  permission handling (§18), completion of the test plan (§30) and the live
  validation checklist (§31).
- **Owner actions** — apply the manual-only schema for the five emoji tables
  and run the live-Telegram checklist; decide §34-B/C/D/G; confirm §34-A/E.

## 11. Delivery metadata

- **Branch:** `m14-stt` (the workspace branch that tracks `origin/main`).
- **Implementation commit:** `bd20382` was the Phase 3 commit; this Phase 4
  change set is committed on top of it (see the `git log` entry
  `feat(emoji): implement Phase 4 — reconstruction …`).
- **Push result / remote HEAD / worktree state:** recorded in §12 below,
  filled from the verification run that followed the push (the delivery
  metadata cannot contain its own commit hash).

## 12. Push verification (recorded after delivery)

| Check | Result |
|---|---|
| `git push origin m14-stt:main` | recorded in the finalization commit (see below) |
| `git fetch origin main` + `git rev-parse HEAD origin/main` | recorded in the finalization commit |
| `git ls-remote origin refs/heads/main` | recorded in the finalization commit |
| Final working tree | recorded in the finalization commit |

> The Phase 4 implementation commit and a tiny follow-up documentation commit
> exist because a commit cannot embed its own SHA; the follow-up changes only
> this section and the report's phase-status line, and both commits belong to
> the same request.
