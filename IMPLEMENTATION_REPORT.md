# IMPLEMENTATION_REPORT.md — multi-select in the Saved Items (Stash) browser

> This file describes ONLY the current state of the repository for this task and
> **replaces every previous report in full**. Earlier phases (view-once media,
> SAVE_TRACE, the Save panel rename) remain in the history and their contracts
> are unchanged by this work.

| Item | Value |
|---|---|
| Task | Add multi-select to the Saved Items browser reached from Save → **Stash** |
| Branch / base | `freebuff/99799eefb106784388e20bc8` at `d96d70b` (= `origin/main` before this change) |
| Date | 2026-10-23 |
| Live Telegram / Supabase / Render | **NOT touched.** No session, no RPC, no SQL, no migration, no `.env`, no deployment |
| Documents not modified | `DATABASE_ARCHITECTURE.md`, `README.md`, `INVESTIGATION.md` |
| Interpreter | CPython 3.12 venv with `backend/requirements.txt` + pytest + pytest-asyncio |

---

## 1. What was implemented

The existing paginated browser (`panel:retrieve_saved`) now has a second mode.
Nothing about the single-item workflow changed: in browse mode every row still
opens that item's own preview panel (`panel:retrieve_item:id:<code>`), and the
preview panel's Retrieve / Rename / File name / Tags / Move / Delete rows are
byte-identical.

**Entry point.** Browse mode gains one row, **Select Multiple**
(`panel:retrieve_saved:select`). It starts a fresh selection session.

**Selection view.** The same page is rendered with one toggle per item:

* each row shows `⊡` (unselected) or `⊠` (selected) + the media glyph + the
  item's presented name + its **exact save code**;
* its callback is `panel:retrieve_saved:toggle:<code>` and toggles exactly that
  code — tapping never opens an item and no search is ever re-run;
* `Prev` / `Next` stay inside select mode (`select:page:N`) and preserve the
  selection; a toggle returns to the page being viewed;
* the footer offers **Send selected**, **Delete selected** and
  **Cancel selection** (clears the selection and returns to browse).

**Bulk review + confirmation.** `bulk:send` / `bulk:delete` render a review
panel that lists the selected items as `label · `code`` and carries ONE explicit
confirm action (`action:retrieve_bulk_send:confirm` /
`action:retrieve_bulk_delete:confirm`), plus **Back to selection** (keeps the
selection). Nothing is executed by merely opening the review.

**Execution.** The actions hand the selection to
`retrieve_service.do_bulk_send` / `do_bulk_delete`, which loop over the SAME
single-item operations (`do_retrieve` / `do_delete`). There is no second
deletion or retrieval implementation. After the run the selection is cleared and
the panel reports one honest outcome per item.

**Honest reporting.** `format_bulk_result` names how many of how many succeeded,
lists the successful codes, and gives each failure its own bounded reason
(`not found (it may already be deleted)`, `database error: …`, `the saved item
is still present after deletion`, the service's own failure text, …). A partial
result is never rendered as a success. Delete success is not inferred from the
single-item return string: the row is re-read after `do_delete` and counts as
deleted only when it is really gone.

## 2. Determinism, ownership and bounds

* **Exact identity** — the selection stores save codes, never positions or
  query results. A toggle is accepted only after `load_saved_item(code, owner)`
  verifies both existence and ownership; a missing or foreign row is refused
  with the same "no longer in your saved items" wording (never distinguishable).
* **Per-owner state** — `backend/helper/selection_state.py`, one bounded
  insertion-ordered `code → presented label` map per owner, plus the page being
  viewed. Synchronous and loop-local, like `input_state`; it never awaits, so a
  toggle cannot interleave with another.
* **Bounds** — at most 40 selected items (`MAX_SELECTION`, mirrored by
  `retrieve_service.MAX_BULK_ITEMS`); the 41st toggle is refused *and the
  refusal is shown*, never silently dropped. The service also reports any
  overflow as a failed item rather than implying it processed more than it did.
* **Expiry** — an untouched selection expires after 15 minutes; the next
  access reports an empty selection.
* **Clearing** — Cancel, a new selection session, and every bulk execution drop
  the selection. Back from a review keeps it. Leaving the panel via the
  standard Back/Home/Close is covered by the TTL and by "select starts fresh".
* **Pagination** — the selection is page-independent; the review lists every
  selected code across pages.
* **Stale / deleted items** — a toggle is re-verified; a bulk action re-verifies
  every code immediately before use and reports vanished items as failures
  while still processing the rest.
* **Callback budget** — every generated callback is
  `panel:retrieve_saved:toggle:S0001` (33 bytes) or shorter; a test asserts all
  of them are ≤ 64 bytes and use a registered `panel:` / `action:` prefix.
* **Empty selection** — review and both actions report "No items are selected"
  and touch nothing.

## 3. Which existing operations support bulk (and which do not)

| Existing operation | Bulk? | Why |
|---|---|---|
| `do_delete` | **yes** | per-item, owner-verified, album-aware; deletion is meaningful for any set |
| `do_retrieve` | **yes** | per-item forward to the current chat; reuses the single authority |
| `do_rename` | no | each item needs its own name — a bulk name would overwrite distinct labels |
| `do_change_file_name` | no | re-uploads the document per item with a per-item name |
| `do_edit_tags` / `do_move` | no | deliberately left to the single-item panel in this task (see §6) |

## 4. Changed files

| File | Change |
|---|---|
| `backend/helper/selection_state.py` | **new** — bounded, expiring, per-owner selection store (toggle/selected/codes/count/clear/page) |
| `backend/helper/symbols.py` | two structure glyphs: `SELECT_EMPTY = ⊡` (U+22A1), `SELECT_MARKED = ⊠` (U+22A0), registered as `select_empty` / `select_marked` |
| `backend/services/retrieve_service.py` | `BulkItemOutcome`, `BulkResult`, `do_bulk_delete`, `do_bulk_send`, `format_bulk_result`, `MAX_BULK_ITEMS`, owner-verified pre-check; module docstring |
| `backend/bot/handlers/retrieve.py` | browse renderer extracted unchanged, selection view, toggle, review panels, two actions, registrations, `extra` grammar documentation |
| `tests/test_retrieve_multi_select.py` | **new** — 34 tests (see §5) |
| `AGENTS.md` | Stash bullet documents the multi-select view and its service reuse |
| `IMPLEMENTATION_REPORT.md` | this report |

No database change, no migration, no new dependency, no change to Save, the AI
tools, providers, runtime, or Telegram connection code.

## 5. Tests executed (all actually run)

| Command | Result |
|---|---|
| `python -m py_compile` on every changed module | **OK** |
| `pytest tests/test_retrieve_multi_select.py -q` | **34 passed** |
| `pytest tests/test_symbols.py tests/test_colored_buttons.py tests/test_retrieve_owner_isolation.py tests/test_saved_item_sender_and_retrieve.py tests/test_save_v2_resolution.py tests/test_save_success_panel.py tests/test_save_v2_wiring.py tests/test_12_save_engine.py -q` | **254 passed** |
| `pytest tests -q -p no:randomly` (full suite) | **6752 passed, 27 skipped, 1 failed** |

The one full-suite failure is
`tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`
("interval advancement exceeded bound"). It reproduces with this working tree
stashed at `HEAD` (verified with `git stash push -u` → the same test fails →
`git stash pop`), so it is pre-existing and unrelated to this change.

Coverage in the new suite:

* browse rows still open their own item panel, plus the Select entry;
* the single-item preview keeps its Retrieve / Rename / Delete callbacks;
* select view renders the exact presented codes as toggles and marks;
* select → deselect → select; labels captured exactly as presented;
* selection survives `select:page:2` and returns to the viewed page on toggle;
* a new selection session discards the previous one; Cancel clears; Back keeps;
* missing / foreign codes are refused with identical wording;
* malformed `extra` values (`toggle:`, `select:page:abc`, `page:0`, …) survive;
* the 40-item cap refuses the 41st item *and* reports it;
* store-level: malformed input, `clear` return value, TTL expiry;
* bulk delete needs the explicit confirm callback and deletes nothing without it;
* bulk delete calls `do_delete` only for ticked codes and removes exactly those;
* a vanished item is never sent to the delete and is reported (1 of 2);
* a delete that leaves the row behind is reported as 0 of 1, not success;
* a foreign-owner row never reaches the delete and is reported as 0 of 1;
* single-item delete still works after a bulk session;
* bulk send calls `do_retrieve` once per ticked code with owner + chat;
* send failures are reported as "Sent 0 of 2" with the service's reason;
* send requires confirmation and a resolvable target chat;
* empty selection → review and both actions do nothing;
* service dedupes + preserves order; the 40-item bound is enforced and overflow
  is reported; partial formatting never starts with the success glyph;
* every multi-select callback is ≤ 64 bytes with a known prefix.

## 6. Limitations / not verified

* **No live Telegram verification.** No panel was rendered by a real Helper
  Bot, no button was tapped by a real owner, and no saved item was really
  forwarded/deleted. All evidence is application-level against the in-memory DB
  fallback and fakes.
* **In-memory fallback quirk.** `retrieve_service.do_delete` only removes the DB
  row when a Supabase client is configured (`if db:`); with the fallback it
  deletes the Telegram message but leaves the row. The bulk layer surfaces that
  honestly ("the saved item is still present after deletion"), so a bulk delete
  in a Supabase-less deployment would report failure even though Telegram
  accepted the message deletion. Fixing `do_delete` was deliberately out of
  scope.
* **Not in this task:** bulk rename, bulk move, bulk tag editing, and a
  "select all on this page" convenience row. They were left out to keep the
  change narrow; the selection model already supports them without new state.
* Pagination is page-index based on `list_saves`; if items are saved/deleted by
  another surface between two pages, a page can shift. Ticking is always by code,
  so a shifted row can never be deleted by accident.
