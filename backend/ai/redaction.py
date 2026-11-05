"""One redaction path for provider error text.

A provider can echo a credential into an error body (a URL carrying
``?key=``, an ``Authorization``/``x-goog-api-key`` header, an ``sk-`` token).
Both the provider that logs a failed response and the tester that reports it
need the same rules, so they live here once instead of being duplicated.
"""
from __future__ import annotations

import re

REDACTED = "***REDACTED***"

_SECRET_PATTERNS = (
    re.compile(r"sk-[a-zA-Z0-9_\-]{8,}", re.IGNORECASE),
    re.compile(r"gsk_[a-zA-Z0-9_\-]{8,}", re.IGNORECASE),
    re.compile(r"ms-key-[a-zA-Z0-9_\-]{8,}", re.IGNORECASE),
    re.compile(r"Bearer\s+[a-zA-Z0-9_\.\-]{8,}", re.IGNORECASE),
    re.compile(r"key=[^&\s\"']+", re.IGNORECASE),
    re.compile(r"x-goog-api-key\s*[:=]\s*\S+", re.IGNORECASE),
)


def redact_secrets(text: str) -> str:
    """Replace credential-shaped substrings with a fixed marker."""
    if not text:
        return ""
    redacted = str(text)
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(REDACTED, redacted)
    return redacted
