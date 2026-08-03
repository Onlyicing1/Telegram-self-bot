"""
Prompt Formatter — formats a ``PromptPackage`` into different provider
styles WITHOUT generating provider-specific API payloads.

Supported formats:
  - ``"generic"``   — flat text block with [section] headers
  - ``"chatml"``     — ChatML-style message list (role/content dicts)
  - ``"openai"``     — OpenAI-style message list (system/user/tool roles)
  - ``"gemini"``     — Gemini-style contents list (role/parts dicts)

The formatter does NOT call any API. It only transforms data.
Future providers will consume the formatted output and wrap it in
their own HTTP request bodies — that step is NOT part of this layer.

All formatters produce frozen outputs (tuples or strings) to maintain
immutability.
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from backend.ai.prompt.serializer import serialize_to_message_list, serialize_to_text
from backend.ai.prompt.template import PromptSection


class PromptFormat(str, Enum):
    """Supported output formats for prompt serialization."""

    GENERIC = "generic"
    CHATML = "chatml"
    OPENAI = "openai"
    GEMINI = "gemini"


def format_generic(sections: dict[PromptSection, str]) -> str:
    """Format as a single text block with [section] headers.

    This is the simplest format — one string, sections delimited by
    headers. Useful for debugging, logging, and local models.
    """
    return serialize_to_text(sections)


def format_chatml(sections: dict[PromptSection, str]) -> tuple[dict[str, str], ...]:
    """Format as ChatML-style message list.

    Returns a tuple of ``{"role": ..., "content": ...}`` dicts.
    Tuple (not list) to preserve immutability.
    """
    messages = serialize_to_message_list(sections)
    return tuple(messages)


def format_openai(sections: dict[PromptSection, str]) -> tuple[dict[str, str], ...]:
    """Format as OpenAI-style message list.

    OpenAI's chat API uses the same ``{"role", "content"}`` structure
    as ChatML. This is kept as a separate function because future
    OpenAI-specific formatting (e.g. function calling, tool schemas
    in a separate field) will diverge from ChatML.
    """
    messages = serialize_to_message_list(sections)
    return tuple(messages)


def format_gemini(sections: dict[PromptSection, str]) -> tuple[dict[str, Any], ...]:
    """Format as Gemini-style contents list.

    Gemini uses ``{"role": ..., "parts": [{"text": ...}]}`` structure.
    System-level sections are merged into a ``"user"`` role message
    (Gemini has no ``"system"`` role — system instructions are a
    separate field in the API, but we group them here for the
    formatter; the provider adapter will split them out).
    """
    messages = serialize_to_message_list(sections)
    gemini_msgs: list[dict[str, Any]] = []
    for msg in messages:
        role = msg["role"]
        if role == "system":
            role = "user"
        gemini_msgs.append({
            "role": role,
            "parts": [{"text": msg["content"]}],
        })
    return tuple(gemini_msgs)


_FORMATTERS = {
    PromptFormat.GENERIC: format_generic,
    PromptFormat.CHATML: format_chatml,
    PromptFormat.OPENAI: format_openai,
    PromptFormat.GEMINI: format_gemini,
}


def format_prompt(
    sections: dict[PromptSection, str],
    fmt: PromptFormat = PromptFormat.GENERIC,
) -> str | tuple[dict[str, Any], ...]:
    """Format sections into the specified provider style.

    Args:
        sections: Dict of ``PromptSection`` → section text.
        fmt:       The target format.

    Returns:
        A string (for generic) or a tuple of message dicts (for others).
    """
    formatter = _FORMATTERS.get(fmt)
    if formatter is None:
        raise ValueError(f"Unknown prompt format: {fmt}")
    return formatter(sections)
