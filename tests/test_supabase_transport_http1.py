"""The shared Supabase client is built on an HTTP/1.1-only transport.

Production failed on the read path with ``ReadError <- ReadError <-
BlockingIOError(errno=11)`` in ``elapsed=0.0s``. ``postgrest`` hard-codes
``http2=True``, so every concurrent call of the one shared client shared a
single multiplexed TLS socket that ``httpcore`` reads and writes from different
worker threads under two independent locks. HTTP/1.1 gives each in-flight
request its own pooled connection and measured clean under the same load.

These tests pin the *construction path* (not a mock of it): the real
``supabase`` client, the real ``postgrest`` client, the real ``httpcore`` pool,
and a real local HTTP server for the request and concurrency cases. No live
Supabase, no credentials, no Render access.
"""
from __future__ import annotations

import logging
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
import supabase
from postgrest import SyncPostgrestClient
from supabase import ClientOptions

from backend.ai.database.task_repository import (
    FALLBACK_REASON_UNAVAILABLE,
    InMemoryTaskRepository,
    SupabaseTaskRepository,
)
from backend.db import client as dbc
from backend.db import transport

_KEY = "aaa.bbb.ccc"  # JWT-shaped: supabase-py validates the format only


def _options(timeout: float = dbc._DB_HTTP_TIMEOUT) -> ClientOptions:
    return ClientOptions(postgrest_client_timeout=timeout)


# ── The transport pin ────────────────────────────────────────────


def test_the_shared_client_is_built_with_an_http1_only_postgrest_transport():
    """The one shared client must never be able to negotiate HTTP/2.

    ``httpcore`` builds its ALPN list as ``["http/1.1", "h2"] if http2 else
    ["http/1.1"]`` and only uses ``HTTP2Connection`` when h2 was negotiated, so
    the pool's ``_http2`` flag is the switch that removes the shared socket.
    """
    client = transport.create_http1_client(
        "https://example.supabase.co", _KEY, _options()
    )
    session = client.postgrest.session

    assert isinstance(session, transport.HTTP1Session)
    pool = session._transport._pool
    assert pool._http2 is False, "HTTP/2 is still enabled on the shared transport"
    assert pool._http1 is True

    # Nothing else about the session changed: same deadline, redirects, base
    # URL and PostgREST headers (Authorization / apiKey / Accept-Profile).
    assert session.timeout == httpx.Timeout(dbc._DB_HTTP_TIMEOUT)
    assert session.follow_redirects is True
    assert str(session.base_url) == "https://example.supabase.co/rest/v1/"
    assert session.headers["Authorization"] == f"Bearer {_KEY}"
    assert session.headers["apiKey"] == _KEY
    assert session.headers["Accept-Profile"] == "public"


def test_get_db_builds_the_shared_client_on_the_http1_transport(monkeypatch):
    """The production entry point itself, with its own call unchanged."""
    assert supabase.create_client is transport.create_http1_client

    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", _KEY)
    monkeypatch.setattr(dbc, "_initialised", False)
    monkeypatch.setattr(dbc, "_client", None)
    monkeypatch.setattr(dbc, "_available", False)
    try:
        client = dbc.get_db()
        assert client is not None
        assert dbc.is_available() is True
        session = client.postgrest.session
        assert isinstance(session, transport.HTTP1Session)
        assert session._transport._pool._http2 is False
        assert session.timeout == httpx.Timeout(dbc._DB_HTTP_TIMEOUT)
    finally:
        built = dbc._client
        if built is not None:
            built.postgrest.aclose()
        monkeypatch.setattr(dbc, "_client", None)
        monkeypatch.setattr(dbc, "_initialised", False)
        monkeypatch.setattr(dbc, "_available", False)


def test_the_stock_postgrest_transport_would_have_enabled_http2():
    """The assertion above is discriminating: the library default is h2 on.

    ``postgrest``'s own ``create_session`` passes ``http2=True``
    (``postgrest/_sync/client.py``), which is why this repository builds the
    session itself.
    """
    stock = SyncPostgrestClient(
        "https://example.supabase.co/rest/v1",
        headers={},
        schema="public",
        timeout=dbc._DB_HTTP_TIMEOUT,
    )
    try:
        assert stock.session._transport._pool._http2 is True
    finally:
        stock.aclose()


def test_the_transport_pin_survives_a_postgrest_client_rebuild():
    """supabase-py drops its PostgREST client on an auth-state change.

    The pin is applied by overriding the factory, so the rebuilt client is
    HTTP/1.1 again instead of silently reverting to the library default.
    """
    client = transport.create_http1_client(
        "https://example.supabase.co", _KEY, _options(8.0)
    )
    assert isinstance(client.postgrest.session, transport.HTTP1Session)

    client._postgrest = None  # what _listen_to_auth_events does
    rebuilt = client.postgrest.session

    assert isinstance(rebuilt, transport.HTTP1Session)
    assert rebuilt._transport._pool._http2 is False


# ── The request path, end to end over a real socket ────────────


def _ok_handler(seen: list[tuple[str, dict]]):
    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
            seen.append(
                (self.path, {k.lower(): v for k, v in self.headers.items()})
            )
            # PostgREST sends ``{}`` as the body of a select; leaving it unread
            # would make the next request on this keep-alive connection look
            # like ``{}GET ...``. That is a harness artefact, not a transport
            # result, so consume it exactly as PostgREST does.
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            body = b"[]"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):  # silence per-request logging
            pass

    return _Handler


def _serve(seen: list[tuple[str, dict]]):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ok_handler(seen))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _shutdown(server: ThreadingHTTPServer) -> None:
    server.shutdown()
    server.server_close()


def test_the_postgrest_request_path_works_through_the_http1_session():
    """Proves the real client's queries still travel and parse unchanged."""
    seen: list[tuple[str, dict]] = []
    server, url = _serve(seen)
    client = transport.create_http1_client(url, _KEY, _options())
    try:
        response = client.table("saved_items").select("*").limit(1).execute()
    finally:
        _shutdown(server)
        client.postgrest.aclose()

    assert response.data == []
    assert len(seen) == 1
    path, headers = seen[0]
    assert path.startswith("/rest/v1/saved_items?")
    assert "select=%2A" in path
    assert headers["apikey"] == _KEY
    assert headers["accept-profile"] == "public"
    # The wire protocol itself: HTTP/1.1, never an h2 upgrade.
    assert "upgrade" not in headers


def test_four_workers_share_the_http1_transport_without_transport_failures():
    """The production shape: one shared client, ``_DB_MAX_WORKERS`` threads.

    HTTP/2 multiplexed those four threads onto one socket; HTTP/1.1 checks a
    connection out to one request at a time, so nothing is read and written
    concurrently. Failures here would be transport failures, not assertions.
    """
    seen: list[tuple[str, dict]] = []
    server, url = _serve(seen)
    client = transport.create_http1_client(url, _KEY, _options())
    per_thread = 25
    outcomes: list[bool] = []
    errors: list[BaseException] = []

    def _worker():
        try:
            for _ in range(per_thread):
                outcomes.append(
                    client.table("ai_tasks").select("*").limit(1).execute().data
                    == []
                )
        except BaseException as exc:  # noqa: BLE001 - recorded, then asserted
            errors.append(exc)

    threads = [
        threading.Thread(target=_worker, daemon=True)
        for _ in range(dbc._DB_MAX_WORKERS)
    ]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
            assert not thread.is_alive()
    finally:
        _shutdown(server)
        client.postgrest.aclose()

    assert errors == []
    assert outcomes == [True] * (dbc._DB_MAX_WORKERS * per_thread)
    assert len(seen) == dbc._DB_MAX_WORKERS * per_thread


# ── Failures are still raised and still classified ────────────


class _Closer:
    """Accepts connections and closes them without responding."""

    def __init__(self) -> None:
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self.url = f"http://127.0.0.1:{self._sock.getsockname()[1]}"
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        self._sock.settimeout(0.1)
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                continue
            conn.close()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self._sock.close()


def test_a_real_transport_failure_still_raises_instead_of_returning_data():
    closer = _Closer()
    client = transport.create_http1_client(closer.url, _KEY, _options(2.0))
    try:
        with pytest.raises(httpx.TransportError):
            client.table("saved_items").select("*").limit(1).execute()
    finally:
        closer.close()
        client.postgrest.aclose()


@pytest.mark.asyncio
async def test_a_real_transport_failure_is_classified_and_never_a_false_success(
    caplog,
):
    """A real failure through the real transport degrades, it does not "work".

    A reset connection is a store-level failure, not a local-resource errno, so
    the existing classification must keep calling it ``unavailable`` — the
    transport change must not turn a genuine failure into a success or into a
    local-resource claim.
    """
    closer = _Closer()
    client = transport.create_http1_client(closer.url, _KEY, _options(2.0))
    repo = SupabaseTaskRepository(client, InMemoryTaskRepository())
    try:
        with caplog.at_level(
            logging.WARNING, logger="backend.ai.database.task_repository"
        ):
            assert await repo.list_tasks(5) == []
    finally:
        closer.close()
        client.postgrest.aclose()

    assert repo.fallback_reason == FALLBACK_REASON_UNAVAILABLE
    warnings = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING
        and r.name == "backend.ai.database.task_repository"
    ]
    assert warnings, caplog.text
    assert any("fallback" in line for line in warnings)


# ── Unchanged behaviour around the transport ────────────


def test_missing_credentials_still_use_the_in_memory_fallback(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    monkeypatch.setattr(dbc, "_initialised", False)
    monkeypatch.setattr(dbc, "_client", None)
    monkeypatch.setattr(dbc, "_available", False)

    assert dbc.get_db() is None
    assert dbc.is_available() is False


def test_the_worker_pool_and_the_timeout_relationship_are_unchanged():
    assert dbc._DB_MAX_WORKERS == 4
    assert 0 < dbc._DB_HTTP_TIMEOUT < dbc._DB_TIMEOUT
    assert dbc._get_db_executor()._max_workers == dbc._DB_MAX_WORKERS
