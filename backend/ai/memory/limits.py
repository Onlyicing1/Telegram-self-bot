"""
Memory limits — the single source of truth for every memory bound.

All memory bounds enforced by the memory layer live here so the
dispatcher, manager, and repositories never scatter magic numbers.

The prompt-token budget reuses the canonical constant from
``backend.ai.prompt.budget`` (``DEFAULT_MAX_MEMORY_TOKENS``) instead of
introducing a second token-budget implementation.
"""
from __future__ import annotations

from backend.ai.memory.types import MemoryEntry
from backend.ai.prompt.budget import DEFAULT_MAX_MEMORY_TOKENS

# ── Retrieval bounds ──
MAX_LONG_RECORDS = 10              # long-tier entries fetched per request
MAX_PERMANENT_RECORDS = 100        # permanent-tier entries fetched per request
RETRIEVAL_MIN_IMPORTANCE = 0.3     # long-tier minimum importance filter

# ── Prompt budget (memory section) ──
MAX_MEMORY_PROMPT_TOKENS = DEFAULT_MAX_MEMORY_TOKENS  # 1000

# ── Write bounds ──
MAX_MEMORY_ENTRY_CHARS = 2000      # per-entry content cap (rejected, never truncated)

# ── Latency bound ──
MEMORY_READ_TIMEOUT_S = 2.0        # bounded memory retrieval inside the dispatcher


def fit_entries_to_token_budget(
    entries: list[MemoryEntry],
    cap_tokens: int = MAX_MEMORY_PROMPT_TOKENS,
) -> list[MemoryEntry]:
    """Deterministic prefix of ``entries`` whose estimated tokens fit the cap.

    Entries are assumed pre-ranked (repositories return importance-descending,
    creation-descending order). The prefix rule keeps ranking deterministic and
    never fabricates token counts — it uses the same estimator as the prompt
    budget. The first entry is always kept so a single large memory still
    surfaces instead of silently vanishing.
    """
    from backend.ai.prompt.budget import estimate_tokens

    total = 0
    kept: list[MemoryEntry] = []
    for entry in entries:
        estimated = estimate_tokens(entry.content)
        if total + estimated > cap_tokens and kept:
            break
        kept.append(entry)
        total += estimated
    return kept
