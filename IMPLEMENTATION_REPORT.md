# IMPLEMENTATION REPORT

## Feature and stage

- **Feature:** Unified History & Event Intelligence
- **Stage completed:** P1 — Manual and link-based history summarization in the
  Glass UI
- **Previous stage:** P0 — Investigation and specification (complete)
- **Next stage:** P2 — Incremental retrieval and per-chat checkpoints
- **Implementation commit:** `8434c2671a5bf8b8e4f186acd4913e1fa3ce2353`
- **Integration merge:** `76fb610` (merge of `origin/main` at `c83a557` into
  the P1 commit; no rebase, reset or force-push)
- **Report/delivery commit:** the commit containing this report, created on top
  of the merge and pushed to `origin/main`

The original inspection found `origin/main == HEAD == c43c5fa` and no P0
History & Event Intelligence artifacts in that checkout. P1 was implemented
from the supplied brief. While delivery was in progress, `origin/main` advanced
to `c83a557` with the actual P0 investigation and specification; the P1 commit
was merged with that history and the implementation was checked against
`INVESTIGATION.md` §11 and the P0 roadmap. The merged tree keeps the P0 work and
the P1 implementation.

## Files changed by P1

| File | Why |
|---|---|
| `backend/bot/handlers/history.py` | New `History & Intelligence` panel: bounded chat picker and paging, count/focus controls, manual background summarization, link resolution and link-boundary summarization, result/failure rendering, registration. No listener. |
| `backend/bot/handlers/misc.py` | One main-menu row: `History & Intelligence` → `panel:history`. |
| `backend/bot/router.py` | Imports and registers the history panel module with the other handlers. |
| `backend/services/history_ai_service.py` | Adds `first_message_id`/`last_message_id` result metadata and a backwards-compatible `with_identifiers` option. Defaults preserve existing AI-chat behavior. |
| `backend/services/appearance_service.py` | Adds `history` to the main-page IDs/defaults so the Appearance page's destination invariant covers the new menu panel. |
| `tests/test_history_panel.py` | New focused P1 tests (registration, picker bounds, controls, run arguments, failures, timeout/cancellation/stale writers, link forms/boundaries, prompt isolation). |
| `tests/test_appearance_ui.py` | Updates the expected main-menu count and checks the new entry is present. |
| `ROADMAP.md` | Marks P1 complete in the existing P0/phase roadmap and records the actual implemented behavior, tests and deferred scope. |

No other source file was changed by the P1 commit. The merge brought in the
P0 documentation and the already-delivered Intelligent Reaction changes from
`origin/main`; those are not P1 changes.

## Manual UI behavior

- Panel: `panel:history`, parent `menu`, title `History & Intelligence`; one
  main-menu entry.
- **Select Chat** reads the account's own dialogs with
  `get_dialogs(limit=100)` under a 15-second RPC timeout. It shows eight chats
  per page with Previous/Next, never scans beyond the 100-dialog bound, and
  handles empty lists and RPC failures honestly. Selecting a chat re-validates
  that peer against the same bounded list.
- Count controls offer exactly 100, 250, 500 and 1000. `COUNTS` is filtered by
  `history_service.MAX_HISTORY_MESSAGES`; any other value is rejected rather
  than converted into an unbounded request. The 1,001 case is explicitly
  tested.
- **Set Focus Instruction** accepts optional text, normalizes whitespace,
  enforces `history_ai_service.MAX_INSTRUCTION_CHARS` (500), and leaves the
  previous focus unchanged when the input is too long. An empty message clears
  the focus.
- **Run Summary** validates the selected chat, obtains the existing
  `get_self_client()` and `get_engine().provider_manager`, snapshots the state,
  and launches one bounded background task. It calls
  `history_ai_service.summarize_history` with `current_message_id=None`, the
  selected chat, count and focus. No AI-chat tool call is involved.
- While running, the panel shows bounded progress. The result reports the chat,
  processed/requested counts, first/last message IDs when available, and whether
  the window was complete, bounded/truncated or empty. A failure or empty
  history is never rendered as a summary; provider/retrieval error text is
  shown as a failure.
- The panel uses `with_identifiers=False`, so the provider prompt contains only
  normalized history text and the owner's focus — no message IDs, sender IDs,
  usernames, chat IDs or links. Existing AI-chat callers keep the original
  attributed prompt by default.

## Link-based behavior and exact range semantics

- **Summarize Message Link** is a registered input. It reuses
  `save_service.parse_link_target` and `save_service._fetch_linked_message`;
  no second parser, resolver, client or access path exists.
- Supported forms are the parser's current forms:
  `https://t.me/<username>/<message_id>`,
  `https://t.me/c/<chat_id>/<message_id>` and `tg://openmessage` message links.
  Chat-only links remain unsupported, as they are in the existing parser.
- Resolution is bounded at 60 seconds. The resolved message ID is checked
  against the requested ID, and the resolved chat is checked against the
  requested numeric chat when the link supplies one. A link that parses does not
  prove access.
- The UI preserves the existing distinct failure categories and their specific
  text: invalid/unsupported link, `peer_unresolved`, `message_missing`, and
  `lookup_failed`, plus the panel's timeout/unexpected-error cases.
- **Boundary:** the UI passes
  `current_message_id = linked_message_id + 1`. The history service's
  `before_id` is exclusive, and `history_ai_service._prepare` maps the current
  message ID to that exclusive bound. Therefore the linked message is included,
  no message newer than it is read, and the requested count ends at the linked
  message. A focused regression test fetches with `before_id=13` for a linked
  message `12`.
- The result identifies the actual first/last processed IDs and states that the
  linked message is the ending point. The selected count and focus are
  snapshotted when the input is submitted.

## Services reused

- `history_ai_service.summarize_history` — the single map-reduce engine and
  provider budget/timeout/failure model.
- `history_service` — bounded retrieval, provenance filtering and
  `MAX_HISTORY_MESSAGES`.
- `save_service.parse_link_target` / `_fetch_linked_message` — link parsing,
  peer resolution, message validation and classified failures.
- `get_engine().provider_manager` — the existing runtime provider manager; no
  new manager was constructed.
- `get_self_client()` — the existing authenticated self client; no new client.
- `guarded_create_task`, `begin_panel_operation`/`finish_panel_operation`,
  `ExistingPanelEditor`, `register_panel`/`register_action`/`register_input`/
  `register_inline_builder`, `InlinePanelBuilder`, `input_state`,
  `inline_sender` and the existing navigation/progress helpers.
- Appearance page registration, so the new menu destination is covered by the
  existing UI invariant.

Long runs are finite: one `guarded_create_task` with a 300-second outer
`asyncio.wait_for`, the engine's own timeout, the existing panel-operation
generation (newest writer wins) and cancellation propagation. Input handlers
validate and launch only; no scheduler, worker, permanent loop or new listener
was added.

## Tests executed and actual results

### Focused, merged tree

Command:

```text
python -m pytest tests/test_history_panel.py tests/test_history_service.py \
  tests/test_history_ai_tools.py tests/test_appearance_ui.py \
  tests/test_colored_buttons.py tests/test_cipher_category.py -q --tb=short
```

Result: **247 passed** (exit 0). This includes all P1 panel tests and the
existing history/service/UI tests.

### Clean P1 commit before the P0 merge

A read-only archive of implementation commit `8434c26` was tested:

```text
python -m pytest tests/test_history_panel.py tests/test_history_service.py \
  tests/test_history_ai_tools.py tests/test_appearance_ui.py -q --tb=short
```

Result: **175 passed** (exit 0).

Full suite on the same clean P1 commit:

```text
python -m pytest -q --tb=no
```

Result: **6 failed, 7483 passed, 27 skipped** (exit 1).

### Merged delivery tree

```text
python -m pytest -q --tb=no
```

Result: **6 failed, 7560 passed, 27 skipped** (exit 1).

### Baseline comparison actually performed

A read-only archive of the new remote baseline `c83a557` was tested with the
same full-suite command:

- Baseline `c83a557`: **6 failed, 7532 passed, 27 skipped**.
- Merged P1 tree: **6 failed, 7560 passed, 27 skipped**.
- Difference: exactly the **28 P1 tests** pass; the failure set is identical.

The six baseline failures are:

1. `tests/test_aed_db_security.py::test_the_documentation_describes_the_restrictive_model`
2. `tests/test_canonical_schema_reconciliation.py::test_the_migration_is_forward_only_and_its_successors_stay_additive`
3. `tests/test_database_setup_order.py::test_the_section_does_not_pretend_the_sql_was_executed`
4. `tests/test_database_setup_order.py::test_the_waiting_answer_migration_is_absorbed_by_part_one`
5. `tests/test_private_message_db_security.py::test_the_rls_model_names_the_two_tables_as_server_only_exceptions`
6. `tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`

They are unchanged from the clean `c83a557` baseline and are not caused by P1.
An earlier baseline at `c43c5fa` was also run before the P0 merge:
**6 failed, 7455 passed, 27 skipped**, with the same six failure names.

### Static checks

```text
python -m compileall -q backend tests   # exit 0
git diff --check                        # exit 0
git show --check --oneline 8434c26      # exit 0
```

## Deliberately not implemented

P1 does not add per-chat checkpoints, incremental retrieval, previous-summary
persistence, automatic per-chat processing, cadence configuration, scheduler
integration, Event Intelligence candidates, Task/Memory/Saved Items
integration, digest delivery, media downloads, OCR/STT/video processing, a new
summarizer, a second ProviderManager, a new Telegram client, a new listener, a
new scheduler/worker, a new AI tool, or any new database table/column/SQL.

Chat-only links remain unsupported and deferred. There is no edit/delete
detection. The existing 1,000-message and 500-character limits remain
authoritative.

## Database and deployment

- **No database change was made. No table, column, index, policy or row was
  created, modified or deleted. No Supabase operation or SQL was executed.**
- `DATABASE_ARCHITECTURE.md` was not edited.
- P1 requires no persistence and adds none.
- **Deployment:** no application deployment was performed as part of P1.
- **Live Telegram verification:** **not performed.** This environment has no
  live Telegram self-account/provider run. The owner must still open the panel
  on the real account, run one real chat and one public message link, and check
  that the reported range matches Telegram. The panel fails honestly rather
  than fabricating a summary if that live path is unavailable.

## Remaining limitations and owner actions

- Live panel/chat/link runs and permission behavior remain unverified on a real
  account.
- P1's chat picker is intentionally limited to the first 100 dialogs; a chat
  outside that bound is not selectable in this phase.
- A failed panel edit/delivery cannot show a result in Telegram; the failure is
  logged by the existing editor and the operation still finishes without
  claiming success.
- P2's checkpoint persistence needs the owner's separate schema decision and
  application; no migration is provided here.
- Existing unrelated work: the pre-merge working-tree snapshot of the
  previously uncommitted Intelligent Reaction changes is retained in the local
  stash `pre-p1-merge: prior uncommitted intelligent reaction worktree`. The
  same feature is now delivered on `origin/main` by later commits, so that
  snapshot was not committed with P1 and was not discarded.

## Git delivery

- Branch: `main`.
- P1 implementation commit `8434c26` was pushed by merging the new
  `origin/main` (`c83a557`) into it; merge commit `76fb610`. No rebase, reset,
  force-push or history rewrite was used.
- This report and the final roadmap/test updates are committed on top of the
  merge and pushed to `origin/main`.
- Remote `main` was fetched and checked after the push. The final verified
  `origin/main` HEAD is the delivery commit containing this report; its exact
  SHA is stated in the completion response.
- Final working tree after delivery: clean, with no staged or unstaged P1
  changes. The unrelated pre-merge snapshot remains in the local stash
  described above.
