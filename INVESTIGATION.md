# INVESTIGATION.md — Colored Inline Buttons: Shared-Path Coverage Diagnosis

This file records **only** the current, evidence-based investigation for the
active task and **replaces every previous investigation in full**; earlier
narratives (the partial colored-button work `10fce45`/`3370eae`, the
colored-button feasibility verdict `1396a19`, Emoji Stage 3 `9073979`, Emoji
Stage 2 `9854601`, Emoji Stage 1 `34bed87`/`fbdf9c4`, and the premium-emoji
diagnoses `78d8744`/`798ff4b`/`50b035a`/`4c12ff3`) remain readable in git
history.

| Item | Value |
|---|---|
| Project / remote | LifeOS Telegram self-bot, `Onlyicing1/Telegram-self-bot` (`origin` = `https://github.com/Onlyicing1/Telegram-self-bot.git`) |
| Target branch | `main` (delivery target `origin/main`) |
| Base revision | `10fce4556be5712105f35d452b27c94386dc594f` — verified `= origin/main` at task start (`git fetch origin` + `git rev-parse origin/main`) |
| Runtime | Python **3.13.15** |
| Telethon | **`1.43.0`** (TL Layer 224) |
| Investigation method | Source inspection of the shared construction/render paths + executable probes (registry-wide render sweep) against the installed package; full repository test suite run locally |
| Live Telegram | **NOT performed.** No `.env`, no credentials; `backend/config.load()` hard-fails. **No Telegram request of any kind was made** |

---

## 1. Reported symptom

The owner tested the deployed build and reported that **only a few buttons**
showed color; the rest were unchanged. The prior implementation was therefore
incomplete, not merely unverified.

## 2. Confirmed root cause (source-supported)

The style channel added by `10fce45` was **opt-in only**. The shared paths that
most buttons actually flow through assigned **no default style**:

* `InlinePanelBuilder._styled_button` forwarded `style=None` for any caller
  that omitted it.
* `panel_render._normalize_button` built tuple buttons and raw
  `KeyboardButtonCallback` objects without a style.
* The auto-injected `⬅ Back` / `🏠 Home` / `❌ Close` rows, the transient
  `⌂ Home` progress row, and `InlinePanelBuilder.add_url` were all unstyled.
* Every other panel family passed no style at all.

This was verified by an executable registry-wide sweep: before the fix, a
render of the registered panels left most buttons with no `bg_*` flag; after
the fix, **0** unstyled buttons remain.

## 3. Why the fix belongs in the shared paths

Because the responsible code is the shared builder/renderer — not individual
handlers — the correct, smallest maintainable change is a **default style at
the shared boundary** (`_normalize_button` and `_styled_button`), with explicit
`success`/`danger` overrides preserved for semantic actions. Adding more
per-handler arguments would have left the same gap for every future button.

## 4. Telethon capability re-verified (installed 1.43.0)

* `Button.inline(text, data=None, style=None, icon=None)` → `KeyboardButtonCallback` (`0xe62bc960`).
* `Button.url(text, url=None, style=None, icon=None)` → `KeyboardButtonUrl` (`0xd80c25ec`) — **URL buttons support a style** in this version, so they are styled rather than excepted.
* `KeyboardButtonStyle(bg_primary, bg_danger, bg_success, icon)` (`0x4fdd3430`).
* Only `primary` / `success` / `danger` are accepted; anything else raises `ValueError` (arbitrary RGB/HEX unsupported).

## 5. Conclusion

* **Coverage root cause:** confirmed — the shared construction/render paths did
  not assign a default style.
* **Source-level implementation:** complete (see `IMPLEMENTATION_REPORT.md`);
  the exhaustive test asserts every registered panel's every button is styled.
* **Live Telegram acceptance / rendering / callback delivery:** **unverified**
  — no credentials or safe destination in this workspace. No Telegram request
  was made. The feature is **not** claimed live-verified.

## 6. Remaining limitation

Live acceptance must be confirmed by the owner with real credentials: send the
`Menu` panel and submenus from the account and confirm (a) Telegram accepts the
styled markup, (b) green/red/blue render as mapped, and (c) callbacks still
route to the existing handlers.
