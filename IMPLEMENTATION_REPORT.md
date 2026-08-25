# Implementation Report — Ghost Seen v2 Stage 6 AI Reply Foundation

## Stage 6

Added the bounded AI Reply foundation without Telegram delivery. The Action Menu now exposes AI Reply only for exactly one selected message, opens compact Context Selection (1/5/10/20 previous messages), loads bounded same-source context, builds an explicitly delimited untrusted-data prompt, and stores source-aware candidate preparation state. Retry remains a fresh preparation request; no provider call or Telegram send is performed in this stage.

Production file changed: `backend/services/ghost_seen_v2.py` (bounded context retrieval and injection-resistant prompt construction); `backend/bot/handlers/ghost_seen_v2.py` (AI Reply registration, context UI, source/selection validation, candidate state, and retry/back controls). Test file added: `tests/test_61_ghost_seen_v2_stage6.py`.

The existing Stages 1–5 behavior, privacy, Browser/Manage performance paths, reply modes, and destination configuration remain preserved. The existing Engine/Dispatcher/provider architecture was inspected and remains untouched; Stage 6 prepares the request boundary for the next execution step rather than implementing delivery or a second provider path.


## Scope

Repaired three confirmed production problems in Ghost Seen v2:

1. **Ghost Seen did not open directly from the main menu** (user had to open another category and press Back first).
2. **Manage Search crashed** with `TypeError: MessageMethods.edit_message() got multiple values for argument 'message'`.
3. **Manage was extremely slow** — ~8.8s to open, ~5.8s per page click — because every Manage interaction re-enumerated the entire Telegram dialog list.

## Root causes

### A. Direct-open failure — blocking DB on the render path

```
main menu → panel:ghost_seen_v2 → _browser_panel_handler → _render_browser
         → resolve_allowed_chats → _ensure_allowed_loaded → _load_allowed_from_db()
         → db.table("bot_settings").select(...).execute()   <-- SYNCHRONOUS, UNBOUNDED, ON THE EVENT LOOP
```

The old privacy loader ran a synchronous Supabase HTTP call directly on the asyncio event loop from the Browser render path. Per `backend/db/client.py`'s own warning, a direct `.execute()` blocks the entire loop until the response arrives; a slow/cold Supabase call froze the first Browser open so the panel never updated. The loader also guarded on `is_available()`, which is `False` before `get_db()` initializes, so the allow-list was never read from the DB after a restart.

### B. Search crash — wrong `edit_message()` call

`_manage_search_input_handler` (and the browser `_search_input_handler`) called:

```python
await helper.edit_message(inline_chat_id, inline_msg_id, message=rendered, buttons=built)
```

Telethon's real signature is `edit_message(entity, message=None, text=None, *, buttons=None)`. The call bound `inline_msg_id` positionally to `message`, then passed `message=rendered` as a keyword → `TypeError: ... got multiple values for argument 'message'`.

### C. Manage slowness — repeated `iter_dialogs()`

Every Manage interaction (`_manage_panel_handler`, `_manage_inline_builder`, `_manage_page_action`, `_toggle_permission_action`, `_manage_search_input_handler`) called `load_private_chats()` → `client.iter_dialogs()`, streaming the full dialog list each time. The previous performance fix intentionally left this broad path for Manage; that design proved too slow in production.

## Actual implementation

### `backend/services/ghost_seen_v2.py`

- **Async, non-blocking privacy loading** — `_ensure_allowed_loaded_async()` runs the Supabase read in a worker thread via `asyncio.to_thread`, calls `get_db()` directly (fixing the `is_available()` init bug), and union-merges into the in-memory `_allowed_chats` set. Loads at most once per process (cached). `_ensure_allowed_loaded()` is now a cheap flag check that never performs I/O.
- **Non-blocking persistence** — `_persist_allowed_to_db()` runs the Supabase write in a background daemon thread so a slow DB never blocks the event loop or a Manage toggle callback.
- **Manage directory cache** — `load_manage_directory()` caches the private-chat directory in-memory for `_MANAGE_DIRECTORY_TTL_S = 60s`; broad discovery happens at most once per TTL and repeated page/search/toggle reuse the cached set. `invalidate_manage_directory()` drops the cache.
- **`resolve_allowed_chats()`** now awaits `_ensure_allowed_loaded_async()` before resolving only the allowed IDs (O(allowed), zero `iter_dialogs`).

### `backend/bot/handlers/ghost_seen_v2.py`

- **`edit_message` fix** — both `_manage_search_input_handler` and `_search_input_handler` now call `helper.edit_message(inline_chat_id, inline_msg_id, rendered, buttons=built)` (positional text).
- **Cached Manage data** — `_manage_panel_handler`, `_manage_inline_builder`, `_manage_page_action`, `_toggle_permission_action`, and `_manage_search_input_handler` now use `load_manage_directory()`.
- **Toggle safety** — `_toggle_permission_action` awaits `_ensure_allowed_loaded_async()` before mutating so persistence always writes the full allow-list.
- **Startup preload** — `register()` schedules `_ensure_allowed_loaded_async()` in the background via `guarded_create_task`, so the first Browser open never performs DB work on the render path.

## New data paths

```
Browser open → _render_browser → resolve_allowed_chats → get_entity(allowed_id)…   (O(allowed), no iter_dialogs)
Manage open → load_manage_directory → iter_dialogs once per 60s TTL, then cached
Manage Next/Prev/Search/Toggle → load_manage_directory (cache hit) → local filter/paginate
```

`iter_dialogs()` now exists only inside `load_private_chats()` (the broad-discovery primitive) and in docstrings. The normal Browser never calls it. Manage is the only intentional broad-discovery path and is now bounded by the directory cache.

## Performance before/after

| Operation | Before | After |
|---|---|---|
| Ghost Seen Browser open | 1 `iter_dialogs()` (streams ~500) + sync DB call on the loop | 0 `iter_dialogs()`; `get_entity()` per allowed ID; async DB load once |
| Manage open | 1 `iter_dialogs()` every open (~8.8s) | 1 `iter_dialogs()` per 60s TTL, then cached |
| Manage Next/Previous | 1 `iter_dialogs()` per click (~5.8s) | 0 (cache hit) — local pagination + one edit |
| Manage Search | 1 `iter_dialogs()` per query + crash | 0 (cache hit) — local filter; no crash |
| Manage Toggle | 1 `iter_dialogs()` per toggle | 0 (cache hit) — permission update + rerender |

## Test files changed

- `tests/test_58_ghost_seen_v2_manage_bounded.py` — updated tests 17/18/19 to patch `service_module.load_private_chats` (handlers now route through `load_manage_directory`) and to invalidate the directory cache.
- `tests/test_60_ghost_seen_v2_nav_search_perf.py` — **new**, 27 focused regression tests (navigation, search, performance, privacy, regression).

## Validation

```
.venv/bin/python -m pytest tests/test_52_ghost_seen_v2_stage1.py -q --asyncio-mode=auto   → PASS
.venv/bin/python -m pytest tests/test_5[2-9]_ghost_seen_v2_*.py tests/test_60_*.py -q     → 144 passed
.venv/bin/python -m pytest tests/ -q --asyncio-mode=auto                                  → 950 passed, 23 skipped, 1 pre-existing warning
.venv/bin/python -m compileall -q backend                                                → PASS
git diff --check                                                                          → PASS
bun tsc -b --noEmit                                                                       → PASS
```

## Source verification

- Normal Browser (`_render_browser` → `resolve_allowed_chats`) calls **no** `iter_dialogs()`.
- Manage is the only intentional broad-discovery path, and it is bounded by the directory cache (at most once per 60s).
- Allowed IDs are the source of Browser membership; the watcher count reads `len(get_allowed_chats())`.
- No synchronous DB call remains on the Browser or Manage render path.
- No 500-button rendering; Manage stays capped at `MANAGE_PAGE_SIZE = 8` rows.
- No legacy Ghost Seen identifiers; exactly one `INVESTIGATION.md`.

## Destination configuration (unchanged, existing keys)

```
GHOST_SEEN_DESTINATION_CHAT_ID=<numeric Telegram chat ID>    # authoritative numeric ID
GHOST_SEEN_DESTINATION_CHAT_NAME=<user-defined display name> # display-only label
```

Read in `backend/config.py` and `backend/services/ghost_seen_v2.py`. The ID is authoritative for future notification flows; the name is display-only and never used to locate or send. Configure both in Render → Environment Variables. No Render configuration was modified and no deployment was performed.

## Delivery

- Commit: `(see git log)`
- Pushed to `origin/main`; `HEAD == origin/main`
- Working tree clean.
- Telegram live E2E: **not performed** (user-provided production logs were the evidence; no live Telegram execution was run).
- You.com / web search / providers / Save / Delete / Retrieve / Profile / unrelated AI infrastructure: **untouched**.
