# IMPLEMENTATION REPORT — Appearance Preview as panel media (no visible URL)

_Base commit: `9b66750d5330ef26742c995da5c42d30199c8351` (`main`). This document
replaces all earlier Appearance reports; only the current verified state is
described._

## 1. Verified root cause

`backend/services/appearance_service.py::with_preview()` appended
`preview_url()` — `https://<PUBLIC_BASE_URL>/appearance/preview/<version>`, an
HTML page, not the image — to the panel body. That function is the single
composition point for BOTH render boundaries
(`panels._apply_appearance` for callback paints and
`apply_to_inline_results` for the initial inline result), so the URL became part
of the message text everywhere. In addition
`backend/helper/panels.py::_appearance_link_preview()` returned
`preview_active()` and `_safe_edit()` forwarded it as `link_preview=True`
(`no_webpage=False`), explicitly asking Telegram to expand that link. Telegram
fetched `/appearance/preview/<version>`, whose Open Graph tags describe the
Render-served Guide page, and rendered the "Onrender" web-page card instead of
the selected image. The gallery, bucket, versioned image route and selection
logic were all correct; the defect was purely the delivery representation.

## 2. Implementation and files changed

| File | Change |
|---|---|
| `backend/services/appearance_service.py` | `with_preview()` no longer composes any URL; new media delivery layer: `caption_for`, `telegram_photo_url`, `delivery_ready` (bounded, 30 s cached prefix listing), `note_built`/`claim_built`, `note_delivered`/`delivered_state`/`forget_delivered`, `desired_state`/`needs_new_message`, `reset_delivery_state`; `apply_to_inline_results` is now async and rebuilds eligible results as photo results via `_as_photo_result`; `telegram_photo` + `_encode_telegram_photo` (JPEG rendition, cap-aware) |
| `backend/helper/inline_engine.py` | awaits the now-async appearance composition |
| `backend/helper/panels.py` | `_appearance_link_preview` returns `False` for every customizable main page (never a card, in either state); `_render_and_edit` calls `_replace_for_delivery` and stops painting the old message when a replacement happened |
| `backend/helper/lifecycle.py` | `create_panel` records the state the inline builder actually produced; new `replace_panel` (send new → register session/timer/state → release old → delete old message) that never destroys the current panel before a replacement exists; `_cleanup_state_locked` forgets a panel's delivery record |
| `backend/web/app.py` | `/appearance/image/{version}` accepts `?fmt=jpeg` and returns the on-demand JPEG rendition; the default still returns the original bytes |
| `tests/test_appearance_delivery.py` | NEW: 20 tests driving the real builder, panel paint boundary, lifecycle replacement and both image routes |
| `tests/test_appearance_ui.py` | inline-composition tests updated to the media contract (awaited builder, photo result, no URL) |
| `AGENTS.md` | §5 Appearance description updated to the media-delivery architecture |
| `INVESTIGATION.md`, `IMPLEMENTATION_REPORT.md` | replaced in full |

No SQL, migration, Supabase object, bucket, Render variable, gallery semantic,
panel registry, unrelated handler or AI/runtime file was touched.

## 3. How the image now reaches Telegram

The initial panel is an inline result, so delivery is decided there:
`apply_to_inline_results` composes the page text, then — only when preview is on,
the selected object provably exists, and the text fits Telegram's caption limit —
returns a rebuilt

```
InputBotInlineResult(type="photo",
    content=InputWebDocument(url="<base>/appearance/image/<version>?fmt=jpeg",
                             mime_type="image/jpeg"),
    send_message=InputBotInlineMessageMediaAuto(message=<panel text>,
                                                reply_markup=<unchanged rows>))
```

Telegram fetches the rendition itself and sends it as the message's PHOTO. The
rendition is encoded from the stored object in a worker thread (`quality=90`,
alpha flattened on white, progressive downscale only when it would exceed the
inline-photo fetch limit), so the stored object is untouched and the private
bucket still has no public policy: the only public surface is the existing
versioned image route. Stale versions 404 there, so a superseded selection can
never be served.

## 4. How the text and the inline keyboard are preserved

- The composed panel text is passed verbatim as the media caption — the same
  `_compose_rendered(title, body)` string the text panel used, including header
  and page overrides.
- Above the caption limit the result stays a text result carrying the FULL text
  (`no_webpage=True`); nothing is truncated and no URL is added.
- `reply_markup` is the very object the panel builder produced (Back/Home/Close,
  styles, callback data), so navigation, registration and owner guards are
  unchanged. `inline_engine._sanitize_results` still normalises those rows.
- Sub-views (`extra != ""`), action results and confirmations are composed
  exactly as before and are never rewritten.

## 5. How URL / web-page-preview leakage is prevented

1. No URL is ever composed into panel text: `with_preview()` is a pure text
   pass-through and the only URL used is an API parameter of the photo document.
2. The image URL is the JPEG image route, never the Open Graph page.
3. `_appearance_link_preview()` returns `False` for every customizable main page,
   so `no_webpage=True` is sent in both the media and the text case; Telegram is
   never asked to expand anything.
4. A text fallback (caption too long, preview off, no image, missing object) is
   still text with previews suppressed, so it cannot produce a card either.
5. Tests assert `"https://"`/`"/appearance/"` are absent from every caption and
   from every text fallback, and that no `no_webpage=False` is requested.

## 6. Media/text transitions and panel lifecycle

Telegram cannot turn a photo message back into text, nor swap its photo without
an upload, so a state change needs a new message:

- `desired_state()` is `("media", version)` or `("text", "")` from current
  settings plus the bounded readiness check; `needs_new_message()` compares it
  with the state recorded for that `(chat_id, msg_id)` — recorded at creation
  from what the builder really produced, and updated on replacement.
- `panels._render_and_edit` performs the replacement only for the HOME view of an
  eligible page (`extra == ""`) and never for sub-views.
- `PanelLifecycleManager.replace_panel` sends the new inline result FIRST,
  registers its session/auto-close timer/state, releases the old session, and
  only then deletes the old message. A failed send (or a returned id equal to the
  old one) leaves the existing panel and its session untouched.
- `_cleanup_state_locked` forgets the delivery record, so close/timeout leaves no
  stale state behind; the registry is bounded (256 entries).
- There is still exactly ONE persistent panel per chat: the replacement is
  created before the old message is deleted, and no second panel path was added.

## 7. Focused test results

```
tests/test_appearance_delivery.py + tests/test_appearance_ui.py + tests/test_appearance_gallery.py
  111 passed in 1.35s
```

New coverage (20 tests) exercises the real delivery path: photo inline result
type/content/`send_message` class, JPEG rendition URL and MIME, the selected
image (not the previous one) being the one handed to Telegram, caption content
with no URL, keyboard rows identical to the text result, full-text fallback above
the caption limit, disabled preview, missing selected object, the JPEG route
(`content-type`, decodable JPEG, immutable cache, original route byte-identical),
stale-version 404, unreadable object → no rendition, switching image marking the
message stale, disabling returning to text, unknown old panel replaced once,
sub-views/other panels never replaced, the paint path replacing instead of
editing, `create_panel` recording the built state, replacement deleting the old
message only after success, a failed replacement keeping the panel and session,
close forgetting the state, and the gallery panel never showing a URL.

## 8. Regression results

```
-k "appearance or panel or lifecycle or inline or settings or symbol or private_message"
  965 passed, 3 skipped in 14.43s

PYTHONPATH=. python -m pytest tests -q -p no:cacheprovider
  1 failed, 7318 passed, 27 skipped in 141.50s
```

The single failure is
`tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`.
It was reproduced on the untouched base commit `9b66750` in a clean worktree
(`1 failed in 0.83s`), so it is pre-existing and unrelated: the scheduler, task
execution and AI modules are not part of this diff.

## 9. Compilation and diff check

```
python -m py_compile backend/services/appearance_service.py backend/helper/inline_engine.py \
  backend/helper/lifecycle.py backend/helper/panels.py backend/web/app.py \
  tests/test_appearance_delivery.py tests/test_appearance_ui.py
  COMPILE_OK
git diff --check
  clean
```

## 10. Commit and push

One commit, subject
`fix: deliver the Appearance preview as panel media, never a visible URL`.
The exact SHA is `HEAD` of `main` after this change; the push result is recorded
in the delivery message for this task.

## 11. Verified remote main HEAD

Verified after pushing with `git fetch origin main`, `git rev-parse HEAD`,
`git rev-parse FETCH_HEAD`, `git merge-base --is-ancestor HEAD FETCH_HEAD` and
`git ls-remote origin refs/heads/main`; all four agreed. See the delivery
message for the SHA.

## 12. Live verification status and remaining limitations

- **Not live-verified.** This environment has no Helper Bot session, owner chat
  or Telegram client, so the rendered panel was not observed. The change is
  code- and test-verified only; the screenshot-level result must be confirmed in
  the running bot.
- Telegram's acceptance of a `photo` inline result whose `InputWebDocument` is
  `…?fmt=jpeg` is the documented inline-photo shape but unproven here. If it were
  refused, the panel keeps its previous message (the answer failure is logged)
  and never falls back to a URL.
- Editing a photo panel on later paints relies on Telegram treating
  `messages.editInlineBotMessage` without `media` as a caption edit; a refusal is
  logged by `_safe_edit` and cannot reintroduce a link.
- The `<version>?fmt=jpeg` rendition is capped for Telegram's inline-photo fetch
  (quality 90, then downscale); extremely large photographs may therefore be
  delivered at a reduced size, while the stored original and
  `/appearance/image/<version>` remain untouched at full quality.
- `_DELIVERED` is process-local; after a restart the first render of an existing
  media panel replaces it once, then behaves normally.
