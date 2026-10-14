# INVESTIGATION.md — Colored Inline Button Styles: Feasibility for the Personal-Account Panel Flow

This file records **only** the current, evidence-based investigation for the
active task and **replaces every previous investigation in full**; earlier
narratives (Emoji Stage 3 auto-reaction engine `9073979`, Emoji Stage 2
selector `9854601`, Emoji Stage 1 `34bed87`/`fbdf9c4`, and the premium-emoji
diagnoses `78d8744`/`798ff4b`/`50b035a`/`4c12ff3`) remain readable in git
history.

| Item | Value |
|---|---|
| Project / remote | LifeOS Telegram self-bot, `Onlyicing1/Telegram-self-bot` (`origin` = `https://github.com/Onlyicing1/Telegram-self-bot.git`) |
| Target branch | `main` (delivery target `origin/main`) |
| Base revision | `90739792183158de953b82cff2378ef1f118c12f` — verified `= origin/main` at task start (`git fetch origin` + `git rev-parse origin/main`) |
| Telethon | **`telethon==1.34.0`**, TL **Layer 173** (`backend/requirements.txt:1`; `telethon/tl/alltlobjects.py` → `LAYER = 173`) |
| Investigation method | Source inspection of the pinned dependency + the current renderer, with **executable local probes**; repository test suite run locally |
| Live Telegram | **NOT performed.** No `.env`, no credentials (`API_ID`/`API_HASH`/`SESSION_STRING`/`BOT_OWNER_ID`/`BOT_TOKEN` all unset); `backend/config.load()` hard-fails. **No Telegram request of any kind was made** |

---

## 1. Task under investigation

Determine whether named colored inline-button styles (`primary`, `success`,
`danger`) can be used by the current **personal-account** self-bot through its
existing inline-panel flow — establishing *actual* compatibility, not merely
schema existence. Four separate claims must be kept distinct: **class/field
existence**, **Telethon construction + serialization**, **Telegram request
acceptance (live)**, and **visible client rendering (live)**.

## 2. Repository state at task start (verified)

* Branch `feat/emoji-panel-single-diagnostic-button`; local HEAD =
  `origin/main` = `9073979`; working tree **clean**. (The checked-out branch is
  not literally named `main` but **is the current `origin/main` head**.)
* No production changes were made by this task (see §5).

## 3. Source findings (verified)

### 3.1 The official MTProto schema *does* define the feature

* `keyboardButtonStyle#4fdd3430 flags:# bg_primary:flags.0?true
  bg_danger:flags.1?true bg_success:flags.2?true icon:flags.3?long =
  KeyboardButtonStyle;` — only three **predefined** background flags
  (primary = dark blue, danger = red, success = green); **no arbitrary
  RGB/HEX** mechanism. (https://core.telegram.org/constructor/keyboardButtonStyle)
* `keyboardButtonCallback#e62bc960 flags:# requires_password:flags.0?true
  style:flags.10?KeyboardButtonStyle text:string data:bytes = KeyboardButton;`
  — the callback button carries an optional `style` field.
  (https://core.telegram.org/constructor/keyboardButtonCallback)
* These constructors are **Layer 225** additions (the page's layer list), i.e.
  newer than the project's pinned Layer 173.

### 3.2 The pinned Telethon (1.34.0 / Layer 173) cannot construct or serialize it

Executable probes against the installed package:

| Probe | Result |
|---|---|
| `hasattr(telethon.tl.types, "KeyboardButtonStyle")` | **False** — the class does not exist |
| `grep -rin style telethon/tl/**` | 0 relevant hits (only an unrelated docstring in `custom/file.py`) |
| `inspect.signature(KeyboardButtonCallback.__init__)` | `(text: str, data: bytes, requires_password: Optional[bool] = None)` — **no `style`** |
| `KeyboardButtonCallback.CONSTRUCTOR_ID` | `0x35bbdb6b` (the **pre-style** id; schema `0xe62bc960` is the styled one) |
| `Button.inline("OK", b"x", style="primary")` | `TypeError: Button.inline() got an unexpected keyword argument 'style'` |
| `KeyboardButtonCallback("OK", b"x", style="primary")` | `TypeError: ... unexpected keyword argument 'style'` |
| `Button.inline("OK", b"payload")._bytes()` | `b'k\xdb\xbb5\x00\x00\x00\x00\x02OK\x00\x07payload'` — flags word carries **only bit 0** (`requires_password`); no `flags.10` exists to set |
| `render(...)` full `InputBotInlineResult._bytes()` contains `b"style"` | **False** |

**Interpretation:** `KeyboardButtonStyle` (class existence) is **absent** from
the pinned version, and the button type has **no `style` field**, so Telethon
1.34.0 **cannot construct or serialize** a styled button. The result would
silently degrade to an ordinary, unstyled button — there is no way to smuggle a
style through this version.

### 3.3 The current renderer cannot preserve a style even if one were attached

`backend/helper/panel_render.py::_normalize_button` (lines 36-56):

* A real `KeyboardButtonCallback` with a different display font is rebuilt via
  `Button.inline(styled, btn.data)` — a fresh **unstyled** object; any style
  attribute is dropped.
* A duck-typed object exposing `text`+`data` is returned **unchanged**, but such
  an object is not a `TLObject` and cannot be serialized into an outgoing
  request, so it cannot carry a style to Telegram either.
* Tuples `("Text", "data")` (the format used throughout the panels) become
  `Button.inline(text, data)` — no style channel at all.

There is **no style parameter, no style field, and no style-preserving path**
anywhere in the renderer.

### 3.4 Sending path (context)

The panels are rendered as inline **results** and clicked through the helper
bot (`backend/helper/inline_engine.py`, `backend/helper/inline_sender.py`).
`reply_markup` on `messages.sendMessage` is documented as "Reply markup for
sending **bot** buttons", and `keyboardButtonCallback` is documented as
"clients send data **to the bot**" — so the style is applied to a
helper-bot-authored inline keyboard, which is the existing, unchanged flow.

## 4. Live verification — blocked

A controlled live test (construct → send from the personal account → observe
rendering → confirm callback routing) **could not be performed**: there is no
authorized session or safe test destination in this workspace (no `.env`, all
required credentials unset; `config.load()` hard-fails). Per the task's Step 2,
the investigation stops after source-level verification and does **not** claim
that colored buttons work.

## 5. Conclusion

* **Class/field existence:** exists in the current **schema** (Layer 225) and
  in newer Telethon (Layer 229 upstream adds `Button.inline(..., style=...)`);
  **absent** from the pinned **1.34.0**.
* **Telethon construction/serialization (pinned):** **not supported** —
  verified by executable probes.
* **Telegram request acceptance (live):** **unverified** (no live test).
* **Visible rendering (live):** **unverified** (no live test).
* **Overall verdict:** **unsupported by the current pinned dependency; the live
  stages remain not-yet-conclusively-verified.** No production change was made
  (Step 3: compatibility not established).

## 6. Exact blocker and next step

**Blocker:** Telethon `1.34.0` (Layer 173) predates the `keyboardButtonStyle`
constructor; it exposes no `KeyboardButtonStyle`, no `style` field, and no
serialization path for it. A dependency upgrade (not authorized here) to a
Telethon release that includes the Layer-225+ keyboard-button-style constructors
would be the prerequisite — followed by an authorized live test to confirm
server acceptance, rendering, and unchanged callback routing.
