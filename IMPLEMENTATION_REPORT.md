# IMPLEMENTATION REPORT — CURRENT STATE

> **This is a CURRENT-STATE document.** It describes the repository as it exists
> at the tip of this phase. Prior phases are summarized in §17 (history) — the
> deep per-phase audits they produced are superseded by this document and by
> `INVESTIGATION.md`. If code changes invalidate any section, update this
> document in the same commit.

---

## 1. Implementation metadata

| Item | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Branch | `main` |
| Base commit (this phase) | `830eba4` (clean tree, `origin/main` equal) |
| Phase | Complete + harden the production-grade AI-generated recurring content task system (semantic interpretation → durable task → prepare-ahead → validated execution → Telegram), incl. the global Bio guardian and source-fidelity contract |
| Implementation commit | `ac3fe40` (`test: pin flood-wait guardian semantics and honest rejection classification`) |
| Report commit | see §16 delivery record |
| Status | **COMPLETE — full suite green (1925 passed, 24 skipped). LIVE Telegram/provider/Supabase verification NOT performed in this workspace** (no session credentials); behavior verified in-process through the real deterministic layers with scripted providers |
| Database impact | **NO DATABASE / SCHEMA CHANGE** (see §14 for one documented, NOT-applied optional migration) |

---

## 2. Architecture (preserved authority chain)

```
Natural language
→ AI semantic interpretation (TaskInterpreter → TaskCandidate → parse_candidate_output)
→ deterministic validation (TaskCandidate.from_untrusted + parse_schedule)
→ TaskCreationService → durable ai_task definition (ai_tasks table)
→ TaskScheduler → occurrence (ai_task_occurrences table, CAS claim)
→ AIActionPreparator → provider → candidate → deterministic policy validation
→ PreparedAction (occurrence-specific, version-stamped, persisted in preparation_metadata)
→ TaskExecutionCoordinator (re-proves prepared action at the boundary)
→ ToolExecutor (SOLE execution authority)
→ registered tool (bio_set_text / send_message / …)
→ existing service (bio_service → bio_guardian → Telegram UpdateProfileRequest)
```

**Authority invariants (enforced and tested):**

- The external AI is a reasoning/generation component only. It never receives
  Telegram RPC, SQL, shell, filesystem, HTTP, or unrestricted execution
  authority. Provider output is arguments-only; the ToolRegistry/ToolExecutor
  boundary decides what runs.
- `ToolExecutor` is the sole execution authority for registered tools. There is
  exactly one scheduler (`TaskScheduler`), one coordinator
  (`TaskExecutionCoordinator`), one executor, one provider path.
- Preparation (AI generation, validation, persistence) is strictly side-effect
  free: it never calls the ToolExecutor, never calls Telegram, never opens the
  Bio guardian window.
- `RuntimeSupervisor` remains the single recovery authority.

---

## 3. Semantic task interpretation (creation)

`backend/ai/task_interpreter.py` — provider-based semantic interpretation, no
regex parsing of user phrasing. The interpreter prompt defines:

- **Registered action contract**: exactly `send_message`, `bio_set_text`,
  `username_set_text` (profile tools with EMPTY `{"text": ""}` arguments —
  content is AI-generated per occurrence, never baked at creation).
- **AI-GENERATED CONTENT CONTRACT**: for per-run varied content the candidate
  must carry `ai_instruction` = the user's request **VERBATIM** (never
  paraphrased/translated/shortened), so source/character/language/length
  semantics survive word-for-word. Static one-time content omits it.
- **Semantic INTERVAL RECOGNITION**: digits in any script or Persian/English
  number words, minute/hour/day/week/second units, once-per-interval markers
  (یک بار/یه بار/یکبار/once every), multi-line requests — a clear interval is
  never rejected as ambiguous; only genuinely schedule-less requests return
  null.
- **EXPLICIT DESTINATION**: chat names only, never numeric ids; runtime
  resolves them.

`backend/ai/task_candidate.py` — deterministic validation of the structured
candidate: exact field set, action aliases normalized, `ai_instruction`
bounded (`validate_ai_instruction`), schedule shapes canonicalized
(value-unit / compound-key / flat-unit / embedded-unit forms with a bounded
unit vocabulary, Persian digits translated; unknown/bool/list shapes fall
through to honest rejection with structure diagnostics), payload bounded,
timezone consistency, flag types. `parse_schedule` remains the deterministic
schedule authority.

`backend/ai/tools/task.py` — `CreateTaskTool` contains the **deterministic
creation gate**: when the ORIGINAL human request derives a content policy
(named source / length / language), `ai_instruction` is forced to the verbatim
request (repairing a model omission/paraphrase) before persistence. A task
with a source/length constraint can never be created as a static task.

**Live-validated phrasings** (in-process): the exact Persian request
"هر 5 دقیقه تکست بیو من رو به یه دیالوگ رندوم از آیانامی ری تغییر بده باید
زیر 60 کاراکتر باشه" and its English equivalent create a 300-second interval
bio task with the verbatim `ai_instruction`, empty action arguments, and no
baked content.

---

## 4. Static vs generated content

- **Static**: content explicitly supplied by the user ("say exactly hello") —
  no `ai_instruction`; the action arguments are the persisted static content
  and the executor path is fully deterministic (zero provider calls).
- **Generated**: per-run varied content — `ai_instruction` is persisted
  verbatim; each occurrence prepares fresh content. The durable task NEVER
  converts the generation request into one static string (the original
  "Ayumi: Every star begins as a dream!" failure mode is impossible: the
  creation gate forces `ai_instruction`, and the interpreter contract forbids
  baking).

---

## 5. Durable semantic task definition

The `ai_tasks` record preserves: action/tool contract (`actions` snapshot),
schedule/cadence (`schedule` + `schedule_type` + `timezone`), generation mode
(non-empty `ai_instruction` ⇒ generated), the verbatim semantic generation
instruction (character, franchise/work context, content type, randomness,
length — all as spoken), deterministic constraints (derived from the
instruction by `derive_policy`), and destination (`notification_destination`
with trusted runtime chat id + delivery flags). No schema change was needed:
the existing `ai_tasks`/`ai_task_occurrences` two-table model carries all of
it (the `ai_instruction` column, persistence, and row reconstruction were
already wired end-to-end and are covered by tests).

---

## 6. AI generation contract (occurrence time)

`backend/ai/task_execution.py::AIActionPreparator` builds each preparation
prompt from the PERSISTED instruction (never the original chat message), the
derived policy's `describe()` (enforced contract), the task's own action
templates (fixed tool names), and the current time/timezone. `tools=[]` — the
provider cannot trigger execution; it can only emit arguments for the task's
own tools. Output must be exactly `{"actions": [...]}`, one entry per
template, same tool names; structure and content are validated
deterministically before anything is accepted.

The generation prompt therefore preserves: generate dialogue; character =
as spoken (e.g. Rei Ayanami / آیانامی ری); franchise/work context = as spoken
(e.g. Neon Genesis Evangelion / انیمه نئون جنسیس); random/different candidate
per occurrence; strict length; destination = the task's bio tool. The model
may use its own knowledge of the character — this is generated in-character
dialogue, not exact-canonical-quote retrieval.

---

## 7. Character / source fidelity contract

`backend/ai/preparation_policy.py` — deterministic policy derived from the
instruction (token-based; the length patterns use the pre-existing regexes,
source extraction and attribution are pure token scans — no new regex):

- **Source extraction** (`_extract_source`): fixed-vocabulary marker scan
  (از/از طرف/from/aus/の/의) with instrumental-marker exclusion
  (استفاده از …), head-noun ranking (دیالوگ/quote/… window), descriptor skip
  (کاراکتر/شخصیت/character), stop tokens (verbs, conjunctions). Pins the
  character, not the anime, for "از آیانامی ری از انیمه نئون جنسیس".
- **Self-attribution** (`_check_attribution`): generated content must OPEN
  with the requested source's full name (all tokens, spoken order,
  case-insensitive; opening quotes tolerated; separator may attach to the
  last name token) followed by a dialogue separator and a non-empty line.
  Rejects: a different speaker ("Ayumi: …"), a short form ("Rei: …"), an
  in-text mention, a name-only line, and unattributed generic text. This is
  deterministic self-attribution of a GENERATED line — never a canonical-quote
  claim.
- **Exact-quote requests fail closed** (`quote_exact`): explicit vocabulary
  (نقل قول/نقل‌قول/عین جمله/کلمه به کلمه/exact quote/verbatim/word for word)
  plus a named source ⇒ the occurrence fails closed ("cannot be independently
  verified: no trusted source corpus or verifier is configured"). A generated
  line — even correctly self-attributed — is never presented as an
  authenticated quotation.
- **Language**: derived only when the instruction names one (فارسی/پارسی/
  persian/…); script-based letter checks.
- **Length**: "زیر 60 کاراکتر" / "below 60 characters" ⇒ `max_length=59`
  (STRICTLY < 60; 60 is rejected). "دقیقا N کاراکتر" / "exactly N" ⇒
  `exact_length=N`. Invalid output is rejected — never truncated, never
  repaired.
- **Garbage checks**: empty, JSON-payload, and provider-failure markers are
  rejected.

---

## 8. Bio guardian (global mutation boundary)

`backend/services/bio_guardian.py` — `guard_bio_mutation(mutation)`:

- **Invariant**: NO successful Telegram bio mutation (`UpdateProfileRequest
  about=...`) more than once within any rolling 60-second window, across EVERY
  path — manual commands, scheduled tasks, AI tasks, prepare-ahead execution,
  retries, recovery, restart, future callers. The guard lives at the shared
  mutation boundary (`bio_service._apply_profile`), not in the scheduler.
- **Concurrency-safe**: one `asyncio.Lock` serializes racing callers; the
  window is re-checked inside the lock — of N simultaneous callers, exactly
  one reaches Telegram.
- **Success semantics**: the window timestamp is recorded ONLY after the
  mutation callable returns successfully. A failed RPC, a FloodWait (surfaces
  as the existing honest "telegram flood wait Ns" failure), a timeout, or a
  cancellation never opens the window — an immediate retry is allowed.
- **Honest rejection**: a blocked mutation raises `BioMutationGuarded` and is
  never reported as success; `bio_service` returns the existing "NOT updated"
  failure text.
- **Retry semantics**: `classify_failure(BioMutationGuarded)` ⇒ permanent,
  non-retryable task failure — a guardian rejection cannot start a retry storm
  or burn occurrence attempts.

`backend/services/bio_service.py` — `_apply_profile` is the single real
mutation boundary: renders through the bio engine, executes exactly one
`UpdateProfileRequest` under `guard_bio_mutation`, persists `last_bio` only
after Telegram confirms success, preserves FloodWait/timeout semantics. The
bio tool (`bio_set_text`), the profile cron scheduler, manual panels, and AI
task execution all converge on it.

---

## 9. Prepare-ahead

`TaskScheduler` creates the next occurrence within the configured horizon;
`TaskExecutionCoordinator.prepare_ahead` runs the bounded AI preparation
(§6) BEFORE the boundary and persists the validated result as a
version-stamped `PreparedAction` in the occurrence's `preparation_metadata`
(no schema change):

- **Side-effect free**: never executes a tool, never calls Telegram, never
  opens the guardian window (tested).
- **Occurrence-specific**: metadata lives on the occurrence row; content is
  never shared across occurrences; a restart cannot reuse an old
  occurrence's content for a new one.
- **Idempotent**: repeated preparation is a no-op once durably prepared;
  duplicate wakes execute once.
- **Failure**: bounded (max 3 rounds), logged, leaves the occurrence
  unprepared for the honest occurrence-time path — never an invalid action,
  never a fabricated fallback.
- **Boundary re-proof** (`_prepared_from_metadata`): at execution the
  persisted action is re-validated — kind, SAME task definition version,
  tool name identical to the occurrence snapshot, contract-valid arguments,
  and policy-valid content. Anything stale/invalid ⇒ ignored (never executed
  blindly) ⇒ honest occurrence-time preparation.

---

## 10. Exactly-once execution & recovery

- Occurrences are claimed via CAS; status transitions
  (pending → claimed → running → succeeded/failed/retry_pending) serialize
  duplicate wakes; concurrent scheduler workers cannot double-execute.
- Prepared occurrences execute exactly once at the boundary with zero
  additional provider rounds (tested).
- Restart/recovery: already-succeeded occurrences never re-run;
  `retry_pending` honors `retry_at`; future claimed occurrences are exempt;
  stale preparation (old task version) is rejected at the boundary.
- The guardian's successful-mutation state is process-local; see §14 for the
  documented (not applied) durable-window option.

---

## 11. Failure / retry semantics

`backend/ai/retry.py` — deterministic classification: TimeoutError ⇒
retryable with bounded exponential backoff (max 3 attempts, 30s→15m); guard
rejections, policy violations, and structural violations ⇒ permanent honest
failures (occurrence marked failed, error metadata recorded). No infinite
retries, no uncontrolled retry storms, no false success claims.

---

## 12. ToolExecutor boundary

AI generation never directly mutates Telegram. The final mutation path is:

```
PreparedAction → TaskExecutionCoordinator → ToolExecutor
→ registered bio tool → bio_service._apply_profile → guard_bio_mutation
→ Telegram UpdateProfileRequest
```

`execute_calls` is bounded by `MAX_EXECUTION_SECONDS`; tool results drive the
occurrence outcome; `_deliver_result` is best-effort post-success delivery
only when the task explicitly opted in.

---

## 13. Tests

Focused suites (all in-process; scripted providers):

| Suite | Coverage |
|---|---|
| `tests/test_task_nl_interval_creation.py` (41) | Exact two live Persian requests → 300s interval bio tasks with verbatim `ai_instruction`; 13 interval phrasings; multi-line requests; ambiguous shapes still rejected; no-intro phrasings stay conversational; model-emitted schedule shapes; interpreter prompt contract; `max_length=59`; Ayumi + unattributed lines rejected; guardian one-mutation-per-window |
| `tests/test_task_source_fidelity.py` (16) | Creation gate (baked Ayumi candidate cannot become a static task); paraphrase repair; unconstrained tasks stay static; interpreter contract; live "Ayumi: Every star begins as a dream!" rejected with zero tool calls; wrong-speaker/short-form/in-text rejected; matching attributed line executes exactly once; drift→regenerate→succeed once; bounded regeneration (exactly `MAX_PREPARATION_ATTEMPTS` rounds) then fail closed; exact-quote task fails closed with zero mutations; prepare-ahead side-effect free, persists validated metadata, boundary executes it with zero provider rounds; drift never persists; manual+scheduled share the guardian; retry cannot bypass |
| `tests/test_preparation_policy_source.py` (31) | Source extraction (Persian/English, head-noun ranking, instrumental exclusion, stop tokens); wrong-speaker/short-form/name-only/in-text/generic (EN+FA) rejected; matching attributed lines accepted (EN+FA); separator variants; exact-quote fail closed; exact-quote without source inert; under-60 ⇒ max 59 (59 ok, 60/61 rejected); exact/at-most unchanged; describe() contract |
| `tests/test_bio_guardian.py` (9) | First mutation opens window; second within 60s rejected honestly; failed mutation never opens window; concurrency (6 callers ⇒ exactly 1 Telegram hit); window expiry allows next; CancelledError propagates without window; WINDOW_SECONDS == 60; **FloodWait ⇒ honest failure, no window, immediate retry allowed**; **guardian rejection ⇒ permanent non-retryable classification (no retry storm)** |
| `tests/test_task_prepare_ahead.py` (20+) | Prepare-ahead persists without executing; static tasks skipped; horizon; idempotency across wakes; failure leaves occurrence unprepared; recurring task prepares N+1 during the interval and executes N+1 once at the boundary (restart simulation); duplicate wakes execute once; stale preparation rejected; policy-invalid content never executes; regenerated content passes and executes once; real preparator wraps policy violations |
| `tests/test_task_ai_preparation.py`, `tests/test_task_execution.py`, `tests/test_task_scheduler.py`, `tests/test_task_restart_recovery.py`, `tests/test_task_hardening.py`, `tests/test_task_candidate_contract.py`, `tests/test_task_contract.py`, `tests/test_task_repository.py` | Occurrence state machine, CAS claims, recovery hardening, restart anti-spam, retry contract, candidate/schedule validation, repository persistence (incl. `ai_instruction` round-trip) |
| `tests/test_task_nl_creation.py`, `tests/test_task_creation_diagnostics.py`, `tests/test_task_send_execution.py`, `tests/test_task_management.py`, `tests/test_task_trigger_events.py` | NL creation flow, diagnostics, send_message execution, management, event triggers |
| `tests/test_15_bio_username.py`, `tests/test_current_bio_determinism.py`, `tests/test_get_bio_full_profile.py` | Bio/username engines, cron scheduler, determinism |

---

## 14. Database impact

**NO DATABASE / SCHEMA CHANGE in this phase or the AI-task phases.** The
existing `ai_tasks` (`ai_instruction` column) and `ai_task_occurrences`
(`preparation_metadata` column) tables fully represent the system.

**Documented optional hardening (NOT applied — requires a schema change, which
the user applies manually):** the Bio guardian's 60-second window is
process-local (`time.monotonic` + asyncio lock). After a process restart an
immediate bio mutation is not blocked by the pre-restart window. `bio_state`
has no mutation timestamp (`updated_at` is written by every state save, so it
cannot reconstruct the window). To make the window survive restarts:

```sql
-- migration (apply manually if desired)
ALTER TABLE public.bio_state
  ADD COLUMN last_bio_mutation_at TIMESTAMPTZ;

-- rollback
ALTER TABLE public.bio_state
  DROP COLUMN last_bio_mutation_at;
```

Guardian startup reconstruction: on first guard use, if
`last_bio_mutation_at` is within `WINDOW_SECONDS` of now, treat the window as
open until it expires. This is OPTIONAL hardening; the exactly-once occurrence
state machine (CAS) is fully durable regardless, and no current test or
behavior depends on it.

---

## 15. Validation (this phase)

| Command | Result |
|---|---|
| `pytest tests/test_bio_guardian.py -q` | **9 passed** |
| `pytest tests/ -q` (full suite) | **1925 passed, 24 skipped, 1 warning** in 64.04s |
| `py_compile` (changed files) | OK |
| `git diff --check` | clean |
| Regex audit | no new regex in this phase; attribution/source logic remains token-based |
| Changed files (this phase) | exactly 1 (`tests/test_bio_guardian.py`) + this report |

Live Telegram, live provider, and live Supabase verification: **NOT performed**
(no session credentials in this workspace). In-process tests exercise the real
deterministic layers with scripted providers; no production claim is made.

---

## 16. Delivery record (verified)

| Item | Value |
|---|---|
| Implementation commit | `ac3fe40` (`test: pin flood-wait guardian semantics and honest rejection classification`) |
| Report commit | `4597fb2` (`docs: rewrite implementation report as current-state document`) |
| Push | `git push origin main` (non-force fast-forward); verified via `fetch` + `rev-parse` + `ls-remote` |
| Verified remote HEAD | equals local HEAD post-push (authoritative `ls-remote`) |
| Working tree | Clean except the pre-existing untracked nested clone `telegram-self-bot/` (untouched) |

---

## 17. Phase history (compact)

| Phase | Commit | Outcome |
|---|---|---|
| Occurrence-time AI preparation activation | `e212f44` | AI-assisted tasks prepare arguments at occurrence time through the provider + deterministic validation |
| Restart-safe recovery hardening | `f1ae030` | Interrupted/retry contract; no duplicate execution on restart |
| Prepare-ahead execution + anti-spam | `d59524c` | Next occurrence prepared within horizon, version-stamped, boundary re-proof, exactly-once |
| Bio source constraints + 60s guardian | `b046f10` | `derive_policy` source/length semantics; hard Bio mutation guardian at the shared boundary |
| Semantic source fidelity (creation gate) | `e8d6cfb` | Interpreter AI-content contract; verbatim `ai_instruction` gate; bounded regeneration; fail-closed validation |
| Semantic interval task creation | `bf98fed` | Registered profile tools + interval contract in interpreter; schedule-shape tolerance; source-extraction fix |
| Source-attributed dialogue generation | `73d0daf` | Two-class contract: generated in-character dialogue with deterministic self-attribution; exact-quote requests fail closed |
| This phase (guardian pins + current-state report) | `ac3fe40` + report | FloodWait/retry-storm pins; full-suite green; current-state documentation |

---

## 18. Remaining limitations / blockers

1. **Live verification**: no live Telegram/provider/Supabase run was performed
   in this workspace; behavior is proven in-process against the real
   deterministic layers with scripted providers.
2. **Canonical-quote authenticity**: no trusted corpus/verifier exists;
   exact-quote requests fail closed by design, and accepted lines are
   self-attributed generated dialogue — never authenticated quotations.
3. **Attribution strictness**: the line must open with the exact spoken source
   name; other spellings/orders (e.g. "Rei Ayanami" for "Ayanami Rei") are
   rejected and regenerated within the bounded budget.
4. **Guardian window durability**: the 60-second window is process-local;
   cross-restart persistence requires the documented (not applied) schema
   addition in §14.
5. **Unclassified schedule phrasings**: interval-without-intro phrasings
   ("پنج دقیقه یکبار") route conversationally by design; months are recognized
   as recurrence markers but their length is the model's semantic choice.
