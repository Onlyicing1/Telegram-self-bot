"""Auto-Reaction engine — Emoji Stage 3 (Emoji & Reaction, ROADMAP §27).

Pins the engine end to end, offline:

* eligibility — only owner-authored outgoing messages are reacted to; an
  incoming message, a foreign author, a message with no usable ids, an
  inline-bot origin and a pending panel-input message are skipped;
* the enabled toggle controls real execution — OFF sends nothing, ON sends
  the SELECTED reaction exactly once through the existing reaction service;
* a missing/invalid selected reaction is a safe no-op (never a fallback);
* deduplication — a duplicate ``NewMessage`` for the same message id does
  not react twice;
* failures — a Telegram error/rate-limit/foreign-target rejection becomes an
  honest ``failed`` outcome and never breaks the loop;
* configuration changes are honored on the next message (no restart, no
  second client);
* architecture — one outgoing handler on the existing update path, no second
  client/loop/scheduler/executor, no ``backend.ai`` import, no second
  reaction send path.

Everything runs offline: the Telegram surface is faked at the client
``__call__`` boundary the typed reaction wrapper already consumes. No live
Telegram behavior is claimed anywhere.
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from telethon.errors import FloodWaitError
from telethon.tl.functions.messages import SendReactionRequest
from telethon.tl.types import ReactionEmoji

from backend.bot.handlers import auto_reaction
from backend.helper import input_state
from backend.services import auto_reaction_service as engine
from backend.services import settings_service
from backend.telegram_api._helpers import serialize_message

OWNER = 424242
OTHER = 991199

CHAT = -1001234567890
CHAT2 = -1009998887771

MSG = 555
MSG2 = 556

EMOJI = "👍"


@pytest.fixture(autouse=True)
def _env():
    before = dict(settings_service.get_all())
    settings_service.load_all()
    settings_service._cache["auto_reaction_enabled"] = False
    settings_service._cache["auto_reaction_emoji"] = ""
    input_state.clear_all()
    engine.reset_loop_guard()
    yield
    input_state.clear_all()
    engine.reset_loop_guard()
    settings_service._cache.clear()
    settings_service._cache.update(before)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── telegram-level fakes (the surface the typed wrapper consumes) ────────────


class _FakeSelfClient:
    """Self-client fake: the message read the reaction service performs plus
    the raw TL request surface (``__call__``) the typed wrapper uses."""

    def __init__(
        self,
        messages: dict[tuple[int, int], Any] | None = None,
        *,
        call_error: Exception | None = None,
    ) -> None:
        self._messages = dict(messages or {})
        self.call_error = call_error
        self.calls: list[Any] = []

    def add(self, chat_id: int, msg_id: int, message: Any) -> None:
        self._messages[(chat_id, msg_id)] = message

    async def get_messages(self, chat_id, ids=None):
        if isinstance(ids, list):
            return [self._messages.get((chat_id, i)) for i in ids]
        return self._messages.get((chat_id, ids))

    async def __call__(self, request):
        self.calls.append(request)
        if self.call_error is not None:
            raise self.call_error
        return SimpleNamespace(updates=[])

    @property
    def reaction_calls(self) -> list[Any]:
        return [c for c in self.calls if isinstance(c, SendReactionRequest)]


def _target_message(msg_id: int = MSG, *, chat_id: int = CHAT) -> SimpleNamespace:
    return SimpleNamespace(
        id=msg_id,
        chat_id=chat_id,
        sender_id=OWNER,
        text="hi",
        message="hi",
        date=None,
        media=None,
        reply_to=None,
        reply_to_msg_id=None,
        out=True,
        entities=[],
    )


def _client_with_target(msg_id: int = MSG, *, chat_id: int = CHAT, **kwargs) -> _FakeSelfClient:
    client = _FakeSelfClient(**kwargs)
    client.add(chat_id, msg_id, _target_message(msg_id, chat_id=chat_id))
    return client


def _outgoing(
    msg_id: int = MSG,
    *,
    chat_id: int = CHAT,
    sender_id: int = OWNER,
    out: bool = True,
    via_bot_id: int | None = None,
    text: str = "hello",
):
    return SimpleNamespace(
        id=msg_id,
        chat_id=chat_id,
        sender_id=sender_id,
        text=text,
        message=text,
        date=None,
        media=None,
        reply_to=None,
        out=out,
        entities=[],
        via_bot_id=via_bot_id,
    )


async def _process(client, message, *, owner_id: int = OWNER):
    return await engine.process_outgoing_message(
        owner_id=owner_id,
        client=client,
        message=serialize_message(message),
        via_bot_id=getattr(message, "via_bot_id", None),
    )


def _enable(emoji: str = EMOJI) -> None:
    settings_service.set_auto_reaction_enabled(True)
    settings_service.set_auto_reaction_emoji(emoji)


# ── 1. enabled + eligible → exactly one reaction, the selected value ─────────


def test_enabled_eligible_message_reacts_once_with_the_selected_value():
    client = _client_with_target()
    _enable(EMOJI)
    result = _run(_process(client, _outgoing()))
    assert result["status"] == engine.STATUS_REACTED
    assert result["reaction"] == {"kind": "emoji", "emoji": EMOJI}
    assert len(client.reaction_calls) == 1
    request = client.reaction_calls[0]
    assert request.msg_id == MSG
    assert isinstance(request.reaction[0], ReactionEmoji)
    assert request.reaction[0].emoticon == EMOJI


def test_the_exact_selected_emoji_is_used_not_a_hardcoded_one():
    client = _client_with_target()
    _enable("🔥")
    _run(_process(client, _outgoing()))
    assert client.reaction_calls[0].reaction[0].emoticon == "🔥"


# ── 2. disabled → nothing sent ───────────────────────────────────────────────


def test_disabled_sends_nothing():
    client = _client_with_target()
    settings_service.set_auto_reaction_enabled(False)
    settings_service.set_auto_reaction_emoji(EMOJI)
    result = _run(_process(client, _outgoing()))
    assert result["status"] == engine.STATUS_DISABLED
    assert client.reaction_calls == []


# ── 3. incoming message → nothing sent ───────────────────────────────────────


def test_incoming_message_is_not_reacted_to():
    client = _client_with_target()
    _enable(EMOJI)
    result = _run(_process(client, _outgoing(out=False)))
    assert result["status"] == engine.STATUS_NOT_OWNER
    assert client.reaction_calls == []


# ── 4. not authored by the current account → nothing sent ────────────────────


def test_foreign_author_is_not_reacted_to():
    client = _client_with_target()
    _enable(EMOJI)
    result = _run(_process(client, _outgoing(sender_id=OTHER, out=True)))
    assert result["status"] == engine.STATUS_NOT_OWNER
    assert client.reaction_calls == []


def test_inline_bot_origin_is_not_reacted_to():
    client = _client_with_target()
    _enable(EMOJI)
    result = _run(_process(client, _outgoing(via_bot_id=777000)))
    assert result["status"] == engine.STATUS_INLINE_ORIGIN
    assert client.reaction_calls == []


def test_pending_panel_input_is_not_reacted_to():
    client = _client_with_target()
    _enable(EMOJI)
    input_state.set_pending(OWNER, "panel_x", _noop_handler, CHAT, "prompt")
    result = _run(_process(client, _outgoing()))
    assert result["status"] == engine.STATUS_PENDING_INPUT
    assert client.reaction_calls == []


def test_a_message_without_usable_ids_is_skipped():
    client = _client_with_target()
    _enable(EMOJI)
    result = _run(engine.process_outgoing_message(
        owner_id=OWNER, client=client, message={"out": True, "sender_id": OWNER},
    ))
    assert result["status"] == engine.STATUS_INVALID
    assert client.reaction_calls == []


async def _noop_handler(*args, **kwargs):
    return None


# ── 5. missing selected reaction → safe no-op ────────────────────────────────


def test_enabled_but_no_reaction_selected_is_a_safe_no_op():
    client = _client_with_target()
    settings_service.set_auto_reaction_enabled(True)
    settings_service.set_auto_reaction_emoji("")
    result = _run(_process(client, _outgoing()))
    assert result["status"] == engine.STATUS_NO_REACTION
    assert client.reaction_calls == []


def test_an_invalid_stored_reaction_is_a_safe_no_op():
    client = _client_with_target()
    settings_service.set_auto_reaction_enabled(True)
    # Bypass the validator to prove the engine itself fails closed.
    settings_service._cache["auto_reaction_emoji"] = "not an emoji \n"
    result = _run(_process(client, _outgoing()))
    assert result["status"] == engine.STATUS_NO_REACTION
    assert client.reaction_calls == []


# ── 6. duplicate update → no duplicate reaction ──────────────────────────────


def test_duplicate_update_does_not_react_twice():
    client = _client_with_target()
    _enable(EMOJI)
    first = _run(_process(client, _outgoing()))
    second = _run(_process(client, _outgoing()))
    assert first["status"] == engine.STATUS_REACTED
    assert second["status"] == engine.STATUS_DUPLICATE
    assert len(client.reaction_calls) == 1


def test_a_second_distinct_message_is_reacted_to():
    client = _client_with_target(MSG)
    client.add(CHAT, MSG2, _target_message(MSG2))
    _enable(EMOJI)
    _run(_process(client, _outgoing(MSG)))
    _run(_process(client, _outgoing(MSG2)))
    assert len(client.reaction_calls) == 2


# ── 7. Telegram error / rate limit → bounded, safe ───────────────────────────


def test_a_telegram_rejection_is_reported_honestly_and_not_retried():
    client = _client_with_target(call_error=FloodWaitError(request=None, capture=30))
    _enable(EMOJI)
    result = _run(_process(client, _outgoing()))
    assert result["status"] == engine.STATUS_FAILED
    assert result["error"]
    # One attempt only — no retry storm.
    assert len(client.reaction_calls) == 1


def test_a_failure_does_not_raise_out_of_the_service():
    client = _client_with_target(call_error=RuntimeError("boom"))
    _enable(EMOJI)
    result = _run(_process(client, _outgoing()))  # must not raise
    assert result["status"] == engine.STATUS_FAILED


def test_a_rejected_message_is_not_retried_on_a_duplicate_update():
    client = _client_with_target(call_error=FloodWaitError(request=None, capture=30))
    _enable(EMOJI)
    _run(_process(client, _outgoing()))
    second = _run(_process(client, _outgoing()))
    assert second["status"] == engine.STATUS_DUPLICATE
    assert len(client.reaction_calls) == 1


# ── 8. configuration changes are honored ─────────────────────────────────────


def test_toggling_off_takes_effect_on_the_next_message():
    client = _client_with_target()
    _enable(EMOJI)
    _run(_process(client, _outgoing(MSG)))
    settings_service.set_auto_reaction_enabled(False)
    result = _run(_process(client, _outgoing(MSG2)))
    assert result["status"] == engine.STATUS_DISABLED
    assert len(client.reaction_calls) == 1


def test_changing_the_selected_reaction_takes_effect_on_the_next_message():
    client = _client_with_target(MSG)
    client.add(CHAT, MSG2, _target_message(MSG2))
    _enable("👍")
    _run(_process(client, _outgoing(MSG)))
    settings_service.set_auto_reaction_emoji("🎉")
    _run(_process(client, _outgoing(MSG2)))
    assert client.reaction_calls[0].reaction[0].emoticon == "👍"
    assert client.reaction_calls[1].reaction[0].emoticon == "🎉"


# ── 9. handler + architecture ────────────────────────────────────────────────


class _CapturingClient:
    def __init__(self, inner: Any = None) -> None:
        self.handlers: list[tuple[Any, Any]] = []
        self._inner = inner

    def on(self, builder):
        def _decorator(callback):
            self.handlers.append((builder, callback))
            return callback

        return _decorator

    def __getattr__(self, name):
        inner = self.__dict__.get("_inner")
        if inner is None:
            raise AttributeError(name)
        return getattr(inner, name)


class _FakeEvent:
    def __init__(self, message) -> None:
        self.message = message
        self.sender_id = message.sender_id
        self.chat_id = message.chat_id
        self.raw_text = message.text


def _registered_handler():
    client = _CapturingClient()
    auto_reaction.register(client, OWNER)
    assert len(client.handlers) == 1
    return client.handlers[0]


def test_handler_registers_exactly_one_outgoing_listener():
    builder, _callback = _registered_handler()
    assert builder.outgoing is True
    assert builder.incoming is False


def test_handler_is_the_only_telegram_listener_in_the_module():
    source = Path(auto_reaction.__file__).read_text()
    assert source.count("@client.on(events.NewMessage(outgoing=True))") == 1
    for forbidden in ("events.MessageEdited", "events.Raw", "events.CallbackQuery", "TelegramClient"):
        assert forbidden not in source, forbidden


def test_handler_ignores_non_owner_events(monkeypatch):
    _builder, callback = _registered_handler()
    calls: list[Any] = []

    async def _record(**kwargs):
        calls.append(kwargs)
        return {}

    monkeypatch.setattr(auto_reaction, "process_outgoing_message", _record)
    _run(callback(_FakeEvent(_outgoing(sender_id=OTHER))))
    assert calls == []


def test_handler_runs_owner_messages_through_the_service(monkeypatch):
    _builder, callback = _registered_handler()
    calls: list[dict] = []

    async def _record(**kwargs):
        calls.append(kwargs)
        return {}

    monkeypatch.setattr(auto_reaction, "process_outgoing_message", _record)
    message = _outgoing(via_bot_id=None)
    _run(callback(_FakeEvent(message)))
    assert len(calls) == 1
    assert calls[0]["owner_id"] == OWNER
    assert calls[0]["message"]["id"] == MSG
    assert calls[0]["message"]["chat_id"] == CHAT


def test_handler_swallows_service_errors(monkeypatch):
    _builder, callback = _registered_handler()

    async def _boom(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(auto_reaction, "process_outgoing_message", _boom)
    _run(callback(_FakeEvent(_outgoing())))  # must not raise


def test_handler_propagates_cancellation(monkeypatch):
    _builder, callback = _registered_handler()

    async def _cancel(**kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(auto_reaction, "process_outgoing_message", _cancel)
    with pytest.raises(asyncio.CancelledError):
        _run(callback(_FakeEvent(_outgoing())))


def test_router_registers_the_auto_reaction_handler():
    from backend.bot import router

    source = Path(router.__file__).read_text()
    assert '"auto_reaction", lambda: auto_reaction.register(client, owner_id)' in source
    # Registered exactly once, before the replacement pipeline.
    assert source.count('"auto_reaction"') == 1


def _imports_of(module_path: str) -> list[str]:
    tree = ast.parse(Path(module_path).read_text())
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def test_the_engine_does_not_import_the_ai_or_a_second_client():
    for module in (engine.__file__, auto_reaction.__file__):
        for name in _imports_of(module):
            assert not name.startswith("backend.ai"), name
            assert "TelegramClient" not in name, name


def test_the_engine_has_no_scheduler_or_executor_of_its_own():
    tree = ast.parse(Path(engine.__file__).read_text())
    # Strip docstrings so prose references don't count as code.
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                node.body = body[1:]
    source = ast.unparse(tree)
    for forbidden in (
        "asyncio.create_task", "ensure_future", "call_later",
        "ThreadPoolExecutor", "TelegramClient", "events.NewMessage",
    ):
        assert forbidden not in source, forbidden


# ── 10. existing behavior stays unaffected ───────────────────────────────────


def test_engine_leaves_the_replacement_state_alone():
    from backend.db import client as db_client

    db_client._fallback["emoji_state"] = {}
    client = _client_with_target()
    _enable(EMOJI)
    _run(_process(client, _outgoing()))
    assert db_client._fallback["emoji_state"] == {}


def test_the_dedup_registry_is_bounded():
    _enable(EMOJI)
    client = _client_with_target()
    for i in range(engine.GUARD_MAX + 50):
        client.add(CHAT, 10000 + i, _target_message(10000 + i))
        _run(_process(client, _outgoing(10000 + i)))
    assert len(engine._handled) <= engine.GUARD_MAX
