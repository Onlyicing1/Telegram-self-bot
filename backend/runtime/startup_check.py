"""
Centralized startup validation — runs before the bot becomes operational.

Validates every critical dependency and aborts cleanly on failure.
The bot never partially starts: if any CRITICAL check fails, the process
exits with a non-zero code so Render restarts it.

Check categories (severity: CRITICAL → abort, WARNING → log and continue):
  1. Environment variables (required vars must be present and non-empty)
  2. Supabase connectivity (optional but warned)
  3. Required AI tables exist (if Supabase is available)
  4. Telegram session validity (deferred to build_client — checked there)
  5. AI providers configured correctly
  6. Ghost Room configuration (optional — warned if missing)
  7. Required directories/files (writable temp dir, etc.)

Usage:
    from backend.runtime.startup_check import run_startup_checks
    results = run_startup_checks(cfg)
    if not results.ok:
        sys.exit(1)
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_REQUIRED_ENV = ("API_ID", "API_HASH", "SESSION_STRING", "BOT_OWNER_ID")
_REQUIRED_AI_TABLES = ("ai_sessions", "ai_messages", "ai_memories", "ai_tool_history")
_CORE_TABLES = ("saved_items", "bio_state", "bot_logs", "username_state", "panel_settings")


@dataclass
class CheckResult:
    name: str
    severity: str  # "CRITICAL" or "WARNING"
    passed: bool
    message: str = ""


@dataclass
class StartupReport:
    results: list[CheckResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(r.passed for r in self.results if r.severity == "CRITICAL")

    @property
    def warnings(self) -> list[CheckResult]:
        return [r for r in self.results if r.severity == "WARNING" and not r.passed]

    @property
    def failures(self) -> list[CheckResult]:
        return [r for r in self.results if r.severity == "CRITICAL" and not r.passed]

    def add(self, r: CheckResult) -> None:
        self.results.append(r)


def _check_env_vars(cfg: dict) -> CheckResult:
    missing = [k for k in _REQUIRED_ENV if not cfg.get(k) and not cfg.get(k.replace("BOT_", ""))]
    if missing:
        return CheckResult("env_vars", "CRITICAL", False,
                            f"Missing required env vars: {', '.join(missing)}")
    owner_id = cfg.get("OWNER_ID") or cfg.get("BOT_OWNER_ID")
    if not isinstance(owner_id, int) or owner_id <= 0:
        return CheckResult("env_vars", "CRITICAL", False,
                            "BOT_OWNER_ID must be a positive integer")
    return CheckResult("env_vars", "CRITICAL", True, "All required env vars present")


def _check_supabase(cfg: dict) -> CheckResult:
    url = cfg.get("SUPABASE_URL", "")
    key = cfg.get("SUPABASE_KEY", "")
    if not url or not key:
        return CheckResult("supabase", "WARNING", False,
                           "SUPABASE_URL/SUPABASE_KEY not set — in-memory fallback")
    try:
        from backend.db import client as db_client
        db = db_client.get_db()
        if db is None:
            return CheckResult("supabase", "WARNING", False,
                               "Supabase client init failed — in-memory fallback")
        db.table("bot_logs").select("id").limit(1).execute()
        return CheckResult("supabase", "WARNING", True, "Supabase reachable")
    except Exception as exc:
        return CheckResult("supabase", "WARNING", False,
                           f"Supabase unreachable: {exc} — in-memory fallback")


def _check_ai_tables(cfg: dict) -> CheckResult:
    if not cfg.get("SUPABASE_AVAILABLE"):
        return CheckResult("ai_tables", "WARNING", True,
                           "Skipped — Supabase not available")
    try:
        from backend.db import client as db_client
        db = db_client.get_db()
        if db is None:
            return CheckResult("ai_tables", "WARNING", True, "Skipped — no DB")
        missing = []
        for table in _REQUIRED_AI_TABLES:
            try:
                db.table(table).select("id").limit(1).execute()
            except Exception:
                missing.append(table)
        if missing:
            return CheckResult("ai_tables", "WARNING", False,
                               f"Missing AI tables: {', '.join(missing)} — AI uses in-memory")
        return CheckResult("ai_tables", "WARNING", True, "All AI tables present")
    except Exception as exc:
        return CheckResult("ai_tables", "WARNING", False, f"Check failed: {exc}")


def _check_core_tables(cfg: dict) -> CheckResult:
    if not cfg.get("SUPABASE_AVAILABLE"):
        return CheckResult("core_tables", "WARNING", True, "Skipped — Supabase not available")
    try:
        from backend.db import client as db_client
        db = db_client.get_db()
        if db is None:
            return CheckResult("core_tables", "WARNING", True, "Skipped — no DB")
        missing = []
        for table in _CORE_TABLES:
            try:
                db.table(table).select("id").limit(1).execute()
            except Exception:
                missing.append(table)
        if missing:
            return CheckResult("core_tables", "CRITICAL", False,
                               f"Missing core tables: {', '.join(missing)}")
        return CheckResult("core_tables", "CRITICAL", True, "All core tables present")
    except Exception as exc:
        return CheckResult("core_tables", "WARNING", False, f"Check failed: {exc}")


def _check_ai_providers(cfg: dict) -> CheckResult:
    try:
        from backend.ai.providers.factory import ProviderFactory
        available = ProviderFactory.available_providers()
        if not available:
            return CheckResult("ai_providers", "WARNING", False, "No providers registered")
        return CheckResult("ai_providers", "WARNING", True,
                           f"Providers available: {', '.join(available)}")
    except Exception as exc:
        return CheckResult("ai_providers", "WARNING", False, f"Provider check failed: {exc}")


def _check_ghost_room(cfg: dict) -> CheckResult:
    ghost = cfg.get("GHOST_ROOM_ID", "")
    if not ghost:
        return CheckResult("ghost_room", "WARNING", False,
                           "GHOST_ROOM_ID not set — feature unavailable")
    return CheckResult("ghost_room", "WARNING", True, "Ghost Room configured")


def _check_directories() -> CheckResult:
    try:
        tmp = tempfile.gettempdir()
        test_path = os.path.join(tmp, ".lifeos_startup_check")
        with open(test_path, "w") as f:
            f.write("ok")
        os.remove(test_path)
        return CheckResult("directories", "CRITICAL", True,
                           f"Temp dir writable: {tmp}")
    except Exception as exc:
        return CheckResult("directories", "CRITICAL", False,
                           f"Temp dir not writable: {exc}")


def _check_session_format(cfg: dict) -> CheckResult:
    session = cfg.get("SESSION_STRING", "")
    if not session:
        return CheckResult("session", "CRITICAL", False, "SESSION_STRING is empty")
    if len(session) < 50:
        return CheckResult("session", "CRITICAL", False,
                           "SESSION_STRING looks too short — may be invalid")
    return CheckResult("session", "CRITICAL", True, "SESSION_STRING present")


def run_startup_checks(cfg: dict) -> StartupReport:
    report = StartupReport()
    report.add(_check_env_vars(cfg))
    report.add(_check_session_format(cfg))
    report.add(_check_supabase(cfg))
    report.add(_check_core_tables(cfg))
    report.add(_check_ai_tables(cfg))
    report.add(_check_ai_providers(cfg))
    report.add(_check_ghost_room(cfg))
    report.add(_check_directories())

    for r in report.results:
        level = logging.INFO if r.passed else (logging.ERROR if r.severity == "CRITICAL" else logging.WARNING)
        logger.log(level, "[STARTUP CHECK] %s: %s — %s", r.severity, r.name, r.message)

    if report.failures:
        logger.error("[STARTUP CHECK] %d CRITICAL failure(s) — aborting:", len(report.failures))
        for f in report.failures:
            logger.error("  ✗ %s: %s", f.name, f.message)
    else:
        logger.info("[STARTUP CHECK] All critical checks passed.")
        if report.warnings:
            logger.warning("[STARTUP CHECK] %d warning(s):", len(report.warnings))
            for w in report.warnings:
                logger.warning("  ⚠ %s: %s", w.name, w.message)

    return report
