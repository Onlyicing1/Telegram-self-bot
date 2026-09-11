# IMPLEMENTATION REPORT — CURRENT STATE

> **This is a CURRENT-STATE document.** It describes the repository as it
> exists at the tip of this phase. If code changes invalidate any section,
> update this document in the same commit.

---

## 1. Implementation metadata

| Item | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Branch | `main` |
| Starting HEAD | `5b54c81f9be8d67f294ffdb42f69c84b452b4da3` (== `origin/main` at phase start) |
| Phase | **Real task deletion + durable task ids** — deleting a task was a lifecycle status write (`status="deleted"`) that left the row durable and inspectable, and the degraded in-memory store numbered its own tasks from 1 |
| Status | **IMPLEMENTED — full suite green (2121 passed, 24 skipped, 0 failed)** |
| Database impact | **NO DATABASE / SCHEMA CHANGE** (see §4) |
| Live verification | **NOT performed** (no credentials in this workspace) — see §7 |
| Delivery record | see §8 |

---

## 2. Source-verified starting state (before this phase)

Traced end to end in the current source:

```
Telegram / AI
  → TaskTransitionTool            {"action": "deleted"}
  → TaskManagementService.delete   → set_status(task_id, "deleted", version)
  → TaskRepository.update_task     → status="deleted", version+1, terminal_at
  → Supabase update (durable row kept)
```

- `TaskManagementService.delete` was documented as: *"deleted is the terminal
  lifecycle state, so a deleted task leaves the list while its row and
  occurrence history stay durable and it remains inspectable by id."*
- `TaskTransitionTool._TRANSITION_STATUSES` exposed `deleted`, and the
  structured-action vocabulary (`backend/ai/actions.py`) plus the prompt
  advertised `{"action":"task_transition","action_status":"deleted"}`.
- `Taskloom` deleted through the same CAS status write.
- `InMemoryTaskRepository` allocated task ids from a process-local counter
  that restarted at `1` (`self._next_task_id = 1`).
- `SupabaseTaskRepository.create_task` already let PostgreSQL assign the id
  (`_task_payload` excludes `id`/`version`/timestamps and the returned record
  is built from the inserted row) — now pinned by a regression test.
- Schema (`supabase/migrations/20260829000001_create_ai_tasks.sql`):
  `ai_tasks.id bigserial PRIMARY KEY`, and
  `ai_task_occurrences.task_id REFERENCES ai_tasks(id) ON DELETE RESTRICT`.

---

## 3. Exact changes

### 3.1 A real repository deletion (`delete_task`) — `backend/ai/database/task_repository.py`

New interface operation, implemented by both repositories:

```python
async def delete_task(owner_id, task_id, expected_version) -> TaskDeletionResult
```

`TaskDeletionResult` reports an outcome the caller must honour instead of
assuming success:

| Outcome | Meaning |
|---|---|
| `deleted` | the row was really removed (affected-row proof from the delete) |
| `not_found` | no such task for this owner (ownership mismatch included) |
| `stale` | version mismatch / the row changed between the CAS read and the delete |

`result.deleted` is true only for `deleted`; `result.durable` is true only
when nothing degraded into the shared in-memory fallback. `fallback_backend`
is set exactly when the mutation happened in memory — the same annotation
contract `create_task`/`update_task` already use.

- **Supabase**: read the row owner+id scoped → version check → `DELETE` the
  task's occurrences → `DELETE` the `ai_tasks` row filtered by
  `id` + `owner_id` + `version` (CAS). Only a non-empty affected-row result
  is reported as `deleted`. Transport failures degrade to the shared fallback
  and are annotated non-durable — a swallowed failure can never be reported as
  a durable deletion, so deletion uses a strict `_run_checked` variant of the
  existing `_run` helper.
- **In-memory**: physically `del` the task, remove that task's occurrences,
  and honour the same CAS/owner rules.

`deleted` was removed as a transition **target** (`_ALLOWED_TASK_TRANSITIONS`).
It stays in `TASK_STATUSES` only so a pre-existing row that already carries it
remains readable (and can only ever be updated to itself) — no new
`deleted` row can be produced.

### 3.2 Deletion is its own operation everywhere

| Layer | Change |
|---|---|
| `backend/ai/task_management.py` | `delete()` now calls `repository.delete_task(...)` and returns the `TaskDeletionResult`; the list docstring records that deletion is a real removal |
| `backend/ai/tools/task_management_tools.py` | `task_transition` no longer accepts `deleted`; new dedicated `TaskDeleteTool` (`task_delete`) requires `task_id` + `expected_version` and renders durable / degraded / stale / not-found honestly |
| `backend/ai/tools/registry.py` | registers `TaskDeleteTool` |
| `backend/ai/tools/executor.py` | progress label for `task_delete` |
| `backend/ai/actions.py` | new `task_delete` structured action (vocabulary, field set, validation, resolution to the tool); `task_transition` vocabulary is now `paused / active / completed` |
| `backend/ai/prompt/template.py` | the model is told to use `task_delete` for deletion and that deletion is never a `task_transition` status |
| `backend/bot/handlers/taskloom.py` | the Delete button uses the real deletion and reports a degraded (memory-only) deletion instead of a fake terminal state |

Nothing bypasses `ToolExecutor`, no second executor/scheduler was added, and
the AI never touches Telegram or persistence directly.

### 3.3 Durable task ids stay PostgreSQL-owned

- **Durable creation** sends no `id`; PostgreSQL `bigserial` assigns it and the
  returned record carries the database-generated id (regression-tested).
- **Degraded creation** must never restart numbering at 1 while durable tasks
  exist. `SupabaseTaskRepository` tracks the highest durable task id it has
  observed (created rows and read rows) and hands it to the fallback as a
  **provisional id floor** through `InMemoryTaskRepository.never_issue_ids_below`
  before any memory task is allocated. The floor is never written to the
  database and never allocates a durable id — PostgreSQL remains the only
  allocator. Because a failed write does not prove reads are down, the
  degradation path first makes one bounded, best-effort ceiling read
  (`SELECT id ... ORDER BY id DESC LIMIT 1`, 3 s cap); if even that fails the
  created task is still explicitly non-durable in the result.

---

## 4. Occurrences and the schema decision

`ai_task_occurrences.task_id` is `ON DELETE RESTRICT`, so a physical task
delete is impossible while children exist. The repository therefore removes the
task's occurrences first and then the task row (both owner-scoped, the task
delete CAS-guarded on `version`). Occurrences are the task's children: a task
that no longer exists cannot keep occurrences, so this is the deletion the
data model implies — not a new policy.

**No schema or migration change is required, and none was added.** No SQL is
to be applied by the owner. (If a future design prefers the database to own
the cascade, the equivalent would be replacing the existing foreign key with
`ON DELETE CASCADE` — for example
`ALTER TABLE ai_task_occurrences DROP CONSTRAINT ai_task_occurrences_task_id_fkey;`
`ALTER TABLE ai_task_occurrences ADD CONSTRAINT ai_task_occurrences_task_id_fkey FOREIGN KEY (task_id) REFERENCES ai_tasks(id) ON DELETE CASCADE;` —
but the application-side ordering above already makes real deletion correct
and safe, so this is **not** required and was **not** applied.)

The `ai_tasks.status` CHECK still lists `'deleted'`; removing a value from a
live CHECK constraint would be a schema change with no functional need (legacy
rows must stay readable), so it is untouched.

---

## 5. Tests

Updated to the new contract (deletion now removes the row instead of writing a
status): `test_task_management.py`, `test_task_repository.py`,
`test_task_hardening.py`, `test_taskloom_ui.py`, `test_capability_exposure_tools.py`,
`test_new_tool_action_path.py`, `test_tool_health_audit.py`,
`test_memory_tools.py`.

New focused regression coverage — `tests/test_task_durable_delete.py`:

| Requirement | Test |
|---|---|
| The dedicated tool removes the row through the executor | `test_task_delete_tool_removes_the_durable_row_through_the_executor` |
| Stale / missing deletion is honest, nothing changes | `test_task_delete_tool_reports_stale_and_missing_honestly` |
| A degraded deletion is never reported as durable | `test_task_delete_tool_never_reports_a_degraded_delete_as_durable` |
| An unreachable durable store cannot be claimed as deleted | `test_task_delete_tool_cannot_claim_deletion_when_the_durable_store_is_down` |
| `task_transition` is no longer a deletion path | `test_task_transition_tool_rejects_deleted`, `test_transition_tool_schema_has_no_deleted_status` |
| The structured action path reaches the repository | `test_structured_task_delete_action_reaches_the_repository` |
| The prompt advertises the dedicated operation | `test_prompt_contract_advertises_task_delete_not_a_deleted_transition` |
| The tool/API surface stays consistent | `test_registry_exposes_the_dedicated_delete_tool`, `test_action_vocabulary_keeps_transition_statuses_and_adds_delete` |

Repository-level coverage in `tests/test_task_repository.py`: physical Supabase
delete (children first, CAS filters), not-found/stale issuing no delete,
`bigserial` id ownership on durable create, the degraded-id floor, and the
non-durable degraded delete. Supabase delete persistence is also asserted in
`tests/test_task_hardening.py` against the "database" rows themselves.

---

## 6. Preserved architecture (still current)

- **Production fallback** — model-level, bounded, deterministic; the Dummy
  provider is never in production routing or fallback.
- **Discovery feed** — one discovery pass feeds the complete production
  candidate pool.
- **Test Modules** — non-blocking launch, concurrency guard, coalescing,
  five-segment progress, diagnostics budget.
- **Bio fidelity** — an explicit bio change persists the registered
  `bio_set_text` action, never `send_message`.
- **Degraded-store honesty** — a memory-only create/transition/delete is always
  reported as non-durable with the single shared `FALLBACK_NOTE`.
- **One recovery authority, one scheduler, one executor** — unchanged.

---

## 7. Database impact and live verification

**Database impact: none.** No schema, table, migration, RLS, policy, or
configuration change. The app now proves its durably-affected rows before it
reports a deletion.

**Live Telegram verification: NOT performed** (no credentials in this
workspace). The owner's end-to-end probe:

1. Ask for a task to be deleted (e.g. «تسک ۳ رو حذف کن»). The reply must be
   the `task_delete` result, and the task must be gone from the next
   `task_list` **and** from the `ai_tasks` table.
2. Create a task while the task list already contains durable ids; the created
   task's id must continue the database sequence (never restart at 1).

---

## 8. Verification and delivery record

- `python -m py_compile` on every changed Python file — OK.
- `git diff --check` — clean.
- **Full suite (`pytest tests`): `2121 passed, 24 skipped, 0 failed`**
  (previous tip: 2103 passed / 24 skipped).

| Item | Value |
|---|---|
| Starting HEAD | `5b54c81f9be8d67f294ffdb42f69c84b452b4da3` (== `origin/main` at start) |
| Change commit | `fix: make task deletion durable and keep task IDs database-owned` — the 17 implementation files above (8 backend, 9 tests) plus this delivery-record update to the report |
| Previous phase (record) | `5b54c81` — `fix: report degraded task transitions as non-durable` (2 files) |
| Working tree | pre-existing untracked stray clone `telegram-self-bot/` deliberately left untouched; no schema/migration files changed |
| Live Telegram / Supabase verification | **NOT performed** (no credentials in this workspace) — the owner runs the §7 probes manually |
