"""
.ping  — Editing the trigger message with PONG (zero-spam policy).
.id    — Chat ID + Message ID of the current context.
.help  — Full command reference.
.health — Internal health report from backend/health.py.
"""
import logging
from telethon import events
from backend.bot.handlers.guard import is_owner
from backend import health

logger = logging.getLogger(__name__)

_HELP = (
    "━━━━━━━━━━━━\n"
    "🧠 **LifeOS**\n"
    "━━━━━━━━━━━━\n"
    "\n"
    "📦 **Save Engine**  _(reply to a message)_\n"
    "  `.save f` · `.s f` — Forward save\n"
    "  `.save d` · `.s d` — Deep save\n"
    "  `.send <code>`       — Forward asset here\n"
    "\n"
    "🔍 **Discovery**\n"
    "  `.list [n]`      — Recent saves\n"
    "  `.find <text>`   — Search saves\n"
    "  `.preview` · `.r` · `.retrieve <code>` — Metadata\n"
    "\n"
    "🗑 **Organizer**\n"
    "  `.del <n>`          — Delete last n messages\n"
    "  `.del id <msgid>`   — Delete from msgid\n"
    "  `.del <code>`       — Delete a saved item\n"
    "  `.organize list`    — Data overview\n"
    "  `.organize clean`   — Purge old logs\n"
    "  `.db clean`         — Remove orphan DB rows\n"
    "  `.db stats`         — Database statistics\n"
    "  `.db vacuum`        — Cleanup + optimize\n"
    "\n"
    "🧬 **Bio Engine**\n"
    "  `.bio on` · `.bio off`     — Toggle cron\n"
    "  `.bio template <tpl>`      — Set template\n"
    "  `.bio text <text>`         — Set {text}\n"
    "  `.bio mood <mood>`         — Set {mood}\n"
    "  `.bio show` · `.bio help`  — Inspect / tokens\n"
    "\n"
    "⚙️ **Utility**\n"
    "  `.ping`   — PONG\n"
    "  `.id`     — Chat & Msg IDs\n"
    "  `.health` — Health report\n"
    "  `.help`   — This message\n"
    "━━━━━━━━━━━━"
)


def _format_uptime(uptime_s):
    if uptime_s is None or uptime_s < 0:
        return "unknown"
    hours = int(uptime_s // 3600)
    minutes = int((uptime_s % 3600) // 60)
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _build_health_report(snap):
    process_ok = snap.get("process_alive", False)
    telegram_ok = snap.get("telethon_connected", False)
    supervisor_ok = snap.get("supervisor_ok", False)
    bio_cron_ok = snap.get("bio_cron_ok", False)
    heartbeat_age = snap.get("heartbeat_age_s")
    uptime_s = snap.get("uptime_s")
    status = snap.get("status", "unknown")

    def indicator(ok):
        return "🟢" if ok else "🔴"

    lines = ["🩺 **LifeOS Health**", ""]

    lines.append(f"{indicator(process_ok)} Process: {'Alive' if process_ok else 'Dead'}")
    lines.append(f"{indicator(telegram_ok)} Telegram: {'Connected' if telegram_ok else 'Disconnected'}")
    lines.append(f"{indicator(supervisor_ok)} Supervisor: {'Running' if supervisor_ok else 'Stopped'}")
    lines.append(f"{indicator(bio_cron_ok)} Bio Cron: {'Running' if bio_cron_ok else 'Stopped'}")

    lines.append("")
    lines.append("Heartbeat:")
    if heartbeat_age is not None:
        if heartbeat_age > 15.0:
            lines.append(f"• Last heartbeat: 🔴 Stale ({int(heartbeat_age)}s)")
        else:
            lines.append(f"• Last heartbeat: {int(heartbeat_age)}s ago")
    else:
        lines.append("• Last heartbeat: never")

    lines.append("")
    lines.append("Runtime:")
    lines.append(f"• Uptime: {_format_uptime(uptime_s)}")

    lines.append("")
    lines.append("Status:")
    if status == "ok":
        lines.append("Everything looks healthy.")
    else:
        lines.append("Issues detected — needs attention.")

    return "\n".join(lines)


def register(client, owner_id: int):

    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.ping$"))
    async def ping(event):
        if not is_owner(event, owner_id):
            return
        try:
            await event.edit("PONG")
        except Exception as exc:
            logger.warning("ping edit failed: %s", exc)

    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.id$"))
    async def id_cmd(event):
        if not is_owner(event, owner_id):
            return
        try:
            chat_id = event.chat_id
            msg_id = event.message.id
            reply = await event.message.get_reply_message()
            lines = [f"**Chat ID:** `{chat_id}`", f"**Msg ID:** `{msg_id}`"]
            if reply:
                lines.append(f"**Reply Msg ID:** `{reply.id}`")
                lines.append(f"**Reply Sender ID:** `{reply.sender_id}`")
            await event.edit("\n".join(lines))
        except Exception as exc:
            logger.warning("id_cmd failed: %s", exc)

    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.help$"))
    async def help_cmd(event):
        if not is_owner(event, owner_id):
            return
        try:
            await event.edit(_HELP)
        except Exception as exc:
            logger.warning("help edit failed: %s", exc)

    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.health$"))
    async def health_cmd(event):
        if not is_owner(event, owner_id):
            return
        try:
            snap = health.snapshot()
            report = _build_health_report(snap)
            await event.edit(report)
        except Exception as exc:
            logger.warning("health_cmd failed: %s", exc)
