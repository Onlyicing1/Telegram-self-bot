"""
Handler registration — wires all command handlers onto the Telethon client.

Each handler module is registered in isolation. If one crashes during
registration, the error is logged and the remaining handlers still register.

Forensic instrumentation: uses print(flush=True) to bypass logging
configuration and prove the exact execution path.
"""
import logging
import sys
import traceback

from backend.bot.handlers import misc, save, retrieve, delete, organize, bio, discover, database

logger = logging.getLogger(__name__)


def register_all(client, owner_id: int, tz_str: str):
    print(f"[FORENSIC] register_all ENTERED: client_id={id(client)}, owner_id={owner_id}, tz={tz_str}", flush=True)
    logger.info("REGISTER_ALL: client id=%s, owner_id=%s, tz=%s", id(client), owner_id, tz_str)
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
        print(f"[FORENSIC] register_all: calling '{name}.register()'", flush=True)
        try:
            fn()
            print(f"[FORENSIC] register_all: '{name}' registered OK", flush=True)
            logger.info("REGISTER_ALL: handler '%s' registered OK on client id(%s)", name, id(client))
        except Exception as exc:
            print(f"[FORENSIC] register_all: '{name}' FAILED: {type(exc).__name__}: {exc}", flush=True)
            traceback.print_exc(file=sys.stdout)
            logger.error("REGISTER_ALL: handler '%s' registration FAILED on client id(%s): %s", name, id(client), exc)
    print(f"[FORENSIC] register_all COMPLETED", flush=True)
