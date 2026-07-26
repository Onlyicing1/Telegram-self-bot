"""
Helper Bot — Inline Mode + Callback Engine infrastructure.

The helper bot is a secondary Telegram client (bot token, not user session)
that handles ONLY:
  - Inline Mode (answering InlineQuery events with panel results)
  - Callback queries (button presses on inline messages)

The self-bot (Telethon StringSession) remains the brain — it processes
commands and business logic. The helper bot is purely a presentation layer.
"""
from backend.helper.client import build_helper, is_available, get_bot_username
from backend.helper.panels import (
    InlinePanelBuilder,
    register_panel,
    get_panel,
    register_action,
    get_action,
    register_input,
    get_input,
)
from backend.helper.inline_engine import (
    register_inline_builder,
    get_inline_builder,
    trigger,
    make_result,
    make_button_rows,
)
from backend.helper.inline_sender import (
    send_inline_panel,
    register_input_listener,
)
from backend.helper.target_context import (
    TargetContext,
    set_target,
    get_target,
    clear_target,
)
from backend.helper.pagination import build_pagination_row, paginate
from backend.helper.panel_timer import (
    init_panel,
    toggle as timer_toggle,
    destroy as timer_destroy,
    get_state as timer_get_state,
    get_countdown_text,
    get_toggle_button_text,
    set_content as timer_set_content,
    has_timer,
    active_count,
    TimerState,
)
from backend.helper.panel_render import render, render_edit, to_edit_buttons
from backend.helper import watchdog

__all__ = [
    "build_helper",
    "is_available",
    "get_bot_username",
    "InlinePanelBuilder",
    "register_panel",
    "get_panel",
    "register_action",
    "get_action",
    "register_input",
    "get_input",
    "register_inline_builder",
    "get_inline_builder",
    "trigger",
    "make_result",
    "make_button_rows",
    "send_inline_panel",
    "register_input_listener",
    "TargetContext",
    "set_target",
    "get_target",
    "clear_target",
    "build_pagination_row",
    "paginate",
    "init_panel",
    "timer_toggle",
    "timer_destroy",
    "timer_get_state",
    "get_countdown_text",
    "get_toggle_button_text",
    "timer_set_content",
    "has_timer",
    "active_count",
    "TimerState",
    "render",
    "render_edit",
    "to_edit_buttons",
    "watchdog",
]
