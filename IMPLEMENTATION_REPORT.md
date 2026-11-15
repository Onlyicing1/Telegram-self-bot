# IMPLEMENTATION REPORT

## G4-A archive deletion and active-run retention correction (current — documentation only)

### Status

**Documentation correction only. G4-A implementation has not started, no stage is
complete, and no evidence/archive/glossary table exists.** No production Python,
test, migration, SQL, Supabase object, Telegram operation, runtime behavior or
deployment change was made.

Base: `origin/main` `76aacc851483dc2ade1d2dab46cd7e2e5a714743` (the verified
reference commit). The local checkout was clean and was fast-forwarded to that
commit before editing; it had no divergent work.

This delivery corrects two defects in the G4-A evidence-persistence design:

1. Revision-level and source-level archive deletion used different
   dependent-record and confirmation rules.
2. The design said archive deletion never touches active runs, without
   explaining that active-run refs may hold their own bounded excerpt copies of
   the same source text.

No deletion or retention behavior is implemented or live-verified. The
deletion/purge rules below are a **proposal awaiting owner approval**; only the
existing directions of manual deletion and no automatic archive expiry are
already approved.

### Deletion reconciliation

The design now gives revision-level and source-level deletion the same proposed
rule.

| Operation | Proposed behavior |
|---|---|
| Delete one archived revision | Explicit owner action. Preflight counts every registered dependent derived record (currently `ai_workspace_knowledge`; future dependent tables must join it), each `revision_id` reference, and matching active-run refs/excerpts. If derived records depend on the revision, deletion is blocked by default; a separate, explicit "delete dependents too" confirmation is required. Without dependents, one explicit confirmation proceeds. The source identity and sibling revisions are untouched. If it is the source's last revision, the source-level rule applies so no empty source is left behind. |
| Delete an archived source | Explicit owner action. Preflight counts all revisions, every registered dependent derived record across them, each reference, and matching active-run refs/excerpts. If any derived record depends on any revision, deletion is blocked by default; a separate explicit confirmation must list the full revision and dependent counts and then remove the source, all revisions and all dependent records together. Without dependents, one explicit confirmation lists the revisions and proceeds. |
| Dependent Workspace Knowledge | Never silently cascaded or orphaned. It blocks the default delete at either level and is removed only by the second explicit cascade confirmation. |
| Active Evidence Run refs | Not dependent records and not cascade-deleted by archive deletion. By default their bounded excerpts, hashes and historical `revision_id` remain until the run's normal TTL expiry, even if the archive revision or source is gone. The unresolved archive reference is labelled as deleted and is never presented as a live archive record. |
| Optional purge | Proposed only. After selecting a revision or source, the owner may separately choose to purge matching excerpts from active runs; source-level deletion may offer one purge covering all revisions. Purge removes matching refs/excerpts without deleting the runs and must never run automatically. An active run's bounded summary can also contain derived content; until a rule is approved, a purge must clear it or disclose that it remains until TTL. |

The proposal preserves these existing requirements: immutable archived
revisions, manual deletion, no automatic archive expiry, no eviction to satisfy
a quota, owner/Workspace isolation, bounded excerpts, local link construction,
and the Evidence Envelope's exclusion from generic tool/audit/conversation
history. No cleanup scheduler, silent eviction, automatic purge or background
deletion is introduced.

### Active-run copies and permanent deletion

The four relevant data classes are explicitly separate:

- **Archived source identity** — owner/source/message provenance only; no text.
- **Immutable archived revision** — the bounded archived text, hash and
  approval/provenance record.
- **Active Evidence Run refs** — a short-TTL bounded excerpt copy per ref, with
  a hash, message id, validation state and optional historical `revision_id`.
- **Derived Workspace Knowledge** — a claim/summary/entity record that
  references `revision_id` and stores no second copy of the source text.

Consequently, deleting an archived revision or source is **not** a global erase
of every copy of the text. It permanently removes the selected archive rows and,
with explicit cascade, dependent derived knowledge. Matching active-run excerpts
remain until their configured TTL unless the owner separately chooses a purge.
The confirmation must disclose that distinction. The design makes no claim of
immediate deletion from a run's optional summary, provider-side logs or any
other copy not covered by the approved purge scope.

### Decisions still pending owner approval

1. **Revision-level dependent deletion:** block by default and require a
   separate explicit cascade confirmation (recommended), or allow one-step
   cascade with an explicit warning.
2. **Source-level dependent deletion:** apply the same block and
   second-confirmation rule as revision-level deletion (recommended), or allow
   one source confirmation to cascade all revisions and dependents.
3. **Default active-run excerpt handling:** keep matching excerpts intact until
   normal TTL expiry without touching the runs (recommended), or require/offer
   an explicit purge as part of archive deletion.
4. **Optional purge:** whether it is offered at revision level, at source level
   as a separate option, or neither (recommended: offer it separately at both
   levels), and whether it must clear an active run's bounded summary as well
   as matching refs.

These are additions to the previously open G4-A decisions: storage model, TTL,
excerpt/ref/run caps, archive quotas, the re-archive approval policy,
expired-run tombstone versus deletion, and the configuration surface. None of
the numeric limits is approved.

### Files changed

| File | Change |
|---|---|
| `ROADMAP.md` | Corrected the retention description, replaced the deletion section with the proposed revision/source rule and active-run retention/purge boundary, updated the conceptual storage table, stage gate and owner-decision list. |
| `LIFEOS_IDEAS.md` | Clarified EVID-002 refs as excerpt snapshots, revised EVID-003 deletion, added §4.5.1 deletion semantics, recorded the new finding, updated G4-A and Step 5 gates/deliverables, expanded the owner-decision list and appended the change-log entry. |
| `IMPLEMENTATION_REPORT.md` | Replaced with this latest documentation-only delivery record; prior delivery records are preserved below. |

### Files intentionally untouched

- All `backend/` production code and `tests/`.
- `supabase/` migrations, `sql/`, `DATABASE_ARCHITECTURE.md` and any database
  object.
- `AGENTS.md`, `AI_MASTER_DESIGN.md`, `INVESTIGATION.md`, `README.md`,
  `docs/`, `guide/` and deployment configuration.
- No G4-A implementation, schema, migration, service, UI, scheduler or runtime
  path was created.

### Validation performed

- Verified branch/HEAD/worktree and fetched `origin/main` before editing:
  remote was `76aacc8`; local HEAD was a clean ancestor and was fast-forwarded
  with no merge, rebase or history rewrite.
- Repository-wide search for deletion, cascade, purge, archive, revision,
  active-run, retention and TTL wording across the repository, including
  Markdown, code and SQL; no competing G4-A implementation or deletion policy
  was found.
- Cross-document review of `ROADMAP.md`, `LIFEOS_IDEAS.md` and
  `IMPLEMENTATION_REPORT.md`; the old one-confirmation source cascade and the
  unqualified "never touches an active run" wording are gone.
- Markdown table column-consistency check over the edited documents.
- `git diff --check`.
- Reviewed the final diff and file list to confirm only the three scoped
  documentation files changed.

### Validation not performed

- **No production test suite was run**, because no application code changed and
  tests would not validate this documentation delivery. No application behavior
  is claimed tested.
- No SQL, Supabase connection, schema/RLS/FK check, migration, Telegram
  operation, live deletion, TTL expiry, purge or deployment was performed.
- No G4-A table or runtime behavior exists, so active-run retention and archive
  deletion could not be exercised live.

### Delivery record

- Commit: the single documentation commit containing this report. Its SHA and
  the independent post-push remote-main verification are stated in the delivery
  response; a file cannot contain the hash of the commit that introduces it.
- Verification: `git fetch` → compare remote `FETCH_HEAD` with local `HEAD` →
  `git merge-base --is-ancestor <sha> FETCH_HEAD` → clean final worktree.

### Remaining blockers before G4-A implementation

1. Owner approval of the storage model (sources, immutable revisions, run refs)
   and the proposed deletion/active-run retention and purge rules.
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

## Previous delivery — G4-A evidence-persistence design correction

Status: documentation only; base `origin/main`
`05424016f4f84d320ccde2af3ad8da5bfde5fc51`. No implementation or schema exists.

| Issue | Correction |
|---|---|
| Mutable archive snapshots | Archived sources hold identity only; immutable revisions hold text. Same-hash capture is a no-op; an edit creates a new owner-approved revision; references identify `revision_id`. |
| Temporary Workspace key | Removed. The persistent Glossary must use real G3-B Workspace identity/authorization; no substitute scope is permitted. |
| G4-B reassigned | G4-B keeps its original meaning (**Workspace-aware durable Evidence**). Restart-safe follow-ups are G4-A/F2; G4-C/G4-D are new documented IDs. |
| Unapproved numbers as facts | Measured facts, assumptions, estimates and proposals are separated; every numeric limit is **PENDING OWNER APPROVAL** and may not enter an implementation contract. |

That correction committed the design record at `76aacc8`. It did not create an
evidence, archive, revision, Workspace or Glossary table, and did not change
code, tests, SQL, Supabase, Telegram, deployment or `DATABASE_ARCHITECTURE.md`.

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
