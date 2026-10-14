# INVESTIGATION.md — Emoji Stage 2: Functional Auto-Reaction Selector + Custom Category Removal

This file records **only** the current, evidence-based investigation and
diagnosis for the active task. It **replaces every previous investigation in
full**; earlier narratives (the Emoji Stage 1 panel restructure, the
premium-emoji inline entitlement diagnosis, the one-button Emoji diagnostic
panel) remain readable in git history (`34bed87`, `fbdf9c4`, `78d8744`,
`798ff4b`, PR #183; `50b035a`, PR #184; `4c12ff3`, PR #185).

| Item | Value |
|---|---|
| Project / remote | LifeOS Telegram self-bot, `Onlyicing1/Telegram-self-bot` (`origin` = `https://github.com/Onlyicing1/Telegram-self-bot.git`) |
| Target branch | `main` (delivery target `origin/main`) |
| Base revision | `34bed87d80af0e94e25954b3dbac601552bfe14f` — verified `= origin/main` at task start (`git fetch origin` + `git rev-parse origin/main`) |
| Telethon | **`telethon==1.34.0`** (`backend/requirements.txt:1`) |
| Investigation method | Source inspection + the repository's own test suite, executed locally |
| Live Telegram | **NOT performed here.** No `.env`, no credentials; `backend/config.load()` hard-fails. No Telegram request of any kind was made |

---

## 1. Task under investigation

Two deliverables:

1. **Complete the Auto-Reaction settings UI** — the Emoji Stage 1 report
   implemented only a bare ON/OFF toggle under General and left the feature
   without a functional reaction selector.
2. **Remove the "Custom Category" feature** — the brief's "Custom Emoji
   category" premise does not match the repository; the actual referent is
   the ROADMAP **§26 Custom Category** (the `🧩 New custom category` entry
   point, its Compose panel and source-category picker), which is to be
   removed.

## 2. Repository state at task start (verified)

* Branch `feat/emoji-panel-single-diagnostic-button`; local HEAD =
  `origin/main` = `34bed87`; working tree **clean** (no untracked files).
  Note: the checked-out branch is not literally named `main` but **is the
  current `origin/main` head** — HEAD == `origin/main` == `34bed87`. The local
  `main` ref lags at `3159587` (an ancestor).
* Documents read: `AGENTS.md`, `IMPLEMENTATION_REPORT.md`, `INVESTIGATION.md`,
  and the actual roadmap file `ROADMAP.md` (Emoji & Reaction). `ROADMAP.md`
  status: Phase 0 (code half) + Phases 1–6 done offline; Phase 7 (hardening)
  + live validation remain PLANNED.
* Files inspected per the brief: `backend/services/premium_emoji_inline_service.py`,
  `backend/helper/inline_engine.py`, `backend/bot/handlers/emoji.py`,
  `tests/test_premium_emoji_inline.py`, `backend/requirements.txt`. The
  Premium Custom Emoji Inline flow is **not** the current task; it was
  previously diagnosed as a bot-entitlement restriction (`78d8744`) and no
  code change is made to it here.

## 3. Findings

### 3.1 "Custom Emoji category" == ROADMAP §26 Custom Category

An exhaustive search (source, tests, docs, full git history) found **no literal
"Custom Emoji category"**. The Emoji Stage 1 report itself named the referent
as the "Custom *Category*". `ROADMAP.md` §26 documents the feature exactly:

> `## 26. Custom Category` … `UI: categories panel → "🧩 New custom category"
> (name input) → compose panel (ordered source list, add/remove, Compose,
> Refresh, Mappings) → 2×5 source picker …`

The concrete surface to remove was mapped precisely:

* **UI** (`backend/bot/handlers/emoji.py`): the `🧩 New custom category`
  button, the compose/source-picker panels and their handlers/actions.
* **Service** (`backend/services/emoji_category_service.py`): the composition
  API (`compose_category`, `refresh_category`, `plan_composition`,
  `category_sources`, `is_custom_category`) and the `E_*` composition error
  codes.
* **DB** (`backend/db/client.py`): `set_emoji_category_sources` — exclusively
  custom-category infrastructure.

### 3.2 The Auto-Reaction setting was incomplete

`settings_service` exposed `auto_reaction_enabled` (Stage 1) but there was
**no selectable reaction value** and **no Telegram-sourced catalog**. The
General panel had only the toggle. There was no way for the owner to choose
which reaction the (future) engine would apply, and no code path read
Telegram's own available-reaction list.

## 4. Diagnosis (evidence-based)

* **Custom Category removal** is a pure application-level subtraction. The
  feature's physical schema (`emoji_categories.is_custom` /
  `source_category_ids`, migration `20260928000001_create_emoji_reaction_tables.sql`)
  is **MANUAL-ONLY** and unexecuted; dropping columns is destructive DDL that
  is out of scope. The correct, minimal action is to remove the UI/service/db
  **code** and leave the physical columns untouched (nothing reads or writes
  them). The canonical setup block is therefore left byte-identical.
* **The reaction selector** needs a real source. Telegram exposes the account's
  catalog through `messages.GetAvailableReactionsRequest`
  (`telethon.tl.functions.messages`), answered by
  `telethon.tl.types.messages.AvailableReactions` (with `reactions:
  list[AvailableReaction]` and a `hash`) or `AvailableReactionsNotModified`
  (catalog unchanged). Each `AvailableReaction` carries `reaction` (the exact
  emoji string `ReactionEmoji` expects back), `title`, `premium`, `inactive`.
  This is the **only** legitimate source — no hardcoded list and no Unicode
  catalog substitute.

## 5. Scope decisions

* **No new database.** The selected reaction is persisted through the
  **existing** `settings_service` (`panel_settings`) path as a new column
  `auto_reaction_emoji`, documented in `DATABASE_ARCHITECTURE.md` for the
  owner to apply manually (same graceful-degradation contract as every other
  `panel_settings` column). No SQL is executed.
* **No second infrastructure.** The catalog read is one typed request through
  the existing self client, wrapped by the existing `guarded_await` watchdog;
  the picker is a normal sub-panel of General (no new listener/scheduler/
  executor/update loop).
* **Fail closed.** An empty catalog, an unsupported response, or an
  out-of-range selection index is reported honestly and never fabricated.

## 6. Unresolved / unverified

* Live Telegram is **not** exercised here. The catalog read, the picker, and
  the persisted selection are verified offline against a fake self client.
* The `panel_settings.auto_reaction_emoji` column is MANUAL-ONLY and
  unapplied; on a configured Supabase without it, the write is a no-op and
  the setting runs on the in-memory cache (documented degradation).
* The reaction engine that would consume the setting remains deferred/not
  implemented (unchanged from Stage 1).
