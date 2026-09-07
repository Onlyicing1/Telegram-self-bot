# IMPLEMENTATION REPORT

## 1. IMPLEMENTATION METADATA

| Item | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Branch | `main` |
| Report type | Current-state implementation report — memory persistence/repository compatibility with the fixed `public.ai_memories` schema (this session); §4–§16 below are retained historical audits from prior sessions |
| This session's change | Memory persistence/repository schema-compatibility fix + 27 focused regression tests (`tests/test_memory_repository_supabase.py`). Production: `backend/ai/database/memory_repository.py`, `backend/ai/persistence.py`. Doc: `DATABASE_ARCHITECTURE.md` (one metadata-default cell). Follow-ups (2026-09-06): source-level live-integration audit of the production memory path (§3.10), then opt-in LIVE Supabase integration-test mechanism (§3.11 — `tests/test_live_supabase_memory.py` + marker registration) |
| Date | 2026-09-06 |
| Status | **COMPLETE — in-process tests verified. LIVE SUPABASE EXECUTION: BLOCKED in this workspace** (opt-in test implemented; real run requires `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY`, which are absent here — the test skips honestly) |
| Commit (this session) | `f6c3bcc25b6a33272a5e9e7e93d3468defa7d57b` (`fix: complete memory repository compatibility with the ai_memories schema`); latest delivery `0944e6ecad1dece76edb84df79e405b8ecff966f` (`test: add opt-in live Supabase integration test for AI memory`) |
| Push result | `ed67d28..0944e6e main -> main` — succeeded, then independently verified via `fetch` + `rev-parse` + `ls-remote` |
| Remote `refs/heads/main` | `0944e6ecad1dece76edb84df79e405b8ecff966f` (authoritative `ls-remote`, post-push) |

## 2. EXECUTIVE SUMMARY

**Current state (verified from current source this session):** the AI memory repository (`SupabaseMemoryRepository`) and the `backend.ai.persistence` memory helpers are fully compatible with the EXISTING fixed `public.ai_memories` table — 9 columns (`id`, `owner_id`, `tier`, `category`, `content`, `importance`, `expires_at`, `metadata`, `created_at`).

- The save payload serializes exactly the 7 application-owned columns (`owner_id`, `tier`, `category`, `content`, `importance`, `expires_at`, `metadata`); `id`/`created_at` are database-generated and never written.
- Duplicate-content idempotency is now complete: the pre-write check filters by content equality, so an identical (owner, tier, content) row is found **regardless of rank** (the old top-1 probe missed lower-ranked duplicates and inserted twice).
- Row reconstruction handles the exact nine-column schema honestly: NULL `importance` reconstructs as the schema default **0.5** (never silently coerced to 0.0), NULL `expires_at`/`metadata` are preserved, and database-generated `id`/`created_at` are read back.
- Honest failure semantics preserved: repository rejection/exception → `save()` returns `False` → tier store returns `None` → `memory_store` reports `success=False`. `MEMORY_WRITE_TIMEOUT_S = 3.0` unchanged.
- Owner isolation, the single MemoryManager authority, the in-memory fallback, and the `memory_store`/`memory_list` tool behavior are unchanged. No SQL was executed; no migration was created; no schema was modified.

Full suite: **1773 passed, 23 skipped** (this session). Live Supabase verification was NOT performed. A follow-up source audit (§3.10) verified the production write/read path through the real Supabase client wiring and established that NO safe live-test mechanism exists in this architecture, so none was added — live verification remains NOT PERFORMED.

Follow-up (§3.11): the smallest safe opt-in live-test mechanism was then implemented (`tests/test_live_supabase_memory.py`, `@pytest.mark.live_supabase`). In this workspace it SKIPS honestly — `SUPABASE_URL`/`SUPABASE_SERVICE_ROLE_KEY` are absent — so no live claim is made. Full suite with the new test: **1773 passed, 24 skipped** (23 pre-existing + 1 live skip).

## 3. CURRENT STATE — MEMORY REPOSITORY SCHEMA COMPATIBILITY (2026-09-06)

### 3.1 Exact defect fixed

The Supabase-backed memory repository was not fully compatible with the existing fixed `ai_memories` schema in two concrete ways:

1. **Incomplete duplicate-content idempotency** — `SupabaseMemoryRepository.save()` probed only the top-1 row (`limit=1`, importance-desc) before inserting. An identical (owner, tier, content) row that was not the top-ranked row was missed, so a duplicate row was inserted. The in-memory fallback and the repository's own module docstring both guarantee “identical (owner, tier, content) writes are idempotent — duplicate entries are never created”; the Supabase implementation violated that guarantee.
2. **Silent NULL-importance substitution** — row reconstruction coerced a NULL `importance` (nullable column, `DEFAULT 0.5`) to `0.0`, silently substituting a value that is neither the schema default nor the `MemoryEntry` default, and changing retrieval ranking semantics.

### 3.2 Exact files changed

| Path | Change |
|---|---|
| `backend/ai/persistence.py` | `_query_memories_sync()` gains a `content` equality filter (`eq("content", ...)`); async `query_memories()` passes `category`/`content` through to the sync helper |
| `backend/ai/database/memory_repository.py` | `save()` pre-check now queries with `content=entry.content` and treats any returned row as the duplicate (complete idempotency); `query()` reconstructs NULL `importance` as `0.5` (explicit `None` check — a stored `0.0` stays `0.0`) |
| `tests/test_memory_repository_supabase.py` | NEW — 27 focused tests driving the repository through a recording fake Supabase client injected via `backend.ai.persistence._get_db` |
| `DATABASE_ARCHITECTURE.md` | §10 `ai_memories` column table: `metadata` default corrected from `—` to `'{}'` (matches the actual schema) |

No migration created, no SQL executed, no Supabase object modified, no other production module touched.

### 3.3 Root cause → fix mapping

| Finding | Root cause | Fix |
|---|---|---|
| Duplicate rows possible via `memory_store` when the identical content already exists with lower importance | Idempotency probe read only the top-1 row; duplicates below the top rank were invisible | Content-equality pre-check in `_query_memories_sync`; `save()` treats any matching row as the duplicate and never inserts twice |
| Reconstructed entries silently reported `importance=0.0` for NULL rows | `float(row.get("importance") or 0.0)` coerced NULL to `0.0` | Explicit `None` check → schema default `0.5`; stored `0.0` preserved |
| (Verified compatible — no change needed) Save payload columns, select columns, ordering, delete/expire/count filters already matched the fixed 9-column schema | — | Pinned by tests so drift fails CI |

### 3.4 Behavior before / after

| Operation | Before | After |
|---|---|---|
| `save()` of identical content ranked below the top row | Duplicate row inserted | Idempotent: no second insert |
| Reconstruct a row with NULL `importance` | `0.0` | `0.5` (schema default) |
| Reconstruct a row with `importance = 0.0` | `0.0` | `0.0` (unchanged, still preserved) |
| Save payload | 7 app-owned columns | 7 app-owned columns (unchanged, pinned by test) |
| Rejection/exception on write | `save() → False` → tier store `None` → tool `success=False` | Identical (unchanged, pinned by test) |
| `MEMORY_WRITE_TIMEOUT_S` | 3.0 | 3.0 (unchanged, pinned by test) |

### 3.5 Tests added / executed

`tests/test_memory_repository_supabase.py` (27 tests) covers, per the task contract: payload uses only compatible columns; owner/tier/category/content/importance/expires_at/metadata persisted correctly; DB-generated `id`/`created_at` not written and read back; exact nine-column row reconstruction (NULL importance → 0.5, NULL expires_at/metadata, stored 0.0 preserved); duplicate-content idempotency regardless of rank; rejection and exception → honest failure (`False` → tier store `None`); owner isolation on query/count and through the tools; tier/category/importance filters + ordering + limits; delete by database id; tier-scoped expired cleanup; the global “no nonexistent `ai_memories` column is ever referenced” assertion; long/permanent `memory_store` and `memory_list` behavior through the Supabase repository; and the 3.0s write-timeout bound.

Exact commands and real results (this session, `.venv/bin/python`, pytest 9.x, `-p no:cacheprovider`):

| Command | Result |
|---|---|
| `pytest tests/test_memory_repository_supabase.py tests/test_memory_tools.py tests/test_37_ai_memory_db.py -q` | **64 passed** |
| Adjacent AI/tool regression set (tool health audit, capability exposure, tool calls, confirmation round-trip, settings RC-5/RC-7 suites, memory tools, memory DB) | **243 passed, 1 warning** |
| `pytest tests/ -q` (full suite) | **1773 passed, 23 skipped, 1 warning in 64.30s** |
| `py_compile backend/ai/database/memory_repository.py backend/ai/persistence.py tests/test_memory_repository_supabase.py` | OK |
| `git diff --check` | clean |

### 3.6 Database / Supabase impact

- **NO schema change.** No migration created or modified. No SQL executed against Supabase. No table/column/index/constraint/RLS object changed.
- The repository adapts to the existing fixed schema; nothing in the database was “corrected” to match code.
- **Live Supabase verification: NOT PERFORMED.** Tests use a recording fake DB client; the in-memory fallback is what the rest of the suite exercises.

### 3.7 Security / architecture

- Single memory authority preserved: the Engine-owned `MemoryManager` is still the only entry point; no second repository/manager/client was introduced.
- Owner scoping preserved on every persistence operation (writes store `context.owner_id`; reads/counts filter `owner_id`); owner identity never comes from model-supplied input.
- Honest failure semantics preserved (None-on-failure, bounded 3.0s write timeout, `asyncio.to_thread` off-loop execution).
- Unchanged: ToolExecutor permission semantics, dispatcher, providers, Telegram boundaries, settings, save/delete/profile systems.

### 3.8 Limitations

- Delete-by-id addresses the database-generated `id` (stringified bigint, as returned by row reconstruction); application-generated UUID ids never exist in the DB and therefore never match — `delete()` fails honestly (`False`), it never corrupts.
- No live Supabase round-trip was performed in this workspace.

### 3.9 Git delivery

- Commit: `f6c3bcc25b6a33272a5e9e7e93d3468defa7d57b` (`fix: complete memory repository compatibility with the ai_memories schema`, 4 files, +610/−11)
- Push: `3ae2e44..f6c3bcc main -> main` — succeeded, independently verified (`git fetch origin`; `rev-parse HEAD` == `rev-parse origin/main` == `git ls-remote origin refs/heads/main` == `f6c3bcc25b6a33272a5e9e7e93d3468defa7d57b`).
- Working tree after delivery: clean except pre-existing untracked `telegram-self-bot/` (nested clone, untouched).

### 3.10 Live-integration verification audit (2026-09-06)

Source-level audit of the REAL production path (no code changed, no live execution). Task rule applied: a live test is added ONLY if the repository already has an established safe way to run against real Supabase credentials — it does not, so none was added (see assessment below).

**Verified production WRITE path (source-traced):**

```
memory_store tool (backend/ai/tools/memory.py)
  → _resolve_memory_manager() → Engine.memory_manager (engine.py:96-111)
  → MemoryManager.store_long / store_permanent (memory/manager.py)
  → LongMemory.store / PermanentMemory.store (long.py:59-66, permanent.py)
  → SupabaseMemoryRepository.save — selected by RepositoryManager when
    SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY are set (database/manager.py)
  → backend.ai.persistence._save_memory_sync
  → _get_db() → backend.db.client.get_db() → supabase.create_client(...)
  → INSERT into public.ai_memories with exactly the 7 app-owned columns
    (owner_id, tier, category, content, importance, expires_at, metadata)
```

Pre-write idempotency: `save()` probes `_query_memories_sync(owner_id, tier, limit=1, content=...)` (content-equality filter) — an identical (owner, tier, content) row is found regardless of rank and never re-inserted. `owner_id` always comes from the tool context, never from model input.

**Verified production READ path (source-traced):**

```
public.ai_memories rows
  → persistence._query_memories_sync — SELECT * filtered by owner_id (+ optional
    tier/category/content equality, min_importance gte); ORDER BY importance DESC,
    created_at DESC, id; LIMIT
  → SupabaseMemoryRepository.query — exact nine-column row reconstruction
    (NULL importance → 0.5 schema default; stored 0.0 preserved; NULL
    expires_at/metadata preserved; DB-generated id/created_at read back)
  → LongMemory.retrieve / PermanentMemory.retrieve_all
  → memory_list tool / MemoryManager.retrieve_for_prompt
```

**Verified ancillary operations (source):** `delete(entry_id)` (`eq("id", ...)` on the database-generated id), `delete_expired(tier)` (`eq tier` + `lt expires_at now`), `count(owner_id, tier)` (`count="exact"`, owner-filtered) — all owner-scoped where applicable. RLS: the backend uses the service-role key by design (bypasses RLS); the client-role policy lockdown described by the user is orthogonal and untouched.

**Live-test path assessment — NO established safe mechanism exists:**

- `tests/conftest.py` states fixtures are "in-memory (no-network) instances of every subsystem, so tests run deterministically without Supabase, Telegram, or external AI providers".
- No `pytest.ini`/`setup.cfg`/`pyproject.toml` marker registration, no `-m live` or equivalent opt-in gate anywhere in the suite.
- No existing test constructs a real Supabase client; the only `os.getenv` usage in tests is `patch("os.getenv", return_value="")` (absence mocking). All Supabase behavior tests inject a recording fake via `monkeypatch.setattr("backend.ai.persistence._get_db", ...)`.
- No `scripts/` utility or standalone live-verification entry point exists.

**Conclusion:** per the task's stop condition, a live test was NOT added. Precisely what is missing for a future safe live test (reported, not built): (1) an opt-in gate — a `live` pytest marker (or separate `tests_live/` directory) that the default run never collects; (2) a credential-presence skip that never reads/prints secret values; (3) a self-cleaning data contract — dedicated test `owner_id` distinct from the real owner, unique content marker, teardown deletes; (4) live credentials in the execution environment (unavailable in this workspace — env access blocked by the sandbox).

**What remains unverified (unchanged):** real PostgREST round-trip (write → read → dedup → NULL/default handling) against the live `ai_memories` table, actual jsonb/timestamptz row shapes as returned by the REST API, and service-role RLS bypass in production. The fake-client tests approximate these but cannot prove them.

**Tests executed this audit (no production code changed):** focused memory suites (`test_memory_repository_supabase.py` + `test_memory_tools.py` + `test_37_ai_memory_db.py`) — **64 passed**; full suite — **1773 passed, 23 skipped, 1 warning in 63.73s**. `git diff --check` clean.

### 3.11 Opt-in LIVE Supabase integration-test mechanism (2026-09-06)

Implements the smallest safe opt-in live-test mechanism requested after §3.10. No production code, schema, migration, or RLS change.

**Files changed:**

| Path | Change |
|---|---|
| `tests/test_live_supabase_memory.py` | NEW — opt-in live test driving the REAL production path (`SupabaseMemoryRepository` → `backend.ai.persistence` → `backend.db.client.get_db()` → real `supabase.create_client`) against the existing `public.ai_memories` table |
| `tests/conftest.py` | `pytest_configure` registers the `live_supabase` marker (no `pytest.ini`/`setup.cfg`/`pyproject.toml` exists in this repo, so the conftest hook is the established mechanism) |

**Opt-in gating (normal suite never performs live operations):**

- `@pytest.mark.live_supabase` on the test.
- Module-level `skipif(not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY))` — honest SKIP with reason when either credential is absent; secrets are never read into test output, printed, or asserted on.
- Explicit run: `SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... pytest tests/test_live_supabase_memory.py -m live_supabase -v`.

**What the live test verifies (when executed with real credentials):**

- **A. REAL INSERT** — long-tier memory through `repo.save()`; success asserted; read-back row has a real database-generated bigint `id`, a real `created_at`, and correct `owner_id`/`tier`/`category`/`content`/`importance`/`expires_at`/`metadata`.
- **B. REAL SELECT** — exact record retrievable through `repo.query()`.
- **C. REAL NULL/DEFAULT** — the repository path always serializes `importance` (never NULL), so the ONE controlled direct insert uses the same production service-role client to create a NULL-importance/NULL-expires/NULL-metadata row; reconstruction through the repository read path must map importance → 0.5 (schema default, never 0.0) and preserve NULL expires_at/metadata. Schema defaults untouched.
- **D. REAL DUPLICATE IDEMPOTENCY** — second identical (owner, tier, content) `save()` creates no new row; `count()` stays stable.
- **E. REAL OWNER ISOLATION** — owner B's repository query cannot see owner A's rows (repository `owner_id` scoping, asserted independently of RLS).
- **F. REAL DELETE** — `repo.delete(<database-generated id>)`; record no longer retrievable.
- **G. CLEANUP** — `finally`-based; strictly limited to rows created by this test (tracked ids + owner+content-scoped fallback; never tier/category-broad, never other owners). Cleanup failures are reported explicitly (pytest.fail when the body passed; stderr note when the body already failed).
- **H. RLS / ACCESS PATH** — same service-role configuration as production; no client policies created, RLS not disabled; the client is asserted non-None and the round-trip itself proves service-role access.

Test-only data: runtime-generated NEGATIVE `owner_id`s (never real Telegram ids) and a unique content marker (`live-supabase-memory-<uuid hex>`), so production rows cannot collide.

**Exact live-test result in THIS workspace: `1 skipped`** — `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` are absent from the execution environment (`os.getenv` probe returned False; the sandbox blocks env access). **Live execution was NOT performed and is NOT claimed.** The skip reason is explicit and the mechanism is ready to run wherever credentials exist.

**Verification categories (explicit distinction):**

| Category | State |
|---|---|
| Migration / schema change | NONE — no migration created, no schema object modified |
| Manual Supabase change (performed OUTSIDE this repository, by the owner, previously reported) | `ai_memories` table verified with the 9 fixed columns; PK on `id`; indexes `(owner_id, tier)` and `(importance)`; RLS enabled; public SELECT policy for anon/authenticated REMOVED; ALL direct table privileges revoked from anon/authenticated; no client-role INSERT/UPDATE/DELETE policies; backend uses the service-role path (RLS bypass by design) |
| Live Supabase verification | NOT PERFORMED in this workspace — credentials absent; opt-in test skips honestly |
| Unit / in-process verification | GREEN — focused memory suites 64 passed; full suite 1773 passed, 24 skipped (23 pre-existing + 1 live skip), 1 warning in 63.39s |

**Remaining limitation:** the live test's assertions are only exercised when it actually runs with real credentials. Until then, the real PostgREST round-trip (write → read → dedup → NULL/default → isolation → delete) remains unverified.

---

## RETAINED HISTORICAL RECORD

The sections below are retained audits and delivery records from prior sessions (unchanged): §4–§12 audit the settings-key-contract commit `8b73497`; §13–§14 the RC-5 ledger; §15 the RC-6/A-1/A-2/A-3 remediation; §16 the memory tool connection. The memory repository schema-compatibility work in §3 supersedes nothing in those records.

## 3. EXACT FILES CHANGED (by retained commit 8b73497)

`git show --numstat 8b73497` (authoritative):

| Path | Category | Added | Removed | Purpose of change |
|---|---|---|---|---|
| `backend/ai/tools/settings.py` | production | 33 | 4 | Add `_setting_key_contract()` + module key lists; embed the contract in `SettingsGetTool`/`SettingsSetTool` descriptions and `key` parameter descriptions |
| `backend/ai/prompt/builder.py` | production | 6 | 2 | Relabel reply-block metadata lines `AI Provider:`→`Provider:`, `AI Model:`→`Model:`; add a 4-line why-comment |
| `tests/test_settings_model_key_contract.py` | test (NEW file) | 316 | 0 | 7 regression tests pinning the repaired model-facing contract |
| `INVESTIGATION.md` | documentation | 27 | 0 | RC-7 finding, confirmation-invariant note, F-3 index line |
| `IMPLEMENTATION_REPORT.md` | documentation | 109 | 1 | §17 (that commit's report) and filling the `<SHA_AFTER_COMMIT>` placeholder in §16.8 |

No other files were touched by that commit. No configuration, schema, migration, or dependency files changed.

### 3a. Files changed by this session's RC-5 completion

| Path | Category | Change |
|---|---|---|
| `tests/test_settings_unknown_key.py` | test | Add `test_patch_settings_endpoint_unknown_key_fails_closed` (web `PATCH /api/settings` unknown key → HTTP 400, repository write never attempted, cache never polluted); module docstring extended; trailing newline restored |

No production code, configuration, schema, or dependency file changed this session.
## 4. EXACT IMPLEMENTATION CHANGES (retained audit — commit `8b73497`)

RC-5's own production implementation (`4a226a0`, test-isolation follow-up `1dda645`) is summarized in §2 and detailed in `INVESTIGATION.md` (RC-5). The subsections below are the retained source-grounded audit of the settings-key-contract commit `8b73497`, preserved for continuity.

### 4.1 `backend/ai/tools/settings.py` — `_setting_key_contract()` and schema enrichment

**Old behavior (parent `1dda645`):**

- `SettingsGetTool.description` → `"Read a bot setting value by key."`
- `SettingsGetTool.parameters["key"]["description"]` → `"The setting key to read."`
- `SettingsSetTool.description` → `"Set a bot setting value by key. Requires owner confirmation."`
- `SettingsSetTool.parameters["key"]["description"]` → `"The setting key to write."`

No valid-key vocabulary existed anywhere in the model-facing surface. The key authorities (`_AI_CONFIG_KEYS` frozenset; `settings_service.known_keys()`, derived from `_DEFAULTS` at `backend/services/settings_service.py:66`) were purely internal enforcement sets, consulted only *after* the model chose a key.

**New behavior (`8b73497`):**

- Module-level `_AI_KEY_LIST` (sorted `", ".join` of `_AI_CONFIG_KEYS`: `history_budget, max_tokens, model, provider, system_prompt, temperature, trigger_en, trigger_fa`) and a lazily-cached `_PANEL_KEY_LIST`.
- `_setting_key_contract()` — lazily imports `settings_service` (avoids import cycles at module load), joins `sorted(settings_service.known_keys())`, and returns: `Valid keys — AI runtime: <list>; panel settings: <list>. The AI model setting is key 'model', never 'ai_model'; the AI provider setting is key 'provider', never 'ai_provider'.`
- All four description/parameter surfaces above now embed `_setting_key_contract()`. These are `@property` accessors, so the contract string is rendered on access.

**Architectural effect:** the contract reaches the model through both existing channels — the dispatcher's native tool-call schema construction and the text `[Available Tools]` block — because both consume the same `Tool.description` / `Tool.parameters`. There is no new key list, no alias table, no second configuration authority: the contract is *derived at runtime from* `_AI_CONFIG_KEYS` and `settings_service.known_keys()`, so it cannot drift from the enforcement sets.

**Explicitly unchanged in this file:** `_AI_CONFIG_KEYS` contents; `_set_ai_config()` routing (`provider`/`model`/`temperature`/`max_tokens`/`history_budget`/`system_prompt`/`trigger_en`/`trigger_fa` branches); the `provider` registration check against the runtime `ProviderManager`; `_apply_runtime_selection()` → `engine.apply_runtime_selection`; the RC-5 fail-closed branch (`if not settings_service.is_valid_key(key): return ToolResult(success=False, "Unknown setting key '...'")`); `SettingsSetTool.permission_level = ADMIN_ONLY`; `SettingsGetTool.permission_level = READ_ONLY`.

### 4.2 `backend/ai/prompt/builder.py` — reply-block labels

**Old behavior (parent):** inside the `[Reply to AI Message]` block, conditional lines rendered `AI Provider: {ctx.reply.ai_provider}` and `AI Model: {ctx.reply.ai_model}` (when those fields are non-empty).

**New behavior:** the same two lines render `Provider: {ctx.reply.ai_provider}` and `Model: {ctx.reply.ai_model}`. A 4-line comment records why (the old label taught the model a non-existent `ai_model` key).

**Architectural effect:** the model-facing prompt now contains only the canonical key names (`provider`, `model`), consistent with the `[Runtime Context]` block. This is a prompt-text change only — no data flow, field, or state change. The internal `ReplyContext.ai_model` / `ReplyContext.ai_provider` fields are untouched (internal context plumbing, never shown as a key name to the model after this commit).

### 4.3 `tests/test_settings_model_key_contract.py` — new regression file

7 tests (detailed in §7). They pin: the schema exposes canonical keys and never offers `ai_model` as valid; `ai_model` is rejected directly and through `ToolExecutor.execute_confirmed`; the ADMIN_ONLY confirmation gate still gates `settings_set`; a confirmed `model` change persists to `config_store`, applies to the runtime `ProviderManager`, and the NEXT `Engine.execute()` request is served by the new model (verified via a recording stub provider); the prompt renders `Model:`/`Provider:` and never `AI Model:`/`AI Provider:`.

### 4.4 Documentation changes in the commit

- `INVESTIGATION.md`: adds the RC-7 section (root cause, fix, test pointer), a one-line confirmation-invariant note in the settings-tools matrix, and the F-3 line in the findings index.
- `IMPLEMENTATION_REPORT.md`: adds §17 documenting this fix and fills §16.8's `<SHA_AFTER_COMMIT>` placeholder with the RC-5 delivery SHAs.

## 5. ROOT CAUSE → FIX MAPPING

Findings are from `INVESTIGATION.md` (read AFTER the commit diff was inspected, per audit protocol).

| Finding | Root cause | Change in 8b73497 | Status | Evidence |
|---|---|---|---|---|
| F-3 / RC-7 — model-facing settings key contract gap (`ai_model` vs `model`) | Prompt label `AI Model:` + unenumerated `key` parameter made the model invent a non-existent key; backend routing was correct and fail-closed | `_setting_key_contract()` embedded in both settings tools' schemas; prompt labels renamed to canonical `Model:`/`Provider:`; 7 regression tests | **FIXED (in source + in-process tests); LIVE VERIFIED: NO** | `git diff 8b73497^ 8b73497 -- backend/ai/tools/settings.py backend/ai/prompt/builder.py`; `tests/test_settings_model_key_contract.py` (7 passed during this audit) |
| F-1 / RC-5 — `settings_set` phantom success on unknown keys | `settings_service.set_setting()` had no allowlist; cached arbitrary keys and returned `True` | **Not this commit** — fixed earlier by `4a226a0` + `1dda645`; `8b73497` *preserves* the fail-closed branch untouched | NOT RELATED TO THIS COMMIT (already FIXED) | RC-5 branch present in `8b73497`'s `SettingsSetTool.execute` (source-inspected); `tests/test_settings_unknown_key.py` passes |
| F-2 / RC-6 — DANGEROUS permission docstring drift | `base.py`/`delete.py`/`organize.py` docstrings contradict executor auto-execute behavior | None — those docstrings are untouched by `8b73497` | NOT FIXED (documentation-only finding, out of this commit's scope) | no diff hunks in those files |
| RC-1 — `❌ Unsupported action: send` | fixed in `1285cdf` (pre-existing) | None | NOT RELATED TO THIS COMMIT | earlier commit |
| RC-2 — `settings_set` could not execute through AI | fixed in `c5d29f7` (pre-existing) | None | NOT RELATED TO THIS COMMIT | earlier commit |
| RC-3 — event request miscreated as interval task | fixed in `1285cdf` (pre-existing) | None | NOT RELATED TO THIS COMMIT | earlier commit |
| RC-4 — documentation drift | documented, unfixed | None | NOT RELATED TO THIS COMMIT | INVESTIGATION.md §7 |
| A-1 / A-2 / A-3 — P3 notes (`delete_messages_by_ids` description overclaim; empty-value `settings_get`; `web_search` error swallow) | minor, documented | None — untouched by `8b73497` | NOT FIXED / NOT RELATED TO THIS COMMIT | no diff hunks in those tools |

## 6. BEHAVIOR CHANGED

**Newly added behavior:**
- The model-facing tool schema (both native provider schemas and the text `[Available Tools]` block) now enumerates every valid settings key across both stores and explicitly names `model`/`provider` as canonical with `ai_model`/`ai_provider` as invalid.

**Corrected behavior:**
- The `[Reply to AI Message]` block labels are `Model:`/`Provider:` instead of `AI Model:`/`AI Provider:` — the prompt no longer teaches a non-existent key. Reply-metadata data flow is unchanged.

**Preserved behavior (verified in the commit's diff and current source):**
- All settings routing: AI-runtime keys → `config_store` (+ `_apply_runtime_selection` for `provider`/`model`); valid panel keys → `settings_service`.
- RC-5 fail-closed rejection of unknown panel keys, including the exact `Unknown setting key '...'` message the live failure produced — `ai_model` is still rejected (now listed in the schema text only as a forbidden alias).
- ADMIN_ONLY confirmation gate on `settings_set` (needs_confirmation → owner confirmation → `execute_confirmed`).
- `ToolRegistry` / `ToolExecutor` / `ProviderManager` / dispatcher plumbing.

**Intentionally unchanged:**
- No alias layer for `ai_model`; no schema/migration; no provider/model runtime logic; no Glass UI changes (the `panel:ai_model` internal UI namespace is unchanged — it is a Glass panel name, not a model-facing key); no other tool's schema.
## 7. TESTS

**Added by the commit** — `tests/test_settings_model_key_contract.py` (new file, 316 lines, 7 tests):

1. `test_settings_tool_schema_exposes_canonical_keys_and_never_ai_model` — contract text/derived lists contain canonical keys, never `ai_model` as valid; `'model'` appears before the forbidden alias.
2. `test_settings_set_and_get_descriptions_carry_the_contract` — both tools' `description` and `parameters["key"]["description"]` embed the contract.
3. `test_ai_model_key_rejected_as_unknown` — direct `execute()` with `key="ai_model"` fails with `Unknown setting key 'ai_model'`; nothing persisted to `config_store`, nothing cached in `settings_service`.
4. `test_ai_model_key_rejected_even_after_owner_confirmation` — `ToolExecutor.execute_confirmed` with `ai_model` still fails closed.
5. `test_model_change_still_requires_confirmation` — `settings_set {key: "model"}` through `execute_calls` returns `needs_confirmation=True`; nothing persisted before confirmation.
6. `test_confirmed_model_change_persists_applies_and_serves_next_request` — the full integration path: confirmed `model` change → persisted to `config_store` → applied to the runtime `ProviderManager` → the NEXT `Engine.execute()` request is served by the new model (recording stub provider).
7. `test_prompt_builder_renders_canonical_provider_model_labels` — prompt renders `Model: model-a` / `Provider: prov-a`; `AI Model:`/`AI Provider:` absent.

Test types: 1–2 unit/schema-level; 3–4 executor-integration fail-closed; 5 confirmation-gate; 6 full AI-path + runtime-state integration; 7 prompt-construction unit. Owner IDs use a dedicated 904xxx range to avoid cross-suite `config_store` pollution (a collision with `test_ai_state_consistency.py`'s 902xxx range was fixed before delivery).

**Executed during THIS audit** (exact commands, actual results — Python 3.10.12, pytest 9.1.1, asyncio 1.4.0 strict mode):

| Command | Result |
|---|---|
| `.venv/bin/python -m pytest tests/test_settings_model_key_contract.py -v -p no:cacheprovider` | **7 passed in 0.26s** |
| `.venv/bin/python -m pytest tests/test_settings_model_key_contract.py tests/test_settings_unknown_key.py tests/test_settings_runtime_switch.py tests/test_confirmation_roundtrip.py -q -p no:cacheprovider` | **93 passed in 0.62s** |
| `.venv/bin/python -m pytest tests/test_ai_menu_state_consistency.py tests/test_tool_health_audit.py tests/test_37_ai_memory_db.py tests/test_09_reply_to_ai.py -q -p no:cacheprovider` | **95 passed, 1 warning in 6.17s** |
| `.venv/bin/python -m pytest tests/ -q -p no:cacheprovider` (full suite) | **1710 passed, 23 skipped, 1 warning in 62.58s** |

The audit-time results match the results recorded in the commit's own §17.5 (1710 passed, 23 skipped).

**Not covered by tests:** live Telegram end-to-end behavior (trigger-word model change in a reply-to-AI context, owner confirmation in a real chat) and real-provider schema rendering (tests use a stub registry/provider; a live provider's schema translation is exercised only through the shared dispatcher plumbing).

## 8. DATABASE / SUPABASE IMPACT

- **No database/schema code changed.** `git show --name-status 8b73497` lists no file under `backend/db/`, `backend/ai/database/`, `backend/ai/persistence.py`, or `supabase/`. No migration was added or modified.
- Runtime data flow through existing stores is unchanged: `config_store` writes (`update_model`, `update_provider`, `update_setting`) and `settings_service` writes keep their pre-commit behavior and tables (`ai_config`, `panel_settings`).
- **Live Supabase verification: NOT performed** (neither at commit time nor during this audit). The in-memory fallback is what the test suite exercises.

## 9. TELEGRAM / LIVE VERIFICATION

**NOT performed.** No live Telegram connection exists in this workspace (no credentials). The original failure was reported from a live session; the fix is source-verified and in-process-test-verified only. The commit's own report (§17.7) states the same. Required manual live check (unchanged): in a chat replying to an AI message, ask the AI to change the model — the tool call must use `key="model"` (not `ai_model`), and after owner confirmation the next request must be served by the new model.

## 10. SECURITY / ARCHITECTURE

| Boundary | Changed by 8b73497? | Detail |
|---|---|---|
| Self Bot execution authority | Preserved | No new executor path; tools still run only via `ToolExecutor` |
| ToolRegistry | Preserved | No registration, permission, or schema-plumbing change; only description text |
| ToolExecutor / confirmation flow | Preserved | `settings_set` remains `ADMIN_ONLY` (`needs_confirmation` before persistence); pinned by `test_model_change_still_requires_confirmation` |
| ProviderManager / runtime selection | Preserved | `_apply_runtime_selection` → `engine.apply_runtime_selection` → `ProviderManager.apply_selection` unchanged; the provider-registration check is unchanged |
| Database authority boundaries | Preserved | Same two stores, same owners, no new store |
| RuntimeSupervisor / TelegramAPI | Preserved | No hunks in `backend/runtime/` or `backend/telegram_api/` |
| AI execution boundaries | Preserved | AI still cannot execute arbitrary Telegram RPC / SQL / shell; the commit only changes *what the model is told* |

**Security-relevant nuance (called out explicitly):** the commit *discloses information to the model* — the complete valid-key list is now visible in tool schemas. This is bounded to keys that already exist in the two authoritative stores, grants no new capability (every key was accepted-or-rejected exactly the same way at execution time before and after), and the disambiguation is the point of the fix. The forbidden-alias mention of `ai_model` in the contract text is informational only; the enforced rejection path is unchanged.

## 11. LIMITATIONS / UNVERIFIED

- **Live Telegram verification:** not performed (no credentials in this workspace). The fix's real-world effect on model behavior (no more `ai_model` generations) is therefore UNVERIFIED live.
- **Live Supabase verification:** not performed; DB-path behavior is verified only through the in-memory fallback and stub-backed store assertions.
- **Real-provider schema rendering:** the contract text is unit-tested at the `Tool` property level; its rendering through each provider's specific schema translation (e.g. Gemini `functionDeclarations`) is not provider-tested.
- **Documentation inconsistency found by this audit:** the delivered commit's own §17.8 recorded commit `5453752` as its delivery SHA. `5453752` exists in the object store with the same subject, same parent (`1dda645`), and a diff against `8b73497` of exactly one line (the §17.8 SHA cell itself) — i.e. a pre-`--amend` artifact, unreferenced by any branch. The actual delivered commit is `8b73497`. This report supersedes that record.
- The commit does not address F-2/RC-6 (stale DANGEROUS docstrings) or the P3 notes A-1/A-2/A-3; they remain open as documented in `INVESTIGATION.md`.

## 12. GIT DELIVERY

| Item | Value |
|---|---|
| Audited commit | `8b734976a2e1b9a5df463dad051c1dad4c3482b3` |
| Current HEAD | `8b734976a2e1b9a5df463dad051c1dad4c3482b3` (= `main`) |
| Present locally | Yes (`git cat-file -t` → commit; ancestor of HEAD per `git merge-base --is-ancestor`) |
| Present on origin/main (local ref) | Yes (`git merge-base --is-ancestor 8b73497 origin/main` → yes) |
| Remote verification | `git ls-remote origin refs/heads/main` → `8b734976a2e1b9a5df463dad051c1dad4c3482b3  refs/heads/main` (executed during this audit; matches HEAD exactly) |
| Working tree at audit start | Clean except pre-existing untracked `telegram-self-bot/` nested clone (untouched, excluded from all operations) |
| Pre-amend artifact | `5453752b4e816f8093f266b7b6e294251d4be64e` — same change pre-amend; unreferenced by any branch; superseded by `8b73497` (see §11) |

The audit above documents commit `8b73497` and the repository state at its audit time. The current delivered state (including this session's RC-5 completion) is recorded in §13–§14.

## 13. RC-5 VERIFICATION LEDGER (current state, this session)

All 8 behavioral proofs required for RC-5 completion, with the tests that pin them:

| Required proof | Result | Evidence |
|---|---|---|
| 1. Unknown panel key through `SettingsSetTool` fails | PASS | `test_settings_set_tool_unknown_key_fails_closed` |
| 2. Unknown key never enters the settings cache | PASS | `UNKNOWN_KEY not in settings_service.get_all()` asserted in the service, tool, confirmed, and web tests |
| 3. Unknown key never persists | PASS | `patch("backend.services.panel_settings_repository.update_field").assert_not_called()` in the service and web tests |
| 4. Unknown key through the real `ToolExecutor` path fails closed | PASS | `test_execute_confirmed_unknown_key_fails_closed` |
| 5. Confirmed unknown key also fails closed | PASS | same test: `needs_confirmation=False` (gate satisfied), `success=False`, `Unknown setting key` in message, cache clean |
| 6. Existing valid panel key still works | PASS | `test_known_key_valid_value_still_succeeds_through_service`, `test_settings_set_tool_valid_panel_key_still_works`, `test_execute_confirmed_valid_panel_key_still_executes` |
| 7. Existing AI-runtime key routing still works | PASS | `test_settings_set_tool_ai_key_still_routes_to_config_store`; RC-7 contract suite `tests/test_settings_model_key_contract.py` (7 tests) |
| 8. Confirmation requirement for `settings_set` remains intact | PASS | `test_settings_model_key_contract.py::test_model_change_still_requires_confirmation`; `ADMIN_ONLY` gate unchanged in source |

Executed commands and actual results (this session):

| Command | Result |
|---|---|
| `.venv/bin/python -m pytest tests/test_settings_unknown_key.py tests/test_settings_model_key_contract.py tests/test_settings_runtime_switch.py tests/test_confirmation_roundtrip.py -q` | **94 passed in 0.81s** |
| `.venv/bin/python -m pytest tests/ -q -p no:cacheprovider` (full suite) | **1711 passed, 23 skipped in 62.52s** |
| `.venv/bin/python -m py_compile backend/services/settings_service.py backend/ai/tools/settings.py backend/ai/tools/executor.py backend/ai/confirmation.py backend/web/app.py tests/test_settings_unknown_key.py` | OK |
| `git diff --check` | clean |

## 14. GIT DELIVERY (current state)

| Item | Value |
|---|---|
| Branch | `main` |
| Implementation commit | 2cc63032d8bc9137dd06afc68c8e29d74b4bc78d (`fix: complete RC-5 regression coverage (web PATCH /api/settings boundary)`) |
| Report/delivery commit | the commit carrying the final version of this file |
| Push result | `git push origin main` — non-force fast-forward |
| Remote verification | `git fetch origin`; `git rev-parse HEAD` == `git rev-parse origin/main` == `git ls-remote origin refs/heads/main`, verified after push |
| Final working tree | clean except pre-existing untracked `telegram-self-bot/` nested clone (untouched) |
| Live Telegram / Supabase verification | **NOT performed** (no credentials in this workspace) — RC-5 is source-verified and in-process-test-verified only |

---

## 15. REMEDIATION OF SMALL SOURCE-PROVEN FINDINGS (RC-6 / A-1 / A-2 / A-3) — 2026-09-06

Current-state section for the bounded remediation delivered after §14. Sections §1–§14
remain the RC-5 current-state report and the retained `8b73497` audit.

### 15.1 Scope decision

| Finding | Addressed? | Basis (current source) |
|---|---|---|
| RC-6 / F-2 — DANGEROUS permission docstring drift | YES (documentation only) | `ToolExecutor._is_auto_executable()` (executor.py:338) executes READ_ONLY/READ_WRITE/DANGEROUS directly; only ADMIN_ONLY/CONFIRMATION_REQUIRED gate. `base.py`/`delete.py`/`organize.py` docstrings claimed the opposite — stale, aligned now. `settings.py`'s ADMIN_ONLY docstring was already accurate (ADMIN_ONLY genuinely gates) and was left untouched. |
| A-1 — `delete_messages_by_ids` description overclaim | YES (model-facing description only) | The enforced boundary lives in `delete_service.delete_verified_self_messages` (re-fetch per chunk, `_is_self_owned` fail-closed: server `out` flag + sender==me) — untouched. Only the schema text changed. |
| A-2 — `settings_get` unset AI key phantom value | YES (read-path honesty) | `SettingsGetTool.execute` returned `success=True, "key = "` for empty/missing AI keys. Now fails with an explicit not-set message. Routing/authorities untouched. |
| A-3 — `web_search` swallowed failure reason | YES (bounded reason) | `ProviderManager.web_search` already produced detailed `error` strings (flowed via `⚠️ Web search failed: {error}`); only the two catch-alls collapsed to a generic message. Both now surface a bounded, secret-redacted reason. |
| A-4 — save/save_by_link long_running exemption | NOT implemented — by decision | Re-examined per instructions: NOT a correctness defect. A tool-level timeout would abort legitimate large Deep Save transfers mid-flight; the 60 s request-level `wait_for` plus the 120 s pending-input expiry remain the real bounds. Left as the documented bounded design. |

### 15.2 Files changed

| Path | Category | Change |
|---|---|---|
| `backend/ai/tools/base.py` | production (docstring) | `PermissionLevel` docstring now states the real authorization model: owner's outgoing message IS the authorization; READ_ONLY/READ_WRITE/DANGEROUS execute directly; ADMIN_ONLY/CONFIRMATION_REQUIRED go through the owner-confirmation round-trip via `execute_confirmed` |
| `backend/ai/tools/delete.py` | production (docstring) | module docstring aligned (DANGEROUS executes directly; deletions bounded by re-fetch + outgoing-only + same chat) |
| `backend/ai/tools/organize.py` | production (docstring) | module docstring aligned (same model; cleanup bounded by service argument validation) |
| `backend/ai/tools/semantic.py` | production (model-facing description) | `DeleteMessagesByIdsTool.description`: removed the unenforced "MUST have been returned by list_recent_messages in this turn" provenance claim; now "Use IDs from list_recent_messages — never invent IDs" + the enforced boundary (re-fetched and re-validated before deletion; invalid/non-outgoing IDs skipped and reported) |
| `backend/ai/tools/settings.py` | production (behavior) | `SettingsGetTool`: unset/empty/None AI-key value → `ToolResult(success=False, "<key> is not set (no value stored for this AI runtime key).", data={key, value:""})`; set keys unchanged (`key = value`, success=True) |
| `backend/ai/tools/websearch.py` | production (behavior) | `WebSearchTool` catch-all now returns `❌ Web search failed: <reason>.` using `web_search_service.sanitize_reason(exc)` |
| `backend/services/web_search_service.py` | production (behavior) | new `sanitize_reason()` — exception type + message, Bearer/header credentials redacted defensively, whitespace-collapsed, ≤200 chars; service catch-all returns `❌ Web search failed: <reason>.`; invalid-result message includes the received type |
| `tests/test_remediation_rc6_a123.py` | test (NEW) | 15 regression tests (see §15.4) |

No schema, migration, configuration, dependency, provider-routing, ToolExecutor, ToolRegistry, or permission-semantics change.

### 15.3 Behavior before → after

| Area | Before | After |
|---|---|---|
| DANGEROUS docs | base/delete/organize docstrings claimed an owner-confirmation round-trip that never exists | docstrings match `_is_auto_executable()`; drift guarded by a source-scanning test |
| `delete_messages_by_ids` schema | claimed turn-scoped ID provenance that is not enforced | states enforced boundary; runtime behavior byte-identical |
| `settings_get` unset AI key | `success=True`, `key = ` (implies a value exists) | `success=False`, `<key> is not set (no value stored for this AI runtime key).` |
| `web_search` unexpected error | generic `❌ Web search failed.` (reason logged as type only) | `❌ Web search failed: <Type>: <message>.` (≤200 chars, Bearer/header secrets redacted) |
| Provider dict errors | `⚠️ Web search failed: {error}` | unchanged |

### 15.4 Tests added — `tests/test_remediation_rc6_a123.py` (15)

RC-6: direct-execution matrix (READ_ONLY/READ_WRITE/DANGEROUS all execute with no confirmation); ADMIN_ONLY never auto-executes (`needs_confirmation=True`, zero tool calls); `execute_confirmed` is the only gate bypass; CONFIRMATION_REQUIRED never auto-executes; docstring-drift guard (no "must ask the owner" in the three files; "IS the authorization" present).
A-1: description states the enforced boundary and no longer contains "in this turn"/"MUST have been returned"; runtime enforcement pinned through the real service path — a non-outgoing ID (server `out=False`) is rejected, `delete_messages` never called, `get_messages` awaited against the same chat.
A-2: unset AI key → `success=False` + "is not set" (empty string and None both covered); set key unchanged (`model = gemini-2.5-flash`, success=True, data preserved).
A-3: `sanitize_reason` keeps type, redacts `X-API-Key`/`Authorization: Bearer` values, truncates ≤200, handles empty messages; service catch-all propagates the reason; tool catch-all propagates the reason; provider dict-error path unchanged (`⚠️ Web search failed: Web search request timed out.`).

### 15.5 Executed validation (exact commands, actual results)

| Command | Result |
|---|---|
| `.venv/bin/python -m pytest tests/test_remediation_rc6_a123.py -q -p no:cacheprovider` | **15 passed** |
| `.venv/bin/python -m pytest tests/test_26_silent_delete.py tests/test_27_delete_ownership.py tests/test_20_advanced_execution.py tests/test_30_delete_timeout_hardening.py tests/test_52_you_search.py tests/test_tool_health_audit.py tests/test_settings_model_key_contract.py tests/test_settings_unknown_key.py tests/test_confirmation_roundtrip.py -q -p no:cacheprovider` | **243 passed, 1 warning** |
| `.venv/bin/python -m pytest tests/ -q -p no:cacheprovider` (full suite) | **1726 passed, 23 skipped, 1 warning in 62.81s** |
| `.venv/bin/python -m py_compile backend/ai/tools/base.py backend/ai/tools/delete.py backend/ai/tools/organize.py backend/ai/tools/semantic.py backend/ai/tools/settings.py backend/ai/tools/websearch.py backend/services/web_search_service.py tests/test_remediation_rc6_a123.py` | OK |
| `git diff --check` | clean |

### 15.6 Security / architecture impact

- No change to ToolExecutor, ToolRegistry, permission semantics, confirmation flow, provider routing, model/provider state authority, Telegram identity/destination/delete security, or Supabase schema.
- RC-6 is documentation-only; the executor's authorization contract is now PINNED by tests (previously only documented).
- A-1 keeps the real boundary (`delete_verified_self_messages` re-fetch + `_is_self_owned` fail-closed) and the test proves a non-outgoing ID cannot be deleted regardless of where the ID came from.
- A-3 is defense-in-depth: provider error strings are already credential-free (pinned by `test_52_you_search.py`), and `sanitize_reason` redacts header/Bearer patterns in the untrusted message text before it reaches the model or logs. No secret material enters the new output.

### 15.7 Limitations / unverified

- Live Telegram and live Supabase verification: **NOT performed** (no credentials in this workspace).
- Live You.com search behavior remains UNKNOWN (`YDC_API_KEY` unreadable here); the failure-reason changes are verified in-process only.
- `settings_get` unset-key wording is new user-visible text; the Glass UI does not consume `SettingsGetTool`, so no UI impact is expected (source-verified: no importer of `SettingsGetTool` outside the registry/dispatcher path).

### 15.8 Delivery

| Item | Value |
|---|---|
| Implementation commit | 514b9758e6c55472bcfed80ef66785efc332486c |
| Push | `git push origin main` (non-force fast-forward) |
| Remote verification | `git fetch origin`; `git rev-parse HEAD` == `git rev-parse origin/main` == `git ls-remote origin refs/heads/main` after push |
| Final working tree | clean except pre-existing untracked `telegram-self-bot/` nested clone (untouched) |

---

## 16. MEMORY TOOL CONNECTION (§13.1) — 2026-09-06

Current-state section for the memory write/read tool connection delivered after §15.
Sections §13–§15 remain the RC-5 and RC-6/A-1/A-2/A-3 current-state reports.

### 16.1 Scope

Implements INVESTIGATION.md §13.1 (ranked #1 remaining chunk): the three-tier
memory subsystem (`backend/ai/memory/`, repository-backed via
`get_repository_manager().memory`) had a fully wired RETRIEVAL path
(`dispatcher.py` bounded `retrieve_for_prompt` → prompt `[Memory]` section)
and a fully implemented WRITE path with **zero production callers** —
`MemoryManager.store_long/store_permanent` were reachable only from tests.

### 16.2 Files changed

| Path | Category | Change |
|---|---|---|
| `backend/ai/tools/memory.py` | production (NEW) | `MemoryStoreTool` (`memory_store`, READ_WRITE) and `MemoryListTool` (`memory_list`, READ_ONLY). Both resolve the engine-owned `MemoryManager` via `_resolve_memory_manager()` (`get_engine().memory_manager`, in-memory fallback only when the engine is unreachable) — the SAME instance the dispatcher's read path uses; no second memory authority. `memory_store`: owner-scoped, tier (`long`/`permanent`) + category (enum, permanent-restricted to fact/preference/instruction) + importance (0.0–1.0) validation, `MAX_MEMORY_ENTRY_CHARS` pre-check, secret-avoidance guidance in the description; executes the sync store via `asyncio.to_thread` bounded by `MEMORY_WRITE_TIMEOUT_S` (mirrors the dispatcher's `MEMORY_READ_TIMEOUT_S` discipline). `memory_list`: tier/query/limit(1–20) filters over `permanent.retrieve_all` + `long.retrieve`, owner-scoped. |
| `backend/ai/tools/registry.py` | production | Registers both tools in `create_default_registry` (36 → 38 tools). |
| `backend/ai/tools/executor.py` | production | `_STATUS_LABELS` entries for the two new tool names. |
| `backend/ai/memory/limits.py` | production | New `MEMORY_WRITE_TIMEOUT_S = 3.0` (single source of memory bounds). |
| `backend/ai/memory/long.py` | production (honesty) | `store()` now honors the repository's `save()` boolean: rejected → `None` (matches the documented "None if persistence failed" contract). Previously a rejected (e.g. oversized) write returned an unpersisted entry. |
| `backend/ai/memory/permanent.py` | production (honesty) | Same `save()` boolean fix. |
| `backend/ai/memory/manager.py` | production (docstring) | `store_long`/`store_permanent` docstrings state the None-on-failure contract explicitly. |
| `tests/test_memory_tools.py` | test (NEW) | 20 regression tests (§16.4). |
| `tests/test_tool_health_audit.py` | test | `EXPECTED_TOOLS` += `memory_store`(READ_WRITE), `memory_list`(READ_ONLY); count 36 → 38. |
| `tests/test_capability_exposure_tools.py` | test | Duplicate-registration count 36 → 38. |
| `INVESTIGATION.md` | docs | §13.1 marked IMPLEMENTED with the delivery summary. |

No schema, migration, configuration, dependency, provider-routing,
ToolExecutor-logic, permission-semantics, or Telegram-boundary change. The
executor, dispatcher, and confirmation flow are untouched.

### 16.3 Behavior before → after

| Area | Before | After |
|---|---|---|
| AI memory writes | Unreachable from the tool surface ("remember that …" requests could only be simulated by the model's prose) | `memory_store` persists through the engine-owned manager; confirmation of truth is the tool result, not model narration |
| Memory reads | Prompt injection only (implicit) | `memory_list` gives the model explicit, filtered read access |
| Tier-store rejected writes | Returned an unpersisted `MemoryEntry` (dishonest success) | Return `None` → tool reports `success=False` |
| Slow/hanging memory store | Would have blocked the calling tool for the generic 10s tool timeout | Fails honestly at `MEMORY_WRITE_TIMEOUT_S` (3s) |
| Registry | 36 tools | 38 tools (audit tests updated) |

### 16.4 Tests added — `tests/test_memory_tools.py` (20)

Registration (38 unique names) and schema/permission contract; long-tier and
permanent-tier store success (default categories `summary`/`fact`, data
payload); missing/oversized content, unknown tier/category, permanent-tier
category restriction, invalid importance; honest failure on repository
rejection (save → False → None) and repository exception; bounded execution
against a hanging repository (timeout surfaces as honest failure);
owner-scoping (owner 2 cannot see owner 1's entries); repository-level
duplicate-content dedup (idempotent writes); list rendering with tier and
query filters, empty-state, unknown tier/limit validation, bounded read; and
the resolver contract (engine manager preferred, in-memory fallback only when
the engine is unreachable).

### 16.5 Executed validation (exact commands, actual results)

| Command | Result |
|---|---|
| `.venv/bin/python -m pytest tests/test_memory_tools.py -q -p no:cacheprovider` | **20 passed** |
| `.venv/bin/python -m pytest tests/test_37_ai_memory_db.py tests/test_tool_health_audit.py tests/test_capability_exposure_tools.py tests/test_10_tool_calls.py tests/test_confirmation_roundtrip.py tests/test_settings_unknown_key.py tests/test_settings_model_key_contract.py tests/test_remediation_rc6_a123.py -q -p no:cacheprovider` | **211 passed, 1 warning** |
| `.venv/bin/python -m pytest tests/ -q -p no:cacheprovider` (full suite) | **1746 passed, 23 skipped, 1 warning in 63.88s** |
| `.venv/bin/python -m py_compile backend/ai/tools/memory.py backend/ai/tools/registry.py backend/ai/tools/executor.py backend/ai/memory/limits.py backend/ai/memory/manager.py backend/ai/memory/long.py backend/ai/memory/permanent.py tests/test_memory_tools.py` | OK |
| `git diff --check` | clean |

### 16.6 Security / architecture impact

- Single memory authority preserved: tools call the manager the Engine owns;
  no new manager, no new repository, no second state. `test_resolver_*` pins
  this.
- Owner-scoped by construction (`context.owner_id` on every write/read);
  cross-owner isolation covered by test.
- WRITE permission is READ_WRITE (auto-executable): memory writes have no
  Telegram side effect, are owner-scoped, size- and time-bounded — the same
  authorization class as bio/username writes, consistent with the
  executor's documented model (owner's outgoing message IS the
  authorization; DANGEROUS/ADMIN_ONLY semantics unchanged).
- The model-facing description discourages storing secrets; content is
  capped at `MAX_MEMORY_ENTRY_CHARS` and the write is time-bounded.
- Retrieval-side bounds (records, token budget, ordering, dedup) were
  already pinned by `test_37_ai_memory_db.py` and are unaffected (verified:
  211-test adjacent run green, including "no writes during normal
  execution").

### 16.7 Limitations / unverified

- Live Telegram / live Supabase verification: **NOT performed** (no
  credentials in this workspace). Persistence is verified through the
  in-memory repository and the Supabase repository's existing unit coverage;
  a live `ai_memories` round-trip remains unverified.
- The model must CHOOSE `memory_store` — no deterministic parser route was
  added (memory phrasing is open-ended; the provider path with the
  registered tool schema is the authority, consistent with the
  task-management routing precedent in `actions.py`).
- ShortMemory remains per-request RAM-only by design and is intentionally
  not tool-exposed.

### 16.8 Delivery record (verified)

| Item | Value |
|---|---|
| Delivery commit | `e2680846c3d1e55390d7c186382b8f5b75ba01b7` (`feat: connect memory subsystem to the AI tool surface (memory_store/memory_list)`) |
| Push | `git push origin main` from `/home/daytona/codebase` → `ab8050e..e268084 main -> main` (non-force fast-forward, succeeded) |
| Remote proof | `git ls-remote origin refs/heads/main` → `e2680846c3d1e55390d7c186382b8f5b75ba01b7` == `git rev-parse HEAD` == `git rev-parse origin/main` (after fresh `git fetch origin`) |
| Validation on the committed tree | `tests/test_memory_tools.py` 20 passed; adjacent set (memory DB, tool health audit, capability exposure, tool calls, confirmation round-trip, settings RC-5, RC-6/A-1/A-2/A-3) 211 passed; full suite **1746 passed, 23 skipped** in 63.76s; `py_compile` OK on all changed modules; `git diff --check` clean |
| Working tree after delivery | clean except the pre-existing untracked nested clone `telegram-self-bot/` (separate stale repository, intentionally untouched) |
| Not verified | Live Telegram / live Supabase round-trip (no credentials in this workspace) |

---

## 17. SAVED-ITEM RETRIEVAL OWNER ISOLATION — 2026-09-07

### 17.1 Exact defect

`retrieve_service.do_retrieve(client, owner_id, save_code, chat_id)` looked up the
`saved_items` row by `save_code` alone and forwarded it without ever comparing
`row["owner_id"]` to the trusted `owner_id`. The `retrieve_save` AI tool passes the
trusted `context.owner_id`, but the service ignored it for authorization — so a
save-code collision with another owner's row would forward that row to the
requesting chat.

### 17.2 Root cause

`do_retrieve` was the only saved-item operation missing the ownership gate that
`do_rename` / `do_move` / `do_delete` already enforce
(`if not row or row.get("owner_id") != owner_id`). The Retrieve audit
(section on retrieval/file-retrieval, previous session) flagged this as the one
real gap in an otherwise sound chain.

### 17.3 Fix (before / after)

| | Behavior |
|---|---|
| **Before** | Any row matching the save code was forwarded: `get_input_entity` + `forward_messages` ran regardless of `owner_id`. |
| **After** | `do_retrieve` returns the established not-found wording `❌ No item found for \`{save_code}\`` when `not row or row.get("owner_id") != owner_id` — BEFORE any `get_input_entity` / `forward_messages` / caption edit. Cross-owner rows are indistinguishable from missing rows (no data leak). Missing/absent `owner_id` fails closed. Authorized-owner success behavior is byte-identical to before. |

### 17.4 Files changed

| File | Change |
|---|---|
| `backend/services/retrieve_service.py` | Added owner check in `do_retrieve` before all Telegram side effects (5 insertions, 1 deletion). |
| `tests/test_retrieve_owner_isolation.py` | New regression suite — 9 tests covering all 8 required cases plus destination-protection pinning. |

### 17.5 Tests added

1. Authorized owner retrieves their own item successfully (`✅`, `forward_messages` called).
2. Different owner cannot retrieve the item (`❌ No item found …`).
3. Cross-owner retrieval does NOT call `get_input_entity`.
4. Cross-owner retrieval does NOT call `forward_messages` (nor `edit_message`).
5. Missing/absent `owner_id` on the row fails closed.
6. Unknown code still fails without Telegram side effects.
7. Cross-owner failure wording is identical to missing-code wording (no data leak).
8. Full executor → `retrieve_save` tool → service chain returns `success=False` and never touches the Telegram client for a cross-owner row.
9. Destination protection unchanged (model-supplied `destination`/`chat_id` ignored; trusted context `chat_id` used) and success/result mapping unchanged for authorized owners.

### 17.6 Validation results (committed tree `8c7e705`)

- Focused: `tests/test_retrieve_owner_isolation.py` + `test_capability_exposure_tools.py` + `test_new_tool_action_path.py` → **73 passed** in 0.32s.
- Full suite: `pytest tests/ -q` → **1782 passed, 24 skipped** in 63.36s.
- `py_compile` OK on both changed Python files.
- `git diff --check` clean.

### 17.7 Delivery record (verified)

| Item | Value |
|---|---|
| Delivery commit | `8c7e70596239094ae0901db61f1c8aa49b720b53` (`fix: enforce owner isolation for saved-item retrieval`) |
| Push | `git push origin main` → `83b0dac..8c7e705 main -> main` (non-force fast-forward, succeeded) |
| Remote proof | `git fetch origin`; `git rev-parse HEAD` == `git rev-parse origin/main` == `git ls-remote origin refs/heads/main` → `8c7e70596239094ae0901db61f1c8aa49b720b53` |
| Working tree | Clean except the pre-existing untracked nested clone `telegram-self-bot/` (separate stale repository, intentionally untouched) |

### 17.8 Explicit statements

- **No database / schema / migration change was made.** `saved_items` schema, RLS, indexes, and `ai_memories` are untouched.
- **Live Telegram / live Supabase verification was NOT performed** (no credentials in this workspace). The fix is verified through the in-process suite against the seeded in-memory fallback database layer.
- Bare-number resolution (`379` → `S0379`) was intentionally NOT implemented — it remains a separate future task.
- Remaining known limitation: `query_save(save_code)` still resolves by code alone (shared with the sibling mutation ops); the owner check now makes a cross-owner collision fail closed instead of forwarding.
