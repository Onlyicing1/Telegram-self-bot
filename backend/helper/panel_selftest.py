"""
Panel self-test — deterministic verification of the inline panel pipeline.

Outputs ONE structured report. No spam logging.
"""
from backend.helper import inline_engine
from backend.helper.panels import get_panel, get_action, _panels, _actions
from backend.helper.panel_timer import (
    _panels as _timer_panels,
    init_panel,
    toggle,
    get_state,
    destroy,
    has_timer,
    active_count,
    TimerState,
)


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

    # 3. timer created via init_panel
    init_panel(self_client, msg_chat_id, msg_id)
    timer_ok = has_timer(msg_chat_id, msg_id)
    report.append(f"Timer Init ....... {'OK' if timer_ok else 'FAIL'}")

    # 4. toggle to paused
    toggle(self_client, msg_chat_id, msg_id)
    paused_ok = get_state(msg_chat_id, msg_id) == TimerState.PAUSED
    report.append(f"Toggle Pause ..... {'OK' if paused_ok else 'FAIL'}")

    # 5. toggle back to active
    toggle(self_client, msg_chat_id, msg_id)
    active_ok = get_state(msg_chat_id, msg_id) == TimerState.ACTIVE
    report.append(f"Toggle Active .... {'OK' if active_ok else 'FAIL'}")

    # 6. only one timer exists
    one_timer = active_count() == 1
    report.append(f"Single Timer ..... {'OK' if one_timer else 'FAIL'}")

    # 7. destroy clears state
    destroy(self_client, msg_chat_id, msg_id)
    destroyed = not has_timer(msg_chat_id, msg_id)
    report.append(f"Destroy .......... {'OK' if destroyed else 'FAIL'}")

    # 8. no timer leaks
    no_leak = active_count() == 0
    report.append(f"No Leaks ......... {'OK' if no_leak else 'FAIL'}")

    # 9. actions registered
    actions_ok = len(_actions) > 0
    report.append(f"Actions .......... {'OK' if actions_ok else 'FAIL'}")

    all_ok = all([
        callback_ok, trigger_ok, timer_ok, paused_ok,
        active_ok, one_timer, destroyed, no_leak, actions_ok,
    ])

    if not all_ok:
        report.append("")
        report.append("FAILED")
    else:
        report.append("")
        report.append("ALL OK")

    return "\n".join(report)
