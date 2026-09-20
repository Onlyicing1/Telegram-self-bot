# IMPLEMENTATION REPORT — CURRENT STATE

## Latest phase — Database architecture repair: canonical schema reconciliation

Repository `Onlyicing1/Telegram-self-bot` · branch `main` · state as of 2026-09-20.

### Phase identity

| Item | Value |
|---|---|
| Phase name | **Canonical schema reconciliation & drift repair** — `DATABASE_ARCHITECTURE.md` becomes a specification whose executable SQL is safe on an EXISTING database instead of a description that only works on an empty one |
| Starting HEAD | `a065e1d` `feat(vault): manage provider API credentials from Telegram` (== `origin/main` at phase start) |
| Implementation commit | the single phase commit that contains this report (`git log -1 --format=%H` re-verifies it) |
| Migration shipped | **`supabase/migrations/20260920000001_reconcile_canonical_schema.sql`** — the complete canonical reconciliation script (§30 of the database document) |
| Supabase executed by this phase | **NO** — no connection, no SQL, no Vault secret, no schema change was performed by the coding agent |
| Database objects touched | the canonical script now reconciles **16** public tables / **180** columns / **21** identity constraints; it adds **no** table that a migration did not already create, **no** secret store and **no** column beyond the ones live code already writes |
| New environment variables | **NONE** |
| New dependencies | **NONE** (`requirements.txt` untouched) |
| Files changed | **five** — NEW `supabase/migrations/20260920000001_reconcile_canonical_schema.sql`, NEW `tests/test_canonical_schema_reconciliation.py`; MODIFIED `supabase/canonical_bootstrap.sql`, `DATABASE_ARCHITECTURE.md`, and this report |
| Application code changed | **NONE** — not one Python module under `backend/`. The defect was in the specification/SQL, so the fix is in the specification/SQL |
| Behavioural change | **none at runtime**; the only behavioural consequence is that pasting the documented SQL into Supabase now converges an existing database instead of aborting |
| Persisted state | **NONE added by the application**; the script's only INSERTs are the deterministic seeds that already existed |
| Live Supabase verification | **NOT PERFORMED** — the script was never executed against any database; see "Validation performed" for exactly what was run instead |
| Live Telegram verification | **NOT PERFORMED** |

### The reported defect

Applying the document's "complete copy-pasteable" SQL to the existing Supabase
project failed with:

```
ERROR:  42703
column "value_type" of relation "bot_settings" does not exist
INSERT INTO bot_settings (key, value, value_type) VALUES ...
```

**Root cause — the mechanism, not the column.** The canonical SQL (and the
migration history it was reconstructed from) established every table with
`CREATE TABLE IF NOT EXISTS`, then immediately ran statements referencing
columns:

```sql
CREATE TABLE IF NOT EXISTS bot_settings (
    key text PRIMARY KEY, value text NOT NULL,
    value_type text NOT NULL DEFAULT 'str', updated_at timestamptz DEFAULT now()
);
INSERT INTO bot_settings (key, value, value_type) VALUES ...;   -- 42703
```

`CREATE TABLE IF NOT EXISTS` is a **silent NO-OP** when the table exists. The
live `bot_settings` predated `value_type`, so the CREATE did nothing, nothing
added the column, and the INSERT failed. `IF NOT EXISTS` was being used as the
*reconciliation* mechanism, and reconciliation was never actually performed.
Adding `ALTER TABLE bot_settings ADD COLUMN value_type …` alone would have fixed
the reported symptom and left the class intact — which is why it was not the fix.

### Additional inconsistencies found by the audit

The audit searched the whole contract for the same class rather than stopping at
the first failure. Everything below was found in the repository (none of it was
supposed; each is now either fixed or explicitly documented as non-canonical):

1. **`panel_settings`** — `20260726143924` created only `(key,
   auto_close_enabled, updated_at)`. The eight CHECK blocks reference
   `auto_close_delay`, `max_deep_save_mb`, … so on a legacy table the *whole*
   `DO $$` body failed to plan. The reconciliation must run **before** the
   constraints, not after.
2. **`saved_items`** — `short_code` / `file_name` arrive with `20260718143752`;
   the unique and trigram indexes reference them, so a database that predates
   that migration failed on `CREATE UNIQUE INDEX … (short_code)`.
3. **`ai_config` — four columns were missing from the canonical contract
   entirely.** `backend/ai/config_store.py` writes `show_question`,
   `stt_model`, `stt_language` and `stt_passes` in **every** upsert payload
   (migrations `20260913000000`, `20260917000001`). A database built from the
   canonical script alone therefore rejected the whole upsert and silently lost
   AI settings on restart — a data-loss defect hidden inside a "complete" spec.
4. **`ai_tasks` / `ai_task_occurrences` were missing from the canonical script**
   although `backend/ai/task_scheduler.py` and `task_execution.py` depend on
   them. `ai_task_occurrences` is the *second* confirmed instance of the class:
   `20260912000001_add_ai_task_occurrences_preparation_metadata.sql` exists
   precisely because `preparation_metadata` never reached tables created before
   it — the migration's own comment says so.
5. **Constraint drift** — a legacy table without the documented UNIQUE made a
   targeted `ON CONFLICT (key) DO NOTHING` unresolvable, and no PRIMARY
   KEY/UNIQUE was ever asserted, so a table missing one stayed broken silently.
6. **The document's own claim was false.** "A byte-identical copy of this script
   also lives at `supabase/canonical_bootstrap.sql`" — the two differed by three
   header lines. Now they are identical *and* a test enforces it.
7. **Stale migration-status prose** — `20260827000001`,
   `20260827000002`, `20260827000003` and `20260827000004` existed but §20 still
   listed their work as ungenerated; §19.3 still said the `panel_settings`
   columns "have no migration file". Corrected in place.
8. **Deliberately left out of the canonical contract** (documented, not
   migrated): `ai_messages.tool_calls` (no reader, no writer — §19.5) and
   `ai_preferences` (no migration, in-memory only — §14, §19.17). Adding them
   would invent a contract the code does not have.

### What the repair does

Every canonical table now follows one fixed sequence:

```
CREATE TABLE IF NOT EXISTS <t> ( … canonical definition … )
ALTER TABLE <t> ADD COLUMN IF NOT EXISTS <col> <type> [DEFAULT <d>]   × 180
UPDATE <t> SET <col> = <deterministic value> WHERE <col> IS NULL      × every NOT NULL column
ALTER TABLE <t> ALTER COLUMN <col> SET DEFAULT <d>                    × every defaulted column
ALTER TABLE <t> ALTER COLUMN <col> SET NOT NULL                       × every NOT NULL column
… indexes, data-guarded CHECKs/foreign key, guarded unique indexes …
… one consolidated identity-constraint block (21 PRIMARY KEY / UNIQUE) …
NOTIFY pgrst, 'reload schema'
COMMIT
-- drift report: any canonical (table, column) still missing (must be empty)
```

* **New-column safety**, the rule that failed before: add (with the canonical
  default when there is one — safe on an existing table because PostgreSQL seeds
  existing rows from it) → **backfill every remaining NULL deterministically** →
  only then enforce `DEFAULT`/`NOT NULL`. The backfill runs for *every* NOT NULL
  column, so a legacy *nullable* column is converged too and `SET NOT NULL`
  cannot fail.
* **Guarded additions**: the 27 CHECK constraints, the task foreign key, the two
  data-dependent unique indexes and all 21 identity constraints are applied only
  when the existing rows can satisfy them; otherwise the script raises a
  `WARNING` naming the table, the constraint and the offending row count and
  **continues** (39 `RAISE WARNING` guards).
* **Non-destructive**: no `DROP TABLE`, `DROP COLUMN`, `TRUNCATE` or
  `DELETE FROM`. The only drops are stale anon **write** policies the documented
  model forbids, and `DROP CONSTRAINT IF EXISTS` inside a guard that recreates
  the constraint canonically.
* **Scope guard**: the script contains **no** `vault.*` reference and never
  names the §29 credential objects in executable SQL, so reconciliation can
  never read, move or delete a secret.

### The three-copy contract

The same text now exists in three places, kept **byte-identical** and enforced
by test — one reconciled definition, no duplication that can drift:

1. the fenced SQL block in `DATABASE_ARCHITECTURE.md` §30 +
   `### The script (single copy-pasteable block …)` — the canonical reference,
2. `supabase/canonical_bootstrap.sql` — convenience copy,
3. `supabase/migrations/20260920000001_reconcile_canonical_schema.sql` — the
   forward-only repository migration.

The migration is **not** a rewrite of history: every `202607…`–`20260919…`
migration file is byte-untouched, the repair is the newest file in
`supabase/migrations/`, and a test asserts that no other migration was edited to
reference it.

### Documentation added

`DATABASE_ARCHITECTURE.md` gains **§30 Canonical Schema Reconciliation & Drift
Repair** (§30.1 defect → §30.2 the nine-item drift audit → §30.3 the contract
incl. the identity block → §30.4 the 180-column inventory → §30.5 the migration
→ §30.6 the drift report → §30.7 safety rules → §30.8 validation → §30.9 the
three-part rollback → §30.10 the optional destructive cleanup, kept separate →
§30.11 the exact manual Supabase action → §30.12 what is NOT proven). §20's
migration-status tables and §19.1/§19.3/§19.8 were corrected to match what is
actually in `supabase/migrations/`, and the canonical table list went from 14 to
16 tables.

### Tests and exact results

New suite `tests/test_canonical_schema_reconciliation.py` — **32 tests**, all
passing. It works in three registers:

* **static identity / consistency** — the three copies are byte-identical; every
  table's CREATE column set equals its `ADD COLUMN` set equals its drift-report
  set; no canonical column is established by a `CREATE … IF NOT EXISTS` alone;
  all 16 tables appear in the identity block; the script is additive-only; the
  security model is intact (no `FOR ALL`, no anon write policy); every
  data-dependent addition is guarded by a warning.
* **simulated execution** — the test parses the real statements of the shipped
  script and applies them with PostgreSQL's semantics to an empty schema, the
  worst-case legacy schema (all nine drift items at once) and an
  already-canonical schema, failing on any unresolved table/column reference.
* **the exact regression required** —
  `test_the_reported_production_failure_is_reproducible_and_fixed` first
  *reproduces* 42703 by applying the old pattern to a `bot_settings` without
  `value_type`, then proves the shipped script converges instead: `value_type`
  exists, is NOT NULL, defaults `'str'`, the three pre-existing rows keep their
  values and gain `value_type = 'str'`, the five seeds land, and the drift
  report is empty.

| Command | Result |
|---|---|
| `pytest tests/test_canonical_schema_reconciliation.py -q` | **32 passed** |
| `pytest` on the credential + STT + media + TTS + this-suite batch (12 files) | **736 passed, 2 skipped** |
| `pytest tests/ -q` (full suite) | **4362 passed, 26 skipped** in 116.30 s |
| Baseline at the starting HEAD | **4330 passed, 26 skipped** → **+32, none removed or weakened** |
| `python -m py_compile tests/test_canonical_schema_reconciliation.py` | clean |
| `git diff --check` | clean |

### Validation performed — and what it does not prove

**NOT performed, and not claimed:** no Supabase connection, no SQL executed
against any project, no Vault secret created, no schema modified. **No local
PostgreSQL server exists in the build environment either**, so the executed
evidence is a faithful simulation of the statement semantics that matter for
this defect (silent no-op CREATE, unresolved column reference, NOT NULL
enforcement, `ON CONFLICT` skipping, default seeding of existing rows) — not the
real planner. `DO $$` guards, `CREATE POLICY`, `GRANT` and `NOTIFY` are
validated for identifiers and text, not executed. The drift report checks column
*existence*; column *types* are not compared against the live database (a type
conflict means the table is not this table and needs manual review). §30.12
states all of this in the document itself.

### Exact manual Supabase action still required

1. SQL Editor as `postgres` → run the **complete** canonical script (the §30
   block, `supabase/canonical_bootstrap.sql` or the migration file — identical).
2. Read the output: `WARNING` lines name any constraint a pre-existing row
   blocked (fix those rows, re-run); the final `missing_canonical_column` result
   set must be **empty**.
3. Nothing else — no table to create by hand, no env var, no Render setting, no
   Vault change.

Rollback: the repair is additive, so **there is no safe data-level rollback and
none is needed**; §30.9 separates the reversal of constraints/indexes (no row
touched), the reversal of the six contract columns (data-losing, per column) and
the reversal of the seeds (data-losing, not recommended), and states that
backups are the only contract that can be honoured for a converging schema. The
§20 cleanup proposals live in §30.10, separated and labelled OPTIONAL and
DESTRUCTIVE.

### Deferred

Live application of the script and the §29 credential-vault objects against the
owner's own Supabase project; verifying the RLS posture and the PostgREST schema
cache live; comparing live column **types**; and the outstanding product work
recorded in the previous phases (TTS provider fallback and its credential pool,
TTS voice/model selection, Native Vision, Video/GIF, provider benchmarking, the
`ai_preferences` decision and the dead-column cleanup decision).

## Previous phase — API Credential Vault PART 2: owner-facing credential management

Repository `Onlyicing1/Telegram-self-bot` · branch `main` · state as of 2026-09-19.

### Phase identity

| Item | Value |
|---|---|
| Phase name | **API Credential Vault PART 2 (owner-facing credential management)** — the Telegram surface, the management service and the five owner-scoped SECURITY DEFINER functions that make a stored credential manageable without ever exposing it |
| Starting HEAD | `f5d93f0` `feat(vault): store credential metadata in Postgres and resolve secrets from Supabase Vault` (== `origin/main` at phase start, i.e. the PART 1 commit) |
| Implementation commit | the single phase commit that contains this report (`git log -1 --format=%H` re-verifies it) |
| Migration shipped | **`supabase/migrations/20260919000002_credential_vault_management.sql`** — functions only; it depends on PART 1 having been applied first |
| Supabase executed by this phase | **NO** — no SQL was run, Supabase was **not** modified, no function was created, no Vault secret was created and no existing key was deleted |
| Database objects created (pending owner action) | `public.api_credential_list(bigint, text)` · `public.api_credential_create(bigint, text, text, text, integer, boolean)` · `public.api_credential_replace_secret(bigint, text, text)` · `public.api_credential_update(bigint, text, text, boolean, integer)` · `public.api_credential_delete(bigint, text)` — no table, no column, no index, no policy, no extension |
| New environment variables | **NONE** — deliberately none, and none read by the new code either |
| New dependencies | **NONE** (`requirements.txt` untouched) |
| Files changed | **ten** — NEW `supabase/migrations/20260919000002_credential_vault_management.sql`, NEW `backend/services/credential_service.py`, NEW `backend/bot/handlers/ai_credentials.py`, NEW `tests/test_credential_management.py`; MODIFIED `backend/bot/handlers/ai_stt_settings.py`, `backend/bot/router.py`, `backend/services/stt_credential_pool.py`, `tests/conftest.py`, `DATABASE_ARCHITECTURE.md`, `IMPLEMENTATION_REPORT.md` |
| Behavioural change | **additive only**: the Media Analysis hub gains one row, the runtime gains three panels and six actions, and `stt_credential_pool` gains one public read-only accessor (`env_var_names`). No provider selection, model choice, recognition, chunking, rotation, fallback or delivery behaviour changed |
| Persisted state | **NONE added by the application** — credential metadata already existed (PART 1); a credential TEST observation is process-local and is never persisted |
| Live Supabase verification | **NOT PERFORMED** — the management path was exercised only against a fake store in `tests/test_credential_management.py`; no Supabase project was contacted |
| Live Telegram verification | **NOT PERFORMED** — no Telegram session exists in this environment |

### Purpose of this phase

PART 1 made a credential **resolvable**; PART 2 makes it **manageable**. The owner
no longer needs to touch Supabase to use a second key:

```
                    Telegram owner
                         │
                         ▼
        AI → Media Analysis → API Credentials      ai_credentials.py
                         │
                         ▼
             credential management service         credential_service.py
                         │
                         ▼
            five owner-scoped SECURITY DEFINER functions
                    /                      \
                   ▼                        ▼
        public.api_credentials          vault.secrets
        (metadata only — still        (the raw key, written only
         no secret column)             through vault.create_secret)
                   │
                   ▼
         public.api_credential_pool   (PART 1, UNCHANGED)
                   │
          ┌────────┴────────┐
          ▼                 ▼
     STT providers     future TTS providers
```

The store stays **generic** by the `provider` token. There is deliberately no
`tts_credential_pool.py`, no `tts_credential_source.py`, no `stt_credentials.py` and
no second secret architecture — the same table, the same resolution function and the
same five management functions serve `gemini`, `groq`, `speechmatics` and `openai`.

### The five functions

| Function | Purpose | Secret access | Returns |
|---|---|---|---|
| `api_credential_list(p_owner_id, p_provider DEFAULT NULL)` | the owner's credential metadata, optionally for one provider | **none** — it never reads a secret column or view | 7-column metadata, ≤ 64 rows |
| `api_credential_create(p_owner_id, p_provider, p_label, p_secret, p_priority DEFAULT 0, p_enabled DEFAULT true)` | creates the Vault secret AND the metadata row referencing it | **writes** one (straight into `vault.create_secret`) | the metadata row |
| `api_credential_replace_secret(p_owner_id, p_credential_id, p_secret)` | swaps in a NEW secret and removes the old one | **writes** one | the metadata row |
| `api_credential_update(p_owner_id, p_credential_id, p_label DEFAULT NULL, p_enabled DEFAULT NULL, p_priority DEFAULT NULL)` | metadata only: label / enabled / priority | **none** — it cannot read or rotate a key | the metadata row |
| `api_credential_delete(p_owner_id, p_credential_id)` | removes the Vault secret and the metadata row | **deletes** one (never returns it) | `boolean` |

Every one of them is `SECURITY DEFINER` with `SET search_path = ''`, owned by
`postgres`, `REVOKE`d from `PUBLIC`/`anon`/`authenticated` and granted to
`service_role` only, takes `p_owner_id` as a required argument and filters every
statement on it, and returns **metadata only** — no return shape in the migration can
hold a secret. There is deliberately **no “show key” function**.

### Secret exposure boundary

| A raw key may exist | A raw key must never reach |
|---|---|
| `vault.secrets` (encrypted), written only by `vault.create_secret` from create/replace | `public.api_credentials` — it still has **no column that can hold one** (PART 2 adds no column) |
| the argument and local variable of the two write functions, for the duration of one call | any return shape, `COMMENT`, or `RAISE` message in the migration |
| one in-flight application request body (`create_credential` / `replace_secret`) and one provider attempt | a log line, a Telegram message, callback data, `ai_config`, an error string returned to Telegram, or a test fixture |

Three independent guards, each pinned by a test: the migration's return shapes are
the metadata projection only; `credential_service` logs **a bounded class and at most
a code — never a database message** and refuses a whole response that contains a
secret-bearing field; and the Telegram surface addresses a credential by a short
non-secret handle (a SHA-256 prefix of the id) so no callback payload can carry a
value. The service additionally refuses any key containing whitespace, which makes an
accidental ordinary chat message fail closed instead of silently becoming a stored
credential.

### Deletion, replacement and orphan handling

* **create** — the metadata `INSERT` runs inside a nested `BEGIN … EXCEPTION` block
  that deletes the secret it just made if the insert fails, then re-raises. A failed
  create cannot leave an unreferenced secret.
* **replace** — new secret first, then the row is switched, and the OLD secret is
  deleted only **after** the switch, because `vault_secret_id` is `ON DELETE
  CASCADE` and deleting it first would have removed the very row being updated. A
  failed switch deletes the new secret instead. If the final cleanup of the old
  secret fails, the swap still stands and one inert, unreferenced secret remains —
  documented, never hidden.
* **delete** — the secret is removed **first** (which cascades the metadata row) and
  the row is then deleted explicitly as a fallback, so no metadata can outlive its
  secret. A refused secret removal aborts the transaction and is reported as a
  failure, never as a success.
* **update** — metadata only; it cannot orphan, rotate or disturb a key.

### The Telegram surface

| Panel / action | What it does |
|---|---|
| `AI → Media Analysis → API Credentials` (`ai_cred`) | one row per provider this build can execute, with the enabled/total count for each |
| one provider (`ai_cred_prov`) | the provider's credentials (label, enabled, priority, last test), whether a **deployment key** exists (present/not present — never named, never shown), and `➕ Add credential` |
| one credential (`ai_cred_one`) | enable/disable, rename, move earlier/later, set priority, replace key, test (when a bounded test exists), delete |
| add / replace input | asks for ONE message containing the key, stores it, **deletes that message**, and reports honestly if the deletion could not be performed |
| delete | asks for confirmation first, then removes the key and the metadata together |

Every credential mutation reloads the affected provider's existing credential pool
(`stt_credential_pool.prepare`), so a key added, enabled, disabled or deleted from
Telegram is in effect on the very next request with no restart — and no provider
selection is ever rewritten.

### The credential test

`credential_service.test_credential` resolves the credential through the **runtime's
own pool**, builds the provider's engine with **exactly that credential** through the
existing `stt_engine_factory.build_engine_with_credential` seam, and makes ONE bounded
request with the existing probe's synthetic tone. It reports a closed state
(`passed` / `unauthorized` / `rate_limited` / `timeout` / `unavailable` /
`not_supported` / `disabled` / `not_found` / `failed`) derived from the adapters' OWN
failure tokens, and the panel states plainly that this proves the provider **accepted
the key** and is **not** a recognition-quality measurement. The test writes nothing
into the STT provider-probe state, so a credential-level failure can never mark a
provider unhealthy; `openai` has no bounded test path in this phase and is reported
to the owner as *not supported*.

### Tests and exact results

| Suite | Result |
|---|---|
| `tests/test_credential_management.py` (**new**) | **99 passed** |
| Regression set — `test_credential_vault`, `test_stt_credential_pool`, `test_stt_fallback`, `test_stt_provider_probe`, `test_ai_stt_settings`, `test_tts_service`, `test_tts_openai_engine`, `test_36_ai_settings_ux`, `test_media_direct_stt` (9 suites) | **537 passed, 2 skipped** |
| **Full suite** | **4330 passed, 26 skipped** in 115.06 s |

The baseline at the starting HEAD `f5d93f0` was **4231 passed, 26 skipped**, so this
phase adds 99 tests and removes or weakens none. `python -m py_compile` is clean on
all seven changed Python files and `git diff --check` is clean.

What the new suite pins, at the level the phase claims:

* the migration and the documented §29.14 SQL are **statement-identical**, every
  function is hardened / `SECURITY DEFINER` / `service_role`-only, no return shape
  can carry a secret, no dynamic SQL exists, and the PART 1 table is not altered;
* the service validates before calling, maps every failure to a bounded class, refuses
  a secret-bearing metadata field outright, keeps the listing bounded, orders exactly
  as the pool resolves, and is owner-scoped (another owner's handle resolves to
  nothing and another owner's row cannot be modified);
* a raw key never reaches a log line on the happy path, on a failure, in a rendered
  panel, in a button label, in a callback payload, in a create/replace outcome, or in
  the repository's own sources — and only the create/replace calls are ever handed one;
* the panels, actions and inputs behave as documented (toggle, ±1 priority, rename,
  priority input, add, replace, delete-with-confirmation, dead handle, vanished
  credential), the key message is deleted and a failed deletion is reported, and an
  unusable reply is refused while still being deleted;
* the existing STT stack is untouched: the pool's public contract, its declared
  environment-variable names, the provider fallback registration and a per-provider
  pool refresh all still behave.

### Supabase status (explicit)

* **Supabase was NOT modified by this phase.** No connection was made, no SQL was
  executed, no function was created and no row was written or deleted.
* **No Vault secret was created by this phase**, and no existing secret was removed.
* The documented SQL in `DATABASE_ARCHITECTURE.md` §29.14 is *not* a summary of the
  migration — it is the migration's statements, byte-identical, with the reversal in
  §29.15 and the manual checklist in §29.18.
* **Live Telegram and live provider verification: NOT PERFORMED.** No provider is
  claimed healthy, no credential is claimed valid and no recognition or synthesis
  quality claim is made anywhere in this phase.

### Exact manual Supabase action still required

1. Apply PART 1 first (§29.10 / `20260919000001_create_api_credential_vault.sql`) as
   `postgres` — it creates the table, the resolution function and the alias.
2. Apply §29.14 / `20260919000002_credential_vault_management.sql` as `postgres` — it
   creates the five management functions. Nothing else is required: no table, no
   seed row, no environment variable, no Render setting.
3. Optional read-only verification:
   `SELECT credential_id, provider, label, enabled, priority, created_at FROM public.api_credentials ORDER BY provider, priority, created_at, credential_id;`
4. Reversal, if ever needed, is §29.15 (the five functions only). Applying neither
   migration leaves the runtime exactly as it is today: the panel reports the store as
   not configured and every provider keeps using its deployment key.

### Explicitly deferred (not in this phase)

TTS provider fallback and a TTS credential pool · a TTS bounded credential test ·
TTS voice/model/format selection · Native Vision · Video/GIF · provider benchmarking
and evidence-based ranking · a persisted credential-health store or history · any
credential-management UI beyond the three panel levels above · the pending
`ai_config` migration and the `DATABASE_ARCHITECTURE.md` §7 refresh · live
verification of PART 1 and PART 2 against the owner's own Supabase project.

### Known limitations (recorded, not hidden)

* A key containing whitespace is refused (documented behavior, and the reason an
  accidental chat message cannot become a credential).
* The replace flow's cleanup of the superseded secret is best-effort; a failure leaves
  one inert, unreferenced secret, recoverable with `SELECT vault.delete_secret('<id>')`.
* A credential test spends one real provider request (a synthetic tone) and its
  observation is process-local: a restart honestly returns every credential to “not
  tested in this session”.
* If the process restarts between the key prompt and the reply, the pending input is
  gone: the reply is not consumed, not stored and not deleted — remove it manually.
* The atomicity of a create/replace is only as good as the surrounding transaction;
  the failure paths above are written to be safe either way, and **no live database
  verification of them has been performed**.

---

## Previous phase — API Credential Vault PART 1: credential metadata + Vault resolution

Repository `Onlyicing1/Telegram-self-bot` · branch `main` · state as of 2026-09-19.

### Phase identity

| Item | Value |
|---|---|
| Phase name | **API Credential Vault PART 1 (infrastructure only)** — a generic, provider-agnostic credential METADATA table plus ONE SECURITY DEFINER resolution function over Supabase Vault |
| Starting HEAD | `88ccfa2` `feat(tts): speak text through a bounded synthesis boundary` (== `origin/main` at phase start) |
| Implementation commit | the single phase commit that contains this report (`git log -1 --format=%H` re-verifies it) |
| Migration shipped | **`supabase/migrations/20260919000001_create_api_credential_vault.sql`** — the repository's first migration for the credential vault, and the first object in this project that uses Supabase Vault |
| Supabase executed by this phase | **NO** — no SQL was run, Supabase was **not** modified, no Vault secret was created, no schema was altered, and no connection was made |
| Database objects created (pending owner action) | `public.api_credentials` (table) · `idx_api_credentials_provider_order`, `idx_api_credentials_owner`, `uq_api_credentials_vault_secret` (indexes) · `public.api_credential_pool(text, bigint)` (function) · `public.stt_credential_pool(text, bigint)` (compatibility alias) |
| New environment variables | **NONE** — deliberately none; Render keeps only the ONE credential per provider plus the Supabase bootstrap secrets |
| New dependencies | **NONE** (`requirements.txt` untouched) |
| Files changed | **five** — NEW `supabase/migrations/20260919000001_create_api_credential_vault.sql`, NEW `tests/test_credential_vault.py`; MODIFIED `backend/ai/credential_source.py`, `DATABASE_ARCHITECTURE.md`, `IMPLEMENTATION_REPORT.md` |
| Behavioural change | **one string**: the credential boundary now calls `api_credential_pool` instead of `stt_credential_pool`. The call shape (`{"p_provider": …}`), the ENV-first precedence, the ordering, the bounds, the fail-closed degradation, the STT pool, the rotation and the provider fallback are all **byte-for-byte unchanged** |
| Persisted state | **NONE added by the application** — the runtime still persists no credential and no secret; only the owner's metadata rows live in the new table |
| Live Supabase verification | **NOT PERFORMED** — **Supabase has NOT been modified by this phase** and no Vault pool exists; the RPC path was exercised only against a fake secret backend in the test suite |
| Live Telegram verification | **NOT PERFORMED** — no Telegram session exists in this environment |

### Purpose of this phase

M2.4 gave the runtime a bounded credential pool and documented a Supabase Vault
RPC — but nothing created it: the table, the function and the mapping existed only
as prose, and the application boundary was written as an STT component. PART 1
supplies the missing infrastructure as **one generic architecture** rather than a
second, TTS-shaped one:

```
ENV credentials            (unchanged — still the FIRST credential of every provider)
        +
Supabase Vault credentials (new — additional, owner-managed, encrypted at rest)
        ↓
generic credential source         backend/ai/credential_source.py
        ↓
bounded credential pool           backend/services/stt_credential_pool.py   (STT consumer)
        ↓
provider execution                the existing adapter for the CURRENT attempt
```

Because the table and the function are keyed by a free-form `provider` token, the
future TTS credential pool is **not** a new module — it is the same
`credential_source.load("openai", ("AI_OPENAI_API_KEY", …))` call. No
`tts_credential_pool.py`, no `tts_credential_source.py`, no second secret store.

Explicitly NOT implemented, per the phase instruction: the Telegram
credential-management UI, any TTS control plane or voice/model selection, Native
Vision, Video/GIF processing, provider benchmarking, account creation, payment
automation, dashboard scraping, arbitrary SQL/shell/Telegram execution, and any
unrelated refactor. **PART 2 (the owner-facing credential surface) is deferred.**

### The database objects

| Object | Kind | Purpose | Contains a secret? |
|---|---|---|---|
| `public.api_credentials` | table | credential bookkeeping: `credential_id`, `provider`, `label`, `owner_id`, `enabled`, `priority`, `vault_secret_id`, `created_at`, `updated_at` | **NO — there is no column anywhere in it that can hold a key** |
| `uq_api_credentials_vault_secret` | UNIQUE index on `(vault_secret_id)` | one Vault secret maps to exactly ONE credential, so rotation can never "rotate" between two names for the same key |
| `idx_api_credentials_provider_order` | index on `(provider, priority, created_at, credential_id)` | backs the deterministic single-provider read |
| `idx_api_credentials_owner` | index on `(owner_id, provider)` | backs the owner-scoped read |
| `public.api_credential_pool(text, bigint)` | SECURITY DEFINER function | the ONE resolution boundary: ordered, ENABLED, decrypted credentials of one provider | the DECRYPTED value is returned to the caller's service-role client and never stored |
| `public.stt_credential_pool(text, bigint)` | SECURITY DEFINER function | deprecated alias of the same contract, kept because the M2.4 report documented that name | as above |

Schema details (types, nullability, defaults, PK, FK + `ON DELETE CASCADE`, all
five CHECK constraints, indexes, RLS, grants, ownership, arguments, return shape,
security boundary, deletion behaviour, orphan handling) are documented in
`DATABASE_ARCHITECTURE.md` **§29**, which also carries the complete manual SQL and
the complete manual rollback SQL, both labelled `NOT EXECUTED BY AI`. A test
asserts that the documented SQL is **statement-identical** to the migration file.

### Vault architecture and the secret boundary

```
vault.secrets (encrypted)  ◄── FK ──  api_credentials.vault_secret_id
        │ decrypted on read
        ▼
vault.decrypted_secrets  ──►  api_credential_pool(p_provider [, p_owner_id])
                              SECURITY DEFINER, search_path = '', OWNER postgres
                              REVOKE PUBLIC/anon/authenticated · GRANT service_role
        ▼
backend/ai/credential_source.py  (ONE db.rpc call, 5 s ceiling, bounded dispatch)
```

* **The application never reads `vault.*`.** Resolution happens inside the
  SECURITY DEFINER function, which runs as its owner; the backend needs no
  privilege on the Vault schema and no knowledge of Vault's internals.
* **The parameter shape is the M2.4 contract unchanged**: the runtime sends
  `{"p_provider": provider}` and omits the optional `p_owner_id`. Owner scoping is
  available at the database boundary (the table is `owner_id NOT NULL` and the
  function filters on it) for the phase that needs it, without a second function.
* **A shared Vault secret cannot be double-mapped** (`uq_api_credentials_vault_secret`),
  so a pool can never silently degrade into repeated attempts with the same key.

### Security model

| Boundary | Rule |
|---|---|
| Raw secret at rest | only inside `vault.secrets`, encrypted by Vault |
| Raw secret in `public` | **never** — `api_credentials` declares no secret-bearing column |
| Raw secret in application memory | only inside the `CredentialRecord` of the attempt that needs it |
| Raw secret in logs / Telegram / AI context | **never** — the only identifier logged is the non-secret `credential_id` |
| Table access | RLS enabled with **zero policies**; `REVOKE ALL … FROM PUBLIC, anon, authenticated`; `GRANT` to `service_role` only |
| Function access | `REVOKE ALL … FROM PUBLIC, anon, authenticated`; `GRANT EXECUTE … TO service_role` only |
| `search_path` hijack | impossible — `SECURITY DEFINER` + `SET search_path = ''` + a fully qualified body |
| Generic SQL execution | **not** created — no dynamic SQL, no `EXECUTE`, no user-supplied identifier, one function with two bounded parameters |
| ENV compatibility | unchanged — the environment credential is still the first and only default; no numbered variables, no environment scanning, no automatic migration of a key into Vault |

`DATABASE_ARCHITECTURE.md` §24.G.3 ("no credentials in the database") is amended in
the same commit rather than silently contradicted: the rule becomes "no raw key in
any table; provider keys live in ENV **or** Vault, and the database holds metadata
only".

### Files changed by this phase

| File | Change |
|---|---|
| `supabase/migrations/20260919000001_create_api_credential_vault.sql` | **NEW** — the metadata table, its indexes/constraints/RLS/grants, the generic `api_credential_pool` function, the `stt_credential_pool` compatibility alias, explicit ownership, and the rollback in the header |
| `backend/ai/credential_source.py` | **MODIFIED** — `VAULT_RPC` → `api_credential_pool`, new documented `LEGACY_VAULT_RPC` constant, and a docstring that no longer describes the boundary as STT-specific. No logic, bound, ordering or validation change |
| `DATABASE_ARCHITECTURE.md` | **MODIFIED** — new §29 (table, RPC, Vault mapping, security model, failure/orphan behaviour, ENV fallback, STT/TTS compatibility, complete manual SQL, complete rollback SQL), TOC entry, migration-status row 13, and corrections to the three stale "zero `.rpc()` calls" statements |
| `tests/test_credential_vault.py` | **NEW** — 69 tests pinning the migration, the documented SQL, the RPC contract, genericity, row validation, failure behaviour, bounds, the ENV fallback, owner scoping, M2.4 compatibility and secret non-leakage |
| `IMPLEMENTATION_REPORT.md` | **MODIFIED** — this section; the M3.0 section is demoted to "Previous phase" |

**Untouched (deliberately):** `media_service.py`, `stt_fallback.py`,
`stt_credential_pool.py`, `stt_engine_factory.py`, `stt_control_plane.py`,
`stt_provider_probe.py`, `stt_consensus.py`, `stt_chunking.py`, the provider
adapters, `tts_service.py`, `openai_tts_engine.py`, the tool layer, the Telegram
panels and handlers, `backend/db/client.py`, `requirements.txt`, `render.yaml`, and
`supabase/canonical_bootstrap.sql` (the canonical public-schema bootstrap is
intentionally left as the core-table contract; §29 records why the Vault RPC sits
outside it).

### Tests added and exact results

| Suite | Result |
|---|---|
| `tests/test_credential_vault.py` (new) | **`69 passed` in 0.37 s** |
| credential + STT suites (`test_stt_credential_pool`, `test_stt_fallback`, `test_ai_stt_settings`, `test_stt_provider_probe`, `test_stt_consensus`) | **`423 passed, 2 skipped` in 1.68 s** |
| media + TTS regression set (16 media suites + 2 TTS suites + the AI presentation suite) | **`1003 passed` in 40.89 s** |
| **Full suite** | **`4231 passed, 26 skipped, 3 warnings` in 114.64 s** |

Count provenance: the M3.0 section below records **4162 passed / 26 skipped** at
this phase's starting HEAD `88ccfa2`. This phase adds **69** tests and deletes,
weakens or skips **none** → **4231 / 26**. (The `26` skips are the pre-existing
opt-in live provider probes; the local interpreter is CPython 3.10.12, while
production pins 3.11.7 in `render.yaml`.)

Also run and clean: `python -m py_compile` on every changed Python file, and
`git diff --check`.

The new suite pins, from the source rather than from prose:

* **the migration** — the file exists under the project's `YYYYMMDDHHMMSS_…`
  naming convention; `api_credentials` declares exactly the nine documented
  columns and **none of them is a secret column**; the FK to `vault.secrets(id)`
  cascades; all five CHECK constraints exist; the id CHECK matches the
  application's own alphabet and length exactly; a Vault secret can map to only
  one credential; RLS is enabled with **no** policy; the table is revoked from
  `PUBLIC`/`anon`/`authenticated` and granted to `service_role`; the function is
  `SECURITY DEFINER` with `SET search_path = ''`, an explicit owner, a bounded
  `LIMIT 8`, a fully deterministic `ORDER BY`, an `enabled` filter and an optional
  owner filter; execution is revoked from `PUBLIC`/`anon`/`authenticated` and
  granted to `service_role` only; the read goes through `vault.decrypted_secrets`
  and never the encrypted table; there is no dynamic SQL; the migration seeds no
  credential and creates no secret; `NOTIFY pgrst` is present; the `stt_credential_pool`
  alias exists and resolves to the generic function;
* **the documentation** — §29 documents the table and both functions, labels the
  manual SQL and the rollback `NOT EXECUTED BY AI`, states explicitly that the
  table stores no secret, names **every** object the migration creates, covers the
  security model / ENV fallback / failure and orphan handling / consumers, and —
  the strongest check in the file — the documented SQL is **statement-identical**
  to the migration (comments and blank lines removed, both directions);
* **the application** — the boundary targets `api_credential_pool` while keeping
  `stt_credential_pool` as a documented alias that is **never** the runtime
  target; the RPC call still sends exactly `{"p_provider": …}` (owner filter
  omitted), so the M2.4 contract is intact; the module imports nothing from
  `backend.bot`, `backend.services.stt*` or Telethon and names no provider, so it
  is genuinely provider-agnostic; a non-STT provider (`openai`) resolves its own
  pool from the same table and RPC while leaving another provider's pool alone;
* **validation and failure** — a response that is not a list, a row that is not an
  object, `None`, `{}`, a missing/`badly named`/over-long id, an empty secret, a
  disabled row and a non-numeric priority each contribute exactly nothing (the
  bad priority falls back to the documented default `0` instead); a missing
  database, a refusing RPC and a malformed response each leave the ENV credential
  in place and still mark the provider loaded; a refusing backend never reduces the
  credentials already being served; no failure raises and no raw provider error
  text is propagated;
* **ordering and bounds** — the ENV credential stays first unless an explicit
  Vault priority outranks it; the pool is capped at
  `MAX_CREDENTIALS_PER_PROVIDER`; the environment is never scanned for an
  undeclared name; an empty backend keeps the ENV credential and an absent ENV
  credential leaves only the Vault pool;
* **secrets never leak** — no secret appears in the load log, in the pool
  description, in the migration, in `DATABASE_ARCHITECTURE.md` or in this report;
  the boundary's only database call is the RPC (no `.table()`/`.insert()`/
  `.upsert()`/`.update()`/`.delete()`), it never references the Vault schema, and
  no key-shaped value (`sk-…`, `AIza…`) is committed in any new artefact;
* **M2.4 compatibility** — the STT pool still loads through the generic boundary
  and reports its counts; a Vault-backed credential still rotates inside the
  provider (a credential-specific failure cools one down and the next Vault
  credential becomes the head of the rotation); a pool whose credentials are ALL
  cooling down is still attempted, so the provider-level fallback remains
  reachable; the credential-vs-provider classification vocabulary is unchanged.

### Exact manual Supabase actions still required

**None of these has been performed.** The complete, copy-pasteable SQL is in
`DATABASE_ARCHITECTURE.md` §29.10 (apply) and §29.11 (rollback).

1. **Apply the migration** in the Supabase SQL Editor as `postgres` (it must own
   the SECURITY DEFINER function so it can read `vault.decrypted_secrets`). If
   Vault is not enabled on the project, enable it from Database → Extensions, or
   let the `CREATE EXTENSION IF NOT EXISTS supabase_vault WITH SCHEMA vault;`
   statement create it.
2. **Optionally** confirm Vault is available (`select * from vault.secrets`). The
   application works without any Vault secret: every provider keeps its ENV
   credential.
3. **For each extra credential**, create the Vault secret and then insert ONE
   metadata row mapping it (`vault.create_secret(...)` + an `INSERT INTO
   public.api_credentials` that selects the secret's id by name — the exact
   snippet is in §29.10). The metadata row must use a non-secret
   `credential_id` of 1–64 characters from `[A-Za-z0-9._-]` and the provider token
   the registry uses (`gemini`, `groq`, `speechmatics`, `openai`). **Priority is
   lower-first**; ties fall back to `created_at` then `credential_id`.
4. **Verify** with the read-only `SELECT credential_id, provider, enabled,
   priority FROM public.api_credentials ORDER BY provider, priority, created_at`
   — the runtime will pick the pool up at the next startup or STT settings save.

The environment credential is **never** migrated automatically, and Render needs
no new variable.

### Known limitations

* **Nothing was applied to Supabase** and no Vault secret exists, so this phase's
  database objects are **untested against a real Postgres**: the SQL was validated
  syntactically by statement-level assertions in the test suite, not by execution.
  Applying it is the owner's action and is the real verification of that half.
* **Live Telegram and live provider verification: NOT PERFORMED** (no session, no
  credential in this environment). No provider, credential or recognition quality
  is claimed anywhere.
* The credential modules keep their existing `STT_CREDENTIAL_*` log tokens even
  though the boundary is now generic. Renaming them was deliberately avoided in an
  infrastructure-only phase because production log queries may key on them; it is
  a cosmetic follow-up.
* In-request runtime health (cooldown, failure counts) remains **process-local** by
  design (M2.4) and is therefore not in the database. `last_used_at` and a
  persisted cooldown were considered and left out: nothing in this phase writes
  them, and an unwritten column would be dead schema.
* `supabase/canonical_bootstrap.sql` is intentionally **not** extended with the
  Vault objects (nor with the earlier `20260827…`–`20260917…` migrations); it
  remains the core-table bootstrap, and §29 records the boundary explicitly.

### Deferred to PART 2 (not in this commit)

The owner-facing credential management surface: listing credentials, enable /
  disable, priority reordering, safe health display, and a `Test` action — built on
  this table and this RPC, adding **no** new secret architecture. Also still
  deferred from earlier phases: TTS provider fallback and a TTS credential pool,
  TTS voice/model/format selection, Native Vision, Video/GIF processing, provider
  benchmarking, and the outstanding live verifications.

### Exact next phase

**API Credential Vault PART 2 — the Telegram credential-management surface**, built
strictly on `api_credentials` + `api_credential_pool`, followed by the live
verification of M3.0 (one real synthesis) and of the Vault path (the first real
credential resolved from Vault).

---

## Previous phase — Media Processing M3.0: controlled Text-to-Speech foundation

Repository `Onlyicing1/Telegram-self-bot` · branch `main` · state as of 2026-09-18.

### Phase identity

| Item | Value |
|---|---|
| Phase name | **Media Processing M3.0** — ONE bounded, provider-aware Text-to-Speech capability: a controlled synthesis boundary, one provider adapter, one AI tool and one read-only surface |
| Starting HEAD | `dbc3f28` `feat(stt): keep a bounded credential pool per provider` (== `origin/main` at phase start) |
| Implementation commit | the single phase commit that contains this report (`git log -1 --format=%H` re-verifies it) |
| Provider selected | **OpenAI speech** — `POST {AI_OPENAI_BASE_URL}/audio/speech`, model **`gpt-4o-mini-tts`**, voice **`alloy`**, `response_format` **`opus`** (see “The provider decision” for why, and for the source of every name) |
| Database migration shipped by this phase | **NONE** — no table, no column, no migration, no SQL. `DATABASE_ARCHITECTURE.md` is untouched |
| New environment variables | **NONE** — the credential is the OpenAI variable this repository already declares, and the base URL is the one it already declares |
| New dependencies | **NONE** (`backend/requirements.txt` untouched; `httpx` was already the provider transport) |
| Files changed | **fourteen** — NEW `backend/services/tts_service.py`, NEW `backend/services/openai_tts_engine.py`, NEW `backend/ai/tools/speech.py`, NEW `backend/bot/handlers/ai_tts_settings.py`, NEW `tests/test_tts_service.py`, NEW `tests/test_tts_openai_engine.py`; MODIFIED `backend/telegram_api/media.py`, `backend/telegram_api/api.py`, `backend/ai/tools/registry.py`, `backend/bot/handlers/ai_stt_settings.py`, `backend/bot/router.py`, `tests/test_tool_health_audit.py`, `tests/test_memory_tools.py`, `tests/test_capability_exposure_tools.py` |
| Behavioural change | **additive only**: one new capability (one new tool, one new read-only panel) and one new bounded Telegram transfer helper. No existing command, panel, tool, provider, engine or STT/media behavior was changed |
| Persisted state | **NONE** — this phase writes no setting, because it has no owner-facing setting to write; the capability is deployment configuration |
| Live Telegram verification | **NOT PERFORMED** — no Telegram session exists in this environment |
| Live provider verification | **NOT PERFORMED** — no provider credential exists in this environment, so no synthesis has ever been sent to OpenAI |
| Speech-quality / Persian claim | **NONE** — no synthesis has been heard, and no quality, pronunciation or language claim is made |

### Purpose of this phase

The media stack could READ: it transcribes (STT), recognises text in images (OCR),
and extracts documents. It could not SPEAK. This phase adds the missing direction
as a capability of its own, with the same discipline the reading capabilities were
built with:

```
AI request (ONE structured action)
        ↓
deterministic capability decision       tts_service (fail-closed)
        ↓
the synthesis boundary                  tts_service      ← NEW
        ↓
the provider adapter                    openai_tts_engine ← NEW
        ↓
normalized clip (bounded bytes)
        ↓
the EXISTING Telegram transfer           telegram_api/media.send_voice
the EXISTING AI tool path                ToolExecutor → text_to_speech tool
```

Speech synthesis is deliberately NOT an STT component. It shares no type, no
seam, no failure token and no provider adapter with recognition; the new modules
import none of `stt_fallback`, `stt_credential_pool`, `stt_control_plane`,
`stt_engine_factory`, `stt_chunking`, `stt_consensus`, `stt_provider_probe`,
`gemini_media_engine`, `groq_stt_engine` or `speechmatics_stt_engine` — proven by
test, and by the fact that a synthesis never touches the process's provisioned STT
engine. `media_service` is untouched, `MediaAnalysis` never represents synthesized
audio, and the M2.3/M2.4 fallback and credential layers are unchanged.

### The provider decision

**OpenAI was chosen because the repository already declares everything it needs,
and nothing new is introduced.**

* The credential variables `AI_OPENAI_API_KEY` / `OPENAI_API_KEY` are the ones
  `backend/ai/providers/factory.py` already resolves for OpenAI, in that same
  precedence order. An installation that already enabled OpenAI for chat can
  speak with **no additional key**.
* The base URL variable `AI_OPENAI_BASE_URL` is that same factory's own.
* The API contract is a single documented, non-preview request that returns the
audio itself as the response body, with a documented `opus` output format —
  which is the low-latency format Telegram's voice-note representation uses, so
  the response is delivered as-is with **no local transcoding and no audio
  dependency**.
* Nothing else in the repository could support synthesis safely: Groq and
  Speechmatics expose transcription only, and the other registered providers are
  OpenAI-compatible CHAT gateways whose synthesis contract is not established by
  anything in this repository. The Gemini media route is a transcription/vision
  engine, and using it here would have meant altering an STT component.

Every name the adapter sends was taken from the provider's own published contract,
not inferred: the endpoint path, the model id, the voice list, the output formats
and the supported-language list. The model and the voice are CLOSED sets in the
adapter, so neither can be typed or computed into a request.

### The capability hierarchy — where each concern lives

| Concern | Owner |
|---|---|
| the registered capability (provider, model, voice, format) | `backend/services/openai_tts_engine.py` (**NEW**) |
| validation, bounds, the capability decision, the timeout, output validation, the normalized clip, the failure taxonomy | `backend/services/tts_service.py` (**NEW**) |
| the provider HTTP request | `backend/services/openai_tts_engine.py` — ONE `httpx.AsyncClient` POST, nothing else |
| the AI-facing request surface | `backend/ai/tools/speech.py` (**NEW**) — registered in the EXISTING registry |
| the Telegram transfer | `backend/telegram_api/media.py` (**extended**, one bounded helper) + the existing facade |
| the owner-facing surface | `backend/bot/handlers/ai_tts_settings.py` (**NEW**), under the EXISTING **AI → Media Analysis** hub |
| tool execution, history, permissions, long-running exemption | the EXISTING `ToolExecutor` / `ToolRegistry` / `Dispatcher` — **unchanged** |
| the provider mesh, STT, OCR, fallback, credential pool, media boundary | **untouched** |

No second executor, no second registry, no second Telegram abstraction, no second
panel framework, no second configuration store.

### The boundary — `backend/services/tts_service.py`

The ONE thing a caller may use is
`await synthesize(text, *, request_id, timeout_s) -> SpeechClip`. It performs, in
order: validate → resolve the registered provider and its credential → ONE
provider call under ONE awaited timeout → validate the audio → return the clip.
There is no retry loop, no queue, no worker, no persisted state.

`SpeechClip` is deliberately minimal — `audio`, `mime_type`, `file_name`,
`characters`, `provider`, `model`, `voice`, `duration_s` — and carries **no chat
id, message id, sender, username, caption, reply text, conversation history or
arbitrary Telegram metadata**. It is bounded resident bytes with no path and no
file handle, is never persisted, and is the only value the caller receives.

`normalize_request_text` removes leading/trailing whitespace only. Interior
whitespace is content and is never collapsed, and the text is never translated,
summarized, truncated or otherwise rewritten: the owner gets exactly the words
that were requested, or an honest refusal.

### The deterministic capability decision

The decision is deterministic and fail-closed, and lives at ONE seam:
`capability_reason()` builds a probe engine through the same adapter a request
would use and returns `""` when synthesis can run, or ONE bounded failure token
(`missing_credential`, `unsupported_model`, `unsupported_voice`,
`provider_unavailable`). Because the panel and the request path consult the same
function, the state the owner is shown and the outcome a request gets **cannot
disagree**.

The synthesis INTENT is the model's structured action — a single registered tool
call with a bounded `text` argument. This phase deliberately adds **no
natural-language “read this aloud” parser**: such a parser would need a new
phrase inventory and language claims this phase cannot support, whereas a tool
call is already the repository's deterministic, schema-bounded request contract,
and it is the mechanism the phase instruction prescribes (“the AI may request a
structured TTS action; the application executes that action”). Consequently the AI
never gains Telegram, filesystem or audio authority: it can only name words, and
the runtime decides whether, where and how they are spoken.

### The adapter — `backend/services/openai_tts_engine.py`

One request, and its whole contract is asserted by test:

```
POST {AI_OPENAI_BASE_URL}/audio/speech      default base https://api.openai.com/v1
Authorization: Bearer <credential>          Content-Type: application/json
{ "model": "gpt-4o-mini-tts", "input": "<the text>", "voice": "alloy",
  "response_format": "opus" }
→ 200, the audio itself as the response body
```

The adapter is async (`httpx.AsyncClient`), so no blocking call can enter the event
loop and no worker thread is involved. It holds no request state, caches nothing
and persists nothing. `build_engine` is a pure function of
`(model, voice, api_key, base_url)`: an unregistered model or voice is refused
here too, and a missing credential yields `(None, "missing_credential")` instead
of an exception, so provisioning stays optional and the boundary reports that
state honestly rather than substituting a provider the owner did not select.

### Bounds, timeout and temporary resources

| Bound | Value | Why |
|---|---|---|
| `MAX_TTS_INPUT_CHARS` | **1000** characters | Finite and far below the speech model's 2000-token request bound in every supported script, so a request refused here was never at risk of being silently cut |
| `TTS_TIMEOUT_S` | **60 s** | ONE wall-clock bound around the provider call, measured by the boundary; the adapter derives every `httpx` phase bound from the remaining budget and never invents a second deadline |
| `MAX_REQUEST_TIMEOUT_S` | **120 s** | The adapter's own ceiling, which clamps any caller-supplied budget so a future caller can never turn it into an unbounded request; the boundary's 60 s is what actually applies |
| `MIN_REQUEST_TIMEOUT_S` | **8 s** | A request is not started with less budget than this left — it fails as `deadline` instead of being started only to time out |
| `MAX_TTS_AUDIO_BYTES` | **5 MiB** | The output ceiling, roughly three minutes of the requested format, so a bounded input cannot legitimately reach it and an over-sized body is a provider anomaly |
| `MEDIA_UPLOAD_TIMEOUT_S` | **120 s** | The finite ceiling for the ONE Telegram transfer |

**Temporary resources: there are none.** The clip is bounded resident bytes and the
path creates no temporary file — no `tempfile`, no `mkstemp`, no `shutil` anywhere
in the boundary (asserted from source) — so cleanup is unconditional by
construction on success, provider failure, validation failure, timeout,
cancellation and unexpected exception. The suite proves that a synthesis and a
failed synthesis both leave the system temp directory byte-identical.

An over-long request is **REFUSED, never truncated**: silently speaking a prefix
would deliver something the owner did not ask for.

### Telegram delivery — ONE voice message

`backend/telegram_api/media.py` gains ONE bounded helper, `send_voice`, in the
same module that already owns the bounded DOWNLOAD — the same
`guarded_await` bound discipline, the same exception mapping, the same
`serialize_message` result shape, and a module-level `VOICE_NOTE_MIME` so a caller
cannot name a different container while asking for a voice message. The facade
exposes it as `TelegramAPI.send_voice`.

The tool sends **exactly one** voice note through that helper and returns ONE
`ToolResult`. There is no multi-message burst, no intermediate provider or debug
message, and the destination is resolved from TRUSTED runtime context
(`extra["chat_id"]`, falling back to the owner's own chat) — never from model
output, exactly like `SendMessageTool` and `RetrieveSaveTool`. The result carries
the bounded synthesis facts (`characters`, `mime_type`, `voice`, `model`) and
deliberately **not** the destination chat, so no Telegram identifier travels back
into the model's conversation either.

### Zero-context guarantee

The provider receives the text being synthesized and the minimum synthesis
configuration, and nothing else:

* the request body has exactly four fields — asserted by test;
* the service's signature has no parameter that could carry Telegram context, and
the tool calls it with `text` plus `(request_id, timeout_s)` only — asserted by a
  spy on that exact call;
* the adapter's `speak(text, *, timeout_s)` has no chat/message parameter, and the
  serialized request is asserted to contain none of `chat`, `message_id`,
  `caption`, `sender`, `username`, `reply`, `owner`, `history`;
* the synthesis log line carries the input LENGTH, never the input text.

### The failure taxonomy

Nineteen closed, deterministic classes — `missing_credential`, `auth`,
`forbidden`, `rate_limit`, `quota_exceeded`, `invalid_request`,
`unsupported_model`, `unsupported_voice`, `empty_input`, `input_too_large`,
`timeout`, `transport`, `server`, `malformed_response`, `empty_audio`,
`output_too_large`, `provider_rejection`, `deadline`, `provider_unavailable` —
each with the leg that raised it (`TTS_STAGE_*`), the provider's HTTP status when
it answered, and an honest `retryable` verdict (transient classes only).

`TtsError` is the ONLY handled failure type: an already-classified provider failure
propagates **unchanged**, `asyncio.CancelledError` is re-raised, and a programming
error is never dressed up as a provider failure. Provider responses are classified
from the status plus the provider's OWN bounded `code`/`type` token (so a refused
voice, a refused model and a refused input are told apart deterministically);
free-form prose is never parsed, no non-2xx is ever re-sent, and the credential is
redacted from every message the adapter produces. `retryable` is metadata only:
**this phase performs no retry and no provider fallback.**

### Configuration and credential model

The credential is deployment configuration and is read through the provider's own
declared variable names — never an environment sweep, never a database column,
never Telegram. There is **no TTS credential pool** in this phase: the instruction
was explicit that the STT pool must not be copied, and the audit found no second
provider to warrant a provider-agnostic refactor, so the M2.4 STT pool is
untouched.

**No owner-facing TTS setting exists, and therefore no schema change was needed.**
The `ai_config` table has a FIXED column set and the writer builds an explicit
column payload, so persisting a new TTS key would have required a new column —
which this phase's scope forbids unless it is unavoidable. It is not unavoidable:
the first phase has one registered capability and no behavior-changing setting, so
the surface is read-only and nothing is persisted. That also keeps the panel
honest — it offers no control that would not work.

### The Telegram surface — read-only, under Media Analysis

```
AI
└── Media Analysis            (ai_media, existing hub — one new row)
    ├── Text recognition       (existing)
    ├── Speech-to-Text         (existing)
    └── Text-to-Speech         (ai_media_tts — NEW, read-only)
```

The screen reports only what is true: the registered provider, model, voice and
output format; the input limit and the synthesis timeout; and whether a credential
is present. It never claims provider HEALTH (a credential existing is not evidence
that a provider answers — only a real request can say that), never prints or hints
at a credential value, and never names an environment variable. When the
capability cannot run it says so plainly and states that nothing is sent. It
registers through the ONE shared panel/navigation registry and registers **no
action and no input** in this phase.

### Files changed by this phase

| File | Change |
|---|---|
| `backend/services/tts_service.py` | **NEW** — the boundary: bounds, closed taxonomy, `TtsError`, `SpeechClip`, the capability decision, the awaited provider call, output validation |
| `backend/services/openai_tts_engine.py` | **NEW** — the adapter: closed model/voice sets, the ONE POST, status classification, secret redaction, bounded phase timeouts |
| `backend/ai/tools/speech.py` | **NEW** — `text_to_speech`: one bounded `text` argument, trusted destination, ONE voice note, a result with no Telegram identifier |
| `backend/bot/handlers/ai_tts_settings.py` | **NEW** — the read-only Text-to-Speech panel + its Media Analysis hub line |
| `backend/ai/tools/registry.py` | MODIFIED — `SpeakTool` registered in the ONE registry (2 lines) |
| `backend/telegram_api/media.py` | MODIFIED — the bounded `send_voice` helper + `VOICE_NOTE_MIME` + the upload ceiling (download path untouched) |
| `backend/telegram_api/api.py` | MODIFIED — the `send_voice` facade method |
| `backend/bot/handlers/ai_stt_settings.py` | MODIFIED — the hub row, the hub status line, and the module's surface tree |
| `backend/bot/router.py` | MODIFIED — register the new handler module (2 lines) |
| `tests/test_tts_service.py` | **NEW** — 56 tests (boundary, tool, panel, isolation) |
| `tests/test_tts_openai_engine.py` | **NEW** — 42 tests (request contract, capability set, credentials, taxonomy, bounds, secret hygiene) |
| `tests/test_tool_health_audit.py` | MODIFIED — the tool inventory gains `text_to_speech: READ_WRITE`; expected count 43 → 44 |
| `tests/test_memory_tools.py` | MODIFIED — registry-count assertion 43 → 44 |
| `tests/test_capability_exposure_tools.py` | MODIFIED — duplicate-registration count 43 → 44 |

### Tests added and exact results

| Suite | Result |
|---|---|
| `tests/test_tts_openai_engine.py` (new) | **`42 passed` in 0.22 s** |
| `tests/test_tts_service.py` (new) | **`56 passed` in 0.26 s** |
| the STT / media regression set (`test_ai_stt_settings.py`, `test_stt_fallback.py`, `test_stt_credential_pool.py`, `test_stt_provider_probe.py`, `test_stt_consensus.py`, `test_media_stt.py`, `test_media_stt_chunking.py`, `test_media_stt_language.py`, `test_media_stt_multipass.py`, `test_media_stt_reliability.py`, `test_media_stt_benchmark.py`, `test_media_direct_stt.py`, `test_media_dedicated_stt.py`, `test_groq_stt_engine.py`, `test_speechmatics_stt_engine.py`) | **`894 passed, 2 skipped` in 9.29 s** |
| **Full suite** | **`4162 passed, 26 skipped, 3 warnings` in 114.75 s** |

Count provenance, so the arithmetic is auditable: this phase's starting HEAD
`dbc3f28` was recorded by the M2.4 section below as **4064 passed / 26 skipped**.
This phase adds **98** tests and deletes, weakens or skips **none** →
**4162 / 26**. (The `26` skips are the pre-existing opt-in live probes; the local
interpreter is CPython 3.10.12 while production is the `render.yaml` pin of
3.11.7.) The three MODIFIED test files above are inventory assertions that must
name every registered tool; they were updated to include the new tool and nothing
else about them changed.

The new suites pin, from the source rather than from prose:

* **the request** — exactly ONE POST to the documented path, with the bearer
  credential and a JSON body of exactly four fields, sent verbatim; the configured
  base URL is honored and the public base is the default; the body can never carry
  a chat/message/caption/sender/reply field; the service and engine signatures
  have no parameter that could carry Telegram context; the tool calls the boundary
  with the text plus `(request_id, timeout_s)` and nothing else;
* **the closed capability set** — only the registered model and a documented voice
  can be built; a typed model or voice is refused; `SUPPORTED_MODELS` and the
  voice set are the declared ones;
* **credentials** — the repository's own OpenAI variables in their existing
  precedence order; a decoy variable is never read; a missing credential is a
  bounded reason and not an exception; the only traced credential identity is a
  variable NAME;
* **validation and bounds** — only surrounding whitespace is removed and interior
  whitespace is preserved; empty/whitespace/non-string input is refused; an
  over-long request is REFUSED (never truncated) and never reaches the provider;
  the limit boundary is inclusive; every bound is the documented finite value; the
  caller's budget is clamped to the ceiling and every `httpx` phase bound is
  derived from what is left;
* **the failure taxonomy** — every status (401/403/404/429/5xx/other) is
  classified with its HTTP status; only transient families are retryable; a 400 is
  narrowed by the provider's own token (voice / model / input / generic); a spent
  quota is its own class; a timeout, a transport failure, an empty body, a JSON
  body on the success path, an over-sized body and a spent deadline are each their
  own class; the taxonomy is closed and every token the adapter can raise belongs
  to it; a programming error is never converted and a `CancelledError` is
  re-raised;
* **the tool** — one bounded `text` argument and no other accepted shape; the
  destination comes from trusted context, not arguments; a missing text, a missing
  transport and an untrusted destination all send nothing; every failure surfaces
  as a failed result with its bounded class and no send; the result data and
  message carry no chat identifier; the metadata (`READ_WRITE`, `safe`,
  `long_running`, `required_arguments`, the bounded parameter schema) is the
  documented one; the tool is present in the ONE registry and visible to the
  provider schema list; the synthesis budget is the request's own envelope capped
  by the boundary ceiling;
* **the surface** — it registers under `ai_media`, reports the registered
  capability, says “No credential on this runtime” when nothing can run, offers no
action and no input (`No owner controls`), and its hub line appears in the Media
  Analysis hub beside the existing rows;
* **temporary resources** — the boundary's source creates no temporary file, and
  both a successful and a failed synthesis leave the temp directory unchanged;
* **async / event-loop safety** — the boundary and the adapter methods are
  coroutines, the service contains no `httpx` and no `to_thread`, and the adapter
  uses `AsyncClient` and no blocking client;
* **secret hygiene** — the credential never appears in a failure message (and is
  redacted when the provider echoes it) and never in a log line, while the bounded
  facts (provider, model, voice, input LENGTH, bytes, elapsed, class, status) do;
* **capability isolation** — neither new module references any STT/media-engine
  module, neither exposes `transcribe`, and a synthesis and a tool call never touch
  the process's provisioned STT engine.

**Syntax / whitespace:** `python -m py_compile` clean on all fourteen changed Python
files; `git diff --check` clean.

### Database impact

**NONE.** No table, no column, no view, no function, no migration, no SQL, and
`DATABASE_ARCHITECTURE.md` is untouched. Nothing on the TTS path reads the
database either: the capability is deployment configuration, so a synthesis makes
no database call at all. There is consequently **no manual Supabase step required
by this phase** — unlike M2.4, whose optional Vault RPC remains outstanding.

### Environment impact

**NONE.** No variable was added, renamed or removed. Speech synthesis uses
`AI_OPENAI_API_KEY` / `OPENAI_API_KEY` (the variables this repository already
declares for OpenAI, first match wins) and `AI_OPENAI_BASE_URL` (that same
declaration), defaulting to `https://api.openai.com/v1`. Consequently:

* an installation that already has an OpenAI key gets the capability with **no
  configuration change**;
* an installation without one sees the capability reported as
  “No credential on this runtime” and nothing is ever sent;
* `render.yaml` is untouched, and no numbered or scanned variable exists.

### Live verification status

* **Live Telegram: NOT PERFORMED.** No Telegram session or traffic exists in this
  environment, so no voice message has been delivered, played or inspected. The
  delivery contract is proven by test against a recording facade and the real
  bounded transfer helper's shape — not by a live walkthrough.
* **Live provider: NOT PERFORMED.** No OpenAI credential exists here, so **no
  synthesis has ever been sent to the real endpoint**. Every provider interaction
  in this phase's evidence is a scripted `httpx` transport or a fake engine. No
  provider is claimed healthy, reachable or correctly billed.
* **Speech quality: unmeasured and unclaimed.** Nothing in this phase has heard a
  generated voice. In particular, although the provider's published contract lists
  Persian among its supported input languages, this phase makes **no claim about
  Persian pronunciation, accent or intelligibility**, and it performed no
  translation and no language detection.
* **Voice-note rendering: unverified live.** The requested `opus` format is the
  format Telegram's voice-note representation uses, so the response is delivered
  as-is with no transcoding; that the delivered message renders as a voice note
  with a server-computed duration has not been observed live (see limitation 3).

### Known limitations

1. **One provider, one model, one voice, no fallback and no retry.** The phase
   instruction deliberately excluded provider management: the adapter marks the
   transient classes retryable, but nothing consumes that verdict yet, so a
   provider outage fails with its own bounded class rather than trying another
   provider. Voice selection is likewise deferred: the voice is fixed so the same
   text produces the same deterministic result.
2. **No owner-facing setting and no persistence.** The surface is read-only by
   design. Making the model, voice or format configurable needs storage, and the
   `ai_config` writer is column-explicit, so that change would require the pending
   migration discussed below — it was NOT smuggled into this phase.
3. **Duration is reported as 0.** The speech response carries no duration metadata
   and the container is deliberately not parsed, so the value is left explicitly
   unknown rather than guessed. Telegram renders the voice note regardless; a
   client that shows a duration derives it itself.
4. **The response container is not re-validated locally.** Only non-emptiness and
   the byte ceiling are checked, so a future format change cannot be refused by a
   stale local magic-bytes guess; Telegram remains the validator of what it
   receives.
5. **A synthesis is the request's whole budget, not a pipelined stream.** One
   request produces one clip; there is no streaming, no partial delivery and no
   chunking.
6. **The tool result is a separate message from the voice note.** The AI's one
   confirmation line follows the existing tool-result convention (the same shape
   `send_message` and `retrieve_save` already use) rather than being merged into
   the voice message.
7. **The capability is deployment-global, not per-owner.** This is a single-owner
   self-bot, so the credential and the registered capability are runtime
   configuration; there is no per-owner TTS profile.

### Deferred work

* **TTS provider fallback and a TTS credential pool** — the analogue of M2.3/M2.4
  for synthesis. The taxonomy already carries an honest `retryable` verdict and
  the adapter is one small seam, but nothing was built ahead of a second provider.
* **Voice / model / format selection with persistence** — needs the `ai_config`
  column decision above, plus a registered candidate registry if the store's
  convention (finite registered candidates, never typed ids) is to hold.
* **Speech-quality benchmarking** — including whether Persian input is delivered
  intelligibly, which only a live listening comparison can establish.
* **Streaming/interruptible synthesis and length/style controls** — not attempted.
* Still open from earlier phases and unchanged by this one: **Native Vision**,
  **Video/GIF processing**, real Persian recognition benchmarking, evidence-based
  STT provider ranking, a persisted STT credential roster, the manual Supabase
  Vault configuration, and the pending `ai_config` migration with the
  documentation-only `DATABASE_ARCHITECTURE.md` §7 refresh.

### Exact next phase

1. **Live verification of M3.0** — send one real synthesis request and confirm:
   exactly ONE voice note arrives in the owner's chat and plays; the log carries
   `TTS_STAGE … stage=tts_completed` with `provider=openai`, `model`, `voice`,
   `chars`, `bytes` and **no text, no credential and no identifier**; and that
   `AI → Media Analysis → Text-to-Speech` reports `Ready`. This is the live test no
   environment here can perform.
2. **Confirm the negative path live** — remove the OpenAI credential and confirm
   the refusal is the bounded `missing_credential` class, that the panel says
   “No credential on this runtime”, and that nothing is sent.
3. **Then, and only then, decide the next capability** in the order the earlier
   phases recorded: real Persian STT benchmarking, the M2.4 Vault configuration,
   or a TTS provider fallback — not a second TTS provider added speculatively.

---


## Previous phase — Media Processing M2.4: STT credential pool and API-key rotation

Repository `Onlyicing1/Telegram-self-bot` · branch `main` · state as of 2026-09-18.

> **Additive predecessor of M3.0 (above).** Nothing in this section is
> superseded: the STT provider layer, the credential pool, the rotation semantics,
> the cooldowns, the bounds and the security guarantees are all exactly as
> recorded here, and M3.0 touched none of them — it added a separate synthesis
> capability that shares no seam with recognition and did not create a TTS copy of
> this pool. The ONE statement this section makes that M3.0 changes is the
> deferral of TTS itself, annotated in “Deferred work” below.

### Phase identity

| Item | Value |
|---|---|
| Phase name | **Media Processing M2.4** — a bounded credential pool per STT provider, with API-key rotation INSIDE a provider and the provider fallback of M2.3 preserved above it |
| Starting HEAD | `9057f05` `feat(stt): keep provider health and fall back within the ordered STT candidates` (== `origin/main` at phase start) |
| Implementation commit | the single phase commit that contains this report (`git log -1 --format=%H` re-verifies it) |
| Database migration shipped by this phase | **NONE** — no SQL was executed, no table, no column, no view, no function, no Vault secret was created by this phase |
| Database migration required from the USER | **the OPTIONAL Supabase-side contract in “Supabase Vault configuration that MUST be performed manually” below** — the runtime works without it, exactly as it does today |
| New environment variables | **NONE** — no `*_KEY_1`/`*_KEY_2` variable exists, by design |
| New dependencies | **NONE** (`requirements.txt` untouched) |
| Files changed | **nine** — NEW `backend/ai/credential_source.py`, NEW `backend/services/stt_credential_pool.py`, NEW `tests/test_stt_credential_pool.py`; MODIFIED `backend/services/stt_fallback.py`, `backend/services/stt_engine_factory.py`, `backend/services/gemini_media_engine.py`, `backend/runtime/supervisor.py`, `backend/bot/handlers/ai_stt_settings.py`, `tests/conftest.py` |
| Behavioural change | **exactly one**: a provider configured with MORE THAN ONE credential now rotates inside the provider before the provider-level fallback is engaged, and a credential that is rejected no longer leaves the provider unusable. A provider with ONE credential (every deployment today) behaves **byte for byte as it did before this phase** |
| Persisted state | **NONE** — the credential snapshot and the credential health/cooldown are process-local runtime posture; no secret is ever written anywhere by this application |
| Live Telegram verification | **NOT PERFORMED** |
| Live provider verification | **NOT PERFORMED** — no provider credential exists in this implementation environment |
| Supabase Vault verification | **NOT PERFORMED** — **no Vault pool has been configured**, so the Vault path has been exercised only against a fake secret backend inside the test suite |
| Recognition-quality claim | **NONE** — this phase changes WHICH credential may answer, never what any provider transcribes |

### Purpose of this phase

The provider layer now survives one provider failing; it could not survive one
KEY failing, because a provider had exactly one credential. This phase adds the
second axis of resilience **without touching the first one**:

```
Speech-to-Text request
    ↓  the selected provider is still attempt 1 (M2.3, unchanged)
selected provider
    ↓
credential pool of THAT provider           ← NEW
    ├── credential A  (priority, then source order)
    ├── credential B
    └── credential C
         ↓
    credential-specific failure (rejected / revoked / spent / rate-limited key)
         ↓  the NEXT credential of the SAME provider is tried
    success ─────────────────────────────────────────────→ transcript
         └── all usable credentials exhausted
                  ↓  the EXISTING provider fallback (M2.3, unchanged)
             next provider ──→ ITS OWN credential pool ──→ …
```

| Before M2.4 | After M2.4 |
|---|---|
| a provider had exactly one key | a provider may have a bounded pool of keys |
| a rejected/revoked/spent key failed the whole provider | the provider keeps serving through its other credentials, and the provider is NOT marked unhealthy |
| a 429 on one key looked like a provider-wide condition | a per-key quota rotates the credential; a 5xx does not |
| keys had to be configured as numbered ENV variables to have more than one | the environment keeps ONE credential per provider; additional credentials live in the secret backend |
| a revoked key required a redeploy to replace | a credential is refreshed at the next STT settings apply (startup or panel save), with no redeploy and no restart |

Explicitly NOT implemented, per the phase instruction: TTS, Native Vision,
Video/GIF processing, any new STT provider, provider benchmarking, provider
quality ranking, automatic account creation, fake accounts, automatic API-key
purchasing, scraping provider dashboards, arbitrary ENV scanning, raw API keys in
an ordinary database table, SQL migrations, Supabase schema changes, direct
Supabase-side administration, Telegram display of raw credentials, any settings
redesign, and any change to `ProviderManager`, the dispatcher, the tool layer,
the `RuntimeSupervisor` recovery architecture, `ToolRegistry`/`ToolExecutor`, the
provider adapters, the Telegram media download boundary, the Gemini STT
instructions, the OCR/PDF/DOCX paths, `stt_chunking.py`, `stt_consensus.py`,
`stt_provider_probe.py`, `stt_control_plane.py`, `requirements.txt`,
`render.yaml`, `DATABASE_ARCHITECTURE.md` or `supabase/migrations/*.sql`.

### The credential hierarchy — where each concern lives

| Concern | Where it lives |
|---|---|
| which PROVIDERS exist, their canonical order, the owner's selection, language, passes | **control plane** (`backend/ai/stt_control_plane.py`) — unchanged |
| which PROVIDER is tried, provider health, provider cooldown, the bounded provider loop | **provider layer** (`backend/services/stt_fallback.py`, M2.3) — extended, not replaced |
| which CREDENTIALS a provider has and in what order | **secret boundary** (`backend/ai/credential_source.py`) — NEW |
| credential health, credential cooldown, credential-vs-provider classification | **credential pool** (`backend/services/stt_credential_pool.py`) — NEW |
| candidate × credential → engine construction (ONE seam) | **engine factory** (`backend/services/stt_engine_factory.py`) — extended with a per-credential entry point |
| the HTTP request itself | the provider adapters — **untouched**; an adapter receives only the credential of the current attempt |
| resolution, ONE download, validation, timeouts, cleanup, normalization, chunking | **media boundary** (`backend/services/media_service.py`) — **untouched by this phase** |

### The secret abstraction — `backend/ai/credential_source.py`

One module answers “which credentials may this provider use?” and nothing else.
It never talks to a provider, never decides which provider to try and never
decides whether a credential is healthy.

* **Two sources, ONE precedence rule.** The deployment's environment credential
  is resolved first (through the provider's OWN declared variable names — the
  same constants the adapters already read: `AI_GEMINI_API_KEY` →
  `GEMINI_API_KEY`, `AI_GROQ_API_KEY` → `GROQ_API_KEY`,
  `AI_SPEECHMATICS_API_KEY`), then the secret backend's credentials follow.
  A missing variable contributes nothing; the first one that carries a value
  wins and the remaining names are not inspected.
* **No numbered ENV lists, no scanning.** `PROVIDER_KEY_1` / `_2` / `_3` are
  explicitly NOT supported, and nothing here enumerates the environment: the
  caller passes the provider's declared names and the module reads exactly
  those. A test pins this (“the environment is never scanned for an undeclared
  name”).
* **Deterministic order**: `(priority, order_index)`, where the environment
  credential is `priority=0, order_index=0` and the backend's credentials follow
  in the order the backend returned them. Explicit priority therefore outranks
  the source order, and a tie puts the environment credential first — so an
  installation that configures nothing keeps the exact key it has today.
* **Bounded**: `MAX_CREDENTIALS_PER_PROVIDER = 4` credentials survive per
  provider (the bound is applied AFTER ordering, so it is the owner's explicit
  priorities that decide, never the order two sources happened to be merged in),
  `MAX_CACHED_PROVIDERS = 16` snapshots are kept, and a backend read is bounded
  by `VAULT_TIMEOUT_S = 5 s`.
* **Fail-closed on an optional source.** A missing function, a permission error,
  a timeout, an unexpected response shape, a row without an id, an id outside the
  safe alphabet, a disabled row and an empty secret each contribute NOTHING and
  are reported as a bounded reason. This boundary can never fail a media request
  and can never invent a credential.
* **Cache semantics.** A snapshot is loaded by `load()` — called only from the
  settings-apply path (startup and after a panel save) — and is then served until
  the next load. `mark_stale(provider)` records that a credential of that provider
  failed, which the next load reports and refreshes; the snapshot itself KEEPS
  being served, deliberately, because dropping it mid-incident would leave the
  runtime with fewer usable credentials than it started with. Nothing is written
  to disk, nothing is logged, and a credential id is the only credential fact that
  ever leaves this module.

### The credential pool — `backend/services/stt_credential_pool.py`

* **Ordering is deterministic** — `(priority, then source order)` — and there is
  no random rotation, no per-request reshuffle and no quality ranking.
* **Rotation order** (`rotation_for`) skips a credential that is serving its own
  cooldown, so a spent key does not cost every later request an attempt. The one
  exception is deliberate and mirrors the provider layer's own pinned rule: when
  EVERY credential of the provider is cooling down the pool is returned
  unchanged, because refusing to attempt the provider at all would turn a
  temporary credential condition into a guaranteed media failure.
* **Classification** decides whether a failure is about the credential (rotate)
  or about the provider (do not burn the pool), reusing the adapters' existing
  bounded vocabulary and their already-attached `http_status`:

| Verdict | Tokens | Rotate the credential? |
|---|---|---|
| **credential-specific** | `auth`, `forbidden`, `missing_credential`, `rate_limit`, `quota_exceeded`, and ANY failure carrying HTTP `401` / `403` / `429` | **yes** — bounded by the two ceilings below, and the provider is NOT marked unhealthy |
| **provider-wide** | `server`, `timeout`, `transport`, `transport_failure`, `upload_failed`, `file_processing`, `malformed_response`, `empty_transcription`, `provider_rejection`, `unsupported_audio`, `unsupported_model`, `operation_deadline`, HTTP 4xx/5xx other than the three above, anything unrecognized | **no** — the remaining credentials would fail the same way; the failure goes straight to the existing provider health/fallback layer |
| **not classified at all** | a bare `MediaError`, a programming error (anything that is not the boundary's `MediaError`) | **no** — it propagates unchanged, and it is never hidden behind a rotation |

  The HTTP status matters because the Gemini adapter reports a rejection as
  `http_rejection` with its status attached; the status is the honest classifier
  there. A 503 is explicitly NOT a credential problem.
* **Credential metadata** (all of it non-secret): the stable credential id, the
  provider, the enabled/disabled state, the optional priority and source order,
  the failure count, the last failure class, the temporary cooldown and the last
  successful use. Quota/exhaustion state is expressed as the cooldown a
  credential-specific rate-limit failure produces. The secret lives ONLY in the
  `CredentialRecord` that is handed to the engine factory for one attempt.
* **Cooldown** is bounded doubling per consecutive failure:
  `60 s → 120 s → 240 s → 480 s → 600 s` (capped), and a success resets it
  immediately. It is process-local, never persisted, and separate from the
  provider cooldown.

### Integration with the provider layer (M2.3 preserved, extended)

`stt_fallback.AttemptPlan.run` still drives ONE attempt through the boundary's
unchanged `_stt_attempt` primitive. The change is that the inner sequence is now
(candidate × credential):

* The provider loop keeps its exact semantics: the SELECTED candidate is attempt 1
  of every request whatever its health, `MAX_PROVIDER_ATTEMPTS = 3`, a cooldown
  prunes only the fallback rotation, a provider that failed this request is not
  retried within it, a success restores provider health immediately, and a
  request with no runnable substitute still propagates the selected provider's
  own failure object verbatim.
* **Provider preference is never rewritten**: a rotation is internal to one
  request, is never written to `ai_config`, is never shown in the Telegram UI, and
  the persisted selection keeps resolving to the same candidate.
* **Only a REAL pool may rotate.** “A pool exists” is asked of the provider's
  CONFIGURATION (`len(credentials_for(provider)) > 1`), never of what happens to
  be cooling down. A provider with one credential therefore keeps its exact
  pre-M2.4 behaviour: a rejected key still propagates unchanged and never becomes
  a provider sweep.
* **A credential failure never marks the provider unhealthy** while another
  credential remains — that is the point of the separate axis.
* **Pool exhaustion hands the failure to the provider layer**, which is the
  documented transition: `STT_CREDENTIAL_POOL_EXHAUSTED` →
  `STT_FALLBACK_POOL_TO_PROVIDER` → the provider cooldown is recorded and the next
  provider is tried (through ITS own pool). The same happens when the credential
  ceiling stops the rotation: either way the provider ran out of credentials the
  runtime is allowed to try, and the abort is bounded and self-healing (the short
  provider cooldown expires, and the credential cooldowns reorder the pool so an
  untried credential goes first next time).
* **Provisioning and execution can never disagree.** `apply_stt_config` builds the
  selected engine from the pool's OWN first credential and records which one that
  was (`provisioned_credential_id`) in the rotation registration; the first
  attempt reuses the already-provisioned engine for that exact credential instead
  of building an equivalent one. The pool decides which credential the engine
  carries — the engine factory never reaches for one itself.
* **The adapters stay untouched.** A pooled credential is passed explicitly
  (`api_key=…`, and the adapter's own `key_env_var` becomes `explicit`); the
  deployment's OWN credential is passed as “resolve your own”, which keeps the
  adapter's existing ENV resolution and its truthful variable-name label. No
  adapter reads a credential pool, a Vault or an id.
* **The Gemini leg** received one additive parameter —
  `apply_stt_settings(stt_settings, credential=(api_key, label) | None)` — whose
  default is the pre-existing `resolve_api_key()` path, so every existing call
  site and test is unchanged. Nothing about the Gemini transport, the STT
  instructions or the recognition passes changed.
* **Two new entry points, both additive**: `build_engine_with_credential(...)`
  beside the unchanged `build_engine(...)`, and `apply_stt_config_async(...)`
  which loads the pools and then calls the unchanged `apply_stt_config(...)`. The
  two places that already owned this state now await the async form: the runtime
  supervisor at startup and the STT settings handler after a save.

### Bounds and timeouts

| Bound | Value | Meaning |
|---|---|---|
| `MAX_PROVIDER_ATTEMPTS` | **3** | unchanged from M2.3 — providers per transcription unit |
| `MAX_CREDENTIAL_ATTEMPTS_PER_PROVIDER` | **3** | credentials attempted per provider inside one unit, whatever the pool contains |
| `MAX_TOTAL_ATTEMPTS` | **6** | ALL attempts of one unit, providers and credential rotations together: credential rotation may at most DOUBLE the pre-existing worst case, so no `credential × retry × provider × chunk × pass` explosion exists |
| `MIN_ATTEMPT_S` | **8.0 s** | unchanged — no attempt is started without this much of the unit's budget left |
| one shared budget | the boundary's `STT_TIMEOUT_S` per chunk, or what is LEFT of the aggregate deadline | **credential rotation never resets the deadline**: a later credential receives only the REMAINING budget, computed immediately before its attempt |
| credential cooldown | **60 s → 600 s** | bounded doubling, capped, process-local |
| `VAULT_TIMEOUT_S` | **5 s** | the credential read's own bound; it runs at settings-apply time and NEVER inside a media request, so it can never charge the STT budget |

### Interaction with M1.8 chunking

Unchanged in contract, and the unit of rotation is the CHUNK:

* the request keeps ONE attempt plan across all chunks, so a credential that
  succeeded is PINNED and continues the later chunks (`_pin(candidate, credential)`);
* a credential that fails on chunk N is never re-tried within the request, so
  chunks 1..N-1 are **never retranscribed** — the chunks that already succeeded
  stay valid and the next credential attempts only chunk N;
* the merge stays ordered (`stt_chunking.join_transcripts`) and a pool exhausted
  mid-recording fails the WHOLE operation instead of returning a partial
  transcript;
* each chunk keeps the same per-chunk / aggregate budget rules, and the rotation
  consumes only what is left of the chunk's own bound;
* one download, the same validation, the same cleanup: the pool adds no second
  transfer and no temporary artefact.

### Interaction with multi-pass / consensus

Untouched. Recognition passes live INSIDE each engine, so one `transcribe()` call
is exactly ONE attempt of this layer however many passes it contains. A failed
credential is never treated as a transcript hypothesis, no hypothesis is taken
from a rotation, and no new consensus mechanism exists.

### Security guarantees

* **The key never leaves the credential record.** The only credential fact ever
  logged is its id — an environment VARIABLE name, or an id the owner chose — and
  the repository's existing redaction (`_safe_detail` in each adapter) keeps a
  provider detail from echoing a key back.
* **Nothing is persisted by the application.** The snapshot and the health state
  are in-process memory only; no file, no table, no column, no log line and no
  Telegram message carries a secret. Two source-scan tests pin that the credential
  modules contain no file write, no `os.environ`, no `to_thread`, no pickle and no
  transport/Telegram import.
* **No arbitrary ENV scanning** — only the provider's declared variable names, as
  a behavioral test proves.
* **Zero Telegram context**: the attempt seam's signature is asserted to have
  nowhere to carry a chat id, message id, sender, caption, reply or user, and the
  credential modules import nothing from `backend.bot` or Telethon.
* **No user-visible credential UI**: this phase adds no panel and exposes no key
  through Telegram. Credential management, if it is ever wanted, is a later phase.

### Supabase Vault configuration that MUST be performed manually

> **Superseded in one respect by PART 1 (top of this report).** The security
> intent, the mapping rules, the bounds and the ENV fallback recorded below all
> still stand, and the section is preserved as the M2.4 record. Two things
> changed when PART 1 shipped the actual migration: the RPC name is now
> `api_credential_pool` (`stt_credential_pool` is kept as a compatibility
> alias), and the Vault side is no longer an abstract contract — it is
> `supabase/migrations/20260919000001_create_api_credential_vault.sql`, with its
> complete SQL in `DATABASE_ARCHITECTURE.md` §29. It is still **NOT applied**:
> no SQL was executed and no Vault secret exists.

**This phase did NOT configure Supabase Vault.** No SQL was executed, no secret
was created, no schema was altered, and `DATABASE_ARCHITECTURE.md` was not
touched. The application-side boundary is implemented and tested against a fake
backend; the real secret store is the owner's to create.

For a pool to exist, the user creates the Vault secrets AND one callable wrapper
over them. The exact contract the application expects:

```
RPC name : stt_credential_pool            (credential_source.VAULT_RPC)
Transport: PostgREST, with the project's EXISTING service-role client
Request  : POST /rest/v1/rpc/stt_credential_pool  body {"p_provider": "<provider>"}
Response : a JSON array of rows, in the desired order:
           [ { "credential_id": "<stable, non-secret id>",   required
               "secret":        "<the API key, decrypted>",   required
               "priority":      <int>,                        optional, default 0
               "enabled":       <bool> },                     optional, default true
             ... ]
```

* `provider` is one of the names the registry uses: `gemini`, `groq`,
  `speechmatics`. The RPC receives it as `p_provider` and returns only that
  provider's credentials.
* `credential_id` is what the logs will show (`vault:<credential_id>`). It must be
  non-secret, 1–64 characters, and only `A-Z a-z 0-9 . _ -`; anything else is
  refused rather than sanitized, so an id can never smuggle a key fragment into a
  log line.
* `secret` must be the DECRYPTED value. The natural implementation stores the key
  in Vault and returns `vault.decrypted_secrets.decrypted_secret`, so the
  application never reads `vault.*` directly and the store's own schema, naming
  and access policy stay the owner's.
* `priority` orders the pool; a lower value is tried first. Ties put the
  deployment's ENV credential first, then the order the RPC returned.
* `enabled: false` (or an omitted/empty `secret`, or a missing/invalid
  `credential_id`) makes the row contribute nothing — a half-configured store
  degrades to the credentials that DO work.
* At most **4** credentials survive per provider, and a read is abandoned after
  **5 s**.
* If the RPC does not exist, is not executable by the service role, or returns
  anything unexpected, the application logs a bounded reason and keeps using the
  environment credential. Nothing breaks, and the media path is unaffected.

**Render ENV role:** unchanged, and deliberately minimal. Render keeps the ONE
credential per provider it already has (`AI_GEMINI_API_KEY`/`GEMINI_API_KEY`,
`AI_GROQ_API_KEY`/`GROQ_API_KEY`, `AI_SPEECHMATICS_API_KEY`), plus the Supabase
bootstrap secrets the deployment already needs to reach its own backend
(`SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`). No `*_KEY_1`/`*_KEY_2`/`*_KEY_3`
variable is introduced, and **no new environment variable at all** is required by
this phase. A deployment that later wants to drop the provider keys from Render
and keep them only in Vault can do so, because the pool is authoritative once it
has been loaded: provisioning uses the pool's first credential either way.

### Files changed by this phase

| File | Change |
|---|---|
| `backend/ai/credential_source.py` | **NEW** (324 lines) — the secret boundary: the ENV credential, the documented `stt_credential_pool` RPC read, deterministic ordering, the bounded row validation, the bounded process-local cache and the fail-closed degradation |
| `backend/services/stt_credential_pool.py` | **NEW** (353 lines) — the credential pool: deterministic rotation order, credential health/cooldown, credential-vs-provider classification, the bounded ceilings and the secret-free `describe()` trace field |
| `backend/services/stt_fallback.py` | **EXTENDED** (513 → 729 lines) — the inner (candidate × credential) loop, the pin extended to the credential, the pool-exhaustion → provider-fallback transition, the credential traces, `MAX_TOTAL_ATTEMPTS`, and the `provisioned_credential_id` registration |
| `backend/services/stt_engine_factory.py` | **EXTENDED** (139 insertions) — `build_engine_with_credential(...)`, the credential-aware provisioning of the selected candidate, the recorded provisioned credential, and `apply_stt_config_async(...)` (load the pools, then apply) |
| `backend/services/gemini_media_engine.py` | **14 insertions** — ONE additive parameter on `apply_stt_settings` (`credential: tuple[str, str] | None = None`), defaulting to the pre-existing `resolve_api_key()` path. No transport, instruction or pass change |
| `backend/runtime/supervisor.py` | **10 insertions** — the startup apply now awaits `apply_stt_config_async`, so the pools are loaded BEFORE the engine is provisioned |
| `backend/bot/handlers/ai_stt_settings.py` | **8 insertions** — the panel save now awaits the same async apply, so a selection change reloads the pools with it |
| `tests/conftest.py` | one autouse reset extended: the credential snapshot and credential health are process-local runtime state, so they may not leak between tests |
| `tests/test_stt_credential_pool.py` | **NEW** (1354 lines, 85 tests) — the contract below |

**Untouched (deliberately):** every provider adapter's execution code
(`groq_stt_engine.py`, `speechmatics_stt_engine.py`, the Gemini request path),
`stt_control_plane.py`, `stt_provider_probe.py`, `stt_consensus.py`,
`stt_chunking.py`, `media_service.py`, `media_ai_service.py`, the Telegram panels
and helper machinery, the dispatcher/engine/tool layer, `ProviderManager`,
`RuntimeSupervisor` recovery, OCR, PDF/DOCX extraction, `backend/db/**`,
`requirements.txt`, `render.yaml`, `supabase/migrations/*.sql`,
`DATABASE_ARCHITECTURE.md` and all secrets.

### Tests added and exact results

| Suite | Result |
|---|---|
| `tests/test_stt_credential_pool.py` (new) | **`85 passed` in 0.76 s** |
| the STT / media suites (`test_stt_fallback.py`, `test_ai_stt_settings.py`, `test_stt_provider_probe.py`, `test_stt_credential_pool.py`, `test_stt_consensus.py`, `test_media_stt.py`, `test_media_stt_chunking.py`, `test_media_stt_reliability.py`, `test_media_stt_multipass.py`, `test_media_stt_language.py`, `test_media_dedicated_stt.py`, `test_media_direct_stt.py`, `test_media_processing.py`, `test_media_scope_and_delivery.py`, `test_groq_stt_engine.py`, `test_speechmatics_stt_engine.py`, `test_media_stt_benchmark.py`, `test_media_gemini_engine.py`, `test_media_ai_integration.py`, `test_media_image_ocr.py`, `test_media_document_extraction.py`) | **`1187 passed, 2 skipped` in 41.87 s** (the 2 skips are the pre-existing opt-in live provider probes — no credential here) |
| **Full suite** | **`4064 passed, 26 skipped, 3 warnings` in 115.01 s** |

Count provenance, so the arithmetic is auditable: the M2.3 section below recorded
**3979 passed / 26 skipped** at this phase's starting HEAD `9057f05`. This phase
adds **85** tests and deletes, weakens or skips **none** → **4064 / 26**. (The `26`
skips are pre-existing opt-in live probes; the local interpreter here was CPython
3.10.12, while production is the `render.yaml` pin of 3.11.7.)

The new suite pins, from the source rather than from prose:

* **ordering** — the environment credential is the first and only default; the
  environment is never scanned for an undeclared name; the pool follows the
  environment in the backend's order; an explicit priority outranks the source
  order; a disabled, unidentified, badly-named or secret-less row contributes
  nothing; the pool is bounded per provider; a refusing backend keeps the
  environment credential and marks the pool configured; an unloaded provider is
  reported unconfigured (which is a DIFFERENT state from loaded-and-empty); a
  stale marking never reduces the runtime's credentials; the description carries
  ids and never a secret; an unconfigured database yields no read at all; the
  Vault read uses exactly the documented RPC name and parameter (in this phase
  that name was `stt_credential_pool`; PART 1 moved the runtime to the generic
  `api_credential_pool` and kept the old name as an alias — the call shape the
  test pins, `{"p_provider": …}`, is unchanged);
* **classification** — every credential class and every credential HTTP status
  (401/403/429) is credential-specific; every provider class and 400/404/5xx is
  not; a programming error and a bare `MediaError` are never credential-specific;
* **health** — its own bounded, capped, doubling cooldown with a fake clock; a
  cooled-down credential leaves the rotation; expiry restores it; success restores
  it immediately; a provider whose credentials are ALL cooling down is still
  attempted; the health map is process-local and resettable;
* **rotation** — one healthy credential serves the request alone (no other engine
  is even asked for); a rejected credential rotates; two failures rotate twice; a
  rate limit and an undecryptable credential rotate BEFORE the provider layer; a
  503 does not burn the pool (the second credential is never even built) and does
  not touch the credential's health; a programming error never rotates; the last
  reason survives into the exhaustion error; pool exhaustion hands over to the
  provider layer with the two transition traces; one bad credential leaves the
  provider healthy; the next provider uses its own pool; a cooldown prunes only
  the credential rotation and never demotes the selection; a single configured
  credential keeps the exact pre-pool behaviour;
* **bounds** — the per-provider credential ceiling is enforced (a four-credential
  pool costs at most three attempts); the per-unit total is exactly
  `MAX_TOTAL_ATTEMPTS`; the budget strictly shrinks between credential attempts
  and is never reset; a starved credential is neither constructed nor started and
  the selected provider's own failure survives; every bound is finite and small;
* **chunking** — a credential that fails on a later chunk never retranscribes the
  earlier chunks; a healthy credential serves every chunk with no switch; an
  exhausted pool mid-recording fails the whole operation and never returns a
  partial transcript; a single-piece request rotates end to end through the media
  boundary with exactly ONE download;
* **security** — a failing and a succeeding rotation both keep every secret out of
  the logs while credential IDS are present; the failure message carries no
  credential; a provisioned engine exposes the label and never the secret; an
  environment credential keeps the adapter's own resolution and variable-name
  label; the credential modules import no transport, no Telegram and no database
  store; the credential layer persists nothing;
* **provisioning** — the engine and the recorded rotation agree on the credential
  they use; an explicit-priority Vault credential is what provisioning uses; a
  refusing backend leaves provisioning unchanged; and provisioning uses the SAME
  order the rotation uses.

The suite replaces the secret backend with a fake at the ONE loading seam and
scripts the engines per `(candidate, credential)` pair, so it says nothing about
recognition quality and contacts no provider.

**Syntax / whitespace:** `python -m py_compile` clean on all nine changed Python
files; `git diff --check` clean.

### Live verification status

* **Live Telegram:** NOT PERFORMED — no Telegram session or traffic exists in this
  environment. The panel still shows the owner's selected candidate, a rotation is
  invisible to the UI, and that claim is proven by test, not by a live walkthrough.
* **Live providers:** NOT PERFORMED — no provider credential exists here, so no
  candidate and no credential was contacted. **No provider is claimed healthy**
  and no credential is claimed valid.
* **Live Supabase Vault:** NOT PERFORMED — **no Vault pool has been created**. The
  Vault path was exercised only against a fake backend that returns the documented
  row shape (and against one that refuses). The application-side contract is
  therefore verified; the real secret store is NOT.
* **Recognition quality:** unchanged and unmeasured. This phase alters WHICH
  credential may answer a request; it does not change what any provider
  transcribes, and no transcript-quality claim is made.

### Known limitations

1. **The pool is loaded at startup and at every STT settings apply, not per
   request.** A credential added to the backend appears at the next settings apply
   (a panel save or a restart); a revoked one keeps being tried until then, takes
   its bounded cooldown, and the rotation moves on to a healthy credential. This is
   deliberate — an in-request secret read would spend the media budget on a
   database call, and a background refresher would be a second scheduler.
2. **The rotation is bounded, not exhaustive.** A pool larger than three
   credentials (or a unit whose budget runs out) stops after the ceiling and hands
   the rest to the provider layer; the untried credential is picked up by a later
   request, because the failed ones are cooling down and the pool order follows
   health.
3. **Health is process-local**: a restart forgets every cooldown, and two Render
   instances do not share it (deliberate — no new table, no new column).
4. **Ordering is priority + source order, not keyword quality.** It exists so a
   request survives one bad key, not to pick the best key.
5. **No credential-management UI.** Enabling/disabling, reordering, viewing health
   and testing a credential from Telegram were explicitly out of scope and remain
   a later phase; credentials are managed in the secret backend and Render.
6. **A credential is trusted by its label.** Rotating between keys of the same
   provider can only change quota and validity, never the response format; the
   adapters' existing validation is unchanged and still refuses a malformed or
   empty transcript.
7. The M1.8/M2.0 persistence note stands unchanged: the `ai_config` STT columns
   still require the pending manual migration, and `DATABASE_ARCHITECTURE.md` §7
   still describes the superseded M1.8 semantics.

### Deferred work

* **TTS** — deliberately absent in this phase: nothing in M2.4 speaks, and no
  local model or cloud voice was added. **Delivered in M3.0** (above) as a separate
  capability on a separate boundary — NOT as a copy of this credential pool, which
  remains STT-only.
* **Native Vision, Video and GIF processing** — still out of scope by the M1.7
  decision; the media boundary still excludes them before any transfer.
* **Credential-management UI in Telegram**, a persisted credential roster, a
  per-credential quota dashboard, real Persian recognition benchmarking, and
  evidence-based provider/key ranking — all still open.
* Any second secret manager (the boundary is written for one to be added without
  touching the STT runtime, but only the ENV and Vault sources exist today).

### Exact order of the remaining work

1. **Configure the Supabase side** (the RPC contract above) and run
   `Test all providers` — that is the live verification of the Vault path, which
   no test in this environment can perform.
2. **Live Telegram verification of M2.3 + M2.4 together**: send a Voice/Audio
   request with the selected provider deliberately un-credentialed, then with a
   first credential deliberately revoked, and confirm the request still succeeds
   while the panel still shows the owner's own selection, and the logs carry
   `STT_CREDENTIAL_ATTEMPT` / `STT_CREDENTIAL_FAILURE` / `STT_CREDENTIAL_SUCCESS`
   (or the ONE `STT_CREDENTIAL_POOL_EXHAUSTED` → `STT_FALLBACK_POOL_TO_PROVIDER`
   transition) with no secret and no identifier in them.
3. **Real Persian recognition benchmarking** on 30–50 real voice messages across
   the registered candidates — the prerequisite for choosing the provider order
   on evidence rather than on the registry's canonical order.
4. Optionally, a persisted credential roster/health design and per-owner ranking —
   deliberately absent today.
5. Applying the pending `ai_config` migration and the documentation-only
   `DATABASE_ARCHITECTURE.md` §7 semantics refresh.

---


## Previous phase — Media Processing M2.3: STT provider health and bounded automatic fallback

Repository `Onlyicing1/Telegram-self-bot` · branch `main` · state as of 2026-09-18.

> **Superseded in three places by M2.4 (above), which is otherwise additive to
> this section.** (a) `backend/services/stt_fallback.py` is no longer 513 lines —
> it was extended with the credential loop, and the per-unit attempt ceiling is
> now `MAX_TOTAL_ATTEMPTS` on top of `MAX_PROVIDER_ATTEMPTS`. (b) The two
> “Intentionally NOT implemented” lists below no longer apply to credential pools
> and key rotation, which M2.4 delivers — and a credential-specific failure is now
> one additional case that may cascade to another provider, but ONLY when the
> provider really has more than one credential. (c) `stt_engine_factory.py`,
> `gemini_media_engine.py`, `backend/bot/handlers/ai_stt_settings.py` and
> `backend/runtime/supervisor.py` are no longer in this section's “untouched”
> list, for the additive reasons recorded in M2.4.

### Phase identity

| Item | Value |
|---|---|
| Phase name | **Media Processing M2.3** — Speech-to-Text provider health and bounded automatic fallback at the STT orchestration seam |
| Starting HEAD | `add0b88` `feat(stt): transcribe over-long audio in bounded chunks` (== `origin/main` at phase start) |
| Implementation commit | the single phase commit that contains this report (`git log -1 --format=%H` re-verifies it) |
| Database migration required | **NO** — no schema change, no column, no table |
| New environment variables | **NONE** |
| New dependencies | **NONE** (`requirements.txt` untouched) |
| Files changed | **five** — NEW `backend/services/stt_fallback.py`, NEW `tests/test_stt_fallback.py`, `backend/services/media_service.py`, `backend/services/stt_engine_factory.py`, `tests/conftest.py` |
| Behavioural change | **exactly one**: a fallback-ELIGIBLE failure of the selected provider no longer fails the operation by itself — the other eligible candidates are then attempted, under a finite ceiling |
| Persisted state | **NONE** — provider health and cooldown are process-local runtime posture |
| Live Telegram verification | **NOT PERFORMED** |
| Live provider verification | **NOT PERFORMED** — no `AI_GROQ_API_KEY` / `AI_SPEECHMATICS_API_KEY` / `AI_GEMINI_API_KEY` exists in this implementation environment, so no request was made and no provider is claimed healthy |
| Recognition-quality claim | **NONE** — this phase changes WHICH provider may answer, never what it transcribes |

### Purpose of this phase

The control plane (M2.0–M2.2) could already express the owner's selection, the
ordered candidate pool and the failure taxonomy, but the execution half was
missing: the SELECTED candidate was the only one ever tried, so one transient
provider failure failed the whole media operation and the owner had to retry by
hand. This phase adds the missing execution half **in front of the existing seam**:

```
Voice / Audio
    ↓  deterministic target resolution, ONE bounded download (unchanged)
media_service.analyze_media()  →  _extract_audio_content / _run_stt_chunked
    ↓  the selected engine is ALWAYS attempt 1
stt_fallback.AttemptPlan.run(...)
    ├── success ─────────────────────────────────────────────→ transcript
    └── fallback-ELIGIBLE failure
            ↓  next eligible candidate (control plane's own canonical order)
        success ──────────────────────────────────────────────→ transcript
            └── ceiling / budget reached → ONE honest exhaustion failure
```

| Before M2.3 | After M2.3 |
|---|---|
| the selected candidate was the only attempt | the selected candidate is the FIRST attempt, and the ordered pool backs it up |
| one transient failure failed the media operation | a transient failure moves to the next eligible candidate |
| a deterministic failure and a transient one were indistinguishable to the caller | only transient failures may fall back; deterministic ones propagate unchanged |
| a manually-chosen provider was required after a failure | no manual provider/model entry is ever needed — the runtime substitutes |
| the selection could be silently changed by a workaround | the selection is never written; a fallback is internal to one request |

Explicitly NOT implemented: any second STT pipeline or executor, any provider
call inside an adapter's own retry (`gemini_media_engine`, `groq_stt_engine`,
`speechmatics_stt_engine` are untouched), ~~credential pools or key rotation~~
(**delivered later, in M2.4 — see the phase above**), a
persisted health table, per-owner ranking, real Persian recognition benchmarking,
TTS, Native Vision, a new provider, a new dependency, and any change to
`ProviderManager`, the dispatcher, the tool layer, `RuntimeSupervisor` recovery,
the Supabase schema, `render.yaml` or `requirements.txt`.

### The new layer — `backend/services/stt_fallback.py`

One new module owns the EXECUTION half and nothing else. It never talks to a
provider, never touches a Telegram object, never rewrites the owner's selection,
and never replaces the boundary: it decides which candidate to try next, in what
order, whether a candidate is temporarily unhealthy, whether a failure is
fallback-eligible, and when to stop.

| Concern | Where it lives |
|---|---|
| which candidates exist, their canonical order, the owner's selection, language, passes | **control plane** (`backend/ai/stt_control_plane.py`) — unchanged, reused as the single source of order |
| candidate → engine construction | **engine factory** (`backend/services/stt_engine_factory.py`) — the ONE seam, consulted lazily per candidate |
| health, failure classification, cooldown, the bounded attempt loop | **this phase** (`backend/services/stt_fallback.py`) |
| resolution, one bounded download, validation, the timeout, cleanup, normalization, chunking | **media boundary** (`backend/services/media_service.py`) — unchanged contracts |
| talking to a provider | the provider adapters — unchanged, and never invoked by this layer directly |

#### The ordered-attempt contract

* **The selected candidate is attempt 1 of every request, always**, whatever its
  health: the owner's preference never permanently loses priority because it
  failed once. Cooldown prunes only the FALLBACK rotation.
* Candidates come from the control plane's registry in its own canonical order
  (a tuple literal), never from a hard-coded list in the media service.
* A candidate that is not registered, not implemented, or **cannot be built**
  (no credential) is skipped and **never invoked** — it is not an attempt, and it
  does not consume the budget.
* A plan is created **per request**. Within one over-long recording the candidate
  that succeeded is PINNED, so a provider switch mid-recording never
  retranscribes the earlier chunks, and a candidate that already failed THIS
  request is not retried within it.
* **Failure classification is fail-closed.** The adapter's own `retryable`
  verdict WINS when present (the adapter that talked to the provider is the
  honest classifier); otherwise only the engines' own transient vocabulary may
  fall back — `timeout`, `transport`, `transport_failure`, `server`,
  `rate_limit`, `operation_deadline`, `upload_timeout`, `request_timeout`,
  `interaction_timeout` — plus the boundary's own `media_stt_timeout` leg
  (the provider was too slow for THIS budget). Everything deterministic
  (`auth`, `forbidden`, `missing_credential`, `unsupported_model`,
  `unsupported_audio`, `invalid_request`, `malformed_response`,
  `empty_transcription`, `provider_rejection`, `upload_failed`,
  `file_processing`, `http_rejection`, anything unrecognized, and every
  non-`MediaError`) propagates unchanged — a programming error is never hidden
  behind a fallback.
* **An exhausted rotation is reported as itself** and never as a bad recording:
  one `MediaError` with `stage=media_stt_exhausted` and
  `failure_class=fallback_exhausted`, naming the attempt count and the
  (already adapter-sanitized) reason of the LAST failure.
* **A request with no runnable substitute keeps the selected provider's own
  failure VERBATIM** — same exception, same stage, same class. Arming this layer
  therefore never rewrites the identity of a single-provider failure, which is
  what keeps every pre-existing single-engine behavior and test intact.

#### Bounds (all finite)

| Bound | Value | Meaning |
|---|---|---|
| `MAX_PROVIDER_ATTEMPTS` | **3** | provider attempts per transcription unit (one chunk, or one single-piece audio) — the selected provider plus at most two substitutes. Multi-pass behavior stays INSIDE each engine: one `transcribe()` call is one attempt, however many passes it contains. The chunked route is therefore bounded at `chunks × passes × 3`, still under the pre-existing chunk count, aggregate deadline and character ceilings |
| `MIN_ATTEMPT_S` | **8.0 s** | a substitute is not even constructed, let alone started, without this much of the unit's budget left — the same floor the adapters use for their own bounded retries |
| one shared budget | the boundary's `STT_TIMEOUT_S` per chunk, or what is LEFT of the aggregate deadline | a later candidate receives only the REMAINING budget, never a fresh one |
| `COOLDOWN_BASE_S` / `COOLDOWN_MAX_S` | **60 s → 600 s** | bounded doubling per consecutive failure, capped; deterministic and short enough that a blipped provider is eligible again within minutes |

#### Provider health and cooldown

* Health is **process-local runtime posture, not configuration**: nothing is
  written to `ai_config`, Supabase or any store, and a restart honestly resets it.
* A candidate that fails a fallback-eligible attempt leaves the FALLBACK rotation
  for a bounded cooldown; a SUCCESS restores it immediately.
* **The Telegram UI state and the persisted selection are never touched** — a
  runtime substitution is internal to one request, and the panel keeps showing
  the owner's chosen candidate. The module reads no ENV and holds no credential.
* A legacy/unresolved stored model deactivates fallback entirely, and a selection
  whose OWN engine cannot be provisioned clears the rotation — in both states the
  boundary keeps its exact pre-fallback, fail-closed single-engine behavior.

#### Import direction (why the module is bound eagerly but reads the boundary lazily)

`backend.services.media_service` binds this module at import time (`from
backend.services import settings_service, stt_chunking, stt_fallback`), so
`stt_fallback` declares **no** module-level import of the boundary and **no**
module-level import of the control plane — reaching the control plane pulls
`backend.services.gemini_media_engine`, which imports `MediaError` from the
boundary. Both are therefore resolved on first use, and the dependency points one
way at import time. All import orders were verified directly (`media_service`
first, `stt_fallback` first, `gemini_media_engine` first, `stt_control_plane`
first, `stt_engine_factory` first).

#### Boundary integration

* `_extract_audio_content` (single-piece, at or under `MAX_STT_DURATION_S`) and
  `_run_stt_chunked` (over-long audio) both drive ONE plan through the boundary's
  **existing** `_run_stt` primitive, reached per attempt via the new thin
  `_stt_attempt` hook, so every attempt keeps the same worker-thread execution,
  awaited timeout on the remaining budget and classified stage as before.
* The boundary traces `stt_fallback_armed` (`selected`, `candidates`) when a
  rotation is active, so a live request is diagnosable in one line.
* The chunked contract is unchanged: chunks in strict source order, one at a
  time, ONE aggregate deadline, and an exhausted attempt plan fails the WHOLE
  operation — a partial transcript is never returned as a complete one.
* An unarmed runtime (legacy, unconfigured or unprovisionable selection) takes the
  exact pre-M2.3 code path.

#### Traces (structured, bounded, content-free)

`STT_FALLBACK_PLAN` (`state=active|inactive`, `selected`, `fallback_candidates`),
`STT_FALLBACK_ATTEMPT` (`candidate`, `index`, `ceiling`, `budget_s`),
`STT_FALLBACK_FAILURE` (`candidate`, `attempt`, `failure_class`, `eligible`),
`STT_FALLBACK_COOLDOWN` (`candidate`, `failures`, `cooldown_s`,
`failure_class`), `STT_FALLBACK_SKIPPED` (`reason=cooldown|not_implemented|
unknown_candidate`), `STT_FALLBACK_STOPPED` (`reason=insufficient_budget`,
`remaining_s`, `attempts`), `STT_FALLBACK_SUCCESS` (`candidate`, `attempt`,
`chars`) and `STT_FALLBACK_EXHAUSTED` (`attempts`, `last_failure_class`) —
candidate ids, attempt indices, failure classes, budgets and durations only.
Never a credential, an audio byte, a transcript, a caption or a Telegram
identifier, and never an owner id (pinned by test).

### Files changed by this phase

| File | Change |
|---|---|
| `backend/services/stt_fallback.py` | **NEW** (513 lines) — the execution half: the registered rotation, fail-closed failure classification, bounded cooldown, the ordered attempt plan with its pin and per-request failure memory, and the ONE exhaustion error |
| `backend/services/media_service.py` | binds the new module, adds the `media_stt_exhausted` stage token and the thin `_stt_attempt` attempt hook, and drives one attempt plan from BOTH the single-piece and the chunked STT routes (unarmed → the previous code path) |
| `backend/services/stt_engine_factory.py` | arms the rotation inside the ONE `apply_stt_config` entry point, from the SAME parsed control plane that provisions the selected engine; clears it when the selected candidate has no engine |
| `tests/test_stt_fallback.py` | **NEW** (960 lines, 68 tests) — the contract below |
| `tests/conftest.py` | one autouse reset: provider health and the rotation are process-local runtime state, so they may not leak between tests (a suite that applies an STT config arms a rotation for the whole process) |

**Untouched (deliberately):** every provider adapter (`gemini_media_engine.py`,
`groq_stt_engine.py`, `speechmatics_stt_engine.py` — no provider-call change, no
instruction change), `stt_control_plane.py`,
`stt_provider_probe.py`, `stt_consensus.py`, `stt_chunking.py`,
`backend/services/media_ai_service.py`, ~~`backend/bot/handlers/ai_stt_settings.py`~~
(**touched by M2.4 only to await the credential-aware apply**),
`backend/helper/**`, the dispatcher/engine/tool layer, `ProviderManager`,
`RuntimeSupervisor`, OCR, PDF/DOCX extraction, the database layer,
`requirements.txt`, `render.yaml`, `supabase/migrations/*.sql`,
`DATABASE_ARCHITECTURE.md` and all secrets.

### Tests added and exact results

| Suite | Result |
|---|---|
| `tests/test_stt_fallback.py` (new, 68 tests) | **`68 passed`** |
| the STT / media suites (`test_media_stt.py`, `test_media_stt_chunking.py`, `test_media_stt_reliability.py`, `test_ai_stt_settings.py`, `test_stt_provider_probe.py`, `test_media_direct_stt.py`, `test_media_stt_language.py`, `test_media_stt_multipass.py`, `test_media_stt_benchmark.py`, `test_stt_consensus.py`, `test_groq_stt_engine.py`, `test_speechmatics_stt_engine.py`, `test_media_gemini_engine.py`, `test_media_processing.py`, `test_media_ai_integration.py`, `test_media_dedicated_stt.py`) | **`932 passed, 2 skipped`** (the 2 skips are the opt-in live provider probes — no credential here) |
| **Full suite** | **`3979 passed, 26 skipped, 3 warnings` in 115.20 s** |

Count provenance, so the arithmetic is auditable: the M2.2.1 section below
recorded **3852 passed / 26 skipped**, the next phase added **25** tests
(`test_media_scope_and_delivery.py`) and this phase's starting HEAD added **34**
(`test_media_stt_chunking.py`) → **3911 / 26** at `add0b88`. This phase adds **68**
and deletes, weakens or skips **none**. (The `26` skips are pre-existing opt-in
live probes; the local interpreter here was CPython 3.10.12, while production is
the `render.yaml` pin of 3.11.7.)

The new suite pins, from the source rather than from prose:

* **classification** — the adapter's `retryable` verdict wins over its class; each
  transient token may fall back; **every** deterministic token and every
  non-`MediaError` may not; the boundary's timeout leg may; `failure_class` is
  never empty;
* **health** — per-candidate cooldown, bounded doubling capped at
  `COOLDOWN_MAX_S`, immediate healing on success, process-local reset, and (by AST
  inspection) that the module imports no store, no DB, no `os`/ENV and reads no
  credential;
* **arming** — no rotation → no plan; no engine → no plan; the rotation is exactly
  the control plane's canonical tail; the selection is attempt 1; a legacy value
  deactivates fallback; the factory arms the rotation from the SAME config it
  applies; a selected candidate with no engine leaves the boundary fail-closed;
  substitutes are built with the owner's own language/pass settings;
* **the attempt loop** — a healthy selection serves the request alone (no
  substitute is even constructed); an eligible failure moves to the next
  candidate; a deterministic failure and a programming error propagate unchanged
  with no substitute built; the ceiling is exactly `MAX_PROVIDER_ATTEMPTS`; the
  budget strictly shrinks between attempts; a starved substitute is neither built
  nor started and the selected provider's own failure survives; exhaustion is
  reported as itself with the LAST bounded reason and never blames the audio; no
  runnable substitute preserves the failure object verbatim; a failed-this-request
  candidate is not retried within it; a cooldown prunes only the fallback
  rotation and never demotes the selection; a success clears the cooldown;
* **the boundary end-to-end** — a single-piece Voice request returns the
  substitute's transcript as `MediaAnalysis.content`; the text is still normalized
  and capped; a fallback rewrites neither the persisted selection nor the engine
  seam nor the UI (and no `ai_config` write happens); an unarmed boundary returns
  the selected provider's failure object itself; exhaustion carries
  `media_stt_exhausted`; an over-long recording is pinned to the candidate that
  worked for every later chunk; an exhausted chunked operation raises instead of
  returning a partial transcript; exactly ONE download happens and no temporary
  artefact survives;
* **hygiene** — the fallback traces are present, bounded and free of the
  transcript, the caption, the owner id and any credential; the attempt seam's
  signature has nowhere to carry a chat, message, sender or caption; the engines
  receive only the validated audio bytes; and the new module adds no HTTP client,
  no subprocess, no socket, no worker thread and no second `transcribe` pipeline.

**Syntax / whitespace:** `python -m py_compile` clean on every changed Python file
(`stt_fallback.py`, `media_service.py`, `stt_engine_factory.py`,
`test_stt_fallback.py`, `conftest.py`); `git diff --check` clean.

### Live verification status

* **Telegram:** NOT performed — no Telegram session or traffic exists in this
  environment. The panel still shows the owner's selected candidate, the runtime
  substitution is invisible to the UI, and that claim is proven by test, not by a
  live walkthrough.
* **Providers:** NOT performed — no credential for any STT provider exists here, so
  no candidate was contacted. **No provider is claimed healthy**, and the
  cooldowns observed are scripted, not live.
* **Recognition quality:** unchanged and unmeasured. This phase alters which
  provider may answer a request; it does not improve what any provider transcribes,
  and no transcript quality claim is made.

### Known limitations

1. A fallback costs wall-clock time inside the SAME budget: an attempt that fails
   slowly leaves less for the substitute, and with less than `MIN_ATTEMPT_S`
   remaining the rotation stops and the selected provider's own failure surfaces.
   This is deliberate (one bounded budget, no unbounded sweep), not a defect.
2. Health is process-local: a restart forgets every cooldown, and two Render
   instances do not share it (deliberate — no new table, no new column).
3. The rotation order is the control plane's canonical order, not a quality
   ranking: it exists so a request survives a provider outage, not to pick the
   most accurate provider. Choosing that order on measured Persian quality is
   still open.
4. A fallback can substitute a provider whose language coverage or container
   support differs from the selected one; the adapter refuses what it cannot take
   and that refusal is classified as a deterministic failure, so it never cascades
   further unless it is genuinely transient.
5. The M1.8/M2.0 persistence note stands unchanged: the `ai_config` STT columns
   still require the pending manual migration, and `DATABASE_ARCHITECTURE.md` §7
   still describes the superseded M1.8 semantics.

### Intentionally NOT implemented

* Any provider call, retry or fallback **inside** an adapter — adapters still only
  talk to their own service; the orchestration lives above them.
* Any second STT pipeline, executor, scheduler or media download path.
* Persisted health/cooldown state, ~~credential pools, key rotation~~ (**delivered
  by M2.4, which keeps the health state unpersisted as well**), per-owner
  ranking.
* Any change to the Telegram UI, the stored selection, the STT instructions, the
  OCR path, the document extractors, the chunking, the consensus/multi-pass
  behavior, or the character/duration/size ceilings.
* Real Persian recognition benchmarking — still the open step (see below).
* Any new dependency, any local Whisper/PyTorch/ONNX/ffmpeg stack, any behavioral
  ENV variable.

### Exact order of the remaining work

M2.3 was implemented **before** the benchmarking step the M2.2.1 section planned
for that slot, on explicit instruction: provider resilience does not depend on
quality data, and it removes the manual provider re-selection the owner had to do
by hand. What remains:

1. **Live Telegram verification of this phase** — with real credentials, send a
   Voice/Audio request with the selected provider deliberately un-credentialed or
   failing, and confirm: the request still succeeds, the panel still shows the
   owner's selection, and the logs carry `STT_FALLBACK_ATTEMPT` →
   `STT_FALLBACK_FAILURE` → `STT_FALLBACK_SUCCESS` (or the ONE
   `STT_FALLBACK_EXHAUSTED` line) with no transcript or identifier in them.
2. **Real Persian recognition benchmarking** on 30–50 real voice messages across
   the registered candidates — still the prerequisite for choosing the rotation
   ORDER on evidence rather than on the registry's canonical order.
3. Optionally, a persisted-cooldown design (a new `ai_config` column or a table)
   and per-owner ranking — deliberately absent today.
4. Applying the pending `ai_config` migration and the documentation-only
   `DATABASE_ARCHITECTURE.md` §7 semantics refresh.

---

## Previous phase — Media Processing M1.8: bounded long-audio STT chunking

Repository `Onlyicing1/Telegram-self-bot` · branch `main` · state as of 2026-09-18.

### Phase identity

| Item | Value |
|---|---|
| Phase name | **Media Processing M1.8** — bounded long-audio STT chunking inside the existing media boundary |
| Starting HEAD | `b5e7c10` `feat: drop video/GIF from media processing, deliver one media response` — equal to `origin/main` at phase start |
| Implementation commit | the single phase commit that contains this report (`feat(stt): transcribe over-long audio in bounded chunks`) |
| Live Telegram verification | **NOT PERFORMED** |

### Purpose of this phase

`MAX_STT_DURATION_S` (300 s) was the longest audio the boundary would **accept**, so
a 14-minute recording could not be transcribed at all — it was refused with
`“exceeds the 300s speech-to-text bound”`. It is now the longest audio ONE
recognition may be asked to handle, and a longer recording is divided into ordered,
bounded chunks that the **unchanged** `SttEngine.transcribe(audio: bytes) -> str`
seam transcribes one at a time.

The division lives at the **orchestration boundary**: no provider adapter, no
`ProviderManager`, no new pipeline, no second download path and no second scheduler
was touched, and `transcribe(audio) -> str` is still the only engine contract — a
provider still receives ONE already-bounded payload per call.

Explicitly NOT implemented: automatic provider fallback, a provider health/cooldown
manager, credential pools, key rotation, TTS, Native Vision, video/GIF processing, new
STT or AI providers, quality benchmarking, Supabase tables or SQL, Render ENV
behaviour, any change to `ProviderManager`, any change to Telegram media target
resolution, any AI-based transcript correction, and any LLM-based chunk merge.

### Audit — what the code actually did with over-long audio

Traced before anything was changed, in the project’s source order:

* `media_service.analyze_media` → `_extract_audio_content` →
  `_validate_audio_payload(data, mime_type)`, whose duration guard raised
  `MediaError("The audio is 330s — exceeds the 300s speech-to-text bound.")` with stage
  `media_validation`. A long voice note therefore never reached an engine at all: no
  transfer-past-validation, no engine call, no partial processing.
* The engines behind the seam each already implement their own bounded multi-pass:
  `GeminiMediaEngine.transcribe` (general `generateContent` route, or the dedicated
  transcription route over the `interactions` API, both inside
  `STT_OPERATION_DEADLINE_S` = 45 s), `GroqWhisperEngine.transcribe` and
  `SpeechmaticsEngine.transcribe` — all three reconciling their passes through the
  existing `stt_consensus.reconcile_hypotheses`.
* The engine is chosen once by configuration (`stt_engine_factory.apply_stt_config` →
  `media_service.set_stt_engine`) from the registered candidates
  (`gemini:default`, `gemini:gemini-3.5-transcribe`, `groq:whisper-large-v3`,
  `groq:whisper-large-v3-turbo`, `speechmatics:standard`), and the boundary holds
  exactly ONE engine reference.
* Existing bounds: `MAX_STT_INPUT_BYTES` 20 MiB (pre-transfer), `MAX_STT_DURATION_S`
  300 s, `MAX_STT_CHANNELS` 2, `MAX_STT_SAMPLE_RATE` 48 kHz, `STT_TIMEOUT_S` 60 s,
  `MAX_STT_CHARS` = `MAX_EXTRACTED_CHARS` (the presentation ceiling).
* **No chunking existed anywhere.** `backend/tools/stt_benchmark.py` is the only other
  audio tool in the repository and it is not on the runtime path.
* **Environment facts that decided the mechanism:** `ffmpeg` is NOT present, the venv
  holds none of pydub/soundfile/numpy/av/torch, and `backend/requirements.txt` carries
  no audio dependency. A decoder-based splitter was therefore not available, and no
  heavy media stack was added for this phase.
* The media request’s own envelope is `media_ai_service.DEFAULT_ENVELOPE_S` = 240 s
  (the handler’s backstop), of which the provider call that answers an analytical media
  request reserves `PROVIDER_CALL_SAFETY_TIMEOUT_S` = 120 s. Those two established
  constants are what the new aggregate deadline is derived from.

### How the audio is divided (`backend/services/stt_chunking.py`)

One new module, standard library only (`typing` is its only import), whose entire
input is a validated payload plus two numeric ceilings — no Telegram object, no chat
id, no filename, no caption, no provider, no credential:

| Container | Divided at | Every chunk is |
|---|---|---|
| OGG/Opus, OGG/Vorbis (`audio/ogg`, `audio/opus`, `application/ogg`) | **OGG page boundaries** — the container’s own unit of framing (lacing table, granule position, CRC over itself) | the stream’s own codec header pages followed by a run of complete pages, **concatenated byte-for-byte** |
| RIFF/WAVE (`audio/wav`, `audio/x-wav`, `audio/wave`, `audio/vnd.wave`) | **frame boundaries** of its `data` chunk (plain PCM) | the source’s own pre-`data` chunks with the RIFF and `data` size fields repatched, so its duration is genuinely its own |
| anything else (incl. **FLAC**) | — | refused (`None`) |

* **Nothing is ever split on an arbitrary byte offset**, and no compressed packet is
  ever cut: an OGG chunk boundary is always a page boundary the stream itself declares,
  and a WAVE boundary always lands on a whole frame. Because every OGG page is copied
  verbatim, **no page CRC is invalidated** and no page has to be rebuilt.
* The chunk’s duration is taken from the stream’s **own granule positions** (Opus
  granules are always 48 kHz units; Vorbis granules are samples at the rate its
  identification header declares — the same rule the boundary’s reader uses), so a
  chunk is closed on the page *before* the one that would push it past the ceiling.
  Chunk 1 is therefore `[0 … 300 s]`, chunk 2 `[300 s … 600 s]`, and so on: contiguous,
  non-overlapping, and ordered.
* **Contiguous with no overlap, deliberately.** No overlap is invented “to improve
  quality”, so there is nothing to deduplicate on merge.
* **Fail-closed on everything else**: an unknown MIME, an unclean page walk (trailing
  bytes, a page whose payload runs past the file), a stream that is not Ogg Opus/Vorbis,
  a first page without the BOS flag, a multiplexed/chained stream (mixed serials), more
  chunks than the cap allows, and a **single indivisible unit** (one OGG page that alone
  spans more than the ceiling) all return `None`. The caller then raises the boundary’s
  existing deterministic refusal —
  `“The audio is Ns — exceeds the 300s speech-to-text bound and this container cannot be
  divided into shorter parts of the same format.”` (stage `media_validation`).
* **FLAC is the concrete refusal.** A FLAC frame boundary cannot be found without
  decoding subframes, and a re-headed FLAC would need STREAMINFO’s total-sample count
  and MD5 rewritten; a long FLAC is reported honestly instead of being approximated.
  This is a deliberate, documented limit, not a silent gap: **long FLAC behaves exactly
  as it did before this phase.**

### The operation around the division

`media_service._run_stt_chunked` owns the operation; the division itself runs off the
event loop (`asyncio.to_thread(stt_chunking.plan, …)`), the same pattern the boundary
already uses for document parsing:

1. the payload is validated once against the **total** ceiling, then (only if it
   exceeds one chunk) planned into ordered chunks;
2. chunks are transcribed **strictly in source order, one at a time**, always with the
   ONE engine the request selected;
3. each chunk’s transcript is normalized by the **existing**
   `_normalize_extracted_text` and appended to the ordered list;
4. the list is joined by `stt_chunking.join_transcripts` — **one newline between
   chunks, nothing else**: no bridging text, no inferred words, no translation, no
   summarization, no deduplication and no second model;
5. the single merged transcript then flows through the **unchanged**
   `_normalize_extracted_text` → `_cap_text(limit)` → `MediaAnalysis` path, so one
   request still produces ONE analysis, ONE owner-facing response and no intermediate
   chunk output.

Stages traced (content-free, `key=value`, the project’s existing trace shape):
`stt_chunks_planned`, `stt_chunk_invoked` (with the chunk index, the chunk count, the
byte count and the applied bound), `stt_chunk_returned` (with the character count, so
an EMPTY chunk is distinguishable from a failure), `stt_chunks_merged`, plus the
existing `stt_engine_invoked` / `stt_engine_returned` on the single-pass route.

### Bounds — per chunk, total, count, deadline

| Bound | Value | Enforced |
|---|---|---|
| Duration of ONE chunk | `MAX_STT_DURATION_S` = 300 s (unchanged constant, new meaning) | by the planner and by each chunk’s own granule span |
| Chunk count per request | `MAX_STT_CHUNKS` = 4 (new, explicit) | by the planner (`> max_chunks` ⇒ refusal) |
| Total duration per request | `MAX_STT_TOTAL_DURATION_S` = **`MAX_STT_CHUNKS × MAX_STT_DURATION_S`** = 1200 s (20 min) | by the duration guard, now applied against the TOTAL bound |
| Input bytes | `MAX_STT_INPUT_BYTES` = 20 MiB (**unchanged**) | before the transfer, as before |
| Aggregate recognition deadline | `STT_TOTAL_TIMEOUT_S` = 120 s (new) | one clock started before chunk 1 |
| Per-chunk call timeout | `min(STT_TIMEOUT_S, remaining aggregate)` | passed to `_run_stt` |

* The total bound is **derived, not inflated**: it is exactly the small explicit chunk
  cap times the existing per-chunk ceiling, so “300 s × a huge number” is impossible.
* The aggregate value is derived from two constants that already govern this request:
  the 240 s media envelope minus the 120 s an analytical media answer reserves for its
  provider call = 120 s for recognition — which is also exactly two per-chunk bounds.
* Temporary storage is unchanged: chunks are **in-memory slices of the already
  validated payload**, so there is no chunk file to write, leak or clean, and peak
  memory stays the payload plus ONE chunk (the plan holds byte ranges, not chunks).
* No new environment variable and no new persisted setting was introduced: these are
  architectural ceilings, not per-deployment behaviour.

### Timeouts — the three layers and their relationship

```
media request envelope        240 s   (media_ai_service.DEFAULT_ENVELOPE_S, the handler backstop)
  └── aggregate recognition   120 s   (STT_TOTAL_TIMEOUT_S)               ← new, one clock per request
        └── per chunk          60 s   (STT_TIMEOUT_S, or what is left of the 120 s)
              └── engine op    45 s   (Gemini STT_OPERATION_DEADLINE_S; its own for each other engine)
```

* Each chunk’s call stays bounded exactly as before; a three-pass configuration inside
  one chunk still runs under the engine’s own single operation deadline.
* **N chunks cannot multiply the request lifetime**: the aggregate clock is checked
  before every chunk, and a spent deadline fails the operation honestly
  (stage `media_stt_timeout`) instead of starting another chunk.
* The 120 s left outside the aggregate is what an analytical media answer needs for its
  provider call, so chunking cannot starve the answer step.

### Interaction with multi-pass / consensus, and provider selection

* No second consensus exists and none was added: each chunk goes through the SAME
  `transcribe` seam, so the engine’s configured pass count multiplies the calls **per
  chunk** (chunks × passes), never the other way around. With both caps in force the
  worst case is 4 × 3 = 12 provider calls, each still bounded by the engine’s own
  deadline and by the aggregate.
* The engine receives ONE bounded payload per call and cannot see a chunk boundary, so
  no provider adapter needed a change and none was made.
* **The selected engine stays authoritative**: every chunk uses it, a chunk failure
  fails the whole operation, and no fallback happens here (there is no health/fallback
  manager yet). The seam where a future per-chunk provider choice belongs is the single
  `engine` binding at the top of `_run_stt_chunked`.

### Zero-context and Telegram output

Unchanged and re-tested for the chunked route: the engine receives nothing but the
chunk’s audio bytes and the adapter’s existing configuration (no chat id, message id,
filename, caption, reply, history, memory or inferred target), `MediaAnalysis.
as_context_text()` still renders no Telegram metadata, and the delivery layer is
untouched — **one media request → one logical result → one controlled Telegram
representation** (M1.7). Internal chunking is never user-facing.

### Exact files changed

| File | Change |
|---|---|
| `backend/services/stt_chunking.py` | **new** — the deterministic OGG-page / RIFF-frame division, the chunk plan, and the ordered transcript join (standard library only) |
| `backend/services/media_service.py` | the per-chunk/total/count/deadline constants, `_validate_audio_payload(max_duration_s=…)`, the chunked branch in `_extract_audio_content`, and `_run_stt_chunked` |
| `tests/test_media_stt_chunking.py` | **new** — the focused regression suite |
| `IMPLEMENTATION_REPORT.md` | this section |

No other file was modified. `media_ai_service.py`, `media.py`, the provider adapters,
`ProviderManager`, the delivery layer, the panel/handler code, `requirements.txt`,
`render.yaml` and the Supabase schema are untouched.

### Tests added and changed

`tests/test_media_stt_chunking.py` (34 tests), grouped exactly as the phase’s risks:

* **SHORT** — ≤ 300 s is ONE engine call on the payload’s **own unchanged bytes**
  (`test_short_audio_is_one_call_on_its_own_unchanged_bytes`), and exactly 300 s stays on
  the single-pass route (`test_exactly_one_chunk_worth_of_audio_stays_on_the_single_pass_route`).
* **BOUNDARY** — 301 s starts the chunked route and yields two ordered chunks
  (`test_one_second_past_the_bound_starts_the_chunked_route`).
* **LONG** — 301/480/840/1200 s produce 2/2/3/4 chunks whose transcripts merge in
  strict source order (`test_a_long_recording_merges_its_chunks_in_source_order`);
  the plan’s own durations, the exact partition of the source’s pages
  (`test_the_chunks_partition_the_source_pages_exactly_once`), the page-only division
  (`test_a_long_note_is_divided_at_ogg_page_boundaries_only`), the frame-level WAVE
  division whose chunks are re-validated by `_validate_audio_payload`
  (`test_a_wav_is_divided_on_frame_boundaries_and_stays_valid_per_chunk`), and the
  verbatim byte reuse (`test_the_source_bytes_are_reused_verbatim_not_re_encoded`).
* **FAILURE** — the first, a middle and the last chunk each fail the whole operation
  with no partial transcript and no later attempt (`test_any_failing_chunk_fails_the_whole_operation`,
  parametrized `fail_at=1,2,3`), no retry on another provider
  (`test_a_failing_engine_is_never_retried_on_another_provider`), an indivisible
  over-long container and a long FLAC are refused honestly with no engine call.
* **OUTPUT** — one normalized value (`test_the_merged_transcript_is_one_normalized_value`),
  the existing ceiling’s honest truncation notice
  (`test_the_existing_output_ceiling_stays_honest_for_a_chunked_transcript`), and the
  zero-context rendering on the chunked route
  (`test_the_chunked_route_keeps_the_zero_context_rule`).
* **BOUNDS** — total duration refused before any transcription, the derivation asserted
  directly, the chunk cap enforced by the route (separately from duration), the spent
  aggregate deadline, and the per-chunk timeout formula.
* **RESOURCES** — the division runs off the event loop, no temporary artefact on success
  or failure, and cancellation propagates with cleanup.
* **MULTI-PASS** — a real `GeminiMediaEngine(stt_passes=3)` over a 2-chunk recording
  issues exactly `chunks × passes` = 6 interaction requests, uploads each chunk’s own
  bytes once, deletes each upload, and merges two chunk transcripts
  (`test_a_three_pass_engine_multiplies_per_chunk_and_stays_bounded`).
* **DEPENDENCY** — the splitter’s imports are asserted to be `__future__` + `typing`
  only (`test_the_splitter_is_standard_library_only`).

**No existing test needed to be changed.** Two of them still pin over-long audio being
refused — `tests/test_media_stt.py::test_audio_longer_than_the_duration_bound_is_refused_before_transcription`
and `tests/test_media_gemini_engine.py::test_a_container_longer_than_the_duration_bound_never_reaches_gemini`
(both 330 s) — and they now pass because their **minimal two-page fixture declares its
entire duration on its only audio page**, which is genuinely indivisible. They are
kept as the pinnacle of the refusal contract, and the positive chunking cases live in
the new suite.

### Validation results

| Run | Result |
|---|---|
| `tests/test_media_stt_chunking.py` | **34 passed** |
| Narrow media/STT suites (media processing, image OCR, document extraction, STT, dedicated STT, direct STT, multi-pass, reliability, engine, consensus, Groq, Speechmatics, provider probe, AI STT settings) | **1002 passed, 2 skipped** |
| Full suite, final tree | **3911 passed, 26 skipped** |
| Full suite without the new file (the same tree’s baseline) | **3877 passed, 26 skipped** — i.e. the 34 new tests are exactly the delta, and **no existing test changed** |
| `python -m py_compile` on both changed modules | clean |
| `git diff --check` | clean |

### Live verification status

**NOT PERFORMED.** No live Telegram voice note, no provider credential and no network
call was used; the fixtures are synthetic containers and the engines are scripted, so
nothing here claims the Persian recognition behaviour of any provider — and, as
`INVESTIGATION.md` §19/§20 already state, recognition QUALITY remains a live
measurement (`backend/tools/stt_benchmark.py`) rather than a property this phase can
assert.

### Intentionally NOT implemented

Provider fallback, provider health/cooldown, credential pools, per-chunk provider
selection, streaming/partial delivery, resumable transcription, FLAC division, MP3/
M4A/WebM support, chunk overlap, cross-chunk deduplication, LLM chunk merging,
transcript timing/diarization, TTS, and any change to OCR.

### Known limitations

1. **A chunk’s OGG granule positions are the source’s own absolute values** (and its
   page sequence numbers are the source’s), because rewriting them would mean
   recomputing the OGG page CRC — a variant the standard library does not provide
   (`zlib.crc32` is the reflected ISO-HDLC CRC) and which cannot be verified in this
   repository without a real Ogg fixture. The decoded audio of a chunk is exactly that
   chunk’s own packets; only the container’s *declared* duration reads as its position
   in the source. See the exact next stage.
2. **Codec header pages are the leading pages whose granule position is 0** — how every
   real Ogg Opus/Vorbis encoder writes the identification, comment and setup pages. An
   encoder that packed an audio packet onto a header page would have that one page
   re-emitted with each chunk (bounded, documented, and never a reason to cut a page).
3. **A single OGG page / a FLAC stream that spans more than one chunk cannot be
   divided** and is refused rather than approximated — hence the two existing 330 s
   tests still assert a refusal.
4. **A 3-pass configuration over 4 chunks can exhaust the 120 s aggregate deadline** and
   fail honestly rather than run unbounded. The default is one pass, where the aggregate
   is not the binding constraint for a 20-minute recording.
5. The total bound (20 min) is what four chunks of 300 s can cover; longer recordings
   need a delivery design the project does not have yet.

### Exact next stage

1. **Re-emit OGG chunk pages with normalized granule positions and renumbered sequence
   numbers**, which requires an OGG CRC-32 implementation plus a real Ogg Opus fixture to
   verify it against; that makes each chunk’s *declared* duration its own and removes
   limitation 1.
2. **Live verification** of a real long voice note end-to-end (one transcript, one
   message) — the provider, credential and account work an offline suite cannot do.
3. **FLAC division** only if a supported container truly needs it, and only with a real
   frame parser rather than a guess.

### Delivery

This phase starts from `b5e7c10`, the `main` tip that already equalled `origin/main`
when it began, and is delivered as ONE commit on top of it — no rebase, no force-push,
no new branch, and no unrelated file touched. The delivered commit is the tip of `main`
(`git log -1 --format=%H` re-verifies it).

---

## Previous phase — Media Processing M1.7: Video/GIF out of scope, and ONE controlled media response

Repository `Onlyicing1/Telegram-self-bot` · branch `main` · state as of 2026-09-18.

### Phase identity

| Item | Value |
|---|---|
| Phase name | **Media Processing M1.7** — explicit Video/GIF scope exclusion + single-message media delivery |
| Starting HEAD | `7e1e69a` `docs: record STT quality investigation findings` — equal to `origin/main` at phase start |
| Implementation commit | the single phase commit that contains this report (`feat: drop video/GIF from media processing, deliver one media response`) |
| Live Telegram verification | **NOT PERFORMED** |

### Purpose of this phase

Two focused corrections, both inside the existing Media Processing architecture and
nowhere else:

1. **Video and GIF are explicitly OUT of Media Processing.** The audit (below)
   found that a GIF really did enter the pipeline — it was downloaded and handed
   to the OCR engine — so the exclusion is not hypothetical.
2. **One media request produces ONE controlled Telegram response.** A single
   extracted document could previously be delivered as a burst of Telegram
   messages (one per rendered page). It is now delivered as exactly one message,
   or as ONE attached document when it cannot fit one message — never as a burst
   and never truncated.

Explicitly out of scope for this phase and NOT implemented: STT chunking, TTS,
Native Vision, new providers, credential pools, provider fallback, any change to
OCR/STT recognition quality, and any change to `MediaAnalysis` limits.

### Part 1 — what was found in the existing Video/GIF path

The audit traced every entry point for Video/GIF into the media pipeline:

- **One classifier.** `backend/ai/media.py::classify_message` is the single
  classifier and the only authority for a media type. Video/GIF reach
  `backend/services/media_service.py::analyze_media` labelled `Video`,
  `GIF` (an `image/gif` document) or `Animation` (`DocumentAttributeAnimated`) —
  Telegram's usual animated-image shape.
- **Video was already inert.** `video/mp4` is neither an extractable MIME nor an
  OCR/STT MIME, so a Video fell through to the generic
  `"No local extraction capability for Video (video/mp4)"` UNSUPPORTED result and
  was never transferred.
- **GIF was the real leak.** `"image/gif"` is listed in
  `OCR_IMAGE_MIME_TYPES`, and `provision_gemini_media_engines()` provisions the
  OCR engine on the live runtime, so `ocr_candidate = is_image_mime(mime) and
  ocr_available()` was `True` for a GIF: it was **downloaded and sent to the OCR
  engine**. `Animation` (`video/mp4`) was inert like Video.
- **A second, narrower leak.** A document carrying `DocumentAttributeVideo` whose
  MIME is `image/gif` is labelled `Video` but still matched the OCR capability by
  MIME alone, so it too reached OCR.
- Video/GIF are in `DOWNLOADABLE_MEDIA_TYPES`, so a video/GIF reply still
  resolves as a media target: the request reaches the media boundary rather than
  silently falling through to the LLM.

### How Video/GIF is now explicitly excluded

`backend/services/media_service.py`:

- New single authority: `UNPROCESSABLE_MEDIA_TYPES = frozenset({"Video", "GIF",
  "Animation"})` with the predicate `is_unprocessable(media_type)`.
- `analyze_media` refuses those types **immediately after** the
  `is_downloadable` check and **before every capability check** (OCR, STT, text,
  container extraction, the size gate and the transfer). The result is the
  boundary's existing honest `UNSUPPORTED` outcome via `_unsupported()`.
- The refusal is fully deterministic: it comes from the existing classifier's own
  label (Telegram metadata), never from model inference, and no regex was added.
- The owner-facing result is the existing convention —
  `media_ai_service.unsupported_text()` renders
  `⚠️ I can't process this Video yet.` plus the reason
  `Video is outside the Media Processing scope (video and GIF are not processed).`
  `answer_media_request` returns that answer **before** the provider branch, so
  `ProviderManager.chat` is never consulted for out-of-scope media.
- `DOWNLOADABLE_MEDIA_TYPES` was deliberately left unchanged: keeping Video/GIF
  "downloadable" is what routes a video/GIF reply into the media boundary and
  therefore to the deterministic refusal. Removing them would have made the
  request fall through to the ordinary LLM path, where the model would answer
  about media it cannot see — not a deterministic unsupported result.
- Ordinary media handling was not touched: no Video/GIF subsystem was added, no
  generic Telegram utility was deleted, and image/audio/PDF/DOCX paths are
  unchanged (pinned by tests).

### Part 2 — what caused the multi-message media output

`backend/ai/tools/delivery.py::deliver_response` paginates: `_format_chunks`
splits anything above `SAFE_LIMIT = 4000` UTF-16 units into pages, chunk 1 is
delivered with `event.edit(...)` and **every** subsequent chunk with
`event.reply(...)`.

The media boundary caps extracted text at
`MAX_EXTRACTED_CHARS = DEFAULT_MAX_CONTEXT_TOKENS (4000) × 4 = 16 000`
characters (`MAX_OCR_CHARS` and `MAX_STT_CHARS` are the same ceiling). One media
result could therefore render as up to five pages — one edited message plus up to
four `event.reply` messages generated automatically — which is the live
"six or more consecutive Telegram messages for one request" symptom.

Internal extraction chunking was **not** the cause. `_TextAccumulator` already
recombines PDF pages and DOCX blocks into ONE `MediaAnalysis.content`; the burst
was produced by the delivery layer paginating one logical result.

### The delivery change

`backend/ai/tools/delivery.py` — new `deliver_single_message()`, used only for
media answers:

1. The rendering is identical to the normal path (`process_output` →
   `format_presentation` → the durable provenance marker), so a result that fits
   one message is byte-for-byte the message the paginating path produced.
2. If `_format_chunks(...)` yields exactly ONE chunk, that chunk is edited into
   the request message exactly once (with the same reply fallback).
3. Otherwise the **COMPLETE** normalized result is sent as ONE attached
   `media-extract.txt` document, and the request message is edited into one
   deterministic notice: the character count, the attachment name, and an
   explicit "nothing was truncated or split". No page-by-page burst is emitted.
4. If the attachment itself cannot be delivered (no client / no resolvable peer),
   the existing paginating `deliver_response` is used as a last resort and logged —
   extracted content is never silently dropped.
5. Secondary notes (backup-model note, optional telemetry line) are normalized by
   the same renderer and ride with the delivered message, or with the notice in
   attachment mode, so neither mode swallows them.

`backend/bot/handlers/ai_unified.py` — `_is_media_result(result)` reads the
**existing** dispatcher stamp `result.metadata["ai_action"]["action"] ==
"media_analysis"` and routes only media answers to `deliver_single_message`
(passing the live client). Every other response — provider answers, tool results,
confirmations, failures, silent deletes — keeps `deliver_response` and its
pagination unchanged.

### Internal chunks vs. user-facing delivery

| Layer | Responsibility |
|---|---|
| `media_service` extractors | produce ONE normalized `MediaAnalysis.content` (bounded, `truncated` flag) |
| `media_ai_service` | turns that analysis into ONE `MediaAnswer` (direct STT or provider answer) |
| dispatcher | ONE `EngineResult` stamped `ai_action.action == "media_analysis"` |
| `deliver_single_message` | decides the ONE Telegram representation: one message, or one attachment + one notice |

No extractor, `MediaAnalysis` limit, OCR/STT provider call or zero-context rule
was changed to achieve this.

### Limits and edge cases

- The attachment name is the fixed `media-extract.txt`; the peer is the event's
  own `input_chat` when the event exposes one, else `event.chat_id`.
- The notice reports the **normalized character count**; the size test uses the
  same UTF-16 accounting as `SAFE_LIMIT`.
- The only case in which more than one Telegram message can still result is the
  attachment being undeliverable (logged, paginated fallback).
- No arbitrary message-count cap was introduced — the bound is representational
  (one message, or one attachment), not a count threshold.
- Media failures, the direct-STT path, the provider path, `ProviderManager`, and
  every `MediaAnalysis`/OCR/STT limit are unchanged.

### Tests added and changed

- **New:** `tests/test_media_scope_and_delivery.py` — 25 focused tests:
  the scope set is exactly `{Video, GIF, Animation}`; Video, both GIF shapes
  (`GIF` and `Animation`) and a `DocumentAttributeVideo` declaring `image/gif`
  are refused, never transferred and never reach a provisioned OCR/STT engine;
  the owner gets the deterministic refusal and the provider is never consulted;
  the dispatcher stamp routes media answers to the single-message path; photos
  still reach OCR, Voice still reaches STT, text/DOCX still extract and the
  PDF/downloadable taxonomy is unchanged; a short media result is one message, a
  many-piece internal extraction is ONE message, a large result is ONE attachment
  with the complete content and a one-line notice, an undeliverable attachment
  still delivers everything, an empty result stays a deterministic failure, and
  normal (non-media) delivery still paginates exactly as before.
- **Changed:** `tests/test_media_image_ocr.py` and
  `tests/test_media_gemini_engine.py` — the `image/gif` rows are now
  Sticker-labelled so the GIF **format** keeps its OCR-boundary coverage
  (signature corroboration, decoding, and the engine's undocumented-container
  guard) while the GIF **media type** is refused by the scope gate. The refusal
  itself is pinned in the new suite.

### Validation results

```
tests/test_media_scope_and_delivery.py ................ 25 passed
media suites (scope_and_delivery, processing, image_ocr, stt, stt_language,
              document_extraction, ai_integration, direct_stt, gemini_engine,
              transcribe_engine) ....................... 449 passed
full suite: pytest tests/ .......... 3328 passed, 24 skipped
(baseline before this phase: 3303 passed, 24 skipped → +25 tests, no test lost)
py_compile media_service / delivery / ai_unified ........ OK
git diff --check ........................................ clean
```

### Live verification status

**NOT PERFORMED.** Nothing here is a live observation: no Telegram request was
sent against this change, and the multi-message symptom is reproduced from the
code path (`MAX_EXTRACTED_CHARS` vs `SAFE_LIMIT` pagination), not measured live.

### Intentionally deferred

1. **Live verification** — reply to a video/GIF and confirm the deterministic
   refusal; send a large PDF/DOCX and confirm one attachment plus one notice.
2. **Video/GIF capability** — if it is ever wanted, `UNPROCESSABLE_MEDIA_TYPES` is
   the single place to change it; no other code knows about the scope.
3. **Reproducing the exact live six-message count** — the bound is derived from
   the code (`16 000` characters ÷ `SAFE_LIMIT`) rather than from a captured
   production transcript.

### Delivery

This phase was implemented on `7e1e69a`, but `origin/main` advanced by twelve
Speech-to-Text commits (`da05ace` … `c3d3e5e`) before the phase could be pushed.
The phase was therefore re-applied on top of `c3d3e5e` as ONE commit — no rebase,
no force-push, no new branch. The only conflict was this report, where both sides
had gained a "Latest phase": the STT phase keeps its section intact as
**Previous phase — M2.2.1**, and not a sentence of it was rewritten.

The media change is untouched by the re-application: over the six code/test files
this phase touches, `git diff c3d3e5e..HEAD` is byte-identical to
`git diff 7e1e69a..5264fea`. The delivered commit is the tip of `main`
(`git log -1 --format=%H` re-verifies it).

---

## Previous phase — M2.2.1: Speech-to-Text panel cleanup (compact control panel + nested STT Settings)

Repository `Onlyicing1/Telegram-self-bot` · branch `main` · state as of 2026-09-17.

### Current stage

M2.2.1 — a **presentation-only cleanup of AI → Media Analysis → Speech-to-Text**,
done before live testing. The screen is now a compact control panel instead of a
page of documentation: the verbose explanatory paragraphs are gone, the candidate
selection is a deterministic **two-column grid** built from the registry, and the
two bounded behavioral controls (language, recognition passes) moved one level
down into a new nested **⚙ STT Settings** panel so they no longer occupy the main
screen. **Nothing about behavior, persistence, callbacks, adapters, the probe,
the media boundary or the fallback status changed** — every action id, input id
and stored key is byte-identical to M2.2.

```
AI                                    (ai)
├── Media Analysis                    (ai_media)
│   ├── Text recognition (OCR)        (ai_media_ocr)   — unchanged (no OCR change)
│   └── Speech-to-Text                (ai_media_stt)
│       ├── Test all providers             → action:ai_stt_test_all          ← ONE global action (unchanged payload)
│       ├── two-column candidate grid      → action:ai_stt_select_candidate:<candidate-id>
│       └── ⚙ STT Settings                 → panel:ai_media_stt_settings     ← NEW navigation row
│           └── ⚙ STT Settings            (ai_media_stt_settings)            ← NEW nested panel
│               ├── Language…              → input:ai_media_stt:stt_language
│               ├── Recognition passes…    → input:ai_media_stt:stt_passes
│               └── ← Back / ⌂ Home        → panel:_nav:back / panel:_nav:home
├── Settings                          (ai_settings)      — still no STT controls
│   └── Advanced                      (ai_settings_adv)  — still no STT controls
└── … (provider, model, usage, health, details, diagnostics: unchanged)
```

| Item | Value |
|---|---|
| **Phase** | M2.2.1 — Speech-to-Text panel UI cleanup (compact panel + nested STT Settings) |
| **Starting HEAD** | `56762a9a6a27c49f814fa4e3d39efc034b026a29` — `feat(stt): make Speechmatics executable and add one global provider test` (== `origin/main`) |
| **Implementation commit** | the single commit of this phase (`git log -1 --format=%H` re-verifies it; recorded in the hand-off response) |
| **Database migration required** | **NO** — no schema change, no column, no table |
| **New environment variables** | **NONE** |
| **New dependencies** | **NONE** (`requirements.txt` untouched) |
| **Files changed** | **four** — `backend/bot/handlers/ai_stt_settings.py`, `tests/test_ai_stt_settings.py`, `tests/test_stt_provider_probe.py`, this report |
| **Behavioral changes** | **NONE** — presentation/navigation only; every callback and input payload is unchanged |
| **Fallback / health manager** | **NOT implemented** (next phase, unchanged) |
| **Live Telegram verification** | **NOT PERFORMED** (no session or traffic in this environment) |
| **Live provider verification** | **NOT PERFORMED** — neither `AI_GROQ_API_KEY` nor `AI_SPEECHMATICS_API_KEY` exists in this implementation environment; no provider is claimed healthy |
| **Recognition-quality claim** | **NONE** |

### Commit lineage

| Commit | Role |
|---|---|
| `01ff211` `feat(stt): add the bounded multi-pass STT accuracy seam` | M1.7e — the opt-in STT-only consensus seam |
| (M1.8 commit) | M1.8 — the owner-managed Gemini STT settings (`stt_model` / `stt_language` / `stt_passes`) and their runtime application |
| `4a1f9a2` `added web investigation report file` | the web research report |
| `8d3ba14` `feat(stt): establish the Speech-to-Text control plane and the Media Analysis surface` | M2.0 — the control plane |
| `9d63766` `feat(stt): add the Groq Whisper STT adapter, … and the provider test` | M2.1 — the Groq adapter, the resolver, the provider probe |
| `56762a9` `feat(stt): make Speechmatics executable and add one global provider test` | M2.2 — the Speechmatics adapter, the ONE global provider test, the secret declaration (**starting HEAD of this phase**) |
| the commit of this phase | M2.2.1 — the compact Speech-to-Text panel, the two-column candidate grid, the nested STT Settings panel and this report |

### Files changed by this phase

| File | Change |
|---|---|
| `backend/bot/handlers/ai_stt_settings.py` | the Speech-to-Text screen became a compact control panel; the candidate buttons are laid out in two columns; language/passes moved into the new `ai_media_stt_settings` panel; the long explanatory paragraphs were replaced by one short hint line |
| `tests/test_ai_stt_settings.py` | the new UI contract: one global test action, two-column grid + registry order, no settings control on the main screen, the nested panel and its values, unchanged payloads, secret/metadata hygiene, the new panel registration and Back navigation |
| `tests/test_stt_provider_probe.py` | updated to the shortened labels (via the handler's own button builder) and the shortened panel hint; the result notice still states it does not measure quality |
| `IMPLEMENTATION_REPORT.md` | this report |

**Untouched (deliberately):** every provider adapter (`groq_stt_engine.py`,
`speechmatics_stt_engine.py`, `gemini_media_engine.py`), `stt_engine_factory.py`,
`stt_control_plane.py`, `stt_provider_probe.py`, `media_service.py`,
`stt_consensus.py`, `config_store.py`, `backend/bot/handlers/ai.py`,
`backend/helper/**` (the shared panel/input/action registry and its navigation
stack are reused as-is), the provider/manager/dispatcher/tool layer, OCR,
Save / Tasks / scheduler / `RuntimeSupervisor`, `render.yaml`, `requirements.txt`,
`supabase/migrations/*.sql`, `DATABASE_ARCHITECTURE.md` and all secrets.

### The Speech-to-Text screen after this phase

| Aspect | Behavior |
|---|---|
| **State block** | `Active · <candidate>` / `Language · <language or Auto>` / `Passes · <N>`, then a numbered `Providers` list where each entry carries its own probe state and the active candidate is marked `· active`. Four lines of state, no prose |
| **One short hint** | a single italic line: `Test all = synthetic capability probe, not a quality benchmark.` — the full warning is repeated in the test result notice, so the short panel never implies a quality measurement. On the whole main screen there is at most ONE italic line and no line exceeds 80 characters (`test_the_stt_panel_is_compact` fails otherwise) |
| **Candidate grid** | deterministic two-column rows built from the candidate list in canonical registry order — a changed registry re-flows the rows with no hard-coded wrapping and never leaves a hole (only the final row may be short). Buttons are presentation-shortened (`Use Gemini` / `Use Gemini Transcribe` / `Use Groq v3` / `Use Groq Turbo` / `Use Speechmatics`) through a display-only label map whose **fallback is the registry label**, so a newly registered candidate is still offered without touching the table. The callback payload is the registered candidate id and is **never derived from the button text** |
| **Selection semantics** | unchanged — the active candidate gets no `Use` button, an unimplemented candidate gets none either, and a legacy/unresolved selection offers the whole implemented pool |
| **Global test** | exactly ONE `Test all providers` row, above the candidate rows, payload `action:ai_stt_test_all`, delegating to the existing `stt_provider_probe.test_candidates()` (no second loop in the handler), re-rendering the same panel **once** with one bounded line per candidate |
| **Nested settings** | `⚙ STT Settings` (`panel:ai_media_stt_settings`, parent `ai_media_stt`) holds `Language…` and `Recognition passes…` — the **same** registered inputs (`input:ai_media_stt:stt_language`, `input:ai_media_stt:stt_passes`), the same prompts, validation, storage keys and runtime application. It shows the current values compactly and reaches them through the shared navigation stack, so Back returns to Speech-to-Text and Home to the usual destination — no second navigation system |
| **Real states preserved** | a failed database read, an unresolved legacy model and a registered-but-unavailable active candidate are all still rendered (they are runtime facts, not documentation); no error was hidden for visual cleanliness |
| **No secrets / no metadata** | no label, body line or button names an environment variable, a credential, a transcript or a Telegram identifier (asserted by test) |

### Carried forward: the STT control plane as it stands (M2.0 → M2.2, unchanged by this phase)

The sections below describe the **current** operating architecture. This phase
changed none of it; they are kept here because they are the live contract behind
the panels above.

#### Global "Test all providers" behavior

| Aspect | Behavior |
|---|---|
| **Control** | exactly ONE button row, `Test all providers` → `action:ai_stt_test_all`, rendered **above** the candidate rows. No candidate has its own Test button, including the active one |
| **What it tests** | every **implemented** candidate, **sequentially**, in the registry's canonical order — the probe owns that order (`stt_provider_probe.test_candidates()`), so the handler adds no second loop |
| **Unimplemented candidates** | reported as `not available` and **never sent a request** |
| **Credential-less providers** | reported as `no credential` and **never sent a request**; credential presence is a separate state from a successful request and never a pass |
| **Rendering** | ONE notice listing every candidate's bounded state (`test passed · <ms>` / `no credential` / `test failed · <failure class>` / `not available`), rendered once on top of the same Speech-to-Text panel — never a message per provider |
| **Payload** | the probe's bounded in-process WAV (1 s, 16 kHz mono PCM16), validated against the media boundary's own audio contract. It contains no speech |
| **Quality claim** | none, and the notice says so: it is explicitly a capability probe that does not measure recognition quality |
| **Persistence** | none — process-local observations only; no `ai_config` write, no new column, no new table, and a restart returns every candidate to `not tested` |
| **Ordering determinism** | the registry order is a tuple literal, so the probe order is identical on every process |

#### Real replied-to audio for the probe — investigated, deliberately NOT added

The earlier task asked whether the existing architecture can safely feed a **real**
replied-to Voice/Audio message into the same bounded probe. It cannot, and no
mechanism was invented to force it:

* the global test is a **callback-query** action (`ActionHandler(event, extra,
  chat_id)`), and a button press carries no replied-to message — there is no
  deterministic media target in that scope at all;
* the only place a reply is available is the **pending-input** path, whose
  handlers receive the owner's *typed text*, not the replied-to media object;
  deriving audio from it would mean adding a new Telegram-context mechanism —
  exactly what that phase was told not to do.

So the global provider test remains a **bounded capability/transport probe**, it
says so on the panel and in its result, and **real Persian recognition
benchmarking remains a separate next step** (see Deferred work). The probe API
already accepts explicit `audio=` bytes, so that step can be built without
touching the adapters.

#### Speechmatics API integration summary

| Aspect | Decision |
|---|---|
| **API** | the official **batch v2 REST API** (`https://asr.api.speechmatics.com/v2`), verified against the published API reference and the vendor's own client source. Speechmatics transcribes **asynchronously**, so one recognition is a bounded job cycle: `POST /v2/jobs` → `GET /v2/jobs/{id}` until `done` → `GET /v2/jobs/{id}/transcript?format=txt`. No WebSocket, no management platform, no temporary-token exchange |
| **Authorization** | `Authorization: Bearer <API key>` on every leg. Never logged, never persisted, never placed in Telegram/Supabase; a provider error body is redacted before it can surface |
| **Request** | `multipart/form-data`: the `config` part is the documented JSON (`{"type":"transcription","transcription_config":{"language":…,"operating_point":…}}`) and the audio is the `data_file` part under a **static, non-identifying** name (`audio.ogg` / `audio.wav` / `audio.flac`), so an untrusted Telegram filename can never leak. No diarization and no extra output are requested |
| **Model mapping** | the registered candidate model IS the API's `operating_point` (`standard`), through an explicit table so an unregistered value can never reach the API |
| **Credential** | `AI_SPEECHMATICS_API_KEY` — the ONE declared secret. No compatibility alias was invented: the repository has no existing Speechmatics credential convention, so a second variable would have been a fabricated convention |
| **Audio input contract** | unchanged: the adapter receives the already-bounded, already-validated payload bytes from `media_service` (resolution, transfer, size/duration/channel/rate/MIME validation, cleanup and normalization all stay there). OGG/Opus, WAV and FLAC are accepted; anything else is refused locally before any request |
| **Language** | an explicit BCP-47 tag is reduced to the ISO-639-1 primary subtag the API documents (`fa-IR` → `fa`); empty means **automatic detection**, which this API spells with its own `language: "auto"` token — never a fabricated code. Nothing is translated, transliterated or forced to English; a Persian transcript returns in Persian script |
| **Recognition passes** | the owner's bounded count (`1..3`) reuses the **existing** STT-only consensus (`stt_consensus.reconcile_hypotheses`): sequential passes over the same audio, each one full job cycle, under ONE deadline. A pass is a recognition attempt, never a transport retry |
| **Bounds** | one engine operation deadline of **45 s**, inside the boundary's own `STT_TIMEOUT_S` (60 s) so the engine's precise reason always wins; connect/write/pool bounds derived from what is **left** of the deadline and re-derived for every leg; a bounded status poll; at most **2** sequential attempts and only for transient conditions |
| **Output ceiling** | `media_service.MAX_STT_CHARS` is applied in the adapter. Empty or unreadable provider output is a **failure**, never a successful empty transcription |

#### Failure classification

One closed token per failure SITE, attached to the raised `MediaError` and emitted
as the `failure_class` field of the adapter's own bounded trace line (with the
socket phase or HTTP status when one applies). The vocabulary is shared by both
adapters — `missing_credential`, `unsupported_model`, `auth`, `forbidden`,
`invalid_request`, `unsupported_audio`, `timeout`, `transport`, `rate_limit`,
`server`, `malformed_response`, `empty_transcription`, `provider_rejection`,
`operation_deadline` — so the provider probe reports the same tokens whichever
adapter failed.

Deterministic failures (a rejected credential or key, a refused payload or config,
an unreadable body, an empty transcript, a rejected/failed job) are **never**
re-sent. Transient ones (`timeout`, `transport`, `429`, `>= 500`) may repeat the
**submit** leg at most once while the deadline has room; a transient failure while
**waiting** for an already-submitted job only repeats the status read (at most
three consecutive times) and then fails — it never submits a second job, so a retry
can never silently double the translation work. Recognition quality is never
reclassified: a poor transcript is a successful provider response.

#### Current provider candidate states

| Candidate id | Provider | Execution | Credential | Test state |
|---|---|---|---|---|
| `gemini:default` | gemini | implemented (unchanged) | `AI_GEMINI_API_KEY` | not tested until probed |
| `gemini:gemini-3.5-transcribe` | gemini | implemented (unchanged) | `AI_GEMINI_API_KEY` | not tested until probed |
| `groq:whisper-large-v3` | groq | implemented (M2.1) | `AI_GROQ_API_KEY` (→ `GROQ_API_KEY`) | not tested until probed |
| `groq:whisper-large-v3-turbo` | groq | implemented (M2.1) | `AI_GROQ_API_KEY` (→ `GROQ_API_KEY`) | not tested until probed |
| `speechmatics:standard` | speechmatics | implemented (M2.2) | `AI_SPEECHMATICS_API_KEY` | not tested until probed |

Every registered candidate is selectable and testable. **An adapter existing is
not a health claim**: only a completed request that returned a non-empty
transcript is reported as passed, and none has been run here.

#### Resulting architecture

```
Telegram UI (AI → Media Analysis → Speech-to-Text → ⚙ STT Settings)
    ↓  a registered candidate id + language + passes
persisted owner configuration  (existing ai_config row, existing 3 keys)
    ↓
STT CONTROL PLANE  (backend/ai/stt_control_plane.py — configuration only)
    ↓                                    ↘
candidate → engine resolver               provider probe (on demand, bounded,
(backend/services/stt_engine_factory.py)  one request per candidate, registry order)
    ↓  gemini → GeminiMediaEngine · groq → GroqWhisperEngine(model)
       speechmatics → SpeechmaticsBatchEngine(operating_point)
the EXISTING media boundary seam (media_service.set_stt_engine)
    ↓
POST {base}/jobs → poll → GET /jobs/{id}/transcript?format=txt
```

The `SttEngine` protocol, `set_stt_engine()`, `get_stt_engine()` and
`stt_available()` are **unchanged**; the media service is unaware of Telegram UI
configuration; and no owner id, chat id, message id, sender, caption, filename,
reply text, history or memory can reach an adapter (verified by test: the seam
takes `bytes` and nothing else, and each engine holds no such state).
**Automatic fallback was not implemented in M2.2.1** — one selected candidate ran.
**M2.3 supersedes this sentence**: the selected candidate is still attempt 1 of
every request and the boundary still fails closed when no engine can be
provisioned, but a fallback-eligible failure now continues through the control
plane's own ordered candidates under a finite ceiling (see the M2.3 section).

#### Configuration / ENV behavior

* ENV holds **secrets only**. `AI_SPEECHMATICS_API_KEY` is declared in `render.yaml`
  with `sync: false`; `AI_GROQ_API_KEY` (already declared) is reused, never
  duplicated.
* No behavioral variable exists for either transcription provider — no
  `AI_*_STT_MODEL`, `AI_*_STT_LANGUAGE`, `AI_*_STT_PASSES` or an API-base
  override. Model selection, language and passes remain Telegram-controlled and
  persisted through the existing `ai_config` row.
* **Render ENV is not a database**: changing the STT candidate, language or pass
  count needs no redeploy and no restart.

### Tests and exact results

| Suite | Result |
|---|---|
| `tests/test_ai_stt_settings.py` (extended) | **`100 passed`** |
| `tests/test_stt_provider_probe.py` (updated) | **`57 passed, 2 skipped`** (the skips are the two opt-in live probes — no credential here) |
| `tests/test_groq_stt_engine.py` (unchanged, re-run) | **`90 passed`** |
| `tests/test_speechmatics_stt_engine.py` (unchanged, re-run) | **`92 passed`** |
| `tests/test_36_ai_settings_ux.py` (unchanged, re-run) | **`9 passed`** |
| the media / STT boundary suites (`test_media_stt.py`, `test_media_dedicated_stt.py`, `test_media_stt_reliability.py`, `test_media_stt_multipass.py`, `test_media_stt_language.py`, `test_media_stt_benchmark.py`, `test_stt_consensus.py`, `test_media_gemini_engine.py`, `test_media_ai_integration.py`, `test_media_direct_stt.py`, `test_media_processing.py`) | **`559 passed`** — the boundary's behavior is unchanged |
| **Full suite** | **`3852 passed, 26 skipped, 3 warnings` in 112.56 s** (`24` of the skips are pre-existing; the other `2` are the opt-in live provider probes. No test was deleted or weakened) |

New/updated coverage added by this phase: exactly one global test action and no
per-candidate Test button; the two-column candidate grid with the registry's
canonical order preserved and no hole except the last row; the active candidate
still absent from the grid and unimplemented candidates still excluded; the
shortened labels (the full candidate set is still offered); **no** settings
control on the main screen while the values stay visible; the `⚙ STT Settings`
navigation row; the nested panel's own title, current values, the two existing
input payloads and Back/Home rows; every callback and input payload unchanged;
Back from STT Settings landing back on Speech-to-Text through the shared
navigation stack; the new panel registered with parent `ai_media_stt`; the main
body staying under 500 characters with ≤ 80-character lines and at most one
italic line; the unavailable-active-candidate warning still rendering on the
compact screen (through a forged control plane, since every registered
capability currently executes); and no credential, transcript, Telegram
identifier or environment variable name appearing anywhere in either panel's
text or buttons.

**Syntax / whitespace:** `python -m py_compile` clean on every changed Python
file; `git diff --check` clean.

### Live verification status

* **Telegram:** NOT performed — the panels, grid, nested settings panel and
  navigation were exercised against the real handlers, the real registries and
  the real panel builders, but no Telegram session rendered them.
* **Groq API / Speechmatics API:** NOT performed — no credential in this
  environment, so no request was made and no reachability is claimed. The opt-in
  live test (`tests/test_stt_provider_probe.py`, skipped without a credential) is
  the safe path for an operator who has one: it uses the bounded probe payload,
  reports only state/model/elapsed/failure class, never prints or persists the
  key, and never treats a synthetic tone as proof of recognition quality.

No provider is claimed healthy merely because an adapter exists, and no
recognition-quality improvement is claimed because a provider answered.

### Intentionally NOT implemented

* **Automatic fallback / failover / cooldown / retry orchestration across
  providers** — deferred in M2.2.1 because the tested providers' Persian quality
  was still unmeasured. **Delivered in M2.3** (fallback never improves
  recognition quality; it only keeps a request from failing on one provider's
  transient fault).
* **Any health manager, failure counters, ranking or persistent health state** —
  observations stay process-local; no `ai_config` column, no Supabase table.
  **M2.3 keeps this**: its health/cooldown map is process-local and persisted
  nowhere.
* **Real Persian recognition benchmarking** — the probe payload is a synthetic
  tone and is never presented as a quality benchmark.
* **A real replied-to audio feed for the probe** (see above) — no new
  Telegram-context mechanism was invented to force it into this line of work.
* **Any behavioral change from this phase** — no adapter, resolver, control plane,
  probe, media boundary, persistence or callback was modified; this phase is
  presentation and navigation only.
* **Any change to the dispatcher, the tool layer, `ProviderManager`, the
  scheduler, `RuntimeSupervisor` recovery, the Supabase schema,
  `DATABASE_ARCHITECTURE.md`, `render.yaml`, `requirements.txt`, OCR or the media
  boundary's execution behavior.**
* **Any new dependency, any local Whisper/PyTorch/ONNX/ffmpeg stack, any second
  media download path or Telegram media boundary, and any behavioral ENV
  variable.**

### Known limitations

1. No automatic fallback: if the selected candidate fails, the media operation
   reports the classified failure; the next candidate is not tried (by design,
   next phase). **Resolved in M2.3** for fallback-eligible failures only — a
   deterministic failure still reports itself and is never cascaded.
2. Speechmatics transcribes **asynchronously**, so a job that outlives the 45 s
   engine deadline fails honestly with `operation_deadline` even though the
   provider might have finished later. Long audio on the Telegram side (the
   boundary admits up to 300 s) can therefore exceed the deadline; the control
   plane, not the adapter, decides which candidate to use.
3. `language: "auto"` is the provider's documented automatic mode; Speechmatics
   may **reject** a job whose language it cannot identify confidently, which
   surfaces honestly as `provider_rejection`.
4. Probe results are process-local by design; a restart forgets them, and
   `credential_missing` is reported independently of reachability.
5. The synthetic probe payload contains no speech, so a healthy provider reports
   `failed` + `empty_transcription` (or a provider-side no-speech rejection)
   until the probe is run with real-speech audio. This never claims quality and
   never fabricates a transcript.
6. The candidate button labels are shortened for the two-column grid; they are
   presentation only and the registry remains the single source of candidate
   identity. A label map that does not know a newly registered candidate falls
   back to its registry label, so no candidate can become unselectable because of
   a label.
7. The M1.8/M2.0 note stands: the `ai_config` STT columns still require the
   pending manual migration; until it is applied the settings degrade to the
   documented in-memory fallback, and `DATABASE_ARCHITECTURE.md` §7 still
   describes the superseded M1.8 semantics.
8. Recognition quality (class A) remains unmeasured and is not claimed.

### Deferred work

* The **STT health/fallback manager** that consumes these capabilities:
  active candidate → provider health → cooldown → next active candidate →
  bounded retry/failover → honest failure, in front of the existing seam —
  **delivered in M2.3** (`backend/services/stt_fallback.py`).
* **Real Persian recognition benchmarking** on 30–50 real voice messages across
  the registered candidates. M2.2.1 recorded this as the step that must come
  *before* automatic fallback; M2.3 was implemented first on explicit instruction,
  so benchmarking remains the open step — now with the additional purpose of
  choosing the rotation ORDER on measured evidence.
* Persisted (if ever wanted) health/cooldown state — deliberately absent today.
* Per-owner fallback re-ranking.
* Applying the pending `ai_config` migration and the documentation-only
  `DATABASE_ARCHITECTURE.md` §7 semantics refresh.

### Exact next stage, as recorded by M2.2.1 — live Telegram verification, then benchmarking (M2.3 was delivered as the fallback phase instead)

1. Open **AI → Media Analysis → Speech-to-Text** on the live account and confirm
   the compact panel, the two-column grid, the nested `⚙ STT Settings` panel and
   its `Language…` / `Recognition passes…` inputs render and persist as expected —
   this UI cleanup exists to make that walkthrough readable on a phone.
2. Run `Test all providers` with the deployment credentials in place to learn each
   candidate's transport/credential state (still not a quality measurement).
3. Feed **real** Persian voice notes through the SAME bounded probe
   (`stt_provider_probe.test_candidate(..., audio=<bounded bytes>)` already
   accepts explicit audio) and record per-candidate transcripts and timings
   **outside** the repository's deterministic suite.
4. Decide, on that evidence, which candidates are worth keeping active and in
   which order — the decision the control plane already models but deliberately
   does not act on.
5. Only after that, the ordered active → cooldown → fallback execution in front
   of the existing `media_service.set_stt_engine` seam (never a second STT
   pipeline), consuming the resolver and failure taxonomy already in place.
   **This item was delivered out of order in M2.3** (steps 1–4 above, the live
   credential walkthrough and the benchmarking, are still outstanding).

### Document version

This document reflects the PART 2 (and PART 1) state of the API Credential Vault.
The credential store is now **manageable from Telegram** as well as resolvable: an
owner-facing surface under **AI → Media Analysis → API Credentials** lists the
providers this build can actually execute, shows each provider's credentials with
their label, enabled state, priority and last bounded test, and supports add, rename,
enable/disable, deterministic priority moves, replace-key, a bounded key test and a
confirmed delete — behind ONE management boundary
(`backend/services/credential_service.py`) over five owner-scoped SECURITY DEFINER
functions, with Supabase Vault as the only place a raw key is ever stored and no
second secret store anywhere. **Neither vault migration has been applied**: Supabase
was not modified, no function and no Vault secret was created, and the two migrations
with their complete manual SQL (`DATABASE_ARCHITECTURE.md` §29) remain an owner
action. A key containing whitespace is refused on purpose, the only statements that
ever see a raw key are the two write functions, and no key can reach a log line, a
rendered panel, callback data or an ordinary database column. The M3.0
speech-synthesis state, the M2.4 credential pool, the M2.3 provider fallback and the
STT control plane all stand as recorded below, and **live Telegram verification, live
provider verification and real Persian recognition and synthesis benchmarking are all
still outstanding**. A history of the earlier phases follows unchanged. If code
changes invalidate any section, update this document in the same commit.

---

### Document version of the M2.4 phase (retained, superseded by the later phases above)

This document reflected the M2.4 state: Speech-to-Text lives under
**AI → Media Analysis** as a compact control panel — one global `Test all
providers` action, a deterministic two-column grid of REGISTERED candidates, and
a nested **⚙ STT Settings** panel holding only the bounded language and
recognition-pass controls. All five registered candidates (Gemini ×2, Groq
Whisper ×2, Speechmatics ×1) remain executable and testable through the ONE
bounded probe that never claims health or recognition quality. The control plane
owns the owner's selection; the candidate → engine resolution is one small seam
in front of the **unchanged** `media_service` STT boundary; and a new,
process-local execution layer (`backend/services/stt_fallback.py`) now keeps the
SELECTED candidate as attempt 1 of every request while falling back through the
control plane's OWN canonical order under a finite attempt ceiling and a shared
budget — so one provider's transient failure no longer fails the media operation
and no selection is ever silently rewritten. Resilience now has a second axis: a
bounded **credential pool** per provider (`backend/ai/credential_source.py` +
`backend/services/stt_credential_pool.py`) rotates a rejected, revoked, spent or
rate-limited key INSIDE the provider before the provider-level fallback is
engaged, while a provider with a single credential keeps its exact pre-M2.4
behaviour — and no raw key, selection or credential is ever written to Telegram,
to a log line or to the database. **The Supabase Vault RPC has NOT been applied** (PART 1, above, ships the
migration, the function and the documented SQL, but no SQL was executed and no
Vault secret exists), **and live Telegram verification, live provider
verification and real
Persian quality benchmarking are all still outstanding.** If code changes
invalidate any section, update this document in the same commit.
