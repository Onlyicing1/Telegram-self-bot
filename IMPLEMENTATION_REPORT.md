# IMPLEMENTATION REPORT — CURRENT STATE

## Latest phase — TTS PART 1 — DURABLE SETTINGS PERSISTENCE: stored, recovered after a restart, and never faked from RAM

Repository `Onlyicing1/Telegram-self-bot` · branch `main`.

### Phase identity

| Item | Value |
|---|---|
| Phase name | **TTS PART 1 — DURABLE SETTINGS PERSISTENCE** — `tts_provider` / `tts_model` / `tts_voice` are persisted by a dedicated atomic write, recovered at startup, and never reported as stored when they were not |
| Type | persistence fix (write isolation + an honest read signal) + focused persistence tests + documentation synchronization — **no schema change** |
| Starting HEAD | `9765769` = `origin/main` (`fix(tts): bind provider model and voice selection to runtime`). The workspace entered this phase at `f9dfd9a` and was fast-forwarded `f9dfd9a..9765769` to `origin/main` before any edit — the ten commits already published (Save V2 management, the TTS provider work) are part of the starting state, and nothing was rewritten. |
| Implementation commit | `afbc5710f99fd1e8ea98d172356d7874dbda4ee3` — `fix(tts): persist provider model and voice settings` |
| Database | **NOT touched.** No SQL was executed, Supabase was not contacted, no production data was read or written. The TTS migration's **executable statements are byte-unchanged** — only its prose header was corrected. |
| STT / OCR / Save / Tasks / RuntimeSupervisor recovery / ProviderManager / provider adapters | **NOT touched** |
| Live Supabase verification | **NOT PERFORMED** — no Supabase connection is available in this environment, and executing SQL is explicitly forbidden for this phase |
| Live Telegram / live provider verification | **NOT PERFORMED** — no Telegram account was driven and no byte left the process |

### The observed problem

Selecting a provider, model or voice in AI → Media Analysis → Text-to-Speech
appeared to work, and the panel reported the new selection — but the three settings
were not durably stored, and they were gone after a restart.

### Exact root cause (verified against the current source)

There is no second store and no second table, and the control plane, the registry,
the engine factory and the credential pool are all already provider-scoped. The
failure is at the **schema boundary of the shared `ai_config` upsert**, and it has
two distinct halves — the second of which the previous phase's read-path patch
*concealed* rather than fixed:

1. `20260923000001_add_ai_config_tts_settings.sql` is documented as *pending manual
   application*, so the live `ai_config` table has **no** `tts_provider` /
   `tts_model` / `tts_voice` column.
2. `config_store._save_config_sync` built ONE upsert payload that named those three
   keys **together with every other AI setting**. PostgREST rejects a payload naming
   a column the table does not have, and it rejects the **whole statement** (42703:
   `column "tts_provider" of relation "ai_config" does not exist`). So a missing TTS
   column destroyed durability for `provider`, `model`, `temperature`,
   `system_prompt`, `trigger_en`, `trigger_fa`, `show_question` and the STT
   settings **as well** — a blast radius the TTS keys had introduced.
3. The exception was caught and the whole config was kept in `_fallback_config`
   (RAM), so the selection worked for the rest of the process and vanished on
   restart.
4. The previous phase then made `get_config` answer a key the row does not carry
   from that RAM value. That removed the wrong-provider symptom but left the value
   **indistinguishable from stored state**: every consumer — the panel, the runtime
   startup hook, the status screen — read a value that no database had accepted, and
   nothing could tell the two apart.

So the settings were "correct in the UI but not durable", and the failure was
invisible. Both halves are fixed here: the write is isolated so it cannot damage
anything else and cannot be damaged by a schema gap, and the read path labels a
RAM-served value as exactly that.

### What changed (application)

`backend/ai/config_store.py`

* **`TTS_STORAGE_KEYS`** — the trio, declared once as the group that is stored
  separately (a test pins it against `tts_control_plane.STORAGE_KEYS`).
* **`_save_tts_sync` / `save_tts_settings(owner_id, provider, model, voice)`** — ONE
  atomic statement naming only `tts_provider` / `tts_model` / `tts_voice` (+ the
  timestamps), on the owner's existing `ai_config` row. It returns `True` only when
  the durable row accepted the write; `False` means session-only and is never
  presented as durable. The three keys are written together, so a stored triple is
  always one consistent combination.
* **The shared payload no longer names a TTS column.** `_save_config_sync` still
  writes the row (and so `save_config` keeps its exact meaning: *the durable
  `ai_config` row was written*, which every existing honesty test depends on), and it
  then delegates the trio to the isolated statement when the incoming config carries
  it. A missing TTS column can now fail only that one statement.
* **`SESSION_ONLY_KEY`** — `get_config` reports every key it had to answer from the
  in-process fallback because the row does not carry it. It is present only when
  there is such a key (an all-durable config is unchanged), it is never persisted and
  never part of `_DEFAULTS`, and it is the read-side half of "RAM is not a store".

`backend/bot/handlers/ai_tts_settings.py`

* `persist_selection` writes the triple through `save_tts_settings` and reports the
  **real durable outcome**.
* `_selection_is_session_only` + one panel line: the control panel says
  `! This selection is not stored — it is lost on restart.` instead of implying the
  selection is stored. The existing `_outcome` notice already refused to announce an
  unwritten selection as accomplished.

The startup path is unchanged and already correct: `RuntimeSupervisor` calls
`_apply_persisted_tts_settings` → `config_store.get_config(self.owner_id)` →
`apply_tts_settings_async`, so the persisted triple is in effect from the first
synthesis, and an unreadable store keeps the applied selection instead of dropping a
configured provider.

### Schema and migration status

* **No schema change and no new migration.** `20260923000001_add_ai_config_tts_settings.sql`
  is correct and is **preserved**: three additive, nullable, default-less `text`
  columns, no CHECK (the registry is the authority on its tokens), RLS unchanged, no
  second table and no second configuration store. Only its **prose header** was
  corrected — it used to describe the whole-upsert degradation this phase removes.
  Its executable statements are byte-identical, so §31.3 part 6 still matches the
  migration statement for statement.
* **Migration status: still PENDING MANUAL APPLICATION.** This phase does not and
  cannot apply it. The subject/table/column/type/default/nullability/ownership/RLS
  contract is unchanged and still documented in §7 (`ai_config`) and §31.3 (part 6 of
  6), and the columns remain compatible with every existing `ai_config` row (additive,
  nullable, no backfill).
* **Honest statement of what is now durable:** with the columns present, the
  persistence path is proven end to end (write → read-after-write → simulated
  restart → runtime startup). With them **absent**, the live database still cannot
  store the trio — what changed is that this is now *contained to the trio* and
  *reported* (session-only), instead of silently taking every other AI setting down
  with it and presenting RAM as stored state. The owner still has to apply the
  migration for durability on the live project.

### DATABASE_ARCHITECTURE.md synchronization

* **ONE authoritative comprehensive executable SQL block — confirmed and preserved.**
  §31.3 carries exactly one complete deployment block (the repository test
  `test_the_document_carries_exactly_one_setup_block` and
  `test_no_second_complete_deployment_block_exists` pin it, and a direct check counts
  **1** complete block), and it **already contains the TTS settings columns** as
  **part 6 of 6**. **No second "final SQL" block was added, and no second block
  exists.**
* The canonical snapshot trio (`supabase/canonical_bootstrap.sql`,
  `20260920000001_reconcile_canonical_schema.sql`, the §31.3 part 1 embed) is
  **byte-identity unchanged** — verified after the edit — and the documented rule
  "a later schema change arrives as its own additive successor rather than by editing
  the snapshot" is respected.
* §7 (`ai_config`) was updated to synchronize the document with the code: it now
  lists the three TTS settings among the columns the base migration does not create,
  and records the persistence contract (dedicated TTS statement; the shared payload
  deliberately names no TTS column; `SESSION_ONLY_KEY` for RAM-served keys).

### Tests added

`tests/test_tts_settings_persistence.py` — **23 tests** over the real chain
(selection action → real `config_store` → a PostgREST-shaped `ai_config` table → real
`tts_control_plane` → real `tts_service` → the real `RuntimeSupervisor` startup hook),
with the column sets **derived from the §31.3 block** rather than typed, and a fake
table that reproduces PostgreSQL's semantics (an unknown column is 42703, `UPDATE`
merges, `INSERT` establishes the row, `owner_id` is not-null):

1. provider persistence · 2. model persistence · 3. voice persistence ·
4. read-after-write (the store, not the handler's candidate, is the authority) ·
5. simulated restart recovers the triple with RAM emptied · 6. runtime startup loads
it · 7. an explicit provider never reverts to the compiled OpenAI default ·
8. the stored triple is valid for its provider (all four providers) ·
9. a provider change leaves no incompatible model or voice · 10. a model change
leaves no incompatible voice and reports the degradation · 11. the trio is written in
ONE isolated statement naming no other column · 12. no other `ai_config` write
carries a TTS key · 13. schema compatibility: every column the store writes is
created by the authoritative setup block, which creates the TTS columns ·
14. the store and the control plane name the same three keys · 15. with the migration
pending, the rest of the configuration still persists (the regression this phase
fixes) · 16. a pending migration is reported session-only, by the store and by the
panel · 17. a session-only selection is lost on restart · 18. a failed write is
reported and never presented as stored · 19. a failed read is never reported as a
stored default · 20. startup keeps the applied selection when the read fails.

Two existing suites were updated because they modelled a write as a whole-row
replacement, which is no longer faithful now that a save performs two statements:
`tests/test_tts_multi_provider.py` (its fake store now receives the dedicated trio
write) and `tests/test_ai_presentation_redesign.py` (its fake table now merges on
`UPDATE` the way PostgREST does, and its single-payload assertion now pins the shared
settings payload plus the isolation of the TTS statement).

### Test results

| Scope | Result |
|---|---|
| `tests/test_tts_settings_persistence.py` | **23 passed** |
| TTS regression (`test_tts_multi_provider`, `test_tts_settings_persistence`, `test_tts_provider_binding`, `test_tts_control_plane`, `test_tts_provider_fallback`) | **214 passed** |
| Documentation / canonical schema (`test_database_setup_order`, `test_canonical_schema_reconciliation`, `test_tts_settings_persistence`) | **78 passed** |
| AI config surfaces (`test_ai_presentation_redesign`, `test_ai_stt_settings`) | **172 passed** |
| Full suite (`pytest tests`) | **4978 passed, 26 skipped** (117.85 s) |
| `py_compile` on the changed Python files · `git diff --check` | clean |

### Manual Supabase action required

Apply the TTS settings migration — idempotent, additive, no data change. Either run
the migration file, or simply run the **ONE §31.3 setup block**, which embeds it as
part 6 of 6:

```sql
ALTER TABLE ai_config ADD COLUMN IF NOT EXISTS tts_provider text;
ALTER TABLE ai_config ADD COLUMN IF NOT EXISTS tts_model    text;
ALTER TABLE ai_config ADD COLUMN IF NOT EXISTS tts_voice    text;
NOTIFY pgrst, 'reload schema';
```

Until it is applied the selection stays session-only, and the panel says so. No SQL
was executed by this phase.

### Live verification status

**No live database persistence was verified, and none is claimed.** The persistence
contract is covered by tests that exercise the real configuration path against a
PostgREST-shaped table for both the pre-migration and the post-migration schema, but
no statement of the migration was run against any Supabase project, and no Telegram
or provider call was made.

### Explicitly out of scope (untouched)

Persian TTS quality · audio/codec conversion · ffmpeg · Telegram voice-message
delivery · STT · OCR · Save · any unrelated TTS architecture.

---

## Previous phase — TTS PROVIDER/MODEL/VOICE BINDING FIX: the selected provider is the provider that speaks

Repository `Onlyicing1/Telegram-self-bot` · branch `main`.

### Phase identity

| Item | Value |
|---|---|
| Phase name | **TTS PROVIDER/MODEL/VOICE BINDING FIX** — an explicit provider selection now binds to the runtime adapter, the model and voice selectors, the credential pool and the executed request |
| Type | bug fix (persistence read path + an honest panel notice) + regression tests — **no schema change, no architecture change** |
| Starting HEAD | `d8c6a4f` = `origin/main` (working tree clean on entry) |
| Implementation commit | the single phase commit that contains this report (`git log -1 --format=%H` re-verifies it) |
| Database | **NOT touched.** No SQL executed, no migration added or changed, Supabase unmodified, `DATABASE_ARCHITECTURE.md` unmodified. The source audit proved the EXISTING migration (`20260923000001_add_ai_config_tts_settings.sql`) already represents provider/model/voice, so application code was fixed instead. |
| STT / OCR / Save / Tasks / RuntimeSupervisor / ProviderManager / adapters | **NOT touched.** The three provider adapters, the registry, the engine factory, the credential pool and the fallback were audited and found **already correctly provider-scoped** — they are unchanged. |
| Live Telegram verification | **NOT PERFORMED** — no live account was driven in this environment |
| Live provider verification | **NOT PERFORMED** — no live API key was used and no byte left the process |

### The observed bug

In a live Telegram test the owner switched Text-to-Speech to **Speechmatics**: the
panel reported Speechmatics, yet generation still behaved as OpenAI and the model
selector still listed OpenAI's `gpt-4o-mini-tts`. Treated as a functional binding
failure, not a cosmetic one.

### Exact root cause

Not in the registry, not in the factory, and not in any adapter — all of those are
provider-scoped and were verified as such. The failure was in the **persistence
read path**, and the chain is fully deterministic:

1. `ai_tts_settings._ai_tts_select_action` resolves a *valid* Speechmatics triple
   and calls `persist_selection` → `config_store.save_config`.
2. `_save_config_sync` builds ONE upsert payload that names
   `tts_provider` / `tts_model` / `tts_voice`. On the live database those columns
   do **not** exist yet (`20260923000001_add_ai_config_tts_settings.sql` is
   documented as *pending manual application*), so PostgREST rejects the **whole
   upsert**: `column "tts_provider" does not exist in "ai_config"`.
3. The exception is caught, the value is written **only** to the in-process
   `_fallback_config`, and `save_config` returns `False` — which `persist_selection`
   handed to a caller that ignored it.
4. `config_store.get_config` — the ONLY read used by the panel, by
   `apply_tts_settings_now` and by supervisor startup — returned
   `{k: row.get(k, v) for k, v in _DEFAULTS.items()}` whenever a durable row
   existed. The owner's row exists but carries no TTS key, so **every read yielded
   `tts_provider = ""` → the compiled default → OpenAI**.
5. `tts_service.apply_tts_settings` therefore installed **OpenAI** as `_selected`,
   `_engine()` → `build_engine` → the OpenAI adapter, and the credential pool
   resolved **OpenAI** credentials. The model panel, which re-reads the store, listed
   OpenAI's model — symptom 4 of the report.
6. The success notice was built from the **candidate** the handler had computed, not
   from any re-read state, so the UI kept claiming Speechmatics — symptom 1.

Two sources of truth: the notice read the computed candidate, the runtime read the
store. The store silently reported the default.

### Investigation answers (the 18 requested, traced callback → adapter)

| # | Finding |
|---|---|
| 1-4 | provider/model/voice persist to `ai_config.tts_provider` / `tts_model` / `tts_voice`; selecting Speechmatics stored `("speechmatics", "", "sarah")` — **in RAM only**, the durable write was rejected |
| 5 | the callback payload is the registry's own provider token (`action:ai_tts_select:<provider>`); model and voice are resolved from THAT provider in the same write |
| 6 | the control plane reads exactly those three keys via `parse_tts_config` → `resolve` |
| 7-9 | ONE registry, `tts_control_plane`; the model panel iterates `get_provider(selection.provider).models`, the voice panel `selection.model_entry.voices` — **both already provider-scoped**; they showed OpenAI only because the read said `provider = ""` |
| 10 | `tts_engine_factory.build_engine_for` dispatches on `entry.provider` after re-validating model+voice against the registry — correct |
| 11-13 | OpenAI is not hard-coded in the runtime; it is the DEFAULT, and it overrode the persisted value only because that value never reached a read |
| 14-15 | model and voice selection are provider/model-scoped, not global — verified by the registry API and its tests |
| 16 | fallback is NOT invoked for a healthy selected provider; it needs a runtime failure first (unchanged) |
| 17 | `tts_credential_pool` resolves per provider from each adapter's own `API_KEY_ENV_VARS` — correct; it resolved OpenAI only because the selection was OpenAI |
| 18 | the request carried the OpenAI model/voice because `_selected` was the OpenAI selection |

### What was fixed

* **`backend/ai/config_store.py`** — `get_config` no longer discards an explicit
  owner selection when the row cannot carry it. A key the row **carries** is still
  authoritative (unchanged); a key **absent from an existing row** — a setting this
  deployment has no column for — now falls back to what this process last wrote,
  then to the compiled default. Deliberately scoped: with **no row at all** the
  compiled default is still returned, which preserves the pre-existing contract
  pinned by `test_toggle_failure_is_honest_neither_silent_nor_false_success`
  (a failed write must never be reported as durable state).
* **`backend/bot/handlers/ai_tts_settings.py`** — `_outcome(saved, …)` makes the
  notice honest: when `persist_selection` did NOT write the durable row, the panel
  says the choice is active for this session and lost on restart, instead of
  announcing an unsaved selection as an accomplished fact.

Nothing else changed: registry, factory, credential pool, fallback, the three
adapters, STT and the schema are untouched.

### Credential resolution and fallback (audited, unchanged)

* **Credentials are provider-scoped**: each adapter declares its own
  `API_KEY_ENV_VARS`, the pool reads them through `adapter_for()`, and a credential
  of one provider is never handed to another. It had been resolving OpenAI only
  because the selection resolved to OpenAI.
* **Fallback semantics were already correct and are unchanged**: the selected
  provider always attempts first; a substitute is attempted only after a runtime
  failure classified as eligible; and a fallback NEVER rewrites the persisted
  provider — the configuration stays Speechmatics and the panel keeps showing it,
  while the resulting clip honestly reports the provider that actually spoke.

### Tests added

**`tests/test_tts_provider_binding.py` — 46 tests**, covering all 21 required
scenarios (model-list per provider ×4, model/voice clearing on switch, persistence
round-trip, factory resolution ×4, credential scoping ×3, no-silent-fallback,
fallback-does-not-rewrite, cross-provider model and voice rejection, UI option
derivation, execution path, and the end-to-end case).

The suite refuses to mock the seam where the bug lives. It runs the **real**
`config_store` against a PostgREST-shaped fake `ai_config` table, then the real
control plane, real registry, real factory, real credential pool, real
`tts_service` and the real provider adapter. Only two doubles exist: the table's
**column set** and the outbound HTTP transport. Every test runs **twice**:

* `with_tts_columns` — the schema after the existing migration is applied;
* `live_no_tts_columns` — the schema as it is today (migration pending), i.e. the
  state the live Telegram test actually ran against.

The seed row models a real owner whose `ai_config` row predates the TTS payload, so
the reproduction is the observed one rather than a synthetic empty-store case.

**Load-bearing proof:** reverting only the one read-path expression
(`row.get(k, v)`) makes the end-to-end test fail with the exact reported symptom —
`AssertionError: assert 'openai' == 'speechmatics'` — and it passes with the fix.

### Verification

| Check | Result |
|---|---|
| New binding suite `test_tts_provider_binding.py` | **46 passed** (both schema states) |
| STT regression (`test_stt_*`, `test_ai_stt_settings`, `test_media_direct_stt`) | **405 passed, 2 skipped** — STT untouched and unaffected |
| TTS regression (`tests/test_tts_*.py`) | **427 passed** |
| Credential/vault + database-docs + AI settings UX | **232 passed** (the "no second credential store", secret-leak and migration-order pins all held) |
| **Full suite** | **4955 passed, 26 skipped** |
| `py_compile` on every changed file | clean |
| `git diff --check` | clean |
| Files changed | **3** — `backend/ai/config_store.py`, `backend/bot/handlers/ai_tts_settings.py`, new `tests/test_tts_provider_binding.py` |

### Live-verification status (not claimed)

* **No live Telegram run and no live provider request were performed** in this
  environment. Every adapter call in the tests uses a scripted HTTP transport.
* The binding is proven at the application layer against the real production chain,
  including the real schema shape; it is **not** claimed as verified on a live
  account or a live provider.
* Persian remains unverified for every voice (unchanged from previous phases).

### Remaining requirement — durability

The fix makes an explicit selection **take effect immediately and stay effective
for the process**, and makes the panel say so honestly. It does **not** make the
setting survive a restart, because the columns still do not exist. Applying the
existing idempotent migration
`supabase/migrations/20260923000001_add_ai_config_tts_settings.sql` (also §31.3
part 6 of 6 of the ONE setup script) is a **manual owner action** — SQL was
deliberately not executed by this agent. Once applied, the same path becomes a
normal durable write and the "Saved only for this session" notice disappears on
its own.

## Previous phase — MULTI-PROVIDER TTS: Gemini, Grok and Speechmatics are real adapters, not registry entries

Repository `Onlyicing1/Telegram-self-bot` · branch `main`.

### Phase identity

| Item | Value |
|---|---|
| Phase name | **MULTI-PROVIDER TTS** — real provider adapters behind the EXISTING TTS control plane (`gemini`, `grok`, `speechmatics`), completing the provider layer the previous phase left registry-only |
| Type | runtime feature (three new provider adapters + the registry/`factory`/pool seam they plug into) + tests + report — **no schema change** |
| Starting HEAD | `c0b7dcc` `feat(tts): add provider selection and credential fallback` (= `origin/main`) |
| Implementation commit | the single phase commit that contains this report (`git log -1 --format=%H` re-verifies it) |
| Database migration required | **NO new migration.** The previous phase's `supabase/migrations/20260923000001_add_ai_config_tts_settings.sql` (three `ai_config` settings columns) is **unchanged and still NOT EXECUTED by this agent** — the owner must apply it (idempotent; §31.3 part 6 of the ONE setup script). Wiring more providers needs no new column: the provider/model/voice triple already exists. `DATABASE_ARCHITECTURE.md` was **not touched**. |
| TTS control plane | **NOT redesigned.** The registry, the credential pool, the bounded fallback, the Telegram panel, the settings persistence and the AI `SpeakTool` are the previous phase's, unchanged in shape. |
| STT / OCR / Vision / Video-GIF / Save V2 / Dispatcher / ProviderManager / ToolExecutor / RuntimeSupervisor | **NOT touched.** |
| Live provider verification | **NOT PERFORMED** — no live provider request was made; no real API key was used and no byte left the process (every test scripts the HTTP transport). |
| Live Telegram verification | **NOT PERFORMED** — no live account was driven in this environment. |

### The rule this phase enforces

A provider counts as **implemented** only when its own adapter exists AND the
application can construct and **invoke** it through the existing control plane.
Existing in a registry, appearing in the UI, carrying model/voice metadata, being
covered by a mock and being called "registered" are each **insufficient**. Every
provider below is now implemented by that definition: each has an adapter module,
a `build_engine` entry point, and a `tests/test_tts_multi_provider.py` case that
drives the REAL adapter end-to-end through `tts_service.synthesize` and asserts the
returned clip's provider, model, voice, MIME type and file name.

### Provider API audit — findings recorded (the required per-provider answers)

Each finding below is the reason the adapter is shaped the way it is; the adapter
docstring is the same record, beside the code it describes.

**Google Gemini (`gemini`) — IMPLEMENTED**

1. Offers TTS: **yes** — the Gemini API documents text-to-speech generation.
2. API: `POST https://generativelanguage.googleapis.com/v1beta/interactions` with an
   `"response_format": {"type": "audio"}` request; the response is a *completed
   interaction* whose `model_output` step carries the audio block.
3. Auth: the `x-goog-api-key` header.
4. Models (registered): `gemini-3.1-flash-tts-preview` (default), `gemini-2.5-flash-preview-tts`,
   `gemini-2.5-pro-preview-tts` — a **closed** allowlist; an unregistered string is refused.
5. Voices: the **30 documented prebuilt voices** (`Kore` first, the provider's own
   single-speaker examples use it), also closed.
6. Output: **raw PCM** (24 kHz, 16-bit, mono). No container is returned.
7. Telegram voice message: PCM is not a voice-note container, so the adapter wraps the
   provider's bytes in a **RIFF/WAVE header written with `struct`** — a 44-byte header,
   **not a transcode**. The clip is a self-describing `audio/wav` file Telegram carries as
   audio; the registry marks it ``voice_note = not_documented`` so the surface states the
   delivery caveat instead of promising a voice note.
8. Persian: the provider's own docs list Persian (`fa`) among its TTS models' languages →
   recorded as ``not_verified`` **with that evidence**, never promoted to `verified`
   without a live request.
9. Limitations: no model-parameter-per-voice; a preview-tier service; WAV delivery for a
   voice note is unproven live.
10. ffmpeg/heavyweight layer: **none** — `struct` only, no new dependency.

**xAI Grok (`grok`) — IMPLEMENTED**

1. Offers TTS: **yes** — xAI documents a Text to Speech service.
2. API: `POST https://api.x.ai/v1/tts`; the accepted body **is** the audio (not a JSON
   envelope, unless timestamps are requested — this adapter never requests them).
3. Auth: `Authorization: Bearer <key>`.
4. Models: the endpoint exposes **no model parameter**, so the registered model is the
   explicit empty string — the provider's own default route, **never an invented model name**.
5. Voices: the **28 documented ids** (`eve` first — the provider's documented default),
   closed.
6. Output: **MP3** (the provider's documented default written out explicitly at
   24 kHz / 128 kbps), requested through the documented `output_format` object.
7. Telegram voice message: **yes** — MP3 is a documented voice-note container, so the bytes
   are delivered as-is; registry ``voice_note = yes``.
8. Persian: **not in the provider's documented 20-language list** (it documents additional
   languages "with varying accuracy") → ``not_verified`` with that evidence; a live request
   is required to establish it.
9. Limitations: no model selection; language is set to the documented `auto` (the adapter
   never guesses the language of the owner's text).
10. ffmpeg/heavyweight layer: **none** — MP3 delivered unchanged.

**Speechmatics (`speechmatics`) — IMPLEMENTED (the previous phase's deferral, corrected)**

1. Offers TTS: **yes** — the Speechmatics TTS preview service.
2. API: `POST https://preview.tts.speechmatics.com/generate/{voice}?output_format=wav_16000`
   with body `{"text": …}`; the response is the audio file itself.
3. Auth: `Authorization: Bearer <key>` — the **same** credential the Speechmatics STT
   adapter declares (`AI_SPEECHMATICS_API_KEY`), so one credential serves both directions.
4. Models: no model parameter → the explicit empty string (the provider's own route).
5. Voices: the **4 documented ids** (`sarah`, `theo`, `megan`, `jack`), closed.
6. Output: **`wav_16000`** — a *complete WAV file with headers* (16 kHz, 16-bit signed,
   mono), sent explicitly so the request cannot drift with a provider-side default change.
7. Telegram voice message: a complete WAV clip is delivered as-is; the registry records
   ``voice_note = not_documented`` (`audio/wav` is not one of the documented voice-note
   containers) so the surface states the caveat.
8. Persian: the provider's own documentation states it supports **English** and all four
   voices are English (UK/US) → ``unsupported`` **with the accent recorded beside each voice**.
9. Correcting the deferral: the previous phase deferred Speechmatics because it read WAV as
   unusable for voice notes. The provider's `wav_16000` is in fact a **self-describing WAV
   file**, which is a container Telegram carries, so the adapter is implemented rather than
   left deferred — the earlier reasoning is corrected here, not carried forward.
10. ffmpeg/heavyweight layer: **none** — WAV delivered unchanged.

**OpenAI (`openai`) — unchanged, still the default**

The pre-existing adapter (`POST {AI_OPENAI_BASE_URL}/audio/speech`, model
`gpt-4o-mini-tts`, 13 documented voices, `opus` in an OGG container) is untouched and
remains the default selection. It is the only provider whose output Telegram documents
as a voice note, which is why the default selection is still OpenAI.

### Providers, models and voices now registered

| Provider | Registered | Implemented | Models | Voices | Container | Voice-note |
|---|---|---|---|---|---|---|
| `openai` | YES | **YES** | `gpt-4o-mini-tts` | 13 | `opus` / `audio/ogg` | **yes** |
| `gemini` | YES | **YES** | 3 (`gemini-3.1-flash-tts-preview` default) | 30 | `wav` / `audio/wav` | not_documented |
| `grok` | YES | **YES** | `` (provider default route) | 28 | `mp3` / `audio/mpeg` | **yes** |
| `speechmatics` | YES | **YES** | `` (provider default route) | 4 | `wav_16000` / `audio/wav` | not_documented |

`provider_ids()` is the deterministic tuple `(openai, gemini, grok, speechmatics)` and
equals `implemented_provider_ids()`. **The registry is derived from the adapters**, not
re-typed: each provider's models, voices, output format and MIME type are read from the
adapter module that owns them, and the registry build **asserts** the two agree (it raises
if `VOICE_ORDER` and `SUPPORTED_VOICES` diverge, or if `VOICE_ORDER[0]` is not
`DEFAULT_VOICE`), so an offer the adapter would reject can never be rendered.

Persian is **never overclaimed**: no voice on this build is `verified`. Gemini is
`not_verified` (documented `fa`, no live verification recorded), Grok is `not_verified`
(no documented Persian), Speechmatics is `unsupported` (documented English-only).

### What was implemented

* **`backend/services/gemini_tts_engine.py` (new)** — the Gemini speech adapter: ONE
  `httpx.AsyncClient` POST, closed model/voice sets, the documented `x-goog-api-key`
  header, base64 audio extraction from the completed interaction (last audio block wins —
  never a concatenation that could interleave two blocks), and the PCM→RIFF/WAVE header
  wrap. Credential variables are the ones the repository **already** declares for Gemini
  (`AI_GEMINI_API_KEY` / `GEMINI_API_KEY`), pinned equal to the media engine's by a test.
* **`backend/services/grok_tts_engine.py` (new)** — the xAI Grok adapter: ONE POST to
  `/v1/tts`, the 28 documented voices, the documented `output_format` object at its own
  documented default, `language: auto`, and the raw response body as the audio. Credential
  variables follow the project's `AI_<PROVIDER>_API_KEY` convention (`AI_XAI_API_KEY` /
  `XAI_API_KEY`).
* **`backend/services/speechmatics_tts_engine.py` (new)** — the Speechmatics adapter: ONE
  POST to `/generate/{voice}?output_format=wav_16000`, the 4 documented voices, WAV body,
  and the **shared** `AI_SPEECHMATICS_API_KEY` credential.
* **`backend/ai/tts_control_plane.py`** — the registry now derives every provider's
  capability from its adapter through ONE `adapter_for(provider)` seam
  (`_ADAPTERS`: openai→`openai_tts_engine`, gemini→`gemini_tts_engine`,
  grok→`grok_tts_engine`, speechmatics→`speechmatics_tts_engine`). An unknown provider token
  raises at registry-build time, so a provider can never be registered without an execution
  path. Added `implemented_provider_ids()`, the `VOICE_NOTE_MIME_TYPES` set and the derived
  `TtsModel.voice_note_compatible`. The deferral mechanism (`implemented=False` → never
  selectable) remains, exercised by a test double.
* **`backend/services/tts_engine_factory.py`** — ONE bounded dispatch over the registered
  providers, adding the `gemini`, `grok` and `speechmatics` branches to the existing
  `openai` one. The model and voice are re-validated against the registry here too, so a
  foreign model/voice can never reach any request body.
* **`backend/services/tts_credential_pool.py`** — `env_var_names()` and
  `registered_providers()` now resolve through the control plane's ONE provider→adapter
  seam (never a hard-coded list, never an environment sweep) and are filtered to
  **implemented** providers.
* **`backend/services/tts_service.py`** — additive: `describe()` reports the selected
  container's `voice_note` capability (`yes` / `not_documented`).
* **`backend/services/credential_service.py`** — the owner-facing label map gains
  `grok` → "Grok (xAI)", so the API Credentials surface names the new provider.
* **`backend/bot/handlers/ai_tts_settings.py`** — the panel states the delivery caveat when
  the selected container is not a documented voice note.

### Verification

| Check | Result |
|---|---|
| Cross-provider: `test_tts_multi_provider.py` | **53 passed** (new — real registry, real factory seam, real adapters; only HTTP and the secret backend are doubles) |
| Adapter: `test_tts_gemini_engine.py` | **55 passed** (new) |
| Adapter: `test_tts_grok_engine.py` | **43 passed** (new) |
| Adapter: `test_tts_speechmatics_engine.py` | **40 passed** (new) |
| Control plane: `test_tts_control_plane.py` | **50 passed** (updated: the "unimplemented provider" cases now use a test double because every real provider is implemented) |
| Fallback: `test_tts_provider_fallback.py` | **42 passed** (updated: the rotation mechanism is pinned with an explicit provider set; the full four-provider rotation lives in the multi-provider suite) |
| Regression: `test_tts_service.py` + `test_tts_openai_engine.py` | **98 passed** (none deleted or weakened) |
| **All TTS suites** (`tests/test_tts_*.py`) | **381 passed** |
| Credential/vault + STT pool + database docs + AI settings + direct STT | **368 passed** ("no second credential store" and the secret-leak pins held) |
| **Full suite** | **4909 passed, 26 skipped** |
| `py_compile` on every changed/new Python file | clean |
| `git diff --check` | clean |

`requirements.txt` is **unchanged**: the adapters use `httpx`, already pinned at `0.27.0`.

### Limitations

* **No live provider request was made**, so no real synthesis through Gemini, Grok or
  Speechmatics is claimed, and no real Persian audio is claimed. Every adapter is exercised
  against a scripted HTTP transport that reproduces the provider's documented response shape.
* **Voice-note delivery for Gemini and Speechmatics is unproven live.** Both produce WAV
  containers the registry marks `voice_note = not_documented`; a live send is required to
  confirm Telegram's treatment of a WAV clip.
* **No voice is `verified` for Persian.** Gemini records its documented `fa` support without
  claiming it; Grok and Speechmatics remain `not_verified` / `unsupported`.
* The three `ai_config` TTS settings columns still require the owner to apply
  `20260923000001_add_ai_config_tts_settings.sql`; until then the selection degrades to the
  in-memory fallback and is lost on restart.
* Credential health remains process-local (by design, matching the STT pool).

### Deferred work / explicit next stage

* Apply `20260923000001_add_ai_config_tts_settings.sql` (idempotent; §31.3 part 6 of 6).
* Run a **live** verification per provider: store a credential in the API Credentials
  surface, select the provider/model/voice from Telegram, and confirm one real clip.
  Record whether the selected voice actually speaks Persian (that is what would move a
  voice from `not_verified` to `verified`), and confirm the actual Telegram treatment of
  the Gemini and Speechmatics WAV containers for voice-note delivery.
* Unchanged from earlier phases: Native Vision, Video/GIF, provider benchmarking, the
  `ai_preferences` decision and the dead-column cleanup decision.

## Previous phase — TTS CONTROL PLANE: provider/model/voice selection, credential pool, bounded fallback

Repository `Onlyicing1/Telegram-self-bot` · branch `main`.

### Phase identity

| Item | Value |
|---|---|
| Phase name | **TTS CONTROL PLANE** — provider selection, model selection, voice selection, credential-pool integration and bounded provider fallback for Text-to-Speech |
| Type | runtime feature (control plane + credential pool + fallback layer + provider seam + Telegram surface + startup apply) + tests + one additive schema migration |
| Starting HEAD | `462258b` = `origin/main` (working tree clean on entry; no unrelated changes to preserve) |
| Implementation commit | the single phase commit that contains this report (`git log -1 --format=%H` re-verifies it) |
| Database migration required | **YES — one additive migration `supabase/migrations/20260923000001_add_ai_config_tts_settings.sql`**, which is also **§31.3 part 6 of 6** of the ONE setup script. **NOT EXECUTED** — the SQL is not run by this agent and must be applied by the owner (idempotent). Rollback is in the migration header. |
| STT | **NOT reimplemented, not touched.** No STT file, engine, provider, chunking, consensus, pool, fallback or test was modified. |
| Live provider verification | **NOT PERFORMED** — no live provider request was made (see below) |
| Live Telegram verification | **NOT PERFORMED** — no live account was driven in this environment |

### Source audit — what actually existed before this phase

| Inspected | Finding |
|---|---|
| `backend/services/tts_service.py` | the ONE TTS boundary: input/output bounds, the closed failure taxonomy, ONE awaited provider call, the normalized `SpeechClip`. Provider resolution was **hard-coded to one module** (`openai_tts_engine.build_engine(SPEECH_MODEL)`) and the voice was **fixed** to `alloy` |
| `backend/services/openai_tts_engine.py` | the ONE speech adapter: `POST {AI_OPENAI_BASE_URL}/audio/speech`, model allowlist `(gpt-4o-mini-tts,)`, 13 documented voices (a `frozenset`, i.e. **no deterministic order**), credential resolution from `AI_OPENAI_API_KEY` / `OPENAI_API_KEY` |
| `backend/bot/handlers/ai_tts_settings.py` | a **read-only** screen that explicitly said *"No owner controls"*; it registered no action and no input, and persisted nothing |
| `backend/ai/config_store.py` | one per-owner `ai_config` row; `_DEFAULTS` merge + one explicit upsert payload. It carried `stt_model`/`stt_language`/`stt_passes` — and **no TTS key at all** |
| `backend/ai/stt_control_plane.py` + `backend/services/stt_*` | the pattern this phase mirrors: a registry of registered candidates, a credential pool over the one secret boundary, and a bounded provider + credential rotation — all **STT-specific** and untouched |
| `backend/ai/credential_source.py` | the **provider-agnostic** secret boundary (environment first, then ONE documented Vault RPC `api_credential_pool`). Its own docstring already names `openai` (TTS) as a consumer |
| `backend/services/credential_service.py` | the owner-facing credential store; `registered_providers()` derived the TTS provider from `tts_service.describe()`, and `refresh_provider()` always reloaded the **STT** pool |
| `supabase/migrations/` + `DATABASE_ARCHITECTURE.md` §30/§31 | `ai_config` had **no** TTS column; the documented rule is that a later schema change arrives as **its own additive migration**, and §31.3 is the ONE executable setup script |
| `tests/test_tts_service.py`, `tests/test_tts_openai_engine.py` | 56 + 47 tests covering the boundary, the adapter and the read-only surface |

Missing exactly: provider selection, model selection, voice selection, credential-pool integration, provider fallback, and a settings surface that persists any of them.

### Providers, models and voices actually supported

| Provider | Registered | Implemented | Model | Voices |
|---|---|---|---|---|
| `openai` | YES | **YES** | `gpt-4o-mini-tts` (the adapter's own `SUPPORTED_MODELS`) | the adapter's 13 documented voices, in a new deterministic `VOICE_ORDER` (`alloy` default first) |
| `speechmatics` | YES | **NO — DEFERRED** | the provider exposes **no** model parameter (its own default route, so the registry stores an empty model id rather than inventing a name) | the four voices its documentation lists (`sarah`, `theo`, `megan`, `jack`) |

The registry is the single authority on `provider → model → voice`, and its voice
list is derived from the adapter's own allowlist, so the two cannot drift. A
registered-but-unimplemented provider is offered as **information only**: it can
never be selected, and the panel says *"not available yet"*.

**Why `speechmatics` is deferred (evidence, not preference):** its documentation
states the output is `wav_16000`/`pcm_16000` **only**, which Telegram voice notes
cannot carry (this project has no audio-transcoding path by deliberate policy — no
ffmpeg), and its own FAQ states the service supports **English** only. Wiring it
would have required either a conversion architecture or a non-voice-note delivery,
so it is registered as a documented deferral instead of being invented.

### Persian status — explicitly NOT claimed

Every voice carries a closed capability state: `verified` / `unsupported` /
`not_verified`. On this build **no voice is `verified`** (no live verification is
recorded), the OpenAI voices are `not_verified` (the provider publishes no
per-language guarantee), and the Speechmatics voices are `unsupported` **with the
provider's own documentation as the recorded evidence**. The panel renders the
state — *"Persian · not verified — live request required"* — so "Persian supported"
is never printed for a capability that was not verified.
**Live provider verification required.**

### What was implemented

* **`backend/ai/tts_control_plane.py` (new)** — the capability registry
  (`TtsProvider` → `TtsModel` → `TtsVoice`), the closed Persian states, the
  deterministic `canonical_order` (the selection first, then the registered
  implementations), and `TtsSelection` resolution. An invalid stored triple is
  degraded **level by level** (provider → its default model → that model's default
  voice) and the degradation is reported, so a stale combination can never reach a
  provider; the Telegram writes always persist a **consistent** triple.
* **`backend/services/tts_credential_pool.py` (new)** — the CREDENTIAL half:
  deterministic order (environment first, then the Vault in the owner's priority
  order), per-credential health with a bounded cooldown, and the
  credential-vs-provider classification. It is **not a second store**: it reads the
  same `credential_source` boundary and therefore the same Vault RPC and the same
  environment variables. No `db.rpc(`, no `vault.*`, no secret column.
* **`backend/services/tts_engine_factory.py` (new)** — the ONE provider → engine
  seam. It re-validates the model and the voice against the registry, passes only
  a **pooled** credential explicitly (the deployment's own credential keeps the
  adapter's existing resolution and its truthful `key_env_var` label), and returns
  a bounded reason instead of substituting another provider.
* **`backend/services/tts_fallback.py` (new)** — the bounded attempt plan: the
  selected provider **always first**, then at most `MAX_PROVIDER_ATTEMPTS = 3`
  providers and `MAX_TOTAL_ATTEMPTS = 6` attempts **sharing ONE deadline**, with a
  `MIN_ATTEMPT_S = 8s` floor so a starved attempt is never started. Rotation is
  deterministic, never random; a credential-specific failure rotates **inside** the
  provider, a provider-wide failure skips the rest of its keys, and only genuinely
  transient classes (or the adapter's own `retryable` verdict) authorize a
  substitute.
* **`backend/services/tts_service.py`** — the boundary now resolves the
  **provisioned selection** through the factory, drives the plan under the same ONE
  awaited timeout, and reports the engine that **actually produced** the clip (after
  a fallback that is the substitute, never the selection). Added one closed
  taxonomy token (`fallback_exhausted`) and the settings-apply entry points
  (`apply_tts_settings` / `apply_tts_settings_async`). No retry loop, no second
  deadline, no new dependency.
* **`backend/services/openai_tts_engine.py`** — additive only: `VOICE_ORDER` (the
  same allowlist as an ordered tuple; the control plane asserts they are equal).
* **`backend/bot/handlers/ai_tts_settings.py`** — the read-only screen became a
  compact control panel: the selection, its output format, its Persian state, the
  credential pool's bounded counts, and the input limit; plus **Model…** and
  **Voice…** screens and one button per selectable provider, and a link to the
  existing **API Credentials** surface (credential management is not duplicated).
  Every choice is a registered `panel:`/`action:` target — never typed input, never
  an environment-variable name.
* **`backend/ai/config_store.py`** — three keys on the existing per-owner row
  (`tts_provider`, `tts_model`, `tts_voice`), empty = the default selection, written
  in the one explicit upsert payload.
* **`backend/runtime/supervisor.py`** — `_apply_persisted_tts_settings()` runs at
  startup beside the STT apply; an unreadable durable read keeps the default
  selection instead of dropping a configured provider.
* **`backend/services/credential_service.py`** — discovery now derives the TTS
  providers from the **registry** (so the credential UI does not change when the
  selection moves), the environment-presence check knows the TTS provider's own
  variables, and `refresh_provider()` reloads the pool that actually serves the
  provider (an STT provider keeps its STT pool).
* **`supabase/migrations/20260923000001_add_ai_config_tts_settings.sql` (new)** —
  three nullable `ai_config` columns, additive, idempotent, no CHECK (the registry
  is the authority on the tokens), with the rollback in its header. It is embedded
  as **§31.3 part 6 of 6** so the ONE setup script stays complete.

### Settings UI / AI integration

`AI → Media Analysis → Text-to-Speech` shows the current provider · model · voice,
the output format, the Persian state, the pool counts and the limit; it offers
provider buttons (registered, implemented, not current), a **Model…** screen (only
this provider's models) and a **Voice…** screen (only this model's voices). A
selection change persists a **consistent** triple and is applied to the live
boundary immediately — **no redeploy, no restart** — and a provider switch can
never leave the previous provider's model or voice persisted.

The AI path is **unchanged and stays text-only**: `SpeakTool` still accepts exactly
one bounded `text` argument, with a test pinning that no provider, model, voice,
credential, endpoint or URL field exists — the model cannot choose any of them, and
the execution layer remains authoritative.

### Verification

| Check | Result |
|---|---|
| Focused: `test_tts_control_plane.py` | **48 passed** (new) |
| Focused: `test_tts_provider_fallback.py` | **42 passed** (new) |
| TTS regression: `test_tts_service.py` + `test_tts_openai_engine.py` | **103 passed** (2 superseded pins updated, none deleted or weakened) |
| Credential/vault: `test_credential_management.py` + `test_credential_vault.py` + `test_stt_credential_pool.py` | passed (the "no second credential store" pin was **strengthened**: the TTS pool must reuse the one boundary and must not own storage) |
| Database docs: `test_database_setup_order.py` + `test_canonical_schema_reconciliation.py` | **55 passed** (the ONE block now pins six parts) |
| STT regression (all STT/media/AI-settings suites) | **1687 passed, 3 skipped** |
| **Full suite** | **4716 passed, 26 skipped** |
| `py_compile` on every changed Python file | clean |
| `git diff --check` | clean |

### Limitations

* The Speechmatics TTS provider is **deferred** (WAV-only output; English-only
  voices) — registered as information, never selectable.
* Only ONE provider is implemented, so the provider-fallback axis is exercised by
  the registry plus test doubles; the machinery, its bounds and its classification
  are real and covered, but a second **real** provider does not yet exist.
* No live provider request was made, so no synthesis of real Persian (or any)
  audio is claimed; every engine in the suite is scripted.
* The migration is **not applied**: until the owner runs it, the three settings
  degrade to the in-memory fallback and are lost on restart, exactly as documented
  for the other pending columns.
* Credential **health** is process-local and resets on restart (by design, matching
  the STT pool).

### Deferred work / explicit next stage

* Apply `20260923000001_add_ai_config_tts_settings.sql` (idempotent; §31.3 part 6
  of 6) against the live Supabase project.
* Then run a **live** verification: store an OpenAI credential in the API
  Credentials surface, select a voice from Telegram, and confirm one real voice
  note; store a second credential and confirm rotation; record whether the
  selected voice actually speaks Persian (which is what would move a voice from
  `not_verified` to `verified`).
* A second real TTS provider remains deferred pending an output format Telegram
  voice notes can carry.
* Unchanged from earlier phases: Native Vision, Video/GIF, provider benchmarking,
  the `ai_preferences` decision and the dead-column cleanup decision.

## Previous phase — SAVE V2 TELEGRAM SYNC: the saved message carries the owner's file name and tags

Repository `Onlyicing1/Telegram-self-bot` · branch `main`.

### Phase identity

| Item | Value |
|---|---|
| Phase name | **SAVE V2 — TELEGRAM SAVED-MEDIA SYNCHRONIZATION** (a focused follow-up to Part 4) |
| Type | runtime feature (service + panel + AI tools/action + shared caption helpers) + tests — **no schema change** |
| Starting HEAD | `6f5dde7` = `origin/main` (working tree clean on entry) |
| Implementation commit | the single phase commit that contains this report (`git log -1 --format=%H` re-verifies it) |
| Database migration required | **NO** — `saved_items` already carries `file_name`, `caption`, `saved_chat_id`, `saved_msg_id`, `file_id`, `file_size` and `mime_type`. No SQL was executed, no migration file was added, and `DATABASE_ARCHITECTURE.md` was not touched. |
| Live Telegram | **NOT VERIFIED** (see below) — no live account was driven in this environment |

### The live-test finding this phase fixes

Live testing showed the Part 4 gap plainly: renaming or re-tagging an item
changed **only the database row**. The actual Saved Messages message kept its old
`University_Week_2.pdf` filename, and the owner's tags existed nowhere the owner
could see — there was no owner-tag section in the caption at all, so the change
was invisible in Telegram.

Root cause: `saved_items` was the ONLY thing being written. Nothing in the
synchronization path touched the Telegram message, because the Telegram filename
lives in the document's **media attributes** (not in a caption or a database
column) and therefore cannot be edited in place.

### File name vs Additional tags — two separate layers, never coupled

| Layer | Setting | Meaning | Sync mechanism |
|---|---|---|---|
| **File name** | `saved_items.file_name` | the filename INSIDE the Telegram document | download → re-upload with only `DocumentAttributeFilename` replaced → replace the saved message |
| **Additional tags** | `saved_items.tags` | the owner's semantic tags | caption edit only — the media is **never** re-sent |
| Display name | `saved_items.display_name` | the item's logical LifeOS label | metadata only; **never** touches the Telegram file name |

The Part 4 panel says *Rename / display name*, so the owner's label behaviour is
preserved exactly and the Telegram filename is changed only by an **explicit
request** — a new `📄 File Name` control on the item panel (its row shows the
current filename) and a new optional `file_name` argument on the AI `rename_save`
tool / `rename_saved_item` action. Passing `display_name` alone still changes
nothing in Telegram.

### Exact architecture after implementation

```
manual: retrieve_item → ✏ Rename      → do_rename(owner, code, name)
        retrieve_item → 📄 File Name  → do_change_file_name(client, owner, code, new_name)
        retrieve_item → 🏷 Tags       → do_edit_tags(owner, code, op, tags, client=…)
AI:     rename_save{display_name?|file_name?} / update_save_tags{tags,mode}
                     │
                     └─► retrieve_service (the ONE authority, owner-scoped)
                            _caption_base        → saved_items.caption, else the live caption
                            with_additional_tags → rewrite ONLY the Additional-tags section
                            with_file_name       → rewrite ONLY the file-name section
                            _sync_saved_caption  → edit_message (tag path, no re-send)
                            _write_metadata_fields → ONE statement + confirm by re-read
```

* **One caption authority** — `save_service.with_additional_tags` /
  `with_file_name` are pure, deterministic caption rewrites; the system hashtag
  line (`#saved…`) marks the END of the metadata block, so a rewrite can never
  reach the original source text.
* **One Telegram caption writer** — `_sync_saved_caption` (tag path) and the
  replacement message (filename path) are the only places a saved message is
  edited; the tag path never downloads or re-uploads media.
* **Owner tags stay the owner's** — `saved_items.tags` holds ONLY owner tags
  (legacy `#saved*` values are preserved untouched and never matched/rendered as
  owner tags) and the generated `#saved…` line never enters the column.

### Replacement-message strategy and failure safety (file name)

Telegram cannot rename a document in place, so the content is downloaded and
re-uploaded as a **new** Saved Messages message with the same bytes and the same
media handling Deep Save uses (`save_service._upload_kwargs_for_media`), which
copies every other attribute and the MIME type:

```
resolve owner item → read the saved message → must be a DOCUMENT with a file name
  → download to a per-operation temp dir (removed on every path)
  → send_file("me", …, caption=<file-name + tags synced>, attributes=<new filename>)
  → confirm saved_msg_id + file_name in the ROW (second owner-verified read)
  → ONLY THEN delete the previous saved message
```

* A failure at **any** step before the confirmation leaves the original message
  **and** the original row untouched, and reports the reason.
* A replacement whose identifiers cannot be confirmed is deliberately **not**
  cleaned up: a failed confirmation read must never destroy a message the row
  may already point at. The row still points at the original, so nothing is lost.
* If the old message cannot be removed, the rename is still reported as done with
  an explicit note — the row already points at the confirmed replacement.
* Photos and documents with no filename attribute are refused honestly (there is
  no file name to change), never degraded into another media type.

### Verification

| Suite | Result |
|---|---|
| `tests/test_save_v2_telegram_sync.py` (**new**, 42 tests) | **`42 passed`** |
| `tests/test_save_v2_management.py` (Part 4, fakes made async-correct) | **`61 passed`** |
| 14 Save/retrieve/AI/db suites together | **`486 passed`** |
| **Full suite** `pytest tests/ -q` | **`4625 passed, 26 skipped, 3 warnings` in 116.72 s** (no test deleted or weakened; the 26 skips are pre-existing) |
| `python -m py_compile` on every changed Python file | clean |
| `git diff --check` | clean |

The new suite pins: the new `DocumentAttributeFilename` actually passed to
`send_file`; media type/attributes/MIME preserved; the row repointed to the new
message; `send_file → db_update → delete_old` ordering; a failed re-upload,
download or confirmation preserving the original message and row; a photo/bare
document refusal; owner isolation; the caption section transforms (add/replace/
clear, idempotent, source text and `#saved…` line byte-preserved, a source-text
line that merely *looks* like a section never rewritten); tag-only edits never
re-uploading; the AI tool/action contract; and the manual panel/input path.

### Live verification status

* **Telegram:** NOT performed — no live session was driven. The panel rows,
  inputs, tools and service operations were exercised against the real handlers
  and real service code with a faked Telegram client, so the RPC **shape** is
  tested but reachability is not claimed.
* **The actual saved message is not claimed to have been verified.**

### Known limitations / deferred work

1. The filename replacement re-uploads the media, so a very large saved document
   costs a download + upload of its full size; a tag-only change never does.
2. An item whose Telegram document carries no filename attribute cannot be
   renamed (there is genuinely no filename to change); this is reported, not
   worked around.
3. Legacy rows saved before the `caption` column was populated are handled by
   reading the message's live caption as the rewrite base — one extra read, only
   for those rows.
4. A rename and a tag edit are two separate state changes; the row is not updated
   atomically across the Telegram call and the database write (the confirmation
   read makes any partial outcome visible and the caption rewrite is idempotent).

### Exact next step — live Telegram verification

1. Save a document, rename its **File Name**, and confirm the retrieved file
   exposes the new filename (and that the item no longer offers the old one).
2. Add / replace / clear **Additional tags** and confirm the saved message's
   `🏷 Additional tags:` section changes while the `#saved…` line and the original
   source text do not.
3. Confirm the item still retrieves correctly by display name, tag and save code,
   and that deleting the OLD message happened only after the replacement existed.

## Previous phase — SAVE V2 PART 4: saved-item management (rename + tags)

Repository `Onlyicing1/Telegram-self-bot` · branch `main`.

### Phase identity

| Item | Value |
|---|---|
| Phase name | **SAVE V2 PART 4** — the final management/integration layer for Saved Items, on BOTH surfaces (manual panel + AI) |
| Type | runtime feature (service + AI tools + action contract + panel) + tests — no schema change |
| Starting HEAD | `0930225` = `origin/main` (working tree clean on entry) |
| Implementation commit | the single phase commit that contains this report (`git log -1 --format=%H` re-verifies it) |
| Database migration required | **NO** — `display_name` (Part 1) and `tags` already exist; Part 4 writes only those existing columns. No SQL was executed and `DATABASE_ARCHITECTURE.md` was not touched. |
| Live Telegram | **NOT YET VERIFIED** (see below) — this is the implementation/test phase |

### What Part 4 adds (and what it deliberately does not)

Parts 1–3 made saved items *saveable* (metadata) and *retrievable* (deterministic
0/1/N resolution). Part 4 makes them **manageable**. Three concrete gaps existed
in the real source before this phase:

1. `retrieve_service.do_rename` **did not rename anything** — it validated nothing,
   wrote no column, and returned `✅ Renamed to \`X\`` (a fabricated success).
2. There was **no tag editing at all**: no way to add, replace or clear an item's
   owner tags after the save.
3. The AI surface had `preview_save` / `delete_save` / `retrieve_save` but **no
   way to rename or re-tag** an item, so the management model was partial.

Deletion was already implemented (`delete_save` → `do_delete`) with the same
owner-scoped row lookup; Part 4 only verifies that contract (a test drives it
through the panel action) and does **not** redesign it.

### Exact architecture after implementation

```
manual panel                                    AI (native tool call / JSON action)
  retrieve_item → ✏ Rename → one line              rename_saved_item  → rename_save
  retrieve_item → 🏷 Tags   → one line             update_saved_item_tags
  retrieve_item → 🗑 Delete (unchanged)                 → update_save_tags
          │                                                   │
          └────────► retrieve_service (the ONE authority) ◄────┘
                       resolve_management_target(owner, save_code|query)
                         → ok / not_found / ambiguous / invalid
                       do_rename(owner, code, name)   → display_name
                       do_edit_tags(owner, code, op, tags) → tags
                       (_write_metadata confirms EVERY write by re-reading)
```

* **One resolver** — `resolve_management_target` calls the Part 3
  `resolve_saved_items`; there is no second search and no second matching rule.
  A test patches `resolve_saved_items` and asserts it is the function used.
* **One metadata writer** — `do_rename` / `do_edit_tags` are the only writers of
  `saved_items.display_name` / owner `tags` after the save, and both go through
  the SHARED `save_service.normalize_display_name` / `normalize_tags` rules.
* **Target rules** — `save_code` XOR `query`; 0 → honest not-found, 1 → act,
  N → list the bounded candidates and NOTHING is written (the AI is instructed
  to ask; the panel renders the candidate list).
* **Owner isolation** — the row is read through `load_saved_item` (owner predicate
  + row identity); a foreign item is reported exactly like a missing one.

### Manual UI behavior

* `retrieve_item` now shows the stored `**Tags**` line and offers
  `✏ Rename` and `🏷 Tags` beside Retrieve / Move / Delete. The preview is the
  same `format_preview` the AI `preview_save` returns, so both surfaces show one
  stored truth.
* Rename collects one line and now **persists** `display_name` (the previous
  handler called a function that wrote nothing).
* Tags collects one line with one documented grammar —
  `a, b` = replace, `+a` = add, `-a` = remove, `-`/`none`/`بدون` = clear all;
  mixed `+`/`-` and empty lines are refused with an owner-readable reason rather
  than guessed. The "no tags" vocabulary is the SAME constant the Save panel's
  metadata step uses (imported, not re-typed).
* No new questionnaire: Save itself is unchanged, and the tags step exists only
  where the owner asks for it.

### AI behavior

* Two new narrowly-scoped tools: `rename_save` (`display_name`) and
  `update_save_tags` (`tags` + explicit `mode` add/replace/remove), both
  `READ_WRITE`, both delegating to the service layer, both resolving the target
  through the shared resolver. `required_arguments` mirror exactly what
  `execute()` rejects.
* The action contract gained `rename_saved_item` and `update_saved_item_tags`
  with exact per-action field sets: a rename cannot carry `tags`, a tag edit
  cannot carry `display_name`, unknown fields are rejected, `save_code` and
  `query` are XOR, and `mode` is required (`replace` + `[]` is the ONE clear-all
  form; empty tags with add/remove is refused).
* The prompt template documents both tools, the explicit modes, and the
  never-choose-among-candidates rule, with JSON examples in English and Persian.
* Registry: **46** tools (was 44); the three tests that pin the tool inventory
  were updated accordingly (`test_tool_health_audit` also gained the two
  permission entries).

### Deterministic resolution / owner isolation — evidence

* `resolve_management_target` takes no Telegram identity at all (a test asserts
  its exact parameter list), and `rename_save` / `update_save_tags` ignore
  model-supplied `owner_id` / `chat_id` (a test asserts the stored row keeps the
  trusted owner).
* A foreign `save_code` and a missing one produce the identical message; a
  same-named item belonging to another owner is never resolved.

### Honest writes (a defect found and fixed while testing)

`update_save_field` returns PostgREST's UPDATE representation, which is not
proof a write landed, so metadata success is derived from a second
owner-verified READ: the stored value must equal the value asked for, otherwise
the caller gets `❌ … not stored — the saved item is unchanged.` Two tests drive
this with a silently-ignored write for both rename and tags.

The new tests also caught a real defect in the first draft: the post-merge tag
list was re-normalized OUTSIDE the try/except, so an add that exceeded the
10-tag bound raised `ValueError` instead of refusing — now refused honestly
before any write (regression test included).

### Tests added and exact results

| Command | Result |
|---|---|
| `pytest tests/test_save_v2_management.py -q` (new, Part 4) | **61 passed** |
| `pytest` on the 10 Save/retrieve suites + the DB-setup suite | **363 passed** |
| `pytest tests/ -q` (full suite) | **4583 passed, 26 skipped** in 116.56s (baseline 4522/26 + the 61 new) |
| `python -m py_compile` on every changed file | clean |
| `git diff --check` | clean |

The new suite covers, at the real service/tool/handler boundaries: rename
unique / ambiguous / missing / invalid-name / foreign-owner; add / replace /
remove / clear-all tags; shared normalization (case-insensitive dedupe, the
limits refused before writing); legacy `#saved*` preservation; tag-based unique
and ambiguous targets; zero-match behavior; the manual panel path (rows, stored
name and tags, real writes, no carry-through, refusal of an unreadable line,
rename persistence, another owner's item untouched, delete's existing
contract); the AI path (registry contract, provider-schema visibility, real
writes, ambiguous refusal, bad mode/shape refusal, ignored model identity); the
action contract (14 invalid payload shapes + the one valid clear-all form,
unrelated actions still rejecting saved-item metadata, JSON→tool mapping); and
regressions for Parts 1–3 plus Save V1.

### Database status

No migration, no SQL, no Supabase connection. `display_name` and `tags` were
already part of the canonical schema applied in the previous phase.

### Known limitations (recorded, not hidden)

* Nothing is verified against live Telegram in this phase (per the phase plan).
* `do_move` still does not persist a folder (it predates Part 4 and has no
  column contract); it was deliberately left untouched — Part 4 is rename +
  tags only.
* Renaming changes the item's LABEL only. The Telegram saved message's own
  caption still carries the original LifeOS caption block — the documented
  behaviour, since that caption is a historical artifact of the upload.
* A tag edit preserves legacy `#saved*` values in the column and edits only the
  owner's tags; those legacy values remain non-searchable and non-displayed, as
  Part 3 documented.

### Live Telegram status = NOT YET VERIFIED

No live Telegram interaction was performed. Every verification above is
in-process (real service/tool/handler code, in-memory database fallback, faked
Telegram client).

### Exact next step — LIVE TELEGRAM VERIFICATION

Perform live Telegram verification of the complete Save V2 flow: save with a
name and tags, retrieve by name and by tag (0/1/N), rename, add/replace/clear
tags, delete — through both the `Menu` panels and the AI trigger path.

---

## Previous phase — DATABASE SETUP CONSOLIDATION: ONE complete Supabase setup script

Repository `Onlyicing1/Telegram-self-bot` · branch `main`.

### Phase identity

| Item | Value |
|---|---|
| Phase name | **DATABASE SETUP CONSOLIDATION** — `DATABASE_ARCHITECTURE.md` now presents exactly ONE user-facing executable SQL artifact for the pending database setup |
| Type | documentation + test contract only — **no runtime, schema, provider, handler, service or Supabase change** |
| Starting HEAD | `72e4afd` = `origin/main` (workspace was 19 commits behind; fast-forwarded to `72e4afd` before editing) |
| Implementation commit | the single phase commit that contains this report (`git log -1 --format=%H` re-verifies it) |
| Database migration required | **NO** — no new migration file, no schema change, no SQL executed. The five existing migrations are untouched; their SQL is only consolidated into the document. |
| Telegram AI Settings controls | **not applicable** — this phase adds no setting, panel or persistence behavior |
| Persistence / runtime config | **not applicable** — unchanged; no `config_store`, no `ai_config`, no engine edit |
| Supabase / live Telegram | untouched / not exercised (no live verification is claimed) |
| Tests updated | `tests/test_database_setup_order.py` (rewritten, 21 tests) and `tests/test_canonical_schema_reconciliation.py` (the copy-pinning contract moved, 33 tests) |

### The problem this phase addresses

§31 still described a **two-stage manual workflow**: it told the owner to paste the
canonical reconciliation block (then in §30.5 / the bootstrap section) *first* and a
separate "steps A–D" block *immediately after step 0*. It also offered a `cat
supabase/migrations/… > lifeos_canonical_setup.sql` shell command as the "single
file" alternative, and the document carried a second complete copy of the
canonical reconciliation SQL inside its bootstrap section. The owner had to find
two blocks, in two places, and run them in the right order — the exact failure mode
this phase removes.

### What changed exactly

The requirement is literal: **ONE fenced SQL block whose contents contain EVERY
SQL statement required.** The new **§31.3 — "ONE COMPLETE SUPABASE SETUP SCRIPT"**
holds that single block. It physically contains, in this order:

1. `20260920000001_reconcile_canonical_schema.sql` — the **complete** canonical
   reconciliation SQL (part 1 of 5), the largest piece, physically inside the block
2. `20260919000001_create_api_credential_vault.sql` — Vault PART 1 (part 2 of 5)
3. `20260919000002_credential_vault_management.sql` — Vault PART 2, all five
   `SECURITY DEFINER` management functions complete and untruncated (part 3 of 5)
4. `20260921000001_add_saved_items_display_name.sql` — the `display_name` column
   (part 4 of 5)
5. `20260922000001_add_saved_items_search_indexes.sql` — `pg_trgm`,
   `idx_saved_items_display_name_trgm`, `idx_saved_items_tags` and the verification
   queries (part 5 of 5)

The block is the five migrations' executable SQL, concatenated in order with a
part banner before each; the migrations' prose comment headers are dropped so the
artifact is **pure runnable SQL** (this is also what removes the words that made a
naive placeholder scan ambiguous). Every statement is verbatim from its migration
and a test pins each one back to its file.

### Exactly ONE executable location

* **§31.3** is the only complete deployment SQL block in the document. The old
  standalone block in the "Canonical Supabase Bootstrap SQL" section is **removed**
  and replaced by a short reference to §31.3.
* **§30.5** no longer claims a second executable canonical block: it states that
  the canonical script is byte-identical in exactly two repository files
  (`supabase/canonical_bootstrap.sql` and the migration) and that its statements
  are embedded as part 1 of the §31.3 block — the only executable canonical SQL.
* **§30.11** now instructs the owner to run the ONE §31.3 block; its old "then
  apply the additive migrations separately" step is gone.
* **§29.10 / §29.14** remain as the object-contract documentation for the Vault
  SQL and now point at §31.3 (parts 2 and 3) as the executable copy.
* The `cat supabase/…` bash workflow is **removed**. §31 contains no ```` ```bash ````
  fence at all.

### Verification against the requested checklist

| Requirement | State |
|---|---|
| ONE fenced SQL block, one clear section | §31.3, `#### 31.3 ONE COMPLETE SUPABASE SETUP SCRIPT` |
| Canonical reconciliation SQL physically inside it | yes — part 1 of 5, complete (`BEGIN;` … drift report) |
| All five migrations included, in order | yes, parts 1–5 in the required order |
| Vault PART 2 complete (five functions, no truncation) | yes |
| `display_name` before its index | yes — part 4 before part 5 |
| Security SQL preserved | `SECURITY DEFINER`, `SET search_path = ''`, `OWNER TO postgres`, `REVOKE`, `GRANT`, RLS, `owner_id` scoping, orphan cleanup, constraints, indexes, `NOTIFY` all present verbatim |
| No destructive statements added | the block introduces none; the only `DELETE`s are the Vault cleanup paths (bounded by `WHERE`) |
| No second complete deployment block | exactly one (asserted by test) |
| No shell command / no two-stage workflow / no placeholder | asserted by test |

### Tests and exact results

| Command | Result |
|---|---|
| `pytest tests/test_database_setup_order.py -q` | **21 passed** |
| `pytest tests/test_canonical_schema_reconciliation.py -q` | **33 passed** |
| `pytest tests/test_credential_management.py tests/test_credential_vault.py tests/test_save_v2_metadata.py` + the two above | **248 passed** |
| `pytest tests/ -q` (full suite) | **4522 passed, 26 skipped** in 117.67s |
| `python -m py_compile` on the two changed test files | clean |
| `git diff --check` | clean |

The new `tests/test_database_setup_order.py` asserts the actual document shape, not
just that a function was called: (a) exactly one complete deployment block exists
and it lives in §31; (b) every part is statement-identical to its migration;
(c) the canonical reconciliation SQL is physically present; (d) the five Vault
functions are present with their security guarantees; (e) `display_name` precedes
its index; (f) the block contains none of `see §30.5`, `paste §30.5`, `paste it
first`, `after step 0`, `cat supabase/`, `run separately`, `[truncated]`, `...`,
`same as above`, `omitted` and uses no `<placeholder>`; (g) §31 has no bash fence
and no old workflow wording; (h) the bootstrap section keeps no second block.

### Known limitations (recorded, not hidden)

* The block is large by design (≈120 KB / ≈2156 lines). A small block that
  referenced another would be wrong; the size is expected.
* The migrations' prose comment headers are not reproduced inside the block. The
  prose lives in the migration files and in §29/§30; only comments were dropped,
  and no statement was altered or removed.
* Recognition of "one block" is by the block's banner plus the structural
  "complete deployment" predicate (canonical `saved_items` + `ai_config` +
  drift report); read-only SQL snippets elsewhere (rollback, cleanup, verification)
  are not complete deployment artifacts and remain.
* No live Supabase/Telegram verification — this phase changed documentation and
  tests only; the SQL was **not** executed.

### Exact next phase

Apply **§31.3** to the live Supabase project as `postgres` (owner action), then
confirm the drift report returns empty. The report's `missing_canonical_column`
result set is the success signal; any `WARNING` names a data-guarded constraint a
pre-existing row blocked.

---

## Previous phase — SAVE V2 PART 2: manual + AI Save metadata wiring

Repository `Onlyicing1/Telegram-self-bot` · branch `main`.

### Phase identity

| Item | Value |
|---|---|
| Phase name | **SAVE V2 PART 2 — manual + AI metadata wiring**: both user-facing Save surfaces can now supply the optional owner metadata (`display_name`/`tags`) through the SAME `SaveMetadata` object and the SAME `execute_save` pipeline Part 1 built |
| Input contract | the `SAVE V2 INVESTIGATION` section below (its **Phase B interaction half**) plus the Part 1 report section — both **re-verified against the current source** before this phase began |
| Part 1 dependency | `49fdb48` — `saved_items.display_name`, the shared `SaveMetadata`, `normalize_display_name`/`normalize_tags`, `caption_hashtags` no longer feeding the `tags` column, and the `20260921000001_add_saved_items_display_name.sql` migration |
| Starting HEAD | `49fdb48` = `origin/main` (in sync — no fetch, no fast-forward, no rebase needed) |
| Implementation commit | the single phase commit that contains this report (`git log -1 --format=%H` re-verifies it) |
| Schema change | **none** — Part 1 already added `display_name`; `DATABASE_ARCHITECTURE.md` needed no change (no inconsistency was created) |
| Supabase / live Telegram | untouched / not exercised (no live verification is claimed) |
| Tests added | `tests/test_save_v2_wiring.py` — **51 tests**; 2 existing assertions updated (below) |

### The problem this phase addresses

Part 1 gave the database a place for an owner name and owner tags, and gave
`execute_save` the capability to persist them — but **nothing user-facing could
supply them**. The manual Save panel went straight from `Deep Save` to `Reply
Mode`, and the AI Save tools declared an empty parameter schema (`parameters ==
{}`), so a request like *"save this as University Schedule and tag it university
semester-2"* had no path to the new columns. This phase wires both surfaces into
the Part 1 contract **without** changing the Save pipeline itself, prompting for
anything, or implementing any retrieval work.

### MANUAL SAVE — the optional `Name & tags` step

The Deep Save source panel gains exactly one row:

```
📥 Save → ⬇️ Deep Save → 💬 Reply Mode        (unchanged, still prompt-free)
                       → ✏️ Name & tags      (NEW, optional)
                       → 🔗 Save using a link (unchanged)
```

* **Never prompted.** `Name & tags` is its own row, exactly like the existing
  optional inputs (`input:save:link`). Nothing asks for a name or tags after a
  save; a plain Reply Mode save stays a two-tap, zero-prompt flow.
* **ONE documented line format**, stated in the input prompt itself:
  `Name | tag, tag`. The first `|` splits the name from the tags; a line with no
  `|` is a name (no tags); an empty left side means no name; an empty right side,
  or `-` / `none` / `no` / `بدون` / `هیچ`, means **explicitly no tags**. A second
  `|` is refused (one delimiter, not five syntaxes), and a line that sets neither
  a name nor tags is refused.
* **Deterministic parsing**, no LLM: `parse_metadata_line` splits the line and
  hands it to the shared `SaveMetadata.from_raw` — lengths, whitespace, dedupe,
  the 10-tag/40-char/120-char bounds stay the service's single authority. A bare
  sentence is never split into invented tags.
* **The metadata reaches the reply save through the pending entry itself.** The
  step arms Reply Mode with `_reply_handler_with_metadata(metadata)` — a closure,
  because `set_pending` stores the handler callable in the per-owner pending
  state. That is the one vehicle this architecture provides, and it expires with
  the state (the same 120 s window as every other pending input). Reading the
  popped state back inside the handler would **not** work: the input listener
  clears the entry before it calls the handler (see *Known limitations* for the
  pre-existing read-back defect this deliberately does not copy).
* `_save_reply_wait_handler` takes the metadata as an optional keyword-only
  argument and forwards it to `execute_save(..., metadata=metadata)`. It performs
  no parsing, no validation and no DB write itself — the handler stays thin.

### AI SAVE — the complete parameter path

The model may now propose the owner's own metadata, and it survives every stage:

```
MODEL → structured save action → validate_action (shape + save-only guard)
      → ActionParseResult.display_name/.tags → resolve_tool_calls
      → SaveTool/SaveByLinkTool arguments → SaveMetadata.from_raw
      → execute_save → saved_items
```

| Stage | Change |
|---|---|
| `SaveTool.parameters` | `display_name` (string) + `tags` (array of strings), both **optional**, with descriptions that forbid inventing either; still no `mode`/caption parameter (Deep Save remains the only save) |
| `SaveByLinkTool.parameters` | the same two optional fields beside the required `link` |
| `ALLOWED_FIELDS` | permits `display_name` + `tags` |
| `validate_action` | a new guard rejects both fields on **any** non-save action, so metadata can never ride along with an unrelated execution; the two Save actions call `_validate_save_metadata`, which checks **shape only** (text / list-of-strings) and returns an `ActionParseResult` rejection otherwise |
| `ActionParseResult` | gained `display_name: str = ""` and `tags: list[str] \| None`; `""`/`None` mean *the owner supplied none* — never “an empty name”/“no tags requested” |
| `resolve_tool_calls` | carries only the fields that were actually supplied (`{}` when there are none — the existing shape), for both `save`/`deep_save` and `save_link` |
| tools | `_save_metadata(context, arguments)` builds the shared `SaveMetadata.from_raw`, **before** the reply is fetched from Telegram, and returns the service's own reason on refusal (no download, no upload, no row) |
| prompt template | teaches that the name/tags are optional and owner-supplied only, that `tags: []` is the explicit decline, and that nothing is invented or asked for |

### Explicit “no tags” — deterministic, owner-authoritative

`explicit_no_tags_requested(text)` recognises the owner's own decline phrases
(`no tags`, `without tags`, `dont tag`, `untagged`, `بدون تگ`, `تگ نزن`, …) and
runs on the **owner's request text**, which the dispatcher already carries in
`context.extra["request_text"]`. When it is true, the Save adapters force
`SaveMetadata.from_raw(name, ())`: a model proposal can never re-add tags the
owner asked not to have. The request never reaches the deterministic fast path
either — `parse_command_intent` returns conversational for a save that names an
item or asks for tags, so the metadata arrives through the validated parameters
instead of being dropped by the fast path.

### Backward compatibility

* A save with no metadata is byte-identical in behavior: `{}` arguments, the
  same confirmation, `display_name = NULL`, `tags = '{}'` (Part 1's contract).
* `execute_save`/`execute_link_save` keep their positional signatures; every
  caller (panel reply, link, `SaveTool`, tests) keeps working.
* `save_code` generation, media-target resolution, Deep Save ordering, size
  limits, owner scoping and caption rendering are untouched.
* `_save_reply_wait_handler`'s new parameter is keyword-only, so the existing
  `set_pending(handler=_save_reply_wait_handler, …)` arm is unchanged.

### Files changed

| File | Change |
|---|---|
| `backend/bot/handlers/save.py` | the `Name & tags` row (both renderings), `parse_metadata_line`, `describe_metadata`, `_reply_handler_with_metadata`, `_save_metadata_input_handler`, the `metadata=` keyword on `_save_reply_wait_handler`, the `register_input("save", "meta", …)` wiring |
| `backend/ai/tools/save.py` | `_request_text`, `_save_metadata`, the two optional parameters on both tools, metadata built before the reply fetch, forwarded to `execute_save`/`execute_link_save` |
| `backend/ai/actions.py` | `ALLOWED_FIELDS`, `_SAVE_ACTIONS`, `_validate_save_metadata`, the save-only guard, `ActionParseResult.display_name/.tags`, `_save_metadata_arguments`, the metadata vocabulary (`explicit_no_tags_requested`, `save_metadata_requested`) and the fast-path deferral |
| `backend/ai/prompt/template.py` | the optional metadata rule + examples (name, tags, both, explicit decline) |
| `backend/services/save_service.py` | `execute_link_save` accepts and forwards the same optional `SaveMetadata` |
| `tests/test_save_v2_wiring.py` | new file — 51 tests |
| `tests/test_14_tool_honesty_glass.py` | the assertion that pinned `SaveTool.parameters == {}` now asserts the two optional parameters (still no mode/caption) |
| `tests/test_20_advanced_execution.py` | the two spy signatures on `execute_link_save` accept `metadata=` and assert the shared empty contract |
| `IMPLEMENTATION_REPORT.md` | this section |

### Tests and exact results

| Command | Result |
|---|---|
| `pytest tests/test_save_v2_wiring.py` | **51 passed** |
| `pytest tests/test_save_v2_wiring.py tests/test_save_v2_metadata.py tests/test_12_save_engine.py tests/test_20_advanced_execution.py tests/test_14_tool_honesty_glass.py tests/test_canonical_schema_reconciliation.py` | **184 passed** |
| `pytest tests/ -q -p no:randomly` | **4439 passed, 26 skipped** in 116.5 s (Part 1 baseline: 4388 passed + these 51 = 4439 — no test removed or weakened) |

The new suite covers the whole parameter path (schema → `ALLOWED_FIELDS` →
validation → `resolve_tool_calls` → tool → service → row), the negative cases
(metadata on another action, wrong types, over-long name/tag list refused before
any transfer, model-injected `save_code` ignored), the explicit-decline override,
the manual line parser (9 accepted forms, 5 refusals), the armed-pending
contract (the tests pop the entry **before** calling the handler, exactly like
the listener) and the two end-to-end saves. `git diff --check` is clean.

### Known limitations (recorded, not hidden)

* **Pre-existing defect found, NOT fixed (out of scope):**
  `backend/bot/handlers/retrieve.py::_retrieve_rename_input_handler` and
  `::_retrieve_move_input_handler` read the item code back with
  `get_pending(owner_id)` — but the input listener pops the pending entry
  (`backend/helper/inline_sender.py`, `clear_pending` **before** the handler
  runs), so the code is always `""` and those flows can only answer “⚠️ No item
  selected.” The Part 2 metadata deliberately uses a different vehicle for this
  exact reason. Fixing retrieve is the retrieval phase's work (it also carries
  the already-recorded `do_rename`/`do_move` false-success finding).
* A name/tag request is now **conversational**: the deterministic fast path
  defers, so it costs one provider round-trip. That is the price of not parsing
  free-form names and tags with a regex.
* The decline vocabulary is bounded and phrase-based; an unusual phrasing that
  the model still passes `tags: []` for behaves identically, since `[]` is
  already the explicit empty contract.
* `as X` name detection is heuristic (`as well/usual/soon/…` are excluded); a
  miss only costs the fast path (the model still receives the request).
* `display_name` remains unindexed and unsearched, and no post-save rename/tag
  editing exists — both are later Save V2 phases, by design.

### Explicitly deferred (unchanged from the investigation's plan)

Not implemented in this phase, and not claimed anywhere:

* **retrieval / semantic search** — no `display_name` or tag lookup, no natural
  language *“give me the university file”*, no `retrieve_save` change;
* **ambiguity handling** — no candidate list, no 0/1/N resolution, no
  candidate-selection actions;
* **manual saved-item management** — no rename UI, no tag editor, no search UI,
  no delete confirmation;
* embeddings/vector search, `file_name`/`short_code` cleanup, and any
  `update_save_field` allow-list redesign.

### Exact next phase

**SAVE V2 PART 3 (retrieval)** — resolve a saved item from what the owner calls
it: candidate lookup by `display_name` and tags (owner-scoped), the 0/1/N
ambiguity contract, and the AI action(s) that select a candidate before
`retrieve_save` runs. Its own report section, its own commit.

---

## Previous phase — SAVE V2 PART 1: the owner-metadata data model and the shared Save contract

Repository `Onlyicing1/Telegram-self-bot` · branch `main`.

### Phase identity

| Item | Value |
|---|---|
| Phase name | **SAVE V2 PART 1 — data model + shared Save metadata**: `saved_items` gains an optional owner `display_name`, the `tags` column stops being filled with invented hashtags, and `SaveMetadata` becomes the single metadata object both Save adapters hand to `execute_save` |
| Input contract | the `SAVE V2 INVESTIGATION` section below, written at HEAD `ecdfdf0` and **re-verified against the current source** before this phase began. This phase executes its **Phase A** (the column + its documentation) and the **data half of Phase B** (the shared metadata model and its persistence) |
| Starting HEAD | `23a51cf` (`docs(save): audit the Save system and plan the user-managed metadata phase`) = `origin/main`. The sandbox was 7 commits behind and was **fast-forwarded** to it before any edit — no rebase, no reset, no force-push |
| Implementation commit | the single phase commit that contains this report (`git log -1 --format=%H` re-verifies it) |
| Migration shipped | `supabase/migrations/20260921000001_add_saved_items_display_name.sql` — forward-only, idempotent, additive, non-destructive |
| Supabase / live Telegram | untouched / not exercised |
| Tests added | `tests/test_save_v2_metadata.py` — **26 tests** |

### The problem this phase addresses

The investigation proved the root cause: **identity and label were never
separated.** `save_code` was the only handle an item had, so every retrieval had
to round-trip through an opaque code and the UI's idea of a "name" was
`media_type` (`retrieve_service._display_name`). At the same time the natural tag
column was already occupied by `build_tags`' five machine-generated hashtags, and
the one field that could have carried a name (`file_name`) is dead. This phase
creates the missing data model and the one shared writer for it. **It changes no
user behavior**: nothing prompts for a name or tags yet.

### Exact schema change

One column, in its own migration:

```sql
ALTER TABLE saved_items
    ADD COLUMN IF NOT EXISTS display_name text;

NOTIFY pgrst, 'reload schema';
```

* **`display_name text`, NULLABLE, NO DEFAULT.** `NULL` means "the owner gave no
  name": display falls back to the source filename and then to `media_type`, and
  resolution never treats `NULL` as a value. The column is never auto-filled,
  never inferred from the caption/filename/media type, and never generated by a
  model.
* **`tags` needs no DDL.** The column already exists as `text[] DEFAULT '{}'`;
  only its **producer** changes (below). No type change, no constraint, no
  backfill, no rewrite of any row.
* **No index is added**: no query reads either column yet. The retrieval phase
  owns the `(owner_id, display_name)` btree and the `tags` GIN decision.
* The migration also **reloads the PostgREST schema cache** — without it the API
  keeps rejecting a write that names the new column — and ends with a
  verification query whose zero-row result means the column exists.
* `file_name` and `short_code` are **untouched** (their cleanup stays the
  separate, owner-gated proposal recorded in §19.7 / §30.10).

### The shared metadata contract

`backend/services/save_service.py::SaveMetadata` — one frozen dataclass holding
the only two fields that are owner metadata:

| Field | Meaning | Absent means |
|---|---|---|
| `display_name: str \| None` | the owner's name for the item | `NULL` — no name was given |
| `tags: tuple[str, ...]` | the owner's semantic tags | `()` — stored `'{}'`, never invented |

* `SaveMetadata.from_raw(display_name, tags)` is the one normalization entry
  point, and `execute_save(..., metadata=…)` re-normalizes whatever it receives,
  so no adapter can bypass the rules.
* `execute_save`'s existing positional signature
  (`client, owner_id, reply_msg, tz_str`) is unchanged; `metadata` is
  **keyword-only and optional**, so every existing call site (the panel reply
  path, the link path, `SaveTool`, the tests) keeps working untouched.
* `insert_fields()` is the single place that decides the payload contribution: it
  always writes the normalized `tags`, and it **omits `display_name` entirely
  when there is no name** — that is what keeps a metadata-less save safe on a
  database that has not applied the migration yet (PostgREST rejects an INSERT
  naming an unknown column). A save *with* a name requires the column and fails
  honestly at insert rather than losing the name silently.
* There is no `ManualSaveMetadata` / `AISaveMetadata` split and no second writer
  of either column: the panel and `SaveTool` are thin adapters over the same
  pipeline — the property the later phases depend on.

### Tag normalization rules (deterministic, one implementation)

| Rule | Behavior |
|---|---|
| trim / whitespace | each tag is trimmed; internal whitespace runs collapse to one space |
| empty values | `None` / `""` / whitespace-only entries are dropped; no tags ⇒ `()` |
| duplicates | deduped **case-insensitively** (`casefold`), first spelling wins |
| case | the owner's casing is preserved exactly as typed |
| Persian / English | identical treatment — no translation, no transliteration, no script folding at storage time |
| max tags | **10** distinct tags; more is **refused**, never truncated |
| max length | **40** characters per tag, **120** per display name; over the bound is **refused** |
| a bare string | is ONE tag, never split per character |
| failure | `ValueError` from the normalizer; `execute_save` answers `⚠️ Nothing was saved: …` **before any download or upload** |

Matching-time folding of Arabic/Persian letter variants (`ي`→`ی`, `ك`→`ک`,
ZWNJ collapse) is deliberately **not** applied to storage — it is a
retrieval-phase concern, where the investigation recorded it as a gap.

### Existing-tag compatibility — what happens to historical rows

1. **No data migration, no backfill, no rewrite.** Rows that already hold the
   generated hashtags (`#saved`, `#saved_photo`, `#saved_2026_09_20`) keep them
   exactly as they are and stay fully readable and retrievable by `save_code`.
2. **New saves stop inventing tags**: a save with no metadata stores `'{}'`. The
   caption keeps its hashtag line (`caption_hashtags`, formerly `build_tags`), so
   a saved message looks unchanged.
3. **The compatibility rule for consumers**: an entry starting with `#` is not an
   owner tag and must be ignored by the resolver. Nothing converts or clears the
   old values; removing them would be a separate, gated cleanup (§30.10).
4. `build_tags` was renamed to `caption_hashtags` because it no longer feeds the
   `tags` column at all — the name now states what it is for.

### Migration safety and validation

Additive only (`ADD COLUMN IF NOT EXISTS` + `NOTIFY` + one verification `SELECT`),
idempotent, no `DROP` / `TRUNCATE` / `DELETE`, no table or index creation, and it
touches no table other than `saved_items` (no Vault reference, no secret store).

| Case | Evidence |
|---|---|
| A — current schema → apply → success | the repository's own PostgreSQL-semantics simulator applies the migration's real statements to the fully reconciled schema; `saved_items.display_name` exists afterwards |
| B — applied twice | the second application changes **nothing** (`snapshot(before) == snapshot(after)`) |
| C — existing rows preserved | two seeded legacy rows survive, `caption` and `owner_id` included |
| D — existing codes preserved | `save_code` values (`S0001`, `SAXCK`) are byte-unchanged and no row is duplicated |
| E — existing tags preserved | the legacy `['#saved', '#saved_photo']` array and the second row's `[]` are unchanged |
| nullability contract | the added column is nullable with no default, and no `NOT NULL` appears for it |
| the snapshot stays frozen | the canonical trio (migration / `canonical_bootstrap.sql` / §30 block) is still byte-identical and still passes all 32 of its tests |

**No PostgreSQL server and no Supabase connection exist in this environment**, so
this is simulated execution with PostgreSQL's semantics (the same register the
reconciliation phase was validated in) plus static statement inspection — not a
live run. §30.12 states that limitation.

### Canonical SQL synchronization — and the one test contract that had to move

The repository's canonical workflow is a **three-copy, byte-identical** script
(the `20260920000001` migration, `supabase/canonical_bootstrap.sql`, the §30 SQL
block), and its tests also asserted that this file is the *newest* migration.
Editing it to absorb a new column would rewrite a migration whose content an
owner who already applied it may hold, and would contradict the repo's own rule
that a forward-only change arrives as its own file. So:

* the trio is **untouched** (still byte-identical) and is now documented as the
  reconciliation **snapshot**;
* the new column arrives in the **newest** additive migration, exactly the way
  `20260917000001_add_ai_config_stt_settings.sql` did for `ai_config`;
* `DATABASE_ARCHITECTURE.md` was synchronized in the five places that would
  otherwise contradict the code: **§2** (the `display_name` column row, the
  rewritten `tags` row carrying its real `'{}'` default and owner semantics, a new
  *User-facing metadata* subsection, and the repository line naming
  `SaveMetadata`), **§20** (migration-status row 16), **§30.4** (the snapshot
  inventory now states that `display_name` sits outside it), **§30.5** (the
  snapshot rule) and **§30.11** (the manual action now includes the additive
  migration);
* exactly one test assertion moved with the contract:
  `test_the_migration_is_new_and_follows_the_project_naming_convention` became
  `test_the_migration_is_forward_only_and_its_successors_stay_additive`. It keeps
  every previous protection (the naming convention, the repair still present,
  historical migrations never rewritten to reference it) and replaces "must be
  the newest file forever" with the property that actually matters: **every file
  newer than the snapshot is additive-only** — no `DROP TABLE`, `TRUNCATE`,
  `DELETE FROM` or `DROP COLUMN` statement (comments are stripped first, so a
  documented rollback block cannot trip it).

### Files changed

| File | Change |
|---|---|
| `supabase/migrations/20260921000001_add_saved_items_display_name.sql` | **NEW** — the additive column + cache reload + verification, with the manual action, rollback and security notes |
| `backend/services/save_service.py` | `SaveMetadata` + `normalize_display_name` + `normalize_tags` and their bounds; `execute_save(..., *, metadata=None)`; the payload carries `tags`/`display_name` through `insert_fields()`; `build_tags` → `caption_hashtags` (caption-only) |
| `tests/test_save_v2_metadata.py` | **NEW** — 26 tests: the contract, persistence, compatibility, ownership and the migration's A–E cases |
| `tests/test_12_save_engine.py` | the one assertion that pinned the old invented tags now pins the new contract (`tags == []`, `display_name` absent/None) |
| `tests/test_canonical_schema_reconciliation.py` | the "must be the newest migration" assertion became the forward-only / successors-additive invariant (above) |
| `DATABASE_ARCHITECTURE.md` | §2, §20, §30.4, §30.5, §30.11 as described |
| `IMPLEMENTATION_REPORT.md` | this section |

`backend/db/client.py` needed **no** change: `insert_save` passes the payload
through and the in-memory fallback stores it verbatim. No handler, no AI tool, no
panel, no provider, no env var and no Supabase object outside `saved_items` was
touched.

### Tests and exact results

| Command | Result |
|---|---|
| `pytest tests/test_save_v2_metadata.py tests/test_canonical_schema_reconciliation.py -q` | **58 passed** |
| Save / database / tool-inventory batch (11 files) | **301 passed** |
| `pytest tests/ -q` (full suite) | **4388 passed, 26 skipped** in 116.62 s |
| Baseline at the starting HEAD (`23a51cf`) | **4362 passed, 26 skipped** → **+26, none removed or weakened** |
| `python -m py_compile` on every changed Python file | clean |
| `git diff --check` | clean |

The tests prove: a metadata-less save still succeeds and invents nothing; a name
persists; tags persist normalized; both persist together; an invalid name refuses
*before* any Telegram call and writes no row; a name never changes `save_code`;
empty/whitespace input follows the documented `NULL` / `'{}'` contract;
duplicate and whitespace-only tags normalize deterministically; legacy hashtag
rows stay readable and untouched; owner scoping and `save_code` retrieval are
unchanged; and the migration is additive, idempotent and preserves
rows/codes/tags.

### Backward compatibility

* Every existing `execute_save` call keeps working **unmodified** (the metadata
  parameter is optional and keyword-only) — proven by the untouched
  `test_12_save_engine.py`, `test_14_tool_honesty_glass.py`,
  `test_saved_item_sender_and_retrieve.py`, `test_20_advanced_execution.py` and
  `test_tool_health_audit.py` suites passing.
* Existing rows remain valid and readable; nothing is regenerated.
* `save_code` generation, format, uniqueness and immutability are untouched.
* The AI `SaveTool` schema, the Save panels, caption rendering, the Deep Save
  order, the size limit and owner isolation are all unchanged.
* Nothing user-facing is exposed yet: **the AI cannot name or tag an item, and no
  prompt asks for either** — metadata the owner cannot yet supply is not offered.

### Known limitations (recorded, not hidden)

1. **The AI and panel surfaces cannot pass metadata yet** — by design (the
   investigation's phases C/E). The pipeline is capable; the wiring is not.
2. **`update_save_field` still has no field allow-list** (investigation §6/§15).
   Nothing model-authored reaches it today, so closing it was out of scope — but
   the management-UI phase must close it before rename/tag editing ships.
3. `do_rename` / `do_move` still report success without persisting, and the
   saved-item delete still has no confirmation. Unchanged, unfixed, out of scope.
4. Legacy rows keep their `#`-prefixed tags until a gated cleanup decides
   otherwise.
5. Applying the migration is a manual Supabase action. Until it is applied, the
   column simply does not exist — harmless for every metadata-less save, and an
   honest insert failure for a named one.

### Explicitly deferred (Save V2 parts C–G)

Search and semantic retrieval (querying `display_name`/`tags`, the deterministic
0/1/N resolver, the typed candidate-list tool contract), ambiguity handling, the
manual management UI (rename, tag editing, delete confirmation), the AI
`save this as X` / `save with tags …` syntax (`ALLOWED_FIELDS`, the validation
branch, `resolve_tool_calls`, `SaveTool.parameters`, the prompt template),
`file_name` / `short_code` cleanup, `update_save_field` allow-listing, and the
optional future indexes. **Save V2 is NOT complete — this is part 1 of the
investigation's A–G sequence.**

### Exact next phase

The investigation's **Phase C** (manual Save: an optional "Name & tags" step in
the Save panel, routed through the existing input machinery, with the plain reply
path staying prompt-free) and **Phase E** (the AI `name`/`tags` fields, changed
atomically across `ALLOWED_FIELDS`, the validation branch, `resolve_tool_calls`,
`SaveTool.parameters` and the prompt template) — after the migration is applied,
so a name written by either surface has a column to land in.

## SAVE V2 — PART 3 — DETERMINISTIC RETRIEVAL RESOLUTION (0 / 1 / N)

> Written against the **merged** state (Parts 1/2 on `origin/main` + the Part 3
> resolution layer). Every statement was re-derived from the merged source and
> every test result is the exact output of a command run in this workspace.

| | |
|---|---|
| Starting HEAD | `23a51cf` `docs(save): audit the Save system…` (== `origin/main` at phase start, worktree clean) |
| Concurrency note | during the phase, `origin/main` advanced by the PART 1 + PART 2 commits (`49fdb48`, `f9dfd9a`); Part 3 was rebased onto them by a merge before pushing |
| Scope | the deterministic resolution layer BEFORE the existing retrieval pipeline; direct `save_code` retrieval untouched |

### 1. Resolver architecture

`backend/services/retrieve_service.py`:

```
owner request (name / tag / save code)
      ↓
resolve_saved_items(owner_id, query, limit=8)          ← no client, no chat,
      ↓                                                   no message parameter
  0 → RESOLUTION_NOT_FOUND      → honest not-found, nothing retrieved
  1 → RESOLUTION_UNIQUE         → the ONE do_retrieve authority, once
  N → RESOLUTION_AMBIGUOUS      → bounded candidate list, NOTHING retrieved
      ↓
confirmed save_code  →  retrieve_service.do_retrieve(...)   ← unchanged
```

* `SavedItemCandidate` — `save_code`, `display_name`, `file_name`, `media_type`,
  `tags` (owner tags only), `created_at`. No origin/saved chat or message id, no
  sender id, no file id, no internal row id, no caption.
* `SavedItemResolution` — `status` (`not_found` / `unique` / `ambiguous`),
  `query`, bounded `candidates`, `overflowed`.
* `MAX_RESOLUTION_CANDIDATES = 8`; the fetch asks for `limit + 1` so one extra
  row **proves** more matches exist and `overflowed` is reported honestly.
* Ordering is deterministic: `created_at DESC`, then `save_code ASC`.
* The resolver is structurally Telegram-free (signature and body asserted by
  test) and writes nothing. It builds directly on the PART 1/2 contract:
  `display_name` and owner-only `tags` (legacy `#saved*` hashtags are never
  treated as owner metadata), and it never touches `save_code`.

### 2. Search semantics

* **Save-code fast path.** A query shaped `^S[A-Z0-9]{4}$` is a *code* request:
  it goes straight to the owner-scoped `load_saved_item` read and is never
  fuzzy-matched (a code-shaped query can never fall through to a display name).
* **Normalization (comparison only).** NFKC + the project's Persian/Arabic
  normalization (`semantic_delete.normalize_text`): digit folding, script-variant
  folding (ی↔ي, ک↔ك), diacritic/zero-width removal, casefold, whitespace
  collapse. No translation, no transliteration; **stored values are never
  rewritten** (test-enforced). Bounds: 128 chars, 6 tokens.
* **Tiers, first non-empty wins.** `name` (every token in the normalized
  `display_name`) → `tag` (whole-tag equality + a whole-query tag form so
  "semester 2" matches `semester-2`) → `mixed` (token in name / file_name / tag).
* **Tag semantics are explicit.** Whole-tag equality only (`"uni"` never matches
  the tag `university`); arbitrary words are never converted into tag queries.
* **Caption is never searched** — nor sender, chat/message ids, Telegram history,
  "recent media" or "last saved file". Only persisted saved-item metadata.
* **Combined narrowing**: every token group must match, so "project plan alpha"
  resolves the "Project Plan" row tagged `alpha`.

### 3. Database query and owner scoping

`backend/db/client.py` adds exactly one read path:

```python
db.table("saved_items").select(_RESOLVE_COLUMNS)
  .eq("owner_id", owner_id)                       # owner scoping INSIDE the query
  .or_(logic)                                     # token groups ANDed, variants ORed
  .order("created_at", desc=True).order("save_code")   # explicit, deterministic
  .limit(limit)                                   # explicit bound
```

* Owner isolation is the `.eq("owner_id", …)` of the query itself — asserted on
  the query string by test, so "fetch globally, filter in Python" cannot regress.
  The in-memory fallback mirrors the same owner filter, tier rule, bound and
  ordering.
* User text can never alter the filter tree: token values are sanitized
  (`resolve_pattern` / `resolve_tag_term` strip the PostgREST tree-structural
  characters) and a test pins that property.
* One query per tier attempt — no N+1, no unbounded fetch; `_RESOLVE_COLUMNS`
  returns only the candidate-display columns.
* Indexes: `idx_saved_items_display_name_trgm` (GIN trigram, `pg_trgm` already
  enabled) and `idx_saved_items_tags` (GIN), plus the existing
  `idx_saved_items_owner_created` for the owner filter + ordering. They ship as
  an **additive successor migration** (see §5) because the canonical script is a
  byte-frozen reconciliation snapshot (§30).

### 4. 0 / 1 / N behavior

| Case | Behavior | Telegram |
|---|---|---|
| **0** | "🔍 No saved item matches … Try the saved name, one of its tags, or its save code." | no retrieval |
| **1** | retrieved immediately through `do_retrieve(self_client, owner_id, save_code, chat_id)` — exactly once | one retrieval |
| **N** | nothing retrieved; the owner gets the bounded numbered list + an explicit-choice prompt | no retrieval |

Ambiguity never falls back to first/newest/closest, an LLM choice, or the last
saved item — including when the fetch overflowed the display bound.

### 5. Schema, migration and indexes

Ships as **`supabase/migrations/20260922000001_add_saved_items_search_indexes.sql`**,
an additive successor to the reconciliation snapshot and to
`20260921000001_add_saved_items_display_name.sql` (the shape the PART 1 migration
established and §20/§30.11 document): `CREATE EXTENSION IF NOT EXISTS pg_trgm`
(the canonical script already enables it), the two `CREATE INDEX IF NOT EXISTS`
statements above, and a PostgREST schema-cache reload. No historical migration
or snapshot copy is touched; idempotent; no row is rewritten; `save_code` is
untouched. `DATABASE_ARCHITECTURE.md` documents the indexes (§2) and the
successor workflow (§30.11). **Nothing was executed against Supabase.**

### 6. Ambiguity UX, selection state, expiration

* **Buttons.** Each candidate row is `action:resolve_pick:{save_code}` carrying
  the exact presented code — never an index, never a fresh search.
* **Numbered reply.** The same codes are held in the existing pending-input
  state (`extra` = JSON list of the presented codes, in the presented order,
  60 s handler timeout, the shared 120 s pending-state expiry). A numbered reply
  (ASCII or Persian/Arabic digits) maps to `codes[i-1]`; an exact presented code
  is also accepted. Nothing else is.
* **Selection is re-verified before retrieval** — owner + existence via
  `load_saved_item` — so a candidate deleted between display and selection fails
  cleanly with **no fallback to any other candidate**. An expired selection is
  refused by the shared TTL before the handler runs. Non-owner/other-chat replies
  are ignored. `cancel` / `لغو` ends the flow.
* **Zero-spam**: results edit the panel in place; the owner's input message is
  deleted where the existing conventions do it.
* **The documented rename/move defect is fixed in the shared path**: the input
  listener now passes the popped state's `extra` to any handler that declares it
  (`backend/helper/inline_sender.py`), and both handlers take the code from it —
  three regression tests pin it. The ambiguity flow uses the same mechanism, so
  it cannot reproduce the defect. `do_rename`/`do_move` themselves remain the
  documented stubs (unchanged, out of scope).

### 7. AI integration

```
model → retrieve_save(query)  or  retrieve_save(save_code)
      → RetrieveSaveTool (trusted owner + chat from ToolContext)
      → resolve_saved_items
      → 0 = honest failure  1 = do_retrieve(...)  N = structured candidates, NO retrieval
      → retrieve_service.do_retrieve   (the only Telegram retrieval authority)
```

* `retrieve_save` requires **exactly one** of `save_code` / `query`
  (`required_any_arguments`), rejects both, and documents: never invent a code,
  never choose among ambiguous matches.
* `owner_id` and the destination chat come from trusted `ToolContext` —
  model-supplied `owner_id`/`chat_id`/`destination` are ignored (test-enforced).
* Ambiguous result: `success=True` with `{outcome: "ambiguous", candidates:
  [{save_code, label}], displayed, more_matches_exist}` and the instruction
  "NOTHING was retrieved. Ask the owner which one they mean — never choose for
  them." Only `save_code` + `label` are exposed.
* Not-found: `success=False`, `{outcome: "not_found", query}`. Unique: the data
  gains `resolved_from: "query"`; the **code path's** data stays byte-identical.
* The JSON action fallback matches the tool: `retrieve_save` accepts `query` XOR
  `save_code`; `preview_saved_item`/`delete_saved_item` stay code-only.
* The prompt template carries both halves of the contract: PART 2's
  display_name/tags rules and the Part 3 resolver rules (query → deterministic
  resolution, never choose among matches, never invent a code).

### 8. Manual integration

The Retrieve panel uses the **same** resolver — one authoritative mechanism:
`Menu → Save → Retrieve → 🔍 Search by Name/Tag` → unique retrieves immediately,
ambiguous lists candidates (buttons + numbered reply), not-found offers
"Search again". `_resolve_pick_action` and `_retrieve_pick_input_handler` both
re-verify owner + existence before the single `do_retrieve`. The pre-existing
browser/Find surfaces keep their literal `ILIKE` behaviour (unification is
deferred, §13) — no existing search API's meaning changed.

### 9. Tests and exact results

New: `tests/test_save_v2_resolution.py` — **61 tests**: 0/1/N; owner isolation
(same name **and** same tag across two owners, the query string asserted,
fallback included); a foreign match indistinguishable from missing; caption
never searched; legacy hashtags never tags; exact/partial/case/whitespace/
punctuation matching; whole-tag and multi-word tag forms; arbitrary words never
treated as tags; name+tag narrowing; duplicate names and tags; the candidate
bound and `overflowed`; deterministic ordering incl. timestamp ties; Persian
names/tags, Persian/Arabic spelling variants, mixed Persian/English; save-code
fast path incl. unknown and foreign codes failing identically through the real
`do_retrieve`; model-supplied owner/destination ignored; the tool's
unique/ambiguous/not-found/XOR contracts and safe candidate payload; the action
layer; filter-injection safety; the manual panel 0/1/N; the listener's numbered
and exact-code selection, Persian digits, invalid input, other chat, non-owner,
expiry and post-deletion staleness; the click action; and the rename/move
carry-through.

| Command | Result |
|---|---|
| `pytest tests/test_save_v2_resolution.py -q` | **61 passed** |
| Parts 1/2 suites + Part 3 + save engine + tool honesty + actions (`test_save_v2_wiring.py`, `test_save_v2_metadata.py`, `test_19_ai_actions.py` included) | **352 passed** in the focused batch |

| `pytest tests/ -q` (full suite, merged tree) | **4500 passed, 26 skipped, 0 failed** in 117.21 s |
| `python -m py_compile` on every changed Python file | clean |
| `git diff --check` | clean |

### 10. Compatibility

* Direct `save_code` retrieval, `do_retrieve`/`do_preview`/`do_delete`,
  `preview_save`/`delete_save` are unchanged; `do_retrieve` remains the final
  Telegram retrieval authority (the resolver never forwards).
* The PART 1/2 write contract is untouched: `SaveMetadata`, the shared
  normalizers, the additive `display_name` migration and the owner-tags
  behaviour all keep their meaning; the resolver only READS those columns.
* Existing rows, codes and metadata are untouched; nothing is regenerated.
* No new dependency, no new env var, no new table, no Vault change.

### 11. Known limitations

* Deterministic substring/whole-tag matching: no typo tolerance, no embeddings,
  no LLM ranking (deliberate).
* The candidate list is bounded at 8 and reports overflow rather than paging.
* Rename/tags editing UI and `update_save_field` allow-listing are still the
  PART 2 report's recorded gaps (Part 3 added no editor).
* The browser/Find search still uses the older literal `ILIKE` search.
* Validated against the repository's in-memory boundary (owner-scoped query
  asserted) — not against a live PostgREST.

### 12. Deferred

Manual Save metadata entry UI unification details, rename + tag editing UI,
unifying `search_saves`/`do_find` with the resolver, `display_name` in the
dashboard API, dead-column cleanup, typo-tolerant matching, and the outstanding
earlier-phase work (TTS provider fallback and its credential pool, TTS
voice/model selection, Native Vision, Video/GIF, provider benchmarking, live
verification of the vault phases, the canonical snapshot and the successor
migrations).

## SAVE V2 INVESTIGATION — investigation only, NOTHING IMPLEMENTED

> Historical: the investigation itself changed no code. Save V2 Part 3
> (the section above) implemented the retrieval-resolution part of its plan;
> the rest remains deferred as recorded there.

> **Status: investigation / design audit.** This task changed no code, no
> schema, no migration, no tool schema, no panel and no retrieval behavior.
> The only file it touched is this report. Every claim below was read from the
> current source at HEAD `ecdfdf0`; nothing is inferred from earlier reports.
> This section is the input contract for a separate implementation prompt.

### Phase identity

| | |
|---|---|
| Task | Save System V2 — full investigation and design audit |
| Repository / branch | `Onlyicing1/Telegram-self-bot`, `main` (worktree `.m14`) |
| Starting HEAD | `ecdfdf0` (`fix(db): reconcile an existing schema instead of trusting CREATE IF NOT EXISTS`) = `origin/main`, worktree clean |
| Deliverable | this section, plus one focused documentation commit |
| Implementation | **NONE** — nothing was implemented, nothing was fixed |
| Supabase / live Telegram | untouched / not exercised |
| Tests run | save-focused suites: **147 passed**; full suite: **4362 passed, 26 skipped** in 115.71 s |

### 1. Current Save architecture

There is **no text-command Save surface**. `AGENTS.md` §5 records the legacy
`.save` / `.save d` / `.save f` dot commands as removed, and the source agrees:
grep finds no handler that dispatches on `.save`. The two surfaces are the
Glass UI panel and the AI trigger word. Both converge on ONE pipeline.

**Stage map (exact module → function → effect):**

| Stage | Manual (Glass UI) | AI | Shared? |
|---|---|---|---|
| User input | reply to a message after `action:save_reply`, or a t.me link via `input:save:link` | `Nova save this` / `اینو سیو کن` (trigger word → `backend/bot/handlers/ai_unified.py`) | — |
| Command / intent | `backend/bot/handlers/save.py::_save_reply_action` → `set_pending(owner, "save_reply", …, timeout=None)` | `backend/ai/actions.py::validate_action` → `resolve_tool_calls` → `{"name":"save","arguments":{}}` / `save_by_link` | pattern only |
| Target resolution | `_save_reply_wait_handler`: `client.get_messages(chat_id, ids=msg_id)` → `reply_to_msg_id` → `client.get_messages(chat_id, ids=target_id)`; link: `save_service.parse_telegram_link` + `get_messages`/`get_entity` | `SaveTool._resolve_reply_message` reads `context.extra["reply_msg"]` (chat_id + message_id) and fetches the real `Message` through the injected client | same service entry |
| Save execution | **`backend/services/save_service.py::execute_save(client, owner_id, reply_msg, tz_str)`** | **the same function**, via `backend/ai/tools/save.py::SaveTool` | **yes — one implementation** |
| Telegram copy | text source → `client.send_message("me", caption)`; media → `mkdtemp(prefix="lifeos_dl_")` → `download_media` → validate (exists, non-zero) → `send_file("me", tmp_path, caption=…)` → `shutil.rmtree(…, ignore_errors=True)` in `finally` | identical | **yes** |
| DB persistence | `db_client.insert_save(payload)` (only AFTER the upload succeeded) | identical | **yes** |
| Retrieval | `action:retrieve_item_exec:{code}` → `backend/services/retrieve_service.py::do_retrieve` | `backend/ai/tools/retrieve_save.py::RetrieveSaveTool` → the same `do_retrieve` | **yes** |
| Telegram delivery | `forward_messages(entity=dest, messages=saved_msg_id, from_peer=source)`, then `edit_message` with a 2-line metadata block + the stored caption (≤1024 chars) | identical | **yes** |

**Facts pinned by source:**

- **AI and manual Save already share 100 % of the implementation path.**
  `execute_save` is the single authoritative Deep Save pipeline; the handler and
  `SaveTool` are two thin adapters over it. The same is true of retrieval
  (`do_retrieve`), preview (`do_preview`) and delete (`do_delete`).
- **Deep Save never forwards.** No `forward_messages` call exists in
  `save_service`; `client.forward_messages` appears only in
  `retrieve_service.do_retrieve`, solely to re-deliver an already-saved item.
  `forward_messages` is also absent from the `Template` tree.
- **Source identity is preserved.** After re-upload the DB row stores the NEW
  message's `saved_chat_id` / `saved_msg_id` / `file_id` / `mime_type` / size
  (from `_extract_uploaded_metadata`), not the origin's.
- **Bounds.** `settings_service.max_deep_save_mb()` (default 50 MB) is checked
  against the source size before the download starts.
- **Errors are honest.** A DB failure after a successful upload returns
  `⚠️ Uploaded to Saved Messages, but the database record failed …` — the upload
  is never silently rolled back or re-labelled a success.

**The DB payload written by `execute_save` is exactly:**

```text
save_code, save_type="deep", origin_chat_id, origin_msg_id, saved_chat_id,
saved_msg_id, sender_name, sender_id, mime_type, file_id, file_size,
media_type, tags, caption, owner_id, created_at
```
`file_name` is **not** in the payload — the column exists and is never written
(`DATABASE_ARCHITECTURE.md` §19.7 already labels `file_name`/`short_code` dead).

### 2. Current database contract (`saved_items`)

Read from `DATABASE_ARCHITECTURE.md` §2 and the canonical SQL
(`supabase/canonical_bootstrap.sql` lines 63–160 =
`supabase/migrations/20260920000001_reconcile_canonical_schema.sql`).

| Column | Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | `bigserial` | NO | `nextval` | PK, `saved_items_pkey` |
| `save_code` | `text` | NO | — | UNIQUE (`saved_items_save_code_key`); `S` + 4 chars |
| `save_type` | `text` | NO | `'forward'` | **no CHECK** in the canonical schema |
| `origin_chat_id` / `origin_msg_id` | `bigint` | YES | — | source location |
| `saved_chat_id` / `saved_msg_id` | `bigint` | YES | — | the NEW saved message |
| `sender_name` / `sender_id` | `text` / `bigint` | YES | — | resolved source sender |
| `mime_type` | `text` | YES | — | |
| `file_id` | `text` | YES | — | Telegram file id of the uploaded copy |
| `file_size` | `bigint` | YES | — | |
| `media_type` | `text` | YES | — | Photo / Video / Document / Voice / Text … |
| `tags` | `text[]` | YES | `'{}'` | **currently holds only machine-generated hashtags** |
| `caption` | `text` | YES | — | the rendered LifeOS block + the source text |
| `file_name` | `text` | YES | — | **dead: never read, never written** |
| `short_code` | `text` | YES | — | **dead: never read, never written** |
| `owner_id` | `bigint` | NO | — | no default; backfilled to `0` then `SET NOT NULL` |
| `created_at` | `timestamptz` | NO | `now()` | |

**Indexes that exist:** `saved_items_pkey` (id), `saved_items_save_code_key`
(UNIQUE), `idx_saved_items_owner`, `idx_saved_items_save_code`,
`idx_saved_items_created_at`, `idx_saved_items_owner_created`,
`idx_saved_items_short_code` (partial UNIQUE, `WHERE short_code IS NOT NULL`),
plus 5 trigram GIN indexes (`…_caption_trgm`, `…_file_name_trgm`,
`…_save_code_trgm`, `…_short_code_trgm`, `…_mime_trgm`) requiring `pg_trgm`.

**RLS:** enabled; a single `anon_select_saved_items` SELECT policy for
`anon` + `authenticated`. All writes use the service-role key.

**`save_code` generation** (`backend/db/client.py::get_next_save_code`):
serialized by `asyncio.Lock`; tries `S` + zero-padded `count+1` (4 digits),
verifies uniqueness via `_is_code_free`, then up to 50 random
`S` + 4 upper-case alphanumeric codes, then falls back to the sequential value.
Stable, unique, and immutable after insertion — **no code path anywhere updates
`save_code`**.

**Answers to the specific contract questions:**

- *Are filenames persisted?* **No.** `file_name` is never in the insert payload.
- *Are captions persisted?* **Yes**, fully (rendered block + source text).
- *Do tags already exist under another name?* The **column** exists (`text[]`),
  but it is filled by `save_service.build_tags(media_type, dt)`, which invents
  five hashtags per save — `#saved`, `#saved_<media>`, `#saved_<year>`,
  `#saved_<year>_<mm>`, `#saved_<year>_<mm>_<dd>`. They are **never**
  user-supplied, and `search_saves` never queries the column. So the column is
  occupied but its contents are non-semantic for owner retrieval.
- *Is there an existing field that could safely represent `display_name`?*
  **No.** `caption` is a rendered multi-line block that retrieval injects into
  the delivered caption;`media_type` is an enum-like label;
  `file_name`/`short_code` are dead; `tags` are synthetic.
- *Can two saves reference the same Telegram message?* **Yes.** There is no
  unique constraint or dedupe check on `(origin_chat_id, origin_msg_id)` — the
  same source can be Deep Saved repeatedly and gets a distinct `save_code` each
  time.

### 3. Current AI Save flow

1. **Intent detection.** `ai_unified.py` matches the configured trigger word and
   forwards the message. In parallel, `backend/ai/actions.py` runs a
   **deterministic fast path** over the ORIGINAL owner message: `_EN_SAVE`
   (`save/saving/saved/store/storing`), `_SAVE_STEMS` (`سیو/ذخیره/ذخیر`) and
   `_DEEP_TOKENS` (`عمیق/deep/کامل`) resolve to a tool call **without spending a
   provider round**. Only this narrow high-confidence vocabulary is fast-pathed.
2. **Parameter extraction.** `validate_action` enforces an exact field set:
   `unknown = sorted(set(raw) - ALLOWED_FIELDS)` is a hard failure. `save` and
   `deep_save` are validated by a branch that accepts **no additional field at
   all**; `save_link` accepts `link` and lower-cases nothing.
3. **Media resolution.** `SaveTool.requires_reply_context = True`; the target is
   the replied-to message, fetched through the injected client from
   `context.extra["reply_msg"]`.
4. **Mode decision.** Deep Save **only**. `save`, `deep_save` and the fast path
   all resolve to the `save` tool → `execute_save` with `save_type='deep'`. The
   `deep_save` action is now cosmetic relative to `save`.
5. **Optional text.** **Not supported.** The caption is always
   `build_caption(...)` + `_append_original_text(...)` (the source message's own
   text). No owner-supplied name, caption or tag can reach the row.
6. **Context to the model.** Only the reply metadata (chat id + message id) in
   `context.extra`; the model never receives a Telethon object, a chat list or
   the saved-items table.
7. **Persistence.** As §2.

**Can the current schema express the three target phrasings?**

| Owner says | Representable today? | Why |
|---|---|---|
| `save this as university weekly schedule semester two` | **No** | `save` has `parameters == {}`; `ALLOWED_FIELDS` would reject a `name` field, and `SaveTool` has nowhere to put it |
| `save this, but don't give it a tag` | **No** | tags are always invented by `build_tags`; there is no tag argument to be absent |
| `save this and tag it university and semester two` | **No** | same — no `tags` argument and no user tag store |

### 4. Current AI retrieval flow

- `retrieve_save` (`backend/ai/tools/retrieve_save.py::RetrieveSaveTool`):
  **requires** `save_code` (validated as `str.isalnum()` after `.upper()`), takes
  the destination from trusted `context.extra["chat_id"]` (never from the
  model), and delegates to `retrieve_service.do_retrieve`.
  Returns `ToolResult(success=…, message=…, data={"save_code", "chat_id"})`.
- `preview_save` (READ_ONLY) → `do_preview`; `delete_save` (DANGEROUS) →
  `do_delete`. All three are addressed **only** by `save_code`:
  `_SAVE_ITEM_ACTIONS` is `("retrieve_save", "preview_saved_item",
  "delete_saved_item")` and `_validate_saved_item_action` accepts the exact field
  set `{"action", "save_code"}`.
- `search` → `discover_service.do_find` → `db_client.search_saves`, which
  ILIKEs **one** pattern against **three** columns:
  `caption.ilike.%q%,save_code.ilike.%q%,mime_type.ilike.%q%`.
  `display_name`, `file_name` and `tags` are **not searched**, and a multi-word
  query becomes a single literal substring.
- `list_saves` → `discover_service.do_list` → `list_recent_saves` (projects
  `save_code, save_type, media_type, mime_type, created_at`).

**Which questions can the current design answer?**

| Question | Current answer |
|---|---|
| Does AI search saved items semantically? | Only a single-substring ILIKE over caption/code/mime. Not tokenised, not name/tag aware. |
| Does `retrieve_save` accept a name? | No — a literal `save_code` is mandatory. |
| A filename? | No. `file_name` is not searched and is always NULL. |
| Tags? | No. The column is not in the query and holds hashtags. |
| Can it return multiple candidates? | Only as **prose** inside `do_find`'s text (up to 20 lines). No structured count, no truncation signal. |
| Can it return zero cleanly? | `do_find` → `🔍 No matches for the query` with **`success=True`** (it is not a `❌`/`⚠️` prefix). `retrieve_save` with an unknown code → `❌ No item found for …`, `success=False`. |
| Is there a deterministic ambiguity mechanism? | **Not for saved items.** `{"action":"clarify","reason":…}` exists (`KIND_CLARIFY`) but the dispatcher fast-paths clarify **only** for `save`/`deep_save`/`save_link`; everything else is handed to the provider conversationally. |
| Does AI receive Telegram context it should not? | No. Only the trusted `chat_id`/reply metadata the runtime injected. The model cannot name a conversation. |

**"give me the saved university semester two schedule" today:** the model can
only emit `search_saved_items{query:"university semester two"}` and then
narrate the returned text; `retrieve_save` is unreachable without a literal
code. Even the search usually misses, because `%university semester two%` is
matched as one literal substring against a caption that contains a rendered
block plus the *original* message text — the tokens are not adjacent there.

### 5. Current manual Save flow

- `Menu` → **📥 Save** (`register_panel("save", parent="menu")`) → rows
  `⬇️ Deep Save` (`panel:save:type:d`) and `🔍 Retrieve` (`panel:retrieve`).
- `panel:save:type:d` → `💬 Reply Mode` (`action:save_reply`) and
  `🔗 Save using a link` (`input:save:link`).
- **Mode selection:** Deep Save only; there is no forward save and no text
  command. `save_service.execute_save` is the only writer of `saved_items`.
- **File targeting:** reply (resolved through `reply_to_msg_id`) or a t.me link
  (`parse_telegram_link` handles `/c/<chat>/<msg>` and `/<username>/<msg>`).
- **Captions:** generated; the owner cannot supply one.
- **Name / metadata:** not possible.
- **Management menu:** exists — `🔍 Retrieve` → Saved Items → Item Detail with
  Retrieve / Rename / Move / Delete (§6).
- **Zero-spam:** yes. Panels edit in place; the owner's reply or input message is
  deleted after handling (`delete_messages(chat_id, [msg_id])` in
  `_save_reply_wait_handler`, `_save_link_input_handler`, and every retrieve
  input handler).
- **Timeout nuance:** reply mode uses `set_pending(..., timeout=None)` so a large
  transfer is not cancelled; the separate 120 s pending-**state** expiry
  (`input_state._INPUT_TIMEOUT_S`) is unchanged.

### 6. Current manual management UI

Registered panels (`backend/bot/handlers/retrieve.py`):
`retrieve` (parent `save`) → `retrieve_saved` (parent `retrieve`) →
`retrieve_item` (parent `retrieve_saved`), plus `retrieve_code` (parent
`retrieve`).

| Capability | Where | Behavior |
|---|---|---|
| list | `retrieve_saved` | `list_saves(owner_id, 8, offset)`, ordered `created_at DESC`; row label `{icon} {name} · {code}` where **`name` = `retrieve_service._display_name(row)` = `row["media_type"]`**, truncated to 20 chars |
| pagination | `retrieve_saved` | Prev / `page/total` / Next rows; page clamped to `total_pages` |
| search | `find` panel (parent **`menu`**, not `retrieve`) | `input:find:query` → `discover_service.do_find` |
| retrieve | `action:retrieve_item_exec:{code}` | `do_retrieve` |
| delete | `action:retrieve_item_delete:{code}` | `do_delete` — **fires immediately, with no confirmation step** (the module docstring says “with confirmation”, the code does not have one) |
| rename | `input:retrieve_item:rename:{code}` | → `retrieve_service.do_rename` — **does not persist** |
| move | `input:retrieve_item:move:{code}` | → `retrieve_service.do_move` — **does not persist, and there is no `folder` column to write** |
| tags | — | **none** |

Panel navigation is the existing shared infrastructure
(`InlinePanelBuilder`, `register_panel(parent=…)`, `panel:_nav:*`,
`backend/helper/panels.py`). Inputs are dispatched by
`_handle_input`, which splits `input:<panel>:<input>:<extra>` and stores
`extra` in `pending[owner_id]["extra"]` — so `input:retrieve_item:rename:S0001`
**does** hand the save code to the handler as `pending["extra"]`.

**Known pre-existing defects found in this scope (documented, NOT fixed):**

1. **`do_rename` is a false success.**
   `backend/services/retrieve_service.py::do_rename` validates the owner, logs,
   then returns a success string (`✅ Renamed to X`) — it **never writes**. The persistence
   primitive it should call already exists and works:
   `db_client.update_save_field(owner_id, code, field, value)` (owner-scoped
   single-field UPDATE).
2. **`do_move` is the same false success**, and there is no column for it at
   all — `folder` is only written into the log context.
3. **No delete confirmation** for a saved item, while the Delete panel does
   confirm counted deletes.
4. **`update_save_field` has no field allow-list** — it forwards `field`
   verbatim into `db.table("saved_items").update({field: value})`. It is a
   latent mass-assignment primitive that becomes reachable from model-authored
   input the moment a tool starts passing a field name.
5. **No test covers `do_rename` or `do_move`** (grep: they appear only as
   owners in `test_retrieve_owner_isolation.py`'s prose). The false success is
   therefore unguarded.

### 7. Existing capabilities (all verified in source)

- Deep Save (download → validate → re-upload) shared byte-for-byte by the
  panel, the link path and the AI tool.
- Owner isolation: `retrieve_service.load_saved_item` is the single
  owner-verified row read (checks `owner_id` **and** that
  `row["save_code"] == code`); `do_retrieve`/`do_delete` check ownership
  *before* any Telegram side effect and report a foreign row with the identical
  not-found wording.
- Stable, immutable short save codes with a unique index and a locked generator.
- Paginated browser, per-item detail panel, manual code entry, retrieve, delete.
- Recent list + ILIKE search, exposed both as panels (`list`, `find`) and AI
  tools (`list_saves`, `search`).
- Dashboard rendering of `media_type`, `save_code`, **`file_name`** and
  **`tags`** (`src/components/SavedItems.tsx`) — both are displayed today and
  `file_name` is always empty.
- `tags text[]` column with a DB default of `'{}'`.
- Input machinery that carries a per-action token in `pending["extra"]`.
- A deterministic fuzzy-match + numbered-clarification engine:
  `backend/ai/chat_resolution.py` (`resolve_chat_name`, `resolve_sender_name`,
  `format_clarification_options`) — exact → containment → character-overlap
  scoring, a strong-match threshold, an 8-option numbered list, fail-closed
  ambiguity. **It resolves Telegram chats/senders for Taskloom, not saved
  items.**
- `db_client.update_save_field` — a working, owner-scoped single-field UPDATE.
- `{"action":"clarify"}` for an honest “I need more information” model outcome.

### 8. Missing capabilities

1. No user-facing name for a saved item (`_display_name` returns `media_type`).
2. No user-supplied tags — the `tags` column is occupied by synthetic hashtags.
3. No way for AI Save to accept a name, caption or tags.
4. No search/resolution over `display_name`, `file_name` or `tags`; search is a
   single un-tokenised substring over caption/code/mime.
5. No semantic saved-item resolution with deterministic 0 / 1 / N semantics.
6. No structured candidate signal — `search` returns prose with `success=True`.
7. No saved-item selection UI (the chat/sender numbered-list engine is not wired
   to saved items).
8. Rename and Move do not persist; Delete has no confirmation.
9. No dedupe: one source message can be saved repeatedly.
10. Dead weight: `file_name`, `short_code` and 5 trigram indexes no query uses.

### 9. Root causes / architectural gaps

1. **Identity and label were never separated.** `save_code` is the only handle
   the AI has, so every retrieval must round-trip through an opaque code, and
   the human-readable label degrades to `media_type`. A *label* concept was
   never modelled — that is the single root cause of the whole phase.
2. **The natural tag column was pre-consumed.** `build_tags` fills `tags` with
   date/type hashtags, so the obvious home for owner tags already contains
   values that are noise for owner retrieval (`#saved_2026_09_20` never helps
   “university”), and the search layer never looks at the column anyway.
3. **`file_name` was added and never used.** The one field that could have held
   a name is dead, and the Deep Save payload omits it — so the placeholder
   exists but carries no data.
4. **Search targets the wrong columns with the wrong matcher.** The query is one
   literal `ILIKE` against `caption` (a rendered block + the *source* text),
   `save_code` and `mime_type`; multi-word semantic queries essentially cannot
   match.
5. **No typed ambiguity channel for saved items.** The tool contract is
   `(success, message, data)` and `data` is unused for search, so “N matches”
   cannot be represented — a deterministic 0/1/N contract needs a bounded
   candidate list, and the selection round-trip needs a mechanism that costs no
   extra model generation. The repo already contains that *pattern*
   (`chat_resolution` + numbered options) — it is simply bound to other
   entities.
6. **The metadata write path exists and is unused.** `update_save_field` works;
   `do_rename`/`do_move` never call it, which is why they report success and
   change nothing.

### Documentation drift found in `DATABASE_ARCHITECTURE.md` §2 (recorded, NOT fixed)

§2's `saved_items` table contradicts the canonical SQL in the same document
(§30) and the migrations:

| §2 claims | Reality (canonical SQL / migrations) |
|---|---|
| `idx_saved_items_save_type` exists | **Exists in no SQL file** — the identifier appears only in §2. |
| CHECK `saved_items_save_type_check: save_type IN ('forward','deep')` | **No CHECK** — only the superseded `20260712234229` had one; `20260714111706` and the canonical SQL both state this explicitly. |
| `save_type` default `—` | `DEFAULT 'forward'`. |
| `tags` default `—` | `DEFAULT '{}'`. |
| `owner_id` default `0` | No default; backfilled to `0` once, then `SET NOT NULL`. |

These do not affect runtime behavior, but they matter for Save V2 because the
phase adds a column to this table and will re-read §2 as the contract.

### 10. Display-name recommendation

**Answer: it requires a schema addition — one new nullable `text` column
`display_name` on `saved_items`. No existing field can carry it safely.**

| Candidate | Verdict |
|---|---|
| `caption` | **No.** It is a rendered multi-line block (icon, code, sender, timestamp, `chat/msg` ids, type, size, mime, filename, hashtags, then the *entire* source text) and retrieval injects it into the delivered caption. Using it as a name would mean parsing and overwriting a field that has a rendering contract, and every legacy row would expose a paragraph as its “name”. |
| `file_name` | **No, deliberately.** It is (a) dead today, (b) semantically the SOURCE filename — Telegram's, or the generated `photo_<code>.jpg` / `<code>.bin` from `generate_filename`, which Save V2 must keep distinct from an owner label, and (c) already documented as a drop candidate in §19.7. Repurposing it would silently invalidate that cleanup plan and make “original filename” unavailable for display or search. Keep the two separate. |
| `media_type` | **No.** An enum-like label (`Photo`/`Document`), not owner data. It is exactly the value `_display_name` wrongly returns today. |
| `save_code` | **No, never.** Immutable identity; changing a label must never touch it. |
| `tags` | **No.** Different concept; see §11. |

**Recommended shape:** `display_name text` — nullable, **no default**, never
auto-populated. `NULL` means “no owner name” and display falls back to the
source filename, then to the media type. Auto-filling it (e.g. from the caption)
would violate the owner's explicit rule: do not invent metadata.

### 11. Tag recommendation

**Answer: no schema addition — reuse the existing `tags text[]` column and
change only its producer.**

| Option | Verdict |
|---|---|
| `text[]` (existing) | **Recommended.** Right shape for a small ordered set of short strings, already rendered by the dashboard (`item.tags.map(...)`), no join, and `tags` is a single column addition *free* — the column is already there. |
| `JSONB` | No benefit. Nothing needs nesting; tags are a flat list, and JSONB would break the existing dashboard rendering and the DB default. |
| Normalized child table (`saved_item_tags`) | Would allow tag-level indexing/counts, but adds a table, an FK, a cascade policy, a migration and an N+1 fetch for a single-owner self-bot whose `saved_items` volume is small. Premature per `AGENTS.md` §13.7. |
| Reuse another metadata field | None exists that is not already spoken for (§10). |

**Required behavior change:** `build_tags()` must stop feeding the `tags`
column. Recommended minimal split:

- keep the synthetic hashtag line **in the caption** (visual parity with
  existing rows, no regression), and
- make `saved_items.tags` **exclusively owner-supplied**, defaulting to `[]`
  when the owner gave none.

This satisfies “if the user does not explicitly provide tags, the system must
NOT invent tags” while keeping the caption unchanged. **Existing legacy rows
keep their hashtag values** — no data migration, no truncation, no destructive
update; the resolver can simply ignore entries starting with `#` if desired.

### 12. Retrieval / search recommendation

Introduce **one deterministic resolver** in the service layer, e.g.
`retrieve_service.resolve_saved_items(owner_id, query, limit=8)` returning a
**typed, bounded candidate list** (never raw rows, never internal `id`s):
`{save_code, display_name, file_name, media_type, tags, created_at}`.

**Matching order (deterministic, no scoring model, no ranking-by-quality):**

1. exact `save_code` match (case-insensitive, upper-cased first);
2. exact `display_name` match (after normalisation);
3. exact tag match (case-insensitive, set membership);
4. exact `file_name` match;
5. **token-AND substring** — split the query on whitespace and require every
   token to appear in at least one of `display_name`, `file_name`, `caption`,
   `tags` (this is the key correctness improvement over today's single
   substring);
6. stable tie-break: `created_at DESC`, then `save_code`.

Bound the result at a small N (8 matches the existing clarification cap in
`chat_resolution`). Never return the whole table; always filter on `owner_id`
inside the DB query and always `ORDER BY` + `LIMIT` so the existing
`idx_saved_items_owner_created` is used. Do **not** change `search_saves` /
`do_find` semantics in the first phase — keep the AI `search` tool and the
`Find` panel byte-compatible.

**Indexing:** at the expected volume (a personal account; the dashboard pages 50
and the panel 8) an owner-scoped, `LIMIT`ed deterministic scan is fine and
`pg_trgm` is already installed. If volume grows, the only indexes worth adding
are `(owner_id, display_name)` btree and a GIN index on `tags`. **No embeddings,
no vector database, no full-text search engine in this phase.**

### 13. Ambiguity-handling recommendation

**Contract:** `0` → honest not-found (same wording as today's
`❌ No item found for …`, fail closed); `1` → act directly, no clarification;
`N` → present a numbered list and require an explicit selection.

Reuse the existing pattern rather than inventing one:
`backend/ai/chat_resolution.py` already ranks candidates, caps the list at 8 and
formats a numbered prompt (`format_clarification_options`). Bind the same shape
to saved items.

**Carrying the selection without extra cost:**

- **Manual UI:** the candidates are a panel page whose rows are
  `panel:retrieve_item:id:{save_code}`. The code **is** the selection token and
  already fits Telegram's 64-byte callback limit (23 bytes). No new mechanism,
  no name in callback data.
- **AI path:** the tool returns
  `ToolResult(success=True, message=<numbered list>` with
  `data={"outcome":"multiple","query":…,"candidates":[{"code","label"}…]})`
  and the assistant relays the list through the existing `clarify` route; the
owner's next message names one item and the model re-issues `retrieve_save` with
  a **real** code. A fully deterministic one-turn round-trip (a short-lived
  pending-input holding the candidate list, answered with “2”) is a legitimate
  later refinement, not a requirement for correctness.

Hard rules: candidates come **only** from the deterministic resolver (the model
may never invent one or pick on its own); internal DB `id`s are never exposed;
`save_code` remains the single public handle; nothing beyond
label/type/date is shown.

### 14. AI / manual unification recommendation

One metadata model, one writer:

1. Extend **`save_service.execute_save`** with keyword-only
   `display_name=None, tags=None` (the existing positional signature
   `(client, owner_id, reply_msg, tz_str)` stays unchanged) and
   add `display_name` to the DB payload. `None` must reproduce today's behavior
   **exactly** for every existing caller (the panel reply path, the link path,
   `SaveTool`).
2. Stop writing synthetic hashtags into `tags`; `tags=None` → `[]`; `tags=[…]` →
   the owner's list, normalised (trim, drop empties, dedupe, bounded count and
   per-tag length).
3. Add `name` and `tags` as **optional** fields to the AI save path:
   `actions.ALLOWED_FIELDS`, the `save`/`deep_save` validation branch,
   `resolve_tool_calls`, `SaveTool.parameters` and the prompt template's JSON
   examples must all change **in the same commit** — `validate_action` rejects
   any unknown field, so a partial change silently breaks the action.
4. Keep manual Save's default frictionless: replying after Reply Mode saves
   immediately with no metadata (today's behavior); add an explicit optional
   “✏ Name & tags” action that opens the existing input machinery. **Never** open
   a mandatory prompt on the plain save path.
5. Both paths call the **same** `execute_save`; there is no second pipeline,
   no second model and no second store.

### 15. Security considerations

- **Owner scoping must move into the query.** `db_client.query_save` is a
  code-only lookup; today the owner check lives in the service. The new resolver
  must apply `eq("owner_id", owner_id)` **in the database query** and still
  re-verify in the service, never only afterwards.
- **`update_save_field` must be called with fixed literal field names.** It has
  no allow-list; the moment a model-derived field name can reach it, it becomes
  mass assignment. Either add a closed allow-list for
  `{display_name, tags, file_name}` or add dedicated typed writers.
- **The model never sees the table or its ids.** Only `save_code` + label + type
  + date may cross the boundary.
- **Callback payloads must carry codes, never names or labels.** Rendering a
  name *as a button label* is fine; the *data* stays the code/handle.
- **Saved-item text is untrusted.** A `caption` embeds the source message's own
  text; the prompt already classifies Telegram message content as untrusted
  data, and resolver output must never be placed in an instruction position.
- **No secret/credential surface is involved.** Save V2 must not touch the
  Vault/credential architecture, must not add env vars, and must not put
  anything sensitive into a caption or a callback payload.

### 16. Performance considerations

- `saved_items` is owner-scoped and small; every new query must keep
  `owner_id` + `ORDER BY created_at DESC` + `LIMIT` so
  `idx_saved_items_owner_created` serves it.
- One extra nullable `text` column costs nothing for reads or the dashboard
  (`/api/saves` already pages 50 rows).
- Token-AND matching over a bounded candidate window is O(candidates), and the
  window is bounded by `owner_id` + `LIMIT` — no full-table scan on the hot
  path.
- Optional later indexes only: `(owner_id, display_name)` btree and/or a GIN
  index on `tags`; `pg_trgm` is already available for substring acceleration.
- No new dependency, no new background worker, no cache, no embedding store.

### 17. Edge cases (analysis only — none implemented)

| # | Case | Deterministic behavior to specify |
|---|---|---|
| 1 | No display name | `display_name IS NULL` → display falls back to source filename → media type; matching never treats NULL as a value |
| 2 | Empty display name | Rejected at validation; an empty/whitespace name is stored as NULL, never `""` |
| 3 | Very long display name | Bounded constant (recommend ≤120 chars, comparable to the existing `_MAX_CHAT_NAME_CHARS`), reject **without silent truncation** |
| 4 | Duplicate display names | Allowed (no unique constraint); resolution returns N → clarification |
| 5 | Similar names | Token-AND + normalisation; if 2+ survive → clarification |
| 6 | Same name, different tags | Distinct items; tags appear in the candidate label so the owner can tell them apart |
| 7 | No tags | Stored `[]`; never invented |
| 8 | One tag / 9. multiple tags | Stored verbatim; bounded count; deduped case-insensitively |
| 10 | Duplicate tags | Deduped at validation |
| 11 | Case differences | Case-insensitive matching; the stored casing is preserved |
| 12 | Persian/English mixed names | Normalisation must fold Arabic/Persian variants (`ي`→`ی`, `ك`→`ک`, `ة`/`ه`, ZWNJ/space collapse) — `chat_resolution._normalize` today only lowercases and collapses separators, so this is a **gap to close** |
| 13 | Persian tags | Same treatment; tags are just normalised strings |
| 14 | Existing files with no new metadata | Legacy rows: `display_name` NULL, `tags` holds hashtags; they must remain fully retrievable by code — no backfill, no rewrite |
| 15 | Multiple matches | Clarification (§13), never a silent pick |
| 16 | Zero matches | Clean not-found; the AI path must report a failure/negative result, not a success string (today `do_find` returns `success=True` for “no matches”) |
| 17 | Existing `save_code` retrieval | Must keep working byte-for-byte, including `save_code`-only paths |
| 18 | Deleted Telegram source | Irrelevant to resolution (Deep Save already re-uploaded); retrieval may still fail — keep today's honest failure |
| 19 | Saved row exists but source fetch fails | Retrieval fails honestly; resolution is a DB-only operation and must not depend on Telegram |
| 20 | Duplicate files, different names | Two codes, two names; both match → clarification |
| 21 | Rename after retrieval | Rename writes `display_name` only; `save_code`, `saved_msg_id`, `caption` semantics are untouched |
| 22 | Delete after rename | Straight delete by code; no dependency on the name |
| 23 | Tag edit after rename | Independent fields; each write is a single-field update |
| 24 | AI Save and manual Save producing identical metadata | Same `execute_save` → identical rows (this is the unification acceptance test) |
| 25 | Owner explicitly says “no tags” | `tags = []` stored; never populated with a default invention |
| 26 | Owner explicitly says “no custom name” | `display_name = NULL`; the item stays code/filename/media-type addressed |
| 27 | “save this” with no name | Works exactly as today — no extra prompt, no invented name |
| 28 | “save this as X” | `name=X` passed through; `save_code` unaffected |
| 29 | “save this with tags X Y” | `tags=[X, Y]` |
| 30 | “save this, no tags” | `tags=[]` |
| 31 | “the file for university” | Resolver → 0 / 1 / N; never a guess |
| 32 | Multiple university files | → clarification with numbered candidates carrying codes |

### 18. Proposed implementation phases

| Phase | Scope | Depends on |
|---|---|---|
| **A — data model** | New forward-only migration adding `display_name text` (nullable, no default) to `saved_items`, idempotent (`ADD COLUMN IF NOT EXISTS`), non-destructive; document it in `DATABASE_ARCHITECTURE.md` §2 + the migration-status table + the canonical reconciliation SQL. **No SQL is executed by the implementer.** | — |
| **B — shared metadata model** | `execute_save(..., *, display_name=None, tags=None)`; include `display_name` in the payload; stop persisting synthetic hashtags (keep them in the caption); normalise `tags`. Update the one existing test that asserts `row["tags"]` is truthy. | A |
| **C — manual Save** | Optional “Name & tags” step in the Save panel, routed through the existing input machinery; the plain reply path stays prompt-free. | B |
| **D — manual management UI** | Fix `do_rename`/`do_move` to persist via an allow-listed `update_save_field`; show `display_name` in the browser/detail with the filename→media-type fallback; edit name/tags from Item Detail; add a real delete confirmation. | A, B |
| **E — AI Save** | Optional `name`/`tags` in `ALLOWED_FIELDS`, `validate_action`, `resolve_tool_calls`, `SaveTool.parameters` and the prompt template — one atomic change. | B |
| **F — AI retrieval + ambiguity** | `resolve_saved_items` + typed candidate `data`; 0/1/N semantics; a name query that resolves without a literal code (extend `retrieve_save`/`preview_save`/`delete_save`, or add one `find_save` tool); numbered clarification reusing `chat_resolution`'s shape. | A, B, D |
| **G — integration + regression** | Cross-surface tests (AI ↔ manual produce identical rows; ambiguity round-trip; legacy rows untouched). | A–F |

Each phase is independently committable; none may alter `save_code` semantics, the
Deep Save order, or owner isolation.

### 19. Exact files expected to change

| File | Expected change |
|---|---|
| `supabase/migrations/<ts>_add_saved_items_display_name.sql` | **NEW** — idempotent `ADD COLUMN IF NOT EXISTS display_name text` |
| `backend/services/save_service.py` | `execute_save` optional `display_name`/`tags`; payload gains `display_name`; `build_tags` stops feeding `tags` |
| `backend/services/retrieve_service.py` | `_display_name` → real label with fallback; `do_rename`/`do_move` persist; new `resolve_saved_items`; preview shows name/tags |
| `backend/db/client.py` | search/resolve over `display_name`/`tags`/`file_name`; field allow-list for updates |
| `backend/services/discover_service.py` | entries render the display name |
| `backend/bot/handlers/save.py` | optional Name & tags step |
| `backend/bot/handlers/retrieve.py` | browser label, rename/move persistence, tag editing, delete confirmation |
| `backend/ai/tools/save.py` | optional `name`/`tags` parameters + pass-through |
| `backend/ai/tools/retrieve_save.py` | candidate `data` / name resolution |
| `backend/ai/actions.py` | `ALLOWED_FIELDS`, validation branch, `resolve_tool_calls` |
| `backend/ai/prompt/template.py` | schema examples for the new optional fields |
| `DATABASE_ARCHITECTURE.md` | §2 column/index/notes, migration-status row, canonical reconciliation SQL (and the §2 drift in this report) |
| `IMPLEMENTATION_REPORT.md` | phase report |
| `tests/test_12_save_engine.py`, `tests/test_saved_item_preview_metadata.py`, `tests/test_saved_items_ai_management.py`, `tests/test_retrieve_owner_isolation.py`, `tests/test_canonical_schema_reconciliation.py`, `tests/test_capability_exposure_tools.py`, `tests/test_tool_health_audit.py` | updated expectations (column list, tool inventory) |
| NEW tests, e.g. `tests/test_save_v2_metadata.py`, `tests/test_save_v2_resolution.py` | metadata write/follow-through, resolver 0/1/N, owner isolation, no-invented-tags, legacy-row compatibility |

### 20. Explicit items that must remain untouched

- `save_code` generation, format, uniqueness and immutability; no existing code
  is regenerated or rewritten.
- The Deep Save order (download → validate → upload → DB) and the invariant that
  **Save never forwards**.
- `forward_messages` stays confined to `retrieve_service.do_retrieve`.
- Owner-isolation semantics and their exact user-visible wording.
- `do_preview`'s read-only contract (pinned by
  `test_saved_item_preview_metadata.py`).
- STT, TTS, OCR, media analysis, Native Vision, Video/GIF.
- `RuntimeSupervisor`, the dispatcher's control flow, `ToolExecutor`, the task
  scheduler and the credential/Vault architecture.
- Every DB object except the single new `saved_items.display_name` column; no
  SQL execution, no Vault change, no `DATABASE_ARCHITECTURE.md` rewrite beyond
  the documented §2 correction.
- `render.yaml`, `requirements.txt` (no new dependency), and the
  `bot_settings`/`panel_settings`/`ai_config` configuration systems — Save V2
  introduces **no** env var and no new setting store.

### Validation performed by this investigation

| Check | Command | Result |
|---|---|---|
| Save-focused suites | `pytest tests/test_12_save_engine.py tests/test_saved_items_ai_management.py tests/test_save_code_grammar.py tests/test_saved_item_preview_metadata.py tests/test_saved_item_sender_and_retrieve.py tests/test_retrieve_owner_isolation.py tests/test_54_ghost_seen_v2_stage3.py -q` | **147 passed** in 0.63 s |
| Full suite (baseline at `ecdfdf0`) | `pytest tests/ -q` | **4362 passed, 26 skipped** in 115.71 s |
| Working tree before / after | `git status --porcelain` | clean / only this report modified |

**Not performed, and therefore not claimed:** live Telegram verification, live
Supabase verification, any schema change, any migration execution, any
implementation. The test numbers above are the *unmodified* baseline; no test
was added, changed or removed by this task.

### Test-coverage gaps found (for the implementation phase)

- `do_rename` / `do_move` are untested — the false success in §6 has no guard.
- No test asserts `saved_items.tags` is **not** auto-invented
  (`test_deep_save_persists_full_metadata` asserts the opposite:
  `assert row["tags"]`).
- No test covers name/tag search, or 0/1/N resolution.
- No test covers `file_name` never being written.
- `test_canonical_schema_reconciliation.py` hard-codes the `saved_items` column
  set — a new column will require the fixture and the expectation to move with
  it.

### Exact next phase

A separate implementation prompt executing **Phase A** (the `display_name`
column + its documentation) — deliberately the smallest change, because every
later phase depends on the column existing and nothing else in the Save system
needs to change for it. Phases B–G follow in order.

## Previous phase — Database architecture repair: canonical schema reconciliation

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
