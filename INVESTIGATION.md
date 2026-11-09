# INVESTIGATION — LifeOS menu stops opening after Appearance media delivery

_Investigated at production commit `98c8f368409a4ecce00a83eea18acb063fe88197`
(`main`, Render service **Self_bot**, deploy `dep-dbp0g6utlv9c73dto2m0`, live).
Every claim below comes from the deployed source, the installed Telethon TL
schema, and the recorded production exception — not from an earlier report._

## 1. Symptom (confirmed production evidence)

After the Appearance media-delivery deploy, repeated real `menu` inline requests
were logged as:

- `Inline router error for panel 'menu'`
- `telethon.errors.rpcerrorlist.PhotoThumbUrlEmptyError: The URL used as a
  thumbnail appears to be empty or has caused another HTTP error (caused by
  SetInlineBotResultsRequest)`
- the stack ending at `backend/helper/inline_engine.py::_inline_router` →
  `await event.answer(built)` → Telethon `events/inlinequery.py::answer`
- `trigger: helper returned zero results for query 'menu'`
- `create_panel: trigger returned success=False msg_id=0`
- `send_inline_panel ... success=False`

So the Helper Bot received the query and built a result, but Telegram refused
the `SetInlineBotResults` submission, the router answered with an EMPTY list, and
the self-bot saw zero inline results. The root menu became unavailable.

## 2. Confirmed root cause (exact field)

`backend/services/appearance_service.py::_as_photo_result()` built:

```python
types.InputBotInlineResult(
    id=..., type="photo", title=...,
    content=types.InputWebDocument(url=<jpeg url>, size=0,
                                   mime_type="image/jpeg", attributes=[]),
    send_message=types.InputBotInlineMessageMediaAuto(message=caption,
                                                      reply_markup=markup),
)
```

The installed Telethon schema (`telethon.tl.types.InputBotInlineResult`,
`telethon==1.43.0`) is:

```
inputBotInlineResult#88bf9319 flags:#
    id:string type:string
    title:flags.1?string description:flags.2?string url:flags.3?string
    thumb:flags.4?InputWebDocument
    content:flags.5?InputWebDocument
    send_message:InputBotInlineMessage = InputBotInlineResult;
```

`content` and `thumb` are **two separate web documents**. The Bot API's
equivalent (`InlineQueryResultPhoto`) likewise requires both `photo_url` and
`thumb_url`. `_as_photo_result()` supplied only `content`, so `thumb` was
absent — an empty thumbnail URL — and Telegram rejected the entire
`SetInlineBotResults` call with `PhotoThumbUrlEmptyError` before it ever fetched
the photo. The failure is in the TL result composition, not in the storage, the
image route, the gallery or the caption.

## 3. Why an Appearance failure emptied the whole menu

`_inline_router` treated any `event.answer()` exception as fatal for that query:

```python
except Exception as exc:
    logger.exception("Inline router error for panel '%s'", panel_id)
    record_inline_answer(panel_id, built, ok=False, ...)
    await event.answer([])          # zero results
```

`event.answer([])` is a VALID submission (the bot answered "nothing"), so the
self-bot's `getInlineBotResults` then returned zero results, `trigger()` failed,
and `create_panel` reported `success=False`. An optional presentation
enhancement therefore made the entire LifeOS menu unavailable — the actual
production regression, separate from the thumbnail bug that triggered it.

## 4. Corrective shape (what the code now does)

1. **Both documents.** `_as_photo_result()` now sets `content` and a separate
   `thumb`, each an `InputWebDocument` with `mime_type="image/jpeg"`. The photo
   keeps the existing `?fmt=jpeg` URL; the thumbnail points at the SAME
   versioned image route with `?fmt=jpeg&thumb=1`, which returns a bounded JPEG
   rendition (longest side ≤ 320 px, quality 80, never enlarged). No new storage
   object, bucket, route family or public surface was added, and the stored
   original bytes are never modified.
2. **Only attempt media that can be described completely.** The photo result is
   built only when preview is active, the selected object provably exists, the
   caption fits, and BOTH URLs are non-empty. Otherwise the panel is the normal
   text result.
3. **Narrow fallback, at the right boundary.** `_inline_router` preserves the
   panel's own text results BEFORE the optional media composition. If
   `event.answer(media)` is refused by a photo/media-specific RPC error, the
   router answers the SAME query again with those preserved text results
   (`no_webpage=True`, same inline keyboard, no Appearance URL) and records the
   answer as `ok=False` with `media delivery failed (<Type>)`. That is the only
   supported place to recover: a failed `SetInlineBotResults` stored nothing for
   the query, so the retry is simply the first successful answer, not a second
   answer to an already-answered query.
4. **Everything else keeps failing honestly.** Network timeouts, flood waits,
   auth errors and every other RPC error are NOT media rejections; the existing
   path (log, record `ok=False`, empty answer) is unchanged. No arbitrary
   exception is converted into a success.

## 5. Verified behaviour boundaries

- Normal text-only panels: unchanged (`no_webpage=True`, one answer).
- Preview OFF / no selected image / missing object / caption too long: unchanged
  text result, one answer, no URL.
- A successful photo answer never invokes the fallback.
- A media rejection produces exactly two `answer()` calls: the refused photo and
  then the text panel.
- The fallback caption/message text contains no `https://` and no
  `/appearance/`; both URLs exist only as API parameters of the web documents.
- The inline keyboard (Back/Home/Close, `panel:save`, callback data and styles)
  is the same object the panel builder produced, carried through unchanged.

## 6. Deliberately not changed

Gallery inventory, selection/deletion, the ten-image limit, private Supabase
Storage, upload semantics, the versioned image route's default original-bytes
response, the Open Graph page, preview enable/disable semantics, panel lifecycle
replacement, callback routing, owner guards, Helper-Bot-only editing, and the
single-persistent-panel architecture. No second Telegram client, scheduler,
panel registry, table or Render variable was added. No SQL was executed and
nothing in Supabase was modified.

## 7. Residual risk (not observable in this environment)

- This workspace has no Telegram session, no owner chat and no Render/Supabase
  credentials, so the fixed result was NOT submitted to Telegram here. The
  correction is verified against the installed TL constructor and by tests that
  drive the real router with the real `PhotoThumbUrlEmptyError`, not by a live
  inline query.
- Telegram must accept the `thumb` web document from the versioned image route;
  the route is tested to return a decodable JPEG for both `?fmt=jpeg` and
  `?fmt=jpeg&thumb=1`, but server-side fetching is only provable in production.
- If Telegram rejects the photo for a reason outside the recognised media-error
  set, the menu still fails through the existing (unchanged) failure path; that
  would be visible as the same `Inline router error` log without a
  `media result rejected ... answering with the preserved text panel` line.
