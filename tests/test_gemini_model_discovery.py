"""Gemini: discovery → test targets → results → Test Modules rendering.

The reported symptom was ``gemini-2.0-flash — INVALID_MODEL`` under "Not
usable", with none of Gemini's real catalog visible. These tests pin every
link of that chain:

  * ONE default model (the runtime's), never a retired discovery id;
  * a key-free, correctly encoded ``/models`` contract (header auth, page
    tokens as parameters, bounded pagination, `generateContent` only, deduped);
  * a phantom provider default never becomes a test target, while the owner's
    explicit selection and the live catalog are always tested;
  * the curated fallback catalog when the API fails or returns nothing usable;
  * a per-model 404 is a MODEL failure, never a disabled provider;
  * the Test Modules payload/renderer show Gemini's discovered models with
    their own statuses and make the usable ones selectable;
  * Cohere and Groq keep their previous target/fallback behaviour.
"""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.ai import discovery as discovery_mod
from backend.ai import model_tester
from backend.ai.discovery import ProviderStatus
from backend.ai.model_discovery import (
    _FALLBACK_CATALOG,
    ModelInfo,
    clear_cache,
    fallback_models,
    fetch_models,
    get_last_fetch_source,
)
from backend.ai.providers.base.contract import ProviderResponse
from backend.ai.providers.base.defaults import get_provider_default

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_ENTRY = next(p for p in discovery_mod._PROVIDERS if p["name"] == "gemini")


def _resp(status_code: int, payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload
    return resp


def _client(*responses) -> AsyncMock:
    """An httpx.AsyncClient stand-in returning ``responses`` in order."""
    client = AsyncMock()
    if len(responses) == 1:
        only = responses[0]

        async def _get(*args, **kwargs):
            return only

        client.get = AsyncMock(side_effect=_get)
    else:
        client.get = AsyncMock(side_effect=list(responses))
    client.__aenter__.return_value = client
    client.__aexit__.return_value = False
    return client


def _generate(name: str, **extra) -> dict:
    return {
        "name": f"models/{name}",
        "supportedGenerationMethods": ["generateContent"],
        **extra,
    }


def _status(name: str, default_model: str, base_url: str = GEMINI_BASE, **kwargs) -> ProviderStatus:
    return ProviderStatus(
        name=name,
        display_name=name.title(),
        env_var=f"AI_{name.upper()}_API_KEY",
        status="available",
        has_key=True,
        validated=True,
        default_model=default_model,
        base_url=base_url,
        icon="◇",
        **kwargs,
    )


# ── 1. one default model ─────────────────────────────────────────────────────


def test_discovery_and_runtime_advertise_the_same_default_model():
    assert GEMINI_ENTRY["default_model"] == get_provider_default("gemini").default_model
    assert GEMINI_ENTRY["default_model"] == "gemini-2.5-flash"
    # The default is used ONLY when nothing is selected: there is no per-model
    # rewrite table anywhere in the selection or execution path.
    from backend.ai.providers.base import defaults as provider_defaults

    assert not hasattr(provider_defaults, "resolve_model")
    assert not hasattr(provider_defaults, "DEPRECATED_MODELS")


# ── 2. the /models contract ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gemini_probe_uses_header_auth_and_a_key_free_trace(monkeypatch, caplog):
    monkeypatch.setenv("GEMINI_API_KEY", "probe-key-123")
    monkeypatch.delenv("AI_GEMINI_API_KEY", raising=False)
    status = discovery_mod._scan_provider(GEMINI_ENTRY)
    client = _client(_resp(200, {"models": []}))

    with patch("backend.ai.discovery.httpx.AsyncClient", return_value=client):
        with caplog.at_level(logging.INFO, logger="backend.ai.discovery"):
            result = await discovery_mod._validate_provider(status)

    assert (result.status, result.validated) == ("available", True)
    call = client.get.call_args
    assert call.args[0] == f"{GEMINI_BASE}/models"
    assert call.kwargs["params"] == {"pageSize": 1}
    assert call.kwargs["headers"] == {"x-goog-api-key": "probe-key-123"}

    trace = "\n".join(record.getMessage() for record in caplog.records)
    assert "PROVIDER_DISCOVERY_PROBE" in trace
    assert "probe-key-123" not in trace


@pytest.mark.asyncio
async def test_gemini_pagination_round_trips_reserved_tokens_and_dedupes():
    clear_cache()
    pages = [
        {
            "models": [
                _generate("gemini-2.5-flash", inputTokenLimit=1048576),
                {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]},
            ],
            "nextPageToken": "tok+2/=",
        },
        {
            "models": [
                _generate("gemini-2.5-pro"),
                {"name": "models/imagen-3.0-generate-002", "supportedGenerationMethods": ["predict"]},
            ],
            "nextPageToken": "tok+3==",
        },
        {
            "models": [
                _generate("gemini-2.5-flash"),
                _generate("gemini-2.5-flash"),
            ],
        },
    ]
    client = _client(*[_resp(200, page) for page in pages])

    with patch("backend.ai.model_discovery.httpx.AsyncClient", return_value=client):
        models = await fetch_models("gemini", "fake-key", GEMINI_BASE)

    assert [m.id for m in models] == ["gemini-2.5-flash", "gemini-2.5-pro"]
    assert get_last_fetch_source("gemini") == "api"
    assert models[0].context_length == 1048576

    calls = client.get.call_args_list
    assert len(calls) == 3
    assert [c.kwargs["params"].get("pageToken") for c in calls] == [None, "tok+2/=", "tok+3=="]
    assert all(c.kwargs["params"]["pageSize"] == 100 for c in calls)
    assert all(c.args[0] == f"{GEMINI_BASE}/models" for c in calls)
    assert all(c.kwargs["headers"] == {"x-goog-api-key": "fake-key"} for c in calls)


@pytest.mark.asyncio
async def test_gemini_pagination_is_bounded():
    clear_cache()
    page = {"models": [_generate("gemini-2.5-flash")], "nextPageToken": "never-ends"}
    client = _client(_resp(200, page))

    with patch("backend.ai.model_discovery.httpx.AsyncClient", return_value=client):
        await fetch_models("gemini", "fake-key", GEMINI_BASE)

    assert client.get.call_count == 10  # _MAX_DISCOVERY_PAGES


@pytest.mark.asyncio
async def test_gemini_api_failure_falls_back_to_the_curated_catalog():
    clear_cache()
    client = _client(_resp(429, {"error": "quota"}))

    with patch("backend.ai.model_discovery.httpx.AsyncClient", return_value=client):
        models = await fetch_models("gemini", "fake-key", GEMINI_BASE)

    assert [m.id for m in models] == _FALLBACK_CATALOG["gemini"]
    assert get_last_fetch_source("gemini") == "fallback"
    assert "gemini-2.0-flash" not in [m.id for m in models]


@pytest.mark.asyncio
async def test_gemini_response_with_no_generate_content_model_falls_back():
    clear_cache()
    client = _client(_resp(200, {"models": [
        {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]},
    ]}))

    with patch("backend.ai.model_discovery.httpx.AsyncClient", return_value=client):
        models = await fetch_models("gemini", "fake-key", GEMINI_BASE)

    assert [m.id for m in models] == _FALLBACK_CATALOG["gemini"]
    assert get_last_fetch_source("gemini") == "fallback"


@pytest.mark.asyncio
async def test_cache_refreshes_when_the_key_changes():
    clear_cache()
    first = _client(_resp(200, {"models": [_generate("gemini-2.5-flash")]}))
    second = _client(_resp(200, {"models": [_generate("gemini-2.5-pro")]}))

    with patch("backend.ai.model_discovery.httpx.AsyncClient", return_value=first):
        a1 = await fetch_models("gemini", "key-a", GEMINI_BASE)
        a2 = await fetch_models("gemini", "key-a", GEMINI_BASE)  # cache hit
    with patch("backend.ai.model_discovery.httpx.AsyncClient", return_value=second):
        b1 = await fetch_models("gemini", "key-b", GEMINI_BASE)  # new credential

    assert [m.id for m in a1] == ["gemini-2.5-flash"]
    assert [m.id for m in a2] == ["gemini-2.5-flash"]
    assert [m.id for m in b1] == ["gemini-2.5-pro"]
    assert first.get.call_count == 1
    assert second.get.call_count == 1


# ── 3. targets ──────────────────────────────────────────────────────────────

async def _targets(status, active_config=None, models=(), source="api"):
    async def fake_fetch(provider_name, api_key, base_url, force_refresh=False):
        return list(models)

    with patch("backend.ai.model_tester._get_env", return_value="fake-key"), \
         patch("backend.ai.model_tester.fetch_models", side_effect=fake_fetch), \
         patch("backend.ai.model_tester.get_last_fetch_source", return_value=source):
        return await model_tester._build_targets([status], active_config or {})


@pytest.mark.asyncio
async def test_a_default_the_live_catalog_lacks_is_not_tested():
    status = _status("gemini", "gemini-2.5-flash-preview-09-2025")
    targets, discovered, _complete = await _targets(
        status,
        models=[
            ModelInfo(id="gemini-2.5-flash", name="gemini-2.5-flash", provider="gemini"),
            ModelInfo(id="gemini-2.5-pro", name="gemini-2.5-pro", provider="gemini"),
        ],
    )

    models = [t["model"] for t in targets]
    assert "gemini-2.5-flash-preview-09-2025" not in models
    assert models == ["gemini-2.5-flash", "gemini-2.5-pro"]
    assert {m["id"] for m in discovered} == {"gemini-2.5-flash", "gemini-2.5-pro"}


@pytest.mark.asyncio
async def test_a_retired_provider_default_never_becomes_a_target():
    status = _status("gemini", "gemini-2.0-flash")
    targets, _discovered, complete = await _targets(
        status,
        models=[
            ModelInfo(id="gemini-2.5-flash", name="gemini-2.5-flash", provider="gemini"),
            ModelInfo(id="gemini-2.5-pro", name="gemini-2.5-pro", provider="gemini"),
        ],
    )

    models = [t["model"] for t in targets]
    assert "gemini-2.0-flash" not in models
    assert models.count("gemini-2.5-flash") == 1
    assert models == ["gemini-2.5-flash", "gemini-2.5-pro"]
    assert [m.id for m in complete["gemini"]] == ["gemini-2.5-flash", "gemini-2.5-pro"]


@pytest.mark.asyncio
async def test_an_explicit_selection_never_changes_the_target_set():
    """The diagnostic is independent of the owner's selection: a configured
    model that is not in the catalog is not injected, and switching the
    selection changes neither the target set nor its order."""
    status = _status("gemini", "gemini-2.5-flash")
    catalog = [ModelInfo(id="gemini-2.5-flash", name="gemini-2.5-flash", provider="gemini")]

    with_selection, _d1, _c1 = await _targets(
        status,
        active_config={"provider": "gemini", "model": "gemini-3.9-retired"},
        models=catalog,
    )
    without_selection, _d2, _c2 = await _targets(status, models=catalog)

    models = [t["model"] for t in with_selection]
    assert "gemini-3.9-retired" not in models
    assert "gemini-2.5-flash" in models
    assert models == [t["model"] for t in without_selection]


@pytest.mark.asyncio
async def test_fallback_source_keeps_the_provider_default_verbatim():
    """Without an API catalog the curated list IS the catalog, so the
    provider's own default stays a target — used exactly as written."""
    fallback = [ModelInfo(id=mid, name=mid, provider="gemini") for mid in _FALLBACK_CATALOG["gemini"]]
    targets, _discovered, _complete = await _targets(
        _status("gemini", "gemini-2.5-flash"), models=fallback, source="fallback"
    )
    models = [t["model"] for t in targets]
    assert models == ["gemini-2.5-flash", "gemini-2.5-pro"]


@pytest.mark.asyncio
async def test_discovery_exception_uses_the_same_fallback_catalog():
    async def exploding_fetch(*args, **kwargs):
        raise RuntimeError("discovery blew up")

    with patch("backend.ai.model_tester._get_env", return_value="fake-key"), \
         patch("backend.ai.model_tester.fetch_models", side_effect=exploding_fetch):
        targets, _discovered, complete = await model_tester._build_targets(
            [_status("gemini", "gemini-2.5-flash")], {}
        )

    models = [t["model"] for t in targets]
    assert models == ["gemini-2.5-flash", "gemini-2.5-pro"]
    assert [m.id for m in complete["gemini"]] == ["gemini-2.5-flash", "gemini-2.5-pro"]


# ── 4. a per-model 404 is never a provider failure ─────────────────────────


@pytest.mark.asyncio
async def test_a_404_is_reported_against_the_exact_id_that_was_requested():
    not_found = ProviderResponse(
        text="models/gemini-2.5-flash is not found for API version v1beta",
        provider_name="gemini",
        success=False,
        metadata={"http_status": 404, "failure_type": "model_not_found"},
    )
    with patch("backend.ai.model_tester._get_env", return_value="fake-key"), \
         patch(
             "backend.ai.providers.gemini.GeminiProvider.chat",
             new_callable=AsyncMock,
             return_value=not_found,
         ) as chat:
        result = await model_tester.test_single_model(
            "gemini", "Google Gemini", "◇", "gemini-2.0-flash"
        )

    assert result["status"] == "INVALID_MODEL"
    assert result["http_status"] == 404
    # No substitution: the requested id is the reported id, so the owner sees
    # the truth about THEIR selection and the provider stays enabled.
    assert result["model"] == "gemini-2.0-flash"
    assert chat.await_args.kwargs["model"] == "gemini-2.0-flash"


def _pm_engine():
    class _FakePM:
        def __init__(self):
            self.candidates: dict[str, list[str]] = {}

        def set_model_candidates(self, name, models):
            self.candidates[name] = models

    class _FakeEngine:
        provider_manager = _FakePM()

    return _FakeEngine.provider_manager, _FakeEngine


@pytest.mark.asyncio
async def test_one_failed_model_never_hides_the_others_in_a_run():
    status = _status("gemini", "gemini-2.5-flash")
    discovered = [
        ModelInfo(id="gemini-2.5-flash", name="gemini-2.5-flash", provider="gemini"),
        ModelInfo(id="gemini-2.5-flash-lite", name="gemini-2.5-flash-lite", provider="gemini"),
        ModelInfo(id="gemini-2.5-pro", name="gemini-2.5-pro", provider="gemini"),
    ]

    async def fake_fetch(provider_name, api_key, base_url, force_refresh=False):
        return discovered

    async def fake_test(provider, display, icon, model, timeout=8.0):
        failed = model == "gemini-2.5-pro"
        return {
            "provider": provider, "display_name": display, "icon": icon, "model": model,
            "status": "INVALID_MODEL" if failed else "AVAILABLE",
            "error": "not found" if failed else None,
            "latency_s": 0.2, "http_status": 404 if failed else 200,
            "retry_after": None, "error_type": None, "provider_code": None,
            "finish_reason": None, "capabilities": [],
        }

    pm, engine = _pm_engine()
    with patch("backend.ai.model_tester.discover_providers", AsyncMock(return_value=[status])), \
         patch("backend.ai.model_tester._get_env", return_value="fake-key"), \
         patch("backend.ai.model_tester.fetch_models", side_effect=fake_fetch), \
         patch("backend.ai.model_tester.test_single_model", side_effect=fake_test), \
         patch("backend.ai.engine.engine.get_engine", return_value=engine):
        payload = await model_tester.test_all_models_streaming(owner_id=0, global_budget=0)

    by_model = {r["model"]: r["status"] for r in payload["results"]}
    assert by_model == {
        "gemini-2.5-flash": "AVAILABLE",
        "gemini-2.5-flash-lite": "AVAILABLE",
        "gemini-2.5-pro": "INVALID_MODEL",
    }
    assert payload["summary"]["available"] == 2
    assert payload["summary"]["invalid"] == 1
    # Only the genuinely AVAILABLE, non-default model feeds the runtime pool.
    assert pm.candidates == {"gemini": ["gemini-2.5-flash-lite"]}


@pytest.mark.asyncio
async def test_gemini_without_a_key_is_not_configured_and_makes_no_request():
    with patch("backend.ai.model_tester._get_env", return_value=""), \
         patch(
             "backend.ai.model_tester.ProviderFactory.create_provider",
             side_effect=AssertionError("must not create a provider without a key"),
         ):
        result = await model_tester.test_single_model(
            "gemini", "Google Gemini", "◇", "gemini-2.5-flash"
        )

    assert result["status"] == "NOT_CONFIGURED"
    assert result["latency_s"] is None


# ── 5. Test Modules payload + rendering ────────────────────────────────────


def _result(model: str, status: str, latency: float | None = 0.2) -> dict:
    return {
        "provider": "gemini", "display_name": "Google Gemini", "icon": "◇",
        "model": model, "status": status,
        "error": None if status == "AVAILABLE" else "not found",
        "latency_s": latency, "http_status": 200 if status == "AVAILABLE" else 404,
        "retry_after": None, "error_type": None, "provider_code": None,
        "finish_reason": None, "capabilities": [],
    }


def test_test_modules_renders_gemini_models_and_their_statuses():
    from backend.bot.handlers.ai import _render_test_results

    payload = {
        "results": [
            _result("gemini-2.5-flash", "AVAILABLE", 0.4),
            _result("gemini-2.5-flash-lite", "AVAILABLE", 0.2),
            _result("gemini-2.5-pro", "INVALID_MODEL", None),
        ],
        "summary": {"available": 2, "failed": 1, "invalid": 1, "total": 3},
    }

    body, buttons = _render_test_results(payload, current=("gemini", "gemini-2.5-flash"))
    datas = [
        btn.data.decode() if isinstance(getattr(btn, "data", None), bytes) else str(getattr(btn, "data", ""))
        for row in buttons for btn in row
    ]

    assert "Usable Models: 2" in body
    # Only the CURRENT pair may name a model in the bounded body.
    assert "gemini-2.5-flash-lite" not in body
    assert "gemini-2.5-pro" not in body
    assert "invalid 1" in body
    assert "action:ai_pick_model:gemini:gemini-2.5-flash" in datas
    assert "action:ai_pick_model:gemini:gemini-2.5-flash-lite" in datas
    assert "action:ai_pick_model:gemini:gemini-2.5-pro" not in datas


# ── 6. Cohere / Groq regressions ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_cohere_and_groq_keep_their_live_defaults_in_the_targets():
    statuses = [
        _status("cohere", "command-r-plus", "https://api.cohere.com/compatibility/v1"),
        _status("groq", "llama-3.3-70b-versatile", "https://api.groq.com/openai/v1"),
    ]
    catalog = {
        "cohere": ["command-r-plus", "command-a-03-2025"],
        "groq": ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"],
    }

    async def fake_fetch(provider_name, api_key, base_url, force_refresh=False):
        return [ModelInfo(id=mid, name=mid, provider=provider_name) for mid in catalog[provider_name]]

    with patch("backend.ai.model_tester._get_env", return_value="fake-key"), \
         patch("backend.ai.model_tester.fetch_models", side_effect=fake_fetch), \
         patch("backend.ai.model_tester.get_last_fetch_source", return_value="api"):
        targets, _discovered, _complete = await model_tester._build_targets(statuses, {})

    by_provider: dict[str, list[str]] = {}
    for target in targets:
        by_provider.setdefault(target["provider"], []).append(target["model"])
    assert by_provider["cohere"] == ["command-r-plus", "command-a-03-2025"]
    assert by_provider["groq"] == ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]


def test_cohere_and_groq_fallback_catalogs_are_unchanged():
    assert fallback_models("cohere")[0].id == "command-r-plus"
    assert "command-a-03-2025" in [m.id for m in fallback_models("cohere")]
    assert fallback_models("groq")[0].id == "llama-3.3-70b-versatile"
    assert _FALLBACK_CATALOG["gemini"] == ["gemini-2.5-flash", "gemini-2.5-pro"]


# ── 7. Structured Gemini failures reach the diagnostic payload ─────────────


class _FakeHTTPResponse:
    def __init__(self, status_code, payload=None, text="", headers=None):
        import json as _json

        self.status_code = status_code
        self._payload = payload
        self.text = text or (_json.dumps(payload) if payload is not None else "")
        self.headers = headers or {}

    def json(self):
        import json as _json

        if self._payload is None:
            raise ValueError("no json body")
        return _json.loads(_json.dumps(self._payload))


class _FakeGeminiClient:
    def __init__(self, response):
        self.response = response
        self.calls: list[tuple[str, dict]] = []

    async def post(self, url, json=None):
        self.calls.append((url, json))
        return self.response


def _gemini_provider(response):
    from backend.ai.providers.base.config import ProviderConfig
    from backend.ai.providers.gemini import GeminiProvider

    provider = GeminiProvider(ProviderConfig(
        provider_name="gemini",
        base_url=GEMINI_BASE,
        api_key="fake-key",
        default_model="gemini-2.5-flash",
        temperature=0.0,
        max_tokens=5,
        timeout=5,
        retry_count=0,
        enabled=True,
    ))
    provider._http_client = _FakeGeminiClient(response)
    return provider


_GEMINI_ERROR_CASES = [
    (
        404,
        {"error": {"code": 404, "status": "NOT_FOUND",
                   "message": "models/gemini-2.5-pro is not found for API version v1beta"}},
        "INVALID_MODEL", "model_lookup",
    ),
    (
        400,
        {"error": {"code": 400, "status": "INVALID_ARGUMENT",
                   "message": "Invalid JSON payload received. Unknown name 'contents'"}},
        "PROVIDER_ERROR", "request_validation",
    ),
    (
        401,
        {"error": {"code": 401, "status": "UNAUTHENTICATED",
                   "message": "API key not valid. Please pass a valid API key.",
                   "details": [{"reason": "API_KEY_INVALID"}]}},
        "AUTH_ERROR", "authentication",
    ),
    (
        403,
        {"error": {"code": 403, "status": "PERMISSION_DENIED",
                   "message": "Permission denied on the requested resource."}},
        "AUTH_ERROR", "authentication",
    ),
    (
        429,
        {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                   "message": "Resource has been exhausted (e.g. check quota)."}},
        "RATE_LIMITED", "rate_limit",
    ),
    (
        500,
        {"error": {"code": 500, "status": "INTERNAL",
                   "message": "Internal error encountered."}},
        "PROVIDER_ERROR", "server",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("http_status,payload,expected_status,expected_origin", _GEMINI_ERROR_CASES)
async def test_gemini_structured_failure_reaches_the_test_payload(
    http_status, payload, expected_status, expected_origin
):
    response = _FakeHTTPResponse(http_status, payload, headers={"retry-after": "17"})
    provider = _gemini_provider(response)
    with patch("backend.ai.model_tester._get_env", return_value="fake-key"), \
         patch("backend.ai.model_tester.ProviderFactory.create_provider", return_value=provider):
        result = await model_tester.test_single_model(
            "gemini", "Google Gemini", "◇", "gemini-2.5-flash"
        )

    assert result["status"] == expected_status
    assert result["http_status"] == http_status
    assert result["provider_code"] == str(payload["error"]["code"])
    assert result["provider_error_type"] == payload["error"]["status"]
    assert result["failure_origin"] == expected_origin
    assert result["operation"] == "generateContent"
    assert result["api_version"] == "v1beta"
    assert result["endpoint"] == "models/gemini-2.5-flash:generateContent"
    assert result["model"] == "gemini-2.5-flash"
    assert result["requested_model"] == "gemini-2.5-flash"
    assert result["provider_error_message"]
    expected_reasons = [d["reason"] for d in (payload["error"].get("details") or [])]
    assert result["provider_error_reasons"] == expected_reasons
    assert "key=" not in (result["endpoint"] or "")
    if http_status == 429:
        assert result["retry_after"] == 17


@pytest.mark.asyncio
async def test_gemini_tests_the_exact_selected_id_without_substitution():
    """A retired selection is tested AS ITSELF, so the failure is attributable."""
    response = _FakeHTTPResponse(404, {"error": {
        "code": 404, "status": "NOT_FOUND",
        "message": "models/gemini-2.0-flash is not found for API version v1beta",
    }})
    provider = _gemini_provider(response)
    client = provider._http_client
    with patch("backend.ai.model_tester._get_env", return_value="fake-key"), \
         patch("backend.ai.model_tester.ProviderFactory.create_provider", return_value=provider):
        result = await model_tester.test_single_model(
            "gemini", "Google Gemini", "◇", "gemini-2.0-flash"
        )

    assert result["requested_model"] == "gemini-2.0-flash"
    assert result["model"] == "gemini-2.0-flash"
    assert result["status"] == "INVALID_MODEL"
    assert result["endpoint"] == "models/gemini-2.0-flash:generateContent"
    assert len(client.calls) == 1
    assert "gemini-2.0-flash:generateContent" in client.calls[0][0]


@pytest.mark.asyncio
async def test_gemini_error_text_is_redacted_bounded_and_never_logged(caplog):
    secret = "AIzaSyD-verySecretKey123456"
    response = _FakeHTTPResponse(400, {"error": {
        "code": 400, "status": "INVALID_ARGUMENT",
        "message": f"API key not valid: key={secret} " + ("boom " * 200),
    }})
    provider = _gemini_provider(response)
    with patch("backend.ai.model_tester._get_env", return_value="fake-key"), \
         patch("backend.ai.model_tester.ProviderFactory.create_provider", return_value=provider), \
         caplog.at_level(logging.WARNING, logger="backend.ai.providers.gemini"):
        result = await model_tester.test_single_model(
            "gemini", "Google Gemini", "◇", "gemini-2.5-flash"
        )

    assert result["status"] == "PROVIDER_ERROR"
    assert secret not in result["provider_error_message"]
    assert "***REDACTED***" in result["provider_error_message"]
    assert len(result["provider_error_message"]) <= 300
    assert secret not in (result["error"] or "")
    trace = "\n".join(record.getMessage() for record in caplog.records)
    assert secret not in trace
    assert "key=" not in trace


@pytest.mark.asyncio
async def test_gemini_rate_limit_without_a_json_body_stays_classified():
    response = _FakeHTTPResponse(429, None, text="Too Many Requests", headers={"retry-after": "9"})
    provider = _gemini_provider(response)
    with patch("backend.ai.model_tester._get_env", return_value="fake-key"), \
         patch("backend.ai.model_tester.ProviderFactory.create_provider", return_value=provider):
        result = await model_tester.test_single_model(
            "gemini", "Google Gemini", "◇", "gemini-2.5-flash"
        )

    assert result["status"] == "RATE_LIMITED"
    assert result["retry_after"] == 9
    assert result["http_status"] == 429
    assert result["failure_origin"] == "rate_limit"
    assert result["provider_code"] == ""
    assert result["provider_error_type"] == ""
