"""
Handler registration — wires all command handlers onto the Telethon client.

Each handler module is registered in isolation. If one crashes during
registration, the error is logged and the remaining handlers still register.

Runtime hooks are registered before command handlers so the supervisor can
track last_update timestamps from incoming Telegram events.
"""
import logging
import sys
import traceback

from telethon import events

from backend.bot.handlers import misc, save, retrieve, delete, organize, bio, discover, database
from backend.runtime.tracer import trace_handler_exception

logger = logging.getLogger(__name__)


def register_runtime_hooks(client) -> None:
    """Register runtime event hooks before command handlers.

    These hooks track last_update timestamps for health telemetry and
    emit SELF_RECONNECTING/SELF_RECONNECTED trace events on Telethon
    internal state changes.
    They must be registered first so they fire before any command handler.
    """
    @client.on(events.NewMessage())
    async def _runtime_update_hook(event):
        from backend.health import set_last_update, set_last_telethon_event
        try:
            set_last_update()
            set_last_telethon_event()
        except Exception:
            pass

    @client.on(events.Raw)
    async def _raw_hook(event):
        from backend.health import set_last_telethon_event
        from backend.runtime.tracer import trace
        try:
            set_last_telethon_event()
            if hasattr(event, 'className') and 'UpdateConfig' in str(type(event)):
                pass
        except Exception:
            pass


def register_all(client, owner_id: int, tz_str: str):
    logger.info("REGISTER_ALL: client id=%s, owner_id=%s, tz=%s", id(client), owner_id, tz_str)

    register_runtime_hooks(client)

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
            logger.info("REGISTER_ALL: handler '%s' registered OK on client id(%s)", name, id(client))
        except Exception as exc:
            traceback.print_exc(file=sys.stdout)
            logger.error("REGISTER_ALL: handler '%s' registration FAILED on client id(%s): %s", name, id(client), exc)
