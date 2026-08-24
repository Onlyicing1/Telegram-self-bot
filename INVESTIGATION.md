# Ghost Seen Removal Investigation

## Decision

The pre-existing Ghost Seen implementation was intentionally removed in this cleanup execution. The next execution will rebuild Ghost Seen v2 from zero; this execution does not add a replacement.

## Removed footprint

- Dedicated production handler: `backend/bot/handlers/ghost_seen.py`.
- Dedicated production service and all in-memory Ghost Seen selection/pagination/reply state: `backend/services/ghost_seen_service.py`.
- Router import and `register_all()` registration for Ghost Seen.
- `Menu`'s Ghost Seen panel entry and Ghost Seen retention panel/actions.
- `GHOST_ROOM_ID` config exposure and dormant startup-check validation.
- Ghost Seen database-stat counting helper and its database statistics output.
- Ghost Seen-specific Supabase migration files.
- Old Ghost Seen implementation contract and dedicated Ghost Seen test modules.

This removes the old browsing, selection, manual reply, callback, input, AI, and delivery implementation as requested. No compatibility wrapper or stale callback registration remains.

## AI/input removal

The old Ghost Seen AI Reply flow, context/disclosure callbacks, pending reply state, AI execution helpers, and `input:ghost_chat:ai_prompt` producer were already removed in the preceding cleanup commit and are absent from the current production tree. The legacy prompt text `Type your instruction for the selected messages.` has no executable producer. The generic AI provider/engine and generic panel/input infrastructure remain because they serve unrelated features.

## Persistence and configuration

Ghost Seen's `ghost_chats` table was feature-specific. Its repository migration and runtime database-stat usage were removed, but no destructive operation was executed against an already-provisioned Supabase database; an existing physical table, if present, is now unused by this repository. `GHOST_ROOM_ID` was feature-specific and is no longer read by production configuration or startup checks. No Render environment or deployment configuration was changed.

## Preserved systems

The shared `backend.helper` panel/input/callback infrastructure, generic AI provider architecture, You.com/web-search implementation, Supabase client, unrelated database services, runtime supervisor, and all unrelated handlers remain intact.

## Verification

The dedicated Ghost Seen test modules were removed with the implementation because they tested deleted behavior. The remaining suite must validate the actual post-removal repository. This document is the single canonical investigation document. Telegram live behavior was not tested; old Telegram messages may remain in chat history, but the repository no longer has code that can render or process the old feature.
