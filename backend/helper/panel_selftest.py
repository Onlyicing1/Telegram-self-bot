"""
Panel self-test — deterministic verification of the inline panel pipeline.

Outputs ONE structured report. No spam logging.
"""
from backend.helper import inline_engine
from backend.helper.panels import get_panel, get_action, _panels, _actions
from backend.helper.lifecycle import get_lifecycle
from backend.services import settings_service


async def run_selftest(self_client, helper_client, owner_id: int) -> str:
    report = ["[PANEL SELFTEST]", ""]

    lifecycle = get_lifecycle()

    # 1. callback registered
    close_panel = get_panel("help")
    callback_ok = close_panel is not None
    report.append(f"Callback ......... {'OK' if callback_ok else 'FAIL'}")
    if not callback_ok:
        report.append("")
        report.append("FAILED")
        return "\n".join(report)

    # 2. lifecycle configured
    lifecycle.configure(self_client, owner_id)
    configured_ok = lifecycle._self_client is not None
    report.append(f"Lifecycle ........ {'OK' if configured_ok else 'FAIL'}")

    # 3. trigger() returns success
    trigger_ok = False
    trigger_reason = ""
    try:
        success, msg_chat_id, msg_id = await lifecycle.create_panel(owner_id, "help")
        trigger_ok = success and msg_id > 0
        if not trigger_ok:
            trigger_reason = "create_panel returned success=False or msg_id=0"
    except Exception as exc:
        trigger_reason = f"create_panel raised: {exc}"
    report.append(f"Trigger .......... {'OK' if trigger_ok else 'FAIL'}")
    if not trigger_ok:
        report.append("")
        report.append("FAILED")
        report.append(f"Reason: {trigger_reason}")
        return "\n".join(report)

    # 4. session exists
    session = lifecycle.sessions.get(msg_chat_id, msg_id)
    session_ok = session is not None
    report.append(f"Session .......... {'OK' if session_ok else 'FAIL'}")

    # 5. timer created (only if auto-close is ON)
    was_enabled = settings_service.is_auto_close_enabled()
    settings_service.set_auto_close_enabled(True)
    timer_ok = lifecycle.timers.has_timer(msg_chat_id, msg_id)
    report.append(f"Timer Init ....... {'OK' if timer_ok else 'FAIL'}")

    # 6. destroy clears state
    await lifecycle.destroy_panel(msg_chat_id, msg_id)
    destroyed = not lifecycle.timers.has_timer(msg_chat_id, msg_id)
    report.append(f"Destroy .......... {'OK' if destroyed else 'FAIL'}")

    # 7. no timer leaks
    no_leak = lifecycle.timers.active_count() == 0
    report.append(f"No Leaks ......... {'OK' if no_leak else 'FAIL'}")

    # 8. no session leaks
    no_session_leak = lifecycle.session_count() == 0
    report.append(f"No Sess Leaks .... {'OK' if no_session_leak else 'FAIL'}")

    # 9. actions registered
    actions_ok = len(_actions) > 0
    report.append(f"Actions .......... {'OK' if actions_ok else 'FAIL'}")

    # Restore original preference
    settings_service.set_auto_close_enabled(was_enabled)

    all_ok = all([
        callback_ok, configured_ok, trigger_ok, session_ok,
        timer_ok, destroyed, no_leak, no_session_leak, actions_ok,
    ])

    if not all_ok:
        report.append("")
        report.append("FAILED")
    else:
        report.append("")
        report.append("ALL OK")

    return "\n".join(report)
