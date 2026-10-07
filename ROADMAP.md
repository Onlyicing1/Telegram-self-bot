# Emoji & Reaction Feature Roadmap

> **Current-feature execution roadmap.** This file is NOT a changelog and NOT a
> permanent historical record. It contains only the roadmap and current
> implementation status for the feature currently being developed: **Emoji &
> Reaction**. When this feature is fully completed, this file will be reset and
> completely replaced with the next feature's roadmap. It never accumulates
> multiple unrelated features.
>
> **Status of this document:** CURRENT-FEATURE ROADMAP + IMPLEMENTATION STATUS.
> Phase 0's code slice (entity-aware serialization §22 + send-only Bot bridge
> §17), Phase 1 (Emoji Library + bounded, deterministic Saved Messages
> import — §7 library-entry shape, §8 storage/dedup, §9 import INCLUDING
> sticker-set resolution/enumeration, the Glass UI import/report panels and
> the 2×5 library browser), Phase 2 (Categories & Mappings — §10 CRUD,
> §11 mapping model + uniqueness, §12 conflict UI, §25 2×5 pagination), and
> Phase 3 (State & Toggle — §13 active-category state, §14 replacement
> toggle, §29 persistence + first-boot OFF) are IMPLEMENTED and unit-TESTED
> (no live Telegram). Everything else below remains PLANNED. Schema files
> are untouched: the `emoji_library`, `emoji_categories`, `emoji_mappings`,
> `emoji_state` and `emoji_chat_overrides` tables are MANUAL-ONLY and have
> not been executed anywhere (§29; exact schema in
> `IMPLEMENTATION_REPORT.md`).

---

## 1. Feature Objective

Give the owner a premium/custom-emoji pipeline for their own outgoing messages:

- Import premium/custom emoji **collections** by sending an emoji from the
  desired collection to **Saved Messages** (the self-bot identifies the
  collection/set and extracts its emojis into an application-managed library).
- Organize imported emojis into **categories** that map **simple emoji →
  premium/custom emoji**.
- While **Emoji Replacement** is ON and a category is active, transform the
  owner's own outgoing messages so that mapped simple emojis appear to the
  audience as premium emojis — via **delete-original + bot-sends-new-message
  reconstruction** in the same destination — while preserving everything that
  is not an emoji.
- Provide a separate **reaction** capability (adding emoji reactions through
  the self-client), cleanly separated from the replacement subsystem.

Product shape (from the feature request):

```
Premium Emoji Library
    |
    +-- Premium Emoji X
    +-- Premium Emoji Y
    +-- Premium Emoji Z
    +-- ...
          |
          v
Categories
    +-- 002     (simple emoji -> premium emoji)
    +-- Aya     (simple emoji -> premium emoji)
    +-- Custom  (mappings reused/composed from existing categories)
```

Status: **PLANNED**.

---

## 2. Current Status

**Overall feature status: IN PROGRESS — Phase 0 (entity-aware serialization
+ send-only Bot bridge), Phase 1 (Emoji Library + deterministic Saved
Messages import), Phase 2 (Categories & Mappings: CRUD, mapping editor,
uniqueness, conflict UI, 2×5 pagination), and Phase 3 (State & Toggle:
replacement toggle, global default + per-chat override, resolution
boundary) implemented and unit-tested; no end-to-end replacement flow yet.**

The repository originally contained **no** emoji-substitution, premium-emoji
library, or reaction feature (verified by grep across `backend/` and `tests/`
for `reaction`, `custom_emoji`, `MessageReactions`, `ReactRequest` — zero
functional hits; the only "emoji" mentions are unrelated tokenization/
character-width logic in `backend/ai/preparation_policy.py`,
`backend/ai/semantic_delete.py`, `backend/ai/tools/delivery.py`,
`backend/helper/font_style.py`, `backend/bot/handlers/ai.py`,
`backend/ai/confirmation.py`).

The feature code now present is the Phase 0 slice (`backend/telegram_api/
_helpers.py`, `backend/telegram_api/bridge.py`, `tests/test_bridge_delivery.py`)
and the Phase 1 slice (`backend/services/emoji_library_service.py`, the
`emoji_library` functions in `backend/db/client.py`, the set-enumeration
facade `backend/telegram_api/custom_emoji.py`, the Glass UI panels
`backend/bot/handlers/emoji.py`, and the three test suites,
`tests/test_emoji_library_import.py`, `tests/test_emoji_set_enumeration.py`,
`tests/test_emoji_ui.py`) and the Phase 2 slice
(`backend/services/emoji_category_service.py`, the `emoji_categories` /
`emoji_mappings` functions in `backend/db/client.py`, the category/mapping
panels in `backend/bot/handlers/emoji.py`, and the two test suites
`tests/test_emoji_category_service.py`, `tests/test_emoji_ui_phase2.py`).
No part of the feature has been verified against live Telegram.

- [x] Premium emoji library — storage + deduplication (IMPLEMENTED, TESTED) + library browser panel (IMPLEMENTED, TESTED)
- [x] Collection import — bounded entity-based Saved Messages scan + bounded set resolution/enumeration + Glass UI import/report panels (IMPLEMENTED, TESTED)
- [x] Category management — CRUD + deletion semantics + mapping counts (Phase 2, IMPLEMENTED, TESTED)
- [x] Mapping UI — editor flow + list/edit/delete + 2×5 pagination (Phase 2, IMPLEMENTED, TESTED)
- [x] Conflict handling — never-silent-overwrite + conflict panel with explicit Replace/Cancel (Phase 2, IMPLEMENTED, TESTED)
- [x] Replacement toggle + active-category state (global default + per-chat override) + resolution boundary (Phase 3, IMPLEMENTED, TESTED)
- [ ] Message reconstruction
- [ ] Entity preservation
- [ ] Bot bridge
- [ ] Loop prevention
- [ ] Custom category
- [ ] Reactions
- [x] Tests — offline suites at 5417 passed / 26 skipped (live verification NOT run)
- [ ] Documentation
- [ ] Final verification

Nothing is complete merely because it is planned. Status vocabulary used in
this document: **PLANNED / IN PROGRESS / IMPLEMENTED / TESTED / VERIFIED /
DEFERRED / BLOCKED**.

---

## 3. Scope

In scope for this feature:

1. Premium/custom emoji **library** (imported from Saved Messages collection
   sends), managed by the application.
2. **Categories** containing `simple emoji → premium emoji` mappings
   (category-scoped definitions; duplicates within one category are a
   conflict, never a silent overwrite).
3. **Selection UI** over Glass UI inline panels with 2×5 pagination.
4. **Active category** selection driving replacement.
5. **Emoji Replacement toggle** (ON/OFF), independent of category choice.
6. **Message reconstruction** of the owner's own outgoing messages:
   transform mapped emojis → delete original → **bot** sends a NEW message to
   the same destination.
7. **Emoji-only transformation invariant** with entity/formatting/media/
   caption preservation.
8. **Multi-emoji** replacement in a single reconstruction; unmapped emojis
   untouched.
9. **Loop prevention** for bot-authored replacement messages.
10. **Custom category** composing/reusing mappings from existing categories.
11. **Reaction functionality** as a separate subsystem (add reactions via the
    self-client).
12. Error/capability handling and configuration/state persistence for all of
    the above.

Out of scope: §4.

Status: **PLANNED**.

---

## 4. Non-Goals / Deferred

| Item | Status | Rationale |
|---|---|---|
| Generic text rewriting / message rephrasing | **NOT a goal** | The transformation layer must modify ONLY emoji content. |
| Naive global string replacement as final architecture | **NOT a goal** | Can corrupt Telegram entities; §22 requires offset-safe entity handling. |
| Editing the original message in place as the replacement mechanism | **NOT a goal** | §F/§15 mandate delete + bot-sends-new reconstruction. |
| Forward Save / forwarding as part of replacement | **NOT a goal** | Repository rule: no Forward Save anywhere in the save path. |
| Incoming (non-owner) message transformation | **NOT a goal** | §I authorization boundary. |
| Any new executor, scheduler, update loop, or semantic/regex intent routing | **NOT a goal** | §3 architectural constraints; §5 existing architecture is authoritative. |
| AI-authored emoji mapping decisions | **Deferred** | AI may interact only via ToolRegistry/ToolExecutor later; no AI involvement designed for MVP. |
| Reaction automation rules (auto-react, scheduled reactions) | **Deferred** | Not requested; keep the reaction subsystem minimal. |
| Database schema/SQL changes | **Deferred to implementation phase (manual)** | Supabase schema changes are manual and outside the repo; `DATABASE_ARCHITECTURE.md` is protected. This roadmap documents requirements only. |
| Telegram Premium purchase management / Fragment username purchases | **NOT a goal (hard constraint)** | The app manages emoji the owner already has; see §28. |

Status: **PLANNED** (deferral decisions, not work).

---

## 5. Existing Architecture Discovered During Investigation

Findings from the current tree (all paths verified):

### 5.1 AI decision path (authoritative pipeline to preserve)

```
RuntimeSupervisor (backend/runtime/supervisor.py) — single recovery authority
  → router.register_all() (backend/bot/router.py)
  → outgoing handlers (backend/bot/handlers/ai_unified.py: @client.on(events.NewMessage(outgoing=True)))
  → Dispatcher (backend/ai/engine/dispatcher.py)
  → ProviderManager (backend/ai/providers/)
  → ToolRegistry / ToolExecutor (backend/ai/tools/registry.py, executor.py)
  → existing services (backend/services/*)
  → Telegram (backend/telegram_api/*, Telethon) / Supabase (backend/db/client.py)
```

- `backend/bot/handlers/ai_unified.py` is the canonical AI activation handler
  (outgoing-only, `is_owner` gate). Any AI interaction with the new feature
  must surface as **tools in `backend/ai/tools/`** executed by the existing
  `ToolExecutor`; no second execution path.
- Deterministic semantic/regex intent routing was deliberately removed
  (commit `eb4d852`); the replacement pipeline must be panel-driven or
  tool-executed, never keyword-matched from message text.

### 5.2 Glass UI inline panel system (UI prior art)

- `backend/helper/panel_registry.py` — `register_panel()` registry (panel
  ids, parents, titles).
- `backend/helper/inline_engine.py` — inline-mode dispatch: the self-bot
  triggers an inline query against the helper bot and auto-sends the result;
  callback actions are routed by `action:*` / `panel:*` / `input:*` callback
  prefixes.
- `backend/helper/inline_sender.py` — `send_inline_panel` + pending-input
  listener.
- `backend/helper/panels.py` — `InlinePanelBuilder` (`add_row(text,
  callback_data)`, `add_buttons(*buttons)`, `build()` returns button rows).
- `backend/helper/input_state.py` — per-owner pending-input state machine.
- Pagination prior art: `backend/bot/handlers/retrieve.py` (per-page slicing,
  `◀ Prev` / `page/total` / `Next ▶` rows), `backend/bot/handlers/misc.py`
  font panel (`action:font_page:prev/next`, `1/N` center button),
  `backend/bot/handlers/ai.py` test-grid pagination (`_test_grid_page_size`).
- Panel settings caching: `backend/helper/panel_settings.py`,
  `backend/services/panel_settings_repository.py` (single-row global
  settings persisted in a `panel_settings` table keyed `"global"`).

### 5.3 Bot-account presence

- `backend/helper/client.py` — the **only** bot account in the system:
  an optional Telethon client built from `BOT_TOKEN` (bot login via
  `start(bot_token)`), exposing `is_available()`, `get_bot_username()`,
  `get_bot_id()`. Used today for inline UI rendering. If `BOT_TOKEN` is unset
  the helper is disabled and Glass UI falls back to edit-in-place text.
- **Implication:** the §17 Bot bridge can only be built on this existing
  helper bot client or a dedicated second bot token. There is no other bot
  infrastructure in the repository.

### 5.4 Telegram facade (self-client RPC)

- `backend/telegram_api/api.py` — `TelegramAPI` facade; tools receive it via
  `ToolContext.telegram`; the service layer may use `TelegramAPI.client` for
  complex operations.
- `backend/telegram_api/messages.py` — `send_message`, `edit_message`,
  `delete_messages`/`delete_message`, forward, search, iterate; short calls
  bounded via `guarded_await` (`backend/runtime/operation_watchdog.py`),
  exceptions normalized to `TelegramAPIError`/`TelegramTimeoutError`
  (`backend/telegram_api/exceptions.py`).
- `backend/telegram_api/_helpers.py::serialize_message` — message → plain
  dict. **Gap closed in the Phase 0 slice:** it now also serializes the
  `entities` list (UTF-16 offsets/lengths + payload) and adds the
  dict→TL rebuild helpers (§22).
- The send-only Bot bridge (`backend/telegram_api/bridge.py`) now exists
  (§17); no reaction or custom-emoji wrapper exists yet in
  `backend/telegram_api/`.

### 5.5 Telethon dependency (verified in venv, telethon 1.34.0)

Full TL surface required by this feature already ships in Telethon 1.34.0
(verified by inspecting the installed package):

- Reactions: `telethon.tl.functions.messages.SendReactionRequest(peer, msg_id,
  big, add_to_recent, reaction=[...])`; types `ReactionEmoji(emoticon)`,
  `ReactionCustomEmoji(document_id)`, `ReactionPaid`, `MessageReactions`,
  `AvailableReaction`, `MessageReactionsList` helper.
- Custom emoji: `MessageEntityCustomEmoji(offset, length, document_id)`,
  `DocumentAttributeCustomEmoji`, `TextWithEntities`,
  `messages.SendMediaRequest` (for stickers as media), sticker-set wrappers
  (`StickerSet`, `messages.get_sticker_set` equivalent).

No new Telegram library is required.

### 5.6 Persistence patterns

- `backend/db/client.py` — Supabase singleton with in-memory fallback; tables
  currently used by code: `saved_items`, `bio_state`, `username_state`,
  `bot_logs`, `emoji_library` (Phase 1 — its dedup read returns `None` on a
  failed durable read so the importer can fail closed instead of deduping
  against RAM); `get_next_save_code()` is atomic; heavy calls run via
  `asyncio.to_thread` with bounded timeouts; the bot never crashes on DB
  errors.
- `backend/ai/config_store.py` — `ai_config` table; documented degradation
  contract (`DEGRADED_READ_KEY`, `SESSION_ONLY_KEY`) distinguishing durable
  rows from in-memory fallback values.
- `backend/services/settings_service.py` + `backend/services/
  panel_settings_repository.py` — validated settings keys persisted as a
  single `panel_settings` row (`key="global"`), cached in-process.
- **No emoji/reaction table exists in any database.** Phase 1 code reads and
  writes an `emoji_library` table through the pattern above with the
  in-memory fallback covering development; the physical table is
  MANUAL-ONLY (exact schema in `IMPLEMENTATION_REPORT.md`, never executed
  here). Categories/mappings schema remains §7; SQL is explicitly out of
  scope for this task (manual Supabase work later).

### 5.7 Guard rails that apply to this feature

- `backend/bot/handlers/guard.py::is_owner` — every handler gates on owner.
- `backend/ai/tools/executor.py` — sole `tool.execute()` caller; per-tool
  status labels; `long_running=True` exemption from the generic tool timeout.
- Deletion authority: self-client can delete only **its own** outgoing
  messages in ordinary chats (§I/§19); the bot client cannot delete the
  user's messages at all (bot accounts cannot delete other users' messages).
- Runtime stability rules (`AGENTS.md` §4): no second supervisor/update loop;
  recovery only through `RuntimeSupervisor`.

Status: **INVESTIGATED — VERIFIED against the current tree**.

---

## 6. Proposed Feature Architecture

New modules (names are proposals; final names at implementation):

```
backend/emoji/                     # new feature package
├── library.py                     # premium emoji library (library ownership)
├── categories.py                  # category model + active-category state
├── mappings.py                    # mapping model + conflict detection
├── importer.py                    # Saved Messages collection import
├── transformer.py                 # emoji-only transformation (entity-safe)
├── reconstructor.py               # delete-original + bot-bridge delivery
└── reactions.py                   # reaction subsystem (separate)

backend/services/emoji_service.py  # business logic facade (handlers/tools call this)
backend/telegram_api/reactions.py  # typed SendReactionRequest wrapper
backend/telegram_api/entities.py   # extend entity-aware serialization (§22 gap)
backend/bot/handlers/emoji.py      # Glass UI panels (library/categories/mappings/toggle)
backend/ai/tools/emoji.py          # optional AI-facing tools (ToolRegistry only)
```

Flow (panel-driven MVP; AI optional and only via existing ToolRegistry):

```
owner sends premium emoji to Saved Messages
  → importer resolves collection → library rows
owner opens Menu → Emoji panel (Glass UI)
  → library browser (2×5 pagination) / category manager / mapping editor
  → conflict UI on duplicate mapping (§12)
owner toggles Emoji Replacement ON + selects active category (§13/§14)
owner sends outgoing message with mapped emoji
  → outgoing handler (self-client, owner-only) detects mapped emojis
  → transformer rewrites ONLY mapped emoji (entity-safe, §16/§22)
  → reconstructor: delete original (self-client) → helper bot sends NEW
    message to SAME destination (§17/§18)
  → loop guard suppresses reprocessing of the bot's message (§24)
```

Explicit constraint compliance:

- Exactly **one** update loop (self-client router handlers); the bot bridge
  **sends messages on demand**, it never registers its own update loop.
  Inbound callback handling for the bot bridge (if any) must reuse the
  existing helper callback machinery or be explicitly justified; default is
  fire-and-forget delivery.
- One scheduler remains (`backend/profile/scheduler.py`); this feature adds
  none.
- No keyword/regex intent routing: activation is explicit (panel toggle +
  active category), and message detection is structural (emoji codepoint →
  mapping lookup), not linguistic.

Status: **PLANNED**.

---

## 7. Core Data / Domain Model

Conceptual model (persistence shape decided at implementation; §29):
**library entries** (owned, one definition per premium emoji) are referenced
by **category mappings** (per-category `simple → premium`), and the **Custom
category** composes references across categories without duplicating library
entries.

```
PremiumEmoji (library entry)
  document_id: int          # Telegram custom-emoji document id
  set_id / set_short_name   # owning collection
  alt_text: str             # fallback glyph Telegram shows for the custom emoji
  source: imported | manual
  imported_from_msg_id      # Saved Messages message that carried it

EmojiCategory
  id / name (e.g. "002", "Aya", "Custom")
  is_custom: bool           # Custom composes references (§26)
  kind: global default | per-chat override (decision §13)

Mapping
  category_id → simple_emoji (unique per category) → premium document_id
  # references a library entry; never duplicates emoji definitions

RuntimeState
  replacement_enabled: bool          # §14
  active_category_id (scope per §13) # §13
```

The three distinct concerns that must not be conflated:

1. **Library ownership/reference** — the library owns each premium emoji
   definition (document_id, set, alt text).
2. **Category mapping** — a category maps a simple emoji to a library entry;
   the same simple emoji may map to different entries in different
   categories.
3. **Custom composition** — the Custom category references existing
   mappings/library entries; it must not duplicate definitions.

Phase 1 persisted shape (IMPLEMENTED + TESTED): table `emoji_library` with
`owner_id`, `document_id`, `alt_text`, `source` ("imported"),
`source_msg_id`, `created_at`, and `UNIQUE (owner_id, document_id)` — the
MANUAL-ONLY schema is documented in `IMPLEMENTATION_REPORT.md` and was never
executed. `set_id`/`set_short_name` remain NOT persisted: set enumeration
enriches the library with the set's real member documents through the same
`(owner_id, document_id)` identity, and every counter (unresolved documents,
setless documents, degraded state) is reported honestly — nothing is
fabricated.

Phase 2 persisted shape (IMPLEMENTED + TESTED offline): the dedicated
tables `emoji_categories` (`owner_id`, `name`, `created_at`, `updated_at`;
`UNIQUE (owner_id, name)`) and `emoji_mappings` (`owner_id`, `category_id`,
`simple_emoji`, `document_id`, `created_at`, `updated_at`;
`UNIQUE (owner_id, category_id, simple_emoji)`) — mapped mappings store a
REFERENCE (`document_id`) only, never an emoji definition. The
`db/client.py` layer enforces both uniqueness shapes and never upserts.
MANUAL-ONLY schema: documented in `IMPLEMENTATION_REPORT.md`, never
executed, no migration file created.

Schema/SQL files and `DATABASE_ARCHITECTURE.md` remain untouched by this
roadmap.

Status: **IN PROGRESS — library entries + categories + mappings + runtime
state IMPLEMENTED and TESTED offline (runtime state = Phase 3).**

---

## 8. Premium Emoji Library

- [x] Library storage (entries keyed by `(owner_id, document_id)` with alt text + provenance; set info deferred to §9 set resolution)
- [x] Deduplication on import (same `document_id` re-import is a counted no-op — never stored twice)
- [x] Library browser panel (Glass UI, 2×5 pagination over entries; shows only what the rows actually contain — alt text, document id, origin, added date)
- [x] Library is the single source of emoji definitions; categories only reference it (Phase 2: mappings persist `document_id` alone and resolve the entry at render/validation time — a missing entry is surfaced honestly)
- [ ] Library deletion semantics (completed for Phase 2 scope: mapping deletion and category deletion never touch the library; a mapping whose entry is missing renders honestly as unavailable — a dedicated library-entry-delete feature does not exist yet)

Unknown — requires implementation/investigation: whether alt text is
sufficient fallback when a viewer lacks the emoji set (Telegram behavior
suggests yes for custom emoji the viewer cannot render; verify live).

Status: **IN PROGRESS — storage + deduplication + browser panel
IMPLEMENTED/TESTED; deletion semantics still PLANNED.**

---

## 9. Collection/Set Import Flow

Implemented — message-level import (Phase 1,
`backend/services/emoji_library_service.py`, TESTED):

- [x] Collect custom-emoji records from the owner's **Saved Messages**
  through the self-client facade (`iter_messages("me", …)`, newest-first,
  exclusive `max_id` cursor, explicit scan/page/record/entity bounds) — an
  explicit deterministic import; no event listener, no "last message"
  inference
- [x] Recognize **entities, not text**: only `MessageEntityCustomEmoji`
  counts; plain Unicode emoji and unrelated entity types are never imported
- [x] Preserve `document_id` + the exact Unicode alt text of each span
  (Phase 0 UTF-16 helpers; corrupt spans fail closed as malformed)
- [x] Insert extracted emojis into the library idempotently
  (`(owner_id, document_id)` dedup; repeat import = counted no-op)
- [x] Honest import report as a service return value (counts, bounds, storage
  mode, error — a collection failure persists NOTHING)
- [x] Failure paths: message without a custom-emoji entity → skipped;
  malformed entity → counted, not stored; Telegram/timeout error → fail
  closed before any write; durable library read failure → abort before any
  scan

Still NOT implemented (this section remains open):

- [ ] Live-Telegram execution of the import (never run live)

IMPLEMENTED + TESTED (Phase 1 remainder,
`backend/telegram_api/custom_emoji.py` + the enrichment stage in
`emoji_library_service`):

- [x] Resolve each newly collected `document_id` into its actual custom-emoji
  document (`messages.GetCustomEmojiDocumentsRequest`, chunked, bounded,
  through the existing self client) and read its REAL
  `DocumentAttributeCustomEmoji` set identity — nothing about set membership
  is ever inferred or fabricated
- [x] Enumerate each unique set exactly once (`messages.GetStickerSetRequest`,
  one bound per unique identity, `hash=0` forces the full member list); the
  set's member documents (id + Unicode alt) enter the library through the
  SAME deduplication — importing one emoji of a collection imports the
  collection
- [x] Bounded enrichment: per-call/total document bounds, ≤20 unique sets,
  ≤200 members processed per set, a set-record budget, the same RPC timeout
  as the scan; each failure stage is distinguishable (document resolution /
  set resolution / set enumeration / persistence) and reported (`set_error`,
  `degraded`, `set_*` counters, `hit_set_limit`, `hit_set_member_limit`) —
  enrichment is explicitly BEST-EFFORT, never aborts the message-level
  import, and a set failure never writes partial silent state
- [x] Import report panel (Glass UI `😀 Emoji` panel → Import; the honest
  report renders straight from the service result — success, degraded and
  failed states all visible with the failure reason, no internals exposed);
  reached from the mother-menu `😀 Emoji` row, owner-authorized by the
  existing callback router

Constraints documented now:

- Import must reuse the Deep-Save-adjacent pattern of reading the owner's
  Saved Messages traffic from the **self-client** (the bot cannot see the
  owner's Saved Messages). — honored by Phase 1.
- Telegram may gate set enumeration for non-premium accounts; the owner's
  account owns the emojis in the intended usage, but the code path must fail
  closed with an honest report, not partial silent imports. — honored by
  Phase 1 (fail-closed reports; no partial silent imports).

Unknown — requires implementation/investigation: whether premium-only sets
enumerate without Premium on the querying account (live verification; the
fail-closed path covers the rejection honestly).

Status: **IN PROGRESS — message-level import + set resolution/enumeration +
report panel IMPLEMENTED/TESTED; live validation remains open.**

---

## 10. Category Model

- [x] Categories CRUD via Glass UI (create, rename, delete, list) with useful mapping counts shown where available (unknown-count state shown honestly when a durable count read fails)
- [ ] Built-in `Custom` category type (`is_custom=True`, §26) — later phase
- [ ] Active-category state (§13)
- [x] Category deletion semantics — mappings die with the category (removed BEFORE the category row, fail-closed); library entries survive
- [x] Same simple emoji may map differently per category (allowed); within one category a simple emoji is defined **exactly once** (enforced at `db/client.py` + `emoji_category_service` before any write)

Status: **IN PROGRESS — CRUD + deletion semantics + uniqueness IMPLEMENTED/TESTED (Phase 2); Custom type remains PLANNED.**

---

## 11. Mapping Model

- [x] Mapping create: pick simple emoji (text/keyboard input via existing input-state machinery) → pick premium emoji from library (2×5 browser)
- [x] Uniqueness: `(category_id, simple_emoji)` unique — enforced at service layer before write (and at the db layer as the backstop; never an upsert)
- [x] Mapping list/edit/remove panels per category (edit re-opens the same picker flow; a stale editor can never resurrect a deleted mapping)
- [x] Mappings store references to library entries (no emoji definitions duplicated — the mapping row holds `document_id` alone; validation resolves the entry against the owner's library)

Status: **IMPLEMENTED — offline only; live Telegram NOT exercised (Phase 2).**

---

## 12. Mapping Conflict Behavior

A duplicate mapping attempt (simple emoji already defined in the category)
must **never** silently overwrite. Required behavior:

- [x] Conflict UI panel showing **actual current and new premium emoji visuals** side by side (rendered from the resolved library entries — alt glyph + document id; an unresolvable entry is shown as explicitly unavailable, never fabricated; in-text custom-emoji rendering remains a Phase 4/§33 matter)
- [x] Choice buttons: **Replace** and **Cancel** (§34-H wording decided at implementation)
- [x] Cancel leaves the mapping untouched (plain panel navigation — no write path)
- [x] Replace overwrites only after explicit confirmation; the replace action re-proves the mapping still exists and the new library entry resolves (a stale panel never resurrects a deleted mapping)
- [x] No silent overwrite path exists anywhere (service-layer guard + UI guard + db-layer duplicate refusal; pinned by tests)

Status: **IMPLEMENTED — offline only; the visual fidelity of custom-emoji rendering inside panel text is NOT live-verified (§33).**

---

## 13. Active Category Behavior

**Decision (documented, derived from repository architecture): global default
+ optional per-chat override.**

Rationale from the codebase:

- The settings layer already implements exactly this split:
  `backend/services/settings_service.py` holds validated **global** settings
  (single `panel_settings` row, cached), while `backend/helper/
  target_context.py` and `backend/helper/context.py` carry **per-chat target
  context** through the panel system. RuntimeSupervisor and the router have
  no per-chat state store; introducing one is new surface and must be
  justified — hence the default is global, per-chat is an override, not a
  parallel authority.
- `backend/telegram_api/messages.py` resolves `chat_id | str` at call time —
  reconstruction already knows the destination chat, so applying a per-chat
  override at reconstruction time is a lookup, not new state.

Required behavior:

- [x] Global default category selectable in the Emoji panel (`emoji_replacement` panel → global picker; owner-scoped, live-category-validated)
- [x] Optional per-chat override (set/clear via panel while targeting that chat through the existing `target_context` machinery; without an armed target the panel explains and withholds the per-chat actions — never a fabricated chat id)
- [x] Resolution order at message time: per-chat override → global default → no replacement (`emoji_state_service.resolve_effective_category`, the Phase 4 boundary; the winning category is validated against the LIVE table so a deleted category fails closed to no-replacement instead of being substituted)
- [x] Documented in panel UI (which scope is in effect — global default, per-chat override, and the effective category are all rendered)

Alternative rejected for MVP: per-chat-only (would make replacement opt-in
per chat with no default) and strictly-global-only (conflicts with §C's
per-category expectations for different audiences). This decision is also
listed in §34 for confirmation.

Status: **IMPLEMENTED + TESTED (offline; §34-A owner sign-off still open).**

---

## 14. Emoji Replacement Toggle

- [x] Global `Emoji Replacement: ON/OFF` toggle (Glass UI `emoji_replacement` panel + persisted setting, §29; first-boot default OFF)
- [x] Independent from active category (toggle OFF ⇒ no processing regardless of category; toggle ON with no active category ⇒ no replacement — both pinned by tests)
- [x] OFF behavior contract at the resolution boundary: `resolve_effective_category` returns None whenever the toggle is OFF, so no downstream consumer can act (the actual untouched-message guarantee is completed by Phase 4's outgoing handler, which does not exist yet)
- [x] Toggle state visible in the Emoji panel header (`Replacement: ✅ ON / ❌ OFF`) and in the Replacement panel itself; Health/Context panel surfacing remains open
- [x] Toggle is owner-controlled only (owner-scoped service + the callback router's owner check)

Status: **IMPLEMENTED + TESTED (offline).**

---

## 15. Message Reconstruction Flow

First-class component. The original message is **NOT edited**.

```
owner sends outgoing message with emoji
  → self-client outgoing handler (owner-authored check §19)
  → toggle OFF?  → do nothing
  → structural emoji scan (codepoint-level, not regex-NL routing)
  → any mapped simple emoji present?
      no  → do nothing (message never touched)
      yes → transformer: rewrite ONLY mapped emojis (§16/§22)
          → reconstructor:
              1. delete original message via self-client
                 (owner's own message — deletable)
              2. helper bot sends a NEW message with the transformed
                 content to the SAME destination chat
          → loop guard marks/recognizes the bot's message (§24)
```

- [ ] Handler wiring: outgoing-only self-client handler (extend router registration; no new update loop)
- [ ] Determination of "has mapped emoji" BEFORE any destructive step (delete only after the new content is fully prepared)
- [ ] Atomic-ish sequencing with honest failure reporting (§28): if the bot send fails, the original must NOT be deleted first — send-then-delete or optimistic ordering with rollback semantics must be decided (§34)
- [ ] New message must carry the transformed entities (§17/§22)
- [ ] Reply threading: new message must reply to the same target the original replied to, where the destination permits (§23)

Ordering decision needed (§34): **send-new-first then delete-original**
(fails safe — worst case is a duplicated message) vs **delete-then-send**
(worst case is message loss). Default proposal: send-first, then delete;
on send failure, leave the original untouched and report the error to the
owner.

Status: **PLANNED**.

---

## 16. Emoji-Only Transformation Invariant

Strict product invariant: **ONLY emojis may change.**

Must be preserved as faithfully as Telegram allows:

- [ ] Ordinary text (byte-identical outside replaced emoji spans)
- [ ] Text order and whitespace (replacements are in-place span rewrites)
- [ ] All Telegram entities: bold, italic, underline, strikethrough, spoiler, code, pre, blockquote, text links, text mentions, custom-emoji entities already present in the original
- [ ] Entity offsets recomputed **only** where a replacement changes span length, never by naive re-chunking
- [ ] Captions (media captions carry their own entities — same rules apply)
- [ ] Media (message media untouched; transformation is text/entity-layer only)
- [ ] Reply information (new message replies to the original's reply target where possible, §23)
- [ ] No generic text rewrite; no global string replace (§4)

Implementation stance: operate on the parsed entity list (`text` +
`entities`) as the source of truth; a naive `str.replace` on message text is
expressly forbidden as the final architecture because it corrupts entity
offsets.

Known Telegram-level limits to be documented at implementation (not silently
dropped):

- Custom emoji entities (`MessageEntityCustomEmoji`) require the sender to
  have the right to use them (§28 capability constraint).
- Some properties are not transferable across a delete+re-send at all
  (e.g. exact original timestamp, message id, service metadata) — listed in
  §23 rather than silently changed.

Status: **PLANNED**.

---

## 17. Telegram Bot Bridge Requirements

**Capability constraint (verified against Telegram Bot API documentation):
"Custom emoji entities can only be used by bots that purchased additional
usernames on Fragment."** A plain bot account therefore **cannot** reliably
send arbitrary premium custom emojis via `MessageEntityCustomEmoji`, even
though it can send the entity structure. This is a hard Telegram-side
constraint that must be designed around, not ignored.

Consequences and open options (decision required, §34):

1. **Fragment-purchased bot username** — the clean path: if the helper bot
   (or a dedicated bridge bot) purchases an additional Fragment username, it
   may use custom-emoji entities. Cost/ownership decision belongs to the
   owner.
2. **Premium-flag path via TL (unverified)** — whether a bot account linked
   differently, or the self-client (user account with Premium) relaying
   through some mechanism, can carry premium emoji is **Unknown — requires
   implementation/investigation**. No path may be assumed.
3. **Graceful degradation** — if the bridge bot lacks the capability, the
   reconstructor must fall back to sending the emoji's **alt text** (a normal
   glyph) instead of silently dropping or erroring; the owner must be able
   to see which mode is active.

Bridge requirements:

- [x] Bridge sender = the existing helper bot client (`backend/helper/client.py`) — implemented in `backend/telegram_api/bridge.py`; no dedicated second bot token added (§34-C default honored)
- [x] Bridge has NO update loop, NO handler registration (send-only by construction)
- [x] Bounded-timeout send path consistent with `backend/telegram_api` conventions (`guarded_await`, normalized exceptions) — unit-tested
- [ ] Rate/flood handling (Telethon `flood_sleep_threshold` already configured on helper; verify adequacy under real replacement traffic)
- [x] Destination resolution identical to the original message's chat (§18) — peer resolved through the SELF client, unit-tested; live delivery NOT verified

Status: **IN PROGRESS — send-only bridge module IMPLEMENTED and unit-TESTED
(`backend/telegram_api/bridge.py`, `tests/test_bridge_delivery.py`); live
delivery NOT verified; capability decision (§34-D) still OPEN.**

---

## 18. Same-Destination Delivery

- [ ] New message is sent to the exact chat the original lived in (peer id preserved from the outgoing event)
- [ ] Destination types must be enumerated at implementation: private chats, groups, supergroups, channels (owner-posted), Saved Messages itself — Unknown — requires implementation/investigation for bot send-permission differences per type (e.g. bot must be a member; channels need post rights)
- [ ] Reply-to preserved: new message replies to the original's `reply_to_msg_id` when present (and when the bot can reply in that chat)
- [ ] Media messages: bot re-sends media with caption entities transformed (§23) — mechanism (re-upload via bot vs URL/file handoff) decided at implementation
- [ ] Silent/notification behavior decided at implementation (default: normal send, no silent flag)

Status: **PLANNED**.

---

## 19. User-Authored-Message Authorization Boundary

- [ ] Replacement processes ONLY messages authored by the owner (`msg.out is True` on the self-client, matching the existing outgoing-only handler pattern in `backend/bot/handlers/ai_unified.py` + `is_owner`)
- [ ] Messages from other users are never transformed, deleted, or touched
- [ ] The bot bridge sends only to destinations where the owner authored the original message; it never initiates sends on its own
- [ ] Owner-only configuration surface (all panels behind `is_owner`)

Note: deletion of the original is possible because the self-client deletes
its own message; bot accounts cannot delete other accounts' messages — this
aligns naturally with the boundary above.

Status: **PLANNED**.

---

## 20. Multi-Emoji Replacement

- [ ] All mapped simple emojis in one message are transformed in the SAME reconstruction (one delete + one send, never N sends)
- [ ] Example contract: `🫪 hello 🗣️ 👋` with 🫪→X and 🗣️→Y mapped ⇒ X hello Y 👋 (👋 untouched if unmapped)
- [ ] Overlapping/adjacent emoji handling defined at implementation (codepoint spans are disjoint by construction; verify with combined sequences/ZWJ emojis — Unknown — requires implementation/investigation for grapheme-cluster vs codepoint spans)
- [ ] Same simple emoji appearing multiple times maps consistently in one message

Status: **PLANNED**.

---

## 21. Unmapped Emoji Behavior

- [ ] Unmapped emojis (any emoji not in the active category's mapping table) pass through byte-identical, including their entity spans
- [ ] No logging/telemetry requirement to record unmapped emojis (keep behavior silent by default)
- [ ] Mixed mapped+unmapped in one message handled per §20

Status: **PLANNED**.

---

## 22. Formatting/Entity Preservation

The `backend/telegram_api/_helpers.py::serialize_message` entity gap
(described in §5.4) was the prerequisite for this invariant. Status:

- [x] Entity-aware message representation — IMPLEMENTED in `backend/telegram_api/_helpers.py`: `serialize_message` now emits an `entities` list (UTF-16 offsets/lengths as Telegram sent them, plus url / document_id / user_id / language payload), `utf16_length` / `utf16_offset` / `utf16_index_at` helpers, and `dict_entities_to_tl` (dict → TL rebuild for sending; unknown types and missing payloads raise, never silently dropped). Pinned by `tests/test_bridge_delivery.py`
- [ ] Transformer operates on (text, entities) tuples; produces new (text, entities) with correct UTF-16 offsets after span replacement
- [ ] Unit tests with mixed scripts (emoji + Persian + Latin) pinning offset correctness — see §30
- [ ] Existing `font_style.py` already solves UTF-16-width issues for the Glass UI font (see `backend/helper/font_style.py`); reuse its analysis where applicable rather than duplicating

Status: **IN PROGRESS — serialization prerequisite IMPLEMENTED + TESTED; the
transformer itself remains PLANNED.**

---

## 23. Media/Caption Handling

- [ ] Text-only messages: covered by §15–§22
- [ ] Media + caption: transform caption entities; media itself re-sent unchanged by the bridge (mechanism TBD at implementation: bot re-upload from downloaded buffer — reusing the Deep-Save download/upload machinery pattern in `backend/services/save_service.py` — vs. passing a Telegram file reference; Unknown — requires implementation/investigation for cross-account file reference reuse)
- [ ] Media without caption: replacement applies only if the message contains transformable text — none ⇒ message untouched (no point reconstructing)
- [ ] Properties NOT preservable through delete+bot-re-send (documented limitation, not silent): original timestamp, original message id, service-side metadata (e.g. via-inline-bot origin), possibly view counts in channels; the roadmap records this so the product decision is explicit
- [ ] Reply threading preservation where the destination permits (§18)

Status: **PLANNED**.

---

## 24. Loop Prevention

First-class requirement: the bot's replacement message must never be
reprocessed.

- [ ] Marking strategy decided and implemented (candidates: prefix/sentinel marker entity, message metadata, or — preferred — structural recognition: the bridge's own messages are recognizable by sender_id == helper bot id in the destination chat; the self-client handler must skip messages whose sender is the bridge bot)
- [ ] Sender-based suppression: `sender_id == helper.get_bot_id()` ⇒ never processed (mirrors how the codebase already distinguishes helper/self identities)
- [ ] Marker/sentinel option evaluated against visibility cost (a visible marker would leak the mechanism to other chat members — likely unacceptable; structural sender check preferred)
- [ ] Loop test in the test plan (§30): bot message must not trigger a second reconstruction
- [ ] Also covers the self-client's own echo of the bot's message (it arrives as an incoming event for the self-client — not outgoing — so the outgoing-only handler structurally ignores it; keep this as an asserted invariant)

Status: **PLANNED**.

---

## 25. Pagination UI (2×5)

- [x] Library browser renders **2 columns × 5 rows = 10 items per page** over library entries (`backend/bot/handlers/emoji.py`, standard panel machinery)
- [x] Navigation row: `[ ◀ ] [ page/pages ] [ ▶ ]` with page-clamping on a shrunk/grown library; entry detail reached via `panel:emoji_entry:<page>:<idx>`
- [x] Callback data scheme reuses existing `panel:*` conventions with page parameters; `truncate_callback_data` already bounds the data (asserted ≤64 bytes in tests)
- [x] Edit-in-place panel updates (no message spam — `AGENTS.md` §13 rule 4)
- [x] Mapping list pagination — Phase 2 (2×5, deterministic offset slicing, clamped pages, callback-bounded)
- [x] 10-per-page pagination applies to: library entries (done), mappings list (Phase 2 done), categories list (Phase 2 done), the picker reusing the library browser, and collection import results (the import is a report panel, not a paged list)

Status: **IN PROGRESS — library, categories, mappings and picker pagination IMPLEMENTED/TESTED; pagination UI is NOT live-verified.**

---

## 26. Custom Category

- [ ] `Custom` category composes mappings **reused from existing categories** (e.g. 🫪→Premium X from 002, 🗣️→Premium B from Aya)
- [ ] Composition stores **references** (category_id + simple_emoji, or the underlying library entry) — never duplicates premium emoji definitions
- [ ] Conflict rules inside Custom mirror §12 (one definition per simple emoji; duplicates get the conflict UI)
- [ ] UI: pick source category → pick mapped simple emoji → confirm; composition surface distinguishes library ownership/reference vs category mapping vs custom composition (§7's three concerns)
- [ ] Source-category mapping changes propagate or are snapshotted — decision required (§34; default proposal: snapshot-on-compose to avoid surprising behavior changes, with an explicit refresh action)

Status: **PLANNED**.

---

## 27. Reaction Subsystem

**Clean separation (requirement L):** message emoji replacement (§6–§26) and
reactions are **different subsystems**. Replacement rewrites outgoing
message content via reconstruction; reactions are Telegram reactions applied
to messages. They share no state, no code path, and no UI panel.

What exists today: **nothing** — zero reaction code in the repository
(verified; §2). The full TL surface ships in Telethon 1.34.0
(`messages.SendReactionRequest`, `ReactionEmoji`, `ReactionCustomEmoji`,
`MessageReactions`, `AvailableReaction` — verified in the installed venv).

Planned additions (no implementation yet):

- [ ] `backend/telegram_api/reactions.py` — typed `SendReactionRequest` wrapper (bounded timeout, normalized exceptions, plain-dict result — consistent with `backend/telegram_api/messages.py` conventions)
- [ ] `backend/services/reaction_service.py` — business logic (react to a message with a chosen emoji; custom-emoji reaction via `ReactionCustomEmoji(document_id)`)
- [ ] Glass UI action: reply-to-message → Emoji panel → React (uses the existing reply-target machinery the Save panel uses)
- [ ] Capability notes: custom-emoji reactions require the reacting account (the self-client) to have the emoji available; reaction availability per chat (`can_react`/`AvailableReaction` flags) should be surfaced honestly in errors
- [ ] Reactions apply to messages the owner chooses (including others' messages — reacting is not authorship); this does NOT conflict with §19 (§19 governs content transformation only)
- [ ] No auto-reaction, no scheduled reactions (deferred, §4)

Status: **PLANNED — subsystem boundary documented; zero existing code**.

---

## 28. Error / Failure / Capability Handling

Capability constraints (documented, not ignored):

| Constraint | Source | Handling |
|---|---|---|
| Bots cannot use custom-emoji entities unless they purchased additional usernames on Fragment | Telegram Bot API (verified) | §17: Fragment-purchased username for the bridge bot, or graceful alt-text degradation; capability mode must be visible to the owner |
| Bot must be able to send in the destination (membership, send rights, channel post rights) | Telegram platform | §18: destination-permission check before deleting the original (contributes to send-first ordering, §15) |
| Premium/custom emoji availability on the owner's account | Telegram Premium | Import paths fail closed with honest reports (§9) |
| Deletion rights: self-client deletes only its own messages; bots delete none of others' | Telegram platform | §19 boundary is structurally enforced |

Failure-handling requirements:

- [ ] Send-first-then-delete ordering (§15 proposal) so a bot-send failure never destroys the original
- [ ] Every Telegram/DB call bounded and normalized (existing `guarded_await` + `TelegramAPIError` conventions)
- [ ] Honest result strings (no fake success) — mirrors `execute_save` honesty rules
- [ ] Flood/rate errors surfaced to the owner; no silent drops
- [ ] DB unavailability: feature state follows the established in-memory-fallback degradation contract (`AGENTS.md` §8) with visible degradation labeling where state is displayed

Status: **PLANNED**.

---

## 29. Configuration / State Persistence

Follows existing patterns (no schema invented here; actual SQL manual, later):

- [x] Toggle + global default persisted (Phase 3: the single-row-per-owner `emoji_state` table following the `bio_state`/`db/client.py` pattern — validated writes, honest failure reporting, in-memory fallback; the bridge config remains open until Phase 4 decides it)
- [x] Library persisted via the `db/client.py` Supabase-or-in-memory-fallback pattern (Phase 1: `emoji_library` functions; a failed durable dedup read returns `None` so import fails closed — RAM is never silently presented as durable; physical table MANUAL-ONLY, schema in `IMPLEMENTATION_REPORT.md`)
- [x] Categories/mappings persisted via the same pattern (Phase 2: dedicated `emoji_categories` / `emoji_mappings` tables following the identical Supabase-or-fallback sync pattern SAME as the library; my durable-write failure paths report None/-1/False so the service can fail closed and the UI shows honest unknown/degraded states; creation of the physical tables is manual/out of scope here)
- [x] Durable-vs-fallback behavior stays honest (Phase 3: a failed durable write reports False and the UI renders nothing-changed; a failed read reports unset so replacement fail-closes OFF; no RAM state is ever presented as durable) — full `config_store`-style labeling keys remain unused
- [ ] New env vars (if any, e.g. a dedicated bridge bot token) follow `config.py` optional-var conventions; default must keep the feature disabled/inert
- [x] Feature flag default: Emoji Replacement OFF on first boot (Phase 3; pinned by tests)

Status: **IN PROGRESS — Phase 3 state persistence IMPLEMENTED/TESTED; bridge config + env vars remain with later phases.**

---

## 30. Test Plan

Follows existing test conventions (`tests/`, pytest, real Dispatcher/
registry/executor patterns like `tests/test_provider_tool_boundary.py` and
`tests/test_semantic_intent_boundary.py`; no live Telegram in tests):

- [x] **Phase 0 slice (done):** `tests/test_bridge_delivery.py` — 22 tests pinning UTF-16 helpers (incl. mid-surrogate `ValueError`), entity serialization round-trip, dict→TL rebuild (incl. fail-closed unknown/missing payload and mention-name resolution through the target client), bridge same-destination send via the helper bot, send-failure honesty, empty-message refusal, unavailable-bot error, timeout normalization. Full suite at this commit: **5180 passed, 26 skipped** (baseline at the merged remote state: 5158 passed — no regressions).
- [x] **Phase 1 slice (done):** `tests/test_emoji_library_import.py` — 50 tests pinning extraction from the Phase 0 serialized representation (valid records, UTF-16 alt preservation incl. Persian/supplementary text, plain-Unicode + unrelated-entity exclusion, malformed document-id/offset/span fail-closed, entity processing bound), import semantics (first import, repeat idempotency, in-scan + durable dedup, call-by-call deterministic `max_id` pagination, scan/record/entity bounds, empty history, inaccessible entries), failure honesty (collection error, page timeout, durable-read abort before scan, insert-failure counts, completeness invariant), persistence (in-memory fallback + faked Supabase path, uniqueness, pagination), context isolation (AST import audit — no `backend.ai`), architecture constraints (no second client/loop/scheduler/executor, no forwarding, exact signature), and a Phase 0 serialization round-trip.
- [x] **Phase 1 remainder (done):** `tests/test_emoji_set_enumeration.py` — 27 tests pinning the facade (`{document_id, alt, set}` resolution through REAL Telethon TL types incl. id/short-name identities, honest absence/setless/unattributed documents, invalid-id dropping + per-call clamping, error/timeout normalization, set-member dict shape, unusable-identity rejection) and the service enrichment (chunked one-batch resolution, one enumeration per shared set, duplicate classification vs scan/library/members, unresolved/setless counters, failed-vs-clean stages, ≤20-set and ≤200-member bounds, set-record budget, first-appearance-order determinism, and the full-import integration: scan records + deduplicated set members persisted, repeat-run dedup incl. the no-new-candidates path, degraded report on enumeration failure, unresolved documents, insert-failure visibility, and fail-closed scan abort). `tests/test_emoji_ui.py` — 18 tests pinning the Glass UI (registration, mother-menu link, bounded callback data, main-panel counters, 2×5 browser + second page + page clamp + empty/alt-fallback honesty, entry detail incl. honest set-scan marking and stale-index failure, import action over the REAL service with success/degraded/no-client honesty, report renderer failure/budget lines, and a no-second-infrastructure source scan).
- [ ] Transformer unit tests: emoji-only rewrite; entity offsets (UTF-16) preserved/recomputed correctly across mapped spans; mixed-script cases (emoji + Persian + Latin); unmapped passthrough; multi-emoji single-pass (§20/§21/§22)
- [ ] Reconstruction flow tests: fake self-client + fake bridge client; assert delete called only after prepared send succeeds; same-destination assertion; reply-to propagation (§15/§18)
- [ ] Authorization tests: non-owner-authored message never processed; incoming events never processed (§19)
- [ ] Loop-prevention tests: bot-sender message never reprocessed (§24)
- [ ] Mapping service tests: uniqueness enforcement; conflict detection returns current+new visuals data; replace only on explicit confirm (§11/§12)
- [x] **Phase 3 slice (done):** `tests/test_emoji_state_phase3.py` — 46 tests pinning the state layer (first-boot default OFF; toggle ON/OFF persist-and-load through the db abstraction; flip semantics; invalid-owner refusal; global default set/nonexistent-rejected/foreign-rejected/cleared; per-chat override set/cleared/nonexistent-rejected/foreign-rejected/per-chat independence; resolution order override > global > none; fallback after clear; no-state → none; OFF ⇒ no resolution despite categories; global used in other chats and for unknown chats; deleted global AND override category fail closed; owner isolation on every read/write; degraded write → False + state unchanged, degraded read → unset/OFF; UI registration, main-panel state line, full-state + effective rendering, no fabricated target chat + per-chat actions withheld without one, honest OFF effective-none, toggle/choose/clear actions mutate only their intended scope, foreign-category choose rejected, owner-scoped picker, empty-library prompt, ≤64-byte callback bounds, engine-owner scoping; architecture: AST import audit (no `backend.ai`), no second client/loop/scheduler/executor/forwarding/events.NewMessage, no Phase 4 surface (transform/reconstruct/delete_original/bridge_send), Telegram-free resolution)
- [x] Category resolution tests: override > default > none (§13)
- [x] Toggle tests: OFF ⇒ no replacement at the resolution boundary (§14; the zero-Telegram-calls form is completed with Phase 4's outgoing handler)
- [x] Import tests: set enumeration covered offline at the fake TL boundary (facade + service + full-import integration — see the Phase 1 remainder line above; live Telegram remains untested)
- [x] **Phase 2 slice (done):** `tests/test_emoji_category_service.py` — 54 tests pinning the service layer (category validation incl. strip/empty/newline/length, create/list-with-counts/rename/delete, duplicate-name refusal, same-name-across-owners, owner isolation on every category/mapping operation, mapping creation with owner-scoped library resolution, reference-only storage, missing/foreign library rejection, list/edit/delete mappings, same-emoji-different-categories, same-document-multiple-mappings, conflict result carrying current+current_entry+new_entry, unresolvable current entry reported not fabricated, db-layer uniqueness backstops, pagination slicing). `tests/test_emoji_ui_phase2.py` — 42 tests pinning the Glass UI (8 panels + 10 actions registration, mother-menu Categories row, categories 2×5 grid with mapping counts + page clamp, mapping-count unknown-state honesty, category detail with stale/delete failure honesty, mappings list with visuals + pagination + remainder page, mapping detail incl. honest missing-entry marking, the add-mapping draft/input/picker flow, stale draft + stale index + deleted-entry picker failures, the conflict panel (current/new visuals, nothing-overwritten, explicit Replace re-proves state, Cancel keeps, replace-without-draft refused, replace-after-mapping-deleted honest, unresolvable-current-visual honest), edit/delete flows, delete-category flow incl. confirm-without-draft, owner isolation at the UI, ≤64-byte callback bounds across all new panels, and the architecture pins: no `backend.ai` import, no `events.NewMessage`, no `create_task`, no forwarding). At this commit: **5371 passed, 26 skipped** (Phase 1 baseline 5275 + 96 new).
- [x] Pagination tests: 10-per-page slicing, page clamp, callback-data bound, second-page remainder (§25 library browser — plus Phase 2: categories grid, mappings list, picker, in `tests/test_emoji_ui_phase2.py`)
- [ ] Custom composition tests: references not duplicates; propagation semantics per §26 decision
- [ ] Reaction wrapper tests: bounded timeout, exception normalization, payload correctness (§27)
- [x] Full-suite gate: keep the whole suite green on every slice (latest run at this commit: **5417 passed, 26 skipped** — Phase 2 baseline 5371 + 46 new)

Status: **IN PROGRESS — Phases 0–3 done (offline); the rest remains PLANNED.**

---

## 31. Validation Plan

Beyond unit tests — verification against real behavior before IMPLEMENTED
can become TESTED/VERIFIED:

- [x] Compile check (`compileall`/`py_compile`) + full test suite green — at this commit: `py_compile` clean on all changed modules, **5417 passed, 26 skipped**
- [ ] Live Telegram validation checklist (manual, owner's environment): import a real collection via Saved Messages (incl. set enumeration through real TL RPCs); map 2 emojis; verify replacement in a private chat, a group, and Saved Messages; verify text/entities/media/caption/reply preservation visually; verify unmapped emoji untouched; verify loop does not occur; verify toggle-OFF leaves everything untouched
- [ ] Bridge capability verification: confirm which custom-emoji send mode actually works with the owner's bot setup (Fragment username vs alt-text fallback) — record the outcome in §17
- [ ] Failure-mode validation: revoke bot send permission in a chat and verify the original message survives (send-first ordering)
- [ ] No architectural drift audit: grep-style confirmation that no second executor/scheduler/update loop and no keyword/regex routing were introduced
- [ ] Documentation consistency: `AGENTS.md`/`IMPLEMENTATION_REPORT.md` updated only when the feature actually lands (not by this roadmap task)

Status: **PLANNED**.

---

## 32. Implementation Phases

Phase order (each phase independently verifiable; nothing is complete until
its checkboxes AND validation pass):

- [ ] **Phase 0 — Prerequisites:** entity-aware serialization (§22 gap in `_helpers.py`) — **DONE + TESTED**; decision on bridge capability (§34-D) — still OPEN (owner); mapping persistence shape (§34-E) — implemented against the default proposal in Phase 2, owner sign-off still OPEN
- [x] **Phase 1 — Library & Import:** domain model (§7 library-entry shape), library storage (dedup on `(owner_id, document_id)`), bounded entity-based Saved Messages collection import (§8, §9 message-level), honest import report, sticker-set resolution/enumeration (§9), Glass UI import/report + library-browser panels, 2×5 library pagination (§25) — **IMPLEMENTED + TESTED**; REMAINING: live validation only
- [x] **Phase 2 — Categories & Mappings:** category CRUD (create/list/rename/delete with mapping counts), mapping editor (simple-emoji input → 2×5 library picker), uniqueness at service + db layers, conflict UI (current-vs-new visuals, explicit Replace/Cancel), per-category mapping list/edit/delete with 2×5 pagination, delete-category cascade — **IMPLEMENTED + TESTED (offline)**; REMAINING: live validation only
- [x] **Phase 3 — State & Toggle:** replacement toggle, active category (global default + per-chat override), deterministic resolution boundary, `emoji_state`/`emoji_chat_overrides` persistence, Glass UI state panel (§13, §14, §29) — **IMPLEMENTED + TESTED (offline)**; REMAINING: live validation only
- [ ] **Phase 4 — Reconstruction:** transformer (entity-safe), reconstructor with send-first ordering, bot bridge send path, same-destination + reply preservation (§15–§23), loop prevention (§24)
- [ ] **Phase 5 — Custom Category:** composition UI + reference semantics (§26)
- [ ] **Phase 6 — Reactions:** `telegram_api/reactions.py` wrapper, reaction service, Glass UI react action (§27)
- [ ] **Phase 7 — Hardening:** error/capability handling completion (§28), test plan completion (§30), validation checklist execution (§31), documentation updates (`AGENTS.md`, `IMPLEMENTATION_REPORT.md`) in the landing commit(s)

Status: **IN PROGRESS — Phase 0 (code half) + Phases 1–3 done offline; live validation and Phases 4–7 remain PLANNED.**

---

## 33. Dependencies / Blockers

Dependencies (already present — verified):

- Telethon 1.34.0 (full reaction + custom-emoji TL surface) — verified in venv
- Helper bot client (`backend/helper/client.py`) — the bridge's foundation
- Glass UI panel system + input-state + callback machinery — all UI needs
- `db/client.py` / `config_store.py` / `settings_service.py` patterns — persistence
- Existing test infrastructure (203 test files, real-component patterns)

Blockers / risks:

1. **Bridge capability (BLOCKED pending decision):** bot custom-emoji sending
   requires a Fragment-purchased additional username (verified Bot API
   constraint). Until the owner decides (§34-D), Phase 4's premium-visual
   delivery mode is BLOCKED; alt-text fallback is the unblocked fallback.
2. **Destination permissions (investigation needed):** bot send rights per
   chat type — Unknown — requires implementation/investigation.
3. **Set enumeration without Premium (investigation needed):** whether
   collection import works on a non-premium querying session for
   premium-gated sets — Unknown — requires implementation/investigation.
4. **Schema work is manual:** new tables require owner-run Supabase
   migrations (out of repo scope); in-memory fallback covers development.
   Phase 2 code now depends on `emoji_categories` / `emoji_mappings` with
   the documented unique indexes — until the owner applies the manual
   schema, a configured Supabase reports write failures honestly and the
   feature runs on the in-memory fallback (same contract Phase 1
   established for `emoji_library`).
5. **Cross-account media re-send mechanism** (§23) — Unknown — requires
   implementation/investigation.

Status: **PLANNED — dependencies verified; blockers documented**.

---

## 34. Decisions That Still Require Confirmation

| # | Decision | Options | Default proposal | Owner input needed |
|---|---|---|---|---|
| A | **Active category scope** | global-only / per-chat-only / global default + per-chat override | global default + per-chat override (§13, derived from existing settings/target-context architecture) | **IMPLEMENTED at Phase 3 (default proposal, as documented):** global default + per-chat override with the override as an override, never a second authority. Owner confirmation still OPEN — reverting would change only the service/UI surface, not the db pattern |
| B | **Reconstruction ordering** | send-new-then-delete / delete-then-send | send-first (never lose the original; worst case temporary duplicate) (§15) | Confirm |
| C | **Bridge bot identity** | reuse helper bot (`BOT_TOKEN`) / dedicated second bot token | reuse helper bot; env override for a dedicated token (§17/§29) | Confirm |
| D | **Premium-visual delivery mode** | Fragment-purchased additional username for the bridge bot / alt-text fallback only / investigate other TL paths | design for both: attempt custom-emoji entities, degrade to alt text; Fragment purchase is the owner's cost/ownership decision (§17/§28) | Required — capability + budget decision |
| E | **Mapping persistence shape** | new dedicated tables / extend an existing generic store | dedicated tables following `db/client.py` patterns (§7/§29); schema work is manual and out of scope here | **PARTIALLY RESOLVED at Phase 2 (implementation, not owner sign-off):** Phase 2 was implemented against the default proposal — dedicated `emoji_categories` / `emoji_mappings` tables following the `db/client.py` pattern. The owner never confirmed this decision; the status stands as “default proposal implemented, awaiting confirmation”. If the owner later chooses differently, the db-layer functions are the only churn surface (the service/UI contract stays). Physical schema is MANUAL-ONLY, documented in `IMPLEMENTATION_REPORT.md`, never executed |
| F | **Custom composition semantics** | live-reference (follows source category changes) / snapshot-on-compose | snapshot-on-compose with explicit refresh action (§26) | Confirm |
| G | **Loop-prevention mechanism** | sender-id structural check / visible sentinel marker / both | sender-id check (invisible to other chat members) (§24) | Confirm |
| H | **Conflict UI wording** | exact button/panel labels | decided at implementation (§12) | **RESOLVED at Phase 2 implementation:** panel “Mapping Conflict”, buttons **Replace** / **Cancel** (was listed Optional) |
| I | **Notification behavior of replacement messages** | normal / silent | normal (§18) | Optional |

No decision above may be resolved silently during implementation without
updating this section.

Status: **OPEN — awaiting confirmation**.

---

## 35. Not Implemented Yet

Current truth after the Phase 0, Phase 1, and Phase 2 code slices:

- Entity-aware serialization + UTF-16 helpers + dict→TL entity rebuild —
  IMPLEMENTED (`backend/telegram_api/_helpers.py`), TESTED.
- Send-only Bot bridge module — IMPLEMENTED (`backend/telegram_api/bridge.py`),
  unit-TESTED; **live Telegram delivery NOT verified**.
- Emoji Library + deterministic Saved Messages import — IMPLEMENTED
  (`backend/services/emoji_library_service.py` + `emoji_library` functions
  in `backend/db/client.py`), TESTED (50 tests); **live Telegram import NOT
  verified**; the physical `emoji_library` table does NOT exist anywhere yet
  (MANUAL-ONLY schema documented in `IMPLEMENTATION_REPORT.md` — nothing
  executed).
- Sticker-set resolution/enumeration — IMPLEMENTED
  (`backend/telegram_api/custom_emoji.py` + the enrichment stage in
  `backend/services/emoji_library_service.py`), TESTED (27 tests); bounded,
  best-effort, honest degradation; **live Telegram set enumeration NOT
  verified**.
- Categories & mappings (Phase 2) — IMPLEMENTED
  (`backend/services/emoji_category_service.py` + `emoji_categories` /
  `emoji_mappings` functions in `backend/db/client.py` + the panels in
  `backend/bot/handlers/emoji.py`), TESTED (96 tests across service + UI);
  **live Telegram NOT exercised**; the physical tables do NOT exist anywhere
  yet (MANUAL-ONLY schema documented in `IMPLEMENTATION_REPORT.md` — nothing
  executed).
- Replacement state & toggle (Phase 3) — IMPLEMENTED
  (`backend/services/emoji_state_service.py` + the `emoji_state` /
  `emoji_chat_overrides` functions in `backend/db/client.py` + the
  Replacement panels in `backend/bot/handlers/emoji.py`), TESTED (46
  tests); **live Telegram NOT exercised**; the physical tables do NOT
  exist anywhere yet (MANUAL-ONLY schema documented in
  `IMPLEMENTATION_REPORT.md` — nothing executed).
- Still NOT implemented: transformer, reconstructor, reaction code; no
  live schema/tables applied; §34-D and the owner sign-offs on §34-A
  (Phase 3 scope, implemented as documented) and §34-E remain open.

---

*Roadmap established from repository investigation at commit `eb4d852`
(branch `main` worktree, Telethon 1.34.0 verified in the active venv);
Phase 0 code slice landed on top of the merged `0c60363` state (see
`IMPLEMENTATION_REPORT.md`). Document will be reset and replaced when the
Emoji & Reaction feature completes.*
