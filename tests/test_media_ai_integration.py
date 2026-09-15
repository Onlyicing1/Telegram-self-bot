"""
Media → LLM integration — the controlled normalized media representation as
ordinary model input, with ZERO Telegram/conversational context.

Contract this file pins (the phase that wires the M1 media boundary into the
owner's selected provider):

  1. A media request is resolved DETERMINISTICALLY, outside the model: the
     target is the replied-to message or the triggering message itself, chosen
     from the runtime's own ids. Nothing else can pick a Telegram message.
  2. The provider receives exactly two controlled inputs — the owner's authored
     request and ``MediaAnalysis.as_context_text()`` — and NOTHING else: no
     reply text, no sender, no chat title, no chat id, no message id, no
     Telegram window, no AI session history, no temporary path, no Telegram or
     Telethon object.
  3. The owner's SELECTED provider answers through the ordinary provider-neutral
     plain-string path (``ProviderManager.chat``), unchanged, and the dead
     ``vision`` path is never used.
  4. Unsupported media, a media failure and a provider failure are all honest:
     nothing is fabricated, nothing falls back to another media source, and the
     model is not asked to guess.
  5. The deterministic command fast path keeps its precedence — a media reply
     that is really a delete/save/retrieve command still resolves as one.
  6. Ordinary text-only requests are untouched: the prompt/context pipeline runs
     exactly as before.

No live Telegram and no network: the Telegram boundary is a scripted fake shaped
like the Telethon client surface the facade consumes, and the provider is a
scripted adapter registered in the REAL ``ProviderRegistry``/``ProviderManager``
so the provider-neutral call path is exercised for real.
"""
from __future__ import annotations

import asyncio
import inspect
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon.tl.types import (
    Document,
    DocumentAttributeFilename,
    DocumentAttributeVideo,
    MessageMediaDocument,
    MessageMediaPhoto,
    Photo,
    PhotoSize,
)

from backend.ai.conversation.context_builder import ReplyContext
from backend.ai.conversation.telegram_context import TelegramChatContext, TelegramContextMessage
from backend.ai.engine.dispatcher import Dispatcher
from backend.ai.engine.hooks import NOOP_HOOKS
from backend.ai.engine.metrics import EngineMetrics
from backend.ai.providers.base.capabilities import ProviderCapabilities
from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.base.contract import BaseProvider, ProviderResponse
from backend.ai.providers.manager.manager import ProviderManager
from backend.ai.providers.registry.registry import ProviderRegistry
from backend.ai.session.request import AIRequest
from backend.ai.tools.context import ToolContext
from backend.ai.tools.executor import ToolExecutor
from backend.ai.tools.registry import create_default_registry
from backend.services import media_ai_service, media_service
from backend.services.media_service import MediaAnalysis, MediaStatus
from backend.telegram_api.api import TelegramAPI

OWNER = 7770001
CHAT = -1007778889999
OTHER_CHAT = -100555000
REPLY_ID = 57494
REQUEST_ID = 57500
REQUEST_TEXT = "این فایل رو خلاصه کن"

#: Distinctive values that must never reach the provider for a media request.
REPLY_PREVIEW = "reply-text-preview-LEAK"
REPLY_SENDER = "sender-name-LEAK"
REPLY_TITLE = "chat-title-LEAK"
WINDOW_TEXT = "surrounding-window-LEAK"
CAPTION = "caption-LEAK"
FILE_NAME = "notes.txt"
PAYLOAD = b"the document body"


# ── Telegram fakes ──


class _FakeMessage:
    """The attribute surface ``backend/ai/media.classify_message`` inspects."""

    def __init__(self, media: Any, *, caption: str = "", mid: int = REPLY_ID,
                 chat_id: int = CHAT) -> None:
        self.media = media
        self.message = caption
        self.text = caption
        self.id = mid
        self.chat_id = chat_id


class _FakeClient:
    """A scripted Telethon-shaped client recording the exact call order."""

    def __init__(self, *, message: Any = None, payload: bytes = PAYLOAD,
                 resolve_error: Exception | None = None,
                 download_error: Exception | None = None) -> None:
        self.message = message
        self.payload = payload
        self.resolve_error = resolve_error
        self.download_error = download_error
        self.events: list[tuple[str, Any]] = []

    async def get_messages(self, chat_id: Any, ids: Any = None, **kwargs: Any) -> Any:
        self.events.append(("get_messages", (chat_id, ids)))
        if self.resolve_error is not None:
            raise self.resolve_error
        return self.message

    async def download_media(self, message: Any, file: Any = None,
                             progress_callback: Any = None, **kwargs: Any) -> Any:
        self.events.append(("download_media", file))
        if self.download_error is not None:
            raise self.download_error
        if file:
            with open(file, "wb") as handle:
                handle.write(self.payload)
            return file
        return None

    def ops(self) -> list[str]:
        return [name for name, _ in self.events]


class _TrapClient:
    """A client that fails the test if the media boundary touches Telegram."""

    async def get_messages(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("media resolution must not run for this request")

    async def download_media(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("media must not be downloaded for this request")


def _text_document(mid: int = REPLY_ID, chat_id: int = CHAT, *, name: str = FILE_NAME,
                   caption: str = CAPTION, size: int = len(PAYLOAD)) -> _FakeMessage:
    return _FakeMessage(
        MessageMediaDocument(document=Document(
            id=1, access_hash=1, file_reference=b"", date=None, mime_type="text/plain",
            size=size, dc_id=1, attributes=[DocumentAttributeFilename(file_name=name)],
        )),
        caption=caption, mid=mid, chat_id=chat_id,
    )


def _photo(mid: int = REPLY_ID, chat_id: int = CHAT) -> _FakeMessage:
    return _FakeMessage(
        MessageMediaPhoto(photo=Photo(
            id=2, access_hash=2, file_reference=b"", date=None,
            sizes=[PhotoSize(type="y", w=1, h=1, size=2048)], dc_id=1,
        )),
        caption=CAPTION, mid=mid, chat_id=chat_id,
    )


def _video(mid: int = REPLY_ID, chat_id: int = CHAT) -> _FakeMessage:
    return _FakeMessage(
        MessageMediaDocument(document=Document(
            id=3, access_hash=3, file_reference=b"", date=None, mime_type="video/mp4",
            size=4096, dc_id=1, attributes=[DocumentAttributeVideo(duration=1, w=1, h=1)],
        )),
        caption=CAPTION, mid=mid, chat_id=chat_id,
    )


# ── Scripted provider (registered in the REAL manager) ──


class _ScriptedProvider(BaseProvider):
    def __init__(self, text: str = "ANSWER", *, success: bool = True,
                 delay: float = 0.0) -> None:
        super().__init__(ProviderConfig(provider_name="scripted", enabled=True, default_model="m1"))
        self._text = text
        self._success = success
        self._delay = delay
        self.prompts: list[list[dict[str, Any]]] = []

    @property
    def name(self) -> str:
        return "scripted"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(supports_tools=True)

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> ProviderResponse:
        self.prompts.append([dict(m) for m in messages])
        if self._delay:
            await asyncio.sleep(self._delay)
        if not self._success:
            return ProviderResponse(
                text="", provider_name="scripted", success=False,
                metadata={"failure_type": "rate_limit"},
            )
        return ProviderResponse(
            text=self._text, provider_name="scripted", success=True,
            metadata={"model": "m1"},
        )

    async def vision(self, *args: Any, **kwargs: Any) -> ProviderResponse:  # pragma: no cover
        raise AssertionError("the dead vision() path must never be used")

    def initialize(self) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def health(self) -> dict[str, Any]:
        return {"healthy": True, "ready": True}


def _manager(provider: _ScriptedProvider) -> ProviderManager:
    registry = ProviderRegistry()
    registry.register(provider)
    manager = ProviderManager(registry)
    manager.switch_provider(provider.name)
    return manager


def _payload(provider: _ScriptedProvider) -> str:
    return "\n".join(
        message["content"] for message in provider.prompts[0]
    ) if provider.prompts else ""


# ── Dispatcher harness ──


def _dispatcher(
    manager: ProviderManager, client: Any, *, fail_prompt_build: bool = False,
) -> tuple[Dispatcher, Any]:
    """The real Dispatcher with a real ToolExecutor and a scripted provider.

    ``fail_prompt_build`` makes any prompt construction fail the test, which is
    how the media tests prove the prompt/context pipeline is bypassed entirely.
    """
    ctx = ToolContext(
        telegram=TelegramAPI(client),
        owner_id=OWNER,
        tz_str="UTC",
        client=client,
        extra={"chat_id": CHAT, "request_id": "media-integration"},
    )
    executor = ToolExecutor(create_default_registry(ctx), ctx)

    conversation = MagicMock()
    session = MagicMock()
    session.session_id = "s"
    session.owner_id = OWNER
    session.active_provider = manager.get_active_name()
    conversation.get_session.return_value = session
    conversation.restore_history = AsyncMock()
    conversation.get_history.return_value = []

    prompt_builder = MagicMock()
    if fail_prompt_build:
        prompt_builder.build.side_effect = AssertionError(
            "a media request must not build a prompt"
        )
    else:
        package = MagicMock()
        package.system_prompt = "sys"
        package.runtime_context = ""
        package.conversation_context = ""
        package.tool_context = ""
        package.user_input = "hi"
        package.metadata = {}
        package.estimated_tokens.estimated_input_tokens = 1
        package.estimated_tokens.prompt_size_chars = 1
        prompt_builder.build.return_value = package

    dispatcher = Dispatcher(
        conversation, prompt_builder, manager, NOOP_HOOKS, EngineMetrics(),
        tool_executor=executor,
    )
    return dispatcher, prompt_builder


def _reply_context(**overrides: Any) -> ReplyContext:
    values: dict[str, Any] = {
        "exists": True,
        "message_id": REPLY_ID,
        "sender_id": 4242,
        "sender_name": REPLY_SENDER,
        "chat_id": CHAT,
        "chat_title": REPLY_TITLE,
        "media_type": "Document",
        "text_preview": REPLY_PREVIEW,
        "timestamp": "2026-09-15T10:00:00+00:00",
    }
    values.update(overrides)
    return ReplyContext(**values)


def _window() -> TelegramChatContext:
    return TelegramChatContext(
        chat_id=CHAT,
        messages=(
            TelegramContextMessage(message_id=1, sender_name=REPLY_SENDER, text=WINDOW_TEXT),
        ),
    )


def _request(**overrides: Any) -> AIRequest:
    values: dict[str, Any] = {
        "session_id": "s",
        "user_message": REQUEST_TEXT,
        "owner_id": OWNER,
        "chat_id": CHAT,
        "message_id": REQUEST_ID,
        "request_id": "media-integration",
        "timeout_s": 240.0,
    }
    values.update(overrides)
    return AIRequest(**values)


# ── 1. Deterministic resolution ──


def test_media_target_uses_only_runtime_ids_and_a_fixed_precedence():
    from backend.ai.engine.dispatcher import Dispatcher as _D

    target = _D._media_target(object.__new__(_D), _request(reply_context=_reply_context()))
    assert target == (CHAT, REPLY_ID)

    # The replied message's own chat wins; the request chat is the fallback.
    target = _D._media_target(
        object.__new__(_D), _request(reply_context=_reply_context(chat_id=0))
    )
    assert target == (CHAT, REPLY_ID)

    # A media message the owner sent themselves is the target when there is no reply.
    target = _D._media_target(
        object.__new__(_D), _request(request_media_type="Document", message_id=REQUEST_ID)
    )
    assert target == (CHAT, REQUEST_ID)


@pytest.mark.parametrize("media_type", ["WebPage", "Contact", "Poll", "Location", "Unknown", ""])
def test_non_downloadable_media_types_never_resolve(media_type):
    from backend.ai.engine.dispatcher import Dispatcher as _D

    dispatcher = object.__new__(_D)

    assert _D._media_target(
        dispatcher, _request(reply_context=_reply_context(media_type=media_type))
    ) is None
    assert _D._media_target(
        dispatcher, _request(request_media_type=media_type)
    ) is None


def test_a_text_only_request_has_no_media_target():
    from backend.ai.engine.dispatcher import Dispatcher as _D

    dispatcher = object.__new__(_D)

    assert _D._media_target(dispatcher, _request()) is None
    assert _D._media_target(dispatcher, _request(reply_context=ReplyContext())) is None


# ── 2. The provider-facing payload ──


@pytest.mark.asyncio
async def test_reply_to_media_is_answered_from_the_authored_request_and_the_media_text():
    provider = _ScriptedProvider("SUMMARY")
    client = _FakeClient(message=_text_document())
    manager = _manager(provider)
    dispatcher, prompt_builder = _dispatcher(manager, client, fail_prompt_build=True)

    result = await dispatcher.dispatch(_request(
        reply_context=_reply_context(), telegram_context=_window(),
    ))

    assert result.success is True
    assert result.response == "SUMMARY"
    prompt_builder.build.assert_not_called()
    assert client.ops() == ["get_messages", "download_media"]

    assert len(provider.prompts) == 1
    messages = provider.prompts[0]
    assert [m["role"] for m in messages] == ["system", "user"]
    assert messages[0]["content"] == media_ai_service.MEDIA_ANALYSIS_SYSTEM_PROMPT
    assert REQUEST_TEXT in messages[1]["content"]
    assert PAYLOAD.decode() in messages[1]["content"]
    assert "Status: extracted" in messages[1]["content"]


@pytest.mark.asyncio
async def test_attached_media_request_is_resolved_from_the_triggering_message():
    provider = _ScriptedProvider("OK")
    client = _FakeClient(message=_text_document(mid=REQUEST_ID))
    dispatcher, _ = _dispatcher(_manager(provider), client, fail_prompt_build=True)

    result = await dispatcher.dispatch(_request(request_media_type="Document"))

    assert result.success is True and result.response == "OK"
    assert client.events[0] == ("get_messages", (CHAT, REQUEST_ID))
    assert REQUEST_TEXT in provider.prompts[0][1]["content"]


@pytest.mark.asyncio
async def test_the_provider_receives_zero_telegram_context_and_no_temp_paths():
    provider = _ScriptedProvider("SUMMARY")
    client = _FakeClient(message=_text_document())
    dispatcher, _ = _dispatcher(_manager(provider), client, fail_prompt_build=True)

    await dispatcher.dispatch(_request(
        reply_context=_reply_context(), telegram_context=_window(),
    ))

    payload = _payload(provider)

    assert REPLY_PREVIEW not in payload
    assert REPLY_SENDER not in payload
    assert REPLY_TITLE not in payload
    assert WINDOW_TEXT not in payload
    assert CAPTION not in payload
    assert FILE_NAME not in payload
    assert str(REPLY_ID) not in payload
    assert str(abs(CHAT)) not in payload
    assert "lifeos_media_" not in payload
    assert "/tmp" not in payload
    assert "Telegram Chat Context" not in payload
    assert "Reply Context" not in payload
    # Two messages only — no history, no memory, no tool block, no system-context dump.
    assert len(provider.prompts[0]) == 2


@pytest.mark.asyncio
async def test_no_telethon_object_or_object_repr_reaches_the_provider():
    provider = _ScriptedProvider("SUMMARY")
    client = _FakeClient(message=_text_document())
    dispatcher, _ = _dispatcher(_manager(provider), client, fail_prompt_build=True)

    await dispatcher.dispatch(_request(
        reply_context=_reply_context(), telegram_context=_window(),
    ))

    for message in provider.prompts[0]:
        assert set(message) == {"role", "content"}
        assert type(message["role"]) is str
        assert type(message["content"]) is str

    blob = _payload(provider)
    assert "telethon" not in blob.lower()
    assert "MessageMedia" not in blob
    assert "object at 0x" not in blob


@pytest.mark.asyncio
async def test_the_media_analysis_is_the_only_media_source(monkeypatch):
    provider = _ScriptedProvider("SUMMARY")
    client = _FakeClient(message=_text_document())
    dispatcher, _ = _dispatcher(_manager(provider), client, fail_prompt_build=True)

    crafted = MediaAnalysis(
        media_type="Document",
        mime_type="text/plain",
        file_size=42,
        file_name=FILE_NAME,
        status=MediaStatus.EXTRACTED,
        content="CONTROLLED-CONTENT",
        reason="",
        caption=CAPTION,
        source_chat_id=CHAT,
        source_message_id=REPLY_ID,
    )

    async def _analyze(*args: Any, **kwargs: Any) -> MediaAnalysis:
        return crafted

    monkeypatch.setattr(media_service, "analyze_media", _analyze)

    await dispatcher.dispatch(_request(reply_context=_reply_context()))

    payload = _payload(provider)
    assert "CONTROLLED-CONTENT" in payload
    assert crafted.as_context_text() in payload
    # Even with the metadata ON the analysis object, only the rendering travels.
    assert CAPTION not in payload
    assert str(REPLY_ID) not in payload


@pytest.mark.asyncio
async def test_the_authored_request_is_never_replaced_by_the_media():
    provider = _ScriptedProvider("SUMMARY")
    client = _FakeClient(message=_text_document())
    dispatcher, _ = _dispatcher(_manager(provider), client, fail_prompt_build=True)

    await dispatcher.dispatch(_request(
        user_message="چند کلمه کلیدی بده", reply_context=_reply_context(),
    ))

    user_message = provider.prompts[0][1]["content"]
    assert user_message.startswith("چند کلمه کلیدی بده")
    assert PAYLOAD.decode() in user_message


# ── 3. Deterministic resolution happens before provider reasoning ──


@pytest.mark.asyncio
async def test_resolution_precedes_the_provider_call_and_needs_no_provider_reasoning():
    order: list[str] = []

    class _OrderedClient(_FakeClient):
        async def get_messages(self, chat_id: Any, ids: Any = None, **kwargs: Any) -> Any:
            order.append("resolve")
            return await super().get_messages(chat_id, ids=ids, **kwargs)

        async def download_media(self, message: Any, file: Any = None, **kwargs: Any) -> Any:
            order.append("download")
            return await super().download_media(message, file=file, **kwargs)

    class _OrderedProvider(_ScriptedProvider):
        async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> ProviderResponse:
            order.append("provider")
            return await super().chat(messages, **kwargs)

    provider = _OrderedProvider("SUMMARY")
    client = _OrderedClient(message=_text_document())
    dispatcher, _ = _dispatcher(_manager(provider), client, fail_prompt_build=True)

    await dispatcher.dispatch(_request(reply_context=_reply_context()))

    assert order == ["resolve", "download", "provider"]


@pytest.mark.asyncio
async def test_the_fast_path_keeps_its_precedence_over_the_media_route():
    provider = _ScriptedProvider("SUMMARY")
    client = _TrapClient()
    dispatcher, _ = _dispatcher(_manager(provider), client)

    result = await dispatcher.dispatch(_request(
        user_message="آخرین پیامم رو پاک کن",
        reply_context=_reply_context(),
    ))

    # A deterministic command still resolves deterministically (no provider round).
    assert result.success is True
    assert result.metadata["finish_state"] == "local_fast_path"
    assert result.metadata["ai_action"]["action"] == "delete_messages"
    assert provider.prompts == []


# ── 4. Unsupported media, failures, honesty ──


@pytest.mark.asyncio
async def test_unsupported_media_produces_an_honest_answer_without_a_provider_round():
    provider = _ScriptedProvider("SHOULD-NOT-RUN")
    client = _FakeClient(message=_photo())
    dispatcher, _ = _dispatcher(_manager(provider), client, fail_prompt_build=True)

    result = await dispatcher.dispatch(_request(reply_context=_reply_context(media_type="Photo")))

    assert result.success is True
    assert "Photo" in result.response
    assert "can't process" in result.response
    assert result.provider == "local" and result.model == "deterministic"
    assert result.metadata["media_status"] == MediaStatus.UNSUPPORTED
    assert provider.prompts == []
    assert "download_media" not in client.ops()


@pytest.mark.asyncio
async def test_unresolvable_media_fails_closed_without_a_provider_round():
    provider = _ScriptedProvider("SHOULD-NOT-RUN")
    client = _FakeClient(message=None)
    dispatcher, _ = _dispatcher(_manager(provider), client, fail_prompt_build=True)

    result = await dispatcher.dispatch(_request(reply_context=_reply_context()))

    assert result.success is False
    assert "Media processing failed" in result.errors[0]
    assert "could not be found" in result.errors[0]
    assert result.response == ""
    assert provider.prompts == []


@pytest.mark.asyncio
async def test_resolution_errors_fail_honestly_and_do_not_fall_back():
    provider = _ScriptedProvider("SHOULD-NOT-RUN")
    client = _FakeClient(resolve_error=ConnectionError("mtproto down"))
    dispatcher, _ = _dispatcher(_manager(provider), client, fail_prompt_build=True)

    result = await dispatcher.dispatch(_request(reply_context=_reply_context()))

    assert result.success is False
    assert "Media processing failed" in result.errors[0]
    assert provider.prompts == []
    assert client.ops() == ["get_messages"]


@pytest.mark.asyncio
async def test_download_failures_fail_honestly_and_clean_up():
    import os
    import tempfile

    provider = _ScriptedProvider("SHOULD-NOT-RUN")
    client = _FakeClient(message=_text_document(), download_error=RuntimeError("flood"))
    dispatcher, _ = _dispatcher(_manager(provider), client, fail_prompt_build=True)
    before = {n for n in os.listdir(tempfile.gettempdir()) if n.startswith("lifeos_media_")}

    result = await dispatcher.dispatch(_request(reply_context=_reply_context()))

    assert result.success is False
    assert "Media processing failed" in result.errors[0]
    assert "flood" in result.errors[0]
    assert provider.prompts == []
    assert {n for n in os.listdir(tempfile.gettempdir()) if n.startswith("lifeos_media_")} == before


@pytest.mark.asyncio
async def test_provider_failure_is_reported_honestly():
    provider = _ScriptedProvider(success=False)
    client = _FakeClient(message=_text_document())
    dispatcher, _ = _dispatcher(_manager(provider), client, fail_prompt_build=True)

    result = await dispatcher.dispatch(_request(reply_context=_reply_context()))

    assert result.success is False
    assert "Media processing failed" in result.errors[0]
    assert "the AI provider failed" in result.errors[0]
    # The manager's own exhausted-candidates reason is surfaced, not invented.
    assert "fallback_exhausted" in result.errors[0]
    assert result.response == ""
    assert len(provider.prompts) >= 1


@pytest.mark.asyncio
async def test_provider_call_is_bounded(monkeypatch):
    monkeypatch.setattr(media_ai_service, "PROVIDER_CALL_SAFETY_TIMEOUT_S", 0.05)
    monkeypatch.setattr(media_ai_service, "MIN_PROVIDER_CALL_TIMEOUT_S", 0.05)
    provider = _ScriptedProvider("SLOW", delay=30.0)
    client = _FakeClient(message=_text_document())
    dispatcher, _ = _dispatcher(_manager(provider), client, fail_prompt_build=True)

    import time
    started = time.monotonic()
    result = await dispatcher.dispatch(_request(reply_context=_reply_context()))
    elapsed = time.monotonic() - started

    assert result.success is False
    assert "did not respond within" in result.errors[0]
    assert elapsed < 5.0


def test_the_provider_call_bound_is_finite_and_never_exceeds_the_envelope():
    assert 0 < media_ai_service.PROVIDER_CALL_SAFETY_TIMEOUT_S < 600
    assert media_ai_service.media_call_timeout(None) == media_ai_service.PROVIDER_CALL_SAFETY_TIMEOUT_S
    assert media_ai_service.media_call_timeout(600) == media_ai_service.PROVIDER_CALL_SAFETY_TIMEOUT_S
    assert media_ai_service.media_call_timeout(60) == 60
    assert media_ai_service.media_call_timeout(0) == media_ai_service.PROVIDER_CALL_SAFETY_TIMEOUT_S
    assert media_ai_service.media_call_timeout("junk") == media_ai_service.PROVIDER_CALL_SAFETY_TIMEOUT_S


@pytest.mark.asyncio
async def test_an_exhausted_budget_never_starts_a_provider_call():
    provider = _ScriptedProvider("SHOULD-NOT-RUN")
    client = _FakeClient(message=_text_document())
    dispatcher, _ = _dispatcher(_manager(provider), client, fail_prompt_build=True)

    result = await dispatcher.dispatch(
        _request(reply_context=_reply_context(), timeout_s=5.0)
    )

    assert result.success is False
    assert "time budget ran out" in result.errors[0]
    assert provider.prompts == []


@pytest.mark.asyncio
async def test_a_video_this_phase_cannot_process_is_honest_and_not_transferred():
    provider = _ScriptedProvider("SHOULD-NOT-RUN")
    client = _FakeClient(message=_video())
    dispatcher, _ = _dispatcher(_manager(provider), client, fail_prompt_build=True)

    result = await dispatcher.dispatch(_request(reply_context=_reply_context(media_type="Video")))

    assert result.success is True
    assert "Video" in result.response
    assert provider.prompts == []
    assert "download_media" not in client.ops()


# ── 5. Provider architecture is untouched ──


@pytest.mark.asyncio
async def test_the_selected_provider_is_used_unchanged_with_no_provider_switching():
    provider = _ScriptedProvider("SUMMARY")
    client = _FakeClient(message=_text_document())
    manager = _manager(provider)
    dispatcher, _ = _dispatcher(manager, client, fail_prompt_build=True)

    before = manager.get_active_name()
    result = await dispatcher.dispatch(_request(reply_context=_reply_context()))

    assert before == "scripted"
    assert manager.get_active_name() == before
    assert result.provider == "scripted"
    assert result.model == "m1"
    assert len(provider.prompts) == 1


def test_the_media_ai_boundary_uses_the_plain_provider_neutral_path():
    source = inspect.getsource(media_ai_service)

    assert "manager.chat(messages, tools=[])" in source
    assert ".vision(" not in source
    assert "backend.ai.providers" not in source

    # The message list carries plain string content and nothing else.
    analysis = MediaAnalysis(
        media_type="Document", mime_type="text/plain", file_size=1,
        status=MediaStatus.EXTRACTED, content="body",
    )
    messages = media_ai_service.build_media_messages("ask", analysis)
    assert [sorted(m) for m in messages] == [["content", "role"], ["content", "role"]]
    assert all(isinstance(m["content"], str) for m in messages)
    assert messages[1]["content"] == f"ask\n\n{analysis.as_context_text()}"


# ── 6. Ordinary requests are untouched ──


@pytest.mark.asyncio
async def test_a_media_reply_that_is_not_a_media_request_type_keeps_the_normal_path():
    """A reply to a link/contact/poll message is NOT a media request."""
    provider = _ScriptedProvider("normal answer")
    client = _TrapClient()
    dispatcher, prompt_builder = _dispatcher(_manager(provider), client)

    result = await dispatcher.dispatch(_request(
        reply_context=_reply_context(media_type="WebPage"),
    ))

    assert result.success is True
    prompt_builder.build.assert_called_once()
    assert provider.prompts, "the ordinary provider path must still run"
    assert provider.prompts[0] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ]


def test_the_activation_handler_threads_the_media_type_into_the_request():
    from backend.bot.handlers import ai_unified

    source = inspect.getsource(ai_unified)
    start = source.index("        request = AIRequest(")
    assert "request_media_type=request_media_type" in source[start:start + 1200]


@pytest.mark.asyncio
async def test_the_activation_path_marks_an_owner_media_message(monkeypatch):
    """Trigger mode: the triggering message's own media is the media signal."""
    from backend.bot.handlers import ai_unified

    captured: list[str] = []

    async def _fake_execute_ai(event, owner_id, prompt_text, trigger_word, tz_str,
                               reply_context=None, client=None, config=None,
                               request_media_type=""):
        captured.append(request_media_type)

    monkeypatch.setattr(ai_unified, "_execute_ai", _fake_execute_ai)

    async def _triggers(owner_id):
        return "Nova", "", None

    monkeypatch.setattr(ai_unified, "_load_triggers", _triggers)
    handler = _capture_handler(ai_unified)

    await handler(_TriggerEvent(_photo(), raw_text="Nova this please"))
    await handler(_TriggerEvent(_FakeMessage(None, mid=REQUEST_ID), raw_text="Nova this please"))

    # A media message WITH an authored request is marked; a text message is not.
    # (A caption-less media message never activates the AI at all — no authored
    # request, no task; that is pinned by the test below.)
    assert captured == ["Photo", ""]


def _capture_handler(module: Any) -> Any:
    class _Client:
        handler = None

        def on(self, _event):
            def _decorator(fn):
                self.handler = fn
                return fn
            return _decorator

    client = _Client()
    module.register(client, owner_id=OWNER, tz_str="UTC")
    assert client.handler is not None
    return client.handler


class _TriggerEvent:
    def __init__(self, message: Any, *, raw_text: str) -> None:
        self.message = message
        self.raw_text = raw_text
        self.is_reply = False
        self.chat_id = CHAT
        self.sender_id = OWNER

    async def edit(self, text: Any = None, buttons: Any = None, **kwargs: Any) -> None:
        return None

    async def get_reply_message(self) -> Any:
        return None


@pytest.mark.asyncio
async def test_a_caption_less_media_message_never_becomes_an_ai_task(monkeypatch):
    from backend.bot.handlers import ai_unified

    calls: list[Any] = []

    async def _fake_execute_ai(*args: Any, **kwargs: Any) -> None:
        calls.append(args)

    monkeypatch.setattr(ai_unified, "_execute_ai", _fake_execute_ai)

    async def _triggers(owner_id: int):
        return "Nova", "", None

    monkeypatch.setattr(ai_unified, "_load_triggers", _triggers)

    class _Event:
        message = _FakeMessage(MessageMediaPhoto(photo=Photo(
            id=9, access_hash=9, file_reference=b"", date=None,
            sizes=[PhotoSize(type="y", w=1, h=1, size=10)], dc_id=1,
        )))
        raw_text = ""
        is_reply = False
        chat_id = CHAT
        sender_id = OWNER

        async def edit(self, text: Any = None, buttons: Any = None, **kwargs: Any) -> None:
            return None

    await _capture_handler(ai_unified)(_Event())

    assert calls == []


def test_the_classifier_taxonomy_decides_which_replies_are_media_requests():
    assert media_service.is_downloadable("Photo") is True
    assert media_service.is_downloadable("Document") is True
    assert media_service.is_downloadable("WebPage") is False
    assert media_service.is_downloadable("Location") is False
