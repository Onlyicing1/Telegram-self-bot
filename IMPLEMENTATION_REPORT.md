# IMPLEMENTATION REPORT — CURRENT STATE

## Latest phase — M1.8: Telegram-manageable Gemini STT settings (owner config, no redeploy)

Repository `Onlyicing1/Telegram-self-bot` · branch `main` · state as of 2026-09-17.

### Current stage

M1.8. The three BEHAVIORAL speech-to-text settings are now **owner settings**
persisted through the existing AI configuration path and edited from Telegram —
they are no longer deployment configuration that requires editing Render ENV and
redeploying.

| Setting | Stored key (`ai_config`) | Default (nothing configured) | Meaning of the default |
|---|---|---|---|
| Gemini STT model | `stt_model` | empty | the general media model answers the STT instruction |
| Gemini STT language hint | `stt_language` | empty | automatic language detection (no `language_codes` sent) |
| Gemini STT recognition passes | `stt_passes` | `1` | the existing single-pass route |

Reached from **AI → Settings**: one state line on the personal surface, the three
controls on the existing **Advanced** sub-panel. No new panel, category, store,
service or scheduler was created.

| Item | Value |
|---|---|
| **Starting HEAD** | `01ff211027e3b9c4f395189710c9ec08940f7742` — `feat(stt): add the bounded multi-pass STT accuracy seam` (== `origin/main`; working tree clean at the start of this phase) |
| **Implementation commit** | the single commit of this phase (`git log -1 --format=%H` re-verifies it; recorded in the hand-off response) |
| **Database migration required** | **YES** — three additive `ai_config` columns. Created as a migration file, **NOT executed**: applying it to the live Supabase project is a manual owner action. |
| **Accuracy claim** | **NONE.** This phase makes recognition behavior configurable. It does not make recognition more accurate, and nothing here measures quality. |
| **Live Telegram verification** | **NOT PERFORMED** (no session or traffic in this environment) |
| **Live Gemini verification** | **NOT PERFORMED** (every engine interaction in this phase was a scripted in-process transport) |

### Commit lineage

| Commit | Role |
|---|---|
| `5b3f6d7` `fix(stt): bound the dedicated transcription operation to one deadline` | M1.7c — the bounded transport (one operation deadline, `uri`-primary representation, classified failures, bounded retry/cleanup) |
| `01ff211` `feat(stt): add the bounded multi-pass STT accuracy seam` | M1.7e — the opt-in STT-only consensus seam (`stt_consensus.py`, `AI_GEMINI_STT_PASSES`) and the repeat-run benchmark. **starting HEAD of this phase** |
| the commit of this phase | M1.8 — the owner-managed STT settings, their persistence, their Telegram controls, their runtime application and this report |

### Files changed by this phase

| File | Change |
|---|---|
| `backend/ai/config_store.py` | the three keys in `_DEFAULTS` and in the `ai_config` upsert payload |
| `backend/services/gemini_media_engine.py` | `STT_SETTING_DEFAULTS`, `STT_LANGUAGE_AUTO`, `stt_settings_from()` (pure) and `apply_stt_settings()` (the ONE engine reconfiguration entry point) |
| `backend/runtime/supervisor.py` | `_provision_media_engines()` is now async and, after provisioning, installs the owner's persisted STT settings through the existing store (`_apply_persisted_stt_settings()`) |
| `backend/bot/handlers/ai_stt_settings.py` **(new)** | the three input handlers, their validation, the panel display helpers and the runtime hand-off |
| `backend/bot/handlers/ai.py` | one transcription state line on the personal Settings surface, the three rows on the existing Advanced panel, and lazy imports of the display helpers |
| `backend/bot/router.py` | registers the new module in the existing handler list |
| `supabase/migrations/20260917000001_add_ai_config_stt_settings.sql` **(new)** | the three columns, the pass-range CHECK, and rollback SQL — **pending manual application** |
| `DATABASE_ARCHITECTURE.md` | §7 column table, the §7 current-status note and a new §19.2b item (schema-change rules) |
| `tests/test_ai_stt_settings.py` **(new)** | 56 tests for defaults, persistence, validation, panels, inputs, runtime application, isolation and registration |
| `tests/test_36_ai_settings_ux.py` | the registration test now covers the new module too (9 unique inputs, same registry) |
| `tests/test_media_gemini_engine.py` | the supervisor-hook test now awaits the async provisioning hook |
| `IMPLEMENTATION_REPORT.md` | this report |

**Untouched:** `INVESTIGATION.md`, `media_service.py`, `backend/ai/media.py`,
`backend/telegram_api/media.py`, `media_ai_service.py`, `dispatcher.py`,
`ai_unified.py`, `stt_consensus.py`, `ProviderManager` and every adapter, the tool
registry/executor, the media boundary and target resolution, `MediaAnalysis`, the
Save/Task/Scheduler/OCR/vision paths, the panel infrastructure, `requirements.txt`,
`render.yaml`, `Procfile`, ENV files and all secrets.

### Why a separate handler module

`backend/bot/handlers/ai.py` is already ~78 KB; the three input handlers plus
their validation and the runtime hand-off are one cohesive unit and live in
`ai_stt_settings.py`, exactly as `ai_test_progress.py` exists for its own AI
sub-feature. The controls still attach to the EXISTING `ai_settings` panel via
the ONE input registry, and the module is registered alongside every other
handler module in `backend/bot/router.py` — no second panel, no second registry.

### Persistence

* Owner-specific: the values live on the owner's single existing `ai_config` row
  (`owner_id` UNIQUE), through `backend/ai/config_store.py` — the same store, the
  same upsert, the same defaults merge every other AI setting uses.
* Survives a process restart **when the `ai_config` column set exists** (below).
  Until the migration is applied, the whole upsert degrades to the documented
  in-memory fallback and the values are lost on restart — the pre-existing,
  already-documented behavior of any missing `ai_config` column, not a new mode.
* Nothing is stored in module globals as a source of truth, nothing in ENV, and
  no parallel settings dictionary was introduced.
* No API key is exposed through Telegram; keys stay environment-backed, and no
  panel label or prompt names an environment variable (asserted by a test).

### Database schema — migration REQUIRED and NOT executed

The existing `ai_config` schema could not store these three settings (they are
new typed behaviors, not arbitrary keys), so the smallest additive change was
made: three columns on the existing table. No new table, no new index, no RLS
change.

**MANUAL SUPABASE ACTION REQUIRED (idempotent — apply in the Supabase SQL editor):**

```sql
ALTER TABLE ai_config
    ADD COLUMN IF NOT EXISTS stt_model text;

ALTER TABLE ai_config
    ADD COLUMN IF NOT EXISTS stt_language text;

ALTER TABLE ai_config
    ADD COLUMN IF NOT EXISTS stt_passes integer NOT NULL DEFAULT 1;

ALTER TABLE ai_config
    DROP CONSTRAINT IF EXISTS ai_config_stt_passes_range;

ALTER TABLE ai_config
    ADD CONSTRAINT ai_config_stt_passes_range
    CHECK (stt_passes BETWEEN 1 AND 3);
```

**Rollback (the application keeps working on the defaults afterward; stored
values are lost):**

```sql
ALTER TABLE ai_config
    DROP CONSTRAINT IF EXISTS ai_config_stt_passes_range;

ALTER TABLE ai_config
    DROP COLUMN IF EXISTS stt_passes;

ALTER TABLE ai_config
    DROP COLUMN IF EXISTS stt_language;

ALTER TABLE ai_config
    DROP COLUMN IF EXISTS stt_model;
```

The repository copy of this migration is
`supabase/migrations/20260917000001_add_ai_config_stt_settings.sql`. **It was not
executed here and no claim is made that the columns exist in the live project.**
`DATABASE_ARCHITECTURE.md` was updated in the same commit because a real schema
change was required (§7 columns, §7 status note, §19.2b).

### The Telegram controls

| Surface | Content |
|---|---|
| AI → Settings (text) | `Voice transcription · <model|default model> · <language|auto> · <n> pass(es)`, or `Voice transcription · unavailable (database read failed)` when the durable read failed — the panel never reports a default as if it were the stored value |
| AI → Settings → Advanced (text) | `Voice transcription model · …`, `Voice transcription language · …`, `Voice recognition passes · n (single pass)` |
| AI → Settings → Advanced (buttons) | `Voice transcription model…` → `input:ai_settings:stt_model`; `Voice transcription language…` → `input:ai_settings:stt_language`; `Voice recognition passes…` → `input:ai_settings:stt_passes` |

Each input closes in ONE edit (the existing `_finish_input` contract: notice on
top of the refreshed Advanced panel), deletes the owner's reply, and on a failed
durable write shows the failure notice — the pattern the other AI settings use.

### Validation bounds

| Setting | Accepted | Rejected (refused, never clamped, nothing stored) |
|---|---|---|
| STT model | one opaque token, `^[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}$`; `reset`/`clear`/`default`/`none` → the default (empty) | empty, spaces, punctuation outside the allowed set, > 64 chars |
| STT language | a BCP-47 shape, `^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$` (e.g. `fa-IR`); `auto`/`reset`/`clear`/empty → automatic (stored empty) | `persian`, `fa IR`, `fa_IR`, `f`, `-IR` |
| STT passes | the integers `1`, `2`, `3` | `0`, `-1`, `4`, `9`, `2.0`, non-integers, empty |

The model is **not** validated against a registry: the project has none for
transcription models, and an unknown id is stored and passed through unchanged
(only the project's existing deprecation map may substitute a retired alias).
The pass count keeps the M1.7e safety bound (1..3, default 1) as an engine
invariant as well as a Telegram one, and the database CHECK enforces it too.

### Runtime configuration behavior (explicit precedence)

1. **Secrets** — the Gemini API key remains environment-backed
   (`resolve_api_key()`), unchanged.
2. **Behavior** — the three STT settings come from the owner's persisted
   `ai_config` row. The engine never reads the store and never learns an owner
   id: the caller (the supervisor at startup, the AI Settings handler after a
   save) reads it and passes plain values into `apply_stt_settings()`. The engine
   is therefore Telegram-agnostic, and a Telegram change is effective on the
   **next media operation** with no redeploy and no restart.
3. **Defaults** — used only when the owner has no configured value (empty
   model / empty language / one pass), which is byte-identical to the behavior
   before this phase.
4. **ENV is not the settings database** — `AI_GEMINI_STT_MODEL`,
   `AI_GEMINI_STT_LANGUAGE` and `AI_GEMINI_STT_PASSES` remain only as the
   deployment bootstrap (`build_gemini_media_engine`), and the persisted row
   supersedes them whenever it can be read (asserted by a test that sets all
   three ENV variables and shows the persisted values winning).
5. **Unreadable store** — a FAILED durable read is not treated as "no settings":
   the supervisor keeps the bootstrap configuration rather than silently
   downgrading a configured model because the database blinked.

Precedence summary:

```
behavioral STT value = persisted ai_config value
                       else (store unreadable) bootstrap ENV/default
                       else (nothing configured) documented default

credential = ENV only
```

### Context isolation (verified, not merely intended)

`stt_settings_from()` is pure over a mapping of the three keys and ignores
everything else, `apply_stt_settings()` accepts only that mapping, and the engine
holds exactly six slots (`_api_key`, `_model`, `_key_env_var`, `_stt_model`,
`_stt_language`, `_stt_passes`). Tests feed `owner_id`, `chat_id`,
`message_id`, `sender`, `username`, `caption`, `filename`, `history`, `memory`
and `reply_text` alongside the settings and assert the engine keeps only the
three values and that none of those strings is reachable from the engine. No
chat history, memory, sender, chat id, message id, filename, caption or reply
text can reach the transcription model.

### Tests and exact results

Focused (all green):

| Suite | Result |
|---|---|
| `tests/test_ai_stt_settings.py` (new, 56 tests) | `56 passed` |
| `tests/test_36_ai_settings_ux.py` | passed (registration now covers 9 unique inputs on the one registry) |
| `tests/test_media_gemini_engine.py` | passed (supervisor hook awaited) |
| `tests/test_media_dedicated_stt.py`, `tests/test_media_stt_reliability.py`, `tests/test_media_stt_multipass.py`, `tests/test_stt_consensus.py`, `tests/test_media_stt_benchmark.py`, `tests/test_media_stt.py` | passed |
| the eight media/settings suites together | `372 passed` |

The new suite covers: deterministic defaults; persistence of a custom model, a
custom language, and passes 1/2/3; reload; the upsert payload carrying all three
keys; the 1..3 bound; the `auto` alias; deprecation substitution vs. unknown-id
pass-through; the Advanced panel showing current values and defaults; the
personal panel's state line and its honest "unavailable" state; the three input
flows changing each value; refusal of malformed models/languages and of 0, −1,
4, 9, 2.0, non-integers; the live engine taking the persisted values; the
persisted values winning over ENV; fail-closed with no credential; context
isolation; the startup application and the unreadable-store degradation; and the
registration path (module list + one panel scope, no duplicate keys, no second
store, no `os.getenv`).

**Full suite:** `3569 passed, 24 skipped, 3 warnings` in 113.83 s
(pre-existing skips only; no test was deleted, weakened or skipped).

**Syntax / whitespace:** `python -m py_compile` clean on every changed Python
file; `git diff --check` clean.

### Live verification status

* **Telegram:** NOT performed. The controls were verified against the real
  handler functions, the real registry and the real panel builders, but no
  Telegram session rendered them.
* **Gemini:** NOT performed. The runtime application was verified against the
  real engine constructor and `media_service` seams; no request was sent.

Neither status may be reported as success.

### Known limitations

1. The migration is **pending manual application**; until then the settings
   degrade to the in-memory fallback (lost on restart) — the same degradation
   already documented for the trigger and `show_question` columns.
2. The startup log line (`GEMINI_MEDIA_ENGINE_PROVISIONED`) still reports the
   bootstrap model; the per-operation STT line and the panel report the effective
   values. (Unchanged from M1.7e's note about the pass count.)
3. `INVESTIGATION.md` is unchanged, so the recognition-quality question (class A)
   remains open and unmeasured.
4. Behavioral settings no longer depend on ENV, so the three `AI_GEMINI_STT_*`
   variables are bootstrap-only and should be treated as deprecated in
   documentation.
5. `main` and `origin/main` verification is recorded in the hand-off response;
   the migration cannot be verified from here at all.

### Deferred work

* Applying the migration to the live Supabase project (owner action).
* Deprecating/removing the `AI_GEMINI_STT_*` bootstrap variables from any
  operator documentation once the columns are live.
* Class A (recognition quality) measurement — `INVESTIGATION.md` §19 stays open.
* Design B (model-based reconciliation) — still deferred, still gated on live
  evidence.

### Explicit next stage — M1.7f: the live evidence pass for class A

Unchanged in substance, and now simpler to run:

1. Apply the migration (or accept the RAM fallback) and open **AI → Settings →
   Advanced** to confirm the three controls render and persist.
2. Leave the recognition passes at `1` and confirm the existing single-pass route
   is unchanged.
3. Run `python -m backend.tools.stt_benchmark --audio v.ogg --reference v.txt
   --passes 1,2,3 --repeat 3` on 5–10 real Persian voice notes, then set the pass
   count **from Telegram** (not from ENV) if — and only if — the numbers justify
   it.
4. Update `INVESTIGATION.md` §19 only from that measurement.

### Document version

This document reflects the M1.8 state: the three Gemini STT settings are
owner-persisted through the existing `ai_config`/`config_store` path, edited from
AI → Settings → Advanced, applied to the live STT engine without a redeploy, with
the bounded transport of M1.7c and the opt-in multi-pass seam of M1.7e unchanged
behind them. The three `ai_config` columns are pending manual application. If
code changes invalidate any section, update this document in the same commit.
