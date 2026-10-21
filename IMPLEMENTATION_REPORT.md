# IMPLEMENTATION_REPORT.md — AED archive scope + Private Message schedule restoration

**Phase:** archive correctness (Anti-Edit / Anti-Delete) and scheduled-policy
restoration (Private Message Management), delivered on top of the Cipher
navigation phase.
**Status:** code and tests COMPLETE and verified offline. **No SQL was executed
and no Supabase schema, table, column or policy was modified.** No live Telegram
account was exercised, so live rendering/enforcement remains unverified (§9).

This report is the current-state report for the two defects below; it replaces
the previous cumulative report as required by the task.

---

## 1. Defects addressed

1. **AED archive visibility and scope.** The archive displayed ordinary,
   unchanged private messages as archive entries, because every captured
   message created a visible snapshot item; and an unresolved peer could be
   admitted as a human private chat purely on the strength of a positive chat
   id.
2. **PV Management schedule policy and restoration.** The schedule had to apply
   the owner's selected restricted policy exactly and had to restore the exact
   pre-schedule mode afterwards (never silently open, never lose the saved
   preference across a restart). The investigation found the evaluator already
   non-destructive, but the rule builder named the exceptions-only policy
   "Closed" (ambiguous) and no test covered the transition matrix, repeated
   evaluation or restart. Both were fixed/pinned in this round.

---

## 2. Repository state before editing

- Repository `Onlyicing1/Telegram-self-bot`, branch `main`, baseline commit
  `46ab0ff2aed0835dc41fcde1ffb02f74ec058e25` (= `origin/main`, verified with
  `git fetch` + `git rev-parse`). All work was done in a detached worktree at
  that commit.
- The shared checkout's pre-existing branch and working-tree changes
  (`INVESTIGATION.md`, the untracked `.aedwork/`, `.m14/`, `.taskrt/`
  directories) were never touched, staged or committed.
- No dependency, environment variable, Telegram session, scheduler, executor or
  update loop was changed.

---

## 3. Defect 1 — AED showed unchanged messages and admitted unverified peers

### 3.1 Behaviour before

- Every eligible `NewMessage` was stored as `KIND_SNAPSHOT`, and
  `build_items()` emitted a list item for every snapshot/edit group. An
  ordinary private message therefore appeared in **Browse archive** even though
  no edit or deletion had been observed, and its detail page reported one
  observed version.
- The capture gate (`eligible`) rejected a confirmed bot but accepted
  `peer_is_bot=None`, i.e. a peer whose entity was missing, unresolvable or not
  a `User`. The handler also degraded entity-resolution failures to
  `peer_is_bot=False`, which the gate treated as "not a bot" rather than
  "unknown".
- At browse/detail time, `filter_bot_items` hid only peers that resolved to a
  bot; an unresolvable peer was kept visible (the documented "safe fallback").

### 3.2 Behaviour after

- A snapshot is an **internal recovery record only**. `build_items()` creates a
  content item only when the message has an observed `KIND_EDIT`; a deletion is
  still its own item, and only an actual `MessageDeleted` event can create one.
  An unchanged message is not listed, not paginated, not counted, not returned
  by detail lookup and not matched by category/query filters.
- The snapshot row remains stored, and is included in the visible item's
  `versions` once an edit makes that item visible, so an original is still
  recoverable. A deletion without an earlier capture remains an honest
  `recoverable=False` row with no fabricated body.
- Capture is fail-closed on a **confirmed tri-state**:
  - `True` = a resolved Telegram `User` with `bot=True` → rejected;
  - `False` = a resolved Telegram `User` with `bot=False` → eligible;
  - `None` = missing/unresolved peer, non-`User` entity, missing `bot` flag →
    rejected.
  A positive chat id alone is never accepted as proof of a human. The same
  strict check is re-proved at browse/detail against the live self client; a
  persisted row that cannot be confirmed is hidden (and counted in the panel's
  "hidden" note), never deleted by the filter.
- Groups, supergroups, channels, Saved Messages, inline-bot-origin messages,
  resolved bots and unresolved peers are excluded at both boundaries.

### 3.3 Where the change lives

- `backend/services/anti_edit_delete_service.py`
  - `eligible()` now requires `peer_is_bot is False` and returns
    `peer_unresolved` otherwise.
  - `build_items()` skips snapshot-only groups (storage unchanged).
  - `_resolve_peer_bot()` caches a human **only** for a resolved `User` with a
    real boolean `bot` flag; a non-`User` object or a missing flag is UNKNOWN
    and takes the existing bounded cooldown.
  - new `peer_human_status()` and `filter_scope_items()` replace
    `filter_bot_items()`; both fail closed (invalid/negative id, the owner's
    own id, no client, bot, unresolved, cooldown).
- `backend/bot/handlers/anti_edit_delete.py`
  - `_resolve_peer_identity()` returns the confirmed tri-state; all three
    capture handlers skip unless it is `False`.
  - list/detail use `filter_scope_items()`; the main panel states that only
    observed edits/deletions are listed.
- Recovery, idempotency (`already_handled`), owner scoping, bounded storage,
  category filters, media metadata and retention cleanup are unchanged.

---

## 4. Defect 2 — Schedule policy selection and exact restoration

### 4.1 What the inspection found

`resolve_mode()` computes the effective policy as
**temporary override → schedule → manual mode**, purely: a schedule never
writes `state["mode"]`, and `state["mode"]` is the owner's durable manual
preference persisted in the whole `private_message_config` document. Activation,
re-evaluation, rule switching and restart therefore already restored the exact
prior mode rather than Open. The real gaps were:

- the schedule rule builder rendered `MODE_CLOSED` as `"Closed"` (via
  `MODE_LABELS[...].split(" (")[0]`), which reads as "closed to everyone"
  instead of the exceptions-only policy the constant represents; and
- no test pinned the four transition cases, repeated evaluation, restart,
  disablement, consecutive-rule switching, or enforcement under a scheduled
  policy.

### 4.2 Behaviour after

- The rule builder names the canonical choices explicitly:
  **Open (everyone)**, **Whitelist only**, **Exceptions only**. The stored value
  is unchanged (`open` / `whitelist_only` / `closed`); no schema or state-shape
  change was needed.
- The schedule remains a temporary override of the saved mode:

  | Mode before schedule | Scheduled policy | During | After |
  |---|---|---|---|
  | Whitelist only | Exceptions only | Exceptions only | Whitelist only |
  | Exceptions only | Whitelist only | Whitelist only | Exceptions only |
  | Open | Whitelist only | Whitelist only | Open |
  | Open | Exceptions only | Exceptions only | Open |

- Evaluations never write state: a schedule that stays active does not replace
  the saved preference, and a restart during the window reloads the same
  preference and still resolves the scheduled policy as effective.
- Disabling the schedule, ending the window or switching to a non-matching rule
  falls back to the saved mode deterministically; switching between consecutive
  rules never restores an intermediate value. Manual `set_mode()` outside an
  active schedule persists normally.
- Enforcement (`decide_incoming`) consumes the effective policy: whitelist-only
  admits only the allow-list, exceptions-only admits only the exceptions list,
  and neither is widened by the underlying manual mode.
- No scheduler, polling loop, executor or lifecycle change was made; no new
  persisted field was introduced.

### 4.3 Where the change lives

- `backend/bot/handlers/private_message.py` — `_RULE_MODE_LABELS` and its use in
  the rule body/button. The default draft, mode cycle, validation, persistence
  and evaluator are untouched.

---

## 5. Files changed

| File | Why |
|---|---|
| `backend/services/anti_edit_delete_service.py` | Confirmed-human scope gate, snapshot-only visibility rule, human/bot classification and the strict browse/detail filter |
| `backend/bot/handlers/anti_edit_delete.py` | Tri-state peer classification in all three capture handlers, strict list/detail filtering, honest panel copy |
| `backend/bot/handlers/private_message.py` | Explicit schedule policy names (Open / Whitelist only / Exceptions only) |
| `tests/test_anti_edit_delete.py` | Updated the old fallback assertions, resolved-human fixtures, and new visibility/scope/recovery tests |
| `tests/test_private_message_management.py` | Transition matrix, repeated evaluation, restart, disable, consecutive rules, manual change, enforcement |
| `tests/test_private_message_panel.py` | Rule builder names each policy exactly |
| `IMPLEMENTATION_REPORT.md` | This report |

No migration, SQL, `DATABASE_ARCHITECTURE.md`, Cipher navigation, settings
default, permission or unrelated feature was changed.

---

## 6. Verification actually executed

### 6.1 Focused

| Command | Result |
|---|---|
| `pytest tests/test_anti_edit_delete.py -q` | **109 passed** |
| `pytest tests/test_private_message_management.py -q` | **51 passed** |
| `pytest tests/test_private_message_panel.py -q` | **51 passed** |
| `pytest tests/test_private_message_scope.py tests/test_private_message_db_security.py tests/test_cipher_category.py tests/test_aed_db_security.py -q` | **173 passed** |

The AED module now proves, among others: a snapshot-only human message is
stored but is not listed/detailed/counted; an observed edit makes the item
visible with the original and edit versions; a deletion is listed only after
the deletion event; an unrecoverable deletion has an empty body and honest
`recoverable=False`; the snapshot still backs a later deletion; groups,
channels, Saved Messages, bots, inline-bot-origin and unresolved/non-`User`
peers are excluded at capture; persisted non-private/unverified rows are hidden
from the list and detail; pagination, category filters, grouping, retention,
owner isolation, idempotency and persistence tests continue to pass.

The PV modules prove all four transition cases (plus Open→Whitelist and
Whitelist→Open), that activation never opens or widens the policy, that six
consecutive evaluations write nothing, that a restart while active reloads the
exact prior mode and still applies the schedule, that disablement restores it,
that consecutive rules never leak an intermediate mode, that manual changes
outside a schedule persist, and that whitelist/exceptions enforcement uses the
effective scheduled policy.

### 6.2 Full suite and static checks

- `pytest -q` → **6540 passed, 27 skipped, 3 warnings in 126.31s**
  (CPython 3.10.12, Telethon 1.43.0 as pinned).
- `python -m py_compile` on the three changed modules and three changed test
  modules → OK.
- `git diff --check` → clean (see the delivery record in the final task
  response).

### 6.3 Not run

- No live Telegram/helper-bot render, capture, edit, deletion or schedule
  transition was executed against a real account.
- No browser/dashboard check is relevant (no web surface changed).
- No SQL or Supabase call was made (deliberately; none is required).

---

## 7. Persistence and database boundaries

- Defect 1 is code-only: it changes which stored rows become visible and which
  peers are eligible; the archive schema, row shape, retention and media storage
  contract are unchanged.
- Defect 2 is code-only: the schedule was already stored inside the existing
  configuration document, and the saved `mode` remains the durable underlying
  preference. No new column, table, migration or document field was added.
- No SQL was executed and no Supabase project was contacted or modified.

---

## 8. Delivery

The round is delivered as one commit on `main` and pushed to the repository's
configured remote; the final task response records the full commit SHA, the
push result and the freshly verified remote `HEAD`. Commit ancestry is checked
against `origin/main` before and after the push.

---

## 9. Remaining limitations and live behaviour not verified

1. **No live verification.** The panel rendering, the real Telegram entity
   flags and the archive's behaviour on a live account were not observed.
   In particular, the strict scope now means a transient `get_chat()` failure
   **drops** that capture (fail closed) instead of storing an unverified row;
   the resulting log line `[AED] … skipped: peer is not a confirmed human
   (bot=None)` is the expected evidence on the deployment.
2. **Browse requires the self client.** Without a live self client, list/detail
   hide every item and report the hidden count. That is the intended fail-closed
   behaviour, not an empty archive; it was not observed on a running process.
3. **Restart/transition behaviour is proven in-process** with the shipped
   evaluator and the real persistence path (fake table), not by restarting the
   deployed service.
4. **Retention** still deletes internal snapshot-only rows after the configured
   window; that is unchanged and deliberate (bounded storage). A deletion
   observed after the snapshot has expired remains honestly not recoverable.

---

## 10. Stage and next stage

- **Completed stage:** AED archive correctness (event-driven visibility +
  confirmed human private-chat scope) and Private Message schedule policy /
  exact mode restoration, following the Cipher category navigation phase.
- **Next stage:** live verification on the deployment — confirm that an edited
  or deleted human private message appears while an untouched one does not, and
  that a scheduled restricted policy applies and restores the prior mode across
  a real restart. No further code change is identified for these two defects.
