# INVESTIGATION.md — AED and Private Message Management

Investigation of **Anti-Edit / Anti-Delete** (AED) and **Private Message
Management** (PM) on `main`. It replaces every previous investigation in full;
earlier rounds (Supabase transport, AED bot exclusion) remain in Git history and
`docs/investigations/anti-edit-delete-status.md`. Nothing but this file changed.

## 1. Scope

| Item | Value |
|---|---|
| Date | 2026-10-20 (UTC) |
| Repository | `Onlyicing1/Telegram-self-bot` (`origin`) |
| Branch / commit | **`main`** @ **`14f1f2dfecd8a8d41f3f2fbe8fd7d250df8811cb`**, verified `= origin/main` (`git fetch origin`, `git rev-parse origin/main`, identical tree `69eb2a5a…`) |
| Working tree at start | clean |
| Runtime | CPython 3.12.3; `supabase 2.4.2`, `telethon 1.43.0` |
| Live Telegram / Render / Supabase | **not accessed** — no credential, no connection, no SQL |

## 2. Executive summary

Both features are **implemented on `main`, registered in the running process and
covered by passing offline tests** — not stubs, not stuck on an unmerged branch.

**AED** captures and archives private human conversations: three handlers feed a
1,475-line service that snapshots content while it exists, keeps edit versions,
records deletions with honest recoverability, browses them through Glass UI
panels and expires them through the existing cleanup worker. Persistence is
**dual-store by design** — dedicated tables when readable, legacy `bot_settings`
shards otherwise, decided once per process. No restore-to-chat action; media is
metadata-only unless a bucket is configured.

**PM** is consulted by the existing all-messages handler before dispatch: a
non-configurable human-private gate, then three independent actions in fixed
order (delete → fixed response → AI response) with a durable per-person
allowance claimed by one conditional write. It uses its own tables and backfills
the Phase-1 `bot_settings` document exactly once.

**One structural problem — documentation/schema drift, not a code bug:** the
canonical setup block (`DATABASE_ARCHITECTURE.md` §31.3) omits the columns added
by `supabase/migrations/20261016000001_add_private_message_counter_reset.sql`
(`ai_counter_reset_enabled`, `ai_counter_reset_seconds`, `window_started_at`) and
never names that migration. The code writes those columns on every PM
configuration save, so a database built only from §31.3 rejects every PM setting
save — exactly the failure that migration's header describes.

Live database status is unverified here. The strongest evidence is a prior
**owner-reported** observation (`2afd89a` report §7; §34.7) that the AED tables
exist (RLS on, zero policies, service_role only, zero rows), while the PM tables
are explicitly *not claimed* to exist (§33.6).

## 3. AED

| Layer | File | Evidence |
|---|---|---|
| Handler | `backend/bot/handlers/anti_edit_delete.py` (593 L) | `register` L487; capture handlers L504/525/546; 5 panels L566–576; 8 actions L578–585; 1 input L587 |
| Service | `backend/services/anti_edit_delete_service.py` (1,475 L) | `eligible` L340, `is_bot_entity` L394, `record_message` L808, `record_deletion` L903, `cleanup_expired` L1248, `store_media_snapshot` L1394 |
| Persistence | `backend/db/client.py` L2750–3180 | legacy shards L2778–2900; `_aed_probe_store_sync` L2945, `aed_store_mode` L2977, `aed_load/save_config` L3028/L3062, `aed_load/save_entries` L3096/L3144 |
| Wiring | `router.py` L129 (registered last); `misc.py` L219 (`panel:archive`); `memory_cleanup.py` L58–60 | no second updater or scheduler |

Implemented: snapshots; edit versions (bounded `MAX_VERSIONS_PER_MESSAGE = 4`);
deletions `recoverable=True` **only** when content was captured beforehand
(service L979–980), otherwise an honest miss (handler L315); one list entry per
message; paginated list (`PAGE_SIZE = 8`), item detail, 7-category filter,
single/group delete, clear-archive with confirmation; config (`enabled`,
never-empty category subset, `retention_days` 1–3650); retention sweep; optional
media objects when `ARCHIVE_MEDIA_BUCKET` exists; bot exclusion by the resolved
`User.bot` flag plus browse-time `filter_bot_items`; owner isolation; honest
failed writes.

Absent (verified, not "unverified"): no restore/re-send of archived content (no
such action exists); no durable media store by default; no import of
pre-migration shard rows (§34.5 — those rows stop being shown once the tables
exist).

Persistence decision: `_aed_probe_store_sync` reads `aed_config` once and caches
only a *definitive* result (missing table `42P01`/`PGRST205` ⇒ legacy shards;
readable ⇒ dedicated). Every read/write routes through it (`_read_config` L224,
`_persist_state` L287, `load_rows_async` L737, `_save_rows` L770,
`cleanup_expired` L1259); a transient probe failure is not cached.

Schema: `20261017000001_create_anti_edit_delete_tables.sql` (285 L) creates
`aed_config` (PK `owner_id`, 3 writable columns) and `aed_archive_entries`
(PK `id`, 24 writable columns, CHECKs on kind/category/positive ids/body length),
one index, RLS with **zero policies**, full `REVOKE` from
`PUBLIC`/`anon`/`authenticated` (table **and** column level) and
`GRANT SELECT/INSERT/UPDATE/DELETE` to `service_role`; embedded verbatim as part
11 of 11 of §31.3 and pinned by `tests/test_aed_db_security.py` (11 tests).
Column sets match `_AED_CONFIG_COLUMNS` / `_AED_ENTRY_COLUMNS`.

Live status (evidence, not verification): §34.7 records the owner configuring the
live project directly (RLS on, no policies, service_role-only), and the `2afd89a`
report §7 records both tables existing with **zero rows** and an empty migration
history. If that holds, `aed_store_mode()` is `dedicated`, `aed_config` has no
row and `_read_config` returns `enabled=false` — **AED is wired but INERT until
the owner enables it.**

## 4. Private Message Management

```
incoming update
  └─ task_events.py L67-69     (the EXISTING all-messages handler)
       └─ private_message.enforce_incoming            L385
            ├─ service.ensure_loaded_async()          persisted state before the first decision
            ├─ resolve_scope(event, owner_id)         L195 — resolves BOTH entities via the self client
            │     └─ service.classify_scope(...)      L585 — the non-configurable gate
            ├─ service.decide_incoming(...)           L1058 — temporary > schedule > mode
            ├─ service.already_handled(chat, msg)     one enforcement per update
            └─ _apply_actions(...) L363               delete → fixed response → AI response
```

| Layer | File | Evidence |
|---|---|---|
| Handler | `backend/bot/handlers/private_message.py` (1,426 L) | `_ai_response` L329, `_apply_actions` L363, `enforce_incoming` L385, `register` L1323; 7 panels / 20 actions / 9 inputs L1350–1426 |
| Service | `backend/services/private_message_service.py` (1,861 L) | `LEGACY_STATE_ROW_KEY` L190, `classify_scope` L585, `_read_state_document` L724, `_load_task_body` L756, `decide_incoming` L1058, `claim_ai_reply` L1147 |
| Persistence | `backend/db/client.py` L2213–2739 | `_PM_CONFIG_COLUMNS` L2244, `_pm_claim_decision` L2299, `get/save_private_message_config` L2400/L2475, `get_legacy_private_message_state` L2433, `reset_private_message_counters` L2729 |
| Wiring | `router.py` L112 (before `task_events`, so the preload is in flight); `misc.py` L218 (`panel:pm`) | — |

Controls: master `enabled`; `mode` (`open`/`whitelist_only`/`closed`); three
**independent** flags `delete_incoming`, `fixed_response_enabled`,
`ai_response_enabled` (executed in that order, handler L371–378);
`fixed_response_text` (≤512); `ai_max_replies_per_person` (1–10000);
`ai_unlimited_replies`; `ai_interval_seconds`; allowlist; exceptions; temporary
override; weekly `schedule` with rules and an IANA timezone; AI counter reset
(toggle + interval); manual `Reset AI counters`. Every control performs an awaited
persisted write; a refused write is reported, never claimed.

Persistence and backfill: dedicated tables only (`private_message_config`,
`private_message_senders` with `PRIMARY KEY (owner_id, sender_id)`); no
`bot_settings` write path remains. The allowance is one CAS update per reply
(`_pm_claim_decision`), returned by a compensating CAS when nothing was
delivered. **Backfill is implemented and tested**: `_read_state_document`
(L724–755) reads the dedicated row first and only when missing reads the legacy
document (`private_message_management`, L190) via
`get_legacy_private_message_state`, then writes the imported config into the
dedicated row once (`_load_task_body` L756–790) — pinned by
`test_a_phase1_document_is_imported_into_the_dedicated_row` and
`test_garbage_json_in_the_legacy_document_is_ignored`. A missing table reads as
"no row" ⇒ disabled ⇒ inert.

| Migration | Content | State |
|---|---|---|
| `20260929000001_create_private_message_tables.sql` (234 L) | both tables, PKs, CHECKs, read index, RLS, **`GRANT SELECT` to `anon`/`authenticated`** + `USING (true)` policies, comments, verification queries, legacy probe | exists; embedded verbatim as part 10 of 11 of §31.3 |
| `20261016000001_add_private_message_counter_reset.sql` | `ai_counter_reset_enabled`, `ai_counter_reset_seconds` (+ CHECK 60…31536000), `window_started_at` | exists; **absent from §31.3 and from the §31 inventory** |

`_PM_CONFIG_COLUMNS` (L2244–2260) includes both reset columns and
`default_state()`/`sanitize_state` (L303–304, L511–519) always supply them, so
every configuration payload names them. The migration header states the
consequence: without them "Supabase rejects the statement … every Private
Message setting save reports failure". The claim CAS also writes
`window_started_at`. `DATABASE_ARCHITECTURE.md` has **zero** occurrences of
`ai_counter_reset`, `window_started_at` or `20261016000001`, and its §31 audit
still says "33 files" while `supabase/migrations/` holds **34**.

Live status: §33.6 says the document "does **not** claim the tables exist in any
Supabase project. They exist as SQL in this repository only"; `ROADMAP.md` §0
repeats "WRITTEN but NOT APPLIED". No owner report records PM tables being
created, so the PM live schema is **unknown**; built from an older §31.3 block it
lacks the reset columns and PM saves fail today.

## 5. Database status by category

| Category | AED | PM |
|---|---|---|
| Designed / documented | **Yes** — §34, inventory row 11 | **Yes** — §33, inventory row 10 |
| SQL / migration exists | **Yes** — `20261017000001…` (285 L), part 11 | **Yes** — `20260929000001…` (234 L, part 10) **and** `20261016000001…` (not embedded) |
| Referenced by code | **Yes** — `client.py` L2903–3180; service L224/287/737/770/1259 | **Yes** — `client.py` L2213–2739; service L724–790/1147/1199 |
| Offline tests pass | **Yes** — 95 + 11 | **Yes** — 38 + 118 + 50 |
| Applied to Supabase | **Not verified here**; prior owner report says yes (manual, empty history) | **Not verified; documented NOT applied** |
| Verified in live database | **No** | **No** |
| Verified in live Telegram | **No** | **No** |

## 6. Runtime wiring

```
RuntimeSupervisor.start()
  └─ router.register_all()                    backend/bot/router.py
       ├─ "private_message"  L112  → panels + configure_owner + state preload
       ├─ "task_events"      L113  → the ONE all-messages handler → enforce_incoming
       └─ "anti_edit_delete" L129  → 3 capture handlers, registered LAST
  └─ start_memory_cleanup()                   backend/runtime/memory_cleanup.py
       └─ anti_edit_delete_service.cleanup_expired()      (no second scheduler)
```

AED files: handler, service, `client.py` (AED block), `router.py`, `misc.py`,
`memory_cleanup.py`, migration `20261017000001…`, §34, its two test files.
PM files: handler, service, `client.py` (PM block), `task_events.py`, `router.py`,
`misc.py`, both migrations, §33, `ROADMAP.md` §0, its three test files.

## 7. Tests — executed here

Ran on `14f1f2d` with `.venv/bin/python -m pytest … -q -p no:cacheprovider`:
AED **95 + 11**, PM **38 + 118 + 50**, setup-order **32** — **344 passed** as one
run (2.22 s); full suite `pytest tests` **6448 passed, 27 skipped, 0 failed**
(132 s).

They prove registration and callback limits, default-OFF state, the scope gate,
dedicated-store routing and legacy fallback, owner isolation, honest failed
writes, retention deletion, migration security and statement identity with §31.3,
PM action order, the CAS allowance, the panel controls and the one-time backfill
— all against **fake DB clients**. They prove no table exists, no SQL validity on
real Postgres, and no Telegram delivery.

## 8. Live verification limits

No Supabase connection, SQL execution or migration was performed here or by any
commit reachable from `14f1f2d`. No Telegram client was started; no Render
deployment or log was read. The only live signals in the repository are
owner-reported AED observations. "AED archive works in production" and "PM policy
works in production" are therefore **untested claims**.

## 9. Conflicts and stale statements

| # | Where | Statement | Reality on `14f1f2d` |
|---|---|---|---|
| 1 | AED service docstring L12–20; PM handler docstring L16–17 | AED "no new table and no migration"; PM "persisted `bot_settings` state" | AED prefers the dedicated tables and PM lives in `private_message_config`; the legacy store is only fallback/backfill. **Both stale.** |
| 2 | §31 audit + `tests/test_database_setup_order.py` L451 | "all 33 migrations" / `"33 files"` | 34 files exist; the unclassified one is the reset migration. The test asserts the prose instead of counting files, so it passes while the inventory is wrong. |
| 3 | §31.3 part 10 + §33 | PM tables "in their final shape" | The code's shape includes the three reset columns; the embedded SQL omits them. **A database built from §31.3 cannot save any PM setting.** |
| 4 | `AGENTS.md` (self-declared single source of truth), `README.md` | — | **Zero** mentions of AED, Private Messages or the archive, though both are menu-reachable. `ROADMAP.md` has no AED section. |
| 5 | Security posture | AED: service_role only, zero policies | PM: `GRANT SELECT` to `anon`/`authenticated` + `USING (true)`, exposing allowlist, exceptions, schedule and per-sender counters to any anon-key holder. Documented (§33.4) but **asymmetric**; owner decision needed. |
| 6 | Branch `feat/private-privacy-aed-persistence` (`de5975d`, 19 commits **behind**) + `stash@{0}` | Alternative AED line + a 2-file stash | Superseded leftovers; the delivered line is `c54a92c` → `2afd89a`, and nothing on `main` imports `_aed_store`. Untouched. |

## 10. Remaining work

**AED:** enable it once the dedicated tables are confirmed (they are empty, so it
is inert); confirm one capture cycle live; decide whether pre-migration shard rows
should be imported (§34.5 defers it); provision `ARCHIVE_MEDIA_BUCKET` for media;
decide whether restore-to-chat is intended.

**PM:** apply the reset migration (or fold its statements into §31.3) so saves
stop failing; confirm both tables exist; live-verify the gate, action order, one
allowance claim and one automatic reset; decide the `anon` SELECT exposure;
reconcile the inventory (34 vs "33") and make its test count files.

## 11. Recommended next step

1. Run `supabase/migrations/20261016000001_add_private_message_counter_reset.sql`
   in the Supabase SQL editor (idempotent, additive, touches no row) — the only
   change that turns PM from "always fails to save" into "can save".
2. Save one PM setting and confirm it persists; then enable AED and observe one
   capture cycle.
3. Add that migration to §31.3 as part 12 so a fresh database is never built
   without columns the code writes (documentation task, out of scope here).

_Precondition:_ read-only Supabase access, which this environment does not have.

## 12. Evidence table

| Claim | Evidence | Status | Confidence / Limitations |
|---|---|---|---|
| AED is implemented, registered and reachable, and captures snapshot/edit/deletion | handler 593 L / service 1,475 L; `router.py` L129; handlers L504/525/546; `misc.py` L219; 95 tests | **Confirmed** | Source read; live delivery not |
| AED has no restore-to-chat action | action list L578–585 | **Confirmed absent** | Full block read |
| AED persistence is dual-store, decided once; tables used by code; migration = §31.3 part 11 | `client.py` L2945/L2977/L2903–3180; service L224/287/737/770/1259; tests L1360–1488; `test_the_canonical_part_is_statement_identical_to_the_migration` | **Confirmed** | Probe caching read; static comparison |
| AED tables exist in the live project, but the feature is not enabled | `2afd89a` report §7 (owner-reported, zero rows); §34.7; default `enabled=false` | **Reported, unverified; inert** | Owner-supplied, may have changed |
| PM has a real path from update to persistence, a full surface and its own tables | `task_events.py` L67–69 → `enforce_incoming` → `classify_scope` → `decide_incoming` → `_apply_actions` → service → `client.py`; 7 panels / 20 actions / 9 inputs; `misc.py` L218; no `bot_settings` write path | **Confirmed** | Source trace; offline tests only |
| The one-time PM backfill exists and is tested, but PM schema status is not | service L190/724–790; `test_a_phase1_document_is_imported_into_the_dedicated_row`; §33.6; `ROADMAP.md` §0 | **Backfill confirmed offline; schema NOT applied / unknown** | No live access; never run against a real row |
| §31.3 omits the PM reset columns the code writes | zero grep hits for `ai_counter_reset`/`window_started_at`/`20261016000001`; `_PM_CONFIG_COLUMNS` L2244–2260; migration header | **Confirmed (static)** | Save failure not reproduced on a real DB |
| Inventory says 33 migrations; 34 exist; neither feature is in `AGENTS.md`/`README.md` | §31; `ls supabase/migrations \| wc -l` = 34; test L451 asserts the prose; grep returns no hits | **Confirmed** | — |
| All AED/PM tests pass | 344 passed (6 files); 6448 passed / 27 skipped full suite | **Confirmed (executed)** | Offline, fake DB clients |
| Live Telegram behaviour of either feature | no session created | **Not verified** | Requires the owner |

## 13. Changes made

`INVESTIGATION.md` replaced in full. Source, tests, migrations,
`DATABASE_ARCHITECTURE.md`, `ROADMAP.md`, `AGENTS.md`, `README.md`,
dependencies, secrets and Supabase untouched. The
`feat/private-privacy-aed-persistence` branch and `stash@{0}` untouched.
