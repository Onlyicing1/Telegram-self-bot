"""
AI Model Availability Tester — lightweight diagnostic testing system.

Tests configured AI providers and models to determine real-time usability
without polluting conversation history or database state.

Flow:
  1. Discover which providers have API keys (ENV scan).
  2. For each configured provider, discover live models via the provider
     API (falling back to the centralized catalog) and select every
     chat-capable candidate — bounded only by the explicit global
     diagnostic budget (``MODEL_TEST_GLOBAL_TEST_BUDGET``), never by a
     hidden per-provider cap.
  3. Test candidates concurrently (bounded semaphore) with per-model
     timeouts; a slow/failed provider never blocks the others.
  4. Classify every result deterministically and return a structured
     payload with a rich summary.
  5. Publish the AVAILABLE results as the runtime model-level fallback
     pool (free-first, configured model excluded). Discovery alone never
     qualifies a model, and neither the response/display cap nor the
     diagnostic budget bounds the pool.

Classification statuses:
  AVAILABLE, NOT_CONFIGURED, AUTH_ERROR, RATE_LIMITED,
  INSUFFICIENT_CREDITS, TIMEOUT, INVALID_MODEL, BLOCKED,
  PROVIDER_ERROR, UNKNOWN_ERROR
"""
from __future__ import annotations
from backend.helper import symbols as sym

import asyncio
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable

from backend.ai.config_store import get_config
from backend.ai.discovery import discover_providers, _get_env, get_provider_info, _PROVIDERS
from backend.ai.model_discovery import (
    fallback_models,
    fetch_models,
    get_last_fetch_source,
    is_chat_capable,
    order_models_for_selector,
    reset_fetch_source,
)
from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.factory import ProviderFactory
from backend.ai.redaction import redact_secrets

logger = logging.getLogger(__name__)

# Bounded diagnostic text: All Results shows ONE short message per failed
# model, so a provider body is never carried through unbounded.
_MAX_ERROR_CHARS = 300

# Explicit GLOBAL diagnostic budget for one complete Test Modules run —
# deliberately NOT a per-provider cap. Discovery may surface hundreds of
# chat-capable models; a complete run must not silently drop eligible
# candidates, but it also must not spend unbounded API calls. The whole
# flat target list is truncated once at this budget (configured/default
# model first, then discovery order). ``MODEL_TEST_GLOBAL_TEST_BUDGET=0``
# removes the cap entirely — a resource tradeoff chosen explicitly.
_GLOBAL_TEST_BUDGET = int(os.getenv("MODEL_TEST_GLOBAL_TEST_BUDGET", "60"))
# Bounded concurrency: never spawn an unbounded task explosion.
_TEST_CONCURRENCY = int(os.getenv("MODEL_TEST_CONCURRENCY", "4"))
# Cap on discovered models included in the RESPONSE PAYLOAD (per provider).
# Display/diagnostic metadata ONLY — it must NEVER bound the production
# candidate feed, which uses the complete discovery result separately.
_MODELS_IN_RESPONSE = 30


def sanitize_error_message(msg: str) -> str:
    """Sanitize error messages to ensure no API keys or credentials leak to UI."""
    return redact_secrets(msg)


def _bounded_error_message(msg: str, limit: int = _MAX_ERROR_CHARS) -> str:
    """One-line, sanitized, bounded provider message for diagnostics."""
    clean = " ".join(sanitize_error_message(msg).split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rstrip() + "…"


def _model_lookup_evidence(provider_type: str, err_text: str) -> bool:
    """Whether a 404's own details actually name a missing MODEL.

    A bare HTTP 404 proves only that the route did not resolve. Treating it
    as an invalid model turns a wrong endpoint, a base-URL typo or a gateway
    route into a false model verdict, so the body must name a model
    (``models/x is not found``, ``model_not_found``) before that conclusion
    is drawn.
    """
    text = (err_text or "").lower()
    mentions_model = "model" in text or "models/" in text
    missing = (
        "not found" in text
        or "does not exist" in text
        or "unknown model" in text
        or "unsupported model" in text
        or provider_type in ("not_found", "model_not_found")
    )
    return mentions_model and missing


_ORIGIN_BY_STATUS = {
    "INVALID_MODEL": "model_lookup",
    "AUTH_ERROR": "authentication",
    "RATE_LIMITED": "rate_limit",
    "INSUFFICIENT_CREDITS": "quota",
    "TIMEOUT": "timeout",
    "BLOCKED": "content_policy",
    "PROVIDER_ERROR": "provider",
    "UNKNOWN_ERROR": "unknown",
}


def _failure_fields(
    metadata: dict[str, Any],
    status: str,
    response_text: str,
    requested_model: str,
) -> dict[str, Any]:
    """Provider-attributed diagnostics kept beside the classification.

    The status says WHAT the tester concluded; these fields say WHY — the id
    actually requested, the operation/endpoint, the provider's own code and
    status, and one bounded sanitized message. ``error_type`` is never reused
    for the provider's status: a local transport class and a provider verdict
    are different facts.
    """
    provider_message = metadata.get("provider_error_message") or response_text or ""
    return {
        "requested_model": requested_model,
        "provider_error_type": metadata.get("provider_error_type"),
        "provider_error_message": _bounded_error_message(provider_message) or None,
        "provider_error_reasons": [
            _bounded_error_message(reason, 80)
            for reason in (metadata.get("provider_error_reasons") or [])[:5]
            if reason
        ],
        "failure_origin": metadata.get("failure_origin") or _ORIGIN_BY_STATUS.get(status, "unknown"),
        "operation": metadata.get("operation"),
        "api_version": metadata.get("api_version"),
        "endpoint": metadata.get("endpoint"),
    }


def _classify_failure(metadata: dict[str, Any], err_text: str) -> tuple[str, str]:
    """Classify a failed provider response into a deterministic status.

    Returns ``(status, sanitized_error)``. Never exposes API keys.
    """
    http_status = metadata.get("http_status")
    err_type = (metadata.get("error_type", "") or "").lower()
    provider_type = (metadata.get("provider_error_type", "") or "").lower()
    provider_code = str(metadata.get("provider_error_code", "") or "")
    err_lower = (err_text or "").lower()

    if err_type == "timeout" or "timeout" in err_lower or "timed out" in err_lower:
        return "TIMEOUT", "Request timed out"

    if "content_filter" in err_lower or "safety" in err_lower or "recitation" in err_lower or "blocked" in err_lower:
        return "BLOCKED", sanitize_error_message(err_text)

    if http_status in (401, 403):
        return "AUTH_ERROR", sanitize_error_message(err_text)

    if http_status == 429:
        retry_after = metadata.get("retry_after")
        suffix = f" (retry-after: {retry_after}s)" if retry_after else ""
        return "RATE_LIMITED", f"Rate limited by provider{suffix}"

    # 402 or credit/quota/billing signals → INSUFFICIENT_CREDITS
    if (
        http_status == 402
        or provider_code == "402"
        or "insufficient_credits" in provider_type
        or "insufficient" in err_lower
        or "quota" in err_lower
        or "out of credits" in err_lower
        or "billing" in err_lower
        or "not enough credits" in err_lower
    ):
        retry_after = metadata.get("retry_after")
        suffix = f" (retry-after: {retry_after}s)" if retry_after else ""
        return "INSUFFICIENT_CREDITS", f"Insufficient credits/quota{suffix}"

    if http_status == 404:
        if _model_lookup_evidence(provider_type, err_text):
            return "INVALID_MODEL", _bounded_error_message(err_text)
        return "PROVIDER_ERROR", _bounded_error_message(
            f"HTTP 404 from the provider (no model-not-found evidence): {err_text}"
        )

    if provider_type in ("not_found", "model_not_found") and "model" in err_lower:
        return "INVALID_MODEL", _bounded_error_message(err_text)

    if http_status and http_status >= 500:
        return "PROVIDER_ERROR", sanitize_error_message(err_text)

    if provider_type or provider_code or http_status:
        return "PROVIDER_ERROR", sanitize_error_message(err_text)

    return "UNKNOWN_ERROR", sanitize_error_message(err_text or "Request failed without details")


def _not_configured_result(
    provider_name: str, display_name: str, icon: str, model_id: str
) -> dict[str, Any]:
    return {
        "provider": provider_name,
        "display_name": display_name,
        "icon": icon,
        "model": model_id,
        "status": "NOT_CONFIGURED",
        "error": "API key not configured in environment",
        "latency_s": None,
        "http_status": None,
        "retry_after": None,
        "error_type": None,
        "provider_code": None,
        "finish_reason": None,
        "capabilities": [],
    }


async def test_single_model(
    provider_name: str,
    display_name: str,
    icon: str,
    model_id: str,
    timeout: float = 8.0,
) -> dict[str, Any]:
    """Test a single model for a provider using isolated test request."""
    # Find provider info in _PROVIDERS
    p_info = next((p for p in _PROVIDERS if p["name"] == provider_name), None)
    if not p_info:
        return {
            "provider": provider_name,
            "display_name": display_name,
            "icon": icon,
            "model": model_id,
            "status": "NOT_CONFIGURED",
            "error": "Provider not recognized in registry",
            "latency_s": None,
            "http_status": None,
            "retry_after": None,
            "error_type": None,
            "provider_code": None,
            "finish_reason": None,
            "capabilities": [],
        }

    api_key = _get_env(p_info["env_vars"])
    if not api_key:
        # Never waste a network request on a provider without a key.
        return _not_configured_result(provider_name, display_name, icon, model_id)

    # The id tested is the id the caller selected, sent verbatim — the same id
    # the runtime would request. Nothing is substituted here, so a retired or
    # unknown id reports as a real failure of THAT model instead of borrowing
    # the result of another one.
    requested_model = model_id
    model_id = (model_id or "").strip()

    base_url = (
        _get_env(p_info["base_url_env"]) if p_info.get("base_url_env") else ""
    ) or p_info.get("default_base_url", "")

    config = ProviderConfig(
        provider_name=provider_name,
        base_url=base_url,
        api_key=api_key,
        default_model=model_id,
        temperature=0.0,
        max_tokens=5,
        timeout=int(timeout),
        retry_count=0,  # Fast fail for diagnostic test
        enabled=True,
    )

    provider_inst = None
    t0 = time.perf_counter()
    try:
        provider_inst = ProviderFactory.create_provider(provider_name, config)
        response = await asyncio.wait_for(
            provider_inst.chat(
                [{"role": "user", "content": "ping"}], model=model_id
            ),
            timeout=timeout,
        )
        latency_s = round(time.perf_counter() - t0, 2)

        metadata = response.metadata or {}
        finish_reason = metadata.get("finish_reason")

        if response.success:
            return {
                "provider": provider_name,
                "display_name": display_name,
                "icon": icon,
                "model": model_id,
                "requested_model": requested_model,
                "status": "AVAILABLE",
                "error": None,
                "latency_s": latency_s,
                "http_status": 200,
                "retry_after": None,
                "error_type": None,
                "provider_code": None,
                "finish_reason": finish_reason,
                "capabilities": [],
            }

        # Failure handling
        http_status = metadata.get("http_status")
        err_text = response.text or "Request failed"
        status, clean_err = _classify_failure(metadata, err_text)

        return {
            "provider": provider_name,
            "display_name": display_name,
            "icon": icon,
            "model": model_id,
            **_failure_fields(metadata, status, err_text, requested_model),
            "status": status,
            "error": clean_err,
            "latency_s": latency_s,
            "http_status": http_status,
            "retry_after": metadata.get("retry_after"),
            "error_type": metadata.get("error_type") or metadata.get("failure_type"),
            "provider_code": metadata.get("provider_error_code"),
            "finish_reason": finish_reason,
            "capabilities": [],
        }

    except asyncio.TimeoutError:
        latency_s = round(time.perf_counter() - t0, 2)
        return {
            "provider": provider_name,
            "display_name": display_name,
            "icon": icon,
            "model": model_id,
            "requested_model": requested_model,
            "provider_error_type": None,
            "provider_error_message": None,
            "failure_origin": "timeout",
            "operation": None,
            "api_version": None,
            "endpoint": None,
            "status": "TIMEOUT",
            "error": f"Request timed out after {timeout}s",
            "latency_s": latency_s,
            "http_status": None,
            "retry_after": None,
            "error_type": "timeout",
            "provider_code": None,
            "finish_reason": None,
            "capabilities": [],
        }
    except Exception as exc:
        latency_s = round(time.perf_counter() - t0, 2)
        clean_err = sanitize_error_message(str(exc))
        return {
            "provider": provider_name,
            "display_name": display_name,
            "icon": icon,
            "model": model_id,
            "requested_model": requested_model,
            "provider_error_type": None,
            "provider_error_message": None,
            "failure_origin": "unknown",
            "operation": None,
            "api_version": None,
            "endpoint": None,
            "status": "UNKNOWN_ERROR",
            "error": clean_err,
            "latency_s": latency_s,
            "http_status": None,
            "retry_after": None,
            "error_type": type(exc).__name__,
            "provider_code": None,
            "finish_reason": None,
            "capabilities": [],
        }
    finally:
        if provider_inst:
            try:
                provider_inst.shutdown()
            except Exception:
                pass


test_single_model.__test__ = False  # Tell pytest not to collect this as a test function


async def _build_targets(
    providers_status: list[Any],
    active_config: dict[str, Any],
    max_per_provider: int | None = None,
    global_budget: int | None = None,
) -> tuple[list[dict[str, str]], list[dict[str, Any]], dict[str, list[Any]]]:
    """Select models to test per provider (discovery-driven).

    Returns ``(targets, discovered_models, complete_models)`` where:
      - ``targets`` is the flat list of (provider, display_name, icon,
        model) to test (bounded by the explicit global diagnostic budget)
      - ``discovered_models`` is the CAPPED model list for the response
        payload (``_MODELS_IN_RESPONSE`` per provider — display only)
      - ``complete_models`` is the COMPLETE chat-capable ModelInfo list per
        provider — the production candidate feed. A display cap must never
        determine which models production fallback is allowed to see.

    There is deliberately NO per-provider test cap: every chat-capable
    candidate is eligible. The ONLY truncation of the TEST target list is
    the explicit global diagnostic budget (``global_budget``), applied
    ONCE to the flat list — fairly: every provider's configured/default
    model is kept first, the remainder is distributed round-robin across
    providers in discovery order. Priority per provider:
      1. the user's currently selected model (when this provider is active)
      2. the provider's default model — only when the LIVE catalog contains
         it (a default the provider no longer serves is a phantom target)
      3. discovered chat-capable models, FREE-FIRST ordered (deduped)

    Every id is used verbatim — a target is always the model that would
    actually be requested, and a newly discovered id needs no code change.
    ``max_per_provider`` is accepted and ignored for backward compatibility
    with the existing batch runner signature.
    """
    targets: list[dict[str, str]] = []
    discovered_models: list[dict[str, Any]] = []
    complete_models: dict[str, list[Any]] = {}

    for p in providers_status:
        if p.capability_kind != "chat":
            continue
        base = {"provider": p.name, "display_name": p.display_name, "icon": p.icon}

        if not p.has_key:
            targets.append({**base, "model": p.default_model or ""})
            continue

        info = get_provider_info(p.name) or {}
        api_key = _get_env(info.get("env_vars", []))
        base_url = p.base_url or info.get("default_base_url", "")

        models = []
        discovery_source = ""
        if api_key:
            try:
                # Claim THIS fetch's provenance only: an earlier provider's
                # source (or an injected fetch that records nothing) must not
                # decide whether the default model is a real catalog member.
                reset_fetch_source(p.name)
                models = await fetch_models(p.name, api_key, base_url, force_refresh=True)
                discovery_source = get_last_fetch_source(p.name)
            except Exception as exc:
                # Discovery failing here is still discovery failing: use the
                # SAME curated catalog the fetch itself falls back to, never an
                # empty list that would leave only the provider default.
                logger.warning("Model discovery failed for %s: %s", p.name, exc)
                models = fallback_models(p.name)
                discovery_source = "fallback" if models else ""

        # COMPLETE production feed: every chat-capable model from this
        # discovery result, uncapped and deduped — never sliced by the
        # display limit below. Ids are used exactly as discovered, so a newly
        # released model is a target with no code change and no substitution.
        full: list[Any] = []
        seen_full: set[str] = set()
        for m in models:
            model_id = (m.id or "").strip()
            if not model_id or model_id in seen_full or not is_chat_capable(model_id):
                continue
            seen_full.add(model_id)
            full.append(m)
        complete_models[p.name] = full

        # FREE-FIRST ordering, using the SAME deterministic selector order
        # the model picker already applies. A provider's raw ``/models``
        # order is arbitrary with respect to cost, and the diagnostic budget
        # truncates the flat target list — so without this the budget could
        # be spent on paid models before a single genuinely-free candidate
        # was ever reached (e.g. OpenRouter's ``:free`` catalog, which sorts
        # late in a raw alphabetical list). Ordering here makes free models
        # testable AND visible within the same explicit budget.
        ordered = order_models_for_selector(full)

        for m in ordered[: _MODELS_IN_RESPONSE]:
            # Response payload list must contain only chat-capable models.
            discovered_models.append(m.__dict__)

        candidates: list[str] = []
        seen: set[str] = set()

        # 1. the model this provider is actually configured to use (the
        #    owner's selection): always tested, so a stale selection surfaces
        #    as a real failure instead of being hidden.
        if active_config.get("provider") == p.name:
            selected = (active_config.get("model") or "").strip()
            if selected and selected not in seen:
                seen.add(selected)
                candidates.append(selected)
        # 2. the provider default, but ONLY when it is part of the catalog
        #    that was just fetched. A default the provider does not serve (a
        #    retired id, or a stale env value) is a phantom target: testing it
        #    reports a model that can be neither selected nor used. Curated
        #    fallback catalogs ARE the list, so their default is kept.
        default_model = (p.default_model or "").strip()
        if default_model and (discovery_source != "api" or default_model in seen_full):
            if default_model not in seen:
                seen.add(default_model)
                candidates.append(default_model)
        # 3. discovered chat-capable models (free-first, deduped)
        for m in ordered:
            if m.id in seen:
                continue
            seen.add(m.id)
            candidates.append(m.id)

        if not candidates and default_model:
            candidates = [default_model]

        for mid in candidates:
            targets.append({**base, "model": mid})

    budget = _GLOBAL_TEST_BUDGET if global_budget is None else max(0, global_budget)
    if budget and len(targets) > budget:
        # Fair distribution under the explicit budget: configured/default
        # model of EVERY provider first, then round-robin the rest across
        # providers in discovery order. Deterministic; no provider is
        # silently dropped just because an earlier catalog is larger.
        by_provider: dict[str, list[dict[str, str]]] = {}
        for t in targets:
            by_provider.setdefault(t["provider"], []).append(t)
        fair: list[dict[str, str]] = [rows[0] for rows in by_provider.values()]
        idx = 1
        while len(fair) < budget:
            progressed = False
            for rows in by_provider.values():
                if idx < len(rows):
                    fair.append(rows[idx])
                    progressed = True
                    if len(fair) >= budget:
                        break
            if not progressed:
                break
            idx += 1
        targets = fair[:budget]
    return targets, discovered_models, complete_models


async def _run_test_with_semaphore(
    sem: asyncio.Semaphore,
    target: dict[str, str],
    per_model_timeout: float,
):
    async with sem:
        return await test_single_model(
            target["provider"],
            target["display_name"],
            target["icon"],
            target["model"],
            timeout=per_model_timeout,
        )


def annotate_is_free(
    results: list[dict[str, Any]],
    complete_models: dict[str, list[Any]],
    providers_status: list[Any],
) -> None:
    """Tag every result with authoritative free/paid metadata (in place).

    ``is_free`` comes ONLY from discovery pricing metadata (the provider
    reports $0 prompt AND $0 completion) — a model name containing "free"
    never sets it. A model with no metadata is reported as not-free rather
    than guessed.
    """
    free_pairs = {
        (p.name, m.id)
        for p in providers_status
        for m in complete_models.get(p.name, [])
        if getattr(m, "is_free", False)
    }
    for item in results:
        item["is_free"] = (item.get("provider"), item.get("model")) in free_pairs


def proven_usable_candidates(
    results: list[dict[str, Any]],
    complete_models: dict[str, list[Any]],
    providers_status: list[Any],
) -> dict[str, list[str]]:
    """Map provider -> model ids PROVEN AVAILABLE by this diagnostic run.

    Production eligibility is a Test Modules RESULT, never a discovery
    side effect: only ``status == "AVAILABLE"`` qualifies. An untested
    model (e.g. cut by the diagnostic budget) or a failed one is never
    silently equivalent to a usable model. Ordering reuses the same
    deterministic free-first selector order; ids without discovery
    metadata keep a stable trailing alphabetical order. Each provider's
    configured/default model is dropped because the router already tries
    it first. Dummy is never a candidate.
    """
    usable: dict[str, list[str]] = {}
    for item in results:
        if item.get("status") != "AVAILABLE":
            continue
        provider = str(item.get("provider") or "")
        model = str(item.get("model") or "")
        if not provider or not model or provider == "dummy":
            continue
        bucket = usable.setdefault(provider, [])
        if model not in bucket:
            bucket.append(model)

    configured = {
        p.name: (p.default_model or "").strip() for p in providers_status
    }
    feed: dict[str, list[str]] = {}
    for provider, ids in usable.items():
        infos = complete_models.get(provider, [])
        known = {m.id for m in infos}
        ordered = [
            m.id for m in order_models_for_selector(infos) if m.id in ids
        ]
        leftover = sorted(i for i in ids if i not in known)
        default_model = configured.get(provider, "")
        feed[provider] = [
            mid for mid in (ordered + leftover) if mid and mid != default_model
        ]
    return feed


def _feed_production_candidates(
    results: list[dict[str, Any]],
    complete_models: dict[str, list[Any]],
    providers_status: list[Any],
) -> None:
    """Publish the AVAILABLE test results to the runtime model-level pool.

    The pool is the EXISTING ProviderManager contract
    (``set_model_candidates``) — runtime-owned, never scraped from rendered
    text — so a proven model is eligible whether or not the Test Modules
    panel is open, and the manager's own health/cooldown/quarantine rules
    still decide runtime use. A provider with no AVAILABLE model gets an
    empty candidate list (never the raw discovery set). Dummy is skipped.
    """
    try:
        from backend.ai.engine.engine import get_engine

        feed = proven_usable_candidates(results, complete_models, providers_status)
        pm = get_engine().provider_manager
        for p in providers_status:
            if p.capability_kind != "chat" or not p.has_key or p.name == "dummy":
                continue
            pm.set_model_candidates(p.name, feed.get(p.name, []))
    except Exception as exc:
        logger.warning("Candidate-pool feed skipped: %s", exc)


async def test_all_models(
    owner_id: int = 0,
    per_model_timeout: float = 8.0,
    overall_timeout: float = 60.0,
    global_budget: int | None = None,
) -> dict[str, Any]:
    """Discover configured providers/models and run availability tests.

    - Concurrent, bounded (semaphore) execution with per-model timeouts.
    - One provider failure never aborts testing of other providers.
    - On overall timeout, completed results are kept and remaining
      targets are reported as TIMEOUT (``partial=True``).
    """
    providers_status = await discover_providers(force_refresh=True)
    active_config = await get_config(owner_id)

    targets, discovered_models, complete_models = await _build_targets(
        providers_status, active_config, global_budget=global_budget,
    )

    sem = asyncio.Semaphore(_TEST_CONCURRENCY)
    tasks = [
        asyncio.ensure_future(_run_test_with_semaphore(sem, t, per_model_timeout))
        for t in targets
    ]

    done, pending = await asyncio.wait(tasks, timeout=overall_timeout)
    for task in pending:
        task.cancel()

    results: list[dict[str, Any]] = []
    tested_at = datetime.now(timezone.utc).isoformat()

    for idx, task in enumerate(tasks):
        target_info = targets[idx] if idx < len(targets) else {"provider": "unknown", "display_name": "Unknown", "icon": sym.MEDIA_STICKER, "model": "unknown"}
        if task in pending:
            results.append({
                "provider": target_info["provider"],
                "display_name": target_info["display_name"],
                "icon": target_info["icon"],
                "model": target_info["model"],
                "status": "TIMEOUT",
                "error": "Overall diagnostic timeout reached",
                "latency_s": None,
                "http_status": None,
                "retry_after": None,
                "error_type": "timeout",
                "provider_code": None,
                "finish_reason": None,
                "capabilities": [],
            })
            continue
        try:
            item = task.result()
        except asyncio.CancelledError:
            continue
        except Exception as exc:
            item = {
                "provider": target_info["provider"],
                "display_name": target_info["display_name"],
                "icon": target_info["icon"],
                "model": target_info["model"],
                "status": "UNKNOWN_ERROR",
                "error": sanitize_error_message(str(exc)),
                "latency_s": None,
                "http_status": None,
                "retry_after": None,
                "error_type": type(exc).__name__,
                "provider_code": None,
                "finish_reason": None,
                "capabilities": [],
            }
        if isinstance(item, dict):
            item.setdefault("tested_at", tested_at)
            results.append(item)

    annotate_is_free(results, complete_models, providers_status)
    _feed_production_candidates(results, complete_models, providers_status)

    summary = _build_summary(results, len(discovered_models))

    return {
        "success": True,
        "tested_at": tested_at,
        "partial": bool(pending),
        "providers": [p.__dict__ for p in providers_status],
        "models": discovered_models,
        "results": results,
        "summary": summary,
    }


def _build_summary(results: list[dict[str, Any]], discovered_count: int) -> dict[str, int]:
    """Deterministic summary buckets (keeps legacy keys for compat)."""
    summary: dict[str, int] = {
        "total": len(results),
        "available": 0,
        "unavailable": 0,
        "error": 0,
        "timeout": 0,
        "not_configured": 0,
        "discovered": discovered_count,
        "tested": 0,
        "failed": 0,
        "rate_limited": 0,
        "invalid": 0,
        "insufficient_credits": 0,
        "blocked": 0,
        "auth_error": 0,
        "provider_error": 0,
        "unknown_error": 0,
    }

    for res in results:
        status = res.get("status", "UNKNOWN_ERROR")
        if status == "AVAILABLE":
            summary["available"] += 1
        elif status == "NOT_CONFIGURED":
            summary["not_configured"] += 1
        elif status == "TIMEOUT":
            summary["timeout"] += 1
            summary["error"] += 1
        elif status == "INVALID_MODEL":
            summary["unavailable"] += 1
            summary["invalid"] += 1
        elif status == "BLOCKED":
            summary["unavailable"] += 1
            summary["blocked"] += 1
        elif status == "AUTH_ERROR":
            summary["error"] += 1
            summary["auth_error"] += 1
        elif status == "RATE_LIMITED":
            summary["error"] += 1
            summary["rate_limited"] += 1
        elif status == "INSUFFICIENT_CREDITS":
            summary["error"] += 1
            summary["insufficient_credits"] += 1
        elif status == "PROVIDER_ERROR":
            summary["error"] += 1
            summary["provider_error"] += 1
        else:  # UNKNOWN_ERROR / ERROR / anything else
            summary["error"] += 1
            summary["unknown_error"] += 1

    summary["tested"] = summary["total"] - summary["not_configured"]
    summary["failed"] = summary["unavailable"] + summary["error"] + summary["timeout"]
    return summary


test_all_models.__test__ = False  # Tell pytest not to collect this as a test function


async def test_all_models_streaming(
    owner_id: int = 0,
    per_model_timeout: float = 8.0,
    overall_timeout: float = 60.0,
    global_budget: int | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    on_result: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Streaming twin of ``test_all_models`` (same targets, same results).

    Identical discovery, target selection, bounded concurrency, per-model
    timeout, overall timeout, and result shape as the batch runner — the
    only difference is that ``on_progress`` fires after every completion and
    ``on_result`` after every result. Used by the Telegram Test Modules UI
    to drive a throttled progress panel; the batch runner stays untouched
    for the web dashboard.
    """
    providers_status = await discover_providers(force_refresh=True)
    active_config = await get_config(owner_id)

    targets, discovered_models, complete_models = await _build_targets(
        providers_status, active_config, global_budget=global_budget,
    )

    total = len(targets)
    completed = 0

    async def _note(item: dict[str, Any]) -> dict[str, Any]:
        nonlocal completed
        completed += 1
        item = dict(item)
        item.setdefault("tested_at", datetime.now(timezone.utc).isoformat())
        if on_result is not None:
            try:
                on_result(item)
            except Exception:
                logger.exception("on_result callback failed")
        if on_progress is not None:
            try:
                on_progress(completed, total)
            except Exception:
                logger.exception("on_progress callback failed")
        return item

    async def _run_one(target: dict[str, str]) -> dict[str, Any]:
        try:
            item = await _run_test_with_semaphore(sem, target, per_model_timeout)
        except Exception as exc:
            item = {
                "provider": target["provider"],
                "display_name": target["display_name"],
                "icon": target["icon"],
                "model": target["model"],
                "status": "UNKNOWN_ERROR",
                "error": sanitize_error_message(str(exc)),
                "latency_s": None,
                "http_status": None,
                "retry_after": None,
                "error_type": type(exc).__name__,
                "provider_code": None,
                "finish_reason": None,
                "capabilities": [],
            }
        return await _note(item)

    sem = asyncio.Semaphore(_TEST_CONCURRENCY)
    if not targets:
        return {
            "success": True,
            "tested_at": datetime.now(timezone.utc).isoformat(),
            "partial": False,
            "providers": [p.__dict__ for p in providers_status],
            "models": discovered_models,
            "results": [],
            "summary": _build_summary([], len(discovered_models)),
        }

    tasks = [asyncio.ensure_future(_run_one(t)) for t in targets]
    done, pending = await asyncio.wait(tasks, timeout=overall_timeout)
    for task in pending:
        task.cancel()

    results: list[dict[str, Any]] = []
    for idx, task in enumerate(tasks):
        target_info = targets[idx] if idx < len(targets) else {
            "provider": "unknown", "display_name": "Unknown", "icon": sym.MEDIA_STICKER, "model": "unknown",
        }
        if task in pending:
            results.append(await _note({
                "provider": target_info["provider"],
                "display_name": target_info["display_name"],
                "icon": target_info["icon"],
                "model": target_info["model"],
                "status": "TIMEOUT",
                "error": "Overall diagnostic timeout reached",
                "latency_s": None,
                "http_status": None,
                "retry_after": None,
                "error_type": "timeout",
                "provider_code": None,
                "finish_reason": None,
                "capabilities": [],
            }))
            continue
        try:
            item = task.result()
        except asyncio.CancelledError:
            continue
        except Exception as exc:
            results.append(await _note({
                "provider": target_info["provider"],
                "display_name": target_info["display_name"],
                "icon": target_info["icon"],
                "model": target_info["model"],
                "status": "UNKNOWN_ERROR",
                "error": sanitize_error_message(str(exc)),
                "latency_s": None,
                "http_status": None,
                "retry_after": None,
                "error_type": type(exc).__name__,
                "provider_code": None,
                "finish_reason": None,
                "capabilities": [],
            }))
            continue
        results.append(item)

    # Only AFTER the run do we know which (provider, model) pairs are usable.
    # Discovery alone never arms the production pool: the candidate set is
    # the AVAILABLE test result (free-first, configured model excluded),
    # published through the existing ProviderManager contract. The response
    # payload cap and the diagnostic budget bound TEST EXECUTION and DISPLAY
    # only — never this feed.
    annotate_is_free(results, complete_models, providers_status)
    _feed_production_candidates(results, complete_models, providers_status)

    return {
        "success": True,
        "tested_at": datetime.now(timezone.utc).isoformat(),
        "partial": bool(pending),
        "providers": [p.__dict__ for p in providers_status],
        "models": discovered_models,
        "results": results,
        "summary": _build_summary(results, len(discovered_models)),
    }
