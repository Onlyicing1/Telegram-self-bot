"""
Persistent Test Modules result snapshot.

One complete Test Modules diagnostic is expensive — every eligible model of
every configured provider is probed live — so its COMPLETED result is stored
once and reused by the next visit. Opening the panel therefore reads a
snapshot; only an explicit "Refresh All Models" spends provider calls.

Storage reuses the existing durable ``bot_settings`` key/value JSON documents
(``backend.db.client``), never a module-level dict, a temporary file or a new
table. Unlike the Anti-Edit / Anti-Delete archive — which grows without bound
and therefore shards — a diagnostic result set is bounded by the catalog
itself, so it is kept in ONE validated document: a single write is atomic, a
failed write leaves the previous complete snapshot untouched, and there is no
half-written generation to reconcile. ``MAX_RESULTS`` / ``MAX_DOCUMENT_BYTES``
keep that document bounded, and an oversized result set is truncated
deterministically with an honest ``counts.truncated`` marker.

Correctness rules:

  - The snapshot is versioned and validated. ``schema_version`` is checked
    before anything is trusted; an unknown version is reported as
    ``unsupported_version`` — never silently read as "no snapshot".
  - A storage read failure is ``unavailable`` — never ``missing``. The UI must
    not spend a full diagnostic because the store was briefly unreachable.
  - Results are matched by the EXACT ``(provider, model_id)`` pair. A catalog
    model without a result is ``NOT_TESTED``; a result whose pair is not in the
    catalog is kept as historical information and is never presented as a
    currently discovered model.
  - Cached results carry ``cached: True``. They are a diagnostic record, never
    live verification, and ``model_tester.proven_usable_candidates`` ignores
    them, so a previously AVAILABLE model can never re-enter the production
    fallback pool on the strength of a cache read.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from backend.ai.model_tester import _build_summary, sanitize_error_message
from backend.db import client as db_client

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

#: The ONE durable document holding the latest snapshot.
SNAPSHOT_KEY = "ai_test_snapshot:v1"

#: Bounds. A complete run may surface ~600 models today; these caps keep the
#: persisted document bounded even if a catalog grows far beyond that.
MAX_RESULTS = 1000
MAX_HISTORICAL = 500
MAX_MODELS = 4000
MAX_PROVIDERS = 64
MAX_DOCUMENT_BYTES = 512 * 1024
MAX_ERROR_CHARS = 200
MAX_TEXT_CHARS = 120
MAX_STATUS_CHARS = 32
MAX_ID_CHARS = 200

STATUS_NOT_TESTED = "NOT_TESTED"

#: Load outcomes. ``unavailable`` is deliberately distinct from ``missing``.
STATE_OK = "ok"
STATE_MISSING = "missing"
STATE_MALFORMED = "malformed"
STATE_UNSUPPORTED = "unsupported_version"
STATE_UNAVAILABLE = "unavailable"


class InvalidSnapshot(Exception):
    """The stored document does not satisfy the snapshot contract."""


@dataclass(frozen=True)
class SnapshotLoad:
    """Outcome of one snapshot read — the state is never inferred from ``None``."""

    state: str
    snapshot: dict[str, Any] | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.state == STATE_OK

    @property
    def missing(self) -> bool:
        return self.state == STATE_MISSING


# ── small bounded coercion helpers ──


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any, limit: int = MAX_TEXT_CHARS) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:limit]


def _id(value: Any) -> str:
    return _text(value, MAX_ID_CHARS) or ""


def _status(value: Any) -> str:
    text = _text(value, MAX_STATUS_CHARS)
    return (text or "UNKNOWN_ERROR").upper()


def _number(value: Any) -> float | int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _integer(value: Any) -> int | None:
    number = _number(value)
    if number is None:
        return None
    try:
        return int(number)
    except (TypeError, ValueError):
        return None


def format_timestamp(value: Any) -> str:
    """Human-readable UTC stamp for the UI; never invents a time."""
    text = _text(value, 64)
    if not text:
        return "unknown"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# ── normalization: runtime payload -> persisted record ──


def _bounded_error(value: Any) -> str | None:
    text = _text(value, MAX_ERROR_CHARS * 4)
    if not text:
        return None
    # The tester's own redaction is the ONE sanitizer; re-applying it is
    # idempotent and covers a snapshot assembled from a foreign payload.
    return sanitize_error_message(text)[:MAX_ERROR_CHARS]


def normalize_result(item: dict[str, Any]) -> dict[str, Any] | None:
    """One bounded, sanitized result record (``None`` when unusable)."""
    if not isinstance(item, dict):
        return None
    provider = _id(item.get("provider"))
    model = _id(item.get("model"))
    if not provider or not model:
        return None
    return {
        "provider": provider,
        "model": model,
        "display_name": _text(item.get("display_name")) or provider,
        "icon": _text(item.get("icon"), 8) or "",
        "status": _status(item.get("status")),
        "error": _bounded_error(item.get("error")),
        "latency_s": _number(item.get("latency_s")),
        "http_status": _integer(item.get("http_status")),
        "retry_after": _number(item.get("retry_after")),
        "error_type": _text(item.get("error_type")),
        "provider_code": _text(item.get("provider_code")),
        "finish_reason": _text(item.get("finish_reason")),
        "is_free": bool(item.get("is_free")),
        "tested_at": _text(item.get("tested_at"), 64),
    }


def normalize_results(items: Any) -> tuple[list[dict[str, Any]], int]:
    """Normalized results plus the number of unusable records dropped."""
    source = list(items or [])
    results: list[dict[str, Any]] = []
    dropped = 0
    for item in source:
        record = normalize_result(item)
        if record is None:
            dropped += 1
            continue
        if len(results) >= MAX_RESULTS:
            dropped += 1
            continue
        results.append(record)
    return results, dropped


def normalize_models(items: Any) -> list[dict[str, Any]]:
    """Catalog metadata (discovery payload ``ModelInfo.__dict__``) — bounded."""
    models: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in items or []:
        if not isinstance(item, dict):
            continue
        model_id = _id(item.get("id"))
        provider = _id(item.get("provider"))
        if not model_id:
            continue
        pair = (provider, model_id)
        if pair in seen:
            continue
        seen.add(pair)
        models.append({
            "id": model_id,
            "name": _text(item.get("name")) or model_id,
            "provider": provider,
            "context_length": _integer(item.get("context_length")) or 0,
            "is_free": bool(item.get("is_free")),
        })
        if len(models) >= MAX_MODELS:
            break
    return models


def normalize_providers(items: Any) -> list[dict[str, Any]]:
    """Provider identity only — never an env var, key, header or base URL."""
    providers: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items or []:
        if not isinstance(item, dict):
            continue
        name = _id(item.get("name"))
        if not name or name in seen:
            continue
        seen.add(name)
        providers.append({
            "name": name,
            "display_name": _text(item.get("display_name")) or name,
            "icon": _text(item.get("icon"), 8) or "",
            "status": _text(item.get("status"), MAX_STATUS_CHARS) or "",
            "has_key": bool(item.get("has_key")),
            "validated": bool(item.get("validated")),
            "default_model": _id(item.get("default_model")),
            "capability_kind": _text(item.get("capability_kind"), 32) or "chat",
        })
        if len(providers) >= MAX_PROVIDERS:
            break
    return providers


# ── reconciliation ──


def build_catalog(
    models: list[dict[str, Any]],
    results: list[dict[str, Any]],
    providers: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """The run's eligible catalog: discovery metadata, tested pairs, defaults.

    A tested pair belongs to the catalog by definition (the run selected it as
    an eligible target), and a provider's configured/default model is eligible
    even when ``/models`` never listed it. Exact ``(provider, model_id)`` pairs
    dedupe the three sources.
    """
    catalog: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(provider: str, model_id: str, name: str = "", is_free: bool = False) -> None:
        if not provider or not model_id:
            return
        pair = (provider, model_id)
        if pair in seen:
            return
        seen.add(pair)
        catalog.append({
            "provider": provider,
            "model": model_id,
            "display_name": _text(name) or model_id,
            "is_free": bool(is_free),
        })

    for entry in models:
        add(str(entry.get("provider") or ""), str(entry.get("id") or ""),
            str(entry.get("name") or ""), bool(entry.get("is_free")))
    for entry in results:
        add(str(entry.get("provider") or ""), str(entry.get("model") or ""),
            str(entry.get("display_name") or ""), bool(entry.get("is_free")))
    for provider in providers:
        add(str(provider.get("name") or ""), str(provider.get("default_model") or ""),
            str(provider.get("display_name") or ""))
    return catalog


def not_tested_result(entry: dict[str, Any]) -> dict[str, Any]:
    """A catalog model with no stored result — explicitly NOT TESTED."""
    return {
        "provider": str(entry.get("provider") or ""),
        "model": str(entry.get("model") or ""),
        "display_name": _text(entry.get("display_name")) or str(entry.get("model") or ""),
        "icon": "",
        "status": STATUS_NOT_TESTED,
        "error": None,
        "latency_s": None,
        "http_status": None,
        "retry_after": None,
        "error_type": None,
        "provider_code": None,
        "finish_reason": None,
        "is_free": bool(entry.get("is_free")),
        "tested_at": None,
    }


def reconcile(
    results: list[dict[str, Any]],
    catalog: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split results into (current catalog, historical) and add NOT_TESTED rows.

    Matching is EXACT on ``(provider, model_id)`` — never a display name. The
    first occurrence of a pair wins, so a duplicated record can never present
    two different statuses for one model. A catalog entry without a result is
    reported NOT_TESTED rather than assumed usable or unusable.
    """
    catalog_pairs = {
        (str(entry.get("provider") or ""), str(entry.get("model") or ""))
        for entry in catalog
    }
    current: list[dict[str, Any]] = []
    historical: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in results:
        pair = (str(item.get("provider") or ""), str(item.get("model") or ""))
        if pair in seen:
            continue
        seen.add(pair)
        if pair in catalog_pairs:
            current.append(item)
        else:
            historical.append({**item, "historical": True})

    for entry in catalog:
        pair = (str(entry.get("provider") or ""), str(entry.get("model") or ""))
        if pair in seen or len(current) >= MAX_RESULTS:
            continue
        seen.add(pair)
        current.append(not_tested_result(entry))
    return current, historical[:MAX_HISTORICAL]


# ── build ──


def build_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    """Convert one diagnostic payload into a validated, bounded snapshot."""
    source = payload or {}
    results, dropped = normalize_results(source.get("results") or [])
    models = normalize_models(source.get("models") or [])
    providers = normalize_providers(source.get("providers") or [])

    catalog = build_catalog(models, results, providers)
    current, historical = reconcile(results, catalog)

    partial = bool(source.get("partial"))
    tested_at = _text(source.get("tested_at"), 64) or _now()
    return {
        "schema_version": SCHEMA_VERSION,
        "created_at": _now(),
        "last_completed_at": tested_at,
        "complete": not partial,
        "partial": partial,
        "summary": _build_summary(current, len(catalog)),
        "providers": providers,
        "models": models,
        "results": current,
        "historical": historical,
        "counts": {
            "results": len(current),
            "catalog": len(catalog),
            "historical": len(historical),
            "dropped": dropped,
            "truncated": False,
        },
    }


# ── exposed payload for the existing renderers ──


def snapshot_to_payload(
    snapshot: dict[str, Any],
    *,
    refresh_running: bool = False,
) -> dict[str, Any]:
    """A payload-shaped, cached-marked view of a stored snapshot.

    ``cached: True`` on the payload and on every result is what keeps a cache
    read out of the production candidate feed and off the "live verified"
    vocabulary. No provider call is made here.
    """
    results = [{**item, "cached": True} for item in snapshot.get("results", [])]
    historical = [
        {**item, "cached": True, "historical": True}
        for item in snapshot.get("historical", [])
    ]
    counts = snapshot.get("counts") or {}
    return {
        "success": True,
        "cached": True,
        "tested_at": snapshot.get("last_completed_at"),
        "snapshot_created_at": snapshot.get("created_at"),
        "snapshot_last_completed_at": snapshot.get("last_completed_at"),
        "snapshot_complete": bool(snapshot.get("complete")),
        "snapshot_partial": bool(snapshot.get("partial")),
        "catalog_count": int(counts.get("catalog") or 0),
        "result_count": len(results),
        "historical_count": len(historical),
        "refresh_running": bool(refresh_running),
        "providers": snapshot.get("providers", []),
        "models": snapshot.get("models", []),
        "results": results,
        "historical_results": historical,
        "partial": bool(snapshot.get("partial")),
        "summary": snapshot.get("summary", {}),
    }


# ── persistence ──


def _dump(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str)


def _fitted(document: dict[str, Any]) -> str:
    """Serialize within ``MAX_DOCUMENT_BYTES``, trimming results deterministically.

    A catalog this large is already exceptional; the trim keeps the durable
    document bounded instead of risking a rejected or slow write. The
    ``counts.truncated`` marker makes the loss visible in the UI.
    """
    doc = dict(document)
    results = list(doc.get("results") or [])
    counts = dict(doc.get("counts") or {})
    while True:
        doc["results"] = results
        doc["counts"] = counts
        payload = _dump(doc)
        if len(payload.encode("utf-8")) <= MAX_DOCUMENT_BYTES:
            return payload
        counts["truncated"] = True
        if not results:
            return _dump(doc)
        results = results[: max(0, len(results) // 2)]


async def save_snapshot(snapshot: dict[str, Any]) -> bool:
    """Persist one snapshot. True only when the durable write landed.

    The document is written in ONE call, so a failure leaves the previous
    complete snapshot exactly as it was — never a half-written result set.
    """
    payload = _fitted(snapshot)
    try:
        saved = await db_client.settings_document_write(SNAPSHOT_KEY, payload)
    except Exception as exc:  # pragma: no cover - defensive, the helper never raises
        logger.warning("[TEST_SNAPSHOT] write failed: %s", exc)
        return False
    if not saved:
        logger.warning("[TEST_SNAPSHOT] the snapshot write did not land")
    return bool(saved)


def _validated(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise InvalidSnapshot("stored snapshot is not an object")
    version = document.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise InvalidSnapshot("snapshot schema version is missing")
    if version != SCHEMA_VERSION:
        raise InvalidSnapshot(f"snapshot schema v{version} is not supported")

    for key in ("created_at", "last_completed_at"):
        if not isinstance(document.get(key), str) or not document.get(key):
            raise InvalidSnapshot(f"snapshot is missing {key}")
    if not isinstance(document.get("complete"), bool) or not isinstance(document.get("partial"), bool):
        raise InvalidSnapshot("snapshot completion state is not boolean")
    for key in ("summary", "counts"):
        if not isinstance(document.get(key), dict):
            raise InvalidSnapshot(f"snapshot {key} is not an object")
    for key in ("providers", "models", "results", "historical"):
        if not isinstance(document.get(key), list):
            raise InvalidSnapshot(f"snapshot {key} is not a list")

    results, dropped = normalize_results(document.get("results"))
    if not results and document.get("results") and dropped:
        raise InvalidSnapshot("snapshot results are unusable")
    historical, _dropped = normalize_results(document.get("historical"))
    return {
        "schema_version": SCHEMA_VERSION,
        "created_at": document["created_at"],
        "last_completed_at": document["last_completed_at"],
        "complete": document["complete"],
        "partial": document["partial"],
        "summary": document["summary"],
        "counts": document["counts"],
        "providers": normalize_providers(document.get("providers")),
        "models": normalize_models(document.get("models")),
        "results": results,
        "historical": [
            {**item, "historical": True} for item in historical[:MAX_HISTORICAL]
        ],
    }


async def load_snapshot() -> SnapshotLoad:
    """Read the persisted snapshot and classify the outcome honestly."""
    try:
        raw, readable = await db_client.settings_document_state(SNAPSHOT_KEY)
    except Exception as exc:  # pragma: no cover - defensive, the helper never raises
        logger.warning("[TEST_SNAPSHOT] read failed: %s", exc)
        return SnapshotLoad(STATE_UNAVAILABLE, detail="snapshot storage could not be read")

    if not readable:
        return SnapshotLoad(STATE_UNAVAILABLE, detail="snapshot storage could not be read")
    if not raw:
        return SnapshotLoad(STATE_MISSING)

    try:
        document = json.loads(raw)
    except Exception:
        return SnapshotLoad(STATE_MALFORMED, detail="stored snapshot is not valid JSON")
    if not isinstance(document, dict):
        return SnapshotLoad(STATE_MALFORMED, detail="stored snapshot is not an object")

    version = document.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        return SnapshotLoad(STATE_MALFORMED, detail="snapshot schema version is missing")
    if version != SCHEMA_VERSION:
        return SnapshotLoad(
            STATE_UNSUPPORTED,
            detail=f"snapshot schema v{version} is not supported (expected v{SCHEMA_VERSION})",
        )

    try:
        snapshot = _validated(document)
    except InvalidSnapshot as exc:
        return SnapshotLoad(STATE_MALFORMED, detail=str(exc))
    return SnapshotLoad(STATE_OK, snapshot)
