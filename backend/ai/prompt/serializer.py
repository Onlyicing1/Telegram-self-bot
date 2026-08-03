"""
Prompt Serializer — converts a ``PromptPackage`` sections dict into a
flat string or a list of message dicts for different provider formats.

The serializer does NOT call any API. It only transforms data.
It is used by the ``PromptFormatter`` to produce the final output
in the requested format.
"""
from __future__ import annotations

from typing import Any

from backend.ai.prompt.template import PromptSection, SECTION_ORDER


def serialize_to_text(sections: dict[PromptSection, str]) -> str:
    """Serialize ordered sections into a single text block.

    Each section is preceded by a header line and separated by a
    blank line. Sections with empty text are omitted.

    Args:
        sections: Dict mapping ``PromptSection`` → section text.

    Returns:
        A single string with all non-empty sections in order.
    """
    parts: list[str] = []
    for section in SECTION_ORDER:
        text = sections.get(section, "")
        if not text:
            continue
        header = f"[{section.value}]"
        parts.append(f"{header}\n{text}")
    return "\n\n".join(parts)


def serialize_to_message_list(
    sections: dict[PromptSection, str],
) -> list[dict[str, str]]:
    """Serialize sections into a list of ``{"role", "content"}`` dicts.

    System-level sections (SYSTEM_RULES, PLATFORM_CONSTRAINTS,
    RUNTIME_RULES, OUTPUT_INSTRUCTIONS) are merged into a single
    ``"system"`` message. Context and state sections become a
    ``"system"`` message. The user message becomes a ``"user"``
    message. Tool results become ``"tool"`` messages.

    Args:
        sections: Dict mapping ``PromptSection`` → section text.

    Returns:
        An ordered list of message dicts.
    """
    messages: list[dict[str, str]] = []

    system_parts: list[str] = []
    context_parts: list[str] = []
    tool_result_parts: list[str] = []
    user_text = ""

    for section in SECTION_ORDER:
        text = sections.get(section, "")
        if not text:
            continue
        if section in (
            PromptSection.SYSTEM_RULES,
            PromptSection.PLATFORM_CONSTRAINTS,
            PromptSection.RUNTIME_RULES,
            PromptSection.OUTPUT_INSTRUCTIONS,
        ):
            system_parts.append(f"[{section.value}]\n{text}")
        elif section == PromptSection.USER_MESSAGE:
            user_text = text
        elif section == PromptSection.TOOL_RESULTS:
            tool_result_parts.append(f"[{section.value}]\n{text}")
        else:
            context_parts.append(f"[{section.value}]\n{text}")

    if system_parts:
        messages.append({"role": "system", "content": "\n\n".join(system_parts)})
    if context_parts:
        messages.append({"role": "system", "content": "\n\n".join(context_parts)})
    if tool_result_parts:
        for part in tool_result_parts:
            messages.append({"role": "tool", "content": part})
    if user_text:
        messages.append({"role": "user", "content": user_text})

    return messages
