"""
Trigger-based AI conversation handler.

Replaces the old `.ai` command with configurable trigger words.
When the owner sends an outgoing message whose first word matches
either the English trigger (case-insensitive) or the Persian trigger
(exact match), the AI subsystem activates:

  1. Loads the owner's trigger config from Supabase
  2. Matches the first word against trigger_en / trigger_fa
  3. Strips the trigger word from the message
  4. Edits the triggering message to show the stripped message + thinking indicator
  5. Restores the saved provider/model from Supabase
  6. Builds an AIRequest with the stripped message
  7. Executes the request through the full AI pipeline (with 60s timeout)
  8. Edits the same message with the AI response (edit-in-place, zero spam)
  9. Records request latency in Supabase

No second messages are ever sent. Everything happens inside the same
Telegram message that the owner originally sent.
"""
import asyncio
import logging
import time

from telethon import events

from backend.bot.handlers.guard import is_owner
from backend.diagnostics import record_event
from backend.runtime.tracer import trace

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
        logger.error("AI trigger handler: could not get engine: %s", exc, exc_info=True)
        return None


async def _load_triggers(owner_id: int) -> tuple[str, str]:
    """Load trigger words from Supabase with a short in-memory cache."""
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
        logger.warning("AI trigger handler: failed to load triggers: %s", exc)
        return "", ""


async def _restore_config(owner_id: int) -> None:
    """Restore saved provider/model from Supabase and apply to the engine."""
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
                logger.warning("AI trigger handler: set_system_prompt failed: %s", exc)
    except Exception as exc:
        logger.warning("AI trigger handler: config restore failed: %s", exc)


def _format_thinking(user_message: str, trigger_label: str) -> str:
    """Format the thinking state message."""
    return (
        f"{user_message}\n"
        f"────────────\n"
        f"🤖 {trigger_label}\n"
        f"⏳ Thinking..."
    )


def _format_response(user_message: str, trigger_label: str, response: str) -> str:
    """Format the final response message."""
    return (
        f"{user_message}\n"
        f"────────────\n"
        f"🤖 {trigger_label}\n"
        f"{response}"
    )


def _format_error(user_message: str, trigger_label: str, error: str) -> str:
    """Format the error state message."""
    return (
        f"{user_message}\n"
        f"────────────\n"
        f"🤖 {trigger_label}\n"
        f"❌ Error\n"
        f"{error}"
    )


def _humanize_error(error: str) -> str:
    """Convert raw error strings into human-readable messages."""
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


async def _execute_ai(event, owner_id: int, user_message: str, trigger_word: str, tz_str: str) -> None:
    """Execute the AI pipeline and edit the triggering message with the result.

    This is the core of the edit-in-place conversation UX:
      1. Edit the message to show the stripped message + thinking indicator
      2. Execute the AI request with a 60s timeout
      3. On success: edit the message with the response
      4. On failure: edit the message with a human-readable error
      5. On timeout: edit the message with a timeout error

    The message is never left in the "thinking" state.
    """
    engine = _get_engine()
    if engine is None:
        try:
            await event.edit(_format_error(user_message, trigger_word, "AI engine not available."))
        except Exception as exc:
            logger.error("AI trigger: failed to edit error state (no engine): %s", exc)
        return

    trigger_label = trigger_word

    await _restore_config(owner_id)

    from backend.ai.session.request import AIRequest

    session_id = f"owner-{owner_id}"
    request = AIRequest(
        session_id=session_id,
        user_message=user_message,
        owner_id=owner_id,
        chat_id=event.chat_id,
        message_id=event.message.id,
        timezone=tz_str,
    )

    try:
        await event.edit(_format_thinking(user_message, trigger_label))
    except Exception as exc:
        logger.warning("AI trigger: failed to edit thinking state: %s", exc)

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
                await record_request(owner_id, result.latency * 1000)
            except Exception as exc:
                logger.warning("AI trigger: record_request failed: %s", exc)

        if result.success and result.response:
            from backend.ai.tools.delivery import deliver_response
            delivery_result = await deliver_response(
                event, user_message, trigger_label, result.response,
            )
            if delivery_result.success:
                logger.info(
                    "AI trigger: response delivered chunks=%d/%d",
                    delivery_result.chunks_delivered, delivery_result.total_chunks,
                )
            else:
                logger.warning(
                    "AI trigger: delivery failed: %s",
                    delivery_result.error,
                )
        elif result.errors:
            error_msg = _humanize_error(result.errors[0])
            final_text = _format_error(user_message, trigger_label, error_msg)
            try:
                await event.edit(final_text)
            except Exception as exc:
                logger.warning("AI trigger: failed to edit error response: %s", exc)
                try:
                    await event.reply(final_text)
                except Exception:
                    pass
        elif result.response:
            error_msg = _humanize_error(result.response)
            final_text = _format_error(user_message, trigger_label, error_msg)
            try:
                await event.edit(final_text)
            except Exception as exc:
                logger.warning("AI trigger: failed to edit provider error: %s", exc)
                try:
                    await event.reply(final_text)
                except Exception:
                    pass
        else:
            final_text = _format_error(user_message, trigger_label, "AI returned no response.")
            try:
                await event.edit(final_text)
            except Exception:
                pass

    except asyncio.TimeoutError:
        trace("AI_TRIGGER_TIMEOUT", owner_id=owner_id, timeout=f"{_AI_TIMEOUT}s")
        logger.error("AI trigger: request timed out after %ss", _AI_TIMEOUT)
        error_text = _format_error(
            user_message, trigger_label,
            f"Request timed out after {int(_AI_TIMEOUT)} seconds.",
        )
        try:
            await event.edit(error_text)
        except Exception as exc:
            logger.error("AI trigger: failed to edit timeout error: %s", exc)

    except asyncio.CancelledError:
        raise

    except Exception as exc:
        logger.exception("AI trigger handler error: %s", exc)
        trace("AI_TRIGGER_HANDLER_ERROR", error=str(exc))
        error_text = _format_error(user_message, trigger_label, _humanize_error(str(exc)))
        try:
            await event.edit(error_text)
        except Exception as edit_exc:
            logger.error("AI trigger: failed to edit error state: %s", edit_exc)


def register(client, owner_id: int, tz_str: str):
    """Register the trigger-based AI handler on outgoing messages.

    This handler fires on ALL outgoing messages. It checks the first word
    against the configured triggers. If no trigger matches, the message
    passes through untouched. Messages starting with '.' (dot commands)
    are always skipped so existing commands continue to work.
    """

    @client.on(events.NewMessage(outgoing=True))
    async def ai_trigger_handler(event):
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
        remaining = words[1].strip() if len(words) > 1 else ""

        if not remaining:
            return

        trigger_en, trigger_fa = await _load_triggers(owner_id)

        if not trigger_en and not trigger_fa:
            return

        from backend.ai.config_store import match_trigger
        if not match_trigger(first_word, trigger_en, trigger_fa):
            return

        trace("AI_TRIGGER_MATCHED", trigger=first_word, en=trigger_en, fa=trigger_fa)
        await _execute_ai(event, owner_id, remaining, first_word, tz_str)
