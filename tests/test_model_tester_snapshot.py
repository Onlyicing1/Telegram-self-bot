"""
Persistent Test Modules result snapshot — storage, display, refresh, safety.

The durable document is a fake ``bot_settings`` store (the two generic
``db_client`` helpers are patched), so these tests never touch Supabase,
Telegram or a provider. The REAL snapshot module, the REAL handlers and the
REAL streaming tester wiring are exercised.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.ai import test_snapshot as ts
from backend.ai.model_tester import proven_usable_candidates
from backend.bot.handlers import ai as ai_module
from backend.bot.handlers import ai_test_progress
from backend.db import client as db_client
from backend.helper import symbols as sym

_PICK = "action:ai_pick_model:"


# ── fake durable store + fixtures ──


class _MemoryStore:
    def __init__(self) -> None:
        self.docs: dict[str, str] = {}
        self.readable = True
        self.writable = True


@pytest.fixture
def store(monkeypatch):
    mem = _MemoryStore()

    async def _read(key):
        if not mem.readable:
            return None, False
        return mem.docs.get(key), True

    async def _write(key, value):
        if not mem.writable:
            return False
        mem.docs[key] = value
        return True

    monkeypatch.setattr(db_client, "settings_document_state", _read)
    monkeypatch.setattr(db_client, "settings_document_write", _write)
    ai_module._last_test_payload = {}
    ai_module._test_running = False
    yield mem
    ai_module._last_test_payload = {}
    ai_module._test_running = False


def _payload(
    *,
    provider: str = "groq",
    models: tuple[str, ...] = ("m1", "m2", "m3"),
    statuses: dict[str, str] | None = None,
    catalog: tuple[str, ...] | None = None,
    tested_at: str = "2026-11-05T10:00:00+00:00",
    partial: bool = False,
    default_model: str = "",
) -> dict:
    statuses = statuses or {}
    results = []
    for model in models:
        status = statuses.get(model, "AVAILABLE")
        results.append({
            "provider": provider,
            "display_name": provider.title(),
            "icon": "◇",
            "model": model,
            "status": status,
            "error": None if status == "AVAILABLE" else "boom",
            "latency_s": 0.1 if status == "AVAILABLE" else None,
            "http_status": 200 if status == "AVAILABLE" else 500,
            "retry_after": None,
            "error_type": None,
            "provider_code": None,
            "finish_reason": "stop",
            "capabilities": [],
        })
    catalog_ids = catalog if catalog is not None else models
    return {
        "success": True,
        "tested_at": tested_at,
        "partial": partial,
        "providers": [{
            "name": provider,
            "display_name": provider.title(),
            "icon": "◇",
            "has_key": True,
            "validated": True,
            "default_model": default_model,
            "capability_kind": "chat",
        }],
        "models": [
            {"id": model, "name": model, "provider": provider, "context_length": 0, "is_free": False}
            for model in catalog_ids
        ],
        "results": results,
        "summary": {},
    }


def _flatten(buttons) -> list[str]:
    datas: list[str] = []
    for row in buttons or []:
        for btn in row or []:
            data = getattr(btn, "data", b"")
            datas.append(data.decode() if isinstance(data, bytes) else str(data))
    return datas


def _labels(buttons) -> list[str]:
    return [str(getattr(btn, "text", "")) for row in buttons or [] for btn in row or []]


async def _seed(store, payload: dict | None = None) -> dict:
    snapshot = ts.build_snapshot(payload or _payload())
    assert await ts.save_snapshot(snapshot) is True
    return snapshot


# ── persistent storage ──


@pytest.mark.asyncio
async def test_save_and_load_round_trip(store):
    await _seed(store)

    loaded = await ts.load_snapshot()

    assert loaded.state == ts.STATE_OK
    assert loaded.ok is True
    assert [r["model"] for r in loaded.snapshot["results"]] == ["m1", "m2", "m3"]
    assert loaded.snapshot["last_completed_at"] == "2026-11-05T10:00:00+00:00"
    assert loaded.snapshot["complete"] is True


@pytest.mark.asyncio
async def test_snapshot_survives_module_state_reset(store):
    """No module-level cache: the document is the only source of truth."""
    await _seed(store)
    ts.load_snapshot.__globals__.pop("_cache", None)  # nothing to clear, by design

    first = await ts.load_snapshot()
    second = await ts.load_snapshot()

    assert first.snapshot == second.snapshot
    assert json.loads(store.docs[ts.SNAPSHOT_KEY])["schema_version"] == ts.SCHEMA_VERSION


@pytest.mark.asyncio
async def test_missing_snapshot_is_missing(store):
    loaded = await ts.load_snapshot()
    assert loaded.state == ts.STATE_MISSING


@pytest.mark.asyncio
async def test_malformed_json_is_malformed_not_missing(store):
    store.docs[ts.SNAPSHOT_KEY] = "{not json"

    loaded = await ts.load_snapshot()

    assert loaded.state == ts.STATE_MALFORMED
    assert loaded.snapshot is None


@pytest.mark.asyncio
async def test_malformed_structure_is_malformed(store):
    store.docs[ts.SNAPSHOT_KEY] = json.dumps({"schema_version": ts.SCHEMA_VERSION})

    loaded = await ts.load_snapshot()

    assert loaded.state == ts.STATE_MALFORMED


@pytest.mark.asyncio
async def test_unsupported_schema_version_is_reported(store):
    store.docs[ts.SNAPSHOT_KEY] = json.dumps({"schema_version": ts.SCHEMA_VERSION + 1})

    loaded = await ts.load_snapshot()

    assert loaded.state == ts.STATE_UNSUPPORTED
    assert "not supported" in loaded.detail


@pytest.mark.asyncio
async def test_storage_read_failure_is_unavailable_not_missing(store):
    store.readable = False

    loaded = await ts.load_snapshot()

    assert loaded.state == ts.STATE_UNAVAILABLE
    assert loaded.state != ts.STATE_MISSING


@pytest.mark.asyncio
async def test_write_failure_is_reported_and_keeps_the_previous_snapshot(store):
    await _seed(store)
    before = store.docs[ts.SNAPSHOT_KEY]

    store.writable = False
    saved = await ts.save_snapshot(ts.build_snapshot(_payload(models=("new1",))))

    assert saved is False
    assert store.docs[ts.SNAPSHOT_KEY] == before
    loaded = await ts.load_snapshot()
    assert [r["model"] for r in loaded.snapshot["results"]] == ["m1", "m2", "m3"]


@pytest.mark.asyncio
async def test_secrets_are_redacted_before_persistence(store):
    payload = _payload(models=("m1",), statuses={"m1": "AUTH_ERROR"})
    payload["results"][0]["error"] = "bad key sk-abcdefgh12345678 / Bearer secrettoken123456"

    await _seed(store, payload)

    stored = store.docs[ts.SNAPSHOT_KEY]
    assert "sk-abcdefgh12345678" not in stored
    assert "secrettoken123456" not in stored
    assert "***REDACTED***" in stored


@pytest.mark.asyncio
async def test_persisted_payload_is_bounded(store):
    payload = _payload(models=tuple(f"m{i:04d}" for i in range(1500)))
    for result in payload["results"]:
        result["error"] = "x" * 5000

    await _seed(store, payload)

    raw = store.docs[ts.SNAPSHOT_KEY]
    document = json.loads(raw)
    assert len(document["results"]) <= ts.MAX_RESULTS
    assert len(raw.encode("utf-8")) <= ts.MAX_DOCUMENT_BYTES
    assert all(len(str(r.get("error") or "")) <= ts.MAX_ERROR_CHARS for r in document["results"])


@pytest.mark.asyncio
async def test_provider_secrets_and_endpoints_are_never_persisted(store):
    payload = _payload()
    payload["providers"][0]["env_var"] = "AI_GROQ_API_KEY"
    payload["providers"][0]["base_url"] = "https://api.example/v1"

    await _seed(store, payload)

    stored = store.docs[ts.SNAPSHOT_KEY]
    assert "AI_GROQ_API_KEY" not in stored
    assert "api.example" not in stored


# ── reconciliation ──


@pytest.mark.asyncio
async def test_newly_discovered_models_are_not_tested(store):
    # Discovery found three models; the run only reached one of them.
    snapshot = await _seed(store, _payload(models=("m1",), catalog=("m1", "m2", "m3")))

    by_model = {r["model"]: r["status"] for r in snapshot["results"]}
    assert by_model == {"m1": "AVAILABLE", "m2": "NOT_TESTED", "m3": "NOT_TESTED"}
    assert snapshot["summary"]["not_tested"] == 2
    assert snapshot["summary"]["tested"] == 1


def test_removed_models_are_separated_as_historical():
    results = [
        {"provider": "groq", "model": "gone"},
        {"provider": "groq", "model": "kept"},
    ]
    catalog = [{"provider": "groq", "model": "kept", "display_name": "kept"}]

    current, historical = ts.reconcile(results, catalog)

    assert [r["model"] for r in current] == ["kept"]
    assert [r["model"] for r in historical] == ["gone"]
    assert historical[0]["historical"] is True


def test_matching_is_exact_on_provider_and_model_id():
    results = [{"provider": "groq", "model": "shared-id"}]
    catalog = [{"provider": "openai", "model": "shared-id", "display_name": "shared-id"}]

    current, historical = ts.reconcile(results, catalog)

    # Same model id under another provider is NOT a match.
    assert [r["model"] for r in historical] == ["shared-id"]
    assert current[0]["provider"] == "openai"
    assert current[0]["status"] == ts.STATUS_NOT_TESTED


def test_duplicate_pairs_are_collapsed_first_wins():
    results = [
        {"provider": "groq", "model": "m1", "status": "AVAILABLE"},
        {"provider": "groq", "model": "m1", "status": "INVALID_MODEL"},
    ]
    catalog = [{"provider": "groq", "model": "m1", "display_name": "m1"}]

    current, _historical = ts.reconcile(results, catalog)

    assert len(current) == 1
    assert current[0]["status"] == "AVAILABLE"


def test_display_name_never_matches_a_model():
    results = [{"provider": "groq", "model": "real-id", "display_name": "Pretty Name"}]
    catalog = [{"provider": "groq", "model": "Pretty Name", "display_name": "real-id"}]

    current, historical = ts.reconcile(results, catalog)

    assert [r["model"] for r in current] == ["Pretty Name"]
    assert [r["model"] for r in historical] == ["real-id"]


# ── cached display ──


@pytest.mark.asyncio
async def test_opening_with_a_snapshot_makes_zero_model_test_requests(store):
    await _seed(store)

    with patch(
        "backend.ai.model_tester.test_single_model",
        AsyncMock(side_effect=AssertionError("no live model test while opening")),
    ) as mock_single, patch(
        "backend.ai.model_tester.test_all_models_streaming",
        AsyncMock(side_effect=AssertionError("no diagnostic while opening")),
    ) as mock_all, patch.object(ai_module, "_runtime_pair", return_value=("groq", "m1")):
        title, body, _buttons = await ai_module._ai_test_models_action(MagicMock(), "", 0)

    mock_single.assert_not_called()
    mock_all.assert_not_called()
    assert title == "Test Modules"
    assert "Cached snapshot" in body
    assert "2026-11-05 10:00 UTC" in body
    assert ai_module._test_running is False


@pytest.mark.asyncio
async def test_cached_statuses_and_models_are_visibly_cached(store):
    await _seed(store, _payload(statuses={"m2": "RATE_LIMITED"}))

    with patch.object(ai_module, "_runtime_pair", return_value=("groq", "m1")):
        _, body, buttons = await ai_module._ai_test_models_action(MagicMock(), "", 0)

    assert "Cached snapshot" in body
    assert "Usable Models: 2" in body
    # Bounded body: model names are only reachable through the buttons.
    assert "`m1`" not in body and "`m3`" not in body
    cached_labels = [label for label in _labels(buttons) if "·cached" in label]
    assert len(cached_labels) == 2
    # Full per-model diagnostics (with cached labelling) stay one tap away.
    _title, details, _details_buttons = await ai_module._ai_test_details_action(None, "", 0)
    assert "RATE_LIMITED (CACHED)" in details
    assert json.loads(store.docs[ts.SNAPSHOT_KEY])["results"][0]["status"] == "AVAILABLE"


@pytest.mark.asyncio
async def test_cached_snapshot_preserves_summary_grouping_and_pagination(store):
    models = tuple(f"m{i:02d}" for i in range(20))
    await _seed(store, _payload(models=models))

    with patch.object(ai_module, "_runtime_pair", return_value=("groq", "m00")):
        _, body, buttons = await ai_module._ai_test_models_action(MagicMock(), "", 0)

    assert f"{sym.OP_SAVE} 20" in body
    # Bounded body: the usable total replaces the per-model provider listing.
    assert f"**{sym.OP_SAVE} Usable Models: 20**" in body
    assert f"{sym.ICON_MODEL} **Groq**" not in body
    assert len([d for d in _flatten(buttons) if d.startswith(_PICK)]) == 18
    assert "action:ai_test_page:1" in _flatten(buttons)

    cached_payload = ai_module._last_test_payload
    _, page_body, page_buttons = await ai_module._ai_test_page_action(None, "1", 0)
    assert ai_module._last_test_payload is cached_payload
    assert "Cached snapshot" in page_body
    assert len([d for d in _flatten(page_buttons) if d.startswith(_PICK)]) == 2


@pytest.mark.asyncio
async def test_snapshot_without_results_runs_the_initial_diagnostic(store):
    empty = ts.build_snapshot(_payload(models=()))
    await ts.save_snapshot(empty)

    with patch.object(ai_test_progress, "run_streaming_test", AsyncMock()) as mock_run:
        _, body, _ = await ai_module._ai_test_models_action(MagicMock(), "", 0)
        for _ in range(5):
            await asyncio.sleep(0)

    assert mock_run.call_count == 1
    assert "Starting the model test run" in body


# ── opening without a snapshot ──


@pytest.mark.asyncio
async def test_first_visit_without_a_snapshot_runs_the_diagnostic(store):
    with patch.object(ai_test_progress, "run_streaming_test", AsyncMock()) as mock_run:
        title, body, _ = await ai_module._ai_test_models_action(MagicMock(), "", 0)
        for _ in range(5):
            await asyncio.sleep(0)

    assert mock_run.call_count == 1
    assert title == "Test Modules"
    assert "update" in body.lower()


@pytest.mark.asyncio
async def test_unreadable_storage_never_starts_a_test_run(store):
    store.readable = False

    with patch.object(ai_test_progress, "run_streaming_test", AsyncMock()) as mock_run:
        _, body, buttons = await ai_module._ai_test_models_action(MagicMock(), "", 0)

    assert mock_run.call_count == 0
    assert "unavailable" in body.lower()
    assert "action:ai_test_refresh" in _flatten(buttons)


@pytest.mark.asyncio
async def test_unsupported_snapshot_never_starts_a_test_run(store):
    store.docs[ts.SNAPSHOT_KEY] = json.dumps({"schema_version": 99})

    with patch.object(ai_test_progress, "run_streaming_test", AsyncMock()) as mock_run:
        _, body, _ = await ai_module._ai_test_models_action(MagicMock(), "", 0)

    assert mock_run.call_count == 0
    assert "unsupported" in body.lower()


# ── explicit refresh ──


@pytest.mark.asyncio
async def test_refresh_action_starts_the_complete_existing_diagnostic(store):
    await _seed(store)

    with patch.object(ai_test_progress, "run_streaming_test", AsyncMock()) as mock_run:
        _, body, _ = await ai_module._ai_test_refresh_action(MagicMock(), "", 0)
        for _ in range(5):
            await asyncio.sleep(0)

    assert mock_run.call_count == 1
    assert "Starting the model test run" in body
    assert ai_module._test_running is True


@pytest.mark.asyncio
async def test_refresh_runs_the_tester_without_budget_or_cap(store):
    fake_event = MagicMock()
    fake_event.chat_id = 1
    fake_event.message_id = 2

    with patch.object(
        ai_test_progress, "test_all_models_streaming",
        AsyncMock(return_value=_payload(tested_at="2026-11-05T11:00:00+00:00")),
    ) as mock_all, patch("backend.helper.panels._safe_edit", AsyncMock(return_value=True)):
        await ai_test_progress.run_streaming_test(1, fake_event)
        for _ in range(30):
            await asyncio.sleep(0)

    mock_all.assert_awaited_once()
    _args, kwargs = mock_all.await_args
    assert "global_budget" not in kwargs, "a full refresh may not be capped"
    assert "max_per_provider" not in kwargs
    loaded = await ts.load_snapshot()
    assert loaded.snapshot["last_completed_at"] == "2026-11-05T11:00:00+00:00"


@pytest.mark.asyncio
async def test_refresh_never_reuses_cached_statuses(store):
    await _seed(store, _payload(statuses={}))

    fake_event = MagicMock()
    fake_event.chat_id = 1
    fake_event.message_id = 2
    fresh = _payload(statuses={"m1": "INVALID_MODEL"}, tested_at="2026-11-05T12:00:00+00:00")

    with patch.object(
        ai_test_progress, "test_all_models_streaming", AsyncMock(return_value=fresh),
    ), patch("backend.helper.panels._safe_edit", AsyncMock(return_value=True)):
        await ai_test_progress.run_streaming_test(1, fake_event)
        for _ in range(30):
            await asyncio.sleep(0)

    loaded = await ts.load_snapshot()
    by_model = {r["model"]: r["status"] for r in loaded.snapshot["results"]}
    assert by_model["m1"] == "INVALID_MODEL"
    assert loaded.snapshot["last_completed_at"] == "2026-11-05T12:00:00+00:00"
    assert ai_module._last_test_payload["cached"] is False
    assert ai_module._last_test_payload["snapshot_saved"] is True


@pytest.mark.asyncio
async def test_write_failure_shows_fresh_results_with_a_warning(store):
    store.writable = False

    payload = await ai_test_progress._persist_test_snapshot(_payload())

    assert payload["snapshot_saved"] is False
    assert payload.get("snapshot_skipped_partial") is None
    _body, _buttons = ai_module._render_test_results(payload, current=("groq", "m1"))
    assert "not saved" in _body


@pytest.mark.asyncio
async def test_partial_run_never_replaces_a_complete_snapshot(store):
    before = await _seed(store)

    payload = await ai_test_progress._persist_test_snapshot(
        _payload(models=("partial1",), partial=True)
    )

    assert payload["snapshot_saved"] is False
    assert payload["snapshot_skipped_partial"] is True
    loaded = await ts.load_snapshot()
    assert loaded.snapshot["last_completed_at"] == before["last_completed_at"]
    assert [r["model"] for r in loaded.snapshot["results"]] == ["m1", "m2", "m3"]


@pytest.mark.asyncio
async def test_cancellation_preserves_the_previous_complete_snapshot(store):
    before = await _seed(store)

    fake_event = MagicMock()
    fake_event.chat_id = 1
    fake_event.message_id = 2
    with patch.object(
        ai_test_progress, "test_all_models_streaming",
        AsyncMock(side_effect=asyncio.CancelledError()),
    ), patch("backend.helper.panels._safe_edit", AsyncMock(return_value=True)):
        with pytest.raises(asyncio.CancelledError):
            await ai_test_progress.run_streaming_test(1, fake_event)

    loaded = await ts.load_snapshot()
    assert loaded.snapshot["last_completed_at"] == before["last_completed_at"]
    assert ai_module._test_running is False


@pytest.mark.asyncio
async def test_a_second_refresh_while_running_launches_nothing(store):
    ai_module._test_running = True

    with patch.object(ai_test_progress, "run_streaming_test", AsyncMock()) as mock_run:
        _, body, _ = await ai_module._ai_test_refresh_action(MagicMock(), "", 0)

    assert mock_run.call_count == 0
    assert "Already running" in body


# ── production safety ──


@pytest.mark.asyncio
async def test_cached_available_never_enters_proven_usable_candidates(store):
    await _seed(store)
    loaded = await ts.load_snapshot()
    payload = ts.snapshot_to_payload(loaded.snapshot)

    assert payload["results"][0]["status"] == "AVAILABLE"
    assert payload["results"][0]["cached"] is True
    assert proven_usable_candidates(payload["results"], {}, []) == {}


def test_live_available_still_qualifies():
    live = [{
        "provider": "groq", "model": "m1", "status": "AVAILABLE",
        "error": None, "latency_s": 0.1,
    }]
    assert proven_usable_candidates(live, {}, []) == {"groq": ["m1"]}


@pytest.mark.asyncio
async def test_not_configured_stays_distinct_from_a_provider_failure(store):
    payload = _payload(models=("m1", "m2"), statuses={
        "m1": "NOT_CONFIGURED", "m2": "PROVIDER_ERROR",
    })
    snapshot = await _seed(store, payload)

    summary = snapshot["summary"]
    assert summary["not_configured"] == 1
    assert summary["provider_error"] == 1
    assert summary["not_tested"] == 0
    assert summary["tested"] == 1


@pytest.mark.asyncio
async def test_timeout_stays_distinct_from_not_tested(store):
    payload = _payload(models=("m1",), catalog=("m1", "m2"), statuses={"m1": "TIMEOUT"})
    snapshot = await _seed(store, payload)

    by_model = {r["model"]: r["status"] for r in snapshot["results"]}
    assert by_model == {"m1": "TIMEOUT", "m2": "NOT_TESTED"}
    assert snapshot["summary"]["timeout"] == 1
    assert snapshot["summary"]["not_tested"] == 1


def test_batch_and_streaming_payloads_build_compatible_snapshots():
    """Both tester twins expose the same payload keys the snapshot consumes."""
    import inspect

    from backend.ai import model_tester

    for name in ("test_all_models", "test_all_models_streaming"):
        source = inspect.getsource(getattr(model_tester, name))
        for key in ('"tested_at"', '"partial"', '"providers"', '"models"', '"results"', '"summary"'):
            assert key in source, f"{name} must expose {key}"


def test_refresh_action_is_registered():
    from backend.helper import get_action

    # ``register`` wires the id at import of the handler module in production.
    with patch.object(ai_module, "register_action") as mock_register_action, \
         patch.object(ai_module, "register_panel"), \
         patch.object(ai_module, "register_inline_builder"), \
         patch.object(ai_module, "register_input"):
        ai_module.register(None, 0)
    registered = [call.args[0] for call in mock_register_action.call_args_list]
    assert "ai_test_refresh" in registered
    assert "ai_test_models" in registered
    assert get_action("ai_test_page") is ai_module._test_modules_page_dispatch
