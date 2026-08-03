"""
Memory subsystem — three-tier memory architecture for the AI Core.

Exports:
    MemoryManager    — unified manager for all tiers
    ShortMemory      — per-request volatile memory
    LongMemory       — cross-session persistent memory (90-day retention)
    PermanentMemory  — always-in-prompt facts (never expires)
    MemoryEntry      — immutable memory record
    MemoryTier       — enum: SHORT, LONG, PERMANENT
    MemoryCategory   — enum: FACT, PREFERENCE, CONTEXT, SUMMARY, INSTRUCTION
    MemoryQuery      — query parameters for memory retrieval
"""
from backend.ai.memory.long import LongMemory, DEFAULT_RETENTION_DAYS
from backend.ai.memory.manager import MemoryManager
from backend.ai.memory.permanent import PermanentMemory
from backend.ai.memory.short import ShortMemory
from backend.ai.memory.types import MemoryCategory, MemoryEntry, MemoryQuery, MemoryTier

__all__ = [
    "MemoryManager",
    "ShortMemory",
    "LongMemory",
    "PermanentMemory",
    "MemoryEntry",
    "MemoryTier",
    "MemoryCategory",
    "MemoryQuery",
    "DEFAULT_RETENTION_DAYS",
]
