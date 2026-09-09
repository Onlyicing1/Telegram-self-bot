# IMPLEMENTATION REPORT — CURRENT STATE

> **This is a CURRENT-STATE document.** It describes the repository as it
> exists at the tip of this phase. If code changes invalidate any section,
> update this document in the same commit.

---

## 1. Implementation metadata

| Item | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Branch | `main` |
| Base commit (this phase) | `9f84ade` (clean tree, `origin/main` equal) |
| Phase | Expand the natural-language task trigger / schedule interpretation system into a broad semantic trigger layer; fix the live rejection of the multi-line Persian bio-task request |
| Implementation commit | `cce41df` (`feat: broaden semantic trigger interpretation and fix live rejection path`) |
| Report commit | see §13 delivery record |
| Status | **COMPLETE — full suite green (2003 passed, 24 skipped). LIVE Telegram/provider/Supabase verification NOT performed in this workspace** (no session credentials); behavior verified in-process through the real deterministic layers with scripted providers following the interpreter prompt contract |
| Database impact | **NO DATABASE / SCHEMA CHANGE** |

---

## 2. Root cause of the observed live rejection

Request: "هر ۵ دقیقه / میخوام بیو پروفایلم رو آپدیت کنید / یه دیالوگ رندوم از
کاراکتر آیانامی ری از انیمه نئون جنسیس بزاری / که زیر 60 کاراکتر باشه"

Observed response: "I could not turn that into a safe, unambiguous schedule,
so I did not create any task." — a single generic terminal failure in
`CreateTaskTool` used for every interpretation failure.

**Verified in source and by reproduction (before any change):** the
deterministic layers (router, `TaskCandidate`, `parse_schedule`, creation
gate) fully ACCEPT this request — with a compliant structured candidate the
task is created with `{"seconds": 300}`, verbatim multi-line `ai_instruction`,
empty bio arguments, source `آیانامی ری`, `max_length=59`. Therefore the live
rejection originated in the **AI interpretation layer**:

1. The interpreter prompt contained a blanket "If any required detail is
   ambiguous or missing, return JSON null" rule and NO few-shot example, so a
   cautious production model returns `null` for this multi-line Persian
   structure (Persian digit ۵, "بزاری", "میخوام ... آپدیت کنید") even though
   the interval is unambiguous → the generic ambiguity rejection.
2. There was NO structured channel for "semantically clear but
   unrepresentable" capabilities (monthly/yearly calendar recurrence), so
   those also collapsed into the same generic ambiguity message.

---

## 3. Architecture (preserved authority chain)

```
Natural language
→ AI semantic interpretation (TaskInterpreter → TaskCandidate → parse_candidate_output)
→ deterministic validation (TaskCandidate.from_untrusted + parse_schedule + task_trigger)
→ TaskCreationService → durable ai_task definition (ai_tasks table)
→ TaskScheduler / TaskEventDispatcher → occurrence (ai_task_occurrences, CAS claim)
→ AIActionPreparator → provider → candidate → deterministic policy validation
→ PreparedAction (occurrence-specific, version-stamped, in preparation_metadata)
→ TaskExecutionCoordinator (re-proves prepared action at the boundary)
→ ToolExecutor (SOLE execution authority) → registered tool → service → Telegram
```

Unchanged and re-pinned by tests: `RuntimeSupervisor` (lifecycle/recovery),
`TaskScheduler` (scheduler authority), `TaskExecutionCoordinator`,
`Dispatcher` (AI orchestration), `ProviderManager` (selection/fallback),
`ToolRegistry`/`ToolExecutor` (capability boundary + sole execution
authority), `TaskInterpreter` (semantic interpretation), Bio guardian,
prepare-ahead, source fidelity, exactly-once occurrence state machine. The
AI never receives Telegram RPC, SQL, shell, filesystem, or unrestricted
HTTP authority.

---

## 4. Semantic interpretation changes (backend/ai/task_interpreter.py)

- **Narrowed NULL RULE**: return `null` ONLY when the message has no
  recognizable schedule/trigger expression AND no clear action (pure
  chit-chat). Clear intervals/times/events are never ambiguous because of
  wording, number words, omitted "every"/"هر", or multi-line layout.
- **UNSUPPORTED CAPABILITY contract**: semantically clear but
  unrepresentable requests (monthly/yearly calendar recurrence, "هر آخر
  هفته"/"every weekend", "first of month", "every year on January 1")
  return the single envelope `{"unsupported": "<capability>"}` — never
  fabricated seconds, never a null.
- **`TaskUnsupportedError(TaskInterpretationError)`** with `.capability`;
  `interpret()` recognizes the envelope; `CreateTaskTool` surfaces it as an
  honest distinct message ("I understood your request, but <capability> is
  not supported yet...") instead of the ambiguity rejection.
- **Few-shot EXAMPLE** mirroring the exact failing multi-line Persian
  structure → the expected compliant JSON (300s interval, `bio_set_text`
  empty args, verbatim `ai_instruction`).
- **COMPOUND INTERVALS**: guidance to compute totals (`every 1 hour and 30
  minutes` → 5400s; `هر نیم ساعت` → 1800s) or emit the structured compound
  shape (`{"hours": 1, "minutes": 30}`).
- **TIME-OF-DAY & CALENDAR**: once (`today at 5` / `فردا ساعت 8 صبح`), daily
  (`every day at 9`, `هر شب ساعت 11`), weekly with the full Persian weekday
  table (دوشنبه=0 .. یکشنبه=6; English Monday=0 .. Sunday=6).
- **EVENT TRIGGERS**: `direction outgoing` for "when I write X", `incoming`
  + sender for "when X messages me", `media_type` for photo/video/voice/
  audio/document/sticker/animation, `is_mention`, `is_reply`, `contains`/
  `text_equals`/`starts_with`, chat names (incl. channel names for channel
  posts) — names only, never ids.

---

## 5. Deterministic validation changes

- **`backend/ai/task_candidate.py`** — compound multi-unit interval
  canonicalization: one or more known duration unit keys sum to seconds
  (`{"hours": 1, "minutes": 30}` → 5400), bounded by a 10-year maximum.
  Calendar `months`/`years` keys are NOT in any unit vocabulary — they fall
  through to honest rejection, never fabricated seconds. All other shape
  bounds unchanged (bool/list/unknown-unit/oversized → rejection with
  structure diagnostics).
- **`backend/ai/task_trigger.py`** — trigger spec/resolved/matcher/summary
  extended with `media_type` (bounded enum: photo/video/voice/audio/
  document/sticker/animation) and `is_mention` (bool), both optional,
  ANDed with the other conditions, counted as trigger conditions (a
  mention-only trigger is valid). Unknown media kinds ("gif") and
  non-bool flags are rejected.
- **`backend/ai/task_event_dispatcher.py`** — `extract_event_context` now
  derives `media_type` deterministically from the Telethon message
  (photo/video/voice/audio/sticker/animation/document/other/None) and
  `mentioned` from `event.mentioned` — both genuinely observable, no new
  execution authority.

---

## 6. Trigger/schedule capabilities now supported

| Capability | Representation | Status |
|---|---|---|
| interval seconds/minutes/hours/days/weeks | `interval` + `{"seconds": N}` (digits any script or number words, Persian + English) | SUPPORTED (incl. `هر نیم ساعت` → 1800) |
| compound durations (1h30m, 2d6h) | `{"seconds": total}` or structured `{"hours":1,"minutes":30}` → canonicalized sum | SUPPORTED |
| monthly / yearly calendar recurrence | — | HONEST UNSUPPORTED (`{"unsupported": ...}` → "not supported yet") |
| one-time at time | `once` `{"at", "timezone"}` (today/tomorrow at HH) | SUPPORTED |
| daily at time | `daily` `{"hour", "minute", "timezone"}` (هر روز/روزانه/هر شب) | SUPPORTED |
| weekly at time | `weekly` `{"weekday" 0-6, "hour", ...}` (Persian + English weekday names) | SUPPORTED |
| message received (sender/chat/content) | `event` trigger: `direction=incoming` + `sender`/`chat`/`contains`/`text_equals`/`starts_with` | SUPPORTED (names resolved from trusted dialogs; unresolvable → honest failure) |
| self-message ("when I write X") | `direction=outgoing` + content condition | SUPPORTED |
| media received (any) | `has_media=true` | SUPPORTED |
| media type (photo/video/voice/audio/document/sticker/animation) | `media_type` | SUPPORTED |
| reply received | `is_reply=true` | SUPPORTED |
| mention | `is_mention=true` (event.mentioned) | SUPPORTED |
| channel posts | chat-name trigger (a channel post arrives as a message in that chat) | SUPPORTED via chat field |
| weekend ("هر آخر هفته") | — | HONEST UNSUPPORTED (two weekdays, not representable in one weekly task) |
| sender/chat ids from the model | — | REJECTED (names only; runtime resolution) |

---

## 7. AI-generated content system (preserved, re-pinned)

Verbatim `ai_instruction` (creation gate + interpreter contract), source
extraction and self-attribution policy, exact-quote fail-closed,
language/length (`زیر 60` ⇒ max 59, never truncated), bounded regeneration
(max 3 rounds) then fail closed, prepare-ahead side-effect freedom,
occurrence-specific prepared actions with boundary re-proof, Bio guardian
(60s rolling window at the shared mutation boundary, concurrency-safe,
success-only advancement, FloodWait honesty), retry semantics (guardian
rejection ⇒ permanent, no retry storm), exactly-once CAS occurrence
lifecycle. No changes to these layers in this phase.

---

## 8. Tests added (tests/test_task_semantic_triggers.py — 70 tests)

- Exact live-failing Persian request → 300s bio task with verbatim
  multi-line `ai_instruction`, empty arguments, source `آیانامی ری`,
  `max_length=59`; English equivalent.
- Interval matrix (24 phrasings): seconds/minutes/hours/days/weeks in
  Persian + English, digits + number words, no-هر forms, half hour.
- Compound intervals (5 phrasings) + structured compound canonicalization
  through the real path.
- Monthly/yearly (10 phrasings) → honest "not supported yet", never the
  ambiguity text, zero tasks persisted.
- Time-of-day (9): daily/nightly/weekly Persian + English weekday mapping,
  once today/tomorrow.
- Event triggers (8): self-message text_equals/contains, incoming content
  match, mention (EN+FA), reply; this-chat + media_type → trusted chat_id;
  unresolvable sender → honest failure.
- Genuine ambiguity (3) → unchanged honest rejection (and NOT the
  unsupported text).
- Deterministic matcher units: photo vs video distinction, mention flag,
  media_type derivation from message objects, bounded spec validation,
  summary labels.
- Prompt contract test (semantic-interpretation, null-rule, unsupported,
  compound, time-of-day, weekday, media_type, is_mention, example strings
  present) and `TaskUnsupportedError` envelope surfacing.

Updated: `tests/test_task_candidate_contract.py` — compound unit-keyed
shapes now canonicalize (5 new cases); month/year keys, zero/negative
compound parts stay rejected.

---

## 9. Validation

| Command | Result |
|---|---|
| `pytest tests/test_task_semantic_triggers.py -q` | **70 passed** |
| Adjacent suites (10 task/trigger/source/prepare/guardian files) | **322 passed** |
| `pytest tests/ -q` (full suite) | **2003 passed, 24 skipped, 1 warning** in 64.40s |
| `py_compile` (5 backend + 2 test files) | OK |
| `git diff --check` | clean |
| Regex audit | no new regex (interpretation remains provider-semantic; trigger matching is deterministic field logic) |
| Changed files | exactly 7 (5 backend + 2 tests) |

Live Telegram / live provider / live Supabase verification: **NOT performed**
(no session credentials in this workspace). The exact failing path was
exercised end-to-end in-process: the request flows through the real
`CreateTaskTool` → `TaskInterpreter` → deterministic candidate validation →
persistence; the scripted provider plays the role the (now explicit) prompt
contract assigns the model.

---

## 10. Database impact

**NO DATABASE / SCHEMA CHANGE.** The existing `ai_tasks` / `ai_task_occurrences`
two-table model represents every supported trigger: schedules persist in
`ai_tasks.schedule` (JSONB), event triggers in `schedule.trigger` (resolved
form: trusted ids + conditions), `ai_instruction` in its existing column.

---

## 11. Architecture boundaries preserved

`RuntimeSupervisor` = lifecycle/recovery · `TaskScheduler` = scheduler ·
`TaskExecutionCoordinator` = execution orchestration · `Dispatcher` = AI
orchestration · `ProviderManager` = provider selection/fallback ·
`ToolRegistry`/`ToolExecutor` = capability boundary + sole execution
authority · Telethon = Telegram authority · AI = reasoning/generation only.
No second scheduler/executor/event authority; no arbitrary RPC/SQL/shell/
filesystem/HTTP authority given to the AI; no regex-based intent parsing;
no UI/Taskloom/provider/bio-guardian changes.

---

## 12. Remaining limitations / blockers

1. **Live verification**: no live Telegram/provider/Supabase run in this
   workspace; in-process tests exercise the real deterministic layers with
   scripted providers.
2. **Monthly/yearly recurrence and weekend triggers**: semantically clear
   but not representable by the existing scheduler — returned as the honest
   "not supported yet" response (by design; implementing them requires
   scheduler/calendar support, out of scope).
3. **Semantic-content triggers** ("messages about X") are expressed as
   `contains` substrings; deep semantic matching is not executed — the
   matcher is deterministic substring logic.
4. **Sender/chat resolution** requires the model to name resolvable
   entities; unresolvable names fail honestly with clarification.
5. **Canonical-quote authenticity** remains unverified by design (no trusted
   corpus; exact-quote requests fail closed; accepted lines are
   self-attributed generated dialogue).

---

## 13. Delivery record (verified)

| Item | Value |
|---|---|
| Implementation commit | `cce41df` (`feat: broaden semantic trigger interpretation and fix live rejection path`) |
| Report commit | `(filled after creation — see git log)` |
| Push | `git push origin main` (non-force fast-forward); verified via `fetch` + `rev-parse` + `ls-remote` |
| Verified remote HEAD | equals local HEAD post-push (authoritative `ls-remote`) |
| Working tree | Clean except the pre-existing untracked nested clone `telegram-self-bot/` (untouched) |
