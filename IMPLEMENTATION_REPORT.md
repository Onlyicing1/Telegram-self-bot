# IMPLEMENTATION REPORT — Appearance media delivery repaired (inline photo thumbnail + safe text fallback)

_Base commit: `98c8f368409a4ecce00a83eea18acb063fe88197` (`main`, the live
Render deploy `dep-dbp0g6utlv9c73dto2m0`). This document replaces all earlier
reports; only the current verified state is described._

## 1. Exact root cause

`backend/services/appearance_service.py::_as_photo_result()` returned an
`InputBotInlineResult` of `type="photo"` with a `content` web document but NO
`thumb` web document. In Telethon's installed TL schema the two are separate
optional fields:

```
inputBotInlineResult#88bf9319 flags:#
    id:string type:string title:flags.1?string description:flags.2?string
    url:flags.3?string thumb:flags.4?InputWebDocument
    content:flags.5?InputWebDocument send_message:InputBotInlineMessage;
```

Telegram validated the missing thumbnail first and answered
`SetInlineBotResultsRequest` with `PhotoThumbUrlEmptyError`. `_inline_router`
caught it, logged `Inline router error for panel 'menu'`, and submitted
`event.answer([])`. The Helper Bot therefore genuinely stored ZERO results, the
self-bot's inline query returned none, `create_panel` failed, and the LifeOS menu
could not open. Optional Appearance media had become a hard dependency of the
entire menu.

## 2. Exact correction

### 2.1 The TL result now carries both documents

```python
types.InputBotInlineResult(
    id=..., type="photo", title=...,
    content=types.InputWebDocument(url="<base>/appearance/image/<v>?fmt=jpeg",
                                   size=0, mime_type="image/jpeg", attributes=[]),
    thumb=types.InputWebDocument(url="<base>/appearance/image/<v>?fmt=jpeg&thumb=1",
                                 size=0, mime_type="image/jpeg", attributes=[]),
    send_message=types.InputBotInlineMessageMediaAuto(message=<caption>,
                                                      reply_markup=<same rows>),
)
```

- `thumb` is the field Telegram requires for an inline photo result; it is no
  longer empty.
- The thumbnail is served by the EXISTING versioned route with `thumb=1`, which
  returns a bounded JPEG rendition (longest side ≤ 320 px, quality 80, no
  upscaling). The photo URL is the existing `?fmt=jpeg` rendition. Neither
  creates a storage object, bucket, credential or new public surface, and the
  stored original bytes stay untouched.
- The photo result is only produced when preview is active, the selected object
  provably exists, the caption fits, and both URLs are non-empty; otherwise the
  normal text result is used.

### 2.2 A rejected media result can no longer empty the menu

`backend/helper/inline_engine.py::_inline_router` now:

1. sanitizes the builder's results and keeps that TEXT list BEFORE the optional
   Appearance composition;
2. attempts the composed answer (photo or text);
3. if and only if a photo answer was attempted and Telegram raised a
   photo/media-specific error (`PhotoThumbUrlEmptyError`,
   `PhotoContentUrlEmptyError`, `PhotoContentTypeInvalidError`,
   `PhotoExtInvalidError`, `PhotoInvalidDimensionsError`,
   `PhotoSaveFileInvalidError`, `PhotoFileMissingError`, `PhotoInvalidError`,
   `MediaEmptyError`, `MediaInvalidError`, `WebpageMediaEmptyError`,
   `MediaCaptionTooLongError`, or an `RPCError` whose text names a photo/thumb),
   answers the SAME query again with the preserved text results, with
   `no_webpage=True` and the same keyboard;
4. logs `[INLINE] media result rejected for panel '<id>' (<Type>) — answering
   with the preserved text panel` (type only, no caption/content), and records
   the answer as `ok=False` with `media delivery failed (<Type>); the panel was
   delivered as text` — the fallback is never reported as image delivery;
5. leaves all other failures on the existing honest path (log, `ok=False`,
   empty answer). Unrelated RPC errors are not retried or converted to success.

Retry safety: a failed `SetInlineBotResults` stores nothing for that query, so a
second `answer()` with a different payload is the first successful submission,
not a second answer to an answered query.

## 3. Files changed

| File | Change |
|---|---|
| `backend/services/appearance_service.py` | `_as_photo_result()` sets `thumb`; `telegram_thumb_url()`; `contains_photo_result()`; `_encode_telegram_thumb()` + `telegram_thumb()` (bounded 320 px JPEG delivery rendition); photo guarded on both URLs |
| `backend/helper/inline_engine.py` | `is_media_result_rejection()` (narrow set), `text_fallback_results()`, `_inline_router` preserves text results and retries with them only for media rejections; records the fallback as `ok=False` |
| `backend/web/app.py` | `/appearance/image/{version}` accepts `thumb=1` alongside `fmt=jpeg` and serves the bounded thumbnail rendition |
| `tests/test_appearance_delivery.py` | NEW regression coverage: thumbnail document fields, both routes returning valid JPEGs and leaving the original untouched, thumbnail absent from captions, real-router fallback on `PhotoThumbUrlEmptyError`, successful photo path, unrelated-error path, preview-disabled text path |
| `INVESTIGATION.md`, `IMPLEMENTATION_REPORT.md` | replaced in full with this verified state |

Not touched: gallery/upload/selection/deletion semantics, the ten-image limit,
private Storage configuration, `DATABASE_ARCHITECTURE.md`, panel lifecycle,
callback routing, owner guards, AI/runtime/scheduler code, and every Render
environment variable.

## 4. Preserved behaviour

- Normal text-only inline results and all non-Appearance panels: unchanged.
- Root menu rendering and callback routing, Back/Home/Close rows, callback data.
- Appearance header/page text customization and its single composition path.
- Preview enable/disable and the caption-limit text fallback.
- Gallery selection, deletion, ten-image limit, versioned image endpoints.
- Helper-Bot-only editing and the single persistent panel per chat.

## 5. Test results (real numbers from this tree)

| Check | Command | Result |
|---|---|---|
| Focused Appearance suites | `pytest tests/test_appearance_delivery.py tests/test_appearance_ui.py tests/test_appearance_gallery.py -q` | **118 passed** |
| Adjacent panels/lifecycle/inline/settings | `pytest tests -q -k "appearance or panel or lifecycle or inline or settings or symbol"` | **768 passed, 3 skipped** |
| Full suite | `pytest tests -q` | **7325 passed, 27 skipped, 1 failed** (142.26 s) |
| Compilation | `py_compile` on the three changed modules + the changed test file | exit `0` |
| Whitespace | `git diff --check` | clean |

New coverage asserts, in particular:

- the photo result has `type == "photo"`, an `InputWebDocument` content and a
  DIFFERENT `InputWebDocument` `thumb` whose URL is
  `…/appearance/image/<version>?fmt=jpeg&thumb=1` with `image/jpeg`;
- both URLs resolve through the existing FastAPI route to decodable JPEGs, the
  thumbnail is capped at 320 px on its longest side, and the original-bytes route
  is still byte-identical;
- no media URL appears in the caption;
- a real `PhotoThumbUrlEmptyError` from `event.answer` results in exactly two
  answers: the refused photo, then the text panel with its original text,
  `no_webpage=True`, the same keyboard, and no URL — and `last_inline_answer`
  reports `ok=False` plus the media-fallback reason, while the log carries the
  error type but not the panel text;
- an accepted photo answer invokes `answer` once and never the fallback;
- an unrelated `TimeoutError` is not treated as a media rejection (empty answer,
  `ok=False`, no fallback claim);
- preview disabled still yields exactly one text answer with previews suppressed.

The single full-suite failure is the known, pre-existing
`tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`
(“interval advancement exceeded bound”), also failing on the untouched base
commit in the previous report's clean-worktree baseline. No scheduler file is in
this diff.

## 6. Commit, push and remote verification

One commit on top of `98c8f36`, subject
`fix: supply the inline photo thumbnail and keep the menu on media failure`.
The exact SHA and the push/fetch/`ls-remote` agreement are recorded in the
delivery message for this task; `main` was fast-forwarded only (no rebase, no
force-push, no history rewrite), and the working tree is clean.

## 7. Live verification status

- **Not deployed or live-verified from this environment.** No Render API
  key/service id/deploy hook, no Supabase credentials and no Telegram session are
  present here, so no deployment was triggered and no real `menu` inline query
  could be observed.
- The deploy `dep-dbp0g6utlv9c73dto2m0` that produced the evidence is still the
  live one; the fix must be deployed before any production claim.
- To verify in production: query `menu`, confirm the bot's answer no longer
  raises `PhotoThumbUrlEmptyError`, `trigger` returns a nonzero `msg_id`, and the
  panel message exists. If Telegram accepts the photo, confirm the image is the
  selected one; if not, expect the honest
  `media result rejected ... answering with the preserved text panel` log and a
  working text panel — which is a fallback, NOT successful image delivery.
