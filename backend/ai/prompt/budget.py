"""
Token Budget Estimator — heuristic token counting without tokenizer libraries.

Estimates token counts using the rules from AI_MASTER_DESIGN.md §26.3:
  - 1 token ≈ 4 characters of English text
  - 1 token ≈ 2 characters of non-English text (Persian/Farsi)

The estimator is conservative — it overestimates to avoid sending
prompts that exceed a model's context window.

Budget caps from AI_MASTER_DESIGN.md §28.6:
  - System prompt:        ≤ 2,000 tokens
  - Conversation context:  ≤ 4,000 tokens
  - Memory injection:      ≤ 1,000 tokens
  - Tool result injection: ≤ 1,500 tokens
  - Total prompt per turn:  ≤ 8,500 tokens
  - Max output per turn:   ≤ 1,000 tokens
"""
from __future__ import annotations

from dataclasses import dataclass, field


# ── Budget caps (from §28.6) ──

DEFAULT_MAX_TOTAL_TOKENS = 8500
DEFAULT_MAX_OUTPUT_TOKENS = 1000
DEFAULT_MAX_SYSTEM_TOKENS = 2000
DEFAULT_MAX_CONTEXT_TOKENS = 4000
DEFAULT_MAX_MEMORY_TOKENS = 1000
DEFAULT_MAX_TOOL_RESULT_TOKENS = 1500


@dataclass(frozen=True)
class TokenBudget:
    """Estimated token budget for a prompt package.

    Attributes:
        estimated_input_tokens:  Estimated tokens consumed by the prompt.
        estimated_output_budget:  Estimated tokens reserved for the model's response.
        estimated_total:          estimated_input_tokens + estimated_output_budget.
        prompt_size_chars:         Total character count of the serialized prompt.
        max_total_tokens:          Configured ceiling for total tokens per turn.
        max_output_tokens:         Configured ceiling for output tokens per turn.
        within_budget:             Whether estimated_total ≤ max_total_tokens.
    """

    estimated_input_tokens: int
    estimated_output_budget: int
    estimated_total: int
    prompt_size_chars: int
    max_total_tokens: int
    max_output_tokens: int
    within_budget: bool


def estimate_tokens(text: str, language: str = "English") -> int:
    """Estimate the token count of a text string.

    Uses the heuristic from §26.3:
      - English: 1 token ≈ 4 characters
      - Non-English (Persian/Farsi): 1 token ≈ 2 characters

    The estimate is conservative (overestimates).
    """
    if not text:
        return 0
    char_count = len(text)
    ratio = 4 if language.lower().startswith("english") else 2
    return (char_count + ratio - 1) // ratio  # ceiling division


def estimate_tokens_for_sections(
    sections: dict[str, str],
    language: str = "English",
) -> dict[str, int]:
    """Estimate per-section token counts.

    Args:
        sections: A dict mapping section name → section text.
        language: The language to use for the estimation heuristic.

    Returns:
        A dict mapping section name → estimated token count.
    """
    return {name: estimate_tokens(text, language) for name, text in sections.items()}


def compute_budget(
    sections: dict[str, str],
    language: str = "English",
    max_total_tokens: int = DEFAULT_MAX_TOTAL_TOKENS,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
) -> TokenBudget:
    """Compute the full token budget for a set of prompt sections.

    Args:
        sections:          Dict of section name → section text.
        language:          Language for token estimation heuristic.
        max_total_tokens:  Ceiling for total tokens (input + output).
        max_output_tokens: Ceiling for output tokens.

    Returns:
        A frozen ``TokenBudget`` with all estimates computed.
    """
    total_chars = sum(len(text) for text in sections.values())
    input_tokens = sum(estimate_tokens(text, language) for text in sections.values())
    output_budget = min(max_output_tokens, max_total_tokens - input_tokens)
    if output_budget < 0:
        output_budget = 0
    estimated_total = input_tokens + output_budget
    within = estimated_total <= max_total_tokens

    return TokenBudget(
        estimated_input_tokens=input_tokens,
        estimated_output_budget=output_budget,
        estimated_total=estimated_total,
        prompt_size_chars=total_chars,
        max_total_tokens=max_total_tokens,
        max_output_tokens=max_output_tokens,
        within_budget=within,
    )
