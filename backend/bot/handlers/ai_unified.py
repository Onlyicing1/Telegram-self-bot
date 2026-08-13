"""
Unified AI activation handler — supports trigger mode and reply mode.

TRIGGER MODE:
  Owner sends "Nova Hello" → trigger word "Nova" is stripped →
  prompt becomes "Hello".

REPLY MODE:
  Owner replies to any message and sends the trigger word.

  When replying to an AI message:
    - The FULL previous AI response is injected as reply CONTEXT.
    - The user's new text (after the trigger word) is the ACTUAL user message.
    - The old AI response is NEVER used as the new user message.

  If the owner replies with only the trigger word (no extra text):
    - The replied-to AI message is still CONTEXT only.
    - The user message becomes a generic continuation prompt
      (e.g. "Continue" or "Tell me more about the above").

Both modes enter the SAME execution pipeline:
  1. Build AIRequest with appropriate reply_context
  2. Edit the triggering message to show "Thinking..."
  3. Execute through engine.execute()
  4. Edit the message with the final response

Edit-in-place UX — no second messages are ever sent.
"""
import asyncio
import logging
import time

from telethon import events

from backend.bot.handlers.guard import is_owner
from backend.diagnostics import record_event
from backend.runtime.tracer import trace
from backend.ai import diagnostics as ai_diag

logger = logging.getLogger(__name__)

_engine = None
_owner_id: int = 0
_tz_str: str = "UTC"
_trigger_cache: dict[str, str] = {"en": "", "fa": "", "ts": 0.0}
_CACHE_TTL = 30.0
_AI_TIMEOUT = 60.0


def configure(engine, owner_id: int, tz_str: str) -> None:
    global _engine, _owner_id, _tz_str
    _engine = engine
    _owner_id = owner_id
    _tz_str = tz_str


def _get_engine():
    global _engine
    if _engine is not None:
        return _engine
    try:
        from backend.ai.engine.engine import get_engine
        _engine = get_engine()
        return _engine
    except Exception as exc:
        logger.error("AI handler: could not get engine: %s", exc, exc_info=True)
        return None


async def _load_triggers(owner_id: int) -> tuple[str, str]:
    now = time.monotonic()
    if (now - _trigger_cache["ts"]) < _CACHE_TTL and _trigger_cache["en"] is not None:
        return _trigger_cache["en"], _trigger_cache["fa"]
    try:
        from backend.ai.config_store import get_triggers
        triggers = await get_triggers(owner_id)
        en = triggers.get("trigger_en", "") or ""
        fa = triggers.get("trigger_fa", "") or ""
        _trigger_cache["en"] = en
        _trigger_cache["fa"] = fa
        _trigger_cache["ts"] = now
        return en, fa
    except Exception as exc:
        logger.warning("AI handler: failed to load triggers: %s", exc)
        return "", ""


async def _restore_config(owner_id: int) -> None:
    try:
        from backend.ai.config_store import get_config
        config = await get_config(owner_id)
        provider = config.get("provider", "")
        model = config.get("model", "")

        engine = _get_engine()
        if engine and provider:
            if engine.provider_manager.registry.has(provider):
                engine.provider_manager.switch_provider(provider)
                if model:
                    pconfig = engine.provider_manager.get_provider_config(provider)
                    pconfig.default_model = model

        if engine:
            try:
                engine.conversation_manager.set_system_prompt(
                    owner_id,
                    config.get("system_prompt", "") or "You are LifeOS Assistant.",
                )
            except Exception as exc:
                logger.warning("AI handler: set_system_prompt failed: %s", exc)
    except Exception as exc:
        logger.warning("AI handler: config restore failed: %s", exc)


def _format_thinking(user_message: str, trigger_label: str) -> str:
    return (
        f"{user_message}\n"
        f"────────────\n"
        f"🤖 {trigger_label}\n"
        f"⏳ Thinking..."
    )


def _format_response(user_message: str, trigger_label: str, response: str) -> str:
    return (
        f"{user_message}\n"
        f"────────────\n"
        f"🤖 {trigger_label}\n"
        f"{response}"
    )


def _format_error(user_message: str, trigger_label: str, error: str) -> str:
    return (
        f"{user_message}\n"
        f"────────────\n"
        f"🤖 {trigger_label}\n"
        f"❌ Error\n"
        f"{error}"
    )


def _truncate(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _humanize_error(error: str) -> str:
    error_lower = error.lower()
    if "401" in error_lower or "unauthorized" in error_lower or "invalid api key" in error_lower:
        return "Invalid API key. Check your provider configuration."
    if "429" in error_lower or "rate" in error_lower:
        return "Rate limited. Please wait and try again."
    if "timeout" in error_lower or "timed out" in error_lower:
        return "Request timed out. The provider took too long to respond."
    if "404" in error_lower or "not found" in error_lower or "model" in error_lower:
        return "Model not found. Check your model selection."
    if "connection" in error_lower or "network" in error_lower or "dns" in error_lower:
        return "Provider unavailable. Network error reaching the API."
    return error[:200] if error else "Unknown error."


async def _extract_reply_context(event, client, user_text: str) -> tuple[str, "ReplyContext", str]:
    """Extract reply context from a replied-to message.

    The replied-to message is ALWAYS treated as CONTEXT — never as the
    user's new message.  The user's actual instruction (``user_text``)
    is the prompt that goes to the AI.

    When replying to a known AI message, the full untruncated AI response
    is injected via ``ReplyContext.ai_content`` so the Prompt Builder can
    include it as high-priority context.

    Returns (user_message, reply_context, error_message).
    On success, error_message is empty. On failure, user_message is empty.
    """
    from backend.ai.conversation.context_builder import ReplyContext
    from backend.ai.media import classify_message

    reply_msg = None
    try:
        reply_msg = await event.get_reply_message()
    except Exception as exc:
        logger.warning("AI handler: could not fetch reply message: %s", exc)
        return "", ReplyContext(), f"Could not read the replied message: {exc}"

    if reply_msg is None:
        return "", ReplyContext(), "No replied message found. Reply to a message first."

    # ── Classify media ──
    media_info = classify_message(reply_msg)

    # ── Extract sender info ──
    sender_name = ""
    sender_id = 0
    try:
        sender = await reply_msg.get_sender()
        if sender:
            sender_id = getattr(sender, "id", 0) or 0
            first = getattr(sender, "first_name", "") or ""
            last = getattr(sender, "last_name", "") or ""
            sender_name = (f"{first} {last}").strip() or getattr(sender, "username", "") or ""
    except Exception:
        pass

    # ── Chat info ──
    chat_title = ""
    chat_id = 0
    try:
        chat_id = reply_msg.chat_id or 0
        chat = await reply_msg.get_chat()
        if chat:
            chat_title = getattr(chat, "title", "") or getattr(chat, "username", "") or ""
    except Exception:
        pass

    # ── Timestamp ──
    msg_timestamp = ""
    try:
        from datetime import timezone
        dt = reply_msg.date
        if dt:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            msg_timestamp = dt.isoformat()
    except Exception:
        pass

    # ── Resolve AI message if the replied-to message is a known AI response ──
    from backend.ai.context.reply_resolver import get_resolver

    resolved = get_resolver().resolve(reply_msg.id or 0)

    # ── Determine the user's actual message ──
    # The user_text is what the owner typed after the trigger word.
    # If they only sent the trigger word with no extra text, use a
    # generic continuation prompt — the replied-to message is context,
    # NOT the user's instruction.
    if user_text:
        user_message = user_text
    elif resolved and resolved.content:
        user_message = "Continue. Tell me more about the above."
    elif media_info.is_text and (media_info.text or media_info.caption):
        user_message = "Continue. Tell me more about the above."
    else:
        user_message = "Continue. Tell me more about the above."

    # ── Build reply context ──
    # The replied-to message content goes into ReplyContext, NOT into
    # the user_message.  The Prompt Builder reads ai_content / text_preview
    # from the ReplyContext and injects it as context.
    ai_content = resolved.content if resolved else ""

    reply_ctx = ReplyContext(
        exists=True,
        message_id=reply_msg.id or 0,
        sender_id=sender_id,
        sender_name=sender_name,
        chat_id=chat_id,
        chat_title=chat_title,
        media_type=media_info.media_type,
        text_preview=(media_info.text or media_info.caption or "")[:200],
        timestamp=msg_timestamp,
        is_ai_message=resolved is not None,
        ai_session_id=resolved.session_id if resolved else "",
        ai_role=resolved.role if resolved else "",
        ai_content=ai_content,
        ai_provider=resolved.provider if resolved else "",
        ai_model=resolved.model if resolved else "",
        ai_timestamp=resolved.timestamp if resolved else "",
    )

    return user_message, reply_ctx, ""


async def _execute_ai(event, owner_id: int, prompt_text: str, trigger_word: str,
                      tz_str: str, reply_context=None) -> None:
    """Execute the AI pipeline and edit the triggering message with the result."""
    from backend.ai.session.request import AIRequest
    from backend.ai.conversation.context_builder import ReplyContext

    engine = _get_engine()
    if engine is None:
        try:
            await event.edit(_format_error(prompt_text, trigger_word, "AI engine not available."))
        except Exception as exc:
            logger.error("AI handler: failed to edit error state (no engine): %s", exc)
        return

    trigger_label = trigger_word
    display_prompt = prompt_text

    rid = ai_diag.new_request_id()
    ai_diag.register_start(rid, owner_id=owner_id)
    logger.info("AI_REQUEST_START id=%s owner=%d mode=trigger", rid, owner_id)

    ai_diag.set_stage(rid, "CONFIG_LOAD")
    logger.info("AI_CONFIG_LOAD_START id=%s", rid)
    await _restore_config(owner_id)
    ai_diag.mark_success("CONFIG_LOAD")
    logger.info("AI_CONFIG_LOAD_END id=%s", rid)

    session_id = f"owner-{owner_id}"
    request = AIRequest(
        session_id=session_id,
        user_message=prompt_text,
        owner_id=owner_id,
        chat_id=event.chat_id,
        message_id=event.message.id,
        reply_context=reply_context or ReplyContext(),
        timezone=tz_str,
        request_id=rid,
    )

    try:
        await event.edit(_format_thinking(display_prompt, trigger_label))
    except Exception as exc:
        logger.warning("AI handler: failed to edit thinking state: %s", exc)

    try:
        result = await asyncio.wait_for(
            engine.execute(request),
            timeout=_AI_TIMEOUT,
        )
        record_event("ai", "execute", 0, "SUCCESS" if result.success else "FAILED",
                     f"provider={result.provider}")

        if result.success:
            try:
                from backend.ai.config_store import record_request
                ai_diag.set_stage(rid, "DB_OPERATION")
                logger.info("AI_DB_OPERATION_START id=%s", rid)
                await record_request(owner_id, result.latency * 1000)
                ai_diag.mark_success("DB_OPERATION")
                logger.info("AI_DB_OPERATION_END id=%s", rid)
            except Exception as exc:
                logger.warning("AI handler: record_request failed: %s", exc)

        if result.success and result.response:
            response_text = _truncate(result.response)
            final_text = _format_response(display_prompt, trigger_label, response_text)
        elif result.errors:
            error_msg = _humanize_error(result.errors[0])
            final_text = _format_error(display_prompt, trigger_label, error_msg)
        else:
            final_text = _format_error(display_prompt, trigger_label, "AI returned no response.")

        ai_diag.set_stage(rid, "TELEGRAM_REPLY")
        logger.info("AI_TELEGRAM_REPLY_START id=%s", rid)
        try:
            await event.edit(final_text)
            ai_diag.mark_success("TELEGRAM_REPLY")
            logger.info("AI_TELEGRAM_REPLY_END id=%s", rid)
            if result.success and result.response:
                from backend.ai.context.reply_resolver import get_resolver
                get_resolver().register(
                    telegram_msg_id=event.message.id,
                    session_id=session_id,
                    role="assistant",
                    content=result.response,
                    provider=result.provider,
                    model=result.model,
                )
        except Exception as exc:
            logger.warning("AI handler: failed to edit final response: %s", exc)
            try:
                await event.reply(final_text)
                ai_diag.mark_success("TELEGRAM_REPLY")
                logger.info("AI_TELEGRAM_REPLY_END id=%s (via reply)", rid)
            except Exception as exc2:
                logger.error("AI handler: both edit and reply failed: %s", exc2)

    except asyncio.TimeoutError:
        ai_diag.register_end(rid)
        trace("AI_TRIGGER_TIMEOUT", owner_id=owner_id, timeout=f"{_AI_TIMEOUT}s", rid=rid)
        logger.error("AI handler: request timed out after %ss", _AI_TIMEOUT)
        error_text = _format_error(
            display_prompt, trigger_label,
            f"Request timed out after {int(_AI_TIMEOUT)} seconds.",
        )
        try:
            await event.edit(error_text)
        except Exception as exc:
            logger.error("AI handler: failed to edit timeout error: %s", exc)

    except asyncio.CancelledError:
        ai_diag.register_end(rid)
        raise

    except Exception as exc:
        ai_diag.register_end(rid)
        logger.exception("AI handler error: %s", exc)
        trace("AI_HANDLER_ERROR", error=str(exc))
        error_text = _format_error(display_prompt, trigger_label, _humanize_error(str(exc)))
        try:
            await event.edit(error_text)
        except Exception as edit_exc:
            logger.error("AI handler: failed to edit error state: %s", edit_exc)


def register(client, owner_id: int, tz_str: str):
    """Register the unified AI activation handler.

    This handler fires on ALL outgoing messages. It detects two activation
    methods:

    METHOD 1 — Trigger Mode (no reply):
      Owner sends "Nova Hello" → trigger "Nova" stripped → prompt = "Hello"
      No reply context is extracted.

    METHOD 2 — Reply-Aware Trigger Mode (message is a reply):
      Owner replies to any message and sends the trigger word, optionally
      with extra text.

      When replying to an AI message:
        - The FULL previous AI response is injected as reply CONTEXT.
        - The user's new text (after the trigger) is the ACTUAL user message.
        - If no extra text, a generic continuation prompt is used.
        - The old AI response is NEVER used as the new user message.

      When replying to a non-AI message:
        - The replied message content is injected as reply context.
        - The user's new text is the user message (or a continuation prompt).

    Messages starting with "." (dot commands) are always skipped.
    """

    @client.on(events.NewMessage(outgoing=True))
    async def ai_unified_handler(event):
        if not is_owner(event, owner_id):
            return

        raw_text = event.raw_text or ""
        if not raw_text:
            return

        if raw_text.startswith("."):
            return

        words = raw_text.split(None, 1)
        if not words:
            return

        first_word = words[0]

        trigger_en, trigger_fa = await _load_triggers(owner_id)
        if not trigger_en and not trigger_fa:
            return

        from backend.ai.config_store import match_trigger
        if not match_trigger(first_word, trigger_en, trigger_fa):
            return

        remaining = words[1].strip() if len(words) > 1 else ""
        is_reply = bool(getattr(event, "is_reply", False))

        # ── Reply-Aware Mode: message is a reply ──
        # ALWAYS extract reply context when the message is a reply,
        # regardless of whether the user wrote extra text.  This ensures
        # the AI receives the replied-to message as context.
        if is_reply:
            trace("AI_TRIGGER_MATCHED", trigger=first_word, mode="reply")
            user_message, reply_ctx, error_msg = await _extract_reply_context(
                event, client, remaining
            )

            if error_msg:
                try:
                    await event.edit(_format_error(first_word, first_word, error_msg))
                except Exception as exc:
                    logger.warning("AI handler: failed to edit reply error: %s", exc)
                return

            await _execute_ai(
                event, owner_id, user_message, first_word, tz_str,
                reply_context=reply_ctx,
            )
            return

        # ── Trigger Mode: no reply, must have remaining text ──
        if not remaining:
            # Only the trigger word with no text and no reply — ignore silently
            return

        trace("AI_TRIGGER_MATCHED", trigger=first_word, mode="trigger")
        await _execute_ai(event, owner_id, remaining, first_word, tz_str)
