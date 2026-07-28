"""
Helper Bot — Inline Mode + Callback Engine infrastructure.
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
    clear_all as clear_all_targets,
)
from backend.helper.pagination import build_pagination_row, paginate
from backend.helper.panel_timer import (
    init_panel,
    destroy as timer_destroy,
    stop_timer,
    stop_all as stop_all_timers,
    set_content as timer_set_content,
    has_timer,
    active_count,
)
from backend.helper.panel_settings import (
    is_auto_close_enabled,
    set_auto_close_enabled,
    toggle_auto_close,
    load as load_settings,
)
from backend.helper.panel_render import render, render_edit, to_edit_buttons
from backend.helper.callback_trace import configure as configure_callback_trace
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
    "clear_all_targets",
    "build_pagination_row",
    "paginate",
    "init_panel",
    "timer_destroy",
    "stop_timer",
    "timer_set_content",
    "has_timer",
    "active_count",
    "stop_all_timers",
    "is_auto_close_enabled",
    "set_auto_close_enabled",
    "toggle_auto_close",
    "load_settings",
    "render",
    "render_edit",
    "to_edit_buttons",
    "configure_callback_trace",
    "watchdog",
]
