# ROADMAP

## Current delivery: G4-A / F1 — Single-source evidence foundation

Status: **G4-A/F1 database schema and privilege hardening are applied to the
live Supabase project and verified (2026-11-17).** The application layer was
hardened by the G4-A delivery-readiness pass on **2026-11-18** (see below).
G2-B (Workspace language), G4-A/F2 (follow-ups), G4-B (Workspace awareness),
G4-C (extraction/Glossary), G4-D, G5-A and later Scheduled History phases
remain deferred. Live PostgreSQL catalog, grants, triggers and sequential RPC
smoke tests passed. Telegram, provider, Explorer callback and Render runtime
flows remain unverified; Evidence is not live-enabled until the application is
deployed and those flows pass. The first delivery's six findings were remediated
(M5 remains deferred until dependent records exist). The implementation is in
`backend/evidence/`, the owner-only Explorer and the two applied Evidence
migrations.

### Delivery readiness pass — 2026-11-18 (G4-A, application layer)

Verified against `6ac156e` and fixed, without touching the database:

- V1 the archive-confirmation panel raised `NameError` (an undefined `key`), so
  the Explorer's only revision-creating path never rendered;
- V2 every success notice used a non-existent `sym.STATE_SUCCESS`, so a
  completed archive or deletion was reported as a failure after its durable
  write;
- V3 source/revision delete callbacks were 66–107 bytes, above Telegram's
  64-byte limit, and were silently truncated into unresolvable ids;
- V4 `observed_state` had no writer, so the approved exact-source revalidation
  (`live`/`edited`/`deleted`/`inaccessible`) was unreachable;
- V5 an unapproved `archive_ref` could still write the ref's revision link.

Also added: archive and revision pagination (the archive list was silently
capped at 20 sources), the canonical `is_owner` gate, and durable RPC
argument/response contract tests. Evidence tests are now 51 (18 foundation + 21
remediation + 12 delivery); the focused panel/history/tool group is 1430 passed;
the full suite is 7960 passed, 27 skipped, 4 pre-existing failures (three
`DATABASE_ARCHITECTURE.md` inventory assertions left untouched by instruction,
and the known time-dependent scheduler test). No SQL was executed, no migration
was added and no runtime deployment was attempted. `G4-A/F2` below keeps its
original meaning (restart-safe follow-ups) and stays deferred.

### What G2-A adds

- **One catalog and resolver** (`backend/ai/language.py`): stable codes with
  labels and directionality for `en`, `fa`, `ar`, `zh` (Persian and English
  required; Taskloom's existing four preserved). External values are validated;
  unsupported values are refused, never coerced.
- **Deterministic precedence:** explicit request > explicit task > global
  `ai_preferences.language` > English. The Glass UI language
  (`panel_settings.language`) is untouched and remains UI-only.
- **Explicit Auto:** available for source-based summaries, never inferred, and
  never able to override an explicit task language. A source's dominant script
  is used when it can be resolved; otherwise the documented English fallback is
  reported honestly. Translation exposes concrete languages only (Auto there
  would be a no-op).
- **One preference snapshot per request:** the dispatcher loads preferences
  once, resolves the language, and passes the same decision to the prompt and to
  every tool. `AIRequest.language` now means "explicit only" (empty otherwise).
- **History:** `summarize_history` accepts a validated language; the same
  decision drives every map call and the reduce call; translation routes its
  language through the same catalog. Result metadata carries the effective
  language, its source and Auto detection. The panel gains a request-level
  language selector and renders the effective language honestly.
- **Tasks:** Taskloom's language choices come from the catalog; the wizard's
  generated-content instruction freezes the owner's global default at creation
  (an explicit task language always wins), so a later preference change cannot
  rewrite an existing task. `preparation_policy` is unchanged and still
  fail-closed.
- **UI:** AI → Settings exposes the global content language (with an honest
  in-memory note while `ai_preferences` has no deployed table). Taskloom keeps
  its task-level selector and shows the effective language in review.

### Limitations reported for G2-A

- The global content language lives in the existing in-memory
  `ai_preferences` repository: there is no deployed `ai_preferences` table, so
  the setting resets on restart. The UI says so instead of claiming a durable
  write.
- Natural-language task instructions are preserved verbatim (source-fidelity
  contract), so the global default is frozen only for wizard-generated content.
  Freezing it for NL tasks needs a durable task-language field (schema change),
  which G2-A does not add.
- No extraction surface exists yet; G4-C must consume the same resolver.
- No live Telegram or provider compliance test was performed.

### G2-A corrective pass (2026-11-15)

- **Invalid language values are rejected.** The dispatcher refuses an
  unsupported explicit request language (or an invalid stored global
  preference it would actually use) through the existing `EngineResult` failure
  convention, before any provider call. No English fallback is substituted and
  no fallback decision is recorded.
- **Prompt language is unambiguous.** The preferences block renders the global
  default as `Global default language`; the runtime context renders the
  effective request language as `Language: … (effective for this response)`.
  Auto keeps its honest detected/fallback wording.
- Catalog, aliases, precedence, the single preference snapshot and the
  one-resolution-per-request design are unchanged. No schema or live operation.

## Implementation record: G4-A / F1 evidence persistence and bounded storage

Status: **G4-A / F1 code implemented offline — 2026-11-16 and remediated
2026-11-17; database migrations applied to live Supabase — 2026-11-17.**
Production code, focused tests, an owner-only Explorer, the durable Evidence
schema and the privilege-hardening migration exist in the repository and live
database. The canonical policy remains `LIFEOS_IDEAS.md` §4.5.1. Live database
triggers, grants and sequential quota/archive RPC probes passed. The bot's
actual service-role client, Telegram retrieval, provider round-trip, Explorer
callbacks and Render deployment have not been verified; the runtime remains
not live-enabled until those checks pass. G4-A/F2 remains deferred.

### F1 remediation record (2026-11-17)

The audit of `7f02e88` reported six issues. Each was re-checked against source
before changing anything:

| # | Finding | Result |
|---|---|---|
| M1 | A ref never identified the revision it produced | **FIXED.** Archiving now writes a write-once `revision_id` onto the ref after validating owner/chat/message; a database trigger and a column-level grant enforce the same rule, so no other ref update is possible and no "latest" pointer exists. |
| M2 | Silent in-memory fallback could report a durable success | **FIXED.** `EvidenceRepository()` is durable-only; an unavailable Supabase raises `EvidenceStorageUnavailable` instead of storing in process memory. Memory is reachable only through the explicit test double. |
| M3 | Quota checks were check-then-act | **FIXED at the application and SQL boundary.** Run admission and archive writes are one serialized operation (`pg_advisory_xact_lock` in `ai_evidence_create_run` / `ai_evidence_archive_revision`; one lock in the test double). Sequential live PostgreSQL quota/archive RPC probes passed; production concurrency and lock-contention stress testing remain unverified. |
| M4 | Cited Summary could run without per-request opt-in | **FIXED.** The service refuses without `opt_in=True`; the AI tool is confirmation-required and cannot auto-execute; the panel toggle is cleared after every run, including the link path. Consent is never remembered. |
| M5 | Dependency check was not part of the deletion operation | **DEFERRED WITH BLOCKER.** F1 has no dependent record class; the durable `count_*_dependents` methods are explicit zero stubs. Before any dependent table exists (G4-C), the preflight and the delete must move into one database transaction and return the dependent list. |
| M6 | Explorer held owner results in module globals | **FIXED.** Every global result cache was removed; views receive the data their own handler loaded, and each opaque id is re-resolved through an owner-filtered service call. |

Verification in that sprint: 35 Evidence tests (including 17 new regression
tests), a 288-test registry/schema/confirmation/document group, and a full run
of **7947 passed, 27 skipped, 1 pre-existing time-dependent scheduler failure**
(`test_recovered_interrupted_occurrence_is_picked_up_by_run_once`, which fails
because its fixed 2026-09-10 schedule is advanced to the current wall clock).
At the end of the offline remediation sprint the migration was still unapplied.
The owner subsequently authorized live application and verification on
2026-11-17; see "Live Supabase setup and verification" below. No Telegram,
Render or provider operation was performed.

### Live Supabase setup and verification (2026-11-17)

Project `telegram-self-bot` (`burfebhahuowywyfgcsc`), PostgreSQL 17.6,
region `ca-central-1`.

Applied remote migrations and matching repository filenames:

- `20261117123943_create_evidence_foundation.sql`
- `20261117124032_harden_evidence_privileges.sql`

Live checks passed:

- Four Evidence tables exist, all with RLS enabled and zero policies.
- Run-expiry, Ref INSERT/UPDATE ownership/revision guard, and revision
  immutability triggers exist and are enabled.
- A live PostgreSQL probe rejected a mismatched Ref/Run owner, accepted a valid
  same-owner ref, allowed one exact revision link, rejected repointing and
  excerpt/revision mutation, and confirmed database-time TTL calculation.
- RPC probes confirmed active-run quota refusal, archive creation, same-hash
  deduplication and changed-content revision numbering. All probe rows were
  cleaned up.
- Exact grants are enforced: refs have UPDATE on `revision_id` only;
  revisions have no UPDATE; client roles have no Evidence table access.
  All seven Evidence functions are executable by `service_role` only and
  have fixed `search_path = pg_catalog, public`.
- The first live ACL inspection found inherited project defaults giving
  `service_role` broader table-level privileges than intended. The second
  migration revoked those defaults on the four Evidence tables and reapplied
  the exact approved grants.

**Pre-existing migration-history drift remains unresolved.** The repository has
44 SQL migration files, while the remote Supabase history has 8 rows. Of those,
39 local timestamps are absent remotely and 3 remote versions have no matching
local filename. The two new Evidence migration versions match exactly. This
historical mismatch was not repaired because doing so safely requires auditing
the state represented by every older migration. **Do not run a bulk
`supabase db push` until that history drift is separately reconciled.**

The Evidence database layer is applied and verified, but the runtime is still
not live-enabled pending actual bot-client, Telegram, provider and UI checks.

### Approved owner decisions (unchanged)

| # | Decision | Effect on G4-A |
|---|---|---|
| 1 | Working evidence is temporary. Nothing is archived automatically; selected evidence may be archived only through an owner-confirmed workflow. | Two retention classes, never one unlimited table. |
| 2 | Ordinary summaries stay plain. Cited Summary is an explicit, owner-selected mode. | The existing summary default does not change. |
| 3 | Follow-ups must remain usable after an application restart. | The active run and the context it needs must be durable. |
| 4 | Conditional subjective assessments are allowed only after explicit evidence thresholds and assessment policies are approved. | No threshold, default or confidence rule may be invented. |
| 5 | Person-level judgments require explicitly approved policies and thresholds. | Gated; no provider may assign a definitive person label. |
| 6 | The Workspace Glossary is persistent, owner-editable and extensible, and distinct from AI memory and source evidence. Its storage follows the real Workspace ownership model. | Bounded store, **blocked on G3-B**; no temporary or owner-level Workspace substitute. |
| 7 | Direct Telegram links are allowed in the owner-facing Evidence Explorer, but identifiers and URLs are resolved and constructed locally. | Provider output is never authoritative for ids, counts, links or existence. |
| 8 | Strict JSON is the initial structured-output contract. A tagged-text fallback may be specified later. | No fallback now, and no silent downgrade of a JSON operation. |
| 9 | Evidence Envelopes never enter ToolResult history, tool arguments, generic audit records, conversation history or provider continuation. Tool results may carry only opaque run ids and bounded counts/coverage. A dedicated access-controlled evidence store is permitted. | A new storage path, never the generic tool path. |
| 10 | Scheduled History is unchanged: body-only and citation-free. | `scheduled_summary_payload` and its callers are untouched. |
| 11 | Archive is hybrid: the system may suggest candidates and explain why, the owner approves each archival, and archived evidence remains until manually deleted. | No automatic archival and no automatic archive expiry. |

### Correction record (2026-11-15, documentation only)

| # | Previous design | Corrected design |
|---|---|---|
| 1 | One archive row per `(owner, peer, message id)`, and a re-archive could refresh/overwrite the snapshot. | Append-only immutable revisions. An edit produces a **new** owner-approved revision; no operation overwrites historical text. |
| 2 | A temporary owner-scoped "workspace key" substituted for the missing Workspace model. | **No substitute.** The Glossary depends on the real G3-B Workspace ownership/authorization model and stays design-only until it exists. |
| 3 | G4-B was reassigned to restart-safe follow-ups. | **G4-B keeps its original meaning: Workspace-aware durable evidence.** Follow-ups are a G4-A delivery phase (G4-A/F2), not a new stage. G4-C/G4-D are new IDs, documented below. |
| 4 | Point row-size estimates and quota-looking numbers were presented too confidently. | Measured facts, estimates, assumptions and proposals are separated. At the time, every numeric limit was marked **PENDING OWNER APPROVAL**; the 2026-11-16 approval now supplies the limits below and in `LIFEOS_IDEAS.md` §4.5.1. |
| 5 | Revision deletion blocked on dependents, but source deletion cascaded after one confirmation; active runs were described only as untouched despite holding excerpt copies. | Both archive levels use the same approved block/second-confirmation rule; active-run excerpts are explicitly TTL snapshots retained by default. No purge is approved or specified. |
| 6 | Expiry was described as if physical deletion or a lazy "sweep" always followed logical TTL expiry, and the unresolved physical policy was reduced to "tombstone versus deletion". | Logical expiry is authoritative-database-time and immediately blocks use. Physical deletion is not automatic; no cleanup scheduler is approved, never-read expired rows can remain indefinitely, and the owner subsequently resolved this as Option 1 (2026-11-16). |

### Retention classes (must remain distinct)

| Class | Lifetime | Logical expiry | Physical deletion | Authority |
|---|---|---|---|---|
| **Active Evidence Run + refs** | 7-day default; owner-selected 1–30 days, stored per run | Yes — authoritative database time; immediate denial; no scheduler | Option 1: opportunistic physical deletion inside an existing authorized operation; no tombstone; no guaranteed deadline | Supports Cited Summary, the Explorer and restart-safe follow-ups; each ref holds its own bounded excerpt copy, not an archive pointer. |
| **Archived Evidence Source + revisions** | Indefinite until manual deletion | No | Manual owner deletion only | Owner-approved long-term source; one or more immutable revisions. |
| **Derived Workspace Knowledge** | Derived record, later stage | No | Explicit, owner-confirmed archive-deletion cascade only | References an archived revision id; never a second copy of the text. |
| **Workspace Glossary** | Persistent | No | Owner CRUD only | Owner definitions; depends on the real Workspace model. |

An Active Run is **not** an archive. An archived source is **not** a chat
export. A derived record is **not** a source. The glossary is **not** evidence.

### Active Evidence Run expiry (APPROVED 2026-11-16)

**TTL and logical expiry**

- The TTL is selected when the run is created and persisted for that run:
  default **7 days**, owner-selectable range **1–30 days**. It is never silently
  extended.
- `expires_at` is compared with authoritative database time. A client clock is
  never the authority.
- Once `expires_at` has passed, the run is immediately unusable for Cited
  Summary, Explorer reads and follow-ups. It is not valid evidence.
- A follow-up fails honestly. The system must not reconstruct the expired run,
  or its evidence, from Telegram history, archive revisions, the run's summary,
  inferred conversation or any other source.
- Expiry of an Active Run does not expire or modify immutable Archive revisions
  or derived Workspace Knowledge.

**Physical deletion — Option 1 approved**

- Logical expiry immediately denies access. Physical deletion of expired run
  data and its refs may occur only opportunistically inside an existing
  authorized operation that already touches the run or admits a new run.
- No scheduler, worker, cron job, background cleanup loop or second lifecycle
  authority is introduced. No tombstone is created.
- An expired run that is never touched again may remain physically stored
  indefinitely; no fixed deletion period is promised. Expired and inaccessible
  therefore does not mean physically deleted.
- At the active-run quota, an authorized operation may first remove expired
  runs; if capacity is still unavailable, the new run is rejected with an
  explicit reason. A valid, unexpired run is never evicted.
- No live run, archive revision or dependent Knowledge is evicted to make room.

The three concerns remain separate: expiry enforcement, opportunistic physical
removal, and the absence of any tombstone.

### Immutable archive revision model

An archive is identified at two levels:

| Concept | Identity | Contents | Mutability |
|---|---|---|---|
| Archived source | opaque `source_id`, unique per `(owner_id, peer_id, message_id)` | Telegram provenance only; **no text** | Identity is stable; the row is created once. |
| Archived revision | opaque `revision_id`, `(source_id, revision_no)`, `content_hash` | bounded source-text snapshot, captured-at, owner-approval reference, last observed source state | **Immutable. Never updated, merged or replaced.** |

Rules:

1. **Capture is idempotent.** Capturing content whose hash already exists for
   that source writes nothing and returns the existing revision. An unchanged
   message therefore never creates a duplicate copy.
2. **An edit creates a candidate, not an overwrite.** A different hash produces
   a *candidate* revision; the row is created only after the owner explicitly
   approves it. The previous revision is untouched.
3. **There is no "replace revision" operation.** No code path may update a
   revision's text, hash or revision number. Re-archiving the same content can
   only be a no-op.
4. **References point at a revision.** A run reference, citation, analysis or
   derived knowledge record identifies `revision_id` (equivalently
   `source_id` + `revision_no` + hash). Nothing means "latest".
5. **Revision numbers are monotonic per source** and never reused.
6. **Telegram changes never mutate stored evidence.** A revalidation records
   the observed state (`live`, `edited`, `deleted`, `inaccessible`) and the
   check time only; the archived text and hash stay as captured.
7. **Owner approval is per archived revision.** Suggestions may be generated
   automatically with a short reason; the write requires the owner's explicit
   approval.
8. **No whole-chat and no media-binary archival.** Only bounded text snapshots
   of selected messages.

### Deletion, unavailability and dependent records (APPROVED 2026-11-16)

Manual deletion is owner-triggered. The dependent-record and active-run rules
below are approved for both revision-level and source-level deletion; there is
no hidden or silent cascade.

**The data classes remain distinct:**

| Data class | What it holds | Revision-level delete | Source-level delete |
|---|---|---|---|
| Archived source identity | Opaque `source_id` and Telegram provenance; no text | Unchanged; removing a now-empty source is a separate explicit action | Removed together with every revision |
| Archived revision text | Immutable bounded text, hash, approval reference and observed state | The selected revision is permanently removed; every other revision is untouched | Every revision of the source is permanently removed |
| Active Evidence Run refs | Bounded excerpt copy, hash, `message_id`, optional `revision_id`, validation state; short TTL | Unchanged by default; the excerpt remains until the run's normal TTL expiry and `revision_id` becomes a historical, unresolved archive reference | Same for refs to every revision of the source |
| Derived Workspace Knowledge | Claim/summary/entity referencing `revision_id`; no second text copy | A dependent record: blocks the default delete and is removed only by the explicit cascade confirmation | Same for dependents of every revision of the source |

Rules:

1. **Both operations are explicit owner actions.** Preflight reports the number
   of revisions (source-level), dependent derived records, and matching
   active-run refs/excerpts. It never deletes while reporting.
2. **Dependent derived records block by default.** The preflight covers every
   registered dependent derived record (currently `ai_workspace_knowledge`; any
   future dependent table must join the preflight) and each record's
   `revision_id` reference. If any derived record references a selected revision
   (or any revision of a selected source), the first confirmation refuses and
   lists the dependent count. A separate,
   explicit "delete dependents too" confirmation is required. Only that second
   confirmation may remove the selected revision(s) together with the dependent
   derived records; no derived record is silently orphaned. The same rule
   applies at source level, where the second confirmation lists the revision
   count and every dependent record across those revisions.
3. **No dependents means one explicit confirmation.** It lists all target
   revisions and the active-run excerpt count. Source-level deletion never
   hides that it removes multiple revisions.
4. **Revision-level deletion is not source-level deletion.** It leaves the
   source identity and every other revision intact. If the selected revision is
   the source's last revision, removing the now-empty source identity requires
   a separate explicit source-level confirmation and its dependent rules; it is
   never an automatic cascade.
5. **Active-run refs are not dependent derived records.** They are bounded
   snapshot copies with their own TTL, not references that keep an archived
   revision alive. Archive deletion does **not** cascade to them by default:
   their excerpts, hashes and historical `revision_id` remain until the run
   expires. An unresolved `revision_id` is labelled as an archive revision
   deleted from the archive; the excerpt is never presented as the archive
   record, never revalidated as one, and never used to reconstruct deleted
   archive text. This archive-only default is approved.
6. **"Permanent deletion" is scoped.** It means the selected archive rows are
   permanently removed and, with the explicit cascade, their dependent derived
   records are removed too. It does not mean every copy of the source text
   disappears immediately while active-run excerpts remain. The confirmation
   must say that matching excerpts remain until the configured TTL. No purge is
   approved or specified.
7. **No purge is approved or specified.** A separate owner-requested purge of
   matching excerpts may be considered later, but this design does not define
   one and implementation must not invent it. Until a purge policy is approved,
   archive deletion leaves matching active-run refs and excerpts untouched until
   their own TTL, and the UI must disclose that retention rather than claim that
   every copy disappeared.
8. **No automatic eviction.** Reaching the approved archive quota refuses the
   new operation; it never deletes archived evidence. No cleanup scheduler,
   silent eviction or TTL-based archive deletion is introduced.
9. **Source unavailability** does not delete or rewrite anything. The Explorer
   labels the original message as edited/deleted/inaccessible while still
   showing the stored revision and its hash.
10. **Derived Workspace Knowledge** is created only in a later stage, references
    `revision_id`, and follows the rules above. It never silently outlives a
    deleted revision and is never presented as the source itself.

### Conceptual storage model (approved design; no schema exists)

No table, migration file, SQL statement or Supabase object was created. The
concepts follow the existing `history_schedules` conventions
(`supabase/migrations/20261114180758_create_history_schedules.sql`): additive,
service-role-only, RLS enabled with zero policies, and no owner foreign key
(there is no owner table).

| Concept | Lifetime | Essential contents | Deletion |
|---|---|---|---|
| `ai_evidence_runs` | TTL | server-generated opaque `run_id`, `owner_id`, `peer_id`, mode, status, `created_at`, `expires_at`, message range, optional bounded summary, counters, `version` | Logical expiry blocks all normal use. Approved Option 1 physical deletion is opportunistic (never a scheduler) and is not guaranteed. |
| `ai_evidence_run_refs` | TTL | `run_id`, ordinal, `message_id`, optional `revision_id` (when the ref came from an archived revision), bounded excerpt, hash, captured-at, validation state | `ON DELETE CASCADE` only when/if the parent run is physically deleted; logical expiry blocks reads; no guaranteed physical removal. Archive deletion never cascades to it. |
| `ai_evidence_sources` | Indefinite | `source_id`, `owner_id`, `peer_id`, `message_id`, first-seen time, last observed source state/check time | Explicit owner delete only; follows the approved source-level rule; no active-run cascade. |
| `ai_evidence_revisions` | Indefinite, immutable | `revision_id`, `source_id`, `revision_no`, `content_hash`, bounded text, captured-at, approval reference, observed state | Explicit owner delete only; never time-based; never updated; no active-run cascade. |
| `ai_workspace_glossary` | Persistent (G4-C, after G3-B) | real Workspace identity, normalized term, term, definition, `version`, timestamps | Owner-confirmed delete; no placeholder scope exists before G3-B. |
| `ai_workspace_knowledge` (later) | Derived | `revision_id`, claim/summary/entity, analysis provenance, revision | A dependent record: blocks default deletion and is removed only by the explicit cascade workflow; never orphaned. |

Storage rules:

- **Owner and Workspace isolation first.** Every read/write is filtered by
  `owner_id`; a Workspace reference must be a real G3-B identity validated for
  that owner. An opaque id alone is never authorization.
- **Runs are logically temporary; physical cleanup is not automatic.** Expiry
  is evaluated on read/write against authoritative database time, so no
  background scheduler is needed. Logical expiry makes the run unusable and
  cannot be reversed by a later chat read. Approved Option 1 physical deletion
  is opportunistic and a never-read run may remain stored indefinitely.
  Expired does not mean deleted.
- **Archive text is stored once per revision.** Identical content is deduped by
  `(source_id, content_hash)`; an edit appends a revision instead of replacing
  text; derived records reference a revision id instead of copying text.
- **No media binaries, no full chats, no sender names.** Only bounded text,
  media presence flags and local attribution.
- **Revalidation is local.** The source message is re-read through the existing
  Telegram boundary; edits/deletes are labelled from the captured revision and
  hash, never silently refreshed.

### Workspace dependency (no substitute)

- The repository has **no** Workspace table, model, service or handler. A
  documentation search also finds no prior Workspace schema.
- The approved persistent Glossary is real, but its ownership/authorization
  must come from the actual Workspace model. **G3-B** is the stage that
  establishes durable Workspace settings/identity.
- G4-A and G4-C must not create an owner-level workspace key, a temporary
  Workspace table, a header/environment scope, or any parallel abstraction that
  would later have to be migrated.
- Therefore: **Glossary implementation is blocked until G3-B is complete and
  its ownership/authorization model is approved.** Until then the Glossary
  remains a documented idea only; no persistence, UI or injection exists.
- Workspace Knowledge keeps its intended scope: derived, structured,
  persistent knowledge linked to source evidence, scoped by the real Workspace
  model. This correction does not widen or narrow it.

### Bounds and quotas — APPROVED 2026-11-16

The owner approved these values. They are application planning limits, not
exact PostgreSQL billing or physical-storage measurements. They must come from
the approved policy/configuration and must not be hard-coded. **The
configuration surface remains an open decision**; glossary quotas are deferred
until G3-B and no glossary number is approved.

| Parameter | Approved value |
|---|---|
| Maximum stored excerpt/snapshot per revision | 1,000 characters |
| Maximum refs per run | 50 |
| Maximum refs per follow-up | 20 |
| Maximum stored normalized text per run | 32 KiB UTF-8 |
| Maximum active runs per owner | 20 |
| Active-run TTL | 7 days default; owner-selectable 1–30 days, persisted per run |
| Archived records per owner | 5,000 |
| Archived data per owner | 50 MiB |
| Archive limit precedence | whichever of 5,000 records or 50 MiB is reached first |
| Duplicate strategy | `(source_id, content_hash)` unique; same hash = no-op |
| Glossary quotas | deferred until the real G3-B Workspace; no value approved |

### Quota-reached behavior (approved)

- **New active run:** opportunistically delete expired runs inside this existing
  admission path; no scheduler is added. If capacity is still unavailable,
  refuse with an honest message stating the approved cap and current usage, and
  let the owner inspect/close runs. A live run is never expired to make space.
- **New archival revision:** refuse with the usage and the approved limit
  (5,000 records or 50 MiB, whichever is reached first). Offer only explicit
  owner choices (delete a selected archived revision, or raise the limit through
  an explicit approval workflow). Never evict or overwrite archived evidence.
- **Glossary:** quotas are deferred until G3-B; no add/edit limit is enforced or
  invented before the real Workspace exists.
- Every refusal reports the actual configured value; the system never invents a
  limit at runtime.

### Storage methodology: measured vs estimated vs assumed

**Measured facts (code/schema, not storage measurements):**

- The F1 migrations define four Evidence tables and their triggers/functions;
  they are applied to the live schema. No Glossary table exists.
- `history_schedules.summary` is bounded at 32 KiB of UTF-8.
- `ai_messages` truncates content at 8,000 characters.
- Existing tables are accessed only through the service-role key with RLS
  enabled and no permissive policies for the private tables.

**Assumptions (not measured):**

- Average excerpt length and how often an edited message is re-archived.
- UTF-8 bytes per character by script (≈1 for Latin, ≈2 for Persian/Arabic,
  ≈3 for CJK; emoji up to 4).
- One or two secondary indexes per table; no unexpected third-party indexes.
- No unusual TOAST compression behavior for this text.

**Estimated range, with PostgreSQL overhead explained:** actual storage is not
the text length alone. It includes the tuple header and null bitmap, alignment
padding, 8 KiB pages with fillfactor/free space, TOAST when a text value is
large (compression and/or out-of-line storage), B-tree index pages and their
own overhead, one index per unique/foreign-key constraint where defined, row
versions/bloat before vacuum, and the revision multiplier for edited sources.
WAL is not part of the table's reported size. A transparent planning example:
400 characters × 2 bytes ≈ 800 B of text; add roughly 0.3–0.8 KB of tuple and
index overhead ⇒ **≈1.1–1.6 KB per revision as an estimate, not a
measurement**. At 1,000 characters (≈2–3 KB of text) ⇒ **≈2.5–4 KB per
revision**. These ranges exclude TOAST/out-of-line edge cases.

**Illustrative scenarios (estimates, not measurements; revision multiplier
included):**

| Scenario | Archived messages/month | Estimated monthly growth | Estimated yearly growth |
|---|---|---|---|
| Small personal | 20 | ≈25–60 KB | ≈0.3–0.7 MB |
| Moderate regular | 200 | ≈0.25–0.6 MB | ≈3–7 MB |
| Heavy collection | 2,000 | ≈2.5–6 MB | ≈30–72 MB |

A second revision for an edited message adds one more revision row; the
estimate assumes a low edit rate and should be re-measured after real use. No
Supabase billing figure, plan limit, or cost claim is made.

### Privacy, authorization and provider isolation (revalidated against `416918e`)

- `backend/ai/tools/executor.py:380` records every execution, and
  `:501`/`:504` persist `arguments` and `result_data=result.data` into
  `ToolHistoryRecord`.
- `backend/ai/engine/dispatcher.py:2341` places the full `ToolResult.data` into
  the tool message of the provider continuation.
- `backend/ai/engine/dispatcher.py:975`, `:1238` and `:1740` send the tool
  result message into conversation history, which
  `backend/ai/runtime/manager.py` persists via `persistence.add_message` into
  `ai_messages` (`backend/ai/persistence.py:167-171`).
- `backend/ai/persistence.py:359-366` writes `arguments` and
  `result_message[:2000]` into `ai_tool_history`; `dispatcher.py:989`/`:1266`/
  `:1303`/`:1736` expose `tool_results` through `EngineResult.metadata`, and
  `backend/ai/prompt/builder.py:355` renders tool history into the prompt.
- `backend/services/history_ai_service.py:646-686` calls the provider with
  `tools=[]` and no `response_format`.

Consequences: the evidence envelope (source/revision ids, excerpts, hashes,
links) must never be placed in `ToolResult.data`, `message`, `arguments`,
metadata or conversation history. An evidence tool returns only an opaque
`run_id`/`revision_id` plus bounded counts/coverage; the owner-facing text is
rendered by dedicated code from the durable record. Links are constructed
locally from a validated record and are never taken from provider output. A
`revision_id` supplied by a provider or callback is re-read owner-scoped and
validated against the run/source before use; an opaque id alone is never
authorization.

### Revised stage plan (corrected)

| Stage | Scope | Depends on | Gate |
|---|---|---|---|
| **G4-A / F1** | Single-source evidence foundation: durable Active Runs with approved TTL, immutable owner-approved archive revisions, opt-in Cited Summary, basic Explorer with locally built links. | G2-A + existing History | **Code implemented/remediated; database migrations applied and verified 2026-11-17; application delivery defects fixed 2026-11-18.** Runtime end-to-end checks remain; M5 is deferred until dependent records exist. |
| **G4-A / F2** | Restart-safe follow-ups on the same run and contract. A delivery phase of G4-A, **not** a new stage. | G4-A / F1 | The approved TTL proves usable in practice. |
| **G4-B** *(original meaning preserved)* | **Workspace-aware durable Evidence:** Workspace-scoped browsing/consumption of the same evidence contract, cross-run within the Workspace. No second evidence format. | G4-A + G3-A + G3-B | Owner approves Workspace persistence and evidence retention scope. |
| **G4-C** *(new ID, documented)* | Strict-JSON extraction and the persistent Workspace Glossary. Glossary storage **blocked until G3-B**. | G4-A + G3-B + approved extraction schema | G3-B complete; extraction schema approved; glossary quotas defined after G3-B. |
| **G4-D** *(new ID, documented)* | Descriptive, then threshold-gated subjective assessments. | G4-C + approved thresholds/person policy | Policy approved; no invented numbers. |

**Why the identifiers changed this way:** G4-B is an existing roadmap identity
("Workspace-aware durable Evidence") and is restored, not reused. Restart-safe
follow-ups are therefore G4-A/F2 rather than a stage. Extraction, the
persistent Glossary and subjective assessments were bundled inside the original
G4-A definition; with durable storage and a real Workspace dependency they no
longer fit one deliverable, so they become G4-C and G4-D. Both are new IDs with
a documented reason, and neither renames or replaces G4-B.

Scheduled History stays outside all stages: body-only and citation-free, with
no evidence run, citation or archive path attached to it.

### Closed F1 product decisions

The 2026-11-16 approval closed the active-run TTL, all active limits, archive
quotas, deletion/active-run retention, per-edit approval and the expired-run
physical policy (Option 1). The F1 implementation also applies the following
closed decisions:

- one centralized owner-level policy, with no per-Workspace configuration
  before G3-B;
- Cited Summary is opt-in from both the History panel and an explicit AI tool,
  chosen per request and never the default;
- archive suggestions come only from evidence reviewed in the current run,
  max 10, each with a reason and owner confirmation;
- no Workspace Glossary, subjective assessment, extraction or cross-source
  intelligence in F1.

### Remaining owner decisions after F1

1. **Glossary quotas:** entry/character limits, only after G3-B defines the real
   Workspace. No glossary number is approved.
2. **Subjective-assessment thresholds** and the person-level judgment policy for
   G4-D.
3. **Tagged-text fallback:** whether to specify one for future non-F1 structured
   operations; F1 has no fallback and fails closed.
4. **Global versus per-Workspace limit overrides** after G3-B. Global policy is
   the F1 default.

**No longer open:** active-run TTL (7-day default, 1–30 days), active limits
(1,000-character excerpt, 50 refs per run, 20 per follow-up, 32 KiB text per
run, 20 runs per owner), archive quotas (5,000 records or 50 MiB per owner,
whichever first), dependent-record blocking with a separate explicit cascade,
active-run excerpt retention until TTL, immutable revisions and per-edit owner
approval, and Option 1 opportunistic physical deletion with no scheduler,
tombstone or fixed deadline. A separate purge remains unspecified and is not
approved.

## Previous delivery: G1-A — Reusable interaction grammar

Status: **Implemented offline for Stash and Taskloom. This is a UI interaction
delivery only; G1-B, G2-A, G5-A beyond this narrow Taskloom work, Workspace and
Scheduled History Phase 6/7 remain deferred.** No schema, migration, Supabase,
Telegram, Render or live-operation change was made.

### What G1-A adds

- **Panel ordering rule:** documented in `AGENTS.md` — primary action, grouped
  controls, item-specific controls, contextual bulk actions, then navigation
  and pagination. The existing `InlinePanelBuilder`, registry, callback
  namespaces, symbols/styles and Appearance boundary are unchanged.
- **Reusable selection:** `backend/helper/selection_state.py` now keys sessions
  by owner + feature scope, preserving the Stash default API, 40-item cap,
  900-second expiry, exact identity and visible mark behavior. Stash remains on
  its existing scope; Taskloom gets its own scope.
- **Taskloom multi-select:** a select mode over the existing task list, exact
  `task_id:version` row callbacks, pagination survival, explicit clear, and
  contextual Pause / Resume / Complete / Delete actions only for applicable
  selected tasks.
- **CAS-revalidated bulk work:** every selected task is re-read through
  `TaskManagementService`; missing, foreign-owner, changed-version and
  status-inapplicable targets are refused individually and reported per item.
- **Destructive confirmation:** Taskloom bulk Delete has a dedicated review
  showing the exact count/targets/consequence, a cancel action, DANGER styling
  on the final confirm, and revalidation at confirmation time. Stash's bulk
  delete confirm uses the same DANGER style.
- **No new scheduler, executor, task repository, selection store, renderer,
  Telegram client, listener or persistence path.**

### Validation and limits

- Focused offline tests cover Stash compatibility, Taskloom selection, scope
  isolation, cap/expiry, pagination, stale/missing/foreign targets, partial
  bulk results, confirmation cancel and destructive confirm styling.
- No live Telegram callback/render, Supabase, Render or live task execution was
  tested.
- G1-B visual identity/accessibility, G2-A language, G5-A Task Center beyond
  selection/bulk, Workspace features and Scheduled History Phase 6/7 are not
  part of this delivery.

## Previous delivery: Scheduled History diagnostics tracing — Phase 7 prerequisite

Status: **An observability-only, occurrence-scoped diagnostic trace layer is
implemented for the existing Scheduled History lifecycle. It changes no
execution, scheduler, cursor, CAS, retrieval, provider, creation or
compensation semantics. Phase 4's owner wizard/task+state creation and the
prerequisite registered Phase 5 action remain implemented and focused-tested.
The Phase 3B migration remains APPLIED to the connected Supabase project
(remote version `20261114180758`). Live Telegram execution, restart/catch-up
validation and provider-failure validation remain unverified; Phase 6/7 stay
deferred.**
The owner chose to move the registered action and trusted task-id plumbing ahead
of Phase 4 as a prerequisite; no phase was renumbered. Phase 3B audited commit:
`a12c20779bc634cb4511fb0e79443b882e8a1a85` (implementation `417a6b8`, upstream
parent `e2f1619`). Phase 3A base: `62a18de`.

### Phase map

| Phase | Scope | Status |
|---|---|---|
| **3A** | Durable History state investigation and design | **Complete** |
| **3B** | Durable schema/repository, oldest-first retrieval, identifier-free payload boundary | **Implemented and audited** — migration applied (remote `20261114180758`) |
| **4** | Deterministic schedule UI | **Implemented** — owner wizard, trusted creation path and compensating deletion |
| **5** | Scheduled History action | **Prerequisite implemented** — registered action, trusted task id, bounded catch-up, CAS commit; live validation deferred |
| **6** | Shared History admission | Deferred; numbering unchanged |
| **7** | Live validation | Deferred; numbering unchanged — occurrence tracing prerequisite delivered |

Phase 3 remains the umbrella durable-state phase. The owner changed only the
delivery order: the Phase 5 action/trusted task-id plumbing was required before
Phase 4 creation, then Phase 4 completed. No phase was renumbered.

### What this delivery adds

- **Registered action:** `scheduled_history` is one `ToolExecutor` action in the
  existing registry. It uses the coordinator-injected trusted task id from
  `ToolContext.extra`; peer, baseline, cursors and prior summary come only from
  the durable `history_schedules` row. It accepts no peer/task/message id from
  arguments and never creates or mutates a schedule.
- **Owner-only creation gate:** `TaskCreationService.create()` rejects
  `scheduled_history` unless the trusted owner-wizard action set is explicitly
  passed. The model/natural-language candidate path cannot create the action.
- **Phase 4 creation:** the History panel opens a deterministic wizard for the
  selected, re-classified peer. The owner chooses hourly/daily/weekly cadence,
  new-only vs explicit backlog baseline, and optional focus. Creation uses the
  existing `TaskCandidate → TaskCreationService → TaskRepository` path, writes
  exactly one task and one peer state row, refuses a live duplicate, refuses
  before any Telegram work when the manager reports durable storage unavailable,
  and compensates by deleting the exact created task when state creation fails.
- **Bounded execution:** one stable upper boundary per run, oldest-first batches
  of at most the peer policy cap, cumulative summary through the existing
  identifier-free History AI boundary, and one CAS commit for summary plus both
  cursors. Media/AI-provenance-only batches may advance the scanned cursor but
  never replace the summary; failures record bounded metadata without moving the
  summary or cursors.
- **No new scheduler, executor, repository, Telegram client, listener, update
  loop or persistence path.** The existing scheduler/coordinator/executor and
  durable-only repository remain the only execution and state paths.

### Scheduled History diagnostics tracing (Phase 7 prerequisite)

- `AI_TASK_TRACE` remains the existing `create_task` request trace and is not
  rebound for scheduled work. `backend/ai/task_trace.py` adds independent
  occurrence and History-creation context variables, scopes and bounded
  structured emitters.
- `SCHEDULED_OCCURRENCE_TRACE` carries one stable `trace_id` per durable
  occurrence (the Scheduler occurrence key, `<task_id>:<scheduled_for UTC ISO>`),
  the task id and a separate `attempt`; `HISTORY_SCHEDULE_TRACE` covers owner
  creation and compensation with a short-lived random creation id.
- Covered stages: owner creation, duplicate/durability checks, separate task
  and state writes, compensation; scheduler recovery, due discovery,
  occurrence creation/idempotent existence, claim, handoff, retry/terminal
  result and notification; coordinator validation, provenance injection,
  action-chain planning, each action start/result, timeout/cancellation and
  terminal outcome; tool provenance/argument validation and service invocation;
  durable state lookup/validation, peer policy, stable upper bound, retrieval,
  safe skip, AI/provider, output validation, CAS commit and failure metadata;
  narrow repository read/CAS outcomes and occurrence-visible memory-fallback
  outcomes.
- Trace output carries only bounded operational metadata. It never logs message
  bodies, summaries, owner instructions, prompts, provider bodies, credentials
  or peer identifiers; raw exception text is not used. Provider metadata values
  are allowlisted, with unknown origins/reasons normalized to `unknown`.
  Ordinary Telegram/AI requests emit no scheduled trace because the occurrence
  scope is absent.
- Twenty focused test functions (22 parameterized cases) cover ordered
  success, cross-stage correlation, retry attempt separation, concurrent
  isolation, reset after success/exception/cancellation, ordinary-request
  silence, provenance rejection, storage/state, peer-policy, retrieval,
  AI/provider, safe-skip/summary, output, CAS/durable-error, compensation,
  repository memory-fallback success/failure, provider metadata allowlisting
  and content-privacy distinctions.
- No schema, migration, scheduler, worker, execution path, cursor/CAS rule,
  provider policy or retrieval limit was changed.

### What Phase 3B actually delivers (source-verified)

- One additive migration, `supabase/migrations/20261114180758_create_history_schedules.sql`
  (part 18 of 18; inventory 42 files), applied to the connected project with
  remote version `20261114180758`:
  `task_id → ai_tasks(id) ON DELETE CASCADE`, `UNIQUE (owner_id, peer_id)`,
  immutable `baseline_cursor`, monotonic `scanned_cursor`/`summarized_cursor`
  with `baseline <= summarized <= scanned`, a 32 KiB UTF-8-bounded `summary`,
  `revision` + CAS `version`, bounded error metadata, status CHECK, one task-id
  index, RLS enabled with zero policies and `SELECT/INSERT/UPDATE/DELETE` granted
  only to `service_role`.
- Six repository primitives on the existing `TaskRepository` hierarchy:
  `create_history_schedule`, `get_history_schedule`,
  `get_history_schedule_by_peer`, `commit_history_success`,
  `record_history_failure`, `reset_history_summary`.
- **Durable-only production semantics.** Every History read/write is a single
  owner-scoped (and, for writes, CAS-versioned) durable operation. When the store
  is unavailable, times out, or the table is absent, the operation raises
  `HistoryStorageUnavailable` and writes nothing. Process memory is never
  substituted; `InMemoryTaskRepository` is an explicitly injected test fake only.
- Bounded oldest-first retrieval: optional `reverse` on
  `messages.iter_messages`/`TelegramAPI.iter_messages` (default unchanged), plus
  `fetch_oldest_history`/`HistoryBatch` with a stable upper bound, exclusive
  lower cursor, defensive sort/dedup and conservative `has_more`.
- `scheduled_summary_payload` — the identifier-free AI boundary (message bodies
  plus the owner's instruction only). No production caller in Phase 3B.
- No scheduler, executor, lifecycle hook, UI, parser, notification, catch-up
  policy, second Telegram client or second update loop.

### Independent audit corrections (this delivery)

1. **Durable-only violation fixed.** The six Supabase History methods caught
   every exception and returned memory-fallback records, so an outage or an
   unapplied migration could report a successful checkpoint that no restart
   could see. They now fail closed (`HistoryStorageUnavailable`); creation also
   refuses when the owning task could only be read from memory. Six regression
   tests cover reads, writes, CAS writes, creation and the structural guard that
   no History method references the fallback.
2. **Merge residue removed.** The merge left three files from the pre-upstream
   Appearance implementation (`backend/services/image_probe.py`,
   `backend/services/preview_storage.py`, `sql/panel_settings_appearance.sql`);
   upstream never had them and nothing references them. They were deleted.
3. **Documentation reconciled.** `ROADMAP.md` (this file), `DATABASE_ARCHITECTURE.md`
   (§16.1, §22, §30 inventory row) and `IMPLEMENTATION_REPORT.md` no longer claim
   an in-memory History fallback. `INVESTIGATION.md` records the audit findings.
4. **Scheduler failure classified.** `test_recovered_interrupted_occurrence_is_picked_up_by_run_once`
   fails identically at `a12c207`, `417a6b8` and `e2f1619`; the scheduler code
   and test are untouched by Phase 3B — demonstrably pre-existing.

### Completed

- Phase 3A source-verified design and approved ownership/lifecycle contract.
- Phase 3B migration, repository, retrieval, payload boundary and focused tests.
- Phase 5 prerequisite: trusted task-id injection, registered
  `scheduled_history` action, bounded incremental checkpoint service, stable
  upper boundary and CAS commit.
- Phase 4: owner-only schedule wizard, deterministic cadences/baseline, task
  and state creation through the existing service, duplicate refusal and
  compensating deletion.
- Focused tests cover owner gating, creation/compensation, cursor/CAS behavior,
  safe-skip, provider failure metadata, trusted task-id injection and the UI
  creation path.
- Independent audit of Phase 3B: repository/CAS/cursor review, durable-only
  enforcement, merge-integrity comparison against both parents,
  migration/§31.3 reconciliation.
- Phase 7 observability prerequisite: occurrence-scoped end-to-end tracing for
  creation/compensation, scheduler/occurrence lifecycle, coordinator actions,
  the History tool/service, AI/provider boundary and repository CAS outcomes;
  22 focused trace cases pass, including correlation, retry, concurrency,
  cleanup, failure distinctions and content privacy.
- `IMPLEMENTATION_REPORT.md` and `INVESTIGATION.md` record the audited state.

### Applied migration and verification (2026-11-14)

- The migration is **applied** to the connected Supabase project. Its live
  migration-history record is version `20261114180758`, name
  `create_history_schedules`; the repository filename was reconciled from
  `20261113000001` to match that recorded version. The SQL is byte-identical.
- Owner-performed live checks passed for the table structure, constraints, RLS
  configuration, grants and sequence privileges.
- A transactional CAS probe ran against the live table and was **rolled back**;
  it left no test row behind.
- No coding agent executed SQL, applied a migration, repaired the migration
  history or modified any Supabase object.

### Not verified / blockers

- **Live scheduled execution is not verified**: no real scheduled occurrence has
  run against a live Telegram chat; the action and UI are covered by focused
  tests with injected retrieval/provider doubles.
- **No live restart/catch-up run, deleted-message gap run, provider-failure run
  or capped-peer refusal was exercised.** These remain Phase 7 work. The new
  tracing was verified only with offline tests and test doubles; its first
  production sequence is still to be observed.
- **Task and state are two durable writes**: state failure is compensated by
  deleting the exact created task, but this is not a single cross-table
  transaction. A crash between the two writes can leave an owner-created task
  without its state; the action then fails closed and reports the missing state.
- The pre-existing scheduler recovery test failure recorded below remains
  unrelated and untouched.

### Deferred evidence-backed constraints (unchanged)

- 1,000-message single-request summaries remain infeasible at 240–300 s with
  concurrency 2 and 4,000-token chunks (~352 Latin / ~176 non-Latin messages).
- No verified chain-wide context floor, so the chunk budget must not grow.
- `ASSUMED_CALL_LATENCY_S = 30` is a planning value; measure
  `ProviderManager.metrics_snapshot` before changing pacing or concurrency.
- `ai_task_occurrences.result_metadata` stays out of summary storage (8 KiB
  bound, occurrence lifecycle ownership).

---

## Appearance (merged from the upstream line, preserved)

The upstream Appearance delivery in the merge is preserved byte-for-byte:
`backend/bot/handlers/appearance.py`, `backend/services/appearance_service.py`
and its three suites (`test_appearance_delivery.py`,
`test_appearance_gallery.py`, `test_appearance_image_cache.py`). Its
`tests/test_appearance.py` was removed by the upstream line itself and replaced
by those suites; the audit confirmed no Appearance coverage was lost.

Implemented: one Appearance category, shared header overrides, per-main-page
title/body overrides, and a preview image with an independent switch, delivered
as the panel's own photo with a versioned `/appearance/image/<version>` rendition,
backed by a durable Storage object key and a ten-image bounded gallery.

Still required (Stage 2): live Telegram verification of small-preview rendering,
tap-through and keyboard preservation on current clients; optional strict-write
hardening for the remaining panel settings.

Explicitly out of scope: a generated default image, web-dashboard upload,
multiple selected images, per-page preview images, scheduled rotation, or any
second renderer/settings table/listener/client/update loop.

---

## Later phases (deferred, in order)

1. **Phase 6 — shared History admission**: one process-wide in-flight/start gate
   inside `history_ai_service`, because per-operation pacing does not coordinate
   bounded concurrent occurrences.
2. **Phase 7 — live validation** of a real schedule, a restart mid-run, a deletion
   of a previously processed source message, a provider failure, and a capped-peer
   refusal.

Completed sequencing note: Phase 5's action and trusted task-id plumbing were
moved ahead as prerequisites, then Phase 4's owner wizard and task/state creation
were completed. No phase was renumbered.

---

## Previous delivery (complete, unchanged): History Peer Policy Phase 2

Base `ffe72fd`: deterministic `history_policy` over the Phase 1 `PeerType`,
per-peer caps (bot/channel 300, user/group/supergroup 1000), dynamic panel count
choices, stale-callback rejection, service-boundary and linked-message enforcement.
78 new policy tests. See the commit history and the report history.

---

## Previous delivery (complete, unchanged): Peer Classification Phase 1

Base `ecc3779`: `PeerType` + pure `classify_entity` in
`backend/telegram_api/peers.py`, migrated into the History picker label,
General → Chat & Message IDs, Ghost Seen eligibility and Anti-Edit bot detection.
Strict entity-flag precedence, fail-closed `unknown`, no persistence or cache.
41 tests.

---

## Previous delivery (complete, unchanged): History AI-pass capacity

Base `7894f31`: script-aware token estimation, concurrency-aware map capacity,
map + reduce call accounting, and an honest refusal naming the AI time budget and
the affordable message count. Chunk budget, concurrency, pacing, deadlines and
ProviderManager ownership were unchanged.

---

## Future roadmap: General AI Execution Orchestrator

**Status: APPROVED DIRECTION / DESIGN REQUIRED / NOT IMPLEMENTED.**

This item is recorded, not started. No checkpoint store, orchestrator module,
multi-model planner or new execution path exists in the repository. Nothing in
this section may be cited as implemented.

### Required order

1. Complete Scheduled History summarization reliability.
2. Verify that fix (offline tests first, then live provider/Render evidence).
3. Design the general AI Execution Orchestrator.
4. Implement its checkpoint/recovery foundation.
5. Add bounded multi-model orchestration and synthesis.
6. Expand adoption incrementally across the Self-Bot's AI workflows.

Step 1 is implemented and offline-tested as of 2026-11-17 (see
`IMPLEMENTATION_REPORT.md`); its live verification is still pending, so step 2
is not complete. Steps 3–6 are not started.

### Problem being solved

A multi-step AI operation today loses all progress when the provider that
served it fails, rate-limits or disappears: the caller either resends the whole
input or gives up. Provider session ids and opaque continuation tokens are not
portable, so they cannot make work resumable across providers. Complex requests
are also sent to a single model even when decomposing them would be cheaper,
more reliable and easier to validate. The Self-Bot needs one application-owned
way to checkpoint progress, validate outputs, resume only what failed and
combine several bounded model results honestly.

### Intended behaviour

- A multi-stage AI operation records structured progress after every validated
  stage and can resume from its last valid checkpoint.
- Any eligible provider can continue the work: the checkpoint, not the model
  session, carries the state.
- Independent subtasks may run concurrently when their inputs are complete;
  dependent subtasks wait for their prerequisites.
- Results are validated deterministically before they are checkpointed or used.
- A final synthesis stage merges validated outputs; partial success and partial
  failure are reported as such, never as full success.
- When no eligible provider remains, the operation fails honestly with bounded
  diagnostics — no fabricated result, no silent truncation.

### Checkpoint semantics

A checkpoint is **application-owned, bounded, structured, validated and
recoverable**. It stores:

- the operation and stage identity, plus a schema version;
- completed and pending stages, with the exact inputs each pending stage needs;
- validated outputs of completed stages, with their provenance and validation
  status;
- concise, bounded summaries of prior context where a stage needs them;
- token/time budgets consumed so far and the remaining budget.

It must NOT store hidden chain-of-thought, provider session identifiers or
opaque continuation state. Passing a previous model's answer to another model is
**not**, by itself, a durable checkpoint; only the persisted, validated,
application-owned record is. Retried operations must be idempotent against the
checkpoint (a stage already completed and validated is never re-run merely
because its response was lost).

### Multi-model decomposition and synthesis

- Decompose only suitable complex requests into bounded subtasks with explicit
  contracts and dependencies.
- Choose an eligible model per subtask from ProviderManager's current candidates
  using capability fit, availability, quality and cost — never a hard-coded
  provider choice in the orchestrator.
- Run independent subtasks concurrently within explicit concurrency and budget
  limits.
- Validate each subtask deterministically before checkpointing it.
- Synthesize validated outputs in a final bounded stage and represent partial
  results honestly.
- Recover only the failed or invalid stages when it is safe to do so.

### Cross-Self-Bot scope

This is a general capability for AI-driven workflows across the Self-Bot, not a
Taskloom-only feature. Candidate adopters include Scheduled History
summarization, Taskloom step execution, Media Analysis, Evidence workflows and
future multi-step tools. Each adopter keeps its own business semantics and result
contracts; the orchestrator supplies checkpointing, stage execution, validation
and synthesis only.

### Architecture boundaries (non-negotiable)

- `ProviderManager` remains the sole authority for provider/model routing,
  fallback, retry, cooldown and health. The orchestrator never calls a provider
  directly and never implements a parallel router.
- The Dispatcher remains the existing AI orchestration entry point.
- `ToolRegistry` remains the capability allowlist.
- `ToolExecutor` remains the sole authority for executing registered AI tools.
- Existing services remain responsible for their business operations;
  deterministic operations are not sent to AI unnecessarily.
- AI may reason and propose; application code validates and executes.
- No arbitrary Telegram RPC, SQL, shell, filesystem or unrestricted HTTP
  execution.
- No second RuntimeSupervisor, scheduler, executor, Telegram client, listener or
  update loop.

### Cost, concurrency, idempotency, validation and privacy

- Per-operation token, time and model-call budgets; bounded concurrency and
  bounded stage counts.
- Idempotent stage execution keyed by operation + stage + input digest.
- Deterministic validation and explicit result contracts per stage.
- Provenance retained for every validated stage output.
- Progress and checkpoint writes owner-scoped and durable; no cross-owner reads.
- The AI payload boundary stays identifier-free where it already is (no peer
  ids, message ids, task ids or other Telegram context unless an approved
  contract explicitly permits it).
- No prompts, message bodies, summaries, tokens or credentials in traces or
  diagnostics; bounded scalar metadata only.

### Dependencies on the existing architecture

- Durable, owner-scoped storage and the existing repository pattern (no schema
  change is approved by this item; a future design must justify any storage it
  needs).
- ProviderManager candidate/health APIs and its honest exhaustion response.
- Dispatcher, ToolRegistry and ToolExecutor boundaries as they exist today.
- The existing occurrence/task trace contract for bounded diagnostics.
- Scheduled History reliability (step 1) as the first verified consumer.

### Acceptance criteria (for the future design, not this delivery)

1. A multi-stage operation resumes from its last validated checkpoint after a
   provider failure, on a different provider, without resending the whole input.
2. Completed and validated stages are never re-executed by a retry.
3. A failed stage is recoverable without discarding validated earlier stages.
4. Partial success is reported distinctly from full success and from failure.
5. Budgets, concurrency and model-call counts are enforced, not advisory.
6. Every stage output is validated deterministically before use.
7. ProviderManager remains the only routing/fallback/retry/health authority;
   grep-level proof that no second provider path exists.
8. No new scheduler, executor, Telegram client, listener or update loop exists.
9. Privacy tests prove no forbidden identifier or private content reaches the
   provider or the trace.
10. Exhaustion produces an honest failure with bounded diagnostics.

### Risks and open design questions

- **Checkpoint growth:** structured state can exceed bounds; needs a size
  contract, compaction and a refusal path.
- **Validation strength:** deterministic validation is only as good as each
  stage's contract; weak contracts invite silent corruption.
- **Cost of decomposition:** planning and synthesis add model calls; measured
  benefit must justify them.
- **Concurrency vs rate limits:** parallel subtasks can amplify 429s; ProviderManager
  cooldowns must bound the fan-out.
- **Storage:** where checkpoints live (existing durable store vs a new table) is
  undecided and requires an approved design and migration.
- **Semantics of "resume":** which stages are safe to replay, and how a
  non-idempotent business action is fenced, remains open.
- **Provenance:** how much per-stage provenance is retained without unbounded
  growth.
- **Adoption:** which workflow migrates first and what evidence proves the
  migration is an improvement.

---

## Open items carried over (untouched by this delivery)

- `tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`
  (“interval advancement exceeded bound”) — the only full-suite failure at
  `a12c207`, reproduced identically at both parents; not caused by Phase 3B.
- Live scheduled History execution and live Telegram behavior remain unverified
  (the migration itself is applied).
