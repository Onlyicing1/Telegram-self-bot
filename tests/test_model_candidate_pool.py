"""Production model-candidate pool: publication, restart restore, safety.

Pins the defect this hotfix closes: the live candidate pool is memory-only, so
after a restart production routing saw one configured model per provider even
though a complete Test Modules run had proved a larger set. The durable feed is
bounded and freshness-checked, only a COMPLETE run may publish it, and every
restored candidate is still re-proven by the router's real attempt.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from backend.ai import candidate_pool, model_tester
from backend.ai.providers.base.capabilities import ProviderCapabilities
from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.base.contract import BaseProvider, ProviderResponse
from backend.ai.providers.manager.manager import ProviderManager
from backend.db import client as db_client


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _Store:
    def __init__(self, document: str | None = None, *, readable: bool = True):
        self.document = document
        self.readable = readable
        self.writes: list[tuple[str, str]] = []
        self.fail_write = False

    async def write(self, key: str, value: str) -> bool:
        self.writes.append((key, value))
        if self.fail_write:
            return False
        self.document = value
        return True

    async def state(self, key: str) -> tuple[str | None, bool]:
        return self.document, self.readable


@pytest.fixture
def _store(monkeypatch):
    store = _Store()
    monkeypatch.setattr(db_client, "settings_document_write", store.write)
    monkeypatch.setattr(db_client, "settings_document_state", store.state)
    return store


class _StubProvider(BaseProvider):
    def __init__(self, name: str = "p", model: str = "primary", responses=None):
        super().__init__(ProviderConfig(provider_name=name, enabled=True, default_model=model))
        self._name = name
        self._responses = list(responses or [])
        self.calls: list[str] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supports_tools=True, supports_function_call=True, supports_json=True,
        )

    async def chat(self, messages, **kwargs: Any) -> ProviderResponse:
        model = str(kwargs.get("model") or self._config.default_model or "")
        self.calls.append(model)
        if self._responses:
            return self._responses.pop(0)
        return ProviderResponse(text="ok", provider_name=self._name, success=True)

    def initialize(self) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def health(self) -> dict[str, Any]:
        return {"healthy": True}


class _Status:
    def __init__(
        self, name: str, *, kind: str = "chat", has_key: bool = True,
        default_model: str = "",
    ):
        self.name = name
        self.capability_kind = kind
        self.has_key = has_key
        self.default_model = default_model


def _manager(*names: str) -> ProviderManager:
    pm = ProviderManager()
    for name in names:
        pm.register_provider(_StubProvider(name))
    pm.switch_provider(names[0])
    return pm


def _available(provider: str, model: str, **extra: Any) -> dict[str, Any]:
    return {"provider": provider, "model": model, "status": "AVAILABLE", **extra}


# ── durable document ────────────────────────────────────────────────────────

def test_publish_and_load_roundtrip_keeps_order_and_empty_providers(_store):
    feed = {"alpha": ["m1", "m2", "m3"], "beta": [], "dummy": ["nope"]}

    assert _run(candidate_pool.publish(feed)) is True

    pool, reason = _run(candidate_pool.load())
    assert reason == ""
    assert pool == {"alpha": ["m1", "m2", "m3"], "beta": []}


def test_a_failed_durable_write_is_reported_honestly(_store):
    _store.fail_write = True
    assert _run(candidate_pool.publish({"alpha": ["m1"]})) is False


def test_missing_unreadable_and_malformed_documents_are_distinct(_store):
    assert _run(candidate_pool.load()) == ({}, "missing")

    _store.readable = False
    assert _run(candidate_pool.load()) == ({}, "unavailable")

    _store.readable = True
    _store.document = "not json"
    assert _run(candidate_pool.load()) == ({}, "malformed")

    _store.document = json.dumps({"schema_version": 99, "published_at": "x", "providers": {}})
    assert _run(candidate_pool.load()) == ({}, "unsupported_version")


def test_a_stale_document_is_never_restored_as_current_availability(_store):
    old = (datetime.now(timezone.utc) - timedelta(seconds=candidate_pool.MAX_AGE_S + 60)).isoformat()
    _store.document = json.dumps({
        "schema_version": candidate_pool.SCHEMA_VERSION,
        "published_at": old,
        "providers": {"p": ["m1"]},
    })
    assert _run(candidate_pool.load()) == ({}, "stale")


def test_a_fresh_document_restores_every_model_in_order(_store):
    _store.document = json.dumps({
        "schema_version": candidate_pool.SCHEMA_VERSION,
        "published_at": datetime.now(timezone.utc).isoformat(),
        "providers": {"p": ["m1", "m2"], "q": ["x1"]},
    })
    pm = _manager("p", "q")

    result = _run(candidate_pool.restore(pm))

    assert result == {"status": "restored", "providers": 2, "models": 3}
    assert pm._model_candidates["p"] == ["m1", "m2"]
    assert pm._model_candidates["q"] == ["x1"]


def test_restore_ignores_providers_that_are_not_registered(_store):
    _store.document = json.dumps({
        "schema_version": candidate_pool.SCHEMA_VERSION,
        "published_at": datetime.now(timezone.utc).isoformat(),
        "providers": {"ghost": ["gone-1", "gone-2"]},
    })
    pm = _manager("p")

    result = _run(candidate_pool.restore(pm))

    assert result["status"] == "no_registered_provider"
    assert "ghost" not in pm._model_candidates


def test_repeated_restore_never_duplicates_candidates(_store):
    _store.document = json.dumps({
        "schema_version": candidate_pool.SCHEMA_VERSION,
        "published_at": datetime.now(timezone.utc).isoformat(),
        "providers": {"p": ["m1", "m2"]},
    })
    pm = _manager("p")

    _run(candidate_pool.restore(pm))
    _run(candidate_pool.restore(pm))

    assert pm._model_candidates["p"] == ["m1", "m2"]


# ── Test Modules publication ────────────────────────────────────────────────

def test_a_complete_feed_arms_every_provider_and_reports_empty_ones(monkeypatch):
    pm = _manager("p", "q")
    monkeypatch.setattr(
        "backend.ai.engine.engine.get_engine", lambda: SimpleNamespace(provider_manager=pm),
    )

    feed = model_tester._feed_production_candidates(
        [_available("p", "m1"), _available("p", "m2")],
        {},
        [_Status("p"), _Status("q")],
    )

    assert feed == {"p": ["m1", "m2"], "q": []}
    assert pm._model_candidates["p"] == ["m1", "m2"]
    assert "q" not in pm._model_candidates


def test_a_partial_run_cannot_destroy_the_existing_pool(monkeypatch):
    pm = _manager("p")
    pm.set_model_candidates("p", ["kept-1", "kept-2"])
    monkeypatch.setattr(
        "backend.ai.engine.engine.get_engine", lambda: SimpleNamespace(provider_manager=pm),
    )

    feed = model_tester._feed_production_candidates(
        [_available("p", "new-only")], {}, [_Status("p")], partial=True,
    )

    assert feed is None
    assert pm._model_candidates["p"] == ["kept-1", "kept-2"]


def test_a_partial_test_run_publishes_nothing(monkeypatch):
    published: list[dict] = []

    async def _record(feed):
        published.append(feed)
        return True

    async def _slow(*args, **kwargs):
        await asyncio.sleep(5)
        return {}

    async def _targets(providers):
        return (
            [{"provider": "p", "display_name": "P", "icon": "x", "model": "m1"}],
            [],
            {},
        )

    monkeypatch.setattr(model_tester, "discover_providers", lambda **kw: _async([]))
    monkeypatch.setattr(model_tester, "_build_targets", _targets)
    monkeypatch.setattr(model_tester, "_run_test_with_semaphore", _slow)
    monkeypatch.setattr("backend.ai.candidate_pool.publish", _record)

    data = _run(model_tester.test_all_models(owner_id=0, overall_timeout=0.01))

    assert data["partial"] is True
    assert published == []


def _async(value):
    async def _coro():
        return value

    return _coro()


# ── restored candidates are really considered and revalidated ───────────────

def test_restored_candidates_are_attempted_and_revalidated_by_routing(_store, monkeypatch):
    monkeypatch.setattr("backend.ai.providers.manager.manager._RETRY_BACKOFF_SECONDS", 0.0)
    _store.document = json.dumps({
        "schema_version": candidate_pool.SCHEMA_VERSION,
        "published_at": datetime.now(timezone.utc).isoformat(),
        "providers": {"p": ["m1", "m2"]},
    })
    pm = ProviderManager()
    provider = _StubProvider(
        "p",
        model="primary",
        responses=[
            # The configured model is attempted (and its existing one-shot
            # transient retry runs) before routing moves to the NEXT model.
            ProviderResponse(text="503 high demand", provider_name="p", success=False,
                             metadata={"http_status": 503}),
            ProviderResponse(text="503 high demand", provider_name="p", success=False,
                             metadata={"http_status": 503}),
            ProviderResponse(text="ok", provider_name="p", success=True),
        ],
    )
    pm.register_provider(provider)
    pm.switch_provider("p")

    _run(candidate_pool.restore(pm))
    response = _run(pm.chat([{"role": "user", "content": "summarize"}]))

    assert response.success is True
    assert provider.calls[:2] == ["primary", "primary"]
    assert provider.calls[2] == "m1", "a restored sibling candidate must be attempted"


def test_a_stale_pool_leaves_the_manager_empty(_store):
    old = (datetime.now(timezone.utc) - timedelta(seconds=candidate_pool.MAX_AGE_S + 60)).isoformat()
    _store.document = json.dumps({
        "schema_version": candidate_pool.SCHEMA_VERSION,
        "published_at": old,
        "providers": {"p": ["m1"]},
    })
    pm = _manager("p")

    result = _run(candidate_pool.restore(pm))

    assert result["status"] == "stale"
    assert pm._model_candidates == {}


def test_the_supervisor_restores_candidates_at_boot(monkeypatch):
    from backend.runtime.supervisor import RuntimeSupervisor

    calls: list[Any] = []
    manager = object()

    async def _restore(target):
        calls.append(target)
        return {"status": "restored", "providers": 1, "models": 2}

    monkeypatch.setattr("backend.ai.candidate_pool.restore", _restore)
    monkeypatch.setattr(
        "backend.ai.engine.engine.get_engine",
        lambda: SimpleNamespace(provider_manager=manager),
    )

    _run(RuntimeSupervisor._restore_ai_model_candidates(object()))

    assert calls == [manager]
