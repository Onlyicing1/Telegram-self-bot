# Emoji & Reaction Feature Roadmap

> **Current-feature execution roadmap.** This file is NOT a changelog and NOT a
> permanent historical record. It contains only the roadmap and current
> implementation status for the feature currently being developed: **Emoji &
> Reaction**. When this feature is fully completed, this file will be reset and
> completely replaced with the next feature's roadmap. It never accumulates
> multiple unrelated features.
>
> **Status of this document:** DESIGN / ROADMAP ONLY. Nothing in this feature
> has been implemented. No application code, no tests, no migrations, and no
> database schema changes were made to deliver this roadmap. Everything below
> marked PLANNED is a design commitment, not completed work.

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

**Overall feature status: PLANNED — nothing implemented.**

The repository currently contains **no** emoji-substitution, premium-emoji
library, or reaction feature (verified by grep across `backend/` and `tests/`
for `reaction`, `custom_emoji`, `MessageReactions`, `ReactRequest` — zero
functional hits; the only "emoji" mentions are unrelated tokenization/
character-width logic in `backend/ai/preparation_policy.py`,
`backend/ai/semantic_delete.py`, `backend/ai/tools/delivery.py`,
`backend/helper/font_style.py`, `backend/bot/handlers/ai.py`,
`backend/ai/confirmation.py`).

- [ ] Premium emoji library
- [ ] Collection import
- [ ] Category management
- [ ] Mapping UI
- [ ] Conflict handling
- [ ] Replacement toggle
- [ ] Message reconstruction
- [ ] Entity preservation
- [ ] Bot bridge
- [ ] Loop prevention
- [ ] Custom category
- [ ] Reactions
- [ ] Tests
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
  dict. **Gap:** it serializes `id/chat_id/sender_id/text/date/has_media/
  reply_to_msg_id/out` and **drops entities entirely**. The §22 invariant
  requires an entity-aware representation before any reconstruction logic is
  written.
- No reaction or custom-emoji wrapper exists in `backend/telegram_api/`.

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
  `bot_logs`; `get_next_save_code()` is atomic; heavy calls run via
  `asyncio.to_thread` with bounded timeouts; the bot never crashes on DB
  errors.
- `backend/ai/config_store.py` — `ai_config` table; documented degradation
  contract (`DEGRADED_READ_KEY`, `SESSION_ONLY_KEY`) distinguishing durable
  rows from in-memory fallback values.
- `backend/services/settings_service.py` + `backend/services/
  panel_settings_repository.py` — validated settings keys persisted as a
  single `panel_settings` row (`key="global"`), cached in-process.
- **No emoji/reaction table or schema exists.** Schema design is §7; SQL is
  explicitly out of scope for this task (manual Supabase work later).

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

Schema/SQL and `DATABASE_ARCHITECTURE.md` remain untouched by this roadmap.

Status: **PLANNED**.

---

## 8. Premium Emoji Library

- [ ] Library storage (entries keyed by `document_id`, with set + alt text)
- [ ] Deduplication on import (same `document_id` re-import is a no-op or update, never a duplicate)
- [ ] Library browser panel (Glass UI, 2×5 pagination over entries)
- [ ] Library is the single source of emoji definitions; categories only reference it
- [ ] Library deletion semantics (what happens to mappings referencing a deleted entry — must be defined, see §34)

Unknown — requires implementation/investigation: whether alt text is
sufficient fallback when a viewer lacks the emoji set (Telegram behavior
suggests yes for custom emoji the viewer cannot render; verify live).

Status: **PLANNED**.

---

## 9. Collection/Set Import Flow

- [ ] Detect a premium/custom emoji sent by the owner **to Saved Messages** (self-client outgoing event, owner-only)
- [ ] Resolve the emoji's **sticker set / collection** (Telegram API: fetch the set by the document's `DocumentAttributeCustomEmoji` reference)
- [ ] Enumerate all documents in the set and extract their `document_id`s + alt text
- [ ] Insert extracted emojis into the library (idempotent)
- [ ] Import report panel (collection name, count, library size)
- [ ] Failure paths: message has no custom-emoji attribute; set fetch fails; set is unavailable/premium-gated

Constraints documented now:

- Import must reuse the Deep-Save-adjacent pattern of reading the owner's
  Saved Messages traffic from the **self-client** (the bot cannot see the
  owner's Saved Messages).
- Telegram may gate set enumeration for non-premium accounts; the owner's
  account owns the emojis in the intended usage, but the code path must fail
  closed with an honest report, not partial silent imports.

Unknown — requires implementation/investigation: exact Telethon call chain
for set enumeration (`messages.GetStickerSetRequest` vs
`account.GetStickerSetRequest`) and whether premium-only sets enumerate
without Premium on the querying account.

Status: **PLANNED**.

---

## 10. Category Model

- [ ] Categories CRUD via Glass UI (create, rename, delete, list)
- [ ] Built-in `Custom` category type (`is_custom=True`, §26) — later phase
- [ ] Active-category state (§13)
- [ ] Category deletion semantics (mappings die with the category; library entries survive)
- [ ] Same simple emoji may map differently per category (allowed); within one category a simple emoji is defined **exactly once** (enforced, §11/§12)

Status: **PLANNED**.

---

## 11. Mapping Model

- [ ] Mapping create: pick simple emoji (text/keyboard input via existing input-state machinery) → pick premium emoji from library (2×5 browser)
- [ ] Uniqueness: `(category_id, simple_emoji)` unique — enforced at service layer before write
- [ ] Mapping list/edit/remove panels per category
- [ ] Mappings store references to library entries (no emoji definitions duplicated)

Status: **PLANNED**.

---

## 12. Mapping Conflict Behavior

A duplicate mapping attempt (simple emoji already defined in the category)
must **never** silently overwrite. Required behavior:

- [ ] Conflict UI panel showing **actual current and new premium emoji visuals** side by side (render each premium emoji in the panel text — Glass UI panels can render custom emoji in message text; visual verification required, see §33)
- [ ] Choice buttons: **replace/change mapping** and **cancel** (exact wording decided at implementation)
- [ ] Cancel leaves the mapping untouched
- [ ] Replace overwrites only after explicit confirmation
- [ ] No silent overwrite path exists anywhere (service-layer guard + UI guard)

Status: **PLANNED**.

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

- [ ] Global default category selectable in the Emoji panel
- [ ] Optional per-chat override (set/clear via panel while targeting that chat through the existing target-context machinery)
- [ ] Resolution order at message time: per-chat override → global default → no replacement
- [ ] Documented in panel UI (which scope is in effect)

Alternative rejected for MVP: per-chat-only (would make replacement opt-in
per chat with no default) and strictly-global-only (conflicts with §C's
per-category expectations for different audiences). This decision is also
listed in §34 for confirmation.

Status: **PLANNED — decision recorded, implementation pending**.

---

## 14. Emoji Replacement Toggle

- [ ] Global `Emoji Replacement: ON/OFF` toggle (Glass UI panel + persisted setting, §29)
- [ ] Independent from active category (toggle OFF ⇒ no processing regardless of category; toggle ON with no active category ⇒ no replacement)
- [ ] OFF behavior contract: messages remain untouched; no deletion; no bot message; no transformation
- [ ] Toggle state visible in the Emoji panel header and Health/Context panels (consistency with existing status surfaces)
- [ ] Toggle is owner-controlled only (is_owner gate)

Status: **PLANNED**.

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

- [ ] Bridge sender = existing helper bot client (`backend/helper/client.py`) or a dedicated second bot token (config; §29)
- [ ] Bridge has NO update loop, NO handler registration (send-only by default)
- [ ] Bounded-timeout send path consistent with `backend/telegram_api` conventions (`guarded_await`, normalized exceptions)
- [ ] Rate/flood handling (Telethon `flood_sleep_threshold` already configured on helper; verify adequacy)
- [ ] Destination resolution identical to the original message's chat (§18)

Status: **PLANNED — capability constraint documented; bridge capability
decision pending (§34)**.

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

The current `backend/telegram_api/_helpers.py::serialize_message` **drops
entities**. Prerequisite work:

- [ ] Entity-aware message representation (raw text + `MessageEntity` list, incl. `MessageEntityCustomEmoji` and UTF-16 code-unit offsets — Telegram entity offsets are UTF-16 based, which matters because emoji occupy two units)
- [ ] Transformer operates on (text, entities) tuples; produces new (text, entities) with correct UTF-16 offsets after span replacement
- [ ] Unit tests with mixed scripts (emoji + Persian + Latin) pinning offset correctness — see §30
- [ ] Existing `font_style.py` already solves UTF-16-width issues for the Glass UI font (see `backend/helper/font_style.py`); reuse its analysis where applicable rather than duplicating

Status: **PLANNED — known serialization gap documented**.

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

- [ ] Library browser and category mapping browser render **2 columns × 5 rows = 10 items per page**
- [ ] Navigation row: `[ ◀ ] [ 1/5 ] [ ▶ ]` (exact glyph style follows the Glass UI conventions already used in `backend/bot/handlers/misc.py` font pages and `backend/bot/handlers/retrieve.py`)
- [ ] Callback data scheme reuses existing `action:*` conventions with page parameters (see `action:font_page:prev/next` prior art; callback-data length limits already handled by `backend/helper/context.py::truncate_callback_data`)
- [ ] Edit-in-place panel updates (no message spam — `AGENTS.md` §13 rule 4)
- [ ] Fallback when helper disabled: edit-in-place text listing (consistent with existing no-helper degradation, `AGENTS.md` §10)
- [ ] 10-per-page pagination applies to: library entries, mappings list, and collection import results

Status: **PLANNED**.

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

- [ ] Toggle + active category + bridge config persisted via the settings pattern (`settings_service.py` / `panel_settings_repository.py` style: validated keys, single-row persistence, in-process cache) — exact table(s) decided with the schema work
- [ ] Library/categories/mappings persisted via the `db/client.py` Supabase-or-in-memory-fallback pattern (new tables; creation is manual/out of scope here)
- [ ] Durable-vs-fallback labeling follows the `config_store.py` contract (`DEGRADED_READ_KEY` / `SESSION_ONLY_KEY`) so a restart can never present RAM state as DB state
- [ ] New env vars (if any, e.g. a dedicated bridge bot token) follow `config.py` optional-var conventions; default must keep the feature disabled/inert
- [ ] Feature flag default: Emoji Replacement OFF on first boot

Status: **PLANNED**.

---

## 30. Test Plan

Follows existing test conventions (`tests/`, pytest, real Dispatcher/
registry/executor patterns like `tests/test_provider_tool_boundary.py` and
`tests/test_semantic_intent_boundary.py`; no live Telegram in tests):

- [ ] Transformer unit tests: emoji-only rewrite; entity offsets (UTF-16) preserved/recomputed correctly across mapped spans; mixed-script cases (emoji + Persian + Latin); unmapped passthrough; multi-emoji single-pass (§20/§21/§22)
- [ ] Reconstruction flow tests: fake self-client + fake bridge client; assert delete called only after prepared send succeeds; same-destination assertion; reply-to propagation (§15/§18)
- [ ] Authorization tests: non-owner-authored message never processed; incoming events never processed (§19)
- [ ] Loop-prevention tests: bot-sender message never reprocessed (§24)
- [ ] Mapping service tests: uniqueness enforcement; conflict detection returns current+new visuals data; replace only on explicit confirm (§11/§12)
- [ ] Category resolution tests: override > default > none (§13)
- [ ] Toggle tests: OFF ⇒ zero Telegram calls (§14)
- [ ] Import tests: set enumeration mocked; idempotent re-import; failure paths honest (§9)
- [ ] Pagination tests: 10-per-page slicing; page clamp; callback round-trip (§25)
- [ ] Custom composition tests: references not duplicates; propagation semantics per §26 decision
- [ ] Reaction wrapper tests: bounded timeout, exception normalization, payload correctness (§27)
- [ ] Full-suite gate: existing 5126-test suite must stay green (baseline at commit `eb4d852`)

Status: **PLANNED**.

---

## 31. Validation Plan

Beyond unit tests — verification against real behavior before IMPLEMENTED
can become TESTED/VERIFIED:

- [ ] Compile check (`compileall`) + full test suite green
- [ ] Live Telegram validation checklist (manual, owner's environment): import a real collection via Saved Messages; map 2 emojis; verify replacement in a private chat, a group, and Saved Messages; verify text/entities/media/caption/reply preservation visually; verify unmapped emoji untouched; verify loop does not occur; verify toggle-OFF leaves everything untouched
- [ ] Bridge capability verification: confirm which custom-emoji send mode actually works with the owner's bot setup (Fragment username vs alt-text fallback) — record the outcome in §17
- [ ] Failure-mode validation: revoke bot send permission in a chat and verify the original message survives (send-first ordering)
- [ ] No architectural drift audit: grep-style confirmation that no second executor/scheduler/update loop and no keyword/regex routing were introduced
- [ ] Documentation consistency: `AGENTS.md`/`IMPLEMENTATION_REPORT.md` updated only when the feature actually lands (not by this roadmap task)

Status: **PLANNED**.

---

## 32. Implementation Phases

Phase order (each phase independently verifiable; nothing is complete until
its checkboxes AND validation pass):

- [ ] **Phase 0 — Prerequisites:** entity-aware serialization (§22 gap in `_helpers.py`); decision on bridge capability (§34-D); confirm mapping persistence shape (§34-E)
- [ ] **Phase 1 — Library & Import:** domain model (§7), library storage, Saved Messages collection import (§8, §9), import report panel
- [ ] **Phase 2 — Categories & Mappings:** category CRUD, mapping editor, uniqueness + conflict UI (§10–§12), 2×5 pagination (§25)
- [ ] **Phase 3 — State & Toggle:** replacement toggle, active category (global default + per-chat override) (§13, §14, §29)
- [ ] **Phase 4 — Reconstruction:** transformer (entity-safe), reconstructor with send-first ordering, bot bridge send path, same-destination + reply preservation (§15–§23), loop prevention (§24)
- [ ] **Phase 5 — Custom Category:** composition UI + reference semantics (§26)
- [ ] **Phase 6 — Reactions:** `telegram_api/reactions.py` wrapper, reaction service, Glass UI react action (§27)
- [ ] **Phase 7 — Hardening:** error/capability handling completion (§28), test plan completion (§30), validation checklist execution (§31), documentation updates (`AGENTS.md`, `IMPLEMENTATION_REPORT.md`) in the landing commit(s)

Status: **PLANNED**.

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
5. **Cross-account media re-send mechanism** (§23) — Unknown — requires
   implementation/investigation.

Status: **PLANNED — dependencies verified; blockers documented**.

---

## 34. Decisions That Still Require Confirmation

| # | Decision | Options | Default proposal | Owner input needed |
|---|---|---|---|---|
| A | **Active category scope** | global-only / per-chat-only / global default + per-chat override | global default + per-chat override (§13, derived from existing settings/target-context architecture) | Confirm or override |
| B | **Reconstruction ordering** | send-new-then-delete / delete-then-send | send-first (never lose the original; worst case temporary duplicate) (§15) | Confirm |
| C | **Bridge bot identity** | reuse helper bot (`BOT_TOKEN`) / dedicated second bot token | reuse helper bot; env override for a dedicated token (§17/§29) | Confirm |
| D | **Premium-visual delivery mode** | Fragment-purchased additional username for the bridge bot / alt-text fallback only / investigate other TL paths | design for both: attempt custom-emoji entities, degrade to alt text; Fragment purchase is the owner's cost/ownership decision (§17/§28) | Required — capability + budget decision |
| E | **Mapping persistence shape** | new dedicated tables / extend an existing generic store | dedicated tables following `db/client.py` patterns (§7/§29); schema work is manual and out of scope here | Confirm at Phase 0 |
| F | **Custom composition semantics** | live-reference (follows source category changes) / snapshot-on-compose | snapshot-on-compose with explicit refresh action (§26) | Confirm |
| G | **Loop-prevention mechanism** | sender-id structural check / visible sentinel marker / both | sender-id check (invisible to other chat members) (§24) | Confirm |
| H | **Conflict UI wording** | exact button/panel labels | decided at implementation (§12) | Optional |
| I | **Notification behavior of replacement messages** | normal / silent | normal (§18) | Optional |

No decision above may be resolved silently during implementation without
updating this section.

Status: **OPEN — awaiting confirmation**.

---

## 35. Not Implemented Yet

**Nothing in this feature is implemented.** Explicit current truth:

- No emoji library, category, or mapping code exists.
- No collection import exists.
- No replacement toggle, transformer, reconstructor, or bot bridge exists.
- No reaction code exists.
- No schema/tables for this feature exist (and none were created — schema
  changes are manual and were explicitly out of scope for this roadmap).
- No tests for this feature exist.
- `DATABASE_ARCHITECTURE.md`, all application code, and all tests are
  unchanged by this roadmap task (only this `ROADMAP.md` was added).

Every checkbox in this document reflects design intent, not completion.
Implementation begins at Phase 0 (§32) after the §34 decisions are confirmed.

---

*Roadmap established from repository investigation at commit `eb4d852`
(branch `main` worktree, Telethon 1.34.0 verified in the active venv).
Document will be reset and replaced when the Emoji & Reaction feature
completes.*
