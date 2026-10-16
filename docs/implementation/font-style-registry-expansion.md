# Implementation Report — Display-Font Registry Expansion (2026-10-16)

> **Status:** COMPLETE for the code, tests and documentation on this branch.
> **Source of truth:** produced from direct source inspection at commit
> `34d7dc3` (branch `premium-emoji-attribution`, `= origin/main` at task start,
> clean tree except one pre-existing unrelated modified file). Every claim below
> was checked against the cited file; where a fact could not be verified in this
> workspace it is marked **NOT VERIFIED** rather than assumed.
>
> This file records **only** the Display-Font Registry Expansion feature. It does
> not replace or restate `IMPLEMENTATION_REPORT.md` (the premium-emoji
> direct-send task) or `INVESTIGATION.md` (the same task's latest-only
> investigation) — both remain the canonical records for that work.

---

## 1. Audited architecture (verified)

| Item | Verified fact |
|---|---|
| Registry module | `backend/helper/font_style.py` — the ONE authoritative allow-list |
| Registry type | `_FONT_REGISTRY: dict[str, FontDef]` → `FONT_KEYS: tuple[str, ...]` (ordered, `default` first) |
| Size **before** this change | **23 keys** (`default` + 22 styled) — verified by loading the module |
| Persisted selection | `panel_settings.dashboard_font` (text, default `'default'`) via `settings_service` → `panel_settings_repository` |
| Derived allow-lists | `settings_service.DASHBOARD_FONTS = FONT_KEYS`; `SendMessageTool.parameters["font"]["enum"] = list(FONT_KEYS)`; `task_wizard.valid_font_key()` → `is_valid_font()`; the wizard font prompt lists `FONT_KEYS` |
| Render-time consumers | `backend/helper/panel_render.py::_style` (panel text + button labels), `backend/profile/engine.py` (visible profile field), `backend/ai/tools/message.py` (outgoing task text at send time) |
| Selection UI | Glass UI: `Menu` → Settings → `🔤 Font` (`backend/bot/handlers/misc.py`, `_FONT_PAGE_SIZE = 8`, `action:font_set:<key>`, `action:font_page:prev/next`) |
| Dashboard UI | `src/App.tsx::DASHBOARD_FONT_OPTIONS` → CSS custom property `--app-font` consumed by `src/index.css` |
| Fallback | `normalize_font_key()` → invalid/missing key ⇒ `default`; `settings_service.dashboard_font()` re-validates on read |
| Existing tests | `tests/test_42_dashboard_font.py`, `test_48_font_panel.py`, `test_50_font_system.py`, `test_51_execution27.py`, `test_20_advanced_execution.py`, `test_task_reliability_repair.py`, `test_canonical_schema_reconciliation.py` |

Audit conclusions that shaped the change:

* **All 13 Unicode mathematical alphanumeric letter families and all 5 digit
  runs are already implemented** (bold, italic, bold-italic, script,
  bold-script, fraktur, bold-fraktur, double-struck, sans, sans-bold,
  sans-italic, sans-bold-italic, monospace + bold/double-struck/sans/
  sans-bold/mono digits). No family was missing, so none was duplicated.
* The genuinely missing, technically distinct, fully implementable styles were
  **decorative combining marks** — the registry had only four (underline,
  strikethrough, overline, wavy underline).
* Enclosed-letter additions were **rejected**: `U+1F130` (squared) is
  uppercase-only and `U+1F110` (parenthesized capitals) belongs to the existing
  `parenthesized` key, so extending it would have changed an existing style's
  output. Both would have produced lopsided near-duplicates.
* The dashboard selector was offering two **off-registry** keys (`system`,
  `serif`) — see §3.
* The transformation approach is ASCII-run-based (`_STYLE_RUN` matches
  `[A-Za-z]+` and standalone `[0-9]+`), so Greek/Cyrillic/Arabic input is never
  styled, by construction.

## 2. Implemented — 4 new styles

| Key | Label | Mark | Full output for `Ab Cd 12!` |
|---|---|---|---|
| `double_underline` | Double Underline | `U+0333` COMBINING DOUBLE LOW LINE | `A̳b̳ C̳d̳ 1̳2̳!` |
| `slash_through` | Slashed | `U+0338` COMBINING LONG SOLIDUS OVERLAY | `A̸b̸ C̸d̸ 1̸2̸!` |
| `tilde_overlay` | Tilde Overlay | `U+0334` COMBINING TILDE OVERLAY | `A̴b̴ C̴d̴ 1̴2̴!` |
| `dot_above` | Dotted | `U+0307` COMBINING DOT ABOVE | `Ȧḃ Ċḋ 1̇2̇!` |

* Registry: **23 → 27 keys**. The four keys are **appended** after
  `wavy_underline`, so every pre-existing key keeps its exact position.
* Each is registered through the existing `FontDef` + `_combining()` path with
  `has_digit_glyphs=True` (a per-character mark applies to digits equally, the
  same rule the four existing combining styles already declare).
* No alias and no near-duplicate: every new style's output is distinct from
  every other key's output (asserted by test).
* Integration is **registry-driven only** — no consumer needed a code change:
  the Font panel gains a fourth page automatically (`27 / 8`), the settings
  validator, the AI send-tool enum and the task-wizard validator/preview all
  follow `FONT_KEYS`.

### Files changed

| File | Change |
|---|---|
| `backend/helper/font_style.py` | +4 registry entries (append-only) and one clarified comment on the combining-mark family |
| `src/App.tsx` | `DASHBOARD_FONT_OPTIONS` keys corrected (`system` → `sans`, `serif` → `serif_bold`), CSS stacks unchanged |
| `tests/test_68_font_registry_expansion.py` | **NEW** — 64 focused tests |
| `docs/implementation/font-style-registry-expansion.md` | **NEW** — this report |
| `ROADMAP.md` | one bounded status section (§36) recording this feature's completion |

Deliberately **unchanged**: `DATABASE_ARCHITECTURE.md`, every `supabase/`
migration and `canonical_bootstrap.sql`, `IMPLEMENTATION_REPORT.md`,
`INVESTIGATION.md`, the emoji/AI/runtime subsystems, and the pre-existing
untracked `telegram-self-bot/`, `scratchpad/` and `backend/test_patch_story.md`
(see §7).

## 3. Compatibility

| Guarantee | Evidence |
|---|---|
| Default font unchanged | `FONT_KEYS[0] is DEFAULT_FONT_KEY is "default"`; `apply_font(x, "default") == x` |
| Invalid-key fallback unchanged | `normalize_font_key` on `None/7/""/bogus/<script>` ⇒ `default`; `settings_service.dashboard_font()` still re-validates on read |
| Persisted selections keep working | Keys are append-only; a stored key resolves to the same `FontDef` as before |
| Existing styles byte-identical | Exact-output regression table for all 22 pre-existing styled keys (see tests §6) |
| Existing style order stable | `FONT_KEYS[:23] == ` the pre-expansion order (asserted) |
| Persistence flow unchanged | `set_dashboard_font` → validator → repository → cache refresh; a new key persists and reloads in-session and from a DB row (tested) |
| Selection flow unchanged | The existing `action:font_set:<key>` path selects each new key (tested); pagination extends to 4 pages automatically |
| Backend allow-list derived, not duplicated | `DASHBOARD_FONTS`, the AI tool enum and the wizard validator all resolve to `FONT_KEYS` (tested) |
| Dashboard/backend consistency | Every key in `src/App.tsx` is now a registry key (tested); `system`/`serif` are gone (they were rejected by the validator, so selecting them returned HTTP 400 and the UI reverted — the two stacks are unchanged, only their keys/labels are now real registry keys) |

## 4. Unicode correctness and honest limits

* These styles are **Unicode character transformations, not downloadable fonts**
  and not arbitrary font rendering. Labels say so implicitly by naming the
  decoration, never "font file"/"typeface".
* **Latin letters** are styled in both cases (`A`–`Z`, `a`–`z`); **digits** are
  marked only as standalone runs, so identifiers such as `S0001` or `@user99`
  are never partially restyled.
* **Persian/Arabic, Greek, Cyrillic and every non-ASCII letter** are preserved
  untouched — there is no decorative Unicode block for them, and this feature
  does **not** claim Persian support. Perspective test: `apply_font("سلام", key)
  == "سلام"` for all four new styles.
* Punctuation, whitespace, emoji, markdown markers, inline ``code`` spans and
  URLs pass through unchanged.
* Because a combining mark is added after each styled character, the rendered
  glyph depends on the client font's support for `U+0333`/`U+0338`/`U+0334`/
  `U+0307`; these are standard Combining Diacritical Marks codepoints, but
  their appearance in a given Telegram client is **NOT VERIFIED** here.
* No codepoint was fabricated and no character is mapped to an unrelated
  symbol: each mark is the standard combining decoration for that name.

## 5. Tests

| Check | Command | Result |
|---|---|---|
| New focused file | `.venv/bin/python -m pytest tests/test_68_font_registry_expansion.py -q` | **64 passed**, exit 0 |
| Font-related suites | `pytest tests/test_42_dashboard_font.py tests/test_48_font_panel.py tests/test_50_font_system.py tests/test_51_execution27.py tests/test_20_advanced_execution.py tests/test_task_reliability_repair.py tests/test_canonical_schema_reconciliation.py tests/test_68_font_registry_expansion.py -q` | **209 passed, 23 skipped**, exit 0 (skips are the pre-existing opt-in `live_supabase`/`live_telegram` tests) |
| Full suite | `.venv/bin/python -m pytest tests -q` (exit status captured) | **5958 passed, 27 skipped, 2 warnings in 119.13s**, **EXIT=0** — baseline at `34d7dc3` was 5894 passed / 27 skipped, i.e. exactly the **+64** new tests, **0 regressions** |
| Python compile | `.venv/bin/python -m compileall -q backend/helper/font_style.py tests/test_68_font_registry_expansion.py` | `COMPILE_OK`, exit 0 |
| TypeScript typecheck (frontend edit) | `node_modules/.bin/tsc -p tsconfig.app.json` | exit 0, no diagnostics |
| Whitespace / diff sanity | `git diff --check` | clean, exit 0 |

What the 64 tests cover: registry integrity (default first, unique keys, exact
`23 + 4` count, append-only order, uniqueness of labels, no alias collisions),
exact conversion per new style for case/digits/mixed text, preservation
(punctuation, whitespace, emoji, Persian, Persian digits, code spans, URLs,
IDs), fallback behaviour, byte-exact regression for all 22 pre-existing styled
keys, persistence (write → capture → reload), selection through the existing
Font action, every new key present on the Font panel's pages, the derived
allow-lists (settings, AI send tool enum, wizard), and the dashboard/backend
key consistency check.

## 6. Not verified / limitations

1. **Live Telegram rendering of the four new marks — NOT VERIFIED.** No
   `API_ID`/`API_HASH`/`SESSION_STRING`/`BOT_OWNER_ID` and no `.env` exist in
   this workspace, so no Telegram request of any kind was made. Every test is
   offline against fakes; nothing here claims how a Telegram client draws the
   combining marks.
2. **`panel_settings_dashboard_font_check` enumerates the previous 23 keys.**
   `DATABASE_ARCHITECTURE.md` §27 / `supabase/migrations/20260920000001_*` /
   `supabase/canonical_bootstrap.sql` constrain `dashboard_font` to the
   pre-expansion list. Consequence: on a database where that constraint exists
   and the row is reachable, writing one of the four new keys is rejected by the
   constraint — `panel_settings_repository.update_field()` catches the error and
   returns `False`, `set_setting()` then keeps the value in the session cache
   and returns `True`, so the selection **works for the running process but does
   not survive a restart** until the owner applies the documented manual SQL.
   This task was explicitly forbidden from adding migrations or executing SQL,
   so **`DATABASE_ARCHITECTURE.md` and every `supabase/` file were left
   untouched**; the divergence is reported here instead. Existing keys and the
   default are unaffected.
3. The four new styles are **not** offered by the dashboard CSS selector: that
   selector maps keys to CSS font stacks, which cannot express a Unicode
   character transform. The Glass UI Font panel is the interface that exposes
   all 27 styles.
4. `IMPLEMENTATION_REPORT.md` and `INVESTIGATION.md` were not rewritten: both
   are, by their own headers, the *latest-only canonical record of the previous
   (premium-emoji) task* and are committed at `HEAD`; overwriting them would
   destroy newer unrelated work. This report is the canonical record for this
   feature; `ROADMAP.md` §36 points to it.

## 7. Git

| Step | Value |
|---|---|
| Branch | `premium-emoji-attribution` (tracks `origin/main`; `git rev-list --left-right --count HEAD...origin/main` = `0 0` at task start) |
| Files in this task's commit | `backend/helper/font_style.py`, `src/App.tsx`, `tests/test_68_font_registry_expansion.py`, `docs/implementation/font-style-registry-expansion.md`, `ROADMAP.md` |
| Not committed (pre-existing, unrelated) | `backend/services/premium_emoji_inline_service.py` (modified before this task began), and the untracked `backend/test_patch_story.md`, `scratchpad/`, `telegram-self-bot/` |
| Push | `git push origin HEAD:main` — fast-forward only; no rebase, no force-push, no history rewrite |
| Remote verification | after the push: `git fetch origin` → `git rev-parse origin/main` = the commit, `git merge-base --is-ancestor <commit> origin/main` exit 0 |

A commit cannot contain its own hash; the commit SHA and the verified
`origin/main` SHA are reported in the delivery response, re-checked with the
commands above after the push.
