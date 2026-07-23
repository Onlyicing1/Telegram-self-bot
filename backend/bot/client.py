"""
Telethon client factory — headless StringSession, never interactive.

Connection parameters tuned for Render Free tier:
  auto_reconnect     — transparently recover from network blips
  connection_retries — up to 5 attempts per disconnect event
  retry_delay        — 2 s between retry attempts
  flood_sleep_threshold — auto-sleep up to 60 s on Telegram flood responses
"""
import logging
from telethon import TelegramClient, events
from telethon.sessions import StringSession

logger = logging.getLogger(__name__)


async def build_client(
    api_id: int,
    api_hash: str,
    session_string: str,
) -> TelegramClient:
    client = TelegramClient(
        StringSession(session_string),
        api_id,
        api_hash,
        system_version="4.16.30-vxCUSTOM",
        device_model="LifeOS",
        auto_reconnect=True,
        connection_retries=5,
        retry_delay=2,
        flood_sleep_threshold=60,
    )
    await client.connect()

    if not await client.is_user_authorized():
        raise RuntimeError(
            "Telethon session is not authorized. "
            "Re-generate SESSION_STRING and update the environment variable."
        )

    me = await client.get_me()
    logger.info("Telethon connected as %s (id=%s)", me.first_name, me.id)

    _forensic_client_id = id(client)
    print(f"[FORENSIC] CHECKPOINT-1 after client.connect()+authorize: "
          f"client_id={id(client)}, user={me.first_name}, user_id={me.id}", flush=True)

    async def _raw_update_handler(update):
        print(f"[FORENSIC-RAW] Update received: type={type(update).__name__}, "
              f"client_id={id(client)}, "
              f"same_as_handler_client={id(client) == _forensic_client_id}", flush=True)

    client.add_event_handler(_raw_update_handler, events.Raw)
    print(f"[FORENSIC] Raw update handler registered on client_id={id(client)}", flush=True)

    async def _all_newmessage_handler(event):
        try:
            print(
                f"[FORENSIC-MSG] NewMessage BEFORE any filtering: "
                f"raw_text={event.raw_text!r}, "
                f"outgoing={getattr(event, 'out', 'N/A')}, "
                f"chat_id={event.chat_id}, "
                f"sender_id={event.sender_id}, "
                f"client_id={id(client)}, "
                f"same_as_handler_client={id(client) == _forensic_client_id}",
                flush=True,
            )
        except Exception as exc:
            print(f"[FORENSIC-MSG] Error reading event: {type(exc).__name__}: {exc}", flush=True)

    client.add_event_handler(_all_newmessage_handler, events.NewMessage())
    print(f"[FORENSIC] All-NewMessage handler registered on client_id={id(client)}", flush=True)

    return client
