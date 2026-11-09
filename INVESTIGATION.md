# INVESTIGATION — Appearance Preview Delivery (visible URL / "Onrender" card)

_Investigated and fixed at `9b66750d5330ef26742c995da5c42d30199c8351` (`main`,
after the ten-image gallery change). Every claim below is derived from the
source and the Telegram objects the code builds, not from the previous report._

## 1. Symptom

With a gallery image selected and Preview ON, the LifeOS main panel showed:

- the Render URL (`https://<service>.onrender.com/appearance/preview/<version>`)
  as part of the message body — visible text;
- Telegram's own web-page preview card above it, titled **Onrender**;
- the Guide page's title/description — i.e. the whole webpage, not the image.

The Appearance panel itself was correct (`Preview: ON`, `Images: 1 / 10`,
`Selected: image 1`), so the defect is in DELIVERY, not in the gallery.

## 2. Verified root cause (exact code)

1. `backend/services/appearance_service.py::with_preview()` composed the panel
   title/body and then did:

   ```python
   url = preview_url() if preview_active() else ""
   if url:
       body = f"{body}\n\n{url}" if body else url
   ```

   `preview_url()` returns `https://<PUBLIC_BASE_URL>/appearance/preview/<version>`,
   i.e. the public HTML page — **not** the image route.

2. `with_preview()` is the single composition function used by BOTH panel
   render boundaries:
   - `backend/helper/panels.py::_apply_appearance()` for every callback paint,
   - `backend/services/appearance_service.py::apply_to_inline_results()` for the
     initial inline panel result.

   So the URL was inserted into the message text on every path, including the
   `send_message.message` of the inline result that becomes the panel message.

3. `backend/helper/panels.py::_appearance_link_preview()` returned
   `preview_active()` for eligible main pages. `_safe_edit()` forwards that as
   `link_preview=…`, and Telethon maps `link_preview=True` to
   `no_webpage=False`. The code therefore **explicitly asked Telegram to expand
   the URL**, guaranteeing the web-page card.

4. `/appearance/preview/<version>` (`backend/web/app.py::appearance_preview`)
   serves an Open Graph HTML page whose `og:title`/`og:description` come from the
   Appearance header text and whose tap target is `/guide/`. That is exactly the
   card Telegram built — including the host name ("Onrender") when no header was
   customised.

Net effect: the owner's image was never part of the message; only a link to an
HTML page was. The gallery, the bucket, the versioned image route and the
selection semantics were all working.

## 3. Why "just remove the URL" is not enough

Removing the appended link alone yields a text-only panel — the requirement is
that the selected image is visibly part of the message. Telegram has no API that
turns a text message into a media message in place, so the delivery mechanism
itself had to change.

## 4. Delivery mechanism now used, and why

The panel message is created by the owner's account from the **Helper Bot's
inline result** (`inline_engine.trigger` → `result.click(chat_id)`), and every
later paint edits that same inline message
(`CallbackQuery.Event.edit` → `messages.editInlineBotMessage`). Telegram's own
shape for "inline result that becomes a photo with a caption and buttons" is:

```
InputBotInlineResult(
    id=…, type="photo",
    content=InputWebDocument(url=<image URL>, mime_type="image/jpeg"),
    send_message=InputBotInlineMessageMediaAuto(message=<text>, reply_markup=<same rows>),
)
```

which is the raw-API equivalent of the Bot API's `InlineQueryResultPhoto`. The
Telegram server fetches the photo URL itself and sends it as message media; the
text becomes the caption; there is no textual link and no place for a web-page
card. Verified against the installed Telethon constructors
(`InputBotInlineResult`, `InputWebDocument`, `InputBotInlineMessageMediaAuto`
all exist with those fields).

Consequences that shaped the implementation:

- **Photo URL must be a JPEG document.** `/appearance/image/<version>` still
  serves the owner's ORIGINAL bytes (PNG/JPEG/WebP, up to 25 MiB). The panel's
  media therefore points at the same route with `?fmt=jpeg`, which encodes a
  delivery rendition on demand (`quality=90`, progressive downscale only if the
  result exceeds Telegram's inline-photo fetch limit). Stored bytes are never
  modified.
- **A caption is capped (1024 characters).** A composed panel above that limit
  is delivered as TEXT with `no_webpage=True`, so the full text is preserved and
  no card appears; it never silently truncates and never inserts a URL.
- **Media and text are mutually exclusive for one message.** Telegram cannot
  remove a photo from a message and cannot exchange one without uploading it, so
  enabling, disabling or switching the selected image requires a NEW inline
  result. This is handled by `PanelLifecycleManager.replace_panel()`: the
  replacement is created first and the old message is deleted only after the new
  one exists (a failed replacement leaves the current panel intact).
- **A missing object must not become a broken photo.** The builder lists the
  gallery prefix before claiming media (`delivery_ready()`), so a stale key
  degrades to a text panel instead of Telegram fetching a 404 as the photo.

## 5. What was deliberately not changed

- Gallery inventory, uploads, selection, deletion, bucket and prefix.
- `/appearance/preview/<version>` and `/appearance/image/<version>` (still
  versioned, still immutable-cached, still the Open Graph page for sharing).
- Panel architecture: same inline engine, same lifecycle, same callback
  registries, same Helper-Bot-only editing, same nav buttons.
- No new Telegram client, listener, loop, table, column, migration or Render
  variable.

## 6. Residual risk (cannot be verified from this environment)

- Telegram's server-side behaviour for a photo inline result whose
  `InputWebDocument` URL serves `?fmt=jpeg` is documented and widely used, but
  this sandbox has no live Helper Bot/owner chat, so the rendered result was not
  observed. If Telegram refused that content, the panel would keep its previous
  text form; the code logs the answer failure and never falls back to a URL.
- Editing a photo inline message by sending only `message`/`reply_markup` (no
  `media`) is Telegram's caption-edit path; a refusal is logged by `_safe_edit`
  and cannot introduce a link, because the text no longer contains one anywhere.
- The delivery-state registry (`_DELIVERED`) is process-local and bounded; after
  a restart the first render of an existing media panel replaces it once.
