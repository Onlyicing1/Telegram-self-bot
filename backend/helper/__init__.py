"""
Helper Bot — Inline Mode + Callback Engine infrastructure.

Re-exports the public API of the helper layer so handlers can import
from a single place:

    from backend.helper import (
        InlinePanelBuilder,
        register_panel,
        send_inline_panel,
        render,
        ...
    )
"""
from backend.helper.panels import (
    InlinePanelBuilder,
    register_panel,
    register_action,
    register_input,
    register_callback_handlers,
    get_panel,
    get_action,
    close_panel,
)
from backend.helper.panel_render import (
    render,
    render_edit,
    to_edit_buttons,
)
from backend.helper.inline_sender import (
    send_inline_panel,
)
from backend.helper.inline_engine import (
    register_inline_builder,
    register_inline_handler,
    set_self_client,
    set_helper_username,
    set_helper_id,
    set_owner_id,
)
from backend.helper.lifecycle import (
    get_lifecycle,
    configure_lifecycle,
)
from backend.helper.target_context import (
    TargetContext,
    set_target,
    get_target,
)
from backend.helper.panel_settings import (
    is_auto_close_enabled,
    reload as reload_settings,
    set_auto_close_enabled,
    toggle_auto_close,
    load as load_settings,
    reload as reload_settings,
)
