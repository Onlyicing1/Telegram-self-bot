# Implementation Report

> **Scope:** this report describes the work performed in the current change
> set only. It is rewritten (not appended) each time; historical narrative
> lives in git history. Nothing here claims live-Telegram verification —
> see §6/§7 for exactly what was and was not verified.

**Feature: Emoji & Reaction — Phase 1 (Library & Import) code slice
completed.**

**Current phase: Phase 1 — Library & Import (message-level slice
IMPLEMENTED + TESTED).**
**Next: Phase 1 remainder (sticker-set resolution/enumeration + Glass UI
import report panel, ROADMAP §9), then Phase 2 — Categories & Mappings
(§32). Phase 0 owner decisions §34-D/§34-E remain OPEN.**

Phase 1 of the current-feature roadmap (`ROADMAP.md` §32) lists the domain
model (§7), library storage, the Saved Messages collection import (§8/§9),
and an import report panel. This change set delivers the library storage,
the deterministic message-level import, and the honest report **as a service
return value**; set enumeration and the Glass UI panel are explicitly NOT
implemented here (§5/§6).

---

## 1. What was implemented

### 1.1 Extraction — Phase 0 representation reused, no second one
(`backend/services/emoji_library_service.py`, new)

- `extract_custom_emoji_records(message)` is a pure function over the
  Phase 0 serialized message dict (`serialize_message` output). It
  recognizes **entities, not text**: only `MessageEntityCustomEmoji`
  entities produce records. Plain Unicode emoji (no such entity) and
  unrelated entity types (bold, text-url, …) are silently ignored — they
  are neither imported nor counted as malformed.
- A record is `{document_id, alt_text, source_msg_id, source}`:
  - `document_id` — the Telegram custom-emoji/document identity, validated
    (int or digit-string, positive; bools/floats/missing rejected),
  - `alt_text` — the exact Unicode text span the entity covers, resolved
    from UTF-16 offsets through Phase 0 `utf16_index_at` (fail closed:
    out-of-range and mid-surrogate spans are malformed, never clamped),
  - `source_msg_id` — the Saved Messages message that carried it (None when
    the message id is absent/invalid),
  - `source` — always `"imported"` (ROADMAP §7).
- Malformed payloads (missing/invalid document id, negative/zero/non-int
  offsets, spans outside the text, custom-emoji entities on empty text) are
  skipped and counted honestly. Per-message entity processing is bounded at
  `MAX_ENTITIES_PER_MESSAGE = 100` with an honest `hit_entity_limit` flag.
- **No sticker-set metadata is persisted**: it is not available from a
  message entity, and nothing is fabricated (ROADMAP §9 set resolution
  remains open).

### 1.2 Import — bounded, deterministic, fail-closed
(`import_from_saved_messages(client, owner_id, …)`)

- Scans **Saved Messages (`"me"`)** through the existing facade
  `backend.telegram_api.messages.iter_messages` (the self client is passed
  in by the caller — no second client), newest-first, paged with an
  exclusive `max_id` cursor that must strictly decrease (deterministic,
  no offset drift, no re-reads). Each page fetch is wrapped in
  `rpc_await` with a bounded timeout.
- **Collect-then-persist**: a Telegram error or page timeout aborts with an
  honest report and persists NOTHING. An unreadable durable library (failed
  Supabase dedup read) aborts BEFORE any scan. A collection failure never
  produces a partial silent import.
- **Deduplication** happens against (a) the durable library (read once,
  fail-closed) and (b) the current scan — both at classification time, so
  the record budget counts only genuinely new entries. Persistence then
  inserts each candidate; per-insert outcomes are reported as
  `imported`/`failed`.
- One module-level `asyncio.Lock` serializes imports so dedup decisions
  stay deterministic under concurrency (same pattern as
  `get_next_save_code`).
- The returned report always carries: `ok`, `error`, `storage`
  (`supabase`/`memory`), `pages`, `scanned_messages`,
  `custom_emoji_seen`, `malformed_entities`, `imported`, `duplicates`,
  `failed`, `library_total`, `end_reached`, `hit_scan_limit`,
  `hit_record_limit`, `hit_entity_limit`. For a completed import the
  invariant `custom_emoji_seen == imported + duplicates + failed` holds.

### 1.3 Persistence — existing db/client.py pattern, no second DB layer
(`backend/db/client.py`)

- `_fallback` gains `emoji_library` (in-memory fallback store).
- `list_emoji_document_ids(owner_id)` — dedup read; returns **`None`
  (never `[]`) when Supabase is configured but the durable read fails**, so
  the importer can fail closed instead of deduping against RAM (the
  `config_store` degraded-read spirit, without inventing new keys). When no
  Supabase is configured the fallback list is the authoritative store. The
  read is bounded at `_EMOJI_MAX_ROWS = 5000`.
- `insert_emoji_entry(row)` — same shape as `insert_save`: fallback path
  refuses duplicates, Supabase path inserts, every failure is logged +
  `record_event`'d and returns `None`; never raises.
- `list_emoji_entries(owner_id, limit, offset)` — newest-first
  `(rows, total)` listing for tests and the future browser panel, mirroring
  `list_saves`.
- No SQL executed, no migration added, no live table touched.

## 2. Files changed

| File | Change |
|---|---|
| `backend/services/emoji_library_service.py` | NEW — extraction (`extract_custom_emoji_records`) + bounded import (`import_from_saved_messages`) + report contract |
| `backend/db/client.py` | `emoji_library` added to `_fallback`; NEW `list_emoji_document_ids` / `insert_emoji_entry` / `list_emoji_entries` (+ `_sync` helpers, `record_event`, watchdog-dispatched via `_run_sync`) |
| `tests/test_emoji_library_import.py` | NEW — 50 focused tests (§4) |
| `ROADMAP.md` | Current-state updates: header, §2, §5.6, §7, §8, §9, §29, §30, §31, §32, §35 — implemented slices marked, later phases explicitly left unimplemented |
| `IMPLEMENTATION_REPORT.md` | This rewrite |

No other file was touched: no handler/router registration, no tool
registry entry, no Glass UI panel, no AI module, no schema/SQL file, no
`DATABASE_ARCHITECTURE.md`, no `AGENTS.md`, no bridge/`_helpers.py`
changes (Phase 0 untouched and still green).

## 3. Library / import data model (MANUAL-ONLY schema — never executed)

Persisted row (all fields actually available from the import path):

| Column | Type | Meaning |
|---|---|---|
| `id` | bigserial PK | row id (house style) |
| `owner_id` | bigint NOT NULL | library owner |
| `document_id` | bigint NOT NULL | Telegram custom-emoji document id — the dedup identity |
| `alt_text` | text NOT NULL DEFAULT `''` | exact Unicode alt span from the carrying message |
| `source` | text NOT NULL DEFAULT `'imported'` | §7 source (future: `'manual'`) |
| `source_msg_id` | bigint NULL | Saved Messages message that carried it |
| `created_at` | timestamptz NOT NULL DEFAULT `now()` | first-import time |
| — | `UNIQUE (owner_id, document_id)` | dedup backstop for the Supabase path |

```sql
-- MANUAL-ONLY — documented, NEVER executed by this repository.
CREATE TABLE IF NOT EXISTS emoji_library (
  id            bigserial PRIMARY KEY,
  owner_id      bigint      NOT NULL,
  document_id   bigint      NOT NULL,
  alt_text      text        NOT NULL DEFAULT '',
  source        text        NOT NULL DEFAULT 'imported',
  source_msg_id bigint,
  created_at    timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT emoji_library_owner_document_key UNIQUE (owner_id, document_id)
);
-- RLS per house style (service-role writes, SELECT-only policies) — owner action required.
```

`set_id`/`set_short_name` are intentionally absent — not available from
message entities; deferred to §9 set resolution. Categories, mappings, and
runtime state (§7) are later phases and have no shape here.

## 4. Import semantics and bounds

| Case | Behavior |
|---|---|
| First import | scan → validate → persist new records; report counts + `library_total` |
| Repeated import | every occurrence counts as `duplicates`; `imported = 0`; rows unchanged (idempotent) |
| Same emoji in several messages (one run) | first occurrence (newest-first scan) wins; later ones counted `duplicates` |
| Malformed/incomplete entity | skipped + `malformed_entities`; never stored |
| Message without entities / without custom emoji | scanned, contributes nothing, no error |
| Ordinary Unicode emoji | no custom-emoji entity → never imported |
| Inaccessible/deleted content | deleted messages simply aren't returned; empty/attribute-less messages serialize to no-op records without crashing |
| Telegram API error / page timeout | `ok=False`, `error` set, **nothing persisted** (fail closed) |
| Durable library read failure | abort **before any scan**, `library_total=None`, zero RPCs to Telegram |
| Insert failure | per-row `failed` count, `ok=False`; no silent RAM fallback while Supabase is configured |
| Pagination boundary | newest-first, exclusive `max_id` cursor; empty or short page ⇒ `end_reached=True`; cursor must strictly decrease (defensive stop) |
| Scan budget exhausted | `hit_scan_limit=True` (end not confirmed) |
| Record budget exhausted | `hit_record_limit=True`, collection stops |
| Entity budget | >100 entities on one message ⇒ first 100 processed, `hit_entity_limit=True` |

Bounds (caller values clamped to the hard caps):

| Bound | Default | Hard cap |
|---|---|---|
| messages scanned per import | 200 | 2000 |
| page size | 50 | 200 |
| new records per import | 500 | 5000 |
| entities processed per message | 100 | — |
| page fetch timeout | 15 s | 0.01–120 s |
| durable dedup read | — | 5000 rows |

Context isolation: the service imports nothing from `backend.ai` (verified
by an AST import-audit test); no chat history, quoted-message context,
conversation state, or interpretation is consulted. Architecture: no second
client/loop/scheduler/executor, no forwarding path, no new router — pinned
by source- and signature-level tests.

## 5. Tests executed and actual results

All commands run with the project venv (`/home/daytona/codebase/.venv`),
exit statuses captured:

1. `py_compile` on the three changed/added files → clean.
2. `pytest tests/test_emoji_library_import.py -q` → **50 passed** (0.49s).
   Coverage: valid record creation + exact Unicode alt preservation
   (Persian/supplementary-plane spans), document-id validation, plain
   Unicode + unrelated-entity exclusion, malformed payload fail-closed
   (missing/bool/float/zero/negative id, bad offsets, out-of-range and
   mid-surrogate spans, empty text), entity processing bound, Phase 0
   serialization round-trip, first import + persistence (fallback path),
   repeat-import idempotency, in-scan + durable dedup, deterministic
   `max_id` pagination asserted call-by-call, scan/record/entity bounds +
   clamping, empty history, inaccessible entries, collection API failure /
   timeout / durable-read-abort / insert-failure honesty (incl. the
   completeness invariant), faked-Supabase persistence path + exact insert
   payload, fallback uniqueness + listing pagination, context isolation
   (AST import audit), and architecture constraints (no second
   client/loop/scheduler/executor, no forwarding, exact signature).
3. `pytest tests/test_bridge_delivery.py tests/test_12_save_engine.py
   tests/test_save_v2_telegram_sync.py
   tests/test_telegram_chat_context.py -q` → **118 passed** (0.54s)
   (Phase 0 regression + regression around the touched db/client surface).
4. `pytest tests/ -q` from the worktree root → **5230 passed, 26 skipped**
   (118.88s) = Phase 0 baseline 5180 + 50 new tests — no regressions.
   (Note: the suite must run with the worktree root as cwd — its
   source-inspection tests resolve `backend/…` paths relative to cwd.)

## 6. Limitations / intentionally untouched / deferred

- **No live Telegram verification** (import included): everything is faked
  at the consumed Telethon surface (`iter_messages` async generator, real
  `MessageEntityCustomEmoji` objects flowing through Phase 0
  `serialize_message`). Saved Messages behavior, rate limits, and real
  entity shapes are unverified live (ROADMAP §31 checklist remains open).
- **No Supabase/live-DB verification**: no SQL executed, no migration
  added, no live table touched — the `emoji_library` table does not exist
  anywhere yet. Until the owner applies the §3 manual-only schema, a
  configured Supabase will report `failed` inserts honestly (in-memory
  fallback covers development when Supabase is not configured).
- Sticker-set resolution/enumeration (§9), library browser panel, Glass UI
  import report panel, categories/mappings/toggle/transformer/
  reconstructor/reactions — NOT implemented (later phases).
- No tool-registry entry and no handler wiring: the service is a dormant
  extension point invoked by tests today (same posture as Phase 0's
  bridge); AI never performs raw Telegram RPCs, and no command router was
  added.
- Supabase-path dedup relies on service-side classification under the
  import lock plus the manual `UNIQUE (owner_id, document_id)` index as
  backstop; libraries beyond the 5000-row dedup read depend on that index.
- §34 owner decisions D (bridge capability) and E (mapping persistence
  shape) remain OPEN; no decision was resolved silently.
- **Intentionally untouched:** `DATABASE_ARCHITECTURE.md`, `AGENTS.md`,
  `INVESTIGATION.md`, `supabase/migrations/`, `backend/telegram_api/*`
  (Phase 0), `backend/bot/router.py` + all handlers, every `backend/ai/*`
  module, and the outer repository checkout outside this worktree.

## 7. Git status

- Single commit on top of `ddfbb1a` in this worktree (branch `m14-stt`
  tracking `origin/main`), containing exactly the five files in §2 —
  inspected via `git status`/`git diff` before staging; no unrelated or
  pre-existing changes included.
- Delivered by pushing to `origin/main`; local HEAD verified equal to
  `origin/main` and the working tree verified clean after the push (exact
  SHAs recorded in the delivery report accompanying this change).
- Verification before commit: focused tests (§4.2–4.3), full suite
  (§4.4), `py_compile` (§4.1), `git diff --check` — all clean.

## 8. Current phase / next phase

- **Current:** Phase 1 — Library & Import — message-level slice
  IMPLEMENTED + TESTED (library storage, deterministic bounded import,
  honest report, 50 focused tests + full suite green).
- **Next:** Phase 1 remainder — sticker-set resolution/enumeration and the
  Glass UI import report panel (ROADMAP §9), owner decisions §34-D/§34-E,
  then Phase 2 — Categories & Mappings (§32).
