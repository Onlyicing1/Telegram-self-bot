# ROADMAP

## Completed — Glass UI Appearance (Stage 1)

- One new `Appearance` main-menu category, registered through the existing
  panel registry and callback router.
- Global header title/description overrides, with independent and full reset.
- Per-main-page title/description overrides, validated against the actual
  top-level menu categories; dynamic operational content is preserved.
- Owner-uploaded preview image with an independent ON/OFF switch, durable
  Supabase Storage object key, content/dimension validation, replacement and
  removal.
- Versioned public Open Graph preview route that opens the existing Guide on
  tap; genuine text-only rendering when the switch is off.
- Helper Bot remains the sole editor of the existing inline panel message.
- Migration, rollback, bucket setup and environment requirements documented.

## Next — Stage 2 (not started)

- Live Telegram verification of small-preview rendering, tap-through and
  keyboard preservation on current clients.
- Reuse of the preview image on additional surfaces only if the owner asks;
  the Stage 1 scope deliberately has no gallery, generated artwork or web
  upload UI.
- Optional hardening: one shared strict-write path for the remaining panel
  settings, replacing the legacy cache-only write fallback.

## Deferred / explicitly out of scope

- Curated image gallery, external image search, generated default image.
- Web-dashboard upload workflow.
- Multiple images, per-page preview images, scheduled rotation.
- Any second renderer, settings table, listener, client or update loop.
