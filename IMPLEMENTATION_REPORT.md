# IMPLEMENTATION REPORT — Appearance Preview Image Gallery

_Base commit: `a301e6422e4211e435b77a3b33224e10fce7c758` (`main`). This document
replaces all earlier Appearance/Delete reports; only the current state is
described._

## 1. Objective and implementation summary

The Appearance → Preview Image feature stored exactly one image and destroyed it
on every replacement. It is now a gallery of up to **10 original images** kept
in the existing private Supabase Storage bucket, with per-image selection and
deletion, an independent enable switch, and upload paths that keep the owner's
bytes exactly as sent while enforcing 25 MiB, 8192 px per side and 50 megapixels.

Highlights:

- the bucket is `APPEARANCE_MEDIA_BUCKET` only (`appearance-assets`); the archive
  fallback is gone;
- inventory is the Storage listing of `appearance/preview/` — no new table, no
  new column, no second settings system;
- only the selected object's key lives on the existing singleton settings row;
- uploads never resize, recompress, convert or strip metadata, and never evict
  an existing image;
- selection writes only the key; deletion removes exactly one object;
- new uploads are validated by decoding (Pillow), not by header guessing, and
  the Telegram download is capped while it streams.

## 2. Exact files changed

| File | Change |
|---|---|
| `backend/services/appearance_service.py` | Gallery listing (`gallery`), token resolution, `select_image`, `delete_image`, `clear_selection`, serialized `install_image`, 25 MiB/8192 px/50 Mpx bounds, Pillow-backed `validate_image` with bounded PNG/JPEG/WebP integrity proofs, dedicated bucket lookup (no archive fallback), extended `preview_status` |
| `backend/bot/handlers/appearance.py` | Gallery panel (`N / 10`, selected mark, select/delete rows, full-gallery state, stale-selection clear), new `appearance_select` / `appearance_delete` / `appearance_clear_selection` actions, bounded `iter_download` streaming upload (no temp file, no `read_bytes`) |
| `backend/requirements.txt` | `pillow==11.3.0` (the only image decoder; no second engine) |
| `tests/test_appearance_ui.py` | Updated where the old contract changed (bucket name, 25 MiB/8192 px limits, upload retention, delete-by-token, bounded-download event) |
| `tests/test_appearance_gallery.py` | New suite: gallery inventory, selection, deletion, failures, upload consistency, quality/bounds, bounded download, preview routes, panel rendering |

No SQL, no migration file, no bucket, no Render variable, no AI/runtime file and
no unrelated panel was touched.

## 3. Gallery behaviour implemented

- Every object under `appearance/preview/` whose name is a generated
  `<32 hex>.<png|jpg|webp>` key is listed (newest first), with its size.
- The selected image is marked (registry glyph) in the text and on its own
  select button; at most one image is active.
- Selecting an inactive image changes **only** `appearance_preview_image_key`:
  no upload, no copy, no deletion.
- Deleting an inactive image leaves the selection untouched; deleting the active
  image clears the key first and then removes that object; nothing else is
  affected.
- Disabling preview keeps every object and the key; re-enabling reuses it.
- A successful upload selects the new image and keeps every previous one.
- At 10 images a further upload is refused with the exact count and the
  instruction to delete one first; nothing is evicted. After one deletion an
  upload is accepted again.
- A listing failure is reported as an unreadable gallery (`known=False`), never
  as "0 / 10"; upload is refused while the count cannot be established.
- A selected key whose object is gone is reported as inconsistent; the owner can
  select another retained image or clear the selection without deleting images.
- Callback payloads carry a 32-character token resolved against a fresh listing,
  so an arbitrary Storage path from a callback can never be selected or deleted.
- Pending input still uses the existing single listener; no client, listener,
  loop, scheduler or second panel message was added.

## 4. Image quality and bounded download

- Limits: 26,214,400 bytes (the bucket's own ceiling), 8192 px per side,
  50,000,000 decoded pixels, formats JPEG/PNG/WebP.
- Format is taken from the decoded content; a declared `image/png` carrying JPEG
  bytes is refused, `image/jpg` is normalised to `image/jpeg`, and an unknown or
  absent declaration is decided by the content.
- Validation happens in a worker thread (`asyncio.to_thread`), so decoding never
  blocks the event loop.
- Bounds are read from the header before any decode. PNG is then proved by
  walking every chunk (CRC) and streaming IDAT through zlib in 64 KiB pieces with
  a 1 MiB output cap, comparing the decompressed length with the header's
  promise — truncated, empty or corrupt pixel streams fail, and a compressed
  bomb cannot expand unboundedly. JPEG is verified and then decoded at a reduced
  draft size (≤1 Mpx). WebP is decoded only under a 12 Mpx budget, above which
  its RIFF container length and chunk structure are checked.
- The upload is streamed through `client.iter_download(media,
  chunk_size=256 KiB)`; the handler stops before a chunk would exceed 25 MiB, so
  the reported Telegram size is irrelevant. Nothing larger than the cap is ever
  buffered and **no temporary file or directory is created**, so success,
  rejection, exception and cancellation all leave nothing behind (the iterator
  is closed in `finally`).
- Bytes are uploaded exactly as received: no resize, recompression, conversion,
  thumbnail, metadata rewrite or truncation.

## 5. Storage architecture and consistency guarantees

- Bucket: `APPEARANCE_MEDIA_BUCKET` only; private; access only through the
  service-role client. The archive bucket/Saved Messages are not used.
- Selected key format: `appearance/preview/<uuid4 hex>.<ext>`, persisted in
  `panel_settings.appearance_preview_image_key` (the existing column). No bytes,
  signed URLs, Telegram ids, captions or filenames are persisted.
- Upload order: gallery list → capacity check → validate → upload new unique
  object → re-list to confirm the object exists → persist the key → on
  persistence failure remove the just-uploaded object and report it. Existing
  objects are never deleted during upload.
- One `asyncio.Lock` serialises the count check and the write, so two concurrent
  uploads at 9/10 produce exactly one success (tested).
- Deletion order: list → resolve token → clear the key if the target is active
  (abort if that write fails) → remove exactly that object. A Storage refusal is
  reported honestly, including the resulting state.
- Logs contain safe identifiers only (exception text), never bytes, URLs,
  credentials or Telegram metadata.

## 6. Supabase live state and migration reconciliation status

- Live columns already exist on `public.panel_settings`:
  `appearance_header_title`, `appearance_header_body`,
  `appearance_page_overrides`, `appearance_preview_enabled`,
  `appearance_preview_image_key`.
- Live bucket already exists: `appearance-assets`, **private**, 25 MiB object
  limit, MIME allow-list `image/jpeg`, `image/png`, `image/webp`.
- Live migration: version `20261109164548`, name
  `20261109000000_add_panel_settings_appearance`.
- Local source discrepancy: `supabase/migrations/20261109000000_add_panel_settings_appearance.sql`
  contains `position(chr(0) in <column>) = 0` predicates that PostgreSQL rejects
  and that were omitted from the SQL applied live.
- Status: **open, untouched.** Reconciling it would mean renaming/rewriting
  migration history or repairing/applying migrations — both explicitly out of
  scope and unsafe without the owner's decision. The gallery requires no schema
  change, so this blocks nothing. `DATABASE_ARCHITECTURE.md` was not modified.
- No SQL was executed and no migration/bucket/storage object was created by this
  change. The coding agent did **not** apply the live migration.

## 7. Render environment status

`APPEARANCE_MEDIA_BUCKET=appearance-assets` was already configured on the
`Self_bot` service before this task and its redeploy had already been triggered.
**No Render environment variable was added, changed or replaced here.** The code
reads that variable exactly as before (minus the archive fallback).

## 8. Focused test results

```
tests/test_appearance_ui.py + tests/test_appearance_gallery.py
  91 passed, 3 warnings in 1.30s

appearance/panel/settings/dashboard suites (-k "appearance or panel or settings
or dashboard or settings_service")      599 passed, 3 skipped
tests/test_private_message_db_security.py + tests/test_database_setup_order.py
+ tests/test_symbols.py with both appearance suites
                                       183 passed  (before the last two test edits)
```

The new suite covers: empty/one/many/ten images, the 11th upload refusal,
upload after a deletion, selection without side effects, unknown/malformed
tokens, inactive/active deletion, one-object-only deletion, disabled/enabled
preview retention, listing failure, missing selected object + clear, deletion
failure, original-byte preservation, no-eviction upload, rejected image,
persistence failure cleanup, upload failure, concurrent uploads, key uniqueness,
JPEG/PNG/WebP content types, MIME mismatch, truncation/malformed/empty-IDAT,
25 MiB boundary ±1 byte, 8192 px side boundary, 50 Mpx boundary, decompression
bomb timing, no transformation, transfer cap mid-stream, misleading reported
size, no temp file, stream closed on rejection, non-media input, both preview
routes, superseded version 404, text-only disabled preview, panel rows/count/
full state/selected mark/glyph discipline/callback length.

## 9. Full test results

```
PYTHONPATH=. python -m pytest tests -q -p no:cacheprovider
  1 failed, 7298 passed, 27 skipped, 3 warnings in 140.70s
```

The single failure is
`tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`.
It was reproduced on the UNTOUCHED base commit `a301e642` in a clean worktree
(`1 failed in 0.83s`), so it is pre-existing and unrelated to this change; the
scheduler, task execution and AI code are untouched by this diff.

## 10. Compilation and diff-check

```
python -m py_compile backend/services/appearance_service.py backend/bot/handlers/appearance.py
  COMPILE_OK
git diff --check
  clean
```

## 11. Commit and push

One commit, subject
`feat(appearance): keep a ten-image preview gallery with bounded original uploads`.
The exact SHA is the `HEAD` of `main` after this change; the push result and the
verified remote HEAD are recorded in the delivery message for this task.

## 12. Verified remote main HEAD

Verified with `git fetch origin main` + `git rev-parse HEAD` +
`git rev-parse FETCH_HEAD` + `git merge-base --is-ancestor HEAD FETCH_HEAD` +
`git ls-remote origin refs/heads/main`; all four identifiers matched after the
push. See the delivery message for the SHA.

## 13. Final working-tree state

Clean: the four modified files and one new test file are committed; no untracked
or modified file remains, and the temporary baseline worktree was removed.

## 14. Not live-verified behaviour

- No real Telegram upload was performed: the 25 MiB stream cap, the pending-input
  delivery of a real photo/document and the Helper Bot's repaint of the gallery
  panel are verified only through the existing listener architecture and fakes.
- No real Supabase Storage call was made: `list/upload/download/remove` are
  exercised against a fake that mirrors the installed `storage3` surface, not
  against the live `appearance-assets` bucket.
- Telegram's own web-page preview rendering (scaling, cropping, cache
  invalidation timing) is not observable from here. The application serves the
  original bytes at a versioned URL with the correct `Content-Type` and an
  immutable cache header; it does not claim Telegram renders them at full
  original resolution.

## 15. Remaining limitations and blockers

- The gallery is capped at 10 by application logic; the bucket itself has no
  such limit, so objects added out-of-band are ignored unless they use the
  generated key format (and would not be counted).
- A single-owner, single-instance deployment is assumed (the in-process lock).
- Above 12 Mpx, PNG is proved structurally (chunk CRCs + full zlib stream length)
  rather than by materialising the bitmap; that is a deliberate memory bound,
  and the same bound limits WebP pixel decoding.
- The migration version/filename discrepancy described in §6 remains open and
  requires a separate decision; nothing in this feature depends on it.
