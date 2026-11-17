# IMPLEMENTATION REPORT

**Reporting date:** 2026-11-17
**Delivery:** Scheduled History summarization reliability — provider/model exhaustion
and failure-metadata persistence
**Base commit:** `0f3c39a02ef547759a4419686b98e46ba3f69e3e` (verified equal to
`origin/main` by `git fetch` + `git rev-parse origin/main` before editing)
**Branch:** `freebuff/f5137e20b1c5761eea04f020`
**Status:** **Both diagnosed defects are fixed and offline-tested. Nothing was
deployed, no SQL was executed, and no live provider, Supabase or Render call was
made from this workspace.**

---

## 1. Starting state (verified against source, not a previous report)

| Item | Verified state |
|---|---|
| Local HEAD before editing | `0f3c39a02ef547759a4419686b98e46ba3f69e3e` |
| `origin/main` | identical to HEAD (fast-forwarded earlier in this task after main was 20 commits ahead) |
| Working tree before editing | clean; only untracked obsolete P1 drafts, removed before delivery |
| Scheduled History path | `scheduled_history` tool → `history_schedule_service` → `history_ai_service.summarize_incremental` → `ProviderManager.chat` → durable CAS |
| History storage | `public.history_schedules` already exists with `summary`, `scanned_cursor`, `summarized_cursor`, `revision`, `version`, `last_error`, `last_error_at`; no schema change was needed |

The live log entries supplied with this task (2026-11-18) were treated as
evidence of *behaviour*, and every conclusion below was re-derived from the
current source.

## 2. Root cause — provider/model exhaustion (no fake success involved)

`ProviderManager` did attempt multiple candidates. The failure is exhaustion, and
three independent facts explain why every candidate was unusable:

1. **The production candidate pool was effectively empty.** Routing's candidate
   list for a provider is the configured default model followed by
   discovery-published models (`ProviderManager._candidate_models`). The durable
   pool (`backend/ai/candidate_pool.py`, `ai_model_candidates:v1`) is restored
   once at boot and only from a **complete** Test Modules run no older than 24 h.
   With no such document the pool is empty, which is exactly the live
   `models=1` shape: one model per provider was attempted. The live matrix shows
   precisely one model per provider (Groq, SambaNova, Cohere), so model-level
   fallback had nothing else to route to.
2. **Several configured default model ids are stale in the repository itself.**
   `backend/ai/providers/base/defaults.py` still configures
   `groq/llama-3.3-70b-versatile`, `sambanova/DeepSeek-V3-0324` and
   `cohere/command-r-plus`. The live responses for those ids were HTTP 404 /
   `model_not_found`. This is a repository-owned configuration defect, not a
   routing defect.
3. **Three providers were genuinely rate-limited** (Gemini, OpenAI,
   NaraRouter). A 429 is not retriedable in the same-model sense: `health.py`
   defines `RETRYABLE_FAILURES = {"network", "timeout", "server"}`. A
   `rate_limited` failure is retried only when its `Retry-After` is short
   (`_attempt_with_retry`), otherwise routing fails over immediately; the logged
   `retryable=False` is therefore **intentional and correct**, not a defect. It
   means "do not hammer this model again immediately", and `chat()` still moved
   to the next candidate.

**No model id was changed.** Credentials for a live `/models` query are not
available in this workspace, so replacing ids would be speculation. Instead the
router now keeps a provider eligible when its configured model has been *proven*
dead in-process: `_candidate_models` appends the repository's curated offline
catalog (`backend/ai/model_discovery.py::_FALLBACK_CATALOG`, exposed as
`fallback_catalog()`) **only after** `_model_unavailable(name, configured)` is
true. Every catalog candidate is still re-proven by a real attempt and is
quarantined by the existing health rules when it fails. This changes no
provider, adds no dependency and invents no identifier.

The History layer still calls `ProviderManager.chat` and nothing else. It has no
provider-specific router, and when the manager reports exhaustion the service
records an honest failure; it never fabricates a summary.

## 3. Root cause — `HISTORY_SCHEDULE_FAILURE_RECORD_FAILED ... error=TypeError`

**Exact expression:** the History CAS write payloads contained raw `datetime`
objects, and `postgrest-py 0.16.11` (from `supabase==2.4.2`) hands the payload to
`httpx` with `json=...`, whose encoder calls `json.dumps`. That raises
`TypeError: Object of type datetime is not JSON serializable` **before any HTTP
request is sent**.

The failing write was `SupabaseTaskRepository.record_history_failure`, whose
payload sets `last_error_at=now` and `updated_at=now`. The same latent defect
existed in `commit_history_success`, `advance_history_scan`,
`reset_history_summary` and the legacy-row repair path — they were only not
observed live because the AI stage failed before a success commit. The
in-memory repository and the test fake table store dicts directly, which is why
the existing suite passed.

**Proof (offline, no live database):**

- `postgrest/_sync/request_builder.py` passes `json=self.json` to httpx.
- `json.dumps({"last_error_at": datetime.now(timezone.utc)})` →
  `TypeError: Object of type datetime is not JSON serializable`.
- `task_repository._serialize(datetime)` → ISO-8601 string; every `ai_tasks`
  write already uses it. History writes did not.

**Fix:** every History write payload is passed through `_serialize` after the
existing validation (validation still accepts datetime-shaped values because
rows are parsed back to datetimes). The TypeError is no longer swallowed or
hidden: the repository still re-raises `TypeError`/`ValueError`, and
`_record_failure` now logs a bounded, single-line `detail=` (message text only,
truncated to 200 chars) in addition to the exception class, so a future transport
failure is actionable. No summary, message body, prompt or identifier is logged.

**Invariants preserved and tested:** a failure record leaves `summary`,
`scanned_cursor`, `summarized_cursor` and `revision` untouched; it increments
`version` exactly once; the next CAS with the old version is stale and returns
`None`; a fresh CAS still has to match; owner/peer scoping and the
one-row-per-owner+peer uniqueness are unchanged.

## 4. Changes

| # | File | Change |
|---|---|---|
| C1 | `backend/ai/database/task_repository.py` | Serialize every History write payload (`record_history_failure`, `commit_history_success`, `advance_history_scan`, `reset_history_summary`, deleted-row repair) with `_serialize` before the PostgREST update; comment records why validation may keep datetime objects |
| C2 | `backend/services/history_schedule_service.py` | `_record_failure` warning now carries a bounded `detail=` for the persistence exception; scheduled AI trace carries `candidates_failed` / `failure_categories` from the AI result |
| C3 | `backend/services/history_ai_service.py` | `HistoryAIError` gained bounded `metadata`; new `_provider_diagnostics` extracts only counts and normalized categories from the manager's matrix (no response text); failed provider calls put those scalars in the occurrence trace and return them in the failure data |
| C4 | `backend/ai/providers/manager/manager.py` | `_candidate_models` appends curated catalog ids for a provider whose configured model was proven unavailable in-process; new `_catalog_models` helper; no routing authority moved |
| C5 | `backend/ai/model_discovery.py` | Public `fallback_catalog()` returning a copy of the curated offline catalog |
| C6 | `tests/test_history_schedules.py` | Regression: every History write payload survives `json.dumps` (the real transport encoding), with correct version/revision behaviour |
| C7 | `tests/test_history_schedule.py` | Regression: exhaustion failure data carries bounded counts/categories only — provider response text and message body are absent |
| C8 | `tests/test_model_candidate_pool.py` | Regression: a proven-dead configured model falls back to curated catalog candidates; a 429 without `Retry-After` is not retried on the same model and fails over |
| C9 | `ROADMAP.md` | New future section: General AI Execution Orchestrator — **APPROVED DIRECTION / DESIGN REQUIRED / NOT IMPLEMENTED**, with ordering, checkpoint semantics, boundaries, budgets, dependencies, acceptance criteria and open questions |
| C10 | `IMPLEMENTATION_REPORT.md` | This report |

No schema, migration, `DATABASE_ARCHITECTURE.md`, Evidence, Taskloom UI,
interpreter, dependency or environment change.

## 5. Tests actually executed

Installed a local virtualenv from `backend/requirements.txt` plus pytest
(Python 3.12.3, pytest 9.2.0, supabase 2.4.2). No test contacted a live service.

| Command | Result |
|---|---|
| `pytest tests/test_history_schedules.py tests/test_history_schedule.py tests/test_model_candidate_pool.py tests/test_history_ai_tools.py tests/test_history_schedule_tracing.py -q` | **176 passed** |
| `pytest tests/ -q` | **7964 passed, 27 skipped, 4 failed** in 168.53 s |
| `python -m compileall -q backend tests` | exit 0 |
| `git diff --check` | exit 0 (no whitespace errors) |

The four full-suite failures are the same set recorded in the previous report at
`6ac156e`; none touches this diff:

- `test_database_architecture_doc::test_every_migration_file_is_classified_in_the_audit_section`
- `test_database_setup_order::test_the_audited_order_is_recorded_in_the_audit_table`
- `test_private_message_db_security::test_the_documented_counts_match_the_repository`
  — these three read `DATABASE_ARCHITECTURE.md`, which this task forbids changing.
- `test_task_scheduler::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`
  — the known time-dependent test (fixed 2026-09-10 schedule is in the past).

The new tests are offline. They prove payload serialization, bounded
diagnostics, catalog fallback selection and rate-limit policy; they do **not**
prove that any live provider now answers.

## 6. Supabase impact

**None.** No SQL was executed, no migration was created or applied, no table,
function, grant, trigger or RLS policy was altered, and no private
summary/message content was read. The `public.history_schedules` contract is
unchanged; the fix only changes the JSON type sent for timestamp columns the
table already has.

## 7. Render / live verification status

**NOT LIVE VERIFIED.** This workspace has no `RENDER_*`, `SUPABASE*` or provider
credentials and no Render shell (Render Free has none). Therefore:

- the deployed commit was not compared with `origin/main`;
- no live provider success was observed;
- no live failure-metadata write was executed against PostgREST.

Expected live effect: the next scheduled run whose AI stage fails will persist
failure metadata instead of logging `TypeError`; the run after a
`model_not_found` on a configured model can now attempt that provider's curated
catalog models. Whether any candidate answers still depends on the live accounts'
quotas and on ids that only a live model-list call can confirm.

## 8. ROADMAP entry

`ROADMAP.md` now contains a separated section
**"Future roadmap: General AI Execution Orchestrator"** with status
**APPROVED DIRECTION / DESIGN REQUIRED / NOT IMPLEMENTED**. It records the
problem, intended behaviour, checkpoint semantics (application-owned, bounded,
validated; not a previous model's answer and no hidden chain-of-thought), bounded
multi-model decomposition/synthesis, cross-Self-Bot scope, ProviderManager /
ToolExecutor boundaries, cost/concurrency/idempotency/validation/privacy
requirements, dependencies, acceptance criteria and risks. It also states the
required order (reliability fix → verify → design → checkpoint foundation →
bounded orchestration → incremental adoption) and marks step 1 as
implemented/offline-tested with live verification still pending.

## 9. Deferred work

- **Live verification** of provider recovery and of failure-metadata persistence
  on Render/Supabase (owner-operated; no credentials here).
- **Authoritative model-id refresh** for the retired Groq, SambaNova and Cohere
  defaults. This needs a live provider model-list call, which was not possible;
  the catalog fallback is a mitigation, not a replacement for that refresh.
- **Filling the durable candidate pool in production** by running a complete Test
  Modules pass within the 24-hour freshness window.
- The **General AI Execution Orchestrator** itself — design only, not implemented.
- The four pre-existing test failures listed in §5.

## 10. Limitations recorded

- The TypeError fix is proven against the real transport's encoding rule
  (`json.dumps`), not against a live PostgREST instance.
- Provider exhaustion remains possible: three providers were genuinely
  rate-limited, and a curated catalog cannot manufacture quota.
- The diagnostics added are counts and normalized categories; they do not
  include provider response text by design.
