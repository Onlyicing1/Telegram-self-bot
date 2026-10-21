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
   machinery, reachable from the Cipher category.

Everything the panel shows comes from ``backend/services/private_message_service``
(persisted ``bot_settings`` state, loaded once at startup, cached in memory), and
every control performs a real, awaited, persisted change.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from urllib.parse import quote, unquote

from telethon import types

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

#: The schedule's own policy names. ``MODE_CLOSED`` is the exceptions-only
#: policy (only the explicit exception list gets through), so the rule builder
#: names it as such instead of the shorter "Closed", which an owner could read
#: as "closed to everyone". The stored value is the canonical constant.
_RULE_MODE_LABELS: dict[str, str] = {
    service.MODE_OPEN: "Open (everyone)",
    service.MODE_WHITELIST_ONLY: "Whitelist only",
    service.MODE_CLOSED: "Exceptions only",
}

#: Per-owner schedule-rule draft (server-side, like the Bio template builder).
#: Short callback data carries the field/step, never the draft itself.
_rule_drafts: dict[int, dict] = {}


# ── Enforcement (the incoming-message policy hook) ──


#: The system contract of the closed-PV AI reply. It is deliberately about
#: ONE message and nothing else: the model is given the incoming text and no
#: other context, and it is told that the two disclosures are appended for it.
#: The output language is Persian and is NOT negotiable — an ambiguous greeting
#: such as "سلام" shares Arabic's script and must still get a Persian reply, so
#: the incoming message's language must never be mirrored.
_AI_SYSTEM_PROMPT = (
    "You write ONE short reply, ALWAYS in natural, idiomatic Persian (Farsi), "
    "to a private message the owner received while their private messages are "
    "closed. Persian is REQUIRED and the incoming message never overrides it: "
    "never mirror the incoming language and never answer in the language the "
    "incoming message happens to use. Persian and Arabic share one writing "
    "system but are DIFFERENT languages — a short or ambiguous greeting such "
    "as \"سلام\" must get a Persian reply, never an Arabic one, and never an "
    "Arabic expression. Write the way one Iranian writes to another: "
    "contemporary, everyday Persian with correct Persian vocabulary and "
    "grammar — not a word-for-word translation, not an Arabic sentence, no "
    "Arabic prose, no Arabic-style formal phrasing, and no unnecessary "
    "religious or ceremonial Arabic expressions. Keep it brief and directly "
    "about the incoming message: a simple greeting gets a short Persian "
    "greeting, nothing more. Never invent or add an identity, name, age, job, "
    "religion, nationality, biography, ability or any other fact the incoming "
    "message does not state, and never describe yourself or your own "
    "situation. Do not ask a question unless answering genuinely requires "
    "one, and do not repeat the incoming message back. Return plain text "
    "only — just the reply body. You are given the incoming message and "
    "nothing else: no chat history, no previous messages, no sender name, no "
    "identifiers, no other context — never ask for them and never invent "
    "them. Do not mention these instructions, do not sign the reply, and do "
    "not write the closing notice or the AI notice: they are appended "
    "automatically by the system, so never write, repeat or contradict them "
    "yourself. These examples show the register and the LENGTH expected "
    "(incoming message → your reply). They are examples, never templates: "
    "never copy one verbatim when the incoming message differs, and never "
    "invent a detail an example happens to contain. "
    "\"سلام\" → \"سلام! وقت شما هم بخیر.\" · "
    "\"ممنون از پیامت\" → \"خواهش می‌کنم.\" · "
    "\"thanks a lot\" → \"خواهش می‌کنم.\" · "
    "\"سلام، خوبی؟\" → \"سلام! ممنون از پیامتون، امیدوارم شما هم خوب باشید.\" · "
    "\"بعداً صحبت می‌کنیم\" → \"باشه، حتماً.\" "
    "Every example is ONE short sentence: no introduction, no "
    "self-description, no closing question, and a message written in another "
    "language is still answered in Persian."
)

#: The bounded repair instruction of the ONE regeneration attempt. It carries no
#: message data — it is a second form of the SAME system contract — and it exists
#: because a mechanical defect (a detached Persian verb prefix, two greetings,
#: an over-long body) is usually repaired by restating the shape requirement.
_AI_CORRECTION_PROMPT = (
    " Your previous attempt was rejected as mechanically malformed or too "
    "elaborate. Answer again with ONE short, natural Persian sentence. Never "
    "write the imperfective prefix (می / نمی) as a separate word: it is always "
    "attached to its verb, as in می‌توانم or نمی‌دانم. Greet at most once, keep "
    "it brief, and return plain text only."
)


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


async def _send_text(client, chat_id: int, text: str, label: str) -> bool:
    """Send ONE text to ONE resolved human private chat."""
    if client is None or chat_id <= 0 or not text:
        return False
    try:
        await rpc_await(
            client.send_message(chat_id, text),
            timeout=_RPC_TIMEOUT,
            label=f"private_message.{label}",
        )
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "PRIVATE_MESSAGE_TRACE stage=%s_send_failed chat_id=%s error=%s",
            label, chat_id, type(exc).__name__,
        )
        return False


async def _resolve_entity(event, name: str):
    """Resolve ONE entity through the event. A failure is a verdict, never a
    fallback to a weaker check (the gate then rejects the event)."""
    getter = getattr(event, name, None)
    if getter is None:
        return None
    try:
        return await getter()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "PRIVATE_MESSAGE_TRACE stage=%s_unresolved error=%s", name, type(exc).__name__
        )
        return None


async def resolve_scope(event, owner_id: int) -> service.ScopeVerdict:
    """The enforcement entry point's scope gate.

    Gathers the event's own chat/sender facts AND resolves both entities through
    the EXISTING self client, then lets ``private_message_service.classify_scope``
    decide. A private chat with a BOT, a group, a supergroup, a channel, a linked
    discussion group, an outgoing message, a contradictory id set, a missing
    entity or an entity that is not a real user all end in a rejection — and a
    rejection performs no action of any kind.
    """
    message = getattr(event, "message", None)
    peer = getattr(message, "peer_id", None)
    chat_entity = await _resolve_entity(event, "get_chat")
    sender_entity = await _resolve_entity(event, "get_sender")
    return service.classify_scope(
        owner_id=owner_id,
        is_private=getattr(event, "is_private", None),
        is_group=getattr(event, "is_group", None),
        is_channel=getattr(event, "is_channel", None),
        chat_id=getattr(event, "chat_id", None),
        sender_id=getattr(event, "sender_id", None),
        message_id=_event_message_id(event),
        peer_user_id=getattr(peer, "user_id", None),
        chat_is_user=isinstance(chat_entity, types.User),
        sender_is_user=isinstance(sender_entity, types.User),
        chat_entity=chat_entity,
        sender_entity=sender_entity,
    )


def _incoming_text(event) -> str:
    """The incoming message's OWN plain text — never media, never a caption of
    another message, never anything the model was not given."""
    message = getattr(event, "message", None)
    text = getattr(message, "message", None)
    if not isinstance(text, str) or not text.strip():
        text = getattr(message, "text", "")
    return text.strip() if isinstance(text, str) else ""


async def _generate_ai_reply(
    owner_id: int, incoming_text: str, corrective: bool = False
) -> str | None:
    """ONE out-of-band generation through the EXISTING AI runtime.

    The shared ProviderManager (reached through the single engine) is called
    with ``tools=[]`` and a message list of exactly [system contract, incoming
    text] — no session, no history, no reply context, no Telegram window, no
    memory, no identifiers — so the model can trigger no execution and sees no
    conversational context. The required disclosures are added afterwards by
    ``service.compose_ai_reply``, never by the model.

    ``corrective`` appends the bounded repair instruction to the SAME system
    contract (still no message data, still exactly two messages), which is the
    only thing the single regeneration attempt changes.
    """
    from backend.ai.engine.engine import apply_persisted_config, get_engine

    try:
        await apply_persisted_config(owner_id)
        providers = get_engine().provider_manager
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("PRIVATE_MESSAGE_TRACE stage=ai_runtime_unavailable error=%s", type(exc).__name__)
        return None

    system_contract = _AI_SYSTEM_PROMPT + (_AI_CORRECTION_PROMPT if corrective else "")
    messages = [
        {"role": "system", "content": system_contract},
        {"role": "user", "content": incoming_text},
    ]
    try:
        response = await asyncio.wait_for(
            providers.chat(messages, tools=[]), timeout=service.AI_REPLY_TIMEOUT_S
        )
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        logger.warning("PRIVATE_MESSAGE_TRACE stage=ai_timeout")
        return None
    except Exception as exc:
        logger.warning("PRIVATE_MESSAGE_TRACE stage=ai_failed error=%s", type(exc).__name__)
        return None
    if not getattr(response, "success", False):
        logger.warning("PRIVATE_MESSAGE_TRACE stage=ai_unsuccessful")
        return None
    text = getattr(response, "text", None)
    return text.strip() if isinstance(text, str) and text.strip() else None


async def _generate_reply_body(owner_id: int, incoming_text: str) -> str | None:
    """The deliverable body for ONE incoming message, or None (safe failure).

    ONE provider call is always made first — the context isolation and the
    ProviderManager boundary are unchanged. Then:

    * a body that passes the deterministic envelope is delivered as-is;
    * a body for a CONFIDENTLY recognized bare greeting that is refused OR
      longer than ``MAX_TRIVIAL_GREETING_REPLY_LENGTH`` is replaced by the
      deterministic short Persian greeting — a greeting always gets a greeting,
      never an elaborate self-describing paragraph;
    * any other refused body gets exactly ONE regeneration through the SAME
      provider path with the repair instruction, and is then dropped if it is
      still refused. A rejected body is never sent, and a provider failure is
      never papered over with canned text.
    """
    greeting = service.classify_trivial_greeting(incoming_text)
    for attempt in range(service.AI_GENERATION_ATTEMPTS):
        generated = await _generate_ai_reply(owner_id, incoming_text, attempt > 0)
        if generated is None:
            return None
        reason, text = service.validate_generated_reply(generated)
        if reason:
            logger.warning(
                "PRIVATE_MESSAGE_TRACE stage=ai_reply_rejected reason=%s attempt=%s",
                reason, attempt + 1,
            )
            # A bare greeting is answered deterministically, so a refused body
            # for one never spends a second provider call.
            if greeting is not None:
                break
            continue
        if greeting is not None and len(text) > service.MAX_TRIVIAL_GREETING_REPLY_LENGTH:
            logger.info(
                "PRIVATE_MESSAGE_TRACE stage=ai_greeting_too_elaborate chars=%s", len(text)
            )
            break
        return service.compose_ai_reply(generated)
    if greeting is not None:
        return service.compose_trivial_greeting_reply(greeting)
    return None


async def _ai_response(client, owner_id: int, event, verdict, config: dict) -> str:
    """Claim → generate → send, giving the claim back when nothing was sent.

    The allowance is claimed by ONE atomic conditional write BEFORE the model is
    asked anything, so two concurrent (or duplicate) updates can never spend the
    same reply; a generation, length or delivery failure releases the exact
    claimed pair, so only a DELIVERED reply consumes the person's allowance.
    """
    sender_id = verdict.sender_id
    async with service.sender_lock(owner_id, sender_id):
        claim = await service.claim_ai_reply(owner_id, sender_id, config)
        if not isinstance(claim, dict) or not claim.get("ok"):
            logger.warning(
                "PRIVATE_MESSAGE_TRACE stage=ai_counter_unavailable sender_id=%s reason=%s",
                sender_id, (claim or {}).get("reason"),
            )
            return "counter_unavailable"
        if not claim.get("claimed"):
            logger.info(
                "PRIVATE_MESSAGE_TRACE stage=ai_denied reason=%s used=%s sender_id=%s",
                claim.get("reason"), claim.get("used"), sender_id,
            )
            return str(claim.get("reason") or "denied")
        token = claim.get("token") or {}
        body = await _generate_reply_body(owner_id, _incoming_text(event))
        if body is None:
            await service.release_ai_reply(owner_id, sender_id, token)
            return "generation_failed"
        if not await _send_text(client, verdict.chat_id, body, "ai_response"):
            await service.release_ai_reply(owner_id, sender_id, token)
            return "send_failed"
        return "sent"


async def _apply_actions(client, owner_id: int, event, verdict, decision) -> None:
    """The deterministic action order: delete, fixed response, AI response.

    Each step is independent: a delete failure never suppresses a response (it
    is logged), a fixed-response failure never suppresses the AI reply, and only
    a DELIVERED AI reply consumes the person's allowance.
    """
    if decision.delete:
        await _delete_incoming(client, verdict.chat_id, verdict.message_id)
    if decision.fixed_response and decision.fixed_response_text:
        await _send_text(
            client, verdict.chat_id, decision.fixed_response_text, "fixed_response"
        )
    if decision.ai_response:
        if _incoming_text(event):
            await _ai_response(client, owner_id, event, verdict, service.state())
        else:
            logger.info(
                "PRIVATE_MESSAGE_TRACE stage=ai_skipped_no_text sender_id=%s", verdict.sender_id
            )


async def enforce_incoming(client, event, owner_id: int) -> bool:
    """Apply the configured policy to ONE incoming update.

    Returns ``True`` when the message may continue into the existing handlers,
    ``False`` when it was handled as unauthorized (so nothing else processes
    it). Never raises into the Telegram event path: an internal failure leaves
    the message permitted, which is exactly the behaviour that existed before
    this feature.

    The scope gate runs FIRST and fails closed: unless the event is a genuine
    human user in a direct private chat with the owner's account, this function
    returns before any action — no delete, no fixed response, no AI request, no
    counter, no enforcement record. Nothing in the configuration can widen that
    boundary (it is not configurable).

    Exactly one enforcement per verified incoming message: the (chat, message)
    pair is marked before the actions run, so a Telegram redelivery, a duplicate
    event or a concurrent second update of the SAME message cannot repeat the
    actions or the AI reply. The mark is forgotten again only when the actions
    never ran (an unexpected failure), so a message the feature did not actually
    handle is still enforceable on redelivery.
    """
    mark: tuple[int, int] | None = None
    try:
        if getattr(event, "out", None) is True:
            return True

        # The persisted policy is resolved before the FIRST decision, so a
        # restart never evaluates a message against the in-memory default.
        await service.ensure_loaded_async()

        verdict = await resolve_scope(event, owner_id)
        if not verdict.eligible:
            logger.debug(
                "PRIVATE_MESSAGE_TRACE stage=out_of_scope reason=%s", verdict.reason
            )
            return True

        decision = service.decide_incoming(
            service.state(),
            sender_id=verdict.sender_id,
            is_private=True,
            is_owner=verdict.sender_id == owner_id,
            now=datetime.now(timezone.utc),
        )
        if decision.permitted:
            return True
        # ONE mark per verified (chat, message) pair, registered BEFORE the
        # actions so a redelivery or a concurrent second update can never run
        # the delete/fixed/AI steps twice. The check is synchronous, so two
        # coroutines racing on the same update cannot both pass it.
        if service.already_handled(verdict.chat_id, verdict.message_id):
            logger.debug(
                "PRIVATE_MESSAGE_TRACE stage=duplicate sender_id=%s message_id=%s",
                verdict.sender_id, verdict.message_id,
            )
            return False

        logger.info(
            "PRIVATE_MESSAGE_TRACE stage=unauthorized mode=%s source=%s "
            "actions=delete:%s,fixed:%s,ai:%s sender_id=%s chat_id=%s",
            decision.mode, decision.source, decision.delete,
            decision.fixed_response, decision.ai_response,
            verdict.sender_id, verdict.chat_id,
        )
        mark = (verdict.chat_id, verdict.message_id)
        await _apply_actions(client, owner_id, event, verdict, decision)
        # The message WAS handled: the mark stays, so its redelivery stays a
        # no-op. Only a failure that skipped the actions forgets it.
        mark = None
        return False
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — never poison the event path
        if mark is not None:
            service.release_handled(*mark)
        logger.warning(
            "PRIVATE_MESSAGE_TRACE stage=handler_error exception=%s", type(exc).__name__,
            exc_info=logger.isEnabledFor(logging.DEBUG),
        )
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


def _reset_window_text(summary) -> str:
    """The automatic-reset configuration as the panel states it."""
    if not summary.ai_counter_reset_enabled:
        return "OFF (counters only change when you reset them)"
    return (
        f"every {service.format_reset_interval(summary.ai_counter_reset_seconds)}"
        f" per person ({summary.ai_counter_reset_seconds}s)"
    )


def _mode_button_row(builder: InlinePanelBuilder, current_mode: str) -> None:
    for mode in service.MODES:
        builder.add_row(
            f"{_mark(mode == current_mode)} {service.MODE_LABELS[mode]}",
            f"action:pm_mode:{mode}",
            PRIMARY,
        )


async def _counter_line(owner_id: int) -> str:
    """The durable per-person AI counters, or an honest "unavailable"."""
    try:
        rows = await service.counter_rows(owner_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        rows = None
    if rows is None:
        return "unavailable (the counter store could not be read)"
    used = 0
    for row in rows:
        value = row.get("ai_replies_used") if isinstance(row, dict) else None
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            used += value
    return f"{used} AI reply(ies) used by {len(rows)} person(s)"


async def _render_main(owner_id: int) -> tuple[str, str, list]:
    now = datetime.now(timezone.utc)
    state_doc = service.state()
    summary = service.summarize(state_doc, now)
    counters = await _counter_line(owner_id)
    limit_text = (
        "unlimited"
        if summary.ai_unlimited_replies
        else str(summary.ai_max_replies_per_person)
    )

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
        f"**Enforcement:** {'ON' if summary.enabled else 'OFF (no action applied)'}",
        f"**Closing actions:** {_mark(summary.delete_incoming)} delete incoming "
        f"· {_mark(summary.fixed_response_enabled)} fixed response "
        f"· {_mark(summary.ai_response_enabled)} AI response",
        f"**AI replies per person:** {limit_text} (interval {summary.ai_interval_seconds}s)",
        f"**AI counters:** {counters}",
        f"**Automatic counter reset:** {_reset_window_text(summary)}",
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
    lines.append(
        "**Scope:** genuine human users in **direct private chats** only. Bot "
        "accounts, groups, supergroups, channels, channel posts and linked "
        "discussion groups are never affected."
    )

    builder = InlinePanelBuilder()
    _mode_button_row(builder, summary.mode)
    builder.add_buttons(
        (sym.label(sym.STATE_WARNING, "Closing actions"), f"panel:{POLICY_PANEL_ID}", PRIMARY),
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



async def _render_actions(owner_id: int) -> tuple[str, str, list]:
    """The INDEPENDENT closing actions and the AI-reply limits."""
    state_doc = service.state()
    summary = service.summarize(state_doc, datetime.now(timezone.utc))
    counters = await _counter_line(owner_id)
    limit_text = "unlimited" if summary.ai_unlimited_replies else str(summary.ai_max_replies_per_person)
    body = [
        "Every action below is independent: enable any combination. They are "
        "applied to an unauthorized message in this fixed order: **delete → "
        "fixed response → AI response**.",
        "",
        f"{_mark(summary.delete_incoming)} **Delete incoming message** — the Self Bot deletes the message it received.",
        f"{_mark(summary.fixed_response_enabled)} **Send fixed response** — the configured text below.",
        f"{_mark(summary.ai_response_enabled)} **Send AI response** — generated by AI, with the closure and AI disclosures.",
        "",
        f"**Fixed response text:** {summary.fixed_response_text or '_not set_ (nothing is sent while it is empty)'}",
        f"**Max AI replies per person:** {limit_text}",
        f"**AI reply interval (seconds):** {summary.ai_interval_seconds}",
        f"**Automatic counter reset:** {_reset_window_text(summary)}",
        f"**AI counters:** {counters}",
        "",
        "A delete failure never suppresses a response; a fixed-response failure "
        "never suppresses the AI reply; only a DELIVERED AI reply consumes the "
        "person's allowance.",
        "",
        "**Reset windows:** a window starts at a person's FIRST counted reply in "
        "the current window. When automatic reset is ON and the window has "
        "lasted the configured interval, the NEXT eligible message observes the "
        "expired window, resets that person's count to zero and starts a new "
        "window — so their allowance becomes available again on that message, "
        "and only then. Automatic reset only resets the COUNT: the maximum, the "
        "minimum interval between replies, the actions and the eligible-message "
        "scope are all unaffected.",
    ]

    builder = InlinePanelBuilder()
    for field_name in service.ACTION_FIELDS:
        builder.add_row(
            f"{_mark(bool(state_doc.get(field_name)))} {service.ACTION_LABELS[field_name]}",
            f"action:pm_action:{field_name}",
            PRIMARY,
        )
    builder.add_row(sym.label(sym.OP_EDIT, "Set fixed response text"), f"input:{POLICY_PANEL_ID}:fixed_text", PRIMARY)
    builder.add_row(
        sym.label(sym.ICON_SELECT, "Set max AI replies per person"),
        f"input:{POLICY_PANEL_ID}:max_replies",
        PRIMARY,
    )
    builder.add_row(
        sym.label(sym.ICON_SELECT, "Set AI reply interval"),
        f"input:{POLICY_PANEL_ID}:interval",
        PRIMARY,
    )
    builder.add_row(
        f"{_mark(summary.ai_unlimited_replies)} Unlimited AI replies",
        "action:pm_unlimited",
        PRIMARY,
    )
    builder.add_row(
        f"{_mark(summary.ai_counter_reset_enabled)} Automatic counter reset",
        "action:pm_reset_window",
        PRIMARY,
    )
    builder.add_row(
        sym.label(sym.SCHEDULE_INTERVAL, "Set counter reset interval"),
        f"input:{POLICY_PANEL_ID}:reset_interval",
        PRIMARY,
    )
    builder.add_row(sym.label(sym.ICON_STATS, "Reset AI counters"), "action:pm_reset_counters", DANGER)
    return "Closing Actions", "\n".join(body), builder.build()


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
        f"**Mode:** {_RULE_MODE_LABELS.get(draft['mode'], service.MODE_LABELS[draft['mode']])}",
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
        sym.label(
            sym.ICON_SELECT,
            f"Mode: {_RULE_MODE_LABELS.get(draft['mode'], draft['mode'])}",
        ),
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
        return await _render_actions(owner_id)
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


async def _action_toggle(event, extra: str, chat_id: int):
    field_name = (extra or "").strip()
    owner_id = inline_engine.get_owner_id()
    if field_name not in service.ACTION_FIELDS:
        return "Closing Actions", "That action is not valid — nothing changed.", []
    current = bool(service.state().get(field_name))
    if not await service.set_action(field_name, not current):
        return "Closing Actions", "Could not save the change. Please try again.", []
    return await _render_actions(owner_id)


async def _unlimited_action(event, extra: str, chat_id: int):
    owner_id = inline_engine.get_owner_id()
    current = bool(service.state().get("ai_unlimited_replies"))
    if not await service.set_ai_unlimited_replies(not current):
        return "Closing Actions", "Could not save the change. Please try again.", []
    return await _render_actions(owner_id)


async def _reset_counters_action(event, extra: str, chat_id: int):
    owner_id = inline_engine.get_owner_id()
    owner = service.owner_id() or owner_id
    if not await service.reset_counters(owner):
        return "Closing Actions", "The AI counters were NOT reset. Please try again.", []
    return await _render_actions(owner_id)


async def _reset_window_toggle_action(event, extra: str, chat_id: int):
    """Enable/disable the AUTOMATIC per-person reset (the manual reset is
    separate and stays available either way)."""
    owner_id = inline_engine.get_owner_id()
    current = bool(service.state().get("ai_counter_reset_enabled"))
    if not await service.set_ai_counter_reset_enabled(not current):
        return "Closing Actions", "Could not save the change. Please try again.", []
    return await _render_actions(owner_id)


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


async def _fixed_text_input_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    owner_id = inline_engine.get_owner_id()
    cleaned = (text or "").strip()
    if not cleaned:
        notice = "The fixed response text cannot be empty."
    elif len(cleaned) > service.MAX_FIXED_RESPONSE_LENGTH:
        notice = f"The fixed response text must be at most {service.MAX_FIXED_RESPONSE_LENGTH} characters."
    else:
        notice = "" if await service.set_fixed_response_text(cleaned) else "Could not save the text. Please try again."
    await _show_after_input(POLICY_PANEL_ID, owner_id, notice, chat_id, msg_id, inline_chat_id, inline_msg_id)


def _int_input_handler(setter, label: str, low: int, high: int):
    async def _handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
        owner_id = inline_engine.get_owner_id()
        raw = (text or "").strip()
        value = int(raw) if raw.isdigit() else None
        if value is None or not (low <= value <= high):
            notice = f"{label} was NOT saved — enter a whole number between {low} and {high}."
        else:
            notice = "" if await setter(value) else f"Could not save {label}. Please try again."
        await _show_after_input(
            POLICY_PANEL_ID, owner_id, notice, chat_id, msg_id, inline_chat_id, inline_msg_id
        )

    return _handler


_max_replies_input_handler = _int_input_handler(
    service.set_ai_max_replies_per_person,
    "Max AI replies per person",
    1,
    service.MAX_AI_REPLIES_PER_PERSON,
)
_interval_input_handler = _int_input_handler(
    service.set_ai_interval_seconds,
    "AI reply interval",
    0,
    service.MAX_AI_INTERVAL_SECONDS,
)


async def _counter_reset_interval_input_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    owner_id = inline_engine.get_owner_id()
    seconds = service.parse_reset_interval((text or "").strip())
    if seconds is None:
        notice = (
            "Counter reset interval was NOT saved — enter a whole number of "
            "minutes, or a number with a unit (90s, 30m, 2h, 3d). Allowed: "
            f"{service.MIN_AI_COUNTER_RESET_SECONDS}s – "
            f"{service.format_reset_interval(service.MAX_AI_COUNTER_RESET_SECONDS)}."
        )
    else:
        notice = (
            ""
            if await service.set_ai_counter_reset_seconds(seconds)
            else "Could not save the counter reset interval. Please try again."
        )
    await _show_after_input(
        POLICY_PANEL_ID, owner_id, notice, chat_id, msg_id, inline_chat_id, inline_msg_id
    )


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
    # Bind the owner whose dedicated configuration and counters this feature
    # reads and writes (the service persists through it).
    service.configure_owner(owner_id)

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

    register_panel(PANEL_ID, lambda event, extra: _panel_result(PANEL_ID, extra, owner_id), parent="cipher", title="Private Messages")
    register_panel(POLICY_PANEL_ID, lambda event, extra: _panel_result(POLICY_PANEL_ID, extra, owner_id), parent=PANEL_ID, title="Closing Actions")
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
    register_action("pm_action", _action_toggle)
    register_action("pm_unlimited", _unlimited_action)
    register_action("pm_reset_window", _reset_window_toggle_action)
    register_action("pm_reset_counters", _reset_counters_action)
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

    register_input(POLICY_PANEL_ID, "fixed_text", {
        "handler": _fixed_text_input_handler,
        "prompt": "**Fixed response text**\n\nEnter the message a human sender receives while this policy is closed:\n\n_Reply below._",
    })
    register_input(POLICY_PANEL_ID, "max_replies", {
        "handler": _max_replies_input_handler,
        "prompt": (
            "**Max AI replies per person**\n\nEnter a whole number between 1 and "
            f"{service.MAX_AI_REPLIES_PER_PERSON}:\n\n_Reply below._"
        ),
    })
    register_input(POLICY_PANEL_ID, "interval", {
        "handler": _interval_input_handler,
        "prompt": (
            "**AI reply interval**\n\nMinimum seconds between two AI replies to the "
            "same person. Enter 0 for no delay:\n\n_Reply below._"
        ),
    })
    register_input(POLICY_PANEL_ID, "reset_interval", {
        "handler": _counter_reset_interval_input_handler,
        "prompt": (
            "**Counter reset interval**\n\nHow long one person's reset window "
            "lasts. Enter a whole number of minutes, or a number with a unit:\n"
            "`90s` · `30m` · `2h` · `3d`\n\n_Reply below._"
        ),
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
