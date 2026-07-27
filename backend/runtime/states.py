"""
Runtime FSM states for the RuntimeSupervisor.

States:
  STARTING    — process boot, config loaded
  CONNECTING  — Telethon client connecting
  AUTHORIZING — Telethon session authorization check
  REGISTERING — command handler registration
  READY       — fully operational
  DEGRADED    — partially operational (e.g. helper bot down)
  RECOVERING  — attempting reconnect after transient failure
  REBUILDING  — rebuilding TelegramClient from scratch
  STOPPING    — shutdown in progress
  FAILED      — unrecoverable, process will exit
"""
from enum import Enum, auto


class RuntimeState(Enum):
    STARTING = auto()
    CONNECTING = auto()
    AUTHORIZING = auto()
    REGISTERING = auto()
    READY = auto()
    DEGRADED = auto()
    RECOVERING = auto()
    REBUILDING = auto()
    STOPPING = auto()
    FAILED = auto()

    def __str__(self) -> str:
        return self.name
