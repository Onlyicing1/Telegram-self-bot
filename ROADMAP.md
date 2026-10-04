# MASTER PROJECT ROADMAP

> **Status:** planning document — derived from the repository at commit
> `a317bc8e23edce5388e264442902b27739d6acf1` (branch `main`, equal to
> `origin/main` at derivation time). No implementation stage was executed to
> produce this document; no production code, test, migration, or
> `DATABASE_ARCHITECTURE.md` content was modified.
>
> **Why this document exists.** The repository had no authoritative roadmap.
> `IMPLEMENTATION_REPORT.md` recorded only *"Stage completed: Stage 6 / Next
> stage: Stage 7"* — a number with no content — and every subsystem carried its
> own local stage names (Taskloom Parts 1–3F, Ghost Seen v2 stages 1–8, Media
> M1.x/M2.x). Without one standing document, every future session would
> rediscover the project state from zero. This file is that document.
>
> **Derivation, not inheritance.** Stage 5 (commit `eb4d852`) and Stage 6
> (commit `a317bc8`) are the only project-wide stages the repository records.
> Everything after Stage 6 below is **derived from verified remaining work**
> and labelled with its confidence in §8 — it was not inherited from any old
> plan and must not be treated as pre-existing project truth.
>
> **Evidence hierarchy used:** current source code → current architecture
> documents (`AGENTS.md`, `DATABASE_ARCHITECTURE.md`, `AI_MASTER_DESIGN.md`) →
> current `IMPLEMENTATION_REPORT.md` → current `INVESTIGATION.md` → tests →
> historical documents only to reconstruct intent. The current
> `INVESTIGATION.md` (roadmap investigation) records the evidence and the
> A–G classification of every finding.
>
> **Architecture guardrails (apply to every remaining stage).** AI is the sole
> interpreter of natural-language intent. No regex/keyword semantic routing, no
> intent classifiers, no deterministic natural-language fast paths. `ToolRegistry`
> is the AI capability allowlist; `ToolExecutor` is the only component that
> calls `tool.execute()`; `ProviderManager` owns provider selection/fallback/
> retry/cooldown; `RuntimeSupervisor` owns runtime lifecycle; there is no second
> scheduler, executor, or update loop. The AI never receives arbitrary Telegram
> RPC, SQL, shell, or unrestricted HTTP. Supabase schema changes are applied
> manually by the owner; `DATABASE_ARCHITECTURE.md` is protected. Render Free
> has no shell — no stage may require one. Media-analysis and Saved-Items target
> resolution keep their context-isolation boundary. Removed architecture must
> not be resurrected: the deterministic semantic router (Stage 5), forward-save,
> legacy dot commands, and Ghost Seen v1 are gone; Ghost Seen v2 is the current
> implementation.

---

## 1. PROJECT DEFINITION OF DONE

**Project goal (as the repository states it):** a headless Telegram self-bot
that turns the owner's own Telegram account into a personal operating system —
Glass UI panels plus a single natural-language AI execution interface
(`Nova`), with durable saves, profile automation, scheduled task workflows,
media analysis, and a self-healing runtime, deployed on Render.

The project is complete only when all of the following are true. Every clause
names its evidence source.

**1.1 Capability completeness.** Every capability registered in the current
tree is either:

1. implemented in source, covered by the test suite, **and** exercised
   end-to-end on the live owner account at least once with recorded evidence
   (Telegram + live Supabase); or
2. explicitly frozen/deferred by a recorded owner decision in this roadmap
   (§7), with the reason and reactivation conditions written down.

Capabilities that currently fail 1(a) are listed in §2.4.

**1.2 AI interface verified live.** The AI — the only natural-language
interface — selects and executes the correct tool for the diagnostic classes
defined in Stage 7, using real providers and models, with recorded per-class
results. Deterministic code continues to validate, bound, authorize, and
execute the model's decision; no routing logic exists locally.

**1.3 Durable state verified live.** The live Supabase database matches the
canonical contract in `DATABASE_ARCHITECTURE.md` §31 (the ONE setup block,
parts 1–8) with no pending objects, and every durable path (saves, profile
state, AI sessions/memories/usage, tasks/occurrences, vault) has been written
and read against it. The in-memory fallback path is verified to degrade
honestly and never crash.

**1.4 Durable workflows verified live.** The single `TaskScheduler` →
`TaskExecutionCoordinator` → `ToolExecutor` path is verified end-to-end on the
live account for each bounded capability (chains, waits, conditional branches,
question/answer parks, restart recovery), with no duplicate execution across
restarts.

**1.5 Production runtime verified.** The application boots on the declared
production runtime (Render Free, Python 3.11.7), serves `/health`, runs its
supervisor (heartbeat, keepalive, failsafe) without a shell, and survives a
restart/redeploy. Local/declared Python parity is resolved or its risk is
recorded.

**1.6 Documentation truth.** `AGENTS.md` (including its delivery-state table),
`README.md`, `IMPLEMENTATION_REPORT.md`, `INVESTIGATION.md`, `ROADMAP.md`, and
`DATABASE_ARCHITECTURE.md` statuses agree with the source. No document claims
an unapplied migration is applied or an unverified behavior is verified. No
removed architecture is described as current.

**1.7 Test truth.** The full deterministic suite passes
(`pytest tests/ -q`, run from the repository root), and every opt-in live suite
(Supabase memory, provider probes, any new live harness) has an honest recorded
outcome — run where credentials exist, or skipped with the reason recorded.

**1.8 Deferred items resolved.** Every item in §7 has an owner decision:
implemented, or accepted as out of scope with the decision recorded. "No
decision" is not a completion state.

---

## 2. VERIFIED CURRENT STATE

### 2.1 Latest verified completed stage and repository state

| Item | Value |
|---|---|
| Latest completed project-wide stage | **Stage 6** — AI→tools contract, context budget, and tool-use policy repair (commit `a317bc8`, 2026-10-04) |
| Stage 6 verification | Unit/integration-verified; recorded full suite **5158 passed, 26 skipped** (`pytest tests/ -q`, ~118 s). Live verification: **not performed** |
| Branch | `main` |
| Local HEAD / `origin/main` | `a317bc8e23edce5388e264442902b27739d6acf1` (equal when this roadmap was derived; re-verify before any delivery claim) |
| Working tree | clean except the pre-existing untracked `telegram-self-bot/` (never staged) |
| Local runtime used for verification | Python 3.10.12 (`/home/daytona/codebase/.venv`); production declares 3.11.7 (`render.yaml`) |
| Tests | 204 test modules under `tests/` |
| Migrations | 30 files in `supabase/migrations/`; the canonical setup is one paste-ready block (`DATABASE_ARCHITECTURE.md` §31.3, parts 1–8); multiple states remain **NOT APPLIED — owner action required** |
| Canonical database objects | 16 reconciled tables (§31.3 part 1) + `api_credentials` (part 2) + `todo_steps` (part 8) |

### 2.2 Major implemented capabilities (source-verified)

| Capability | Where | State |
|---|---|---|
| Glass UI (inline panels; ONE text command `Menu`, exact raw-text equality) | `backend/bot/handlers/`, `backend/helper/` | Implemented; helper bot optional (disabled = valid state); text fallback exists |
| Deep Save only (download → new Saved Messages message; no forward) + short `S####` codes + owner name/tags metadata + deterministic 0/1/N resolution | `backend/services/save_service.py`, `backend/services/retrieve_service.py` | Implemented; forwarding exists only in retrieval |
| Retrieve / Delete / Discover (List/Find) / Database panels | `backend/bot/handlers/{retrieve,delete,discover,database}.py` + services | Implemented |
| Profile engines (Bio `about` + Username `first_name`; ONE shared minute-boundary scheduler) | `backend/profile/`, `backend/bio/`, `backend/username/` | Implemented |
| RuntimeSupervisor (single recovery authority; heartbeat, keepalive, failsafe, task guard, operation watchdog) | `backend/runtime/` | Implemented; `startup_check` and `tg_retry` dormant-but-tested |
| AI runtime: 15 registry entries (12 OpenAI-compatible chat adapters + Gemini + Dummy + `you` search), manager-owned selection/fallback/retry/cooldown, engine/dispatcher pipeline, memory tiers, three-tier context | `backend/ai/providers/`, `backend/ai/engine/`, `backend/ai/memory/`, `backend/ai/conversation/` | Implemented |
| AI tools: **55 registered tools**, declaration-based provider requiredness; `MAX_TOOL_ROUNDS=3`, `MAX_TOOLS_PER_TURN=5` | `backend/ai/tools/` | Implemented (Stage 6) |
| Durable task system (Taskloom): ONE scheduler + ONE coordinator + the same executor; action chains, result refs, `not_before` waits, one conditional branch, question/answer parks on `waiting_answer`, multi-question interleaving, prepare-ahead, bounded retries (`MAX_ATTEMPTS = 3`) | `backend/ai/task_*.py` | Implemented through Part 3F |
| Ghost Seen v2 (private-chat browser, message viewer, explicit selection, reply modes, AI reply, allow-list persistence) | `backend/bot/handlers/ghost_seen_v2.py`, `backend/services/ghost_seen_v2.py` | Implemented (stages 1–8 + hardening); Ghost Seen v1 removed |
| Media analysis (documents, image OCR boundary, voice/audio STT boundary) with Groq/Speechmatics/Gemini engines, fallback rotation, credential pool, chunking, consensus, provider probe | `backend/services/media_*.py`, `backend/services/stt_*.py`, `backend/services/gemini_media_engine.py` | Implemented; **no live recognition loop ever run** |
| Credential Vault (metadata table + resolution RPC + five management RPCs over Supabase Vault) | `supabase/migrations/2026091900000*`, `backend/services/credential_service.py`, `backend/ai/credential_source.py` | Implemented in repo and tests; **not applied live, no secret ever created** |
| TTS (four adapters, control plane, service, fallback, credential pool) | `backend/services/tts_*.py`, `backend/ai/tts_control_plane.py` | Implemented, then **frozen and hidden from the UI** (commit `ee5967f`) |
| Web server + read-only React dashboard | `backend/web/app.py`, `src/` | Implemented; SPA served only if `dist/` exists (gitignored; no build step in `render.yaml`) |
| Deployment configuration | `render.yaml`, `Procfile` | Declares Python 3.11.7, start `python -m backend.main`, `/health`; newer optional provider keys and `YDC_API_KEY` are not enumerated |

### 2.3 Known limitations (recorded; source- or report-backed)

1. **No live provider/model verification** that a real model selects the
   correct tools (Stage 6 report §10.1).
2. **No live Telegram run by any phase** (Stage 6 §10.2; the `5c47f66`
   implementation-report rebuild §12).
3. **No live Supabase execution by any phase**; multiple migrations are
   documented as NOT APPLIED (`DATABASE_ARCHITECTURE.md` §20/§29/§30/§31).
4. **Persian STT quality is unmeasured**; `we_investigation_report.md`
   recommends a controlled 30–50 sample test across five candidate routes
   before any quality claim.
5. **TTS is frozen**: live evaluation found it not useful/reliable enough;
   reactivation requirements are recorded in the freeze commit (`ee5967f`).
6. **Credential Vault is unused live**: no secret created, panel never
   exercised against the live project.
7. **Python version skew**: local 3.10.12 vs declared 3.11.7.
8. **Whole-prompt ceiling is a diagnostic, not an enforced bound**; a
   multi-model token counting layer is an explicit "separate change"
   (Stage 6 §10.5).
9. **The JSON fallback (providers without native tool calling) is narrower**
   than native tool calling by design (Stage 6 §10.3).
10. **The dashboard is not guaranteed in production** (`dist/` is gitignored
    and `render.yaml` has no build step) — documented as optional in README.
11. **Documentation drift**: `AGENTS.md` §15 delivery-state table still records
    `6bec694` (pre-Stage-5); `backend/ai/providers/__init__.py` still describes
    providers as NOT_IMPLEMENTED stubs; `backend/ai/stt_control_plane.py` and
    `stt_provider_probe.py` still call the fallback/cooldown layer "a later
    phase" although `backend/services/stt_fallback.py` exists; stale skipped
    tests remain in `tests/test_51_execution27.py` for the removed Ghost Seen v1.

### 2.4 Implemented but not live-verified (the verification frontier)

- AI tool selection/execution with real providers and models (Stage 7).
- Core Glass UI + Save/Retrieve/Delete + Bio/Username + AI activation +
  Ghost Seen v2 on the real account (Stage 9).
- Durable task lifecycle on the live scheduler clock against the live DB
  (Stage 10).
- OCR/STT recognition on real messages; Persian quality; Vault secret
  resolution (Stage 11).
- Production deployment on Render Free and Python 3.11.7 parity (Stage 12).

### 2.5 Explicitly not implemented (design "future ideas", not current requirements)

Vision/image understanding (`vision()` returns "vision not supported";
`DEFAULT_VISION_ENABLED=False`), streaming (inline panels do not support it;
`DEFAULT_STREAMING_ENABLED=False`), a plugin subsystem (only the `EngineHooks`
extension point exists; the design document's Phase 6 was never adopted into
the current architecture), embedding-based search, smart model routing,
conversation export, cross-task data flow, and the other `AI_MASTER_DESIGN.md`
§20 ideas. None of these is a current requirement; they are listed in §7 as
optional unless the owner decides otherwise.

---

## 3. COMPLETED STAGES

### 3.1 Project-wide numbered stages (the only ones the repository records)

| Stage | Objective | Major work | Commit(s) | Verification status |
|---|---|---|---|---|
| Stage 5 | Make the AI the sole interpreter of natural-language intent | Removed the deterministic semantic router (`parse_command_intent`), the pre-provider fast path, the deterministic task candidate, and the semantic delete/structural predicates; boundary pinned by four suites | `eb4d852` (+ `916536c`, `62dadfe` context) | Test-verified at the time; live verification not performed |
| Stage 6 | Repair the AI→tools contract, context budget, and tool-use policy | Declaration-based provider `required`; history budget (`DEFAULT_MAX_HISTORY_TOKENS=4000`); Gemini `systemInstruction` join + recursive schema conversion; tool-agnostic nudge; 1:1 continuation alignment; decision principles; DeleteTool description | `a317bc8` | Unit/integration-verified: 5158 passed / 26 skipped recorded; live verification not performed |

### 3.2 Completed subsystem arcs (evidence-backed; numbering is local to each arc)

| Arc | Objective | Evidence | Verification status |
|---|---|---|---|
| Taskloom Parts 1, 2, 3A–3F | Durable multi-step workflows built in bounded phases (todo list → steps → chains → waits → branches → question/answer → multi-question → conversational continuation) | Commits `af0a8d7` … `679ae09`; suites `test_todo_*`, `test_task_*` (263 tests across the six phase suites at the earlier audit); `IMPLEMENTATION_REPORT.md` at `5c47f66` §3–§7 | Test-verified; live path never exercised |
| Ghost Seen v2 stages 1–8 + hardening | Private-chat browser/viewer/selection/reply/AI-reply with allow-list persistence | Commits `2489d11` … `8679e18`; suites `tests/test_52…test_66_ghost_seen_v2_*` | Test-verified; live path never exercised |
| Media Processing M1.1–M1.8 / M2.1–M2.4 / M3.0 | Bounded deterministic media boundary (documents → OCR → STT → chunking → engines → fallback → credential pool → vault integration) | Commits `f08d08f` … `a6370da` + the STT series; suites `test_media_*`, `test_stt_*`, `test_groq_stt_engine.py`, `test_speechmatics_stt_engine.py` | Test-verified; no live recognition loop ever run |
| TTS Parts 1–2 (then frozen) | Text-to-speech providers, control plane, native voice delivery; then a deliberate UI freeze | Commits `c0b7dcc` … `8aab7f2`, freeze `ee5967f`; suites `test_tts_*` | Test-verified; frozen by owner decision; not live-verified |
| Credential Vault Parts 1–2 | Metadata table + resolution RPC + five management RPCs over Supabase Vault | `a065e1d`, `f5d93f0`; suites `test_credential_vault.py`, `test_credential_management.py`; `DATABASE_ARCHITECTURE.md` §29 | Test-verified; migrations NOT APPLIED; no secret ever created |
| Save V2 | Owner name + shared metadata contract; deterministic 0/1/N resolution; Telegram media sync | `f9dfd9a`, `5220c38`, `462258b`, `6f5dde7`; suites `test_save_v2_*` | Test-verified; live path never exercised |
| STT dedicated-route investigations and fixes | Direct-STT route, language contract, timeout analysis, multi-pass | Commits `da05ace` … `3089387`; `INVESTIGATION.md` history §18–§20 | Test- + analysis-verified; quality unmeasured |

Stages before Stage 5 are not reconstructible as a numbered sequence from the
current source (the earlier history is a series of subsystem arcs, listed
above); per the evidence rule they are not invented here.

---

## 4. REMAINING STAGES

The repository defines **no Stage 7, 8, …**; the stages below are derived from
the verified remaining work in §2. Numbering continues the project-wide
sequence (Stage 6 → Stage 7) so `IMPLEMENTATION_REPORT.md` continuity holds.
Confidence per stage: §8.

### Stage 7 — Live AI provider/model verification and repair

**Objective:** prove, with real providers and models, that the post-Stage-6
contract + decision policy produce correct tool selection, correct arguments,
and honest answers — and repair every genuine defect the live runs expose.

**Why this stage exists:** Stage 6 fixed the contract but explicitly recorded
"no live provider/model run … do not prove a specific live model will select
`web_search`" (Stage 6 report §10.1). The Stage 5 investigation's matrix
(classes A–J) was designed to be checked against a real provider; that check
was never performed. The AI execution agent is the project's primary
interface, so this is the highest-value unverified capability.

**Prerequisites:** Stage 6 at HEAD (done). Real provider key(s) supplied by the
owner in the runtime environment (never committed). No database dependency for
the selection classes (the in-memory fallback is a valid state).

**Tasks:**

1. Fix the live verification matrix. Cover the Stage 5 classes A–J plus the
   Stage 6 behavioral cases: current-info → `web_search`; explicit search;
   implicit search (no "search"/"web" word); immediate multi-step; durable
   request → `create_task`; capability mention → no tool; explicit save with a
   replied target; genuinely ambiguous → clarify; multi-turn correction;
   tool-result continuation. Map each class to its expected tool/answer and
   pass criteria.
2. Build the bounded, opt-in live harness. Add an opt-in suite under `tests/`
   (skipped without a key, gated on the existing provider env vars; no new
   runtime path) that drives the production `Dispatcher` → `PromptBuilder` →
   real `ProviderManager` → real `ToolRegistry`/`ToolExecutor`. Execute only
   read-only tools (or stub at the service boundary): destructive execution is
   verified on Telegram in Stage 9, never from a test run.
3. Execute against ≥2 real providers and ≥2 models where the plan allows
   (e.g. Gemini + Groq/OpenRouter), with a per-run request cap, and never print
   or persist a secret. Record per class: selected tool, arguments, tool-result
   flow, final answer, failure class.
4. Repair the defects the live runs expose — scoped to the contract surfaces:
   provider schema mapping, prompt budget/instructions, continuation
   alignment, tool descriptions/declarations. No routing, no classifiers, no
   deterministic fast paths.
5. Evaluate the whole-prompt budget against each live model used (the 55-tool
   block is ~15.5 k chars; the Stage 6 ceiling is diagnostic). If a model's
   context window overflows, implement the bounded fix (provider-aware token
   counting or a tool-catalog cap) as a scoped part of this stage.
6. Re-run the affected suites and the full deterministic suite; record results
   and residual model limitations in `IMPLEMENTATION_REPORT.md`.

**Files/modules likely involved:** `backend/ai/engine/dispatcher.py`,
`backend/ai/prompt/{builder,template,budget}.py`,
`backend/ai/providers/*` (schema translation only),
`backend/ai/tools/*` (declarations/descriptions only if proven wrong),
`tests/test_ai_tool_decision_policy.py`, a new opt-in live suite,
`IMPLEMENTATION_REPORT.md`.

**Architecture constraints:** AI remains the sole intent interpreter; no
keyword lists, classifiers, or deterministic fast paths anywhere;
`ToolRegistry`, `ToolExecutor`, `ProviderManager` keep their authority;
credentials only from env or the Vault; bounded cost (cap requests per
provider per run).

**Tests/validation:** the opt-in live suite runs green with a key and skips
honestly without one; the four Stage-5/6 boundary suites
(`test_semantic_intent_boundary.py`, `test_regex_routing_removal.py`,
`test_intent_routing_boundary.py`, `test_provider_tool_boundary.py`) and the
full deterministic suite stay green; per-class results recorded.

**Definition of Done:** recorded per-class live evidence on at least two real
providers; every failure either fixed with a regression pin or recorded as a
residual model limitation with evidence; no contract regressions; no routing
introduced; results persisted in the execution record.

**Blocked/deferred items:** providers requiring paid keys or unavailable in the
owner's environment; the documented narrower JSON fallback for providers
without native tool calling (recorded, not fixed by bypassing); near-neighbour
tool-selection ambiguity (e.g. `retrieve_save` vs `preview_save`) remains a
model-decision matter with the descriptions as the only guidance.

**Dependencies:** Stage 6 (done). Independent of Stage 8.

### Stage 8 — Live Supabase schema application and persistence verification

**Objective:** bring the live database to the canonical contract and verify
every durable path (write, read, search, fallback) against it.

**Why this stage exists:** every database state in the repository says the live
project has not been touched by any phase; the canonical setup block is
"NOT APPLIED — owner action required", and durable features degrade honestly
until it is applied. Live CRUD has never been exercised
(`tests/test_live_supabase_memory.py` skips without credentials).

**Prerequisites:** owner access to the Supabase SQL Editor (schema changes are
manual by design; Render Free has no shell and no stage may require one).

**Tasks:**

1. Apply the canonical database contract: the owner pastes the ONE block from
   `DATABASE_ARCHITECTURE.md` §31.3 (parts 1–8) into the Supabase SQL Editor as
   `postgres` on a fresh database; on an existing database, verify/apply the
   additive successors it enumerates. No agent-executed SQL.
2. Verify the applied schema from the application: boot the runtime against the
   live project, confirm the drift report / PostgREST schema-cache reload
   signals, and exercise the Database panel (stats/maintenance read paths only).
3. Run the existing opt-in live test with real credentials
   (`tests/test_live_supabase_memory.py`), and add bounded opt-in live tests
   for durable paths that lack one (saves + search indexes, bio/username state,
   AI config/session/message/tool-history/usage/provider-stats), using
   test-only rows and strict cleanup.
4. Verify the vault schema objects exist and resolve nothing yet (Stage 11
   provisions a secret); verify the STT/TTS/todo columns are present so their
   panels persist settings.
5. Verify the in-memory fallback: run with Supabase absent and with a
   deliberately invalid configuration; confirm honest degradation and no crash.
6. Record the applied migration state and the live evidence; update the
   migration-status documentation (status text only — no schema content change)
   through the project's documentation workflow.

**Files/modules likely involved:** `DATABASE_ARCHITECTURE.md` (status text
only), `backend/db/client.py`, repositories under `backend/ai/database/`,
`backend/ai/persistence.py`, opt-in tests under `tests/`,
`IMPLEMENTATION_REPORT.md`.

**Architecture constraints:** schema changes stay manual and owner-applied;
the service-role key is used only for writes; the RLS model is unchanged; the
in-memory fallback remains the failure mode; no second database client; no
shell requirement.

**Tests/validation:** opt-in live tests pass with credentials and skip without;
fallback runs verified; the full deterministic suite stays green.

**Definition of Done:** the live database matches the canonical contract with
no pending objects; every durable path is written and read live with recorded
evidence; fallback verified; documentation statuses updated.

**Blocked/deferred items:** any owner decision to keep a feature on the
fallback only; destructive cleanup proposals (dead columns, orphan
`ghost_chats`) stay out of scope — owner decision in Stage 13.

**Dependencies:** none strictly, but Stages 9–11 depend on it.

### Stage 9 — Live Telegram end-to-end verification of the core surfaces

**Objective:** exercise the Glass UI and every non-AI core surface end-to-end
on the owner's real account against the live database; repair the defects found.

**Why this stage exists:** no phase has ever performed a live Telegram run. The
interface contract (`Menu` exact-match, inline panels, edit-in-place
zero-spam, Deep Save without forwarding, delete ownership chokepoint, profile
engines, AI activation) is only test-verified.

**Prerequisites:** Stage 8 (applied schema) for accurate persistence; Stage 7
recommended first so the AI classes are already validated.

**Tasks:**

1. Glass UI: `Menu` opens the mother panel; the helper-bot path and the
   helper-disabled text fallback both render; navigation across Save, Delete,
   List, Find, Database, AI, Profile, Settings/General, Context, Health.
2. Deep Save live: reply-mode media save; verify the NEW Saved Messages copy
   (no forward), metadata (owner name/tags), the `S####` code, Retrieve/List/
   Find, preview, rename/tag edits, and link save.
3. Delete live: last-N, from-ID, replied, recent-browser; verify the ownership
   chokepoint refuses foreign targets; zero-spam preserved.
4. Bio/Username live: enable/disable, template/mood/text, the minute-boundary
   scheduler; FloodWait handling observed.
5. AI activation live: `Nova <request>`, reply-to-AI continuation,
   edited-in-place response; observe the trigger edge case (`Nova:` with
   punctuation does not activate — the recorded P3-4 limitation) and decide
   whether the cosmetic fix is wanted.
6. Ghost Seen v2 live: browser/viewer/selection/reply modes/AI reply; verify
   allow-list restart persistence.
7. Repair defects; re-run the affected suites.

**Files/modules likely involved:** `backend/bot/handlers/*`,
`backend/helper/*`, `backend/services/{save,retrieve,delete,bio,username}_service.py`,
`backend/services/ghost_seen_v2.py`, tests for any fixed defect,
`IMPLEMENTATION_REPORT.md`.

**Architecture constraints:** edit-in-place zero-spam; owner-only gating; Deep
Save never forwards; AI activation unchanged (no new commands); helper-disabled
remains valid; runtime supervision untouched.

**Tests/validation:** recorded live checklist per surface; fixed defects pinned
by tests; the full deterministic suite stays green.

**Definition of Done:** every core surface exercised live with recorded
evidence; defects fixed or recorded; zero-spam and owner-gating hold;
helper-disabled mode confirmed valid.

**Blocked/deferred items:** fixes that change documented behavior need an owner
decision; the `Nova:` punctuation polish stays optional unless the owner asks.

**Dependencies:** Stage 8. Stage 7 recommended.

### Stage 10 — Live durable task system (Taskloom) verification

**Objective:** prove the single scheduler/coordinator/executor path end-to-end
live: claim → execute → persist → notify, across chains, waits, branches,
questions, and restarts.

**Why this stage exists:** the durable task system is complete through Part 3F
and test-verified, but its live path was "deliberately left as owner-side
verification work" (`5c47f66` §12): the live schema, Telegram delivery, and the
real scheduler clock were never exercised.

**Prerequisites:** Stage 8 (the `waiting_answer` widening + task tables
applied) and Stage 9 (Telegram verified).

**Tasks:**

1. Live: create a recurring task through AI (`create_task`); verify the
   `ai_tasks` row, occurrence claim, a single execution per boundary, and the
   owner notification.
2. Live: a multi-action chain with a `$ref` to an earlier action's output; a
   `not_before` wait that parks and resumes; one conditional branch whose
   selected branch executes and whose other branch is marked `skipped`.
3. Live: one question park (`waiting_answer`) → owner reply → CAS resume →
   chained continuation; then a multi-question chain where each answer resumes
   the same occurrence.
4. Restart recovery: restart the process while a wait and while a question are
   parked; verify no duplicate execution, no spam, correct resume, and the
   `restart_side_effect_uncertain` contract where applicable.
5. Verify bounded retry behavior (`MAX_ATTEMPTS = 3`) and that the retry query
   and the recovery query leave `waiting_answer` untouched.
6. Repair defects; re-run the task suites and the full deterministic suite.

**Files/modules likely involved:** `backend/ai/task_scheduler.py`,
`backend/ai/task_execution.py`, `backend/ai/task_answers.py`,
`backend/ai/database/task_repository.py`,
`backend/bot/handlers/{task_events,taskloom,todo}.py`, `tests/test_task_*`,
`IMPLEMENTATION_REPORT.md`.

**Architecture constraints:** ONE scheduler, ONE coordinator, ONE executor; no
second update loop; occurrence idempotency and CAS rules unchanged;
fail-closed validation; no dynamic replanning.

**Tests/validation:** live evidence per bounded capability; restart duplication
check; the existing suites stay green.

**Definition of Done:** recorded live evidence for each capability; restart
never duplicates or spams; no second authority introduced; failures fixed or
recorded.

**Blocked/deferred items:** deliberate non-features (dynamic replanning,
parallel/looped chains, question timeouts/reminders, cross-task data flow,
multimodal answers) remain out of scope unless the owner decides otherwise in
Stage 13.

**Dependencies:** Stages 8 and 9.

### Stage 11 — Live media analysis, speech, and credential-vault verification

**Objective:** verify OCR/STT live on real messages, measure Persian STT
quality against the research report's plan, and verify the credential vault
end-to-end.

**Why this stage exists:** media analysis is implemented but "no live
recognition loop was ever run"; Persian quality is unmeasured and
`we_investigation_report.md` explicitly says a winner cannot be defended
without a controlled test on real project samples; the Vault migrations were
never applied and no secret was ever created. TTS reactivation (Stage 13)
depends on this evidence.

**Prerequisites:** Stage 8 (vault migrations applied) and Stage 9 (Telegram
verified).

**Tasks:**

1. Apply/verify the vault migrations (via Stage 8's application), create ONE
   real secret through the credential panel, and verify
   `api_credential_pool`/`stt_credential_pool` resolution and the M2.4
   credential pool; confirm the management panel reports each state honestly.
2. Live OCR: reply to photos/documents with text and verify the extracted
   content, the honest UNSUPPORTED results for out-of-scope types, and the
   zero-context rule.
3. Live STT: reply to real Persian voice/audio; verify the direct-STT delivery
   route, chunked long audio, and fallback behavior with one provider made
   unavailable.
4. Persian quality evaluation: run the controlled benchmark recommended by
   `we_investigation_report.md` (30–50 real samples; compare the five
   shortlisted routes, incl. Speechmatics, Groq Whisper large-v3 and turbo,
   and a hosted Persian Whisper); record the results and choose/confirm the
   route.
5. Record evidence for the TTS decision (Stage 13): whether the recorded freeze
   reason (usefulness/reliability) is resolved by the measured path.

**Files/modules likely involved:** `backend/services/media_service.py`,
`backend/services/media_ai_service.py`,
`backend/services/{groq,speechmatics}_stt_engine.py`,
`backend/services/gemini_media_engine.py`, `backend/services/tts_*.py`,
`backend/ai/stt_control_plane.py`, `backend/ai/credential_source.py`,
`backend/services/credential_service.py`, tests, `IMPLEMENTATION_REPORT.md`.

**Architecture constraints:** media context isolation (no Telegram context to
providers); credential values never printed; fallback/cooldown semantics
unchanged; no new provider HTTP path; TTS stays frozen until the owner decides.

**Tests/validation:** live OCR/STT evidence; the opt-in provider probes
(`tests/test_stt_provider_probe.py`) executed where credentials exist; the
benchmark recorded; the suites stay green.

**Definition of Done:** live OCR/STT evidence; Vault secret resolution
verified; recorded Persian quality evaluation and route decision; no fabricated
transcripts; TTS decision input recorded.

**Blocked/deferred items:** routes requiring paid keys the owner does not hold;
TTS reactivation itself belongs to Stage 13.

**Dependencies:** Stages 8 and 9.

### Stage 12 — Production deployment and runtime verification (Render Free)

**Objective:** verify the product boots and runs correctly on the declared
production runtime, and close the deployment/parity gaps.

**Why this stage exists:** no production-parity runtime check was ever
performed; the local Python (3.10.12) differs from the declared production
Python (3.11.7); `render.yaml` enumerates only a subset of the supported
provider env keys and has no dashboard build step while `dist/` is gitignored;
Render Free has no shell.

**Prerequisites:** Stages 7–11 recommended so behavior is verified before
production verification.

**Tasks:**

1. Deploy via `render.yaml`; verify boot, `/health` 200, and clean logs;
   verify supervisor behavior (heartbeat, keepalive, failsafe) in production.
2. Verify environment completeness: enumerate the supported provider keys (the
   factory env map) plus `YDC_API_KEY` and `GHOST_SEEN_*`; update `render.yaml`
   with the keys the owner wants in the blueprint; document the rest as
   dashboard-set optional keys.
3. Verify no-shell operation: every maintenance path (database maintenance,
   credential management, profile engines, task recovery) is reachable through
   Telegram panels and none requires Render Shell.
4. Verify restart/redeploy stability: recovery layers, session validity, no
   duplicate task execution, resource use within the free plan.
5. Resolve the dashboard question: build `dist/` in the deploy pipeline (an
   explicit build step) or confirm/document local-only use — owner choice.
6. Record production-parity evidence; verify on Python 3.11 (the declared
   runtime) or record the skew as an accepted limitation.

**Files/modules likely involved:** `render.yaml`, `Procfile`, `README.md`
(deployment section), `backend/web/app.py`, `backend/runtime/*`,
`IMPLEMENTATION_REPORT.md`.

**Architecture constraints:** no shell requirement; secrets only in the
platform's env configuration; no second web server; the supervisor remains the
single recovery authority; the health endpoint is unchanged.

**Tests/validation:** production boot + `/health` evidence; restart evidence;
the deterministic suite green locally on the supported versions.

**Definition of Done:** production boots and serves health with recorded
evidence; self-healing verified; no shell dependency; env vars documented;
parity resolved or its risk recorded; the dashboard outcome decided.

**Blocked/deferred items:** paid Render features; any owner decision to keep
the dashboard local-only.

**Dependencies:** Stages 7–11 recommended.

### Stage 13 — Deferred-capability decisions and final documentation closure

**Objective:** resolve every deferred/optional item with a recorded owner
decision, and make the repository documentation final and self-consistent.

**Why this stage exists:** the Definition of Done (§1.8) requires deferred
items to be decided, not left implicit; and several documents currently
contradict or lag the source (`AGENTS.md` §15, provider `__init__` docstring,
STT control-plane docstrings, stale Ghost Seen v1 skipped tests, historical
documents under `docs/`).

**Prerequisites:** Stages 7–12 (the decisions need their evidence; TTS needs
the Stage 11 quality evidence).

**Tasks:**

1. TTS: the owner decides reactivate (restore the three Media-Analysis rows,
   fix the freeze reason, live-verify the provider round trip and a delivered
   voice note) or keep frozen; record the decision and conditions.
2. Vision / streaming / plugins: record decisions (implement, keep dormant, or
   remove the dormant flags) — none is currently required.
3. Taskloom bounded extensions (question timeout/reminder, dynamic replanning,
   cross-task data flow, parallel/loops, multimodal): accept as out of scope or
   schedule; record.
4. Database cleanup decisions: dead `saved_items.short_code`/`file_name`,
   orphan `ghost_chats`, `bot_settings` vs `panel_settings` consolidation,
   retention consumers, `ai_preferences`: owner decides; destructive actions
   only with a backup and explicit approval.
5. Documentation closure: update `AGENTS.md` (incl. §15 delivery table and any
   section invalidated since), `README.md`, `IMPLEMENTATION_REPORT.md`; mark
   superseded historical documents under `docs/`; fix stale docstrings; clean
   up or honestly re-skip the stale `test_51` remnants; ensure this file's
   statuses match reality.
6. Final DoD audit: walk §1 clause by clause and record the outcome for each.

**Files/modules likely involved:** `AGENTS.md`, `README.md`,
`IMPLEMENTATION_REPORT.md`, `INVESTIGATION.md`, `ROADMAP.md`,
`DATABASE_ARCHITECTURE.md` (status/decision text), `docs/**`,
`backend/ai/providers/__init__.py`, `backend/ai/stt_control_plane.py`,
`backend/ai/stt_provider_probe.py`, `tests/test_51_execution27.py`.

**Architecture constraints:** no weakening of any guardrail; no removed
architecture reintroduced; database changes remain manual.

**Tests/validation:** final full-suite run; `git diff --check`; documentation
consistency review against source.

**Definition of Done:** every §7 item has a recorded decision; docs agree with
source; the full suite green; no stale claim anywhere in the documentation map.

**Blocked/deferred items:** anything the owner chooses to keep deferred —
recorded as a decision with its rationale.

**Dependencies:** Stages 7–12.

---

## 5. STAGE DEPENDENCY GRAPH

```
Stage 7 (live AI verification)     Stage 8 (live DB / schema)
                                          |
                                          v
                                   Stage 9 (live Telegram core)
                                          |
                                          v
                                   Stage 10 (live Taskloom)
                                          |
                                          v
                                Stage 11 (media / speech / vault)
                                          |
                                          v
                                  Stage 12 (production runtime)
                                          |
                                          v
                                 Stage 13 (decisions + doc closure)

Stage 7 is independent of Stage 8; it is scheduled first because it validates
Stage 6 and the primary interface.
```

Supported dependencies only:

- 9, 10, 11 → 8 (they persist to the live DB; 10 also needs the
  `waiting_answer` widening).
- 10, 11 → 9 (both are exercised through the Telegram interface).
- 12 → 7–11 recommended (verify behavior live before production verification).
- 7 is independent of 8–12; it is scheduled first because it validates the
  newest work (Stages 5–6) and the primary interface.
- 13 → 7–12 (the decisions require their evidence).

### 5.1 Two-stage execution chunks

| Chunk | Stages | Content |
|---|---|---|
| 1 | **Stage 7 + Stage 8** | Verify the AI contract live; apply + verify the live database |
| 2 | Stage 9 + Stage 10 | Verify the core Telegram surfaces live; verify durable task workflows live |
| 3 | Stage 11 + Stage 12 | Verify media/speech/vault live; verify the production runtime |
| 4 | Stage 13 | Deferred-item decisions and documentation closure |

A future execution prompt "Implement Stage 7 and Stage 8" is self-sufficient
from §4: each stage carries its objective, tasks, files, constraints, tests,
DoD, blocked items, and dependencies. Stages 7 and 8 are **planned, not
completed** — nothing in this document claims otherwise.

---

## 6. UNRESOLVED QUESTIONS

These prevented a fully certain roadmap and are **not** silently resolved:

1. **Who performs live verification?** Stages 7–12 require the owner's real
   Telegram account, provider keys, Supabase project, and Render deployment.
   The coding agent has never had access to any of them. If the owner intends
   agent-assisted verification, the exact split of responsibilities must be
   defined per stage (this roadmap assumes owner-driven execution with agent
   guidance/repairs).
2. **Which migrations are actually applied in the live project?** The
   repository states "NOT APPLIED" as of the documents' dates, but the live
   Supabase state was never inspected. Stage 8 must establish the real state
   before its verification list can be exact.
3. **Is `render.yaml` the authoritative production configuration?** The Render
   dashboard may hold env vars and build settings not visible in the repo.
4. **Should the dashboard be served in production?** README says optional;
   `render.yaml` has no build step; `dist/` is gitignored. Owner decision in
   Stage 12.
5. **TTS reactivation intent.** The freeze records a usefulness/reliability
   problem; whether the owner wants it addressed is unknown (Stage 13).
6. **Persian STT acceptance threshold.** No target quality is defined; without
   one, Stage 11 can record results but cannot pass/fail them.
7. **Taskloom extension intent** (question timeout/reminder, cross-task flow):
   deliberately out of scope today; owner decision in Stage 13.
8. **Database cleanup intent** (dead columns, orphan `ghost_chats`,
   `bot_settings` consolidation, retention consumers): no decision exists.
9. **Python 3.11 parity intent**: fix the local-verification skew or accept it?
10. **Historical `docs/` documents** (`ghost-room-ai-foundation-contract.md`,
    `PHASE_1_DATABASE_DISCOVERY.md`, `we_investigation_report.md`): keep as
    record, mark superseded, or replace? (Stage 13 decides.)

---

## 7. EXPLICITLY DEFERRED / OPTIONAL WORK

Non-blocking. Not required for the Definition of Done unless the owner
promotes an item (recorded decision in Stage 13).

| Item | Evidence | Status |
|---|---|---|
| TTS reactivation: restore UI rows, fix reliability, live-verify | freeze commit `ee5967f` and its recorded reactivation requirements | Deferred by owner freeze; decision in Stage 13 |
| STT "M-line" continuation beyond quality evaluation (new engines/routes) | STT investigations §18–§20; `we_investigation_report.md` | Frozen at the M-line; revisit only with benchmark evidence |
| Vision / image understanding (`vision_enabled=False`) | `providers/base/contract.py`, `openai_compat.py` "vision not supported"; design §20.1 | Not implemented; optional |
| Streaming responses | `DEFAULT_STREAMING_ENABLED=False`; inline panels cannot stream | Optional by design |
| Plugin subsystem (design Phase 6) | only the `EngineHooks` extension point; the current architecture never adopted plugins | Optional / historical intent |
| Multi-model token counting layer | Stage 6 §10.5 "a separate change" | Optional unless Stage 7 proves overflow |
| Taskloom extensions: question timeout/reminders, dynamic replanning, cross-task data flow, parallel/looping, multimodal answers | `5c47f66` §11 "NOT IMPLEMENTED" list | Deliberate non-goals; owner may promote |
| DB cleanup: dead `saved_items.short_code`/`file_name`, orphan `ghost_chats`, `bot_settings` consolidation, retention consumers, `ai_preferences` | `DATABASE_ARCHITECTURE.md` §20/§22/§30.10 | Owner-gated; destructive only with a backup |
| Trigger punctuation polish (`Nova:` does not activate; P3-4) | Stage 5 investigation P3-4; `match_trigger` + `ai_unified` split behavior | Cosmetic; optional |
| Output-guideline / tool-overlap refinements (P2-1, P3-3) | Stage 5 investigation | Optional polish; descriptions remain the guidance |
| `render.yaml` provider-key completeness / dashboard build step | §2.2/§2.3 of this roadmap, verified against `render.yaml` | Folded into Stage 12 as owner guidance |
| Stale docstrings and skipped `test_51` remnants | §2.3 (11) of this roadmap | Hygiene; folded into Stage 13 |

**Historical / obsolete — must NOT be scheduled as work:** the deterministic
semantic router (removed Stage 5), forward-save, legacy dot commands, Ghost
Seen v1 and its removed AI-reply flow, the Ghost Room greenfield contract
(superseded by Ghost Seen v2), and the old design-document `Phase 1–5` labels
(their capabilities were delivered in evolved form; only "Phase 6 — Plugins"
remains historical intent).

---

## 8. ROADMAP CONFIDENCE

| Stage | Confidence | Basis |
|---|---|---|
| 7 — Live AI verification | **VERIFIED BY SOURCE** | Stage 6 report §10.1 explicitly records the missing live run; the Stage 5 investigation designed the matrix; the source confirms the pipeline exists |
| 8 — Live DB/schema | **VERIFIED BY SOURCE** | `DATABASE_ARCHITECTURE.md` §20/§29/§30/§31 "NOT APPLIED — owner action required"; the opt-in live test skips without credentials |
| 9 — Live Telegram core | **STRONGLY DERIVED** | No live run ever (Stage 6 §10.2; `5c47f66` §12); the surfaces exist and are test-verified; the exact checklist is derived from the registered panels/features |
| 10 — Live Taskloom | **STRONGLY DERIVED** | `5c47f66` §12 states the live path was left as owner-side verification; §3 documents each bounded capability |
| 11 — Media/speech/vault | **STRONGLY DERIVED** | "No live recognition loop was ever run"; `we_investigation_report.md` recommends the controlled benchmark; the Vault was never applied/used |
| 12 — Production runtime | **STRONGLY DERIVED** | Production-parity check never performed (`5c47f66` §12); the Python skew and `render.yaml` gaps verified in source |
| 13 — Decisions + closure | **PARTIALLY DERIVED** | The need is verified (DoD §1.8; doc drift verified in source); the individual decisions belong to the owner and cannot be pre-made |

No stage is labelled VERIFIED BY SOURCE unless the repository itself states or
shows the requirement. Nothing here is "UNCERTAIN-as-task": uncertain items are
confined to §6.

---

*End of ROADMAP.md. Planning document only — no implementation was performed.*
