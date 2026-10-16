# IMPLEMENTATION_REPORT.md — Private Message Management: Persian reply quality + configurable automatic counter reset

> This file describes ONLY the current state of the repository after this task. It
> **replaces every previous report in full**. The previous Private Message
> Management reports (Phase 1 `bf89042`, the scope/action/persistence phase
> `e3548dd`, the Persian-language policy `e8dd2c8`, the validation + duplicate
> protection `702c1d1`), the emoji reports, the colored-button work, the font
> registry work and the LifeOS guide website remain readable in git history and
> are referenced here only by commit id.

| Item | Value |
|---|---|
| Task | Repair the unnatural closed-PV AI replies, and add a configurable per-person automatic counter reset |
| Project | LifeOS Telegram self-bot (`Onlyicing1/Telegram-self-bot`) |
| Branch / base revision | `main`, branched from `702c1d1fb4e018709f6ede90af5695357e54e613` (verified `= origin/main`, fetched at task start) |
| Outcome | **Repaired and offline-TESTED.** The reported malformed body is now refused instead of delivered, a bare greeting can no longer receive an elaborate self-describing paragraph (it gets one short idiomatic Persian greeting), and the per-person allowance gains a configurable, durable, atomic automatic reset. |
| Supabase | **NOT touched — no SQL was executed, no database was modified.** `DATABASE_ARCHITECTURE.md` was NOT edited (it stays as the owner approved it). The required schema change is a **repository-only SQL proposal awaiting the owner's manual application** (§5). |
| Live provider verification | **No** — no provider credentials in this workspace (§7). |
| Live Telegram verification | **No** — no live session in this workspace (§7). |

---

## 1. Persian response quality — what changed

### 1.1 Why the previous fix could not be enough

The previous change (`702c1d1`) added a prompt contract plus a deterministic
validator. The reported body proves the gap:

```
سلام، من در خدمت شما هستم. لطفاً حرفی به شما میادی میکنم. چگونه می توانم به شما کمک کنم؟
```

It is **Persian script**, has letters, is inside the length envelope, is not
Latin-dominant and is not classified as Arabic — so it passed every check that
existed and was delivered. The specific mechanical defect is `می توانم`: Persian
writes the imperfective prefix **attached** to its verb (`میتوانم`, joined with
a ZWNJ, or `میتوانم`). A whitespace-delimited token that is exactly the bare
prefix is not Persian word formation — it is the mark of mechanically generated
pseudo-Persian. (`من در خدمت شما هستم` is a separate, non-deterministic problem:
a stiff ceremonial opener; that half is addressed by the contract, §1.3.)

### 1.2 Changes (`backend/services/private_message_service.py`)

1. **A new deterministic refusal: `malformed_persian_prefix`.** A
   whitespace-delimited word that is exactly `می` or `نمی` refuses the body. The
   ZWNJ is deliberately **not** a separator, so `میتوانم` / `نمیدانم` stay ONE
   token and correct Persian is never hit (proven by tests).
2. **A new deterministic refusal: `repetitive_greeting`.** A body that uses the
   same greeting word more than once (`سلام! سلام، خوبی؟`) is refused — the
   "repetitive greetings" class from the request. The word list is three
   greetings long and is documented as one narrow defect class, not a grammar
   check.
3. **A deterministic answer path for a confidently recognized bare greeting.**
   `classify_trivial_greeting()` matches the ENTIRE incoming message (after
   removing invisibles, trimming punctuation, folding Arabic yeh/kaf and alef
   variants, lower-casing, and bounding the length and word count) against a
   small documented greeting list (`سلام`, `سلام علیکم`, `السلام علیکم`, `درود`,
   `وقت بخیر`, `صبح/ظهر/عصر/شب بخیر`, `hi`, `hello`, …). A message with any extra
   content — a question, a request, a name — is **not** trivial and stays an
   ordinary AI request.
4. **`MAX_TRIVIAL_GREETING_REPLY_LENGTH = 80`** and
   `compose_trivial_greeting_reply()`. If the model's body for a recognized bare
   greeting is refused **or** longer than that envelope, the delivered body is
   the short idiomatic Persian greeting for that greeting's time of day
   (`سلام! وقت شما هم بخیر.` / `صبح شما هم بخیر.` / …). Nothing about the owner or
   the assistant is claimed in it, and no question is asked.
5. **`validate_generated_reply` documents its own limit** ("not provably
   malformed", never "proven fluent"). No claim of a Persian grammar detector is
   made anywhere.

### 1.3 Changes (`backend/bot/handlers/private_message.py`)

1. **Few-shot examples inside the existing system contract** (not extra
   messages — the request shape stays exactly `[system, user]`), covering a
   greeting, thanks, a Latin-script message, a simple social question and a
   message that needs no elaborate answer. They state the register and the
   expected LENGTH, are explicitly "examples, never templates", and every
   previously asserted prompt requirement is preserved verbatim (verified by the
   existing prompt tests, which still pass unchanged).
2. **A bounded regeneration: exactly ONE retry** (`AI_GENERATION_ATTEMPTS = 2`)
   for a **general** request whose body was refused. It reuses the same provider
   path (`ProviderManager.chat(messages, tools=[])`), the same context isolation
   (still exactly two messages, still no history/name/id/context), and appends a
   repair instruction (`_AI_CORRECTION_PROMPT`) to the **system** contract. A
   second refusal is dropped: nothing is sent and the claimed allowance is
   released. A bare greeting never spends a second provider call — it is answered
   deterministically (§1.2).
3. **The provider is still always called first for every eligible AI-response
   message.** The deterministic greeting is a *fallback for a bad body*, never a
   substitute for generation: if the provider fails outright (exception,
   timeout, `success=False`, empty text) the ordinary safe-failure path runs —
   no canned text is sent and the claim is released.

---

## 2. Deterministic code vs provider quality — the honest split

**Guaranteed by deterministic code (offline-proven):**

* the reported malformed body is refused, with reason `malformed_persian_prefix`
  — it can never be delivered again;
* a body with a detached `می`/`نمی` prefix, or that greets twice, is refused;
* empty, whitespace-only, letter-less, over-long, English/Latin-dominant and
  clearly Arabic bodies are refused (pre-existing checks, unchanged);
* a recognized bare greeting is answered by **one** short idiomatic Persian
  greeting, never by an elaborate or self-describing paragraph, even when the
  model proposes one;
* both application-owned disclosures are appended exactly once, outside the
  model's body, on every delivery path (model body, deterministic greeting, and
  after a successful regeneration);
* no refused body is ever sent; a refusal/failure releases the claim and consumes
  nothing;
* the context isolation is unchanged: exactly the system contract (plus, on the
  retry only, the repair sentence) and the incoming message text reach the
  provider — asserted by tests on the exact message list, roles and kwargs.

**NOT guaranteed by deterministic code — provider-quality dependent:**

* whether an arbitrary, non-greeting Persian sentence a provider produces is
  genuinely fluent, grammatical and idiomatic. Nothing here is a Persian grammar
  or semantics checker; the deterministic checks certify **shape**, and the
  greeting path is the only place a final body is fully deterministic.
* whether the model respects the contract's prohibition on inventing biography,
  identity, religion, profession or abilities. There is no deterministic way to
  prove a sentence invents nothing (the earlier attempt at keyword lists is
  exactly what this task forbids re-doing). It is enforced by the contract, by
  the few-shot examples (none of which contains an invented fact), and by the
  deterministic greeting body, which contains no self-description at all.
* fluency of a **general** request's reply that survived validation. The bounded
  regeneration raises the chance of a good result; it does not certify one.

---

## 3. Automatic per-person counter reset — semantics

**Configurable.** Two settings on the existing Private Message Management panel:
`ai_counter_reset_enabled` (default **OFF**) and `ai_counter_reset_seconds`
(default **86400** = one day, allowed range **60 s … 365 days**).

**Window semantics (rolling, deterministic, restart-safe):**

* Each person's used count lives in a **rolling window anchored to their first
  counted reply in the current window** (`private_message_senders.window_started_at`).
* The window is **only ever rolled in the incoming-message quota path**. There is
  **no periodic task, scheduler, background loop, worker or new runtime**; the
  same is true for the whole feature.
* When automatic reset is ON and `now − window_started_at ≥
  ai_counter_reset_seconds`, the **next eligible incoming message** observes the
  expired window, resets that person's count to zero and starts a new window
  **inside the same claim**. That message is the first reply of the new window, so
  the person's allowance becomes available again exactly then — and the ceiling
  applies again immediately.
* The reset is evaluated and applied **atomically with the counter claim**: it is
  the same conditional write. There is no separate step that could disagree with
  the allowance.
* When automatic reset is **OFF**, nothing ever expires (a manual reset is the
  only edge) — but the window anchor is still maintained, so enabling the feature
  later never loses a person's position.
* A **process restart cannot reset a counter or restart a window**: both are
  durable columns.
* An automatic reset **resets the count only**. The maximum, the minimum interval
  between replies, the three action flags and eligible-message scope are
  unaffected.
* The **minimum interval (`ai_interval_seconds`) is a separate floor** read from
  the last *delivered* reply, independently of the window — so a reset never
  shortens the anti-spam delay.
* **Human-private eligibility still precedes everything**: the scope gate runs
  first and fails closed, so a bot, group, supergroup, channel, linked discussion
  group, outgoing message, unresolved sender or contradictory event never
  creates a window, never rolls one, never claims and never spends anything.

**Stated limitation (deliberate, fail-closed, and pinned by a test):** a counter
row that already sits at its ceiling with **no recorded window** — i.e. a row
created before the migration whose person had used up their allowance — is *not*
silently forgiven when automatic reset is enabled. It has no counted reply to
anchor a window to, and treating "window unknown" as "window expired" would need
a NULL-safe CAS identity (a row with `ai_replies_used = 1` and no window could
otherwise be rolled twice by two racing claims). Such a person stays at the
ceiling until the owner uses the manual reset or raises the limit. The feature
never grants an allowance it cannot justify; the manual reset remains the escape
hatch.

### 3.1 Panel controls (`Menu → Private Messages → Closing actions`)

| Control | Kind | Effect |
|---|---|---|
| `Automatic counter reset` | toggle (`action:pm_reset_window`) | enables/disables the automatic reset; the rendered mark and the stored value agree |
| `Set counter reset interval` | input (`input:pm_policy:reset_interval`) | accepts a whole number of **minutes** or an explicit unit (`90s`, `30m`, `2h`, `3d`, case-insensitive); rejects malformed, negative, fractional, unknown-unit, overflowing and out-of-range input **without changing anything**, and says so |
| `Max AI replies per person` | input (pre-existing) | unchanged |
| `Unlimited AI replies` | toggle (pre-existing) | unchanged |
| `Set AI reply interval` | input (pre-existing) | unchanged |
| `Reset AI counters` | action (pre-existing) | manual reset, unchanged |

The panel reports the current reset configuration in **both** the main panel and
the Closing Actions panel, including the explicit `OFF (counters only change when
you reset them)` state, and the Closing Actions panel documents the window
semantics in the same text the owner's question is answered with (when a window
starts, when the allowance returns, and that only the count is reset). All
symbols come from the existing registry (`sym.SCHEDULE_INTERVAL`, `sym.ICON_STATS`,
`_mark`) — no emoji literal and no variation-selector glyph was introduced.

---

## 4. Manual reset and unlimited mode — unchanged behaviour, re-verified

* **Manual reset** (`Reset AI counters`, `action:pm_reset_counters`): deletes that
  owner's counter rows, which clears the used count **and** the reset window for
  every tracked person of this owner, with truthful success/failure reporting (a
  failed delete reports failure and the panel says so).
* **Unlimited mode**: bypasses the maximum-count ceiling only. It does **not**
  disable the separate `ai_interval_seconds` floor, and it does not change the
  window/eligibility rules.
* **A delivered reply consumes one reply** under limited mode; a failed or
  rejected generation/delivery releases its claim (including restoring the
  expired window it had rolled, so a failure cannot silently hand out a fresh
  window).
* The delete / fixed-response / AI-response actions remain independent and keep
  their fixed order.

---

## 5. Schema change required — MANUAL SUPABASE ACTION

The existing tables do **not** contain the necessary fields, so this feature
needs a schema change. It is provided as a repository artifact only:

```
supabase/migrations/20261016000001_add_private_message_counter_reset.sql
```

| Object | Change | Why |
|---|---|---|
| `private_message_config.ai_counter_reset_enabled` | `boolean NOT NULL DEFAULT false` | the automatic-reset switch; OFF by default so applying the file can never start resetting anything |
| `private_message_config.ai_counter_reset_seconds` | `integer NOT NULL DEFAULT 86400`, `CHECK (BETWEEN 60 AND 31536000)` | the window length; the CHECK mirrors the panel's own bounds |
| `private_message_senders.window_started_at` | `timestamptz` (nullable) | the person's window anchor. NULL means "no window recorded yet" and the next counted reply starts one — deliberately not `NOT NULL DEFAULT now()`, which would backfill everyone with an artificial instant this migration cannot know |

Pure `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` / constraint swap: additive,
idempotent, no table created or dropped, no row touched, no RLS/grant change, no
counter reset, no allowance handed out. It is **not** part of the ONE setup block
in `DATABASE_ARCHITECTURE.md` §31.3 (that document is protected and was not
edited), so **the owner must apply it manually in the Supabase SQL editor**.

**Consequences until it is applied (stated honestly):** the configuration write
names the two new columns, so on a database without them Supabase rejects the
statement, the service keeps the previous in-memory state and every Private
Message setting save reports failure instead of pretending to be stored. No
automatic reset can run. **Automatic reset is therefore NOT claimed operational
until the owner applies the SQL and verifies it.**

**CAS dependency to be aware of:** the claim's conditional write now includes the
window (`ai_replies_used` **and** `window_started_at`), which is what closes the
boundary race described in §3. The filter is only added when the row actually has
a window, and a PostgreSQL `timestamptz` equality is a timestamp comparison, so
the value read from the row round-trips exactly (the same pattern the existing
release CAS already used for `last_ai_reply_at`). This is the one behaviour that
has to be confirmed against the real Supabase project after the migration is
applied.

---

## 6. Files changed and why

```
backend/bot/handlers/private_message.py             edit — few-shot examples in the system contract,
                                                            _AI_CORRECTION_PROMPT, _generate_reply_body
                                                            (bounded regeneration + greeting envelope),
                                                            the two reset controls (toggle + input),
                                                            panel status text and window documentation
backend/services/private_message_service.py         edit — reset config fields + bounds + parse/format
                                                            helpers + setters + summary fields; the greeting
                                                            classifier, the deterministic greeting reply, the
                                                            two new validator refusals; module docstring
backend/db/client.py                                edit — window-aware claim decision, the window in the
                                                            CAS identity, the window in the release
                                                            compensation, the two new config columns
supabase/migrations/20261016000001_add_private_message_counter_reset.sql
                                                    new  — the reviewable, additive SQL proposal (§5);
                                                            NOT executed
tests/test_private_message_scope.py                 edit — 27 new tests (quality + quota/reset, end to end
                                                            through the real handler and the real db contract);
                                                            2 existing tests re-pointed at a non-greeting input
tests/test_private_message_panel.py                 edit — 3 new tests (registration, rendered status +
                                                            window documentation, toggle/input validation)
```

No other file was touched: no new scheduler/worker/event handler/Telegram
client/executor/AI runtime, no dependency added, no environment variable added,
no SQL executed, no other feature's behaviour changed (`_AI_SYSTEM_PROMPT` is
still referenced by this module only — asserted by the pre-existing locality
test), and the request-shape structural counts are unchanged (one
`_apply_actions`, one `_ai_response`, one `_generate_ai_reply`, one
`already_handled`, two `_send_text` call sites).

---

## 7. Tests run and exact outcomes

| Command | Result |
|---|---|
| `pytest tests/test_private_message_scope.py -q` | **118 passed** (90 before → +28) |
| `pytest tests/test_private_message_management.py -q` | **38 passed** (unchanged) |
| `pytest tests/test_private_message_panel.py -q` | **50 passed** (47 before → +3) |
| the three files together | **206 passed** |
| `tests/test_database_setup_order.py` + `tests/test_canonical_schema_reconciliation.py` + `tests/test_symbols.py` | **87 passed** (the new migration file is additive and undocumented in §31, as designed) |
| full offline suite, this tree | **6258 passed, 27 skipped, 40 failed** (6325 collected — the pinning test above was added after that run and is covered by the focused runs) |
| full offline suite, pristine base revision `702c1d1` | **6228 passed, 27 skipped, 40 failed** (6295 collected) |
| failure-set diff (base vs this tree) | **identical — zero new failures** |
| `python -m py_compile` on the three changed modules + both changed test modules | clean |
| `git diff --check` (worktree and staged) | clean |

The 40 failures are **entirely** `tests/test_colored_buttons.py`
(`Button style 'primary' rejected by Telethon`): this sandbox has Telethon
**1.34.0** while the repository pins 1.43.0. They were reproduced unchanged on
the pristine base revision — pre-existing and unrelated to this task.

**One pre-existing time-dependent test flaw (not touched, not mine):**
`tests/test_private_message_panel.py::test_the_schedule_panel_names_the_next_transition`
fails whenever the wall clock is inside the rule it creates (22:00–23:00 UTC),
because the panel then correctly reports the rule's *end* as the next transition
while the test asserts the *start*. It failed on the pristine base revision at
22:55 UTC and passed in both trees after 23:00 UTC — i.e. it is a wall-clock
dependent test, unrelated to this change (and fixed by nothing here).

New tests (all offline; they drive the real `task_events → enforce_incoming`
path with real Telethon entities, a recording Telegram client and a recording
provider):

| Requirement | Test |
|---|---|
| the exact reported malformed body | `test_the_reported_malformed_persian_reply_is_refused` |
| detached verb prefix refused, attached forms kept | `test_a_detached_persian_verb_prefix_is_refused_but_attached_forms_are_not` |
| repetitive greeting refused | `test_a_reply_that_greets_twice_is_refused` |
| only a bare greeting is trivial | `test_only_a_bare_greeting_is_classified_as_a_trivial_greeting` |
| the deterministic greeting is short, natural, discloses once, invents nothing | `test_the_deterministic_greeting_is_short_natural_and_carries_both_disclosures_once` |
| few-shot examples exist without weakening the contract | `test_the_prompt_carries_idiomatic_few_shot_examples_without_weakening_the_contract` |
| the reported case end to end (provider still asked; broken body never sent) | `test_the_reported_malformed_reply_makes_a_bare_greeting_get_the_short_greeting` |
| elaborate-but-valid greeting reply replaced | `test_an_elaborate_but_valid_reply_to_a_greeting_is_replaced_by_the_short_greeting` |
| provider failure never yields canned text | `test_a_provider_failure_never_produces_a_canned_reply` |
| bounded regeneration (exactly one, same isolation) then drop | `test_a_refused_body_for_a_general_request_is_regenerated_once_then_dropped` |
| the regeneration is a real repair path | `test_the_bounded_regeneration_delivers_a_good_second_attempt` |
| disclosures exactly once on every path | `test_the_disclosures_land_exactly_once_on_every_delivery_path` |
| automatic reset disabled | `test_automatic_reset_is_off_by_default_and_nothing_is_ever_rolled` |
| first reply starts the window; ceiling holds inside it | `test_the_first_reply_starts_the_window_and_the_limit_holds_inside_it` |
| eligible again only after expiry | `test_the_same_person_becomes_eligible_again_only_after_the_window_expires` |
| two people, independent windows | `test_two_people_keep_independent_reset_windows` |
| manual reset clears counts and windows | `test_a_manual_reset_clears_the_counts_and_the_windows` |
| unlimited bypasses the ceiling, keeps the interval | `test_unlimited_mode_bypasses_the_ceiling_but_keeps_the_minimum_interval` |
| counter-store failure fails closed at a boundary | `test_a_counter_store_failure_at_a_reset_boundary_fails_closed` |
| failure restores the expired window | `test_a_failed_generation_restores_the_expired_window_instead_of_restarting_it` |
| restart keeps the window | `test_the_window_survives_a_restart` |
| concurrent messages at the boundary | `test_concurrent_messages_at_a_reset_boundary_do_not_bypass_the_quota` |
| the persistence contract closes the boundary race (real db path, stale read) | `test_the_persistence_contract_itself_closes_the_window_race` |
| the window is part of the CAS identity | `test_the_reset_window_is_part_of_the_claim_cas_identity` |
| out-of-scope events touch no window | `test_an_out_of_scope_event_never_creates_or_rolls_a_reset_window` |
| a pre-migration ceiling row is not silently forgiven | `test_a_counter_row_that_predates_the_window_column_is_not_silently_forgiven` |
| reset config validated, never half applied | `test_the_reset_configuration_is_validated_and_never_half_applied` |
| interval input: units accepted, junk refused | `test_the_reset_interval_input_accepts_units_and_refuses_anything_else` |
| one control per setting, no duplicates | `test_the_actions_panel_carries_exactly_one_control_per_setting` |
| rendered status agrees with storage, ON and OFF | `test_the_panel_shows_the_reset_configuration_including_when_it_is_disabled` |
| input validation + documented semantics in the panel | `test_the_reset_interval_input_validates_and_states_the_window_semantics` |

All pre-existing eligibility (bot / group / supergroup / channel / discussion
group / unresolved sender / outgoing), duplicate-update, action-independence,
context-isolation and ProviderManager-usage regressions still pass unchanged —
except the **two** tests whose input had to change: they asserted that an
Arabic/over-long model body is never sent, and they used `سلام` as the input,
which is now (deliberately) a deterministic greeting rather than a provider body.
Their input is now a general Persian request, so the assertion keeps its exact
original meaning.

---

## 8. Live verification — what remains

**NOT verified live.** This workspace has no `API_ID`/`API_HASH`/`SESSION_STRING`
and no AI provider credentials, so **no provider was called and no Telegram
request of any kind was made**. Specifically **not** claimed: that the configured
provider now returns fluent Persian for arbitrary inputs, and that the automatic
reset behaves correctly against the real Supabase project.

The exact owner acceptance steps (after deploying `main` **and applying the SQL in
§5**):

1. `Menu` → **Private Messages → Closing actions** → mode **Closed** → enable
   **Send AI response**, interval `0`, a limit ≥ 3.
2. From a second human account send `سلام`. Expect **one** short Persian
   greeting (not an elaborate paragraph), ending with each notice exactly once.
3. Send a general request (e.g. `میشه کمکم کنی؟`). Expect a short Persian reply;
   if the model returns a broken body it is refused and retried once, and nothing
   is sent if the retry also fails (the counter does **not** advance).
4. In **Closing actions**, enable **Automatic counter reset** and set the interval
   to `2m`. Send enough messages to hit the limit, wait past two minutes from the
   person's first reply in the window, then send another: it must be answered
   again (the counter restarts at 1) — before the interval elapses it must not be.
5. Use **Reset AI counters** and confirm counts *and* windows are cleared; toggle
   **Unlimited AI replies** and confirm the ceiling is gone while the reply
   interval still throttles.
6. Useful log lines: `PRIVATE_MESSAGE_TRACE stage=ai_reply_rejected reason=<…>
   attempt=<1|2>`, `stage=ai_greeting_too_elaborate`, and
   `[PM_DB] counter window expired, count reset: owner=… sender=… was=…`.

---

## 9. Delivery state

Delivered as **one commit on `main`** carrying the code, the SQL proposal, the
tests and this report; it is pushed fast-forward to `origin/main` only because
the branch was verified to be exactly the remote's ancestor (no rebase, no
force-push, no history rewrite, no unrelated file staged). The verified remote
SHA, the `merge-base --is-ancestor` result and the final working-tree state are
re-verified with the commands below after the push and reported to the owner:

```
git fetch origin
git rev-parse origin/main
git merge-base --is-ancestor <sha> origin/main
git status --short
```
