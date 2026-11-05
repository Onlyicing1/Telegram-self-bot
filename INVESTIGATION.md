# INVESTIGATION — the dead "All Results" button in Test Modules

## Metadata

| Item | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` (`origin`) |
| Branch | `freebuff/3ac6db55494250a4a63f4039`, fast-forwarded onto `origin/main` before any edit |
| `origin/main` at investigation time | `21f3df0d9e0a01ed31c10b3891eb280120726cf7` — `feat: expose provider-attributed failure diagnostics in Test Modules` (read with `git fetch origin main` + `git ls-remote origin refs/heads/main`) |
| Working tree at start | clean; `HEAD` (`586d073`) was an ancestor of `21f3df0`, so the worktree was fast-forwarded first and every finding below was re-established on `21f3df0` |
| Investigation date | 2026-11-05 (UTC) |
| Question | Why does tapping **All Results** in the Test Modules panel produce no visible reaction at all? |
| Not touched | SQL / Supabase, `DATABASE_ARCHITECTURE.md`, model selection, availability classification, provider fallback, Render, any live Telegram session |

## 1. Verdict

The click **is received, admitted, acknowledged, routed, resolved, authorized, dispatched and executed**. It stops at the last step: the panel **edit**.

`_ai_test_details_action` renders the *entire* diagnostic for the last run into **one** message body. A run has no per-provider cap (`backend/ai/model_tester.py::_build_targets`: "There is deliberately NO per-provider cap"), and every failing model now contributes up to six diagnostic lines. Telegram rejects any message text above **4096 characters**. The edit therefore fails with *Message is too long*, `backend/helper/panels.py::_safe_edit` logs a warning and returns `False`, and because the callback was already answered (acknowledgement moved before dispatch in `5b34a3a`), the owner's client shows a completed tap and **no change on screen**. That is exactly the reported symptom: nothing happens, no transition, no visible error.

## 2. The runtime path, stage by stage

| # | Stage | Where | Result |
|---|---|---|---|
| 1 | Button data | `ai.py` `_render_test_results` | `action:ai_test_details` (21 bytes, far below Telegram's 64-byte callback-data cap; `truncate_callback_data` leaves it intact) |
| 2 | Telegram delivery | `/* → */` | The update arrives as a `CallbackQuery` (the helper hook in `backend/helper/client.py::register_helper_hooks` records every delivered query before any gate) |
| 3 | Admission + ack | `panels.py` `_callback_router` → `callback_gate.admit` / `callback_gate.ack` | Admitted; answered **first**, before any lookup or render — so the ack is not the bottleneck and cannot be delayed by diagnostic work |
| 4 | Coordinates / owner / session | `resolve_callback_message`, `is_owner`, `_resolve_session` | All pass for a click on a live Test Modules panel |
| 5 | Prefix dispatch | `data.startswith("action:")` → `_handle_action(event, "ai_test_details", …)` | Correct branch |
| 6 | Registry resolution | `get_action("ai_test_details")` | Registered by `register()` (`register_action("ai_test_details", _ai_test_details_action)`), same id as the button data — no id mismatch |
| 7 | Handler call | `await handler(event, extra, chat_id)` | Signature matches `ActionHandler`; returns `("Test Modules", body, buttons)` |
| 8 | Render | `_render_and_edit` → `_finalize_panel` → `render_edit` | Succeeds; produces one text |
| 9 | Paint | `_present` → `_safe_edit` → `event.edit(...)` | **FAILS** — the text exceeds Telegram's 4096-character message limit; the exception is caught, logged as `[CALLBACK] edit FAILED: …`, and the click finishes with `outcome="completed"` |
| 10 | User-visible outcome | — | Nothing. The ack already happened, the edit was dropped, no error surfaced |

Stage 9 is the first — and only — break. Registration, routing, registry, authorization, session and acknowledgement are all correct, which is why the existing renderer tests kept passing while the live button did nothing.

## 3. Why the body exceeds the limit

* `_build_targets` deliberately tests **every** chat-capable model of every provider (only the explicit global budget truncates it), so a real run's `results` list is dozens to hundreds of entries.
* The All Results view emitted one line per model **plus**, for each failing model, `_failure_diagnostic_lines()`: HTTP status · provider code · provider error type · failure origin, the endpoint/operation with API version, retry-after, provider reason list, and a bounded (160-char) sanitized provider message.
* So the body grows with `O(results)` and nothing bounded its total size. Measured on the previous tree with 45 results and only the simpler diagnostics of that commit, the rendered text was **5408 characters** — already 31 % over the cap. The current renderer adds more lines per failure, so it is larger.

Telegram's per-message text limit is 4096 characters; the edit RPC is rejected outright, not truncated. There is no fallback path that re-sends a shorter message, and `_safe_edit`'s contract is "best effort, never raise", so the failure is silent by design.

## 4. Secondary defect found in the same path

Handler exceptions were also silent. `_handle_action` lets exceptions escape, the router's `except Exception` logs `[CALLBACK] callback router error` and finishes the click as `failed` — but the click has already been acknowledged and nothing is painted. A bug in any action therefore looks exactly like the dead All Results button: acknowledged, no change, no message.

## 5. What was ruled out

| Candidate | Why it is not the cause |
|---|---|
| Button renderer / label glyph | The data string is present in the rendered keyboard and correct |
| Action id mismatch | Button data and `register_action` id are identical; `get_action` resolves the handler (asserted by test) |
| Handler signature / argument parsing | `(event, extra, chat_id)` matches `ActionHandler`; `extra` is empty for the un-paged button |
| Missing registration at startup | `backend/bot/router.py` imports `ai` and `register()` wires the action; the import-time pager registration is a separate, additional wiring |
| Owner gate / session gate | Both pass for the owner's own live panel; a rejection there answers the click immediately, which is not "no reaction on a live panel" |
| Delayed acknowledgement | Ack now runs before dispatch (`5b34a3a`); measured order asserted by the new tests |
| `_last_test_payload` empty | The payload is set by `ai_test_progress.run_streaming_test`; when it *is* empty the handler returns an explicit "No results yet" explanation — the opposite of silence |
| Message-edit failure due to `message not modified` | `_safe_edit` already treats that as a benign no-op; this failure is a different, length-related rejection |

## 6. Diagnosis summary

The interaction does not stop in the panel architecture at all: it stops in **message-size accounting**. The view that must open is unbounded by construction, and Telegram refuses the resulting edit. Any fix must bound the rendered body while keeping the diagnostic content — truncating it would defeat the feature, which exists precisely to show the failures.
