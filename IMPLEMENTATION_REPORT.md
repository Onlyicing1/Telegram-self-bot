# IMPLEMENTATION REPORT

**Reporting date:** 2026-11-16
**Latest delivery:** G4-A evidence policy approval recorded (documentation only)
**Base inspected:** `origin/main` `b7836043027d869de261587de61c52cebab5d1d1`
**Status:** The G4-A evidence design decisions are owner-approved. G4-A is
**not implemented**; no evidence table, migration, SQL, Supabase object or
runtime path exists. Implementation remains gated on explicit owner
authorization of the schema/migration.

This report describes only the latest documentation delivery and the current
project status. It is not an append-only changelog. Decisions that still matter
are summarized as current context, not as separate delivery histories.

## Latest delivery — G4-A evidence policies (documentation only)

### Scope

The owner approved the complete G4-A evidence policy: retention classes, TTL,
active-run limits, archive quotas, immutable revisions, deletion rules,
re-archival behavior and the expired-run physical policy. This delivery records
those decisions in the canonical documentation so future sessions do not
re-open them or mistake them for implementation.

No application code, test, migration, SQL, Supabase object, scheduler, Telegram
operation, runtime behavior or deployment changed. No G4-A table or runtime
path exists.

### What changed

| File | Change |
|---|---|
| `LIFEOS_IDEAS.md` | Replaced the pending §4.5.1 status and numeric placeholders with the approved data classes, TTL, limits, archive quotas, physical-deletion policy, deletion/dependency rules and immutable-revision rules. Updated EVID-001/002/003 dependencies and wording, G4-A/G4-C boundaries, stage/readiness gates, §10 decisions 3/17/19 and the change log. |
| `ROADMAP.md` | Recorded the approved policy in the design record; replaced pending TTL/limits/quota/deletion sections with approved values and Option 1; updated retention, quota, staging and readiness text; reduced the owner-decision list to the remaining open questions. |
| `IMPLEMENTATION_REPORT.md` | Replaced with this current-only report. No historical delivery sections are appended. |

### Approved evidence policy

The canonical detailed record is `LIFEOS_IDEAS.md` §4.5.1; `ROADMAP.md` holds
the implementation-facing summary. In brief:

- **Active Evidence Runs** are durable across application restarts, temporary
  and TTL-bound. The default TTL is 7 days; the owner selects 1–30 days at
  creation and the value is persisted for that run. Expiry uses authoritative
  database time, immediately denies retrieval/Explorer/Cited Summary/follow-up,
  and is never silently extended. Expired runs are never reconstructed from
  Telegram, the Archive, summaries or conversation history.
- **Expired physical deletion** is Option 1: opportunistic deletion of the run
  and its refs only inside an existing authorized operation. There is no
  scheduler, worker, cleanup loop, tombstone or promised physical-deletion
  deadline. An untouched expired run may remain physically stored indefinitely.
- **Approved active limits:** excerpt 1,000 characters; 50 references per run;
  20 references per follow-up; 32 KiB normalized text per run; 20 active runs
  per owner. At the cap, expired runs may be removed opportunistically; if
  capacity is still unavailable the new run is rejected explicitly. A valid,
  unexpired run is never evicted.
- **Evidence Archive** is manual and owner-approved. Revisions are immutable and
  append-only; the same content hash is a no-op; changed content requires a new
  owner-approved revision with no automatic refresh or replacement; references
  identify the exact revision and there is no mutable “latest” pointer.
  Telegram edits/deletes update observed source status only and never rewrite
  the snapshot.
- **Archive quotas:** 5,000 records or 50 MiB per owner, whichever is reached
  first. A reached quota rejects new archival and reports the reason; archived
  evidence is never automatically deleted, evicted or overwritten. These are
  application planning limits, not exact PostgreSQL billing or physical-storage
  measurements.
- **Archive deletion:** dependent records block deletion by default at both
  revision and source level. A separate explicit owner confirmation with a
  clear impact explanation is required for a cascade; no silent cascade exists.
  Archive deletion does not delete active-run excerpt snapshots, which remain
  governed by their own TTL. No purge is approved or specified; one may be
  considered separately, but implementation must not invent it.
- **Workspace Glossary:** scoped to the real G3-B Workspace. No fake or
  temporary workspace key is permitted. Glossary quotas and implementation are
  deferred until G3-B establishes real Workspace identity, persistence and
  authorization.

### Privacy and architecture invariants preserved

- Evidence Envelopes never enter generic `ToolResult` data, tool arguments, tool
  history, audit records, conversation history, continuation prompts or fallback
  rendering. Generic results expose only an opaque run id and bounded
  non-sensitive counts/coverage.
- Provider inputs receive only minimum bounded excerpts and opaque local keys.
  Telegram message ids, sender ids, owner ids, Workspace ids, URLs and hashes are
  never exposed to the provider, and no Telegram conversation context is passed
  into isolated media/evidence analysis.
- There is no second scheduler, executor, Telegram client, listener or update
  loop; `ToolExecutor` remains the sole AI tool execution authority. AI gains no
  arbitrary Telegram RPC, SQL, shell, filesystem or unrestricted HTTP execution.
- Scheduled History remains body-only, citation-free and outside the evidence
  system.

### Boundaries and dependencies

- **G4-A / F1** is the single-source evidence foundation (EVID-001/002/003,
  INTEL-001/002/003/037). Its policy is approved; it is not implemented.
- **G4-A / F2** is restart-safe follow-ups on the same contract, not a new
  stage. It remains blocked by F1 and needs the approved TTL to prove usable in
  practice.
- **G4-B** keeps its original meaning: Workspace-aware durable evidence, blocked
  by G3-A/G3-B and the real Workspace model.
- **G4-C** is strict-JSON extraction plus the persistent Workspace Glossary.
  Glossary storage is blocked until G3-B; glossary quotas are deferred.
- **G4-D** is threshold-gated subjective assessment and remains blocked on
  explicit assessment and person-level policies.

### Current state and blockers

- No `ai_evidence_runs`, `ai_evidence_run_refs`, `ai_evidence_sources`,
  `ai_evidence_revisions`, `ai_workspace_glossary` or
  `ai_workspace_knowledge` table exists.
- `DATABASE_ARCHITECTURE.md` was inspected read-only and was not modified.
- The remaining implementation gate is explicit owner authorization of the
  schema/migration. Supabase schema changes remain manual and are not authorized
  by the design approval or by this documentation task.
- Remaining open product decisions are the limit configuration surface, Cited
  Summary surface, archive suggestions, glossary quotas after G3-B,
  assessment/person policy thresholds, an optional tagged-text fallback, and
  global versus per-Workspace limit configuration. They do not reopen the
  approved numeric policy.

### Verification performed

- Inspected the freshly fetched `origin/main` at `b783604`.
- Inspected `AGENTS.md`, `LIFEOS_IDEAS.md`, `ROADMAP.md`,
  `IMPLEMENTATION_REPORT.md` and `DATABASE_ARCHITECTURE.md` (read-only).
- Checked that the approved values, Option 1 policy, immutable revisions,
  deletion/dependency rules and G3-B deferral are consistent across the edited
  documents.
- Ran documentation whitespace and diff checks; no code, test, SQL, Supabase,
  migration, Telegram or deployment artifact was produced.

### Not implemented or live-verified

- No G4-A feature, evidence table, migration, SQL, Supabase object, scheduler,
  Explorer, Cited Summary, follow-up path, archive workflow, extraction,
  glossary or assessment exists.
- No tests were added or run because this delivery changes documentation only.
- No Telegram, Render or live provider operation was performed.
