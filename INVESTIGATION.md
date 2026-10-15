# INVESTIGATION.md — Unified Functional Unicode System (Glass UI + Taskloom)

This file records **only** the current, evidence-based investigation for the
active task and **replaces every previous investigation in full**; earlier
narratives (the colored-inline-button feasibility verdict `1396a19`, the
colored-button coverage work `10fce45`/`a6f4cae`, Emoji Stages 1-3
`fbdf9c4`/`34bed87`/`9854601`/`9073979`, the premium-emoji diagnoses
`78d8744`/`798ff4b`/`50b035a`/`4c12ff3`, and the first Unicode-migration commit
`68c9cec`) remain readable in git history.

| Item | Value |
|---|---|
| Project / remote | LifeOS Telegram self-bot, `Onlyicing1/Telegram-self-bot` (`origin` = `https://github.com/Onlyicing1/Telegram-self-bot.git`) |
| Target branch | `main` (delivery target `origin/main`) |
| Checked-out branch | `feat/emoji-panel-single-diagnostic-button` |
| Base revision | `68c9cec58643e0d69f3327b1088573b7bf47466d` — verified `= origin/main` at task start (`git fetch origin` + `git rev-parse origin/main`) |
| Runtime | Python **3.13** |
| Telethon | **`1.43.0`** (installed from `backend/requirements.txt`) |
| Investigation method | Source inspection of the panel/Taskloom rendering paths + an executable AST-based glyph scan over `backend/` + the full repository test suite run locally |
| Live Telegram | **NOT performed.** No `.env`, no credentials; `backend/config.load()` hard-fails. **No Telegram request of any kind was made** |

---

## 0. Scope reconciliation (why this investigation is not the premium-emoji one)

The task brief named the **Premium Custom Emoji Inline flow** as the unresolved
feature. The current repository state contradicts that framing, and the
evidence is checked below rather than assumed:

* The premium-emoji inline flow is **already implemented** in
  `backend/services/premium_emoji_inline_service.py` (including the
  wrapper-unwrap fix `_resolve_inline_result`, which reads the payload from a
  `custom.InlineResult` wrapper's `.result.send_message` — not from the
  wrapper itself), and its focused suites pass locally:
  `tests/test_premium_emoji_inline.py`, `test_premium_emoji_direct.py`,
  `test_premium_emoji_probe.py`, `test_premium_emoji_self_send_route.py` ->
  **284 passed, 1 skipped** (the skip is the opt-in live self-send).
* The canonical reports on disk described the **colored-inline-button** work,
  and the working tree carries the **Unified Functional Unicode System**
  migration across the panels and Taskloom — the task actually in progress.
* No separate roadmap entry exists for the Unicode system; the repository's
  roadmap file (`ROADMAP.md`) is the emoji-specific roadmap and is left
  untouched (the Unicode work does not invalidate it).

Conclusion: the premium-emoji inline flow is **not** the current unresolved
failure; it is complete at the source/test level (live Telegram acceptance
remains unverified, as it always has been). This task therefore completes the
**Unicode symbol system** that is genuinely pending, without touching the
premium-emoji flow.

---

## 1. Reported symptom / objective

The panel system and the Taskloom task system rendered their functional
symbols (task-state icons, schedule icons, nav arrows, pagination, wizard
marks, success/failure prefixes, provider/model marks) as ad-hoc, per-handler
string literals. The same semantic could render with different glyphs in
different panels, some glyphs were full emoji (e.g. `⚙`, `❌`, `📄`), and there
was no single place to change or audit the convention. The objective: **one
authoritative, deterministic Unicode symbol registry** plus a **consistent
button-label UX** (`"<glyph> <text>"`) wired through every shared path —
without changing any callback/action data, layout, or font behavior.

## 2. Confirmed root cause (source-supported)

There was **no central registry**. Symbols were hard-coded literals scattered
across handlers:

* `backend/bot/handlers/taskloom.py` kept private `_STATUS_ICON` /
  `_SCHEDULE_ICON` dicts and literal panel titles (`▦ Taskloom`,
  `＋ New task`); `todo.py` used inline literals (`▶️`, `✅`, `➕`, `❌`).
* `backend/helper/panels.py` shared nav/progress builders emitted literal
  `⬅ Back` / `🏠 Home` / `❌ Close`.
* `backend/bot/handlers/misc.py`, `ai.py`, `bio.py`, `username.py`, `emoji.py`,
  `ghost_seen_v2.py` each hard-coded their own nav/action glyphs and status
  marks.
* `backend/ai/task_management_interface.py` kept an emoji `_STATUS_LABELS`
  table; `backend/ai/task_wizard.py` kept emoji action titles.
* `backend/ai/engine/dispatcher.py`, `backend/ai/tools/delivery.py`, and
  `backend/bot/handlers/ai_stt_settings.py` still carried full-color emoji
  (`❌`, `📄`, `⚙`) in owner-facing notices.

Because the responsible literals are spread across many handlers, the correct,
smallest maintainable fix is a **single registry consumed by every path** —
not more per-handler literals.

## 3. Why the registry (and not per-handler edits)

A shared module (`backend/helper/symbols.py`) gives: one glyph per semantic,
a documented reuse policy, deterministic pure-lookup helpers, and an
enforceable invariant (tests prove no undocumented duplicate exists and no
full-color emoji remains). This is the same "fix the shared path" reasoning
the earlier colored-button task used — the shared boundary, not individual
call sites, is where consistency must live.

## 4. Design constraints verified in source

* **Font pipeline** (`backend/helper/font_style.py`): transforms only Latin
  letters and standalone digit runs; its own styled digit tables are data, not
  UI glyphs, and are exempt from the glyph scan. Registry glyphs must
  therefore avoid emoji and the `U+FE0F` variation selector. The registry uses
  abstract marks only.
* **Callback data is never styled**: `backend/helper/panel_render.py` styles
  only the button *text*; the registry changes labels, never `data`. Verified
  by test.
* **Fail-safe lookups**: `status_glyph` / `schedule_glyph` / `content_glyph`
  return a safe default for an unknown value (no KeyError, no silent emoji).
* **Legacy service-failure prefixes** (`backend/ai/tools/base.py`) are kept on
  purpose so a service that has not yet moved to the registry still reports
  failure honestly; that documented literal set is exempt from the scan.

## 5. Conclusion

* **Root cause:** confirmed — no central registry; symbols were scattered
  literals, several of them full-color emoji.
* **Source-level implementation:** complete (see `IMPLEMENTATION_REPORT.md`);
  the shared nav/progress/input paths, Taskloom, Todo, General/Settings,
  Bio/Username, Emoji, Ghost Seen, the AI panels (provider/model/health/usage/
  diagnostics/credentials/STT/TTS), and the AI task surfaces now consume the
  registry; `tests/test_symbols.py` enforces the convention and proves **zero
  full-color emoji literals remain** in production code.
* **Live Telegram acceptance:** **unverified** — no credentials or safe
  destination in this workspace. No Telegram request was made. The feature is
  **not** claimed live-verified.

## 6. Remaining limitation

Live confirmation must be performed by the owner with real credentials: open
the `Menu` and representative submenus and confirm the glyphs/labels render as
intended in the Telegram client. That is the only remaining gap.
