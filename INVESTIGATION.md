# Investigation — Premium Custom Emoji Inline-Result Inspection Failure (2026-10-10)

Canonical, latest-only record of why the live Premium custom-emoji inline flow
stopped at `INLINE_RESULT_REJECTED`/`entity_present=False reason=no_send_message`
after every earlier stage had succeeded, what object Telethon actually returns
from `inline_query`, and which object was read instead. This document replaces
the previous investigation record in full. Earlier records — the via-bot
feasibility study (`b04cb41`), the implementation record (`977247e`), the
checkpoint-2 fix record (`e13deee`), the `98c2825` audit, the boundary-tracing
task (`a54877b`), and the callback-receipt task (`1bd843d`) — remain readable
in git history and are referenced here only by commit id.

| Item | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Branch | `main` |
| Revision this record was produced from | `1bd843d` (verified = `origin/main` at the start of this task) + this task's changes |
| Telethon | **pinned `telethon==1.34.0`** (`backend/requirements.txt:1`); installed 1.34.0 — every shape below was read from that installed source |
| Question | What object does `TelegramClient.inline_query` actually return, where does its `send_message` live, and why did inspection find none? |
| Verdict | **(1)** `inline_query` returns `telethon.tl.custom.InlineResults` — a list of `custom.InlineResult` **wrappers** whose raw TL `BotInlineResult` lives in `.result`; the wrapper has **no `send_message` attribute at all**. **(2)** `_inspect_inline_result` read `getattr(results[0], "send_message", None)` — always `None` on a wrapper — so it reported `reason=no_send_message` and `INLINE_RESULT_REJECTED` **without ever looking at the payload Telegram actually returned**. **(3)** Telegram stripping the entity was never established and is **not** the cause: the send was never attempted. **(4)** Fixed by reading the payload through the real shapes (`.result.send_message`), with a distinct honest diagnosis for an empty answer, an unsupported shape and a real result without a send-message payload. |
| Production code changed | **Yes** — `backend/services/premium_emoji_inline_service.py` (inspection + diagnoses), `backend/helper/inline_engine.py` (expose the zero-results reason), `backend/bot/handlers/emoji.py` (honest report line). No mechanism, client, loop or scheduler added. |
| Live Telegram verification | **Not performed in this task** (no live session in this workspace); the fix is source-verified against the installed Telethon and test-pinned with the real wrapper/TL classes. |

---

## 0. Status of every claim

| # | Statement | Status |
|---|---|---|
| 1 | `TelegramClient.inline_query` returns `custom.InlineResults(...)` | **CONFIRMED** — `telethon/client/bots.py` (`inline_query`, final statement) |
| 2 | `InlineResults` is a `list` subclass whose elements are `custom.InlineResult` wrappers | **CONFIRMED** — `telethon/tl/custom/inlineresults.py` `__init__` (`InlineResult(client, x, original.query_id, entity=entity)`) |
| 3 | The wrapper stores the raw TL object in `self.result` and exposes `.message`/`.click`; it has **no `send_message`** | **CONFIRMED** — `telethon/tl/custom/inlineresult.py` `__init__` (`self.result = original`), `message` property, `click()`. Runtime check: `hasattr(custom.InlineResult, "send_message") is False` |
| 4 | The raw object (`types.BotInlineResult` / `BotInlineMediaResult`) is what carries `send_message` | **CONFIRMED** — `telethon/tl/types/__init__.py` field lists (`BotInlineResult(id, type, send_message, …)`) |
| 5 | `getattr(results[0], "send_message", None)` is therefore **always `None`** on a real (or correctly faked) wrapper | **CONFIRMED** by 2–4 |
| 6 | That exactly reproduces the live evidence: `INLINE_RESULT_INSPECTED entity_present=False reason=no_send_message` → `DIAGNOSIS diagnosis=INLINE_RESULT_REJECTED` | **CONFIRMED** — `_inspect_inline_result` (`send_message is None` branch) → `classify_diagnosis` (`not ok` → `INLINE_RESULT_REJECTED`) |
| 7 | The inspected object and the clicked object are the **same** wrapper | **CONFIRMED** — service inspects `results[0]`, then `_send_result` → `inline_engine.click_result(self_client, chat_id, results[0])` → `result.click(chat_id)` |
| 8 | The earlier `e13deee` "fix" addressed this | **REFUTED** — it changed `getattr(first, "message")` to `getattr(first, "send_message")`; **both** are wrapper-level and both are `None`. Neither the schema field nor the object was wrong *in the way assumed*: the object was wrong. |
| 9 | Telegram stripped / rejected the custom-emoji entity | **NOT ESTABLISHED — and contradicted by this path**: inspection never read a payload, so no stripping claim is supported; the send was not attempted. |
| 10 | The store of the entity inside the helper's `InputBotInlineMessageText` was invalid | **REFUTED** (unchanged) — the payload validated and the query started with `entity_count=1` |

---

## 1. The reported live evidence

Deployed commit `1bd843ddc81f3f43ed0937b67f7af011ae724b95`, Render healthy. Three
consecutive attempts (😈 and 🥰) reached **every** earlier stage:

```
[CALLBACK] received …
_handle_action: action_id='emoji_premium_inline'
[PREMIUM_INLINE] SOURCE_ENTITY_VALIDATED
[PREMIUM_INLINE] OUTBOUND_PAYLOAD_BUILT          (offset 15, length 2, text_utf16_len 17)
[PREMIUM_INLINE] INLINE_QUERY_STARTED
```

and each then produced:

```
[PREMIUM_INLINE] INLINE_RESULT_INSPECTED entity_present=False reason=no_send_message
DIAGNOSIS diagnosis=INLINE_RESULT_REJECTED
```

**What this establishes:** the callback, the action, the source-entity
extraction, the outbound payload build (offset/length/UTF-16 length exactly as
designed) and the inline query all ran; the failure is precisely the inspection
of the object returned by `inline_query`. **What it does NOT establish:** that
Telegram stripped the entity, that a send was attempted, or that Telegram
rejected the emoji — `no_send_message` is the inspection's own verdict about
the object it was handed.

---

## 2. Mandatory source inspection — the real return value (Telethon 1.34.0)

### 2.1 What `inline_query` returns

`telethon/client/bots.py`:

```python
result = await self(functions.messages.GetInlineBotResultsRequest(...))
return custom.InlineResults(self, result, entity=peer if entity else None)
```

So the return value is **not** a raw TL object and **not** a plain list of TL
results.

### 2.2 What `InlineResults` contains

`telethon/tl/custom/inlineresults.py`:

```python
class InlineResults(list):
    def __init__(self, client, original, *, entity=None):
        super().__init__(InlineResult(client, x, original.query_id, entity=entity)
                         for x in original.results)
        self.result = original      # the messages.BotResults TL object
        self.query_id = original.query_id
        ...
```

Each element is a `custom.InlineResult` **wrapper**. `len(results)` and
`results[0]` work (it is a `list`), which is why the existing code got that far.

### 2.3 What the wrapper exposes

`telethon/tl/custom/inlineresult.py`:

```python
def __init__(self, client, original, query_id=None, *, entity=None):
    self._client = client
    self.result = original            # ← the raw BotInlineResult / BotInlineMediaResult
    self._query_id = query_id
    self._entity = entity

@property
def message(self):                    # ← the documented accessor
    return self.result.send_message

async def click(self, entity=None, ...):
    ...
    req = functions.messages.SendInlineBotResultRequest(
        peer=entity, query_id=self._query_id, id=self.result.id, ...)
```

The wrapper's public surface is `type`, `message`, `title`, `description`,
`url`, `photo`, `document`, `click`, `download_media`. **There is no
`send_message` attribute on the wrapper.** Verified at runtime against the
installed 1.34.0: `hasattr(custom.InlineResult, "send_message") is False`; an
instance has `.result` and `.message` only.

`inline_engine._sanitize_results`' `getattr(r, "send_message", None)` is **not**
affected: it runs on the *helper bot's* outbound `InputBotInlineResult` objects,
which really do have `send_message`.

### 2.4 Where `send_message` really lives

`types.BotInlineResult(id, type, send_message, title=None, description=None,
url=None, thumb=None, content=None)` and
`types.BotInlineMediaResult(id, type, send_message, photo=None, document=None,
title=None, description=None)` — the payload field is on the **TL object**, e.g.
`wrapper.result.send_message`.

### 2.5 The defect in one line

```python
first = results[0]                          # a custom.InlineResult WRAPPER
send_message = getattr(first, "send_message", None)   # ALWAYS None
```

`_inspect_inline_result` therefore never looked at
`first.result.send_message` — the object Telegram had actually returned — and
reported `entity_present=False reason=no_send_message`, which
`classify_diagnosis` mapped to `INLINE_RESULT_REJECTED`.

---

## 3. Why the `e13deee` change did not fix it

`e13deee` is titled "read send_message field on BotInlineResult in checkpoint 2"
and replaced `getattr(first, "message", None)` with
`getattr(first, "send_message", None)`. Both reads target the **wrapper**, and
neither exists there, so the live log after `e13deee` shows the same
`reason=no_send_message`. The commit's premise — that `first` *is* a
`BotInlineResult` — was the actual mistake. The schema note it added
(`BotInlineResult` stores its message under `send_message`, not `message`) is
true, but only of `first.result`.

---

## 4. The object traced through the code

| Stage | Code | Object handed on |
|---|---|---|
| query half | `inline_engine.query_results` (`self_client.inline_query(…)` → `results`) | the `custom.InlineResults` list, **elements are `custom.InlineResult` wrappers** |
| inspection | `premium_emoji_inline_service._inspect_inline_result(results, payload)` | `results[0]` — the **wrapper** |
| send half | `premium_emoji_inline_service._send_result` → `inline_engine.click_result(self_client, chat_id, results[0])` → `result.click(chat_id)` | **the same wrapper** (its `click` reads `self.result.id` + `self._query_id`) |

Conclusion: the object that was *inspected* and the object that would have been
*clicked* are one and the same. Only the **read** was wrong — the send mechanism
never needed to change, and it did not.

The Saved-Messages-only gate, the genuine-entity requirement, the UTF-16
offset/length validation, the `document_id` checks and the Telegram-owned
`via_bot_id` verification are all untouched by this fix.

---

## 5. The fix

**`backend/services/premium_emoji_inline_service.py`**

1. New `_resolve_inline_result(result)` — the single place that turns an
   inline result into its stored payload, understanding **both** real shapes:
   * a raw `types.BotInlineResult` / `types.BotInlineMediaResult`
     (`_INLINE_RESULT_TYPES`) → used as-is;
   * anything else → its `.result`, which must itself be one of those types.
   Returns `(send_message, tl_object, kind, detail)`. No payload found yields
   `kind` `"unsupported"` (a shape this module refuses to guess about) or
   `"no_send_message"` (a real result with no payload) with a bounded `detail`
   — never a stripping claim.
2. `_inspect_inline_result` now:
   * reports an **empty** result list as `reason="empty"`;
   * resolves the payload via `_resolve_inline_result` instead of
     `getattr(first, "send_message", None)`;
   * records `wrapper_class` / `tl_class` (runtime `module.Class`, bounded,
     non-sensitive) for the trace and the evidence block;
   * keeps the entity scan, `document_id_match` and `span_match` exactly as
     before, now against the payload Telegram actually returned.
3. New diagnoses + honest branches: `INLINE_RESULT_EMPTY`,
   `INLINE_RESULT_UNSUPPORTED`, `INLINE_RESULT_NO_SEND_MESSAGE` (added alongside
   the existing `INLINE_RESULT_REJECTED` / `INLINE_RESULT_ENTITY_MISSING`);
   `classify_diagnosis`, the post-inspection failure branches and
   `outcome_summary` were extended to match.
4. No second client, listener, scheduler, update loop or send mechanism; no
   Unicode fallback; no fabricated entity; nothing sent when the payload cannot
   be inspected safely.

**`backend/helper/inline_engine.py`** — exposes the zero-results reason as
`INLINE_ZERO_RESULTS_REASON` (and uses it in `query_results`), so the service
can distinguish an *empty answer* from a *failed query* without matching prose.
`trigger`'s contract is unchanged.

**`backend/bot/handlers/emoji.py`** — the panel report renders the reason
honestly ("the helper bot returned none" / "not inspectable (…)") instead of
labelling every non-entity case "entity missing".

---

## 6. Diagnosis decision table (what each outcome now means)

| Diagnosis | Evidence it is derived from | Meaning |
|---|---|---|
| `INLINE_RESULT_EMPTY` | the helper answered with zero results (or the inspected list was empty) | nothing to inspect; **nothing sent** |
| `INLINE_RESULT_UNSUPPORTED` | `reason=="unsupported"` — the wrapper/object shape is not a stored `BotInlineResult` (or `.result` is missing/`None`) | the object could not be inspected safely; **nothing sent**; explicitly **not** evidence of stripping |
| `INLINE_RESULT_NO_SEND_MESSAGE` | `reason=="no_send_message"` — a real `BotInlineResult` whose `send_message` is absent/`None` | a genuine result with no payload; **nothing sent** |
| `INLINE_RESULT_REJECTED` | the query itself raised/timed out (`ok` False, no shape reason) | the helper could not answer; **nothing sent** |
| `INLINE_RESULT_ENTITY_MISSING` | payload inspected (`ok` True) but no `MessageEntityCustomEmoji` inside it | a real Telegram-side result restriction; **nothing sent** |
| `INLINE_SEND_FAILED` | payload had the entity; `click`/`sendInlineBotResult` failed | the send itself failed |
| `READBACK_FAILED` | sent, but the exact message could not be read back | nothing about the stored entity is proven |
| `STORED_ENTITY_STRIPPED` / `_MISMATCH` / `STORED_ATTRIBUTION_MISSING` | the stored message's own entities/`via_bot_id` | exactly what the stored message shows |
| `STORED_ENTITY_VERIFIED_RENDER_UNVERIFIED` | entity + id + span + `via_bot_id` all match | the entity is stored and attributed; **display is still the client's decision** |

---

## 7. What remains unverified

1. **No live Telegram/Render verification in this task.** The fix is proven
   against the installed Telethon classes and the full test suite, not against
   a live `getInlineBotResults` response.
2. **What the real Telegram payload contains** — whether Telegram *keeps* the
   custom-emoji entity inside the returned `BotInlineResult` — is still
   unknown. It could not be observed before, because the inspection never read
   the payload. The next live run answers exactly that.
3. Consequently the **send and the exact-message read-back have still never
   been exercised live**.
4. `BotInlineMessageText` vs. other `BotInlineMessage` variants: the helper
   answers with an article/text result, so the payload is a
   `BotInlineMessageText`; a media variant would be inspected the same way
   (`_scan_custom_emoji` reads `entities`), but no live sample exists.
5. Render-side log completeness beyond the quoted excerpts.
