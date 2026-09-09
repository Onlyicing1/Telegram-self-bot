"""Broad semantic trigger/schedule interpretation regression tests.

Exercises the REAL creation path (CreateTaskTool → TaskInterpreter → scripted
provider → TaskCandidate → deterministic validation → repository) for the
exact live-failing multi-line Persian request, the interval matrix
(seconds/minutes/hours/days/weeks, digits + number words, Persian + English,
compound durations), time-of-day/daily/weekly triggers, message/self-message/
content/media/mention event triggers, honest unsupported-capability results
(monthly/yearly recurrence), and the deterministic event matcher.

The scripted provider plays the role the interpreter prompt contract assigns
the model: it returns compliant structured candidates (or the documented
{\"unsupported\": ...} envelope). Deterministic layers are the real code.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.ai.providers.base.capabilities import ProviderCapabilities
from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.base.contract import BaseProvider, ProviderResponse
from backend.ai.providers.manager.manager import ProviderManager
from backend.ai.tools.context import ToolContext

OWNER = 777
CHAT_ID = -1001
TZ = "Asia/Tehran"

LIVE_PERSIAN_REQUEST = (
    "هر ۵ دقیقه\n"
    "میخوام بیو پروفایلم رو آپدیت کنید\n"
    "یه دیالوگ رندوم از کاراکتر آیانامی ری از انیمه نئون جنسیس بزاری\n"
    "که زیر 60 کاراکتر باشه"
)


class _FakeProvider(BaseProvider):
    def __init__(self, response_text: str):
        super().__init__(ProviderConfig(provider_name="fake", enabled=True, default_model="m"))
        self.response_text = response_text
        self.calls = 0
        self.last_messages = None

    @property
    def name(self) -> str:
        return "fake"

    @property
    def capabilities(self):
        return ProviderCapabilities(supports_tools=True, supports_function_call=True)

    async def chat(self, messages, **kwargs):
        self.calls += 1
        self.last_messages = messages
        return ProviderResponse(text=self.response_text, provider_name="fake", success=True)

    def initialize(self):
        return None

    def shutdown(self):
        return None

    def count_tokens(self, text):
        return max(1, len(text) // 4)

    def health(self):
        return {"healthy": True}


def _provider_manager(response_text: str) -> tuple[ProviderManager, _FakeProvider]:
    pm = ProviderManager()
    provider = _FakeProvider(response_text)
    pm.register_provider(provider)
    pm.switch_provider("fake")
    pm._fallback_chain = []
    return pm, provider


def _ctx(pm: ProviderManager) -> ToolContext:
    return ToolContext(
        telegram=None, owner_id=OWNER, tz_str=TZ, client=None,
        extra={"provider_manager": pm, "chat_id": CHAT_ID},
    )


def _candidate(
    schedule_type: str,
    schedule: dict,
    *,
    actions=None,
    ai_instruction: str | None = None,
    label: str = "Task",
    destination: dict | None = None,
) -> str:
    value = {
        "label": label,
        "schedule_type": schedule_type,
        "schedule": schedule,
        "timezone": TZ,
        "actions": actions or [{"name": "send_message", "arguments": {"text": "x"}}],
        "notification_destination": destination or {},
    }
    if ai_instruction is not None:
        value["ai_instruction"] = ai_instruction
    return json.dumps(value, ensure_ascii=False)


async def _create(pm: ProviderManager, request: str):
    from backend.ai.database import manager as dbm
    from backend.ai.tools.task import CreateTaskTool

    manager = dbm.RepositoryManager(supabase_available=False)
    with patch.object(dbm, "get_repository_manager", return_value=manager):
        tool = CreateTaskTool(_ctx(pm))
        result = await tool.execute(_ctx(pm), {"request": request})
    return result, manager


async def _tasks(manager):
    return await manager.task.list_tasks(OWNER)


# ═══════════════════════ 1. THE EXACT LIVE FAILURE ═══════════════════════


@pytest.mark.asyncio
async def test_exact_failing_persian_request_creates_semantic_task():
    """THE regression: the exact multi-line request that produced the live
    'safe, unambiguous schedule' rejection must create a 300s interval bio
    task when the model follows the semantic contract."""
    from backend.ai.preparation_policy import derive_policy

    compliant = _candidate(
        "interval", {"seconds": 300},
        actions=[{"name": "bio_set_text", "arguments": {"text": ""}}],
        ai_instruction=LIVE_PERSIAN_REQUEST, label="Bio update",
    )
    pm, provider = _provider_manager(compliant)
    result, manager = await _create(pm, LIVE_PERSIAN_REQUEST)

    assert result.success is True, result.message
    task = (await _tasks(manager))[0]
    assert task.schedule_type == "interval"
    assert task.schedule == {"seconds": 300.0}
    assert task.ai_instruction == LIVE_PERSIAN_REQUEST  # verbatim, multi-line
    assert task.actions == [{"name": "bio_set_text", "arguments": {"text": ""}}]
    policy = derive_policy(task.ai_instruction)
    assert policy.source == "آیانامی ری"
    assert policy.max_length == 59


@pytest.mark.asyncio
async def test_english_equivalent_creates_same_semantic_task():
    request = (
        "Every 5 minutes, change my bio to a random Rei Ayanami dialogue. "
        "It must be under 60 characters."
    )
    compliant = _candidate(
        "interval", {"seconds": 300},
        actions=[{"name": "bio_set_text", "arguments": {"text": ""}}],
        ai_instruction=request, label="Bio update",
    )
    pm, _ = _provider_manager(compliant)
    result, manager = await _create(pm, request)
    assert result.success is True, result.message
    task = (await _tasks(manager))[0]
    assert task.schedule == {"seconds": 300.0}
    assert task.ai_instruction == request


# ═══════════════════════ 2. INTERVAL MATRIX ═══════════════════════


@pytest.mark.parametrize("phrase,seconds", [
    ("every 5 seconds", 5),
    ("every five seconds", 5),
    ("هر 5 ثانیه", 5),
    ("هر پنج ثانیه", 5),
    ("every 5 minutes", 300),
    ("every five minutes", 300),
    ("هر 5 دقیقه", 300),
    ("هر پنج دقیقه", 300),
    ("پنج دقیقه یکبار", 300),
    ("هر 5 دقیقه یه بار", 300),
    ("every 2 hours", 7200),
    ("every two hours", 7200),
    ("هر 2 ساعت", 7200),
    ("هر دو ساعت", 7200),
    ("every day", 86400),
    ("daily", 86400),
    ("هر روز", 86400),
    ("روزانه", 86400),
    ("every week", 604800),
    ("weekly", 604800),
    ("هر هفته", 604800),
    ("هفتگی", 604800),
    ("every half hour", 1800),
    ("هر نیم ساعت", 1800),
])
@pytest.mark.asyncio
async def test_interval_matrix_creates_correct_duration(phrase, seconds):
    pm, _ = _provider_manager(_candidate("interval", {"seconds": seconds}))
    result, manager = await _create(pm, phrase)
    assert result.success is True, f"{phrase!r}: {result.message}"
    task = (await _tasks(manager))[0]
    assert task.schedule == {"seconds": float(seconds)}, phrase


@pytest.mark.parametrize("phrase,seconds", [
    ("every 1 hour and 30 minutes", 5400),
    ("every 2 days and 6 hours", 205200),
    ("هر 1 ساعت و 30 دقیقه", 5400),
    ("هر 2 روز و 6 ساعت", 205200),
    ("هر یک ساعت و نیم", 5400),
])
@pytest.mark.asyncio
async def test_compound_intervals_canonicalize(phrase, seconds):
    pm, _ = _provider_manager(_candidate("interval", {"seconds": seconds}))
    result, manager = await _create(pm, phrase)
    assert result.success is True, result.message
    assert (await _tasks(manager))[0].schedule == {"seconds": float(seconds)}


@pytest.mark.asyncio
async def test_compound_structured_shape_sums_deterministically():
    """A model may emit the structured compound form; the deterministic
    canonicalizer sums known units (1h30m → 5400s)."""
    pm, _ = _provider_manager(_candidate("interval", {"hours": 1, "minutes": 30}))
    result, manager = await _create(pm, "every 1 hour and 30 minutes")
    assert result.success is True, result.message
    assert (await _tasks(manager))[0].schedule == {"seconds": 5400.0}


# ═══════════════════════ 3. MONTHLY / YEARLY → HONEST UNSUPPORTED ═══════════════════════


@pytest.mark.parametrize("phrase", [
    "every month do X", "monthly do X", "هر ماه انجام بده", "ماهانه انجام بده",
    "every year do X", "yearly do X", "annually do X",
    "هر سال انجام بده", "سالانه انجام بده",
    "on the first of every month do X",
])
@pytest.mark.asyncio
async def test_calendar_monthly_yearly_is_honest_unsupported(phrase):
    """Semantically clear but unrepresentable recurrence must produce the
    explicit unsupported-capability response — never the ambiguity rejection,
    never fabricated seconds."""
    pm, _ = _provider_manager(json.dumps({"unsupported": "monthly/yearly recurrence"}))
    result, manager = await _create(pm, phrase)
    assert result.success is False
    assert "not supported yet" in result.message
    assert "could not turn that into a safe, unambiguous schedule" not in result.message
    assert await _tasks(manager) == []


# ═══════════════════════ 4. TIME-OF-DAY / CALENDAR TRIGGERS ═══════════════════════


@pytest.mark.parametrize("phrase,schedule_type,schedule", [
    ("every day at 9", "daily", {"hour": 9, "minute": 0, "timezone": TZ}),
    ("every night at 11", "daily", {"hour": 23, "minute": 0, "timezone": TZ}),
    ("هر روز ساعت 9", "daily", {"hour": 9, "minute": 0, "timezone": TZ}),
    ("every Monday at 10", "weekly", {"weekday": 0, "hour": 10, "minute": 0, "timezone": TZ}),
    ("every Friday at 8 PM", "weekly", {"weekday": 4, "hour": 20, "minute": 0, "timezone": TZ}),
    ("هر دوشنبه ساعت 10", "weekly", {"weekday": 0, "hour": 10, "minute": 0, "timezone": TZ}),
    ("هر جمعه ساعت 8 شب", "weekly", {"weekday": 4, "hour": 20, "minute": 0, "timezone": TZ}),
    ("today at 5", "once", {"at": "2026-09-09T17:00:00", "timezone": TZ}),
    ("tomorrow at 8 AM", "once", {"at": "2026-09-10T08:00:00", "timezone": TZ}),
])
@pytest.mark.asyncio
async def test_time_of_day_triggers_are_representable(phrase, schedule_type, schedule):
    """The model maps the semantic phrase to the structured schedule; the
    deterministic layers accept and persist it unchanged (shape-validated)."""
    pm, _ = _provider_manager(_candidate(schedule_type, schedule))
    result, manager = await _create(pm, phrase)
    assert result.success is True, f"{phrase!r}: {result.message}"
    task = (await _tasks(manager))[0]
    assert task.schedule_type == schedule_type
    assert task.schedule == schedule


# ═══════════════════════ 5. MESSAGE / SELF / CONTENT / MEDIA / MENTION TRIGGERS ═══════════════════════


@pytest.mark.parametrize("phrase,trigger", [
    (
        "when I write start",
        {"type": "telegram_message", "direction": "outgoing", "text_equals": "start"},
    ),
    (
        "when I type /start",
        {"type": "telegram_message", "direction": "outgoing", "text_equals": "/start"},
    ),
    (
        "وقتی من نوشتم شروع",
        {"type": "telegram_message", "direction": "outgoing", "contains": ["شروع"]},
    ),
    (
        "when someone sends me a message containing urgent",
        {"type": "telegram_message", "direction": "incoming", "contains": ["urgent"]},
    ),
    (
        "when someone mentions me",
        {"type": "telegram_message", "direction": "incoming", "is_mention": True},
    ),
    (
        "وقتی کسی منو منشن کرد",
        {"type": "telegram_message", "direction": "incoming", "is_mention": True},
    ),
    (
        "when someone replies",
        {"type": "telegram_message", "direction": "incoming", "is_reply": True},
    ),
])
@pytest.mark.asyncio
async def test_event_triggers_persist_semantic_conditions(phrase, trigger):
    """Self-message, content-match, mention, and reply triggers survive the
    real creation path with their semantic conditions intact (names resolve
    at runtime; no ids here)."""
    pm, _ = _provider_manager(_candidate("event", {"trigger": trigger}))
    result, manager = await _create(pm, phrase)
    assert result.success is True, f"{phrase!r}: {result.message}"
    task = (await _tasks(manager))[0]
    assert task.schedule_type == "event"
    stored = task.schedule["trigger"]
    for key, value in trigger.items():
        assert stored.get(key) == value, f"{phrase!r}: {stored}"


@pytest.mark.asyncio
async def test_media_type_trigger_with_this_chat_resolves_trusted_chat_id():
    """'when someone sends a photo in this chat' → incoming + media_type=photo
    + the trusted request chat id (never a model-supplied number)."""
    trigger = {
        "type": "telegram_message", "direction": "incoming",
        "chat": "this chat", "media_type": "photo",
    }
    pm, _ = _provider_manager(_candidate("event", {"trigger": trigger}))
    result, manager = await _create(pm, "when someone sends a photo in this chat")
    assert result.success is True, result.message
    stored = (await _tasks(manager))[0].schedule["trigger"]
    assert stored["chat_id"] == CHAT_ID
    assert stored["media_type"] == "photo"
    assert stored["direction"] == "incoming"


@pytest.mark.asyncio
async def test_unresolvable_sender_fails_closed_honestly():
    """A named sender that cannot be resolved against the trusted dialogs is
    an honest failure, not a fabricated trigger."""
    trigger = {"type": "telegram_message", "direction": "incoming", "sender": "Nobody-Real-42"}
    pm, _ = _provider_manager(_candidate("event", {"trigger": trigger}))
    result, manager = await _create(pm, "when Nobody-Real-42 messages me")
    assert result.success is False
    assert "Could not resolve the sender" in result.message
    assert await _tasks(manager) == []


# ═══════════════════════ 6. GENUINE AMBIGUITY STAYS REJECTED ═══════════════════════


@pytest.mark.parametrize("phrase", [
    "یه وقتایی انجامش بده",
    "some time later",
    "occasionally do it",
])
@pytest.mark.asyncio
async def test_genuine_ambiguity_keeps_the_honest_rejection(phrase):
    pm, _ = _provider_manager("null")
    result, manager = await _create(pm, phrase)
    assert result.success is False
    assert "could not turn that into a safe, unambiguous schedule" in result.message
    assert "not supported yet" not in result.message
    assert await _tasks(manager) == []


# ═══════════════════════ 7. DETERMINISTIC EVENT MATCHER (media type + mention) ═══════════════════════


def _event(**overrides):
    base = {
        "chat_id": -1001, "sender_id": 5, "text": "hi",
        "has_media": False, "media_type": None, "is_reply": False,
        "mentioned": False, "out": False,
    }
    base.update(overrides)
    return base


def test_media_type_matcher_distinguishes_photo_from_video():
    from backend.ai.task_trigger import event_trigger_matches

    photo_trigger = {"type": "telegram_message", "direction": "incoming", "media_type": "photo"}
    assert event_trigger_matches(photo_trigger, _event(has_media=True, media_type="photo")) is True
    assert event_trigger_matches(photo_trigger, _event(has_media=True, media_type="video")) is False
    assert event_trigger_matches(photo_trigger, _event(has_media=True, media_type=None)) is False


def test_mention_matcher_requires_mention_flag():
    from backend.ai.task_trigger import event_trigger_matches

    mention_trigger = {"type": "telegram_message", "direction": "incoming", "is_mention": True}
    assert event_trigger_matches(mention_trigger, _event(mentioned=True)) is True
    assert event_trigger_matches(mention_trigger, _event(mentioned=False)) is False
    any_trigger = {"type": "telegram_message", "direction": "any"}
    assert event_trigger_matches(any_trigger, _event(mentioned=True)) is True


def test_media_type_derivation_from_message_object():
    from backend.ai.task_event_dispatcher import extract_event_context

    msg = SimpleNamespace(id=1, message="", media=object(), reply_to_msg_id=None, date=None)
    assert extract_event_context(SimpleNamespace(
        message=msg, chat_id=-1, sender_id=5, out=False, raw_text=""
    ))["media_type"] == "other"

    for kind in ("photo", "video", "voice", "audio", "sticker", "animation", "document"):
        msg = SimpleNamespace(id=1, message="", media=object(), reply_to_msg_id=None, date=None)
        setattr(msg, kind, object())
        ctx = extract_event_context(SimpleNamespace(
            message=msg, chat_id=-1, sender_id=5, out=False, raw_text=""
        ))
        assert ctx["media_type"] == kind

    ctx = extract_event_context(SimpleNamespace(
        message=SimpleNamespace(id=1, message="", media=None, reply_to_msg_id=None, date=None),
        chat_id=-1, sender_id=5, out=False, raw_text="", mentioned=True,
    ))
    assert ctx["media_type"] is None
    assert ctx["mentioned"] is True


def test_media_type_and_mention_spec_validation_is_bounded():
    from backend.ai.task_trigger import TaskTriggerError, validate_trigger_spec

    spec = validate_trigger_spec({
        "type": "telegram_message", "direction": "incoming",
        "media_type": "photo", "is_mention": True,
    })
    assert spec["media_type"] == "photo" and spec["is_mention"] is True

    with pytest.raises(TaskTriggerError):
        validate_trigger_spec({"type": "telegram_message", "media_type": "gif"})
    with pytest.raises(TaskTriggerError):
        validate_trigger_spec({"type": "telegram_message", "is_mention": "yes"})


def test_trigger_summary_includes_media_and_mention():
    from backend.ai.task_trigger import trigger_summary

    text = trigger_summary({
        "type": "telegram_message", "direction": "incoming",
        "media_type": "photo", "is_mention": True,
    })
    assert "media: photo" in text
    assert "is a mention" in text


# ═══════════════════════ 8. PROMPT CONTRACT CARRIES THE NEW SEMANTICS ═══════════════════════


@pytest.mark.asyncio
async def test_interpreter_prompt_carries_trigger_semantics():
    from backend.ai.task_interpreter import TaskInterpreter

    pm, provider = _provider_manager("null")
    interpreter = TaskInterpreter(pm)
    with pytest.raises(Exception):
        await interpreter.interpret(LIVE_PERSIAN_REQUEST, timezone=TZ)
    system = provider.last_messages[0]["content"]
    for needle in (
        "SEMANTIC INTERPRETATION", "NULL RULE", "UNSUPPORTED CAPABILITY",
        "COMPOUND INTERVALS", "TIME-OF-DAY", "WEEKDAY NUMBERS",
        "media_type", "is_mention", "EXAMPLE", "هر ۵ دقیقه",
    ):
        assert needle in system, needle


def test_interpreter_surfaces_unsupported_envelope():
    from backend.ai.task_interpreter import TaskInterpreter, TaskUnsupportedError

    pm, _ = _provider_manager(json.dumps({"unsupported": "monthly recurrence"}))
    with pytest.raises(TaskUnsupportedError) as exc_info:
        asyncio.run(TaskInterpreter(pm).interpret("هر ماه انجام بده", timezone=TZ))
    assert exc_info.value.capability == "monthly recurrence"