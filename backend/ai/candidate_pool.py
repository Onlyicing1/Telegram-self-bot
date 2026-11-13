"""
Durable publication of Test Modules' proven model candidates.

``ProviderManager``'s candidate pool is deliberately in-memory: the Test Modules
run proves which ``(provider, model)`` pairs answer, and the router then uses
that proof for the rest of the process. The gap this module closes is the
process boundary: every restart/redeploy starts with an EMPTY pool, so a
production request sees only each provider's configured default model (the
``models=1`` production log line) even though the last Test Modules run proved a
much larger set.

The pool is persisted as ONE bounded ``bot_settings`` JSON document — the same
generic document entry point the Test Modules snapshot already uses, so no
schema change and no new database object. It is NOT a live-availability claim:

* only a COMPLETE Test Modules run may overwrite the document; a partial run
  (overall timeout, cancelled, interrupted) publishes nothing, so it can never
  shrink a complete pool;
* a document older than :data:`MAX_AGE_S` is refused — stale results are never
  restored as current availability;
* every restored candidate is still re-proven by the router's own live attempt:
  a failing pair is cooled down or marked unavailable by the EXISTING
  ``ProviderManager`` health rules, and the router continues with the remaining
  candidates;
* only providers registered in THIS process are populated, so a removed or
  renamed provider leaves no stale candidate reference behind.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

#: Durable document key (``bot_settings``). Versioned like the Test Modules
#: snapshot so an older shape can never be read as this one.
POOL_KEY = "ai_model_candidates:v1"
SCHEMA_VERSION = 1

#: Freshness contract. A candidate set older than this is not restored at all;
#: within it, routing still revalidates each candidate with a real attempt.
MAX_AGE_S = 24 * 60 * 60.0

#: Bounds: a document is data from the store, never an unbounded provider list.
MAX_PROVIDERS = 32
MAX_MODELS_PER_PROVIDER = 256
MAX_MODEL_CHARS = 200

#: Clock skew tolerated on ``published_at`` (a future timestamp is not "fresh").
_FUTURE_SKEW_S = 300.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clean_model(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    model = " ".join(value.split())
    return model[:MAX_MODEL_CHARS]


def build_document(feed: dict[str, list[str]]) -> dict[str, Any]:
    """The bounded durable shape of one COMPLETE candidate feed.

    Providers with an empty list are kept on purpose: restoring the document
    clears those providers instead of leaving an older, longer list in memory.
    """
    providers: dict[str, list[str]] = {}
    for index, (name, models) in enumerate(feed.items()):
        if index >= MAX_PROVIDERS:
            break
        provider = str(name or "").strip()
        if not provider or provider == "dummy" or not isinstance(models, list):
            continue
        cleaned: list[str] = []
        for raw in models[:MAX_MODELS_PER_PROVIDER]:
            model = _clean_model(raw)
            if model and model not in cleaned:
                cleaned.append(model)
        providers[provider] = cleaned
    return {
        "schema_version": SCHEMA_VERSION,
        "published_at": _now().isoformat(),
        "providers": providers,
    }


def _parse_document(raw: str) -> tuple[dict[str, list[str]], str]:
    try:
        document = json.loads(raw)
    except (TypeError, ValueError):
        return {}, "malformed"
    if not isinstance(document, dict):
        return {}, "malformed"
    if document.get("schema_version") != SCHEMA_VERSION:
        return {}, "unsupported_version"
    published = document.get("published_at")
    if not isinstance(published, str) or not published.strip():
        return {}, "malformed"
    try:
        stamp = datetime.fromisoformat(published)
    except (TypeError, ValueError):
        return {}, "malformed"
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    age = (_now() - stamp).total_seconds()
    if age < -_FUTURE_SKEW_S:
        return {}, "malformed"
    if age > MAX_AGE_S:
        return {}, "stale"

    providers = document.get("providers")
    if not isinstance(providers, dict):
        return {}, "malformed"
    pool: dict[str, list[str]] = {}
    for index, (name, models) in enumerate(providers.items()):
        if index >= MAX_PROVIDERS:
            break
        provider = str(name or "").strip()
        if not provider or provider == "dummy" or not isinstance(models, list):
            continue
        cleaned: list[str] = []
        for raw_model in models[:MAX_MODELS_PER_PROVIDER]:
            model = _clean_model(raw_model)
            if model and model not in cleaned:
                cleaned.append(model)
        pool[provider] = cleaned
    if not pool:
        return {}, "empty"
    return pool, ""


async def publish(feed: dict[str, list[str]]) -> bool:
    """Persist one complete feed. True only when the durable write landed.

    A failure never changes in-memory routing (the caller already armed the
    live ``ProviderManager``); it only means the next restart falls back to the
    previous document.
    """
    try:
        from backend.db import client as db_client

        document = build_document(feed)
        payload = json.dumps(document, separators=(",", ":"), sort_keys=True)
        saved = await db_client.settings_document_write(POOL_KEY, payload)
    except Exception as exc:  # noqa: BLE001 — persistence never breaks a run
        logger.warning("CANDIDATE_POOL_PUBLISH_FAILED error=%s", type(exc).__name__)
        return False
    models = sum(len(models) for models in document["providers"].values())
    logger.info(
        "CANDIDATE_POOL_PUBLISHED providers=%d models=%d saved=%s",
        len(document["providers"]), models, bool(saved),
    )
    return bool(saved)


async def load() -> tuple[dict[str, list[str]], str]:
    """Read the durable pool: ``(pool, reason)`` with ``reason == ""`` on success."""
    try:
        from backend.db import client as db_client

        raw, readable = await db_client.settings_document_state(POOL_KEY)
    except Exception as exc:  # noqa: BLE001 — an unreadable store is not "empty"
        logger.warning("CANDIDATE_POOL_READ_FAILED error=%s", type(exc).__name__)
        return {}, "unavailable"
    if not readable:
        return {}, "unavailable"
    if not raw:
        return {}, "missing"
    return _parse_document(raw)


async def restore(manager: Any) -> dict[str, Any]:
    """Populate the live manager from the durable pool.

    Called ONCE by ``RuntimeSupervisor`` after the boot AI configuration, before
    the update run loop starts. Only providers this process actually registered
    are populated; every other entry is ignored rather than kept as a stale
    reference. The returned status is what the supervisor logs, so an absent,
    stale or unreadable document is explicit — never a silent empty pool.
    """
    pool, reason = await load()
    if not pool:
        logger.info("CANDIDATE_POOL_RESTORE status=%s", reason or "empty")
        return {"status": reason or "empty", "providers": 0, "models": 0}

    try:
        registered = set(manager.list_providers())
    except Exception:  # noqa: BLE001 — a manager without the API cannot be fed
        registered = set()
    applied = 0
    models = 0
    for name, model_ids in pool.items():
        if name not in registered:
            continue
        manager.set_model_candidates(name, list(model_ids))
        applied += 1
        models += len(model_ids)
    status = "restored" if applied else "no_registered_provider"
    logger.info(
        "CANDIDATE_POOL_RESTORE status=%s providers=%d models=%d",
        status, applied, models,
    )
    return {"status": status, "providers": applied, "models": models}
