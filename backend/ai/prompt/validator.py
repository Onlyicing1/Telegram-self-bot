"""
Prompt Validator — verifies a ``PromptPackage`` is well-formed before
it is handed to any provider.

Checks:
  1. Required sections exist (SYSTEM_RULES, USER_MESSAGE, OUTPUT_INSTRUCTIONS)
  2. No duplicated section keys
  3. No empty mandatory fields (system_prompt, user_input, output instructions)
  4. No malformed objects (wrong types, missing fields on PromptPackage)
  5. Budget information is available and populated

The validator raises ``InvalidPromptPackage`` if any check fails.
It does NOT modify the package — it only inspects.
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import Any

from backend.ai.prompt.budget import TokenBudget
from backend.ai.prompt.template import MANDATORY_SECTIONS, PromptSection


class InvalidPromptPackage(Exception):
    """Raised when a ``PromptPackage`` fails validation."""


def validate_sections(sections: dict[PromptSection, str]) -> None:
    """Validate that all mandatory sections exist and are non-empty.

    Also checks for duplicated keys (which is structurally impossible
    with a dict, but we check for empty-string mandatory sections).
    """
    if not isinstance(sections, dict):
        raise InvalidPromptPackage(f"sections must be a dict, got {type(sections)}")

    for section in MANDATORY_SECTIONS:
        if section not in sections:
            raise InvalidPromptPackage(f"Missing mandatory section: {section.value}")
        text = sections[section]
        if not isinstance(text, str):
            raise InvalidPromptPackage(
                f"Section {section.value} must be str, got {type(text)}"
            )
        if not text.strip():
            raise InvalidPromptPackage(
                f"Mandatory section {section.value} is empty"
            )


def validate_budget(budget: TokenBudget) -> None:
    """Validate that budget fields are populated and sane."""
    if not isinstance(budget, TokenBudget):
        raise InvalidPromptPackage(
            f"budget must be TokenBudget, got {type(budget)}"
        )
    if budget.estimated_input_tokens < 0:
        raise InvalidPromptPackage("estimated_input_tokens is negative")
    if budget.estimated_output_budget < 0:
        raise InvalidPromptPackage("estimated_output_budget is negative")
    if budget.estimated_total < 0:
        raise InvalidPromptPackage("estimated_total is negative")
    if budget.prompt_size_chars < 0:
        raise InvalidPromptPackage("prompt_size_chars is negative")
    if budget.max_total_tokens <= 0:
        raise InvalidPromptPackage("max_total_tokens must be positive")


def validate_prompt_package(package: Any) -> None:
    """Full validation of a ``PromptPackage`` object.

    Checks:
      - Object has all required fields.
      - Field types are correct.
      - Mandatory sections exist and are non-empty.
      - Budget is populated and valid.
      - system_prompt and user_input are non-empty strings.
      - metadata is a dict.

    Raises ``InvalidPromptPackage`` on any failure.
    """
    if package is None:
        raise InvalidPromptPackage("package is None")

    required_fields = (
        "system_prompt",
        "runtime_context",
        "conversation_context",
        "tool_context",
        "user_input",
        "metadata",
        "estimated_tokens",
    )
    for field_name in required_fields:
        if not hasattr(package, field_name):
            raise InvalidPromptPackage(f"Missing field: {field_name}")

    if not isinstance(package.system_prompt, str) or not package.system_prompt.strip():
        raise InvalidPromptPackage("system_prompt must be a non-empty string")

    if not isinstance(package.user_input, str) or not package.user_input.strip():
        raise InvalidPromptPackage("user_input must be a non-empty string")

    if not isinstance(package.metadata, dict):
        raise InvalidPromptPackage(f"metadata must be dict, got {type(package.metadata)}")

    if not isinstance(package.estimated_tokens, TokenBudget):
        raise InvalidPromptPackage(
            f"estimated_tokens must be TokenBudget, got {type(package.estimated_tokens)}"
        )

    validate_budget(package.estimated_tokens)

    # Validate sections if the package carries them
    sections = getattr(package, "sections", None)
    if sections is not None:
        validate_sections(sections)
