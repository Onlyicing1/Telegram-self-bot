"""Intelligent Reaction — AI-driven per-message reactions on outgoing messages.

Pins the contract end to end, offline:

* eligibility — only owner-authored outgoing messages with text are evaluated;
  an incoming message, a foreign author, an inline-bot origin, a pending panel
  input, an unusable id pair and a message with no text are skipped;
* per-message probability — every message carries its OWN probability from the
  AI and its OWN independent random draw (0%, 100% and mid-range boundaries),
  with no fixed ratio, quota, rolling window or compensation for misses;
* semantic selection — the AI's chosen reaction is applied exactly, from
  Telegram's catalog, to the owner's OWN outgoing message (a replied-to message
  is context only and is never the target);
* no-reaction decisions and unsupported reactions — nothing is sent;
* context isolation — the model receives the message text and at most one
  replied-to text, never ids, sender identity or history;
* failure handling — a provider failure, invalid JSON, an out-of-range
  probability or a timeout sends nothing and never falls back to a fixed emoji;
* mutual exclusion with the fixed Auto-Reaction setting, in both directions;
* architecture — one outgoing listener on the existing update path, no second
  client/loop/scheduler/executor, no direct provider import (the existing
  ProviderManager is the only route).

Everything runs offline: Telegram is faked at the client ``__call__`` boundary
the typed reaction wrapper consumes, the reaction catalog is a fake, and the AI
is a scripted ``ProviderManager``-shaped object.
"""
from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from telethon.tl.functions.messages import SendReactionRequest

from backend.ai.providers.base import ProviderResponse
from backend.bot.handlers import intelligent_reaction
from backend.helper import input_state
from backend.services import intelligent_reaction_service as engine
from backend.services import reaction_source_service
from backend.services import settings_service
from backend.telegram_api._helpers import serialize_message

OWNER = 424242
OTHER = 991199

CHAT = -1001234567890
MSG = 555
MSG2 = 556
REPLIED = 900

CATALOG = ("👍", "❤", "🎉", "🔥", "😢")
MIGRATION = (
    Path(engine.__file__).resolve().parent.parent.parent
    / "supabase" / "migrations"
    / "20261112140000_add_panel_settings_intelligent_reaction.sql"
)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    before = dict(settings_service.get_all())
    settings_service.load_all()
    for key, value in (
        ("auto_reaction_enabled", False),
        ("auto_reaction_emoji", ""),
        ("intelligent_reaction_enabled", False),
    ):
        settings_service._cache[key] = value
    input_state.clear_all()
    engine.reset_loop_guard()
    reaction_source_service.reset_cache()

    async def _catalog(client, *, force: bool = False):
        return {
            "ok": True,
            "error": None,
            "detail": None,
            "cached": True,
            "options": [
                {
                    "emoji": emoji,
                    "reaction": {"kind": "emoji", "emoji": emoji},
                    "title": "",
                    "premium": False,
                }
                for emoji in CATALOG
            ],
        }

    monkeypatch.setattr(reaction_source_service, "load_reactions", _catalog)
    yield
    input_state.clear_all()
    engine.reset_loop_guard()
    settings_service._cache.clear()
    settings_service._cache.update(before)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _enable() -> None:
    settings_service._cache["intelligent_reaction_enabled"] = True


# ── Telegram + AI fakes ──────────────────────────────────────────────────────


class _Client:
    """Self-client fake: the message read the reaction service performs plus the
    raw TL request surface (``__call__``) the typed wrapper uses."""

    def __init__(self) -> None:
        self.messages: dict[tuple[int, int], Any] = {}
        self.calls: list[Any] = []

    def add(
        self,
        msg_id: int,
        *,
        chat_id: int = CHAT,
        text: str = "hi",
        sender_id: int = OWNER,
        out: bool = True,
        reply_to: int | None = None,
    ) -> None:
        self.messages[(chat_id, msg_id)] = SimpleNamespace(
            id=msg_id,
            chat_id=chat_id,
            sender_id=sender_id,
            text=text,
            message=text,
            date=None,
            media=None,
            reply_to=(SimpleNamespace(reply_to_msg_id=reply_to) if reply_to else None),
            out=out,
            entities=[],
            via_bot_id=None,
        )

    async def get_messages(self, chat_id, ids=None):
        if isinstance(ids, list):
            return [self.messages.get((chat_id, i)) for i in ids]
        return self.messages.get((chat_id, ids))

    async def __call__(self, request):
        self.calls.append(request)
        return SimpleNamespace(updates=[])

    @property
    def reaction_calls(self) -> list[Any]:
        return [c for c in self.calls if isinstance(c, SendReactionRequest)]


class _Provider:
    """ProviderManager-shaped fake: scripted answers, recorded calls."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        if not self.answers:
            raise AssertionError("unexpected extra AI call")
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class _SlowProvider:
    async def chat(self, messages, **kwargs):
        await asyncio.sleep(0.2)
        return ProviderResponse(text='{"probability": 100}', provider_name="fake", success=True)


def _ok(probability: Any, reaction: Any = None) -> ProviderResponse:
    return ProviderResponse(
        text=json.dumps({"probability": probability, "reaction": reaction}),
        provider_name="fake",
        success=True,
    )


def _raw(text: str, *, success: bool = True) -> ProviderResponse:
    return ProviderResponse(text=text, provider_name="fake", success=success)


def _outgoing(
    msg_id: int = MSG,
    text: str = "hello",
    *,
    reply_to: int | None = None,
    chat_id: int = CHAT,
    sender_id: int = OWNER,
    out: bool = True,
    via_bot_id: int | None = None,
):
    return SimpleNamespace(
        id=msg_id,
        chat_id=chat_id,
        sender_id=sender_id,
        text=text,
        message=text,
        date=None,
        media=None,
        reply_to=(SimpleNamespace(reply_to_msg_id=reply_to) if reply_to else None),
        out=out,
        entities=[],
        via_bot_id=via_bot_id,
    )


async def _process(client, message, provider, *, rng: Any = 0.0):
    draw = rng if callable(rng) else (lambda: rng)
    return await engine.process_outgoing_message(
        owner_id=OWNER,
        client=client,
        message=serialize_message(message),
        via_bot_id=getattr(message, "via_bot_id", None),
        provider_manager=provider,
        random_fn=draw,
    )


def _client(msg_id: int = MSG, *, text: str = "hello", reply_to: int | None = None) -> _Client:
    client = _Client()
    client.add(msg_id, text=text, reply_to=reply_to)
    return client


# ── 1. per-message probability ───────────────────────────────────────────────


def test_each_message_gets_its_own_probability_and_decision():
    client = _Client()
    client.add(MSG, text="first")
    client.add(MSG2, text="second")
    provider = _Provider(_ok(10, "👍"), _ok(90, "🎉"))
    _enable()

    first = _run(_process(client, _outgoing(MSG, "first"), provider, rng=0.5))
    second = _run(_process(client, _outgoing(MSG2, "second"), provider, rng=0.5))

    assert first["status"] == engine.STATUS_NOT_SELECTED
    assert first["probability"] == 10
    assert second["status"] == engine.STATUS_REACTED
    assert second["probability"] == 90
    assert len(provider.calls) == 2
    contents = [call["messages"][1]["content"] for call in provider.calls]
    assert "first" in contents[0] and "second" in contents[1]
    assert len(client.reaction_calls) == 1
    assert client.reaction_calls[0].msg_id == MSG2


def test_zero_percent_never_reacts_even_with_the_lowest_draw():
    client = _client()
    provider = _Provider(_ok(0, "👍"))
    _enable()
    result = _run(_process(client, _outgoing(), provider, rng=0.0))
    assert result["status"] == engine.STATUS_NOT_SELECTED
    assert result["probability"] == 0
    assert client.reaction_calls == []


def test_hundred_percent_always_reacts():
    client = _client()
    provider = _Provider(_ok(100, "🔥"))
    _enable()
    result = _run(_process(client, _outgoing(), provider, rng=0.999999))
    assert result["status"] == engine.STATUS_REACTED
    assert len(client.reaction_calls) == 1


def test_probability_boundaries_are_exact():
    assert engine.passes_probability(0, 0.0) is False
    assert engine.passes_probability(100, 0.999999) is True
    assert engine.passes_probability(50, 0.4999) is True
    assert engine.passes_probability(50, 0.5) is False
    assert engine.passes_probability(12.5, 0.1249) is True
    assert engine.passes_probability(12.5, 0.125) is False


def test_random_checks_are_independent_per_message_with_no_quota():
    client = _Client()
    provider = _Provider(*[_ok(50, "👍") for _ in range(4)])
    _enable()
    draws = iter([0.9, 0.1, 0.9, 0.1])
    statuses = []
    for index in range(4):
        msg_id = 1000 + index
        client.add(msg_id, text=f"m{index}")
        statuses.append(
            _run(
                _process(
                    client,
                    _outgoing(msg_id, f"m{index}"),
                    provider,
                    rng=lambda: next(draws),
                )
            )["status"]
        )
    assert statuses == [
        engine.STATUS_NOT_SELECTED,
        engine.STATUS_REACTED,
        engine.STATUS_NOT_SELECTED,
        engine.STATUS_REACTED,
    ]
    assert [call.msg_id for call in client.reaction_calls] == [1001, 1003]


def test_no_compensation_and_no_rolling_window():
    client = _Client()
    provider = _Provider(*[_ok(100, "👍") for _ in range(6)])
    _enable()
    statuses = []
    for index in range(6):
        msg_id = 2000 + index
        client.add(msg_id, text=f"m{index}")
        statuses.append(_run(_process(client, _outgoing(msg_id, f"m{index}"), provider))["status"])
    assert statuses == [engine.STATUS_REACTED] * 6
    assert len(client.reaction_calls) == 6

    # An earlier decision never suppresses or compensates a later message.
    client.add(3000, text="declined")
    provider.answers.append(_ok(0, "👍"))
    declined = _run(_process(client, _outgoing(3000, "declined"), provider))
    assert declined["status"] == engine.STATUS_NOT_SELECTED
    assert len(client.reaction_calls) == 6


def _code_only(path: str) -> str:
    tree = ast.parse(Path(path).read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                node.body = body[1:]
    return ast.unparse(tree)


def test_no_fixed_ratio_or_quota_exists_in_the_decision_code():
    code = _code_only(engine.__file__).lower()
    for forbidden in ("0.25", "quota", "every_fourth", "every fourth", "rolling", "window"):
        assert forbidden not in code, forbidden


# ── 2. semantic selection + target ───────────────────────────────────────────


def test_semantic_reaction_selection_uses_the_exact_catalog_emoji():
    client = _client()
    provider = _Provider(_ok(100, "🎉"))
    _enable()
    result = _run(_process(client, _outgoing(), provider))
    assert result["status"] == engine.STATUS_REACTED
    request = client.reaction_calls[0]
    assert request.msg_id == MSG
    assert request.peer == CHAT
    assert request.reaction[0].emoticon == "🎉"


def test_the_reaction_target_is_always_the_owners_outgoing_message():
    client = _Client()
    client.add(MSG, text="my reply", reply_to=REPLIED)
    client.add(REPLIED, text="someone else's question", sender_id=OTHER, out=False)
    provider = _Provider(_ok(100, "👍"))
    _enable()
    result = _run(_process(client, _outgoing(MSG, "my reply", reply_to=REPLIED), provider))
    assert result["status"] == engine.STATUS_REACTED
    request = client.reaction_calls[0]
    assert request.msg_id == MSG
    assert request.msg_id != REPLIED
    assert request.peer == CHAT


def test_no_reaction_decision_sends_nothing():
    client = _client()
    provider = _Provider(_ok(80, None))
    _enable()
    result = _run(_process(client, _outgoing(), provider))
    assert result["status"] == engine.STATUS_NO_REACTION
    assert result["reaction"] is None
    assert len(provider.calls) == 1
    assert client.reaction_calls == []


def test_unsupported_reaction_is_rejected_and_nothing_is_sent():
    client = _client()
    provider = _Provider(_ok(100, "🦄"))
    _enable()
    result = _run(_process(client, _outgoing(), provider))
    assert result["status"] == engine.STATUS_FAILED
    assert "catalog" in (result["error"] or "")
    assert client.reaction_calls == []


# ── 3. context isolation ─────────────────────────────────────────────────────


def test_model_receives_only_the_message_and_one_replied_to_text():
    client = _Client()
    client.add(MSG, text="my answer", reply_to=REPLIED)
    client.add(REPLIED, text="the incoming question", sender_id=OTHER, out=False)
    provider = _Provider(_ok(100, "👍"))
    _enable()
    _run(_process(client, _outgoing(MSG, "my answer", reply_to=REPLIED), provider))

    call = provider.calls[0]
    assert len(call["messages"]) == 2
    assert call["messages"][0]["role"] == "system"
    content = call["messages"][1]["content"]
    assert "my answer" in content
    assert "the incoming question" in content
    for forbidden in (str(CHAT), str(MSG), str(REPLIED), str(OWNER), str(OTHER)):
        assert forbidden not in content, forbidden


def test_no_reply_means_no_context_section_and_no_history():
    client = _client()
    provider = _Provider(_ok(100, "👍"))
    _enable()
    _run(_process(client, _outgoing(), provider))
    content = provider.calls[0]["messages"][1]["content"]
    assert "Replied-to" not in content
    assert len(provider.calls[0]["messages"]) == 2


def test_replied_to_context_is_bounded():
    client = _Client()
    client.add(MSG, text="short", reply_to=REPLIED)
    client.add(REPLIED, text="x" * 5000, sender_id=OTHER, out=False)
    provider = _Provider(_ok(100, "👍"))
    _enable()
    _run(_process(client, _outgoing(MSG, "short", reply_to=REPLIED), provider))
    content = provider.calls[0]["messages"][1]["content"]
    assert "x" * (engine.MAX_CONTEXT_CHARS + 1) not in content


# ── 4. failure handling ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "answer",
    [
        _raw("", success=False),
        _raw("not json at all"),
        _raw('{"reaction": "👍"}'),
        RuntimeError("provider exploded"),
    ],
)
def test_ai_failure_never_reacts(answer):
    client = _client()
    provider = _Provider(answer)
    _enable()
    result = _run(_process(client, _outgoing(), provider))
    assert result["status"] == engine.STATUS_FAILED
    assert client.reaction_calls == []


@pytest.mark.parametrize("probability", [-1, 101, "50", True, None])
def test_invalid_probability_is_rejected(probability):
    client = _client()
    provider = _Provider(_ok(probability, "👍"))
    _enable()
    result = _run(_process(client, _outgoing(), provider))
    assert result["status"] == engine.STATUS_FAILED
    assert client.reaction_calls == []


def test_a_timeout_sends_nothing(monkeypatch):
    monkeypatch.setattr(engine, "AI_TIMEOUT_S", 0.01)
    client = _client()
    _enable()
    result = _run(_process(client, _outgoing(), _SlowProvider()))
    assert result["status"] == engine.STATUS_FAILED
    assert "timed out" in (result["error"] or "")
    assert client.reaction_calls == []


def test_no_provider_manager_is_an_honest_failure():
    client = _client()
    _enable()
    result = _run(_process(client, _outgoing(), None))
    assert result["status"] == engine.STATUS_FAILED
    assert client.reaction_calls == []


def test_catalog_unavailable_sends_nothing_and_never_calls_the_ai(monkeypatch):
    async def _down(client, *, force: bool = False):
        return {"ok": False, "error": "E_TELEGRAM", "detail": "down", "options": []}

    monkeypatch.setattr(reaction_source_service, "load_reactions", _down)
    client = _client()
    provider = _Provider(_ok(100, "👍"))
    _enable()
    result = _run(_process(client, _outgoing(), provider))
    assert result["status"] == engine.STATUS_CATALOG_UNAVAILABLE
    assert provider.calls == []
    assert client.reaction_calls == []


# ── 5. eligibility ──────────────────────────────────────────────────────────


def test_disabled_sends_nothing_and_never_calls_the_ai():
    client = _client()
    provider = _Provider(_ok(100, "👍"))
    result = _run(_process(client, _outgoing(), provider))
    assert result["status"] == engine.STATUS_DISABLED
    assert provider.calls == []
    assert client.reaction_calls == []


def test_incoming_foreign_inline_and_pending_input_are_skipped():
    _enable()
    provider = _Provider()
    client = _client()

    assert _run(_process(client, _outgoing(out=False), provider))["status"] == engine.STATUS_NOT_OWNER
    assert _run(
        _process(client, _outgoing(MSG2, sender_id=OTHER), provider)
    )["status"] == engine.STATUS_NOT_OWNER
    assert _run(
        _process(client, _outgoing(MSG2, via_bot_id=777000), provider)
    )["status"] == engine.STATUS_INLINE_ORIGIN

    input_state.set_pending(OWNER, "panel_x", _noop_handler, CHAT, "prompt")
    assert _run(_process(client, _outgoing(MSG2), provider))["status"] == engine.STATUS_PENDING_INPUT

    assert provider.calls == []
    assert client.reaction_calls == []


async def _noop_handler(*args, **kwargs):
    return None


def test_invalid_message_shape_is_skipped():
    _enable()
    result = _run(
        engine.process_outgoing_message(
            owner_id=OWNER,
            client=_client(),
            message={"out": True, "sender_id": OWNER},
            provider_manager=_Provider(),
        )
    )
    assert result["status"] == engine.STATUS_INVALID


def test_a_message_without_text_is_not_evaluated():
    client = _client()
    provider = _Provider(_ok(100, "👍"))
    _enable()
    result = _run(_process(client, _outgoing(text=""), provider))
    assert result["status"] == engine.STATUS_NO_TEXT
    assert provider.calls == []
    assert client.reaction_calls == []


def test_duplicate_update_reacts_once():
    client = _client()
    provider = _Provider(_ok(100, "👍"))
    _enable()
    first = _run(_process(client, _outgoing(), provider))
    second = _run(_process(client, _outgoing(), provider))
    assert first["status"] == engine.STATUS_REACTED
    assert second["status"] == engine.STATUS_DUPLICATE
    assert len(provider.calls) == 1
    assert len(client.reaction_calls) == 1


def test_the_dedup_registry_is_bounded():
    _enable()
    client = _Client()
    provider = _Provider(*[_ok(0, None) for _ in range(engine.GUARD_MAX + 20)])
    for index in range(engine.GUARD_MAX + 20):
        msg_id = 5000 + index
        client.add(msg_id, text=f"m{index}")
        _run(_process(client, _outgoing(msg_id, f"m{index}"), provider))
    assert len(engine._handled) <= engine.GUARD_MAX


# ── 6. mutual exclusion with Auto-Reaction ──────────────────────────────────


class _FakeRepo:
    """In-memory panel_settings row so set_setting() behaves like a live DB."""

    def __init__(self, **overrides: Any) -> None:
        self.row = dict(settings_service._DEFAULTS)
        self.row.update(overrides)

    def load(self):
        return dict(self.row)

    def update_field(self, field, value):
        self.row[field] = value
        return True


def test_enabling_intelligent_reaction_disables_auto_reaction(monkeypatch):
    fake = _FakeRepo(auto_reaction_enabled=True, intelligent_reaction_enabled=False)
    monkeypatch.setattr(settings_service, "repo", fake)
    assert settings_service.set_intelligent_reaction_enabled(True) is True
    assert fake.row["intelligent_reaction_enabled"] is True
    assert fake.row["auto_reaction_enabled"] is False
    assert settings_service.is_auto_reaction_enabled() is False


def test_enabling_auto_reaction_disables_intelligent_reaction(monkeypatch):
    fake = _FakeRepo(auto_reaction_enabled=False, intelligent_reaction_enabled=True)
    monkeypatch.setattr(settings_service, "repo", fake)
    assert settings_service.set_auto_reaction_enabled(True) is True
    assert fake.row["auto_reaction_enabled"] is True
    assert fake.row["intelligent_reaction_enabled"] is False
    assert settings_service.is_intelligent_reaction_enabled() is False


def test_toggles_respect_the_exclusion(monkeypatch):
    fake = _FakeRepo(auto_reaction_enabled=False, intelligent_reaction_enabled=True)
    monkeypatch.setattr(settings_service, "repo", fake)
    assert settings_service.toggle_auto_reaction() is True
    assert fake.row["intelligent_reaction_enabled"] is False


def test_both_enabled_is_a_conflict_and_sends_nothing():
    settings_service._cache["auto_reaction_enabled"] = True
    settings_service._cache["intelligent_reaction_enabled"] = True
    client = _client()
    provider = _Provider(_ok(100, "👍"))
    result = _run(_process(client, _outgoing(), provider))
    assert result["status"] == engine.STATUS_CONFLICT
    assert provider.calls == []
    assert client.reaction_calls == []


def test_the_two_modes_use_independent_settings():
    assert "auto_reaction_emoji" not in _code_only(engine.__file__)
    assert settings_service._DEFAULTS["intelligent_reaction_enabled"] is False


# ── 7. persistence contract ─────────────────────────────────────────────────


def test_the_setting_default_is_off_and_has_accessors():
    assert settings_service._DEFAULTS["intelligent_reaction_enabled"] is False
    assert settings_service.is_intelligent_reaction_enabled() is False
    assert callable(settings_service.set_intelligent_reaction_enabled)
    assert callable(settings_service.toggle_intelligent_reaction)


def test_the_migration_is_additive_and_off_by_default():
    raw = MIGRATION.read_text(encoding="utf-8")
    # The header documents its own rollback; only executed statements count.
    body = "\n".join(
        line for line in raw.split("*/", 1)[1].splitlines()
        if line.strip() and not line.strip().startswith("--")
    )
    assert "ADD COLUMN IF NOT EXISTS intelligent_reaction_enabled boolean;" in body
    assert "ALTER COLUMN intelligent_reaction_enabled SET DEFAULT false;" in body
    assert "ALTER COLUMN intelligent_reaction_enabled SET NOT NULL;" in body
    for forbidden in (
        "DROP COLUMN", "DROP TABLE", "TRUNCATE", "DELETE FROM",
        "SET DEFAULT true", "= true",
    ):
        assert forbidden not in body, forbidden


# ── 8. handler + architecture ───────────────────────────────────────────────


class _CapturingClient:
    def __init__(self) -> None:
        self.handlers: list[tuple[Any, Any]] = []

    def on(self, builder):
        def _decorator(callback):
            self.handlers.append((builder, callback))
            return callback

        return _decorator


class _FakeEvent:
    def __init__(self, message) -> None:
        self.message = message
        self.sender_id = message.sender_id
        self.chat_id = message.chat_id
        self.raw_text = message.text


def _registered_handler():
    client = _CapturingClient()
    intelligent_reaction.register(client, OWNER)
    assert len(client.handlers) == 1
    return client.handlers[0]


def test_handler_registers_exactly_one_outgoing_listener():
    builder, _callback = _registered_handler()
    assert builder.outgoing is True
    assert builder.incoming is False


def test_handler_runs_owner_messages_through_the_service(monkeypatch):
    _builder, callback = _registered_handler()
    calls: list[dict] = []

    async def _record(**kwargs):
        calls.append(kwargs)
        return {}

    monkeypatch.setattr(intelligent_reaction, "process_outgoing_message", _record)
    _run(callback(_FakeEvent(_outgoing())))
    assert len(calls) == 1
    assert calls[0]["owner_id"] == OWNER
    assert calls[0]["message"]["id"] == MSG


def test_handler_ignores_non_owner_events(monkeypatch):
    _builder, callback = _registered_handler()
    calls: list[Any] = []

    async def _record(**kwargs):
        calls.append(kwargs)
        return {}

    monkeypatch.setattr(intelligent_reaction, "process_outgoing_message", _record)
    _run(callback(_FakeEvent(_outgoing(sender_id=OTHER))))
    assert calls == []


def test_handler_swallows_service_errors_and_propagates_cancellation(monkeypatch):
    _builder, callback = _registered_handler()

    async def _boom(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(intelligent_reaction, "process_outgoing_message", _boom)
    _run(callback(_FakeEvent(_outgoing())))  # must not raise

    async def _cancel(**kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(intelligent_reaction, "process_outgoing_message", _cancel)
    with pytest.raises(asyncio.CancelledError):
        _run(callback(_FakeEvent(_outgoing(MSG2))))


def test_the_service_has_no_second_telegram_loop_scheduler_or_provider():
    code = _code_only(engine.__file__)
    for forbidden in (
        "asyncio.create_task",
        "ensure_future",
        "call_later",
        "ThreadPoolExecutor",
        "TelegramClient",
        "events.NewMessage",
    ):
        assert forbidden not in code, forbidden


def _imports_of(module_path: str) -> list[str]:
    tree = ast.parse(Path(module_path).read_text())
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def test_the_service_never_imports_a_provider_directly():
    for name in _imports_of(engine.__file__):
        assert not name.startswith("backend.ai.providers"), name


def test_router_registers_intelligent_reaction_once_before_auto_reaction():
    from backend.bot import router

    source = Path(router.__file__).read_text()
    assert source.count('"intelligent_reaction"') == 1
    assert source.count("intelligent_reaction.register(client, owner_id)") == 1
    assert source.index('"intelligent_reaction"') < source.index('"auto_reaction"')


# ── 9. the toggle is reachable from the General panel ───────────────────────


def _register_misc():
    from backend.bot.handlers import misc

    class _FakeClient:
        def on(self, *args, **kwargs):
            def _decorator(fn):
                return fn

            return _decorator

    misc.register(client=_FakeClient(), owner_id=OWNER)


def _panel_datas(builder) -> list[str]:
    out = []
    for row in builder:
        for button in row:
            raw = getattr(button, "data", button)
            out.append(raw.decode("utf-8") if isinstance(raw, bytes) else str(raw))
    return out


def test_general_panel_exposes_and_registers_the_toggle():
    from backend.bot.handlers import misc
    from backend.helper.panels import get_action

    _register_misc()
    datas = _panel_datas(misc._build_general_buttons())
    assert "action:general_toggle_intelligent_reaction" in datas
    assert get_action("general_toggle_intelligent_reaction") is not None


def test_general_body_states_the_mode_and_the_exclusion():
    from backend.bot.handlers import misc

    settings_service._cache["intelligent_reaction_enabled"] = True
    body = _run(misc._general_body())
    assert "Intelligent Reaction" in body
    assert "mutually exclusive" in body
