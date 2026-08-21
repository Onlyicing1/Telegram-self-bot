"""
TASK 34 — AI model capacity UI, OpenRouter free models, two-column selector,
Details integrity, and telemetry exactly-once regression coverage.

  1. remaining-context math never invents a limit.
  2. Free-model detection is pricing-metadata-driven only (never the name).
  3. Free models pin first with deterministic ordering and no duplication.
  4. The two-column selector's index+hash callbacks stay under Telegram's
     64-byte limit and resolve back to the exact rendered model; a stale
     hash re-renders instead of mis-selecting.
  5. Details renders ONLY from the latest AIExecutionRecord — success,
     failed/rate-limited/fallback, estimated vs actual vs unavailable — and
     never falls back to another request's identity or usage.
  6. Every execution is recorded exactly once: provider path, deterministic
     fast path, and early-stage engine failures all land one record each.
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest

from backend.ai.engine.result import EngineResult
from backend.ai.engine.telemetry import (
    format_tokens_exact,
    remaining_context,
    telemetry,
)
from backend.ai.model_discovery import (
    ModelInfo,
    _is_free_pricing,
    clear_cache,
    get_model_context_length,
    order_models_for_selector,
)


@pytest.fixture(autouse=True)
def _reset_telemetry():
    telemetry.reset_for_tests()
    yield
    telemetry.reset_for_tests()


@pytest.fixture(autouse=True)
def _clean_model_cache():
    clear_cache()
    yield
    clear_cache()


# ── 1. Remaining-context math ──


def test_remaining_context_computes_capacity():
    assert remaining_context(8412, 32768) == 24356
    assert remaining_context(1, 1000) == 999
    assert remaining_context(32768, 32768) == 0
    # Over-limit clamps at zero instead of going negative.
    assert remaining_context(40000, 32768) == 0


def test_remaining_context_unknown_limit_is_none():
    # Unknown limit → None: callers must show "unavailable", never a number.
    assert remaining_context(8412, 0) is None
    # Unknown usage → None as well.
    assert remaining_context(0, 32768) is None
    assert remaining_context(0, 0) is None


# ── 2. Free-model detection (pricing metadata ONLY) ──


def test_free_detection_requires_zero_prompt_and_completion_pricing():
    assert _is_free_pricing({"pricing": {"prompt": "0", "completion": "0"}}) is True
    assert _is_free_pricing({"pricing": {"prompt": 0, "completion": 0}}) is True


def test_free_detection_rejects_nonzero_or_missing_pricing():
    assert _is_free_pricing({"pricing": {"prompt": "0", "completion": "0.000001"}}) is False
    assert _is_free_pricing({"pricing": {"prompt": "0.001", "completion": "0"}}) is False
    assert _is_free_pricing({}) is False
    assert _is_free_pricing({"pricing": "free"}) is False
    # A name containing "free" without pricing metadata is NEVER free.
    assert _is_free_pricing({"id": "some-model:free"}) is False


def test_model_info_default_is_not_free():
    m = ModelInfo(id="x:free", name="x:free")
    assert m.is_free is False


# ── 3. Free pinning + deterministic ordering ──


def test_order_models_pins_free_first_and_stays_deterministic():
    models = [
        ModelInfo(id="zeta", name="zeta"),
        ModelInfo(id="beta-free", name="beta", is_free=True),
        ModelInfo(id="alpha", name="alpha"),
        ModelInfo(id="alpha-free", name="alpha", is_free=True),
        ModelInfo(id="mid", name="mid"),
    ]
    ordered = order_models_for_selector(models)
    assert [m.id for m in ordered] == ["alpha-free", "beta-free", "alpha", "mid", "zeta"]
    # Deterministic across calls, original list untouched, no duplication.
    assert order_models_for_selector(ordered) == ordered
    assert len(ordered) == len(models)
    assert len({m.id for m in ordered}) == len(models)


def test_order_models_without_metadata_is_plain_alphabetical():
    models = [
        ModelInfo(id="b", name="b"),
        ModelInfo(id="a-free", name="a"),  # name says free but metadata doesn't
        ModelInfo(id="c", name="c"),
    ]
    assert [m.id for m in order_models_for_selector(models)] == ["a-free", "b", "c"]


def test_get_model_context_length_from_cache_only():
    clear_cache()
    assert get_model_context_length("prov", "m1") == 0
    from backend.ai import model_discovery as md

    md._model_cache["prov"] = {
        "models": [
            ModelInfo(id="m1", name="m1", provider="prov", context_length=32768),
            ModelInfo(id="m2", name="m2", provider="prov", context_length=0),
        ],
        "timestamp": time.time(),
    }
    assert get_model_context_length("prov", "m1") == 32768
    assert get_model_context_length("prov", "m2") == 0  # unknown stays unknown
    assert get_model_context_length("other", "m1") == 0


# ── 4. Two-column selector ──


def _flatten_buttons(buttons):
    rows = []
    for row in buttons:
        if isinstance(row, list):
            cells = []
            for btn in row:
                data = getattr(btn, "data", None)
                text = getattr(btn, "text", None) or ""
                if isinstance(data, bytes):
                    data = data.decode("utf-8", errors="replace")
                cells.append((text, data))
            rows.append(cells)
    return rows


_MIXED_MODELS = [
    ModelInfo(id="paid/z-9", name="z-9", provider="openrouter", context_length=128000),
    ModelInfo(id="free/b-2", name="b-2", provider="openrouter", context_length=32000, is_free=True),
    ModelInfo(id="paid/a-1", name="a-1", provider="openrouter", context_length=8192),
    ModelInfo(id="free/c-3-long-name-that-goes-on-and-on", name="c-3-long-name-that-goes-on-and-on",
              provider="openrouter", is_free=True),
]


@pytest.mark.asyncio
async def test_two_column_selector_renders_pinned_grid_within_callback_limit():
    from backend.bot.handlers import ai as ai_module

    config = {"provider": "openrouter", "model": "paid/a-1"}
    with patch.object(ai_module, "_get_saved_config", AsyncMock(return_value=config)), \
         patch("backend.ai.model_discovery.fetch_models", AsyncMock(return_value=_MIXED_MODELS)), \
         patch("backend.ai.model_discovery.get_api_key_for_provider", return_value="k"), \
         patch("backend.ai.model_discovery.get_base_url_for_provider", return_value="https://x"):
        title, body, buttons = await ai_module._ai_model_panel_handler(None, "")

    assert title == "🤖 Model"
    assert "Free" in body
    rows = _flatten_buttons(buttons)
    grid_rows = [
        r for r in rows
        if r and all(str(d).startswith("action:ai_model_pick_idx:") for _, d in r)
    ]
    # Two columns per row; odd counts leave a single-button final row.
    assert all(len(r) <= 2 for r in grid_rows)
    flat = [cell for row in grid_rows for cell in row]
    assert len(flat) == len(_MIXED_MODELS)
    # Telegram's hard callback-data ceiling is respected for EVERY button,
    # including the long-id model that would truncate today.
    for _, data in flat:
        assert len(data.encode()) <= 64

    # Index+hash callbacks resolve back to the exact rendered model, pinned
    # free models first.
    from backend.ai.model_discovery import order_models_for_selector

    expected = {m.id for m in order_models_for_selector(_MIXED_MODELS)}
    resolved = set()
    for _, data in flat:
        _, _, page, idx, h = str(data).split(":")
        candidate = order_models_for_selector(_MIXED_MODELS)[int(idx)]
        assert ai_module._model_callback_hash(candidate.id) == h
        resolved.add(candidate.id)
    assert resolved == expected
    # Current selection keeps its mark.
    marked = [t for t, _ in flat]
    assert any(t.startswith("✓ ") for t in marked)
    assert not any("✅" in t for t in marked)


@pytest.mark.asyncio
async def test_pick_idx_action_selects_the_hashed_model():
    from backend.bot.handlers import ai as ai_module

    ordered = order_models_for_selector(_MIXED_MODELS)
    target = next(m for m in ordered if m.id == "free/b-2")
    extra = f"0:{ordered.index(target)}:{ai_module._model_callback_hash(target.id)}"

    saved = {}
    config = {"provider": "openrouter", "model": ""}
    with patch.object(ai_module, "_get_saved_config", AsyncMock(return_value=config)), \
         patch.object(ai_module, "_save_config", AsyncMock(side_effect=lambda o, c: saved.update(c))), \
         patch.object(ai_module, "_apply_runtime_selection") as apply_sel, \
         patch.object(ai_module, "_get_owner_id", AsyncMock(return_value=1)), \
         patch("backend.ai.model_discovery.fetch_models", AsyncMock(return_value=_MIXED_MODELS)), \
         patch("backend.ai.model_discovery.get_api_key_for_provider", return_value="k"), \
         patch("backend.ai.model_discovery.get_base_url_for_provider", return_value="https://x"):
        title, body, buttons = await ai_module._ai_model_pick_idx_action(None, extra, 0)

    assert saved.get("model") == "free/b-2"
    apply_sel.assert_called_once_with("openrouter", "free/b-2")
    assert title == "AI"


@pytest.mark.asyncio
async def test_pick_idx_action_rerenders_on_stale_hash():
    from backend.bot.handlers import ai as ai_module

    config = {"provider": "openrouter", "model": ""}
    with patch.object(ai_module, "_get_saved_config", AsyncMock(return_value=config)), \
         patch.object(ai_module, "_save_config", AsyncMock()), \
         patch("backend.ai.model_discovery.fetch_models", AsyncMock(return_value=_MIXED_MODELS)), \
         patch("backend.ai.model_discovery.get_api_key_for_provider", return_value="k"), \
         patch("backend.ai.model_discovery.get_base_url_for_provider", return_value="https://x"):
        title, body, buttons = await ai_module._ai_model_pick_idx_action(None, "0:1:deadbeef", 0)

    # A stale index/hash must re-render the panel, never select blindly.
    assert title == "🤖 Model"
    assert body  # panel content present


# ── 5. Details renders ONLY from the record ──


def _record(**overrides):
    from backend.ai.engine.telemetry import AIExecutionRecord

    base = dict(
        timestamp="2026-08-21T16:41:33+00:00",
        provider="gemini",
        model="gemini-2.5-flash",
        status="success",
        input_tokens=2184,
        output_tokens=487,
        total_tokens=2671,
        token_source="actual",
        context_tokens=8412,
        max_context=32768,
        latency=2.734,
        retry_count=0,
        fallback_used=False,
        tool_call_count=1,
    )
    base.update(overrides)
    record = AIExecutionRecord(**base)
    telemetry._records.append(record)
    return record


@pytest.mark.asyncio
async def test_details_success_shows_actual_usage_and_remaining():
    from backend.bot.handlers import ai as ai_module

    _record(max_context=32768)
    with patch.object(ai_module, "_resolve_context_limit", AsyncMock(return_value=32768)):
        title, body, buttons = await ai_module._ai_details_panel_handler(None, "")

    assert title == "AI · Details"
    assert "gemini-2.5-flash" in body
    assert "Google" in body
    assert "Ready" in body
    assert f"{format_tokens_exact(2184)} in · {format_tokens_exact(487)} out" in body
    assert f"{format_tokens_exact(8412)} / {format_tokens_exact(32768)}" in body
    assert f"{format_tokens_exact(24356)} left" in body
    assert "≈ est." not in body
    assert "Unavailable" not in body


@pytest.mark.asyncio
async def test_details_failed_rate_limited_fallback_shows_unavailable_tokens():
    from backend.bot.handlers import ai as ai_module

    _record(
        provider="dummy", model="", status="failed",
        input_tokens=0, output_tokens=0, total_tokens=0,
        token_source="unavailable", context_tokens=0, max_context=0,
        latency=7.767, fallback_used=True, error_reason="Rate limited",
    )
    with patch.object(ai_module, "_resolve_context_limit", AsyncMock(return_value=0)):
        title, body, buttons = await ai_module._ai_details_panel_handler(None, "")

    assert "Failed — Rate limited" in body
    assert "Fallback" in body
    assert "Yes" in body
    assert "Unavailable" in body
    # No fabricated usage anywhere; nothing consumed → context unavailable.
    assert "≈ est." not in body


@pytest.mark.asyncio
async def test_details_never_uses_config_identity_for_a_blank_record():
    from backend.bot.handlers import ai as ai_module

    _record(model="", provider="")
    other_config = {"model": "some-other-request-model", "provider": "cohere"}
    with patch.object(ai_module, "_get_saved_config", AsyncMock(return_value=other_config)), \
         patch.object(ai_module, "_resolve_context_limit", AsyncMock(return_value=0)):
        title, body, buttons = await ai_module._ai_details_panel_handler(None, "")

    # Stale-data prevention: an execution with no identity shows "—",
    # never the persisted config's model from a different request.
    lines = [l.strip() for l in body.splitlines() if l.strip()]
    model_row = next(l for l in lines if l.startswith("Model"))
    assert model_row.endswith("—")
    assert "some-other-request-model" not in body
    assert "deterministic" not in body


@pytest.mark.asyncio
async def test_details_marks_estimated_usage_and_unknown_limit():
    from backend.bot.handlers import ai as ai_module

    _record(token_source="estimated", max_context=0)
    with patch.object(ai_module, "_resolve_context_limit", AsyncMock(return_value=0)):
        title, body, buttons = await ai_module._ai_details_panel_handler(None, "")

    assert "≈ est." in body
    assert "limit unknown" in body


@pytest.mark.asyncio
async def test_details_empty_state_without_records():
    from backend.bot.handlers import ai as ai_module

    title, body, buttons = await ai_module._ai_details_panel_handler(None, "")
    assert "No AI requests yet" in body


# ── 6. Telemetry integrity: recorded EXACTLY once, failures included ──


class _ScriptedProvider:
    PROVIDER_NAME = "scripted34"
    PROVIDER_VERSION = "1.0.0"

    def __init__(self, *, success=True, response_model="", failure_text=""):
        from backend.ai.providers.base.config import ProviderConfig
        from backend.ai.providers.base.contract import ProviderResponse

        self._response_cls = ProviderResponse
        self.config = ProviderConfig(
            provider_name=self.PROVIDER_NAME, enabled=True,
            default_model="config-default-model",
        )
        self._success = success
        self._response_model = response_model
        self._failure_text = failure_text

    @property
    def name(self):
        return self.PROVIDER_NAME

    def initialize(self):
        pass

    def shutdown(self):
        pass

    async def chat(self, messages, **kwargs):
        if self._success:
            meta = {"finish_reason": "stop"}
            if self._response_model:
                meta["model"] = self._response_model
            return self._response_cls(
                text="ok", provider_name=self.name, success=True,
                usage={"prompt_tokens": 40, "completion_tokens": 8, "total_tokens": 48},
                metadata=meta,
            )
        return self._response_cls(
            text=self._failure_text, provider_name=self.name, success=False,
            usage={}, metadata={"failure_type": "rate_limited"},
        )

    def count_tokens(self, text: str) -> int:
        return len(text) // 4

    def health(self) -> dict:
        return {"healthy": True, "provider": self.name}


def _engine_with(provider):
    from backend.ai.engine.engine import Engine
    from backend.ai.providers.manager.manager import ProviderManager

    pm = ProviderManager()
    pm.register_provider(provider)
    pm.switch_provider(provider.PROVIDER_NAME)
    return Engine(providers=pm), pm


async def _run(engine):
    from backend.ai.session.request import AIRequest

    return await engine.execute(
        AIRequest(session_id="t34", user_message="hi there", owner_id=42,
                  chat_id=-100, message_id=1)
    )


@pytest.mark.asyncio
async def test_provider_success_records_exactly_once_with_serving_model():
    engine, _pm = _engine_with(
        _ScriptedProvider(success=True, response_model="served-real-model")
    )
    result = await _run(engine)

    records = telemetry.recent(50)
    assert len(records) == 1
    assert result.success is True
    # The model that ACTUALLY served wins over the active config default.
    assert records[0].model == "served-real-model"
    assert records[0].token_source == "actual"
    assert records[0].total_tokens == 48


@pytest.mark.asyncio
async def test_failed_rate_limited_request_records_honest_unavailable_tokens():
    engine, pm = _engine_with(
        _ScriptedProvider(success=False, failure_text="HTTP 429 rate limited")
    )
    result = await _run(engine)

    assert result.success is False
    records = telemetry.recent(50)
    assert len(records) == 1
    rec = records[0]
    assert rec.status == "failed"
    # Emergency fallback answered the terminal failure.
    assert rec.fallback_used is True
    assert rec.provider == "dummy"
    assert rec.error_reason == "Rate limited"
    # A failed request consumed nothing — no estimated tokens dressed up
    # as usage, no stale context figure.
    assert rec.token_source == "unavailable"
    assert rec.total_tokens == 0
    assert rec.input_tokens == 0
    assert rec.context_tokens == 0


def test_fast_path_records_exactly_once_per_execution():
    from backend.ai.session.request import AIRequest

    class _NoopProvider(_ScriptedProvider):
        pass

    engine, _pm = _engine_with(_ScriptedProvider())
    request = AIRequest(session_id="t34f", user_message="save this", owner_id=42,
                        chat_id=-100, message_id=1)
    start = time.perf_counter()
    before = len(telemetry.recent(50))
    engine._dispatcher._build_fast_path_result(
        request, "rid-1", start, {"stages": []},
        success=True, text="Action completed.", action="save",
        kind="executable", target="replied_message",
    )
    records = telemetry.recent(50)
    assert len(records) == before + 1
    rec = records[-1]
    assert rec.status == "success"
    assert rec.token_source == "unavailable"
    assert rec.provider == "local"


def test_engine_stage_failure_is_recorded_safely():
    engine, _pm = _engine_with(_ScriptedProvider())
    before = len(telemetry.recent(50))
    engine._dispatcher._fail(
        RuntimeError("boom"), "prompt_builder", time.perf_counter(), [], {"stages": []}
    )
    records = telemetry.recent(50)
    assert len(records) == before + 1
    rec = records[-1]
    assert rec.status == "failed"
    assert rec.error_reason == "System error"
    assert rec.token_source == "unavailable"
