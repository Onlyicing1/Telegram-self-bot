"""Dynamic, test-verified production model fallback.

These tests exercise the REAL ``ProviderManager`` routing path. Every
assertion is about what the manager actually did — which (provider, model)
was asked, in what order, with which request, and how much overlapped — never
about an internal list being populated.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from backend.ai import model_tester
from backend.ai.providers.base.capabilities import ProviderCapabilities
from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.base.contract import BaseProvider, ProviderResponse
from backend.ai.providers.manager import manager as manager_module
from backend.ai.providers.manager.health import (
    ProviderHealthState,
    classify_rate_limit_scope,
)
from backend.ai.providers.manager.manager import ProviderManager


# ── Doubles ──


class _Probe:
    """Shared record of every model call, and how many overlapped."""

    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.calls: list[tuple[str, str]] = []
        self.messages: list[list[dict[str, Any]]] = []

    def enter(self, provider: str, model: str, messages: list[dict[str, Any]]) -> None:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.calls.append((provider, model))
        self.messages.append(messages)

    def exit(self) -> None:
        self.active -= 1


class _ModelProvider(BaseProvider):
    """Scripted provider that records the model each request actually asked."""

    def __init__(
        self,
        name: str,
        responses: list[ProviderResponse] | None = None,
        model: str = "",
        probe: _Probe | None = None,
    ) -> None:
        super().__init__(
            ProviderConfig(provider_name=name, enabled=True, default_model=model)
        )
        self._name = name
        self._responses = list(responses or [])
        self._probe = probe if probe is not None else _Probe()
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supports_tools=True, supports_function_call=True, supports_json=True,
        )

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> ProviderResponse:
        model = str(kwargs.get("model") or self._config.default_model or "")
        self.calls += 1
        self._probe.enter(self._name, model, messages)
        try:
            # Two yields: a parallel fan-out would overlap right here.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            if self._responses:
                return self._responses.pop(0)
            return ProviderResponse(text="ok", provider_name=self._name, success=True)
        finally:
            self._probe.exit()

    def initialize(self) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def health(self) -> dict[str, Any]:
        return {"healthy": True}


class _ProviderStatus:
    """Minimal stand-in for a discovered provider record."""

    def __init__(
        self,
        name: str,
        default_model: str = "",
        capability_kind: str = "chat",
        has_key: bool = True,
    ) -> None:
        self.name = name
        self.default_model = default_model
        self.capability_kind = capability_kind
        self.has_key = has_key


class _EngineStub:
    def __init__(self, pm: ProviderManager) -> None:
        self.provider_manager = pm


def _fail(text: str = "failed", **meta: Any) -> ProviderResponse:
    return ProviderResponse(text=text, provider_name="", success=False, metadata=meta)


def _ok(text: str = "ok") -> ProviderResponse:
    return ProviderResponse(text=text, provider_name="", success=True)


def _available(provider: str, model: str, **extra: Any) -> dict[str, Any]:
    return {
        "provider": provider,
        "model": model,
        "status": "AVAILABLE",
        "latency_s": 0.1,
        "http_status": 200,
        **extra,
    }


def _feed(
    monkeypatch: pytest.MonkeyPatch,
    pm: ProviderManager,
    results: list[dict[str, Any]],
    providers: list[_ProviderStatus],
    complete_models: dict[str, list[Any]] | None = None,
) -> None:
    """Publish diagnostic results through the REAL production feed path."""
    import backend.ai.engine.engine as engine_module

    monkeypatch.setattr(engine_module, "get_engine", lambda: _EngineStub(pm))
    model_tester._feed_production_candidates(results, complete_models or {}, providers)


@pytest.fixture(autouse=True)
def _no_retry_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the ONE bounded retry, but not its wall-clock sleep."""
    monkeypatch.setattr(manager_module, "_RETRY_BACKOFF_SECONDS", 0.0)


# ── Dynamic pool completeness ──


@pytest.mark.asyncio
async def test_twenty_available_models_are_not_truncated(monkeypatch):
    """A 20-model AVAILABLE pool is published whole and every model is tried."""
    pm = ProviderManager()
    results = [_available("p", f"m{i:02d}") for i in range(20)]
    _feed(monkeypatch, pm, results, [_ProviderStatus("p")])

    assert pm._model_candidates["p"] == [f"m{i:02d}" for i in range(20)]

    probe = _Probe()
    p = _ModelProvider(
        "p",
        [_fail(http_status=404, failure_type="model_not_found") for _ in range(20)],
        probe=probe,
    )
    pm.register_provider(p)
    pm.switch_provider("p")

    response = await pm.chat([{"role": "user", "content": "hi"}])

    assert response.success is False
    assert [model for _, model in probe.calls] == [f"m{i:02d}" for i in range(20)]
    assert p.calls == 20, "no candidate may be silently dropped"


@pytest.mark.asyncio
async def test_configured_primary_model_is_attempted_first():
    pm = ProviderManager()
    probe = _Probe()
    p = _ModelProvider("p", [_ok()], model="primary", probe=probe)
    pm.register_provider(p)
    pm.switch_provider("p")
    pm.set_model_candidates("p", ["free-1", "free-2"])

    await pm.chat([{"role": "user", "content": "hi"}])

    assert probe.calls == [("p", "primary")]


@pytest.mark.asyncio
async def test_remaining_candidates_follow_deterministic_order():
    pm = ProviderManager()
    probe = _Probe()
    p = _ModelProvider(
        "p",
        [_fail(http_status=404, failure_type="model_not_found") for _ in range(4)],
        model="primary",
        probe=probe,
    )
    pm.register_provider(p)
    pm.switch_provider("p")
    pm.set_model_candidates("p", ["free-1", "free-2", "free-3"])

    await pm.chat([{"role": "user", "content": "hi"}])

    assert [model for _, model in probe.calls] == ["primary", "free-1", "free-2", "free-3"]


@pytest.mark.asyncio
async def test_original_request_reaches_every_candidate_unchanged():
    pm = ProviderManager()
    probe = _Probe()
    p = _ModelProvider(
        "p",
        [_fail(http_status=404, failure_type="model_not_found") for _ in range(2)],
        model="primary",
        probe=probe,
    )
    pm.register_provider(p)
    pm.switch_provider("p")
    pm.set_model_candidates("p", ["free-1"])
    messages = [{"role": "user", "content": "the exact original request"}]

    await pm.chat(messages)

    assert len(probe.messages) == 2
    for seen in probe.messages:
        assert seen is messages
        assert seen == [{"role": "user", "content": "the exact original request"}]


# ── Sequential, first-usable-response-wins ──


@pytest.mark.asyncio
async def test_failed_first_model_is_followed_by_exactly_one_next_model():
    pm = ProviderManager()
    probe = _Probe()
    p = _ModelProvider(
        "p",
        [_fail(http_status=404, failure_type="model_not_found"), _ok()],
        model="m1",
        probe=probe,
    )
    pm.register_provider(p)
    pm.switch_provider("p")
    pm.set_model_candidates("p", ["m2", "m3"])

    response = await pm.chat([{"role": "user", "content": "hi"}])

    assert response.success is True
    assert [model for _, model in probe.calls] == ["m1", "m2"]


@pytest.mark.asyncio
async def test_successful_first_model_blocks_every_later_candidate():
    pm = ProviderManager()
    probe = _Probe()
    p = _ModelProvider("p", [_ok()], model="m1", probe=probe)
    pm.register_provider(p)
    pm.switch_provider("p")
    pm.set_model_candidates("p", ["m2", "m3", "m4", "m5"])

    response = await pm.chat([{"role": "user", "content": "hi"}])

    assert response.success is True
    assert [model for _, model in probe.calls] == ["m1"]
    assert p.calls == 1


@pytest.mark.asyncio
async def test_successful_third_model_blocks_fourth_and_later():
    pm = ProviderManager()
    probe = _Probe()
    p = _ModelProvider(
        "p",
        [
            _fail(http_status=404, failure_type="model_not_found"),
            _fail(http_status=404, failure_type="model_not_found"),
            _ok(),
        ],
        model="m1",
        probe=probe,
    )
    pm.register_provider(p)
    pm.switch_provider("p")
    pm.set_model_candidates("p", ["m2", "m3", "m4", "m5"])

    await pm.chat([{"role": "user", "content": "hi"}])

    # The THIRD candidate serves the request; the fourth and later never run.
    assert [model for _, model in probe.calls] == ["m1", "m2", "m3"]
    assert p.calls == 3


@pytest.mark.asyncio
async def test_candidate_requests_never_overlap():
    pm = ProviderManager()
    probe = _Probe()
    first = _ModelProvider(
        "first",
        [_fail(http_status=404, failure_type="model_not_found") for _ in range(3)],
        model="f0",
        probe=probe,
    )
    second = _ModelProvider("second", [_ok()], model="s0", probe=probe)
    pm.register_provider(first)
    pm.register_provider(second)
    pm.switch_provider("first")
    pm._fallback_chain = ["second"]
    pm.set_model_candidates("first", ["f1", "f2"])

    response = await pm.chat([{"role": "user", "content": "hi"}])

    assert response.success is True
    assert probe.max_active == 1, "production fallback must never fan out"
    assert len(probe.calls) == 4


# ── Model-level vs provider-level health ──


@pytest.mark.asyncio
async def test_model_timeout_does_not_disable_sibling_models():
    pm = ProviderManager()
    probe = _Probe()
    p = _ModelProvider(
        "p",
        [
            _fail("timeout", failure_type="timeout"),
            _fail("timeout", failure_type="timeout"),
            _ok(),
        ],
        model="slow-model",
        probe=probe,
    )
    pm.register_provider(p)
    pm.switch_provider("p")
    pm.set_model_candidates("p", ["fast-model"])

    response = await pm.chat([{"role": "user", "content": "hi"}])

    assert response.success is True
    assert [model for _, model in probe.calls] == ["slow-model", "slow-model", "fast-model"]
    assert pm._health.state("p") == ProviderHealthState.HEALTHY
    assert pm._health.model_cooldown_remaining("p", "slow-model") > 0
    assert pm._health.is_model_available("p", "fast-model") is True


@pytest.mark.asyncio
async def test_model_not_found_marks_only_that_pair():
    pm = ProviderManager()
    probe = _Probe()
    p = _ModelProvider(
        "p",
        [
            _fail(http_status=404, failure_type="model_not_found"),
            _ok(),
        ],
        model="dead-model",
        probe=probe,
    )
    pm.register_provider(p)
    pm.switch_provider("p")
    pm.set_model_candidates("p", ["live-model", "other-model"])

    await pm.chat([{"role": "user", "content": "hi"}])

    assert ("p", "dead-model") in pm._unavailable_models
    assert ("p", "live-model") not in pm._unavailable_models
    assert pm._health.state("p") == ProviderHealthState.HEALTHY

    # The next request never asks the dead pair again.
    probe.calls.clear()
    await pm.chat([{"role": "user", "content": "hi"}])
    assert probe.calls[0] == ("p", "live-model")


@pytest.mark.asyncio
async def test_auth_failure_disables_whole_provider():
    pm = ProviderManager()
    probe = _Probe()
    dead = _ModelProvider(
        "dead",
        [_fail("unauthorized", http_status=401, failure_type="auth")],
        model="m1",
        probe=probe,
    )
    backup = _ModelProvider("backup", [_ok()], model="b1", probe=probe)
    pm.register_provider(dead)
    pm.register_provider(backup)
    pm.switch_provider("dead")
    pm._fallback_chain = ["backup"]
    pm.set_model_candidates("dead", ["m2", "m3"])

    response = await pm.chat([{"role": "user", "content": "hi"}])

    assert response.success is True
    assert pm._health.state("dead") == ProviderHealthState.DISABLED
    assert [call for call in probe.calls] == [("dead", "m1"), ("backup", "b1")]
    assert (response.metadata or {}).get("fallback_to") == "backup"


@pytest.mark.asyncio
async def test_provider_quota_rate_limit_cools_the_whole_provider():
    pm = ProviderManager()
    probe = _Probe()
    p = _ModelProvider(
        "p",
        [
            _fail(
                "429 quota",
                http_status=429,
                failure_type="rate_limited",
                retry_after=30,
                provider_error_message=(
                    "You exceeded your current quota, please check your plan and billing details."
                ),
            ),
            _ok(),
        ],
        model="m1",
        probe=probe,
    )
    pm.register_provider(p)
    pm.switch_provider("p")
    pm.set_model_candidates("p", ["m2", "m3"])

    response = await pm.chat([{"role": "user", "content": "hi"}])

    assert response.success is False
    assert pm._health.state("p") == ProviderHealthState.COOLING_DOWN
    assert [model for _, model in probe.calls] == ["m1"], (
        "a shared/account quota must not be fired at every sibling model"
    )


@pytest.mark.asyncio
async def test_model_scoped_rate_limit_keeps_siblings_eligible():
    pm = ProviderManager()
    probe = _Probe()
    p = _ModelProvider(
        "p",
        [
            _fail(
                "429 per model",
                http_status=429,
                failure_type="rate_limited",
                retry_after=30,
                rate_limit_scope="model",
            ),
            _ok(),
        ],
        model="m1",
        probe=probe,
    )
    pm.register_provider(p)
    pm.switch_provider("p")
    pm.set_model_candidates("p", ["m2"])

    response = await pm.chat([{"role": "user", "content": "hi"}])

    assert response.success is True
    assert [model for _, model in probe.calls] == ["m1", "m2"]
    assert pm._health.state("p") == ProviderHealthState.HEALTHY
    assert pm._health.model_cooldown_remaining("p", "m1") > 0


@pytest.mark.asyncio
async def test_ambiguous_rate_limit_is_handled_conservatively():
    pm = ProviderManager()
    probe = _Probe()
    p = _ModelProvider(
        "p",
        [
            _fail("429 opaque", http_status=429, failure_type="rate_limited", retry_after=30),
            _ok(),
        ],
        model="m1",
        probe=probe,
    )
    pm.register_provider(p)
    pm.switch_provider("p")
    pm.set_model_candidates("p", ["m2", "m3"])

    response = await pm.chat([{"role": "user", "content": "hi"}])

    assert response.success is False
    assert pm._health.state("p") == ProviderHealthState.COOLING_DOWN
    assert [model for _, model in probe.calls] == ["m1"]


def test_rate_limit_scope_only_follows_established_evidence():
    groq = {
        "provider_error_message": (
            "Rate limit reached for model `llama-3.3-70b-versatile` in organization "
            "`org_123` on requests per min (RPM): Limit 30, Used 30."
        )
    }
    assert classify_rate_limit_scope(groq, "llama-3.3-70b-versatile") == "model"

    quota = {"provider_error_code": "insufficient_quota"}
    assert classify_rate_limit_scope(quota, "gpt-4o") == "provider"

    gemini = {"provider_error_message": "You exceeded your current quota, please check your plan."}
    assert classify_rate_limit_scope(gemini, "gemini-2.0-flash") == "provider"

    assert classify_rate_limit_scope({}, "opaque-model") == "unknown"
    assert classify_rate_limit_scope({"rate_limit_scope": "model"}, "x") == "model"


# ── Cached / non-live results never arm production ──


@pytest.mark.asyncio
async def test_cached_available_cannot_activate_production_candidates(monkeypatch):
    pm = ProviderManager()
    results = [_available("p", f"m{i}", cached=True) for i in range(5)]
    _feed(monkeypatch, pm, results, [_ProviderStatus("p")])

    assert pm._model_candidates.get("p") is None

    probe = _Probe()
    p = _ModelProvider("p", [_ok()], model="primary", probe=probe)
    pm.register_provider(p)
    pm.switch_provider("p")

    await pm.chat([{"role": "user", "content": "hi"}])
    assert probe.calls == [("p", "primary")]


@pytest.mark.asyncio
async def test_only_available_results_enter_the_pool(monkeypatch):
    pm = ProviderManager()
    results = [
        _available("p", "good-1"),
        _available("p", "good-2"),
        {"provider": "p", "model": "untested", "status": "NOT_TESTED"},
        {"provider": "p", "model": "unconfigured", "status": "NOT_CONFIGURED"},
        {"provider": "p", "model": "skipped", "status": "NOT_ATTEMPTED"},
        {"provider": "p", "model": "throttled", "status": "RATE_LIMITED"},
        {"provider": "p", "model": "forbidden", "status": "AUTH_ERROR"},
        {"provider": "p", "model": "gone", "status": "INVALID_MODEL"},
        {"provider": "p", "model": "cached-good", "status": "AVAILABLE", "cached": True},
    ]
    _feed(monkeypatch, pm, results, [_ProviderStatus("p")])

    pool = pm._model_candidates["p"]
    assert pool == ["good-1", "good-2"]


@pytest.mark.asyncio
async def test_discovery_only_models_never_enter_the_pool(monkeypatch):
    """A discovered but never-tested model is not a production candidate."""
    pm = ProviderManager()
    _feed(monkeypatch, pm, [], [_ProviderStatus("p")])

    assert pm._model_candidates.get("p") is None
    probe = _Probe()
    p = _ModelProvider("p", [_ok()], model="primary", probe=probe)
    pm.register_provider(p)
    pm.switch_provider("p")

    await pm.chat([{"role": "user", "content": "hi"}])
    assert probe.calls == [("p", "primary")]


# ── Preserved behaviour ──


@pytest.mark.asyncio
async def test_bounded_retry_and_timeout_are_preserved():
    assert manager_module._MAX_RETRIES == 1
    assert manager_module._PROVIDER_RPC_TIMEOUT == 30.0

    pm = ProviderManager()
    probe = _Probe()
    p = _ModelProvider(
        "p",
        [
            _fail("timeout", failure_type="timeout"),
            _fail("timeout", failure_type="timeout"),
            _ok(),
        ],
        model="m1",
        probe=probe,
    )
    pm.register_provider(p)
    pm.switch_provider("p")
    pm.set_model_candidates("p", ["m2"])

    response = await pm.chat([{"role": "user", "content": "hi"}])

    assert response.success is True
    assert [model for _, model in probe.calls] == ["m1", "m1", "m2"], (
        "exactly ONE immediate retry per candidate, never a loop"
    )


@pytest.mark.asyncio
async def test_independent_provider_fallback_still_works():
    pm = ProviderManager()
    probe = _Probe()
    first = _ModelProvider(
        "first",
        [_fail(http_status=404, failure_type="model_not_found") for _ in range(2)],
        model="f1",
        probe=probe,
    )
    second = _ModelProvider("second", [_ok()], model="s1", probe=probe)
    pm.register_provider(first)
    pm.register_provider(second)
    pm.switch_provider("first")
    pm._fallback_chain = ["second"]
    pm.set_model_candidates("first", ["f2"])

    response = await pm.chat([{"role": "user", "content": "hi"}])

    assert response.success is True
    assert (response.metadata or {}).get("fallback") is True
    assert (response.metadata or {}).get("fallback_to") == "second"
    assert [call for call in probe.calls] == [("first", "f1"), ("first", "f2"), ("second", "s1")]


@pytest.mark.asyncio
async def test_structured_output_and_tool_calls_survive_fallback():
    tool_call = {"id": "c1", "name": "save", "arguments": {"x": 1}}
    pm = ProviderManager()
    probe = _Probe()
    p = _ModelProvider(
        "p",
        [
            ProviderResponse(text="not json", provider_name="p", success=True),
            ProviderResponse(
                text="", provider_name="p", success=True, tool_calls=[tool_call],
            ),
        ],
        model="m1",
        probe=probe,
    )
    pm.register_provider(p)
    pm.switch_provider("p")
    pm.set_model_candidates("p", ["m2"])

    response = await pm.chat(
        [{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "save"}}],
        response_format={"type": "json_object"},
        output_validator=lambda r: bool(r.tool_calls) or r.text.strip().startswith("{"),
    )

    assert response.success is True
    assert response.tool_calls == [tool_call]
    assert [model for _, model in probe.calls] == ["m1", "m2"]


@pytest.mark.asyncio
async def test_exhausting_every_candidate_returns_honest_failure():
    pm = ProviderManager()
    probe = _Probe()
    p = _ModelProvider(
        "p",
        [_fail(http_status=404, failure_type="model_not_found") for _ in range(3)],
        model="m1",
        probe=probe,
    )
    pm.register_provider(p)
    pm.switch_provider("p")
    pm.set_model_candidates("p", ["m2", "m3"])

    response = await pm.chat([{"role": "user", "content": "hi"}])

    assert response.success is False
    assert response.provider_name == ""
    meta = response.metadata or {}
    assert meta.get("fallback_exhausted") is True
    assert len(meta.get("errors") or []) == 3
    assert "All AI providers failed" in response.text


# ── Actual model identity across real timeout/exception failures ──


class _RaisingProvider(_ModelProvider):
    """Provider whose ``chat()`` RAISES for named models, and can script others.

    Unlike a scripted ``ProviderResponse``, this exercises the manager's own
    timeout/exception handlers, which is where the attempted model used to be
    lost.
    """

    def __init__(
        self,
        name: str,
        faults: dict[str, list[BaseException]] | None = None,
        responses: dict[str, list[ProviderResponse]] | None = None,
        model: str = "",
        probe: _Probe | None = None,
    ) -> None:
        super().__init__(name, [], model=model, probe=probe)
        self._faults = {mid: list(queue) for mid, queue in (faults or {}).items()}
        self._by_model = {mid: list(queue) for mid, queue in (responses or {}).items()}

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> ProviderResponse:
        model = str(kwargs.get("model") or self._config.default_model or "")
        self.calls += 1
        self._probe.enter(self._name, model, messages)
        try:
            await asyncio.sleep(0)
            scripted = self._by_model.get(model)
            if scripted:
                step = scripted.pop(0)
                if isinstance(step, BaseException):
                    raise step
                return step
            faults = self._faults.get(model)
            if faults:
                raise faults.pop(0)
            return ProviderResponse(text="ok", provider_name=self._name, success=True)
        finally:
            self._probe.exit()


def _health_neutral_failure() -> ProviderResponse:
    """A 400 'request' failure: advances the router without touching health."""
    return _fail("bad request for this model", http_status=400, failure_type="request")


@pytest.mark.asyncio
async def test_call_once_preserves_requested_model_on_timeout_and_exception():
    """The real failure handlers keep the attempted model in metadata."""
    pm = ProviderManager()
    p = _RaisingProvider(
        "p",
        {"model-B": [asyncio.TimeoutError("slow"), RuntimeError("boom")]},
        model="model-A",
    )
    pm.register_provider(p)

    timed_out = await pm._call_once(p, [{"role": "user", "content": "x"}], {}, "model-B")
    assert timed_out.success is False
    assert (timed_out.metadata or {})["failure_type"] == "timeout"
    assert (timed_out.metadata or {})["requested_model"] == "model-B"

    crashed = await pm._call_once(p, [{"role": "user", "content": "x"}], {}, "model-B")
    assert crashed.success is False
    assert (crashed.metadata or {})["failure_type"] == "network"
    assert (crashed.metadata or {})["requested_model"] == "model-B"

    # A configured-default call is not mislabelled with an override model.
    defaulted = await pm._call_once(p, [{"role": "user", "content": "x"}], {}, "")
    assert defaulted.success is True
    assert "requested_model" not in (defaulted.metadata or {})


@pytest.mark.asyncio
async def test_backup_model_real_timeout_cools_only_that_model():
    pm = ProviderManager()
    probe = _Probe()
    p = _RaisingProvider(
        "p",
        {"model-B": [asyncio.TimeoutError(), asyncio.TimeoutError()]},
        responses={"model-A": [_health_neutral_failure()]},
        model="model-A",
        probe=probe,
    )
    pm.register_provider(p)
    pm.switch_provider("p")
    pm.set_model_candidates("p", ["model-B"])

    response = await pm.chat([{"role": "user", "content": "hi"}])

    assert response.success is False
    assert [model for _, model in probe.calls] == ["model-A", "model-B", "model-B"]
    assert pm._health.model_cooldown_remaining("p", "model-B") > 0
    assert pm._health.model_cooldown_remaining("p", "model-A") == 0
    assert pm._health.is_model_available("p", "model-A") is True
    assert pm._health.state("p") == ProviderHealthState.HEALTHY
    assert ("p", "model-A") not in pm._unavailable_models


@pytest.mark.asyncio
async def test_backup_model_real_exception_cools_only_that_model():
    pm = ProviderManager()
    probe = _Probe()
    p = _RaisingProvider(
        "p",
        {"model-B": [RuntimeError("socket exploded"), RuntimeError("again")]},
        responses={"model-A": [_health_neutral_failure()]},
        model="model-A",
        probe=probe,
    )
    pm.register_provider(p)
    pm.switch_provider("p")
    pm.set_model_candidates("p", ["model-B"])

    await pm.chat([{"role": "user", "content": "hi"}])

    assert pm._health.model_cooldown_remaining("p", "model-B") > 0
    assert pm._health.model_cooldown_remaining("p", "model-A") == 0
    assert pm._health.state("p") == ProviderHealthState.HEALTHY


@pytest.mark.asyncio
async def test_retry_failure_stays_attributed_to_the_backup_model():
    """Initial attempt AND its bounded retry both fail on model-B."""
    pm = ProviderManager()
    probe = _Probe()
    p = _RaisingProvider(
        "p",
        {"model-B": [asyncio.TimeoutError(), asyncio.TimeoutError()]},
        responses={"model-A": [_health_neutral_failure()]},
        model="model-A",
        probe=probe,
    )
    pm.register_provider(p)
    pm.switch_provider("p")
    pm.set_model_candidates("p", ["model-B"])

    await pm.chat([{"role": "user", "content": "hi"}])

    assert [model for _, model in probe.calls] == ["model-A", "model-B", "model-B"]
    assert pm._health.model_cooldown_remaining("p", "model-B") > 0
    # Exactly one model owns the cooldown.
    assert pm._health.is_model_available("p", "model-A") is True


@pytest.mark.asyncio
async def test_rate_limit_retry_failure_attributes_the_attempted_model():
    pm = ProviderManager()
    probe = _Probe()
    p = _RaisingProvider(
        "p",
        responses={
            "model-A": [_health_neutral_failure()],
            "model-B": [
                _fail(
                    "429 per model",
                    http_status=429,
                    failure_type="rate_limited",
                    retry_after=0.01,
                    rate_limit_scope="model",
                ),
                asyncio.TimeoutError(),
            ],
        },
        model="model-A",
        probe=probe,
    )
    pm.register_provider(p)
    pm.switch_provider("p")
    pm.set_model_candidates("p", ["model-B"])

    await pm.chat([{"role": "user", "content": "hi"}])

    # The short Retry-After retry ran on model-B and raised a real timeout.
    assert [model for _, model in probe.calls] == ["model-A", "model-B", "model-B"]
    assert pm._health.model_cooldown_remaining("p", "model-B") > 0
    assert pm._health.model_cooldown_remaining("p", "model-A") == 0
    # A model-scoped limit never takes the provider down.
    assert pm._health.state("p") == ProviderHealthState.HEALTHY


@pytest.mark.asyncio
async def test_configured_primary_failure_still_names_the_primary():
    pm = ProviderManager()
    probe = _Probe()
    p = _RaisingProvider(
        "p",
        {"model-A": [asyncio.TimeoutError(), asyncio.TimeoutError()]},
        model="model-A",
        probe=probe,
    )
    pm.register_provider(p)
    pm.switch_provider("p")
    pm.set_model_candidates("p", ["model-B"])

    response = await pm.chat([{"role": "user", "content": "hi"}])

    assert response.success is True
    assert [model for _, model in probe.calls] == ["model-A", "model-A", "model-B"]
    assert pm._health.model_cooldown_remaining("p", "model-A") > 0
    assert pm._health.is_model_available("p", "model-B") is True


@pytest.mark.asyncio
async def test_sibling_model_stays_eligible_after_backup_cooldown():
    pm = ProviderManager()
    probe = _Probe()
    p = _RaisingProvider(
        "p",
        {"model-B": [asyncio.TimeoutError(), asyncio.TimeoutError()]},
        responses={"model-A": [_health_neutral_failure()]},
        model="model-A",
        probe=probe,
    )
    pm.register_provider(p)
    pm.switch_provider("p")
    pm.set_model_candidates("p", ["model-B"])

    await pm.chat([{"role": "user", "content": "hi"}])
    assert pm._health.model_cooldown_remaining("p", "model-B") > 0

    # The next request must be served by the healthy primary, never by a
    # globally cooled provider and never by the cooling pair.
    probe.calls.clear()
    second = await pm.chat([{"role": "user", "content": "hi again"}])

    assert second.success is True
    assert [model for _, model in probe.calls] == ["model-A"]
    assert pm._health.state("p") == ProviderHealthState.HEALTHY
