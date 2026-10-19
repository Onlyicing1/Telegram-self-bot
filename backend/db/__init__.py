"""Database package.

Importing it installs the HTTP/1.1 Supabase client factory on the public
``supabase.create_client`` entry point, so the one shared client that
``backend.db.client.get_db()`` builds is never multiplexed over a single TLS
socket by HTTP/2. See ``backend/db/transport.py`` for the measurements behind
that choice.

A missing transport dependency is not fatal: ``get_db()`` already degrades to the
in-memory fallback when it cannot build a client.
"""
import logging

logger = logging.getLogger(__name__)

try:
    from backend.db import transport
except ImportError:  # supabase/httpx/postgrest unavailable: get_db() falls back
    transport = None
    logger.warning("[SAVE_DB] Supabase transport unavailable — get_db() will fall back.")
else:
    transport.install_http1_transport()

__all__ = ["transport"]
