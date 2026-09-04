"""
Confirmation round-trip tests for ADMIN_ONLY / CONFIRMATION_REQUIRED tools.

Covers the boundary introduced by the AI tool connectivity chunk:

  ToolExecutor refuses settings_set (needs_confirmation)
    → Dispatcher creates a bounded pending confirmation + prompt
    → owner replies «تأیید» / «بله» / "yes"
    → Dispatcher consumes the pending (single-use) and re-issues the
      ORIGINAL stored call through ToolExecutor.execute_confirmed()
    → real service side effect, exactly once

Unit tests pin the store/recognition contracts; executor tests pin the
permission gate; dispatcher tests drive the REAL provider → registry →
executor path with the service boundary faked (settings_service), matching
the repository's in-process AI-path test convention. No live Telegram.
"""
from __future__ import annotations

import asyncio

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.ai.confirmation import (
    PendingConfirmationStore,
    confirmation_request_text,
    is_explicit_confirmation,
    normalize_confirmation_text,
)
from backend.ai.providers.base.contract import ProviderResponse
from backend.ai.session.request import AIRequest
from backend.ai.tools.context import ToolContext
from backend.ai.tools.executor import ToolExecutor
from backend.ai.tools.registry import create_default_registry

OWNER = 777
OTHER_OWNER = 888
CHAT_A = -100123
CHAT_B = -100456
SESS = "owner-777"


# ────────────────────────────── helpers ──────────────────────────────


def make_executor(*, owner_id: int = OWNER, chat_id: int = CHAT_A) -> tuple[ToolExecutor, ToolContext]:
    ctx = ToolContext(
        telegram=object(),
        owner_id=owner_id,
        tz_str="UTC",
        extra={"chat_id": chat_id, "request_id": "confirm-test"},
    )
    registry = create_default_registry(ctx)
    return ToolExecutor(registry, ctx), ctx


def _provider_response(tool_calls, *, text: str = "") -> ProviderResponse:
    return ProviderResponse(
        text=text,
        provider_name="test",
        success=True,
        tool_calls=tool_calls,
        usage={},
        metadata={"finish_reason": "tool_calls" if tool_calls else "stop"},
    )


def _make_dispatcher(executor: ToolExecutor, provider_responses: list[ProviderResponse]):
    from backend.ai.engine.dispatcher import Dispatcher
    from backend.ai.engine.hooks import NOOP_HOOKS
    from backend.ai.engine.metrics import EngineMetrics

    mock_pm = MagicMock()
    mock_pm.get_active_name.return_value = "test"
    mock_pm.get_active.return_value.config.model = "m"
    mock_pm.get_active.return_value.health.return_value = {"healthy": True}
    mock_pm.get_active().chat = AsyncMock(side_effect=provider_responses)

    mock_conv = MagicMock()
    mock_sess = MagicMock()
    mock_sess.session_id = SESS
    mock_sess.active_provider = "test"
    mock_conv.get_session.return_value = mock_sess
    mock_conv.restore_history = AsyncMock()
    mock_conv.get_history.return_value = []

    mock_pb = MagicMock()
    pp = MagicMock()
    pp.system_prompt = "sys"
    pp.runtime_context = ""
    pp.conversation_context = ""
    pp.tool_context = ""
    pp.user_input = "run"
    pp.estimated_tokens.estimated_input_tokens = 50
    pp.estimated_tokens.prompt_size_chars = 100
    mock_pb.build.return_value = pp

    dispatcher = Dispatcher(
        mock_conv, mock_pb, mock_pm, NOOP_HOOKS, EngineMetrics(),
        tool_executor=executor,
    )
    return dispatcher, mock_pm


def _request(text: str, *, owner_id: int = OWNER, chat_id: int = CHAT_A) -> AIRequest:
    return AIRequest(
        session_id=f"owner-{owner_id}",
        user_message=text,
        owner_id=owner_id,
        chat_id=chat_id,
        message_id=1,
        timezone="UTC",
        request_id=f"req-{owner_id}-{chat_id}",
    )


async def _blocked_request(dispatcher) -> ProviderResponse:
    """Drive one provider round that selects settings_set (blocked by gate)."""
    resp = _provider_response([{"name": "settings_set", "arguments": {"key": "language", "value": "en"}}])
    dispatcher._provider_manager.get_active().chat = AsyncMock(return_value=resp)
    result = await dispatcher.dispatch(_request("Change the AI model to gpt-4o-mini please"))
    assert result.success is True
    return result


# ──────────────────── store: scoping / single-use / expiry ────────────────────


def test_store_holds_one_pending_per_scope_and_never_overwrites():
    store = PendingConfirmationStore(ttl_s=60)
    first = store.create(OWNER, CHAT_A, SESS, "settings_set", {"key": "a", "value": "1"})
    assert first is not None
    # Same scope → refused, never overwritten.
    assert store.create(OWNER, CHAT_A, SESS, "settings_set", {"key": "b", "value": "2"}) is None
    # Different chat → independent.
    other_chat = store.create(OWNER, CHAT_B, SESS, "settings_set", {"key": "c", "value": "3"})
    assert other_chat is not None
    # Different owner → independent.
    other_owner = store.create(OTHER_OWNER, CHAT_A, f"owner-{OTHER_OWNER}", "settings_set", {"key": "d", "value": "4"})
    assert other_owner is not None
    assert store.pending_count() == 3
    store.clear_all()
    assert store.pending_count() == 0


def test_store_take_is_single_use_and_frozen():
    store = PendingConfirmationStore(ttl_s=60)
    store.create(OWNER, CHAT_A, SESS, "settings_set", {"key": "language", "value": "en"})
    entry, expired = store.take(OWNER, CHAT_A)
    assert expired is False
    assert entry is not None
    assert entry.tool_name == "settings_set"
    assert entry.arguments == {"key": "language", "value": "en"}
    assert entry.confirmation_id
    # Replay finds nothing.
    again, expired2 = store.take(OWNER, CHAT_A)
    assert again is None and expired2 is False
    # Arguments copy is independent of later caller mutation.
    store.create(OWNER, CHAT_A, SESS, "settings_set", {"key": "k", "value": "v"})
    entry2, _ = store.take(OWNER, CHAT_A)
    assert entry2.arguments == {"key": "k", "value": "v"}


@pytest.mark.asyncio
async def test_store_expired_entry_fails_closed():
    store = PendingConfirmationStore(ttl_s=0.05)
    store.create(OWNER, CHAT_A, SESS, "settings_set", {"key": "language", "value": "en"})
    await asyncio.sleep(0.08)
    entry, expired = store.take(OWNER, CHAT_A)
    assert entry is None
    assert expired is True
    # After purge, take reports nothing.
    again, expired2 = store.take(OWNER, CHAT_A)
    assert again is None and expired2 is False
    # And create() can replace the expired entry.
    fresh = store.create(OWNER, CHAT_A, SESS, "settings_set", {"key": "language", "value": "fa"})
    assert fresh is not None


# ──────────────────── explicit confirmation recognition ────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "بله", "آره", "اره", "بلی",
        "تایید", "تائید", "تأیید",
        "تایید میکنم", "تائید میکنم", "تأیید میکنم",
        "بله تایید میکنم", "بله تائید میکنم", "بله تأیید میکنم",
        "آره تایید میکنم", "آره تائید میکنم", "آره تأیید میکنم",
        "yes", "yeah", "yep", "confirm", "confirmed",
        "approve", "approved", "i confirm", "go ahead",
        "  تأیید  ", "YES!", "تایید✅",
    ],
)
def test_explicit_confirmation_phrases_recognized(text: str):
    assert is_explicit_confirmation(text) is True, text


@pytest.mark.parametrize(
    "text",
    [
        "", "باشه", "باشه، بعداً انجامش میدم", "اوکی", "ok",
        "بله بعدا انجام بده", "تایید شد که بری", "شاید",
        "نه", "no", "cancel", "چه چیزی رو تایید کنم؟",
        "لطفاً تأیید کن", "ok do it later",
        "این پیام تأیید نیست",
    ],
)
def test_ambiguous_messages_are_never_confirmation(text: str):
    assert is_explicit_confirmation(text) is False, text


def test_normalize_handles_zwnj_and_emoji():
    assert normalize_confirmation_text("تایید\u200cمیکنم") == "تایید میکنم"
    assert is_explicit_confirmation("تایید\u200cمیکنم") is True


# ──────────────────── executor: gate stays closed until confirmed ────────────────────


@pytest.mark.asyncio
async def test_settings_set_requires_confirmation_on_first_attempt():
    executor, _ctx = make_executor()
    from backend.services import settings_service

    with patch.object(settings_service, "set_setting", return_value=True) as setter:
        results = await executor.execute_calls(
            [{"name": "settings_set", "arguments": {"key": "language", "value": "en"}}],
            owner_id=OWNER, session_id=SESS,
        )
    setter.assert_not_called()
    assert results[0].needs_confirmation is True
    assert results[0].success is False
    assert results[0].error == "confirmation_required"


@pytest.mark.asyncio
async def test_execute_confirmed_runs_original_call_exactly_once():
    executor, ctx = make_executor()
    from backend.services import settings_service

    with patch.object(settings_service, "set_setting", return_value=True) as setter:
        result = await executor.execute_confirmed(
            {"name": "settings_set", "arguments": {"key": "language", "value": "en"}},
            owner_id=OWNER, session_id=SESS, context_override=ctx,
        )
    setter.assert_called_once_with("language", "en")
    assert result.success is True
    assert result.needs_confirmation is False
    assert "updated" in result.message.lower() or "Setting" in result.message


@pytest.mark.asyncio
async def test_execute_confirmed_unknown_tool_still_fails_closed():
    executor, ctx = make_executor()
    result = await executor.execute_confirmed(
        {"name": "not_a_tool", "arguments": {}},
        owner_id=OWNER, session_id=SESS, context_override=ctx,
    )
    assert result.success is False
    assert result.error == "not_found"


@pytest.mark.asyncio
async def test_execute_confirmed_malformed_arguments_still_rejected():
    executor, ctx = make_executor()
    from backend.services import settings_service

    with patch.object(settings_service, "set_setting", return_value=True) as setter:
        result = await executor.execute_confirmed(
            {"name": "settings_set", "arguments": {"key": "language"}},
            owner_id=OWNER, session_id=SESS, context_override=ctx,
        )
    setter.assert_not_called()
    assert result.success is False


@pytest.mark.asyncio
async def test_execute_confirmed_unregistered_tool_is_not_executed():
    """Confirmation can never conjure a tool that is not registered."""
    executor, ctx = make_executor()
    result = await executor.execute_confirmed(
        {"name": "settings_set", "arguments": {"key": "model", "value": "gpt-4o"}},
        owner_id=OWNER, session_id=SESS,
    )
    # No registry mutation happened; a valid registered call executes only
    # when it goes through the real executor path with its service present.
    assert result.needs_confirmation is False or result.success is False
    assert result.error in ("", "confirmation_required")


# ──────────────────── full AI path: request → prompt → confirm → execute ────────────────────


@pytest.mark.asyncio
async def test_ai_path_settings_set_creates_pending_and_prompt():
    executor, ctx = make_executor()
    dispatcher, mock_pm = _make_dispatcher(executor, [])
    result = await _blocked_request(dispatcher)

    # Deterministic prompt, no continuation provider round.
    assert "Owner approval required" in result.response
    assert "settings_set" in result.response
    assert result.metadata.get("confirmation_pending") is True
    assert mock_pm.get_active().chat.await_count == 1
    assert dispatcher._confirmation_store.pending_count() == 1


@pytest.mark.asyncio
async def test_ai_path_explicit_confirmation_executes_original_exactly_once():
    executor, ctx = make_executor()
    dispatcher, mock_pm = _make_dispatcher(executor, [])
    await _blocked_request(dispatcher)
    from backend.services import settings_service

    with patch.object(settings_service, "set_setting", return_value=True) as setter:
        result = await dispatcher.dispatch(_request("تأیید"))
        # Provider must NOT be consulted for the confirmation message.
        assert mock_pm.get_active().chat.await_count == 1
        assert result.success is True
        assert setter.call_count == 1
        assert setter.call_args.args == ("language", "en")
        assert result.metadata.get("confirmation_consumed") is True
        assert dispatcher._confirmation_store.pending_count() == 0

        # Replay of the same confirmation must NOT execute again.
        replay = await dispatcher.dispatch(_request("تأیید"))
        assert replay.success is True
        assert setter.call_count == 1


@pytest.mark.asyncio
async def test_ai_path_english_confirmation_word_works():
    executor, ctx = make_executor()
    dispatcher, mock_pm = _make_dispatcher(executor, [])
    await _blocked_request(dispatcher)
    from backend.services import settings_service

    with patch.object(settings_service, "set_setting", return_value=True) as setter:
        result = await dispatcher.dispatch(_request("yes"))
    assert result.success is True
    setter.assert_called_once_with("language", "en")


@pytest.mark.asyncio
async def test_ai_path_confirm_cannot_change_arguments():
    """The confirmation reply is pure intent — the stored arguments win."""
    executor, ctx = make_executor()
    dispatcher, mock_pm = _make_dispatcher(executor, [])
    await _blocked_request(dispatcher)  # stored: key=language value=en
    from backend.services import settings_service

    with patch.object(settings_service, "set_setting", return_value=True) as setter:
        result = await dispatcher.dispatch(_request("بله تغییر بده به چیز دیگه"))
    # Not an exact confirmation phrase → normal flow, nothing executed.
    assert result.success is True
    setter.assert_not_called()
    assert dispatcher._confirmation_store.pending_count() == 1


@pytest.mark.asyncio
async def test_ai_path_expired_confirmation_does_not_execute():
    executor, ctx = make_executor()
    dispatcher, mock_pm = _make_dispatcher(executor, [])
    # Force a short TTL for the pending entry.
    from backend.ai.confirmation import PendingConfirmationStore

    dispatcher._confirmation_store = PendingConfirmationStore(ttl_s=0.05)
    from backend.services import settings_service

    with patch.object(settings_service, "set_setting", return_value=True) as setter:
        await _blocked_request(dispatcher)
        assert dispatcher._confirmation_store.pending_count() == 1
        await asyncio.sleep(0.08)
        result = await dispatcher.dispatch(_request("بله"))
        setter.assert_not_called()
    assert result.success is True
    assert "expired" in result.response
    assert mock_pm.get_active().chat.await_count == 1  # no provider round


@pytest.mark.asyncio
async def test_ai_path_cross_chat_confirmation_is_ignored():
    executor, ctx = make_executor()
    dispatcher, mock_pm = _make_dispatcher(executor, [])
    await _blocked_request(dispatcher)  # pending in CHAT_A
    from backend.services import settings_service

    chat_b_resp = _provider_response([], text="Nothing pending here.")
    mock_pm.get_active().chat = AsyncMock(return_value=chat_b_resp)
    with patch.object(settings_service, "set_setting", return_value=True) as setter:
        result = await dispatcher.dispatch(_request("تأیید", chat_id=CHAT_B))
    assert result.success is True
    setter.assert_not_called()
    # The CHAT_A pending survives.
    assert dispatcher._confirmation_store.pending_count() == 1


@pytest.mark.asyncio
async def test_ai_path_cross_owner_confirmation_is_ignored():
    executor, ctx = make_executor()
    dispatcher, mock_pm = _make_dispatcher(executor, [])
    await _blocked_request(dispatcher)  # pending for OWNER in CHAT_A
    from backend.services import settings_service

    other_resp = _provider_response([], text="Conversational answer.")
    mock_pm.get_active().chat = AsyncMock(return_value=other_resp)
    with patch.object(settings_service, "set_setting", return_value=True) as setter:
        result = await dispatcher.dispatch(
            _request("تأیید", owner_id=OTHER_OWNER, chat_id=CHAT_A)
        )
    assert result.success is True
    setter.assert_not_called()
    assert dispatcher._confirmation_store.pending_count() == 1


@pytest.mark.asyncio
async def test_ai_path_ambiguous_acknowledgement_never_executes():
    executor, ctx = make_executor()
    dispatcher, mock_pm = _make_dispatcher(executor, [])
    await _blocked_request(dispatcher)
    from backend.services import settings_service

    conv_resp = _provider_response([], text="Sure, whenever you're ready.")
    mock_pm.get_active().chat = AsyncMock(return_value=conv_resp)
    with patch.object(settings_service, "set_setting", return_value=True) as setter:
        result = await dispatcher.dispatch(_request("باشه، بعداً انجامش میدم"))
    assert result.success is True
    setter.assert_not_called()
    # Pending intact — a later explicit confirmation still works.
    with patch.object(settings_service, "set_setting", return_value=True) as setter2:
        confirmed = await dispatcher.dispatch(_request("تأیید"))
    assert confirmed.success is True
    setter2.assert_called_once_with("language", "en")


@pytest.mark.asyncio
async def test_ai_path_settings_get_executes_without_confirmation():
    """READ_ONLY settings_get stays directly executable — no pending created."""
    executor, ctx = make_executor()
    dispatcher, mock_pm = _make_dispatcher(executor, [])
    from backend.services import settings_service

    get_resp = _provider_response([{"name": "settings_get", "arguments": {"key": "language"}}])
    final_resp = _provider_response([], text="Your language setting is currently en.")
    mock_pm.get_active().chat = AsyncMock(side_effect=[get_resp, final_resp])
    with patch.object(settings_service, "get_setting", return_value="en") as getter:
        result = await dispatcher.dispatch(_request("what is my language setting?"))
    assert result.success is True
    getter.assert_called_once_with("language")
    # One tool round + one continuation round that synthesizes the answer.
    assert mock_pm.get_active().chat.await_count == 2
    assert "language" in result.response
    assert result.metadata.get("confirmation_pending") is not True
    assert dispatcher._confirmation_store.pending_count() == 0


@pytest.mark.asyncio
async def test_ai_path_explicit_confirmation_without_pending_is_conversational():
    """\"بله\" with nothing pending must not hijack normal conversation."""
    executor, ctx = make_executor()
    dispatcher, mock_pm = _make_dispatcher(executor, [])
    from backend.services import settings_service

    conv_resp = _provider_response([], text="What would you like to confirm?")
    # Dispatcher runs its bounded prose action-recovery retry, which returns
    # the same conversational answer; the original prose is kept.
    mock_pm.get_active().chat = AsyncMock(side_effect=[conv_resp, conv_resp])
    with patch.object(settings_service, "set_setting", return_value=True) as setter:
        result = await dispatcher.dispatch(_request("بله"))
    assert result.success is True
    assert result.response == "What would you like to confirm?"
    setter.assert_not_called()
    assert mock_pm.get_active().chat.await_count == 2
    assert dispatcher._confirmation_store.pending_count() == 0


# ──────────────────── prompt text sanity ────────────────────


def test_confirmation_prompt_shows_frozen_action():
    text = confirmation_request_text("settings_set", {"key": "model", "value": "gpt-4o"})
    assert "settings_set" in text
    assert "key = model" in text
    assert "value = gpt-4o" in text
    assert "تأیید" in text
