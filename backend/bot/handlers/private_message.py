"""Private Message Management — Glass UI panel + the incoming-message policy hook.

Two responsibilities, both attached to EXISTING architecture:

1. **Enforcement** — :func:`enforce_incoming` is called by the existing
   all-messages handler (``backend/bot/handlers/task_events.py``) before it
   dispatches anything, so an unauthorized private message is handled by the
   configured policy and never reaches the event-task path. No new listener,
   no new client, no new loop.
2. **Glass UI** — the ``panel:pm`` family (mode, unauthorized policy,
   allowlist, exceptions, temporary override, schedule) rendered through the
   existing ``register_panel`` / ``register_action`` / ``register_input``
   machinery, reachable from the mother panel.

Everything the panel shows comes from ``backend/services/private_message_service``
(persisted ``bot_settings`` state, loaded once at startup, cached in memory), and
every control performs a real, awaited, persisted change.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from urllib.parse import quote, unquote

from backend.helper import (
    InlinePanelBuilder,
    register_action,
    register_inline_builder,
    register_input,
    register_panel,
    render,
)
from backend.helper import inline_engine
from backend.helper import symbols as sym
from backend.helper.client import get_client
from backend.helper.rpc_timeout import rpc_await
from backend.helper.styles import DANGER, PRIMARY
from backend.services import private_message_service as service

logger = logging.getLogger(__name__)

PANEL_ID = "pm"
POLICY_PANEL_ID = "pm_policy"
ALLOW_PANEL_ID = "pm_allow"
EXCEPT_PANEL_ID = "pm_except"
TEMP_PANEL_ID = "pm_temp"
SCHEDULE_PANEL_ID = "pm_sched"
RULE_PANEL_ID = "pm_rule"

_RPC_TIMEOUT = 15.0
_MAX_LISTED_ENTRIES = 10
_TEMPORARY_OPTIONS: tuple[tuple[str, int], ...] = (
    (service.MODE_OPEN, 30),
    (service.MODE_OPEN, 8 * 60),
    (service.MODE_WHITELIST_ONLY, 30),
    (service.MODE_WHITELIST_ONLY, 8 * 60),
    (service.MODE_CLOSED, 30),
    (service.MODE_CLOSED, 8 * 60),
)
_MODE_CYCLE: tuple[str, ...] = service.MODES

#: Per-owner schedule-rule draft (server-side, like the Bio template builder).
#: Short callback data carries the field/step, never the draft itself.
_rule_drafts: dict[int, dict] = {}


# ── Enforcement (the incoming-message policy hook) ──


def _is_private_event(event) -> bool:
    flag = getattr(event, "is_private", None)
    if isinstance(flag, bool):
        return flag
    chat_id = getattr(event, "chat_id", None)
    sender_id = getattr(event, "sender_id", None)
    return bool(isinstance(chat_id, int) and chat_id > 0 and chat_id == sender_id)


def _event_message_id(event) -> int:
    message_id = getattr(event, "message_id", None)
    if isinstance(message_id, int) and message_id > 0:
        return message_id
    message = getattr(event, "message", None)
    candidate = getattr(message, "id", None)
    return candidate if isinstance(candidate, int) and candidate > 0 else 0


async def _delete_incoming(client, chat_id: int, message_id: int) -> bool:
    if client is None or chat_id == 0 or message_id == 0:
        return False
    try:
        await rpc_await(
            client.delete_messages(chat_id, [message_id], revoke=True),
            timeout=_RPC_TIMEOUT,
            label="private_message.delete_incoming",
        )
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("PRIVATE_MESSAGE_TRACE stage=delete_failed chat_id=%s error=%s", chat_id, type(exc).__name__)
        return False


async def _send_autoreply(client, sender_id: int, text: str) -> bool:
    if client is None or sender_id <= 0 or not text:
        return False
    try:
        await rpc_await(
            client.send_message(sender_id, text),
            timeout=_RPC_TIMEOUT,
            label="private_message.autoreply",
        )
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("PRIVATE_MESSAGE_TRACE stage=autoreply_failed sender_id=%s error=%s", sender_id, type(exc).__name__)
        return False


async def enforce_incoming(client, event, owner_id: int) -> bool:
    """Apply the configured policy to ONE incoming update.

    Returns ``True`` when the message may continue into the existing handlers,
    ``False`` when it was handled as unauthorized (so nothing else processes
    it). Never raises into the Telegram event path: an internal failure leaves
    the message permitted, which is exactly the behaviour that existed before
    this feature, and is therefore the safe failure mode.
    """
    try:
        if getattr(event, "out", False):
            return True
        if not _is_private_event(event):
            return True

        # The persisted policy is resolved before the FIRST decision, so a
        # restart never evaluates a message against the in-memory default.
        await service.ensure_loaded_async()

        sender_id = getattr(event, "sender_id", None)
        chat_id = getattr(event, "chat_id", 0) or 0
        message_id = _event_message_id(event)
        decision = service.decide_incoming(
            service.state(),
            sender_id=sender_id,
            is_private=True,
            is_owner=sender_id == owner_id,
            now=datetime.now(timezone.utc),
        )
        if decision.permitted:
            return True

        logger.info(
            "PRIVATE_MESSAGE_TRACE stage=unauthorized mode=%s source=%s action=%s sender_id=%s chat_id=%s",
            decision.mode, decision.source, decision.action, sender_id, chat_id,
        )
        if decision.action == service.POLICY_DELETE:
            await _delete_incoming(client, int(chat_id or 0), message_id)
        elif decision.action == service.POLICY_AUTOREPLY:
            ok = isinstance(sender_id, int) and sender_id > 0 and service.consume_autoreply_slot(sender_id)
            if ok:
                await _send_autoreply(client, int(sender_id), decision.autoreply_text)
            else:
                logger.info(
                    "PRIVATE_MESSAGE_TRACE stage=autoreply_suppressed sender_id=%s retry_in=%ss",
                    sender_id,
                    service.autoreply_seconds_remaining(sender_id) if isinstance(sender_id, int) else 0,
                )
        return False
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — never poison the event path
        logger.warning("PRIVATE_MESSAGE_TRACE stage=handler_error exception=%s", type(exc).__name__)
        return True


# ── Rendering ──


def _decode_state(value: str) -> tuple[int, str]:
    page, query = 1, ""
    for part in (value or "").split("&"):
        key, separator, val = part.partition("=")
        if key == "p" and separator and val.isdigit():
            page = max(1, int(val))
        elif key == "q" and separator:
            query = unquote(val)
    return page, query


def _encode_state(page: int, query: str = "") -> str:
    return f"p={max(1, int(page))}&q={quote(query or '', safe='')}"


def _directory() -> dict[int, str]:
    """The cached private-chat directory as ``{user_id: display_name}``.

    Reuses the Ghost Seen directory cache (TTL-bounded), so opening the
    allow-list picker never re-enumerates the whole dialog list on every tap.
    """
    try:
        from backend.services import ghost_seen_v2
        cached = ghost_seen_v2._manage_directory
    except Exception:
        return {}
    return {int(chat.chat_id): chat.display_name for chat in (cached or [])}


async def _load_picker(owner_id: int) -> list:
    from backend.services import ghost_seen_v2
    return await ghost_seen_v2.load_manage_directory(inline_engine.get_self_client(), owner_id)


def _entry_lines(entries: tuple[int, ...], names: dict[int, str]) -> str:
    if not entries:
        return "_none_"
    lines = []
    for user_id in entries[:_MAX_LISTED_ENTRIES]:
        label = names.get(user_id, "")
        lines.append(f"· `{user_id}` {label}".rstrip())
    if len(entries) > _MAX_LISTED_ENTRIES:
        lines.append(f"_…and {len(entries) - _MAX_LISTED_ENTRIES} more_")
    return "\n".join(lines)


def _persistence_line() -> str:
    return "database" if service.persistence_available() else "memory only (no database configured)"


def _mark(active: bool) -> str:
    """The registry's selected / not-selected marks — never a literal glyph."""
    return sym.STEP_COMPLETED if active else sym.BULLET


def _mode_button_row(builder: InlinePanelBuilder, current_mode: str) -> None:
    for mode in service.MODES:
        builder.add_row(
            f"{_mark(mode == current_mode)} {service.MODE_LABELS[mode]}",
            f"action:pm_mode:{mode}",
            PRIMARY,
        )


async def _render_main(owner_id: int) -> tuple[str, str, list]:
    now = datetime.now(timezone.utc)
    state_doc = service.state()
    summary = service.summarize(state_doc, now)

    control = service.SOURCE_LABELS.get(summary.source, summary.source)
    if summary.temporary is not None:
        expires = service.format_instant(
            service.parse_instant(summary.temporary.get("expires_at")), summary.schedule_timezone
        )
        control = (
            f"{control} · {service.format_remaining(summary.temporary_remaining_seconds)} left"
            f" (until {expires})"
        )

    lines = [
        f"**Effective mode:** {service.MODE_LABELS.get(summary.mode, summary.mode)}",
        f"**Control:** {control}",
        f"**Enforcement:** {'ON' if summary.enabled else 'OFF (no policy action)'}",
        f"**Unauthorized policy:** {service.POLICY_LABELS.get(summary.policy, summary.policy)}",
        f"**Allowlist:** {len(summary.allowlist)} user(s)",
        f"**Exceptions:** {len(summary.exceptions)} user(s)",
        f"**Schedule:** {'ON' if summary.schedule_enabled else 'OFF'} ({summary.schedule_timezone}, {len(summary.schedule_rules)} rule(s))",
    ]
    if summary.next_transition_at is not None:
        lines.append(
            "**Next transition:** "
            f"{service.format_instant(summary.next_transition_at, summary.schedule_timezone)}"
            f" — {summary.next_transition_label}"
        )
    else:
        lines.append("**Next transition:** none scheduled")
    lines.append(f"**Storage:** {_persistence_line()}")
    lines.append("")
    lines.append(
        "This controls how the Self Bot handles **incoming private messages** "
        "it has already received. It does not change your Telegram privacy "
        "settings and cannot stop someone from sending a message."
    )

    builder = InlinePanelBuilder()
    _mode_button_row(builder, summary.mode)
    builder.add_buttons(
        (sym.label(sym.STATE_WARNING, "Unauthorized policy"), f"panel:{POLICY_PANEL_ID}", PRIMARY),
        (sym.label(sym.ICON_SELECT, "Allowlist"), f"panel:{ALLOW_PANEL_ID}", PRIMARY),
    )
    builder.add_buttons(
        (sym.label(sym.ICON_PIN, "Exceptions"), f"panel:{EXCEPT_PANEL_ID}", PRIMARY),
        (sym.label(sym.STATE_EXPIRED, "Temporary"), f"panel:{TEMP_PANEL_ID}", PRIMARY),
    )
    builder.add_row(sym.label(sym.SCHEDULE_WEEKLY, "Schedule"), f"panel:{SCHEDULE_PANEL_ID}", PRIMARY)
    builder.add_row(
        sym.label(
            sym.OP_PAUSE if summary.enabled else sym.OP_RESUME,
            "Pause enforcement" if summary.enabled else "Enable enforcement",
        ),
        "action:pm_toggle_enabled",
        PRIMARY,
    )
    builder.add_row(sym.label(sym.OP_REFRESH, "Refresh"), "action:pm_refresh", PRIMARY)
    return "Private Messages", "\n".join(lines), builder.build()



async def _render_policy(owner_id: int) -> tuple[str, str, list]:
    state_doc = service.state()
    policy = str(state_doc.get("policy") or service.POLICY_IGNORE)
    text = str(state_doc.get("autoreply_text") or "")
    body = [
        f"**Current policy:** {service.POLICY_LABELS.get(policy, policy)}",
        "",
        "Applies only to messages from senders who are NOT authorized in the "
        "current mode.",
        "· **Ignore** — the message is left untouched and not processed.",
        "· **Delete** — the Self Bot deletes the message it received.",
        "· **Auto-reply** — the Self Bot answers once, then stays silent for "
        f"{int(service.AUTOREPLY_COOLDOWN_SECONDS)}s per sender.",
    ]
    if text:
        body.append(f"\n**Auto-reply text:** {text}")
    else:
        body.append("\n**Auto-reply text:** _not set — an auto-reply policy without text is treated as Ignore_")

    builder = InlinePanelBuilder()
    for candidate in service.POLICIES:
        builder.add_row(
            f"{_mark(candidate == policy)} {service.POLICY_LABELS[candidate]}",
            f"action:pm_policy:{candidate}",
            PRIMARY,
        )
    builder.add_row(sym.label(sym.OP_EDIT, "Set auto-reply text"), f"input:{POLICY_PANEL_ID}:autoreply", PRIMARY)
    return "Unauthorized Policy", "\n".join(body), builder.build()


async def _render_members(owner_id: int, panel_id: str, extra: str) -> tuple[str, str, list]:
    field_name = "allowlist" if panel_id == ALLOW_PANEL_ID else "exceptions"
    page, query = _decode_state(extra)
    chats = await _load_picker(owner_id)
    from backend.services.ghost_seen_v2 import manage_page_items

    view = manage_page_items(chats, page, query)
    state_doc = service.state()
    names = _directory()
    entries = tuple(state_doc.get(field_name) or [])

    title = "Allowlist" if field_name == "allowlist" else "Exceptions"
    body = [
        f"**{title}:** {len(entries)} user(s)",
        _entry_lines(entries, names),
        "",
        (
            "Authorized in **whitelist only** mode."
            if field_name == "allowlist"
            else "Authorized even in **closed** mode."
        ),
        "Tap a recent private chat to add or remove it. `Add` resolves a "
        "@username or numeric id through Telegram before storing it.",
    ]
    builder = InlinePanelBuilder()
    for index, chat in enumerate(view.chats, start=1):
        mark = "ON" if int(chat.chat_id) in entries else "OFF"
        builder.add_row(
            f"{index}. {chat.display_name}  {mark}",
            f"action:{'pm_allow_toggle' if field_name == 'allowlist' else 'pm_except_toggle'}:{int(chat.chat_id)}",
            PRIMARY,
        )
    if view.total_pages > 1:
        nav = []
        if view.page > 1:
            nav.append((sym.label(sym.OP_PREV, "Prev"), f"action:pm_page:{panel_id}:{view.page - 1}", PRIMARY))
        nav.append((f"{view.page}/{view.total_pages}", "panel:_nav:noop", PRIMARY))
        if view.page < view.total_pages:
            nav.append((sym.label(sym.OP_NEXT, "Next"), f"action:pm_page:{panel_id}:{view.page + 1}", PRIMARY))
        builder.add_buttons(*nav)
    builder.add_row(sym.label(sym.OP_CREATE, "Add user"), f"input:{panel_id}:add", PRIMARY)
    builder.add_row(sym.label(sym.OP_REFRESH, "Refresh"), f"panel:{panel_id}", PRIMARY)
    return title, "\n".join(body), builder.build()


async def _render_temporary(owner_id: int) -> tuple[str, str, list]:
    now = datetime.now(timezone.utc)
    state_doc = service.state()
    summary = service.summarize(state_doc, now)
    temporary = summary.temporary
    if temporary is None:
        body = [
            "**No temporary override is active.**",
            "",
            "A temporary override wins over the schedule and over the manual "
            "mode until it expires, then falls back automatically.",
        ]
    else:
        mode_label = service.MODE_LABELS.get(
            str(temporary.get("mode")), str(temporary.get("mode"))
        )
        body = [
            f"**Temporary mode:** {mode_label}",
            "**Activated:** "
            f"{service.format_instant(service.parse_instant(temporary.get('activated_at')), summary.schedule_timezone)}",
            "**Expires:** "
            f"{service.format_instant(service.parse_instant(temporary.get('expires_at')), summary.schedule_timezone)}",
            f"**Remaining:** {service.format_remaining(summary.temporary_remaining_seconds)}",
        ]
    builder = InlinePanelBuilder()
    for mode, minutes in _TEMPORARY_OPTIONS:
        label = service.MODE_LABELS[mode].split(" (")[0]
        duration = "30m" if minutes == 30 else f"{minutes // 60}h"
        builder.add_row(
            sym.label(sym.STATE_EXPIRED, f"{label} · {duration}"),
            f"action:pm_temp_set:{mode}:{minutes}",
            PRIMARY,
        )
    builder.add_row(sym.label(sym.OP_DELETE, "Clear temporary"), "action:pm_temp_clear", PRIMARY)
    return "Temporary Mode", "\n".join(body), builder.build()


def _draft(owner_id: int) -> dict:
    draft = _rule_drafts.get(owner_id)
    if draft is None:
        draft = {"days": [], "start": "09:00", "end": "18:00", "mode": service.MODE_WHITELIST_ONLY, "index": None}
        _rule_drafts[owner_id] = draft
    return draft


def _draft_scope(draft: dict) -> str:
    index = draft.get("index")
    return "new rule" if index is None else f"rule {int(index) + 1}"


def _draft_body(owner_id: int, notice: str = "") -> str:
    state_doc = service.state()
    schedule = state_doc.get("schedule") or {}
    draft = _draft(owner_id)
    lines = []
    if notice:
        lines.append(sym.label(sym.STATE_WARNING, notice))
        lines.append("")
    lines.extend([
        f"**Timezone:** {schedule.get('timezone')} (change it on the Schedule screen)",
        f"**Days:** {service.describe_days(draft['days'])}",
        f"**Window:** {draft['start']} – {draft['end']}",
        f"**Mode:** {service.MODE_LABELS[draft['mode']]}",
        "",
        "Days not selected do not match. An end earlier than the start spans "
        "midnight and belongs to the start day.",
        f"Editing: {_draft_scope(draft)}",
    ])
    return "\n".join(lines)


def _draft_buttons(owner_id: int) -> list:
    draft = _draft(owner_id)
    builder = InlinePanelBuilder()
    days = draft["days"]
    row_a = []
    for day in range(4):
        row_a.append(
            (f"{_mark(day in days)}{service.WEEKDAY_LABELS[day]}", f"action:pm_rule_day:{day}", PRIMARY)
        )
    builder.add_buttons(*row_a)
    row_b = []
    for day in range(4, 7):
        row_b.append(
            (f"{_mark(day in days)}{service.WEEKDAY_LABELS[day]}", f"action:pm_rule_day:{day}", PRIMARY)
        )
    row_b.append((sym.label(sym.OP_CLOSE, "None"), "action:pm_rule_days_clear", PRIMARY))
    builder.add_buttons(*row_b)
    builder.add_buttons(
        (sym.label(sym.META_TIME, "Start"), f"input:{RULE_PANEL_ID}:start", PRIMARY),
        (sym.label(sym.META_TIME, "End"), f"input:{RULE_PANEL_ID}:end", PRIMARY),
    )
    builder.add_row(
        sym.label(sym.ICON_SELECT, f"Mode: {service.MODE_LABELS[draft['mode']].split(' (')[0]}"),
        "action:pm_rule_mode",
        PRIMARY,
    )
    builder.add_row(sym.label(sym.OP_SAVE, "Save rule"), "action:pm_rule_save", PRIMARY)
    builder.add_row(sym.label(sym.OP_BACK, "Cancel"), f"panel:{SCHEDULE_PANEL_ID}", DANGER)
    return builder.build()


async def _render_rule(owner_id: int, notice: str = "") -> tuple[str, str, list]:
    return "Schedule Rule", _draft_body(owner_id, notice), _draft_buttons(owner_id)


async def _render_schedule(owner_id: int) -> tuple[str, str, list]:
    now = datetime.now(timezone.utc)
    state_doc = service.state()
    summary = service.summarize(state_doc, now)
    body = [
        f"**Schedule:** {'ON' if summary.schedule_enabled else 'OFF'}",
        f"**Timezone:** {summary.schedule_timezone}",
        f"**Rules:** {len(summary.schedule_rules)}",
        "",
    ]
    if summary.schedule_rules:
        for index, rule in enumerate(summary.schedule_rules):
            active = summary.source == service.SOURCE_SCHEDULE and service.rule_matches(
                rule, now.astimezone(service.resolve_zone(summary.schedule_timezone) or timezone.utc)
            )
            body.append(f"{_mark(active)} {index + 1}. {service.describe_rule(rule)}")
    else:
        body.append("_no rules yet_")
    body.append("")
    if summary.next_transition_at is not None:
        body.append(
            f"**Next transition:** {service.format_instant(summary.next_transition_at, summary.schedule_timezone)}"
            f" — {summary.next_transition_label}"
        )
    else:
        body.append("**Next transition:** none")
    body.append("")
    body.append(
        "Precedence: **temporary override** → **schedule** → **manual mode**. "
        "Later rules win when several match the same moment."
    )

    builder = InlinePanelBuilder()
    builder.add_row(
        sym.label(
            sym.OP_PAUSE if summary.schedule_enabled else sym.OP_RESUME,
            "Disable schedule" if summary.schedule_enabled else "Enable schedule",
        ),
        "action:pm_sched_toggle",
        PRIMARY,
    )
    builder.add_row(sym.label(sym.META_TIME, "Set timezone"), f"input:{SCHEDULE_PANEL_ID}:timezone", PRIMARY)
    for index, rule in enumerate(summary.schedule_rules):
        builder.add_buttons(
            (sym.label(sym.OP_EDIT, str(index + 1)), f"action:pm_sched_edit:{index}", PRIMARY),
            (sym.label(sym.OP_DELETE, str(index + 1)), f"action:pm_sched_del:{index}", PRIMARY),
        )
    builder.add_row(sym.label(sym.OP_CREATE, "Add rule"), "action:pm_sched_new", PRIMARY)
    return "Schedule", "\n".join(body), builder.build()


async def _panel_result(panel_id: str, extra: str, owner_id: int) -> tuple[str, str, list]:
    await service.ensure_loaded_async()
    if panel_id == PANEL_ID:
        return await _render_main(owner_id)
    if panel_id == POLICY_PANEL_ID:
        return await _render_policy(owner_id)
    if panel_id in (ALLOW_PANEL_ID, EXCEPT_PANEL_ID):
        return await _render_members(owner_id, panel_id, extra)
    if panel_id == TEMP_PANEL_ID:
        return await _render_temporary(owner_id)
    if panel_id == SCHEDULE_PANEL_ID:
        return await _render_schedule(owner_id)
    if panel_id == RULE_PANEL_ID:
        return await _render_rule(owner_id)
    return await _render_main(owner_id)


def _inline_builder(panel_id: str, owner_id: int):
    async def _builder(event, extra: str) -> list:
        title, body, buttons = await _panel_result(panel_id, extra, owner_id)
        return [render(title, body, buttons)]

    return _builder


# ── Actions ──


async def _mode_action(event, extra: str, chat_id: int):
    if not await service.set_mode(extra.strip()):
        return "Private Messages", "That mode is not valid — nothing changed.", []
    return await _render_main(inline_engine.get_owner_id())


async def _toggle_enabled_action(event, extra: str, chat_id: int):
    state_doc = service.state()
    if not await service.set_enabled(not bool(state_doc.get("enabled", True))):
        return "Private Messages", "Could not save the change. Please try again.", []
    return await _render_main(inline_engine.get_owner_id())


async def _refresh_action(event, extra: str, chat_id: int):
    await service.reload_async()
    return await _render_main(inline_engine.get_owner_id())


async def _policy_action(event, extra: str, chat_id: int):
    if not await service.set_policy(extra.strip()):
        return "Unauthorized Policy", "That policy is not valid — nothing changed.", []
    return await _render_policy(inline_engine.get_owner_id())


async def _page_action(event, extra: str, chat_id: int):
    parts = (extra or "").split(":")
    panel_id = parts[0] if parts and parts[0] in (ALLOW_PANEL_ID, EXCEPT_PANEL_ID) else ALLOW_PANEL_ID
    page = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
    owner_id = inline_engine.get_owner_id()
    return await _panel_result(panel_id, _encode_state(page), owner_id)


async def _members_toggle(event, extra: str, chat_id: int, field_name: str, panel_id: str):
    try:
        user_id = int(str(extra).strip())
    except (TypeError, ValueError):
        return "Allowlist", "That user reference is not usable — nothing changed.", []
    if user_id <= 0:
        return "Allowlist", "That user reference is not usable — nothing changed.", []
    state_doc = service.state()
    entries = tuple(state_doc.get(field_name) or [])
    ok = (
        await service.remove_list_entry(field_name, user_id)
        if user_id in entries
        else await service.add_list_entry(field_name, user_id)
    )
    if not ok:
        return "Allowlist", "Could not save the change. Please try again.", []
    owner_id = inline_engine.get_owner_id()
    return await _panel_result(panel_id, _encode_state(1), owner_id)


async def _allow_toggle_action(event, extra: str, chat_id: int):
    return await _members_toggle(event, extra, chat_id, "allowlist", ALLOW_PANEL_ID)


async def _except_toggle_action(event, extra: str, chat_id: int):
    return await _members_toggle(event, extra, chat_id, "exceptions", EXCEPT_PANEL_ID)


async def _temp_set_action(event, extra: str, chat_id: int):
    mode, _, minutes_text = (extra or "").partition(":")
    if not minutes_text.isdigit():
        return "Temporary Mode", "That duration is not valid — nothing changed.", []
    if not await service.set_temporary(mode.strip(), int(minutes_text)):
        return "Temporary Mode", "That temporary mode is not valid — nothing changed.", []
    return await _render_temporary(inline_engine.get_owner_id())


async def _temp_clear_action(event, extra: str, chat_id: int):
    if not await service.clear_temporary():
        return "Temporary Mode", "Could not clear the temporary mode. Please try again.", []
    return await _render_temporary(inline_engine.get_owner_id())


async def _schedule_toggle_action(event, extra: str, chat_id: int):
    state_doc = service.state()
    if not await service.set_schedule_enabled(not bool((state_doc.get("schedule") or {}).get("enabled"))):
        return "Schedule", "Could not save the change. Please try again.", []
    return await _render_schedule(inline_engine.get_owner_id())


async def _schedule_new_action(event, extra: str, chat_id: int):
    owner_id = inline_engine.get_owner_id()
    _rule_drafts[owner_id] = {
        "days": [],
        "start": "09:00",
        "end": "18:00",
        "mode": service.MODE_WHITELIST_ONLY,
        "index": None,
    }
    return await _render_rule(owner_id)


async def _schedule_edit_action(event, extra: str, chat_id: int):
    owner_id = inline_engine.get_owner_id()
    try:
        index = int(str(extra).strip())
    except (TypeError, ValueError):
        return await _render_schedule(owner_id)
    rules = service.state()["schedule"]["rules"]
    if not (0 <= index < len(rules)):
        return "Schedule", "That rule no longer exists.", []
    rule = rules[index]
    _rule_drafts[owner_id] = {
        "days": list(rule.get("days") or []),
        "start": str(rule.get("start")),
        "end": str(rule.get("end")),
        "mode": str(rule.get("mode")),
        "index": index,
    }
    return await _render_rule(owner_id)


async def _schedule_delete_action(event, extra: str, chat_id: int):
    owner_id = inline_engine.get_owner_id()
    try:
        index = int(str(extra).strip())
    except (TypeError, ValueError):
        return "Schedule", "That rule reference is not usable.", []
    if not await service.delete_schedule_rule(index):
        return "Schedule", "Could not delete that rule. Please try again.", []
    return await _render_schedule(owner_id)


async def _rule_day_action(event, extra: str, chat_id: int):
    owner_id = inline_engine.get_owner_id()
    draft = _draft(owner_id)
    try:
        day = int(str(extra).strip())
    except (TypeError, ValueError):
        return await _render_rule(owner_id)
    if 0 <= day <= 6:
        days = set(draft["days"])
        if day in days:
            days.discard(day)
        else:
            days.add(day)
        draft["days"] = sorted(days)
    return await _render_rule(owner_id)


async def _rule_days_clear_action(event, extra: str, chat_id: int):
    owner_id = inline_engine.get_owner_id()
    _draft(owner_id)["days"] = []
    return await _render_rule(owner_id)


async def _rule_mode_action(event, extra: str, chat_id: int):
    owner_id = inline_engine.get_owner_id()
    draft = _draft(owner_id)
    current = draft["mode"] if draft["mode"] in _MODE_CYCLE else _MODE_CYCLE[0]
    draft["mode"] = _MODE_CYCLE[(_MODE_CYCLE.index(current) + 1) % len(_MODE_CYCLE)]
    return await _render_rule(owner_id)


async def _rule_save_action(event, extra: str, chat_id: int):
    owner_id = inline_engine.get_owner_id()
    draft = _draft(owner_id)
    rule = {
        "days": list(draft["days"]),
        "start": draft["start"],
        "end": draft["end"],
        "mode": draft["mode"],
    }
    if not await service.save_schedule_rule(rule, draft.get("index")):
        return await _render_rule(
            owner_id,
            "A rule needs at least one day, a start and an end that differ, "
            "and a valid mode.",
        )
    _rule_drafts.pop(owner_id, None)
    return await _render_schedule(owner_id)


# ── Inputs ──


async def _show_after_input(panel_id: str, owner_id: int, notice: str, chat_id, msg_id, inline_chat_id, inline_msg_id) -> None:
    """Re-render the owning panel in the panel message and remove the reply."""
    if panel_id == RULE_PANEL_ID:
        title, body, buttons = await _render_rule(owner_id, notice)
    else:
        title, body, buttons = await _panel_result(panel_id, _encode_state(1), owner_id)
        if notice:
            body = f"{sym.label(sym.STATE_WARNING, notice)}\n\n{body}"
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        from backend.helper.panel_render import render_edit

        rendered, built = render_edit(title, body, buttons)
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, rendered, buttons=built)
        except Exception as exc:
            logger.warning("private_message panel input edit failed: %s", exc)
    client = inline_engine.get_self_client()
    if client is not None and chat_id and msg_id:
        try:
            await client.delete_messages(chat_id, [msg_id])
        except Exception:
            pass


async def _autoreply_input_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    owner_id = inline_engine.get_owner_id()
    cleaned = (text or "").strip()
    if not cleaned:
        notice = "The auto-reply text cannot be empty."
    elif len(cleaned) > service.MAX_AUTOREPLY_LENGTH:
        notice = f"The auto-reply text must be at most {service.MAX_AUTOREPLY_LENGTH} characters."
    else:
        notice = "" if await service.set_autoreply_text(cleaned) else "Could not save the text. Please try again."
    await _show_after_input(POLICY_PANEL_ID, owner_id, notice, chat_id, msg_id, inline_chat_id, inline_msg_id)


def _add_user_input_handler(panel_id: str, field_name: str):
    """One input handler per list panel — the pending-input callback carries no
    panel-specific extra, so the target list is bound where it is registered."""

    async def _handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
        owner_id = inline_engine.get_owner_id()
        user_id, notice = await _resolve_user_reference((text or "").strip(), owner_id)
        if user_id is not None and not await service.add_list_entry(field_name, user_id):
            notice = "Could not save that user. Please try again."
        await _show_after_input(panel_id, owner_id, notice, chat_id, msg_id, inline_chat_id, inline_msg_id)

    return _handler


async def _resolve_user_reference(text: str, owner_id: int) -> tuple[int | None, str]:
    """Resolve the owner's input to a VALIDATED Telegram user id.

    The reference is resolved through the typed Telegram entity layer (the
    established, timeout-bounded boundary) and must come back as a real user;
    an unresolvable or non-user reference is refused instead of being stored.
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return None, "Enter a @username or a numeric user id."
    if cleaned.lstrip("@").isdigit():
        candidate = int(cleaned.lstrip("@"))
        if candidate <= 0:
            return None, "That is not a usable user id."
        if candidate == owner_id:
            return None, "You cannot add yourself."
    client = inline_engine.get_self_client()
    if client is None:
        return None, "Telegram is not connected, so the user cannot be verified right now."
    try:
        from backend.telegram_api.entities import get_entity

        entity = await get_entity(client, cleaned)
    except asyncio.CancelledError:
        raise
    except Exception:
        return None, f"Telegram could not resolve “{cleaned}”. Use a @username or an id you have chatted with."
    resolved_id = getattr(entity, "id", None)
    if not isinstance(resolved_id, int) or resolved_id <= 0:
        return None, "That reference is a chat, not a user."
    if not getattr(entity, "first_name", None) and not getattr(entity, "username", None) and not getattr(entity, "bot", False):
        return None, "That reference is a chat, not a user."
    if resolved_id == owner_id:
        return None, "You cannot add yourself."
    return resolved_id, f"Added `{resolved_id}`."


def _rule_time_input_handler(field: str):
    """One handler per time field of the rule draft."""

    async def _handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
        owner_id = inline_engine.get_owner_id()
        draft = _draft(owner_id)
        parsed = service.parse_hhmm((text or "").strip())
        notice = ""
        if parsed is None:
            notice = "Enter a time as HH:MM (24-hour), for example 09:30."
        else:
            draft[field] = service.format_hhmm(parsed)
        await _show_after_input(RULE_PANEL_ID, owner_id, notice, chat_id, msg_id, inline_chat_id, inline_msg_id)

    return _handler


async def _timezone_input_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    owner_id = inline_engine.get_owner_id()
    cleaned = (text or "").strip()
    if service.resolve_zone(cleaned) is None:
        notice = f"“{cleaned}” is not a usable IANA timezone (for example Asia/Tehran)."
    else:
        notice = "" if await service.set_schedule_timezone(cleaned) else "Could not save the timezone. Please try again."
    await _show_after_input(SCHEDULE_PANEL_ID, owner_id, notice, chat_id, msg_id, inline_chat_id, inline_msg_id)


# ── Registration ──


def register(client, owner_id: int, tz_str: str = "UTC") -> None:
    # Preload the persisted state in the background so the common case never
    # waits on a database read. The gate and the panel both await the same
    # single-flight load, so registration order is an optimization, not a
    # correctness requirement.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        try:
            from backend.runtime.task_guard import guarded_create_task

            guarded_create_task(service.ensure_loaded_async(), name="private_message:preload_state")
        except Exception:
            pass

    register_panel(PANEL_ID, lambda event, extra: _panel_result(PANEL_ID, extra, owner_id), parent="menu", title="Private Messages")
    register_panel(POLICY_PANEL_ID, lambda event, extra: _panel_result(POLICY_PANEL_ID, extra, owner_id), parent=PANEL_ID, title="Unauthorized Policy")
    register_panel(ALLOW_PANEL_ID, lambda event, extra: _panel_result(ALLOW_PANEL_ID, extra, owner_id), parent=PANEL_ID, title="Allowlist")
    register_panel(EXCEPT_PANEL_ID, lambda event, extra: _panel_result(EXCEPT_PANEL_ID, extra, owner_id), parent=PANEL_ID, title="Exceptions")
    register_panel(TEMP_PANEL_ID, lambda event, extra: _panel_result(TEMP_PANEL_ID, extra, owner_id), parent=PANEL_ID, title="Temporary Mode")
    register_panel(SCHEDULE_PANEL_ID, lambda event, extra: _panel_result(SCHEDULE_PANEL_ID, extra, owner_id), parent=PANEL_ID, title="Schedule")
    register_panel(RULE_PANEL_ID, lambda event, extra: _panel_result(RULE_PANEL_ID, extra, owner_id), parent=SCHEDULE_PANEL_ID, title="Schedule Rule")

    register_inline_builder(PANEL_ID, _inline_builder(PANEL_ID, owner_id))
    register_inline_builder(POLICY_PANEL_ID, _inline_builder(POLICY_PANEL_ID, owner_id))
    register_inline_builder(ALLOW_PANEL_ID, _inline_builder(ALLOW_PANEL_ID, owner_id))
    register_inline_builder(EXCEPT_PANEL_ID, _inline_builder(EXCEPT_PANEL_ID, owner_id))
    register_inline_builder(TEMP_PANEL_ID, _inline_builder(TEMP_PANEL_ID, owner_id))
    register_inline_builder(SCHEDULE_PANEL_ID, _inline_builder(SCHEDULE_PANEL_ID, owner_id))
    register_inline_builder(RULE_PANEL_ID, _inline_builder(RULE_PANEL_ID, owner_id))

    register_action("pm_mode", _mode_action)
    register_action("pm_toggle_enabled", _toggle_enabled_action)
    register_action("pm_refresh", _refresh_action)
    register_action("pm_policy", _policy_action)
    register_action("pm_page", _page_action)
    register_action("pm_allow_toggle", _allow_toggle_action)
    register_action("pm_except_toggle", _except_toggle_action)
    register_action("pm_temp_set", _temp_set_action)
    register_action("pm_temp_clear", _temp_clear_action)
    register_action("pm_sched_toggle", _schedule_toggle_action)
    register_action("pm_sched_new", _schedule_new_action)
    register_action("pm_sched_edit", _schedule_edit_action)
    register_action("pm_sched_del", _schedule_delete_action)
    register_action("pm_rule_day", _rule_day_action)
    register_action("pm_rule_days_clear", _rule_days_clear_action)
    register_action("pm_rule_mode", _rule_mode_action)
    register_action("pm_rule_save", _rule_save_action)

    register_input(POLICY_PANEL_ID, "autoreply", {
        "handler": _autoreply_input_handler,
        "prompt": "**Auto-reply text**\n\nEnter the message one unauthorized sender receives (once per cooldown):\n\n_Reply below._",
    })
    register_input(ALLOW_PANEL_ID, "add", {
        "handler": _add_user_input_handler(ALLOW_PANEL_ID, "allowlist"),
        "prompt": "**Allowlist — add user**\n\nSend a @username or a numeric user id. It is resolved through Telegram before it is stored:\n\n_Reply below._",
    })
    register_input(EXCEPT_PANEL_ID, "add", {
        "handler": _add_user_input_handler(EXCEPT_PANEL_ID, "exceptions"),
        "prompt": "**Exceptions — add user**\n\nSend a @username or a numeric user id. It is resolved through Telegram before it is stored:\n\n_Reply below._",
    })
    register_input(RULE_PANEL_ID, "start", {
        "handler": _rule_time_input_handler("start"),
        "prompt": "**Rule start**\n\nEnter the start time as HH:MM (24-hour):\n\n_Reply below._",
    })
    register_input(RULE_PANEL_ID, "end", {
        "handler": _rule_time_input_handler("end"),
        "prompt": "**Rule end**\n\nEnter the end time as HH:MM (24-hour). An earlier end spans midnight:\n\n_Reply below._",
    })
    register_input(SCHEDULE_PANEL_ID, "timezone", {
        "handler": _timezone_input_handler,
        "prompt": "**Schedule timezone**\n\nEnter an IANA timezone name (for example Asia/Tehran):\n\n_Reply below._",
    })
