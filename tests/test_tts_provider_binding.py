"""TTS provider/model/voice BINDING — configuration must equal execution.

The observed live bug: the owner selects Speechmatics, the panel reports
Speechmatics, yet synthesis still ran through OpenAI and the model selector still
listed OpenAI's model. That is a BINDING failure, not a cosmetic one: provider
selection, model selection, voice selection, persistence, the control plane, the
engine factory and the adapter must be one deterministic chain.

This suite therefore refuses to mock the seam where the bug lives. It exercises:

    Telegram callback → real config_store (real PostgREST-shaped fake DB)
    → real tts_control_plane registry → real tts_engine_factory
    → real tts_credential_pool → real tts_service → real provider adapter

Only two things are doubles: the database COLUMN SET (so the live schema can be
reproduced exactly) and the outbound HTTP transport. Everything internal is the
production chain.

Two column sets are exercised:

  * ``TTS_COLUMNS``     — the schema after ``20260923000001_add_ai_config_tts_settings``
                          has been applied;
  * ``NO_TTS_COLUMNS``  — the schema as it exists today, because that migration is
                          documented as NOT EXECUTED. This is the state the live
                          Telegram test ran against.

Live Telegram and live provider verification were NOT performed: no real API key
was used and no byte left the process.
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from backend.ai import config_store, credential_source, tts_control_plane as plane
from backend.services import (
    gemini_tts_engine,
    grok_tts_engine,
    openai_tts_engine,
    speechmatics_tts_engine,
    tts_credential_pool,
    tts_engine_factory,
    tts_fallback,
    tts_service,
)

_HTTPX_ASYNC_CLIENT = httpx.AsyncClient

OWNER = 7283627550
SPOKEN = "سلام، این یک آزمون است"

SECRETS = {
    "openai": "sk-binding-openai-key-000000000000000000",
    "gemini": "AIza-binding-gemini-key-00000000000000000",
    "grok": "xai-binding-grok-key-000000000000000000000",
    "speechmatics": "sm-binding-speechmatics-key-0000000000",
}

OPUS = b"OggS\x00\x02\x00\x00\x00\x00\x00\x00\x00\x00OpusHead" + b"\x11" * 32
MP3 = b"\xff\xfb\x90\x00" + b"\x22" * 64
WAV = b"RIFF" + (36 + 32).to_bytes(4, "little") + b"WAVE" + b"\x33" * 64
PCM = b"\x44" * 64

#: Every ``ai_config`` column EXCEPT the three TTS settings — the live schema
#: while ``20260923000001_add_ai_config_tts_settings.sql`` remains unapplied.
NO_TTS_COLUMNS = frozenset({
    "id", "owner_id", "provider", "model", "temperature", "max_tokens",
    "system_prompt", "history_budget", "is_configured", "trigger_en",
    "trigger_fa", "show_question", "stt_model", "stt_language", "stt_passes",
    "last_request_at", "last_latency_ms", "updated_at", "created_at",
})
TTS_COLUMNS = NO_TTS_COLUMNS | {"tts_provider", "tts_model", "tts_voice"}


# ── A PostgREST-shaped fake that enforces a column set ──────────────────────


class _Response:
    def __init__(self, data: Any) -> None:
        self.data = data


class _Query:
    def __init__(self, rows: dict[int, dict], columns: frozenset[str]) -> None:
        self._rows = rows
        self._columns = columns
        self._op = "select"
        self._payload: dict | None = None
        self._owner: int | None = None

    def select(self, *_args: Any) -> "_Query":
        return self

    def insert(self, payload: dict) -> "_Query":
        self._op = "insert"
        self._payload = dict(payload)
        return self

    def update(self, payload: dict) -> "_Query":
        self._op = "update"
        self._payload = dict(payload)
        return self

    def eq(self, field: str, value: Any) -> "_Query":
        if field == "owner_id":
            self._owner = int(value)
        return self

    def maybe_single(self) -> "_Query":
        return self

    def _reject_unknown_columns(self, payload: dict) -> None:
        """PostgREST refuses a payload naming a column the table does not have."""
        for key in payload:
            if key not in self._columns:
                raise Exception(f'column "{key}" does not exist in "ai_config"')

    def execute(self) -> _Response:
        if self._op == "insert":
            self._reject_unknown_columns(self._payload or {})
            owner = int((self._payload or {})["owner_id"])
            self._rows[owner] = dict(self._payload or {})
            return _Response(dict(self._rows[owner]))
        if self._op == "update":
            self._reject_unknown_columns(self._payload or {})
            row = self._rows.get(self._owner)
            if row is None:
                return _Response(None)
            row.update(self._payload or {})
            return _Response(dict(row))
        row = self._rows.get(self._owner)
        return _Response(dict(row) if row else None)


class _FakeDB:
    """An ``ai_config`` table that only knows the columns it is declared with."""

    def __init__(self, columns: frozenset[str]) -> None:
        self.rows: dict[int, dict] = {}
        self.payloads: list[dict] = []
        self._columns = columns

    def table(self, _name: str) -> _Query:
        query = _Query(self.rows, self._columns)
        original_execute = query.execute

        def execute() -> _Response:  # type: ignore[no-redef]
            if query._op in ("insert", "update"):
                self.payloads.append(dict(query._payload or {}))
            return original_execute()

        query.execute = execute  # type: ignore[method-assign]
        return query


@pytest.fixture(params=[TTS_COLUMNS, NO_TTS_COLUMNS], ids=["with_tts_columns", "live_no_tts_columns"])
def columns(request) -> frozenset[str]:
    return request.param


@pytest.fixture
def db(columns, monkeypatch) -> _FakeDB:
    from backend.bot.handlers import ai_tts_settings
    from backend.helper import inline_engine

    fake = _FakeDB(columns)
    # A real owner already HAS an ``ai_config`` row (provider/model/triggers
    # were written long before the TTS triple entered the payload), so the row
    # is seeded with exactly this schema's columns. With the TTS columns absent
    # — the live state — the row therefore carries no TTS key at all, which is
    # the situation the observed bug ran into.
    row: dict = {column: None for column in columns}
    row.update({
        "id": 1,
        "owner_id": OWNER,
        "provider": "gemini",
        "model": "gemini-2.0-flash",
        "is_configured": True,
        "show_question": False,
        "stt_passes": 1,
    })
    fake.rows[OWNER] = row
    monkeypatch.setattr(config_store, "_get_db", lambda: fake)
    monkeypatch.setattr(inline_engine, "_owner_id", OWNER)
    # The real handler functions, backed by the real store.
    monkeypatch.setattr(
        ai_tts_settings, "owner_and_config",
        _real_owner_and_config,
    )
    _reset_runtime(monkeypatch)
    return fake


async def _real_owner_and_config() -> tuple[int, dict]:
    """The production read path: inline_engine owner id → real ``get_config``."""
    from backend.bot.handlers.ai import _get_saved_config
    from backend.helper import inline_engine

    return inline_engine._owner_id, await _get_saved_config(inline_engine._owner_id)


def _reset_runtime(monkeypatch) -> None:
    monkeypatch.setattr(tts_service, "_selected", None)
    credential_source.reset()
    tts_credential_pool.reset()
    tts_fallback.clear_registration()
    tts_fallback.reset_health()
    # Isolated per test (restored by monkeypatch) so this suite never clears
    # another suite's in-process config values through the module global.
    monkeypatch.setattr(config_store, "_fallback_config", {})
    for module in (openai_tts_engine, gemini_tts_engine, grok_tts_engine, speechmatics_tts_engine):
        for name in module.API_KEY_ENV_VARS:
            # Each provider's FIRST declared variable carries its credential; the
            # alternates are cleared so an identical value can never be reached
            # through another provider's fallback name.
            if name == module.API_KEY_ENV_VARS[0]:
                monkeypatch.setenv(name, SECRETS[module.PROVIDER_NAME])
            else:
                monkeypatch.delenv(name, raising=False)


def _gemini_body(pcm: bytes = PCM) -> bytes:
    import base64

    return json.dumps({
        "status": "completed",
        "steps": [{"type": "model_output", "content": [
            {"type": "audio", "data": base64.b64encode(pcm).decode()},
        ]}],
    }).encode()


class _Router(httpx.AsyncBaseTransport):
    """Routes each documented provider host; ``fail`` forces a 5xx on a host."""

    def __init__(self, fail: str = "") -> None:
        self.requests: list[httpx.Request] = []
        self._fail = fail

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        for token in ("speechmatics.com", "api.x.ai", "generativelanguage.googleapis.com", "api.openai.com"):
            if token in url:
                if self._fail and token == self._fail:
                    return httpx.Response(503, content=b"{}",
                                          headers={"content-type": "application/json"})
                if token == "speechmatics.com":
                    return httpx.Response(200, content=WAV,
                                          headers={"content-type": "audio/wav"})
                if token == "api.x.ai":
                    return httpx.Response(200, content=MP3,
                                          headers={"content-type": "audio/mpeg"})
                if token == "generativelanguage.googleapis.com":
                    return httpx.Response(200, content=_gemini_body(),
                                          headers={"content-type": "application/json"})
                return httpx.Response(200, content=OPUS,
                                      headers={"content-type": "audio/ogg"})
        return httpx.Response(404, content=b"{}", headers={"content-type": "application/json"})


@pytest.fixture
def http(monkeypatch) -> _Router:
    router = _Router()
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda timeout=None, **rest: _HTTPX_ASYNC_CLIENT(transport=router, timeout=timeout),
    )
    return router


def _payloads(rows: Any) -> list[str]:
    """Every rendered button's callback payload, flattened and decoded.

    The panel builder hands back Telethon ``KeyboardButtonCallback`` objects whose
    ``data`` is BYTES, so it is decoded here rather than stringified (``str(b"x")``
    would yield ``"b'x'"`` and defeat any prefix test).
    """
    payloads: list[str] = []
    for row in rows:
        for button in (row if isinstance(row, list) else [row]):
            data = getattr(button, "data", "")
            if isinstance(data, (bytes, bytearray)):
                data = bytes(data).decode("utf-8", "replace")
            payloads.append(str(data))
    return payloads


async def _surface(monkeypatch):
    from backend.bot.handlers import ai_tts_settings as module
    from backend.bot.handlers import ai as ai_module

    monkeypatch.setattr(ai_module, "_nav_buttons", lambda _builder: None)
    return module


def _triple() -> tuple[str, str, str]:
    config = config_store._fallback_config.get(OWNER) or {}
    return (
        str(config.get("tts_provider") or ""),
        str(config.get("tts_model") or ""),
        str(config.get("tts_voice") or ""),
    )


# ══ 1-4 · The model selector is provider-scoped ════════════════════════════


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "expected_models", "forbidden_models"),
    [
        ("openai", {"gpt-4o-mini-tts"}, {"gemini-", "grok-", ""}),
        ("gemini", {"gemini-3.1-flash-tts-preview", "gemini-2.5-flash-preview-tts",
                    "gemini-2.5-pro-preview-tts"}, {"gpt-4o-mini-tts"}),
        ("grok", {""}, {"gpt-4o-mini-tts", "gemini-"}),
        ("speechmatics", {""}, {"gpt-4o-mini-tts", "gemini-"}),
    ],
)
async def test_1_model_selector_lists_only_the_selected_providers_models(
    monkeypatch, db, provider, expected_models, forbidden_models,
):
    module = await _surface(monkeypatch)
    await module._ai_tts_select_action(None, provider, 0)

    _title, body, _buttons = await module._ai_media_tts_model_panel_handler(None, "")

    entry = plane.get_provider(provider)
    assert set(entry.model_ids()) == expected_models
    for model_id in forbidden_models:
        if model_id:
            assert model_id not in body, f"{provider} panel leaked model {model_id}"
    for model in entry.models:
        assert (model.model_id or "provider default") in body or model.label in body


# ══ 5-6 · Switching provider clears the incompatible model/voice ═══════════


@pytest.mark.asyncio
async def test_5_selecting_speechmatics_after_openai_clears_the_openai_model(monkeypatch, db):
    module = await _surface(monkeypatch)
    await module._ai_tts_select_action(None, "openai", 0)
    assert _triple()[1] in ("", "gpt-4o-mini-tts")

    await module._ai_tts_select_action(None, "speechmatics", 0)

    provider, model, _voice = _triple()
    assert provider == "speechmatics"
    assert model != "gpt-4o-mini-tts"
    selection = plane.parse_tts_config(await config_store.get_config(OWNER))
    assert selection.model in plane.model_ids("speechmatics")


@pytest.mark.asyncio
async def test_6_selecting_speechmatics_after_openai_clears_the_openai_voice(monkeypatch, db):
    module = await _surface(monkeypatch)
    await module._ai_tts_select_action(None, "openai", 0)
    await module._ai_tts_select_voice_action(None, "shimmer", 0)
    assert _triple()[2] == "shimmer"

    await module._ai_tts_select_action(None, "speechmatics", 0)

    provider, _model, voice = _triple()
    assert provider == "speechmatics"
    assert voice not in ("shimmer", "alloy")
    selection = plane.parse_tts_config(await config_store.get_config(OWNER))
    assert voice in plane.voice_ids(selection.provider, selection.model)
    assert selection.is_valid


# ══ 7 · Persistence round-trip ═════════════════════════════════════════════


@pytest.mark.asyncio
async def test_7_persisted_speechmatics_survives_a_reload(monkeypatch, db):
    module = await _surface(monkeypatch)
    await module._ai_tts_select_action(None, "speechmatics", 0)

    reloaded = plane.parse_tts_config(await config_store.get_config(OWNER))

    assert reloaded.provider == "speechmatics", reloaded
    assert reloaded.model in plane.model_ids("speechmatics")
    assert reloaded.voice in plane.voice_ids("speechmatics", reloaded.model)
    assert tts_service.current_selection().provider == "speechmatics"


# ══ 8-11 · The factory resolves the SELECTED provider's adapter ════════════


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "engine_class"),
    [
        ("openai", openai_tts_engine.OpenAiSpeechEngine),
        ("gemini", gemini_tts_engine.GeminiSpeechEngine),
        ("grok", grok_tts_engine.GrokSpeechEngine),
        ("speechmatics", speechmatics_tts_engine.SpeechmaticsSpeechEngine),
    ],
)
async def test_8_factory_resolves_the_selected_providers_adapter(
    monkeypatch, db, provider, engine_class,
):
    module = await _surface(monkeypatch)
    await module._ai_tts_select_action(None, provider, 0)
    await module.apply_tts_settings_now(OWNER)

    selection = plane.parse_tts_config(await config_store.get_config(OWNER))
    assert selection.provider == provider
    engine, reason = tts_engine_factory.build_engine(selection)

    assert reason == "" and engine is not None, reason
    assert isinstance(engine, engine_class), f"{provider} resolved {type(engine).__name__}"
    assert engine.provider == provider


# ══ 12-14 · Credential resolution is provider-scoped ═══════════════════════


@pytest.mark.parametrize(
    ("provider", "own_env"),
    [
        ("speechmatics", speechmatics_tts_engine.API_KEY_ENV_VARS[0]),
        ("gemini", gemini_tts_engine.API_KEY_ENV_VARS[0]),
        ("grok", grok_tts_engine.API_KEY_ENV_VARS[0]),
    ],
)
@pytest.mark.asyncio
async def test_12_provider_credentials_are_never_resolved_from_openai(
    monkeypatch, db, provider, own_env,
):
    module = await _surface(monkeypatch)
    await module._ai_tts_select_action(None, provider, 0)
    await module.apply_tts_settings_now(OWNER)

    selection = plane.parse_tts_config(await config_store.get_config(OWNER))
    record = tts_credential_pool.first_for(selection.provider)

    assert record is not None, f"no credential resolved for {provider}"
    assert record.provider == provider
    if record.is_env:
        assert own_env in record.credential_id


# ══ 15 · An explicit selection is never silently replaced by OpenAI ════════


@pytest.mark.asyncio
async def test_15_explicit_speechmatics_never_becomes_openai(monkeypatch, db, http):
    module = await _surface(monkeypatch)
    await module._ai_tts_select_action(None, "speechmatics", 0)
    await module.apply_tts_settings_now(OWNER)

    assert tts_service.current_selection().provider == "speechmatics"
    assert tts_service.describe()["provider"] == "speechmatics"

    clip = await tts_service.synthesize(SPOKEN)

    assert clip.provider == "speechmatics"
    assert "api.openai.com" not in "".join(str(r.url) for r in http.requests)


# ══ 16 · A runtime fallback never rewrites the persisted configuration ══════


@pytest.mark.asyncio
async def test_16_runtime_fallback_does_not_rewrite_persisted_provider(monkeypatch, db):
    router = _Router(fail="speechmatics.com")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            httpx, "AsyncClient",
            lambda timeout=None, **rest: _HTTPX_ASYNC_CLIENT(
                transport=router, timeout=timeout,
            ),
        )
        module = await _surface(mp)
        await module._ai_tts_select_action(None, "speechmatics", 0)
        await module.apply_tts_settings_now(OWNER)
        assert tts_service.current_selection().provider == "speechmatics"

        clip = await tts_service.synthesize(SPOKEN)

        # A substitute provider produced the clip...
        assert clip.provider != "speechmatics"
        # ...but the owner's selection is untouched in RAM and in the store.
        assert tts_service.current_selection().provider == "speechmatics"
        assert plane.parse_tts_config(
            await config_store.get_config(OWNER)
        ).provider == "speechmatics"

    _title, body, _buttons = await module._ai_media_tts_panel_handler(None, "")
    assert "Provider · Speechmatics" in body, body


# ══ 17-18 · Cross-provider model/voice combinations are rejected ═══════════


@pytest.mark.parametrize(
    ("provider", "foreign_model", "foreign_voice", "expected"),
    [
        ("speechmatics", "gpt-4o-mini-tts", "alloy", tts_service.FAILURE_UNSUPPORTED_MODEL),
        ("gemini", "gpt-4o-mini-tts", "alloy", tts_service.FAILURE_UNSUPPORTED_MODEL),
        ("grok", "gemini-3.1-flash-tts-preview", "Kore", tts_service.FAILURE_UNSUPPORTED_MODEL),
        ("speechmatics", "", "alloy", tts_service.FAILURE_UNSUPPORTED_VOICE),
        ("gemini", "gemini-3.1-flash-tts-preview", "alloy", tts_service.FAILURE_UNSUPPORTED_VOICE),
        ("openai", "gpt-4o-mini-tts", "sarah", tts_service.FAILURE_UNSUPPORTED_VOICE),
    ],
)
def test_17_cross_provider_triple_is_refused(provider, foreign_model, foreign_voice, expected):
    record = credential_source.CredentialRecord(
        credential_id="explicit", provider=provider,
        secret=SECRETS[provider], source=credential_source.SOURCE_VAULT,
    )
    engine, reason = tts_engine_factory.build_engine_for(
        provider, foreign_model, foreign_voice, record,
    )
    assert engine is None
    assert reason == expected


@pytest.mark.asyncio
async def test_18_cross_provider_stored_triple_degrades_within_the_provider(monkeypatch, db):
    await tts_service.apply_tts_settings_async({
        "tts_provider": "speechmatics", "tts_model": "gpt-4o-mini-tts", "tts_voice": "alloy",
    })

    selection = tts_service.current_selection()

    assert selection.provider == "speechmatics"
    assert selection.model in plane.model_ids("speechmatics")
    assert selection.voice in plane.voice_ids("speechmatics", selection.model)
    assert selection.adjusted, "the degradation is reported, never silent"


# ══ 19 · The UI derives its options from the persisted selection ═══════════


@pytest.mark.asyncio
async def test_19_ui_model_options_are_derived_from_the_selected_provider(monkeypatch, db):
    module = await _surface(monkeypatch)

    await module._ai_tts_select_action(None, "speechmatics", 0)
    _title, body, _buttons = await module._ai_media_tts_model_panel_handler(None, "")
    assert "Provider · Speechmatics" in body
    assert plane.get_model("openai", plane.DEFAULT_MODEL_ID).label not in body
    for model in plane.get_provider("speechmatics").models:
        assert model.label in body

    await module._ai_tts_select_action(None, "gemini", 0)
    _title, body, _buttons = await module._ai_media_tts_model_panel_handler(None, "")
    assert "Provider · Gemini" in body
    assert plane.get_model("openai", plane.DEFAULT_MODEL_ID).label not in body
    gemini = plane.get_provider("gemini")
    for model in gemini.models:
        assert model.label in body
    # exactly this provider's models, no more and no less
    assert body.count("· current") + body.count("· available") == len(gemini.models)

    _title, voice_body, voice_buttons = await module._ai_media_tts_voice_panel_handler(None, "")
    assert "Provider · Gemini" in voice_body
    payloads = _payloads(voice_buttons)
    # every voice offered belongs to the SELECTED provider/model, and no
    # OpenAI voice is offered — asserted on the callback payloads, never on
    # substrings (an OpenAI id such as "ash" occurs inside "Flash").
    offered = {
        data.split(":", 2)[-1] for data in payloads if data.startswith("action:ai_tts_select_voice:")
    }
    assert offered
    selection = plane.parse_tts_config(await config_store.get_config(OWNER))
    assert selection.provider == "gemini"
    # every voice of the SELECTED provider/model is offered (the current one is
    # withheld because it is already selected) — and nothing else is.
    assert offered == (
        set(plane.voice_ids("gemini", plane.get_provider("gemini").default_model_id))
        - {selection.voice}
    )
    assert selection.voice not in offered
    for voice in openai_tts_engine.VOICE_ORDER:
        assert voice not in offered


# ══ 20 · The execution path uses the provider the owner selected ═══════════


@pytest.mark.asyncio
async def test_20_execution_path_uses_the_selected_provider(monkeypatch, db, http):
    module = await _surface(monkeypatch)
    await module._ai_tts_select_action(None, "grok", 0)
    await module.apply_tts_settings_now(OWNER)

    clip = await tts_service.synthesize(SPOKEN)

    assert clip.provider == "grok"
    assert clip.mime_type == "audio/mpeg"
    assert len(http.requests) == 1
    assert "api.x.ai" in str(http.requests[0].url)


# ══ 21 · THE END-TO-END BINDING (the most important test) ══════════════════


@pytest.mark.asyncio
async def test_21_end_to_end_speechmatics_selection_executes_speechmatics(
    monkeypatch, db, http,
):
    """settings → persisted config → control plane → registry → factory → adapter."""
    module = await _surface(monkeypatch)

    await module._ai_tts_select_action(None, "speechmatics", 0)
    assert tts_service.current_selection().provider == "speechmatics"

    reloaded = plane.parse_tts_config(await config_store.get_config(OWNER))
    assert reloaded.provider == "speechmatics"

    engine, reason = tts_engine_factory.build_engine(reloaded)
    assert reason == "" and engine is not None
    assert isinstance(engine, speechmatics_tts_engine.SpeechmaticsSpeechEngine)

    record = tts_credential_pool.first_for(reloaded.provider)
    assert record is not None and record.provider == "speechmatics"

    clip = await tts_service.synthesize(SPOKEN)

    # selected / resolved / executed / credential are ONE provider
    assert tts_service.current_selection().provider == "speechmatics"
    assert reloaded.provider == "speechmatics"
    assert clip.provider == "speechmatics"
    assert clip.model not in ("gpt-4o-mini-tts",)
    assert clip.voice not in ("alloy", "shimmer", "echo", "fable", "nova", "onyx")
    assert clip.voice in plane.voice_ids("speechmatics", clip.model)

    sent = b"".join(bytes(r.content) for r in http.requests)
    urls = "".join(str(r.url) for r in http.requests)
    assert "api.openai.com" not in urls, urls
    assert b"gpt-4o-mini-tts" not in sent
    assert openai_tts_engine.API_KEY_ENV_VARS[0].encode() not in sent
    for header in [h for r in http.requests for h, _v in r.headers.items()]:
        assert SECRETS["openai"].encode() not in sent

    # the credential that provisioned the engine belongs to speechmatics
    assert record.provider == tts_credential_pool.first_for("speechmatics").provider
