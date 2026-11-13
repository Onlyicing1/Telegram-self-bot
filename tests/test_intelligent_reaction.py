"""Intelligent Reaction — fixed owner probability, AI semantic choice only.

Pins the corrected contract end to end, offline:

* probability is OWNER configuration, read per message from
  ``settings_service.intelligent_reaction_probability()`` (default 25, integer
  0–100): 0% never reacts and costs no catalog/AI call, 1–99% performs exactly
  one independent draw, 100% always passes, and nothing stateful (counter,
  quota, rolling window, streak) exists;
* the AI answers ONLY ``{"reaction": <emoji|null>}``; a legacy ``probability``
  field, an extra field, a non-catalog emoji or malformed JSON sends nothing and
  never overrides the owner's percentage;
* the reaction set comes solely from Telegram's catalog via
  ``reaction_source_service`` (same snapshot offered to the AI and used to
  validate its answer); an unavailable catalog fails closed;
* bounded context: one message text, at most one replied-to text, a coarse chat
  type and an explicit reply flag — never ids, usernames, titles or history;
* eligibility and guards: owner-authored outgoing only, text required,
  inline-origin skipped, pending panel input skipped, duplicates skipped, the
  fixed Auto-Reaction mode wins while ON;
* honest outcomes: exactly one stable status per evaluation, Telegram failures
  reported as ``telegram_send_failed``, at most one send attempt.

Everything runs offline: Telegram's catalog and reaction send are patched at the
service boundaries, and the AI is a scripted ``ProviderManager``-shaped object.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from backend.services import intelligent_reaction_service as engine
from backend.services import reaction_service
from backend.services import reaction_source_service
from backend.services import settings_service

OWNER = 424242
OTHER = 991199
CHAT = -1001234567890
MSG = 555
REPLIED = 900
CATALOG = ("👍", "❤", "🎉", "🔥")
MIGRATION = (
    Path(engine.__file__).resolve().parent.parent.parent
    / "supabase" / "migrations"
    / "20261112160000_add_panel_settings_intelligent_reaction_probability.sql"
)


def _run(coro):
    return asyncio.run(coro)


def _flat(value) -> str:
    if isinstance(value, dict):
        return " ".join(_flat(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return " ".join(_flat(item) for item in value)
    return str(value)


@pytest.fixture(autouse=True)
def _isolated_settings():
    """Keep the settings cache and the dedup registry per-test."""
    old_cache = dict(settings_service._cache)
    engine.reset_loop_guard()
    engine.reset_coalescer()
    yield
    settings_service._cache.clear()
    settings_service._cache.update(old_cache)
    engine.reset_loop_guard()
    engine.reset_coalescer()


class FakeResponse:
    def __init__(self, text: str = "", success: bool = True):
        self.text = text
        self.success = success


class FakeManager:
    """ProviderManager-shaped: records every call, answers with a script."""

    def __init__(self, text: str = "", *, success: bool = True, delay: float = 0.0, exc=None):
        self.text = text
        self.success = success
        self.delay = delay
        self.exc = exc
        self.calls: list[dict] = []

    async def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc is not None:
            raise self.exc
        return FakeResponse(self.text, success=self.success)


def _settings(monkeypatch, *, enabled=True, probability=100, auto=False):
    monkeypatch.setattr(settings_service, "is_intelligent_reaction_enabled", lambda: enabled)
    monkeypatch.setattr(settings_service, "intelligent_reaction_probability", lambda: probability)
    monkeypatch.setattr(settings_service, "is_auto_reaction_enabled", lambda: auto)


def _catalog(monkeypatch, emojis=CATALOG):
    calls: list = []

    async def fake(client):
        calls.append(client)
        return {
            "ok": True, "error": None, "detail": None, "cached": False,
            "options": [
                {"reaction": {"kind": "emoji", "emoji": e}, "emoji": e, "title": ""}
                for e in emojis
            ],
        }

    monkeypatch.setattr(reaction_source_service, "load_reactions", fake)
    return calls


def _forbid_catalog(monkeypatch):
    async def boom(client):
        raise AssertionError("the catalog must not be requested")

    monkeypatch.setattr(reaction_source_service, "load_reactions", boom)


def _send(monkeypatch, result=None):
    calls: list = []

    async def fake(client, owner_id, chat_id, msg_id, reaction):
        calls.append((client, owner_id, chat_id, msg_id, reaction))
        return result if result is not None else {"ok": True, "reaction": reaction}

    monkeypatch.setattr(reaction_service, "react_to_message", fake)
    return calls


def _forbid_send(monkeypatch):
    async def boom(*args, **kwargs):
        raise AssertionError("no reaction may be sent")

    monkeypatch.setattr(reaction_service, "react_to_message", boom)


def _message(**overrides):
    message = {
        "id": MSG,
        "chat_id": CHAT,
        "sender_id": OWNER,
        "text": "Hello there, shipping the fix now",
        "out": True,
        "reply_to_msg_id": None,
    }
    message.update(overrides)
    return message


def _process(manager=None, *, message=None, client=None, **kwargs):
    kwargs.setdefault("draw", lambda: 0.0)
    return _run(
        engine.process_outgoing_message(
            owner_id=kwargs.pop("owner_id", OWNER),
            client=client if client is not None else object(),
            message=message if message is not None else _message(),
            manager=manager,
            **kwargs,
        )
    )


# ── 1. the probability gate (deterministic, no AI, no Telegram) ─────────────

def test_probability_zero_never_passes_and_consumes_no_draw():
    def draw():
        raise AssertionError("0% must not draw")

    assert engine.probability_passes(0, draw=draw) is False


def test_probability_hundred_always_passes_and_consumes_no_draw():
    def draw():
        raise AssertionError("100% must not draw")

    assert engine.probability_passes(100, draw=draw) is True


def test_probability_mid_range_uses_exactly_one_independent_draw():
    draws = iter([0.49, 0.51])
    assert engine.probability_passes(50, draw=lambda: next(draws)) is True
    assert engine.probability_passes(50, draw=lambda: next(draws)) is False


def test_probability_boundary_is_strictly_less_than_percent():
    assert engine.probability_passes(25, draw=lambda: 0.2499) is True
    assert engine.probability_passes(25, draw=lambda: 0.25) is False


@pytest.mark.parametrize("bad", [-1, 101, 1000, True, False, "50", 25.5, None])
def test_invalid_probability_fails_closed(bad):
    assert engine.probability_passes(bad, draw=lambda: 0.0) is False


def test_probability_draw_type_error_fails_closed():
    assert engine.probability_passes(50, draw=lambda: "not-a-number") is False


# ── 2. chat type + bounded context ──────────────────────────────────────────

def test_classify_chat_type_is_bounded():
    assert engine.classify_chat_type(is_private=True) == "private"
    assert engine.classify_chat_type(is_group=True) == "group"
    assert engine.classify_chat_type(is_group=True, is_supergroup=True) == "supergroup"
    assert engine.classify_chat_type(is_channel=True) == "channel"
    assert engine.classify_chat_type() == "unknown"
    assert engine.normalize_chat_type("garbage") == "unknown"


def test_build_context_keeps_only_the_bounded_fields():
    context = engine.build_context(
        _message(text="x" * 5000),
        chat_type="group", is_reply=True, replied_text="y" * 5000,
    )
    assert set(context) == {"message", "chat_type", "is_reply", "replied_text"}
    assert len(context["message"]) <= engine.MAX_MESSAGE_CHARS
    assert len(context["replied_text"]) <= engine.MAX_REPLIED_CHARS
    assert context["chat_type"] == "group" and context["is_reply"] is True
    assert str(OWNER) not in _flat(context) and str(CHAT) not in _flat(context)


def test_build_context_drops_replied_text_when_not_a_reply():
    context = engine.build_context(
        _message(), chat_type="private", is_reply=False, replied_text="leak",
    )
    assert context["replied_text"] == "" and context["is_reply"] is False


def test_prompt_contains_the_message_and_catalog_but_no_identity():
    context = engine.build_context(
        _message(text="deploy is green"), chat_type="private",
        is_reply=True, replied_text="did it pass?",
    )
    messages = engine.build_ai_messages(context, CATALOG)
    assert len(messages) == 2
    payload = messages[1]["content"]
    assert "deploy is green" in payload and "did it pass?" in payload
    for emoji in CATALOG:
        assert emoji in payload
    for leaked in (str(CHAT), str(MSG), str(OWNER), "sender_id", "chat_id"):
        assert leaked not in payload
    parsed = json.loads(payload)
    assert set(parsed) == {"message", "chat_type", "is_reply", "replied_text", "allowed_reactions"}
    system = messages[0]["content"].lower()
    assert "reaction" in system and "json" in system
    assert "probability" in system, "the model must be told probability is not its job"


# ── 3. the strict AI answer contract ────────────────────────────────────────

@pytest.mark.parametrize("emoji", CATALOG)
def test_valid_reaction_passes(emoji):
    status, parsed = engine.parse_ai_reaction(json.dumps({"reaction": emoji}), CATALOG)
    assert status is None and parsed == emoji


def test_null_reaction_is_a_semantic_no():
    status, parsed = engine.parse_ai_reaction('{"reaction": null}', CATALOG)
    assert status == engine.STATUS_NO_SEMANTIC_REACTION and parsed is None


@pytest.mark.parametrize("answer", [
    '{"probability": 90, "reaction": "👍"}',
    '{"reaction": "👍", "probability": 10}',
    '{"reaction": "👍", "reason": "nice"}',
    '{"reaction": "👍", "confidence": 0.9}',
    '{"reaction": "👍", "reaction": "❤"}',
    "no json here",
    "",
    "[]",
    "null",
    '{"reaction": 3}',
    '{"reaction": {"emoji": "👍"}}',
])
def test_invalid_answers_are_rejected(answer):
    status, parsed = engine.parse_ai_reaction(answer, CATALOG)
    assert status == engine.STATUS_INVALID_AI_RESPONSE and parsed is None


def test_legacy_probability_answer_can_never_override_the_owner_setting():
    """The removed contract must stay removed: a probability field invalidates."""
    for legacy in ('{"probability": 100, "reaction": "👍"}',
                   '{"probability": 0, "reaction": null}'):
        status, _ = engine.parse_ai_reaction(legacy, CATALOG)
        assert status == engine.STATUS_INVALID_AI_RESPONSE


def test_unsupported_emoji_is_rejected_with_its_own_status():
    status, parsed = engine.parse_ai_reaction('{"reaction": "🦄"}', CATALOG)
    assert status == engine.STATUS_UNSUPPORTED_REACTION and parsed == "🦄"


def test_non_catalog_but_valid_emoji_is_rejected():
    status, parsed = engine.parse_ai_reaction('{"reaction": "🤖"}', ("👍",))
    assert status == engine.STATUS_UNSUPPORTED_REACTION and parsed == "🤖"


# ── 4. catalog snapshot: Telegram only, same list for input and validation ──

def test_catalog_snapshot_uses_the_telegram_source(monkeypatch):
    _catalog(monkeypatch, ("👍", "❤"))
    assert _run(engine.catalog_snapshot(object())) == ("👍", "❤")


def test_catalog_snapshot_deduplicates_preserving_order(monkeypatch):
    _catalog(monkeypatch, ("👍", "❤", "👍", "🎉"))
    assert _run(engine.catalog_snapshot(object())) == ("👍", "❤", "🎉")


@pytest.mark.parametrize("failure", [
    {"ok": False, "error": "telegram", "detail": "boom", "options": []},
    {"ok": True, "options": []},
    None,
])
def test_catalog_snapshot_fails_closed(monkeypatch, failure):
    async def fake(client):
        return failure

    monkeypatch.setattr(reaction_source_service, "load_reactions", fake)
    assert _run(engine.catalog_snapshot(object())) is None


def test_catalog_snapshot_survives_an_unexpected_exception(monkeypatch):
    async def boom(client):
        raise RuntimeError("network")

    monkeypatch.setattr(reaction_source_service, "load_reactions", boom)
    assert _run(engine.catalog_snapshot(object())) is None


# ── 5. eligibility + guards, in the documented order ────────────────────────

def test_invalid_owner_and_message_are_refused(monkeypatch):
    _settings(monkeypatch)
    for owner in (0, -1, "1", True):
        assert _process(owner_id=owner)["status"] == engine.STATUS_INVALID
    assert _process(message={"id": 0, "chat_id": CHAT})["status"] == engine.STATUS_INVALID


def test_foreign_or_incoming_message_is_not_evaluated(monkeypatch):
    _settings(monkeypatch)
    _forbid_catalog(monkeypatch)
    assert _process(message=_message(out=False))["status"] == engine.STATUS_NOT_OWNER
    assert _process(message=_message(sender_id=OTHER))["status"] == engine.STATUS_NOT_OWNER


def test_inline_bot_origin_is_skipped(monkeypatch):
    _settings(monkeypatch)
    _forbid_catalog(monkeypatch)
    outcome = _process(message=_message(), via_bot_id=777)
    assert outcome["status"] == engine.STATUS_INLINE_ORIGIN


def test_pending_panel_input_is_skipped(monkeypatch):
    _settings(monkeypatch)
    _forbid_catalog(monkeypatch)
    monkeypatch.setattr(engine, "get_pending", lambda owner: {"chat_id": CHAT})
    assert _process()["status"] == engine.STATUS_PENDING_INPUT


def test_disabled_mode_skips_before_any_catalog_read(monkeypatch):
    _settings(monkeypatch, enabled=False)
    _forbid_catalog(monkeypatch)
    assert _process()["status"] == engine.STATUS_DISABLED


def test_auto_reaction_active_wins(monkeypatch):
    _settings(monkeypatch, auto=True)
    _forbid_catalog(monkeypatch)
    assert _process()["status"] == engine.STATUS_AUTO_REACTION_ACTIVE


def test_message_without_text_is_skipped(monkeypatch):
    _settings(monkeypatch)
    _forbid_catalog(monkeypatch)
    assert _process(message=_message(text="   "))["status"] == engine.STATUS_NO_TEXT
    assert _process(message=_message(text=None))["status"] == engine.STATUS_NO_TEXT


def test_zero_percent_costs_no_catalog_and_no_ai(monkeypatch):
    _settings(monkeypatch, probability=0)
    _forbid_catalog(monkeypatch)
    manager = FakeManager('{"reaction": "👍"}')
    assert _process(manager)["status"] == engine.STATUS_PROBABILITY_ZERO
    assert manager.calls == []


def test_probability_miss_costs_no_catalog_and_no_ai(monkeypatch):
    _settings(monkeypatch, probability=10)
    _forbid_catalog(monkeypatch)
    manager = FakeManager('{"reaction": "👍"}')
    outcome = _process(manager, draw=lambda: 0.9)
    assert outcome["status"] == engine.STATUS_PROBABILITY_MISS
    assert manager.calls == []


def test_hundred_percent_passes_the_gate(monkeypatch):
    _settings(monkeypatch, probability=100)
    _catalog(monkeypatch)
    _send(monkeypatch)
    manager = FakeManager('{"reaction": "🎉"}')
    assert _process(manager, draw=lambda: 0.999999)["status"] == engine.STATUS_REACTED


def test_unavailable_catalog_sends_nothing(monkeypatch):
    _settings(monkeypatch)
    async def fake(client):
        return {"ok": False, "error": "empty", "detail": "no reactions", "options": []}
    monkeypatch.setattr(reaction_source_service, "load_reactions", fake)
    _forbid_send(monkeypatch)
    manager = FakeManager('{"reaction": "👍"}')
    assert _process(manager)["status"] == engine.STATUS_CATALOG_UNAVAILABLE
    assert manager.calls == []


def test_missing_client_fails_closed(monkeypatch):
    _settings(monkeypatch)
    outcome = _run(engine.process_outgoing_message(
        owner_id=OWNER, client=None, message=_message(), draw=lambda: 0.0,
    ))
    assert outcome["status"] == engine.STATUS_CATALOG_UNAVAILABLE


def test_missing_ai_engine_sends_nothing(monkeypatch):
    _settings(monkeypatch)
    _catalog(monkeypatch)
    _forbid_send(monkeypatch)
    monkeypatch.setattr(engine, "_resolve_manager", lambda manager: None)
    assert _process(None)["status"] == engine.STATUS_AI_FAILURE


# ── 6. end to end: one evaluation, at most one send, honest status ──────────

def test_successful_reaction_targets_the_owner_outgoing_message(monkeypatch):
    _settings(monkeypatch, probability=100)
    _catalog(monkeypatch)
    calls = _send(monkeypatch)
    manager = FakeManager('{"reaction": "❤"}')
    client = object()

    outcome = _process(manager, client=client)

    assert outcome["status"] == engine.STATUS_REACTED
    assert outcome["reaction"] == {"kind": "emoji", "emoji": "❤"}
    assert len(calls) == 1
    sent_client, owner_id, chat_id, msg_id, reaction = calls[0]
    assert sent_client is client and owner_id == OWNER
    assert (chat_id, msg_id) == (CHAT, MSG)
    assert reaction == {"kind": "emoji", "emoji": "❤"}
    assert len(manager.calls) == 1
    assert manager.calls[0]["kwargs"]["response_format"] == {"type": "json_object"}


def test_the_same_message_is_evaluated_at_most_once(monkeypatch):
    _settings(monkeypatch, probability=100)
    _catalog(monkeypatch)
    calls = _send(monkeypatch)
    manager = FakeManager('{"reaction": "👍"}')

    first = _process(manager)
    second = _process(manager)

    assert first["status"] == engine.STATUS_REACTED
    assert second["status"] == engine.STATUS_DUPLICATE
    assert len(calls) == 1 and len(manager.calls) == 1


def test_semantic_null_sends_nothing(monkeypatch):
    _settings(monkeypatch)
    _catalog(monkeypatch)
    _forbid_send(monkeypatch)
    outcome = _process(FakeManager('{"reaction": null}'))
    assert outcome["status"] == engine.STATUS_NO_SEMANTIC_REACTION


def test_legacy_probability_answer_is_an_invalid_response(monkeypatch):
    _settings(monkeypatch, probability=100)
    _catalog(monkeypatch)
    _forbid_send(monkeypatch)
    outcome = _process(FakeManager('{"probability": 100, "reaction": "👍"}'))
    assert outcome["status"] == engine.STATUS_INVALID_AI_RESPONSE


def test_unsupported_emoji_is_not_substituted(monkeypatch):
    _settings(monkeypatch)
    _catalog(monkeypatch, ("👍", "❤"))
    _forbid_send(monkeypatch)
    outcome = _process(FakeManager('{"reaction": "🎉"}'))
    assert outcome["status"] == engine.STATUS_UNSUPPORTED_REACTION
    assert outcome["reaction"] is None


def test_invalid_json_sends_nothing(monkeypatch):
    _settings(monkeypatch)
    _catalog(monkeypatch)
    _forbid_send(monkeypatch)
    assert _process(FakeManager("not json"))["status"] == engine.STATUS_INVALID_AI_RESPONSE


def test_provider_failure_sends_nothing(monkeypatch):
    _settings(monkeypatch)
    _catalog(monkeypatch)
    _forbid_send(monkeypatch)
    manager = FakeManager('{"reaction": "👍"}', success=False)
    assert _process(manager)["status"] == engine.STATUS_AI_FAILURE


def test_provider_exception_sends_nothing(monkeypatch):
    _settings(monkeypatch)
    _catalog(monkeypatch)
    _forbid_send(monkeypatch)
    manager = FakeManager(exc=RuntimeError("provider down"))
    assert _process(manager)["status"] == engine.STATUS_AI_FAILURE


def test_provider_timeout_sends_nothing(monkeypatch):
    _settings(monkeypatch)
    _catalog(monkeypatch)
    _forbid_send(monkeypatch)
    manager = FakeManager('{"reaction": "👍"}', delay=0.2)
    outcome = _process(manager, timeout_s=0.01)
    assert outcome["status"] == engine.STATUS_AI_TIMEOUT


def test_telegram_rejection_is_not_reported_as_reacted(monkeypatch):
    _settings(monkeypatch)
    _catalog(monkeypatch)
    _send(monkeypatch, {"ok": False, "error": "telegram", "detail": "REACTION_INVALID"})
    outcome = _process(FakeManager('{"reaction": "👍"}'))
    assert outcome["status"] == engine.STATUS_TELEGRAM_FAILED
    assert outcome["status"] != engine.STATUS_REACTED


def test_telegram_exception_is_not_reported_as_reacted(monkeypatch):
    _settings(monkeypatch)
    _catalog(monkeypatch)

    async def boom(*args, **kwargs):
        raise RuntimeError("connection lost")

    monkeypatch.setattr(reaction_service, "react_to_message", boom)
    outcome = _process(FakeManager('{"reaction": "👍"}'))
    assert outcome["status"] == engine.STATUS_TELEGRAM_FAILED


def test_cleanup_leaves_no_fixed_fallback_emoji(monkeypatch):
    _settings(monkeypatch)
    _catalog(monkeypatch)
    _forbid_send(monkeypatch)
    manager = FakeManager("")
    assert _process(manager)["status"] == engine.STATUS_INVALID_AI_RESPONSE


def test_cancellation_is_reraised(monkeypatch):
    _settings(monkeypatch)
    _catalog(monkeypatch)
    manager = FakeManager(exc=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        _process(manager)


# ── 7. settings: default, validation, honest persistence ────────────────────

def test_probability_defaults_to_twenty_five():
    settings_service._cache.pop("intelligent_reaction_probability", None)
    assert settings_service.intelligent_reaction_probability() == 25
    assert settings_service._DEFAULTS["intelligent_reaction_probability"] == 25


def test_probability_accessor_falls_back_for_a_corrupt_cached_value():
    for bad in (101, -4, "50", True, None):
        settings_service._cache["intelligent_reaction_probability"] = bad
        assert settings_service.intelligent_reaction_probability() == 25


@pytest.mark.parametrize("value", [0, 1, 25, 50, 99, 100])
def test_valid_probability_is_persisted(monkeypatch, value):
    settings_service._cache["intelligent_reaction_probability"] = 25
    monkeypatch.setattr(settings_service, "_db_configured", lambda: False)
    monkeypatch.setattr(settings_service.repo, "update_field", lambda *a, **k: False)
    assert settings_service.set_intelligent_reaction_probability(value) is True
    assert settings_service._cache["intelligent_reaction_probability"] == value


@pytest.mark.parametrize("bad", [-1, 101, 1000, True, False, "50", 25.5, None])
def test_invalid_probability_is_rejected_before_the_write(monkeypatch, bad):
    def must_not_write(*args, **kwargs):
        raise AssertionError("the repository must not be touched")

    monkeypatch.setattr(settings_service.repo, "update_field", must_not_write)
    assert settings_service.set_intelligent_reaction_probability(bad) is False


def test_rejected_database_write_does_not_claim_success(monkeypatch):
    settings_service._cache["intelligent_reaction_probability"] = 25
    monkeypatch.setattr(settings_service, "_db_configured", lambda: True)
    monkeypatch.setattr(settings_service.repo, "update_field", lambda *a, **k: False)
    assert settings_service.set_intelligent_reaction_probability(40) is False
    assert settings_service._cache["intelligent_reaction_probability"] == 25


def test_probability_setter_uses_the_strict_setter(monkeypatch):
    seen = []

    def strict(key, value):
        seen.append((key, value))
        return True

    monkeypatch.setattr(settings_service, "set_setting_strict", strict)
    monkeypatch.setattr(settings_service, "_db_configured", lambda: False)
    assert settings_service.set_intelligent_reaction_probability(75) is True
    assert seen == [("intelligent_reaction_probability", 75)]


def _persisted_row(value):
    return {"key": "global", "intelligent_reaction_probability": value}


def test_a_persisted_probability_is_confirmed_and_becomes_effective(monkeypatch):
    settings_service._cache["intelligent_reaction_probability"] = 25
    monkeypatch.setattr(settings_service, "_db_configured", lambda: True)
    monkeypatch.setattr(settings_service.repo, "update_field", lambda *a, **k: True)
    monkeypatch.setattr(settings_service.repo, "load", lambda: _persisted_row(60))

    assert settings_service.set_intelligent_reaction_probability(60) is True
    assert settings_service.intelligent_reaction_probability() == 60


def test_an_accepted_write_that_did_not_persist_is_not_a_success(monkeypatch):
    settings_service._cache["intelligent_reaction_probability"] = 25
    monkeypatch.setattr(settings_service, "_db_configured", lambda: True)
    monkeypatch.setattr(settings_service.repo, "update_field", lambda *a, **k: True)
    monkeypatch.setattr(settings_service.repo, "load", lambda: _persisted_row(25))

    assert settings_service.set_intelligent_reaction_probability(60) is False
    assert settings_service.intelligent_reaction_probability() == 25


def test_an_unreadable_persisted_row_is_not_a_success(monkeypatch):
    settings_service._cache["intelligent_reaction_probability"] = 25
    monkeypatch.setattr(settings_service, "_db_configured", lambda: True)
    monkeypatch.setattr(settings_service.repo, "update_field", lambda *a, **k: True)
    monkeypatch.setattr(settings_service.repo, "load", lambda: None)

    assert settings_service.set_intelligent_reaction_probability(60) is False
    assert settings_service.intelligent_reaction_probability() == 25


def test_probability_is_a_registered_key_with_a_zero_to_hundred_validator():
    assert "intelligent_reaction_probability" in settings_service._DEFAULTS
    validator = settings_service._VALIDATORS["intelligent_reaction_probability"]
    assert validator(0) and validator(100) and validator(25)
    assert not validator(-1) and not validator(101) and not validator(True)


# ── 8. the General panel: display, button and input action ──────────────────

def test_general_panel_shows_the_current_probability(monkeypatch):
    from backend.bot.handlers import misc

    _settings(monkeypatch, probability=42, enabled=True)
    monkeypatch.setattr(settings_service, "auto_reaction_emoji", lambda: "")
    body = _run(misc._general_body())
    assert "Reaction Probability: 42%" in body
    assert "Intelligent Reaction:" in body


def test_general_panel_exposes_the_probability_input(monkeypatch):
    from backend.bot.handlers import misc

    _settings(monkeypatch, probability=25)
    monkeypatch.setattr(settings_service, "auto_reaction_emoji", lambda: "")
    assert "input:general:intelligent_reaction_probability" in _flat(misc._build_general_buttons())


def test_probability_input_handler_saves_a_valid_value(monkeypatch):
    from backend.bot.handlers import misc

    seen = []
    monkeypatch.setattr(
        settings_service, "set_intelligent_reaction_probability",
        lambda value: seen.append(value) or True,
    )
    monkeypatch.setattr(misc, "get_client", lambda: None)
    monkeypatch.setattr("backend.helper.inline_engine._self_client", None)

    _run(misc._general_intelligent_reaction_probability_handler("65", 1, 2, None, None))
    assert seen == [65]


def test_probability_input_handler_accepts_the_boundaries(monkeypatch):
    from backend.bot.handlers import misc

    seen = []
    monkeypatch.setattr(
        settings_service, "set_intelligent_reaction_probability",
        lambda value: seen.append(value) or True,
    )
    monkeypatch.setattr(misc, "get_client", lambda: None)
    monkeypatch.setattr("backend.helper.inline_engine._self_client", None)

    _run(misc._general_intelligent_reaction_probability_handler("0", 1, 2, None, None))
    _run(misc._general_intelligent_reaction_probability_handler("100", 1, 2, None, None))
    _run(misc._general_intelligent_reaction_probability_handler(" 50% ", 1, 2, None, None))
    assert seen == [0, 100, 50]


@pytest.mark.parametrize("bad", ["", "-5", "101", "abc", "25.5", "1e2", "0x10"])
def test_probability_input_handler_rejects_invalid_text(monkeypatch, bad):
    from backend.bot.handlers import misc

    def must_not_store(value):
        raise AssertionError(f"invalid input must not be stored: {value!r}")

    monkeypatch.setattr(settings_service, "set_intelligent_reaction_probability", must_not_store)
    monkeypatch.setattr(misc, "get_client", lambda: None)
    monkeypatch.setattr("backend.helper.inline_engine._self_client", None)

    _run(misc._general_intelligent_reaction_probability_handler(bad, 1, 2, None, None))


def test_probability_input_handler_is_registered_on_the_general_panel():
    from backend.bot.handlers import misc
    from backend.helper.panels import get_input

    misc._register_panels()
    config = get_input("general", "intelligent_reaction_probability")
    assert config is not None
    assert "0 to 100" in str(config)
    assert callable(config["handler"])


def test_failed_save_is_reported_without_a_false_success(monkeypatch):
    from backend.bot.handlers import misc

    edits = []

    class Helper:
        async def edit_message(self, chat_id, msg_id, text):
            edits.append(text)

    monkeypatch.setattr(settings_service, "set_intelligent_reaction_probability", lambda v: False)
    monkeypatch.setattr(misc, "get_client", lambda: Helper())
    monkeypatch.setattr("backend.helper.inline_engine._self_client", None)

    _run(misc._general_intelligent_reaction_probability_handler("40", 1, 2, "c", "m"))
    assert edits and "left unchanged" in edits[0]
    assert "set to 40%" not in edits[0]


# ── 9. migration: probability only, enabled untouched ──────────────────────

def _sql_body() -> str:
    """The executable statements, with the documentation blocks removed."""
    sql = MIGRATION.read_text(encoding="utf-8")
    statements = sql.split("*/", 1)[-1]
    return "\n".join(
        line for line in statements.splitlines()
        if line.strip() and not line.strip().startswith("--")
    )


def test_probability_migration_exists_and_is_probability_only():
    body = _sql_body()
    assert "ADD COLUMN IF NOT EXISTS intelligent_reaction_probability integer;" in body
    assert "SET DEFAULT 25;" in body
    assert "ALTER COLUMN intelligent_reaction_probability SET NOT NULL;" in body
    assert "CHECK (intelligent_reaction_probability >= 0 AND intelligent_reaction_probability <= 100)" in body
    assert "intelligent_reaction_enabled" not in body, (
        "the enabled column already has its own applied migration and must not be touched"
    )
    assert "DROP COLUMN" not in body


def test_probability_migration_leaves_existing_values_alone():
    body = _sql_body()
    assert "WHERE intelligent_reaction_probability IS NULL" in body
    assert "UPDATE panel_settings SET intelligent_reaction_probability = 25 WHERE" in body
    assert "NOTIFY pgrst" not in body


def test_earlier_enabled_migration_is_still_present_and_untouched():
    earlier = MIGRATION.parent / "20261112140000_add_panel_settings_intelligent_reaction.sql"
    text = earlier.read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS intelligent_reaction_enabled boolean;" in text
    assert "intelligent_reaction_probability" not in text


# ── dynamic request coalescing ──────────────────────────────────────────────
#
# These tests synchronise on events and on the coalescer's own state, never on
# wall-clock sleeps, so they prove the protocol rather than a timing accident.

def _batch_answer(count: int, reaction: str = "👍") -> str:
    return json.dumps({"results": [{"id": i, "reaction": reaction} for i in range(1, count + 1)]})


def _payload(messages) -> dict:
    return json.loads(messages[1]["content"])


def _prompt_count(messages) -> int:
    payload = _payload(messages)
    return len(payload["messages"]) if "messages" in payload else 1


async def _until(predicate, attempts: int = 10000) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not reached")


class _GateManager:
    """ProviderManager fake whose calls block until the test releases them."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list = []
        self.in_flight = 0
        self.max_in_flight = 0
        self._gates: list = []
        self._changed = asyncio.Event()

    async def chat(self, messages, **kwargs):
        self.calls.append(messages)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        gate = asyncio.Event()
        self._gates.append(gate)
        self._changed.set()
        try:
            await gate.wait()
        finally:
            self.in_flight -= 1
        if not self.answers:
            raise AssertionError("unexpected extra AI call")
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return FakeResponse(answer if isinstance(answer, str) else json.dumps(answer))

    async def wait_for_call(self, count: int) -> None:
        while len(self.calls) < count:
            await self._changed.wait()
            self._changed.clear()

    def release(self, index: int) -> None:
        self._gates[index].set()


class _AutoManager:
    """Fast provider that answers any prompt and records concurrency."""

    def __init__(self):
        self.calls: list = []
        self.in_flight = 0
        self.max_in_flight = 0

    async def chat(self, messages, **kwargs):
        self.calls.append(messages)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        await asyncio.sleep(0)
        self.in_flight -= 1
        count = _prompt_count(messages)
        if count == 1 and "messages" not in _payload(messages):
            return FakeResponse(json.dumps({"reaction": "👍"}))
        return FakeResponse(_batch_answer(count))


async def _submit(message, manager, *, draw: float = 0.0):
    return await engine.process_outgoing_message(
        owner_id=OWNER,
        client=object(),
        message=message,
        manager=manager,
        draw=lambda: draw,
    )


def _msg(msg_id: int, text: str = "hello", **overrides) -> dict:
    return _message(id=msg_id, text=text, **overrides)


def _coalescer_state():
    return engine._scope_for(OWNER)


def test_first_message_starts_immediately_and_a_follower_only_queues(monkeypatch):
    _settings(monkeypatch, probability=100)
    _catalog(monkeypatch)
    sent = _send(monkeypatch)

    async def main():
        manager = _GateManager(json.dumps({"reaction": "👍"}), json.dumps({"reaction": "🔥"}))
        first = asyncio.create_task(_submit(_msg(MSG, "a"), manager))
        await manager.wait_for_call(1)
        assert len(manager.calls) == 1

        second = asyncio.create_task(_submit(_msg(MSG + 1, "b"), manager))
        await _until(lambda: len(_coalescer_state().pending) == 1)
        assert len(manager.calls) == 1, "a follower must not start a concurrent request"

        manager.release(0)
        await manager.wait_for_call(2)
        assert _prompt_count(manager.calls[1]) == 1
        manager.release(1)

        assert (await first)["status"] == engine.STATUS_REACTED
        assert (await second)["status"] == engine.STATUS_REACTED
        assert manager.max_in_flight == 1
        assert [call[3] for call in sent] == [MSG, MSG + 1]
        assert _coalescer_state().leader is False and _coalescer_state().pending == []

    asyncio.run(main())


def test_messages_during_a_request_share_the_next_request(monkeypatch):
    _settings(monkeypatch, probability=100)
    _catalog(monkeypatch)
    sent = _send(monkeypatch)

    async def main():
        manager = _GateManager(
            json.dumps({"reaction": "👍"}),
            json.dumps({"results": [
                {"id": 1, "reaction": "🎉"},
                {"id": 2, "reaction": "🔥"},
                {"id": 3, "reaction": None},
            ]}),
        )
        first = asyncio.create_task(_submit(_msg(MSG, "m0"), manager))
        await manager.wait_for_call(1)
        followers = [
            asyncio.create_task(_submit(_msg(MSG + index, f"m{index}"), manager))
            for index in range(1, 4)
        ]
        await _until(lambda: len(_coalescer_state().pending) == 3)
        manager.release(0)
        await manager.wait_for_call(2)
        assert _prompt_count(manager.calls[1]) == 3, "the queued messages must share one request"
        assert _payload(manager.calls[1])["messages"][0]["message"] == "m1"
        manager.release(1)

        results = await asyncio.gather(first, *followers)
        assert [item["status"] for item in results] == [
            engine.STATUS_REACTED,
            engine.STATUS_REACTED,
            engine.STATUS_REACTED,
            engine.STATUS_NO_SEMANTIC_REACTION,
        ]
        assert [(call[2], call[3], call[4]["emoji"]) for call in sent] == [
            (CHAT, MSG, "👍"),
            (CHAT, MSG + 1, "🎉"),
            (CHAT, MSG + 2, "🔥"),
        ]
        assert manager.max_in_flight == 1

    asyncio.run(main())


def test_messages_during_the_second_batch_form_a_third_batch(monkeypatch):
    _settings(monkeypatch, probability=100)
    _catalog(monkeypatch)
    _send(monkeypatch)

    async def main():
        manager = _GateManager(
            json.dumps({"reaction": "👍"}),
            _batch_answer(2),
            _batch_answer(2),
        )
        first = asyncio.create_task(_submit(_msg(MSG, "m0"), manager))
        await manager.wait_for_call(1)
        second = asyncio.create_task(_submit(_msg(MSG + 1, "m1"), manager))
        third = asyncio.create_task(_submit(_msg(MSG + 2, "m2"), manager))
        await _until(lambda: len(_coalescer_state().pending) == 2)
        manager.release(0)
        await manager.wait_for_call(2)
        assert _prompt_count(manager.calls[1]) == 2

        fourth = asyncio.create_task(_submit(_msg(MSG + 3, "m3"), manager))
        fifth = asyncio.create_task(_submit(_msg(MSG + 4, "m4"), manager))
        await _until(lambda: len(_coalescer_state().pending) == 2)
        manager.release(1)
        await manager.wait_for_call(3)
        assert _prompt_count(manager.calls[2]) == 2, "later arrivals form the third request"
        manager.release(2)

        results = await asyncio.gather(first, second, third, fourth, fifth)
        assert [item["status"] for item in results] == [engine.STATUS_REACTED] * 5
        assert len(manager.calls) == 3
        assert manager.max_in_flight == 1

    asyncio.run(main())


def test_no_artificial_delay_or_timer_exists_in_the_coalescer():
    source = Path(engine.__file__).read_text()
    body = source.split("# ── dynamic coalescing", 1)[1]
    for forbidden in ("asyncio.sleep", "time.sleep", "call_later", "debounce"):
        assert forbidden not in body, forbidden


def test_null_and_unsupported_results_fail_only_their_own_message(monkeypatch):
    _settings(monkeypatch, probability=100)
    _catalog(monkeypatch)
    sent = _send(monkeypatch)

    async def main():
        manager = _GateManager(
            json.dumps({"reaction": "👍"}),
            json.dumps({"results": [
                {"id": 1, "reaction": "🎉"},
                {"id": 2, "reaction": None},
                {"id": 3, "reaction": "🦄"},
            ]}),
        )
        first = asyncio.create_task(_submit(_msg(MSG, "m0"), manager))
        await manager.wait_for_call(1)
        followers = [
            asyncio.create_task(_submit(_msg(MSG + index, f"m{index}"), manager))
            for index in range(1, 4)
        ]
        await _until(lambda: len(_coalescer_state().pending) == 3)
        manager.release(0)
        await manager.wait_for_call(2)
        manager.release(1)

        results = await asyncio.gather(first, *followers)
        assert [item["status"] for item in results] == [
            engine.STATUS_REACTED,
            engine.STATUS_REACTED,
            engine.STATUS_NO_SEMANTIC_REACTION,
            engine.STATUS_UNSUPPORTED_REACTION,
        ]
        assert [call[3] for call in sent] == [MSG, MSG + 1]

    asyncio.run(main())


@pytest.mark.parametrize(
    "answer",
    [
        json.dumps({"results": [
            {"id": 1, "reaction": "👍"},
            {"id": 9, "reaction": "👍"},
        ]}),
        json.dumps({"results": [
            {"id": 1, "reaction": "👍"},
            {"id": 1, "reaction": "👍"},
        ]}),
        json.dumps({"results": [{"id": 1, "reaction": "👍"}]}),
        json.dumps({"results": [{"probability": 100, "reaction": "👍"}]}),
        json.dumps({"probability": 100, "reaction": "👍"}),
        json.dumps({"results": [{"id": 1, "reaction": "👍", "probability": 100}]}),
        "not json at all",
    ],
)
def test_an_unmappable_batch_answer_fails_every_message(monkeypatch, answer):
    _settings(monkeypatch, probability=100)
    _catalog(monkeypatch)
    sent = _send(monkeypatch)

    async def main():
        manager = _GateManager(json.dumps({"reaction": "👍"}), answer)
        first = asyncio.create_task(_submit(_msg(MSG, "a"), manager))
        await manager.wait_for_call(1)
        followers = [
            asyncio.create_task(_submit(_msg(MSG + index, f"m{index}"), manager))
            for index in range(1, 3)
        ]
        await _until(lambda: len(_coalescer_state().pending) == 2)
        manager.release(0)
        await manager.wait_for_call(2)
        manager.release(1)
        results = await asyncio.gather(first, *followers)
        assert results[0]["status"] == engine.STATUS_REACTED
        assert results[1]["status"] == engine.STATUS_INVALID_AI_RESPONSE
        assert results[2]["status"] == engine.STATUS_INVALID_AI_RESPONSE
        assert [call[3] for call in sent] == [MSG]

    asyncio.run(main())


def test_a_failed_request_does_not_block_the_next_message(monkeypatch):
    _settings(monkeypatch, probability=100)
    _catalog(monkeypatch)
    _send(monkeypatch)

    async def main():
        manager = _GateManager(RuntimeError("provider exploded"), json.dumps({"reaction": "👍"}))
        first = asyncio.create_task(_submit(_msg(MSG, "a"), manager))
        await manager.wait_for_call(1)
        manager.release(0)
        assert (await first)["status"] == engine.STATUS_AI_FAILURE
        assert _coalescer_state().leader is False and _coalescer_state().pending == []

        second = asyncio.create_task(_submit(_msg(MSG + 1, "b"), manager))
        await manager.wait_for_call(2)
        manager.release(1)
        assert (await second)["status"] == engine.STATUS_REACTED

    asyncio.run(main())


def test_a_cancelled_leader_settles_followers_and_unblocks_later_messages(monkeypatch):
    _settings(monkeypatch, probability=100)
    _catalog(monkeypatch)
    sent = _send(monkeypatch)

    async def main():
        manager = _GateManager(json.dumps({"reaction": "👍"}))
        first = asyncio.create_task(_submit(_msg(MSG, "a"), manager))
        await manager.wait_for_call(1)
        second = asyncio.create_task(_submit(_msg(MSG + 1, "b"), manager))
        await _until(lambda: len(_coalescer_state().pending) == 1)

        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

        interrupted = await second
        assert interrupted["status"] == engine.STATUS_AI_FAILURE
        assert "interrupted" in (interrupted["error"] or "")
        assert _coalescer_state().leader is False and _coalescer_state().pending == []
        assert sent == []

        recovered = await _submit(_msg(MSG + 2, "c"), FakeManager(json.dumps({"reaction": "👍"})))
        assert recovered["status"] == engine.STATUS_REACTED
        assert [call[3] for call in sent] == [MSG + 2]

    asyncio.run(main())


def test_a_duplicate_update_while_queued_is_still_rejected(monkeypatch):
    _settings(monkeypatch, probability=100)
    _catalog(monkeypatch)
    _send(monkeypatch)

    async def main():
        manager = _GateManager(json.dumps({"reaction": "👍"}))
        first = asyncio.create_task(_submit(_msg(MSG, "a"), manager))
        await manager.wait_for_call(1)
        duplicate = await _submit(_msg(MSG, "a"), manager)
        assert duplicate["status"] == engine.STATUS_DUPLICATE
        manager.release(0)
        assert (await first)["status"] == engine.STATUS_REACTED
        assert len(manager.calls) == 1

    asyncio.run(main())


def test_the_batch_bound_splits_a_burst_without_losing_messages(monkeypatch):
    _settings(monkeypatch, probability=100)
    _catalog(monkeypatch)
    sent = _send(monkeypatch)
    monkeypatch.setattr(engine, "MAX_BATCH_MESSAGES", 2)

    async def main():
        manager = _GateManager(
            json.dumps({"reaction": "👍"}), _batch_answer(2), json.dumps({"reaction": "👍"}),
        )
        first = asyncio.create_task(_submit(_msg(MSG, "m0"), manager))
        await manager.wait_for_call(1)
        followers = [
            asyncio.create_task(_submit(_msg(MSG + index, f"m{index}"), manager))
            for index in range(1, 4)
        ]
        await _until(lambda: len(_coalescer_state().pending) == 3)
        manager.release(0)
        await manager.wait_for_call(2)
        assert _prompt_count(manager.calls[1]) == 2
        manager.release(1)
        await manager.wait_for_call(3)
        assert _prompt_count(manager.calls[2]) == 1, "the overflow is the next request"
        manager.release(2)
        results = await asyncio.gather(first, *followers)
        assert [item["status"] for item in results] == [engine.STATUS_REACTED] * 4
        assert len(sent) == 4

    asyncio.run(main())


def test_a_catalog_outage_fails_the_batch_without_an_ai_call(monkeypatch):
    _settings(monkeypatch, probability=100)
    async def down(client):
        return {"ok": False, "error": "E_TELEGRAM", "detail": "down", "options": []}
    monkeypatch.setattr(reaction_source_service, "load_reactions", down)
    _forbid_send(monkeypatch)
    _send(monkeypatch)

    async def main():
        manager = _GateManager()
        results = await asyncio.gather(
            _submit(_msg(MSG, "a"), manager),
            _submit(_msg(MSG + 1, "b"), manager),
        )
        assert [item["status"] for item in results] == [engine.STATUS_CATALOG_UNAVAILABLE] * 2
        assert manager.calls == []

    asyncio.run(main())


def test_probability_zero_never_queues_and_never_calls_ai(monkeypatch):
    _settings(monkeypatch, probability=0)
    _forbid_catalog(monkeypatch)
    _forbid_send(monkeypatch)

    async def main():
        manager = _GateManager()
        result = await _submit(_msg(MSG, "a"), manager)
        assert result["status"] == engine.STATUS_PROBABILITY_ZERO
        assert manager.calls == []

    asyncio.run(main())


def test_a_sustained_burst_never_overlaps_requests_and_loses_nothing(monkeypatch):
    _settings(monkeypatch, probability=100)
    _catalog(monkeypatch)
    sent = _send(monkeypatch)

    async def main():
        manager = _AutoManager()
        messages = [_msg(1000 + index, f"<m{index:02d}>") for index in range(25)]
        results = await asyncio.gather(*(
            _submit(message, manager) for message in messages
        ))
        assert manager.max_in_flight == 1, "only one AI request may be in flight"
        assert [item["status"] for item in results] == [engine.STATUS_REACTED] * 25
        prompts = "\n".join(call[1]["content"] for call in manager.calls)
        for index in range(25):
            assert prompts.count(f"<m{index:02d}>") == 1, f"message {index} lost or duplicated"
        assert len(sent) == 25
        assert _coalescer_state().leader is False and _coalescer_state().pending == []

    asyncio.run(main())
