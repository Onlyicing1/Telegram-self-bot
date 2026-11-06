"""Bounded continuation diagnostics for multi-turn tool-call failures.

A provider can accept a plain chat request and still reject the MULTI-TURN
tool-call continuation the dispatcher sends after a tool has run: one
assistant turn carrying ``tool_calls`` followed by one ``tool`` message per
call. The Model Tester cannot see that difference — it sends a single
``ping`` — so a continuation-only serialization/capability defect stayed
invisible while ordinary model availability looked healthy.

This module answers exactly that question for exactly the pair that failed:
does THIS ``(provider, model)`` accept the continuation contract? It sends
ONE synthetic, fixed conversation through the provider's own adapter, built
from the live provider's own ``ProviderConfig`` (same credential, endpoint
and model the failing request used).

Boundaries (deliberate, and the reason this is not a second Model Tester):

* It never executes a tool and never calls the ``ToolExecutor``; the
  synthetic tool names are not registered anywhere.
* It never reads the owner's message, tool arguments, tool results, Telegram
  state or conversation history, and it never writes history.
* It reports only a classification. It does not touch provider health,
  cooldowns, quarantine, metrics, candidate eligibility or fallback policy —
  ``ProviderManager`` stays the sole routing authority, and no production
  provider instance is used (a separate instance owns the probe's HTTP
  client).
* It is bounded three ways: a single qualifying pair per continuation round,
  a monotonic per-pair cooldown, and single-flight execution with a strict
  timeout. It runs as one existing-primitive task
  (``guarded_create_task``), never as a loop, worker or scheduler, and the
  user's response path is never awaited on it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import replace
from typing import Any

from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.base.contract import NOT_IMPLEMENTED, ProviderResponse
from backend.ai.providers.factory import ProviderFactory
from backend.runtime.task_guard import guarded_create_task

logger = logging.getLogger(__name__)

#: One probe per (provider, model) per half hour — a repeated failure inside
#: this window is logged as skipped, never re-probed.
PROBE_COOLDOWN_SECONDS = 1800.0
#: Strict per-probe timeout. A probe that hangs is a TIMEOUT result, not a
#: lingering task.
PROBE_TIMEOUT_SECONDS = 12.0
#: Output budget for the probe request. 64 tokens is enough for a short
#: acknowledgement or one function call and keeps the diagnostic cheap; the
#: production generation budget is never spent on it.
PROBE_MAX_TOKENS = 64
#: Probe sampling temperature: 0 keeps the diagnostic deterministic, so the
#: same pair answers the same way instead of varying with sampling.
PROBE_TEMPERATURE = 0.0

#: Only these continuation failure categories get a probe. ``auth``,
#: ``rate_limited``, ``model_not_found``, ``timeout``, ``network`` and
#: ``server`` already state what happened, and probing them would spend quota
#: to re-learn the same classification.
QUALIFYING_CATEGORIES = frozenset({"request", "tool_call"})

#: Provider-reported finish reasons that mean generation stopped at the
#: output-token limit. Only an explicit reason from the adapter qualifies:
#: ``length`` for the OpenAI-compatible envelope, ``MAX_TOKENS`` for Gemini.
#: Content-filter reasons (``content_filter``, ``SAFETY``, ``RECITATION``)
#: are deliberately absent, and response length or empty text is never used
#: to infer truncation.
TRUNCATION_FINISH_REASONS = frozenset({"length", "MAX_TOKENS"})

#: Classification vocabulary — the only values this module ever emits.
CONTINUATION_ACCEPTED = "CONTINUATION_ACCEPTED"
CONTINUATION_REJECTED = "CONTINUATION_REJECTED"
#: The probe could not settle the question: it stopped at its output budget,
#: so the answer says nothing about whether the continuation was accepted.
CONTINUATION_INCONCLUSIVE = "CONTINUATION_INCONCLUSIVE"
AUTH_ERROR = "AUTH_ERROR"
RATE_LIMITED = "RATE_LIMITED"
MODEL_NOT_FOUND = "MODEL_NOT_FOUND"
TIMEOUT = "TIMEOUT"
UNSUPPORTED = "UNSUPPORTED"
UNKNOWN = "UNKNOWN"
SKIPPED = "SKIPPED"

#: Synthetic identity. Never a registered tool, never a real request.
SYNTHETIC_TOOL_NAME = "diag_probe"
SYNTHETIC_CALL_IDS = ("diag_call_1", "diag_call_2")
SYNTHETIC_VALUES = ("alpha", "beta")
SYNTHETIC_USER_TEXT = "Diagnostic continuation probe."

#: Per-pair probe state, process-local and bounded by the number of pairs the
#: router can actually fail on (the registered providers' candidate models).
_last_probe_at: dict[tuple[str, str], float] = {}
_in_flight: set[tuple[str, str]] = set()


def synthetic_tool_definition() -> dict[str, Any]:
    """The OpenAI-format tool declaration sent with the probe.

    The production continuation round always carries the request's tool
    definitions, so the probe must carry one too: a provider that rejects
    tool-definition + tool-result combinations only fails when both are
    present.
    """
    return {
        "type": "function",
        "function": {
            "name": SYNTHETIC_TOOL_NAME,
            "description": "Echo a fixed diagnostic value.",
            "parameters": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
        },
    }


def build_synthetic_conversation() -> list[dict[str, Any]]:
    """A fixed two-tool continuation, serialized exactly as production does.

    Mirrors ``Dispatcher._build_continuation_messages``: one assistant turn
    carrying OpenAI-format ``tool_calls`` (``type``/``function.arguments`` as
    a JSON string) and one ``tool`` message per call carrying ``tool_call_id``
    and ``name`` with a JSON result body. Two tool responses are included
    because a provider may accept a single response and reject the parallel
    /multi-part shape.
    """
    return [
        {"role": "user", "content": SYNTHETIC_USER_TEXT},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": SYNTHETIC_TOOL_NAME,
                        "arguments": json.dumps({"value": value}),
                    },
                }
                for call_id, value in zip(SYNTHETIC_CALL_IDS, SYNTHETIC_VALUES, strict=True)
            ],
        },
        *[
            {
                "role": "tool",
                "tool_call_id": call_id,
                "name": SYNTHETIC_TOOL_NAME,
                "content": json.dumps(
                    {
                        "tool": SYNTHETIC_TOOL_NAME,
                        "success": True,
                        "message": "ok",
                        "data": {"value": value},
                        "error": None,
                    }
                ),
            }
            for call_id, value in zip(SYNTHETIC_CALL_IDS, SYNTHETIC_VALUES, strict=True)
        ],
    ]


def classify_probe_response(response: ProviderResponse) -> tuple[str, str]:
    """Map one probe response to ``(result, category)``.

    ``category`` is the provider's own normalized failure category (or a
    fixed token for the cases the taxonomy does not name). Nothing else from
    the response — text, headers, error bodies — is ever carried out.
    """
    metadata = response.metadata or {}
    category = str(metadata.get("failure_type") or "").strip().lower()
    status = metadata.get("http_status")
    if response.success:
        # A capped answer proves nothing either way, so it must not be read as
        # a rejection — nor as acceptance of a contract the model never
        # finished. Only the adapter's own finish reason can say this.
        if str(metadata.get("finish_reason") or "").strip() in TRUNCATION_FINISH_REASONS:
            return CONTINUATION_INCONCLUSIVE, "output_truncated"
        if (response.text or "").strip() == NOT_IMPLEMENTED:
            return UNSUPPORTED, "not_implemented"
        if response.text or response.tool_calls:
            return CONTINUATION_ACCEPTED, ""
        # A success with no text and no tool call proves the continuation was
        # accepted but not answered — never counted as an accepted contract.
        return CONTINUATION_REJECTED, "empty_response"
    if category == "auth" or status in (401, 403):
        return AUTH_ERROR, "auth"
    if category == "rate_limited" or status == 429:
        return RATE_LIMITED, "rate_limited"
    if category == "model_not_found" or status == 404:
        return MODEL_NOT_FOUND, "model_not_found"
    if category == "timeout":
        return TIMEOUT, "timeout"
    if category in QUALIFYING_CATEGORIES:
        return CONTINUATION_REJECTED, category
    return UNKNOWN, category or "unknown"


def reset_state() -> None:
    """Clear the probe bookkeeping (used between isolated diagnostic runs)."""
    _last_probe_at.clear()
    _in_flight.clear()


def _cooldown_remaining(key: tuple[str, str], now: float) -> float:
    started = _last_probe_at.get(key)
    if started is None:
        return 0.0
    return max(0.0, PROBE_COOLDOWN_SECONDS - (now - started))


def _qualifying_failure(response: ProviderResponse) -> tuple[str, str, str] | None:
    """The first failed candidate whose category justifies a probe.

    Read from the manager's own failure matrix — the same record the routing
    decision was made from — so the pair is the one that actually failed, not
    a reconstruction from the final response.
    """
    matrix = (response.metadata or {}).get("provider_matrix")
    if not isinstance(matrix, list):
        return None
    for entry in matrix:
        if not isinstance(entry, dict) or entry.get("outcome") != "failed":
            continue
        category = str(entry.get("category") or "").strip().lower()
        if category not in QUALIFYING_CATEGORIES:
            continue
        provider = str(entry.get("provider") or "")
        model = str(entry.get("model") or "")
        if not provider:
            continue
        return provider, "" if model == "-" else model, category
    return None


def _log_event(
    *,
    request_id: str,
    provider: str,
    model: str,
    result: str,
    category: str,
    latency: float,
    skipped: str = "-",
    level: int = logging.INFO,
) -> None:
    logger.log(
        level,
        "PROVIDER_CONTINUATION_DIAGNOSTIC id=%s provider=%s model=%s result=%s "
        "category=%s latency_s=%.3f max_tokens=%d temperature=%.1f "
        "cooldown_s=%d skipped=%s",
        request_id or "-", provider or "-", model or "-", result,
        category or "-", latency, PROBE_MAX_TOKENS, PROBE_TEMPERATURE,
        int(PROBE_COOLDOWN_SECONDS), skipped or "-",
    )


async def run_continuation_probe(
    provider_manager: Any,
    provider_name: str,
    model: str,
    *,
    request_id: str = "",
    trigger_category: str = "",
) -> str:
    """Probe ONE ``(provider, model)`` continuation contract. Never raises.

    Returns the classification. The probe instance is created from the LIVE
    provider's own config (same credential, endpoint and default model), so
    it talks to the same account the failing request did — without sharing
    that instance's HTTP client.
    """
    key = (provider_name, model)
    started = time.perf_counter()
    result = UNKNOWN
    category = trigger_category
    try:
        instance = _probe_instance(provider_manager, provider_name, model)
        if isinstance(instance, str):
            result = instance
            category = "provider_unavailable"
        else:
            try:
                response = await asyncio.wait_for(
                    _chat(instance, model),
                    timeout=PROBE_TIMEOUT_SECONDS,
                )
                result, category = classify_probe_response(response)
            except asyncio.TimeoutError:
                result, category = TIMEOUT, "timeout"
            except Exception as exc:  # noqa: BLE001
                # The probe's own failure is evidence, never an error path.
                logger.debug(
                    "Continuation probe raised %s for %s/%s",
                    type(exc).__name__, provider_name or "-", model or "-",
                )
                result, category = UNKNOWN, "probe_exception"
            finally:
                _shutdown(instance)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "Continuation probe unavailable for %s/%s: %s",
            provider_name or "-", model or "-", type(exc).__name__,
        )
        result, category = UNKNOWN, "probe_unavailable"
    finally:
        _in_flight.discard(key)
        _log_event(
            request_id=request_id,
            provider=provider_name,
            model=model,
            result=result,
            category=category,
            latency=round(time.perf_counter() - started, 3),
        )
    return result


async def _chat(instance: Any, model: str) -> ProviderResponse:
    kwargs: dict[str, Any] = {"tools": [synthetic_tool_definition()]}
    if model:
        kwargs["model"] = model
    return await instance.chat(build_synthetic_conversation(), **kwargs)


def _probe_instance(
    provider_manager: Any, provider_name: str, model: str,
) -> Any:
    """A separate provider built from the live provider's config, or a result.

    Returns the instance on success, or a classification string when the pair
    cannot be probed at all (nothing was sent).
    """
    registry = getattr(provider_manager, "registry", None)
    live = registry.get(provider_name) if registry is not None else None
    if live is None:
        return UNSUPPORTED
    if getattr(live, "CAPABILITY_KIND", "chat") != "chat":
        return UNSUPPORTED
    config = getattr(live, "config", None)
    if not isinstance(config, ProviderConfig):
        return UNSUPPORTED
    # A COPY: the probe's timeout/HTTP client and generation budget must never
    # be the production instance's, and nothing here mutates the live config.
    probe_config = replace(config, extra=dict(config.extra or {}))
    probe_config.max_tokens = PROBE_MAX_TOKENS
    probe_config.temperature = PROBE_TEMPERATURE
    if model and not probe_config.default_model:
        probe_config.default_model = model
    return ProviderFactory.create_provider(provider_name, probe_config)


def _shutdown(instance: Any) -> None:
    try:
        instance.shutdown()
    except Exception:  # noqa: BLE001
        pass


def schedule_continuation_diagnostic(
    provider_manager: Any,
    response: ProviderResponse,
    *,
    request_id: str = "",
) -> asyncio.Task | None:
    """Schedule at most ONE bounded probe for a qualifying continuation failure.

    Returns the task when a probe was scheduled (callers may ignore it), or
    None when nothing qualified or the pair is inside its cooldown /
    already being probed. Never raises and never blocks the caller.
    """
    try:
        failure = _qualifying_failure(response)
        if failure is None:
            return None
        provider_name, model, category = failure
        key = (provider_name, model)
        now = time.monotonic()
        if key in _in_flight:
            _log_event(
                request_id=request_id, provider=provider_name, model=model,
                result=SKIPPED, category=category, latency=0.0,
                skipped="in_flight", level=logging.DEBUG,
            )
            return None
        remaining = _cooldown_remaining(key, now)
        if remaining > 0:
            _log_event(
                request_id=request_id, provider=provider_name, model=model,
                result=SKIPPED, category=category, latency=0.0,
                skipped="cooldown", level=logging.DEBUG,
            )
            return None
        coro = run_continuation_probe(
            provider_manager, provider_name, model,
            request_id=request_id, trigger_category=category,
        )
        asyncio.get_running_loop()
        _last_probe_at[key] = now
        _in_flight.add(key)
        return guarded_create_task(
            coro, name=f"ai-continuation-diagnostic:{provider_name or 'unknown'}",
        )
    except Exception:  # noqa: BLE001
        # A diagnostic can never affect the request that triggered it.
        try:
            coro.close()  # type: ignore[possibly-undefined]
        except Exception:  # noqa: BLE001
            pass
        return None
