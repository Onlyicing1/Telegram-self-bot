# INVESTIGATION.md — Telegram Inline Button Color Semantics

This file records **only** the current, evidence-based investigation for the
active task and **replaces every previous investigation in full**; earlier
narratives (the Unified Functional Unicode System, the colored-button coverage
work `10fce45`/`a6f4cae`, the colored-button feasibility verdict `1396a19`,
Emoji Stages 1-3 `fbdf9c4`/`34bed87`/`9854601`/`9073979`, and the premium-emoji
diagnoses `78d8744`/`798ff4b`/`50b035a`/`4c12ff3`) remain readable in git
history.

| Item | Value |
|---|---|
| Project / remote | LifeOS Telegram self-bot, `Onlyicing1/Telegram-self-bot` (`origin` = `https://github.com/Onlyicing1/Telegram-self-bot.git`) |
| Target branch | `main` (delivery target `origin/main`) |
| Checked-out branch | `feat/emoji-panel-single-diagnostic-button` |
| Base revision | `9c14ca00e1b20dda615d6d16e3f39e1497b426fd` — verified `= origin/main` at task start (`git fetch origin` + `git rev-parse origin/main`) |
| Runtime | Python **3.13** |
| Telethon | **`1.43.0`** (installed from `backend/requirements.txt`) |
| Investigation method | Source inspection of every styled call site across `backend/bot/handlers/*` + the shared `backend/helper/panels.py` builder + the repository test suite run locally |
| Live Telegram | **NOT performed.** No `.env`, no credentials; `backend/config.load()` hard-fails. **No Telegram request of any kind was made** |

---

## 0. Scope reconciliation (why this investigation is not the premium-emoji one)

The task brief named the **Premium Custom Emoji Inline flow** as the unresolved
feature. The current repository state contradicts that framing, and the
evidence is checked here rather than assumed:

* The premium-emoji inline flow is **already implemented** in
  `backend/services/premium_emoji_inline_service.py`, including the
  wrapper-unwrap fix `_resolve_inline_result`, which reads the payload from a
  `custom.InlineResult` wrapper's `.result.send_message` (a raw
  `BotInlineResult`/`BotInlineMediaResult` is accepted too) and distinguishes
  `empty` / `unsupported` / `no_send_message` / entity-missing. Its focused
  suites pass locally: `tests/test_premium_emoji_inline.py`,
  `test_premium_emoji_direct.py`, `test_premium_emoji_probe.py`,
  `test_premium_emoji_self_send_route.py` → **238 passed, 1 skipped** (the skip
  is the opt-in live self-send). The historical
  `INLINE_RESULT_INSPECTED entity_present=False reason=no_send_message` label is
  therefore a *pre-fix* observation; the current source resolves the wrapper and
  inspects the real payload.
* The working tree carries an **uncommitted, unfinished** change set that
  recolors inline buttons — the task actually in progress and the one this
  investigation resolves.
* No separate roadmap entry exists for button colors; the repository's roadmap
  file (`ROADMAP.md`) is the emoji-specific roadmap and is left untouched (this
  work does not invalidate it).

Conclusion: the premium-emoji inline flow is **not** the current unresolved
failure; it is complete at the source/test level (live Telegram acceptance
remains unverified, as it always has been). This task therefore completes the
**button color semantics** correction that is genuinely pending, without
touching the premium-emoji flow.

---

## 1. Reported symptom / objective

The task requires one consistent **button color semantic** across the whole
Glass UI:

* **PRIMARY (blue)** — the default for ordinary actions and navigation,
  **including Save and Delete**. An ordinary delete action is blue, not red.
* **SUCCESS (green)** — reserved for explicit confirmation actions
  (e.g. "Confirm", "Apply Template", "Complete").
* **DANGER (red)** — reserved for destructive/cancel/abort actions
  (e.g. "Confirm Delete", "Cancel").

The prior (now superseded) convention colored a button by the *category* of the
operation: Save was green, Delete was red. That is the defect: colour must
encode the *intent to confirm or cancel*, not the category of the underlying
operation.

## 2. Confirmed root cause (source-supported)

The semantic mapping was inverted at the shared boundary and at individual call
sites:

* `backend/helper/styles.py` documented `SUCCESS` as "safe positive actions
  (Save, approve, confirm-safe)" and `DANGER` as "destructive actions (Delete,
  confirm-delete)" — a category rule, not an intent rule.
* `backend/bot/handlers/misc.py::_build_menu_buttons` gave **Save → SUCCESS**
  and **Delete → DANGER** (and the context panel's Deep Save → SUCCESS).
* `backend/bot/handlers/save.py` gave **Deep Save / Reply Mode → SUCCESS**.
* `backend/bot/handlers/delete.py` gave the delete-mode entry points
  **DANGER** and the explicit **"Confirm Delete" → DANGER** (confirmation was
  red, not green).
* `backend/bot/handlers/bio.py` and `username.py` gave **Clear / Reset →
  DANGER** and **Apply Template → SUCCESS**.
* `backend/bot/handlers/taskloom.py` gave **Complete task → SUCCESS**,
  **Delete task → DANGER**; `todo.py` gave **Complete → SUCCESS**, **Delete →
  DANGER**.
* `backend/bot/handlers/emoji.py`, `retrieve.py`, `ghost_seen_v2.py`,
  `ai_credentials.py` gave the various **Delete / Remove → DANGER** while
  cancellation actions carried **no** colour.

Because the responsible mapping is spread across the shared builder and many
handlers, the correct smallest fix is to correct the **shared semantics
documentation and default** and to re-color each call site to the intent rule.

## 3. Design constraints verified in source

* **Named styles only.** Telegram supports exactly three named button styles
  (`primary`, `success`, `danger`); `backend/helper/styles.py` exposes exactly
  these three constants.
* **Default is PRIMARY.** `backend/helper/panels.py::_styled_button` defaults an
  omitted style to `PRIMARY`, so a caller that omits the argument never emits a
  silently-unstyled button; an unsupported style falls back to unstyled.
* **`extra_rows` now carries an optional style.** `_handle_input` reads
  `label, data, style` (two-tuple rows still accepted) so input-prompt Cancel
  rows can be red.
* **Callback data is never styled.** `panel_render.py` styles only the button
  *text*; colour never changes `data`. Verified by the existing callback-data
  test.
* **Tuple / raw / URL buttons default to PRIMARY** through the same shared
  `_styled_button`/`_url` path.

## 4. Conclusion

* **Root cause:** confirmed — colour was assigned by operation category
  (Save=green, Delete=red) instead of by confirm/cancel intent.
* **Source-level implementation:** complete (see `IMPLEMENTATION_REPORT.md`);
  the shared semantics doc + every styled call site now follow the intent rule.
* **Live Telegram acceptance:** **unverified** — no credentials or safe
  destination in this workspace. No Telegram request was made. The feature is
  **not** claimed live-verified.

## 5. Remaining limitation

Live confirmation must be performed by the owner with real credentials: open the
`Menu` and representative submenus (Save, Delete, Bio/Username builders, Emoji
category delete, AI credentials delete, Taskloom/Todo detail) and confirm the
buttons render with the corrected colours in the Telegram client. That is the
only remaining gap.
