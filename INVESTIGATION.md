# INVESTIGATION — Appearance Preview Image (current state)

_Investigated and implemented at `a301e6422e4211e435b77a3b33224e10fce7c758`
(`main`, "Add Glass UI Appearance personalization with header, page text and
preview image"). Everything below was verified against the source at that
commit and against the installed dependencies, not against the previous report's
wording._

## 1. What existed before this change

`backend/services/appearance_service.py` stored ONE preview image:

- `bucket()` read `APPEARANCE_MEDIA_BUCKET` **or fell back to
  `ARCHIVE_MEDIA_BUCKET`**, so an unset Appearance bucket silently reused the
  Saved-Messages archive bucket.
- `install_image()` uploaded a new key and then **deleted the previous object**,
  so every "Replace" destroyed the only other image.
- `MAX_IMAGE_MB = 5` and `MAX_IMAGE_SIDE = 4096` — far below Telegram's own
  photo limits and the 25 MiB object ceiling already configured on the bucket.
- Validation was header-only (`sniff_image`): a truncated, mislabelled or
  decompression-bomb file with a plausible header was accepted.
- `backend/bot/handlers/appearance.py` called `event.download_media(file=tmpdir)`
  and then `Path(path).read_bytes()`, i.e. an **unbounded** download bounded only
  by the file size Telegram *reported* afterwards.
- The panel offered only Enable/Disable, Upload/Replace and Remove; there was no
  gallery, no per-image selection and no way to keep more than one image.

The image itself is served by `backend/web/app.py`:

- `/appearance/preview/<version>` — Open Graph HTML whose tap target is the
  existing `/guide/` site;
- `/appearance/image/<version>` — the stored bytes with an immutable one-year
  cache header.

Both routes derive everything from `preview_version()`, which is the selected
object key's stem. That contract is correct and was kept.

## 2. Installed dependency surface (verified, not assumed)

| Package | Version | Relevant API |
|---|---|---|
| `storage3` (via `supabase==2.4.2`) | 0.8.x | `storage.from_(bucket).list(path, {"limit", "offset", "sortBy"})` → `list[dict]` with `name`/`created_at`/`metadata.size`; `.upload(path, bytes, {"content-type", "upsert"})`; `.download(path)`; `.remove([path])` |
| `telethon` | 1.43.0 | `client.iter_download(file, *, offset, stride, limit, chunk_size, request_size, …)` — `limit` is a count of **chunks**, so a byte ceiling must be enforced by counting yielded bytes |
| `pillow` | 11.3.0 (added) | `Image.open`, `img.size`, `img.format`, `img.verify()`, `img.draft()`, `img.load()`; no image-decoding library was previously installed |

`storage3`'s `list()` merges caller options over its defaults and sets
`prefix` from `path`, so the gallery is discovered with a single call.

## 3. What was wrong, precisely

1. **No gallery inventory existed.** The bucket was write-only in practice: the
   application never listed objects, so it could not display, select, count or
   delete an individual image.
2. **Replacement destroyed images.** `install_image()` deleted the previous
   object after persisting the new key, which makes "keep up to 10" impossible.
3. **The archive bucket could receive Appearance images.** The fallback violated
   the "Appearance images never go to the archive bucket" boundary.
4. **The quality contract was too small.** 5 MiB / 4096 px discarded legitimate
   high-resolution uploads while the bucket itself accepts 25 MiB.
5. **Validation could not detect corruption.** Magic-byte parsing proves the
   header, not the image: truncated IDAT/entropy data and decompression bombs
   passed.
6. **The download had no transfer-time bound.** Telegram's reported size was
   trusted before the transfer, and the file was then read into memory whole.
7. **The failure mode of a missing selected object was silence.** A stale key
   still produced "stored: yes" with no way to see it or clear it.

## 4. What the new implementation does

- **Bucket** — `bucket()` returns `APPEARANCE_MEDIA_BUCKET` only; the archive
  fallback is gone. `storage_available()` still requires a configured DB.
- **Inventory** — `gallery()` lists `appearance/preview/` through the Storage
  API and returns `{items, known, count, error, selected, missing_selected}`.
  A listing failure returns `known=False` plus an error and is never rendered as
  "empty". Objects whose name is not a generated `appearance/preview/<32 hex>.<ext>`
  key (folder placeholders, nested paths, hand-uploaded files) are not gallery
  members.
- **Selection** — `select_image(token)` resolves the short token against the
  listing and writes only `appearance_preview_image_key`; an arbitrary
  callback-supplied key is never trusted.
- **Deletion** — `delete_image(token)` clears the selected key FIRST when the
  target is active (and aborts if that write fails), then removes exactly that
  one object. A refused Storage deletion is reported as a failure, with the
  cleared/kept state stated explicitly.
- **Clearing a stale reference** — `clear_selection()` drops the key without
  touching objects; the panel offers it whenever a stored key is not in the
  listing.
- **Upload** — serialized by one `asyncio.Lock` (single-instance runtime), so a
  count check cannot be raced past 10. Order: list → capacity check → validate
  (in a worker thread) → new unique object → re-list to confirm the write →
  persist the key → only on failure remove the new object. Existing objects are
  never deleted or evicted.
- **Validation** — Pillow identifies the real format; side, pixel and byte
  bounds are read before any decode; PNG is proved by walking its chunks and
  streaming IDAT through zlib in bounded pieces (CRC + exact decompressed
  length); JPEG is decoded at a reduced draft size; WebP is fully decoded only
  under the 12 Mpx budget, above which its RIFF container is checked.
- **Bounded transfer** — the handler streams `client.iter_download(media,
  chunk_size=256 KiB)` into a bytearray, stopping before a chunk would exceed
  25 MiB. No temporary file exists at all, so nothing can leak; the stream is
  closed on success, rejection and cancellation.
- **UI** — the Preview panel lists `N / 10`, marks the selected image with the
  registry glyph, gives every image a select and a delete button, hides the
  upload row at 10 and explains that nothing was evicted. Callback payloads stay
  ≤ 64 bytes (`action:appearance_select:<32 hex>`).
- **Routes** — unchanged contract: only the selected version is served, the
  bytes are the original upload, the content type comes from the bytes, the
  cache header stays immutable, and a superseded version 404s.

## 5. Supabase live state and the migration discrepancy

- Live project `telegram-self-bot` (`burfebhahuowywyfgcsc`), storage bucket
  **`appearance-assets`**: private, 25 MiB object ceiling, MIME allow-list
  `image/jpeg`, `image/png`, `image/webp`. Empty when created. **No SQL, bucket
  or migration action was performed by this change.**
- The five `panel_settings` columns already exist live:
  `appearance_header_title`, `appearance_header_body`,
  `appearance_page_overrides`, `appearance_preview_enabled`,
  `appearance_preview_image_key`.
- Remote migration history records `20261109164548` with name
  `20261109000000_add_panel_settings_appearance`.
- The repository file is `supabase/migrations/20261109000000_add_panel_settings_appearance.sql`.
  Its constraints contain `position(chr(0) in <column>) = 0`; PostgreSQL rejects
  `chr(0)` in text, and those predicates were omitted from the SQL that was
  applied live. The file's other statements (columns, defaults, backfill,
  NOT NULL, other constraints, singleton insert, `NOTIFY pgrst`) match the live
  schema.
- **This discrepancy remains open and was deliberately not touched**: renaming
  or rewriting `20261109000000_…` would desynchronise the recorded remote
  version, and "repair"/push/apply are out of scope for a code change. The
  gallery needs no new schema, so nothing is blocked by it. A future, separate
  migration task should reconcile the filename/version and the invalid `chr(0)`
  predicates; `DATABASE_ARCHITECTURE.md` was not modified.

## 6. Render

`APPEARANCE_MEDIA_BUCKET=appearance-assets` was already set on the `Self_bot`
service and its deployment was triggered before this task. **No Render
environment change was made here**, and the variable is read exactly as before
(`bucket()`), only without the archive fallback.

## 7. Residual risks

- 10 concurrent owners do not exist in a single-owner self-bot, so an
  `asyncio.Lock` is the correct scope; a multi-instance deployment would need an
  external lock and is out of scope.
- Supabase Storage `list()` is strongly consistent after `upload()`; the
  post-upload re-list is the verification step. A pathological eventual-
  consistency window would reject a valid upload (the object is removed) rather
  than keep an unverified one.
- Telegram decides its own preview rendering/zoom; only the original bytes and
  correct metadata are guaranteed here.
