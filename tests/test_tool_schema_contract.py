"""
Provider-facing tool schema contract (Stage 6 — Workstream A1).

The investigation measured that ``Dispatcher._build_tool_definitions``
inferred requiredness from the ABSENCE of a ``default``: 40 of the 55
registered tools advertised optional (or alternative) parameters as
mandatory, contradicting their own descriptions and the system prompt.

These tests pin the fix where the model actually reads it: the OpenAI-format
function schema produced by the REAL dispatcher serializer from the REAL
registry — never from Python annotations or hand-built dicts.
"""
from __future__ import annotations

from backend.ai.engine.dispatcher import Dispatcher
from backend.ai.tools.base import declared_provider_required_arguments
from backend.ai.tools.context import ToolContext
from backend.ai.tools.registry import create_default_registry


def _registry():
    return create_default_registry(
        ToolContext(telegram=None, owner_id=1, tz_str="UTC")
    )


def _functions():
    registry = _registry()
    dispatcher = Dispatcher.__new__(Dispatcher)
    dispatcher.set_tool_registry(registry)
    functions = {
        definition["function"]["name"]: definition["function"]
        for definition in dispatcher._build_tool_definitions()
    }
    return registry, functions


def _params(functions, name):
    return functions[name]["parameters"]


def _required(functions, name):
    return set(_params(functions, name).get("required") or [])


def test_every_registered_tool_reaches_the_provider_exactly_once():
    registry, functions = _functions()
    assert len(registry.list_names()) == 55
    assert set(functions) == set(registry.list_names())


def test_provider_required_lists_come_from_the_tool_declaration_only():
    """The serializer must never infer requiredness from `default` absence."""
    registry, functions = _functions()
    for tool in registry.list():
        properties = _params(functions, tool.name).get("properties") or {}
        expected = [
            name
            for name in declared_provider_required_arguments(tool)
            if name in properties
        ]
        assert list(_params(functions, tool.name).get("required") or []) == expected, tool.name
        assert set(expected) <= set(properties), tool.name


def test_optional_parameters_are_no_longer_advertised_as_required():
    """The exact regression set measured in INVESTIGATION.md (P0-1)."""
    _, functions = _functions()
    for name, forbidden in (
        ("save", {"display_name", "tags"}),
        ("save_by_link", {"display_name", "tags"}),
        ("web_search", {"count", "freshness", "include_domains"}),
        ("delete", {"count", "mode", "until_time", "after_time", "boundary_id", "query", "semantic"}),
        ("list_recent_messages", {"limit"}),
        ("account_show", {"fields"}),
        ("translate_history", {"count", "language", "instruction"}),
        ("summarize_history", {"count", "instruction"}),
        ("memory_list", {"tier", "query", "limit"}),
        ("todo_step_list", {"task_id", "query"}),
    ):
        required = _required(functions, name)
        wrong = required & forbidden
        assert not wrong, f"{name} still marks optional parameters required: {sorted(wrong)}"


def test_web_search_requires_only_the_query():
    _, functions = _functions()
    params = _params(functions, "web_search")
    assert params["required"] == ["query"]
    count = params["properties"]["count"]
    assert count["default"] == 10
    assert count["minimum"] == 1 and count["maximum"] == 100
    assert params["properties"]["freshness"]["enum"] == ["day", "week", "month", "year"]
    assert params["properties"]["include_domains"]["items"] == {"type": "string"}


def test_save_metadata_stays_optional():
    _, functions = _functions()
    params = _params(functions, "save")
    assert params.get("required") in (None, [])
    assert set(params["properties"]) == {"display_name", "tags"}
    assert params["properties"]["tags"]["items"] == {"type": "string"}


def test_genuinely_required_parameters_survive():
    _, functions = _functions()
    expected = {
        "search": "query",
        "settings_get": "key",
        "settings_set": ("key", "value"),
        "create_task": "request",
        "task_inspect": "task_id",
        "delete_by_id": "message_id",
        "save_by_link": "link",
    }
    for name, arguments in expected.items():
        expected_set = {arguments} if isinstance(arguments, str) else set(arguments)
        assert _required(functions, name) == expected_set, name


def test_a_mixed_tool_marks_only_its_required_parameter():
    _, functions = _functions()
    params = _params(functions, "send_message")
    assert params["required"] == ["text"]
    assert "font" not in params["required"]
    assert len(params["properties"]["font"]["enum"]) > 1
    assert params["properties"]["text"]["maxLength"] == 4096


def test_enum_nested_and_array_schemas_are_preserved():
    _, functions = _functions()
    delete = _params(functions, "delete")["properties"]
    assert delete["mode"]["enum"] == [
        "last_n", "all", "until_time", "until_message", "filtered",
    ]
    semantic = delete["semantic"]
    assert semantic["type"] == "object"
    assert set(semantic["properties"]) == {"query", "word_count", "english_word_count"}
    assert semantic["properties"]["word_count"]["minimum"] == 1
    account = _params(functions, "account_show")["properties"]
    assert account["fields"]["items"]["type"] == "string"
    assert account["fields"]["items"]["enum"] == [
        "first_name", "last_name", "full_name", "username",
    ]
    tags = _params(functions, "update_save_tags")
    assert tags["properties"]["tags"]["items"] == {"type": "string"}
    assert set(tags["required"]) == {"tags", "mode"}


def test_alternative_addressing_is_not_marked_required():
    """id+version OR title reference: neither alternative is mandatory."""
    _, functions = _functions()
    assert _required(functions, "task_transition") == {"action"}
    assert "query" not in _required(functions, "task_transition")
    assert "complete_steps" not in _required(functions, "task_transition")
    assert _required(functions, "task_delete") == set()
    assert _required(functions, "retrieve_save") == set()
    assert _required(functions, "todo_step_transition") == {"action"}
    # ... while an always-required field stays required.
    assert _required(functions, "todo_edit") == {"title"}
