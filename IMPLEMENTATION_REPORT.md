# IMPLEMENTATION REPORT — CURRENT STATE

## Latest phase — M2.0: Speech-to-Text control-plane foundation (AI → Media Analysis)

Repository `Onlyicing1/Telegram-self-bot` · branch `main` · state as of 2026-09-17.

### Current stage

M2.0 — **Phase 1 of the Speech-to-Text control plane only.** The STT
configuration is no longer three free-form fields inside **AI → Settings →
Advanced**; it is a capability under the new **AI → Media Analysis** surface,
and the model is no longer something the owner types.

```
AI                                    (ai)
├── Media Analysis                    (ai_media)
│   ├── Text recognition (OCR)        (ai_media_ocr)
│   └── Speech-to-Text                (ai_media_stt)
│       ├── pick a REGISTERED candidate   → action:ai_stt_select_candidate:<candidate-id>
│       ├── Language…                     → input:ai_media_stt:stt_language
│       └── Recognition passes…           → input:ai_media_stt:stt_passes
├── Settings                          (ai_settings)   — STT controls REMOVED
│   └── Advanced                      (ai_settings_adv) — STT controls REMOVED
└── … (provider, model, usage, health, details, diagnostics: unchanged)
```

| Item | Value |
|---|---|
| **Phase** | M2.0 — STT control-plane foundation (registry + configuration model + Media Analysis surface) |
| **Starting HEAD** | `4a1f9a241d76da27fab38c7a25f8a19f81a4fabe` — `added web investigation report file` (== `origin/main`; working tree clean at the start of this phase) |
| **Implementation commit** | the single commit of this phase (`git log -1 --format=%H` re-verifies it; recorded in the hand-off response) |
| **Database migration required** | **NO** — the existing `ai_config` columns are sufficient (see *Persistence*); no SQL was written, invented or executed |
| **New tables** | **NONE** |
| **New provider adapters** | **NONE** (Speechmatics / Groq Whisper are registered as *unimplemented capabilities only*) |
| **STT execution path** | **UNCHANGED** (only the control-plane → engine conversion was inserted in front of the existing seam) |
| **Live Telegram verification** | **NOT PERFORMED** (no session or traffic in this environment) |
| **Live STT provider verification** | **NOT PERFORMED** (no provider request is made anywhere in this phase) |

### Commit lineage

| Commit | Role |
|---|---|
| `01ff211` `feat(stt): add the bounded multi-pass STT accuracy seam` | M1.7e — the opt-in STT-only consensus seam |
| (M1.8 commit) | M1.8 — the owner-managed Gemini STT settings (`stt_model` / `stt_language` / `stt_passes`), their persistence, their Telegram controls (AI → Settings → Advanced) and their runtime application |
| `4a1f9a2` `added web investigation report file` | the web research report — **starting HEAD of this phase** |
| the commit of this phase | M2.0 — the STT control plane: candidate registry, structured configuration, the AI → Media Analysis surface, and this report |

### Files changed by this phase

| File | Change |
|---|---|
| `backend/ai/stt_control_plane.py` **(new)** | the capability registry (`SttCandidate`, `STT_CANDIDATES`), the configuration model (`SttControlPlane`), `parse_stt_config()`, the persistence mapping (`storage_value()`, `engine_settings()`) — stateless, no Telegram/DB/network/boundary imports |
| `backend/bot/handlers/ai_stt_settings.py` | becomes the **Media Analysis** surface: the `ai_media` / `ai_media_ocr` / `ai_media_stt` panels, the candidate-selection action, the two bounded inputs (language, passes) and the runtime hand-off. The free-form model input is **deleted** |
| `backend/bot/handlers/ai.py` | the `▣ Media Analysis` entry on the AI main panel; the STT state line removed from **Settings** and the three STT rows/values removed from **Advanced**; module docstring updated |
| `backend/ai/config_store.py` | documentation only — the STT key comments now describe the control-plane semantics (registered candidate id / legacy-unresolved) and the unchanged column set. **No behavior change** |
| `backend/runtime/supervisor.py` | `_apply_persisted_stt_settings()` now resolves the stored selection through `stt_control_plane.engine_settings()` before handing it to the existing engine seam (the ONE compatibility hook) |
| `tests/test_ai_stt_settings.py` | rewritten/extended (87 tests) for the control plane, the Media Analysis surface, the move out of Settings/Advanced, legacy compatibility and the engine conversion |
| `tests/test_36_ai_settings_ux.py` | the registration test now asserts 6 AI-Settings inputs (STT gone) and the two `ai_media_stt` inputs |
| `IMPLEMENTATION_REPORT.md` | this report |

**Untouched (deliberately):** `backend/services/media_service.py` (seam and
execution behavior), `backend/services/gemini_media_engine.py` (transports,
`apply_stt_settings`, `stt_settings_from`, `STT_MAX_PASSES`),
`backend/services/stt_consensus.py`, `backend/bot/handlers/ai_unified.py`,
`backend/ai/engine/dispatcher.py`, the provider adapters and `ProviderManager`,
the tool registry/executor, `backend/ai/media.py`,
`backend/telegram_api/media.py`, `media_ai_service.py`, the Save/Task/Scheduler
paths, OCR behavior, the panel infrastructure, `requirements.txt`,
`render.yaml`, `Procfile`, `supabase/migrations/*.sql`, `DATABASE_ARCHITECTURE.md`,
ENV files and all secrets.

### What was implemented

1. **A capability-specific candidate registry** (`stt_control_plane.STT_CANDIDATES`)
   — a deterministic tuple of `SttCandidate(candidate_id, provider, model, label,
   implemented, note)`. Identity is `provider:model`; `implemented` separates
   "this project knows the capability exists" from "this project can run it
   today". The registry is the ONLY source of candidates, which is what removes
   manual model entry:

   | Candidate | Provider | Model | Implemented |
   |---|---|---|---|
   | `gemini:default` | gemini | *(provider default → general media model)* | **yes** |
   | `gemini:gemini-3.5-transcribe` | gemini | `gemini-3.5-transcribe` | **yes** |
   | `groq:whisper-large-v3` | groq | `whisper-large-v3` | no — registered capability only |
   | `groq:whisper-large-v3-turbo` | groq | `whisper-large-v3-turbo` | no — registered capability only |
   | `speechmatics:standard` | speechmatics | `standard` | no — registered capability only |

2. **A structured configuration model** (`SttControlPlane`) representing the
   available pool, the selected active candidate, the **ordered active-first
   fallback list**, the language preference, the bounded pass count, the
   legacy/unresolved state and the "selected but not executable" state.

3. **A new AI → Media Analysis surface** built from the project's existing panel
   / input / action registries (`register_panel` / `register_input` /
   `register_action` + `InlinePanelBuilder` + `_finish_input` → one edit per
   interaction, no new messages). No parallel Telegram UI framework, no second
   registry, no second store.

4. **The STT controls left AI → Settings and AI → Settings → Advanced** (state
   line and the three rows/inputs removed). Everything else on those two panels
   is preserved, and the eight remaining settings (wake words, reply stats, reply
   presentation, creativity, response length, conversation memory, personality
   prompt) behave exactly as before.

5. **A deterministic no-manual-entry selection path**: the STT panel offers one
   button per *implemented, non-active* registered candidate
   (`action:ai_stt_select_candidate:<candidate-id>`); the two remaining inputs
   are the bounded behavioral settings. A typed model identifier is no longer
   reachable anywhere in the surface.

6. **The control-plane → engine conversion** (`engine_settings()`), consumed by
   exactly two callers: the runtime supervisor at startup and the handlers after
   a save.

### Resulting architecture

```
Telegram UI (AI → Media Analysis → Speech-to-Text)
    ↓  a registered candidate id + language + passes
persisted owner configuration  (existing ai_config row, existing 3 keys)
    ↓
STT CONTROL PLANE  (backend/ai/stt_control_plane.py — stateless)
    ↓  active candidate + ordered fallback list + engine values
(a LATER phase: the candidate test / health / fallback manager)
    ↓
the EXISTING media boundary seam (media_service.set_stt_engine)
```

The control plane owns *configuration*; the media boundary keeps owning
*execution*. The control plane imports neither Telegram nor the database nor
`media_service`, and no Telegram object, owner id, chat id, message id, filename
or caption can cross into it (verified by test, `engine_settings()` carries
exactly three keys).

### Persistence behavior

* **Same store, same row, same keys.** The three values live on the owner's
  single existing `ai_config` row through `backend/ai/config_store.py` — the same
  upsert, the same defaults merge, the same in-memory fallback. No second store,
  no new table, **no new column**, no SQL.
* `stt_model` keeps its column but changes MEANING: empty = the default
  candidate, a *registered candidate id* = that candidate, anything else =
  legacy/unresolved. `storage_value()` maps the default candidate back to the
  empty string, so "nothing configured" and "the default" remain one state and
  the key never grows a new format.
* `stt_language` (empty = automatic) and `stt_passes` (integer, 1..3) are
  unchanged, still written by the same upsert payload, still merged by the same
  `_DEFAULTS`.
* A failed durable write still degrades to the documented in-memory fallback;
  a failed durable *read* is still flagged (`DEGRADED_READ_KEY`) and the
  supervisor still keeps the provisioned bootstrap settings instead of
  downgrading a configured model because the database blinked.

### Backward compatibility behavior

| Stored `stt_model` | Parsed as | Engine receives |
|---|---|---|
| empty / unset | the default candidate (`gemini:default`) | `""` → the general media route, byte-identical to before this phase |
| a registered candidate id | that candidate (active) | its model (`gemini-3.5-transcribe`) or `""` for the provider default |
| `gemini-2.5-pro` (or any other unregistered value) | **legacy / unresolved** — no candidate is selected, the pool is unranked | the stored value **verbatim** |

An unregistered value is **never** silently re-pointed at another registered
candidate, and a selected candidate that is not executable (registered, not
implemented) maps to the boundary's existing fail-closed "no dedicated STT
model" state and is labelled as such on the panel. The three `AI_GEMINI_STT_*`
ENV variables remain bootstrap-only, exactly as M1.8 documented.

### Tests and exact results

| Suite | Result |
|---|---|
| `tests/test_ai_stt_settings.py` (rewritten, 87 tests) | `87 passed` |
| `tests/test_36_ai_settings_ux.py`, `tests/test_11_runtime_wiring.py`, `tests/test_33_ai_telemetry.py`, `tests/test_ai_presentation_redesign.py` | `119 passed` |
| the nine media / STT boundary suites (`test_media_stt.py`, `test_media_dedicated_stt.py`, `test_media_stt_reliability.py`, `test_media_stt_multipass.py`, `test_media_stt_language.py`, `test_media_stt_benchmark.py`, `test_stt_consensus.py`, `test_media_gemini_engine.py`, `test_media_ai_integration.py`) | `442 passed` (unchanged — the boundary and the execution path are untouched) |
| **Full suite** | **`3600 passed, 24 skipped, 3 warnings` in 114.89 s** (pre-existing skips only; no test was deleted, weakened or skipped) |

The rewritten suite covers, among others: the registry's provider+model identity;
deterministic canonical order and a deterministic active-first fallback order;
persistence of the active candidate, the language and each pass count 1/2/3
through the existing config store; the 1..3 bound; the `auto` alias; refusal of
an unregistered candidate (both `storage_value()` and the panel's offered
candidates); legacy values that stay unresolved and keep the previous engine
behavior; the Media Analysis panel registering under `ai` with OCR and
Speech-to-Text under it; STT controls absent from `ai_settings` /
`ai_settings_adv` and from their rendered bodies/buttons; the untouched Advanced
controls; the two bounded inputs and their refusals; the candidate action
(selects implemented, refuses unregistered and unimplemented); the engine
receiving the resolved model; context isolation (the engine keeps exactly six
slots and none of the poisoned metadata is reachable); and the supervisor
startup hook with an unreadable store.

**Syntax / whitespace:** `python -m py_compile` clean on every changed Python
file; `git diff --check` clean.

### Live verification status

* **Telegram:** NOT performed. The panels, inputs and action were exercised
  against the real handler functions, the real panel/input/action registries and
  the real panel builders, but no Telegram session rendered them.
* **STT providers:** NOT performed — and out of scope: this phase makes no
  provider request at all, and neither credential presence nor reachability is
  measured anywhere.

Neither status may be reported as success.

### Intentionally NOT implemented in this phase

* **Speechmatics** (no adapter, no `AI_SPEECHMATICS_API_KEY`, no request) — only
  a registered, non-selectable capability entry.
* **Groq Whisper** (no transcription adapter, no request) — same, capability
  entries only. A general Groq *chat* provider existing in the repository is not
  evidence of an STT capability and was not treated as one.
* **Any new STT provider adapter or transport.**
* **Runtime fallback / retry orchestration / cooldown / concurrency** — the
  configuration can *represent* an ordered candidate list, but nothing executes
  a failover yet.
* **The provider/model test manager** (reachability, latency, failure class,
  cooldown, last test result) — only the data model it will need was
  established, and credential-presence is deliberately kept distinct from
  "tested and usable".
* **Any change to OCR behavior, the media boundary, the dispatcher, the tool
  layer, `ProviderManager`, the scheduler, `RuntimeSupervisor` recovery or the
  Supabase schema.**

### Known limitations

1. **`DATABASE_ARCHITECTURE.md` §7 was intentionally NOT edited.** The schema is
   unchanged, so no schema documentation change was required by this phase's
   rules; the `stt_model` row there still describes the M1.8 semantics ("an
   opaque typed model id, edited from AI → Settings → Advanced") and is now
   **superseded by this report** until a documentation-only update is made.
2. The fallback order is derived from the canonical registry order rather than
   stored per-owner, so it cannot yet be re-ranked by the owner. That belongs to
   the later test/fallback phase.
3. `groq:*` and `speechmatics:*` appear in the pool as `not available yet`; they
   are honest placeholders, not usable engines.
4. The M1.8 note stands: the `ai_config` STT columns require the pending manual
   migration; until it is applied the settings degrade to the in-memory fallback.
5. Recognition quality (class A) remains unmeasured.

### Deferred work

* The **STT provider/model test manager** (reachability, latency, failure class,
  cooldown, last-test result) and the credential discovery that feeds it.
* The **runtime fallback executor** (primary → next active candidate → honest
  failure) in front of the existing media boundary seam.
* The **Speechmatics and Groq Whisper adapters** (and their ENV credentials).
* Per-owner fallback re-ranking, if it is wanted.
* Applying the pending `ai_config` migration and the documentation-only
  `DATABASE_ARCHITECTURE.md` §7 semantics refresh.
* Class A (recognition quality) measurement — `INVESTIGATION.md` §19 stays open.

### Explicit next stage — M2.1: the STT provider test manager

1. Add the credential discovery for the registered candidates (ENV only, never
   Telegram/Supabase) and expose `credential present` **separately** from
   `tested`.
2. Add the bounded, on-demand "Test STT providers" action that probes each
   implemented candidate once, with a failure class, latency and a bounded
   cooldown, surfaced through the existing Media Analysis panel.
3. Only after that, add the runtime fallback executor in front of the existing
   `media_service.set_stt_engine` seam — one candidate at a time, honest failure
   at the end.

### Document version

This document reflects the M2.0 state: Speech-to-Text is a capability under
**AI → Media Analysis**, configured by picking a REGISTERED candidate from the
control plane's registry plus a bounded language and pass count; the values are
persisted on the owner's existing `ai_config` row with no schema change; an
unregistered stored value stays legacy/unresolved and keeps its previous engine
behavior; and the media boundary, the execution path and every provider adapter
are unchanged. Provider adapters and runtime fallback execution are deferred to
the next phase. If code changes invalidate any section, update this document in
the same commit.
