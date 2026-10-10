# IMPLEMENTATION_REPORT.md — Premium Custom-Emoji Inline Result: Where the Entity Is Lost

This file describes ONLY the current state of the repository after this task. It
**replaces every previous report in full**. Earlier reports — the emoji feature
implementation (`977247e`), the checkpoint-2 "field" fix (`e13deee`), the
checkpoint-2 wrapper fix (`9633c0b`), the via-bot feasibility study (`b04cb41`),
the `98c2825` audit, the boundary-tracing task (`a54877b`) and the
callback-receipt task (`1bd843d`) — are preserved in git history and referenced
here only by commit id.

| Item | Value |
|---|---|
| Task | Targeted fix: find and fix the earliest remaining defect in the path that **builds, submits and returns** the helper bot's Premium custom-emoji inline result |
| Project | LifeOS Telegram self-bot (`Onlyicing1/Telegram-self-bot`), branch `main` |
| Base revision | `9633c0b` (the deployed commit; verified `= origin/main` at task start) |
| Telethon | **`telethon==1.34.0`** (`backend/requirements.txt:1`) |
| Outcome | **Determination + the one defect that was still actionable.** The entity is **not** lost in application code and **not** in TL serialization: the registered builder answers with one real `MessageEntityCustomEmoji`, and that entity is provably inside the bytes of the `messages.setInlineBotResults` request the helper bot sends (re-read from the real wire bytes with the pinned Telethon). Telegram's **stored** answer — the payload `getInlineBotResults` returns — comes back without it, which is the live `INLINE_RESULT_ENTITY_MISSING`. The defect this task fixed is the **attribution gap**: nothing recorded what the helper bot itself submitted, so a stored result without an entity could not be attributed to Telegram or to this application. It now can, at runtime. |
| Supabase | **Not touched** (no SQL, no schema, no migration, no request; `DATABASE_ARCHITECTURE.md` untouched) |
| Live Telegram verification | **No** (no live session in this workspace); see §7 |

---

## 1. Root cause — what the evidence establishes

| # | Statement | Evidence | Status |
|---|---|---|---|
| 1 | The helper builder answers with exactly one real `MessageEntityCustomEmoji` (real document id, UTF-16 offset 15, length 2, span = the document `alt`) | `test_the_registered_router_answers_the_query_with_the_real_entity` (real `types.InputBotInlineResult` / `InputBotInlineMessageText`) | **CONFIRMED** |
| 2 | That entity is present in the **serialized** `messages.setInlineBotResults` request | `test_the_answer_carries_the_entity_into_the_set_inline_bot_results_bytes` — `BinaryReader(bytes(request)).tgread_object()` re-reads `MessageEntityCustomEmoji(5361626279781934801, 15, 2)` | **CONFIRMED** |
| 3 | `inline_engine._sanitize_results` never touches `entities` (it only normalizes `reply_markup.rows`) | `test_the_sanitizer_keeps_the_entity_and_only_normalizes_button_rows` | **CONFIRMED** |
| 4 | The stored payload Telegram returned had **zero** entities while the text was present | live log (deployed `9633c0b`): `INLINE_RESULT_INSPECTED entity_present=False … entity_count=0` | **CONFIRMED (live)** |
| 5 | The pre-send gate then refused to send, exactly as designed | `DIAGNOSIS diagnosis=INLINE_RESULT_ENTITY_MISSING`, no `INLINE_SEND_STARTED` | **CONFIRMED (live)** |
| 6 | Therefore the entity is lost in **Telegram's processing of the bot's inline answer** (`setInlineBotResults` ⇒ `getInlineBotResults`) | 1–2 rule out application code and serialization; 4 is the server's own reply | **CONFIRMED** |
| 7 | Whether the bot's entitlement (Fragment-purchased username / Premium bot owner) is the server-side rule that drops it | not documented for an inline-supplied entity | **UNRESOLVED — documented as such** |

Explicitly **not** claimed: that `messages.sendInlineBotResult` or the
exact-message read-back is broken. Neither has ever been reached, and nothing
here changes either.

## 2. Files changed (and why)

| File | Change | Rationale |
|---|---|---|
| `backend/helper/inline_engine.py` | `record_inline_answer(query_key, results, *, ok, error)` + `last_inline_answer(query_key, *, since=None)` + `_count_custom_emoji()`; the inline router records, for **registered** query keys only, whether the answer completed without raising and how many custom-emoji entities the built answer carried (with their document ids, bounded to 32) | `event.answer` returns a boolean, so the bot's own `setInlineBotResults` submission was observable nowhere; a stored result without an entity could not be attributed. Bounded by construction (registered keys only, in-memory, scalars) — **no** new client, listener, loop, scheduler or executor |
| `backend/services/premium_emoji_inline_service.py` | The attempt's monotonic start mark (`query_started`) is compared against that record so only an answer submitted **after** the query counts as evidence; the record is stored in the evidence block as `inline_result["answer"]` and in the `INLINE_RESULT_INSPECTED` trace; new `_entity_missing_detail()` turns an entity-less stored payload into an attributed verdict (dropped by Telegram / answer did not complete / answer carried none / unproven); `_empty_answer_evidence()` added to the "every key present" evidence shape | The failure must name **which side** lost the entity instead of leaving it to guesswork — the previous investigation had to list this as unknown |
| `backend/bot/handlers/emoji.py` | `_premium_inline_report` renders one new line: `Helper answer: submitted N custom-emoji entity/entities · accepted by Telegram` / `· the submission did not complete` / `no submission of this bot was recorded` | The owner-facing panel must show the bot's own submission, not only Telegram's stored copy |
| `tests/test_premium_emoji_inline.py` | 13 new tests (§3) + 2 imports | Pin the submission path, the wire bytes, the sanitizer, the fail-closed paths and the new attribution |
| `INVESTIGATION.md`, `IMPLEMENTATION_REPORT.md` | Replaced in full | Latest-only canonical records (task requirement) |

**Unchanged on purpose:** the entity/UTF-16 validation, the real source
`document_id` and span, the Saved-Messages-only destination gate, the
`via_bot_id` attribution verification, the `inline_engine.query_results` /
`click_result` architecture, the send mechanism, the fail-closed pre-send gate
and the no-Unicode-fallback rule. No unrelated refactor, no Supabase change, no
`tests/test_stage13.py` / `DATABASE_ARCHITECTURE.md` / `ROADMAP.md` change.

## 3. Tests added (all in `tests/test_premium_emoji_inline.py`)

1. `test_the_registered_router_answers_the_query_with_the_real_entity` — the
   **registered** router (through `register_inline_handler` + the production
   `emoji.register`) answers `premium_emoji_send:<doc>:<glyph>` with exactly one
   result carrying exactly the expected `MessageEntityCustomEmoji`; document id,
   UTF-16 offset/length and the covered glyph are validated.
2. `test_the_answer_carries_the_entity_into_the_set_inline_bot_results_bytes` —
   the real `SetInlineBotResultsRequest` is serialized and **re-parsed**; the
   entity is on the wire with the exact geometry.
3. `test_the_sanitizer_keeps_the_entity_and_only_normalizes_button_rows` — the
   sanitizer removes/replaces nothing; Glass UI results stay entity-free.
4. `test_a_malformed_query_key_never_reaches_the_answer` — unusable document id,
   unusable id, and missing glyph each answer `[]` (fail closed, nothing built).
5. `test_the_helper_bot_records_what_its_own_answer_submitted` — the submission
   record (result count, custom-emoji count, document ids) and the `since` filter
   that prevents an older answer being read as this attempt's evidence.
6. `test_the_answer_record_counts_only_real_custom_emoji_entities` — a bold
   entity is not counted as a custom emoji.
7. `test_a_failing_answer_is_recorded_as_not_completed` — a raising builder is
   recorded honestly and still answers `[]`.
8. `test_a_stored_result_without_the_entity_is_attributed_to_telegram` — the
   live shape: payload inspected, entity absent, submission recorded ⇒ the
   detail says the entity was **dropped by Telegram, not by this application**,
   nothing is sent, and the panel renders the helper's submission.
9. `test_a_recorded_submission_without_the_entity_blames_this_application` —
   a recorded answer with zero entities is attributed to this application.
10. `test_a_failed_submission_is_reported_as_such` — `ok=False` is reported.
11. `test_an_unrecorded_submission_leaves_the_cause_unproven` — no record ⇒
    "unproven", never a Telegram claim.
12. `test_entity_missing_detail_tolerates_a_missing_evidence_block` — the
    verdict helper is total.
13. `test_the_inspection_trace_carries_the_submission_evidence` — the
    `INLINE_RESULT_INSPECTED` line carries `answer_recorded` / `answer_ok` /
    `answer_custom_emoji_count`.

Requirements 4.5–4.8 of the task are covered by the **existing, unchanged**
tests: the pre-send inspection still refuses a genuinely entity-less stored
result; the verified-path tests still require the real entity **and** Telegram's
`via_bot_id`; no Unicode fallback is ever sent; a non-Saved-Messages destination
is refused.

## 4. Verification executed

All from the repository root; the pipe status is captured (`PIPESTATUS[0]`).

| Check | Command | Result |
|---|---|---|
| Compile | `.venv/bin/python -m compileall -q backend/helper/inline_engine.py backend/services/premium_emoji_inline_service.py backend/bot/handlers/emoji.py` | exit 0 (**COMPILE_OK**) |
| Focused file | `.venv/bin/python -m pytest tests/test_premium_emoji_inline.py -q` | **75 passed**, exit 0 (baseline 62; +13 = exactly the new tests) |
| Emoji-related suites | `.venv/bin/python -m pytest tests/test_premium_emoji_inline.py tests/test_premium_emoji_probe.py tests/test_emoji_ui.py tests/test_emoji_ui_phase2.py tests/test_emoji_replacement_phase4.py tests/test_emoji_state_phase3.py tests/test_emoji_composition_phase5.py tests/test_emoji_category_service.py tests/test_emoji_library_import.py tests/test_emoji_set_enumeration.py -q` | **528 passed**, exit 0 |
| Full suite | `timeout 500 .venv/bin/python -m pytest tests/ -q` | **5794 passed, 26 skipped, 3 warnings in 128.13s**, exit 0 (baseline 5781; +13, **0 regressions**) |
| Whitespace / diff sanity | `git diff --check` | exit 0 |
| Diff review | full `git diff` (3 production files + tests + 2 docs) | scoped; only bounded ids/offsets/counts/class names traced; no secrets, tokens or message bodies |

No test was skipped, weakened, deleted or suppressed, and no check was filtered
in a way that hides its exit status.

## 5. Delivery

| Step | Value |
|---|---|
| Commit | one commit for this task's implementation — `backend/helper/inline_engine.py`, `backend/services/premium_emoji_inline_service.py`, `backend/bot/handlers/emoji.py`, `tests/test_premium_emoji_inline.py`, `INVESTIGATION.md`, `IMPLEMENTATION_REPORT.md` (the docs-only addendum that carries this very sentence is a second, report-only commit; no production file is touched by it) |
| Push | pushed to `origin/main` as a fast-forward (`9633c0b..<commit>`); no rebase, no force-push, no history rewrite |
| Remote verification | `git fetch origin main` → no new commits; `git rev-parse HEAD` = `git rev-parse origin/main` = `git ls-remote origin refs/heads/main`; `git merge-base --is-ancestor <commit> origin/main` exit 0; the remote `INVESTIGATION.md`/`IMPLEMENTATION_REPORT.md` were re-read from `origin/main` |
| Final working tree | clean apart from the pre-existing untracked `.m14/` scratch directory (never touched); the checkout sits on the task branch `fix/premium-emoji-inline-entity` whose tip is `origin/main`; the pre-existing, unrelated unpushed local `main` (`db02e13`, bounded PDF/DOCX media extraction) is preserved untouched and also pointed at by `backup/db02e13-doc-extraction` |
| Supabase | untouched — no SQL, no schema, no migration, no request; `DATABASE_ARCHITECTURE.md` unchanged |
| Protected files | `tests/test_stage13.py`, `DATABASE_ARCHITECTURE.md` and `ROADMAP.md` were not modified by any commit of this task |

A commit cannot contain its own hash: the commit SHA and the verified
`origin/main` SHA are reported in this task's delivery response, and the remote
state was re-checked with the commands above after the push.

## 6. Repository / remote state at task start

| Check | Command | Result |
|---|---|---|
| Branch / status | `git status --short --branch` | on `main`; only pre-existing untracked scratch directories (`.m14/`) — never touched |
| HEAD | `git rev-parse HEAD` | `db02e13…` locally, so the working tree was moved onto a task branch created from `origin/main` (`9633c0b`) to work on the real deployed code; local `main` and the pre-existing local commits were left intact (see §7.5) |
| Fetch | `git fetch origin main` | fast-forwarded `b04cb41..9633c0b` |
| Remote tip | `git rev-parse origin/main` | `9633c0b073984fa379236e7240a6661c01cbfeb5` = the deployed commit |

No rebase, no force-push, no history rewrite, nothing discarded.

## 7. Not verified live / limitations

1. **No live Telegram or Render execution.** The determination is verified
   against the installed pinned Telethon (including the real request bytes) and
   pinned by tests — not against a live `getInlineBotResults` response.
2. **The entity still does not reach the self account today.** §1.6 is
   unchanged by this task: the stored inline result carries no entity, so the
   pre-send gate keeps refusing and no message is sent.
3. **Why Telegram drops it remains undocumented** (no entity-type restriction is
   documented for `inputBotInlineMessageText`, and the Bot API's bot-side
   custom-emoji entitlement is written for messages a bot sends directly). No
   workaround was invented and no entitlement was probed.
4. **`sendInlineBotResult` and the exact-message read-back are still unproven
   live** — they have never been reached.
5. **Unresolved pre-existing workspace divergence (not caused by this task):**
   local `main` sits on the unpushed `db02e13` (bounded PDF/DOCX media
   extraction) which diverges from `origin/main`; it was neither published nor
   discarded. This task's changes are committed on the remote `main`. As a
   safety net the divergent commit is also pointed at by the local branch
   `backup/db02e13-doc-extraction`.
6. **The first live run after this commit is the deciding one** — §7 of
   `INVESTIGATION.md` lists exactly what each possible trace proves.

## 8. Next stage

Deploy this commit and re-run `Menu → Emoji → ✨ Send Premium Emoji → reply with
a genuine Premium emoji`. The window must now contain
`INLINE_RESULT_INSPECTED … answer_recorded=True answer_ok=True
answer_custom_emoji_count=1`, which **confirms on the live path** that the
helper bot submitted the entity and Telegram dropped it from the stored answer.
From there the only remaining documented options are the bot-side entitlements
(Fragment-purchased username for the helper bot, or a Premium bot owner) — an
owner cost/ownership decision, not a code change — or a different, documented
delivery mechanism. If instead the trace shows `answer_ok=False`,
`answer_custom_emoji_count=0` or `answer_recorded=False`, the recorded reason
now names an application-side defect that the previous evidence could not see.

## 9. Supabase / database

**Untouched.** No SQL executed, no schema or migration changed, no Supabase
request made, `DATABASE_ARCHITECTURE.md` unchanged. The new state is in-memory
only (one bounded dict in the process).
