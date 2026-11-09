# IMPLEMENTATION REPORT — Panel update identity + bounded selected-image cache

_Base commit: `f687e601e9f91e9cca0a88e70166614358732997` (`main`). This document
replaces all earlier reports; only the current verified state is described._

## 1. Root cause of the panel replacement regression

`backend/helper/panels.py::_render_and_edit` consulted
`_replace_for_delivery(...)` on **every** paint, and that decision was made from
the paint's own text:

```python
# backend/services/appearance_service.py (before)
def desired_state(title="", body=""):
    caption = caption_for(_compose_rendered(title, body))   # "" if empty or >1024
    if preview_active() and _READY_VALUE and caption:
        return ("media", preview_version())
    return ("text", "")

def needs_new_message(chat_id, msg_id, title, body):
    return delivered_state(chat_id, msg_id) != desired_state(title, body)
```

For a panel message already on screen as `("media", <version>)`, any paint whose
text could not be a caption — empty, or longer than Telegram's 1024-character
caption limit (a Save metadata/result block, a terminal Model Tester results
view, a header + live page body) — computed `("text", "")` and therefore
"needs a new message". `replace_panel(panel_id, "")` then created a **new inline
panel** (the panel's home view) and deleted the old message.

The eligibility gate could not stop it: `_handle_action` returns the session's
current panel with its (empty) nav extra, so an **action result** looked exactly
like a customizable home view. The result was:

- the panel message the operation was still editing (`chat_id, msg_id`) was
  deleted mid-operation;
- the owner saw the rebuilt category (root menu) instead of the save result or
  the progress line, and the operation's later edits went to a dead message.

A second, smaller contributor: `_READY_VALUE` (does the selected object really
exist?) was only computed by the inline build, never by the edit path, so a
repaint in a fresh process could also compute `("text","")` for a media panel.

## 2. Exact behaviour changed (Part 1)

### 2.1 Delivery identity is no longer content

`backend/services/appearance_service.py`:

```python
def desired_state(*, caption_fits: bool = True) -> tuple[str, str]:
    version = preview_version()
    if caption_fits and preview_active() and _READY_VALUE and version:
        return ("media", version)
    return ("text", "")

def needs_new_message(chat_id, msg_id, *, caption_fits: bool = True) -> bool:
    desired = desired_state(caption_fits=caption_fits)
    delivered = delivered_state(chat_id, msg_id)
    if delivered is None:
        return desired[0] == "media"
    return delivered != desired
```

`fits_as_caption(title, body)` answers whether a photo message can carry this
MAIN-PAGE view at all (a capability, not a text diff). `delivery_ready()` now
records whether its last probe FAILED (`delivery_unknown()`), so a transient
Storage listing error is never read as "this panel should be text".

### 2.2 Operational paints are not eligible

`backend/helper/panels.py`:

- `_render_and_edit(..., *, home_view: bool = False)` passes that flag to
  `_replace_for_delivery`, which returns `False` immediately when it is not set;
- paint tuples now carry the flag: `_handle_panel` → `not extra` (a panel's own
  home view only), `_handle_navigation` home → `True`, back → `not prev_extra`,
  `_handle_action` → `False` (an action result is always operational);
- the router and `dispatch_click` pass it through; `_replace_for_delivery` also
  keeps the existing nav-extra guard and awaits `delivery_ready()` before
  deciding, keeping the panel when the probe could not answer.

## 3. How normal panel updates now preserve the existing message

A Save result, a Model Tester progress line, a status or confirmation is painted
by `_render_and_edit` with `home_view=False`, so `_replace_for_delivery` returns
without doing anything; `_finalize_panel` → `render_edit` → `_present` →
`_safe_edit` edits the SAME `(chat_id, msg_id)` (with the existing unchanged
content skip and single-writer painter). Repeated updates are repeated edits of
one message; the keyboard is rebuilt from the panel's own result exactly as
before, and the session/nav stack is untouched.

## 4. Which genuine media transitions still replace

Replacement happens only when the recorded delivery state differs from the
identity the settings require:

| Transition | Replaces |
|---|---|
| Selected image version changed (upload+select, or selecting another stored image) | yes, once |
| Preview disabled (media → text) | yes, once |
| Preview re-enabled (text → media) | yes, once |
| Selected object deleted / selection cleared while a media panel is on screen | yes, once |
| Unknown state (panel from an older build) that should be media | yes, once |
| Main-page HOME view whose full text cannot be a caption on a media message | yes, once (full text preserved; the replacement renders that page, never the root menu) |
| Save / model test / progress / status / confirmation content | **no** |
| Category navigation, Back, Home (caption fits) | **no** |
| A failed replacement | **no** — it is attempted, fails, and the old message is painted |

`replace_panel` itself is unchanged: create first, delete the old message only
after the new one exists; a failed replacement keeps the existing panel.

## 5. Cache design, budget and invalidation (Part 2)

`backend/services/appearance_service.py`:

- `IMAGE_CACHE_BUDGET_BYTES = MAX_IMAGE_BYTES (25 MiB) + TELEGRAM_PHOTO_MAX_BYTES
  (5 MiB) + 256 KiB` — the most ONE selected image can legitimately need;
- `_CACHE: dict[version, {"original", "photo", "thumb", "mime", "used"}]` —
  process-local RAM only; `_retain_only(version)` drops every other version, so
  the cache holds the selected image and nothing else (the gallery is never
  prefetched);
- `_remember()` refuses any piece that would exceed the budget (the caller still
  receives the bytes; memory never grows past the budget);
- `_cache_lock(version)` (one `asyncio.Lock` per version) with double-checked
  `_original_locked()` / `_rendition_locked()`: one download, one encode per
  rendition, and the photo and thumbnail are encoded from the SAME cached
  original bytes;
- stale guards: every entry point re-checks `version == preview_version()`, and
  a download/encode that finishes after a selection change is neither cached nor
  served;
- invalidation: `install_image` (after select), `select_image`, `delete_image`
  (active and non-active), `clear_selection` all call `forget_image_cache()`;
  `reset_delivery_state()` clears it too.

No Redis, DB, bucket, worker, second cache service or new environment variable.
The durable object stays in the existing private bucket; a restart simply
empties the cache.

## 6. Storage download counts in the tests (before / after)

| Scenario | Before | After (measured) |
|---|---|---|
| `image_payload` + `telegram_photo` + `telegram_thumb` for one version | 3 downloads, 2 encodes | **1 download**, 1 encode each |
| Repeating all three | 3 more downloads | **0** |
| Six concurrent `telegram_photo(version)` | 6 downloads, 6 encodes | **1 download, 1 encode** |
| Concurrent payload + photo + thumb | 3 downloads | **1 download** |
| Rendering a panel (photo + thumb URLs) | 2 downloads + 2 encodes | **1 download + 2 encodes, then 0** |
| Uploads during rendering/cache fill | 0 extra | **0 extra** (asserted) |

## 7. Exact files changed

| File | Change |
|---|---|
| `backend/helper/panels.py` | `home_view` plumbed through `_render_and_edit`, `_replace_for_delivery`, `_handle_panel`, `_handle_navigation`, `_handle_action`, the router and `dispatch_click`; replacement now awaits readiness, keeps the panel on an unknown probe, and uses the caption-capability flag |
| `backend/services/appearance_service.py` | `desired_state`/`needs_new_message` content-independent (+ `caption_fits`), `fits_as_caption`, `delivery_unknown`/`_READY_ERROR`, bounded version-keyed single-flight cache (`IMAGE_CACHE_BUDGET_BYTES`, `_CACHE`, `_cache_lock`, `_original_locked`, `_rendition_locked`, `_remember`, `forget_image_cache`, `cached_image_bytes`), rewritten `image_payload` / `telegram_photo` / `telegram_thumb`, invalidation in the four selection-changing flows |
| `tests/test_panel_update_delivery.py` | NEW — 12 end-to-end tests through real dispatch/lifecycle/paint paths |
| `tests/test_appearance_image_cache.py` | NEW — 14 tests counting real Storage calls, concurrency, bounds and invalidation |
| `tests/test_appearance_delivery.py` | Updated the four assertions that pinned the old content-driven signature, and the paint-path test to declare `home_view=True` |
| `tests/test_64_ghost_seen_v2_tool_isolation_search.py` | Its `_render_and_edit` test double accepts the new keyword argument |
| `ROADMAP.md`, `INVESTIGATION.md`, `IMPLEMENTATION_REPORT.md` | Replaced in full with this verified state |

Not touched: gallery capacity/validation/upload bytes, Storage bucket and key
format, versioned image routes and cache headers, panel lifecycle, callback
routing/keyboards/callback data, the Helper Bot, `DATABASE_ARCHITECTURE.md`,
environment variables, AI/runtime/scheduler code.

## 8. Test results (real numbers from this tree)

| Check | Command | Result |
|---|---|---|
| New Part 1 suite | `pytest tests/test_panel_update_delivery.py -q` | **12 passed** |
| New Part 2 suite | `pytest tests/test_appearance_image_cache.py -q` | **14 passed** |
| Appearance delivery + gallery + UI | `pytest tests/test_appearance_delivery.py tests/test_appearance_gallery.py tests/test_appearance_ui.py -q` | **118 passed** |
| All five Appearance suites | `pytest tests/test_panel_update_delivery.py tests/test_appearance_image_cache.py tests/test_appearance_delivery.py tests/test_appearance_gallery.py tests/test_appearance_ui.py -q` | **144 passed** |
| Save / Model Tester / panel / lifecycle / inline / settings adjacency (18 suites) | `pytest tests/test_model_tester.py … tests/test_media_scope_and_delivery.py -q` | **661 passed** |
| Full suite | `pytest -q` | **7351 passed, 1 failed, 27 skipped** (143.90 s) |
| Compilation | `py_compile` on both changed modules + both new test files | exit `0` |
| Whitespace | `git diff --check` | exit `0` |

The single full-suite failure is the known, pre-existing
`tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`
(“interval advancement exceeded bound”), which also fails on the untouched base
tree; no scheduler file is in this diff. It is reported, not fixed here.

## 9. Review of the complete diff

- No code path creates a panel message for ordinary content: `replace_panel` is
  reachable only through `_replace_for_delivery` with `home_view=True`, an
  eligible panel and a real delivery-state difference.
- No second executor/sender: all paints still go through `_safe_edit` /
  `_present`; all replacements through `PanelLifecycleManager.replace_panel`.
- The cache never uploads, deletes or mutates the stored object, and every
  failure still returns `None` (the existing text fallback is unchanged).
- Dynamic operational content is still never composed with the Appearance
  header/page overrides (`_handle_action` returns the raw result).

## 10. Commit, push and remote verification

One commit on top of `f687e60`, subject
`fix: keep panel updates on one message and cache the selected image`. The
commit cannot contain its own SHA; the exact SHA and the fresh
`git fetch` → `rev-parse` → `merge-base --is-ancestor` verification are recorded
in the task's delivery message. Fast-forward delivery only: no rebase, no
force-push, no history rewrite, and no unrelated work discarded.

## 11. Not verified live

- No Telegram session, owner chat, Supabase credentials or Render deployment is
  available here, so no real panel was driven: the identity, transitions and
  caption behaviour are verified through the real code paths in tests, not by a
  live client.
- Supabase Storage latency and the cache's real-world hit rate were not
  observed; the tests count the actual calls, not wall-clock behaviour.
- Whether Telegram accepts a long main-page view as a replaced TEXT panel (and
  the exact client rendering) remains a production observation.
