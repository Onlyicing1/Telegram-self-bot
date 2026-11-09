# Roadmap — Panel update identity + selected-image cache

Status legend: **IMPLEMENTED + TESTED** = code in this tree with regression
coverage; **IMPLEMENTED, NOT LIVE-VERIFIED** = code exists, no real Telegram
client or Storage bucket was exercised; **LIMITATION** = deliberately not done /
not provable here.

This document describes the CURRENT task only; earlier feature roadmaps are
superseded (their completion state remains recorded in `IMPLEMENTATION_REPORT.md`
history).

## Part 1 — Panel updates must not rebuild the panel

| Requirement | Status | Where |
|---|---|---|
| Ordinary callback updates on an existing photo panel never call `replace_panel` | IMPLEMENTED + TESTED | `panels._render_and_edit` → `_replace_for_delivery(home_view=False)` |
| Action results (Save, Model Tester, status/progress) paint the SAME message | IMPLEMENTED + TESTED | `_handle_action` returns `(panel, result, home_view=False)` |
| Repeated progress updates keep one panel message identity | IMPLEMENTED + TESTED | `panels._edit_panel_message` → `_safe_edit` (unchanged) + no replacement |
| Delivery identity = preview flag + selected version + message kind | IMPLEMENTED + TESTED | `appearance_service.desired_state` / `needs_new_message` |
| Ordinary text/status/content is NOT an input to the replacement decision | IMPLEMENTED + TESTED | `desired_state(*, caption_fits)` — no title/body |
| Category navigation keeps the image attached | IMPLEMENTED + TESTED | `_handle_panel` → `home_view = not extra` |
| Sub-views (pickers, `panel:x:type:d`) are never replaced | IMPLEMENTED + TESTED | nav-extra guard + `home_view` |
| Selecting a different image version still replaces the panel | IMPLEMENTED + TESTED | `needs_new_message` version comparison |
| Disabling / re-enabling preview still transitions text↔media | IMPLEMENTED + TESTED | `needs_new_message` on the delivery state |
| A failed genuine transition keeps the existing panel and view | IMPLEMENTED + TESTED | `replace_panel` creates first; failure → paint the old message |
| A transient gallery-read failure is NOT a delivery verdict | IMPLEMENTED + TESTED | `delivery_ready` → `delivery_unknown`, `_replace_for_delivery` |
| Only a main page's home view may transition for caption length | IMPLEMENTED + TESTED | `fits_as_caption`, `caption_fits` |
| Operational content is never composed as the customizable home view | IMPLEMENTED + TESTED | `_handle_panel` (composition) vs `_handle_action` (none) |
| Live Telegram behaviour of a long caption on a photo message | IMPLEMENTED, NOT LIVE-VERIFIED | needs a real client |
| Root menu rebuilt for a content-only change | FIXED (regression removed) | `_replace_for_delivery` |

**Decision.** The replacement decision now reads exactly one thing: the panel
message's delivery identity. A paint additionally declares whether it is the
panel's HOME view (`home_view`), which is the only paint that may legitimately
need a text↔media transition (a view Telegram cannot hold as a caption). Content
— save results, model-test progress, status lines — can no longer influence it,
because those paints keep editing the message the running operation addresses.

## Part 2 — Selected-image cache

| Requirement | Status | Where |
|---|---|---|
| Cache only the selected image + its renditions | IMPLEMENTED + TESTED | `appearance_service._CACHE`, `_retain_only` |
| Keyed by the selected version token (no stale bytes) | IMPLEMENTED + TESTED | `_CACHE[version]`, `version != preview_version()` guards |
| Explicit memory budget (25 MiB original + 5 MiB photo + 256 KiB) | IMPLEMENTED + TESTED | `IMAGE_CACHE_BUDGET_BYTES` |
| An image that cannot fit is delivered uncached, never grows memory | IMPLEMENTED + TESTED | `_remember` budget check |
| Concurrent requests share ONE download + ONE encode (single flight) | IMPLEMENTED + TESTED | `_cache_lock` + double-checked `_original_locked` / `_rendition_locked` |
| Photo and thumbnail are encoded from the same cached original | IMPLEMENTED + TESTED | `_rendition_locked` → `_original_locked` |
| No Redis / DB / second bucket / worker / second cache service | IMPLEMENTED (by construction) | process-local dict + `asyncio.Lock` only |
| Cache disappears on restart; durable copy stays in Storage | IMPLEMENTED (by construction) | module-level RAM only |
| Gallery objects are never prefetched | IMPLEMENTED + TESTED | only the selected version is ever fetched |
| Upload + select / select / delete active / clear all invalidate | IMPLEMENTED + TESTED | `forget_image_cache()` calls in those four flows |
| Original bytes, upload quality, gallery capacity, key format unchanged | IMPLEMENTED (no code touched) | `install_image`, `_KEY_RE`, limits |
| Real Supabase Storage latency/behaviour of the cache | IMPLEMENTED, NOT LIVE-VERIFIED | no bucket/credentials here |

**Decision.** The cache is a bounded, version-keyed, process-local memo for the
ONE image the panel delivers. It is a delivery optimisation only: every miss
still goes through the existing `_download_sync` / encoder path, and every
failure returns `None` exactly as before (so the existing text fallback is
untouched).

## Test requirements and actual results

| Check | Command | Result |
|---|---|---|
| New Part 1 regression suite | `pytest tests/test_panel_update_delivery.py -q` | **12 passed** |
| New Part 2 cache suite | `pytest tests/test_appearance_image_cache.py -q` | **14 passed** |
| Appearance delivery/gallery/UI | `pytest tests/test_appearance_delivery.py tests/test_appearance_gallery.py tests/test_appearance_ui.py -q` | **118 passed** |
| All five appearance suites together | see IMPLEMENTATION_REPORT §6 | **144 passed** |
| Save / Model Tester / panels / lifecycle / inline / settings adjacency | 18 suites, see IMPLEMENTATION_REPORT §6 | **661 passed** |
| Full suite | `pytest -q` | **7351 passed, 1 failed, 27 skipped** (143.90 s) |
| Compilation | `py_compile` on both modules + both new test files | exit 0 |
| Whitespace | `git diff --check` | exit 0 |

The single full-suite failure is the known pre-existing
`tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`
(“interval advancement exceeded bound”), which also fails on the untouched base
tree; no scheduler file is in this diff.

## Remaining work

1. Live-verify on a real client: a Save, a Model Tester run and a few category
   hops must keep ONE panel message with the image attached.
2. Live-verify the genuine transitions: switching the selected image, turning
   the preview off and on must each replace the panel exactly once.
3. Live-verify cache behaviour against real Supabase Storage latency (the tests
   count calls, not wall-clock behaviour).
4. Confirm a main page whose composed text exceeds Telegram's caption limit
   transitions to a text panel with the FULL text (never truncated, never the
   root menu).
5. Apply the Appearance migration / Storage bucket setup if that is still
   pending in the target environment (unchanged by this task).
