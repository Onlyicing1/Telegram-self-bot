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
import contextvars
import logging
import os
import time
from typing import Any

from telethon import events

from backend.bot.handlers.guard import is_owner
from backend.diagnostics import record_event
from backend.runtime.tracer import trace
from backend.ai import diagnostics as ai_diag

logger = logging.getLogger(__name__)

_engine = None
_owner_id: int = 0
_tz_str: str = "UTC"
#: TTL cache of the owner's ``ai_config`` snapshot: the trigger words AND the
#: row they were read from. The snapshot is cached with them so a cache hit
#: still threads the DURABLE row (the presentation preference lives in it)
#: instead of forcing the callers to fall back to a compiled default.
_trigger_cache: dict[str, Any] = {"en": "", "fa": "", "ts": 0.0, "config": None}
_CACHE_TTL = 30.0
_AI_TIMEOUT = 60.0
#: Envelope for one AI EXECUTION (not for waiting on a concurrency slot, which
#: keeps ``_AI_TIMEOUT``). The inner work is already bounded: every provider HTTP
#: call carries ``ProviderConfig.timeout`` (30s), the tool loop is bounded by
#: ``MAX_TOOL_ROUNDS``, and the tool executor exempts ``long_running`` tools from
#: the generic 10s tool timeout. A 60s envelope silently defeated that exemption:
#: chunked history work (and Deep Save) were killed mid-flight and surfaced as a
#: timeout instead of a result. This is the backstop, and it must be wide enough
#: for the work the tool contract already declares long-running.
_AI_EXECUTE_TIMEOUT = 240.0
_AI_MAX_CONCURRENCY = 4
_RPC_T = 30.0

# The ``ai_config`` snapshot the activation handler already read (triggers),
# visible to the presentation helpers for the request's lifetime. This avoids
# a second durable config read per AI message — the preference is threaded,
# never re-fetched. No cache: it is set fresh for every request.
_PREFETCHED_CONFIG: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "prefetched_ai_config", default=None
)

# Appended to the text reply ONLY when the Taskloom wizard panel could not be
# sent (helper bot unavailable): the owner still needs an actionable path to
# the SAME structured creation flow.
_WIZARD_UNAVAILABLE_HINT = (
    "Open **Menu → Taskloom → ＋ New task** to set this up with structured options."
)
_ai_semaphore: asyncio.Semaphore | None = None

# Sentinel for "the caller has not fetched the replied message yet". A real
# fetch can legitimately return None (= no replied message), so None cannot
# double as "not fetched".
_REPLY_UNFETCHED = object()

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


def _wizard_signal(result) -> dict | None:
    """The Taskloom-wizard signal a tool result asked the delivery layer to surface.

    Only a ``create_task`` result that could not produce a COMPLETE task
    definition carries it (an unsupported capability, or a candidate-level
    interpretation failure). Provider, timeout, and persistence failures
    carry nothing: retrying is the right answer there, not filling in a form.
    """
    metadata = getattr(result, "metadata", None) or {}
    for item in metadata.get("tool_results") or []:
        if not isinstance(item, dict) or item.get("tool_name") != "create_task":
            continue
        data = item.get("data")
        if isinstance(data, dict) and data.get("open_taskloom_wizard"):
            return data
    return None


def _wizard_notice(signal: dict) -> str:
    """One honest, bounded line explaining why the structured wizard opened."""
    reason = str(signal.get("wizard_reason") or "").strip()
    capability = " ".join(str(signal.get("capability") or "").split())[:80]
    if reason == "unsupported_capability" and capability:
        return f"⚠ `{capability}` cannot be scheduled as one sentence — choose the options below."
    if reason == "incomplete_request":
        return (
            "⚠ This task needs a schedule (and content details) the request "
            "did not fully specify — choose them below."
        )
    return "⚠ I need a few structured choices for this task — fill them in below."


async def _open_task_wizard(event, client, owner_id: int, signal: dict) -> bool:
    """Open the EXISTING Taskloom creation wizard for a task request the
    natural-language path could not complete.

    Returns True only when the Glass UI panel was actually sent. The wizard
    draft is reset with an explanatory notice; nothing is prefilled from the
    request and nothing is persisted — the owner's own choices still build the
    candidate that the same ``TaskCreationService`` / ``TaskRepository`` store.
    """
    from backend.bot.handlers import taskloom
    from backend.helper import send_inline_panel
    from backend.helper.rpc_timeout import rpc_await

    chat_id = getattr(event, "chat_id", None)
    if not isinstance(chat_id, int):
        return False
    taskloom.reset_wizard_draft(owner_id, notice=_wizard_notice(signal))
    try:
        opened = await rpc_await(
            send_inline_panel(client, chat_id, taskloom.WIZARD_PANEL_QUERY),
            timeout=_RPC_T,
            label="taskloom.wizard_bridge",
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("AI handler: task wizard panel send failed: %s", exc)
        return False
    if not opened:
        return False
    try:
        await rpc_await(
            event.delete(), timeout=_RPC_T, label="taskloom.wizard_bridge_delete"
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.debug("AI handler: wizard bridge placeholder delete skipped: %s", exc)
    return True


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


async def _load_triggers(owner_id: int) -> tuple[str, str, dict | None]:
    """Resolve the owner's trigger words (TTL-cached) and hand back the row.

    The third value is the ``ai_config`` snapshot this call read — and on a
    cache hit it is the snapshot that read stored, because the presentation
    preference lives in the same row as the triggers. Returning ``None`` here
    for a cache hit would push every caller that needs the preference onto the
    compiled default for the whole TTL window, i.e. a persisted
    ``show_question = true`` would be ignored by the reply renderer. ``None``
    is only returned when no row could be read at all.

    The caller passes the snapshot to the config restore so one request reads
    that row at most once.
    """
    now = time.monotonic()
    if (now - _trigger_cache["ts"]) < _CACHE_TTL and _trigger_cache["en"] is not None:
        return _trigger_cache["en"], _trigger_cache["fa"], _trigger_cache.get("config")
    try:
        from backend.ai.config_store import get_config
        config = await get_config(owner_id)
        en = config.get("trigger_en", "") or ""
        fa = config.get("trigger_fa", "") or ""
        _trigger_cache["en"] = en
        _trigger_cache["fa"] = fa
        _trigger_cache["ts"] = now
        # Same request, same row: the restore reuses THIS snapshot instead of
        # issuing a second identical read. It is also retained by the trigger
        # cache so a later cache hit threads the same durable row rather than
        # falling back to compiled defaults.
        _trigger_cache["config"] = config
        return en, fa, config
    except Exception as exc:
        logger.warning("AI handler: failed to load triggers: %s", exc)
        _trigger_cache["config"] = None
        return "", "", None


def invalidate_config_cache() -> None:
    """Drop the cached ``ai_config`` snapshot so the next request re-reads it.

    Called after an interactive settings change (e.g. the AI → Settings
    "my message in replies" toggle) so the change is visible on the very next
    message instead of up to ``_CACHE_TTL`` later.
    """
    _trigger_cache["ts"] = 0.0
    _trigger_cache["config"] = None


async def _restore_config(owner_id: int, config: dict | None = None) -> None:
    # Single shared restore: provider/model → apply_runtime_selection,
    # temperature/max_tokens → the active provider's runtime config,
    # conversation session sync, system prompt. Same path as boot.
    try:
        from backend.ai.engine.engine import apply_persisted_config
        await apply_persisted_config(owner_id, config=config)
    except Exception as exc:
        logger.warning("AI handler: config restore failed: %s", exc)


def _show_question_pref(owner_id: int, config: dict | None = None) -> bool:
    """The owner's "show my message in replies" presentation preference.

    Durable: read through the existing AI-config path (``config_store``),
    never from any RAM store. The caller's already-loaded ``ai_config``
    snapshot is authoritative when present (that is the same row the request
    resolved its triggers from); the request-scoped context value is used as
    the fallback for call sites that do not hold the snapshot. Only when no
    snapshot exists at all is the compiled default used.
    """
    for candidate in (config, _context_config()):
        if candidate is not None and "show_question" in candidate:
            return bool(candidate["show_question"])
    from backend.ai.config_store import _DEFAULTS

    return bool(_DEFAULTS["show_question"])


def _context_config() -> dict | None:
    """The request's ``ai_config`` snapshot, or ``None`` outside a request."""
    try:
        return _PREFETCHED_CONFIG.get()
    except LookupError:
        return None


def _format_thinking(user_message: str, show_question: bool) -> str:
    from backend.ai.tools.delivery import format_thinking

    return format_thinking(user_message, show_question)


def _format_error(user_message: str, error: str, show_question: bool) -> str:
    from backend.ai.tools.delivery import format_failure

    return format_failure(user_message, f"Error\n{error}", show_question)


def _format_failure(user_message: str, notice: str, show_question: bool) -> str:
    from backend.ai.tools.delivery import format_failure

    return format_failure(user_message, notice, show_question)


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


async def _extract_reply_context(
    event, client, user_text: str, reply_msg: Any = _REPLY_UNFETCHED,
) -> tuple[str, "ReplyContext", str]:
    """Extract reply context from a replied-to message.

    The replied-to message is ALWAYS treated as CONTEXT — never as the
    user's new message.  The user's actual instruction (``user_text``)
    is the prompt that goes to the AI.

    When replying to a known AI message, the full untruncated AI response
    is injected via ``ReplyContext.ai_content`` so the Prompt Builder can
    include it as high-priority context.

    ``reply_msg`` may carry the message the activation handler already
    fetched for its reply-to-AI check: one request must not issue the same
    Telegram fetch twice. It is only fetched here when the caller has none.

    Returns (user_message, reply_context, error_message).
    On success, error_message is empty. On failure, user_message is empty.
    """
    from backend.ai.conversation.context_builder import ReplyContext
    from backend.ai.media import classify_message

    if reply_msg is _REPLY_UNFETCHED:
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


async def _load_telegram_chat_context(client, chat_id, message_id, reply_context,
                                      tz_str: str):
    """Fetch this request's bounded Telegram surrounding-message window ONCE.

    Optional enrichment: the surrounding Telegram messages of the chat the AI
    was triggered in. The snapshot is request-scoped (never persisted, never
    merged into the AI history) and threaded to the Context/Prompt layers
    through the ``AIRequest``. A missing anchor, an unavailable client, a
    Telegram failure, or a timeout all degrade to ``None`` — the AI request
    always proceeds with the context it already had.

    The replied-to message (when this request is a reply) is EXCLUDED from the
    window: it already travels as the higher-fidelity ``ReplyContext``, so the
    same message is never rendered twice.
    """
    from backend.ai.conversation.telegram_context import fetch_telegram_chat_context

    exclude: tuple[int, ...] = ()
    if reply_context is not None and getattr(reply_context, "exists", False):
        exclude = (int(getattr(reply_context, "message_id", 0) or 0),)

    snapshot = await fetch_telegram_chat_context(
        client, chat_id, message_id, tz_str=tz_str, exclude_message_ids=exclude,
    )
    if snapshot.is_empty:
        return None
    logger.info(
        "TELEGRAM_CHAT_CONTEXT chat_id=%s message_id=%s messages=%d truncated=%s",
        chat_id, message_id, len(snapshot.messages), snapshot.truncated,
    )
    return snapshot


async def _media_type_of(message: Any) -> str:
    """The media label of the TRIGGERING message itself, or empty.

    Pure attribute inspection through the existing classifier — no download, no
    fetch, no network. It only tells the runtime that this request HAS a media
    target; the target itself is resolved from the request's own chat/message
    ids, never from text and never by the model.
    """
    if message is None:
        return ""
    try:
        from backend.ai.media import classify_message
        info = classify_message(message)
    except Exception as exc:  # noqa: BLE001 — classification never breaks a request
        logger.debug("AI handler: media classification failed: %s", exc)
        return ""
    return info.media_type if info.has_media else ""


async def _execute_ai(event, owner_id: int, prompt_text: str, trigger_word: str,
                      tz_str: str, reply_context=None, client=None,
                      config: dict | None = None, request_media_type: str = "") -> None:
    """Execute the AI pipeline and deliver the result via centralized delivery.

    ``trigger_word`` identifies the activation that started the request; the
    reply presentation is renderer-owned and never shows a trigger label.

    ``config`` carries the ``ai_config`` snapshot the handler already read for
    this request (trigger resolution), so the config restore does not read the
    same row twice. ``None`` means "read it in the restore", never "empty".

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

    show_question = _show_question_pref(owner_id, config)
    _PREFETCHED_CONFIG.set(None)
    engine = _get_engine()
    if engine is None:
        try:
            await event.edit(_format_error(prompt_text, "AI engine not available.", show_question))
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
                prompt_text,
                "Too many AI requests in progress. Please try again shortly.",
                show_question,
            ))
        except Exception:
            pass
        return

    display_prompt = prompt_text

    try:
        ai_diag.set_stage(rid, "CONFIG_LOAD")
        logger.info("AI_CONFIG_LOAD_START id=%s", rid)
        await _restore_config(owner_id, config=config)
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
        # One bounded Telegram read per request, threaded forward as a snapshot;
        # no later layer (ContextBuilder, PromptBuilder, dispatcher, tools,
        # delivery) reads these messages again.
        telegram_context = await _load_telegram_chat_context(
            client, request_chat_id, request_message_id, reply_context, tz_str,
        )
        request = AIRequest(
            session_id=session_id,
            user_message=prompt_text,
            owner_id=owner_id,
            chat_id=request_chat_id,
            message_id=request_message_id,
            reply_context=reply_context or ReplyContext(),
            telegram_context=telegram_context,
            timezone=tz_str,
            request_id=rid,
            timeout_s=_AI_EXECUTE_TIMEOUT,
            request_media_type=request_media_type,
        )

        async def _status_callback(status: str) -> None:
            try:
                from backend.ai.tools.delivery import format_status
                await event.edit(format_status(display_prompt, status, show_question))
            except Exception as exc:
                logger.debug("AI handler: status edit failed: %s", exc)

        try:
            await event.edit(_format_thinking(display_prompt, show_question))
        except Exception as exc:
            logger.warning("AI handler: failed to edit thinking state: %s", exc)

        result = await asyncio.wait_for(
            engine.execute(request, status_callback=_status_callback),
            timeout=request.timeout_s or _AI_EXECUTE_TIMEOUT,
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
            # ── Natural-language → Taskloom wizard bridge ──
            # A create_task result that could not produce a complete task
            # definition surfaces the EXISTING structured creation wizard
            # instead of only a refusal. The wizard is the same one the direct
            # Taskloom button opens and converges on the same candidate /
            # persistence path; if the panel cannot be sent (helper bot
            # unavailable) the text reply is delivered with an actionable hint.
            wizard_unavailable = False
            wizard = _wizard_signal(result)
            if wizard is not None:
                ai_diag.set_stage(rid, "TASK_WIZARD")
                if await _open_task_wizard(event, client, owner_id, wizard):
                    ai_diag.mark_success("TASK_WIZARD")
                    logger.info(
                        "AI_EXEC_TRACE request_id=%s stage=task_wizard_opened reason=%s",
                        rid, wizard.get("wizard_reason") or "-",
                    )
                    return
                wizard_unavailable = True
                logger.info(
                    "AI_EXEC_TRACE request_id=%s stage=task_wizard_unavailable "
                    "reason=%s", rid, wizard.get("wizard_reason") or "-",
                )
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
            if wizard_unavailable:
                response_text = f"{response_text}\n\n_{_WIZARD_UNAVAILABLE_HINT}_"
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
                event, display_prompt, response_text, show_question,
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
                display_prompt, _failure_notice(result), show_question
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
            final_text = _format_error(display_prompt, error_msg, show_question)
            try:
                await event.edit(final_text)
            except Exception as exc:
                logger.warning("AI handler: failed to edit no-response error: %s", exc)

    except asyncio.TimeoutError:
        # The module constant, never ``request.timeout_s``: an earlier bounded
        # await in this block can raise TimeoutError before the request object
        # exists.
        trace("AI_TRIGGER_TIMEOUT", owner_id=owner_id, timeout=f"{_AI_EXECUTE_TIMEOUT}s", rid=rid)
        logger.error("AI handler: request timed out after %ss (id=%s)", _AI_EXECUTE_TIMEOUT, rid)
        error_text = _format_error(
            display_prompt,
            f"Request timed out after {int(_AI_EXECUTE_TIMEOUT)} seconds.",
            show_question,
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
        error_text = _format_error(display_prompt, _humanize_error(str(exc)), show_question)
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

        trigger_en, trigger_fa, config_snapshot = await _load_triggers(owner_id)
        _PREFETCHED_CONFIG.set(config_snapshot)
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
        # Fetched ONCE for the whole request: the reply-to-AI sniff below and
        # the reply-context extraction use the SAME Telegram object instead of
        # re-issuing the fetch.
        reply_message: Any = _REPLY_UNFETCHED
        if is_reply:
            from backend.ai.context.reply_resolver import get_resolver
            try:
                reply_message = await event.get_reply_message()
                if reply_message is not None:
                    resolved = get_resolver().resolve(reply_message.id or 0)
                    if resolved is not None:
                        reply_to_ai = True
            except Exception as exc:
                logger.warning("AI handler: reply-to-AI check failed: %s", exc)
                reply_message = _REPLY_UNFETCHED

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
                event, client, user_text, reply_msg=reply_message
            )

            if error_msg:
                try:
                    await event.edit(
                        _format_error(user_text, error_msg, _show_question_pref(owner_id, config_snapshot))
                    )
                except Exception as exc:
                    logger.warning("AI handler: failed to edit reply error: %s", exc)
                return

            await _execute_ai(
                event, owner_id, user_message, trigger_label, tz_str,
                reply_context=reply_ctx, client=client, config=config_snapshot,
            )
            return

        # ── Trigger Mode: no reply, must have remaining text ──
        if not user_text:
            return

        trace("AI_TRIGGER_MATCHED", trigger=trigger_label, mode="trigger")
        # An owner-authored request on a message that CARRIES media (e.g. a file
        # with the caption "این رو خلاصه کن") is a deterministic media request:
        # the triggering message is the target. A caption-less media message
        # never reaches here — the empty ``raw_text`` guard above returns first,
        # exactly as before.
        await _execute_ai(
            event, owner_id, user_text, trigger_label, tz_str,
            client=client, config=config_snapshot,
            request_media_type=await _media_type_of(getattr(event, "message", None)),
        )
