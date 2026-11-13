# IMPLEMENTATION REPORT — History & Intelligence paginated summaries

_Base commit: `c18a2d5baeceefa66f064712bd36eba638b8faa5`. The Helper-Bot
in-place delivery fix from `77b762b` is the starting point for this change.
This document replaces all earlier reports and describes only the current
state._

## 1. Root cause of the truncation

Two independent cuts discarded generated text:

1. **Delivery cut.** The result was fitted to the panel's Telegram limit with a
   visible truncation marker, so a long summary could not be read past the
   first caption/message.
2. **Generation cut.** `_summary_body` discarded everything after
   `MAX_SUMMARY_CHARS = 2800` and appended `… (summary truncated for display)`.
   This happened BEFORE delivery, so pagination alone would still have paged an
   already-truncated string.

Both cuts are gone. `_summary_body` now keeps the complete generated summary,
and the delivery path splits that complete body into pages. There is no
truncation marker for the summary any more.

## 2. Delivery ownership (unchanged)

The panel is an inline result sent by the owner's account with the Helper Bot
as its inline identity. The only editor remains the Helper Bot:

- `history._editor` builds `ExistingPanelEditor` with `helper=get_client()` and
  **`self_client=None`**;
- the edit is Telethon's `edit_message(InputBotInlineMessageID, text=...,
  buttons=...)` (`messages.editInlineBotMessage`) using the callback's own
  inline handle;
- page navigation uses the same editor and the same handle.

No replacement panel, second message, Saved Messages message, redirect, new
Telegram client, update loop or scheduler was added. The panel's recorded
delivery state decides only the text bound (photo caption 1,024 vs text 4,096);
it never changes the destination.

## 3. Pagination design

`_paginate(body, limit, measure=_rendered_units)` returns one page when the body
already fits, otherwise it splits the body:

1. paragraphs (blank-line boundaries);
2. lines inside a paragraph;
3. sentences (English and common RTL terminators);
4. words;
5. a UTF-16 hard cut as the last resort, with a guard that avoids cutting an
   unclosed `**` or backtick span when a nearer boundary exists.

A block that overflows the page in progress is split against the remaining
space first, so short metadata does not consume page 1; the summary starts on
the page the owner receives.

Each page is rendered as:

```
**History & Intelligence**

Page N/M

<page content>
```

Page 1 is delivered automatically. A single-page result keeps the previous
buttons and has no page indicator or paging controls. `Previous` is omitted on
page 1 and `Next` on the last page (they are not disabled placeholders).
Navigation is `action:history_page:<token>:<index>` and edits the existing
panel in place.

### Exact Telegram limits

`_rendered_units` measures the text through `panel_render.render_edit("", text,
[])` — the same renderer the editor uses immediately before the RPC — not a
raw character count. This matters because the Glass UI font replaces Latin
letters (and some digit runs) with mathematical glyphs outside the BMP, which
cost two UTF-16 units each. The page budget also reserves the title and a
two-digit `Page 99/99` indicator.

Limits used: photo panel `appearance_service.MAX_CAPTION_CHARS` (1,024); text
panel `MAX_RESULT_TEXT_CHARS` (4,096). The panel's `delivered_state` chooses the
bound, with `preview_active()` as the existing fallback.

## 4. Files changed

| File | Change |
|---|---|
| `backend/bot/handlers/history.py` | Removed both display truncations; added page splitting, rendered-unit measurement, page state, `history_page` navigation, in-place page rendering, honest expired/refused-edit handling, and explicit retained-result bounds |
| `tests/test_history_panel.py` | Added pagination, navigation, independence, stale/expired/invalid callback, failed-edit, missing-handle, new-run invalidation, TTL/size bound, exact limit, styled-font and complete-summary tests |
| `ROADMAP.md` | Recorded the pagination implementation and its actual verification status |
| `IMPLEMENTATION_REPORT.md` | Replaced in full with this report |

Not changed: `history_service`, `history_ai_service`, provider code, panel
registries, `panel_edit.py`, database/Supabase, migrations,
`DATABASE_ARCHITECTURE.md`, helper bot runtime. Nothing was deployed.

## 5. Page-state lifecycle, bounds and cleanup

- State is one in-memory `OrderedDict` keyed by a 12-hex UUID token.
- Each `_PageState` stores the token, `(panel chat id, panel message id)`, the
  tuple of page strings, timestamps and the current index. One panel can have
  at most one retained state because `_remember_pages` invalidates that panel's
  previous token first.
- TTL: `_PAGE_STATE_TTL_S = 6 hours`, refreshed on a successful navigation.
- Panel bound: `_PAGE_STATE_MAX = 16` states; the least-recently-used entry is
  evicted deterministically.
- Pruning is lazy, on state lookup/creation; there is no background cleanup
  loop, timer, task or database row.
- A new History run calls `_forget_panel_pages` before its first progress edit,
  so buttons from the previous run can never paint a page that no longer
  belongs to the panel.
- Restarting the process drops state; old page buttons then produce the honest
  on-panel "no longer available" notice.
- Retained-result bounds: `MAX_RESULT_PAGES = 64` and
  `MAX_RETAINED_RESULT_CHARS = 40_000` (sum of page UTF-16 units). If either is
  exceeded, `_deliver_result` does not store state; it edits the SAME panel with
  an honest failure (`result_too_long`) and nothing is sent anywhere else.

## 6. Callback and navigation safety

- A callback carries its own 12-hex token; the state is looked up by token, then
  validated against the callback's chat and panel message. A token from panel A
  cannot paint panel B.
- Unknown/expired tokens paint "These result pages are no longer available" on
  the same panel through the same Helper Bot handle. Malformed tokens or
  indexes, out-of-range indexes and stale chat/message bindings do not edit the
  panel at all.
- A refused page edit is logged as `HISTORY_PAGE_EDIT_FAILED` with a stable
  failure kind; the index is not advanced and no `HISTORY_PAGE_DELIVERED` line
  is written. `delivered`/index updates happen only after `editor.edit`
  returns true.
- A missing inline handle is reported as `kind=no_inline_panel_handle`; no
  chat/message fallback exists because `_editor` never supplies the self client.
- The existing callback gate serialises turns per panel, so two clicks on the
  same panel cannot edit it concurrently.

## 7. Tests executed and actual results

Environment: `/tmp/venv`, Python 3.12. No live Telegram, provider, database or
network call is used by these tests.

| Check | Command | Result |
|---|---|---|
| Focused History suite | `pytest tests/test_history_panel.py -q` | **55 passed** |
| Panel/callback/lifecycle regressions | `pytest tests -q -k "history or panel or edit or callback or lifecycle"` | **863 passed, 3 skipped** |
| Full suite | `pytest tests -q` | **7600 passed, 27 skipped, 6 failed** (same six pre-existing failures listed below) |
| Compilation | `python3 -m py_compile backend/bot/handlers/history.py tests/test_history_panel.py` | **exit 0** |
| Whitespace | `git diff --check` | **exit 0** |

The focused file now covers:

- a short result with no paging controls;
- a long result split into pages;
- first-page and last-page `Previous`/`Next` behavior;
- walking every page in the same panel and preserving content;
- exact 1,024/4,096 limits including title and page indicator;
- a styled Glass UI font (non-BMP glyphs) staying within the limit;
- paragraph/sentence/word splitting and balanced `**` markers;
- a complete summary beyond the old 2,800-character display cap;
- two panels with independent tokens/indexes and cross-panel rejection;
- malformed, out-of-range, stale-chat and stale-message callbacks;
- expired state answered honestly on the same panel;
- a refused navigation edit not advancing state;
- a missing inline handle making no edit;
- a new run invalidating old pages;
- TTL expiry, per-panel eviction, LRU/panel bound;
- a result beyond the retained-page bound failing honestly on the original
  panel with no state stored;
- Helper-Bot-only edits, no self-client edit, no lifecycle call, no replacement
  or Saved Messages delivery.

### Full-suite failures (pre-existing)

The same six tests failed before this change (baseline: 7,587 passed, 27
skipped, 6 failed). This change adds 13 focused tests; no History or pagination
test is among the failures:

- `tests/test_aed_db_security.py::test_the_documentation_describes_the_restrictive_model`
- `tests/test_canonical_schema_reconciliation.py::test_the_migration_is_forward_only_and_its_successors_stay_additive`
- `tests/test_database_setup_order.py::test_the_section_does_not_pretend_the_sql_was_executed`
- `tests/test_database_setup_order.py::test_the_waiting_answer_migration_is_absorbed_by_part_one`
- `tests/test_private_message_db_security.py::test_the_rls_model_names_the_two_tables_as_server_only_exceptions`
- `tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`

They are documentation/schema-drift and an unrelated scheduler-recovery test;
none touches `history.py`, panel delivery or pagination. They were not modified
to make them pass.

## 8. What was verified automatically

- Complete summary retention through page state (no 2,800-character cut, no
  "truncated for display" marker).
- Page text length measured after the actual font/render transform.
- Navigation state isolation between two panels and rejection across panels.
- Expiry, malformed callbacks, invalid indexes, failed edits and missing inline
  handles.
- Only the Helper Bot entity receives edits; the self-bot client receives none;
  no other message/panel is created.
- State bounds and TTL, and new-run invalidation.

## 9. Live Telegram verification status

**Not performed by this implementation session.** There are no Telegram,
Render or Supabase credentials in the delivery environment, so no live panel,
callback, caption or navigation was exercised.

The owner previously reported one successful live result in the original panel
for a summary that fit its panel. That is an owner observation from before
pagination; it does not verify multi-page navigation, photo-caption paging, the
styled-font measurement or any failure path. Those remain the owner's live
acceptance step.

## 10. Remaining limitations

- Page state is process-local and expires after six hours. A restart or expiry
  makes old page buttons show the honest "no longer available" notice; the owner
  must run the summary again. This is preferred to unbounded or persistent
  result state and required no schema change.
- Navigation state is not persisted and is not shared across processes.
- Splitting preserves all words and content, but whitespace exactly at a page
  boundary may be normalised (for example a blank line becomes the page break).
  An over-long unbroken token is cut at UTF-16 boundaries.
- The 64-page/40,000-character safety bound refuses a result that exceeds it
  rather than retaining unbounded state or silently truncating it.
- Correctness of the text bound depends on Telegram accepting the measured
  caption/message. A permission, deleted-panel or invalid-handle refusal is
  handled honestly on the panel, but cannot be prevented client-side.
- No deployment was performed.
