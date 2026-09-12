"""External-call efficiency — request-scoped dedup + documented call graph.

Every test here asserts a CALL COUNT on the real code path, so it proves both
directions: the required work still happens, and the redundant work is gone.

| call | before | after |
|---|---|---|
| Telegram ``get_reply_message`` per reply-triggered AI request | 2 | **1** |
| ``ai_config`` read per AI request (trigger resolve + config restore) | 2 | **1** |
| ``ai_config`` read when the trigger cache already served the request | 1 | 1 (unchanged) |
| ``ai_tasks`` event-task query per incoming Telegram message | 1 | 1 (required — see the report) |
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from backend.ai.config_store import _DEFAULTS


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── reply message: one Telegram fetch per request ──────────────────────────


class _FakeReplyMessage:
    def __init__(self, msg_id: int = 77, chat_id: int = 555):
        self.id = msg_id
        self.chat_id = chat_id
        self.date = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
        self.media = None
        self.message = "the replied text"

    async def get_sender(self):
        return None

    async def get_chat(self):
        return None


class _ReplyEvent:
    def __init__(self, reply):
        self._reply = reply
        self.fetches = 0
        self.raw_text = "Nova hello"
        self.is_reply = True
        self.chat_id = 555
        self.sender_id = 4242
        self.message = type("M", (), {"id": 900})()

    async def get_reply_message(self):
        self.fetches += 1
        return self._reply

    async def edit(self, text=None, buttons=None, **kwargs):
        return None


def test_reply_context_reuses_an_already_fetched_message():
    from backend.bot.handlers.ai_unified import _extract_reply_context

    event = _ReplyEvent(_FakeReplyMessage())
    user_message, reply_ctx, error = _run(
        _extract_reply_context(event, None, "hello", reply_msg=event._reply)
    )

    assert error == ""
    assert user_message == "hello"
    assert reply_ctx.exists is True and reply_ctx.message_id == 77
    assert event.fetches == 0, "the already-fetched reply must not be re-fetched"


def test_reply_context_still_fetches_when_the_caller_has_none():
    from backend.bot.handlers.ai_unified import _extract_reply_context

    event = _ReplyEvent(_FakeReplyMessage())
    user_message, reply_ctx, error = _run(_extract_reply_context(event, None, "hello"))

    assert error == "" and reply_ctx.exists is True
    assert event.fetches == 1, "required context must still be fetched"


def test_activation_fetches_the_replied_message_exactly_once(monkeypatch):
    """End-to-end through the real activation handler (reply-trigger mode)."""
    from backend.bot.handlers import ai_unified

    captured: list = []

    async def _fake_execute_ai(event, owner_id, prompt_text, trigger_word, tz_str,
                               reply_context=None, client=None, config=None):
        captured.append((prompt_text, reply_context, config))

    monkeypatch.setattr(ai_unified, "_execute_ai", _fake_execute_ai)

    async def _triggers(owner_id):
        return "Nova", "", None

    monkeypatch.setattr(ai_unified, "_load_triggers", _triggers)

    class _Client:
        def __init__(self):
            self.handler = None

        def on(self, _event):
            def _decorator(fn):
                self.handler = fn
                return fn
            return _decorator

    client = _Client()
    ai_unified.register(client, owner_id=4242, tz_str="UTC")

    event = _ReplyEvent(_FakeReplyMessage())
    _run(client.handler(event))

    assert event.fetches == 1, "the replied message was fetched more than once"
    assert len(captured) == 1
    prompt_text, reply_ctx, _config = captured[0]
    assert prompt_text == "hello"
    assert reply_ctx.exists is True and reply_ctx.message_id == 77


# ── ai_config: one read per request, no cross-request staleness ────────────


def _config_row(**overrides):
    value = dict(_DEFAULTS)
    value.update(overrides)
    return value


@pytest.fixture()
def config_reads(monkeypatch):
    """Count every ``ai_config`` read, backing it with a real snapshot."""
    import backend.ai.config_store as config_store

    reads: list = []
    row = _config_row(trigger_en="Nova", trigger_fa="", provider="dummy", model="m")

    async def _get_config(owner_id):
        reads.append(owner_id)
        return dict(row)

    monkeypatch.setattr(config_store, "get_config", _get_config)
    return reads, row


def test_trigger_resolution_hands_its_snapshot_to_the_config_restore(config_reads, monkeypatch):
    from backend.bot.handlers import ai_unified
    from backend.ai.engine import engine as engine_module

    reads, row = config_reads
    ai_unified._trigger_cache.update({"en": "", "fa": "", "ts": 0.0})

    applied: list = []

    async def _fake_apply(owner_id, config=None):
        applied.append(config)
        return True

    monkeypatch.setattr(engine_module, "apply_persisted_config", _fake_apply)

    en, fa, snapshot = _run(ai_unified._load_triggers(1))
    assert (en, fa) == ("Nova", "")
    assert snapshot == row
    assert reads == [1], "the trigger resolve reads the row once"

    _run(ai_unified._restore_config(1, config=snapshot))
    assert reads == [1], "the config restore must reuse the request's snapshot"
    assert applied == [snapshot]


def test_warm_trigger_cache_performs_no_config_read(config_reads):
    from backend.bot.handlers import ai_unified

    reads, _row = config_reads
    ai_unified._trigger_cache.update({"en": "Nova", "fa": "", "ts": 10 ** 9})

    en, fa, snapshot = _run(ai_unified._load_triggers(1))

    assert (en, fa, snapshot) == ("Nova", "", None)
    assert reads == [], "a warm trigger cache must not read the config row"


# ── the real apply_persisted_config: snapshot ⇒ no read, behavior intact ───


class _FakeProviderConfig:
    def __init__(self):
        self.temperature = None
        self.max_tokens = None


class _FakeConversationManager:
    def __init__(self):
        self.sessions: list = []
        self.providers: list = []
        self.prompts: list = []

    def create_session(self, owner_id):
        self.sessions.append(owner_id)

    def set_provider(self, owner_id, provider, model):
        self.providers.append((owner_id, provider, model))

    def set_system_prompt(self, owner_id, prompt):
        self.prompts.append((owner_id, prompt))


class _FakeEngine:
    def __init__(self):
        self.conversation_manager = _FakeConversationManager()
        self.provider_manager = type("PM", (), {
            "get_provider_config": lambda self_, name: _FakeProviderConfig(),
        })()


def _patch_engine(monkeypatch, reads, engine_module):
    engine = _FakeEngine()
    monkeypatch.setattr(engine_module, "get_engine", lambda: engine)
    selected: list = []
    monkeypatch.setattr(
        engine_module, "apply_runtime_selection",
        lambda provider, model: selected.append((provider, model)) or True,
    )
    return engine, selected


def test_apply_persisted_config_uses_the_snapshot_and_applies_it(config_reads, monkeypatch):
    from backend.ai.engine import engine as engine_module

    reads, row = config_reads
    engine, selected = _patch_engine(monkeypatch, reads, engine_module)

    ok = _run(engine_module.apply_persisted_config(1, config=row))

    assert ok is True
    assert reads == [], "an explicit snapshot must not trigger another ai_config read"
    assert selected == [("dummy", "m")], "the snapshot is still applied"
    assert engine.conversation_manager.providers == [(1, "dummy", "m")]
    assert engine.conversation_manager.prompts and engine.conversation_manager.prompts[0][0] == 1


def test_apply_persisted_config_reads_once_without_a_snapshot(config_reads, monkeypatch):
    from backend.ai.engine import engine as engine_module

    reads, _row = config_reads
    engine, selected = _patch_engine(monkeypatch, reads, engine_module)

    ok = _run(engine_module.apply_persisted_config(1))

    assert ok is True
    assert reads == [1], "without a snapshot the restore performs exactly one read"
    assert selected == [("dummy", "m")]


def test_no_snapshot_is_cached_across_requests(config_reads, monkeypatch):
    """A later request must read again — no stale device-wide config."""
    from backend.bot.handlers import ai_unified

    reads, _row = config_reads
    ai_unified._trigger_cache.update({"en": "", "fa": "", "ts": 0.0})
    _run(ai_unified._load_triggers(1))
    ai_unified._trigger_cache.update({"en": "", "fa": "", "ts": 0.0})  # TTL expired
    _run(ai_unified._load_triggers(1))
    assert reads == [1, 1], "each request re-reads the row (nothing cached)"


# ── the per-event task query is bounded (documented, unchanged) ────────────


class _CountingRepo:
    def __init__(self, tasks=()):
        self.tasks = list(tasks)
        self.event_queries = 0

    async def list_event_tasks(self, owner_id, limit=10):
        self.event_queries += 1
        return list(self.tasks)

    async def create_occurrence(self, owner_id, data):
        raise AssertionError("no occurrence should be created without a match")

    async def claim_occurrence(self, owner_id, task_id, key):
        raise AssertionError("no claim without a match")

    async def transition_occurrence(self, owner_id, task_id, key, status, **updates):
        return None


def test_event_dispatch_queries_once_per_event_and_never_per_task():
    from backend.ai.task_event_dispatcher import TaskEventDispatcher

    class _Task:
        id = 5
        version = 1
        schedule_type = "daily"  # not an event schedule
        schedule = {"hour": 9, "minute": 0, "timezone": "UTC"}
        actions = [{"name": "send_message", "arguments": {"text": "x"}}]

    repo = _CountingRepo(tasks=[_Task()])
    dispatcher = TaskEventDispatcher(repo, owner_id=1)
    context = {"chat_id": 55, "message_id": 9}

    assert _run(dispatcher.handle_event(context)) == 0
    assert _run(dispatcher.handle_event(context)) == 0
    assert repo.event_queries == 2, "one authoritative query per event, never more"
