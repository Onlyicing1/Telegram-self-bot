"""
Token estimation — lightweight, no tokenizer.

estimated_tokens ≈ len(text) / 4

This is intentionally a coarse heuristic. It is NOT a tokenizer. It
exists only to give the runtime a deterministic, offline budget signal
for history trimming. The divisor of 4 is a widely-used approximation
(roughly 4 characters per token for English text) and is deliberately
fixed so the runtime remains deterministic.
"""
from __future__ import annotations


def estimate_tokens(text: str) -> int:
    """Return a deterministic token estimate for ``text``.

    Uses ``len(text) / 4`` rounded up so an empty string yields 0 and a
    single character yields 1. Never raises, never returns negative.
    """
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)
