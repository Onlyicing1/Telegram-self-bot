# IMPLEMENTATION_REPORT.md — Premium Custom-Emoji Inline Flow: Entitlement Diagnosis Fix

This file describes **only** the current state of the repository after this task
and **replaces every previous report in full**. The direct-send POC report
(`34d7dc3`) and the inline route audits remain readable in git history.

| Item | Value |
|---|---|
| Task | **Diagnose and fix the Premium Custom-Emoji Inline-Result failure** — establish the earliest confirmed failure and apply the smallest evidence-based fix |
| Project / branch | LifeOS Telegram self-bot (`Onlyicing1/Telegram-self-bot`), `main` (tracks `origin/main`) |
| Base revision | `34d7dc3b24d39beccc91368c66cabf2db34f5b6f` — freshly fetched and verified `= origin/main` at task start |
| Telethon | **`telethon==1.34.0`** (`backend/requirements.txt:1`) |
| **Stage completed** | Root cause confirmed as the **documented bot custom-emoji entitlement restriction**; the inline flow now reports the honest, machine-readable `INLINE_BOT_NOT_ENTITLED` verdict for a proven correlated drop, plus regression tests and these canonical reports |
| **Root cause (confirmed)** | The helper bot authors the inline result; it has no Fragment-purchased additional username and does not satisfy the bot-owner-Premium allowance, so Telegram stores the bot's inline result with the custom-emoji entity **removed** (`entity_count=0`, text intact). This application's own payload, entity, UTF-16 geometry and request bytes are correct; the send never runs because the gate refuses first |
| Real Telegram send/read-back in this workspace | **NO — not performed.** No credentials and no `.env`; `backend/config.load()` hard-fails without them. **No Telegram request of any kind was made here** |
| Does this prove the artifact renders? | **NO.** The fix is a **diagnosis** refinement. End-to-end acceptance (`via_bot_id` + genuine entity on a real stored message) is **not** claimed; the inline route cannot satisfy it while the helper bot lacks entitlement |
| Production send path | **Unchanged behaviour** — no gate, send, read-back or validation change; only the emitted diagnosis is made precise |

---

## 1. What was confirmed (root cause)

See `INVESTIGATION.md` §1–§3 for the full evidence chain. In brief:

* The `reason=no_send_message` label was a **pre-fix wrapper bug** (reading
  `send_message` off the `custom.InlineResult` wrapper instead of
  `.result.send_message`); it is fixed and is **not** the current failure.
* With the wrapper fix, the stored inline result is inspected **successfully**
  (`ok=True`) but carries `entity_present=False entity_count=0` with the text
  intact — Telegram stored the bot's result **without** the entity.
* Telegram documents that custom-emoji entities "can only be used by bots that
  purchased additional usernames on Fragment or in the messages directly sent by
  the bot … if the owner of the bot has a Telegram Premium subscription"
  (<https://core.telegram.org/bots/api>). The helper bot is not entitled, so the
  entity is dropped **server-side**.
* This is a **Telegram-side restriction**, not an application defect; the fix is
  to report it precisely rather than to bypass the fail-closed gate.

## 2. Files changed

| File | Change |
|---|---|
| `backend/services/premium_emoji_inline_service.py` | Added the `INLINE_BOT_NOT_ENTITLED` verdict constant; added `_answer_proves_the_bot_submitted_this_emoji`; `classify_diagnosis` returns the new verdict **only** for a proven, correlated drop (recorded, error-free, matching `document_id`); recorded `expected_document_id` in the inline-result evidence; `outcome_summary` and `_entity_missing_detail` name the documented entitlement rule. **No send/gate/read-back behaviour changed.** |
| `tests/test_premium_emoji_inline.py` | Updated `test_a_stored_result_without_the_entity_is_attributed_to_telegram` to expect `INLINE_BOT_NOT_ENTITLED`; added `test_the_entitlement_verdict_needs_the_bot_own_correlated_submission`; added the new label to `test_the_panel_notice_carries_no_diagnosis_and_no_success_claim`; refreshed the header contract |
| `INVESTIGATION.md`, `IMPLEMENTATION_REPORT.md` | **Replaced in full** — canonical latest-only records |

**Deliberately unchanged:** the send path (`_send_result`, `messages.sendInlineBotResult`),
the pre-send gates, the read-back and `via_bot_id` verification, the helper
bridge, `inline_engine`, the direct/probe/self-send services, the Glass UI
panel behaviour, the database, the AI runtime, and unrelated working-tree state
(the pre-existing untracked `telegram-self-bot/` directory is untouched and is
**not** part of this commit).

### Requirements, item by item

1. **Owner's existing self-client + existing click/send mechanism** — unchanged;
   no client/listener/loop/executor added.
2. **Saved Messages-only restriction** — unchanged.
3. **Genuine `MessageEntityCustomEmoji` only; no fabricated entity, no Unicode
   fallback** — unchanged; the fix touches only diagnosis text/constants.
4. **Correct `document_id` validation** — unchanged.
5. **UTF-16 offset/length validation** — unchanged.
6. **`via_bot_id` attribution verification** — unchanged.
7. **Fail closed** — unchanged; the new verdict is another fail-closed outcome.
8. **Distinguish the outcomes** — improved: a **proven correlated** drop now
   reports `INLINE_BOT_NOT_ENTITLED` (documented entitlement restriction) rather
   than the neutral `INLINE_RESULT_ENTITY_MISSING`; unproven cases keep the
   neutral label, and empty/unsupported/no-payload/send-failed/readback-failed
   distinctions are untouched.
9. **No bypass to look successful** — nothing is sent; no validation weakened.
10. **No second client/listener/scheduler/update loop/executor/sender** — none.

## 3. Exact behaviour introduced

```
inline_result.ok == True
  and inline_result.entity_present == False
  and _answer_proves_the_bot_submitted_this_emoji(answer, expected_document_id)
      => INLINE_BOT_NOT_ENTITLED        # documented entitlement restriction
  otherwise
      => INLINE_RESULT_ENTITY_MISSING   # neutral; cause unproven
```

`_answer_proves_the_bot_submitted_this_emoji` is true **only** when the bot's own
answer was recorded for this attempt, its submission raised nothing (`ok=True`),
and it carried this exact `document_id`. Any other shape (unrecorded, failed
submission, other emoji, missing id) keeps the neutral verdict — so Telegram is
never blamed without proof.

## 4. Tests and validation

| Check | Command | Result |
|---|---|---|
| Compile | `python -m compileall -q backend/services/premium_emoji_inline_service.py tests/test_premium_emoji_inline.py` | **COMPILE OK**, exit 0 |
| Focused | `python -m pytest tests/test_premium_emoji_inline.py -q` | **85 passed**, exit 0 |
| Emoji suites | `python -m pytest tests/test_premium_emoji_inline.py tests/test_premium_emoji_direct.py tests/test_premium_emoji_probe.py tests/test_premium_emoji_self_send_route.py tests/test_emoji_ui.py -q` | **256 passed, 1 skipped**, exit 0 (the skip is the pre-existing opt-in `live_telegram` test) |
| Full suite | `python -m pytest tests -q` | **5895 passed, 27 skipped**, exit 0 (baseline `34d7dc3`: 5894 passed / 27 skipped → exactly **+1** net new test, **0 regressions**) |
| Whitespace / diff sanity | `git diff --check` | exit 0 |
| Full diff review | `git diff` read end-to-end | done before commit |

### Offline (green) vs live (NOT executed) — kept strictly separate

**Offline:** the classification boundary (proven vs unproven drop), the new
verdict's emission and summary text, the unchanged real-wrapper/real-TL-object
inspection, wire-byte round-trip, no-Unicode-fallback, and the panel-leak guard —
all against fakes at the Telegram boundary and the real Telethon classes.

**Live:** the actual Telegram send and read-back. **Not executed here** (no
credentials). No statement claims end-to-end acceptance.

## 5. Acceptance criteria vs. what is proven where

| Criterion (task) | Offline | Live |
|---|---|---|
| Earliest confirmed failure identified | ✅ (documented entitlement restriction) | n/a |
| Entity/id/UTF-16 validation, Saved-Messages gate, `via_bot_id` verification preserved | ✅ | unchanged |
| Exact inspected result is the clicked result | ✅ (pinned against the real `InlineResults`) | pending |
| Fail-closed distinctions (empty/unsupported/no-payload/entity-missing/proven-entitlement) | ✅ | pending |
| No Unicode fallback, no fabricated entity, no bypass | ✅ | pending |
| **The inline route produces a genuine entity + `via_bot_id` on real Telegram** | **cannot be decided offline; blocked by the bot's entitlement** | **NOT SATISFIED — requires a Fragment-purchased helper-bot username (out of scope) or the separate direct route** |

## 6. Remaining limitations and the precise next step

* **Inherent limitation:** the inline route's acceptance criterion (a genuine
  entity with the helper bot's `via_bot_id`) cannot be met while the helper bot
  lacks a Fragment-purchased additional username. This is the owner's
  cost/ownership decision (`ROADMAP.md` §17, §28) and is **not** part of this
  task.
* **Not verified:** any live Telegram send/read-back; the fix is a diagnosis
  refinement only.
* **Precise next step:** to obtain a genuine custom emoji in a real chat, run the
  **direct owner-authored route** live (`Menu` → Emoji → "🧪 Live test · direct
  self-chat", `premium_emoji_direct_service`) — it uses the owner's own account
  and is not subject to the helper bot's entitlement restriction (it carries no
  `via_bot_id` by construction). That is the route that can satisfy the artifact
  requirement; the inline route cannot until the helper bot is entitled.

## 7. Git status

| Step | Value |
|---|---|
| Files in this task's commit | `backend/services/premium_emoji_inline_service.py`, `tests/test_premium_emoji_inline.py`, `INVESTIGATION.md`, `IMPLEMENTATION_REPORT.md` |
| Commit | one commit for the task: `78d8744538da934541255ff86a10d2da531b0883` (`fix(emoji): report the inline entity drop as the documented bot entitlement restriction`) |
| Direct push to `main` | **Not used.** An earlier direct `git push origin HEAD:main` was rejected (HTTP 403, read-only token at that time). Direct-to-`main` was not retried after write access was restored; the least-invasive PR route was used instead. |
| Branch publication | `git push origin HEAD:refs/heads/fix/premium-emoji-inline-entitlement-diagnostic` → `[new branch]`; remote branch verified `= 78d8744538da934541255ff86a10d2da531b0883` (`git ls-remote` + API `git/ref`) |
| Pull request | **Created and verified**: <https://github.com/Onlyicing1/Telegram-self-bot/pull/183> — state `open`, head `fix/premium-emoji-inline-entitlement-diagnostic` (`78d8744…`), base `main`, 1 commit, 4 changed files, mergeable |
| `origin/main` | `34d7dc3b24d39beccc91368c66cabf2db34f5b6f` — **unchanged** (the PR is open, **not merged**) |
| Working tree | **clean** (`git status --porcelain` empty); remote URL left token-free |

A commit cannot contain its own hash: the commit SHA and the verified remote
state are reported here and in this task's delivery response, re-checked with
`git fetch` + `git ls-remote` + the GitHub API.

### Verified delivery state (this task)

| Item | Value |
|---|---|
| Local commit / HEAD | `78d8744538da934541255ff86a10d2da531b0883` (`fix(emoji): report the inline entity drop as the documented bot entitlement restriction`) |
| Commit already available remotely before this task? | **NO** — no remote branch contained it |
| Remote branch created | **YES** — `fix/premium-emoji-inline-entitlement-diagnostic` = `78d8744538da934541255ff86a10d2da531b0883` (verified via `git ls-remote` and `GET /repos/…/git/ref/heads/…`) |
| Pull request | **YES** — [#183](https://github.com/Onlyicing1/Telegram-self-bot/pull/183), `open`, head `fix/premium-emoji-inline-entitlement-diagnostic` → base `main`, 1 commit, 4 files, `mergeable=True` (verified via `GET /repos/…/pulls/183`) |
| Permission errors | An **earlier** session hit HTTP 403 (`Resource not accessible by integration`) on `POST /repos/…/git/refs` with the then-available token. In this session the token was write-capable: `POST /repos/…/git/refs` returned **201** and the branch push succeeded. No authorization was bypassed. |
| `main` changed? | **NO** — `origin/main` remains `34d7dc3b24d39beccc91368c66cabf2db34f5b6f`; the commit is **not** an ancestor of `origin/main` (it is only on the PR head branch) |
| Deployment | **NOT deployed** — the change is not on `main` |
| Live Telegram verification | **NOT performed** (no credentials in the workspace; no Telegram request made) |
| Merge | **NOT performed** — the PR is intentionally left open; merging is not authorized here and repository policy requires the PR route |
| Exact next step | Review and merge PR #183 into `main` (or `git push origin HEAD:main` from a write-capable environment), then re-verify: `git fetch origin` → `git rev-parse origin/main` → `git merge-base --is-ancestor 78d8744538da934541255ff86a10d2da531b0883 origin/main` (exit 0) |



