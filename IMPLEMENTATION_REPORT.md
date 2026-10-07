# Implementation Report

> **This document reflects the CURRENT repository state after Phase 5.**
> It replaces the Phase 4 report. Where this document and the code disagree,
> the code is authoritative and this document must be updated.

| Field | Value |
|---|---|
| Feature | Emoji & Reaction (premium/custom emoji replacement) |
| Phase | **Phase 5 — Custom Category / Composition (§26)** |
| Status | **IMPLEMENTED + TESTED offline** (unit/regression suites); live Telegram/Supabase **NOT verified** |
| Branch | `m14-stt` |
| Base commit (before this phase) | `6ca2db1` — docs(emoji): record the Phase 4 delivery metadata |
| Schema work | Repository-side contract only. **No SQL was executed.** `is_custom` / `source_category_ids` are MANUAL-ONLY (SQL + rollback in §8) |
| §34-F (snapshot vs live reference) | Implemented as the documented default (snapshot-on-compose + explicit Refresh). **Owner confirmation remains OPEN** |
| Next phase | Phase 6 — Reactions (§27) |

---

## 1. Objective (Phase 5)

Add a **Custom Category** that composes its mappings from existing categories,
without touching anything outside §26:

- a category TYPE that is explicit metadata (never inferred from the name);
- composition from owner-chosen, owner-scoped source categories in a
  deterministic order (the owner's order IS the precedence);
- conflicts (the same simple emoji in several sources) refused until the owner
  explicitly confirms — never silently overwritten;
- **snapshot** semantics per the §34-F default proposal: a composition is a
  concrete snapshot, later source changes do NOT move it, and only an explicit
  Refresh rebuilds it (atomically, or not at all);
- a Glass UI built only from the existing panel/callback/input architecture;
- compatibility with Phase 2 (mapping model, conflict machinery), Phase 3
  (state + resolution — unchanged at the public contract level) and Phase 4
  (the transformer/reconstruction never learns what a Custom Category is).

Explicitly out of scope (not implemented, not touched): reactions (§27),
media/caption reconstruction (§23), owner-visible failure surfacing (§28),
AI tools, scheduled/automatic composition, new background jobs, live Telegram
validation, any unrelated refactor.

---

## 2. Implementation status

| Area | Status |
|---|---|
| Custom Category type metadata (`is_custom`, explicit) | IMPLEMENTED + TESTED |
| Source list persistence (`source_category_ids`, manual column) | IMPLEMENTED + TESTED (in-memory fallback; real Supabase column is MANUAL-ONLY) |
| Composition plan (validation + merge + conflict report, **no writes**) | IMPLEMENTED + TESTED |
| Compose / Refresh (snapshot persist, atomic-or-nothing, rollback, honest reporting) | IMPLEMENTED + TESTED |
| Conflict confirmation (first source wins, explicit owner action) | IMPLEMENTED + TESTED |
| Cycle / self / duplicate / foreign / missing source rejection | IMPLEMENTED + TESTED |
| Nested Custom source → flattened concrete snapshot | IMPLEMENTED + TESTED |
| Snapshot invariance (source edit/add/remove/delete) + explicit Refresh | IMPLEMENTED + TESTED |
| Manual mapping edits disabled on a composed category | IMPLEMENTED + TESTED |
| Glass UI: custom creation, compose panel, 2×5 source picker, refresh, honest failure panels | IMPLEMENTED + TESTED |
| Active-category compatibility (global default, per-chat override, resolution order) | IMPLEMENTED + TESTED (no Phase 3 change) |
| Phase 4 pipeline compatibility (no special path) | IMPLEMENTED + TESTED |
| Live Telegram / Supabase verification | **NOT RUN — unverified** |

---

## 3. What was implemented

### 3.1 Category type metadata — `backend/services/emoji_category_service.py`

- `create_category(owner_id, raw_name, *, is_custom=False)`: a custom row is
  created with `is_custom=True` and `source_category_ids=[]`; an ordinary row
  is written **byte-identically to Phase 2** (the two fields are only ever sent
  for custom rows), so Phase 2 behavior and Phase 2 tests are untouched.
- `is_custom_category(row)` — the single type predicate; the type is explicit
  row metadata and is **never** inferred from the name (a category literally
  named "Custom" stays ordinary).
- `category_sources(row)` — the persisted, normalized source list (accepts a
  JSON string from a jsonb/Text storage read); an absent list reads as honestly
  empty, never fabricated.
- `create_category` now reports `E_STORAGE` when the write fails and the name is
  free (previously the same path existed; custom creation is the first caller
  that can hit it on a Supabase without the manual columns).

### 3.2 Composition service (same module, isolated §26 section)

Deliberately **isolated** so that changing the §34-F decision later touches
only these functions — the mapping model, the resolution boundary and the
replacement pipeline stay as they are.

- `_validate_source_ids(raw)` — bounded (`MAX_SOURCE_CATEGORIES = 20`),
  ordered, distinct positive ids; duplicate ⇒ explicit `E_SOURCE_DUPLICATE`
  (never silently collapsed); empty/None ⇒ `E_NO_SOURCES`; malformed ⇒
  `E_SOURCE_MISSING`.
- `_reachable_category(owner, start_ids, target_id)` — bounded walk
  (`MAX_SOURCE_GRAPH_VISITS = 200`) over the **persisted** source graph:
  returns `cycle` when the target is reachable (self-composition included),
  `unresolved` when the graph is too large to prove acyclic (fail closed).
- `plan_composition(owner, category_id, source_ids)` — computes the snapshot
  the composition WOULD produce, with **zero writes**: target must be a real
  owner-scoped Custom Category (`E_NOT_CUSTOM` otherwise), sources validated,
  self/cycle rejected, every source read with a bounded, complete listing
  (`E_MAPPING_LIST_INCOMPLETE` fails the whole plan closed), then merged
  deterministically with **first source wins**; later definitions are reported
  in `conflicts` and never applied. Reports `scanned`, `unresolvable`,
  `empty_sources`, `missing_sources`.
- `compose_category(..., confirm_conflicts=False)` — recomputes the plan against
  live state (a stale UI flow can never apply a stale plan) and refuses to write
  anything when conflicts exist and the owner has not confirmed
  (`E_CONFLICT`); on confirmation it persists the snapshot.
- `refresh_category(..., confirm_conflicts=False)` — rebuilds from the
  **persisted** sources only: a deleted/foreign source is dropped from the
  composition and reported (`missing_sources` / `removed_sources`), never
  fabricated; when nothing is left the previous snapshot is kept and the result
  says why (`E_SOURCE_MISSING`); new conflicts revealed by changed sources also
  require explicit confirmation, otherwise the previous valid snapshot stays.
- `_replace_category_snapshot` — validates the target set (bounded,
  `MAX_SNAPSHOT_MAPPINGS = 2000`, exactly one entry per simple emoji), then
  applies **new-first** (additions/repoints before any deletion) so a failure
  leaves the previous rows intact; a failure during that phase is undone with a
  compensating rollback whose success is reported (`rolled_back`); deletions run
  last and a failed deletion leaves a superset, reported honestly as
  `E_SNAPSHOT_INCOMPLETE` instead of pretending the snapshot equals the sources.
- `_persist_composition` — source list first, snapshot second, with the previous
  source list restored when the snapshot write fails.
- Snapshot mappings are ordinary `emoji_mappings` rows pointing at the SAME
  library entries (`document_id`) worldwide — the emoji library is never
  duplicated, and the transformer/resolver never sees a distinction.

### 3.3 Persistence — `backend/db/client.py`

- NEW `set_emoji_category_sources(owner_id, category_id, source_ids) -> bool`
  (plus the sync worker) following the existing db-layer pattern: owner-scoped
  `update(...).eq("owner_id", ...).eq("id", ...)`, `try/except` at the boundary,
  honest `False` on failure or a missing row, never raises, wraps the heavy call
  through the module's bounded `_run_sync` helper. Only ever called for Custom
  Categories.
- `insert_emoji_category` needed no change: it already persists the row dict it
  is given, so `is_custom` / `source_category_ids` ride along on custom rows
  only.

### 3.4 Glass UI — `backend/bot/handlers/emoji.py`

- Categories panel: new **"🧩 New custom category"** row; custom rows render
  with the 🧩 marker (from the explicit type flag, never from the name).
- Custom creation flow: name input (existing pending-input machinery), then the
  compose panel opens with an empty draft armed; duplicate names and storage
  failures are reported honestly.
- Compose panel (`panel:emoji_compose:<cid>`): the ordered source list
  (numbered, order = precedence), Snapshot mapping count, Add source,
  per-source Remove, **Compose snapshot**, **Refresh from sources**, Mappings
  and Back.
- Source picker (`panel:emoji_sources:<cid>:<page>`): the existing 2×5 grid
  convention, clamped pages, owner-scoped, the composed category itself is never
  a candidate, already-selected sources stay visible and marked `✓`, indices are
  re-validated on tap (`stale index` ⇒ honest "List changed — pick again"),
  duplicates and the 20-source bound are refused with a notice.
- Conflict panel: current precedence (kept source, dropped sources, per simple
  emoji) + **"✅ Confirm (first source wins)"** / Cancel — the confirm action is
  the ONLY path that turns reported precedence into stored state, and the panel
  states "Nothing has been written yet".
- Result panel: honest success (mappings/sources/conflicts resolved/dropped
  missing sources/empty sources/unresolvable entries) or the exact error
  (conflict, incomplete read, incomplete snapshot, storage, missing source,
  cycle, self, duplicate, too many, no sources, not custom, not found);
  the previous snapshot is never claimed intact unless the operation says so.
- Mappings panel / mapping detail / mapping add/edit/delete actions for a
  composed category offer **Compose/Refresh instead of manual edits**, so
  Refresh semantics can never fight hand edits (the mapping detail for a
  composed category keeps only the Compose row).
- Everything is registration-based on the existing panels/actions/inputs
  machinery: no new listener, no new client, no new loop, no new executor.

### 3.5 Tests

See §4–§5. The new suite is `tests/test_emoji_composition_phase5.py`
(80 tests).

---

## 4. Exact files changed

| File | Change |
|---|---|
| `backend/services/emoji_category_service.py` | Custom type metadata, composition section (`plan_composition`, `compose_category`, `refresh_category`, snapshot replace + rollback, cycle/validation helpers, new error codes and bounds) |
| `backend/db/client.py` | `set_emoji_category_sources` + sync worker (owner-scoped source-list persistence, honest failures) |
| `backend/bot/handlers/emoji.py` | Custom creation, compose panel, source picker, refresh/confirm actions, conflict + result panels, edit-posture guards, registration of the new panels/actions |
| `tests/test_emoji_composition_phase5.py` | NEW — 80 focused Phase 5 tests |
| `ROADMAP.md` | Current-state update: Phase 5 implemented/tested offline, §34-F status, remaining work, Phase 6/7 ordering |

**Intentionally untouched files** (verified by the diff): `backend/ai/**`
(no AI involvement, no tool), `backend/runtime/**` (no supervisor/scheduler
change), `backend/bot/router.py` and `backend/bot/handlers/emoji_replacement.py`
(the Phase 4 listener is unchanged — composition adds no listener), Phase 4
`backend/services/emoji_transformer.py` / `emoji_replacement_service.py` (no
custom-category logic), `backend/services/emoji_state_service.py` (Phase 3
contract unchanged), `backend/telegram_api/**`, `supabase/**` (no migration,
no SQL), `DATABASE_ARCHITECTURE.md` (it documents live Supabase tables only and
never contained the emoji tables — unchanged since Phase 1), `AGENTS.md`
(§15 doc-consistency rule: only the report/roadmap are phase documents).

---

## 5. Tests added (focused Phase 5, `tests/test_emoji_composition_phase5.py`, 80 tests)

- **Model (7):** custom creation records explicit type metadata; ordinary
  categories stay ordinary; the type is never inferred from the name; shared
  name uniqueness; listing carries the flag; rename keeps type + snapshot;
  delete removes the snapshot and keeps the library.
- **Composition (19):** one source; reference-only storage with no library
  duplication; multiple sources; owner order = precedence (both orders proven);
  idempotent recompose; duplicate source refused; self-composition refused;
  missing source refused honestly; foreign-owner source refused; ordinary target
  refused; 7 parametrized invalid source lists; source-list bound; empty source
  category → honest empty snapshot; unresolvable entry counted and copied
  honestly; nested Custom source flattened to concrete mappings; two-level and
  three-level cycles rejected.
- **Conflicts (4):** refused until confirmed with the full conflict record and
  nothing written; confirmed → first source wins and the dropped source is
  reported; at most one mapping per simple emoji across three sources; the
  conflict panel writes nothing until the confirm action.
- **Snapshot semantics (12):** stored rows are not live references; source edit
  / addition / mapping removal / category deletion each leave the snapshot
  untouched; the source deletion case also proves the Custom Category stays
  listed and selectable and that Refresh then reports the missing source while
  preserving the snapshot; Refresh drops a deleted source and reports it;
  Refresh refuses a newly appeared conflict until confirmed; a failed write
  keeps the previous snapshot (`rolled_back`); an incomplete mapping read fails
  the plan closed; Refresh without sources / on an ordinary category / on a
  deleted category refused; Refresh leaves the active state alone.
- **Edit posture (4):** manual edit refused, manual delete refused, mappings
  panel offers Compose/Refresh instead of "Add mapping" (ordinary panel
  unchanged), mapping detail offers Compose and no edit/delete rows.
- **UI (16):** registration of the new panels/actions; categories panel offers
  custom creation and marks custom rows; custom-creation input flow arms the
  composer; duplicate names reported honestly; precedence-ordered compose panel;
  honest missing/ordinary compose panels; `E_NOT_CUSTOM` result panel; 2×5
  source picker with clamped pages; `✓` marking + never offering itself; no
  other categories ⇒ honest panel; add-source appends in tap order and does not
  compose; duplicate tap notice; stale index honesty; remove-source draft
  update; owner isolation across all compose actions (foreign category untouched,
  no state leak); stale callbacks after deletion fail safely; every compose
  callback carries real owner-scoped ids; ≤64-byte callback data across all
  compose panels; ordinary-category surface unchanged.
- **Integration + architecture (8):** Custom Category as global default and as
  per-chat override driving the Phase 4 pipeline; toggle respected; deleted
  Custom Category fails closed; snapshot-vs-source drift proven end-to-end
  through the bridge until Refresh; Phase 4 modules contain no custom-category
  logic; no `backend.ai` / `backend.runtime` import; no
  `create_task` / `new_event_loop` / `run_until_complete` / `call_later`; no
  `re` module / `re.` usage; no `events.NewMessage` in the Phase 5 surface.

No existing test was removed, weakened, or skipped.

---

## 6. Checks actually executed and exact results

All commands were run from the repository root with the project venv
(`/home/daytona/codebase/.venv`, Python 3.10).

| Check | Command | Result (exit status) |
|---|---|---|
| Focused Phase 5 | `python -m pytest tests/test_emoji_composition_phase5.py -q` | **80 passed** (exit 0) |
| Phase 0–4 emoji + bridge regression + Phase 5 | `python -m pytest tests/test_emoji_library_import.py tests/test_emoji_category_service.py tests/test_emoji_ui.py tests/test_emoji_ui_phase2.py tests/test_emoji_set_enumeration.py tests/test_emoji_state_phase3.py tests/test_emoji_replacement_phase4.py tests/test_bridge_delivery.py tests/test_emoji_composition_phase5.py -q` | **412 passed** (exit 0) — Phase 4 baseline 332 + 80 |
| Full suite | `python -m pytest tests/ -q` | **5570 passed, 26 skipped, 3 warnings** (exit 0, 119.16s) — Phase 4 baseline 5490 + 80 |
| Compile | `python -m py_compile backend/bot/handlers/emoji.py backend/db/client.py backend/services/emoji_category_service.py tests/test_emoji_composition_phase5.py` | clean (`PY_COMPILE_OK`, exit 0) |
| Whitespace / conflict markers | `git diff --check` | clean (`DIFF_CHECK_OK`, exit 0) |
| Diff inspection | `git status --short`, `git diff --stat` | only the intended files changed (see §4) |

No live Telegram call and no live Supabase call was made anywhere in this work.

---

## 7. Architecture constraints verified

| Constraint | How it is verified |
|---|---|
| Deterministic, no AI | AST import audit on `emoji_category_service.py` and `handlers/emoji.py`: no `backend.ai` / `backend.runtime` import (test). No tool, dispatcher, provider, prompt or memory module was touched |
| No second loop / client / executor | The Phase 5 surface has no `events.NewMessage`, no `create_task`, `new_event_loop`, `run_until_complete`, `call_later` (AST + source tests); the only Telegram listener in the feature remains the Phase 4 one, and no new Telegram client/session exists |
| No regex / keyword routing | No `re` module import and no `re.` usage in the service or the UI (AST test); composition matches explicit mapping keys (`simple_emoji` dict keys) and explicit ids only |
| No second category abstraction | The Custom Category uses the existing `emoji_categories` row plus the existing `emoji_mappings` rows; `is_custom` is a flag on the same row/model, not a parallel model |
| Phase 2 conflict machinery reused | The composition conflict report mirrors the §12 “never silently overwrite” rule; no second conflict subsystem was introduced |
| Phase 3 contract unchanged | `emoji_state_service` was not modified; resolution order stays OFF ⇒ none, else valid per-chat override, else valid global default, else none; tests prove a Custom Category can be both a global default and a per-chat override |
| Phase 4 needs no special path | `emoji_transformer.py` and `emoji_replacement_service.py` contain no `is_custom` / `category_sources` / `plan_composition` reference (test) and were not modified; an end-to-end test drives the real bridge with a composed snapshot |
| Owner boundary | Every service function takes `owner_id` and every read/write is owner-scoped; UI callbacks re-validate every id against the owner (tests: foreign category rejected on panel/picker/add/refresh/confirm, nothing mutated, no state leak); callback data is never authorization |
| Confidence in limits | Bounds are explicit and tested: 20 sources, 2000 snapshot mappings, 200 graph visits, 64-byte callback data, 2×5 pages |

---

## 8. Persistence / schema status

- No SQL was executed. No Supabase project was touched. No migration file was
  created (matching Phases 1–4: the emoji tables are MANUAL-ONLY).
- Repository-side contract added: `emoji_categories.is_custom` (boolean,
  explicit type flag) and `emoji_categories.source_category_ids` (ordered id
  list). They are written **only** for custom rows, so a Supabase without the
  columns keeps ordinary category CRUD working unchanged while custom creation
  and composition report honest storage failures (no silent downgrade).
- In the in-memory fallback (the mode every offline test uses) the two fields
  are plain row keys and behave exactly as the contract describes.

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

- Constraints/indexes: no new unique constraint and no new index — the source
  list is read with its own row and is never queried by content; the existing
  `(owner_id, name)` uniqueness and mapping uniqueness are unaffected.
- RLS: unchanged. The columns live on an existing table whose policies already
  apply (service-role writes from the bot; dashboard reads through the backend
  API). No policy change is required by this addition and none was invented.

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

- No live custom-category creation, composition, refresh, conflict
  confirmation or source deletion was performed.
- No live replacement was run through a composed category, and the premium
  emoji / bridge capability question (§34-D) is still open.
- The manual SQL in §8.1 was not executed anywhere; the physical
  `emoji_categories.is_custom` / `source_category_ids` columns do not exist in
  any environment as a result of this work.

Everything above is offline behavior only: unit/regression suites plus the
in-memory fallback store and a fake helper-bot/self-client boundary.

---

## 10. Limitations (honest)

- **§34-F is not owner-approved.** The snapshot-on-compose default is
  implemented and tested, but the owner has not confirmed it. Switching to live
  references later touches only the composition functions
  (`plan_composition` / `compose_category` / `refresh_category`) — the mapping
  model, resolution boundary and replacement pipeline would not change.
- **No live schema.** Custom Category persistence needs the manual columns
  (§8.1). Until then, a Supabase-backed runtime reports honest storage failures
  for custom creation/composition.
- **Refresh is bounded, not transactional.** The repository has no
  cross-statement transaction in the db layer, so a failed snapshot write is
  undone with a compensating rollback and reported (`rolled_back`); a failed
  deletion leaves a superset reported as `E_SNAPSHOT_INCOMPLETE`. A retry
  (Refresh again) is the documented recovery.
- **Nested composition is snapshot-flattened, not live.** A Custom source is
  read at compose/refresh time; its own later refreshes do not propagate until
  the outer category is refreshed. Cycles are rejected rather than supported.
- **Composed mappings are read-only by design.** Manual add/edit/delete on a
  composed category is refused with guidance; the only edit paths are sources +
  Compose/Refresh.
- **The 20-source and 2000-mapping bounds are fixed constants**, not
  configuration; exceeding them is refused with a clear error instead of
  degrading silently.
- **Unresolvable source mappings are copied, not dropped.** They are counted
  (`unresolvable`) and reported; the resolver keeps failing closed on them at
  replacement time (Phase 4 behavior unchanged).
- **Not implemented here** (unchanged from before this phase): media/caption
  reconstruction (§23), reactions (§27), owner-visible failure surfacing
  (§28), flood/rate validation (§17), destination-type permission handling
  (§18).

---

## 11. Unresolved owner decisions

| # | Decision | Current status after Phase 5 |
|---|---|---|
| A | Active category scope | Implemented as documented (global default + per-chat override). Confirmation still OPEN |
| B | Reconstruction ordering | Implemented as documented (send-first). Confirmation still OPEN |
| C | Bridge bot identity | Implemented as documented (existing helper bot). Confirmation still OPEN |
| D | Premium-visual delivery mode | UNRESOLVED — no alt-text fallback, no entitlement probing, no fabricated success |
| E | Mapping persistence shape | Default proposal implemented (dedicated tables). Owner sign-off still OPEN |
| F | **Custom composition semantics** | **Phase 5 implemented the documented default (snapshot-on-compose with explicit Refresh). Owner confirmation remains OPEN — this phase does NOT claim owner approval.** |
| G | Loop-prevention mechanism | Implemented as documented (structural, no visible marker). Confirmation still OPEN |

---

## 12. Remaining work / next phase

- **Next: Phase 6 — Reactions (§27)**: `telegram_api/reactions.py` wrapper,
  reaction service, Glass UI react action. NOT started; no reaction code exists
  in the repository.
- **Then: Phase 7 — Hardening (§28)**: error/capability handling completion,
  test-plan completion (§30), validation-checklist execution (§31),
  documentation updates.
- Live-Telegram validation of the whole feature (including a composed category
  driving a real replacement) remains pending the owner's environment.
- The manual SQL in §8.1 must be applied by the owner before Custom Category
  persistence works against a real Supabase project.

---

## 13. Delivery metadata

A commit cannot contain its own SHA, so this section records the Phase 5
commit and the push verification; the delivery-metadata commit that carries
these values is the only commit after it.

| Field | Value |
|---|---|
| Branch | `m14-stt` |
| Phase 5 commit | `3901037` — `feat(emoji): implement Phase 5 — custom category composition (snapshot-on-compose, conflict confirmation, cycle rejection)` |
| Commit contents | the 6 intended files only (`IMPLEMENTATION_REPORT.md`, `ROADMAP.md`, `backend/bot/handlers/emoji.py`, `backend/db/client.py`, `backend/services/emoji_category_service.py`, `tests/test_emoji_composition_phase5.py`) |
| Push result | `6ca2db1..3901037  m14-stt -> main` (fast-forward, exit 0) |
| Remote main HEAD | `39010378203251fa04417df98ce56912177169f9` |
| Final worktree state | clean — verified after the delivery-metadata commit (see §14) |

---

## 14. Push verification (recorded after delivery)

Run before the delivery-metadata commit, from the repository root on branch
`m14-stt`:

- `git fetch origin main` → fetched `main` into `FETCH_HEAD` (exit 0)
- `git push origin m14-stt:main` → `6ca2db1..3901037  m14-stt -> main` (exit 0)
- `git rev-parse HEAD` → `39010378203251fa04417df98ce56912177169f9`
- `git rev-parse origin/main` → `39010378203251fa04417df98ce56912177169f9`
- `git ls-remote origin refs/heads/main` →
  `39010378203251fa04417df98ce56912177169f9\trefs/heads/main`

Local `HEAD`, the fetched `origin/main` and the live remote `refs/heads/main`
are the same commit — the Phase 5 changes exist on the remote, not only
locally. After the delivery-metadata commit (this file's §13–§14 values only),
`git status --short` is empty and `git log --oneline -2` lists `3901037`
directly beneath the metadata commit, so the final worktree is clean and
`HEAD == origin/main` still holds.
