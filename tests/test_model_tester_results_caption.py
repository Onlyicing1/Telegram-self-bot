"""Model Tester results: bounded media caption + complete paginated access.

The Test Modules panel message can be a PHOTO (Appearance preview), so the
composed title+body is a Telegram media caption (1024 chars). A large snapshot
used to print every usable model in the body, and Telegram rejected the edit
with "caption is too long". These tests pin BOTH halves of the fix:

  - the body is a bounded summary (no model listing, no unbounded provider
    names), validated with the existing caption capability helper, and
  - every usable (provider, model) pair stays reachable exactly once through
    the adaptive, paginated model buttons, with its exact callback data.

The run itself is always stubbed at the tester boundary: these tests never
touch a provider, Supabase or Telegram.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.ai import test_snapshot as ts
from backend.bot.handlers import ai as ai_module
from backend.bot.handlers import ai_test_progress
from backend.db import client as db_client
from backend.helper import symbols as sym
from backend.services import appearance_service

_PICK = "action:ai_pick_model:"
_PAGE = "action:ai_test_page:"
_PROVIDERS = (
    ("groq", "Groq"),
    ("openrouter", "OpenRouter"),
    ("gemini", "Google Gemini"),
)
_LONG_MODEL = "vendor/" + "extremely-long-model-identifier-" * 2 + "v1"


def _raw(btn) -> str:
    data = getattr(btn, "data", b"")
    return data.decode() if isinstance(data, bytes) else str(data)


def _flatten(buttons) -> list[str]:
    return [_raw(btn) for row in buttons or [] for btn in row or []]


def _labels(buttons) -> list[str]:
    return [str(getattr(btn, "text", "")) for row in buttons or [] for btn in row or []]


def _payload(*, usable: int = 61, failing: int = 4) -> dict:
    """A realistic large run: 61 usable models across 3 providers."""
    results: list[dict] = []
    for i in range(usable):
        provider, display = _PROVIDERS[i % len(_PROVIDERS)]
        model = f"model-{i:02d}-chat-v1"
        results.append({
            "provider": provider,
            "display_name": display,
            "icon": "◇",
            "model": model,
            "status": "AVAILABLE",
            "error": None,
            "latency_s": round(0.05 * (i + 1), 2),
            "http_status": 200,
            "retry_after": None,
            "error_type": None,
            "provider_code": None,
            "is_free": i % 5 == 0,
            "cached": i % 7 == 0,
        })
    for i, status in enumerate(
        ["INVALID_MODEL", "RATE_LIMITED", "TIMEOUT", "AUTH_ERROR"][:failing]
    ):
        provider, display = _PROVIDERS[i % len(_PROVIDERS)]
        results.append({
            "provider": provider,
            "display_name": display,
            "icon": "◇",
            "model": f"failed-{i}-chat-v1",
            "status": status,
            "error": "provider said no",
            "latency_s": None,
            "http_status": 404,
            "retry_after": None,
            "error_type": "invalid_request_error",
            "provider_code": "model_not_found",
            "failure_origin": "provider",
            "operation": "chat.completions",
            "endpoint": "/v1/chat/completions",
            "is_free": False,
            "cached": False,
        })
    return {
        "success": True,
        "tested_at": "2026-11-09T09:00:00+00:00",
        "partial": False,
        "cached": True,
        "snapshot_last_completed_at": "2026-11-09T09:00:00+00:00",
        "snapshot_complete": True,
        "result_count": len(results),
        "catalog_count": len(results),
        "providers": [
            {
                "name": provider,
                "display_name": display,
                "icon": "◇",
                "has_key": True,
                "validated": True,
                "default_model": "",
                "capability_kind": "chat",
            }
            for provider, display in _PROVIDERS
        ],
        "models": [
            {
                "id": r["model"],
                "name": r["model"],
                "provider": r["provider"],
                "context_length": 0,
                "is_free": r["is_free"],
            }
            for r in results
        ],
        "results": results,
        "summary": {
            "available": usable,
            "failed": failing,
            "rate_limited": 1,
            "not_configured": 0,
            "invalid": 1,
            "insufficient_credits": 0,
        },
    }


def _usable_pairs(payload: dict) -> list[str]:
    usable = ai_module._usable_results(payload["results"])
    return [f"{r['provider']}:{r['model']}" for r in usable]


def _all_pages(payload: dict) -> list[tuple[str, list]]:
    """Every page body/buttons, following the real pager until the last page."""
    pages: list[tuple[str, list]] = []
    page = 0
    while True:
        body, buttons = ai_module._render_test_results(payload, page=page)
        pages.append((body, buttons))
        if f"{_PAGE}{page + 1}" not in _flatten(buttons):
            return pages
        page += 1
        assert page < 200, "pager never reached a last page"


@pytest.fixture
def store(monkeypatch):
    mem: dict[str, str] = {}

    async def _read(key):
        return mem.get(key), True

    async def _write(key, value):
        mem[key] = value
        return True

    monkeypatch.setattr(db_client, "settings_document_state", _read)
    monkeypatch.setattr(db_client, "settings_document_write", _write)
    ai_module._last_test_payload = {}
    ai_module._test_running = False
    yield mem
    ai_module._last_test_payload = {}
    ai_module._test_running = False


# ── 1 + 2. the body is bounded and still carries the summary ────────────────


def test_large_results_body_composes_within_the_media_caption_limit():
    payload = _payload()
    active = ai_module._usable_results(payload["results"])[0]

    for page, (body, _buttons) in enumerate(_all_pages(payload)):
        composed = appearance_service._compose_rendered("Test Modules", body)
        assert appearance_service.fits_as_caption("Test Modules", body), f"page {page}"
        assert len(composed) <= appearance_service.MAX_CAPTION_CHARS
        # Safe margin, not just under the edge.
        assert len(composed) <= appearance_service.MAX_CAPTION_CHARS - 100

    body, _ = ai_module._render_test_results(
        payload, current=(active["provider"], active["model"])
    )
    assert len(body) < appearance_service.MAX_CAPTION_CHARS


def test_compact_body_keeps_counts_current_pair_usable_total_and_snapshot():
    payload = _payload()
    active = ai_module._usable_results(payload["results"])[0]

    body, _ = ai_module._render_test_results(
        payload, current=(active["provider"], active["model"])
    )

    assert "**Model Tests**" in body
    # Available + failed counts and the relevant failure categories.
    assert f"{sym.OP_SAVE} 61 · {sym.STATE_FAILED} 4" in body
    assert "rate 1" in body
    assert "invalid 1" in body
    assert "not configured 0" in body
    assert "no credits 0" in body
    # Current runtime pair, usable total and snapshot provenance.
    assert "Current: " in body and active["model"] in body
    assert f"**{sym.OP_SAVE} Usable Models: 61**" in body
    assert "Cached snapshot" in body
    assert "2026-11-09 09:00 UTC" in body
    assert "Every usable model is on the buttons below" in body
    # No per-model listing and no provider-name listing in the body.
    assert "**Long Provider Display**" not in body
    assert "failed-0-" not in body


# ── 3 + 4. every usable pair reachable exactly once, exact callbacks ────────


def test_every_usable_model_is_reachable_exactly_once_across_pages():
    payload = _payload()
    expected = _usable_pairs(payload)

    collected: list[str] = []
    for body, buttons in _all_pages(payload):
        pick_datas = [d for d in _flatten(buttons) if d.startswith(_PICK)]
        pairs = [d[len(_PICK):] for d in pick_datas]
        collected.extend(pairs)
        # The grid never exceeds the adaptive 3-column page size.
        rows = [
            [_raw(btn) for btn in row or []]
            for row in buttons or []
        ]
        model_rows = [row for row in rows if row and all(d.startswith(_PICK) for d in row)]
        assert all(len(row) <= 3 for row in model_rows)
        assert len(pairs) <= ai_module._test_grid_page_size(3)

    assert collected == expected, "page order must stay deterministic"
    assert len(collected) == len(set(collected)) == len(expected) == 61


def test_callback_data_is_exact_for_provider_and_model_pairs():
    payload = _payload()
    _body, buttons = ai_module._render_test_results(payload, current=("groq", "m"))

    datas = [d for d in _flatten(buttons) if d.startswith(_PICK)]
    usable = ai_module._usable_results(payload["results"])
    # Exact provider:model data, in the same deterministic order, unchanged.
    assert datas == [
        f"{_PICK}{r['provider']}:{r['model']}" for r in usable[:len(datas)]
    ]


def test_long_identifiers_keep_the_body_bounded():
    payload = _payload()
    long_provider = "provider-with-a-very-long-display-name"

    body, buttons = ai_module._render_test_results(
        payload, current=(long_provider, _LONG_MODEL)
    )

    composed = appearance_service._compose_rendered("Test Modules", body)
    assert appearance_service.fits_as_caption("Test Modules", body)
    assert len(composed) <= appearance_service.MAX_CAPTION_CHARS - 100
    labels = _labels(buttons)
    assert labels and all(len(label) <= 64 for label in labels)
    # Telegram's 64-byte callback budget is applied by the existing helper;
    # this change does not alter that (pre-existing) encoding rule.
    pick_datas = [d for d in _flatten(buttons) if d.startswith(_PICK)]
    assert pick_datas and all(len(d.encode()) <= 64 for d in pick_datas)


def test_active_cached_and_free_indicators_live_on_the_model_buttons():
    payload = _payload()
    usable = ai_module._usable_results(payload["results"])
    active = usable[1]
    cached = next(r for r in usable if r.get("cached"))
    free = next(r for r in usable if r.get("is_free"))

    _body, buttons = ai_module._render_test_results(
        payload, current=(active["provider"], active["model"])
    )
    labels = _labels(buttons)
    datas = _flatten(buttons)
    active_data = f"{_PICK}{active['provider']}:{active['model']}"
    active_labels = [
        label for label, data in zip(labels, datas) if data == active_data
    ]
    assert active_labels and all(
        label.startswith(f"{sym.MEDIA_VOICE} ") for label in active_labels
    )
    assert any("·cached" in label for label in labels)
    assert any("·free" in label for label in labels)
    assert cached["model"] in "".join(labels) or cached["model"].split("/")[-1] in "".join(labels)
    assert free["model"] in "".join(labels) or free["model"].split("/")[-1] in "".join(labels)


# ── 6. paging is presentation-only ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_paging_is_presentation_only_and_never_starts_a_diagnostic():
    payload = _payload()
    ai_module._last_test_payload = payload
    before = [dict(r) for r in payload["results"]]

    with patch(
        "backend.ai.model_tester.test_all_models_streaming", AsyncMock()
    ) as mock_all, patch(
        "backend.ai.model_tester.test_single_model", AsyncMock()
    ) as mock_single:
        title, body, buttons = await ai_module._ai_test_page_action(None, "1", 0)

    mock_all.assert_not_called()
    mock_single.assert_not_called()
    assert title == "Test Modules"
    assert ai_module._last_test_payload is payload
    assert [dict(r) for r in payload["results"]] == before
    assert appearance_service.fits_as_caption("Test Modules", body)
    assert any(d.startswith(f"{_PAGE}2") for d in _flatten(buttons))


# ── 7. opening a saved snapshot: bounded body, zero provider calls ──────────


@pytest.mark.asyncio
async def test_opening_a_saved_snapshot_renders_the_bounded_body(store):
    payload = _payload()
    assert await ts.save_snapshot(ts.build_snapshot(payload)) is True

    with patch(
        "backend.ai.model_tester.test_single_model",
        AsyncMock(side_effect=AssertionError("no live model test while opening")),
    ) as mock_single, patch(
        "backend.ai.model_tester.test_all_models_streaming",
        AsyncMock(side_effect=AssertionError("no diagnostic while opening")),
    ) as mock_all, patch.object(
        ai_module, "_runtime_pair", return_value=("groq", "model-00")
    ):
        title, body, buttons = await ai_module._ai_test_models_action(MagicMock(), "", 0)

    mock_single.assert_not_called()
    mock_all.assert_not_called()
    assert title == "Test Modules"
    assert "Usable Models: 61" in body
    assert "Cached snapshot" in body
    assert appearance_service.fits_as_caption(title, body)
    assert len([d for d in _flatten(buttons) if d.startswith(_PICK)]) <= 18


# ── 8. streaming completion uses the same bounded renderer ─────────────────


@pytest.mark.asyncio
async def test_streaming_completion_uses_the_same_bounded_renderer():
    payload = _payload()
    fake_event = MagicMock()
    fake_event.chat_id = 1
    fake_event.message_id = 2

    with patch.object(
        ai_test_progress, "test_all_models_streaming", AsyncMock(return_value=payload)
    ), patch.object(
        ai_test_progress, "_persist_test_snapshot", AsyncMock(return_value=payload)
    ), patch.object(
        ai_module, "_runtime_pair", return_value=("groq", "model-00")
    ), patch(
        "backend.helper.panels._safe_edit", AsyncMock(return_value=True)
    ) as mock_edit:
        await ai_test_progress.run_streaming_test(1, fake_event)
        for _ in range(10):
            await asyncio.sleep(0)

    assert mock_edit.await_count == 1
    text, buttons = mock_edit.await_args.args[1], mock_edit.await_args.args[2]
    assert len(text) <= appearance_service.MAX_CAPTION_CHARS
    assert appearance_service.caption_for(text)
    assert "Usable Models: 61" in text
    with patch.object(ai_module, "_runtime_pair", return_value=("groq", "model-00")):
        assert ai_module._render_test_results(payload)[0] == text
    assert len([d for d in _flatten(buttons) if d.startswith(_PICK)]) == 18


# ── 9. initial run / explicit refresh unchanged ─────────────────────────────


@pytest.mark.asyncio
async def test_initial_run_and_refresh_workflow_is_unchanged(store):
    with patch.object(ai_module, "_get_owner_id", AsyncMock(return_value=1)), patch.object(
        ai_test_progress, "run_streaming_test", AsyncMock()
    ) as mock_run:
        _title, body, _buttons = await ai_module._ai_test_models_action(MagicMock(), "", 0)
        for _ in range(5):
            await asyncio.sleep(0)
    assert mock_run.call_count == 1
    assert "Starting the model test run" in body

    assert await ts.save_snapshot(ts.build_snapshot(_payload())) is True
    # The mocked run never clears the flag, exactly like an in-flight run.
    ai_module._test_running = False
    with patch.object(
        ai_test_progress, "run_streaming_test", AsyncMock()
    ) as refresh_run:
        _title, refresh_body, _buttons = await ai_module._ai_test_refresh_action(
            MagicMock(), "", 0
        )
        for _ in range(5):
            await asyncio.sleep(0)
    assert refresh_run.call_count == 1
    assert "Starting the model test run" in refresh_body
    assert ai_module._test_running is True


# ── 10. All Results keeps full diagnostics and pagination ───────────────────


@pytest.mark.asyncio
async def test_all_results_diagnostics_remain_full_and_paginated():
    payload = _payload(usable=5, failing=4)
    # Rich failures: the full diagnostic blocks are what paginate this view.
    for i in range(80):
        base = dict(payload["results"][-1])
        base["model"] = f"diagnostic-{i:02d}-chat-v1"
        payload["results"].append(base)
    payload["summary"]["failed"] = sum(
        1 for r in payload["results"] if r["status"] != "AVAILABLE"
    )
    ai_module._last_test_payload = payload

    bodies: list[str] = []
    page = 0
    while True:
        title, body, buttons = await ai_module._ai_test_details_action(None, str(page), 0)
        bodies.append(body)
        assert title == "Test Modules"
        assert "**All Results**" in body
        datas = _flatten(buttons)
        has_next = f"action:ai_test_details:{page + 1}" in datas
        if not has_next:
            break
        assert f"action:ai_test_details:{page - 1}" in datas or page == 0
        page += 1
        assert page < 200

    assert page >= 1, "a large diagnostic run must paginate"
    joined = "\n".join(bodies)
    assert "_Page 1/" in joined
    # Nothing is omitted: every failing pair, old and added, is present.
    for i in range(4):
        assert f"failed-{i}-chat-v1" in joined
    assert all(f"diagnostic-{i:02d}-chat-v1" in joined for i in range(80))
