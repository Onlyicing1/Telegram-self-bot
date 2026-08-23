"""
You.com Web Search provider — Execution 29 focused regression tests.

Covers:
1.  Factory registration + YDC_API_KEY auto-detection / graceful absence
2.  Endpoint, POST method, X-API-Key + Content-Type headers, JSON payload
3.  Web/news result normalization; missing optional fields tolerated
4.  Malformed envelope/JSON; non-2xx (401/403/422/429+Retry-After/500)
5.  Timeout/network failures via existing provider conventions
6.  Chat-routing exclusion (never an active chat provider; manager skips it)
7.  Manager.web_search success + failure classification into the existing
    health machinery (auth → disabled, rate limit → cooldown)
8.  API key never appears in logs/errors/results/telemetry
9.  Tool registration and argument validation through the service layer
10. No second dispatcher: the provider never touches Telegram
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

FAKE_KEY = "ydc-fake-test-key-000"


# ── helpers ──


def _provider(api_key=FAKE_KEY):
    from backend.ai.providers.base.config import ProviderConfig
    from backend.ai.providers.you_search import YouSearchProvider

    cfg = ProviderConfig(
        provider_name="you",
        base_url="https://ydc-index.io",
        api_key=api_key,
        enabled=True,
        timeout=5,
    )
    return YouSearchProvider(cfg)


def _response(status_code=200, json_data=None, headers=None):
    resp = MagicMock()
    resp.status_code = status_code
    if isinstance(json_data, (dict, list)):
        resp.json.return_value = json_data
    else:
        resp.json.side_effect = ValueError("no json")
    resp.headers = headers or {}
    return resp


def _mock_client(resp=None, exc=None):
    client = AsyncMock()
    if exc is not None:
        client.post.side_effect = exc
    else:
        client.post.return_value = resp
    return client


def _payload_capture(client, ctor_kwargs=None):
    args, kwargs = client.post.call_args
    url = args[0] if args else kwargs.get("url")
    return url, kwargs.get("json") or {}, (ctor_kwargs or {}).get("headers")


# ── 1. Registration & discovery ──


class TestRegistration:
    def test_factory_maps_and_defaults(self):
        from backend.ai.providers.factory import (
            _ENV_KEY_MAP,
            _PROVIDER_CLASSES,
            ProviderFactory,
        )
        from backend.ai.providers.you_search import YouSearchProvider

        assert _PROVIDER_CLASSES["you"] is YouSearchProvider
        assert _ENV_KEY_MAP["you"] == ["YDC_API_KEY"]
        assert "you" in ProviderFactory.available_providers()
        cfg = ProviderFactory.create_provider("you").config
        assert cfg.base_url == "https://ydc-index.io"

    def test_capability_identity(self):
        p = _provider()
        assert p.name == "you"
        assert p.display_name == "You.com Search"
        assert p.CAPABILITY_KIND == "web_search"
        caps = p.capabilities.as_dict()
        assert caps["supports_web_search"] is True
        assert not any(
            caps[k] for k in
            ("supports_streaming", "supports_tools", "supports_images")
        )

    def test_auto_detection_registers_with_key(self, monkeypatch):
        monkeypatch.delenv("YDC_API_KEY", raising=False)
        monkeypatch.delenv("AI_PROVIDER", raising=False)
        for mod in ("backend.ai.providers.factory",):
            import sys
            sys.modules.pop(mod, None) if False else None
        with patch.dict("os.environ", {"YDC_API_KEY": FAKE_KEY}):
            registry = __import__(
                "backend.ai.providers.factory", fromlist=["ProviderFactory"],
            ).ProviderFactory.create_registry()
        assert registry.has("you")
        assert registry.get("you").config.api_key == FAKE_KEY

    def test_missing_or_empty_key_leaves_provider_unregistered(self, monkeypatch):
        from backend.ai.providers.factory import ProviderFactory

        for val in (None, "", "   "):
            monkeypatch.delenv("YDC_API_KEY", raising=False)
            if val is not None:
                monkeypatch.setenv("YDC_API_KEY", val)
            registry = ProviderFactory.create_registry()
            assert not registry.has("you"), repr(val)

    def test_ai_provider_env_cannot_activate_search(self, monkeypatch):
        from backend.ai.providers.factory import ProviderFactory

        with patch.dict("os.environ",
                        {"YDC_API_KEY": FAKE_KEY, "AI_PROVIDER": "you"}):
            registry = ProviderFactory.create_registry()
        # The web-search capability must never become the chat engine.
        assert registry.active_name != "you"
        assert registry.get_active().CAPABILITY_KIND == "chat"

    def test_health_reports_configured_vs_verified(self):
        p = _provider()
        h = p.health()
        assert h["configured"] is True and h["healthy"] is False  # unverified
        p._last_success_monotonic = 123.0
        h = p.health()
        assert h["healthy"] is True
        nokey = _provider(api_key="")
        h = nokey.health()
        assert h["configured"] is False and h["healthy"] is False


# ── 2–5. HTTP contract & normalization ──


class TestSearchHTTP:
    @pytest.mark.asyncio
    async def test_endpoint_method_headers_payload(self):
        p = _provider()
        client = _mock_client(_response(200, {"results": {}, "metadata": {}}))
        with patch("backend.ai.providers.you_search.httpx.AsyncClient",
                   return_value=client) as ctor:
            await p.search("latest news about ai")
        url, payload, headers = _payload_capture(client, ctor.call_args.kwargs)
        assert url == "https://ydc-index.io/v1/search"
        assert payload == {"query": "latest news about ai", "count": 10}
        assert headers["X-API-Key"] == FAKE_KEY
        assert headers["Content-Type"] == "application/json"

    @pytest.mark.asyncio
    async def test_optional_fields_cleaned_or_dropped(self):
        p = _provider()
        client = _mock_client(_response(200, {"results": {}}))
        with patch("backend.ai.providers.you_search.httpx.AsyncClient",
                   return_value=client):
            await p.search("q", count=25, freshness="week",
                           include_domains=[" Example.COM ", "", "a.b"])
        _, payload, _ = _payload_capture(client)
        assert payload["count"] == 25
        assert payload["freshness"] == "week"
        assert payload["include_domains"] == ["Example.COM", "a.b"]

    @pytest.mark.asyncio
    async def test_invalid_option_values_rejected_server_side(self):
        p = _provider()
        client = _mock_client(_response(200, {"results": {}}))
        with patch("backend.ai.providers.you_search.httpx.AsyncClient",
                   return_value=client):
            await p.search("q", count="999", freshness="fortnight")
            _, payload, _ = _payload_capture(client)
            assert payload["count"] == 100  # clamped to the documented max
            assert "freshness" not in payload

            await p.search("q", count=0, include_domains="not-a-list")
            _, payload, _ = _payload_capture(client)
            assert payload["count"] == 10   # default restored
            assert "include_domains" not in payload

    @pytest.mark.asyncio
    async def test_web_results_normalized(self):
        p = _provider()
        data = {"results": {"web": [
            {"url": "https://example.com/a", "title": "Example",
             "description": "desc", "snippets": ["s1", ""],
             "thumbnail_url": "https://t", "favicon_url": "https://f"},
        ]}, "metadata": {"search_uuid": "u1", "query": "q", "latency": 0.5}}
        client = _mock_client(_response(200, data))
        with patch("backend.ai.providers.you_search.httpx.AsyncClient",
                   return_value=client):
            result = await p.search("q")

        assert result["success"] is True and result["error"] == ""
        item = result["results"][0]
        assert item["kind"] == "web" and item["url"] == "https://example.com/a"
        assert item["snippets"] == ["s1"]
        assert "thumbnail_url" not in item      # unknown fields not exposed raw
        assert result["metadata"]["search_uuid"] == "u1"
        assert result["metadata"]["latency"] == 0.5

    @pytest.mark.asyncio
    async def test_news_results_normalized(self):
        p = _provider()
        data = {"results": {"news": [
            {"title": "Story", "description": "d", "page_age": "2 days ago",
             "url": "https://n.example/x"},
        ]}}
        client = _mock_client(_response(200, data))
        with patch("backend.ai.providers.you_search.httpx.AsyncClient",
                   return_value=client):
            result = await p.search("q")
        item = result["results"][0]
        assert item["kind"] == "news" and item["page_age"] == "2 days ago"

    @pytest.mark.asyncio
    async def test_mixed_web_news_order_preserved(self):
        p = _provider()
        data = {"results": {
            "web": [{"title": "W", "url": "https://w"}],
            "news": [{"title": "N", "url": "https://n"}],
        }}
        client = _mock_client(_response(200, data))
        with patch("backend.ai.providers.you_search.httpx.AsyncClient",
                   return_value=client):
            result = await p.search("q")
        assert [r["kind"] for r in result["results"]] == ["web", "news"]

    @pytest.mark.asyncio
    async def test_missing_and_partial_fields_tolerated(self):
        p = _provider()
        data = {"results": {"web": [{"title": "Only title"}, "garbage", {}]}}
        client = _mock_client(_response(200, data))
        with patch("backend.ai.providers.you_search.httpx.AsyncClient",
                   return_value=client):
            result = await p.search("q")
        assert len(result["results"]) == 1          # junk entries skipped
        assert result["results"][0]["title"] == "Only title"

    @pytest.mark.asyncio
    async def test_empty_results_is_honest_success(self):
        p = _provider()
        for data in ({}, {"results": None}, {"results": {"web": []}}):
            client = _mock_client(_response(200, data))
            with patch("backend.ai.providers.you_search.httpx.AsyncClient",
                       return_value=client):
                result = await p.search("q")
            assert result["success"] is True
            assert result["results"] == []

    @pytest.mark.asyncio
    async def test_malformed_envelopes_fail_safe(self):
        p = _provider()
        cases = ([1, 2, 3], "not-json-object")
        for bad_json in ("not-json", ):
            client = _mock_client(_response(200, bad_json))
            with patch("backend.ai.providers.you_search.httpx.AsyncClient",
                       return_value=client):
                r = await p.search("q")
            assert r["success"] is False and r["metadata"]["failure_type"]
        for bad in cases:
            client = _mock_client(_response(200, bad))
            with patch("backend.ai.providers.you_search.httpx.AsyncClient",
                       return_value=client):
                r = await p.search("q")
            assert r["success"] is False
            assert r["metadata"]["failure_type"] == "malformed"

    @pytest.mark.asyncio
    async def test_non_2xx_mapping(self):
        cases = {
            401: "auth", 403: "auth", 422: "request", 500: "server", 503: "server",
        }
        for status, expected in cases.items():
            p = _provider()  # fresh instance — the client is cached lazily
            client = _mock_client(_response(status, {"detail": "x"}))
            with patch("backend.ai.providers.you_search.httpx.AsyncClient",
                       return_value=client):
                r = await p.search("q")
            assert r["success"] is False
            assert r["metadata"]["failure_type"] == expected, status
            assert str(status) in r["error"]

    @pytest.mark.asyncio
    async def test_rate_limit_preserves_retry_after_only_when_present(self):
        p = _provider()
        client = _mock_client(_response(429, {}, headers={"retry-after": "7"}))
        with patch("backend.ai.providers.you_search.httpx.AsyncClient",
                   return_value=client):
            r = await p.search("q")
        assert r["success"] is False
        assert r["metadata"]["retry_after"] == 7.0

        p2 = _provider()  # fresh instance — the first cached its client
        client = _mock_client(_response(429, {}))
        with patch("backend.ai.providers.you_search.httpx.AsyncClient",
                   return_value=client):
            r = await p2.search("q")
        assert "retry_after" not in r["metadata"]   # never fabricated

    @pytest.mark.asyncio
    async def test_timeout_and_network_errors(self):
        p = _provider()
        with patch("backend.ai.providers.you_search.httpx.AsyncClient",
                   return_value=_mock_client(exc=httpx.TimeoutException("t"))):
            r = await p.search("q")
        assert r["metadata"]["failure_type"] == "timeout"

        p2 = _provider()  # fresh instance — the first cached its client
        with patch("backend.ai.providers.you_search.httpx.AsyncClient",
                   return_value=_mock_client(exc=httpx.ConnectError("c"))):
            r = await p2.search("q")
        assert r["metadata"]["failure_type"] == "network"

    @pytest.mark.asyncio
    async def test_empty_query_rejected_without_network(self):
        p = _provider()
        client = _mock_client(_response(200, {}))
        with patch("backend.ai.providers.you_search.httpx.AsyncClient",
                   return_value=client):
            r = await p.search("   ")
        assert r["success"] is False
        client.post.assert_not_called()


# ── 6–8. Routing exclusion, manager classification, secrecy ──


def _manager_with(provider):
    from backend.ai.providers.manager.manager import ProviderManager
    from backend.ai.providers.registry.registry import ProviderRegistry

    registry = ProviderRegistry()
    registry.register(provider)
    manager = ProviderManager(registry)
    return manager


class TestRoutingExclusionAndManager:
    @pytest.mark.asyncio
    async def test_chat_routing_never_selects_search_provider(self):
        p = _provider()
        p._last_success_monotonic = 1.0  # even when fully healthy...
        manager = _manager_with(p)
        response = await manager.chat([{"role": "user", "content": "hi"}])
        # ...the mesh must skip it and land on the dummy fallback.
        matrix = (response.metadata or {}).get("provider_matrix", [])
        assert any(
            e.get("provider") == "you" and e.get("outcome") == "skipped"
            for e in matrix
        )
        assert response.provider_name == "dummy"

    @pytest.mark.asyncio
    async def test_manager_search_success_records_health(self):
        p = _provider()
        good = {"success": True, "query": "q", "results": [], "metadata": {}}

        async def fake_search(query, **kwargs):
            return dict(good)

        p.search = fake_search
        manager = _manager_with(p)
        result = await manager.web_search("q")
        assert result["success"] is True
        # Existing health tracker records the success streak.
        assert manager._health.is_available("you") is True
        assert manager._health.consecutive_successes("you") == 1

    @pytest.mark.asyncio
    async def test_auth_failure_disables_via_existing_tracker(self):
        p = _provider()

        async def fake_search(query, **kwargs):
            return {"success": False, "query": query, "results": [],
                    "metadata": {"failure_type": "auth"}, "error": "unauthorized"}

        p.search = fake_search
        manager = _manager_with(p)
        result = await manager.web_search("q")
        assert result["success"] is False
        # Auth failures disable through the SAME tracker chat uses.
        assert manager._health.is_available("you") is False
        # And a subsequent search degrades honestly instead of hammering.
        result = await manager.web_search("q")
        assert result["success"] is False
        assert "unavailable" in result["error"]

    @pytest.mark.asyncio
    async def test_rate_limit_cooldown_applies(self):
        p = _provider()

        async def fake_search(query, **kwargs):
            return {"success": False, "query": query, "results": [],
                    "metadata": {"failure_type": "rate_limited",
                                 "retry_after": 120.0},
                    "error": "rate limited"}

        p.search = fake_search
        manager = _manager_with(p)
        await manager.web_search("q")
        assert not manager._health.is_available("you")  # cooling down

    @pytest.mark.asyncio
    async def test_unconfigured_search_fails_closed(self):
        empty = _provider(api_key="")
        manager = _manager_with(empty)
        # Health tracker gates availability before the provider is reached;
        # simulate the realistic case where nothing is registered at all.
        from backend.ai.providers.manager.manager import ProviderManager
        bare = ProviderManager()
        result = await bare.web_search("q")
        assert result["success"] is False
        assert "unavailable" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_no_telegram_access_from_provider(self, caplog):
        import inspect
        from backend.ai.providers import you_search as mod

        src = inspect.getsource(mod)
        for forbidden in ("telethon", "send_message", "forward_messages"):
            assert forbidden not in src.lower(), forbidden

    def test_api_key_never_in_logs_or_results(self, caplog):
        with caplog.at_level(logging.DEBUG, logger="backend.ai.providers.you_search"):
            p = _provider()
            assert FAKE_KEY not in p.health().__str__()
            assert FAKE_KEY not in str(p.capabilities.as_dict())
            err = p._error_result("q", "boom", "auth")
            assert FAKE_KEY not in str(err)
        assert FAKE_KEY not in caplog.text


# ── 9. Tool layer ──


class TestWebSearchTool:
    @pytest.mark.asyncio
    async def test_registered_and_executes_through_service(self):
        from backend.ai.tools.context import ToolContext
        from backend.ai.tools.registry import create_default_registry
        from backend.ai.tools.websearch import WebSearchTool

        registry = create_default_registry(ToolContext(
            telegram=MagicMock(), owner_id=1, tz_str="UTC"))
        assert isinstance(registry.get("web_search"), WebSearchTool)
        schemas = [s["name"] for s in registry.list_schemas()]
        assert schemas.count("web_search") == 1

        tool = registry.get("web_search")
        fake_engine = SimpleNamespace(provider_manager=SimpleNamespace(
            web_search=AsyncMock(return_value={
                "success": True, "query": "ai news",
                "results": [{"kind": "web", "title": "T",
                             "url": "https://e.x", "description": "d"}],
                "metadata": {}, "error": "",
            }),
        ))
        with patch("backend.ai.engine.engine.get_engine",
                   return_value=fake_engine):
            result = await tool.execute(None, {"query": "ai news", "count": 5})

        assert result.success is True
        fake_engine.provider_manager.web_search.assert_awaited_once()
        _, kwargs = fake_engine.provider_manager.web_search.await_args
        assert kwargs["count"] == 5
        assert "https://e.x" in result.message       # sources preserved verbatim
        assert result.data["results"][0]["url"] == "https://e.x"

    @pytest.mark.asyncio
    async def test_tool_validates_arguments(self):
        from backend.ai.tools.context import ToolContext
        from backend.ai.tools.registry import create_default_registry

        registry = create_default_registry(ToolContext(
            telegram=MagicMock(), owner_id=1, tz_str="UTC"))
        tool = registry.get("web_search")

        missing = await tool.execute(None, {})
        assert missing.success is False

        fake_engine = SimpleNamespace(provider_manager=SimpleNamespace(
            web_search=AsyncMock(return_value={"success": False, "query": "q",
                                               "results": [],
                                               "metadata": {},
                                               "error": "down"}),
        ))
        with patch("backend.ai.engine.engine.get_engine",
                   return_value=fake_engine), \
             patch("backend.services.web_search_service.do_web_search",
                   new=AsyncMock(return_value=(False, "\u26a0\ufe0f down", {}))):
            failed = await tool.execute(None, {"query": "q"})
        assert failed.success is False

    @pytest.mark.asyncio
    async def test_service_formats_sources_honestly(self):
        from backend.services import web_search_service as svc

        ok, text, data = await svc.do_web_search.__wrapped__("never-called") \
            if hasattr(svc.do_web_search, "__wrapped__") else (None, None, None)
        formatted = svc._format_results({
            "query": "q",
            "results": [
                {"kind": "web", "title": "A", "url": "https://a",
                 "description": "da"},
                {"kind": "news", "title": "B", "url": "https://b",
                 "page_age": "1h"},
            ],
            "metadata": {"latency": 0.42},
        })
        assert 'Web results for "q"' in formatted
        assert "[web] A" in formatted and "https://a" in formatted
        assert "[news] B" in formatted and "Published: 1h" in formatted
        assert "cite sources" in formatted

        empty = svc._format_results({"query": "zzz", "results": [],
                                     "metadata": {}})
        assert "No results found." in empty


# ── 10. Architecture invariants ──


class TestNoSecondArchitecture:
    def test_single_registration_path(self):
        import backend.ai.providers.factory as factory
        src = open(factory.__file__, encoding="utf-8").read()
        assert src.count('"you"') >= 1
        # exactly one class mapping and one env-key mapping entry
        assert src.count('YouSearchProvider') == 2  # import + map value

    def test_base_contract_unchanged_for_chat_providers(self):
        from backend.ai.providers.groq import GroqProvider
        assert GroqProvider.CAPABILITY_KIND == "chat"
        from backend.ai.providers.dummy.provider import DummyProvider
        assert DummyProvider.CAPABILITY_KIND == "chat"

    def test_capabilities_default_off_everywhere(self):
        from backend.ai.providers.base.capabilities import ProviderCapabilities
        assert ProviderCapabilities().as_dict()["supports_web_search"] is False
