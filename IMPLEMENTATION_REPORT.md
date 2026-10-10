# IMPLEMENTATION_REPORT.md — Premium Custom Emoji: Reading the Real Inline-Result Payload

This file describes ONLY the current state of the repository after this task.
It replaces every previous report in full. Earlier reports — the emoji feature
implementation (`977247e`), the checkpoint-2 "field" fix (`e13deee`), the
feasibility study (`b04cb41`), the `98c2825` audit, the boundary-tracing task
(`a54877b`), and the callback-receipt task (`1bd843d`) — are preserved in git
history and referenced here only by commit id.

| Item | Value |
|---|---|
| Task | Diagnose and fix the Premium custom-emoji flow at the earliest confirmed failing stage: inspection of the object returned by Telethon's `inline_query` |
| Project | LifeOS Telegram self-bot (`Onlyicing1/Telegram-self-bot`), branch `main` |
| Base revision | `1bd843d` (verified equal to `origin/main` at task start) |
| Telethon | **`telethon==1.34.0`** (`backend/requirements.txt:1`); all shapes verified against that installed source |
| Status | **Root cause found and fixed.** `inline_query` returns `custom.InlineResults` — a list of `custom.InlineResult` **wrappers**, whose raw TL `BotInlineResult` lives in `.result`; the wrapper has **no `send_message`**. The inspection read `getattr(results[0], "send_message", None)` (always `None`) and reported `reason=no_send_message` → `INLINE_RESULT_REJECTED` without ever looking at the payload Telegram returned. The read now goes through the real shapes, with distinct honest diagnoses for an empty answer, an unsupported shape and a real result without a payload. |
| Production code changed | **Yes** — `backend/services/premium_emoji_inline_service.py`, `backend/helper/inline_engine.py`, `backend/bot/handlers/emoji.py`. No new client, listener, scheduler, update loop or send mechanism. |
| Supabase | **Not touched** (no SQL, no schema, no migration, no request) |
| Live Telegram verification | **No** (no live session in this workspace); see §7 |

---

## 1. Root cause (verified from the installed source)

`telethon/client/bots.py` returns `custom.InlineResults(self, result, entity=…)`.
`telethon/tl/custom/inlineresults.py` builds that list as
`InlineResult(client, x, original.query_id, entity=entity)` for every
`original.results` entry — i.e. **the elements are wrappers, not raw TL objects**.
`telethon/tl/custom/inlineresult.py` stores the raw object as `self.result`
(`self.result = original`), exposes it as `.message`
(`return self.result.send_message`) and clicks via `self.result.id`.

The payload therefore lives at **`results[0].result.send_message`**; the wrapper
itself has **no `send_message` attribute** — confirmed at runtime against the
installed 1.34.0 (`hasattr(custom.InlineResult, "send_message") is False`).
`_inspect_inline_result` read `getattr(first, "send_message", None)`, which is
`None` for every real response, so it produced the live
`INLINE_RESULT_INSPECTED entity_present=False reason=no_send_message` and
`classify_diagnosis` mapped that to `INLINE_RESULT_REJECTED`.

The earlier `e13deee` commit changed `.message` → `.send_message`, but **both are
wrapper-level reads**; the wrong part was the *object*, not the field. Telegram
keeping or stripping the entity was never established — nothing was ever sent.

## 2. Files changed

| File | Change | Rationale |
|---|---|---|
| `backend/services/premium_emoji_inline_service.py` | New `_resolve_inline_result()` (both real shapes → stored payload, with `unsupported` / `no_send_message` kinds) and `_class_path()`; `_inspect_inline_result()` resolves `results[0]` through it, handles an empty list, and records `reason`/`wrapper_class`/`tl_class`; `_empty_inline_evidence()` gained those keys; three new diagnoses (`INLINE_RESULT_EMPTY`, `INLINE_RESULT_UNSUPPORTED`, `INLINE_RESULT_NO_SEND_MESSAGE`) wired into `classify_diagnosis`, the post-inspection failure branches (honest per-reason details) and `outcome_summary` | Read the actual stored payload; never label an unknown shape as Telegram-side stripping |
| `backend/helper/inline_engine.py` | New `INLINE_ZERO_RESULTS_REASON` constant, used by `query_results` | Let the service tell an empty answer apart from a failed query without matching prose; `trigger`'s contract unchanged |
| `backend/bot/handlers/emoji.py` | `_premium_inline_report` renders the new reasons ("the helper bot returned none" / "not inspectable (…)") instead of "entity missing" for every non-entity case | The user-facing report must not claim stripping for a shape that was never inspected |
| `tests/test_premium_emoji_inline.py` | Fakes rewritten to the **real** Telethon shapes (`_FakeInlineResult` now subclasses `telethon.tl.custom.InlineResult`; `_bot_inline_result()` builds a real `types.BotInlineResult`; the old `send_message`-on-the-wrapper fake is gone; unused `result_for_query` removed) + 10 new tests | Regression-proof the exact live failure |
| `INVESTIGATION.md`, `IMPLEMENTATION_REPORT.md` | Replaced in full | Latest-only canonical records (task requirement) |

**No other production code changed. No unrelated refactors. No Supabase schema,
SQL, migration or request. `DATABASE_ARCHITECTURE.md` untouched.**

## 3. Tests added (all in `tests/test_premium_emoji_inline.py`)

* `test_the_telethon_wrapper_keeps_the_payload_on_result_only` — the real
  wrapper has **no `send_message`**; `wrapper.message is
  wrapper.result.send_message`.
* `test_inspection_reads_the_real_wrapper_payload` — a real
  `custom.InlineResult` around a real `BotInlineResult`: entity found,
  `document_id`/UTF-16 offset/length/span validated, and `wrapper_class` /
  `tl_class` recorded as the real classes.
* `test_inspection_accepts_a_raw_bot_inline_result_too` — a raw TL object works.
* `test_the_real_container_shape_sends_and_the_inspected_result_is_the_clicked_one`
  — a real `custom.InlineResults` built from `messages.BotResults`: the flow
  verifies end-to-end and the object handed to the click path **is** `results[0]`.
* `test_a_wrapper_without_a_stored_result_fails_closed` — unsupported shape,
  nothing sent.
* `test_send_message_placed_on_the_returned_object_is_not_accepted` — the live
  bug's shape (`send_message` on the returned object) is **refused**, not trusted.
* `test_a_real_result_without_a_send_message_payload_fails_closed` — a real
  `BotInlineResult(send_message=None)` → `INLINE_RESULT_NO_SEND_MESSAGE`.
* `test_a_real_wrapper_whose_payload_lacks_the_entity_is_entity_missing` — the
  payload **was** inspected; the entity is genuinely absent; no send.
* `test_the_inspection_trace_names_the_real_classes` — the bounded
  `INLINE_RESULT_INSPECTED` line names `wrapper=`/`tl=`.
* `test_the_report_renders_an_unsupported_shape_honestly` — the panel says "not
  inspectable", never "entity missing".
* Renamed `test_zero_inline_results_is_reported_as_rejected` →
  `test_zero_inline_results_is_reported_as_empty` (the refined, correct
  diagnosis).

Coverage required by the task is met: the real wrapper shape with a valid
custom-emoji entity; correct underlying-payload validation (document id, UTF-16
offset/length, span); the inspected result is the clicked one; empty results
fail closed; unsupported/malformed shapes fail closed without sending; a valid
wrapper whose payload lacks the entity does not pass; and the pre-existing
read-back (`via_bot_id` + entity) and no-Unicode-fallback / Saved-Messages-only
tests are untouched and still pass.

## 4. Verification executed

All from the repository root, with `set -o pipefail` where piped.

| Check | Command | Result |
|---|---|---|
| Compile (changed files) | `.venv/bin/python -m compileall -q backend/services/premium_emoji_inline_service.py backend/helper/inline_engine.py backend/bot/handlers/emoji.py tests/test_premium_emoji_inline.py` | **COMPILE_OK** (exit 0) |
| Focused file | `.venv/bin/python -m pytest tests/test_premium_emoji_inline.py -q` | **62 passed** — exit 0 |
| Focused suites | `.venv/bin/python -m pytest tests/test_premium_emoji_inline.py tests/test_emoji_ui.py tests/test_premium_emoji_probe.py tests/test_helper_callback_receipt.py -q` | **144 passed in 0.59s** — exit 0 |
| Full suite | `timeout 400 .venv/bin/python -m pytest tests/ -q` | **5781 passed, 26 skipped, 2 warnings in 118.64s** — exit 0 (baseline 5771; +10 = exactly the 10 new tests, 0 regressions) |
| Whitespace / diff sanity | `git diff --check` | **DIFF_CHECK_OK** (exit 0) |
| Final diff review | full `git diff` of `backend/` + `git status --porcelain -uall` | Scoped to the 4 files; only bounded ids/offsets/class paths logged; no secrets, tokens or message bodies |

No test was skipped, weakened or suppressed; the only assertion changed is the
zero-results diagnosis, which now asserts the **more precise** new verdict
(`INLINE_RESULT_EMPTY`) plus the same `readback.attempted is False`.

## 5. Delivery

| Step | Value |
|---|---|
| Commit | This task's single commit — `backend/services/premium_emoji_inline_service.py`, `backend/helper/inline_engine.py`, `backend/bot/handlers/emoji.py`, `tests/test_premium_emoji_inline.py`, `INVESTIGATION.md`, `IMPLEMENTATION_REPORT.md` |
| Push | Pushed to `origin/main` as the final step of this task |
| Remote verification | `git fetch origin` + `git rev-parse origin/main` + `git merge-base --is-ancestor <commit> origin/main` — see the task's delivery response for the exact SHAs |
| Working tree | Clean except the pre-existing untracked `telegram-self-bot/` (never touched) |

A commit cannot contain its own hash: the full commit SHA and the verified
`origin/main` SHA are reported in this task's delivery response, and the remote
state was re-checked after the push with the commands above.

## 6. Repository / remote verification (task start)

| Check | Command | Result |
|---|---|---|
| Branch / status | `git status --short --branch` | `main...origin/main`; the task's edits in progress + pre-existing untracked `telegram-self-bot/` |
| HEAD | `git rev-parse HEAD` | `1bd843ddc81f3f43ed0937b67f7af011ae724b95` |
| Fetch | `git fetch origin` | no new commits |
| Remote tip | `git rev-parse origin/main` | `1bd843ddc81f3f43ed0937b67f7af011ae724b95` (equal to HEAD) |
| Ancestry | `git merge-base --is-ancestor <c> origin/main` for `1bd843d`, `a54877b`, `e13deee`, `98c2825`, `7d27e11` | all exit 0 |

No history rewriting, no `rebase`, no force-push, nothing discarded; the
pre-existing untracked `telegram-self-bot/` was never touched.

## 7. Not verified live / limitations

1. **No live Telegram or Render execution.** The fix is verified against the
   installed Telethon 1.34.0 source/classes and the full suite — not against a
   live `getInlineBotResults` response. Nothing here claims the feature works
   end-to-end.
2. **Whether Telegram keeps the custom-emoji entity in the returned
   `BotInlineResult` is still unknown** — it could not be observed before,
   because the inspection never read the payload. That is precisely what the
   next live run establishes.
3. **The send (`sendInlineBotResult`) and the exact-message read-back have never
   been exercised live** and are therefore still unproven in production.
4. The helper answers with an article/text result, so the live payload is
   expected to be a `BotInlineMessageText`; a media variant would be inspected
   the same way but has no live sample.
5. The other previously recorded unrelated findings are unchanged and not
   revisited here (nararouter timeout, `PeerChannel` panel-edit warning, the
   `ReadError` burst living in the bounded DB pool).

## 8. Next live test — the stage it must confirm

Deploy this commit, then run `Menu → Emoji → ✨ Send Premium Emoji → reply with a
genuine Premium emoji`. The pipeline must now advance **past** inline-result
inspection. Search the window for:

| Log after `INLINE_QUERY_STARTED` | Meaning |
|---|---|
| `INLINE_RESULT_INSPECTED entity_present=True …` then `INLINE_SEND_STARTED` | inspection now reads the real payload and the send was attempted — the earliest failing stage is cleared |
| `INLINE_RESULT_INSPECTED entity_present=False` with **no** `reason=no_send_message` | a genuine Telegram-side result restriction (payload inspected, entity absent) — `INLINE_RESULT_ENTITY_MISSING` |
| `reason=unsupported` / `reason=no_send_message` | the returned object is not what Telethon documents — report it; the trace now names `wrapper=` and `tl=` |
| `INLINE_SEND_ACCEPTED` + `READBACK_RESULT` | the send and the exact-message read-back ran — compare `document_id_match` / `span_match` / `via_bot_match` |
| `DIAGNOSIS diagnosis=STORED_ENTITY_VERIFIED_RENDER_UNVERIFIED` | the entity is stored and Telegram-attributed; **rendering is still for the owner to confirm visually** |

## 9. Supabase / database

Not touched in any way: no SQL executed, no schema or migration changed, no
Supabase request made, `DATABASE_ARCHITECTURE.md` untouched.
