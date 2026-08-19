"""
AI tool honesty + compact glass model message regression tests.

Covers the "false success" bug class: services communicate failures as
"❌ ..."/"⚠️ ..." strings, and tools must NEVER wrap those in
``success=True`` — the AI must answer from the real outcome.

Also covers:
  - delete_service returns REAL deleted counts and real errors
  - DeleteTool / DeleteByIdTool report actual counts (0, partial, error)
  - SaveTool is Deep Save only (no forward mode) and delegates to
    ``execute_save`` — it never fabricates a mode code
  - Deep save from a protected chat never falls back to forwarding
  - The compact glass Test Models message (provider-grouped usable list,
    no per-model diagnostic paragraphs, buttons preserved) plus the
    one-tap "All Results" diagnostics view.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch

from backend.ai.tools.base import ToolResult, result_from_service
from backend.ai.tools.context import ToolContext
from backend.services import delete_service


# ── result_from_service ──


def test_result_from_service_marks_failure_prefixes_as_failure():
    assert result_from_service("✅ Done.").success is True
    assert result_from_service("🗑 Deleted 3 messages.").success is True
    assert result_from_service("❌ Delete failed: boom").success is False
    assert result_from_service("⚠️ Template cannot be empty.").success is False


def test_result_from_service_preserves_message_and_data():
    res = result_from_service("❌ DB error", data={"key": 1})
    assert res.message == "❌ DB error"
    assert res.data == {"key": 1}


# ── delete_service real counts ──


class FakeDeleteClient:
    def __init__(self, ids=(), fail_on_delete=False):
        self.ids = list(ids)
        self.deleted = []
        self.fail_on_delete = fail_on_delete

    async def iter_messages(self, chat_id, **kwargs):
        limit = kwargs.get("limit")
        for mid in self.ids:
            yield type("M", (), {"id": mid, "out": True})()
            if limit is not None and len(self.deleted) + 1 >= limit:
                break

    async def delete_messages(self, chat_id, ids):
        if self.fail_on_delete:
            raise RuntimeError("Telegram RPC failed")
        if isinstance(ids, (list, tuple)):
            self.deleted.extend(ids)
        else:
            self.deleted.append(ids)


@pytest.mark.asyncio
async def test_do_del_n_counts_returns_real_count():
    client = FakeDeleteClient(ids=(1, 2, 3, 4, 5))
    deleted, error = await delete_service.do_del_n_counts(client, -100, 3)
    assert error is None
    assert deleted == 3
    assert client.deleted == [1, 2, 3]


@pytest.mark.asyncio
async def test_do_del_n_counts_zero_when_nothing_matches():
    client = FakeDeleteClient(ids=())
    deleted, error = await delete_service.do_del_n_counts(client, -100, 5)
    assert error is None
    assert deleted == 0


@pytest.mark.asyncio
async def test_do_del_n_counts_surfaces_real_error():
    client = FakeDeleteClient(ids=(1, 2, 3), fail_on_delete=True)
    deleted, error = await delete_service.do_del_n_counts(client, -100, 2)
    assert error is not None
    assert "Telegram RPC failed" in str(error)
    assert deleted == 0


@pytest.mark.asyncio
async def test_do_del_id_counts_counts_batches():
    client = FakeDeleteClient(ids=(10, 11, 12, 13))
    deleted, error = await delete_service.do_del_id_counts(client, -100, 10)
    assert error is None
    assert deleted == 4


# ── DeleteTool honesty ──


class _FakeTelegram:
    def __init__(self, client):
        self.client = client


def _ctx(client, chat_id=-100, reply_meta=True) -> ToolContext:
    extra: dict = {"chat_id": chat_id}
    if reply_meta:
        extra["reply_msg"] = {"chat_id": 100, "message_id": 200}
    return ToolContext(
        telegram=_FakeTelegram(client),
        owner_id=1,
        tz_str="UTC",
        extra=extra,
    )


@pytest.mark.asyncio
async def test_delete_tool_reports_real_deleted_count():
    from backend.ai.tools.delete import DeleteTool

    client = FakeDeleteClient(ids=(1, 2, 3, 4, 5))
    tool = DeleteTool(_ctx(client))
    result = await tool.execute(_ctx(client), {"count": 3})
    assert result.success is True
    assert result.data["count"] == 3
    assert "Deleted 3" in result.message


@pytest.mark.asyncio
async def test_delete_tool_zero_when_none_matched():
    from backend.ai.tools.delete import DeleteTool

    client = FakeDeleteClient(ids=())
    result = await DeleteTool(_ctx(client)).execute(_ctx(client), {"count": 3})
    assert result.success is True
    assert result.data["count"] == 0
    assert "nothing was deleted" in result.message


@pytest.mark.asyncio
async def test_delete_tool_never_converts_error_to_success():
    from backend.ai.tools.delete import DeleteTool

    client = FakeDeleteClient(ids=(1, 2, 3), fail_on_delete=True)
    result = await DeleteTool(_ctx(client)).execute(_ctx(client), {"count": 3})
    assert result.success is False
    assert "Telegram RPC failed" in result.message
    assert result.data["count"] == 0


@pytest.mark.asyncio
async def test_delete_by_id_tool_honest_results():
    from backend.ai.tools.delete import DeleteByIdTool

    ok_client = FakeDeleteClient(ids=(10, 11, 12))
    result = await DeleteByIdTool(_ctx(ok_client)).execute(_ctx(ok_client), {"message_id": 10})
    assert result.success is True
    assert result.data["count"] == 3

    bad_client = FakeDeleteClient(ids=(10, 11), fail_on_delete=True)
    result = await DeleteByIdTool(_ctx(bad_client)).execute(_ctx(bad_client), {"message_id": 10})
    assert result.success is False
    assert "Telegram RPC failed" in result.message


# ── SaveTool mode routing + honesty ──


class _FakeMessage:
    chat_id = 100
    id = 200
    text = "hello"
    media = None
    sender_id = 777


@pytest.mark.asyncio
async def test_save_tool_is_deep_only_and_has_no_mode_param():
    from backend.ai.tools.save import SaveTool

    assert SaveTool.parameters.fget(SaveTool) == {}

    with patch("backend.services.save_service.execute_save", AsyncMock(return_value="✅ Saved")) as m:
        tool = SaveTool(_ctx(FakeDeleteClient()))
        with patch.object(tool, "_resolve_reply_message", AsyncMock(return_value=_FakeMessage())):
            result = await tool.execute(_ctx(FakeDeleteClient()), {})

    assert result.success is True
    # execute_save is called with (client, owner_id, reply_msg, tz_str) — no mode.
    assert len(m.await_args.args) == 4
    assert m.await_args.args[2] is not None


@pytest.mark.asyncio
async def test_save_tool_service_error_string_is_not_success():
    from backend.ai.tools.save import SaveTool

    with patch(
        "backend.services.save_service.execute_save",
        AsyncMock(return_value="❌ Deep Save failed: unable to download the source message."),
    ):
        tool = SaveTool(_ctx(FakeDeleteClient()))
        with patch.object(tool, "_resolve_reply_message", AsyncMock(return_value=_FakeMessage())):
            result = await tool.execute(_ctx(FakeDeleteClient()), {})

    assert result.success is False
    assert "unable to download" in result.message


@pytest.mark.asyncio
async def test_bio_tool_service_error_string_is_not_success():
    from backend.ai.tools.bio import BioSetTemplateTool

    with patch("backend.services.bio_service.do_template", AsyncMock(return_value="❌ DB error: boom")):
        tool = BioSetTemplateTool(_ctx(FakeDeleteClient()))
        result = await tool.execute(_ctx(FakeDeleteClient()), {"template": "t"})

    assert result.success is False
    assert "DB error" in result.message


# ── Deep save protected-chat behavior ──

import os
import shutil
import tempfile

from telethon.tl.types import MessageMediaDocument, DocumentAttributeFilename


@pytest.mark.asyncio
async def test_deep_save_protected_chat_never_forwards():
    """Deep save from a chat whose content cannot be copied must report the
    download failure — it must NEVER fall back to forward_messages."""
    from backend.services import save_service

    calls = []

    class ProtectedClient:
        async def forward_messages(self, entity, messages):
            calls.append("forward_messages")
            raise AssertionError("deep save must never forward")

        async def download_media(self, message, file=None, **kwargs):
            calls.append("download_media")
            raise RuntimeError("You can't download messages from a protected chat")

        async def send_file(self, *args, **kwargs):
            calls.append("send_file")
            return type("S", (), {"chat_id": "me", "id": 1, "media": None})()

        async def send_message(self, *args, **kwargs):
            calls.append("send_message")
            return type("S", (), {"chat_id": "me", "id": 1, "media": None})()

    doc = type("D", (), {
        "id": 9999,
        "mime_type": "image/jpeg",
        "size": 100,
        "attributes": [DocumentAttributeFilename("photo.jpg")],
    })()
    media = MessageMediaDocument(document=doc)
    msg = type("M", (), {
        "chat_id": -100,
        "id": 5,
        "text": "protected",
        "media": media,
        "sender_id": 777,
        "sender": None,
    })()

    async def get_sender():
        return None
    msg.get_sender = get_sender  # type: ignore[attr-defined]

    with patch.object(save_service.db_client, "get_next_save_code", AsyncMock(return_value="S001")):
        result = await save_service.execute_save(ProtectedClient(), 1, msg, "UTC")

    assert "download_media" in calls
    assert "forward_messages" not in calls
    assert result.startswith("❌")
    assert "protected chat" in result


# ── Compact glass Test Models message ──


def _flatten_button_datas(buttons) -> list[str]:
    datas = []
    for row in buttons:
        if isinstance(row, list):
            for btn in row:
                data = getattr(btn, "data", None)
                if isinstance(data, bytes):
                    datas.append(data.decode("utf-8", errors="replace"))
                elif isinstance(data, str):
                    datas.append(data)
        else:
            data = getattr(row, "data", None)
            if isinstance(data, bytes):
                datas.append(data.decode("utf-8", errors="replace"))
            elif isinstance(data, str):
                datas.append(data)
    return datas


def _sample_payload() -> dict:
    def res(provider, display, model, status, latency=None, http=None, error=None):
        return {
            "provider": provider, "display_name": display, "icon": "🤖",
            "model": model, "status": status, "error": error,
            "latency_s": latency, "http_status": http, "retry_after": None,
            "error_type": None, "provider_code": None, "finish_reason": None,
            "capabilities": [], "tested_at": "2026-08-18T00:00:00+00:00",
        }

    results = [
        res("groq", "Groq", "openai/gpt-oss-120b", "AVAILABLE", latency=0.4),
        res("groq", "Groq", "openai/gpt-oss-20b", "AVAILABLE", latency=0.6),
        res("groq", "Groq", "allam-2-7b", "AVAILABLE", latency=0.5),
        res("gemini", "Google Gemini", "gemini-2.0-flash", "INVALID_MODEL", http=404, error="Model not found"),
        res("openrouter", "OpenRouter", "openai/gpt-4o", "INSUFFICIENT_CREDITS", http=402, error="insufficient credits"),
        res("openai", "OpenAI", "gpt-4o", "RATE_LIMITED", http=429, error="rate limited, retry-after: 30"),
    ]
    return {
        "success": True,
        "tested_at": "2026-08-18T00:00:00+00:00",
        "partial": False,
        "providers": [],
        "models": [],
        "results": results,
        "summary": {
            "total": 6, "available": 3, "unavailable": 1, "error": 2,
            "timeout": 0, "not_configured": 0, "discovered": 6, "tested": 6,
            "failed": 3, "rate_limited": 1, "invalid": 1, "insufficient_credits": 1,
            "blocked": 0, "auth_error": 0, "provider_error": 0, "unknown_error": 0,
        },
    }


@pytest.mark.asyncio
async def test_glass_test_models_message_is_compact_and_preserves_buttons():
    from backend.bot.handlers import ai as ai_module

    with patch("backend.ai.model_tester.test_all_models", AsyncMock(return_value=_sample_payload())):
        title, body, buttons = await ai_module._ai_test_models_action(None, "", 0)

    assert title == "🧪 Test Models"
    # Compact: provider-grouped usable list, model name dominant.
    assert "**✅ Usable Models**" in body
    assert "🟢 **Groq**" in body
    assert "• `gpt-oss-120b`" in body
    assert "• `gpt-oss-20b`" in body
    # Summary chips still present.
    assert "Available: 3" in body
    # No diagnostic paragraphs in the main message.
    assert "HTTP" not in body
    assert "retry-after" not in body
    # Failed models: one compact line each, capped.
    assert "**⚠️ Not usable: 3**" in body
    # Buttons preserved: pick-model rows + re-run + details.
    datas = _flatten_button_datas(buttons)
    assert any(d.startswith("action:ai_pick_model:groq:openai/gpt-oss-120b") for d in datas)
    assert "action:ai_test_models" in datas
    assert "action:ai_test_details" in datas
    assert "panel:ai_model" in datas
    assert "panel:ai_status" in datas
    assert any(d.startswith("panel:_nav:") for d in datas)


@pytest.mark.asyncio
async def test_glass_test_models_no_usable_state():
    from backend.bot.handlers import ai as ai_module

    payload = _sample_payload()
    payload["results"] = [r for r in payload["results"] if r["status"] != "AVAILABLE"]
    payload["summary"]["available"] = 0

    with patch("backend.ai.model_tester.test_all_models", AsyncMock(return_value=payload)):
        title, body, buttons = await ai_module._ai_test_models_action(None, "", 0)

    assert "No usable chat models right now" in body
    datas = _flatten_button_datas(buttons)
    assert not any(d.startswith("action:ai_pick_model") for d in datas)
    assert "action:ai_test_models" in datas


@pytest.mark.asyncio
async def test_glass_test_details_shows_full_diagnostics_from_cache():
    from backend.bot.handlers import ai as ai_module

    with patch("backend.ai.model_tester.test_all_models", AsyncMock(return_value=_sample_payload())):
        await ai_module._ai_test_models_action(None, "", 0)

    title, body, buttons = await ai_module._ai_test_details_action(None, "", 0)
    assert "All Results" in body
    assert "HTTP 404" in body
    assert "INSUFFICIENT_CREDITS" in body
    datas = _flatten_button_datas(buttons)
    assert "action:ai_test_models" in datas


def test_glass_register_wires_details_action():
    from backend.bot.handlers import ai as ai_module

    with patch.object(ai_module, "register_action") as mock_register_action, \
         patch.object(ai_module, "register_panel"), \
         patch.object(ai_module, "register_inline_builder"), \
         patch.object(ai_module, "register_input"):
        ai_module.register(None, 0)

    registered_ids = [call.args[0] for call in mock_register_action.call_args_list]
    assert "ai_test_details" in registered_ids
    # All pre-existing actions preserved.
    for action in (
        "ai_select_provider", "ai_select_model", "ai_pick_model",
        "ai_refresh_providers", "ai_refresh_models", "ai_start_chat",
        "ai_status_refresh", "ai_diagnostics_refresh", "ai_test_models",
    ):
        assert action in registered_ids, f"action {action} must remain registered"
