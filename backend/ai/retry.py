"""Deterministic retry classification and backoff policy."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

MAX_ATTEMPTS = 3
BASE_BACKOFF_SECONDS = 30
MAX_BACKOFF_SECONDS = 15 * 60


class FailureClass:
    RETRYABLE = "retryable"
    PERMANENT = "permanent"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RetryDecision:
    classification: str
    retry: bool
    reason: str


def classify_failure(error: BaseException | str) -> RetryDecision:
    if isinstance(error, (KeyboardInterrupt, SystemExit)):
        return RetryDecision(FailureClass.CANCELLED, False, type(error).__name__)
    if isinstance(error, BaseException):
        if isinstance(error, TimeoutError):
            return RetryDecision(FailureClass.RETRYABLE, True, "timeout")
        name = type(error).__name__.lower()
        text = str(error).lower()
    else:
        name = ""
        text = error.lower()
    if "cancel" in name or "cancel" in text:
        return RetryDecision(FailureClass.CANCELLED, False, "cancelled")
    if "timeout" in name or "temporar" in text or "rate limit" in text:
        return RetryDecision(FailureClass.RETRYABLE, True, "transient")
    return RetryDecision(FailureClass.UNKNOWN, False, "unclassified")


def retry_delay(attempt: int) -> timedelta:
    if not 1 <= attempt < MAX_ATTEMPTS:
        raise ValueError("retry delay requires an attempt below the maximum")
    return timedelta(seconds=min(MAX_BACKOFF_SECONDS, BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))))


def can_retry(attempt: int) -> bool:
    return 1 <= attempt < MAX_ATTEMPTS
