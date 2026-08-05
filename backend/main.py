"""
LifeOS — deterministic entry point with full crash diagnostics.

Everything starts through the RuntimeSupervisor, which owns every runtime
coroutine: self-client run loop, heartbeat, helper bot, bio cron, web server.

Crash diagnostics:
  - Global exception hooks (sys.excepthook, threading.excepthook, asyncio loop)
  - Signal capture (SIGTERM, SIGINT, SIGABRT, SIGQUIT)
  - SystemExit / KeyboardInterrupt / GeneratorExit capture
  - Last-exception ring buffer (100 exceptions, 100 warnings, 100 events)
  - Crash snapshot dumped before every process exit
  - PROCESS_EXIT_REASON trace before every exit — no silent exits

Startup:
  1. Config validation (hard-exit on missing required vars)
  2. Crash diagnostics installed (signal handlers, exception hooks, atexit)
  3. RuntimeSupervisor.start() — connects, authorizes, registers,
     starts helper, bio cron, web server, heartbeat, run loop

Shutdown (SIGTERM/SIGINT):
  RuntimeSupervisor.stop() — deterministic shutdown of all tasks,
  bio cron, helper, and self-client.

If recovery fails repeatedly, the supervisor calls sys.exit(1) so
Render's platform restarts the process automatically. Before that exit,
the crash diagnostics module dumps the full crash snapshot.
"""
import asyncio
import atexit
import logging
import signal
import sys
import traceback

import backend.config as cfg_module
from backend.runtime.supervisor import RuntimeSupervisor
from backend.runtime.tracer import trace, trace_exception, trace_uncaught
from backend.runtime.task_guard import guarded_create_task
from backend.runtime.crash_diagnostics import (
    install_all as install_crash_diagnostics,
    record_exit_reason,
    dump_crash_snapshot,
    dump_buffers,
    record_exception,
    record_runtime_event,
    capture_signal,
    uptime_s,
)

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logging.getLogger("backend").setLevel(logging.INFO)
logging.getLogger("telethon").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

supervisor_placeholder: list = [None]


def _atexit_handler() -> None:
    from backend.runtime.crash_diagnostics import exit_reason_is_set, get_exit_reason
    if not exit_reason_is_set():
        record_exit_reason("UNKNOWN", "atexit called without prior exit reason")
    dump_buffers()
    trace("PROCESS_TERMINATING", exit_reason=get_exit_reason(), uptime=f"{uptime_s()}s")


async def main() -> None:
    cfg = cfg_module.load()

    loop = asyncio.get_running_loop()

    install_crash_diagnostics()
    atexit.register(_atexit_handler)

    from backend.runtime.crash_diagnostics import _async_loop_exception_handler
    loop.set_exception_handler(_async_loop_exception_handler)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, lambda s=sig: guarded_create_task(
                _handle_signal(supervisor_placeholder[0], s), name="lifeos-signal-handler"
            ))
        except (NotImplementedError, RuntimeError):
            pass

    supervisor = RuntimeSupervisor(cfg)
    supervisor_placeholder[0] = supervisor

    startup_attempts = 0
    while True:
        startup_attempts += 1
        try:
            await supervisor.start()
            break
        except Exception as exc:
            trace_exception("STARTUP_FAILED", exc, attempt=startup_attempts)
            record_exception(exc, source=f"startup_attempt_{startup_attempts}")
            logger.error("Startup attempt %d failed: %s", startup_attempts, exc)
            if startup_attempts >= 5:
                record_exit_reason("STARTUP_FAILED", f"attempt {startup_attempts}: {exc}")
                dump_crash_snapshot(reason=f"startup_failed:{type(exc).__name__}")
                logger.error("Startup failed after %d attempts — exiting so Render restarts", startup_attempts)
                sys.exit(1)
            delay = min(30.0, 2.0 * (2 ** startup_attempts))
            logger.info("Retrying startup in %.1fs...", delay)
            await asyncio.sleep(delay)

    await supervisor.shutdown_event.wait()

    await supervisor.stop()

    record_exit_reason("CLEAN_SHUTDOWN", "supervisor.stop() completed")
    logger.info("LifeOS stopped cleanly.")


async def _handle_signal(supervisor, signum: int) -> None:
    sig_name = _signal_name(signum)
    trace("SIGNAL_RECEIVED", signal=sig_name, signum=signum, uptime=f"{uptime_s()}s")
    record_runtime_event("SIGNAL_RECEIVED", f"{sig_name} (signum={signum})")

    if supervisor is not None:
        supervisor.shutdown_event.set()


def _signal_name(signum: int) -> str:
    names = {
        signal.SIGTERM: "SIGTERM",
        signal.SIGINT: "SIGINT",
        signal.SIGABRT: "SIGABRT",
    }
    if hasattr(signal, "SIGQUIT"):
        names[signal.SIGQUIT] = "SIGQUIT"
    return names.get(signum, f"SIGNAL_{signum}")


if __name__ == "__main__":
    install_crash_diagnostics()
    atexit.register(_atexit_handler)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        record_exit_reason("KEYBOARD_INTERRUPT", "KeyboardInterrupt at top level")
        dump_crash_snapshot(reason="KeyboardInterrupt")
        sys.exit(130)
    except SystemExit:
        if not _exit_reason_already_set():
            record_exit_reason("SYSTEM_EXIT", "SystemExit at top level")
        dump_crash_snapshot(reason="SystemExit")
        raise
    except BaseException as exc:
        record_exit_reason("UNHANDLED_EXCEPTION", f"{type(exc).__name__}: {exc}")
        record_exception(exc, source="main_baseException")
        dump_crash_snapshot(reason=f"unhandled:{type(exc).__name__}")
        trace_exception("RUNTIME_ABORT", exc)
        traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stdout)
        sys.exit(1)


def _exit_reason_already_set() -> bool:
    from backend.runtime.crash_diagnostics import exit_reason_is_set
    return exit_reason_is_set()
