"""
.ai <text> — DEPRECATED AI conversation handler.

This handler is kept for backward compatibility only. The primary
AI activation method is now the trigger-based system in
``backend.bot.handlers.ai_trigger``.

When the owner sends `.ai <message>`, the handler:
  1. Restores the saved provider/model from Supabase (auto-restore)
  2. Gets or creates an AI session for the owner
  3. Edits the triggering message to show the message + thinking indicator
  4. Builds an AIRequest with the user's message
  5. Executes the request through the full AI pipeline (with 60s timeout)
  6. Edits the same message with the AI response (edit-in-place, zero spam)
  7. Records request latency in Supabase

Falls back to plain-text edit-in-place (zero-spam policy).
"""
import asyncio
import logging

from telethon import events

from backend.bot.handlers.guard import is_owner
from backend.diagnostics import record_event
from backend.runtime.tracer import trace
from backend.ai import diagnostics as ai_diag

logger = logging.getLogger(__name__)

_engine = None
_owner_id: int = 0
_tz_str: str = "UTC"

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
                logger.warning("AI handler: set_system_prompt failed: %s", exc)
    except Exception as exc:
        logger.warning("AI handler: config restore failed: %s", exc)


def _truncate(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


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


def register(client, owner_id: int, tz_str: str):

    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.ai(?:\s+(.+))?$"))
    async def ai_cmd(event):
        if not is_owner(event, owner_id):
            return

        raw_text = event.raw_text or ""
        match = raw_text.split(None, 1)
        user_message = match[1].strip() if len(match) > 1 else ""

        if not user_message:
            try:
                await event.edit(
                    "🧠 **AI Assistant**\n\n"
                    "Usage: `.ai <message>`\n\n"
                    "Example: `.ai Hello, how are you?`\n\n"
                    "I can help you save messages, manage your bio/username, "
                    "search saved items, delete messages, and more."
                )
            except Exception as exc:
                logger.warning("ai help edit failed: %s", exc)
            return

        engine = _get_engine()
        if engine is None:
            try:
                await event.edit("❌ AI engine not available.")
            except Exception:
                pass
            return

        await _restore_config(owner_id)

        from backend.ai.session.request import AIRequest

        rid = ai_diag.new_request_id()
        ai_diag.register_start(rid, owner_id=owner_id)
        logger.info("AI_REQUEST_START id=%s owner=%d mode=cmd", rid, owner_id)

        session_id = f"owner-{owner_id}"
        request = AIRequest(
            session_id=session_id,
            user_message=user_message,
            owner_id=owner_id,
            chat_id=event.chat_id,
            message_id=event.message.id,
            timezone=tz_str,
            request_id=rid,
        )

        thinking_text = (
            f"{user_message}\n"
            f"────────────\n"
            f"🤖 AI\n"
            f"⏳ Thinking..."
        )

        try:
            await event.edit(thinking_text)
        except Exception:
            pass

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
                except Exception:
                    pass

            if result.success and result.response:
                response_text = _truncate(result.response)
                final_text = (
                    f"{user_message}\n"
                    f"────────────\n"
                    f"🤖 AI\n"
                    f"{response_text}"
                )
            elif result.errors:
                error_msg = _humanize_error(result.errors[0])
                final_text = (
                    f"{user_message}\n"
                    f"────────────\n"
                    f"🤖 AI\n"
                    f"❌ Error\n"
                    f"{error_msg}"
                )
            else:
                final_text = (
                    f"{user_message}\n"
                    f"────────────\n"
                    f"🤖 AI\n"
                    f"❌ Error\n"
                    f"AI returned no response."
                )

            try:
                ai_diag.set_stage(rid, "TELEGRAM_REPLY")
                logger.info("AI_TELEGRAM_REPLY_START id=%s", rid)
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
                logger.warning("ai response edit failed: %s", exc)
                try:
                    await event.reply(final_text)
                    ai_diag.mark_success("TELEGRAM_REPLY")
                    logger.info("AI_TELEGRAM_REPLY_END id=%s (via reply)", rid)
                except Exception:
                    pass

        except asyncio.TimeoutError:
            ai_diag.register_end(rid)
            trace("AI_CMD_TIMEOUT", owner_id=owner_id, timeout=f"{_AI_TIMEOUT}s", rid=rid)
            logger.error("AI cmd: request timed out after %ss", _AI_TIMEOUT)
            error_text = (
                f"{user_message}\n"
                f"────────────\n"
                f"🤖 AI\n"
                f"❌ Error\n"
                f"Request timed out after {int(_AI_TIMEOUT)} seconds."
            )
            try:
                await event.edit(error_text)
            except Exception:
                pass

        except asyncio.CancelledError:
            ai_diag.register_end(rid)
            raise

        except Exception as exc:
            ai_diag.register_end(rid)
            logger.exception("AI handler error: %s", exc)
            trace("AI_HANDLER_ERROR", error=str(exc))
            error_text = (
                f"{user_message}\n"
                f"────────────\n"
                f"🤖 AI\n"
                f"❌ Error\n"
                f"{_humanize_error(str(exc))}"
            )
            try:
                await event.edit(error_text)
            except Exception:
                pass
