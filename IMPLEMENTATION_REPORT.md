# IMPLEMENTATION REPORT

## G4-A evidence-persistence design correction (current — documentation only)

### Status

**Documentation and architecture correction only. G4-A implementation has not
started, no stage is complete, and no evidence/archive/glossary table exists.**
No production Python, test, migration, SQL, Supabase object, Telegram operation,
deployment or `DATABASE_ARCHITECTURE.md` change was made.

Base: `origin/main` `05424016f4f84d320ccde2af3ad8da5bfde5fc51` (verified before
editing). This delivery corrects the design record committed there.

### What was corrected

| Issue | Previous record | Corrected record |
|---|---|---|
| **1. Mutable archive snapshots** | One archive row per `(owner, peer, message id)`; re-archiving could refresh/overwrite the stored snapshot. | Archived **sources** hold identity only; **immutable revisions** hold text. Identical hash = no write. An edited message creates a new owner-approved revision; no overwrite, merge or "replace revision" path exists. References identify `revision_id`, so earlier analyses stay traceable. |
| **2. Fake Workspace scope** | The persistent Glossary used a temporary owner-scoped "workspace key". | The substitute is removed. The Glossary must use the real Workspace identity/ownership/authorization from **G3-B**; no owner-level key, temporary table or parallel scope is permitted. Glossary persistence/UI/injection is **blocked until G3-B**. |
| **3. G4-B overwritten** | G4-B was reassigned to restart-safe follow-ups. | **G4-B keeps its original meaning: Workspace-aware durable Evidence.** Follow-ups are G4-A's second delivery phase (G4-A/F2). G4-C (extraction + persistent Glossary) and G4-D (assessments) are new IDs, explicitly documented, not renamings of G4-B. |
| **4. Unapproved numbers as facts** | Point row-size estimates and quota-looking numbers (5,000 records, 50 MiB, 20 runs, 7-day TTL, 1,000 chars, 50 refs, 32 KiB, 200 entries, 50,000 chars). | Measured facts, assumptions, estimates and proposals are separated. PostgreSQL overhead (tuple/page/TOAST/index/fillfactor/bloat/revision multiplier) is explained. Every numeric limit is marked **PENDING OWNER APPROVAL**, and no value may be hard-coded into an implementation contract. |

### Corrected archive revision model (design only)

- `ai_evidence_sources`: stable identity per `(owner_id, peer_id, message_id)`;
  stores no text.
- `ai_evidence_revisions`: append-only immutable rows — `revision_id`,
  `source_id`, `revision_no`, `content_hash`, bounded text snapshot,
  captured-at, owner-approval reference, last observed source state.
- Same hash → no write (idempotent). New hash → candidate revision requiring
  explicit owner approval. No update/merge/replacement of stored text.
- Runs, citations and derived knowledge reference a specific revision. Nothing
  means "latest".
- Telegram edits/deletes never mutate stored text; revalidation records
  `live`/`edited`/`deleted`/`inaccessible` and a check time only.
- Deletion is owner-confirmed per revision or source; dependent derived records
  are reported and block deletion by default until an explicit cascade. No
  automatic expiry and no eviction at a future quota.

### Corrected Workspace and stage dependencies

| Stage | Scope | Dependency |
|---|---|---|
| G4-A / F1 | Single-source durable runs (TTL), immutable archive revisions, opt-in Cited Summary, Explorer with local links | G2-A + History; owner approval of model/TTL/quotas |
| G4-A / F2 | Restart-safe follow-ups on the same contract (not a new stage) | G4-A / F1 |
| G4-B *(original)* | Workspace-aware durable evidence | G4-A + G3-A + G3-B |
| G4-C *(new)* | Strict-JSON extraction + persistent Workspace Glossary | G4-A + **G3-B for the Glossary** + approved schema/limits |
| G4-D *(new)* | Threshold-gated subjective assessments | G4-C + approved thresholds/person policy |

### Approved decisions preserved (not weakened)

1. Active Evidence Runs are durable and expire on an explicit TTL.
2. Archival is opt-in; the system may suggest, the owner approves.
3. Archived evidence remains until manual deletion.
4. No automatic archival.
5. No whole-chat archival.
6. No media-binary archival.
7. Workspace Knowledge is derived, structured, persistent and linked to source
   evidence.
8. Cited Summary is explicitly selected, never automatic.
9. Follow-ups survive application restart.
10. The Evidence Envelope never enters `ToolResult.data`, tool arguments, tool
    history, audit records, conversation history, provider continuation or
    fallback tool-result rendering.
11. Generic tool results expose only opaque run/revision ids and bounded
    non-sensitive counts/coverage.
12. Scheduled History remains unchanged, body-only and citation-free.
13. This task authorizes no Supabase schema change; the owner applies schema
    changes manually.

### Decisions still pending owner approval

1. Active-run TTL duration.
2. Excerpt/snapshot size, refs per run, active runs per owner.
3. Archive revision/byte quotas and the configuration surface holding limits.
4. Revision deletion policy with dependent derived knowledge (block vs explicit
   cascade).
5. Whether every edited-message re-archive needs a fresh approval.
6. Glossary entry/character limits (after G3-B).
7. Cited Summary surface (panel / AI trigger / both).
8. Archive-suggestion source.
9. Assessment thresholds and person-level policy.
10. Tagged-text fallback timing.
11. Expired-run tombstone vs outright deletion.
12. Global vs per-Workspace limit configuration.

### Files changed

| File | Change |
|---|---|
| `ROADMAP.md` | Design record replaced: correction record, immutable revision model, deletion/unavailability rules, real Workspace dependency, quota-status table, storage methodology, corrected stage plan, roadmap-change record, open decisions. |
| `LIFEOS_IDEAS.md` | EVID-001/002/003/004 revised; §4.5.1 rewritten; LANG-005 blocked on G3-B; dependency graph, §5.3–§5.5, G4 stages, priorities, sequence, deferred items, open decisions and change log corrected. |
| `IMPLEMENTATION_REPORT.md` | This report replaces the previous document with the latest verified state; prior deliveries are summarized below. |

### Files intentionally untouched

- All `backend/` production code and `tests/`.
- `supabase/` migrations, `sql/`, and `DATABASE_ARCHITECTURE.md` (this task
  forbids modifying it).
- `AGENTS.md`, `AI_MASTER_DESIGN.md`, `INVESTIGATION.md`, `README.md`,
  `docs/`, `guide/` (no G4 stage definitions requiring correction; checked by
  search).

### Validation performed

- Re-verified `origin/main` before editing: `0542401`; worktree clean.
- Re-checked the source references cited by the design (executor/dispatcher/
  persistence/prompt paths, History bounds, `history_schedules` migration and
  service-role-only RLS posture). No implementation was added.
- Repository-wide search for `G4-A|G4-B|G4-C|G4-D|Workspace-aware|restart-safe|
  Evidence Archive|Workspace Glossary`; all active occurrences now agree.
- Confirmed `G4-B` no longer appears as follow-ups; follow-ups are G4-A/F2.
- Confirmed no active design instruction uses a temporary workspace key or
  substitute scope; only the correction record names the removed proposal.
- Confirmed every numeric limit is labelled pending/unapproved.
- Markdown table column-consistency check over both edited documents: 0
  inconsistent tables.
- `git diff --check`: clean.

### Validation not performed

- No production test suite was run; no production code changed, so it would not
  validate this delivery. No application behavior is claimed tested.
- No live PostgreSQL storage measurement, `pg_total_relation_size`, TOAST
  inspection, Supabase read/write, SQL execution, Telegram operation or
  deployment was performed. All size figures are explicitly estimates.
- No schema/RLS verification was possible because no schema exists.

### Delivery record

- Commit SHA: the single documentation commit containing this report; its SHA
  and the post-push remote-main verification are stated in the delivery
  response (a file cannot contain the hash of the commit that introduces it).
- Remote main verification: `git fetch` → compare remote `FETCH_HEAD` with
  local `HEAD` → `git merge-base --is-ancestor <sha> FETCH_HEAD` → clean
  worktree. Results are reported in the delivery response.

### Remaining blockers before G4-A implementation

1. Owner approval of the storage model (sources + immutable revisions + run
   refs) and the deletion/dependent-record policy.
2. Owner approval of the TTL, excerpt size, refs/run, runs/owner, archive
   quotas, and the configuration surface that holds them.
3. A decision on whether every revised archive needs fresh approval.
4. For G4-B: completion and approval of G3-A/G3-B Workspace identity/settings.
5. For G4-C: completion and approval of G3-B (Glossary) and the extraction
   schema/limits.
6. For G4-D: approved thresholds and person-level policy.
7. No SQL, migration or schema change may be authored until the owner approves
   the model and applies it manually.

---

## Previous delivery — G2-A: global, request and task language

Status: implemented offline. No schema, migration, Supabase, Telegram or
deployment change was made.

- **One catalog/resolver** (`backend/ai/language.py`): `en`, `fa`, `ar`, `zh`
  with stable codes, labels, directionality, provider hint and explicit `auto`;
  unsupported values raise `LanguageError` instead of becoming English.
- **Precedence:** explicit request > explicit task > global
  `ai_preferences.language` > English. `panel_settings.language` stays UI-only.
- **Auto** is summary-only (source-script detection, documented English
  fallback) and never overrides an explicit task language.
- **One preference snapshot** per request drives the prompt and every tool;
  `AIRequest.language` means "explicit only".
- **History:** `summarize_history` accepts a validated language; the same
  decision drives map and reduce. Result metadata carries effective language,
  source and Auto detection. The panel has a request-level language selector.
- **Tasks:** Taskloom uses the catalog; generated-content instructions freeze
  the global default at creation; `preparation_policy` is unchanged.
- **Rejection:** an invalid explicit request language, or an invalid stored
  preference that would actually be used, fails through `EngineResult` before
  any provider call; no English/Auto fallback is substituted. Prompts render
  `Global default language` separately from the effective request language.
- **Tests observed at the final corrective pass:** 36 language tests, 308
  focused, full suite 7,912 passed / 27 skipped / 1 failed (the pre-existing
  scheduler recovery test). Exit status 1.
- **Limitations:** the global language lives in the in-memory
  `ai_preferences` repository (no deployed table), so it resets on restart;
  natural-language task instructions stay verbatim; provider compliance is not
  verified. G2-B (Workspace language), G4-A and G5-A remain unimplemented.

## Previous delivery — G1-A: reusable interaction grammar

Implemented offline for Stash and Taskloom; UI interaction only.

- Documented panel ordering (primary action, grouped controls, item controls,
  contextual bulk actions, navigation/pagination).
- `selection_state.py` keys sessions by owner + feature scope, preserves the
  Stash API, 40-item cap, 900-second expiry and exact identity; Taskloom gets
  its own scope.
- Taskloom multi-select: exact `task_id:version` callbacks, pagination survival,
  explicit clear, contextual Pause/Resume/Complete/Delete.
- Every selected target is re-read through `TaskManagementService`; missing,
  foreign, changed-version and status-inapplicable targets are refused
  individually and reported per item.
- Bulk delete has a dedicated review (count/targets/consequence), Cancel,
  DANGER styling and revalidation at confirmation.
- No new scheduler, executor, repository, selection store, renderer, Telegram
  client, listener or persistence path.
- **Tests observed:** 146 focused passed; full suite 7,876 passed / 1 failed /
  27 skipped (same pre-existing scheduler test).

## Previous delivery — Scheduled History diagnostics tracing (Phase 7 prerequisite)

Observability only; no execution, scheduler, cursor, CAS, retrieval, provider,
creation or compensation semantics changed.

- `SCHEDULED_OCCURRENCE_TRACE` uses the exact occurrence key as its stable
  `trace_id` plus a separate attempt; `HISTORY_SCHEDULE_TRACE` covers owner
  creation and compensation. Ordinary Telegram/AI traffic stays silent, and the
  existing `AI_TASK_TRACE` create-task binding is untouched.
- Covered stages: owner creation/compensation, scheduler recovery/due/claim/
  retry/notification, coordinator validation/action chain/timeouts, tool
  provenance/arguments, durable state/peer policy/retrieval, AI/provider,
  output validation, CAS commit and repository fallback outcomes.
- Trace fields are bounded and content-free: ids, statuses, counts, cursors,
  revision/version, exception classes and allowlisted provider categories. No
  message text, summaries, instructions, prompts, credentials or peer
  identifiers are logged.
- **Tests observed:** 22 tracing cases passed; focused set 139 passed / 1 failed
  (pre-existing scheduler recovery test); full applicable suite 7,856 passed /
  27 skipped / 1 failed.
- **Phase state:** Phase 3A design complete; Phase 3B migration applied remotely
  as `20261114180758`; Phase 4 wizard and Phase 5 prerequisite implemented;
  Phase 6 (shared admission) and Phase 7 (live validation) deferred. Live
  scheduled execution, restart/catch-up, deleted-message gaps and
  provider-failure runs remain unverified.

## Historical state summary (unchanged)

- Scheduled History is durable-only: an unavailable store raises
  `HistoryStorageUnavailable`; process memory is a test fake, never a
  production substitute.
- `history_schedules` is service-role-only: RLS enabled, zero policies, all
  privileges revoked from PUBLIC/anon/authenticated.
- No Evidence, Archive, Revision, Workspace or Glossary table exists; no G4
  stage is implemented.
- The pre-existing scheduler recovery test failure remains open and untouched.
