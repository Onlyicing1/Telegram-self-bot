# Implementation Report

> **Scope:** this report describes the work performed in the current change
> set only. It is rewritten (not appended) each time; historical narrative
> lives in git history. Nothing here claims live-Telegram or live-Supabase
> verification — see §10/§12 for exactly what was and was not verified.

**Feature: Emoji & Reaction — Phase 2 COMPLETE (Categories & Mappings).**

**Current phase: Phase 2 — Categories & Mappings IMPLEMENTED + TESTED
(offline only).**
**Next: Phase 3 — State & Toggle (ROADMAP §32). Live validation of Phases
0–2 remains the owner's §31 checklist; §34-D stays OPEN, §34-E is
implemented against its default proposal with owner sign-off still OPEN.**

This change set delivers ONLY Phase 2 on top of the Phase 0 + Phase 1
state: category CRUD, the mapping editor, per-category mapping uniqueness,
the duplicate/conflict UI, and 2×5 pagination for the new lists. Phase 1
(library, import, set enumeration, library browser) is reused as-is — not
refactored. Phases 3+ (replacement toggle, active category, transformer,
reconstruction, Custom composition, reactions, AI tools) are explicitly
NOT implemented.

---

## 1. What was implemented

### 1.1 Persistence layer (`backend/db/client.py` extended)

New dedicated tables following the exact Phase 1 `emoji_library` pattern
(Supabase-or-in-memory-fallback sync helpers, `_run_sync` dispatch, honest
`record_event` telemetry, never raising):

- **Categories** — `insert_emoji_category` (duplicate `(owner_id, name)`
  refused → `None`; durable UNIQUE index is the backstop),
  `get_emoji_category`, `get_emoji_category_by_name`,
  `list_emoji_categories` (newest-first, `(rows, total)`),
  `update_emoji_category` (rename; refuses to take a sibling's name; no
  upsert), `delete_emoji_category` (row-only; callers own the mapping
  cascade order).
- **Mappings** — `get_emoji_entry` (single owner-scoped library lookup —
  mappings resolve their premium emoji through this, never a copy),
  `insert_emoji_mapping` (duplicate `(owner_id, category_id, simple_emoji)`
  refused → `None`, never an overwrite), `get_emoji_mapping`,
  `list_emoji_mappings` (newest-first per category), `update_emoji_mapping`
  (only when the row still exists — a stale panel cannot resurrect a
  deleted mapping), `delete_emoji_mapping`,
  `delete_emoji_mappings_for_category` (`-1` signals a failed durable write
  so the caller aborts instead of half-deleting),
  `count_emoji_mappings_by_category` (`None` on durable-read failure — the
  UI must show an unknown-count state, not a fabricated 0).
- `_fallback` gained the `emoji_categories` / `emoji_mappings` stores so
  the no-Supabase mode is fully functional (same contract as Phase 1).

### 1.2 Service layer (`backend/services/emoji_category_service.py`, new)

The business-logic boundary the UI drives — deterministic, fail closed,
zero AI involvement (`backend.ai` is not imported):

- Category: `create_category`, `list_categories` (rows + total +
  per-category mapping counts, counts `None` on durable-read failure),
  `get_category`, `category_mapping_count`, `rename_category`,
  `delete_category`.
- Mapping: `create_mapping`, `list_mappings`, `get_mapping`,
  `replace_mapping`, `delete_mapping`.
- Validation helpers `clean_category_name` / `clean_simple_emoji`
  (single-line, non-empty after strip, ≤64 / ≤32 characters — structural
  validation only; no invented emoji semantics).

### 1.3 Glass UI (`backend/bot/handlers/emoji.py` extended)

Standard panel machinery only (registry, `InlinePanelBuilder`,
`panel:`/`action:` callbacks, owner check in the existing callback router;
the single `register()` call is unchanged in `router.py`):

- Main `😀 Emoji` panel gains the **🗂 Categories** row.
- **Categories** panel: 2×5 paged list, per-row `name · <count>` labels
  (`?` when counts are unreadable), page clamp, `＋ New category`.
- **Category** detail: name + live mapping count, `🗺 Mappings`,
  `✏️ Rename`, `🗑 Delete`.
- **Delete Category** confirmation panel: names the category, states how
  many mappings will be removed, states that library entries are NOT
  deleted; explicit `🗑 Delete` / `✗ Cancel`.
- **Mappings** panel per category: 2×5 paged list of `simple → premium`
  rows with honest `[entry missing]` marking when a referenced library
  entry no longer exists, `＋ Add mapping`, page clamp.
- **Mapping** detail: `simple → premium` visual, library entry id +
  missing-entry honesty, `✏️ Change`, `🗑 Remove`.
- **Pick Emoji** panel: the mapping editor's premium-emoji step — the
  existing library browser shape over the owner's library with the same
  2×5 grid and pager.
- Mapping flows run through a per-owner **draft buffer** (the Bio-builder
  server-side-buffer convention) with a 10-minute staleness bound: step
  1 arms a pending input for the simple emoji (existing `input_state`
  machinery, prompt via the existing listener contract), step 2 renders
  the picker, and the pick action calls the service.
- **Mapping Conflict** panel: dedicated panel rendered from the service's
  conflict result — actual current mapping visual vs requested new visual
  side by side, `_Nothing has been overwritten._`, buttons
  **Replace** / **Cancel**. Replace calls `replace_mapping` (the ONLY
  overwrite path); Cancel is a plain navigation with no write path.

---

## 2. Exact files changed

| File | Change |
|---|---|
| `backend/db/client.py` | + category/mapping db helpers + `get_emoji_entry` + 2 fallback stores (~640 lines) |
| `backend/services/emoji_category_service.py` | NEW — Phase 2 service layer (~430 lines) |
| `backend/bot/handlers/emoji.py` | + category/mapping/conflict panels, editor flow, draft buffer, registration (~820 lines added) |
| `tests/test_emoji_category_service.py` | NEW — 54 service tests |
| `tests/test_emoji_ui_phase2.py` | NEW — 42 UI/architecture tests |
| `ROADMAP.md` | current-state rewrite for Phase 2 |
| `IMPLEMENTATION_REPORT.md` | this rewrite |

NOT touched: `DATABASE_ARCHITECTURE.md`, `AGENTS.md`, `AI_MASTER_DESIGN.md`,
`backend/ai/**` (except nothing), `backend/telegram_api/**` (Phase 0/1 code
unchanged), `backend/bot/router.py` (registration call already wired),
`backend/bot/handlers/misc.py` (mother-menu row already present),
`supabase/migrations/**`, `render.yaml`, deployment docs, Phase 1 tests.
No SQL executed anywhere.

---

## 3. Category semantics (as implemented)

- A category is an owner-scoped row `(id, owner_id, name, created_at,
  updated_at)`; names are unique **per owner** (exact match after strip —
  deterministic; no case folding, no invented normalization).
- Validation is deterministic and fail closed: non-empty after strip,
  single-line, ≤64 characters; anything else is rejected with an honest
  code (`invalid_name` / `name_too_long`), never auto-corrected.
- Duplicate name on create/rename → refused (`name_exists`), never
  overwritten; the UI states it. The same name is allowed for a DIFFERENT
  owner.
- `list_categories` is newest-first and reports per-category live mapping
  counts; when the durable count read fails the count is shown as `?` /
  `unknown` — never a fabricated 0.
- Deleting a category removes ALL of its mappings FIRST, then the category
  row; a failed mapping-removal write aborts the whole deletion (fail
  closed). A failed category-row delete after mappings were removed is
  reported honestly (mappings gone, category remains — retry-able,
  idempotent). Library entries are NEVER touched by category deletion.
- No `is_custom` column, no built-in `Custom` category, no
  active-category state — §26/§13 are later phases and nothing about them
  was invented into the schema.

## 4. Mapping semantics (as implemented)

- A mapping is `(id, owner_id, category_id, simple_emoji, document_id,
  created_at, updated_at)` — a pure REFERENCE to the library: the row
  stores no alt text, no source, no definition (pinned by test). The
  library remains the single source of premium-emoji definitions.
- Creation resolves `document_id` against the owner's library via
  `get_emoji_entry` BEFORE any write; a missing or foreign-owner entry is
  rejected (`library_entry_not_found`). The mapping editor's picker only
  offers the owner's own entries, and the service re-proves it.
- Editor flow (create): category → simple emoji via existing
  `input_state` pending-input machinery → 2×5 library picker → service
  create. Change flow: from an existing mapping, re-opens the picker
  seeded with the mapping's simple emoji; the pick lands on the conflict
  path and explicit Replace performs the edit.
- `(owner_id, category_id, simple_emoji)` is unique — enforced at the
  service boundary BEFORE the write, with the db layer refusing
  duplicates as the backstop (and no upsert anywhere). The same simple
  emoji may exist in different categories; the same library document may
  be referenced by multiple mappings (both pinned by tests).
- Mapping deletion removes only the mapping row; the referenced library
  entry survives (pinned by test).

## 5. Conflict behavior (as implemented)

- A duplicate attempt NEVER silently overwrites — service detects the
  existing mapping before any write and returns
  `{conflict: True, current, current_entry, new_entry}`.
- The dedicated conflict panel shows the ACTUAL current premium emoji
  visual and the requested new one side by side (alt glyph + document id
  from the resolved library entries). When the CURRENT entry can no
  longer be resolved, the panel says `unavailable` for that side — it
  never fabricates a visual (pinned by test).
- Buttons (§34-H wording, decided here): **Replace** /
  **Cancel**. Replace overwrites only after this explicit confirmation,
  and re-proves (a) the mapping still exists and (b) the new library
  entry resolves — a stale panel fails honestly and changes nothing.
- Cancel leaves the mapping untouched (no write path at all — pinned by
  test). No silent overwrite path exists at any layer (pinned by tests
  at service + db layers).

---

## 6. Persistence status

Implemented against the EXISTING accepted contract of this feature:
`db/client.py` Supabase-or-in-memory-fallback, exactly as Phase 1
established for `emoji_library`. ROADMAP §34-E is still an OPEN owner
decision; Phase 2 implements its recorded DEFAULT proposal (dedicated
tables following `db/client.py` patterns) without claiming owner
confirmation. The user-facing consequences:

- No Supabase configured → fully functional in-memory mode (development
  contract, same as Phase 1).
- Supabase configured but the physical tables absent → writes fail
  honestly (`storage_failed` → honest UI message, nothing changed) and
  count reads report unknown counts — RAM is never silently presented as
  durable (ROADMAP §29 degradation contract).

## 7. Manual-only schema status (documented, NOT executed)

The physical schema below is EXACTLY what Phase 2's db layer expects. It
is documented for the OWNER to apply manually; this repository never
executed it, created no migration file, and left `DATABASE_ARCHITECTURE.md`
untouched:

```sql
-- MANUAL-ONLY — documented, NEVER executed by this repository.
CREATE TABLE IF NOT EXISTS emoji_categories (
  id         bigserial PRIMARY KEY,
  owner_id   bigint      NOT NULL,
  name       text        NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT emoji_categories_owner_name_key UNIQUE (owner_id, name)
);

CREATE TABLE IF NOT EXISTS emoji_mappings (
  id           bigserial PRIMARY KEY,
  owner_id     bigint      NOT NULL,
  category_id  bigint      NOT NULL,
  simple_emoji text        NOT NULL,
  document_id  bigint      NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT emoji_mappings_owner_category_emoji_key
             UNIQUE (owner_id, category_id, simple_emoji)
);
-- RLS per house style (service-role writes, SELECT-only policies) — owner action required.
```

DELIBERATE: no `is_custom` column (§26 is a later phase), no
`set_id`/`set_short_name` on mappings (mappings reference the library,
which owns provenance), no active-category columns (§13 is Phase 3).

---

## 8. Tests actually executed and exact results

Environment: repository venv (`python -m pytest`), no live Telegram, no
live Supabase. Run from the repository root with the pre-existing untracked
`telegram-self-bot/` mirror excluded from collection (it is not part of
this package and duplicates test basenames — see §10).

1. `py_compile` on ALL changed Python files → **exit 0**.
2. Focused Phase-2 suites:
   `tests/test_emoji_category_service.py` → **54 passed**
   `tests/test_emoji_ui_phase2.py` → **42 passed**
   (combined: **96 passed**)
3. Existing Phase-1 emoji suites: `tests/test_emoji_library_import.py`,
   `tests/test_emoji_set_enumeration.py`, `tests/test_emoji_ui.py` →
   **all passed (no regressions)**.
4. Existing Phase-0 bridge suite: `tests/test_bridge_delivery.py` →
   **all passed**.
5. Full suite (`pytest -q --ignore=telegram-self-bot`) →
   **5371 passed, 26 skipped in 120.33s** = Phase 1 baseline 5275 + 96 new
   tests — no regressions.
6. `git diff --check` → clean (exit 0).

## 9. What was NOT tested

- **Live Telegram verification: NOT performed.** The whole Phase 2 surface
  (panel rendering inside a real inline session, pending-input timing, the
  conflict panel's actual visual rendering of custom emoji, pagination
  under real Telegram update flow) is unverified live. ROADMAP §31's
  checklist remains open.
- **Live Supabase verification: NOT performed.** No SQL executed, no
  migration added, no live table touched. The physical
  `emoji_categories` / `emoji_mappings` tables still do not exist
  anywhere; until the owner applies §7's manual-only schema, a configured
  Supabase reports write failures honestly.
- The Supabase code paths are exercised only through the same
  fake-client pattern Phase 1 used (in-memory fallback is the primary
  test store); no real network call was made.
- The in-panel custom-emoji RENDERING fidelity question (ROADMAP §12/§33:
  panels render alt glyph + document id; whether real custom-emoji
  entities can appear in panel text) remains unverified — unchanged from
  Phase 1, still scheduled for live validation.

## 10. What remains intentionally unimplemented

- Replacement toggle + active category (Phase 3, §13/§14) — NOT
  implemented, including no persisted toggle state.
- Transformer, reconstructor, bot delivery, loop prevention (Phase 4,
  §15–§24) — NOT implemented; no outgoing message is ever transformed;
  no `forward_messages` call exists.
- Custom category composition (Phase 5, §26) — NOT implemented; no
  `is_custom` column exists.
- Reactions (Phase 6, §27) — NOT implemented; zero reaction code.
- AI tools for this feature — NOT implemented; `backend.ai` is not
  imported by any changed module (pinned by an AST audit test).
- §34-D (bridge capability) — still OPEN (owner).
- §34-E owner sign-off — still OPEN; Phase 2 implements the recorded
  default proposal only.
- Live schema application — manual, owner-only (§7 above).

## 11. Current phase status

**Phase 2 — Categories & Mappings: IMPLEMENTED + TESTED (offline).**
Category CRUD, mapping editor, uniqueness, conflict UI, and 2×5
pagination are complete against the documented contracts; every Phase 2
behavior is pinned by tests; the full suite is green. What "complete"
does NOT mean here: no live Telegram or live Supabase verification has
been performed, and no physical schema was applied.

## 12. Next phase

**Phase 3 — State & Toggle** (ROADMAP §32): replacement toggle +
active category (global default + per-chat override), persisted via the
settings pattern (§29). Nothing blocks it except the owner's live
validation appetite; §34-A (scope decision) is its recorded open owner
input. Alternatively, the owner may first run the §31 live-validation
checklist over Phases 1–2 — the roadmap's phase order allows either.
