# Investigation — Remaining Project Roadmap (Reconstruction)

## Status

**Investigation and planning only — no implementation performed.**

This document **replaces the previous `INVESTIGATION.md` completely** (the
Stage-5-era forensic audit of AI request understanding). It is a read-only
reconstruction of the authoritative remaining project roadmap from the current
repository. It changes no production code, no test, no migration, no prompt,
and no `DATABASE_ARCHITECTURE.md` content. Nothing was executed against live
Telegram, a live AI provider, or live Supabase. No runtime test suite was run
for this task; the validation performed was repository/source/diff validation.

**Planning deliverables of this task:**

- **`ROADMAP.md` created** — the authoritative MASTER PROJECT ROADMAP. No
  roadmap document existed before (the only "roadmap" text in the repository is
  `AI_MASTER_DESIGN.md` §17, the superseded design-document Phase 1–6 plan).
  `IMPLEMENTATION_REPORT.md` recorded only *"Stage completed: Stage 6 / Next
  stage: Stage 7"* — a number with no content — so one standing document is
  required for two-stage-at-a-time execution.
- **`INVESTIGATION.md` replaced** — this file.
- **`IMPLEMENTATION_REPORT.md` annotated** with a planning-deliverable pointer;
  it still records **Stage 6 as the last completed stage**. Stage 7 is
  **planned, not completed**.

**Baseline:** repository `Onlyicing1/Telegram-self-bot`, branch `main`, HEAD
`a317bc8e23edce5388e264442902b27739d6acf1` = `origin/main`, working tree clean
except the pre-existing untracked `telegram-self-bot/` (never staged).

---

## Task and method

**Question:** what remains before LifeOS can be considered complete, and in
what order should that work proceed?

**Evidence hierarchy used:** current source code → current architecture
documents (`AGENTS.md`, `DATABASE_ARCHITECTURE.md`, `AI_MASTER_DESIGN.md`) →
current `IMPLEMENTATION_REPORT.md` → the previous `INVESTIGATION.md` → tests
and their coverage → historical documents only to reconstruct intent.

**Method:**

1. Verified the repository state (branch, HEAD, remote equality, tree status)
   and confirmed no roadmap document existed.
2. Read the current workflow documents: `IMPLEMENTATION_REPORT.md` (Stage 6),
   the previous `INVESTIGATION.md` (Stage-5 audit), `AGENTS.md`,
   `DATABASE_ARCHITECTURE.md` (read-only, incl. its migration-status,
   credential-vault, reconciliation and canonical-setup sections),
   `AI_MASTER_DESIGN.md` (its §17 roadmap and §20 future ideas), `README.md`,
   and the two documents under `docs/` plus `we_investigation_report.md`.
3. Mapped the implementation surface against the source: entry point, runtime,
   handlers, services, AI engine/dispatcher/providers/registry/executor,
   task/scheduler modules, media/STT/TTS modules, credential vault, web app,
   migrations, deployment files, and the 204 test modules.
4. Searched the tree for future-work markers (`TODO`, `FIXME`, `deferred`,
   `remaining work`, `next stage`, `later phase`, `not implemented`,
   `NOT PROVEN`, `NOT VERIFIED`, `blocked`, `planned`, `roadmap`, `phase`,
   `stage`, `deferred capability`) and classified every finding A–G (below).
   Marker hits that are ordinary vocabulary (e.g. `_TODO_ADD_FIELDS` field
   sets, "future placeholder" docstrings that describe the current design)
   were not converted into tasks.
5. Derived the remaining stages from the *confirmed* remaining work only, and
   wrote them into `ROADMAP.md` with objective, prerequisites, tasks, files,
   constraints, tests, DoD, blockers and dependencies per stage.

**Classification key used for every finding:** **A** COMPLETED (implemented and
sufficiently validated) · **B** PARTIALLY IMPLEMENTED · **C** IMPLEMENTED BUT
NOT VERIFIED · **D** REQUIRED REMAINING WORK · **E** OPTIONAL / NON-BLOCKING ·
**F** HISTORICAL / OBSOLETE · **G** UNCERTAIN. Uncertainty was never converted
into a task; the uncertain items live in the roadmap's §6.

---

## Repository state

| Item | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` (remote `origin`) |
| Branch | `main` |
| Local HEAD | `a317bc8e23edce5388e264442902b27739d6acf1` — `fix(ai): repair the tool contract, context budget and tool-use policy` (Stage 6) |
| Remote HEAD (`origin/main`) | `a317bc8e23edce5388e264442902b27739d6acf1` (equal; verified with `git fetch origin` + `git rev-parse origin/main` + `git merge-base --is-ancestor` → exit 0) |
| Working tree | clean except the pre-existing untracked nested repository `telegram-self-bot/` (never staged) |
| Local Python used by the project venv | 3.10.12; production declares 3.11.7 (`render.yaml`) |
| Migrations | 30 files in `supabase/migrations/`; canonical setup is ONE paste-ready block (`DATABASE_ARCHITECTURE.md` §31.3, parts 1–8); multiple individual states remain **NOT APPLIED — owner action required** |
| Tests | 204 test modules under `tests/`; Stage 6 recorded full suite 5158 passed / 26 skipped |
| Roadmap document before this task | none (`find`/`grep` for roadmap documents returned only `AI_MASTER_DESIGN.md` §17, superseded) |

---

## Confirmed facts (source- or document-verified)

These are the facts the roadmap is built from. "Evidence" cites where each fact
was read; nothing in this table is carried over from an old report without
re-verification against the current tree.

| # | Fact | Class | Evidence |
|---|---|---|---|
| 1 | Stage 5 (removal of the deterministic semantic router and fast path) is complete; the boundary is pinned by four suites | A | `eb4d852`; no hits for `parse_command_intent`/`_try_local_fast_path` in `backend/`; `tests/test_semantic_intent_boundary.py`, `test_regex_routing_removal.py`, `test_intent_routing_boundary.py`, `test_provider_tool_boundary.py` |
| 2 | Stage 6 (tool contract, history budget, Gemini system messages, decision policy, continuation alignment) is complete and committed | A | `a317bc8`; `IMPLEMENTATION_REPORT.md`; recorded 5158 passed / 26 skipped |
| 3 | 55 tools are registered in the live registry; provider-facing `required` is declaration-based | A | `backend/ai/tools/registry.py` (55 registered tools); `backend/ai/tools/base.py` `provider_required_arguments`; `tests/test_tool_schema_contract.py` |
| 4 | Provider layer: 15 registry entries — 12 OpenAI-compatible chat adapters, Gemini (custom mapper), Dummy (never selected in production), `you` (web-search capability, never a chat engine) | A | `backend/ai/providers/factory.py`; `tests/test_provider_system_instruction_contract.py` |
| 5 | `ProviderManager` owns selection/fallback/retry/cooldown; `RuntimeSupervisor` is the single recovery authority | A | `backend/ai/providers/manager/manager.py`; `backend/runtime/supervisor.py` |
| 6 | `ToolExecutor` is the only component that calls `tool.execute()` | A | `backend/ai/tools/executor.py`; no other production caller found |
| 7 | Durable task system complete through Part 3F (chains, waits, branches, question parks, multi-question, continuation, prepare-ahead) | A | commits `83c5abd`…`679ae09`; `backend/ai/task_*.py`; `tests/test_task_*` |
| 8 | Ghost Seen v2 implemented (stages 1–8 + hardening) and registered in the router | A | `backend/bot/handlers/ghost_seen_v2.py`; `backend/bot/router.py`; `tests/test_52…66_ghost_seen_v2_*` |
| 9 | Media processing implemented through the M-line (documents, OCR, STT, chunking, engines, fallback, credential pool, consensus, probe) | A | `backend/services/media_service.py`; `backend/services/stt_*.py`; `backend/services/gemini_media_engine.py`; `tests/test_media_*` |
| 10 | Save V2 metadata/resolution/Telegram-sync complete; Deep Save never forwards (forwarding exists only in retrieval) | A | `backend/services/save_service.py`; `backend/services/retrieve_service.py` (only `forward_messages` use); `tests/test_save_v2_*` |
| 11 | Credential Vault control plane complete in repo/tests (metadata table, resolution RPC, five management RPCs) | A (repo) | `supabase/migrations/2026091900000*`; `backend/services/credential_service.py`; `tests/test_credential_vault.py`, `test_credential_management.py` |
| 12 | **No live provider/model verification** that a real model selects the correct tools | C | Stage 6 report §10.1 |
| 13 | **No live Telegram run by any phase** | C | Stage 6 report §10.2; `IMPLEMENTATION_REPORT.md` at `5c47f66` §12 |
| 14 | **No live Supabase execution by any phase**; migrations documented NOT APPLIED | C/D | `DATABASE_ARCHITECTURE.md` §20, §29, §30.11, §31 ("NOTHING has been executed") |
| 15 | **No production-parity check** (Render / Python 3.11.7) ever performed | C | `5c47f66` §12 |
| 16 | **No live recognition loop ever run** for OCR/STT; Persian quality unmeasured | C | `5c47f66` §10/§11; `we_investigation_report.md` (recommends a controlled 30–50 sample test over five routes) |
| 17 | Credential Vault never applied live; **no secret ever created**; panel never exercised live | C | `DATABASE_ARCHITECTURE.md` §29.13; `5c47f66` §10 |
| 18 | TTS frozen and hidden from the UI by owner decision (implementation retained; reactivation requirements recorded) | E (frozen) | commit `ee5967f`; `backend/bot/handlers/ai_stt_settings.py` hub diff |
| 19 | The canonical database contract is ONE paste-ready block (§31.3, parts 1–8) covering 16 reconciled tables + `api_credentials` + `todo_steps`; owner action required | D | `DATABASE_ARCHITECTURE.md` §31.1–§31.3; `supabase/canonical_bootstrap.sql` byte-identical to `20260920000001_reconcile_canonical_schema.sql` |
| 20 | `render.yaml` declares Python 3.11.7 and `/health` but enumerates only a subset of provider env keys (`YDC_API_KEY`, newer adapters, `GHOST_SEEN_*` absent) and has no dashboard build step; `dist/` is gitignored | D/E | `render.yaml`; `.gitignore`; `backend/ai/providers/factory.py` env maps |
| 21 | Local/declared Python skew: 3.10.12 local vs 3.11.7 declared | D/E | `.venv/bin/python --version`; `render.yaml` |
| 22 | Documented whole-prompt ceiling is a diagnostic, not an enforced bound; multi-model token counting is "a separate change" | B/E | Stage 6 report §10.5 |
| 23 | The JSON fallback for providers without native tool calling is narrower than native tool calling, by design | E | Stage 6 report §10.3; prompt OUTPUT rule 8 |
| 24 | Documentation drift: `AGENTS.md` §15 delivery table records `6bec694` (pre-Stage-5); `backend/ai/providers/__init__.py` still describes providers as NOT_IMPLEMENTED stubs; `backend/ai/stt_control_plane.py` / `stt_provider_probe.py` call the fallback/cooldown layer "a later phase" although `backend/services/stt_fallback.py` exists; stale skipped Ghost Seen v1 tests remain in `tests/test_51_execution27.py` | D (hygiene) | direct file reads |
| 25 | Removed architecture (must not be resurrected): semantic router/fast path, forward-save, legacy dot commands, Ghost Seen v1 and its removed AI-reply flow | F | Stage 5 report; `router.py`/`misc.py`; `test_51` skip reasons; `ghost_seen_v2` current files |
| 26 | `docs/implementation/ghost-room-ai-foundation-contract.md` is superseded: it describes `_try_local_fast_path` (removed in Stage 5) and a greenfield "Ghost Room" that was delivered as Ghost Seen v2 | F | doc text vs `backend/bot/handlers/ghost_seen_v2.py` |
| 27 | `docs/investigations/PHASE_1_DATABASE_DISCOVERY.md` is largely superseded by the later schema reconciliation (its "missing" `ai_usage`/`ai_provider_stats` migrations now exist; canonical contract exists); its remaining questions (`ghost_chats`, `ai_preferences`, retention) are owner decisions | F/E | doc vs `supabase/migrations/`; `DATABASE_ARCHITECTURE.md` §30 |
| 28 | Design-document "future ideas" not implemented: vision (`vision not supported`; `DEFAULT_VISION_ENABLED=False`), streaming (panels cannot stream), plugins (only `EngineHooks`), embedding search, smart routing, conversation export | E | `AI_MASTER_DESIGN.md` §18/§20; `backend/ai/providers/base/contract.py`; `backend/ai/config/defaults.py`; `backend/ai/engine/hooks.py` |
| 29 | Taskloom's deliberate non-features (no question timeout/reminder, no dynamic replanning, no parallel/loops, no cross-task data flow, no multimodal answers) | E | `5c47f66` §11 "NOT IMPLEMENTED" list |
| 30 | Opt-in live harnesses already exist and skip honestly without credentials (`test_live_supabase_memory.py`, STT provider probes) | support | test skip conditions read directly |
| 31 | `AI_MASTER_DESIGN.md` §17 "Development Roadmap" is the historical design-doc plan; its Phase 1–5 capabilities were delivered in evolved form under the Glass UI architecture; Phase 6 (Plugins) was never adopted | F | design doc vs current architecture; `AGENTS.md` (no plugin subsystem) |
| 32 | The current architecture is documented as authoritative in `AGENTS.md`, including the one-command `Menu`, no dot commands, Deep Save only, single-scheduler/executor/authority rules | A | `AGENTS.md`; cross-checked against `misc.py` (`raw_text == "Menu"`), `save_service.py` (no forward), task system |

---

## Derived planning (not inherited)

The repository never defines what Stage 7+ contains. The **planning** below is
derived by this investigation from the confirmed facts above; it is recorded in
full — with tasks, files, constraints, tests, DoD, blockers and dependencies —
in `ROADMAP.md` §4. It is derived, not authoritative-by-history.

| Stage | Derived scope | Derived from facts |
|---|---|---|
| 7 — Live AI provider/model verification and repair | Run the A–J/Stage-6 diagnostic matrix against ≥2 real providers; repair contract defects; evaluate the whole-prompt budget on real models | 12, 22, 23 |
| 8 — Live Supabase schema application and persistence verification | Apply the canonical §31.3 block (owner), verify every durable path + fallback live | 14, 19, 17 |
| 9 — Live Telegram end-to-end verification of the core surfaces | Glass UI, Deep Save/Retrieve/Delete, profile engines, AI activation, Ghost Seen v2 on the real account | 13, 12 |
| 10 — Live durable task system verification | Task lifecycle, chains/waits/branches/questions, restart safety on the live scheduler clock | 7, 13, 14 |
| 11 — Live media/speech/vault verification | OCR/STT live, Persian quality benchmark, Vault secret provisioning/resolution | 16, 17 |
| 12 — Production deployment and runtime verification (Render Free) | Boot, `/health`, self-healing, no-shell paths, parity, dashboard decision | 15, 20, 21 |
| 13 — Deferred-capability decisions and final documentation closure | Owner decisions for §7 items; documentation truth (drift fixes) | 18, 24, 27, 28, 29 |

**Ordering logic (derived from actual dependencies, not a template):**

- Stage 7 and Stage 8 are independent foundations and form the first execution
  chunk. Stage 7 validates the primary interface (the newest work); Stage 8
  brings the live database to the contract everything durable needs.
- Stages 9, 10 and 11 are live-capability verifications that persist to the
  database and travel through Telegram, so they depend on Stage 8 (and 10/11 on
  Stage 9). Stage 10 additionally needs the `waiting_answer` widening.
- Stage 12 verifies the verified behavior under production constraints, so it
  follows 7–11.
- Stage 13 closes the Definition of Done: deferred items are decided (they need
  the evidence from 7–12) and the documentation is made truthful.

**Two-stage chunking** (`ROADMAP.md` §5.1): `Stage 7 + Stage 8`,
`Stage 9 + Stage 10`, `Stage 11 + Stage 12`, `Stage 13`.

---

## Explicitly NOT treated as remaining work

- **Removed architecture** — the deterministic semantic router, the pre-provider
  fast path, forward-save, dot commands, and Ghost Seen v1 (facts 25–26). None
  is scheduled anywhere.
- **Superseded documents** — the Ghost Room contract and the Phase-1 database
  discovery report are historical (facts 26–27); they are kept as record and
  revisited only for decisions, never as active requirements.
- **Historical design intent** — `AI_MASTER_DESIGN.md` Phase 1–5 labels and its
  §20 ideas (facts 28, 31). Optional items are isolated in `ROADMAP.md` §7.
- **Marker noise** — `TODO`-prefixed identifiers in `backend/ai/actions.py`
  (e.g. `_TODO_ADD_FIELDS`), "future placeholder" docstrings describing the
  current design, and stale docstrings that contradict the source (fact 24)
  are not capabilities; the stale docstrings are assigned to the documentation
  closure stage, not to a feature stage.
- **Uncertainty** — items with insufficient evidence (roadmap §6) were left
  unresolved rather than converted into tasks.

---

## Source evidence index

Primary files read/verified while producing this investigation (all at HEAD
`a317bc8` unless noted):

- Workflow docs: `IMPLEMENTATION_REPORT.md`, `INVESTIGATION.md` (previous),
  `AGENTS.md`, `DATABASE_ARCHITECTURE.md` (§20 migration status, §29 vault,
  §30 reconciliation, §31 canonical setup), `AI_MASTER_DESIGN.md` (§17, §18,
  §20), `README.md`, `docs/**`, `we_investigation_report.md`.
- History: `git log`/`git show` for `a317bc8`, `eb4d852`, `5c47f66`, `ee5967f`,
  the Taskloom series, the Ghost Seen v2 series, and the M-line commits.
- Source: `backend/main.py`, `backend/config.py`, `backend/runtime/supervisor.py`,
  `backend/bot/router.py`, `backend/bot/handlers/{misc,ai_unified,ghost_seen_v2,ai_tts_settings}.py`,
  `backend/ai/tools/{registry,executor,base}.py`,
  `backend/ai/providers/factory.py`, `backend/ai/providers/manager/manager.py`,
  `backend/ai/engine/dispatcher.py`, `backend/ai/prompt/*.py`,
  `backend/ai/task_*.py`, `backend/services/{save_service,retrieve_service,media_service,stt_fallback,tts_service,credential_service,ghost_seen_v2}.py`,
  `backend/web/app.py`, `backend/ai/config/defaults.py`.
- Database: `supabase/migrations/` (30 files), `supabase/canonical_bootstrap.sql`.
- Deployment: `render.yaml`, `Procfile`, `.gitignore`, `package.json`, `src/**`.
- Tests: the `tests/` inventory (204 modules) incl. the opt-in live skips and
  the stale `test_51` skips.

The delivery report records the exact git/status commands used for validation;
`git diff --check` and `git status` were run before delivery.

---

## Unresolved questions

Recorded in full in `ROADMAP.md` §6; the essentials:

1. Who performs live verification (the coding agent has never had live account,
   provider, Supabase, or Render access)?
2. Which migrations are actually applied in the live Supabase project?
3. Is `render.yaml` the authoritative production configuration?
4. Should the dashboard be served in production?
5. Does the owner intend to reactivate TTS?
6. What is the acceptance threshold for Persian STT quality?
7. Which Taskloom extensions (if any) does the owner want?
8. Which database cleanup items (if any) does the owner approve?
9. Is the Python 3.10/3.11 skew to be fixed or accepted?
10. Keep, mark superseded, or replace the historical `docs/` documents?

---

## Validation performed for this investigation

- Repository state verified: branch, HEAD, `origin/main` equality, tree status.
- Every roadmap stage traced to the confirmed facts above; every fact traced to
  a source file, a repository document, or a git object at the current HEAD.
- Contradiction search: removed architecture checked absent from the roadmap;
  historical/deprecated work checked excluded; no Stage 7/8 completion claimed.
- `git diff --check` clean; staged diff inspected; working tree inspected.

No runtime tests were run for this documentation-only task, and no live
Telegram, provider, or Supabase contact was made. That is not a limitation of
the roadmap — it is exactly the gap the roadmap schedules.

---

End of investigation. Planning document only — no implementation.
