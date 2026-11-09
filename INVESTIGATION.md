# INVESTIGATION — Panel updates rebuilt the menu, and every image request re-downloaded

_Base commit: `f687e601e9f91e9cca0a88e70166614358732997` (`main`). Every claim
below comes from this tree's source and from deterministic tests that drive the
real dispatch, lifecycle, render and delivery paths._

## 1. Symptom

After the Appearance preview became the panel's **media**, the image correctly
appeared and stayed attached while navigating categories. But operations that
repaint the current panel several times — Save, the Model Tester, progress and
status updates — sometimes produced a **new panel message** instead of updating
the existing one. The section or operation then failed to continue: it kept
editing the message that had just been replaced/deleted, while the owner saw the
panel's home view (the root category) instead of the result or progress.

## 2. Confirmed root cause — the replacement decision took the paint's TEXT

`backend/helper/panels.py::_render_and_edit` called `_replace_for_delivery(...)`
for **every** paint, before editing. `_replace_for_delivery` gated on two
things only:

1. `appearance_service.is_eligible(panel_id)` — the current panel id;
2. the session's current nav `extra == ""`.

An **action** result satisfies both: `_handle_action` returns
`(current_panel, result)` from the session's nav (a main page with empty extra),
so a Save result, a Model Tester progress view or a status line was evaluated as
if it were the customizable home view.

The decision itself came from `appearance_service.needs_new_message(chat, msg,
title, body)`, whose desired state was derived from the **paint's text**:

```python
caption = caption_for(_compose_rendered(title, body))   # "" when empty or > 1024
if preview_active() and _READY_VALUE and caption:
    return ("media", preview_version())
return ("text", "")
```

So for a panel message already delivered as `("media", <version>)`:

- a paint whose text was **empty or longer than Telegram's 1024-character
  caption limit** (a Save metadata block, a terminal test-results view, a
  combined header + live body) computed `("text", "")`;
- `delivered != desired` → `replace_panel(panel_id, "")`;
- `replace_panel` creates a **new inline panel** (the panel's home view) and
  then deletes the old message.

Two further facts made it worse:

- `_READY_VALUE` (readiness of the selected object) was only ever computed by
  the inline build; the edit path never probed it, so a panel repaint in a fresh
  process could compute `("text","")` while the message was genuinely media.
- "content changed" and "delivery changed" were therefore the **same input**:
  any ordinary content change whose text could not be a caption was read as a
  media transition.

## 3. What was ruled out

- **Not** a Save/Model Tester defect: both edit `(chat_id, msg_id)` through
  `_safe_edit` / `_edit_panel_message`; neither asks for a replacement.
- **Not** a lifecycle defect: `replace_panel` already creates first and deletes
  the old message only after the new one exists, and a failed replacement leaves
  the old panel intact.
- **Not** a keyboard/nav defect: Back/Home/Close and callback data are carried
  through unchanged.
- **Not** the Appearance composition path: `_handle_action` never applies page
  overrides/header to a result (that boundary was already correct).

## 4. The second defect — repeated Storage work per request

`backend/services/appearance_service.py` downloaded and encoded independently on
every call:

| Call | Storage download | Encoding |
|---|---|---|
| `image_payload(version)` | yes | — |
| `telegram_photo(version)` | yes | JPEG photo |
| `telegram_thumb(version)` | yes | JPEG thumbnail |

The photo and thumbnail paths each downloaded the same stored object, and a
panel open (photo + thumbnail URLs) therefore cost two downloads plus two
encodes every time. Nothing was cached between requests.

## 5. Corrective shape

1. **Delivery identity only.** `desired_state()` no longer accepts the view's
   text. It is `("media", <selected version>)` when the preview is active, the
   selected version exists and the object is genuinely deliverable, and
   `("text", "")` otherwise. `needs_new_message()` compares that identity with
   the state recorded for the message. Ordinary content can never move it.
2. **Operational paints are not eligible.** `_render_and_edit` takes
   `home_view`; the router derives it from the paint (`_handle_panel`:
   `not extra`; `_handle_navigation` home/back; `_handle_action`: `False`). Only
   a main page's own home view may transition.
3. **Readiness is proven, and an unknown answer changes nothing.** The
   replacement path awaits `delivery_ready()`; a probe that FAILED sets
   `delivery_unknown()` and the existing panel is kept.
4. **Caption capability, not caption diff.** `fits_as_caption(title, body)`
   answers whether a photo message can hold this view at all; only then may the
   home view transition to text (the full text is preserved, and the
   replacement renders the intended page, never the root menu).
5. **Bounded, version-keyed, single-flight cache** for the selected image's
   original bytes and both renditions, with invalidation on every selection
   change.

## 6. Verified boundaries (tests)

- Action paints on a photo panel: no inline panel created, no message deleted,
  session stays on the same message.
- Save result on the Save panel: same message, Back/Home/Close intact, and the
  result text contains no menu body.
- Model Tester launch + three progress edits: one message, no replacement.
- Category navigation and Back/Home: no replacement when only the view changed.
- Version change / preview off / preview on: exactly one replacement, old
  message deleted only after the new one exists.
- Failed replacement: attempted once, old message kept and painted.
- Cache: first request 1 download; photo+thumb+payload 1 download and one encode
  each; six concurrent photo requests 1 download and 1 encode; no uploads during
  delivery; selection/upload/delete/clear all drop the previous bytes.

## 7. Deliberately not changed

Panel lifecycle and callback routing, the single-writer painter, keyboards and
callback data, gallery capacity and validation, upload bytes/quality, the
Storage key format, the versioned image routes and their cache headers,
`DATABASE_ARCHITECTURE.md`, environment variables, and every AI/runtime path.
No new Telegram client, scheduler, executor, listener, table, bucket or service
was added; no SQL was executed and nothing in Supabase was modified.

## 8. Not observable here

No Telegram session, owner chat, Supabase credentials or Render deployment is
available in this workspace, so no live panel was driven and no real Storage
object was fetched. The correction is verified by tests that drive the real
dispatch/lifecycle/render/delivery code and count the real Storage calls, and by
source inspection of the installed Telethon behaviour — not by a live client.
