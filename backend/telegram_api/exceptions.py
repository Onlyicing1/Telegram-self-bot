"""
Exception hierarchy for the Telegram Internal API layer.

All exceptions inherit from ``TelegramAPIError`` so callers can catch
the entire family with a single ``except TelegramAPIError``.
"""
from __future__ import annotations


class TelegramAPIError(Exception):
    """Base exception for all Telegram API layer errors."""


class TelegramTimeoutError(TelegramAPIError):
    """Raised when a Telegram RPC call exceeds the bounded timeout."""


class TelegramFloodWaitError(TelegramAPIError):
    """Raised when Telegram returns a FloodWait response."""

    def __init__(self, seconds: int) -> None:
        self.seconds = seconds
        super().__init__(f"FloodWait: must wait {seconds}s")


class TelegramNotFoundError(TelegramAPIError):
    """Raised when a requested entity (chat, user, message) is not found."""
