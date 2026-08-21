"""
Diagnostics module — in-memory event history ("black box").

Provides:
  - An in-memory circular event log (500 entries, automatic overwrite)
  - Event recording from every subsystem (Telethon, Bio, DB, Save, etc.)
  - Event filtering and formatting

No database, no disk writes. All operations are synchronous and
non-blocking — they touch module-level state only, never perform I/O.
"""
import logging
from collections import deque
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_RING_SIZE = 500
_event_ring: deque = deque(maxlen=_RING_SIZE)

_TG_MSG_LIMIT = 4096


def record_event(module: str, action: str, duration_ms: float, result: str, details: str | None = None) -> None:
    entry = {
        "ts": datetime.now(timezone.utc),
        "module": module,
        "action": action,
        "duration_ms": round(duration_ms, 1),
        "result": result,
        "details": details,
    }
    _event_ring.append(entry)


def get_events() -> list:
    return list(_event_ring)


def _format_duration(ms: float) -> str:
    if ms < 1000:
        return f"{int(ms)}ms"
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{minutes}m {secs}s"


def _format_event(e: dict) -> str:
    ts = e["ts"].strftime("%H:%M:%S")
    dur = _format_duration(e["duration_ms"])
    line = f"{ts} | {e['module']} | {e['action']} | {dur} | {e['result']}"
    if e.get("details"):
        line += f" | {e['details']}"
    return line


def filter_events(limit: int = 20, module: str | None = None, errors_only: bool = False) -> list:
    events = get_events()
    if errors_only:
        events = [e for e in events if e["result"] not in ("SUCCESS",)]
    if module:
        events = [e for e in events if e["module"].lower() == module.lower()]
    events.reverse()
    return events[:limit]


def format_events(events: list) -> str:
    if not events:
        return "📭 No events recorded."
    lines = [f"📋 **Event Log** ({len(events)})", ""]
    for e in events:
        lines.append(f"```\n{_format_event(e)}\n```")
    return "\n".join(lines)


def split_message(text: str, limit: int = _TG_MSG_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            parts.append(remaining)
            break
        chunk = remaining[:limit]
        last_nl = chunk.rfind("\n")
        if last_nl > limit // 2:
            split_at = last_nl
        else:
            split_at = limit
        parts.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")
    return parts
