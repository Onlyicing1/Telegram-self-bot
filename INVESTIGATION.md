# INVESTIGATION.md — Canonical Forensic Report

> **Rule:** this file is the single canonical investigation report for the
> LifeOS repository. Every new investigation **fully replaces** this file —
> never append. Always base findings on actual repository source; distinguish
> confirmed facts, direct evidence, likely causes, and unknowns.

---

## INVESTIGATION METADATA

| Field | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Branch | `main` |
| HEAD (local, verified) | `7631a6cdd2151ec066fe4cb74a0c6de3b1a3f216` |
| Investigation date | 2026-08-18 |
| Scope | **Bio** + **Username** subsystems: complete current structure, execution flow, state, scheduling, persistence, handlers, AI/tool integration, Telegram execution, and Bio-vs-Username comparison. |
| Status | **INVESTIGATION ONLY** — no production code changed, no DB changes, no commit. Only `INVESTIGATION.md` was replaced. |

> Note: the canonical report file is `INVESTIGATION.md` (this file). It was
> previously named `INVESTIGATION_REPORT.md`; the rename is already reflected
> in the repository. This write fully replaces the prior Save-System report.

---

## 1. EXECUTIVE SUMMARY

Bio and Username are **two near-verbatim mirror implementations of the same
"profile field renderer" concept**, differing only in which Telegram profile
field they control and which DB table they use:

- **Bio** → `about` field → `bio_state` table → `last_bio` state key.
- **Username** → `first_name` field → `username_state` table → `last_name` state key.

Both engines do **not** run their own cron loops. Instead each registers a
single "updater" callable into one **shared Profile Scheduler**
(`backend/profile/scheduler.py`), which fires once per minute at `HH:MM:00`,
collects the changed fields from all updaters, and sends a **single**
`UpdateProfileRequest` to Telegram. This is the intended (and correctly
implemented) merge point.

The three user-facing surfaces are: **Glass UI panels** (`.menu → Profile →
Bio/Username`), **AI tools** (6 bio + 6 username tools), and — importantly —
**no dot-command surface at all** (`.bio`/`.username` are documented in
README but not registered).

The investigation confirmed the happy path is structurally sound (dedup per
engine, single merged RPC, idempotent scheduler start, client swap on rebuild).
It also confirmed several **real defects**, the most significant being that
**turning off one engine cancels the shared scheduler and therefore also stops
the other engine**, because both engines' `stop_cron()` call the same
`profile_scheduler.stop_cron()`.

---

## 2. BIO ARCHITECTURE

Layers (top to bottom):

| Layer | File | Role |
|---|---|---|
| Glass UI handler | `backend/bot/handlers/bio.py` | Registers panels `bio` (parent `profile`) and `biohelp` (parent `bio`), actions, inputs, and a server-side template builder buffer. |
| Service | `backend/services/bio_service.py` | `do_on/do_off/do_show/do_template/do_text/do_mood` — all business logic; both UI and AI tools call these. |
| Engine | `backend/bio/engine.py` | Template rendering + updater registration; delegates cron lifecycle to the shared scheduler. |
| Shared scheduler | `backend/profile/scheduler.py` | Single per-minute task; merges updater results; sends `UpdateProfileRequest`. |
| Persistence | `backend/db/client.py` | `get_bio_state`, `get_or_create_bio_state`, `update_bio_state` over `bio_state` table with in-memory fallback. |
| AI tools | `backend/ai/tools/bio.py` | 6 tool classes wrapping `bio_service`. Registered in `create_default_registry()` (`backend/ai/tools/registry.py`). |
| Telegram primitive | direct `client(UpdateProfileRequest(...))` in scheduler | Actually sends the profile change. |

Bio state fields (`bio_state` table): `owner_id` (unique), `template`,
`mood`, `custom_text`, `is_active`, `last_bio`, `updated_at`.

Bio default template (DB / engine / service): `🕒 {time} | 💭 {mood}`.
Tokens: `{time}` (HH:MM), `{mood}`, `{text}`.

---

## 3. BIO EXECUTION PATH

### Enable (`do_on`)
```
Glass UI: action:bio_on → _bio_on_action
  → bio_service.do_on(_self_client, _owner_id, _resolve_tz())
      → db_client.get_or_create_bio_state(owner_id)
      → db_client.update_bio_state(owner_id, {"is_active": True})
      → bio_engine.start_cron(client, owner_id, tz_str)
          → _ensure_registered()  → profile_scheduler.register_updater("bio", _bio_updater)
          → profile_scheduler.start_cron(client, owner_id, tz_str)  [idempotent]
      → render_bio(...) → returns "✅ Bio cron ON\nPreview: ..."
```
AI equivalent: `BioOnTool.execute` → `bio_service.do_on(context.telegram.client, context.owner_id, context.tz_str)`.

### Per-minute tick
```
profile_scheduler._cron_loop
  → sleep to next HH:MM:00
  → _collect_updates(owner_id, tz_str)
      → _bio_updater(owner_id, tz_str)
          → get_bio_state → if not active: return None
          → new_bio = render_bio(template, mood, text, tz)
          → if new_bio == last_bio: return None   (dedup)
          → update_bio_state({last_bio: new_bio, updated_at})
          → return {"about": new_bio}
  → merged.update(...)
  → client(UpdateProfileRequest(**updates))     [30s timeout]
  → set_last_bio_update()
```

### Disable (`do_off`)
```
Glass UI: action:bio_off → _bio_off_action
  → bio_service.do_off(owner_id)
      → update_bio_state(owner_id, {"is_active": False})
      → bio_engine.stop_cron()  → profile_scheduler.stop_cron()
      → "⏹ Bio cron OFF"
```

### Set template / text / mood / show
Each writes the corresponding `bio_state` column via `update_bio_state`, or
reads via `get_or_create_bio_state` (show). Template builder (Glass UI) keeps
an in-memory per-owner buffer `_builder_buffers: dict[int, str]` in the handler
module and only calls `do_template` on "Apply".

---

## 4. USERNAME ARCHITECTURE

Identical in structure to Bio, with these substitutions:

| Bio | Username |
|---|---|
| `backend/bio/engine.py` | `backend/username/engine.py` |
| `backend/services/bio_service.py` | `backend/services/username_service.py` |
| `backend/bot/handlers/bio.py` | `backend/bot/handlers/username.py` |
| `backend/ai/tools/bio.py` | `backend/ai/tools/username.py` |
| `bio_state` table | `username_state` table |
| `render_bio` | `render_username` |
| `_bio_updater` → `{"about": ...}` | `_username_updater` → `{"first_name": ...}` |
| `last_bio` state key | `last_name` state key |
| default `🕒 {time} | 💭 {mood}` | default `{time} | {mood}` |

The `username_state` table is created by
`supabase/migrations/20260801215007_create_username_state_table.sql` (mirrors
`bio_state`; single row per owner via `UNIQUE(owner_id)`).

---

## 5. USERNAME EXECUTION PATH

Mirrors Bio exactly:

- **Enable**: `action:username_on` → `_username_on_action` →
  `username_service.do_on(_self_client, _owner_id, _resolve_tz())` →
  `get_or_create_username_state` → `update_username_state(is_active=True)` →
  `username_engine.start_cron(...)` → register updater `"username"` +
  `profile_scheduler.start_cron(...)`.
- **Tick**: `_username_updater` → `get_username_state` → if inactive return
  None → `render_username` → dedup on `last_name` → `update_username_state`
  → return `{"first_name": ...}` → merged into the shared
  `UpdateProfileRequest`.
- **Disable**: `action:username_off` → `username_service.do_off` →
  `update_username_state(is_active=False)` → `username_engine.stop_cron()` →
  `profile_scheduler.stop_cron()`.
- **AI**: `UsernameOnTool`/`UsernameOffTool`/etc. call `username_service`
  directly.

---

## 6. BIO vs USERNAME

| Dimension | Bio | Username | Verdict |
|---|---|---|---|
| Engine | `backend/bio/engine.py` | `backend/username/engine.py` | Near-verbatim duplicate |
| Service | `bio_service.py` | `username_service.py` | Near-verbatim duplicate |
| Glass handler | `handlers/bio.py` | `handlers/username.py` | Near-verbatim duplicate |
| AI tools | `tools/bio.py` (6 tools) | `tools/username.py` (6 tools) | Near-verbatim duplicate |
| DB functions | `get_bio_state` / `get_or_create_bio_state` / `update_bio_state` | `get_username_state` / `get_or_create_username_state` / `update_username_state` | Duplicate with field-key differences |
| Table | `bio_state` | `username_state` | Separate (mirror schema) |
| Controlled field | `about` | `first_name` | Different |
| State key | `last_bio` | `last_name` | Different |
| Default template | `🕒 {time} | 💭 {mood}` | `{time} | {mood}` | Different |
| Scheduler | shared `profile/scheduler.py` | shared (same) | **Shared** |
| Cron loop | none (delegated) | none (delegated) | **Shared** |
| ENV auto-start | `BIO_UPDATE_ENABLED` | *(none)* | **Asymmetric** |
| Health telemetry | `bio_cron_ok`, `last_bio_update` | *(none)* | **Asymmetric** |
| Dot command | none registered | none registered | Same (both absent) |
| Restart/resume | `_resume_bio_cron` (DB flag or ENV) | `_resume_username_cron` (DB flag only) | **Asymmetric** |

Key structural points:

1. **The scheduler is the only shared infrastructure.** Both engines correctly
   rely on it and do not spawn their own loops — this matches the
   "one profile scheduler" invariant in `AI_MASTER_DESIGN.md` §29.1.9.
2. **Everything above the scheduler is duplicated.** The two engines/services/
   handlers/tools could be parameterized by `field` + `state key`, but are not.
3. **The one place they must interact (stop semantics) is where the shared
   abstraction is misused** — see §7.1.

---

## 7. CONFIRMED FACTS

### 7.1 CONFIRMED — Disabling one engine stops both (shared `stop_cron`)
**Evidence:** `bio_service.do_off` → `bio_engine.stop_cron()` →
`profile_scheduler.stop_cron()`; `username_service.do_off` →
`username_engine.stop_cron()` → `profile_scheduler.stop_cron()`. There is a
single `profile_scheduler._task`; `stop_cron()` cancels it regardless of which
engine asked.
**Impact:** If Username is active and the user turns Bio off, the shared
scheduler is cancelled and Username updates stop too (its `is_active` remains
`True`, but nothing calls its updater until some future `start_cron`).
**Class:** CONFIRMED (source-proven). Not covered by tests (no bio/username
tests exist).

### 7.2 CONFIRMED — Two profile-update implementations; `telegram_api/profile.py` is orphaned
**Evidence:** The scheduler sends the RPC directly:
`client(UpdateProfileRequest(**updates))` inside `_cron_loop`, wrapped in
`asyncio.wait_for(..., timeout=_API_TIMEOUT)`. Separately,
`backend/telegram_api/profile.py:update_profile` wraps the same RPC with
`guarded_await` + `TelegramAPIError`/`TelegramTimeoutError`, and
`TelegramAPI.update_profile` (`backend/telegram_api/api.py`) calls it. A repo
grep shows **no external caller** of either `update_profile` — the scheduler is
the only place a profile update actually happens.
**Impact:** Dead code; the profile RPC timeout/error mapping path is never
exercised. Two divergent ways to perform the same operation exist.
**Class:** CONFIRMED (source-proven).

### 7.3 CONFIRMED — No `.bio` / `.username` dot commands exist
**Evidence:** `handlers/bio.py` and `handlers/username.py` `register()` only
call `register_panel`, `register_inline_builder`, `register_action`,
`register_input`. No `@client.on(events.NewMessage(pattern=...))` for `.bio`/
`.username`. The only text command registered is `.menu` (in `misc.py`).
README documents `.bio on/off/show/template/text/mood` and
`.username on/off/show/template` — these are **not implemented**.
**Class:** CONFIRMED (source-proven). Documentation/implementation mismatch.

### 7.4 CONFIRMED — Bio handler default reset template is malformed
**Evidence:** `backend/bot/handlers/bio.py:56`:
`_DEFAULT_TEMPLATE = "🕒 {time} | 💭 {mood"` (missing closing `}` on `{mood}`).
`_bio_builder_reset_action` (lines 342-343) uses it. `render_bio` replaces
`"{mood}"` (with brace), so `{mood` remains literal in the rendered bio.
The username handler's `_DEFAULT_TEMPLATE = "{time} | {mood}"` (line 53) is
correct.
**Class:** CONFIRMED (source-proven).

### 7.5 CONFIRMED — Health telemetry is bio-only
**Evidence:** `backend/health.py` has `_bio_cron_ok`, `set_bio_cron_ok`,
`_last_bio_update`, `set_last_bio_update`. No username equivalents. The
scheduler calls `set_last_bio_update()` after **any** successful
`UpdateProfileRequest`, including username-only updates.
**Impact:** "last bio update" is a mislabeled metric for username-only changes;
the health snapshot cannot report username engine health independently.
**Class:** CONFIRMED (source-proven).

### 7.6 CONFIRMED — Bio vs Username are near-verbatim duplicates
**Evidence:** `engine.py`, `*_service.py`, `handlers/*.py`, `ai/tools/*.py`,
and the DB state functions are line-for-line mirrors differing only in field
names / keys. See §6.
**Class:** CONFIRMED (source-proven).

### 7.7 CONFIRMED — Scheduler merge + dedup design is correct
**Evidence:** `_collect_updates` iterates `list(_updaters)`, merges non-None
dicts with `merged.update(result)`, and only sends when `updates` is non-empty.
Each updater independently returns `None` on `is_active == False` or unchanged
render. Therefore: ≤1 `UpdateProfileRequest` per minute, both fields combined
when both change.
**Class:** CONFIRMED (source-proven). This is the correct shared-merge design.

### 7.8 CONFIRMED — Scheduler lifecycle is idempotent and restart-safe (same process)
**Evidence:** `start_cron` returns early if `_task and not _task.done()`.
`update_client` swaps `_client`. Each engine's `_ensure_registered` guards with
a module-level `_registered` bool so updaters register exactly once.
**Class:** CONFIRMED (source-proven).

### 7.9 CONFIRMED — Bio has an ENV auto-start; Username does not
**Evidence:** `supervisor._resume_bio_cron` also checks
`self.cfg.get("BIO_UPDATE_ENABLED")`; `_resume_username_cron` only checks the
DB `is_active` flag. There is no `USERNAME_UPDATE_ENABLED` equivalent.
**Class:** CONFIRMED (source-proven).

### 7.10 CONFIRMED — Bio/Username resume + client-swap on rebuild
**Evidence:** `supervisor.py` lines ~667-675: `bio_engine.update_client(self.client)`,
`username_engine.update_client(self.client)`, then `_resume_bio_cron()` and
`_resume_username_cron()`. Shutdown (~968-971) calls both engines' `stop_cron`
(redundant — same scheduler task).
**Class:** CONFIRMED (source-proven).

### 7.11 CONFIRMED — AI tool integration exists for both engines
**Evidence:** `create_default_registry()` (`backend/ai/tools/registry.py`)
registers 6 Bio + 6 Username tools. `BioOnTool`/`UsernameOnTool` pass
`context.telegram.client` (raw Telethon client) into `do_on`. `TelegramAPI.client`
is a property returning `self._client` (`backend/telegram_api/api.py`), so the
scheduler receives a callable Telethon client.
**Class:** CONFIRMED (source-proven).

### 7.12 CONFIRMED — No dedicated Bio/Username tests exist
**Evidence:** `ls tests/ | grep -iE "bio|username|profile|schedul"` returns
nothing. The full suite does not exercise these engines.
**Class:** CONFIRMED (filesystem-verified).

---

## 8. LIKELY CAUSES / RISKS

1. **LIKELY — Cross-engine shutdown is a latent regression, not an observed
   crash.** The source proves the shared `stop_cron` kills both engines; there
   is no test/log evidence in this sandbox of a live occurrence, but the code
   path is deterministic.
2. **LIKELY — Updater re-registration is fragile if `_updaters` is ever
   cleared.** `unregister_updater` exists but is never called. If a future
   change clears the list while `_registered` stays `True`, engines would not
   re-register. Currently dormant.
3. **LIKELY — `_get_tz`'s `except (ZoneInfoNotFoundError, Exception)` is
   redundant** (the broad `Exception` subsumes the specific one) and slightly
   masks intent, but it is functionally safe (falls back to UTC).
4. **LIKELY — Username `get_or_create` error path differs subtly from Bio.**
   `_get_or_create_username_state_sync` uses bare `raise` after DB
   insert/reload failure, while the bio equivalent logs and continues to the
   in-memory fallback. The async wrappers both catch and fall back, so callers
   are not crashed, but the two paths behave differently under DB failure and
   produce extra error logging on the username side.
5. **LIKELY — `first_name` updates may be subject to Telegram name-frequency
   limits** beyond the FloodWait already handled. This is a platform-behavior
   concern, not proven from source.

---

## 9. UNKNOWN / MISSING EVIDENCE

- **Live Telegram behavior** of `UpdateProfileRequest` (both `about` and
  `first_name`) was not verified — no credentials/session in this sandbox.
- **Production environment values** (whether `BIO_UPDATE_ENABLED`, `TZ`, or
  Supabase are actually set) cannot be verified from the repository.
- **Whether the cross-engine stop bug has manifested in production** — no log
  evidence is available here.
- **Exact Telegram name-change rate limits** for `first_name` are not verified.

---

## 10. EXACT FILES

| File | Role |
|---|---|
| `backend/bio/engine.py` | Bio renderer + updater + cron delegation |
| `backend/username/engine.py` | Username renderer + updater + cron delegation |
| `backend/profile/scheduler.py` | Shared per-minute profile scheduler (single task) |
| `backend/services/bio_service.py` | Bio business logic (`do_*`) |
| `backend/services/username_service.py` | Username business logic (`do_*`) |
| `backend/bot/handlers/bio.py` | Bio Glass UI panels/actions/inputs/builder |
| `backend/bot/handlers/username.py` | Username Glass UI panels/actions/inputs/builder |
| `backend/bot/handlers/misc.py` | `menu`/`profile` panels, `.menu` command, `_resolve_tz` |
| `backend/ai/tools/bio.py` | 6 Bio AI tools |
| `backend/ai/tools/username.py` | 6 Username AI tools |
| `backend/ai/tools/registry.py` | `create_default_registry()` — registers all tools |
| `backend/ai/tools/context.py` | `ToolContext` (telegram/client/owner_id/tz_str) |
| `backend/db/client.py` | `bio_state` + `username_state` get/get_or_create/update + fallback |
| `backend/runtime/supervisor.py` | Resume/swap/shutdown of the engines |
| `backend/main.py` | Entry point (delegates to `RuntimeSupervisor`) |
| `backend/telegram_api/profile.py` | **Orphaned** `update_profile` wrapper |
| `backend/telegram_api/api.py` | `TelegramAPI` facade (`.client` property) |
| `backend/health.py` | Bio-only health flags (`bio_cron_ok`, `last_bio_update`) |
| `backend/helper/inline_engine.py` | `_self_client` / `_owner_id` globals + setters |
| `supabase/migrations/20260801215007_create_username_state_table.sql` | `username_state` schema |

---

## 11. EXACT FUNCTIONS / CLASSES

**Engine (`backend/bio/engine.py`)**
`_get_tz`, `render_bio`, `_bio_updater`, `_ensure_registered`, `start_cron`,
`update_client`, `stop_cron`, `is_running`.

**Engine (`backend/username/engine.py`)**
`_get_tz`, `render_username`, `_username_updater`, `_ensure_registered`,
`start_cron`, `update_client`, `stop_cron`, `is_running`.

**Scheduler (`backend/profile/scheduler.py`)**
`_backoff`, `get_tz`, `_seconds_to_next_minute`, `register_updater`,
`unregister_updater`, `_set_client`, `update_client`, `_collect_updates`,
`_cron_loop`, `_supervised_cron`, `start_cron`, `stop_cron`, `is_running`.

**Service (`bio_service.py` / `username_service.py`)**
`do_on`, `do_off`, `do_show`, `do_template`, `do_text`, `do_mood`.

**Handlers (`bio.py` / `username.py`)**
`_build_*_main_buttons`, `_*_panel_handler`, `_*help_panel_handler`,
`_*_inline_builder`, `_*help_inline_builder`, builder actions
(`_*_builder_add/space/clear/reset/apply_action`), `_*_on_action`,
`_*_off_action`, `_*_reply_mode_action`, `_*_text/mood_input_handler`,
`_*_custom_reply_handler`, `register`.

**AI tools (`ai/tools/bio.py` / `ai/tools/username.py`)**
`BioSetTemplateTool`, `BioSetTextTool`, `BioSetMoodTool`, `BioOnTool`,
`BioOffTool`, `BioShowTool` (and the `Username*` equivalents).

**Registry** `backend/ai/tools/registry.py: create_default_registry(context)`.

**DB** `backend/db/client.py`: `_get_bio_state_sync`/`get_bio_state`,
`_get_or_create_bio_state_sync`/`get_or_create_bio_state`,
`_update_bio_state_sync`/`update_bio_state`, and the `username_state`
equivalents.

**Supervisor** `backend/runtime/supervisor.py`: `_resume_bio_cron`,
`_resume_username_cron`, `update_client` swap block, shutdown block.

---

## 12. EXECUTION PATHS

### Bio enable (Glass UI)
```
.menu → panel:profile ("👤 Bio" button → panel:bio)
  → "✅ Enable Sync" → panel:biohelp:cmd:on → action:bio_on
  → _bio_on_action → bio_service.do_on(_self_client, _owner_id, _resolve_tz())
    → get_or_create_bio_state → update_bio_state(is_active=True)
    → bio_engine.start_cron → profile_scheduler.register_updater("bio") + start_cron
```

### Bio disable (Glass UI)
```
action:bio_off → _bio_off_action → bio_service.do_off
  → update_bio_state(is_active=False)
  → bio_engine.stop_cron → profile_scheduler.stop_cron()   ⚠ stops username too
```

### Username enable/disable — identical shape, `first_name`/`username_state`.

### Per-minute tick (shared)
```
profile_scheduler._cron_loop
  → sleep to HH:MM:00
  → _collect_updates
       → _bio_updater      → {"about": ...} | None
       → _username_updater → {"first_name": ...} | None
  → if merged: client(UpdateProfileRequest(**merged))  [30s timeout, FloodWait sleep]
  → set_last_bio_update()
```

### AI path
```
create_default_registry(ToolContext)
  → BioOnTool.execute → bio_service.do_on(context.telegram.client, ...)
  → same engine/scheduler path
```

### Startup / rebuild / shutdown
```
RuntimeSupervisor.start → _resume_bio_cron (DB flag OR BIO_UPDATE_ENABLED)
                        → _resume_username_cron (DB flag only)
rebuild: bio_engine.update_client + username_engine.update_client
         → _resume_bio_cron + _resume_username_cron
shutdown: bio_engine.stop_cron() → username_engine.stop_cron()  (same task, twice)
```

---

## 13. RECOMMENDED FIX SURFACE

*(Do NOT implement here — this is for the execution agent.)*

1. **Decouple per-engine "off" from the shared scheduler stop.**
   `stop_cron` must only cancel the shared task when **no** engine is still
   active. Minimal approach: move "should the scheduler keep running" into the
   scheduler (e.g., `stop_cron(force=...)` or have the engines consult both
   states before calling `profile_scheduler.stop_cron`). This is the highest-
   priority fix.
2. **Consolidate the Bio/Username duplication** into a single parameterized
   profile-engine abstraction (field name, state key, default template, table
   accessor) — or, if consolidation is out of scope, at minimum eliminate the
   drift that caused §7.4 and §7.5.
3. **Fix the malformed Bio default template** (`"🕒 {time} | 💭 {mood"` →
   `"🕒 {time} | 💭 {mood}"`).
4. **Decide the fate of `backend/telegram_api/profile.py`.** Either route the
   scheduler through `update_profile` (single RPC path + timeout/error mapping)
   or delete the orphaned wrapper. Do not keep both.
5. **Add symmetric health telemetry** for the username engine (e.g.,
   `username_cron_ok`, `last_username_update`) and stop mislabeling
   username-only updates as "last_bio_update".
6. **Resolve the ENV asymmetry** (add a username auto-start flag or document
   the intentional asymmetry).
7. **Add `.bio`/`.username` dot commands** or correct the README to remove
   commands that do not exist.
8. **Add regression tests** (§15) for the cross-engine stop bug, dedup, merge,
   and template rendering.
9. **Align the Username DB `get_or_create` error path** with Bio's
   catch-and-fallback behavior (or make both raise consistently).

---

## 14. REMAINING WORK

- Execute the §13 fixes (the cross-engine `stop_cron` bug is the priority).
- Add Bio/Username unit tests (none exist today).
- Verify live Telegram profile updates (both `about` and `first_name`).
- Reconcile README `.bio`/`.username` documentation with the actual Glass-UI-
  only surface.
- Decide the `telegram_api/profile.py` consolidation.

---

## 15. VALIDATION

Checks the execution agent should perform after any change:

- **Unit tests** for: `render_bio` / `render_username` (all three tokens),
  dedup (`None` when unchanged), inactive → `None`, merge of both fields,
  single `UpdateProfileRequest` per tick, `start_cron` idempotency.
- **Regression test** that turning off Bio while Username is active keeps the
  scheduler running (and vice versa).
- **Verify** only one profile-update RPC path exists after the fix.
- **Compile** modified Python files (`py_compile`) and run the full suite.
- **Live Telegram verification** of `about` and `first_name` updates (only if
  a real session is available — do not claim otherwise).

---

### Verification levels for this investigation

- `SOURCE VERIFIED` ✅ (all claims read directly from the repository files listed above)
- `TEST VERIFIED` ❌ (no Bio/Username tests exist; no tests were run — investigation only)
- `LIVE TELEGRAM VERIFIED` ❌ (no credentials/session in this environment)
