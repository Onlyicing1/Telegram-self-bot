# INVESTIGATION.md — Colored Inline Buttons: Dependency Selection and Implementation Basis

This file records **only** the current, evidence-based investigation for the
active task and **replaces every previous investigation in full**; earlier
narratives (the colored-button feasibility verdict `1396a19`, Emoji Stage 3
`9073979`, Emoji Stage 2 `9854601`, Emoji Stage 1 `34bed87`/`fbdf9c4`, and the
premium-emoji diagnoses `78d8744`/`798ff4b`/`50b035a`/`4c12ff3`) remain readable
in git history.

| Item | Value |
|---|---|
| Project / remote | LifeOS Telegram self-bot, `Onlyicing1/Telegram-self-bot` (`origin` = `https://github.com/Onlyicing1/Telegram-self-bot.git`) |
| Target branch | `main` (delivery target `origin/main`) |
| Base revision | `1396a19eac7a9bb2c379992e22923fbcd1862acf` — verified `= origin/main` at task start (`git fetch origin` + `git rev-parse origin/main`) |
| Runtime | Python **3.13.15** |
| Telethon | **upgraded `1.34.0` → `1.43.0`** (TL Layer 173 → 224) |
| Investigation method | Source inspection of candidate wheels + executable probes against the installed package; full repository test suite run locally |
| Live Telegram | **NOT performed.** No `.env`, no credentials; `backend/config.load()` hard-fails. **No Telegram request of any kind was made** |

---

## 1. Task under investigation

Implement Telegram's named colored inline-button styles (`primary`, `success`,
`danger`) in the **existing** LifeOS main panel, through the existing shared
renderer — provided dependency compatibility and regression checks pass. The
prior task (`1396a19`) established that the pinned `telethon==1.34.0` (Layer 173)
has no `KeyboardButtonStyle`, no `style` field, and no serialization path; this
task resolves the dependency question and implements the feature.

## 2. Dependency selection (the specific compatibility question)

### 2.1 Smallest Telethon release with the required API

Candidate wheels were downloaded and inspected (`telethon/tl/alltlobjects.py`,
`telethon/tl/custom/button.py`) to find the earliest release exposing both the
`KeyboardButtonStyle` constructor and the `Button.inline(..., style=...)` API:

| Telethon | TL Layer | `KeyboardButtonStyle` | `Button.inline(style=)` |
|---|---|---|---|
| 1.34.0 (pinned) | 173 | No | No |
| 1.38.0 | 193 | No | No |
| 1.40.0 | 214 | No | No |
| 1.41.0 | 214 | No | No |
| 1.42.0 | 216 | No | No |
| **1.43.0** | **224** | **Yes** | **Yes** |
| 1.44.0 | 227 | Yes | Yes |
| 1.45.0 | 229 | Yes | Yes |

**`1.43.0` is the smallest release that supports the feature** and was selected.
`Requires-Python: >=3.5`, dependencies unchanged (`pyaes`, `rsa`), so it is
compatible with the project's Python 3.13 and existing usage.

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
  `Button.inline("Save", b"panel:save", style="primary")._bytes()` begins
  `b'`\xc9+\xe6\x00\x04\x00\x00'` (`0x400` = bit 10 set).
* In this Telethon, `Button.inline` always wraps a `KeyboardButtonStyle` (even
  when no style is requested), so unstyled buttons also set `flags.10` with all
  `bg_*` unset — a valid, semantically-unstyled wire form. A bare
  `KeyboardButtonCallback(...)` without `style` sets no `flags.10`.

### 2.4 Regression check of the upgrade

The **full suite** was run against `telethon==1.43.0` before any code change:
**5913 passed, 27 skipped** — identical to the recorded `1.34.0` baseline, i.e.
**no regression** from the upgrade. The suite was run again after the code
change (see `IMPLEMENTATION_REPORT.md`).

## 3. Implementation basis (current renderer)

`backend/helper/panel_render.py::_normalize_button` normalizes tuples and
`Button` objects. The prior version rebuilt a font-restyled button via
`Button.inline(styled, btn.data)` — dropping any style — and had no style
channel. This task adds a minimal style path (see `IMPLEMENTATION_REPORT.md`)
that preserves the selected style through normalization, display-text
formatting, inline-result construction, and the existing callback path.

## 4. Conclusion

* **Dependency compatibility:** confirmed — `1.43.0` provides the constructor,
  the field, and the serialization path; the upgrade caused no test regression.
* **Source-level implementation:** complete (see `IMPLEMENTATION_REPORT.md`).
* **Live Telegram acceptance / visual rendering / callback delivery:**
  **unverified** — no credentials or safe test destination in this workspace.
  No Telegram request was made. The feature is **not** claimed live-verified.

## 5. Remaining limitation

Live acceptance must be confirmed by the owner with real credentials: send the
existing `Menu` panel from the account and confirm (a) Telegram accepts the
request, (b) Save/Delete/AI render green/red/blue in a current client, and
(c) each styled button's callback still routes to the existing handler.
