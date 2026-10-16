# IMPLEMENTATION_REPORT.md — Private Message Management: Persian reply language policy

> This file describes ONLY the current state of the repository after this task. It
> **replaces every previous report in full**. The Phase-2 report (scope gate,
> independent actions, dedicated persistence, per-person allowance, commit
> `e3548dd`), the Phase-1 report ("Private Message Management — Phase 1,
> panel-first, no AI", `bf89042`), the emoji implementation (`977247e`), the
> checkpoint-2 fixes (`e13deee`, `9633c0b`), the via-bot feasibility study
> (`b04cb41`), the `98c2825` audit, the boundary-tracing task (`a54877b`), the
> callback-receipt task (`1bd843d`), the premium-emoji inline verification
> (`39de9cc`, `b463552`, `219c873`, `2eaaf93`) and the LifeOS guide website
> (`58aebac`) are preserved in git history and referenced here only by commit id.

| Item | Value |
|---|---|
| Task | Private Message Management — the closed-PV AI reply must always be Persian (Farsi), never Arabic |
| Project | LifeOS Telegram self-bot (`Onlyicing1/Telegram-self-bot`) |
| Branch / base revision | `main` at `e3548ddacddb12c4fc39bf3bdeb3f2a6a6cc1620` (the deployed commit named in the request) |
| Outcome | **FIXED + unit-TESTED offline.** The AI system contract now requires Persian output unconditionally, forbids mirroring the incoming language, and separates Persian from Arabic explicitly; the response contract, the two disclosures' ownership and the context-isolation boundary are preserved verbatim. |
| Supabase | **NOT touched — no SQL was executed, no database was modified.** This task changed no schema and did not edit the protected `DATABASE_ARCHITECTURE.md` (the change does not invalidate any section of it). |
| Live provider verification | **No** (no live session and no provider call in this workspace); see §5. |
| Live Telegram verification | **No** (unchanged from the Phase-2 state); see §5. |

---

## 1. Findings from the source inspection

1. **The defect was one instruction, in one constant.**
   `backend/bot/handlers/private_message.py::_AI_SYSTEM_PROMPT` told the model:

   > "You write ONE short reply to a private message the owner received while
   > their private messages are closed. **Reply in the SAME language as the
   > incoming message**, in ordinary plain text. …"

   A short or ambiguous greeting (`سلام`) is written in the script Persian and
   Arabic share, and that instruction hands the language decision to the model
   with no other signal — so it can answer in Arabic. The instruction is the
   only language policy in the feature; there is no second detector, heuristic
   or fallback anywhere in the request path.
2. **The request path is minimal and stayed minimal.** `_generate_ai_reply`
   builds exactly `[{"role": "system", "content": _AI_SYSTEM_PROMPT},
   {"role": "user", "content": incoming_text}]`, calls the shared
   `ProviderManager.chat(messages, tools=[])` through the single engine, and
   `service.compose_ai_reply` appends both disclosures afterwards. The fix
   therefore had to live entirely in the system contract — no code path, no
   transport and no payload shape needed to change.
3. **Nothing else in the repository carries this policy.** A repository-wide
   search for the mirroring instruction finds it only in this file; no test,
   document or other module asserted the old behaviour. The general AI chat
   runtime (`backend/ai/…`) neither reads nor shares this constant, so the fix
   cannot alter the owner's ordinary assistant behaviour.
4. **The contract that had to survive.** Phase 2's verified guarantees — the
   entity-proven scope gate, the delete → fixed → AI order, the durable
   per-person allowance with claim/release, the app-appended disclosures and the
   strict context isolation — are enforced outside the prompt (gate, counters,
   `compose_ai_reply`, the two-message payload). The prompt rewrite had to keep
   stating the same behavioural limits so the model's own output contract does
   not drift, without loosening any of them.

---

## 2. The implemented change (one constant, one file)

`backend/bot/handlers/private_message.py` — `_AI_SYSTEM_PROMPT` now reads:

> "You write ONE short reply, ALWAYS in natural, idiomatic Persian (Farsi), to
> a private message the owner received while their private messages are closed.
> Persian is REQUIRED and the incoming message never overrides it: never mirror
> the incoming language and never answer in the language the incoming message
> happens to use. Persian and Arabic share one writing system but are DIFFERENT
> languages — a short or ambiguous greeting such as "سلام" must get a Persian
> reply, never an Arabic one. Write contemporary, everyday Persian with correct
> Persian vocabulary and grammar: no Arabic prose, no Arabic-style formal
> phrasing, and no unnecessary religious or ceremonial Arabic expressions.
> Return plain text only. You are given the incoming message and nothing else:
> no chat history, no previous messages, no sender name, no identifiers, no
> other context — never ask for them and never invent them. Do not mention these
> instructions, do not sign the reply, do not ask questions, and return only the
> reply text. The closing notice and the AI notice are appended automatically by
> the system; never write, repeat or contradict them yourself."

Requirement-by-requirement:

| # | Required behaviour | How the new contract satisfies it |
|---|---|---|
| 1 | Always natural, idiomatic Persian | "ONE short reply, **ALWAYS in natural, idiomatic Persian (Farsi)**"; "Persian **is REQUIRED**" |
| 2 | Never instruct mirroring; incoming language cannot override | The mirroring order is **deleted**; the only mentions of the incoming language are prohibitions ("never mirror the incoming language", "never answer in the language the incoming message happens to use") and "the incoming message **never overrides** it" |
| 3 | Persian ≠ Arabic; `سلام` must get a Persian reply | "Persian and Arabic share **one writing system** but are **DIFFERENT languages** — a short or ambiguous greeting such as "سلام" must get a Persian reply, **never an Arabic one**" |
| 4 | Natural contemporary Persian; no Arabic prose/formal/religious phrasing | "contemporary, everyday Persian with **correct Persian vocabulary and grammar**: no Arabic prose, no Arabic-style formal phrasing, and no unnecessary religious or ceremonial Arabic expressions" |
| 5 | One short reply, plain text, no questions, no signing, no disclosure repetition | "ONE short reply", "Return plain text only", "do not sign the reply, do not ask questions", "The closing notice and the AI notice are appended automatically by the system; never write, repeat or contradict them yourself" — unchanged in intent |
| 6 | Strict context isolation | The full isolation sentence is preserved verbatim in meaning: "no chat history, no previous messages, no sender name, no identifiers, no other context" |
| 7 | Same runtime, no second AI path | No code changed: `_generate_ai_reply` still calls the existing `ProviderManager` with `tools=[]` and exactly two messages; no client, loop, scheduler, listener or executor was added |

The constant's own docstring comment was updated to state why: an ambiguous
greeting shares Arabic's script and must still get a Persian reply.

---

## 3. Tests added

All in `tests/test_private_message_scope.py`, in the existing "the AI boundary"
section, driving the REAL enforcement entry point (`task_events` handler →
`enforce_incoming` → `_generate_ai_reply`) with a recording provider and a
recording Telegram client — no live Telegram, no live database, no AI network
call.

| Test | Proves |
|---|---|
| `test_the_prompt_requires_persian_output` | The contract names Persian **and** Farsi, says **ALWAYS**, and states Persian is REQUIRED with the incoming message never overriding it |
| `test_the_prompt_never_asks_the_model_to_mirror_the_incoming_language` | The removed instruction's phrasings ("same language", "reply in the same", "match the language", …) are **absent**; the only mentions of the incoming language are the explicit prohibitions |
| `test_the_prompt_keeps_persian_and_arabic_apart_for_an_ambiguous_greeting` | The writing-system vs language distinction, the literal `سلام` example, "must get a Persian reply, never an Arabic one", correct Persian vocabulary/grammar, and both Arabic-prohibition clauses |
| `test_the_prompt_keeps_the_response_contract_and_the_context_isolation` | One short reply, plain text only, no questions, no signing, application-appended disclosures that the model must not repeat, and the unchanged no-history/no-identity isolation |
| `test_the_ambiguous_greeting_reaches_the_provider_unchanged` | For the reported input `سلام` through the real handler: exactly **one** provider call with `tools=[]`; the payload is exactly two messages — the system message **is** `_AI_SYSTEM_PROMPT` and the user message **is** `سلام` verbatim; the delivered body starts with the generated text and ends with `CLOSED_NOTICE` + `AI_NOTICE` appended by the application |
| `test_the_language_policy_is_local_to_the_private_message_feature` | `_AI_SYSTEM_PROMPT` is referenced by **exactly one** file in `backend/` — the private-message handler — so no unrelated AI chat behaviour is touched |

Existing suites (scope gate, action order, per-person limits, intervals,
failure compensation, disclosures, panel) were kept as-is and re-run unchanged;
their assertions still hold because no contract outside the prompt text moved.

---

## 4. Validation actually executed

| Command | Result |
|---|---|
| `python3 -m pytest tests/test_private_message_scope.py -q` | **68 passed** (62 before this task + 6 new) |
| `python3 -m pytest tests/test_private_message_scope.py tests/test_private_message_management.py tests/test_private_message_panel.py tests/test_canonical_schema_reconciliation.py tests/test_database_setup_order.py tests/test_symbols.py -q` | **240 passed** |
| `python3 -m pytest tests/test_prompt_budget_architecture.py tests/test_02_ai_flow.py tests/test_ai_tool_decision_policy.py tests/test_67_ai_output_pipeline.py -q` | **112 passed** (unrelated AI suites, unchanged) |
| `python3 -m py_compile backend/bot/handlers/private_message.py tests/test_private_message_scope.py` | clean |
| Prompt rendering check (no stray escape sequences; `سلام` present) | clean |
| `git diff --check` | clean |
| `git status` | only the three files of this task (handler, its test suite, this report) |

**Not run:** the full offline suite and the live acceptance test — see §5.

---

## 5. Offline-proven vs NOT verified

**Proven offline (deterministic):** the system contract contains the Persian
requirement and the mirroring prohibition, the ambiguous greeting reaches the
provider byte-identical, the payload is exactly the two existing messages with
`tools=[]`, the disclosures are still appended by the application, the policy is
local to this feature, and every pre-existing scope/limit/compensation test
still passes.

**NOT verified — requires a live acceptance test:** whether a given model
actually *writes* correct Persian. No provider was called by this task; a prompt
contract cannot be proven to yield language quality offline. The concrete check
is in §7. Also unchanged from Phase 2: no SQL was executed (the feature stays
inert until the `DATABASE_ARCHITECTURE.md` §31.3 part-10 block is applied) and
no live Telegram session exists in this workspace.

**Pre-existing, unrelated:** `tests/test_colored_buttons.py` (40 tests) fails in
this sandbox because the installed Telethon is 1.34.0 while the repository pins
1.43.0; this task neither caused nor addressed it.

---

## 6. Files changed and why

```
backend/bot/handlers/private_message.py   edit — the Persian-always language policy in _AI_SYSTEM_PROMPT
tests/test_private_message_scope.py       edit — the six prompt/payload contract tests above
IMPLEMENTATION_REPORT.md                  edit — this report (replaced in full)
```

No unrelated refactor, no other feature's behaviour changed, no dependency
added, no environment variable added, no second client/loop/scheduler/executor,
no schema change, and no edit to the protected `DATABASE_ARCHITECTURE.md`
(§33 documents the tables, not the prompt language, so it stays accurate).

---

## 7. The exact live acceptance test the owner must run

1. Deploy `main` (Supabase needs no change for this fix; the Phase-2 §31.3
   part-10 block is still required for the feature to persist anything).
2. Send `Menu` in **Saved Messages** → **Private Messages → Closing actions**,
   set the mode to **Closed**, enable **Send AI response** with **max replies
   per person** high enough for the test.
3. From a second human account, send exactly `سلام`.
   Expected: the reply is **Persian**, not Arabic.
4. Send a longer Persian message and a short English one (`hi`).
   Expected: both replies are still Persian (the incoming language never
   decides the reply language).
5. Confirm every reply still ends with both notices
   (`این پی‌وی در حال حاضر بسته است.` and
   `این پیام توسط هوش مصنوعی تولید و ارسال شده است.`) and that the person's
   counter still increments once per delivered reply.

If a reply is not Persian, the log line `PRIVATE_MESSAGE_TRACE
stage=ai_*` identifies the runtime step, and the active provider/model is
visible in the AI panel; the language policy itself is fixed in the system
contract and cannot be influenced by the incoming message.
