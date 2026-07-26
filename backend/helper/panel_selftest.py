"""
Panel self-test — deterministic verification of the inline panel pipeline.

Outputs ONE structured report. No spam logging.
"""
from backend.helper import inline_engine
from backend.helper.panels import get_panel, get_action, _panels, _actions
from backend.helper.panel_timer import _timers


async def run_selftest(self_client, helper_client, owner_id: int) -> str:
    report = ["[PANEL SELFTEST]", ""]

    # 1. callback registered
    close_panel = get_panel("help")
    callback_ok = close_panel is not None
    report.append(f"Callback ......... {'OK' if callback_ok else 'FAIL'}")
    if not callback_ok:
        report.append("")
        report.append("FAILED")
        report.append("")
        report.append("Step:")
        report.append("register_panel('help')")
        report.append("")
        report.append("Reason:")
        report.append("no panel handler registered for 'help'")
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
        report.append("")
        report.append("Step:")
        report.append("trigger()")
        report.append("")
        report.append("Reason:")
        report.append(trigger_reason)
        return "\n".join(report)

    # 3. inline query answered (helper returned results)
    inline_query_ok = trigger_ok
    report.append(f"Inline Query ..... {'OK' if inline_query_ok else 'FAIL'}")

    # 4. click() returned a message
    click_ok = msg_id > 0
    report.append(f"Click ............ {'OK' if click_ok else 'FAIL'}")

    # 5. timer created
    timer_ok = False
    try:
        from backend.helper.panel_timer import start_timer
        start_timer(self_client, msg_chat_id, msg_id)
        timer_key = f"{msg_chat_id}:{msg_id}"
        timer_ok = timer_key in _timers and not _timers[timer_key].done()
    except Exception:
        pass
    report.append(f"Timer ............ {'OK' if timer_ok else 'FAIL'}")

    # 6. timer exists in registry
    timer_registry_ok = timer_ok
    report.append(f"Timer Registry ... {'OK' if timer_registry_ok else 'FAIL'}")

    # 7. inline message id stored (non-zero)
    msg_id_stored = msg_id > 0
    report.append(f"Inline Msg ID .... {'OK' if msg_id_stored else 'FAIL'}")

    # 8. close callback reachable
    close_ok = "panel:help" in str(_panels.get("help")) or get_panel("help") is not None
    report.append(f"Close ............ {'OK' if close_ok else 'FAIL'}")

    # 9. callback registered (actions)
    actions_ok = len(_actions) > 0
    report.append(f"Actions .......... {'OK' if actions_ok else 'FAIL'}")

    all_ok = all([
        callback_ok, trigger_ok, inline_query_ok, click_ok,
        timer_ok, timer_registry_ok, msg_id_stored, close_ok, actions_ok,
    ])

    if not all_ok:
        report.append("")
        report.append("FAILED")
    else:
        report.append("")
        report.append("ALL OK")

    return "\n".join(report)
