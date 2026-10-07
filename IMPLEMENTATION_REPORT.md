# Implementation Report

> **This document reflects the CURRENT repository state after Phase 6.**
> It replaces the Phase 5 report. Where this document and the code disagree,
> the code is authoritative and this document must be updated.

| Field | Value |
|---|---|
| Feature | Emoji & Reaction (premium/custom emoji replacement + reactions) |
| Phase | **Phase 6 — Reactions (§27)** |
| Status | **IMPLEMENTED + TESTED offline** (unit/regression suites); live Telegram/Supabase **NOT verified** |
| Branch | `main` |
| Base commit (before this phase) | `1f5a36d` — docs(emoji): fix the tab escaping artifact in the Phase 5 delivery section |
| Schema work | **None added by Phase 6.** No table, no column, no migration, no SQL. **No SQL was executed.** |
| Next phase | Phase 7 — Hardening (§28/§30/§31) |

---

## 1. Objective (Phase 6)

Implement the minimal reaction subsystem described by ROADMAP §27 — and
nothing else:

```
Telegram self-client reaction RPC
  → typed Telegram API wrapper   (backend/telegram_api/reactions.py)
  → reaction service             (backend/services/reaction_service.py)
  → existing Glass UI action     (backend/bot/handlers/emoji.py: 💬 React)
  → deterministic execution against the selected message
```

The phase explicitly does **not** redesign the Emoji Replacement subsystem:
reactions share no state, no code path and no panel with replacement. Out of
scope (untouched): media/caption reconstruction (§23), owner-visible failure
surfacing (§28), AI tools, auto/scheduled reactions, new background jobs, live
Telegram validation, any unrelated refactor or dependency change.

---

## 2. Implementation status

| Area | Status |
|---|---|
| Typed `SendReactionRequest` wrapper (2 forms: emoji / custom emoji) | IMPLEMENTED + TESTED |
| Input validation BEFORE any RPC (target + reaction value) | IMPLEMENTED + TESTED |
| Bounded timeout through the existing watchdog (`guarded_await`) | IMPLEMENTED + TESTED |
| Telegram exception normalization (`TelegramAPIError` / `TelegramTimeoutError`) | IMPLEMENTED + TESTED |
| Reaction service (deterministic apply, exactly ONE attempt, honest result) | IMPLEMENTED + TESTED |
| Owner boundary + explicit validated target (stale/foreign/missing/ malformed) | IMPLEMENTED + TESTED |
| Glass UI: 💬 React action (reply mode + honest result panel) | IMPLEMENTED + TESTED |
| Callback owner validation through the existing router | IMPLEMENTED + TESTED |
| No persistence / no schema | CONFIRMED (no db-layer change) |
| Live Telegram / Supabase verification | **NOT RUN — unverified** |

---

## 3. What was implemented

### 3.1 Typed Telegram wrapper — `backend/telegram_api/reactions.py` (NEW)

A narrow typed wrapper around `messages.SendReactionRequest` (Telethon
1.34.0), following exactly the conventions of its siblings `messages.py` /
`custom_emoji.py`:

- **One public call**: `send_reaction(client, chat_id, msg_id, reaction, *,
  big=False, add_to_recent=True)`. The caller passes the self client; the
  module builds the request itself. No parameter accepts an arbitrary TL
  request, so arbitrary RPC execution is not reachable through this boundary.
- **Exactly two reaction forms**: `ReactionEmoji(emoticon=…)` and
  `ReactionCustomEmoji(document_id=…)`, described by one plain-dict shape
  (`{"kind": "emoji", "emoji": …}` / `{"kind": "custom_emoji", "document_id": …}`).
- **Validation before any RPC**: `valid_chat_id` (non-zero int, never a bool),
  `valid_message_id` (positive int), `normalize_emoji` (non-empty single-line
  value ≤ 32 UTF-16 units — a single grapheme; ZWJ sequences pass, sentences do
  not), `normalize_document_id` (positive int / digit string), and
  `normalize_reaction` which fails closed on any unknown kind or extra/missing
  payload. The value is validated, never rewritten.
- **Bounded timeout**: the existing runtime watchdog (`guarded_await`,
  `telegram:send_reaction`, 30 s) — the same mechanism the sibling modules use.
  No second timeout system was invented.
- **Exception normalization**: `TelegramTimeoutError` on a timeout,
  `TelegramAPIError` for everything else (an already-normalized error is
  re-raised unchanged; `CancelledError` always propagates).
- **Honest plain-dict result**: `{chat_id, message_id, reaction, big}` echoed
  from the validated input. Telegram's `Updates` response is not a reaction
  state, so nothing about the message's current reactions is inferred from it.
- **No retry and no fallback**: exactly one request per call; a rejected
  reaction is reported, never re-sent in a different representation.

Telethon resolves the request's `peer` itself (`SendReactionRequest.resolve`
→ `client.get_input_entity`), so the wrapper passes the explicit integer
`chat_id` and does not fabricate an `InputPeer`.

### 3.2 Reaction service — `backend/services/reaction_service.py` (NEW)

The deterministic business layer for ONE explicit operation:
`react_to_message(client, owner_id, chat_id, msg_id, reaction)`.

- **Owner boundary**: `owner_is_valid` (positive int, never a bool) is checked
  before anything else; an invalid/absent owner is refused without a single RPC.
- **Explicit, validated target — never inferred**: the service resolves the
  exact `(chat_id, msg_id)` pair through the existing Telegram facade
  (`telegram_api.messages.get_message`) BEFORE reacting. A missing/deleted
  message (`E_TARGET`), a target that resolves to a different chat
  (`E_TARGET_FOREIGN`), or an unreadable target read (`E_TARGET_STALE`) is
  refused and produces **no** reaction call. There is no "last message", no
  chat-history context, no sender heuristic and no keyword routing.
- **Fail-closed input validation**: `E_OWNER`, `E_TARGET`, `E_REACTION`
  (unusable reaction value), `E_NO_CLIENT` (self client not connected) all
  return before any RPC.
- **Exactly one attempt, honest failure**: a Telegram rejection returns
  `E_TELEGRAM` with Telegram's own message as `detail`; no retry, no
  substitution of a different reaction representation.
- **No persistence, no second infrastructure**: a reaction is an action, not
  configuration — the module touches no table, imports no db layer, creates no
  client/loop/scheduler/executor, and imports nothing from `backend.ai`.
- `reaction_label()` renders the owner-facing label without ever fabricating a
  visual (a custom emoji is named by its document id).

### 3.3 Glass UI — `backend/bot/handlers/emoji.py`

ONE new row (`💬 React to a message` → `action:emoji_react`) on the existing
Emoji panel, plus the new action and its reply-mode handler. It reuses exactly
the machinery the Save panel's Deep Save reply mode uses:

- **Arming** (`_react_action`): refuses honestly without a self client / owner /
  chat, then arms the existing per-owner pending-input state
  (`input_state.set_pending`, panel id `emoji_react`, **the current chat**, the
  panel message's id, a 90 s backstop timeout) and renders the prompt in place.
  Arming performs no Telegram call.
- **Reply flow** (`_react_reply_wait_handler`): the owner replies to the exact
  message they want to react to. The handler reads that reply back through the
  self client and resolves the target deterministically from the reply's own
  `reply_to_msg_id` — **never** the owner's reply itself. A cross-chat reply
  header (`reply_to_peer_id`) is refused rather than guessed at.
- **The reply's own content is the reaction value**: a premium emoji arrives as
  a `MessageEntityCustomEmoji` (→ `ReactionCustomEmoji(document_id)`), anything
  else is the reply's stripped text (→ `ReactionEmoji`). One representation is
  chosen from what Telegram actually delivered — the custom-emoji entity wins
  over its fallback glyph, and there is never a substitution fallback.
- **Honest panels**: success states the applied reaction and the target message
  id; failures render the service's `detail` plus its stable error code
  (`E_TARGET`, `E_TARGET_FOREIGN`, `E_TARGET_STALE`, `E_REACTION`,
  `E_TELEGRAM`, …). Non-reply, empty reaction value and unreadable reply each
  get their own explicit message.
- **Owner-scoped**: the action arms the pending entry for the engine's owner id
  and the reply flow passes that same owner id to the service; the action is
  reachable only through the existing owner-gated callback router (a non-owner
  callback is answered and dispatches nothing).
- **No new listener, no new panel, no new input registration**: one action
  registered in the existing action registry; the module still contains no
  `events.*`, no `@client.on`, no `create_task`, no `asyncio.Lock`.

### 3.4 Tests — `tests/test_reaction_phase6.py` (NEW, 80 tests)

See §5.

---

## 4. Exact files changed

| File | Change |
|---|---|
| `backend/telegram_api/reactions.py` | **NEW** — typed `SendReactionRequest` wrapper (2 forms, validation, bounded timeout, normalized exceptions, plain-dict echo) |
| `backend/services/reaction_service.py` | **NEW** — deterministic apply-reaction service (owner boundary, explicit target resolution, fail-closed results, one attempt) |
| `backend/bot/handlers/emoji.py` | +138 lines: the `💬 React to a message` row, `_react_action`, `_react_reply_wait_handler`, `_reaction_from_reply`, `_render_reaction_result`, the reply-mode constants and the `emoji_react` action registration |
| `tests/test_reaction_phase6.py` | **NEW** — 80 focused Phase 6 tests |
| `IMPLEMENTATION_REPORT.md` | Current-state rewrite (this document) |
| `ROADMAP.md` | Current-state update: Phase 6 implemented/tested offline, Phase 7 next, reactions no longer unimplemented |

**Intentionally untouched files** (verified by `git status` / `git diff`):
`backend/ai/**` (no AI involvement, no tool), `backend/runtime/**` (no
supervisor/keepalive change — the new code only *uses* `guarded_await`),
`backend/bot/router.py` (no new listener is registered anywhere),
`backend/bot/handlers/emoji_replacement.py`, `backend/services/
emoji_transformer.py`, `backend/services/emoji_replacement_service.py`,
`backend/services/emoji_state_service.py`, `backend/services/
emoji_library_service.py`, `backend/services/emoji_category_service.py`,
`backend/db/client.py` (no persistence), `backend/helper/**` (the existing
panel/input/callback machinery is reused unchanged), `backend/telegram_api/`
siblings (`api.py`, `messages.py`, `custom_emoji.py`, `bridge.py`),
`supabase/**` and `sql/**` (no migration, no SQL), `DATABASE_ARCHITECTURE.md`
(no schema requirement was discovered), `AGENTS.md` (§15 doc-consistency rule:
only the phase documents are updated).

---

## 5. Tests added (focused Phase 6, `tests/test_reaction_phase6.py`, 80 tests)

The suite is split so that the dependency order is explicit:

- **Wrapper — payload correctness (5):** exactly one typed
  `SendReactionRequest` with the right `peer`/`msg_id`; emoji form serializes
  `ReactionEmoji`; custom form serializes `ReactionCustomEmoji` with its
  document id; the `big` flag rides only when asked; the result is a plain
  JSON-safe dict; a negative (supergroup) chat id is a valid target.
- **Wrapper — invalid input rejected before any RPC (15):** 9 parametrized
  unusable target pairs (`0`, `None`, `True`, `"1"`, negative/`None`/string/
  bool message ids) and 16 parametrized unusable reaction values (non-dict,
  wrong kind, `"paid"`, empty/whitespace/surrounding-whitespace/sentence/
  non-string emoji, missing/zero/negative/bool/`"abc"` document id) — every one
  raises `TelegramAPIError` and **no request is recorded**.
- **Wrapper — normalization edges (3):** the two accepted forms and
  digit-string document ids; the 32-unit emoji bound at its edge; a ZWJ family
  sequence accepted as one reaction.
- **Wrapper — timeout + exception normalization (5):** a hanging call is cut
  at the bound (with exactly one attempt recorded); a Telegram
  `asyncio.TimeoutError` becomes `TelegramTimeoutError`; a rejection becomes
  `TelegramAPIError` with the cause chained and exactly one attempt (no
  silent retry); an already-normalized error is re-raised unchanged;
  `CancelledError` is never swallowed; and the source pins the existing
  watchdog (`guarded_await` + `_SHORT_CALL_TIMEOUT`) as the timeout mechanism.
- **Service — success (3):** reacts once to the explicit target; reacts with a
  custom emoji; leaves the Phase 3 state/categories/mappings untouched.
- **Service — fail-closed target (4):** missing target, foreign target
  (resolved in another chat), stale/unreadable target read, deleted target id —
  each reported with its own error code and **zero** reaction requests.
- **Service — boundary + validation before any RPC (5):** 5 parametrized
  invalid owners, 4 parametrized unusable target pairs, unusable reaction
  value, missing self client, and the single `owner_is_valid` predicate.
- **Service — honest rejection, no retry (4):** a rejected custom-emoji
  reaction reports `E_TELEGRAM` with Telegram's message and keeps the reaction
  in the result; the same call is never retried in another representation (both
  directions proven); a reaction performs no message side effect (the only
  recorded request is `SendReactionRequest`).
- **Service — label (1):** the owner-facing label for emoji / custom / unknown.
- **UI — registration + arming (6):** the action is registered and reachable
  from the panel; callback data stays ≤ 64 bytes; arming sets the reply-mode
  pending entry for the current chat with no Telegram call; honest failures
  without a self client / owner / chat.
- **UI — reply flow (15):** reacts to the replied-to message (never the reply);
  a premium-emoji entity selects the custom-emoji form; unrelated entities are
  ignored; non-reply, cross-chat reply header, missing reply, unreadable reply,
  empty reaction value, invalid reaction value, Telegram rejection, stale
  target and foreign target each render an honest panel with **no** unwanted
  RPC; no message side effect; the result panel offers the next reaction slot.
- **UI — callback owner validation (3):** a non-owner callback is answered and
  dispatches nothing (no edit, no pending input); the owner callback reaches
  the action through the **existing** router and arms the entry; the react
  flow adds no listener (`events.*` / `@client.on` absent from the section).
- **Separation from the replacement subsystems (3):** a pending react input is
  left alone by the Phase 4 pipeline (`skipped_pending_input`, zero Telegram
  calls); the new modules contain no replacement surface (`emoji_state`,
  `resolve_effective_category`, `emoji_mappings`, `emoji_transformer`,
  `process_outgoing_message`, `transform_message`, `send_reconstructed`,
  `bridge` — checked on code with comments/strings stripped); the react UI
  section contains no mapping/state/transform/delete/send surface.
- **Architecture audit (6):** AST import audit (no `backend.ai`, no scheduler/
  supervisor/task-guard/executor/inline-engine); no second loop/client/session/
  executor (`TelegramClient`, `run_until_disconnected`, `create_task`,
  `immortal_create_task`, `guarded_create_task`, `asyncio.Lock`,
  `new_event_loop`, `run_until_complete`, `call_later`, `forward_messages`,
  `SendMessagesRequest`, `events.NewMessage`); no regex/keyword routing; the
  wrapper exposes no callable that accepts an arbitrary TL request; no db/
  schema surface; the flow registers one action (not a panel or an input).

**No existing test was removed, weakened, or skipped.** One Phase 3
architecture test (`tests/test_emoji_state_phase3.py::test_phase3_surface_keeps_no_execution_logic`,
which forbids the words *transform*/*reconstruct* anywhere in
`backend/bot/handlers/emoji.py`, including prose) caught the first wording of
the new section comment; the comment was reworded — the test was left
untouched.

---

## 6. Checks actually executed and exact results

All commands were run from the repository root (`/home/daytona/codebase`,
Python 3.10).

| Check | Command | Result (exit status) |
|---|---|---|
| Focused Phase 6 | `python3 -m pytest tests/test_reaction_phase6.py -q` | **80 passed** (exit 0, 0.50s) |
| Emoji & Reaction regression (Phases 0–6) | `python3 -m pytest tests/test_emoji_library_import.py tests/test_emoji_category_service.py tests/test_emoji_ui.py tests/test_emoji_ui_phase2.py tests/test_emoji_set_enumeration.py tests/test_emoji_state_phase3.py tests/test_emoji_replacement_phase4.py tests/test_bridge_delivery.py tests/test_emoji_composition_phase5.py tests/test_reaction_phase6.py -q` | **492 passed** (exit 0) — Phase 5 baseline 412 + 80 |
| Full suite | `python3 -m pytest tests/ -q -p no:randomly` | **5650 passed, 26 skipped, 3 warnings** (exit 0, 122.13s) — Phase 5 baseline 5570 + 80 |
| Compile | `python3 -m py_compile backend/telegram_api/reactions.py backend/services/reaction_service.py backend/bot/handlers/emoji.py tests/test_reaction_phase6.py` | clean (`PY_COMPILE_OK`, exit 0) |
| Whitespace / conflict markers | `git diff --check` | clean (`DIFF_CHECK_OK`, exit 0) |
| Diff inspection | `git status --short`, `git diff --stat` | only the intended files changed (see §4) |

No live Telegram call and no live Supabase call was made anywhere in this
work. The Telethon reaction surface was verified in the installed venv
(1.34.0): `messages.SendReactionRequest`, `ReactionEmoji`,
`ReactionCustomEmoji`, `ReactionEmpty` all exist; `ReactionPaid` does **not**
exist in this version and is not used (the roadmap's mention of it was
inaccurate and nothing depends on it).

---

## 7. Architecture constraints verified

| Constraint | How it is verified |
|---|---|
| Deterministic, no AI | AST import audit on both new modules: no `backend.ai` import (test). No tool, dispatcher, provider, prompt or memory module was touched |
| No second client / loop / listener / executor / scheduler | Source audit of both new modules for `TelegramClient`, `run_until_disconnected`, `create_task`, `immortal_create_task`, `guarded_create_task`, `asyncio.Lock`, `new_event_loop`, `run_until_complete`, `call_later`, `events.NewMessage`, `forward_messages`, `SendMessagesRequest`; the react UI section is checked for `events.*` / `@client.on`; `backend/bot/router.py` is unchanged |
| No arbitrary TL execution | The wrapper constructs exactly one typed request; no public callable takes a `request` parameter (test) |
| No regex / keyword routing | No `re` import or `re.` usage in the new modules (tokenized source test) |
| Existing timeout/exception conventions reused | The wrapper uses `guarded_await` with the module `_SHORT_CALL_TIMEOUT` and maps to `TelegramTimeoutError` / `TelegramAPIError` exactly like `messages.py` / `custom_emoji.py`; no new timeout system |
| Reactions ≠ replacement (§27) | Separation tests: no replacement-state/mapping/transformer reference in code; no delete/send/forward side effect (the only recorded request is `SendReactionRequest`); a pending react input is left to the Phase 4 pipeline's existing `skipped_pending_input` path |
| Explicit target only | The service re-resolves `(chat_id, msg_id)` and rejects missing / foreign / stale targets; the UI refuses cross-chat reply headers and non-replies; tests cover each refusal with zero RPC |
| Owner boundary is never callback data | The action arms the pending entry with the engine owner id; the reply flow passes that owner id to the service; the service refuses an invalid owner before any RPC; the existing callback router's `is_owner` gate is exercised end to end (a non-owner callback dispatches nothing) |
| Glass UI machinery reused | One action in the existing action registry, the existing pending-input reply mode, the existing `_edit_inline` edit path, the existing callback router — no new panel, no new input registration, no new listener |
| No schema / persistence | `backend/db/client.py` is untouched; the new modules contain no db/schema surface (test) |
| Confidence in limits | Bounds are explicit and tested: 30 s per RPC, 90 s handler backstop, 32 UTF-16 units per reaction emoji, ≤ 64-byte callback data |

---

## 8. Persistence / schema status

- **Phase 6 adds NOTHING to the database.** No table, no column, no index, no
  policy, no migration file. ROADMAP §27 describes reactions as an action, not
  persistent configuration, and the implementation matches: no SQL was
  executed, no Supabase project was touched.
- `DATABASE_ARCHITECTURE.md` was deliberately left untouched — no schema
  requirement was discovered.
- Carried forward from Phase 5 (unchanged, still MANUAL-ONLY and unexecuted):
  the `emoji_categories` Custom Category columns.

### 8.1 Required manual SQL (MANUAL-ONLY — not executed by this repository)

```sql
-- Emoji & Reaction, Phase 5 (§26): Custom Category columns.
-- Apply ONLY if a Supabase project is configured and Custom Category
-- persistence is wanted. Safe to run once; idempotent via IF NOT EXISTS.

alter table public.emoji_categories
  add column if not exists is_custom boolean not null default false;

alter table public.emoji_categories
  add column if not exists source_category_ids jsonb not null default '[]'::jsonb;
```

Convergence reasoning (why `CREATE TABLE IF NOT EXISTS` would NOT be enough):
`emoji_categories` already exists in any environment where the Phase 2 manual
schema was applied, so a create-if-not-exists statement would be a no-op and
would leave the new columns missing. The statements above are column additions
against the existing table and are the actual convergence requirement. On a
fresh environment, run the Phase 1–2 manual schema first, then these two
statements (in that order).

### 8.2 Rollback (MANUAL-ONLY)

```sql
-- Undo Phase 5's columns only. Mapping rows are NOT touched by this rollback,
-- so categories keep replacing normally as ordinary (non-composed) categories.

alter table public.emoji_categories
  drop column if exists source_category_ids;

alter table public.emoji_categories
  drop column if exists is_custom;
```

Rollback consequences (honest): after the rollback, custom rows are no longer
recognized as custom (`is_custom_category` is false), the compose UI reports
“Custom category not found”, composition refuses with honest storage failures,
and ordinary category CRUD plus replacement keep working. No data (library
entries or mapping rows) is destroyed by the rollback itself.

---

## 9. Telegram / live verification status — NOT live-verified

Nothing in this phase was validated against the real Telegram API or a real
Supabase project:

- No live reaction was applied (neither a Unicode emoji nor a custom-emoji
  document), so the §27 capability question — whether the reacting account's
  own premium emoji are accepted as reactions in a given chat — is **open**.
- No live reaction was attempted on another person's message or in a
  restricted chat, and reaction availability (`AvailableReaction` /
  `can_react`) is **not** pre-checked: Telegram's own error is surfaced
  honestly in `E_TELEGRAM.detail` instead of being guessed at.
- Reaction *removal* (`ReactionEmpty`) and multiple reactions were never in
  scope and are not implemented.
- The Phase 5 manual SQL was not executed anywhere; the physical
  `emoji_categories.is_custom` / `source_category_ids` columns do not exist in
  any environment as a result of this work.

Everything above is offline behavior only: unit/regression suites plus a fake
self-client boundary (the TL-request surface) and the in-memory fallback.

---

## 10. Limitations (honest)

- **No live validation** (see §9). The wrapper's request shape is verified
  against the real Telethon 1.34.0 types and the client's own `resolve()` path,
  but no request has been sent to Telegram.
- **Reaction availability is not pre-checked.** A chat that disallows reactions
  or a custom emoji the account cannot use fails with Telegram's own error,
  reported honestly — no capability probing, no fabricated success.
- **One reaction per call, one representation.** Changing or removing an
  existing reaction is not implemented (the owner re-runs React with another
  emoji; Telegram replaces the previous reaction server-side, which this
  repository does not verify).
- **The reaction value comes from the owner's reply content** (its custom-emoji
  entity, else its stripped text). A reply with surrounding text is refused as
  an unusable reaction value rather than parsed — no inference, no trimming of
  a sentence into an emoji.
- **Cross-chat replies are refused** rather than resolved; Telegram's
  `reply_to_peer_id` is treated as a foreign target and reported.
- **The service performs one extra bounded read** (the target resolution)
  before the reaction. This is intentional (an explicit validated target), and
  it means a reaction costs two bounded RPCs.
- **The 90 s pending-input backstop** is a constant, not configuration; the
  primary bounds are the wrapper's two 30 s RPC timeouts.
- **Owner-visible failure surfacing (§28) is still incomplete** — reaction
  failures are shown in the panel the owner is looking at, but there is no
  broader notification surface; structured log lines remain the only out-of-band
  record.
- **Documentation gap carried forward:** the base-table DDL for
  `emoji_library` / `emoji_categories` / `emoji_mappings` / `emoji_state` /
  `emoji_chat_overrides` (Phases 1–3) is recorded in the git history of this
  file (the Phase 1–3 reports), not restated by this current-state document;
  §8.1 records only the Phase 5 column additions. The db-layer code contract
  is authoritative for the column sets.
- **Not implemented here** (unchanged): media/caption reconstruction (§23),
  owner-visible failure surfacing (§28), flood/rate validation (§17),
  destination-type permission handling (§18), auto/scheduled reactions
  (deferred, §4).

---

## 11. Unresolved owner decisions

| # | Decision | Current status after Phase 6 |
|---|---|---|
| A | Active category scope | Implemented as documented (global default + per-chat override). Confirmation still OPEN |
| B | Reconstruction ordering | Implemented as documented (send-first). Confirmation still OPEN |
| C | Bridge bot identity | Implemented as documented (existing helper bot). Confirmation still OPEN |
| D | Premium-visual delivery mode | UNRESOLVED — no alt-text fallback, no entitlement probing, no fabricated success |
| E | Mapping persistence shape | Default proposal implemented (dedicated tables). Owner sign-off still OPEN |
| F | Custom composition semantics | Implemented as the documented default (snapshot-on-compose + explicit Refresh). Owner confirmation still OPEN |
| G | Loop-prevention mechanism | Implemented as documented (structural, no visible marker). Confirmation still OPEN |
| H | **Reaction UX (new in Phase 6)** | Implemented as the documented default: the panel arms reply mode and the reply's own content is the reaction value; the wrapper/service support both emoji and custom-emoji forms. **The owner never approved this UX** — it is the smallest flow that satisfies §27 without inference. Alternatives (a library picker for the reaction value, a fixed quick-reaction set) remain open and would touch only the UI section |

---

## 12. Remaining work / next phase

- **Next: Phase 7 — Hardening**: owner-visible failure surfacing (§28), the
  remaining test-plan items (§30), the validation-checklist execution (§31) and
  the documentation landing updates (`AGENTS.md`, this report) — per §32.
- Live-Telegram validation of the whole feature (library import, replacement,
  composition, and now reactions) remains pending the owner's environment.
- The manual SQL in §8.1 must be applied by the owner before Custom Category
  persistence works against a real Supabase project.
- Open owner decisions A–H (above) are **not** approved by this phase merely
  because the documented defaults were implemented.

---

## 13. Delivery metadata

A commit cannot contain its own SHA. The Phase 6 implementation commit is
recorded here by its subject line; its exact SHA (and the SHA of the
documentation-only delivery-metadata commit that carries this section) live in
the repository's git history.

| Field | Value |
|---|---|
| Branch | `main` |
| Base commit (before this phase) | `1f5a36d` — docs(emoji): fix the tab escaping artifact in the Phase 5 delivery section |
| Phase 6 commit | `feat(emoji): implement Phase 6 — reactions (typed SendReactionRequest wrapper, reaction service, Glass UI react action)` |
| Commit contents | the 6 intended files only (`backend/bot/handlers/emoji.py`, `backend/services/reaction_service.py`, `backend/telegram_api/reactions.py`, `tests/test_reaction_phase6.py`, `IMPLEMENTATION_REPORT.md`, `ROADMAP.md`) |
| Push result | recorded in §14 after the delivery check |
| Remote main HEAD after the Phase 6 push | recorded in §14 (local `HEAD` == `origin/main` == `refs/heads/main` required) |
| Final worktree state | recorded in §14 |

---

## 14. Push verification (recorded after delivery)

The verification commands and their exact results are recorded here by the
documentation-only commit that follows the Phase 6 implementation commit:

- `git fetch origin main` → fetched `main` into `FETCH_HEAD` (exit 0)
- `git push origin main` → fast-forward to the Phase 6 commit (exit 0)
- `git rev-parse HEAD` == `git rev-parse origin/main` (same commit)
- `git ls-remote origin refs/heads/main` → the same commit (SHA + TAB +
  `refs/heads/main`)
- `git status --short` → empty (clean worktree, no untracked leftovers)

If any of these lines disagrees with the repository's live state, the live
state is authoritative and this section must be corrected.
