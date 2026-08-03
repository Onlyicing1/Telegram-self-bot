"""
Prompt Builder Layer — converts ConversationContext into a deterministic PromptPackage.

This package is the SOLE consumer of ``ConversationContext`` (produced
by the Conversation Layer) and the SOLE producer of ``PromptPackage``
(consumed by future AI providers).

What this package does:
  - Assembles prompt sections in a fixed, deterministic order
  - Estimates token budgets using heuristic rules (no tokenizer libs)
  - Validates the assembled package for completeness
  - Formats the package into multiple provider styles (generic, ChatML, OpenAI, Gemini)
  - Produces one immutable ``PromptPackage`` object

What this package does NOT do:
  - Call any AI model (Gemini, OpenAI, OpenRouter, etc.)
  - Generate provider-specific API payloads
  - Execute tools
  - Access Telegram, Supabase, or any external service
  - Modify any existing feature
  - Use globals or singletons

Public API::

    from backend.ai.prompt import (
        PromptBuilder,
        PromptPackage,
        PromptSection,
        PromptFormat,
        TokenBudget,
        PromptValidator,
        format_prompt,
        estimate_tokens,
        compute_budget,
    )

Architecture (from AI_MASTER_DESIGN.md §7, §26)::

    ConversationContext (from Conversation Layer)
           │
           ▼
    ┌──────────────────────────────────────────────┐
    │                PromptBuilder                   │
    │  ├─ Template   (static section templates)      │
    │  ├─ Budget     (heuristic token estimation)     │
    │  ├─ Validator  (completeness checks)            │
    │  └─ Formatter  (generic/ChatML/OpenAI/Gemini)   │
    └──────────────────────┬───────────────────────┘
                           │
                           ▼
    ┌──────────────────────────────────────────────┐
    │             PromptPackage (frozen)             │
    │  ├─ system_prompt                              │
    │  ├─ runtime_context                            │
    │  ├─ conversation_context                       │
    │  ├─ tool_context                               │
    │  ├─ user_input                                 │
    │  ├─ metadata                                   │
    │  ├─ estimated_tokens (TokenBudget)             │
    │  └─ sections: dict[PromptSection, str]          │
    └──────────────────────┬───────────────────────┘
                           │
                           ▼ (future, not built yet)
    ┌──────────────────────────────────────────────┐
    │               AI Provider                      │
    │  (receives PromptPackage, calls model)         │
    └──────────────────────────────────────────────┘

Prompt section order (NEVER changes):
    1. System Rules
    2. Platform Constraints
    3. Runtime Rules
    4. Current Context
    5. Conversation State
    6. Current Tool Metadata
    7. Tool Results (future)
    8. User Message
    9. Output Instructions
"""
from backend.ai.prompt.budget import (
    DEFAULT_MAX_CONTEXT_TOKENS,
    DEFAULT_MAX_MEMORY_TOKENS,
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEFAULT_MAX_SYSTEM_TOKENS,
    DEFAULT_MAX_TOOL_RESULT_TOKENS,
    DEFAULT_MAX_TOTAL_TOKENS,
    TokenBudget,
    compute_budget,
    estimate_tokens,
    estimate_tokens_for_sections,
)
from backend.ai.prompt.builder import PromptBuilder, PromptPackage
from backend.ai.prompt.formatter import (
    PromptFormat,
    format_chatml,
    format_generic,
    format_gemini,
    format_openai,
    format_prompt,
)
from backend.ai.prompt.serializer import serialize_to_message_list, serialize_to_text
from backend.ai.prompt.template import (
    MANDATORY_SECTIONS,
    SECTION_ORDER,
    PromptSection,
)
from backend.ai.prompt.validator import (
    InvalidPromptPackage,
    validate_budget,
    validate_prompt_package,
    validate_sections,
)

__all__ = [
    # Builder
    "PromptBuilder",
    "PromptPackage",
    # Template
    "PromptSection",
    "SECTION_ORDER",
    "MANDATORY_SECTIONS",
    # Budget
    "TokenBudget",
    "estimate_tokens",
    "estimate_tokens_for_sections",
    "compute_budget",
    "DEFAULT_MAX_TOTAL_TOKENS",
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "DEFAULT_MAX_SYSTEM_TOKENS",
    "DEFAULT_MAX_CONTEXT_TOKENS",
    "DEFAULT_MAX_MEMORY_TOKENS",
    "DEFAULT_MAX_TOOL_RESULT_TOKENS",
    # Serializer
    "serialize_to_text",
    "serialize_to_message_list",
    # Formatter
    "PromptFormat",
    "format_prompt",
    "format_generic",
    "format_chatml",
    "format_openai",
    "format_gemini",
    # Validator
    "InvalidPromptPackage",
    "validate_prompt_package",
    "validate_sections",
    "validate_budget",
]
