"""
Handler registration — wires all command handlers onto the Telethon client.

Each handler module is registered in isolation. If one crashes during
registration, the error is logged and the remaining handlers still register.
"""
import logging

from backend.bot.handlers import misc, save, retrieve, delete, organize, bio, discover, database

logger = logging.getLogger(__name__)


def register_all(client, owner_id: int, tz_str: str):
    handlers = [
        ("misc", lambda: misc.register(client, owner_id)),
        ("save", lambda: save.register(client, owner_id, tz_str)),
        ("retrieve", lambda: retrieve.register(client, owner_id)),
        ("delete", lambda: delete.register(client, owner_id)),
        ("organize", lambda: organize.register(client, owner_id)),
        ("bio", lambda: bio.register(client, owner_id, tz_str)),
        ("discover", lambda: discover.register(client, owner_id, tz_str)),
        ("database", lambda: database.register(client, owner_id, tz_str)),
    ]

    for name, fn in handlers:
        try:
            fn()
            logger.info("Handler '%s' registered.", name)
        except Exception as exc:
            logger.error("Handler '%s' registration FAILED: %s", name, exc)
