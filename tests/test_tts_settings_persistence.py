"""TTS PART 1 — durable settings persistence for the Text-to-Speech selection.

The owner's selection (``tts_provider`` / ``tts_model`` / ``tts_voice``) must be
DURABLE: written to the existing per-owner ``ai_config`` row through the existing
store, recovered after a restart, and never confused with the in-process
in-memory fallback.

The live failure this suite pins: the three keys used to ride in the SAME upsert
payload as every other AI setting, and on a database that does not yet carry the
TTS columns PostgREST rejects the whole statement (42703) — so the TTS selection
was session-only AND the owner's provider, model, triggers and STT settings
stopped persisting at the same time, while ``get_config`` kept serving the
in-process value as if it were stored.

Only two things are doubled here: the database COLUMN SET (so both the
pre-migration and the post-migration schema can be exercised) and nothing else.
The chain under test is the production one:

    selection action -> real ``config_store`` -> the ``ai_config`` table
    -> real ``tts_control_plane`` -> real ``tts_service``
    -> real ``RuntimeSupervisor`` startup hook

The column sets are DERIVED from the ONE authoritative setup block in
DATABASE_ARCHITECTURE.md §31.3 rather than typed here, so this suite cannot pass
against a schema the document does not actually create.

No live Supabase, Telegram or speech provider was contacted, and no SQL was
executed: the database is a PostgREST-shaped fake that refuses a payload naming a
column its table does not have.
"""
from __future__ import annotations

import re
import types
from typing import Any

import pytest

from backend.ai import config_store, credential_source, tts_control_plane as plane
from backend.services import tts_credential_pool, tts_fallback, tts_service
from tests.test_canonical_schema_reconciliation import doc_setup_block

OWNER = 7283627550

#: The three keys this phase owns, and the ONLY columns added by
#: ``20260923000001_add_ai_config_tts_settings.sql``.
TTS_STORAGE_KEYS = frozenset({"tts_provider", "tts_model", "tts_voice"})


def _block_ai_config_columns() -> frozenset[str]:
    """Every ``ai_config`` column the authoritative setup block creates.

    Read from the document, not typed here: the block is the ONE statement set the
    owner pastes, so a column it does not create must never be written by the app.
    """
    block = doc_setup_block()
    # ``\s+`` (not a literal space): the block's parts wrap the statement across
    # lines, and a part whose columns were missed would silently shrink the
    # schema this suite tests against.
    columns = set(
        re.findall(r"ALTER TABLE ai_config\s+ADD COLUMN IF NOT EXISTS\s+(\w+)", block)
    )
    assert columns, "the authoritative block must establish ai_config's columns"
    return frozenset(columns)


#: The schema AFTER the TTS migration is applied (and after the block is run).
POST_MIGRATION_COLUMNS = _block_ai_config_columns()
#: The schema as it exists while that migration is still pending.
PRE_MIGRATION_COLUMNS = POST_MIGRATION_COLUMNS - TTS_STORAGE_KEYS


# ── A PostgREST-shaped ``ai_config`` table ───────────────────────────────────


class _PostgrestError(Exception):
    """What PostgREST raises: an unknown column, a violated constraint, or I/O."""


class _Response:
    def __init__(self, data: Any) -> None:
        self.data = data


class _Query:
    def __init__(self, table: "FakeAiConfig") -> None:
        self._table = table
        self._op = "select"
        self._payload: dict[str, Any] | None = None
        self._owner: int | None = None

    def select(self, *_columns: Any) -> "_Query":
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

    def execute(self) -> _Response:
        table = self._table
        if self._op == "select":
            if table.fail_reads:
                raise _PostgrestError("connection refused")
            row = table.rows.get(self._owner)
            return _Response(dict(row) if row is not None else None)

        payload = self._payload or {}
        table.statements.append(dict(payload))
        # PostgreSQL's 42703, as PostgREST surfaces it.
        for key in payload:
            if key not in table.columns:
                raise _PostgrestError(
                    f'column "{key}" of relation "ai_config" does not exist'
                )
        if table.fail_writes:
            raise _PostgrestError("connection refused")

        if self._op == "insert":
            if "owner_id" not in payload:
                raise _PostgrestError(
                    'null value in column "owner_id" violates not-null constraint'
                )
            owner = int(payload["owner_id"])
            row = {name: None for name in table.columns}
            row.update(payload)
            table.rows[owner] = row
            return _Response(dict(row))

        row = table.rows.get(self._owner)
        if row is None:
            return _Response(None)
        row.update(payload)
        return _Response(dict(row))


class FakeAiConfig:
    """An ``ai_config`` table that only knows the columns it is given."""

    def __init__(
        self,
        columns: frozenset[str] = POST_MIGRATION_COLUMNS,
        *,
        fail_writes: bool = False,
        fail_reads: bool = False,
    ) -> None:
        self.columns = frozenset(columns)
        self.rows: dict[int, dict[str, Any]] = {}
        self.statements: list[dict[str, Any]] = []
        self.fail_writes = fail_writes
        self.fail_reads = fail_reads

    def table(self, _name: str) -> _Query:
        return _Query(self)

    def seed_owner_row(self, provider: str = "gemini", model: str = "gemini-2.0-flash") -> None:
        """A real owner already has an ``ai_config`` row, shaped by THIS schema."""
        row = {name: None for name in self.columns}
        row.update({
            "id": 1,
            "owner_id": OWNER,
            "provider": provider,
            "model": model,
            "is_configured": True,
            "show_question": False,
            "stt_passes": 1,
        })
        self.rows[OWNER] = row

    # ── assertions helpers ────────────────────────────────────────────────

    def row(self) -> dict[str, Any]:
        return dict(self.rows.get(OWNER) or {})

    def stored(self, key: str) -> Any:
        return self.row().get(key)

    def statements_writing(self, *keys: str) -> list[dict[str, Any]]:
        wanted = set(keys)
        return [s for s in self.statements if wanted <= set(s)]

    def statements_touching(self, *keys: str) -> list[dict[str, Any]]:
        wanted = set(keys)
        return [s for s in self.statements if wanted & set(s)]


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture()
def db(monkeypatch) -> FakeAiConfig:
    from backend.bot.handlers import ai as ai_module
    from backend.bot.handlers import ai_tts_settings
    from backend.helper import inline_engine

    fake = FakeAiConfig(POST_MIGRATION_COLUMNS)
    fake.seed_owner_row()
    monkeypatch.setattr(config_store, "_get_db", lambda: fake)
    monkeypatch.setattr(inline_engine, "_owner_id", OWNER)
    monkeypatch.setattr(ai_module, "_nav_buttons", lambda _builder: None)
    monkeypatch.setattr(ai_tts_settings, "_nav_buttons", lambda _builder: None, raising=False)
    _isolate_process_state(monkeypatch)
    return fake


@pytest.fixture()
def pending_db(monkeypatch) -> FakeAiConfig:
    """The live schema: ``20260923000001`` has not been applied yet."""
    from backend.bot.handlers import ai as ai_module
    from backend.helper import inline_engine

    fake = FakeAiConfig(PRE_MIGRATION_COLUMNS)
    fake.seed_owner_row()
    monkeypatch.setattr(config_store, "_get_db", lambda: fake)
    monkeypatch.setattr(inline_engine, "_owner_id", OWNER)
    monkeypatch.setattr(ai_module, "_nav_buttons", lambda _builder: None)
    _isolate_process_state(monkeypatch)
    return fake


def _isolate_process_state(monkeypatch) -> None:
    """Give every test its own in-process state (restored by monkeypatch)."""
    monkeypatch.setattr(config_store, "_fallback_config", {})
    monkeypatch.setattr(tts_service, "_selected", None)
    credential_source.reset()
    tts_credential_pool.reset()
    tts_fallback.clear_registration()
    tts_fallback.reset_health()


def _simulate_restart(monkeypatch, db: FakeAiConfig) -> None:
    """Drop EVERY in-process value; only the durable row can answer now.

    A restart is exactly this: the process's memory is gone and the ``ai_config``
    table is all that is left. Nothing is cleared from ``db`` on purpose.
    """
    monkeypatch.setattr(config_store, "_fallback_config", {})
    monkeypatch.setattr(tts_service, "_selected", None)
    credential_source.reset()
    tts_credential_pool.reset()
    tts_fallback.clear_registration()
    tts_fallback.reset_health()


async def _selection_action(provider: str) -> tuple[str, str, list]:
    from backend.bot.handlers import ai_tts_settings as module

    result = await module._ai_tts_select_action(None, provider, 0)
    assert result is not None
    return result


async def _model_action(model_id: str) -> tuple[str, str, list]:
    from backend.bot.handlers import ai_tts_settings as module

    result = await module._ai_tts_select_model_action(None, model_id, 0)
    assert result is not None
    return result


async def _voice_action(voice_id: str) -> tuple[str, str, list]:
    from backend.bot.handlers import ai_tts_settings as module

    result = await module._ai_tts_select_voice_action(None, voice_id, 0)
    assert result is not None
    return result


async def _startup() -> None:
    """Run the REAL supervisor startup hook, with only its owner id supplied."""
    from backend.runtime.supervisor import RuntimeSupervisor

    stub = types.SimpleNamespace(owner_id=OWNER)
    await RuntimeSupervisor._apply_persisted_tts_settings(stub)


def _stored_values(db: FakeAiConfig) -> dict[str, str]:
    """What the ROW holds, in the selection's own vocabulary (empty = default)."""
    return {key: str(db.stored(key) or "") for key in plane.STORAGE_KEYS}


def _effective(db: FakeAiConfig) -> dict[str, str]:
    """The selection's own storage values, read back from the row."""
    return plane.parse_tts_config(_stored_values(db)).storage_values()


def _resolved_from_store(config: dict) -> plane.TtsSelection:
    return plane.parse_tts_config(config)


# ══ 1-4 · Provider, model and voice each persist; read-after-write agrees ═══


@pytest.mark.asyncio
async def test_selecting_a_provider_persists_it(db):
    await _selection_action("speechmatics")

    assert db.stored("tts_provider") == "speechmatics"
    assert _resolved_from_store(await config_store.get_config(OWNER)).provider == "speechmatics"


@pytest.mark.asyncio
async def test_selecting_a_model_persists_it(db):
    await _selection_action("gemini")
    model = plane.get_provider("gemini").model_ids()[-1]
    await _model_action(model)

    assert db.stored("tts_model") == model
    assert _resolved_from_store(await config_store.get_config(OWNER)).model == model


@pytest.mark.asyncio
async def test_selecting_a_voice_persists_it(db):
    await _selection_action("openai")
    await _voice_action("shimmer")

    assert db.stored("tts_voice") == "shimmer"
    assert _resolved_from_store(await config_store.get_config(OWNER)).voice == "shimmer"


@pytest.mark.asyncio
async def test_every_store_read_after_write_returns_the_stored_triple(db):
    """The store, not the handler's candidate, is the authority after a write."""
    await _selection_action("speechmatics")

    for _ in range(3):
        selection = _resolved_from_store(await config_store.get_config(OWNER))
        assert selection.storage_values() == _stored_values(db)
        assert selection.storage_values() == _effective(db)
        assert selection.is_valid


# ══ 5-6 · A restart recovers the selection from persistence, not from RAM ═══


@pytest.mark.asyncio
async def test_the_selection_survives_a_simulated_restart(monkeypatch, db):
    await _selection_action("speechmatics")
    await _voice_action(plane.voice_ids("speechmatics", "")[2])
    before = _resolved_from_store(await config_store.get_config(OWNER))
    assert before.provider == "speechmatics" and before.voice == "megan"

    _simulate_restart(monkeypatch, db)
    assert config_store._fallback_config == {}  # nothing left in memory

    recovered = _resolved_from_store(await config_store.get_config(OWNER))
    assert (recovered.provider, recovered.model, recovered.voice) == (
        before.provider, before.model, before.voice
    )
    assert recovered.storage_values() == _stored_values(db)


@pytest.mark.asyncio
async def test_runtime_startup_loads_the_persisted_selection(monkeypatch, db):
    await _selection_action("speechmatics")
    await _voice_action(plane.voice_ids("speechmatics", "")[3])
    durable = _stored_values(db)

    _simulate_restart(monkeypatch, db)
    await _startup()

    installed = tts_service.current_selection()
    assert installed.provider == "speechmatics"
    assert installed.storage_values() == durable
    assert installed.voice == "jack"


@pytest.mark.asyncio
async def test_an_explicit_provider_never_reverts_to_the_openai_default(monkeypatch, db):
    """OpenAI is the compiled default; an explicit selection must outrank it."""
    await _selection_action("grok")
    assert plane.DEFAULT_PROVIDER_ID == "openai"

    _simulate_restart(monkeypatch, db)
    await _startup()

    assert tts_service.current_selection().provider == "grok"
    assert tts_service.describe()["provider"] == "grok"


# ══ 7-9 · The stored triple stays internally consistent ════════════════════


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "gemini", "grok", "speechmatics"])
async def test_the_stored_triple_is_a_valid_triple_for_its_provider(db, provider):
    await _selection_action(provider)

    selection = _resolved_from_store(await config_store.get_config(OWNER))

    assert selection.provider == provider
    assert selection.is_valid, selection
    assert selection.adjusted == "", "a freshly written selection needs no degradation"


@pytest.mark.asyncio
async def test_a_provider_change_leaves_no_incompatible_model_or_voice(db):
    await _selection_action("openai")
    await _voice_action("shimmer")
    assert db.stored("tts_voice") == "shimmer"

    await _selection_action("speechmatics")

    stored = _stored_values(db)
    selection = _resolved_from_store(await config_store.get_config(OWNER))
    assert stored["tts_provider"] == "speechmatics"
    assert stored["tts_model"] != "gpt-4o-mini-tts"
    assert stored["tts_voice"] != "shimmer", "the old provider's voice must not remain"
    assert stored["tts_voice"] in plane.voice_ids(selection.provider, selection.model)
    assert stored == selection.storage_values()
    assert selection.provider == "speechmatics" and selection.is_valid


@pytest.mark.asyncio
async def test_a_model_change_leaves_no_incompatible_voice(db):
    """A voice the selected model does not offer is never the effective voice."""
    foreign_voice = plane.voice_ids("speechmatics", "")[0]
    model = plane.get_provider("gemini").model_ids()[0]
    assert foreign_voice not in plane.voice_ids("gemini", model)

    assert await config_store.save_tts_settings(OWNER, "gemini", model, foreign_voice)

    selection = _resolved_from_store(await config_store.get_config(OWNER))

    assert selection.provider == "gemini"
    assert selection.model == model
    assert selection.voice != foreign_voice
    assert selection.voice in plane.voice_ids("gemini", model)
    assert selection.adjusted, "the degradation is reported, never silent"


# ══ 10 · The write is isolated, so it cannot damage the rest of the config ══


@pytest.mark.asyncio
async def test_the_selection_is_written_in_one_isolated_statement(db):
    await _selection_action("speechmatics")

    trio_writes = db.statements_writing(*plane.STORAGE_KEYS)
    assert trio_writes, "the trio must be written"
    for payload in trio_writes:
        extra = set(payload) - set(plane.STORAGE_KEYS) - {"owner_id", "created_at", "updated_at"}
        assert not extra, f"the TTS write must name no other column: {sorted(extra)}"


@pytest.mark.asyncio
async def test_no_other_ai_config_write_carries_a_tts_key(db):
    """The shared payload must stay writable on a schema without the TTS columns."""
    from backend.bot.handlers import ai as ai_module

    await ai_module._save_config(OWNER, {**await config_store.get_config(OWNER), "model": "x"})

    shared = [p for p in db.statements if "model" in p]
    assert shared, "the shared upsert must have been attempted"
    for payload in shared:
        assert not (set(payload) & TTS_STORAGE_KEYS), sorted(set(payload) & TTS_STORAGE_KEYS)


@pytest.mark.asyncio
async def test_a_store_writing_an_unknown_column_is_caught_by_the_setup_block(db):
    """Schema compatibility: the block must create every column the store writes."""
    from backend.bot.handlers import ai as ai_module

    await ai_module._save_config(OWNER, await config_store.get_config(OWNER))
    await _selection_action("gemini")

    written: set[str] = set()
    for payload in db.statements:
        written |= set(payload)
    assert written, "the store must have written something"
    assert written <= POST_MIGRATION_COLUMNS, sorted(written - POST_MIGRATION_COLUMNS)
    assert TTS_STORAGE_KEYS <= POST_MIGRATION_COLUMNS, (
        "the ONE authoritative setup block must create the TTS settings columns"
    )


def test_the_config_store_and_the_control_plane_name_the_same_three_keys():
    assert tuple(config_store.TTS_STORAGE_KEYS) == tuple(plane.STORAGE_KEYS)
    assert set(plane.STORAGE_KEYS) == TTS_STORAGE_KEYS


# ══ 11-13 · The pending migration degrades ONLY the trio, and says so ═══════


@pytest.mark.asyncio
async def test_a_pending_tts_migration_never_breaks_the_rest_of_the_configuration(pending_db):
    """The regression this phase fixes: a missing TTS column used to reject the
    WHOLE upsert, so the owner's provider, model and triggers stopped persisting."""
    assert not (set(pending_db.columns) & TTS_STORAGE_KEYS)

    saved = await config_store.save_config(OWNER, {
        **await config_store.get_config(OWNER),
        "provider": "cerebras",
        "model": "llama-3.3-70b",
        "trigger_en": "Nova",
        "show_question": True,
    })

    assert saved is True, "the durable ai_config row WAS written"
    assert pending_db.stored("provider") == "cerebras"
    assert pending_db.stored("model") == "llama-3.3-70b"
    assert pending_db.stored("trigger_en") == "Nova"
    assert pending_db.stored("show_question") is True


@pytest.mark.asyncio
async def test_a_pending_tts_migration_is_reported_as_session_only(pending_db):
    from backend.bot.handlers import ai_tts_settings as module

    saved = await module.persist_selection(OWNER, plane.default_selection())

    assert saved is False
    assert pending_db.stored("tts_provider") is None, "nothing reached the row"
    assert module._outcome(saved, "done").count("this session") == 1

    # ...and the owner-facing panel says the same thing, because the store
    # reports the trio as served from RAM.
    config = await config_store.get_config(OWNER)
    assert set(config[config_store.SESSION_ONLY_KEY]) >= TTS_STORAGE_KEYS
    assert module._selection_is_session_only(config) is True
    _title, body, _buttons = await module._ai_media_tts_panel_handler(None, "")
    assert "not stored" in body


@pytest.mark.asyncio
async def test_a_session_only_selection_is_lost_on_restart(monkeypatch, pending_db):
    from backend.bot.handlers import ai_tts_settings as module

    await module._ai_tts_select_action(None, "speechmatics", 0)
    # visible for this process...
    assert _resolved_from_store(await config_store.get_config(OWNER)).provider == "speechmatics"
    assert "speechmatics" not in {v for v in pending_db.row().values() if isinstance(v, str)}

    _simulate_restart(monkeypatch, pending_db)

    # ...and gone after it, because RAM is not a store.
    assert _resolved_from_store(await config_store.get_config(OWNER)).provider == (
        plane.DEFAULT_PROVIDER_ID
    )
    assert not module._selection_is_session_only(await config_store.get_config(OWNER))


# ══ 14-15 · Database failure behavior ═════════════════════════════════════


@pytest.mark.asyncio
async def test_a_failed_write_is_reported_and_never_presented_as_stored(monkeypatch):
    from backend.bot.handlers import ai_tts_settings as module
    from backend.helper import inline_engine

    failing = FakeAiConfig(POST_MIGRATION_COLUMNS, fail_writes=True)
    failing.seed_owner_row()
    monkeypatch.setattr(config_store, "_get_db", lambda: failing)
    monkeypatch.setattr(inline_engine, "_owner_id", OWNER)
    _isolate_process_state(monkeypatch)

    saved = await module.persist_selection(OWNER, plane.resolve("speechmatics", "", "sarah"))

    assert saved is False
    assert failing.row()["tts_provider"] is None, "the rejected write reached no column"
    assert "this session" in module._outcome(saved, "done")
    # The row exists but carries no selection, so the store still reports the
    # default: a failed write is never reported as a stored value.
    assert _resolved_from_store(await config_store.get_config(OWNER)).provider == (
        plane.DEFAULT_PROVIDER_ID
    )

    _simulate_restart(monkeypatch, failing)
    assert _resolved_from_store(await config_store.get_config(OWNER)).provider == (
        plane.DEFAULT_PROVIDER_ID
    )


@pytest.mark.asyncio
async def test_a_failed_read_is_never_reported_as_a_stored_default(monkeypatch):
    failing = FakeAiConfig(POST_MIGRATION_COLUMNS, fail_reads=True)
    failing.seed_owner_row(provider="speechmatics")
    monkeypatch.setattr(config_store, "_get_db", lambda: failing)
    monkeypatch.setattr(tts_service, "_selected", None)
    monkeypatch.setattr(config_store, "_fallback_config", {})

    config = await config_store.get_config(OWNER)

    assert config[config_store.DEGRADED_READ_KEY] is True
    assert plane.parse_tts_config({} if config[config_store.DEGRADED_READ_KEY] else config).provider == (
        plane.DEFAULT_PROVIDER_ID
    )


@pytest.mark.asyncio
async def test_startup_keeps_the_applied_selection_when_the_read_fails(monkeypatch):
    failing = FakeAiConfig(POST_MIGRATION_COLUMNS, fail_reads=True)
    failing.seed_owner_row(provider="speechmatics")
    monkeypatch.setattr(config_store, "_get_db", lambda: failing)
    monkeypatch.setattr(config_store, "_fallback_config", {})
    monkeypatch.setattr(tts_service, "_selected", plane.resolve("speechmatics", "", "sarah"))

    await _startup()

    assert tts_service.current_selection().provider == "speechmatics"
