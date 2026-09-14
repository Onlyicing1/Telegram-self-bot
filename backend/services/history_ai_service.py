"""
History AI service — LLM translation and summarization of Telegram history.

This is the orchestration boundary for the two explicit history operations the
owner can ask for:

    "translate the last 500 messages"
    "summarize the last 1000 messages"

Layering:

    Telegram -> backend/telegram_api -> backend/services/history_service.py
             -> this module (chunk + LLM map + LLM reduce)
             -> backend/ai/tools/history_ai.py (thin tools)

Rules this module exists to enforce:

  * Telegram history comes EXCLUSIVELY from ``backend/services/history_service``.
    No Telethon import, no ``iter_messages``, no cursor handling here.
  * Provenance eligibility is NOT re-implemented here. The history service
    already excludes AI-provenance-bearing messages (and strips the marker), so
    this module never inspects the marker.
  * Both operations are genuine LLM work through the existing
    ``ProviderManager`` (``manager.chat``), exactly like
    ``backend/ai/task_interpreter.py`` does for task interpretation. There is no
    local extractive/frequency/keyword "summarizer" and no heuristic translation.
  * Chunking keeps a single request inside the existing architecture: the
    history is cut into chunks whose size is derived from the project's own
    token estimator, the context budget and the active provider's output budget.
    The map phase is PACED (``MAX_CALL_SPACING_S``) and bounded to
    ``MAP_CONCURRENCY`` calls in flight, so a large history never arrives at a
    provider as a burst — the burst is what used to trip a 429, and the provider
    manager then cools that provider down for 60s, failing the whole operation.
    The whole operation is bounded by a budget derived from the request's own
    envelope (``AIRequest.timeout_s``, threaded through
    ``ToolContext.extra["request_timeout_s"]``).
  * Failures are honest: a retrieval, provider, or budget failure returns
    ``success=False`` with the real reason. Partial work is never presented as a
    completed translation or summary.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, Sequence

from backend.ai.prompt.budget import DEFAULT_MAX_CONTEXT_TOKENS, estimate_tokens
from backend.ai.providers.base.config import ProviderConfig
from backend.services import history_service
from backend.services.history_service import HistoryError, HistoryMessage

logger = logging.getLogger(__name__)

TRANSLATE = "translate"
SUMMARIZE = "summarize"

#: Below this many messages a request is not worth chunking at all.
DEFAULT_COUNT = 100

#: Envelope assumed only when a caller supplies none. It mirrors the handler's
#: long-running execution backstop (``ai_unified._AI_EXECUTE_TIMEOUT``) because
#: these tools are declared ``long_running`` — the authoritative value is always
#: the caller's own (``AIRequest.timeout_s``, threaded through
#: ``ToolContext.extra["request_timeout_s"]``), so a tighter caller stays
#: tighter; this default must not silently refuse work the caller would allow.
DEFAULT_ENVELOPE_S = 240.0
#: Time inside the envelope reserved for retrieval, the initial provider round
#: and final delivery. Only the remainder may be spent on chunked LLM work.
ENVELOPE_RESERVE_S = 20.0
#: Floor for the LLM budget, so a very small envelope still allows one call.
MIN_LLM_BUDGET_S = 20.0
#: Concurrency of the map phase. Bounded to 2 so a large history never puts a
#: burst of simultaneous requests on one provider (the 429 that follows is
#: expensive: ``providers/manager/health.py::DEFAULT_COOLDOWN_SECONDS`` cools
#: that provider down for 60s, which then fails the rest of the operation).
MAP_CONCURRENCY = 2
#: Minimum interval between two map-call starts. This is the rate-limit guard:
#: the old behaviour fired every chunk at once. 5s spacing holds a big request
#: to at most ~12 provider calls per minute, below the per-minute quotas of the
#: providers this project supports (e.g. Gemini free tier 15 RPM).
MAX_CALL_SPACING_S = 5.0
#: Bounded timeout for a single provider call. Mirrors
#: ``backend/ai/task_interpreter.py::INTERPRET_TIMEOUT_SECONDS`` and
#: ``ProviderConfig.timeout`` (30s), the provider HTTP bound.
PER_CALL_TIMEOUT_S = 30.0

#: Tokens of history text per TRANSLATION chunk. A translated chunk is roughly
#: as long as its input, so it is bounded by the provider's configured output
#: budget (``ProviderConfig.max_tokens``, 4096 by default) as well as by the
#: context budget.
TRANSLATE_CHUNK_TOKEN_BUDGET = min(
    DEFAULT_MAX_CONTEXT_TOKENS, ProviderConfig().max_tokens // 2,
)
#: Tokens of history text per SUMMARIZATION chunk. A chunk summary is far
#: shorter than its input, so the binding constraint is the context budget, not
#: the output budget — using it means far fewer provider calls for the same
#: history (which is exactly what keeps a 500/1000-message request under the
#: provider's rate limit).
SUMMARIZE_CHUNK_TOKEN_BUDGET = DEFAULT_MAX_CONTEXT_TOKENS


# ── budget / pacing ───────────────────────────────────────────────────────


def llm_budget(envelope_s: Any) -> float:
    """LLM time available inside a request envelope (never below the floor)."""
    try:
        envelope = float(envelope_s)
    except (TypeError, ValueError):
        envelope = DEFAULT_ENVELOPE_S
    if envelope <= 0:
        envelope = DEFAULT_ENVELOPE_S
    return max(MIN_LLM_BUDGET_S, envelope - ENVELOPE_RESERVE_S)


def max_map_calls(budget_s: float) -> int:
    """Paced provider calls that fit in ``budget_s``.

    One call's worst-case latency is reserved at the end, because the last
    paced call still has to run to completion inside the same budget.
    """
    if budget_s <= PER_CALL_TIMEOUT_S:
        return max(1, int(budget_s / MAX_CALL_SPACING_S))
    return max(1, int((budget_s - PER_CALL_TIMEOUT_S) / MAX_CALL_SPACING_S) + 1)


def call_spacing(calls: int, budget_s: float) -> float:
    """Start interval that fits ``calls`` paced calls inside ``budget_s``."""
    if calls <= 1:
        return 0.0
    room = budget_s - PER_CALL_TIMEOUT_S
    if room <= 0:
        return MAX_CALL_SPACING_S
    return min(MAX_CALL_SPACING_S, room / (calls - 1))

MAX_INSTRUCTION_CHARS = 500

MEDIA_PLACEHOLDER = "[media]"
EMPTY_PLACEHOLDER = "(empty message)"

#: Which failure codes the caller can distinguish. ``success=False`` always
#: carries one of these, so a failure is never confused with an empty history.
ERROR_HISTORY = "history_unavailable"
ERROR_PROVIDER = "ai_provider_failed"
ERROR_TOO_LARGE = "history_too_large_for_one_request"
ERROR_ENGINE = "ai_engine_unavailable"


class HistoryAIError(Exception):
    """Raised inside this module when the operation cannot be completed."""

    def __init__(self, message: str, *, code: str = ERROR_PROVIDER) -> None:
        super().__init__(message)
        self.code = code


# ── planning ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class HistoryPlan:
    """The eligible history for one operation, already bounded and noted."""

    operation: str
    requested: int
    count: int
    capped: bool
    language: str
    instruction: str
    messages: tuple[HistoryMessage, ...]
    truncated: bool

    @property
    def text_messages(self) -> tuple[HistoryMessage, ...]:
        """Messages that actually carry text (media-only ones cannot be translated)."""
        return tuple(m for m in self.messages if m.text.strip())

    def notes(self) -> list[str]:
        """Honest scope notes — never presented as a complete processing claim."""
        notes: list[str] = []
        if self.capped:
            notes.append(
                f"⚠️ The most recent {history_service.MAX_HISTORY_MESSAGES} messages "
                "is the maximum per request."
            )
        if len(self.messages) < self.count:
            notes.append(
                f"⚠️ Only {len(self.messages)} of the requested {self.count} "
                "messages were available"
                + (", and older messages may exist." if self.truncated else ".")
            )
        return notes


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clamp_instruction(value: Any) -> str:
    text = value if isinstance(value, str) else ""
    return " ".join(text.split())[:MAX_INSTRUCTION_CHARS]


async def _prepare(
    source: Any,
    chat_id: Any,
    *,
    operation: str,
    count: Any,
    language: Any = None,
    instruction: Any = None,
    current_message_id: Any = None,
    request_id: str = "",
) -> HistoryPlan:
    """Retrieve the eligible history for one operation through the history service.

    ``current_message_id`` is the Telegram message that triggered this request.
    It becomes the history service's exclusive ``before_id`` cursor, so the
    owner's own command can never be part of the history it asked about.
    """
    bound = history_service.MAX_HISTORY_MESSAGES
    requested = _coerce_int(count, DEFAULT_COUNT)
    if requested <= 0:
        requested = DEFAULT_COUNT
    capped = requested > bound
    effective = min(requested, bound)
    before_id = _coerce_int(current_message_id, 0) or None

    logger.info(
        "AI_EXEC_TRACE request_id=%s stage=history_retrieval_started "
        "operation=%s count=%s before_id=%s",
        request_id or "-", operation, effective, before_id if before_id else "-",
    )
    try:
        slice_ = await history_service.fetch_recent_history(
            source, chat_id, count=effective, before_id=before_id,
        )
    except HistoryError as exc:
        logger.warning(
            "AI_EXEC_TRACE request_id=%s stage=history_retrieval_failed "
            "operation=%s error=%s",
            request_id or "-", operation, exc,
        )
        raise HistoryAIError(str(exc), code=ERROR_HISTORY) from exc
    logger.info(
        "AI_EXEC_TRACE request_id=%s stage=history_retrieval_completed "
        "operation=%s messages=%s truncated=%s",
        request_id or "-", operation, len(slice_.messages), slice_.truncated,
    )

    language_text = _clamp_instruction(language)
    return HistoryPlan(
        operation=operation,
        requested=requested,
        count=effective,
        capped=capped,
        language=language_text,
        instruction=_clamp_instruction(instruction),
        messages=tuple(slice_.messages),
        truncated=slice_.truncated,
    )


# ── chunking ──────────────────────────────────────────────────────────────


def _estimate_tokens(text: str) -> int:
    """Conservative branch of the project's token estimator (2 chars/token).

    ``backend/ai/prompt/budget.py`` documents the heuristic as 4 chars/token for
    English and 2 for non-English, and states the estimate is deliberately
    conservative. Taking the non-English branch for every chunk means a chunk of
    Persian/Arabic or mixed text still fits its budget.
    """
    return max(estimate_tokens(text, "English"), estimate_tokens(text, "Persian"))


def _visible_body(message: HistoryMessage) -> str:
    body = message.text.strip()
    if body:
        return body
    return MEDIA_PLACEHOLDER if message.has_media else EMPTY_PLACEHOLDER


def _attribution(message: HistoryMessage) -> str:
    """Speaker label — same convention as the bounded Telegram context."""
    if message.out:
        return "You"
    return f"User {message.sender_id}" if message.sender_id else "Unknown"


@dataclass(frozen=True)
class _Chunk:
    """One bounded slice of history with its rendered prompt lines.

    Only messages that carry text are ever placed here: a media-only or empty
    message has nothing to translate or summarize, so it is never sent to the
    model (which also removes any chance of the model inventing text for it).
    Its place in the translated output is restored locally from the message
    itself.
    """

    lines: tuple[str, ...]
    message_ids: tuple[int, ...]


def _render_line(message: HistoryMessage, *, with_attribution: bool) -> str:
    body = _visible_body(message)
    if with_attribution:
        return f"[{message.message_id}] {_attribution(message)}: {body}"
    return f"[{message.message_id}] {body}"


def _chunk_history(
    messages: Sequence[HistoryMessage],
    *,
    with_attribution: bool,
    token_budget: int,
) -> list[_Chunk]:
    """Cut the text-bearing messages into chunks that fit ``token_budget``.

    A message is never split and never dropped: a single message larger than the
    budget becomes its own chunk. Order is preserved across chunks.
    """
    chunks: list[_Chunk] = []
    lines: list[str] = []
    ids: list[int] = []
    used = 0

    def _flush() -> None:
        nonlocal lines, ids, used
        if lines:
            chunks.append(_Chunk(lines=tuple(lines), message_ids=tuple(ids)))
        lines, ids, used = [], [], 0

    for message in messages:
        if not message.text.strip():
            continue
        line = _render_line(message, with_attribution=with_attribution)
        cost = _estimate_tokens(line) + 1
        if lines and used + cost > token_budget:
            _flush()
        lines.append(line)
        ids.append(message.message_id)
        used += cost
    _flush()
    return chunks


# ── provider calls ────────────────────────────────────────────────────────


def _failure_reason(response: Any) -> str:
    metadata = getattr(response, "metadata", None) or {}
    category = str(metadata.get("failure_type") or "").strip()
    text = str(getattr(response, "text", "") or "").strip()
    if text and category:
        return f"{category}: {text}"
    return text or category or "unknown provider error"


async def _call(
    manager: Any, *, system: str, payload: str, request_id: str = "",
) -> str:
    """One bounded LLM call through the existing ProviderManager.

    ``tools=[]`` keeps the auxiliary call a plain completion — the history
    operations never request further tool calls.
    """
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": payload},
    ]
    logger.info(
        "AI_EXEC_TRACE request_id=%s stage=provider_call_started", request_id or "-",
    )
    try:
        response = await asyncio.wait_for(
            manager.chat(messages, tools=[]),
            timeout=PER_CALL_TIMEOUT_S,
        )
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError as exc:
        logger.warning(
            "AI_EXEC_TRACE request_id=%s stage=provider_call_failed error=timeout",
            request_id or "-",
        )
        raise HistoryAIError(
            f"the AI provider did not respond within {PER_CALL_TIMEOUT_S:.0f}s",
            code=ERROR_PROVIDER,
        ) from exc
    except Exception as exc:  # noqa: BLE001 — provider mesh boundary
        logger.warning(
            "AI_EXEC_TRACE request_id=%s stage=provider_call_failed error=%s",
            request_id or "-", type(exc).__name__,
        )
        raise HistoryAIError(
            f"the AI provider call failed: {type(exc).__name__}: {exc}",
            code=ERROR_PROVIDER,
        ) from exc

    if not getattr(response, "success", False):
        logger.warning(
            "AI_EXEC_TRACE request_id=%s stage=provider_call_failed error=%s",
            request_id or "-", _failure_reason(response),
        )
        raise HistoryAIError(
            f"the AI provider failed ({_failure_reason(response)})",
            code=ERROR_PROVIDER,
        )
    text = str(getattr(response, "text", "") or "").strip()
    if not text:
        logger.warning(
            "AI_EXEC_TRACE request_id=%s stage=provider_call_failed error=empty_response",
            request_id or "-",
        )
        raise HistoryAIError(
            "the AI provider returned an empty response", code=ERROR_PROVIDER,
        )
    logger.info(
        "AI_EXEC_TRACE request_id=%s stage=provider_call_completed chars=%s",
        request_id or "-", len(text),
    )
    return text


async def _map(
    manager: Any,
    chunks: Sequence[_Chunk],
    worker: Any,
    *,
    budget_s: float,
    request_id: str = "",
) -> list[str]:
    """Run the map phase paced inside the budget (never as one burst).

    Starts are spaced by :func:`call_spacing` so a large history does not put a
    simultaneous wave of requests on one provider; the semaphore still bounds
    how many are in flight at once. Results keep the chunk order.
    """
    semaphore = asyncio.Semaphore(MAP_CONCURRENCY)
    spacing = call_spacing(len(chunks), budget_s)
    loop = asyncio.get_running_loop()
    base = loop.time()
    logger.info(
        "AI_EXEC_TRACE request_id=%s stage=map_planned calls=%s spacing_s=%.2f "
        "budget_s=%.0f concurrency=%s",
        request_id or "-", len(chunks), spacing, budget_s, MAP_CONCURRENCY,
    )

    async def _one(index: int, chunk: _Chunk) -> str:
        if spacing:
            wait = (base + index * spacing) - loop.time()
            if wait > 0:
                await asyncio.sleep(wait)
        async with semaphore:
            return await worker(chunk)

    try:
        results = await asyncio.wait_for(
            asyncio.gather(
                *(_one(index, chunk) for index, chunk in enumerate(chunks)),
                return_exceptions=True,
            ),
            timeout=budget_s,
        )
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError as exc:
        raise HistoryAIError(
            f"the AI work did not finish within {budget_s:.0f}s",
            code=ERROR_PROVIDER,
        ) from exc

    for result in results:
        if isinstance(result, asyncio.CancelledError):
            raise result
    failures = [r for r in results if isinstance(r, BaseException)]
    if failures:
        first = failures[0]
        if isinstance(first, HistoryAIError):
            raise first
        raise HistoryAIError(
            f"the AI provider failed: {type(first).__name__}: {first}",
            code=ERROR_PROVIDER,
        )
    return [str(r) for r in results]


# ── prompt construction ───────────────────────────────────────────────────


_TRANSLATE_SYSTEM = (
    "You are a precise translator working inside a Telegram assistant.\n"
    "Translate the messages the user sends.\n"
    "Rules:\n"
    "- Return EXACTLY one line per input message, in the same order, formatted "
    "as: [id] translation\n"
    "- Copy the numeric id inside the brackets exactly as given. Never invent, "
    "merge, split, reorder or omit a message.\n"
    "- Keep each translation on a single line (no line breaks inside a message).\n"
    "- Translate only; do not answer, comment, explain or add headings, "
    "numbering or notes.\n"
    "- Keep placeholders such as [media] as they are."
)

_SUMMARIZE_SYSTEM = (
    "You write faithful summaries of Telegram conversations.\n"
    "Rules:\n"
    "- Summarize ONLY what the provided messages contain. Never invent facts, "
    "names, numbers, dates or decisions.\n"
    "- Write flowing prose (or a short bullet list when that is clearly more "
    "readable). Do not include message ids, speaker ids, chunk numbers or any "
    "technical detail about your input.\n"
    "- Be concise and specific: cover the topics, decisions, questions and open "
    "items.\n"
    "- Output the summary text only: no preamble, no 'Summary:' heading, no "
    "notes about your process."
)

_REDUCE_SYSTEM = (
    "You merge partial summaries of ONE Telegram conversation into a single "
    "coherent final summary.\n"
    "Rules:\n"
    "- Merge them into one flowing summary; remove repetition, keep concrete "
    "facts, names, numbers, dates, decisions and open questions.\n"
    "- Never invent anything that is not present in the partial summaries.\n"
    "- Do not mention this merge step, the number of parts, or any chunk/"
    "message id.\n"
    "- Output the final summary text only: no preamble, no headings, no notes."
)


def _language_clause(language: str) -> str:
    if language:
        return f"Write the result in {language}."
    return (
        "If no target language was requested, use English. Never leave a message "
        "in a different script than the target."
    )


def _with_instruction(system: str, instruction: str) -> str:
    if not instruction:
        return system
    return f"{system}\n- The owner added this instruction: {instruction}"


def _summarize_payload(chunks: Sequence[str], instruction: str = "") -> str:
    body = "\n\n".join(chunks)
    if instruction:
        return f"Owner instruction: {instruction}\n\n{body}"
    return body


# ── result parsing / assembly ─────────────────────────────────────────────


def _parse_translation(raw: str, expected_ids: Sequence[int]) -> dict[int, str]:
    """Parse ``[id] text`` lines and require every expected id.

    A missing message is an honest failure — never a silently dropped
    translation.
    """
    expected = set(expected_ids)
    parsed: dict[int, str] = {}
    for line in raw.splitlines():
        match = re.match(r"^\s*\[(\d+)\]\s*(.*)$", line)
        if not match:
            continue
        message_id = int(match.group(1))
        body = match.group(2).strip()
        if message_id in expected and body and message_id not in parsed:
            parsed[message_id] = body
    missing = sorted(expected - set(parsed))
    if missing:
        raise HistoryAIError(
            f"the AI provider returned no translation for {len(missing)} of "
            f"{len(expected)} messages",
            code=ERROR_PROVIDER,
        )
    return parsed


def _resolve_manager(provider_manager: Any) -> Any:
    if provider_manager is not None:
        return provider_manager
    from backend.ai.engine.engine import get_engine

    engine = get_engine()
    return getattr(engine, "provider_manager", None) if engine is not None else None


def _result_data(plan: HistoryPlan, *, language: str = "") -> dict[str, Any]:
    """Small structured payload. Never carries message bodies or internals."""
    return {
        "operation": plan.operation,
        "requested": plan.requested,
        "processed": len(plan.messages),
        "text_messages": len(plan.text_messages),
        "truncated": plan.truncated,
        "capped": plan.capped,
        "language": language,
    }


def _finish(plan: HistoryPlan, body: str, language: str = "") -> tuple[bool, str, dict[str, Any]]:
    notes = plan.notes()
    text = body.strip()
    if notes:
        text = f"{text}\n\n" + "\n".join(notes) if text else "\n".join(notes)
    return True, text, _result_data(plan, language=language)


# ── public API ────────────────────────────────────────────────────────────


async def translate_history(
    source: Any,
    chat_id: Any,
    *,
    count: Any = DEFAULT_COUNT,
    language: Any = None,
    instruction: Any = None,
    provider_manager: Any = None,
    current_message_id: Any = None,
    request_id: str = "",
    timeout_s: Any = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Translate the most recent eligible Telegram messages.

    Returns ``(success, text, data)`` — the same shape as
    ``backend/services/web_search_service.do_web_search``. Message ids and
    chronological order are preserved; empty/media-only messages keep their
    place with a placeholder instead of being dropped.

    ``current_message_id`` excludes the requesting command from the history.
    """
    manager = _resolve_manager(provider_manager)
    if manager is None:
        return False, "❌ Translation unavailable — the AI engine is not running.", {
            "error": ERROR_ENGINE,
        }

    try:
        plan = await _prepare(
            source, chat_id, operation=TRANSLATE, count=count,
            language=language, instruction=instruction,
            current_message_id=current_message_id, request_id=request_id,
        )
    except HistoryAIError as exc:
        return False, f"❌ Couldn't read the Telegram history: {exc}", {
            "error": exc.code,
        }

    if not plan.messages:
        return True, "There are no messages to translate in this chat.", _result_data(plan)

    chunks = _chunk_history(
        plan.messages, with_attribution=False,
        token_budget=TRANSLATE_CHUNK_TOKEN_BUDGET,
    )
    budget = llm_budget(timeout_s)
    capacity = max_map_calls(budget)
    if len(chunks) > capacity:
        return False, (
            f"❌ These {len(plan.messages)} messages need {len(chunks)} AI passes, "
            f"more than the {capacity} one request can run while paced to stay "
            "under the provider's rate limits. Try fewer messages."
        ), {"error": ERROR_TOO_LARGE, **_result_data(plan)}

    if not chunks:
        # Messages exist but none has text: keep identity, translate nothing.
        body = "\n".join(f"[{m.message_id}] {_visible_body(m)}" for m in plan.messages)
        return _finish(plan, body)

    system = _with_instruction(
        f"{_TRANSLATE_SYSTEM}\n- {_language_clause(plan.language)}", plan.instruction,
    )

    async def _translate_chunk(chunk: _Chunk) -> str:
        return await _call(
            manager, system=system, payload="\n".join(chunk.lines),
            request_id=request_id,
        )

    try:
        raw_results = await _map(
            manager, chunks, _translate_chunk,
            budget_s=budget, request_id=request_id,
        )
        translated: dict[int, str] = {}
        for chunk, raw in zip(chunks, raw_results, strict=False):
            translated.update(_parse_translation(raw, chunk.message_ids))
    except HistoryAIError as exc:
        return False, f"❌ Translation failed: {exc}", {
            "error": exc.code, **_result_data(plan),
        }

    lines = [
        f"[{m.message_id}] {translated.get(m.message_id) or _visible_body(m)}"
        for m in plan.messages
    ]
    return _finish(plan, "\n".join(lines), language=plan.language)


async def summarize_history(
    source: Any,
    chat_id: Any,
    *,
    count: Any = DEFAULT_COUNT,
    instruction: Any = None,
    provider_manager: Any = None,
    current_message_id: Any = None,
    request_id: str = "",
    timeout_s: Any = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Summarize the most recent eligible Telegram messages with the LLM.

    Hierarchical (map-reduce): each bounded chunk is summarized by the provider,
    then the chunk summaries are merged by one further provider call when the
    history spans more than one chunk. A single-chunk history returns that one
    summary — no fake aggregation step.

    ``current_message_id`` excludes the requesting command from the history.
    """
    manager = _resolve_manager(provider_manager)
    if manager is None:
        return False, "❌ Summarization unavailable — the AI engine is not running.", {
            "error": ERROR_ENGINE,
        }

    try:
        plan = await _prepare(
            source, chat_id, operation=SUMMARIZE, count=count,
            instruction=instruction,
            current_message_id=current_message_id, request_id=request_id,
        )
    except HistoryAIError as exc:
        return False, f"❌ Couldn't read the Telegram history: {exc}", {
            "error": exc.code,
        }

    if not plan.text_messages:
        if plan.messages:
            return True, "None of these messages contains text to summarize.", _result_data(plan)
        return True, "There are no messages to summarize in this chat.", _result_data(plan)

    chunks = _chunk_history(
        plan.messages, with_attribution=True,
        token_budget=SUMMARIZE_CHUNK_TOKEN_BUDGET,
    )
    budget = llm_budget(timeout_s)
    # When a reduce step follows, its own call must fit inside the same budget.
    map_budget = budget - PER_CALL_TIMEOUT_S if len(chunks) > 1 else budget
    capacity = max_map_calls(map_budget)
    if len(chunks) > capacity:
        return False, (
            f"❌ These {len(plan.messages)} messages need {len(chunks)} AI passes, "
            f"more than the {capacity} one request can run while paced to stay "
            "under the provider's rate limits. Try fewer messages."
        ), {"error": ERROR_TOO_LARGE, **_result_data(plan)}

    system = _with_instruction(_SUMMARIZE_SYSTEM, plan.instruction)

    async def _summarize_chunk(chunk: _Chunk) -> str:
        return await _call(
            manager, system=system, payload="\n".join(chunk.lines),
            request_id=request_id,
        )

    try:
        partials = await _map(
            manager, chunks, _summarize_chunk,
            budget_s=map_budget, request_id=request_id,
        )
        if len(partials) == 1:
            final = partials[0]
        else:
            final = await asyncio.wait_for(
                _call(
                    manager,
                    system=_with_instruction(_REDUCE_SYSTEM, plan.instruction),
                    payload=_summarize_payload(partials, plan.instruction),
                    request_id=request_id,
                ),
                timeout=PER_CALL_TIMEOUT_S,
            )
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        return False, (
            f"❌ Summary failed: the final AI step did not finish within "
            f"{PER_CALL_TIMEOUT_S:.0f}s."
        ), {"error": ERROR_PROVIDER, **_result_data(plan)}
    except HistoryAIError as exc:
        return False, f"❌ Summary failed: {exc}", {
            "error": exc.code, **_result_data(plan),
        }

    return _finish(plan, final)
