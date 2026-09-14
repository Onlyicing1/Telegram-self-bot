# Telegram Surrounding-Message Provenance Investigation

> **Investigation only — nothing was implemented.** This document replaces the
> previous `INVESTIGATION.md` entirely; no earlier content is preserved, merged,
> or appended. No production code, tests, `IMPLEMENTATION_REPORT.md`, schema,
> migrations, configuration, presentation, delivery logic, or context-retrieval
> logic was modified. No fix, provenance system, cache, database object, or
> polling loop was added.

| Item | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Branch | `main` |
| Revision audited | `79e4b7b` (`docs: investigate Telegram message provenance for chat context`) |
| Feature under review | Telegram surrounding-message context for the AI (`IMPLEMENTATION_REPORT.md`, "Latest phase") |
| Question | Can the surrounding Telegram window distinguish human-authored from self-bot/AI-generated or AI-modified messages? |
| Verdict | **Partially — exactly one positive provenance signal exists (in-memory, non-durable), and the window does not consult it today.** |
| Changes made | only this document |

---

## 1. Scope

The AI now receives a bounded window of nearby messages from the same Telegram
chat (`IMPLEMENTATION_REPORT.md`, "Latest phase — Telegram surrounding-message
context for the AI"). A live Telegram test showed that the resulting context can
contain previous **self-bot/AI output**, which is not desired.

This investigation determines, from source only, whether the current
architecture carries authoritative provenance that separates the six message
categories in the lifecycle:

| # | Category |
|---|---|
| 1 | Genuine human-authored owner messages |
| 2 | Owner messages later edited **in place** by the AI |
| 3 | Messages sent/created by the self-bot |
| 4 | Temporary AI status messages (e.g. `Reading messages...`) |
| 5 | Final AI-generated responses |
| 6 | Messages from other Telegram users |

Hard constraint honored throughout: **`sender_id == owner_id` is never treated
as proof of human authorship.** The AI edits the owner's own Telegram message in
place (3, 6), so the owner's account legitimately holds both human-authored and
AI-produced content.

Trace covered end-to-end:

```
Telegram message/event
  -> AI trigger
  -> AI request creation
  -> original Telegram message identity
  -> edit-in-place delivery
  -> surrounding-message retrieval
  -> (provenance filtering -- ABSENT)
  -> prompt/context construction
```

---

## 2. Current Context Retrieval

### 2.1 The exact path (traced in source)

```
Telegram NewMessage (outgoing, owner)
  `- ai_unified handler                      backend/bot/handlers/ai_unified.py
      `- _execute_ai                         (:572)
          request_chat_id    = event.chat_id            (:594)
          request_message_id = event.message.id         (:595)
          `- _load_telegram_chat_context(...)           (:539)
              `- fetch_telegram_chat_context(...)       telegram_context.py (:364)
                    `- _read_window(...)                (:309)
                          client.iter_messages(
                              chat_id,
                              limit=MAX_CONTEXT_MESSAGES,   # 10
                              max_id=message_id)            (:316)
                    `- _resolve_sender_names(...)        (:323)  <=4 lookups
                    `- build_chat_context(...)           (:241)
                          `- _to_record(...)             (:205)
          `- AIRequest(telegram_context=snapshot)       (:668)
              `- ContextBuilder (pure assembler)
                  `- dataclasses.replace(ctx, telegram_chat=...)
                      `- PromptBuilder -> `[Telegram Chat Context]`
```

One Telegram read per request; `ContextBuilder` and `PromptBuilder` perform no
I/O on the snapshot (documented in `IMPLEMENTATION_REPORT.md` and asserted in
`tests/test_telegram_chat_context.py`, e.g.
`test_prompt_builder_only_formats_the_already_built_snapshot`).

### 2.2 Hard bounds (`backend/ai/conversation/telegram_context.py`)

| Constant | Value | Line |
|---|---|---|
| `MAX_CONTEXT_MESSAGES` | 10 | `:40` |
| `MAX_MESSAGE_CHARS` | 200 (per message, `...` suffix) | `:43` |
| `MAX_TOTAL_CHARS` | 1500 (oldest dropped first) | `:46` |
| `MAX_SENDER_RESOLVES` | 4 | `:49` |
| `FETCH_TIMEOUT_S` | 3.0 (whole read) | `:52` |

### 2.3 What the window keeps and drops

`build_chat_context` (`:241`) is the only selection step:

- drops the triggering message id and any `exclude_message_ids`
  (`if msg_id in excluded`, `:275`) — the handler passes the reply target
  (`ai_unified.py:539`, `exclude` tuple);
- drops anything at/after the anchor ("never invent future context");
- sorts chronologically, keeps the newest `MAX_CONTEXT_MESSAGES`, then applies
  the per-message and total text budgets.

### 2.4 There is no provenance filter of any kind

`_to_record` (`:205`) reads exactly: `sender_id` (`:210`), `id` (`:215`),
`out` (`:218`), `date` (`:219`), the message text, and a media label. Of these,
`out` is used **only** for the render label (`attribution`, `:117-125`,
returning `"You"`) and to skip name resolution (`:334`). Nothing in
`build_chat_context` or `fetch_telegram_chat_context` consults AI provenance.

Consequence: `msg.body` is the message's **current** text, so a previous AI
answer is included verbatim and labelled `You: ...`.

---

## 3. Message Lifecycle

### 3.1 Primary path — the AI never sends a separate answer

`deliver_response` edits the triggering message in place
(`backend/ai/tools/delivery.py:668`, `await event.edit(messages[0])`), and the
thinking/status states are edits of that **same** message
(`ai_unified.py:676`, `:681`). Therefore:

```
T0  owner types "hi"
      -> Telegram msg id 100, out=True, sender_id=owner
T1  AI handles it and edits msg 100 in place
      -> same id 100, out=True, sender_id=owner,
         text = the AI answer, edit_date bumped,
         ReplyResolver[100] = the AI record
T2  owner sends the next message
      -> msg id 101; surrounding window for 101 = [msg 100]
         renders as:  "You: <previous AI answer>"
```

The Telegram message keeps the owner's `id`, the owner's `sender_id`, and
`out=True`. **`out=True` / `sender_id == owner_id` therefore cannot distinguish
category 1 from categories 2 and 5** — the exact trap the task flagged.

### 3.2 Category matrix (as observed by the window)

| Category | `id` | `out` | `sender_id` | Text visible to window | Positive marker? |
|---|---|---|---|---|---|
| 1 Human owner message | own | `True` | owner | as typed | none (absence only) |
| 2 Owner message AI-edited in place (= 5 after delivery) | own | `True` | owner | **the AI answer** | `ReplyResolver` (4.3) |
| 3 Self-bot-created message | new | `True` | owner | self-bot/AI text | none |
| 4 Temporary status string | n/a | n/a | n/a | **never a separate message** (8) | none |
| 5 Final AI response | own (edited) or new (category 3) | `True` | owner | AI text | only when it reused the trigger id |
| 6 Other participant | own | `False` | other | as typed | `out=False` |

---

## 4. Available Provenance Signals

### 4.1 Per-message Telegram metadata (all traced; nothing assumed)

| Field | Read at | Provenance value |
|---|---|---|
| `msg.id` | `telegram_context.py:215` | identity only — no authorship |
| `msg.out` | `:218` | True for **every** owner-account message (1, 2, 3, 5) |
| `msg.sender_id` | `:210`, `:334` | owner for 1/2/3/5; other for 6 |
| `msg.from_id` | not read | same lineage as `sender_id` |
| `msg.date` | `:219` | send time; an in-place edit does **not** change it |
| `msg.edit_date` | **never read by this module** | set on any edit — AI's and humans' alike; not authoritative (10) |
| `msg.message` / `msg.text` | `_message_text` (`:184-190`) | **current** text; for category 2 this is already the AI answer. Renderer-owned content, not a marker |
| `msg.reply_to` | not read | reply linkage only |
| `msg.media` | `classify_message` via `_media_type` (`:192-203`) | media presence only |

### 4.2 Request-level metadata available when the AI request starts

| Signal | Source | Covers |
|---|---|---|
| `request_chat_id`, `request_message_id` | `ai_unified.py:594-595` | the current trigger message only |
| `event.message` | handler entry | the trigger message object |
| `ReplyContext.is_ai_message`, `ai_session_id`, `ai_role`, `ai_content`, `ai_provider`, `ai_model`, `ai_timestamp` | built at `ai_unified.py:493-527`; fields at `context_builder.py:69`; consumed at `prompt/builder.py:294` | the **replied-to** message only |
| `TelegramChatContext` snapshot | `telegram_context.py:134` | the surrounding window (no provenance field) |
| `ReplyResolver` record | `backend/ai/context/reply_resolver.py` | **the only positive marker that a Telegram message id holds AI output** |

### 4.3 The one positive marker — `ReplyResolver`

`backend/ai/context/reply_resolver.py`:

- **What it is:** a process-wide in-memory singleton mapping
  `telegram_msg_id -> ResolvedAIContent` (module docstring, `:10`;
  `_MAX_ENTRIES = 500`, `:26`).
- **Who writes it:** `ai_unified.py:803`, after a **successful** in-place
  delivery — `get_resolver().register(telegram_msg_id=event.message.id, ...)`.
  Only the trigger message id is ever registered.
- **Who reads it:** the reply-to-AI check at `ai_unified.py:495` and `:949`
  (`get_resolver().resolve(reply_msg.id)`), which feeds
  `ReplyContext.is_ai_message` (`context_builder.py:69`) and
  `prompt/builder.py:294`.
- **Lifetime:** RAM-only; `register` (`:81`) evicts the oldest entry at the cap
  (`:126`); `resolve` is `:130`; `clear()` exists for tests. Nothing is
  persisted and nothing is rebuilt after a restart.

Task-side provenance exists but is **not applicable here**:
`backend/ai/task_contract.py` `SCHEDULED_OCCURRENCE_EXTRA` (`:21`),
`trusted_message_ids` (`:139`), and `message_reference_provenance_error`
(`:190`) govern *task-creation argument* authority (which message a task may
delete/save later), not the surrounding-window read.

### 4.4 Answers to the twelve required questions

| # | Question | Answer (evidence) |
|---|---|---|
| 1 | What Telegram metadata is available for surrounding messages? | `id`, `out`, `sender_id`, `date`, `message`/`text`, media — 4.1 |
| 2 | What metadata is available when an AI request starts? | `request_chat_id`, `request_message_id`, `ReplyContext`, the snapshot — 4.2 |
| 3 | How is the original user message identified? | `event.message.id` captured before any status edit — `ai_unified.py:595` — and excluded from the window (`telegram_context.py:275`) |
| 4 | How is edit-in-place delivery implemented? | `deliver_response` -> `event.edit(messages[0])` — `delivery.py:668`; statuses via `event.edit` — `ai_unified.py:676`, `:681` |
| 5 | Is there existing internal tracking of AI-handled message IDs? | **Yes, one:** `ReplyResolver`, for the edited trigger id only — 4.3 (`ai_unified.py:803`) |
| 6 | Can a manually-authored owner message be distinguished from an owner message later edited by AI? | **Only in-process, and only in one direction:** resolver hit => AI-edited; resolver miss => *not proven* human (5, 6) |
| 7 | Can previous AI responses be identified reliably? | Only when the answer reused the trigger id **and** the process still holds the mapping; new-message responses cannot (7) |
| 8 | Can temporary AI/status messages be identified reliably? | **Not needed** — they never persist as separate messages (8) |
| 9 | Can this be done after a process restart? | **No** — the only marker is RAM-only (6.3, 12 G2) |
| 10 | Does the architecture persist any provenance information? | **No.** `ReplyResolver` is in-memory; no table/column/field exists for it (6.3) |
| 11 | Which filtering approaches are authoritative? | Only the `ReplyResolver` denylist and `out=False` for other participants — 9 |
| 12 | Which approaches would be unsafe heuristics? | Text/format matching, `out`-based owner dropping, `edit_date`, length/recency — 10 |

---

## 5. Genuine Owner Messages

**Identifiable positively: no. Identifiable negatively: yes.**

- Telegram metadata for such a message is identical to an AI-edited owner
  message: `out=True`, `sender_id=owner`, arbitrary text
  (`telegram_context.py:210`, `:218`).
- The only available discriminator is the **absence** of a `ReplyResolver`
  record — and absence is not proof: it also describes an AI-edited message
  whose mapping was evicted (`reply_resolver.py:126`) or lost to a restart.
- Therefore the system can only conclude "not *known* to be AI". Treating that
  as "known to be human" would be an allowlist built on absence (10).

Practical consequence: a filter may **keep** these messages (they must be kept),
but the system cannot certify them as human, and must never use certification as
a precondition for keeping.

---

## 6. AI-Edited Owner Messages

This is category 2/5 and it is the **structural** cause of the reported symptom.

### 6.1 Why it happens

The AI answers by editing the owner's own Telegram message (`delivery.py:668`);
the message therefore remains `out=True` with `sender_id=owner`, and its stored
text becomes the AI answer (`_message_text`, `telegram_context.py:184-190`). A
later request's window reads that text back (2.4) and labels it `You`
(`:117-125`).

### 6.2 How they can be recognized (in-process)

`get_resolver().resolve(message_id) is not None` => that id's content is AI
output:

- written at `ai_unified.py:803` after successful delivery;
- read at `:495` and `:949` for the reply target;
- surfaced as `ReplyContext.is_ai_message` (`context_builder.py:69`) and used by
  `prompt/builder.py:294`.

Because the very same handler already uses this mechanism for the reply target,
applying it to the surrounding window is a **reuse of existing architecture**,
not a new provenance system.

### 6.3 Limits of that recognition

| Limit | Evidence |
|---|---|
| RAM-only: a restart empties the map | `reply_resolver.py:10`, `:130` |
| LRU cap 500 with eviction of the oldest | `reply_resolver.py:26`, `:126` |
| Only one id per AI turn is registered (the edited trigger message) | `ai_unified.py:803` |
| Not written on failure paths (only after a successful delivery) | `ai_unified.py:800-818` |

### 6.4 Policy question — deliberately left open

Whether an **historically** AI-edited owner message should be shown to the model
as context or excluded is a product policy, not a source fact. The source proves
such messages **can** be identified while the process lives and **cannot** be
identified after a restart. That case is recorded as uncertain (13, U1/U3)
rather than silently decided.

---

## 7. Self-Bot / AI-Generated Messages

Messages the self-bot **creates** are a different failure mode from in-place
edits: they have **no marker at all**.

| Producer | Source | Registered? |
|---|---|---|
| chunked/split AI answer (`messages[1:]`) | `delivery.py:679` `event.reply(message)` | **no** — returned `Message` discarded |
| edit-failure fallback for the first chunk | `delivery.py:673` `event.reply(messages[0])` | **no** |
| error/failure fallback reply | `ai_unified.py:831` `event.reply(final_text)` | **no** |
| scheduled `send_message` action | `backend/ai/tools/message.py:133` `telegram.send_message(chat_id, text)` | **no** |
| task result delivery | `backend/ai/task_execution.py:727` `telegram.send_message(chat_id, text)` | **no** |
| task outcome notification | `backend/runtime/supervisor.py:380` `TelegramAPI(self.client).send_message(owner, message)` | **no** |
| Deep Save re-upload to Saved Messages | `backend/services/save_service.py:397`, `:428` `send_message("me", ...)` / `send_file` | **no** |

`deliver_response` returns only counts
(`DeliveryResult(True, delivered, len(messages))`, `delivery.py:683`); the
`Message` objects from `event.reply(...)` are not captured anywhere.

**Therefore these messages are indistinguishable from genuine owner messages by
any current source mechanism** — they carry `out=True`, `sender_id=owner`, and
no resolver entry. This is a confirmed gap (12, G1/G3), bounded by the fact that
it only affects chats where the self-bot actually posts (Saved Messages for
scheduled sends/saves/notifications, and the trigger chat for long answers and
failure fallbacks).

---

## 8. Temporary / Status Messages

**On the primary path they are not separate Telegram messages at all.**

- `format_thinking` (`delivery.py:424`), `format_status` (`:435`), and
  `format_failure` (`:449`) are rendered into the **same** triggering message by
  `event.edit(...)`: `ai_unified.py:676` (status callback), `:681` (thinking),
  `:610`, `:624`, `:851` (error/timeout states).
- Because `iter_messages` returns a message's **current** content, a transient
  string such as `Reading messages...` is never observed as a surrounding
  message. By the time a later request reads the window, that id holds either
  the final answer (category 2/5) or the failure text — still the same id.
- Exception: when delivery falls back to `event.reply(...)`
  (`delivery.py:673`, `:679`; `ai_unified.py:831`), that text becomes a **new**
  message and joins category 3, inheriting its "no marker" gap.

Consequence: **no status-specific filter is required**, and no status text can
serve as a provenance marker (it does not persist).

---

## 9. Safe Filtering Mechanisms

Only two filters are authoritative from source.

### 9.1 Keep non-owner messages (category 6)

`out=False` (`telegram_context.py:218`) is authoritative for "not the owner's
account". These messages render by display name (`:117-125`, `:323-361`) and
must be kept unchanged.

### 9.2 Exclude messages positively identified as AI output (the denylist)

Exclude any window message id for which `ReplyResolver.resolve(id)`
(`reply_resolver.py:130`) returns a record. This is:

- deterministic and provider-independent (no model involvement);
- grounded in trusted runtime state written by the handler itself
  (`ai_unified.py:803`);
- **conservative**: it removes only content the system positively knows is AI,
  so it can never remove a message it merely *suspects*;
- already the established mechanism for the reply target
  (`ai_unified.py:495`, `:949`; `context_builder.py:69`; `builder.py:294`).

### 9.3 Preserve existing selection rules

Keep the current-key exclusion and `exclude_message_ids`
(`telegram_context.py:275`; `ai_unified.py:539`), the anchor/future rule, the
chronological sort, and all bounds (`:40-52`).

### 9.4 Fail-open on uncertainty (keep, don't drop)

Because absence of a marker is not proof of humanity (5), any filter must
default to **keeping** a message it cannot classify. Excluding an unclassifiable
message would risk deleting legitimate human context, which the requirement
explicitly forbids.

---

## 10. Unsafe Heuristics

None of the following may be used as authoritative provenance.

| Heuristic | Why it is unsafe (source-grounded reason) |
|---|---|
| Presentation characters `│`, `─`, `└─`, `┘─` | Presentation is renderer-owned and has already changed three times (`IMPLEMENTATION_REPORT.md`: RTL connector phases). The visible format is not a persisted attribute of the message. **Not reliable.** |
| Status/AI text `Reading messages...`, the trigger word, the AI name, `🤖` | Human-reproducible strings. `format_status` (`delivery.py:435`) does not persist (8), and a human may type any of these. **Not reliable.** |
| `sender_id == owner_id` / `out=True` | True for categories 1, 2, 3 and 5 alike (`telegram_context.py:218`) — removes legitimate human context. Explicitly forbidden. |
| `edit_date is not None` / `edit_date` ordering | Humans edit messages too; the field is not even read by the module today. Not authoritative. |
| Text length, recency window, Markdown/emoji shape | Correlational at best; no source attribute proves authorship. |
| Applying `is_ai_message` to arbitrary window messages | That field is derived from the resolver **for the reply target only** (`context_builder.py:69`); reusing it as ground truth for other ids is an allowlist built on absence (5). |
| Asking the provider/model to classify provenance | Non-deterministic and provider-dependent; the architecture requires deterministic, fail-closed behavior. |

**Explicit verdict:** `│`, `└─`, `┘─`, and `Reading messages...` are **NOT**
reliable provenance markers, and the source proves nothing that would make them
authoritative.

---

## 11. CONFIRMED SAFE

1. **Other participants are identifiable.** `out=False` + `sender_id` read at
   `telegram_context.py:218`, `:210`, rendered at `:117-125`. (-> 9.1)
2. **The triggering message and the reply target are already excluded.**
   `telegram_context.py:275` (`if msg_id in excluded`) plus the anchor bound;
   `ai_unified.py:539` supplies the reply-target exclusion. No duplication.
3. **Messages at/after the anchor are never included.** `build_chat_context`
   (`telegram_context.py:241`) enforces the "no future context" rule.
4. **A positive, in-process marker for AI-in-place content exists.**
   `ReplyResolver.register` (`reply_resolver.py:81`) / `resolve` (`:130`), called
   at `ai_unified.py:803`, and already consumed for the reply target
   (`ai_unified.py:495`, `:949`; `context_builder.py:69`;
   `prompt/builder.py:294`).
5. **Status/thinking/presentation strings do not persist as separate Telegram
   messages on the primary path.** `ai_unified.py:676`, `:681`, `:851`;
   `delivery.py:424`, `:435`, `:449`. (-> 8)
6. **The surrounding read is single-shot and bounded**, so a narrowing filter
   cannot introduce extra Telegram traffic: `telegram_context.py:316`
   (one `iter_messages`), `:40-52` (bounds).

---

## 12. CONFIRMED GAP

**G1 — The surrounding window applies no provenance filter at all.**
`_to_record` (`telegram_context.py:205-222`) reads `out`/`sender_id` for display
only (`:117-125`, `:334`); `build_chat_context` (`:241`) consults nothing
provenance-related. This is the direct cause of the observed live behavior.

**G2 — Provenance is not durable.**
The only marker is RAM-only with an LRU cap of 500
(`reply_resolver.py:10`, `:26`, `:126`) and is never rebuilt or persisted, so
after a restart no previously AI-edited message can be recognized.

**G3 — Self-bot-created messages are unmarked.**
Chunked answers and fallback replies (`delivery.py:673`, `:679`;
`ai_unified.py:831`), scheduled sends (`tools/message.py:133`), task results
(`task_execution.py:727`), task notifications (`supervisor.py:380`), and Deep
Save re-uploads (`save_service.py:397`, `:428`) create messages whose ids are
discarded (`delivery.py:683`) and never registered anywhere.

**G4 — No provenance is persisted anywhere in the architecture.**
There is no table, column, or request-scoped record describing which Telegram
message ids the self-bot produced or overwrote. (`ReplyResolver` is the only
tracking, and it is memory-only.)

---

## 13. UNCERTAIN

**U1 — Which category caused the live symptom.**
Source cannot decide whether the observed residual AI text came from
in-place-edited messages (category 2, fixable today via the resolver) or from
self-bot-created messages (category 3, needing a new marker), because the actual
chat content was not inspected (no live access in this investigation).

**U2 — Whether the AI is ever triggered where the self-bot posts as itself**
(e.g. Saved Messages, where scheduled `send_message`, task notifications, and
Deep Save re-uploads accumulate as `out=True` messages — `message.py:133`,
`supervisor.py:380`, `save_service.py:397`, `:428`).

**U3 — Whether provenance must survive a process restart.** The reported
behavior is live-process; G2 means any restart-based expectation would require
the (non-existent) durable provenance.

**U4 — Whether `edit_date` is populated for the in-place AI edit on every
Telegram client.** Relevant only as a hypothetical fallback; the field is not
read today and is not authoritative in any case (10).

**U5 — How historically AI-edited owner messages should be treated.** Depends on
U1/U3 and on a product decision; the source cannot make it (6.4).

**Hypotheses (plausible, not proven):**

- **H1** — The observed contamination is predominantly category 2, since the
  primary delivery path always reuses the trigger message (`delivery.py:668`)
  and long answers/scheduled output are less common than ordinary replies.
- **H2** — If the chat is Saved Messages, a substantial share of the window is
  category 3 from scheduled tasks and Deep Save uploads.
- **H3** — After any process restart, category 2 collapses into
  "indistinguishable from category 1" for the pre-restart history (direct
  consequence of G2).

---

## 14. Minimum Required Fix Surface

Identified, **not implemented**. No schema, no new store, no new subsystem.

**Phase 1 — reuse the marker that already exists** (addresses the observed
symptom if it is category 2):

| File | Change |
|---|---|
| `backend/ai/conversation/telegram_context.py` | optional `is_ai_message` predicate / pre-computed `exclude_ai_ids` accepted by `build_chat_context` (`:241`) and `fetch_telegram_chat_context` (`:364`); one check in the existing exclusion loop (`:275`), fail-open on miss |
| `backend/bot/handlers/ai_unified.py` | `get_resolver` is already imported/used in this module (`:493`, `:495`, `:945`, `:949`) — supply the predicate at the single call site (`:539`), preserving the single Telegram read |

Effect: category 2/5 is removed deterministically; categories 1, 4 and 6 are
untouched; bounds and ordering are unchanged.

**Phase 2 — only if U1 shows category 3 in the live chat:**
extend the existing registry instead of adding one — capture the `Message`
objects currently discarded by `deliver_response` (`delivery.py:673`, `:679`)
and register their ids through the same `ReplyResolver`
(`ai_unified.py:803` is the existing write site). No new map, no schema.

**Explicitly not required / not permitted:** database column, table, or
migration; a second provenance system; a new cache; a message database; new
Telegram polling or update loop; changes to delivery, presentation, providers,
scheduler, ToolExecutor, Taskloom, or the AI runtime history; any text-based
heuristic.

---

## 15. Recommended Next Implementation Stage

**Stage: implement Phase 1 only — bounded `ReplyResolver`-based exclusion of
known AI-edited messages from the surrounding window.**

Focused tests (fake Telegram client + fake/real resolver, no live Telegram) must
prove:

1. a previously AI-edited message id is excluded from the window;
2. a genuine owner message is kept and still renders as `You`;
3. another participant's message (`out=False`) is kept and named;
4. an empty resolver leaves current behavior unchanged;
5. an unknown/failed resolver lookup fails **open** (message kept, never dropped);
6. the single Telegram read is preserved and all bounds
   (10 / 200 / 1500 / 4 / 3.0 s) are unchanged;
7. the reply target is still excluded exactly once and never duplicated;
8. `[Current Request]` ordering and the context-is-not-instruction contract are
   unchanged.

Then re-observe the live chat to resolve U1/U2 and decide whether Phase 2
(registering new self-bot message ids) is required.

**Do not** proceed to Phase 2, add durability, or adopt any text-based heuristic
before U1 is resolved.

---

## 16. Validation Status

| Item | Status |
|---|---|
| Scope honored | only `INVESTIGATION.md` modified |
| Production code / tests / `IMPLEMENTATION_REPORT.md` / schema / migrations / config | **untouched** |
| Fix implemented | **none** (investigation only) |
| New provenance system / cache / table / polling | **none added** |
| Source evidence | every conclusion cites the exact file and function/line (2-10) |
| `git status` | only `INVESTIGATION.md` changed |
| `git diff --check` | clean |
| Full-file review | performed start-to-end; all previous content removed |
| Automated tests run | **none** — no code changed, so no suite is relevant to this document |
| Live Telegram / Supabase / Render verification | **not performed** (out of scope for this investigation) |

### What is proven vs. what is not

**Proven from source:** the retrieval path and its bounds and exclusions (2);
the absence of any provenance filter in the window (2.4); the in-place delivery
mechanism and its consequences for `out`/`sender_id` (3, 6); the existence,
write site, read sites, and non-durable lifetime of `ReplyResolver` (4.3, 6.3);
the unmarked status of self-bot-created messages (7); the non-persistence of
status strings (8); the list of authoritative vs. unsafe filters (9, 10).

**Not proven / not verified:** the actual content of the live chat and therefore
which category caused the observed contamination (U1); any behavior after a
process restart in production (U3); Telegram client rendering/behavior details
(U4); and whether the recommended Phase 1 change is sufficient in practice
(requires the live chat and, later, an implementation stage).

---

**No fix was implemented. Only this document was modified.**
