"""
Panel self-test — deterministic verification of the inline panel pipeline.

Outputs ONE structured report. No spam logging.
"""
from backend.helper import inline_engine
from backend.helper.panels import get_panel, get_action, _panels, _actions
from backend.helper.panel_timer import (
    _panels as _timer_panels,
    init_panel,
    destroy,
    has_timer,
    active_count,
)
from backend.helper.panel_settings import is_auto_close_enabled, set_auto_close_enabled


async def run_selftest(self_client, helper_client, owner_id: int) -> str:
    report = ["[PANEL SELFTEST]", ""]

    # 1. callback registered
    close_panel = get_panel("help")
    callback_ok = close_panel is not None
    report.append(f"Callback ......... {'OK' if callback_ok else 'FAIL'}")
    if not callback_ok:
        report.append("")
        report.append("FAILED")
        return "\n".join(report)

    # 2. trigger() returns success
    trigger_ok = False
    trigger_reason = ""
    try:
        success, msg_chat_id, msg_id = await inline_engine.trigger(
            self_client, owner_id, "help"
        )
        trigger_ok = success and msg_id > 0
        if not trigger_ok:
            trigger_reason = "trigger() returned success=False or msg_id=0"
    except Exception as exc:
        trigger_reason = f"trigger() raised: {exc}"
    report.append(f"Trigger .......... {'OK' if trigger_ok else 'FAIL'}")
    if not trigger_ok:
        report.append("")
        report.append("FAILED")
        report.append(f"Reason: {trigger_reason}")
        return "\n".join(report)

    # 3. timer created via init_panel (only if auto-close is ON)
    was_enabled = is_auto_close_enabled()
    set_auto_close_enabled(True)
    init_panel(self_client, msg_chat_id, msg_id)
    timer_ok = has_timer(msg_chat_id, msg_id)
    report.append(f"Timer Init ....... {'OK' if timer_ok else 'FAIL'}")

    # 4. disable auto-close → new panels should NOT have timers
    set_auto_close_enabled(False)
    await destroy(self_client, msg_chat_id, msg_id)
    init_panel(self_client, msg_chat_id, msg_id)
    no_timer_when_disabled = not has_timer(msg_chat_id, msg_id)
    report.append(f"Disabled No Timer. {'OK' if no_timer_when_disabled else 'FAIL'}")

    # 5. re-enable → new panels should have timers
    set_auto_close_enabled(True)
    await destroy(self_client, msg_chat_id, msg_id)
    init_panel(self_client, msg_chat_id, msg_id)
    timer_when_enabled = has_timer(msg_chat_id, msg_id)
    report.append(f"Enabled Timer ... {'OK' if timer_when_enabled else 'FAIL'}")

    # 6. only one timer exists
    one_timer = active_count() == 1
    report.append(f"Single Timer ..... {'OK' if one_timer else 'FAIL'}")

    # 7. destroy clears state
    await destroy(self_client, msg_chat_id, msg_id)
    destroyed = not has_timer(msg_chat_id, msg_id)
    report.append(f"Destroy .......... {'OK' if destroyed else 'FAIL'}")

    # 8. no timer leaks
    no_leak = active_count() == 0
    report.append(f"No Leaks ......... {'OK' if no_leak else 'FAIL'}")

    # 9. actions registered
    actions_ok = len(_actions) > 0
    report.append(f"Actions .......... {'OK' if actions_ok else 'FAIL'}")

    # Restore original preference
    set_auto_close_enabled(was_enabled)

    all_ok = all([
        callback_ok, trigger_ok, timer_ok, no_timer_when_disabled,
        timer_when_enabled, one_timer, destroyed, no_leak, actions_ok,
    ])

    if not all_ok:
        report.append("")
        report.append("FAILED")
    else:
        report.append("")
        report.append("ALL OK")

    return "\n".join(report)
