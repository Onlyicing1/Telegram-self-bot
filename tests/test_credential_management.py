"""
API Credential Vault — PART 2 (owner-facing management).

PART 1 added the metadata table and the resolution RPC. This phase adds the
MANAGEMENT boundary the owner drives from Telegram:

    Telegram owner
        ↓
    AI → Media Analysis → API Credentials      backend/bot/handlers/ai_credentials.py
        ↓
    backend/services/credential_service.py     validation, bounded reasons, handles
        ↓
    five owner-scoped SECURITY DEFINER RPCs    (this suite pins the migration)
        ├── public.api_credentials             metadata ONLY — no secret column
        └── vault.secrets                      the raw key, via vault.create_secret

What this suite proves, and what it deliberately does NOT:

  * the migration and the documented manual SQL are the same statements, and both
    are labelled NOT EXECUTED BY AI — nothing here executes SQL;
  * every management function is SECURITY DEFINER, hardened, owner-scoped and
    granted to `service_role` only;
  * the SERVICE validates, classifies, orders and returns METADATA only, and a
    response that carried a secret-bearing field is refused outright;
  * the TELEGRAM surface renders metadata and a bounded result, addresses a
    credential by a non-secret handle, and never puts a value into callback data;
  * a raw secret never reaches a log line, a rendered message, a callback payload
    or an ordinary database column;
  * the existing STT credential pool / fallback / probe behavior is untouched.

No provider, no credential and no recognition quality is claimed: the store is a
fake that mirrors the migration's behavior at the RPC boundary, and no request to
any provider happens except through an injected engine double.
"""
from __future__ import annotations

import ast
import logging
import pathlib
import re
from typing import Any

import pytest

from backend.ai import credential_source
from backend.services import credential_service

REPO = pathlib.Path(__file__).resolve().parent.parent
MIGRATION_NAME = "20260919000002_credential_vault_management.sql"
MIGRATION_PATH = REPO / "supabase" / "migrations" / MIGRATION_NAME
PART1_NAME = "20260919000001_create_api_credential_vault.sql"
DOC_PATH = REPO / "DATABASE_ARCHITECTURE.md"
REPORT_PATH = REPO / "IMPLEMENTATION_REPORT.md"

MIGRATION = MIGRATION_PATH.read_text(encoding="utf-8")
#: The same text with every whitespace run collapsed, so a statement wrapped across
#: lines (as the long signatures are) can still be asserted as one string.
FLAT_MIGRATION = re.sub(r"\s+", " ", MIGRATION)
DOC = DOC_PATH.read_text(encoding="utf-8")
HANDLER_PATH = REPO / "backend" / "bot" / "handlers" / "ai_credentials.py"
HANDLER_SOURCE = HANDLER_PATH.read_text(encoding="utf-8")
SERVICE_PATH = REPO / "backend" / "services" / "credential_service.py"
SERVICE_SOURCE = SERVICE_PATH.read_text(encoding="utf-8")

#: Distinctive fake secrets. Their appearance anywhere outside the fake vault is
#: unambiguous, and they contain no whitespace (the service refuses one that does).
SECRET_A = "sm-key-alpha-4c1f77b2"
SECRET_B = "sm-key-bravo-3d905aaa"
SECRET_OPENAI = "oa-key-charlie-9ab4cccc"
ALL_SECRETS = (SECRET_A, SECRET_B, SECRET_OPENAI)

OWNER = 7283627550
OTHER_OWNER = 1111111111

_MANAGEMENT_FUNCTIONS = {
    "api_credential_list": "(bigint, text)",
    "api_credential_create": "(bigint, text, text, text, integer, boolean)",
    "api_credential_replace_secret": "(bigint, text, text)",
    "api_credential_update": "(bigint, text, text, boolean, integer)",
    "api_credential_delete": "(bigint, text)",
}


# ── SQL / doc parsing helpers ───────────────────────────────────────────────


def _statements(text: str) -> list[str]:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("--")
    ]


def _sql_blocks(section: str) -> list[str]:
    return re.findall(r"```sql\n(.*?)```", section, flags=re.S)


def _part2_section() -> str:
    markers = [m for m in ("## 29.13", "### 29.13") if m in DOC]
    assert markers, "DATABASE_ARCHITECTURE.md must document the PART 2 contract in §29.13+"
    return DOC[DOC.index(markers[0]):]


def _assert_no_secret(text: str, where: str) -> None:
    for secret in ALL_SECRETS:
        assert secret not in text, f"a raw secret leaked into {where}"


# ── The fake store (the migration's behavior, at the RPC boundary) ─────────


class _FakeResponse:
    def __init__(self, data: Any) -> None:
        self.data = data


class _FakeQuery:
    def __init__(self, data: Any) -> None:
        self._data = data

    def execute(self):
        return _FakeResponse(self._data)


class _FakeDb:
    """A Supabase client whose ONLY job is to answer "no Vault credentials"."""

    def rpc(self, name, payload):  # noqa: ANN001 — mirrors supabase-py
        return _FakeQuery([])


class _FakeStore:
    """`public.api_credentials` + `vault.secrets`, behind the management RPCs.

    Mirrors the migration's validation, owner scoping, ordering, return shape and
    failure tokens — including that a METADATA read cannot return a secret, because
    only the create/replace calls are ever handed one.
    """

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.secrets: dict[str, str] = {}
        self.calls: list[tuple[str, dict]] = []
        self.failure: BaseException | None = None
        self.failure_on: str = ""
        self.leak_metadata = False
        self.list_limit = credential_service.MAX_LISTED
        self.counter = 0

    # -- plumbing ---------------------------------------------------------

    def __call__(self, name: str, payload: dict) -> Any:
        self.calls.append((name, dict(payload)))
        if self.failure is not None and (not self.failure_on or self.failure_on == name):
            error, self.failure = self.failure, None
            raise error
        return getattr(self, f"_{name}")(payload)

    def public(self, row: dict) -> dict:
        out = {
            "credential_id": row["credential_id"],
            "provider": row["provider"],
            "label": row["label"],
            "enabled": row["enabled"],
            "priority": row["priority"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        if self.leak_metadata:
            out["secret"] = self.secrets.get(row["credential_id"], "")
        return out

    def own(self, payload: dict) -> list[dict]:
        owner = payload.get("p_owner_id")
        if not owner or int(owner) <= 0:
            raise ValueError("invalid_owner")
        return [row for row in self.rows.values() if row["owner_id"] == int(owner)]

    def find(self, payload: dict) -> dict:
        wanted = str(payload.get("p_credential_id") or "")
        for row in self.own(payload):
            if row["credential_id"] == wanted:
                return row
        raise ValueError("credential_not_found")

    def _validate(self, payload: dict, *, label: Any = None, secret: Any = None) -> None:
        provider = payload.get("p_provider")
        if provider is not None:
            token = str(provider).strip()
            if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,31}", token):
                raise ValueError("invalid_provider")
        if label is not None:
            text = str(label).strip()
            if not text or len(text) > 80:
                raise ValueError("invalid_label")
        if secret is not None:
            text = str(secret)
            if not text:
                raise ValueError("empty_secret")
            if len(text) > 8192:
                raise ValueError("secret_too_long")

    # -- the five RPCs ----------------------------------------------------

    def _api_credential_list(self, payload: dict) -> list[dict]:
        rows = self.own(payload)
        provider = payload.get("p_provider")
        if provider:
            rows = [row for row in rows if row["provider"] == provider]
        rows.sort(key=lambda r: (r["provider"], r["priority"], r["created_at"], r["credential_id"]))
        return [self.public(row) for row in rows[: self.list_limit]]

    def _api_credential_create(self, payload: dict) -> list[dict]:
        self._validate(payload, label=payload.get("p_label"), secret=payload.get("p_secret"))
        self.counter += 1
        credential_id = f"c{self.counter:012x}"
        row = {
            "credential_id": credential_id,
            "provider": str(payload["p_provider"]).strip(),
            "label": str(payload["p_label"]).strip(),
            "owner_id": int(payload["p_owner_id"]),
            "enabled": bool(payload.get("p_enabled", True)),
            "priority": max(0, min(1_000_000, int(payload.get("p_priority") or 0))),
            "created_at": "2026-09-19T00:00:00+00:00",
            "updated_at": "2026-09-19T00:00:00+00:00",
        }
        self.secrets[credential_id] = str(payload["p_secret"])
        self.rows[credential_id] = row
        return [self.public(row)]

    def _api_credential_replace_secret(self, payload: dict) -> list[dict]:
        self._validate(payload, secret=payload.get("p_secret"))
        row = self.find(payload)
        self.secrets[row["credential_id"]] = str(payload["p_secret"])
        row["updated_at"] = "2026-09-19T00:00:01+00:00"
        return [self.public(row)]

    def _api_credential_update(self, payload: dict) -> list[dict]:
        self._validate(payload, label=payload.get("p_label"))
        priority = payload.get("p_priority")
        if priority is not None and not 0 <= int(priority) <= 1_000_000:
            raise ValueError("invalid_priority")
        row = self.find(payload)
        if payload.get("p_label") is not None:
            row["label"] = str(payload["p_label"]).strip()
        if payload.get("p_enabled") is not None:
            row["enabled"] = bool(payload["p_enabled"])
        if priority is not None:
            row["priority"] = int(priority)
        row["updated_at"] = "2026-09-19T00:00:02+00:00"
        return [self.public(row)]

    def _api_credential_delete(self, payload: dict) -> bool:
        try:
            row = self.find(payload)
        except ValueError:
            return False
        self.secrets.pop(row["credential_id"], None)
        self.rows.pop(row["credential_id"], None)
        return True


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _no_ambient_provider_keys(monkeypatch):
    for name in (
        "AI_GEMINI_API_KEY", "GEMINI_API_KEY", "AI_GROQ_API_KEY", "GROQ_API_KEY",
        "AI_SPEECHMATICS_API_KEY", "AI_OPENAI_API_KEY", "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def store(monkeypatch):
    fake = _FakeStore()
    monkeypatch.setattr(credential_service, "_rpc_sync", fake)
    # The store IS configured here, so the service must classify a refusal by its
    # token rather than reporting "not configured".
    monkeypatch.setattr("backend.db.client.get_db", lambda: _FakeDb())
    credential_source.reset()
    credential_service.clear_tests()
    yield fake
    credential_source.reset()
    credential_service.clear_tests()


def _patch_owner(monkeypatch, owner: int = OWNER):
    from backend.bot.handlers import ai as ai_module

    async def _owner_id():
        return owner

    monkeypatch.setattr(ai_module, "_get_owner_id", _owner_id)


@pytest.fixture
def ui(monkeypatch):
    """The Telegram surface with the owner pinned and no Telegram client."""
    from backend.bot.handlers import ai as ai_module
    from backend.helper import inline_engine

    _patch_owner(monkeypatch)
    monkeypatch.setattr(inline_engine, "_self_client", None)
    finished: list[str] = []

    async def _finish(notice, *_args, **_kwargs):
        finished.append(notice)

    monkeypatch.setattr(ai_module, "_finish_input", _finish)
    return finished


def _flatten(buttons) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for row in buttons:
        cells = row if isinstance(row, list) else [row]
        for btn in cells:
            data = getattr(btn, "data", None) or ""
            if isinstance(data, bytes):
                data = data.decode("utf-8", errors="replace")
            out.append((str(getattr(btn, "text", "") or ""), str(data)))
    return out


def _datas(buttons) -> list[str]:
    return [data for _text, data in _flatten(buttons)]


def _async_return(value):
    async def _inner(*_args, **_kwargs):
        return value

    return _inner


async def _seed(store, provider="speechmatics", label="Main", secret=SECRET_A,
                priority=0, owner=OWNER, enabled=True):
    outcome = await credential_service.create_credential(
        owner, provider, label, secret, priority=priority, enabled=enabled,
    )
    assert outcome.ok, outcome.reason
    return outcome.credential


# ══ 1. The migration ═══════════════════════════════════════════════════════


def _media_error(failure_class: str, http_status: int = 0):
    from backend.services.media_service import MediaError

    error = MediaError("bounded")
    error.failure_class = failure_class
    error.http_status = http_status
    return error


def test_migration_filename_and_location_follow_the_convention():
    assert re.fullmatch(r"\d{14}_[a-z0-9_]+\.sql", MIGRATION_NAME)
    assert MIGRATION_PATH in sorted((REPO / "supabase" / "migrations").glob("*.sql"))
    assert MIGRATION_NAME > PART1_NAME, "PART 2 must sort after the PART 1 migration"


def test_every_management_function_is_hardened_and_service_role_only():
    for name, signature in _MANAGEMENT_FUNCTIONS.items():
        reference = f"FUNCTION public.{name}{signature}"
        assert reference in FLAT_MIGRATION, name
        assert f"ALTER {reference} OWNER TO postgres;" in FLAT_MIGRATION, name
        assert (
            f"REVOKE ALL ON {reference} FROM PUBLIC, anon, authenticated;" in FLAT_MIGRATION
        ), name
        assert (
            f"GRANT EXECUTE ON {reference} TO service_role;" in FLAT_MIGRATION
        ), name
    assert FLAT_MIGRATION.count("OWNER TO postgres;") == len(_MANAGEMENT_FUNCTIONS)


def test_every_function_is_security_definer_with_an_empty_search_path():
    bodies = re.findall(r"(CREATE OR REPLACE FUNCTION .*?\$\$;)", MIGRATION, flags=re.S)
    assert len(bodies) == len(_MANAGEMENT_FUNCTIONS)
    for body in bodies:
        assert "SECURITY DEFINER" in body
        assert "SET search_path = ''" in body
        assert "SECURITY INVOKER" not in body


def test_the_migration_is_not_a_generic_sql_endpoint():
    lowered = MIGRATION.lower()
    assert "execute format" not in lowered
    assert "execute '" not in lowered
    assert "set local role" not in lowered
    assert "dynamic sql" not in lowered


def test_no_management_return_shape_can_carry_a_secret():
    """Every RETURNS TABLE lists the metadata projection — never a secret field."""
    shapes = re.findall(r"RETURNS TABLE \((.*?)\n\)", MIGRATION, flags=re.S)
    assert shapes, "the migration must declare explicit return shapes"
    for shape in shapes:
        names = [
            line.split()[0]
            for line in shape.splitlines()
            if line.strip() and not line.strip().startswith("--")
        ]
        assert names == [
            "credential_id", "provider", "label", "enabled", "priority",
            "created_at", "updated_at",
        ], names
        assert not [name for name in names if name in ("secret", "api_key", "token")]


def test_the_metadata_table_is_left_exactly_as_part_1_created_it():
    """PART 2 adds no column: the table still has nowhere to put a key."""
    assert "ALTER TABLE public.api_credentials" not in MIGRATION
    assert "ADD COLUMN" not in MIGRATION
    assert "CREATE TABLE" not in MIGRATION
    assert "DROP TABLE" not in MIGRATION


def test_only_the_two_write_functions_reach_the_secret_store():
    list_body = MIGRATION[
        MIGRATION.index("FUNCTION public.api_credential_list("):
        MIGRATION.index("FUNCTION public.api_credential_create(")
    ]
    update_body = MIGRATION[
        MIGRATION.index("FUNCTION public.api_credential_update("):
        MIGRATION.index("FUNCTION public.api_credential_delete(")
    ]
    delete_body = MIGRATION[MIGRATION.index("FUNCTION public.api_credential_delete("):]
    # A metadata read and a metadata update never touch a secret at all.
    assert "vault." not in list_body
    assert "vault." not in update_body
    # Only a WRITE of a secret may create one, and only create/replace/delete touch
    # the store. The two functions that accept a key are the only ones that do.
    assert "vault.create_secret(" not in list_body
    assert "vault.create_secret(" not in update_body
    assert "vault.create_secret(" not in delete_body
    assert "vault.create_secret(" in MIGRATION
    assert "vault.update_secret(" not in MIGRATION, (
        "the swap uses create-then-repoint, never an in-place rewrite"
    )


def test_the_migration_never_inserts_a_secret_into_the_metadata_table():
    insert = MIGRATION[MIGRATION.index("INSERT INTO public.api_credentials"):]
    insert = insert[: insert.index("RETURNING")]
    assert "p_secret" not in insert
    assert "v_secret_id" in insert, "the row must reference the Vault secret, not carry it"


def test_the_migration_does_not_seed_or_drop_anything_shared():
    assert "INSERT INTO vault" not in MIGRATION
    assert "DROP EXTENSION" not in MIGRATION
    assert "DROP TABLE" not in MIGRATION
    assert "NOTIFY pgrst, 'reload schema';" in MIGRATION


def test_the_rollback_in_the_migration_header_is_scoped():
    header = MIGRATION[: MIGRATION.index("-- ====")]
    for name, signature in _MANAGEMENT_FUNCTIONS.items():
        assert f"DROP FUNCTION IF EXISTS public.{name}{signature};" in header
    assert "DROP TABLE" not in header


# ══ 2. The documentation ═══════════════════════════════════════════════════


def test_the_documentation_documents_the_part_2_contract():
    section = _part2_section()
    for name in _MANAGEMENT_FUNCTIONS:
        assert name in section, name
    assert "NOT EXECUTED BY AI" in section
    assert "Vault" in section and "owner_id" in section


def test_the_documented_part_2_sql_is_statement_identical_to_the_migration():
    section = _part2_section()
    blocks = _sql_blocks(section[section.index("### 29.14"):])
    assert blocks, "§29.14 must contain the complete manual SQL"
    assert _statements(blocks[0]) == _statements(MIGRATION)


def test_the_documented_part_2_rollback_is_complete_and_scoped():
    section = _part2_section()
    rollback_blocks = _sql_blocks(section[section.index("### 29.15"):])
    assert rollback_blocks, "§29.15 must carry the reversal SQL"
    rollback = rollback_blocks[0]
    for name, signature in _MANAGEMENT_FUNCTIONS.items():
        assert f"DROP FUNCTION IF EXISTS public.{name}{signature};" in rollback
    assert "DROP TABLE" not in rollback
    assert "DROP EXTENSION" not in rollback
    assert "DROP SCHEMA" not in rollback


def test_the_documentation_does_not_pretend_the_sql_was_executed():
    section = _part2_section()
    assert "NOT EXECUTED BY AI" in section
    for secret in ALL_SECRETS:
        assert secret not in DOC


def test_the_migration_status_table_records_the_part_2_migration():
    assert MIGRATION_NAME in DOC


# ══ 3. Application source guarantees ══════════════════════════════════════


#: Statement-level SQL and direct secret-store access, as they would actually be
#: WRITTEN. Plain substrings like "vault." would match prose, so these are checked
#: as statements instead, which is what "no SQL in this layer" really means.
_SQL_OR_VAULT = (
    r"(?i)\binsert\s+into\b",
    r"(?i)\bdelete\s+from\b",
    r"(?i)\bupdate\s+public\.",
    r"(?i)\bcreate\s+table\b",
    r"(?i)\balter\s+table\b",
    r"(?i)\bdrop\s+table\b",
    r"(?i)\bselect\s+\*\b",
    r"(?i)from\s+public\.api_credentials\b",
    r"vault\.create_secret\(",
    r"vault\.update_secret\(",
    r"vault\.secrets\b",
    r"vault\.decrypted_secrets\b",
    r"\.table\(",
)


def _code_only(source: str) -> str:
    """The module's EXECUTABLE code: docstrings and comments removed.

    A docstring is allowed to NAME the store it must not touch; only real code is
    checked, which is what "this layer never runs SQL" actually means.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            first = body[0] if body else None
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                body.pop(0)
    return ast.unparse(tree)


def _assert_no_sql_or_secret_store(source: str, where: str) -> None:
    code = _code_only(source)
    for pattern in _SQL_OR_VAULT:
        assert not re.search(pattern, code), f"{where} must not contain {pattern}"
    return code


def test_the_handler_never_touches_the_database_or_the_secret_store():
    code = _assert_no_sql_or_secret_store(HANDLER_SOURCE, "the handler")
    assert ".rpc(" not in code
    assert "backend.db" not in code
    assert "supabase" not in code.lower()


def test_the_service_never_executes_sql_and_never_reads_the_store_directly():
    code = _assert_no_sql_or_secret_store(SERVICE_SOURCE, "the service")
    assert "db.rpc(" in code, "the store is reached through an RPC only"
    assert ".execute()" in code


def test_no_second_credential_store_module_exists():
    """There is ONE secret architecture; the capability pools are not stores.

    The Text-to-Speech phase adds its own POOL (order, health, classification) but
    no second STORE: the pool must read the one secret boundary
    (``backend/ai/credential_source.py``, and through it the one Vault RPC) and
    must not reach a database, a Vault schema or a secret itself.
    """
    services = REPO / "backend" / "services"
    ai_dir = REPO / "backend" / "ai"
    assert not (services / "tts_credentials.py").exists()
    assert not (services / "stt_credentials.py").exists()
    assert not (services / "credential_vault.py").exists()
    assert not (ai_dir / "tts_credential_source.py").exists()

    pool = (services / "tts_credential_pool.py").read_text(encoding="utf-8")
    assert "credential_source" in pool, "the TTS pool must reuse the one secret boundary"
    for forbidden in ("db.rpc(", "get_db(", "vault.", "decrypted_secrets", "create_secret"):
        assert forbidden not in pool, f"the TTS pool must not own storage: {forbidden}"

    source = (ai_dir / "credential_source.py").read_text(encoding="utf-8")
    assert source.count("\nVAULT_RPC = ") == 1, "there is exactly ONE Vault RPC contract"


def test_the_new_surface_is_wired_into_the_one_router():
    from backend.bot import router as router_module

    source = pathlib.Path(router_module.__file__).read_text(encoding="utf-8")
    assert "ai_credentials" in source
    assert "ai_credentials.register(client, owner_id)" in source


# ══ 4. Provider discovery ══════════════════════════════════════════════════


def test_registered_providers_are_derived_from_the_existing_registries():
    providers = credential_service.registered_providers()
    assert providers[:3] == ("gemini", "groq", "speechmatics")
    assert "openai" in providers, "the TTS provider shares the same credential store"
    assert len(providers) == len(set(providers))


def test_an_imaginary_provider_is_never_offered_or_accepted(store):
    assert not credential_service.is_registered_provider("skynet")
    assert credential_service.provider_label("skynet") == "Skynet"


@pytest.mark.asyncio
async def test_creating_a_credential_for_an_unregistered_provider_is_refused(store):
    outcome = await credential_service.create_credential(OWNER, "skynet", "X", SECRET_A)
    assert not outcome.ok
    assert outcome.reason == credential_service.REASON_INVALID_PROVIDER
    assert store.rows == {}, "nothing reached the store"


def test_only_providers_with_a_runtime_adapter_are_testable():
    assert credential_service.provider_testable("speechmatics")
    assert credential_service.provider_testable("groq")
    assert not credential_service.provider_testable("openai"), (
        "no bounded credential test exists for the TTS provider yet"
    )


# ══ 5. The service: create / list / update / delete ═══════════════════════


@pytest.mark.asyncio
async def test_a_created_credential_is_returned_as_metadata_only(store):
    item = await _seed(store)
    assert item.credential_id.startswith("c")
    assert item.provider == "speechmatics"
    assert item.label == "Main"
    assert item.enabled is True
    assert store.secrets[item.credential_id] == SECRET_A
    _assert_no_secret(repr(item), "the create outcome")


@pytest.mark.asyncio
async def test_listing_returns_the_owners_metadata_in_the_store_order(store):
    await _seed(store, label="Second", priority=2)
    await _seed(store, label="First", priority=1)
    result = await credential_service.list_credentials(OWNER, "speechmatics")
    assert result.ok
    assert [item.label for item in result.credentials] == ["First", "Second"]
    _assert_no_secret(repr(result), "a listing")


@pytest.mark.asyncio
async def test_the_store_is_never_asked_for_a_secret_on_a_read(store):
    item = await _seed(store)
    await credential_service.list_credentials(OWNER)
    await credential_service.update_metadata(OWNER, item.credential_id, enabled=False)
    await credential_service.resolve_handle(OWNER, item.handle)
    for name, payload in store.calls:
        if name in (credential_service.RPC_LIST, credential_service.RPC_UPDATE,
                    credential_service.RPC_DELETE):
            assert "p_secret" not in payload, name


@pytest.mark.asyncio
async def test_owner_scoping_hides_another_owners_credential(store):
    mine = await _seed(store, owner=OWNER)
    theirs = await _seed(store, owner=OTHER_OWNER, label="Theirs")

    listed = await credential_service.list_credentials(OTHER_OWNER)
    assert [item.credential_id for item in listed.credentials] == [theirs.credential_id]
    assert await credential_service.resolve_handle(OTHER_OWNER, mine.handle) is None
    assert await credential_service.credential(OTHER_OWNER, mine.credential_id) is None

    outcome = await credential_service.update_metadata(
        OTHER_OWNER, mine.credential_id, enabled=False,
    )
    assert not outcome.ok
    assert outcome.reason == credential_service.REASON_NOT_FOUND
    assert store.rows[mine.credential_id]["enabled"] is True, "the other owner's row is intact"


@pytest.mark.asyncio
async def test_a_handle_is_short_deterministic_and_derived_from_the_public_id():
    first = credential_service.handle_for("c0a1b2c3d4e5")
    assert first == credential_service.handle_for("c0a1b2c3d4e5")
    assert len(first) == credential_service.HANDLE_LENGTH
    assert first != credential_service.handle_for("c0a1b2c3d4e6")
    assert re.fullmatch(r"[0-9a-f]+", first)


@pytest.mark.asyncio
async def test_update_touches_metadata_only(store):
    item = await _seed(store)
    outcome = await credential_service.update_metadata(
        OWNER, item.credential_id, label="Renamed", enabled=False, priority=7,
    )
    assert outcome.ok and outcome.credential is not None
    assert outcome.credential.label == "Renamed"
    assert outcome.credential.enabled is False
    assert outcome.credential.priority == 7
    assert store.secrets[item.credential_id] == SECRET_A, "the key was not rotated"


@pytest.mark.asyncio
async def test_update_refuses_an_empty_change(store):
    item = await _seed(store)
    outcome = await credential_service.update_metadata(OWNER, item.credential_id)
    assert not outcome.ok
    assert outcome.reason == credential_service.REASON_REJECTED


@pytest.mark.asyncio
async def test_replacing_the_secret_never_returns_a_key(store):
    item = await _seed(store)
    outcome = await credential_service.replace_secret(OWNER, item.credential_id, SECRET_B)
    assert outcome.ok
    assert store.secrets[item.credential_id] == SECRET_B
    assert store.secrets[item.credential_id] != SECRET_A
    _assert_no_secret(repr(outcome), "a replace outcome")
    assert SECRET_A not in repr(store.rows[item.credential_id])


@pytest.mark.asyncio
async def test_delete_removes_the_secret_and_the_metadata(store):
    item = await _seed(store)
    outcome = await credential_service.delete_credential(OWNER, item.credential_id)
    assert outcome.ok
    assert item.credential_id not in store.secrets
    assert item.credential_id not in store.rows


@pytest.mark.asyncio
async def test_delete_reports_a_missing_credential_instead_of_succeeding(store):
    outcome = await credential_service.delete_credential(OWNER, "cnotthere")
    assert not outcome.ok
    assert outcome.reason == credential_service.REASON_NOT_FOUND


# ══ 6. Validation and the bounded reason vocabulary ═══════════════════════


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "expected"),
    (
        ({"provider": "speechmatics", "label": "", "secret": SECRET_A},
         credential_service.REASON_INVALID_LABEL),
        ({"provider": "speechmatics", "label": "x" * 81, "secret": SECRET_A},
         credential_service.REASON_INVALID_LABEL),
        ({"provider": "Speechmatics", "label": "Main", "secret": SECRET_A},
         credential_service.REASON_INVALID_PROVIDER),
        ({"provider": "speechmatics", "label": "Main", "secret": ""},
         credential_service.REASON_EMPTY_SECRET),
        ({"provider": "speechmatics", "label": "Main", "secret": "x" * 8193},
         credential_service.REASON_SECRET_TOO_LONG),
        ({"provider": "speechmatics", "label": "Main", "secret": "sm key with spaces"},
         credential_service.REASON_SECRET_SHAPE),
        ({"provider": "speechmatics", "label": "Main", "secret": "sm-key\nbroken"},
         credential_service.REASON_SECRET_SHAPE),
    ),
)
async def test_create_refuses_an_invalid_request_with_a_bounded_reason(store, kwargs, expected):
    outcome = await credential_service.create_credential(OWNER, **kwargs)
    assert not outcome.ok
    assert outcome.reason == expected
    assert store.rows == {}, "nothing reached the store"
    assert outcome.reason_label and "None" not in outcome.reason_label


@pytest.mark.asyncio
async def test_a_whitespace_secret_fails_closed_so_an_ordinary_message_cannot_become_a_key(store):
    """A pasted key is one token; a normal chat message is refused outright."""
    outcome = await credential_service.create_credential(
        OWNER, "speechmatics", "Main", "سلام حال شما چطوره",
    )
    assert not outcome.ok
    assert outcome.reason == credential_service.REASON_SECRET_SHAPE
    assert store.secrets == {}


@pytest.mark.asyncio
async def test_priority_is_bounded_by_the_store_contract(store):
    item = await _seed(store)
    for bad in (-1, 1_000_001):
        outcome = await credential_service.update_metadata(OWNER, item.credential_id, priority=bad)
        assert not outcome.ok
        assert outcome.reason == credential_service.REASON_INVALID_PRIORITY


@pytest.mark.asyncio
async def test_an_invalid_owner_is_refused_before_any_call(store):
    for owner in (0, -5, "nope"):
        listed = await credential_service.list_credentials(owner)
        assert not listed.ok
        assert listed.reason == credential_service.REASON_INVALID_OWNER
    assert store.calls == []


def test_every_reason_has_owner_facing_wording():
    reasons = (
        credential_service.REASON_NOT_CONFIGURED, credential_service.REASON_UNAVAILABLE,
        credential_service.REASON_INVALID_OWNER, credential_service.REASON_INVALID_PROVIDER,
        credential_service.REASON_INVALID_LABEL, credential_service.REASON_INVALID_PRIORITY,
        credential_service.REASON_INVALID_CREDENTIAL, credential_service.REASON_EMPTY_SECRET,
        credential_service.REASON_SECRET_TOO_LONG, credential_service.REASON_SECRET_SHAPE,
        credential_service.REASON_NOT_FOUND, credential_service.REASON_REJECTED,
        credential_service.REASON_FAILED,
    )
    for reason in reasons:
        assert reason in credential_service.REASON_LABELS, reason


# ══ 7. Store failures ═════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_a_store_that_refuses_a_statement_is_classified_not_echoed(store, caplog):
    store.failure = ValueError("credential_not_found")
    with caplog.at_level(logging.DEBUG):
        outcome = await credential_service.create_credential(OWNER, "speechmatics", "Main", SECRET_A)
    assert not outcome.ok
    assert outcome.reason == credential_service.REASON_NOT_FOUND
    _assert_no_secret(caplog.text, "a failed create")


@pytest.mark.asyncio
async def test_an_unknown_store_failure_is_a_bounded_failure_not_a_crash(store):
    store.failure = RuntimeError("something the service has never seen")
    outcome = await credential_service.create_credential(OWNER, "speechmatics", "Main", SECRET_A)
    assert not outcome.ok
    assert outcome.reason == credential_service.REASON_FAILED


@pytest.mark.asyncio
async def test_a_missing_management_rpc_is_reported_as_not_configured(store):
    """Before the PART 2 SQL is applied the surface says so instead of failing."""
    store.failure = RuntimeError(
        '{"code":"PGRST202","message":"Could not find the function public.api_credential_list"}'
    )
    listed = await credential_service.list_credentials(OWNER)
    assert not listed.ok
    assert listed.reason == credential_service.REASON_NOT_CONFIGURED


@pytest.mark.asyncio
async def test_an_unconfigured_database_is_reported_as_not_configured(monkeypatch):
    monkeypatch.setattr("backend.db.client.get_db", lambda: None)
    listed = await credential_service.list_credentials(OWNER)
    assert not listed.ok
    assert listed.reason == credential_service.REASON_NOT_CONFIGURED
    outcome = await credential_service.delete_credential(OWNER, "c1")
    assert not outcome.ok
    assert outcome.reason == credential_service.REASON_NOT_CONFIGURED


@pytest.mark.asyncio
async def test_a_malformed_metadata_response_is_refused(store, monkeypatch):
    monkeypatch.setattr(
        credential_service, "_rpc_sync",
        lambda name, payload: [{"credential_id": "c1"}],
    )
    listed = await credential_service.list_credentials(OWNER)
    assert listed.ok and listed.credentials == (), "an unusable row contributes nothing"


@pytest.mark.asyncio
async def test_a_response_carrying_a_secret_bearing_field_is_refused_outright(store):
    """Fail-closed: a schema change that started returning a key is a refusal."""
    await _seed(store)
    store.leak_metadata = True
    listed = await credential_service.list_credentials(OWNER)
    assert not listed.ok
    assert listed.reason == credential_service.REASON_FAILED
    assert listed.credentials == ()


@pytest.mark.asyncio
async def test_a_non_mapping_row_contributes_nothing(monkeypatch):
    monkeypatch.setattr("backend.db.client.get_db", lambda: _FakeDb())
    monkeypatch.setattr(credential_service, "_rpc_sync", lambda name, payload: ["nope", 42])
    listed = await credential_service.list_credentials(OWNER)
    assert listed.ok and listed.credentials == ()


@pytest.mark.asyncio
async def test_the_listing_is_bounded(store):
    for index in range(credential_service.MAX_LISTED + 5):
        await _seed(store, label=f"Key {index}", secret=f"sm-key-{index}")
    listed = await credential_service.list_credentials(OWNER)
    assert listed.ok
    assert len(listed.credentials) <= credential_service.MAX_LISTED
    assert store.list_limit == credential_service.MAX_LISTED


# ══ 8. Secrets never reach a log, a payload or a report ══════════════════


@pytest.mark.asyncio
async def test_no_secret_reaches_a_log_line_on_the_happy_path(store, caplog):
    with caplog.at_level(logging.DEBUG):
        item = await _seed(store, secret=SECRET_A)
        await credential_service.replace_secret(OWNER, item.credential_id, SECRET_B)
        await credential_service.delete_credential(OWNER, item.credential_id)
    _assert_no_secret(caplog.text, "the log output of a full lifecycle")


@pytest.mark.asyncio
async def test_no_secret_reaches_a_log_line_on_failure(store, caplog):
    store.failure = RuntimeError("refused")
    with caplog.at_level(logging.DEBUG):
        await credential_service.create_credential(OWNER, "speechmatics", "Main", SECRET_A)
        await credential_service.replace_secret(OWNER, "c1", SECRET_B)
    _assert_no_secret(caplog.text, "the log output of a failed write")


@pytest.mark.asyncio
async def test_the_secret_crosses_into_the_store_and_nowhere_else(store):
    item = await _seed(store, secret=SECRET_OPENAI, provider="openai", label="Speech")
    assert store.secrets[item.credential_id] == SECRET_OPENAI
    # The metadata row the store keeps cannot hold it: the projection has no field.
    assert SECRET_OPENAI not in repr(store.rows[item.credential_id])
    assert SECRET_OPENAI not in repr(store.public(store.rows[item.credential_id]))


def test_no_secret_literal_appears_in_the_repository_changes():
    _assert_no_secret(HANDLER_SOURCE, "the handler source")
    _assert_no_secret(SERVICE_SOURCE, "the service source")
    _assert_no_secret(MIGRATION, "the migration")


# ══ 9. The Telegram surface: panels ══════════════════════════════════════


def test_the_surface_registers_under_media_analysis_with_the_shared_registry(monkeypatch):
    from backend.bot.handlers import ai_credentials as module

    panels: list[tuple[str, str, str]] = []
    actions: list[str] = []
    monkeypatch.setattr(
        module, "register_panel",
        lambda panel_id, handler, parent="menu", title="": panels.append((panel_id, parent, title)),
    )
    monkeypatch.setattr(module, "register_inline_builder", lambda *a, **k: None)
    monkeypatch.setattr(module, "register_action", lambda action_id, handler: actions.append(action_id))
    monkeypatch.setattr(module, "register_input", lambda *a, **k: None)

    module.register(None, OWNER)

    assert ("ai_cred", "ai_media", "API Credentials") in panels
    assert ("ai_cred_prov", "ai_cred", "Provider credentials") in panels
    assert ("ai_cred_one", "ai_cred_prov", "Credential") in panels
    assert set(actions) == {
        "ai_cred_toggle", "ai_cred_up", "ai_cred_down",
        "ai_cred_test", "ai_cred_delete", "ai_cred_delete_yes",
    }


@pytest.mark.asyncio
async def test_the_media_analysis_hub_offers_the_credentials_row(ui, store, monkeypatch):
    from backend.bot.handlers import ai as ai_module
    from backend.bot.handlers import ai_stt_settings as hub

    config = {"provider": "gemini", "model": "gemini-2.5-flash", "is_configured": True}
    monkeypatch.setattr(ai_module, "_get_saved_config", _async_return(dict(config)))
    title, body, buttons = await hub._ai_media_panel_handler(None, "")

    assert title == "Media Analysis"
    assert "panel:ai_cred" in _datas(buttons)
    assert "API Credentials · no managed keys" in body


@pytest.mark.asyncio
async def test_the_hub_reports_an_unavailable_store_honestly(monkeypatch):
    from backend.bot.handlers import ai_credentials as module

    monkeypatch.setattr("backend.db.client.get_db", lambda: None)
    assert await module.hub_line(OWNER) == "API Credentials · store not available"


@pytest.mark.asyncio
async def test_the_root_panel_lists_registered_providers_only(ui, store):
    from backend.bot.handlers import ai_credentials as module

    title, body, buttons = await module._credentials_root(OWNER)
    datas = _datas(buttons)
    assert title == "API Credentials"
    for provider in credential_service.registered_providers():
        assert f"panel:ai_cred_prov:{provider}" in datas
    assert "Speechmatics · no managed keys" in body


@pytest.mark.asyncio
async def test_the_provider_panel_lists_credentials_and_the_add_flow(ui, store):
    from backend.bot.handlers import ai_credentials as module

    item = await _seed(store, label="Main")
    await _seed(store, label="Spare", secret=SECRET_B, priority=1, enabled=False)

    title, body, buttons = await module._provider_panel(OWNER, "speechmatics")
    datas = _datas(buttons)
    assert title == "Speechmatics"
    assert "Managed keys · 1 enabled of 2" in body
    assert "Main — Enabled · priority 0 · not tested in this session" in body
    assert "Spare — Disabled · priority 1 · not tested in this session" in body
    assert "Deployment key · not present" in body
    assert f"panel:ai_cred_one:{item.handle}" in datas
    assert "input:ai_cred:secret_add_speechmatics" in datas


@pytest.mark.asyncio
async def test_the_provider_panel_reports_a_deployment_key_without_ever_naming_it(ui, store, monkeypatch):
    from backend.bot.handlers import ai_credentials as module

    monkeypatch.setenv("AI_SPEECHMATICS_API_KEY", SECRET_A)
    _title, body, _buttons = await module._provider_panel(OWNER, "speechmatics")
    assert "Deployment key · present" in body
    assert "AI_SPEECHMATICS_API_KEY" not in body
    _assert_no_secret(body, "the provider panel")


@pytest.mark.asyncio
async def test_the_credential_panel_offers_every_bounded_action(ui, store):
    from backend.bot.handlers import ai_credentials as module

    item = await _seed(store)
    _title, body, buttons = await module._credential_panel(OWNER, item.handle)
    datas = _datas(buttons)
    assert f"action:ai_cred_toggle:{item.handle}" in datas
    assert f"action:ai_cred_up:{item.handle}" in datas
    assert f"action:ai_cred_down:{item.handle}" in datas
    assert f"action:ai_cred_test:{item.handle}" in datas
    assert f"action:ai_cred_delete:{item.handle}" in datas
    assert f"input:ai_cred:secret_replace_{item.handle}" in datas
    assert f"input:ai_cred:label_{item.handle}" in datas
    assert f"input:ai_cred:priority_{item.handle}" in datas
    assert f"Id · {item.credential_id}" in body


@pytest.mark.asyncio
async def test_callback_payloads_carry_a_handle_never_a_value(ui, store):
    from backend.bot.handlers import ai_credentials as module

    item = await _seed(store)
    for _title, _body, buttons in (
        await module._credentials_root(OWNER),
        await module._provider_panel(OWNER, "speechmatics"),
        await module._credential_panel(OWNER, item.handle),
    ):
        for _text, data in _flatten(buttons):
            _assert_no_secret(data, "a callback payload")
            assert len(data.encode("utf-8")) <= 64, data


@pytest.mark.asyncio
async def test_nothing_rendered_ever_contains_the_key(ui, store):
    from backend.bot.handlers import ai_credentials as module

    item = await _seed(store, secret=SECRET_A)
    for _title, body, buttons in (
        await module._credentials_root(OWNER),
        await module._provider_panel(OWNER, "speechmatics"),
        await module._credential_panel(OWNER, item.handle),
        await module._provider_with_notice("speechmatics", "✓ Key added"),
    ):
        _assert_no_secret(body, "a rendered panel body")
        for text, _data in _flatten(buttons):
            _assert_no_secret(text, "a rendered button label")


@pytest.mark.asyncio
async def test_an_unknown_provider_or_handle_renders_an_honest_notice(ui, store):
    from backend.bot.handlers import ai_credentials as module

    _title, body, buttons = await module._ai_cred_prov_panel_handler(None, "skynet")
    assert "Unknown provider" in body
    assert "panel:ai_cred_prov:skynet" not in _datas(buttons)

    _title, body, _buttons = await module._credential_panel(OWNER, "deadbeef1234")
    assert "not there any more" in body


# ══ 10. The Telegram surface: actions ════════════════════════════════════


@pytest.mark.asyncio
async def test_toggle_disables_and_enables_without_touching_the_key(ui, store):
    from backend.bot.handlers import ai_credentials as module

    item = await _seed(store)
    _title, body, _buttons = await module._ai_cred_toggle_action(None, item.handle, 1)
    assert "disabled" in body
    assert store.rows[item.credential_id]["enabled"] is False
    assert store.secrets[item.credential_id] == SECRET_A

    _title, body, _buttons = await module._ai_cred_toggle_action(None, item.handle, 1)
    assert "enabled" in body
    assert store.rows[item.credential_id]["enabled"] is True


@pytest.mark.asyncio
async def test_the_mutating_actions_refresh_the_relevant_credential_pool(ui, store, monkeypatch):
    from backend.bot.handlers import ai_credentials as module

    refreshed: list[str] = []

    async def _refresh(provider):
        refreshed.append(provider)
        return 1

    monkeypatch.setattr(credential_service, "refresh_provider", _refresh)
    item = await _seed(store)
    await module._ai_cred_toggle_action(None, item.handle, 1)
    await module._ai_cred_delete_yes_action(None, item.handle, 1)
    assert refreshed == ["speechmatics", "speechmatics"]


@pytest.mark.asyncio
async def test_priority_moves_one_deterministic_step_and_stops_at_the_bound(ui, store):
    from backend.bot.handlers import ai_credentials as module

    item = await _seed(store, priority=1)
    _title, body, _buttons = await module._ai_cred_up_action(None, item.handle, 1)
    assert "priority 0" in body
    assert store.rows[item.credential_id]["priority"] == 0

    _title, body, _buttons = await module._ai_cred_up_action(None, item.handle, 1)
    assert "Already first" in body
    assert store.rows[item.credential_id]["priority"] == 0

    _title, body, _buttons = await module._ai_cred_down_action(None, item.handle, 1)
    assert store.rows[item.credential_id]["priority"] == 1


@pytest.mark.asyncio
async def test_delete_asks_first_and_then_removes_both_the_row_and_the_key(ui, store):
    from backend.bot.handlers import ai_credentials as module

    item = await _seed(store)
    _title, body, buttons = await module._ai_cred_delete_action(None, item.handle, 1)
    assert "Delete credential" in body
    assert f"action:ai_cred_delete_yes:{item.handle}" in _datas(buttons)
    assert item.credential_id in store.rows, "the confirmation deletes nothing"

    _title, body, _buttons = await module._ai_cred_delete_yes_action(None, item.handle, 1)
    assert "Deleted" in body
    assert item.credential_id not in store.rows
    assert item.credential_id not in store.secrets


@pytest.mark.asyncio
async def test_a_delete_whose_store_cleanup_failed_is_not_reported_as_success(ui, store):
    from backend.bot.handlers import ai_credentials as module

    item = await _seed(store)
    store.failure = RuntimeError('{"code":"42501","message":"permission denied"}')
    store.failure_on = credential_service.RPC_DELETE
    _title, body, _buttons = await module._ai_cred_delete_yes_action(None, item.handle, 1)
    assert "Nothing was deleted" in body
    assert item.credential_id in store.rows
    assert item.credential_id in store.secrets


@pytest.mark.asyncio
async def test_an_action_on_a_dead_handle_changes_nothing(ui, store):
    from backend.bot.handlers import ai_credentials as module

    await _seed(store)
    before = dict(store.rows)
    for action in (module._ai_cred_toggle_action, module._ai_cred_up_action,
                   module._ai_cred_down_action, module._ai_cred_delete_yes_action):
        _title, body, _buttons = await action(None, "deadbeef0000", 1)
        assert "gone" in body or "not there any more" in body
    assert store.rows == before


# ══ 11. The Telegram surface: inputs and the secret lifecycle ════════════


@pytest.mark.asyncio
async def test_the_add_flow_stores_the_key_labels_it_and_deletes_the_message(ui, store, monkeypatch):
    from backend.bot.handlers import ai_credentials as module
    from backend.helper import inline_engine

    deleted: list[tuple] = []

    class _Client:
        async def delete_messages(self, chat_id, ids):
            deleted.append((chat_id, tuple(ids)))

    monkeypatch.setattr(inline_engine, "_self_client", _Client())
    await _seed(store, label="Main")

    handler = module._make_secret_add_handler("speechmatics")
    await handler(SECRET_B, 555, 777, 555, 999)

    stored = [row for row in store.rows.values() if row["label"] == "Speechmatics 2"]
    assert len(stored) == 1
    assert store.secrets[stored[0]["credential_id"]] == SECRET_B
    assert deleted == [(555, (777,))], "the key message is deleted"
    assert ui, "the panel was restored through the shared input completion path"
    _assert_no_secret("".join(ui), "an input completion notice")


@pytest.mark.asyncio
async def test_a_failed_deletion_is_reported_instead_of_hidden(ui, store, monkeypatch):
    from backend.bot.handlers import ai_credentials as module
    from backend.helper import inline_engine

    class _Client:
        async def delete_messages(self, chat_id, ids):
            raise RuntimeError("no permission")

    monkeypatch.setattr(inline_engine, "_self_client", _Client())
    handler = module._make_secret_add_handler("speechmatics")
    await handler(SECRET_A, 555, 777, 555, 999)
    assert any("could not be deleted" in notice for notice in ui)
    _assert_no_secret("".join(ui), "a deletion-failure notice")


@pytest.mark.asyncio
async def test_the_add_flow_refuses_an_unusable_reply_and_deletes_it_anyway(ui, store, monkeypatch):
    from backend.bot.handlers import ai_credentials as module
    from backend.helper import inline_engine

    deleted: list[tuple] = []

    class _Client:
        async def delete_messages(self, chat_id, ids):
            deleted.append((chat_id, tuple(ids)))

    monkeypatch.setattr(inline_engine, "_self_client", _Client())
    handler = module._make_secret_add_handler("speechmatics")

    await handler("   ", 1, 2, 1, 3)
    await handler("a b c", 1, 4, 1, 5)
    assert store.rows == {}, "nothing was stored"
    assert deleted == [(1, (2,)), (1, (4,))], "both replies were removed"
    assert any("Nothing was stored" in notice for notice in ui)
    _assert_no_secret("".join(ui), "a refusal notice")


@pytest.mark.asyncio
async def test_the_replace_flow_swaps_the_key_and_keeps_the_metadata(ui, store):
    from backend.bot.handlers import ai_credentials as module

    item = await _seed(store, label="Main", priority=3)
    handler = module._make_secret_replace_handler(item.credential_id)
    await handler(SECRET_B, 1, 2, 1, 3)

    assert store.secrets[item.credential_id] == SECRET_B
    assert store.rows[item.credential_id]["label"] == "Main"
    assert store.rows[item.credential_id]["priority"] == 3
    assert any("Key replaced" in notice for notice in ui)
    _assert_no_secret("".join(ui), "a replace notice")


@pytest.mark.asyncio
async def test_the_replace_flow_reports_a_store_failure_without_claiming_success(ui, store):
    from backend.bot.handlers import ai_credentials as module

    item = await _seed(store)
    store.failure = RuntimeError("credential_not_found")
    store.failure_on = credential_service.RPC_REPLACE
    handler = module._make_secret_replace_handler(item.credential_id)
    await handler(SECRET_B, 1, 2, 1, 3)

    assert store.secrets[item.credential_id] == SECRET_A
    assert any("Nothing was changed" in notice for notice in ui)
    _assert_no_secret("".join(ui), "a failed replace notice")


@pytest.mark.asyncio
async def test_the_rename_input_validates_and_never_rotates_the_key(ui, store):
    from backend.bot.handlers import ai_credentials as module

    item = await _seed(store, label="Main")
    handler = module._make_label_handler(item.credential_id)
    await handler("Renamed", 1, 2, 1, 3)
    assert store.rows[item.credential_id]["label"] == "Renamed"
    assert store.secrets[item.credential_id] == SECRET_A
    assert any("Renamed to Renamed" in notice for notice in ui)

    await handler("x" * 200, 1, 4, 1, 5)
    assert store.rows[item.credential_id]["label"] == "Renamed", "an over-long label is refused"


@pytest.mark.asyncio
async def test_the_priority_input_refuses_a_non_integer_and_applies_a_valid_one(ui, store):
    from backend.bot.handlers import ai_credentials as module

    item = await _seed(store, priority=0)
    handler = module._make_priority_handler(item.credential_id)
    await handler("soon", 1, 2, 1, 3)
    assert store.rows[item.credential_id]["priority"] == 0
    assert any("whole number" in notice for notice in ui)

    await handler(" 4 ", 1, 4, 1, 5)
    assert store.rows[item.credential_id]["priority"] == 4


@pytest.mark.asyncio
async def test_an_input_handler_whose_credential_vanished_says_so(ui, store):
    from backend.bot.handlers import ai_credentials as module

    handler = module._make_label_handler("cgone")
    await handler("Whatever", 1, 2, 1, 3)
    assert any("gone" in notice for notice in ui)
    assert store.rows == {}


# ══ 12. The credential test ══════════════════════════════════════════════


class _Engine:
    def __init__(self, error: BaseException | None = None, text: str = "ok") -> None:
        self._error = error
        self._text = text

    def transcribe(self, audio: bytes) -> str:
        if self._error is not None:
            raise self._error
        return self._text


def _patch_engine(monkeypatch, engine):
    from backend.ai import stt_provider_probe
    from backend.services import stt_engine_factory

    def _build(candidate, credential, **_kwargs):
        assert credential is not None, "the test must use the credential it was given"
        return engine, ""

    monkeypatch.setattr(stt_engine_factory, "build_engine_with_credential", _build)
    monkeypatch.setattr(stt_provider_probe, "test_audio_payload", lambda *_a, **_k: b"RIFF")


@pytest.fixture
def pool(monkeypatch):
    """A pool whose single Vault credential is the one the service will test."""
    from backend.services import stt_credential_pool

    state: dict[str, Any] = {"provider": "speechmatics", "credential_id": ""}

    def _records(provider):
        if provider != state["provider"] or not state["credential_id"]:
            return ()
        return (
            credential_source.CredentialRecord(
                credential_id=f"vault:{state['credential_id']}",
                provider=provider,
                secret=SECRET_A,
                source=credential_source.SOURCE_VAULT,
                priority=0,
                order_index=1,
            ),
        )

    async def _refresh(provider):
        return 1

    monkeypatch.setattr(stt_credential_pool, "credentials_for", _records)
    monkeypatch.setattr(credential_service, "refresh_provider", _refresh)
    return state


@pytest.mark.asyncio
async def test_a_test_that_completes_reports_passed_for_the_credential_it_was_given(ui, store, pool, monkeypatch):
    item = await _seed(store)
    pool["credential_id"] = item.credential_id
    _patch_engine(monkeypatch, _Engine())

    result = await credential_service.test_credential(OWNER, item.credential_id)
    assert result.passed
    assert result.credential_id == item.credential_id
    assert result.provider == "speechmatics"
    assert result.latency_ms >= 0
    assert credential_service.last_test_state(item.credential_id) == credential_service.TEST_PASSED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    (
        (_media_error("auth", http_status=401), credential_service.TEST_UNAUTHORIZED),
        (_media_error("forbidden", http_status=403), credential_service.TEST_UNAUTHORIZED),
        (_media_error("rate_limit", http_status=429), credential_service.TEST_RATE_LIMITED),
        (_media_error("timeout"), credential_service.TEST_TIMEOUT),
        (_media_error("transport"), credential_service.TEST_UNAVAILABLE),
        (_media_error("server"), credential_service.TEST_UNAVAILABLE),
        (_media_error("http_rejection", http_status=400), credential_service.TEST_FAILED),
    ),
)
async def test_a_failure_is_classified_from_the_adapters_own_vocabulary(
    ui, store, pool, monkeypatch, error, expected,
):
    item = await _seed(store)
    pool["credential_id"] = item.credential_id
    _patch_engine(monkeypatch, _Engine(error=error))

    result = await credential_service.test_credential(OWNER, item.credential_id)
    assert result.state == expected
    assert result.state_label in credential_service.TEST_STATE_LABELS.values()
    _assert_no_secret(repr(result), "a test result")


@pytest.mark.asyncio
async def test_a_test_never_marks_the_provider_probe_state(ui, store, pool, monkeypatch):
    """A credential observation is not provider health, and is not written as one."""
    from backend.ai import stt_provider_probe

    stt_provider_probe.clear_results()
    item = await _seed(store)
    pool["credential_id"] = item.credential_id
    _patch_engine(monkeypatch, _Engine(error=_media_error("auth", http_status=401)))

    await credential_service.test_credential(OWNER, item.credential_id)
    assert stt_provider_probe.result_state("speechmatics:standard") == "not_tested"


@pytest.mark.asyncio
async def test_a_disabled_credential_is_not_tested(ui, store, pool):
    item = await _seed(store, enabled=False)
    result = await credential_service.test_credential(OWNER, item.credential_id)
    assert result.state == credential_service.TEST_DISABLED


@pytest.mark.asyncio
async def test_a_credential_the_store_cannot_resolve_is_not_reported_as_healthy(ui, store):
    item = await _seed(store)
    result = await credential_service.test_credential(OWNER, item.credential_id)
    assert result.state == credential_service.TEST_NOT_FOUND
    assert not result.passed


@pytest.mark.asyncio
async def test_a_provider_without_a_test_path_says_not_supported(ui, store):
    item = await _seed(store, provider="openai", label="Speech")
    result = await credential_service.test_credential(OWNER, item.credential_id)
    assert result.state == credential_service.TEST_NOT_SUPPORTED


@pytest.mark.asyncio
async def test_an_unknown_credential_is_not_tested(ui, store):
    result = await credential_service.test_credential(OWNER, "cnotmine")
    assert result.state == credential_service.TEST_NOT_FOUND


@pytest.mark.asyncio
async def test_the_test_action_renders_a_bounded_notice(ui, store, pool, monkeypatch):
    from backend.bot.handlers import ai_credentials as module

    item = await _seed(store)
    pool["credential_id"] = item.credential_id
    _patch_engine(monkeypatch, _Engine(error=_media_error("auth", http_status=401)))

    _title, body, _buttons = await module._ai_cred_test_action(None, item.handle, 1)
    assert "Key test · Unauthorized" in body
    assert "not recognition quality" in body
    assert "healthy" not in body.lower()
    _assert_no_secret(body, "the test notice")


@pytest.mark.asyncio
async def test_a_test_result_never_carries_a_transcript_or_a_body(ui, store, pool, monkeypatch, caplog):
    item = await _seed(store)
    pool["credential_id"] = item.credential_id
    _patch_engine(monkeypatch, _Engine(text="a transcript we must never surface"))
    with caplog.at_level(logging.DEBUG):
        result = await credential_service.test_credential(OWNER, item.credential_id)
    assert result.passed, "a completed request means the provider accepted the key"
    for text in (repr(result), result.state_label, caplog.text):
        assert "transcript we must never surface" not in text


# ══ 13. The existing STT stack is untouched ══════════════════════════════


def test_the_pool_exposes_the_providers_own_declared_variable_names():
    from backend.services import stt_credential_pool

    assert stt_credential_pool.env_var_names("speechmatics") == ("AI_SPEECHMATICS_API_KEY",)
    assert stt_credential_pool.env_var_names("groq") == ("AI_GROQ_API_KEY", "GROQ_API_KEY")
    assert stt_credential_pool.env_var_names("gemini") == ("AI_GEMINI_API_KEY", "GEMINI_API_KEY")
    assert stt_credential_pool.env_var_names("nobody") == ()
    assert stt_credential_pool.env_var_names("nobody") == (), "no environment sweep"


def test_the_pool_and_fallback_public_contract_is_unchanged():
    from backend.services import stt_credential_pool, stt_fallback

    for name in ("prepare", "credentials_for", "first_for", "rotation_for", "reset",
                 "is_configured", "registered_providers", "env_var_names"):
        assert callable(getattr(stt_credential_pool, name)), name
    for name in ("register_plan", "clear_registration", "reset_health"):
        assert callable(getattr(stt_fallback, name)), name


@pytest.mark.asyncio
async def test_refreshing_one_provider_never_touches_another(store, monkeypatch):
    from backend.services import stt_credential_pool

    seen: list[tuple] = []
    real_prepare = stt_credential_pool.prepare

    async def _prepare(providers=None):
        seen.append(tuple(providers) if providers is not None else None)
        return {provider: 1 for provider in (providers or ())}

    monkeypatch.setattr(stt_credential_pool, "prepare", _prepare)
    assert await credential_service.refresh_provider("speechmatics") == 1
    assert seen == [("speechmatics",)]
    assert real_prepare is not None


@pytest.mark.asyncio
async def test_a_refresh_failure_is_never_fatal(store, monkeypatch):
    from backend.services import stt_credential_pool

    async def _boom(providers=None):
        raise RuntimeError("store down")

    monkeypatch.setattr(stt_credential_pool, "prepare", _boom)
    assert await credential_service.refresh_provider("speechmatics") == 0
