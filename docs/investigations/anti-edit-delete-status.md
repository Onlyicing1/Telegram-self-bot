# Anti-Edit / Anti-Delete — Investigation Status

> Dedicated investigation record for the Anti-Edit / Anti-Delete feature.
> It was created because the repository's canonical `INVESTIGATION.md` currently
> documents a **different, completed** task (Inline Button Color Semantics,
> commit `2805a88`) and was deliberately **not** overwritten, so its unrelated
> evidence is preserved. See `IMPLEMENTATION_REPORT.md` for the task report and
> `INVESTIGATION.md` for the button-color investigation.

| Item | Value |
|---|---|
| Date | 2026-10-17 |
| Project / remote | LifeOS Telegram self-bot, `Onlyicing1/Telegram-self-bot` |
| Branch / HEAD | `main` @ `330ef39f12dd7b908653af26109d2b218c4f8091` |
| `origin/main` | `330ef39f12dd7b908653af26109d2b218c4f8091` (verified via `git fetch origin` + `git rev-parse origin/main`; `HEAD` is an ancestor) |
| Method | Local source inspection + Git history/ref inspection. No live Telegram, no deployment, no SQL, no Supabase. |
| Live verification | **None.** No credentials/`.env`; no Telegram request of any kind was made. |

---

## 1. Confirmed findings (verified)

### 1.1 The feature is wired but its modules do not exist

* `backend/bot/router.py` **line 20** imports the handler symbol:
  `anti_edit_delete,` inside `from backend.bot.handlers import (...)`.
* `backend/bot/router.py` **line 129** registers it:
  `("anti_edit_delete", lambda: anti_edit_delete.register(client, owner_id, tz_str)),`
* `backend/bot/handlers/__init__.py` contains **only** a comment line
  (`# backend/bot/handlers/__init__.py`) — it re-exports nothing, so the symbol
  must resolve to a submodule `backend/bot/handlers/anti_edit_delete.py`.
* That module **does not exist**:
  `ls backend/bot/handlers/anti_edit_delete.py` → "No such file or directory";
  `find . -iname "*anti_edit*"` (excluding `.git`) → no results.
* A second target is also missing:
  `backend/services/anti_edit_delete_service.py` does not exist, yet
  `backend/runtime/memory_cleanup.py` **line 58** does
  `from backend.services import anti_edit_delete_service` and **line 60** calls
  `await anti_edit_delete_service.cleanup_expired()`.
* `backend/runtime/supervisor.py` **line 51** imports
  `from backend.bot.router import register_all`, so the router (and therefore the
  missing import) is loaded at bot startup.

### 1.2 The missing module is absent from all history and all branches

* `git log --all --diff-filter=A -- backend/bot/handlers/anti_edit_delete.py`
  → no results (never added in any reachable commit).
* `git log --all -S "anti_edit_delete"` returns only the two commits that
  *reference* the name, never one that *creates* the module:
  * `330ef39` — `backend/bot/router.py` (+7 lines)
  * `dfca937` — `backend/runtime/memory_cleanup.py` (+20 lines)
* No remote branch (`feat/ai-model-test-button-…`,
  `feat/emoji-diagnostics-workflow`, `feat/emoji-panel-single-diagnostic-button`,
  `fix/premium-emoji-inline-entitlement-diagnostic`) contains an `anti_edit` file.

### 1.3 Reproduction of the reported startup error

```
$ python -c "from backend.bot.handlers import anti_edit_delete"
ImportError: cannot import name 'anti_edit_delete' from 'backend.bot.handlers'
  (/workspace/project/Telegram-self-bot/backend/bot/handlers/__init__.py)
```

`backend/bot/router.py` itself compiles (`python -m py_compile` clean); the
failure is an import-time name error, not a syntax error.

### 1.4 The menu row claimed by the commit message is absent

* `330ef39`'s message says it "wires the module last on the one shared Telethon
  client; `misc.py` adds the mother-menu row", but the commit changes **only**
  `backend/bot/router.py` (7 insertions).
* `grep -in "anti\|archive" backend/bot/handlers/misc.py` → no matches; the
  mother menu has no archive/anti-edit row.
* `grep -rni "archive" backend/helper/` → no matches (no archive panel/action).

---

## 2. Cause

**Missing-module failure (verified).** The registration/wiring commits
(`dfca937`, `330ef39`) were delivered to `origin/main`, but the modules they
reference — `backend/bot/handlers/anti_edit_delete.py` and
`backend/services/anti_edit_delete_service.py` — were never committed. Because
`supervisor.py` imports the router at startup, the bot raises the `ImportError`
above before any handler registers.

This is **not** a misdirected import path (the package name matches the
repository convention — every sibling name in the same import list is a module in
`backend/bot/handlers/`) and **not** a circular import (the failure is a plain
"name not found").

---

## 3. Inferences (unverified)

* The intended implementation may exist outside this repository (another
  machine, a stash, or the "locally authored `6da5c4f..901e156`" chain named in
  `330ef39`'s message). Those SHAs are **not present** here
  (`git cat-file -t 901e156` → "Not a valid object name";
  `git fetch origin 901e156` → "couldn't find remote ref"). Recorded, not retried.
* Feature scope (capture, archive schema, retention, panel actions) is
  undocumented; the only contract available today is the two call sites:
  `register(client, owner_id, tz_str)` and
  `await cleanup_expired() -> dict` with at least `archive_deleted`,
  `snapshots_deleted`, `objects_deleted` keys.

---

## 4. Unknowns / not verified

* No live Telegram or deployment verification (no credentials, no `.env`, no
  Render shell — none was used).
* No database/Supabase inspection; whether an archive table/migration exists was
  not investigated (not needed to explain the import failure).
* No tests were run for this feature: **no anti-edit test files exist**
  (`grep -rln "anti_edit" tests/` → none), and `pytest`/`telethon` are not
  installed in this workspace.

---

## 5. Blockers

* `import backend.bot.router` cannot run to completion here because `telethon`
  is not installed (`ModuleNotFoundError: No module named 'telethon'`). The
  failing import was reproduced directly and independently of Telethon.
* The authored commits `901e156` / `6da5c4f` are not retrievable from `origin`.

---

## 6. Status and next stage

**Implementation is incomplete and the application cannot start** (the missing
handler import raises at load time). The smallest repair, once the feature intent
is confirmed by the owner, is to add the two missing modules (and, if in scope,
the mother-menu row). Next stage: **Implementation**.
