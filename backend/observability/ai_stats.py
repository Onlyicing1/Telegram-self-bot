"""
AI statistics aggregator — aggregates AI execution metrics.

Reuses EngineMetrics.snapshot() and ProviderManager.metrics_snapshot().
No duplicated counters.
"""
from __future__ import annotations

from typing import Any


def ai_statistics() -> dict[str, Any]:
    """Return aggregated AI execution statistics.

    Reads from:
      - Engine.metrics_snapshot()  (execution counts, latency, tokens)
      - ProviderManager.metrics_snapshot()  (per-provider health)
      - ProviderManager.get_active_name()  (active provider/model)
    """
    from backend.ai.engine.engine import get_engine

    try:
        engine = get_engine()
    except Exception:
        return {"available": False, "error": "engine not initialized"}

    try:
        metrics = engine.metrics_snapshot()
        provider_mgr = engine.provider_manager
        provider_metrics = provider_mgr.metrics_snapshot()
        active_provider = provider_mgr.get_active_name()

        total = metrics.get("total_executions", 0)
        successful = metrics.get("successful_executions", 0)
        failed = metrics.get("failed_executions", 0)
        success_rate = round(successful / total, 4) if total > 0 else 0.0
        failure_rate = round(failed / total, 4) if total > 0 else 0.0

        total_tokens = metrics.get("total_prompt_tokens", 0) + metrics.get("total_completion_tokens", 0)
        model_usage = _compute_model_usage(provider_mgr, provider_metrics)
        cost_estimation = _estimate_cost(active_provider, metrics)

        return {
            "available": True,
            "total_requests": total,
            "successful_requests": successful,
            "failed_requests": failed,
            "success_rate": success_rate,
            "failure_rate": failure_rate,
            "average_latency_s": metrics.get("average_latency", 0.0),
            "min_latency_s": metrics.get("min_latency", 0.0),
            "max_latency_s": metrics.get("max_latency", 0.0),
            "total_prompt_tokens": metrics.get("total_prompt_tokens", 0),
            "total_completion_tokens": metrics.get("total_completion_tokens", 0),
            "total_tokens": total_tokens,
            "conversation_count": metrics.get("conversation_count", 0),
            "provider_usage": metrics.get("provider_usage", {}),
            "model_usage": model_usage,
            "failure_counts": metrics.get("failure_counts", {}),
            "active_provider": active_provider,
            "active_model": _get_active_model(provider_mgr),
            "provider_metrics": provider_metrics,
            "cost_estimation": cost_estimation,
            "tool_usage": tool_usage_frequency(),
        }
    except Exception as exc:
        return {"available": False, "error": str(exc)}


def _get_active_model(provider_mgr: Any) -> str:
    try:
        config = provider_mgr.get_provider_config()
        return config.model if config and config.model else ""
    except Exception:
        return ""


def tool_usage_frequency(owner_id: int = 0) -> dict[str, int]:
    """Return tool call frequency from the tool history repository."""
    from backend.ai.database.manager import get_repository_manager

    try:
        repo = get_repository_manager().tool_history
        if hasattr(repo, "recent"):
            records = repo.recent(owner_id, limit=500)
            freq: dict[str, int] = {}
            for r in records:
                name = r.tool_name if hasattr(r, "tool_name") else ""
                if name:
                    freq[name] = freq.get(name, 0) + 1
            return freq
    except Exception:
        pass
    return {}


_COST_PER_1K_TOKENS: dict[str, dict[str, float]] = {
    "openai": {"prompt": 0.005, "completion": 0.015},
    "gemini": {"prompt": 0.00025, "completion": 0.0005},
    "openrouter": {"prompt": 0.0005, "completion": 0.0015},
    "groq": {"prompt": 0.0001, "completion": 0.0001},
    "cerebras": {"prompt": 0.0001, "completion": 0.0001},
    "mistral": {"prompt": 0.0005, "completion": 0.0015},
    "dummy": {"prompt": 0.0, "completion": 0.0},
}


def _estimate_cost(provider: str, metrics: dict[str, Any]) -> dict[str, Any]:
    prompt_tokens = metrics.get("total_prompt_tokens", 0)
    completion_tokens = metrics.get("total_completion_tokens", 0)
    rates = _COST_PER_1K_TOKENS.get(provider, {"prompt": 0.0, "completion": 0.0})
    prompt_cost = (prompt_tokens / 1000) * rates["prompt"]
    completion_cost = (completion_tokens / 1000) * rates["completion"]
    return {
        "available": provider in _COST_PER_1K_TOKENS,
        "estimated_cost_usd": round(prompt_cost + completion_cost, 6),
        "prompt_cost_usd": round(prompt_cost, 6),
        "completion_cost_usd": round(completion_cost, 6),
        "provider": provider,
    }


def _compute_model_usage(provider_mgr: Any, provider_metrics: dict[str, dict[str, Any]]) -> dict[str, int]:
    model_usage: dict[str, int] = {}
    try:
        for name in provider_mgr.list_providers():
            try:
                config = provider_mgr.get_provider_config(name)
                model = config.model if config and config.model else ""
                if model:
                    model_usage[model] = provider_metrics.get(name, {}).get("requests", 0)
            except Exception:
                pass
    except Exception:
        pass
    return model_usage
