"""
Organizer — removed.

All Organizer functionality has been moved to other panels:
  - Delete last N / Delete from Msg ID → .panel context panel (delete)
  - Data Overview and Clean Old Logs → removed

This module is kept as a no-op stub so the router import doesn't break.
"""
import logging

logger = logging.getLogger(__name__)


def register(client, owner_id: int):
    pass
