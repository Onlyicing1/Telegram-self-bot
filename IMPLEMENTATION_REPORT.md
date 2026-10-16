# IMPLEMENTATION_REPORT.md — Private Message Management: reply quality, deterministic validation and duplicate protection

> This file describes ONLY the current state of the repository after this task. It
> **replaces every previous report in full**. The Persian-language-policy report
> (`e8dd2c8`), the Phase-2 report (scope gate, independent actions, dedicated
> persistence, per-person allowance, `e3548dd`), the Phase-1 report ("Private
> Message Management — Phase 1, panel-first, no AI", `bf89042`), the emoji
> implementation (`977247e`), the checkpoint-2 fixes (`e13deee`, `9633c0b`), the
> via-bot feasibility study (`b04cb41`), the `98c2825` audit, the boundary-tracing
> task (`a54877b`), the callback-receipt task (`1bd843d`), the premium-emoji
> inline verification (`39de9cc`, `b463552`, `219c873`, `2eaaf93`) and the LifeOS
> guide website (`58aebac`) are preserved in git history and referenced here only
> by commit id.

| Item | Value |
|---|---|
| Task | Closed-PV AI reply: Persian reply quality, deterministic pre-send validation, and duplicate-reply protection |
| Project | LifeOS Telegram self-bot (`Onlyicing1/Telegram-self-bot`) |
| Branch / base revision | `main` at `e8dd2c8c662c92ea0dc41359b18faa0a544ea3b9` (the commit named in the request) |
| Outcome | **FIXED + unit-TESTED offline.** The prompt no longer invites invented identity/facts or word-for-word prose, malformed/foreign/over-long model output can no longer be delivered, an echoed disclosure is de-duplicated so each notice lands exactly once, and the duplicate mark is now released when enforcement fails before acting. |
| Supabase | **NOT touched — no SQL was executed, no database was modified, no schema changed.** `DATABASE_ARCHITECTURE.md` was NOT edited (nothing in it became inaccurate). |
| Live provider verification | **No** (no provider call in this workspace); see §6. |
| Live Telegram verification | **No** (no live session in this workspace); see §6. |

---

## 1. Root causes — what the source actually establishes

The path was traced end to end:
incoming update → `backend/bot/handlers/task_events.py` handler →
`private_message.enforce_incoming` → `_apply_actions` → `_ai_response` →
`_generate_ai_reply` → `ProviderManager.chat(messages, tools=[])` →
`service.compose_ai_reply` → `client.send_message`.

### 1.1 ESTABLISHED: the prompt was the quality defect

The previous contract required Persian but said nothing about not inventing
facts. The reported outputs are exactly what that allows: a reply that claims a
religion/profession ("من مسیحیان است که به عنوان مساعد ذكاء صناعي … کار میکنم")
invents facts the incoming message never stated; others are stiff,
translation-flavoured or repeat the greeting back. The prompt also ordered "do
not ask questions" absolutely (now qualified) and never required brevity or a
plain greeting for a greeting. Source inspection also confirms `_generate_ai_reply`
passes **only** the system contract plus the incoming text (`tools=[]`,
`apply_persisted_config` first), so the model has no history, no sender and no
facts — whatever it writes about itself is invented by construction.

### 1.2 ESTABLISHED: no delivery-side filter existed

`compose_ai_reply` accepted **any** non-empty string, appended the two notices
and sent it. Consequences proven by the new tests:

* a reply written entirely in **Arabic** was delivered;
* a reply written entirely in **English** was delivered;
* a 4,000-character wall of text was delivered up to the delivery ceiling;
* if a model **echoed** the two notice lines, the delivered message carried each
  of them **twice** — the application cannot detect that today because nothing
  inspects the model's body for its own notice lines.

### 1.3 ESTABLISHED: one verified update has exactly ONE action path

* `enforce_incoming` is called by exactly one handler (`task_events`), which is
  registered exactly once per client instance (`router.register_all`);
  `_trigger_reconnect()` re-uses the same client (Telegram handlers are not
  re-registered), and the failsafe full reset builds a **new** client and
  registers on it — never twice on the same client.
* `private_message.register` registers panels/actions/inputs only — **no**
  message handler of its own.
* Structurally there is one `await _apply_actions(`, one `await _ai_response(`,
  one `await _generate_ai_reply(` and one `service.already_handled(` call site
  (asserted by a new structural test), and `_ai_response` claims the allowance
  through ONE CAS write under the per-sender lock before generating.
* The account's own outgoing messages cannot re-enter: `enforce_incoming`
  returns immediately when `event.out is True`, and the AI reply is sent by the
  account itself, so its own update is outgoing.
* The fixed response and the AI response are independent flags (own column, own
  control) and BOTH run, in the documented delete → fixed → AI order; a repeat
  of the same message is suppressed by the (chat, message) mark, so the pair is
  never mistaken for a duplicate execution.

### 1.4 NOT ESTABLISHED: the cause of the repeated disclosures

The evidence in the request **cannot** be resolved by source inspection, and it
is not asserted here. What the source does say:

* three *different* bodies were observed. Three different bodies can be produced
  by three separate incoming messages — and the bodies read as answers to three
  different inputs — but a sampling provider can also vary the wording of one
  input, so this is **not** proof either way;
* the only *duplication* mechanism the source can explain is the echo case in
  §1.2 (notices twice inside ONE reply). A duplicate *send* is not possible
  without either a Telegram redelivery of the same update (suppressed by the
  bounded `(chat, message)` mark, which was already in place) or a failure that
  left the mark set while the actions never ran (now released — §2.3);
* the honest conclusion is: **root cause not established from source**. The
  production log lines `PRIVATE_MESSAGE_TRACE stage=unauthorized …`,
  `stage=duplicate …` and `stage=ai_reply_rejected reason=…` now make it
  decidable from the owner's own logs: three `stage=unauthorized` lines with
  three different `message_id`s = three separate messages; a `stage=duplicate`
  line at the same `message_id` = a redelivery.

---

## 2. Changes made

### 2.1 The system contract (`backend/bot/handlers/private_message.py`)

Still exactly ONE system message and ONE user message — the context isolation is
untouched (no names, ids, history, memory, media, reply context). Added to
`_AI_SYSTEM_PROMPT`:

* **no invention**: "Never invent or add an identity, name, age, job, religion,
  nationality, biography, ability or any other fact the incoming message does not
  state, and never describe yourself or your own situation";
* **brevity and directness**: "Keep it brief and directly about the incoming
  message: a simple greeting gets a short Persian greeting, nothing more";
* **no echo**: "do not repeat the incoming message back";
* **natural Persian, not translation**: "Write the way one Iranian writes to
  another: contemporary, everyday Persian … not a word-for-word translation, not
  an Arabic sentence … and never an Arabic expression";
* **questions only when needed**: "Do not ask a question unless answering
  genuinely requires one";
* the previously verified Persian requirements, the Arabic/Arabic-expression
  prohibitions, the plain-text-only rule and the "the notices are appended by
  the system" rule are all preserved.

### 2.2 Deterministic pre-send validation (`backend/services/private_message_service.py`)

`validate_generated_reply(generated) -> (reason, body)` is the ONE verdict, and
`compose_ai_reply` now runs it before composing. Reasons (all proven by tests):
`empty`, `notices_only`, `too_long`, `no_letters`, `not_persian_script`,
`arabic` — and `""` (deliverable). A refusal means **no send** and the ordinary
safe-failure path: the claimed allowance is released (only a DELIVERED reply
consumes it). A refused body is dropped, never repaired or paraphrased.

* **Empty/whitespace-only** and non-string bodies → `empty`.
* **Over-long** bodies (> `MAX_AI_GENERATED_LENGTH` = 1200 chars, a documented
  short-reply envelope; the existing 4,000-char delivery ceiling still applies
  to the composed text) → `too_long`.
* **Letter-less** bodies (punctuation/emoji/digits only) → `no_letters`.
* **Foreign script**: no Persian/Arabic-script letters at all, or Latin letters
  outnumbering them (an English answer) → `not_persian_script`.
* **Clearly Arabic** → `arabic`, using the deliberately asymmetric check in
  §5: it needs Arabic evidence AND the total absence of Persian evidence.
* **Echoed notices**: the two application-owned notice LINES are removed from
  the model's body before composing (line-exact, logged), so each notice appears
  exactly once; a body that is only the notices → `notices_only`.

### 2.3 Duplicate protection (`enforce_incoming`)

* The guard is the existing stable key: the **verified** `(chat_id, message_id)`
  pair from the scope gate, marked BEFORE the actions run.
* The check and the mark are **synchronous**, so two coroutines racing on the
  same update cannot both pass it (the concurrency test proves it), and the
  per-sender lock plus the durable CAS claim still bound what one person can
  spend.
* **New**: `service.release_handled(chat_id, message_id)` — the mark is released
  again when enforcement failed BEFORE the actions ran, so a message the feature
  never actually handled is not remembered as done. A completed enforcement
  keeps its mark, and each individual step (delete/fixed/AI) still fails
  independently without suppressing the others — the existing action semantics
  are unchanged.
* No new scheduler, executor, client, listener, loop or parallel runtime; no
  unbounded structure (the registry stays bounded, TTL 900 s, max 512 entries,
  process-local as before).

---

## 3. Files changed and why

```
backend/bot/handlers/private_message.py    edit — the tightened system contract; the release-on-failure
                                                  duplicate mark; docstrings for both
backend/services/private_message_service.py edit — MAX_AI_GENERATED_LENGTH, the script/vocabulary evidence
                                                  tables, is_clearly_arabic, validate_generated_reply,
                                                  notice de-duplication, release_handled, module docstring
tests/test_private_message_scope.py        edit — the prompt contract tests, the validator tests, the
                                                  duplicate/concurrency/action-independence/outgoing tests,
                                                  the structural single-path test; existing harness texts
                                                  updated to valid Persian bodies
```

No unrelated refactor, no dependency added (`unicodedata` is stdlib, already used
elsewhere in this repository), no environment variable added, no schema change,
no SQL executed, `DATABASE_ARCHITECTURE.md` untouched, no second AI runtime and
no change to any other feature's behaviour (the constant and the validator are
local to this feature — asserted by the existing locality test).

---

## 4. Tests run and exact outcomes

| Command | Result |
|---|---|
| `pytest tests/test_private_message_scope.py -q` | **90 passed** (68 before this task; 22 new) |
| `pytest tests/test_private_message_management.py -q` | **38 passed** |
| `pytest tests/test_private_message_panel.py -q` | **47 passed** |
| `pytest … scope + management + panel + canonical schema + setup order + symbols -q` | **261 passed** |
| Full offline suite, run in 6 chunks (6294 collected): chunk 1 / chunk 2+3 / chunk 4+5+6 | 1221 + 1947 + 3059 passed = **6227 passed, 27 skipped, 40 failed** |
| Full offline suite **excluding `tests/test_colored_buttons.py`** (the failing file) | **6215 passed, 27 skipped, 0 failed** |
| `python3 -m py_compile` on both changed Python files + the test file | clean |
| `git diff --check` | clean |

The 40 failures are **entirely** `tests/test_colored_buttons.py`
(`Button style 'primary' rejected by Telethon`): this sandbox has Telethon
1.34.0 while the repository pins 1.43.0. Pre-existing, unrelated to this task —
every other test file passes.

New tests (all offline, driving the REAL `task_events` → `enforce_incoming` path
with real Telethon entities, a recording Telegram client and a recording
provider):

| Requirement | Test |
|---|---|
| `سلام` → one Persian reply, notices once | `test_a_simple_greeting_is_answered_once_in_persian_with_one_copy_of_each_notice` |
| Arabic/English input never changes the contract | `test_the_incoming_language_never_changes_the_contract` |
| Prompt forbids invented facts/self-description | `test_the_prompt_forbids_inventing_facts_and_describing_itself` |
| Prompt requires brevity/natural Persian | `test_the_prompt_requires_brevity_and_natural_persian_over_translation` |
| Empty / whitespace-only rejected | `test_the_reply_validator_refuses_empty_and_whitespace_only_bodies` |
| Malformed & foreign script rejected | `test_the_reply_validator_refuses_letterless_and_foreign_script_bodies` |
| Clearly Arabic rejected (7 samples) | `test_the_reply_validator_refuses_clearly_arabic_bodies` |
| Valid everyday Persian accepted (13 samples) | `test_the_reply_validator_keeps_valid_persian_and_the_ambiguous_greeting` |
| Over-long → safe failure path + claim released | `test_the_reply_validator_refuses_an_over_long_body`, `test_an_over_long_reply_takes_the_safe_failure_path_and_releases_the_claim` |
| Validation reads only the generated text | `test_the_reply_validator_reads_only_the_generated_text` |
| Disclosures exactly once (incl. model echo) | `test_the_disclosures_are_appended_exactly_once_even_when_the_model_echoes_them`, `test_the_ai_reply_carries_both_disclosures_exactly_once`, `test_a_body_that_is_only_the_notices_is_refused` |
| Duplicate update cannot answer twice | `test_a_duplicate_delivery_of_one_message_answers_once` |
| Concurrent duplicates cannot bypass the guard | `test_concurrent_deliveries_of_one_message_answer_once` |
| Same sender, different message still answered | `test_a_different_message_from_the_same_sender_is_still_answered` |
| Fixed + AI both run once, not mistaken for duplicates | `test_both_independent_responses_run_once_for_one_message` |
| Failed validation/generation/delivery releases the claim | above + the existing `test_only_a_delivered_reply_consumes_the_allowance`, `test_a_failed_delivery_gives_the_claim_back`, `test_an_unreadable_counter_store_fails_closed` |
| Failed enforcement is not remembered as handled | `test_a_failure_before_the_actions_does_not_remember_the_message_as_handled`, `test_a_completed_enforcement_keeps_the_duplicate_mark` |
| The account's own outgoing reply is never incoming | `test_the_accounts_own_sent_reply_is_never_an_incoming_human_message` |
| One action path per update (structural) | `test_one_incoming_message_has_exactly_one_action_path` |
| Eligibility boundary unchanged | every pre-existing bot/group/channel/supergroup/unresolved-entity test still passes |

---

## 5. Persian-output validation: what it catches, what it does not

`is_clearly_arabic` is a **bounded deterministic heuristic**, not language
detection:

* **Never rejects a shared code point.** `سلام` alone — the exact ambiguous
  greeting from the report — is accepted (`_persian_evidence` finds nothing and
  `_arabic_tokens` finds nothing, so the Arabic verdict is impossible).
* **Arabic evidence** = Arabic-only letter forms (`ك ي ة ى`), Arabic-only
  vocabulary tokens (`في`, `على`, `كيف`, `حالك`, `شكرا`, `مرحبا`, `السلام`,
  `عليكم`, … matched as whole tokens after diacritic removal and alef folding).
* **Persian evidence** = Persian-only letters (`پ چ ژ گ`), Persian kaf/yeh
  (`ک ی`), Persian function words as whole tokens (`را`, `که`, `این`, `از`,
  `شما`, `چطور`, `کنم`, …) and Persian morphology (the `می`/`نمی` prefix and the
  `است` copula suffix).
* **Verdict rule**: Arabic verdict = (≥2 Arabic tokens) OR (≥1 Arabic token AND
  an Arabic-only letter form) OR (half the letters are Arabic-only forms) AND
  **no** Persian evidence at all.
* **Verified behaviour**: 13 everyday Persian replies (including ones with no
  Persian-only letter: `سلام`, `وقت بخیر، الان پاسخ می‌دم.`, `بله، حتماً`) are
  accepted, and 7 Arabic replies are rejected.

**Honest limitations** (measured, not hidden): `أنا هنا لمساعدتك` was accepted
before the alef folding was added and is rejected now, but the heuristic remains
conservative by design — an Arabic sentence of unlisted vocabulary can pass, and
a Persian sentence typed entirely with Arabic-form letters (`ك`/`ي`) and none of
the listed Persian words would be rejected. The validator also **cannot** judge
fluency: two of the three reported outputs are technically Persian and are
accepted — the prompt is the tool for those, not the validator. No claim of
perfect language detection is made anywhere in this change.

---

## 6. Duplicate protection: guarantees and limitations

**Guaranteed (offline-proven):** for ONE verified incoming human private-chat
message, exactly one enforcement runs per process: one provider generation and
one AI send; a redelivery of the same `(chat, message)` pair and a concurrent
second delivery of it are both suppressed; the fixed response and the AI response
still each run once when both are enabled; different messages from the same
person are still answered under the ordinary quota/interval rules; and an
enforcement that failed before its actions ran is not remembered as handled.
Outgoing (self-account) messages can never enter the path.

**Limitations:** the mark lives in the same bounded, TTL'd, **process-local**
registry as before (a restart or a second process instance forgets it — durable
per-message dedup would need a new table, which this task was explicitly
forbidden from adding), and the per-person interval/limit remains the durable
backstop for that case. Telegram redelivery is already bounded by the TTL.

---

## 7. Live verification — what remains

**NOT verified live:** no provider was called and no Telegram session exists in
this workspace, so "actual Persian fluency from the configured provider" is NOT
claimed. The checks proven here are the prompt contract, the request
construction, the validation/safety behaviour and the duplicate semantics.

The exact acceptance test for the owner (after deploying `main`; Supabase needs
no change for this task):

1. `Menu` → **Private Messages → Closing actions** → mode **Closed** → enable
   **Send AI response**, interval `0`, a limit ≥ 3.
2. From a second human account send `سلام`. Expect **one** short Persian reply,
   ending with each notice exactly once.
3. Send an Arabic message (`كيف حالك؟`) and an English one (`hello`). Expect two
   Persian replies (the incoming language never decides the reply language).
4. Send several messages from a different account and confirm no reply repeats,
   and that the AI counter advances once per delivered reply.
5. If a reply ever looks wrong, `PRIVATE_MESSAGE_TRACE` now names it:
   `stage=ai_reply_rejected reason=<empty|notices_only|too_long|no_letters|not_persian_script|arabic>`,
   `stage=duplicate message_id=…`, or `stage=unauthorized … message_id=…` — these
   lines distinguish "one bad reply" from "the same message answered twice"
   without reading message contents.
