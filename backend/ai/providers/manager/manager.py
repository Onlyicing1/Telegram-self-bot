"""
ProviderManager — the sole intermediary between the AI Engine and
the provider layer.

The Conversation Manager, Dispatcher, and Prompt Builder NEVER call
a provider directly. They call ``ProviderManager.chat()`` and receive
a ``ProviderResponse``. The manager:

  1. Gets the active provider from the registry.
  2. Validates it (health + enabled).
  3. If unhealthy → uses the fallback provider.
  4. Calls ``provider.chat()`` inside a guarded await.
  5. If the call crashes OR returns ``success=False`` (429/500/quota/
     invalid request/...), the configured fallback chain is tried, then
     the emergency fallback. The emergency fallback ALWAYS returns
     ``success=False`` with the original failures preserved — never fake
     success.
  6. Records latency + success/failure in per-provider metrics.

The manager also exposes:
  - ``switch_provider(name)``  → switch the active provider
  - ``register_provider(p)``   → add a new provider at runtime
  - ``unregister_provider(name)``→ remove a provider
  - ``get_active_name()``      → current provider name
  - ``list_providers()``       → all registered names
  - ``provider_health(name)``  → health dict
  - ``metrics_snapshot()``     → all provider metrics
  - ``capabilities(name)``     → ProviderCapabilities for a provider
  - ``get_provider_config(name)`` → ProviderConfig for a provider
  - ``update_provider_config(name, field, value)`` → update + validate
  - ``reset_provider_config(name)`` → reset to defaults
  - ``validate_provider(name)`` → ValidationResult
  - ``export_configs()``       → all configs as dicts

The manager never crashes. Every exception is caught and converted
into a fallback response.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from typing import Any, Iterator

from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.base.contract import BaseProvider, ProviderResponse
from backend.ai.providers.base.defaults import resolve_model
from backend.ai.providers.base.exceptions import ProviderUnavailable
from backend.ai.providers.manager.config_manager import ProviderConfigManager
from backend.ai.providers.manager.health import (
    AUTH_FAILURES,
    DEFAULT_COOLDOWN_SECONDS,
    RETRYABLE_FAILURES,
    ProviderHealthTracker,
)
from backend.ai.providers.manager.metrics import ProviderMetricsRegistry
from backend.ai.providers.registry.registry import ProviderRegistry
from backend.runtime.operation_watchdog import guarded_await

logger = logging.getLogger(__name__)

_PROVIDER_RPC_TIMEOUT = 30.0
_MAX_RETRIES = 1
_RETRY_BACKOFF_SECONDS = 1.0
_DEFAULT_CONCURRENCY = 4
_PROVIDER_CONCURRENCY: dict[str, int] = {"zai": 2}
_MODEL_UNAVAILABLE_TTL = 3600.0


class ProviderManager:
    """Manages provider lifecycle, routing, fallback, and metrics.

    The manager is the ONLY object that calls ``provider.chat()``.
    All other layers call ``manager.chat()``.
    """

    __slots__ = (
        "_registry", "_metrics", "_config_mgr", "_fallback_chain", "_health",
        "_unavailable_models",
    )

    def __init__(self, registry: ProviderRegistry | None = None) -> None:
        self._registry = registry or ProviderRegistry()
        self._metrics = ProviderMetricsRegistry()
        self._config_mgr = ProviderConfigManager()
        self._fallback_chain: list[str] = []
        self._health = ProviderHealthTracker(
            default_concurrency=_DEFAULT_CONCURRENCY,
            concurrency_overrides=_PROVIDER_CONCURRENCY,
        )
        self._unavailable_models: dict[tuple[str, str], float] = {}
        self._ensure_dummy_fallback()
        self._load_env_fallback_chain()

    # ── Public API ──

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> ProviderResponse:
        """Route a request through the provider mesh. Never raises.

        Eligible providers (the active provider first, then the configured
        fallback chain, then any other registered provider) are scored by
        capability match, health, and recent reliability, and tried in score
        order until one succeeds. Skipped and failed providers are recorded
        in a failure matrix attached to the returned response metadata so the
        full routing decision is observable without exposing secrets.

        A successful fallback response carries ``fallback`` / ``fallback_from``
        / ``fallback_to`` metadata. Each candidate is given at most ONE
        immediate retry for transient errors and is marked cooling_down,
        quarantined, or disabled on failure so a dead provider is never
        retried forever.
        """
        needs_tools = bool(kwargs.get("tools"))
        candidates = self._ordered_candidates(include_all=True)

        # Terminal case: no real provider configured — return the dummy's
        # own "not configured" response instead of a fallback synthesis.
        if candidates and candidates[0][0] == "dummy":
            return await self._call_once(candidates[0][1], messages, kwargs)

        first = candidates[0][0] if candidates else ""
        matrix: list[dict[str, Any]] = []
        failures: list[str] = []
        eligible: list[tuple[float, str, BaseProvider]] = []

        for name, provider in candidates:
            if name == "dummy":
                continue
            skip = self._skip_reason(name, provider, needs_tools)
            if skip:
                matrix.append({"provider": name, "outcome": "skipped", "reason": skip})
                logger.info("PROVIDER_SKIPPED provider=%s reason=%s", name, skip)
                continue
            score = self._score(name, provider)
            eligible.append((score, name, provider))
            logger.info("ROUTER_SCORE provider=%s score=%.3f", name, score)

        # The active provider is the user's explicit choice — keep it first
        # when eligible; the rest are ordered by score (capability ×
        # reliability × availability × latency). Deterministic and stable.
        eligible.sort(key=lambda item: (item[1] != first, -item[0]))

        for idx, (score, name, provider) in enumerate(eligible):
            model = self._effective_model(provider) or "-"
            logger.info("ROUTER_SELECTED provider=%s model=%s score=%.3f", name, model, score)
            response = await self._attempt_with_retry(provider, messages, kwargs)
            if response.success:
                # A semantically-empty or all-malformed "success" is not a
                # usable execution answer. When another eligible provider is
                # healthy, fail over to it for THIS request — bounded and
                # deterministic, and nothing has executed yet (no tool has
                # run), so a destructive action can never double-run. The
                # provider is NOT cooled down: an empty/stalled model output
                # is a request-level quality signal, penalized via the
                # quality metrics; a single provider keeps the dispatcher's
                # own bounded nudge-retry as its recovery path.
                no_output = self._has_no_usable_output(response)
                if no_output and idx + 1 < len(eligible):
                    category = "STRUCTURED_OUTPUT" if (response.tool_calls or []) else "EMPTY_RESPONSE"
                    matrix.append({"provider": name, "outcome": category.lower(), "score": round(score, 3)})
                    logger.info(
                        "AI_PROVIDER_FAILURE provider=%s model=%s category=%s failover=1",
                        name, model, category,
                    )
                    continue
                matrix.append({"provider": name, "outcome": "success", "score": round(score, 3)})
                if name != first:
                    meta = dict(response.metadata or {})
                    meta["fallback"] = True
                    meta["fallback_from"] = first or None
                    meta["fallback_to"] = name
                    from_provider = self._registry.get(first) if first else None
                    from_model = self._effective_model(from_provider) if from_provider is not None else "-"
                    logger.info(
                        "AI_PROVIDER_FAILOVER from=%s/%s to=%s/%s",
                        first or "-", from_model, name, model,
                    )
                    response = replace(response, metadata=meta)
                meta = dict(response.metadata or {})
                meta["provider_matrix"] = matrix
                return replace(response, metadata=meta)
            matrix.append({"provider": name, "outcome": "failed", "reason": (response.text or "")[:200]})
            failures.append(f"{name}: success=False ({response.text[:200]})")

        return await self._fallback(messages, failures=failures, matrix=matrix, **kwargs)

    def vision(self, messages: list[dict[str, Any]], images: list[bytes], **kwargs: Any) -> ProviderResponse:
        """Send a vision request. Never raises."""
        provider = self._get_healthy_provider()
        provider_name = provider.name
        start = time.perf_counter()
        try:
            response = provider.vision(messages, images, **kwargs)
            latency = time.perf_counter() - start
            self._metrics.record(provider_name, latency=latency, error="")
            return response
        except Exception as exc:
            latency = time.perf_counter() - start
            self._metrics.record(provider_name, latency=latency, error=str(exc))
            logger.warning("ProviderManager: '%s' crashed during vision: %s", provider_name, exc)
            return self._fallback_vision(messages, images, **kwargs)

    def stream(self, messages: list[dict[str, Any]], **kwargs: Any) -> Iterator[ProviderResponse]:
        """Stream a chat response. Falls back to dummy on error."""
        provider = self._get_healthy_provider()
        try:
            yield from provider.stream(messages, **kwargs)
        except Exception as exc:
            logger.warning("ProviderManager: '%s' crashed during stream: %s", provider.name, exc)
            self._metrics.record(provider.name, latency=0.0, error=str(exc))
            yield from self._fallback_stream(messages, **kwargs)

    def count_tokens(self, text: str) -> int:
        """Estimate token count using the active provider. Never raises."""
        try:
            return self._get_healthy_provider().count_tokens(text)
        except Exception:
            return max(1, len(text) // 4)

    # ── Provider management ──

    def register_provider(self, provider: BaseProvider) -> bool:
        return self._registry.register(provider)

    def unregister_provider(self, name: str) -> bool:
        return self._registry.unregister(name)

    def switch_provider(self, name: str) -> bool:
        if not self._registry.has(name):
            logger.warning("ProviderManager: cannot switch to '%s' — not registered", name)
            return False
        return self._registry.switch_provider(name)

    def apply_selection(self, provider: str, model: str = "") -> bool:
        """Switch the active provider and apply the selected model to the
        runtime provider instance in one call.

        This is the single path UI selections (web + glass) and the chat
        entry points use so the (provider, model) pair the user picks is
        exactly what the runtime sends at request time. The model is
        written to the registered provider's OWN config object (the
        authoritative one) — never a divergent copy.
        """
        if not provider or not self._registry.has(provider):
            logger.warning("ProviderManager: apply_selection: provider '%s' not registered", provider)
            return False
        self._registry.switch_provider(provider)
        if model:
            config = self._config_for(provider)
            if config is not None:
                config.default_model = model
                logger.info("ProviderManager: applied model '%s' to runtime provider '%s'", model, provider)
        return True

    def get_active_name(self) -> str:
        return self._registry.active_name

    def get_active(self) -> BaseProvider:
        return self._registry.get_active()

    def list_providers(self) -> list[str]:
        return self._registry.list()

    def list_metadata(self) -> list[dict[str, Any]]:
        return self._registry.list_metadata()

    def provider_health(self, name: str) -> dict[str, Any]:
        return self._registry.health_status(name)

    def capabilities(self, name: str | None = None) -> Any:
        from backend.ai.providers.base.capabilities import ProviderCapabilities
        provider = self._registry.get(name) if name else self._get_healthy_provider()
        if provider is None:
            return ProviderCapabilities()
        return provider.capabilities

    def metrics_snapshot(self) -> dict[str, dict[str, Any]]:
        return self._metrics.snapshot()

    @property
    def registry(self) -> ProviderRegistry:
        return self._registry

    @property
    def config_manager(self) -> ProviderConfigManager:
        return self._config_mgr

    def _config_for(self, name: str) -> ProviderConfig:
        """Return the ONE authoritative ProviderConfig for a provider.

        Registered providers own the config instance used at request time
        (``provider.config``). The config manager must never hold a
        divergent copy of a registered provider's config, otherwise a
        model selected through the UI never reaches the runtime request.
        Unregistered (default-only) providers fall back to the config
        manager so discovery/status still has something to read.
        """
        provider = self._registry.get(name)
        if provider is not None:
            return provider.config
        return self._config_mgr.get_config(name)

    def get_provider_config(self, name: str | None = None) -> ProviderConfig:
        """Return the authoritative ProviderConfig (active if name is None).

        This is the SINGLE source of truth for a provider's runtime
        configuration — the same object the provider instance reads when
        it builds a request. Model/provider selection must go through this
        so the runtime always uses the selected (provider, model) pair.
        """
        if name is None:
            name = self._registry.active_name
        return self._config_for(name)

    def update_provider_config(self, name: str, field: str, value: Any) -> Any:
        """Update a provider config field. Returns the ValidationResult."""
        return self._config_mgr.update(name, field, value, config=self._config_for(name))

    def reset_provider_config(self, name: str) -> ProviderConfig:
        """Reset a provider's config to factory defaults."""
        return self._config_mgr.reset(name, config=self._config_for(name))

    def validate_provider(self, name: str) -> Any:
        """Validate a provider's config. Returns ValidationResult."""
        return self._config_mgr.validate(name, config=self._config_for(name))

    def export_configs(self) -> dict[str, dict[str, Any]]:
        """Export all provider configs as dicts."""
        return {
            name: self._config_for(name).as_dict()
            for name in sorted(set(list(self._config_mgr.list_provider_names()) + self._registry.list()))
        }

    # ── Internal ──

    def _get_healthy_provider(self) -> BaseProvider:
        """Return the first healthy, available provider (active → chain → fallback)."""
        for name, provider in self._ordered_candidates():
            if name == "dummy":
                continue
            if self._health.is_available(name) and self._provider_healthy(provider):
                return provider
        logger.warning("ProviderManager: no healthy provider available — using emergency fallback")
        return self._registry.get_fallback()

    # ── Routing helpers ──

    def _ordered_candidates(self, include_all: bool = False) -> list[tuple[str, BaseProvider]]:
        """Return ordered ``(name, provider)`` pairs: active first, then chain.

        The active provider is always included (obtained via
        ``registry.get_active()``, which is also the interface a mock registry
        exposes). The dummy provider is never part of the fallback chain — it
        is only reached as the terminal emergency fallback via ``_fallback()``.

        When ``include_all`` is True, every other registered non-dummy
        provider is appended as an additional safety net so a mesh with a
        healthy-but-unconfigured-fallback provider can still route to it.
        """
        candidates: list[tuple[str, BaseProvider]] = []
        active = self._registry.get_active()
        active_name = self._safe_provider_name(active)
        candidates.append((active_name, active))

        for name in self._fallback_chain:
            if not name or name == "dummy" or any(c[0] == name for c in candidates):
                continue
            provider = self._registry.get(name)
            if provider is not None:
                candidates.append((name, provider))

        if include_all:
            for name in self._registry.list():
                if name == "dummy" or any(c[0] == name for c in candidates):
                    continue
                provider = self._registry.get(name)
                if provider is not None:
                    candidates.append((name, provider))
        return candidates

    @staticmethod
    def _safe_provider_name(provider: BaseProvider) -> str:
        """Return a provider's name as a real string (empty when undetermined)."""
        name = getattr(provider, "name", "")
        if isinstance(name, str):
            return name
        try:
            pn = provider.provider_name()
            if isinstance(pn, str):
                return pn
        except Exception:
            pass
        return ""

    @staticmethod
    def _provider_healthy(provider: BaseProvider) -> bool:
        try:
            return bool(provider.health().get("healthy", False))
        except Exception:
            return False

    def _skip_reason(self, name: str, provider: BaseProvider, needs_tools: bool) -> str:
        """Return a skip reason when a provider is not eligible, else ""."""
        if not self._health.is_available(name):
            return f"state={self._health.state(name)}"
        if not self._provider_healthy(provider):
            return "unhealthy"
        model = self._effective_model(provider)
        if model and self._model_unavailable(name, model):
            return f"model={model}:unavailable"
        if needs_tools and not self._supports_tools(provider):
            return "capability=no_tool_calls"
        return ""

    @staticmethod
    def _supports_tools(provider: BaseProvider) -> bool:
        """True when the provider declares native tool/function calling."""
        try:
            caps = provider.capabilities
            return bool(caps.supports_tools or caps.supports_function_call)
        except Exception:
            return False

    def _score(self, name: str, provider: BaseProvider) -> float:
        """Deterministic provider score: reliability × latency × streak.

        The formula is deliberately simple and explainable in logs:
          base 1.0
          × (0.5 + 0.5 × HTTP success rate)   when the provider has history
          × latency factor (≤1, lower latency scores higher)
          − 0.1 per consecutive failure (max 0.5)
          + 0.05 per consecutive success (max 0.2)
        """
        score = 1.0
        metrics = self._metrics.get(name)
        if metrics.requests:
            score *= 0.5 + 0.5 * metrics.success_rate
            avg = metrics.average_latency
            if avg > 0:
                score *= min(1.0, 2.0 / max(0.5, avg))
            if metrics.quality_requests:
                score *= 0.5 + 0.5 * metrics.quality_success_rate
        score -= min(0.5, 0.1 * self._health.consecutive_failures(name))
        score += min(0.2, 0.05 * self._health.consecutive_successes(name))
        return round(max(0.0, score), 4)

    def _effective_model(self, provider: BaseProvider) -> str:
        """Return the provider's resolved model name (deprecation-substituted)."""
        try:
            return resolve_model(self._safe_provider_name(provider), provider.config.default_model or "")
        except Exception:
            return ""

    def _model_unavailable(self, name: str, model: str) -> bool:
        """True when (name, model) was marked unavailable and the TTL is live."""
        until = self._unavailable_models.get((name, model))
        return until is not None and time.monotonic() < until

    def _mark_model_unavailable(self, name: str, model: str) -> None:
        """Mark a (provider, model) pair unavailable so it is not retried.

        A 404 model-not-found is a permanent configuration error for THAT
        model. It must never be hammered on every request; the TTL allows a
        re-check later (e.g. after the provider re-enables the model) without
        requiring a process restart.
        """
        self._unavailable_models[(name, model)] = time.monotonic() + _MODEL_UNAVAILABLE_TTL
        logger.warning("PROVIDER_MODEL_UNAVAILABLE provider=%s model=%s", name, model)

    async def _call_once(
        self,
        provider: BaseProvider,
        messages: list[dict[str, Any]],
        kwargs: dict[str, Any],
    ) -> ProviderResponse:
        """Call a provider exactly once (bounded). Never raises.

        Hard exceptions are converted into a structured ``success=False``
        response with a ``failure_type`` so the routing machine can classify
        and cooldown without ever leaking an exception upward.
        """
        name = self._safe_provider_name(provider)
        start = time.perf_counter()
        try:
            response = await guarded_await(
                provider.chat(messages, **kwargs),
                name=f"provider:chat:{name or 'unknown'}",
                timeout=_PROVIDER_RPC_TIMEOUT,
            )
            latency = time.perf_counter() - start
            self._metrics.record(name, latency=latency, error="" if response.success else (response.text or "")[:200])
            return response
        except asyncio.TimeoutError as exc:
            latency = time.perf_counter() - start
            self._metrics.record(name, latency=latency, error=f"timeout: {exc}")
            logger.warning("ProviderManager: '%s' timed out during chat", name)
            return ProviderResponse(
                text=f"Request timed out: {exc}",
                provider_name=name,
                success=False,
                metadata={"failure_type": "timeout", "error": str(exc)},
            )
        except Exception as exc:
            latency = time.perf_counter() - start
            self._metrics.record(name, latency=latency, error=f"{type(exc).__name__}: {exc}")
            logger.warning("ProviderManager: '%s' crashed during chat: %s", name, exc)
            return ProviderResponse(
                text=f"Request failed: {type(exc).__name__}: {exc}",
                provider_name=name,
                success=False,
                metadata={"failure_type": "network", "error": f"{type(exc).__name__}: {exc}"},
            )

    async def _attempt_with_retry(
        self,
        provider: BaseProvider,
        messages: list[dict[str, Any]],
        kwargs: dict[str, Any],
    ) -> ProviderResponse:
        """Attempt one provider with at most ONE immediate retry.

        Applies the per-provider concurrency semaphore for the whole
        attempt. On success the provider is marked healthy. On failure the
        provider's health state is updated (cooldown/disabled) so the caller
        advances to the next candidate.
        """
        name = self._safe_provider_name(provider)
        model = self._effective_model(provider) or "-"
        async with self._health.acquire(name):
            logger.info("AI_PROVIDER_ATTEMPT provider=%s model=%s attempt=1", name, model)
            response = await self._call_once(provider, messages, kwargs)
            if response.success:
                self._health.record_success(name)
                self._note_response_quality(name, response, kwargs)
                return response

            ftype = self._failure_type(response)
            logger.warning(
                "AI_PROVIDER_FAILURE provider=%s model=%s category=%s retryable=%s",
                name, model, ftype, ftype in RETRYABLE_FAILURES,
            )
            if ftype not in RETRYABLE_FAILURES:
                self._apply_failure(name, ftype, response)
                return response

            # ONE bounded retry for transient/network/server errors, after a
            # short backoff so a burst cannot hammer the upstream API.
            await asyncio.sleep(_RETRY_BACKOFF_SECONDS)
            logger.info("AI_PROVIDER_ATTEMPT provider=%s model=%s attempt=2", name, model)
            retry_response = await self._call_once(provider, messages, kwargs)
            if retry_response.success:
                self._health.record_success(name)
                self._note_response_quality(name, retry_response, kwargs)
                logger.info(
                    "AI_PROVIDER_FAILURE provider=%s model=%s category=TRANSIENT_RECOVERED",
                    name, model,
                )
                return self._with_retry_metadata(retry_response, 1)

            self._apply_failure(name, self._failure_type(retry_response), retry_response)
            return retry_response

    @staticmethod
    def _failure_type(response: ProviderResponse) -> str:
        meta = response.metadata or {}
        if meta.get("failure_type"):
            return str(meta["failure_type"])
        status = meta.get("http_status")
        if status == 429:
            return "rate_limited"
        if status in (401, 403):
            return "auth"
        if status == 404:
            return "model_not_found"
        if isinstance(status, int) and 500 <= status < 600:
            return "server"
        if isinstance(status, int) and 400 <= status < 500:
            return "request"
        return meta.get("error_type") or "unknown"

    @staticmethod
    def _has_no_usable_output(response: ProviderResponse) -> bool:
        """True when a "successful" response carries no usable execution output.

        Covers an empty response (no text, no tool call) and a response
        whose tool calls are ALL malformed (unparsable arguments). Both are
        structured-output failures — never a usable answer for the
        execution layer.
        """
        if response.text:
            return False
        calls = response.tool_calls or []
        if not calls:
            return True
        return all(bool(c.get("malformed_arguments")) for c in calls)

    def _apply_failure(self, name: str, ftype: str, response: ProviderResponse) -> None:
        meta = response.metadata or {}
        if ftype in AUTH_FAILURES:
            self._health.mark_disabled(name)
            return
        if ftype in ("request", "model_not_found"):
            # Permanent configuration/request errors (e.g. a model name the
            # API does not recognize). Never retried, never cooled down — the
            # caller advances to the next fallback candidate deterministically.
            if ftype == "model_not_found":
                provider = self._registry.get(name)
                model = (response.metadata or {}).get("model") or (
                    self._effective_model(provider) if provider is not None else ""
                )
                if model:
                    self._mark_model_unavailable(name, model)
            logger.warning(
                "provider-health: '%s' config/request error (%s) — not cooling down",
                name, ftype,
            )
            return
        retry_after: float | None = None
        if ftype == "rate_limited":
            try:
                retry_after = float(meta.get("retry_after", DEFAULT_COOLDOWN_SECONDS))
            except (TypeError, ValueError):
                retry_after = DEFAULT_COOLDOWN_SECONDS
        # Transient failures feed the circuit breaker: per-category cooldown
        # now, quarantine after a run of consecutive failures.
        self._health.record_failure(name, ftype, retry_after)

    def _note_response_quality(
        self,
        name: str,
        response: ProviderResponse,
        kwargs: dict[str, Any],
    ) -> None:
        """Record an HTTP-success-but-semantically-empty response.

        A provider that returns ``success=True`` with no text and no tool call
        while tools were requested is a semantic failure (the same signal that
        produces ``action=none`` downstream). It is logged and fed into the
        quality metrics (lowering the router score) but NOT cooled down — a
        single empty response is recovered by the dispatcher's own bounded
        retry.
        """
        if not kwargs.get("tools"):
            return
        ok = bool(response.text or any(
            not c.get("malformed_arguments") for c in (response.tool_calls or [])
        ))
        self._metrics.record_quality(name, ok)
        if not ok:
            logger.info(
                "PROVIDER_RESPONSE_INVALID provider=%s reason=empty_or_malformed_with_tools",
                name,
            )

    @staticmethod
    def _with_retry_metadata(response: ProviderResponse, retry_count: int) -> ProviderResponse:
        meta = dict(response.metadata or {})
        meta["ai_retry_count"] = retry_count
        return replace(response, metadata=meta)

    def health_snapshot(self) -> dict[str, Any]:
        """Return per-provider cooldown/disabled state for observability."""
        return self._health.snapshot()

    async def _fallback(
        self,
        messages: list[dict[str, Any]],
        failures: list[str] | None = None,
        matrix: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> ProviderResponse:
        """Emergency fallback — always returns success=False with diagnostics.

        Even though the dummy provider "succeeds" deterministically, a
        fallback after real provider failures must NEVER look like a
        successful AI answer. The original failure information (and the
        provider failure matrix) is preserved.
        """
        fallback = self._registry.get_fallback()
        errors = list(failures or [])
        detail = "; ".join(errors) if errors else "no additional provider error information"
        try:
            await guarded_await(
                fallback.chat(messages, **kwargs),
                name=f"provider:fallback:{fallback.name}",
                timeout=_PROVIDER_RPC_TIMEOUT,
            )
        except Exception as exc:
            logger.error("ProviderManager: FALLBACK CRASHED: %s", exc)
            errors.append(f"fallback: {type(exc).__name__}: {exc}")
        return ProviderResponse(
            text=f"All AI providers failed. Last error: {detail}",
            provider_name=fallback.name,
            success=False,
            metadata={
                "fallback": True,
                "emergency": True,
                "fallback_exhausted": True,
                "errors": errors,
                "provider_matrix": list(matrix or []),
                "fallback_chain_tried": list(self._fallback_chain),
            },
        )

    def _fallback_vision(self, messages: list[dict[str, Any]], images: list[bytes], **kwargs: Any) -> ProviderResponse:
        fallback = self._registry.get_fallback()
        try:
            return fallback.vision(messages, images, **kwargs)
        except Exception as exc:
            logger.error("ProviderManager: FALLBACK VISION CRASHED: %s", exc)
            return ProviderResponse(
                text=f"All AI providers failed. Last error: {exc}",
                provider_name=fallback.name,
                success=False,
                metadata={"fallback": True, "emergency": True, "fallback_exhausted": True},
            )

    def _fallback_stream(self, messages: list[dict[str, Any]], **kwargs: Any) -> Iterator[ProviderResponse]:
        fallback = self._registry.get_fallback()
        try:
            yield from fallback.stream(messages, **kwargs)
        except Exception as exc:
            logger.error("ProviderManager: FALLBACK STREAM CRASHED: %s", exc)
            yield ProviderResponse(
                text=f"All AI providers failed. Last error: {exc}",
                provider_name=fallback.name,
                success=False,
                metadata={"fallback": True, "emergency": True, "fallback_exhausted": True},
            )

    def _ensure_dummy_fallback(self) -> None:
        from backend.ai.providers.dummy.provider import DummyProvider
        if not self._registry.has("dummy"):
            dummy = DummyProvider()
            self._registry.register(dummy)
        self._registry.set_fallback("dummy")

    def _load_env_fallback_chain(self) -> None:
        import os
        chain_str = os.getenv("AI_PROVIDER_FALLBACK", "")
        if chain_str:
            self._fallback_chain = [p.strip() for p in chain_str.split(",") if p.strip()]
            logger.info("ProviderManager: fallback chain = %s", self._fallback_chain)


