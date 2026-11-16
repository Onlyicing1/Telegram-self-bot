# IMPLEMENTATION REPORT

**Reporting date:** 2026-11-16
**Latest delivery:** G4-A active-run expiry correction and report consolidation
**Base inspected:** `origin/main` `4faf695e842cdc6487788d78cf24fe44122ebe2a`
**Status:** G4-A is documented but **not implemented**; implementation remains
blocked pending owner approval.

This report describes only the latest documentation delivery and the current
project status. It is not an append-only changelog. Decisions that still matter
are summarized as current context, not as separate delivery histories.

## Latest delivery — G4-A expiry semantics (documentation only)

### Scope

This delivery corrected two documentation defects:

1. Active Evidence Run expiry was written as though logical expiry always led
   to physical deletion or a lazy sweep.
2. `IMPLEMENTATION_REPORT.md` had accumulated delivery sections instead of
   describing only the current delivery.

No application code, test, migration, SQL, Supabase object, scheduler, Telegram
operation, runtime behavior or deployment changed. No G4-A table or runtime
path exists.

### What changed

| File | Change |
|---|---|
| `ROADMAP.md` | Added the dated correction record; split retention into logical expiry and physical deletion; added the active-run expiry section and pending Options 1–3; corrected storage and quota wording; added the expired-run physical policy to the G4-A/F1 gate and owner decisions. |
| `LIFEOS_IDEAS.md` | Clarified EVID-002 expiry and risks; split the retention table; added the logical-versus-physical expiry subsection with Options 1–3; corrected deletion and limit wording; updated G4-A, Stage 5, findings and open decisions; appended the dated change-log row. |
| `IMPLEMENTATION_REPORT.md` | Replaced with this current-only report; no other delivery narratives are retained. |

### Logical expiry

- `expires_at` is evaluated against authoritative database time, never an
  untrusted client clock.
- Once expired, the run is unusable for normal retrieval, Explorer, Cited
  Summary and follow-ups, and is not valid evidence.
- A follow-up fails honestly. The expired run and its evidence are never
  reconstructed from Telegram history, archived evidence, a stored summary,
  inferred conversation or another source.
- Active-run expiry does not expire or modify immutable Archive revisions or
  derived Workspace Knowledge.

### Physical deletion

- Logical expiry does not delete rows and is not evidence of physical deletion.
- No cleanup scheduler, background worker, cron job or independent cleanup loop
  is approved or introduced.
- Any approved physical removal is opportunistic inside an existing authorized
  operation that touches the run or admits a new run. It is never a new periodic
  process.
- A never-read expired run may remain physically stored indefinitely. The design
  guarantees no fixed deletion period, and does not describe "expired and
  inaccessible" as "physically deleted".
- No live run, Archive revision or dependent Knowledge record is evicted to make
  room.

Expiry enforcement, physical removal, and any optional minimal tombstone remain
separate concerns.

### Expired-run physical options (all pending owner approval)

| Option | Proposed behavior | Consequences |
|---|---|---|
| **1. Physical deletion** (recommended, unapproved) | Delete expired run rows and their refs, with no tombstone, only through an existing authorized operation. | Strongest data minimization. Never-read rows can remain indefinitely. An old run id later looks like an absent run. |
| **2. Tombstone retention** (unapproved) | Delete evidence text, excerpts and summaries; retain only opaque `run_id`, `owner_id`, lifecycle status, `created_at`, `expires_at` and a deletion timestamp. | Records existence and expiry without evidence. `run_id` and `owner_id` exist solely for owner-scoped lookup; the status and timestamps prove only that the run existed and expired. No peer/message ids, ranges, content, summaries, provider payloads or recoverable evidence. It needs the same opportunistic cleanup, and adds a small metadata-retention footprint. |
| **3. Deferred physical deletion** (unapproved) | Logical expiry applies immediately; physical deletion waits for a separately approved mechanism. | No new scheduler. Storage and privacy exposure grow until such a mechanism is approved; no fixed deletion period. |

**Recommendation, not approved:** Option 1, outright physical deletion, because
Active Runs are temporary and no cleanup mechanism or evidence tombstone is
currently required. This is not an owner decision. Bounded physical retention
would require a separately approved mechanism.

### Current G4-A context preserved

- Active Evidence Runs remain durable across application restarts, temporary,
  TTL-bound and separate from permanent archive storage. Their storage backend,
  TTL and limits are not approved and no table exists.
- Archiving is system-suggested but requires explicit owner approval; there is
  no automatic archival. Archived evidence remains until manual deletion.
- Archive revisions are immutable and append-only. Capture is idempotent for the
  same content hash; changed content requires a newly approved revision; no
  earlier revision is overwritten. References identify the exact `revision_id`,
  and there is no mutable "latest revision" pointer.
- Source identity is separate from revision content. Telegram edits and deletes
  may update observed source status but never rewrite stored revision content.
- Derived Workspace Knowledge keeps provenance to its source revision. Dependent
  records block default archive deletion and require a separate explicit
  cascade; these rules remain pending approval.
- The Workspace Glossary depends on the real G3-B Workspace model; no temporary
  or substitute workspace key is permitted.
- G4-B retains its original meaning: Workspace-aware durable Evidence, not
  follow-up support. Follow-ups are G4-A/F2.
- Evidence envelopes, excerpts and evidence text remain outside generic
  ToolResult data, tool arguments, audit records, conversation history, provider
  continuation messages and fallback rendering. Generic results expose only the
  opaque run id and bounded non-sensitive counts or coverage.
- Archive deletion is not global erasure while active-run excerpt copies remain.
  Active-run excerpt retention or purge, and dependent Knowledge cascade, remain
  subject to explicit owner approval. No automatic cleanup or unapproved
  cascade is introduced.
- All TTLs, quotas, excerpt/reference/run limits and storage estimates remain
  proposals or estimates, not approved facts.

### Verification performed

- Inspected the live branch, local HEAD, worktree, remote `origin/main`, ancestry
  and divergence before editing. The checkout was clean and matched the verified
  remote base `4faf695e842cdc6487788d78cf24fe44122ebe2a`; no synchronization
  operation was needed.
- Read the complete current contents of `ROADMAP.md`, `LIFEOS_IDEAS.md` and
  `IMPLEMENTATION_REPORT.md` before editing.
- Searched the repository for conflicting wording about active-run expiry,
  physical deletion, tombstones, cleanup schedulers, archive retention,
  revision immutability, deletion cascades, G4-B and the G3-B dependency.
- Checked Markdown structure and table column consistency across the edited
  documents.
- Ran `git diff --check` and reviewed the complete diff and changed-file list.

No application test suite was run. This delivery changes documentation only;
no application behavior is claimed as tested.

### Not implemented or live-verified

- No G4-A application implementation, service, UI, repository, table, index,
  RLS policy or runtime path was created.
- No code or tests were changed.
- No SQL was written or executed; no migration, Supabase object, schema or
  `DATABASE_ARCHITECTURE.md` change was made.
- No scheduler, worker, cron job or cleanup loop was added.
- No Telegram operation, live expiry, deletion, purge, archive operation,
  storage measurement or deployment was performed.
- No claim is made that G4-A is ready to implement.

### Pending owner approval

1. The G4-A storage model for runs, refs, archive sources and immutable
   revisions.
2. Active-run TTL duration.
3. Maximum excerpt/snapshot size, refs per run, text per run and active runs per
   owner.
4. Archive revision/byte quotas and the configuration surface that holds all
   approved limits.
5. Revision-level deletion: block dependent derived records and require a
   separate cascade confirmation (recommended), or allow one-step cascade with a
   warning.
6. Source-level deletion: use the same block/second-confirmation rule
   (recommended), or allow one source confirmation to cascade all revisions.
7. Default active-run excerpt handling on archive deletion: retain until TTL
   (recommended), or require/offer a purge.
8. Optional purge scope: revision level, source level or neither; and whether it
   must clear a run's bounded summary as well as matching refs.
9. Whether every edited-message re-archive needs fresh owner approval or may use
   a bounded pre-approval policy.
10. Expired-run physical policy: Option 1 physical deletion (recommended,
    unapproved), Option 2 deletion plus a minimal non-content tombstone, or
    Option 3 deferred physical deletion. The trigger and, for Option 2, exact
    fields must also be approved. No cleanup scheduler is authorized.
11. G3-B Workspace persistence, identity and authorization model before the
    Glossary or G4-B can proceed.
12. The extraction schema and limits for G4-C.
13. G4-D assessment thresholds and the person-level judgment policy.
14. Cited Summary surface: panel-only, AI-trigger-only, or both.
15. Archive suggestion sources and how many suggestions may be shown.
16. Tagged-text fallback, if any, without weakening strict JSON validation.

No item above is approved by this documentation delivery.

### Remaining blocker

G4-A implementation is blocked on explicit owner approval of the storage model,
TTL, quotas, excerpt limits, archive deletion/active-run retention rules and the
expired-run physical policy. No schema or code may be authored before those
decisions.

### Delivery record

- Commit: the single documentation commit containing this report. Its SHA and
  the independent post-push remote-main verification are stated in the delivery
  response, because a file cannot contain the hash of the commit that introduces
  it.
- Push verification: fetch remote, compare remote `main` with local `HEAD`,
  confirm ancestry, inspect the pushed file list and confirm a clean worktree.
