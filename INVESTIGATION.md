# INVESTIGATION

## Investigation Metadata

| Item | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` (`origin`) |
| Branch | `main` (worktree branch `freebuff/3ac6db55494250a4a63f4039`, which is at the same commit as `origin/main`) |
| Baseline named by the task | `dc974939a312d7abd80d5f80cde83098c16a5ab3` |
| **Actual HEAD at investigation time** | **`011b9df3aa94880b962214dcca2cd143d27ee355`** — `fix(save): edit reply mode's existing panel instead of sending a status message` (one commit **after** the named baseline; `011b9df` is a descendant of `dc97493`) |
| `origin/main` at investigation time | `011b9df3aa94880b962214dcca2cd143d27ee355` — verified with `git fetch origin` + `git rev-parse FETCH_HEAD`; this clone keeps no local `origin/main` ref, so `FETCH_HEAD` is the remote tip read |
| Working tree at start | clean (`git status --short` produced no output) |
| Investigation date | 2026-10-22 (UTC) |
| Question | Can the optional Helper Bot edit the exact existing LifeOS inline panel **asynchronously**, after the original callback handler has returned, and drive that same message as the Save loading/progress/final surface? |
| Scope | `backend/helper/*`, the Save Reply Mode handler, the Save-by-Link contrast path, the cancellation/timer machinery around a panel, the pinned Telethon release, and the tests that touch these paths |
| Explicitly out of scope | private `tg://openmessage` link resolution, Save pipeline semantics, database/Supabase, Render, any implementation change |
| **Status** | **Investigation only.** No production code, test, migration, config or other document was modified. Nothing was sent to Telegram, no session string or credential was read, no SQL was executed, no deployment was touched. The only two deliverables are this file and its documentation commit. |

### Method and its limits

* Every repository claim below was re-read from the working tree at `011b9df` (line ranges are against that commit); none is carried over from an earlier round.
* Every library claim was checked against the **pinned** release `telethon==1.43.0` (`backend/requirements.txt:1`), installed into an isolated throwaway virtualenv under `/tmp` (outside the repository). The upstream interpreter used for the repository's runtime is not installed in this clone; the pinned wheel is what deployment resolves.
* The off-line probes construct real Telethon objects (`UpdateInlineBotCallbackQuery`, `InputBotInlineMessageID64`, a `TelegramClient` whose `__call__` is stubbed) and record the **exact request object** `edit_message` builds. They never connect, never authenticate, and never transmit.
* Consequence of that limit: the probes can prove *what the installed client does with a given identifier*. They cannot prove *what Telegram returns for these panels*. That distinction drives the verdict.

---

## 1. Executive Summary

### Verdict: **UNRESOLVED**

The operation is **supported by the installed client library**, but it is **not currently reachable by this codebase**, and whether Telegram even supplies the identifier it needs for *these* panels is **unproven without one live click**.

Three separate facts must all hold; only two are established:

| # | Required fact | Status |
|---|---|---|
| 1 | Telethon 1.43.0 can edit a message sent via an inline query result by its inline-message identifier, with no CallbackQuery involved | **CONFIRMED** |
| 2 | The Helper Bot — not the owner's account — is the principal that may use that method on this message | **CONFIRMED** (the helper is the message's `via_bot`) |
| 3 | Telegram actually delivers an inline-message identifier to the Helper Bot for the LifeOS panels, so it has one to keep | **UNKNOWN** — depends on the click arriving as `UpdateInlineBotCallbackQuery`; the repository captures nothing, so there is no runtime evidence either way |

What is certain in the negative direction:

* The code that exists today **cannot** do it. There is no stored inline identifier anywhere: `trigger()` reads `Message.inline_message_id`, an attribute the pinned Telethon does not have, so the value is always `""` and every downstream inline-id branch is dead. `_panel_coords()` and the pending-input state carry only two integers (`chat_id`, `message_id`).
* The identifier **cannot be reconstructed** from `(chat_id, message_id)`; Telegram exposes no lookup for it. It must be captured from an update at send time or click time and kept in memory.
* The existing idiom in several handlers — `helper.edit_message(chat_id, msg_id, …)` — is `messages.editMessage` issued by a **bot** against the **owner's** message. That is not the inline path and is not expected to be permitted (`LIKELY`, see §5).

So: the Helper Bot is the *only* account that can use `messages.editInlineBotMessage` on this message, and the self-bot is the *only* account that can use `messages.editMessage` on it. Which one is usable today is decided by fact 3, and fact 3 is unverified.

**Practical consequence:** the behaviour that already ships — Reply Mode driving progress through the **self client** on the panel it armed — is the supported path *today*. A Helper-Bot editor is a possible refinement, not a requirement, and must not be assumed to work before the live check in §9.

---

## 2. Current Save Reply Mode Behavior

### 2.1 How the panel comes into existence

`Menu` → `send_inline_panel(self_client, chat_id, "save")` (`backend/helper/inline_sender.py:24-70`) → `PanelLifecycleManager.create_panel` (`backend/helper/lifecycle.py:119-174`) → `inline_engine.trigger(self._self_client, chat_id, query)` (`backend/helper/inline_engine.py:251-284`), which is two RPC halves:

1. `query_results()` — `self_client.inline_query(helper_username, query, entity=chat_id)` (`inline_engine.py:209-231`).
2. `click_result()` — `result.click(chat_id)` (`inline_engine.py:233-248`).

The click is issued by the **self client**, so Telegram creates an ordinary message in the chat authored by the owner, stamped `via_bot_id = helper bot`. If the helper is unavailable, `Menu` falls back to `event.edit(text)` on the owner's own `Menu` message (`backend/bot/handlers/misc.py:909-914`), which is also authored by the owner.

Either way, **the panel message belongs to the self account.**

### 2.2 Callback path (helper side)

`register_callback_handlers(helper, owner_id)` is wired in `backend/runtime/supervisor.py:415-433`. The router (`backend/helper/panels.py:362-441`):

1. `resolve_callback_message(event)` → `(chat_id, message_id, inline_message_id)` (`panels.py:89-121`).
2. owner gate, non-empty data gate.
3. `_resolve_session(...)` → live `Session` or drop (`panels.py:322-360`).
4. dispatch on the `panel:` / `action:` / `input:` prefix; `event.answer()` in `finally` (`panels.py:436-441`).

Reply Mode is an action: `_handle_action` (`panels.py:549-575`) calls `_save_reply_action` (`backend/bot/handlers/save.py:92-120`), which:

* requires a `chat_id`;
* resolves the panel's own coordinates via `_panel_coords(event, chat_id)` (`save.py:133-156`);
* arms the pending input with `set_pending(owner_id, "save_reply", _save_reply_wait_handler, chat_id, wait_text, inline_chat_id=panel_chat, inline_msg_id=panel_msg_id, extra="", timeout=None)` (`save.py:108-116`);
* returns `("Deep Save", wait_text, [])`.

`_handle_action` then renders that result onto the **same** panel through `_render_and_edit` → `_safe_edit` → `event.edit(...)` (`panels.py:310-320`, `48-68`). `event.edit` is executed by the helper client that received the click.

### 2.3 Pending state

`set_pending` stores per-owner state (`backend/helper/input_state.py:27-56`):

| Key | Type / value |
|---|---|
| `panel_id` | `"save_reply"` |
| `handler` | the coroutine function (a closure when metadata was armed) |
| `chat_id` | the chat the owner is in |
| **`inline_chat_id`** | `int` — the panel's chat id |
| **`inline_msg_id`** | `int` — the panel's message id |
| `extra`, `timeout` | `""`, `None` (unbounded handler execution for Deep Save) |
| `created_at` | monotonic; `_INPUT_TIMEOUT_S = 120` expires the entry (`input_state.py:24, 58-69`) |

No inline-message identifier, no access hash, no helper-side handle is stored.

### 2.4 Reply path (after the callback is gone)

`register_input_listener` (`backend/helper/inline_sender.py:75-120`) matches the owner's outgoing message in the pending chat, **pops** the entry, and calls:

```
handler(text, event.chat_id, event.message.id, inline_chat_id, inline_msg_id, **extra)
```

(`inline_sender.py:116-119`). `_save_reply_wait_handler` (`save.py:398-453`) then:

1. `clear_pending(owner_id)`;
2. `editor = _panel_editor(client, inline_chat_id, inline_msg_id)` (`save.py:323-335`) — `None` when either coordinate is falsy, in which case the save runs with **no** UI and no replacement message is created (`save.py:414-419`);
3. writes `**Deep Save — Reply Mode**\n\nResolving the target message…` through that editor before any target lookup (`save.py:421`);
4. binds `SaveProgressReporter(editor, title=REPLY_PANEL_TITLE)` (`save.py:422`);
5. resolves the owner's reply → the replied-to message → `save_service.execute_save(self_client, …)` with `progress=reporter.handle` (`save.py:426-440`), so the **self client remains the sole Save executor**;
6. `reporter.close(result)` renders the terminal text (`save.py:451`), then deletes the owner's reply message (`save.py:453-456`).

The editor itself is `_status_editor` (`save.py:282-321`): `self_client.edit_message(chat_id, msg_id, rendered)` — the text passed positionally as the third parameter (`text`), the deduped/"not modified"-tolerant wrapper. It deliberately passes **no `buttons`**, so the panel's inline keyboard is preserved by omission.

Save by Link is the contrast case: it has no panel to keep, so `_start_status` creates ONE message with the self client and binds the reporter to it (`save.py:337-380`).

### 2.5 Background machinery that also touches the panel

* The panel timer is armed once, at creation (`lifecycle.py:163-168`), from `auto_close_enabled=True`, `auto_close_delay=120` (`backend/services/settings_service.py:69-70`).
* While armed it edits the same message every 30 s with a countdown (`backend/helper/panel_timer.py:21, 121-149, 151-175`).
* At expiry it calls `timeout_panel` → `_cleanup_locked` — "Panel closed" edit, session/render/pending cleanup, then a delayed delete 2.5 s later (`lifecycle.py:289-340`, `40-47`).
* `_sync_timer` only refreshes text/buttons (`panels.py:290-295`); it never restarts or disarms the timer. Reply Mode therefore inherits the timer started when `Menu` created the panel.

---

## 3. Inline Panel Ownership and Identifiers

### 3.1 Who sent the message produced by `result.click(chat_id)`

**The owner's own account.** `InlineResult.click` is a wrapper over `functions.messages.SendInlineBotResultRequest(peer=entity, query_id=…, id=…)` executed by the client that ran the inline query (`.venv: telethon/tl/custom/inlineresult.py:105-166`, request at `:153`). Telegram stores the result as a normal chat message whose author is the requesting account and whose `via_bot_id` is the helper bot. The repository documents the same fact at `backend/bot/handlers/save.py:282-296` and `323-335`.

That is why two *different* API methods apply, to two *different* principals:

| Principal | Method | Scope |
|---|---|---|
| Owner / self-bot | `messages.editMessage(peer, msg_id, …)` | the message it authored |
| Helper bot (`via_bot`) | `messages.editInlineBotMessage(id=InputBotInlineMessageID, …)` | the inline message it produced |

### 3.2 Identifiers at each stage

**(a) During the original CallbackQuery** — the event arrives on the **helper** client.

| Identifier | Availability |
|---|---|
| `event.chat_id`, `event.message_id` | present (derived from the update's peer + message id) |
| `event.sender_id`, `event.data`, `event.query.query_id`, `event.chat_instance` | present |
| `event.query.msg_id` **as `InputBotInlineMessageID` / `InputBotInlineMessageID64`** | present **only** when the click is delivered as `UpdateInlineBotCallbackQuery`. This object *is* the editable inline handle (`dc_id`, `owner_id`, `id`, `access_hash`). |
| `event.inline_message_id` | **does not exist in Telethon 1.43.0** — see §4.4 |
| `event.via_inline` | `True` exactly for the inline update flavour |

Verified off-line: for an inline-flavoured click `event.via_inline` is `True` and `event.query.msg_id` is an `InputBotInlineMessageID64`; for the normal flavour `via_inline` is `False` and `query.msg_id` is a plain `int`. For the 64-bit form Telethon derives the peer from the **high 32 bits of `msg_id.id`**, which that form does not carry — so `event.chat_id` came back as `0` in the probe. The router survives that only because `_resolve_session` falls back to `find_by_msg_id` (`panels.py:322-340`).

**(b) In the pending Reply Mode state** (`input_state.py:27-56`, written at `save.py:108-116`): two integers only — `inline_chat_id`, `inline_msg_id`. `Session.inline_message_id` exists (`backend/helper/session_manager.py:24-35, 42-66, 72-78`) but is **always `""` in production**, because it is filled from `trigger()`'s return value, which reads an attribute the pinned Telethon does not define (`inline_engine.py:265`). The `_inline_index` therefore never holds an entry, and `panels.py:343-352` is unreachable.

**(c) In the later reply-input handler** (`inline_sender.py:116-119` → `save.py:398`): the same two integers. After the callback has returned, **no inline-message identifier exists anywhere in the process**.

### 3.3 What follows

The identifier the Helper Bot would need is *created by Telegram*, is available (if at all) only in an update, and is **not derivable** from `(chat_id, message_id)` or from the chat. Capturing it means storing the TL object (or its four scalars) in the pending entry and carrying it to the input handler — nothing more exotic than what `extra` already does.

---

## 4. Telethon API Evidence

Pinned release: `telethon==1.43.0` (`backend/requirements.txt:1`), installed and inspected directly (not from memory). All line references in this section are inside the installed package.

### 4.1 The method

`TelegramClient.edit_message(entity, message=None, text=None, *, …, buttons=None, …)` — `client/messages.py:1085`.

Its own docstring, `client/messages.py:1108-1116`:

> `entity` … You may also pass a `InputBotInlineMessageID` or `InputBotInlineMessageID64`, **which is the only way to edit messages that were sent after the user selects an inline query result**.

Return: a `Message` for the normal path; **a boolean** for the inline path (`client/messages.py:1184-1187`; confirmed by probe).

### 4.2 The request it builds

`client/messages.py:1213, 1229-1245`:

```python
if isinstance(entity, (types.InputBotInlineMessageID, types.InputBotInlineMessageID64)):
    text = text or message
    message = entity
...
request = functions.messages.EditInlineBotMessageRequest(
    id=entity,
    message=text,
    no_webpage=not link_preview,
    entities=formatting_entities,
    media=media,
    reply_markup=self.build_reply_markup(buttons),
)
exported = self.session.dc_id != entity.dc_id
if exported:
    sender = await self._borrow_exported_sender(entity.dc_id)
    return await self._call(sender, request)
```

* Class: `telethon/tl/functions/messages.py:1703`, `CONSTRUCTOR_ID = 0x83557dba`.
* Verified constructor: `(id, no_webpage=None, invert_media=None, message=None, media=None, reply_markup=None, entities=None)`.
* **Naming trap:** this release suffixes requests with `Request`. `functions.messages.EditInlineBotMessage` **does not exist** (`hasattr` → `False`); only `EditInlineBotMessageRequest`. Any implementation must use the real name or, preferably, `edit_message`.
* Cross-DC: Telethon borrows an exported sender for `entity.dc_id`; skipping that is the documented cause of `MESSAGE_ID_INVALID` with no effect (`client/messages.py:1237-1245`).

### 4.3 Editing does not depend on the CallbackQuery

`CallbackQuery.Event.edit` (`events/callbackquery.py:299-320`) simply forwards to `client.edit_message(self.query.msg_id, …)` when `query.msg_id` is an inline id; otherwise `(input_chat, query.msg_id)`. The event object contributes nothing else — no query id, no token, no lifetime. Once the `InputBotInlineMessageID` is in hand, the edit is an ordinary request.

`event.answer()` (the 15-second "loading" acknowledgement) is a different RPC entirely and does not gate edits.

### 4.4 `event.inline_message_id` is not a Telethon attribute

A package-wide search for `inline_message_id` finds exactly one definition: `utils.resolve_inline_message_id()` (`utils.py:1321`), which parses the **Bot-API base64 string** into `(message_id, peer, dc_id, access_hash)` — not the TL object.

The custom event exposes no such property (probe: `hasattr(event, "inline_message_id") == False` for both callback flavours). Therefore every `getattr(event, "inline_message_id", None)` in the repository is permanently `None`:

* `backend/helper/panels.py:105`
* `backend/helper/client.py:151` (the receipt log line)
* `backend/helper/inline_engine.py:265`
* `backend/bot/handlers/save.py:137-155` (the docstring premise of `_panel_coords`)

### 4.5 Keyboard behaviour (verified request shapes)

`client/buttons.py:9-96` builds `ReplyInlineMarkup` only when at least one button is an inline type. Probe results for `EditInlineBotMessageRequest`:

| Call | `reply_markup` in the request | Effect on the keyboard |
|---|---|---|
| `buttons` omitted / `None` | `None` (flag unset) | **kept** — MTProto edits leave unset fields untouched |
| `buttons=[[KeyboardButtonCallback(...)]]` (what `panel_render.render_edit` returns) | `ReplyInlineMarkup` | replaced |
| `buttons=[]` | **`ReplyKeyboardMarkup(rows=[])`** | wrong markup type for an inline edit; likely refused (`UNKNOWN`, not live-tested) |
| `buttons=ReplyInlineMarkup(rows=[])` | `ReplyInlineMarkup(rows=[])` | **removed** |

The middle row matters because `panel_render` / `panels._safe_edit` pass `buttons=[]` for text-only states (`backend/helper/panels.py:63`). An implementation using the inline path must omit `buttons` to keep the keyboard, and pass an empty `ReplyInlineMarkup` to clear it — never `[]`.

### 4.6 A second, click-independent source for the identifier

`types.UpdateBotInlineSend(user_id, query, id, geo=None, msg_id=None)` — the "a user picked my inline result" update — carries an **optional** `InputBotInlineMessageID` as `msg_id`. The Helper Bot already subscribes to `events.Raw` (`backend/helper/client.py:157-166`), so this update is delivered. Whether Telegram populates the optional field for these chats is `UNKNOWN` and belongs to the live check.

---

## 5. Findings by Evidence Level

### 5.1 CONFIRMED

| # | Finding | Evidence |
|---|---|---|
| C1 | The panel message produced by `result.click(chat_id)` is authored by the owner's account; the helper is its `via_bot` | `inline_engine.py:233-248`, `251-284`; `telethon/tl/custom/inlineresult.py:105-166`; repo statement `save.py:282-296` |
| C2 | A message sent after an inline-result selection can be edited only through `InputBotInlineMessageID`(64), i.e. `messages.editInlineBotMessage` | `client/messages.py:1108-1116, 1213, 1229-1245`; `tl/functions/messages.py:1703` |
| C3 | That edit needs no CallbackQuery object, only the identifier; it edits in place and returns `bool` | `events/callbackquery.py:299-320`; `client/messages.py:1184-1187`; probe |
| C4 | Telethon 1.43.0 has no `event.inline_message_id`; every repo read of it is permanently empty | package-wide search; probe (`hasattr` false) |
| C5 | The repository stores no inline identifier: pending state has only two ints; `Session.inline_message_id` is always `""` because `trigger()` reads a non-existent `Message` attribute | `input_state.py:27-56`; `save.py:108-116`; `inline_engine.py:265`; `session_manager.py:24-35, 42-66, 72-78` |
| C6 | Reply Mode's panel editing today is done by the **self client** with `messages.editMessage`, passing no buttons (keyboard preserved by omission) | `save.py:282-321, 323-335, 398-451` |
| C7 | Save execution is confined to the self client: `execute_save(self_client, …)`, and the reporter is a pure renderer | `save.py:426-440`; `backend/helper/save_progress.py:72-190` |
| C8 | `SaveProgressReporter` accepts any async `edit` callable and swallows edit failures; milestones are capped (2–5 per phase), so a swap of editor needs no Save change | `save_progress.py:72-190`; `save_service.py:531-542` |
| C9 | Callback routing is independent of any later edit: owner gate, session resolution and `event.answer()` in `finally` | `panels.py:362-441` |
| C10 | `buttons=[]` produces a `ReplyKeyboardMarkup`, not an inline markup; only an empty `ReplyInlineMarkup` clears an inline keyboard | probe against `client/buttons.py:9-96` |
| C11 | The panel timer (default 120 s, edits every 30 s, then "Panel closed" + delete after 2.5 s) runs for the panel's whole life and is never restarted or disarmed by Reply Mode | `settings_service.py:69-70`; `panel_timer.py:21, 121-175`; `lifecycle.py:163-168, 289-340` |
| C12 | Pending state expires after `_INPUT_TIMEOUT_S = 120` s, independently of the unbounded handler timeout | `input_state.py:24, 58-69` |
| C13 | Mocked tests do not exercise the real event shape: they fabricate `inline_message_id` on a `SimpleNamespace`, an attribute real events lack | `tests/test_helper_callback_receipt.py:62-67`; `tests/test_save_live_status_and_private_link.py:371-392` |

### 5.2 LIKELY

| # | Finding | Basis |
|---|---|---|
| L1 | `helper.edit_message(chat_id, msg_id, …)` — used at `save.py:271` and mirrored in `bio.py`, `discover.py`, `retrieve.py`, `private_message.py`, `anti_edit_delete.py` — is `messages.editMessage` from a **bot** against the owner's message and will be refused (`MESSAGE_AUTHOR_REQUIRED` / `MESSAGE_ID_INVALID`) | method/principal mismatch per C1/C2; the repository's own history records the live failure (commit `c127d82`: progress "was edited through the helper bot, which cannot edit the owner's own panel message — so it never appeared"); not re-tested live in this round |
| L2 | For a "via bot" message, Telegram delivers `UpdateInlineBotCallbackQuery` (with `event.via_inline == True`), which would make the identifier available at click time | Telethon's `via_inline` documentation (`events/callbackquery.py:256-268`) describes exactly this case; `UNKNOWN` in practice until observed |
| L3 | The auto-close timer will destroy the progress surface of any save that runs — or starts — more than ~120 s after `Menu`, and its countdown edits will interleave with progress text | C11 plus `set_content` only refreshing text (`panel_timer.py:74-90`); timing not observed live |
| L4 | An edit issued from inside the transfer callback inherits the helper's `flood_sleep_threshold=60` (`backend/helper/client.py:61-74`), so a `FLOOD_WAIT` would sleep inside the download/upload loop | `save_service.py:531-542` awaits the reporter, which awaits the edit |

### 5.3 UNKNOWN

| # | Finding | Why unresolved |
|---|---|---|
| U1 | Whether LifeOS panel clicks arrive as `UpdateInlineBotCallbackQuery` carrying `InputBotInlineMessageID(64)`, i.e. whether the Helper Bot ever possesses the identifier | Requires one live click; source cannot tell. This single fact decides the verdict |
| U2 | Whether Telegram accepts `EditInlineBotMessage` from the helper minutes after the click, and for a message in a chat the bot is not a member of | Standard behaviour per the method's purpose, but not verifiable without a live call |
| U3 | Whether `UpdateBotInlineSend.msg_id` is populated for these chats (an alternative capture point) | Flag-optional field; live observation needed |
| U4 | Whether a `ReplyKeyboardMarkup(rows=[])` payload is refused by `editInlineBotMessage` | Server-side validation; not documented for this method |
| U5 | Whether the `chat_id == 0` derivation for the 64-bit identifier can mis-resolve a session when two chats share a message id | The router falls back to `find_by_msg_id` (`panels.py:322-340`); real-world collision behaviour unobserved |

---

## 6. Relevant Files and Exact Code Locations

### Repository (`011b9df`)

| Path | Lines | What it establishes |
|---|---|---|
| `backend/helper/inline_engine.py` | `209-231` | `query_results` — the self client runs `inline_query` |
| | `233-248` | `click_result` — the self client runs `result.click` |
| | `251-284` | `trigger` — returns `(ok, chat_id, msg_id, inline_message_id)`; `inline_message_id` read from a non-existent `Message` attribute (`:265`) |
| `backend/helper/panels.py` | `48-68` | `_safe_edit` — `event.edit(text, buttons=…)`, `buttons=[]` for empty states |
| | `89-121` | `resolve_callback_message` — `getattr(event, "inline_message_id", None)` (`:105`) |
| | `290-295` | `_sync_timer` — refreshes text only |
| | `310-320` | `_render_and_edit` — finalizes + edits the panel |
| | `322-360` | `_resolve_session` — inline-id lookup branch (dead in production) |
| | `362-441` | `_callback_router` — owner gate, dispatch, `answer()` in `finally` |
| | `549-575` | `_handle_action` — calls the action, then renders on the same panel |
| | `577-634` | `_handle_input` — `set_pending(...)` with the two ints (`:602`) |
| `backend/helper/panel_render.py` | `9-30, 45-75` | `_style`, `_callback` (button styles), `render_edit` returning `(text, rows-of-buttons)` |
| `backend/helper/client.py` | `45-97` | `build_helper` — `flood_sleep_threshold=60` (`:61-74`) |
| | `116-166` | `register_helper_hooks` — receipt log with `getattr(event, "inline_message_id", …)` (`:151`) and the `events.Raw` hook (`:157-166`) |
| `backend/helper/lifecycle.py` | `119-174` | `create_panel` — session create, timer start; `inline_message_id=inline_msg_id or inline_msg_id` (`:160`) |
| | `289-340` | `_cleanup_locked` — close edit + 2.5 s delayed delete |
| `backend/helper/session_manager.py` | `24-35, 42-66, 72-78` | `Session.inline_message_id`; `_inline_index`; `get_by_inline_id` |
| `backend/helper/input_state.py` | `24, 27-56, 58-69` | pending entry fields (`inline_chat_id`, `inline_msg_id` ints) and 120 s expiry |
| `backend/helper/inline_sender.py` | `24-70` | `send_inline_panel` — the `Menu` → panel entry point |
| | `75-120` | `register_input_listener` — pops pending, calls handler with two ints (`:116-119`) |
| `backend/bot/handlers/save.py` | `92-120` | `_save_reply_action` — arms pending with panel coordinates |
| | `133-156` | `_panel_coords` — `(chat_id, message_id)` only |
| | `244-280` | `_save_metadata_input_handler` — the unsupported `helper.edit_message(chat, msg, …)` idiom (`:271`) |
| | `282-321` | `_status_editor` — self client, positional text, no buttons, dedupe + "not modified" |
| | `323-335` | `_panel_editor` — returns `None` without usable coordinates |
| | `337-380` | `_start_status` — Save-by-Link's one message |
| | `398-453` | `_save_reply_wait_handler` — the whole Reply Mode trace |
| `backend/helper/save_progress.py` | `72-190` | `SaveProgressReporter` — injectable editor, capped milestones, terminal close |
| `backend/services/save_service.py` | `531-542` | `_emit_progress` awaits the reporter inside the transfer |
| `backend/helper/panel_timer.py` | `21, 55-72, 121-175` | countdown edits every 30 s; expiry → `timeout_panel` |
| `backend/services/settings_service.py` | `69-70` | `auto_close_enabled=True`, `auto_close_delay=120` |
| `backend/runtime/supervisor.py` | `415-433` | helper client registers inline + callback handlers |
| `backend/requirements.txt` | `1` | `telethon==1.43.0` |

### Tests (evidence of coverage, not of Telegram behaviour)

| Path | Lines | Relevance |
|---|---|---|
| `tests/test_save_live_status_and_private_link.py` | `105-186` | Reply Mode edits the existing panel, before the target lookup, never creates a message |
| | `201-229` | asserts the panel is edited by the self client **only** (a `ForbiddenHelper` that raises) |
| | `253-299` | Save-by-Link never uses the helper |
| | `332-369` | coordinates travel from arming to the terminal result |
| | `371-392` | `_InlineEvent` with a fabricated `inline_message_id` — asserts a path real events cannot take (C13) |
| | `394-402` | pins the production edit/send argument shapes against `TelegramClient` |
| `tests/test_helper_callback_receipt.py` | `58-95` | the receipt hook logging `inline_msg_id=''` from a `SimpleNamespace` |
| `tests/test_save_v2_wiring.py` | `514-519` | `_Helper.edit_message(chat_id, msg_id, text, …)` — a mock whose signature does not prove Telegram accepts that principal/method pair |

### Installed library (`telethon 1.43.0`)

| Location | Lines | Relevance |
|---|---|---|
| `telethon/client/messages.py` | `1085`, `1108-1116`, `1184-1187`, `1213`, `1229-1245` | `edit_message`, inline-id rule, boolean return, request build, DC export |
| `telethon/tl/functions/messages.py` | `1703-1755` | `EditInlineBotMessageRequest`, `0x83557dba` |
| `telethon/events/callbackquery.py` | `90-97`, `256-268`, `299-320` | update → event (peer/msg_id), `via_inline`, `Event.edit` via inline id |
| `telethon/tl/custom/inlineresult.py` | `105-166` | `click` → `SendInlineBotResultRequest` |
| `telethon/client/buttons.py` | `9-96` | `build_reply_markup` — inline vs reply keyboard decision |
| `telethon/utils.py` | `1321-1345` | `resolve_inline_message_id` — parses the Bot-API base64 form only |

---

## 7. Feasibility and Failure Modes

### 7.1 What the evidence does establish

* The library-side operation exists, is documented for exactly this case, is independent of the callback lifecycle, and handles cross-DC correctly.
* The Helper Bot is the correct principal for it, and the self-bot is the correct principal for `messages.editMessage`.
* A same-message editing flow (loading → progress → terminal) is feasible in principle: in-place edit, keyboard kept by omitting `buttons`, keyboard removed with an empty `ReplyInlineMarkup`.
* An implementation would stay UI-only: `SaveProgressReporter` takes any editor, and Save already executes on the self client.

### 7.2 What it does not establish

* That the Helper Bot receives an inline identifier for these panels (U1). Without it, none of the above is reachable — and the identifier cannot be reconstructed later.
* That Telegram accepts the edit minutes later, from a bot that may not be in the chat (U2).

### 7.3 Failure modes

1. **Missing identifier (present today).** Pending state and the input handler hold only ints; `event.inline_message_id` does not exist; `Session.inline_message_id` is always `""`. Any implementation that assumes an identifier exists fails silently or with an attribute error. The correct behaviour remains "no UI" (`save.py:414-419`).
2. **Wrong principal/method (present today).** `helper.edit_message(chat_id, msg_id, …)` is `messages.editMessage` from a bot on the owner's message — `MESSAGE_AUTHOR_REQUIRED` / `MESSAGE_ID_INVALID` expected (`LIKELY`, L1). It is covered only by mocks (`tests/test_save_v2_wiring.py:514-519`).
3. **Deleted panel before the edit.** The auto-close timer closes and deletes the panel after 120 s by default, and never re-arms for Reply Mode (C11). Progress after that point is lost, and each later edit raises a message-id error that the reporter swallows (`save_progress.py:135-149`).
4. **Conflicting writer.** The 30 s countdown edits the same message and restores stale `title/body` from `set_content` (`panel_timer.py:74-90, 151-175`) — text flicker between countdown and progress, and at expiry a "Panel closed" overwrite.
5. **Pending expiry.** A reply later than 120 s after arming matches no pending entry (`input_state.py:24, 58-69`); the save never starts.
6. **Restart.** The identifier is in-memory only; a restart loses it (and the in-flight save's surface).
7. **DC handling.** Skipping `entity.dc_id` produces `MESSAGE_ID_INVALID` with no effect (`client/messages.py:1237-1245`).
8. **Wrong keyboard shape.** Passing `buttons=[]` sends `ReplyKeyboardMarkup` (C10); accepted-or-refused by the server is unverified (U4).
9. **Latency inside the transfer.** Awaited progress edits inherit the helper's 60 s flood sleep (L4).
10. **Name drift.** `EditInlineBotMessage` (no suffix) does not exist in 1.43.0 (C2 note); only `EditInlineBotMessageRequest` / `edit_message`.

---

## 8. Minimal Safe Implementation Plan

**Only if §9 clears U1.** The plan is deliberately additive and reversible; it does not touch `_safe_edit`, the callback router, the timer, or Save's execution path.

1. **Capture, don't invent.** In `_save_reply_action` (`save.py:92-120`), when `event.via_inline` is true and `event.query.msg_id` is an `InputBotInlineMessageID`/`…64`, keep that object (or its `dc_id`/`owner_id`/`id`/`access_hash`). Never derive it from `(chat_id, msg_id)`.
2. **Carry it through the pending entry.** Add one optional `inline_id` field to `set_pending` (`input_state.py:27-56`) and thread it to the handler exactly as `extra` is threaded (`inline_sender.py:104-120`). Keep `inline_chat_id` / `inline_msg_id` untouched as the fallback path.
3. **Add one editor, reuse the existing one.** A `_helper_panel_editor(helper, inline_id)` that calls `helper.edit_message(inline_id, text=rendered)` — **no `buttons` argument**, same dedupe and `"not modified"` tolerance as `_status_editor` (`save.py:282-321`). Keep `_panel_editor` as the fallback when no identifier was captured, so a missing id degrades to today's behaviour instead of a new message.
4. **Decide the waiting model explicitly.** Either keep the awaited edit (2–5 milestones per phase, already capped) or move it to `guarded_create_task` with drop-stale semantics — not both, and not without accounting for the helper's flood sleep.
5. **Do not create messages.** A missing identifier must remain "run without UI", never a replacement status message.
6. **Leave the timer alone in the same change.** If the timer conflict is fixed, it is a separate, independently reviewable change (`panel_timer.py` / `lifecycle.py`).
7. **Update the ownership claim.** `AGENTS.md` §6 and the docstrings at `save.py:282-296` state the self client is the *only* editor. That is true of `messages.editMessage`; the `via_bot` may also edit. If a helper editor ships, that wording must be corrected in the same commit.

Invariants preserved by this plan: callback routing unchanged (helper still receives and answers clicks); Save still executes only via `save_service.execute_save(self_client, …)`; no new messages; no change to code paths that already work.

---

## 9. Required Live Verification

Minimum manual sequence, performed by the owner with the real bot (never from this clone, and not part of any automated run):

1. **Identify the update flavour (decisive).** Open `Menu` → `Save` → `Deep Save` → `Reply Mode` and press the button. Temporarily log `event.via_inline` and `type(event.query.msg_id).__name__` (a runtime log line only; no committed code). Confirmation needed: `via_inline == True` and an `InputBotInlineMessageID`/`…64`, i.e. the helper really receives the handle. If instead it logs a plain `int`, the Helper-Bot path is **not available** and the investigation closes as UNSUPPORTED.
2. **Edit in place, immediately.** With the captured id, call `editInlineBotMessage` (`client.edit_message(id_obj, text="loading…")`) within seconds of the click. Must confirm: same message, same id, keyboard intact and still clickable, text replaced.
3. **Edit asynchronously.** Wait >15 s (callback answered/expired) and repeat. Must confirm the identifier is still valid — this is the "after the handler returned" property the task asks about.
4. **Confirm the timer conflict.** Repeat at >2 minutes after `Menu`. Expect `MESSAGE_ID_INVALID` (or a "Panel closed" overwrite) once the auto-close timer has fired — that failure must be observed rather than inferred before any implementation relies on long-running progress.
5. **Keyboard control.** Repeat with `buttons=ReplyInlineMarkup(rows=[])` (keyboard must disappear) and, once, with `buttons=[]` (records whether that shape is refused). Then confirm a plain text edit leaves the keyboard present.
6. **Confirm the negative control.** Attempt `helper.edit_message(chat_id, msg_id, text)` on the same message and record the exact exception, validating L1.

Only steps 1–3 are gating. Steps 4–6 shape the implementation and the follow-up fixes.

---

## 10. Remaining Work

**Next concrete step:** run §9 step 1 — one click, one temporary diagnostic log of `event.via_inline` and the type of `event.query.msg_id`. That single observation converts U1 from `UNKNOWN` to a definite answer and settles the verdict.

**Unresolved dependency:** Telegram's callback-update flavour for inline-result messages sent into a private chat/self-chat (U1), plus acceptance of the deferred edit (U2). Both require live traffic; neither can be settled from source, tests or mocks.

**Everything else is already decided:** the API exists and is correct for this principal; the repository stores nothing usable today; the self-client editor already ships and is the safe default; the auto-close timer and the pending-state expiry are the real obstacles to a long-running progress surface, independent of which account performs the edit.

**Documentation consistency notes (deliberately not edited here):**
* `AGENTS.md:180` points to `INVESTIGATION.md` for the dormant `startup_check` / `tg_retry` paths; this replacement no longer covers them, and the older audit lives on in Git history and in `docs/investigations/`.
* `README.md:265` still describes `INVESTIGATION.md` as a dead-code/duplication audit.
* The task's named baseline (`dc97493…`) is one commit behind the actual HEAD (`011b9df…`); every line reference here is against `011b9df`.
