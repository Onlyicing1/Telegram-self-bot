"""
AI Model Availability Tester — lightweight diagnostic testing system.

Tests configured AI providers and models to determine real-time usability
without polluting conversation history or database state.

Flow:
  1. Enumerate EVERY chat provider in the registry — configured or not. The
     owner's selected provider/model is never consulted: this is an
     independent diagnostic, not a test of the current selection.
  2. For a provider with a key, discover its live model catalog (falling
     back to the centralized catalog when that is empty or unusable);
     without a key, read that same catalog and issue NO request. Every
     model that genuinely accepts the standard chat completion becomes a
     target — no budget, cap, slice or priority queue drops one.
  3. Test candidates concurrently (bounded semaphore) with per-model
     timeouts; a slow/failed provider never blocks the others.
  4. Classify every result deterministically and return a structured
     payload with a rich summary.
  5. Publish the AVAILABLE results as the runtime model-level fallback
     pool (free-first, configured model excluded). Discovery alone never
     qualifies a model, and neither the response/display cap nor an
     unattempted target can enter the pool.

Classification statuses:
  AVAILABLE, NOT_CONFIGURED, AUTH_ERROR, RATE_LIMITED,
  INSUFFICIENT_CREDITS, TIMEOUT, INVALID_MODEL, BLOCKED,
  PROVIDER_ERROR, UNKNOWN_ERROR, NOT_ATTEMPTED

``NOT_ATTEMPTED`` is never a provider response: it exists only for a
caller-imposed overall deadline that ended the run before a target could
be tried, so a partial run can never be reported as a complete one.
"""
from __future__ import annotations
from backend.helper import symbols as sym

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable

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

# Bounded concurrency: never spawn an unbounded task explosion. This is the
# ONLY bound on how many models a run tests — every eligible target is
# scheduled, so a catalog of 200 models produces 200 results.
_TEST_CONCURRENCY = int(os.getenv("MODEL_TEST_CONCURRENCY", "4"))
# Cap on discovered models included in the RESPONSE PAYLOAD (per provider).
# Display metadata ONLY — it never bounds which models are TESTED, and never
# bounds the production candidate feed.
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


def _not_attempted_result(target: dict[str, str]) -> dict[str, Any]:
    """A target the run never reached — explicitly NOT a provider response."""
    return {
        "provider": target.get("provider", "unknown"),
        "display_name": target.get("display_name", "Unknown"),
        "icon": target.get("icon", sym.MEDIA_STICKER),
        "model": target.get("model", "unknown"),
        "status": "NOT_ATTEMPTED",
        "error": "Not attempted — overall diagnostic deadline reached",
        "latency_s": None,
        "http_status": None,
        "retry_after": None,
        "error_type": "not_attempted",
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
    active_config: dict[str, Any] | None = None,
) -> tuple[list[dict[str, str]], list[dict[str, Any]], dict[str, list[Any]]]:
    """Select every chat-capable model of every chat provider.

    Returns ``(targets, discovered_models, complete_models)`` where:
      - ``targets`` is the flat list of (provider, display_name, icon,
        model) to test — the COMPLETE eligible set
      - ``discovered_models`` is the display-capped model list for the
        response payload (``_MODELS_IN_RESPONSE`` per provider)
      - ``complete_models`` is the complete chat-capable ModelInfo list per
        provider — the production candidate feed

    Selection depends on the catalogs ALONE: the owner's active
    provider/model is never read here, so the same catalogs always produce
    the same target set. Per provider:
      * API key present → the live discovery result, falling back to the
        centralized catalog when it is empty or has nothing usable;
      * no API key → that same centralized catalog, with NO request.

    A missing key therefore removes neither the provider nor its models from
    the diagnosis: they are scheduled and answer NOT_CONFIGURED without any
    client construction or network call. Provider defaults that are not
    members of the applicable catalog are never invented as targets, and a
    (provider, model) pair is scheduled at most once.

    Nothing truncates the target list — no global budget, no per-provider
    cap, no slice, no priority queue. Only ``_TEST_CONCURRENCY`` bounds how
    many run at once and the per-model timeout bounds each one.

    ``active_config`` is accepted for call-site compatibility and IGNORED: it
    is never read, so the selection cannot add, remove or reorder a target.
    """
    _ = active_config
    targets: list[dict[str, str]] = []
    discovered_models: list[dict[str, Any]] = []
    complete_models: dict[str, list[Any]] = {}
    seen_pairs: set[tuple[str, str]] = set()

    for p in providers_status:
        if p.capability_kind != "chat":
            continue
        base = {"provider": p.name, "display_name": p.display_name, "icon": p.icon}

        models: list[Any] = []
        discovery_source = ""
        if p.has_key:
            info = get_provider_info(p.name) or {}
            api_key = _get_env(info.get("env_vars", []))
            base_url = p.base_url or info.get("default_base_url", "")
            if api_key:
                try:
                    # Provenance of THIS fetch only: an earlier provider's
                    # source must never be inherited.
                    reset_fetch_source(p.name)
                    models = await fetch_models(
                        p.name, api_key, base_url, force_refresh=True
                    )
                    discovery_source = get_last_fetch_source(p.name)
                except Exception as exc:
                    logger.warning("Model discovery failed for %s: %s", p.name, exc)
                    models = []
                    discovery_source = "fallback"

        if not models:
            # No key, or a discovery result with nothing usable: the ONE
            # centralized catalog. A provider default that is not a catalog
            # member is never added as a phantom model.
            models = fallback_models(p.name)
            discovery_source = discovery_source or "fallback"

        logger.debug(
            "Test Modules catalog for %s: %s (%d models)",
            p.name, discovery_source or "unknown", len(models),
        )

        # COMPLETE feed: every chat-capable model of this catalog, uncapped
        # and deduped — never sliced by the display limit below.
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
        # the model picker already applies, so a genuinely-free catalog is
        # both visible and tested first as a matter of ordering only — never
        # of truncation. The provider's default is then moved to the front IF
        # it is a real member of this catalog (order only, never an addition).
        ordered = order_models_for_selector(full)
        default_model = (p.default_model or "").strip()
        if default_model and any((m.id or "").strip() == default_model for m in ordered):
            ordered = sorted(
                ordered, key=lambda m: 0 if (m.id or "").strip() == default_model else 1
            )

        for m in ordered[: _MODELS_IN_RESPONSE]:
            # Response payload list must contain only chat-capable models.
            discovered_models.append(m.__dict__)

        for m in ordered:
            model_id = (m.id or "").strip()
            if not model_id:
                continue
            pair = (p.name, model_id)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            targets.append({**base, "model": model_id})

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
    overall_timeout: float | None = None,
    global_budget: int | None = None,
) -> dict[str, Any]:
    """Test every chat-capable model of every chat provider.

    - Every eligible target is scheduled (no budget/cap/slice).
    - Concurrent, bounded (semaphore) execution with per-model timeouts.
    - One provider failure never aborts testing of other providers.
    - ``overall_timeout`` defaults to None (run to completion). A caller that
      imposes one gets every unreached target as ``NOT_ATTEMPTED`` — never as
      a provider response — and ``partial=True``.
    - ``global_budget`` is accepted for call-site compatibility and IGNORED:
      it no longer truncates the target list.
    """
    _ = global_budget  # deliberately unused: complete coverage
    providers_status = await discover_providers(force_refresh=True)

    targets, discovered_models, complete_models = await _build_targets(
        providers_status,
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
            # Cancelled before it ran: report it as unattempted, never as a
            # provider TIMEOUT (the provider was never asked).
            results.append(_not_attempted_result(target_info))
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
        "not_attempted": 0,
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
        elif status == "NOT_ATTEMPTED":
            summary["not_attempted"] += 1
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

    summary["tested"] = (
        summary["total"] - summary["not_configured"] - summary["not_attempted"]
    )
    summary["failed"] = summary["unavailable"] + summary["error"] + summary["timeout"]
    return summary


test_all_models.__test__ = False  # Tell pytest not to collect this as a test function


async def test_all_models_streaming(
    owner_id: int = 0,
    per_model_timeout: float = 8.0,
    overall_timeout: float | None = None,
    global_budget: int | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    on_result: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Streaming twin of ``test_all_models`` (same targets, same results).

    Identical discovery, target selection, bounded concurrency, per-model
    timeout, overall-timeout semantics and result shape as the batch runner —
    the only difference is that ``on_progress`` fires after every completion
    and ``on_result`` after every result. The two paths therefore always
    cover the same models; a run cannot be complete in one and truncated in
    the other. ``global_budget`` is accepted and IGNORED.
    """
    _ = global_budget  # deliberately unused: complete coverage
    providers_status = await discover_providers(force_refresh=True)

    targets, discovered_models, complete_models = await _build_targets(
        providers_status,
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
            results.append(await _note(_not_attempted_result(target_info)))
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
