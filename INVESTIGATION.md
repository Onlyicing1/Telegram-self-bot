# INVESTIGATION.md — Colored Inline Buttons: Dependency Selection and Semantic Mapping

This file records **only** the current, evidence-based investigation for the
active task and **replaces every previous investigation in full**; earlier
narratives (the colored-button feasibility verdict `1396a19`, the first
colored-button implementation `3370eae`, Emoji Stage 3 `9073979`, Emoji Stage 2
`9854601`, Emoji Stage 1 `34bed87`/`fbdf9c4`, and the premium-emoji diagnoses
`78d8744`/`798ff4b`/`50b035a`/`4c12ff3`) remain readable in git history.

| Item | Value |
|---|---|
| Project / remote | LifeOS Telegram self-bot, `Onlyicing1/Telegram-self-bot` (`origin` = `https://github.com/Onlyicing1/Telegram-self-bot.git`) |
| Target branch | `main` (delivery target `origin/main`) |
| Base revision | `3370eae982a0785038224a42e6d68884be48c2c5` — verified `= origin/main` at task start (`git fetch origin` + `git rev-parse origin/main`) |
| Runtime | Python **3.13.15** |
| Telethon | **`1.43.0`** (TL Layer 224) — already pinned by the prior task; re-confirmed below as the smallest release with the required API |
| Investigation method | Source inspection of candidate wheels + executable probes against the installed package; full repository test suite run locally |
| Live Telegram | **NOT performed.** No `.env`, no credentials; `backend/config.load()` hard-fails. **No Telegram request of any kind was made** |

---

## 1. Task under investigation

Apply Telegram's three officially supported inline-button styles
(`primary`/blue, `success`/green, `danger`/red) **consistently** across the
existing LifeOS main panel and its first-level submenus, using the existing
shared renderer — without a secondary panel, arbitrary colors, or a parallel
rendering system. The prior task (`3370eae`) had already added the style
channel and colored the main panel's Save/Delete/AI buttons; this task extends
the mapping and makes it consistent and restrained.

## 2. Dependency selection (re-verified)

### 2.1 Smallest Telethon release with the required API

Candidate wheels were inspected (`telethon/tl/alltlobjects.py`,
`telethon/tl/custom/button.py`) to find the earliest release exposing both the
`KeyboardButtonStyle` constructor and the `Button.inline(..., style=...)` API:

| Telethon | TL Layer | `KeyboardButtonStyle` | `Button.inline(style=)` |
|---|---|---|---|
| 1.34.0 (original pin) | 173 | No | No |
| 1.38.0 | 193 | No | No |
| 1.40.0 / 1.41.0 | 214 | No | No |
| 1.42.0 | 216 | No | No |
| **1.43.0** | **224** | **Yes** | **Yes** |
| 1.44.0 | 227 | Yes | Yes |
| 1.45.0 | 229 | Yes | Yes |

**`1.43.0` is the smallest release that supports the feature.** It was already
selected and pinned in `backend/requirements.txt` by `3370eae`;
`Requires-Python: >=3.5`, dependencies unchanged (`pyaes`, `rsa`), so it is
compatible with the project's Python 3.13 and existing usage. No further
dependency change was required for this task.

### 2.2 The API (verified in the installed 1.43.0)

* `Button.inline(text, data=None, style=None, icon=None)`.
* `Button._get_style` accepts exactly `{'primary','danger','success'}` or
  `None`, and raises `ValueError` for anything else (arbitrary RGB/HEX is **not**
  supported — only the three Telegram-defined background flags).
* `KeyboardButtonCallback(text, data, requires_password=None, style=None)`,
  `CONSTRUCTOR_ID = 0xe62bc960`; `KeyboardButtonUrl(text, url, style=None)`,
  `CONSTRUCTOR_ID = 0xd80c25ec`.
* `KeyboardButtonStyle(bg_primary, bg_danger, bg_success, icon)`,
  `CONSTRUCTOR_ID = 0x4fdd3430`.

### 2.3 Serialization behaviour (verified by executable probe)

* A styled button sets `flags.10` and embeds the `KeyboardButtonStyle`; e.g.
  `Button.inline("Save", b"panel:save", style="success")._bytes()` contains a
  `0x400` (bit 10) flags word.
* In this Telethon, `Button.inline` always wraps a `KeyboardButtonStyle` (even
  when no style is requested), so unstyled buttons also set `flags.10` with all
  `bg_*` unset — a valid, semantically-unstyled wire form. A bare
  `KeyboardButtonCallback(...)` without `style` sets no `flags.10`.

## 3. Rendering / callback path (verified)

Panel button definitions flow through `InlinePanelBuilder`
(`backend/helper/panels.py`) → `panel_render._normalize_button` →
`render()` → `InputBotInlineMessageText.reply_markup`. The builder's
`add_row`/`add_buttons` accept an optional trailing `style`; `_styled_button`
→ `panel_render._callback` builds a real `KeyboardButtonCallback` (data already
encoded) with the named style. `_normalize_button` preserves an existing style
through font restyling (`_style_name_from_tl`), so styling survives the
text/font-formatting step. Callback data is never passed through the display
formatter, and URL buttons are untouched.

## 4. Conclusion

* **Dependency compatibility:** confirmed — `1.43.0` provides the constructor,
  the field, and the serialization path (already in place).
* **Source-level implementation:** complete (see `IMPLEMENTATION_REPORT.md`).
* **Live Telegram acceptance / visual rendering / callback delivery:**
  **unverified** — no credentials or safe test destination in this workspace.
  No Telegram request was made. The feature is **not** claimed live-verified.

## 5. Remaining limitation

Live acceptance must be confirmed by the owner with real credentials: send the
existing `Menu` panel (and a submenu) from the account and confirm (a) Telegram
accepts the styled markup, (b) the buttons render blue/green/red in a current
client, and (c) each styled button's callback still routes to the existing
handler.
