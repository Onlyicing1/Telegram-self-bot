"""Temporary trace collector for .help debugging.

Collects trace checkpoints in memory during a single .help execution,
then sends ONE message to Saved Messages with the complete trace.

This module is temporary and must be removed after debugging is complete.
"""
import time
from datetime import datetime

_entries: list[str] = []
_start: float = 0.0


def reset() -> None:
    global _entries, _start
    _entries = []
    _start = time.monotonic()


def trace(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S.") + f"{int((time.time() % 1) * 1000):03d}"
    elapsed = time.monotonic() - _start if _start else 0.0
    _entries.append(f"[{ts}] (+{elapsed:.3f}s) {msg}")


async def flush_to_saved_messages(client) -> None:
    if not _entries:
        return
    text = "===== HELP TRACE =====\n" + "\n".join(_entries) + "\n===== END ====="
    try:
        await client.send_message("me", text)
    except Exception:
        pass
    _entries.clear()
