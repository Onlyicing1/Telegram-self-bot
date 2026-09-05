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

REPLY-TO-AI MODE (no trigger word needed):
  Owner replies to a known AI message with plain text that does NOT
  start with a trigger word.  The reply is detected BEFORE the trigger
  rejection, so the AI is activated with:
    - The user's full text as the user message.
    - The replied-to AI message as high-priority context.

Both modes enter the SAME execution pipeline:
  1. Build AIRequest with appropriate reply_context
  2. Edit the triggering message to show "Thinking..."
  3. Execute through engine.execute()
  4. Deliver the final response via the centralized delivery module

Short responses edit the original message in-place (zero-spam).
Oversized responses are safely split and delivered in chunks.
"""
import asyncio
import logging
import os
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
_AI_MAX_CONCURRENCY = 4
_ai_semaphore: asyncio.Semaphore | None = None

# Tools whose successful execution must end silently: the Telegram deletion
# is the only visible effect, and a confirmation must never become a message.
_DELETE_TOOL_NAMES = frozenset({
    "delete",
    "delete_replied",
    "delete_by_id",
    "delete_message_by_id",
    "delete_messages_by_ids",
})


def _is_silent_delete(result) -> bool:
    """True when the request executed a pure delete round that fully succeeded.

    Delete runs silently by design: the deletion itself is the only visible
    effect, so the tool-result confirmation (counts, considered messages)
    stays internal — logs, conversation history, and telemetry — and never
    becomes a Telegram message. A failed delete is NOT silent: the error must
    reach the user and can never be mistaken for a success confirmation.
    """
    tool_results = (result.metadata or {}).get("tool_results") or []
    if not tool_results:
        return False
    for item in tool_results:
        if item.get("tool_name") not in _DELETE_TOOL_NAMES:
            return False
    return all(bool(item.get("success")) for item in tool_results)


def _get_concurrency_semaphore() -> asyncio.Semaphore:
    """Return the process-wide AI concurrency limiter.

    Bounds how many AI requests can run at once so a burst of messages
    cannot accumulate unbounded ``ai_active`` requests. Overridable via
    the ``AI_MAX_CONCURRENCY`` environment variable.
    """
    global _ai_semaphore
    if _ai_semaphore is None:
        try:
            limit = int(os.getenv("AI_MAX_CONCURRENCY", str(_AI_MAX_CONCURRENCY)))
        except (TypeError, ValueError):
            limit = _AI_MAX_CONCURRENCY
        _ai_semaphore = asyncio.Semaphore(max(1, limit))
    return _ai_semaphore


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
    # Single shared restore: provider/model → apply_runtime_selection,
    # temperature/max_tokens → the active provider's runtime config,
    # conversation session sync, system prompt. Same path as boot.
    try:
        from backend.ai.engine.engine import apply_persisted_config
        await apply_persisted_config(owner_id)
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


def _format_failure(user_message: str, trigger_label: str, notice: str) -> str:
    return (
        f"{user_message}\n"
        f"────────────\n"
        f"🤖 {trigger_label}\n"
        f"{notice}"
    )


def _failure_notice(result) -> str:
    """Compact, human notice for a failed AI execution.

    Reads ONLY the dispatcher's normalized metadata — never raw provider
    errors, HTTP codes, or tracebacks. Each line answers one question:
    what happened, why, and whether recovery was attempted.
    """
    from backend.ai.engine.telemetry import humanize_failure

    metadata = getattr(result, "metadata", None) or {}
    errors = getattr(result, "errors", None) or []
    raw = str(errors[-1]) if errors else str(getattr(result, "response", "") or "")
    ftype = str(metadata.get("failure_type", "") or "")
    if not ftype:
        return _humanize_error(raw)
    reason = humanize_failure(ftype, raw)
    recovery: list[str] = []
    retries = int(metadata.get("retry_count", 0) or 0)
    if retries > 0:
        recovery.append(f"{retries} retr{'y' if retries == 1 else 'ies'}")
    if metadata.get("fallback_used"):
        recovery.append("backup tried")
    lines = ["✕ Couldn't get a response", reason]
    if ftype == "auth":
        lines.append("Check your API key configuration.")
    if recovery:
        lines.append(f"↻ {' · '.join(recovery)}")
    return "\n".join(lines)


def _describe_empty_result(result) -> str:
    """Turn an empty EngineResult into a meaningful, deterministic message.

    Replaces the generic "AI returned no response." masking by reading the
    dispatcher's finish-state classification.
    """
    metadata = result.metadata or {}
    finish_state = metadata.get("finish_state", "")
    finish_reason = metadata.get("finish_reason", "") or ""
    if finish_state == "tool_rounds_exhausted":
        pending = len(metadata.get("pending_tool_calls", []))
        rounds = metadata.get("tool_rounds_executed", 0)
        return (
            f"Tool round limit reached after {rounds} round(s) — "
            f"{pending} pending tool call(s) were not executed."
        )
    if finish_state == "tool_only":
        return "The AI requested tools but produced no final text response."
    if finish_state == "provider_blocked":
        suffix = f" ({finish_reason})" if finish_reason else ""
        return f"Response blocked by the provider{suffix}."
    if finish_state == "token_truncated":
        return "Response truncated because the token limit was reached."
    if finish_state == "empty":
        suffix = f" (provider finish reason: {finish_reason})" if finish_reason else ""
        return f"AI returned no response.{suffix}"
    return "AI returned no response."


def _humanize_error(error: str) -> str:
    """Convert an internal AI failure into a clean, provider-agnostic message.

    Provider internals (429, model not found, cooldown, HTTP status codes,
    connection resets) stay in the logs — the Telegram response only ever
    carries an actionable, non-leaky message. Only authentication failures
    (a genuine configuration problem the owner must fix) are surfaced, and
    still without the raw provider detail.
    """
    error_lower = error.lower()
    if (
        "401" in error_lower or "403" in error_lower
        or "unauthorized" in error_lower or "invalid api key" in error_lower
    ):
        return "AI provider authentication failed. Check your API key configuration."
    if (
        "all ai providers failed" in error_lower
        or "429" in error_lower or "rate" in error_lower
        or "cooling" in error_lower or "cooldown" in error_lower
        or "timeout" in error_lower or "timed out" in error_lower
        or "model not found" in error_lower or "404" in error_lower
        or "connection" in error_lower or "network" in error_lower
        or "dns" in error_lower or "unavailable" in error_lower
    ):
        return "AI is temporarily unavailable. Please try again shortly."
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

    # ── Extract sender info + chat info (parallel — independent network calls) ──
    sender_name = ""
    sender_id = 0
    chat_title = ""
    chat_id = reply_msg.chat_id or 0
    sender_fetch, chat_fetch = await asyncio.gather(
        reply_msg.get_sender(),
        reply_msg.get_chat(),
        return_exceptions=True,
    )
    if isinstance(sender_fetch, BaseException):
        sender_fetch = None
    if isinstance(chat_fetch, BaseException):
        chat_fetch = None
    if sender_fetch is not None:
        try:
            sender_id = getattr(sender_fetch, "id", 0) or 0
            first = getattr(sender_fetch, "first_name", "") or ""
            last = getattr(sender_fetch, "last_name", "") or ""
            sender_name = (f"{first} {last}").strip() or getattr(sender_fetch, "username", "") or ""
        except Exception:
            pass
    if chat_fetch is not None:
        try:
            chat_title = getattr(chat_fetch, "title", "") or getattr(chat_fetch, "username", "") or ""
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
    """Execute the AI pipeline and deliver the result via centralized delivery.

    A single request id tracks the whole lifecycle, and ``register_end`` runs
    in a ``finally`` block so ``ai_active`` can never leak — whether the
    provider times out, a tool fails, or Telegram delivery raises.
    """
    from backend.ai.session.request import AIRequest
    from backend.ai.conversation.context_builder import ReplyContext

    # Capture the immutable Telegram anchor before config/provider work or
    # any in-place status edit. Delete uses this original message ID as the
    # active-request exclusion and as the boundary for "up to this message".
    request_chat_id = getattr(event, "chat_id", None)
    request_message_id = getattr(getattr(event, "message", None), "id", None)
    rid = ai_diag.new_request_id()
    ai_diag.register_start(rid, owner_id=owner_id)
    logger.info("AI_REQUEST_START id=%s owner=%d", rid, owner_id)
    logger.info(
        "TELEGRAM_CHAT_RESOLVE id=%s chat_id=%s request_message_id=%s",
        rid, request_chat_id, request_message_id,
    )
    logger.info("AI_EXEC_TRACE request_id=%s stage=telegram_received", rid)

    engine = _get_engine()
    if engine is None:
        try:
            await event.edit(_format_error(prompt_text, trigger_word, "AI engine not available."))
        except Exception as exc:
            logger.error("AI handler: failed to edit error state (no engine): %s", exc)
        ai_diag.register_end(rid)
        logger.info("AI_REQUEST_END id=%s", rid)
        return

    sem = _get_concurrency_semaphore()
    try:
        await asyncio.wait_for(sem.acquire(), timeout=_AI_TIMEOUT)
    except asyncio.TimeoutError:
        ai_diag.register_end(rid)
        logger.warning("AI handler: rejecting request id=%s (concurrency limit reached)", rid)
        try:
            await event.edit(_format_error(
                prompt_text, trigger_word,
                "Too many AI requests in progress. Please try again shortly.",
            ))
        except Exception:
            pass
        return

    trigger_label = trigger_word
    display_prompt = prompt_text

    try:
        ai_diag.set_stage(rid, "CONFIG_LOAD")
        logger.info("AI_CONFIG_LOAD_START id=%s", rid)
        await _restore_config(owner_id)
        ai_diag.mark_success("CONFIG_LOAD")
        logger.info("AI_CONFIG_LOAD_END id=%s", rid)

        try:
            pm = engine.provider_manager
            provider_name = pm.get_active_name()
            model = ""
            try:
                model = pm.get_active().config.default_model or ""
            except Exception:
                pass
            logger.info("AI_PROVIDER_RESOLVE id=%s provider=%s model=%s", rid, provider_name, model)
        except Exception as exc:
            logger.debug("AI handler: provider resolve log failed: %s", exc)

        session_id = f"owner-{owner_id}"
        request = AIRequest(
            session_id=session_id,
            user_message=prompt_text,
            owner_id=owner_id,
            chat_id=request_chat_id,
            message_id=request_message_id,
            reply_context=reply_context or ReplyContext(),
            timezone=tz_str,
            request_id=rid,
        )

        async def _status_callback(status: str) -> None:
            try:
                await event.edit(
                    f"{display_prompt}\n"
                    f"────────────\n"
                    f"🤖 {trigger_label}\n"
                    f"{status}"
                )
            except Exception as exc:
                logger.debug("AI handler: status edit failed: %s", exc)

        try:
            await event.edit(_format_thinking(display_prompt, trigger_label))
        except Exception as exc:
            logger.warning("AI handler: failed to edit thinking state: %s", exc)

        result = await asyncio.wait_for(
            engine.execute(request, status_callback=_status_callback),
            timeout=_AI_TIMEOUT,
        )
        record_event("ai", "execute", 0, "SUCCESS" if result.success else "FAILED",
                     f"provider={result.provider}")

        # Request telemetry is fire-and-forget and writes only the latency
        # columns — it must never rewrite the full AI config or block the
        # response path during normal inference.
        if result.success:
            try:
                from backend.ai.config_store import record_request
                from backend.runtime.task_guard import guarded_create_task
                guarded_create_task(
                    record_request(owner_id, result.latency * 1000),
                    name="ai:record-request",
                )
            except Exception as exc:
                logger.warning("AI handler: scheduling record_request failed: %s", exc)

        if result.success and result.response:
            # ── Silent delete ──
            # A successful pure-delete execution must not produce any
            # Telegram confirmation: the deletion is the only visible effect.
            # Delivering "Deleted N message(s)..." would be spam, and when
            # the delete removed the request message itself the delivery
            # fallback would even turn it into a brand-new confirmation
            # message. The tool result stays internal (logs, history,
            # telemetry); the request message is reverted to the owner's
            # original text (best effort — it may already be deleted).
            if _is_silent_delete(result):
                tools = ",".join(
                    item.get("tool_name", "")
                    for item in (result.metadata or {}).get("tool_results") or []
                )
                logger.info("AI_DELETE_SILENT id=%s tools=%s", rid, tools)
                logger.info(
                    "AI_EXEC_TRACE request_id=%s stage=delete_silent tools=%s",
                    rid, tools,
                )
                ai_diag.set_stage(rid, "DELETE_SILENT")
                ai_diag.mark_success("DELETE_SILENT")
                try:
                    await event.edit(display_prompt)
                except Exception as exc:
                    logger.debug(
                        "AI handler: silent delete revert edit skipped "
                        "(request message likely deleted as part of the "
                        "operation): %s", exc,
                    )
                return
            ai_diag.set_stage(rid, "TELEGRAM_REPLY")
            logger.info("AI_RESPONSE_SEND_START id=%s", rid)
            from backend.ai.tools.delivery import deliver_response
            response_text = result.response
            if result.metadata.get("tool_rounds_exhausted"):
                pending = len(result.metadata.get("pending_tool_calls", []))
                response_text = (
                    f"{response_text}\n\n⚠️ Tool round limit reached — "
                    f"{pending} pending tool call(s) were not executed."
                )
            # Secondary status under the answer — never a diagnostic block.
            # A fallback recovery gets its own one-line note (the user should
            # know a backup model answered); the optional compact per-request
            # telemetry line follows the owner's preference (off by default).
            # Neither ever invents numbers: the line renders from the
            # normalized execution record and omits unavailable usage.
            from backend.ai.engine.telemetry import compact_telemetry_line, telemetry
            notes: list[str] = []
            if result.metadata.get("fallback_used"):
                notes.append("_↻ Backup model used_")
            if telemetry.get_telemetry_pref(owner_id):
                line = compact_telemetry_line(telemetry.last())
                if line:
                    notes.append(f"_{line}_")
            if notes:
                response_text = f"{response_text}\n\n" + "\n".join(notes)
            delivery_result = await deliver_response(
                event, display_prompt, trigger_label, response_text,
            )
            if delivery_result.success:
                ai_diag.mark_success("TELEGRAM_REPLY")
            logger.info(
                "AI_EXEC_TRACE request_id=%s stage=telegram_response success=%s",
                rid, delivery_result.success,
            )
            logger.info(
                "AI_RESPONSE_SEND_END id=%s chunks=%d/%d",
                rid, delivery_result.chunks_delivered, delivery_result.total_chunks,
            )
            from backend.ai.context.reply_resolver import get_resolver
            meta = result.metadata or {}
            get_resolver().register(
                telegram_msg_id=event.message.id,
                session_id=session_id,
                role="assistant",
                content=result.response,
                provider=result.provider,
                model=result.model,
                input_tokens=getattr(result, "prompt_tokens", 0) or 0,
                output_tokens=getattr(result, "completion_tokens", 0) or 0,
                total_tokens=getattr(result, "total_tokens", 0) or 0,
                token_source=str(meta.get("token_source", "") or ""),
                latency_s=float(getattr(result, "latency", 0.0) or 0.0),
                retry_count=int(meta.get("retry_count", 0) or 0),
                fallback_used=bool(meta.get("fallback_used", False)),
            )
        elif result.errors or result.response:
            logger.info(
                "AI_PROVIDER_FAILURE id=%s provider=%s model=%s",
                rid, result.provider, result.model,
            )
            final_text = _format_failure(
                display_prompt, trigger_label, _failure_notice(result)
            )
            try:
                await event.edit(final_text)
            except Exception as exc:
                logger.warning("AI handler: failed to edit error response: %s", exc)
                try:
                    await event.reply(final_text)
                except Exception:
                    pass
        else:
            error_msg = _describe_empty_result(result)
            final_text = _format_error(display_prompt, trigger_label, error_msg)
            try:
                await event.edit(final_text)
            except Exception as exc:
                logger.warning("AI handler: failed to edit no-response error: %s", exc)

    except asyncio.TimeoutError:
        trace("AI_TRIGGER_TIMEOUT", owner_id=owner_id, timeout=f"{_AI_TIMEOUT}s", rid=rid)
        logger.error("AI handler: request timed out after %ss (id=%s)", _AI_TIMEOUT, rid)
        error_text = _format_error(
            display_prompt, trigger_label,
            f"Request timed out after {int(_AI_TIMEOUT)} seconds.",
        )
        try:
            await event.edit(error_text)
        except Exception as exc:
            logger.error("AI handler: failed to edit timeout error: %s", exc)

    except asyncio.CancelledError:
        raise

    except Exception as exc:
        logger.exception("AI handler error: %s (id=%s)", exc, rid)
        trace("AI_HANDLER_ERROR", error=str(exc))
        error_text = _format_error(display_prompt, trigger_label, _humanize_error(str(exc)))
        try:
            await event.edit(error_text)
        except Exception as edit_exc:
            logger.error("AI handler: failed to edit error state: %s", edit_exc)

    finally:
        ai_diag.register_end(rid)
        sem.release()
        logger.info("AI_REQUEST_END id=%s", rid)


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

    METHOD 3 — Reply-to-AI Mode (no trigger word needed):
      Owner replies to a known AI message with plain text that does NOT
      start with a trigger word.  The reply is detected BEFORE the trigger
      rejection, so the AI is activated with the full text as the user
      message and the replied-to AI message as context.

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
        remaining = words[1].strip() if len(words) > 1 else ""

        trigger_en, trigger_fa = await _load_triggers(owner_id)
        trigger_matched = False
        if trigger_en or trigger_fa:
            from backend.ai.config_store import match_trigger
            trigger_matched = match_trigger(first_word, trigger_en, trigger_fa)

        is_reply = bool(getattr(event, "is_reply", False))

        # ── Detect reply to a known AI message ──
        # This check happens BEFORE the trigger rejection so that replying
        # to an AI message with plain text (no trigger word) still activates
        # the AI.  The replied AI message becomes context and the user's
        # full text becomes the prompt.
        reply_to_ai = False
        if is_reply:
            from backend.ai.context.reply_resolver import get_resolver
            try:
                reply_msg = await event.get_reply_message()
                if reply_msg is not None:
                    resolved = get_resolver().resolve(reply_msg.id or 0)
                    if resolved is not None:
                        reply_to_ai = True
            except Exception as exc:
                logger.warning("AI handler: reply-to-AI check failed: %s", exc)

        if not trigger_matched and not reply_to_ai:
            return

        # Determine the actual user message and trigger label
        if trigger_matched:
            trigger_label = first_word
            user_text = remaining
        else:
            trigger_label = "AI"
            user_text = raw_text

        # ── Reply-Aware Mode: message is a reply ──
        if is_reply:
            trace("AI_TRIGGER_MATCHED", trigger=trigger_label, mode="reply",
                  reply_to_ai=reply_to_ai)
            user_message, reply_ctx, error_msg = await _extract_reply_context(
                event, client, user_text
            )

            if error_msg:
                try:
                    await event.edit(_format_error(trigger_label, trigger_label, error_msg))
                except Exception as exc:
                    logger.warning("AI handler: failed to edit reply error: %s", exc)
                return

            await _execute_ai(
                event, owner_id, user_message, trigger_label, tz_str,
                reply_context=reply_ctx,
            )
            return

        # ── Trigger Mode: no reply, must have remaining text ──
        if not user_text:
            return

        trace("AI_TRIGGER_MATCHED", trigger=trigger_label, mode="trigger")
        await _execute_ai(event, owner_id, user_text, trigger_label, tz_str)
