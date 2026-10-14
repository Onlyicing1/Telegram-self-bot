# INVESTIGATION.md — Emoji Stage 1: Panel Restructure + Auto-Reaction Setting Entry

This file records **only** the current, evidence-based investigation and
diagnosis for the active task. It **replaces every previous investigation in
full**; earlier narratives (the premium-emoji inline entitlement diagnosis, the
one-button Emoji diagnostic panel) remain readable in git history (`78d8744`,
`798ff4b`, PR #183; `50b035a`, PR #184; `4c12ff3`, PR #185).

| Item | Value |
|---|---|
| Project / remote | LifeOS Telegram self-bot, `Onlyicing1/Telegram-self-bot` (`origin` = `https://github.com/Onlyicing1/Telegram-self-bot.git`) |
| Target branch | `main` (tracks `origin/main`) |
| Base revision | `15fa807e07c429f0870eee3950eccc9c9ae4deb6` — verified `= origin/main` at task start (`git fetch origin` + `git rev-parse origin/main`) |
| Telethon | **`telethon==1.34.0`** (`backend/requirements.txt:1`) |
| Investigation method | Source inspection + the repository's own test suite, executed locally |

---

## 1. Task under investigation

**Stage 1 — Emoji Panel Restructure and Auto-Reaction Settings Entry.** Scope
is limited to (a) restructuring the existing Emoji UI and (b) introducing the
Auto-Reaction setting entry under **General**. The complete automatic reaction
engine and any Telegram API investigation are explicitly **out of scope**.

## 2. Repository state at task start (verified)

* Branch `feat/emoji-panel-single-diagnostic-button`; local HEAD =
  `origin/main` = `15fa807`; working tree **clean**; no untracked files.
* Documents present and read: `AGENTS.md`, `IMPLEMENTATION_REPORT.md`,
  `INVESTIGATION.md`, and the current-feature roadmap `ROADMAP.md` (Emoji &
  Reaction). `ROADMAP.md` §32 status: Phase 0 (code half) + Phases 1–6 done
  offline; Phase 7 (hardening) + live validation remain PLANNED.

## 3. Findings

### 3.1 The "Custom Emoji category" premise does not match the repository

The brief refers to a "Custom Emoji category" in the Emoji panel. A search
across **source, tests, docs, and the full git history** found **no such
literal**. The only near-hit is `INVESTIGATION.md`'s own mention of the
"Premium Custom Emoji inline failure" (a *product* failure, not a UI category).
Consequences:

* There is **no "Custom Emoji category" button, panel, or action** to remove.
* The concept that *does* exist is the ROADMAP §26 **Custom *Category*** — an
  `is_custom=True` composed category type reached through the **Categories**
  panel. Stage 1 must **not** remove it (removing a feature would exceed the
  "restructure" scope).

### 3.2 The Emoji primary panel is already at the Stage 1 target state

`backend/bot/handlers/emoji.py::_emoji_panel_handler` returns **exactly one**
application-level row (`🧪 Run Emoji Diagnostics`), and the panel is registered
nav-free (`set_panel_nav_free(_MAIN_PANEL, True)`), so the shared renderer
`_finalize_panel` injects **no** Back/Home/Close into it. This was delivered by
the prior task (`4c12ff3`) and is pinned by
`tests/test_emoji_diagnostics_ui.py`. Stage 1 therefore **verifies and pins**
this state; it does not re-implement it.

### 3.3 Emoji sub-panels already carry standard navigation

Every non-root Emoji sub-panel (Library, Entry, Categories, Category, Mappings,
Mapping, Picker, Replacement, State-Scope-Picker, Compose, Source-Picker) is
rendered through `_finalize_panel`, which injects Back + Home + Close, wired to
the existing `_handle_navigation` handler. The General panel is likewise a
sub-panel and already receives the same navigation row. This satisfies the
"Back/Home/Close" requirement **by construction**; Stage 1 pins it with tests
and documents the deliberate primary-panel exception.

### 3.4 No Auto-Reaction setting existed

A repository-wide search found **no** auto-reaction references. The General
panel (`backend/bot/handlers/misc.py`) exposed only `Ping`, `Chat & Msg IDs`,
and `Health Dashboard`. The existing settings mechanism is
`backend/services/settings_service.py` (column-per-setting, cached, validated,
Supabase-or-in-memory-fallback via `panel_settings_repository`). It is the
correct, existing home for a new General setting — no parallel store is
justified.

## 4. Root cause / conclusion

* There is **no defect** in the Emoji panel to fix; the panel already meets the
  Stage 1 target, and the "Custom Emoji category" premise has no referent.
* The concrete Stage 1 gap is the **missing Auto-Reaction setting entry under
  General**, which is implemented through the existing `settings_service`.
* Persistence reuses the existing column-per-setting mechanism; the new
  `panel_settings.auto_reaction_enabled` column is **proposed** (documented in
  `DATABASE_ARCHITECTURE.md` §6) and **not applied** — no SQL was executed. The
  setting degrades to the in-memory cache until the owner applies it.

## 5. Evidence — what is verified vs unverified

**Verified (executed locally):**

* Full suite: **5938 passed, 27 skipped** (`pytest -q`); the 27 skips are
  pre-existing opt-in live gates (Supabase / live Telegram / live STT).
* Focused Stage 1 suite: **21 passed**; focused Emoji/General/settings subset:
  **351 passed**.
* `compileall` / `py_compile` clean on the changed/added modules;
  `git diff --check` clean.
* These tests use in-process fakes and make **no** network calls; they confirm
  the contract, not the live system.

**Unverified:**

* **Live Telegram:** no `.env` and no credentials exist in this workspace, so
  `backend/config.load()` hard-fails. **No Telegram request of any kind was
  made.** The General panel render, the toggle persistence, and a reopen
  reflecting the stored value are therefore unverified live.
* **Durable persistence:** the proposed `panel_settings.auto_reaction_enabled`
  column is not applied; the setting runs on the in-memory cache.
* **Deployment:** no Render deployment was inspected for this commit.

## 6. Delivery

* **Branch:** `main` (fast-forward).
* **Commit:** recorded in the delivery block below after the push.

<!-- DELIVERY_BLOCK -->
