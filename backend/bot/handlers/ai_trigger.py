"""
Trigger-based AI conversation handler.

Replaces the old `.ai` command with configurable trigger words.
When the owner sends an outgoing message whose first word matches
either the English trigger (case-insensitive) or the Persian trigger
(exact match), the AI subsystem activates:

  1. Loads the owner's trigger config from Supabase
  2. Matches the first word against trigger_en / trigger_fa
  3. Strips the trigger word from the message
  4. Restores the saved provider/model from Supabase
  5. Builds an AIRequest with the stripped message
  6. Executes the request through the full AI pipeline
  7. Edits the triggering message with the AI response
  8. Records request latency in Supabase

The old `.ai` command handler (ai_cmd.py) is kept for backward
compatibility but is deprecated. The trigger system is the default.

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
_trigger_cache: dict[str, str] = {"en": "", "fa": "", "ts": 0.0}
_CACHE_TTL = 30.0


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
        logger.warning("AI trigger handler: could not get engine: %s", exc)
        return None


async def _load_triggers(owner_id: int) -> tuple[str, str]:
    """Load trigger words from Supabase with a short in-memory cache."""
    import time
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
            except Exception:
                pass
    except Exception as exc:
        logger.warning("AI trigger handler: config restore failed: %s", exc)


async def _execute_ai(event, owner_id: int, user_message: str, tz_str: str) -> None:
    """Execute the AI pipeline and edit the triggering message with the result."""
    engine = _get_engine()
    if engine is None:
        try:
            await event.edit("❌ AI engine not available.")
        except Exception:
            pass
        return

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
        await event.edit("🧠 Thinking…")
    except Exception:
        pass

    try:
        result = await engine.execute(request)
        record_event("ai", "execute", 0, "SUCCESS" if result.success else "FAILED",
                     f"provider={result.provider}")

        if result.success:
            try:
                from backend.ai.config_store import record_request
                await record_request(owner_id, result.latency * 1000)
            except Exception:
                pass

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
            logger.warning("AI trigger response edit failed: %s", exc)
            try:
                await event.reply(f"🧠 {response_text}")
            except Exception:
                pass

    except Exception as exc:
        logger.exception("AI trigger handler error: %s", exc)
        trace("AI_TRIGGER_HANDLER_ERROR", error=str(exc))
        try:
            await event.edit(f"❌ AI error: {exc}")
        except Exception:
            pass


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
        await _execute_ai(event, owner_id, remaining, tz_str)
