"""G2-A — the ONE language catalog, resolver and its integrations.

Contract pinned here:

  1. One catalog (en/fa/ar/zh) with stable codes, labels and directionality.
  2. Deterministic precedence: request > task > global preference > English.
  3. ``auto`` is explicit only, never inferred, and its fallback is honest.
  4. The dispatcher resolves ONCE per request and every downstream reader uses
     that same snapshot.
  5. History summaries and translations receive the resolved language, both map
     and reduce prompts, and report the effective language in their metadata.
  6. A task freezes the global default at creation; an explicit task language
     always wins and preparation_policy still rejects mismatches.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from backend.ai import language as language_ai
from backend.ai import task_wizard
from backend.ai.conversation.context_builder import PreferencesContext
from backend.ai.database.manager import get_repository_manager
from backend.ai.database.task_repository import InMemoryTaskRepository
from backend.ai.engine.dispatcher import Dispatcher
from backend.ai.engine.engine import Engine
from backend.ai.providers.base.capabilities import ProviderCapabilities
from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.base.contract import BaseProvider, ProviderResponse
from backend.ai.providers.manager.manager import ProviderManager
from backend.ai.providers.registry.registry import ProviderRegistry
from backend.ai.preparation_policy import (
    PreparationPolicyError,
    derive_policy,
    validate_content,
)
from backend.ai.session.request import AIRequest
from backend.ai.task_creation import TaskCreationService
from backend.ai.tools.context import ToolContext
from backend.bot.handlers import history as history_panel
from backend.services import history_ai_service, history_service

OWNER = 424242


@pytest.fixture(autouse=True)
def _reset_global_language():
    """Keep the process-wide preferences record deterministic per test."""
    repo = get_repository_manager().preferences
    repo.update(OWNER, {"language": "English"})
    yield
    repo.update(OWNER, {"language": "English"})


# ── 1. Catalog ────────────────────────────────────────────────────────────


def test_catalog_has_the_required_and_existing_languages():
    assert "en" in language_ai.BY_CODE and "fa" in language_ai.BY_CODE
    assert language_ai.TASK_CODES == ("en", "fa", "ar", "zh")
    assert language_ai.get("fa").direction == "rtl"
    assert language_ai.get("en").direction == "ltr"
    assert language_ai.get("fa").label == "Persian"
    assert language_ai.provider_hint("fa")


def test_external_values_validate_against_the_catalog():
    assert language_ai.normalize("English") == "en"
    assert language_ai.normalize("fa") == "fa"
    assert language_ai.normalize("Persian") == "fa"
    assert language_ai.normalize("farsi") == "fa"
    assert language_ai.normalize("Arabic") == "ar"
    assert language_ai.normalize("Chinese") == "zh"
    assert language_ai.normalize("") == ""
    assert language_ai.normalize(None) == ""
    assert language_ai.normalize("auto") == language_ai.AUTO
    with pytest.raises(language_ai.LanguageError):
        language_ai.normalize("klingon")
    with pytest.raises(language_ai.LanguageError):
        language_ai.normalize(42)


# ── 2. Resolver precedence ────────────────────────────────────────────────


def test_request_beats_task_beats_global_beats_default():
    decision = language_ai.resolve(
        request="fa", task="ar", global_value="zh"
    )
    assert (decision.code, decision.source) == ("fa", "request")

    decision = language_ai.resolve(task="ar", global_value="zh")
    assert (decision.code, decision.source) == ("ar", "task")

    decision = language_ai.resolve(global_value="zh")
    assert (decision.code, decision.source) == ("zh", "global")

    decision = language_ai.resolve()
    assert (decision.code, decision.source) == ("en", "default")


def test_an_explicit_task_language_cannot_be_overridden_by_the_global_default():
    decision = language_ai.resolve(task="fa", global_value="en")
    assert decision.mode == "fixed"
    assert decision.code == "fa"
    assert decision.source == "task"


def test_unsupported_values_are_rejected_not_coerced():
    with pytest.raises(language_ai.LanguageError):
        language_ai.resolve(request="klingon")


# ── 3. Auto contract ──────────────────────────────────────────────────────


def test_auto_is_explicit_and_reportable():
    decision = language_ai.resolve(request="auto", source_texts=["چطوری گلم"])
    assert decision.mode == "auto"
    assert decision.source == "source"
    assert decision.detected is True
    assert decision.code == "fa"
    assert "auto-detected" in decision.prompt_label


def test_auto_is_never_selected_implicitly():
    decision = language_ai.resolve(global_value="en", source_texts=["چطوری گلم"])
    assert decision.mode == "fixed"
    assert decision.source == "global"
    assert decision.detected is False


def test_auto_without_a_resolvable_source_uses_the_documented_fallback():
    decision = language_ai.resolve(request="auto", source_texts=[])
    assert decision.mode == "auto"
    assert decision.source == "auto_fallback"
    assert decision.code == "en"
    assert decision.detected is False
    assert "fallback" in decision.prompt_label


def test_auto_never_overrides_an_explicit_task_language():
    decision = language_ai.resolve(
        request=None, task="fa", global_value="auto", source_texts=["Hello"]
    )
    assert decision.code == "fa"
    assert decision.source == "task"


def test_source_detection_supports_arabic_and_chinese():
    assert language_ai.detect_source_language(["مرحبا بالعالم"]) == "ar"
    assert language_ai.detect_source_language(["你好世界"]) == "zh"
    assert language_ai.detect_source_language(["hello world"]) == "en"
    assert language_ai.detect_source_language(["abcde", "你好世界"]) == ""


def test_decision_metadata_is_bounded_and_never_user_text():
    decision = language_ai.resolve(request="fa")
    metadata = decision.as_metadata()
    assert metadata["language"] == "Persian"
    assert metadata["language_code"] == "fa"
    assert metadata["language_source"] == "request"
    assert metadata["language_mode"] == "fixed"


# ── 4. AIRequest and dispatcher integration ───────────────────────────────


def test_ai_request_language_is_empty_unless_explicitly_set():
    request = AIRequest(
        session_id="s", user_message="hi", owner_id=OWNER, chat_id=1, message_id=2
    )
    assert request.language == ""
    explicit = AIRequest(
        session_id="s", user_message="hi", owner_id=OWNER, chat_id=1, message_id=2,
        language="Persian",
    )
    assert explicit.language == "Persian"


def test_global_preference_reaches_the_dispatcher_prompt_preferences():
    get_repository_manager().preferences.update(OWNER, {"language": "fa"})
    dispatcher = Dispatcher.__new__(Dispatcher)
    preferences = dispatcher._load_preferences(OWNER)
    assert isinstance(preferences, PreferencesContext)
    assert preferences.language == "Persian"


def test_explicit_request_language_wins_in_the_session_view():
    dispatcher = Dispatcher.__new__(Dispatcher)
    decision = language_ai.resolve(request="fa", global_value="en")
    request = AIRequest(
        session_id="s", user_message="hi", owner_id=OWNER, chat_id=1, message_id=2,
        language="Persian",
        metadata={"language_decision": decision, "global_language": "en"},
    )
    session = SimpleNamespace(
        session_id="s", owner_id=OWNER, tool_history=[], pending_tool=None,
    )
    view = dispatcher._adapt_session(session, request)
    assert view.language == "Persian"


def test_tool_context_carries_the_same_language_snapshot():
    dispatcher = Dispatcher.__new__(Dispatcher)
    dispatcher._provider_manager = None
    dispatcher._tool_executor = SimpleNamespace(
        _context=ToolContext(telegram=None, owner_id=OWNER, tz_str="UTC", client=None),
    )
    decision = language_ai.resolve(global_value="fa")
    request = AIRequest(
        session_id="s", user_message="hi", owner_id=OWNER, chat_id=1, message_id=2,
        metadata={"language_decision": decision, "global_language": "fa"},
    )
    context = dispatcher._build_tool_context(request)
    assert context.extra["global_language"] == "fa"
    assert context.extra["language_decision"] is decision


# ── 5. History ────────────────────────────────────────────────────────────


def _message(mid: int, text: str) -> history_service.HistoryMessage:
    return history_service.HistoryMessage(
        message_id=mid, chat_id=-100, sender_id=1, out=False,
        date=datetime(2026, 1, 1, tzinfo=timezone.utc), text=text,
        reply_to_msg_id=None, has_media=False, ai_provenance=False,
    )


def _plan(*messages: history_service.HistoryMessage) -> history_ai_service.HistoryPlan:
    return history_ai_service.HistoryPlan(
        operation=history_ai_service.SUMMARIZE,
        requested=len(messages),
        count=len(messages),
        capped=False,
        language="",
        instruction="",
        messages=tuple(messages),
        truncated=False,
    )


@pytest.mark.asyncio
async def test_summary_receives_the_resolved_language(monkeypatch):
    plan = _plan(_message(1, "hello"))

    async def _prepare(*_args, **_kwargs):
        return plan

    prompts: list[str] = []

    async def _call(_manager, *, system, payload, request_id, deadline):
        prompts.append(system)
        return "summary"

    monkeypatch.setattr(history_ai_service, "_prepare", _prepare)
    monkeypatch.setattr(history_ai_service, "_resolve_manager", lambda _m: object())
    monkeypatch.setattr(history_ai_service, "_call", _call)

    ok, _text, data = await history_ai_service.summarize_history(
        None, -100, count=1, language="fa", global_language="en",
    )

    assert ok is True
    assert "Write the result in Persian." in prompts[0]
    assert data["language"] == "Persian"
    assert data["language_code"] == "fa"
    assert data["language_source"] == "request"


@pytest.mark.asyncio
async def test_every_map_and_reduce_stage_uses_the_same_language(monkeypatch):
    plan = _plan(_message(1, "one"), _message(2, "two"))

    async def _prepare(*_args, **_kwargs):
        return plan

    def _chunks(_messages, **_kwargs):
        first = SimpleNamespace(lines=["one"], message_ids=[1])
        second = SimpleNamespace(lines=["two"], message_ids=[2])
        return [first, second]

    prompts: list[str] = []

    async def _call(_manager, *, system, payload, request_id, deadline):
        prompts.append(system)
        return "part"

    monkeypatch.setattr(history_ai_service, "_prepare", _prepare)
    monkeypatch.setattr(history_ai_service, "_resolve_manager", lambda _m: object())
    monkeypatch.setattr(history_ai_service, "_chunk_history", _chunks)
    monkeypatch.setattr(history_ai_service, "map_call_capacity", lambda _b: 10)
    monkeypatch.setattr(history_ai_service, "_call", _call)

    ok, _text, data = await history_ai_service.summarize_history(
        None, -100, count=2, language="Persian",
    )

    assert ok is True
    assert len(prompts) == 3  # map, map, reduce
    assert all("Write the result in Persian." in prompt for prompt in prompts)
    assert data["language"] == "Persian"


@pytest.mark.asyncio
async def test_summary_auto_uses_the_source_dominant_language(monkeypatch):
    plan = _plan(_message(1, "چطوری گلم"))

    async def _prepare(*_args, **_kwargs):
        return plan

    prompts: list[str] = []

    async def _call(_manager, *, system, payload, request_id, deadline):
        prompts.append(system)
        return "خلاصه"

    monkeypatch.setattr(history_ai_service, "_prepare", _prepare)
    monkeypatch.setattr(history_ai_service, "_resolve_manager", lambda _m: object())
    monkeypatch.setattr(history_ai_service, "_call", _call)

    ok, _text, data = await history_ai_service.summarize_history(
        None, -100, count=1, language="auto",
    )

    assert ok is True
    assert data["language"] == "Persian"
    assert data["language_mode"] == "auto"
    assert data["language_detected"] is True
    assert "Persian" in prompts[0]


@pytest.mark.asyncio
async def test_translation_honors_an_explicit_language(monkeypatch):
    plan = _plan(_message(1, "hello"))

    async def _prepare(*_args, **_kwargs):
        return plan

    prompts: list[str] = []

    async def _call(_manager, *, system, payload, request_id, deadline):
        prompts.append(system)
        return "[1] سلام"

    monkeypatch.setattr(history_ai_service, "_prepare", _prepare)
    monkeypatch.setattr(history_ai_service, "_resolve_manager", lambda _m: object())
    monkeypatch.setattr(history_ai_service, "_call", _call)

    ok, _text, data = await history_ai_service.translate_history(
        None, -100, count=1, language="fa",
    )

    assert ok is True
    assert "Write the result in Persian." in prompts[0]
    assert data["language"] == "Persian"


@pytest.mark.asyncio
async def test_unsupported_history_language_is_rejected(monkeypatch):
    plan = _plan(_message(1, "hello"))

    async def _prepare(*_args, **_kwargs):
        return plan

    monkeypatch.setattr(history_ai_service, "_prepare", _prepare)
    monkeypatch.setattr(history_ai_service, "_resolve_manager", lambda _m: object())

    ok, message, data = await history_ai_service.summarize_history(
        None, -100, count=1, language="klingon",
    )
    assert ok is False
    assert data["error"] == history_ai_service.ERROR_LANGUAGE

    ok, message, data = await history_ai_service.translate_history(
        None, -100, count=1, language="klingon",
    )
    assert ok is False
    assert data["error"] == history_ai_service.ERROR_LANGUAGE


@pytest.mark.asyncio
async def test_auto_is_not_a_translation_target(monkeypatch):
    plan = _plan(_message(1, "hello"))

    async def _prepare(*_args, **_kwargs):
        return plan

    monkeypatch.setattr(history_ai_service, "_prepare", _prepare)
    monkeypatch.setattr(history_ai_service, "_resolve_manager", lambda _m: object())

    ok, _message_text, data = await history_ai_service.translate_history(
        None, -100, count=1, language="auto",
    )
    assert ok is False
    assert data["error"] == history_ai_service.ERROR_LANGUAGE


def test_history_result_renders_the_effective_language():
    body = history_panel._summary_body(
        "fake summary",
        {
            "processed": 2,
            "requested": 2,
            "language": "Persian",
            "language_mode": "auto",
            "language_detected": True,
        },
        chat_name="Chat",
    )
    assert "Language: Persian (auto, detected from source)" in body


# ── 6. Tasks ──────────────────────────────────────────────────────────────


def _service() -> TaskCreationService:
    return TaskCreationService(InMemoryTaskRepository(), OWNER)


def test_task_freezes_the_global_default_at_creation():
    get_repository_manager().preferences.update(OWNER, {"language": "fa"})
    frozen = _service()._freeze_language(
        {"ai_instruction": "write a message with a randomly generated line"}
    )
    assert frozen["ai_instruction"].endswith("in Persian")
    assert derive_policy(frozen["ai_instruction"]).language == "persian"


def test_task_explicit_language_is_never_rewritten():
    get_repository_manager().preferences.update(OWNER, {"language": "fa"})
    instruction = "write a message with a randomly generated line, in English"
    frozen = _service()._freeze_language({"ai_instruction": instruction})
    assert frozen["ai_instruction"] == instruction


def test_a_later_global_change_does_not_alter_the_frozen_task():
    get_repository_manager().preferences.update(OWNER, {"language": "fa"})
    frozen = _service()._freeze_language(
        {"ai_instruction": "write a message with a randomly generated line"}
    )
    get_repository_manager().preferences.update(OWNER, {"language": "en"})
    assert frozen["ai_instruction"].endswith("in Persian")


def test_preparation_policy_still_rejects_a_task_language_mismatch():
    policy = derive_policy("write a message with a randomly generated line, in Persian")
    assert policy.language == "persian"
    with pytest.raises(PreparationPolicyError):
        validate_content("this is English", policy)
    assert validate_content("این یک پیام فارسی است", policy)


def test_a_verbatim_natural_language_instruction_is_never_rewritten():
    """The source-fidelity contract owns NL instructions; freezing must not
    change the owner's/model's words. That path needs a durable task-language
    field, which G2-A does not add (reported as a limitation)."""
    get_repository_manager().preferences.update(OWNER, {"language": "fa"})
    instruction = "summarize the last 100 messages of the university chat"
    frozen = _service()._freeze_language({"ai_instruction": instruction})
    assert frozen["ai_instruction"] == instruction


# ── 7. Real dispatch behavior ──────────────────────────────────────────────


class _ScriptedProvider(BaseProvider):
    """Records every payload; never calls the network."""

    def __init__(self, payloads: list[list[dict[str, str]]]) -> None:
        super().__init__(
            ProviderConfig(provider_name="scripted", enabled=True, default_model="m1")
        )
        self._payloads = payloads

    @property
    def name(self) -> str:
        return "scripted"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(supports_tools=True)

    async def chat(self, messages, **kwargs):
        self._payloads.append([dict(m) for m in messages])
        return ProviderResponse(text="ok", provider_name="scripted", success=True)

    def initialize(self) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def health(self) -> dict[str, bool]:
        return {"healthy": True, "ready": True}


def _engine(payloads: list[list[dict[str, str]]]) -> Engine:
    registry = ProviderRegistry()
    registry.register(_ScriptedProvider(payloads))
    return Engine(providers=ProviderManager(registry))


async def _dispatch(engine: Engine, **overrides):
    base = {
        "session_id": "lang-session",
        "user_message": "hello",
        "owner_id": OWNER,
        "chat_id": -100555000,
        "message_id": 11,
    }
    base.update(overrides)
    return await engine.execute(AIRequest(**base))


def _payload_text(payloads: list[list[dict[str, str]]]) -> str:
    return "\n".join(str(m.get("content", "")) for m in payloads[0])


@pytest.mark.asyncio
async def test_invalid_explicit_request_language_is_rejected_before_the_provider():
    payloads: list[list[dict[str, str]]] = []
    result = await _dispatch(_engine(payloads), language="klingon")

    assert result.success is False
    assert result.errors and "Unsupported output language" in result.errors[0]
    assert result.metadata["language_rejected"] == "request"
    # No English fallback decision was substituted.
    assert "language" not in result.metadata
    # The provider was never called.
    assert payloads == []


@pytest.mark.asyncio
async def test_valid_request_language_wins_in_the_rendered_prompt():
    payloads: list[list[dict[str, str]]] = []
    result = await _dispatch(
        _engine(payloads), language="Persian", session_id="lang-explicit",
    )

    assert result.success is True
    text = _payload_text(payloads)
    assert "Language: Persian (effective for this response)" in text
    # The global default stays visible but is explicitly labelled lower-priority
    # and can never be read as this request's language instruction.
    assert "Global default language: English" in text
    assert "Language: English" not in text


@pytest.mark.asyncio
async def test_valid_global_preference_applies_without_an_explicit_value():
    get_repository_manager().preferences.update(OWNER, {"language": "fa"})
    payloads: list[list[dict[str, str]]] = []
    result = await _dispatch(_engine(payloads), session_id="lang-global")

    assert result.success is True
    text = _payload_text(payloads)
    assert "Language: Persian (effective for this response)" in text
    assert "Global default language: Persian" in text


@pytest.mark.asyncio
async def test_explicit_auto_keeps_its_honest_fallback():
    payloads: list[list[dict[str, str]]] = []
    result = await _dispatch(
        _engine(payloads), language="auto", session_id="lang-auto",
    )

    assert result.success is True
    text = _payload_text(payloads)
    assert "auto fallback" in text
    assert "auto-detected" not in text


@pytest.mark.asyncio
async def test_invalid_stored_preference_is_rejected_not_read_as_english():
    get_repository_manager().preferences.update(OWNER, {"language": "klingon"})
    payloads: list[list[dict[str, str]]] = []
    result = await _dispatch(_engine(payloads), session_id="lang-bad-global")

    assert result.success is False
    assert result.errors and "stored AI content language" in result.errors[0]
    assert result.metadata["language_rejected"] == "global"
    assert "English" not in result.errors[0]
    assert payloads == []


@pytest.mark.asyncio
async def test_a_valid_explicit_language_is_not_blocked_by_broken_stored_data():
    get_repository_manager().preferences.update(OWNER, {"language": "klingon"})
    payloads: list[list[dict[str, str]]] = []
    result = await _dispatch(
        _engine(payloads), language="Persian", session_id="lang-explicit-bad-global",
    )

    assert result.success is True
    assert "Language: Persian (effective for this response)" in _payload_text(payloads)


@pytest.mark.asyncio
async def test_a_failed_preference_read_is_not_repeated(monkeypatch):
    """One read per request: a failure must not be retried behind dispatch's back.

    The stored language is Persian. If dispatch read the repository a second
    time inside ``_load_preferences`` (the bug), the prompt would show Persian
    even though the decision came from the FAILED first read. The documented
    English default must apply to both, and the repository must be read once.
    """
    get_repository_manager().preferences.update(OWNER, {"language": "fa"})
    from backend.ai.database import preferences_repository as prefs_module

    real = prefs_module.InMemoryPreferencesRepository.get_or_create
    calls = {"count": 0}

    def _flaky(self, owner_id):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("preferences store unavailable")
        return real(self, owner_id)

    monkeypatch.setattr(
        prefs_module.InMemoryPreferencesRepository, "get_or_create", _flaky,
    )
    payloads: list[list[dict[str, str]]] = []
    result = await _dispatch(_engine(payloads), session_id="lang-single-read")

    assert calls["count"] == 1
    assert result.success is True
    text = _payload_text(payloads)
    assert "Language: English (effective for this response)" in text
    assert "Global default language: English" in text
    # "Persian" also appears in the static capability guidance; the LANGUAGE
    # instructions must not pick it up from the would-be second read.
    assert "Language: Persian" not in text
    assert "Global default language: Persian" not in text


@pytest.mark.asyncio
async def test_invalid_stored_language_never_reaches_the_prompt():
    """A malformed stored value must not be rendered into the model payload."""
    sentinel = "invalid-language-sentinel-7f31"
    get_repository_manager().preferences.update(OWNER, {"language": sentinel})
    payloads: list[list[dict[str, str]]] = []
    result = await _dispatch(
        _engine(payloads), language="Persian", session_id="lang-safe-global",
    )

    assert result.success is True
    text = _payload_text(payloads)
    assert "Language: Persian (effective for this response)" in text
    assert sentinel not in text
    assert (
        "Global default language: Unavailable (invalid stored preference)" in text
    )


def test_review_reports_explicit_and_effective_language():
    assert task_wizard.language_label("fa") == "Persian"
    get_repository_manager().preferences.update(OWNER, {"language": "fa"})
    assert task_wizard.language_label("", global_language="fa") == (
        "Persian (global default)"
    )
