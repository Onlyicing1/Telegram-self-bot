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
    ``ToolContext.extra["request_timeout_s"]``). Inside that budget every single
    provider call is bounded by :func:`call_timeout` — the provider safety
    ceiling intersected with the time left in the operation — so no step of a
    long operation is cut back to the bare provider HTTP timeout and no call is
    ever unbounded.
  * Failures are honest: a retrieval, provider, or budget failure returns
    ``success=False`` with the real reason. Partial work is never presented as a
    completed translation or summary.
"""
from __future__ import annotations
from backend.helper import symbols as sym

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, Sequence

from backend.ai.prompt.budget import (
    DEFAULT_MAX_CONTEXT_TOKENS,
    estimate_tokens_script_aware,
)
from backend.ai.providers.base.config import ProviderConfig
from backend.services import history_policy, history_service
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
#: Latency assumed for ONE provider call when pacing the map phase and sizing
#: how many calls fit a budget. Mirrors
#: ``backend/ai/task_interpreter.py::INTERPRET_TIMEOUT_SECONDS`` and
#: ``ProviderConfig.timeout`` (30s), the provider HTTP bound — i.e. a typical
#: call. This is a PACING assumption, not the bound a call actually runs under:
#: that is :func:`call_timeout`.
ASSUMED_CALL_LATENCY_S = 30.0
#: Finite safety ceiling for ONE provider call, derived from the operation's own
#: default envelope (half of it): a single call may never consume the whole
#: operation, but it must be allowed to exceed the provider HTTP bound, because
#: ``ProviderManager.chat`` walks a model-level candidate chain — every candidate
#: contributing its configured model and then a retry — so one successful call's
#: wall time is not one HTTP timeout. The effective per-call timeout is
#: ``min(this, time left in the operation)``.
PROVIDER_CALL_SAFETY_TIMEOUT_S = DEFAULT_ENVELOPE_S / 2
#: A provider call is not worth starting with less time left than one pacing
#: interval: the operation fails honestly instead of starting work it cannot
#: finish before its deadline.
MIN_PROVIDER_CALL_TIMEOUT_S = MAX_CALL_SPACING_S

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
    if budget_s <= ASSUMED_CALL_LATENCY_S:
        return max(1, int(budget_s / MAX_CALL_SPACING_S))
    return max(1, int((budget_s - ASSUMED_CALL_LATENCY_S) / MAX_CALL_SPACING_S) + 1)


def call_spacing(calls: int, budget_s: float) -> float:
    """Start interval that fits ``calls`` paced calls inside ``budget_s``."""
    if calls <= 1:
        return 0.0
    room = budget_s - ASSUMED_CALL_LATENCY_S
    if room <= 0:
        return MAX_CALL_SPACING_S
    return min(MAX_CALL_SPACING_S, room / (calls - 1))


def llm_deadline(budget_s: Any) -> float:
    """Monotonic deadline for an LLM phase that starts now."""
    try:
        budget = float(budget_s)
    except (TypeError, ValueError):
        budget = MIN_LLM_BUDGET_S
    return asyncio.get_running_loop().time() + max(0.0, budget)


def remaining_s(deadline: float | None) -> float | None:
    """Seconds left before ``deadline``; ``None`` when there is no deadline."""
    if deadline is None:
        return None
    return max(0.0, float(deadline) - asyncio.get_running_loop().time())


def map_call_capacity(budget_s: float) -> int:
    """Provider calls one map phase can actually FINISH inside ``budget_s``.

    Two independent constraints, and the smaller one wins:

    * **Pacing.** Starts are at most ``MAX_CALL_SPACING_S`` apart, so the last
      start plus one planned call must fit (``max_map_calls``).
    * **Concurrency.** At most ``MAP_CONCURRENCY`` calls run at once; a call is
      planned at ``ASSUMED_CALL_LATENCY_S``, so the phase can complete at most
      ``MAP_CONCURRENCY`` calls per planning interval. Scheduled start times
      alone are not a completion bound: N starts five seconds apart do not mean
      N completions by the deadline when only two calls run concurrently.

    ``ASSUMED_CALL_LATENCY_S`` is a PLANNING value (the provider HTTP bound, the
    same one pacing uses), not a guarantee: the hard guarantee stays
    :func:`call_timeout`'s bound and the phase deadline itself. A call slower
    than planned consumes the remaining budget and fails the operation honestly
    instead of silently overshooting it.
    """
    try:
        budget = float(budget_s)
    except (TypeError, ValueError):
        budget = 0.0
    paced = max_map_calls(budget)
    if budget <= 0:
        return 1
    concurrent = MAP_CONCURRENCY * int(budget // ASSUMED_CALL_LATENCY_S)
    return max(1, min(paced, concurrent))


def required_ai_calls(chunk_count: int) -> int:
    """Provider calls ONE history operation makes: a call per part, plus a merge.

    One chunk needs no merge. Zero chunks (nothing with text) make no call.
    ``ProviderManager``'s retries and fallbacks stay inside a single call and
    are never counted as separate History-level passes.
    """
    if not isinstance(chunk_count, int) or chunk_count <= 0:
        return 0
    return chunk_count + (1 if chunk_count > 1 else 0)


def _affordable_message_count(
    messages: Sequence[HistoryMessage],
    *,
    with_attribution: bool,
    token_budget: int,
    with_ids: bool,
    capacity: int,
) -> int:
    """Largest leading message count whose chunk count fits ``capacity``.

    Used only to make a refusal actionable; it runs on the already-fetched
    history (no Telegram read, no provider call) and reuses the exact chunker,
    so the number it reports is the one the gate would accept.
    """
    limit = max(1, int(capacity))
    low, high = 0, len(messages)
    while low < high:
        middle = (low + high + 1) // 2
        chunks = _chunk_history(
            messages[:middle], with_attribution=with_attribution,
            token_budget=token_budget, with_ids=with_ids,
        )
        if len(chunks) <= limit:
            low = middle
        else:
            high = middle - 1
    return low


def _capacity_refusal(
    plan: HistoryPlan,
    *,
    chunks: int,
    reduce_calls: int,
    capacity: int,
    budget_s: float,
    with_attribution: bool,
    token_budget: int,
    with_ids: bool,
) -> str:
    """Honest, actionable refusal naming the REAL constraint.

    The limit is the operation's own AI time budget under the existing pacing
    and concurrency, not a provider rate-limit quota (no RPM/RPD value is read
    anywhere). The text also states how much work one request can hold.
    """
    needed = chunks + reduce_calls
    parts = f"{chunks} to cover the parts"
    if reduce_calls:
        parts += f" + {reduce_calls} to merge them"
    affordable = _affordable_message_count(
        plan.messages, with_attribution=with_attribution,
        token_budget=token_budget, with_ids=with_ids, capacity=capacity,
    )
    return (
        f"{sym.STATE_FAILED} These {len(plan.messages)} messages need {needed} AI calls "
        f"({parts}), more than the {capacity} one request can finish inside its "
        f"{budget_s:.0f}s AI budget while staying paced at one call every "
        f"{MAX_CALL_SPACING_S:g}s with {MAP_CONCURRENCY} in flight. "
        f"About {affordable} messages fit in one request."
    )


def call_timeout(deadline: float | None) -> float:
    """Effective timeout for ONE provider call.

    ``min(provider safety ceiling, time left in the operation)`` — finite in both
    directions: a genuinely stuck HTTP/provider operation can never wait forever,
    and a step of a long operation is never cut back to the provider HTTP timeout
    while the operation still has minutes of its envelope left.
    """
    left = remaining_s(deadline)
    if left is None:
        return PROVIDER_CALL_SAFETY_TIMEOUT_S
    return min(PROVIDER_CALL_SAFETY_TIMEOUT_S, left)

MAX_INSTRUCTION_CHARS = 500

MEDIA_PLACEHOLDER = "[media]"
EMPTY_PLACEHOLDER = "(empty message)"

#: Which failure codes the caller can distinguish. ``success=False`` always
#: carries one of these, so a failure is never confused with an empty history.
ERROR_HISTORY = "history_unavailable"
ERROR_PROVIDER = "ai_provider_failed"
ERROR_TOO_LARGE = "history_too_large_for_one_request"
ERROR_ENGINE = "ai_engine_unavailable"
#: The requested count is not permitted for this chat's peer type. This is a
#: PRODUCT policy refusal, distinct from retrieval and AI-capacity failures.
ERROR_POLICY = "history_peer_policy"


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
                f"{sym.STATE_WARNING} The most recent {history_service.MAX_HISTORY_MESSAGES} messages "
                "is the maximum per request."
            )
        if len(self.messages) < self.count:
            notes.append(
                f"{sym.STATE_WARNING} Only {len(self.messages)} of the requested {self.count} "
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

    # Product policy runs BEFORE any Telegram read or provider call: the chat's
    # CURRENT entity is resolved and classified, then the count is validated
    # against its peer type. The 1,000-message global bound above keeps its
    # explicit "capped" note (never a silent clamp); the per-peer cap is NEVER
    # clamped — an over-limit request is refused here.
    try:
        peer_type = await history_service.classify_chat(source, chat_id)
    except HistoryError as exc:
        logger.warning(
            "AI_EXEC_TRACE request_id=%s stage=history_peer_resolve_failed "
            "operation=%s error=%s",
            request_id or "-", operation, exc,
        )
        raise HistoryAIError(str(exc), code=ERROR_HISTORY) from exc
    refusal = history_policy.validate_count(peer_type, effective)
    if refusal is not None:
        logger.warning(
            "AI_EXEC_TRACE request_id=%s stage=history_peer_policy_refused "
            "operation=%s peer_type=%s count=%s",
            request_id or "-", operation, peer_type.value, effective,
        )
        raise HistoryAIError(refusal, code=ERROR_POLICY)

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
    """Script-aware conservative estimate (see ``estimate_tokens_script_aware``).

    Latin-only text uses the documented 4 chars/token ratio; any non-ASCII
    character (Persian, Arabic, mixed script, accented Latin, emoji) keeps the
    2 chars/token worst case. The chunk budget itself is unchanged — only the
    number of characters that fit inside it is measured correctly.
    """
    return estimate_tokens_script_aware(text)


def _visible_body(message: HistoryMessage) -> str:
    body = message.text.strip()
    if body:
        return body
    return MEDIA_PLACEHOLDER if message.has_media else EMPTY_PLACEHOLDER


def scheduled_summary_payload(
    messages: Sequence[HistoryMessage], *, instruction: Any = "",
) -> str:
    """The ONLY text a scheduled History summarization may send to a model.

    Message bodies and the owner-authored instruction, nothing else: no chat,
    peer, sender or message identifiers, no sender attribution, no dates, no
    media/task/occurrence/database metadata and no unrelated history. Empty and
    media-only messages contribute nothing (there is no text to summarize).
    Identifiers that the owner themselves typed INSIDE a message body are
    content and are not semantically redacted.
    """
    body = "\n\n".join(
        message.text.strip() for message in messages if message.text.strip()
    )
    instruction_text = _clamp_instruction(instruction)
    if instruction_text and body:
        return f"Owner instruction: {instruction_text}\n\n{body}"
    if instruction_text:
        return f"Owner instruction: {instruction_text}"
    return body


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


def _render_line(
    message: HistoryMessage, *, with_attribution: bool, with_id: bool = True,
) -> str:
    body = _visible_body(message)
    prefix = f"[{message.message_id}] " if with_id else ""
    if with_attribution:
        return f"{prefix}{_attribution(message)}: {body}"
    return f"{prefix}{body}"


def _chunk_history(
    messages: Sequence[HistoryMessage],
    *,
    with_attribution: bool,
    token_budget: int,
    with_ids: bool = True,
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
        line = _render_line(
            message, with_attribution=with_attribution, with_id=with_ids,
        )
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
    manager: Any,
    *,
    system: str,
    payload: str,
    request_id: str = "",
    deadline: float | None = None,
) -> str:
    """One LLM call through the existing ProviderManager, bounded by ``deadline``.

    ``tools=[]`` keeps the auxiliary call a plain completion — the history
    operations never request further tool calls. The bound is
    :func:`call_timeout`: the provider safety ceiling intersected with the time
    left in the operation — never unlimited, never a bare HTTP timeout.
    """
    timeout = call_timeout(deadline)
    if timeout < MIN_PROVIDER_CALL_TIMEOUT_S:
        logger.warning(
            "AI_EXEC_TRACE request_id=%s stage=provider_call_failed "
            "error=deadline_exhausted timeout_s=%.1f",
            request_id or "-", timeout,
        )
        raise HistoryAIError(
            "the AI work did not finish: the request's time budget ran out "
            "before this AI provider call",
            code=ERROR_PROVIDER,
        )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": payload},
    ]
    logger.info(
        "AI_EXEC_TRACE request_id=%s stage=provider_call_started timeout_s=%.1f",
        request_id or "-", timeout,
    )
    try:
        response = await asyncio.wait_for(
            manager.chat(messages, tools=[]),
            timeout=timeout,
        )
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError as exc:
        logger.warning(
            "AI_EXEC_TRACE request_id=%s stage=provider_call_failed error=timeout "
            "timeout_s=%.1f",
            request_id or "-", timeout,
        )
        raise HistoryAIError(
            f"the AI provider did not respond within {timeout:g}s",
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
    first = plan.messages[0].message_id if plan.messages else None
    last = plan.messages[-1].message_id if plan.messages else None
    return {
        "operation": plan.operation,
        "requested": plan.requested,
        "processed": len(plan.messages),
        "text_messages": len(plan.text_messages),
        "truncated": plan.truncated,
        "capped": plan.capped,
        "language": language,
        # The exact eligible range the operation covered, for callers that must
        # report it (the History & Intelligence panel). Additive metadata: no
        # message body, sender identity or peer id is exposed here.
        "first_message_id": first,
        "last_message_id": last,
    }


def _prepare_failure(exc: HistoryAIError) -> tuple[bool, str, dict[str, Any]]:
    """The honest result for a planning failure.

    A product-policy refusal is not a retrieval failure and must not be worded
    as one: the chat was read fine, its type simply does not permit that count.
    """
    if exc.code == ERROR_POLICY:
        return False, f"{sym.STATE_FAILED} {exc}", {"error": exc.code}
    return False, f"{sym.STATE_FAILED} Couldn't read the Telegram history: {exc}", {
        "error": exc.code,
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
        return False, f"{sym.STATE_FAILED} Translation unavailable — the AI engine is not running.", {
            "error": ERROR_ENGINE,
        }

    try:
        plan = await _prepare(
            source, chat_id, operation=TRANSLATE, count=count,
            language=language, instruction=instruction,
            current_message_id=current_message_id, request_id=request_id,
        )
    except HistoryAIError as exc:
        return _prepare_failure(exc)

    if not plan.messages:
        return True, "There are no messages to translate in this chat.", _result_data(plan)

    chunks = _chunk_history(
        plan.messages, with_attribution=False,
        token_budget=TRANSLATE_CHUNK_TOKEN_BUDGET,
    )
    budget = llm_budget(timeout_s)
    deadline = llm_deadline(budget)
    capacity = map_call_capacity(budget)
    if len(chunks) > capacity:
        return False, _capacity_refusal(
            plan, chunks=len(chunks), reduce_calls=0, capacity=capacity,
            budget_s=budget, with_attribution=False,
            token_budget=TRANSLATE_CHUNK_TOKEN_BUDGET, with_ids=True,
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
            request_id=request_id, deadline=deadline,
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
        return False, f"{sym.STATE_FAILED} Translation failed: {exc}", {
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
    with_identifiers: bool = True,
) -> tuple[bool, str, dict[str, Any]]:
    """Summarize the most recent eligible Telegram messages with the LLM.

    Hierarchical (map-reduce): each bounded chunk is summarized by the provider,
    then the chunk summaries are merged by one further provider call when the
    history spans more than one chunk. A single-chunk history returns that one
    summary — no fake aggregation step.

    ``current_message_id`` excludes the requesting command from the history.
    ``with_identifiers=False`` is the deterministic UI mode: the provider gets
    only message text and the owner's instruction, never message ids or sender
    ids. The default ``True`` preserves every existing caller's prompt.
    """
    manager = _resolve_manager(provider_manager)
    if manager is None:
        return False, f"{sym.STATE_FAILED} Summarization unavailable — the AI engine is not running.", {
            "error": ERROR_ENGINE,
        }

    try:
        plan = await _prepare(
            source, chat_id, operation=SUMMARIZE, count=count,
            instruction=instruction,
            current_message_id=current_message_id, request_id=request_id,
        )
    except HistoryAIError as exc:
        return _prepare_failure(exc)

    if not plan.text_messages:
        if plan.messages:
            return True, "None of these messages contains text to summarize.", _result_data(plan)
        return True, "There are no messages to summarize in this chat.", _result_data(plan)

    chunks = _chunk_history(
        plan.messages, with_attribution=with_identifiers,
        token_budget=SUMMARIZE_CHUNK_TOKEN_BUDGET,
        with_ids=with_identifiers,
    )
    budget = llm_budget(timeout_s)
    # One deadline for the whole operation; every call is bounded by what is left
    # of it (capped by the provider safety ceiling in :func:`call_timeout`).
    deadline = llm_deadline(budget)
    # When a reduce step follows, the map phase stops one call short of the
    # deadline so the reduce still has room — the reduce itself is NOT capped at
    # that reservation: it may use the remaining budget up to the ceiling.
    map_budget = budget - ASSUMED_CALL_LATENCY_S if len(chunks) > 1 else budget
    map_deadline = min(deadline, llm_deadline(map_budget))
    # One merge call after the map phase when the history spans >1 chunk; its
    # time is already reserved by ``map_budget`` above. The gate counts it, so
    # the number in the message is the number of provider calls the operation
    # actually makes (retries/fallbacks stay inside one call).
    reduce_calls = 1 if len(chunks) > 1 else 0
    capacity = map_call_capacity(map_budget)
    if len(chunks) > capacity:
        return False, _capacity_refusal(
            plan, chunks=len(chunks), reduce_calls=reduce_calls, capacity=capacity,
            budget_s=map_budget, with_attribution=with_identifiers,
            token_budget=SUMMARIZE_CHUNK_TOKEN_BUDGET, with_ids=with_identifiers,
        ), {"error": ERROR_TOO_LARGE, **_result_data(plan)}

    system = _with_instruction(_SUMMARIZE_SYSTEM, plan.instruction)

    async def _summarize_chunk(chunk: _Chunk) -> str:
        return await _call(
            manager, system=system, payload="\n".join(chunk.lines),
            request_id=request_id, deadline=map_deadline,
        )

    try:
        partials = await _map(
            manager, chunks, _summarize_chunk,
            budget_s=map_budget, request_id=request_id,
        )
        if len(partials) == 1:
            final = partials[0]
        else:
            # Bounded by the operation's remaining time (up to the provider
            # safety ceiling) — not by one call's assumed latency.
            final = await _call(
                manager,
                system=_with_instruction(_REDUCE_SYSTEM, plan.instruction),
                payload=_summarize_payload(partials, plan.instruction),
                request_id=request_id,
                deadline=deadline,
            )
    except asyncio.CancelledError:
        raise
    except HistoryAIError as exc:
        return False, f"{sym.STATE_FAILED} Summary failed: {exc}", {
            "error": exc.code, **_result_data(plan),
        }

    return _finish(plan, final)
