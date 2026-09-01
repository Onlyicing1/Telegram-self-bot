# INVESTIGATION

## PROBLEM

Taskloom buttons are reportedly absent in the deployed Telegram self-bot while Render reports helper-bot authentication failure (`A wait of 1212 seconds is required`, caused by `ImportBotAuthorizationRequest`). Runtime telemetry then shows `helper_enabled=True`, `helper_connected=False`, `READY_BUT_DISCONNECTED`, and recovery activity.

## CURRENT RUNTIME OBSERVATIONS

- Helper startup calls `build_helper()` and can fail during `client.start(bot_token=...)`; the exception is wrapped as `RuntimeError("Helper bot login failed: ...")`.
- On failure, `RuntimeSupervisor._start_helper()` sets helper connectivity false and logs `Helper bot start failed`.
- The heartbeat invariant treats a disconnected helper as unhealthy when `helper_enabled=True`, producing `READY_BUT_DISCONNECTED` and triggering recovery.
- These facts establish helper failure and recovery pressure, but do not by themselves establish that button construction failed.

## TASKLOOM UI SOURCE PATH

The registered self-client handler path is:

`backend/bot/router.py::register_all()` → `backend/bot/handlers/ai.py::register()` (AI panel registration) → AI panel builder `_ai_main_panel_handler()` creates the Taskloom button with callback data `panel:taskloom` → `backend/bot/handlers/taskloom.py::register()` registers `panel:taskloom` and its inline builder.

The Taskloom panel path is:

`taskloom._taskloom_panel()` → `InlinePanelBuilder.add_row()` / `add_buttons()` → `Button.inline(...)` → `taskloom._taskloom_inline_builder()` → `render(...)` → `panel_render.render()` → `types.ReplyInlineMarkup(rows=...)`.

For a root/menu entry, `misc._menu_panel_handler()` / `_menu_inline_builder()` constructs the menu and the AI panel is reached through callback data `panel:ai`. The AI panel then exposes `🧵 Taskloom` with `panel:taskloom`.

## BUTTON CONSTRUCTION

Taskloom-specific buttons are constructed in `backend/bot/handlers/taskloom.py`:

- `_taskloom_panel()` builds task rows, pagination, and navigation.
- `_task_detail_panel()` builds pause/resume/complete/delete/refresh buttons.
- `InlinePanelBuilder.add_row()` and `add_buttons()` in `backend/helper/panels.py` convert each tuple into Telethon `Button.inline` objects.
- `backend/helper/panel_render.py::render()` converts the rows into `types.KeyboardButtonRow` and `types.ReplyInlineMarkup` inside a `types.InputBotInlineResult`.

The button definitions and callback data are present in source. No feature gate inside `taskloom.register()` disables these registrations; its only local failure path logs registration failure if an exception occurs.

## TELEGRAM CLIENT

Initial panel delivery is performed by the **self-bot client**, but through Telegram Inline Mode:

`misc.menu_cmd()` receives the outgoing `Menu` event and calls `send_inline_panel(client, event.chat_id, "menu")`; `send_inline_panel()` uses the self client as its `self_client` argument; `PanelLifecycleManager.create_panel()` calls `inline_engine.trigger(self_client, ...)`; `inline_engine.trigger()` calls `self_client.inline_query(_helper_username, query, entity=chat_id)` and then `results[0].click(chat_id)`.

Thus the message containing the buttons is inserted into the target chat by the self account's `inline_query(...).click(...)` operation. The helper bot supplies the inline-query answer, not the initiating Telegram client.

Subsequent panel edits are also performed by the self client when the callback event is an inline message resolved to the configured self client/session: `panels._safe_edit()` calls `event.edit(...)`, while lifecycle reuse calls `self._self_client.edit_message(...)`. Cleanup uses `self._self_client.edit_message()` and `delete_messages()`.

## HELPER BOT DEPENDENCY

- **Button construction:** No helper connection is required. `taskloom._taskloom_panel()`, `InlinePanelBuilder`, `Button.inline`, `render`, and `ReplyInlineMarkup` are local object construction and do not call the helper client.
- **Initial message send/edit:** The inline delivery path does require a connected helper bot username. `inline_engine.trigger()` returns false when `_helper_username` is empty and logs that inline mode cannot start; `send_inline_panel()` likewise returns false before calling the lifecycle. Therefore the normal inline Taskloom message cannot be created while the helper is unavailable. `misc.menu_cmd()` has a no-helper fallback to edit-in-place, but that fallback is selected only when `get_client()` is `None`, not merely when a helper object exists but its username is unavailable.
- **Callback registration:** The callback router is registered on the helper client in `RuntimeSupervisor._start_helper()` via `register_callback_handlers(helper, owner_id)`. Inline-query routing is also registered there via `register_inline_handler(helper, owner_id)`. If helper startup fails before these calls, neither helper-side handler is registered.
- **Callback execution:** Helper connectivity is required for the helper bot to receive inline queries and callback queries. The callback router itself uses the helper client event registration, while actual callback edits delegate through the callback event/self-client lifecycle path described above.

## EXACT FAILURE POINT

The concrete source-level failure for normal Glass UI creation is:

`backend/helper/inline_engine.py::trigger()` checks `if not _helper_username:` and returns `(False, chat_id, 0, "")`. `_helper_username` is populated only after successful `build_helper()` and `get_me()` in `RuntimeSupervisor._start_helper()`. The observed `ImportBotAuthorizationRequest` failure prevents that setup, so the subsequent `send_inline_panel()` / `PanelLifecycleManager.create_panel()` path receives failure and no inline panel message with buttons is created.

A separate source-level observation is that `misc.menu_cmd()` chooses plain-text fallback only when `get_client()` returns `None`; `helper_client` is not assigned on failed startup, but `backend.helper.client._client` is also left unset after `build_helper()` disconnects its local client. Whether that exact fallback executes in the deployed process depends on the current module state and event timing; it is not proven from logs alone.

## CAUSALITY

**Does `helper_connected=False` explain why Taskloom buttons are missing?**

**CONFIRMED for the normal inline-button path:** yes, when helper startup failure leaves `_helper_username` empty, `inline_engine.trigger()` refuses the inline query before panel results are requested or clicked. This prevents initial inline Taskloom buttons from being delivered even though local button construction is valid.

**Not confirmed as the only possible UI symptom:** the source contains a no-helper edit-in-place fallback in `misc.menu_cmd()`, and the logs supplied do not show the exact `Menu` event, `send_inline_panel` result, or fallback edit exception. Therefore the causal chain from helper failure to the user's precise observed screen is source-proven for inline mode but not fully proven for every possible fallback path.

## RUNTIME WIRING

`backend/bot/router.py::register_all()` includes `("taskloom", lambda: taskloom.register(...))`, after the AI handler. `taskloom.register()` installs the `taskloom` and `taskloom_task` panels/builders and four task actions in the global helper registries. This registration occurs during `RuntimeSupervisor._build_and_register()` before helper startup, so helper authentication failure occurs after the Taskloom definitions have been registered on the self-client startup path.

However, the inline query builder and callback router are attached to the helper only in `_start_helper()`. A failed helper startup prevents those helper-side registrations. No source evidence shows a Taskloom-specific registration exception in the supplied logs; the known helper exception occurs at helper login.

## RECENT task.py REPAIR IMPACT

The recent `backend/ai/tools/task.py` syntax repair is not on the Taskloom UI registration or panel-rendering path. `RuntimeSupervisor._build_and_register()` calls `register_all()` and then `_wire_ai_tools()`. A task-tool import/wiring failure can affect AI tool execution, but the Taskloom panel definitions are registered by `taskloom.register()` independently. No source evidence demonstrates that the repaired `task.py` syntax caused missing Taskloom buttons.

## CONFIRMED ROOT CAUSE

**CONFIRMED ROOT CAUSE:** The normal inline Taskloom button delivery path requires a helper username and a functioning helper inline-query handler. The observed helper authentication failure prevents helper initialization, leaves the inline engine without `_helper_username`, and causes `inline_engine.trigger()` / `send_inline_panel()` to fail before the generated markup can be clicked into the chat.

The local button objects themselves are not the failure: source construction is present and does not require a networked helper client.

## REMAINING UNKNOWN

- Whether the deployed user opened `Menu` while `_helper_username` was empty, and whether `misc.menu_cmd()` selected or successfully completed its edit-in-place fallback.
- Whether any Telegram API exception occurred during the fallback `event.edit()`; no such event log was supplied.
- Whether the deployed artifact exactly matches the inspected repository commit; no deployment revision identifier was provided.
- Whether callbacks were attempted after a panel was created; no callback trace was provided.

## RECOMMENDED NEXT IMPLEMENTATION SCOPE

If implementation is authorized, the smallest justified scope is to make the existing startup/fallback behavior observably and reliably handle the already-supported helper-unavailable state without changing button definitions, callback security, scheduler behavior, or Telegram boundaries. First add focused tests around `Menu` with helper unavailable and around `inline_engine.trigger()` with no helper username; then make only the minimal existing-path correction required by those tests. Do not add another UI engine or Telegram client.

## HARD CONSTRAINTS

- Investigation findings are source-derived; no production code, tests, configuration, dependencies, database, Supabase, schema, or SQL were changed by this investigation.
- The Self Bot remains the Telegram execution authority for initiating inline queries and editing/deleting panel messages.
- The helper bot remains the existing optional inline-query/callback provider; no second Telegram path was introduced.
- Existing owner validation in `misc.menu_cmd()`, helper inline routing, and callback routing remains authoritative.
- No scheduler, executor, retry engine, arbitrary Telegram RPC, arbitrary chat ID, or provider direct Telegram control was introduced.
- `IMPLEMENTATION_REPORT.md` and `tests/test_stage13.py` were not modified.
