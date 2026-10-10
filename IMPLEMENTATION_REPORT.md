# IMPLEMENTATION_REPORT.md — Premium Custom-Emoji Inline Result: Failure Boundary, the One Fix, and the Next Step

This file describes **only** the current state of the repository after this task.
It **replaces every previous report in full**. Earlier reports — the feature
implementation (`977247e`), the checkpoint-2 field fix (`e13deee`), the
checkpoint-2 wrapper fix (`9633c0b`), the attribution commit (`b463552`), the
via-bot feasibility study (`b04cb41`), the `98c2825` audit, the boundary trace
(`a54877b`), the callback receipt (`1bd843d`) and the render-boundary
investigation (`06eeb8d`) — are preserved in git history and referenced here only
by commit id.

| Item | Value |
|---|---|
| Task | Resolve the remaining Premium custom-emoji inline-delivery failure: find the exact failure boundary, fix only what evidence supports, and state the next concrete step |
| Project | LifeOS Telegram self-bot (`Onlyicing1/Telegram-self-bot`), branch `main` |
| Base revision | `39de9cc` — **the real `origin/main` at task start** (`git fetch` → `git rev-parse origin/main` → `git ls-remote` all agree); the workspace was fast-forwarded from `06eeb8d` with `--ff-only` (no rebase, no force-push, nothing discarded) |
| Deployed implementation | the inline feature chain `977247e … b463552`; `39de9cc` is docs-only on top of it |
| Telethon | **`telethon==1.34.0`**, layer 173 (`backend/requirements.txt:1`) |
| Outcome | **Boundary located and corroborated; one narrow diagnostic defect fixed; no delivery change made and none invented.** The entity leaves the bot in a valid `setInlineBotResults` request (wire-byte proven) and Telegram's *stored* answer comes back without it — the fail-closed gate therefore refuses to send and the flow ends at `INLINE_RESULT_ENTITY_MISSING` exactly as designed. |
| Supabase | **Not touched** — no SQL, no schema, no migration, no request; `DATABASE_ARCHITECTURE.md` unchanged |
| Live Telegram verification | **No** (no live session in this workspace — `freebuff-env list` shows no credentials); see §6 |

---

## 1. Root cause / failure boundary — what the evidence establishes

| # | Statement | Evidence | Status |
|---|---|---|---|
| 1 | The helper builder answers with exactly one real `MessageEntityCustomEmoji`, wrapping exactly the document's `alt` at its real UTF-16 offset | `test_the_registered_router_answers_the_query_with_the_real_entity`; `validate_inline_payload` | **CONFIRMED** |
| 2 | That entity is present in the **serialized** `messages.setInlineBotResults` request | `test_the_answer_carries_the_entity_into_the_set_inline_bot_results_bytes` (`BinaryReader(bytes(request)).tgread_object()`) | **CONFIRMED** |
| 3 | The pinned Telethon's schema carries `entities` on **both** `inputBotInlineMessageText` and the stored `botInlineMessageText` (layer 173) | installed-source inspection this task (§3 of `INVESTIGATION.md`) | **CONFIRMED** |
| 4 | A returned payload that keeps the entity would be **parsed and read correctly** — the empty live result is not a client artifact | **new** `test_a_stored_result_parsed_from_response_bytes_keeps_the_entity` (serialize → parse → inspect) | **CONFIRMED** |
| 5 | `_sanitize_results` never touches `entities` | `test_the_sanitizer_keeps_the_entity_and_only_normalizes_button_rows` | **CONFIRMED** |
| 6 | The inline answer is not server-cached (`cache_time=0`), so the returned result belongs to this attempt | `inline_engine._inline_router` → Telethon `Event.answer` default | **CONFIRMED** |
| 7 | The stored payload Telegram returned had **zero** entities while the text was present | live log: `INLINE_RESULT_INSPECTED entity_present=False … entity_count=0` | **CONFIRMED (live)** |
| 8 | The helper bot's own submission completed and carried one custom-emoji entity | live log: `answer_ok=True answer_custom_emoji_count=1` | **CONFIRMED (live)** |
| 9 | Therefore the entity is lost in **Telegram's processing of the bot's inline answer** | 1–8 | **CONFIRMED** |
| 10 | *Which* Telegram rule drops it (bot entitlement? another server rule?) | no documentation covers an entity a bot supplies inside an inline result | **UNRESOLVED — documented as such** |

Explicitly **not** claimed: that `messages.sendInlineBotResult` or the
exact-message read-back is broken. Neither has ever been reached.

**The defect that was still actionable** was in the *attribution*: the verdict
"dropped by Telegram, not by this application" was derived from a submission
**count**, not from the submitted entity's **identity**, even though the
document ids were already recorded. It is fixed in §2. No delivery defect is
evidenced, so none was invented.

---

## 2. Implementation changes

| File | Change | Rationale |
|---|---|---|
| `backend/services/premium_emoji_inline_service.py` | `_entity_missing_detail(evidence, expected_document_id=None)` now requires the recorded submission to have carried **this emoji's** `document_id` before attributing the loss to Telegram; a submission that carried another entity is reported as uncorrelated with the cause **unproven**. `_inspect_inline_result` adds `answer_document_id_match=True\|False` to the `INLINE_RESULT_INSPECTED` trace. The call site passes `payload["entity"]["document_id"]`. The module docstring now lists the **three** distinguishable routes to a genuine entity instead of asserting there are exactly two | Implements the task's "correlate the exact submitted payload with the actual result returned by Telegram": a verdict must never blame the platform for an entity this attempt did not submit. Bounded, fail-closed, no behaviour change to the send path |
| `tests/test_premium_emoji_inline.py` | 3 new tests (§3) | Pin the correlation, the trace field, and the response-parse round-trip |

**Unchanged on purpose:** the entity/UTF-16 validation, the real source
`document_id` and span, the Saved-Messages-only destination gate, the
fail-closed pre-send check, the `via_bot_id` attribution verification, the
`inline_engine.query_results` / `click_result` architecture, the send
mechanism, and the no-Unicode-fallback rule. No new client, listener, loop,
scheduler, executor or second delivery path. No Supabase/schema/SQL change, no
unrelated refactor, no `ROADMAP.md` / `DATABASE_ARCHITECTURE.md` /
`tests/test_stage13.py` change.

---

## 3. Tests added (all in `tests/test_premium_emoji_inline.py`)

1. `test_the_attribution_requires_the_submitted_entity_to_be_this_emoji` — a
   submission that carried a *different* document id yields
   `INLINE_RESULT_ENTITY_MISSING` with both ids named, "unproven" stated, and
   **no** "dropped by Telegram" claim; nothing is sent (`clicks == 0`).
2. `test_the_inspection_trace_correlates_the_submitted_document_id` — the
   `INLINE_RESULT_INSPECTED` line carries `answer_document_id_match=True` for a
   matching submission and `=False` for a mismatched one.
3. `test_a_stored_result_parsed_from_response_bytes_keeps_the_entity` — a real
   `BotInlineResult` → `BotInlineMessageText` + `MessageEntityCustomEmoji`
   survives serialize → parse and is read back by `_inspect_inline_result` with
   the exact geometry, `document_id_match` and `span_match` true (the response
   side of the boundary).

Task requirements already covered by the **existing, unchanged** tests: the
entity-less returned result is still rejected before sending; a
non-Saved-Messages destination is still refused; no Unicode fallback is ever
built or sent; success still cannot be reported without the real entity **and**
Telegram's `via_bot_id`; the registered router/answer construction and its wire
bytes are pinned.

---

## 4. Verification executed

All from the repository root; pipe status captured (`PIPESTATUS[0]`).

| Check | Command | Result |
|---|---|---|
| Compile | `.venv/bin/python -m compileall -q backend/services/premium_emoji_inline_service.py` | exit 0 (**COMPILE_OK**) |
| Focused file | `.venv/bin/python -m pytest tests/test_premium_emoji_inline.py -q` | **78 passed**, exit 0 (baseline 75; +3 = exactly the new tests) |
| Emoji/inline/bridge/reaction suites (13 files) | `.venv/bin/python -m pytest tests/test_premium_emoji_inline.py tests/test_premium_emoji_probe.py tests/test_emoji_ui.py tests/test_emoji_ui_phase2.py tests/test_emoji_replacement_phase4.py tests/test_emoji_state_phase3.py tests/test_emoji_composition_phase5.py tests/test_emoji_category_service.py tests/test_emoji_library_import.py tests/test_emoji_set_enumeration.py tests/test_reaction_phase6.py tests/test_bridge_delivery.py tests/test_helper_callback_receipt.py -q` | **636 passed**, exit 0 |
| Full suite | `timeout 560 .venv/bin/python -m pytest tests/ -q` | **5797 passed, 26 skipped, 3 warnings in 119.71s**, exit 0 (baseline 5794 + 3 new, **0 regressions**) |
| Whitespace / diff sanity | `git diff --check` | exit 0 |
| Diff review | full `git diff` (1 production file + tests) | scoped; only bounded ids/offsets/counts/booleans; no secrets, tokens or message bodies |

No test was skipped, weakened, deleted or suppressed, and no check was filtered
in a way that hides its exit status.

---

## 5. Repository / remote state at task start

| Check | Command | Result |
|---|---|---|
| Branch / status | `git status --short` | clean; on `main`, tracking `origin/main` |
| Local HEAD | `git rev-parse HEAD` | `06eeb8d…` (behind the remote) |
| Fetch | `git fetch origin main` | `06eeb8d..39de9cc` — the remote had **14 newer commits** (the inline feature chain; `git rev-list --count 06eeb8d..39de9cc`) |
| Ancestry / fast-forward | `git merge-base --is-ancestor 06eeb8d origin/main` | exit 0 → the workspace was fast-forwarded with `git merge --ff-only origin/main`; no rebase, no force-push, nothing discarded |
| Remote tip | `git rev-parse origin/main` / `git ls-remote origin refs/heads/main` | `39de9cca6d45e3e7f499d28666a968899ef1824e` (both agree) |

The previously recorded delivery table in `AGENTS.md` (§15, snapshot
2026-10-01, `6bec694`) is **stale relative to this fetch** — the fresh remote
state above is authoritative.

---

## 6. Not verified live / limitations

1. **No live Telegram execution.** This workspace has no `SESSION_STRING`/`BOT_TOKEN`
   (`freebuff-env list`), so no message was sent and no emoji was ever rendered.
   The boundary conclusion is verified against the pinned Telethon (including
   real request bytes and a response parse round-trip) and pinned by tests — not
   against a live `getInlineBotResults` response.
2. **The entity still does not reach the self account today.** The fail-closed
   gate keeps refusing, so `sendInlineBotResult` and the exact-message read-back
   remain unproven.
3. **Why Telegram drops the entity remains undocumented.** No entity-type or
   custom-emoji error is documented for `setInlineBotResults`, and the documented
   bot-side custom-emoji entitlement (Fragment username; Bot API 9.4 owner-Premium
   allowance) is written for messages a bot sends directly — its applicability to
   an inline-supplied entity is **not documented**. No entitlement was probed and
   no workaround was added.
4. **The alternative route is documented but unexercised** (see §7): the owner's
   own account authoring the entity to Saved Messages needs no bot entitlement,
   but it has never been implemented or live-tested here, so it is a hypothesis
   with a documentation basis — not a confirmed fix.

---

## 7. Next concrete step

1. **Owner-run, live (one run):** `Menu → Emoji → ✨ Send Premium Emoji → reply
   with a genuine Premium emoji`, then read the panel/trace. The decisive fields
   are now `answer_recorded`, `answer_ok`, `answer_custom_emoji_count`,
   **`answer_document_id_match`** and the panel's `Owner Premium: … · emoji
   free: …` line. `INVESTIGATION.md` §7 gives the full interpretation table; the
   key case is `answer_document_id_match=True` + `entity_present=False`, which
   confirms on the live path that Telegram dropped *this* emoji's submitted
   entity.
2. **If the drop is confirmed live and a working path is still required**, the
   only remaining options are entitlement/ownership decisions, none of which is a
   code fix:
   * the helper bot's Fragment-purchased username (§17/§28 — Premium/Fragment
     purchases are explicitly **not** a goal of this project), or
   * a Premium owner for a direct bot-sent message (Bot API 9.4 — explicitly
     scoped to messages **directly sent by the bot**, not to inline results), or
   * **route 3**: the owner's own account sending the entity itself to Saved
     Messages, which Telegram documents as free for everyone in the self chat.
     This is untested here and must be proven live (exact-message read-back)
     before it is offered as a delivery path; it is **not** implemented in this
     task.
3. Only after such a live result should a delivery change be designed — and never
   as a Unicode fallback, which remains excluded by the approved behaviour.

---

## 8. Delivery

| Step | Value |
|---|---|
| Commit | one commit for this task: `backend/services/premium_emoji_inline_service.py`, `tests/test_premium_emoji_inline.py`, `INVESTIGATION.md`, `IMPLEMENTATION_REPORT.md` |
| Push | `git push origin main`, fast-forward only; no rebase, no force-push, no history rewrite |
| Remote verification | after the push: `git fetch origin main` → `git rev-parse HEAD` = `git rev-parse origin/main` = `git ls-remote origin refs/heads/main`; `git merge-base --is-ancestor <commit> origin/main` exit 0; `git status --short` clean |
| Supabase | untouched — no SQL, no schema, no migration, no request; `DATABASE_ARCHITECTURE.md` unchanged |
| Protected files | `tests/test_stage13.py`, `DATABASE_ARCHITECTURE.md`, `ROADMAP.md` not modified |

A commit cannot contain its own hash: the commit SHA and the verified
`origin/main` SHA are reported in this task's delivery response, and the remote
state was re-checked with the commands above after the push.
