"""Test Modules UI — current runtime model, adaptive 2/3-column grid, paging.

Covers the requirements that the results view (a) shows the model the
runtime would ACTUALLY use, (b) lays the usable-model buttons out in an
adaptive grid, and (c) pages without dropping or duplicating a model.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.bot.handlers import ai as ai_module
from backend.bot.handlers import ai_test_progress

_PICK = "action:ai_pick_model:"
_PAGE = "action:ai_test_page:"


def _raw(btn) -> str:
    data = getattr(btn, "data", b"")
    return data.decode() if isinstance(data, bytes) else str(data)


def _flatten(buttons) -> list[str]:
    return [_raw(btn) for row in buttons or [] for btn in row or []]


def _labels(buttons) -> list[str]:
    return [str(getattr(btn, "text", "")) for row in buttons or [] for btn in row or []]


def _model_rows(buttons) -> list[list[str]]:
    """Rows that consist of usable-model pick buttons only."""
    rows: list[list[str]] = []
    for row in buttons or []:
        datas = [_raw(btn) for btn in row or []]
        if datas and all(d.startswith(_PICK) for d in datas):
            rows.append(datas)
    return rows


def _payload(models: dict[str, list[str]], status: str = "AVAILABLE") -> dict:
    results = []
    for provider, ids in models.items():
        for i, model in enumerate(ids):
            results.append({
                "provider": provider,
                "display_name": provider.title(),
                "icon": "◇",
                "model": model,
                "status": status,
                "latency_s": round(0.1 * (i + 1), 2),
                "http_status": 200,
                "error": None,
            })
    return {
        "results": results,
        "summary": {
            "available": len(results), "failed": 0, "rate_limited": 0,
            "not_configured": 0, "invalid": 0, "insufficient_credits": 0,
        },
    }


def _fake_engine(provider: str, model: str, healthy: bool = True):
    class _Cfg:
        default_model = model

    class _Active:
        name = provider

        def health(self):
            return {"healthy": healthy}

    class _Mgr:
        def get_active(self):
            return _Active()

        def get_provider_config(self, name=None):
            return _Cfg()

    class _Engine:
        provider_manager = _Mgr()

    return _Engine()


# ── Requirement: show the CURRENT runtime model ──


def test_current_runtime_pair_is_rendered_from_the_provider_manager():
    """The 'Current:' line comes from the authoritative runtime state."""
    with patch.object(
        ai_module, "_get_engine",
        return_value=_fake_engine("groq", "llama-3.3-70b-versatile"),
    ):
        body, _ = ai_module._render_test_results(_payload({"groq": ["m1"]}))

    assert "Current: Groq · llama-3.3-70b-versatile" in body


def test_current_line_ignores_a_divergent_persisted_config():
    """A persisted pair the runtime did not adopt is never shown as current."""
    runtime = _fake_engine("openrouter", "openrouter/auto")
    with patch.object(ai_module, "_get_engine", return_value=runtime), \
         patch.object(ai_module, "_get_saved_config", AsyncMock(
             return_value={"provider": "groq", "model": "llama-3.3-70b-versatile"})):
        body, _ = ai_module._render_test_results(_payload({"groq": ["m1"]}))

    assert "Current: OpenRouter · openrouter/auto" in body
    assert "llama-3.3-70b-versatile" not in body


def test_current_line_is_honest_when_the_runtime_reports_nothing():
    with patch.object(ai_module, "_runtime_pair", return_value=("", "")):
        body, _ = ai_module._render_test_results(_payload({"groq": ["m1"]}))
    assert "Current: unavailable" in body


@pytest.mark.asyncio
async def test_launch_view_shows_the_current_runtime_model():
    async def _noop(*_args, **_kwargs):
        return None

    ai_module._test_running = False
    try:
        with patch.object(ai_module, "_get_owner_id", AsyncMock(return_value=1)), \
             patch.object(
                 ai_module, "_runtime_pair",
                 return_value=("openrouter", "openrouter/auto"),
             ), \
             patch.object(ai_test_progress, "run_streaming_test", _noop):
            title, body, _ = await ai_module._ai_test_models_action(MagicMock(), "", 0)
            for _ in range(5):
                await asyncio.sleep(0)
    finally:
        ai_module._test_running = False

    assert title == "Test Modules"
    assert "Current: OpenRouter · openrouter/auto" in body


def test_progress_view_shows_the_current_runtime_model_first():
    text = ai_test_progress.render_progress_view(
        1,
        4,
        {"provider": "groq", "model": "m1", "status": "AVAILABLE"},
        "Current: Groq · llama-3.3-70b-versatile",
    )
    first, second = text.splitlines()[:2]
    assert first == "Current: Groq · llama-3.3-70b-versatile"
    # Five-segment progress bar preserved: 1/4 = 25% -> one filled segment.
    assert second.startswith("▰▱▱▱▱")
    assert len(second.split()[0]) == 5


# ── Requirement: active model marked in place ──


def test_active_runtime_model_is_marked_in_place_not_duplicated():
    payload = _payload({"groq": ["m1", "m2"]})
    body, buttons = ai_module._render_test_results(payload, current=("groq", "m2"))

    assert "◉ `m2`" in body
    assert "• `m1`" in body
    marks = [label for label in _labels(buttons) if label.startswith("◉ ")]
    assert len(marks) == 1
    assert any("m2" in label for label in marks)
    # Exactly one button per model — the mark is not a duplicate.
    datas = [d for d in _flatten(buttons) if d.startswith(_PICK)]
    assert len(datas) == 2
    assert len(set(datas)) == 2


def test_non_active_results_keep_the_plain_usable_marker():
    payload = _payload({"groq": ["m1"]})
    body, buttons = ai_module._render_test_results(payload, current=("openai", "gpt-4o"))
    assert "• `m1`" in body
    assert "◉" not in body
    assert all(not label.startswith("◉ ") for label in _labels(buttons))


# ── Requirement: adaptive 2/3-column grid ──


def test_normal_usable_set_uses_two_columns():
    payload = _payload({"groq": [f"m{i:02d}" for i in range(12)]})
    _, buttons = ai_module._render_test_results(payload, current=("groq", "m00"))

    widths = [len(row) for row in _model_rows(buttons)]
    assert widths == [2] * 6
    assert len([d for d in _flatten(buttons) if d.startswith(_PICK)]) == 12
    # A normal set fits one page — no pagination chrome.
    assert not any(d.startswith(_PAGE) for d in _flatten(buttons))


def test_large_usable_set_uses_three_columns():
    payload = _payload({"groq": [f"m{i:02d}" for i in range(20)]})
    _, buttons = ai_module._render_test_results(payload, current=("groq", "m00"))

    widths = [len(row) for row in _model_rows(buttons)]
    assert max(widths) == 3
    assert widths.count(3) == 6  # 18 models on page one: 6 rows of 3
    assert len([d for d in _flatten(buttons) if d.startswith(_PICK)]) == 18


def test_threshold_is_the_historical_single_view_capacity():
    assert ai_module._test_grid_columns(12) == 2
    assert ai_module._test_grid_columns(13) == 3
    assert ai_module._test_grid_page_size(2) == 12
    assert ai_module._test_grid_page_size(3) == 18


# ── Requirement: pagination preserves every model exactly once ──


@pytest.mark.asyncio
async def test_pagination_covers_every_model_exactly_once():
    models = [f"m{i:02d}" for i in range(20)]
    payload = _payload({"groq": models})
    ai_module._last_test_payload = payload

    collected: list[str] = []
    page = 0
    while True:
        _, _, buttons = await ai_module._ai_test_page_action(None, str(page), 0)
        for row in _model_rows(buttons):
            assert len(row) <= 3  # never a fourth column, on any page
            collected += [d.rsplit(":", 1)[-1] for d in row]
        if f"{_PAGE}{page + 1}" not in _flatten(buttons):
            break
        page += 1

    assert page == 1  # two pages in the 3-column layout
    assert sorted(collected) == sorted(models)
    assert len(collected) == len(set(collected)) == len(models)


@pytest.mark.asyncio
async def test_pager_is_pure_presentation_and_never_reruns_tests():
    payload = _payload({"groq": [f"m{i:02d}" for i in range(20)]})
    ai_module._last_test_payload = payload

    with patch(
        "backend.ai.model_tester.test_all_models_streaming", AsyncMock()
    ) as mock_run:
        _, _, buttons = await ai_module._ai_test_page_action(None, "1", 0)

    mock_run.assert_not_called()
    assert ai_module._last_test_payload is payload
    assert len([d for d in _flatten(buttons) if d.startswith(_PICK)]) == 2


@pytest.mark.asyncio
async def test_pager_clamps_an_out_of_range_page():
    payload = _payload({"groq": [f"m{i:02d}" for i in range(20)]})
    ai_module._last_test_payload = payload

    _, _, buttons = await ai_module._ai_test_page_action(None, "99", 0)
    body, _ = ai_module._render_test_results(payload, page=99)
    # Clamped to the last page instead of rendering nothing.
    assert len([d for d in _flatten(buttons) if d.startswith(_PICK)]) == 2
    assert "No usable chat models right now" not in body


# ── Requirement: stable, unique callback mapping ──


def test_model_callbacks_are_unique_and_deterministic():
    payload = _payload({"groq": ["a", "b", "c"], "openai": ["d"]})
    _, first = ai_module._render_test_results(payload, current=("groq", "a"))
    _, second = ai_module._render_test_results(payload, current=("groq", "a"))

    datas = [d for d in _flatten(first) if d.startswith(_PICK)]
    assert datas == [d for d in _flatten(second) if d.startswith(_PICK)]
    assert sorted(datas) == sorted([
        "action:ai_pick_model:groq:a",
        "action:ai_pick_model:groq:b",
        "action:ai_pick_model:groq:c",
        "action:ai_pick_model:openai:d",
    ])


def test_the_pager_action_is_registered():
    from backend.helper import get_action

    assert get_action("ai_test_page") is ai_module._test_modules_page_dispatch


# ── Requirement: preserve the existing UI safety rails ──


def test_grid_keeps_the_summary_and_existing_actions_without_emoji():
    payload = _payload({"groq": ["m1"]})
    body, buttons = ai_module._render_test_results(payload, current=("groq", "m1"))
    datas = _flatten(buttons)

    assert "✓ 1" in body
    assert "action:ai_test_models" in datas
    assert "action:ai_test_details" in datas
    assert "panel:ai_model" in datas
    assert any(d.startswith("panel:_nav:") for d in datas)
    forbidden = "🧪🟢✅⚠️🔄🤖🔍💳🚫🔵🟡🟠🔴⚪❓⚡🧠"
    assert not any(ch in body for ch in forbidden)
    assert not any(ch in label for label in _labels(buttons) for ch in forbidden)
