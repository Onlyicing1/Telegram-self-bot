"""
LifeOS — deterministic entry point.

Everything starts through the RuntimeSupervisor, which owns every runtime
coroutine: self-client run loop, heartbeat, helper bot, bio cron, web server.

Startup:
  1. Config validation (hard-exit on missing required vars)
  2. RuntimeSupervisor.start() — connects, authorizes, registers,
     starts helper, bio cron, web server, heartbeat, run loop

Shutdown (SIGTERM/SIGINT):
  RuntimeSupervisor.stop() — deterministic shutdown of all tasks,
  bio cron, helper, and self-client.

If recovery fails repeatedly, the supervisor calls sys.exit(1) so
Render's platform restarts the process automatically.
"""
import asyncio
import logging
import signal
import sys

import backend.config as cfg_module
from backend.runtime.supervisor import RuntimeSupervisor

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logging.getLogger("backend").setLevel(logging.INFO)
logging.getLogger("telethon").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


async def main() -> None:
    cfg = cfg_module.load()

    supervisor = RuntimeSupervisor(cfg)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, supervisor.shutdown_event.set)
        except NotImplementedError:
            pass

    await supervisor.start()

    await supervisor.shutdown_event.wait()

    await supervisor.stop()

    logger.info("LifeOS stopped cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
