# Implementation Report

> **Scope:** this report describes the work performed in the current change
> set only. It is rewritten (not appended) each time; historical narrative
> lives in git history. Nothing here claims live-Telegram or live-Supabase
> verification — see §12 for exactly what was and was not verified.

**Feature: Emoji & Reaction — Phase 1 COMPLETE (Library & Import, including
the Phase 1 remainder).**

**Current phase: Phase 1 — Library & Import IMPLEMENTED + TESTED in full.**
**Next: Phase 2 — Categories & Mappings (ROADMAP §32). Phase 0 owner
decisions §34-D/§34-E remain OPEN.**

The Phase 1 message-level slice (library storage, deterministic bounded
Saved Messages import, honest report as a service return value) was
delivered in the previous commit. This change set completes Phase 1 by
delivering (a) sticker-set resolution/enumeration for the imported custom
emojis and (b) the Glass UI import/report panel plus the 2×5 library
browser. Phase 2 categories/mappings are explicitly NOT implemented.

---

## 1. What was implemented

### 1.1 Set-enumeration facade (`backend/telegram_api/custom_emoji.py`, new)

Typed, bounded wrappers over the two Telegram TL requests enrichment needs
— same conventions as `backend/telegram_api/messages.py` (short bounded
calls through `guarded_await`, exceptions normalized to
`TelegramAPIError`/`TelegramTimeoutError`, plain-dict results; callers never
touch Telethon objects):

- `get_custom_emoji_documents(client, document_ids)` — resolves custom-emoji
  document ids into `[{document_id, alt, set}]` via
  `messages.GetCustomEmojiDocumentsRequest`. One document per id Telegram
  actually returned **with a `DocumentAttributeCustomEmoji` attribute**;
  anything else is honestly absent. `alt` is the attribute's Unicode
  fallback text (whatever Telegram provides); `set` is the REAL
  Telegram-provided set identity (`{"kind": "id", "id", "access_hash"}` or
  `{"kind": "short_name", "short_name"}`) decoded from the attribute's
  `stickerset` field, or `None` when the document carries no usable
  identity. Invalid/foreign ids are dropped; the request is clamped at
  `MAX_DOCUMENTS_PER_CALL = 100` ids.
- `get_sticker_set(client, set_ref)` — fetches ONE sticker/custom-emoji set
  with its full member list via `messages.GetStickerSetRequest(stickerset,
  hash=0)` (hash=0 forces the full list instead of a not-modified stub).
  Returns `{set_id, access_hash, title, short_name, count, members}` where
  each member is `{document_id, alt}` — only documents carrying a
  custom-emoji attribute appear as members (a sticker set requested by
  mistake contributes no fake emoji records). Unusable identities raise
  `TelegramAPIError`; failures/timeout normalize like every facade call.

**Honesty contract:** a document Telegram did not return, a document
without a custom-emoji attribute, and a document without a usable set
identity are all reported as absent/`set: None` — nothing about set
membership is ever inferred, guessed, or fabricated here.

### 1.2 Bounded enrichment stage in the importer
(`backend/services/emoji_library_service.py` extended)

`import_from_saved_messages(client, owner_id, *, max_messages, page_size,
max_records, max_set_records, page_timeout)` grew ONE optional bounded
parameter (`max_set_records`, default 500, hard cap 2000) and one
BEST-EFFORT enrichment stage between the scan and persistence:

1. **Document resolution** — each newly collected candidate `document_id`
   is resolved once, in `MAX_DOC_RESOLVE_BATCH = 50`-sized chunks, at most
   `MAX_DOC_RESOLVE_CALLS = 10` chunks per import (≤500 documents), against
   the SAME bounded RPC timeout as the scan.
2. **Identity grouping** — resolved documents are grouped by their real
   set identity in first-appearance order of the scan; at most
   `MAX_SETS_PER_IMPORT = 20` unique sets per import are kept (excess is
   counted as `hit_set_limit`).
3. **Set enumeration** — each unique set is enumerated exactly once, one
   RPC each; at most `MAX_SET_MEMBERS_PER_SET = 200` members are processed
   per set. A member is classified exactly once: already in the scan
   candidates, the durable library, or seen as another member ⇒
   `set_duplicates` (counted, never stored twice); new members become
   library records up to the `set_record_limit` budget.

Set members persist through the SAME `db_client.insert_emoji_entry` path
with the same `(owner_id, document_id)` deduplication. Their row shape
identifies their origin honestly: `source_msg_id = None` (no message
carried them) and `alt_text` is Telegram's own alt for the member document.
**No schema change and no new column is required or made:** set identity is
used transiently to resolve members; `set_id`/`set_short_name` remain
unpersisted exactly as before.

**Failure semantics (enrichment is best-effort by contract):** a set-stage
failure is counted and reported (`set_error` + `degraded`) and NEVER aborts
the import — the message-level records the scan successfully collected
still persist. The four stages are distinguishable in the report:
document-resolution failure, set resolution failure (non-dict set info /
`SET_NOT_FOUND`), set enumeration failure, and set-member persistence
failure (`set_failed`, which DOES make `ok=False` since it means the
library is missing rows the report claims). Report keys added: `degraded`,
`set_error`, `documents_resolved`, `unresolved_documents`,
`documents_without_set`, `sets_resolved`, `set_members_seen`,
`set_members_malformed`, `set_imported`, `set_duplicates`, `set_failed`,
`hit_set_limit`, `hit_set_member_limit`. For a fully successful import the
invariant `custom_emoji_seen + set_members_seen == imported + duplicates +
failed + set_imported + set_duplicates + set_failed` holds.

**Existing importer guarantees are untouched and re-pinned by tests:**
newest-first scan, exclusive strictly-decreasing `max_id` cursor, bounded
scan/page/record/entity budgets, bounded per-page RPC timeout, import lock
for deterministic dedup, collect-then-persist fail-closed (a Telegram or
durable-read failure persists NOTHING), honest per-insert outcomes. The
enrichment stage runs INSIDE the same import lock, after the scan and
before any write.

### 1.3 Glass UI panels (`backend/bot/handlers/emoji.py`, new) + wiring

Standard panel machinery only — `register_panel`/`register_action` from
`backend.helper.panels`, the `InlinePanelBuilder` button conventions, the
`panel:*`/`action:*` callback routing (owner-authorized by the existing
callback router's `is_owner` gate), and the existing page/`truncate_
callback_data` prior art. No new UI framework, no `events.NewMessage`
handler, no second loop, no scheduler, no forwarding.

- **`😀 Emoji` panel** (parent `menu`; the mother menu in
  `backend/bot/handlers/misc.py` gains the `😀 Emoji` row) — shows the
  library total (honest `unknown` on a failed read; empty-state text when
  zero) with two actions: `⬇ Import from Saved Messages` and
  `📚 Library`.
- **Import action (`action:emoji_import`)** — runs the REAL bounded service
  (`import_from_saved_messages`) with the self client from
  `backend.helper.inline_engine.get_self_client()` and the owner from
  `get_owner_id()`. Missing-client/owner and unexpected crashes fail
  honestly (`! …`). The report body is rendered STRAIGHT from the service
  result: `✓ Import complete`, or `✗ …` with the exact failure/error
  string, or `◌ …` degradation (set enumeration error, unresolved
  documents, setless documents, budget flags) — never a disguised success.
  Counters shown are the user-facing ones (scanned/seen/bad, imported/
  duplicates/failed, sets resolved/members imported/duplicates/failed,
  library total, end-of-Saved-Messages, and nudges to re-run when a scan
  or record budget stopped the run). No developer internals, stack traces,
  or raw RPC names leak. The panel offers `↻ Import again` and `📚 Library`.
- **`📚 Library` browser** (`panel:emoji_library`, parent `emoji`) — 2×5
  grid (10 entries/page, two buttons per row) over
  `db_client.list_emoji_entries`, newest-first, with a clamp-safe
  `[◀] [page/pages] [▶]` pager row. Each entry button opens
  **`panel:emoji_entry:<page>:<idx>`** — a compact detail (alt text,
  document id, origin — `Saved Messages #id` or `Set scan` — and added
  date), failing honestly (`Entry not found`) on a stale index after
  library changes. The browser displays ONLY what the rows actually
  contain; no set/category metadata is displayed or implied (it is not
  persisted).

`backend/bot/router.py` registers the module (`emoji.register`) alongside
the other handlers — registration isolation unchanged (a module crash
during registration cannot take down the rest).

### 1.4 The previous Phase 1 slice (unchanged here)

`extract_custom_emoji_records` (entity-based recognition from the Phase 0
serialized representation, UTF-16 span resolution, bounded per-message
entity processing) and the original import semantics/bounds/persistence
(`backend/db/client.py` `emoji_library` helpers) are documented in the
previous report and are only referenced here; the only service signature
change is the added optional `max_set_records` budget.

---

## 2. Files changed

| File | Change |
|---|---|
| `backend/telegram_api/custom_emoji.py` | NEW — document + sticker-set resolution facade (§1.1) |
| `backend/services/emoji_library_service.py` | bounded best-effort enrichment stage (`_enumerate_sets`, set-member persistence, `set_*`/`degraded` report keys, `max_set_records` budget) |
| `backend/bot/handlers/emoji.py` | NEW — `😀 Emoji` panel, import action, `📚 Library` 2×5 browser, entry detail (§1.3) |
| `backend/bot/router.py` | register the `emoji` handler module |
| `backend/bot/handlers/misc.py` | mother menu gains the `😀 Emoji` row |
| `tests/test_emoji_set_enumeration.py` | NEW — 27 focused tests (§10) |
| `tests/test_emoji_ui.py` | NEW — 18 focused tests (§10) |
| `tests/test_emoji_library_import.py` | signature-pinning test updated for the `max_set_records` parameter |
| `ROADMAP.md` | current-state updates (header, §2, §5.4/§5.6, §7, §8, §9, §25, §30, §31, §32, §35) |
| `IMPLEMENTATION_REPORT.md` | this rewrite |

Intentionally untouched: `DATABASE_ARCHITECTURE.md`, `AGENTS.md`,
`INVESTIGATION.md`, `supabase/` (no migration), `backend/db/client.py`,
`backend/telegram_api/_helpers.py`/`bridge.py`/`messages.py`/`api.py`
(Phase 0 untouched), every `backend/ai/*` module, `backend/helper/*`, the
web dashboard, and everything outside this feature. **No SQL executed; no
live DB touched.**

---

## 3. Data model (MANUAL-ONLY schema — unchanged from Phase 1)

The physical `emoji_library` schema is EXACTLY the Phase 1 one (owner_id,
document_id, alt_text, source, source_msg_id, created_at,
`UNIQUE (owner_id, document_id)`) — documented, never executed by this
repository, and unchanged in this change set:

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

DELIBERATE: `set_id`/`set_short_name` are still NOT persisted. Enumeration
needs the set identity only transiently (to fetch members); once members
are resolved, the library's granularity — one row per emoji document — is
sufficient, and inventing set columns now would be speculative schema
expansion ahead of Phase 2's category/mapping design. Set provenance is
implicit: members imported through set enumeration carry `source_msg_id =
NULL` + Telegram's own alt (surfaced honestly as `Set scan` in the UI).

---

## 4. Enrichment semantics and bounds

| Case | Behavior |
|---|---|
| Candidate emoji resolves with a set identity | its set is resolved once and members enter the library (dedup) |
| Several candidates share one set | resolved/enumerated ONCE; the result reused for all of them |
| Candidate document Telegram does not return | `unresolved_documents` + `degraded`; never guessed |
| Document without custom-emoji attribute | excluded by the facade; counted unresolved |
| Document without usable set identity | `documents_without_set` + `degraded`; never fabricated |
| Set RPC rejected (e.g. `SET_ID_INVALID`, permissions) | enumeration for that import stops, `set_error` + `degraded`; already-resolved sets keep their members |
| Set info missing | same as a set failure (honest, fail-closed) |
| Member persistence failure | `set_failed` count; `ok=False` |
| Set-member budget(s) exhausted | `hit_set_limit` / `hit_set_member_limit` + `degraded` |
| Import with no new candidates | enrichment doesn't run at all (zero extra RPCs) |
| Message-scan failure | enrichment never runs; NOTHING persists (existing fail-closed contract) |

Bounds (callers clamped to the hard caps — enrichment-specific rows bold):

| Bound | Default | Hard cap |
|---|---|---|
| **documents resolved per import** | **all candidates** | **500 (10 chunks × 50)** |
| **document ids per resolution RPC** | — | **50 (service) / 100 (facade hard guard)** |
| **unique sets enumerated per import** | — | **20** |
| **members processed per set** | — | **200** |
| **new set-member records per import** | 500 | 2000 |
| messages scanned / page size / records / entities / timeout | (unchanged Phase 1 bounds) | (unchanged) |

No unbounded Telegram pagination exists anywhere in the path; every RPC is
wrapped in the same bounded timeout as the scan and normalized
(`asyncio.CancelledError` always re-raised).

---

## 5. UI behavior (Glass UI)

| Surface | Behavior |
|---|---|
| Mother menu | new `😀 Emoji` row → `panel:emoji` |
| `😀 Emoji` panel | library total (or honest `unknown`/empty text) + `⬇ Import` + `📚 Library` |
| Import tap | runs the real bounded service; panel edits in place to the honest report (success `✓` / failed `✗` with reason / degraded `◌` with counters), then `↻ Import again` / `📚 Library` |
| `📚 Library` | 2×5 browser, 10/page, clamp-safe ◀/page/pages/▶ pager, empty-state honest |
| Entry tap | detail: alt text, document id, origin (`Saved Messages #id` / `Set scan`), added date; stale index ⇒ `Entry not found` honestly |

Owner-only by the existing callback router (`is_owner` gate on every
callback); no keyword/regex routing is involved — activation is a panel
button only.

---

## 6. Architecture / context isolation

- The service stays deterministic and context-isolated: no `backend.ai`
  import (AST-audited in the Phase 1 tests, still green), no chat history,
  no reply text, no sender info, no conversational inference, no AI.
- No second Telegram API abstraction: the facade extends the existing
  `backend/telegram_api` conventions; the UI reuses the self client from
  `inline_engine` and the existing panel/callback machinery.
- No second client, update loop, scheduler, executor, or repository was
  added (asserted by source-level tests for BOTH new modules; the Glass UI
  test file grep-scans `backend/bot/handlers/emoji.py` for forbidden
  constructs — `TelegramClient`, `create_task`, `events.NewMessage`,
  `forward_messages`, …).
- Telegram RPCs bounded everywhere (facade guard + per-call timeout).

---

## 7. What was intentionally NOT implemented (later phases)

Phase 2+ per ROADMAP §32: category CRUD, mapping editor/uniqueness/conflict
UI, active category + replacement toggle, transformer/reconstructor/loop
prevention, custom category composition, the reaction subsystem, §34-D/§34-E
owner decisions. No replacement of messages happens in this change set; no
AI tools were registered; no `set_id`/`set_short_name` persistence; no
library deletion UI (deletion semantics are a §34-level decision).

---

## 8. Tests added (this change set)

### 8.1 `tests/test_emoji_set_enumeration.py` — 27 tests

Offline, at the REAL TL surface (`client(request)` + real
`DocumentAttributeCustomEmoji` / `InputStickerSetID` / `InputStickerSetShortName`):

- **Facade (11):** document resolution with id-based and short-name set
  identities; absent document honestly missing; attribute-less document not
  a custom emoji; unusable set identity ⇒ `set: None`; invalid-id dropping +
  `MAX_DOCUMENTS_PER_CALL` clamping (single RPC asserted); API-error and
  timeout normalization; set-member dict shape; unusable/unparseable set
  reference rejection; set failure normalization.
- **Service enrichment (10):** members collected as library records
  (correct row shape incl. `source_msg_id=None`); one chunked resolution +
  one enumeration for a shared set; duplicate classification (repeated
  member / library hit / scan candidate); unresolved + setless counting;
  enumeration failure reported-not-raised with partial results kept;
  document-resolution failure stops cleanly; ≤ `MAX_SETS_PER_IMPORT` sets
  with `hit_set_limit`; ≤ `MAX_SET_MEMBERS_PER_SET` per set; set-record
  budget with its own flag; first-appearance-order determinism across runs.
- **Import integration (6):** full run persists scan records + deduplicated
  set members and satisfies the report invariant; repeat-run dedup
  (including that no new candidates ⇒ no enrichment RPCs at all); degraded
  report on enumeration failure while the scan record still persists;
  degraded on unresolved documents; insert-failure visibility
  (`ok=False`, `failed`/`set_failed` counts) against a failing faked
  Supabase; scan failure still aborts before any persistence.

### 8.2 `tests/test_emoji_ui.py` — 18 tests

Offline, panels driven directly (no Telegram):

- **Registration/menu (3):** panels `emoji`/`emoji_library`/`emoji_entry` +
  `action:emoji_import` registered; mother menu carries `panel:emoji`;
  every library-button callback ≤64 bytes.
- **Main panel (2):** counters + actions; empty state offers the import.
- **Library browser (5):** first page = exactly 10 entry buttons in a 2×5
  grid + correct pager; second page shows the remainder + `page 2/2`;
  out-of-range page clamps; empty state honest; missing alt text falls
  back to `·`.
- **Entry detail (3):** shows only what the row has (alt, id, Received-from
  origin, date); set members honestly marked `Set scan`; stale index ⇒
  `not found`.
- **Import action + honesty (5):** success path runs the REAL service
  (self-client faked at the TL boundary; a set member imported and counted
  `+1`); degraded rendering (`◌` + enumeration failure text) is NOT a
  success; missing self client fails honestly; report renderer covers
  failure (`✗` + reason) and budget states (re-run nudge); source scan
  proves no second client/loop/scheduler/forwarding in the UI module.

### 8.3 Regression coverage

`tests/test_emoji_library_import.py` (50) stays green — only its
signature-pinning test was updated for the added optional `max_set_records`
parameter (the importer guarantees it pins are unchanged). Phase 0
(`tests/test_bridge_delivery.py`, 22) untouched and green.

---

## 9. Commands executed and actual results

All commands run from the worktree root (`/home/daytona/codebase`) with the
project venv; exit statuses captured:

1. `py_compile` on all changed/added Python files (facade, service, handler,
   router, misc, three test files) → **clean**.
2. `pytest tests/test_emoji_library_import.py tests/test_emoji_set_enumeration.py
   tests/test_emoji_ui.py tests/test_bridge_delivery.py -q` → **117 passed**
   (0.50s) — Phase 1 focused + Phase 0 regression.
3. `pytest tests/ -q` (full suite, worktree root as cwd) → **5275 passed,
   26 skipped** (119.97s) = Phase 1 baseline 5230 + 45 new tests — no
   regressions.
4. `git diff --check` → clean.

## 10. What was NOT tested / NOT verified

- **Live Telegram verification: NOT performed.** Saved Messages scans, real
  `GetCustomEmojiDocumentsRequest`/`GetStickerSetRequest` responses,
  premium-set gating, flood/rate behavior, entity shapes on real sets, and
  the inline Glass UI rendering are unverified live (ROADMAP §31 checklist
  remains open; ROADMAP §33 blocker 3 stays open).
- **Live Supabase verification: NOT performed.** No SQL executed, no
  migration added, no live table touched. The `emoji_library` physical
  table still does not exist anywhere; until the owner applies the §3
  manual-only schema, a configured Supabase reports insert failures
  honestly (set members included).
- Mocks/fakes sit at the consumed Telethon surface (`client(request)` TL
  boundary with real TL types, `client.iter_messages` async generator) —
  by design, per the no-live-Telegram-tests requirement.

## 11. Limitations / honest notes

- Enrichment is BEST-EFFORT by contract: a Telegram-side set failure never
  blocks the message-level import (the report distinguishes the two, and
  `ok` stays truthful — only persistence failures and import aborts make it
  `False`).
- ≤500 candidate documents, ≤20 unique sets and ≤200 members per set are
  per-import bounds (deliberately small, deterministic, and cheap);
  larger collections arrive across repeated imports (the panel says so).
- Set identity relies on REAL Telegram-provided `stickerset` references;
  documents that genuinely lack one are counted, not fabricated.
- The library browser shows alt text/id/origin/date only — set/category
  metadata is not persisted (§3) and is therefore not shown (never
  pretended).
- The import panel reports counts and budgets in user-facing wording; raw
  RPC names never surface (the report line keeps the service's one-line
  failure reason, which is already plain-language).
- Owners of large libraries will re-run Import to page through budgets;
  auto-chaining budgets is deliberately deferred to keep behavior bounded
  and predictable.

## 12. Verification summary (explicit)

- Automated verification: §9 (all green).
- Live Telegram verification: **NOT performed.**
- Live Supabase verification: **NOT performed.**

## 13. Current phase / next phase

- **Current:** Phase 1 — Library & Import — COMPLETE: message-level slice
  (previous commit) + set resolution/enumeration + Glass UI import/report
  panels + 2×5 library browser (this commit); 45 new tests; full suite
  5275 passed, 26 skipped.
- **Next:** Phase 2 — Categories & Mappings (ROADMAP §32), pending nothing
  from Phase 1 except live validation, which remains the owner's §31
  checklist. Phase 0 owner decisions §34-D/§34-E stay OPEN.
