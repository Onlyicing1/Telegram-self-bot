# IMPLEMENTATION_REPORT.md — Private Message Management, Phase 1 (panel-first, no AI)

This file describes ONLY the current state of the repository after this task. It
**replaces every previous report in full**. Earlier reports — the emoji feature
implementation (`977247e`), the checkpoint-2 fixes (`e13deee`, `9633c0b`), the
via-bot feasibility study (`b04cb41`), the `98c2825` audit, the boundary-tracing
task (`a54877b`), the callback-receipt task (`1bd843d`), the premium-emoji inline
verification (`39de9cc`, `b463552`, `219c873`, `2eaaf93`) and the LifeOS guide
website (`58aebac`) — are preserved in git history and referenced here only by
commit id.

| Item | Value |
|---|---|
| Task | Implement Private Message Management in the LifeOS panel — Phase 1, before AI integration |
| Project | LifeOS Telegram self-bot (`Onlyicing1/Telegram-self-bot`) |
| Branch / base revision | feature branch **based on `origin/main` = `58aebac`** (the report is pushed to `origin/main`) |
| Outcome | **IMPLEMENTED + TESTED offline.** A real panel (`Menu → Private Messages`) drives real, validated, persisted state; the existing all-messages handler consults that state before processing an incoming private message and applies the configured policy. **No AI tool was added** (Phase 2). |
| Supabase | **Not touched** — no SQL, no schema change, no migration, no `DATABASE_ARCHITECTURE.md` change |
| Live Telegram verification | **No** (no live session in this workspace); see §7 |

---

## 1. Architecture findings (all verified in the current source)

| Area | Path | What the implementation uses |
|---|---|---|
| Glass UI panels | `backend/helper/panel_registry.py`, `backend/helper/panels.py`, `backend/helper/panel_render.py` | `register_panel` / `register_action` / `register_input` + `InlinePanelBuilder`; `panel:*`, `action:*`, `input:*` callback prefixes dispatched by `panels.py::register_callback_handlers`. The new panels are ordinary registrations (parent `menu`). |
| Mother panel | `backend/bot/handlers/misc.py::_build_menu_buttons` | One new row: `Private Messages → panel:pm` (styled `PRIMARY` like every other menu row). |
| Pending inputs | `backend/helper/input_state.py` + `backend/helper/inline_sender.py::register_input_listener` | The owner's reply to an `input:*` prompt is routed to the registered handler; the handler re-renders the owning panel and deletes the owner's reply (the established Bio/Settings input pattern). |
| Persistence | `backend/db/client.py` (singleton Supabase client, optional), `bot_settings` table | ONE JSON document in the **existing** `bot_settings` key/value row `private_message_management` — the same store `backend/services/ghost_seen_v2.py` already writes (`value_type = 'str'`), created on first write. **No new table, no new column.** Bounded `asyncio.to_thread` round trips (`wait_for`, 5 s). |
| Settings conventions | `backend/services/panel_settings_repository.py`, `backend/services/settings_service.py`, `AGENTS.md` §8 | The panel never touches Supabase directly: repository-style access lives in the service, `TZ` stays the authoritative runtime timezone (`os.getenv("TZ", "Asia/Tehran")`, as in `misc.py`/`telemetry.py`/`save.py`), and the missing-database case is the documented in-memory fallback. |
| Incoming messages | `backend/bot/handlers/task_events.py` — the only handler registered on `events.NewMessage()` (all directions) | The policy is applied **inside that existing handler**, before the task-event dispatcher and the answer resolver. No second listener, no second client, no second update loop. |
| Scheduling | `backend/profile/scheduler.py` (minute-boundary profile cron), `backend/ai/task_scheduler.py` (task occurrences) | **Neither is a general policy ticker**, and none was added. A mode is a pure function of `(now, persisted state)`, so the effective policy is recomputed on every incoming message and every panel render — a boundary has nothing to do except change how the *next* message is treated, so a ticker would be dead code. This is the documented gap and the smallest compatible answer (§7). |
| Telegram user resolution | `backend/telegram_api/entities.py::get_entity` (timeout-bounded, typed), `backend/services/ghost_seen_v2.py::load_manage_directory` / `manage_page_items` | Allowlist/exception candidates are the owner's real private chats (bounded, cached directory — no repeated dialog enumeration); the `Add` input resolves a reference through the typed entity layer and refuses anything that is not a real user. Only the numeric user id is stored. |
| UI conventions | `backend/helper/symbols.py`, `backend/helper/styles.py`, `tests/test_symbols.py` | Every glyph comes from the symbol registry (no emoji literals — enforced by `test_symbols.py`), and buttons carry `PRIMARY`/`DANGER` per the documented style rules. |

---

## 2. Implemented (user-visible controls → real state)

### 2.1 Panel surface (`Menu → Private Messages`)

| Control | Effect (all persisted) |
|---|---|
| **Mode: Open / Whitelist only / Closed** | `action:pm_mode:{mode}` → `private_message_service.set_mode` → `bot_settings`. The current mode carries the registry's `✓` mark. |
| **Pause / Enable enforcement** | Master switch. OFF performs **no policy action at all** (identical to the pre-feature behaviour) while still displaying the configured mode. |
| **Unauthorized policy** | `Ignore` / `Delete the message` / `Auto-reply once (rate limited)`, plus an `Auto-reply text` input (validated: non-empty, ≤512 chars). An auto-reply policy with no text falls back to **Ignore**. |
| **Allowlist** | Paginated (8/page) list of the owner's private chats with `ON`/`OFF` marks; one tap adds/removes; `Add user` input resolves `@username`/id through Telegram first. |
| **Exceptions** | The same workflow for the explicit exceptions used by **Closed** mode, kept independent of the allowlist. |
| **Temporary** | One-tap overrides (`Open`/`Whitelist only`/`Closed` × 30 m / 8 h) plus **Clear temporary**; shows activation, expiry and the live remaining time. |
| **Schedule** | Enable/disable, IANA timezone (validated), rules list with per-rule edit/delete, and a draft-based rule builder (day toggles, start/end `HH:MM` inputs, mode cycle, save/cancel). Shows the next transition and the precedence rule. |
| **Status block** | Effective mode, **control source** (manual / temporary override / schedule), remaining time when temporary, unauthorized policy, allowlist and exception counts, schedule state + timezone + rule count, **next scheduled transition**, and the storage line (`database` / `memory only (no database configured)`). |
| **Refresh** | Re-reads the persisted document (`reload_async`) instead of trusting the cache. |

### 2.2 Policy engine (`backend/services/private_message_service.py`)

* **Modes** — `open` (nobody is restricted), `whitelist_only` (only allow-listed senders bypass), `closed` (only explicitly listed exceptions bypass).
* **Precedence, deterministic** — `temporary` (while not expired) → `schedule` (enabled + a rule matches the local wall clock) → manually stored `mode`. When several rules match, the **last** matching rule wins (rules keep the owner's order).
* **Schedule semantics** — per-day rules (`Mon`…`Sun`), `HH:MM` start/end in an explicit IANA timezone. A window whose end is at or before its start **spans midnight and belongs to the day it starts on**. Boundaries are evaluated on the local wall clock, so a DST day is deterministic (pinned by a test on the 2026-11-01 `America/New_York` transition).
* **Next transition** — computed from the temporary expiry and the schedule boundaries in a bounded 8-day window; a boundary is reported **only when the effective mode really changes** across it (a schedule flip hidden under an active temporary override is never promised).
* **Temporary expiry is enforced server-side** — the stored record stops applying the moment it expires (no frontend timer, no reliance on a tick), and the expired record is persisted away on the panel path (`drop_expired_temporary`).
* **Validation at the boundary** — modes, policies, timezones (`ZoneInfo`), `HH:MM` (strict two-digit form), day sets, rule limits (12), list limits (200), autoreply length, and temporary durations (1 min – 7 days) are all validated **before** any write; an invalid value leaves both memory and storage untouched.
* **Write-failure safety** — a candidate state is built and validated as a copy and only committed to memory **after** the database accepted it, so memory and storage can never disagree.
* **Restart correctness** — the document is loaded once (single-flight) and **awaited on first use** (the first incoming message, the first panel render), so a message arriving during startup is never judged against the in-memory default.
* **Backward compatibility** — an absent/`garbage`/partially-malformed document sanitizes to `open` + `ignore` with no allowlist, exceptions, temporary override or schedule: the pre-feature behaviour.
* **Auto-reply rate limit** — one answer per unauthorized sender per 5 minutes, with the recent-sender map pruned on every decision.

### 2.3 Incoming-message hook (`backend/bot/handlers/private_message.py::enforce_incoming`)

Called first, unconditionally, by the existing `task_events` handler:

* **Scope** — incoming **private** messages only. Group/channel messages and the owner's own messages are never touched (the owner can still answer the bot's questions, and the answer resolver is unaffected).
* **Ignore** — nothing is sent or deleted; the message is simply not processed further (it never reaches the event-task dispatcher).
* **Delete** — the message the Self Bot received is deleted through the self client (`delete_messages(chat_id, [id], revoke=True)`, 15 s bound). A failed delete is logged and the message is still not processed.
* **Auto-reply** — the configured text is sent to the sender through the self client, rate-limited as above.
* **Failure mode** — an internal error leaves the message permitted (exactly the pre-feature behaviour) and is logged with bounded identifiers only (`sender_id`, `chat_id`, mode, source, action; never message content).

---

## 3. Files changed

| File | Change |
|---|---|
| `backend/services/private_message_service.py` | **NEW** — state document, validation, `bot_settings` persistence, effective-mode/precedence resolution, schedule evaluation and next-transition computation, authorization + decision, summary/formatting helpers. |
| `backend/bot/handlers/private_message.py` | **NEW** — the Glass UI panel family (`pm`, `pm_policy`, `pm_allow`, `pm_except`, `pm_temp`, `pm_sched`, `pm_rule`), its actions/inputs, and `enforce_incoming`. |
| `backend/bot/handlers/task_events.py` | The existing all-messages handler consults `enforce_incoming` first (docstring updated). |
| `backend/bot/router.py` | Registers `private_message` before `task_events`. |
| `backend/bot/handlers/misc.py` | One mother-panel row (`panel:pm`). |
| `tests/test_private_message_management.py` | **NEW** — 35 tests for the policy engine. |
| `tests/test_private_message_panel.py` | **NEW** — 39 tests for the panel surface and the enforcement hook. |

**Unchanged on purpose:** no AI tool, no provider contract, no scheduler, no
dispatcher, no second listener/client/executor, no Supabase schema, no
`DATABASE_ARCHITECTURE.md`/`ROADMAP.md` change, no unrelated refactor.

---

## 4. Tests and verification

All commands from the worktree root (`.venv` from the parent checkout); exit
statuses preserved.

| Check | Command | Result |
|---|---|---|
| Compile | `.venv/bin/python -m compileall -q backend/` | exit 0 (**COMPILE_ALL_OK**) |
| Focused (policy engine) | `python -m pytest tests/test_private_message_management.py -q` | **35 passed** |
| Focused (panel + enforcement) | `python -m pytest tests/test_private_message_panel.py -q` | **39 passed** |
| Conformance touched by the new panel | `python -m pytest tests/test_symbols.py -q` | **24 passed** |
| Full suite (this branch) | `timeout 560 python -m pytest tests/ -q -p no:randomly` | **6125 passed, 40 failed, 27 skipped** in 123 s |
| Full suite (pristine `origin/main`, same environment) | same command in a clean worktree at `58aebac` | **6051 passed, 40 failed, 27 skipped** in 123 s |
| Failure-set comparison | `diff` of the two `FAILED ...` lists | **identical** — the 40 failures are the pre-existing `tests/test_colored_buttons.py` group (the installed Telethon lacks the button-`style` support those tests assert), i.e. **0 regressions** and exactly **+74 tests** |

Both full-suite runs and the focused suites above were re-executed **after the
final source edit** (they are not carried over from an earlier revision). The
pre-existing failure group was confirmed independently by running
`tests/test_colored_buttons.py` alone in the pristine `58aebac` worktree:
**40 failed, 12 passed** — identical to this branch.
| Whitespace / diff sanity | `git diff --check` | exit 0 |

What the tests actually cover: OPEN/WHITELIST_ONLY/CLOSED decisions; authorized
vs unauthorized senders; owner and group-message exemptions; the master switch;
ignore/delete/auto-reply decisions including the empty-text fallback and the
per-sender cooldown; allowlist add/remove/view with `bot_settings` round trips;
invalid identifiers and invalid settings (mode, policy, timezone, times, days,
durations, limits) changing nothing; temporary activation, deterministic
expiry, fallback and persistence cleanup; manual mode having no expiry;
per-day and per-timezone schedule matching; overnight windows; DST determinism;
the disabled schedule; last-rule-wins; the temporary → schedule → manual
precedence; next-transition computation (including boundaries that change
nothing and those hidden by an override); malformed/absent stored documents;
persistence failure reverting the in-memory state; concurrent mutations
serializing; restart/reload restoring the effective policy; the panel rendering
the persisted (not local) state, its pagination, every action/input, and the
enforcement hook deleting / ignoring / auto-replying / permitting exactly as
the persisted policy dictates — with and without a task dispatcher wired.

**Distinctions:** these are **unit + offline integration tests** (fake
`bot_settings`, fake self client, fake helper bot). There is **no live Telegram
verification** (see §7).

---

## 5. Delivery

| Step | Value |
|---|---|
| Commit | ONE commit for this task: `backend/services/private_message_service.py`, `backend/bot/handlers/private_message.py`, `backend/bot/handlers/task_events.py`, `backend/bot/router.py`, `backend/bot/handlers/misc.py`, `tests/test_private_message_management.py`, `tests/test_private_message_panel.py`, `IMPLEMENTATION_REPORT.md` |
| Push | pushed to `origin/main` from the task branch created at `origin/main` (fast-forward; no rebase, no force-push, no history rewrite) |
| Remote verification | `git fetch origin` → `git rev-parse origin/main` → `git merge-base --is-ancestor <commit> origin/main` (exit 0) |
| Final working tree | the main checkout is untouched: its pre-existing uncommitted `INVESTIGATION.md` change and the untracked `.m14/` worktree are preserved exactly as found |

A commit cannot contain its own hash: the commit SHA and the verified
`origin/main` SHA are reported in this task's delivery response.

---

## 6. Implementation status vocabulary

| Item | Status |
|---|---|
| Panel UI (mode, policy, allowlist, exceptions, temporary, schedule, status) | **IMPLEMENTED, TESTED** |
| Real backend settings + `bot_settings` persistence + in-memory fallback | **IMPLEMENTED, TESTED** |
| Incoming-message policy on the existing event path | **IMPLEMENTED, TESTED** |
| Allowlist + exceptions with validated Telegram resolution | **IMPLEMENTED, TESTED** |
| Temporary + manual modes with deterministic expiry | **IMPLEMENTED, TESTED** |
| Scheduling (days, time window, timezone, precedence, next transition) | **IMPLEMENTED, TESTED** |
| Live Telegram behaviour (real delete / real auto-reply against real senders) | **DEFERRED — NOT VERIFIED LIVE** |
| AI integration (tool, registry, executor) | **DEFERRED to Phase 2 (by instruction)** |
| Telegram account-level privacy / delivery blocking | **NOT IMPLEMENTED — not supported by the account API** |

---

## 7. Limitations (explicit, and honest)

1. **No live Telegram verification.** This workspace has no `API_ID`/`API_HASH`/
   `SESSION_STRING`/`BOT_OWNER_ID`, no `.env` and no live session, so no request
   was made to Telegram and the panel was never opened on a real account. The
   delete/auto-reply calls are exercised against a fake client only.
2. **This feature is application-side.** It never changes the account's Telegram
   privacy settings and **cannot prevent anyone from sending a private
   message**; `closed`/`whitelist_only` describe what the Self Bot does with a
   message Telegram has already delivered. No transport-level blocking is
   claimed anywhere, in the code or in the panel text.
3. **The delete policy deletes a received message** (`revoke=True`), so the
   sender can also lose it. Its success depends on the account's own
   permissions and on Telegram; a failure is logged and the message is still
   not processed.
4. **The auto-reply is a single fixed text per policy**, rate-limited to one per
   sender per 5 minutes; there is no per-user message, no schedule-aware text and
   no AI-written reply (Phase 2 is a separate decision).
5. **No background ticker by design.** A schedule boundary is applied when the
   next message arrives (or when the panel renders). Nothing is "due" at the
   boundary itself, so no message is ever delayed or replayed — but a mode
   change is never observable before the next message.
6. **Storage**: with no Supabase configured the state lives in this process only;
   the panel says so (`memory only (no database configured)`) instead of implying
   durability.
7. **Not verified live:** the inline rendering of the new panels through the
   optional helper bot (the helper is absent here), and the exact appearance of
   button colours (the installed Telethon build in this workspace rejects the
   `style=` argument and falls back to an unstyled button — a pre-existing
   environment condition that also fails 40 pre-existing tests, see §4).

---

## 8. Supabase / database

**Untouched.** No SQL was executed, no schema or migration was changed, no
Supabase request was made, and `DATABASE_ARCHITECTURE.md` was not modified.

**No manual database action is required for this feature.** It stores ONE row in
the **existing** `bot_settings` table (`key = 'private_message_management'`,
`value` = JSON, `value_type = 'str'`), created on the first panel write exactly
like the Ghost Seen allow-list row. The canonical schema already covers that
table, so there is nothing to apply.

---

## 9. Phase roadmap (preserved sequence)

### PHASE 1 — PANEL-FIRST IMPLEMENTATION — **this task (DONE: implemented + tested offline)**
Panel UI · real backend settings and persistence · incoming-message policy
integration · allowlist and exceptions · temporary/manual modes · scheduling on
the verified existing architecture · focused tests and documentation.

### PHASE 2 — AI INTEGRATION — **NEXT, NOT STARTED (by instruction)**
Source-grounded plan, using only the existing AI architecture:

1. Add **one** stateless tool module under `backend/ai/tools/` (e.g.
   `private_message.py`) wrapping the existing service — never a second
   executor, never direct Telegram/SQL access from the model.
2. Register it in `ToolRegistry` with explicit, bounded arguments: `mode`,
   `policy`, `autoreply_text`, `list_action` (`add`/`remove`/`list`),
   `user` (resolved id or reference), `temporary_minutes`, and read-only
   `status`/`schedule` views. Give it `consumable_output_fields` only if a later
   action genuinely needs to consume an output (e.g. the effective mode).
3. Extend the action vocabulary in `backend/ai/actions.py` so the model's
   proposal resolves to that tool (`validate_action` → `resolve_tool_calls`),
   with the same fail-closed validation already used for save/delete/task
   actions; nothing keyword-matched, nothing model-authored beyond the
   arguments.
4. Execution stays in `ToolExecutor` (the owner's message is the authorization
   in this single-owner bot); destructive steps (delete policy, clearing the
   allowlist) keep their confirmation semantics.
5. Update the prompt template with the Phase-2 capability description, and add
   AI integration tests (tool arguments rejected when malformed; the service
   called exactly once; the persisted state the panel later shows is the state
   the tool wrote).

### PHASE 3 — HARDENING AND VERIFICATION — **PLANNED**
Regression coverage for the interaction between the policy gate and the event
dispatcher; permission/security review of the delete + auto-reply paths; runtime
and restart behaviour under recovery/rebuild; a live Telegram verification pass
(delete, auto-reply, panel rendering through the helper bot); documentation and
deployment verification.

**Note on `ROADMAP.md`:** it is the repository's *current-feature* roadmap
(Emoji & Reaction) and was deliberately left untouched, because this task's
instruction named the implementation report as the document to update and the
contract records the phase sequence here. Replacing `ROADMAP.md` with Phase 1–3
of Private Message Management is a one-line decision for the owner if the
repository-wide convention should switch to this feature.
