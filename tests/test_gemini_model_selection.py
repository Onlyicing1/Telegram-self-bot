"""Gemini model selection — the owner's choice, end to end.

The invariant these tests pin:

    UI selection → persisted ai_config → runtime configuration
      → ProviderManager (chat) / GeminiMediaEngine (media, STT)
      → the exact model id in the outbound request

There is deliberately no per-model substitution table: a retired or unknown id
must fail AS that id, attributed to it, instead of being silently rewritten into
a model the owner never chose. Every test here uses the real configuration
store, the real provider/manager and the real media engine; only the HTTP
boundary and the Telegram session are fakes.
"""
from __future__ import annotations

import time
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.ai import config_store
from backend.ai import model_discovery
from backend.ai import model_tester
from backend.ai.engine import engine as engine_mod
from backend.ai.engine.engine import Engine
from backend.ai.model_discovery import (
    clear_cache,
    fetch_models,
    get_last_fetch_source,
)
from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.gemini import GeminiProvider
from backend.services import gemini_media_engine as gem
from backend.services import media_service, stt_engine_factory

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"

OWNER_A = 991001
OWNER_B = 991002
OWNER_STT = 991101


# ── fakes ───────────────────────────────────────────────────────────────────


def _resp(status_code: int, payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = {}
    resp.json.return_value = payload
    return resp


def _discovery_client(*responses) -> AsyncMock:
    client = AsyncMock()
    client.get = AsyncMock(side_effect=list(responses))
    client.__aenter__.return_value = client
    client.__aexit__.return_value = False
    return client


class _GeminiHTTP:
    """The provider's outbound HTTP, recording every URL and payload."""

    def __init__(self, status: int = 200, payload: dict | None = None) -> None:
        self.status = status
        self.payload = payload or {}
        self.calls: list[tuple[str, dict]] = []

    async def post(self, url, json=None, **_kwargs):
        self.calls.append((url, json or {}))
        return _resp(self.status, self.payload)


_OK_PAYLOAD = {
    "candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}],
    "usageMetadata": {},
}

_NOT_FOUND_PAYLOAD = {
    "error": {"code": 404, "status": "NOT_FOUND", "message": "model not found"},
}


def _engine_with_gemini(model: str, *, status: int = 200, payload: dict | None = None):
    engine = Engine()
    provider = GeminiProvider(ProviderConfig(
        provider_name="gemini", api_key="test-key", enabled=True, default_model=model,
    ))
    http = _GeminiHTTP(status, payload if payload is not None else _OK_PAYLOAD)
    provider._http_client = http
    engine.provider_manager.register_provider(provider)
    return engine, provider, http


@pytest.fixture(autouse=True)
def _restore_media_engines():
    ocr, stt = media_service.get_ocr_engine(), media_service.get_stt_engine()
    yield
    media_service.set_ocr_engine(ocr)
    media_service.set_stt_engine(stt)


# ── 1. discovery: the catalogue comes from the provider ─────────────────────


@pytest.mark.asyncio
async def test_gemini_models_are_discovered_from_the_api_not_a_static_list():
    clear_cache("gemini")
    client = _discovery_client(_resp(200, {"models": [
        {"name": "models/gemini-future-9-pro", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-future-9-flash", "supportedGenerationMethods": ["generateContent"]},
    ]}))

    with patch("backend.ai.model_discovery.httpx.AsyncClient", return_value=client):
        models = await fetch_models("gemini", "fake-key", GEMINI_BASE, force_refresh=True)

    assert [m.id for m in models] == ["gemini-future-9-flash", "gemini-future-9-pro"]
    assert get_last_fetch_source("gemini") == "api"


# ── 2. selection → persistence → runtime ────────────────────────────────────


@pytest.mark.asyncio
async def test_a_newly_discovered_model_is_persisted_and_applied(monkeypatch):
    from backend.bot.handlers import ai as ai_module

    engine, provider, _http = _engine_with_gemini("gemini-2.5-flash")
    monkeypatch.setattr(engine_mod, "get_engine", lambda: engine)

    async def _owner() -> int:
        return OWNER_A

    monkeypatch.setattr(ai_module, "_get_owner_id", _owner)

    await ai_module._ai_pick_model_action(
        None, "gemini:gemini-future-9-pro", 0,
    )

    stored = await config_store.get_config(OWNER_A)
    assert stored["provider"] == "gemini"
    assert stored["model"] == "gemini-future-9-pro"
    assert engine.provider_manager.get_active_name() == "gemini"
    assert provider.config.default_model == "gemini-future-9-pro"


@pytest.mark.asyncio
async def test_the_persisted_selection_survives_a_boot_restore(monkeypatch):
    """A stale ENV default never overrides a persisted selection."""
    monkeypatch.setenv("AI_GEMINI_MODEL", "gemini-env-stale")
    engine, provider, http = _engine_with_gemini("gemini-env-stale")
    monkeypatch.setattr(engine_mod, "get_engine", lambda: engine)

    config = await config_store.get_config(OWNER_B)
    config.update({"provider": "gemini", "model": "gemini-user-choice"})
    await config_store.save_config(OWNER_B, config)

    assert await engine_mod.apply_persisted_config(OWNER_B) is True
    assert provider.config.default_model == "gemini-user-choice"

    response = await engine.provider_manager.chat([{"role": "user", "content": "hi"}])

    assert response.success is True
    url, _payload = http.calls[0]
    assert "models/gemini-user-choice:generateContent" in url
    assert "gemini-env-stale" not in url
    assert (response.metadata or {}).get("model") == "gemini-user-choice"


@pytest.mark.asyncio
async def test_a_retired_selected_model_is_requested_as_itself_and_fails(monkeypatch):
    """No substitution: the request names the selection and the failure is its own."""
    engine, provider, http = _engine_with_gemini(
        "gemini-2.0-flash", status=404, payload=_NOT_FOUND_PAYLOAD,
    )
    monkeypatch.setattr(engine_mod, "get_engine", lambda: engine)

    config = await config_store.get_config(OWNER_B)
    config.update({"provider": "gemini", "model": "gemini-2.0-flash"})
    await config_store.save_config(OWNER_B, config)
    await engine_mod.apply_persisted_config(OWNER_B)

    response = await engine.provider_manager.chat([{"role": "user", "content": "hi"}])

    assert response.success is False
    assert http.calls, "the provider must have been asked"
    for url, _payload in http.calls:
        assert "models/gemini-2.0-flash:generateContent" in url
        assert "gemini-2.5-flash" not in url
    # The selection itself is preserved: nothing was rewritten behind the owner.
    assert provider.config.default_model == "gemini-2.0-flash"


def test_a_provider_model_mismatch_fails_without_switching():
    engine, provider, _http = _engine_with_gemini("gemini-2.5-flash")
    assert engine.provider_manager.apply_selection("gemini", "gemini-2.5-flash") is True

    assert engine.provider_manager.apply_selection("gemini-unregistered", "m") is False
    assert engine.provider_manager.get_active_name() == "gemini"
    assert provider.config.default_model == "gemini-2.5-flash"


@pytest.mark.asyncio
async def test_owner_configurations_stay_isolated():
    first = await config_store.get_config(OWNER_A)
    first.update({"provider": "gemini", "model": "gemini-owner-a"})
    await config_store.save_config(OWNER_A, first)

    second = await config_store.get_config(OWNER_B)
    second.update({"provider": "gemini", "model": "gemini-owner-b"})
    await config_store.save_config(OWNER_B, second)

    assert (await config_store.get_config(OWNER_A))["model"] == "gemini-owner-a"
    assert (await config_store.get_config(OWNER_B))["model"] == "gemini-owner-b"


# ── 3. testing uses the exact id ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_model_testing_requests_the_exact_id_it_reports(monkeypatch):
    captured: dict = {}

    async def fake_chat(self, messages, **kwargs):
        captured["model"] = kwargs.get("model")
        return MagicMock(success=True, text="ok", metadata={"model": kwargs.get("model")})

    monkeypatch.setattr(
        "backend.ai.providers.gemini.GeminiProvider.chat", fake_chat,
    )
    with patch("backend.ai.model_tester._get_env", return_value="fake-key"):
        result = await model_tester.test_single_model(
            "gemini", "Google Gemini", "◇", "gemini-future-9-pro",
        )

    assert captured["model"] == "gemini-future-9-pro"
    assert result["model"] == "gemini-future-9-pro"
    assert result["requested_model"] == "gemini-future-9-pro"


@pytest.mark.asyncio
async def test_targets_keep_discovered_and_selected_ids_unrenamed(monkeypatch):
    status = types.SimpleNamespace(
        name="gemini", display_name="Google Gemini", icon="◇",
        capability_kind="chat", has_key=True, base_url=GEMINI_BASE,
        default_model="gemini-2.5-flash",
    )
    discovered = [
        model_discovery.ModelInfo(id="gemini-future-9-pro", name="gemini-future-9-pro", provider="gemini"),
        model_discovery.ModelInfo(id="gemini-2.5-flash", name="gemini-2.5-flash", provider="gemini"),
    ]

    async def fake_fetch(*_args, **_kwargs):
        return discovered

    monkeypatch.setattr(model_tester, "fetch_models", fake_fetch)
    monkeypatch.setattr(model_tester, "get_last_fetch_source", lambda _n: "api")
    monkeypatch.setattr(model_tester, "_get_env", lambda *_: "fake-key")

    targets, _discovered, complete = await model_tester._build_targets(
        [status], {"provider": "gemini", "model": "gemini-2.0-flash"},
    )

    ids = [t["model"] for t in targets]
    # Selection-independent: a configured id outside the catalog is never
    # injected, and every catalog id is used verbatim.
    assert "gemini-2.0-flash" not in ids
    assert "gemini-future-9-pro" in ids
    assert "gemini-2.5-flash" in ids
    assert sorted(ids) == sorted(set(ids)), "no duplicates, no renaming"
    assert {m.id for m in complete["gemini"]} == {"gemini-2.5-flash", "gemini-future-9-pro"}


# ── 4. Gemini Media / STT ───────────────────────────────────────────────────


async def _persist_stt(owner: int, **stt) -> dict:
    config = await config_store.get_config(owner)
    config.update({"provider": "gemini", "model": "gemini-2.5-flash", **stt})
    await config_store.save_config(owner, config)
    return await config_store.get_config(owner)


@pytest.mark.asyncio
async def test_the_selected_stt_model_is_persisted_and_used_in_the_request(monkeypatch):
    monkeypatch.setenv("AI_GEMINI_API_KEY", "test-key")
    monkeypatch.delenv(gem.STT_MODEL_ENV_VAR, raising=False)
    # The chat selection and the STT selection are DIFFERENT settings: a chat
    # model must never become the transcription model by accident.
    persisted = await _persist_stt(
        OWNER_STT,
        stt_model=f"gemini:{gem.DEDICATED_TRANSCRIPTION_MODEL}",
        stt_language="fa-IR",
        stt_passes=1,
    )

    status = stt_engine_factory.apply_stt_config(persisted)
    assert status["configured"] is True

    engine = media_service.get_stt_engine()
    assert isinstance(engine, gem.GeminiMediaEngine)
    assert engine.stt_model == gem.DEDICATED_TRANSCRIPTION_MODEL
    assert engine.stt_language == "fa-IR"
    assert engine.stt_transport == "interactions"

    captured: dict = {}

    class _Client:
        def post(self, url, json=None, headers=None):
            captured["url"] = url
            captured["body"] = json
            return _resp(200, {
                "status": "completed",
                "steps": [{"type": "model_output",
                           "content": [{"type": "text", "text": "سلام"}]}],
            })

    def fake_perform(self, deadline, stage, call, **_kwargs):
        return call(_Client())

    monkeypatch.setattr(gem.GeminiMediaEngine, "_perform", fake_perform)

    text = engine._transcribe_interaction(
        {"type": "audio", "uri": "files/x", "mime_type": "audio/ogg"},
        time.monotonic() + 30.0,
    )

    assert text == "سلام"
    assert captured["url"].endswith("/interactions")
    assert captured["body"]["model"] == gem.DEDICATED_TRANSCRIPTION_MODEL


@pytest.mark.asyncio
async def test_an_explicit_stt_model_is_never_replaced_by_the_media_default(monkeypatch):
    monkeypatch.setenv(gem.STT_MODEL_ENV_VAR, gem.DEDICATED_TRANSCRIPTION_MODEL)
    monkeypatch.setenv("AI_GEMINI_API_KEY", "test-key")

    engine, media_model, _reason = gem.build_gemini_media_engine()

    assert engine is not None
    assert engine.stt_model == gem.DEDICATED_TRANSCRIPTION_MODEL
    assert engine.stt_model != gem.DEFAULT_MEDIA_MODEL
    assert engine.model == media_model == gem.DEFAULT_MEDIA_MODEL
    # OCR keeps the general media model; the selection never leaks into it.
    assert engine._transcription_body({"type": "audio"})["model"] == gem.DEDICATED_TRANSCRIPTION_MODEL


@pytest.mark.asyncio
async def test_no_stt_selection_keeps_the_documented_default_route(monkeypatch):
    monkeypatch.setenv("AI_GEMINI_API_KEY", "test-key")
    monkeypatch.delenv(gem.STT_MODEL_ENV_VAR, raising=False)

    monkeypatch.delenv("AI_GEMINI_MEDIA_MODEL", raising=False)
    monkeypatch.delenv("AI_GEMINI_MODEL", raising=False)
    monkeypatch.setenv("AI_GEMINI_MEDIA_MODEL", gem.DEFAULT_MEDIA_MODEL)

    persisted = await _persist_stt(OWNER_STT, stt_model="")
    status = stt_engine_factory.apply_stt_config(persisted)
    assert status["configured"] is True

    engine = media_service.get_stt_engine()
    assert engine.stt_model == ""
    assert engine.model == gem.DEFAULT_MEDIA_MODEL
    assert engine.stt_transport == "generate_content"


@pytest.mark.asyncio
async def test_changing_the_stt_selection_takes_effect_without_a_restart(monkeypatch):
    monkeypatch.setenv("AI_GEMINI_API_KEY", "test-key")

    first = await _persist_stt(
        OWNER_STT, stt_model=f"gemini:{gem.DEDICATED_TRANSCRIPTION_MODEL}",
    )
    stt_engine_factory.apply_stt_config(first)
    dedicated_engine = media_service.get_stt_engine()
    assert dedicated_engine.stt_model == gem.DEDICATED_TRANSCRIPTION_MODEL

    second = await _persist_stt(OWNER_STT, stt_model="")
    stt_engine_factory.apply_stt_config(second)
    default_engine = media_service.get_stt_engine()

    assert default_engine is not dedicated_engine
    assert default_engine.stt_model == ""
    # The replaced engine keeps the selection it was built with.
    assert dedicated_engine.stt_model == gem.DEDICATED_TRANSCRIPTION_MODEL


@pytest.mark.asyncio
async def test_the_direct_stt_path_never_routes_through_the_provider_manager(monkeypatch):
    monkeypatch.setenv("AI_GEMINI_API_KEY", "test-key")
    persisted = await _persist_stt(
        OWNER_STT, stt_model=f"gemini:{gem.DEDICATED_TRANSCRIPTION_MODEL}",
    )
    stt_engine_factory.apply_stt_config(persisted)
    engine = media_service.get_stt_engine()

    async def _forbidden(*_args, **_kwargs):
        raise AssertionError("direct STT must not call ProviderManager.chat")

    monkeypatch.setattr(
        "backend.ai.providers.manager.manager.ProviderManager.chat", _forbidden,
    )

    def fake_perform(self, deadline, stage, call, **_kwargs):
        return call(type("C", (), {
            "post": lambda self_, url, json=None, headers=None: _resp(200, {
                "status": "completed",
                "steps": [{"type": "model_output",
                           "content": [{"type": "text", "text": "ok"}]}],
            }),
        })())

    monkeypatch.setattr(gem.GeminiMediaEngine, "_perform", fake_perform)

    assert engine._transcribe_interaction(
        {"type": "audio", "uri": "files/x"}, time.monotonic() + 30.0,
    ) == "ok"
