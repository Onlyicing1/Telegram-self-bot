# Implementation Report

> **Scope:** this report describes the work performed in the current change
> set only. It is rewritten (not appended) each time; historical narrative
> lives in git history. Nothing here claims live-Telegram or live-Supabase
> verification — see §9/§10 for exactly what was and was not verified.

**Feature: Emoji & Reaction — Phase 3 COMPLETE (State & Toggle).**

**Current phase: Phase 3 — State & Toggle IMPLEMENTED + TESTED (offline
only).**
**Next: Phase 4 — Reconstruction (ROADMAP §32). Live validation of Phases
0–3 remains the owner's §31 checklist; §34-D stays OPEN; §34-E (Phase 2
default-proposal implementation) and §34-A (this phase's implemented
scope decision) still await owner sign-off.**

This change set delivers ONLY Phase 3 on top of the Phase 0–2 state: the
Emoji Replacement ON/OFF toggle, the global default active category, the
optional per-chat override, the deterministic resolution boundary Phase 4
will consume, their Glass UI surface, and their persistence through the
existing `db/client.py` pattern. Phase 2 (categories & mappings) is reused
as-is — not refactored. Phase 4+ (transformer, reconstruction, bot-bridge
send path, loop prevention, Custom composition, reactions, AI tools) is
explicitly NOT implemented.

---

## 1. What was implemented

### 1.1 Persistence layer (`backend/db/client.py` extended)

Two new stores following the exact Phase 1/2 emoji pattern (sync helpers,
`_run_sync` dispatch, honest `record_event` telemetry, never raising,
in-memory `_fallback` covering no-Supabase mode):

- **`emoji_state`** — the owner's single global row: `replacement_enabled`
  (bool; absent row = False = first-boot default OFF) and
  `global_default_category_id` (nullable int). Functions:
  `get_emoji_state` (`None` when no row exists yet — every absent field
  means its default), `upsert_emoji_state` (insert-or-update merge;
  `False` on a durable-write failure — never a phantom success).
- **`emoji_chat_overrides`** — one row per `(owner_id, chat_id)` holding
  `override_category_id` (nullable int). Functions:
  `get_emoji_chat_override` (`None` = no override), `upsert_emoji_chat_override`
  (creates the row when absent; `False` on durable-write failure).
- `_fallback` gained `emoji_state` (dict keyed by owner) and
  `emoji_chat_overrides` (list) so degraded mode is fully functional.

No SQL was executed, no migration file was created, no live table was
touched — the physical tables are MANUAL-ONLY (§6).

### 1.2 Service layer (`backend/services/emoji_state_service.py`, new)

The Phase 4 state-resolution boundary. Deterministic, fail-closed, zero AI
involvement (`backend.ai` is not imported), no scheduler/executor/loop:

- **Toggle (§14)** — `replacement_enabled(owner)` (default False),
  `set_replacement_enabled`, `toggle_replacement` (returns the NEW value;
  a degraded write or failed read leaves the toggle OFF — fail closed,
  never silently ON).
- **Global default (§13)** — `get_global_default_category` /
  `set_global_default_category` / `clear_global_default_category`. A set
  request validates that the category is a REAL owner-scoped row in the
  live `emoji_categories` table; nonexistent and foreign-owner ids are
  rejected (False).
- **Per-chat override (§13)** — `get_chat_override` /
  `set_chat_override` / `clear_chat_override`. The same ownership +
  existence validation as the global default; per-chat rows are fully
  independent (each chat may override to a different category).
- **Resolution (the §13/§14 contract, pinned by tests):**

  ```
  replacement OFF        -> None (no replacement), whatever the categories
  override set           -> override category
  else global default    -> global default category
  else                   -> None (no replacement)
  ```

  `resolve_effective_category(owner, chat_id)` implements exactly this.
  The winning category is validated against the LIVE owner-scoped
  category table at resolution time, so a category deleted after it was
  selected can never remain effective: the resolution fails closed to
  None instead of fabricating a substitute (a deleted override does NOT
  silently fall back to the global default either). `chat_id=None`
  (chat unknown) resolves through the global default alone.

### 1.3 Glass UI (`backend/bot/handlers/emoji.py` extended)

Standard panel machinery only — no new UI framework, no keyword routing,
all callbacks owner-scoped through the existing callback router:

- **Main `😀 Emoji` panel** — new `Replacement: ✅ ON / ❌ OFF` line and a
  `🔁 Replacement` row.
- **New `emoji_replacement` panel** — shows the toggle state, the current
  global default (by name), the per-chat override for the CURRENT target
  chat (resolved through the existing `backend.helper.target_context`
  machinery — a real armed reply target; never a fabricated chat id),
  and the effective/resolved category for that target with the honest
  OFF / no-category explanations. Actions:
  - `action:emoji_state_toggle` — Turn ON/OFF.
  - `action:emoji_state_global_pick` → the 2×5 `emoji_state_scope`
    category picker (`global` scope) → `action:emoji_state_choose`.
  - `action:emoji_state_override_pick:<chat_id>` → the same picker
    (`override` scope) — offered ONLY when a real target chat is armed.
  - `action:emoji_state_clear:<chat_id>` — clear the override.
- Without a target chat the panel says so explicitly ("no target chat —
  reply to a message in the chat you want to target") and offers no
  per-chat actions; the global actions remain.
- Category pickers list owner-scoped categories only; a rejected choose
  (deleted/foreign category) renders an explicit nothing-changed notice.

### 1.4 Repository note (merge alignment)

The worktree arrived with an uncommitted local Phase 1-remainder slice
(`8a5d23d`) that duplicated work already pushed upstream (`ba5a3b5`,
`3b0e476` — including a full Phase 2). It was merged and reconciled: the
superseded local surfaces (duplicate panel/set-resolution test files and a
duplicate router registration) were dropped in favor of the remote
implementations, and the pre-existing test-suite ordering issue caused by a
leaked `inline_engine._owner_id` in another module's fixture was fixed in
that reconciliation. The delivered Phase 3 code sits on the remote Phase 2
state unchanged.

---

## 2. Exact files changed

| File | Change |
|---|---|
| `backend/db/client.py` | `emoji_state` + `emoji_chat_overrides` added to `_fallback`; NEW `get_emoji_state` / `upsert_emoji_state` / `get_emoji_chat_override` / `upsert_emoji_chat_override` (+ sync helpers) |
| `backend/services/emoji_state_service.py` | NEW — toggle / global default / per-chat override / validated resolution boundary |
| `backend/bot/handlers/emoji.py` | Replacement line + row on the main panel; NEW `emoji_replacement` + `emoji_state_scope` panels; NEW actions `emoji_state_toggle` / `emoji_state_global_pick` / `emoji_state_override_pick` / `emoji_state_choose` / `emoji_state_clear` |
| `tests/test_emoji_state_phase3.py` | NEW — 46 focused tests (§5) |
| `ROADMAP.md` | Current-state updates (Phases 3 sections, §29, §30, §32, §35) |
| `IMPLEMENTATION_REPORT.md` | This rewrite |

No other file was touched: no tool-registry entry, no AI module, no
schema/SQL file, no `DATABASE_ARCHITECTURE.md`, no `AGENTS.md`, no
bridge/`_helpers.py` change, no router change (the emoji module was
already registered).

---

## 3. State semantics (as implemented)

| Concern | Storage | Default | Validation |
|---|---|---|---|
| `replacement_enabled` | `emoji_state` (single row/owner) | **False (OFF)** | bool |
| Global default category | `emoji_state.global_default_category_id` | unset (None) | live owner-scoped category required at set time |
| Per-chat override | `emoji_chat_overrides` (row per chat) | unset (None) | live owner-scoped category required at set time |

The toggle is independent of categories: OFF disables everything; ON with
no resolvable category still means no replacement (both pinned by tests).

---

## 4. Resolution contract (Phase 4 boundary)

`resolve_effective_category(owner_id, chat_id) -> int | None`:

1. owner invalid → None
2. replacement OFF → None (regardless of any stored category)
3. override for `chat_id` set AND its category still exists → that category
4. override set but category deleted → None (fail closed; no fallback)
5. else global default set AND its category still exists → that category
6. else global default deleted → None (fail closed)
7. else → None

The Phase 4 transformer/reconstructor must consume ONLY this function (or
the raw getters for diagnostics) — no replacement processing exists in
this phase.

---

## 5. Tests actually executed and exact results

All commands run with the project venv
(`/home/daytona/codebase/.venv`), exit statuses captured, cwd
`/home/daytona/codebase/.m14` (source-inspection tests resolve paths
relative to cwd):

1. `python -m pytest tests/test_emoji_state_phase3.py -q` → **46 passed**
   (0.44s). Coverage: first-boot default OFF; ON persists/loads; OFF
   persists/loads; toggle flip; invalid-owner refusal; global default
   set + nonexistent rejection + foreign-owner rejection + clear;
   override set/clear + nonexistent/foreign rejection + per-chat
   independence; resolution order (override > global > none); fallback
   after clear; no-override/no-default → None; OFF ⇒ no resolution
   despite categories; global used in other chats; deleted
   global/override category fails closed; unknown chat (None) resolves
   via global default; owner isolation on every read/write; degraded
   write → False + state unchanged; degraded override write; degraded
   read → unset; UI registration; main-panel state line (ON and default
   OFF); full-state panel; effective-global display; no fabricated
   target chat + per-chat actions withheld; honest OFF effective-none;
   toggle action mutates only the toggle; choose actions mutate only
   their own scope; foreign category choose rejected; clear action
   clears only the override; owner-scoped picker; empty-library
   picker prompt; ≤64-byte callback bounds; engine-owner scoping; AST
   import audit (no `backend.ai`); no second client/loop/scheduler/
   executor/forwarding/events.NewMessage; no Phase 4 surface
   (transform/reconstruct/delete_original/bridge_send); resolution is
   Telegram-free.
2. `python -m pytest tests/test_emoji_state_phase3.py
   tests/test_emoji_category_service.py tests/test_emoji_ui.py
   tests/test_emoji_ui_phase2.py tests/test_emoji_library_import.py
   tests/test_emoji_set_enumeration.py tests/test_bridge_delivery.py -q`
   → **259 passed** (0.82s) — no Phase 0–2 regressions.
3. `python -m pytest tests/ -q` → **5417 passed, 26 skipped** (119.16s)
   = Phase 2 baseline 5371 + 46 new — no regressions.
4. `py_compile` on the four changed/added Python files → clean.
5. `git diff --check` → clean.

---

## 6. Persistence behavior

- **Durable mode (Supabase configured, tables applied):** the toggle and
  global default survive restart via the `emoji_state` row; per-chat
  overrides survive restart via `emoji_chat_overrides` rows.
- **No Supabase configured:** the in-memory `_fallback` stores are the
  authoritative store (development mode; state is process-local — the
  established repository convention).
- **Supabase configured but the write fails:** the service returns False,
  the UI renders an explicit nothing-changed notice, and the state stays
  exactly as it was — no phantom success, RAM never presented as durable.
- **Supabase configured but the read fails:** the read reports None, which
  the state semantics treat as "not set" (OFF / no category) — replacement
  fail-closes OFF, never ON.
- The physical `emoji_state` / `emoji_chat_overrides` tables do NOT exist
  anywhere yet (MANUAL-ONLY schema below; nothing executed).

---

## 7. Manual-only schema status (documented, NOT executed)

```sql
-- MANUAL-ONLY — documented, NEVER executed by this repository.
CREATE TABLE IF NOT EXISTS emoji_state (
  owner_id                   bigint PRIMARY KEY,
  replacement_enabled        boolean NOT NULL DEFAULT false,
  global_default_category_id bigint,
  updated_at                 timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS emoji_chat_overrides (
  owner_id              bigint NOT NULL,
  chat_id               bigint NOT NULL,
  override_category_id  bigint,
  updated_at            timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT emoji_chat_overrides_owner_chat_key UNIQUE (owner_id, chat_id)
);
-- RLS per house style (service-role writes, SELECT-only policies) — owner action required.
-- No SQL was run anywhere; no migration file was added.
```

---

## 8. §34 decision status after this phase

- **§34-A (active category scope)** — implemented as documented:
  global default + per-chat override (per-chat is an override, never a
  second authority). Owner sign-off still OPEN.
- **§34-D (bridge capability), §34-B (ordering), §34-C (bridge identity),
  §34-F (custom composition), §34-G (loop prevention), §34-I
  (notifications)** — untouched, still OPEN.
- **§34-H** — resolved at Phase 2 (unchanged).
- **§34-E** — stands as "default proposal implemented at Phase 2,
  awaiting owner confirmation" (unchanged; Phase 3 added two more
  manual-only tables on the same pattern).

No §34 decision was resolved silently.

---

## 9. What was NOT tested / not live-verified

- **No live Telegram verification** — no real outgoing message was ever
  processed (Phase 3 processes nothing at runtime by design), no real
  panel was opened on Telegram, the target-context arming path was
  exercised only through the in-memory `TargetContext` store.
- **No live Supabase verification** — everything ran against the
  in-memory fallback; the durable write/read paths are pinned by the
  pattern-level tests only (same posture as Phases 1–2).
- The physical schema (§7) was never applied anywhere.
- Custom-emoji rendering inside panel text (§33) remains unverified.

## 10. What remains intentionally unimplemented (Phase 4+)

Transformer, entity transformation, message reconstruction,
delete-original logic, bot-bridge send-path changes, same-destination
delivery, reply preservation, media re-send, loop prevention, custom
category, reactions, AI tools, ToolRegistry/ToolExecutor/
ProviderManager changes, new schedulers/update-loops/executors,
semantic/regex routing — all remain Phase 4+ per ROADMAP §32. Phase 3
provides ONLY the configuration/state layer and the deterministic
resolution boundary.

## 11. Current phase status

- **Current:** Phase 3 — State & Toggle — IMPLEMENTED + TESTED (offline).
- **Next:** Phase 4 — Reconstruction (§15–§24). Its consumption contract
  is §4 above.
