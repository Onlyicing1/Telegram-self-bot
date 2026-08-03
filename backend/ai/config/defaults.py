"""
Defaults — default values for every AIConfig field.

All defaults are provider-independent. No provider-specific overrides
live here. Provider overrides are applied by ``ConfigManager`` at
snapshot time using the provider-override registry (see
``config.py``).

These defaults follow AI_MASTER_DESIGN.md:
  - temperature: 1.0 (neutral sampling)
  - top_p: 1.0 (full nucleus)
  - max_tokens: 4096 (standard output budget)
  - timeout: 30 seconds (§4.8)
  - retry_count: 3 (§4.7 — exponential backoff)
  - history_budget: 4000 tokens (§5.2 — summarization threshold)
  - tool_budget: 2000 tokens (§7 — tool schema budget)
"""
from __future__ import annotations

DEFAULT_ENABLED: bool = False
DEFAULT_PROVIDER: str = "dummy"
DEFAULT_MODEL: str = "dummy-1"
DEFAULT_TEMPERATURE: float = 1.0
DEFAULT_TOP_P: float = 1.0
DEFAULT_MAX_TOKENS: int = 4096
DEFAULT_TIMEOUT: int = 30
DEFAULT_RETRY_COUNT: int = 3
DEFAULT_SYSTEM_PROMPT: str = (
    "You are LifeOS Assistant, an AI integrated into a Telegram self-bot. "
    "You help the owner manage their Telegram account. "
    "You call tools to perform actions. You never perform actions directly. "
    "You respond concisely. You do not hallucinate capabilities. "
    "If you are unsure, you ask for clarification."
)
DEFAULT_HISTORY_BUDGET: int = 4000
DEFAULT_TOOL_BUDGET: int = 2000
DEFAULT_STREAMING_ENABLED: bool = False
DEFAULT_VISION_ENABLED: bool = False
DEFAULT_REASONING_ENABLED: bool = False
DEFAULT_DEVELOPER_MODE: bool = False
