"""
.ai <text> — AI conversation handler.

Wires the AI Engine to Telegram. When the owner sends .ai <message>,
the handler:
  1. Gets or creates an AI session for the owner
  2. Builds an AIRequest with the user's message
  3. Executes the request through the full AI pipeline
  4. Edits the triggering message with the AI response

The AI works immediately after deployment if an API key is configured.
Without an API key, the dummy provider returns a placeholder response.

Falls back to plain-text edit-in-place (zero-spam policy).
"""
import asyncio
import logging

from telethon import events

from backend.bot.handlers.guard import is_owner
from backend.diagnostics import record_event
from backend.runtime.tracer import trace

logger = logging.getLogger(__name__)

_engine = None
_owner_id: int = 0
_tz_str: str = "UTC"


def configure(engine, owner_id: int, tz_str: str) -> None:
    global _engine, _owner_id, _tz_str
    _engine = engine
    _owner_id = owner_id
    _tz_str = tz_str


def _get_engine():
    if _engine is not None:
        return _engine
    try:
        from backend.ai.engine.engine import get_engine
        _engine = get_engine()
        return _engine
    except Exception as exc:
        logger.warning("AI handler: could not get engine: %s", exc)
        return None


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
            await event.edit("🧠 Thinking…")
        except Exception:
            pass

        try:
            result = await engine.execute(request)
            record_event("ai", "execute", 0, "SUCCESS" if result.success else "FAILED",
                         f"provider={result.provider}")

            if result.success and result.response:
                response_text = result.response
            elif result.errors:
                response_text = f"❌ AI error: {result.errors[0]}"
            else:
                response_text = "❌ AI returned no response."

            if len(response_text) > 4000:
                response_text = response_text[:4000] + "…"

            try:
                await event.edit(f"🧠 {response_text}")
            except Exception as exc:
                logger.warning("ai response edit failed: %s", exc)
                try:
                    await event.reply(f"🧠 {response_text}")
                except Exception:
                    pass

        except Exception as exc:
            logger.exception("AI handler error: %s", exc)
            trace("AI_HANDLER_ERROR", error=str(exc))
            try:
                await event.edit(f"❌ AI error: {exc}")
            except Exception:
                pass
