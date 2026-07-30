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

Global exception handlers:
  - sys.excepthook — catches uncaught synchronous exceptions
  - loop.set_exception_handler — catches uncaught asyncio exceptions
  Both route through the tracer so nothing disappears silently.
"""
import asyncio
import logging
import signal
import sys
import traceback

import backend.config as cfg_module
from backend.runtime.supervisor import RuntimeSupervisor
from backend.runtime.tracer import trace, trace_exception, trace_uncaught

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logging.getLogger("backend").setLevel(logging.INFO)
logging.getLogger("telethon").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


def _global_excepthook(exc_type, exc_value, exc_tb):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    trace_uncaught(exc_value)
    logger.error("UNCAUGHT synchronous exception:")
    traceback.print_exception(exc_type, exc_value, exc_tb, file=sys.stdout)


def _async_exception_handler(loop, context):
    exc = context.get("exception")
    if exc is not None:
        trace_exception("UNCAUGHT_EXCEPTION", exc, source="asyncio_loop")
    else:
        msg = context.get("message", "unknown async error")
        trace("UNCAUGHT_EXCEPTION", source="asyncio_loop", message=msg)
    logger.error("UNCAUGHT async exception: %s", context)
    loop.default_exception_handler(context)


async def main() -> None:
    cfg = cfg_module.load()

    loop = asyncio.get_running_loop()
    loop.set_exception_handler(_async_exception_handler)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(
                _handle_signal(supervisor_placeholder[0])
            ))
        except NotImplementedError:
            pass

    supervisor = RuntimeSupervisor(cfg)
    supervisor_placeholder[0] = supervisor

    await supervisor.start()

    await supervisor.shutdown_event.wait()

    await supervisor.stop()

    logger.info("LifeOS stopped cleanly.")


supervisor_placeholder: list = [None]


async def _handle_signal(supervisor):
    if supervisor is not None:
        supervisor.shutdown_event.set()
    trace("SHUTDOWN_SIGNAL", signal="SIGTERM/SIGINT")


if __name__ == "__main__":
    sys.excepthook = _global_excepthook
    asyncio.run(main())
