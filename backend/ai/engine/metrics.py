"""
Metrics — in-RAM execution metrics for the AI Engine.

Collects aggregate statistics across every ``execute()`` call. Nothing
is persisted to a database. The metrics object is owned by the engine
and updated by the dispatcher after each run.

Collected:
  - Execution count (total, successful, failed)
  - Average latency (seconds)
  - Provider usage (per-provider call count)
  - Conversation count (distinct owners seen)
  - Prompt size (cumulative chars)
  - Estimated tokens (cumulative prompt + completion)
  - Failures (per-error-message count)

Everything is deterministic and lives in RAM only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Set


@dataclass
class EngineMetrics:
    """Aggregate execution metrics for the AI Engine. In-memory only."""

    total_executions: int = 0
    successful_executions: int = 0
    failed_executions: int = 0
    total_latency_seconds: float = 0.0
    min_latency_seconds: float = 0.0
    max_latency_seconds: float = 0.0
    provider_usage: Dict[str, int] = field(default_factory=dict)
    conversation_owners: Set[int] = field(default_factory=set)
    total_prompt_chars: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    failure_counts: Dict[str, int] = field(default_factory=dict)

    def record(
        self,
        *,
        success: bool,
        provider: str,
        owner_id: int,
        latency: float,
        prompt_chars: int,
        prompt_tokens: int,
        completion_tokens: int,
        error: str = "",
    ) -> None:
        self.total_executions += 1
        self.total_latency_seconds += latency
        if self.min_latency_seconds == 0.0 or latency < self.min_latency_seconds:
            self.min_latency_seconds = latency
        if latency > self.max_latency_seconds:
            self.max_latency_seconds = latency

        if success:
            self.successful_executions += 1
        else:
            self.failed_executions += 1
            if error:
                self.failure_counts[error] = self.failure_counts.get(error, 0) + 1

        if provider:
            self.provider_usage[provider] = self.provider_usage.get(provider, 0) + 1

        self.conversation_owners.add(owner_id)
        self.total_prompt_chars += prompt_chars
        self.total_prompt_tokens += prompt_tokens
        self.total_completion_tokens += completion_tokens

    def average_latency(self) -> float:
        if self.total_executions == 0:
            return 0.0
        return self.total_latency_seconds / self.total_executions

    def conversation_count(self) -> int:
        return len(self.conversation_owners)

    def snapshot(self) -> dict:
        return {
            "total_executions": self.total_executions,
            "successful_executions": self.successful_executions,
            "failed_executions": self.failed_executions,
            "average_latency": round(self.average_latency(), 6),
            "min_latency": round(self.min_latency_seconds, 6),
            "max_latency": round(self.max_latency_seconds, 6),
            "provider_usage": dict(self.provider_usage),
            "conversation_count": self.conversation_count(),
            "total_prompt_chars": self.total_prompt_chars,
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_completion_tokens": self.total_completion_tokens,
            "failure_counts": dict(self.failure_counts),
        }

    def reset(self) -> None:
        self.total_executions = 0
        self.successful_executions = 0
        self.failed_executions = 0
        self.total_latency_seconds = 0.0
        self.min_latency_seconds = 0.0
        self.max_latency_seconds = 0.0
        self.provider_usage.clear()
        self.conversation_owners.clear()
        self.total_prompt_chars = 0
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.failure_counts.clear()
