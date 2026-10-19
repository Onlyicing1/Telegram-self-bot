"""Supabase client construction, pinned to HTTP/1.1.

``postgrest`` builds its own ``httpx.Client`` with ``http2=True`` hard-coded
(``postgrest/_sync/client.py::create_session``), and supabase-py constructs that
client internally, so nothing in this repository could turn HTTP/2 off. HTTP/2
multiplexes every request of the shared client onto ONE TLS connection, and
``httpcore`` guards that connection with two independent locks (``_read_lock``
and ``_write_lock``) over h2 state that is not thread-safe. With the bounded
four-worker pool dispatching synchronous calls concurrently, one worker can read
while another writes the same socket; the resulting teardown fails in-flight
reads immediately, which is the production shape (``ReadError <- ReadError <-
BlockingIOError(errno=11)``, ``elapsed=0.0s``).

Measured on this stack: one shared client + HTTP/2 + four threads failed
repeatedly, while the same load over HTTP/1.1, or with one client per thread, or
with one thread, stayed clean. HTTP/1.1 also gives every in-flight request its
own pooled connection, so no socket is ever read and written by two threads.

Only the transport changes here: the same one shared client, the same worker
pool, the same transport deadline, the same headers, timeout, redirect and
verification behaviour, and the same PostgREST request path. Disabling HTTP/2 is
not a retry, a timeout change or a fallback: a genuine transport failure still
raises exactly as before.
"""
from __future__ import annotations

import logging
from typing import Dict, Optional, Union

import httpx
import supabase
from postgrest import SyncPostgrestClient
from postgrest.constants import DEFAULT_POSTGREST_CLIENT_TIMEOUT
from supabase import Client as SupabaseClient
from supabase import ClientOptions

logger = logging.getLogger(__name__)

__all__ = [
    "HTTP1Session",
    "HTTP1PostgrestClient",
    "HTTP1SupabaseClient",
    "create_http1_client",
    "install_http1_transport",
]


class HTTP1Session(httpx.Client):
    """The PostgREST httpx session, with HTTP/2 never offered.

    ``httpcore`` builds its ALPN list as ``["http/1.1", "h2"] if http2 else
    ["http/1.1"]`` and only ever selects ``HTTP2Connection`` when h2 was
    negotiated, so ``http2=False`` on this session's transport is what removes
    the shared multiplexed socket.

    ``postgrest`` closes its session through ``session.aclose()`` (its own
    ``SyncClient`` helper defines it); httpx's synchronous client only has
    ``close()``.
    """

    def aclose(self) -> None:
        self.close()


class HTTP1PostgrestClient(SyncPostgrestClient):
    """PostgREST client whose session is HTTP/1.1 only."""

    def create_session(
        self,
        base_url: str,
        headers: Dict[str, str],
        timeout: Union[int, float, httpx.Timeout],
        verify: bool = True,
    ) -> HTTP1Session:
        return HTTP1Session(
            base_url=base_url,
            headers=headers,
            timeout=timeout,
            verify=verify,
            follow_redirects=True,
            http2=False,
        )


class HTTP1SupabaseClient(SupabaseClient):
    """The shared Supabase client, built on the HTTP/1.1 PostgREST session.

    Overriding the factory (rather than swapping an existing session) keeps the
    pin in force if supabase-py rebuilds its PostgREST client, which it does on
    an auth-state change.
    """

    @staticmethod
    def _init_postgrest_client(
        rest_url: str,
        headers: Dict[str, str],
        schema: str,
        timeout: Union[int, float, httpx.Timeout] = DEFAULT_POSTGREST_CLIENT_TIMEOUT,
    ) -> HTTP1PostgrestClient:
        return HTTP1PostgrestClient(
            rest_url, headers=headers, schema=schema, timeout=timeout
        )


def create_http1_client(
    url: str,
    key: str,
    options: Optional[ClientOptions] = None,
) -> HTTP1SupabaseClient:
    """Drop-in for ``supabase.create_client`` with the HTTP/1.1 transport.

    Same validation, auth headers and lazily built sub-clients; only the
    PostgREST transport differs.
    """
    if options is None:
        options = ClientOptions(
            postgrest_client_timeout=DEFAULT_POSTGREST_CLIENT_TIMEOUT
        )
    client = HTTP1SupabaseClient.create(url, key, options)
    logger.info("[SAVE_DB] Supabase transport=http/1.1 (HTTP/2 disabled).")
    return client


def install_http1_transport() -> bool:
    """Point the public ``supabase.create_client`` factory at this module.

    ``backend.db.client.get_db()`` is the single construction call site and it
    imports ``create_client`` from the ``supabase`` package at call time, so
    installing the factory here keeps one construction boundary instead of
    duplicating it. Idempotent; returns whether this call installed it.
    """
    if getattr(supabase, "create_client", None) is create_http1_client:
        return False
    supabase.create_client = create_http1_client
    return True
