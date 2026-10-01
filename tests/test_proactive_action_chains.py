"""Bounded proactive multi-action Todo/task execution — regression tests.

Covers the per-request proactive-initiative feature (Mode A / Mode B):

  1. a request WITHOUT authorization is never expanded;
  2. a request WITH explicit authorization may plan a bounded ordered chain;
  3. one request -> ONE coherent task / ONE ordered sequence;
  4. actions keep their declared order;
  5. the action chain stays inside the EXISTING bounds (5) — unchanged;
  6. unregistered / unrelated actions are rejected through the real registry;
  7. ambiguity still falls back to clarification (NULL RULE untouched);
  8. confirmation-gated actions stay confirmation-gated;
  9. the ToolRegistry -> ToolExecutor path is unchanged;
 10. no second scheduler/executor/dispatcher is introduced;
 11. no recursive task creation is ever planned or executed;
 12. existing literal multi-action creation stays compatible;
 13-16. existing Todo / conditional / continuation / security suites stay green
        (proven by the full-suite run; the focused files are exercised here).

Every test runs REAL boundaries: TaskInterpreter -> TaskCandidate ->
TaskCreationService -> TaskRepository, and Engine -> Dispatcher ->
ToolExecutor for the conversational surface. Nothing is faked except the
provider response itself.
"""
from __future__ import annotations

import ast
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.ai.database import manager as dbm
from backend.ai.database.task_repository import InMemoryTaskRepository
from backend.ai.engine.dispatcher import MAX_TOOL_ROUNDS
from backend.ai.prompt import template
from backend.ai.proactive import (
    PROACTIVE_AUTHORIZED_RULES,
    has_proactive_authorization,
)
from backend.ai.providers.base.contract import ProviderResponse
from backend.ai.task_contract import MAX_ACTIONS, scheduled_creation_error
from backend.ai.task_creation import TaskCreationError, TaskCreationService
from backend.ai.task_interpreter import (
    PROACTIVE_EXPANSION_INSTRUCTIONS,
    TaskInterpreter,
)
from backend.ai.tools.base import PermissionLevel, Tool, ToolResult
from backend.ai.tools.context import ToolContext
from backend.ai.tools.executor import MAX_TOOLS_PER_TURN
from backend.ai.tools.registry import ToolRegistry, create_default_registry
from backend.ai.tools.task import CreateTaskTool
from tests.test_11_runtime_wiring import ScriptedProvider
from tests.test_task_semantic_completeness import _Provider, _manager

OWNER = 777
REF = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

# The owner's own example messages carrying EXPLICIT initiative authorization.
SAMPLE_RELATED_PERSIAN = (
    "این کار رو انجام بده و اگر چیزهای مرتبط دیگه‌ای هم لازمه، خودت انجام بده."
)
SAMPLE_SMALL_TASKS_PERSIAN = (
    "این Todo رو انجام بده، هر کار کوچیکی هم که برای کامل شدنش "
    "لازمه خودت انجام بده."
)
# A task-creation request that carries BOTH a schedule (completeness gate)
# and explicit English authorization.
AUTH_REQUEST = (
    "every 5 minutes run my plan and if anything else related is needed, "
    "use your judgment"
)
PLAIN_REQUEST = "every 5 minutes run my plan"


def _system_text(provider: _Provider) -> str:
    return " ".join(
        str(item["content"])
        for item in provider.last_messages
        if item["role"] == "system"
    )


def _payload(actions, label="Plan"):
    return {
        "label": label,
        "schedule_type": "interval",
        "schedule": {"seconds": 300},
        "timezone": "UTC",
        "actions": actions,
        "notification_destination": {},
    }


def _send(text):
    return {"name": "send_message", "arguments": {"text": text}}


def _ctx(provider_manager, *, proactive: bool = False) -> ToolContext:
    extra = {"provider_manager": provider_manager, "chat_id": -1001}
    if proactive:
        extra["proactive_authorized"] = True
    return ToolContext(
        telegram=None, owner_id=OWNER, tz_str="UTC", client=None, extra=extra
    )


def _registry():
    return create_default_registry(
        ToolContext(telegram=None, owner_id=OWNER, tz_str="UTC")
    )


async def _create_through_tool(request: str, payload: dict, *, proactive: bool = False):
    """Run the REAL create_task tool path against an in-memory repository."""
    provider = _Provider(json.dumps(payload))
    provider_manager = _manager(provider)
    repository_manager = dbm.RepositoryManager(supabase_available=False)
    context = _ctx(provider_manager, proactive=proactive)
    with patch.object(dbm, "get_repository_manager", return_value=repository_manager):
        result = await CreateTaskTool(context).execute(context, {"request": request})
    return result, provider, repository_manager


# ═══════════════════════════════════════════════════════════════════════════
# 1. Detector — explicit, per-request, fail-closed
# ═══════════════════════════════════════════════════════════════════════════


def test_owner_samples_are_detected_as_explicit_authorization():
    assert has_proactive_authorization(SAMPLE_RELATED_PERSIAN) is True
    assert has_proactive_authorization(SAMPLE_SMALL_TASKS_PERSIAN) is True


@pytest.mark.parametrize(
    "text",
    [
        "do this and use your judgment for anything else",
        "as you see fit — handle the rest",
        "be proactive about my todos",
        "do what's needed and finish",
        "at your discretion",
        "handle the rest yourself",
    ],
)
def test_english_initiative_phrases_are_detected(text):
    assert has_proactive_authorization(text) is True


def test_plain_requests_are_not_authorized():
    for text in (
        "every 5 minutes update my bio",
        "این کار رو انجام بده",
        "delete the last message",
        "show my tasks",
        PLAIN_REQUEST,
        "این کار رو انجام بده.",
    ):
        assert has_proactive_authorization(text) is False, text


def test_ordinary_language_is_never_treated_as_authorization():
    """Bare delegation, opinion prompts, and social phrasing are Mode A."""
    for text in (
        "خودت انجام بده",                      # bare delegation
        "به نظر خودت چیه؟",                    # opinion question
        "feel free to ask",                     # permission-adjacent
        "if anything else, let me know",        # opposite of authorization
        "i have no other work to do",
    ):
        assert has_proactive_authorization(text) is False, text


def test_detector_fails_closed_on_non_text():
    assert has_proactive_authorization("") is False
    assert has_proactive_authorization("   ") is False
    assert has_proactive_authorization(None) is False
    assert has_proactive_authorization(123) is False
    assert has_proactive_authorization(["use your judgment"]) is False


def test_zwnj_and_split_spellings_agree():
    assert has_proactive_authorization("چیزهای مرتبط دیگه‌ای را بکن") is True
    assert has_proactive_authorization("چیزهای مرتبط دیگهای را بکن") is True
    assert has_proactive_authorization("چیزهای مرتبط دیگه ای را بکن") is True


# ═══════════════════════════════════════════════════════════════════════════
# 2. Durable planning — the interpreter's bounded expansion contract
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_unauthorized_request_gets_no_expansion_contract():
    """Mode A: planning instructions stay byte-identical — nothing appended."""
    payload = _payload(
        [{"name": "bio_set_text", "arguments": {"text": ""}}],
        label="Bio update",
    )
    payload["ai_instruction"] = PLAIN_REQUEST
    provider = _Provider(json.dumps(payload))
    candidate = await TaskInterpreter(_manager(provider)).interpret(
        PLAIN_REQUEST, timezone="UTC"
    )
    assert "PROACTIVE EXPANSION" not in _system_text(provider)
    assert candidate.actions == [
        {"name": "bio_set_text", "arguments": {"text": ""}}
    ]


@pytest.mark.asyncio
async def test_interpreter_self_detects_authorization_in_the_request():
    provider = _Provider(json.dumps(_payload([_send("hello")])))
    await TaskInterpreter(_manager(provider)).interpret(
        SAMPLE_RELATED_PERSIAN, timezone="UTC"
    )
    system = _system_text(provider)
    assert "PROACTIVE EXPANSION (AUTHORIZED" in system
    # Every pre-existing contract survives alongside the added block.
    assert "Never invent missing schedule" in system
    assert "NULL RULE" in system


@pytest.mark.asyncio
async def test_authorized_raw_owner_message_flags_planning_even_when_request_distilled():
    """The owner's raw message (context flag) proves consent even when the
    model passed a distilled task request with no authorization phrase."""
    payload = _payload([_send("hello")])
    provider = _Provider(json.dumps(payload))
    context = _ctx(_manager(provider), proactive=True)
    repository_manager = dbm.RepositoryManager(supabase_available=False)
    with patch.object(dbm, "get_repository_manager", return_value=repository_manager):
        result = await CreateTaskTool(context).execute(
            context, {"request": PLAIN_REQUEST}
        )
    assert result.success is True, result.message
    assert "PROACTIVE EXPANSION (AUTHORIZED" in _system_text(provider)
    assert len(await repository_manager.task.list_tasks(OWNER)) == 1


@pytest.mark.asyncio
async def test_one_authorized_request_creates_one_coherent_ordered_task():
    """ONE create_task call -> ONE task holding the ordered chain."""
    actions = [_send(f"step {i}") for i in range(1, 6)]
    result, provider, repository_manager = await _create_through_tool(
        AUTH_REQUEST, _payload(actions, label="Ordered plan"), proactive=True
    )
    assert result.success is True, result.message
    assert provider.calls == 1, "one request must not become repeated create_task calls"
    tasks = await repository_manager.task.list_tasks(OWNER)
    assert len(tasks) == 1
    assert [a["arguments"]["text"] for a in tasks[0].actions] == [
        f"step {i}" for i in range(1, 6)
    ]


@pytest.mark.asyncio
async def test_action_chain_bounds_are_unchanged():
    """The feature never raises any existing bound."""
    assert MAX_ACTIONS == 5
    assert MAX_TOOLS_PER_TURN == 5
    assert MAX_TOOL_ROUNDS == 3

    six = [_send(f"n{i}") for i in range(6)]
    result, _, repository_manager = await _create_through_tool(
        AUTH_REQUEST, _payload(six), proactive=True
    )
    assert result.success is False
    assert await repository_manager.task.list_tasks(OWNER) == []


@pytest.mark.asyncio
async def test_literal_multi_action_request_stays_compatible_without_authorization():
    """Mode A still accepts multiple actions when the OWNER literally
    asked for all of them — only self-derived expansion is gated."""
    actions = [_send("first"), _send("second")]
    result, provider, repository_manager = await _create_through_tool(
        "every 5 minutes send first then send second",
        _payload(actions),
        proactive=False,
    )
    assert result.success is True, result.message
    assert "PROACTIVE EXPANSION" not in _system_text(provider)
    tasks = await repository_manager.task.list_tasks(OWNER)
    assert len(tasks) == 1
    assert len(tasks[0].actions) == 2


@pytest.mark.asyncio
async def test_authorized_plan_with_unregistered_action_is_rejected():
    """The registry decides — an action that no registered tool can run is
    refused even inside an authorized proactive plan."""
    payload = _payload(
        [{"name": "make_me_a_sandwich", "arguments": {"depth": 1}}]
    )
    provider = _Provider(json.dumps(payload))
    candidate = await TaskInterpreter(_manager(provider)).interpret(
        AUTH_REQUEST, timezone="UTC", proactive_authorized=True
    )
    assert "PROACTIVE EXPANSION (AUTHORIZED" in _system_text(provider)

    repository = InMemoryTaskRepository()
    with pytest.raises(TaskCreationError) as excinfo:
        await TaskCreationService(repository, OWNER, _registry()).create(
            candidate.as_creation_candidate(), REF
        )
    assert "not a registered tool" in str(excinfo.value)
    assert await repository.list_tasks(OWNER) == []


@pytest.mark.asyncio
async def test_authorized_plan_with_confirmation_gated_action_is_rejected():
    """Permission for initiative never bypasses a confirmation gate."""
    payload = _payload(
        [{"name": "settings_set", "arguments": {"key": "language", "value": "en"}}]
    )
    provider = _Provider(json.dumps(payload))
    candidate = await TaskInterpreter(_manager(provider)).interpret(
        AUTH_REQUEST, timezone="UTC", proactive_authorized=True
    )
    assert "PROACTIVE EXPANSION (AUTHORIZED" in _system_text(provider)

    repository = InMemoryTaskRepository()
    with pytest.raises(TaskCreationError) as excinfo:
        await TaskCreationService(repository, OWNER, _registry()).create(
            candidate.as_creation_candidate(), REF
        )
    assert "requires owner confirmation" in str(excinfo.value)
    assert await repository.list_tasks(OWNER) == []


def test_recursive_task_creation_is_forbidden_in_proactive_plans():
    """The plan contract forbids create_task inside chains, and the existing
    execution-time refusal remains the hard backstop."""
    assert "create_task" in PROACTIVE_EXPANSION_INSTRUCTIONS
    assert "NEVER appear inside actions" in PROACTIVE_EXPANSION_INSTRUCTIONS
    assert scheduled_creation_error(True), (
        "the execution-time recursion refusal must remain in place"
    )


def test_static_and_dynamic_rules_bound_the_conversation_surface():
    assert PROACTIVE_AUTHORIZED_RULES.startswith(
        "Proactive authorization: AUTHORIZED"
    )
    assert "5 tool calls" in PROACTIVE_AUTHORIZED_RULES
    assert "confirmation gate" in PROACTIVE_AUTHORIZED_RULES
    assert "create_task repeatedly" in PROACTIVE_AUTHORIZED_RULES
    assert "clarifying question" in PROACTIVE_AUTHORIZED_RULES
    assert "Proactive initiative is PER-REQUEST" in template.RUNTIME_RULES_TEMPLATE


def test_no_second_scheduler_executor_or_registry_is_introduced():
    """backend/ai/proactive.py may depend only on shared utilities — never on
    an execution authority."""
    module_path = Path(__file__).resolve().parents[1] / "backend" / "ai" / "proactive.py"
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden = {
        "backend.ai.task_scheduler",
        "backend.ai.task_execution",
        "backend.ai.engine.dispatcher",
        "backend.ai.engine.engine",
        "backend.ai.tools.executor",
        "backend.ai.tools.registry",
        "backend.bot",
    }
    assert not (imported & forbidden), imported & forbidden


# ═══════════════════════════════════════════════════════════════════════════
# 3. Conversational surface — the per-request system line + tool context
# ═══════════════════════════════════════════════════════════════════════════


class _Probe(Tool):
    """Tiny recording tool shared by the ordering / flag tests."""

    def __init__(self, probe_name: str, log: list) -> None:
        self._probe_name = probe_name
        self._log = log

    @property
    def name(self) -> str:
        return self._probe_name

    @property
    def description(self) -> str:
        return "Records execution order and the proactive flag it received."

    @property
    def parameters(self) -> dict:
        return {}

    @property
    def permission_level(self) -> PermissionLevel:
        return PermissionLevel.READ_ONLY

    @property
    def safe(self) -> bool:
        return True

    @property
    def return_type(self) -> str:
        return "ToolResult"

    async def execute(self, context: ToolContext, arguments: dict) -> ToolResult:
        self._log.append(
            (self._probe_name, (context.extra or {}).get("proactive_authorized"))
        )
        return ToolResult(success=True, message=f"{self._probe_name} ok")


def _usage():
    return {"prompt_tokens": 100, "completion_tokens": 5, "total_tokens": 105}


def _make_engine(provider, log, *, with_create_task: bool = False):
    from backend.ai.engine.engine import Engine

    engine = Engine(providers=_manager(provider))
    registry = ToolRegistry()
    registry.register(_Probe("probe_one", log))
    registry.register(_Probe("probe_two", log))
    if with_create_task:
        # The REAL registered create_task tool — the composition tests run
        # the genuine dispatcher -> ToolExecutor -> CreateTaskTool path.
        registry.register(CreateTaskTool(
            ToolContext(telegram=None, owner_id=OWNER, tz_str="UTC", client=None)
        ))
    real_ctx = ToolContext(telegram=None, owner_id=OWNER, tz_str="UTC", client=None)
    engine.attach_tools(registry, real_ctx, owner_id=OWNER, tz_str="UTC")
    return engine


@pytest.mark.asyncio
async def test_authorized_request_appends_bounded_rules_and_keeps_order():
    from backend.ai.session.request import AIRequest

    log: list = []
    provider = ScriptedProvider(
        [
            # ONE round carries an ordered two-action sequence (within the
            # existing 5-per-turn bound); the continuation reports the result.
            ProviderResponse(
                text="",
                provider_name="scripted",
                success=True,
                tool_calls=[
                    {"id": "c1", "name": "probe_one", "arguments": {}},
                    {"id": "c2", "name": "probe_two", "arguments": {}},
                ],
                usage=_usage(),
            ),
            ProviderResponse(
                text="done",
                provider_name="scripted",
                success=True,
                usage=_usage(),
            ),
        ]
    )
    engine = _make_engine(provider, log)

    result = await engine.execute(
        AIRequest(
            session_id="proactive-1",
            user_message=SAMPLE_RELATED_PERSIAN,
            owner_id=OWNER,
            chat_id=-1,
            message_id=1,
        )
    )

    assert result.success is True, result.errors
    assert result.metadata.get("proactive_authorized") is True
    first = provider.received[0]
    lines = [
        i
        for i, m in enumerate(first)
        if m.get("role") == "system"
        and str(m.get("content", "")).startswith("Proactive authorization: AUTHORIZED")
    ]
    assert len(lines) == 1, "exactly one bounded rules message"
    user_index = max(i for i, m in enumerate(first) if m.get("role") == "user")
    assert lines[0] < user_index, "rules must precede the owner's message"
    # The rules persist into the continuation round.
    assert any(
        str(m.get("content", "")).startswith("Proactive authorization: AUTHORIZED")
        for m in provider.received[1]
        if m.get("role") == "system"
    )
    # Ordered execution through the ToolExecutor + the flag reached the tools.
    assert log == [("probe_one", True), ("probe_two", True)]
    assert result.response == "done"


@pytest.mark.asyncio
async def test_plain_request_gets_no_expansion_rules():
    from backend.ai.session.request import AIRequest

    log: list = []
    provider = ScriptedProvider(
        [
            ProviderResponse(
                text="",
                provider_name="scripted",
                success=True,
                tool_calls=[{"id": "c1", "name": "probe_one", "arguments": {}}],
                usage=_usage(),
            ),
            ProviderResponse(
                text="plain answer",
                provider_name="scripted",
                success=True,
                usage=_usage(),
            ),
        ]
    )
    engine = _make_engine(provider, log)

    result = await engine.execute(
        AIRequest(
            session_id="proactive-2",
            user_message=PLAIN_REQUEST,
            owner_id=OWNER,
            chat_id=-1,
            message_id=1,
        )
    )

    assert result.success is True, result.errors
    assert result.metadata.get("proactive_authorized") is False
    assert not any(
        str(m.get("content", "")).startswith("Proactive authorization:")
        for m in provider.received[0]
    )
    # The tool context carries the same fail-closed False.
    assert log == [("probe_one", False)]


# ═══════════════════════════════════════════════════════════════════════════
# 4. Full composition — ONE authorized message becomes ONE multi-action task
# ═══════════════════════════════════════════════════════════════════════════


def _candidate_payload():
    """The task candidate the interpreter round returns for AUTH_REQUEST.

    Uses REAL registered tools (send_message) — never arbitrary fakes — and
    models exactly the derived chain the expansion contract asks for: the
    owner's stated action first, then the directly useful follow-ups.
    """
    return _payload(
        [
            _send("plan step 1"),
            _send("plan step 2"),
            _send("plan step 3"),
        ],
        label="Authorized plan",
    )


def _compose_provider(
    candidate_payload: dict, *, distilled_request: str = AUTH_REQUEST
) -> ScriptedProvider:
    """Scripted provider for the FULL composition: only the provider boundary
    is mocked. The response ORDER mirrors the real call sequence —

      1. dispatcher round -> the model emits ONE real create_task tool call
         (the model's distilled task request);
      2. TaskInterpreter (INSIDE create_task execution) -> the candidate;
      3. dispatcher continuation -> the final answer.
    """
    return ScriptedProvider(
        [
            ProviderResponse(
                text="",
                provider_name="scripted",
                success=True,
                tool_calls=[
                    {
                        "id": "t1",
                        "name": "create_task",
                        "arguments": {"request": distilled_request},
                    }
                ],
                usage=_usage(),
            ),
            ProviderResponse(
                text=json.dumps(candidate_payload),
                provider_name="scripted",
                success=True,
                usage=_usage(),
            ),
            ProviderResponse(
                text="done",
                provider_name="scripted",
                success=True,
                usage=_usage(),
            ),
        ]
    )


@pytest.mark.asyncio
async def test_one_authorized_message_composes_one_multi_action_task():
    """THE planner-composition regression: one natural-language message with
    explicit initiative authorization, answered by the model with ONE real
    create_task tool call, must produce ONE persisted task whose action
    chain carries the derived ordered actions — through the REAL dispatcher
    -> ToolExecutor -> CreateTaskTool -> TaskInterpreter ->
    TaskCreationService path. Nothing expands the chain executor-side.
    """
    from backend.ai.session.request import AIRequest

    provider = _compose_provider(_candidate_payload())
    log: list = []
    engine = _make_engine(provider, log, with_create_task=True)

    repository_manager = dbm.RepositoryManager(supabase_available=False)
    with patch.object(
        dbm, "get_repository_manager", return_value=repository_manager
    ):
        result = await engine.execute(
            AIRequest(
                session_id="proactive-compose",
                user_message=SAMPLE_RELATED_PERSIAN,
                owner_id=OWNER,
                chat_id=-1,
                message_id=1,
            )
        )

    assert result.success is True, result.errors
    assert result.metadata.get("proactive_authorized") is True
    # Call sequence proves the shape: conversational round, ONE interpreter
    # round inside create_task, ONE continuation — no repeated create_task.
    assert len(provider.received) == 3
    # ONE create_task call -> ONE task with the DERIVED ordered actions.
    tasks = await repository_manager.task.list_tasks(OWNER)
    assert len(tasks) == 1
    task = tasks[0]
    assert len(task.actions) == 3
    assert [a["name"] for a in task.actions] == ["send_message"] * 3
    assert [a["arguments"]["text"] for a in task.actions] == [
        "plan step 1",
        "plan step 2",
        "plan step 3",
    ]
    # The conversational round carried the AUTHORIZED rules line (and nothing
    # else injected the expansion — it is the interpreter's contract).
    assert any(
        str(m.get("content", "")).startswith("Proactive authorization: AUTHORIZED")
        for m in provider.received[0]
        if m.get("role") == "system"
    )
    # The interpreter round (inside create_task) received the EXPANSION
    # contract — the planner, not the executor, is what multiplies actions.
    assert "PROACTIVE EXPANSION (AUTHORIZED" in " ".join(
        str(m.get("content"))
        for m in provider.received[1]
        if m.get("role") == "system"
    )
    assert result.response == "done"


@pytest.mark.asyncio
async def test_one_unauthorized_message_composes_no_expanded_chain():
    """Mode A composition: the same single create_task tool call under a
    request WITHOUT authorization still creates the literally named task,
    but the planner never received the expansion contract."""
    from backend.ai.session.request import AIRequest

    provider = _compose_provider(
        _payload([_send("just this one")], label="Literal plan"),
        distilled_request=PLAIN_REQUEST,
    )
    log: list = []
    engine = _make_engine(provider, log, with_create_task=True)

    repository_manager = dbm.RepositoryManager(supabase_available=False)
    with patch.object(
        dbm, "get_repository_manager", return_value=repository_manager
    ):
        result = await engine.execute(
            AIRequest(
                session_id="proactive-compose-a",
                user_message=PLAIN_REQUEST,
                owner_id=OWNER,
                chat_id=-1,
                message_id=1,
            )
        )

    assert result.success is True, result.errors
    assert result.metadata.get("proactive_authorized") is False
    assert len(provider.received) == 3
    tasks = await repository_manager.task.list_tasks(OWNER)
    assert len(tasks) == 1
    assert len(tasks[0].actions) == 1
    assert not any(
        str(m.get("content", "")).startswith("Proactive authorization: AUTHORIZED")
        for m in provider.received[0]
        if m.get("role") == "system"
    )
    assert "PROACTIVE EXPANSION" not in " ".join(
        str(m.get("content"))
        for m in provider.received[1]
        if m.get("role") == "system"
    )
