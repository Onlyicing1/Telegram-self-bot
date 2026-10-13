# INVESTIGATION.md — Emoji Panel: One-Button Diagnostic Workflow

This file records **only** the current, evidence-based investigation and
diagnosis. It **replaces every previous investigation in full**; earlier
narratives (the premium-emoji inline entitlement diagnosis) remain readable in
git history (`78d8744`, `798ff4b`, PR #183, and the previous `50b035a`).

| Item | Value |
|---|---|
| Project / remote | LifeOS Telegram self-bot, `Onlyicing1/Telegram-self-bot` (`origin` = `https://github.com/Onlyicing1/Telegram-self-bot.git`) |
| Target branch | `main` (tracks `origin/main`) |
| Telethon | **`telethon==1.34.0`** (`backend/requirements.txt:1`) |
| Investigation method | Source inspection + the repository's own test suite, executed locally |

---

## 1. Task under investigation

Refactor the Emoji panel so its **primary panel exposes exactly ONE
application-level button** — `🧪 Run Emoji Diagnostics` — which executes a real,
bounded diagnostic workflow and renders an evidence-based trace. Every other
button (send, import, react, library, categories, replacement, helper-bot
tests, direct tests, Back/Home/Close) must be gone from the primary panel.
Existing product capabilities must be preserved.

## 2. What the source actually shows (findings)

### 2.1 The primary panel before this change

`backend/bot/handlers/emoji.py::_emoji_panel_handler` returned **seven**
application-level rows: the diagnostics action plus `✨ Send Premium Emoji`
(`emoji_premium_inline`), `⬇ Import from Saved Messages`, `💬 React to a
message`, and the `📚 Library` / `🗂 Categories` / `🔁 Replacement` sub-panels.
The diagnostic *result* view (`_diagnostics_buttons`) additionally exposed
`💬 React to a message` and `📚 Library`. So the "one button" requirement was
**not** met.

### 2.2 The hidden source of extra buttons — automatic navigation injection

This is the decisive finding. The shared panel renderer
`backend/helper/panels.py::_finalize_panel` appends navigation automatically:

```python
if is_root:
    _add_close_button(builder)          # "❌ Close"   → panel:_nav:close
else:
    _add_nav_buttons(builder)           # "⬅ Back"/"🏠 Home"/"❌ Close"
```

Every callback render goes through `_render_and_edit → _finalize_panel`
(`backend/helper/panels.py:278`) and the reuse path through
`backend/helper/lifecycle.py:215`. Because the Emoji panel is a **sub-panel**
(`register_panel(_MAIN_PANEL, …, parent="menu")`), it is rendered with the
`else` branch — i.e. it silently gained **Back + Home + Close** in addition to
whatever the handler returned.

**Therefore, removing rows from the handler alone is insufficient.** The panel
would still expose three application-level navigation buttons. Any fix that
only edits the handler would fail the strict one-button acceptance test.

### 2.3 There is no alternate UI path for the other capabilities

A repository-wide search for `panel:emoji_library`, `panel:emoji_categories`,
`panel:emoji_replacement`, `action:emoji_import`, `action:emoji_react`,
`emoji_premium_inline`, `emoji_react_premium`, `emoji_premium_direct` found
references **only** inside `backend/bot/handlers/emoji.py`. The AI tool layer
(`backend/ai/tools/`) has no emoji/reaction tool. The mother menu (`Menu`) links
only `panel:emoji`. So today the Emoji sub-panels and the import/react/premium
actions are reachable **only** through the primary Emoji panel's buttons.

### 2.4 The diagnostic workflow already exists and is real

`_diagnostics_action` + `_diagnostics_reply_handler` already run the **real**
production route `premium_emoji_inline_service.send_premium_emoji_via_inline`
and render a trace built by `backend/services/premium_emoji_diagnostics.py` from
the service's own outcome record (stages, statuses, classification, evidence).
Nothing in the trace is simulated. This part needed no change beyond removing
its extra buttons.

### 2.5 The Premium Custom Emoji inline failure (background, unchanged)

The unresolved product failure remains the documented **Telegram bot
custom-emoji entitlement restriction**: the helper bot's correlated inline
answer provably carried the entity, yet Telegram stored the inline result with
the entity removed, so the fail-closed gate refuses before any send. The trace
reports this honestly (`INLINE_BOT_NOT_ENTITLED`, blocked at
`result_inspected`). The historical `reason=no_send_message` label was a
pre-fix wrapper bug (reading `send_message` off the `custom.InlineResult`
wrapper instead of `.result.send_message`) — **not** the current root cause.
This refactor surfaces the real outcome; it does not re-diagnose it.

## 3. Root cause of the one-button failure (confirmed)

1. The handler returned seven rows.
2. `_finalize_panel` auto-injected Back/Home/Close into every non-root render
   of the Emoji sub-panel.
3. There is no per-panel opt-out, so a handler-only edit cannot satisfy the
   strict requirement.

## 4. Hypotheses considered and rejected

* *"Removing rows from `_emoji_panel_handler` is enough."* **Rejected** — the
  automatic nav injection (2.2) still adds three buttons.
* *"`panel_id` is unavailable at finalize time, so the injection cannot be
  conditioned."* **Rejected** — `_finalize_panel(title, body, buttons,
  panel_id, …)` receives `panel_id` on both the callback path and the reuse
  path; `_handle_action` passes the current panel id and `_edit_inline` (the
  diagnostic reply path) bypasses `_finalize_panel` entirely.
* *"A disabled helper is a disconnect."* **Rejected** (per AGENTS.md 4/10) —
  an idle/disabled helper is a valid state and never triggers recovery.

## 5. Evidence gaps (explicitly not verified)

* **Live Telegram:** no `.env` and no credentials exist in this workspace, so
  `backend/config.load()` hard-fails. **No Telegram request of any kind was
  made.** End-to-end acceptance is therefore unverified.
* **Render deployment:** the commit is on `origin/main`, which is the deploy
  source (`render.yaml`/`Procfile`), but the Render build/runtime was not
  inspected from here — treat the deploy as triggered, not confirmed.

## 6. Conclusion

The smallest source-supported fix is: (a) render the primary Emoji panel with
**exactly one** row, (b) add a minimal, explicit **per-panel nav opt-out** in
`_finalize_panel` and mark the Emoji panel nav-free in its `register()`, and
(c) keep every other capability's handler/panel/action registered. See
`IMPLEMENTATION_REPORT.md` for what was implemented and the test results.
